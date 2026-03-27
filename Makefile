# Ordo AI Stack Makefile
.PHONY: test test-audit test-dashboard test-gateway smoke-test lint help

help:
	@echo "Targets:"
	@echo "  test          - Run all tests"
	@echo "  test-audit    - Run ops-controller audit tests"
	@echo "  test-dashboard - Run dashboard health tests"
	@echo "  test-gateway  - Run model gateway contract tests"
	@echo "  smoke-test    - Run docker compose up -d and verify service health"
	@echo "  lint          - Ruff check (dashboard, tests, Python services)"

test:
	python -m pytest tests/ -v

lint:
	python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts

test-audit:
	python -m pytest tests/test_ops_controller_audit.py -v

test-dashboard:
	python -m pytest tests/test_dashboard_health.py -v

test-gateway:
	python -m pytest tests/test_model_gateway_contract.py -v

smoke-test:
	./scripts/smoke_test.sh
