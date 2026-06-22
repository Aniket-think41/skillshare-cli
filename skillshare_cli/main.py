"""
skillshare — command-line client for the SkillShare registry.

Auth (BACKEND_SPEC §10): `skillshare login` exchanges email+password for a
Personal Access Token (skst_…) and stores it in
~/.config/skillshare/credentials.json (chmod 600). `SKILLSHARE_TOKEN` /
`SKILLSHARE_API_URL` env vars override the stored config (useful in CI).

    skillshare login                          interactive login, mints a PAT
    skillshare whoami / orgs / tokens         account
    skillshare search "code review" --type SKILL
    skillshare get pr-reviewer                full resource detail
    skillshare pull pr-reviewer -o ./skills   save SKILL.md / config / note + assets
    skillshare list --org think41             org resources
    skillshare list --pod pod-retrieval       pod library incl. inherited
    skillshare upload diagram.png --kind image          store a file, print its URL
    skillshare add note --org think41 --title "..." -f notes.md \
        --image diagram.png --video demo.mp4 --link https://docs.example.com
    skillshare star <id> / publish <id> / import <id> --org <slug>
    skillshare pin <id> / unpin <id> / pins      curate your profile's pinned set
    skillshare avatar me.png                      set your profile photo
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

import httpx

from .authflow import (
    CONFIG_DIR,
    CREDS_FILE,
    DEFAULT_API,
    AuthError,
    api_url,
    clear_creds,
    device_login,
)
from .authflow import load_creds as _load_creds
from .authflow import save_creds as _save_creds
from .authflow import valid_access_token
from .local.detect import DETECTORS
from .local.scan import reconcile, redacted_payload, scan

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GLYPH = {"SKILL": "◆", "MCP": "▣", "NOTE": "▤"}
STATUS_CACHE = CONFIG_DIR / "status-cache.json"


def _invalidate_status_cache() -> None:
    """Drop the cached status-line counts so the next render recomputes. Called
    after actions that genuinely change the gap (install/push) — never on dismiss,
    so dismissing a notification can't move the count."""
    try:
        STATUS_CACHE.unlink()
    except OSError:
        pass


def _fg(hex_color: str) -> str:
    """24-bit truecolor foreground escape from a #rrggbb string."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return f"\033[38;2;{r};{g};{b}m"


# website theme accents, reused in the status line (skill=violet, mcp=blue, note=orange)
VIOLET, BLUE, ORANGE = _fg("#735ae5"), _fg("#2f6df0"), _fg("#e2552b")
KIND_FG = {"skill": VIOLET, "mcp": BLUE, "note": ORANGE}
KIND_MARK = {"skill": "◆", "mcp": "▣", "note": "▤"}


def token() -> str | None:
    """Current bearer token, auto-refreshed when near expiry (see authflow)."""
    return valid_access_token()


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def request(method: str, path: str, *, body: dict | None = None, params: dict | None = None, auth: bool = True):
    headers = {}
    if auth:
        t = token()
        if not t:
            die("not logged in — run `skillshare login` (or set SKILLSHARE_TOKEN)")
        headers["Authorization"] = f"Bearer {t}"
    try:
        res = httpx.request(method, f"{api_url()}{path}", json=body, params=params, headers=headers, timeout=30)
    except httpx.HTTPError as e:
        die(f"cannot reach {api_url()} — is the API running? ({e.__class__.__name__})")
    if res.status_code == 401 and auth:
        die("your session is no longer valid — run `skillshare login` to sign in again")
    if res.status_code >= 400:
        try:
            err = res.json()["error"]
            die(f"{err['code']}: {err['message']}")
        except (KeyError, ValueError):
            die(f"HTTP {res.status_code}: {res.text[:200]}")
    return res.json() if res.text else None


def upload_file(path: str, kind: str) -> dict:
    """Upload a local file via POST /api/uploads (multipart) and return
    {file_url, filename, content_type, size_bytes}. Storage backend is the
    server's concern (local disk in dev, S3/R2 in prod) — we just get a URL."""
    p = Path(path).expanduser()
    if not p.is_file():
        die(f"file not found: {path}")
    t = token()
    if not t:
        die("not logged in — run `skillshare login` (or set SKILLSHARE_TOKEN)")
    try:
        with p.open("rb") as fh:
            res = httpx.post(
                f"{api_url()}/api/uploads",
                headers={"Authorization": f"Bearer {t}"},
                data={"kind": kind},
                files={"file": (p.name, fh)},
                timeout=120,
            )
    except httpx.HTTPError as e:
        die(f"cannot reach {api_url()} ({e.__class__.__name__})")
    if res.status_code >= 400:
        try:
            err = res.json()["error"]
            die(f"{err['code']}: {err['message']}")
        except (KeyError, ValueError):
            die(f"HTTP {res.status_code}: {res.text[:200]}")
    return res.json()


def _resolve_attachment(value: str, kind: str) -> dict:
    """A CLI attachment value is either a local file (uploaded, then attached)
    or an already-public URL (attached as-is). Links are always URLs."""
    p = Path(value).expanduser()
    if kind != "link" and p.is_file():
        up = upload_file(str(p), kind)
        return {"kind": kind, "url": up["file_url"], "title": p.name}
    return {"kind": kind, "url": value}


def print_resources(rows: list[dict]) -> None:
    if not rows:
        print(f"{DIM}no resources{RESET}")
        return
    for r in rows:
        scope = r.get("scope_label") or r["scope_type"]
        author = (r.get("author") or {}).get("username", "?")
        pub = " 🌐" if r.get("is_public") else ""
        print(
            f"{GLYPH.get(r['type'], '·')} {BOLD}{r['title']}{RESET} {DIM}v{r['version']}{RESET}"
            f"  [{r['id']}]  {DIM}{scope.lower()} · @{author} · ★{r['stars_count']}{pub}{RESET}"
        )
        if r.get("description"):
            print(f"    {r['description'][:100]}")


# ---------------- commands ----------------

def _client_name() -> str:
    host = os.uname().nodename if hasattr(os, "uname") else "this device"
    return f"SkillShare CLI on {host}"


