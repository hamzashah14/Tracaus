"""`setup` end to end on stubbed AWS: discovery, questions, files, roles, profile and plan."""

import io
import json
import os
import stat

import pytest

from infra.guided.common import GuidedError
from infra.guided.console import Console
from infra.guided.prompt import AnswersPrompter, NeedsAnswer, Refused, ScriptedPrompter
from infra.guided.roles import OWNER_TAG
from infra.guided.setup import Options, Setup
from tests.guided_fakes import ACCOUNT, REGION, Fake

ID1 = "i-0123456789abcdef0"
LB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/orders-alb/50dc6c495c0c9188"
TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/orders/73e2d6bc24d8a067"
PROFILE_ID = "eu.anthropic.model-v1:0"
OPERATOR = f"arn:aws:sts::{ACCOUNT}:assumed-role/tracaus-staging-operator/session"
USER = f"arn:aws:iam::{ACCOUNT}:user/alice"

ANSWERS = {
    "profile": "default",
    "account_id": ACCOUNT,
    "region": REGION,
    "targets": [f"ec2:{ID1}", "ecs:prod/orders-api"],
    f"disk_path:{ID1}": "/",
    f"process_exe:{ID1}": "nginx",
    f"nginx:{ID1}": True,
    f"existing_logs:{ID1}": ["/app/web"],
    "alb:prod/orders-api": True,
    "ecs_logs:prod/orders-api": ["/ecs/orders"],
    "project": "tracaus",
    "environment": "staging",
    "release_id": "r001",
    "emails": "ops@example.com",
    "status_base_url": "https://localhost:8501/",
    "model_provider": "bedrock",
    "bedrock_region": REGION,
    "model_choice": PROFILE_ID,
    "write_profiles": True,
}


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


ROLE_ARNS = [f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-{n}" for n in ("operator", "cfn-execution", "ui")]


def prime(fake, *, windows=False, name="web-1", roles="found"):
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    instances = [{"InstanceId": ID1, "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": name}]}]
    if windows:
        instances.append(
            {"InstanceId": "i-0fedcba9876543210", "State": {"Name": "running"}, "Platform": "windows"}
        )
    fake.on("ec2", "describe_instances", {"Reservations": [{"Instances": instances}]})
    fake.on("ecs", "list_clusters", {"clusterArns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/prod"]})
    fake.on(
        "ecs",
        "describe_clusters",
        {
            "clusters": [
                {"clusterName": "prod", "settings": [{"name": "containerInsights", "value": "enabled"}]}
            ]
        },
    )
    fake.on(
        "ecs", "list_services", {"serviceArns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/prod/orders-api"]}
    )
    fake.on(
        "ecs",
        "describe_services",
        {
            "services": [
                {
                    "serviceName": "orders-api",
                    "status": "ACTIVE",
                    "taskDefinition": "orders:1",
                    "launchType": "FARGATE",
                    "loadBalancers": [{"targetGroupArn": TG}],
                }
            ]
        },
    )
    fake.on(
        "ecs",
        "describe_task_definition",
        {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "app",
                        "logConfiguration": {
                            "logDriver": "awslogs",
                            "options": {"awslogs-group": "/ecs/orders"},
                        },
                    }
                ]
            }
        },
    )
    fake.on(
        "elbv2",
        "describe_target_groups",
        {"TargetGroups": [{"TargetGroupArn": TG, "LoadBalancerArns": [LB]}]},
    )
    dims = [{"Name": "InstanceId", "Value": ID1}]
    fake.on(
        "cloudwatch",
        "list_metrics",
        {
            "Metrics": [
                {"Namespace": "CWAgent", "MetricName": "mem_used_percent", "Dimensions": dims},
                {
                    "Namespace": "CWAgent",
                    "MetricName": "disk_used_percent",
                    "Dimensions": dims + [{"Name": "path", "Value": "/"}],
                },
                {
                    "Namespace": "CWAgent",
                    "MetricName": "procstat_lookup_pid_count",
                    "Dimensions": dims
                    + [{"Name": "exe", "Value": "nginx"}, {"Name": "pid_finder", "Value": "native"}],
                },
            ]
        },
    )
    fake.on("logs", "describe_log_groups", {"logGroups": [{"logGroupName": "/app/web"}]})
    fake.on("logs", "describe_log_streams", {"logStreams": [{"logStreamName": ID1}]})
    fake.on("logs", "describe_log_groups", {"logGroups": [{"logGroupName": "/ecs/orders"}]})
    fake.on(
        "bedrock",
        "list_inference_profiles",
        {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileName": "Model",
                    "inferenceProfileArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{PROFILE_ID}",
                    "models": [
                        {"modelArn": f"arn:aws:bedrock:{REGION}::foundation-model/anthropic.model-v1:0"}
                    ],
                    "inferenceProfileId": PROFILE_ID,
                    "status": "ACTIVE",
                    "type": "SYSTEM_DEFINED",
                }
            ]
        },
    )
    for arn in ROLE_ARNS if roles != "skip" else []:
        if roles == "found":
            fake.on("iam", "get_role", role(arn))
        else:
            fake.denied("iam", "get_role", "NoSuchEntity")


