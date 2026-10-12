"""Read-only: ask AWS where each ECS service's logs go and whether an ALB is in front, and print what to paste."""

import json
import re

from botocore.exceptions import ClientError

MAX_GROUPS = 8  # deployment.json takes at most this many log groups per service


def awslogs(task_definition):
    """(log groups written through awslogs, other log drivers in use) for a task definition."""
    groups, others = [], set()
    for container in task_definition.get("containerDefinitions", []):
        config = container.get("logConfiguration") or {}
        if config.get("logDriver") == "awslogs":
            group = (config.get("options") or {}).get("awslogs-group")
            if group and group not in groups:
                groups.append(group)
        else:
            others.add(config.get("logDriver") or "none")
    return groups, sorted(others)


def application_balancers(elbv2, target_group_arns):
    """{target group ARN: (application load balancer ARNs, other load balancer ARNs)}."""
    found = {}
    for arn in target_group_arns:
        balancers = elbv2.describe_target_groups(TargetGroupArns=[arn])["TargetGroups"][0].get(
            "LoadBalancerArns", []
        )
        found[arn] = (
            [b for b in balancers if ":loadbalancer/app/" in b],
            [b for b in balancers if ":loadbalancer/app/" not in b],
        )
    return found


def discover(spec, clients):
    """One result per ECS entry: the suggested blocks and notes. `spec` is raw JSON, so a draft file works."""
    region = spec["monitor_region"]
    ecs, elbv2 = clients("ecs", region), clients("elbv2", region)
    results, owners = [], {}
    for item in spec.get("ecs_services", []):
        notes, suggestion = [], {"existing_log_groups": [], "load_balancer": None}
        try:
            response = ecs.describe_services(cluster=item["cluster"], services=[item["service"]])
        except ClientError as error:
            if error.response["Error"]["Code"] != "ClusterNotFoundException":
                raise
            response = {"services": []}
        service = next((s for s in response["services"] if s.get("serviceName") == item["service"]), None)
        if service is None or service.get("status") != "ACTIVE":
            results.append(
                {"id": item["id"], "notes": ["The cluster or service was not found or is not ACTIVE."]}
            )
            continue
        task_definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])[
            "taskDefinition"
        ]
        groups, others = awslogs(task_definition)
        if others:
            notes.append(
                f"Some containers log with {', '.join(others)}; Tracaus can read only awslogs groups."
            )
        if not groups:
            notes.append("No awslogs group found; logs stay unread (existing_log_groups can be empty).")
        if len(groups) > MAX_GROUPS:
            notes.append(
                f"{len(groups)} groups found; deployment.json takes at most {MAX_GROUPS}. Keep the ones that matter."
            )
        suggestion["existing_log_groups"] = [{"name": g} for g in groups[:MAX_GROUPS]]
        for group in groups:
            owners.setdefault(group, []).append(item["id"])
        attached = [lb["targetGroupArn"] for lb in service.get("loadBalancers", []) if "targetGroupArn" in lb]
        balancers = application_balancers(elbv2, attached) if attached else {}
        usable = [(tg, apps[0]) for tg, (apps, _) in balancers.items() if apps]
        if usable:
            tg, lb = usable[0]
            suggestion["load_balancer"] = {"load_balancer_arn": lb, "target_group_arn": tg}
            if len(usable) > 1:
                notes.append("The service is attached to several ALB target groups; the first is suggested.")
        elif attached and any(others for _, others in balancers.values()):
            notes.append(
                "The service is behind a load balancer that is not an ALB; only ALBs are supported, use null."
            )
        elif attached:
            notes.append(
                "The service's target group is not attached to any load balancer yet, so there is no ALB to monitor."
            )
        else:
            notes.append("No load balancer is attached to the service: load_balancer is null.")
        declared = item.get("load_balancer", "unset")
        if declared is None and suggestion["load_balancer"]:
            notes.append(
                "deployment.json says no ALB, but the service sits behind one; set load_balancer to monitor it."
            )
        if isinstance(declared, dict) and declared.get("target_group_arn") not in attached:
            notes.append("deployment.json names a target group the service does not use.")
        listed = {g["name"] for g in item.get("existing_log_groups", [])}
        if "existing_log_groups" in item and listed != set(groups[:MAX_GROUPS]):
            notes.append(
                "deployment.json lists different log groups than the current task definition writes to."
            )
        results.append(
            {
                "id": item["id"],
                "service": f"{item['cluster']}/{item['service']}",
                "task_definition": re.sub(r"^.*/", "", service["taskDefinition"]),
                **suggestion,
                "notes": notes,
            }
        )
    for group, ids in owners.items():
        if len(ids) > 1:
            for result in results:
                if result["id"] in ids:
                    result["notes"].append(
                        f"{group} is also used by {', '.join(i for i in ids if i != result['id'])}; "
                        "one log group can belong to one target, so give each service its own."
                    )
    return results


def render(results):
    lines = []
    for result in results:
        head = f"{result['id']}  {result.get('service', '')}  {result.get('task_definition', '')}".rstrip()
        lines.append(head)
        for key in ("existing_log_groups", "load_balancer"):
            if key in result:
                lines.append(
                    f'  "{key}": {json.dumps(result[key], indent=2).replace(chr(10), chr(10) + "  ")}'
                )
        lines += [f"  note: {note}" for note in result["notes"]]
        lines.append("")
    return "\n".join(lines)


def main(spec, clients):
    from infra.deploy.verify import VerificationError

    if clients("sts", spec["monitor_region"]).get_caller_identity()["Account"] != spec["account_id"]:
        raise VerificationError("Wrong AWS account; no reads permitted")
    return discover(spec, clients)
