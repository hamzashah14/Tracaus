# Operate Tracaus

Day-2 runbook. Tracaus runs in your own AWS account with no maintainer-run service, so you own every alarm, queue,
secret and bill. Terms are in the [glossary](ARCHITECTURE.md#glossary). To set up or change the deployment, see
[DEPLOY.md](DEPLOY.md). To prove a deployment works, use [ACCEPTANCE.md](ACCEPTANCE.md). These procedures follow the
code and were tested locally with fakes. The maintainers have not rehearsed them on real AWS, so rehearse them on
staging first.

Run every command here from the repository root with the virtual environment active
(`source .venv/bin/activate`).

Tracaus runs in local single-user mode by default. Parts marked "team mode only" apply when the UI host sets
`TRACAUS_TEAM_FILE` ([DEPLOY.md](DEPLOY.md#7-optional-team-mode)). Replay, alert recipients, erasure, retention,
pausing and restore apply to every deployment.

## Conventions

- Run commands from the repository root. They assume the automation work directory `.local/customer`, with the
  rendered bundle in `.local/customer/bundle`. Adjust if you rendered elsewhere.
- `REVIEW_HASH` is the `review_hash` field in that bundle's `bundle.json`. Commands refuse a bundle that no longer
  matches it. Every re-render changes it, so use the hash of the exact bundle you reviewed.
- Keep outputs private: `umask 077; mkdir -p .local/customer/private; chmod 700 .local/customer/private`. Commands
  write files as mode 0600 and refuse symlinks, but you must secure the directory and its parents. Never put secrets,
  raw logs, cloud responses or contact details in tickets, chat, CLI arguments or the repository.
- Name a primary and backup before go-live for the service owner (each observed service's `owner`), on-call
  (`deployment-oncall` in alarm descriptions), security and data owner, and budget owner. Add an escalation route
  that does not depend on Tracaus.

## Respond to an alert or incident

1. Record the UTC time, environment, release, bundle hash, alarm name, incident ID and safe outcome code. The alarm
   description names its owner.
2. Check the real service from outside first. Investigation status says what Tracaus did, not whether your service is up.
3. Trace in CloudWatch Logs Insights by the fields `Component`, `outcome`, `incident_id` and `fence`. Logs hold safe
   codes only (no prompts, tool payloads, URLs, contacts or AWS responses). Read the original event and checkpoints
   only from authorized private storage.

**Service down or telemetry missing.**

1. Test readiness from your intended external vantage point. A metric or a low request count does not prove availability.
   Confirm DNS is public, TLS validates and the response meets the route's status, size and time limits.
   `DESTINATION_DENIED` means the host resolved to a non-public address or nothing. That is a deployment input
   problem, not an application failure. `TIMEOUT` can be DNS or a hanging response.
2. Check the CWAgent metric dimensions, heartbeat timer and file, agent role, shipped log stream and timestamp skew.
   A stopped timer or missing metric is telemetry uncertainty. Do not call an idle app hung from missing logs.
3. Repair the component and wait for healthy samples through the evaluation windows. Expect the alarm to return to OK
   and the earlier incident to gain `recovered_at` and `recovery_event_id`. Recovery does not cancel running work,
   out-of-order events never replace newer state, and an expired incident cannot be linked.
4. Nginx access counts and diagnostic-event counts can describe the same 500, 502, 503 and 504 failures, so do not add
   them. If one log source vanishes, check rotation and permissions. An unsupported timestamp format needs a
   reviewed release.

**Model or tool outage.** Initial notifications do not depend on the model, so confirm they arrived first. Then
separate `COUNT_FAILED` or `INFERENCE_FAILED`, `INVOCATION_FAILED`, partial or no-data evidence, and limit or
deadline outcomes in the logs. Check model availability, IAM, entitlements, quota and region. Do not add a broad
permission policy, disable CountTokens or bypass the budgets. Pause investigation if needed
([maintenance](#maintenance-pausing-and-spend)). There is no automatic failover between runtime targets or between
model providers. An ambiguous remote run waits for its lease to expire and never starts a second executor. Qualify any
replacement model, provider, target or release on staging first.

With a Model API, check the provider's status page, its quota and billing, and that the key still works. Also check
that the secret version the release pins still exists. A provider error (authentication, rate limit, overload,
timeout, network or an unreadable response) fails that call. Tracaus does not retry inside the call, because a retried
billable request could be billed twice, so the durable worker retries within its limits. A run that stops with
`TOKEN_ACCOUNTING_MISMATCH` ends incomplete with "operator review required". It means the provider's reported
usage did not fit the reservation: more input than reserved, more output than allowed, cache charges or no usage. With
an OpenAI-compatible provider the reservation is only a local estimate. A lower `bytes_per_token` raises that
estimate, but changing it is a new release, so qualify it on staging.

**Lost notification or canary.** The canary is a synthetic alert sent once per UTC slot. Its expectation is written
before it is published.

1. Inspect the current slot's `CANARY` record, incident and initial notification. `PUBLISHER_ACCEPTED` means SNS
   accepted the message, not that mail arrived. A recipient receipt needs the receipt consumer's record with the same
   publisher MessageId. SNS can also deliver twice after an ambiguous publish, so a duplicate email can be legitimate.
2. Check both subscriptions (primary and fallback), the receipt mapping and its DLQ, the sender, consumer and
   observer schedules, and Lambda Errors and Throttles. An unconfirmed, filtered or removed subscriber fails checks.
3. CloudWatch alarms reach you through the fallback topic without the primary notifier. Observer failure and
   heartbeat alarms, and fallback delivery failures, publish to the primary topic. None proves an inbox got mail. To
   prove it, receive the marked canary in the real primary mailbox and record it. Repeat within your
   `email_receipt_max_age_hours` (24 to 168) and after every recipient change. The ID ends in `-initial`, and the
   command needs clean reviewed source:

   ```bash
   python -m infra.deploy.durable_ops attest-email --bundle .local/customer/bundle --review-hash REVIEW_HASH --notification-id RECEIVED_NOTIFICATION_ID --confirm-inbox-delivery --output .local/customer/private/attest.json
   ```

   Never attest from an SNS publish log or an SQS receipt. The primary attestation does not cover the fallback
   mailbox: exercise it separately and keep that receipt.
4. Do not disable a canary or freshness alarm because it shows unfinished setup. The first-run warning stays until a
   receipt and an inbox check exist. To retest, invoke the pinned canary function once under your operator role. It
   never starts model work.

## Repeated and overlapping alarms

One failure often trips several alarms (CPU, nginx errors and the process count) and a flapping alarm can fire again
and again. Without a guard, every alarm would start its own investigation and send two emails. Tracaus keeps **one open
incident per target** (an EC2 instance or an ECS service):

- The first alarm for an instance opens an incident, investigates it and starts a window of
  `incident_cooldown_minutes` (default 15, set in `runtime.json`, 0 turns it off, maximum 120).
- Any other alarm for that instance inside the window is stored with its original event and counted on the open
  incident (`suppressed_alarms` on the incident status page). It starts no investigation and sends no email. The
  window does not extend, so a long outage opens a new incident every 15 minutes at most.
- If observers are on, the OK transition of a folded alarm still links to the open incident.
- Folded: alarms, and the task-stop events of an ECS service (a crash loop stops a task every few seconds).
- Never folded: an EC2 `stopped` or `terminated` event always opens its own incident.
- A true duplicate of a message (SNS or SQS delivering twice) is dropped, as before, and is not counted.

The trade-off is that a different failure on the same instance inside the window is not investigated on its own.
The investigation looks at the time around the first alarm and runs soon after it, so it does not cover a failure
that starts later in the window. Check the incident's `suppressed_alarms` count if you suspect a second cause, and ask
in chat about the instance. Set 0 if you
would rather have every alarm investigated and pay for it. A planned stop and start of an instance still opens an
incident for the stop: pause model work first if that matters (see Maintenance below).

The `Suppressed` metric in the Pipeline namespace counts folded alarms. Not tried on real AWS.

## ECS services

An ECS incident is investigated with `fetch_ecs` (service state, stopped tasks, deployments, load balancer health),
metrics and the service's log groups. The report's `task_failure` finding must quote a stopped task. A deployment or
scale-in stop is not an incident, so a normal release does not page you.

- **Reports without log evidence.** If a new task definition revision writes to a different log group, Tracaus
  keeps reading the old one. Run `python -m infra ecs-discover`, update `existing_log_groups`, and cut a new release.
- **Deleted or renamed services.** `fetch_ecs` then says the service was not found, and `check` fails for a new
  release until the inventory is corrected.
- **Behind an ALB.** `alb5xx` and `unhealthy` alarms exist only when `load_balancer` is set. Changing the target
  group of a service means updating the file and cutting a new release.
- **Services you did not list.** The task-stop rule matches your clusters and your service names separately, so with
  several clusters it can deliver an event for a service you did not list. Tracaus ignores it and counts it in the
  `Ignored` metric. It does not go to a dead-letter queue.
- **Not tried on real ECS.** See [SERVERS.md](SERVERS.md#ecs-services) for what is unverified.

## Check pipeline health

- **Dashboard.** With observations deployed, the CloudWatch dashboard `<project>-<environment>-operations` shows
  handoffs, model and tool outcomes, usage, queue age, observer freshness and receipts. Counters are approximate.
- **Queues.** Four work queues (`-ingress-queue`, `-work-queue`, `-initial-queue`, `-report-queue`) each have a
  dead-letter queue (DLQ, `-dead`) that receives a message after five failed receives. There are also
  `-delivery-dead`, `-stream-dead`, `-observation-dead` and the `-observation-receipts` queue. Any DLQ message
  raises an alarm. SQS deletes messages after 14 days, so triage well before.
- **Ledger.** The incident DynamoDB table is the authority on what Tracaus accepted. SQS counts are approximate and the
  pending-intents index is eventually consistent, so read queue age and ledger state together.
- **Sweeps.** Four independent sweeps run every minute: pending intents, overdue incidents, expired workers and
  expired notifications. If progress stalls, read the saved `SWEEP` records and failure metrics. A manual invoke with
  an empty payload checks pending intents only.

For any backlog, first decide whether the event was never accepted, is a poison source, has a pending intent, is
leased or running, or is finished. Keep capture running even when you pause model work.

- Fix the real defect first (permissions, target, worker, notification path, pinned version, queue mapping,
  visibility, redrive, partial-batch settings). Do not bulk-send messages, delete an unexplained backlog, edit a
  lease or fencing token, or reset budgets.
- Ingress DLQ: privately validate the exact SNS source, account, topic and transition. Never launder an invalid
  source into an accepted event. For dispatch, notification and work DLQs, match the exact intent to the ledger
  before any controlled redrive. For stream, delivery and observation DLQs, separate transport failure from
  recipient or parser failure.
- Afterwards, account for every accepted event as active, completed, degraded or an explicit terminal failure. Count
  duplicates and non-actionable events separately and confirm real inbox delivery. Dashboard totals are not accounting.

## Replay an incident or notification

Neither script runs model work or sends email. Each records an audit entry with your AWS identity and queues an
intent for the normal workers.

| Situation | Script |
|---|---|
| Accepted incident `PENDING` or `RETRY`, under 3 attempts, inside its 10-minute deadline | `scripts/replay_incident.py` |
| Initial or report email `FAILED` or `AMBIGUOUS`, not expired, fewer than 2 earlier replays | `scripts/replay_notification.py` |

Completed, degraded, active, expired, exhausted and deleted incidents are refused. Poison events and delivery DLQ
messages have no replay tool and need a separately reviewed procedure.

**The review step.** Each script runs in two passes. The first changes nothing. It writes a review file with the
current status, attempt count and fencing token (a counter that grows with each attempt), plus a `review_hash`
fingerprint of them. Read it. The second pass (`--apply FILE`) recomputes the review from live state and refuses
unless it matches your file exactly, so anything that changed since you looked forces a new review. A refusal prints
only a generic message and exits 1, so re-run the first pass to see why.

```bash
python scripts/replay_incident.py --spec .local/customer/deployment.json --event-id EVENT_ID --output .local/customer/private/replay-review.json
python scripts/replay_incident.py --spec .local/customer/deployment.json --event-id EVENT_ID --output .local/customer/private/replay-result.json --apply .local/customer/private/replay-review.json
```

For a notification use `--incident-id INCIDENT_ID --kind INITIAL` (or `REPORT`) instead of `--event-id`. Applying a
notification replay resets its attempt count. Each notification allows two such replays of three attempts. Keep the
original DLQ record as evidence and never reset counters by hand. Diagnose first: an SDK timeout is not proof that
nothing was published.

## Change alert recipients

The alert addresses are `notification_email` (one) or `notification_emails` (up to five) in `deployment.json`. The
optional `fallback_email` in `runtime.json` serves both fallback topics. Without it they use the same addresses as the reports. The subscriptions live in updateable stacks. If
observers are deployed, their create-only runtime also holds both addresses, so the change needs a new `release_id`
and observer runtime ([DEPLOY.md](DEPLOY.md#appendix-a-manual-commands)).

1. Put the new addresses in your private settings, confirm you own the mailboxes, then re-render and review the bundle.
2. List stale subscriptions owned by Tracaus's stacks (routing, durable foundation, observation foundation). Review every
   topic, subscription and old address independently, then apply:

   ```bash
   python -m infra.maintain.security_ops recipients-plan --bundle .local/customer/bundle --review-hash REVIEW_HASH --output .local/customer/private/recipient-plan.json
   python -m infra.maintain.security_ops recipients-apply --bundle .local/customer/bundle --review-hash REVIEW_HASH --plan .local/customer/private/recipient-plan.json --output .local/customer/private/recipient-result.json
   ```

   Apply needs clean reviewed source. It rechecks the diff, unsubscribes only reviewed email subscriptions and requires
   AWS to report each as gone. It refuses unconfirmed or foreign subscriptions and ambiguous outcomes. Resolve those
   by hand as your SNS administrator, because an API timeout is not proof of retirement. It sends no email.
3. Apply the reviewed CloudFormation updates so the old addresses cannot return. Until the new subscriptions are
   confirmed, email has no recipient, so do steps 2 to 4 in one window.
4. Confirm the new subscriptions from the mailboxes. Run `verify-routing` and `verify-observation-routing` (they check
   registration, not delivery). Receive real messages on both routes and run `attest-email` again, because old
   attestations belong to the old address and do not count.
5. Check the old recipients get nothing new. A rollback must never bring a departed recipient back. Subscriptions
   created outside these stacks need a separate inventory and review.

## Team access and secret rotation

In the default mode there is no per-person access: whoever has the shared UI password can use the UI, so rotate it
as described below. Cursor secret, workload credential and Model API key rotation apply to every deployment.

**Change who has access (team mode only).** Edit `team.toml` on the UI host. It applies on the person's next
request, with no restart. A request already running finishes. Disable them at the identity provider too, because
there is no remote logout. Take each `sub` from your identity provider's admin tools, never a typed email. A missing
or invalid file stops the UI, and it never opens to everyone. Review the list monthly, and at once after a departure,
incident or provider change. Rehearse secret rotation quarterly.

**Read the audit line (team mode only).** Each chat request, report view and refused sign-in writes one JSON object
to the UI's standard output. It has `ts`, `event` (always `tracaus.audit`), `sub`, `role`, `instance`, `action` and
`outcome`, plus `instance_count` and `tokens` (`input` and `output` counts only) when they apply. It never contains
prompt, log or exception text.

- `action` is `chat`, `report` (it names the instance) or `sign_in`.
- `outcome` is `OK`, `RATE_LIMITED`, or `DENIED_ROLE` (a viewer forced a submit and was refused on the server). A
  refused sign-in is `DENIED_NOT_LISTED`, `DENIED_ISSUER`, `DENIED_EXPIRED`, `DENIED_MFA` or `DENIED_CLAIMS`. A chat
  that does not finish is `ERROR` or `PARTIAL`.
- A refused sign-in is written once per browser session and reason, and a report view once per incident in a browser
  session. A person who is not on the list and reloads in new sessions writes one line per session, and the rate is
  not capped. Put the log under your platform's normal log retention and alerting. The audit trail is only as
  durable as the platform that keeps standard output.
- The hourly chat limit is counted per person (`sub`) in memory in one UI process, across that person's browser
  sessions. It resets when the UI restarts, and a second process counts separately.

**Rotate secrets.** Record the approved source, bundle, key versions and rollback labels privately. Pause new
interactive work and let accepted requests finish.

1. *OIDC client and cookie secrets (team mode only).* Rotate them with your identity provider and in
   `.streamlit/secrets.toml`, restart the UI, clear browser sessions and require fresh MFA.
2. *Log cursor secret.* `durable_ops cursor-version` (same arguments) collects the new version. It changes the
   immutable tool binding, so old pagination cursors fail. Rebuild the tools, cut a new release and restart
   discovery.
3. *Workload credentials.* Use SSO or assumed roles, never long-lived keys. Rotate a leaked credential at its source,
   review your account's audit logs privately, and replace pinned releases if configuration changed. Test allowed and
   denied calls from each role before resuming.
4. *Shared UI password (default mode).* Export a new `APP_PASSWORD` (12 or more characters) in the shell that launches
   the UI, then restart the UI process. Sessions held by the old process end. The launcher does not read `.env`. The
   password has no per-person audit or lockout, so also rotate it whenever anyone who knew it leaves.
5. *Model API key.* This needs a new release. See [Rotating the Model API key](#rotating-the-model-api-key).

## Rotating the Model API key

Only with a Model API provider. Each release pins the key's secret ARN and one exact `VersionId` (the `MODEL_API`
setting). The release fingerprint covers that version, and the runtime roles may read only that version. Tracaus never
picks up a newer version on its own, so a new key means a new release. The secret is named `PROJECT-ENVIRONMENT/model-api-key`
and you create and fill it yourself. Tracaus's automation only reads its metadata.

1. Create the new key at the provider. Add it as a new version of the secret with your own admin credentials. Keep
   the old key valid and the old version in place, because the live release still reads them. Only the new version may
   hold the `AWSCURRENT` label.
2. Pin the new version in a new release. Use a new `release_id` and a new work directory (or the manual commands in
   [DEPLOY.md](DEPLOY.md#appendix-a-manual-commands)). Automation pins the current version at its `model-secret` step.
   By hand, run this read-only command against a bundle rendered for the new release and save its result as the
   `model_secret` binding. It refuses unless exactly one
   version holds `AWSCURRENT`, and it never reads the key:

   ```bash
   python -m infra.deploy.durable_ops model-secret-version --bundle .local/customer/bundle --review-hash REVIEW_HASH --output .local/customer/model-secret.json
   ```

3. Re-render, then build, create and seal the new release stacks and run `verify-candidate`. Then run the paid staging
   canary, which proves that the new key, model and protocol work, and promote through the reviewed routing change
   ([DEPLOY.md](DEPLOY.md#appendix-a-manual-commands)). The canary is staging only. Treat production cutover as a
   separate reviewed procedure.
4. After the new release works, revoke the old key at the provider. Remove the old secret version only after you retire
   the old release. A rollback to the old release needs that version and a key that still works. If you rotate because
   the key leaked, revoke it at the provider first and accept that model work fails until the new release is live.
   Pause investigation meanwhile.

## Switching model provider

The provider and model are part of a release, so switching is a new release, never an in-place edit. Change
`model_provider`, `model_api` (or `model_arns`) and `model_id` in `deployment.json`. Changing `protocol`, `base_url` or
`bytes_per_token` is also a new release. Use a new `release_id` and a new work directory, and follow the same steps as
the key rotation: render, build, seal, `verify-candidate`, the paid staging canary and a reviewed routing change.
Create the key secret first if the new provider is a Model API. Incidents already running keep the release and policy
they started under. A Model API works only with the standalone runtime target, because AgentCore is Bedrock only.
Before relying on a provider, complete the Model API items in [ACCEPTANCE.md](ACCEPTANCE.md).

## Retention and evidence erasure

| Data | Where and how long |
|---|---|
| Incident evidence and reports | Private versioned KMS bucket. Lifecycle and incident TTL follow `retention_days` in `runtime.json` (7 to 365). |
| Team audit lines (team mode only) | One JSON line per event on the UI's standard output. Kept as long as your platform's log service keeps them. Subject, role, action, outcome and token counts, never prompt or log text. |
| Raw logs and provider data | Stay in your systems. CloudWatch Logs follow `log_retention_days` in `deployment.json`. |
| Excerpts and questions sent to a Model API provider | Held by that provider under its own terms. Tracaus cannot erase them. |
| Erasure tombstones | 35 days live. Keep your own private purge registry through the longest backup or export window. |

TTL and lifecycle cleanup are asynchronous, though live reads still deny expired records. Deleting live data does not
delete backups, point-in-time recovery (PITR) history, exports, provider or model retention, old logs, the UI's
audit lines or email copies. Shared KMS keys do not erase backups individually. System logs may name AWS
principals, so classify that data yourself.

**Erase an incident.** This is a security-owner decision for a terminal, quiescent incident only. Finish or suppress
pending delivery first. Never erase running or retryable work. Wait at least 15 minutes past its recorded deadline or
lease.

```bash
python -m infra.maintain.security_ops erase-plan --bundle .local/customer/bundle --review-hash REVIEW_HASH --incident INCIDENT_ID --output .local/customer/private/erasure-plan.json
python -m infra.maintain.security_ops erase-apply --bundle .local/customer/bundle --review-hash REVIEW_HASH --plan .local/customer/private/erasure-plan.json --output .local/customer/private/erasure-result.json
```

Review the incident, row digests, account, bucket, and every object version and delete marker. Apply needs clean
reviewed source and rechecks the plan. It marks the incident `DELETING`, removes readable pointers, deletes only the
reviewed versions, checks that none appeared, removes the details and leaves minimal `DELETED` tombstones. Report
reads, new investigation and notification replay are denied from `DELETING` on. After a partial failure the denial
stays. Keep the private record, fix the cause and generate a new plan. Do not retry blindly or delete by broad
prefix. Manifests over 10,000 rows or versions, and any unversioned or foreign object, are refused. The erasure role
needs exact-table read, update, put and delete, `ListBucketVersions` on `incidents/`, `DeleteObjectVersion` and the
STS account check. Recipient changes need exact-topic inspect and unsubscribe plus stack read.

## Chat speed and token use

**What you see.** While a question runs, the chat shows the current step ("Thinking", "Reading logs",
"Reading metrics", "Checking the answer against the evidence"), and your question appears at once. Under each answer it
shows the tokens used and the time taken. The step names are fixed text: they never contain model, log or user text.

**Why replies do not appear word by word.** Every answer is redacted and, in staging and production, checked against the
evidence it cites before it is shown. The check needs the complete answer, so showing a partial one would skip it. Live
steps give the feel of a chat without that risk.

**What bounds the cost of one question.** All of it comes from `runtime_limits` in `runtime.json`:

| Limit | What it caps |
| --- | --- |
| `tokens_reserved` | Input plus output tokens added up over every model step of the question. A reservation is never refunded |
| `model_steps`, `tool_calls`, `log_queries` | How many model rounds, tool calls and log searches one question may use |
| `output_tokens` | The length of one model reply |
| `context_bytes` | The size of everything sent to the model in one step, including the conversation so far. A long conversation hits this and asks you to start a new one |
| `tool_bytes`, `window_minutes` | The size of one tool result, and the time window one tool call may read (a log search returns at most 50 lines) |

Each model step also makes one token-count call before the model call, so a question that needs several tool calls
takes several round trips. To make answers cheaper or faster, lower `tokens_reserved`, `model_steps`, `tool_bytes` and
`context_bytes`, or choose a smaller model in a region close to the UI. A change to `runtime_limits` is a new release,
so qualify it on staging first. Nothing here has been measured against a real model: read the token and time figures
under your first real answers before you set limits.

## Maintenance, pausing and spend

Declare an owner, UTC start and end, and a resume deadline first.

- **Pause model work only.** Set `investigation_paused: true` in `runtime.json`, re-render and execute the reviewed
  routing change. This stops only the work queue consumer. Capture, dispatch, initial notifications, sweeps and
  observers keep running, and running calls may finish. Incidents still waiting at their 10-minute deadline become
  `DEGRADED`, and replay is refused after that. Resuming changes the bundle, so it needs a new canary and review
  ([DEPLOY.md](DEPLOY.md#appendix-a-manual-commands)). Then reconcile the backlog.
- **Maintenance mode.** Set `maintenance_mode: true` in `deployment.json`. It switches off the four queue consumers,
  the stream consumer, the sweep schedule and service alarm actions. The observer keeps its own heartbeat and outbox
  age, but canary, subscription and inbox checks report `SUPPRESSED`, not success, and no canary expectation is
  created. Queue-age alerts are suppressed. DLQ, observer, Lambda and delivery-failure signals stay live. Queues keep
  messages for 14 days, so end maintenance well inside that. Check the result with `verify-routing`. With observers
  deployed, this flag and `observability.enabled` are also set in the create-only observer runtime, so changing
  either needs a new `release_id` there.
- **Ending maintenance.** Release the reviewed change. Check collector freshness, subscriptions, an initial alert and
  a fallback receipt, and invoke the canary once if the next slot is far off. Reconcile pending work within existing
  budgets before resuming model investigation.
- **Spend spike.** Pause model work first. Stop manual chat: there is no shared chat cap, so stop the UI process (in
  team mode you can instead remove people from `team.toml`). Limit heavy Logs Insights or dashboard refreshes. Keep
  capture and notifications. To stop observation work too, set `observability.enabled: false` in `deployment.json`
  with an external temporary monitor and a re-enable deadline. That stops new health alarms, observer schedules and
  receipt mapping, but not stored resources or their recurring costs. Never destroy queues, keys or rollback artifacts
  to clear alarms. Billing notifications are not a hard cutoff, so compare real usage with your approved budget.

## Back up and restore

A restored table can bring back erased evidence. Nothing in the repository automates a restore, so follow this
order.

1. Restore into isolated resources with no UI, model, notification or stream permission. Never attach a restored
   table to serving roles or workers at once. Record the restore time, PITR or export point and object history.
2. Compare your purge registry with the restored rows. Erase prohibited evidence again from reviewed manifests and
   keep the tombstones. Check recovery links, checkpoints and notifications.
3. Check report denial, no event or replay resurrection, IAM and recipient decisions. Get security-owner approval and
   a second operator's attestation before attaching a serving role or enabling workers. Then retire superseded
   backups under your retention and legal policy.

## Roll back and retire old releases

Release stacks are immutable (termination protection plus a stack policy that denies updates), so new code is a new
release. Roll back by promoting a previously qualified, compatible release through the reviewed routing change
([DEPLOY.md](DEPLOY.md#appendix-a-manual-commands)).

- Pause investigation first. Account for active leases and incidents accepted under the old release.
- An incident keeps the policy and release it first ran under. Finish it there or leave a terminal operator-review
  outcome. Never reset its counters, give it new model settings or send one attempt to both runtime targets.
- Review old recipients and endpoints, retire obsolete caller permissions, record the denials and have a second
  operator attest.
- A release pins its model provider and, for a Model API, one key version. Rolling back to it needs that version to
  exist and its key to still work at the provider.
- Do not delete the ledger, queues, KMS keys or evidence to undo code. `retirement-plan` and `retire` are promotion
  gates for stale alarms and email subscriptions, not a way to remove releases. Nothing here deletes an old release
  stack or key label. Keep them for your rollback window, then have the security owner review and remove them by hand.

## Known limits

- Not rehearsed on real AWS. Local fakes do not prove IAM, quota, delivery or recovery behavior.
- Log queries are bounded in count, bytes and time, but CloudWatch Logs Insights has no billed-byte cap, so this is not
  a hard dollar limit. Token reservations are never refunded, and observed usage is a lower bound if a response is lost.
- Folding repeated alarms (`incident_cooldown_minutes`) is per instance, not per alarm: a second, unrelated failure
  inside the window shares the first incident. It is local-tested only.
- Each runtime target you offer needs its own qualification. Linux collector checks do not prove Windows telemetry.
  Private-only endpoints and automatic fleet discovery are unsupported. A scaled fleet needs an inventory refresh
  and a new release ([DEPLOY.md](DEPLOY.md#39-watch-more-servers-or-change-anything-else-later)).
- Model API: never run against a live provider. A call that takes longer than 24 seconds fails and is not retried in
  the request, and the provider might still bill it. An OpenAI-compatible reservation is a local estimate, not an upper
  bound, and the run stops only after the call that exceeded it. Redacted excerpts leave your AWS account, and
  redaction is best effort. Diagnosis quality on non-Claude models is unmeasured.
- Removing a person from `team.toml` does not cancel a request already running, and upstream cancellation of
  AgentCore calls is unproven.
- Team mode: one AWS role serves every person, and Tracaus's code, not IAM, enforces each person's instance list. There
  is no remote logout. The hourly limit is per UI process and resets on restart. It has not been run against a real
  identity provider.
  If the whole AWS account or region fails, Tracaus's alerts fail with it, so keep an external monitor and contact route.
