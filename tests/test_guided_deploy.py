"""`deploy` and `status`: progress from the journal, every named stop, and what a failure tells the user."""

import io
import json
import os
import stat

import pytest

from infra.guided.common import GuidedError
from infra.guided.console import Console
from infra.guided.deploy import READY, Deploy, DeployOptions, status
from infra.guided.prompt import Refused, ScriptedPrompter
from tests.guided_fakes import Fake

HASH = "ab" * 32
STAGES = ["foundation-tools", "durable-foundation", "routing"]
PAID = "Candidate ready: resume with --allow-model-invocation"
EMAILS = "Confirm the subscription emails sent to the addresses configured as notification_email, then resume apply"
EXPIRED = "Canary receipt expired/differs; inspect it and explicitly use --retry-canary for new paid work"


def state(*, done=(), working=None, status="RUNNING", nxt="", auto=False, built=True, plan_hash=HASH):
    stages = {s: {"status": "COMPLETE"} for s in done}
    if working:
        stages[working] = {"status": "INTENT"}
    return {
        "version": 1,
        "plan_hash": plan_hash,
        "stages": stages,
        "status": status,
        "next": nxt,
        "auto_resume": auto,
        "built": built,
    }


class Episode:
    """One `apply` run: the journal snapshots seen while it runs, then its exit code and stderr."""

    def __init__(self, snapshots, code, err="", final=None):
        self.snapshots, self.code, self.err, self.final = list(snapshots), code, err, final


class Process:
    def __init__(self, work, episode, stderr, interrupt=False):
        self.work, self.episode, self.stderr, self.interrupt = work, episode, stderr, interrupt
        self.returncode, self.terminated = None, False

    def poll(self):
        if self.interrupt:
            raise KeyboardInterrupt
        if self.episode.snapshots:
            (self.work / "state.json").write_text(json.dumps(self.episode.snapshots.pop(0)))
            return None
        if self.episode.final is not None:
            (self.work / "state.json").write_text(json.dumps(self.episode.final))
        self.stderr.write(self.episode.err.encode())
        self.returncode = self.episode.code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    kill = terminate


class Popen:
    def __init__(self, work, episodes, interrupt=False):
        self.work, self.episodes, self.calls, self.processes, self.interrupt = (
            work,
            list(episodes),
            [],
            [],
            interrupt,
        )

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        process = Process(self.work, self.episodes.pop(0), kwargs["stderr"], self.interrupt)
        self.processes.append(process)
        return process


class Runner:
    def __init__(self, *, dirty="", plan_hash=HASH, check=0, stages=STAGES):
        self.calls, self.dirty, self.plan_hash, self.check, self.stages = [], dirty, plan_hash, check, stages

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "status"]:
            return 0, self.dirty
        if "dry-run" in argv:
            return 0, json.dumps({"plan_hash": self.plan_hash, "stages": self.stages})
        if "check" in argv:
            return self.check, ""
        raise AssertionError(argv)


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "automation.json").write_text(json.dumps({"profile": "tracaus-staging-operator"}))
    (folder / "plan.hash").write_text(json.dumps({"plan_hash": HASH}))
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "stages": STAGES,
                "resources": {
                    "durable-foundation": {"stack": "tracaus-staging-found", "region": "eu-central-1"}
                },
            }
        )
    )
    return folder


def make(work, episodes, answers=None, *, runner=None, allowed=(), options=None, clock=None, interrupt=False):
    out = io.StringIO()
    console = Console(out, work / "guided.log")
    popen = Popen(work, episodes, interrupt)
    fake = Fake()
    kwargs = {"clock": clock} if clock else {}
    deploy = Deploy(
        console,
        ScriptedPrompter(answers or {}, allowed),
        work,
        options or DeployOptions(),
        root=work,
        clients_for=lambda profile: fake,
        popen=popen,
        runner=runner or Runner(),
        sleep=lambda _: None,
        **kwargs,
    )
    return deploy, popen, out, fake


def ready_episode():
    return Episode(
        [state(working="foundation-tools"), state(done=["foundation-tools"], working="durable-foundation")],
        0,
        final=state(done=STAGES, status=READY),
    )


