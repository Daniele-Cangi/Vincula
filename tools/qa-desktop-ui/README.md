# GTK managed-group protocol fixture

Run on Linux with Python GObject/GTK3 and a disposable graphical test display:

```sh
xvfb-run -a python3 -B tools/qa-desktop-ui/protocol_gtk.py "$PWD"
```

The fixture imports the repository GUI but substitutes the controller with
temporary synthetic programs. It blocks the status/network and legacy-sharing
entrypoints. It does not invoke the real desktop controller or transfer files
over a network. A private temporary directory contains the fake runtime and
stop/EOF receipts.

Seven scenarios cover mutually exclusive legacy sharing, stop while transfer
and catalog flags are busy and a synthetic RPC is pending, late readiness,
missing prerequisites, unexpected exit, close/EOF and malformed status frames.
These are GUI/protocol checks, not real input/clipboard acceptance.
