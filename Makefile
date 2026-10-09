.PHONY: run install check seed test lint clean

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# One-command install + seed + start (idempotent -- safe to re-run).
# Runs with the embedded scheduler ON (weekly report, daily alert checks,
# dead-man's switch) -- the real production behavior. Set DISABLE_SCHEDULER=1
# yourself if you don't want that for a quick local look.
# The config check runs first: missing optional keys (demo mode) are only
# warnings; genuinely broken settings stop here with a readable report.
run: install check seed
	$(VENV)/bin/uvicorn dashboard.main:app --app-dir src --port 8000

install: $(VENV)/.installed .env

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt
	touch $(VENV)/.installed

.env:
	cp .env.example .env

# Validate .env/environment without starting the server (exit 1 on errors).
# SKIP_CONFIG_CHECK=1 bypasses it (here and at server startup).
check: install
	$(PY) scripts/check_config.py

seed: install
	$(PY) scripts/seed_demo_data.py

test: $(VENV)/.dev-installed
	$(PY) -m pytest -q

lint: $(VENV)/.dev-installed
	$(VENV)/bin/ruff check .

$(VENV)/.dev-installed: install requirements-dev.txt
	$(PIP) install -q -r requirements-dev.txt
	touch $(VENV)/.dev-installed

clean:
	rm -rf $(VENV) data/*.db data/*.db-wal data/*.db-shm data/evidence/ data/exports/
