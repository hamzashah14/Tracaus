import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest

from tests.helpers import LOCAL_A, LOCAL_B, local_tools_value, write_local_tools
from tracaus import local_tools
from tracaus.config import AppConfig
from tracaus.runtime import LambdaTools, Limits, RuntimeStop

PREFIX = "/tracaus/staging"
SENTINEL = "SENTINEL-DO-NOT-ECHO"


@pytest.fixture
def config(tmp_path):
    return local_tools.load(write_local_tools(tmp_path))


def tools_for(config, allowed=None):
    return config.tools("eu-central-1", "123456789012", allowed or set(config.instances), Limits(), Mock())


def run(tools, name, **args):
    return tools.invoke(name, args, time.time() + 200)


def logs_client(groups):
    client = MagicMock()
    client.describe_log_groups.return_value = {"logGroups": [{"logGroupName": name} for name in groups]}
    return client


# --- load / validation -------------------------------------------------------------------------


def test_load_accepts_a_valid_file(config):
    assert config.instances == [LOCAL_A, LOCAL_B] and config.log_prefix == PREFIX
    assert config.monitor_region == "eu-central-1" and len(config.metric_catalog) == 3


def too_many_instances():
    ids = [f"i-{n:017x}" for n in range(101)]
    return {"instances": ids, "log_groups": [f"{PREFIX}/{ids[0]}/app"], "metric_catalog": []}


@pytest.mark.parametrize(
    "change,field",
    [
        ({"extra": 1}, "extra"),
        ({"version": 2}, "version"),
        ({"version": True}, "version"),
        ({"version": "1"}, "version"),
        ({"monitor_region": SENTINEL}, "monitor_region"),
        ({"log_prefix": SENTINEL}, "log_prefix"),
        ({"log_prefix": "/tracaus/staging/"}, "log_prefix"),
        ({"instances": []}, "instances"),
        ({"instances": [SENTINEL]}, "instances"),
        ({"instances": [LOCAL_A, LOCAL_A]}, "instances"),
        (too_many_instances(), "instances"),
        ({"log_groups": []}, "log_groups"),
        ({"log_groups": [f"{PREFIX}/{LOCAL_A}/{SENTINEL} x"]}, "log_groups"),
        ({"log_groups": [f"{PREFIX}/{LOCAL_A}/a", f"{PREFIX}/{LOCAL_A}/a"]}, "log_groups"),
        ({"log_groups": ["/other/prefix/app"]}, "log_groups"),
        ({"log_groups": [f"{PREFIX}/i-0aaaaaaaaaaaaaaaa/app"]}, "log_groups"),
        ({"log_groups": [f"{PREFIX}/{LOCAL_A}"]}, "log_groups"),
        ({"metric_catalog": [{"id": SENTINEL}]}, "metric_catalog"),
        ({"instances": [LOCAL_B], "log_groups": [f"{PREFIX}/{LOCAL_B}/app"]}, "metric_catalog"),
    ],
)
def test_load_rejects_invalid_files_without_echoing_values(tmp_path, change, field):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(local_tools_value(**change)))
    with pytest.raises(ValueError, match=field) as error:
        local_tools.load(str(path))
    assert SENTINEL not in str(error.value)


@pytest.mark.parametrize(
    "missing", ["version", "monitor_region", "log_prefix", "instances", "log_groups", "metric_catalog"]
)
def test_load_requires_every_field(tmp_path, missing):
    value = local_tools_value()
    del value[missing]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=missing):
        local_tools.load(str(path))


@pytest.mark.parametrize("content", ["not json", "[]", ""])
def test_load_rejects_non_object_files(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        local_tools.load(str(path))


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="cannot be read"):
        local_tools.load(str(tmp_path / "missing.json"))


# --- adapter ----------------------------------------------------------------------------------


