"""Small dialogs shared by the pages: ask for a few values, pick from a list, choose a folder."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from input_page import ERROR, hint_label, markup  # noqa: E402


class FieldsDialog(Gtk.Dialog):
    """Asks for a few values. fields: [(key, label, default, kind)], kind: "text", "secret" (hidden text),
    "check", a list of choices, or "folder". The widgets are in self.widgets[key]; values() reads them. check(values) may return an error
    text, which is shown and keeps the dialog open."""

    def __init__(self, parent, title, fields, intro="", ok="OK", check=None):
        super().__init__(title=title, transient_for=parent, modal=True, default_width=520)
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.check, self.widgets = check, {}
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)
        row = 0
        if intro:
            grid.attach(hint_label(intro), 0, row, 2, 1)
            row += 1
        for key, label, default, kind in fields:
            if kind == "check":
                w = Gtk.CheckButton(label=label, active=bool(default))
                grid.attach(w, 1, row, 1, 1)
            else:
                grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
                if isinstance(kind, (list, tuple)):
                    w = Gtk.ComboBoxText(hexpand=True)
                    for choice in kind:
                        w.append(str(choice), str(choice))
                    w.set_active_id(str(default) if default else (str(kind[0]) if kind else None))
                elif kind == "folder":
                    w = Gtk.FileChooserButton(title=label, action=Gtk.FileChooserAction.SELECT_FOLDER, hexpand=True)
                    if default:
                        w.set_filename(str(default))
                else:
                    w = Gtk.Entry(text=str(default or ""), hexpand=True, activates_default=True,
                                  visibility=kind != "secret")
                grid.attach(w, 1, row, 1, 1)
            self.widgets[key] = w
            row += 1
        self.error = Gtk.Label(xalign=0, wrap=True)
        grid.attach(self.error, 0, row, 2, 1)
        self.get_content_area().add(grid)
        self.show_all()

    def values(self):
        out = {}
        for key, w in self.widgets.items():
            if isinstance(w, Gtk.CheckButton):
                out[key] = w.get_active()
            elif isinstance(w, Gtk.ComboBoxText):
                out[key] = w.get_active_id() or ""
            elif isinstance(w, Gtk.FileChooserButton):
                out[key] = w.get_filename() or ""
            else:
                out[key] = w.get_text().strip()
        return out

    def ask(self):
        """Run until the values pass the check or the user cancels; the values, or None."""
        result = None
        while self.run() == Gtk.ResponseType.OK:
            vals = self.values()
            why = self.check(vals) if self.check else None
            if not why:
                result = vals
                break
            self.error.set_markup(markup(why, ERROR))
        self.destroy()
        return result


def ask_fields(parent, title, fields, intro="", ok="OK", check=None):
    return FieldsDialog(parent, title, fields, intro, ok, check).ask()


def choose_folder(parent, title, start=None, create=False):
    """A folder picked by the user, or None."""
    action = Gtk.FileChooserAction.CREATE_FOLDER if create else Gtk.FileChooserAction.SELECT_FOLDER
    dlg = Gtk.FileChooserDialog(title=title, transient_for=parent, action=action)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose", Gtk.ResponseType.OK)
    if start:
        dlg.set_current_folder(str(start))
    path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
    dlg.destroy()
    return path
