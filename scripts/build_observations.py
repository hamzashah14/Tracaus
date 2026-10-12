"""Build customer-owned observation observers without AWS access."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infra.deploy.observations import build_release  # noqa: E402
from infra.spec import load  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    spec = load(args.spec)
    if "observability" not in spec:
        raise ValueError("Declare observability configuration before building")
    build_release(spec, args.output, args.wheelhouse)


if __name__ == "__main__":
    main()
