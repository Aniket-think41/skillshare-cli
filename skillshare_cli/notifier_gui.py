#!/usr/bin/env python3
"""Themed SkillShare notification card (GTK3 + libhandy, bottom-right).

A standalone GUI — run as::

    python3 notifier_gui.py '<json-card-spec>'

Must run under a Python with PyGObject + libhandy (the system python3), so
`skillshare watch` and the MCP server spawn it as a subprocess. Self-contained:
each CTA shells out to the `skillshare` CLI.

Design: a flat, lightly translucent card with soft rounded corners and a
diffuse shadow (no hard border, no internal divider lines — just spacing),
a per-kind accent color used sparingly (avatar fill + a small flat label),
a Handy.Avatar for the person who shared it, and pill-shaped flat buttons.
The window must stay GTK3 (libhandy) because GTK4 can't self-position to
the bottom-right corner.

Card spec (JSON)::

    {"type":"inbound"|"local", "kind":"skill"|"mcp"|"note",
     "badge":"New skill", "title":"...", "description":"...",
     "details":[["Type","Skill"],["Pod","Retrieval Pod"],["Shared by","@priya"]],
     "avatar":"priya",
     "resource_id":"res-...", "notification_id":"ntf-...",   # inbound
     "fingerprint":"fp_...", "target_org":"think41",          # local
     "offset":0, "timeout":30}

CTAs: inbound → Add / Dismiss · local → Push / Later.
"""

import json
import os
import shutil
import subprocess
import sys

os.environ.setdefault("GDK_BACKEND", "x11")  # XWayland lets us self-position bottom-right

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Handy", "1")
from gi.repository import Gdk, GLib, Gtk, Handy, Pango  # noqa: E402

try:
    gi.require_foreign("cairo")
    import cairo
    HAVE_CAIRO = True
except Exception:  # pragma: no cover
    cairo = None
    HAVE_CAIRO = False

# --- website theme (src/app/globals.css) ---
THEME = {
    "bg": "#ffffff", "ink": "#141413", "body": "#50504c", "muted": "#8a8d94",
    "line": "#e9e9f0", "surface": "#f7f9fb",
    "skill": "#735ae5", "mcp": "#2f6df0", "note": "#e2552b",
}
SHADOW_PAD = 30  # transparent margin around the card so the drop-shadow has room
SKILLSHARE = shutil.which("skillshare") or os.path.expanduser("~/.local/bin/skillshare")


