"""log_files: the generated CloudWatch agent file ships the application's own log files."""

import json

import pytest

from infra.spec import ROOT, cwagent, load, log_groups

A = "i-0123456789abcdef0"
WEB = "/myapp/prod/web"


def make_spec(tmp_path, files, existing=None, example="deployment", groups=None):
    raw = json.loads((ROOT / f"examples/{example}.example.json").read_text())
    raw["reference_only"] = False
    item = raw["instances"][0]
    item["log_files"] = files
    if existing is not None:
        item["existing_log_groups"] = existing
    if groups is not None:
        item["log_groups"] = groups
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(raw))
    return load(path)


def entries(spec):
    return cwagent(spec, spec["instances"][0])["logs"]["logs_collected"]["files"]["collect_list"]


def test_an_owned_group_gets_tracaus_name_the_instance_stream_and_retention(tmp_path):
    spec = make_spec(tmp_path, [{"group": "application", "file": "/var/log/myapp/app.log"}])
    assert {
        "file_path": "/var/log/myapp/app.log",
        "log_group_name": f"/tracaus/staging/{A}/application",
        "log_stream_name": A,
        "retention_in_days": spec["log_retention_days"],
    } in entries(spec)
    assert f"/tracaus/staging/{A}/application" in log_groups(
        spec
    )  # the stack creates the group the agent writes


def test_an_existing_group_keeps_its_name_and_the_agent_never_sets_its_retention(tmp_path):
    spec = make_spec(tmp_path, [{"group": WEB, "file": "/srv/web/*.log"}], [{"name": WEB}])
    item = next(e for e in entries(spec) if e["log_group_name"] == WEB)
    assert item == {"file_path": "/srv/web/*.log", "log_group_name": WEB, "log_stream_name": A}


def test_nginx_comes_first_and_the_heartbeat_stays_last(tmp_path):
    files = [
        {"group": "application", "file": "/var/log/myapp/app.log"},
        {"group": "application", "file": "/var/log/myapp/error.log"},
    ]
    listed = entries(make_spec(tmp_path, files, example="observability"))
    paths = [e["file_path"] for e in listed]
    assert paths == [
        "/var/log/nginx/access.log",
        "/var/log/nginx/error.log",
        "/var/log/myapp/app.log",
        "/var/log/myapp/error.log",
        "/var/log/tracaus-collector-heartbeat.log",
    ]


def test_no_log_files_gives_the_same_file_as_before(tmp_path):
    spec = make_spec(tmp_path, [])
    assert [e["file_path"] for e in entries(spec)] == [
        "/var/log/nginx/access.log",
        "/var/log/nginx/error.log",
    ]


@pytest.mark.parametrize(
    "files",
    [
        [{"group": "elsewhere", "file": "/var/log/a.log"}],  # not declared on the instance
        [{"group": "application", "file": "/var/log/a.log"}] * 2,
        [{"group": "application", "file": "/var/log/../etc/shadow"}],
        [{"group": "application", "file": "relative.log"}],
        [{"group": "application", "file": "/var/log/a b.log"}],
        [{"group": "application"}],
        [{"group": "application", "file": "/var/log/a.log", "mode": "x"}],
    ],
)
def test_bad_log_files_are_rejected_without_echoing_values(tmp_path, files):
    with pytest.raises(ValueError) as error:
        make_spec(tmp_path, files)
    assert "shadow" not in str(error.value) and "elsewhere" not in str(error.value)


def test_a_group_the_file_already_ships_cannot_be_repeated(tmp_path):
    shipped = [{"group": "collector-heartbeat", "file": "/var/log/other.log"}]
    with pytest.raises(ValueError, match="Invalid log_files"):
        make_spec(tmp_path, shipped, example="observability")
    both = [{"group": "nginx-access", "file": "/var/log/other.log"}]
    with pytest.raises(ValueError, match="Invalid log_files"):
        make_spec(tmp_path, both, groups=["application", "nginx-access"])  # nginx_alarm ships it


def test_a_file_can_target_a_group_of_the_instance_only(tmp_path):
    other = [{"group": WEB, "file": "/srv/web/app.log"}]
    with pytest.raises(ValueError, match="Invalid log_files"):
        make_spec(tmp_path, other)  # WEB is not declared as existing on this instance
