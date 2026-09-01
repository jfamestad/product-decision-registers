.PHONY: help install deploy diff test run

help:
	@echo "Decision Registers MCP Infrastructure"
	@echo "Targets:"
	@echo "  install   - Install dependencies (uv sync)"
	@echo "  deploy    - Deploy CDK stack"
	@echo "  diff      - Show CDK changes"
	@echo "  test      - Run test suite"
	@echo "  run       - Run the MCP server locally"

install:
	uv sync
	@echo "Dependencies installed."

deploy:
	cd infra && cdk deploy --require-approval never
	@echo "Stack deployed. On first run, attach the ManagedPolicy ARN (from outputs above) to your SSO permission set."

diff:
	cd infra && cdk diff

test:
	uv run pytest

run:
	uv run triad-dr-mcp
