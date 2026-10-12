"""Customer-operated Tracaus web client. Run with: streamlit run app.py."""

import hmac
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from tracaus import chat, team
from tracaus import status as incident_status
from tracaus.config import AppConfig

load_dotenv()
settings = AppConfig.from_env()
TEAM_FILE = settings.team_file
ASSETS = Path(__file__).resolve().parent / "assets"
st.set_page_config(
    page_title="Tracaus · Infrastructure investigations",
    page_icon=str(ASSETS / "tracaus-mark.png"),
    layout="wide",
)
# White wordmark for the dark theme; the square mark shows when the sidebar is collapsed.
st.logo(str(ASSETS / "tracaus-logo-white.png"), size="large", icon_image=str(ASSETS / "tracaus-mark.png"))
st.markdown(
    """<style>
.block-container { max-width: 1120px; padding-top: 4.5rem; padding-bottom: 5rem; }
[data-testid="stSidebar"] { border-right: 1px solid #243140; }
[data-testid="stChatMessage"] { border: 1px solid #243140; border-radius: 14px; }
.tracaus-label { color: #5eead4; font-size: .73rem; font-weight: 700; letter-spacing: .18em; margin-bottom: .6rem; }
.tracaus-title { font-size: clamp(2rem, 4vw, 3.3rem); line-height: 1.12; font-weight: 650; letter-spacing: -.04em; margin-bottom: .8rem; }
.tracaus-subtitle { color: #9fafc2; line-height: 1.6; max-width: 670px; margin-bottom: 1.7rem; }
</style>""",
    unsafe_allow_html=True,
)


# Fixed step names from the runtime, never model, log or user text.
STEPS = {
    "thinking": "Thinking…",
    "tool:fetch_logs": "Reading logs…",
    "tool:fetch_metrics": "Reading metrics…",
    "tool:fetch_ecs": "Reading ECS service state…",
    "tool": "Using a read-only tool…",
    "checking": "Checking the answer against the evidence…",
}


def clear_conversation():
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.last_prompt = ""
    st.session_state.connection_state = "Configured · not yet verified"


def sign_out():
    st.session_state.authenticated = False
    st.session_state.pop("auth_at", None)
    clear_conversation()
    if TEAM_FILE and st.user.get("is_logged_in", False):
        st.logout()


def authenticate():
    password = st.session_state.get("workspace_password", "")
    accepted = hmac.compare_digest(password.encode(), settings.password.encode())
    st.session_state.authenticated = accepted
    st.session_state.login_error = not accepted
    st.session_state.pop("workspace_password", None)
    if accepted:
        st.session_state.auth_at = time.monotonic()
    else:
        time.sleep(1)


if "messages" not in st.session_state:
    clear_conversation()
if "attempts" not in st.session_state:
    st.session_state.attempts = []
if (
    not TEAM_FILE
    and st.session_state.get("authenticated")
    and time.monotonic() - st.session_state.get("auth_at", 0) > chat.SESSION_SECONDS
):
    sign_out()
    st.session_state.session_expired = True

member = None
roster = None
if TEAM_FILE:
    # Streamlit's native OIDC verifies state, nonce and signature. Never trust browser headers
    # or session_state as an identity.
    st.session_state.authenticated = False
    if st.get_option("server.trustedUserHeaders") or not st.get_option("server.enableXsrfProtection"):
        clear_conversation()
        st.error("Team mode requires XSRF protection and disabled trusted-header identity overrides.")
        st.stop()
    if not st.user.get("is_logged_in", False):
        st.subheader("Sign in to your workspace")
        st.caption("Use your organization's identity provider and multi-factor authentication.")
        if st.button("Sign in with SSO", type="primary"):
            try:
                st.login()
            except Exception:
                st.error("SSO is not configured. Ask the deployment operator to check the sign-in settings.")
        st.stop()
    try:
        roster = team.load(TEAM_FILE, settings.allowed_instances())
    except (team.TeamError, ValueError) as error:
        clear_conversation()
        st.error(f"The team access list cannot be used. {error}")
        st.stop()
    try:
        member = roster.authorize(st.user.to_dict())
    except team.TeamDenied as denied:
        clear_conversation()
        if st.session_state.get("team_denied") != denied.reason:
            st.session_state.team_denied = denied.reason
            team.audit(
                str(st.user.get("sub", ""))[:256], None, None, "sign_in", "DENIED_" + denied.reason.upper()
            )
        st.error(team.DENIED_MESSAGES[denied.reason])
        st.button("Sign out", on_click=sign_out, key="team_denied_sign_out")
        st.stop()
    st.session_state.pop("team_denied", None)
    st.session_state.authenticated = True