class Runner:
    """Stands in for subprocesses: git answers, the dry-run summary, and a record of every call."""

    def __init__(self, *, dirty="", repo=True, dry=None):
        self.calls, self.dirty, self.repo = [], dirty, repo
        self.dry = dry or (
            0,
            json.dumps(
                {"plan_hash": "ab" * 32, "stages": ["foundation-tools", "durable-runtime", "routing"]}
            ),
        )

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return (0, "true") if self.repo else (128, "not a repo")
        if argv[:2] == ["git", "status"]:
            return 0, self.dirty
        if "dry-run" in argv:
            return self.dry
        raise AssertionError(f"unexpected program {argv}")


def identity_of(account, arn):
    def factory(profile):
        return lambda: {"Account": account, "UserId": "U", "Arn": arn}

    return factory


operator_identity = identity_of(ACCOUNT, OPERATOR)


def make(
    tmp_path, answers=None, *, runner=None, prompter=None, identity=operator_identity, allowed=(), **options
):
    fake = Fake()
    out = io.StringIO()
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    console = Console(out, work / "guided.log")
    prompter = prompter or ScriptedPrompter({**ANSWERS, **(answers or {})}, allowed)
    setup = Setup(
        console,
        prompter,
        work,
        Options(skip_install=True, **options),
        root=tmp_path,
        clients_for=lambda profile: fake,
        identity_for=identity,
        profiles_for=lambda: ["default", "admin"],
        region_for=lambda profile: REGION,
        runner=runner or Runner(),
        config_path=tmp_path / "aws-config",
        sleep=lambda _: None,
    )
    return setup, fake, out, work


def test_a_full_run_writes_everything_and_tells_the_user_each_step(tmp_path):
    setup, fake, out, work = make(tmp_path)
    prime(fake)
    assert setup.run() == 0
    text = out.getvalue()
    for number in range(1, 10):
        assert f"[{number}/9]" in text
    for name in (
        "deployment.json",
        "runtime.json",
        "automation.json",
        "answers.json",
        "discovery.json",
        "plan.hash",
    ):
        assert (work / name).exists()
        assert stat.S_IMODE(os.stat(work / name).st_mode) == 0o600
    spec = json.loads((work / "deployment.json").read_text())
    assert [i["id"] for i in spec["instances"]] == [ID1]
    assert spec["ecs_services"][0]["load_balancer"]["target_group_arn"] == TG
    assert spec["model_id"] == PROFILE_ID and spec["account_id"] == ACCOUNT
    assert "Routing to Tracaus starts only at 'routing'" in text
    assert "Next: ./bin/tracaus deploy" in text
    assert "[profile tracaus-staging-operator]" in (tmp_path / "aws-config").read_text()
    fake.done()


def test_answers_never_hold_secrets_or_the_admin_profile(tmp_path):
    setup, fake, _, work = make(tmp_path)
    prime(fake)
    setup.run()
    answers = json.loads((work / "answers.json").read_text())
    assert "admin_profile" not in answers and not any("password" in k or "secret" in k for k in answers)
    assert answers["emails"] == ["ops@example.com"]


def test_the_answers_file_replays_the_same_run(tmp_path):
    setup, fake, _, work = make(tmp_path)
    prime(fake)
    setup.run()
    first = (work / "deployment.json").read_text()
    replay, fake2, _, work2 = make(tmp_path / "again", prompter=AnswersPrompter(work / "answers.json"))
    prime(fake2)
    assert replay.run() == 0
    assert (work2 / "deployment.json").read_text() == first


def test_a_rerun_keeps_the_previous_settings(tmp_path):
    setup, fake, _, work = make(tmp_path)
    prime(fake)
    setup.run()
    again, fake2, _, _ = make(tmp_path, {"release_id": "r002"})
    prime(fake2)
    again.run()
    assert json.loads((work / "deployment.json.previous").read_text())["release_id"] == "r001"
    assert json.loads((work / "deployment.json").read_text())["release_id"] == "r002"


