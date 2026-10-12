"""A `clients(service, region)` factory backed by real botocore clients behind Stubbers, for the guided tests."""

import boto3
from botocore.stub import Stubber

from infra.guided.prompt import NeedsAnswer, Prompter

REGION, ACCOUNT = "eu-central-1", "123456789012"


class Fake:
    """`clients(service, region)` backed by stubbed clients, one per service."""

    def __init__(self):
        self.stubs, self.real = {}, {}
        self.session = boto3.session.Session(
            aws_access_key_id="synthetic", aws_secret_access_key="synthetic", region_name=REGION
        )

    def __call__(self, service, region):
        if service not in self.real:
            self.real[service] = self.session.client(service)
            self.stubs[service] = Stubber(self.real[service])
            self.stubs[service].activate()
        return self.real[service]

    def on(self, service, method, response, params=None):
        self(service, REGION)
        self.stubs[service].add_response(method, response, params)

    def denied(self, service, method, code="AccessDeniedException", message=""):
        self(service, REGION)
        self.stubs[service].add_client_error(method, service_error_code=code, service_message=message)

    def done(self):
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()


class ScriptedPrompter(Prompter):
    """Tests: answers by key. A tuple answers repeated asks of one key in order."""

    def __init__(self, answers, allowed=()):
        super().__init__(allowed)
        self.answers, self.asked = dict(answers), []

    def _take(self, key):
        self.asked.append(key)
        if key not in self.answers:
            raise NeedsAnswer(f"No scripted answer for '{key}'.")
        value = self.answers[key]
        if isinstance(value, tuple):  # a tuple is consumed one item per ask
            if not value:
                raise NeedsAnswer(f"Scripted answers for '{key}' ran out.")
            self.answers[key] = value[1:]
            return value[0]
        return value

    def ask(self, key, question, default=None, validate=None):
        value = self._take(key)
        return self._remember(key, validate(value) if validate else value)

    def choose(self, key, question, options, default=None):
        return self._remember(key, self._take(key))

    def pick_many(self, key, question, options, minimum=0, maximum=None, default=()):
        return self._remember(key, list(self._take(key)))

    def confirm(self, key, question, default=False, consent=None):
        if consent and key not in self.answers:
            return self._consent(consent)
        value = bool(self._take(key))
        return value if consent else self._remember(key, value)

    def secret(self, key, question):
        return self._take(key)

    def typed(self, key, question, expected):
        return self._take(key) == expected
