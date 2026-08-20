VENV_BIN := $(if $(wildcard .venv/bin/python),.venv/bin/,)

PYTHON ?= $(VENV_BIN)python
RUFF ?= $(VENV_BIN)ruff
MYPY ?= $(VENV_BIN)mypy

CHECK_PATHS := src tests tools

.PHONY: check
check:
	$(RUFF) check $(CHECK_PATHS)
	$(RUFF) format --check $(CHECK_PATHS)
	$(MYPY)
	$(PYTHON) -m unittest discover -s tests
