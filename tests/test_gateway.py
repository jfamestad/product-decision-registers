"""Tests for `triad_dr.gateway` — the AgentCore Gateway adapter.

Two halves, matching the module's two consumers:

  - **Schema generation**, which runs at `cdk synth`. The gateway advertises
    what these functions produce, so a translation that silently drops an
    argument produces a tool nobody can call correctly. These tests assert
    the output conforms to AgentCore's `SchemaDefinition` subset (only
    type/description/items/properties/required, only the six scalar and
    container types) and that the translator refuses, loudly, anything it
    cannot express.
  - **The Lambda handler**, which runs per tool call. The point of dispatching
    through `MCPServer.call_tool` is that the gateway inherits stdio's
    behavior rather than reimplementing it, so these assert the dispatch,
    the AgentCore tool-name prefix stripping, and that `Store`'s typed errors
    arrive as messages rather than tracebacks.

Reuses the moto fixtures from `tests/conftest.py`. Nothing here touches
`triad_dr.server`, which is the point: the gateway is additive and the stdio
path is untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from triad_dr import gateway
from triad_dr.server import build_server

# The only keys AgentCore's SchemaDefinition accepts. See
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_SchemaDefinition.html
ALLOWED_SCHEMA_KEYS = {"type", "description", "items", "properties", "required"}
ALLOWED_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _lambda_context(tool_name: str | None) -> Any:
    """Build a stand-in for the Lambda context AgentCore Gateway passes.

    Args:
        tool_name: The value for `bedrockAgentCoreToolName`, or None to
            simulate an invocation that did not come from a gateway.

    Returns:
        An object shaped like a Lambda context object.
    """
    custom = {} if tool_name is None else {"bedrockAgentCoreToolName": tool_name}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def _assert_conforms(node: dict[str, Any], path: str) -> None:
    """Assert a schema node uses only what AgentCore can express.

    Args:
        node: The translated schema node.
        path: Location, for a useful assertion message.
    """
    extra = set(node) - ALLOWED_SCHEMA_KEYS
    assert not extra, f"{path}: keys AgentCore rejects: {sorted(extra)}"
    assert node["type"] in ALLOWED_TYPES, f"{path}: bad type {node['type']!r}"
    if node["type"] == "array":
        assert "items" in node, f"{path}: array without items"
        _assert_conforms(node["items"], f"{path}[]")
    for name, sub in (node.get("properties") or {}).items():
        _assert_conforms(sub, f"{path}.{name}")
    for required in node.get("required", []):
        assert required in (node.get("properties") or {}), (
            f"{path}: required names {required!r}, which is not in properties"
        )


# ----------------------------------------------------------------------
# Schema generation (synth time)
# ----------------------------------------------------------------------


def test_tool_definitions_cover_every_registered_tool(store: Any) -> None:
    """Every tool the stdio server registers is advertised by the gateway.

    This is the drift guard. The schema is generated from the same
    `build_server` registry stdio uses, so adding a tool cannot leave the
    gateway behind -- but only if generation actually walks the whole
    registry, which is what this asserts.
    """
    import asyncio

    registered = {t.name for t in asyncio.run(build_server(store).list_tools())}
    advertised = {d["name"] for d in gateway.tool_definitions()}

    assert advertised == registered


def test_tool_definitions_conform_to_agentcore_schema() -> None:
    """No generated schema uses a construct AgentCore would reject.

    A schema the gateway rejects fails the whole `CreateGatewayTarget` call,
    so this is checked here rather than discovered during a deploy.
    """
    definitions = gateway.tool_definitions()
    assert definitions, "expected at least one tool"

    for definition in definitions:
        assert set(definition) == {"name", "description", "inputSchema"}
        assert definition["description"], f"{definition['name']}: empty description"
        assert definition["inputSchema"]["type"] == "object"
        _assert_conforms(definition["inputSchema"], definition["name"])


def test_tool_definitions_are_sorted_by_name() -> None:
    """Generation is deterministic, so a synth diff shows real changes only."""
    names = [d["name"] for d in gateway.tool_definitions()]
    assert names == sorted(names)


def test_optional_arguments_survive_translation() -> None:
    """`T | None` becomes a plain `T` that is simply not required.

    Pydantic renders optionals as `anyOf: [T, {"type": "null"}]`, which
    AgentCore has no equivalent for. Dropping the null arm is the only
    faithful translation -- getting this wrong would make every optional
    argument untypeable or mandatory.
    """
    dr_list = next(d for d in gateway.tool_definitions() if d["name"] == "dr_list")
    schema = dr_list["inputSchema"]

    assert schema["properties"]["project"] == {"type": "string"}
    assert "project" not in schema.get("required", [])


def test_literal_arguments_keep_their_allowed_values_in_the_description() -> None:
    """Enum values survive as prose, because SchemaDefinition has no `enum`.

    `Store` still rejects a bad value, so this is about a model being able
    to pick a correct one rather than about safety.
    """
    dr_create = next(d for d in gateway.tool_definitions() if d["name"] == "dr_create")
    rec_type = dr_create["inputSchema"]["properties"]["rec_type"]

    assert rec_type["type"] == "string"
    for value in ("ADR", "IDR", "MDR"):
        assert value in rec_type["description"]


def test_nested_models_are_inlined() -> None:
    """`$ref`/`$defs` are flattened; AgentCore cannot follow a reference."""
    dr_answer = next(d for d in gateway.tool_definitions() if d["name"] == "dr_answer")
    produces = dr_answer["inputSchema"]["properties"]["produces"]

    assert produces["type"] == "object"
    assert "rec_type" in produces["properties"]


def test_unsupported_schema_construct_fails_loudly() -> None:
    """An untranslatable argument raises at synth, naming where it came from.

    The alternative is a gateway advertising a schema that disagrees with
    the tool behind it, which nothing would notice until a call failed in a
    way that looks like the caller's fault.
    """
    with pytest.raises(ValueError, match="dr_thing.weird"):
        gateway._to_schema_definition({"type": "string", "pattern": "^x"}, "dr_thing.weird")

    with pytest.raises(ValueError, match="non-null variant"):
        gateway._to_schema_definition(
            {"anyOf": [{"type": "string"}, {"type": "integer"}]}, "dr_thing.union"
        )


# ----------------------------------------------------------------------
# Tool-name handling
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("registers___dr_create", "dr_create"),
        ("SomeTarget___dr_list", "dr_list"),
        # No prefix: a direct invocation for debugging behaves the same.
        ("dr_get", "dr_get"),
        # Only the first delimiter is a separator.
        ("registers___dr___odd", "dr___odd"),
    ],
)
def test_strip_target_prefix(raw: str, expected: str) -> None:
    """The gateway's `${target}___${tool}` prefix is removed, once."""
    assert gateway.strip_target_prefix(raw) == expected


