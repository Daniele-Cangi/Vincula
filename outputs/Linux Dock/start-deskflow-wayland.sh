#!/bin/bash
# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail
base="$(dirname -- "$(readlink -f -- "$0")")"
engine="$base/bin/deskflow-core"
export LD_LIBRARY_PATH="$base/lib"
export QT_PLUGIN_PATH="$base/lib/qt6/plugins"
export QT_QPA_PLATFORM=xcb

if [[ -x "$base/clipboard-release/bin/deskflow-core" ]]; then
    engine="$base/clipboard-release/bin/deskflow-core"
    export LD_LIBRARY_PATH="$base/clipboard-release/lib:$LD_LIBRARY_PATH"
    export QT_PLUGIN_PATH="$base/clipboard-release/lib/qt6/plugins"
elif [[ -x "$base/clipboard-native/deskflow-core" ]]; then
    engine="$base/clipboard-native/deskflow-core"
    export LD_LIBRARY_PATH="$base/clipboard-native/lib:$LD_LIBRARY_PATH"
fi

umask 077
"$engine" client --new-instance -s "$base/settings/dock-client.conf" 2>&1 | tee "$base/client.log"
