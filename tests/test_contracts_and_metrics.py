import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from openapi_schema_validator import OAS30Validator
from openapi_spec_validator import validate

from tests.helpers import ROOT, body, load_lambda
from tracaus.metrics import resolve, validate_catalog
from tracaus.transport import bounded_envelope, dumps

IID = "i-0123456789abcdef0"
metrics = load_lambda("fetch_metrics")
logs = load_lambda("fetch_logs")


def check_contract(tool, response):
    document = json.loads((ROOT / "schemas" / f"{tool}.json").read_text())
    schema = next(iter(document["paths"].values()))["get"]["responses"][
        str(response["response"]["httpStatusCode"])
    ]["content"]["application/json"]["schema"]
    OAS30Validator(schema).validate(body(response))


@pytest.mark.parametrize("tool", ["fetch_logs", "fetch_metrics"])
def test_openapi_documents_validate(tool):
    validate(json.loads((ROOT / "schemas" / f"{tool}.json").read_text()))


@pytest.mark.parametrize(
    "response",
    [
        {"Datapoints": []},
        {
            "Datapoints": [
                {
                    "Timestamp": datetime(2026, 9, 24, 10, tzinfo=timezone.utc),
                    "Average": 42,
                    "Unit": "Percent",
                }
            ]
        },
    ],
)
def test_actual_metric_success_and_no_data_contract(response):
    client = MagicMock()
    client.get_metric_statistics.return_value = response
    with patch("boto3.client", return_value=client):
        result = metrics.lambda_handler({"parameters": [{"name": "instance_id", "value": IID}]}, None)
    check_contract("fetch_metrics", result)


@pytest.mark.parametrize("tool", ["fetch_logs", "fetch_metrics"])
@pytest.mark.parametrize(
    "event",
    [
        {"parameters": "bad"},
        {"parameters": [None]},
        {"parameters": [{"name": "hours_back", "value": []}]},
        {"parameters": []},
    ],
)
def test_invalid_input_error_contract(tool, event):
    result = (logs if tool == "fetch_logs" else metrics).lambda_handler(event, None)
    assert body(result)["status"] == "error"
    check_contract(tool, result)


def test_api_access_errors_are_not_no_data_and_do_not_expose_details():
    client = MagicMock()
    client.get_metric_statistics.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "sensitive-instance-and-secret"}}, "GetMetricStatistics"
    )
    with patch("boto3.client", return_value=client):
        result = metrics.lambda_handler({"parameters": [{"name": "instance_id", "value": IID}]}, None)
    assert body(result)["error_code"] == "METRIC_ACCESS_FAILED"
    assert "sensitive" not in dumps(result)
    check_contract("fetch_metrics", result)


def test_nginx_dimensionless_metric_matches_alarm():
    descriptor = resolve(
        {"instance_id": IID, "namespace": "AIOpsNginx", "metric_name": f"nginx-upstream-errors-{IID}"}
    )
    assert descriptor["dimensions"] == [] and descriptor["statistic"] == "Sum"
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": []}
    with patch("boto3.client", return_value=client):
        metrics.fetch(
            {"instance_id": IID, "namespace": "AIOpsNginx", "metric_name": f"nginx-upstream-errors-{IID}"}
        )
    assert client.get_metric_statistics.call_args.kwargs["Dimensions"] == []


@pytest.mark.parametrize(
    "namespace,name,dimensions,unit",
    [
        ("AWS/EBS", "VolumeQueueLength", {"VolumeId": "vol-0123456789abcdef0"}, "Count"),
        (
            "CWAgent",
            "procstat_lookup_pid_count",
            {"InstanceId": IID, "exe": "nginx", "pid_finder": "native"},
            "Count",
        ),
        ("CWAgent", "disk_used_percent", {"InstanceId": IID, "path": "/data", "fstype": "ext4"}, "Percent"),
    ],
)
def test_custom_descriptors_exact_dimensions(tmp_path, monkeypatch, namespace, name, dimensions, unit):
    descriptor = {
        "id": "custom",
        "instance_id": IID,
        "namespace": namespace,
        "metric_name": name,
        "statistic": "Maximum",
        "unit": unit,
        "dimensions": dimensions,
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([descriptor]))
    monkeypatch.setenv("METRIC_CATALOG_FILE", str(path))
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": []}
    with patch("boto3.client", return_value=client):
        metrics.fetch({"instance_id": IID, "metric_id": "custom"})
    request = client.get_metric_statistics.call_args.kwargs
    assert {item["Name"]: item["Value"] for item in request["Dimensions"]} == dimensions
    assert request["Unit"] == unit and request["Statistics"] == ["Maximum"]
    with pytest.raises(ValueError):
        resolve({"instance_id": IID, "metric_id": "custom", "namespace": "attacker"})


