"""Amazon Bedrock AgentCore Gateway adapter for the decision registers tool layer.

The stdio server (`triad_dr.server`) stays the only implementation of the
`dr_*` tools. This module makes that same server reachable over an
AgentCore Gateway MCP endpoint, and it does so without touching a line of
it: `build_server` is imported, never modified, subclassed, or copied.

Two consumers, one source of truth:

  - **At synth time**, `tool_definitions()` asks a schema-only server for
    its own tool list and translates it into the `ToolDefinition` shape
    AgentCore accepts. `infra/stacks/gateway_stack.py` inlines the result
    into the gateway target, so the gateway advertises exactly the tools
    `build_server` registers. It cannot fall behind: there is no
    hand-maintained schema to forget to update.
  - **At runtime**, `handler` is the Lambda the gateway invokes. It strips
    the target-name prefix the gateway prepends, dispatches through the
    same `MCPServer.call_tool` path the stdio transport uses, and returns
    the tool's structured result.

Because dispatch goes through `call_tool`, every behavior the stdio
transport has -- pydantic argument validation, `Store`'s typed errors
arriving as actionable messages, the governance warnings riding along in
the response -- is the gateway's behavior too, by construction rather
than by parallel implementation.

**One thing does differ, deliberately: the MCP `isError` flag.** A Lambda
target returns a value; it does not get to set the flag on the envelope
the gateway builds, so a failed tool call arrives as a *successful* result
whose payload is `{"error": "<message>"}` and `isError: false`. The
alternative is raising, which makes the gateway return `isError: true`
with a generic message and throws away the remediation text -- and that
text ("SSO session expired -- run: aws sso login ...") is the thing this
codebase spends the most effort producing. A caller that branches on
`isError` rather than reading the payload will misread a failure as a
success; a caller that reads the payload gets strictly more than the flag
would have told it. Setting the gateway's `exceptionLevel` to `DEBUG`
would recover both at the cost of exposing granular internals to every
caller.

**The project namespace is per-call here, not per-process.** stdio runs
one server per repo and reads `DR_PROJECT` from that repo's `.mcp.json`.
A gateway is one deployment serving every caller, so there is no repo to
read it from: callers pass `project=` explicitly, and a tool called
without it fails with `NoProjectError` rather than silently writing to
someone else's namespace. Setting `DR_PROJECT` on the Lambda is supported
for a single-project gateway, but it is deliberately not the default --
a wrong default here writes real records to the wrong project.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import boto3
import structlog
from botocore.config import Config
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from triad_dr.server import DEFAULT_TABLE_NAME, build_server
from triad_dr.store import Store

logger = structlog.get_logger(__name__)

# AgentCore Gateway exposes a target's tools as "${target_name}___${tool_name}",
# so the handler has to strip the prefix before dispatching. Documented at
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html
TOOL_NAME_DELIMITER = "___"

# The JSON Schema keywords the translation below knows how to handle.
# Anything outside this set raises at synth time rather than producing a
# target whose advertised schema quietly disagrees with the tool's real
# signature -- the same reason `registers_stack._validate_filter_policy`
# fails synth on a bad filter key instead of letting a subscriber go deaf.
_KNOWN_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "default",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "title",
        "type",
    }
)

# AgentCore's SchemaDefinition accepts only these types. See
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_SchemaDefinition.html
_SUPPORTED_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})


# ----------------------------------------------------------------------
# Synth-time: MCP tool schemas -> AgentCore ToolDefinitions
# ----------------------------------------------------------------------


def _schema_only_store() -> Store:
    """Build a `Store` that can describe itself but never talk to AWS.

    `tool_definitions()` runs during `cdk synth`, where requiring real
    credentials or a resolved region to read a *schema* would be absurd --
    and would make synth fail on a laptop with an expired SSO session.
    `Store.__init__` only calls `resource.Table(name)`, so a stub with
    that one method is enough to construct the server; every tool closure
    captures it but none of them run.

    Returns:
        A `Store` whose DynamoDB resource is an inert stub.
    """
    return Store(table_name="schema-only", resource=SimpleNamespace(Table=lambda _name: None))


def _describe(node: dict[str, Any]) -> str | None:
    """Build a property description that survives the keywords AgentCore drops.

    `SchemaDefinition` has no `enum` and no `default`, so a `Literal`
    argument would reach the gateway as a bare string with its allowed
    values nowhere in sight. Folding both into the description keeps the
    hint where a model reading the tool list will actually see it.
    `Store` still validates the value either way, so this costs hinting,
    never safety.

    Args:
        node: The JSON Schema node for one property.

    Returns:
        The description text, or None when there is nothing to say.
    """
    parts: list[str] = []
    if node.get("description"):
        parts.append(str(node["description"]).strip())
    if "enum" in node:
        allowed = ", ".join(json.dumps(v) for v in node["enum"])
        parts.append(f"One of: {allowed}.")
    if node.get("default") is not None:
        parts.append(f"Defaults to {json.dumps(node['default'])}.")
    return " ".join(parts) or None


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every `$ref` with the definition it points at.

    Pydantic emits `$defs` + `$ref` for nested models (`dr_answer`'s
    `produces`, `dr_supersede`'s `new_record`). AgentCore has no notion of
    either, so the tree is flattened before translation.

    Args:
        node: A JSON Schema fragment.
        defs: The schema's `$defs` map.

    Returns:
        The fragment with refs resolved in place.

    Raises:
        ValueError: If a `$ref` points outside `#/$defs/`, or at a name
            that is not there.
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise ValueError(f"Unsupported $ref target {ref!r}; only {prefix}* is resolvable.")
        name = ref[len(prefix) :]
        if name not in defs:
            raise ValueError(f"$ref {ref!r} names a definition that is not in $defs.")
        # Merge any siblings of the $ref (pydantic adds `default`/`title`
        # alongside it) over the resolved target, so they are not lost.
        resolved = dict(_inline_refs(defs[name], defs))
        resolved.update({k: v for k, v in node.items() if k != "$ref"})
        return resolved
    return {k: _inline_refs(v, defs) for k, v in node.items()}


def _to_schema_definition(node: dict[str, Any], path: str) -> dict[str, Any]:
    """Translate one JSON Schema node into an AgentCore `SchemaDefinition`.

    Handles the four shapes pydantic actually emits for these tools:
    optionals as `anyOf: [T, {"type": "null"}]`, `Literal` as
    `enum` + `type`, free-form mappings as `additionalProperties`, and
    ordinary object/array nesting.

    Args:
        node: The JSON Schema node to translate.
        path: Dotted location of this node, used in error messages so an
            unsupported construct names the tool and argument it came from.

    Returns:
        A dict with only the keys `SchemaDefinition` allows: `type`, and
        optionally `description`, `items`, `properties`, `required`.

    Raises:
        ValueError: If the node uses a JSON Schema construct AgentCore
            cannot express. Raised at synth time deliberately -- the
            alternative is a gateway advertising a schema that disagrees
            with the tool behind it.
    """
    unknown = set(node) - _KNOWN_KEYWORDS
    if unknown:
        raise ValueError(
            f"{path}: unsupported JSON Schema keyword(s) {sorted(unknown)}. "
            "AgentCore's SchemaDefinition accepts only type/description/items/"
            "properties/required; teach triad_dr.gateway how to express this "
            "or change the tool signature."
        )

    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        if len(variants) != 1:
            raise ValueError(
                f"{path}: anyOf with {len(variants)} non-null variant(s) has no "
                "SchemaDefinition equivalent. Only Optional[T] (T | None) translates."
            )
        # Carry the outer description/default onto the surviving variant --
        # pydantic puts them beside the anyOf, not inside it.
        merged = dict(variants[0])
        for key in ("description", "default"):
            if key in node and key not in merged:
                merged[key] = node[key]
        return _to_schema_definition(merged, path)

    node_type = node.get("type")
    if node_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"{path}: type {node_type!r} is not one of {sorted(_SUPPORTED_TYPES)}."
        )

    out: dict[str, Any] = {"type": node_type}
    description = _describe(node)
    if description:
        out["description"] = description

    if node_type == "array":
        items = node.get("items")
        if isinstance(items, dict):
            out["items"] = _to_schema_definition(items, f"{path}[]")
        else:
            # An untyped list. AgentCore requires `items`, and a string is
            # the only honest lowest common denominator.
            out["items"] = {"type": "string"}

    if node_type == "object":
        properties = node.get("properties")
        if properties:
            out["properties"] = {
                name: _to_schema_definition(sub, f"{path}.{name}")
                for name, sub in properties.items()
            }
            required = [r for r in node.get("required", []) if r in properties]
            if required:
                out["required"] = required
        # `additionalProperties` (a free-form dict like `fields` or `patch`)
        # has no SchemaDefinition equivalent, so the object is advertised
        # without properties. Store validates the contents on the way in.

    return out


def tool_definitions(server: MCPServer | None = None) -> list[dict[str, Any]]:
    """Render every registered MCP tool as an AgentCore `ToolDefinition`.

    Args:
        server: The server to read tools from. Defaults to a schema-only
            server built by `_schema_only_store` -- no AWS calls, no
            credentials needed, which is what makes this callable from
            `cdk synth`.

    Returns:
        One dict per tool, each `{"name", "description", "inputSchema"}`,
        sorted by name so the synthesized template is stable across runs
        and a diff shows real changes rather than dict ordering.

    Raises:
        ValueError: If any tool's schema uses a construct AgentCore cannot
            express. See `_to_schema_definition`.
    """
    server = server if server is not None else build_server(_schema_only_store())
    tools = asyncio.run(server.list_tools())

    definitions: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: t.name):
        schema = dict(tool.input_schema or {})
        defs = schema.pop("$defs", {})
        schema = _inline_refs(schema, defs)
        input_schema = _to_schema_definition(schema, tool.name)
        # A tool with no arguments still needs an object-typed inputSchema.
        input_schema.setdefault("type", "object")
        definitions.append(
            {
                "name": tool.name,
                "description": tool.description or tool.name,
                "inputSchema": input_schema,
            }
        )
    return definitions


# ----------------------------------------------------------------------
# Runtime: the Lambda the gateway invokes
# ----------------------------------------------------------------------

# Built once per execution environment and reused across warm invocations.
# This is the one place the codebase keeps a module-level singleton: a
# Lambda that rebuilt the boto3 resource and re-registered twenty tools on
# every call would pay that cost per request for no benefit. It is still
# dependency-injected underneath -- `build_server(store)` is the same call
# `main()` makes -- and `_reset_cache()` exists so tests never inherit one
# container's server.
_SERVER: MCPServer | None = None


def _server() -> MCPServer:
    """Return the cached `MCPServer`, building it on first use.

    Returns:
        A server wired to a `Store` over the table named by `DR_TABLE`
        (default `triad-decision-registers`).
    """
    global _SERVER  # noqa: PLW0603 - warm-container cache; see _SERVER's comment.
    if _SERVER is None:
        table_name = os.environ.get("DR_TABLE", DEFAULT_TABLE_NAME)
        config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 3})
        store = Store(table_name=table_name, resource=boto3.resource("dynamodb", config=config))
        _SERVER = build_server(store)
        logger.info("gateway.server_built", table=table_name)
    return _SERVER


def _reset_cache() -> None:
    """Drop the cached server. For tests only."""
    global _SERVER  # noqa: PLW0603 - see _SERVER's comment.
    _SERVER = None


def strip_target_prefix(tool_name: str) -> str:
    """Remove the `${target_name}___` prefix AgentCore adds to tool names.

    Args:
        tool_name: The name as the gateway reports it, e.g.
            `registers___dr_create`.

    Returns:
        The bare tool name (`dr_create`). Returned unchanged when the
        delimiter is absent, so a direct invocation for debugging works
        the same as one routed through a gateway.
    """
    index = tool_name.find(TOOL_NAME_DELIMITER)
    if index == -1:
        return tool_name
    return tool_name[index + len(TOOL_NAME_DELIMITER) :]


def _tool_name_from_context(context: Any) -> str:
    """Read the invoked tool's name out of the Lambda client context.

    Args:
        context: The Lambda context object. The gateway puts its metadata
            in `context.client_context.custom`.

    Returns:
        The bare tool name, prefix stripped.

    Raises:
        ValueError: If the context carries no `bedrockAgentCoreToolName`.
            That means the function was invoked by something other than an
            AgentCore Gateway, and guessing a tool would be worse than
            failing.
    """
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName")
    if not raw:
        raise ValueError(
            "No 'bedrockAgentCoreToolName' in the Lambda client context. This "
            "function is an AgentCore Gateway target and cannot infer which "
            "tool to run from the payload alone."
        )
    return strip_target_prefix(str(raw))


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """AgentCore Gateway Lambda target entry point.

    The gateway passes the tool's arguments as the event (a flat map of
    the `inputSchema` properties) and the tool name in the client context.

    Args:
        event: The tool arguments. `None` or `{}` for a no-argument tool.
        context: The Lambda context, carrying the gateway metadata.

    Returns:
        The tool's structured result as a JSON-serializable dict, or
        `{"error": "<message>"}` when the tool raised a known failure.

        Errors become messages here for the same reason they do in
        `server.py`: `Store`'s typed errors already carry actionable
        remediation ("SSO session expired -- run: aws sso login ..."), and
        that text is worth more to the caller than a Lambda stack trace.
        Genuine bugs are deliberately *not* caught -- they propagate, fail
        the invocation, and land in CloudWatch as the defects they are.

    Raises:
        ValueError: If the client context carries no tool name.
    """
    tool_name = _tool_name_from_context(context)
    arguments = event or {}

    try:
        result = asyncio.run(_server().call_tool(tool_name, arguments))
    except ToolError as exc:
        logger.info("gateway.tool_error", tool=tool_name, error=str(exc))
        return {"error": str(exc)}

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    # Every dr_* tool returns a dict, so this is a defensive fallback
    # rather than a path taken in practice.
    text = "".join(getattr(block, "text", "") for block in getattr(result, "content", []))
    return {"result": text}
