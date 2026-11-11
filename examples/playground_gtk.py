#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small GTK 3 window full of things a pointer has to cope with.

Used for the live desktop tests and as something to point ``cec`` at:

* a menu bar with a nested submenu (hover-safe routing),
* a slider and a check box (value- and state-closed loops),
* a button that keeps relocating itself (targets that move mid-flight),
* a button that summons a window on top of the others (occlusion),
* a text entry.

Everything that happens is appended to the status line at the bottom, and,
when ``--log FILE`` is given, to that file as one JSON object per line.
"""

from __future__ import annotations

import argparse
import json
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


class Playground(Gtk.Window):
    def __init__(self, log_path: str | None) -> None:
        super().__init__(title="CEC Playground")
        self.set_default_size(760, 520)
        self.set_resizable(False)
        self._log = open(log_path, "a", buffering=1) if log_path else None
        self._hop = 0
        self._hopping: int | None = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self._menus(), False, False, 0)

        self.fixed = Gtk.Fixed()
        root.pack_start(self.fixed, True, True, 0)

        self.export = Gtk.Button(label="Export")
        self.export.connect("clicked", lambda *_: self.note("clicked", "Export"))
        self.fixed.put(self.export, 40, 30)

        self.wrap = Gtk.CheckButton(label="Wrap lines")
        self.wrap.connect("toggled", lambda b: self.note("toggled", "Wrap lines", active=b.get_active()))
        self.fixed.put(self.wrap, 40, 90)

        label = Gtk.Label(label="Volume")
        self.fixed.put(label, 40, 150)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_size_request(420, -1)
        self.volume.set_value(20)
        self.volume.set_draw_value(True)
        self.volume.get_accessible().set_name("Volume")
        self.volume.connect("value-changed", lambda s: self.note("value", "Volume", value=s.get_value()))
        self.fixed.put(self.volume, 110, 135)

        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Search")
        self.search.get_accessible().set_name("Search")
        self.search.set_size_request(260, -1)
        self.search.connect("changed", lambda e: self.note("text", "Search", text=e.get_text()))
        self.search.connect("activate", lambda e: self.note("submitted", "Search", text=e.get_text()))
        self.fixed.put(self.search, 40, 210)

        self.hopper = Gtk.Button(label="Catch me")
        self.hopper.connect("clicked", lambda *_: self.note("clicked", "Catch me"))
        self.fixed.put(self.hopper, 40, 290)

        hop = Gtk.ToggleButton(label="Start hopping")
        hop.connect("toggled", self._toggle_hopping)
        self.fixed.put(hop, 40, 400)

        cover = Gtk.Button(label="Cover soon")
        cover.connect("clicked", self._cover_soon)
        self.fixed.put(cover, 200, 400)

        self.guarded = Gtk.Button(label="Guarded")
        self.guarded.connect("clicked", lambda *_: self.note("clicked", "Guarded"))
        self.fixed.put(self.guarded, 560, 400)

        self.status = Gtk.Label(label="ready")
        self.status.set_xalign(0)
        self.status.get_accessible().set_name("Status")
        root.pack_end(self.status, False, False, 6)

        self.connect("destroy", Gtk.main_quit)

    def _menus(self) -> Gtk.MenuBar:
        bar = Gtk.MenuBar()
        for title, entries in (
            ("File", ["New", "Open", ("Export as", ["PNG image", "SVG drawing", "PDF document"]), "Print", "Close"]),
            ("Edit", ["Undo", "Redo", "Preferences"]),
        ):
            top = Gtk.MenuItem(label=title)
            top.set_submenu(self._menu(entries))
            bar.append(top)
        return bar

    def _menu(self, entries: list) -> Gtk.Menu:
        menu = Gtk.Menu()
        for entry in entries:
            if isinstance(entry, tuple):
                item = Gtk.MenuItem(label=entry[0])
                item.set_submenu(self._menu(entry[1]))
            else:
                item = Gtk.MenuItem(label=entry)
                item.connect("activate", lambda _i, name=entry: self.note("activated", name))
            menu.append(item)
        menu.show_all()
        return menu

    def note(self, kind: str, name: str, **data: object) -> None:
        self.status.set_text(f"{kind}: {name} {data if data else ''}".strip())
        if self._log:
            self._log.write(json.dumps({"t": time.time(), "kind": kind, "name": name, **data}) + "\n")

    def _toggle_hopping(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._hopping = GLib.timeout_add(700, self._hop_once)
        elif self._hopping is not None:
            GLib.source_remove(self._hopping)
            self._hopping = None

    def _hop_once(self) -> bool:
        spots = [(40, 290), (430, 250), (560, 320), (250, 330), (600, 220)]
        self._hop = (self._hop + 1) % len(spots)
        self.fixed.move(self.hopper, *spots[self._hop])
        return True

    def _cover_soon(self, *_: object) -> None:
        """After a short delay, put a window over the ``Guarded`` button for a while."""

        def show() -> bool:
            cover = Gtk.Window(type=Gtk.WindowType.POPUP)
            cover.set_default_size(260, 140)
            cover.add(Gtk.Label(label="I am in the way"))
            ox, oy = self.get_window().get_origin()[1:]
            alloc = self.guarded.get_allocation()
            cover.move(ox + alloc.x - 80, oy + alloc.y - 50)
            cover.show_all()
            self.note("covered", "Guarded")
            GLib.timeout_add(1500, lambda: (cover.destroy(), self.note("uncovered", "Guarded"))[0] and False)
            return False

        GLib.timeout_add(500, show)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", help="append events to this file as JSON lines")
    ap.add_argument("--at", default="560,180", help="window position as X,Y")
    args = ap.parse_args()
    win = Playground(args.log)
    x, y = (int(v) for v in args.at.split(","))
    win.move(x, y)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
