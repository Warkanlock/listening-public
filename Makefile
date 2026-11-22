# Listening Room: the one command surface (plan 035).
#
# Everything from a checkout goes through these targets, and every
# Python invocation goes through Poetry, so the interpreter is the
# project's pinned one (pyproject.toml), never whatever python3
# happens to be first on PATH. The app is Python standard library
# only; Poetry manages the interpreter and the virtualenv, and the
# dependency list is empty and stays empty.
#
#   make install           set the room up (Poetry, checks)
#   make start             start the room (./run.sh, Poetry interpreter)
#   make dev               dev server in the foreground, auto-restart
#   make build             build the React frontend into frontend/dist/
#   make dev-ui            Vite dev server for the React frontend
#   make test              the unittest suite (the parked test wave)
#   make verify-ui         the rendered gate: drive the built interface
#                          in a headless browser against a fixture room
#
# Extra arguments pass through where the underlying piece accepts
# them: make dev ARGS="--port 8899", make start ARGS="--port 8899".

.PHONY: help install start dev build dev-ui test verify-ui

help:
	@echo 'Listening Room: make targets'
	@echo ''
	@echo '  make install            Set the room up: Poetry install (creates the'
	@echo '                          project venv), then the preflight inside the'
	@echo '                          Poetry environment.'
	@echo '  make start              Start the room through ./run.sh, running'
	@echo '                          under the Poetry interpreter when the venv'
	@echo '                          exists. Extra args: ARGS="--port 8899".'
	@echo '  make dev                Run the dev server in the foreground; it'
	@echo '                          restarts when a backend Python file changes.'
	@echo '                          Extra args: ARGS="--port 8899".'
	@echo '  make build              Build the React frontend (frontend/react)'
	@echo '                          into frontend/dist/ with Vite.'
	@echo '  make dev-ui             Run the Vite dev server for the React'
	@echo '                          frontend; /api proxies to 127.0.0.1:8765.'
	@echo '  make test               Run the unittest suite under Poetry.'
	@echo '  make verify-ui          Run the rendered gate (plan 050): boot a'
	@echo '                          fixture room and drive the built interface'
	@echo '                          in a headless browser; screenshots and the'
	@echo '                          report land in verification/ui/. Needs an'
	@echo '                          interpreter with Playwright available:'
	@echo '                          VERIFY_UI_PYTHON=/path/to/python make verify-ui'
	@echo '  make help               This list.'

install:
	@command -v poetry >/dev/null 2>&1 || { \
		echo 'Poetry is not installed, and this Makefile cannot install it.'; \
		echo 'Install it one of these ways, then run make install again:'; \
		echo '  macOS:  brew install poetry'; \
		echo '  other:  the official installer at https://install.python-poetry.org'; \
		echo 'See README.md.'; \
		exit 1; \
	}
	poetry install
	poetry run python scripts/preflight.py
	@command -v yarn >/dev/null 2>&1 || { \
		echo 'Yarn is not installed, and this Makefile cannot install it.'; \
		echo 'Install it one of these ways, then run make install again:'; \
		echo '  macOS:  brew install yarn'; \
		echo '  other:  npm install --global yarn'; \
		echo 'See README.md.'; \
		exit 1; \
	}
	yarn --cwd frontend/react install --frozen-lockfile
	$(MAKE) build
	./listening install-command

build:
	yarn --cwd frontend/react build

start:
	@if [ ! -f frontend/dist/index.html ] || [ ! -f frontend/dist/about/index.html ] || \
	    [ frontend/react/yarn.lock -nt frontend/dist/index.html ]; then \
		echo 'Frontend build is missing or stale; building it first.'; \
		$(MAKE) build; \
	fi
	./run.sh $(ARGS)

dev:
	poetry run python scripts/dev.py $(ARGS)

dev-ui:
	yarn --cwd frontend/react dev

test:
	poetry run python scripts/audit_tests.py

# The rendered gate (plan 050, step 1b): scripts/verify_ui.py boots a
# fixture room and drives the built interface in headless Chromium.
# It needs Playwright importable by the interpreter that runs it,
# which the Poetry environment does not carry (the app is stdlib
# only; the browser is test tooling of the environment, like ffmpeg).
# VERIFY_UI_PYTHON names that interpreter; the default is python3,
# and the script itself reports the gap and exits non-zero when no
# browser tooling is available. It never passes silently.
VERIFY_UI_PYTHON ?= python3

verify-ui:
	$(VERIFY_UI_PYTHON) scripts/audit_tests.py --ui

# Server targets (plan 041): put the room on the always-on
# Debian/Ubuntu server with one command, then manage the systemd
# user service it installs. `make server` does the whole setup
# (scripts/server_setup.sh, which carries the platform gate and
# refuses politely anywhere else); the rest are thin systemctl and
# journalctl wrappers, plus the health check on server-status.

.PHONY: server server-status server-logs server-restart server-stop

server:
	./scripts/server_setup.sh

server-status:
	systemctl --user status listening-room.service --no-pager || true
	@code=$$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8765/ 2>/dev/null || true); \
	if [ "$$code" = "200" ] || [ "$$code" = "401" ]; then \
		echo "Health check: the room is answering on http://127.0.0.1:8765/ (HTTP $$code)."; \
	else \
		echo "Health check: the room is not answering on http://127.0.0.1:8765/ (HTTP $${code:-none})."; \
	fi

server-logs:
	journalctl --user -u listening-room.service -f

server-restart:
	systemctl --user restart listening-room.service

server-stop:
	systemctl --user stop listening-room.service
