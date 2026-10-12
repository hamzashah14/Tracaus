# Team mode: replace the identity module with OIDC login and an allowlist

Status: draft for review. Nothing in this document is implemented yet.

## 1. Purpose and scope

Tracaus's current architecture stays as it is: the deployment automation, the
durable incident pipeline, the observers, the optional AgentCore runtime, the
Model API provider and local tools mode. This change touches one thing only.

Today, letting several people use a shared Tracaus UI requires the optional
identity module: OIDC with MFA checks, signed session tickets, a DynamoDB
identity table, a Secrets Manager signing key, a session-issuer role, a
dedicated chat-gateway Lambda, per-user and global quotas, a CloudTrail audit
trail, and CLI commands to grant, review and revoke access. It works as
designed, but it is the largest source of setup work, code and documentation
in the project.

This design replaces that module with a much smaller **team mode**:

- people sign in with the OIDC login built into Streamlit;
- one small allowlist file says who may use which instances;
- there is no database, signing secret, gateway function or extra stack.

### Success criteria

1. A team can share one hosted Tracaus UI with per-person sign-in, per-person
   instance access and a per-request audit line, with no AWS resources beyond
   the ones the default deployment already creates.
2. Default (local single-user) mode and the rest of the architecture behave
   exactly as they do today. The rendered CloudFormation for a deployment
   without the identity block is unchanged.
3. The deployment tool no longer has an identity path: no extra stages, no
   `init --identity`, no first-grant step and no staging ticket.

### Non-goals

- Per-person revocation lists, shared quotas, session stores or an
  identity-data erasure tool. They go away with the module. A team that needs
  them should keep using the version at the `v0-enterprise` tag.
- Hosting the UI. The team runs it where it already runs services and puts
  HTTPS in front of it.
- The log-source, agent-configuration and IAM work (section 10).

## 2. Decisions already made

- Keep the whole current architecture and feature set.
- Replace the OIDC identity module with Streamlit's OIDC login plus an
  allowlist. Remove the old module rather than keep two ways to do the same
  job.
- Consider log sources, CloudWatch agent configuration, IAM roles and the
  log-group convention next, as separate designs.

## 3. What the identity module is today

Files that go away: `tracaus/identity.py` (383 lines), `tracaus/quotas.py` (114),
`tracaus/chat_gateway.py` (69), `infra/identity.py` (286), `infra/identity_ops.py`
(211), `infra/evidence_audit.py` (189), `infra/chat.py` (80),
`scripts/dev/validate_identity.py` and `examples/identity.example.json`.
The identity branches also come out of `tracaus/config.py`, `tracaus/execution.py`,
`tracaus/chat.py`, `tracaus/status.py`, `app.py`, `infra/automation.py`,
`infra/durable*.py`, `infra/owned_*.py`, `infra/deployment_preflight.py` and
`scripts/run_customer_ui.py`. Tests are about 1,300 lines (`test_identity.py`,
`test_identity_ops.py`, `test_identity_wiring.py` and the identity parts of
other files).

Deployment stages it adds: `identity-foundation`, `identity-secret`,
`identity-foundation-bound`, `chat-runtime` and `initial-access`. Resources it
creates: an identity table, a signing secret, a session-issuer role, a chat
gateway function and role, a CloudTrail trail and an audit bucket.

Things that look related but stay, verified in the code:

- `tracaus/governance.py` (`Erasure`) erases incident evidence (incident rows and
  stored report versions), not identity data. It backs the `erase-plan` and
  `erase-apply` commands, which stay.
- `infra/security_ops.py` keeps `erase-plan`, `erase-apply`, `recipients-plan`
  and `recipients-apply`. Only `access-review` is removed.
- `verify_ui_role` (in `infra/identity.py`) is also used by default mode, so it
  moves into `infra/durable_ops.py`. The reference-only and binding check in
  `identity_ops.guard` is also used by the recipient and erase commands, so a
  local copy moves into `security_ops.py`.
- `tracaus/work_policy.py` is trimmed, not deleted: the diagnostics evaluation
  script still reads its chat limits.
- `tracaus/safety.py` keeps its redaction vocabulary.

## 4. The new team mode

### Enabling it

Team mode is on when the UI host sets `TRACAUS_TEAM_FILE` to the path of a team
file. When it is not set, the UI behaves as it does today in default mode (one
shared password, local use). When it is set, `APP_PASSWORD` is not used.

