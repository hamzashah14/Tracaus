"""The role stack, the operator check and the AWS profile blocks."""

import io
import json
import os
import stat

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY

from infra.guided import roles as guided_roles
from infra.guided.console import Console
from infra.guided.prompt import ScriptedPrompter
from infra.guided.roles import (
    OWNER_TAG,
    RolesError,
    deploy_roles,
    plan_roles,
    profile_block,
    render,
    role_arns,
    stack_name,
    trust_for,
    verify_operator,
    write_profiles,
)
from infra.spec import ROOT, load
from tests.guided_fakes import ACCOUNT, REGION, Fake

ARNS = role_arns("tracaus", "staging", ACCOUNT)
STACK = "tracaus-staging-iam"
MISSING = "Stack with id tracaus-staging-iam does not exist"


@pytest.fixture
def spec(tmp_path):
    value = json.loads((ROOT / "examples/deployment.example.json").read_text())
    value.update(
        reference_only=False,
        ci_principal_arn=ARNS["operator"],
        deployment_role_arn=ARNS["execution"],
        ui_principal_arn=ARNS["ui"],
    )
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return load(path)


@pytest.fixture
def fake():
    return Fake()


def console():
    out = io.StringIO()
    return Console(out), out


def stack(status, tagged=True):
    return {
        "Stacks": [
            {
                "StackName": STACK,
                "CreationTime": "2026-01-01T00:00:00Z",
                "StackStatus": status,
                **({"Tags": [OWNER_TAG]} if tagged else {}),
            }
        ]
    }


def event(number, resource="OperatorRole", status="CREATE_IN_PROGRESS", reason=None):
    item = {
        "StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1",
        "EventId": f"e{number}",
        "StackName": STACK,
        "Timestamp": "2026-01-01T00:00:00Z",
        "LogicalResourceId": resource,
        "ResourceStatus": status,
    }
    if reason:
        item["ResourceStatusReason"] = reason
    return {"StackEvents": [item]}


class Clock:
    def __init__(self, step=1.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def run(fake, spec, prompter=None, **kwargs):
    out_console, out = console()
    result = deploy_roles(
        fake,
        spec,
        {"Resources": {}},
        out_console,
        prompter or ScriptedPrompter({}),
        sleep=lambda _: None,
        clock=Clock(),
        **kwargs,
    )
    return result, out.getvalue()


def test_default_names_and_stack_name(spec):
    assert ARNS["operator"].endswith("role/tracaus-staging-operator")
    assert stack_name(spec) == STACK


def test_trust_is_the_callers_own_principal_when_known():
    principal = f"arn:aws:iam::{ACCOUNT}:user/alice"
    assert trust_for({"account": ACCOUNT, "principal_arn": principal}) == ([principal], None)
    trust, warning = trust_for({"account": ACCOUNT, "principal_arn": None})
    assert trust == [f"arn:aws:iam::{ACCOUNT}:root"] and "Narrow" in warning


def test_the_rendered_template_trusts_only_that_principal(spec):
    principal = f"arn:aws:iam::{ACCOUNT}:user/alice"
    template = render(spec, [principal])
    for role in ("OperatorRole", "UiPrincipalRole"):
        statement = template["Resources"][role]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement["Principal"]["AWS"] == [principal]
    assert len(json.dumps(template)) < 51200  # CloudFormation's inline template limit


def test_plan_roles_reports_each_role(fake):
    fake.on("iam", "get_role", role(ARNS["operator"]))
    fake.denied("iam", "get_role", "NoSuchEntity")
    fake.denied("iam", "get_role", "AccessDenied")
    assert plan_roles(fake, ARNS) == {"operator": "found", "execution": "missing", "ui": "unknown"}


def role(arn):
    return {
        "Role": {
            "Path": "/",
            "RoleName": arn.rsplit("/", 1)[-1],
            "RoleId": "AROAEXAMPLE1234567",
            "Arn": arn,
            "CreateDate": "2026-01-01T00:00:00Z",
        }
    }


def test_a_new_stack_is_created_and_watched_until_complete(fake, spec):
    fake.denied("cloudformation", "describe_stacks", "ValidationError", MISSING)
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
        {
            "StackName": STACK,
            "TemplateBody": ANY,
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "Tags": [OWNER_TAG],
            "OnFailure": "ROLLBACK",
        },
    )
    fake.on("cloudformation", "describe_stack_events", event(1))
    fake.on("cloudformation", "describe_stacks", stack("CREATE_IN_PROGRESS"))
    fake.on(
        "cloudformation",
        "describe_stack_events",
        {"StackEvents": event(2, "OperatorRole", "CREATE_COMPLETE")["StackEvents"] + event(1)["StackEvents"]},
    )
    fake.on("cloudformation", "describe_stacks", stack("CREATE_COMPLETE"))
    result, output = run(fake, spec)
    assert result == "created" and "OperatorRole: CREATE_COMPLETE" in output
    fake.done()