def waiting(message, auto=False, done=()):
    return Episode([], 2, final=state(done=done, status="WAITING", nxt=message, auto=auto))


def failed(err, done=(), working="durable-foundation"):
    return Episode([], 1, err=err, final=state(done=done, working=working, status="FAILED"))


ANSWERS = {"apply": True}


def test_a_clean_run_shows_each_stage_and_ends_ready(work):
    deploy, popen, out, _ = make(work, [ready_episode()], ANSWERS)
    assert deploy.run() == 0
    text = out.getvalue()
    for number in range(1, 6):
        assert f"[{number}/5]" in text
    assert "foundation-tools: working" in text and "durable-foundation: working" in text
    assert "Built the Lambda packages" in text
    argv = popen.calls[0]
    assert "--plan-hash" in argv and HASH in argv and argv[argv.index("--wait-seconds") + 1] == "900"
    assert "Nothing is routed to Tracaus before 'routing'" in text and "./bin/tracaus ui" in text


def test_apply_output_files_are_private_and_the_log_is_redacted(work):
    deploy, _, _, _ = make(work, [ready_episode()], ANSWERS)
    deploy.run()
    assert stat.S_IMODE(os.stat(work / "apply.err").st_mode) == 0o600
    assert "ops@example.com" not in (work / "guided.log").read_text()


def test_the_user_must_agree_before_anything_is_created(work):
    deploy, popen, _, _ = make(work, [ready_episode()], {"apply": False})
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert error.value.code == 2 and popen.calls == []


def test_yes_skips_the_apply_question_but_not_the_paid_one(work):
    deploy, popen, _, _ = make(work, [waiting(PAID)], options=DeployOptions(yes=True))
    with pytest.raises(Refused, match="--allow-paid-canary"):
        deploy.run()
    assert len(popen.calls) == 1


def test_the_paid_canary_needs_an_explicit_yes_then_resumes_with_the_flag(work):
    episodes = [waiting(PAID), ready_episode()]
    deploy, popen, out, _ = make(work, episodes, {**ANSWERS, "paid_canary": True})
    assert deploy.run() == 0
    assert "--allow-model-invocation" not in popen.calls[0] and "--allow-model-invocation" in popen.calls[1]


def test_declining_the_paid_canary_stops_without_losing_anything(work):
    deploy, popen, _, _ = make(work, [waiting(PAID)], {**ANSWERS, "paid_canary": False})
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert error.value.code == 2 and "kept" in error.value.safe and len(popen.calls) == 1


def test_the_flag_allows_the_paid_canary_without_a_person(work):
    deploy, popen, _, _ = make(work, [waiting(PAID), ready_episode()], ANSWERS, allowed={"paid-canary"})
    assert deploy.run() == 0 and "--allow-model-invocation" in popen.calls[1]


def test_subscription_emails_wait_for_the_user_then_resume(work):
    deploy, popen, out, _ = make(work, [waiting(EMAILS), ready_episode()], {**ANSWERS, "emails_done": True})
    assert deploy.run() == 0 and len(popen.calls) == 2
    assert "Confirm subscription" in out.getvalue()


def test_unconfirmed_emails_stop_with_code_two(work):
    deploy, _, _, _ = make(work, [waiting(EMAILS)], {**ANSWERS, "emails_done": False})
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert error.value.code == 2 and "kept" in error.value.safe


def test_an_expired_canary_is_a_second_paid_call_and_retry_is_used_once(work):
    episodes = [waiting(PAID), waiting(EXPIRED), waiting(EMAILS), ready_episode()]
    deploy, popen, _, _ = make(
        work, episodes, {**ANSWERS, "paid_canary": True, "paid_retry": True, "emails_done": True}
    )
    assert deploy.run() == 0
    third, fourth = popen.calls[2], popen.calls[3]
    assert "--retry-canary" in third and "--allow-model-invocation" in third
    assert "--retry-canary" not in fourth and "--allow-model-invocation" in fourth


def test_stops_that_need_a_person_are_reported_not_retried(work):
    for message in (
        "Legacy resource retirement requires separate review",
        "Production candidates provisioned; existing gates",
    ):
        deploy, popen, _, _ = make(work, [waiting(message)], ANSWERS)
        with pytest.raises(GuidedError) as error:
            deploy.run()
        assert error.value.code == 2 and message[:20] in error.value.cause and len(popen.calls) == 1


