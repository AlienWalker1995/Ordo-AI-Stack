# Ordo AI Stack Makefile
.PHONY: test test-audit test-dashboard test-gateway smoke-test lint help \
        decrypt-secrets up down logs rotate-internal-tokens

RUNTIME_ENV := $(HOME)/.ai-toolkit/runtime/.env
# Every docker-compose invocation that touches services with SOPS-substituted
# variables (oauth2-proxy, hermes-gateway, etc.) must pass BOTH env files —
# compose's `--env-file` REPLACES, not merges, so a single --env-file pointing
# at runtime/.env loses every feature flag in .env and vice versa.
COMPOSE := docker compose --env-file .env --env-file $(RUNTIME_ENV)

help:
	@echo "Targets:"
	@echo "  decrypt-secrets - Decrypt secrets/*.sops to ~/.ai-toolkit/runtime/"
	@echo "  up            - decrypt-secrets + docker compose up -d (full stack)"
	@echo "  down          - docker compose down"
	@echo "  logs          - tail docker compose logs"
	@echo "  test          - Run all tests"
	@echo "  test-audit    - Run ops-controller audit tests"
	@echo "  test-dashboard - Run dashboard health tests"
	@echo "  test-gateway  - Run model gateway contract tests"
	@echo "  smoke-test    - Run docker compose up -d and verify service health"
	@echo "  lint          - Ruff check (dashboard, tests, Python services)"

decrypt-secrets:
	@./scripts/secrets/decrypt.sh

up: decrypt-secrets
	$(COMPOSE) up -d

down: decrypt-secrets
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

rotate-internal-tokens:
	@./scripts/secrets/rotate-internal.sh

test:
	python -m pytest tests/ -v

lint:
	python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp worker

test-audit:
	python -m pytest tests/test_ops_controller_audit.py -v

test-dashboard:
	python -m pytest tests/test_dashboard_health.py -v

test-gateway:
	python -m pytest tests/test_model_gateway_contract.py -v

smoke-test:
	./scripts/smoke_test.sh
