SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV ?= dev
COMPOSE := ./scripts/compose-env.sh

.PHONY: help up down restart ps logs watch build pull check auth-fix clean \
	dev-up dev-down dev-restart dev-logs dev-watch dev-check dev-auth-fix \
	staging-up staging-down staging-restart staging-logs staging-check \
	prod-up prod-down prod-restart prod-logs prod-check

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
