"""Build deterministic Lambda ZIPs from hash-verified wheels, shared code and catalog."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tracaus.metrics import validate_catalog  # noqa: E402 — runnable from outside the repository

FUNCTIONS = ("fetch_logs", "fetch_metrics", "fetch_ecs")
PIPELINE_FUNCTIONS = (
    "incident_ingress",
    "incident_dispatch",
    "incident_investigate",
    "incident_initial",
    "incident_report",
    "incident_reconcile",
)
OBSERVATION_FUNCTIONS = ("observation_probe", "observation_canary", "observation_receipt")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(
    functions,
    output,
    catalog_path,
    wheelhouse,
    log_scope_path=None,
    architecture="x86_64",
    existing_path=None,
    ecs_path=None,
):
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Lambda builds require Python 3.12.")
    if architecture not in {"x86_64", "arm64"}:
        raise ValueError("Unsupported artifact architecture")
    catalog = json.dumps(
        validate_catalog(json.loads(catalog_path.read_text())), sort_keys=True, separators=(",", ":")
    ).encode()
    lock = ROOT / "requirements/lambda.lock"
    output.mkdir(parents=True, exist_ok=True)
    # An isolated download destination excludes stale, extra wheels. pip verifies
    # hashes even when reading wheels from a caller-provided offline wheelhouse.
    with tempfile.TemporaryDirectory(prefix="tracaus-wheels-") as temp:
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_aarch64" if architecture == "arm64" else "manylinux2014_x86_64",
            "--python-version",
            "312",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--dest",
            temp,
            "-r",
            str(lock),
        ]
        if wheelhouse:
            command += ["--no-index", "--find-links", str(wheelhouse.resolve())]
        subprocess.run(command, check=True)
        entries = {}
        wheels = {}
        for wheel in sorted(Path(temp).glob("*.whl")):
            wheels[wheel.name] = digest(wheel.read_bytes())
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.endswith("/") or ".data/" in name or name.endswith(".pyc"):
                        continue
                    if name.startswith("/") or ".." in Path(name).parts or name in entries:
                        raise ValueError("Invalid or conflicting wheel entry")
                    entries[name] = archive.read(name)
        for path in sorted((ROOT / "tracaus").glob("*.py")):
            entries[str(path.relative_to(ROOT))] = path.read_bytes()
        entries["config/metric-catalog.json"] = catalog
        scope = json.loads(log_scope_path.read_text()) if log_scope_path else []
        if not isinstance(scope, list) or any(
            not isinstance(group, str) or not group.startswith("/") for group in scope
        ):
            raise ValueError("Log scope must be an explicit list of log group names")
        entries["config/log-scope.json"] = json.dumps(sorted(set(scope)), separators=(",", ":")).encode()
        if existing_path:
            entries["config/existing-log-groups.json"] = json.dumps(
                json.loads(existing_path.read_text()), sort_keys=True, separators=(",", ":")
            ).encode()
        if ecs_path:
            entries["config/ecs-targets.json"] = json.dumps(
                json.loads(ecs_path.read_text()), sort_keys=True, separators=(",", ":")
            ).encode()
        entries["requirements/lambda.lock"] = lock.read_bytes()
        for filename in (
            "agent-instruction.txt",
            "schemas/fetch_logs.json",
            "schemas/fetch_metrics.json",
            "tracaus_agentcore.py",
            *(["schemas/fetch_ecs.json"] if ecs_path else []),
        ):
            entries[filename] = (ROOT / filename).read_bytes()
        manifest = {
            "python": "3.12",
            "architecture": architecture,
            "lock_sha256": digest(lock.read_bytes()),
            "wheels": wheels,
            "functions": {},
        }
        for function in functions:
            source = ROOT / "lambda" / function / "lambda_function.py"
            files = {**entries, "lambda_function.py": source.read_bytes()}
            artifact = output / f"{function}.zip"
            with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in sorted(files.items()):
                    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            manifest["functions"][function] = {
                "artifact": artifact.name,
                "sha256": digest(artifact.read_bytes()),
                "bytes": artifact.stat().st_size,
                "source_files": {
                    name: digest(data)
                    for name, data in files.items()
                    if name.startswith("tracaus/")
                    or name
                    in {
                        "lambda_function.py",
                        "config/metric-catalog.json",
                        "config/log-scope.json",
                        "config/existing-log-groups.json",
                        "config/ecs-targets.json",
                        "agent-instruction.txt",
                        "schemas/fetch_logs.json",
                        "schemas/fetch_metrics.json",
                        "schemas/fetch_ecs.json",
                        "tracaus_agentcore.py",
                    }
                },
            }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--function", choices=FUNCTIONS + PIPELINE_FUNCTIONS + OBSERVATION_FUNCTIONS, action="append"
    )
    parser.add_argument("--output", type=Path, default=ROOT / ".build/lambda")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config/metric-catalog.json")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--log-scope", type=Path)
    parser.add_argument("--ecs-targets", type=Path)
    parser.add_argument("--architecture", choices=("x86_64", "arm64"), default="x86_64")
    args = parser.parse_args()
    build(
        args.function or (FUNCTIONS if args.ecs_targets else FUNCTIONS[:2]),
        args.output,
        args.catalog,
        args.wheelhouse,
        args.log_scope,
        args.architecture,
        ecs_path=args.ecs_targets,
    )


if __name__ == "__main__":
    main()
