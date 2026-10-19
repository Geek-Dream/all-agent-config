.PHONY: test check compile smoke

PYTHON ?= .venv/bin/python

test:
	$(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall -q app tests

smoke:
	$(PYTHON) -c 'from app import registry; agents = registry.load_agents(); print(f"registered agents: {len(agents)}")'

check: test compile smoke
	git diff --check
