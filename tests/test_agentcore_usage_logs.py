from __future__ import annotations

import os
import unittest
from unittest import mock

from atsuite.aws.Agentcore import AWSAgentCore
from atsuite.execution.cleanup import cleanup_owned_resource
from atsuite.recommender.identity import digest


class FakeLogs:
    class exceptions:
        class ConflictException(Exception):
            pass

        class ResourceAlreadyExistsException(Exception):
            pass

    def __init__(self):
        self.groups = []
        self.sources = {}
        self.destinations = {}
        self.deliveries = []
        self.deleted = []

    def create_log_group(self, logGroupName):
        if logGroupName in self.groups:
            raise self.exceptions.ResourceAlreadyExistsException(logGroupName)
        self.groups.append(logGroupName)

    def put_delivery_source(self, name, resourceArn, logType):
        if name in self.sources:
            raise self.exceptions.ConflictException(name)
        self.sources[name] = {"name": name, "resourceArns": [resourceArn], "logType": logType}
        return {"deliverySource": self.sources[name]}

    def put_delivery_destination(self, name, deliveryDestinationConfiguration, outputFormat):
        if name in self.destinations:
            raise self.exceptions.ConflictException(name)
        self.destinations[name] = {"name": name, "deliveryDestinationConfiguration": deliveryDestinationConfiguration}
        return {"deliveryDestination": self.destinations[name]}

    def create_delivery(self, deliverySourceName, deliveryDestinationArn):
        if any(row["deliverySourceName"] == deliverySourceName for row in self.deliveries):
            raise self.exceptions.ConflictException(deliverySourceName)
        self.deliveries.append({"id": "d1", "deliverySourceName": deliverySourceName, "deliveryDestinationArn": deliveryDestinationArn})

    def describe_delivery_sources(self, **kwargs):
        return {"deliverySources": list(self.sources.values())}

    def describe_deliveries(self, **kwargs):
        return {"deliveries": list(self.deliveries)}

    def describe_delivery_destinations(self, **kwargs):
        return {"deliveryDestinations": list(self.destinations.values())}

    def delete_delivery(self, id):
        self.deliveries = [row for row in self.deliveries if row["id"] != id]
        self.deleted.append(("delivery", id))

    def delete_delivery_source(self, name):
        self.sources.pop(name, None)
        self.deleted.append(("source", name))

    def delete_delivery_destination(self, name):
        self.destinations.pop(name, None)
        self.deleted.append(("destination", name))

    def delete_log_group(self, logGroupName):
        self.groups = [group for group in self.groups if group != logGroupName]
        self.deleted.append(("log_group", logGroupName))


class FakeAgentCoreControl:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, arn, ownership=""):
        self.arn = arn
        self.ownership = ownership
        self.deleted = []

    def get_agent_runtime(self, agentRuntimeId):
        return {"agentRuntimeArn": self.arn}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"atsuite-ownership": self.ownership}}

    def delete_agent_runtime(self, agentRuntimeId):
        self.deleted.append(agentRuntimeId)


class FakeSts:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class AgentCoreUsageLogDeliveryTests(unittest.TestCase):
    def make_deployer(self, logs):
        node_name = "at-" + "a" * 40
        control = FakeAgentCoreControl(
            "arn:aws:bedrock-agentcore:us-east-1:111:runtime/" + "atsuite_" + node_name.replace("-", "_")
        )
        with mock.patch.dict(os.environ, {"AWS_ACCOUNT_ID": "111", "ECR_REPOSITORY_NAME": "repo"}):
            deployer = AWSAgentCore(
                node_name=node_name,
                tag="tag",
                ecr_client=None,
                agentcore_client=control,
                region="us-east-1",
                runtime_options={"strict_experiment": True, "ownership": "own"},
                logs_client=logs,
            )
        deployer._runtime_id = deployer.runtime_name
        return deployer

    def test_setup_creates_log_group_delivery_source_destination_and_delivery(self):
        logs = FakeLogs()
        deployer = self.make_deployer(logs)
        deployer._setup_usage_logs()
        expected_group = AWSAgentCore.usage_log_group(deployer._runtime_id)
        self.assertEqual(logs.groups, [expected_group])
        self.assertEqual([source["logType"] for source in logs.sources.values()], ["USAGE_LOGS"])
        self.assertEqual(len(logs.destinations), 1)
        self.assertEqual(len(logs.deliveries), 1)

    def test_setup_is_idempotent_on_redeploy(self):
        logs = FakeLogs()
        deployer = self.make_deployer(logs)
        deployer._setup_usage_logs()
        deployer._setup_usage_logs()
        self.assertEqual(len(logs.groups), 1)
        self.assertEqual(len(logs.sources), 1)
        self.assertEqual(len(logs.destinations), 1)
        self.assertEqual(len(logs.deliveries), 1)

    def test_cleanup_removes_delivery_resources_and_log_group(self):
        account, region = "111", "us-east-1"
        runtime_id = "atsuite_at_" + "a" * 40
        arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
        logs = FakeLogs()
        source_name, destination_name = AWSAgentCore.usage_delivery_names(runtime_id)
        logs.put_delivery_source(source_name, arn, "USAGE_LOGS")
        logs.put_delivery_destination(
            destination_name,
            {"destinationResourceArn": f"arn:aws:logs:{region}:{account}:log-group:{AWSAgentCore.usage_log_group(runtime_id)}"},
            "json",
        )
        logs.create_delivery(source_name, f"arn:aws:logs:{region}:{account}:delivery-destination:{destination_name}")
        logs.create_log_group(AWSAgentCore.usage_log_group(runtime_id))
        ownership = digest({"tenant": "t", "project": "p", "experiment": "exp-1", "candidate": "c1"}, length=64)
        control = FakeAgentCoreControl(arn, ownership=ownership)
        record = {"id": "exp-1", "tenant": "t", "project": "p", "accounts": {"aws_agentcore": account},
                  "approval": {"accounts": {"aws_agentcore": account}}}
        resource = {"provider": "aws_agentcore", "region": region, "account_id": account, "physical_name": runtime_id,
                    "ownership": ownership, "tenant": "t", "project": "p", "experiment_id": "exp-1",
                    "candidate_id": "c1", "runtime_id": runtime_id}

        result = cleanup_owned_resource(resource, record, clients={"sts": FakeSts(account), "agentcore": control, "logs": logs})

        self.assertEqual(result["cleanup_status"], "pending")
        self.assertEqual(control.deleted, [runtime_id])
        self.assertEqual(logs.sources, {})
        self.assertEqual(logs.destinations, {})
        self.assertEqual(logs.deliveries, [])
        self.assertEqual(logs.groups, [])


if __name__ == "__main__":
    unittest.main()