def test_an_existing_complete_stack_is_left_alone(fake, spec):
    fake.on("cloudformation", "describe_stacks", stack("UPDATE_COMPLETE"))
    assert run(fake, spec)[0] == "exists"
    fake.done()


def test_a_failed_creation_reports_the_reason_and_that_nothing_was_left(fake, spec):
    fake.denied("cloudformation", "describe_stacks", "ValidationError", MISSING)
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    fake.on(
        "cloudformation",
        "describe_stack_events",
        event(1, "OperatorRole", "CREATE_FAILED", "not authorized to iam:CreateRole"),
    )
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_IN_PROGRESS"))
    fake.on(
        "cloudformation",
        "describe_stack_events",
        event(1, "OperatorRole", "CREATE_FAILED", "not authorized to iam:CreateRole"),
    )
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_COMPLETE"))
    fake.on(
        "cloudformation",
        "describe_stack_events",
        event(1, "OperatorRole", "CREATE_FAILED", "not authorized to iam:CreateRole"),
    )
    with pytest.raises(RolesError) as error:
        run(fake, spec)
    message = str(error.value)
    assert "not authorized to iam:CreateRole" in message and "no roles were left behind" in message
    fake.done()


def test_a_rolled_back_stack_that_this_tool_made_is_removed_after_consent(fake, spec):
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_COMPLETE"))
    fake.on("cloudformation", "delete_stack", {}, {"StackName": STACK})
    fake.on("cloudformation", "describe_stacks", stack("DELETE_IN_PROGRESS"))
    fake.denied("cloudformation", "describe_stacks", "ValidationError", MISSING)
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    fake.on("cloudformation", "describe_stack_events", event(1))
    fake.on("cloudformation", "describe_stacks", stack("CREATE_COMPLETE"))
    result, output = run(fake, spec, ScriptedPrompter({"reset-iam-stack": True}))
    assert result == "created" and "Removed the failed stack" in output
    fake.done()


def test_a_rolled_back_stack_without_our_tag_is_never_deleted(fake, spec):
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_COMPLETE", tagged=False))
    with pytest.raises(RolesError, match="not created by this tool"):
        run(fake, spec, ScriptedPrompter({"reset-iam-stack": True}))
    fake.done()


def test_declining_the_cleanup_leaves_the_stack(fake, spec):
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_COMPLETE"))
    with pytest.raises(RolesError, match="Left the failed stack"):
        run(fake, spec, ScriptedPrompter({"reset-iam-stack": False}))
    fake.done()


def test_cleanup_needs_the_iam_flag_without_a_person(fake, spec):
    fake.on("cloudformation", "describe_stacks", stack("ROLLBACK_COMPLETE"))
    from infra.guided.prompt import Refused

    with pytest.raises(Refused, match="--allow-iam-bootstrap"):
        run(fake, spec, ScriptedPrompter({}))


