#!/usr/bin/python3 -B
"""GTK desktop console for the Windows Dock Ethernet link."""

from datetime import datetime
from pathlib import Path, PureWindowsPath
import sys
from threading import Event, Thread

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango

import windows_ssh as windows
from remote_desktop import share_input, show_windows


STYLE = b"""
window.dock, .dock viewport, .dock scrolledwindow { background-color: #0a111a; color: #e8f2f8; }
.dock label { color: #e8f2f8; }
.dock button {
    min-height: 29px;
    padding: 3px 12px;
    border-radius: 6px;
    border: 1px solid #2b4051;
    background-image: none;
    background-color: #152333;
    color: #e8f2f8;
    text-shadow: none;
    box-shadow: none;
}
.dock button:hover { background-color: #203446; border-color: #476376; }
.dock button.suggested-action { background-color: #56e0df; border-color: #56e0df; color: #07222a; }
.dock button.suggested-action:hover { background-color: #8ceceb; border-color: #8ceceb; }
.dock button.stop-action { background-color: #48232c; border-color: #bb6674; color: #ffc3c8; }
.dock button.stop-action:hover { background-color: #612e39; }
.dock button:disabled { background-color: #111a25; border-color: #223141; color: #8193a5; }
.dock button label { color: inherit; }
.dock button:focus, .dock entry:focus {
    outline: 2px solid #56e0df;
    outline-offset: -3px;
}
.dock entry {
    min-height: 28px;
    padding: 3px 9px;
    border-radius: 5px;
    border: 1px solid #2b4051;
    background: #0b1520;
    color: #e8f2f8;
    caret-color: #56e0df;
    box-shadow: none;
}
.dock entry:focus { border-color: #56e0df; }
.dock entry placeholder { color: #9aafc2; }
.dock .hero {
    background: #0e1926;
    padding: 18px 24px;
    border-bottom: 1px solid #26394a;
    box-shadow: inset 3px 0 #56e0df;
}
.dock .hero-title { font-size: 27px; font-weight: bold; }
.dock .hero-meta { color: #9eb5c7; font-family: monospace; font-size: 11px; }
.dock .connection {
    color: #f6d295; background: #30271c; border: 1px solid #5c4931;
    border-radius: 6px; padding: 6px 9px;
    font-family: monospace; font-size: 11px; font-weight: bold;
}
.dock .connection.online { color: #82edb4; background: #173329; border-color: #306450; }
.dock .navigation button {
    border-radius: 0;
    border-color: transparent;
    border-bottom: 2px solid transparent;
    background-color: #0a111a;
    color: #a2b7c9;
}
.dock .navigation button:hover { background-color: #142333; color: #e8f2f8; }
.dock .navigation button:checked {
    border-bottom-color: #56e0df;
    background-color: #101e2b;
    color: #56e0df;
    font-weight: bold;
}
.dock .section-title { font-size: 20px; font-weight: bold; }
.dock .card {
    padding: 14px 16px;
    border-radius: 8px;
    background: #132131;
    border: 1px solid #2b4051;
}
.dock .card-title { color: #c0d3e4; font-weight: bold; }
.dock .metric { font-size: 21px; font-weight: bold; }
.dock .muted { color: #a2b7c9; }
.dock .session-online { color: #82edb4; }
.dock .session-waiting { color: #f6d295; }
.dock .footer {
    background-color: #0e1926; color: #a2b7c9; padding: 9px 24px;
    border-top: 1px solid #26394a;
}
.dock .footer.failed { color: #ffc3c8; }
.dock .console text { background: #0b1520; color: #e8f2f8; }
.dock .console text selection, .dock entry selection { background: #56e0df; color: #07222a; }
.dock progressbar trough { background: #243648; border-color: #243648; }
.dock progressbar progress { background: #56e0df; border-color: #56e0df; }
"""


def label(text="", style=None):
    widget = Gtk.Label(label=text, xalign=0)
    if style:
        widget.get_style_context().add_class(style)
    return widget


def vertical(spacing=8):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def add(box, widget, expand=False):
    box.pack_start(widget, expand, expand, 0)


def card(title):
    panel = vertical(9)
    panel.get_style_context().add_class("card")
    add(panel, label(title, "card-title"))
    return panel


