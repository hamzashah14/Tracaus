"""`teardown` against an in-memory account (tests/aws_world.py): the order, what survives, and above all what
happens when something cannot be deleted."""

import io
import json
import re

import pytest

from infra.guided import __main__ as cli
from infra.guided import roles as guided_roles
from infra.guided.common import GuidedError
from infra.guided.console import Console
from infra.guided.prompt import AnswersPrompter, ConsolePrompter, Refused, YesPrompter
from infra.guided.teardown import Options, Teardown
from tests.aws_world import ACCOUNT, KEY, SPEC, World, bystanders, deploy, resource
from tests.guided_fakes import ScriptedPrompter

NAME = "tracaus-test-staging"
P = NAME


class Clock:
    """Every reading is one second later, so the timeouts end quickly without sleeping."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def profile_file(tmp_path):
    path = tmp_path / "aws-config"
    path.write_text(
        "[profile mine]\nregion = eu-central-1\n\n"
        + "\n".join(
            f"# >>> tracaus:{NAME}-{n} >>>\n[profile {NAME}-{n}]\nrole_arn = x\n# <<< tracaus:{NAME}-{n} <<<\n"
            for n in ("operator", "ui-principal", "ui")
        )
    )
    return path


def make(tmp_path, world=None, *, options=None, prompter=None, account=ACCOUNT):
    world = world if world is not None else deploy(World())
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "deployment.json").write_text(json.dumps(SPEC))
    (work / "state.json").write_text("{}")
    (work / "ui-connection.json").write_text("{}")
    out = io.StringIO()
    config = profile_file(tmp_path)
    teardown = Teardown(
        Console(out),
        prompter or ScriptedPrompter({}),
        work,
        options or Options(admin_profile="admin", confirm=NAME),
        clients_for=lambda profile: world,
        identity_for=lambda profile: (
            lambda: {"Account": account, "Arn": f"arn:aws:iam::{account}:user/admin"}
        ),
        profiles_for=lambda: ["admin", "mine"],
        config_path=config,
        sleep=lambda seconds: None,
        clock=Clock(),
    )
    return teardown, world, work, out, config


def stack_status(world, name):
    return next(s["status"] for s in world.stacks.values() if s["name"] == name)


def order_of_stack_deletes(world):
    names = {s["id"]: s["name"] for s in world.stacks.values()}
    seen = []
    for stack in world.deleted("delete_stack"):
        name = names[stack["StackName"]]
        if name not in seen:
            seen.append(name)
    return seen


# ---------------------------------------------------------------------- the happy path


def test_full_teardown_removes_everything_in_order(tmp_path):
    teardown, world, work, out, config = make(tmp_path, bystanders(deploy(World())))
    assert teardown.run() == 0
    # Stacks, newest first, the role stack last.
    assert order_of_stack_deletes(world) == [
        f"{P}-routing",
        f"{P}-r1-durable-runtime",
        f"{P}-r1-owned-tools",
        f"{P}-durable-foundation",
        f"{P}-foundation-tools",
        f"{P}-iam",
    ]
    # What CloudFormation keeps is gone too.
    assert not [b for b in world.buckets if b != "my-own-bucket"]
    assert not world.tables and not world.queues and not world.topics and not world.secrets
    assert not world.functions and not world.roles
    assert world.groups == {"/my/own/application"}
    # The key is only scheduled, for the AWS minimum.
    assert world.keys[KEY]["KeyState"] == "PendingDeletion"
    assert world.deleted("schedule_key_deletion")[0]["PendingWindowInDays"] == 7
    assert "scheduled" in out.getvalue() or "2026-10-19" in out.getvalue()
    # Termination protection was switched off first, and the cursor secret has no recovery window.
    assert world.deleted("update_termination_protection")[0]["EnableTerminationProtection"] is False
    assert world.deleted("delete_secret")[0]["ForceDeleteWithoutRecovery"] is True
    # 2,500 versions and markers were removed in three batches; every S3 call named the expected owner.
    batches = [kw for kw in world.deleted("delete_objects") if kw["Bucket"].endswith("-reports")]
    assert [len(kw["Delete"]["Objects"]) for kw in batches] == [1000, 1000, 500]
    assert all(
        kw["ExpectedBucketOwner"] == ACCOUNT
        for kw in world.deleted("delete_objects") + world.deleted("delete_bucket")
    )
    # Local clean-up: the marked profiles are gone, the user's own profile stays, the journal is set aside.
    text = config.read_text()
    assert "[profile mine]" in text and "tracaus:" not in text
    assert not (work / "state.json").exists() and list(work.glob("state.json.torn-down-*"))
    assert not (work / "teardown.json").exists()
    assert "Teardown finished" in out.getvalue() and "MANUAL" not in out.getvalue()


def test_things_that_are_not_tracaus_are_never_touched(tmp_path):
    teardown, world, *_ = make(tmp_path, bystanders(deploy(World())))
    teardown.run()
    assert "my-own-bucket" in world.buckets and world.buckets["my-own-bucket"]["objects"]
    assert stack_status(world, "my-app") == "CREATE_COMPLETE"
    assert stack_status(world, "other-prod-routing") == "CREATE_COMPLETE"
    assert stack_status(world, "lookalike") == "CREATE_COMPLETE"
    touched = {kw.get("Bucket") for kw in world.deleted("delete_objects")}
    assert "my-own-bucket" not in touched


def test_split_regions_use_each_resources_own_region(tmp_path):
    teardown, world, work, *_ = make(tmp_path, deploy(World(), tools_region="us-east-1"))
    (work / "deployment.json").write_text(json.dumps({**SPEC, "bedrock_region": "us-east-1"}))
    assert teardown.run() == 0
    tools_bucket_calls = [
        c for c in world.calls if c[2] == "delete_bucket" and c[3]["Bucket"].endswith("-tools")
    ]
    assert tools_bucket_calls[0][1] == "us-east-1"
    assert not world.buckets


def test_nothing_found_is_a_clean_no_op(tmp_path):
    teardown, world, work, out, _ = make(tmp_path, World())
    assert teardown.run() == 0
    assert not world.deleted("delete_stack")
    assert "Nothing to delete" in out.getvalue()
    assert (work / "state.json").exists()


def test_dry_run_shows_the_plan_and_deletes_nothing(tmp_path):
    teardown, world, work, out, config = make(tmp_path, options=Options(admin_profile="admin", dry_run=True))
    assert teardown.run() == 0
    text = out.getvalue()
    for expected in (
        f"{P}-routing",
        "DynamoDB table",
        "incident database",
        "KMS key",
        "Dry run",
        "Not touched",
    ):
        assert expected in text
    assert not world.deleted("delete_stack") and not world.deleted("delete_objects")
    assert (work / "state.json").exists() and "tracaus:" in config.read_text()


# ---------------------------------------------------------------------- confirmation and identity


def test_a_wrong_confirm_value_is_refused_before_anything_is_deleted(tmp_path):
    teardown, world, *_ = make(tmp_path, options=Options(admin_profile="admin", confirm="tracaus-prod"))
    with pytest.raises(Refused):
        teardown.run()
    assert not world.deleted("delete_stack")


def test_typing_the_wrong_name_stops_the_run(tmp_path):
    teardown, world, *_ = make(
        tmp_path,
        options=Options(admin_profile="admin"),
        prompter=ScriptedPrompter({"confirm_name": "yes please"}),
    )
    with pytest.raises(GuidedError) as stopped:
        teardown.run()
    assert stopped.value.code == 3 and "Nothing was deleted" in stopped.value.safe
    assert not world.deleted("delete_stack")


def test_typing_the_exact_name_proceeds(tmp_path):
    teardown, world, *_ = make(
        tmp_path, options=Options(admin_profile="admin"), prompter=ScriptedPrompter({"confirm_name": NAME})
    )
    assert teardown.run() == 0
    assert not world.tables


def test_no_answers_file_or_yes_can_confirm_a_deletion(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"confirm_name": NAME}))
    for prompter in (YesPrompter(), AnswersPrompter(answers)):
        teardown, world, *_ = make(tmp_path, options=Options(admin_profile="admin"), prompter=prompter)
        with pytest.raises(Refused):
            teardown.run()
        assert not world.deleted("delete_stack")


def test_the_terminal_prompter_asks_for_the_name_and_needs_a_terminal(tmp_path):
    out = io.StringIO()

    class Tty(io.StringIO):
        def isatty(self):
            return True

    prompter = ConsolePrompter(Console(out), stdin=Tty(), input_fn=lambda prompt: NAME)
    assert prompter.typed("confirm_name", "Sure?", NAME) is True
    assert (
        ConsolePrompter(Console(out), stdin=Tty(), input_fn=lambda prompt: "nope").typed("k", "Sure?", NAME)
        is False
    )
    with pytest.raises(Exception, match="No terminal"):
        ConsolePrompter(Console(out), stdin=io.StringIO()).typed("k", "Sure?", NAME)


def test_a_profile_from_another_account_is_refused(tmp_path):
    teardown, world, *_ = make(tmp_path, account="999999999999")
    with pytest.raises(GuidedError) as refused:
        teardown.run()
    assert refused.value.code == 3 and "different account" in refused.value.what
    assert not world.calls


def test_a_missing_deployment_file_is_explained(tmp_path):
    teardown, world, work, *_ = make(tmp_path)
    (work / "deployment.json").unlink()
    with pytest.raises(GuidedError, match="no deployment"):
        teardown.run()
    assert not world.calls


# ---------------------------------------------------------------------- things that go wrong


def test_a_stack_that_is_still_changing_is_waited_for(tmp_path):
    world = deploy(World())
    stack = next(s for s in world.stacks.values() if s["name"].endswith("-routing"))
    stack.update(status="UPDATE_IN_PROGRESS", final="UPDATE_COMPLETE", ticks=2)
    teardown, world, *_ = make(tmp_path, world)
    assert teardown.run() == 0
    assert stack_status(world, f"{P}-routing") == "DELETE_COMPLETE"


def test_a_stack_stuck_in_progress_is_reported_not_forced(tmp_path):
    world = deploy(World())
    stack = next(s for s in world.stacks.values() if s["name"].endswith("-routing"))
    stack.update(status="UPDATE_IN_PROGRESS", final="UPDATE_COMPLETE", ticks=10**6)
    teardown, world, work, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert "still changing" in out.getvalue() and "MANUAL REMOVAL NEEDED" in out.getvalue()
    assert (work / "state.json").exists()


def test_a_stack_that_depends_on_another_is_retried_after_it(tmp_path):
    world = deploy(World())
    tools = next(s for s in world.stacks.values() if s["name"].endswith("-owned-tools"))
    tools["blocked_by"] = [f"{P}-durable-foundation"]  # deleted later in the normal order
    teardown, world, *_ = make(tmp_path, world)
    assert teardown.run() == 0
    names = {s["id"]: s["name"] for s in world.stacks.values()}
    tries = [names[kw["StackName"]] for kw in world.deleted("delete_stack")].count(f"{P}-r1-owned-tools")
    assert tries == 2 and stack_status(world, f"{P}-r1-owned-tools") == "DELETE_COMPLETE"


def test_a_resource_cloudformation_cannot_delete_is_kept_listed_and_the_rest_goes(tmp_path):
    world = deploy(World())
    runtime = next(s for s in world.stacks.values() if s["name"].endswith("-durable-runtime"))
    runtime["resources"].append(resource("Odd", "AWS::Weird::Thing", "weird-1"))
    runtime["delete_fails"] = {"Odd": "The thing is in use"}
    teardown, world, work, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    retained = [kw for kw in world.deleted("delete_stack") if kw.get("RetainResources")]
    assert retained and retained[0]["RetainResources"] == ["Odd"]
    assert stack_status(world, f"{P}-r1-durable-runtime") == "DELETE_COMPLETE"
    text = out.getvalue()
    assert (
        "MANUAL REMOVAL NEEDED" in text
        and "AWS::Weird::Thing weird-1" in text
        and "The thing is in use" in text
    )
    assert not world.tables  # the rest was still removed
    # Local files stay so that the run can be repeated.
    assert (work / "state.json").exists() and (work / "teardown.json").exists()


def test_a_stack_that_will_not_delete_keeps_the_role_stack_and_is_listed(tmp_path):
    world = deploy(World())
    routing = next(s for s in world.stacks.values() if s["name"].endswith("-routing"))
    routing["refuse_delete"] = "Stack is in a state that cannot be deleted"
    teardown, world, work, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert stack_status(world, f"{P}-iam") == "CREATE_COMPLETE"
    text = out.getvalue()
    assert "Kept the role stack" in text and f"CloudFormation stack {P}-routing" in text
    assert (work / "state.json").exists()
    assert not world.tables  # independent resources are still removed


def test_object_lock_is_reported_and_the_key_is_not_scheduled(tmp_path):
    world = deploy(World())
    world.object_lock = True
    teardown, world, work, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    text = out.getvalue()
    assert "AccessDenied" in text and "Object Lock" in text
    assert any(b.endswith("-reports") for b in world.buckets)
    assert world.keys[KEY]["KeyState"] == "Enabled"
    assert "would make that unreadable" in text
    assert not world.tables  # the table is independent of the bucket


def test_a_resource_whose_name_is_not_ours_is_not_deleted(tmp_path):
    world = deploy(World())
    foreign = f"https://sqs.eu-central-1.amazonaws.com/{ACCOUNT}/someone-elses-queue"
    world.queues.add(foreign)
    foundation = next(s for s in world.stacks.values() if s["name"].endswith("-durable-foundation"))
    foundation["resources"].append(resource("Odd", "AWS::SQS::Queue", foreign, True))
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert foreign in world.queues and "prefix" in out.getvalue()


def test_a_role_in_an_instance_profile_is_listed_not_deleted(tmp_path):
    world = deploy(World())
    role = next(iter(world.roles))
    world.roles[role]["profiles"] = ["my-instance-profile"]
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert role in world.roles and "my-instance-profile" in out.getvalue()
    assert world.roles[role]["inline"] == ["policy"]  # nothing was stripped from a role that stays


def test_a_bucket_owned_by_someone_else_is_not_emptied(tmp_path):
    world = deploy(World())
    world.account = "999999999999"  # the bucket name now belongs to another account
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert all(b["objects"] for b in world.buckets.values())
    assert not world.deleted("delete_objects") and "403" in out.getvalue()


def test_the_role_stack_is_kept_on_request_with_its_profiles(tmp_path):
    teardown, world, work, out, config = make(
        tmp_path, options=Options(admin_profile="admin", confirm=NAME, keep_roles=True)
    )
    assert teardown.run() == 0
    assert stack_status(world, f"{P}-iam") == "CREATE_COMPLETE"
    assert "tracaus:" in config.read_text()
    assert not world.tables


def test_a_role_stack_that_was_not_made_by_this_tool_is_left_alone_and_listed(tmp_path):
    world = deploy(World())
    iam = next(s for s in world.stacks.values() if s["name"] == f"{P}-iam")
    iam["tags"] = []  # created by an administrator with the printed command
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert stack_status(world, f"{P}-iam") == "CREATE_COMPLETE"
    assert "not created by this tool" in out.getvalue()


def test_a_tagged_resource_that_teardown_did_not_know_is_reported(tmp_path):
    world = deploy(World())
    world.extra_tagged = [f"arn:aws:sqs:eu-central-1:{ACCOUNT}:{P}-mystery-queue"]
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    assert f"{P}-mystery-queue" in out.getvalue() and "MANUAL REMOVAL NEEDED" in out.getvalue()


def test_a_dying_key_does_not_trigger_a_false_leftover_warning(tmp_path):
    teardown, world, *_ = make(tmp_path)
    assert teardown.run() == 0  # the scheduled key still shows in the tag index; it is known, so not reported


# ---------------------------------------------------------------------- interruption and resume


def test_an_expired_sign_in_stops_clearly_and_a_second_run_finishes(tmp_path):
    world = deploy(World())
    world.fail("s3", "delete_objects", "ExpiredToken", times=1)
    teardown, world, work, out, _ = make(tmp_path, world)
    with pytest.raises(GuidedError) as stopped:
        teardown.run()
    assert "sign-in" in stopped.value.what and "continues where it stopped" in stopped.value.next_step
    # The stacks are already gone, so only the saved list says what is left.
    assert (work / "teardown.json").exists()
    world.failures.clear()
    again, *_ = make(tmp_path, world)
    assert again.run() == 0
    assert not world.buckets and not world.tables
    assert world.keys[KEY]["KeyState"] == "PendingDeletion"


def test_a_rerun_after_success_finds_nothing(tmp_path):
    teardown, world, *_ = make(tmp_path)
    assert teardown.run() == 0
    calls = len(world.calls)
    again, world, work, out, _ = make(tmp_path, world)
    assert again.run() == 0
    assert "Nothing to delete" in out.getvalue()
    assert not [c for c in world.calls[calls:] if c[2].startswith("delete")]


def test_an_unexpected_error_says_what_is_safe(tmp_path, monkeypatch):
    teardown, *_ = make(tmp_path)
    monkeypatch.setattr(teardown, "step_kept", lambda: 1 / 0)
    with pytest.raises(GuidedError) as stopped:
        teardown.run()
    assert "stays deleted" in stopped.value.safe and "again" in stopped.value.next_step


# ---------------------------------------------------------------------- AgentCore


def agentcore_world(endpoint_id=None):
    world = deploy(World())
    runtime = "tracaus_test_staging_r1_agentcore-1234567890"
    arn = f"arn:aws:bedrock-agentcore:eu-central-1:{ACCOUNT}:runtime/{runtime}"
    world.runtimes.add(runtime)
    world.endpoints[(runtime, "release_r1")] = "READY"
    world.stack(
        f"{P}-r1-agentcore-endpoint",
        "eu-central-1",
        [
            resource(
                "Endpoint",
                "AWS::BedrockAgentCore::RuntimeEndpoint",
                endpoint_id or f"{arn}/runtime-endpoint/release_r1",
                True,
            ),
            resource("Runtime", "AWS::BedrockAgentCore::Runtime", arn, True),
        ],
        tags=[
            {"Key": "Project", "Value": "tracaus-test"},
            {"Key": "Environment", "Value": "staging"},
            {"Key": "ManagedBy", "Value": "tracaus-cloudformation"},
        ],
    )
    return world


def test_agentcore_endpoint_goes_before_its_runtime(tmp_path):
    teardown, world, *_ = make(tmp_path, agentcore_world())
    assert teardown.run() == 0
    operations = [c[2] for c in world.calls if c[0] == "bedrock-agentcore-control"]
    assert operations.index("delete_agent_runtime_endpoint") < operations.index("delete_agent_runtime")
    assert not world.runtimes and not world.endpoints


def test_an_agentcore_id_in_an_unknown_format_is_listed(tmp_path):
    teardown, world, _, out, _ = make(tmp_path, agentcore_world(endpoint_id="???"))
    assert teardown.run() == 1
    assert "not in a recognised format" in out.getvalue()
    assert any(runtime for runtime in world.runtimes)  # the runtime could not go while the endpoint stays


# ---------------------------------------------------------------------- the profile file


def test_only_the_marked_blocks_are_removed_and_a_backup_is_made(tmp_path):
    path = profile_file(tmp_path)
    names = [f"{NAME}-operator", f"{NAME}-ui-principal", f"{NAME}-ui"]
    result = guided_roles.remove_profiles(path, names)
    assert result["removed"] == names and result["backup"]
    text = path.read_text()
    assert "[profile mine]" in text and "tracaus:" not in text and NAME not in text
    assert "tracaus:" in open(result["backup"]).read()
    assert guided_roles.remove_profiles(path, names)["removed"] == []  # again: nothing to do


def test_a_profile_you_wrote_yourself_is_not_removed(tmp_path):
    path = tmp_path / "config"
    path.write_text(f"[profile {NAME}-ui]\nrole_arn = mine\n")
    assert guided_roles.remove_profiles(path, [f"{NAME}-ui"])["removed"] == []
    assert "role_arn = mine" in path.read_text()
    assert guided_roles.remove_profiles(tmp_path / "missing", ["x"]) == {"removed": [], "backup": None}


# ---------------------------------------------------------------------- the command line


def test_the_command_line_passes_every_option_through(tmp_path, monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, console, prompter, directory, options):
            seen["options"], seen["prompter"] = options, prompter

        def run(self):
            return 0

    monkeypatch.setattr(cli.guided_teardown, "Teardown", Fake)
    args = cli.parser().parse_args(
        ["teardown", "--admin-profile", "root", "--confirm", NAME, "--dry-run", "--keep-roles", "--yes"]
    )
    assert cli.dispatch(args, Console(io.StringIO()), tmp_path) == 0
    options = seen["options"]
    assert (options.admin_profile, options.confirm, options.dry_run, options.keep_roles) == (
        "root",
        NAME,
        True,
        True,
    )
    assert isinstance(seen["prompter"], YesPrompter)


def test_the_launcher_and_usage_know_the_command():
    from pathlib import Path

    text = (Path(cli.ROOT) / "bin" / "tracaus").read_text()
    assert "teardown" in text and re.search(r"setup \| deploy \| status \| ui \| teardown", text)


def test_a_damaged_or_tampered_saved_list_is_ignored_entry_by_entry(tmp_path):
    teardown, world, work, out, _ = make(tmp_path, World())
    key = {
        "type": "AWS::KMS::Key",
        "id": "some-other-key",
        "region": "us-west-2",
        "stack": "x",
    }  # not a Tracaus region
    good = {
        "type": "AWS::SQS::Queue",
        "id": f"https://sqs.eu-central-1.amazonaws.com/{ACCOUNT}/{P}-q",
        "region": "eu-central-1",
        "stack": "x",
    }
    (work / "teardown.json").write_text(
        json.dumps(
            {"name": NAME, "account": ACCOUNT, "items": [key, good, {"type": "AWS::Weird::X"}, "text", None]}
        )
    )
    world.queues.add(good["id"])
    assert teardown.run() == 0
    assert good["id"] not in world.queues
    assert out.getvalue().count("Ignoring an entry") == 4
    assert not world.deleted("schedule_key_deletion")


def test_a_longer_name_does_not_hide_a_leftover_behind_a_handled_one(tmp_path):
    world = deploy(World())
    # Names that merely start with a handled name must still be reported; exact ARNs of handled things must not.
    world.extra_tagged = [
        f"arn:aws:s3:::{P}-{ACCOUNT}-eu-central-1-reports-backup",
        f"arn:aws:sqs:eu-central-1:{ACCOUNT}:{P}-ingress-queue-2",
        f"arn:aws:lambda:eu-central-1:{ACCOUNT}:function:{P}-r1-fetch-logs:7",  # a version of a handled function
    ]
    teardown, world, _, out, _ = make(tmp_path, world)
    assert teardown.run() == 1
    text = out.getvalue()
    assert "reports-backup" in text and "ingress-queue-2" in text
    assert "fetch-logs:7" not in text
