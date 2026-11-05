#!/usr/bin/env bash
# Lumi's local checks: the same ones CI runs (.github/workflows/ci.yml), plus two that need the network.
#   lumi-tv/check.sh [--server] [--tv] [--quality] [--e2e]      (no flags: --server --tv)
#   --server   server/: ruff check, ruff format --check, pytest
#   --tv       tv/: Gradle unit tests and lintRelease (JDK 17: JAVA_HOME as set, else on macOS
#              /usr/libexec/java_home -v 17 or Homebrew's openjdk@17)
#   --quality  scripts/quality_check.py --fresh: scored picks against real TMDB (TMDB_TOKEN in server/.env)
#   --e2e      scripts/e2e_check.py: a throwaway server, the fake TV and an MCP client against real TMDB
# Every step runs even when an earlier one failed; the exit code is 1 if any did.
# LUMI_OFFLINE=1 passes --offline to Gradle (a cached toolchain; the first build must be online).
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
want_server=0 want_tv=0 want_quality=0 want_e2e=0
for a in "$@"; do
  case "$a" in
    --server) want_server=1;; --tv) want_tv=1;; --quality) want_quality=1;; --e2e) want_e2e=1;;
    *) echo "unknown: $a"; exit 2;;
  esac
done
[ $# -eq 0 ] && want_server=1 want_tv=1

failed=""
step() {  # step <name> <command...>: runs the command, remembers a failure, goes on
  local name="$1"; shift
  echo "== $name"
  if "$@"; then :; else failed="$failed, $name"; echo "$name FAILED"; fi
}
in_server() { (cd "$here/server" && "$@"); }
in_tv() { (cd "$here/tv" && "$@"); }

server_format() {
  in_server uv run ruff format --check . || { echo "fix: cd server && uv run ruff format ."; return 1; }
}
server_tests() {
  local out
  if out="$(in_server uv run pytest -q 2>&1)"; then echo "${out##*$'\n'}"; else echo "$out" | tail -25; return 1; fi
}
tv_tests() {
  if [ -z "${JAVA_HOME:-}" ] && [ "$(uname)" = Darwin ]; then
    local brew=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
    if home="$(/usr/libexec/java_home -v 17 2>/dev/null)"; then export JAVA_HOME="$home"
    elif [ -d "$brew" ]; then export JAVA_HOME="$brew"
    fi
  fi
  local offline=""
  [ "${LUMI_OFFLINE:-0}" = 1 ] && offline="--offline"
  in_tv ./gradlew -q $offline testDebugUnitTest lintRelease
}

if [ $want_server = 1 ]; then
  step "server lint" in_server uv run ruff check .
  step "server format" server_format
  step "server tests" server_tests
fi
if [ $want_tv = 1 ]; then
  step "TV unit tests and lint" tv_tests
fi
if [ $want_quality = 1 ]; then
  step "quality check (real TMDB)" in_server uv run python scripts/quality_check.py --fresh
fi
if [ $want_e2e = 1 ]; then
  step "end-to-end check (real TMDB, throwaway server)" in_server uv run python scripts/e2e_check.py
fi
if [ -n "$failed" ]; then echo "FAILED:${failed#,}"; exit 1; fi
echo "all checks passed"
