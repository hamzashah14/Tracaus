"""Incident status lookup scoped to a caller's instances."""

import time

import pytest

from tracaus import status

IID = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
INCIDENT = "a" * 32


class Table:
    def __init__(self, items):
        self.items = items

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": self.items.get((Key["PK"], Key["SK"]))}


class Resource:
    def __init__(self, items):
        self.items = items

    def Table(self, name):
        return Table(self.items)


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE", "incidents")
    monkeypatch.setenv("MONITOR_REGION", "eu-central-1")
    items = {
        (f"INCIDENT#{INCIDENT}", "META"): {
            "instance_id": IID,
            "status": "DONE",
            "ttl": int(time.time()) + 3600,
        }
    }
    monkeypatch.setattr(status.boto3, "resource", lambda *args, **kwargs: Resource(items))
    return items


def test_default_lookup_is_unscoped(storage):
    assert status.load(INCIDENT)["instance_id"] == IID


def test_scoped_lookup_returns_an_incident_on_a_listed_instance(storage):
    assert status.load(INCIDENT, allowed=frozenset({IID, OTHER}))["instance_id"] == IID


def test_scoped_lookup_hides_an_incident_on_another_instance(storage):
    assert status.load(INCIDENT, allowed=frozenset({OTHER})) is None


def test_scoped_lookup_with_an_empty_list_sees_nothing(storage):
    assert status.load(INCIDENT, allowed=frozenset()) is None
