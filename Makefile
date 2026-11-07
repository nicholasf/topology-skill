# Install the topology hooks for each agent harness. See README.md,
# "Getting agents to use the topology without being asked".
#
#   make install-claude          # user scope (~/.claude, ~/.pi, ~/.config/goose)
#   make install-pi SCOPE=project

TOPOLOGY := PYTHONPATH=src python3 -m topology.cli
SCOPE ?= user
AGENTS := claude pi goose

.PHONY: help install uninstall status test harness-test \
	$(addprefix install-,$(AGENTS)) $(addprefix uninstall-,$(AGENTS))

help:
	@echo "make install-claude | install-pi | install-goose | install       (SCOPE=user|project)"
	@echo "make uninstall-claude | uninstall-pi | uninstall-goose | uninstall"
	@echo "make status | test | harness-test"

install: $(addprefix install-,$(AGENTS))

uninstall: $(addprefix uninstall-,$(AGENTS))

$(addprefix install-,$(AGENTS)): install-%:
	$(TOPOLOGY) hooks install --target $* --scope $(SCOPE)

$(addprefix uninstall-,$(AGENTS)): uninstall-%:
	$(TOPOLOGY) hooks uninstall --target $* --scope $(SCOPE)

status:
	$(TOPOLOGY) hooks status --scope $(SCOPE)

test:
	uv run --extra dev pytest -q

harness-test:
	python3 tests/harness/run_harness.py
