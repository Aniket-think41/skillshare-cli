"""Content fingerprinting for local artifacts (F2).

A fingerprint is a stable short hash of an artifact's *normalized* content, so
an unchanged file always maps to the same id (→ stays dismissed / known pushed)
while an edit produces a new id (→ re-surfaces as a fresh candidate)."""

import hashlib
import json


def _normalize(text: str) -> str:
    # Line-ending + trailing-whitespace insensitive; collapse trailing blank lines.
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def fingerprint_text(text: str) -> str:
    h = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()
    return f"fp_{h[:24]}"


def fingerprint_obj(obj) -> str:
    """Fingerprint structured data (e.g. an MCP server config) independent of key
    ordering / whitespace."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return fingerprint_text(canonical)
