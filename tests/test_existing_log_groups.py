"""Existing log groups: Tracaus reads groups that already exist and never creates them."""

import copy
import hashlib
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from infra import release
from infra.spec import ROOT, existing_log_groups, load, log_groups, metric_catalog, readable_log_groups
from infra.verify import VerificationError, coverage
from scripts.dev.validate_infrastructure import examples
from scripts.make_local_tools import build as local_file
from tests.helpers import body, load_lambda
from tests.test_infrastructure import good_clients
from tracaus import local_tools

logs = load_lambda("fetch_logs")
A, B = "i-0123456789abcdef0", "i-11111111111111111"
WEB, WORKER = "/myapp/prod/web", "/myapp/prod/worker"
OWN = f"/tracaus/staging/{A}/application"  # inside the example deployment's own log prefix


def make_spec(tmp_path, existing=None, extra=None, groups=None):
    raw = json.loads((ROOT / "examples/deployment.example.json").read_text())
    raw["reference_only"] = False
    item = raw["instances"][0]
    if existing is not None:
        item["existing_log_groups"] = existing
    if groups is not None:
        item["log_groups"] = groups
    if extra is not None:
        raw["instances"].append({**copy.deepcopy(item), "id": B, **extra})
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(raw))
    return load(path)


def test_the_helpers_split_created_from_read_groups(tmp_path):
    plain = make_spec(tmp_path)
    assert existing_log_groups(plain) == {} and readable_log_groups(plain) == log_groups(plain)
    spec = make_spec(tmp_path, [{"name": WEB}, {"name": WORKER, "streams": "all"}])
    assert existing_log_groups(spec) == {A: {WEB: "instance", WORKER: "all"}}
    assert set(readable_log_groups(spec)) == set(log_groups(spec)) | {WEB, WORKER}
    assert WEB not in log_groups(spec)  # the stack never creates it


@pytest.mark.parametrize(
    "existing",
    [
        [{"name": OWN}],
        [{"name": WEB}, {"name": WEB}],
        [{"name": "/myapp/"}],
        [{"name": "/myapp/*"}],
        [{"name": WEB, "streams": "some"}],
        [{"name": WEB, "extra": 1}],
    ],
)
def test_bad_existing_groups_are_rejected_without_echoing_values(tmp_path, existing):
    with pytest.raises(ValueError) as error:
        make_spec(tmp_path, existing)
    assert WEB not in str(error.value) and "myapp" not in str(error.value)


def test_the_rejection_names_the_rule(tmp_path):
    with pytest.raises(ValueError, match="not one of Tracaus's own groups"):
        make_spec(tmp_path, [{"name": OWN}])
    with pytest.raises(ValueError, match="not one of Tracaus's own groups"):
        make_spec(tmp_path, [{"name": "application"}])  # the example's own short group name


def test_an_instance_needs_at_least_one_group(tmp_path):
    with pytest.raises(ValueError, match="needs log_groups or existing_log_groups"):
        make_spec(tmp_path, groups=[])
    spec = make_spec(tmp_path, [{"name": WEB}], groups=[])  # only existing groups is fine
    assert existing_log_groups(spec) == {A: {WEB: "instance"}}


def test_a_group_read_whole_belongs_to_one_instance(tmp_path):
    with pytest.raises(ValueError, match="one instance only"):
        make_spec(tmp_path, [{"name": WEB, "streams": "all"}], extra={})
    with pytest.raises(ValueError, match="one instance only"):
        make_spec(tmp_path, [{"name": WEB}], extra={"existing_log_groups": [{"name": WEB, "streams": "all"}]})
    # Two instances may share a group when each reads only its own streams.
    spec = make_spec(tmp_path, [{"name": WEB}], extra={})
    assert existing_log_groups(spec) == {A: {WEB: "instance"}, B: {WEB: "instance"}}


def test_the_stack_creates_nothing_and_the_tool_may_only_read_the_group(tmp_path):
    spec = make_spec(tmp_path, [{"name": WEB}])
    rendered = examples(spec)
    assert all(WEB not in json.dumps(t) for stage, t in rendered.items() if stage != "owned-tools")
    tools = rendered["owned-tools"]["Resources"]
    function = tools["Logs"]["Properties"]["Environment"]["Variables"]
    assert function["EXISTING_LOG_GROUPS_FILE"] == "config/existing-log-groups.json"
    assert "EXISTING_LOG_GROUPS_FILE" not in tools["Metrics"]["Properties"]["Environment"]["Variables"]
    policy = json.dumps(tools["LogsRole"])
    assert f"log-group:{WEB}:*" in policy
    plain = examples(make_spec(tmp_path))["owned-tools"]["Resources"]
    assert "EXISTING_LOG_GROUPS_FILE" not in plain["Logs"]["Properties"]["Environment"]["Variables"]


