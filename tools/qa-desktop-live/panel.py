#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owner-local, user-operated desktop QA panel. No network or global hooks.

Normal mode imports only the native GUI for this OS. --self-test is headless:
it does not import GTK/Tk or access a clipboard/display.
"""
import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from uuid import UUID

MAX_BYTES = 16384
MAX_COUNTER = 2**31 - 1
MARKER = "VINCULA"
COLORS = {"bg": "#0b121a", "card": "#13212e", "button": "#183c49", "active": "#245868",
          "fg": "#e5edf4", "muted": "#a2b7c9", "cyan": "#5fd4cf", "danger": "#ff8caf"}


class PanelError(Exception):
    """Fixed diagnostic code; never includes input/clipboard/error text."""


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise PanelError("invalid_uuid")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PanelError("duplicate_config_key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Config:
    run_id: str
    local_node: str
    label: str
    nodes: dict[str, str]


def parse_config(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise PanelError("config_size")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if (not isinstance(value, dict) or set(value) != {"protocol", "run_id", "local_node", "label", "nodes"}
            or type(value["protocol"]) is not int or value["protocol"] != 1
            or not isinstance(value["nodes"], dict) or not 1 <= len(value["nodes"]) <= 64):
        raise PanelError("config_schema")
    def label(text):
        if not isinstance(text, str) or not 1 <= len(text) <= 80 or not text.isprintable():
            raise PanelError("invalid_label")
        return text
    run_id, local = canonical_uuid(value["run_id"]), canonical_uuid(value["local_node"])
    nodes = {canonical_uuid(key): label(text) for key, text in value["nodes"].items()}
    if local not in nodes:
        raise PanelError("local_node_not_registered")
    return Config(run_id, local, label(value["label"]), nodes)


def token(config, node=None):
    return "VINCULA-CLIP-" + config.run_id + "-" + (config.local_node if node is None else node)


def token_matches(config, text):
    """Discard arbitrary text. Return only registered UUIDs for this QA run."""
    if not isinstance(text, str) or len(text) > 65536:
        return ()
    return tuple(node for node in sorted(config.nodes)
                 if re.search(r"(?<![A-Za-z0-9-])" + re.escape(token(config, node)) + r"(?![A-Za-z0-9-])", text))


@dataclass
class Report:
    config: Config
    copied_count: int = 0
    paste_matches: list[str] = field(default_factory=list)
    input_marker_seen: bool = False
    click_count: int = 0
    focus_count: int = 0
    state: str = "ready"

    def increment(self, name):
        if self.state != "closed":
            setattr(self, name, min(MAX_COUNTER, getattr(self, name) + 1))

    def pasted(self, text):
        matches = token_matches(self.config, text) if self.state != "closed" else ()
        for node in matches:
            if node not in self.paste_matches:
                self.paste_matches.append(node)
        return matches

    def marker(self, text):
        changed = self.state != "closed" and not self.input_marker_seen and text == MARKER
        self.input_marker_seen |= changed
        return changed

    def document(self):
        return {"protocol": 1, "run_id": self.config.run_id, "local_node": self.config.local_node,
                "copied_count": self.copied_count, "paste_matches": list(self.paste_matches),
                "input_marker_seen": self.input_marker_seen, "click_count": self.click_count,
                "focus_count": self.focus_count, "state": self.state}

    def encoded(self):
        raw = json.dumps(self.document(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(raw) > MAX_BYTES:
            raise PanelError("report_size")
        return raw


class WindowsPrivacy:
    """Inspect owner/DACL by SID, never localized text or external programs."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.api = c.WinDLL("advapi32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        pointer = c.c_void_p
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.LocalFree.argtypes = [pointer]
        self.api.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
        self.api.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, pointer, w.DWORD, c.POINTER(w.DWORD)]
        self.api.ConvertSidToStringSidW.argtypes = [pointer, c.POINTER(pointer)]
        self.api.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, c.c_int, w.DWORD, c.POINTER(pointer), pointer,
                                                  c.POINTER(pointer), pointer, c.POINTER(pointer)]
        self.api.GetNamedSecurityInfoW.restype = w.DWORD
        self.api.GetAce.argtypes = [pointer, w.DWORD, c.POINTER(pointer)]
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD,
                                                                                c.POINTER(pointer), pointer]
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
        class SecurityAttributes(c.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", pointer), ("inherit_handle", w.BOOL)]
        self.security_attributes = SecurityAttributes
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(SecurityAttributes),
                                           w.DWORD, w.DWORD, w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        handle, size = w.HANDLE(), w.DWORD()
        if not self.api.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, c.byref(handle)):
            raise PanelError("private_path_check_failed")
        try:
            self.api.GetTokenInformation(handle, 1, None, 0, c.byref(size))
            if not size.value:
                raise PanelError("private_path_check_failed")
            buffer = c.create_string_buffer(size.value)
            if not self.api.GetTokenInformation(handle, 1, buffer, size, c.byref(size)):
                raise PanelError("private_path_check_failed")
            self.user_sid = self.sid(c.c_void_p.from_buffer(buffer).value)
            self.trusted = {self.user_sid, "S-1-5-18", "S-1-5-32-544"}
        finally:
            self.kernel.CloseHandle(handle)

    def sid(self, address):
        c = self.c
        text = c.c_void_p()
        if not address or not self.api.ConvertSidToStringSidW(address, c.byref(text)):
            raise PanelError("private_path_check_failed")
        try:
            return c.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    def verify(self, path):
        c = self.c
        owner, dacl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
        if self.api.GetNamedSecurityInfoW(str(path), 1, 0x00000005, c.byref(owner), None,
                                        c.byref(dacl), None, c.byref(descriptor)):
            raise PanelError("private_path_check_failed")
        try:
            if self.sid(owner) not in self.trusted or not dacl:
                raise PanelError("path_not_private")
            for index in range(c.c_ushort.from_address(dacl.value + 4).value):
                ace = c.c_void_p()
                if not self.api.GetAce(dacl, index, c.byref(ace)):
                    raise PanelError("private_path_check_failed")
                kind = c.c_ubyte.from_address(ace.value).value
                if kind == 1:  # A denied ACE grants nothing.
                    continue
                if kind != 0:
                    raise PanelError("unsupported_private_acl")
                if c.c_uint32.from_address(ace.value + 4).value and self.sid(ace.value + 8) not in self.trusted:
                    raise PanelError("path_not_private")
        finally:
            self.kernel.LocalFree(descriptor)

    def temporary(self, parent):
        """Create a new private Medium file; never change an existing ACL."""
        import msvcrt
        from ctypes import wintypes as w
        c = self.c
        descriptor = c.c_void_p()
        sddl = ("O:" + self.user_sid + "D:P(A;;FA;;;" + self.user_sid
                + ")(A;;FA;;;SY)(A;;FA;;;BA)S:(ML;;NW;;;ME)")
        if not self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
            raise PanelError("private_file_create_failed")
        try:
            attributes = self.security_attributes(c.sizeof(self.security_attributes), descriptor, False)
            for _ in range(10):
                path = parent / (".vincula-qa-" + secrets.token_hex(16) + ".tmp")
                handle = self.kernel.CreateFileW(str(path), 0xC0000000, 7, c.byref(attributes), 1, 0x00200080, None)
                if handle == w.HANDLE(-1).value:
                    if c.get_last_error() in (80, 183):
                        continue
                    raise PanelError("private_file_create_failed")
                try:
                    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
                except Exception:
                    self.kernel.CloseHandle(handle)
                    path.unlink(missing_ok=True)
                    raise
                return fd, str(path)
            raise PanelError("private_file_create_failed")
        finally:
            self.kernel.LocalFree(descriptor)