def test_text_from_aws_cannot_reach_the_terminal_raw(tmp_path):
    setup, fake, out, work = make(tmp_path)
    prime(fake, name="web\x1b[2J\x1b]0;pwned\x07-1")
    setup.run()
    assert "\x1b" not in out.getvalue() and "\x07" not in out.getvalue()
    assert "\x1b" not in (work / "guided.log").read_text()


def test_windows_instances_are_left_out_with_a_note(tmp_path):
    setup, fake, out, _ = make(tmp_path)
    prime(fake, windows=True)
    setup.run()
    assert "Windows instance and is not supported" in out.getvalue()


def test_nothing_found_stops_cleanly_and_writes_nothing(tmp_path):
    setup, fake, _, work = make(tmp_path)
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    fake.on("ec2", "describe_instances", {"Reservations": []})
    fake.on("ecs", "list_clusters", {"clusterArns": []})
    with pytest.raises(GuidedError, match="No EC2 instance or ECS service"):
        setup.run()
    assert not (work / "deployment.json").exists()


def test_both_services_denied_stops_with_what_to_grant(tmp_path):
    setup, fake, _, _ = make(tmp_path)
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    fake.denied("ec2", "describe_instances", "UnauthorizedOperation")
    fake.denied("ecs", "list_clusters", "AccessDeniedException")
    with pytest.raises(GuidedError, match="Could not read EC2 or ECS") as error:
        setup.run()
    assert "ec2:DescribeInstances" in error.value.next_step


def test_one_denied_service_is_left_out_and_the_run_continues(tmp_path):
    setup, fake, out, work = make(tmp_path, {"targets": [f"ec2:{ID1}"]})
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    fake.on(
        "ec2",
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": ID1, "State": {"Name": "running"}}]}]},
    )
    fake.denied("ecs", "list_clusters", "AccessDeniedException")
    prime_rest = prime  # reuse the CloudWatch, logs, Bedrock and IAM stubs only
    fake2 = Fake()
    prime_rest(fake2)
    for service in ("cloudwatch", "logs", "bedrock", "iam"):
        fake.stubs.setdefault(service, None)
    # copy the primed queues for the services this scenario still needs
    for service in ("cloudwatch", "logs", "bedrock", "iam"):
        fake.real[service] = fake2.real[service]
        fake.stubs[service] = fake2.stubs[service]
    assert setup.run() == 0
    assert "Could not read ECS services (AccessDeniedException)" in out.getvalue()


def test_a_profile_from_another_account_is_refused(tmp_path):
    setup, fake, _, work = make(tmp_path, {"account_id": "999999999999"})
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    with pytest.raises(GuidedError) as error:
        setup.run()
    assert error.value.code == 3 and not (work / "deployment.json").exists()


def test_missing_roles_are_created_after_a_typed_yes(tmp_path):
    setup, fake, out, work = make(tmp_path, {"admin_profile": "admin", "create_roles": True})
    prime(fake, roles="missing")
    fake.denied(
        "cloudformation",
        "describe_stacks",
        "ValidationError",
        "Stack with id tracaus-staging-iam does not exist",
    )
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    fake.on(
        "cloudformation",
        "describe_stack_events",
        {
            "StackEvents": [
                {
                    "StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1",
                    "EventId": "e1",
                    "StackName": "s",
                    "Timestamp": "2026-01-01T00:00:00Z",
                    "LogicalResourceId": "OperatorRole",
                    "ResourceStatus": "CREATE_COMPLETE",
                }
            ]
        },
    )
    fake.on(
        "cloudformation",
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "s",
                    "CreationTime": "2026-01-01T00:00:00Z",
                    "StackStatus": "CREATE_COMPLETE",
                    "Tags": [OWNER_TAG],
                }
            ]
        },
    )
    for arn in ROLE_ARNS:
        fake.on("iam", "get_role", role(arn))
    assert setup.run() == 0
    assert "only step that needs administrator-level IAM rights" in out.getvalue()
    assert (work / "iam.template.json").exists()
    assert "admin_profile" not in json.loads((work / "answers.json").read_text())
    fake.done()


def test_the_admin_must_be_in_the_same_account(tmp_path):
    other = identity_of("999999999999", "arn:aws:iam::999999999999:user/x")
    setup, fake, _, _ = make(tmp_path, {"admin_profile": "admin", "create_roles": True}, identity=other)
    prime(fake, roles="missing")
    with pytest.raises(GuidedError) as error:
        setup.run()
    assert error.value.code == 3 and "different account" in error.value.what


