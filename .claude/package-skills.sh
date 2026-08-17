#!/usr/bin/env bash
# Package each Claude Code skill as an individual ZIP for manual import into
# the Claude Desktop app via Settings > Customize > Skills.
#
# Usage:
#   ./package-skills.sh [skills-dir] [output-dir]
#
# Defaults to .claude/skills/ for skills and build/skills/ for output.
# Reads skills.location from [tool.repomatic] in pyproject.toml if present.
#
# --- Why this exists ---
#
# Claude has three separate skill systems that do not share a registry:
#
#   1. Claude Code (CLI + Desktop Code tab)
#      Skills live in ~/.claude/skills/ (personal) or .claude/skills/ (project).
#      Auto-discovered at runtime, invocable via /skill-name. The Desktop
#      Customize > Skills panel does not display them (UI bug, not a loading
#      bug: they still work via the / menu).
#
#   2. Claude Desktop chat (claude.ai / Desktop app chat mode)
#      Runs in a server-side container. The primary route is now the plugin
#      archive: Customize > Plugins accepts one upload that carries every
#      skill and agent (verified 2026-08-09; see docs/plugin.md). This
#      script's per-skill ZIPs remain for the older Customize > Skills
#      panel, where the mount is unreliable: metadata is registered in the
#      system prompt, yet SKILL.md files are often not mounted on disk
#      (#26254), and uploads sometimes return internal server errors
#      (#26310).
#
#   3. Cowork
#      Another container. Plugins install and their skills work in chat
#      (hooks and sub-agents run only in Cowork and appear greyed out in
#      chat). It also reads project-level .claude/skills/ from the session
#      mount, but ignores ~/.claude/skills/ (personal skills). Opening a
#      project that contains .claude/skills/ exposes those skills to
#      Cowork.
#
# This script packages each skill as a ZIP for manual upload to the
# Customize > Skills panel of surface 2. It cannot automate the upload: the
# Desktop app sends ZIPs to Anthropic's servers via its Electron bridge, and
# there is no public API or local storage to write to. The plugin archive
# supersedes it for the bulk case (one upload, agents included): keep this
# script for the surfaces and accounts where a plugin is not an option.
#
# --- Workaround for Cowork ---
#
# Cowork mounts project-level .claude/skills/ into the session container.
# Opening the project in Cowork exposes the skills: they are readable on
# disk but not registered as / slash commands. Tell Cowork to read and
# follow a skill directly, e.g.:
#
#   "Read and follow .claude/skills/repomatic-audit/SKILL.md"
#
# --- Upstream issues to track ---
#
# See: https://github.com/kdeldycke/repomatic/issues/2540

set -euo pipefail

# Try to read skills.location from pyproject.toml if no argument given.
if [ -z "${1:-}" ] && [ -f pyproject.toml ]; then
	configured=$(python3 -c "
import sys
try:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    data = tomllib.loads(open('pyproject.toml', 'rb').read().decode())
    loc = data.get('tool', {}).get('repomatic', {}).get('skills', {}).get('location', '')
    if loc:
        print(loc.strip('./'))
except Exception:
    pass
" 2>/dev/null || true)
fi

SKILLS_DIR="${1:-${configured:-.claude/skills}}"
OUTPUT_DIR="${2:-build/skills}"

if [ ! -d "$SKILLS_DIR" ]; then
	echo "No skills directory found at $SKILLS_DIR" >&2
	exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

count=0
for skill_dir in "$SKILLS_DIR"/*/; do
	[ -f "$skill_dir/SKILL.md" ] || continue
	skill_name="$(basename "$skill_dir")"
	zip -q -j "$OUTPUT_DIR/$skill_name.zip" "$skill_dir"SKILL.md
	count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
	echo "No skills with SKILL.md found in $SKILLS_DIR" >&2
	exit 1
fi

echo "Packaged $count skills into $OUTPUT_DIR/"
ls -1 "$OUTPUT_DIR"/*.zip
echo ""
echo "Upload each ZIP via Claude Desktop > Settings > Customize > Skills."
