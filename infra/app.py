"""CDK app for decision registers infrastructure.

Two stacks, deployed independently:

  - `RegistersStack` -- the table, IAM policy, and event pipeline. Everything
    the stdio MCP server needs, and the only stack `make deploy` touches.
  - `GatewayStack` -- the Cognito-authorized AgentCore Gateway endpoint, for
    reaching the same tools without a local checkout. Purely additive; the
    stdio path neither knows nor cares whether it is deployed.

`GatewayStack` is only added to the app once its Lambda dependency layer has
been built (`make gateway-layer`, which `make deploy-gateway` runs for you).
Otherwise `make deploy` -- which has never needed a layer and still doesn't --
would start failing at synth for a stack the operator never asked for. The
omission prints a line to stderr so a missing stack is never a mystery.

Both stacks are named explicitly in the Makefile's `cdk` invocations: a bare
`cdk deploy` is an error in a multi-stack app, and `make deploy` quietly
deploying a gateway would be a surprise.
"""

import sys
from pathlib import Path

import aws_cdk as cdk

from stacks.gateway_stack import LAYER_DIR, GatewayStack
from stacks.registers_stack import RegistersStack


def main() -> None:
    """Initialize and synthesize the CDK app."""
    app = cdk.App()

    RegistersStack(
        app,
        "RegistersStack",
        description="DynamoDB table and IAM policy for decision registers MCP server",
    )

    if Path(LAYER_DIR / "python").is_dir():
        GatewayStack(
            app,
            "GatewayStack",
            description=(
                "Cognito-authorized AgentCore Gateway MCP endpoint for decision registers"
            ),
        )
    else:
        print(
            f"note: GatewayStack omitted -- no Lambda dependency layer at {LAYER_DIR}/python. "
            "Run `make gateway-layer` (or `make deploy-gateway`, which builds it) to include it.",
            file=sys.stderr,
        )

    app.synth()


if __name__ == "__main__":
    main()
