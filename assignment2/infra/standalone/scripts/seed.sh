#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STANDALONE_DIR=$(dirname -- "$SCRIPT_DIR")

for service in bank-a1 bank-b1; do
  docker compose \
    --env-file "$STANDALONE_DIR/.env" \
    -f "$STANDALONE_DIR/compose.yaml" \
    run --rm --no-deps "$service" \
    python -m server.app.seed
done
