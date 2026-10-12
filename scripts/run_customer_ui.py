"""Launch the loopback UI from generated connection references and a scoped profile."""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parents[1]


def environment(path, profile, *, session_factory=boto3.Session):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Connection file must be private (chmod 600) and not a symlink")
    data = json.loads(path.read_text())
    values = data["environment"]
    if not isinstance(values, dict) or any(
        not isinstance(k, str) or not k.isupper() or not isinstance(v, str) for k, v in values.items()
    ):
        raise ValueError("Invalid generated UI connection")
    role = data.get("ui_role_arn")
    account = values["EXPECTED_ACCOUNT_ID"]
    if not isinstance(role, str) or not role.startswith(f"arn:aws:iam::{account}:role/"):
        raise ValueError("Generated connection has no scoped UI role")
    identity = (
        session_factory(profile_name=profile)
        .client("sts", region_name=values["MONITOR_REGION"])
        .get_caller_identity()
    )
    expected = f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[-1]}/"
    if identity["Account"] != account or not identity["Arn"].startswith(expected):
        raise ValueError("UI profile must assume the generated UI role; do not use deployment credentials")
    result = dict(os.environ)
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "TRACAUS_TEAM_FILE",
    ):
        result.pop(key, None)
    result.update(values)
    result.update(AWS_PROFILE=profile, AWS_EC2_METADATA_DISABLED="true", PYTHON_DOTENV_DISABLED="1")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--team-file", type=Path)
    args = parser.parse_args()
    try:
        env = environment(args.connection, args.profile)
        if args.team_file:
            team_path = args.team_file
            if team_path.is_symlink() or not team_path.is_file() or team_path.stat().st_mode & 0o022:
                raise ValueError("Team file must be a regular file that group and others cannot write")
            env["TRACAUS_TEAM_FILE"] = str(team_path.resolve())
            env.pop("APP_PASSWORD", None)  # Team mode signs in through the identity provider only.
        elif not env.get("APP_PASSWORD"):
            print(
                "APP_PASSWORD is not set: export it (12+ characters) in this shell first; .env is not loaded.",
                file=sys.stderr,
            )
        os.chdir(ROOT)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(ROOT / "app.py"),
                "--server.address",
                "127.0.0.1",
                "--server.headless",
                "true",
            ],
            env,
        )
        return 0
    except (ValueError, KeyError, TypeError, OSError, BotoCoreError, ClientError) as exc:
        print(
            str(exc)
            if isinstance(exc, ValueError)
            else f"UI launch failed ({type(exc).__name__}); no connection assumed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
