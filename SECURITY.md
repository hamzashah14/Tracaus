# Security reporting

Do not post credentials, customer data, exploit details or raw cloud responses in
public issues. If the repository enables GitHub private vulnerability reporting,
use its **Security → Report a vulnerability** form. If it is unavailable, request
a private contact channel from the maintainer without disclosing vulnerability
details publicly. No dedicated security email or response SLA is established.

Include affected source/version, a synthetic reproduction, impact and relevant
denial/scope boundaries. Operators retain responsibility for their own AWS account,
identity provider, hosting, telemetry, data classification, costs and incident response.

The project is locally tested and has not completed live production qualification.
Before production use, complete the [live acceptance checklist](docs/ACCEPTANCE.md)
and rehearse the security procedures in [operations](docs/OPERATE.md).

## Deployment security model

Tracaus is customer-operated, so you choose how much access it gets. Read this before you
expose the UI to anyone else.

**Default: local single-user mode.** One shared password (`APP_PASSWORD`, at least 12
characters) protects the web UI. Chat runs with the UI role's AWS credentials. These protections still apply: the `ALLOWED_INSTANCE_IDS` allowlist,
pinned numeric tool versions, bounded `RUNTIME_LIMITS`, release binding, the diagnostic
policy and redaction. These do not exist: per-person sign-in, audit or revocation, a
shared spend cap, a lockout after wrong passwords and dedicated chat capacity (chat uses
the same tool functions or AgentCore runtime as incident investigations). The throttle of
20 requests per hour is kept per browser session, so a new session resets it. Anyone who
has the password can use the model and tools the UI role can reach and read every
incident report. Keep the UI on `127.0.0.1` or behind your own SSO or VPN proxy.

**Optional: team mode.** Start the UI with `--team-file` (it sets `TRACAUS_TEAM_FILE`) and several people can share one UI. They sign in
through Streamlit's OIDC login with your identity provider. A `team.toml` allowlist on the UI
host says who may use which instances, as a viewer or an investigator. Tracaus checks the token's
issuer, the immutable subject, the sign-in age and, by default, an `mfa` value in the `amr`
claim. A missing or invalid file stops the UI and never opens it to everyone. Each chat request,
report view and refused sign-in writes one audit line to the UI's standard output, never with
prompt or log text. Limits you accept: one AWS role serves every person, and Tracaus's code, not
IAM, enforces each person's instance list, so anyone who can run code in the UI process or read
its environment holds that role. There is no remote logout: removing a person applies on their
next request, but a request already running finishes. The hourly chat limit is counted per person
inside one UI process and resets on restart. Team mode has only been tested offline, never
against a real identity provider.

**Local tools mode (development only).** With `TRACAUS_LOCAL_TOOLS` set, the UI process runs the
two read-only tool handlers itself with your own AWS credentials, and nothing is deployed. The
UI process then holds CloudWatch Logs and Metrics read access directly. You lose the pinned
immutable tool code, the per-function IAM scoping and the instance and log-group scope baked
into a deployment. Tracaus still checks that scope in code, from a file you control, but IAM no
longer backs those checks up. The mode needs `ENVIRONMENT=development`. Use it only with the
password UI on your own machine, keep the UI on `127.0.0.1`, and use the deployed tools for
anything shared or production.

**Model provider.** With Bedrock, redacted excerpts go to Amazon Bedrock in your account
and region. With a Model API, redacted log and metric excerpts and your chat questions leave your
AWS account for that provider's HTTPS endpoint. The provider sets its own retention and
quotas and bills you, and redaction is best effort. The API key lives in a pinned
Secrets Manager version and never in configuration or environment variables. In the
default mode the UI role can read that version. The Model API option has never run
against a live provider.
