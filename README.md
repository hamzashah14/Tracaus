<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/tracaus-logo-animated-white.svg">
    <img alt="Tracaus" src="assets/tracaus-logo-animated-black.svg" width="480">
  </picture>
</p>

<p align="center">
  <strong>Trace the cause.</strong><br />
  An AI incident investigator for your AWS EC2 and ECS workloads<br />
  Read-only. Runs in your account. No hosted service. Three commands: <code>setup</code>, <code>deploy</code>, <code>ui</code>.
</p>

<p align="center">
  <a href="https://github.com/hamzashah14/Tracaus/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/hamzashah14/Tracaus/ci.yml?branch=main&label=build" alt="Build Status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/status-pre--release-orange" alt="Status: pre-release">
  <a href="https://github.com/hamzashah14/Tracaus/stargazers"><img src="https://img.shields.io/github/stars/hamzashah14/Tracaus?style=flat" alt="GitHub Stars"></a>
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#features">Features</a> ·
  <a href="#what-it-reads">What it reads</a> ·
  <a href="#three-choices">Choices</a> ·
  <a href="#what-does-it-cost">Cost</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="#folder-structure">Folders</a> ·
  <a href="#documentation">Docs</a>
</p>

---

## What is Tracaus?

Tracaus (trace + cause) is a free, open-source AIOps assistant for AWS.

When a CloudWatch alarm fires, it reads the logs and metrics of the affected EC2
instance or ECS service, asks an AI model (Amazon Bedrock by default) to reason over that
evidence, and writes a short report that separates what was observed from what is only a
guess. You get the alert first and a link to the report when it is ready. You can also ask
questions in a web chat. You run all of it in your own AWS account.

## Get started

Deploy first, then open the UI: the UI only works once the backend exists. Each command explains what it is doing,
can be run again, and says what is safe and what to run next if something fails.

**Before you start**

- An AWS account and an AWS profile you can sign in with (SSO or access keys). **Once**, to create three IAM roles,
  you also need a profile that can create IAM roles and CloudFormation stacks. Skip that if the roles already exist.
