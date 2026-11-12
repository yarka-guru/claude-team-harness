#!/bin/zsh -l
set -u
skill=$1 model=$2 effort=$3 budget=$4 hour=$5
team="${0:A:h:h}"
workspace="${team:h:h}"
claude_bin="${CLAUDE_BIN:-$(command -v claude)}"
note="$team/rituals/$skill/$(date +%F).md"
lock="${TMPDIR:-/tmp}/claude-team-ritual.lock"
echo "=== $(date '+%F %T') $skill"
if [[ -e "$note" ]]; then
  echo "skip: $note already exists"
  exit 0
fi
now=$((10#$(date +%H)))
if (( now < hour || now > hour + 3 )); then
  echo "skip: outside the $hour:00-$((hour + 3)):59 window (missed run after sleep)"
  exit 0
fi
if [[ -d "$lock" ]]; then
  pid=$(cat "$lock/pid" 2>/dev/null)
  if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
    rm -rf "$lock"
  elif [[ -z "$pid" && -n $(find "$lock" -maxdepth 0 -mmin +1) ]]; then
    rm -rf "$lock"
  fi
fi
if ! mkdir "$lock" 2>/dev/null; then
  echo "skip: another ritual is running ($lock)"
  exit 0
fi
echo $$ > "$lock/pid"
trap 'rm -rf "$lock"' EXIT INT TERM HUP
cd "$workspace" || exit 1
if [[ -z "$claude_bin" ]]; then
  echo "claude not found on PATH; set CLAUDE_BIN"
  exit 1
fi
TEAM_RITUAL=$skill perl -e 'alarm 3600; exec @ARGV' "$claude_bin" -p "/$skill" --model "$model" --effort "$effort" --permission-mode auto --max-budget-usd "$budget" </dev/null
rc=$?
echo "=== $(date '+%F %T') $skill exit $rc"
exit $rc
