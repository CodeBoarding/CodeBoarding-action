#!/usr/bin/env bash
# Forwards local review-preview arguments to the canonical test harness.
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/../tests/run_local.sh" "$@"
