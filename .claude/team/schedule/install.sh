#!/bin/bash
set -euo pipefail
team="$(cd "$(dirname "$0")/.." && pwd)"
model="${RITUAL_MODEL:-opus}"
if ! [[ "$model" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RITUAL_MODEL may contain only letters, digits, '.', '_' and '-', got: $model" >&2
  exit 1
fi
agents="$HOME/Library/LaunchAgents"
uid=$(id -u)
mkdir -p "$agents" "$team/state"
chmod +x "$team/schedule/run-ritual.sh"

xml() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

weekdays() {
  local out="<array>"
  for d in 1 2 3 4 5; do
    out+="<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"
  done
  echo "$out</array>"
}

envvars=""
for v in CLAUDE_BIN RITUAL_MODEL TEAM_LANG; do
  if [[ -n "${!v:-}" ]]; then
    envvars+="<key>$v</key><string>$(xml "${!v}")</string>"
  fi
done
if [[ -n "$envvars" ]]; then
  envvars="<key>EnvironmentVariables</key><dict>$envvars</dict>"
fi

weekly() {
  echo "<dict><key>Weekday</key><integer>$1</integer><key>Hour</key><integer>$2</integer><key>Minute</key><integer>$3</integer></dict>"
}

job() {
  local label="com.claude-team.$1" plist="$agents/com.claude-team.$1.plist" t
  t=$(xml "$team")
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>$t/schedule/run-ritual.sh</string><string>$(xml "$1")</string><string>$(xml "$2")</string><string>$(xml "$3")</string><string>$(xml "$4")</string><string>$(xml "$6")</string></array>
<key>StartCalendarInterval</key>$5
$envvars
<key>StandardOutPath</key><string>$t/state/ritual-$(xml "$1").log</string>
<key>StandardErrorPath</key><string>$t/state/ritual-$(xml "$1").log</string>
</dict></plist>
PLIST
  plutil -lint "$plist" >/dev/null
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$plist"
  echo "installed $label"
}

job daily sonnet medium 3 "$(weekdays 9 7)" 9
job planning "$model" high 5 "$(weekly 1 9 37)" 9
job retro "$model" high 6 "$(weekly 5 16 7)" 16
job reflect "$model" high 6 "$(weekdays 18 47)" 18
launchctl list | grep com.claude-team
