"""The three IAM roles and the AWS profiles that use them.

The role stack is the one AWS resource the guided commands create outside `apply`, with a profile the user
chooses once. It is created through CloudFormation, so a failure rolls itself back. The only thing this module
ever deletes is a stack it created itself, whose first creation ended in ROLLBACK_COMPLETE (it then holds no
resources). Profiles are written into `~/.aws/config` inside marked blocks, after a backup."""

import datetime
import json
import os
import re
import time
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from infra import bootstrap_iam
from infra.guided.console import clean
from infra.guided.discover import role_state

OWNER_TAG = {"Key": "ManagedBy", "Value": "tracaus-iam-bootstrap"}
ROLE_ARN = re.compile(r"arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+")
PROFILE = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
REGION = re.compile(r"[a-z]{2}-[a-z]+-[0-9]")
SUCCESS = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}


class RolesError(Exception):
    """The roles or profiles could not be set up. The message says what is safe and what to do."""


def role_arns(project, environment, account):
    prefix = f"arn:aws:iam::{account}:role"
    return {
        "operator": f"{prefix}/{project}-{environment}-operator",
        "execution": f"{prefix}/{project}-{environment}-cfn-execution",
        "ui": f"{prefix}/{project}-{environment}-ui",
    }


def stack_name(spec):
    return f"{spec['project']}-{spec['environment']}-iam"


def trust_for(caller):
    """Who may assume the roles: the caller's own principal when known, else the whole account (with a warning)."""
    if caller.get("principal_arn"):
        return [caller["principal_arn"]], None
    return [f"arn:aws:iam::{caller['account']}:root"], (
        "Your sign-in is not a plain IAM user or a role whose ARN could be read, so the roles will trust every "
        "identity in the account that an IAM policy allows. Narrow it later with --operator-trust."
    )


def render(spec, trust, *, instance_role=False):
    return bootstrap_iam.render(spec, operator_trust=trust, ui_trust=trust, instance_role=instance_role)


def plan_roles(clients, arns):
    """`{role: state}` for the three roles, using `found`, `missing`, `conflict` or `unknown`."""
    return {name: role_state(clients, arn)["state"] for name, arn in arns.items()}


def _describe(cfn, name):
    try:
        stacks = cfn.describe_stacks(StackName=name)["Stacks"]
    except ClientError as error:
        if "does not exist" in error.response.get("Error", {}).get("Message", ""):
            return None
        raise
    return stacks[0]


def _tagged(stack):
    return OWNER_TAG in stack.get("Tags", [])


_MAYBE_RUNNING = (
    "The stack {name} may still be creating in AWS and keeps running by itself. Run setup again to watch it."
)


