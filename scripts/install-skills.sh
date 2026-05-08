#!/usr/bin/env bash
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)/skills"
DEST_CLAUDE="${HOME}/.claude/skills/fleet"
mkdir -p "$DEST_CLAUDE"
for d in "$SRC"/*/; do
  name=$(basename "$d")
  rm -rf "$DEST_CLAUDE/$name"
  ln -sfn "$d" "$DEST_CLAUDE/$name"
  echo "linked $DEST_CLAUDE/$name -> $d"
done
echo "done"