@pytest.mark.parametrize(
    "params",
    [
        {"instance_id": "i-00000000000000000"},
        {"instance_id": IID, "namespace": "Unapproved"},
        {"instance_id": IID, "metric_id": "unknown"},
        {"instance_id": IID, "metric_name": "disk_used_percent", "path": "/private"},
    ],
)
def test_unauthorized_descriptors_rejected(params):
    with pytest.raises(ValueError):
        resolve(params)


def test_missing_instance_scope_fails_closed(monkeypatch):
    monkeypatch.delenv("ALLOWED_INSTANCE_IDS")
    with pytest.raises(ValueError, match="not configured"):
        resolve({"instance_id": IID})


def test_catalog_rejects_mismatched_instance():
    with pytest.raises(ValueError):
        validate_catalog(
            [
                {
                    "id": "invalid",
                    "instance_id": IID,
                    "namespace": "CWAgent",
                    "metric_name": "x",
                    "statistic": "Sum",
                    "dimensions": {"InstanceId": "i-00000000000000000"},
                }
            ]
        )


def test_partial_contract_preserves_nearest_lines():
    result = {
        "status": "ok",
        "complete": True,
        "truncated": False,
        "lines_before": [{"timestamp": str(i), "message": "界" * 500} for i in range(50)],
    }
    envelope = bounded_envelope({}, result)
    check_contract("fetch_logs", envelope)
    parsed = body(envelope)
    assert parsed["status"] == "partial" and parsed["complete"] is False
    assert parsed["lines_before"][-1]["timestamp"] == "49"
    assert len(dumps(envelope).encode()) <= 20000


def test_invalid_log_input_does_not_attempt_credential_resolution():
    with patch("boto3.client") as client:
        response = logs.lambda_handler({"parameters": []}, None)
    client.assert_not_called()
    assert response["response"]["httpStatusCode"] == 400


def test_catalog_entry_resolves_to_exact_descriptor(tmp_path, monkeypatch):
    entry = {
        "id": "nginx-process",
        "instance_id": IID,
        "namespace": "CWAgent",
        "metric_name": "procstat_lookup_pid_count",
        "statistic": "Minimum",
        "unit": "Count",
        "dimensions": {"InstanceId": IID, "exe": "nginx", "pid_finder": "native"},
    }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps([entry]))
    monkeypatch.setenv("METRIC_CATALOG_FILE", str(catalog_path))
    descriptor = resolve({"instance_id": IID, "metric_id": entry["id"]})
    assert {item["Name"]: item["Value"] for item in descriptor["dimensions"]} == entry["dimensions"]
    assert descriptor["statistic"] == "Minimum"


@pytest.mark.parametrize(
    "namespace,metric,statistic,dimensions",
    [
        ("AWS/EC2", "StatusCheckFailed", "Maximum", {"InstanceId": IID}),
        ("AWS/EC2", "CPUUtilization", "Average", {"InstanceId": IID}),
        ("CWAgent", "mem_used_percent", "Average", {"InstanceId": IID}),
        ("CWAgent", "disk_used_percent", "Average", {"InstanceId": IID, "path": "/"}),
        ("CWAgent", "procstat_lookup_pid_count", "Minimum", {"InstanceId": IID}),
        ("AIOpsNginx", f"nginx-upstream-errors-{IID}", "Sum", {}),
    ],
)
def test_supported_builtin_alarm_queries_exact_published_descriptor(namespace, metric, statistic, dimensions):
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": []}
    with patch("boto3.client", return_value=client):
        metrics.fetch(
            {"instance_id": IID, "namespace": namespace, "metric_name": metric, "statistic": statistic}
        )
    request = client.get_metric_statistics.call_args.kwargs
    assert request["Namespace"] == namespace and request["MetricName"] == metric
    assert request["Statistics"] == [statistic]
    assert {item["Name"]: item["Value"] for item in request["Dimensions"]} == dimensions
