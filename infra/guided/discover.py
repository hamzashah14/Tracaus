"""Read-only discovery of what already runs in the account.

Every function takes `clients(service, region)` and returns plain data. A call that AWS denies or throttles
gives state `unknown` with the error code, never `missing`: AccessDenied does not mean absent. Anything that
cannot be reached at all (no network, no credentials) raises DiscoveryError. Values that came from AWS are
checked against the same patterns the settings schema uses, and the ones that fail are skipped with a note."""

import re

from botocore.exceptions import BotoCoreError, ClientError

from infra.ecs_discover import application_balancers, awslogs

INSTANCE = re.compile(r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})")
ECS_NAME = re.compile(r"[A-Za-z0-9_-]{1,255}")
LOG_GROUP = re.compile(r"[A-Za-z0-9_./#-]{1,512}")
MAX_INSTANCES, MAX_CLUSTERS, MAX_SERVICES = 200, 50, 200
MEMORY, DISK, PROCESS = "mem_used_percent", "disk_used_percent", "procstat_lookup_pid_count"


class DiscoveryError(Exception):
    """AWS could not be reached or no credentials are set. The message never carries a response body."""


def _code(error):
    return error.response.get("Error", {}).get("Code", "Unknown")


def _reach(call, *args, **kwargs):
    """Run a call; turn transport problems into DiscoveryError and keep ClientError for the caller."""
    try:
        return call(*args, **kwargs)
    except ClientError:
        raise
    except BotoCoreError as error:
        raise DiscoveryError(f"cannot reach AWS ({type(error).__name__})") from None


def _unknown(error, **extra):
    return {"state": "unknown", "reason": _code(error), **extra}


def caller(clients, region):
    """Who the credentials are, and the IAM principal ARN to trust, when it can be worked out."""
    identity = _reach(clients("sts", region).get_caller_identity)
    account, arn = identity["Account"], identity["Arn"]
    result = {"account": account, "arn": arn, "kind": "other", "principal_arn": None}
    user = re.fullmatch(r"arn:aws:iam::\d{12}:user/(.+)", arn)
    session = re.fullmatch(r"arn:aws:sts::\d{12}:assumed-role/([^/]+)/[^/]+", arn)
    if arn == f"arn:aws:iam::{account}:root":
        result["kind"] = "root"
    elif user:
        result.update(kind="user", principal_arn=arn)
    elif session:
        result["kind"] = "role"
        try:
            role = _reach(clients("iam", region).get_role, RoleName=session[1])["Role"]["Arn"]
            result["principal_arn"] = role
        except ClientError:
            pass  # the role path cannot be read, so the caller decides how to narrow trust
    return result


def ec2_instances(clients, region):
    """Running and stopped instances. Windows instances are listed but not supported."""
    ec2 = clients("ec2", region)
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}],
        PaginationConfig={"MaxItems": MAX_INSTANCES + 1},  # lazy: no page beyond the cap is requested
    )
    items, skipped, capped = [], 0, False
    try:
        for page in _reach(lambda: list(pages)):
            for reservation in page.get("Reservations", []):
                for item in reservation.get("Instances", []):
                    if not INSTANCE.fullmatch(item.get("InstanceId", "")):
                        skipped += 1
                        continue
                    tags = {t.get("Key"): t.get("Value", "") for t in item.get("Tags", [])}
                    windows = item.get("Platform") == "windows" or "Windows" in item.get(
                        "PlatformDetails", ""
                    )
                    items.append(
                        {
                            "id": item["InstanceId"],
                            "name": tags.get("Name", ""),
                            "state": item.get("State", {}).get("Name", ""),
                            "platform": "windows" if windows else "linux",
                            "profile_arn": item.get("IamInstanceProfile", {}).get("Arn"),
                            "supported": not windows,
                        }
                    )
    except ClientError as error:
        return _unknown(error, items=[], skipped=0, capped=False)
    items.sort(key=lambda i: (i["name"], i["id"]))
    if len(items) > MAX_INSTANCES:
        items, capped = items[:MAX_INSTANCES], True
    return {"state": "found", "items": items, "skipped": skipped, "capped": capped}


