"""The ways a guided question gets answered, and the rules that keep sensitive ones from being skipped."""

import io
import json

import pytest

from infra.guided.console import Console
from infra.guided.prompt import (
    AnswersPrompter,
    ConsolePrompter,
    NeedsAnswer,
    Refused,
    ScriptedPrompter,
    YesPrompter,
    valid_email,
    valid_profile,
    valid_project,
    valid_region,
    valid_url,
)


class Tty(io.StringIO):
    def isatty(self):
        return True


def console_prompter(lines, allowed=(), tty=True, secrets=()):
    out = io.StringIO()
    feed = iter(lines)
    secret_feed = iter(secrets)

    def read(prompt=""):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    stdin = Tty() if tty else io.StringIO()
    prompter = ConsolePrompter(
        Console(out), allowed, stdin=stdin, input_fn=read, secret_fn=lambda _: next(secret_feed)
    )
    return prompter, out


@pytest.mark.parametrize(
    "check,good,bad",
    [
        (valid_project, "tracaus", "Bad Name"),
        (valid_region, "eu-central-1", "europe"),
        (valid_email, "ops@example.com", "not-an-email"),
        (valid_url, "https://localhost:8501/", "http://x"),
        (valid_profile, "my-profile.1", "bad]\nname"),
    ],
)
def test_validators_accept_good_and_reject_bad_without_echoing(check, good, bad):
    assert check(good) == good
    with pytest.raises(ValueError) as error:
        check(bad)
    assert bad.strip() not in str(error.value)


def test_console_ask_uses_the_default_on_enter_and_validates():
    prompter, out = console_prompter(["", "x y", "tracaus"])
    assert prompter.ask("project", "Project?", default="tracaus", validate=valid_project) == "tracaus"
    prompter, out = console_prompter(["Bad Name", "good"])
    assert prompter.ask("project", "Project?", validate=valid_project) == "good"
    assert "2 to 12" in out.getvalue()
    assert prompter.record == {"project": "good"}


def test_console_gives_up_after_three_bad_answers():
    prompter, _ = console_prompter(["a b", "c d", "e f"])
    with pytest.raises(NeedsAnswer, match="project"):
        prompter.ask("project", "Project?", validate=valid_project)


def test_console_refuses_when_there_is_no_terminal():
    prompter, _ = console_prompter([], tty=False)
    with pytest.raises(NeedsAnswer, match="--answers"):
        prompter.ask("project", "Project?")


def test_console_end_of_input_stops_instead_of_looping():
    prompter, _ = console_prompter([])
    with pytest.raises(NeedsAnswer, match="Input ended"):
        prompter.ask("project", "Project?")


def test_choose_by_number_or_default():
    options = [("staging", "Staging"), ("production", "Production")]
    prompter, _ = console_prompter(["9", "2"])
    assert prompter.choose("environment", "Env?", options, default="staging") == "production"
    prompter, _ = console_prompter([""])
    assert prompter.choose("environment", "Env?", options, default="staging") == "staging"


@pytest.mark.parametrize(
    "text,expected",
    [("1,3", ["a", "c"]), ("1-2", ["a", "b"]), ("all", ["a", "b", "c"]), ("none", []), ("2,2", ["b"])],
)
def test_pick_many_forms(text, expected):
    prompter, _ = console_prompter([text])
    options = [("a", "A"), ("b", "B"), ("c", "C")]
    assert prompter.pick_many("targets", "Which?", options) == expected


def test_pick_many_enforces_the_limits_and_rejects_nonsense():
    options = [("a", "A"), ("b", "B")]
    prompter, out = console_prompter(["0", "1-9", "1,2"])
    assert prompter.pick_many("targets", "Which?", options, minimum=1, maximum=2) == ["a", "b"]
    prompter, _ = console_prompter(["none", "none", "none"])
    with pytest.raises(NeedsAnswer):
        prompter.pick_many("targets", "Which?", options, minimum=1)


