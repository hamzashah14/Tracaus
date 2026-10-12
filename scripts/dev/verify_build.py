"""Build each package set twice, require identical manifests, then import it with only bundled dependencies."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from infra.observations import build_release  # noqa: E402
from infra.spec import load  # noqa: E402
from scripts.build_lambdas import PIPELINE_FUNCTIONS, build  # noqa: E402

CATALOG = ROOT / "config/metric-catalog.json"
WHEELS = ROOT / ".build/wheels"
LAMBDA_IMPORT = "import boto3,lambda_function; print(boto3.__version__)"

# Per target: output dir prefix (.build/<dir>-a and -b), builder(output), the statement run under
# `python -S` inside the extracted ZIP, the determinism error text and an optional PASS note.
TARGETS = {
    "tools": {
        "dir": "lambda",
        # Runs the documented build_lambdas.py CLI (default functions), not just build().
        "build": lambda out: subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_lambdas.py"),
                "--output",
                str(out),
                "--wheelhouse",
                str(WHEELS),
            ],
            check=True,
        ),
        "import": "import boto3,lambda_function; from tracaus.metrics import catalog; "
        "assert isinstance(catalog(),list); print(boto3.__version__)",
        "mismatch": "Independent builds do not match",
    },
    "pipeline": {
        "dir": "pipeline",
        "build": lambda out: build(PIPELINE_FUNCTIONS, out, CATALOG, WHEELS),
        "import": LAMBDA_IMPORT,
        "mismatch": "Pipeline packages differ between independent builds",
    },
    "agentcore": {
        "dir": "agentcore",
        "build": lambda out: build(("incident_investigate",), out, CATALOG, WHEELS, architecture="arm64"),
        "import": "import boto3,tracaus_agentcore,tracaus.execution,tracaus.runtime; print(boto3.__version__)",
        "mismatch": "AgentCore host builds differ",
        "note": "; live AgentCore boot pending",
    },
    "observation": {
        "dir": "observation",
        "build": lambda out: build_release(load(ROOT / "examples/observability.example.json"), out, WHEELS),
        "import": LAMBDA_IMPORT,
        "mismatch": "Observation packages differ between independent builds",
    },
}


def verify(name):
    target = TARGETS[name]
    outputs = [ROOT / ".build" / f"{target['dir']}-{suffix}" for suffix in ("a", "b")]
    for output in outputs:
        target["build"](output)
    a, b = [json.loads((path / "manifest.json").read_text()) for path in outputs]
    if a != b:
        raise AssertionError(target["mismatch"])
    versions = set()
    for function, artifact in a["functions"].items():
        with tempfile.TemporaryDirectory(prefix=f"tracaus-{name}-") as temp:
            with zipfile.ZipFile(outputs[0] / artifact["artifact"]) as archive:
                # Our locked SDK dependencies are pure Python. Reject architecture-specific
                # binaries rather than mistake an import on macOS for proof of Linux ARM64.
                if a["architecture"] == "arm64" and any(
                    p.endswith((".so", ".dll", ".dylib", ".pyd")) for p in archive.namelist()
                ):
                    raise AssertionError(
                        "Host verification requires a native Linux ARM64 runner for binary dependencies"
                    )
                archive.extractall(temp)
            # -S removes site-packages: the SDK and shared library must come from the
            # ZIP. No client is constructed or credentials accessed.
            run = subprocess.run(
                [sys.executable, "-S", "-c", target["import"]],
                cwd=temp,
                env={"PATH": os.defpath, "AWS_EC2_METADATA_DISABLED": "true"},
                text=True,
                capture_output=True,
            )
            if run.returncode:
                raise AssertionError(f"Isolated import failed for {function}: {run.stderr.strip()}")
            versions.add(run.stdout.strip())
    count = len(a["functions"])
    print(
        f"PASS {name}: {count} identical ZIP pair(s) and {count} isolated import(s) "
        f"(boto3 {', '.join(sorted(versions))}){target.get('note', '')}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", choices=list(TARGETS), help="default: all, in this order")
    failed = []
    for name in parser.parse_args().targets or TARGETS:
        try:
            verify(name)
        except (AssertionError, subprocess.CalledProcessError) as error:
            print(f"FAIL {name}: {error}")
            failed.append(name)
    if failed:
        raise SystemExit(f"FAIL: {', '.join(failed)}")


if __name__ == "__main__":
    main()