def test_declining_the_role_step_prints_the_command_and_waits(tmp_path):
    setup, fake, out, work = make(
        tmp_path, {"admin_profile": "admin", "create_roles": False, "roles_check": False}
    )
    prime(fake, roles="missing")
    with pytest.raises(GuidedError) as error:
        setup.run()
    assert error.value.code == 2
    assert "aws cloudformation deploy --template-file" in out.getvalue()
    assert (work / "deployment.json").exists()  # the settings are kept for the next run


def test_non_interactive_mode_will_not_create_roles_without_the_flag(tmp_path):
    answers = {k: v for k, v in ANSWERS.items()}
    setup, fake, _, _ = make(tmp_path, prompter=ScriptedPrompter(answers), admin_profile="admin")
    prime(fake, roles="missing")
    with pytest.raises(Refused, match="--allow-iam-bootstrap"):
        setup.run()


def test_the_flag_allows_it_without_a_person(tmp_path):
    setup, fake, _, _ = make(
        tmp_path, prompter=ScriptedPrompter(ANSWERS, allowed={"iam-bootstrap"}), admin_profile="admin"
    )
    prime(fake, roles="missing")
    fake.denied(
        "cloudformation",
        "describe_stacks",
        "ValidationError",
        "Stack with id tracaus-staging-iam does not exist",
    )
    fake.on(
        "cloudformation",
        "create_stack",
        {"StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1"},
    )
    fake.on(
        "cloudformation",
        "describe_stack_events",
        {
            "StackEvents": [
                {
                    "StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1",
                    "EventId": "e1",
                    "StackName": "s",
                    "Timestamp": "2026-01-01T00:00:00Z",
                    "LogicalResourceId": "OperatorRole",
                    "ResourceStatus": "CREATE_COMPLETE",
                }
            ]
        },
    )
    fake.on(
        "cloudformation",
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "s",
                    "CreationTime": "2026-01-01T00:00:00Z",
                    "StackStatus": "CREATE_COMPLETE",
                    "Tags": [OWNER_TAG],
                }
            ]
        },
    )
    for arn in ROLE_ARNS:
        fake.on("iam", "get_role", role(arn))
    assert setup.run() == 0


def test_partial_roles_without_a_stack_are_not_papered_over(tmp_path):
    setup, fake, _, _ = make(tmp_path)
    prime(fake, roles="skip")
    fake.on("iam", "get_role", role(ROLE_ARNS[0]))
    fake.denied("iam", "get_role", "NoSuchEntity")
    fake.denied("iam", "get_role", "NoSuchEntity")
    fake.denied("cloudformation", "describe_stacks", "ValidationError", "Stack with id x does not exist")
    with pytest.raises(GuidedError, match="Only some of the three roles exist"):
        setup.run()


def test_a_role_under_another_path_is_a_conflict(tmp_path):
    setup, fake, _, _ = make(tmp_path)
    prime(fake, roles="skip")
    fake.on("iam", "get_role", role(f"arn:aws:iam::{ACCOUNT}:role/other/tracaus-staging-operator"))
    fake.on("iam", "get_role", role(ROLE_ARNS[1]))
    fake.on("iam", "get_role", role(ROLE_ARNS[2]))
    with pytest.raises(GuidedError, match="different path"):
        setup.run()


def test_the_operator_profile_check_failure_is_explained(tmp_path):
    bad = identity_of(ACCOUNT, f"arn:aws:sts::{ACCOUNT}:assumed-role/other/s")
    setup, fake, _, _ = make(tmp_path, identity=bad)
    prime(fake)
    with pytest.raises(GuidedError, match="operator profile does not work"):
        setup.run()


def test_a_failed_plan_shows_why_and_changes_nothing_in_aws(tmp_path):
    runner = Runner(dry=(1, "Invalid deployment field: instances.0.disk_path"))
    setup, fake, _, _ = make(tmp_path, runner=runner)
    prime(fake)
    with pytest.raises(GuidedError, match="plan could not be built") as error:
        setup.run()
    assert "disk_path" in error.value.cause


def test_a_dirty_checkout_warns_and_a_missing_repo_stops(tmp_path):
    setup, fake, out, _ = make(tmp_path, runner=Runner(dirty=" M infra/spec.py\n"))
    prime(fake)
    setup.run()
    assert "need" in out.getvalue() and "clean checkout" in out.getvalue()
    setup2, _, _, _ = make(tmp_path / "n", runner=Runner(repo=False))
    with pytest.raises(GuidedError, match="not a Git checkout"):
        setup2.run()