class PrivateFiles:
    def __init__(self):
        self.windows = WindowsPrivacy() if sys.platform == "win32" else None

    def verify(self, path, directory=False):
        info = path.lstat()
        if (path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or not directory and info.st_nlink != 1):
            raise PanelError("unsafe_path")
        if self.windows:
            self.windows.verify(path)
        elif info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PanelError("path_not_private")

    def absolute(self, value, exists):
        path = Path(value)
        if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            raise PanelError("absolute_path_required")
        if path.resolve(strict=exists) != path:
            raise PanelError("canonical_path_required")
        return path

    def config(self, value):
        path = self.absolute(value, True)
        self.verify(path.parent, directory=True)
        self.verify(path)
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PanelError("unsafe_path")
            raw = stream.read(MAX_BYTES + 1)
        return parse_config(raw)

    def temporary(self, parent):
        if self.windows:
            return self.windows.temporary(parent)
        return tempfile.mkstemp(prefix=".vincula-qa-", suffix=".tmp", dir=parent)


class ReportStore:
    def __init__(self, privacy, value):
        self.privacy = privacy
        self.path = privacy.absolute(value, False)
        privacy.verify(self.path.parent, directory=True)
        if self.path.exists() or self.path.is_symlink():
            raise PanelError("report_already_exists")
        self.created = False

    def write(self, report):
        self.privacy.verify(self.path.parent, directory=True)
        if self.path.exists() or self.path.is_symlink():
            if not self.created:
                raise PanelError("report_already_exists")
            self.privacy.verify(self.path)
        fd, name = self.privacy.temporary(self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                self.privacy.verify(temporary)
                stream.write(report.encoded())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.created = True
            self.privacy.verify(self.path)
        finally:
            temporary.unlink(missing_ok=True)


class Controller:
    def __init__(self, config, store):
        self.config, self.store, self.report = config, store, Report(config)
        self.alive = True
        self.status = lambda message: None

    def publish(self):
        try:
            self.store.write(self.report)
        except Exception:
            self.status("Report non aggiornabile. Chiudi la finestra e avvisa l’operatore.")

    def event(self, name):
        if self.alive:
            self.report.increment(name)
            self.publish()

    def copied(self):
        self.report.increment("copied_count")
        self.publish()
        self.status("Token sintetico copiato. Passa a un altro schermo e usa Incolla.")

    def pasted(self, text):
        if not self.alive:
            return
        matches = self.report.pasted(text)
        self.publish()
        self.status("Token ricevuto da: " + ", ".join(self.config.nodes[node] for node in matches)
                    if matches else "Nessun token di questo collaudo riconosciuto. Nessun testo è stato registrato.")

    def marker(self, text):
        if self.report.marker(text):
            self.publish()
            self.status("Marker VINCULA completato. Il report contiene soltanto un sì/no.")

    def close(self):
        self.alive = False
        self.report.state = "closed"
        self.publish()


def gtk_panel(controller):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GObject, Gtk
    window = Gtk.Window(title="Vincula · QA desktop · " + controller.config.label)
    window.set_default_size(720, 530)
    window.set_border_width(20)
    css = Gtk.CssProvider()
    css.load_from_data(("window, box {background:" + COLORS["bg"] + ";color:" + COLORS["fg"] + ";}"
        "label {color:" + COLORS["fg"] + ";} button {background:" + COLORS["button"]
        + ";color:" + COLORS["fg"] + ";border:1px solid " + COLORS["cyan"] + ";padding:11px;border-radius:7px;}"
        "button:hover {background:" + COLORS["active"] + ";} entry {background:" + COLORS["card"]
        + ";color:" + COLORS["fg"] + ";padding:10px;} .title {font-size:23px;font-weight:bold;color:"
        + COLORS["cyan"] + ";} .muted {color:" + COLORS["muted"] + ";} .token {font-family:monospace;}").encode())
    Gtk.StyleContext.add_provider_for_screen(window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
    window.add(scroller)
    scroller.add(body)
    def text(value, style=None):
        widget = Gtk.Label(label=value, xalign=0)
        widget.set_line_wrap(True)
        widget.set_max_width_chars(75)
        if style:
            widget.get_style_context().add_class(style)
        body.pack_start(widget, False, False, 0)
        return widget
    text("VINCULA / DESKTOP QA", "title")
    text(controller.config.label + " · pannello di prova locale", "muted")
    text("Copia il token su questo PC. Incolla nella finestra QA di un altro PC. "
         "Gli appunti vengono letti solo premendo Incolla o Ctrl+V in questa finestra.")
    text(token(controller.config), "token")
    actions = Gtk.Box(spacing=12)
    body.pack_start(actions, False, False, 0)
    copy_button = Gtk.Button(label="Copia token · Ctrl+C")
    paste_button = Gtk.Button(label="Incolla e verifica · Ctrl+V")
    actions.pack_start(copy_button, True, True, 0)
    actions.pack_start(paste_button, True, True, 0)
    text("Prova tastiera: digita VINCULA nel campo. Sono accettati soltanto i prefissi di questo marker.")
    entry = Gtk.Entry()
    entry.set_max_length(len(MARKER))
    entry.set_placeholder_text("VINCULA")
    body.pack_start(entry, False, False, 0)
    status = text("Pronto per le tue azioni. Nessuna lettura automatica degli appunti.", "muted")
    controller.status = status.set_text
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    def focused():
        if controller.alive and window.is_active():
            return True
        controller.status("Porta questa finestra in primo piano e ripeti l’azione.")
        return False
    def copy(*unused):
        if focused():
            try:
                clipboard.set_text(token(controller.config), -1)
                controller.copied()
            except Exception:
                controller.status("Copia non disponibile. Ripeti con questa finestra attiva.")
    def paste(*unused):
        if focused():
            try:
                clipboard.request_text(lambda selection, value, data: controller.pasted(value), None)
            except Exception:
                controller.status("Incolla non disponibile. Ripeti con questa finestra attiva.")
    copy_button.connect("clicked", copy)
    paste_button.connect("clicked", paste)
    def key(widget, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            name = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval))
            if name in {"c", "v", "x"}:
                (copy if name == "c" else paste if name == "v" else lambda: None)()
                return True
        return False
    for widget in (window, entry):
        widget.connect("key-press-event", key)
    def entry_action(widget, action):
        GObject.signal_stop_emission_by_name(widget, action)
        if action == "copy-clipboard":
            copy()
        elif action == "paste-clipboard":
            paste()
    for action in ("copy-clipboard", "paste-clipboard", "cut-clipboard"):
        entry.connect(action, lambda widget, selected=action: entry_action(widget, selected))
    resetting = [False]
    def changed(widget):
        if resetting[0]:
            return
        value = widget.get_text()
        if not MARKER.startswith(value):
            resetting[0] = True
            widget.set_text("")
            resetting[0] = False
        else:
            controller.marker(value)
    entry.connect("changed", changed)
    last_click = [None]
    def click(widget, event):
        identity = (event.time, event.button)
        if identity != last_click[0]:
            last_click[0] = identity
            controller.event("click_count")
        return widget is entry and event.button == 2  # No implicit PRIMARY selection paste.
    for widget in (window, copy_button, paste_button, entry):
        widget.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        widget.connect("button-press-event", click)
    window.connect("focus-in-event", lambda *args: controller.event("focus_count") or False)
    window.connect("map-event", lambda *args: controller.publish() or False)
    def close(*unused):
        controller.close()
        Gtk.main_quit()
        return False
    window.connect("delete-event", close)
    window.show_all()
    Gtk.main()


def tk_panel(controller):
    import tkinter as tk
    root = tk.Tk()
    root.title("Vincula · QA desktop · " + controller.config.label)
    root.configure(bg=COLORS["bg"])
    root.geometry("760x570")
    root.minsize(650, 500)
    body = tk.Frame(root, bg=COLORS["bg"], padx=22, pady=20)
    body.pack(fill="both", expand=True)
    labels = []
    def text(value, color="fg", size=12):
        widget = tk.Label(body, text=value, bg=COLORS["bg"], fg=COLORS[color], justify="left", anchor="w",
                          wraplength=680, font=("Segoe UI", size))
        widget.pack(fill="x", pady=(0, 14))
        labels.append(widget)
        return widget
    text("VINCULA / DESKTOP QA", "cyan", 22)
    text(controller.config.label + " · pannello di prova locale", "muted")
    text("Copia il token su questo PC. Incolla nella finestra QA di un altro PC. "
         "Gli appunti vengono letti solo con Incolla o Ctrl+V in questa finestra.")
    text(token(controller.config), "muted", 10)
    row = tk.Frame(body, bg=COLORS["bg"])
    row.pack(fill="x", pady=(0, 18))
    status_var = tk.StringVar(value="Pronto per le tue azioni. Nessuna lettura automatica degli appunti.")
    controller.status = status_var.set
    def focused():
        if controller.alive and root.focus_displayof() is not None:
            return True
        controller.status("Porta questa finestra in primo piano e ripeti l’azione.")
        return False
    def copy(event=None):
        if focused():
            try:
                root.clipboard_clear()
                root.clipboard_append(token(controller.config))
                controller.copied()
            except tk.TclError:
                controller.status("Copia non disponibile. Ripeti con questa finestra attiva.")
        return "break"
    def paste(event=None):
        if focused():
            try:
                controller.pasted(root.clipboard_get())
            except tk.TclError:
                controller.status("Nessun token testuale disponibile. Nessun testo è stato registrato.")
        return "break"
    for label, command in (("Copia token · Ctrl+C", copy), ("Incolla e verifica · Ctrl+V", paste)):
        tk.Button(row, text=label, command=command, bg=COLORS["button"], fg=COLORS["fg"],
                  activebackground=COLORS["active"], activeforeground=COLORS["fg"], relief="flat", bd=1,
                  highlightbackground=COLORS["cyan"], padx=14, pady=11, font=("Segoe UI", 11)).pack(side="left", expand=True, fill="x", padx=5)
    text("Prova tastiera: digita VINCULA nel campo. Sono accettati soltanto i prefissi di questo marker.")
    variable = tk.StringVar()
    entry = tk.Entry(body, textvariable=variable, bg=COLORS["card"], fg=COLORS["fg"], insertbackground=COLORS["cyan"],
                     relief="flat", font=("Consolas", 15), validate="key",
                     validatecommand=(root.register(lambda value: MARKER.startswith(value)), "%P"))
    entry.pack(fill="x", ipady=10, pady=(0, 18))
    variable.trace_add("write", lambda *args: controller.marker(variable.get()))
    status_label = tk.Label(body, textvariable=status_var, bg=COLORS["bg"], fg=COLORS["muted"], justify="left", anchor="w",
                            wraplength=680, font=("Segoe UI", 11))
    status_label.pack(fill="x")
    labels.append(status_label)
    def wrap(event):
        width = max(200, event.width - 44)
        for widget in labels:
            if int(widget.cget("wraplength")) != width:
                widget.configure(wraplength=width)
    body.bind("<Configure>", wrap)
    for widget in (root, entry):
        for sequence in ("<Control-c>", "<Control-C>", "<<Copy>>"):
            widget.bind(sequence, copy)
        for sequence in ("<Control-v>", "<Control-V>", "<<Paste>>"):
            widget.bind(sequence, paste)
        for sequence in ("<Control-x>", "<Control-X>", "<<Cut>>"):
            widget.bind(sequence, lambda event: "break")
    root.bind("<ButtonPress>", lambda event: controller.event("click_count"), add="+")
    root.bind("<FocusIn>", lambda event: controller.event("focus_count"), add="+")
    def close():
        controller.close()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after_idle(controller.publish)
    root.mainloop()


def self_test():
    """Pure fixtures; no GUI imports, clipboard, credentials, SSH or network."""
    import unittest
    ids = ["0d97d16f-1624-4467-b834-f0592a4d29ab", "a197f389-14b2-4716-86a2-70314e2ee4e2"]
    document = {"protocol": 1, "run_id": "a9e6dfbf-e39f-435a-9a01-b787eb596f18", "local_node": ids[0],
                "label": "Ubuntu QA", "nodes": {ids[0]: "Ubuntu", ids[1]: "Windows"}}
    config = parse_config(json.dumps(document).encode())
    class Fixtures(unittest.TestCase):
        def test_config(self):
            self.assertEqual(config.local_node, ids[0])
            for change in ({"protocol": True}, {"run_id": "not-uuid"}, {"nodes": {}}, {"label": "bad\nlabel"},
                           {"local_node": "00000000-0000-0000-0000-000000000000"}):
                with self.assertRaises((PanelError, ValueError)):
                    parse_config(json.dumps(document | change).encode())
        def test_duplicate_and_bounded_config(self):
            with self.assertRaises(PanelError):
                parse_config(b'{"protocol":1,"protocol":1}')
            with self.assertRaises(PanelError):
                parse_config(b" " * (MAX_BYTES + 1))
        def test_token_matches_only_registered_run(self):
            self.assertEqual(token_matches(config, token(config, ids[1])), (ids[1],))
            self.assertEqual(token_matches(config, "prefix\n" + token(config) + "\n" + token(config, ids[1])), tuple(sorted(ids)))
            for value in (None, "arbitrary-private-sentinel", token(config) + "x", token(config).replace(config.run_id, ids[1]),
                          token(config, "00000000-0000-0000-0000-000000000000"), "x" * 65537):
                self.assertEqual(token_matches(config, value), ())
        def test_report_never_contains_text_or_labels(self):
            report = Report(config)
            report.pasted("arbitrary-private-sentinel " + token(config, ids[1]))
            report.pasted(token(config, ids[1]))
            report.increment("copied_count")
            raw = report.encoded()
            self.assertNotIn(b"arbitrary-private-sentinel", raw)
            self.assertNotIn(b"Ubuntu", raw)
            self.assertEqual(report.paste_matches, [ids[1]])
            self.assertLess(len(raw), MAX_BYTES)
        def test_marker_and_closed_state(self):
            report = Report(config)
            for value in ("", "VIN", "VINCULA-private-sentinel"):
                self.assertFalse(report.marker(value))
            self.assertTrue(report.marker(MARKER))
            self.assertFalse(report.marker(MARKER))
            report.state = "closed"
            report.increment("copied_count")
            self.assertEqual(report.pasted(token(config)), ())
            self.assertEqual(report.copied_count, 0)
            self.assertTrue(report.input_marker_seen)
        def test_counter_bounds(self):
            report = Report(config, click_count=MAX_COUNTER)
            report.increment("click_count")
            self.assertEqual(report.click_count, MAX_COUNTER)
        def test_maximum_node_report_is_bounded(self):
            nodes = {str(UUID(int=index + 1)): "N" * 80 for index in range(64)}
            maximum = parse_config(json.dumps(document | {"local_node": next(iter(nodes)), "nodes": nodes}).encode())
            report = Report(maximum)
            report.pasted("\n".join(token(maximum, node) for node in nodes))
            self.assertEqual(len(report.paste_matches), 64)
            self.assertLess(len(report.encoded()), MAX_BYTES)
            with self.assertRaises(PanelError):
                parse_config(json.dumps(document | {"nodes": nodes | {ids[0]: "extra"}}).encode())
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Fixtures))
    return 0 if result.wasSuccessful() else 1