def test_confirm_plain_and_consent():
    prompter, _ = console_prompter(["", "maybe", "y"])
    assert prompter.confirm("ok", "Go?", default=True) is True
    assert prompter.confirm("ok2", "Go?") is True
    prompter, _ = console_prompter(["y"])
    assert prompter.confirm("iam", "Create roles?", consent="iam-bootstrap") is False  # needs the full word
    prompter, _ = console_prompter(["YES"])
    assert prompter.confirm("iam", "Create roles?", consent="iam-bootstrap") is True
    assert "iam" not in prompter.record


def test_secret_is_not_recorded_or_logged():
    prompter, out = console_prompter([], secrets=["hunter2hunter2"])
    assert prompter.secret("pw", "Password?") == "hunter2hunter2"
    assert "pw" not in prompter.record and "hunter2" not in out.getvalue()


def test_answers_file_replays_and_fails_on_missing_keys(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"project": "tracaus", "targets": ["a"], "go": True}))
    prompter = AnswersPrompter(path)
    assert prompter.ask("project", "?", validate=valid_project) == "tracaus"
    assert prompter.pick_many("targets", "?", [("a", "A"), ("b", "B")], minimum=1) == ["a"]
    assert prompter.confirm("go", "?") is True
    with pytest.raises(NeedsAnswer, match="region"):
        prompter.ask("region", "?")
    with pytest.raises(NeedsAnswer, match="not valid"):
        AnswersPrompter({"project": "Bad Name"}).ask("project", "?", validate=valid_project)


def test_answers_file_cannot_hold_secrets_or_be_a_list(tmp_path):
    with pytest.raises(Refused):
        AnswersPrompter({"ui_password": "x"})
    path = tmp_path / "answers.json"
    path.write_text("[1]")
    with pytest.raises(NeedsAnswer):
        AnswersPrompter(path)


def test_sensitive_questions_need_their_explicit_flag():
    for prompter in (AnswersPrompter({}), YesPrompter(), ScriptedPrompter({})):
        with pytest.raises(Refused, match="--allow-paid-canary"):
            prompter.confirm("canary", "Spend money?", consent="paid-canary")
    allowed = YesPrompter(allowed={"paid-canary"})
    assert allowed.confirm("canary", "Spend money?", consent="paid-canary") is True


def test_yes_prompter_takes_defaults_and_stops_without_one():
    prompter = YesPrompter()
    assert prompter.ask("project", "?", default="tracaus") == "tracaus"
    assert prompter.confirm("x", "?", default=True) is True
    assert prompter.pick_many("t", "?", [("a", "A")], minimum=0, default=()) == []
    with pytest.raises(NeedsAnswer):
        prompter.ask("email", "?")
    with pytest.raises(NeedsAnswer):
        prompter.pick_many("t", "?", [("a", "A")], minimum=1)
    with pytest.raises(NeedsAnswer):
        prompter.secret("pw", "?")


def test_scripted_prompter_consumes_tuples_in_order():
    prompter = ScriptedPrompter({"email": ("a@example.com", "b@example.com"), "project": "tracaus"})
    assert prompter.ask("email", "?") == "a@example.com"
    assert prompter.ask("email", "?") == "b@example.com"
    with pytest.raises(NeedsAnswer):
        prompter.ask("email", "?")
    assert prompter.ask("project", "?") == "tracaus" and prompter.ask("project", "?") == "tracaus"


def test_a_flag_is_the_consent_even_at_a_terminal():
    prompter, out = console_prompter([], allowed={"paid-canary"}, tty=False)
    assert prompter.confirm("canary", "Spend money?", consent="paid-canary") is True
    other, _ = console_prompter(["yes"], allowed={"paid-canary"})
    assert other.confirm("retry", "Spend more?", consent="paid-canary-retry") is True  # typed yes still works


def test_a_list_answer_is_offered_again_as_one_line():
    out = io.StringIO()
    prompter = ConsolePrompter(
        Console(out),
        defaults={"emails": ["a@example.com", "b@example.com"]},
        stdin=Tty(),
        input_fn=lambda _: "",
    )
    assert prompter.ask("emails", "Emails?") == "a@example.com, b@example.com"