def test_coverage_fails_closed_when_an_existing_group_is_missing(tmp_path):
    spec = make_spec(tmp_path, [{"name": WEB}])
    services, factory = good_clients(spec)
    services["logs"].describe_log_groups.return_value = {"logGroups": [{"logGroupName": WEB + "-old"}]}
    with pytest.raises(VerificationError, match="Existing log group not found"):
        coverage(spec, factory)
    services["logs"].describe_log_groups.return_value = {"logGroups": [{"logGroupName": WEB}]}
    assert coverage(spec, factory)["status"] == "PASS"
    assert services["logs"].describe_log_groups.call_args.kwargs == {"logGroupNamePrefix": WEB}


def packaged(tmp_path, spec, existing=None):
    """A minimal build directory that satisfies checked_build, with the packaged scope files given."""
    names = {str(p.relative_to(ROOT)) for p in (ROOT / "tracaus").glob("*.py")} | {
        "agent-instruction.txt",
        "schemas/fetch_logs.json",
        "schemas/fetch_metrics.json",
        "schemas/fetch_ecs.json",
        "tracaus_agentcore.py",
    }
    lock = (ROOT / "requirements/lambda.lock").read_bytes()
    files = {name: (ROOT / name).read_bytes() for name in names}
    files["requirements/lambda.lock"] = lock
    files["config/metric-catalog.json"] = json.dumps(metric_catalog(spec)).encode()
    files["config/log-scope.json"] = json.dumps(log_groups(spec)).encode()
    if existing is not None:
        files["config/existing-log-groups.json"] = json.dumps(existing).encode()
    manifest = {
        "python": "3.12",
        "architecture": "x86_64",
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "functions": {},
    }
    for function in ("fetch_logs", "fetch_metrics"):
        source = ROOT / "lambda" / function / "lambda_function.py"
        members = {**files, "lambda_function.py": source.read_bytes()}
        with zipfile.ZipFile(tmp_path / f"{function}.zip", "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        manifest["functions"][function] = {
            "artifact": f"{function}.zip",
            "sha256": hashlib.sha256((tmp_path / f"{function}.zip").read_bytes()).hexdigest(),
            "source_files": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in members.items()
                if name not in {"requirements/lambda.lock"}
            },
        }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))


def test_the_release_check_binds_the_packaged_file_to_the_inventory(tmp_path):
    declared = make_spec(tmp_path, [{"name": WEB}])
    packaged(tmp_path, declared, existing=existing_log_groups(declared))
    release.checked_build(tmp_path, declared)
    with pytest.raises(VerificationError, match="existing log groups differ"):
        release.checked_build(tmp_path, make_spec(tmp_path))  # a stray file the inventory does not declare
    packaged(tmp_path, declared, existing={A: {WEB: "all"}})
    with pytest.raises(VerificationError, match="existing log groups differ"):
        release.checked_build(tmp_path, declared)  # different content
    packaged(tmp_path, declared)
    with pytest.raises(VerificationError, match="existing log groups differ"):
        release.checked_build(tmp_path, declared)  # declared but not packaged


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    def configure(existing, scope=("/aiops/" + A + "/application",)):
        (tmp_path / "scope.json").write_text(json.dumps(list(scope)))
        (tmp_path / "existing.json").write_text(json.dumps(existing))
        monkeypatch.setenv("LOG_SCOPE_FILE", str(tmp_path / "scope.json"))
        monkeypatch.setenv("EXISTING_LOG_GROUPS_FILE", str(tmp_path / "existing.json"))
        monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{A},{B}")

    return configure


def call(params, client=None):
    event = {"parameters": [{"name": k, "value": v} for k, v in params.items()]}
    with patch("boto3.client", return_value=client or MagicMock()):
        return body(logs.lambda_handler(event, None))


def insights():
    client = MagicMock()
    client.start_query.return_value = {"queryId": "q"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}
    return client


