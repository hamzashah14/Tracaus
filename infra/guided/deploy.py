"""`deploy`, `status` and `ui`: run the reviewed plan with live progress, explain every stop and failure, and
open the UI.

`apply` itself is unchanged. It runs as a child process; this module follows its journal (`state.json`) and turns
each stop into a question with a plain answer. Nothing here deletes anything in AWS."""

import contextlib
import dataclasses
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from infra.automation import factory, private_json
from infra.guided import roles as guided_roles
from infra.guided.common import ROUTING_STAGE, STAGES, GuidedError, aws_env, parse_json_object, run
from infra.guided.console import clean
from infra.spec import ROOT

STOP_PAID = "Candidate ready"
STOP_EMAILS = "Confirm the subscription emails"
STOP_EXPIRED = "Canary receipt expired"
STOP_AMBIGUOUS = "Previous paid canary outcome is ambiguous"
READY = "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
MAX_ROUNDS = 12  # each round waits up to 15 minutes inside apply
POLL, HEARTBEAT = 2.0, 30.0
PASSWORD_FILE = "ui-password"


@dataclasses.dataclass
class DeployOptions:
    yes: bool = False
    skip_check: bool = False


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def log_tail(directory, lines=8):
    """The last lines of the private operations log, cleaned. The tools write no raw cloud payloads to it."""
    try:
        text = (Path(directory) / "operations.log").read_text(errors="replace")
    except OSError:
        return []
    return [clean(line, 200) for line in text.strip().splitlines()[-lines:] if line.strip()]