- Amazon Bedrock access to a model that supports tool use and token counting, in the region you choose (or a model
  API key, see [Three choices](#three-choices)).
- Running targets in one region: Linux EC2 instances and/or ECS services. EC2 instances publish metrics and logs
  with the CloudWatch agent, and ECS services log with `awslogs`. `setup` tells you exactly what is missing.
- 1 to 5 alert email addresses you can open (you confirm subscription emails during `deploy`).
- macOS or Linux (Windows: WSL), Python 3.12 and Git. Set an AWS Budgets alarm: Tracaus does not create one.

**1. Get the code and run setup**

```bash
git clone https://github.com/hamzashah14/Tracaus.git && cd Tracaus
./bin/tracaus setup
```

The launcher creates `.venv` and installs the verified dependencies first. `setup` reads your running EC2 instances
and ECS services (read-only), asks which to watch, asks for the alert emails and the model, creates the three IAM
roles if they are missing (only after you type `yes`, with an administrator profile it uses once), writes your
settings to `.local/customer/`, and prints the plan and what is missing on each server. It changes nothing in AWS
except those roles.

**2. Fix what `setup` reported (your side)**

Install the CloudWatch agent on EC2, ship the logs, turn on `awslogs` for ECS. Tracaus never changes your servers.
The generated agent files are in `.local/customer/collector-examples/`; steps are in [SERVERS.md](docs/SERVERS.md).
Missing metrics stop the deployment at its test step, so do this before `deploy`.

**3. Deploy**

```bash
./bin/tracaus deploy
```

It checks your permissions (read-only), asks once, then creates the resources and shows each stage as it completes.
It stops twice for you: a typed `yes` for **one paid model request** (a test through the real tools), and
confirming the **subscription emails** AWS sends. Nothing is routed to Tracaus before its `routing` stage, so an
earlier failure leaves your alarms and servers untouched. Run `deploy` again to resume after a stop or a failure, and
`./bin/tracaus status` any time to see where it is. It never deletes anything.

**4. Open the UI**

```bash
./bin/tracaus ui
```

It adds the UI AWS profiles, generates a password (shown once, saved in `.local/customer/ui-password`), and starts the
UI on `http://127.0.0.1:8501`, on your computer only. Ask questions about your listed servers in the chat, and open
reports from the alert emails (`?incident=ID` on that address).

**5. Check that it works**

Work through [ACCEPTANCE.md](docs/ACCEPTANCE.md): telemetry, alert delivery, access and recovery. Start simple: break
something on a staging host and confirm an alarm fires and an alert email arrives. After `deploy`,
**automatic investigations are paused on purpose**: alerts still go out, and you enable investigations in a later
release ([DEPLOY.md](docs/DEPLOY.md), section 3.8).

**More than one person using the UI?** Use team mode: each person signs in through your identity provider
([Three choices](#three-choices), [DEPLOY.md](docs/DEPLOY.md), section 7). Do not host the shared-password UI for others.

> **Status:** locally tested, never run against real AWS. Treat your first deployment as staging.

## Features

<details open>
<summary><strong>Evidence first, guesses labelled</strong></summary>

Each report separates what was observed in logs and metrics from what is only a hypothesis.
Missing logs alone are not treated as proof of a hang.

</details>

<details>
<summary><strong>EC2 and ECS</strong></summary>

List EC2 instances, ECS services (Fargate or EC2 launch type), or both, up to 10 in total.
For ECS, Tracaus reads where ECS sends things by default: `awslogs` log groups, `AWS/ECS` and
load balancer metrics, and EventBridge task events. See [SERVERS.md](docs/SERVERS.md#ecs-services).

</details>

<details>
<summary><strong>Read-only, no automatic fixes</strong></summary>

Its tools only read CloudWatch logs and metrics for the EC2 instances and ECS services you list,
and, for an ECS service, its state (deployments, stopped tasks, load balancer health).
Tracaus suggests; a person decides and acts.

</details>

<details>
<summary><strong>The alert never waits for the model</strong></summary>

The first alert is sent without waiting for the model. If the model is down, the investigation
is retried within fixed limits, then recorded as degraded.

</details>

<details>
<summary><strong>Your account, your bill</strong></summary>

Everything is deployed and operated by you, in your AWS regions. AWS bills Bedrock, CloudWatch,
Lambda and the other services to you. A Model API provider, if you pick one, bills you separately.
There is no maintainer account, control plane or subscription.

</details>

<details>
<summary><strong>Chat with the same tools</strong></summary>

The web UI lets you ask follow-up questions. Chat uses the same pinned read-only tools as the
automatic investigations, under the runtime limits.

</details>

## What it reads

| | EC2 instance | ECS service (Fargate or EC2 launch type) |
|---|---|---|
| Logs | CloudWatch agent log groups, and existing log groups you list | `awslogs` log groups, such as `/ecs/<family>` |
| Metrics | CloudWatch agent metrics (CPU, memory, disk, optional Nginx and process) | `AWS/ECS` CPU and memory, `AWS/ApplicationELB` if an ALB is in front, Container Insights if you turn it on |
| Events | Instance stop | Task stopped (EventBridge) |
| Service state | n/a | Deployments, service events, stopped tasks with stop code and container exit codes, load balancer target health |
| You set up | CloudWatch agent, heartbeat, Nginx logging | `awslogs` driver, and Container Insights if you want it |

Tracaus never installs anything on your servers or tasks. Server setup stays with you:
[EC2](docs/SERVERS.md), [ECS](docs/SERVERS.md#ecs-services).

## How it works

```mermaid
flowchart LR
    Events[CloudWatch / EventBridge] --> Ingress[SNS / ingress queue]
    Ingress --> Ledger[DynamoDB ledger / outbox]
    Ledger --> Alert[Independent initial notification]
    Ledger --> Worker[Fenced investigation worker]
    UI[Web UI] --> Runtime[Python orchestration / Bedrock or Model API]
    Worker --> Runtime
    Runtime --> Tools[Pinned read-only tools / CloudWatch]
    Worker --> Evidence[Private versioned reports]
    Evidence --> Followup[Follow-up notification]
```

Each investigation is limited in tokens, tool calls, query windows and time. See the
[architecture and glossary](docs/ARCHITECTURE.md).

## Three choices

You make each choice when you deploy. The defaults need the least setup.

| Choice | Default | Option |
| --- | --- | --- |
| Who signs in | **Local single-user mode.** One shared password protects the web UI. | **Team mode.** Several people share one UI. Each signs in through your identity provider with MFA and sees only the instances on their entry in a `team.toml` file on the UI host. No AWS resources. |
| Which model | **Amazon Bedrock.** | **Model API.** An OpenAI-compatible or Anthropic Messages endpoint. Its key lives in AWS Secrets Manager. |
| Where it runs | **Standalone AWS Lambda.** | **Amazon Bedrock AgentCore.** Bedrock only, so it cannot be combined with a Model API or team mode. |

Pass `--team-file` to the UI launcher for team mode. Set `model_provider` in
`deployment.json` for a Model API. The [deployment guide](docs/DEPLOY.md) covers both.
The Model API option has never run against a live provider, and diagnosis quality
on non-Claude models is unmeasured.

## What does it cost?

There is no cost estimate yet. The main cost drivers are:

- **Model tokens** for each automatic investigation and each chat question: Bedrock, or your Model API provider's bill.
- **CloudWatch** log ingestion, Logs Insights queries, metrics and alarms.
- **Always-on storage and keys:** DynamoDB (with point-in-time recovery), S3 versioning, KMS and Secrets Manager.
- **Messaging and compute:** Lambda, SNS and SQS, plus optional AgentCore.

Limits on tokens, tool calls and queries bound each request, but they are not a
dollar cap, and billing alerts do not stop spend. Stored data, keys and alarms keep
costing money while investigations are paused. Watch Cost Explorer for the first
week, and set a budget before you enable investigations. To cut model spend fast,
pause investigations and stop chat ([operations](docs/OPERATE.md)).

## FAQ

<details>
<summary><strong>Does Tracaus change my servers?</strong></summary>

No. Its tools only read CloudWatch logs and metrics, and recommendations are text for a
person to review. Setup does create AWS resources of its own, and you install the CloudWatch
agent on your servers yourself ([server setup](docs/SERVERS.md)).

</details>

<details>
<summary><strong>Can I use it without OIDC/SSO?</strong></summary>

Yes, that is the default. The UI then has one shared password, and chat runs with the UI's own
AWS role. The instance allowlist, pinned tools, runtime limits, release binding and redaction
still apply. Per-person sign-in, audit, revocation and a shared spend cap do not exist, and the
20 requests per hour limit is per browser session, so a new session resets it. Anyone with the
password can use the model and tools the UI role reaches and read every incident report. Keep
the UI on `127.0.0.1` or behind your own SSO or VPN proxy, or use team mode. Details:
[SECURITY.md](SECURITY.md).

</details>

<details>
<summary><strong>What if the model is down?</strong></summary>

The initial alert is still sent. The investigation is retried within fixed limits, then
recorded as degraded ("operator review required"). Chat shows an error with a reference.

</details>

<details>
<summary><strong>Is my data sent anywhere?</strong></summary>

With Bedrock, redacted log and metric excerpts go to Amazon Bedrock in your account and region.
With a Model API, those excerpts and the questions you type leave your AWS account for the
provider's HTTPS endpoint. That provider sets its own retention and quotas and bills you.
Reports are stored in a private, versioned S3 bucket you own. Output is redacted by pattern
matching before it returns to the model or is stored; this is best effort, not a guarantee.
Tracaus makes no calls to the maintainers, and Streamlit usage statistics are turned off.

</details>

<details>
<summary><strong>Can I use a model other than Bedrock?</strong></summary>

Yes, on standalone Lambda: an OpenAI-compatible endpoint with tool calling, or the Anthropic
Messages API. Run the paid staging canary and the
[diagnostic evaluation](evaluations/diagnostics/README.md) against it first. An OpenAI-compatible
endpoint has no token-count call, so Tracaus reserves a local estimate that is not an upper
bound. It stops the run if the provider reports more input than estimated, or no usage.

</details>

<details>
<summary><strong>What is not supported yet?</strong></summary>

Automatic discovery (you list instances and ECS services), EKS, Network and Gateway load
balancers in front of ECS, health probes of private VPC-only endpoints, a desktop app, Windows
servers (the telemetry setup is for Linux EC2) and a Model API with AgentCore.

</details>

## Folder structure

```text
bin/tracaus          the three commands: setup, deploy, status, ui
app.py               the web UI (Streamlit)

tracaus/             the product. Runs in AWS (inside the Lambdas) and in the UI
lambda/              thin Lambda entry points; each one calls code in tracaus/

infra/               deployment tooling. Runs on your computer only, never in AWS
  spec.py            reads and checks your deployment.json
  guided/            what bin/tracaus runs: discovery, questions, deploy with progress
  stacks/            writes the CloudFormation templates
  deploy/            plan, check, apply, verify and seal a release
  maintain/          day-2 work: retire old resources, change recipients, erase data

scripts/             small helpers: build the Lambda ZIPs, server heartbeat, replay, run the UI
  dev/               checks for CI and maintainers; users never need these

schemas/ config/     tool contracts and the default metric catalog, shipped inside the Lambdas
agent-instruction.txt  the prompt the model receives
examples/            sample deployment.json files that setup and the docs start from
evaluations/         synthetic diagnostic cases for testing report quality
docs/                the guides below
tests/               offline tests (no AWS needed)
requirements/        hash-pinned dependency locks
assets/              logo files
```

Where code runs: **`tracaus/` and `lambda/` run in your AWS account and in the UI. `infra/` and `scripts/` run on
your computer (or your servers, for the heartbeat).** [CONTRIBUTING.md](CONTRIBUTING.md#where-each-file-fits)
says what each file does.

## Try it without deploying (development only)

Only want to see the UI, or chat against your own CloudWatch without deploying anything? This is for development; for
real use, follow [Get started](#get-started).

You need Python 3.12 and no AWS account for the preview. Create a virtual environment and activate it (run the
`source` line again in each new terminal):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements/app.lock
cp .env.example .env    # skip this if you already have a .env, and keep yours
chmod 600 .env
```

In `.env`, keep `ENVIRONMENT=development` and set `APP_PASSWORD` to a private value of at least 12 characters. Then
run `streamlit run app.py --server.address 127.0.0.1` and open `http://127.0.0.1:8501/`. With no backend settings the UI
shows a "Connect your runtime" checklist and chat stays disabled. That is expected.

### Try it against your own CloudWatch (no deployment)

<details>
<summary>Steps and limits</summary>

For development, the UI can run its two read-only tools (`fetch_logs`, `fetch_metrics`) in
its own process with your AWS credentials. You skip the tool Lambdas and `infra.deploy.automation`.

1. Write the one-file tool config: copy `examples/local-tools.example.json` and edit it, or
   generate it from a `deployment.json` (the shape of `examples/deployment.example.json`).
   Generating makes no AWS call and will not overwrite a file without `--force`:
   `mkdir -p .local && python scripts/make_local_tools.py --spec deployment.json --out .local/local-tools.json`
2. In `.env`, set `TRACAUS_LOCAL_TOOLS=.local/local-tools.json`, `APP_PASSWORD`, `BEDROCK_REGION`,
   `BEDROCK_MODEL_ID` and `EXPECTED_ACCOUNT_ID`. Keep `ENVIRONMENT=development` and the prefilled
   `RUNTIME_LIMITS`. Leave `LOGS_TOOL_ARN`, `METRICS_TOOL_ARN` and `RUNTIME_RELEASE` empty.
3. Use a read-only AWS profile (`AWS_PROFILE` in `.env`, or an SSO sign-in) with
   `logs:DescribeLogGroups`, `logs:StartQuery` (billable), `logs:GetQueryResults`,
   `logs:StopQuery`, `cloudwatch:GetMetricStatistics`, and `bedrock:InvokeModel` and
   `bedrock:CountTokens` on your model. Tracaus does not check that it matches `EXPECTED_ACCOUNT_ID`.
4. Run `streamlit run app.py --server.address 127.0.0.1` and sign in. "Connection details"
   shows "Local tools (this machine's AWS credentials)".

**Limits.** The tools read log groups named `<log_prefix>/<instance-id>/<suffix>`, the
layout of the agent file in [SERVERS.md](docs/SERVERS.md), and the groups you list under
`existing_log_groups` ([SERVERS.md](docs/SERVERS.md#use-log-groups-that-already-exist)). Any other
group, such as `/aws/lambda/...`, is out of reach. The UI process holds your read credentials, so Tracaus's checks run in code and
IAM does not back them up. Keep the UI on `127.0.0.1`. Use the deployed tools for anything
shared or production. This mode has never run against real AWS either, and it covers EC2 instances only.

</details>

## Documentation

In the order you need them:

1. [Prerequisites](docs/PREREQUISITES.md): everything that must exist or be decided before you deploy, with a quick checklist.
2. [Server setup](docs/SERVERS.md): the CloudWatch agent, heartbeat and Nginx on EC2, and ECS services.
3. [Deployment guide](docs/DEPLOY.md): the three commands in detail, the manual commands behind them, team mode, Model API and AgentCore.
4. [Live acceptance](docs/ACCEPTANCE.md): the checklist to complete before relying on a deployment.
5. [Operations](docs/OPERATE.md): incidents, replay, recipients, rotation, erasure, restore, rollback.
6. [Architecture](docs/ARCHITECTURE.md): components, workflows and a glossary.
7. [Diagnostic evaluations](evaluations/diagnostics/README.md): synthetic cases and optional paid model tests.

## Community

Questions, bug reports and feature ideas are welcome as [GitHub issues](https://github.com/hamzashah14/Tracaus/issues).
Report a vulnerability privately, as described in [SECURITY.md](SECURITY.md).

## Star History

<p align="center">
  <a href="https://www.star-history.com/?repos=hamzashah14%2FTracaus&type=date&legend=top-left">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=hamzashah14/Tracaus&type=date&theme=dark&legend=top-left" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=hamzashah14/Tracaus&type=date&legend=top-left" />
      <img width="600" alt="Star History Chart" src="https://api.star-history.com/chart?repos=hamzashah14/Tracaus&type=date&legend=top-left" />
    </picture>
  </a>
</p>

## Contributors

<a href="https://github.com/hamzashah14/Tracaus/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=hamzashah14/Tracaus&max=100&columns=20" />
</a>

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks.

## License

[MIT](LICENSE). Dependencies keep their own licenses.