def test_an_instance_reads_its_own_existing_group_filtered_to_its_streams(deployed):
    deployed({A: {WEB: "instance"}})
    client = insights()
    result = call({"instance_id": A, "log_group_name": WEB}, client)
    assert result["status"] == "no_matching_lines" and result["log_group"] == WEB
    queries = [c.kwargs["queryString"] for c in client.start_query.call_args_list]
    assert all(f"filter @logStream = '{A}'" in q for q in queries)
    assert {c.kwargs["logGroupName"] for c in client.start_query.call_args_list} == {WEB}


def test_a_group_declared_for_all_streams_is_read_whole(deployed):
    deployed({A: {WORKER: "all"}})
    client = insights()
    call({"instance_id": A, "log_group_name": WORKER}, client)
    assert all("@logStream" not in c.kwargs["queryString"] for c in client.start_query.call_args_list)


def test_another_instance_cannot_read_it_and_nothing_reaches_aws(deployed):
    deployed({A: {WEB: "instance"}})
    client = insights()
    result = call({"instance_id": B, "log_group_name": WEB}, client)
    assert result["status"] == "error"
    client.start_query.assert_not_called()


def test_an_undeclared_group_is_refused_even_with_existing_groups_configured(deployed):
    deployed({A: {WEB: "instance"}})
    client = insights()
    for name in ("/myapp/prod/other", "/var/secret/logs", "/aiops/" + B + "/application"):
        assert call({"instance_id": A, "log_group_name": name}, client)["status"] == "error"
    client.start_query.assert_not_called()


def test_discovery_lists_the_existing_groups_after_the_tracaus_ones(deployed):
    deployed({A: {WEB: "instance", WORKER: "all"}, B: {"/other/app": "instance"}})
    client = MagicMock()
    client.describe_log_groups.return_value = {"logGroups": [{"logGroupName": f"/aiops/{A}/application"}]}
    result = call({"instance_id": A}, client)
    assert result["log_groups"] == [f"/aiops/{A}/application", WEB, WORKER]
    assert result["complete"] is True


def test_an_instance_with_only_existing_groups_can_be_discovered_and_read(deployed):
    deployed({A: {WEB: "instance"}}, scope=())
    client = MagicMock()
    client.describe_log_groups.return_value = {"logGroups": []}
    assert call({"instance_id": A}, client)["log_groups"] == [WEB]
    assert call({"instance_id": A, "log_group_name": WEB}, insights())["status"] == "no_matching_lines"


def test_an_empty_scope_without_existing_groups_is_still_invalid(deployed, monkeypatch):
    deployed({}, scope=())
    monkeypatch.delenv("EXISTING_LOG_GROUPS_FILE")
    assert call({"instance_id": A}, MagicMock())["status"] == "error"


def test_a_malformed_existing_file_fails_closed(deployed):
    deployed({A: {WEB: "everything"}})
    client = insights()
    assert call({"instance_id": A, "log_group_name": WEB}, client)["status"] == "error"
    client.start_query.assert_not_called()


def test_local_tools_file_carries_the_same_groups_and_scope(tmp_path):
    spec = make_spec(tmp_path, [{"name": WEB}, {"name": WORKER, "streams": "all"}])
    value = local_file(spec)
    config = local_tools.parse(value)
    assert config.existing == {A: {WEB: "instance", WORKER: "all"}}
    assert "existing_log_groups" not in local_file(make_spec(tmp_path))
    env = config.client().env
    assert json.loads(open(env["EXISTING_LOG_GROUPS_FILE"]).read()) == config.existing
    assert local_tools.parse(local_file(make_spec(tmp_path))).client().env["EXISTING_LOG_GROUPS_FILE"] == ""


@pytest.mark.parametrize(
    "change",
    [
        {A: {OWN: "instance"}},  # inside log_prefix
        {"i-99999999999999999": {WEB: "instance"}},  # not a listed instance
        {A: {WEB: "everything"}},
        {A: {}},
        {A: {WEB: "all"}, B: {WEB: "instance"}},
    ],
)
def test_local_tools_rejects_bad_existing_groups(tmp_path, change):
    spec = make_spec(tmp_path, extra={})
    value = local_file(spec)
    value["existing_log_groups"] = change
    with pytest.raises(ValueError, match="existing_log_groups"):
        local_tools.parse(value)
