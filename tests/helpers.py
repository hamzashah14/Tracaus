import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_lambda(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "lambda" / name / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body(envelope):
    import json

    return json.loads(envelope["response"]["responseBody"]["application/json"]["body"])


LOCAL_A = "i-0123456789abcdef0"
LOCAL_B = "i-0fedcba987654321a"


def local_tools_value(**overrides):
    """A valid local tools file with two instances; synthetic values only."""

    def entry(key, instance, name, **extra):
        return {
            "id": key,
            "instance_id": instance,
            "namespace": "AWS/EC2",
            "metric_name": name,
            "statistic": "Average",
            "dimensions": {"InstanceId": instance},
            **extra,
        }

    value = {
        "version": 1,
        "monitor_region": "eu-central-1",
        "log_prefix": "/tracaus/staging",
        "instances": [LOCAL_A, LOCAL_B],
        "log_groups": [
            f"/tracaus/staging/{LOCAL_A}/application",
            f"/tracaus/staging/{LOCAL_A}/nginx-error",
            f"/tracaus/staging/{LOCAL_B}/application",
        ],
        "metric_catalog": [
            entry("a-cpu", LOCAL_A, "CPUUtilization"),
            entry(
                "a-queue",
                LOCAL_A,
                "queue_depth",
                namespace="Custom/App",
                statistic="Maximum",
                dimensions={"InstanceId": LOCAL_A, "service": "api"},
            ),
            entry("b-cpu", LOCAL_B, "CPUUtilization"),
        ],
    }
    value.update(overrides)
    return value


def write_local_tools(tmp_path, **overrides):
    import json

    path = tmp_path / "local-tools.json"
    path.write_text(json.dumps(local_tools_value(**overrides)))
    return str(path)
