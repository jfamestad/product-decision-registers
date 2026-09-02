.PHONY: help install deploy diff test run gateway-layer gateway-schema deploy-gateway

help:
	@echo "Decision Registers MCP Infrastructure"
	@echo "Targets:"
	@echo "  install         - Install dependencies (uv sync)"
	@echo "  deploy          - Deploy the registers stack (table, IAM policy, events)"
	@echo "  diff            - Show CDK changes"
	@echo "  test            - Run test suite"
	@echo "  run             - Run the stdio MCP server locally"
	@echo ""
	@echo "  gateway-layer   - Build the Lambda dependency layer (Linux/ARM64, no Docker)"
	@echo "  gateway-schema  - Write the generated AgentCore tool schema for inspection"
	@echo "  deploy-gateway  - Build the layer, then deploy registers + AgentCore gateway"

install:
	uv sync
	@echo "Dependencies installed."

deploy:
	cd infra && cdk deploy RegistersStack --require-approval never
	@echo "Stack deployed. On first run, attach the ManagedPolicy ARN (from outputs above) to your SSO permission set."

diff:
	cd infra && cdk diff

test:
	uv run pytest

run:
	uv run triad-dr-mcp

# The gateway Lambda imports mcp / pydantic / structlog / python-ulid, none of
# which ship with the Lambda Python runtime. There is no Docker here to bundle
# them with (same constraint the events Lambda documents), so uv cross-resolves
# them for the Lambda's platform instead. Dependencies come from the lockfile,
# not a second hand-maintained list, so the layer cannot drift from pyproject.
gateway-layer:
	rm -rf build/lambda-layer
	mkdir -p build
	uv export --no-dev --no-emit-project --no-hashes -o build/lambda-requirements.txt
	uv pip install \
		--target build/lambda-layer/python \
		--python-platform aarch64-manylinux2014 \
		--python-version 3.12 \
		-r build/lambda-requirements.txt
	@echo "Layer built at build/lambda-layer/python."

# The schema the gateway advertises is generated at synth time from the MCP
# server's own tool registry, so there is no artifact to keep in sync. This
# target just writes it out when you want to read it.
gateway-schema:
	mkdir -p build
	uv run python -c "import json; from triad_dr.gateway import tool_definitions; \
		open('build/gateway-tools.json','w').write(json.dumps(tool_definitions(), indent=2))"
	@echo "Wrote build/gateway-tools.json"

deploy-gateway: gateway-layer
	cd infra && cdk deploy RegistersStack GatewayStack --require-approval never
	@echo "Gateway deployed. Use GatewayUrl + a Cognito token; see README 'Deployable MCP endpoint'."