def cmd_login(args) -> None:
    url = (args.api or api_url()).rstrip("/")

    # Legacy: direct email/password login (kept for scripts/CI). The default is the
    # browser device flow, like `gh auth login`.
    if args.password or args.email:
        email = args.email or input("Email: ").strip()
        password = args.password or getpass.getpass("Password: ")
        res = httpx.post(f"{url}/api/auth/login", json={"email": email, "password": password}, timeout=30)
        if res.status_code >= 400:
            die(res.json().get("error", {}).get("message", f"login failed (HTTP {res.status_code})"))
        body = res.json()
        _save_creds({
            "api_url": url,
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_at": __import__("time").time() + body.get("expires_in", 900),
            "username": body["user"]["username"],
        })
        print(f"Logged in as {BOLD}@{body['user']['username']}{RESET} — stored in {CREDS_FILE}")
        return

    # Default: browser device-authorization flow.
    try:
        user = device_login(api=url, client_name=_client_name(), open_browser=not args.no_browser, emit=print)
    except AuthError as e:
        die(str(e))
    print(f"{BOLD}✓ Logged in as @{user.get('username', '?')}{RESET} — credentials stored in {CREDS_FILE}")
    _invalidate_status_cache()


def cmd_logout(_args) -> None:
    creds = _load_creds()
    # Legacy PATs were revocable server-side; rotating tokens just get dropped locally.
    if creds.get("token_id") and creds.get("token"):
        try:
            request("DELETE", f"/api/auth/tokens/{creds['token_id']}")
            print("Token revoked on the server.")
        except SystemExit:
            print("Could not revoke remotely — removing local credentials anyway.", file=sys.stderr)
    clear_creds()
    print("Logged out.")


def cmd_whoami(_args) -> None:
    u = request("GET", "/api/auth/me")
    print(f"{BOLD}{u['display_name']}{RESET} (@{u['username']}) — {u['email']}")
    print(f"{DIM}publisher: {'yes' if u['is_publisher'] else 'no'} · api: {api_url()}{RESET}")


def cmd_orgs(_args) -> None:
    for o in request("GET", "/api/orgs"):
        print(f"{BOLD}{o['name']}{RESET}  {DIM}{o['slug']} · {o['my_role']} · {o['plan']}{RESET}")


def cmd_tokens(_args) -> None:
    for t in request("GET", "/api/auth/tokens"):
        last = str(t["last_used_at"] or "never")[:19]
        print(f"{t['token_prefix']}…  {BOLD}{t['name']}{RESET}  {DIM}[{t['id']}] last used {last}{RESET}")


def cmd_search(args) -> None:
    params = {"q": args.query, "sort": args.sort}
    if args.type:
        params["type"] = args.type.upper()
    if args.tag:
        params["tag"] = args.tag
    print_resources(request("GET", "/api/public/resources", params=params, auth=False))


def attachments_md(r) -> str:
    """Render typed attachments as a portable '## Resources' markdown section
    (hybrid content model — content_md stays canonical, this appends media)."""
    items = r.get("attachments") or []
    if not items:
        return ""
    lines = ["", "## Resources", ""]
    for a in items:
        title = a.get("title") or a.get("url")
        cap = f" — {a['caption']}" if a.get("caption") else ""
        lines.append(f"- **{a.get('kind', 'link')}**: [{title}]({a['url']}){cap}")
    return "\n".join(lines) + "\n"


def _download(url: str, dest: Path) -> bool:
    try:
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as res:
            if res.status_code >= 400:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in res.iter_bytes():
                    fh.write(chunk)
        return True
    except httpx.HTTPError:
        return False


def pull_attachments(r: dict, target: Path, fetch_assets: bool) -> str:
    """Like attachments_md, but for `pull`: optionally download image/video/file
    attachments into target/attachments/ and reference the local copies."""
    items = r.get("attachments") or []
    if not items:
        return ""
    lines = ["", "## Resources", ""]
    for a in items:
        kind, url = a.get("kind", "link"), a["url"]
        title = a.get("title") or url
        cap = f" — {a['caption']}" if a.get("caption") else ""
        if fetch_assets and kind in ("image", "video", "file"):
            name = re.sub(r"[^A-Za-z0-9._-]", "-", url.split("/")[-1].split("?")[0]) or f"{kind}-asset"
            if _download(url, target / "attachments" / name):
                print(f"{DIM}  ↓ attachments/{name}{RESET}")
                lines.append(f"- **{kind}**: [{title}](attachments/{name}){cap}")
                continue
        lines.append(f"- **{kind}**: [{title}]({url}){cap}")
    return "\n".join(lines) + "\n"


def cmd_get(args) -> None:
    path = f"/api/resources/{args.id}" if token() else f"/api/public/resources/{args.id}"
    r = request("GET", path, auth=bool(token()))
    author = (r.get("author") or {}).get("username", "?")
    print(f"{GLYPH.get(r['type'], '·')} {BOLD}{r['title']}{RESET} v{r['version']}  {DIM}{r['type']} · @{author} · ★{r['stars_count']}{RESET}")
    print(f"{DIM}{r['description']}{RESET}\n")
    if r["type"] == "MCP":
        if r.get("server_url"):
            print(f"{BOLD}server url:{RESET} {r['server_url']}")
        if r.get("config_json"):
            print(f"{BOLD}config:{RESET}\n{r['config_json']}")
        if r.get("content_md"):
            print(f"\n{r['content_md']}")
    else:
        print(r.get("content_md") or "(no content)")
    section = attachments_md(r)
    if section:
        print(section)


def cmd_use(args) -> None:
    """Print a resource's raw content — no decorations, nothing written to disk —
    so it can be piped straight into an agent (e.g. `skillshare use <id> | claude`).
    Skills/notes emit their markdown body; MCP servers emit their config JSON."""
    path = f"/api/resources/{args.id}" if token() else f"/api/public/resources/{args.id}"
    r = request("GET", path, auth=bool(token()))
    if r["type"] == "MCP":
        sys.stdout.write(r.get("config_json") or "{}")
    else:
        body = r.get("content_md") or ""
        section = attachments_md(r)
        sys.stdout.write(body + (section or ""))
    sys.stdout.write("\n")


def cmd_pull(args) -> None:
    path = f"/api/resources/{args.id}" if token() else f"/api/public/resources/{args.id}"
    r = request("GET", path, auth=bool(token()))
    out = Path(args.output).expanduser()
    slug = re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-")
    target = out / slug
    target.mkdir(parents=True, exist_ok=True)
    assets = not args.no_assets
    section = pull_attachments(r, target, assets)
    if r["type"] == "SKILL":
        (target / "SKILL.md").write_text((r.get("content_md") or "") + section)
        print(f"wrote {target / 'SKILL.md'}")
        if r.get("file_url"):
            name = re.sub(r"[^A-Za-z0-9._-]", "-", r["file_url"].split("/")[-1].split("?")[0]) or "package"
            if assets and _download(r["file_url"], target / name):
                print(f"{DIM}  ↓ {name}{RESET}")
            else:
                print(f"{DIM}package: {r['file_url']}{RESET}")
    elif r["type"] == "MCP":
        (target / "mcp-config.json").write_text(r.get("config_json") or "{}")
        print(f"wrote {target / 'mcp-config.json'}")
        if r.get("content_md") or r.get("attachments"):
            (target / "README.md").write_text((r.get("content_md") or "") + section)
            print(f"wrote {target / 'README.md'}")
    else:
        (target / f"{slug}.md").write_text((r.get("content_md") or "") + section)
        print(f"wrote {target / (slug + '.md')}")


