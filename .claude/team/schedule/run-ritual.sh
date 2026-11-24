#!/bin/zsh -l
set -u
skill=$1 model=$2 effort=$3 budget=$4 hour=$5
team=$(cd "$(dirname "$0")/.." && pwd -P) || exit 1
workspace=$(cd "$team/../.." && pwd -P) || exit 1
claude_bin="${CLAUDE_BIN:-$(command -v claude)}"
day=$(date +%F)
note="$team/rituals/$skill/$day.md"
lock="$team/state/ritual.lock"
limit="${RITUAL_LOCK_WAIT:-5400}"
echo "=== $(date '+%F %T') $skill"

due() {
  if [[ "$(date +%F)" != "$day" ]]; then
    echo "skip: the day changed while waiting"
    return 1
  fi
  if [[ -e "$note" ]]; then
    echo "skip: $note already exists"
    return 1
  fi
  now=$((10#$(date +%H)))
  if (( now < hour || now > hour + 3 )); then
    echo "skip: outside the $hour:00-$((hour + 3)):59 window (missed run after sleep)"
    return 1
  fi
}

stop() {
  kill "$waiter" 2>/dev/null
  echo "stop: signal while waiting for $lock"
  exit "$1"
}

due || exit 0
case "$limit" in
  '' | *[!0-9]*)
    echo "RITUAL_LOCK_WAIT must be a whole number of seconds, got: $limit"
    exit 1
    ;;
esac
if [[ -z "$claude_bin" ]]; then
  echo "claude not found on PATH; set CLAUDE_BIN"
  exit 1
fi
mkdir -p "$team/state" || exit 1
exec 9>>"$lock" || exit 1
/usr/bin/lockf -s -t 0 9
rc=$?
if (( rc == 75 )); then
  echo "wait: another ritual holds $lock, waiting up to ${limit}s"
  started=$(date +%s)
  /usr/bin/lockf -s -t "$limit" 9 &
  waiter=$!
  trap 'stop 129' HUP
  trap 'stop 130' INT
  trap 'stop 143' TERM
  wait "$waiter"
  rc=$?
  trap - HUP INT TERM
  if (( rc == 75 )); then
    echo "skip: another ritual still holds $lock after ${limit}s"
    exit 0
  fi
  if (( rc == 0 )); then
    echo "lock: acquired after $(( $(date +%s) - started ))s"
    due || exit 0
  fi
fi
if (( rc != 0 )); then
  echo "lockf failed with exit $rc on $lock"
  exit 1
fi
cd "$workspace" || exit 1
TEAM_RITUAL=$skill perl -e 'alarm 3600; exec @ARGV' "$claude_bin" -p "/$skill" --model "$model" --effort "$effort" --permission-mode auto --max-budget-usd "$budget" </dev/null 9>&-
rc=$?
echo "=== $(date '+%F %T') $skill exit $rc"
exit $rc
