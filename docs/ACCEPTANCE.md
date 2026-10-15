# Live acceptance checklist

**The maintainers have never run these checks against real AWS.** Tracaus's automated tests use fakes. They say nothing
about your IAM, quotas, identity provider, email delivery or recovery. Treat every box below as unproven until you
tick it with evidence from your own staging deployment.

Before you start, freeze the source revision, bundle and review hash, inventory, model and model provider, runtime
target, sign-in mode, owners and budget. Run on staging with an approved, bounded budget, and keep investigations paused while you qualify
the release. The paid canary is staging only, and going to production is a separate reviewed procedure of your own.
Record UTC times, receipts and safe causes. Keep raw logs, cloud responses and contacts private. Qualify each runtime
target you offer (standalone or AgentCore) and each model provider (Bedrock or a Model API) separately. Linux collector
checks do not prove Windows telemetry. Sections marked "team mode only" or "only with a Model API" apply only if you
enabled that option.

## Telemetry

- [ ] **Outages are detected, bounded and mailed.** Stopping the app listener (EC2 still running), stopping Nginx, and
  breaking a critical dependency each raise an alarm and an incident, and the initial email reaches a real inbox within
  your target. A stalled response, trickling body, or DNS or TLS failure ends at the route timeout without hanging
  the observer.
  How: break each on a staging host (and point one route at a slow endpoint), then check the alarm, incident, probe
  code and inbox. Restore, and confirm the alarm returns to OK and the incident shows `recovered_at`.
- [ ] **Collector faults differ from app faults.** Stopping CWAgent, the heartbeat timer or log shipping each raises a
  telemetry alarm, never an invented application hang. An idle healthy service, log rotation and clock skew raise no
  false alarm.
  How: stop each in turn and compare alarm names, then leave a quiet service through a full freshness window.
- [ ] **Coverage is exact.** Every monitored instance, metric, dimension, disk path, process and owner matches your
  inventory. Real sanitized Nginx lines with 500, 502, 503 and 504 match, and a 200 does not. Replacing or scaling an
  instance needs a reviewed inventory refresh, so no member goes unobserved.
  How: compare the rendered `coverage.json` with your real hosts. The strict coverage check also runs inside the paid
  canary ([DEPLOY.md](DEPLOY.md)).

## ECS services (only with `ecs_services`)

- [ ] **A crash is detected, folded and explained.** Stopping a task by making its essential container exit (or an
  out-of-memory kill), then letting the service restart it a few times, opens one incident, mails once, and the
  report's `task_failure` finding quotes the stopped task's stop code and exit code.
  How: on a staging service, run a container that exits, then check the incident, `suppressed_alarms` and the report.
- [ ] **A deployment is quiet.** A normal rolling deployment and a scale-in open no incident.
  How: deploy a new task definition revision and change the desired count, then check that no incident appears.
- [ ] **A failed health check is an incident.** Break the health check path of a service behind an ALB: targets turn
  unhealthy, the `unhealthy` alarm fires, and the report shows the target's health reason.
- [ ] **Log evidence and the ALB answer match reality.** `ecs-discover` agrees with `deployment.json`, and the
  report cites lines from the service's log group.
- [ ] **No secrets leave the task definition.** Put a marker value in a task definition environment variable and
  check that it appears in no stored evidence or email.

## Notification delivery

