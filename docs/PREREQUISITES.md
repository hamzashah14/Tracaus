# Prerequisites: what must exist before you deploy Tracaus

> **Status.** The deployment tooling has only been tested locally. It has never been run against a
> real AWS account. Treat every item here as unproven until you have done it in your own staging
> account.

This page answers one question: what must already exist, and what must you have decided, before you
run `python -m infra.automation`? It lists requirements only. The steps are in the
[deployment guide](DEPLOY.md). Terms are in the [glossary](ARCHITECTURE.md#glossary).

**How to use this page.** Work through the quick checklist, then read the section behind each box you
cannot tick. Sections 1 to 7 apply to everyone who deploys. Section 8 applies only to the options you
choose. Only trying chat on your own machine, with no deployment? Read local tools mode in section 8
instead: sections 2 and 3 (account quotas, the three roles) are not needed for it, except model access
(2.4) and the read permissions listed there.
Section 9 shows the cheapest ways to confirm you are ready. Every rule comes from the code or the
other guides. Where something is not verified, the text says so. Three terms: the *operator* is the
person and role that run the tool. A *canary* is one deliberate, paid test request. *Observers* are an
optional add-on of three Lambda functions that probe your services and send test alerts
([SERVERS.md](SERVERS.md#what-the-observers-check)).

## Quick checklist

- [ ] **Decisions made:** sign-in mode, model provider and runtime target (section 1).
- [ ] **One AWS account** with its 12-digit ID, and two enabled regions: monitor and Bedrock (2.1).
- [ ] **Quotas fit.** Lambda unreserved concurrency is at least 100 above what Tracaus reserves (2.3).
- [ ] **A model that works:** Bedrock access for a model that supports tools and `CountTokens`, or a
  Model API key secret that you created (2.4, 8).
- [ ] **Three IAM roles exist** (operator, CloudFormation execution, UI workload) and you have an AWS
  profile that is a session of the operator role. No access keys in any file (section 3).
- [ ] **1 to 10 Linux EC2 instances** in the monitor region, publishing the metrics and logs Tracaus
  expects (section 4).
- [ ] **One to five alert addresses** and a fixed HTTPS status URL for alert links (6.1).
- [ ] **A workstation:** Python 3.12, Git, a clean committed checkout and the locked installs
  (section 5).
- [ ] **Three private JSON files** drafted under `.local/` with real values (6.1).
- [ ] **People and budget booked:** AWS administrator, mailbox owners, a budget owner with a budget
  alarm in AWS Budgets, and an approver for the paid canary (2.5, 7).
- [ ] **Only if chosen:** team mode (an OIDC identity provider), Model API account, or AgentCore (section 8).
- [ ] **Checks pass:** `dry-run` succeeds offline and `check` exits 0 (section 9).

## 1. Decisions to make first

Each choice has a default. The three defaults together are the shortest path. A Model API and team mode
cannot be combined with AgentCore.

**Who signs in.** Default: local single-user mode, with the UI on your machine behind one shared
password. Option: team mode (your identity provider with MFA, and a `team.toml` allowlist of people
and instances on the UI host). It is on if, and only if, the UI host sets `TRACAUS_TEAM_FILE`. It needs
no AWS resources and works only with `standalone`. How:
[DEPLOY.md section 7](DEPLOY.md#7-optional-team-mode).

**Which model.** Default: Amazon Bedrock in your account. Option: a Model API (an OpenAI-compatible or
Anthropic Messages endpoint). Redacted log and metric excerpts then leave your AWS account, and the
provider bills you outside AWS. How:
[DEPLOY.md section 8](DEPLOY.md#8-optional-use-a-model-api-instead-of-bedrock).

**Which runtime.** Default: `standalone`, which runs on Lambda. Option: `agentcore`, which runs on
Amazon Bedrock AgentCore with Bedrock models only. It needs extra IAM permissions. How:
[DEPLOY.md section 9](DEPLOY.md#9-optional-agentcore-runtime).

## 2. AWS account side

### 2.1 Account and regions

| Item | Requirement |
| --- | --- |
| Account | One account. Put its 12-digit ID in `account_id`. Every command compares the caller's account to it and stops with "Wrong AWS account; no writes permitted". The three IAM roles must be in it |
| Partition | Standard `aws` only. The tool builds every ARN as `arn:aws:...` |
| Monitor region | Alarms, queues, tables, pipeline functions, log groups. **Your EC2 instances must be here.** `check` looks them up in this region |
| Bedrock region | Model calls, the two read-only tool functions, the tools bucket and the secrets. It may equal the monitor region. A same-region deployment still gets separate buckets |
| Both regions | Enabled for your account (`opt-in-not-required` or `opted-in`). Names look like `eu-central-1`. A name with a fourth part, such as `us-gov-west-1`, fails the schema |

### 2.2 AWS services Tracaus uses

Most resources are named `PROJECT-ENVIRONMENT-...` (for example `tracaus-staging-incidents`). Release stacks and
functions add `RELEASE_ID`. Buckets add the account ID and region. Names must stay within 64 characters.

| Service | What Tracaus creates | Needed? |
| --- | --- | --- |
| CloudFormation | One stack per stage. You never write a template | Required |
| IAM | Roles under the path `/PROJECT/ENVIRONMENT/` (3.5) | Required |
| S3 | Private versioned buckets: tools (Bedrock region), monitor and reports (monitor region) | Required |
| KMS | One key for stored reports | Required |
| DynamoDB | The incident table, with a stream | Required |
| SQS | Work, ingress, initial and report queues, each with a dead-letter queue, plus stream and delivery dead-letter queues | Required |
| SNS | Alarms, reports and incident-fallback topics. Observers add two | Required |
| Lambda | Two tool functions (Bedrock region), six pipeline functions (monitor region). Observers add three | Required |
| EventBridge | A rule for EC2 stop and terminate events, and a one-minute sweep schedule. Observers add schedules | Required |
| CloudWatch and Logs | Alarms per instance and per dead-letter queue, log groups, metric filters. A dashboard with observers only | Required |
| Secrets Manager | `PROJECT-ENVIRONMENT/log-cursor` (generated). `.../model-api-key` is yours | Required, plus optional |
| Bedrock | Model calls: `bedrock:InvokeModel` and `bedrock:CountTokens` on your `model_arns` | Required unless Model API |
| Bedrock AgentCore | A runtime and endpoint per release | Optional |
| EC2 | Nothing is created. Tracaus only reads your instances and their state events | Existing |

### 2.3 Quotas and limits to check

| Limit | What Tracaus needs | How to check |
| --- | --- | --- |
| Lambda concurrent executions (monitor region) | `check` fails if `initial_reserved_concurrency` is greater than `UnreservedConcurrentExecutions` minus 100. The example value is 2, so you need at least 102 unreserved. A resume does not count Tracaus's own earlier reservations twice | Service Quotas, or `aws lambda get-account-settings --region MONITOR_REGION`, field `AccountLimit.UnreservedConcurrentExecutions` |
| Bedrock model quotas (Bedrock region) | `check` does not read them. One investigation can reserve up to `tokens_reserved` tokens (example 32000, limit 100000) over up to `model_steps` calls (limit 16) | Service Quotas. Only the paid canary proves it |
| Model API provider | Its own rate limits, quota and billing. An AWS budget does not see them | The provider |
| CloudWatch Logs Insights | No limit is documented here. A run is bounded by `log_queries` (limit 48) and `window_minutes` (limit 30), but nothing caps the bytes scanned | Not verified |
| SNS, SQS, DynamoDB, S3, KMS | No quota is documented here. Tracaus creates a small fixed set (2.2) | Not verified |

### 2.4 Bedrock model access

Skip this if you use a Model API (section 8).

- [ ] The model supports Converse (Bedrock's chat call) with tools, and `CountTokens` for the exact
  request. Not every model does. A catalog entry is not proof.
- [ ] Model access is granted in the Bedrock region.
- [ ] `model_id` and `model_arns` are ready: 1 to 10 ARNs of a foundation model or inference profile.
  A profile must belong to your account, and every model behind it must also be listed.

`check` reads only the model's metadata (`GetFoundationModel` or `GetInferenceProfile`). It does not
test access, quota or Converse. The paid staging canary does. An organization policy that denies
`bedrock:InvokeModel` or `bedrock:CountTokens` breaks the canary, and `check` cannot see it.

### 2.5 Billing

Set a budget alarm for the account in AWS Budgets before `apply`. Tracaus does not create one. Name a
budget owner who approves a pilot budget and the one paid canary. Review spend by service in Cost
Explorer after one week of staging. This repository gives no price estimate. The cost drivers are in
[DEPLOY.md section 2](DEPLOY.md#2-what-it-costs). Token limits and billing alerts are not a dollar
cap, and nothing shuts down when a budget is crossed.

### 2.6 What Tracaus does not create

| You provide | Notes |
| --- | --- |
| The monitored EC2 fleet | Existing Linux instances. Tracaus never installs software on them |
| CloudWatch agent, heartbeat, Nginx logging, readiness routes | Section 4 |
| The three IAM roles, the instance role for the agent, your AWS profiles | Section 3. Tracaus never gives itself permissions |
| Alert addresses | One to five. A shared team address works. A separate fallback address is optional |
| HTTPS hosting for the UI and the status URL | The launcher serves the UI on `127.0.0.1` only. Shared hosting is yours |
| Identity provider (team mode), Model API account and key secret | Only with those options (section 8) |
| AWS Budgets alarm, and an external monitor | If the whole account or region fails, Tracaus's alerts fail with it |

## 3. IAM roles and identities

### 3.1 The three roles that must already exist

| `deployment.json` field | Role | Used for | Trust policy |
| --- | --- | --- | --- |
| `ci_principal_arn` | Operator | The role you run `check` and `apply` as. Your credentials must be an assumed-role session of exactly this role | Yours: whatever lets you sign in (SSO or an assumed role) |
| `deployment_role_arn` | CloudFormation execution | CloudFormation assumes it to create the resources | An `Allow` for `sts:AssumeRole` whose `Principal.Service` is the single string `cloudformation.amazonaws.com` |
| `ui_principal_arn` | UI workload | The identity the UI runs as. Tracaus's generated UI role trusts only this role | Yours: it must let the person or workload that runs the UI assume it. `check` only confirms the role exists |

- Without existing roles, an administrator can create all three with one template: `python -m infra bootstrap-iam` ([DEPLOY.md](DEPLOY.md#33-fill-in-the-three-files)).
- All three must be explicit, different roles in `account_id`.
- Each ARN must match what IAM returns, path included. Otherwise: "Configured IAM role is absent or has
  a different path".
- Use SSO, assumed roles or workload roles. Do not attach AdministratorAccess to clear a blocker.

### 3.2 Permission groups the roles need

The authoritative list is whatever `check` reports for your inventory (3.3). It is a conservative
screen, mainly of create, tag and read actions. It lists no delete actions, so whether the roles suffice
for later stack updates is not verified. `PROJECT-ENVIRONMENT` means your `project` and `environment`
joined by a hyphen.

<details>
<summary>Operator role (ci_principal_arn), screened in the monitor and Bedrock regions</summary>

- **CloudFormation** on stacks `PROJECT-ENVIRONMENT-*`: `CreateChangeSet`, `DescribeChangeSet`, `GetTemplate`, `ExecuteChangeSet`, `DescribeStacks`, `DescribeStackResource`, `SetStackPolicy`, `GetStackPolicy`, `UpdateTerminationProtection`.
- **`iam:PassRole`** on the execution role, only with `iam:PassedToService` set to `cloudformation.amazonaws.com`.
- **S3** `PutObject`, `GetObjectVersion` on `releases/RELEASE_ID/*` in the tools bucket (Bedrock region) and the monitor bucket (monitor region).
- **Lambda** `InvokeFunction` on `function:PROJECT-ENVIRONMENT-*:*`, for the canary.
- **Secrets Manager** `DescribeSecret`, `UpdateSecretVersionStage` on `secret:PROJECT-ENVIRONMENT/*`.
- **Reads in the monitor region.** IAM: `GetRole`, `ListRolePolicies`, `GetRolePolicy`, `ListAttachedRolePolicies`. Lambda: `GetFunction`, `GetFunctionConfiguration`, `GetFunctionConcurrency`, `GetAccountSettings`, `ListEventSourceMappings`. DynamoDB: `DescribeTable`, `DescribeTimeToLive`, `DescribeContinuousBackups`. SNS: `ListSubscriptionsByTopic`, `GetSubscriptionAttributes`, `GetTopicAttributes`. SQS: `GetQueueAttributes`. Logs: `DescribeLogGroups`, `TestMetricFilter`. CloudWatch: `ListMetrics`, `DescribeAlarms`. EventBridge: `DescribeRule`, `ListTargetsByRule`. EC2: `DescribeInstances`, `DescribeRegions`. S3: `GetBucketVersioning`, `GetBucketLocation`. KMS: `DescribeKey`, `GetKeyPolicy`. If the Bedrock region differs, the Lambda, IAM, Logs and S3 reads repeat there.
- **Calls `check` makes itself, outside the screen:** `iam:GetRole` on the three configured roles, `iam:SimulatePrincipalPolicy` for the operator and execution roles, and `bedrock:GetFoundationModel` or `bedrock:GetInferenceProfile` (Bedrock only).

</details>

<details>
<summary>CloudFormation execution role (deployment_role_arn): create, tag and read</summary>

- **S3:** `CreateBucket`, `PutBucketVersioning`, `PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `PutBucketPolicy`, `PutLifecycleConfiguration`, `PutBucketTagging`, `GetBucketLocation`, `GetBucketVersioning`.
- **SNS:** `CreateTopic`, `SetTopicAttributes`, `Subscribe`, `GetTopicAttributes`, `TagResource`. **SQS:** `CreateQueue`, `SetQueueAttributes`, `GetQueueAttributes`, `TagQueue`.
- **DynamoDB:** `CreateTable`, `DescribeTable`, `UpdateTimeToLive`, `UpdateContinuousBackups`, `TagResource`.
- **Lambda:** `CreateFunction`, `PublishVersion`, `GetFunction`, `GetFunctionConfiguration`, `PutFunctionConcurrency`, `AddPermission`, `CreateEventSourceMapping`, `UpdateEventSourceMapping`, `TagResource`.
- **Logs:** `CreateLogGroup`, `PutRetentionPolicy`, `PutMetricFilter`, `TagResource`. **EventBridge:** `PutRule`, `PutTargets`, `DescribeRule`, `TagResource`. **CloudWatch:** `PutMetricAlarm`, `PutDashboard`, `TagResource`.
- **Secrets Manager:** `CreateSecret`, `DescribeSecret`, `GetRandomPassword`, `TagResource`. **IAM** on `role/PROJECT/ENVIRONMENT/*`: `CreateRole`, `GetRole`, `PutRolePolicy`, `TagRole`.
- **KMS:** `CreateKey`, `CreateAlias`, `PutKeyPolicy`, `EnableKeyRotation`, `DescribeKey`, `TagResource`.
- **`iam:PassRole`** on `role/PROJECT/ENVIRONMENT/*`, with `iam:PassedToService` set to `lambda.amazonaws.com`.
- **Scope.** S3, SNS, SQS, DynamoDB, Lambda and EventBridge are screened on names starting `PROJECT-ENVIRONMENT-`. Secrets Manager is screened on names starting `PROJECT-ENVIRONMENT/`. Logs uses any log group. CloudWatch and KMS use `*`, and so do `GetRandomPassword` and the event source mapping actions. In a second region only the S3, Lambda, Logs, Secrets Manager and IAM groups are screened.
- **AgentCore only, Bedrock region, resource `*`:** `bedrock-agentcore:CreateAgentRuntime`, `GetAgentRuntime`, `CreateAgentRuntimeEndpoint`, `GetAgentRuntimeEndpoint`, `TagResource`, plus `iam:PassRole` with `iam:PassedToService` set to `bedrock-agentcore.amazonaws.com`.

</details>

### 3.3 Read what your account needs

1. **Offline.** `dry-run` writes `plan.json` in your work directory. Its `permission_screen` list has
   every screened request: `principal`, `region`, `actions`, `resource` and `context`.
2. **Read-only AWS.** `check` runs each request through the IAM policy simulator and writes
   `preflight.json`. Its `status` is `SIMULATED` or `BLOCKED`. Each entry in `blockers` shows the
   `principal`, `action`, `resource`, `decision` and `missing_context`. `check` exits 1 on any blocker.
   Give the file to your security administrator.

Simulation cannot see organization, session or resource policies. It is not a policy generator and not
proof of access.

### 3.4 Profiles

Tracaus uses the normal AWS credential chain. `automation.json` names one profile, and the tool uses it
for every check and subprocess and drops ambient static keys. Use short-lived credentials from SSO or
an assumed role. Never put access keys in the JSON files, `.env` or any other file. A `~/.aws/config`
example (adjust it to how your organization signs in):

```ini
# Operator: a session of ci_principal_arn.
[profile customer-deployment]
role_arn = arn:aws:iam::123456789012:role/customer-ci
source_profile = YOUR_BASE_LOGIN_PROFILE

# UI: create this after apply, using ui_role_arn from ui-connection.json.
[profile customer-ui]
role_arn = ROLE_ARN_FROM_ui-connection.json
source_profile = YOUR_UI_PRINCIPAL_PROFILE

# A session of ui_principal_arn.
[profile YOUR_UI_PRINCIPAL_PROFILE]
role_arn = arn:aws:iam::123456789012:role/customer-ui
source_profile = YOUR_BASE_LOGIN_PROFILE
```

`apply` runs its subprocesses with the EC2 instance-metadata credential source disabled. An instance
role as your only credential source will not work, so run from a workstation (not tested).

### 3.5 Roles Tracaus creates for itself

All sit under the path `/PROJECT/ENVIRONMENT/` and are tagged `ManagedBy=tracaus-cloudformation`.
CloudFormation generates the names.

| Role | Stage | Trusts | Purpose |
| --- | --- | --- | --- |
| Validation role | `foundation-tools` | `ci_principal_arn` | Read stack status and tools-bucket objects |
| Ingestion and notification roles | `foundation-monitor` | Lambda | Send to the incidents queue, publish to the reports topic |
| Tool roles (2) | `owned-tools` | Lambda | Read-only CloudWatch Logs and metrics queries, log-cursor secret |
| Pipeline roles (6) | `durable-runtime` | Lambda | Ledger, queues, topics, evidence and key, model access, tool calls |
| UI role | `routing` | `ui_principal_arn` | Call the model and tools (or AgentCore) and read incident reports |
| Observer roles (3) | `observation-runtime` | Lambda | Probes, canaries, receipts. Observers only |
| AgentCore execution roles | `agentcore-runtime` | `bedrock-agentcore.amazonaws.com` | Host the runtime. AgentCore only |

### 3.6 The instance role on each monitored server

Each EC2 instance needs an instance profile whose role lets the CloudWatch agent publish metrics to
the `CWAgent` namespace and write log events to Tracaus's log groups, with the instance ID as the stream
name. AWS documents a managed policy for this, `CloudWatchAgentServerPolicy`. This repository does not
verify its contents, so check AWS's agent documentation. Never put administrator credentials on a
server.

## 4. Monitored servers

Tracaus reads CloudWatch and never installs anything on your servers. The steps are in
[SERVERS.md](SERVERS.md). Finish them before the staging canary, which stops if a metric or log group
is missing.

| Item | Requirement |
| --- | --- |
| Instances | Existing Linux EC2 instances, 1 to 10, listed by hand, in the monitor region. No discovery. Windows is not qualified |
| Instance ID | `i-` plus 8 or 17 lowercase hex digits. The heartbeat script accepts only 17 |
| CloudWatch agent | Installed with the file that `dry-run` writes to `.local/customer/collector-examples/INSTANCE_ID.json`. Service enabled and running. Metrics every 60 seconds |
| Metrics | Namespace `CWAgent`, dimension `InstanceId`: `mem_used_percent`, `swap_used_percent`, `disk_used_percent` (adds dimension `path` set to `disk_path`), and, when `process_exe` is set, `procstat_lookup_pid_count` (adds `exe` and `pid_finder`). AWS/EC2 `StatusCheckFailed` is always required, and `CPUUtilization` with `resource_alarms`. Wrong dimensions are the most common failure |
| Log groups | `/PROJECT/ENVIRONMENT/INSTANCE_ID/NAME`, with `/LOG_SEGMENT` before the instance ID if you set one. The stream name is the instance ID. Retention is `log_retention_days` |
| Application logs | The generated file ships the files you list in `log_files`, and nothing else beyond Nginx and the heartbeat. The agent must be able to read each file |
| Nginx (`nginx_alarm: true`) | Standard combined format in `/var/log/nginx/access.log` and the error log in `/var/log/nginx/error.log`, shipped to the `nginx-access` and `nginx-error` groups |
| Heartbeat (observers only) | Run `scripts/collector_heartbeat.py` every minute from a supervised timer. It appends one JSON line to `/var/log/tracaus-collector-heartbeat.log` and makes no AWS calls. It needs Python 3 and only the standard library (minimum version not verified) |
| Readiness routes (observers only) | Public HTTPS on port 443, with a path. No credentials, query or redirect. Private VPC-only endpoints are not supported |
| Time and network | Synchronized clocks (UTC), a rotated heartbeat file, and a path from the agent to CloudWatch in the monitor region (not verified here; see AWS's agent documentation) |

**Log group creation.** Tracaus's stack creates the groups in `log_groups`, and the tool never adopts a resource that
already exists. Groups in `existing_log_groups` are only read, never created or adopted. Not verified: an agent that starts before `apply` and is allowed to create log groups
could create them first and block the stack. To be safe, withhold that permission from the instance
role, or start the agent after the foundation stages finish.

**Nginx format and filters.** The standard combined format is:

```text
$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"
```

`nginx_filters` needs an `access` and an `error` filter. Each has a CloudWatch Logs filter `pattern`, a
sample `match` line that must match and a sample `miss` line that must not. Use sanitized real lines.
The example access filter matches only 502 and 504. With observers it must match 500, 502, 503 and 504.
The example miss line has status 200 and 502 bytes, to catch a filter that reads the byte count as the
status.

**Check telemetry.** Before `apply`, open the `CWAgent` metrics in the CloudWatch console and confirm
each instance shows the metrics above with exactly those dimensions. Tracaus creates the log groups and
Nginx metrics during `apply`, so check those after the foundation stages: each group should have a
stream named after the instance with recent events, and the `PROJECT/ENVIRONMENT/Nginx` metrics should
appear after a test request. The coverage check inside the canary enforces all of it.

## 5. Local machine (the operator workstation)

| Item | Requirement |
| --- | --- |
| OS | macOS or Linux. On Windows use WSL. Native Windows stops with "Deployment automation currently requires macOS/Linux" |
| Python | 3.12 (`.python-version` pins 3.12.14). The package builder refuses any other minor version |
| Git | A checkout of this repository with at least one commit. `dry-run` and `check` read the current commit. `apply` needs `git status` to be completely clean, including untracked files, and the same commit as your `dry-run`, so a new commit means a new plan hash. `.local/`, `.build/` and `.venv/` are ignored |
| Installs | `python3.12 -m venv .venv`, `source .venv/bin/activate`, `python -m pip install --require-hashes -r requirements/dev.lock`, then download the Lambda wheels once ([DEPLOY.md 3.1](DEPLOY.md#31-install-and-prepare)). Run `source .venv/bin/activate` again in each new terminal |
| AWS CLI | Optional. The tool uses boto3 from the lock. The CLI helps you sign in, confirm your account, check quotas and create the Model API secret. No version is pinned |
| Network | HTTPS to the AWS endpoints of the services in 2.2, in both regions, and to a package index for the two `pip` commands. A UI machine also needs the Model API host, if you use one |
| Working directory | Under `.local/` in the checkout, such as `.local/customer`. `init` creates it with mode 700 and the files with mode 600 |
| Browser | For the UI (Streamlit's default is `http://127.0.0.1:8501`) and, with team mode, your identity provider |
| Time and disk | Disk is not documented. `apply` waits up to 900 seconds by default and you resume it several times. The canary receipt is valid for one hour, so plan one sitting in which you can confirm the emails |

## 6. Configuration you must prepare

### 6.1 The three private JSON files

`python -m infra.automation init --work-dir .local/customer` writes them with synthetic examples
(`reference_only: true`, `.invalid` addresses) that `check` and `apply` reject. Relative paths in
`automation.json` resolve against that file. Keep real values out of Git. The rules are explained in
[DEPLOY.md 3.3](DEPLOY.md#33-fill-in-the-three-files).

**`automation.json`.** Exactly these five keys, all required.

| Key | Type and rules |
| --- | --- |
| `version` | `1` |
| `spec`, `runtime_config` | Paths to `deployment.json` and `runtime.json` |
| `profile` | AWS profile name (letters, digits, `_ . @ -`, up to 128), or `null` for the standard credential chain |
| `wheelhouse` | Path to the verified wheels. `init` writes the absolute path of `.build/wheels` |

**`deployment.json`.** Unknown keys are rejected.

| Field | Req. | Type and constraints | Where to find it |
| --- | --- | --- | --- |
| `project` | yes | `^[a-z][a-z0-9]{1,11}$` (2 to 12 characters) | Your choice. It prefixes every name |
| `environment` | yes | `development`, `staging` or `production`. Only `staging` finishes automatically | Your choice |
| `account_id` | yes | 12 digits | Console account menu, or `aws sts get-caller-identity` |
| `monitor_region`, `bedrock_region` | yes | Like `eu-central-1` | Where your instances live. Where your model is enabled |
| `release_id` | yes | `^[a-z0-9][a-z0-9-]{0,15}$`. New for any new code or setting | Your choice |
| `model_id` | yes | Bedrock: 1 to 2048 characters. Model API: 1 to 128, starting with a letter or digit | Bedrock console, or your provider |
| `model_provider` | no | `bedrock` (default) or `model_api` | Section 1 |
| `model_arns` | Bedrock | 1 to 10 unique foundation-model or inference-profile ARNs. Forbidden with `model_api` | Bedrock console |
| `model_api` | Model API | `protocol` (`openai` or `anthropic`), `base_url`, optional `bytes_per_token` (`openai` only, 1 to 8). Forbidden with Bedrock | Your provider (section 8) |
| `executor_mode`, `maintenance_mode`, `reserved_concurrency` | yes | `qualified`. `false`. `null` (or 0 to 1000, where 0 needs `maintenance_mode`) | Keep as generated |
| `notification_email` or `notification_emails` | one of the two | One address, or a list of 1 to 5 distinct addresses. Not both | Mailboxes you own |
| `log_retention_days` | yes | 7, 14, 30, 60, 90, 180 or 365 | Your retention decision |
| `ui_principal_arn`, `ci_principal_arn`, `deployment_role_arn` | yes | Role ARNs in `account_id`, all different | `aws iam get-role --role-name NAME`: copy `Arn`, path included |
| `instances` | yes | 1 to 10 unique objects with the six fields below | EC2 console |
| `instances[].id` | yes | `i-` plus 8 or 17 lowercase hex digits | EC2 console |
| `instances[].log_groups` | yes | 0 to 8 unique names of 1 to 40 letters, digits, `_` or `-`. May be empty if `existing_log_groups` is set | Your choice. Tracaus creates these groups and the agent writes the same names |
| `instances[].existing_log_groups` | no | Up to 8 objects. `name`: 1 to 512 letters, digits, `_`, `.`, `/`, `#` or `-`, a group that already exists in the monitor region, not under Tracaus's own log prefix. `streams` (optional): `instance` (default) or `all` | `aws logs describe-log-groups`. Tracaus reads these groups and never creates or changes them. See [SERVERS.md](SERVERS.md#use-log-groups-that-already-exist) |
| `instances[].log_files` | no | Up to 16 objects, each with `group` (one of the instance's own `log_groups` or `existing_log_groups`, not the Nginx or heartbeat group) and `file` (absolute path of up to 200 letters, digits, `_`, `.`, `/`, `*`, `?` or `-`, no `..`). No repeated pair | Where your application writes its logs. [SERVERS.md](SERVERS.md#step-1-install-the-cloudwatch-agent) |
| `instances[].disk_path` | yes | Absolute path of letters, digits, `/`, `_`, `-` | The mount point to alarm on |
| `instances[].resource_alarms`, `nginx_alarm` | yes | Boolean each | Your choice |
| `instances[].process_exe` | yes | `null`, or 1 to 40 characters of letters, digits, `_`, `.`, `-` | The executable name to count |
| `nginx_filters` | yes | `access` and `error`, each with `pattern`, `match`, `miss` (1 to 2048 characters). Required even without Nginx | Section 4 |
| `log_segment` | yes | `""`, or up to 24 letters, digits, `_` or `-` | Your choice |
| `reference_only` | yes | Boolean. `false` for a real deployment | Keep `true` until values are real |
| `observability` | no | Object, below. Leave it out unless you want observers | Section 4 |

**`observability` (optional).** Every field is required. Keep `enabled` false at first. The whole block
can be at most 2500 bytes.

| Field | Rule |
| --- | --- |
| `enabled`, `interval_minutes`, `canary_interval_minutes` | Boolean. 5 to 15. 60 to 1440, in whole hours that divide a day |
| `receipt_deadline_seconds`, `email_receipt_max_age_hours` | 300 to 1800 and at least one interval. 24 to 168 |
| `services` | 1 to 10, one per instance. Each has `id`, `owner` (a letter, then up to 31 letters, digits, `_`, `-`), `instance_id`, `collector_metric_id` (a declared `CWAgent` metric, such as `INSTANCE_ID-memory`), `heartbeat_log_group` (one of that instance's `log_groups`), `freshness_seconds` (300 to 1800) and `routes` |
| `routes` | 1 to 3 per service, each with `id`, `url`, `timeout_seconds` (1 to 5), `latency_ms` (up to the timeout) and `statuses` (200 to 299). The URL follows the readiness rules in section 4 |

**`runtime.json`.** Starts from `examples/durable.example.json`.

| Field | Req. | Type and constraints |
| --- | --- | --- |
| `status_base_url` | yes | `https://` host with an optional path of letters, digits, `/`, `_`, `-`. No query, no credentials. Alert emails link to it |
| `fallback_email` | no | A valid address with no comma. If you omit it, the fallback topics use the same addresses as the reports |
| `retention_days` | yes | Integer, 7 to 365 |
| `incident_cooldown_minutes` | no | Whole number, 0 to 120. Default 15 when omitted. Later alarms for an instance with an open incident inside this window are stored and counted, not investigated. 0 turns it off |
| `initial_reserved_concurrency` | yes | Integer, 2 to 1000 |
| `investigation_paused` | yes | Boolean. Must be `true` for the first deployment |
| `runtime_target` | yes | `standalone` or `agentcore` |
| `runtime_limits` | yes | Set all eight, as in the example. Each is at least 1 and at most: `tokens_reserved` 100000, `model_steps` 16, `tool_calls` 16, `log_queries` 48, `output_tokens` 4096 (below `tokens_reserved`), `window_minutes` 30, `context_bytes` 64000, `tool_bytes` 20000 |

### 6.2 Environment variables

A deployed UI gets almost every variable from the `ui-connection.json` file that `apply` writes at the
end of a staging run, and the launcher passes it on.

| Variable | Needed by | Set by hand? |
| --- | --- | --- |
| `APP_PASSWORD` | Default mode | **Yes.** Export it in the shell that starts the launcher (6.4). Never with team mode. The launcher does not read `.env` |
| `TRACAUS_TEAM_FILE` | Team mode | **Through the launcher:** pass `--team-file PATH`. The launcher ignores a value exported in your shell. Set it yourself only if you start `streamlit run app.py` by hand ([DEPLOY.md Appendix A](DEPLOY.md#appendix-a-manual-commands)) |
| `TRACAUS_LOCAL_TOOLS` | Local tools mode | **Yes, hand-set, development only.** Path to the one-file tool config (section 8). Needs `ENVIRONMENT=development`. Never on a deployed UI |
| `ENVIRONMENT`, `RUNTIME_TARGET`, `BEDROCK_REGION`, `BEDROCK_MODEL_ID`, `EXPECTED_ACCOUNT_ID`, `ALLOWED_INSTANCE_IDS`, `RUNTIME_LIMITS`, `RUNTIME_RELEASE`, `TRACAUS_DIAGNOSTIC_POLICY`, `EXECUTION_PURPOSE`, `OBS_NAMESPACE` (observers) | A deployed UI | **Never.** Generated. Do not invent a `RUNTIME_RELEASE` |
| `MONITOR_REGION`, `INCIDENT_TABLE`, `REPORT_BUCKET` | A deployed UI | **Never.** Added by `apply` |
| `LOGS_TOOL_ARN`, `METRICS_TOOL_ARN` | `standalone` | **Never.** Generated. Absent with `agentcore` |
| `AGENTCORE_RUNTIME_ARN`, `AGENTCORE_ENDPOINT` | `agentcore` | **Never.** Generated |
| `MODEL_API` | Model API | **Never.** Generated JSON with the protocol, base URL, secret ARN and secret version. It never holds the key |
| `AWS_PROFILE`, `AWS_EC2_METADATA_DISABLED`, `PYTHON_DOTENV_DISABLED` | The launcher | **Never.** The launcher sets them |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Nothing | **Never.** The launcher removes them |

**Local preview only.** The README preview needs no AWS account. Copy `.env.example` to `.env`, keep
`ENVIRONMENT=development` and set `APP_PASSWORD`. The UI then shows a setup checklist and chat stays
off. That `.env` is not for a deployed backend.

### 6.3 `.streamlit/secrets.toml` (team mode only)

Create it on the UI machine and run `chmod 600 .streamlit/secrets.toml`. Git ignores it. Fill the two
empty secrets privately. Make the cookie secret long, random and different from the client secret.
Check the keys against Streamlit's OIDC guide for your provider. `prompt = "login"` forces a fresh
sign-in. Do not rely on `max_age`: the sign-in library ignores it.

```toml
[auth]
redirect_uri = "https://YOUR_UI_HOST/oauth2callback"
cookie_secret = ""
client_id = "YOUR_OIDC_CLIENT_ID"
client_secret = ""
server_metadata_url = "https://YOUR_IDENTITY_HOST/.well-known/openid-configuration"
client_kwargs = { scope = "openid", prompt = "login" }
```

### 6.4 `APP_PASSWORD` (default mode)

- At least 12 characters. The text box accepts at most 256.
- Export it in the shell that runs the launcher, and do not write it to a file:
  `read -rs APP_PASSWORD && export APP_PASSWORD`.
- A session lasts 30 minutes, and chat is limited to 20 requests per hour for each browser session.
- One shared password is not for a UI that other people reach
  ([DEPLOY.md section 4](DEPLOY.md#4-open-the-ui-on-your-own-machine)).

## 7. People and process

| Who | What they do | When |
| --- | --- | --- |
| You, the deployment operator | Run the commands, read `plan.json`, own the plan hash | Throughout |
| AWS or security administrator | Creates and reviews the three roles, reads `preflight.json`, raises quotas, refuses AdministratorAccess | Before `check` |
| Server owner | Installs the CloudWatch agent, heartbeat, Nginx logging and readiness routes | Before the canary |
| Owners of both mailboxes | Confirm the AWS subscription emails (the fallback address gets one early, the primary near the end). Later prove inbox delivery ([ACCEPTANCE.md](ACCEPTANCE.md)) | During `apply`, within one hour of the canary |
| Budget owner | Sets the budget alarm and approves the pilot budget | Before `apply` |
| Approver of the paid canary | Authorizes `--allow-model-invocation`, one bounded model request | At the "Candidate ready" stop |
| Owners for incident response, security and data | Approve retention, model and query limits | Before `dry-run` |
| Identity provider administrator | Registers the app, enforces MFA, finds each user's `sub` | Before the team UI starts (team mode) |
| Data owner and provider account holder | Approve sending excerpts to the provider. Hold the account and key | Before `check` (Model API) |

## 8. Prerequisites for the optional modules

**Team mode** ([DEPLOY.md section 7](DEPLOY.md#7-optional-team-mode)). It needs no AWS resources and works only
with `standalone`.

- An OIDC web app with MFA. The ID token must carry `iss`, `sub`, an `auth_time` (or `iat`) and, unless you set
  `require_mfa = false`, an `amr` list containing `mfa`. Sign-ins older than `session_hours` are refused. Whether
  your provider sends these claims cannot be checked offline.
- The exact callback `https://YOUR_UI_HOST/oauth2callback`, plus the client ID, client secret,
  discovery URL and cookie secret in `.streamlit/secrets.toml` (6.3). A local staging pilot may use the
  loopback address if your provider allows it.
- A `team.toml` file on the UI host: the issuer, and for each person the immutable `sub` (never an email), a role
  (`viewer` or `investigator`) and a subset of your instances.
- For shared use, your own TLS reverse proxy on the UI host, serving one fixed HTTPS address.

**Model API** ([DEPLOY.md section 8](DEPLOY.md#8-optional-use-a-model-api-instead-of-bedrock)).

- A provider account and a model with tool calling, behind an OpenAI-compatible Chat Completions
  endpoint or the Anthropic Messages API. `standalone` only.
- `base_url`: `https://`, a DNS host name with a dot, at most 256 characters, and no IP address, port,
  credentials, query or fragment. Tracaus adds `/chat/completions` (`openai`) or `/v1/messages`
  (`anthropic`). Redirects are not followed.
- A secret you create **before `check`**, in your account and `bedrock_region`, named exactly
  `PROJECT-ENVIRONMENT/model-api-key`. Its value is the bare key (8 to 4096 printable ASCII
  characters), it has exactly one `AWSCURRENT` version, and it uses the default KMS key. Steps:
  [DEPLOY.md 8.3](DEPLOY.md#83-create-the-api-key-secret).
- Egress: Lambda (public internet access) and, in default mode, the UI machine must reach the host.
  Your data owner must approve the data leaving your AWS account.
- The operator role needs `secretsmanager:DescribeSecret` on `PROJECT-ENVIRONMENT/*` in
  `bedrock_region`. It does not read the key. The option has never run against a live provider.

**AgentCore** ([DEPLOY.md section 9](DEPLOY.md#9-optional-agentcore-runtime)).

- AgentCore available in your Bedrock region. `check` does not test this. A Bedrock model only.
- The extra execution-role permissions in 3.2.
- `apply` builds the ARM64 host package on your machine from the same hash-locked wheels, with Python
  3.12. No ARM64 machine is needed. The runtime name (`PROJECT_ENVIRONMENT_RELEASE_agentcore`) must be at
  most 48 characters, so keep `project` and `release_id` short.
- It has never booted on real AWS.

**Local tools mode, no deployment** ([README](../README.md#try-it-against-your-own-cloudwatch-no-deployment)).
Development only. It runs chat with the two read-only tools inside the UI process and your own AWS
credentials. It cannot run incident investigations. It needs:

- Python 3.12 and the app install from the README, in a virtual environment.
- AWS credentials from the standard chain (a profile or SSO) with `logs:DescribeLogGroups`,
  `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery` and `cloudwatch:GetMetricStatistics`.
  `StartQuery` is billable. Limit it to the listed log groups, as the deployed policy does. The UI does
  not check that the credentials belong to `EXPECTED_ACCOUNT_ID`, so check your profile yourself.
- A model. With Bedrock, `bedrock:InvokeModel` and `bedrock:CountTokens` on the model (2.4). With a
  Model API, `MODEL_API` in `.env` and `secretsmanager:GetSecretValue` on the key secret you created.
  The configuration check accepts `MODEL_API` in this mode, but no test runs it end to end.
  `BEDROCK_REGION` and `BEDROCK_MODEL_ID` stay required either way.
- The one-file config named by `TRACAUS_LOCAL_TOOLS`. These keys are required and unknown keys are rejected:
  `version` (1), `monitor_region`, `log_prefix`, `instances` (1 to 100), `log_groups` (up to 1000, each
  under `<log_prefix>/<listed instance>/`) and `metric_catalog` (may be empty; each entry belongs to a
  listed instance). The optional `existing_log_groups` maps a listed instance to its existing groups,
  `{"i-...": {"/myapp/prod/web": "instance"}}`, where the value is `instance` or `all` as in
  [SERVERS.md](SERVERS.md#use-log-groups-that-already-exist). `log_groups` may be empty only when it is set. Write it by hand from `examples/local-tools.example.json`, or run
  `python scripts/make_local_tools.py --spec deployment.json --out .local/local-tools.json`. That makes
  no AWS call, refuses to overwrite without `--force` and writes mode 600.
- Servers whose logs are in Tracaus's layout, `<log_prefix>/<instance-id>/<name>` ([SERVERS.md](SERVERS.md)),
  or in groups you list under `existing_log_groups`. Any other group, such as `/aws/lambda/...`, is not
  reachable. EC2 metrics and the standard `CWAgent`
  memory, swap and disk metrics work with an empty catalog.
- In `.env`: `ENVIRONMENT=development`, `RUNTIME_TARGET=standalone`, no `TRACAUS_TEAM_FILE`,
  `APP_PASSWORD`, `BEDROCK_REGION`, `BEDROCK_MODEL_ID`, `EXPECTED_ACCOUNT_ID` and `RUNTIME_LIMITS`. Leave
  `LOGS_TOOL_ARN`, `METRICS_TOOL_ARN`, `RUNTIME_RELEASE` and `LOG_CURSOR_SECRET_ARN` unset. Set
  `ALLOWED_INSTANCE_IDS` only if it lists the same instances as the file.

## 9. Verify your prerequisites

Go from the cheapest check to the most expensive. Commit the reviewed source before `dry-run`.

```bash
python -m infra.automation init --work-dir .local/customer
# Fill in the three files, then:
python -m infra.automation dry-run --config .local/customer/automation.json --work-dir .local/customer
python -m infra.automation check --config .local/customer/automation.json --work-dir .local/customer
```

1. **`dry-run`, offline.** It makes no AWS call. It catches schema and rule errors: roles that are not
   distinct or not in the account, model ARN rules, runtime limits, a removed team sign-in setting
   (`identity`, `security` or `initial_access`), `model_api` with `agentcore`, `investigation_paused` set to
   `false`, a bad status URL or email. It prints the plan hash and writes `plan.json` and the agent files.
   For the exact message behind a generic "Deployment failed (ValueError)", see
   [DEPLOY.md section 5](DEPLOY.md#5-troubleshooting).
2. **`check`, read-only AWS.** It exits 0, or 1 with a message or a blocker count. It never writes to
   AWS. It reads the following:

| What `check` reads | Prerequisite it catches |
| --- | --- |
| STS caller identity | Wrong account. Credentials not from the operator role |
| IAM `GetRole` on the three roles | A missing role, a path mismatch, an execution role without the CloudFormation trust |
| EC2 `DescribeRegions` | A region that is not enabled |
| EC2 `DescribeInstances` (monitor region) | An instance that is missing, terminated or in another region |
| Lambda `GetAccountSettings` | Too little unreserved concurrency |
| Bedrock model or profile metadata | `model_id` and `model_arns` that do not match |
| Secrets Manager `DescribeSecret` (Model API) | A missing key secret, or no single current version |
| IAM policy simulation | A denied or context-dependent action for the operator or execution role |

`check` does **not** verify telemetry, Bedrock access or quota, Converse support, organization
policies, mailboxes, your identity provider, AgentCore availability, whether Lambda can reach the Model
API host, or the trust policy of `ui_principal_arn`. The coverage check inside the paid canary covers
telemetry, and the canary covers the model. Check telemetry yourself first, by hand, in the CloudWatch
console (section 4).

### Common blockers

| Message or symptom | Missing prerequisite |
| --- | --- |
| "Synthetic reference inputs cannot check/deploy AWS" | `reference_only` is still `true`, or the examples are unchanged |
| "Deployment files must live under ignored .local/" | The work directory is outside `.local/` in this checkout |
| "Wrong AWS account; no writes permitted" | The profile points at another account, or `account_id` is wrong |
| "Run with the configured CI/operator role credentials" | The profile is not a session of `ci_principal_arn` |
| "Configured IAM role is absent or has a different path" | A role is missing, or its ARN does not match IAM |
| "Deployment role does not explicitly trust CloudFormation" | The execution role's trust policy (3.1) |
| "A selected AWS region is disabled" | A region is not enabled for your account |
| "Create/configure the declared EC2 inventory before deployment" | An instance is missing, terminated or not in the monitor region |
| "Reserved concurrency would consume Lambda's required unreserved capacity" | Lambda quota (2.3) |
| "Permission preflight blocked; inspect private preflight.json" | A role lacks a screened action (3.3) |
| "Model catalog ARN differs from declared model_arns" | The `model_arns` do not match the model (2.4) |
| "Create the model API key secret NAME before deploying" | The key secret is missing in `bedrock_region` (8) |
| "Apply requires a clean reviewed source checkout", "Run dry-run and supply its exact --plan-hash before apply" | Uncommitted or untracked files, or a new commit or setting change since `dry-run` |
| A build step fails with "... failed; inspect private operations.log"; the log says "Lambda builds require Python 3.12." | The virtual environment uses another Python version |
| "Required metric unavailable: INSTANCE_ID-memory" | The agent is not publishing that metric with the declared dimensions (4) |
| "Unsupported access format; validate the customer's actual format" | With observers, Nginx is not logging the standard combined format (4) |
| "Access filter does not cover declared failed-request statuses" | With observers, the access filter must match 500, 502, 503 and 504 |
| `apply` stops and asks you to confirm the subscription emails | A mailbox owner has not clicked the AWS link (7) |

## 10. Next steps

Deploy with [DEPLOY.md](DEPLOY.md). Set up telemetry on your servers with [SERVERS.md](SERVERS.md).
Prove the result before you rely on it with [ACCEPTANCE.md](ACCEPTANCE.md).
