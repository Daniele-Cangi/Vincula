#!/bin/bash
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
runtime="$(dirname -- "$(readlink -f -- "$0")")"
mode="${1:-}"
id="${2:-}"
[[ "$id" =~ ^[a-f0-9]{32}$ ]] || exit 2
unit="linux-dock-input-$id.service"
if [[ "$mode" == stop ]]; then
    state="$(systemctl --user show --property=ActiveState --value "$unit")"
    if [[ "$state" != inactive && "$state" != failed ]]; then
        systemctl --user stop "$unit"
    fi
    exit
fi
[[ "$mode" == start ]] || exit 2

if ! systemctl --user is-active --quiet graphical-session.target; then
    printf '%s\n' 'Apri la sessione grafica Ubuntu prima di condividere mouse e tastiera.' >&2
    exit 1
fi
session="$(systemctl --user show-environment | sed -n 's/^XDG_SESSION_TYPE=//p')"
case "$session" in
    wayland)
        if [[ ! -x "$runtime/deskflow-wayland/start-client" ]]; then
            printf '%s\n' 'Il ricevitore Deskflow Wayland non è installato su Ubuntu.' >&2
            exit 1
        fi
        client=("$runtime/deskflow-wayland/start-client")
        ;;
    x11)
        client=("$runtime/bin/barrierc" --no-daemon --no-restart --no-tray --enable-crypto
                --profile-dir "$runtime/input-profile" --name dock-ubuntu 127.0.0.1:24801)
        ;;
    *)
        printf '%s\n' 'Sessione grafica Ubuntu non supportata.' >&2
        exit 1
        ;;
esac
if ! flock --exclusive --nonblock --conflict-exit-code 75 "$runtime/desktop-session.lock" \
    systemd-run --user --collect --wait --pipe --quiet \
      --unit="$unit" --property=PartOf=graphical-session.target --property=TimeoutStopSec=2s \
      /bin/bash -c '
        exec 3<&0
        "$@" </dev/null 3<&- &
        client=$!
        ( IFS= read -r -u 3 || : ) &
        # Exiting on SSH EOF makes systemd stop the receiver and its children.
        wait -n -p finished "$client" "$!" || exit "$?"
        [[ "$finished" != "$client" ]]
      ' bash \
      "${client[@]}"; then
    printf '%s\n' 'Condivisione interrotta. Verifica il server e il consenso di Ubuntu.' >&2
    exit 1
fi
