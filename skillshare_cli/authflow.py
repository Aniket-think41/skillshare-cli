"""Browser device-login + token lifecycle, shared by the CLI and the MCP server.

Implements the OAuth device-authorization flow against the backend
(/api/auth/device/*): the client gets a short user_code + a verification URL the
user opens in a browser to approve, then polls until a rotating access+refresh
token pair is issued. `valid_access_token()` returns a usable access token,
auto-refreshing (and persisting the rotated pair) when it's about to expire.

Credentials live in ~/.config/skillshare/credentials.json (chmod 600):
    {api_url, access_token, refresh_token, expires_at (epoch), username}
A legacy `token` (PAT) field is still honored for back-compat.
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from pathlib import Path

import httpx

CONFIG_DIR = Path(os.environ.get("SKILLSHARE_CONFIG_DIR", "~/.config/skillshare")).expanduser()
CREDS_FILE = CONFIG_DIR / "credentials.json"
DEFAULT_API = "https://skillshare-backend-1081098542602.us-central1.run.app"

# refresh this many seconds before the access token actually expires
_REFRESH_SKEW = 60


class AuthError(Exception):
    pass


def load_creds() -> dict:
    try:
        return json.loads(CREDS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_creds(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(json.dumps(data, indent=2))
    CREDS_FILE.chmod(0o600)


def api_url() -> str:
    return (os.environ.get("SKILLSHARE_API_URL") or load_creds().get("api_url") or DEFAULT_API).rstrip("/")


def _err_message(res: httpx.Response, fallback: str) -> str:
    try:
        return res.json()["error"]["message"]
    except (KeyError, ValueError):
        return f"{fallback} (HTTP {res.status_code})"


def device_login(api: str | None = None, client_name: str = "", open_browser: bool = True,
                 emit=print) -> dict:
    """Run the full device flow. Prints the verification URL (emit), optionally
    opens the browser, polls until approved, persists creds, returns the user dict.
    Raises AuthError on denial/expiry/timeout."""
    api = (api or api_url()).rstrip("/")
    try:
        res = httpx.post(f"{api}/api/auth/device/start", json={"client_name": client_name}, timeout=30)
    except httpx.HTTPError as e:
        raise AuthError(f"cannot reach {api} — is the API running? ({e.__class__.__name__})")
    if res.status_code >= 400:
        raise AuthError(_err_message(res, "could not start login"))
    d = res.json()
    url = d["verification_uri_complete"]

    emit("")
    emit("  To finish signing in, open this URL in your browser:")
    emit("")
    emit(f"      {url}")
    emit("")
    emit(f"  Verification code: {d['user_code']}")
    emit("  (You can log in or create an account there.) Waiting for approval…")
    emit("")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    interval = max(1, int(d.get("interval", 5)))
    deadline = time.time() + int(d.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            pr = httpx.post(f"{api}/api/auth/device/token", json={"device_code": d["device_code"]}, timeout=30)
        except httpx.HTTPError:
            continue  # transient; keep polling
        if pr.status_code >= 400:
            raise AuthError(_err_message(pr, "login failed"))
        st = pr.json()
        status = st.get("status")
        if status == "approved":
            save_creds({
                "api_url": api,
                "access_token": st["access_token"],
                "refresh_token": st["refresh_token"],
                "expires_at": time.time() + int(st.get("expires_in", 900)),
                "username": (st.get("user") or {}).get("username"),
            })
            return st.get("user") or {}
        if status in ("denied", "expired"):
            raise AuthError(f"login {status} — start again with `skillshare login`")
        # pending → keep polling
    raise AuthError("login timed out — start again with `skillshare login`")


def _refresh(api: str, refresh_token: str) -> dict:
    res = httpx.post(f"{api}/api/auth/refresh", json={"refresh_token": refresh_token}, timeout=30)
    if res.status_code >= 400:
        raise AuthError(_err_message(res, "could not refresh session"))
    return res.json()


def valid_access_token() -> str | None:
    """Return a usable bearer token, refreshing if the access token is near expiry.
    Resolution order: SKILLSHARE_TOKEN env → legacy PAT in creds → access token
    (auto-refreshed). Returns None when there are no credentials at all."""
    env = os.environ.get("SKILLSHARE_TOKEN")
    if env:
        return env
    creds = load_creds()
    if creds.get("token"):  # legacy PAT
        return creds["token"]
    access = creds.get("access_token")
    refresh = creds.get("refresh_token")
    if not access:
        return None
    if time.time() < creds.get("expires_at", 0) - _REFRESH_SKEW:
        return access
    if not refresh:
        return access  # no way to refresh; let the request 401 and prompt re-login
    try:
        api = creds.get("api_url") or api_url()
        j = _refresh(api, refresh)
    except AuthError:
        return access  # refresh failed; the caller's 401 handler surfaces re-login
    creds.update(
        access_token=j["access_token"],
        refresh_token=j.get("refresh_token", refresh),
        expires_at=time.time() + int(j.get("expires_in", 900)),
    )
    save_creds(creds)
    return creds["access_token"]


def clear_creds() -> None:
    try:
        CREDS_FILE.unlink()
    except OSError:
        pass