def test_tools_are_the_unmodified_runtime_class_with_synthetic_numeric_arns(config):
    tools = tools_for(config)
    assert type(tools) is LambdaTools and isinstance(tools.client, local_tools.LocalLambdaClient)
    assert tools.arns == {
        "fetch_logs": "arn:aws:lambda:eu-central-1:123456789012:function:tracaus-local-fetch_logs:1",
        "fetch_metrics": "arn:aws:lambda:eu-central-1:123456789012:function:tracaus-local-fetch_metrics:1",
    }
    assert tools_for(config).client is tools.client  # one temp directory per distinct file


def test_handlers_are_loaded_privately_and_never_registered_as_lambda_function(config):
    client = config.client()
    assert "lambda_function" not in sys.modules
    assert not [name for name in sys.modules if name.startswith("tracaus_local")]
    logs, metrics = client.modules["fetch_logs"], client.modules["fetch_metrics"]
    assert logs is not metrics and (logs.MONITOR_REGION, metrics.MONITOR_REGION) == ("eu-central-1",) * 2
    assert logs.LOG_GROUP_PREFIX == PREFIX


def test_scope_files_are_private_and_the_environment_is_only_set_during_the_call(config, monkeypatch):
    client, seen = config.client(), {}

    def spy(event, context):
        seen.update({k: os.environ.get(k) for k in client.env})
        seen["secret"] = os.environ.get("LOG_CURSOR_SECRET")
        seen["remaining"] = context.get_remaining_time_in_millis()
        return {"messageVersion": "1.0"}

    monkeypatch.setattr(client.modules["fetch_logs"], "lambda_handler", spy)
    monkeypatch.delenv("LOG_CURSOR_SECRET")
    before = dict(os.environ)
    function = "arn:aws:lambda:eu-central-1:123456789012:function:tracaus-local-fetch_logs:1"
    response = client.invoke(FunctionName=function, InvocationType="RequestResponse", Payload=b"{}")
    assert response["StatusCode"] == 200 and "FunctionError" not in response
    assert json.loads(response["Payload"].read()) == {"messageVersion": "1.0"}
    assert seen["ALLOWED_INSTANCE_IDS"] == f"{LOCAL_A},{LOCAL_B}" and len(seen["secret"]) >= 32
    assert seen["remaining"] == 120_000
    assert json.loads(open(seen["LOG_SCOPE_FILE"]).read()) == sorted(config.log_groups)
    assert json.loads(open(seen["METRIC_CATALOG_FILE"]).read()) == config.metric_catalog
    assert stat.S_IMODE(os.stat(client.directory).st_mode) == 0o700
    for key in ("LOG_SCOPE_FILE", "METRIC_CATALOG_FILE"):
        assert stat.S_IMODE(os.stat(seen[key]).st_mode) == 0o600
    assert dict(os.environ) == before  # nothing leaks into the host process


def test_client_rejects_unknown_functions_and_async_invocation(config):
    client = config.client()
    function = "arn:aws:lambda:eu-central-1:123456789012:function:tracaus-local-fetch_logs:1"
    with pytest.raises(ValueError):
        client.invoke(
            FunctionName=function.replace("fetch_logs", "other"),
            InvocationType="RequestResponse",
            Payload=b"{}",
        )
    with pytest.raises(ValueError):
        client.invoke(FunctionName=function, InvocationType="Event", Payload=b"{}")


def test_handler_crash_is_invalid_tool_response_without_leaking_the_error(config, monkeypatch, caplog):
    def boom(event, context):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(config.client().modules["fetch_logs"], "lambda_handler", boom)
    with pytest.raises(RuntimeStop, match="INVALID_TOOL_RESPONSE"):
        run(tools_for(config), "fetch_logs", instance_id=LOCAL_A)
    assert SENTINEL not in caplog.text


def test_cross_instance_request_is_refused_before_any_handler_call(config, monkeypatch):
    tools = tools_for(config, allowed={LOCAL_A})
    monkeypatch.setattr(tools.client, "invoke", Mock())
    with pytest.raises(RuntimeStop, match="UNAUTHORIZED_INSTANCE"):
        run(tools, "fetch_logs", instance_id=LOCAL_B)
    tools.client.invoke.assert_not_called()


