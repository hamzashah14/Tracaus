"""Build the Lambda packages, write the IAM template, look up ECS logs; deploy through infra.deploy.durable_ops."""

import argparse
import sys
from pathlib import Path

from infra.deploy import release
from infra.deploy.verify import VerificationError
from infra.spec import (
    ROOT,
    ecs_targets,
    existing_log_groups,
    load,
    log_groups,
    metric_catalog,
    tool_functions,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Build the inventory-bound read-only tools")
    build_parser.add_argument("--spec", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--wheelhouse", type=Path, required=True)
    pipeline_parser = commands.add_parser("build-pipeline", help="Build the durable pipeline Lambda packages")
    pipeline_parser.add_argument("--output", type=Path, default=ROOT / ".build/pipeline")
    pipeline_parser.add_argument("--wheelhouse", type=Path, required=True)
    observers_parser = commands.add_parser(
        "build-observations", help="Build the customer-owned observer packages"
    )
    observers_parser.add_argument("--spec", type=Path, required=True)
    observers_parser.add_argument("--output", type=Path, required=True)
    observers_parser.add_argument("--wheelhouse", type=Path, required=True)
    iam_parser = commands.add_parser(
        "bootstrap-iam",
        help="Write the CloudFormation template that creates the roles named in deployment.json",
    )
    iam_parser.add_argument("--spec", type=Path, required=True)
    iam_parser.add_argument("--output", type=Path, required=True)
    iam_parser.add_argument(
        "--operator-trust",
        action="append",
        help="Principal ARN that may assume the operator role (repeatable)",
    )
    iam_parser.add_argument(
        "--ui-trust", action="append", help="Principal ARN that may assume the UI principal role (repeatable)"
    )
    iam_parser.add_argument(
        "--agentcore", action="store_true", help="Also allow the AgentCore runtime stages"
    )
    iam_parser.add_argument(
        "--instance-role", action="store_true", help="Also create the instance role and profile for the agent"
    )
    discover_parser = commands.add_parser(
        "ecs-discover",
        help="Read-only: show where each ECS service's logs go and whether an ALB is in front",
    )
    discover_parser.add_argument("--spec", type=Path, required=True)
    discover_parser.add_argument("--profile", help="AWS profile (the operator role)")
    args = parser.parse_args()
    if args.command == "ecs-discover":
        try:
            import json

            import boto3

            from infra.deploy import ecs_discover
            from infra.deploy.aws import clients

            raw = json.loads(args.spec.read_text())
            if args.profile:
                session = boto3.Session(profile_name=args.profile)
                factory = lambda service, region: session.client(service, region_name=region)  # noqa: E731
            else:
                factory = clients
            print(ecs_discover.render(ecs_discover.main(raw, factory)))
        except (VerificationError, ValueError, KeyError, OSError) as exc:
            print(f"ECS discovery failed ({type(exc).__name__}); nothing was changed.", file=sys.stderr)
            return 1
        return 0
    if args.command == "bootstrap-iam":
        try:
            from infra.stacks import bootstrap_iam

            spec = load(args.spec)
            template = bootstrap_iam.render(
                spec,
                operator_trust=args.operator_trust,
                ui_trust=args.ui_trust,
                agentcore=args.agentcore,
                instance_role=args.instance_role,
            )
            release.write_json(args.output, template)
        except (VerificationError, ValueError, KeyError, OSError) as exc:
            print(f"IAM template failed ({type(exc).__name__}); nothing was written to AWS.", file=sys.stderr)
            return 1
        print(
            f"Wrote {args.output}. As an AWS administrator, deploy it once in {spec['monitor_region']}:\n"
            f"  aws cloudformation deploy --template-file {args.output} "
            f"--stack-name {spec['project']}-{spec['environment']}-iam --capabilities CAPABILITY_NAMED_IAM "
            f"--region {spec['monitor_region']}"
        )
        return 0
    if args.command == "build-pipeline":
        from scripts.build_lambdas import PIPELINE_FUNCTIONS, build

        build(PIPELINE_FUNCTIONS, args.output, ROOT / "config/metric-catalog.json", args.wheelhouse)
        return 0
    if args.command == "build-observations":
        from infra.deploy.observations import build_release

        spec = load(args.spec)
        if "observability" not in spec:
            raise ValueError("Declare observability configuration before building")
        build_release(spec, args.output, args.wheelhouse)
        return 0
    try:
        from scripts.build_lambdas import build

        spec = load(args.spec)
        args.output.mkdir(parents=True, exist_ok=True)
        release.write_json(args.output / "metric-catalog.json", metric_catalog(spec))
        release.write_json(args.output / "log-scope.json", log_groups(spec))
        existing = existing_log_groups(spec)
        if existing:
            release.write_json(args.output / "existing-log-groups.json", existing)
        ecs = ecs_targets(spec)
        if ecs:
            release.write_json(args.output / "ecs-targets.json", ecs)
        build(
            tool_functions(spec),
            args.output,
            args.output / "metric-catalog.json",
            args.wheelhouse,
            args.output / "log-scope.json",
            existing_path=args.output / "existing-log-groups.json" if existing else None,
            ecs_path=args.output / "ecs-targets.json" if ecs else None,
        )
        return 0
    except (VerificationError, ValueError, KeyError, OSError) as exc:
        print(f"Tool build failed ({type(exc).__name__}); no deployment occurred.", file=sys.stderr)
        if isinstance(exc, VerificationError):
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
