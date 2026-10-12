"""Team file loader, authorization, hourly limit and audit line."""

import json
import os
import subprocess
import sys

import pytest

from tests.helpers import ROOT
from tracaus import team

IID = "i-0123456789abcdef0"
OTHER = "i-0fedcba9876543210"
ALLOWED = {IID, OTHER}
ISSUER = "https://login.example.invalid"
NOW = 1_800_000_000.0

TEXT = f"""issuer = "{ISSUER}"

[[users]]
sub = "user-1"
role = "investigator"
instances = ["{IID}"]

[[users]]
sub = "user-2"
role = "viewer"
instances = ["{IID}", "{OTHER}"]
"""


def write(tmp_path, text=TEXT, mode=0o600):
    path = tmp_path / "team.toml"
    path.write_text(text)
    os.chmod(path, mode)
    return str(path)


def bump(path):
    """Move the modification time forward so the loader sees an edit."""
    info = os.stat(path)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))


def claims(**changes):
    """Verified claims for user-1; pass `name=...` (Ellipsis) to delete a claim."""
    value = {"iss": ISSUER, "sub": "user-1", "auth_time": NOW - 60, "amr": ["pwd", "mfa"]}
    value.update(changes)
    return {key: item for key, item in value.items() if item is not ...}


def roster(tmp_path, text=TEXT):
    return team.load(write(tmp_path, text), ALLOWED)


@pytest.fixture(autouse=True)
def fresh_state():
    team._CACHE.clear()
    team._HITS.clear()


# ---- loading ---------------------------------------------------------------


def test_valid_file_loads_with_defaults(tmp_path):
    loaded = roster(tmp_path)
    assert loaded.issuer == ISSUER
    assert loaded.require_mfa is True
    assert loaded.session_seconds == 8 * 3600
    assert loaded.chat_per_hour == 20
    assert loaded.users["user-1"] == team.Access("user-1", "investigator", frozenset({IID}))
    assert loaded.users["user-2"].instances == frozenset({IID, OTHER})


def test_explicit_settings_override_the_defaults(tmp_path):
    text = TEXT.replace(
        f'issuer = "{ISSUER}"\n',
        f'issuer = "{ISSUER}"\nrequire_mfa = false\nsession_hours = 2\n\n[limits]\nchat_per_user_per_hour = 5\n',
    )
    loaded = roster(tmp_path, text)
    assert (loaded.require_mfa, loaded.session_seconds, loaded.chat_per_hour) == (False, 7200, 5)


def variant(old, new):
    text = TEXT.replace(old, new)
    assert text != TEXT, f"variant did not change the text: {old!r}"
    return text


