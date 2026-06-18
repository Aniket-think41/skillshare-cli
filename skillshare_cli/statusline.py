"""Wire the SkillShare status line into Claude Code's settings.json.

The status bar is a Claude Code feature (settings.json → statusLine), not an MCP
capability, so we enable it by merging a `statusLine` entry into the user's (or
project's) settings.json. Used by both `skillshare setup-statusline` and the MCP
`setup_statusline` tool. Idempotent, JSON-safe, and reversible.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


class StatusLineError(Exception):
    pass


def status_command() -> str:
    """Best-effort command string Claude Code should run for the status line.
    Prefers the installed `skillshare` entry point (absolute path, since Claude
    Code's exec environment may not share the user's PATH); falls back to running
    the module with the current interpreter."""
    exe = shutil.which("skillshare")
    if exe:
        return f'"{exe}" status'
    return f'"{sys.executable}" -m skillshare_cli.main status'


def claude_settings_path(scope: str = "user", cwd: Path | None = None) -> Path:
    if scope == "project":
        return (cwd or Path.cwd()) / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise StatusLineError(
            f"{path} isn't valid JSON — fix or remove it, then re-run (I won't risk corrupting it)"
        )
    if not isinstance(data, dict):
        raise StatusLineError(f"{path} doesn't contain a JSON object")
    return data


def _write(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def _is_ours(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and "skillshare" in entry["command"].lower()
    )


def enable(scope: str = "user", force: bool = False, command: str | None = None,
           cwd: Path | None = None) -> dict:
    """Merge the statusLine entry into settings.json (preserving everything else).
    Returns a result dict with `status` ∈ enabled | already-enabled | exists-different."""
    path = claude_settings_path(scope, cwd)
    settings = _load(path)
    cmd = command or status_command()
    desired = {"type": "command", "command": cmd}
    existing = settings.get("statusLine")
    if existing == desired:
        return {"status": "already-enabled", "changed": False, "path": str(path), "command": cmd}
    if existing and not _is_ours(existing) and not force:
        # Don't clobber a status line the user (or another tool) set up.
        return {"status": "exists-different", "changed": False, "path": str(path),
                "existing": existing, "command": cmd}
    settings["statusLine"] = desired
    _write(path, settings)
    return {"status": "enabled", "changed": True, "path": str(path), "command": cmd, "previous": existing}


def disable(scope: str = "user", force: bool = False, cwd: Path | None = None) -> dict:
    """Remove the SkillShare status line. Won't remove a non-SkillShare statusLine
    unless force=True. Returns `status` ∈ removed | not-set | not-ours | no-settings."""
    path = claude_settings_path(scope, cwd)
    if not path.exists():
        return {"status": "no-settings", "changed": False, "path": str(path)}
    settings = _load(path)
    existing = settings.get("statusLine")
    if not existing:
        return {"status": "not-set", "changed": False, "path": str(path)}
    if not _is_ours(existing) and not force:
        return {"status": "not-ours", "changed": False, "path": str(path), "existing": existing}
    removed = settings.pop("statusLine", None)
    _write(path, settings)
    return {"status": "removed", "changed": True, "path": str(path), "removed": removed}