# --- the real handlers, fake CloudWatch -------------------------------------------------------


def test_discovery_returns_only_in_scope_groups(config):
    names = [
        f"{PREFIX}/{LOCAL_A}/application",
        f"{PREFIX}/{LOCAL_A}/unlisted",
        f"{PREFIX}/{LOCAL_A}/nginx-error",
    ]
    fake = logs_client(names)
    with patch("boto3.client", return_value=fake) as make:
        result, complete = run(tools_for(config), "fetch_logs", instance_id=LOCAL_A)
    assert complete and result["log_groups"] == [names[0], names[2]]
    assert [hint["metric_id"] for hint in result["metric_catalog"]] == ["a-cpu", "a-queue"]
    fake.describe_log_groups.assert_called_once_with(logGroupNamePrefix=f"{PREFIX}/{LOCAL_A}/", limit=20)
    assert make.call_args.args == ("logs",) and make.call_args.kwargs["region_name"] == "eu-central-1"


@pytest.mark.parametrize(
    "group",
    [f"{PREFIX}/{LOCAL_A}/unlisted", f"{PREFIX}/{LOCAL_B}/application", "/other/prefix/application"],
)
def test_out_of_scope_group_is_a_400_and_never_creates_a_cloudwatch_client(config, group):
    with patch("boto3.client") as make:
        result, complete = run(tools_for(config), "fetch_logs", instance_id=LOCAL_A, log_group_name=group)
    assert not complete and result["error_code"] == "INVALID_REQUEST"
    make.assert_not_called()


def test_in_scope_group_is_searched(config):
    row = [
        {"field": "@timestamp", "value": "2026-10-09 10:00:00.000"},
        {"field": "@message", "value": "boom"},
    ]
    fake = MagicMock()
    fake.start_query.return_value = {"queryId": "q"}
    fake.get_query_results.return_value = {"status": "Complete", "results": [row]}
    with patch("boto3.client", return_value=fake):
        result, complete = run(
            tools_for(config),
            "fetch_logs",
            instance_id=LOCAL_A,
            log_group_name=f"{PREFIX}/{LOCAL_A}/application",
        )
    assert complete and result["status"] == "ok" and result["lines_before"][0]["message"] == "boom"
    assert {call.kwargs["logGroupName"] for call in fake.start_query.call_args_list} == {
        f"{PREFIX}/{LOCAL_A}/application"
    }


def test_discovery_paginates_without_a_configured_cursor_secret(tmp_path, monkeypatch):
    groups = [f"{PREFIX}/{LOCAL_A}/service-{n:02d}" for n in range(45)]
    config = local_tools.load(write_local_tools(tmp_path, log_groups=groups))
    monkeypatch.delenv("LOG_CURSOR_SECRET")
    fake = MagicMock()

    def page(**kwargs):
        start = int(kwargs.get("nextToken", "0"))
        end = start + kwargs["limit"]
        return {
            "logGroups": [{"logGroupName": name} for name in groups[start:end]],
            **({"nextToken": str(end)} if end < len(groups) else {}),
        }

    fake.describe_log_groups.side_effect = page
    collected, token = [], None
    tools = tools_for(config)
    with patch("boto3.client", return_value=fake):
        for _ in range(5):
            args = {"instance_id": LOCAL_A, **({"next_token": token} if token else {})}
            result, complete = tools.invoke("fetch_logs", args, time.time() + 200)
            collected += result["log_groups"]
            if complete:
                break
            token = result["next_token"]
    assert collected == groups and "LOG_CURSOR_SECRET" not in os.environ


