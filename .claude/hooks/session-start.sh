#!/bin/bash
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

echo '{"async": true, "asyncTimeout": 300000}'

# Install project Python dependencies
pip install -q -r "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/requirements.txt"

# Pre-warm uvx cache for ha-mcp so it starts quickly
uvx --python 3.13 ha-mcp@latest --help >/dev/null 2>&1 || true
