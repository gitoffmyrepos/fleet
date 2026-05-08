#!/usr/bin/env bash
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)/commands"
DEST="${HOME}/.claude/commands/fleet"
mkdir -p "$DEST"
for f in "$SRC"/*.md; do
  name=$(basename "$f")
  # rename fleet-foo.md → foo.md inside the namespaced dir
  target="${name#fleet-}"
  target="${target/fleet.md/_root.md}"
  rm -f "$DEST/$target"
  ln -sfn "$f" "$DEST/$target"
  echo "linked $DEST/$target -> $f"
done
echo "done"
