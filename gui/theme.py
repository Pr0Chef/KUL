"""The app's look: one palette, light and dark, applied as CSS on top of GTK's own Adwaita theme.

Adwaita is used as the base on every desktop, so the app looks the same on Ubuntu (Yaru), Fedora or KDE. The
palette is a small set of named colors; the CSS below only refers to those names. Dark mode follows the
desktop's preference; ORFS_RUNNER_THEME=light or dark overrides it.
"""

import os

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

PALETTES = {
    "light": {
        "bg": "#f3f5f8", "surface": "#ffffff", "surface_alt": "#f8f9fb", "border": "#dfe3ea",
        "text": "#1c2230", "text_muted": "#6b7385", "hover": "#eef1f6", "code_bg": "#f4f6fa",
        "accent": "#3d5afe", "accent_text": "#ffffff", "accent_soft": "#e8ecff",
        "success": "#1e8e5a", "warning": "#b7791f", "danger": "#d64545", "info": "#2f6fd6",
        "header_bg": "#1b2130", "header_text": "#f2f4f8",
    },
    "dark": {
        "bg": "#171a21", "surface": "#1f232c", "surface_alt": "#1b1f27", "border": "#2e3440",
        "text": "#e6e9ef", "text_muted": "#9aa3b5", "hover": "#272c37", "code_bg": "#1a1e26",
        "accent": "#6c84ff", "accent_text": "#ffffff", "accent_soft": "#262d4a",
        "success": "#3ccf8e", "warning": "#e3b04b", "danger": "#ff6b6b", "info": "#6aa4ff",
        "header_bg": "#11141a", "header_text": "#e6e9ef",
    },
}

CSS = """
window, .background { background-color: @bg; color: @text; }
label { color: inherit; }

/* Header bar: dark, flat, with the app's mark and name. */
headerbar { background: @header_bg; border: none; box-shadow: none; min-height: 48px; padding: 0 10px; }
headerbar .title { color: @header_text; font-weight: 700; }
headerbar .subtitle { color: alpha(@header_text, 0.6); }
headerbar .app-name { color: @header_text; font-weight: 700; font-size: 11pt; }
headerbar .app-path { color: alpha(@header_text, 0.6); font-size: 8.5pt; }
headerbar button { background: alpha(@header_text, 0.07); color: @header_text;
        border: 1px solid alpha(@header_text, 0.14);
                   box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; border-radius: 6px;
        padding: 4px 12px; }
headerbar button:hover { background: alpha(@header_text, 0.16); }
headerbar button label { color: @header_text; }
headerbar button.titlebutton { background: transparent; border: none; padding: 4px; }
headerbar separator { background: alpha(@header_text, 0.18); min-width: 1px; margin: 12px 6px; }

/* Buttons, entries and lists. */
button { border-radius: 6px; padding: 5px 12px; background-image: none; background-color: @surface; color: @text;
         border: 1px solid @border; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; }
button:hover { background-color: @hover; }
button:active, button:checked { background-color: shade(@hover, 0.96); }
button:disabled { background-color: @surface_alt; color: alpha(@text_muted, 0.7); border-color: alpha(@border, 0.7); }
button.suggested-action { background-color: @accent; color: @accent_text; border-color: @accent; font-weight: 600; }
button.suggested-action label { color: @accent_text; }
button.suggested-action:hover { background-color: shade(@accent, 1.1); }
button.suggested-action:disabled { background-color: alpha(@accent, 0.3); border-color: transparent; }
button.suggested-action:disabled label { color: alpha(@accent_text, 0.55); }
button.destructive-action { background-color: transparent; color: @danger; border-color: alpha(@danger, 0.55); }
button.destructive-action label { color: @danger; }
button.destructive-action:hover { background-color: alpha(@danger, 0.08); }
button.destructive-action:disabled { border-color: @border; }
button.destructive-action:disabled label { color: alpha(@text_muted, 0.7); }
entry, spinbutton:not(.vertical) { background-color: @surface; color: @text; border: 1px solid @border;
        border-radius: 6px;
        box-shadow: none; min-height: 30px; padding: 0 8px; }
entry:focus, spinbutton:focus { border-color: @accent; box-shadow: 0 0 0 2px alpha(@accent, 0.22); }
entry.error { border-color: @danger; color: @danger; }
spinbutton entry { border: none; box-shadow: none; }
spinbutton button { border: none; border-left: 1px solid @border; border-radius: 0; }
combobox button.combo { min-height: 30px; }
checkbutton check, radiobutton radio { background-image: none; background-color: @surface;
        border: 1px solid shade(@border, 0.9); }
checkbutton check:checked, radiobutton radio:checked { background-color: @accent; border-color: @accent;
        color: @accent_text; }
*:selected, *:selected:focus { background-color: @accent_soft; color: @text; }
textview text { background-color: @surface; color: @text; }
textview.monospace text, .code { background-color: @code_bg; }
scrolledwindow undershoot, scrolledwindow overshoot { background: none; }
tooltip { background-color: @header_bg; color: @header_text; border-radius: 6px; }
tooltip * { color: @header_text; }

/* Tables. */
treeview.view { background-color: @surface; color: @text; }
treeview.view:hover { background-color: @hover; }
treeview.view:selected { background-color: @accent_soft; color: @text; }
treeview.view header button { background-color: @surface_alt; color: @text_muted; font-weight: 700; font-size: 9pt;
        border: none; border-bottom: 1px solid @border; border-right: 1px solid alpha(@border, 0.6); border-radius: 0;
        padding: 6px 8px; }

/* The run panel on the left. */
.side { background-color: @surface_alt; border-right: 1px solid @border; padding: 18px 16px; }
.side-label { color: @text_muted; font-weight: 600; }
.eyebrow { color: @text_muted; font-size: 8pt; font-weight: 700; letter-spacing: 1px; margin-top: 10px; }
.plan { font-family: monospace; font-size: 9pt; padding: 10px 12px; border-radius: 8px; background-color: @code_bg;
        border: 1px solid @border; color: @text; }
.stage-name { font-weight: 600; }
.stage-meta { color: @text_muted; font-size: 9pt; }

/* Main tabs: flat, with an accent underline. */
notebook { background-color: @bg; border: none; }
notebook > header { background-color: @surface; border-bottom: 1px solid @border; box-shadow: none; padding: 0 8px; }
notebook > header tab { padding: 11px 16px; border: none; background: transparent; box-shadow: none; min-height: 0; }
notebook > header tab label { color: @text_muted; font-weight: 600; }
notebook > header tab:hover label { color: @text; }
notebook > header tab:checked { box-shadow: inset 0 -2px @accent; }
notebook > header tab:checked label { color: @accent; }
notebook > stack { background-color: @bg; }

/* Inputs: the page list on the left, then a page with a header and cards. */
stacksidebar { background-color: @surface; border-right: 1px solid @border; }
stacksidebar list { background-color: @surface; }
stacksidebar row { padding: 9px 16px; border: none; color: @text; }
stacksidebar row label { color: @text; }
stacksidebar row:hover { background-color: @hover; }
stacksidebar row:selected { background-color: @accent_soft; box-shadow: inset 3px 0 @accent; }
stacksidebar row:selected label { color: @accent; font-weight: 600; }
.inputs-head { background-color: @surface; border-bottom: 1px solid @border; padding: 10px 16px; }
.page-title { font-size: 15pt; font-weight: 700; color: @text; }
.page-subtitle { color: @text_muted; }
.card { background-color: @surface; border: 1px solid @border; border-radius: 10px; padding: 16px 18px; }
.card-title, .section-title { font-weight: 700; font-size: 10.5pt; color: @text; margin-bottom: 4px; }
.muted { color: @text_muted; }
.action-bar { background-color: @surface; border-top: 1px solid @border; padding: 10px 16px; }
.preview { background-color: @surface; border: 1px solid @border; border-radius: 10px; }

/* Reports: a card per sign-off check. */
.tile { padding: 12px 14px; margin: 4px; border-left: 3px solid @border; }
.tile-pass { border-left-color: @success; }
.tile-fail { border-left-color: @danger; }
.tile-warn { border-left-color: @warning; }
.tile-name { color: @text_muted; font-size: 9pt; font-weight: 600; }
.tile-value { font-size: 16pt; font-weight: 700; color: @text; }
.chip { font-size: 8pt; font-weight: 700; padding: 1px 8px; border-radius: 10px; }
.chip-pass { background-color: alpha(@success, 0.14); color: @success; }
.chip-fail { background-color: alpha(@danger, 0.14); color: @danger; }
.chip-warn { background-color: alpha(@warning, 0.16); color: @warning; }
.chip-muted { background-color: alpha(@text_muted, 0.14); color: @text_muted; }
flowboxchild { padding: 0; }

/* Status bar. */
.statusbar { background-color: @surface; border-top: 1px solid @border; color: @text_muted; padding: 6px 14px;
        font-size: 9pt; }
infobar { border: none; }
paned > separator { background-image: none; background-color: @border; min-width: 1px; min-height: 1px; }
paned.wide > separator { background-color: @border; }
"""