def action_button(text, handler, suggested=False):
    button = Gtk.Button(label=text)
    button.connect("clicked", handler)
    if suggested:
        button.get_style_context().add_class("suggested-action")
    return button


def gib(value):
    return f"{value / (1024 ** 3):.1f}".replace(".", ",") + " GiB"


def uptime(seconds):
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return (f"{days} g " if days else "") + f"{hours} h {minutes} min"


class DockWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Windows Dock")
        self.get_style_context().add_class("dock")
        self.set_default_size(860, 730)
        self.set_size_request(660, 510)
        self.connect("delete-event", self.on_close)
        self.busy = False
        self.close_pending = False
        self.remote_stop = None
        self.remote_mode = None
        try:
            self.set_icon_from_file(str(Path(__file__).with_name("windows-dock-icon.png")))
        except GLib.Error:
            self.set_icon_name("computer")
        self.profile = PureWindowsPath(r"C:\Users\Admin")
        self.suggested_remote = ""

        root = vertical(0)
        self.add(root)
        add(root, self.build_header())

        self.content = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tabs = Gtk.StackSwitcher(stack=self.content, halign=Gtk.Align.START)
        tabs.get_style_context().add_class("navigation")
        tabs.set_margin_start(24)
        tabs.set_margin_end(24)
        tabs.set_margin_top(14)
        add(root, tabs)

        self.content.add_titled(self.status_page(), "status", "Stato")
        self.content.add_titled(self.transfer_page(), "files", "Trasferimenti")
        self.content.add_titled(self.script_page(), "scripts", "PowerShell")
        self.content.add_titled(self.input_page(), "input", "Mouse e tastiera")
        add(root, self.content, True)

        self.footer = label("Pronto", "footer")
        self.footer.set_selectable(True)
        self.footer.set_line_wrap(True)
        add(root, self.footer)

    def build_header(self):
        header = Gtk.Box(spacing=14)
        header.get_style_context().add_class("hero")
        try:
            path = Path(__file__).with_name("windows-dock-icon.png")
            logo = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 48, 48, True))
        except GLib.Error:
            logo = Gtk.Image.new_from_icon_name("computer", Gtk.IconSize.DIALOG)
        logo.set_valign(Gtk.Align.CENTER)
        add(header, logo)
        titles = vertical(3)
        add(titles, label("Vincula", "hero-title"))
        self.host_line = label(f"Windows Dock  ·  {windows.HOST}", "hero-meta")
        self.host_line.set_ellipsize(Pango.EllipsizeMode.END)
        add(titles, self.host_line)
        add(header, titles, True)
        self.connection = label("SSH da verificare", "connection")
        self.connection.set_valign(Gtk.Align.CENTER)
        add(header, self.connection)
        self.desktop_button = action_button("Desktop remoto", self.toggle_desktop)
        self.desktop_button.set_valign(Gtk.Align.CENTER)
        add(header, self.desktop_button)
        return header

    def page(self, title, button=None):
        outer = vertical(14)
        outer.set_border_width(24)
        heading = Gtk.Box(spacing=10)
        add(heading, label(title, "section-title"), True)
        if button is not None:
            add(heading, button)
        add(outer, heading)
        return outer

    def status_page(self):
        refresh = action_button("Aggiorna", lambda _button: self.refresh(), True)
        page = self.page("Stato di Windows", refresh)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.status_body = vertical(12)
        scroll.add(self.status_body)
        add(page, scroll, True)
        add(self.status_body, label("In attesa dei dati del PC Windows.", "muted"))
        return page

    def refresh(self):
        self.start("Lettura dello stato di Windows…", windows.status, self.show_status,
                   failure=self.show_status_failure)

    def show_status_failure(self):
        self.connection.set_label("SSH non verificato")
        self.connection.get_style_context().remove_class("online")

    def show_status(self, report):
        self.profile = PureWindowsPath(report["profile"])
        self.host_line.set_text(f"Windows Dock  ·  {windows.HOST}")
        self.host_line.set_tooltip_text(f"{report['host'].lower()} · {report['user']}")
        self.connection.set_label("SSH attivo")
        self.connection.get_style_context().add_class("online")
        for child in self.status_body.get_children():
            child.destroy()

        overview = card("Sistema")
        add(overview, label(report["os"], "metric"))
        add(overview, label(f"Build {report['build']}  ·  Acceso da {uptime(report['uptime'])}", "muted"))
        add(self.status_body, overview)

        row = Gtk.Box(spacing=12, homogeneous=True)
        cpu = card("Processore")
        add(cpu, label(f"{report['cpu']:.0f} %" if report["cpu"] is not None else "—", "metric"))
        add(row, cpu, True)

        memory = card("Memoria")
        used = report["ram_total"] - report["ram_free"]
        add(memory, label(f"{gib(used)} / {gib(report['ram_total'])}", "metric"))
        add(memory, self.meter(used, report["ram_total"]))
        add(row, memory, True)
        add(self.status_body, row)

        disks = card("Unità Windows")
        for disk in report["disks"]:
            total, free = disk["total"], disk["free"]
            if not total or free is None:
                add(disks, label(f"{disk['drive']}  ·  Spazio non disponibile", "muted"))
                continue
            add(disks, label(f"{disk['drive']}  ·  {gib(free)} liberi di {gib(total)}", "muted"))
            add(disks, self.meter(total - free, total))
        if not report["disks"]:
            add(disks, label("Nessuna unità locale rilevata.", "muted"))
        add(self.status_body, disks)
        self.status_body.show_all()
        return f"Aggiornato alle {datetime.now():%H:%M:%S}"

    @staticmethod
    def meter(used, total):
        meter = Gtk.ProgressBar()
        meter.set_fraction(min(1, max(0, used / total)) if total else 0)
        return meter

    def field(self, panel, title, placeholder, browse=None):
        caption = label(title)
        add(panel, caption)
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(placeholder_text=placeholder)
        caption.set_mnemonic_widget(entry)
        add(row, entry, True)
        if browse:
            add(row, action_button("Sfoglia…", lambda _button: browse(entry)))
        add(panel, row)
        return entry

    def transfer_page(self):
        page = self.page("Trasferisci file")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(spacing=14, homogeneous=True, valign=Gtk.Align.START)
        scroll.add(body)
        add(page, scroll, True)

        send = card("Linux → Windows")
        self.upload_local = self.field(send, "File su Linux", "/home/utente/documento.txt",
                                       self.choose_upload)
        self.upload_remote = self.field(send, "Destinazione su Windows", r"C:\Users\Admin\documento.txt")
        send_action = action_button("Invia file", lambda _button: self.transfer(True), True)
        send_action.set_halign(Gtk.Align.END)
        add(send, send_action)
        add(body, send, True)

        receive = card("Windows → Linux")
        self.download_remote = self.field(receive, "File su Windows", r"C:\Users\Admin\documento.txt")
        self.download_local = self.field(receive, "Destinazione su Linux", "/home/utente/documento.txt",
                                         self.choose_download)
        receive_action = action_button("Scarica file", lambda _button: self.transfer(False), True)
        receive_action.set_halign(Gtk.Align.END)
        add(receive, receive_action)
        add(body, receive, True)
        return page

    def choose_file(self, entry, *, save=False, name="", powershell=False):
        mode = Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN
        title = "Salva su Linux" if save else "Seleziona un file su Linux"
        dialog = Gtk.FileChooserNative.new(title, self, mode, "Seleziona", "Annulla")
        if name and save:
            dialog.set_current_name(name)
        if powershell:
            file_filter = Gtk.FileFilter()
            file_filter.set_name("Script PowerShell (.ps1)")
            file_filter.add_pattern("*.[pP][sS]1")
            dialog.add_filter(file_filter)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if path:
            entry.set_text(path)
        return path

    def choose_upload(self, entry):
        path = self.choose_file(entry)
        current = self.upload_remote.get_text()
        if path and (not current or current == self.suggested_remote):
            self.suggested_remote = str(self.profile / Path(path).name)
            self.upload_remote.set_text(self.suggested_remote)

    def choose_download(self, entry):
        filename = PureWindowsPath(self.download_remote.get_text()).name
        self.choose_file(entry, save=True, name=filename)

    def transfer(self, upload):
        if upload:
            local = self.upload_local.get_text()
            remote = self.upload_remote.get_text()
            job, description = windows.upload, "Invio verso Windows…"
            args, direction = (local, remote), "Inviato su Windows"
            destination = remote
        else:
            local = self.download_local.get_text()
            remote = self.download_remote.get_text()
            job, description = windows.download, "Download verso Linux…"
            args, direction = (remote, local), "Salvato su Linux"
            destination = local
        if not local.strip() or not remote.strip():
            self.message("Indica il file e il percorso di destinazione.", failed=True)
            return
        self.start(description, lambda: job(*args), lambda _: f"{direction} · {destination}")

    def script_page(self):
        page = self.page("Esegui script PowerShell")
        panel = card(f"Account di esecuzione su Windows · {windows.USER}")
        self.script_file = self.field(panel, "Script .ps1 su Linux", "/home/utente/script.ps1",
                                      lambda entry: self.choose_file(entry, powershell=True))
        execute = action_button("Esegui su Windows", lambda _button: self.execute(), True)
        execute.set_halign(Gtk.Align.END)
        add(panel, execute)
        add(page, panel)

        heading = Gtk.Box(spacing=8)
        add(heading, label("Risultato", "card-title"), True)
        add(heading, action_button("Copia output", self.copy_output))
        add(page, heading)
        self.output = Gtk.TextView(editable=False, monospace=True,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.set_left_margin(12)
        self.output.set_right_margin(12)
        self.output.set_top_margin(10)
        self.output.get_style_context().add_class("console")
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.output)
        add(page, scroll, True)
        return page

    def copy_output(self, _button):
        buffer = self.output.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        if text:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def execute(self):
        path = self.script_file.get_text()
        if not path.strip():
            self.message("Seleziona uno script .ps1.", failed=True)
            return
        self.output.get_buffer().set_text("")
        self.start("Script in esecuzione su Windows…", lambda: windows.run_script(path),
                   self.show_script)

    def show_script(self, result):
        code, stdout, stderr = result
        parts = [f"Codice di uscita: {code}"]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"Errori e diagnostica\n{stderr}")
        self.output.get_buffer().set_text("\n\n".join(parts))
        return f"Script terminato · codice {code}"

    def message(self, text, failed=False):
        self.footer.set_text(text)
        style = self.footer.get_style_context()
        if failed:
            style.add_class("failed")
        else:
            style.remove_class("failed")

    def start(self, description, job, success, failure=None):
        if self.busy:
            return
        self.busy = True
        self.set_job_pages_sensitive(False)
        self.message(description)

        def worker():
            try:
                value = job()
            except (OSError, windows.LinkError) as error:
                GLib.idle_add(self.finish, success, None, str(error), failure)
            else:
                GLib.idle_add(self.finish, success, value, None, failure)

        Thread(target=worker, daemon=True).start()

    def finish(self, success, value, error, failure):
        self.busy = False
        self.set_job_pages_sensitive(True)
        if error:
            if failure:
                failure()
            self.message(error, failed=True)
        else:
            self.message(success(value))
        self.finish_close()
        return False

    def set_job_pages_sensitive(self, sensitive):
        for name in ("status", "files", "scripts"):
            self.content.get_child_by_name(name).set_sensitive(sensitive)

    def input_page(self):
        page = self.page("Condividi mouse e tastiera")
        panel = card("Posizione dello schermo Windows")
        self.input_edge = Gtk.ComboBoxText()
        for direction, title in (("east", "A destra di Linux"), ("west", "A sinistra di Linux"),
                                 ("north", "Sopra Linux"), ("south", "Sotto Linux")):
            self.input_edge.append(direction, title)
        self.input_edge.set_active_id("east")
        self.input_edge.set_tooltip_text("Scegli il bordo da cui passare allo schermo Windows.")
        add(panel, self.input_edge)

        guide = label("Porta il puntatore oltre il bordo scelto. Premi Ctrl+F12 per cambiare PC.", "muted")
        guide.set_line_wrap(True)
        add(panel, guide)

        row = Gtk.Box(spacing=10)
        self.input_status = label("Disattivata", "muted")
        self.input_status.set_line_wrap(True)
        add(row, self.input_status, True)
        self.input_button = action_button("Attiva condivisione", self.toggle_input, True)
        add(row, self.input_button)
        add(panel, row)
        add(page, panel)
        return page

    def toggle_desktop(self, _button):
        if self.remote_stop is not None:
            self.stop_remote()
        else:
            self.toggle_remote("desktop", show_windows)

    def toggle_input(self, _button):
        edge = self.input_edge.get_active_id()

        def run(stop):
            share_input(stop, edge, lambda: GLib.idle_add(self.input_connected))

        self.toggle_remote("input", run)

    def input_connected(self):
        if self.remote_mode == "input" and not self.remote_stop.is_set():
            self.input_status.set_label("Condivisione attiva")
            style = self.input_status.get_style_context()
            style.remove_class("session-waiting")
            style.add_class("session-online")
        return False

    def toggle_remote(self, mode, job):
        if self.remote_stop is not None:
            if mode == self.remote_mode:
                self.stop_remote()
            return

        stop = Event()
        self.remote_stop = stop
        self.remote_mode = mode
        self.input_button.set_sensitive(mode == "input")
        self.input_edge.set_sensitive(False)
        self.desktop_button.get_style_context().add_class("stop-action")
        if mode == "desktop":
            self.desktop_button.set_label("Chiudi desktop remoto")
            self.input_status.set_label("Chiudi il desktop remoto per usare questa modalità.")
        else:
            self.input_status.set_label("Connessione via Ethernet…")
            self.input_status.get_style_context().add_class("session-waiting")
            self.input_button.get_style_context().add_class("stop-action")
            self.input_button.set_label("Disattiva condivisione")
            self.desktop_button.set_label("Disattiva condivisione")

        def worker():
            error_text = ""
            try:
                job(stop)
            except (OSError, windows.LinkError) as error:
                error_text = str(error)
            GLib.idle_add(self.finish_remote, mode, error_text)

        Thread(target=worker).start()

    def stop_remote(self):
        self.remote_stop.set()
        self.desktop_button.set_sensitive(False)
        self.input_button.set_sensitive(False)
        if self.remote_mode == "input":
            self.input_status.set_label("Chiusura…")
            style = self.input_status.get_style_context()
            style.remove_class("session-online")
            style.add_class("session-waiting")

    def finish_remote(self, mode, error):
        self.remote_stop = None
        self.remote_mode = None
        self.desktop_button.set_label("Desktop remoto")
        self.desktop_button.get_style_context().remove_class("stop-action")
        self.desktop_button.set_sensitive(True)
        self.input_button.set_label("Attiva condivisione")
        self.input_button.get_style_context().remove_class("stop-action")
        self.input_button.set_sensitive(True)
        self.input_edge.set_sensitive(True)
        self.input_status.set_label("Disattivata")
        style = self.input_status.get_style_context()
        style.remove_class("session-online")
        style.remove_class("session-waiting")
        if error and not self.close_pending:
            title = "Condivisione di mouse e tastiera" if mode == "input" else "Collegamento desktop remoto"
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                       message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text=title)
            dialog.format_secondary_text(error)
            dialog.run()
            dialog.destroy()
        self.finish_close()
        return False

    def on_close(self, *_args):
        if self.remote_stop is not None:
            self.stop_remote()
        if self.busy or self.remote_stop is not None:
            self.close_pending = True
        if self.busy:
            self.message("Attendi il termine dell’operazione prima di chiudere.")
        return self.close_pending

    def finish_close(self):
        if self.close_pending and not self.busy and self.remote_stop is None:
            self.close_pending = False
            self.close()

class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.windowsdock.Console",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            css = Gtk.CssProvider()
            css.load_from_data(STYLE)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            window = DockWindow(self)
            window.show_all()
            window.refresh()
        window.deiconify()
        surface = window.get_window()
        if isinstance(surface, GdkX11.X11Window):
            surface.set_events(surface.get_events() | Gdk.EventMask.PROPERTY_CHANGE_MASK)
            window.present_with_time(GdkX11.x11_get_server_time(surface))
        else:
            window.present()


if __name__ == "__main__":
    sys.exit(Application().run(sys.argv))