def file_self_test(value):
    """Exercise real file permissions in an operator-provided private directory."""
    import unittest
    from unittest.mock import patch
    privacy = PrivateFiles()
    directory = privacy.absolute(value, True)
    privacy.verify(directory, directory=True)
    identifier = "a197f389-14b2-4716-86a2-70314e2ee4e2"
    config = Config("a9e6dfbf-e39f-435a-9a01-b787eb596f18", identifier, "QA", {identifier: "QA"})

    class FileFixtures(unittest.TestCase):
        def setUp(self):
            self.path = directory / ("vincula-qa-test-" + secrets.token_hex(16) + ".json")
            self.store = ReportStore(privacy, str(self.path))

        def tearDown(self):
            if self.store.created:
                privacy.verify(self.path)
                self.path.unlink()

        def test_private_atomic_report_and_existing_destination(self):
            report = Report(config)
            self.store.write(report)
            privacy.verify(self.path)
            report.increment("click_count")
            self.store.write(report)
            privacy.verify(self.path)
            self.assertEqual(self.path.read_bytes(), report.encoded())
            with self.assertRaises(PanelError):
                ReportStore(privacy, str(self.path))
            self.assertEqual(self.path.read_bytes(), report.encoded())

        def test_failed_encoding_preserves_report_and_cleans_temporary(self):
            report = Report(config)
            self.store.write(report)
            before = set(directory.iterdir())
            class InvalidReport:
                def encoded(self):
                    raise PanelError("synthetic_encode_failure")
            with self.assertRaises(PanelError):
                self.store.write(InvalidReport())
            self.assertEqual(set(directory.iterdir()), before)
            self.assertEqual(self.path.read_bytes(), report.encoded())

        @unittest.skipUnless(sys.platform == "win32", "Native Windows CREATE_NEW/ACL regression")
        def test_windows_private_at_creation_and_collision_does_not_truncate(self):
            with patch.object(secrets, "token_hex", return_value="native-collision-fixture"):
                fd, name = privacy.temporary(directory)
                path = Path(name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        privacy.verify(path)
                        stream.write(b"synthetic-exclusive-file")
                    with self.assertRaises(PanelError):
                        privacy.temporary(directory)
                    privacy.verify(path)
                    self.assertEqual(path.read_bytes(), b"synthetic-exclusive-file")
                finally:
                    path.unlink(missing_ok=True)

    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FileFixtures))
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--report")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-files", metavar="PRIVATE_DIRECTORY")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.self_test_files:
        return file_self_test(args.self_test_files)
    if not args.config or not args.report:
        parser.error("--config and --report are required")
    try:
        privacy = PrivateFiles()
        config = privacy.config(args.config)
        store = ReportStore(privacy, args.report)
        controller = Controller(config, store)
        if sys.platform == "win32":
            try:
                import tkinter  # Availability check only; tk_panel creates the one owned window.
            except ImportError:
                raise PanelError("tkinter_missing") from None
            tk_panel(controller)
        else:
            gtk_panel(controller)
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, PanelError) else "panel_start_failed"
        print(json.dumps({"protocol": 1, "error": code}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