def deploy_roles(
    clients,
    spec,
    template,
    console,
    prompter,
    *,
    poll=5.0,
    timeout=900,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Create the role stack with the administrator profile behind `clients`. Returns `created` or `exists`."""
    region, name = spec["monitor_region"], stack_name(spec)
    cfn = clients("cloudformation", region)
    started = False  # True once AWS may be building the stack: from then on "nothing was changed" is not true
    try:
        stack = _describe(cfn, name)
        if stack is not None:
            status = stack["StackStatus"]
            if status in SUCCESS:
                console.ok(f"The role stack {name} already exists ({status}).")
                return "exists"
            if status == "ROLLBACK_COMPLETE":
                _reset(cfn, name, stack, console, prompter, poll, timeout, sleep, clock)
            elif status.endswith("_IN_PROGRESS"):
                started = True
                console.info(f"The role stack {name} is already changing ({status}); watching it.")
                _wait(cfn, name, console, poll, timeout, sleep, clock)
                return "created"
            else:
                raise RolesError(
                    f"The role stack {name} is in state {status}. Nothing was changed. Open it in the CloudFormation "
                    "console, read its events, and have an administrator resolve it before running setup again."
                )
        console.say(
            f"Creating the role stack {name} in {region}. CloudFormation undoes it by itself if it fails."
        )
        started = True
        cfn.create_stack(
            StackName=name,
            TemplateBody=json.dumps(template),
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=[OWNER_TAG],
            OnFailure="ROLLBACK",
        )
        _wait(cfn, name, console, poll, timeout, sleep, clock)
        return "created"
    except BotoCoreError as error:
        raise RolesError(
            f"Could not reach AWS ({type(error).__name__}). "
            + (_MAYBE_RUNNING.format(name=name) if started else "Nothing was changed by this step.")
        ) from None
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "Unknown")
        if started:
            raise RolesError(
                f"AWS answered {code} while the role stack was being created. "
                + _MAYBE_RUNNING.format(name=name)
            ) from None
        raise RolesError(
            f"AWS refused the role stack request ({code}). Nothing was created. The profile you picked must be allowed "
            "to create CloudFormation stacks and IAM roles, or ask an administrator to run the printed command."
        ) from None


def _reset(cfn, name, stack, console, prompter, poll, timeout, sleep, clock):
    """Remove a failed first creation. Only a stack this tool tagged, in ROLLBACK_COMPLETE, which holds nothing."""
    if not _tagged(stack):
        raise RolesError(
            f"The role stack {name} ended in ROLLBACK_COMPLETE but was not created by this tool, so it will not be "
            "deleted. Delete it yourself (it holds no resources), then run setup again."
        )
    if not prompter.confirm(
        "reset-iam-stack",
        f"The earlier attempt to create {name} failed and was rolled back. Delete that empty stack and try again?",
        consent="iam-bootstrap",
    ):
        raise RolesError(f"Left the failed stack {name} in place. Run setup again when you are ready.")
    cfn.delete_stack(StackName=name)
    deadline = clock() + timeout
    while clock() < deadline:
        stack = _describe(cfn, name)
        if stack is None:
            console.ok(f"Removed the failed stack {name}.")
            return
        if stack["StackStatus"] == "DELETE_FAILED":
            raise RolesError(f"Deleting {name} failed. Open it in the CloudFormation console.")
        sleep(poll)
    raise RolesError(f"Deleting {name} is taking longer than {timeout}s; run setup again to continue.")


def _wait(cfn, name, console, poll, timeout, sleep, clock):
    seen, deadline = set(), clock() + timeout
    while True:
        for event in _new_events(cfn, name, seen):
            console.info(
                f"{clean(event.get('LogicalResourceId', ''))}: {clean(event.get('ResourceStatus', ''))}"
            )
        stack = _describe(cfn, name)
        status = stack["StackStatus"] if stack else "DELETE_COMPLETE"
        if status in SUCCESS:
            return
        if not status.endswith("_IN_PROGRESS"):
            raise RolesError(_failure(cfn, name, status))
        if clock() >= deadline:
            raise RolesError(
                f"The role stack {name} is still working after {timeout}s. It keeps running in AWS; run setup again to continue."
            )
        sleep(poll)


def _new_events(cfn, name, seen):
    try:
        events = cfn.describe_stack_events(StackName=name)["StackEvents"]
    except ClientError:
        return []
    fresh = [e for e in reversed(events) if e["EventId"] not in seen]
    seen.update(e["EventId"] for e in fresh)
    return fresh


def _failure(cfn, name, status):
    reasons = []
    try:
        for event in cfn.describe_stack_events(StackName=name)["StackEvents"]:
            if event.get("ResourceStatus", "").endswith("FAILED") and event.get("ResourceStatusReason"):
                reasons.append(f"{event.get('LogicalResourceId', '?')}: {event['ResourceStatusReason']}")
    except ClientError:
        pass
    detail = clean(reasons[-1], 300) if reasons else "no reason was recorded"
    return (
        f"The role stack {name} ended in {status}: {detail}. CloudFormation removed what it had created, so no "
        "roles were left behind. Fix the cause (often missing IAM permissions on the profile you picked) and run setup again."
    )


def verify_operator(make_identity, role_arn, console, *, attempts=12, delay=5, sleep=time.sleep):
    """After creation IAM needs a moment. Wait until the operator profile really assumes the operator role."""
    expected = f"arn:aws:sts::{role_arn.split(':')[4]}:assumed-role/{role_arn.rsplit('/', 1)[-1]}/"
    last = "no attempt was made"
    for number in range(attempts):
        try:
            arn = make_identity()["Arn"]
            if arn.startswith(expected):
                return
            raise RolesError(
                "The operator profile does not assume the operator role. Check role_arn and source_profile."
            )
        except ClientError as error:
            last = error.response.get("Error", {}).get("Code", "Unknown")
        except BotoCoreError as error:
            last = type(error).__name__
        if number + 1 < attempts:
            console.info(f"Waiting for IAM to let you assume the operator role ({last})...")
            sleep(delay)
    raise RolesError(
        f"The operator profile cannot assume the operator role yet ({last}). The roles exist. Your sign-in must be "
        "named in the operator role's trust policy (IAM console, the role, Trust relationships), or, if the roles "
        "were made by someone else, ask them to add it. Then run setup again."
    )


def profile_block(name, role_arn, source_profile, region):
    """One profile section, validated so no value can add a line or another section."""
    if not PROFILE.fullmatch(name) or name == "default" or not PROFILE.fullmatch(source_profile):
        raise RolesError("A profile name is not valid.")
    if name == source_profile:
        raise RolesError(
            "The profile that assumes the role cannot be the role's own profile. Pick your normal sign-in profile."
        )
    if not ROLE_ARN.fullmatch(role_arn) or not REGION.fullmatch(region or ""):
        raise RolesError("A role ARN or region is not valid.")
    return f"[profile {name}]\nrole_arn = {role_arn}\nsource_profile = {source_profile}\nregion = {region}\n"


def _markers(name):
    return f"# >>> tracaus:{name} >>>", f"# <<< tracaus:{name} <<<"


def config_path():
    return Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")


def write_profiles(path, blocks, *, now=None):
    """Add or update marked profile blocks. Returns {"written", "unchanged", "skipped", "backup"}.

    A profile with that name that already exists outside our markers is never touched (it is `skipped`). The file
    is backed up first, written atomically, re-read, and restored if the re-read does not match."""
    path = Path(path)
    real = path.resolve()
    if real.exists() and (not real.is_file() or real.stat().st_uid != os.getuid()):
        raise RolesError("The AWS config path is not a regular file you own; edit it by hand.")
    text = real.read_text() if real.exists() else ""
    result = {"written": [], "unchanged": [], "skipped": [], "backup": None}
    updated, applied = text, []
    for block in blocks:
        name = re.match(r"\[profile ([^\]]+)\]", block)[1]
        start, end = _markers(name)
        pattern = re.compile(re.escape(start) + r"\n.*?" + re.escape(end) + r"\n?", re.S)
        wrapped = f"{start}\n{block}{end}\n"
        if pattern.search(updated):
            if pattern.search(updated)[0] == wrapped:
                result["unchanged"].append(name)
                continue
            updated = pattern.sub(lambda _: wrapped, updated, count=1)
        elif re.search(rf"^\[profile {re.escape(name)}\]\s*(?:[#;].*)?$", updated, re.M):
            result["skipped"].append(name)
            continue
        else:
            updated += (
                ("" if not updated or updated.endswith("\n") else "\n") + ("\n" if updated else "") + wrapped
            )
        result["written"].append(name)
        applied.append(block)
    if not result["written"]:
        return result
    if real.exists():
        stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        backup = real.with_name(f"{real.name}.tracaus-backup-{stamp}")
        backup.write_text(text)
        os.chmod(backup, 0o600)
        result["backup"] = str(backup)
    real.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = real.with_name(real.name + ".tmp")
    mode = real.stat().st_mode & 0o777 if real.exists() else 0o600
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, real)
    finally:
        temporary.unlink(missing_ok=True)
    if not _matches(real, applied):
        real.write_text(text)
        if result["backup"]:
            Path(result["backup"]).unlink(
                missing_ok=True
            )  # the original is back; the copy would only confuse
        raise RolesError("The AWS config did not read back as written; the original was restored.")
    return result


def _matches(path, blocks):
    from botocore.configloader import load_config

    try:
        profiles = load_config(str(path)).get("profiles", {})
    except Exception:  # noqa: BLE001 - any parse failure means the edit is not trusted
        return False
    for block in blocks:
        name = re.match(r"\[profile ([^\]]+)\]", block)[1]
        wanted = dict(line.split(" = ", 1) for line in block.splitlines()[1:])
        if name not in profiles or any(profiles[name].get(k) != v for k, v in wanted.items()):
            return False
    return True