CLAUDE_SKILLS_DIR = Path("~/.claude/skills").expanduser()
INSTALL_DIR = Path("~/.config/skillshare/installed").expanduser()


def cmd_install(args) -> None:
    """Install a resource into local tools — the toast 'Add' action. Skills land
    in Claude Code's skills dir so they're usable immediately; MCP configs and
    notes are saved under ~/.config/skillshare/installed. Optionally marks the
    source notification read."""
    r = request("GET", f"/api/resources/{args.id}")
    slug = re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-") or "resource"
    if r["type"] == "SKILL":
        target = CLAUDE_SKILLS_DIR / slug
        target.mkdir(parents=True, exist_ok=True)
        section = pull_attachments(r, target, True)
        dest = target / "SKILL.md"
        dest.write_text((r.get("content_md") or "") + section)
    elif r["type"] == "MCP":
        target = INSTALL_DIR / "mcp" / slug
        target.mkdir(parents=True, exist_ok=True)
        dest = target / "mcp-config.json"
        dest.write_text(r.get("config_json") or "{}")
        if r.get("content_md"):
            (target / "README.md").write_text(r["content_md"])
    else:
        target = INSTALL_DIR / "notes"
        target.mkdir(parents=True, exist_ok=True)
        dest = target / f"{slug}.md"
        dest.write_text(r.get("content_md") or "")
    print(f"installed {BOLD}{r['title']}{RESET} → {dest}")
    _invalidate_status_cache()  # the install gap just shrank — reflect it now
    if getattr(args, "notification", None):
        try:
            request("POST", "/api/notifications/read", body={"ids": [args.notification]})
        except SystemExit:
            pass


def cmd_read(args) -> None:
    request("POST", "/api/notifications/read", body={"ids": args.ids})
    print(f"{DIM}marked {len(args.ids)} notification(s) read{RESET}")


def cmd_list(args) -> None:
    params = {}
    if args.query:
        params["q"] = args.query
    if args.type:
        params["type"] = args.type.upper()
    if args.pod:
        params["scope"] = args.scope
        rows = request("GET", f"/api/pods/{args.pod}/resources", params=params)
    elif args.org:
        rows = request("GET", f"/api/orgs/{args.org}/resources", params=params)
    else:
        die("provide --org <slug> or --pod <id>")
    print_resources(rows)


def cmd_add_note(args) -> None:
    content = Path(args.file).read_text() if args.file else sys.stdin.read()
    if args.pod:
        scope = {"scope_type": "POD", "scope_id": args.pod}
    elif args.org:
        org = request("GET", f"/api/orgs/{args.org}")
        scope = {"scope_type": "ORG", "scope_id": org["id"]}
    else:
        die("provide --org <slug> or --pod <id>")
    # --link is always a URL; --image/--video/--attach accept a local file
    # (uploaded automatically) OR a URL.
    attachments = (
        [{"kind": "link", "url": u} for u in (args.link or [])]
        + [_resolve_attachment(v, "image") for v in (args.image or [])]
        + [_resolve_attachment(v, "video") for v in (args.video or [])]
        + [_resolve_attachment(v, "file") for v in (args.attach or [])]
    )
    r = request(
        "POST",
        "/api/resources",
        body={
            "type": "NOTE",
            "title": args.title,
            "description": args.description or "",
            "content_md": content,
            "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
            "attachments": attachments,
            **scope,
        },
    )
    extra = f" with {len(attachments)} attachment(s)" if attachments else ""
    print(f"created {BOLD}{r['title']}{RESET} [{r['id']}] in {r['scope_type'].lower()} scope{extra}")


def cmd_upload(args) -> None:
    up = upload_file(args.file, args.kind)
    print(up["file_url"])


def cmd_star(args) -> None:
    r = request("POST", f"/api/resources/{args.id}/star")
    print(f"★ starred {BOLD}{r['title']}{RESET} ({r['stars_count']} stars)")


def cmd_pin(args) -> None:
    r = request("POST", f"/api/resources/{args.id}/pin")
    print(f"📌 pinned {BOLD}{r['title']}{RESET} to your profile")


def cmd_unpin(args) -> None:
    r = request("DELETE", f"/api/resources/{args.id}/pin")
    print(f"unpinned {BOLD}{r['title']}{RESET}")


def cmd_pins(_args) -> None:
    me = request("GET", "/api/auth/me")
    print_resources(request("GET", f"/api/users/{me['username']}/pinned"))


def cmd_avatar(args) -> None:
    """Upload a local image and set it as the profile picture."""
    p = Path(args.file).expanduser()
    if not p.is_file():
        die(f"file not found: {args.file}")
    t = token()
    if not t:
        die("not logged in — run `skillshare login` (or set SKILLSHARE_TOKEN)")
    try:
        with p.open("rb") as fh:
            res = httpx.post(
                f"{api_url()}/api/auth/me/avatar",
                headers={"Authorization": f"Bearer {t}"},
                files={"file": (p.name, fh)},
                timeout=120,
            )
    except httpx.HTTPError as e:
        die(f"cannot reach {api_url()} ({e.__class__.__name__})")
    if res.status_code >= 400:
        try:
            die(f"{res.json()['error']['code']}: {res.json()['error']['message']}")
        except (KeyError, ValueError):
            die(f"HTTP {res.status_code}: {res.text[:200]}")
    print(f"updated profile photo → {res.json().get('avatar_url')}")


