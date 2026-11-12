#!/bin/bash
set -uo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
out="${1:-}"
if [[ -z "$out" || $# -ne 1 ]]; then
  echo "usage: sudo diag-domain.sh <output file>" >&2
  exit 2
fi
if (( EUID != 0 )); then
  echo "run with sudo: sudo $0 $out" >&2
  exit 1
fi
state=/var/db/claude-team
anchor=com.apple/claude-team
addr=$(awk '/# claude-team[ \t\r]*$/ {print $1; exit}' /etc/hosts 2>/dev/null)
host=$(awk '/# claude-team[ \t\r]*$/ {print $2; exit}' /etc/hosts 2>/dev/null)
port=$(sed -n 's/^# claude-team port=\([0-9]*\) .*/\1/p' /etc/pf.anchors/claude-team 2>/dev/null | head -1)
[[ -n "$port" ]] || port="${TEAM_PORT:-7777}"
backend=$(sed -n 's/.*-> 127\.0\.0\.1 port \([0-9]*\).*/\1/p' /etc/pf.anchors/claude-team 2>/dev/null | head -1)
[[ "$port" =~ ^[0-9]{1,5}$ ]] || port=7777
[[ "$backend" =~ ^[0-9]{1,5}$ ]] || backend=""
[[ "$addr" =~ ^[0-9.]+$ ]] || addr=""
[[ "$host" =~ ^[a-z0-9.-]+$ ]] || host=""

section() {
  printf '\n===== %s\n' "$1"
  shift
  "$@" 2>&1
}

umask 077
if [[ -e "$out" || -L "$out" ]] || ! { set -C; exec 3> "$out"; } 2>/dev/null || [[ -L "$out" || ! -f "$out" ]]; then
  echo "cannot create $out as a new regular file; give a new file name" >&2
  exit 1
fi
set +C
{
  printf 'claude-team domain diagnostics, %s, %s\n' "$(date '+%F %T %Z')" "$(sw_vers -productVersion 2>/dev/null)"
  printf 'host=%s addr=%s port=%s backend=%s\n' "$host" "$addr" "$port" "$backend"
  section "pfctl -s info" pfctl -s info
  section "pfctl -s nat" pfctl -s nat
  section "pfctl -a $anchor -s nat" pfctl -a "$anchor" -s nat
  section "pfctl -s Anchors" pfctl -s Anchors
  section "pfctl -a com.apple -s Anchors" pfctl -a com.apple -s Anchors
  section "pfctl -s state (lines for our address and ports)" sh -c "pfctl -s state 2>&1 | grep -E '127\\.0\\.0\\.1[:.]($port|${backend:-none})|${addr:-none}[:.]80' | head -200"
  section "ifconfig lo0" ifconfig lo0
  section "/etc/pf.anchors/claude-team" cat /etc/pf.anchors/claude-team
  section "/etc/hosts (claude-team line)" grep -n 'claude-team' /etc/hosts
  section "grep anchor /etc/pf.conf" grep -n 'anchor' /etc/pf.conf
  section "$state" ls -la "$state"
  section "$state/pf.token" cat "$state/pf.token"
  section "$state/pf.prior" cat "$state/pf.prior"
  section "$state/alias" cat "$state/alias"
  section "$state/boot.log (last 50 lines)" tail -50 "$state/boot.log"
  section "listeners on $port and ${backend:-?}" sh -c "lsof -nP -iTCP:$port -sTCP:LISTEN; lsof -nP -iTCP:${backend:-0} -sTCP:LISTEN"
  section "connect 127.0.0.1:$port" nc -z -v -G 2 -w 2 127.0.0.1 "$port"
  section "connect 127.0.0.1:${backend:-?} (the redirect target: direct connections time out by design)" nc -z -v -G 2 -w 2 127.0.0.1 "${backend:-0}"
  section "connect ${addr:-?}:80" nc -z -v -G 2 -w 2 "${addr:-127.0.0.77}" 80
  section "curl -sI http://${host:-?}/" curl -s -I --max-time 3 "http://${host:-mission.control}/"
} >&3
exec 3>&-
chmod 644 "$out"
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then chown "$SUDO_UID:$SUDO_GID" "$out"; fi
echo "wrote $out"
