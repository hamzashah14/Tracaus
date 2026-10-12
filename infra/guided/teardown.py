"""`teardown`: remove what `deploy` created, and say loudly what could not be removed.

Order: the CloudFormation stacks (newest first), then what CloudFormation keeps when it deletes a stack (the
incident table, buckets, queues, topics, functions, roles, log groups, secrets, then the KMS key), then the role
stack, then the profile entries `setup` wrote. Only resources that belong to a stack tagged with this project and
environment are touched, and named ones must also carry the project prefix. Your servers, your own log groups and
model access are never touched.

The list of what exists is saved before the first deletion, so an interrupted run continues where it stopped.
Anything that cannot be removed is listed at the end with the reason and the manual steps, and the exit code is 1."""

import collections
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from infra.deploy.automation import factory, private_json
from infra.guided import roles as guided_roles
from infra.guided.common import STAGES, GuidedError
from infra.guided.console import clean
from infra.guided.deploy import read_json
from infra.guided.prompt import NeedsAnswer, Refused, valid_profile
from infra.guided.setup import default_identity, default_profiles
from infra.spec import log_prefix, prefix

OWNERS = {"tracaus-cloudformation", "tracaus-iam-bootstrap"}
INVENTORY = "teardown.json"
MAX_STACKS = 1000
KEY_WINDOW_DAYS = 7  # the AWS minimum
STACK_TIMEOUT, TABLE_TIMEOUT, AGENT_TIMEOUT = 1800, 180, 600
ORDER = {stage: number for number, stage in enumerate(STAGES)}  # creation order

MISSING = {
    "NoSuchBucket",
    "404",
    "NotFound",
    "NotFoundException",
    "NoSuchEntity",
    "ResourceNotFoundException",
    "ResourceNotFound",
    "AWS.SimpleQueueService.NonExistentQueue",
    "QueueDoesNotExist",
}
SIGNED_OUT = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "InvalidAccessKeyId",
    "RequestExpired",
    "UnrecognizedClientException",
    "AuthFailure",
    "SignatureDoesNotMatch",
}
EXPORT_IN_USE = re.compile(r"in use by|cannot be deleted as it is in use", re.I)
RUNTIME_ID = re.compile(r"(?:runtime/)?([A-Za-z0-9_]+-[A-Za-z0-9]+)$")
ENDPOINT_ID = re.compile(r"runtime/([A-Za-z0-9_-]+)/runtime-endpoint/([A-Za-z0-9_-]+)$")

# type -> (what to call it, order of removal). Everything here is kept by CloudFormation or may fail to delete.
KINDS = {
    "AWS::BedrockAgentCore::RuntimeEndpoint": ("AgentCore endpoint", 0),
    "AWS::BedrockAgentCore::Runtime": ("AgentCore runtime", 1),
    "AWS::Lambda::Function": ("Lambda function", 2),
    "AWS::SQS::Queue": ("SQS queue", 3),
    "AWS::SNS::Topic": ("SNS topic", 3),
    "AWS::Logs::LogGroup": ("log group", 3),
    "AWS::SecretsManager::Secret": ("secret", 3),
    "AWS::DynamoDB::Table": ("DynamoDB table (the incident database)", 4),
    "AWS::S3::Bucket": ("S3 bucket (every version of every object)", 4),
    "AWS::IAM::Role": ("IAM role", 5),
    "AWS::KMS::Key": ("KMS key", 6),
}
KEY_PHASE = 6
HOW = {
    "AWS::BedrockAgentCore::RuntimeEndpoint": "Bedrock AgentCore console: delete the endpoint, then the runtime.",
    "AWS::BedrockAgentCore::Runtime": "Bedrock AgentCore console: delete the runtime (after its endpoints).",
    "AWS::Lambda::Function": "Lambda console: delete the function and its /aws/lambda log group.",
    "AWS::SQS::Queue": "SQS console: delete the queue.",
    "AWS::SNS::Topic": "SNS console: delete the topic.",
    "AWS::Logs::LogGroup": "CloudWatch console, Log groups: delete the group.",
    "AWS::SecretsManager::Secret": "Secrets Manager console: delete the secret.",
    "AWS::DynamoDB::Table": "DynamoDB console: turn off deletion protection, then delete the table.",
    "AWS::S3::Bucket": "S3 console: Empty the bucket (all versions), then Delete it.",
    "AWS::IAM::Role": "IAM console: delete the role's policies, then the role.",
    "AWS::KMS::Key": "KMS console: Schedule key deletion (7 days is the minimum).",
}

Item = collections.namedtuple("Item", "type id region stack")


class Options:
    def __init__(self, admin_profile=None, confirm=None, dry_run=False, keep_roles=False):
        self.admin_profile, self.confirm, self.dry_run, self.keep_roles = (
            admin_profile,
            confirm,
            dry_run,
            keep_roles,
        )