def _cli(args):
    try:
        subprocess.Popen([SKILLSHARE, *args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _mix(hex_color, other, f):
    """Mix hex_color toward `other` by fraction f (0..1)."""
    a = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


def _darken(hex_color, f=0.14):
    return _mix(hex_color, "#000000", f)


def _rgba(hex_color, alpha):
    """Convert a hex color into an rgba(...) string for translucent fills."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "rgba(%d,%d,%d,%.2f)" % (r, g, b, alpha)


def _css(accent, rounded):
    if rounded:
        # Compositor available: flat, near-opaque, NO border —
        # just one soft diffuse shadow to lift it off the desktop.
        card_bg = _rgba("#ffffff", 0.985)
        card_border = ""
        shadow = ("box-shadow: 0 1px 2px rgba(20,16,40,0.05), "
                  "0 16px 38px rgba(20,16,40,0.18);")
        radius = 18
    else:
        # No alpha compositing available — fall back to a plain opaque panel.
        card_bg = THEME["bg"]
        card_border = "border: 1px solid %s;" % THEME["line"]
        shadow = ""
        radius = 0

    data = dict(
        THEME, accent=accent, accent700=_darken(accent),
        radius=radius, card_bg=card_bg, card_border=card_border, shadow=shadow,
        accent_soft=_mix(accent, "#ffffff", 0.86),   # solid light tint for the badge
        ghost_bg="#eceaf1", ghost_bg_hover="#e0dde8",  # solid grey for Dismiss/Later
    )
    return ("""
    * { font-family: "Inter", "Ubuntu", "Cantarell", sans-serif; }
    .ss-card { background-color: %(card_bg)s; border-radius: %(radius)dpx;
               padding: 15px 18px; %(card_border)s %(shadow)s }
    .ss-title { color: %(ink)s; font-weight: 700; font-size: 12pt; }
    .ss-badge { color: %(accent)s; background-color: %(accent_soft)s; font-weight: 700;
                font-size: 7pt; letter-spacing: 0.5px; border-radius: 999px;
                padding: 2px 9px; }
    .ss-desc  { color: %(body)s; font-size: 9pt; }
    .ss-meta  { color: %(muted)s; font-size: 8pt; }
    avatar { background-color: %(accent)s; color: #ffffff; font-weight: 800; }
    .ss-primary { background-image: none; background-color: %(accent)s; color: #ffffff;
                  border: none; border-radius: 999px; font-weight: 700; font-size: 8.5pt;
                  padding: 6px 16px; }
    .ss-primary:hover { background-color: %(accent700)s; }
    .ss-ghost { background-image: none; background-color: %(ghost_bg)s; color: %(body)s;
                border: none; border-radius: 999px; font-weight: 600; font-size: 8.5pt;
                padding: 6px 14px; }
    .ss-ghost:hover { background-color: %(ghost_bg_hover)s; }
    .ss-close { background: none; border: none; box-shadow: none; color: %(muted)s;
                padding: 0; font-size: 9pt; }
    .ss-close:hover { color: %(ink)s; }
    """ % data).encode()


class Card(Gtk.Window):
    def __init__(self, card):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.card = card
        kind = (card.get("kind") or "skill").lower()
        accent = THEME.get(kind, THEME["skill"])

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(360 + 2 * SHADOW_PAD, -1)

        screen = self.get_screen()
        rounded = False
        if HAVE_CAIRO:
            vis = screen.get_rgba_visual()
            if vis is not None:
                self.set_visual(vis)
                self.set_app_paintable(True)
                self.connect("draw", self._clear_bg)
                rounded = True

        prov = Gtk.CssProvider()
        prov.load_from_data(_css(accent, rounded))
        Gtk.StyleContext.add_provider_for_screen(screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card_box.get_style_context().add_class("ss-card")  # padding via CSS
        pad = SHADOW_PAD if rounded else 0
        for setter in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(card_box, setter)(pad)
        self.add(card_box)

        # Horizontal layout: avatar in a left column, everything else in a right
        # content column. Wide-and-short instead of a tall, boxy stack.
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        card_box.pack_start(body, True, True, 0)

        avatar = Handy.Avatar.new(38, str(card.get("avatar") or card.get("title") or "?"), True)
        avatar.set_valign(Gtk.Align.START)
        body.pack_start(avatar, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.pack_start(content, True, True, 0)

        # top line: badge pill on the left, close ✕ pinned to the right
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if card.get("badge"):
            badge_wrap = Gtk.Box()  # hug the text instead of stretching across
            badge = Gtk.Label(label=card["badge"])
            badge.get_style_context().add_class("ss-badge")
            badge.set_valign(Gtk.Align.CENTER)
            badge_wrap.pack_start(badge, False, False, 0)
            badge_wrap.set_valign(Gtk.Align.CENTER)
            top.pack_start(badge_wrap, False, False, 0)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("ss-close")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_valign(Gtk.Align.START)
        close.connect("clicked", lambda *_: self._dismiss())
        top.pack_end(close, False, False, 0)
        content.pack_start(top, False, False, 0)

        title = Gtk.Label(label=card.get("title", ""), xalign=0)
        title.get_style_context().add_class("ss-title")
        title.set_line_wrap(True)
        title.set_max_width_chars(30)
        title.set_margin_top(5)
        content.pack_start(title, False, False, 0)

        desc = (card.get("description") or "").strip()
        if desc:
            if len(desc) > 120:
                desc = desc[:117].rstrip() + "…"
            d = Gtk.Label(label=desc, xalign=0)
            d.get_style_context().add_class("ss-desc")
            d.set_line_wrap(True)
            d.set_max_width_chars(44)
            d.set_margin_top(4)
            content.pack_start(d, False, False, 0)

        # details collapse into a single muted meta line (the values, joined) —
        # keeps the card horizontal instead of a tall label/value table.
        details = card.get("details") or []
        meta_text = "  ·  ".join(str(v) for _, v in details if str(v).strip())
        if meta_text:
            m = Gtk.Label(label=meta_text, xalign=0)
            m.get_style_context().add_class("ss-meta")
            m.set_line_wrap(True)
            m.set_max_width_chars(46)
            m.set_margin_top(8)
            content.pack_start(m, False, False, 0)

        self.btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btnrow.set_margin_top(12)
        prim, ghost = ("Push", "Later") if card.get("type") == "local" else ("Add", "Dismiss")
        pb = Gtk.Button(label=prim)
        pb.get_style_context().add_class("ss-primary")
        pb.connect("clicked", lambda *_: self._primary())
        gb = Gtk.Button(label=ghost)
        gb.get_style_context().add_class("ss-ghost")
        gb.connect("clicked", lambda *_: self._ghost())
        self.btnrow.pack_end(pb, False, False, 0)
        self.btnrow.pack_end(gb, False, False, 0)
        content.pack_start(self.btnrow, False, False, 0)

        self.status = Gtk.Label(label="", xalign=0)
        self.status.get_style_context().add_class("ss-meta")
        self.status.set_margin_top(12)
        content.pack_start(self.status, False, False, 0)

        self.connect("destroy", Gtk.main_quit)
        GLib.timeout_add_seconds(int(card.get("timeout", 30)), self._on_timeout)

    # --- behaviour ---
    def _clear_bg(self, _w, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        return False

    def _primary(self):
        c = self.card
        if c.get("type") == "local":
            _cli(["push", "--fingerprint", c.get("fingerprint", ""),
                  "--org", c.get("target_org", ""), "--yes"])
            self._confirm("Pushing to %s…" % (c.get("target_org") or "your org"))
        else:
            args = ["install", c.get("resource_id", "")]
            if c.get("notification_id"):
                args += ["--notification", c["notification_id"]]
            _cli(args)
            self._confirm("Adding to your local tools…")

    def _ghost(self):
        c = self.card
        if c.get("type") != "local" and c.get("notification_id"):
            _cli(["read", c["notification_id"]])  # Dismiss = mark read
        self._dismiss()

    def _confirm(self, text):
        self.btnrow.hide()
        self.status.set_text("✓ " + text)
        self.status.show()
        GLib.timeout_add(1400, self._dismiss)

    def _on_timeout(self):
        self._dismiss()
        return False

    def _dismiss(self, *_):
        try:
            self.destroy()
        except Exception:
            Gtk.main_quit()
        return False

    def place(self):
        self.show_all()
        self.status.hide()
        w, h = self.get_size()
        display = self.get_display()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        edge = 8
        offset = int(self.card.get("offset", 0))
        x = geo.x + geo.width - w - edge
        y = geo.y + geo.height - h - edge - offset * (h - SHADOW_PAD)
        self.move(x, y)


def main():
    if len(sys.argv) < 2:
        return
    try:
        card = json.loads(sys.argv[1])
    except ValueError:
        return
    Handy.init()
    Card(card).place()
    Gtk.main()


if __name__ == "__main__":
    main()
