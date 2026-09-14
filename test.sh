#!/usr/bin/env bash
# End-to-end check of ASSIGNMENT.md: the reviewers' routine plus every CRM and assistant requirement.
# WARNING: like ./reset.sh, this removes this project's data. It stops the app when it finishes.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
LOG="$(mktemp)"
DEV_PID=""

compose() { docker compose --project-directory "$SCRIPT_DIR" --file "$SCRIPT_DIR/compose.yml" "$@"; }

# Checks run inside the app container, so the host needs only a shell and Docker.
check() {
  echo "== $1"
  compose exec -T app python - "$1" < tests/check_requirements.py
}

start() {
  echo "== ./dev.sh (log: $LOG)"
  ./dev.sh >"$LOG" 2>&1 &
  DEV_PID=$!
  for _ in $(seq 150); do
    if compose exec -T app python -c "import urllib.request as r; r.urlopen('http://localhost:3000/')" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  echo "The app did not answer within 5 minutes." >&2
  exit 1
}

stopped() { wait "$DEV_PID" || true; } # dev.sh exits once its containers are stopped

trap 'echo "FAILED. Last lines of the dev.sh log:" >&2; tail -n 30 "$LOG" >&2' ERR

echo "== files the assignment says to leave alone"
if git rev-parse --git-dir >/dev/null 2>&1; then
  first_commit="$(git rev-list --max-parents=0 HEAD)"
  git diff --exit-code --stat "$first_commit" -- ASSIGNMENT.md verify.sh data docs/environment.md docs/image-policy.md
  echo "  ok  unchanged since the starter commit"
fi

echo "== pinned versions"
if grep -nE '^FROM |--from=|image: ' Dockerfile compose.yml | grep -vE ':[0-9]+\.[0-9]+'; then
  echo "The images above are not pinned to a version (at least major.minor, never latest)." >&2
  exit 1
fi
grep -q 'uv sync --locked' Dockerfile && test -f uv.lock
echo "  ok  images pinned to exact versions, dependencies installed from the committed uv.lock"

./reset.sh
start
./verify.sh
check data  # archive → importer → Postgres
check web   # page → request → backend → database → page
check agent # Toolbox → Preparer → Checker → Coordinator → saved run
check scale # indexes

echo "== docker compose down, then ./dev.sh again"
before="$(compose exec -T app python - fingerprint < tests/check_requirements.py)"
compose down
stopped
start
grep -q "import: already done, keeping existing data" "$LOG"
echo "  ok  [importer] the second start logged 'import: already done, keeping existing data'"
echo "== kept"
compose exec -T -e BEFORE="$before" app python - kept < tests/check_requirements.py

echo "== ./reset.sh, then ./dev.sh again"
./reset.sh
stopped
start
grep -q "import: activities" "$LOG"
echo "  ok  [importer] the start after a reset imported the archive again"
check data

compose down
stopped
echo "All checks passed. The data is back to a fresh import; start the app with ./dev.sh."
