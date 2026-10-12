"""Guided output: sanitizing AWS text, redaction, the step format and the private log."""

import io
import os
import stat

import pytest

from infra.guided_console import Console, clean, redact


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class Ascii(io.StringIO):
    encoding = "ascii"


def test_clean_removes_escape_sequences_and_control_characters():
    hostile = "web\x1b[2J\x1b[31m-1\x1b]0;title\x07\nname\r\x00‮evil​"
    result = clean(hostile)
    assert "\x1b" not in result and "\n" not in result and "‮" not in result
    assert result == "web-1 name evil"


def test_clean_truncates_and_handles_non_strings():
    assert clean("a" * 500, 20) == "a" * 17 + "..."
    assert clean(None) == "None" and clean(12) == "12"


@pytest.mark.parametrize(
    "text,gone",
    [
        ("contact ops@example.com now", "ops@example.com"),
        ("key AKIAABCDEFGHIJKLMNOP used", "AKIAABCDEFGHIJKLMNOP"),
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("password=hunter2hunter2 and more", "hunter2hunter2"),
        ('{"api_key": "sk-live-1234"}', "sk-live-1234"),
        ("blob " + "A1b2" * 15, "A1b2" * 15),
    ],
)
def test_redact_removes_secrets(text, gone):
    assert gone not in redact(text)


def test_redact_keeps_ordinary_text():
    text = "Creating stack tracaus-staging-foundation in eu-central-1"
    assert redact(text) == text


def test_steps_are_numbered_and_timed():
    ticks = iter([0.0, 3.0])
    out = io.StringIO()
    console = Console(out, clock=lambda: next(ticks))
    console.begin(2)
    with console.step("Discovering EC2"):
        pass
    assert "[1/2] Discovering EC2" in out.getvalue()
    assert "done in 3s" in out.getvalue()


def test_a_failing_step_reports_and_reraises():
    out = io.StringIO()
    console = Console(out)
    console.begin(1)
    with pytest.raises(RuntimeError), console.step("Create roles"):
        raise RuntimeError("boom")
    assert "step failed" in out.getvalue()


def test_plain_ascii_when_the_stream_is_not_utf8():
    out = Ascii()
    console = Console(out)
    console.ok("fine")
    console.fail("broken")
    assert "OK" in out.getvalue() and "FAIL" in out.getvalue()
    assert out.getvalue().isascii()


def test_color_only_on_a_terminal_and_never_with_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    plain, colored = io.StringIO(), Tty()
    Console(plain).ok("x")
    Console(colored).ok("x")
    assert "\x1b[" not in plain.getvalue() and "\x1b[" in colored.getvalue()
    monkeypatch.setenv("NO_COLOR", "1")
    quiet = Tty()
    Console(quiet).ok("x")
    assert "\x1b[" not in quiet.getvalue()


def test_aws_text_is_cleaned_before_it_reaches_the_terminal():
    out = io.StringIO()
    Console(out).info("instance named \x1b[2Jevil\x07")
    assert "\x1b" not in out.getvalue() and "\x07" not in out.getvalue()


def test_the_log_is_private_redacted_and_not_followed_through_a_link(tmp_path):
    log = tmp_path / "guided.log"
    console = Console(io.StringIO(), log)
    console.say("sent to ops@example.com")
    console.ok("password=hunter2hunter2")
    text = log.read_text()
    assert "ops@example.com" not in text and "hunter2hunter2" not in text
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    link = tmp_path / "link.log"
    link.symlink_to(tmp_path / "elsewhere.log")
    with pytest.raises(OSError):
        Console(io.StringIO(), link)


def test_explain_prints_the_four_parts():
    out = io.StringIO()
    Console(out).explain("Roles missing", "no such role", "nothing was changed", "run setup again")
    value = out.getvalue()
    for part in (
        "Roles missing",
        "Cause: no such role",
        "Safe: nothing was changed",
        "Next: run setup again",
    ):
        assert part in value
