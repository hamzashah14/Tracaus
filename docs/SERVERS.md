# Set up your servers for Tracaus

Tracaus reads CloudWatch logs and metrics for your EC2 instances. It never installs
anything on them. For ECS services, see [ECS services](#ecs-services) below. You install and configure the CloudWatch agent (CWAgent), a heartbeat
timer and, if you use Nginx, its logging. This page says what must exist and how to
check it. Do it before the staging canary in [DEPLOY.md](DEPLOY.md): the canary runs a
coverage check and stops if a required metric or log group is missing.

**What is supported.** Existing Linux EC2 instances, 1 to 10 of them, listed by hand in
`deployment.json`. Windows collectors are not qualified. There is no automatic discovery
of instances or autoscaling members. When an instance is replaced, update the inventory,
build a new release and cut over (see [OPERATE.md](OPERATE.md)). Terms are in the
[glossary](ARCHITECTURE.md#glossary).

## What Tracaus expects

Everything below is derived from the instance entries in `deployment.json`.

| Setting | What must exist on the instance and in CloudWatch |
| --- | --- |
| `id` | An EC2 instance that is not terminated. The generated log stream name is the instance ID |
| `log_groups` (names such as `application`) | A log group `/PROJECT/ENVIRONMENT/INSTANCE_ID/NAME`. If `log_segment` is set, it comes before the instance ID |
| `log_files` (optional) | The files the generated agent file ships, each into one of the instance's groups (see Step 1) |
| `existing_log_groups` (optional) | Log groups that already exist in the monitor region. Tracaus only reads them (see below) |
| `nginx_alarm: true` | Log groups `.../nginx-access` and `.../nginx-error`, plus the Nginx metrics below |
| `resource_alarms: true` | CPU, memory and disk metrics for alarms |
| `disk_path` | The disk metric with this `path` dimension (for example `/`) |
| `process_exe` | A process-count metric for this executable (for example `nginx`) |
| `observability` (optional) | The heartbeat file, shipped to a dedicated heartbeat log group |

Log retention is `log_retention_days`. Tracaus creates the groups named in `log_groups` and your agent
writes into them. Groups you list in `existing_log_groups` are yours: Tracaus never creates, tags or
changes them.

## Step 1: install the CloudWatch agent

1. Give the instance a role that lets the agent publish the logs and metrics above.
   Never put administrator credentials on a server.
2. Run `dry-run` from [DEPLOY.md](DEPLOY.md). It writes one agent configuration per instance to
   `.local/customer/collector-examples/INSTANCE_ID.json`. It makes no connection to your servers.
3. Install the CloudWatch agent with AWS's instructions, apply that configuration, then start and
   enable the agent service.

The generated configuration publishes, every 60 seconds, to the `CWAgent` namespace with the
`InstanceId` dimension:

- `mem_used_percent` and `swap_used_percent`.
- Disk `used_percent` for `disk_path` (published as `disk_used_percent`, with a `path` dimension).
- A process count for `process_exe` (published as `procstat_lookup_pid_count`).
- If `nginx_alarm` is true: `/var/log/nginx/access.log` and `/var/log/nginx/error.log` to the two
  Nginx log groups.
- If `observability` is configured: `/var/log/tracaus-collector-heartbeat.log` to the heartbeat group.

**Your application logs.** The file ships them when you list them in `log_files`. Each item names
a group the instance already declares (in `log_groups` or `existing_log_groups`) and an absolute
file path, which may use `*` or `?`:

```json
"log_files": [
  {"group": "application", "file": "/var/log/myapp/app.log"},
  {"group": "/myapp/prod/web", "file": "/srv/web/logs/*.log"}
]
```

- A group from `log_groups` is written as `/PROJECT/ENVIRONMENT/INSTANCE_ID/NAME`, with the
  instance ID as the stream and `log_retention_days` as retention.
- An existing group keeps its own name, gets the instance ID as the stream and is never given a
  retention setting by the agent. Streams written this way are named after the instance, so the
  default `streams: instance` finds them. Use `streams: all` for a group that something else writes,
  such as Docker's `awslogs` driver.
- The nginx and heartbeat groups are already shipped, so `log_files` cannot repeat them. Two items
  cannot be the same group and file. Paths cannot contain `..`.
- The agent runs as root by default. If you run it as another user, check that it can read each file.
- After you change `log_files`, run `dry-run` again, copy the new file to the instance and restart
  the agent.

Common starting points. They are the usual defaults and have not been checked against a live
instance, so look at your own setup:

| Application | Log source |
| --- | --- |
| Nginx | Set `nginx_alarm`. The file ships `/var/log/nginx/access.log` and `error.log` |
| Gunicorn or Uvicorn | The agent does not read the systemd journal. Log to a file: `--access-logfile` and `--error-logfile` for Gunicorn, or `StandardOutput=append:/var/log/myapp/app.log` in the unit (systemd 240 or later) |
| Node.js with pm2 | `/home/USER/.pm2/logs/*.log` |
| Docker, `json-file` driver | `/var/lib/docker/containers/*/*-json.log`. Each line is a JSON wrapper with a `log` field. All containers go to one stream |
| Docker, `awslogs` driver | Docker writes to CloudWatch itself. List the group in `existing_log_groups` with `streams: all`. The instance role needs the log write permissions, and the group must exist |
| Anything else that writes a file | That file |

## Use log groups that already exist

If your application already writes to CloudWatch (a Docker `awslogs` group, an application group
from another tool, a group your own agent file fills), list it instead of moving the logs. Add
`existing_log_groups` to the instance in `deployment.json`:

```json
"existing_log_groups": [
  {"name": "/myapp/prod/web"},
  {"name": "/myapp/prod/worker", "streams": "all"}
]
```

- **Names.** Use the exact group name, up to 8 per instance, in the monitor region and the same
  account. Wildcards and prefixes are not supported. A name under Tracaus's own log prefix is
  rejected. `log_groups` may then be empty, but an instance needs at least one group of either kind.
- **Read only.** Tracaus's stack does not create, tag, set retention on or delete these groups. The
  log tool gets read access to each one, and nothing else changes.
- **`streams`.** The default, `instance`, reads only streams named after the instance ID. That is
  the CloudWatch agent default (`log_stream_name` of `{instance_id}`), so one group can serve
  many instances and each one sees only its own lines. If the streams have other names (Docker
  uses container IDs), set `streams` to `all`. The group is then read whole, so it may belong to
  one instance only. The same group under `all` for two instances is rejected, because one
  instance could then read the other's logs.
- **Check.** The coverage check stops with "Existing log group not found" if a group is missing.
- **Not covered.** Nginx metric filters and the heartbeat stay on Tracaus's own groups. Groups in
  another account or region are not supported. Not verified: whether a KMS-encrypted group
  needs extra key permissions for the log tool role. Test one before you rely on it.

Chat finds these groups the same way as the others: asking for an instance's log groups lists
the existing ones after Tracaus's own, and a search is accepted only for the instance that lists
the group. To use the groups in the local chat mode, `scripts/make_local_tools.py` copies them
into the local tools file.

## Step 2: Nginx logging (if `nginx_alarm` is true)

- Log the standard combined format to `/var/log/nginx/access.log`, and the error log to
  `/var/log/nginx/error.log`. With `observability` configured, Tracaus parses your `match` line and
  rejects other access formats: "Unsupported access format; validate the customer's actual format".
- `nginx_filters` in `deployment.json` has an `access` and an `error` filter. Each has a `pattern`
  (a CloudWatch Logs filter pattern), a `match` sample line and a `miss` sample line. Use sanitized
  real lines from your own servers. Tracaus tests every pattern against both samples. The `match`
  line must match and the `miss` line must not.
- The `miss` sample guards against reading the byte count as a status. The example miss line has
  status 200 and 502 bytes.
- With `observability` configured, the access filter must match status 500, 502, 503 and 504.
  The coverage check rejects anything narrower: "Access filter does not cover declared
  failed-request statuses". The example in `examples/observability.example.json` does this. The
  default example in `examples/deployment.example.json` matches only 502 and 504.
- Metrics are published to `PROJECT/ENVIRONMENT/Nginx`. Without observers, both filters feed one
  metric, `nginx-upstream-errors-INSTANCE_ID`. With observers, access failures are
  `nginx-failed-requests-INSTANCE_ID` and error-log events are `nginx-diagnostic-events-INSTANCE_ID`.
  These are separate counts and separate alarms. Do not add them up as unique requests. Each alarm
  fires at 5 or more in 60 seconds.

## Step 3: the heartbeat (needed only with `observability`)

A quiet application writes few logs, so quiet logs cannot show that the collector is alive.
The heartbeat does. Run `scripts/collector_heartbeat.py` every minute on each instance, from a
supervised scheduler such as a systemd timer, with the real instance ID:

```bash
python3 /opt/tracaus/scripts/collector_heartbeat.py --instance-id "$INSTANCE_ID" --file /var/log/tracaus-collector-heartbeat.log
```

The path `/opt/tracaus/scripts` is only an example. The script appends one JSON line with the
instance ID and a UTC timestamp. It makes no AWS calls and installs no timer. CWAgent ships the file.

- Install the script and its unit root-owned, and make the file readable by the agent.
- For a systemd timer, use `OnBootSec=30s`, `OnUnitActiveSec=60s` and `AccuracySec=5s`.
  A oneshot service should run the command. Enable and start the timer, then check both statuses.
- Keep the clock synchronized (UTC) and rotate the file.
- The script accepts only instance IDs with exactly 17 hex digits. The deployment schema also
  accepts 8-digit IDs, which this script rejects.

## Step 4: readiness behavior (needed only with `observability`)

An observer calls the HTTPS routes you declare for each service. The route must return a
non-success status when an essential dependency is unhealthy. If one route cannot say that,
declare separate dependency routes (1 to 3 per service). Tracaus cannot add readiness behavior inside
your application.

Routes must be public HTTPS on port 443 with a path, with no credentials, query string or
redirect. Every DNS answer must be public. The probe checks the original TLS hostname. Private
VPC-only endpoints are not supported. Do not expose a private service just to satisfy a probe.

## Check that telemetry is flowing

1. In the CloudWatch console, open each log group. Each instance should have a stream named after
   it with recent events, and a one-line-per-minute heartbeat stream. For an existing group with
   `streams` set to `instance`, look for a stream named after the instance.
2. Open the `CWAgent` metrics. For each instance you should see `mem_used_percent`
   (`InstanceId`), `disk_used_percent` (`InstanceId` and `path`) and
   `procstat_lookup_pid_count` (`InstanceId`, `exe` and `pid_finder`). Tracaus needs these exact
   dimensions. Mismatched dimensions are the most common failure.
3. For Nginx, open the `PROJECT/ENVIRONMENT/Nginx` metrics after a test request or two.
4. If the instance sets `resource_alarms`, also expect the AWS/EC2 `CPUUtilization` metric. The
   `StatusCheckFailed` metric is always required.

Tracaus runs its own coverage check during the staging canary and again before routing is promoted.
It requires every metric descriptor above to exist and every Nginx filter to pass its samples.
It fails with "Required metric unavailable: INSTANCE_ID-memory" (or the matching id), "Required
evidence log group is absent", or "Required access metric filter failed its positive/negative
fixtures". It proves the metrics exist and the filters parse. It does not prove that an alarm
fires or an email arrives. Use [ACCEPTANCE.md](ACCEPTANCE.md) for that.

## ECS services

Tracaus reads an ECS service (Fargate or EC2 launch type) the way it reads an instance: logs and
metrics, plus one read-only tool for the service's state. Nothing is installed or changed in your
cluster. You list each service by hand under `ecs_services`, next to or instead of `instances`, with at
most 10 targets in total. EKS and standalone tasks are not supported.

```json
"instances": [],
"ecs_services": [
  {
    "id": "ecs-orders",
    "cluster": "prod-cluster",
    "service": "orders-api",
    "load_balancer": {
      "load_balancer_arn": "arn:aws:elasticloadbalancing:eu-central-1:111122223333:loadbalancer/app/orders-alb/50dc6c495c0c9188",
      "target_group_arn": "arn:aws:elasticloadbalancing:eu-central-1:111122223333:targetgroup/orders/73e2d6bc24d8a067"
    },
    "existing_log_groups": [{"name": "/ecs/orders-api"}],
    "resource_alarms": true,
    "container_insights": false
  }
]
```

| Field | Rule |
| --- | --- |
| `id` | A label you choose: `ecs-` plus 1 to 24 lowercase letters, digits or `_` (no hyphen). It appears in alarm names, chat and reports |
| `cluster`, `service` | Names, not ARNs. Both must exist in the monitor region and the service must be ACTIVE before `check` |
| `load_balancer` | **Required, so the question is always answered.** `null` when no Application Load Balancer is in front. Otherwise the ALB and the target group the service uses, both in your account and region. Network and Gateway load balancers are not supported: use `null` |
| `existing_log_groups` | Optional, up to 8 (sidecars write to their own groups). The `awslogs` groups the service writes to. Tracaus reads them whole and never creates or changes them. One log group can belong to one service, so give each service its own. Leave it out and Tracaus reads no logs for this service |
| `resource_alarms` | Default `true`. CPU and memory alarms |
| `container_insights` | Default `false`. Set `true` only if Container Insights is on for the cluster; it adds a running-task-count alarm. Container Insights is opt-in and billed by AWS, so it is not assumed |

An ECS-only deployment keeps `"instances": []`. `nginx_filters` is needed only when an instance has
`nginx_alarm`. Observers (`observability`) and `log_files` apply to EC2 instances only.

**Find the values.** You only need the cluster and the service name. Put them in `deployment.json` and run:

```bash
python -m infra ecs-discover --spec .local/customer/deployment.json --profile PROFILE
```

It is read-only. For each service it prints the `awslogs` groups of the current task definition and the
ALB and target group the service is attached to (or says there is none), as blocks to paste. It also says
what looks wrong: a log driver Tracaus cannot read, a load balancer that is not an ALB, a log group two
services share, or a file that disagrees with what AWS reports. It works on a draft file, before the
`load_balancer` key exists. It needs the operator role to describe services, task definitions and target
groups.

**Where Tracaus looks, by default.**

| Signal | Where ECS puts it | What Tracaus reads | What you do |
| --- | --- | --- | --- |
| Container logs | CloudWatch Logs through `awslogs`. The console default is the group `/ecs/<task definition family>` and the stream `<prefix>/<container>/<task id>` | The groups in `existing_log_groups` | Nothing, if `awslogs` is on |
| CPU and memory | `AWS/ECS`, per service, free | Alarms and the metrics tool | Nothing |
| Task stops | EventBridge, default bus | The `ecs-down` rule Tracaus creates | Nothing |
| Service, deployment and task state | The ECS API | `fetch_ecs` | Nothing |
| ALB errors and health | `AWS/ApplicationELB` | Alarms and the metrics tool | Answer `load_balancer` |
| ALB target health reasons | The ELB API | `fetch_ecs` | Nothing |
| Zero running tasks | Container Insights (opt-in) | A `running` alarm, only with `container_insights` | Turn Container Insights on |
| ALB access logs | S3 (opt-in) | Not read | Nothing |

**Yours to set up.** Tracaus never does these: a task definition with the `awslogs` driver pointing at an
existing log group (Fargate also needs `awslogs-stream-prefix`) and an execution role allowed to write to it;
the log group and its retention; the load balancer, its target group and health checks; Container Insights if
you want the running-task alarm. If a service logs elsewhere (FireLens to a third party, no logging), it still
works, with no log reads.

**Alarms Tracaus creates** (names `PROJECT-ENVIRONMENT-<id>-<suffix>`; like the EC2 alarms, each needs two
consecutive periods in breach): `cpu` and `memory` above 90 percent; `running` below 1 (Container Insights only);
with an ALB, `alb5xx` (target 5xx count of 5 or more per minute) and `unhealthy` (any unhealthy target). The
load-balancer-wide 5xx count has no target group dimension, so it is not alarmed: two services behind one
shared ALB would otherwise alarm on each other's errors.

**Which task stops start an incident.** One EventBridge rule watches `ECS Task State Change` for your clusters
and services. Rolling deployments and scale-in stop tasks on purpose, so they are not incidents.

| Stop code | Incident |
| --- | --- |
| `TaskFailedToStart`, `EssentialContainerExited` (a crash, an OOM kill, a task that exited) | Yes |
| `SpotInterruption`, `TerminationNotice`, `InfrastructureHealth` (the host went away) | Yes |
| `ServiceSchedulerInitiated` | Only when the reason names a health check. A deployment or scale-in is routine |
| `UserInitiated` | No: a person stopped it |
| Missing or a code Tracaus does not know | Yes |

A crash loop folds into one incident through the per-target cooldown ([OPERATE.md](OPERATE.md#repeated-and-overlapping-alarms)).

**What the investigation reads.** `fetch_logs` and `fetch_metrics` work as for an instance. `fetch_ecs` adds the
service's deployments and rollout state, recent service events, tasks that stopped in the window with stop code,
stopped reason and container exit codes, a summary of the task definition (never environment values or secrets)
and, with an ALB, the health of each target with its reason.

**Limits.** Local tools mode (development chat) covers EC2 only. Observers, Nginx filters and heartbeats are EC2
only. A task definition that changes its log group leaves reports without log evidence until you update the file
and cut a new release; `ecs-discover` shows the difference. Not verified on real ECS: the event and metric
shapes follow AWS's documentation; whether `RunningTaskCount` reports 0 when a service has no tasks; the exact
stopped-reason text of a failed health check; and the IAM scoping of the ECS read actions.

## What the observers check

Observers are three optional Lambda functions in your account: a health probe, a notification
canary and a receipt consumer. Set `observability.enabled` to true to switch on their schedules
and alarm actions.

- **Health.** Each service route is probed. The result is an `Availability` metric per service
  and route. A `TelemetryFresh` metric per service is true only when both the declared CWAgent
  metric (`collector_metric_id`) is fresh and a valid shipped heartbeat is within
  `freshness_seconds`. Both are needed. Idle application logs never count as a heartbeat.
- **Delivery.** The canary sends a synthetic notification through the alert path. A separate
  consumer records its receipt. The observer checks the due times, whether the primary and
  fallback subscriptions are confirmed, and whether a person has attested a fresh inbox receipt
  (`attest-email`, see [ACCEPTANCE.md](ACCEPTANCE.md)).
- **Alarm timing.** A service health or freshness alarm needs two bad periods. A missing observer
  heartbeat needs three. A missed daily canary can take up to a day plus its deadline to show.
- **Limits.** The observer Lambda has a 180-second timeout and stops its own checks at about 150
  seconds. Incomplete checks raise explicit attention alarms. A maximum-size inventory still needs
  staging qualification.

Settings and their allowed ranges are in [DEPLOY.md](DEPLOY.md). Recovery and alarm response are in
[OPERATE.md](OPERATE.md).
