#!/bin/bash
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
if [[ "$(uname -s)" != Darwin ]]; then
  echo "install-domain.sh supports macOS only (pf, launchd); open http://127.0.0.1:${TEAM_PORT:-7777} instead" >&2
  exit 1
fi
host="${TEAM_HOST:-mission.control}"
port="${TEAM_PORT:-7777}"
backend="${TEAM_BACKEND_PORT:-}"
mark="# claude-team"
label="com.claude-team.domain"
anchor="com.apple/claude-team"
if ! [[ "$host" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
  echo "TEAM_HOST must be a lowercase dotted name like mission.control, got: $host" >&2
  exit 1
fi
if ! [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port < 1024 || 10#$port > 65535 )); then
  echo "TEAM_PORT must be 1024-65535, got: $port" >&2
  exit 1
fi
[[ -n "$backend" ]] || backend=$((10#$port + 1))
if ! [[ "$backend" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$backend < 1024 || 10#$backend > 65535 || 10#$backend == 10#$port )); then
  echo "TEAM_BACKEND_PORT must be 1024-65535 and differ from TEAM_PORT, got: $backend" >&2
  exit 1
fi
case "$host" in
  *.localhost) addr=127.0.0.1 ;;
  *) addr="${TEAM_ADDR:-127.0.0.77}" ;;
esac
octet='(0|[1-9][0-9]{0,2})'
if ! [[ "$addr" =~ ^$octet\.$octet\.$octet\.$octet$ ]] || (( BASH_REMATCH[1] != 127 || BASH_REMATCH[2] > 255 || BASH_REMATCH[3] > 255 )) \
    || (( BASH_REMATCH[4] == 0 || BASH_REMATCH[4] >= 255 )); then
  echo "TEAM_ADDR must be a 127.x.y.z loopback address (octets 0-255 without leading zeros, last octet 1-254), got: $addr" >&2
  exit 1
fi
if [[ "$addr" == 127.0.0.1 && "$host" != *.localhost ]]; then
  echo "TEAM_ADDR 127.0.0.1 is only used for *.localhost names; it would take port 80 of 127.0.0.1 from your other servers" >&2
  exit 1
fi
if [[ -n "${TEAM_ETC:-}" ]]; then
  live=0
  etc="$TEAM_ETC"
  daemons="${TEAM_DAEMONS:-$TEAM_ETC/LaunchDaemons}"
  state="$TEAM_ETC/db/claude-team"
  pfctl="${TEAM_PFCTL:-}"
  ifconfig="${TEAM_IFCONFIG:-}"
  lsof="${TEAM_LSOF:-}"
  nc="${TEAM_NC:-}"
  curl="${TEAM_CURL:-}"
else
  live=1
  etc=/etc
  daemons=/Library/LaunchDaemons
  state=/var/db/claude-team
  pfctl=/sbin/pfctl
  ifconfig=/sbin/ifconfig
  lsof=/usr/sbin/lsof
  nc=/usr/bin/nc
  curl=/usr/bin/curl
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

xml() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

pids_on() {
  if [[ -n "$lsof" ]]; then "$lsof" -t -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u || true; fi
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

here="$(cd "$(dirname "$0")" && pwd)"
on_port=$(pids_on "$port")
on_backend=$(pids_on "$backend")
listening=0
[[ -n "$on_port" ]] && listening=1
for pid in $on_backend; do
  if ! grep -qx "$pid" <<< "$on_port"; then
    echo "127.0.0.1:$backend is already in use by another program, and the redirect would make it unreachable for direct connections:" >&2
    "$lsof" -nP -iTCP:"$backend" -sTCP:LISTEN >&2 || true
    echo "pick a free port for the redirect: sudo TEAM_BACKEND_PORT=<port> $0" >&2
    exit 1
  fi
done
prev=$(awk '/ # claude-team[ \t\r]*$/ {print $2; exit}' "$hosts" 2>/dev/null || true)

mkdir -p "$state"
chmod 755 "$state"
[[ -e "$state/hosts.bak" ]] || cp -p "$hosts" "$state/hosts.bak"

undo() {
  local ours left=""
  ours=$(cat "$state/alias" 2>/dev/null || true)
  echo "FAIL: $1" >&2
  echo "undoing the whole install with $here/uninstall-domain.sh:" >&2
  if ! /bin/bash "$here/uninstall-domain.sh" >&2; then
    echo "FAIL: uninstall-domain.sh failed; run it again: sudo $here/uninstall-domain.sh" >&2
    exit 1
  fi
  [[ -e "$daemons/$label.plist" ]] && left="$left $daemons/$label.plist"
  [[ -e "$etc/pf.anchors/claude-team" ]] && left="$left $etc/pf.anchors/claude-team"
  [[ -e "$state" ]] && left="$left $state"
  grep -q ' # claude-team[[:space:]]*$' "$hosts" && left="$left the-marked-line-in-$hosts"
  [[ -n "$(pf -a "$anchor" -s nat 2>/dev/null || true)" ]] && left="$left rules-in-pf-anchor-$anchor"
  if [[ -n "$ours" && "$(ifc lo0 || true)" == *"inet $ours "* ]]; then left="$left lo0-alias-$ours"; fi
  if [[ -n "$left" ]]; then
    echo "FAIL: still present after the undo:$left" >&2
  else
    echo "undone: no LaunchDaemon, anchor file, pf rules, pf enable reference, alias we added, hosts line or $state is left" >&2
  fi
  if [[ -n "$prev" ]]; then
    echo "The previous install (http://$prev) is removed as well: the local domain is now uninstalled." >&2
  fi
  echo "The dashboard stays at http://127.0.0.1:$port. Details for a bug report: sudo $here/diag-domain.sh /tmp/claude-team-diag.txt" >&2
  exit 1
}

tmp=""
trap 'if [[ -n "$tmp" ]]; then rm -f "$tmp"; fi' EXIT

write_hosts() {
  tmp=$(mktemp "$(dirname "$hosts")/.hosts.XXXXXX")
  perl -ne 'print unless / \Q# claude-team\E[ \t\r]*$/' "$hosts" > "$tmp"
  if [[ -s "$tmp" && -n "$(tail -c 1 "$tmp")" ]]; then
    printf '\n' >> "$tmp"
    touch "$state/hosts.nonl"
  fi
  printf '%s %s %s\n' "$addr" "$host" "$mark" >> "$tmp"
  chmod 644 "$tmp"
  if (( live )); then chown root:wheel "$tmp"; fi
  mv -f "$tmp" "$hosts"
  if (( live )); then
    dscacheutil -flushcache
    killall -HUP mDNSResponder
  fi
}

old=""
[[ -f "$state/alias" ]] && old=$(cat "$state/alias")
if [[ -n "$old" && "$old" != "$addr" ]]; then
  ifc lo0 -alias "$old" || true
  rm -f "$state/alias"
fi
lo=$(ifc lo0 || true)
if [[ "$addr" != 127.0.0.1 && ! -f "$state/alias" && "$lo" != *"inet $addr "* ]]; then
  ifc lo0 alias "$addr" up
  echo "$addr" > "$state/alias"
fi

mkdir -p "$etc/pf.anchors"
printf '# claude-team port=%s backend=%s\nrdr pass on lo0 inet proto tcp from any to %s port 80 -> 127.0.0.1 port %s\n' \
  "$port" "$backend" "$addr" "$backend" > "$etc/pf.anchors/claude-team"
chmod 644 "$etc/pf.anchors/claude-team"

if [[ ! -f "$state/pf.prior" ]]; then
  if [[ "$(pf -s info 2>/dev/null || true)" == *"Status: Enabled"* ]]; then echo enabled; else echo disabled; fi > "$state/pf.prior"
fi

boot="$state/boot.sh"
{
  echo '#!/bin/bash'
  echo 'PATH=/usr/bin:/bin:/usr/sbin:/sbin'
  printf 'state=%q\nanchorfile=%q\npfctl=%q\nifconfig=%q\n' "$state" "$etc/pf.anchors/claude-team" "${pfctl:-/sbin/pfctl}" "${ifconfig:-/sbin/ifconfig}"
  if [[ -f "$state/alias" ]]; then
    printf "\"\$ifconfig\" lo0 alias %q up\n" "$addr"
  fi
  printf "\"\$pfctl\" -a %q -f \"\$anchorfile\" || exit 1\n" "$anchor"
  cat <<'BOOT'
out=$("$pfctl" -E 2>&1) || { echo "$out" >&2; exit 1; }
new=$(printf '%s\n' "$out" | sed -n 's/^Token[[:space:]]*:[[:space:]]*//p')
if [ -z "$new" ]; then echo "pfctl -E printed no token: $out" >&2; exit 1; fi
old=$(cat "$state/pf.token" 2>/dev/null || true)
printf '%s\n' "$new" > "$state/pf.token.tmp" && mv -f "$state/pf.token.tmp" "$state/pf.token"
if [ -n "$old" ] && [ "$old" != "$new" ]; then "$pfctl" -X "$old" >/dev/null 2>&1 || true; fi
exit 0
BOOT
} > "$boot"
chmod 700 "$boot"

plist="$daemons/$label.plist"
mkdir -p "$daemons"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$(xml "$boot")</string></array>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>$(xml "$state/boot.log")</string>
<key>StandardErrorPath</key><string>$(xml "$state/boot.log")</string>
</dict></plist>
PLIST
chmod 644 "$plist"
plutil -lint "$plist" >/dev/null

before=$(cat "$state/pf.token" 2>/dev/null || true)
if (( live )); then
  chown -R root:wheel "$state" "$plist"
  launchctl bootout "system/$label" 2>/dev/null || true
  launchctl bootstrap system "$plist"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ "$(cat "$state/pf.token" 2>/dev/null || true)" != "$before" ]] && break
    sleep 1
  done
else
  echo "would run: launchctl bootstrap system $plist (RunAtLoad runs $boot)"
  if [[ -n "$pfctl" && -n "$ifconfig" ]] && ! /bin/bash "$boot"; then
    echo "boot.sh failed (the previous pf token was kept)" >&2
  fi
fi

echo "host $host, address $addr, port 80 -> 127.0.0.1:$backend (the dashboard's backend listener, reachable only through the redirect; direct use stays on 127.0.0.1:$port)"
if (( ! live )) && [[ -z "$pfctl" || -z "$ifconfig" ]]; then
  write_hosts
  echo "dry run without TEAM_PFCTL and TEAM_IFCONFIG: boot.sh not run, pf checks skipped"
  exit 0
fi
ok=1
if [[ "$(pf -a "$anchor" -s nat 2>/dev/null || true)" =~ to\ ${addr//./\\.}\ port\ (=\ )?80\ -\>\ 127\.0\.0\.1\ port\ (=\ )?$backend ]]; then
  echo "ok: pf anchor $anchor redirects $addr:80 to 127.0.0.1:$backend"
else
  echo "FAIL: pf anchor $anchor has no rdr rule for $addr:80 -> 127.0.0.1:$backend (see $state/boot.log)" >&2
  ok=0
fi
if [[ "$(pf -s info 2>/dev/null || true)" == *"Status: Enabled"* ]]; then
  echo "ok: pf is enabled"
else
  echo "FAIL: pf is not enabled (see $state/boot.log)" >&2
  ok=0
fi
if grep -Eq '^[[:space:]]*rdr-anchor[[:space:]]+"com\.apple/\*"' "$etc/pf.conf" 2>/dev/null; then
  echo "ok: $etc/pf.conf evaluates rdr-anchor \"com.apple/*\""
else
  echo "WARNING: $etc/pf.conf has no rdr-anchor \"com.apple/*\" line, so the redirect is never evaluated; add it or load the anchor from your own pf.conf" >&2
fi
if (( ! ok )); then
  undo "the redirect is not in place"
fi
write_hosts
if [[ -z "$lsof" || -z "$nc" || -z "$curl" ]]; then
  echo "dry run without TEAM_LSOF, TEAM_NC and TEAM_CURL: connection checks skipped"
  exit 0
fi
if (( ! listening )); then
  echo "WARNING: nothing listened on 127.0.0.1:$port before the install, so the direct and domain checks are skipped; start the dashboard and run this script again" >&2
  echo "installed: http://$host (unchecked)"
  exit 0
fi
direct=0
for _ in 1 2 3; do
  if "$nc" -z -G 2 -w 2 127.0.0.1 "$port" >/dev/null 2>&1; then direct=1; break; fi
  sleep 1
done
if (( ! direct )); then
  undo "after loading the redirect, TCP connections to 127.0.0.1:$port time out although a server listened there before"
fi
echo "ok: 127.0.0.1:$port still answers direct connections"
served=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if "$curl" -s -I --max-time 2 "http://$host/" 2>/dev/null | tr -d '\r' | grep -qi '^server: team-dashboard'; then served=1; break; fi
  sleep 1
done
if (( ! served )); then
  echo "FAIL: http://$host/ did not answer with the dashboard within 10 s. The redirect stays installed." >&2
  echo "      A dashboard of this version opens 127.0.0.1:$backend by itself within a few seconds; one started on an older version does not." >&2
  echo "      Restart it: python3 $(dirname "$here")/team.py dashboard stop && python3 $(dirname "$here")/team.py dashboard start" >&2
  echo "      If it still fails: sudo $here/diag-domain.sh /tmp/claude-team-diag.txt" >&2
  exit 1
fi
echo "ok: http://$host/ answers with the dashboard"
echo "installed: http://$host"
