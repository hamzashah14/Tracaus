"""`setup`: read the account, ask the rest, write the settings, and show the plan.

Nothing here changes a server or a task. AWS is written to once, for the role stack, and only after an explicit
yes. Every step announces what it is about to do and what it found."""

import dataclasses
import json
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import botocore.session

from infra.deploy.automation import factory, private_json
from infra.guided import discover
from infra.guided import files as guided_files
from infra.guided import roles as guided_roles
from infra.guided.common import ROUTING_STAGE, STAGES, GuidedError, aws_env, parse_json_object
from infra.guided.common import run as run_process
from infra.guided.console import clean
from infra.guided.prompt import (
    valid_email,
    valid_profile,
    valid_project,
    valid_region,
    valid_release,
    valid_url,
)
from infra.spec import ROOT

TOTAL_STEPS = 9
DISK = re.compile(r"/[A-Za-z0-9/_-]*")
EXE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
LOG_FILE = re.compile(r"/[A-Za-z0-9_./*?-]{1,200}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
LB_ARN = re.compile(
    r"arn:aws:elasticloadbalancing:[a-z0-9-]+:\d{12}:loadbalancer/app/[A-Za-z0-9-]{1,32}/[0-9a-f]{16}"
)
TG_ARN = re.compile(
    r"arn:aws:elasticloadbalancing:[a-z0-9-]+:\d{12}:targetgroup/[A-Za-z0-9-]{1,32}/[0-9a-f]{16}"
)
MODEL_ARN = re.compile(
    r"arn:aws:bedrock:[a-z0-9-]+:(?:\d{12})?:(?:foundation-model|inference-profile|application-inference-profile)/[A-Za-z0-9.:-]+"
)


@dataclasses.dataclass
class Options:
    skip_install: bool = False
    admin_profile: str | None = None
    yes: bool = False


def default_profiles():
    return sorted(botocore.session.Session().available_profiles)


def default_region(profile):
    return botocore.session.Session(profile=profile).get_config_variable("region")


def default_identity(profile):
    return lambda: factory(profile)("sts", None).get_caller_identity()  # same timeouts as every other call


def _matching(pattern, message):
    def check(value):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(message)
        return value

    return check


valid_disk = _matching(DISK, "Use an absolute path such as / or /data (letters, digits, / _ - only).")
valid_log_file = _matching(LOG_FILE, "Use an absolute file path or pattern such as /var/log/app/*.log.")
valid_model_id = _matching(MODEL_ID, "Use a model ID such as the one shown in the Bedrock console.")


def valid_emails(value):
    items = [part.strip() for part in value.split(",")] if isinstance(value, str) else list(value)
    if not 1 <= len(items) <= 5 or len(set(items)) != len(items):
        raise ValueError("Enter 1 to 5 different email addresses, separated by commas.")
    return [valid_email(item) for item in items]


def valid_arns(value):
    items = [part.strip() for part in value.split(",")] if isinstance(value, str) else list(value)
    if not 1 <= len(items) <= 10 or any(not MODEL_ARN.fullmatch(item) for item in items):
        raise ValueError("Enter 1 to 10 Bedrock model or inference-profile ARNs, separated by commas.")
    return items


def valid_https_base(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"https://(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?:/[A-Za-z0-9._~/-]*)?",
        value,
    ):
        raise ValueError("Use an https:// address with a domain name, without credentials or a query.")
    return value


def tail(text, lines=6):
    return " | ".join(clean(line, 160) for line in text.strip().splitlines()[-lines:])


class Setup:
    """One run of `setup`. The collaborators are injected so tests never touch AWS, the disk outside a temp
    folder, or the real `~/.aws/config`."""

    def __init__(
        self,
        console,
        prompter,
        work_dir,
        options=None,
        *,
        root=ROOT,
        clients_for=factory,
        identity_for=default_identity,
        profiles_for=default_profiles,
        region_for=default_region,
        runner=run_process,
        config_path=None,
        sleep=time.sleep,
    ):
        self.console, self.prompter, self.options = console, prompter, options or Options()
        self.root, self.clients_for, self.identity_for = Path(root), clients_for, identity_for
        self.profiles_for, self.region_for, self.runner, self.sleep = profiles_for, region_for, runner, sleep
        self.work_dir = Path(work_dir)
        self.config_path = config_path
        self.facts = {"agent": {}, "groups": {}}
        self.claimed_groups = set()  # ECS log groups already given to a selected service
        self.ec2, self.ecs, self.profile, self.region, self.account, self.caller = (
            [],
            [],
            None,
            None,
            None,
            {},
        )

    # ------------------------------------------------------------------ the run

    def run(self):
        self.console.begin(TOTAL_STEPS)
        try:
            self.step_computer()
            self.step_identity()
            self.step_discover()
            self.step_targets()
            self.step_settings()
            self.step_roles()
            self.step_profiles()
            self.step_plan()
            self.step_report()
        finally:
            if self.prompter.record:  # keep every answer given so far, even when a later step stops the run
                self._save_answers()
        return 0

    # ------------------------------------------------------------------ 1 computer

    def step_computer(self):
        with self.console.step("Checking this computer"):
            if sys.version_info[:2] != (3, 12):
                raise GuidedError(
                    "Python 3.12 is required",
                    f"this is Python {sys.version_info[0]}.{sys.version_info[1]}",
                    "Nothing was changed.",
                    "Run ./bin/tracaus, which picks Python 3.12 for you.",
                )
            self.console.ok("Python 3.12")
            code, _ = self.runner(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.root, timeout=30)
            if code != 0:
                raise GuidedError(
                    "This folder is not a Git checkout",
                    "the deployment records the source commit",
                    "Nothing was changed.",
                    "Clone the repository with git and run setup inside it.",
                )
            _, status = self.runner(["git", "status", "--porcelain"], cwd=self.root, timeout=30)
            changed = [line for line in status.splitlines() if line.strip()]
            if changed:
                self.console.warn(
                    f"{len(changed)} file(s) differ from the last commit (first: {clean(changed[0][3:], 80)}). "
                    "Setup can continue, but 'deploy' needs a clean checkout: commit or stash them first."
                )
            else:
                self.console.ok("The checkout is clean")
            wheels = self.root / ".build" / "wheels"
            if self.options.skip_install or (wheels / ".complete").exists():
                self.console.ok("The Lambda packages are already downloaded")
                return
            self.console.say(
                "Downloading the verified Lambda packages (needs the network, about a minute)..."
            )
            code, output = self.runner(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheels),
                    "-r",
                    str(self.root / "requirements" / "lambda.lock"),
                ],
                cwd=self.root,
                timeout=1200,
            )
            if code != 0:
                raise GuidedError(
                    "Could not download the Lambda packages",
                    tail(output),
                    "Nothing in AWS was touched.",
                    "Check the network or proxy, then run setup again (it continues from here).",
                )
            (wheels / ".complete").write_text(
                "ok\n"
            )  # an interrupted download is not mistaken for a finished one
            self.console.ok("Downloaded the Lambda packages")

    # ------------------------------------------------------------------ 2 identity

    def step_identity(self):
        with self.console.step("Choosing the AWS account to read"):
            profiles = self.profiles_for()
            if not profiles:
                raise GuidedError(
                    "No AWS profile was found",
                    "~/.aws/config and ~/.aws/credentials list none",
                    "Nothing was changed.",
                    "Create one with 'aws configure sso' (or 'aws configure'), then run setup again.",
                )
            default = "default" if "default" in profiles else None
            self.profile = self.prompter.choose(
                "profile",
                "Which AWS profile should Tracaus use to read your account? (read-only)",
                [(name, name) for name in profiles],
                default=default,
            )
            valid_profile(self.profile)
            self.region = self.region_for(self.profile)
            self.console.say(f"Asking AWS who {self.profile} is (one read-only call)...")
            self.caller = discover.caller(self.clients_for(self.profile), self.region or "us-east-1")
            self.console.ok(f"Signed in as a {self.caller['kind']} in account {self.caller['account']}")
            answer = self.prompter.ask(
                "account_id",
                f"Account ID to monitor (press Enter to confirm {self.caller['account']})",
                default=self.caller["account"],
                validate=_matching(re.compile(r"\d{12}"), "An account ID has 12 digits."),
            )
            if answer != self.caller["account"]:
                raise GuidedError(
                    "The profile belongs to a different account than the one you named",
                    f"the profile is in {self.caller['account']}",
                    "Nothing was changed.",
                    "Pick the profile for the account you want to monitor, or correct the account ID.",
                    code=3,
                )
            self.account = answer
            self.region = self.prompter.ask(
                "region",
                "Region to monitor (where your servers and services run)",
                default=self.region,
                validate=valid_region,
            )

    # ------------------------------------------------------------------ 3 discover

    def step_discover(self):
        with self.console.step(f"Reading what runs in {self.region} (read-only)"):
            clients = self.clients_for(self.profile)
            ec2 = discover.ec2_instances(clients, self.region)
            ecs = discover.ecs_services(clients, self.region)
            for name, result in (("EC2 instances", ec2), ("ECS services", ecs)):
                if result["state"] == "unknown":
                    self.console.warn(f"Could not read {name} ({result['reason']}); they are left out.")
                else:
                    extra = " (list cut at the safety limit)" if result.get("capped") else ""
                    self.console.ok(f"{len(result['items'])} {name} found{extra}")
            if ec2["state"] == "unknown" and ecs["state"] == "unknown":
                raise GuidedError(
                    "Could not read EC2 or ECS",
                    f"AWS said {ec2['reason']} and {ecs['reason']}",
                    "Nothing was changed.",
                    "Give the profile ec2:DescribeInstances and the ecs:List/Describe actions (see docs/PREREQUISITES.md), "
                    "or pick another profile.",
                )
            for item in ec2["items"]:
                if not item["supported"]:
                    self.console.warn(
                        f"{clean(item['name'] or item['id'])} is a Windows instance and is not supported."
                    )
            self.ec2 = [i for i in ec2["items"] if i["supported"]]
            self.ecs = ecs["items"]
            if not self.ec2 and not self.ecs:
                raise GuidedError(
                    f"No EC2 instance or ECS service was found in {self.region}",
                    "there is nothing to watch yet",
                    "Nothing was written.",
                    "Check the region, start your servers or services, and run setup again.",
                )

    # ------------------------------------------------------------------ 4 targets

    def step_targets(self):
        with self.console.step("Choosing what to watch"):
            options = []
            for item in self.ec2:
                state = "" if item["state"] == "running" else f"  [{item['state']}: no metrics while stopped]"
                options.append(
                    (f"ec2:{item['id']}", f"EC2  {clean(item['name'] or '-', 40)}  {item['id']}{state}")
                )
            for item in self.ecs:
                balancer = {
                    "alb": "ALB",
                    "none": "no load balancer",
                    "other": "NLB/other",
                    "unknown": "load balancer ?",
                }
                options.append(
                    (
                        f"ecs:{item['cluster']}/{item['service']}",
                        f"ECS  {clean(item['cluster'], 40)}/{clean(item['service'], 40)}  "
                        f"{clean(item['launch_type'])}  {balancer[item['load_balancer']]}",
                    )
                )
            picked = self.prompter.pick_many(
                "targets",
                "Which of these should Tracaus watch? (1 to 10)",
                options,
                minimum=1,
                maximum=guided_files.MAX_TARGETS,
            )
            self.picked = picked
            self.console.ok(f"{len(picked)} selected")
            self.choices_ec2, self.choices_ecs = [], []
            ec2_ids = [p[4:] for p in picked if p.startswith("ec2:")]
            if ec2_ids:
                self._read_instances(ec2_ids)
            taken = set()
            for value in picked:
                if value.startswith("ec2:"):
                    self.choices_ec2.append(self._ask_instance(value[4:]))
                else:
                    item = next(i for i in self.ecs if f"ecs:{i['cluster']}/{i['service']}" == value)
                    entry = self._ask_service(item, taken)
                    taken.add(entry["id"])
                    self.choices_ecs.append(entry)
            names = [g for e in self.choices_ecs for g in e["log_groups"]]
            if names:
                self.facts["groups"].update(
                    discover.log_groups_exist(self.clients_for(self.profile), self.region, names)
                )

    def _read_instances(self, ids):
        clients = self.clients_for(self.profile)
        self.console.say("Reading each instance's CloudWatch agent metrics and existing log groups...")
        for instance in ids:
            facts = discover.cwagent_facts(clients, self.region, instance)
            self.facts["agent"][instance] = facts
            note = {"found": "agent metrics found", "missing": "no agent metrics in the last 3 hours"}.get(
                facts["state"], f"could not read metrics ({facts.get('reason')})"
            )
            self.console.info(f"{instance}: {note}")
        found = discover.log_group_candidates(clients, self.region, ids)
        self.facts["log_candidates"] = found
        if found["state"] == "unknown":
            self.console.warn(
                f"Could not scan log groups ({found['reason']}); you can type group names instead."
            )
        elif found["capped"]:
            self.console.warn(
                "The log group scan stopped at its safety limit; some groups may be missing from the list."
            )

    def _ask_instance(self, instance):
        info = next(i for i in self.ec2 if i["id"] == instance)
        label = clean(info["name"] or instance, 60)
        agent = self.facts["agent"].get(instance, {})
        paths = [p for p in agent.get("disk_paths", []) if DISK.fullmatch(p)]
        exes = [e for e in agent.get("exes", []) if EXE.fullmatch(e)]
        if paths:
            disk = self.prompter.choose(
                f"disk_path:{instance}",
                f"Which disk should be watched on {label}?",
                [(p, p) for p in paths],
                default="/" if "/" in paths else paths[0],
            )
        else:
            disk = self.prompter.ask(
                f"disk_path:{instance}", f"Disk path to watch on {label}", default="/", validate=valid_disk
            )
        exe = None
        if exes:
            choice = self.prompter.choose(
                f"process_exe:{instance}",
                f"Which process should be counted on {label}?",
                [("", "none")] + [(e, e) for e in exes],
                default="nginx" if "nginx" in exes else "",
            )
            exe = choice or None
        nginx = self.prompter.confirm(
            f"nginx:{instance}",
            f"Is Nginx in front of the application on {label}? (adds Nginx 5xx and error alarms)",
            default="nginx" in exes,
        )
        candidates = self.facts.get("log_candidates", {}).get("groups", {}).get(instance, [])[:8]
        existing = []
        if candidates:
            existing = self.prompter.pick_many(
                f"existing_logs:{instance}",
                f"Existing log groups Tracaus may read for {label} (it never changes them)",
                [(g, clean(g, 90)) for g in candidates],
                minimum=0,
                maximum=8,
            )
        entry = {
            "id": instance,
            "label": label,
            "disk_path": disk,
            "process_exe": exe,
            "nginx": nginx,
            "existing_log_groups": existing,
            "log_groups": [],
            "log_files": [],
        }
        for name in existing:
            self.facts["groups"][name] = True
        if not existing:
            entry["log_groups"] = ["application"]
            path = self.prompter.ask(
                f"logfile:{instance}",
                f"Path of the application log file on {label} (Tracaus creates the log group 'application' for it)",
                default="/var/log/app/*.log",
                validate=valid_log_file,
            )
            entry["log_files"] = [{"group": "application", "file": path}]
        return entry

    def _ask_service(self, item, taken):
        key = f"{item['cluster']}/{item['service']}"
        label = clean(key, 80)
        for note in item["notes"]:
            self.console.warn(f"{label}: {clean(note, 160)}")
        for driver in item["other_log_drivers"]:
            self.console.warn(f"{label}: containers using the {clean(driver, 30)} log driver cannot be read.")
        detected = item["load_balancer"] == "alb"
        in_front = self.prompter.confirm(
            f"alb:{key}",
            f"Is an Application Load Balancer in front of {label}? "
            f"(detected: {'yes' if detected else 'no' if item['load_balancer'] == 'none' else 'not sure'})",
            default=detected,
        )
        balancer = None
        if in_front and detected:
            balancer = dict(item["alb"])
        elif in_front:
            if item["load_balancer"] == "other":
                self.console.warn(
                    f"{label}: only Application Load Balancers are supported, so ALB alarms stay off."
                )
            else:
                balancer = {
                    "load_balancer_arn": self.prompter.ask(
                        f"alb_arn:{key}",
                        "Load balancer ARN",
                        validate=_matching(LB_ARN, "Use an ALB ARN (loadbalancer/app/...)."),
                    ),
                    "target_group_arn": self.prompter.ask(
                        f"tg_arn:{key}",
                        "Target group ARN",
                        validate=_matching(TG_ARN, "Use a target group ARN."),
                    ),
                }
        groups = []
        offered = [g for g in item["log_groups"][:8] if g not in self.claimed_groups]
        for shared in item["log_groups"][:8]:
            if shared in self.claimed_groups:
                self.console.warn(
                    f"{label}: log group {clean(shared, 90)} is already used by another selected service. "
                    "A log group is read whole, so it can belong to one service only; it is left out here."
                )
        if offered:
            groups = self.prompter.pick_many(
                f"ecs_logs:{key}",
                f"Log groups to read for {label} (read whole)",
                [(g, clean(g, 90)) for g in offered],
                minimum=0,
                maximum=8,
                default=tuple(offered),
            )
            self.claimed_groups.update(groups)
        return {
            "id": guided_files.ecs_id(item["service"], taken),
            "label": label,
            "cluster": item["cluster"],
            "service": item["service"],
            "load_balancer": balancer,
            "log_groups": groups,
            "container_insights": item["container_insights"],
        }

    # ------------------------------------------------------------------ 5 settings

    def step_settings(self):
        with self.console.step("Collecting the remaining settings and writing the files"):
            ask = self.prompter.ask
            project = ask(
                "project", "Project name (used in resource names)", default="tracaus", validate=valid_project
            )
            environment = self.prompter.choose(
                "environment",
                "Environment (only 'staging' finishes automatically; see docs/DEPLOY.md)",
                [("staging", "staging"), ("development", "development"), ("production", "production")],
                default="staging",
            )
            release = ask(
                "release_id",
                "Release ID (a new one for every change)",
                default="r001",
                validate=valid_release,
            )
            emails = ask("emails", "Alert email addresses, comma-separated (1 to 5)", validate=valid_emails)
            self.console.say(
                "Alert links use a fixed https address. If you only run the UI on this computer, keep the default "
                "and open the same ?incident=ID on http://127.0.0.1:8501 (docs/DEPLOY.md, section 4)."
            )
            status = ask(
                "status_base_url",
                "Address for links in alert emails",
                default="https://localhost:8501/",
                validate=valid_url,
            )
            model = self._ask_model()
            roles = guided_roles.role_arns(project, environment, self.account)
            self.operator_profile = f"{project}-{environment}-operator"
            self.choices = {
                "project": project,
                "environment": environment,
                "account_id": self.account,
                "region": self.region,
                "bedrock_region": model.pop("bedrock_region"),
                "release_id": release,
                "emails": emails,
                "model": model,
                "roles": roles,
                "profile": self.operator_profile,
                "status_base_url": status,
                "ec2": self.choices_ec2,
                "ecs": self.choices_ecs,
            }
            try:
                guided_files.write(self.work_dir, guided_files.build(self.choices))
                self.spec = guided_files.validate(self.work_dir)
            except guided_files.FilesError as error:
                raise GuidedError(
                    "The settings could not be written",
                    str(error),
                    "No file was replaced.",
                    "Fix the answer named above and run setup again.",
                ) from None
            self._save_answers()
            private_json(self.work_dir / "discovery.json", {"region": self.region, "facts": self.facts})
            self.console.ok(
                f"Wrote deployment.json, runtime.json and automation.json in {self.work_dir.name}/"
            )
            if model["provider"] == "model_api":
                from infra.spec import prefix

                self.console.warn(
                    f"Create the model API key secret {prefix(self.spec)}/model-api-key in {self.spec['bedrock_region']} "
                    "yourself before 'deploy'. Tracaus never reads or writes the key value."
                )

    def _ask_model(self):
        provider = self.prompter.choose(
            "model_provider",
            "Which model service should write the reports?",
            [
                ("bedrock", "Amazon Bedrock (default; stays in your account)"),
                ("model_api", "A model API (OpenAI-compatible or Anthropic; prompts leave AWS)"),
            ],
            default="bedrock",
        )
        if provider == "model_api":
            return {
                "provider": "model_api",
                "bedrock_region": self.region,
                "protocol": self.prompter.choose(
                    "model_protocol",
                    "Which protocol does it speak?",
                    [("openai", "OpenAI-compatible"), ("anthropic", "Anthropic Messages")],
                    default="openai",
                ),
                "base_url": self.prompter.ask(
                    "model_base_url", "Base URL of the model API", validate=valid_https_base
                ),
                "model_id": self.prompter.ask("model_id", "Model name", validate=valid_model_id),
            }
        bedrock_region = self.prompter.ask(
            "bedrock_region", "Bedrock region", default=self.region, validate=valid_region
        )
        self.console.say(
            "The model must support tool use and token counting. 'deploy' checks that you can reach it."
        )
        found = discover.bedrock_profiles(self.clients_for(self.profile), bedrock_region)
        if found["state"] == "unknown":
            self.console.warn(
                f"Could not list Bedrock profiles ({found['reason']}); enter the model yourself."
            )
        options = [
            (p["id"], f"{clean(p['name'], 50)}  ({clean(p['id'], 60)})") for p in found.get("items", [])[:15]
        ]
        options.append(("", "Another model ID, typed by hand"))
        choice = self.prompter.choose(
            "model_choice", "Which model?", options, default=options[0][0] if len(options) > 1 else ""
        )
        if choice:
            arns = next(p["arns"] for p in found["items"] if p["id"] == choice)
            return {
                "provider": "bedrock",
                "bedrock_region": bedrock_region,
                "model_id": choice,
                "model_arns": arns,
            }
        model_id = self.prompter.ask("model_id", "Model ID or inference profile ID", validate=valid_model_id)
        resolved = discover.resolve_model(self.clients_for(self.profile), bedrock_region, model_id)
        if resolved["state"] == "missing":
            raise GuidedError(
                "Bedrock does not know that model ID",
                f"nothing named that exists in {bedrock_region}",
                "Nothing was written to AWS.",
                "Check the ID and that model access is enabled, then run setup again.",
            )
        arns = resolved["arns"] or self.prompter.ask(
            "model_arns", "Model ARNs (could not be read), comma-separated", validate=valid_arns
        )
        return {
            "provider": "bedrock",
            "bedrock_region": bedrock_region,
            "model_id": model_id,
            "model_arns": arns,
        }

    def _save_answers(self):
        """Merge this run's answers over the saved ones. A deployment that already started keeps its answers."""
        if (self.work_dir / "state.json").exists():
            return
        try:
            previous = json.loads((self.work_dir / "answers.json").read_text())
        except (OSError, ValueError):
            previous = {}
        record = {k: v for k, v in self.prompter.record.items() if k != "admin_profile"}
        private_json(self.work_dir / "answers.json", {**previous, **record})

    # ------------------------------------------------------------------ 6 roles

    def step_roles(self):
        with self.console.step("Checking the three IAM roles"):
            arns = guided_roles.role_arns(self.choices["project"], self.choices["environment"], self.account)
            states = guided_roles.plan_roles(self.clients_for(self.profile), arns)
            for name, state in states.items():
                self.console.info(f"{name} role: {state}")
            if "conflict" in states.values():
                raise GuidedError(
                    "A role with one of these names exists under a different path",
                    "the deployment needs the exact ARNs it was given",
                    "Nothing was changed.",
                    "Pick another project or environment name, or remove the other role.",
                )
            if set(states.values()) == {"found"}:
                self.console.ok("All three roles exist")
                return
            if "unknown" in states.values():
                if self.prompter.confirm(
                    "roles_exist",
                    "IAM could not be fully read. Do the three roles already exist?",
                    default=False,
                ):
                    return
            if "found" in states.values() and self._stack_present() is False:
                raise GuidedError(
                    "Only some of the three roles exist",
                    "the role template creates all three, so it would fail on the ones that exist",
                    "Nothing was changed.",
                    "Create the missing roles yourself, or delete the partial set, then run setup again.",
                )
            self._create_roles(arns)

    def _stack_present(self):
        """True or False when the stack can be looked up, None when this read-only profile cannot tell."""
        try:
            cfn = self.clients_for(self.profile)("cloudformation", self.region)
            return guided_roles._describe(cfn, guided_roles.stack_name(self.spec)) is not None
        except Exception:  # noqa: BLE001 - any failure to look means we cannot tell
            return None

    def _create_roles(self, arns):
        trust, warning = guided_roles.trust_for(self.caller)
        template = guided_roles.render(self.spec, trust)
        template_path = self.work_dir / "iam.template.json"
        private_json(template_path, template)
        stack = guided_roles.stack_name(self.spec)
        self.console.say(
            f"\nThree roles are missing. This is the only step that needs administrator-level IAM rights, and only once:\n"
            f"  - {arns['operator'].split(':role/')[1]}   runs the deployment (you assume it)\n"
            f"  - {arns['execution'].split(':role/')[1]}   lets CloudFormation create Tracaus's own resources\n"
            f"  - {arns['ui'].split(':role/')[1]}   lets the UI reach the tools\n"
            f"They will trust: {', '.join(trust)}\n"
            f"Template: {template_path.name} (CloudFormation stack {stack})"
        )
        if warning:
            self.console.warn(warning)
        admin = self.options.admin_profile or self.prompter.choose(
            "admin_profile",
            "Which profile may create IAM roles? It is used for this one step only.",
            [(name, name) for name in self.profiles_for()],
            default=self.profile,
        )
        self.prompter.record.pop("admin_profile", None)
        valid_profile(admin)
        consent = self.prompter.confirm(
            "create_roles",
            f"Create these three IAM roles in account {self.account} with profile '{admin}'?",
            consent="iam-bootstrap",
        )
        if consent:
            who = self.identity_for(admin)()
            if who["Account"] != self.account:
                raise GuidedError(
                    "That profile belongs to a different account",
                    f"it is in {who['Account']}, not {self.account}",
                    "Nothing was changed.",
                    "Pick the profile for the account you named.",
                    code=3,
                )
            try:
                guided_roles.deploy_roles(
                    self.clients_for(admin),
                    self.spec,
                    template,
                    self.console,
                    self.prompter,
                    sleep=self.sleep,
                )
            except guided_roles.RolesError as error:
                raise GuidedError(
                    "The IAM role stack failed", str(error), "", "Run setup again after fixing the cause."
                ) from None
            self._wait_for_roles(arns)
            return
        self._hand_over(template_path, stack, arns)

    def _hand_over(self, template_path, stack, arns):
        self.console.say(
            "Nothing was created. Ask an administrator to run this once:\n"
            f"  aws cloudformation deploy --template-file {shlex.quote(str(template_path))} --stack-name {stack} "
            f"--capabilities CAPABILITY_NAMED_IAM --region {self.region} --profile ADMIN_PROFILE"
        )
        for _ in range(20):
            if not self.prompter.confirm(
                "roles_check", "Has an administrator created them? Check now?", default=False
            ):
                break
            states = guided_roles.plan_roles(self.clients_for(self.profile), arns)
            if set(states.values()) == {"found"}:
                self.console.ok("All three roles exist now")
                return
            if "unknown" in states.values():
                self.console.warn("This profile cannot read IAM, so Tracaus cannot see the roles itself.")
                if self.prompter.confirm(
                    "roles_confirmed", "Did the administrator confirm all three roles exist?", default=False
                ):
                    return
                continue
            self.console.warn("They are not all there yet.")
        raise GuidedError(
            "Waiting for the IAM roles",
            "an administrator has not created them yet",
            "Your settings are saved; nothing in AWS was changed by Tracaus.",
            "Run setup again once the roles exist.",
            code=2,
        )

    def _wait_for_roles(self, arns):
        for _ in range(12):
            states = guided_roles.plan_roles(self.clients_for(self.profile), arns)
            if set(states.values()) == {"found"}:
                self.console.ok("All three roles exist")
                return
            if "unknown" in states.values():
                self.console.warn(
                    "This profile cannot read IAM; 'deploy' verifies the roles itself. Continuing."
                )
                return
            self.sleep(5)
        self.console.warn("The roles were created but are not visible yet; continuing.")

    # ------------------------------------------------------------------ 7 profiles

    def step_profiles(self):
        with self.console.step("Setting up the operator AWS profile"):
            roles = self.choices["roles"]
            block = guided_roles.profile_block(
                self.operator_profile, roles["operator"], self.profile, self.region
            )
            path = Path(self.config_path) if self.config_path else guided_roles.config_path()
            self.console.say(
                f"Tracaus needs an AWS profile that assumes the operator role. It will be added to {path} between "
                "marker comments, after a backup. No keys are written."
            )
            if not self.prompter.confirm(
                "write_profiles", f"Add the profile '{self.operator_profile}' now?", default=True
            ):
                self.console.say("Add this yourself, then 'deploy' will work:\n" + block)
                self.profile_ready = False
                return
            try:
                result = guided_roles.write_profiles(path, [block])
            except guided_roles.RolesError as error:
                raise GuidedError(
                    "The AWS config was not changed",
                    str(error),
                    "The original file is intact.",
                    "Add the profile by hand:\n" + block,
                ) from None
            for name in result["skipped"]:
                self.console.warn(
                    f"A profile named {name} already exists outside Tracaus's markers; it was not changed."
                )
            if result["backup"]:
                self.console.info(f"Backup of the config: {Path(result['backup']).name}")
            self.console.ok("Profile ready")
            self.console.say("Checking that the operator role can be assumed (IAM can take a minute)...")
            try:
                guided_roles.verify_operator(
                    self.identity_for(self.operator_profile),
                    roles["operator"],
                    self.console,
                    sleep=self.sleep,
                )
            except guided_roles.RolesError as error:
                self._show_trust(roles["operator"])
                raise GuidedError(
                    "The operator profile does not work yet",
                    str(error),
                    "Your settings are saved.",
                    "Fix it, then run setup again.",
                ) from None
            self.profile_ready = True
            self.console.ok("The operator role can be assumed")

    def _show_trust(self, role_arn):
        """Say who the operator role trusts, when this profile may read it, so the user can see what is missing."""
        try:
            iam = self.clients_for(self.profile)("iam", self.region)
            document = iam.get_role(RoleName=role_arn.rsplit("/", 1)[-1])["Role"]["AssumeRolePolicyDocument"]
            if isinstance(document, str):
                document = json.loads(unquote(document))
            statements = document.get("Statement", [])
            statements = statements if isinstance(statements, list) else [statements]
            principals = []
            for statement in statements:
                value = (statement.get("Principal") or {}).get("AWS", [])
                principals += [value] if isinstance(value, str) else list(value)
            self.console.info(
                "The operator role trusts: " + (", ".join(clean(p, 90) for p in principals) or "nobody")
            )
            self.console.info(f"You are signed in as: {clean(self.caller.get('arn', ''), 120)}")
        except Exception:  # noqa: BLE001 - this is a hint only; any failure to read it just skips the hint
            self.console.info("Could not read the operator role's trust policy with this profile.")

    # ------------------------------------------------------------------ 8 plan

    def step_plan(self):
        with self.console.step("Building the plan (no AWS calls)"):
            code, output = self.runner(
                [
                    sys.executable,
                    "-m",
                    "infra.deploy.automation",
                    "dry-run",
                    "--config",
                    str(self.work_dir / "automation.json"),
                    "--work-dir",
                    str(self.work_dir),
                ],
                cwd=self.root,
                env=aws_env(None),
                timeout=300,
            )
            if code != 0:
                raise GuidedError(
                    "The plan could not be built",
                    tail(output),
                    "Nothing in AWS was touched.",
                    "Fix the settings named above and run setup again.",
                )
            try:
                summary = parse_json_object(output)
                plan_hash = summary["plan_hash"]
            except (ValueError, KeyError):
                raise GuidedError(
                    "The plan output could not be read", "", "Nothing in AWS was touched.", "Run setup again."
                ) from None
            private_json(self.work_dir / "plan.hash", {"plan_hash": plan_hash})
            self.console.ok("Plan ready. This is what 'deploy' will do, in order:")
            for number, stage in enumerate(summary["stages"], 1):
                self.console.say(f"  {number:>2}. {stage:<26} {STAGES.get(stage, '')}")
            self.console.say(
                f"\nRouting to Tracaus starts only at '{ROUTING_STAGE}'. A failure before it changes nothing "
                "about your alarms or servers. Investigations stay paused until you enable them.\n"
                "Billable: Bedrock or your model API, CloudWatch, DynamoDB, S3, KMS, Lambda, SNS and SQS. "
                "Set an AWS Budgets alarm before you deploy (Tracaus does not create one)."
            )

    # ------------------------------------------------------------------ 9 report

    def step_report(self):
        with self.console.step("Checking your servers against what Tracaus will read"):
            report = guided_files.readiness(self.choices, self.facts)
            print_report(self.console, report)
            gaps = sum(1 for e in report for c in e["checks"] if c["state"] != "found")
            self.console.say(
                "\nThe CloudWatch agent, log shipping and Container Insights stay on your side. The agent files for "
                f"your instances are in {self.work_dir.name}/collector-examples/.\n"
                + (f"{gaps} item(s) above are missing or unknown. " if gaps else "")
                + "Next: ./bin/tracaus deploy"
            )


def print_report(console, report):
    marks = {"found": "ok", "missing": "MISSING", "unknown": "unknown"}
    for entry in report:
        console.say(f"\n  {entry['target']}  {clean(entry['label'])}")
        for check in entry["checks"]:
            line = f"    {marks[check['state']]:<8} {check['name']}"
            if check["action"]:
                line += f"  -> {check['action']}"
            console.say(line)