def cwagent_facts(clients, region, instance_id):
    """What the CloudWatch agent publishes for one instance, judged against the metrics Tracaus reads.

    Tracaus reads exact dimension sets. A disk metric that carries extra dimensions (device, fstype) is
    reported separately, because it would not be found."""
    metrics = clients("cloudwatch", region)
    pages = metrics.get_paginator("list_metrics").paginate(
        Namespace="CWAgent",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        RecentlyActive="PT3H",
    )
    facts = {"state": "missing", "any": False, "memory": False, "disk_paths": [], "disk_wide": [], "exes": []}
    try:
        found = [m for page in _reach(lambda: list(pages)) for m in page.get("Metrics", [])]
    except ClientError as error:
        return _unknown(error, **{k: v for k, v in facts.items() if k != "state"})
    disk, wide, exes = set(), set(), set()
    for metric in found:
        dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
        names, name = set(dims), metric.get("MetricName")
        if name == MEMORY and names == {"InstanceId"}:
            facts["memory"] = True
        elif name == DISK and "path" in dims:
            (disk if names == {"InstanceId", "path"} else wide).add(dims["path"])
        elif (
            name == PROCESS
            and names == {"InstanceId", "exe", "pid_finder"}
            and dims["pid_finder"] == "native"
        ):
            exes.add(dims["exe"])
    facts.update(
        state="found" if found else "missing",
        any=bool(found),
        disk_paths=sorted(disk),
        disk_wide=sorted(wide - disk),
        exes=sorted(exes),
    )
    return facts


def log_group_candidates(clients, region, instance_ids, group_limit=100, call_limit=300):
    """Existing log groups that hold a stream named after each instance.

    Bounded: at most `group_limit` groups and `call_limit` stream lookups, so a large account cannot make this
    slow. When a bound is hit the result says so, and the caller asks the user instead."""
    logs = clients("logs", region)
    found = {instance: [] for instance in instance_ids}
    try:
        groups = []
        pages = logs.get_paginator("describe_log_groups").paginate(
            PaginationConfig={"MaxItems": group_limit + 1}  # lazy: no page beyond the cap is requested
        )
        for page in _reach(lambda: list(pages)):
            groups += [g["logGroupName"] for g in page.get("logGroups", []) if "logGroupName" in g]
        capped = len(groups) > group_limit
        groups, calls = groups[:group_limit], 0
        for group in groups:
            if not LOG_GROUP.fullmatch(group):
                continue
            for instance in instance_ids:
                if calls >= call_limit:
                    capped = True
                    break
                calls += 1
                streams = _reach(
                    logs.describe_log_streams, logGroupName=group, logStreamNamePrefix=instance, limit=1
                ).get("logStreams", [])
                if streams and streams[0].get("logStreamName", "").startswith(instance):
                    found[instance].append(group)
    except ClientError as error:
        return _unknown(error, groups=found, capped=False)
    return {"state": "found", "groups": found, "capped": capped}


def ecs_services(clients, region):
    """Every ACTIVE ECS service with where its logs go and whether an Application Load Balancer is in front."""
    ecs, elbv2 = clients("ecs", region), clients("elbv2", region)
    items, capped = [], False
    try:
        arns = [
            arn
            for page in _reach(
                lambda: list(
                    ecs.get_paginator("list_clusters").paginate(
                        PaginationConfig={"MaxItems": MAX_CLUSTERS + 1}
                    )
                )
            )
            for arn in page.get("clusterArns", [])
        ]
        if len(arns) > MAX_CLUSTERS:
            arns, capped = arns[:MAX_CLUSTERS], True
        insights = {}
        for start in range(0, len(arns), 100):
            for cluster in _reach(
                ecs.describe_clusters, clusters=arns[start : start + 100], include=["SETTINGS"]
            )["clusters"]:
                setting = {s["name"]: s["value"] for s in cluster.get("settings", [])}
                insights[cluster["clusterName"]] = setting.get("containerInsights") in {"enabled", "enhanced"}
        definitions = {}
        for name in sorted(insights):
            if len(items) >= MAX_SERVICES:
                capped = True
                break
            if not ECS_NAME.fullmatch(name):
                continue
            service_arns = [
                arn
                for page in _reach(
                    lambda: list(
                        ecs.get_paginator("list_services").paginate(
                            cluster=name, PaginationConfig={"MaxItems": MAX_SERVICES + 1}
                        )
                    )
                )
                for arn in page.get("serviceArns", [])
            ]
            for start in range(0, len(service_arns), 10):
                if len(items) >= MAX_SERVICES:
                    capped = True
                    break
                batch = _reach(ecs.describe_services, cluster=name, services=service_arns[start : start + 10])
                for service in batch.get("services", []):
                    if service.get("status") != "ACTIVE" or not ECS_NAME.fullmatch(
                        service.get("serviceName", "")
                    ):
                        continue
                    if len(items) >= MAX_SERVICES:
                        capped = True
                        continue
                    task = service["taskDefinition"]
                    if task not in definitions:
                        definitions[task] = _reach(ecs.describe_task_definition, taskDefinition=task)[
                            "taskDefinition"
                        ]
                    groups, others = awslogs(definitions[task])
                    notes = []
                    kept = [g for g in groups if LOG_GROUP.fullmatch(g)]
                    if len(kept) != len(groups):
                        notes.append("A log group name with unexpected characters was skipped.")
                    items.append(
                        {
                            "cluster": name,
                            "service": service["serviceName"],
                            "launch_type": service.get("launchType", "capacity-provider"),
                            "container_insights": insights[name],
                            "log_groups": kept,
                            "other_log_drivers": others,
                            "target_groups": [
                                b["targetGroupArn"]
                                for b in service.get("loadBalancers", [])
                                if "targetGroupArn" in b
                            ],
                            "notes": notes,
                        }
                    )
        for item in items:
            _balancer(item, elbv2)
    except ClientError as error:
        return _unknown(error, items=[], capped=False)
    items.sort(key=lambda i: (i["cluster"], i["service"]))
    return {"state": "found", "items": items, "capped": capped}