with st.sidebar:
    st.caption("Your cloud. Your investigation.")
    st.divider()
    st.markdown("**Workspace**")
    st.caption("Customer-operated · AWS / CloudWatch")
    if st.session_state.get("authenticated"):
        st.button("New conversation", on_click=clear_conversation, width="stretch")
        st.button("Sign out", on_click=sign_out, width="stretch")
        st.divider()
        st.markdown("**Connection**")
        st.caption(st.session_state.connection_state if not settings.problems() else "Setup required")
        st.caption(f"Region: {settings.region or 'Not configured'}")
        st.caption(f"Runtime: {settings.runtime_target}")
        st.caption(f"Environment: {settings.environment}")
        if settings.local_tools:
            st.caption("Local tools · this machine's AWS credentials")
    st.divider()
    st.caption(
        "Investigations read the cloud resources allowed by your deployment. Review recommendations before making changes."
    )

st.markdown('<div class="tracaus-label">INFRASTRUCTURE INTELLIGENCE</div>', unsafe_allow_html=True)
st.markdown('<div class="tracaus-title">Investigate with context.</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="tracaus-subtitle">Connect an incident, its logs and its metrics. Tracaus helps you work from evidence toward an explanation—inside your own cloud.</div>',
    unsafe_allow_html=True,
)

if not TEAM_FILE and len(settings.password) < 12:
    with st.container(border=True):
        st.subheader("Set up your workspace")
        st.info("Set APP_PASSWORD to at least 12 characters in your private .env file before signing in.")
        st.code(
            "cp .env.example .env\n# Set APP_PASSWORD and the verified runtime connection settings.",
            language="bash",
        )
        st.caption(
            "No cloud resources are created by opening this app. Deployment instructions are in the project README."
        )
    st.stop()

if not st.session_state.get("authenticated"):
    if st.session_state.pop("session_expired", False):
        st.info("Your session expired. Sign in again to continue.")
    with st.container(border=True):
        st.subheader("Sign in to your workspace")
        st.caption("Use the access password configured by the deployment operator.")
        with st.form("sign_in", clear_on_submit=True):
            st.text_input("Workspace password", type="password", max_chars=256, key="workspace_password")
            st.form_submit_button("Open workspace", type="primary", on_click=authenticate)
        if st.session_state.get("login_error"):
            st.error("The workspace password did not match.")
    st.stop()

requested_incident = st.query_params.get("incident")
if requested_incident:
    with st.container(border=True):
        st.subheader("Incident status")
        try:
            if member is not None:
                snapshot = incident_status.load(requested_incident, allowed=member.instances)
            else:
                snapshot = incident_status.load(requested_incident)
            if snapshot is None:
                st.info("This incident was not found or has passed its retention period.")
            else:
                st.write({key: value for key, value in snapshot.items() if key != "report"})
                if snapshot.get("report"):
                    label = (
                        "Partial investigation checkpoint"
                        if snapshot.get("partial")
                        else "Investigation report"
                    )
                    st.text_area(label, snapshot["report"], height=280, disabled=True)
                if member is not None:
                    if "viewed" not in st.session_state:
                        st.session_state.viewed = set()
                    if requested_incident not in st.session_state.viewed:
                        st.session_state.viewed.add(requested_incident)
                        team.audit(member.sub, member.role, snapshot.get("instance_id"), "report", "OK")
        except (ValueError, RuntimeError, OSError):
            st.warning("Incident status is unavailable. Ask the deployment operator to check storage access.")
        except Exception:
            # AWS client errors can include private infrastructure details.
            st.warning("Incident status is unavailable. Ask the deployment operator to check storage access.")

problems = settings.problems()
if problems:
    with st.container(border=True):
        st.subheader("Connect your runtime")
        st.info("Your workspace is ready for configuration. Runtime connectivity has not been checked.")
        for problem in problems:
            st.markdown(f"- {problem}")
        if settings.local_tools:
            connection_example = "\n".join(
                [
                    "ENVIRONMENT=development",
                    "BEDROCK_REGION=",
                    "BEDROCK_MODEL_ID=",
                    "EXPECTED_ACCOUNT_ID=",
                    "RUNTIME_LIMITS=",
                    "TRACAUS_LOCAL_TOOLS=",
                ]
            )
        else:
            connection_example = (
                f"RUNTIME_TARGET={settings.runtime_target}\nBEDROCK_REGION=\nBEDROCK_MODEL_ID=\n"
                "EXPECTED_ACCOUNT_ID=\nALLOWED_INSTANCE_IDS=\nRUNTIME_LIMITS=\nRUNTIME_RELEASE=\n"
            )
            connection_example += (
                "AGENTCORE_RUNTIME_ARN=\nAGENTCORE_ENDPOINT="
                if settings.runtime_target == "agentcore"
                else "LOGS_TOOL_ARN=\nMETRICS_TOOL_ARN=\n# ECS_TOOL_ARN=  (only with ECS services)\n"
                "# Development only: TRACAUS_LOCAL_TOOLS=<local tools file> replaces RUNTIME_RELEASE and the tool ARNs."
            )
        st.code(connection_example, language="bash")
        st.caption(
            "Use your AWS profile, SSO session or workload role. The first investigation verifies that the configured agent can respond."
        )
    st.chat_input("Complete the connection settings to investigate", disabled=True)
    st.stop()

