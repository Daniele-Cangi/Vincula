# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local operator interface for the authorized job runner."""

import json
import tkinter as tk
from tkinter import messagebox, ttk

import jobs


def main():
    window = tk.Tk()
    window.title("Vincula · Permessi dei lavori")
    window.geometry("720x380")
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    choice = tk.StringVar()
    rows = {}
    ttk.Label(frame, text="Permessi su questo PC").pack(anchor="w")

    def change_mode():
        with jobs.transaction() as db:
            jobs.set_mode(db, choice.get())
        refresh()

    for label, value in (("Richiedi approvazione", "ask"),
                         ("Accesso completo a programmi e cartelle autorizzati", "full")):
        ttk.Radiobutton(frame, text=label, variable=choice, value=value,
                        command=change_mode).pack(anchor="w", pady=2)

    ttk.Label(frame, text="Richieste in attesa").pack(anchor="w", pady=(14, 3))
    requests = ttk.Treeview(frame, columns=("program", "id"), show="headings", selectmode="browse", height=5)
    requests.heading("program", text="Richiesta")
    requests.heading("id", text="ID lavoro")
    requests.pack(fill="both", expand=True)

    def review(allow):
        selected = requests.selection()
        if not selected or selected[0] not in rows:
            return
        kind, key = selected[0].split(":", 1)
        if allow and not messagebox.askyesno("Approva questa richiesta", rows[selected[0]], parent=window):
            return
        try:
            with jobs.transaction() as db:
                decide = jobs.files.decide if kind == "file" else jobs.decide
                decide(db, key, allow)
        except ValueError as error:
            messagebox.showinfo("Richiesta aggiornata", str(error), parent=window)
        refresh()

    actions = ttk.Frame(frame)
    actions.pack(fill="x", pady=(10, 0))
    ttk.Button(actions, text="Approva…", command=lambda: review(True)).pack(side="left")
    ttk.Button(actions, text="Rifiuta", command=lambda: review(False)).pack(side="left", padx=8)

    def refresh():
        with jobs.transaction() as db:
            choice.set(jobs.mode(db))
            pending = jobs.pending_jobs(db)
            file_pending = jobs.files.permission_items(db)
        entries = [("job", row["id"], row["program"],
                    json.dumps(json.loads(row["argv"]), ensure_ascii=False, indent=2)) for row in pending]
        entries.extend(("file", row["id"], jobs.files.LABELS[row["operation"]],
                        f"{row['root']}/{row['relative']}\nOffset: {row['offset']}") for row in file_pending)
        current = {f"{kind}:{key}" for kind, key, _, _ in entries}
        for stale in set(rows) - current:
            requests.delete(stale)
            del rows[stale]
        for kind, key, label, details in entries:
            item = f"{kind}:{key}"
            if item not in rows:
                rows[item] = details
                requests.insert("", "end", iid=item, values=(label, key))

    def poll():
        refresh()
        window.after(750, poll)

    poll()
    window.mainloop()


if __name__ == "__main__":
    main()
