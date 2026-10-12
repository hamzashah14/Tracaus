"""The `ui` command, the command-line entry point and the launcher script."""

import io
import json
import os
import stat
import subprocess
import sys

import pytest
from botocore.configloader import load_config

from infra.guided import __main__ as cli
from infra.guided.common import GuidedError, guided_lock
from infra.guided.console import Console
from infra.guided.deploy import launch_ui
from infra.guided.discover import DiscoveryError
from infra.guided.prompt import (
    AnswersPrompter,
    ConsolePrompter,
    NeedsAnswer,
    Refused,
    ScriptedPrompter,
    YesPrompter,
)
from infra.spec import ROOT

ACCOUNT, REGION = "123456789012", "eu-central-1"
UI_ROLE = f"arn:aws:iam::{ACCOUNT}:role/tracaus/staging/ui-abc"


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "ui-connection.json").write_text(
        json.dumps({"environment": {"EXPECTED_ACCOUNT_ID": ACCOUNT}, "ui_role_arn": UI_ROLE})
    )
    (folder / "deployment.json").write_text(
        json.dumps(
            {
                "project": "tracaus",
                "environment": "staging",
                "monitor_region": REGION,
                "ui_principal_arn": f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-ui",
            }
        )
    )
    (folder / "answers.json").write_text(json.dumps({"profile": "default"}))
    return folder


class Launch:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        return 0


def run_ui(work, tmp_path, answers=None, **kwargs):
    out, launch = io.StringIO(), Launch()
    console = Console(out, work / "guided.log")
    code = launch_ui(
        console,
        ScriptedPrompter({"write_ui_profiles": True, **(answers or {})}),
        work,
        config_path=tmp_path / "config",
        runner=launch,
        delay=0,
        **kwargs,
    )
    return code, launch, out.getvalue()


def test_ui_adds_both_profiles_sets_a_password_and_starts_the_launcher(work, tmp_path):
    code, launch, text = run_ui(work, tmp_path)
    assert code == 0
    profiles = load_config(str(tmp_path / "config"))["profiles"]
    assert profiles["tracaus-staging-ui-principal"]["role_arn"].endswith("role/tracaus-staging-ui")
    assert profiles["tracaus-staging-ui"] == {
        "role_arn": UI_ROLE,
        "source_profile": "tracaus-staging-ui-principal",
        "region": REGION,
    }
    argv, env = launch.calls[0]
    assert "--profile" in argv and "tracaus-staging-ui" in argv and env["APP_PASSWORD"] not in argv
    assert len(env["APP_PASSWORD"]) >= 12 and "http://127.0.0.1:8501" in text


def test_the_password_is_private_shown_once_and_never_logged(work, tmp_path):
    _, launch, text = run_ui(work, tmp_path)
    password = launch.calls[0][1]["APP_PASSWORD"]
    path = work / "ui-password"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and password in text
    assert password not in (work / "guided.log").read_text()
    _, again, text2 = run_ui(work, tmp_path)
    assert again.calls[0][1]["APP_PASSWORD"] == password and password not in text2


def test_a_password_file_that_others_can_read_is_refused(work, tmp_path):
    (work / "ui-password").write_text("x" * 20)
    os.chmod(work / "ui-password", 0o644)
    with pytest.raises(GuidedError, match="not private"):
        run_ui(work, tmp_path)


def test_a_typed_password_must_be_long_enough(work, tmp_path):
    with pytest.raises(GuidedError, match="too short"):
        run_ui(work, tmp_path, {"ui_password": "short"}, prompt_password=True)
    _, launch, _ = run_ui(work, tmp_path, {"ui_password": "a-long-enough-password"}, prompt_password=True)
    assert launch.calls[0][1]["APP_PASSWORD"] == "a-long-enough-password"
    assert not (work / "ui-password").exists()


