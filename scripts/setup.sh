#!/usr/bin/env bash
# Reproducible setup: pinned AgentDojo checkout + venv with pinned requirements.
set -euo pipefail
cd "$(dirname "$0")/.."
AGENTDOJO_COMMIT=089ed468cf3ed0322acc66b0211f26d9d90dbf60
PY=${PYTHON:-python3}   # any Python >= 3.10; we used Homebrew python 3.14.7
if [ ! -d third_party/agentdojo ]; then
  git clone https://github.com/ethz-spylab/agentdojo third_party/agentdojo
fi
git -C third_party/agentdojo checkout -q "$AGENTDOJO_COMMIT"
"$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q --no-deps -e third_party/agentdojo
.venv/bin/python -m pytest -q tests
