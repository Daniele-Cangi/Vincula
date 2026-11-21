#!/bin/bash
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

if ! systemctl --user is-active --quiet graphical-session.target ||
   ! systemctl --user show-environment | grep -qx 'XDG_SESSION_TYPE=x11'; then
    printf '%s\n' 'Apri una sessione grafica X11 su Ubuntu per ricevere mouse e tastiera.' >&2
    exit 1
fi
if ! flock --exclusive --nonblock --conflict-exit-code 75 "$runtime/desktop-session.lock" \
    systemd-run --user --collect --wait --pipe --quiet \
      --unit="$unit" --property=PartOf=graphical-session.target --property=TimeoutStopSec=2s \
      /bin/bash -c '
        exec 3<&0
        "$@" </dev/null 3<&- &
        client=$!
        ( IFS= read -r -u 3 || : ) &
        # Exiting on SSH EOF makes systemd stop the whole service, including Barrier.
        wait -n -p finished "$client" "$!" || exit "$?"
        [[ "$finished" != "$client" ]]
      ' bash \
      "$runtime/bin/barrierc" --no-daemon --no-restart --no-tray --enable-crypto --profile-dir "$runtime/input-profile" \
      --name dock-ubuntu 127.0.0.1:24801; then
    printf '%s\n' 'Condivisione interrotta. Verifica la sessione X11 e chiudi eventuali altre sessioni Dock.' >&2
    exit 1
fi
