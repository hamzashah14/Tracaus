"""Questions for the guided commands, and the ways they get answered.

`ConsolePrompter` asks a person. `AnswersPrompter` replays an answers file. `YesPrompter` accepts defaults.
Questions that create IAM roles or spend money carry a `consent` name; no
non-interactive prompter answers them unless the matching flag was given on the command line. Messages never
echo what was typed."""

import getpass
import json
import re
import sys
from pathlib import Path

MAX_TRIES = 3
SECRET_KEYS = re.compile(r"(?i)password|secret|token|api[_-]?key|credential")


class NeedsAnswer(Exception):
    """A question has no answer and cannot be asked. The message names the key, never a value."""


class Refused(Exception):
    """A safety rule stopped the run (a sensitive question without its explicit flag)."""


def valid_project(value):
    if not re.fullmatch(r"[a-z][a-z0-9]{1,11}", value):
        raise ValueError("Use 2 to 12 lowercase letters or digits, starting with a letter.")
    return value


def valid_release(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", value):
        raise ValueError("Use 1 to 16 lowercase letters, digits or hyphens.")
    return value


def valid_region(value):
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", value):
        raise ValueError("Use a region name such as eu-central-1.")
    return value


def valid_email(value):
    if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise ValueError("Enter an email address.")
    return value


def valid_url(value):
    if not re.fullmatch(r"https://[A-Za-z0-9._:-]+(?:/[A-Za-z0-9/_-]+)*/?", value):
        raise ValueError("Use a fixed https:// address without a query or credentials.")
    return value


def valid_profile(value):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", value):
        raise ValueError("Use letters, digits and _ . @ - only, up to 128 characters.")
    return value


class Prompter:
    """The interface. `key` names the answer in answers.json. `consent` marks IAM and paid questions."""

    def __init__(self, allowed=()):
        self.allowed, self.record = set(allowed), {}

    def ask(self, key, question, default=None, validate=None):
        raise NotImplementedError

    def choose(self, key, question, options, default=None):
        raise NotImplementedError

    def pick_many(self, key, question, options, minimum=0, maximum=None, default=()):
        raise NotImplementedError

    def confirm(self, key, question, default=False, consent=None):
        raise NotImplementedError

    def secret(self, key, question):
        raise NotImplementedError

    def typed(self, key, question, expected):
        """A destructive step: the person types `expected`. Only a terminal can answer; files and --yes cannot."""
        raise Refused(f"'{key}' must be typed at a terminal, or pass --confirm {expected}")

    def _remember(self, key, value):
        self.record[key] = value
        return value

    def _consent(self, consent):
        """Non-interactive consent: only the explicit command-line flag counts."""
        if consent not in self.allowed:
            raise Refused(
                f"This step needs the explicit --allow-{consent} flag; answers files and --yes cannot consent"
            )
        return True


class ConsolePrompter(Prompter):
    """Asks a person at a terminal. Without a terminal it refuses instead of waiting."""

    def __init__(
        self, console, allowed=(), *, defaults=None, stdin=None, input_fn=input, secret_fn=getpass.getpass
    ):
        super().__init__(allowed)
        self.console, self.input, self.secret_fn = console, input_fn, secret_fn
        self.defaults = dict(defaults or {})  # earlier answers, offered again as the default
        self.stdin = stdin or sys.stdin

    def _ready(self, key):
        if not getattr(self.stdin, "isatty", lambda: False)():
            raise NeedsAnswer(f"No terminal to ask '{key}'. Use --answers FILE or --yes.")

    def _read(self, key, suffix=""):
        try:
            return self.input(f"  > {suffix}").strip()
        except EOFError:
            raise NeedsAnswer(f"Input ended before '{key}' was answered.") from None

    def ask(self, key, question, default=None, validate=None):
        self._ready(key)
        stored = self.defaults.get(key)
        if isinstance(stored, list) and all(isinstance(item, str) for item in stored):
            stored = ", ".join(stored)  # a list answer (alert emails) is offered again as one line
        default = stored if isinstance(stored, str) else default
        for _ in range(MAX_TRIES):
            self.console.say(
                f"\n{question}" + (f"  [default: {default}]" if default not in (None, "") else "")
            )
            text = self._read(key) or (default or "")
            try:
                if text == "":
                    raise ValueError("An answer is needed.")
                return self._remember(key, validate(text) if validate else text)
            except ValueError as error:
                self.console.warn(str(error))
        raise NeedsAnswer(f"No valid answer for '{key}' after {MAX_TRIES} tries.")

    def choose(self, key, question, options, default=None):
        self._ready(key)
        values = [value for value, _ in options]
        default = self.defaults[key] if self.defaults.get(key) in values else default
        for _ in range(MAX_TRIES):
            self.console.say(f"\n{question}")
            for number, (value, label) in enumerate(options, 1):
                self.console.say(f"  {number}. {label}" + ("  [default]" if value == default else ""))
            text = self._read(key)
            if text == "" and default in values:
                return self._remember(key, default)
            if text.isdigit() and 1 <= int(text) <= len(values):
                return self._remember(key, values[int(text) - 1])
            self.console.warn(f"Enter a number from 1 to {len(values)}.")
        raise NeedsAnswer(f"No valid answer for '{key}' after {MAX_TRIES} tries.")

    def pick_many(self, key, question, options, minimum=0, maximum=None, default=()):
        self._ready(key)
        values = [value for value, _ in options]
        if isinstance(self.defaults.get(key), list):
            default = [v for v in self.defaults[key] if v in values]
        for _ in range(MAX_TRIES):
            self.console.say(f"\n{question}")
            for number, (value, label) in enumerate(options, 1):
                self.console.say(f"  {number}. {label}" + ("  [previous]" if value in default else ""))
            self.console.say("  Enter numbers separated by commas or ranges such as 1-3, 'all' or 'none'.")
            text = self._read(key).lower()
            picked = self._numbers(text, len(values), default, values)
            if picked is not None and len(picked) >= minimum and (maximum is None or len(picked) <= maximum):
                return self._remember(key, picked)
            self.console.warn(
                f"Pick {minimum}"
                + (f" to {maximum}" if maximum is not None else " or more")
                + " of the listed numbers."
            )
        raise NeedsAnswer(f"No valid answer for '{key}' after {MAX_TRIES} tries.")

    @staticmethod
    def _numbers(text, count, default, values):
        if text == "":
            return list(default)
        if text == "none":
            return []
        if text == "all":
            return list(values)
        chosen = []
        for part in text.replace(" ", "").split(","):
            low, _, high = part.partition("-")
            if not low.isdigit() or (high and not high.isdigit()):
                return None
            first, last = int(low), int(high or low)
            if not 1 <= first <= last <= count:
                return None
            chosen += [values[n - 1] for n in range(first, last + 1) if values[n - 1] not in chosen]
        return chosen

    def confirm(self, key, question, default=False, consent=None):
        if consent and consent in self.allowed:  # the flag on the command line is the consent
            return True
        self._ready(key)
        if isinstance(self.defaults.get(key), bool) and not consent:
            default = self.defaults[key]
        for _ in range(MAX_TRIES):
            if consent:
                self.console.say(f"\n{question}\n  Type 'yes' to continue, anything else to stop.")
                return self._read(key).lower() == "yes"
            self.console.say(f"\n{question}  [{'Y/n' if default else 'y/N'}]")
            text = self._read(key).lower()
            if text == "":
                return self._remember(key, default)
            if text in {"y", "yes", "n", "no"}:
                return self._remember(key, text[0] == "y")
            self.console.warn("Answer y or n.")
        raise NeedsAnswer(f"No valid answer for '{key}' after {MAX_TRIES} tries.")

    def typed(self, key, question, expected):
        self._ready(key)
        self.console.say(f"\n{question}\n  Type {expected} to continue, anything else to stop.")
        return self._read(key) == expected

    def secret(self, key, question):
        self._ready(key)
        try:
            return self.secret_fn(f"  {question} (hidden): ")
        except EOFError:
            raise NeedsAnswer(f"Input ended before '{key}' was answered.") from None


class AnswersPrompter(Prompter):
    """Replays answers.json. A missing key stops the run; sensitive questions need their flag."""

    def __init__(self, source, allowed=()):
        super().__init__(allowed)
        data = json.loads(Path(source).read_text()) if not isinstance(source, dict) else source
        if not isinstance(data, dict):
            raise NeedsAnswer("The answers file must hold one JSON object.")
        if any(SECRET_KEYS.search(str(k)) for k in data):
            raise Refused("The answers file must not hold secrets; remove any password, token or key entry.")
        self.data = data

    def _get(self, key):
        if key not in self.data:
            raise NeedsAnswer(f"The answers file has no entry for '{key}'.")
        return self.data[key]

    def ask(self, key, question, default=None, validate=None):
        value = self._get(key)
        try:
            return self._remember(key, validate(value) if validate else value)
        except (ValueError, TypeError):
            raise NeedsAnswer(f"The answer for '{key}' is not valid.") from None

    def choose(self, key, question, options, default=None):
        value = self._get(key)
        if value not in [v for v, _ in options]:
            raise NeedsAnswer(f"The answer for '{key}' is not one of the offered choices.")
        return self._remember(key, value)

    def pick_many(self, key, question, options, minimum=0, maximum=None, default=()):
        value = self._get(key)
        allowed = [v for v, _ in options]
        if (
            not isinstance(value, list)
            or any(v not in allowed for v in value)
            or len(value) < minimum
            or (maximum is not None and len(value) > maximum)
        ):
            raise NeedsAnswer(f"The answer for '{key}' is not a valid selection.")
        return self._remember(key, value)

    def confirm(self, key, question, default=False, consent=None):
        if consent:
            return self._consent(consent)
        value = self._get(key)
        if not isinstance(value, bool):
            raise NeedsAnswer(f"The answer for '{key}' must be true or false.")
        return self._remember(key, value)

    def secret(self, key, question):
        raise NeedsAnswer(f"'{key}' is a secret and cannot come from a file; run without --answers.")


class YesPrompter(Prompter):
    """Accepts every default. A question with no default stops the run; consent questions need their flag."""

    def ask(self, key, question, default=None, validate=None):
        if default in (None, ""):
            raise NeedsAnswer(f"'{key}' has no default; answer it or use --answers.")
        return self._remember(key, validate(default) if validate else default)

    def choose(self, key, question, options, default=None):
        if default not in [v for v, _ in options]:
            raise NeedsAnswer(f"'{key}' has no default; answer it or use --answers.")
        return self._remember(key, default)

    def pick_many(self, key, question, options, minimum=0, maximum=None, default=()):
        if len(default) < minimum:
            raise NeedsAnswer(f"'{key}' has no default selection; answer it or use --answers.")
        return self._remember(key, list(default))

    def confirm(self, key, question, default=False, consent=None):
        if consent:
            return self._consent(consent)
        return self._remember(key, default)

    def secret(self, key, question):
        raise NeedsAnswer(f"'{key}' is a secret; run without --yes.")
