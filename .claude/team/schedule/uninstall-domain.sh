#!/bin/bash
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
if [[ "$(uname -s)" != Darwin ]]; then
  echo "uninstall-domain.sh supports macOS only" >&2
  exit 1
fi
label="com.claude-team.domain"
anchor="com.apple/claude-team"
if [[ -n "${TEAM_ETC:-}" ]]; then
  live=0
  etc="$TEAM_ETC"
  daemons="${TEAM_DAEMONS:-$TEAM_ETC/LaunchDaemons}"
  state="$TEAM_ETC/db/claude-team"
  pfctl="${TEAM_PFCTL:-}"
  ifconfig="${TEAM_IFCONFIG:-}"
else
  live=1
  etc=/etc
  daemons=/Library/LaunchDaemons
  state=/var/db/claude-team
  pfctl=/sbin/pfctl
  ifconfig=/sbin/ifconfig
  if (( EUID != 0 )); then
    echo "run with sudo: sudo $0" >&2
    exit 1
  fi
fi

pf() {
  if [[ -n "$pfctl" ]]; then "$pfctl" "$@"; else echo "would run: pfctl $*" >&2; fi
}

ifc() {
  if [[ -n "$ifconfig" ]]; then "$ifconfig" "$@"; else echo "would run: ifconfig $*" >&2; fi
}

hosts="$etc/hosts"
hops=0
while [[ -L "$hosts" ]]; do
  hops=$((hops + 1))
  if (( hops > 16 )); then
    echo "$etc/hosts: more than 16 symlink hops, giving up" >&2
    exit 1
  fi
  link=$(readlink "$hosts")
  if [[ "$link" == /* ]]; then hosts="$link"; else hosts="$(dirname "$hosts")/$link"; fi
done
if (( live )) && [[ "$(stat -f %u "$(dirname "$hosts")")" != 0 ]]; then
  echo "$hosts is in a directory not owned by root; refusing to rewrite it" >&2
  exit 1
fi

if (( live )); then
  launchctl bootout "system/$label" 2>/dev/null || true
else
  echo "would run: launchctl bootout system/$label"
fi
rm -f "$daemons/$label.plist"
if [[ -s "$state/pf.token" ]]; then
  pf -X "$(cat "$state/pf.token")" || true
fi
pf -a "$anchor" -F all || true
if [[ "$(cat "$state/pf.prior" 2>/dev/null || true)" == enabled && "$(pf -s info 2>/dev/null || true)" == *"Status: Disabled"* ]]; then
  pf -e || true
fi
rm -f "$etc/pf.anchors/claude-team"
if [[ -f "$state/alias" ]]; then
  ifc lo0 -alias "$(cat "$state/alias")" || true
fi

tmp=$(mktemp "$(dirname "$hosts")/.hosts.XXXXXX")
trap 'rm -f "$tmp"' EXIT
perl -ne 'print unless / \Q# claude-team\E[ \t\r]*$/' "$hosts" > "$tmp"
if [[ -f "$state/hosts.nonl" ]]; then
  perl -0777 -i -pe 's/\n\z//' "$tmp"
fi
chmod 644 "$tmp"
if (( live )); then chown root:wheel "$tmp"; fi
mv -f "$tmp" "$hosts"

if (( live )); then
  dscacheutil -flushcache
  killall -HUP mDNSResponder
fi
if [[ -f "$state/hosts.bak" ]]; then
  mv -f "$state/hosts.bak" "$etc/hosts.claude-team.bak"
fi
rm -rf "$state"
echo "removed the claude-team hosts line, pf anchor $anchor, our pf enable reference, our lo0 alias and $label; backup of the original hosts at $etc/hosts.claude-team.bak"
