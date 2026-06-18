"""Secret redaction for local artifacts before they're previewed or pushed (F2).

MCP configs and skill files routinely embed API keys, tokens, and connection
strings. We must NEVER send those to a shared registry. This module returns a
redacted copy plus a list of what was redacted, so the CLI/MCP can show the user
exactly what will (and won't) be sent before any upload."""

import re

# Field names whose values are secrets regardless of content.
SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|auth|authorization|bearer|private[_-]?key|"
    r"connection[_-]?string|dsn|credential)"
)

# Value patterns that look like secrets even under an innocuous key.
VALUE_PATTERNS = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("url_creds", re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@")),
    ("long_secret", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
]

PLACEHOLDER = "${REDACTED}"


def redact_text(text: str) -> tuple[str, list[str]]:
    """Redact secret-shaped substrings in free text. Returns (redacted, findings)."""
    findings: list[str] = []
    out = text
    for label, pat in VALUE_PATTERNS:
        if label == "url_creds":
            def _u(m: re.Match) -> str:
                findings.append("url_creds")
                return f"{m.group(1)}${{REDACTED}}@"
            out = pat.sub(_u, out)
            continue

        def _r(_m: re.Match, _label=label) -> str:
            findings.append(_label)
            return PLACEHOLDER

        out = pat.sub(_r, out)
    return out, findings


def redact_obj(obj, _key: str | None = None):
    """Recursively redact a config object. Values under secret-named keys are
    fully replaced; other strings are scanned for secret-shaped values. Returns
    (redacted_obj, findings)."""
    findings: list[str] = []

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and SECRET_KEY_RE.search(str(k)):
                out[k] = PLACEHOLDER
                findings.append(f"{k}")
            else:
                rv, f = redact_obj(v, k)
                out[k] = rv
                findings.extend(f)
        return out, findings

    if isinstance(obj, list):
        out_list = []
        for v in obj:
            rv, f = redact_obj(v, _key)
            out_list.append(rv)
            findings.extend(f)
        return out_list, findings

    if isinstance(obj, str):
        # A whole-value secret under a secret-ish key is handled above; here scan content.
        if _key and SECRET_KEY_RE.search(str(_key)):
            findings.append(str(_key))
            return PLACEHOLDER, findings
        rt, f = redact_text(obj)
        return rt, f + findings

    return obj, findings