def test_slow_aws_stages_are_waited_for_then_given_up_gracefully(work):
    episodes = [
        waiting("durable-foundation: CloudFormation is still processing", auto=True) for _ in range(12)
    ]
    deploy, popen, _, _ = make(work, episodes, ANSWERS)
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert error.value.code == 2 and len(popen.calls) == 12 and "deploy again" in error.value.next_step


def test_a_failure_before_routing_says_nothing_was_touched(work):
    (work / "operations.log").write_text("coverage start\nRequired metric unavailable: i-0abc-memory\n")
    deploy, _, _, _ = make(
        work,
        [
            failed(
                "infra.deploy.durable_ops failed; inspect private operations.log", done=["foundation-tools"]
            )
        ],
        ANSWERS,
    )
    with pytest.raises(GuidedError) as error:
        deploy.run()
    caught = error.value
    assert "Required metric unavailable: i-0abc-memory" in caught.cause
    assert "alarms and servers are untouched" in caught.safe and "durable-foundation" in caught.safe
    assert "collector-examples" in caught.next_step and caught.code == 1


def test_a_failure_after_routing_began_says_so(work):
    episode = Episode(
        [],
        1,
        err="boom",
        final={
            **state(done=["foundation-tools", "durable-foundation"], status="FAILED"),
            "stages": {
                "foundation-tools": {"status": "COMPLETE"},
                "durable-foundation": {"status": "COMPLETE"},
                "routing": {"status": "INTENT"},
            },
        },
    )
    deploy, _, _, _ = make(work, [episode], ANSWERS)
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "after routing began" in error.value.safe and "paused" in error.value.safe


def test_cloudformation_failure_reasons_are_read_and_shown(work):
    deploy, _, _, fake = make(work, [failed("x", done=["foundation-tools"])], ANSWERS)
    fake.on(
        "cloudformation",
        "describe_stack_events",
        {
            "StackEvents": [
                {
                    "StackId": "arn:aws:cloudformation:eu-central-1:123456789012:stack/x/1",
                    "EventId": "e1",
                    "StackName": "tracaus-staging-found",
                    "Timestamp": "2026-01-01T00:00:00Z",
                    "LogicalResourceId": "Table",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "Insufficient permissions\x1b[2J",
                }
            ]
        },
    )
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "Table: Insufficient permissions" in error.value.cause and "\x1b" not in error.value.cause


def test_a_changed_source_points_to_refresh(work):
    deploy, _, _, _ = make(
        work, [failed("Configuration/source changed; create a new deployment work directory")], ANSWERS
    )
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "refresh" in error.value.next_step


def test_the_plan_changing_since_setup_needs_a_new_yes(work):
    deploy, popen, _, _ = make(
        work, [ready_episode()], {**ANSWERS, "replan": False}, runner=Runner(plan_hash="cd" * 32)
    )
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert error.value.code == 3 and popen.calls == []
    deploy, _, _, _ = make(
        work, [ready_episode()], {**ANSWERS, "replan": True}, runner=Runner(plan_hash="cd" * 32)
    )
    # the journal-free run proceeds with the new hash, which is then saved
    (work / "state.json").unlink(missing_ok=True)
    deploy.run()
    assert json.loads((work / "plan.hash").read_text())["plan_hash"] == "cd" * 32


def test_a_dirty_checkout_is_refused_before_anything_runs(work):
    deploy, popen, _, _ = make(work, [ready_episode()], ANSWERS, runner=Runner(dirty=" M infra/spec.py\n"))
    with pytest.raises(GuidedError, match="not clean"):
        deploy.run()
    assert popen.calls == []


