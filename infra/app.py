"""CDK app for decision registers infrastructure."""

import aws_cdk as cdk

from stacks.registers_stack import RegistersStack


def main() -> None:
    """Initialize and synthesize the CDK app."""
    app = cdk.App()

    RegistersStack(
        app,
        "RegistersStack",
        description="DynamoDB table and IAM policy for decision registers MCP server",
    )

    app.synth()


if __name__ == "__main__":
    main()