def test_handler_without_gateway_context_fails_clearly() -> None:
    """Invoked by something other than a gateway, it says so.

    Guessing a tool from the payload would be worse than failing: the
    arguments alone do not identify which of twenty tools was meant.
    """
    with pytest.raises(ValueError, match="bedrockAgentCoreToolName"):
        gateway.handler({}, _lambda_context(None))


# ----------------------------------------------------------------------
# The handler (runtime)
# ----------------------------------------------------------------------


@pytest.fixture
def gateway_server(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the handler's cached server at the moto-backed store.

    The module-level cache is a warm-container optimization; a test that
    inherited one container's server would be testing the wrong table.
    """
    gateway._reset_cache()
    monkeypatch.setattr(gateway, "_SERVER", build_server(store))
    yield
    gateway._reset_cache()


@pytest.mark.usefixtures("gateway_server")
def test_handler_dispatches_a_prefixed_tool_name() -> None:
    """A gateway-shaped invocation reaches the tool and returns its result."""
    result = gateway.handler(
        {"rec_type": "IDR"}, _lambda_context("registers___dr_template")
    )

    assert result["rec_type"] == "IDR"
    assert result["persona"] == "investor"
    assert "bet" in result["fields"]


@pytest.mark.usefixtures("gateway_server")
def test_handler_creates_a_record_through_the_real_tool_layer(store: Any) -> None:
    """The handler writes through `Store`, not around it.

    Governance warnings computed by `Store` have to survive the trip --
    an unfunded ADR warns and succeeds here exactly as it does over stdio,
    because it is the same code path.
    """
    result = gateway.handler(
        {"rec_type": "ADR", "title": "Use DynamoDB", "project": "demo"},
        _lambda_context("registers___dr_create"),
    )

    assert result["ref"].startswith("ADR-")
    assert result["title"] == "Use DynamoDB"
    # No governing IDR exists, so the no-bet-no-work warning must be present.
    assert any("bet" in w.lower() for w in result.get("warnings", []))


@pytest.mark.usefixtures("gateway_server")
def test_handler_returns_typed_errors_as_messages() -> None:
    """A known failure comes back as `{"error": ...}`, never a stack trace.

    `Store`'s errors carry their own remediation, and that text is worth
    more to a caller than a Lambda traceback.
    """
    result = gateway.handler(
        {"ref": "ADR-9999", "project": "demo"}, _lambda_context("registers___dr_get")
    )

    assert "error" in result
    assert "ADR-9999" in result["error"]


@pytest.mark.usefixtures("gateway_server")
def test_handler_returns_validation_errors_as_messages() -> None:
    """Bad arguments are a message too, not an unhandled exception."""
    result = gateway.handler(
        {"rec_type": "XXX"}, _lambda_context("registers___dr_template")
    )

    assert "error" in result


@pytest.mark.usefixtures("gateway_server")
def test_handler_rejects_an_unknown_tool_as_a_message() -> None:
    """A tool the server does not have fails as a message, not a crash."""
    result = gateway.handler({}, _lambda_context("registers___dr_nonexistent"))

    assert "error" in result


@pytest.mark.usefixtures("gateway_server")
def test_handler_tolerates_a_missing_event_body() -> None:
    """A no-argument tool can be invoked with an empty or absent payload."""
    result = gateway.handler(None, _lambda_context("registers___dr_projects"))

    assert "projects" in result


@pytest.mark.usefixtures("gateway_server")
def test_handler_requires_an_explicit_project() -> None:
    """With no `DR_PROJECT` on the Lambda, a caller must name the project.

    This is the multi-tenancy guard. One gateway serves every repo, so a
    defaulted namespace would write real records into the wrong project.
    """
    result = gateway.handler(
        {"rec_type": "ADR", "title": "No project given"},
        _lambda_context("registers___dr_create"),
    )

    assert "error" in result
    assert "project" in result["error"].lower()
