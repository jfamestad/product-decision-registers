"""CDK stack for the deployable MCP endpoint: Amazon Bedrock AgentCore
Gateway, Cognito inbound authorization, and the Lambda target that runs the
same `dr_*` tool layer the stdio server runs.

This stack is **additive**. `RegistersStack` (table, IAM policy, event
pipeline) is unchanged and still deploys on its own, and the stdio server
keeps working exactly as before whether or not this stack exists. Deploying
it adds a second way to reach the same tools over the same table -- it does
not migrate anything.

Shape:

    MCP client --(bearer token)--> AgentCore Gateway --(IAM role)--> Lambda
         |                              |                              |
    Cognito token endpoint      CUSTOM_JWT authorizer        triad_dr.gateway.handler
                                                                       |
                                                             MCPServer.call_tool
                                                                       |
                                                              Store -> DynamoDB

Why a Lambda target rather than hosting the MCP server on AgentCore
Runtime: Runtime takes a container image, which means an ECR repository and
a Docker build. This repo has no Docker available (see the README's
gotchas), and a Lambda plus a uv-built dependency layer needs none. The
tool layer is identical either way -- `handler` dispatches through
`MCPServer.call_tool`, the same entry point stdio uses -- so moving to
Runtime later replaces this stack's packaging without touching a tool.
"""

import json
import re
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct

from stacks.registers_stack import TABLE_NAME
from triad_dr.gateway import TOOL_NAME_DELIMITER, tool_definitions

# infra/stacks/gateway_stack.py -> infra/stacks -> infra -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Same asset as the events Lambda: the whole `src` directory, so `triad_dr`
# stays an importable package at runtime.
_SRC_DIR = _REPO_ROOT / "src"

# Built by `make gateway-layer`, which cross-resolves the project's runtime
# dependencies for Linux/ARM64 with uv. Not checked in and not built at
# synth time -- synth stays offline and fast. `app.py` reads this to decide
# whether the gateway stack can be added to the app at all, so that
# `make deploy` (registers only) never needs a layer built.
LAYER_DIR = _REPO_ROOT / "build" / "lambda-layer"

# The gateway prefixes every tool with its target's name, so the tools
# arrive at an MCP client as "registers___dr_create". Kept short and
# hyphen-free: the tool name a model sees is this plus the delimiter plus
# the tool, and the target name pattern is ([0-9a-zA-Z][-]?){1,100}.
_TARGET_NAME = "registers"

# CreateGateway's documented name pattern: ([0-9a-zA-Z][-]?){1,48}.
# Alphanumerics with optional single hyphens -- underscores are rejected.
_GATEWAY_NAME = "triad-dr-registers"

# Cognito hosted-domain prefixes are global across all AWS accounts, so the
# default here can collide with someone else's. Override at synth:
#   cdk deploy GatewayStack -c cognitoDomainPrefix=triad-dr-<something>
_DEFAULT_DOMAIN_PREFIX = "triad-dr-registers"

# Cognito's own constraint on a hosted-UI domain prefix.
_DOMAIN_PREFIX_PATTERN = r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$"

# The OAuth resource server and scope a caller has to request. A token
# without this scope is rejected by the gateway's authorizer.
_RESOURCE_SERVER_ID = "triad-dr"
_SCOPE_NAME = "invoke"

# A conservative ceiling on the inline tool schema. AgentCore does not
# document a limit on inlinePayload, and the twenty tool descriptions are
# the product here -- they carry the governance rules a model reads before
# calling anything, so truncating them to fit would defeat the point.
# Failing synth with a pointer to the S3 alternative beats discovering the
# limit during a deploy that half-succeeded.
_MAX_INLINE_SCHEMA_BYTES = 120_000


