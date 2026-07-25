.DEFAULT_GOAL := help
SHELL := /bin/bash
include .env
export

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

db-up:  ## start Oracle 23ai Free
	docker compose up -d
	@echo "waiting for healthy container..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' rba-oracle)" = "healthy" ]; do sleep 5; printf "."; done
	@echo " ready on localhost:$(ORACLE_PORT)/$(ORACLE_SERVICE)"

db-down:  ## stop the container (keeps the volume)
	docker compose down

db-reset:  ## stop AND destroy the data volume — re-runs sql/00_setup on next up
	docker compose down -v

db-logs:  ## tail container logs
	docker compose logs -f oracle

db-shell:  ## sqlplus as the app user
	docker exec -it rba-oracle sqlplus $(ORACLE_USER)/$(ORACLE_PASSWORD)@//localhost:1521/$(ORACLE_SERVICE)

ddl:  ## create/refresh the star schema
	@echo "TODO: step 1 — run sql/10_ddl"

generate:  ## generate synthetic data to data/raw
	@echo "TODO: step 1 — python -m src.generator.main"

load:  ## load data/raw into Oracle
	@echo "TODO: step 1"

lint:  ## ruff check + format
	ruff check src tests || true
	ruff format --check src || true

test:  ## run unit tests
	pytest -q

.PHONY: help db-up db-down db-reset db-logs db-shell ddl generate load lint test
