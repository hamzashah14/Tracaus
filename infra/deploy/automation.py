"""Customer deployment CLI: init, dry-run, check, apply and resumable status.

No AWS call in init/dry-run/status. Apply reuses the existing reviewed/sealed
release gates, never installs collectors or bypasses inbox qualification.
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from infra.deploy import deployment_preflight, durable
from infra.deploy.verify import PendingConfirmation, VerificationError
from infra.spec import ROOT, cwagent, digest, load, name, prefix
from infra.stacks import templates


class Waiting(RuntimeError):
    """Resumable dependency, in-progress operation or required human action."""

    def __init__(self, message, *, automatic=False):
        super().__init__(message)
        self.automatic = automatic


def confirmation_wait(recipients):
    """The human step when the only thing missing is a click on the subscription emails."""
    return Waiting(
        f"Confirm the subscription emails sent to the addresses configured as {' and '.join(recipients)}, "
        "then resume apply with the same plan hash"
    )


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Replace atomically so an interruption cannot truncate the resume journal.
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def private_dir(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / ".local"):
        raise VerificationError("Deployment files must live under ignored .local/")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def locked(directory):
    # File locking is released on crash; do not use stale PID lock files.
    if os.name != "posix":
        raise VerificationError("Deployment automation currently requires macOS/Linux; use WSL on Windows")
    import fcntl

    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise VerificationError("Another deployment process owns this work directory") from None
        yield
    finally:
        os.close(fd)


def model_api(spec):
    return spec.get("model_provider", "bedrock") == "model_api"


def stage_order(spec, config):
    stages = ["foundation-tools", "foundation-monitor", "durable-foundation"]
    if model_api(spec):
        stages += ["model-secret"]  # Pins the out-of-band key secret version before any runtime renders.
    stages += ["owned-tools"]
    if config["runtime_target"] == "agentcore":
        stages += ["agentcore-runtime", "agentcore-endpoint"]
    stages += ["durable-runtime"]
    if "observability" in spec:
        stages += ["observation-foundation", "observation-runtime", "health-bootstrap"]
    return (
        stages
        + ["candidate-verification", "staging-canary", "routing"]
        + (["observations"] if "observability" in spec else [])
        + ["registration-verification", "manual-acceptance"]
    )


def next_release_id(current):
    """The next release name: bump a trailing number (example001 to example002) or append -2."""
    match = re.fullmatch(r"(.*?)([0-9]+)", current)
    value = match[1] + str(int(match[2]) + 1).zfill(len(match[2])) if match else current + "-2"
    if len(value) > 16:
        raise VerificationError(
            "Choose the new release ID yourself with --release-id (at most 16 characters)"
        )
    return value


def refresh(source, directory, release_id=None):
    """Start the next release from an earlier one: the same settings, a new release ID, an empty journal.

    Copies only the three settings files. The old plan, journal, outputs and connection file stay behind,
    because a new release is planned, checked and applied from scratch."""
    source = Path(source).resolve()
    old = json.loads((source / "automation.json").read_text())
    if set(old) != {"version", "spec", "runtime_config", "profile", "wheelhouse"} or old["version"] != 1:
        raise VerificationError("Automation configuration has unknown or missing fields")
    if source == Path(directory).resolve():
        raise VerificationError("Use a new work directory for the new release")
    spec = json.loads((source / old["spec"]).read_text())
    runtime = json.loads((source / old["runtime_config"]).read_text())
    new_id = release_id or next_release_id(spec["release_id"])
    if new_id == spec["release_id"]:
        raise VerificationError("The new release needs a release ID that differs from the old one")
    names = ("deployment.json", "runtime.json", "automation.json")
    if any((directory / name).exists() for name in names):
        raise VerificationError("Refresh never overwrites existing customer files")
    try:
        private_json(directory / "deployment.json", {**spec, "release_id": new_id})
        private_json(directory / "runtime.json", runtime)
        private_json(
            directory / "automation.json",
            {
                **old,
                "spec": "deployment.json",
                "runtime_config": "runtime.json",
                "wheelhouse": str((source / old["wheelhouse"]).resolve()),
            },
        )
        load(directory / "deployment.json")
    except (ValueError, OSError):
        for name in names:
            (directory / name).unlink(missing_ok=True)
        raise VerificationError("The new release ID or the copied settings are not valid") from None
    return spec["release_id"], new_id


def plan(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if "initial_access" in value:
        raise VerificationError(durable.TEAM_SIGNIN_CHANGED)
    if set(value) != {"version", "spec", "runtime_config", "profile", "wheelhouse"} or value["version"] != 1:
        raise VerificationError("Automation configuration has unknown or missing fields")
    if value["profile"] is not None and (
        not isinstance(value["profile"], str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", value["profile"])
    ):
        raise VerificationError("Invalid AWS profile name")
    paths = {}
    for field in ("spec", "runtime_config", "wheelhouse"):
        if not isinstance(value[field], str) or not value[field]:
            raise VerificationError("Supply spec, runtime configuration and verified wheelhouse paths")
        paths[field] = (path.parent / value[field]).resolve()
    spec = load(paths["spec"])
    config = durable.load_config(paths["runtime_config"], spec)
    if model_api(spec) and config["runtime_target"] != "standalone":
        raise VerificationError("model_api is supported only by the standalone runtime")
    if not config["investigation_paused"]:
        raise VerificationError(
            "Initial deployment must keep investigation_paused true; activation is a separate qualified release"
        )
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = {
        "version": 1,
        "source_sha": source,
        "spec": spec,
        "runtime_config": config,
        "profile": value["profile"],
        "paths": {k: str(v) for k, v in paths.items()},
        "stages": stage_order(spec, config),
        "automatic_activation": False,
        "manual": [
            "EC2/application/CloudWatch Agent configuration and telemetry",
            "Local UI: export APP_PASSWORD (12+ characters) in your shell before launching it",
            *(
                [
                    f"Create secret {prefix(spec)}/model-api-key in {spec['bedrock_region']} out of band; automation never reads or writes its value"
                ]
                if model_api(spec)
                else []
            ),
            "Primary/fallback email confirmation and real inbox qualification",
            "Production promotion/activation after customer acceptance",
        ],
    }
    from infra.deploy import durable_ops
    from infra.stacks import durable_templates

    previews = {
        "foundation-tools": templates.foundation(spec, "tools"),
        "foundation-monitor": templates.foundation(spec, "monitor"),
        "durable-foundation": durable_templates.foundation(spec, config),
    }
    result["bootstrap_templates"] = previews
    result["resources"] = {
        stage: {
            "stack": name(spec, stage, stage in durable_ops.IMMUTABLE),
            "region": durable_ops.stage_region(spec, stage),
            "create_only": stage in durable_ops.IMMUTABLE,
            "resource_types": sorted({r["Type"] for r in previews[stage]["Resources"].values()})
            if stage in previews
            else {
                "owned-tools": [
                    "AWS::Lambda::Function",
                    "AWS::Lambda::Version",
                    "AWS::IAM::Role",
                    "AWS::Logs::LogGroup",
                ],
                "durable-runtime": [
                    "AWS::Lambda::Function",
                    "AWS::Lambda::Version",
                    "AWS::IAM::Role",
                    "AWS::Logs::LogGroup",
                ],
                "agentcore-runtime": ["AWS::BedrockAgentCore::Runtime", "AWS::IAM::Role"],
                "agentcore-endpoint": ["AWS::BedrockAgentCore::RuntimeEndpoint", "AWS::Logs::LogGroup"],
                "routing": [
                    "AWS::CloudWatch::Alarm",
                    "AWS::Events::Rule",
                    "AWS::Lambda::EventSourceMapping",
                    "AWS::SNS::Subscription",
                    "AWS::IAM::Role",
                    "AWS::Lambda::Permission",
                ],
                "observation-foundation": ["AWS::SQS::Queue", "AWS::SNS::Subscription"],
                "observation-runtime": [
                    "AWS::Lambda::Function",
                    "AWS::Lambda::Version",
                    "AWS::IAM::Role",
                    "AWS::Logs::LogGroup",
                ],
                "observations": [
                    "AWS::Events::Rule",
                    "AWS::CloudWatch::Dashboard",
                    "AWS::CloudWatch::Alarm",
                    "AWS::Lambda::Permission",
                ],
            }.get(stage, []),
        }
        for stage in result["stages"]
        if stage in durable_ops.STAGES
    }
    result["preview_limit"] = (
        "Bootstrap templates are exact; later runtime resources resolve collected AWS outputs and immutable artifact versions during apply. Actual change sets are inspected before execution. No invoice/free-tier estimate or production qualification."
    )
    result["permission_screen"] = deployment_preflight.permission_requests(spec, config)
    result["plan_hash"] = digest(result)
    return result


def factory(profile):
    session = boto3.Session(profile_name=profile)
    return lambda service, region: session.client(
        service,
        region_name=region,
        config=Config(
            connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 2, "mode": "standard"}
        ),
    )


class Driver:
    def __init__(self, planned, directory):
        self.plan, self.directory = planned, directory
        self.env = {**os.environ, "AWS_EC2_METADATA_DISABLED": "true", "PYTHON_DOTENV_DISABLED": "1"}
        if planned["profile"]:
            # A named profile must not silently lose to ambient static credentials.
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                self.env.pop(key, None)
            self.env["AWS_PROFILE"] = planned["profile"]
        self.clients = factory(planned["profile"])

    def command(self, module, arguments, output=None):
        command = [
            sys.executable,
            "-m",
            "infra.deploy.operator",
            "--profile",
            self.plan["profile"] or "",
            "--module",
            module,
            "--",
            *map(str, arguments),
        ]
        if output:
            command += ["--output", str(output)]
        # Do not stream raw cloud errors, request payloads or credentials to users.
        with (self.directory / "operations.log").open("ab") as log:
            os.chmod(log.name, 0o600)
            result = subprocess.run(
                command, cwd=ROOT, env=self.env, stdout=log, stderr=log, timeout=600, umask=0o077
            )
        if (
            result.returncode == PendingConfirmation.EXIT_CODE
            and module == "infra.deploy.durable_ops"
            and output
        ):
            raise confirmation_wait(json.loads(output.read_text())["recipients"])
        if result.returncode:
            raise VerificationError(
                f"{module} failed; inspect private operations.log and repair before resuming"
            )
        return json.loads(output.read_text()) if output else None

    def op(self, bundle, command, **kwargs):
        arguments = [command, "--bundle", self.directory / "bundle", "--review-hash", bundle["review_hash"]]
        for key, value in kwargs.items():
            if value is not None:
                arguments += ["--" + key.replace("_", "-")]
                if value != "":
                    arguments.append(value)
        return self.command("infra.deploy.durable_ops", arguments, self.directory / "operation.json")

    def render(self, bindings):
        private_json(self.directory / "bindings.json", bindings)
        p = self.plan["paths"]
        current_spec = load(p["spec"])
        if (
            current_spec != self.plan["spec"]
            or durable.load_config(p["runtime_config"], current_spec) != self.plan["runtime_config"]
        ):
            raise VerificationError(
                "Customer configuration changed during deployment; stop and review a new plan"
            )
        current_source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if current_source != self.plan["source_sha"]:
            raise VerificationError("Source revision changed during deployment")
        return durable.render(
            Path(p["spec"]),
            Path(p["runtime_config"]),
            self.directory / "bundle",
            self.directory / "build/pipeline",
            self.directory / "bindings.json",
            self.directory / "build/tools",
            self.directory / "build/host"
            if self.plan["runtime_config"]["runtime_target"] == "agentcore"
            else None,
            self.directory / "build/observation" if "observability" in self.plan["spec"] else None,
        )

    def build(self):
        p = self.plan["paths"]
        builds = self.directory / "build"
        # Build commands output a directory, not operation JSON.
        commands = [
            (
                "infra",
                ["build", "--spec", p["spec"], "--wheelhouse", p["wheelhouse"], "--output", builds / "tools"],
            ),
            ("infra", ["build-pipeline", "--wheelhouse", p["wheelhouse"], "--output", builds / "pipeline"]),
        ]
        if self.plan["runtime_config"]["runtime_target"] == "agentcore":
            commands.append(
                (
                    "scripts.build_lambdas",
                    [
                        "--function",
                        "incident_investigate",
                        "--architecture",
                        "arm64",
                        "--wheelhouse",
                        p["wheelhouse"],
                        "--output",
                        builds / "host",
                    ],
                )
            )
        if "observability" in self.plan["spec"]:
            commands.append(
                (
                    "infra",
                    [
                        "build-observations",
                        "--spec",
                        p["spec"],
                        "--wheelhouse",
                        p["wheelhouse"],
                        "--output",
                        builds / "observation",
                    ],
                )
            )
        for module, arguments in commands:
            self.command(module, arguments)

    def inspect_stack(self, bundle, stage):
        info = bundle["stages"][stage]
        client = self.clients("cloudformation", info["region"])
        try:
            response = client.describe_stacks(StackName=info["stack"])["Stacks"][0]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in exc.response[
                "Error"
            ].get("Message", ""):
                return None
            raise
        expected = dict((r["Key"], r["Value"]) for r in templates.tagged(bundle["spec"]))
        actual = dict((r["Key"], r["Value"]) for r in response.get("Tags", []))
        if any(actual.get(k) != v for k, v in expected.items()):
            raise VerificationError("Existing stack is not owned by this deployment")
        return response

    def assert_deployed_by_plan(self, bundle, current):
        """DescribeChangeSet never reports the role, so check the stack once its change has run."""
        if current.get("RoleARN") != bundle["spec"]["deployment_role_arn"]:
            raise VerificationError("Stack was not deployed by the planned CloudFormation execution role")

    def template_matches(self, bundle, stage):
        info = bundle["stages"][stage]
        body = self.clients("cloudformation", info["region"]).get_template(StackName=info["stack"])[
            "TemplateBody"
        ]
        return (
            templates.template_hash(json.loads(body) if isinstance(body, str) else body)
            == info["template_hash"]
        )

    def stage(self, bundle, stage, record, save, *, receipt=None, retirement=None):
        info = bundle["stages"][stage]
        client = self.clients("cloudformation", info["region"])
        if record.get("template_hash") != info["template_hash"]:
            if record:
                raise VerificationError("Checkpoint template changed; do not reuse a deployment journal")
            record.update(template_hash=info["template_hash"], status="INTENT")
            save()  # Durable intent before any request; recover lost responses by name.
        current = self.inspect_stack(bundle, stage)
        if (
            current
            and current["StackStatus"] != "REVIEW_IN_PROGRESS"
            and current["StackStatus"].endswith("_IN_PROGRESS")
        ):
            raise Waiting(
                f"{stage}: CloudFormation is still processing; rerun apply to resume", automatic=True
            )
        if (
            current
            and current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            and self.template_matches(bundle, stage)
        ):
            self.assert_deployed_by_plan(bundle, current)
            if info["create_only"]:
                self.op(bundle, "seal-runtime", stage=stage)
            record["status"] = "COMPLETE"
            save()
            return
        if current and current["StackStatus"] not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "REVIEW_IN_PROGRESS",
        }:
            raise VerificationError(
                f"{stage}: stack needs operator recovery; rollback/failure is not success"
            )
        if current and info["create_only"] and current["StackStatus"] != "REVIEW_IN_PROGRESS":
            raise VerificationError("Immutable release exists with a different template; use a new release")
        cs_name = "review-" + bundle["review_hash"][:24]
        try:
            cs = client.describe_change_set(StackName=info["stack"], ChangeSetName=cs_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"ChangeSetNotFound", "ChangeSetNotFoundException"}:
                # A missing stack can return ValidationError; verify exact absence.
                if not (
                    current is None
                    and exc.response["Error"]["Code"] == "ValidationError"
                    and "does not exist" in exc.response["Error"].get("Message", "")
                ):
                    raise
            # Existing REVIEW_IN_PROGRESS stack means CreateChangeSet was accepted
            # but its response was lost. It is safe only with our exact intent.
            if current and current["StackStatus"] == "REVIEW_IN_PROGRESS":
                raise VerificationError("Pending stack has no matching change set; inspect before retry")
            self.op(bundle, "change-set", stage=stage)
            raise Waiting(f"{stage}: change set requested; rerun apply to resume", automatic=True)
        if (
            cs["Status"] in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}
            or cs.get("ExecutionStatus") == "EXECUTE_IN_PROGRESS"
        ):
            raise Waiting(f"{stage}: change set is still processing", automatic=True)
        if cs["Status"] == "FAILED":
            # An unchanged stable stack is valid only if its actual template matches.
            if (
                current
                and current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
                and self.template_matches(bundle, stage)
            ):
                self.assert_deployed_by_plan(bundle, current)
                record["status"] = "COMPLETE"
                save()
                return
            raise VerificationError(f"{stage}: change set failed; inspect private AWS events")
        inspected = self.op(bundle, "inspect", stage=stage, change_set=cs_name)
        # Removing/replacing existing resources is never automatically approved.
        changes = inspected["changes"]
        if any(
            r.get("Type") != "Resource"
            or r.get("ResourceChange", {}).get("Action") not in {"Add", "Modify"}
            or r.get("ResourceChange", {}).get("Replacement", "False") != "False"
            for r in changes
        ):
            raise VerificationError(
                "Change set deletes/replaces resources; separate operator review required"
            )
        private_json(self.directory / f"review-{stage}.json", inspected)
        record.update(
            status="EXECUTION_INTENT",
            change_set=inspected["change_set"],
            change_hash=inspected["review_hash"],
        )
        save()
        options = dict(
            stage=stage, change_set=inspected["change_set"], change_set_hash=inspected["review_hash"]
        )
        if receipt:
            private_json(self.directory / "canary.json", receipt)
            options["receipt"] = self.directory / "canary.json"
        if retirement:
            private_json(self.directory / "retirement.json", retirement)
            options["retirement_plan"] = self.directory / "retirement.json"
        self.op(bundle, "execute", **options)
        raise Waiting(f"{stage}: execution requested; rerun apply to resume", automatic=True)


def deploy(planned, directory, driver, *, allow_model=False, retry_canary=False):
    journal = directory / "state.json"
    state = (
        json.loads(journal.read_text())
        if journal.exists()
        else {
            "version": 1,
            "plan_hash": planned["plan_hash"],
            "bindings": {},
            "stages": {},
            "status": "RUNNING",
        }
    )
    if state.get("version") != 1 or state.get("plan_hash") != planned["plan_hash"]:
        raise VerificationError("Configuration/source changed; create a new deployment work directory")

    def save():
        private_json(journal, state)

    def stage(stage_name, **kwargs):
        bundle = driver.render(state["bindings"])
        if stage_name not in bundle["stages"]:
            raise VerificationError("Required deployment bindings are missing")
        record = state["stages"].setdefault(stage_name, {})
        driver.stage(bundle, stage_name, record, save, **kwargs)
        return bundle

    def collect(stage_name, key):
        bundle = driver.render(state["bindings"])
        state["bindings"][key] = driver.op(bundle, "collect", stage=stage_name)
        save()

    if retry_canary:
        if not allow_model:
            raise VerificationError("Retry requires explicit paid invocation authorization")
        state.setdefault("canary_history", []).append(
            {
                "previous_receipt": state.pop("canary", None),
                "previous_request": state.pop("canary_requested", False),
                "reason": "Explicit operator --retry-canary",
            }
        )
        save()
    save()
    try:
        # Recheck permissions on every resume. Capacity accounts for the already verified
        # own reservation, never trusts journal-only values.
        initial = planned["runtime_config"]["initial_reserved_concurrency"]
        remaining = initial
        if state["stages"].get("durable-runtime"):
            bundle = driver.render(state["bindings"]) if state.get("built") else None
            current = driver.inspect_stack(bundle, "durable-runtime") if bundle else None
            if (
                current
                and current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
                and driver.template_matches(bundle, "durable-runtime")
            ):
                client = driver.clients("lambda", planned["spec"]["monitor_region"])
                output = driver.op(bundle, "collect", stage="durable-runtime")
                arn = output["InitialVersionArn"]
                if (
                    client.get_function_concurrency(FunctionName=arn.rsplit(":", 1)[0]).get(
                        "ReservedConcurrentExecutions"
                    )
                    == initial
                ):
                    remaining -= initial
        report = getattr(driver, "preflight", None)
        if report is None:
            report = deployment_preflight.check(planned, driver.clients, remaining_reserved=remaining)
            driver.preflight = report
        private_json(directory / "preflight.json", report)
        if report["status"] != "SIMULATED":
            raise VerificationError("Permission preflight blocked; inspect private preflight.json")
        if not state.get("built"):
            driver.build()
            state["built"] = True
            save()
        for name in ("foundation-tools", "foundation-monitor", "durable-foundation"):
            stage(name)
        collect("durable-foundation", "foundation")
        bundle = driver.render(state["bindings"])
        if "secret" not in state["bindings"]:
            state["bindings"]["secret"] = driver.op(bundle, "cursor-version")
            save()
        if model_api(planned["spec"]) and "model_secret" not in state["bindings"]:
            # Read-only: pins the out-of-band key secret's current version; the value is never read here.
            state["bindings"]["model_secret"] = driver.op(bundle, "model-secret-version")
            save()
        if "tool_artifacts" not in state["bindings"]:
            bundle = driver.render(state["bindings"])
            state["bindings"]["tool_artifacts"] = driver.op(
                bundle, "upload", build_dir=directory / "build/tools", artifact_kind="tools"
            )
            save()
        stage("owned-tools")
        collect("owned-tools", "tools")
        for kind, key in [
            ("host", "host_artifact"),
            ("observation", "observation_artifacts"),
            ("pipeline", "artifacts"),
        ]:
            if (
                kind == "host"
                and planned["runtime_config"]["runtime_target"] != "agentcore"
                or kind == "observation"
                and "observability" not in planned["spec"]
            ):
                continue
            if key not in state["bindings"]:
                bundle = driver.render(state["bindings"])
                state["bindings"][key] = driver.op(
                    bundle, "upload", build_dir=directory / "build" / kind, artifact_kind=kind
                )
                save()
        if planned["runtime_config"]["runtime_target"] == "agentcore":
            stage("agentcore-runtime")
            collect("agentcore-runtime", "agentcore_candidate")
            # RuntimeArn is unused candidate metadata; render expects exact ID/version.
            state["bindings"]["agentcore_candidate"].pop("RuntimeArn", None)
            save()
            stage("agentcore-endpoint")
            collect("agentcore-endpoint", "agentcore")
        stage("durable-runtime")
        collect("durable-runtime", "versions")
        if "observability" in planned["spec"]:
            stage("observation-foundation")
            stage("observation-runtime")
            collect("observation-runtime", "observation_versions")
            bundle = driver.render(state["bindings"])
            driver.op(bundle, "seed-health")
        bundle = driver.render(state["bindings"])
        driver.op(bundle, "verify-candidate")
        # This output has references only; credentials stay separate.
        connection = json.loads((directory / "bundle/routing.json").read_text())["Outputs"][
            "RuntimeConnection"
        ]["Value"]
        env = {
            **json.loads(connection),
            "MONITOR_REGION": planned["spec"]["monitor_region"],
            "INCIDENT_TABLE": state["bindings"]["foundation"]["TableName"],
            "REPORT_BUCKET": state["bindings"]["foundation"]["EvidenceBucket"],
        }
        if planned["spec"]["environment"] != "staging":
            raise Waiting(
                "Production candidates provisioned; existing gates require separately qualified staging and production cutover"
            )
        if not allow_model:
            raise Waiting("Candidate ready: resume with --allow-model-invocation")
        # Paid work is not blindly repeated on an ambiguous failure.
        receipt = state.get("canary")
        if receipt:
            from infra.deploy.release import require_receipt

            try:
                require_receipt(bundle, receipt)
            except VerificationError:
                raise Waiting(
                    "Canary receipt expired/differs; inspect it and explicitly use --retry-canary for new paid work"
                ) from None
        elif state.get("canary_requested"):
            raise Waiting(
                "Previous paid canary outcome is ambiguous; inspect private result before explicitly authorizing --retry-canary"
            )
        else:
            state["canary_requested"] = True
            save()
            receipt = driver.op(bundle, "canary", allow_model_invocation="")
            state["canary"] = receipt
            save()
        private_json(directory / "canary.json", receipt)
        retirement = driver.op(bundle, "retirement-plan", receipt=directory / "canary.json")
        if retirement.get("disable_alarms") or retirement.get("unsubscribe"):
            raise Waiting(
                "Legacy resource retirement requires separate review; automation never deletes subscriptions or alarms"
            )
        stage("routing", receipt=receipt, retirement=retirement)
        if "observability" in planned["spec"]:
            stage("observations")
        driver.op(bundle, "verify-routing")
        if "observability" in planned["spec"]:
            driver.op(bundle, "verify-observation-routing")
        role = driver.op(bundle, "collect", stage="routing")["UiRoleArn"]
        private_json(
            directory / "ui-connection.json",
            {"environment": env, "ui_role_arn": role},
        )
        state["status"] = "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
        state["next"] = (
            "Confirm notification subscriptions and actual delivery; complete customer live acceptance. Investigation remains paused."
        )
        save()
    except Waiting as exc:
        state["status"] = "WAITING"
        state["auto_resume"] = exc.automatic
        state["next"] = str(exc)
        save()
    except Exception:
        state["status"] = "FAILED"
        state["next"] = "Repair the recorded failed step, then resume the same reviewed configuration"
        save()
        raise
    return {
        "status": state["status"],
        "auto_resume": state.get("auto_resume", False) if state["status"] == "WAITING" else False,
        "next": state["next"],
        "completed_stages": [k for k, v in state["stages"].items() if v.get("status") == "COMPLETE"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "refresh", "dry-run", "check", "apply", "status"])
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--from-dir", type=Path, help="refresh: the work directory of the release to start from"
    )
    parser.add_argument("--release-id", help="refresh: the new release ID (default: the old one, counted up)")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--plan-hash")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=900,
        help="Automatically wait/resume AWS steps, up to 3600 seconds; 0 advances once",
    )
    parser.add_argument("--allow-model-invocation", action="store_true")
    parser.add_argument(
        "--retry-canary",
        action="store_true",
        help="Explicitly authorize replacing an expired or ambiguous paid canary after inspection",
    )
    args = parser.parse_args()
    try:
        directory = private_dir(args.work_dir)
        with locked(directory):
            if args.command == "init":
                import shutil

                targets = {
                    "deployment.json": ROOT / "examples/deployment.example.json",
                    "runtime.json": ROOT / "examples/durable.example.json",
                }
                if any((directory / p).exists() for p in [*targets, "automation.json"]):
                    raise VerificationError("Init never overwrites existing customer files")
                for name, source in targets.items():
                    shutil.copyfile(source, directory / name)
                    os.chmod(directory / name, 0o600)
                private_json(
                    directory / "automation.json",
                    {
                        "version": 1,
                        "spec": "deployment.json",
                        "runtime_config": "runtime.json",
                        "profile": None,
                        "wheelhouse": str(ROOT / ".build/wheels"),
                    },
                )
                print(
                    "Created private templates. Fill deployment.json/runtime.json/automation.json; see docs/DEPLOY.md. Reference inputs cannot deploy AWS."
                )
                return 0
            if args.command == "refresh":
                if not args.from_dir:
                    raise VerificationError("--from-dir is required")
                old_id, new_id = refresh(args.from_dir, directory, args.release_id)
                print(
                    f"Created release {new_id} (from {old_id}) in {directory}. Edit deployment.json there for what "
                    "changed, for example the instances, then run dry-run, check and apply with this work directory. "
                    "Nothing from the old run's plan, journal or outputs was copied."
                )
                return 0
            if args.command == "status":
                state = json.loads((directory / "state.json").read_text())
                print(json.dumps({k: state.get(k) for k in ("status", "next", "plan_hash")}, indent=2))
                return 0
            if not args.config:
                raise VerificationError("--config is required")
            planned = plan(args.config)
            if args.command == "dry-run":
                private_json(directory / "plan.json", planned)
                for instance in planned["spec"]["instances"]:
                    private_json(
                        directory / "collector-examples" / (instance["id"] + ".json"),
                        cwagent(planned["spec"], instance),
                    )
                print(
                    json.dumps(
                        {
                            "plan_hash": planned["plan_hash"],
                            "reference_only": planned["spec"]["reference_only"],
                            "stages": planned["stages"],
                            "runtime_target": planned["runtime_config"]["runtime_target"],
                            "resources": planned["resources"],
                            "preview_limit": planned["preview_limit"],
                            "automatic_activation": False,
                            "manual": planned["manual"],
                            "details": str(directory / "plan.json"),
                        },
                        indent=2,
                    )
                )
                return 0
            if planned["spec"]["reference_only"]:
                raise VerificationError("Synthetic reference inputs cannot check/deploy AWS")
            if args.command == "check":
                report = deployment_preflight.check(planned, factory(planned["profile"]))
                private_json(directory / "preflight.json", report)
                print(
                    json.dumps(
                        {
                            "status": report["status"],
                            "permission_blockers": len(report["blockers"]),
                            "details": str(directory / "preflight.json"),
                        },
                        indent=2,
                    )
                )
                return 0 if not report["blockers"] else 1
            if args.plan_hash != planned["plan_hash"]:
                raise VerificationError("Run dry-run and supply its exact --plan-hash before apply")
            if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
                raise VerificationError("Apply requires a clean reviewed source checkout")
            if args.retry_canary and not args.allow_model_invocation:
                raise VerificationError("--retry-canary needs --allow-model-invocation")
            if not 0 <= args.wait_seconds <= 3600:
                raise VerificationError("--wait-seconds must be between 0 and 3600")
            driver = Driver(planned, directory)
            deadline = time.monotonic() + args.wait_seconds
            result = deploy(
                planned,
                directory,
                driver,
                allow_model=args.allow_model_invocation,
                retry_canary=args.retry_canary,
            )
            while result["status"] == "WAITING" and result["auto_resume"] and time.monotonic() < deadline:
                print(json.dumps({"status": "WAITING", "next": result["next"]}), flush=True)
                time.sleep(min(5, max(0, deadline - time.monotonic())))
                result = deploy(
                    planned,
                    directory,
                    driver,
                    allow_model=args.allow_model_invocation,
                )
            print(json.dumps(result, indent=2))
            return 2 if result["status"] == "WAITING" else 0
    except (
        VerificationError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        BotoCoreError,
        ClientError,
        subprocess.SubprocessError,
    ) as exc:
        if isinstance(exc, ClientError):
            private_json(
                directory / "error.json",
                {
                    "status": "FAILED",
                    "operation": exc.operation_name,
                    "error_code": exc.response.get("Error", {}).get("Code", "UNKNOWN"),
                    "next": "Repair the named API permission/prerequisite; never treat AccessDenied as absence",
                },
            )
        # Known local verification messages contain no raw cloud payloads.
        print(
            str(exc)
            if isinstance(exc, VerificationError)
            else f"Deployment failed ({type(exc).__name__}); no success claimed. Inspect private evidence.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