There is nothing for the deployment tool to know about. The deployment
configuration has no team block, and the tool creates nothing for team mode.

### Sign-in

Streamlit's native OIDC login (`st.login`), configured in
`.streamlit/secrets.toml` under `[auth]`. The existing guard stays: team mode
refuses to run if Streamlit's trusted-header override is set or XSRF
protection is off.

The installed OIDC library forwards `prompt` to the provider but ignores
`max_age`, which the current guide recommends. The new guide uses
`prompt = "login"` to force a fresh sign-in and relies on `session_hours` for
the age limit. Whether a provider sends `auth_time` and `amr` cannot be
checked offline.

Team mode requires `RUNTIME_TARGET=standalone`: chat runs in the UI process,
while the AgentCore host applies its own instance allowlist. The UI reports a
configuration problem for any other target.

### The team file (`team.toml`)

Read with Python's standard `tomllib`. It holds no secrets.

```toml
issuer = "https://login.example.invalid"     # must equal the token's iss
require_mfa = true                           # require "mfa" in the amr claim
session_hours = 8                            # maximum age counted from sign-in

[limits]
chat_per_user_per_hour = 20

[[users]]
sub = "OIDC_SUBJECT"        # the immutable subject, never an email address
role = "investigator"       # "viewer": reports only; "investigator": may chat
instances = ["i-0123456789abcdef0"]   # instance IDs from the deployment inventory
```

Rules:

- `sub` values are unique. At most 100 users.
- `instances` must be a subset of the deployment's allowed instances. An
  instance that is not in the deployment is a configuration error.
- Unknown keys and malformed values are rejected with an error that names the
  field and never echoes the value.
- An invalid file stops the UI from starting, with a clear message. It never
  falls back to open access.

### Authorization

A request is allowed only when all of these hold:

1. the token's issuer equals `issuer` and the signed-in subject is listed;
2. the token's `auth_time` (or `iat` when the provider sends no `auth_time`) is
   within `session_hours`, and, when `require_mfa` is true, its `amr` claim
   contains `mfa`;
3. the role permits the action (viewers cannot chat);
4. the instance is in that user's list.

`exp` is deliberately not checked. Streamlit keeps its sign-in cookie for 30
days and never refreshes the token, and ID tokens usually expire within the
hour, so requiring a future `exp` would sign people out early. `session_hours`
is the limit. A timestamp more than five minutes in the future is also refused.
Every claim is type-checked, so a malformed claim is a denial and never an
error.

A signed-in user who is not listed sees a message saying so and nothing else.
If the provider supplies no `amr` claim and `require_mfa` is true, the user is
denied with a message that explains why.

Report links (`?incident=ID`) follow the same rule: the incident's instance
must be in the user's list.

### Revocation and limits

- The file is re-read whenever its modification time changes, so removing a
  user takes effect on that user's next request. No restart is needed.
- The per-user chat limit is held in memory in the UI process. It resets when
  the process restarts and is not shared across processes.

### Audit

Each request writes one structured JSON line to standard output: time,
subject, role, the instance when the action names one (report views), the
number of authorized instances, action, outcome and token counts when known.
It never contains prompt text or log content. The team's container or process platform keeps the
log.

### Hosting

The launcher keeps binding the UI to `127.0.0.1`. A team puts a TLS reverse
proxy on the same host (or runs a container with its own network setup) and
serves the UI at its fixed HTTPS address. That address is also the
`status_base_url` that alert emails link to.

### Where chat runs

In the UI process with the UI role's AWS credentials, the same path default
mode uses today. The instance allowlist, pinned tool versions, bounded limits,
release binding, diagnostic policy and redaction still apply.

## 5. What is removed

- Code: the identity module, quotas, the chat gateway and its subprocess
  client, the identity branches of the deployment and UI code, and the
  identity checks in `status.py`.
- Deployment: the five stages above, `init --identity`, the `identity` and
  `security` blocks of `runtime.json`, `initial_access` in `automation.json`,
  the staging-ticket path of the canary and the launcher, and the identity
  commands (grant, access review, pin signing key, identity erasure).
- Resources never created any more: the identity table, signing secret,
  session-issuer role, chat gateway function and role, the audit trail and its
  bucket.
- Tests for all of the above, and `scripts/dev/validate_identity.py` with its
  CI step and example file.
- Documentation: the team sign-in section of the deployment guide, the
  identity-only sections of the operations guide, the OIDC items of the
  prerequisites and acceptance pages, and the identity wording in the security
  page. All are rewritten for team mode.

