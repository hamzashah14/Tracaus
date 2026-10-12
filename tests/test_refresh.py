"""Starting the next release from an earlier one."""

import json
from pathlib import Path

import pytest

from infra.deploy import automation
from infra.deploy.verify import VerificationError
from tests.test_deployment_automation import run_main


@pytest.fixture
def old(tmp_path, monkeypatch):
    """A work directory as `init` leaves it, plus the files a running deployment adds."""

    def make(path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(automation, "private_dir", make)
    directory = tmp_path / "old"
    assert run_main(monkeypatch, "init", "--work-dir", directory) == 0
    for name in ("state.json", "plan.json", "ui-connection.json"):
        (directory / name).write_text("{}")
    return directory


def snapshot(directory):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


@pytest.mark.parametrize(
    "current,expected",
    [("example001", "example002"), ("r9", "r10"), ("r99", "r100"), ("stage", "stage-2"), ("a-3", "a-4")],
)
def test_the_next_release_counts_up(current, expected):
    assert automation.next_release_id(current) == expected


def test_a_release_id_that_would_be_too_long_must_be_chosen(tmp_path):
    with pytest.raises(VerificationError, match="--release-id"):
        automation.next_release_id("abcdefghijklmn99" + "")  # 16 characters; the next one has 17


def test_refresh_copies_the_settings_with_a_new_release_id(old, tmp_path, monkeypatch, capsys):
    before = snapshot(old)
    new = tmp_path / "new"
    assert run_main(monkeypatch, "refresh", "--from-dir", old, "--work-dir", new) == 0
    assert "example002" in capsys.readouterr().out
    assert snapshot(old) == before  # the old release is untouched
    assert sorted(p.name for p in new.iterdir() if p.is_file() and p.name != ".lock") == [
        "automation.json",
        "deployment.json",
        "runtime.json",
    ]
    old_spec = json.loads((old / "deployment.json").read_text())
    new_spec = json.loads((new / "deployment.json").read_text())
    assert new_spec == {**old_spec, "release_id": "example002"}
    assert (new / "runtime.json").read_text() and json.loads(
        (new / "runtime.json").read_text()
    ) == json.loads((old / "runtime.json").read_text())
    settings = json.loads((new / "automation.json").read_text())
    assert settings["spec"] == "deployment.json" and settings["runtime_config"] == "runtime.json"
    assert settings["wheelhouse"] == json.loads((old / "automation.json").read_text())["wheelhouse"]
    for name in ("automation.json", "deployment.json", "runtime.json"):
        assert (new / name).stat().st_mode & 0o777 == 0o600
    assert automation.plan(new / "automation.json")["spec"]["release_id"] == "example002"


def test_refresh_resolves_a_relative_wheelhouse_against_the_old_directory(old, tmp_path):
    settings = json.loads((old / "automation.json").read_text())
    settings["wheelhouse"] = "wheels"
    (old / "automation.json").write_text(json.dumps(settings))
    new = tmp_path / "new"
    new.mkdir()
    automation.refresh(old, new)
    assert json.loads((new / "automation.json").read_text())["wheelhouse"] == str((old / "wheels").resolve())


def test_an_explicit_release_id_is_used(old, tmp_path):
    new = tmp_path / "new"
    new.mkdir()
    assert automation.refresh(old, new, "march-2")[1] == "march-2"
    assert json.loads((new / "deployment.json").read_text())["release_id"] == "march-2"


@pytest.mark.parametrize("release_id", ["example001", "Bad_ID", "x" * 17])
def test_a_wrong_release_id_leaves_nothing_behind(old, tmp_path, release_id):
    new = tmp_path / "new"
    new.mkdir()
    with pytest.raises(VerificationError):
        automation.refresh(old, new, release_id)
    assert list(new.iterdir()) == []


def test_refresh_never_overwrites_and_needs_a_new_directory(old, tmp_path, monkeypatch, capsys):
    new = tmp_path / "new"
    assert run_main(monkeypatch, "refresh", "--from-dir", old, "--work-dir", new) == 0
    first = snapshot(new)
    assert run_main(monkeypatch, "refresh", "--from-dir", old, "--work-dir", new) == 1  # files exist now
    assert snapshot(new) == first
    assert run_main(monkeypatch, "refresh", "--from-dir", old, "--work-dir", old) == 1
    assert run_main(monkeypatch, "refresh", "--work-dir", tmp_path / "other") == 1  # --from-dir missing
    capsys.readouterr()


def test_refresh_refuses_an_old_directory_with_other_settings(old, tmp_path):
    settings = json.loads((old / "automation.json").read_text())
    settings["initial_access"] = []
    (old / "automation.json").write_text(json.dumps(settings))
    new = tmp_path / "new"
    new.mkdir()
    with pytest.raises(VerificationError):
        automation.refresh(old, new)
    assert list(new.iterdir()) == []