BAD_FILES = {
    "unknown top-level key": "extra = 1\n" + TEXT,
    "unknown user key": variant('role = "viewer"', 'role = "viewer"\nnote = "x"'),
    "unknown limits key": variant(f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\n[limits]\nspeed = 1\n'),
    "missing issuer": variant(f'issuer = "{ISSUER}"\n', ""),
    "http issuer": variant("https://", "http://"),
    "issuer with a query": variant(ISSUER, ISSUER + "?x=1"),
    "issuer not a string": variant(f'issuer = "{ISSUER}"', "issuer = 5"),
    "no users": f'issuer = "{ISSUER}"\n',
    "users not a list": f'issuer = "{ISSUER}"\nusers = "x"\n',
    "duplicate subject": variant('sub = "user-2"', 'sub = "user-1"'),
    "empty subject": variant('sub = "user-2"', 'sub = ""'),
    "subject with a space": variant('sub = "user-2"', 'sub = "user 2"'),
    "subject too long": variant('sub = "user-2"', 'sub = "' + "s" * 257 + '"'),
    "unknown role": variant('role = "viewer"', 'role = "admin"'),
    "role not a string": variant('role = "viewer"', "role = 1"),
    "instance outside the deployment": variant(f'"{OTHER}"', '"i-0aaaaaaaaaaaaaaaa"'),
    "malformed instance": variant(f'["{IID}"]', '["i-123"]'),
    "duplicate instance": variant(f'["{IID}", "{OTHER}"]', f'["{IID}", "{IID}"]'),
    "no instances": variant(f'instances = ["{IID}"]\n\n[[users]]', "instances = []\n\n[[users]]"),
    "instances not a list": variant(f'instances = ["{IID}"]\n\n[[users]]', 'instances = "x"\n\n[[users]]'),
    "session_hours zero": variant(f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nsession_hours = 0\n'),
    "session_hours too large": variant(
        f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nsession_hours = 25\n'
    ),
    "session_hours boolean": variant(
        f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nsession_hours = true\n'
    ),
    "session_hours string": variant(f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nsession_hours = "8"\n'),
    "limit zero": variant(
        f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\n[limits]\nchat_per_user_per_hour = 0\n'
    ),
    "limit too large": variant(
        f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\n[limits]\nchat_per_user_per_hour = 1001\n'
    ),
    "require_mfa not a boolean": variant(
        f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nrequire_mfa = "yes"\n'
    ),
}


@pytest.mark.parametrize("text", list(BAD_FILES.values()), ids=list(BAD_FILES))
def test_invalid_files_are_rejected(tmp_path, text):
    with pytest.raises(team.TeamError):
        roster(tmp_path, text)


def test_a_hundred_and_one_users_are_rejected(tmp_path):
    users = "".join(
        f'\n[[users]]\nsub = "u{n}"\nrole = "viewer"\ninstances = ["{IID}"]\n' for n in range(101)
    )
    with pytest.raises(team.TeamError):
        roster(tmp_path, f'issuer = "{ISSUER}"\n{users}')


def test_too_many_instances_per_user_are_rejected(tmp_path):
    deployed = {f"i-{n:017x}" for n in range(101)}
    instances_list = ", ".join(f'"i-{n:017x}"' for n in range(101))
    text = (
        f'issuer = "{ISSUER}"\n\n[[users]]\nsub = "user-1"\nrole = "viewer"\ninstances = [{instances_list}]\n'
    )
    with pytest.raises(team.TeamError):
        team.load(write(tmp_path, text), deployed)


def test_exactly_one_hundred_instances_per_user_loads(tmp_path):
    deployed = {f"i-{n:017x}" for n in range(100)}
    instances_list = ", ".join(f'"i-{n:017x}"' for n in range(100))
    text = (
        f'issuer = "{ISSUER}"\n\n[[users]]\nsub = "user-1"\nrole = "viewer"\ninstances = [{instances_list}]\n'
    )
    loaded = team.load(write(tmp_path, text), deployed)
    assert len(loaded.users["user-1"].instances) == 100


def test_exactly_two_hundred_fifty_six_char_subject_loads(tmp_path):
    subject_256 = "s" * 256
    text = (
        f'issuer = "{ISSUER}"\n\n[[users]]\nsub = "{subject_256}"\nrole = "viewer"\ninstances = ["{IID}"]\n'
    )
    loaded = roster(tmp_path, text)
    assert subject_256 in loaded.users


def test_errors_name_fields_and_never_echo_values(tmp_path):
    private = "PRIVATE-SUBJECT-VALUE"
    text = TEXT.replace('sub = "user-1"', f'sub = "{private}"').replace(
        'role = "investigator"', 'role = "boss-value"'
    )
    with pytest.raises(team.TeamError) as raised:
        roster(tmp_path, text)
    message = str(raised.value)
    assert "users[0].role" in message
    assert private not in message and "boss-value" not in message


def test_missing_file_directory_bad_toml_and_unsafe_modes_are_rejected(tmp_path):
    with pytest.raises(team.TeamError):
        team.load(str(tmp_path / "absent.toml"), ALLOWED)
    with pytest.raises(team.TeamError):
        team.load(str(tmp_path), ALLOWED)
    with pytest.raises(team.TeamError):
        team.load(write(tmp_path, "broken = ["), ALLOWED)
    with pytest.raises(team.TeamError):
        team.load(write(tmp_path, TEXT, mode=0o660), ALLOWED)
    with pytest.raises(team.TeamError):
        team.load(write(tmp_path, "#" * (256 * 1024 + 1)), ALLOWED)


def test_invalid_file_encoding_is_rejected(tmp_path):
    path = tmp_path / "team.toml"
    path.write_bytes(b'issuer = "\xff"')
    with pytest.raises(team.TeamError):
        team.load(str(path), ALLOWED)


def test_deeply_nested_toml_is_rejected(tmp_path):
    text = "a = " + "[" * 100000 + "]" * 100000
    with pytest.raises(team.TeamError):
        team.load(write(tmp_path, text), ALLOWED)


# ---- authorization ---------------------------------------------------------


def test_listed_user_with_fresh_mfa_is_authorized(tmp_path):
    access = roster(tmp_path).authorize(claims(), now=NOW)
    assert access == team.Access("user-1", "investigator", frozenset({IID}))


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"iss": "https://other.example.invalid"}, "issuer"),
        ({"iss": ...}, "issuer"),
        ({"sub": "stranger"}, "not_listed"),
        ({"sub": 7}, "not_listed"),
        ({"sub": ["user-1"]}, "not_listed"),
        ({"sub": None}, "not_listed"),
        ({"sub": ...}, "not_listed"),
        ({"auth_time": NOW - 8 * 3600 - 1}, "expired"),
        ({"auth_time": NOW + 301}, "expired"),
        ({"auth_time": "yesterday"}, "claims"),
        ({"auth_time": True}, "claims"),
        ({"auth_time": ...}, "claims"),
        ({"auth_time": ..., "iat": NOW - 9 * 3600}, "expired"),
        ({"amr": ...}, "mfa"),
        ({"amr": "mfa"}, "mfa"),
        ({"amr": ["pwd"]}, "mfa"),
        ({"auth_time": float("nan")}, "expired"),
        ({"auth_time": float("inf")}, "expired"),
        ({"auth_time": 10**400}, "claims"),
        ({"auth_time": -(10**400)}, "claims"),
    ],
)
def test_malformed_or_unacceptable_claims_are_denied_never_raised(tmp_path, changes, reason):
    with pytest.raises(team.TeamDenied) as denied:
        roster(tmp_path).authorize(claims(**changes), now=NOW)
    assert denied.value.reason == reason
    assert reason in team.DENIED_MESSAGES