def _balancer(item, elbv2):
    """Set `load_balancer` ("none", "alb", "other" or "unknown") and the ARNs when it is an ALB."""
    item["load_balancer"], item["alb"] = "none", None
    if not item["target_groups"]:
        return
    try:
        attached = application_balancers(elbv2, item["target_groups"])
    except ClientError as error:
        item["load_balancer"] = "unknown"
        item["notes"].append(f"Could not read the load balancer ({_code(error)}).")
        return
    for target_group, (apps, others) in attached.items():
        if apps:
            item["load_balancer"] = "alb"
            item["alb"] = {"load_balancer_arn": apps[0], "target_group_arn": target_group}
            if len(item["target_groups"]) > 1:
                item["notes"].append("More than one target group; the first with an ALB is offered.")
            return
        if others:
            item["load_balancer"] = "other"
            item["notes"].append("A Network or Gateway load balancer is in front; Tracaus supports ALB only.")
            return
    item["load_balancer"] = "unknown"
    item["notes"].append("The target group is not attached to a load balancer yet.")


def bedrock_profiles(clients, region):
    """System-defined inference profiles with the ARNs a deployment must list for them."""
    bedrock = clients("bedrock", region)
    items = []
    try:
        pages = _reach(
            lambda: list(
                bedrock.get_paginator("list_inference_profiles").paginate(typeEquals="SYSTEM_DEFINED")
            )
        )
        for page in pages:
            for profile in page.get("inferenceProfileSummaries", []):
                arns = [profile["inferenceProfileArn"]] + [m["modelArn"] for m in profile.get("models", [])]
                if profile.get("status") == "ACTIVE" and len(arns) <= 10:
                    items.append(
                        {
                            "id": profile["inferenceProfileId"],
                            "name": profile.get("inferenceProfileName", ""),
                            "arns": arns,
                        }
                    )
    except ClientError as error:
        return _unknown(error, items=[])
    return {"state": "found", "items": sorted(items, key=lambda i: i["id"])}


def resolve_model(clients, region, identifier):
    """The `model_arns` for one model ID: a profile (with its destination models) or a foundation model."""
    bedrock = clients("bedrock", region)
    try:
        profile = _reach(bedrock.get_inference_profile, inferenceProfileIdentifier=identifier)
        arns = [profile["inferenceProfileArn"]] + [m["modelArn"] for m in profile.get("models", [])]
        return {"state": "found", "kind": "profile", "arns": arns}
    except ClientError as error:
        if _code(error) not in {"ValidationException", "ResourceNotFoundException"}:
            return _unknown(error, arns=[])
    try:
        model = _reach(bedrock.get_foundation_model, modelIdentifier=identifier)
        return {"state": "found", "kind": "foundation", "arns": [model["modelDetails"]["modelArn"]]}
    except ClientError as error:
        if _code(error) in {"ValidationException", "ResourceNotFoundException"}:
            return {"state": "missing", "arns": []}
        return _unknown(error, arns=[])


def role_state(clients, arn):
    """`found`, `missing`, `conflict` (a role of that name exists under another path) or `unknown`."""
    name = arn.rsplit("/", 1)[-1]
    try:
        role = _reach(clients("iam", None).get_role, RoleName=name)["Role"]
    except ClientError as error:
        return {"state": "missing"} if _code(error) == "NoSuchEntity" else _unknown(error)
    return {"state": "found" if role["Arn"] == arn else "conflict"}


def log_groups_exist(clients, region, names):
    """{name: True/False/None}: does each log group exist? None when the read was denied."""
    logs, result = clients("logs", region), {}
    for name in names:
        if not LOG_GROUP.fullmatch(name):
            result[name] = None
            continue
        try:
            groups = _reach(logs.describe_log_groups, logGroupNamePrefix=name).get("logGroups", [])
        except ClientError:
            result[name] = None
            continue
        result[name] = any(g.get("logGroupName") == name for g in groups)
    return result
