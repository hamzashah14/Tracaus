"""A `clients(service, region)` factory backed by real botocore clients behind Stubbers, for the guided tests."""

import boto3
from botocore.stub import Stubber

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