def test_missing_setup_files_point_to_setup(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    deploy, _, _, _ = make(folder, [])
    with pytest.raises(GuidedError, match="No settings"):
        deploy.run()


def test_a_journal_from_another_plan_is_not_reused(work):
    (work / "state.json").write_text(json.dumps(state(plan_hash="ff" * 32)))
    deploy, popen, _, _ = make(work, [ready_episode()], ANSWERS)
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "refresh" in error.value.next_step and popen.calls == []


def test_blocked_permissions_are_listed_and_nothing_is_applied(work):
    report = {
        "blockers": [
            {"action": "iam:PassRole", "resource": "arn:aws:iam::1:role/x", "decision": "implicitDeny"}
        ]
    }

    class Writing(Runner):
        def __call__(self, argv, **kwargs):
            if "check" in argv:
                (work / "preflight.json").write_text(json.dumps(report))
            return super().__call__(argv, **kwargs)

    deploy, popen, _, _ = make(work, [ready_episode()], ANSWERS, runner=Writing(check=1))
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "iam:PassRole" in error.value.cause and "AdministratorAccess" in error.value.next_step
    assert popen.calls == []


def test_a_stale_blocker_list_is_never_shown_and_the_real_message_is(work):
    (work / "preflight.json").write_text(json.dumps({"blockers": [{"action": "old:Action"}]}))

    class Failing(Runner):
        def __call__(self, argv, **kwargs):
            if "check" in argv:
                return 1, "Wrong AWS account for the configured profile\n"
            return super().__call__(argv, **kwargs)

    deploy, popen, _, _ = make(work, [ready_episode()], ANSWERS, runner=Failing())
    with pytest.raises(GuidedError) as error:
        deploy.run()
    assert "old:Action" not in error.value.cause and "Wrong AWS account" in error.value.cause


def test_agreeing_to_the_first_paid_request_never_covers_a_second(work):
    episodes = [waiting(PAID), waiting(EXPIRED)]
    deploy, popen, _, _ = make(work, episodes, ANSWERS, allowed={"paid-canary"})
    with pytest.raises(Refused, match="--allow-paid-canary-retry"):
        deploy.run()
    assert len(popen.calls) == 2 and "--retry-canary" not in popen.calls[1]
    deploy, popen, _, _ = make(
        work,
        [waiting(PAID), waiting(EXPIRED), ready_episode()],
        ANSWERS,
        allowed={"paid-canary", "paid-canary-retry"},
    )
    assert deploy.run() == 0 and "--retry-canary" in popen.calls[2]


def test_skip_check_skips_only_the_read_only_check(work):
    runner = Runner(check=1)
    deploy, _, _, _ = make(
        work, [ready_episode()], ANSWERS, runner=runner, options=DeployOptions(skip_check=True)
    )
    assert deploy.run() == 0 and not any("check" in c for c in runner.calls)


def test_an_interrupt_stops_the_child_and_keeps_the_journal(work):
    deploy, popen, _, _ = make(work, [ready_episode()], ANSWERS, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        deploy.run()
    assert popen.processes[0].terminated


def test_a_silent_stretch_prints_a_heartbeat(work):
    ticks = iter(range(0, 1000, 20))
    deploy, _, out, _ = make(
        work,
        [Episode([state(), state(), state()], 0, final=state(done=STAGES, status=READY))],
        ANSWERS,
        clock=lambda: next(ticks),
    )
    deploy.run()
    assert "still working" in out.getvalue()


def test_status_shows_each_stage_and_whether_routing_started(work):
    (work / "state.json").write_text(
        json.dumps(
            state(done=["foundation-tools"], working="durable-foundation", status="FAILED", nxt="Repair it")
        )
    )
    out = io.StringIO()
    fake = Fake()
    fake.on(
        "cloudformation",
        "describe_stacks",
        {
            "Stacks": [
                {"StackName": "s", "CreationTime": "2026-01-01T00:00:00Z", "StackStatus": "ROLLBACK_COMPLETE"}
            ]
        },
    )
    assert status(Console(out), work, clients_for=lambda profile: fake) == 0
    text = out.getvalue()
    assert (
        "ok      foundation-tools" in text
        and "working durable-foundation" in text
        and "pending routing" in text
    )
    assert "not started; your alarms and servers are untouched" in text
    assert "ROLLBACK_COMPLETE" in text and "Run ./bin/tracaus deploy to continue." in text


def test_status_without_a_deployment_says_what_to_run(tmp_path):
    out = io.StringIO()
    status(Console(out), tmp_path, online=False)
    assert "./bin/tracaus setup" in out.getvalue()
