"""Optional local tools mode for developers: the UI process runs Tracaus's own tool handlers in-process.

Chat works with the developer's AWS credentials instead of deployed tool Lambda versions. The handlers, the
runtime's request/response validation and the allowlists are the deployed ones; only the transport differs.
Development, standalone and password-protected UI only. Deployed code never imports this module.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from tracaus.config import INSTANCE, REGION
from tracaus.metrics import validate_catalog

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("fetch_logs", "fetch_metrics")
FIELDS = {"version", "monitor_region", "log_prefix", "instances", "log_groups", "metric_catalog"}
OPTIONAL = {"existing_log_groups"}
PREFIX = re.compile(r"/[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64}){0,3}")
GROUP = re.compile(r"[A-Za-z0-9_\-./#]{1,512}")  # fetch_logs' own pattern and CloudWatch's length limit
FUNCTION = re.compile(
    r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:tracaus-local-(fetch_logs|fetch_metrics):[1-9][0-9]*"
)
MAX_FILE_BYTES = 1 << 20
MAX_GROUPS = 1000
MAX_CATALOG_BYTES = 262144  # tracaus.metrics.catalog's own file limit
# The deployed tool Lambdas have a 120 s timeout; the runtime's own deadline still caps each call at 30 s.
CONTEXT = SimpleNamespace(get_remaining_time_in_millis=lambda: 120_000)
_LOCK = threading.Lock()
_CLIENTS = {}


def parse(value):
    """Strict validation. Messages name fields only, never submitted values."""
    if not isinstance(value, dict):
        raise ValueError("Local tools file must contain a JSON object.")
    if value.keys() - FIELDS - OPTIONAL:
        raise ValueError(f"Unknown field(s): {', '.join(sorted(value.keys() - FIELDS - OPTIONAL))}.")
    if FIELDS - value.keys():
        raise ValueError(f"Missing field(s): {', '.join(sorted(FIELDS - value.keys()))}.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported version (expected 1).")
    region, prefix = value["monitor_region"], value["log_prefix"]
    if not isinstance(region, str) or not REGION.fullmatch(region):
        raise ValueError("Invalid monitor_region.")
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise ValueError("Invalid log_prefix.")
    instances, groups = value["instances"], value["log_groups"]
    if (
        not isinstance(instances, list)
        or not 1 <= len(instances) <= 100
        or any(not isinstance(item, str) or not INSTANCE.fullmatch(item) for item in instances)
        or len(set(instances)) != len(instances)
    ):
        raise ValueError("Invalid instances: 1-100 unique EC2 instance IDs are required.")
    owned = tuple(f"{prefix}/{item}/" for item in instances)
    existing = _existing(value.get("existing_log_groups", {}), instances, prefix)
    if (
        not isinstance(groups, list)
        or not (1 if not existing else 0) <= len(groups) <= MAX_GROUPS
        or any(not isinstance(g, str) or not GROUP.fullmatch(g) or g.endswith("/") for g in groups)
        or len(set(groups)) != len(groups)
        or any(not g.startswith(owned) for g in groups)
    ):
        raise ValueError("Invalid log_groups: each must be unique and under log_prefix/<listed instance>/.")
    try:
        catalog = validate_catalog(value["metric_catalog"])
    except ValueError as error:
        raise ValueError(f"Invalid metric_catalog: {error}") from None
    if {item["instance_id"] for item in catalog} - set(instances):
        raise ValueError("Invalid metric_catalog: a descriptor belongs to an instance that is not listed.")
    if len(json.dumps(catalog)) > MAX_CATALOG_BYTES:
        raise ValueError("Invalid metric_catalog: too large.")
    return LocalConfig(region, prefix, list(instances), list(groups), catalog, existing)


def _existing(value, instances, prefix):
    """{instance: {group: "instance" | "all"}}: groups that already exist, read but never created."""
    bad = ValueError(
        "Invalid existing_log_groups: listed instances, valid names outside log_prefix, one reader per streams=all group."
    )
    if not isinstance(value, dict) or any(key not in instances for key in value):
        raise bad
    seen = {}
    for groups in value.values():
        if not isinstance(groups, dict) or not groups or len(groups) > 8:
            raise bad
        for name, mode in groups.items():
            if (
                not isinstance(name, str)
                or not GROUP.fullmatch(name)
                or name.endswith("/")
                or name.startswith(f"{prefix}/")
                or mode not in ("instance", "all")
            ):
                raise bad
            seen.setdefault(name, []).append(mode)
    if any("all" in modes and len(modes) > 1 for modes in seen.values()):
        raise bad
    return {key: dict(groups) for key, groups in value.items()}


def load(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ValueError("Local tools file cannot be read.") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Local tools file is too large.")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Local tools file is not valid JSON.") from None
    return parse(value)


def problems(settings):
    """Local mode is valid only for a development, standalone, password-protected UI with no deployed pins."""
    found = []
    if settings.environment != "development":
        found.append("Local tools run only with ENVIRONMENT=development.")
    if settings.runtime_target != "standalone":
        found.append("Local tools require RUNTIME_TARGET=standalone.")
    if settings.logs_arn or settings.metrics_arn:
        found.append(
            "Remove LOGS_TOOL_ARN and METRICS_TOOL_ARN: TRACAUS_LOCAL_TOOLS replaces the deployed tools."
        )
    if os.getenv("LOG_CURSOR_SECRET_ARN"):
        found.append("Remove LOG_CURSOR_SECRET_ARN: local tools keep their pagination secret in memory.")
    try:
        config = load(settings.local_tools)
    except ValueError as error:
        found.append(f"Fix the TRACAUS_LOCAL_TOOLS file: {error}")
    else:
        if settings.allowed_ids and set(settings.allowed_ids.split(",")) != set(config.instances):
            found.append(
                "ALLOWED_INSTANCE_IDS must list the same instances as the TRACAUS_LOCAL_TOOLS file, or be unset."
            )
    return found


@dataclass
class LocalConfig:
    monitor_region: str
    log_prefix: str
    instances: list
    log_groups: list
    metric_catalog: list
    existing: dict = field(default_factory=dict)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def client(self):
        """One private temp directory and one set of loaded handlers per distinct file content."""
        with _LOCK:
            if self.fingerprint not in _CLIENTS:
                _CLIENTS[self.fingerprint] = LocalLambdaClient(self)
            return _CLIENTS[self.fingerprint]

    def tools(self, region, account, allowed, policy, reserve, anchor=None, access_guard=None):
        """The unmodified LambdaTools, pointed at synthetic numeric versions that this process answers."""
        from tracaus.runtime import LambdaTools

        arns = {tool: f"arn:aws:lambda:{region}:{account}:function:tracaus-local-{tool}:1" for tool in TOOLS}
        return LambdaTools(
            region,
            account,
            arns,
            allowed,
            policy,
            reserve,
            anchor,
            client=self.client(),
            access_guard=access_guard,
        )


def _load(tool):
    # Both handlers are named lambda_function, so they are loaded by path under private names and never
    # registered in sys.modules or put on sys.path.
    spec = importlib.util.spec_from_file_location(
        f"tracaus_local_{tool}", ROOT / "lambda" / tool / "lambda_function.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _environment(values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class LocalLambdaClient:
    """The one boto3 Lambda client method LambdaTools uses, answered by in-process handlers.

    The handlers read their scope from the environment at call time, so it is set for the duration of each
    call only (serialised by a lock) and restored afterwards: nothing leaks into the host process.
    """

    def __init__(self, config):
        self._directory = tempfile.TemporaryDirectory(prefix="tracaus-local-tools-")  # mkdtemp: mode 0700
        self.directory = Path(self._directory.name)
        self.env = {
            "ALLOWED_INSTANCE_IDS": ",".join(config.instances),
            "LOG_SCOPE_FILE": self._write("log-scope.json", sorted(config.log_groups)),
            "EXISTING_LOG_GROUPS_FILE": self._write("existing-log-groups.json", config.existing)
            if config.existing
            else "",  # an empty value also hides one set in the host environment
            "METRIC_CATALOG_FILE": self._write("metric-catalog.json", config.metric_catalog),
        }
        self._secret = secrets.token_urlsafe(48)  # discovery cursors; there is no Secrets Manager locally
        self.modules = {tool: _load(tool) for tool in TOOLS}
        for module in self.modules.values():
            module.MONITOR_REGION = config.monitor_region  # read at import time by the deployed handlers
        self.modules["fetch_logs"].LOG_GROUP_PREFIX = config.log_prefix

    def _write(self, name, value):
        path = self.directory / name
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump(value, handle)
        return str(path)

    def invoke(self, FunctionName, InvocationType, Payload):
        match = FUNCTION.fullmatch(FunctionName) if isinstance(FunctionName, str) else None
        if not match or InvocationType != "RequestResponse":
            raise ValueError("Local tools answer only synchronous tracaus-local function versions.")
        tool = match[1]
        try:
            with _LOCK:
                secret = os.environ.get("LOG_CURSOR_SECRET") or self._secret
                with _environment({**self.env, "LOG_CURSOR_SECRET": secret}):
                    envelope = self.modules[tool].lambda_handler(json.loads(Payload), CONTEXT)
            body = json.dumps(envelope).encode()
        except Exception as error:
            # Same outcome as a real unhandled Lambda error: the runtime reports INVALID_TOOL_RESPONSE.
            logging.getLogger("tracaus").warning("local_tool=%s error=%s", tool, type(error).__name__)
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(b'{"errorType":"LocalToolError"}'),
            }
        return {"StatusCode": 200, "Payload": io.BytesIO(body)}
