"""Write one local heartbeat for a customer timer; performs no AWS calls."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", args.instance_id):
        raise ValueError("Declare a valid inventory instance")
    record = {
        "type": "tracaus.collector-heartbeat",
        "instance_id": args.instance_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with args.file.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
