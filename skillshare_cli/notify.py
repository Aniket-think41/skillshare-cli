"""Desktop notifications for SkillShare — interactive, themed cards.

Used by both the CLI (`skillshare watch`) and the MCP server so that inbox
items (something added/published in a scope you belong to) and ready-to-share
local artifacts pop a native card at the bottom-right of the screen — with CTAs
(Add / Reject for inbound, Push / Maybe later for local), themed to match the
website.

The card itself is a standalone GTK process (`notifier_gui.py`) spawned
non-blocking; if GTK/`gi` isn't available it falls back to a libnotify toast,
then to stderr. Everything here is best-effort and never raises.

A small JSON state file (shared by CLI + MCP) dedupes what's already been shown,
so a card fires once per item across both surfaces.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "SkillShare"
CONFIG_DIR = Path(os.environ.get("SKILLSHARE_CONFIG_DIR", "~/.config/skillshare")).expanduser()
STATE_FILE = CONFIG_DIR / "watch-state.json"
GUI = Path(__file__).with_name("notifier_gui.py")
PANEL = Path(__file__).with_name("panel_gui.py")
_MAX_REMEMBERED = 500


# ---------------- shared dedup state ----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
        STATE_FILE.chmod(0o600)
    except OSError:
        pass


def _remember(values, new) -> list:
    seen = list(dict.fromkeys(list(values) + list(new)))
    return seen[-_MAX_REMEMBERED:]


# ---------------- presentation ----------------

@functools.lru_cache(maxsize=4)
def _has_gi(exe: str) -> bool:
    try:
        return subprocess.run([exe, "-c", "import gi"], capture_output=True, timeout=6).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _gui_python() -> str | None:
    """A Python interpreter that can import PyGObject (usually system python3,
    not the venv that runs skillshare)."""
    for cand in ("/usr/bin/python3", shutil.which("python3") or "", sys.executable):
        if cand and os.path.exists(cand) and _has_gi(cand):
            return cand
    return None


def _toast(title: str, body: str = "", *, icon: str | None = None) -> None:
    """Fallback when the GTK card can't run: a plain libnotify/zenity toast."""
    tool = shutil.which("notify-send") or shutil.which("zenity")
    try:
        if tool and tool.endswith("notify-send"):
            cmd = ["notify-send", "-a", APP_NAME]
            if icon:
                cmd += ["-i", icon]
            subprocess.run(cmd + [title, body], check=False, timeout=5)
            return
        if tool and tool.endswith("zenity"):
            subprocess.run(["zenity", "--notification", "--text", f"{title}\n{body}".strip()],
                           check=False, timeout=5)
            return
    except (OSError, subprocess.SubprocessError):
        pass
    print(f"\U0001f514 {title} — {body}", file=sys.stderr)


