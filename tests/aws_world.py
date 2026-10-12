"""A small in-memory AWS account for the teardown tests.

Every call is checked against the real botocore operation shape, so a wrong parameter name or a missing required
one fails the test the way it would fail in AWS. The behaviour (what CloudFormation keeps, what S3 refuses to
delete, how DynamoDB deletion protection works) is a simplified copy of the real services and has not been checked
against a live account."""

import datetime
import itertools

from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters

ACCOUNT, REGION = "123456789012", "eu-central-1"
SESSION = get_session()
COUNTER = itertools.count(1)


def error(code, message="synthetic"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


class Client:
    def __init__(self, world, service, region):
        self.world, self.service, self.region = world, service, region

    def __getattr__(self, operation):
        if operation == "get_paginator":
            return lambda op: Paginator(self, op)
        return lambda **kwargs: self.world.call(self.service, self.region, operation, kwargs)


class Paginator:
    def __init__(self, client, operation):
        self.client, self.operation = client, operation

    def paginate(self, **kwargs):
        kwargs.pop("PaginationConfig", None)
        return [getattr(self.client, self.operation)(**kwargs)]


class World:
    def __init__(self, account=ACCOUNT):
        self.account = account
        self.stacks, self.buckets, self.tables, self.keys = {}, {}, {}, {}
        self.queues, self.topics, self.groups, self.functions, self.secrets = (
            set(),
            set(),
            set(),
            set(),
            set(),
        )
        self.roles, self.runtimes, self.endpoints = {}, set(), {}
        self.calls, self.failures, self.arns, self.extra_tagged = [], {}, {}, []
        self.object_lock = False

    def __call__(self, service, region):
        return Client(self, service, region)

    # ------------------------------------------------------------------ the call path

    def call(self, service, region, operation, kwargs):
        model = SESSION.get_service_model(service).operation_model(
            "".join(p.title() for p in operation.split("_"))
        )
        try:
            validate_parameters(kwargs, model.input_shape)
        except (
            Exception
        ) as problem:  # not a BotoCoreError: the code under test must not be able to swallow it
            raise AssertionError(f"{service}.{operation}: {problem}") from None
        self.calls.append((service, region, operation, kwargs))
        planned = self.failures.get((service, operation))
        if planned and planned[2] > 0:
            self.failures[(service, operation)] = (*planned[:2], planned[2] - 1)
            raise error(planned[0], planned[1])
        return getattr(self, f"{service.replace('-', '_')}_{operation}")(region, **kwargs)

    def fail(self, service, operation, code, message="synthetic", times=99):
        self.failures[(service, operation)] = (code, message, times)

    def deleted(self, operation):
        return [kw for _, _, op, kw in self.calls if op == operation]

    # ------------------------------------------------------------------ CloudFormation

    def stack(self, name, region, resources, *, tags=None, protected=False, **extra):
        stack_id = f"arn:aws:cloudformation:{region}:{self.account}:stack/{name}/{next(COUNTER):08d}"
        self.stacks[stack_id] = {
            "id": stack_id,
            "name": name,
            "region": region,
            "status": "CREATE_COMPLETE",
            "tags": tags or [],
            "protected": protected,
            "resources": resources,
            "events": [],
            "ticks": 0,
            **extra,
        }
        return stack_id

    def _stack(self, key):
        for stack in self.stacks.values():
            if key == stack["id"] or (key == stack["name"] and stack["status"] != "DELETE_COMPLETE"):
                return stack
        raise error("ValidationError", f"Stack with id {key} does not exist")

    def _live(self, name):
        return any(s["name"] == name and s["status"] != "DELETE_COMPLETE" for s in self.stacks.values())

    def _tick(self, stack):
        if stack["status"].endswith("IN_PROGRESS") and "final" in stack:
            if stack["ticks"] > 0:
                stack["ticks"] -= 1
            else:
                stack["status"] = stack["final"]

    def cloudformation_describe_stacks(self, region, StackName=None, **_):
        found = (
            [self._stack(StackName)]
            if StackName
            else [
                s for s in self.stacks.values() if s["region"] == region and s["status"] != "DELETE_COMPLETE"
            ]
        )
        for stack in found:
            self._tick(stack)
        return {
            "Stacks": [
                {
                    "StackId": s["id"],
                    "StackName": s["name"],
                    "StackStatus": s["status"],
                    "Tags": s["tags"],
                    "EnableTerminationProtection": s["protected"],
                }
                for s in found
            ]
        }

    def cloudformation_list_stack_resources(self, region, StackName, **_):
        return {"StackResourceSummaries": [dict(r) for r in self._stack(StackName)["resources"]]}

    def cloudformation_describe_stack_events(self, region, StackName, **_):
        return {"StackEvents": list(reversed(self._stack(StackName)["events"]))}

    def cloudformation_update_termination_protection(self, region, StackName, EnableTerminationProtection):
        self._stack(StackName)["protected"] = EnableTerminationProtection
        return {"StackId": StackName}

    def cloudformation_delete_stack(self, region, StackName, RetainResources=(), **_):
        stack = self._stack(StackName)
        if stack.get("refuse_delete"):
            raise error("ValidationError", stack["refuse_delete"])
        if stack["protected"]:
            raise error("ValidationError", "Stack cannot be deleted while TerminationProtection is enabled")
        failures = {}
        blockers = [n for n in stack.get("blocked_by", ()) if self._live(n)]
        if blockers:
            failures[stack["resources"][0]["LogicalResourceId"]] = (
                f"Export x cannot be deleted as it is in use by {blockers[0]}"
            )
        for logical, reason in stack.get("delete_fails", {}).items():
            if logical not in RetainResources:
                failures[logical] = reason
        for resource in stack["resources"]:
            logical = resource["LogicalResourceId"]
            if logical in failures:
                resource.update(ResourceStatus="DELETE_FAILED", ResourceStatusReason=failures[logical])
            elif logical in RetainResources or resource.get("retain"):
                resource["ResourceStatus"] = "DELETE_SKIPPED"
            else:
                self._drop(resource["ResourceType"], resource["PhysicalResourceId"])
                resource["ResourceStatus"] = "DELETE_COMPLETE"
            stack["events"].append(
                {
                    "EventId": f"e{next(COUNTER)}",
                    "StackId": stack["id"],
                    "StackName": stack["name"],
                    "LogicalResourceId": logical,
                    "ResourceStatus": resource["ResourceStatus"],
                    "ResourceStatusReason": resource.get("ResourceStatusReason", ""),
                }
            )
        stack.update(
            status="DELETE_IN_PROGRESS", final="DELETE_FAILED" if failures else "DELETE_COMPLETE", ticks=1
        )

    def _drop(self, kind, physical):
        store = {
            "AWS::S3::Bucket": self.buckets,
            "AWS::DynamoDB::Table": self.tables,
            "AWS::KMS::Key": self.keys,
            "AWS::SQS::Queue": self.queues,
            "AWS::SNS::Topic": self.topics,
            "AWS::Logs::LogGroup": self.groups,
            "AWS::Lambda::Function": self.functions,
            "AWS::SecretsManager::Secret": self.secrets,
            "AWS::IAM::Role": self.roles,
        }.get(kind)
        if store is not None:
            store.pop(physical, None) if isinstance(store, dict) else store.discard(physical)

    # ------------------------------------------------------------------ S3

    def _bucket(self, name, owner=None):
        if owner and owner != self.account:
            raise error("403", "Forbidden")
        if name not in self.buckets:
            raise error("404", "Not Found")
        return self.buckets[name]

    def s3_head_bucket(self, region, Bucket, ExpectedBucketOwner=None):
        self._bucket(Bucket, ExpectedBucketOwner)
        return {}

    def s3_list_object_versions(self, region, Bucket, MaxKeys=1000, ExpectedBucketOwner=None, **_):
        objects = self._bucket(Bucket, ExpectedBucketOwner)["objects"]
        page = objects[:MaxKeys]
        return {
            "Versions": [{"Key": k, "VersionId": v} for k, v, marker in page if not marker],
            "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v, marker in page if marker],
        }

    def s3_delete_objects(self, region, Bucket, Delete, ExpectedBucketOwner=None, **_):
        bucket = self._bucket(Bucket, ExpectedBucketOwner)
        if self.object_lock:
            return {
                "Errors": [
                    {"Key": o["Key"], "VersionId": o["VersionId"], "Code": "AccessDenied"}
                    for o in Delete["Objects"]
                ]
            }
        gone = {(o["Key"], o["VersionId"]) for o in Delete["Objects"]}
        bucket["objects"] = [o for o in bucket["objects"] if (o[0], o[1]) not in gone]
        return {}

    def s3_delete_bucket(self, region, Bucket, ExpectedBucketOwner=None):
        if self._bucket(Bucket, ExpectedBucketOwner)["objects"]:
            raise error("BucketNotEmpty", "The bucket you tried to delete is not empty")
        del self.buckets[Bucket]

    # ------------------------------------------------------------------ DynamoDB

    def dynamodb_describe_table(self, region, TableName):
        if TableName not in self.tables:
            raise error("ResourceNotFoundException", "Requested resource not found")
        table = self.tables[TableName]
        if table.get("settling", 0) > 0:
            table["settling"] -= 1
            return {"Table": {"TableName": TableName, "TableStatus": "UPDATING", **self._flags(table)}}
        return {"Table": {"TableName": TableName, "TableStatus": "ACTIVE", **self._flags(table)}}

    @staticmethod
    def _flags(table):
        return {"DeletionProtectionEnabled": table["protected"]}

    def dynamodb_update_table(self, region, TableName, DeletionProtectionEnabled=None, **_):
        self.tables[TableName]["protected"] = DeletionProtectionEnabled
        self.tables[TableName]["settling"] = 1
        return {}

    def dynamodb_delete_table(self, region, TableName):
        table = self.tables[TableName]
        if table["protected"]:
            raise error(
                "ValidationException",
                "Resource cannot be deleted as it is currently protected against deletion",
            )
        del self.tables[TableName]
        return {}

    # ------------------------------------------------------------------ KMS

    def kms_describe_key(self, region, KeyId, **_):
        if KeyId not in self.keys:
            raise error("NotFoundException", "Key does not exist")
        return {"KeyMetadata": dict(self.keys[KeyId], KeyId=KeyId)}

    def kms_schedule_key_deletion(self, region, KeyId, PendingWindowInDays=30):
        date = datetime.datetime(2026, 10, 19, tzinfo=datetime.timezone.utc)
        self.keys[KeyId].update(KeyState="PendingDeletion", DeletionDate=date)
        return {
            "KeyId": KeyId,
            "DeletionDate": date,
            "KeyState": "PendingDeletion",
            "PendingWindowInDays": PendingWindowInDays,
        }

    # ------------------------------------------------------------------ queues, topics, logs, functions, secrets

    def sqs_delete_queue(self, region, QueueUrl):
        if QueueUrl not in self.queues:
            raise error("AWS.SimpleQueueService.NonExistentQueue", "The specified queue does not exist")
        self.queues.discard(QueueUrl)

    def sns_get_topic_attributes(self, region, TopicArn):
        if TopicArn not in self.topics:
            raise error("NotFound", "Topic does not exist")
        return {"Attributes": {}}

    def sns_delete_topic(self, region, TopicArn):
        self.topics.discard(TopicArn)

    def logs_describe_log_groups(self, region, logGroupNamePrefix="", **_):
        return {
            "logGroups": [
                {"logGroupName": g} for g in sorted(self.groups) if g.startswith(logGroupNamePrefix)
            ]
        }

    def logs_delete_log_group(self, region, logGroupName):
        if logGroupName not in self.groups:
            raise error("ResourceNotFoundException", "The specified log group does not exist")
        self.groups.discard(logGroupName)

    def lambda_get_function(self, region, FunctionName, **_):
        if FunctionName not in self.functions:
            raise error("ResourceNotFoundException", "Function not found")
        return {}

    def lambda_delete_function(self, region, FunctionName, **_):
        self.functions.discard(FunctionName)

    def secretsmanager_describe_secret(self, region, SecretId):
        if SecretId not in self.secrets:
            raise error("ResourceNotFoundException", "Secrets Manager can't find the specified secret")
        return {"ARN": SecretId}

    def secretsmanager_delete_secret(self, region, SecretId, **_):
        self.secrets.discard(SecretId)
        return {"ARN": SecretId}

    # ------------------------------------------------------------------ IAM

    def iam_get_role(self, region, RoleName):
        if RoleName not in self.roles:
            raise error("NoSuchEntity", "The role cannot be found")
        return {"Role": {"RoleName": RoleName, "Path": self.roles[RoleName]["path"]}}

    def iam_list_role_policies(self, region, RoleName, **_):
        return {"PolicyNames": list(self.roles[RoleName]["inline"])}

    def iam_list_attached_role_policies(self, region, RoleName, **_):
        return {
            "AttachedPolicies": [
                {"PolicyArn": a, "PolicyName": a.rsplit("/", 1)[-1]} for a in self.roles[RoleName]["attached"]
            ]
        }

    def iam_list_instance_profiles_for_role(self, region, RoleName, **_):
        return {"InstanceProfiles": [{"InstanceProfileName": n} for n in self.roles[RoleName]["profiles"]]}

    def iam_delete_role_policy(self, region, RoleName, PolicyName):
        self.roles[RoleName]["inline"].remove(PolicyName)

    def iam_detach_role_policy(self, region, RoleName, PolicyArn):
        self.roles[RoleName]["attached"].remove(PolicyArn)

    def iam_delete_role(self, region, RoleName):
        role = self.roles[RoleName]
        if role["inline"] or role["attached"]:
            raise error("DeleteConflict", "Cannot delete entity, must delete policies first.")
        del self.roles[RoleName]

    # ------------------------------------------------------------------ AgentCore

    def bedrock_agentcore_control_get_agent_runtime_endpoint(self, region, agentRuntimeId, endpointName):
        key = (agentRuntimeId, endpointName)
        if key not in self.endpoints:
            raise error("ResourceNotFoundException", "not found")
        if self.endpoints[key] == "DELETING":
            self.endpoints[key] = "GONE"
            return {"status": "DELETING"}
        if self.endpoints[key] == "GONE":
            del self.endpoints[key]
            raise error("ResourceNotFoundException", "not found")
        return {"status": "READY"}

    def bedrock_agentcore_control_delete_agent_runtime_endpoint(
        self, region, agentRuntimeId, endpointName, **_
    ):
        self.endpoints[(agentRuntimeId, endpointName)] = "DELETING"
        return {}

    def bedrock_agentcore_control_get_agent_runtime(self, region, agentRuntimeId, **_):
        if agentRuntimeId not in self.runtimes:
            raise error("ResourceNotFoundException", "not found")
        return {}

    def bedrock_agentcore_control_delete_agent_runtime(self, region, agentRuntimeId, **_):
        if any(runtime == agentRuntimeId for runtime, _ in self.endpoints):
            raise error("ConflictException", "the runtime still has endpoints")
        self.runtimes.discard(agentRuntimeId)
        return {}

    # ------------------------------------------------------------------ tag index

    def resourcegroupstaggingapi_get_resources(self, region, **_):
        live = [arn for (kind, ident), arn in self.arns.items() if self._exists(kind, ident)]
        return {"ResourceTagMappingList": [{"ResourceARN": arn} for arn in live + self.extra_tagged]}

    def _exists(self, kind, ident):
        stores = {
            "bucket": self.buckets,
            "table": self.tables,
            "key": self.keys,
            "queue": self.queues,
            "topic": self.topics,
            "group": self.groups,
            "function": self.functions,
            "secret": self.secrets,
            "role": self.roles,
        }
        return ident in stores[kind]


SPEC = {
    "project": "tracaus-test",
    "environment": "staging",
    "account_id": ACCOUNT,
    "monitor_region": REGION,
    "bedrock_region": REGION,
    "log_segment": "",
    "release_id": "r1",
}
KEY = "1234abcd-12ab-34cd-56ef-1234567890ab"


def resource(logical, kind, physical, retain=False):
    return {
        "LogicalResourceId": logical,
        "ResourceType": kind,
        "PhysicalResourceId": physical,
        "ResourceStatus": "CREATE_COMPLETE",
        "retain": retain,
    }


def deploy(world, *, protect=True, tools_region=REGION, region=REGION):
    """A finished deployment of tracaus-test-staging: stacks plus the resources they keep, and the role stack."""
    project, env = SPEC["project"], SPEC["environment"]
    p = f"{project}-{env}"
    tags = [
        {"Key": "Project", "Value": project},
        {"Key": "Environment", "Value": env},
        {"Key": "ManagedBy", "Value": "tracaus-cloudformation"},
    ]
    reports, tools = f"{p}-{ACCOUNT}-{region}-reports", f"{p}-{ACCOUNT}-{tools_region}-tools"
    world.buckets[reports] = {"objects": [(f"k{n}", f"v{n}", n % 7 == 0) for n in range(2500)]}
    world.buckets[tools] = {"objects": [("a.zip", "v1", False)]}
    world.tables[f"{p}-incidents"] = {"protected": True}
    world.keys[KEY] = {"KeyState": "Enabled", "KeyManager": "CUSTOMER"}
    queue = f"https://sqs.{region}.amazonaws.com/{ACCOUNT}/{p}-ingress-queue"
    topic = f"arn:aws:sns:{region}:{ACCOUNT}:{p}-alarms"
    group = f"/{project}/{env}/i-0123456789abcdef0/application"
    secret = f"arn:aws:secretsmanager:{tools_region}:{ACCOUNT}:secret:{p}/log-cursor-AbCdEf"
    tools_fn, pipeline_fn = f"{p}-r1-fetch-logs", f"{p}-r1-incident-ingress"
    role = f"{p}-r1-durable-runtime-IngressRole-ABC123"
    world.queues.add(queue)
    world.topics.add(topic)
    world.groups |= {group, f"/aws/lambda/{tools_fn}", f"/aws/lambda/{pipeline_fn}"}
    world.secrets.add(secret)
    world.functions |= {tools_fn, pipeline_fn}
    world.roles[role] = {
        "path": f"/{project}/{env}/",
        "inline": ["policy"],
        "attached": ["arn:aws:iam::aws:policy/Example"],
        "profiles": [],
    }
    world.arns.update(
        {
            ("bucket", reports): f"arn:aws:s3:::{reports}",
            ("bucket", tools): f"arn:aws:s3:::{tools}",
            ("table", f"{p}-incidents"): f"arn:aws:dynamodb:{region}:{ACCOUNT}:table/{p}-incidents",
            ("key", KEY): f"arn:aws:kms:{region}:{ACCOUNT}:key/{KEY}",
            ("queue", queue): f"arn:aws:sqs:{region}:{ACCOUNT}:{p}-ingress-queue",
            ("topic", topic): topic,
            ("group", group): f"arn:aws:logs:{region}:{ACCOUNT}:log-group:{group}",
            ("secret", secret): secret,
            ("function", tools_fn): f"arn:aws:lambda:{tools_region}:{ACCOUNT}:function:{tools_fn}",
            ("role", role): f"arn:aws:iam::{ACCOUNT}:role/{project}/{env}/{role}",
        }
    )
    world.stack(
        f"{p}-foundation-tools",
        tools_region,
        [
            resource("Artifacts", "AWS::S3::Bucket", tools, True),
            resource("CursorSecret", "AWS::SecretsManager::Secret", secret, True),
        ],
        tags=tags,
    )
    world.stack(
        f"{p}-durable-foundation",
        region,
        [
            resource("Evidence", "AWS::S3::Bucket", reports, True),
            resource("EvidencePolicy", "AWS::S3::BucketPolicy", reports),
            resource("Incidents", "AWS::DynamoDB::Table", f"{p}-incidents", True),
            resource("EvidenceKey", "AWS::KMS::Key", KEY, True),
            resource("IngressQueue", "AWS::SQS::Queue", queue, True),
            resource("Alarms", "AWS::SNS::Topic", topic, True),
            resource("AppLogs", "AWS::Logs::LogGroup", group, True),
        ],
        tags=tags,
    )
    world.stack(
        f"{p}-r1-owned-tools",
        tools_region,
        [
            resource("FetchLogs", "AWS::Lambda::Function", tools_fn, True),
            resource("FetchLogsVersion", "AWS::Lambda::Version", f"{tools_fn}:1", True),
        ],
        tags=tags,
    )
    world.stack(
        f"{p}-r1-durable-runtime",
        region,
        [
            resource("Ingress", "AWS::Lambda::Function", pipeline_fn, True),
            resource("IngressRole", "AWS::IAM::Role", role, True),
        ],
        tags=tags,
        protected=protect,
    )
    world.stack(f"{p}-routing", region, [resource("Rule", "AWS::Events::Rule", f"{p}-rule")], tags=tags)
    world.stack(
        f"{p}-iam",
        region,
        [resource("Operator", "AWS::IAM::Role", f"{p}-operator")],
        tags=[{"Key": "ManagedBy", "Value": "tracaus-iam-bootstrap"}],
    )
    return world


def bystanders(world):
    """Things that are not Tracaus's, which teardown must leave alone."""
    world.buckets["my-own-bucket"] = {"objects": [("keep", "v1", False)]}
    world.groups.add("/my/own/application")
    world.stack(
        "other-prod-routing",
        REGION,
        [resource("Rule", "AWS::Events::Rule", "x")],
        tags=[
            {"Key": "Project", "Value": "other"},
            {"Key": "Environment", "Value": "prod"},
            {"Key": "ManagedBy", "Value": "tracaus-cloudformation"},
        ],
    )
    world.stack("my-app", REGION, [resource("Bucket", "AWS::S3::Bucket", "my-own-bucket")])
    world.stack(  # same project and environment tags, but another tool made it
        "lookalike",
        REGION,
        [resource("Rule", "AWS::Events::Rule", "y")],
        tags=[
            {"Key": "Project", "Value": SPEC["project"]},
            {"Key": "Environment", "Value": SPEC["environment"]},
            {"Key": "ManagedBy", "Value": "terraform"},
        ],
    )
    return world
