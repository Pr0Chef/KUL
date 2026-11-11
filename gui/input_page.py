"""The parts every page of the Inputs tab shares: how it loads, shows unsaved changes, reverts and saves.

A page is one entry in the Inputs sidebar. The Inputs tab (inputs_tab.InputsTab) owns the design's state and calls
the page at these moments:
  load()        the config was read (again); self.tab.effective holds every variable as make resolves it.
  forget()      another config was picked: drop unsaved edits.
  settings_changed()  a flow setting changed elsewhere (on another page); show the new pending values.
  is_dirty()    whether the page has unsaved edits; the app does not run while any page has them.
  save(), revert()    the page's Save and Revert buttons.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

MUTED = "#8a8f98"
WARN = "#c88a00"
ERROR = "#c0392b"


def markup(text, color):
    return f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>'


def hint_label(text=""):
    """A grey, wrapping label for explanations under a field or a section."""
    label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
    label.set_markup(markup(text, MUTED))
    return label


def section(text):
    label = Gtk.Label(label=text, xalign=0)
    label.get_style_context().add_class("section-title")
    return label


def scrolled(child):
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.add(child)
    return sw


class InputPage:
    name = ""   # id in the Inputs stack
    title = ""  # label in the Inputs sidebar

    def __init__(self, tab):
        self.tab = tab
        self.widget = self.build()

    # ---- what a page provides ----
    def build(self):
        raise NotImplementedError

    def load(self):
        pass

    def forget(self):
        pass

    def settings_changed(self):
        pass

    def is_dirty(self):
        return False

    def save(self):
        pass

    def revert(self):
        self.forget()
        self.load()
        self.tab.dirty()

    # ---- shared widgets ----
    def footer(self, hint=""):
        """The bottom row: a hint, the Saved / Unsaved state, Revert and Save."""
        box = Gtk.Box(spacing=8, margin_top=4)
        box.pack_start(hint_label(hint), True, True, 0)
        self.state = Gtk.Label()
        self.revert_btn = Gtk.Button(label="Revert", tooltip_text="Throw away the unsaved changes on this page")
        self.revert_btn.connect("clicked", lambda *_: self.revert())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save())
        for w in (self.state, self.revert_btn, self.save_btn):
            box.pack_start(w, False, False, 0)
        return box

    def update_buttons(self, idle, why):
        """Called by the Inputs tab whenever the unsaved state or the run state changes."""
        if not hasattr(self, "save_btn"):
            return
        dirty = self.is_dirty()
        self.save_btn.set_sensitive(dirty and idle)
        self.save_btn.set_tooltip_text(why)
        self.revert_btn.set_sensitive(dirty)
        self.state.set_markup(markup("Unsaved changes", WARN) if dirty else markup("Saved", MUTED))
