import copy
import json
from unittest.mock import Mock

import pytest

from infra.deploy import durable_ops
from infra.deploy.verify import VerificationError
from infra.spec import ROOT, load
from infra.stacks import owned_runtime
from scripts.dev.validate_durable import examples

SPEC = load(ROOT / "examples/deployment.example.json")
BASE = json.loads((ROOT / "examples/durable.example.json").read_text())


def bindings(spec=SPEC, target="standalone"):
    config = {**BASE, "runtime_target": target}
    _, data, _, versions = examples(spec, config, include_bindings=True)
    data["versions"] = versions
    return data


def test_agentcore_always_has_deployment_environment_even_without_identity_config():
    data = bindings(target="agentcore")
    stages = examples(SPEC, {**BASE, "runtime_target": "agentcore"})
    assert (
        stages["agentcore-runtime"]["Resources"]["Runtime"]["Properties"]["EnvironmentVariables"][
            "ENVIRONMENT"
        ]
        == "staging"
    )
    assert owned_runtime.caller_environment(SPEC, BASE, data)["ENVIRONMENT"] == "staging"


def test_post_promotion_ui_role_trust_and_grants_are_verified(monkeypatch):
    from infra.stacks import durable_templates

    config = BASE
    data = bindings()
    planned = durable_templates.active_routing(
        SPEC,
        data["foundation"],
        data["versions"],
        config=config,
        owned_bindings=data,
    )["Resources"]["UiRole"]["Properties"]
    arn = f"arn:aws:iam::{SPEC['account_id']}:role/{SPEC['project']}/{SPEC['environment']}/Ui"
    iam = Mock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": arn,
            "Path": planned["Path"],
            "AssumeRolePolicyDocument": planned["AssumeRolePolicyDocument"],
        }
    }
    iam.list_role_policies.return_value = {"PolicyNames": ["runtime"]}
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    iam.get_role_policy.return_value = {
        "PolicyDocument": copy.deepcopy(planned["Policies"][0]["PolicyDocument"])
    }
    monkeypatch.setattr(
        durable_ops,
        "owned_stack",
        lambda *a, **kw: (None, {"Outputs": [{"OutputKey": "UiRoleArn", "OutputValue": arn}]}),
    )
    bundle = {"spec": SPEC, "config": config, "bindings": data}
    durable_ops.verify_ui_role(bundle, lambda *a: iam)
    iam.get_role_policy.return_value["PolicyDocument"]["Statement"].append(
        {"Effect": "Allow", "Action": "dynamodb:PutItem", "Resource": "*"}
    )
    with pytest.raises(VerificationError, match="grants differ"):
        durable_ops.verify_ui_role(bundle, lambda *a: iam)


# --- Default mode: local single-user -----------------------------------------------------------


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_default_mode_has_no_identity_and_every_execution_host_accepts_both_purposes(target):
    config = {**BASE, "runtime_target": target}
    stages = examples(SPEC, config)
    env = owned_runtime.environment(SPEC, config, bindings(target=target))
    assert env["EXECUTION_PURPOSE"] == "both"
    assert (
        stages["durable-runtime"]["Resources"]["Investigate"]["Properties"]["Environment"]["Variables"][
            "EXECUTION_PURPOSE"
        ]
        == "both"
    )
    connection = json.loads(stages["routing"]["Outputs"]["RuntimeConnection"]["Value"])
    assert connection["EXECUTION_PURPOSE"] == "both"
    if target == "agentcore":
        host = stages["agentcore-runtime"]["Resources"]["Runtime"]["Properties"]["EnvironmentVariables"]
        assert host["EXECUTION_PURPOSE"] == "both"


# --- Pluggable model provider (standalone target only) ----------------------------------------

MODEL_API = {"protocol": "openai", "base_url": "https://llm.example.invalid/v1", "bytes_per_token": 3.5}
MODEL_SECRET = {
    "arn": f"arn:aws:secretsmanager:{SPEC['bedrock_region']}:{SPEC['account_id']}:secret:{SPEC['project']}-{SPEC['environment']}/model-api-key-AbCdEf",
    "version_id": "c" * 32,
}


