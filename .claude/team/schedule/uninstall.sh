#!/bin/bash
uid=$(id -u)
for j in daily planning retro reflect; do
  launchctl bootout "gui/$uid/com.claude-team.$j" 2>/dev/null && echo "removed com.claude-team.$j"
  rm -f "$HOME/Library/LaunchAgents/com.claude-team.$j.plist"
done