class GatewayStack(cdk.Stack):
    """Cognito-authorized AgentCore Gateway over the decision registers tools.

    Resources:
      - Cognito user pool, hosted domain, resource server (`triad-dr/invoke`)
        and a machine-to-machine app client using the client-credentials
        grant
      - Lambda (`triad_dr.gateway.handler`) plus a dependency layer, granted
        read/write on the registers table
      - IAM service role the gateway assumes to invoke that Lambda
      - AgentCore Gateway with a `CUSTOM_JWT` authorizer pointed at the user
        pool, and one MCP Lambda target carrying the generated tool schema

    Attributes:
        gateway: The AgentCore gateway. `gateway.attr_gateway_url` is the
            MCP endpoint clients connect to.
        handler_function: The Lambda target running the tool layer.
        user_pool: The Cognito user pool authorizing inbound requests.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        """Initialize the GatewayStack.

        Args:
            scope: The construct scope.
            construct_id: The construct ID.
            **kwargs: Additional keyword arguments to pass to the Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        # Imported by name rather than taken as a cross-stack prop. Passing
        # the construct would add a CloudFormation export to RegistersStack,
        # and `make deploy` (registers only, gateway not in the app) would
        # then try to delete an export the gateway still imports -- a
        # deployment that fails for reasons nothing in this repo explains.
        # The name is a shared constant, not a guess.
        table = dynamodb.Table.from_table_name(self, "RegistersTable", TABLE_NAME)

        domain_prefix = self._resolve_domain_prefix()

        self.user_pool, domain, client, scope_string = self._create_cognito(domain_prefix)
        self.handler_function = self._create_handler_function(table)
        gateway_role = self._create_gateway_role(self.handler_function)
        self.gateway = self._create_gateway(gateway_role, client)
        self._create_target(self.gateway)

        self._create_outputs(domain, client, scope_string)

    # ------------------------------------------------------------------
    # Cognito (inbound authorization)
    # ------------------------------------------------------------------

    def _resolve_domain_prefix(self) -> str:
        """Resolve and validate the Cognito hosted-domain prefix.

        Cannot be derived from the account ID: `self.account` is an
        unresolved token in an environment-agnostic stack, and CDK
        validates this prop against a literal regex at synth time.

        Returns:
            The domain prefix to use.

        Raises:
            ValueError: If a context-supplied prefix is not a legal Cognito
                domain prefix. Caught here so the failure names the fix
                rather than surfacing as a CloudFormation rollback.
        """
        prefix = self.node.try_get_context("cognitoDomainPrefix") or _DEFAULT_DOMAIN_PREFIX
        if not re.match(_DOMAIN_PREFIX_PATTERN, prefix):
            raise ValueError(
                f"cognitoDomainPrefix {prefix!r} is not a legal Cognito domain prefix "
                f"(must match {_DOMAIN_PREFIX_PATTERN}). "
                "Pass a valid one with: cdk deploy -c cognitoDomainPrefix=<prefix>"
            )
        return prefix

    def _create_cognito(
        self, domain_prefix: str
    ) -> tuple[cognito.UserPool, cognito.UserPoolDomain, cognito.UserPoolClient, str]:
        """Create the user pool, domain, resource server, and M2M client.

        Self sign-up is disabled: this pool exists to gate a decision
        register, so identities are admin-created rather than anyone with
        an email address being able to mint one.

        The app client uses the **client-credentials** grant. An MCP client
        is a headless process with no browser to complete an authorization
        code flow, so the credential it presents is a client ID and secret
        exchanged for a bearer token at the pool's token endpoint.

        Args:
            domain_prefix: Globally unique hosted-domain prefix.

        Returns:
            `(user_pool, domain, client, scope_string)`, where
            `scope_string` is the full `resource/scope` a caller requests.
        """
        user_pool = cognito.UserPool(
            self,
            "RegistersUserPool",
            user_pool_name="triad-dr-registers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # A user pool holds real identities; losing it on a stack
            # teardown is not recoverable, so it outlives the stack in the
            # same way the registers table does.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        domain = user_pool.add_domain(
            "RegistersUserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        scope = cognito.ResourceServerScope(
            scope_name=_SCOPE_NAME,
            scope_description="Invoke decision register tools through the AgentCore gateway",
        )
        resource_server = user_pool.add_resource_server(
            "RegistersResourceServer",
            identifier=_RESOURCE_SERVER_ID,
            scopes=[scope],
        )

        client = user_pool.add_client(
            "RegistersGatewayClient",
            user_pool_client_name="triad-dr-gateway",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(resource_server, scope)],
            ),
            access_token_validity=cdk.Duration.hours(1),
        )

        return user_pool, domain, client, f"{_RESOURCE_SERVER_ID}/{_SCOPE_NAME}"

    # ------------------------------------------------------------------
    # The Lambda target
    # ------------------------------------------------------------------

    def _create_dependency_layer(self) -> _lambda.LayerVersion:
        """Create the layer carrying the project's runtime dependencies.

        The Lambda imports `mcp`, `pydantic`, `structlog` and `python-ulid`,
        none of which ship with the Lambda Python runtime, and this repo
        cannot bundle them at synth time -- there is no Docker here, which
        is the same constraint the events Lambda documents. `make
        gateway-layer` resolves them for Linux/ARM64 with uv instead, which
        needs no container.

        Returns:
            The layer version built from `build/lambda-layer`.

        Raises:
            ValueError: If the layer has not been built. Synth fails with
                the command to run rather than deploying a function that
                would `ImportError` on its first invocation.
        """
        if not (LAYER_DIR / "python").is_dir():
            raise ValueError(
                f"Lambda dependency layer not found at {LAYER_DIR}/python. "
                "Build it first:  make gateway-layer\n"
                "The gateway Lambda imports mcp/pydantic/structlog/python-ulid, "
                "none of which are in the Lambda runtime. Deploying without the "
                "layer produces a function that fails on its first invocation."
            )

        return _lambda.LayerVersion(
            self,
            "GatewayDepsLayer",
            code=_lambda.Code.from_asset(str(LAYER_DIR)),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
            description="mcp, pydantic, structlog and python-ulid for the gateway target",
        )

    def _create_handler_function(self, table: dynamodb.ITable) -> _lambda.Function:
        """Create the Lambda the gateway invokes for every tool call.

        `DR_PROJECT` is deliberately **not** set. stdio gets its project
        namespace from the repo's `.mcp.json`; a gateway serves every repo
        at once, so callers pass `project=` per call. A default here would
        silently write records into whichever project happened to be
        configured.

        Args:
            table: The registers table to grant access to.

        Returns:
            The configured Lambda function.
        """
        function = _lambda.Function(
            self,
            "GatewayToolFunction",
            function_name="triad-dr-gateway-tools",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="triad_dr.gateway.handler",
            code=_lambda.Code.from_asset(str(_SRC_DIR)),
            layers=[self._create_dependency_layer()],
            # Importing mcp + pydantic dominates a cold start; 512 MB keeps
            # that under a second and costs nothing extra per call, since
            # Lambda bills GB-seconds and the extra memory shortens the run.
            memory_size=512,
            timeout=cdk.Duration.seconds(30),
            environment={"DR_TABLE": table.table_name},
            log_group=logs.LogGroup(
                self,
                "GatewayToolFunctionLogs",
                # Named explicitly. An unnamed LogGroup gets a CDK-generated
                # physical name, so `aws logs tail /aws/lambda/<function>` --
                # the first thing anyone tries -- finds nothing at all.
                log_group_name="/aws/lambda/triad-dr-gateway-tools",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            description="AgentCore Gateway target: the dr_* MCP tool layer over the registers table",
        )
        table.grant_read_write_data(function)
        return function

    # ------------------------------------------------------------------
    # The gateway
    # ------------------------------------------------------------------

    def _create_gateway_role(self, function: _lambda.Function) -> iam.Role:
        """Create the service role AgentCore assumes to invoke the target.

        Scoped two ways: the trust policy only lets the AgentCore service
        assume it on behalf of gateways in this account, and the permission
        policy only allows invoking this one function.

        Args:
            function: The Lambda target the gateway must be able to invoke.

        Returns:
            The gateway service role.
        """
        role = iam.Role(
            self,
            "GatewayServiceRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    # The gateway ARN is not known until the gateway exists,
                    # so this wildcards the gateway name while still pinning
                    # the account, partition and region.
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:bedrock-agentcore:"
                            f"{self.region}:{self.account}:gateway/*"
                        )
                    },
                },
            ),
            description="Role AgentCore Gateway assumes to invoke the decision registers tools",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[function.function_arn],
            )
        )
        return role

    def _create_gateway(
        self, role: iam.Role, client: cognito.UserPoolClient
    ) -> agentcore.CfnGateway:
        """Create the MCP gateway with Cognito inbound authorization.

        `CUSTOM_JWT` means the gateway validates a bearer token against the
        user pool's OIDC discovery document before any target runs.
        Unauthenticated requests are rejected at the gateway, so the Lambda
        is never invoked and the table is never touched.

        `allowedClients` (not `allowedAudience`) is the right constraint for
        a client-credentials token: those carry `client_id` and no `aud`
        claim, so matching on audience would reject every valid token.

        Args:
            role: The gateway service role.
            client: The Cognito app client whose tokens are accepted.

        Returns:
            The gateway.
        """
        discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}/.well-known/openid-configuration"
        )

        return agentcore.CfnGateway(
            self,
            "RegistersGateway",
            name=_GATEWAY_NAME,
            protocol_type="MCP",
            role_arn=role.role_arn,
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_clients=[client.user_pool_client_id],
                )
            ),
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    instructions=(
                        "Decision registers for one developer across many projects: "
                        "IDR (Investor, the bet), MDR (Seller, market and user), ADR "
                        "(Builder, architecture). Every ADR and MDR links to a "
                        "governing IDR -- no bet, no work. Every tool takes an "
                        "explicit project= argument; this gateway serves many "
                        "projects and never guesses a namespace."
                    ),
                )
            ),
            description="MCP endpoint for the triad-dr decision registers",
        )

    def _create_target(self, gateway: agentcore.CfnGateway) -> agentcore.CfnGatewayTarget:
        """Create the Lambda target carrying the generated tool schema.

        The schema is generated from the running MCP server's own tool
        registry (`triad_dr.gateway.tool_definitions`), not hand-written, so
        the gateway advertises exactly the tools the stdio server registers.
        Adding a tool to `server.py` and re-synthesizing is the whole update
        path; there is no second definition to keep in step.

        Args:
            gateway: The gateway to attach the target to.

        Returns:
            The gateway target.

        Raises:
            ValueError: If the generated schema exceeds
                `_MAX_INLINE_SCHEMA_BYTES`.
        """
        definitions = tool_definitions()
        payload_bytes = len(json.dumps(definitions))
        if payload_bytes > _MAX_INLINE_SCHEMA_BYTES:
            raise ValueError(
                f"Generated tool schema is {payload_bytes} bytes, over the "
                f"{_MAX_INLINE_SCHEMA_BYTES}-byte ceiling this stack sets for an "
                "inline payload. Switch the target to the S3 tool-schema option "
                "(ToolSchemaProperty(s3=...)) and upload the generated JSON, "
                "rather than trimming the tool descriptions -- they carry the "
                "governance rules a model reads before calling anything."
            )

        target = agentcore.CfnGatewayTarget(
            self,
            "RegistersTarget",
            name=_TARGET_NAME,
            gateway_identifier=gateway.attr_gateway_identifier,
            description="The dr_* decision register tools, backed by a Lambda",
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    # The gateway calls the Lambda as itself, using the
                    # service role above. No outbound credential to broker.
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=self.handler_function.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=definitions,
                        ),
                    )
                )
            ),
        )
        target.add_dependency(gateway)
        return target

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _create_outputs(
        self,
        domain: cognito.UserPoolDomain,
        client: cognito.UserPoolClient,
        scope_string: str,
    ) -> None:
        """Emit everything needed to point an MCP client at this gateway.

        The client secret is deliberately not an output -- CloudFormation
        outputs are readable by anyone with describe-stacks. The command to
        fetch it is emitted instead.

        Args:
            domain: The Cognito hosted domain.
            client: The app client.
            scope_string: The full OAuth scope callers request.
        """
        cdk.CfnOutput(
            self,
            "GatewayUrl",
            value=self.gateway.attr_gateway_url,
            description="MCP endpoint. Point an MCP client here with a bearer token.",
        )

        cdk.CfnOutput(
            self,
            "GatewayArn",
            value=self.gateway.attr_gateway_arn,
            description="ARN of the AgentCore gateway",
        )

        cdk.CfnOutput(
            self,
            "GatewayToolPrefix",
            value=f"{_TARGET_NAME}{TOOL_NAME_DELIMITER}",
            description=(
                "Prefix the gateway adds to every tool name "
                f"(e.g. {_TARGET_NAME}{TOOL_NAME_DELIMITER}dr_create)"
            ),
        )

        cdk.CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.user_pool.user_pool_id,
            description="User pool authorizing inbound gateway requests",
        )

        cdk.CfnOutput(
            self,
            "CognitoClientId",
            value=client.user_pool_client_id,
            description="App client ID for the client-credentials token request",
        )

        cdk.CfnOutput(
            self,
            "CognitoTokenEndpoint",
            value=f"{domain.base_url()}/oauth2/token",
            description="POST client_credentials here to get a bearer token",
        )

        cdk.CfnOutput(
            self,
            "CognitoScope",
            value=scope_string,
            description="OAuth scope to request; a token without it is rejected",
        )

        cdk.CfnOutput(
            self,
            "CognitoClientSecretCommand",
            value=(
                "aws cognito-idp describe-user-pool-client "
                f"--user-pool-id {self.user_pool.user_pool_id} "
                f"--client-id {client.user_pool_client_id} "
                "--query UserPoolClient.ClientSecret --output text"
            ),
            description="Run this to read the client secret; it is not exported as an output",
        )

        cdk.CfnOutput(
            self,
            "GatewayToolFunctionName",
            value=self.handler_function.function_name,
            description="Lambda running the dr_* tool layer behind the gateway",
        )
