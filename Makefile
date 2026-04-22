SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= dev
COMPOSE := ./scripts/compose-env.sh

.PHONY: help up down restart ps logs watch build pull check auth-fix clean \
	dev-up dev-down dev-restart dev-logs dev-watch dev-check dev-auth-fix \
	staging-up staging-down staging-restart staging-logs staging-check \
	prod-up prod-down prod-restart prod-logs prod-check \
	dist brew-formula brew-install lint test \
	ext-install ext-reload ext-logs ext-status ext-id

help:
	@echo "Obscura SDLC Commands"
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

# --- Browser extension ---------------------------------------------------

EXT_DIR := packages/browser-extension
EXT_ID_FILE := $(EXT_DIR)/.keys/EXTENSION_ID

ext-id:
	@cat $(EXT_ID_FILE)

ext-install:
	@echo "→ installing native-messaging host for $(shell cat $(EXT_ID_FILE))"
	@cd $(EXT_DIR)/native-host && ./install.sh
	@echo ""
	@echo "→ load the unpacked extension once if you haven't:"
	@echo "    open -a 'Google Chrome' 'chrome://extensions'"
	@echo "    toggle Developer mode → Load unpacked → $(CURDIR)/$(EXT_DIR)"

ext-reload:
	@pkill -f obscura_native_host.py 2>/dev/null || true
	@echo "killed running native host; reload the Obscura card on chrome://extensions"

ext-logs:
	@tail -f $${OBSCURA_HOME:-$$HOME/.obscura}/logs/browser-extension-host.log

ext-status:
	@echo "extension id      : $$(cat $(EXT_ID_FILE))"
	@echo "manifest installed: $$(ls -1 \
		$$HOME/Library/Application\ Support/Google/Chrome/NativeMessagingHosts/com.obscura.host.json \
		$$HOME/.config/google-chrome/NativeMessagingHosts/com.obscura.host.json \
		2>/dev/null | head -1 || echo 'NOT INSTALLED — run make ext-install')"
	@echo "host process(es)  :"
	@ps -ax -o pid,command | grep obscura_native_host.py | grep -v grep || echo "  (none running)"
