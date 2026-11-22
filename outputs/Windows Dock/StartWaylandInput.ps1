# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

param([switch]$Client, [switch]$Stop, [int]$TunnelProcessId)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$name = Split-Path -Leaf $root
if ($name -notmatch '^VinculaWayland-[a-f0-9]{32}$') { throw 'Cartella di sessione non valida.' }
$engine = Join-Path $env:LOCALAPPDATA 'Linux Dock/input-engine/deskflow-1.27.0-win-x64-portable'
$binary = Join-Path $engine 'deskflow-core.exe'
$settings = Join-Path $root 'client.conf'
$lease = Join-Path $root 'lease'
$task = "Vincula Input $name"

function Stop-ClientTask {
    Remove-Item -LiteralPath $lease -Force -ErrorAction SilentlyContinue
    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    $entry = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    while ($entry.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 150
        $entry = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    }
    if ($entry.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $task
    }
    if ($entry) { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue }
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        throw 'Rimozione del processo pianificato Deskflow non confermata.'
    }
}

if ($Stop) {
    Stop-ClientTask
    return
}

if ($Client) {
    if ($TunnelProcessId -le 0) { throw 'Tunnel SSH non identificato.' }
    $arguments = 'client --new-instance -s "' + $settings + '"'
    $process = Start-Process -FilePath $binary -ArgumentList $arguments -WorkingDirectory $engine -WindowStyle Hidden -PassThru
    try {
        while (-not $process.WaitForExit(500)) {
            if (-not (Test-Path -LiteralPath $lease)) { break }
            $owner = Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 24802 -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess
            if ($TunnelProcessId -notin $owner) { break }
        }
    } finally {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            [void]$process.WaitForExit(2000)
        }
        $process.Dispose()
    }
    return
}

$certificate = Join-Path $engine 'settings/tls/dock-windows.pem'
$trusted = Join-Path $engine 'settings/tls/trusted-clients'
if (-not (Test-Path -LiteralPath $binary) -or -not (Test-Path -LiteralPath $certificate)) {
    throw 'Deskflow su Windows non disponibile.'
}
$knownPeer = @([IO.File]::ReadAllLines($trusted) | Where-Object { $_ -match '^v2:sha256:[0-9a-fA-F]{64}$' })
if (-not $knownPeer.Count) { throw 'Impronta TLS verificata di Ubuntu assente.' }

$tls = Join-Path $root 'tls'
[void][IO.Directory]::CreateDirectory($tls)
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllLines((Join-Path $tls 'trusted-servers'), [string[]]$knownPeer, $utf8)
$certPath = $certificate.Replace('\', '/')
[IO.File]::WriteAllText($settings, @"
[core]
computerName=dock-windows
port=24802
processMode=1
[client]
remoteHost=127.0.0.1
[security]
tlsEnabled=true
checkPeerFingerprints=true
certificate="$certPath"
"@, $utf8)
[IO.File]::WriteAllText($lease, '', $utf8)

$listener = Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 24802 -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $listener) { throw 'Il tunnel SSH non ascolta su Windows.' }
$powershell = Join-Path $PSHOME 'powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' + $PSCommandPath +
    '" -Client -TunnelProcessId ' + $listener.OwningProcess
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$options = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $task -Action $action -Principal $principal -Settings $options | Out-Null
try {
    Start-ScheduledTask -TaskName $task
    [Console]::WriteLine('VinculaWaylandClientStarted')
    [void][Console]::In.ReadToEnd()
} finally {
    Stop-ClientTask
}
