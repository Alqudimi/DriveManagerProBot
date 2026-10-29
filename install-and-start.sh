#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$ROOT/install-colab.sh"
bash "$ROOT/healthcheck.sh"
exec bash "$ROOT/run.sh"
