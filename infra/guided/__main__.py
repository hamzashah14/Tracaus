"""`python -m infra.guided setup|deploy|status|ui`: the guided path through docs/DEPLOY.md.

Every command prints numbered steps, explains each failure (what, why, what is safe, what next) and exits with
0 done, 1 failed, 2 waiting on a named step, 3 refused by a safety rule, 130 interrupted."""

import argparse
import sys
import traceback
from pathlib import Path

from infra.automation import private_dir
from infra.guided import deploy as guided_deploy
from infra.guided import setup as guided_setup
from infra.guided.common import GuidedError, guided_lock
from infra.guided.console import Console
from infra.guided.discover import DiscoveryError
from infra.guided.prompt import AnswersPrompter, ConsolePrompter, NeedsAnswer, Refused, YesPrompter
from infra.spec import ROOT

DEFAULT_DIR = ROOT / ".local" / "customer"


def parser():
    p = argparse.ArgumentParser(prog="tracaus", description=__doc__.splitlines()[0])
    commands = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--work-dir", type=Path, default=DEFAULT_DIR, help="where the settings live (under .local/)"
    )
    common.add_argument("--debug", action="store_true", help="show the full error trace")
    common.add_argument(
        "--yes", action="store_true", help="accept defaults; never answers IAM or paid questions"
    )
    common.add_argument("--answers", type=Path, help="replay an answers.json instead of asking")
    common.add_argument(
        "--allow-iam-bootstrap", action="store_true", help="agree, without asking, to create the IAM roles"
    )
    common.add_argument(
        "--allow-paid-canary", action="store_true", help="agree, without asking, to the paid model request"
    )
    common.add_argument(
        "--allow-paid-canary-retry",
        action="store_true",
        help="also agree, without asking, to a SECOND paid request after an expired or unclear first one",
    )
    setup = commands.add_parser(
        "setup", parents=[common], help="read the account, ask the rest, write the settings"
    )
    setup.add_argument("--skip-install", action="store_true", help="do not download the Lambda packages")
    setup.add_argument("--admin-profile", help="profile allowed to create the IAM roles (used once)")
    deploy = commands.add_parser(
        "deploy", parents=[common], help="check, apply with live progress, handle every stop"
    )
    deploy.add_argument("--skip-check", action="store_true", help="skip the read-only permission check")
    commands.add_parser(
        "status", parents=[common], help="read-only: what exists, what is routed, what to do next"
    )
    ui = commands.add_parser("ui", parents=[common], help="open the UI on 127.0.0.1")
    ui.add_argument("--team-file", type=Path, help="team mode: the team.toml allowlist")
    ui.add_argument(
        "--password-prompt", action="store_true", help="type the UI password instead of generating one"
    )
    return p


def prompter_for(args, console, directory):
    allowed = {"iam-bootstrap"} if args.allow_iam_bootstrap else set()
    if args.allow_paid_canary:
        allowed.add("paid-canary")
    if args.allow_paid_canary_retry:
        allowed.add("paid-canary-retry")
    if args.answers:
        return AnswersPrompter(args.answers, allowed)
    if args.yes:
        return YesPrompter(allowed)
    return ConsolePrompter(console, allowed, defaults=guided_deploy.read_json(directory / "answers.json", {}))


def dispatch(args, console, directory):
    prompt = prompter_for(args, console, directory)
    if args.command == "setup":
        options = guided_setup.Options(
            skip_install=args.skip_install, admin_profile=args.admin_profile, yes=args.yes
        )
        return guided_setup.Setup(console, prompt, directory, options).run()
    if args.command == "deploy":
        return guided_deploy.Deploy(
            console, prompt, directory, guided_deploy.DeployOptions(yes=args.yes, skip_check=args.skip_check)
        ).run()
    if args.command == "status":
        return guided_deploy.status(console, directory)
    return guided_deploy.launch_ui(
        console, prompt, directory, team_file=args.team_file, prompt_password=args.password_prompt
    )


def main(argv=None):
    args = parser().parse_args(argv)
    console = Console()
    try:
        directory = private_dir(args.work_dir)
    except Exception as error:  # noqa: BLE001 - VerificationError and OSError both mean "cannot use this folder"
        console.explain(
            "This work folder cannot be used",
            str(error),
            "Nothing was changed.",
            f"Use a folder under {ROOT / '.local'}.",
        )
        return 1
    console.open_log(directory / "guided.log")
    try:
        if args.command in {"setup", "deploy"}:  # status and ui never block them (or each other)
            with guided_lock(directory):
                return dispatch(args, console, directory)
        return dispatch(args, console, directory)
    except GuidedError as error:
        console.explain(error.what, error.cause, error.safe, error.next_step)
        return error.code
    except Refused as error:
        console.explain(
            "Stopped by a safety rule",
            str(error),
            "Nothing was changed.",
            "Run the command again with the flag it names, if you mean it.",
        )
        return 3
    except NeedsAnswer as error:
        console.explain(
            "A question could not be answered",
            str(error),
            "Nothing was changed by this question.",
            "Run in a terminal, or pass --answers FILE.",
        )
        return 1
    except DiscoveryError as error:
        console.explain(
            "Could not reach AWS",
            str(error),
            "Files are written whole or not at all. If setup had already started the IAM role stack, it keeps "
            "running in AWS: run setup again to watch it.",
            "Check the network, the profile and that you are signed in (aws sso login).",
        )
        return 1
    except KeyboardInterrupt:
        console.say(
            "\nInterrupted. Files are written whole or not at all, and a running deployment keeps its journal."
        )
        console.say("Run the same command again to continue.")
        return 130
    except Exception as error:  # noqa: BLE001 - last resort: say what happened without a raw trace
        if args.debug:
            traceback.print_exc()
        console.explain(
            "Something unexpected went wrong",
            type(error).__name__,
            "Nothing was deleted. Files are written whole or not at all.",
            "Run again with --debug to see the trace, and report it if it repeats.",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
