#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Git Bash on Windows needs a Windows-style path for the volume and no path rewriting.
if HOST_DIR="$(cd "$SCRIPT_DIR" && pwd -W 2>/dev/null)"; then
  export MSYS_NO_PATHCONV=1
else
  HOST_DIR="$SCRIPT_DIR"
fi

docker run --rm \
  --volume "$HOST_DIR:/work:ro" \
  --workdir /work \
  docker.io/library/python:3.13.15-slim-trixie \
  python analysis/check_dependencies.py
