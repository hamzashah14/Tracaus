"""Shared by the guided commands: the error type, plain-language stage names and the way other programs run
(argument lists only, never a shell)."""

import contextlib
import json
import os
import subprocess


class GuidedError(Exception):
    """A failure with the four things a user needs: what failed, why, what is safe, and what to do next."""

    def __init__(self, what, cause="", safe="", next_step="", code=1):
        super().__init__(what)
        self.what, self.cause, self.safe, self.next_step, self.code = what, cause, safe, next_step, code


STAGES = {
    "foundation-tools": "Regional bootstrap stack for the tool functions",
    "foundation-monitor": "Regional bootstrap stack for monitoring",
    "durable-foundation": "Incident database, evidence storage, keys and queues",
    "model-secret": "Pin the version of your model API key secret",
    "owned-tools": "Read-only tool functions (logs, metrics, ECS), fixed to your server list",
    "agentcore-runtime": "AgentCore runtime",
    "agentcore-endpoint": "AgentCore endpoint",
    "durable-runtime": "Alert pipeline and investigation worker (investigations stay paused)",
    "observation-foundation": "Observers: queues and subscriptions",
    "observation-runtime": "Observers: probe functions",
    "health-bootstrap": "Observers: first health checks",
    "candidate-verification": "Check the built release end to end, without a model call",
    "staging-canary": "One paid model request through the real tools",
    "routing": "Route your CloudWatch alarms and stop events to Tracaus",
    "observations": "Observers: schedules and alarms",
    "registration-verification": "Check subscriptions and routing",
    "manual-acceptance": "Your own delivery check (see docs/ACCEPTANCE.md)",
}

# Nothing reaches Tracaus before this stage, so an earlier failure leaves your alarms and servers untouched.
ROUTING_STAGE = "routing"


STATIC_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def aws_env(profile=None):
    """The environment for a child that talks to AWS. A named profile cannot lose to ambient static keys."""
    env = {**os.environ, "AWS_EC2_METADATA_DISABLED": "true", "PYTHON_DOTENV_DISABLED": "1"}
    if profile:
        for key in STATIC_KEYS:
            env.pop(key, None)
        env["AWS_PROFILE"] = profile
    return env


def run(argv, *, cwd=None, env=None, timeout=900):
    """`(exit code, combined output)`. A timeout gives code 124 and says so; the child is killed."""
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as error:
        return 127, f"could not start the program ({type(error).__name__})"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def parse_json_object(text):
    """The first JSON object in a program's output, ignoring anything printed before or after it."""
    start = text.index("{")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


@contextlib.contextmanager
def guided_lock(directory):
    """One guided command per work folder. Released by the OS when the process ends, even after a crash."""
    if os.name != "posix":
        raise GuidedError(
            "The guided commands need macOS or Linux",
            "file locking is not available",
            "Nothing was changed.",
            "Use WSL.",
        )
    import fcntl

    fd = os.open(os.path.join(directory, ".guided.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuidedError(
                "Another guided command is using this folder",
                "a second run would race the first",
                "Nothing was changed by this run.",
                "Wait for the other command to finish, or close it.",
            ) from None
        yield
    finally:
        os.close(fd)