def test_team_mode_passes_the_file_and_sets_no_password(work, tmp_path, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    team = tmp_path / "team.toml"
    _, launch, _ = run_ui(work, tmp_path, team_file=team)
    argv, env = launch.calls[0]
    assert argv[argv.index("--team-file") + 1] == str(team) and "APP_PASSWORD" not in env
    assert not (work / "ui-password").exists()


def test_declining_the_profile_prints_the_blocks_to_add_by_hand(work, tmp_path):
    _, _, text = run_ui(work, tmp_path, {"write_ui_profiles": False})
    assert "[profile tracaus-staging-ui]" in text and not (tmp_path / "config").exists()


def test_ui_before_a_finished_deploy_points_to_deploy(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(GuidedError, match="not finished"):
        launch_ui(Console(io.StringIO()), ScriptedPrompter({}), folder, delay=0)


def test_ui_needs_the_recorded_setup_profile(work, tmp_path):
    (work / "answers.json").write_text("{}")
    with pytest.raises(GuidedError, match="not recorded"):
        run_ui(work, tmp_path)


def parse(*argv):
    return cli.parser().parse_args(list(argv))


def test_flags_choose_the_prompter_and_the_consent_set(tmp_path):
    console = Console(io.StringIO())
    assert isinstance(cli.prompter_for(parse("setup"), console, tmp_path), ConsolePrompter)
    assert isinstance(cli.prompter_for(parse("setup", "--yes"), console, tmp_path), YesPrompter)
    answers = tmp_path / "a.json"
    answers.write_text("{}")
    assert isinstance(
        cli.prompter_for(parse("setup", "--answers", str(answers)), console, tmp_path), AnswersPrompter
    )
    allowed = cli.prompter_for(
        parse("deploy", "--allow-iam-bootstrap", "--allow-paid-canary"), console, tmp_path
    ).allowed
    assert allowed == {"iam-bootstrap", "paid-canary"}
    assert cli.prompter_for(parse("deploy"), console, tmp_path).allowed == set()


def test_yes_never_adds_a_consent(tmp_path):
    assert cli.prompter_for(parse("deploy", "--yes"), Console(io.StringIO()), tmp_path).allowed == set()


@pytest.mark.parametrize(
    "error,code",
    [
        (GuidedError("a", "b", "c", "d", code=2), 2),
        (Refused("needs a flag"), 3),
        (NeedsAnswer("emails"), 1),
        (DiscoveryError("cannot reach AWS"), 1),
        (KeyboardInterrupt(), 130),
        (RuntimeError("secret detail"), 1),
    ],
)
def test_each_failure_maps_to_an_exit_code_and_a_plain_message(tmp_path, monkeypatch, capsys, error, code):
    def boom(args, console, directory):
        raise error

    monkeypatch.setattr(cli, "dispatch", boom)
    monkeypatch.setattr(cli, "private_dir", lambda path: tmp_path)
    assert cli.main(["status", "--work-dir", str(tmp_path)]) == code
    shown = capsys.readouterr()
    assert "Traceback" not in shown.out + shown.err and "secret detail" not in shown.out + shown.err


def test_debug_shows_the_trace_for_unexpected_errors(tmp_path, monkeypatch, capsys):
    def boom(args, console, directory):
        raise RuntimeError("detail")

    monkeypatch.setattr(cli, "dispatch", boom)
    monkeypatch.setattr(cli, "private_dir", lambda path: tmp_path)
    cli.main(["status", "--debug", "--work-dir", str(tmp_path)])
    assert "RuntimeError: detail" in capsys.readouterr().err


def test_a_work_folder_outside_local_is_refused(tmp_path, capsys):
    assert cli.main(["status", "--work-dir", str(tmp_path / "elsewhere")]) == 1
    assert "cannot be used" in capsys.readouterr().out


def test_two_guided_commands_cannot_share_a_folder(tmp_path):
    with (
        guided_lock(tmp_path),
        pytest.raises(GuidedError, match="Another guided command"),
        guided_lock(tmp_path),
    ):
        pass
    with guided_lock(tmp_path):  # released again after the first block ended
        pass


def test_the_launcher_prints_help_without_installing_anything():
    done = subprocess.run(
        ["sh", str(ROOT / "bin/tracaus"), "--help"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0 and "setup" in done.stdout and "deploy" in done.stdout and "ui" in done.stdout
    bad = subprocess.run(
        ["sh", str(ROOT / "bin/tracaus"), "nope"], capture_output=True, text=True, check=False
    )
    assert bad.returncode == 2 and "Usage" in bad.stderr
    assert os.access(ROOT / "bin/tracaus", os.X_OK)


def test_the_module_help_runs():
    done = subprocess.run(
        [sys.executable, "-m", "infra.guided", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0 and "setup" in done.stdout


def test_status_and_ui_do_not_wait_for_a_running_deploy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "private_dir", lambda path: tmp_path)
    with guided_lock(tmp_path):  # a deploy is running in this folder
        assert cli.main(["status", "--work-dir", str(tmp_path)]) == 0
        assert cli.main(["deploy", "--work-dir", str(tmp_path)]) == 1  # but a second deploy is refused
    assert "Another guided command" in capsys.readouterr().out


def test_the_retry_flag_is_separate_from_the_first_paid_flag(tmp_path):
    console = Console(io.StringIO())
    both = cli.prompter_for(parse("deploy", "--allow-paid-canary"), console, tmp_path).allowed
    assert both == {"paid-canary"}
    retry = cli.prompter_for(parse("deploy", "--allow-paid-canary-retry"), console, tmp_path).allowed
    assert retry == {"paid-canary-retry"}


def test_a_saved_password_that_is_empty_or_short_is_refused_and_the_write_is_whole(work, tmp_path):
    (work / "ui-password").write_text("")
    os.chmod(work / "ui-password", 0o600)
    with pytest.raises(GuidedError, match="too short or empty"):
        run_ui(work, tmp_path)
    (work / "ui-password").unlink()
    run_ui(work, tmp_path)
    assert not (work / "ui-password.tmp").exists() and len((work / "ui-password").read_text().strip()) >= 12


def test_profile_errors_in_ui_are_explained_not_unexpected(work, tmp_path):
    (work / "answers.json").write_text(json.dumps({"profile": "tracaus-staging-ui-principal"}))
    with pytest.raises(GuidedError, match="could not be set up"):
        run_ui(work, tmp_path)