_mode = "light"


def mode():
    """'light' or 'dark', as applied."""
    return _mode


def color(name):
    """A palette color by name, for markup and drawing."""
    return PALETTES[_mode][name]


def wants_dark(settings):
    forced = os.environ.get("ORFS_RUNNER_THEME", "").lower()
    if forced in ("light", "dark"):
        return forced == "dark"
    name = (settings.get_property("gtk-theme-name") or "").lower()
    return bool(settings.get_property("gtk-application-prefer-dark-theme")) or name.endswith("dark")


def apply():
    """Use Adwaita (light or dark, following the desktop) with the app's palette on top. Call once, early."""
    global _mode
    settings = Gtk.Settings.get_default()
    _mode = "dark" if wants_dark(settings) else "light"
    settings.set_property("gtk-theme-name", "Adwaita")
    settings.set_property("gtk-application-prefer-dark-theme", _mode == "dark")
    names = "".join(f"@define-color {k} {v};\n" for k, v in PALETTES[_mode].items())
    # Adwaita draws selections and focus with these; pointing them at the palette keeps one accent everywhere.
    names += "@define-color theme_selected_bg_color @accent;\n@define-color theme_selected_fg_color @accent_text;\n"
    provider = Gtk.CssProvider()
    provider.load_from_data((names + CSS).encode())
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return _mode


def status_dot(state, text):
    """Pango markup: a colored dot and the text, for stage and check states."""
    hue = {"pass": "success", "fail": "danger", "warn": "warning", "run": "info"}.get(state, "text_muted")
    text_color = color("text_muted" if hue == "text_muted" else "text")
    return (f'<span foreground="{color(hue)}">●</span>  '
            f'<span foreground="{text_color}">{GLib.markup_escape_text(text)}</span>')