A deployment configuration that still contains the removed blocks is rejected
with one clear message saying team sign-in changed and pointing to the guide.
No deployment of this project exists yet, so there is no data to migrate.

## 6. What does not change

Default-mode behavior, the model providers, local tools mode, AgentCore, the
observers, recipient and replay tooling, the diagnostic evaluation, release
verification and CI. The rendered stages for a deployment without an identity
block must be byte-identical (after normalizing content hashes) before and
after this change, which is how the removal is proven safe.

## 7. Interfaces that change

| Interface | Before | After |
| --- | --- | --- |
| UI host environment | `KIRA_AUTH_MODE=oidc` plus session, key and policy variables | `TRACAUS_TEAM_FILE` only |
| `runtime.json` | optional `identity` and `security` blocks | removed; rejected with a clear message |
| `automation.json` | `initial_access` list | removed |
| `init` | `--identity` flag | removed |
| Launcher | staging-ticket option; drops identity variables | no ticket option; accepts `--team-file` and passes `TRACAUS_TEAM_FILE` to the UI |
| Canary | needs a private ticket in identity mode | always the ticket-free `runtime_canary` path |

## 8. Trade-offs stated plainly

- There is no central session store, so there is no remote logout and no
  revocation list. Removing a user from the file ends their access on their
  next request, but an in-flight request completes.
- One AWS role serves every user of the hosted UI. Per-person scope is
  enforced in Tracaus's code, not by IAM. Anyone who can run code in the UI
  process, or read its environment, holds that role.
- Rate limits are per process, so several processes allow several times the
  limit. Run one process, or accept that.
- The audit log is only as durable as the platform that keeps standard output.
- Compared with today, a hosted team UI now also needs the UI host to be
  trusted. That was already true of default mode.

## 9. Testing

- Remove the identity tests and add focused offline tests for: the team file
  loader (valid, and each rejection, with no value echoed); each authorization
  rule (not listed, wrong issuer, expired `auth_time`, missing or wrong `amr`,
  viewer cannot chat, instance not in the user's list); revocation by editing
  the file; the in-memory limit; the audit line contents (and that it carries
  no prompt or log text); report links; invalid file stops startup; the
  trusted-header and XSRF guard; and the UI flow with Streamlit's app tester.
- Prove the removal with the render comparison in section 6, and the full
  existing suite, lint, validators, build verification and secret scan.
- The first live check is part of the real staging deployment, as for the rest
  of the project.

## 10. Next designs (out of scope here)

These come after team mode and each gets its own design:

1. **Log sources.** Two supported options. A: logs already in existing log
   groups (Docker, Nginx, application), mapped to an instance by explicit
   configuration, with an optional stream prefix when instances share a group.
   B: Tracaus's own convention, where the user creates log groups named by
   instance ID (`<prefix>/<instance-id>/<suffix>`) and Tracaus's generated agent
   configuration ships to them. Today only B exists: the tool handlers, the
   templates and the IAM scope all assume it.
2. **CloudWatch agent configuration** per application type (Nginx, Docker with
   the awslogs driver, plain application logs), including which metrics to
   publish.
3. **IAM roles.** Reduce the three roles that must exist today and the long
   permission lists, for example with a bootstrap template and ready-made
   policies.
4. **Alarm intake.** Today the pipeline accepts only alarms named by its own
   convention (the name must contain the instance ID), so a team's existing
   alarms are rejected. Accepting them needs an explicit alarm-to-instance
   mapping.

## 11. Risks and open questions

- **Token claims.** Streamlit passes the provider's ID-token claims through
  unfiltered, but providers differ: some send `auth_time` and `amr`, some do
  not. If `amr` is missing, `require_mfa = true` blocks that provider. The
  fallback is `require_mfa = false`, with MFA enforced at the provider and
  stated in the documentation. Nothing here has run against a real provider.
- **Silent re-login.** With the default `prompt`, some providers reuse their
  session and keep an old `auth_time`, which would deny the user repeatedly.
  The guide uses `prompt = "login"`.
- **Shared files.** Several modules are shared with non-identity code. The
  plan must trim, not delete, those (for example the default work limits).
- **Size.** The removal is large, roughly 2,000 lines of code and 1,300 lines
  of tests. The render comparison and the existing suite are the safety net.
- **Nothing here has run on real AWS.**