def test_an_interrupt_during_the_questions_leaves_no_settings(tmp_path):
    class Interrupting(ScriptedPrompter):
        def ask(self, key, *args, **kwargs):
            if key == "project":
                raise KeyboardInterrupt
            return super().ask(key, *args, **kwargs)

    setup, fake, _, work = make(tmp_path, prompter=Interrupting(ANSWERS))
    prime(fake)
    with pytest.raises(KeyboardInterrupt):
        setup.run()
    assert not (work / "deployment.json").exists() and not (work / ".staging").exists()


def test_a_missing_answer_stops_with_the_name_of_the_question(tmp_path):
    answers = {k: v for k, v in ANSWERS.items() if k != "emails"}
    setup, fake, _, _ = make(tmp_path, prompter=ScriptedPrompter(answers))
    prime(fake)
    with pytest.raises(NeedsAnswer, match="emails"):
        setup.run()


def test_the_model_api_option_asks_for_the_provider_and_reminds_about_the_secret(tmp_path):
    answers = {
        "model_provider": "model_api",
        "model_protocol": "openai",
        "model_base_url": "https://api.example.com/v1",
        "model_id": "model-x",
    }
    setup, fake, out, work = make(tmp_path, answers)
    prime(fake)
    setup.run()
    spec = json.loads((work / "deployment.json").read_text())
    assert spec["model_provider"] == "model_api" and "model_arns" not in spec
    assert "model-api-key" in out.getvalue()


def test_roles_that_cannot_all_be_read_ask_instead_of_guessing(tmp_path):
    setup, fake, _, _ = make(tmp_path, {"roles_exist": True})
    prime(fake, roles="skip")
    fake.on("iam", "get_role", role(ROLE_ARNS[0]))
    fake.denied("iam", "get_role", "AccessDenied")
    fake.on("iam", "get_role", role(ROLE_ARNS[2]))
    assert setup.run() == 0


def test_answers_from_an_earlier_run_survive_a_run_that_stops_early(tmp_path):
    setup, fake, _, work = make(tmp_path)
    (work / "answers.json").write_text(json.dumps({"emails": ["old@example.com"], "keep": 1}))
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": USER})
    fake.on("ec2", "describe_instances", {"Reservations": []})
    fake.on("ecs", "list_clusters", {"clusterArns": []})
    with pytest.raises(GuidedError):
        setup.run()
    saved = json.loads((work / "answers.json").read_text())
    assert saved["keep"] == 1 and saved["emails"] == ["old@example.com"] and saved["profile"] == "default"


def test_a_started_deployment_keeps_its_answers(tmp_path):
    setup, fake, _, work = make(tmp_path, {"profile": "default"})
    (work / "answers.json").write_text(json.dumps({"profile": "original"}))
    (work / "state.json").write_text("{}")
    prime(fake)
    with pytest.raises(GuidedError):
        setup.run()
    assert json.loads((work / "answers.json").read_text()) == {"profile": "original"}


def test_a_log_group_already_given_to_another_service_is_left_out_with_a_note(tmp_path):
    setup, _, out, _ = make(tmp_path)
    setup.claimed_groups = {"/ecs/shared"}
    item = {
        "cluster": "prod",
        "service": "blue",
        "launch_type": "FARGATE",
        "container_insights": False,
        "log_groups": ["/ecs/shared"],
        "other_log_drivers": [],
        "target_groups": [],
        "notes": [],
        "load_balancer": "none",
        "alb": None,
    }
    setup.prompter = ScriptedPrompter({"alb:prod/blue": False})
    entry = setup._ask_service(item, set())
    assert entry["log_groups"] == [] and "already used by another selected service" in out.getvalue()


def test_the_wheelhouse_marker_decides_whether_to_download(tmp_path):
    runner = Runner()
    calls = []

    def recording(argv, **kwargs):
        calls.append(argv)
        return (0, "") if "pip" in argv else runner(argv, **kwargs)

    for marker, expected in ((False, True), (True, False)):
        folder = tmp_path / str(marker)
        (folder / ".build" / "wheels").mkdir(parents=True)
        (folder / ".build" / "wheels" / "x.whl").write_text("partial")
        if marker:
            (folder / ".build" / "wheels" / ".complete").write_text("ok")
        calls.clear()
        setup, fake, _, _ = make(folder, runner=recording)
        setup.options.skip_install = False
        setup.root = folder
        setup.step_computer()
        assert any("pip" in c for c in calls) is expected