def private_open(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb")


class Deploy:
    def __init__(
        self,
        console,
        prompter,
        work_dir,
        options=None,
        *,
        root=ROOT,
        clients_for=factory,
        popen=subprocess.Popen,
        runner=run,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.console, self.prompter, self.options = console, prompter, options or DeployOptions()
        self.work_dir, self.root, self.clients_for = Path(work_dir), Path(root), clients_for
        self.popen, self.runner, self.sleep, self.clock = popen, runner, sleep, clock
        self.flags, self.stages, self.plan_hash, self.profile = [], [], None, None

    # ------------------------------------------------------------------ the run

    def run(self):
        self.console.begin(5)
        self.step_plan()
        self.step_check()
        self.step_confirm()
        self.step_apply()
        self.step_finish()
        return 0

    @property
    def config(self):
        return self.work_dir / "automation.json"

    def _command(self, name, *extra):
        return [
            sys.executable,
            "-m",
            "infra.automation",
            name,
            "--config",
            str(self.config),
            "--work-dir",
            str(self.work_dir),
            *extra,
        ]

    # ------------------------------------------------------------------ 1 plan

    def step_plan(self):
        with self.console.step("Reading your plan"):
            settings = read_json(self.config)
            saved = read_json(self.work_dir / "plan.hash")
            if not settings or not saved:
                raise GuidedError(
                    "No settings were found in this folder",
                    f"{self.work_dir.name}/ has no automation.json or plan.hash",
                    "Nothing was changed.",
                    "Run ./bin/tracaus setup first.",
                )
            self.profile = settings.get("profile")
            code, status = self.runner(["git", "status", "--porcelain"], cwd=self.root, timeout=30)
            changed = [line for line in status.splitlines() if line.strip()]
            if code != 0 or changed:
                raise GuidedError(
                    "The checkout is not clean",
                    f"{len(changed)} file(s) differ from the last commit"
                    if changed
                    else "git could not be read",
                    "Nothing was changed. 'apply' records the exact source commit.",
                    "Commit or stash your changes, then run deploy again. Files under .local/ never count.",
                )
            code, output = self.runner(
                self._command("dry-run"), cwd=self.root, env=aws_env(None), timeout=300
            )
            if code != 0:
                raise GuidedError(
                    "The plan could not be rebuilt",
                    " | ".join(clean(line, 160) for line in output.strip().splitlines()[-4:]),
                    "Nothing in AWS was touched.",
                    "Fix the settings named above (or run setup again).",
                )
            try:
                summary = parse_json_object(output)
            except ValueError:
                raise GuidedError(
                    "The plan output could not be read", "", "Nothing was changed.", "Run setup again."
                ) from None
            self.plan_hash, self.stages = summary["plan_hash"], summary["stages"]
            if self.plan_hash != saved.get("plan_hash"):
                self.console.warn(
                    "The plan differs from the one setup showed you (the source commit or a setting changed)."
                )
                if not self.prompter.confirm("replan", "Use the new plan?", default=False):
                    raise GuidedError(
                        "The plan changed since setup",
                        "the source commit or a setting is different now",
                        "Nothing was changed.",
                        "Review the change, then run deploy again and confirm the new plan, or run setup again.",
                        code=3,
                    )
                private_json(self.work_dir / "plan.hash", {"plan_hash": self.plan_hash})
            self.console.ok(f"Plan {self.plan_hash[:12]} with {len(self.stages)} stages")
            state = read_json(self.work_dir / "state.json")
            if state:
                if state.get("plan_hash") != self.plan_hash:
                    raise GuidedError(
                        "The settings or the source changed after this deployment started",
                        "the journal belongs to a different plan",
                        "Nothing was deleted; the earlier journal is kept.",
                        "Start the next release: python -m infra.automation refresh --from-dir "
                        f"{shlex.quote(str(self.work_dir))} --work-dir {shlex.quote(str(self.work_dir) + '-r2')}",
                    )
                done = sum(1 for s in state.get("stages", {}).values() if s.get("status") == "COMPLETE")
                self.console.info(f"Resuming: {done} of {len(self.stages)} stages are already complete.")

    # ------------------------------------------------------------------ 2 check

    def step_check(self):
        with self.console.step("Checking permissions and your account (read-only)"):
            if self.options.skip_check:
                self.console.warn("Skipped (--skip-check). 'apply' repeats the permission check itself.")
                return
            for stale in ("preflight.json", "error.json"):  # never show a blocker list from an earlier run
                (self.work_dir / stale).unlink(missing_ok=True)
            code, output = self.runner(
                self._command("check"), cwd=self.root, env=aws_env(self.profile), timeout=900
            )
            if code == 0:
                self.console.ok("Permissions are sufficient")
                return
            report = read_json(self.work_dir / "preflight.json", {})
            error = read_json(self.work_dir / "error.json", {})
            lines = [
                f"{clean(b.get('action'))} on {clean(b.get('resource'), 90)}: {clean(b.get('decision'))}"
                for b in report.get("blockers", [])[:8]
            ]
            if error:
                lines.append(
                    f"AWS {clean(error.get('operation'))} failed with {clean(error.get('error_code'))}"
                )
            if not lines:  # the check stopped before it could write a report: show what it said
                lines = [clean(line, 200) for line in output.strip().splitlines()[-4:] if line.strip()]
            raise GuidedError(
                "The permission check found problems",
                "; ".join(lines) or "the check printed nothing; see operations.log in the work folder",
                "Nothing was changed in AWS: the check only reads.",
                "Ask your security administrator to grant what is listed (do not use AdministratorAccess), "
                "or check that the operator profile works. Then run deploy again.",
            )

    # ------------------------------------------------------------------ 3 confirm

    def step_confirm(self):
        with self.console.step("Confirming what will be created"):
            for number, stage in enumerate(self.stages, 1):
                self.console.say(f"  {number:>2}. {stage:<26} {STAGES.get(stage, '')}")
            self.console.say(
                f"\nNothing is routed to Tracaus before '{ROUTING_STAGE}'. The paid model request needs your separate yes. "
                "Investigations stay paused. Nothing is ever deleted by these commands."
            )
            if self.options.yes:
                self.console.ok("Continuing (--yes)")
                return
            if not self.prompter.confirm(
                "apply", "Create these resources in your AWS account now?", default=False
            ):
                raise GuidedError(
                    "Stopped before changing AWS",
                    "you answered no",
                    "Nothing was changed.",
                    "Run ./bin/tracaus deploy when you are ready.",
                    code=2,
                )

    # ------------------------------------------------------------------ 4 apply

    def step_apply(self):
        with self.console.step("Deploying (this takes a while; progress is shown as it happens)"):
            once = []
            for _ in range(MAX_ROUNDS):
                code, error = self._apply_once([*self.flags, *once])
                once = []
                state = read_json(self.work_dir / "state.json", {})
                if code == 0 and state.get("status") == READY:
                    return
                if code == 2 and state.get("status") == "WAITING":
                    once = self._stop(state)
                    continue
                raise self._failure(code, error, state)
            raise GuidedError(
                "AWS is still working after several waits",
                "the stages are slow, not failed",
                "Everything created so far is kept.",
                "Run ./bin/tracaus deploy again to keep waiting.",
                code=2,
            )

    def _apply_once(self, extra):
        argv = self._command("apply", "--plan-hash", self.plan_hash, "--wait-seconds", "900", *extra)
        out, err = self.work_dir / "apply.out", self.work_dir / "apply.err"
        with private_open(out) as stdout, private_open(err) as stderr:
            process = self.popen(
                argv,
                cwd=self.root,
                env=aws_env(self.profile),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            try:
                self._watch(process)
            except KeyboardInterrupt:
                self._terminate(process)
                raise
        return process.returncode, " ".join(clean(err.read_text(errors="replace"), 400).split())

    def _watch(self, process):
        started = last = self.clock()
        seen, begun = set(), {}
        while process.poll() is None:
            if self._news(seen, begun):
                last = self.clock()
            elif self.clock() - last >= HEARTBEAT:
                self.console.info(f"still working ({self.clock() - started:.0f}s so far)...")
                last = self.clock()
            self.sleep(POLL)
        self._news(seen, begun)

    def _news(self, seen, begun):
        state = read_json(self.work_dir / "state.json", {})
        fresh = False
        if state.get("built") and "built" not in seen:
            seen.add("built")
            self.console.ok("Built the Lambda packages from the verified wheels")
            fresh = True
        for stage in self.stages:
            record = state.get("stages", {}).get(stage)
            if not record:
                continue
            if record.get("status") == "COMPLETE" and (stage, "done") not in seen:
                seen.add((stage, "done"))
                took = f" ({self.clock() - begun[stage]:.0f}s)" if stage in begun else ""
                self.console.ok(f"{stage}: {STAGES.get(stage, '')}{took}")
                fresh = True
            elif (stage, "start") not in seen and record.get("status") != "COMPLETE":
                seen.add((stage, "start"))
                begun[stage] = self.clock()
                self.console.info(f"{stage}: working - {STAGES.get(stage, '')}")
                fresh = True
        return fresh

    def _terminate(self, process):
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()

    # ------------------------------------------------------------------ stops

    def _stop(self, state):
        """Handle a named stop from `apply`. Returns the one-shot flags for the next round, or raises."""
        message = clean(state.get("next", ""), 300)
        if state.get("auto_resume"):
            self.console.info(f"AWS is still working: {message}")
            return []
        if STOP_PAID in message:
            return self._paid(
                "The next step spends money: ONE bounded model request through the real tools, using your model "
                "provider (Bedrock, or your model API). It proves the whole path works. Run it now?",
                [],
            )
        if STOP_EXPIRED in message or STOP_AMBIGUOUS in message:
            return self._paid(
                "The earlier paid test is no longer valid, or its outcome is unknown (it may already have been billed). "
                "Running it again is a SECOND paid model request. Do it?",
                ["--retry-canary"],
                consent="paid-canary-retry",
                key="paid_retry",
            )
        if STOP_EMAILS in message:
            self.console.say(
                "\nAWS sent subscription emails (alerts and fallback). Open each inbox and click 'Confirm subscription'. "
                "Check spam folders too."
            )
            if not self.prompter.confirm(
                "emails_done", "Have you confirmed them all? (apply checks again right away)", default=False
            ):
                raise GuidedError(
                    "Waiting for the subscription emails",
                    message,
                    "Everything created so far is kept.",
                    "Confirm the emails, then run ./bin/tracaus deploy again.",
                    code=2,
                )
            return []
        raise GuidedError(
            "The deployment stopped at a step that needs you",
            message or "no message was recorded",
            "Everything created so far is kept. Nothing is deleted.",
            "Follow the message above, then run ./bin/tracaus deploy again.",
            code=2,
        )

    def _paid(self, question, once, consent="paid-canary", key="paid_canary"):
        """Ask for a paid step. A retry has its own consent, so agreeing to the first request never covers a second.

        The permission flag stays for later rounds; `once` flags are used one time."""
        if not self.prompter.confirm(key, question, consent=consent):
            raise GuidedError(
                "Stopped before the paid model request",
                "you did not agree to it",
                "Everything created so far is kept.",
                "Run ./bin/tracaus deploy again when you are ready.",
                code=2,
            )
        if "--allow-model-invocation" not in self.flags:
            self.flags.append("--allow-model-invocation")
        return once

    # ------------------------------------------------------------------ failure

    def _failure(self, code, error, state):
        stages = [s for s in self.stages if state.get("stages", {}).get(s, {}).get("status") != "COMPLETE"]
        stage = stages[0] if stages else "(none)"
        routed = ROUTING_STAGE in state.get("stages", {})
        tail = log_tail(self.work_dir)
        reasons = self._stack_reasons(stage)
        cause = " | ".join(part for part in [error, *reasons, *tail[-4:]] if part) or "no detail was recorded"
        if "Configuration/source changed" in error or "Source revision changed" in error:
            next_step = (
                f"python -m infra.automation refresh --from-dir {shlex.quote(str(self.work_dir))} "
                f"--work-dir {shlex.quote(str(self.work_dir) + '-r2')}, "
                "then run setup against the new folder."
            )
        elif any("Required metric unavailable" in line for line in tail + [error]):
            next_step = (
                "A metric your servers should publish is missing (named above). Fix it on the server or task, using "
                "the agent files in collector-examples/, wait a few minutes, then run ./bin/tracaus deploy."
            )
        elif "Permission preflight blocked" in error:
            next_step = "Run ./bin/tracaus deploy again: it shows each blocked permission."
        else:
            next_step = (
                "Fix the cause above, then run ./bin/tracaus deploy. It resumes from the saved journal."
            )
        safe = (
            f"Stopped at stage '{stage}'. Nothing is routed to Tracaus yet, so your alarms and servers are untouched. "
            "CloudFormation undid any failed stack change. Nothing was deleted."
            if not routed
            else f"Stopped at stage '{stage}', after routing began, so alerts may already reach Tracaus. "
            "Investigations stay paused. Nothing was deleted. Run ./bin/tracaus status to see what exists."
        )
        return GuidedError(f"Deployment stopped (exit {code})", cause, safe, next_step, code=1)

    def _stack_reasons(self, stage):
        plan = read_json(self.work_dir / "plan.json", {})
        info = plan.get("resources", {}).get(stage)
        if not info:
            return []
        try:
            cfn = self.clients_for(self.profile)("cloudformation", info["region"])
            events = cfn.describe_stack_events(StackName=info["stack"])["StackEvents"]
        except (ClientError, BotoCoreError, KeyError):
            return []
        reasons = [
            f"{e.get('LogicalResourceId', '?')}: {e['ResourceStatusReason']}"
            for e in events
            if e.get("ResourceStatus", "").endswith("FAILED") and e.get("ResourceStatusReason")
        ]
        return [clean(r, 200) for r in reasons[:3]]

    # ------------------------------------------------------------------ 5 finish

    def step_finish(self):
        with self.console.step("Finishing"):
            state = read_json(self.work_dir / "state.json", {})
            self.console.ok("The backend is deployed and registered. Investigations are paused.")
            if not (self.work_dir / "ui-connection.json").exists():
                self.console.warn("No ui-connection.json was written; only staging deployments write one.")
            self.console.say(
                "\nWhat now:\n"
                "  1. Check that alert emails really arrive (docs/ACCEPTANCE.md).\n"
                "  2. Open the UI:  ./bin/tracaus ui\n"
                f"Status: {clean(state.get('status', ''))}\nNothing about this run can be undone by these commands; "
                "see docs/OPERATE.md to roll back or retire a release."
            )


def status(console, work_dir, *, clients_for=factory, online=True):
    """Read-only: the journal, each stage, what is routed, the stacks if AWS can be read, and the next command."""
    work_dir = Path(work_dir)
    state, plan = read_json(work_dir / "state.json"), read_json(work_dir / "plan.json", {})
    if not state:
        console.say(
            "No deployment has started in this folder. Run ./bin/tracaus setup, then ./bin/tracaus deploy."
        )
        return 0
    stages = plan.get("stages") or list(state.get("stages", {}))
    console.say(f"Deployment status: {clean(state.get('status'))}")
    first = None
    for stage in stages:
        record = state.get("stages", {}).get(stage)
        done = bool(record) and record.get("status") == "COMPLETE"
        if not done and first is None:
            first = stage
        mark = "ok     " if done else ("working" if record else "pending")
        console.say(f"  {mark} {stage:<26} {STAGES.get(stage, '')}")
    routed = ROUTING_STAGE in state.get("stages", {})
    console.say(
        "Routing to Tracaus: "
        + (
            "started (alerts may reach Tracaus; investigations are paused)"
            if routed
            else "not started; your alarms and servers are untouched"
        )
    )
    if online:
        profile = (read_json(work_dir / "automation.json", {}) or {}).get("profile")
        for stage, info in plan.get("resources", {}).items():
            try:
                cfn = clients_for(profile)("cloudformation", info["region"])
                value = cfn.describe_stacks(StackName=info["stack"])["Stacks"][0]["StackStatus"]
            except (ClientError, BotoCoreError, KeyError, IndexError):
                value = "not readable"
            console.say(f"  stack {clean(info['stack'], 60)}: {clean(value)}")
    if state.get("next"):
        console.say(f"Next: {clean(state['next'], 300)}")
    if first and state.get("status") != READY:
        console.say("Run ./bin/tracaus deploy to continue.")
    return 0


# ---------------------------------------------------------------------- ui


def ui_profiles(spec, connection, source_profile, region):
    """The two profile blocks the UI needs: the UI principal role, then the generated UI role through it."""
    names = (
        f"{spec['project']}-{spec['environment']}-ui-principal",
        f"{spec['project']}-{spec['environment']}-ui",
    )
    principal = guided_roles.profile_block(names[0], spec["ui_principal_arn"], source_profile, region)
    ui = guided_roles.profile_block(names[1], connection["ui_role_arn"], names[0], region)
    return names[1], [principal, ui]


def launch_ui(
    console,
    prompter,
    work_dir,
    *,
    config_path=None,
    runner=None,
    opener=webbrowser.open,
    delay=4.0,
    team_file=None,
    prompt_password=False,
):
    """Add the UI profiles, set a password, and start the UI on 127.0.0.1."""
    work_dir = Path(work_dir)
    connection = read_json(work_dir / "ui-connection.json")
    spec = read_json(work_dir / "deployment.json")
    answers = read_json(work_dir / "answers.json", {})
    if not connection or not spec:
        raise GuidedError(
            "The deployment is not finished",
            "there is no ui-connection.json yet",
            "Nothing was changed.",
            "Run ./bin/tracaus deploy first.",
        )
    source = answers.get("profile")
    if not source:
        raise GuidedError(
            "The AWS profile used for setup is not recorded",
            "answers.json has no profile",
            "Nothing was changed.",
            "Run ./bin/tracaus setup again, or add the two UI profiles yourself (docs/DEPLOY.md, section 4).",
        )
    console.begin(1 if team_file else 2)
    with console.step("Preparing the UI profile"):
        try:
            name, blocks = ui_profiles(spec, connection, source, spec["monitor_region"])
            path = Path(config_path) if config_path else guided_roles.config_path()
            if prompter.confirm(
                "write_ui_profiles", f"Add the profile '{name}' to {path}? (a backup is kept)", default=True
            ):
                result = guided_roles.write_profiles(path, blocks)
                for skipped in result["skipped"]:
                    console.warn(
                        f"A profile named {skipped} already exists outside Tracaus's markers; left unchanged."
                    )
                console.ok("UI profile ready")
            else:
                console.say("Add these yourself:\n" + "\n".join(blocks))
        except guided_roles.RolesError as error:
            raise GuidedError(
                "The UI profile could not be set up",
                str(error),
                "The AWS config was not changed.",
                "Add the two profiles by hand (docs/DEPLOY.md, section 4).",
            ) from None
    env = {}
    if not team_file:
        with console.step("Setting the UI password"):
            password = _password(console, prompter, work_dir, prompt_password)
            env["APP_PASSWORD"] = password
    argv = [
        sys.executable,
        str(ROOT / "scripts" / "run_customer_ui.py"),
        "--connection",
        str(work_dir / "ui-connection.json"),
        "--profile",
        name,
    ]
    if team_file:
        argv += ["--team-file", str(team_file)]
    console.say("\nStarting the UI on http://127.0.0.1:8501 (this computer only). Press Ctrl-C to stop it.")
    if delay:
        timer = threading.Timer(delay, _open, (opener,))
        timer.daemon = True
        timer.start()
    return (runner or subprocess.call)(argv, env={**os.environ, **env})


def _open(opener):
    with contextlib.suppress(Exception):  # a missing browser must never stop the UI
        opener("http://127.0.0.1:8501")


def _password(console, prompter, work_dir, prompt):
    path = Path(work_dir) / PASSWORD_FILE
    if prompt:
        value = prompter.secret("ui_password", "UI password (12 or more characters)")
        if len(value) < 12:
            raise GuidedError(
                "The password is too short",
                "it needs 12 or more characters",
                "Nothing was changed.",
                "Try again.",
            )
        return value
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise GuidedError(
                "The saved password file is not private",
                f"{path.name} must be mode 600 and not a link",
                "Nothing was changed.",
                f"Run: chmod 600 {path}  (or delete it to get a new password)",
            )
        value = path.read_text().strip()
        if len(value) < 12:
            raise GuidedError(
                "The saved password is too short or empty",
                f"{path.name} must hold 12 or more characters",
                "Nothing was changed.",
                f"Delete {shlex.quote(str(path))} to get a new password.",
            )
        console.ok(f"Using the saved password in {path.name} (mode 600)")
        return value
    value = secrets.token_urlsafe(15)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)  # the file appears whole or not at all
    finally:
        temporary.unlink(missing_ok=True)
    console.ok(f"Created a password and saved it in {path.name} (mode 600). It is shown once, here:")
    console.secret_line(f"    {value}")
    return value