def cmd_org_logo(args) -> None:
    """Upload a local image and set it as an org's logo (admins only)."""
    p = Path(args.file).expanduser()
    if not p.is_file():
        die(f"file not found: {args.file}")
    t = token()
    if not t:
        die("not logged in — run `skillshare login` (or set SKILLSHARE_TOKEN)")
    try:
        with p.open("rb") as fh:
            res = httpx.post(
                f"{api_url()}/api/orgs/{args.org}/logo",
                headers={"Authorization": f"Bearer {t}"},
                files={"file": (p.name, fh)},
                timeout=120,
            )
    except httpx.HTTPError as e:
        die(f"cannot reach {api_url()} ({e.__class__.__name__})")
    if res.status_code >= 400:
        try:
            die(f"{res.json()['error']['code']}: {res.json()['error']['message']}")
        except (KeyError, ValueError):
            die(f"HTTP {res.status_code}: {res.text[:200]}")
    print(f"updated {BOLD}{args.org}{RESET} logo → {res.json().get('logo_url')}")


def cmd_publish(args) -> None:
    r = request("POST", f"/api/resources/{args.id}/publish")
    print(f"🌐 published {BOLD}{r['title']}{RESET} to the public marketplace")


def cmd_import(args) -> None:
    r = request("POST", f"/api/resources/{args.id}/import", body={"org_slug": args.org})
    print(f"imported {BOLD}{r['title']}{RESET} into {args.org} [{r['id']}]")


def cmd_follow(args) -> None:
    res = request("POST", f"/api/public/publishers/{args.username}/follow")
    print(f"following {BOLD}@{args.username}{RESET} — you'll be notified on new releases ({res['followers_count']} followers)")


def cmd_unfollow(args) -> None:
    res = request("DELETE", f"/api/public/publishers/{args.username}/follow")
    print(f"unfollowed {BOLD}@{args.username}{RESET} ({res['followers_count']} followers)")


def cmd_follow_org(args) -> None:
    res = request("POST", f"/api/orgs/{args.org}/follow")
    print(f"following org {BOLD}{args.org}{RESET} — notified on new releases ({res['followers_count']} followers)")


def cmd_unfollow_org(args) -> None:
    res = request("DELETE", f"/api/orgs/{args.org}/follow")
    print(f"unfollowed org {BOLD}{args.org}{RESET} ({res['followers_count']} followers)")


_VERB_GLYPH = {"created": "✦", "published": "🌐"}


def cmd_inbox(args) -> None:
    """Show notifications for resources added/published in scopes you belong to
    (F1). Each carries a ready-to-run install command."""
    params = {"limit": args.limit}
    if args.unread:
        params["unread_only"] = "true"
    rows = request("GET", "/api/notifications", params=params)
    if not rows:
        print(f"{DIM}inbox empty{RESET}")
    else:
        for n in rows:
            actor = (n.get("actor") or {}).get("username", "?")
            when = str(n.get("created_at") or "")[:16].replace("T", " ")
            dot = "" if n.get("read_at") else f"{BOLD}●{RESET} "
            glyph = _VERB_GLYPH.get(n["verb"], "·")
            print(f"{dot}{glyph} {BOLD}{n['object_title']}{RESET} {DIM}{n['verb']} by @{actor} · {when}{RESET}")
            if n.get("install_command"):
                print(f"    {DIM}$ {n['install_command']}{RESET}")
    if args.mark_read and rows:
        request("POST", "/api/notifications/read", body={"all": True})
        print(f"{DIM}— marked {len(rows)} read{RESET}")


KIND_GLYPH = {"skill": "◆", "mcp": "▣", "note": "▤"}
STATUS_TAG = {"new": f"{BOLD}NEW{RESET}", "pushed": f"{DIM}pushed{RESET}", "dismissed": f"{DIM}dismissed{RESET}"}


def _scan_rows(args):
    """Scan local sources and reconcile against the server's provenance state."""
    sources = args.source or list(DETECTORS)
    cands = scan(sources=sources, notes_dir=args.notes_dir)
    try:
        state = request("GET", "/api/local-state")
    except SystemExit:
        state = []
    return reconcile(cands, state)


def cmd_scan(args) -> None:
    rows = _scan_rows(args)
    if not args.include_dismissed:
        rows = [r for r in rows if r["status"] != "dismissed"]
    if args.json:
        print(json.dumps([
            {"fingerprint": r["fingerprint"], "status": r["status"], "kind": r["candidate"].kind,
             "name": r["candidate"].name, "source": r["candidate"].source, "title": r["candidate"].title,
             "path": r["candidate"].path, "resource_id": r["resource_id"]}
            for r in rows
        ], indent=2))
        return
    if not rows:
        print(f"{DIM}no local skills / MCP servers / notes found{RESET}")
        return
    for r in rows:
        c = r["candidate"]
        _, findings = redacted_payload(c)
        sec = f" {DIM}· {len(findings)} secret(s) will be redacted{RESET}" if findings else ""
        print(f"{KIND_GLYPH.get(c.kind, '·')} {BOLD}{c.title}{RESET} {STATUS_TAG.get(r['status'], r['status'])}"
              f"  {DIM}{c.kind} · {c.source} · {r['fingerprint'][:14]}{RESET}{sec}")
        print(f"    {DIM}{c.path}{RESET}")
    n_new = sum(1 for r in rows if r["status"] == "new")
    if n_new:
        print(f"\n{n_new} new — push with {BOLD}skillshare push --org <slug>{RESET} (or --pod <id>)")


def _resolve_push_scope(args) -> dict:
    if args.pod:
        return {"scope_type": "POD", "scope_id": args.pod}
    if args.org:
        org = request("GET", f"/api/orgs/{args.org}")
        return {"scope_type": "ORG", "scope_id": org["id"]}
    die("provide --org <slug> or --pod <id> as the push target")


def cmd_push(args) -> None:
    scope = _resolve_push_scope(args)
    rows = _scan_rows(args)
    targets = [r for r in rows if r["status"] == "new"]
    if args.fingerprint:
        targets = [r for r in rows if r["fingerprint"].startswith(args.fingerprint)]
        if not targets:
            die(f"no candidate matching fingerprint {args.fingerprint}")
    if not targets:
        print(f"{DIM}nothing new to push (everything is already pushed or dismissed){RESET}")
        return
    for r in targets:
        c = r["candidate"]
        payload, findings = redacted_payload(c)
        print(f"\n{KIND_GLYPH.get(c.kind, '·')} {BOLD}{c.title}{RESET} {DIM}({c.kind} from {c.source}){RESET}")
        if findings:
            print(f"  {BOLD}⚠ {len(findings)} secret(s) redacted before upload:{RESET} {DIM}{', '.join(findings)}{RESET}")
        if not args.yes:
            ans = input(f"  push to {scope['scope_type'].lower()} {args.org or args.pod}? [y/N/d=dismiss] ").strip().lower()
            if ans == "d":
                request("POST", "/api/local-state", body={
                    "fingerprint": r["fingerprint"], "kind": c.kind, "status": "dismissed",
                    "source": c.source, "name": c.name})
                print(f"  {DIM}dismissed — won't ask again{RESET}")
                continue
            if ans != "y":
                print(f"  {DIM}skipped (will ask again next scan){RESET}")
                continue
        created = request("POST", "/api/resources", body={**payload, **scope})
        request("POST", "/api/local-state", body={
            "fingerprint": r["fingerprint"], "kind": c.kind, "status": "pushed",
            "source": c.source, "resource_id": created["id"], "name": c.name})
        print(f"  {BOLD}✓ pushed{RESET} [{created['id']}]")
        _invalidate_status_cache()  # one fewer local artifact awaiting push


