#!/usr/bin/env bash
# Reimporte le workflow n8n canonique depuis le workflow.json du dépôt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKFLOW_JSON="$REPO_ROOT/workflow.json" \
  python3 "$SCRIPT_DIR/import_workflow.py"