def _rank(name):
    return max((number for stage, number in ORDER.items() if name.endswith("-" + stage)), default=-1)


def _is_known(arn, names):
    """True when the ARN names one of `names` exactly. A longer name that merely starts with one does not count."""
    parts = set(re.split(r"[:/]", arn))
    return any(
        arn.endswith((f":{name}", f":{name}:*")) if re.search(r"[:/]", name) else name in parts
        for name in names
    )


def _code(error):
    return error.response.get("Error", {}).get("Code", "Unknown")


class Teardown:
    def __init__(
        self,
        console,
        prompter,
        work_dir,
        options=None,
        *,
        clients_for=factory,
        identity_for=default_identity,
        profiles_for=default_profiles,
        config_path=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.console, self.prompter, self.options = console, prompter, options or Options()
        self.work_dir, self.clients_for, self.identity_for = Path(work_dir), clients_for, identity_for
        self.profiles_for, self.config_path, self.sleep, self.clock = profiles_for, config_path, sleep, clock
        self.spec = self.clients = None
        self.stacks, self.items, self.foreign_iam = [], [], None
        self.manual, self.outcome, self.stack_failed = [], {}, False
        self.handlers = {
            "AWS::BedrockAgentCore::RuntimeEndpoint": self._endpoint,
            "AWS::BedrockAgentCore::Runtime": self._runtime,
            "AWS::Lambda::Function": self._function,
            "AWS::SQS::Queue": self._queue,
            "AWS::SNS::Topic": self._topic,
            "AWS::Logs::LogGroup": self._log_group,
            "AWS::SecretsManager::Secret": self._secret,
            "AWS::DynamoDB::Table": self._table,
            "AWS::S3::Bucket": self._bucket,
            "AWS::IAM::Role": self._role,
            "AWS::KMS::Key": self._key,
        }

    # ------------------------------------------------------------------ the run

    def run(self):
        self.console.begin(7)
        try:
            self.step_identity()
            self.step_find()
            if not self.step_plan():
                return 0
            self.step_stacks()
            self.step_kept()
            self.step_roles_and_local()
            return self.step_report()
        except (GuidedError, Refused, NeedsAnswer, KeyboardInterrupt):
            raise
        except Exception as error:
            raise GuidedError(
                "Teardown stopped on an unexpected error",
                type(error).__name__,
                "Whatever was already deleted stays deleted. Nothing is half-written.",
                "Run teardown again: it finds what is left and continues. Add --debug for the trace.",
            ) from error

    @property
    def name(self):
        return f"{self.spec['project']}-{self.spec['environment']}"

    @property
    def regions(self):
        return list(dict.fromkeys([self.spec["monitor_region"], self.spec["bedrock_region"]]))

    # ------------------------------------------------------------------ 1. who deletes

    def step_identity(self):
        with self.console.step("Choosing who deletes"):
            spec = read_json(self.work_dir / "deployment.json")
            if not isinstance(spec, dict) or any(
                key not in spec
                for key in ("project", "environment", "account_id", "monitor_region", "bedrock_region")
            ):
                raise GuidedError(
                    "There is no deployment to tear down in this folder",
                    f"{self.work_dir / 'deployment.json'} is missing or incomplete",
                    "Nothing was changed.",
                    "Run teardown from the folder you used for setup (--work-dir), or restore deployment.json.",
                )
            self.spec = {"log_segment": "", **spec}
            profiles = self.profiles_for()
            if not profiles:
                raise GuidedError(
                    "No AWS profile was found",
                    "~/.aws/config and ~/.aws/credentials list none",
                    "Nothing was changed.",
                    "Sign in (aws sso login) or create a profile, then run teardown again.",
                )
            answers = read_json(self.work_dir / "answers.json", {}) or {}
            profile = self.options.admin_profile or self.prompter.choose(
                "admin_profile",
                "Which profile deletes everything Tracaus created? It needs administrator rights.",
                [(name, name) for name in profiles],
                default=answers.get("profile"),
            )
            self.prompter.record.pop("admin_profile", None)
            valid_profile(profile)
            try:
                who = self.identity_for(profile)()
            except ClientError as error:
                raise self._signed_out(_code(error)) from None
            except BotoCoreError as error:
                raise GuidedError(
                    "Could not reach AWS",
                    type(error).__name__,
                    "Nothing was changed.",
                    "Check the network and that you are signed in, then run teardown again.",
                ) from None
            if who["Account"] != spec["account_id"]:
                raise GuidedError(
                    "That profile belongs to a different account",
                    f"it is in {who['Account']}, this deployment is in {spec['account_id']}",
                    "Nothing was changed.",
                    "Pick the profile for the account you deployed to.",
                    code=3,
                )
            self.clients = self.clients_for(profile)
            self.console.ok(f"Signed in as {clean(who['Arn'])} in account {spec['account_id']}")

    # ------------------------------------------------------------------ 2. what exists

    def step_find(self):
        with self.console.step("Looking for what Tracaus created"):
            try:
                self._find_stacks()
                found = {item for stack in self.stacks if not stack["iam"] for item in self._resources(stack)}
            except ClientError as error:
                code = _code(error)
                if code in SIGNED_OUT:
                    raise self._signed_out(code) from None
                raise GuidedError(
                    "AWS refused to list the stacks",
                    code,
                    "Nothing was changed.",
                    "Use a profile with administrator rights (--admin-profile) and run teardown again.",
                ) from None
            except BotoCoreError as error:
                raise GuidedError(
                    "Could not reach AWS", type(error).__name__, "Nothing was changed.", "Run teardown again."
                ) from None
            saved = read_json(self.work_dir / INVENTORY, {}) or {}
            if saved.get("name") == self.name and saved.get("account") == self.spec["account_id"]:
                found |= self._saved_items(saved)
            elif saved:
                self.console.warn(f"Ignoring {INVENTORY}: it belongs to another deployment.")
            self.items = sorted(found, key=lambda i: (KINDS[i.type][1], i.type, i.id))
            if self.stacks or self.items:
                private_json(
                    self.work_dir / INVENTORY,
                    {
                        "name": self.name,
                        "account": self.spec["account_id"],
                        "items": [item._asdict() for item in self.items],
                    },
                )
            live = [s for s in self.stacks if not s["iam"]]
            self.console.info(
                f"{len(live)} stack(s), {len(self.items)} resource(s) that CloudFormation keeps"
            )

    def _saved_items(self, saved):
        """Items from an earlier run. Anything that is not a well-formed item of a known kind is ignored."""
        items = set()
        for raw in saved.get("items", []) if isinstance(saved.get("items"), list) else []:
            if (
                isinstance(raw, dict)
                and set(raw) == set(Item._fields)
                and all(isinstance(v, str) for v in raw.values())
                and raw["type"] in KINDS
                and raw["region"] in self.regions
            ):
                items.add(Item(**raw))
            else:
                self.console.warn(f"Ignoring an entry in {INVENTORY} that is not a resource Tracaus created.")
        return items

    def _find_stacks(self):
        spec, iam_name = self.spec, guided_roles.stack_name(self.spec)
        for region in self.regions:
            cfn = self.clients("cloudformation", region)
            pages = cfn.get_paginator("describe_stacks").paginate(PaginationConfig={"MaxItems": MAX_STACKS})
            for page in pages:
                for stack in page["Stacks"]:
                    tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
                    record = {
                        "id": stack["StackId"],
                        "name": stack["StackName"],
                        "region": region,
                        "status": stack["StackStatus"],
                        "protected": bool(stack.get("EnableTerminationProtection")),
                        "iam": False,
                    }
                    if stack["StackName"] == iam_name and region == spec["monitor_region"]:
                        if tags.get("ManagedBy") == "tracaus-iam-bootstrap":
                            self.stacks.append({**record, "iam": True})
                        else:
                            self.foreign_iam = record
                    elif (
                        tags.get("Project") == spec["project"]
                        and tags.get("Environment") == spec["environment"]
                        and tags.get("ManagedBy") in OWNERS
                    ):
                        self.stacks.append(record)

    def _resources(self, stack):
        cfn = self.clients("cloudformation", stack["region"])
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack["id"]):
            for resource in page["StackResourceSummaries"]:
                if (
                    resource["ResourceType"] in KINDS
                    and resource.get("PhysicalResourceId")
                    and resource["ResourceStatus"] != "DELETE_COMPLETE"
                ):
                    yield Item(
                        resource["ResourceType"],
                        resource["PhysicalResourceId"],
                        stack["region"],
                        stack["name"],
                    )

    # ------------------------------------------------------------------ 3. the plan and the confirmation

    def step_plan(self):
        with self.console.step("What will be deleted"):
            if not self.stacks and not self.items and not self.foreign_iam:
                self.console.ok(
                    f"Nothing from {self.name} was found in account {self.spec['account_id']} "
                    f"({', '.join(self.regions)}). Nothing to delete."
                )
                return False
            self._show_plan()
            if self.options.dry_run:
                self.console.say("\nDry run: nothing was deleted. Run again without --dry-run to delete.")
                return False
            self._confirm()
            return True

    def _show_plan(self):
        say, spec = self.console.say, self.spec
        say(f"\nAccount {spec['account_id']}, project {spec['project']}, environment {spec['environment']}.")
        live = self._ordered([s for s in self.stacks if not s["iam"]])
        if live:
            say("\nCloudFormation stacks, newest first:")
            for stack in live:
                note = "   termination protection on; switched off first" if stack["protected"] else ""
                say(f"  {stack['name']:<46} {stack['region']:<14} {stack['status']}{note}")
        by_type = collections.defaultdict(list)
        for item in self.items:
            by_type[item.type].append(self._short(item))
        if by_type:
            say("\nKept by CloudFormation when a stack is deleted, so removed here as well:")
            for kind, ids in by_type.items():
                shown = ", ".join(clean(i, 60) for i in ids[:4]) + (", ..." if len(ids) > 4 else "")
                say(f"  {KINDS[kind][0]} x{len(ids)}: {shown}")
            if "AWS::DynamoDB::Table" in by_type or "AWS::S3::Bucket" in by_type:
                say("  The incident database and all stored reports and evidence are deleted for good.")
            if "AWS::KMS::Key" in by_type:
                say(
                    f"  The KMS key is only scheduled for deletion ({KEY_WINDOW_DAYS} days is the AWS minimum)."
                )
            if any(i.startswith(f"{log_prefix(spec)}/") for i in by_type.get("AWS::Logs::LogGroup", [])):
                say(
                    f"  The log groups under {log_prefix(spec)}/ hold the logs your CloudWatch agent shipped; they are "
                    "deleted (the agent makes an empty group again the next time it writes)."
                )
            if "AWS::SecretsManager::Secret" in by_type:
                say(
                    "  The generated log-cursor secret is deleted at once; a new deployment generates a new one."
                )
        iam = next((s for s in self.stacks if s["iam"]), None)
        if iam and not self.options.keep_roles:
            say(f"\nThen the role stack {iam['name']} (the operator, CloudFormation and UI roles).")
        elif iam:
            say(f"\nThe role stack {iam['name']} is kept (--keep-roles).")
        if self.foreign_iam:
            say(f"\nThe stack {self.foreign_iam['name']} was not created by this tool, so it is left alone.")
        say(
            "\nThen your local AWS profile entries that setup added (only the marked blocks) and the deployment "
            "journal in the work folder are set aside."
            "\nNot touched: your EC2 instances and ECS services, the CloudWatch agent, your own log groups, "
            "Bedrock model access, other projects."
        )

    def _confirm(self):
        expected = self.name
        question = (
            f"This permanently deletes the deployment {expected} in account {self.spec['account_id']}, "
            "including its incident database and stored evidence."
        )
        if self.options.confirm is not None:
            if self.options.confirm != expected:
                raise Refused(f"--confirm must be exactly {expected}")
            return
        if not self.prompter.typed("confirm_name", question, expected):
            raise GuidedError(
                "Teardown was not confirmed",
                "the name was not typed exactly",
                "Nothing was deleted.",
                "Run teardown again when you are sure.",
                code=3,
            )

    def _ordered(self, stacks):
        return sorted(stacks, key=lambda s: (-_rank(s["name"]), s["name"]))

    # ------------------------------------------------------------------ 4. the stacks

    def step_stacks(self):
        with self.console.step("Deleting the CloudFormation stacks"):
            pending = self._ordered([s for s in self.stacks if not s["iam"]])
            if not pending:
                self.console.ok("No stacks are left to delete")
                return
            for attempt in range(3):
                retry = []
                for stack in pending:
                    if self._delete_stack(stack, last=attempt == 2) == "retry":
                        retry.append(stack)
                if not retry or len(retry) == len(pending):
                    for stack in retry:  # a stack that keeps failing on exports needs a person
                        self._manual_stack(stack, "another stack still uses what it exports")
                    break
                pending = retry
            self.stack_failed = any(m["kind"] == "stack" for m in self.manual)

    def _delete_stack(self, stack, *, last):
        """Returns done, retry or failed. Failures are recorded for the final report."""
        label = f"{stack['name']} ({stack['region']})"
        cfn = self.clients("cloudformation", stack["region"])
        try:
            status = self._wait_stack(cfn, stack)
            if status is None:
                return self._manual_stack(
                    stack, "it is still changing after 30 minutes; it keeps running in AWS"
                )
            if status != "DELETE_COMPLETE":
                if stack["protected"]:
                    cfn.update_termination_protection(
                        EnableTerminationProtection=False, StackName=stack["id"]
                    )
                    self.console.info(f"{label}: termination protection switched off")
                cfn.delete_stack(StackName=stack["id"])
                status = self._wait_stack(cfn, stack, after_delete=True)
            if status == "DELETE_FAILED":
                failed = self._failed_resources(cfn, stack)
                if any(EXPORT_IN_USE.search(r["reason"]) for r in failed) and not last:
                    self.console.warn(f"{label}: another stack still uses it; trying again after the others")
                    return "retry"
                status = self._delete_keeping(cfn, stack, failed)
            if status == "DELETE_COMPLETE":
                self.console.ok(f"{label}: deleted")
                return "done"
            if status is None:
                return self._manual_stack(
                    stack, "deletion is still running after 30 minutes; it continues in AWS"
                )
            return self._manual_stack(stack, f"it ended in {status}")
        except ClientError as error:
            code = _code(error)
            if code in SIGNED_OUT:
                raise self._signed_out(code) from None
            return self._manual_stack(
                stack, f"AWS answered {code}: {clean(error.response['Error'].get('Message', ''), 160)}"
            )
        except BotoCoreError as error:
            return self._manual_stack(stack, f"could not reach AWS ({type(error).__name__})")

    def _delete_keeping(self, cfn, stack, failed):
        """A stack stuck in DELETE_FAILED: delete it again while keeping the resources that could not go."""
        if not failed:
            return "DELETE_FAILED"
        names = [r["logical"] for r in failed]
        self.console.warn(
            f"{stack['name']}: could not delete {', '.join(names)}; keeping them and deleting the rest"
        )
        cfn.delete_stack(StackName=stack["id"], RetainResources=names)
        status = self._wait_stack(cfn, stack, after_delete=True)
        if status == "DELETE_COMPLETE":
            for resource in failed:
                if resource["type"] not in KINDS:
                    self.manual.append(
                        {
                            "kind": "resource",
                            "what": f"{resource['type']} {resource['physical']} ({stack['region']})",
                            "why": f"CloudFormation could not delete it: {resource['reason']}",
                            "how": "Open it in its service console and delete it.",
                        }
                    )
        return status

    def _wait_stack(self, cfn, stack, *, after_delete=False):
        """Wait until the stack is not changing. Returns its status, DELETE_COMPLETE once it is gone, or None.

        Right after a delete request AWS may still report the old status for a moment, so a few polls ignore it."""
        seen, deadline, grace = set(), self.clock() + STACK_TIMEOUT, 3 if after_delete else 0
        while True:
            for event in guided_roles.new_events(cfn, stack["id"], seen):
                status = event.get("ResourceStatus", "")
                if status in {"DELETE_COMPLETE", "DELETE_FAILED", "DELETE_SKIPPED"}:
                    detail = (
                        f" ({clean(event['ResourceStatusReason'], 140)})" if status == "DELETE_FAILED" else ""
                    )
                    self.console.info(f"{clean(event.get('LogicalResourceId', ''))}: {status}{detail}")
            try:
                status = cfn.describe_stacks(StackName=stack["id"])["Stacks"][0]["StackStatus"]
            except ClientError as error:
                if "does not exist" in error.response.get("Error", {}).get("Message", ""):
                    return "DELETE_COMPLETE"
                raise
            if grace and not status.startswith("DELETE"):
                grace -= 1
            elif not status.endswith("_IN_PROGRESS"):
                return status
            if self.clock() >= deadline:
                return None
            self.sleep(5)

    def _failed_resources(self, cfn, stack):
        failed = []
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack["id"]):
            for r in page["StackResourceSummaries"]:
                if r["ResourceStatus"] == "DELETE_FAILED":
                    failed.append(
                        {
                            "logical": r["LogicalResourceId"],
                            "type": r["ResourceType"],
                            "physical": r.get("PhysicalResourceId", "?"),
                            "reason": clean(r.get("ResourceStatusReason", "no reason was recorded"), 200),
                        }
                    )
        return failed

    def _manual_stack(self, stack, why):
        self.console.fail(f"{stack['name']} ({stack['region']}): {why}")
        self.manual.append(
            {
                "kind": "stack",
                "what": f"CloudFormation stack {stack['name']} ({stack['region']})",
                "why": why,
                "how": "CloudFormation console: open the stack, read its events, fix the cause, delete it; "
                "or run teardown again.",
            }
        )
        return "failed"

    # ------------------------------------------------------------------ 5. what CloudFormation keeps

    def step_kept(self):
        with self.console.step("Removing what CloudFormation keeps"):
            if not self.items:
                self.console.ok("Nothing was kept")
                return
            for item in sorted(
                self.items, key=lambda i: KINDS[i.type][1]
            ):  # the key must come after its data
                label = f"{KINDS[item.type][0].split(' (')[0]} {clean(self._short(item), 80)}"
                if KINDS[item.type][1] == KEY_PHASE and self._data_left():
                    state, note = (
                        "manual",
                        (
                            "not scheduled: something it encrypts is still there, and deleting the key would "
                            "make that unreadable"
                        ),
                    )
                else:
                    state, note = self._remove(item)
                self.outcome[item] = state
                if state == "manual":
                    self.console.fail(f"{label}: {note}")
                    self.manual.append(
                        {
                            "kind": "resource",
                            "what": f"{label} ({item.region})",
                            "why": note,
                            "how": HOW[item.type],
                        }
                    )
                elif state == "scheduled":
                    self.console.warn(f"{label}: {note}")
                else:
                    self.console.ok(f"{label}: {note or ('already gone' if state == 'gone' else 'deleted')}")

    def _data_left(self):
        return any(
            state == "manual" and KINDS[item.type][1] < KEY_PHASE for item, state in self.outcome.items()
        )

    def _remove(self, item):
        try:
            return self.handlers[item.type](item)
        except ClientError as error:
            code = _code(error)
            if code in SIGNED_OUT:
                raise self._signed_out(code) from None
            if code in MISSING:
                return "gone", "already gone"
            return "manual", f"AWS answered {code}: {clean(error.response['Error'].get('Message', ''), 160)}"
        except BotoCoreError as error:
            return "manual", f"could not reach AWS ({type(error).__name__}); run teardown again"

    # --- name checks

    def _short(self, item):
        """The name inside an ARN or URL, or the id itself."""
        if ":function:" in item.id:
            return item.id.split(":function:", 1)[1].split(":")[0]
        if ":secret:" in item.id:
            return item.id.split(":secret:", 1)[1]
        if item.id.startswith(("arn:", "http")):
            return item.id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return item.id

    def _owned(self, name):
        p, e = self.spec["project"], self.spec["environment"]
        return name.startswith(
            (
                f"{prefix(self.spec)}-",
                f"{prefix(self.spec)}/",
                f"{log_prefix(self.spec)}/",
                f"/{p}/{e}/",
                f"/aws/lambda/{prefix(self.spec)}-",
                f"/aws/bedrock-agentcore/runtimes/{p}_{e}_",
            )
        )

    @staticmethod
    def _foreign():
        return "manual", "its name does not start with this project's prefix, so it is not deleted here"

    def _absent(self, call, **kwargs):
        try:
            call(**kwargs)
        except ClientError as error:
            if _code(error) in MISSING:
                return True
            raise
        return False

    def _until(self, check, timeout, poll=3.0):
        deadline = self.clock() + timeout
        while not check():
            if self.clock() >= deadline:
                return False
            self.sleep(poll)
        return True

    # --- one handler per kind of resource

    def _endpoint(self, item):
        match = ENDPOINT_ID.search(item.id)
        if not match:
            return "manual", "the endpoint id was not in a recognised format"
        runtime, endpoint = match.groups()
        agent = self.clients("bedrock-agentcore-control", item.region)
        agent.get_agent_runtime_endpoint(agentRuntimeId=runtime, endpointName=endpoint)
        agent.delete_agent_runtime_endpoint(agentRuntimeId=runtime, endpointName=endpoint)
        gone = self._until(
            lambda: self._absent(
                agent.get_agent_runtime_endpoint, agentRuntimeId=runtime, endpointName=endpoint
            ),
            AGENT_TIMEOUT,
        )
        return "deleted", "" if gone else "deleting in AWS"

    def _runtime(self, item):
        match = RUNTIME_ID.search(item.id)
        if not match:
            return "manual", "the runtime id was not in a recognised format"
        agent = self.clients("bedrock-agentcore-control", item.region)
        agent.get_agent_runtime(agentRuntimeId=match[1])
        agent.delete_agent_runtime(agentRuntimeId=match[1])
        return "deleted", "deleting in AWS"

    def _function(self, item):
        name = self._short(item)
        if not self._owned(name):
            return self._foreign()
        lam, state = self.clients("lambda", item.region), "deleted"
        try:
            lam.get_function(FunctionName=name)
            lam.delete_function(FunctionName=name)
        except ClientError as error:
            if _code(error) not in MISSING:
                raise
            state = "gone"
        self._drop_log_group(item.region, f"/aws/lambda/{name}")
        return state, ""

    def _drop_log_group(self, region, name):
        logs = self.clients("logs", region)
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name):
            if any(group["logGroupName"] == name for group in page["logGroups"]):
                logs.delete_log_group(logGroupName=name)
                return True
        return False

    def _log_group(self, item):
        if not self._owned(item.id):
            return self._foreign()
        return ("deleted", "") if self._drop_log_group(item.region, item.id) else ("gone", "")

    def _queue(self, item):
        if not self._owned(self._short(item)):
            return self._foreign()
        self.clients("sqs", item.region).delete_queue(QueueUrl=item.id)
        return "deleted", "AWS frees the name after about a minute"

    def _topic(self, item):
        if not self._owned(self._short(item)):
            return self._foreign()
        sns = self.clients("sns", item.region)
        sns.get_topic_attributes(TopicArn=item.id)
        sns.delete_topic(TopicArn=item.id)
        return "deleted", ""

    def _secret(self, item):
        if not self._owned(self._short(item)):
            return self._foreign()
        secrets = self.clients("secretsmanager", item.region)
        secrets.describe_secret(SecretId=item.id)
        secrets.delete_secret(SecretId=item.id, ForceDeleteWithoutRecovery=True)
        return "deleted", "no recovery window"

    def _table(self, item):
        if not self._owned(item.id):
            return self._foreign()
        ddb = self.clients("dynamodb", item.region)
        table = ddb.describe_table(TableName=item.id)["Table"]
        if table.get("DeletionProtectionEnabled"):
            ddb.update_table(TableName=item.id, DeletionProtectionEnabled=False)
            if not self._until(
                lambda: ddb.describe_table(TableName=item.id)["Table"]["TableStatus"] == "ACTIVE",
                TABLE_TIMEOUT,
            ):
                return "manual", "the table did not become ready after deletion protection was switched off"
        ddb.delete_table(TableName=item.id)
        return "deleted", "the incident database is gone"

    def _bucket(self, item):
        if not self._owned(item.id):
            return self._foreign()
        s3, owner = self.clients("s3", item.region), {"ExpectedBucketOwner": self.spec["account_id"]}
        s3.head_bucket(Bucket=item.id, **owner)
        removed = 0
        while True:
            page = s3.list_object_versions(Bucket=item.id, MaxKeys=1000, **owner)
            objects = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if not objects:
                break
            result = s3.delete_objects(Bucket=item.id, Delete={"Objects": objects, "Quiet": True}, **owner)
            errors = result.get("Errors", [])
            if errors:
                codes = ", ".join(sorted({e.get("Code", "?") for e in errors})[:3])
                return "manual", (
                    f"{len(errors)} object version(s) could not be deleted ({codes}); with Object Lock the "
                    "retention must end first"
                )
            removed += len(objects)
            self.console.info(f"{item.id}: {removed} object versions removed so far")
        s3.delete_bucket(Bucket=item.id, **owner)
        return "deleted", f"{removed} object version(s) removed first"

    def _role(self, item):
        iam = self.clients("iam", self.spec["monitor_region"])
        role = iam.get_role(RoleName=item.id)["Role"]
        if not (
            self._owned(item.id) or role.get("Path") == f"/{self.spec['project']}/{self.spec['environment']}/"
        ):
            return self._foreign()
        inline = [
            n
            for p in iam.get_paginator("list_role_policies").paginate(RoleName=item.id)
            for n in p["PolicyNames"]
        ]
        attached = [
            a["PolicyArn"]
            for p in iam.get_paginator("list_attached_role_policies").paginate(RoleName=item.id)
            for a in p["AttachedPolicies"]
        ]
        profiles = iam.list_instance_profiles_for_role(RoleName=item.id)["InstanceProfiles"]
        if profiles:
            names = ", ".join(clean(p["InstanceProfileName"], 60) for p in profiles)
            return "manual", f"it is part of the instance profile {names}; remove it from the profile first"
        for policy in inline:
            iam.delete_role_policy(RoleName=item.id, PolicyName=policy)
        for arn in attached:
            iam.detach_role_policy(RoleName=item.id, PolicyArn=arn)
        iam.delete_role(RoleName=item.id)
        return "deleted", ""

    def _key(self, item):
        kms = self.clients("kms", item.region)
        meta = kms.describe_key(KeyId=item.id)["KeyMetadata"]
        if meta.get("KeyManager") == "AWS":
            return "manual", "this is an AWS-managed key and cannot be deleted"
        if meta.get("KeyState") == "PendingDeletion":
            return "scheduled", f"deletion was already scheduled for {meta['DeletionDate']:%Y-%m-%d}"
        reply = kms.schedule_key_deletion(KeyId=item.id, PendingWindowInDays=KEY_WINDOW_DAYS)
        return (
            "scheduled",
            f"AWS deletes it on {reply['DeletionDate']:%Y-%m-%d}; until then it can be restored",
        )

    # ------------------------------------------------------------------ 6. role stack and local files

    def step_roles_and_local(self):
        with self.console.step("Removing the IAM roles and your local profile entries"):
            iam = next((s for s in self.stacks if s["iam"]), None)
            if self.foreign_iam:
                self.manual.append(
                    {
                        "kind": "stack",
                        "what": f"CloudFormation stack {self.foreign_iam['name']} ({self.foreign_iam['region']})",
                        "why": "it was not created by this tool, so it was left alone",
                        "how": "If it is yours to remove, delete it in the CloudFormation console.",
                    }
                )
            if iam and self.options.keep_roles:
                self.console.info(f"Kept the role stack {iam['name']} (--keep-roles)")
            elif iam and self.stack_failed:
                self.console.warn(
                    f"Kept the role stack {iam['name']}: a stack that CloudFormation could not delete still needs its "
                    "execution role. Run teardown again after fixing that stack."
                )
            elif iam:
                self._delete_stack(iam, last=True)
            if self.manual:
                self.console.warn("Local files are kept so that teardown can be run again after the fix.")
                return
            self._clean_local()

    def _clean_local(self):
        names = [f"{self.name}-{suffix}" for suffix in ("operator", "ui-principal", "ui")]
        path = self.config_path or guided_roles.config_path()
        try:
            # With --keep-roles the profiles that use those roles stay too.
            result = {"removed": []} if self.options.keep_roles else guided_roles.remove_profiles(path, names)
            if result["removed"]:
                self.console.ok(f"Removed the profile entries {', '.join(result['removed'])} from {path}")
                self.console.info(f"A backup of the file is at {result['backup']}")
        except guided_roles.RolesError as error:
            self.console.warn(f"Left {path} alone: {error}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for name in ("state.json", "ui-connection.json", INVENTORY):
            source = self.work_dir / name
            if source.is_file() and name != INVENTORY:
                source.rename(source.with_name(f"{name}.torn-down-{stamp}"))
                self.console.info(f"Set aside {name} (saved as {name}.torn-down-{stamp})")
            elif source.is_file():
                source.unlink()

    # ------------------------------------------------------------------ 7. the final check and report

    def step_report(self):
        with self.console.step("Checking what is left"):
            self._leftovers()
        if not self.manual:
            self.console.say(
                f"\nTeardown finished. Everything Tracaus created for {self.name} is removed"
                + (
                    " (the KMS key goes after its waiting period)."
                    if "scheduled" in self.outcome.values()
                    else "."
                )
            )
            self.console.say(
                "Delete the EC2 test instance, its role and any admin access key yourself if you made them for this test."
            )
            return 0
        say = self.console.say
        say("\n" + "=" * 72)
        say(f"MANUAL REMOVAL NEEDED: {len(self.manual)} item(s) are still in your AWS account.")
        say("They were NOT deleted, and they may keep costing money or holding data.")
        say("=" * 72)
        for number, entry in enumerate(self.manual, 1):
            self.console.fail(f"{number}. {entry['what']}")
            say(f"      Why:  {clean(entry['why'], 300)}")
            say(f"      How:  {entry['how']}")
        say("\nFix the cause or delete them by hand, then run ./bin/tracaus teardown again to confirm.")
        return 1

    def _leftovers(self):
        """Ask the tag index what Tracaus-owned things AWS still lists. It can trail a deletion by a few minutes."""
        handled = {self._short(item) for item in self.items} | {s["name"] for s in self.stacks}
        if self.options.keep_roles:
            handled |= {f"{self.name}-{suffix}" for suffix in ("operator", "cfn-execution", "ui")}
        filters = [
            {"Key": "Project", "Values": [self.spec["project"]]},
            {"Key": "Environment", "Values": [self.spec["environment"]]},
            {"Key": "ManagedBy", "Values": sorted(OWNERS)},
        ]
        for region in self.regions:
            try:
                pages = (
                    self.clients("resourcegroupstaggingapi", region)
                    .get_paginator("get_resources")
                    .paginate(TagFilters=filters, PaginationConfig={"MaxItems": 500})
                )
                for page in pages:
                    for mapping in page["ResourceTagMappingList"]:
                        arn = mapping["ResourceARN"]
                        if _is_known(arn, handled):
                            continue
                        self.manual.append(
                            {
                                "kind": "resource",
                                "what": f"{arn}",
                                "why": "AWS still lists this Tracaus-tagged resource and teardown did not remove it "
                                "(the tag index can trail a deletion by a few minutes)",
                                "how": "Run teardown again in a few minutes; if it is still listed, delete it yourself.",
                            }
                        )
            except (ClientError, BotoCoreError) as error:
                code = _code(error) if isinstance(error, ClientError) else type(error).__name__
                if code in SIGNED_OUT:
                    raise self._signed_out(code) from None
                self.console.warn(f"Could not double-check {region} with the tag index ({code}).")
        if not self.manual:
            self.console.ok("No Tracaus-tagged resources are listed any more")

    # ------------------------------------------------------------------ shared

    @staticmethod
    def _signed_out(code):
        return GuidedError(
            "Your AWS sign-in expired or is not valid",
            code,
            "Whatever was already deleted stays deleted. Nothing is half-written.",
            "Sign in again (aws sso login) and run teardown again; it continues where it stopped.",
        )
