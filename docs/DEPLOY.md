# Deploy Tracaus in your AWS account

> **Status.** The deployment tools have only been tested locally. They have never been run
> against a real AWS account, so expect rough edges. Deploy to a staging environment first,
> start with a small pilot, and do not treat the result as production-ready.

Tracaus runs in your own AWS account. You run the deployment commands from your own workstation,
you pay the AWS bill, and the maintainers operate nothing. This is the only deployment guide.
Follow it in order, from an empty account to a first working UI. Terms are in the
[glossary](ARCHITECTURE.md#glossary).

## Choose your setup

You make three choices. Each has a default. The three defaults together are the shortest path,
and sections 1 to 6 cover that path from start to finish. Read sections 7 to 9 only if you pick
an option.

| Choice | Default (the shortest path) | Option | Where |
| --- | --- | --- | --- |
| Who signs in to the UI | **Local single-user mode.** You run the UI on your own machine and protect it with one shared password. There is no identity provider | **Team mode.** Several people share one UI. Each signs in through your identity provider and sees only the instances on their allowlist entry. One small file on the UI host, no AWS resources | Section 7 |
| Which model | **Amazon Bedrock** | **A model API:** an OpenAI-compatible or Anthropic Messages endpoint, with its key in AWS Secrets Manager. Your prompts leave your AWS account | Section 8 |
| Which runtime | **`standalone`:** Lambda functions | **`agentcore`:** Amazon Bedrock AgentCore hosts. Bedrock models only | Section 9 |

The options combine freely, with two exceptions. A model API works only with `standalone`, and the
tool rejects the other combination. Team mode also needs `standalone`: the UI reports a
configuration problem for `agentcore`.

Team mode is on if, and only if, the UI host sets `TRACAUS_TEAM_FILE` (section 7; the launcher sets it from `--team-file`). The deployment
tool knows nothing about it and creates nothing for it.

## Who does what

| Who | What they do |
| --- | --- |
| You, the administrator | Run the commands in this guide and own the AWS bill |
| Your AWS account | Hosts everything Tracaus creates: Lambda functions, queues, DynamoDB tables, S3 buckets, secrets, alarms |
| Your monitored servers | You install the CloudWatch agent and a heartbeat on each ([SERVERS.md](SERVERS.md)). Tracaus never installs software on them |
| Your mailboxes | People confirm subscription emails and check that alerts really arrive |
| Your identity provider (IdP), optional | Only with team mode: you register Tracaus's web UI there and enforce MFA. Tracaus cannot do that for you |
| Your model API provider, optional | Only with a model API: you hold the account and the API key, create the secret that stores it, and pay the provider |

**The whole path (default)**

1. Check the prerequisites (section 1) and read the cost notes (section 2).
2. Set up telemetry on your servers ([SERVERS.md](SERVERS.md)).
3. Install, generate settings, fill them in, dry-run and check (steps 3.1 to 3.5).
4. Run `apply`. It stops at "Candidate ready" (steps 3.6 and 3.7).
5. Resume `apply` with `--allow-model-invocation` to run the paid canary (step 3.7).
6. Confirm the emails and resume until it reports ready (steps 3.7 and 3.8). Then move on to
   [ACCEPTANCE.md](ACCEPTANCE.md).
7. Open the UI on your own machine (section 4).

Team mode adds an IdP registration and a `team.toml` file on the UI host (section 7). A model API
adds a secret that you create before `check` (section 8).

## Guided path: three commands

`./bin/tracaus setup`, `deploy` and `ui` wrap sections 3 and 4 below. Use them first; the manual commands remain the
fallback and nothing in them changed. Each command prints numbered steps, says what it is about to do, explains every
failure (what failed, why, what is safe, what to do next) and can be run again.

| Command | What it does | What you answer |
| --- | --- | --- |
| `setup` | Reads EC2 and ECS (read-only), writes `deployment.json`, `runtime.json`, `automation.json`, builds the plan, and reports what is missing on each server or task | The profile and account, what to watch, the ALB question per ECS service, alert emails, the model, and whether to create the IAM roles |
| `deploy` | Re-checks the plan, runs `check`, asks once, then runs `apply` and shows each stage as it completes | Yes to apply; a typed `yes` to the paid model request; confirming the subscription emails |
| `status` | Read-only: stages, whether anything is routed, stack statuses, the next command | nothing |
| `ui` | Adds the UI profiles, creates a password (shown once, saved mode 600), starts the UI on `127.0.0.1` | whether to write the profiles |

**Does it need an administrator?** Only once, to create the three IAM roles, and only if they do not exist. That needs a
profile that can create CloudFormation stacks with `CAPABILITY_NAMED_IAM` and IAM roles. `setup` asks which profile to use,
asks you to type `yes`, uses it for that one stack and never again. If you cannot, it prints the command for an
administrator and waits. Your own sign-in must be allowed to assume the operator role: the roles trust your own
principal when `setup` can read it, otherwise the whole account (with a warning). Bedrock model access and a Lambda
quota raise happen outside the tools and can need an administrator.

**Files.** All under `.local/customer/` (override with `--work-dir`): the three settings files, `answers.json` (your
answers, no secrets, reused as defaults), `discovery.json`, `plan.json`, `plan.hash`, `collector-examples/`,
`guided.log` (everything printed, redacted), and after `deploy` the journal and `ui-connection.json`.

**If something fails.** Nothing is routed to Tracaus before the `routing` stage, so an earlier failure leaves your alarms
and servers untouched. CloudFormation undoes a failed stack change by itself, and the journal keeps what is done:
run `deploy` again to resume. The commands never delete queues, keys, evidence, subscriptions or alarms; the only AWS
resource `setup` may remove is its own empty role stack after a failed first creation, and only after you agree. A changed
source or setting needs a new release (`refresh`, section 3.9). To roll back or retire a release, see
[OPERATE.md](OPERATE.md#roll-back-and-retire-old-releases).

| Flag | Meaning |
| --- | --- |
| `--work-dir PATH` | Where the settings live (under `.local/`) |
| `--answers FILE` | Replay an `answers.json` without asking; stops on any missing answer |
| `--yes` | Accept defaults. It never answers IAM or paid questions |
| `--allow-iam-bootstrap`, `--allow-paid-canary` | Consent, without asking, to creating the roles or the first paid model request |
| `--allow-paid-canary-retry` | Separate consent to a SECOND paid request after an expired or unclear first one. `--allow-paid-canary` never covers it |
| `--admin-profile NAME` | `setup`: the profile that may create the IAM roles |
| `--skip-install`, `--skip-check` | `setup`: skip the package download. `deploy`: skip the read-only permission check |
| `--team-file FILE`, `--password-prompt` | `ui`: team mode, or type the password instead of generating one |
| `--debug` | Show the full trace of an unexpected error |

Exit codes: 0 done, 1 failed, 2 waiting on a named step, 3 refused by a safety rule, 130 interrupted. This path has
only been tested with stubbed AWS: it has never run against a real account.

## 1. What you need before you start

Read [PREREQUISITES.md](PREREQUISITES.md) first. It lists what must already exist before you run
the deploy tool: the AWS account, regions and quotas, the three IAM roles, your monitored servers,
your workstation, the private configuration files, and the people you need. It also shows how to
check each item. Return here when its quick checklist is complete and `dry-run` passes.

## 2. What it costs

You pay for everything Tracaus creates. This guide gives no price estimate, because cost depends on
your account, regions, model, fleet, schedules, retention and usage. Review these drivers:

- Bedrock inference (model, tokens, tool calls), and AgentCore if you choose it. With a model API
  you pay the provider instead, outside your AWS bill (section 8).
- CloudWatch log ingestion, Logs Insights queries, metrics, alarms and the dashboard.
- Lambda, DynamoDB (including point-in-time recovery), S3 versioning, KMS, Secrets Manager, SNS
  and SQS.

Retained storage, keys and alarms keep costing money even when investigations and observers are
paused. Token reservations and query counts bound the work. They are not a dollar cap. Tracaus has
no pre-query maximum on the bytes Logs Insights scans, and billing alerts do not shut anything
down.

Before you apply, set a budget alarm for the account in AWS Budgets (Tracaus does not create one).
After one week of staging, review actual cost by service in Cost Explorer before you enable anything
more. To pause model work and keep alerts, see
[maintenance, pausing and spend](OPERATE.md#maintenance-pausing-and-spend).

## 3. Deploy with the automation tool

This is the primary path. The sequence is: **init, fill three private files, dry-run, check,
apply, finish the named human steps, resume apply.** Every command is safe to rerun. Interrupted
work keeps its progress. Run everything from the repository root.

The tool deploys the backend only. It does not install software on servers, create an EC2 host
for the UI, confirm inboxes, or (with team mode) register your IdP.

### 3.1 Install and prepare

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # run again in each new terminal; the prompt shows (.venv)
python -m pip install --require-hashes -r requirements/dev.lock
python -m pip download --require-hashes --only-binary=:all: --dest .build/wheels -r requirements/lambda.lock
```

The first commands create and activate a virtual environment, then install the deployment tools.
Every command in this guide assumes you run it from the repository root with that environment
active. The last command downloads hash-verified Lambda packages once. `apply` builds every package
(inventory-bound tools, six pipeline functions, the ARM64 AgentCore host and the observers, when
configured) from those wheels. It never installs or upgrades application dependencies.

Commit the reviewed source before `dry-run`. The plan records the commit, so a new commit after
`dry-run` means a new plan hash. Private files live under `.local/`, which Git ignores.

### 3.2 Generate the private files

```bash
python -m infra.deploy.automation init --work-dir .local/customer
```

This makes the folder (mode 700) and three owner-only files (mode 600). It makes no AWS call and
never overwrites existing files. The work directory must be under `.local/` in this checkout.
`init` copies `examples/durable.example.json` as `runtime.json`.

| File | What it holds |
| --- | --- |
| `automation.json` | Which AWS profile to use, and where the other files and the wheels are |
| `deployment.json` | Account, regions, release ID, model, IAM roles, server inventory, Nginx filters, alert addresses |
| `runtime.json` | Status URL, optional fallback address, retention, capacity, model and tool limits |

The examples are synthetic (`reference_only: true`, `.invalid` addresses). `check` and `apply`
reject them before they touch AWS. Keep every real value, secret and receipt out of Git.

### 3.3 Fill in the three files

All relative paths resolve against `automation.json`, not your terminal's folder.

**`automation.json`.** It must contain exactly these five keys, or you get "Automation
configuration has unknown or missing fields".

| Key | Value |
| --- | --- |
| `version` | `1` |
| `spec`, `runtime_config` | Paths to `deployment.json` and `runtime.json` (the generated names) |
| `profile` | AWS profile name (letters, digits, `_ . @ -`, up to 128 characters), or `null` for the standard credential chain. A named profile is used for every check and subprocess, and ambient static keys cannot override it |
| `wheelhouse` | Path to the verified wheels. `init` writes the absolute path of `.build/wheels` in your checkout |

```json
{
  "version": 1,
  "spec": "deployment.json",
  "runtime_config": "runtime.json",
  "profile": "customer-deployment",
  "wheelhouse": "../../.build/wheels"
}
```

Run the profile as a session of the operator role (`ci_principal_arn`). `check` fails with "Run
with the configured CI/operator role credentials" otherwise.

**`deployment.json`.** Set `reference_only` to `false`. Keep `executor_mode` at `qualified`.

| Group | Settings and rules |
| --- | --- |
| Names and places | `project`: 2 to 12 lowercase letters or digits, starting with a letter. `environment`: `development`, `staging` or `production` (only `staging` finishes automatically, see 3.8). `account_id`: 12 digits. `monitor_region`, `bedrock_region`. `release_id`: 1 to 16 lowercase letters, digits or hyphens. A release is immutable, so use a new `release_id` for any new code or configuration. Resource names must stay within 64 characters |
| Roles and recipient | `ci_principal_arn`, `deployment_role_arn`, `ui_principal_arn`: explicit, different roles in `account_id`. `notification_email` (one address) or `notification_emails` (a list of 1 to 5), not both: everyone listed receives the alerts and reports |
| Model (Bedrock, the default) | `model_id` and `model_arns` (1 to 10 ARNs of a foundation model or inference profile; a profile must belong to your account, and every model behind a profile must also be listed). If `model_id` is an ARN it must be in `model_arns`. To use a model API instead, see section 8: it replaces these fields |
| Switches | `maintenance_mode`: keep `false` (true disables routing). `reserved_concurrency`: leave `null`. 0 needs `maintenance_mode`, and the value is only used for a headroom check |
| Retention | `log_retention_days`: 7, 14, 30, 60, 90, 180 or 365. `log_segment`: empty, or up to 24 letters, digits, `_` or `-`, added to log group names |
| Servers | `instances`: 0 to 10 (at most 10 targets with `ecs_services`, and at least one target). Each needs `id` (`i-` plus 8 or 17 hex digits), `log_groups` (up to 8 names of up to 40 letters, digits, `_` or `-`, which Tracaus creates), `disk_path` (absolute path such as `/`), `resource_alarms`, `nginx_alarm`, `process_exe` (`null` or an executable name). Optional: `existing_log_groups`, up to 8 log groups that already exist and that Tracaus only reads (see [SERVERS.md](SERVERS.md#use-log-groups-that-already-exist)), and `log_files`, up to 16 files the generated agent file ships (see [SERVERS.md](SERVERS.md#step-1-install-the-cloudwatch-agent)). Each instance needs at least one group of either kind |
| ECS services | `ecs_services` (optional, up to 10): each has `id` (`ecs-` plus up to 24 letters, digits or `_`), `cluster`, `service`, `load_balancer` (**required**: `null`, or the ALB and target group ARNs) and optionally `existing_log_groups`, `resource_alarms`, `container_insights`. Find the values with `python -m infra ecs-discover`. See [SERVERS.md](SERVERS.md#ecs-services) |
| Nginx | `nginx_filters.access` and `.error`, each with `pattern`, `match` (a sample line that must match) and `miss` (one that must not). Required only when an instance has `nginx_alarm: true`. See [SERVERS.md](SERVERS.md) |

**ECS services.** Put each service's `id`, `cluster` and `service` in `deployment.json` (keep
`"instances": []` if you have no EC2 instances), run `python -m infra ecs-discover --spec .local/customer/deployment.json`
and paste the `existing_log_groups` and `load_balancer` it prints. `check` then confirms every service exists, is
ACTIVE and uses the ALB you declared. An ECS deployment gets a third tool Lambda (`fetch_ecs`) and an `ecs-down`
EventBridge rule. Nothing about ECS has run against real AWS.

The telemetry must match the inventory exactly: instance IDs, log groups, metric dimensions
(`InstanceId`, disk `path`, process `exe`). The generated agent examples help, but installing
and configuring agents stays your job.

**`runtime.json`.** Created from `examples/durable.example.json`.

| Group | Settings and rules |
| --- | --- |
| Alerts | `status_base_url`: a fixed `https://` URL. `fallback_email` (optional): one address for the two fallback topics, which carry alarms about Tracaus's own pipeline. Omit it and those topics use the same addresses as the reports |
| Retention and capacity | `retention_days`: 7 to 365. `initial_reserved_concurrency`: 2 to 1000 (capacity kept for initial notifications) |
| Repeated alarms | `incident_cooldown_minutes` (optional): 0 to 120, default 15. One open incident per instance: later alarms inside the window are stored and counted on it, with no new investigation or email. 0 investigates every alarm ([OPERATE.md](OPERATE.md#repeated-and-overlapping-alarms)) |
| Model pause | `investigation_paused`: must be `true`. The tool rejects `false`: "Initial deployment must keep investigation_paused true; activation is a separate qualified release" |
| Runtime | `runtime_target`: `standalone` (default) or `agentcore` (section 9) |
| `runtime_limits` | All eight fields are required. Each is at least 1 and at most: `tokens_reserved` 100000, `model_steps` 16, `tool_calls` 16, `log_queries` 48, `output_tokens` 4096, `window_minutes` 30 (minutes on each side of the incident time), `context_bytes` 64000, `tool_bytes` 20000. `output_tokens` must be below `tokens_reserved`. The example uses 32000, 8, 8, 24, 1024, 15, 48000 and 20000 |

These are limits and reservations, not measured production sizing. Team mode adds nothing to
`runtime.json`.

**Optional: observers.** Observers add health probes and notification canaries. To use them, copy
the `observability` object from `examples/observability.example.json` into `deployment.json` and fill
it in. That example file also widens `nginx_filters.access` to match 500, 502, 503 and 504. Copy
that too: with observers, the coverage check requires all four. Keep `enabled` at `false` for the
first deployment (it keeps schedules and alarm actions off). Changing it later changes the
release. Every field is required:

| Field | Rule |
| --- | --- |
| `interval_minutes` | 5 to 15 |
| `canary_interval_minutes` | 60 to 1440, in whole hours that divide a day (1440 means daily at 00:00 UTC) |
| `receipt_deadline_seconds` | 300 to 1800, and at least one interval |
| `email_receipt_max_age_hours` | 24 to 168 |
| `services` | 1 to 10. Every instance needs one. Each has `id`, `instance_id`, `owner`, `collector_metric_id`, `heartbeat_log_group`, `freshness_seconds` (300 to 1800) and `routes` |
| `routes` | 1 to 3 per service, each with `id`, `url`, `timeout_seconds` (1 to 5), `latency_ms` (up to the timeout) and `statuses` (200 to 299). The URL is public HTTPS on port 443 with no credentials or query |

`collector_metric_id` must name a declared CWAgent metric on that instance (for example
`INSTANCE_ID-memory`). `heartbeat_log_group` must be one of that instance's `log_groups`, and every
service on an instance shares one. The whole block can be at most 2500 bytes.

**No roles yet? Create them with one template.** Write the three role ARNs you want into
`deployment.json` first (any names, for example `role/tracaus-staging-operator`,
`role/tracaus-staging-cfn-execution` and `role/tracaus-staging-ui`). Then render the template. It makes no
AWS call:

```bash
python -m infra bootstrap-iam --spec .local/customer/deployment.json --output .local/customer/iam.template.json
```

An AWS administrator deploys it once, with the command the tool prints:

```bash
aws cloudformation deploy --template-file .local/customer/iam.template.json \
  --stack-name PROJECT-ENVIRONMENT-iam --capabilities CAPABILITY_NAMED_IAM --region MONITOR_REGION
```

After that the administrator is no longer needed: every later step runs as the operator role (see the
profile example in [PREREQUISITES.md](PREREQUISITES.md#34-profiles)).

| Option | Effect |
| --- | --- |
| `--operator-trust ARN`, `--ui-trust ARN` | Who may assume the operator role and the UI principal role. Repeat the option for several. The default is your whole account (`arn:aws:iam::ACCOUNT:root`), which still needs an IAM policy that allows `sts:AssumeRole`. Narrow it to your administrators and the people who run the UI |
| `--agentcore` | Adds the AgentCore runtime actions to the execution role. Use it only with `runtime_target` `agentcore` |
| `--instance-role` | Also creates an instance role and profile for the monitored servers: AWS's `CloudWatchAgentServerPolicy` plus a deny on creating log groups under your project path, so only Tracaus's stack creates them. Attach the profile to each instance |

What the roles can do:

- **Operator.** Change and read stacks named `PROJECT-ENVIRONMENT-*`, pass the execution role to
  CloudFormation only, upload and read release files in the project buckets, invoke the project's
  functions for the canary, move secret version stages, and read what `check` inspects. It cannot
  create roles or functions itself.
- **CloudFormation execution.** Only the CloudFormation service can assume it. It creates the
  resources whose names start with `PROJECT-ENVIRONMENT-`, and IAM roles only under the path
  `/PROJECT/ENVIRONMENT/`, with inline policies and no managed-policy attachment. Because it can create
  roles, treat it as a powerful role and keep the stack's template review (`dry-run`) in the process.
- **UI principal.** May only assume the role Tracaus generates for the UI.

The template allows more than the screened actions where an update or delete needs it (for example
`s3:*` on the project buckets) but always within the name prefix, except for the few APIs that accept
no resource scope. Tests confirm that every request in the `check` permission screen is covered, and
`check` then simulates them against the real roles before anything is deployed. Whether the roles
suffice for every real stack operation is not verified: the template has never been deployed to AWS.

### 3.4 Preview without deploying

```bash
python -m infra.deploy.automation dry-run --config .local/customer/automation.json --work-dir .local/customer
```

This makes **no AWS calls and no AWS writes**. It validates your files and writes `plan.json`. It
also writes `collector-examples/INSTANCE_ID.json`, one CloudWatch agent configuration per server.
It prints a `plan_hash`. You need that hash for `apply`.

Read `plan.json` before going on. It lists the ordered steps, stack names and regions, the exact
bootstrap templates, the permission screen and the steps left to you. The
ordered steps are `foundation-tools`, `foundation-monitor`, `durable-foundation`, `owned-tools`,
`durable-runtime`, `candidate-verification`, `staging-canary`, `routing`,
`registration-verification` and `manual-acceptance`. Observers add their own steps. A model API
adds a `model-secret` step before `owned-tools`.

The plan is not a price quote, proof of permissions or a real CloudFormation change set. Later
templates need real outputs, so their exact form appears during `apply`, where each real change
set is inspected and saved privately. Changing any setting or the source commit changes the plan
hash.

### 3.5 Check your account (read-only)

```bash
python -m infra.deploy.automation check --config .local/customer/automation.json --work-dir .local/customer
```

This calls AWS with read-only requests. It checks that you are in the right account and role,
that the three IAM roles exist (and the execution role trusts CloudFormation), that both regions
are enabled, that every declared EC2 instance exists, and Lambda concurrency headroom. With
Bedrock it also reads the model's metadata. With a model API it instead checks that the key secret
exists and has one current version (section 8). Then it simulates the screened actions, including
`iam:PassRole` for the intended service. Any denial, missing context or failed simulation blocks
`apply`. It writes `preflight.json` and exits 0, or 1 with a blocker count. API failures write
`error.json` with the operation and error code only.

This is a conservative screen. It is **not** a complete policy builder or a guarantee. It
cannot see organization policies and session policies. Do not attach AdministratorAccess to clear a
blocker. Have your security administrator review `preflight.json`. See the AWS note on
[IAM policy simulator limits](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_testing-policies.html).

### 3.6 Apply the reviewed plan

`apply` creates billable resources. Read `plan.json` first. Use the hash from your own `dry-run`:

```bash
python -m infra.deploy.automation apply --config .local/customer/automation.json --work-dir .local/customer --plan-hash YOUR_PLAN_HASH
python -m infra.deploy.automation status --work-dir .local/customer
```

`apply` checks that the hash and the source match, that the checkout is clean and that the preflight
passes. Then it works through these steps:

1. Builds the packages from the wheels.
2. Creates the foundations: two regional bootstrap stacks, the incident database, evidence storage
   and queues. It collects the outputs and the exact version of the log-cursor secret that AWS
   generates. With a model API it also pins the current version of your key secret (section 8).
3. Uploads versioned artifacts, then creates and seals the immutable tool and runtime releases.
   With observers it also creates the observer stacks and seeds the health checks.
4. Verifies the candidate.
5. **Stops** for the human step in 3.7. It then continues with the canary, routing and
   registration checks, and writes `ui-connection.json` as its last step.

AgentCore adds steps of its own (section 9).

A CloudFormation stack is a group of resources managed together. A change set is its preview. The
approved plan authorizes additive steps without a prompt for each one. Every change set is still
inspected against the template, role and account. Deletions, replacements, foreign owners, failed
stacks and drift stop the tool. It never deletes queues, evidence or subscriptions to recover.
Stage names and their immutable or updateable status are in Appendix A.

**Waiting and exit codes.** `apply` waits up to 900 seconds by default and polls every 5 seconds.
Use `--wait-seconds 0` to advance once, or any value up to 3600. When AWS is still working, it
returns `WAITING` with exit code 2. Run the same command again to resume. A human step returns
immediately.

**The work directory.** `.local/customer` holds `state.json` (the resume journal), `bundle/`
(rendered templates and `bundle.json`), `build/`, `bindings.json`, `preflight.json`, change-set
reviews, the canary receipt, `operations.log` and `ui-connection.json`. Keep it, and never edit or
discard the journal to force a retry. A file lock stops two processes from using one directory.
Never run the same release from two directories at once.

### 3.7 The human steps

`apply` stops for these. None can be skipped.

**A. "Candidate ready" (exit 2).** The backend candidate is built and verified. The message is
"Candidate ready: resume with --allow-model-invocation". Run `apply` again with the same config
and plan hash and one more flag:

```bash
python -m infra.deploy.automation apply --config .local/customer/automation.json --work-dir .local/customer --plan-hash YOUR_PLAN_HASH --allow-model-invocation
```

This **spends money**: one bounded model request that calls the staging Investigate function. It
must use both the log and metric tools and return a valid diagnosis. Before it, a coverage check
must pass. Your servers must already publish the metrics, or it stops with "Required metric
unavailable". With a model API, the provider bills this request, not AWS. You may add
`--allow-model-invocation` to the very first `apply` and skip this stop.
Leave it off the first time if you want to check your telemetry before the paid call.

**B. Confirm the emails.** AWS sends one subscription email per address and topic. The fallback
addresses (the `fallback_email`, or your alert addresses if you set none) get one for the incident
fallback topic (early in the run) and one for the observation fallback topic if you configured
observers. The alert addresses get their report-topic email only when the routing stage runs, near
the end. Confirm each one when it arrives. The registration check requires every subscription
to be confirmed. The first `apply` that reaches routing therefore stops with `WAITING` (exit 2):
"Confirm the subscription emails sent to the addresses configured as notification_email ..., then
resume apply with the same plan hash". It names the settings, never the addresses. After you
confirm, run the same command again. Nothing is created again; the earlier steps are only
rechecked, then the registration check runs. Do this within one hour of the canary (see C). Any
other subscription problem (another address or protocol, a missing or unsubscribed subscription)
is still `FAILED`.

**C. The canary receipt.** A passing canary writes a receipt that is valid for one hour. Every
later `apply` checks it. If it is older, you see "Canary receipt expired/differs". Then you
need `--retry-canary` together with `--allow-model-invocation`, which is a second paid call. A
canary that fails for any reason, even before a model call (for example missing telemetry), is
recorded as ambiguous. After you fix the cause, resume with `--retry-canary` and
`--allow-model-invocation`. Retries keep the previous receipts in the journal.

Two more stops can appear. "Legacy resource retirement requires separate review" means the
retirement plan names alarms or subscriptions to remove. The tool never deletes them, so review
them with the commands in Appendix A. Production environments stop as described in 3.8.

### 3.8 Status, finished and what comes after

| Status | Exit | Meaning |
| --- | --- | --- |
| `RUNNING` | n/a | A run was interrupted. Run `apply` again |
| `WAITING` | 2 | AWS is in progress, or a named human step is next. Resume with the same config and hash |
| `FAILED` | 1 | A prerequisite, API call or gate failed. Read `operations.log`, `preflight.json` or `error.json` privately, repair, then resume. `AccessDenied` never means "absent" |
| `INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING` | 0 | Provisioning and registration passed. Automatic investigation is still paused |

`status --work-dir .local/customer` prints the status, the next action and the plan hash. It makes
no AWS call. If you change a setting or the commit after `apply` started, the old journal no longer
applies: "Configuration/source changed; create a new deployment work directory". Use a new work
directory and a new `release_id`; `refresh` (3.9) does both.

**What finished means.** Only `environment: staging` can reach the last row. It means the backend
exists and is registered. The initial deployment always keeps `investigation_paused` true. It does
**not** mean production qualification, and it does not prove an email reached a person. Do the
inbox and acceptance checks in [ACCEPTANCE.md](ACCEPTANCE.md).

**Other environments.** For `production` or `development` the tool provisions the candidate and
stops with `WAITING`: "Production candidates provisioned; existing gates require separately
qualified staging and production cutover". The message says "production" even for `development`.
No flag continues past it, because the paid canary and the promotion receipt exist only for
staging. The tool writes no `ui-connection.json` for these environments (it writes
one only at the end of a staging run). This repository has no command that promotes a production
bundle. Qualify a staging release first, then treat production cutover as a separate, reviewed
customer process.

**Enabling automatic investigation** is also outside the tool. It needs a changed release, a new canary
and a reviewed routing change. Appendix A lists the manual commands. They have not been run against
real AWS.

After the last row, open the UI (section 4).

### 3.9 Watch more servers, or change anything else, later

The server list is part of the release, so any change to it, such as adding or removing a server, a log
group or a service, is a new release. It is not an edit in place. Start it from the finished one:

```bash
python -m infra.deploy.automation refresh --from-dir .local/customer --work-dir .local/customer-r2
```

This copies `deployment.json`, `runtime.json` and `automation.json` into the new folder with the release
ID counted up (`example001` becomes `example002`; pass `--release-id NAME` to choose). It copies nothing
from the old run: not its plan, its journal, its outputs or `ui-connection.json`. It never overwrites, and the
old folder is not touched. Then:

1. Edit `.local/customer-r2/deployment.json`: add the instances, their `log_groups` and any alarms.
2. Install the CloudWatch agent on each new server with the file `dry-run` writes (see [SERVERS.md](SERVERS.md)).
3. Run `dry-run`, `check` and `apply` with the new folder, as in 3.4 to 3.6.
4. With team mode, add the new instance IDs to `team.toml`. A listed instance must be in the deployed list.
5. When the new release works, retire the old one ([OPERATE.md](OPERATE.md#roll-back-and-retire-old-releases)).

A separate project or team in the same AWS account uses a different `project` or `environment` in its own
work directory. That gives it its own resources, but the account's Lambda concurrency is shared, so each
deployment's `initial_reserved_concurrency` must fit what is left. This has not been tried on real AWS.

## 4. Open the UI on your own machine

Only want to try chat without deploying anything? Use the README recipe
[Try it against your own CloudWatch](../README.md#try-it-against-your-own-cloudwatch-no-deployment)
instead of this section. It is for development only.

This section is for default mode (team mode: section 7). The tool writes `ui-connection.json`
as its last step, so start here when `status` shows
`INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING`.

**1. Give the UI an AWS profile.** The UI process must not use deployment credentials. Its profile
assumes the `ui_role_arn` from `ui-connection.json`. That role trusts only your `ui_principal_arn`.
The launcher checks the account and the assumed role and refuses anything else. A standard AWS
profile looks like this (adjust to how your organization signs in):

```ini
[profile customer-ui]
role_arn = ROLE_ARN_FROM_ui-connection.json
source_profile = A_PROFILE_THAT_ASSUMES_YOUR_UI_PRINCIPAL_ROLE
```

**2. Choose a password and launch.** Default mode signs in with one shared password. Export it in
the shell that starts the launcher. It must have at least 12 characters. The launcher reads no
`.env`, so a value there is ignored, and it prints a hint to stderr if `APP_PASSWORD` is missing.

```bash
read -rs APP_PASSWORD && export APP_PASSWORD   # type a private password; it is not shown
python scripts/run_customer_ui.py --connection .local/customer/ui-connection.json --profile customer-ui
```

The launcher binds the UI to `127.0.0.1` and copies no AWS credentials. Open the address it prints
(Streamlit's default is `http://127.0.0.1:8501`) and sign in with the password. A password session
ends after 30 minutes. An alert email links to `status_base_url` plus `?incident=ID`. If that
address does not reach your machine, open the same `?incident=ID` on the local address instead.

**What default mode does, and what it does not.**

- Chat runs inside the UI process, with the UI role's AWS credentials. It calls the pinned tool
  functions and the model directly. (With `agentcore` the UI calls the AgentCore endpoint instead,
  section 9.)
- These protections still apply: the instance allowlist (`ALLOWED_INSTANCE_IDS`), pinned tool
  versions, bounded limits (`RUNTIME_LIMITS`), release binding, the diagnostic policy and
  redaction. They come from the generated connection file.
- The only throttle is 20 requests per hour for each browser session. There is no per-person
  sign-in, audit line or shared limit.

**Do not host a default-mode UI for other people.** It is protected by one shared password. Anyone
who knows that password can start model and tool calls with the UI role's AWS permissions, and can
read every incident report. Keep it on `127.0.0.1`, or put it behind your own SSO or VPN proxy. If
several people need their own access, use team mode (section 7).

## 5. Troubleshooting

Most commands print a short, safe message. Details stay in private files: `operations.log`,
`preflight.json`, `error.json` and `state.json` in the work directory. Never paste them into
public places.

| Message or symptom | What it means and what to do |
| --- | --- |
| "Deployment files must live under ignored .local/", "Init never overwrites existing customer files" | `--work-dir` must be inside `.local/` of this checkout. For a fresh start use a new work directory |
| "Synthetic reference inputs cannot check/deploy AWS" | `reference_only` is still `true`, or the examples are unchanged |
| "Deployment failed (ValueError); no success claimed. Inspect private evidence." | A setting in `deployment.json` or `runtime.json` breaks a rule in 3.3, and the automation tool does not say which. Run `python -m infra.deploy.durable --spec .local/customer/deployment.json --config .local/customer/runtime.json --output .local/customer/check-render`. It makes no AWS call and ends with the exact message. Examples: "Invalid deployment field: NAME", "UI, CI and deployment identities must be distinct", "Model ARN must be included in model_arns", "Invalid durable URL, distinct fallback recipient or retention", "Invalid runtime limit: NAME" |
| "Automation configuration has unknown or missing fields", "Invalid AWS profile name", "Supply spec, runtime configuration and verified wheelhouse paths" | `automation.json` must have exactly the five keys in 3.3, with valid values |
| "Initial deployment must keep investigation_paused true; activation is a separate qualified release" | Set `investigation_paused` to `true` in `runtime.json` |
| "Team sign-in changed: remove the identity, security and initial_access settings. Team access is now a team.toml allowlist on the UI host; see docs/DEPLOY.md section 7." | An older `runtime.json` still has an `identity` or `security` block, or an older `automation.json` still has `initial_access`. Delete those keys. Team access is now the `team.toml` file on the UI host (section 7), and the deployment tool no longer deals with it. Stacks and resources that an earlier release created for that sign-in stay in your account, because the tool never deletes foreign resources: remove them yourself (the dedicated chat function, the AgentCore chat runtime and endpoint, the session table, the signing secret, the audit bucket and trail, and the issuer role). Do not reuse an old `ui-connection.json`; the UI refuses to start with one |
| "Run dry-run and supply its exact --plan-hash before apply" | Rerun `dry-run` and copy the new hash. Any change to settings or the commit changes it |
| "Apply requires a clean reviewed source checkout" | Commit or remove every change, including untracked files |
| "Wrong AWS account; no writes permitted", "Run with the configured CI/operator role credentials" | The profile points at another account, or is not a session of `ci_principal_arn` |
| "Configured IAM role is absent or has a different path", "Deployment role does not explicitly trust CloudFormation" | One of the three roles is missing at that ARN, or the execution role lacks the CloudFormation trust |
| "A selected AWS region is disabled", "Create/configure the declared EC2 inventory before deployment" | Enable the region. Create the instance, or remove it from the inventory if it is terminated |
| "Reserved concurrency would consume Lambda's required unreserved capacity" | Lower `initial_reserved_concurrency` or raise the Lambda quota |
| "Model profile and its destination models must match declared model_arns", "Model catalog ARN differs from declared model_arns" | Bedrock only. List every model behind the profile in `model_arns` |
| "Create the model API key secret NAME before deploying" | Model API only. The secret does not exist in `bedrock_region`. Create it as in 8.3 |
| "Model API key secret has no unique current version" | Model API only. The secret must have exactly one version labelled `AWSCURRENT` |
| "Bind the exact owned model API key secret ARN and immutable version" | Model API only. The secret's ARN is not `PROJECT-ENVIRONMENT/model-api-key` plus a 6-character suffix in `bedrock_region` and `account_id`. Check the name and region (8.3) |
| "model_api is supported only by the standalone runtime", "MODEL_API is set, but the AgentCore target is Bedrock-only", "AgentCore uses a Bedrock model only; remove MODEL_API." | A model API cannot be used with `agentcore`. Choose one (section 9) |
| "Permission preflight blocked; inspect private preflight.json" | A simulated action was denied. Have your security administrator fix the role, then rerun `check` |
| "Another deployment process owns this work directory" | Another run is active. Wait for it |
| "Configuration/source changed; create a new deployment work directory", "Customer configuration changed during deployment", "Source revision changed during deployment" | Do not edit files or commit during a run. Revert, or start a new work directory (3.8) |
| "Existing stack is not owned by this deployment", "Immutable release exists with a different template; use a new release" | A same-named stack has other tags, or a release already exists with other content. Set a new `release_id` |
| "STAGE: stack needs operator recovery; rollback/failure is not success", "STAGE: change set failed; inspect private AWS events" | Read the stack's events in CloudFormation, then repair it. Never delete retained resources blindly |
| "Change set deletes/replaces resources; separate operator review required" | The tool will not approve this. Review it by hand (Appendix A) |
| "MODULE failed; inspect private operations.log and repair before resuming" | A sub-command failed. Read `operations.log`. An email that only awaits its confirmation click is not this error: it is a `WAITING` stop (3.7 B) |
| "Confirm the subscription emails sent to the addresses configured as ..." | `WAITING`, exit 2. A subscription email is still unconfirmed. Click the link in each one, then resume with the same plan hash (3.7 B) |
| "Required metric unavailable: ID", "Required evidence log group is absent", "Required access metric filter failed its positive/negative fixtures", "Access filter does not cover declared failed-request statuses" | Telemetry is not yet as declared. See [SERVERS.md](SERVERS.md), then resume with `--retry-canary` (3.7 C) |
| "Canary requires explicit paid invocation authorization in staging" | The canary needs `environment: staging` and `--allow-model-invocation` |
| "Owned runtime canary failed", "Canary did not prove both successful tool contracts and model completion" | The function failed or the run stopped early. Read the staging Investigate function's CloudWatch logs in the monitor region. With a model API, common causes are an API host that Lambda cannot reach, a rejected key, a model without tool calling, or a token report that fails Tracaus's accounting check (8.1). The failed canary is ambiguous (3.7 C) |
| "Canary receipt expired/differs", "Previous paid canary outcome is ambiguous" | See 3.7 C |
| "Legacy resource retirement requires separate review" | Review the named alarms or subscriptions by hand. The tool never deletes them |
| "Routing includes an unexpected or unconfirmed subscriber" | A subscriber differs from the plan, for example another address or protocol, or one that is missing or unsubscribed. This stays `FAILED`. Correct it, then resume. A pending email alone waits instead (3.7 B) |
| "UI profile must assume the generated UI role; do not use deployment credentials", "Connection file must be private (chmod 600) and not a symlink" | Fix the profile (section 4), or run `chmod 600` on `ui-connection.json` |
| "APP_PASSWORD is not set: export it (12+ characters) in this shell first; .env is not loaded." | Default mode. Export `APP_PASSWORD` before you run the launcher (section 4). The UI page may mention a `.env` file, but the launcher does not read it |
| "Set a valid MODEL_API setting (...)" | The `MODEL_API` setting in the UI environment is malformed. Use the generated `ui-connection.json` unchanged |
| "The team access list cannot be used. Team file field 'users[0].instances' is invalid" (the message names a field, never a value) | Team mode only. Fix that entry in `team.toml` (7.4): an unknown key, a duplicate `sub`, or an instance that is not in your deployment inventory. The same message covers a missing or unreadable file, one over 256 KiB or one that group or others can write (use `chmod 600`). The UI stops. It never opens to everyone |
| "Team mode requires XSRF protection and disabled trusted-header identity overrides." | Team mode only. Streamlit's XSRF protection is off, or `trustedUserHeaders` is set. Turn XSRF protection back on and empty `trustedUserHeaders` in `.streamlit/config.toml` (or wherever you set them), then restart (7.6) |
| "Deployment failed (ErrorType); no success claimed" | An unexpected error. Read `error.json` if present, and `operations.log` |

## 6. Next steps

- Prove it works: [ACCEPTANCE.md](ACCEPTANCE.md) (real inbox delivery, fault drills, team-mode
  checks if you enabled it, handover).
- Run it day to day: [OPERATE.md](OPERATE.md) (incidents, recipients, team access, rotation,
  backup, rollback, spend).
- Understand the design: [ARCHITECTURE.md](ARCHITECTURE.md). Report a security problem:
  [SECURITY.md](../SECURITY.md).

---

**Sections 7 to 9 are optional. Skip them on the default path.**

## 7. Optional: team mode

Team mode lets several people share one UI, each with their own sign-in and their own list of
instances. It needs no AWS resources and no deployment step. You write one small file on the
machine that runs the UI. It is on when `TRACAUS_TEAM_FILE` is set; otherwise the UI uses the single
shared password. It has only been tested offline. It has never been run against a real identity
provider, and whether yours sends the claims it needs cannot be checked offline.

### 7.1 What changes

- People sign in through your identity provider, with the multi-factor sign-in it enforces.
- A `team.toml` file lists who may use which instances, as a viewer or an investigator.
- Chat runs in the UI process with the UI role's credentials, as in default mode. Tracaus checks each
  person's instance list in code. IAM does not. One AWS role serves everyone, so anyone who can run
  code in the UI process or read its environment holds that role.
- Team mode needs `RUNTIME_TARGET=standalone`. With `agentcore` the UI reports "Team mode needs
  RUNTIME_TARGET=standalone: chat runs in the UI process."
- The sign-in check runs before anything else is shown. A person who is signed in but not allowed
  sees one message and a Sign out button.

### 7.2 Register the app with your identity provider

- Register an OIDC web application. Note its issuer, client ID, client secret and discovery URL
  (`.../.well-known/openid-configuration`).
- Register the exact callback `https://YOUR_UI_HOST/oauth2callback`. For a local staging pilot,
  use the loopback address you open in the browser, only if your provider allows it. Hosted use
  needs your HTTPS hostname (7.6).
- Enforce MFA. The signed ID token must carry `iss`, a stable `sub`, an `auth_time` timestamp (or
  `iat`, if your provider sends no `auth_time`) and, unless you set `require_mfa = false` (7.4),
  an `amr` list that contains `mfa`. Tracaus refuses a sign-in older than `session_hours`.
- Note the issuer. It goes into `team.toml` (7.4) and must equal the token's `iss` value.

### 7.3 Write the sign-in settings

Create `.streamlit/secrets.toml` on the UI machine. Replace the placeholders and fill both empty
secrets privately. An empty secret is not usable:

```toml
[auth]
redirect_uri = "https://YOUR_UI_HOST/oauth2callback"
cookie_secret = ""
client_id = "YOUR_OIDC_CLIENT_ID"
client_secret = ""
server_metadata_url = "https://YOUR_IDENTITY_HOST/.well-known/openid-configuration"
client_kwargs = { scope = "openid", prompt = "login" }
```

`prompt = "login"` forces a fresh sign-in. Do not rely on `max_age`: the sign-in library ignores
it.

Run `chmod 600 .streamlit/secrets.toml`. Make the cookie secret a long random value, different
from the client secret. These two are also separate from the log-cursor secret that AWS generates.
Never put any of them in the JSON files, `team.toml` or Git. Parameters differ by provider, so
check them against
[Streamlit's OIDC guide](https://docs.streamlit.io/develop/concepts/connections/authentication).
Do not turn on token exposure, and never add identity headers at a proxy.

### 7.4 Write team.toml

Keep it outside the repository, or under a path that Git ignores (the repository ignores
`/team.toml`). It holds no secrets.

```toml
issuer = "https://login.example.invalid"   # must equal the token's iss claim
require_mfa = true                          # needs "mfa" in the token's amr claim
session_hours = 8                           # longest sign-in age, from auth_time (or iat)

[limits]
chat_per_user_per_hour = 20

[[users]]
sub = "OIDC_SUBJECT"        # the immutable subject, never an email address
role = "investigator"       # "viewer" reads reports only; "investigator" may also chat
instances = ["i-0123456789abcdef0"]   # instance IDs from your deployment
```

Rules:

- Up to 100 users. Each `sub` appears once. Every instance must be in your deployment's inventory.
  Unknown keys are refused. An error names the field and never shows the value.
- `session_hours` is 1 to 24 (default 8) and `chat_per_user_per_hour` is 1 to 1000 (default 20).
  `require_mfa` defaults to `true`.
- The sign-in age comes from `auth_time` (or `iat`). The `exp` claim is not checked, because
  Streamlit never refreshes the token. A claim with the wrong shape is a denial, not an error.
- If the file is missing or invalid the UI stops and says so. It never opens to everyone.
- Keep the file private (`chmod 600`). The UI refuses a file that group or others can write or
  that is over 256 KiB.
- If your provider does not send the `amr` claim, set `require_mfa = false` and enforce MFA at the
  provider.

Each entry in `instances` may be an EC2 instance ID or an ECS service label such as `ecs-orders`.

Ask your provider's admin tools for each person's immutable `sub`. Do not type an email address.

### 7.5 Start the UI

Give the UI an AWS profile as in section 4, step 1. Then launch it with the team file:

```bash
python scripts/run_customer_ui.py --connection .local/customer/ui-connection.json --profile customer-ui --team-file /path/to/team.toml
```

Only `--team-file` turns team mode on: the launcher ignores a `TRACAUS_TEAM_FILE` that you exported
in your shell. The path must be a regular file, not a symlink, and not writable by group or
others. The launcher removes `APP_PASSWORD`, so the shared password never works in this mode. It
binds the UI to `127.0.0.1`, reads no `.env`, and copies no AWS credentials.

### 7.6 Host it

The launcher binds the UI to `127.0.0.1`, so put a TLS reverse proxy on the same host and serve
the UI at one fixed HTTPS address. Set `status_base_url` in `runtime.json` to that same HTTPS
address (3.3), because alert emails link to it. It is part of the release, so changing it later
needs a new `release_id`. Register the callback for that address with your IdP (7.2), and test
sign-in again.

- Keep Streamlit on loopback behind your own TLS proxy. Do not expose the raw backend origin.
- Forward WebSockets. Accept only your exact public hostname and reject other `Host` and `Origin`
  values.
- Keep Streamlit's CORS and XSRF protection (`.streamlit/config.toml`) and keep
  `trustedUserHeaders` empty. Never let a proxy add identity headers. Team mode refuses to run
  otherwise.

### 7.7 Remove access, review and limits

- **Remove a person:** delete their entry. The file is re-read when it changes, so it applies on
  their next request. An in-flight request finishes. There is no remote logout. Also disable them
  at the identity provider.
- **Audit:** each chat request, report view and refused sign-in prints one JSON line to the UI's
  standard output: time, subject, role, action, outcome, token counts, and the instance for report
  views. It never contains prompt, log or error text. Keep that output in your platform's log
  service. The outcomes are listed in [OPERATE.md](OPERATE.md#team-access-and-secret-rotation).
- **Limits:** the hourly limit is counted per person inside one UI process. It is shared across
  that person's browser sessions, it resets on restart, and several processes would each count
  separately. Run one process.

## 8. Optional: use a model API instead of Bedrock

The default model provider is Amazon Bedrock. Choose this option only if you must use another
provider. Tracaus then calls a model over HTTPS, in one of two protocols:

- `openai`: an OpenAI-compatible Chat Completions endpoint.
- `anthropic`: the Anthropic Messages API.

It works only with the `standalone` runtime. AgentCore stays Bedrock only, and the tool rejects
`model_api` together with `agentcore`. The API key lives in AWS Secrets Manager. You create that
secret yourself, and Tracaus never writes it.

### 8.1 Read this first: what you accept

- **Your data leaves your AWS account.** The prompts include redacted log and metric excerpts.
  They are sent over HTTPS to the provider you name. Redaction is best-effort, not a guarantee.
  Read the provider's terms for retention, training use and data location before you start.
- **Cost, quotas, rate limits, retention and outages belong to the provider.** They are not on
  your AWS bill, and an AWS budget alarm does not see them. Tracaus's token limits are not a dollar
  cap.
- **Token accounting is weaker than with Bedrock.**
  - `anthropic`: Tracaus asks the provider's `count_tokens` endpoint before each call. Anthropic
    documents that count as an estimate. After the call, Tracaus checks the input tokens the provider
    billed against that count.
  - `openai`: there is no count endpoint. Tracaus reserves a local estimate: the request size in
    UTF-8 bytes divided by `bytes_per_token` (default 3), plus 64. This is **not an upper bound**.
    A lower `bytes_per_token` reserves more. If the provider later reports more prompt tokens than
    the estimate, Tracaus stops the run (it fails closed). Those tokens are already billed.
  - A response that reports no token usage stops the run. So does a response that reports
    prompt-cache tokens.
  - Some older OpenAI-compatible servers ignore `max_completion_tokens`. Tracaus stops a run when the
    reported output is above its limit.
- **It is not proven.** The model API code has only been tested offline against fakes. It has never
  been run against a live provider. How well non-Claude models diagnose incidents has not been
  measured. Before you rely on it, run the paid staging canary (3.7) and the paid diagnostics
  evaluation with the `MODEL_API` setting in your shell
  ([evaluations/diagnostics/README.md](../evaluations/diagnostics/README.md)). After `apply`
  finishes, `ui-connection.json` holds the exact `MODEL_API` value.
- **Network.** Tracaus's Lambda functions have public internet egress (this project puts none in a
  VPC), so the API host must be reachable from them. Chat runs in the UI process,
  so the machine that runs the UI must reach the API host too, and the UI role can read the key
  version.

### 8.2 Settings in `deployment.json`

Delete `model_arns` (it is forbidden with a model API). Set these fields instead:

```json
"model_provider": "model_api",
"model_id": "PROVIDER_MODEL_NAME",
"model_api": {
  "protocol": "openai",
  "base_url": "https://api.example.com/v1",
  "bytes_per_token": 3
}
```

| Field | Rule |
| --- | --- |
| `model_provider` | `model_api`. The default is `bedrock` |
| `model_id` | The provider's model name: 1 to 128 characters (letters, digits, `.`, `_`, `:`, `/`, `-`), starting with a letter or digit |
| `model_api.protocol` | `openai` or `anthropic` |
| `model_api.base_url` | `https://` plus a DNS host name with a dot, then an optional path of letters, digits and `. _ ~ / -`. No IP address, port, user info, query, fragment or `localhost`. Keep it under 256 characters. Tracaus adds the endpoint path itself: `openai` appends `/chat/completions` (so the URL usually ends in `/v1`), `anthropic` appends `/v1/messages` (so it usually has no `/v1`). Redirects are not followed, so give the final URL |
| `model_api.bytes_per_token` | Optional, `openai` only: a number from 1 to 8. The default is 3 |
| `model_arns` | Required with Bedrock. Must be absent with `model_api` |
| `bedrock_region` | Still required. It is where the tool functions and the key secret live |

### 8.3 Create the API key secret

You do this once, before `check`. Tracaus never creates or edits the secret.

- **Name:** exactly `PROJECT-ENVIRONMENT/model-api-key`, using your `project` and `environment`
  (for example `tracaus-staging/model-api-key`).
- **Place:** your `account_id`, in `bedrock_region`. AWS adds a 6-character suffix to the ARN, so
  the ARN reads `...:secret:PROJECT-ENVIRONMENT/model-api-key-XXXXXX`. Tracaus rejects any other ARN.
- **Value:** the bare API key and nothing else: no JSON, no spaces, no quotes. It must be 8 to 4096
  printable ASCII characters.
- **Versions:** exactly one version labelled `AWSCURRENT`. A new secret has one.
- **Encryption:** use the default key. Tracaus adds no KMS permission for this secret, so a secret
  under your own KMS key needs decrypt rights that you add yourself.

Use credentials that may create secrets (add `--profile NAME` if needed). This keeps the key out of
your shell history and removes the file afterwards:

```bash
umask 077
read -rs MODEL_API_KEY                       # paste the key; it is not shown
printf '%s' "$MODEL_API_KEY" > "$HOME/model-api-key.txt"
unset MODEL_API_KEY
aws secretsmanager create-secret --region BEDROCK_REGION --name PROJECT-ENVIRONMENT/model-api-key --secret-string "file://$HOME/model-api-key.txt"
rm "$HOME/model-api-key.txt"
```

You can also create it in the AWS console with the same name and value. To check the result
without reading the key, run:

```bash
aws secretsmanager describe-secret --region BEDROCK_REGION --secret-id PROJECT-ENVIRONMENT/model-api-key
```

The output must show that name, the ARN and exactly one version with `AWSCURRENT` in
`VersionIdsToStages`. The operator role (`ci_principal_arn`) needs `secretsmanager:DescribeSecret`
on `PROJECT-ENVIRONMENT/*` in `bedrock_region`. `check` screens it. The operator role does not need
to read the key.

### 8.4 What `check` and `apply` do differently

- **`check`** makes no Bedrock call. It calls `DescribeSecret` instead. A missing secret blocks it
  with "Create the model API key secret NAME before deploying". `preflight.json` then reports
  `model_catalog_visible: null` and `model_secret_present: true`. `check` does not read the key,
  call the endpoint or test your quota. Only the paid canary proves those.
- **`apply`** runs a read-only `model-secret` step before `owned-tools`. It pins the one current
  version of your secret as `model_secret` in `bindings.json`. It never reads the key value. The
  functions read the key at run time, using that exact version.
- **IAM.** The roles that call the model get `secretsmanager:GetSecretValue` on that secret, limited
  to that one version. They get no Bedrock permissions. The UI role is one of them.
- **Setting.** Each function receives a `MODEL_API` setting with the protocol, base URL, secret ARN
  and secret version. It never holds the key.

### 8.5 Rotate the key

A release reads the exact secret version it was pinned to, so rotation means a new release:

1. Add a new version of the secret. It becomes `AWSCURRENT`:

   ```bash
   aws secretsmanager put-secret-value --region BEDROCK_REGION --secret-id PROJECT-ENVIRONMENT/model-api-key --secret-string "file://$HOME/model-api-key.txt"
   ```

   Use the same private-file steps as in 8.3, and remove the file afterwards.
2. Choose a new `release_id` and a new work directory. Then run `dry-run`, `check` and `apply`
   again. The new run pins the new current version.
3. Keep the old version until the old release is retired. Retire it as described in
   [OPERATE.md](OPERATE.md).

### 8.6 Qualify it

The paid staging canary (3.7) shows that the key, the endpoint, tool calling and Tracaus's token checks
work together. Do not skip it. Then run the diagnostics evaluation with the `MODEL_API` setting in
your shell, and have a named person read the results
([evaluations/diagnostics/README.md](../evaluations/diagnostics/README.md)).

## 9. Optional: AgentCore runtime

`standalone` runs on Lambda. `agentcore` runs the work in Amazon Bedrock AgentCore hosts instead.
Set `"runtime_target": "agentcore"` in `runtime.json`. It works only with Bedrock models. A model
API (section 8) is rejected with it.

- **Extra permissions.** The CloudFormation execution role also needs Runtime and endpoint
  management, and `iam:PassRole` with `iam:PassedToService` set to
  `bedrock-agentcore.amazonaws.com`. `check` screens these.
- **ARM64 build.** `apply` builds an ARM64 host package. The manual command is in Appendix A.
- **The runtime.** `apply` creates one AgentCore runtime and endpoint (`agentcore-runtime`,
  `agentcore-endpoint`). The UI calls that endpoint directly with the UI role. Team mode does not
  work with it (section 7).
- **The host.** It speaks the AgentCore HTTP `/ping` and `/invocations` contract and binds
  `0.0.0.0:8080` only in AgentCore hosting mode. Never expose it as a public API. Code ships as a
  versioned S3 ZIP on Python 3.12. AWS patches the language runtime, and you update the bundled
  dependencies. A local import check on macOS does not prove a real boot. See
  [AWS direct-code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html).

## Appendix A: manual commands

Use this to recover or inspect a stuck run, or to do what the tool refuses (enabling
investigation, reviewing deletions). These are the commands the automation runs for you. They have
not been run against real AWS. Pick one path per release. If you start by hand, do not run
`apply` on the same release afterwards.

**Files and names.** Automation and manual use the same file formats.

| Automation | Manual | Notes |
| --- | --- | --- |
| `deployment.json` | `deployment.json` | Same schema |
| `runtime.json` | `runtime.json` (older docs call it `durable.json`) | Same schema. Start from `examples/durable.example.json` |
| `automation.json` | none | Profile and wheel path are automation only |
| `bindings.json`, written from `state.json` | `bindings.json`, written by you | JSON object of collected outputs. Never invent a value |
| `bundle/`, `build/...` | Any folders you pass | The commands below use the same layout |
| `ui-connection.json` | `.env` | See below |
| `scripts/run_customer_ui.py` | `streamlit run app.py` | The launcher ignores `.env` |
| Plan hash | Review hash | The plan hash guards `apply`. Each rendered bundle has its own review hash |

**Build and render.** Skip the lines that do not apply to you.

```bash
python -m infra build --spec .local/customer/deployment.json --output .local/customer/build/tools --wheelhouse .build/wheels
python scripts/build_pipeline.py --output .local/customer/build/pipeline --wheelhouse .build/wheels
# agentcore only (ARM64 host):
python scripts/build_lambdas.py --function incident_investigate --architecture arm64 --output .local/customer/build/host --wheelhouse .build/wheels
# observers only:
python scripts/build_observations.py --spec .local/customer/deployment.json --output .local/customer/build/observation --wheelhouse .build/wheels
python -m infra.deploy.durable --spec .local/customer/deployment.json --config .local/customer/runtime.json --output .local/customer/bundle --build-dir .local/customer/build/pipeline --tool-build-dir .local/customer/build/tools
```

Add `--host-build-dir`, `--observation-build-dir` and `--bindings .local/customer/bindings.json`
as they apply. The render prints `review_hash`. **Every re-render changes it.** Commit source
first: mutating commands refuse a dirty tree or a different commit. To use one named profile for
every command, run it as `python -m infra.deploy.operator --profile NAME --module infra.deploy.durable_ops -- ...`.

**The stage cycle.** Each stack needs these three commands, then a wait. `STAGE` is a stage name
from the table below. Take `CHANGE_SET_ID` from the `Id` in `change-set.json` (or use the name
`review-` plus the first 24 characters of the review hash). `INSPECTED_HASH` is `review_hash` in
`inspected-change.json`. Execution requested is not deployment complete. Wait until CloudFormation
reports `CREATE_COMPLETE` or `UPDATE_COMPLETE` before you collect or seal.

```bash
python -m infra.deploy.durable_ops change-set --bundle .local/customer/bundle --review-hash REVIEW_HASH --stage STAGE --output .local/customer/change-set.json
python -m infra.deploy.durable_ops inspect --bundle .local/customer/bundle --review-hash REVIEW_HASH --stage STAGE --change-set CHANGE_SET_ID --output .local/customer/inspected-change.json
python -m infra.deploy.durable_ops execute --bundle .local/customer/bundle --review-hash REVIEW_HASH --stage STAGE --change-set CHANGE_SET_ID --change-set-hash INSPECTED_HASH --output .local/customer/execution.json
```

Then `collect --stage STAGE --output FILE` reads the stack outputs, and `seal-runtime --stage STAGE`
adds a stack policy and termination protection to an immutable stage. Stage commands take
`--bundle`, `--review-hash` and `--output` in every case.

**Stage order.** Re-render after each binding, and use the new review hash. Stages marked
"release" are create-only, retained and sealed: never update one, publish a new `release_id`.
The others are updateable foundations. Rows that mention a model API or `agentcore` apply only if
you chose that option.

| Step | Stage or command | Save the result as binding |
| --- | --- | --- |
| 1 | `foundation-tools`, `foundation-monitor`, `durable-foundation` (render with empty bindings) | after `durable-foundation`, `collect` as `foundation` |
| 2 | `cursor-version` (read-only). A model API adds `model-secret-version` (read-only, before step 5) | `secret`, and `model_secret` |
| 3 | `upload --artifact-kind tools --build-dir ...` | `tool_artifacts` |
| 4 | `owned-tools` (release), `collect`, then `seal-runtime` | `tools` |
| 5 | `upload --artifact-kind pipeline` (and `host` for agentcore, `observation` for observers) | `artifacts`, `host_artifact`, `observation_artifacts` |
| 6 | agentcore only: `agentcore-runtime`, then `agentcore-endpoint` (release stages, each collected and sealed) | `agentcore_candidate` (remove `RuntimeArn`, keep `RuntimeId` and `RuntimeVersion`), `agentcore` |
| 7 | `durable-runtime` (release) | `versions` |
| 8 | Observers only: `observation-foundation`, `observation-runtime` (release), then `seed-health` | `observation_versions` |
| 9 | `verify-candidate`. Optional reads: `verify-runtime`, `verify-observations` | none |
| 10 | `canary --allow-model-invocation` | `--output canary.json` |
| 11 | `retirement-plan --receipt canary.json`. If it names alarms or subscriptions, review them, run `retire --receipt canary.json --retirement-plan FILE`, then get a new, empty plan | none |
| 12 | `routing` with `execute ... --receipt canary.json --retirement-plan FILE` (receipt under one hour old), then `observations` | none |
| 13 | `verify-routing`, `verify-observation-routing`, `collect --stage routing` | `UiRoleArn` |

Each binding is exactly the JSON that its command wrote to `--output`. `seed-health` writes
health metrics and may probe approved endpoints, but it runs no model and activates no schedule.
Never use `DEFAULT` or an invented version for an AgentCore endpoint. `--artifact-kind` accepts
`pipeline` (default), `tools`, `host` or `observation`.

**Enabling automatic investigation.** Set `investigation_paused` to `false` in `runtime.json`,
re-render (new review hash), then repeat `verify-candidate`, the canary, `retirement-plan` and the
`routing` cycle. A stale receipt or hash must never be reused. Do this only after
[ACCEPTANCE.md](ACCEPTANCE.md) passes in staging.

**Observer activation and inbox check.** After `routing` and `observations` pass, invoke the pinned
Canary function once (`CanaryVersionArn` in `observation_versions`) with your own AWS tools. The
event body is ignored. It needs `observability.enabled` set to `true` and makes no model call. Wait
for its incident, the initial notification and the test receipt. When the message arrives in the
primary inbox, record it with `attest-email`, using the received notification ID (32 hex digits
followed by `-initial`). The command is in
[OPERATE.md](OPERATE.md#respond-to-an-alert-or-incident). Never attest from a publish log or a
queue receipt. A new recipient voids an attestation, so repeat it at least every
`email_receipt_max_age_hours`.

**UI settings by hand.** Copy the JSON in `Outputs.RuntimeConnection.Value` of
`.local/customer/bundle/routing.json` into `.env` as `NAME=value` lines (see `.env.example`). Add
`MONITOR_REGION`, `INCIDENT_TABLE` (`foundation.TableName`) and `REPORT_BUCKET`
(`foundation.EvidenceBucket`).

- Default mode: set `APP_PASSWORD` (at least 12 characters).
- Team mode: set `TRACAUS_TEAM_FILE` to the absolute path of your `team.toml` (section 7). Do not set
  `APP_PASSWORD`: team mode does not use it.

Run `chmod 600 .env` (and `.streamlit/secrets.toml` with team mode), then
`streamlit run app.py --server.address 127.0.0.1`. Real environment variables win over
`.env`. Restart after any change.

## Appendix B: longer tasks

**Hosting the UI for other people.** The tool does not create a UI host. The launcher is for one
person on loopback. For shared use you own the hosting.

- Without team mode the only protection is one shared password, and anyone who knows it has the
  UI role's read scope (section 4). Do not host that for others, except behind your own SSO or
  VPN proxy. Prefer team mode (section 7).
- Keep Streamlit on loopback behind your own TLS proxy. Do not expose the raw backend origin.
- Forward WebSockets. Accept only your exact public hostname and reject other `Host` and `Origin`
  values.
- Keep Streamlit's CORS and XSRF protection (`.streamlit/config.toml`) and keep
  `trustedUserHeaders` empty. Never let a proxy add identity headers.
- With team mode, register the final callback with your IdP and test sign-in again. Alert links
  must resolve through `status_base_url` to this UI, behind sign-in.
- A browser login or token never authorizes direct Lambda, tool or S3 access. Give users the URL
  and sign-in steps only after approval, and never share deployment credentials, passwords or
  session keys.

**Existing resources.** Tracaus's templates create log groups, topics and storage. If a resource of
the same kind already exists, decide who owns it before you apply. The tool never imports or
takes over foreign resources, and it never deletes them. Keep immutable release stacks and
retained evidence apart from the updateable foundations and routing.
