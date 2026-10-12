"""Setup then deploy's plan step with the REAL `dry-run`: the files the wizard writes must plan, and the plan hash
must be the same when it is rebuilt later (otherwise `deploy` would always say the plan changed)."""

import io
import json
import shutil
import uuid

import pytest

from infra.guided.common import run as real_run
from infra.guided.console import Console
from infra.guided.deploy import Deploy, DeployOptions
from infra.guided.prompt import ScriptedPrompter
from infra.guided.setup import Options, Setup
from infra.spec import ROOT
from tests.guided_fakes import Fake
from tests.test_guided_setup import ANSWERS, ID1, ROLE_ARNS, operator_identity, prime


@pytest.fixture
def work():
    folder = ROOT / ".local" / f"test-guided-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, mode=0o700)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def git_then_real(argv, **kwargs):
    if argv[:2] == ["git", "rev-parse"]:
        return 0, "true"
    if argv[:2] == ["git", "status"]:
        return 0, ""
    return real_run(argv, **{**kwargs, "cwd": ROOT})


def test_what_setup_writes_really_plans_and_the_plan_is_stable(work, tmp_path):
    fake, out = Fake(), io.StringIO()
    prime(fake)
    setup = Setup(
        Console(out, work / "guided.log"),
        ScriptedPrompter(ANSWERS),
        work,
        Options(skip_install=True),
        root=ROOT,
        clients_for=lambda profile: fake,
        identity_for=operator_identity,
        profiles_for=lambda: ["default"],
        region_for=lambda profile: "eu-central-1",
        runner=git_then_real,
        config_path=tmp_path / "aws-config",
        sleep=lambda _: None,
    )
    assert setup.run() == 0
    saved = json.loads((work / "plan.hash").read_text())["plan_hash"]
    assert len(saved) == 64
    plan = json.loads((work / "plan.json").read_text())
    assert plan["stages"][0] == "foundation-tools" and "routing" in plan["stages"]
    assert (work / "collector-examples" / f"{ID1}.json").exists()
    assert plan["spec"]["ecs_services"][0]["id"] == "ecs-orders_api"
    assert ROLE_ARNS[0] == plan["spec"]["ci_principal_arn"]

    # a later `deploy` rebuilds the plan and must reach the same hash
    deploy = Deploy(
        Console(io.StringIO(), work / "guided.log"),
        ScriptedPrompter({"apply": False}),
        work,
        DeployOptions(skip_check=True),
        root=ROOT,
        runner=git_then_real,
    )
    deploy.console.begin(5)
    deploy.step_plan()
    assert deploy.plan_hash == saved
