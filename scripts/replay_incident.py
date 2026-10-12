"""Inspect or submit a reviewed replay; terminal incidents cannot rerun paid work."""

import argparse
import json
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.deploy.verify import VerificationError, assert_account  # noqa: E402
from infra.spec import load, name  # noqa: E402
from tracaus.ledger import Ledger  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", type=Path, help="Previously saved exact review JSON")
    args = parser.parse_args()
    try:
        spec = load(args.spec)
        if spec["reference_only"]:
            raise VerificationError("Synthetic reference cannot replay cloud incidents")
        sts = boto3.client("sts", region_name=spec["monitor_region"])
        assert_account(sts, spec)
        store = Ledger(name(spec, "incidents"), region_name=spec["monitor_region"])
        plan = store.replay_plan(args.event_id)
        if args.apply:
            reviewed = json.loads(args.apply.read_text())
            if reviewed != plan:
                raise VerificationError("Replay plan changed; review it again")
            result = store.replay(reviewed, sts.get_caller_identity()["Arn"])
        else:
            result = plan
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print("Replay review saved" if not args.apply else "Replay intent recorded")
        return 0
    except Exception:
        print("Replay refused; no success receipt was produced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