def api_spec(**api):
    base = {k: v for k, v in SPEC.items() if k != "model_arns"}
    return {
        **base,
        "model_provider": "model_api",
        "model_id": "example-model-v1",
        "model_api": {**MODEL_API, **api},
    }


def api_bindings(target="standalone", **secret):
    data = bindings(target=target)
    data["model_secret"] = {**MODEL_SECRET, **secret}
    return data


def test_model_api_environment_and_permissions_are_pinned_and_bedrock_free():
    spec, data = api_spec(), api_bindings()
    env = owned_runtime.environment(spec, BASE, data)
    assert json.loads(env["MODEL_API"]) == {
        "protocol": "openai",
        "base_url": MODEL_API["base_url"],
        "bytes_per_token": 3.5,
        "secret_arn": MODEL_SECRET["arn"],
        "secret_version": MODEL_SECRET["version_id"],
    }
    assert env["MODEL_API"] == json.dumps(json.loads(env["MODEL_API"]), sort_keys=True, separators=(",", ":"))
    assert env["BEDROCK_MODEL_ID"] == "example-model-v1" and env["BEDROCK_REGION"] == SPEC["bedrock_region"]
    assert "MODEL_API" not in owned_runtime.environment(SPEC, BASE, bindings())
    permissions = owned_runtime.model_permissions(spec, data)
    assert "bedrock" not in json.dumps(permissions)
    assert permissions[0] == {
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": MODEL_SECRET["arn"],
        "Condition": {"StringEquals": {"secretsmanager:VersionId": MODEL_SECRET["version_id"]}},
    }
    assert [p["Resource"] for p in permissions if p["Action"] == "lambda:InvokeFunction"] == [
        list(data["tools"].values())
    ]
    assert owned_runtime.caller_permissions(spec, BASE, data) == permissions


def test_model_api_anthropic_never_emits_bytes_per_token():
    value = json.loads(
        owned_runtime.environment(api_spec(protocol="anthropic"), BASE, api_bindings())["MODEL_API"]
    )
    assert value["protocol"] == "anthropic" and "bytes_per_token" not in value
    value = json.loads(
        owned_runtime.environment(
            {**api_spec(), "model_api": {"protocol": "openai", "base_url": MODEL_API["base_url"]}},
            BASE,
            api_bindings(),
        )["MODEL_API"]
    )
    assert "bytes_per_token" not in value