with st.expander("Connection details", expanded=False):
    st.write(
        {
            "Region": settings.region,
            "Runtime": settings.runtime_target,
            "Model": settings.model_id,
            "Endpoint": settings.agentcore_endpoint
            if settings.runtime_target == "agentcore"
            else "Local tools (this machine's AWS credentials)"
            if settings.local_tools
            else "Local Python runtime",
            "Environment": settings.environment,
            "Status": st.session_state.connection_state,
        }
    )
    st.caption(
        "Configured does not mean connected. A successful response verifies only that request, not overall cloud health."
    )

if not st.session_state.messages:
    st.markdown("**Start with a question**")
    cols = st.columns(3)
    for column, title, detail in zip(
        cols,
        ["Investigate an alert", "Explore a log gap", "Check resource pressure"],
        [
            "Include the instance, alarm and exact time.",
            "Compare activity before and after the incident.",
            "Correlate CPU, memory and disk with symptoms.",
        ],
    ):
        with column, st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(detail)
    st.caption("Example: What changed on i-0123456789abcdef0 around 2026-09-24T15:00:00+05:00?")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["content"]:
            st.markdown(message["content"])
        if message.get("status") in {"error", "partial"}:
            st.warning(message["message"])
            st.caption(f"{message['code']} · Reference {message['reference']}")
            if message["status"] == "partial":
                st.caption("Partial result · the investigation did not complete.")
        if message.get("usage"):
            used = sum(message["usage"].get(key, 0) for key in ("input_tokens", "output_tokens"))
            took = f" · {message['seconds']:.0f} s" if message.get("seconds") else ""
            st.caption(f"{used:,} tokens{took}")

now = time.monotonic()
st.session_state.attempts = chat.recent_attempts(st.session_state.attempts, now)
work_cap = chat.MAX_REQUESTS_PER_HOUR
if member is not None:
    work_cap = roster.chat_per_hour
    st.caption(
        f"Your access: {member.role} · {len(member.instances)} authorized instance(s) · up to {work_cap} investigations per hour. Evidence is redacted; review recommendations before taking action."
    )
work_limit = len(st.session_state.attempts) >= work_cap
scope_limit = member is not None and member.role != "investigator"
if scope_limit:
    st.info("Your viewer role allows report access. Ask your operator for investigation access.")
history_limit = len(st.session_state.messages) >= chat.MAX_HISTORY_MESSAGES
if work_limit:
    st.info(
        "This browser session has reached its hourly investigation limit. Try again after earlier requests leave the one-hour window."
    )
if history_limit:
    st.info(
        "This conversation reached its history limit. Start a new conversation to reset the agent context."
    )
retry = False
if st.session_state.messages and st.session_state.messages[-1].get("status") in {"error", "partial"}:
    retry = st.button("Retry in a new conversation", disabled=work_limit or scope_limit)
prompt = st.chat_input(
    "Describe the incident. Include an instance ID and timestamp…",
    max_chars=chat.MAX_PROMPT_CHARS,
    disabled=work_limit or history_limit or scope_limit,
)
if retry:
    prompt = st.session_state.last_prompt
if prompt and prompt.strip():
    if member is not None and member.role != "investigator":
        team.audit(member.sub, member.role, None, "chat", "DENIED_ROLE", instance_count=len(member.instances))
        st.stop()  # Defence in depth: the input is disabled for viewers.
    if member is not None and not team.admit(member.sub, roster.chat_per_hour):
        team.audit(
            member.sub, member.role, None, "chat", "RATE_LIMITED", instance_count=len(member.instances)
        )
        st.info("You reached your hourly investigation limit. Try again later.")
        st.stop()
    if retry:
        clear_conversation()  # Only after the checks pass: a refused retry keeps the conversation.
    st.session_state.attempts.append(time.monotonic())
    st.session_state.last_prompt = prompt
    with st.chat_message("user"):
        st.markdown(prompt)
    started = time.monotonic()
    with st.chat_message("assistant"), st.status("Starting the investigation…", expanded=True) as box:

        def show(step):
            box.update(label=STEPS.get(step, "Working…"))
            box.write(STEPS.get(step, "Working…"))

        if member is not None:
            result = chat.invoke(
                prompt,
                st.session_state.session_id,
                settings,
                history=st.session_state.messages,
                allowed=member.instances,
                progress=show,
            )
            usage = result.usage or {}
            team.audit(
                member.sub,
                member.role,
                None,
                "chat",
                "OK" if result.status == "ok" else result.status.upper(),
                instance_count=len(member.instances),
                tokens={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)}
                if usage
                else None,
            )
        else:
            result = chat.invoke(
                prompt,
                st.session_state.session_id,
                settings,
                history=st.session_state.messages,
                progress=show,
            )
        box.update(
            label="Done" if result.status == "ok" else "Stopped before a complete answer",
            state="complete" if result.status == "ok" else "error",
            expanded=False,
        )
    st.session_state.messages = chat.append_exchange(
        st.session_state.messages, prompt, result, seconds=time.monotonic() - started
    )
    st.session_state.connection_state = (
        "Last request succeeded" if result.status == "ok" else "Last request incomplete"
    )
    st.rerun()
