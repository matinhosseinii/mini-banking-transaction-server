#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STANDALONE_DIR=$(dirname -- "$SCRIPT_DIR")

docker compose \
  --env-file "$STANDALONE_DIR/.env" \
  -f "$STANDALONE_DIR/compose.yaml" \
  run --rm --no-deps api \
  python -m server.app.seed
