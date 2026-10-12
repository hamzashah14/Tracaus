"""Write the local tools file (see docs) from a deployment spec. Makes no AWS call.

The log groups and metric catalog come from the same helpers the deployment pipeline uses, so local chat is
scoped exactly like the deployed tools.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infra import spec as deployment  # noqa: E402 — runnable from outside the repository
from tracaus import local_tools  # noqa: E402


def build(spec):
    value = {
        "version": 1,
        "monitor_region": spec["monitor_region"],
        "log_prefix": deployment.log_prefix(spec),
        "instances": [item["id"] for item in spec["instances"]],
        "log_groups": deployment.log_groups(spec),
        "metric_catalog": deployment.metric_catalog(spec),
    }
    if deployment.existing_log_groups(spec):
        value["existing_log_groups"] = deployment.existing_log_groups(spec)
    return value


def write(path, value, force=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if force else os.O_EXCL)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)  # an overwritten file keeps its old mode otherwise
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="deployment spec JSON")
    parser.add_argument("--out", type=Path, required=True, help="local tools file to create")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        value = build(deployment.load(args.spec))
        local_tools.parse(value)  # the exact validation the UI applies
        write(args.out, value, args.force)
    except FileExistsError:
        print(f"{args.out} already exists; pass --force to overwrite it.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"Local tools file not written: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}. Set TRACAUS_LOCAL_TOOLS={args.out} with ENVIRONMENT=development.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
