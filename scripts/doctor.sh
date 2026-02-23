#!/usr/bin/env bash
set -euo pipefail

echo "[doctor] python: $(python3 --version)"
echo "[doctor] mailhub: $(python3 -c 'import mailhub; print(mailhub.__version__)' 2>/dev/null || echo 'not installed')"

: "${MAILHUB_STATE_DIR:=${HOME}/.openclaw/state/mailhub}"
echo "[doctor] MAILHUB_STATE_DIR=${MAILHUB_STATE_DIR}"
mkdir -p "${MAILHUB_STATE_DIR}"

echo "[doctor] running mailhub doctor"
mailhub doctor || true