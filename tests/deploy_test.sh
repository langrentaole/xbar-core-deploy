#!/bin/sh
# Exercise the real deployment shell with Docker and the migration boundary mocked.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
mkdir -p "$TMP/repo/tests" "$TMP/bin"
cp "$ROOT/deploy.sh" "$ROOT/.env.example" "$ROOT/docker-compose.yml" "$TMP/repo/"
REAL_PYTHON=$(command -v python3)
export REAL_PYTHON
cat > "$TMP/bin/docker" <<'MOCK'
#!/bin/sh
set -eu
printf 'docker %s\n' "$*" >> "$MOCK_LOG"
if [ "$1" = volume ]; then
  [ "$MOCK_SCHEMA" != orphaned-volume ] || echo test-project_mysql-data
  exit 0
fi
[ "$1" = compose ] || exit 90
shift
while [ $# -gt 0 ]; do
  case "$1" in --project-directory|--env-file|-f) shift 2 ;; *) break ;; esac
done
case "$1" in
  version) ;;
  config)
    if [ "${2:-}" = --format ]; then
      echo '{"name":"test-project","volumes":{"mysql-data":{"name":"test-project_mysql-data"}}}'
    fi ;;
  ps)
    case " $* " in
      *' -q '*)
        case "$MOCK_SCHEMA" in fresh|orphaned-volume) ;; *) echo test-db ;; esac ;;
    esac ;;
  exec)
    [ "$MOCK_SCHEMA" != unreadable ] || exit 1
    echo "$MOCK_SCHEMA" ;;
  pull|up) ;;
  *) exit 91 ;;
esac
MOCK
cat > "$TMP/bin/python3" <<'MOCK'
#!/bin/sh
set -eu
case "$1" in
  */billing_upgrade.py)
    printf 'helper %s\n' "$*" >> "$MOCK_LOG"
    # The old pin must survive until the coordinated helper takes over.
    grep -q '^XBAR_CORE_IMAGE=old-image$' "$XBAR_CORE_ENV_FILE" || exit 92
    exit "${MOCK_HELPER_EXIT:-0}"
    ;;
  *) exec "$REAL_PYTHON" "$@" ;;
esac
MOCK
chmod +x "$TMP/bin/docker" "$TMP/bin/python3"
PATH="$TMP/bin:$PATH"
export PATH
export XBAR_CORE_ENV_FILE="$TMP/repo/test.env"
export XBAR_CORE_UPGRADE_STATE_DIR="$TMP/checkpoint"
export MOCK_LOG="$TMP/commands"
fail() { echo "FAIL: $*" >&2; cat "$MOCK_LOG" >&2; exit 1; }
setup() {
  MOCK_SCHEMA=$1
  export MOCK_SCHEMA
  : > "$MOCK_LOG"
  sed -e 's/^XBAR_CORE_IMAGE=.*/XBAR_CORE_IMAGE=old-image/' \
      -e 's/replace-with-[^ ]*/0123456789abcdef0123456789abcdef/g' \
      -e 's/xbar.example.com/core.test/' -e 's/vibe.example.com/vibe.test/' \
      "$ROOT/.env.example" > "$XBAR_CORE_ENV_FILE"
  cp "$XBAR_CORE_ENV_FILE" "$TMP/before.env"
}
run() { sh "$TMP/repo/deploy.sh" "$1" > "$TMP/output" 2>&1; }
for action in update install up; do
  setup 127
  run "$action" || { cat "$TMP/output"; fail "127 $action failed"; }
  grep -q '^helper .*--apply' "$MOCK_LOG" || fail "127 $action bypassed coordinated helper"
  grep -q -- '--project test-project' "$MOCK_LOG" || fail 'resolved project not forwarded'
  grep -q -- '--image langrentaole/xbar-core@sha256:e1ef1e42d9ba833b19da543490f04eee18e1e4302507dd2701b3094c65ca1bab' "$MOCK_LOG" || fail 'wrong migration target'
  if grep -Eq '^docker .* (up|pull) ' "$MOCK_LOG"; then fail "127 $action ran ordinary deployment"; fi
  echo "PASS: schema 127 $action uses coordinated upgrade"
done
setup 127
MOCK_HELPER_EXIT=1
export MOCK_HELPER_EXIT
if run update; then fail 'helper failure ignored'; fi
cmp -s "$TMP/before.env" "$XBAR_CORE_ENV_FILE" || fail 'failed helper mutated env'
if grep -Eq '^docker .* (up|pull) ' "$MOCK_LOG"; then fail 'helper failure fell back to compose'; fi
unset MOCK_HELPER_EXIT
echo 'PASS: helper failure cannot fall through'
for schema in 128 126 unreadable orphaned-volume; do
  for action in update install up; do
    setup "$schema"
    if run "$action"; then fail "$schema $action was accepted"; fi
    cmp -s "$TMP/before.env" "$XBAR_CORE_ENV_FILE" || fail "$schema changed env before refusal"
    if grep -Eq '^helper |^docker .* (up|pull) ' "$MOCK_LOG"; then fail "$schema performed mutation"; fi
    if [ "$schema" = 128 ]; then
      grep -q -- '--resume' "$TMP/output" || fail '128 lacks recovery guidance'
    fi
  done
  echo "PASS: $schema refuses before mutation"
done
for schema in 129 130; do
for action in update install up; do
  setup "$schema"
  run "$action" || { cat "$TMP/output"; fail "129 routine $action failed"; }
  grep -q '^XBAR_CORE_IMAGE=langrentaole/xbar-core@sha256:' "$XBAR_CORE_ENV_FILE" || fail '129 pin not synced'
  if [ "$action" = update ]; then
    grep -q ' pull core-api core-edge$' "$MOCK_LOG" || fail '129 did not pull app images'
  fi
  grep -q ' up -d --wait --remove-orphans$' "$MOCK_LOG" || fail '129 routine startup missing'
  if grep -q '^helper ' "$MOCK_LOG"; then fail '129 incorrectly invokes migration helper'; fi
  echo "PASS: schema $schema routine $action"
done
done
for action in install up; do
  setup fresh
  run "$action" || { cat "$TMP/output"; fail "fresh $action failed"; }
  grep -q '^XBAR_CORE_IMAGE=langrentaole/xbar-core@sha256:' "$XBAR_CORE_ENV_FILE" || fail 'fresh pin not synced'
  grep -q ' up -d --wait --remove-orphans$' "$MOCK_LOG" || fail 'fresh startup missing'
  if grep -q '^helper ' "$MOCK_LOG"; then fail 'fresh install invoked migration helper'; fi
  echo "PASS: fresh $action"
done