@pytest.mark.parametrize(
    "status", ["CREATE_FAILED", "ROLLBACK_FAILED", "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED"]
)
def test_stacks_in_other_failed_states_are_not_touched(fake, spec, status):
    fake.on("cloudformation", "describe_stacks", stack(status))
    with pytest.raises(RolesError, match="Nothing was changed"):
        run(fake, spec)
    fake.done()


def test_access_denied_explains_what_to_do(fake, spec):
    fake.denied("cloudformation", "describe_stacks", "AccessDenied")
    with pytest.raises(RolesError, match="administrator"):
        run(fake, spec)


def test_a_slow_stack_times_out_without_being_cancelled(fake, spec):
    fake.denied("cloudformation", "describe_stacks", "ValidationError", MISSING)
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    for _ in range(3):
        fake.on("cloudformation", "describe_stack_events", event(1))
        fake.on("cloudformation", "describe_stacks", stack("CREATE_IN_PROGRESS"))
    with pytest.raises(RolesError, match="keeps running in AWS"):
        run(fake, spec, timeout=2)


def test_verify_operator_waits_for_iam_then_succeeds():
    calls = iter(
        [
            ClientError({"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity"),
            {"Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/tracaus-staging-operator/s"},
        ]
    )

    def identity():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    out_console, out = console()
    verify_operator(identity, ARNS["operator"], out_console, delay=0, sleep=lambda _: None)
    assert "Waiting for IAM" in out.getvalue()


def test_verify_operator_rejects_the_wrong_role_at_once():
    out_console, _ = console()
    with pytest.raises(RolesError, match="does not assume"):
        verify_operator(
            lambda: {"Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/other/s"},
            ARNS["operator"],
            out_console,
            sleep=lambda _: None,
        )


def test_verify_operator_gives_up_with_the_reason():
    def identity():
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity")

    out_console, _ = console()
    with pytest.raises(RolesError, match="AccessDenied"):
        verify_operator(identity, ARNS["operator"], out_console, attempts=2, sleep=lambda _: None)


def block(name="tracaus-operator", arn=None):
    return profile_block(name, arn or ARNS["operator"], "my-sso", REGION)


@pytest.mark.parametrize(
    "args",
    [
        ("bad name", ARNS["operator"], "src", REGION),
        ("default", ARNS["operator"], "src", REGION),
        ("ok", ARNS["operator"] + "\nrole_arn = evil", "src", REGION),
        ("ok", "arn:aws:iam::1:role/x", "src", REGION),
        ("ok", ARNS["operator"], "src]\n[profile x", REGION),
        ("ok", ARNS["operator"], "src", "nowhere"),
    ],
)
def test_profile_values_cannot_inject_lines_or_sections(args):
    with pytest.raises(RolesError):
        profile_block(*args)


def test_profiles_are_appended_in_a_marked_block_with_a_backup(tmp_path):
    path = tmp_path / "config"
    path.write_text("[default]\nregion = eu-west-1\n")
    os.chmod(path, 0o640)
    result = write_profiles(path, [block()])
    text = path.read_text()
    assert result["written"] == ["tracaus-operator"] and result["backup"]
    assert "# >>> tracaus:tracaus-operator >>>" in text and "source_profile = my-sso" in text
    assert text.startswith("[default]\nregion = eu-west-1\n")
    assert open(result["backup"]).read() == "[default]\nregion = eu-west-1\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_writing_twice_changes_nothing_the_second_time(tmp_path):
    path = tmp_path / "config"
    write_profiles(path, [block()])
    before = path.read_text()
    again = write_profiles(path, [block()])
    assert again["unchanged"] == ["tracaus-operator"] and not again["written"] and again["backup"] is None
    assert path.read_text() == before


def test_a_changed_role_updates_only_our_block(tmp_path):
    path = tmp_path / "config"
    path.write_text("[profile mine]\nregion = x-y-1\n")
    write_profiles(path, [block()])
    write_profiles(path, [block(arn=ARNS["operator"] + "2")])
    text = path.read_text()
    assert text.count("[profile tracaus-operator]") == 1 and "operator2" in text
    assert "[profile mine]\nregion = x-y-1\n" in text


def test_an_existing_unmarked_profile_is_never_overwritten(tmp_path):
    path = tmp_path / "config"
    path.write_text("[profile tracaus-operator]\nrole_arn = arn:aws:iam::1:role/theirs\n")
    result = write_profiles(path, [block()])
    assert result["skipped"] == ["tracaus-operator"] and not result["written"]
    assert "theirs" in path.read_text()


def test_a_missing_config_file_is_created_private(tmp_path):
    path = tmp_path / ".aws" / "config"
    result = write_profiles(path, [block()])
    assert result["backup"] is None and stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_original_is_restored_when_the_result_does_not_read_back(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text("[default]\nregion = eu-west-1\n")
    monkeypatch.setattr(guided_roles, "_matches", lambda *a: False)
    with pytest.raises(RolesError, match="restored"):
        write_profiles(path, [block()])
    assert path.read_text() == "[default]\nregion = eu-west-1\n"


def test_a_symlinked_config_is_written_through_and_stays_a_link(tmp_path):
    real = tmp_path / "dotfiles" / "aws-config"
    real.parent.mkdir()
    real.write_text("[default]\nregion = eu-west-1\n")
    link = tmp_path / "config"
    link.symlink_to(real)
    write_profiles(link, [block()])
    assert link.is_symlink() and "tracaus:tracaus-operator" in real.read_text()


def test_the_config_really_parses_with_botocore(tmp_path):
    from botocore.configloader import load_config

    path = tmp_path / "config"
    write_profiles(path, [block(), profile_block("tracaus-ui", ARNS["ui"], "my-sso", REGION)])
    profiles = load_config(str(path))["profiles"]
    assert profiles["tracaus-operator"]["role_arn"] == ARNS["operator"]
    assert profiles["tracaus-ui"]["source_profile"] == "my-sso"


def test_a_profile_cannot_be_its_own_source():
    with pytest.raises(RolesError, match="own profile"):
        profile_block("tracaus-staging-operator", ARNS["operator"], "tracaus-staging-operator", REGION)


def test_a_header_with_a_trailing_comment_is_an_existing_profile_and_the_file_is_untouched(tmp_path):
    path = tmp_path / "config"
    path.write_text("[profile tracaus-operator]  # mine\nrole_arn = arn:aws:iam::1:role/theirs\n")
    result = write_profiles(path, [block()])
    assert result["skipped"] == ["tracaus-operator"] and not list(tmp_path.glob("config.tracaus-backup*"))


def test_a_restored_edit_leaves_no_stray_backup(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text("[default]\nregion = eu-west-1\n")
    monkeypatch.setattr(guided_roles, "_matches", lambda *a: False)
    with pytest.raises(RolesError):
        write_profiles(path, [block()])
    assert not list(tmp_path.glob("config.tracaus-backup*"))


def test_the_read_back_requires_the_profile_to_be_present(tmp_path):
    path = tmp_path / "config"
    path.write_text("[default]\nregion = eu-west-1\n")
    assert guided_roles._matches(path, [block()]) is False


def test_an_operator_in_another_account_is_not_accepted():
    out_console, _ = console()
    other = "arn:aws:sts::999999999999:assumed-role/tracaus-staging-operator/s"
    with pytest.raises(RolesError, match="does not assume"):
        verify_operator(lambda: {"Arn": other}, ARNS["operator"], out_console, sleep=lambda _: None)


def test_an_error_after_the_stack_was_requested_never_claims_nothing_was_created(fake, spec):
    fake.denied("cloudformation", "describe_stacks", "ValidationError", MISSING)
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    fake.denied("cloudformation", "describe_stack_events", "Throttling")
    fake.denied("cloudformation", "describe_stacks", "Throttling")
    with pytest.raises(RolesError) as error:
        run(fake, spec)
    assert "may still be creating" in str(error.value) and "Nothing was created" not in str(error.value)
