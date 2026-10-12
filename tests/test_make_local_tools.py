import json
import stat

import pytest

from scripts import make_local_tools
from tests.helpers import ROOT
from tracaus import local_tools

SPEC = ROOT / "examples/deployment.example.json"
EXAMPLE = ROOT / "examples/local-tools.example.json"
IID = "i-0123456789abcdef0"


def generate(out, *extra):
    return make_local_tools.main(["--spec", str(SPEC), "--out", str(out), *extra])


def test_generated_file_loads_with_the_spec_derived_scope(tmp_path):
    out = tmp_path / "local-tools.json"
    assert generate(out) == 0
    config = local_tools.load(str(out))
    assert config.log_prefix == "/tracaus/staging" and config.monitor_region == "eu-central-1"
    assert config.instances == [IID]
    assert config.log_groups == [
        f"/tracaus/staging/{IID}/{name}" for name in ("application", "nginx-access", "nginx-error")
    ]
    assert len(config.metric_catalog) == 6 and json.loads(out.read_text())["version"] == 1


def test_generated_file_is_private(tmp_path):
    out = tmp_path / "local-tools.json"
    generate(out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_existing_file_is_not_overwritten_without_force(tmp_path, capsys):
    out = tmp_path / "local-tools.json"
    out.write_text("keep me")
    out.chmod(0o644)
    assert generate(out) == 1 and out.read_text() == "keep me"
    assert "--force" in capsys.readouterr().err
    assert generate(out, "--force") == 0
    assert local_tools.load(str(out)) and stat.S_IMODE(out.stat().st_mode) == 0o600


def test_symlink_target_is_never_followed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("keep me")
    out = tmp_path / "link.json"
    out.symlink_to(target)
    assert generate(out, "--force") == 1 and target.read_text() == "keep me"


def test_invalid_spec_writes_nothing(tmp_path, capsys):
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({**json.loads(SPEC.read_text()), "account_id": "not-an-account"}))
    out = tmp_path / "local-tools.json"
    assert make_local_tools.main(["--spec", str(spec), "--out", str(out)]) == 1
    assert not out.exists() and "account_id" in capsys.readouterr().err


def test_published_example_is_valid_and_has_the_generated_shape(tmp_path):
    out = tmp_path / "local-tools.json"
    generate(out)
    assert json.loads(EXAMPLE.read_text()).keys() == json.loads(out.read_text()).keys()
    assert local_tools.load(str(EXAMPLE)).instances == [IID]


def test_generator_makes_no_aws_call(tmp_path):
    # tests/conftest.py replaces boto3.client and sockets with failing stubs for every test.
    assert generate(tmp_path / "local-tools.json") == 0


@pytest.mark.parametrize("missing", ["--spec", "--out"])
def test_arguments_are_required(missing, tmp_path):
    args = {"--spec": str(SPEC), "--out": str(tmp_path / "x.json")}
    del args[missing]
    with pytest.raises(SystemExit):
        make_local_tools.main([item for pair in args.items() for item in pair])
