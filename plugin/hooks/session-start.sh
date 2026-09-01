#!/usr/bin/env bash
# SessionStart hook: surface blocked automated workers and decision-register
# bets that are past their review date. Blocked workers come first — someone
# is stopped right now, whereas an overdue review is merely late.
#
# Design constraints, in priority order:
#   1. Never break or delay a session. Always exit 0. Hard timeout.
#   2. Silent when there is nothing to say. A hook that speaks every session
#      trains the user to ignore it, and then it is worth less than nothing.
#   3. Silent when not configured. Most repos are not register repos.
#
# It reads. It never writes a record.

set -uo pipefail

# Not a register repo — nothing to do.
if [[ -z "${DR_PROJECT:-}" ]]; then
  exit 0
fi

# Locate the CLI. Prefer one on PATH; fall back to the project checkout if the
# operator points TRIAD_DR_HOME at it. If neither resolves, stay quiet — an
# unconfigured hook should be invisible, not noisy.
DR_CMD=()
if command -v triad-dr >/dev/null 2>&1; then
  DR_CMD=(triad-dr)
elif [[ -n "${TRIAD_DR_HOME:-}" && -f "${TRIAD_DR_HOME}/pyproject.toml" ]] && command -v uv >/dev/null 2>&1; then
  DR_CMD=(uv run --project "${TRIAD_DR_HOME}" triad-dr)
else
  exit 0
fi

# Hard wall-clock cap. Expired SSO, a cold DNS lookup, a captive portal or a VPN
# blackhole must not hold up a session start. GNU `timeout` is absent on a stock
# macOS, so fall back to a portable backgrounded-kill instead of running unbounded.
TIMEOUT_SECS=8
_TMP="$(mktemp 2>/dev/null)" || exit 0
trap 'rm -f "${_TMP}"' EXIT

if command -v timeout >/dev/null 2>&1; then
  timeout "${TIMEOUT_SECS}" "${DR_CMD[@]}" inbox --quiet >"${_TMP}" 2>/dev/null || true
elif command -v gtimeout >/dev/null 2>&1; then
  gtimeout "${TIMEOUT_SECS}" "${DR_CMD[@]}" inbox --quiet >"${_TMP}" 2>/dev/null || true
else
  "${DR_CMD[@]}" inbox --quiet >"${_TMP}" 2>/dev/null &
  _pid=$!
  ( sleep "${TIMEOUT_SECS}"; kill -TERM "${_pid}" 2>/dev/null ) >/dev/null 2>&1 &
  _watchdog=$!
  # Drop the watchdog from the job table: killing a tracked job makes bash print
  # "Terminated" to stderr, and a hook must not emit anything it did not mean to.
  disown "${_watchdog}" 2>/dev/null || true
  wait "${_pid}" 2>/dev/null || true
  kill -TERM "${_watchdog}" 2>/dev/null || true
fi

OUT="$(cat "${_TMP}" 2>/dev/null)"

# Nothing blocked and nothing overdue is the healthy case and prints nothing.
if [[ -z "${OUT//[[:space:]]/}" ]]; then
  exit 0
fi

cat <<EOF
<decision-register-status>
${OUT}

Blocked workers are the more urgent item — someone is stopped right now; clear
them with /triad-dr:answer. Bets past review are merely late; use
/triad-dr:review for those. Mention this to the user once, briefly, then
continue with whatever they actually asked for. Do not act on it unprompted,
and do not repeat this later in the session.
</decision-register-status>
EOF

exit 0
