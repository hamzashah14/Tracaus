"""Things a MagicMock table cannot show: DynamoDB reads numbers back as Decimal and rejects reserved words.

Both mistakes passed the mock-based tests and only failed against a DynamoDB-compatible backend: an
unaliased `ttl` in a condition (a reserved word) and `format(Decimal, "012d")` in the retry paths.
"""

import ast
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from tracaus.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
# AWS's list of DynamoDB reserved words, one per line: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ReservedWords.html
RESERVED = {
    w.strip().upper() for w in (Path(__file__).parent / "dynamodb_reserved_words.txt").read_text().split()
}
EXPRESSIONS = {
    "ConditionExpression",
    "UpdateExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
}
GRAMMAR = {"SET", "REMOVE", "ADD", "DELETE", "AND", "OR", "NOT", "BETWEEN", "IN"}
IID = "a" * 32


def unaliased_reserved_words(expression):
    # A name starting with "#" or ":" is an alias or a value; a name followed by "(" is a function.
    words = re.findall(r"(?<![#:\w])([A-Za-z_]\w*)(?!\w)(?!\s*\()", expression)
    return sorted({w for w in words if w.upper() in RESERVED and w.upper() not in GRAMMAR})


def test_every_dynamodb_expression_aliases_reserved_words():
    found = []
    for path in [*(ROOT / "tracaus").glob("*.py"), *(ROOT / "lambda").glob("*/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            pairs = []
            if isinstance(node, ast.keyword) and node.arg in EXPRESSIONS:
                pairs.append((node.value, node.value))
            if isinstance(node, ast.Dict):
                pairs += [
                    (k, v)
                    for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value in EXPRESSIONS
                ]
            for _, value in pairs:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    hits = unaliased_reserved_words(value.value)
                    if hits:
                        found.append(f"{path.relative_to(ROOT)}:{value.lineno} {hits}: {value.value[:60]}")
    assert not found, "alias these with ExpressionAttributeNames:\n" + "\n".join(found)


def test_the_scan_catches_the_mistake_it_exists_for():
    assert unaliased_reserved_words("attribute_exists(PK) AND ttl>:now") == ["ttl"]
    assert unaliased_reserved_words("attribute_exists(PK) AND #t>:now") == []
    assert unaliased_reserved_words("SET #s=:x REMOVE lease_owner ADD fencing_token :one") == []


def decimal_claim(**extra):
    # What Ledger.claim() hands back after reading the row: numbers are Decimal, not int.
    return {
        "PK": "INCIDENT#" + IID,
        "status": "RUNNING",
        "lease_owner": "worker",
        "fencing_token": Decimal(1),
        "deadline_epoch": Decimal(10_000_000_000),
        "lease_until": Decimal(5),
        "attempts": 1,
        "ttl": Decimal(20_000_000_000),
        **extra,
    }


def test_retry_formats_a_decimal_deadline():
    client = MagicMock()
    assert Ledger("test", client, MagicMock()).retry(decimal_claim()) == "RETRY"
    values = client.transact_write_items.call_args.kwargs["TransactItems"][1]["Update"][
        "ExpressionAttributeValues"
    ]
    assert values[":deadline_key"]["S"] == f"{10_000_000_000:012d}#{IID}"


def test_lease_recovery_formats_a_decimal_deadline():
    client = MagicMock()
    assert Ledger("test", client, MagicMock()).recover(decimal_claim(), 100) == "RETRY"
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    keys = [
        v["ExpressionAttributeValues"][":deadline_key"]["S"]
        for r in items
        for v in r.values()
        if ":deadline_key" in v.get("ExpressionAttributeValues", {})
    ]
    assert keys == [f"{10_000_000_000:012d}#{IID}"]
