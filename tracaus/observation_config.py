"""Strict secret-free, release-owned observation inventory; no network access."""

import json
import re
from urllib.parse import urlsplit


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value)


def validate(value, instances):
    required = {
        "enabled",
        "interval_minutes",
        "canary_interval_minutes",
        "receipt_deadline_seconds",
        "email_receipt_max_age_hours",
        "services",
    }
    if not isinstance(value, dict) or set(value) != required or type(value["enabled"]) is not bool:
        raise ValueError("Observation settings require explicit schedule, canary, receipt and service fields")
    for key, low, high in (
        ("interval_minutes", 5, 15),
        ("canary_interval_minutes", 60, 1440),
        ("receipt_deadline_seconds", 300, 1800),
        ("email_receipt_max_age_hours", 24, 168),
    ):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("Invalid observation timing")
    if value["receipt_deadline_seconds"] < value["interval_minutes"] * 60:
        raise ValueError("Receipt deadline must include one observer interval")
    if value["canary_interval_minutes"] % 60 or 1440 % value["canary_interval_minutes"]:
        raise ValueError("Canary interval must divide a UTC day in whole hours")
    services = value["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= 10:
        raise ValueError("Configure 1 to 10 observed services")
    heartbeat_groups = {}
    ids, inventory = set(), {item["id"]: item for item in instances}
    for service in services:
        if not isinstance(service, dict) or set(service) != {
            "id",
            "instance_id",
            "owner",
            "routes",
            "collector_metric_id",
            "heartbeat_log_group",
            "freshness_seconds",
        }:
            raise ValueError("Invalid observed service fields")
        if not identifier(service["id"]) or service["id"] in ids or service["instance_id"] not in inventory:
            raise ValueError("Observed service must have a unique ID and inventory instance")
        ids.add(service["id"])
        if not identifier(service["owner"]) or not identifier(service["collector_metric_id"]):
            raise ValueError("Declare service owner and collector metric ID")
        if type(service["freshness_seconds"]) is not int or not 300 <= service["freshness_seconds"] <= 1800:
            raise ValueError("Invalid freshness interval")
        if service["heartbeat_log_group"] not in inventory[service["instance_id"]]["log_groups"]:
            raise ValueError("Dedicated heartbeat log group must be in the declared inventory")
        previous = heartbeat_groups.setdefault(service["instance_id"], service["heartbeat_log_group"])
        if previous != service["heartbeat_log_group"]:
            raise ValueError("Services on an instance must share its collector heartbeat group")
        routes = service["routes"]
        if not isinstance(routes, list) or not 1 <= len(routes) <= 3:
            raise ValueError("Configure 1 to 3 readiness/dependency routes")
        route_ids = set()
        for route in routes:
            if not isinstance(route, dict) or set(route) != {
                "id",
                "url",
                "timeout_seconds",
                "latency_ms",
                "statuses",
            }:
                raise ValueError("Invalid probe fields")
            if not identifier(route["id"]) or route["id"] in route_ids or route["id"] == "telemetry":
                raise ValueError("Probe IDs must be unique per service")
            route_ids.add(route["id"])
            url = route["url"]
            if not isinstance(url, str) or len(url) > 512 or any(ord(c) <= 32 for c in url):
                raise ValueError("Invalid probe URL")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parsed.hostname)
                or not parsed.path.startswith("/")
                or "\\" in parsed.path
            ):
                raise ValueError("Probe requires a credential-free HTTPS route on port 443")
            if type(route["timeout_seconds"]) is not int or not 1 <= route["timeout_seconds"] <= 5:
                raise ValueError("Probe timeout must be 1 to 5 seconds")
            if (
                type(route["latency_ms"]) is not int
                or not 1 <= route["latency_ms"] <= route["timeout_seconds"] * 1000
            ):
                raise ValueError("Invalid probe latency threshold")
            if (
                not isinstance(route["statuses"], list)
                or not route["statuses"]
                or any(type(s) is not int or not 200 <= s <= 299 for s in route["statuses"])
            ):
                raise ValueError("Probe requires explicit successful HTTP status codes")
    # Lambda environment has a 4KiB total limit. Keep a separate, small observer config.
    if len(json.dumps(value, separators=(",", ":")).encode()) > 2500:
        raise ValueError("Observation inventory exceeds the bounded Lambda configuration size")
    return value
