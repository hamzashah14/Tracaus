"""Terminal output for the guided commands.

Numbered steps, plain statements of what is about to happen, text from AWS stripped of terminal control
characters, and a redacted log file. Nothing else in the guided code prints."""

import contextlib
import datetime
import os
import re
import sys
import time

# ANSI CSI and OSC sequences, then any remaining control, bidirectional-override and zero-width characters.
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_REDACTIONS = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b"), "<aws-key-id>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (
        re.compile(
            r"(?i)\b(secret|token|password|passwd|api[_-]?key|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+"
        ),
        r"\1\2<redacted>",
    ),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_-])"), "<redacted>"),
)


def clean(value, limit=200):
    """One printable line: escape sequences and control characters gone, truncated. Safe for AWS-supplied text."""
    text = _ESCAPES.sub("", str(value))
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."


def redact(text):
    """Remove emails, key IDs, bearer tokens, `secret=...` pairs and long opaque strings."""
    text = str(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class Console:
    """Prints steps and results. `stream` defaults to stdout; `log_path` receives every line, redacted."""

    def __init__(self, stream=None, log_path=None, *, color=None, plain=None, clock=time.monotonic):
        self.stream = stream or sys.stdout
        self.clock = clock
        encoding = (getattr(self.stream, "encoding", None) or "ascii").lower()
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.plain = plain if plain is not None else not encoding.replace("-", "").startswith("utf")
        self.color = (
            color
            if color is not None
            else tty and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        )
        self.total, self.number, self.log_path = 0, 0, None
        if log_path:
            self.open_log(log_path)

    def open_log(self, path):
        """Append to a private log. The file is created 600 and never followed through a symlink."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.log_path = path

    def _log(self, line):
        if self.log_path:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            try:
                os.write(fd, f"{stamp} {redact(line)}\n".encode())
            finally:
                os.close(fd)

    def _paint(self, text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def _emit(self, prefix, text, code=None, indent="  "):
        for number, raw in enumerate(str(text).split("\n")):
            line = clean(raw, 400)
            head = prefix if number == 0 else " " * len(prefix)
            shown = f"{indent}{self._paint(head, code) if code and number == 0 else head} {line}".rstrip()
            print(shown, file=self.stream, flush=True)
            self._log(f"{head} {line}")

    @property
    def marks(self):
        return (
            {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
            if self.plain
            else {"ok": "✓", "warn": "!", "fail": "✗"}
        )

    def begin(self, total):
        """Start a sequence of `total` numbered steps."""
        self.total, self.number = total, 0

    @contextlib.contextmanager
    def step(self, text):
        """Announce a step, then report how long it took, or that it failed (the error is re-raised)."""
        self.number += 1
        label = f"[{self.number}/{self.total}]" if self.total else "[-]"
        print("", file=self.stream)
        self._emit(label, text, "1")
        started = self.clock()
        try:
            yield
        except BaseException:
            self.fail(f"step failed after {self.clock() - started:.0f}s")
            raise
        self.ok(f"done in {self.clock() - started:.0f}s")

    def ok(self, text):
        self._emit(self.marks["ok"], text, "32", indent="      ")

    def warn(self, text):
        self._emit(self.marks["warn"], text, "33", indent="      ")

    def fail(self, text):
        self._emit(self.marks["fail"], text, "31", indent="      ")

    def info(self, text):
        self._emit("-", text, indent="      ")

    def say(self, text=""):
        """A plain line, for statements before a change and for summaries."""
        for raw in str(text).split("\n"):
            print(clean(raw, 400), file=self.stream, flush=True)
            self._log(clean(raw, 400))

    def explain(self, what, cause, safe, next_step):
        """The standard failure block: what failed, why, what is safe, and what to do."""
        print("", file=self.stream)
        self._emit(self.marks["fail"], what, "31", indent="")
        for label, value in (("Cause", cause), ("Safe", safe), ("Next", next_step)):
            if value:
                self._emit(f"{label}:", value, indent="      ")