def test_metrics_resolve_builtin_and_catalog_descriptors(config):
    now = datetime.now(timezone.utc)
    fake = MagicMock()
    fake.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": now, "Average": 12.5, "Maximum": 20.0, "Unit": "Percent"}]
    }
    tools = tools_for(config)
    with patch("boto3.client", return_value=fake) as make:
        builtin, ok = run(tools, "fetch_metrics", instance_id=LOCAL_A, metric_name="CPUUtilization")
        catalog, ok_catalog = run(tools, "fetch_metrics", instance_id=LOCAL_A, metric_id="a-queue")
        refused, ok_refused = run(tools, "fetch_metrics", instance_id=LOCAL_A, metric_id="b-cpu")
    assert ok and builtin["descriptor"]["metric_id"] == "builtin" and builtin["namespace"] == "AWS/EC2"
    assert ok_catalog and catalog["namespace"] == "Custom/App" and catalog["summary"]["last"] == 20.0
    assert {"Name": "service", "Value": "api"} in fake.get_metric_statistics.call_args_list[1].kwargs[
        "Dimensions"
    ]
    assert not ok_refused and refused["error_code"] == "INVALID_REQUEST"
    assert make.call_args.kwargs["region_name"] == "eu-central-1"


# --- problems() --------------------------------------------------------------------------------


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    for key, value in {
        "ENVIRONMENT": "development",
        "BEDROCK_REGION": "eu-central-1",
        "EXPECTED_ACCOUNT_ID": "123456789012",
        "BEDROCK_MODEL_ID": "fixture-model",
        "RUNTIME_LIMITS": json.dumps(Limits().__dict__),
        "TRACAUS_LOCAL_TOOLS": write_local_tools(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "RUNTIME_RELEASE",
        "LOGS_TOOL_ARN",
        "METRICS_TOOL_ARN",
        "ALLOWED_INSTANCE_IDS",
        "LOG_CURSOR_SECRET_ARN",
        "RUNTIME_TARGET",
        "MODEL_API",
    ):
        monkeypatch.delenv(key, raising=False)


def test_problems_accepts_a_local_configuration_without_release_or_tool_arns(local_env):
    assert local_tools.problems(AppConfig.from_env()) == []


def test_problems_accepts_a_matching_allowlist_in_any_order(local_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{LOCAL_B},{LOCAL_A}")
    assert local_tools.problems(AppConfig.from_env()) == []


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("ENVIRONMENT", "production", "ENVIRONMENT=development"),
        ("ENVIRONMENT", "staging", "ENVIRONMENT=development"),
        ("RUNTIME_TARGET", "agentcore", "RUNTIME_TARGET=standalone"),
        ("LOGS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:logs:1", "LOGS_TOOL_ARN"),
        ("METRICS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:m:1", "METRICS_TOOL_ARN"),
        (
            "LOG_CURSOR_SECRET_ARN",
            "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x",
            "LOG_CURSOR_SECRET_ARN",
        ),
        ("ALLOWED_INSTANCE_IDS", LOCAL_A, "ALLOWED_INSTANCE_IDS"),
        ("ALLOWED_INSTANCE_IDS", f"{LOCAL_A},{LOCAL_B},i-0aaaaaaaaaaaaaaaa", "ALLOWED_INSTANCE_IDS"),
    ],
)
def test_problems_rejects_each_unsafe_combination(local_env, monkeypatch, key, value, expected):
    monkeypatch.setenv(key, value)
    found = local_tools.problems(AppConfig.from_env())
    assert len(found) == 1 and expected in found[0]


def test_problems_reports_a_missing_or_invalid_file_without_its_contents(local_env, monkeypatch, tmp_path):
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", str(tmp_path / "missing.json"))
    assert any("TRACAUS_LOCAL_TOOLS" in item for item in local_tools.problems(AppConfig.from_env()))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(local_tools_value(monitor_region=SENTINEL)))
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", str(bad))
    found = local_tools.problems(AppConfig.from_env())
    assert any("monitor_region" in item for item in found) and SENTINEL not in " ".join(found)