@pytest.mark.parametrize("value", [None, "text", ["list"], 5])
def test_non_mapping_claims_are_denied(tmp_path, value):
    with pytest.raises(team.TeamDenied) as denied:
        roster(tmp_path).authorize(value, now=NOW)
    assert denied.value.reason == "claims"


def test_age_boundaries(tmp_path):
    loaded = roster(tmp_path)
    assert loaded.authorize(claims(auth_time=NOW - 8 * 3600), now=NOW).sub == "user-1"
    assert loaded.authorize(claims(auth_time=NOW + 299), now=NOW).sub == "user-1"


def test_iat_is_used_when_the_provider_sends_no_auth_time(tmp_path):
    assert roster(tmp_path).authorize(claims(auth_time=..., iat=NOW - 60), now=NOW).sub == "user-1"


def test_mfa_can_be_switched_off(tmp_path):
    text = TEXT.replace(f'issuer = "{ISSUER}"\n', f'issuer = "{ISSUER}"\nrequire_mfa = false\n')
    assert roster(tmp_path, text).authorize(claims(amr=...), now=NOW).sub == "user-1"


def test_the_issuer_is_checked_before_the_subject(tmp_path):
    with pytest.raises(team.TeamDenied) as denied:
        roster(tmp_path).authorize(claims(iss="https://other.example.invalid"), now=NOW)
    assert denied.value.reason == "issuer"


# ---- edits take effect and failures are closed ------------------------------


def test_removing_a_user_takes_effect_on_the_next_load(tmp_path):
    path = write(tmp_path)
    team.load(path, ALLOWED).authorize(claims(), now=NOW)
    write(tmp_path, TEXT.replace('sub = "user-1"', 'sub = "user-9"'))
    bump(path)
    with pytest.raises(team.TeamDenied):
        team.load(path, ALLOWED).authorize(claims(), now=NOW)


def test_a_file_that_becomes_invalid_fails_closed(tmp_path):
    path = write(tmp_path)
    team.load(path, ALLOWED)
    write(tmp_path, "broken = [")
    bump(path)
    with pytest.raises(team.TeamError):
        team.load(path, ALLOWED)


def test_an_unchanged_file_is_parsed_once(tmp_path, monkeypatch):
    path = write(tmp_path)
    calls = []
    real = team.tomllib.load
    monkeypatch.setattr(team.tomllib, "load", lambda handle: calls.append(1) or real(handle))
    team.load(path, ALLOWED)
    team.load(path, ALLOWED)
    assert len(calls) == 1
    bump(path)
    team.load(path, ALLOWED)
    assert len(calls) == 2


def test_a_change_in_the_deployed_instances_invalidates_the_cache(tmp_path):
    path = write(tmp_path)
    team.load(path, ALLOWED)
    with pytest.raises(team.TeamError):
        team.load(path, {IID})  # OTHER is no longer deployed


# ---- hourly limit ------------------------------------------------------------


def test_hourly_limit_is_per_subject_and_slides():
    assert [
        team.admit("a", 2, now=1000.0),
        team.admit("a", 2, now=1001.0),
        team.admit("a", 2, now=1002.0),
    ] == [
        True,
        True,
        False,
    ]
    assert team.admit("b", 2, now=1002.0) is True
    assert team.admit("a", 2, now=1000.0 + 3600) is True  # the first request left the window


# ---- audit line --------------------------------------------------------------


def test_audit_line_is_one_json_object_without_content(capsys):
    team.audit(
        "user-1", "investigator", None, "chat", "OK", instance_count=2, tokens={"input": 10, "output": 5}
    )
    line = capsys.readouterr().out.strip()
    data = json.loads(line)
    assert set(data) == {
        "ts",
        "event",
        "sub",
        "role",
        "instance",
        "instance_count",
        "action",
        "outcome",
        "tokens",
    }
    assert data["event"] == "tracaus.audit" and data["sub"] == "user-1" and data["instance"] is None
    assert "\n" not in line


def test_audit_omits_what_is_unknown(capsys):
    team.audit("user-1", "viewer", IID, "report", "OK")
    data = json.loads(capsys.readouterr().out)
    assert data["instance"] == IID and "tokens" not in data and "instance_count" not in data


def test_audit_filters_tokens_to_valid_keys_and_types(capsys):
    team.audit(
        "user-1",
        "investigator",
        None,
        "chat",
        "OK",
        tokens={"input": 10, "output": 5, "extra": 99, "output_float": 5.5},
    )
    data = json.loads(capsys.readouterr().out)
    assert data["tokens"] == {"input": 10, "output": 5}

    team.audit("user-1", "investigator", None, "chat", "OK", tokens={})
    data = json.loads(capsys.readouterr().out)
    assert "tokens" not in data


# ---- packaging -----------------------------------------------------------------


def test_module_needs_only_the_standard_library():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import tracaus.team;"
        "assert 'streamlit' not in sys.modules and 'boto3' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-S", "-c", code, str(ROOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