def cmd_github(args) -> None:
    """Import skill(s)/MCP(s)/note(s) from a public GitHub repo. Detection runs
    server-side; secrets in any MCP config are redacted before import."""
    preview = request("POST", "/api/resources/github/preview", body={"url": args.url})
    repo = preview.get("repo", {})
    detected = preview.get("detected", [])
    header = f"{BOLD}{repo.get('owner', '?')}/{repo.get('repo', '?')}{RESET}"
    if repo.get("description"):
        header += f"  {DIM}{repo['description'][:80]}{RESET}"
    print(header)
    if not detected:
        print(f"{DIM}no skills, MCP servers, or notes detected in that repository{RESET}")
        return
    for d in detected:
        sec = f" {DIM}· {len(d['redaction'])} secret(s) redacted{RESET}" if d.get("redaction") else ""
        print(f"{KIND_GLYPH.get(d['kind'], '·')} {BOLD}{d['title']}{RESET}"
              f"  {DIM}{d['kind']} · {d['source_path']}{RESET}{sec}")
        if d.get("description"):
            print(f"    {d['description'][:100]}")
    if args.dry_run:
        print(f"\n{len(detected)} detected — import with "
              f"{BOLD}skillshare github <url> --org <slug>{RESET} (or --pod <id>)")
        return

    scope = _resolve_push_scope(args)
    if args.yes:
        select: list[str] = []  # empty = import everything detected
    else:
        select = []
        for d in detected:
            ans = input(f"  import {BOLD}{d['title']}{RESET} ({d['kind']})? [Y/n] ").strip().lower()
            if ans in ("", "y", "yes"):
                select.append(d["fingerprint"])
        if not select:
            print(f"{DIM}nothing selected{RESET}")
            return

    result = request("POST", "/api/resources/github/import",
                     body={"url": args.url, "select": select, **scope})
    for r in result.get("created", []):
        print(f"  {BOLD}✓ imported{RESET} {GLYPH.get(r['type'], '·')} {r['title']}  [{r['id']}]")
    for s in result.get("skipped", []):
        print(f"  {DIM}↷ skipped {s['title']} — {s['reason']} [{s.get('resource_id', '')}]{RESET}")
    if result.get("created"):
        _invalidate_status_cache()


def _primary_org_slug() -> str:
    """The org a 'Push' CTA targets by default: first one you administer, else
    your first membership. Empty string if you belong to none."""
    try:
        orgs = request("GET", "/api/orgs") or []
    except SystemExit:
        return ""
    if not orgs:
        return ""
    admin = next((o for o in orgs if (o.get("role") or "").upper() == "ADMIN"), None)
    return (admin or orgs[0]).get("slug", "")


def cmd_watch(args) -> None:
    """Poll the inbox + local artifacts and pop a themed desktop card for each
    new item. Runs in the foreground; use --once for a cron/systemd timer."""
    import time

    from .notify import announce_inbox, announce_shareable

    interval = max(10, args.interval)
    target_org = "" if args.no_scan else _primary_org_slug()
    scope = "inbox" if args.no_scan else "inbox + local artifacts"
    mode = "single poll" if args.once else f"every {interval}s"
    print(f"{BOLD}skillshare watch{RESET} — {scope}, {mode}"
          + (f" {DIM}(push → {target_org}){RESET}" if target_org else "")
          + f". {DIM}Ctrl-C to stop.{RESET}")
    first = True
    try:
        while True:
            try:
                items = request("GET", "/api/notifications", params={"unread_only": "true", "limit": 30})
            except SystemExit:
                items = None  # transient API blip — don't kill the watcher
            n_in = announce_inbox(items or [], seed_silently=first and not args.notify_existing)
            n_share = 0
            if not args.no_scan:
                n_share = announce_shareable(_scan_rows(args), target_org)
            if n_in or n_share:
                print(f"{DIM}· popped {n_in} inbox + {n_share} shareable card(s){RESET}")
            if args.once:
                break
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _index_from_rows(rows) -> tuple[set, dict]:
    """From local scan rows, the set of local fingerprints + per-kind set of local
    names/slugs — used to decide whether a platform resource is already local."""
    fps, names = set(), {"skill": set(), "mcp": set(), "note": set()}
    for r in rows:
        c = r["candidate"]
        fps.add(r["fingerprint"])
        if c.kind in names:
            names[c.kind].add(_slugify(c.name or ""))
            names[c.kind].add(_slugify(c.title or ""))
    return fps, names


def _is_local(r: dict, fps: set, names: dict) -> bool:
    """Is this platform resource already set up on this machine? Matches by
    push-provenance (source_fingerprint), local name/slug, or install path."""
    kind, slug = r["type"].lower(), _slugify(r["title"])
    if r.get("source_fingerprint") and r["source_fingerprint"] in fps:
        return True
    if kind in names and slug in names[kind]:
        return True
    if kind == "skill" and (CLAUDE_SKILLS_DIR / slug).exists():
        return True
    if kind == "note" and (INSTALL_DIR / "notes" / f"{slug}.md").exists():
        return True
    if kind == "mcp" and (INSTALL_DIR / "mcp" / slug).exists():
        return True
    return False


def _available_resources(args) -> list:
    """Resources visible to me on SkillShare that I don't have locally yet."""
    try:
        resources = request("GET", "/api/auth/me/resources")
    except SystemExit:
        return []
    try:
        rows = _scan_rows(args)
    except SystemExit:
        rows = []
    fps, names = _index_from_rows(rows)
    return [r for r in resources if not _is_local(r, fps, names)]


def _panel_item(r: dict) -> dict:
    return {
        "id": r["id"], "kind": r["type"].lower(), "title": r["title"],
        "description": r.get("description") or "", "version": r.get("version"),
        "author": (r.get("author") or {}).get("username"),
        "scope": (r.get("scope_type") or "").lower(),
        "scope_name": r.get("scope_name"),
        "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
        "tags": r.get("tags") or [], "stars": r.get("stars_count", 0),
    }