- [ ] **Primary inbox.** Initial and follow-up emails reach the real mailbox, and the incident link opens the UI
  behind its sign-in over HTTPS.
  How: receive the marked canary, then record it with `attest-email`
  ([OPERATE.md](OPERATE.md#respond-to-an-alert-or-incident)). An SNS acceptance or SQS receipt is not enough.
- [ ] **Fallback route.** With the primary notifier or its mapping disabled, the initial email goes missing but the
  independent CloudWatch fallback email still arrives. A fallback delivery fault reaches the primary route.
  How: disable them on staging and keep the fallback inbox receipt. A primary attestation never covers it.
- [ ] **Lost pieces are noticed.** Removing, un-confirming or filtering a subscriber, or disabling the canary sender,
  observer or receipt consumer, raises an alarm that reaches a working route. DLQ growth and queue age page the named
  owner, and recovery drops nothing.
  How: do each on staging and watch the observer outcome codes (for example `CANARY_RECEIPT_MISSED`,
  `PRIMARY_SUBSCRIPTION_MISSING`) and the alarms.
- [ ] **Recipient change.** The old address gets nothing new, the new one gets real mail, and an attestation for the old
  address does not count.
  How: follow [Change alert recipients](OPERATE.md#change-alert-recipients). The observer reports
  `EMAIL_RECEIPT_UNVERIFIED` until you attest the new address.

## Sign-in and access

- [ ] **Local single-user mode (default): exposure and scope.** A wrong password is refused. The UI is reachable only as
  you intend: on `127.0.0.1`, or behind your own SSO or VPN proxy, and never on the open internet by its raw port. A
  question about an instance outside `ALLOWED_INSTANCE_IDS` is refused. The UI role can do no more than read incident
  evidence and invoke the model and tools, or the AgentCore runtime (with a Model API, it can also read the pinned key
  version). Your security owner accepts in writing that anyone with the shared password can use the model and tools
  and read every incident report, and that the throttle of 20 requests per hour is per browser session only.
  How: try a wrong password, a second browser session and an unlisted instance. Probe the UI host from outside. Assume
  the UI role and call forbidden AWS APIs directly.
- [ ] **Local single-user mode (default): chat works from the UI.** The staging canary calls the candidate
  investigation function, not the UI's own path. A real chat question from the launched UI returns a validated answer
  using the UI role.
  How: launch the UI with `run_customer_ui.py` and `APP_PASSWORD` exported, ask about one listed instance, and check the
  logs and the provider or Bedrock usage.
- [ ] **Allowlist and claims (team mode only).** A listed person who signed in with MFA gets in. An unlisted person
  sees only the not-listed message. A wrong issuer, an old sign-in and a missing or non-`mfa` `amr` claim are each
  refused. A viewer cannot chat. An instance outside the person's list is refused for chat and for `?incident=ID`.
  How: sign in as one investigator, one viewer and one unlisted user. To provoke the other refusals, put a wrong
  `issuer` in `team.toml`, wait past `session_hours`, or sign in without MFA at the provider. Check the message and
  the audit line each time.
- [ ] **Revocation and fail-closed (team mode only).** Removing a person from `team.toml` stops their next request,
  with no restart. A missing or invalid file stops the UI, and it never opens to everyone. Team mode shows its error
  and nothing else when `trustedUserHeaders` is set or XSRF protection is off.
  How: remove a signed-in test user and reload ([OPERATE.md](OPERATE.md#team-access-and-secret-rotation)). Break the
  file on staging and reload. Set `trustedUserHeaders` or turn XSRF protection off, restart, and open the UI.
- [ ] **The audit line (team mode only).** Each chat request, report view and refused sign-in writes one JSON line to
  the UI's standard output and to the CloudWatch Logs group `/PROJECT/ENVIRONMENT/audit`, with no prompt or log text.
  How: ask a question that contains a unique marker word, then check that the log lines show `sub`, `role`, `action`,
  `outcome` and token counts, and that the marker appears in none of them. Check the group has them too, and that the
  UI shows no warning about audit records. Then remove `AUDIT_LOG_GROUP` from the UI's environment and confirm the
  warning appears.
- [ ] **The hourly limit (team mode only).** The per-person limit holds in one UI process and resets when it restarts.
  How: set `chat_per_user_per_hour` to 2 on staging and send two requests. That browser session then stops
  accepting questions. Sign in as the same person in a second browser session and ask one more: it is refused and the
  log shows `RATE_LIMITED`. Restart the UI and confirm the person can chat again.
- [ ] **Edge (team-hosted UI only, either mode).** Host and Origin checks, direct-origin bypass, XSRF, WebSocket,
  cookies, header impersonation and unauthenticated public paths behave as intended.
  How: probe the public hostname and the raw backend origin from outside.

## Bounded investigation canary

- [ ] **Investigation works and fails safely.** One paid canary returns a final answer from successful logs and metrics
  calls. Model denial or outage, tool error, no data, deadline and report failure each leave distinct logs, metrics and
  a durable terminal or degraded status. One incident traces end to end with no raw secrets in logs.
  How: run `verify-candidate`, then the paid canary ([DEPLOY.md](DEPLOY.md)). It calls the candidate investigation
  function. Inject each fault on staging and trace by `incident_id` and `fence`.

## Model API (only with a Model API)

The maintainers have never run the Model API option against a live provider. Prove every item below on staging.

- [ ] **The canary proves key, model and protocol.** The paid canary returns a final answer through the provider. That
  shows the pinned key version can be read, the provider accepts the model name and the protocol's tool calling works.
  A call that takes more than 24 seconds fails, so a normal answer must arrive inside that cap.
  How: run `verify-candidate` and the paid canary ([DEPLOY.md](DEPLOY.md)). Check the provider's usage log for the
  request.
- [ ] **Data egress is approved.** Your data owner approved in writing that redacted log and metric excerpts and chat
  questions leave your AWS account for the provider's endpoint, under the provider's retention, region and terms.
  Redaction is best effort.
  How: record the decision with the provider terms and the `base_url`. Review redacted output on real samples.
- [ ] **Provider quota and budget hold.** The provider's quota, rate limits and billing alert cover your expected alert
  and chat load. Tracaus has no dollar cap, and the provider bills you outside AWS.
  How: set a limit or alert at the provider. Send a burst and watch for rate-limit errors. Tracaus does not retry inside a
  call, so the durable worker retries within its limits.
- [ ] **Token accounting fails closed.** A run stops with `TOKEN_ACCOUNTING_MISMATCH` and does not continue when the
  provider reports more input than Tracaus reserved, or no usage. With an OpenAI-compatible provider the reservation is a
  local estimate, not an upper bound. The Anthropic count endpoint is the provider's own estimate.
  How: observe the stop yourself. With the OpenAI protocol, on a staging release with `bytes_per_token` at 8 (the
  smallest estimate allowed), ask one chat question and expect the run to stop. Then compare real prompts with the
  provider's billed input and qualify the `bytes_per_token` you will use (default 3). Any change is a new release.
- [ ] **Diagnosis quality is measured on your model.** Quality on non-Claude models is unmeasured by the maintainers.
  How: run the paid evaluation with `MODEL_API` set ([evaluations](../evaluations/diagnostics/README.md)). A named
  person reads the results, not just the pass rate.

## Load and quota

- [ ] **Limits hold under load.** Chat and alert load, queue capacity, deadlines and hard worker termination meet your
  targets. There is no shared chat cap (team mode adds only a per-person hourly limit), and chat shares capacity with
  incident work, so confirm that mix is acceptable. Every accepted event ends in the ledger as active, completed,
  degraded or a terminal failure.
  How: send a burst at your expected peak, then compare ledger counts with accepted events.

## Recovery drill

- [ ] **Restore and erase.** An isolated restore, purge-registry reconciliation and all-version erasure succeed. Reports
  stay denied after erasure, and no event or replay comes back.
  How: follow [Back up and restore](OPERATE.md#back-up-and-restore) and
  [erasure](OPERATE.md#retention-and-evidence-erasure) on staging.
- [ ] **Rollback and maintenance.** Rolling back and forward again keeps event accounting and pinned policies.
  Maintenance entry and exit suppress only planned checks, and the canary and receipt work after exit.
  How: follow [rollback](OPERATE.md#roll-back-and-retire-old-releases) and
  [maintenance](OPERATE.md#maintenance-pausing-and-spend).

## Handover

- [ ] **Owners, budget and CI.** Named primary and backup operators, service, security and budget owners approve the
  handover, emergency procedures and any time-limited exceptions (each with an owner and an expiry). A second operator
  completes the runbooks without help from the implementer. Spend matches your approved budget. CI passes on the
  frozen source.
  How: the second operator runs the drill and signs the record. Check your billing console and the CI run.
