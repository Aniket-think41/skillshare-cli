"""Scan orchestration + reconciliation + redaction (F2).

Ties the detectors, fingerprinting, server-side provenance, and secret redaction
together so both the CLI and the MCP server share one implementation."""

from __future__ import annotations

import json
from pathlib import Path

from .detect import DETECTORS, Candidate
from .redact import redact_obj, redact_text


def scan(sources: list[str] | None = None, notes_dir: str | None = None,
         home: Path | None = None, cwd: Path | None = None) -> list[Candidate]:
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    ndir = Path(notes_dir).expanduser() if notes_dir else None
    chosen = sources or list(DETECTORS)
    found: list[Candidate] = []
    for s in chosen:
        det = DETECTORS.get(s)
        if det:
            found.extend(det(home, cwd, ndir))
    # dedupe by fingerprint (same artifact configured in two places → one candidate)
    seen: set[str] = set()
    uniq: list[Candidate] = []
    for c in found:
        fp = c.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        uniq.append(c)
    return uniq


def reconcile(candidates: list[Candidate], server_state: list[dict]) -> list[dict]:
    """Annotate each candidate with its known status. server_state is the list
    from GET /api/local-state. status ∈ new | pushed | dismissed."""
    by_fp = {s["fingerprint"]: s for s in server_state}
    rows: list[dict] = []
    for c in candidates:
        fp = c.fingerprint()
        known = by_fp.get(fp)
        rows.append({
            "candidate": c,
            "fingerprint": fp,
            "status": known["status"] if known else "new",
            "resource_id": (known or {}).get("resource_id"),
        })
    return rows


def redacted_payload(c: Candidate) -> tuple[dict, list[str]]:
    """Build the resource-create payload for a candidate with secrets stripped,
    plus the list of redaction findings to show the user before pushing."""
    findings: list[str] = []
    payload: dict = {
        "type": {"skill": "SKILL", "mcp": "MCP", "note": "NOTE"}[c.kind],
        "title": c.title,
        "description": c.description,
        "tags": list(c.tags),
        "source_fingerprint": c.fingerprint(),
    }
    if c.kind == "mcp":
        raw = c.raw_config or {}
        red, f = redact_obj(raw)
        findings += f
        payload["config_json"] = json.dumps(red, indent=2)
        if c.server_url:
            url_red, uf = redact_text(c.server_url)
            payload["server_url"] = url_red
            findings += uf
        # carry any prose
        if c.content_md:
            cmd, cf = redact_text(c.content_md)
            payload["content_md"] = cmd
            findings += cf
    else:  # skill / note
        cmd, f = redact_text(c.content_md)
        payload["content_md"] = cmd
        findings += f
    return payload, sorted(set(findings))