def _compute_status_segment(args) -> str:
    """The SkillShare part of the status line: how many skills/MCP/notes are
    available to install (on SkillShare, in my scopes, not yet local) — plus
    local artifacts not pushed yet. Network/scan happen here, so it's cached."""
    try:
        resources = request("GET", "/api/auth/me/resources")
    except SystemExit:
        return f"{DIM}◆ skillshare login{RESET}"
    try:
        rows = _scan_rows(args)
    except SystemExit:
        rows = []
    fps, names = _index_from_rows(rows)
    inst = {"skill": 0, "mcp": 0, "note": 0}
    for r in resources:
        k = r["type"].lower()
        if k in inst and not _is_local(r, fps, names):
            inst[k] += 1
    pend = {"skill": 0, "mcp": 0, "note": 0}
    for r in rows:
        if r["status"] == "new" and r["candidate"].kind in pend:
            pend[r["candidate"].kind] += 1
    sep = f"{DIM} · {RESET}"
    seg = f"{VIOLET}◆ SkillShare{RESET}"
    inst_bits = [f"{KIND_FG[k]}{KIND_MARK[k]} {n} {k}{RESET}" for k, n in inst.items() if n]
    seg += ("  ↓ " + sep.join(inst_bits) + f" {DIM}to install{RESET}") if inst_bits else f"  {DIM}all caught up{RESET}"
    push_bits = [f"{KIND_FG[k]}{KIND_MARK[k]} {n} {k}{RESET}" for k, n in pend.items() if n]
    if push_bits:
        seg += f"  {DIM}│{RESET}  ⬆ " + sep.join(push_bits) + f" {DIM}to push{RESET}"
    return seg


