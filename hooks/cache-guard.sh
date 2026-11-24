#!/bin/sh
# Fail-open POSIX wrapper for the cache-guard Antigravity hook.
# Emits completely empty stdout on no-op or error so PreToolUse never treats {} as deny.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="${PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export PLUGIN_ROOT

# Termux (Android) keeps its binaries outside the default PATH.
if [ -d /data/data/com.termux/files/usr/bin ]; then
  PATH="/data/data/com.termux/files/usr/bin:$PATH"
  export PATH
fi

# An explicit PLUGIN_DATA always wins; a set ANTIGRAVITY_APP_DATA_DIR derives it.
# When both are unset PLUGIN_DATA stays unset so guard.py resolves the surface from
# the hook payload's transcriptPath instead of a forced default.
if [ -z "${PLUGIN_DATA:-}" ] && [ -n "${ANTIGRAVITY_APP_DATA_DIR:-}" ]; then
  case "$ANTIGRAVITY_APP_DATA_DIR" in
    /*) PLUGIN_DATA="$ANTIGRAVITY_APP_DATA_DIR/plugin_data/cache-guard" ;;
    *) PLUGIN_DATA="$HOME/.gemini/$ANTIGRAVITY_APP_DATA_DIR/plugin_data/cache-guard" ;;
  esac
fi
if [ -n "${PLUGIN_DATA:-}" ]; then
  export PLUGIN_DATA
fi

PY_BIN="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY_BIN" ] && [ -x "/usr/bin/python3" ]; then
  PY_BIN="/usr/bin/python3"
fi
if [ -z "$PY_BIN" ] && [ -x "/data/data/com.termux/files/usr/bin/python3" ]; then
  PY_BIN="/data/data/com.termux/files/usr/bin/python3"
fi
if [ -z "$PY_BIN" ]; then
  exit 0
fi

if [ -n "${PLUGIN_DATA:-}" ]; then
  ERR_DIR="$PLUGIN_DATA"
  ERR_LOG="$PLUGIN_DATA/hook-errors.log"
else
  ERR_DIR="${TMPDIR:-/tmp}"
  ERR_LOG="$ERR_DIR/cache-guard-hook-errors.log"
fi
ERR_LOG_MAX_BYTES=131072
if ! mkdir -p "$ERR_DIR" 2>/dev/null || ! ( : >> "$ERR_LOG" ) 2>/dev/null; then
  ERR_LOG="/dev/null"
fi
if [ "$ERR_LOG" != "/dev/null" ] && [ -f "$ERR_LOG" ]; then
  ERR_BYTES=$(wc -c < "$ERR_LOG" 2>/dev/null | tr -dc '0-9')
  case "$ERR_BYTES" in
    '' | *[!0-9]*) ERR_BYTES=0 ;;
  esac
  if [ "$ERR_BYTES" -gt "$ERR_LOG_MAX_BYTES" ]; then
    : > "$ERR_LOG" 2>/dev/null || true
  fi
fi

OUT=$("$PY_BIN" "$PLUGIN_ROOT/hooks/guard.py" 2>>"$ERR_LOG")
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$OUT" ] || [ "$OUT" = "{}" ]; then
  exit 0
fi

printf '%s\n' "$OUT"
exit 0
