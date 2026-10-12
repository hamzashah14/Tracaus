"""Run a deployment operation with one explicit SDK session across all adapters."""

import argparse
import runpy
import sys

import boto3

MODULES = (
    "infra",
    "infra.deploy.durable_ops",
    "scripts.build_pipeline",
    "scripts.build_lambdas",
    "scripts.build_observations",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="")
    parser.add_argument("--module", required=True, choices=MODULES)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    # Setting AWS_PROFILE alone can still allow ambient providers to win.
    # All boto3.client paths, including remote canaries, use this same session.
    boto3.setup_default_session(profile_name=args.profile or None)
    sys.argv = [args.module, *args.arguments]
    if len(sys.argv) > 1 and sys.argv[1] == "--":
        sys.argv.pop(1)
    runpy.run_module(args.module, run_name="__main__")


if __name__ == "__main__":
    main()
