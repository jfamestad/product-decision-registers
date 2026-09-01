"""Shared pytest fixtures for triad_dr store tests.

Uses moto's `mock_aws` to simulate DynamoDB, creating a table matching the
CDK definition in `infra/stacks/registers_stack.py`: pk/sk both STRING,
on-demand (PAY_PER_REQUEST) billing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from triad_dr.store import Store

TABLE_NAME = "triad-decision-registers-test"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide dummy AWS credentials and a clean env for each test.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("DR_PROJECT", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def dynamodb_resource(aws_credentials: None) -> Iterator[Any]:
    """Start moto's DynamoDB mock and create the decision registers table.

    Args:
        aws_credentials: Ensures dummy credentials are in place first.

    Yields:
        A boto3 DynamoDB ServiceResource pointed at the mocked backend.
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.Table(TABLE_NAME).wait_until_exists()
        yield resource


@pytest.fixture
def store(dynamodb_resource: Any) -> Store:
    """Build a `Store` wired to the mocked DynamoDB table.

    Args:
        dynamodb_resource: The mocked DynamoDB resource with the table
            already created.

    Returns:
        A `Store` instance ready for use in tests.
    """
    return Store(table_name=TABLE_NAME, resource=dynamodb_resource)