def test_model_api_renders_every_standalone_caller_without_model_grants():
    from infra.stacks import durable_templates

    spec = api_spec()
    _, _, artifacts, _ = examples(SPEC, BASE, include_bindings=True)
    data = api_bindings()
    runtime = durable_templates.runtime(spec, BASE, artifacts, data["foundation"], owned_bindings=data)
    routing = durable_templates.active_routing(
        spec, data["foundation"], data["versions"], config=BASE, owned_bindings=data
    )
    roles = [
        runtime["Resources"]["InvestigateRole"],
        routing["Resources"]["UiRole"],
    ]
    envs = [
        runtime["Resources"]["Investigate"]["Properties"]["Environment"]["Variables"],
        json.loads(routing["Outputs"]["RuntimeConnection"]["Value"]),
    ]
    for rendered in roles:
        assert "bedrock:" not in json.dumps(rendered)
    for env in envs:
        assert json.loads(env["MODEL_API"])["secret_version"] == MODEL_SECRET["version_id"]
    statements = [s for r in roles for s in r["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]]
    pinned = [s for s in statements if s["Resource"] == MODEL_SECRET["arn"]]
    assert pinned and all(
        s["Action"] == "secretsmanager:GetSecretValue"
        and s["Condition"] == {"StringEquals": {"secretsmanager:VersionId": MODEL_SECRET["version_id"]}}
        for s in pinned
    )
    # The default UI role holds the model key.
    assert any(
        s["Resource"] == MODEL_SECRET["arn"]
        for s in routing["Resources"]["UiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    )


def test_bedrock_fingerprint_is_unchanged_and_model_api_fingerprint_is_bound_to_endpoint_and_secret():
    import hashlib

    from infra.spec import digest

    data = bindings()
    config = {**BASE, "runtime_target": "standalone"}
    legacy = digest(
        {
            "model_id": SPEC["model_id"],
            "model_arns": SPEC["model_arns"],
            "tools": data["tools"],
            "limits": owned_runtime.asdict(owned_runtime.Limits(**config["runtime_limits"])),
            "runtime_target": "standalone",
            "release_id": SPEC["release_id"],
            "contracts": {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                for p in (
                    "agent-instruction.txt",
                    "schemas/fetch_logs.json",
                    "schemas/fetch_metrics.json",
                    "tracaus/diagnosis.py",
                    "tracaus/safety.py",
                )
            },
        }
    )
    assert owned_runtime.fingerprint(SPEC, config, data) == legacy
    assert owned_runtime.fingerprint({**SPEC, "model_provider": "bedrock"}, config, data) == legacy
    seen = {owned_runtime.fingerprint(api_spec(), config, api_bindings())}
    for spec, secret in (
        (api_spec(base_url="https://other.example.invalid/v1"), {}),
        (api_spec(protocol="anthropic"), {}),
        (api_spec(bytes_per_token=4), {}),
        (api_spec(), {"version_id": "d" * 32}),
        (api_spec(), {"arn": MODEL_SECRET["arn"].replace("AbCdEf", "ZzZzZz")}),
    ):
        seen.add(owned_runtime.fingerprint(spec, config, api_bindings(**secret)))
    assert len(seen) == 6 and legacy not in seen


def test_model_api_is_rejected_with_agentcore_on_every_render_path():
    spec, config = api_spec(), {**BASE, "runtime_target": "agentcore"}
    data = api_bindings(target="agentcore")
    artifact = {"bucket": "synthetic-bucket", "key": "releases/a.zip", "version_id": "synthetic"}
    for call in (
        lambda: owned_runtime.validate_bindings(spec, config, data),
        lambda: owned_runtime.environment(spec, config, data),
        lambda: owned_runtime.caller_environment(spec, config, data),
        lambda: owned_runtime.agentcore_release(spec, config, data, artifact),
    ):
        with pytest.raises(VerificationError, match="AgentCore"):
            call()


@pytest.mark.parametrize(
    "binding",
    [
        None,
        {"arn": MODEL_SECRET["arn"]},
        {**MODEL_SECRET, "extra": "x"},
        {**MODEL_SECRET, "version_id": "AWSCURRENT"},
        {**MODEL_SECRET, "version_id": ""},
        {**MODEL_SECRET, "arn": MODEL_SECRET["arn"].replace(SPEC["account_id"], "999999999999")},
        {**MODEL_SECRET, "arn": MODEL_SECRET["arn"].replace("model-api-key", "other-secret")},
        {**MODEL_SECRET, "arn": MODEL_SECRET["arn"].replace("eu-central-1", "us-east-1")},
        {**MODEL_SECRET, "arn": MODEL_SECRET["arn"] + "/extra"},
    ],
)
def test_model_api_requires_exact_owned_secret_and_immutable_version(binding):
    data = bindings()
    if binding is not None:
        data["model_secret"] = binding
    with pytest.raises(VerificationError, match="model API"):
        owned_runtime.validate_bindings(api_spec(), BASE, data)
    with pytest.raises(VerificationError, match="model API"):
        owned_runtime.model_permissions(api_spec(), data)


def test_bedrock_ignores_model_secret_binding_and_never_needs_it():
    data = api_bindings()
    env = owned_runtime.environment(SPEC, BASE, data)
    assert "MODEL_API" not in env
    assert MODEL_SECRET["arn"] not in json.dumps(owned_runtime.model_permissions(SPEC, data))