def _status_segment(args) -> str:
    import time
    now = time.time()
    cache = _read_json(STATUS_CACHE)
    if not args.refresh and cache and now - cache.get("ts", 0) < args.max_age:
        return cache.get("seg", "")
    if not args.refresh and cache:
        # Stale: serve the cached line instantly, refresh in the background.
        try:
            import subprocess
            subprocess.Popen([sys.argv[0], "status", "--refresh"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        return cache.get("seg", "")
    seg = _compute_status_segment(args)
    try:
        STATUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_CACHE.write_text(json.dumps({"ts": now, "seg": seg}))
    except OSError:
        pass
    return seg


def cmd_status(args) -> None:
    """Render a one-line status for the AI tool's status bar. Claude Code passes
    its session context (model, cwd) as JSON on stdin; we keep that prefix fresh
    and append the (cached) SkillShare segment."""
    ctx = {}
    if not sys.stdin.isatty():
        try:
            ctx = json.load(sys.stdin)
        except (ValueError, OSError):
            ctx = {}
    seg = _status_segment(args)
    if args.refresh:
        return  # background refresh just repopulated the cache; nothing to print
    prefix_bits = []
    cwd = (ctx.get("workspace") or {}).get("current_dir") or ctx.get("cwd")
    if cwd:
        prefix_bits.append(f"{DIM}{Path(cwd).name}{RESET}")
    model = (ctx.get("model") or {}).get("display_name")
    if model:
        prefix_bits.append(f"{DIM}{model}{RESET}")
    prefix = f" {DIM}·{RESET} ".join(prefix_bits)
    if prefix and seg:
        print(f"{prefix}  {DIM}│{RESET}  {seg}")
    else:
        print(seg or prefix)


def cmd_setup_statusline(args) -> None:
    """Wire the SkillShare status line into Claude Code's settings.json so the
    install/push counts show in the status bar (one-time; the data then refreshes
    automatically). Only Claude Code renders a status line."""
    from . import statusline as sl

    if args.print_only:
        print(sl.status_command())
        return
    scope = "project" if args.project else "user"
    try:
        res = sl.disable(scope=scope, force=args.force) if args.remove else sl.enable(scope=scope, force=args.force)
    except sl.StatusLineError as e:
        die(str(e))

    status = res["status"]
    path = res["path"]
    if status == "enabled":
        print(f"{BOLD}✓ Status line enabled{RESET} in {path}")
        print(f"  {DIM}command: {res['command']}{RESET}")
        print(f"  {DIM}Restart Claude Code (or reload) to see it.{RESET}")
    elif status == "already-enabled":
        print(f"{DIM}Status line already enabled in {path} — nothing to do.{RESET}")
    elif status == "exists-different":
        print(f"{BOLD}A different statusLine is already set{RESET} in {path}:")
        print(f"  {DIM}{res['existing']}{RESET}")
        print(f"  Re-run with {BOLD}--force{RESET} to replace it.")
    elif status == "removed":
        print(f"{BOLD}✓ Status line removed{RESET} from {path}. Restart Claude Code to apply.")
    elif status == "not-ours":
        print(f"The statusLine in {path} isn't SkillShare's — left it alone. Use {BOLD}--force{RESET} to remove anyway.")
    else:  # not-set / no-settings
        print(f"{DIM}No SkillShare status line to remove ({status}).{RESET}")


def cmd_available(args) -> None:
    """List resources on SkillShare (in your scopes) that aren't set up locally
    yet. `--json` feeds the panel; otherwise prints a grouped, installable list."""
    items = _available_resources(args)
    if args.json:
        print(json.dumps([_panel_item(r) for r in items], indent=2))
        return
    if not items:
        print(f"{DIM}you're all caught up — nothing new to install{RESET}")
        return
    by: dict = {"SKILL": [], "MCP": [], "NOTE": []}
    for r in items:
        by.setdefault(r["type"], []).append(r)
    for t, label in (("SKILL", "Skills"), ("MCP", "MCP servers"), ("NOTE", "Notes")):
        group = by.get(t) or []
        if not group:
            continue
        print(f"\n{BOLD}{label}{RESET} {DIM}({len(group)}){RESET}")
        for r in group:
            author = (r.get("author") or {}).get("username", "?")
            print(f"  {GLYPH.get(t, '·')} {BOLD}{r['title']}{RESET} {DIM}v{r['version']} · @{author} · "
                  f"{r['scope_type'].lower()}{RESET}  [{r['id']}]")
            if r.get("description"):
                print(f"      {DIM}{r['description'][:90]}{RESET}")
    print(f"\n{DIM}install one:{RESET} skillshare install <id>   {DIM}· open the panel:{RESET} skillshare panel")


def cmd_panel(args) -> None:
    """Open the clickable install panel (expandable rows, one-click install).
    Falls back to the text list if no GUI is available."""
    items = [_panel_item(r) for r in _available_resources(args)]
    from .notify import open_panel
    if not open_panel(items):
        print(f"{DIM}(no desktop GUI available — showing the list instead){RESET}")
        args.json = False
        cmd_available(args)


def cmd_dismiss(args) -> None:
    request("POST", "/api/local-state", body={
        "fingerprint": args.fingerprint, "kind": args.kind, "status": "dismissed",
        "source": args.source or "", "name": args.name or ""})
    print(f"{DIM}dismissed {args.fingerprint} — won't be recommended again{RESET}")


_FEEDBACK_CATEGORIES = ["bug", "idea", "praise", "question", "other", "general"]


def cmd_feedback(args) -> None:
    """Send product feedback to the SkillShare team — a rating and a message,
    optionally about a specific resource. Prompts for anything not passed as a
    flag so `skillshare feedback` on its own is fully interactive."""
    message = args.message
    if not message:
        if sys.stdin.isatty():
            print(f"{DIM}Your feedback (what's working, what's not, ideas):{RESET}")
            message = input("> ").strip()
        else:
            message = sys.stdin.read().strip()
    if not message:
        die("feedback message is required (pass -m \"...\" or pipe it in)")

    rating = args.rating
    if rating is None and not args.yes and sys.stdin.isatty():
        ans = input("Rating 1-5 (Enter to skip): ").strip()
        if ans:
            try:
                rating = int(ans)
            except ValueError:
                die("rating must be a number 1-5")
    if rating is not None and not (1 <= rating <= 5):
        die("rating must be between 1 and 5")

    body = {
        "message": message,
        "category": args.category,
        "source": "cli",
        "context": {"cwd": os.getcwd()},
    }
    if rating is not None:
        body["rating"] = rating
    if args.resource:
        body["target_type"] = "resource"
        body["target_id"] = args.resource

    fb = request("POST", "/api/feedback", body=body)
    stars = ("★" * fb["rating"] + "☆" * (5 - fb["rating"])) if fb.get("rating") else ""
    where = f" on {fb['target_id']}" if fb.get("target_type") == "resource" else ""
    print(f"{BOLD}✓ thanks for the feedback{RESET}{where}  {DIM}{stars} · {fb['category']} · [{fb['id']}]{RESET}")


def cmd_feedback_list(_args) -> None:
    """Show the feedback you've submitted."""
    rows = request("GET", "/api/feedback/mine")
    if not rows:
        print(f"{DIM}you haven't sent any feedback yet{RESET}")
        return
    for f in rows:
        when = str(f.get("created_at") or "")[:16].replace("T", " ")
        stars = ("★" * f["rating"]) if f.get("rating") else f"{DIM}—{RESET}"
        tag = f"{DIM}reviewed{RESET}" if f["status"] == "reviewed" else f"{BOLD}open{RESET}"
        print(f"{stars} {BOLD}{f['category']}{RESET} {tag} {DIM}· {when}{RESET}")
        print(f"    {f['message'][:120]}")


def main() -> None:
    p = argparse.ArgumentParser(prog="skillshare", description="SkillShare registry CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("login", help="sign in via the browser (device flow) and store credentials")
    sp.add_argument("--api", help=f"API base URL (default {DEFAULT_API})")
    sp.add_argument("--no-browser", action="store_true", help="don't auto-open the browser; just print the URL")
    sp.add_argument("--email", help="legacy: log in directly with email/password instead of the browser")
    sp.add_argument("--password", help="legacy: password for --email login (omit to be prompted)")
    sp.set_defaults(fn=cmd_login)

    sub.add_parser("logout", help="revoke the stored token and remove credentials").set_defaults(fn=cmd_logout)
    sub.add_parser("whoami", help="show the authenticated user").set_defaults(fn=cmd_whoami)
    sub.add_parser("orgs", help="list my organizations").set_defaults(fn=cmd_orgs)
    sub.add_parser("tokens", help="list my personal access tokens").set_defaults(fn=cmd_tokens)

    sp = sub.add_parser("search", help="search the public marketplace")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--type", choices=["skill", "mcp", "note", "SKILL", "MCP", "NOTE"])
    sp.add_argument("--tag")
    sp.add_argument("--sort", default="stars", choices=["stars", "installs", "newest", "updated"])
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("get", help="show a resource's full content")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_get)

    sp = sub.add_parser("use", help="print a skill/note's raw content for piping into an agent (no local install)")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_use)

    sp = sub.add_parser("pull", help="save a resource locally (SKILL.md / mcp-config.json / note.md + attachments)")
    sp.add_argument("id")
    sp.add_argument("-o", "--output", default=".", help="output directory (default .)")
    sp.add_argument("--no-assets", action="store_true", help="don't download attachment files; keep URLs")
    sp.set_defaults(fn=cmd_pull)

    sp = sub.add_parser("upload", help="upload a file and print its stored URL")
    sp.add_argument("file")
    sp.add_argument("--kind", default="file", choices=["image", "video", "file"])
    sp.set_defaults(fn=cmd_upload)

    sp = sub.add_parser("list", help="list org or pod resources (incl. inherited)")
    sp.add_argument("--org", help="org slug")
    sp.add_argument("--pod", help="pod id")
    sp.add_argument("--scope", default="all", choices=["all", "pod", "project", "org"])
    sp.add_argument("--type", choices=["skill", "mcp", "note", "SKILL", "MCP", "NOTE"])
    sp.add_argument("-q", "--query", default="")
    sp.set_defaults(fn=cmd_list)

    add = sub.add_parser("add", help="create a resource").add_subparsers(dest="kind", required=True)
    sp = add.add_parser("note", help="create a NOTE from a file or stdin")
    sp.add_argument("--title", required=True)
    sp.add_argument("--org", help="org slug (org-level note)")
    sp.add_argument("--pod", help="pod id (pod-level note)")
    sp.add_argument("-f", "--file", help="markdown file (default: read stdin)")
    sp.add_argument("--description")
    sp.add_argument("--tags", help="comma-separated")
    sp.add_argument("--link", action="append", help="attach a URL (repeatable)")
    sp.add_argument("--image", action="append", help="attach an image — local file (uploaded) or URL (repeatable)")
    sp.add_argument("--video", action="append", help="attach a video — local file (uploaded) or URL (repeatable)")
    sp.add_argument("--attach", action="append", help="attach a file — local file (uploaded) or URL (repeatable)")
    sp.set_defaults(fn=cmd_add_note)

    sp = sub.add_parser("inbox", help="notifications: resources added/published in your scopes")
    sp.add_argument("--unread", action="store_true", help="only unread")
    sp.add_argument("--mark-read", action="store_true", help="mark everything read after listing")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_inbox)

    def _scan_flags(parser):
        parser.add_argument("--source", action="append", choices=list(DETECTORS),
                            help="limit to a source (repeatable); default = all")
        parser.add_argument("--notes-dir", help="also scan this directory's *.md as notes")

    sp = sub.add_parser("scan", help="find local skills/MCP/notes and see which aren't on the platform yet")
    _scan_flags(sp)
    sp.add_argument("--include-dismissed", action="store_true", help="also show ones you dismissed")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("push", help="push local artifacts to the platform (secrets redacted, with preview)")
    _scan_flags(sp)
    sp.add_argument("--org", help="target org slug")
    sp.add_argument("--pod", help="target pod id")
    sp.add_argument("--fingerprint", help="push only the candidate with this fingerprint (prefix ok)")
    sp.add_argument("--yes", action="store_true", help="don't prompt for confirmation")
    sp.set_defaults(fn=cmd_push)

    sp = sub.add_parser("github", help="import skills/MCP/notes from a public GitHub repo URL")
    sp.add_argument("url", help="github.com/owner/repo (optionally /tree/<ref>/<subpath>)")
    sp.add_argument("--org", help="target org slug")
    sp.add_argument("--pod", help="target pod id")
    sp.add_argument("--dry-run", action="store_true", help="just show what was detected; import nothing")
    sp.add_argument("--yes", action="store_true", help="import everything detected without prompting")
    sp.set_defaults(fn=cmd_github)

    sp = sub.add_parser("status", help="one-line status for an AI tool's status bar (install gap + local to-push)")
    _scan_flags(sp)
    sp.add_argument("--max-age", type=int, default=60, help="seconds to cache the counts (default 60)")
    sp.add_argument("--refresh", action="store_true", help="recompute the cache without printing (internal)")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("setup-statusline", help="show the install/push counts in Claude Code's status bar")
    sp.add_argument("--project", action="store_true", help="write to ./.claude/settings.json (default: ~/.claude/settings.json)")
    sp.add_argument("--remove", action="store_true", help="remove the SkillShare status line")
    sp.add_argument("--force", action="store_true", help="replace an existing different statusLine (or remove a non-SkillShare one)")
    sp.add_argument("--print", dest="print_only", action="store_true", help="just print the command string; change nothing")
    sp.set_defaults(fn=cmd_setup_statusline)

    sp = sub.add_parser("available", help="list resources on SkillShare (your scopes) not set up locally yet")
    _scan_flags(sp)
    sp.add_argument("--json", action="store_true", help="machine-readable output (used by the panel)")
    sp.set_defaults(fn=cmd_available)

    sp = sub.add_parser("panel", help="open the clickable install panel (expandable rows, one-click install)")
    _scan_flags(sp)
    sp.set_defaults(fn=cmd_panel)

    sp = sub.add_parser("install", help="install a resource into local tools (the toast 'Add' action)")
    sp.add_argument("id")
    sp.add_argument("--notification", help="mark this notification id read after installing")
    sp.set_defaults(fn=cmd_install)

    sp = sub.add_parser("read", help="mark notification id(s) read (the toast 'Reject' action)")
    sp.add_argument("ids", nargs="+")
    sp.set_defaults(fn=cmd_read)

    sp = sub.add_parser("watch", help="pop desktop notifications for new inbox items + shareable local artifacts")
    _scan_flags(sp)
    sp.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    sp.add_argument("--once", action="store_true", help="run a single poll and exit (for cron/systemd timer)")
    sp.add_argument("--no-scan", action="store_true", help="only watch the inbox; skip the local-artifact scan")
    sp.add_argument("--notify-existing", action="store_true", help="also toast items already unread at startup")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("dismiss", help="remember not to recommend a local artifact (by fingerprint)")
    sp.add_argument("fingerprint")
    sp.add_argument("--kind", default="skill", choices=["skill", "mcp", "note"])
    sp.add_argument("--source", default="")
    sp.add_argument("--name", default="")
    sp.set_defaults(fn=cmd_dismiss)

    sp = sub.add_parser("feedback", help="send product feedback (rating + message), optionally about a resource")
    sp.add_argument("-m", "--message", help="the feedback text (omit to be prompted / read stdin)")
    sp.add_argument("-r", "--rating", type=int, help="rating 1-5 (optional)")
    sp.add_argument("-c", "--category", default="general", choices=_FEEDBACK_CATEGORIES,
                    help="what kind of feedback (default general)")
    sp.add_argument("--resource", help="resource id this feedback is about (omit for general/platform)")
    sp.add_argument("--yes", action="store_true", help="don't prompt for a rating")
    sp.set_defaults(fn=cmd_feedback)

    sub.add_parser("feedback-list", help="list the feedback you've submitted").set_defaults(fn=cmd_feedback_list)

    sp = sub.add_parser("star", help="star a resource")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_star)

    sp = sub.add_parser("pin", help="pin a resource to your profile (max 6)")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_pin)

    sp = sub.add_parser("unpin", help="remove a resource from your pinned set")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_unpin)

    sub.add_parser("pins", help="list the resources pinned to your profile").set_defaults(fn=cmd_pins)

    sp = sub.add_parser("avatar", help="upload an image and set it as your profile photo")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_avatar)

    sp = sub.add_parser("org-logo", help="upload an image and set it as an org's logo (admin)")
    sp.add_argument("org", help="org slug")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_org_logo)

    sp = sub.add_parser("publish", help="publish a resource to the marketplace")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_publish)

    sp = sub.add_parser("import", help="copy a public resource into your org")
    sp.add_argument("id")
    sp.add_argument("--org", required=True, help="target org slug")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("follow", help="follow a publisher (get notified on new releases)")
    sp.add_argument("username")
    sp.set_defaults(fn=cmd_follow)

    sp = sub.add_parser("unfollow", help="unfollow a publisher")
    sp.add_argument("username")
    sp.set_defaults(fn=cmd_unfollow)

    sp = sub.add_parser("follow-org", help="follow (watch) an org for release notifications")
    sp.add_argument("org", help="org slug")
    sp.set_defaults(fn=cmd_follow_org)

    sp = sub.add_parser("unfollow-org", help="unfollow an org")
    sp.add_argument("org", help="org slug")
    sp.set_defaults(fn=cmd_unfollow_org)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
