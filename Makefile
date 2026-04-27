SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= dev
COMPOSE := ./scripts/compose-env.sh
# How to invoke `obscura-auth`. Override with `OBSCURA_AUTH=obscura-auth`
# if you've run `uv tool install` and the script is on your PATH.
OBSCURA_AUTH ?= uv run obscura-auth

.PHONY: help up down restart ps logs watch build pull check auth-fix clean \
	dev-up dev-down dev-restart dev-logs dev-watch dev-check dev-auth-fix \
	staging-up staging-down staging-restart staging-logs staging-check \
	prod-up prod-down prod-restart prod-logs prod-check \
	local-up local-down local-restart local-logs \
	dist brew-formula brew-install lint test

help:
	@echo "Obscura SDLC Commands"
	@echo "  make local-up | local-down | local-restart | local-logs   # keychain → shell → compose (no .env needed)"
	@echo "  make dev-up | dev-down | dev-restart | dev-logs | dev-watch | dev-check | dev-auth-fix"
	@echo "  make staging-up | staging-down | staging-restart | staging-logs | staging-check"
	@echo "  make prod-up | prod-down | prod-restart | prod-logs | prod-check"
	@echo "  make up ENV=<dev|staging|prod>"

up:
	$(COMPOSE) $(ENV) up -d --build

down:
	$(COMPOSE) $(ENV) down

restart:
	$(COMPOSE) $(ENV) down
	$(COMPOSE) $(ENV) up -d --build

ps:
	$(COMPOSE) $(ENV) ps

logs:
	$(COMPOSE) $(ENV) logs -f --tail=200

watch:
	$(COMPOSE) $(ENV) up --watch

build:
	$(COMPOSE) $(ENV) build

pull:
	$(COMPOSE) $(ENV) pull

check:
	OBSCURA_ENV=$(ENV) ./scripts/dev-auth-bootstrap-check.sh --start

auth-fix:
	OBSCURA_ENV=$(ENV) ./scripts/dev-auth-bootstrap-check.sh --fix

clean:
	$(COMPOSE) $(ENV) down -v

# Environment shortcuts

dev-up:
	$(MAKE) up ENV=dev

dev-down:
	$(MAKE) down ENV=dev

dev-restart:
	$(MAKE) restart ENV=dev

dev-logs:
	$(MAKE) logs ENV=dev

dev-watch:
	$(MAKE) watch ENV=dev

dev-check:
	$(MAKE) check ENV=dev

dev-auth-fix:
	$(MAKE) auth-fix ENV=dev

staging-up:
	$(MAKE) up ENV=staging

staging-down:
	$(MAKE) down ENV=staging

staging-restart:
	$(MAKE) restart ENV=staging

staging-logs:
	$(MAKE) logs ENV=staging

staging-check:
	$(MAKE) check ENV=staging

prod-up:
	$(MAKE) up ENV=prod

prod-down:
	$(MAKE) down ENV=prod

prod-restart:
	$(MAKE) restart ENV=prod

prod-logs:
	$(MAKE) logs ENV=prod

prod-check:
	$(MAKE) check ENV=prod

# =============================================================================
# Local Docker (Path 2: keychain → shell env → compose interpolation)
# =============================================================================
# These targets don't read a `.env` file — they pull every configured
# secret out of the OS keyring (and already-set env vars), eval the
# `export FOO=...` lines into the make shell, then hand off to
# `docker compose`. The compose file's `${VAR:-}` references pick them
# up. One command, no plaintext .env on disk, same flow as prod minus
# the secrets manager.

local-up:
	@eval "$$($(OBSCURA_AUTH) secrets export)" && docker compose up -d --build

local-down:
	docker compose down

local-restart:
	$(MAKE) local-down
	$(MAKE) local-up

local-logs:
	docker compose logs -f --tail=200

# =============================================================================
# Packaging & Release
# =============================================================================

VERSION := $(shell python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

# Build sdist + wheel
dist:
	@echo "Building obscura $(VERSION)..."
	rm -rf dist/
	uv build
	@echo "Artifacts in dist/"

# Create a tarball for Homebrew
brew-tarball: dist
	@echo "Creating brew tarball..."
	git archive --format=tar.gz --prefix=obscura-$(VERSION)/ -o dist/obscura-$(VERSION).tar.gz HEAD
	@echo "Tarball: dist/obscura-$(VERSION).tar.gz"
	@echo "SHA256: $$(shasum -a 256 dist/obscura-$(VERSION).tar.gz | cut -d' ' -f1)"

# Install from local formula (for testing)
brew-install: brew-tarball
	cp dist/obscura-$(VERSION).tar.gz "$$(brew --cache)/obscura-$(VERSION).tar.gz"
	brew install --formula Formula/obscura.rb

# Uninstall brew formula
brew-uninstall:
	brew uninstall obscura || true

# Install locally with uv (dev)
install-local:
	uv tool install -e .

# Quick checks
lint:
	ruff check .
	ruff format --check .

typecheck:
	pyright

test:
	pytest tests/ -v -m "not e2e"
