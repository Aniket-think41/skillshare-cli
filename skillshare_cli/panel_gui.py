#!/usr/bin/env python3
"""SkillShare install panel — built from libhandy components (GTK3).

Reads a JSON list of available resources from stdin and shows them grouped by
kind. Each resource is a Handy.ExpanderRow inside a Handy.PreferencesGroup —
ready-made components that get the spacing, title/subtitle, expand affordance,
and suffix-action layout right, so the Install button never collides with the
edges. Expanding a row reveals the description, scope, date, author and tags.
Install shells out to the `skillshare` CLI.

Run under a Python with PyGObject + libhandy (system python3). Launched by
`skillshare panel`; items arrive on stdin (avoids argv limits).
"""

import json
import os
import shutil
import subprocess
import sys

os.environ.setdefault("GDK_BACKEND", "x11")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Handy", "1")
from gi.repository import Gtk, Handy  # noqa: E402

KIND_LETTER = {"skill": "S", "mcp": "M", "note": "N"}
SECTIONS = [("skill", "Skills"), ("mcp", "MCP servers"), ("note", "Notes")]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SKILLSHARE = shutil.which("skillshare") or os.path.expanduser("~/.local/bin/skillshare")

CSS = b"""
.badge { border-radius: 9px; min-width: 30px; min-height: 30px; }
.badge label { color: #ffffff; font-weight: 800; font-size: 11pt; }
.badge-skill { background-color: #735ae5; }
.badge-mcp   { background-color: #2f6df0; }
.badge-note  { background-color: #e2552b; }
.btn-skill { background-image: none; background-color: #735ae5; color: #fff; border: none; border-radius: 9px; font-weight: 700; padding: 5px 16px; }
.btn-skill:hover { background-color: #5a41d6; }
.btn-mcp { background-image: none; background-color: #2f6df0; color: #fff; border: none; border-radius: 9px; font-weight: 700; padding: 5px 16px; }
.btn-mcp:hover { background-color: #2557c9; }
.btn-note { background-image: none; background-color: #e2552b; color: #fff; border: none; border-radius: 9px; font-weight: 700; padding: 5px 16px; }
.btn-note:hover { background-color: #c0461f; }
.btn-done { background-image: none; background-color: #f1f0fb; color: #5a41d6; border: 1px solid #e9e9f0; border-radius: 9px; font-weight: 700; padding: 5px 16px; }
.ss-desc { color: #50504c; font-size: 10.5pt; }
.ss-tag  { color: #5a41d6; background-color: #efeafe; border-radius: 7px; padding: 2px 9px; font-size: 8.5pt; }
.ss-empty { color: #8a8d94; font-size: 12pt; }
"""


def _fmt_date(iso):
    if not iso:
        return ""
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return "%s %d, %d" % (_MONTHS[m - 1], d, y)
    except (ValueError, IndexError):
        return ""


def _badge(kind):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    ctx = box.get_style_context()
    ctx.add_class("badge")
    ctx.add_class("badge-%s" % kind)
    box.set_size_request(30, 30)
    box.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label(label=KIND_LETTER.get(kind, "?"))
    lbl.set_halign(Gtk.Align.CENTER)
    lbl.set_valign(Gtk.Align.CENTER)
    box.pack_start(lbl, True, True, 0)
    return box


def _install(button, item):
    try:
        subprocess.Popen([SKILLSHARE, "install", item["id"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    button.set_label("Installed ✓")
    button.set_sensitive(False)
    ctx = button.get_style_context()
    for c in ("btn-skill", "btn-mcp", "btn-note"):
        ctx.remove_class(c)
    ctx.add_class("btn-done")


def _detail_body(item):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_margin_top(8)
    box.set_margin_bottom(14)
    box.set_margin_start(16)
    box.set_margin_end(16)
    desc = (item.get("description") or "").strip() or "No description provided."
    d = Gtk.Label(label=desc, xalign=0)
    d.get_style_context().add_class("ss-desc")
    d.set_line_wrap(True)
    box.pack_start(d, False, False, 0)
    if item.get("tags"):
        tags = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for tag in item["tags"][:6]:
            tl = Gtk.Label(label=tag)
            tl.get_style_context().add_class("ss-tag")
            tags.pack_start(tl, False, False, 0)
        box.pack_start(tags, False, False, 0)
    return box


def _row(item):
    kind = item.get("kind", "skill")
    row = Handy.ExpanderRow()
    row.set_title(item.get("title", ""))

    bits = []
    if item.get("scope_name") or item.get("scope"):
        bits.append(item.get("scope_name") or item.get("scope"))
    date = _fmt_date(item.get("created_at"))
    if date:
        bits.append("added %s" % date)
    if item.get("author"):
        bits.append("@%s" % item["author"])
    if item.get("version"):
        bits.append("v%s" % item["version"])
    row.set_subtitle("  ·  ".join(bits))

    row.add_prefix(_badge(kind))

    install = Gtk.Button(label="Install")
    install.get_style_context().add_class("btn-%s" % kind)
    install.set_valign(Gtk.Align.CENTER)
    install.connect("clicked", _install, item)
    row.add_action(install)

    row.add(_detail_body(item))
    return row


def build(items):
    Handy.init()
    win = Gtk.Window()
    win.set_default_size(540, 680)
    win.connect("destroy", Gtk.main_quit)

    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(win.get_screen(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    header = Gtk.HeaderBar()
    header.set_show_close_button(True)
    header.set_title("SkillShare")
    header.set_subtitle("Available to install — %d in your orgs, projects & teams" % len(items)
                        if items else "You're all caught up")
    win.set_titlebar(header)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    win.add(scroll)

    clamp = Handy.Clamp()
    clamp.set_margin_top(18)
    clamp.set_margin_bottom(18)
    clamp.set_margin_start(12)
    clamp.set_margin_end(12)
    scroll.add(clamp)

    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    clamp.add(column)

    if not items:
        empty = Gtk.Label(label="✓ Nothing new to install.")
        empty.get_style_context().add_class("ss-empty")
        empty.set_margin_top(60)
        column.pack_start(empty, False, False, 0)
    else:
        for kind, label in SECTIONS:
            group_items = [it for it in items if it.get("kind") == kind]
            if not group_items:
                continue
            group = Handy.PreferencesGroup()
            group.set_title(label)
            group.set_description("%d available" % len(group_items))
            for it in group_items:
                group.add(_row(it))
            column.pack_start(group, False, False, 0)

    win.show_all()
    try:
        w, h = win.get_size()
        geo = win.get_display().get_primary_monitor().get_geometry()
        win.move(geo.x + geo.width - w - 30, geo.y + 60)
    except Exception:
        pass


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else "[]"
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        items = []
    build(items)
    Gtk.main()


if __name__ == "__main__":
    main()
