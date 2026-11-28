# User-operated desktop QA panel

This standalone panel is for a later, explicitly scheduled live desktop test.
Creating or testing these files does **not** activate sharing. It has no SSH,
network, credential access, global input hooks, UI automation or screenshots.

Linux uses GTK 3 through the already-installed PyGObject (native Wayland/X11).
Windows uses the Python standard-library Tkinter frontend. If that interpreter
lacks Tkinter, startup returns the fixed diagnostic `tkinter_missing`; this tool
does not install it. Do not fall back to X11 clipboard utilities on Wayland.

Create a fresh owner-private directory and UTF-8 config file, then launch in the
actual graphical user session only when the desktop engine is ready:

```sh
python3 -I -B panel.py --config /private/config.json --report /private/report.json
```

On Windows use `py.exe -3 -I -B panel.py` with absolute private Windows paths.
The directory and config must already be private: Linux owner-only permissions;
Windows owner/DACL limited to the current user, SYSTEM and Administrators.
Symlinks/reparse points, linked files, traversal and existing report filenames
are refused. No ACLs or existing permissions are repaired by the panel.
Windows report temporary files are created exclusively with an explicit owner
SID, protected current-user/SYSTEM/Administrators DACL and Medium integrity label
before any report bytes are written. This avoids Python 3.14's `mkstemp` ACL,
which can grant a logon SID that the private-file validator correctly refuses.
Atomic replacement preserves the new file's private ACL; Linux keeps mode 0600.

Example config (replace the sample run UUID with one fresh UUID shared by all
panels; use the real registered node UUID on each PC):

```json
{"protocol":1,"run_id":"a9e6dfbf-e39f-435a-9a01-b787eb596f18","local_node":"0d97d16f-1624-4467-b834-f0592a4d29ab","label":"Ubuntu QA","nodes":{"0d97d16f-1624-4467-b834-f0592a4d29ab":"Ubuntu","a197f389-14b2-4716-86a2-70314e2ee4e2":"Windows","12365c6d-f1c3-41ad-a4cb-458363b2fae5":"Kali"}}
```

The config is at most 16 KiB, has exactly these fields, canonical UUIDs, and up
to 64 UUID-to-label entries including the local node. Labels are printable and
at most 80 characters. Config values do not supply executable paths or report
destinations; the two owner-selected CLI paths are the only file interface.

The operator performs the actual test in the focused panel:

1. Press **Copia token** or **Ctrl+C**. Only
   `VINCULA-CLIP-<run_uuid>-<local_uuid>` is copied, even when the marker entry
   has focus. No initial clipboard is read, retained or restored.
2. Cross to another screen, focus its panel and press **Incolla e verifica** or
   **Ctrl+V**. Only this user action reads the clipboard. The UI shows matched
   configured node labels, never pasted text. Unrecognized/oversized data is
   discarded. GTK clipboard operations originate inside the real focused user
   gesture, preserving Wayland's input serial; there is no background polling.
3. Type **VINCULA** in the marker entry. Only prefixes of that fixed marker are
   accepted; the report retains a sticky boolean, never characters or key codes.
   Own-window clicks and focus events increment counters. Nothing outside this
   panel is observed.
4. Close the panel normally to publish `state: "closed"`.

The atomically replaced private report is at most 16 KiB and contains only:

```json
{"protocol":1,"run_id":"...","local_node":"...","copied_count":0,"paste_matches":[],"input_marker_seen":false,"click_count":0,"focus_count":0,"state":"ready"}
```

`paste_matches` is a deduplicated list of registered source UUIDs from this run;
the panel never records arbitrary clipboard text, stderr, original clipboard
data, input characters, cursor coordinates or credentials. Counters saturate at
2³¹−1. `ready` means this QA panel is available for interaction; it does not attest
the sharing engine. The copy counter records local accepted copy actions; remote
propagation is demonstrated by the receiving panel's matched source UUID.

Run the pure bounded-config/token/marker/report fixtures without GTK/Tk, display
or clipboard access:

```sh
python3 -I -B panel.py --self-test
```

Test actual file creation, two atomic writes, refusal of an existing destination
and failure cleanup in an existing operator-provided private directory:

```sh
python3 -I -B -W error panel.py --self-test-files /private/qa-directory
```

On Windows use `py.exe -3 -I -B -W error panel.py --self-test-files C:\private\qa-directory`.
That native run also verifies privacy before writing the temporary file and
`CREATE_NEW` collision handling without truncating an existing file. Tests remove
only their freshly created synthetic files. They neither import GTK/Tk nor use
a display, clipboard, network or desktop engine. A POSIX run explicitly skips
the Windows ACL/collision regression.

Actual GUI rendering, Tk availability on the selected Windows interpreter and
live Wayland/three-node behavior remain gates for the later operator test.
