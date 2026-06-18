"""Local artifact detectors (F2).

Each detector knows where one agent keeps its skills / MCP servers / notes and
yields Candidate records. Claude Code is the primary, fully-supported source;
Cursor, Claude Desktop, and Codex are supported via their known config files.
Add a new agent by writing one `detect_*` function and registering it below."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # py311+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # codex (TOML) detector degrades gracefully

from .fingerprint import fingerprint_obj, fingerprint_text


@dataclass
class Candidate:
    kind: str  # skill | mcp | note
    name: str
    source: str  # claude-code | cursor | claude-desktop | codex
    path: str
    title: str
    description: str = ""
    content_md: str = ""
    server_url: str | None = None
    config_json: str | None = None
    raw_config: dict | None = None
    tags: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        if self.kind == "mcp" and self.raw_config is not None:
            return fingerprint_obj(self.raw_config)
        return fingerprint_text(self.content_md or self.config_json or self.title)


# ---------------- helpers ----------------

def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _read_toml(p: Path) -> dict | None:
    if tomllib is None:
        return None
    try:
        return tomllib.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _h1(text: str) -> str | None:
    m = re.search(r"^\s*#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-frontmatter reader: `key: value` pairs between leading ---."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _skill_candidate(skill_md: Path, source: str) -> Candidate:
    text = skill_md.read_text(errors="replace")
    fm = _frontmatter(text)
    name = fm.get("name") or skill_md.parent.name
    return Candidate(
        kind="skill",
        name=name,
        source=source,
        path=str(skill_md),
        title=fm.get("name") or _h1(text) or name,
        description=fm.get("description", ""),
        content_md=text,
    )


def _mcp_candidate(name: str, cfg: dict, source: str, path: str) -> Candidate:
    server_url = cfg.get("url") if isinstance(cfg, dict) else None
    raw = {"mcpServers": {name: cfg}}
    return Candidate(
        kind="mcp",
        name=name,
        source=source,
        path=path,
        title=name,
        description=(cfg.get("command") or cfg.get("url") or "") if isinstance(cfg, dict) else "",
        server_url=server_url,
        config_json=json.dumps(raw, indent=2),
        raw_config=raw,
    )


def _servers_from(obj: dict | None, source: str, path: str, key: str = "mcpServers") -> list[Candidate]:
    if not obj or not isinstance(obj.get(key), dict):
        return []
    return [_mcp_candidate(n, c, source, path) for n, c in obj[key].items() if isinstance(c, dict)]


# ---------------- detectors ----------------

def detect_claude_code(home: Path, cwd: Path, notes_dir: Path | None) -> list[Candidate]:
    out: list[Candidate] = []
    # skills: SKILL.md under user + project skills dirs
    for base in (home / ".claude" / "skills", cwd / ".claude" / "skills"):
        if base.is_dir():
            for skill_md in base.glob("*/SKILL.md"):
                out.append(_skill_candidate(skill_md, "claude-code"))
    # mcp: ~/.claude.json (top-level + per-project) and project .mcp.json
    cfg = _read_json(home / ".claude.json")
    if cfg:
        out += _servers_from(cfg, "claude-code", str(home / ".claude.json"))
        projects = cfg.get("projects")
        if isinstance(projects, dict):
            for proj_path, pcfg in projects.items():
                if isinstance(pcfg, dict):
                    out += _servers_from(pcfg, "claude-code", f"~/.claude.json#projects/{proj_path}")
    out += _servers_from(_read_json(cwd / ".mcp.json"), "claude-code", str(cwd / ".mcp.json"))
    # notes: project memory files + an optional notes dir of markdown
    for note in (cwd / "CLAUDE.md", cwd / "AGENTS.md", home / ".claude" / "CLAUDE.md"):
        if note.is_file():
            text = note.read_text(errors="replace")
            out.append(Candidate(
                kind="note", name=note.name, source="claude-code", path=str(note),
                title=_h1(text) or note.stem, content_md=text,
            ))
    if notes_dir and notes_dir.is_dir():
        for md in sorted(notes_dir.glob("*.md")):
            text = md.read_text(errors="replace")
            out.append(Candidate(
                kind="note", name=md.name, source="claude-code", path=str(md),
                title=_h1(text) or md.stem, content_md=text,
            ))
    return out


def detect_cursor(home: Path, cwd: Path, _notes_dir: Path | None) -> list[Candidate]:
    out: list[Candidate] = []
    for p in (home / ".cursor" / "mcp.json", cwd / ".cursor" / "mcp.json"):
        out += _servers_from(_read_json(p), "cursor", str(p))
    return out


def detect_claude_desktop(home: Path, _cwd: Path, _notes_dir: Path | None) -> list[Candidate]:
    paths = [
        home / ".config" / "Claude" / "claude_desktop_config.json",  # linux
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",  # mac
        Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json",  # win
    ]
    out: list[Candidate] = []
    for p in paths:
        out += _servers_from(_read_json(p), "claude-desktop", str(p))
    return out


def detect_codex(home: Path, _cwd: Path, _notes_dir: Path | None) -> list[Candidate]:
    cfg = _read_toml(home / ".codex" / "config.toml")
    if not cfg or not isinstance(cfg.get("mcp_servers"), dict):
        return []
    path = str(home / ".codex" / "config.toml")
    return [_mcp_candidate(n, c, "codex", path) for n, c in cfg["mcp_servers"].items() if isinstance(c, dict)]


DETECTORS = {
    "claude-code": detect_claude_code,
    "cursor": detect_cursor,
    "claude-desktop": detect_claude_desktop,
    "codex": detect_codex,
}