def present_card(card: dict) -> bool:
    """Pop the themed GTK card (non-blocking). Falls back to a toast. Returns
    True if the rich card was launched."""
    py = _gui_python()
    if py and GUI.exists():
        try:
            env = dict(os.environ)
            env.setdefault("GDK_BACKEND", "x11")
            subprocess.Popen([py, str(GUI), json.dumps(card)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    icon = "document-send" if card.get("type") == "local" else "dialog-information"
    body = card.get("description") or card.get("meta") or ""
    _toast(card.get("title", APP_NAME), body, icon=icon)
    return False


def open_panel(items: list[dict]) -> bool:
    """Open the clickable install panel (a GTK window). Items are passed on stdin
    as JSON so a long list never hits argv limits. Returns True if launched."""
    py = _gui_python()
    if not (py and PANEL.exists()):
        return False
    try:
        env = dict(os.environ)
        env.setdefault("GDK_BACKEND", "x11")
        proc = subprocess.Popen([py, str(PANEL)], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.stdin.write(json.dumps(items).encode())
        proc.stdin.close()
        return True
    except OSError:
        return False


# ---------------- card builders ----------------

def _actor(n: dict) -> str:
    a = n.get("actor")
    if isinstance(a, dict):
        return a.get("username", "?")
    return a or "?"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_date(iso) -> str:
    if not iso:
        return ""
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return f"{_MONTHS[m - 1]} {d}, {y}"
    except (ValueError, IndexError):
        return ""


_KIND_LABEL = {"skill": "Skill", "mcp": "MCP server", "note": "Note"}
_SCOPE_LABEL = {"org": "Organization", "project": "Project", "pod": "Pod"}


def _inbound_card(n: dict, offset: int) -> dict:
    kind = (n.get("object_kind") or "skill").lower()
    verb = (n.get("verb") or "added").lower()
    scope_type = (n.get("scope_type") or "ORG").lower()
    actor = _actor(n)
    details = [["Type", _KIND_LABEL.get(kind, kind.title())]]
    if n.get("scope_name"):
        details.append([_SCOPE_LABEL.get(scope_type, "Scope"), n["scope_name"]])
    details.append(["Shared by", f"@{actor}"])
    date = _fmt_date(n.get("created_at"))
    if date:
        details.append(["Added", date])
    badge = "Published" if verb == "published" else "New"
    return {
        "type": "inbound",
        "kind": kind,
        "badge": f"{badge} {kind}",
        "title": n.get("object_title") or n.get("title") or "New resource",
        "description": n.get("object_description") or "",
        "details": details,
        "avatar": actor,
        "resource_id": n.get("object_id"),
        "notification_id": n.get("id"),
        "offset": offset,
    }


def announce_inbox(items: list[dict], *, seed_silently: bool = False) -> int:
    """Present a card for each inbox notification not shown before. `items` are
    notification dicts (id, object_*, verb, actor, scope_type).

    seed_silently: mark current items seen WITHOUT popping — used on a fresh
    watcher start so a backlog doesn't flood. Returns cards shown.
    """
    state = _load_state()
    seen = set(state.get("seen_notification_ids", []))
    fresh = [n for n in items if n.get("id") and n["id"] not in seen]
    shown = 0
    if fresh and not seed_silently:
        for i, n in enumerate(fresh[:5]):
            present_card(_inbound_card(n, offset=i))
            shown += 1
    state["seen_notification_ids"] = _remember(seen, [n["id"] for n in items if n.get("id")])
    _save_state(state)
    return shown


def _cand(row: dict):
    return row.get("candidate")


def _shareable_card(row: dict, target_org: str, offset: int) -> dict:
    c = _cand(row)
    kind = (getattr(c, "kind", None) or (c.get("kind") if isinstance(c, dict) else None) or "skill").lower()
    title = getattr(c, "title", None) or (c.get("title") if isinstance(c, dict) else None) or "Local artifact"
    desc = getattr(c, "description", None) or (c.get("description") if isinstance(c, dict) else None) or ""
    source = getattr(c, "source", None) or (c.get("source") if isinstance(c, dict) else None) or "local"
    details = [
        ["Type", _KIND_LABEL.get(kind, kind.title())],
        ["Found in", source],
    ]
    if target_org:
        details.append(["Push to", target_org])
    details.append(["Status", "Not shared yet"])
    return {
        "type": "local",
        "kind": kind,
        "badge": f"Local {kind}",
        "title": title,
        "description": desc,
        "details": details,
        "avatar": kind,
        "fingerprint": row.get("fingerprint"),
        "target_org": target_org,
        "offset": offset,
    }


def announce_shareable(rows: list[dict], target_org: str = "") -> int:
    """Present a card for each NEW local artifact (status == 'new') not shown
    before. `rows` come from reconcile() — each has 'status', 'fingerprint',
    'candidate'. Returns cards shown."""
    state = _load_state()
    done = set(state.get("shared_toasted_fingerprints", []))
    fresh = [r for r in rows if r.get("status") == "new" and r.get("fingerprint") not in done]
    shown = 0
    if fresh:
        for i, r in enumerate(fresh[:5]):
            present_card(_shareable_card(r, target_org, offset=i))
            shown += 1
    state["shared_toasted_fingerprints"] = _remember(done, [r["fingerprint"] for r in fresh if r.get("fingerprint")])
    _save_state(state)
    return shown
