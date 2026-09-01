"""triad_dr: DynamoDB-backed storage layer for the Decision Registers MCP server.

Implements the ADR / IDR / MDR data model and store operations defined in
`decision-registers-mcp-spec.md` (sections 4 and 5). This package is
transport-agnostic: `Store` is a pure Python API with no MCP awareness.
The MCP tool layer (`server.py`) is a separate consumer of `Store`.
"""

from __future__ import annotations

from triad_dr.logging import configure_logging

# stdout is the MCP protocol channel under stdio transport. Pin structured
# logging to stderr at import time so no entry point can leak a log line into
# the JSON-RPC stream. See triad_dr/logging.py.
configure_logging()

__all__ = ["configure_logging"]
__version__ = "0.1.0"
