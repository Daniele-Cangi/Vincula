// SPDX-FileCopyrightText: 2026 Daniele Cangi
// SPDX-License-Identifier: GPL-3.0-or-later

using System;
using System.Diagnostics;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;
using CheckBoxState = System.Windows.Forms.VisualStyles.CheckBoxState;

public sealed class DockResult
{
    public int Code;
    public string Output;
    public string Errors;
}

internal sealed class InputStopException : IOException
{
    public InputStopException(string message) : base(message) { }
}

public enum TransferPhase { Preparing, Transferring, Publishing, Completed, Failed }

public sealed class TransferUpdate
{
    public readonly TransferPhase Phase;
    public readonly long? Bytes, Total;
    public TransferUpdate(TransferPhase phase, long? bytes, long? total)
    { Phase = phase; Bytes = bytes; Total = total; }
}

public sealed class TransferProgressState
{
    public long Generation { get; private set; }
    public TransferPhase Phase { get; private set; }
    public long Bytes { get; private set; }
    public long? Total { get; private set; }
    public bool HasSample { get; private set; }
    public bool Terminal { get { return Phase == TransferPhase.Completed || Phase == TransferPhase.Failed; } }
    public long Begin()
    {
        Generation++; Phase = TransferPhase.Preparing; Bytes = 0; Total = null; HasSample = false;
        return Generation;
    }
    public bool Accept(long generation, TransferUpdate update)
    {
        if (generation != Generation || Terminal || (int)update.Phase < (int)Phase)
            return false;
        Phase = update.Phase;
        if (update.Total.HasValue)
            Total = Math.Max(0, update.Total.Value);
        HasSample = update.Bytes.HasValue;
        if (HasSample)
            Bytes = Math.Max(Bytes, Math.Max(0, update.Bytes.Value));
        if (Total.HasValue)
            Bytes = Math.Min(Bytes, Total.Value);
        return true;
    }
    public int? Percent
    {
        get
        {
            if (Phase == TransferPhase.Completed) return 100;
            if (!HasSample || !Total.HasValue || Phase == TransferPhase.Preparing) return null;
            if (Total.Value == 0) return 0;
            return Math.Min(99, (int)((decimal)Bytes * 100 / Total.Value));
        }
    }
}

public sealed class TransferSampler : IDisposable
{
    private readonly CancellationTokenSource stop = new CancellationTokenSource();
    private readonly Thread worker;
    public TransferSampler(Func<long?> observe, Action<long?> publish, int intervalMilliseconds = 1000)
    {
        worker = new Thread(() => {
            while (!stop.IsCancellationRequested)
            {
                long? size = null;
                try { size = observe(); } catch (Exception) { }
                if (stop.IsCancellationRequested) break;
                try { publish(size); } catch (Exception) { }
                if (stop.Token.WaitHandle.WaitOne(intervalMilliseconds)) break;
            }
        }) { IsBackground = true, Name = "Vincula transfer progress" };
        worker.Start();
    }
    public void Dispose()
    {
        stop.Cancel();
        worker.Join(); // Called only on the transfer worker, never the UI thread.
        stop.Dispose();
    }
}

public static class DockTransport
{
    private static readonly string Root = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Linux Dock");
    private static readonly string Config = Path.Combine(Root, "ssh_config");
    private static readonly Encoding Utf8 = new UTF8Encoding(false, true);
    private const string LinuxUserBus = "export XDG_RUNTIME_DIR=/run/user/$(id -u)\n"
        + "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus\n";
    private const int OutputLimit = 128 * 1024;
    private const int OutputTail = 4096;
    private const string LinuxPublish = @"from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from errno import EEXIST, EINVAL, ENOSYS, EOPNOTSUPP
from os import fsencode, strerror
from sys import argv, exit

unsupported = 'La destinazione Linux non supporta la rinomina atomica senza sovrascrittura.'
rename = getattr(CDLL(None, use_errno=True), 'renameat2', None)
if rename is None:
    exit(unsupported)
rename.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
rename.restype = c_int
at_fdcwd, rename_noreplace = -100, 1
source, destination = argv[1:]
if rename(at_fdcwd, fsencode(source), at_fdcwd, fsencode(destination), rename_noreplace) != 0:
    error = get_errno()
    if error == EEXIST:
        exit('Il file Linux di destinazione esiste gia.')
    if error in (EINVAL, ENOSYS, EOPNOTSUPP):
        exit(unsupported)
    exit(strerror(error))
";

    private static DockResult Run(string executable, string arguments, int timeout)
    {
        string command = Path.Combine(Environment.SystemDirectory, "OpenSSH", executable);
        var info = new ProcessStartInfo(command, "-F \"" + Config + "\" " + arguments);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = new UTF8Encoding(false);
        info.StandardErrorEncoding = new UTF8Encoding(false);
        using (Process process = Process.Start(info))
        using (StreamReader output = process.StandardOutput)
        using (StreamReader errors = process.StandardError)
        {
            var clock = Stopwatch.StartNew();
            int limit = timeout * 1000;
            Task<string> stdout = ReadOutput(output);
            Task<string> stderr = ReadOutput(errors);
            if (!process.WaitForExit(limit)
                || !Task.WaitAll(new Task[] { stdout, stderr }, Math.Max(0, limit - (int)clock.ElapsedMilliseconds)))
            {
                StopDesktopProcess(process);
                throw new IOException("Tempo limite raggiunto (" + timeout + " s).");
            }
            return new DockResult {
                Code = process.ExitCode,
                Output = stdout.Result.TrimEnd('\r', '\n'),
                Errors = stderr.Result.TrimEnd('\r', '\n')
            };
        }
    }

    private static async Task<string> ReadOutput(StreamReader reader)
    {
        var prefix = new StringBuilder();
        var tail = new StringBuilder();
        var buffer = new char[8192];
        bool truncated = false;
        int count;
        while ((count = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) != 0)
        {
            int kept = Math.Min(count, OutputLimit - prefix.Length);
            prefix.Append(buffer, 0, kept);
            int trailing = Math.Min(count - kept, OutputTail);
            if (trailing != 0)
            {
                // Preserve the final remote exit record even if script output fills the buffer.
                truncated |= tail.Length + count - kept > OutputTail;
                tail.Remove(0, Math.Max(0, tail.Length + trailing - OutputTail));
                tail.Append(buffer, count - trailing, trailing);
            }
        }
        if (truncated)
            prefix.Append("\n[… output troncato …]\n");
        prefix.Append(tail);
        return prefix.ToString();
    }

    private static DockResult Bash(string source, int timeout = 35)
    {
        return Run("ssh.exe", "-nT linux-dock " + BashCommand(source), timeout);
    }

    private static string BashCommand(string source)
    {
        string encoded = Convert.ToBase64String(Utf8.GetBytes(source + "\n"));
        return "\"printf %s " + encoded + " | base64 -d | bash\"";
    }

    private static string Success(DockResult result)
    {
        if (result.Code != 0)
        {
            string detail = String.IsNullOrWhiteSpace(result.Errors) ? result.Output : result.Errors;
            throw new IOException(String.IsNullOrWhiteSpace(detail)
                ? "Operazione terminata con codice " + result.Code + "." : detail);
        }
        return result.Output;
    }

    private static void Sftp(string line)
    {
        string batch = Path.GetTempFileName();
        try
        {
            File.WriteAllText(batch, line + "\n", Utf8);
            Success(Run("sftp.exe", "-q -b \"" + batch + "\" linux-dock", 900));
        }
        finally
        {
            File.Delete(batch);
        }
    }

    private static string BashName(string name)
    {
        return "'" + name.Replace("'", "'\\''") + "'";
    }

    private static string SftpName(string name)
    {
        return "\"" + name + "\"";
    }

    private static string LinuxFile(string value)
    {
        if (!value.StartsWith("/", StringComparison.Ordinal) || value.EndsWith("/", StringComparison.Ordinal)
            || value.IndexOfAny(new[] { '\r', '\n', '\0', '\\', '"' }) >= 0
            || value.Split('/').Any(part => part == "." || part == ".."))
            throw new ArgumentException("Indica un percorso assoluto Linux con il nome del file, ad esempio /home/utente/note.txt.");
        return value;
    }

    public static string Status()
    {
        return Success(Bash(@"set -e
. /etc/os-release
printf 'Sistema: %s\n' ""$PRETTY_NAME""
printf 'Host: %s\n' ""$(hostname)""
printf 'Account: %s\n' ""$(id -un)""
printf 'Home: %s\n' ""$HOME""
printf 'Attivo da %s\n\n' ""$(uptime -p | sed 's/^up //')""
cpu_snapshot() {
    awk '/^cpu / { total=0; for (i=2;i<=9;i++) total+=$i; printf ""%.0f %.0f\n"", total, $5+$6; exit }' /proc/stat
}
read -r t1 i1 < <(cpu_snapshot)
sleep 0.25
read -r t2 i2 < <(cpu_snapshot)
if (( t2 > t1 )); then printf 'CPU: %s%%\n' ""$((100 * (t2-t1-i2+i1) / (t2-t1)))""; fi
free -h | awk '/^Mem:/ { printf ""RAM: %s usati di %s (%s disponibili)\n"", $3, $2, $7 }'
printf '\nDischi\n'
df -h -x tmpfs -x devtmpfs -x squashfs --output=target,used,avail,pcent"));
    }

    private static void TransferReport(Action<TransferUpdate> progress, TransferPhase phase, long? bytes, long? total)
    {
        if (progress != null)
            try { progress(new TransferUpdate(phase, bytes, total)); } catch (Exception) { }
    }

    private static long? RemoteFileSize(string path)
    {
        try
        {
            string quoted = BashName(path);
            string value = Success(Bash("if [ -f " + quoted + " ] && [ ! -L " + quoted
                + " ]; then stat -c %s -- " + quoted + "; fi", 2));
            long size;
            return Int64.TryParse(value, out size) && size >= 0 ? (long?)size : null;
        }
        catch (Exception) { return null; } // Observability must not abort a valid transfer.
    }

    public static void Upload(string local, string remote) { Upload(local, remote, null); }

    public static void Upload(string local, string remote, Action<TransferUpdate> progress)
    {
        TransferReport(progress, TransferPhase.Preparing, null, null);
        local = Path.GetFullPath(local);
        if (!File.Exists(local))
            throw new FileNotFoundException("Il file su Windows non esiste.", local);
        remote = LinuxFile(remote);
        string stage = remote.Substring(0, remote.LastIndexOf('/') + 1)
            + ".linux-dock-" + Guid.NewGuid().ToString("N") + ".part";
        string target = BashName(remote);
        string temporary = BashName(stage);
        long total = new FileInfo(local).Length;
        TransferReport(progress, TransferPhase.Preparing, null, total);
        Success(Bash("if [ -e " + target + " ] || [ -L " + target
            + " ]; then echo 'Il file Linux di destinazione esiste gia.' >&2; exit 1; fi"));
        try
        {
            using (var sampler = progress == null ? null : new TransferSampler(
                () => RemoteFileSize(stage), bytes => TransferReport(progress, TransferPhase.Transferring, bytes, total)))
                Sftp("put " + SftpName(local.Replace('\\', '/')) + " " + SftpName(stage));
            TransferReport(progress, TransferPhase.Publishing, null, total);
            Success(Bash("python3 - " + temporary + " " + target + " <<'DOCK_PUBLISH'\n"
                + LinuxPublish + "DOCK_PUBLISH"));
            TransferReport(progress, TransferPhase.Completed, total, total);
        }
        catch (Exception error)
        {
            string warning = Cleanup("Rimozione del file temporaneo su Linux non confermata: " + stage,
                () => Success(Bash("rm -f -- " + temporary, 15)));
            if (warning.Length != 0)
                throw new IOException(error.Message + "\n" + warning, error);
            throw;
        }
    }

    public static void Download(string remote, string local) { Download(remote, local, null); }

    public static void Download(string remote, string local, Action<TransferUpdate> progress)
    {
        TransferReport(progress, TransferPhase.Preparing, null, null);
        remote = LinuxFile(remote);
        local = Path.GetFullPath(local);
        if (File.Exists(local) || Directory.Exists(local))
            throw new IOException("Il file Windows di destinazione esiste già. Scegli un altro nome.");
        string temporary = Path.Combine(Path.GetDirectoryName(local),
            ".linux-dock-" + Guid.NewGuid().ToString("N") + ".part");
        // One metadata query for the selected source. Progress observes only our local temporary file.
        long? total = progress == null ? null : RemoteFileSize(remote);
        TransferReport(progress, TransferPhase.Preparing, null, total);
        long completedBytes;
        try
        {
            using (var sampler = progress == null ? null : new TransferSampler(
                () => File.Exists(temporary) ? (long?)new FileInfo(temporary).Length : null,
                bytes => TransferReport(progress, TransferPhase.Transferring, bytes, total)))
                Sftp("get " + SftpName(remote) + " " + SftpName(temporary.Replace('\\', '/')));
            long actual = new FileInfo(temporary).Length;
            TransferReport(progress, TransferPhase.Publishing, actual, actual);
            File.Move(temporary, local);
            completedBytes = actual;
        }
        finally
        {
            File.Delete(temporary);
        }
        TransferReport(progress, TransferPhase.Completed, completedBytes, completedBytes);
    }

    public static DockResult Script(string local)
    {
        local = Path.GetFullPath(local);
        if (!File.Exists(local) || !String.Equals(Path.GetExtension(local), ".sh", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Seleziona un file Bash con estensione .sh.");
        string text = File.ReadAllText(local, Utf8).Replace("\r\n", "\n");
        string linuxTemp = Success(Bash("mktemp -d /tmp/linux-dock.XXXXXXXXXX"));
        if (!Regex.IsMatch(linuxTemp, @"\A/tmp/linux-dock\.[a-zA-Z0-9]{10}\z"))
            throw new IOException("Linux ha restituito un percorso temporaneo inatteso.");
        string unit = "linux-dock-script-" + Guid.NewGuid().ToString("N") + ".service";
        string marker = "\nLinuxDockScript_" + Guid.NewGuid().ToString("N") + ":";
        string windowsTemp = null;
        DockResult result;
        try
        {
            windowsTemp = Path.GetTempFileName();
            File.WriteAllText(windowsTemp, text, Utf8);
            Sftp("put " + SftpName(windowsTemp.Replace('\\', '/')) + " "
                + SftpName(linuxTemp + "/script.sh"));
            const string execute = "bash ./script.sh\ncode=$?\nprintf '\\n%s%d\\n' \"$1\" \"$code\"";
            result = Bash(LinuxUserBus + "systemd-run --user --quiet --wait --pipe --collect --service-type=exec"
                + " --expand-environment=no --unit=" + unit + " --working-directory=" + BashName(linuxTemp)
                + " --property=RuntimeMaxSec=600s --property=TimeoutStopSec=5s"
                + " --property=KillMode=control-group /bin/bash -c " + BashName(execute)
                + " bash " + BashName(marker.Substring(1)) + " </dev/null", 620);
            ConfirmScript(result, marker);
        }
        catch (Exception error)
        {
            string warning = CleanScript(windowsTemp, linuxTemp, unit);
            if (warning.Length != 0)
                throw new IOException(error.Message + "\n" + warning, error);
            throw;
        }
        result.Errors = AppendDiagnostic(result.Errors, CleanScript(windowsTemp, linuxTemp, unit));
        return result;
    }

    private static void ConfirmScript(DockResult result, string marker)
    {
        int footer = result.Output.LastIndexOf(marker, StringComparison.Ordinal);
        int remoteCode;
        if (result.Code != 0 || footer < 0
            || !Int32.TryParse(result.Output.Substring(footer + marker.Length), out remoteCode))
        {
            string reason = result.Code == 255 ? "Collegamento SSH interrotto."
                : "Il servizio di esecuzione non ha ricevuto il codice dello script.";
            string detail = AppendDiagnostic(result.Errors, result.Output);
            throw new IOException(AppendDiagnostic("Esito Bash non confermato su Linux. " + reason, detail));
        }
        result.Code = remoteCode;
        result.Output = result.Output.Substring(0, footer).TrimEnd('\r', '\n');
    }

    private static string CleanScript(string local, string remote, string unit)
    {
        string warning = Cleanup("Pulizia non confermata su Linux: " + remote, () =>
            Success(Bash("set -e\n" + LinuxUserBus
                + "state=$(systemctl --user show --property=ActiveState --value " + unit + ")\n"
                + "if [ \"$state\" != inactive ] && [ \"$state\" != failed ]; then "
                + "systemctl --user stop " + unit + "; fi\n"
                + "rm -rf -- " + BashName(remote), 15)));
        if (local != null)
            warning = AppendDiagnostic(warning,
                Cleanup("Pulizia non confermata su Windows: " + local, () => File.Delete(local)));
        return warning;
    }

    private static string Cleanup(string message, Action action)
    {
        try
        {
            action();
            return "";
        }
        catch (Exception error)
        {
            return message + "\n" + error.Message;
        }
    }

    private static string AppendDiagnostic(string original, string warning)
    {
        if (String.IsNullOrEmpty(warning))
            return original;
        return String.IsNullOrEmpty(original) ? warning : original + "\n" + warning;
    }

    public static void Desktop(CancellationToken stop)
    {
        const int localPort = 15901;
        string viewer = Path.Combine(Root, @"vnc-package\PFiles\TightVNC\tvnviewer.exe");
        string profile = Path.Combine(Root, "vnc-linux.vnc");
        if (!File.Exists(viewer) || !File.Exists(profile))
            throw new IOException("Il visualizzatore o il profilo del desktop remoto non è disponibile.");

        var local = new TcpListener(IPAddress.Loopback, localPort);
        try { local.Start(); }
        catch (SocketException error)
        {
            throw new IOException("La porta locale 15901 è occupata. Chiudi la sessione desktop precedente.", error);
        }
        finally { local.Stop(); }

        string ssh = Path.Combine(Environment.SystemDirectory, "OpenSSH", "ssh.exe");
        const string marker = "LinuxDockForwardReady";
        int confirmed = 0;
        var diagnostics = new StringBuilder();
        string args = "-F \"" + Config + "\" -T -o ExitOnForwardFailure=yes"
            + " -L 127.0.0.1:15901:127.0.0.1:5909 linux-dock "
            + "\"printf '%s\\n' " + marker + "; cat >/dev/null\"";
        using (Process tunnel = StartInputProcess(ssh, args, Root, line =>
        {
            if (line == marker)
                Interlocked.Exchange(ref confirmed, 1);
            else
                NoteInput(diagnostics, line);
        }, holdInput: true))
        {
            try
            {
                if (!WaitForDesktop(tunnel, diagnostics, () => Volatile.Read(ref confirmed) != 0, localPort, stop))
                    return;
                var viewerInfo = new ProcessStartInfo(viewer,
                    "-optionsfile=\"" + profile + "\" -useclipboard=no -scale=auto -viewonly=no") {
                        UseShellExecute = false
                    };
                using (Process window = Process.Start(viewerInfo))
                {
                    try
                    {
                        while (!stop.IsCancellationRequested && !window.WaitForExit(200))
                        {
                            if (tunnel.HasExited)
                                throw new IOException(InputError(diagnostics, "Il tunnel SSH si è interrotto."));
                        }
                        if (!stop.IsCancellationRequested && window.HasExited && window.ExitCode != 0)
                            throw new IOException("TightVNC si è chiuso con codice " + window.ExitCode + ".");
                    }
                    finally
                    {
                        StopDesktopProcess(window);
                    }
                }
            }
            finally
            {
                StopDesktopProcess(tunnel);
            }
        }
    }

    private static bool WaitForDesktop(Process tunnel, StringBuilder diagnostics, Func<bool> confirmed,
        int port, CancellationToken stop)
    {
        var timer = Stopwatch.StartNew();
        while (timer.Elapsed.TotalSeconds < 10 && !stop.IsCancellationRequested)
        {
            if (tunnel.HasExited)
                throw new IOException(InputError(diagnostics, "Il tunnel SSH si è chiuso."));
            if (confirmed())
            {
                try
                {
                    using (var channel = new TcpClient())
                    {
                        channel.Connect(IPAddress.Loopback, port);
                        NetworkStream stream = channel.GetStream();
                        stream.ReadTimeout = 400;
                        if (stream.ReadByte() == 'R' && stream.ReadByte() == 'F'
                            && stream.ReadByte() == 'B' && stream.ReadByte() == ' ' && !tunnel.HasExited)
                            return true;
                    }
                }
                catch (SocketException) { }
                catch (IOException) { }
            }
            stop.WaitHandle.WaitOne(150);
        }
        if (stop.IsCancellationRequested)
            return false;
        throw new IOException("Il server desktop Linux non risponde. Verifica che la sessione X11 sia aperta.");
    }

    public static void ShareInput(string direction, CancellationToken stop, Action connected)
    {
        ShareInput(direction, false, stop, connected);
    }

    internal static void CheckInputConnection()
    {
        Success(Bash("true", 8));
    }

    internal static void ShareInput(string direction, bool shareClipboard, CancellationToken stop, Action connected)
    {
        string[] sides = { "left", "right", "up", "down" };
        string[] opposite = { "right", "left", "down", "up" };
        int index = Array.IndexOf(sides, direction);
        if (index < 0)
            throw new ArgumentException("Seleziona la posizione dello schermo Ubuntu.");

        string engine = Path.Combine(Root, @"input-engine\deskflow-1.27.0-win-x64-portable");
        string executable = Path.Combine(engine, "deskflow-core.exe");
        string settings = Path.Combine(engine, "settings");
        string certificate = Path.Combine(settings, "tls", "dock-windows.pem");
        if (!File.Exists(executable) || !File.Exists(certificate))
            throw new IOException("Il componente per condividere mouse e tastiera non è disponibile.");

        var listener = new TcpListener(IPAddress.Loopback, 24800);
        try { listener.Start(); }
        catch (SocketException error)
        {
            throw new IOException("La porta locale 24800 è occupata.", error);
        }
        finally { listener.Stop(); }

        string token = Guid.NewGuid().ToString("N");
        string conf = Path.Combine(settings, "Dock-" + token + ".conf");
        string layout = Path.Combine(settings, "Dock-" + token + ".txt");
        string layoutText = "section: links\n dock-windows:\n  " + direction + " = dock-ubuntu\n"
            + " dock-ubuntu:\n  " + opposite[index] + " = dock-windows\nend\n"
            + "section: options\n keystroke(control+F12) = switchToNextScreen\nend\n";
        string ini = "[core]\ncomputerName=dock-windows\ninterface=127.0.0.1\nport=24800\nprocessMode=1\n"
            + "[server]\nexternalConfig=true\nexternalConfigFile=\"" + layout.Replace('\\', '/') + "\"\n"
            + "protocol=barrier\nenableClipboard=" + (shareClipboard ? "true" : "false")
            + "\nclipboardSize=" + (shareClipboard ? "3" : "0") + "\ndisableLockToComputer=true\n"
            + "[security]\ntlsEnabled=true\ncheckPeerFingerprints=true\ncertificate=\""
            + certificate.Replace('\\', '/') + "\"\n"
            + "[computer_dock-windows]\nname=dock-windows\n[computer_dock-ubuntu]\nname=dock-ubuntu\n";
        var diagnostics = new StringBuilder();
        int authenticated = 0;
        string stopWarning = "";
        const string receiver = "~/.local/share/linux-dock/vnc/receive-input";
        try
        {
            File.WriteAllText(layout, layoutText, Utf8);
            File.WriteAllText(conf, ini, Utf8);
            using (Process server = StartInputProcess(executable, "server -s \"" + conf + "\"",
                engine, line => NoteInput(diagnostics, line)))
            {
                Process client = null;
                try
                {
                    var deadline = Stopwatch.StartNew();
                    while (!InputPortReady())
                    {
                        if (stop.WaitHandle.WaitOne(120))
                            return;
                        if (server.HasExited || deadline.Elapsed.TotalSeconds >= 8)
                            throw new IOException(InputError(diagnostics, "Deskflow non si è avviato."));
                    }
                    if (stop.IsCancellationRequested)
                        return;

                    string ssh = Path.Combine(Environment.SystemDirectory, "OpenSSH", "ssh.exe");
                    string arguments = "-F \"" + Config + "\" -T -o ExitOnForwardFailure=yes"
                        + " -R 127.0.0.1:24801:127.0.0.1:24800 linux-dock "
                        + receiver + " start " + token;
                    client = StartInputProcess(ssh, arguments, Root, line =>
                    {
                        NoteInput(diagnostics, line);
                        if (line.IndexOf("connected to server", StringComparison.OrdinalIgnoreCase) >= 0)
                            Interlocked.Exchange(ref authenticated, 1);
                    }, holdInput: true);

                    deadline.Restart();
                    bool notified = false;
                    while (!stop.WaitHandle.WaitOne(140))
                    {
                        if (server.HasExited || client.HasExited)
                            throw new IOException(InputError(diagnostics, "La condivisione si è interrotta."));
                        if (!notified && Volatile.Read(ref authenticated) == 1)
                        {
                            connected();
                            notified = true;
                        }
                        if (!notified && deadline.Elapsed.TotalSeconds >= 12)
                            throw new IOException(InputError(diagnostics, "Il PC Ubuntu non ha confermato la connessione."));
                    }
                }
                finally
                {
                    stopWarning = Cleanup("Arresto di Deskflow non confermato.", () => StopDesktopProcess(server));
                    if (client != null)
                    {
                        using (client)
                            stopWarning = AppendDiagnostic(stopWarning, StopInputTunnel(client, receiver, token));
                    }
                }
            }
        }
        finally
        {
            string files = AppendDiagnostic(
                Cleanup("Pulizia del layout Deskflow non confermata.", () => File.Delete(layout)),
                Cleanup("Pulizia della configurazione Deskflow non confermata.", () => File.Delete(conf)));
            if (stopWarning.Length != 0)
                throw new InputStopException(InputError(diagnostics, AppendDiagnostic(stopWarning, files)));
            if (files.Length != 0)
                throw new IOException(files);
        }
    }

    private static string StopInputTunnel(Process client, string receiver, string token)
    {
        try
        {
            try { client.StandardInput.Close(); }
            catch (IOException) { }
            if (client.WaitForExit(3500) && client.ExitCode == 0)
                return "";
        }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }

        string warning = Cleanup("Chiusura del tunnel SSH non confermata.", () => StopDesktopProcess(client));
        return AppendDiagnostic(warning, Cleanup("Arresto della condivisione su Ubuntu non confermato.",
            () => Success(Bash(receiver + " stop " + token, 12))));
    }

    private static bool InputPortReady()
    {
        try
        {
            using (var socket = new TcpClient())
                socket.Connect(IPAddress.Loopback, 24800);
            return true;
        }
        catch (SocketException) { return false; }
    }

    private static Process StartInputProcess(string executable, string arguments, string folder,
        Action<string> output, bool holdInput = false)
    {
        var info = new ProcessStartInfo(executable, arguments) {
            WorkingDirectory = folder, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = holdInput
        };
        var process = new Process { StartInfo = info };
        process.OutputDataReceived += (sender, line) => { if (line.Data != null) output(line.Data); };
        process.ErrorDataReceived += (sender, line) => { if (line.Data != null) output(line.Data); };
        if (!process.Start())
        {
            process.Dispose();
            throw new IOException("Il processo di condivisione non si è avviato.");
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return process;
    }

    private static void NoteInput(StringBuilder buffer, string message)
    {
        lock (buffer)
        {
            buffer.AppendLine(message);
            if (buffer.Length > 12000)
                buffer.Remove(0, buffer.Length - 12000);
        }
    }

    private static string InputError(StringBuilder buffer, string message)
    {
        lock (buffer)
        {
            string[] lines = buffer.ToString().Trim().Split('\n');
            return message + Environment.NewLine + String.Join("\n", lines.Skip(Math.Max(0, lines.Length - 10)));
        }
    }

    private static void StopDesktopProcess(Process process)
    {
        if (process.HasExited)
            return;
        try { process.Kill(); }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception)
        {
            if (!process.HasExited)
                throw;
        }
        if (!process.WaitForExit(3000))
            throw new IOException("Il processo " + process.Id + " risulta ancora attivo dopo tre secondi.");
    }
}

internal static class NodeCatalog
{
    private static string RuntimeScript(bool enrollment, bool nodeCheck)
    {
        string configured = Environment.GetEnvironmentVariable("VINCULA_RUNTIME");
        string app = AppDomain.CurrentDomain.BaseDirectory;
        string standard = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Vincula", "runtime");
        string[] roots = String.IsNullOrWhiteSpace(configured)
            ? new[] { app, Path.GetFullPath(Path.Combine(app, "..", "..")), standard }
            : new[] { configured };
        foreach (string root in roots)
        {
            string candidate = Path.Combine(root, "agent", "nodes.py");
            if (File.Exists(candidate) && File.Exists(Path.Combine(root, "agent", "node_registry.py"))
                && File.Exists(Path.Combine(root, "agent", "desktop_layout.py")))
            {
                if (enrollment && !File.Exists(Path.Combine(root, "agent", "node_enrollment.py")))
                    throw new IOException("Aggiorna il runtime Vincula: gli inviti richiedono agent/node_enrollment.py. Il catalogo manuale resta disponibile.");
                if (nodeCheck && !File.Exists(Path.Combine(root, "agent", "node_probe.py")))
                    throw new IOException("Aggiorna il runtime Vincula: la verifica del nodo richiede agent/node_probe.py. Il catalogo manuale resta disponibile.");
                return candidate;
            }
        }
        throw new IOException("Runtime dei nodi incompleto. Installa nodes.py, node_registry.py e desktop_layout.py "
            + "nella cartella agent dell’app oppure imposta VINCULA_RUNTIME alla radice di Vincula.");
    }

    internal static Dictionary<string, object> Map(object value)
    {
        var map = value as Dictionary<string, object>;
        if (map == null)
            throw new IOException("Risposta del runtime non valida.");
        return map;
    }

    internal static string Text(Dictionary<string, object> map, string key, string fallback = "")
    {
        object value;
        return map.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : fallback;
    }

    internal static object[] Items(object value)
    {
        var items = value as object[];
        if (items == null)
            throw new IOException("Elenco dei nodi non valido.");
        return items;
    }

    internal static bool HasLegacyProfile()
    {
        string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Linux Dock", "ssh_config");
        try
        {
            return File.Exists(path) && File.ReadAllLines(path).Any(line =>
                Regex.IsMatch(line, @"^\s*Host\s+", RegexOptions.IgnoreCase)
                && Regex.Split(line.Trim(), @"\s+").Skip(1).Any(host =>
                    String.Equals(host, "linux-dock", StringComparison.OrdinalIgnoreCase)));
        }
        catch (IOException) { return false; }
        catch (UnauthorizedAccessException) { return false; }
    }

    internal static string ReadPublicText(string path, int limit, bool hostKey = false)
    {
        if (hostKey && !String.Equals(Path.GetExtension(path), ".pub", StringComparison.OrdinalIgnoreCase))
            throw new IOException("Seleziona esclusivamente la chiave host pubblica con estensione .pub.");
        FileAttributes attributes = File.GetAttributes(path);
        if ((attributes & (FileAttributes.ReparsePoint | FileAttributes.Directory | FileAttributes.Device)) != 0)
            throw new IOException("Seleziona un file pubblico regolare, senza collegamenti.");
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            if (stream.Length > limit)
                throw new IOException("Il file pubblico supera il limite di " + (limit / 1024) + " KiB.");
            var bytes = new byte[limit + 1];
            int count = 0, received;
            while (count < bytes.Length && (received = stream.Read(bytes, count, bytes.Length - count)) > 0)
                count += received;
            if (count > limit)
                throw new IOException("Il file pubblico supera il limite di " + (limit / 1024) + " KiB.");
            string value;
            try { value = new UTF8Encoding(false, true).GetString(bytes, 0, count); }
            catch (DecoderFallbackException) { throw new IOException("Il file pubblico deve essere UTF-8."); }
            if (hostKey && !value.StartsWith("ssh-ed25519 ", StringComparison.Ordinal))
                throw new IOException("Il file selezionato non contiene una chiave SSH pubblica Ed25519.");
            return hostKey ? value.TrimEnd('\r', '\n') : value;
        }
    }

    internal static ProcessStartInfo DesktopCommand()
    {
        string script = Path.Combine(Path.GetDirectoryName(RuntimeScript(false, false)), "desktop_app.py");
        if (!File.Exists(script))
            throw new IOException("Aggiorna il runtime Vincula: manca il controller del gruppo desktop.");
        string interpreter = Environment.GetEnvironmentVariable("VINCULA_PYTHON");
        bool launcher = String.IsNullOrWhiteSpace(interpreter);
        return new ProcessStartInfo(launcher ? "py.exe" : interpreter,
            (launcher ? "-3 " : "") + "-IB \"" + script + "\" run") {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false, true), StandardErrorEncoding = new UTF8Encoding(false)
        };
    }

    internal static Dictionary<string, object> Request(Dictionary<string, object> request)
    {
        var json = new JavaScriptSerializer();
        string action = Text(request, "action");
        string interpreter = Environment.GetEnvironmentVariable("VINCULA_PYTHON");
        bool launcher = String.IsNullOrWhiteSpace(interpreter);
        var info = new ProcessStartInfo(launcher ? "py.exe" : interpreter,
            (launcher ? "-3 " : "") + "\"" + RuntimeScript(action.StartsWith("enrollment_", StringComparison.Ordinal), action == "enrollment_check") + "\"") {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false)
        };
        using (Process process = Process.Start(info))
        {
            Task<string> stdout = process.StandardOutput.ReadToEndAsync();
            Task<string> stderr = process.StandardError.ReadToEndAsync();
            string payload = Regex.Replace(json.Serialize(request), @"[^\x00-\x7F]",
                match => "\\u" + ((int)match.Value[0]).ToString("x4"));
            process.StandardInput.Write(payload);
            process.StandardInput.Close();
            int timeout = action == "enrollment_check" ? 75 : 15;
            if (!process.WaitForExit(timeout * 1000))
            {
                process.Kill();
                throw new IOException("Il runtime non ha risposto entro " + timeout + " secondi. Aggiorna l’elenco per verificarne lo stato.");
            }
            if (!Task.WaitAll(new Task[] { stdout, stderr }, 1000))
                throw new IOException("Risposta del runtime incompleta.");
            Dictionary<string, object> reply;
            try { reply = Map(json.DeserializeObject(stdout.Result)); }
            catch (ArgumentException) { throw new IOException("Risposta del runtime non valida. Verifica Python e l’installazione di Vincula."); }
            object ok;
            if (!reply.TryGetValue("ok", out ok) || !(ok is bool) || !(bool)ok)
            {
                object error;
                var detail = reply.TryGetValue("error", out error) ? Map(error) : new Dictionary<string, object>();
                if (Text(detail, "code") == "conflict")
                {
                    if (action == "enrollment_check")
                        throw new IOException("Il catalogo, la rotta o la chiave host sono cambiati. Aggiorna l’elenco e ripeti la verifica.");
                    throw new IOException("Il catalogo è cambiato. Aggiorna l’elenco e riapri il modulo prima di salvare.");
                }
                throw new IOException("Catalogo non aggiornato. " + Text(detail, "message", "Errore del runtime."));
            }
            bool invitationReply = action == "enrollment_export" || action == "enrollment_preview";
            bool complete = invitationReply ? reply.ContainsKey("invitation") && reply["invitation"] is Dictionary<string, object>
                : reply.ContainsKey("registry") && reply.ContainsKey("revision");
            if (process.ExitCode != 0 || !complete)
                throw new IOException("Risposta del runtime incompleta.");
            return reply;
        }
    }
}

internal sealed class DockWindow : Form
{
    private const string InputSettings = @"HKEY_CURRENT_USER\Software\Vincula\Linux Dock";
    private static readonly Color Canvas = Color.FromArgb(10, 17, 26);
    private static readonly Color Header = Color.FromArgb(14, 25, 38);
    private static readonly Color Surface = Color.FromArgb(19, 33, 49);
    private static readonly Color Inset = Color.FromArgb(11, 21, 32);
    private static readonly Color Line = Color.FromArgb(43, 64, 81);
    private static readonly Color Ink = Color.FromArgb(232, 242, 248);
    private static readonly Color Muted = Color.FromArgb(162, 183, 201);
    private static readonly Color ControlFill = Color.FromArgb(36, 56, 76);
    private static readonly Color ControlBorder = Color.FromArgb(116, 140, 163);
    private static readonly Color DisabledBorder = Color.FromArgb(92, 115, 136);
    private static readonly Color DisabledInk = Color.FromArgb(194, 211, 225);
    private static readonly Color Cyan = Color.FromArgb(86, 224, 223);
    private static readonly Color OnCyan = Color.FromArgb(7, 34, 42);
    private static readonly Color Online = Color.FromArgb(130, 237, 180);
    private static readonly Color Waiting = Color.FromArgb(246, 210, 149);
    private static readonly Color Danger = Color.FromArgb(255, 195, 200);
    private readonly TabControl tabs = new DockTabs();
    private readonly Label footer = new Label();
    private readonly Label connection = new Label();
    private readonly TextBox status = ConsoleBox();
    private readonly TextBox output = ConsoleBox();
    private readonly Label systemMetric = MetricValue();
    private readonly Label cpuMetric = MetricValue();
    private readonly Label ramMetric = MetricValue();
    private readonly TransferProgressState transferState = new TransferProgressState();
    private readonly Label transferStatus = new Label { AutoSize = true, ForeColor = Muted, Text = "Nessun trasferimento avviato." };
    private readonly Label transferBytes = new Label { AutoSize = true, ForeColor = Muted, Text = "La conferma finale resterà visibile qui." };
    private readonly TransferBar transferBar = new TransferBar();
    private bool busy;
    private bool nodesBusy;
    private bool nodesLoaded;
    private Dictionary<string, object> nodesSnapshot;
    private readonly ListView nodeList = new ListView();
    private readonly Label nodesStatus = new Label();
    private Button nodeAdd, nodeEdit, nodeRemove, nodeRefresh, nodeDesktop, nodeImport, nodeExport, nodeCheck;
    private readonly TabPage nodesPage;
    private NodeEditor nodeEditor;
    private DesktopEditor desktopEditor;
    private InvitationEditor invitationEditor;
    private NodeCheckEditor nodeCheckEditor;
    private readonly Button desktopButton;
    private readonly Button inputHeaderButton;
    private readonly ComboBox inputSide = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly CheckBox shareClipboard = new DockCheckBox {
        Text = "Condividi gli appunti con Ubuntu in entrambe le direzioni",
        AutoSize = true, Anchor = AnchorStyles.Left, Checked = true
    };
    private readonly CheckBox autoConnectInput = new CheckBox {
        Text = "Avvia all'apertura e riconnetti se il collegamento cade",
        AutoSize = true, Anchor = AnchorStyles.Left, Checked = true
    };
    private readonly System.Windows.Forms.Timer inputRetry = new System.Windows.Forms.Timer { Interval = 5000 };
    private readonly Label inputStatus = new Label();
    private readonly TabPage inputPage;
    private Button inputButton;
    private Button inputGroupButton;
    private Button groupButton;
    private readonly Label groupStatus = new Label { AutoSize = true, ForeColor = Muted };
    private ManagedGroupSession managedSession;
    private bool managedUncertain;
    private sealed class ManagedGroupSession
    {
        internal readonly object Gate = new object();
        internal Process Process;
        internal volatile bool StopRequested;
        internal bool CloseInput, Writing, StopSent, InputClosed;
        internal bool CleanupComplete, Terminal, ProtocolFault;
        internal string State = "starting", Message = "Avvio del gruppo…";
    }

    private sealed class TransferBar : Control
    {
        private readonly System.Windows.Forms.Timer animation = new System.Windows.Forms.Timer { Interval = 90 };
        private int offset;
        internal bool Indeterminate;
        internal int Percent;
        internal TransferBar()
        {
            Height = 26; MinimumSize = new Size(80, 24); Dock = DockStyle.Fill;
            AccessibleRole = AccessibleRole.ProgressBar;
            AccessibleName = "Avanzamento trasferimento";
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
            animation.Tick += delegate { offset = (offset + 1) % 24; Invalidate(); };
        }
        internal void Display(int? percent, bool active)
        {
            Percent = percent ?? 0; Indeterminate = !percent.HasValue && active;
            animation.Enabled = Indeterminate && Visible;
            AccessibleDescription = Indeterminate ? "Avanzamento in verifica" : Percent + "%";
            Invalidate();
        }
        protected override void OnVisibleChanged(EventArgs args)
        { base.OnVisibleChanged(args); animation.Enabled = Indeterminate && Visible; }
        protected override void OnPaint(PaintEventArgs args)
        {
            var bounds = new Rectangle(1, 1, Math.Max(0, Width - 2), Math.Max(0, Height - 2));
            using (var background = new SolidBrush(Inset)) args.Graphics.FillRectangle(background, bounds);
            int width = Indeterminate ? bounds.Width / 4 : (int)((long)bounds.Width * Percent / 100);
            int left = Indeterminate ? (bounds.Width + width) * offset / 24 - width : 0;
            args.Graphics.SetClip(bounds);
            using (var fill = new SolidBrush(Cyan)) args.Graphics.FillRectangle(fill, bounds.X + left, bounds.Y, width, bounds.Height);
            args.Graphics.ResetClip();
            using (var border = new Pen(ControlBorder)) args.Graphics.DrawRectangle(border, 0, 0, Math.Max(0, Width - 1), Math.Max(0, Height - 1));
        }
        protected override void Dispose(bool disposing)
        { if (disposing) animation.Dispose(); base.Dispose(disposing); }
    }
    private CancellationTokenSource inputSession;
    private CancellationTokenSource desktopSession;
    private bool closePending;
    private bool automaticInputWanted;
    private bool restartInputAfterSideChange;
    private string linuxHome = "";

    private sealed class DockTabs : TabControl
    {
        public DockTabs()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaintBackground(PaintEventArgs args)
        {
            args.Graphics.Clear(Canvas);
        }

        protected override void OnPaint(PaintEventArgs args)
        {
            using (var active = new SolidBrush(Surface))
            using (var inactive = new SolidBrush(ControlFill))
            using (var accent = new SolidBrush(Cyan))
            using (var border = new Pen(ControlBorder))
            {
                for (int index = 0; index < TabCount; index++)
                {
                    Rectangle bounds = GetTabRect(index);
                    bounds.Inflate(-1, 0);
                    bool selected = index == SelectedIndex;
                    args.Graphics.FillRectangle(selected ? active : inactive, bounds);
                    if (selected)
                        args.Graphics.FillRectangle(accent, bounds.Left, bounds.Bottom - 2, bounds.Width, 2);
                    else
                        args.Graphics.DrawRectangle(border, bounds.Left, bounds.Top, bounds.Width - 1, bounds.Height - 1);
                    TextRenderer.DrawText(args.Graphics, TabPages[index].Text, Font, bounds,
                        selected ? Cyan : Ink, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter
                            | TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis);
                    if (selected && Focused && ShowFocusCues)
                    {
                        bounds.Inflate(-3, -3);
                        ControlPaint.DrawFocusRectangle(args.Graphics, bounds, Ink, Surface);
                    }
                }
            }
        }

        protected override void OnSelectedIndexChanged(EventArgs args)
        {
            base.OnSelectedIndexChanged(args);
            Invalidate();
        }

        protected override void OnGotFocus(EventArgs args)
        {
            base.OnGotFocus(args);
            Invalidate();
        }

        protected override void OnLostFocus(EventArgs args)
        {
            base.OnLostFocus(args);
            Invalidate();
        }
    }

    private sealed class DockButton : Button
    {
        protected override void OnPaint(PaintEventArgs args)
        {
            if (Enabled)
            {
                base.OnPaint(args);
                return;
            }
            // WinForms uses system disabled text colors even when a flat button sets ForeColor.
            using (var background = new SolidBrush(Surface))
                args.Graphics.FillRectangle(background, ClientRectangle);
            using (var border = new Pen(DisabledBorder))
                args.Graphics.DrawRectangle(border, 0, 0, ClientSize.Width - 1, ClientSize.Height - 1);
            TextRenderer.DrawText(args.Graphics, Text, Font, ClientRectangle, DisabledInk,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter
                | TextFormatFlags.SingleLine | TextFormatFlags.EndEllipsis | TextFormatFlags.HidePrefix);
        }
    }

    private sealed class DockCheckBox : CheckBox
    {
        protected override void OnPaint(PaintEventArgs args)
        {
            if (Enabled)
            {
                base.OnPaint(args);
                return;
            }

            using (var background = new SolidBrush(Canvas))
                args.Graphics.FillRectangle(background, ClientRectangle);
            CheckBoxState state = Checked ? CheckBoxState.CheckedDisabled : CheckBoxState.UncheckedDisabled;
            Size glyph = CheckBoxRenderer.GetGlyphSize(args.Graphics, state);
            CheckBoxRenderer.DrawCheckBox(args.Graphics, new Point(0, (Height - glyph.Height) / 2), state);
            Rectangle textBounds = Rectangle.FromLTRB(glyph.Width + 4, 0, Width, Height);
            TextRenderer.DrawText(args.Graphics, Text, Font, textBounds, DisabledInk,
                TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine
                | TextFormatFlags.EndEllipsis | TextFormatFlags.HidePrefix);
        }
    }

    public DockWindow() : this(false) { }

    private Image LoadBrandImage()
    {
        string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "linux-dock-icon.png");
        try
        {
            if (File.Exists(path))
            {
                using (var source = Image.FromFile(path))
                    return new Bitmap(source, 96, 96);
            }
        }
        catch (ArgumentException) { }
        catch (IOException) { }
        return Icon.ToBitmap();
    }

    private DockWindow(bool useSavedInputSettings)
    {
        Text = "Linux Dock";
        string icon = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "linux-dock.ico");
        try
        {
            if (File.Exists(icon))
                Icon = new Icon(icon);
        }
        catch (ArgumentException) { }
        catch (IOException) { }
        MinimumSize = new Size(680, 510);
        ClientSize = new Size(860, 650);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Canvas;
        Font = new Font("Segoe UI", 10);
        ForeColor = Ink;
        FormClosing += delegate(object sender, FormClosingEventArgs args) {
            automaticInputWanted = false;
            inputRetry.Stop();
            if (inputSession != null)
                StopInput();
            if (desktopSession != null)
                desktopSession.Cancel();
            if (managedSession != null)
            {
                closePending = true;
                SetGroupStatus("Arresto del gruppo prima della chiusura…", "stopping");
                QueueGroupStop(managedSession, true);
            }
            if (busy || nodesBusy || desktopSession != null || inputSession != null || managedSession != null)
            {
                closePending = true;
                args.Cancel = true;
            }
            if (busy)
                footer.Text = "La finestra si chiuderà al termine dell’operazione.";
        };

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 94));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
        Controls.Add(layout);

        var hero = new Panel { BackColor = Header, Dock = DockStyle.Fill, Margin = new Padding(0) };
        hero.Paint += delegate(object sender, PaintEventArgs args) {
            using (var accent = new SolidBrush(Cyan))
                args.Graphics.FillRectangle(accent, 0, 0, 3, hero.ClientSize.Height);
            using (var border = new Pen(Line))
                args.Graphics.DrawLine(border, 0, hero.ClientSize.Height - 1,
                    hero.ClientSize.Width, hero.ClientSize.Height - 1);
        };
        var logo = new PictureBox { Image = LoadBrandImage(), Size = new Size(48, 48),
            SizeMode = PictureBoxSizeMode.Zoom, Left = 22, Top = 22 };
        hero.Controls.Add(logo);
        FormClosed += delegate { logo.Image.Dispose(); };
        hero.Controls.Add(new Label { Text = "Vincula", AutoSize = true, ForeColor = Ink,
            Font = new Font("Segoe UI", 21, FontStyle.Bold), Left = 82, Top = 12 });
        hero.Controls.Add(new Label { Text = "Linux Dock  ·  Ubuntu via SSH",
            ForeColor = Muted, AutoSize = true, Font = new Font("Consolas", 9), Left = 84, Top = 55 });
        connection.AutoSize = true;
        connection.Font = new Font("Consolas", 9, FontStyle.Bold);
        connection.Padding = new Padding(9, 4, 9, 4);
        SetConnection("SSH da verificare", false);
        connection.Top = 13;
        hero.Controls.Add(connection);
        desktopButton = ActionButton("Desktop remoto", ToggleDesktop, primary: false);
        inputHeaderButton = ActionButton("Attiva condivisione", ToggleInput);
        var actions = new FlowLayoutPanel {
            Top = 47, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            WrapContents = false
        };
        actions.Controls.Add(inputHeaderButton);
        actions.Controls.Add(desktopButton);
        hero.Controls.Add(actions);
        EventHandler positionConnection = delegate {
            connection.Left = hero.ClientSize.Width - connection.PreferredWidth - 24;
            actions.Left = hero.ClientSize.Width - actions.Width - 21;
        };
        hero.Resize += positionConnection;
        connection.TextChanged += positionConnection;
        actions.SizeChanged += positionConnection;
        positionConnection(this, EventArgs.Empty);
        layout.Controls.Add(hero, 0, 0);

        tabs.Dock = DockStyle.Fill;
        tabs.Margin = new Padding(16, 14, 16, 0);
        tabs.ItemSize = new Size(0, 35);
        tabs.Padding = new Point(14, 5);
        tabs.TabPages.Add(BuildStatus());
        tabs.TabPages.Add(BuildTransfers());
        tabs.TabPages.Add(BuildScripts());
        inputPage = BuildInput();
        tabs.TabPages.Add(inputPage);
        nodesPage = BuildNodes();
        tabs.TabPages.Add(nodesPage);
        tabs.SelectedIndexChanged += delegate {
            if (tabs.SelectedTab == nodesPage && !nodesLoaded && !nodesBusy)
                StartCatalog(new Dictionary<string, object> { { "action", "list" } });
        };
        layout.Controls.Add(tabs, 0, 1);

        footer.Dock = DockStyle.Fill;
        footer.TextAlign = ContentAlignment.MiddleLeft;
        footer.Padding = new Padding(15, 0, 0, 0);
        footer.AutoEllipsis = true;
        footer.Text = "Pronto";
        footer.BackColor = Header;
        footer.ForeColor = Muted;
        layout.Controls.Add(footer, 0, 2);
        if (useSavedInputSettings)
            InitializeInputSettings();
        inputSide.SelectedIndexChanged += ChangeInputSide;
        Shown += delegate {
            if (useSavedInputSettings)
                SetAutomaticInput(autoConnectInput.Checked);
            RefreshStatus();
        };
        FormClosed += delegate { inputRetry.Dispose(); };
    }

    private void InitializeInputSettings()
    {
        int side = ReadInputSetting("ScreenSide", 0);
        inputSide.SelectedIndex = side >= 0 && side < inputSide.Items.Count ? side : 0;
        shareClipboard.Checked = ReadInputSetting("ShareClipboard", 1) != 0;
        autoConnectInput.Checked = ReadInputSetting("AutoConnectInput", 1) != 0;
        inputSide.SelectedIndexChanged += delegate { SaveInputSetting("ScreenSide", inputSide.SelectedIndex); };
        shareClipboard.CheckedChanged += delegate { SaveInputSetting("ShareClipboard", shareClipboard.Checked ? 1 : 0); };
        autoConnectInput.CheckedChanged += delegate {
            SaveInputSetting("AutoConnectInput", autoConnectInput.Checked ? 1 : 0);
            SetAutomaticInput(autoConnectInput.Checked);
        };
        inputRetry.Tick += delegate { TryAutomaticInput(); };
    }

    private static int ReadInputSetting(string name, int fallback)
    {
        try { return Registry.GetValue(InputSettings, name, fallback) as int? ?? fallback; }
        catch (System.Security.SecurityException) { return fallback; }
        catch (UnauthorizedAccessException) { return fallback; }
        catch (IOException) { return fallback; }
    }

    private void SaveInputSetting(string name, int value)
    {
        try { Registry.SetValue(InputSettings, name, value, RegistryValueKind.DWord); }
        catch (Exception error)
        {
            if (!(error is System.Security.SecurityException || error is UnauthorizedAccessException || error is IOException))
                throw;
            footer.Text = "Le preferenze valgono per questa apertura di Linux Dock.";
        }
    }

    private void SetAutomaticInput(bool wanted)
    {
        automaticInputWanted = wanted;
        inputRetry.Enabled = wanted;
        if (wanted)
            TryAutomaticInput();
    }

    private void TryAutomaticInput()
    {
        if (automaticInputWanted && !closePending && !busy && !nodesBusy && inputSession == null && desktopSession == null
            && managedSession == null && !managedUncertain)
            StartInput(true);
    }

    private TabPage BuildNodes()
    {
        var page = Page("Nodi");
        var content = PageLayout(page, 5);
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 78));
        nodesStatus.Text = "Aggiorna il catalogo per leggere i computer registrati.";
        nodesStatus.Dock = DockStyle.Fill;
        nodesStatus.AutoEllipsis = true;
        nodesStatus.ForeColor = Muted;
        nodesStatus.TextAlign = ContentAlignment.MiddleLeft;
        content.Controls.Add(nodesStatus, 0, 0);
        nodeList.View = View.Details;
        nodeList.FullRowSelect = true;
        nodeList.MultiSelect = false;
        nodeList.HideSelection = false;
        nodeList.Dock = DockStyle.Fill;
        nodeList.BackColor = Inset;
        nodeList.ForeColor = Ink;
        nodeList.Columns.Add("Computer", 190);
        nodeList.Columns.Add("Sistema", 85);
        nodeList.Columns.Add("UUID", 250);
        nodeList.Columns.Add("Collegamento", 145);
        nodeList.SelectedIndexChanged += delegate { SetCatalogButtons(); };
        content.Controls.Add(nodeList, 0, 1);
        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true, AutoSize = true };
        nodeAdd = ActionButton("Aggiungi", () => EditNode(null));
        nodeEdit = ActionButton("Modifica", () => EditNode(SelectedNode()), false);
        nodeRemove = ActionButton("Rimuovi", RemoveNode, false);
        nodeRefresh = ActionButton("Aggiorna", () => StartCatalog(new Dictionary<string, object> { { "action", "list" } }), false);
        actions.Controls.AddRange(new Control[] { nodeAdd, nodeEdit, nodeRemove, nodeRefresh });
        content.Controls.Add(actions, 0, 2);
        var invitations = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true, AutoSize = true };
        nodeImport = ActionButton("Importa invito", ImportInvitation, false);
        nodeExport = ActionButton("Esporta invito", () => EditInvitation(null), false);
        nodeCheck = ActionButton("Verifica nodo", () => EditNodeCheck(null), false);
        invitations.Controls.AddRange(new Control[] { nodeImport, nodeExport, nodeCheck });
        content.Controls.Add(invitations, 0, 3);
        var desktopActions = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        desktopActions.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
        desktopActions.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        nodeDesktop = ActionButton("Layout desktop e prerequisiti", EditDesktop, false);
        nodeDesktop.Anchor = AnchorStyles.Left;
        desktopActions.Controls.Add(nodeDesktop, 0, 0);
        desktopActions.Controls.Add(new Label { Text = "Catalogo e layout: le altre schede usano ancora il profilo a due PC.", Dock = DockStyle.Fill,
            ForeColor = Muted, TextAlign = ContentAlignment.MiddleLeft }, 0, 1);
        content.Controls.Add(desktopActions, 0, 4);
        SetCatalogButtons();
        return page;
    }

    private Dictionary<string, object> SelectedNode()
    {
        return nodeList.SelectedItems.Count == 0 ? null
            : (Dictionary<string, object>)nodeList.SelectedItems[0].Tag;
    }

    private void SetCatalogButtons()
    {
        if (nodeAdd == null)
            return;
        var selected = SelectedNode();
        bool available = nodesSnapshot != null && !nodesBusy;
        var registry = nodesSnapshot != null && nodesSnapshot["registry"] != null
            ? NodeCatalog.Map(nodesSnapshot["registry"]) : null;
        nodeAdd.Enabled = available;
        nodeAdd.Text = available && registry == null ? "Registra questo PC" : "Aggiungi";
        nodeEdit.Enabled = available && selected != null;
        nodeRemove.Enabled = available && selected != null && registry != null
            && NodeCatalog.Text(selected, "id") != NodeCatalog.Text(registry, "local");
        nodeRefresh.Enabled = !nodesBusy;
        nodeList.Enabled = !nodesBusy;
        if (nodeDesktop != null)
            nodeDesktop.Enabled = available && registry != null && managedSession == null && !managedUncertain;
        if (nodeImport != null)
            nodeImport.Enabled = nodeExport.Enabled = available && registry != null;
        if (nodeCheck != null)
            nodeCheck.Enabled = available && registry != null && selected != null;
        if (inputGroupButton != null)
            inputGroupButton.Enabled = !nodesBusy && managedSession == null && !managedUncertain;
    }

    private void ShowCatalog(Dictionary<string, object> reply)
    {
        var previous = SelectedNode();
        nodesSnapshot = reply;
        nodesLoaded = true;
        nodeList.Items.Clear();
        if (reply["registry"] == null)
        {
            nodesStatus.Text = "Profilo a due PC. Registra questo computer per creare il catalogo.";
            return;
        }
        var registry = NodeCatalog.Map(reply["registry"]);
        foreach (object entry in NodeCatalog.Items(registry["nodes"]))
        {
            var node = NodeCatalog.Map(entry);
            bool local = NodeCatalog.Text(node, "id") == NodeCatalog.Text(registry, "local");
            string network = local ? "Locale" : "Da configurare";
            if (!local && node.ContainsKey("ssh"))
                network = NodeCatalog.Text(NodeCatalog.Map(node["ssh"]), "network", "SSH");
            else if (!local && node.ContainsKey("legacy_route"))
                network = "Profilo SSH esistente";
            var item = new ListViewItem(NodeCatalog.Text(node, "label"));
            item.SubItems.Add(NodeCatalog.Text(node, "family") == "windows" ? "Windows" : "Linux");
            item.SubItems.Add(NodeCatalog.Text(node, "id"));
            item.SubItems.Add(network);
            item.Tag = node;
            nodeList.Items.Add(item);
            if (previous != null && NodeCatalog.Text(previous, "id") == NodeCatalog.Text(node, "id"))
                item.Selected = true;
        }
        nodesStatus.Text = nodeList.Items.Count + " computer nel catalogo.";
    }

    private async void StartCatalog(Dictionary<string, object> request, Action saved = null, Action<string> failed = null,
        Action<Dictionary<string, object>> received = null)
    {
        if (nodesBusy)
            return;
        nodesBusy = true;
        nodesStatus.Text = "Aggiornamento del catalogo…";
        nodesStatus.ForeColor = Muted;
        SetCatalogButtons();
        try
        {
            var reply = await Task.Run(() => NodeCatalog.Request(request));
            if (reply.ContainsKey("registry"))
                ShowCatalog(reply);
            else
                nodesStatus.Text = "Invito pubblico validato.";
            // Callbacks can open a dialog, but no second request may start until finally.
            if (saved != null)
                saved();
            if (received != null)
                received(reply);
        }
        catch (Exception error)
        {
            nodesStatus.Text = error.Message;
            nodesStatus.ForeColor = Danger;
            if (failed != null)
                failed(error.Message);
        }
        finally
        {
            nodesBusy = false;
            SetCatalogButtons();
            FinishClose();
        }
    }

    private void EditNode(Dictionary<string, object> node)
    {
        if (nodesSnapshot == null || nodesBusy)
            return;
        if (nodeEditor != null)
        {
            nodeEditor.Activate();
            return;
        }
        nodeEditor = new NodeEditor(this, node);
        nodeEditor.FormClosed += delegate { nodeEditor = null; };
        nodeEditor.Show(this);
    }

    private void ImportInvitation()
    {
        if (nodesBusy || nodesSnapshot == null || nodesSnapshot["registry"] == null)
            return;
        if (invitationEditor != null)
        {
            invitationEditor.Activate();
            return;
        }
        try
        {
            using (var dialog = new OpenFileDialog { Filter = "Inviti JSON (*.json)|*.json", Title = "Apri invito pubblico Vincula" })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK)
                    return;
                string raw = NodeCatalog.ReadPublicText(dialog.FileName, 32768);
                StartCatalog(new Dictionary<string, object> { { "action", "enrollment_preview" }, { "invitation_text", raw } },
                    received: reply => EditInvitation(NodeCatalog.Map(reply["invitation"])));
            }
        }
        catch (Exception error)
        {
            nodesStatus.Text = error.Message;
            nodesStatus.ForeColor = Danger;
        }
    }

    private void EditInvitation(Dictionary<string, object> invitation)
    {
        if (nodesSnapshot == null || nodesSnapshot["registry"] == null)
            return;
        if (invitationEditor != null)
        {
            invitationEditor.Activate();
            return;
        }
        invitationEditor = new InvitationEditor(this, invitation);
        invitationEditor.FormClosed += delegate { invitationEditor = null; };
        invitationEditor.Show(this);
    }

    private void EditNodeCheck(string nodeId)
    {
        if (nodesBusy || nodesSnapshot == null || nodesSnapshot["registry"] == null)
            return;
        var registry = NodeCatalog.Map(nodesSnapshot["registry"]);
        var node = nodeId == null ? SelectedNode() : NodeCatalog.Items(registry["nodes"]).Select(NodeCatalog.Map)
            .FirstOrDefault(item => NodeCatalog.Text(item, "id") == nodeId);
        if (node == null)
        {
            nodesStatus.Text = "Seleziona un computer con UUID persistente nel catalogo.";
            return;
        }
        if (nodeCheckEditor != null && !nodeCheckEditor.IsDisposed)
        {
            if (nodeCheckEditor.NodeId == NodeCatalog.Text(node, "id"))
            {
                nodeCheckEditor.Activate();
                nodeCheckEditor.Submit();
                return;
            }
            nodeCheckEditor.Close();
        }
        nodeCheckEditor = new NodeCheckEditor(this, node);
        nodeCheckEditor.FormClosed += delegate { nodeCheckEditor = null; };
        nodeCheckEditor.Show(this);
        nodeCheckEditor.Submit();
    }

    private void RemoveNode()
    {
        var node = SelectedNode();
        if (node == null || nodesSnapshot == null)
            return;
        StartCatalog(new Dictionary<string, object> {
            { "action", "remove" }, { "node_id", node["id"] },
            { "expected_revision", nodesSnapshot["revision"] }
        });
    }

    private void EditDesktop()
    {
        if (managedSession != null || managedUncertain)
        {
            SetGroupStatus("Ferma il gruppo prima di modificarne la disposizione.", "stopping");
            return;
        }
        if (nodesBusy || nodesSnapshot == null || nodesSnapshot["registry"] == null)
            return;
        if (desktopEditor != null)
        {
            desktopEditor.Activate();
            return;
        }
        desktopEditor = new DesktopEditor(this);
        desktopEditor.FormClosed += delegate { desktopEditor = null; };
        desktopEditor.Show(this);
    }

    private void ConfigureDesktopGroup()
    {
        if (managedSession != null || managedUncertain)
        {
            SetGroupStatus("Ferma il gruppo prima di modificarne la disposizione.", "stopping");
            return;
        }
        if (nodesBusy)
            return;
        if (desktopEditor != null)
        {
            desktopEditor.Activate();
            return;
        }
        StartCatalog(new Dictionary<string, object> { { "action", "list" } }, received: reply => {
            if (closePending)
                return;
            // Catalog callbacks run before nodesBusy is cleared in finally.
            BeginInvoke((Action)(() => {
                if (closePending || IsDisposed)
                    return;
                if (nodesSnapshot["registry"] == null)
                {
                    tabs.SelectedTab = nodesPage;
                    nodesStatus.Text = "Registra questo PC nel catalogo prima di configurare il gruppo di schermi.";
                }
                else
                    EditDesktop();
            }));
        });
    }

    private void SetGroupStatus(string text, string state)
    {
        groupStatus.Text = text;
        groupStatus.ForeColor = state == "ready" ? Online : state == "failed" ? Danger
            : state == "stopped" ? Muted : Waiting;
    }

    private void GroupControls()
    {
        bool active = managedSession != null, blocked = active || managedUncertain;
        groupButton.Text = active ? "Ferma gruppo" : blocked ? "Arresto non confermato" : "Avvia gruppo";
        groupButton.Enabled = !managedUncertain;
        StyleButton(groupButton, true, stop: blocked);
        inputGroupButton.Enabled = !blocked && !nodesBusy;
        inputButton.Enabled = !blocked && desktopSession == null;
        inputSide.Enabled = !blocked;
        shareClipboard.Enabled = !blocked && inputSession == null;
        autoConnectInput.Enabled = !blocked;
        desktopButton.Enabled = !blocked && inputSession == null;
        if (blocked)
        {
            inputHeaderButton.Text = active ? "Ferma gruppo" : "Arresto non confermato";
            inputHeaderButton.Enabled = active;
            StyleButton(inputHeaderButton, true, stop: true);
        }
        else if (inputSession == null && desktopSession == null)
        {
            inputHeaderButton.Text = "Attiva condivisione";
            inputHeaderButton.Enabled = true;
            StyleButton(inputHeaderButton, true);
        }
        SetCatalogButtons();
    }

    private async void ToggleManagedGroup()
    {
        if (managedSession != null)
        {
            StopManagedGroup();
            return;
        }
        if (managedUncertain)
        {
            SetGroupStatus("Arresto del gruppo non confermato. Verifica i nodi prima di avviare altre condivisioni.", "failed");
            return;
        }
        if (inputSession != null || desktopSession != null)
        {
            SetGroupStatus("Ferma prima il collegamento attuale a due PC o il desktop remoto, usando il suo pulsante.", "failed");
            return;
        }
        if (desktopEditor != null)
        {
            SetGroupStatus("Salva e chiudi la configurazione prima di avviare il gruppo.", "waiting_ready");
            return;
        }
        inputRetry.Stop(); // Keep the stored preference, but never restart legacy after stopping a group.
        var session = new ManagedGroupSession();
        managedSession = session;
        SetGroupStatus(session.Message, session.State);
        GroupControls();
        try { await Task.Run(() => RunManagedGroup(session)); }
        catch (Exception)
        {
            session.CleanupComplete = false;
            session.State = "failed";
            session.Message = "Il controller del gruppo non ha confermato l’arresto.";
        }
        finally
        {
            if (managedSession == session)
            {
                managedSession = null;
                managedUncertain = !session.CleanupComplete;
                SetGroupStatus(managedUncertain
                    ? "Arresto del gruppo non confermato. Verifica i nodi prima di avviare altre condivisioni."
                    : session.Message, managedUncertain ? "failed" : session.State);
                GroupControls();
                FinishClose();
            }
        }
    }

    private void StopManagedGroup()
    {
        var session = managedSession;
        if (session == null)
            return;
        SetGroupStatus("Arresto del gruppo in corso…", "stopping");
        QueueGroupStop(session, closePending);
    }

    private static void QueueGroupStop(ManagedGroupSession session, bool closeInput = false)
    {
        Process process;
        lock (session.Gate)
        {
            session.StopRequested = true;
            session.CloseInput |= closeInput;
            process = session.Process;
            if (process == null || session.Writing)
                return;
            session.Writing = true;
        }
        Task.Run(() => {
            try
            {
                bool send;
                lock (session.Gate)
                {
                    send = !session.StopSent;
                    session.StopSent = true;
                }
                if (send)
                {
                    process.StandardInput.Write("{\"action\":\"stop\"}\n");
                    process.StandardInput.Flush();
                }
                bool close;
                lock (session.Gate) { close = session.CloseInput; }
                if (close)
                {
                    process.StandardInput.Close();
                    lock (session.Gate) { session.InputClosed = true; }
                }
            }
            catch (IOException) { }
            catch (InvalidOperationException) { }
            finally
            {
                bool finishClose;
                lock (session.Gate)
                {
                    session.Writing = false;
                    finishClose = session.CloseInput && !session.InputClosed;
                    if (finishClose)
                        session.InputClosed = true;
                }
                if (finishClose)
                    try { process.StandardInput.Close(); }
                    catch (IOException) { }
                    catch (InvalidOperationException) { }
            }
        });
    }

    private void PostGroupStatus(ManagedGroupSession session, string state, string message, bool cleanup)
    {
        if (IsDisposed || Disposing || !IsHandleCreated)
            return;
        try
        {
            BeginInvoke((Action)(() => {
                if (managedSession != session || IsDisposed)
                    return;
                if (session.StopRequested && (state == "starting" || state == "waiting_ready" || state == "ready"))
                    return;
                SetGroupStatus(message, state);
                if (state == "failed" && !cleanup)
                    lock (session.Gate) { session.StopSent = false; }
            }));
        }
        catch (InvalidOperationException) { }
    }

    private static string GroupLine(StreamReader reader, Action oversized)
    {
        var result = new StringBuilder();
        bool tooLong = false;
        while (true)
        {
            int character = reader.Read();
            if (character < 0)
                return result.Length == 0 && !tooLong ? null : "";
            if (character == '\n')
                return tooLong ? "" : result.ToString();
            if (result.Length < 16384)
                result.Append((char)character);
            else if (!tooLong)
            {
                tooLong = true;
                oversized();
            }
        }
    }

    private void GroupProtocolFailure(ManagedGroupSession session)
    {
        session.ProtocolFault = session.Terminal = true;
        session.State = "failed";
        session.Message = "Risposta del controller non valida. Arresto richiesto.";
        QueueGroupStop(session);
        PostGroupStatus(session, session.State, session.Message, false);
    }

    private void RunManagedGroup(ManagedGroupSession session)
    {
        Process process = null;
        bool started = false;
        try
        {
            process = new Process { StartInfo = NodeCatalog.DesktopCommand() };
            if (!process.Start())
                throw new IOException("Impossibile avviare il controller del gruppo.");
            started = true;
            lock (session.Gate) { session.Process = process; }
            if (session.StopRequested)
                QueueGroupStop(session);
            Task.Run(() => {
                try
                {
                    var buffer = new char[4096];
                    while (process.StandardError.Read(buffer, 0, buffer.Length) != 0) { }
                }
                catch (IOException) { }
                catch (InvalidOperationException) { }
            });
            var serializer = new JavaScriptSerializer();
            string line;
            while ((line = GroupLine(process.StandardOutput, () => GroupProtocolFailure(session))) != null)
            {
                try
                {
                    var data = serializer.DeserializeObject(line) as Dictionary<string, object>;
                    object type, stateValue, text, cleanupValue;
                    if (data == null || !data.TryGetValue("type", out type) || !(type is string) || (string)type != "status"
                        || !data.TryGetValue("state", out stateValue) || !(stateValue is string)
                        || !data.TryGetValue("message", out text) || !(text is string)
                        || !data.TryGetValue("cleanup_complete", out cleanupValue) || !(cleanupValue is bool))
                        throw new ArgumentException("Invalid desktop status fields");
                    string state = (string)stateValue;
                    if (!new[] { "starting", "waiting_ready", "ready", "stopping", "stopped", "failed" }.Contains(state))
                        throw new ArgumentException("Invalid desktop state");
                    bool active = state == "starting" || state == "waiting_ready" || state == "ready";
                    if ((session.Terminal || session.StopRequested) && active)
                        continue;
                    string message = (string)text;
                    if (message.Length > 2000)
                        message = message.Substring(0, 2000);
                    session.Terminal |= state == "stopped" || state == "failed";
                    session.CleanupComplete = (bool)cleanupValue && (state == "stopped" || state == "failed");
                    session.State = session.ProtocolFault ? "failed" : state;
                    if (!session.ProtocolFault)
                        session.Message = message;
                    PostGroupStatus(session, session.State, session.Message, session.CleanupComplete);
                }
                catch (ArgumentException) { GroupProtocolFailure(session); }
                catch (InvalidOperationException) { GroupProtocolFailure(session); }
            }
            process.WaitForExit(); // Keep ownership of the original handle; never kill/adopt by PID.
        }
        catch (Exception error)
        {
            session.State = "failed";
            session.Message = !started ? error.Message
                : "Il controller del gruppo si è interrotto senza confermare l’arresto.";
            if (!started)
                session.CleanupComplete = true;
            else
            {
                QueueGroupStop(session, true);
                process.WaitForExit();
            }
        }
        finally
        {
            if (process != null)
                process.Dispose();
        }
    }

    private sealed class NodeCheckEditor : Form
    {
        private readonly DockWindow owner;
        private readonly string nodeId;
        internal string NodeId { get { return nodeId; } }
        private string requestRevision, originId;
        private bool running;
        private readonly Dictionary<string, Label> values = new Dictionary<string, Label>();
        private readonly TextBox metadata = ConsoleBox();
        private readonly Label message = new Label { AutoSize = true, Dock = DockStyle.Fill, MaximumSize = new Size(620, 0) };
        private readonly Button checkButton;

        internal NodeCheckEditor(DockWindow parent, Dictionary<string, object> node)
        {
            owner = parent;
            nodeId = NodeCatalog.Text(node, "id");
            Text = "Verifica nodo";
            BackColor = Canvas;
            ForeColor = Ink;
            Font = parent.Font;
            AutoScaleMode = AutoScaleMode.Font;
            MinimumSize = new Size(540, 450);
            ClientSize = new Size(700, 670);
            StartPosition = FormStartPosition.CenterParent;
            var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(16) };
            body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(body);
            var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
            var form = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            viewport.Controls.Add(form);
            body.Controls.Add(viewport, 0, 0);
            var identity = ConsoleBox();
            identity.Text = NodeCatalog.Text(node, "label") + "\r\nUUID " + nodeId;
            identity.WordWrap = true;
            identity.Font = parent.Font;
            identity.MinimumSize = new Size(0, 70);
            form.Controls.Add(identity, 0, 0);
            form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            string[] keys = { "ssh", "identity", "helper", "policy", "worker", "desktop" };
            string[] captions = { "SSH autenticato", "UUID persistente", "Helper su disco", "Permessi osservati", "Worker autonomo", "Desktop" };
            for (int index = 0; index < keys.Length; index++)
            {
                var card = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, ColumnCount = 1,
                    BackColor = Surface, Padding = new Padding(10), Margin = new Padding(0, 0, 0, 8) };
                card.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
                card.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                card.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                card.Controls.Add(new Label { Text = captions[index], AutoSize = true, ForeColor = Muted }, 0, 0);
                var value = new Label { Text = "Non verificato", AutoSize = true, Dock = DockStyle.Fill, MaximumSize = new Size(620, 0) };
                values[keys[index]] = value;
                card.Controls.Add(value, 0, 1);
                form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                form.Controls.Add(card, 0, index + 1);
            }
            metadata.Text = "Nessuna verifica eseguita.";
            metadata.Font = parent.Font;
            metadata.WordWrap = true;
            metadata.MinimumSize = new Size(0, 80);
            form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            form.Controls.Add(metadata, 0, 7);
            message.Text = "La presenza dell’helper non prova l’avvio del servizio. Worker senza sessione grafica e desktop richiedono collaudi separati.";
            message.ForeColor = Muted;
            body.Controls.Add(message, 0, 1);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true };
            actions.Controls.Add(ActionButton("Chiudi", Close, false));
            checkButton = ActionButton("Verifica nodo", Submit);
            actions.Controls.Add(checkButton);
            body.Controls.Add(actions, 0, 2);
        }

        private static string Description(string name, Dictionary<string, object> check)
        {
            string state = NodeCatalog.Text(check, "state"), reason = NodeCatalog.Text(check, "reason");
            if (reason == "termination_unconfirmed") return "Arresto della verifica non confermato";
            if (name == "ssh")
            {
                if (reason == "local_node") return "Nodo locale: SSH non necessario";
                if (reason == "legacy_route_requires_explicit") return "Profilo precedente: configura una rotta SSH esplicita";
                if (state == "authenticated") return "Autenticazione SSH riuscita";
                if (state == "failed") return "Autenticazione o collegamento SSH non riusciti";
            }
            if (name == "identity")
            {
                if (state == "match") return "UUID persistente corrispondente";
                if (state == "mismatch") return "UUID diverso: identità del nodo non confermata";
                if (state == "ambiguous") return "Identità ambigua: verifica la configurazione del destinatario";
                if (state == "unavailable") return "UUID persistente non disponibile";
            }
            if (name == "helper")
            {
                if (state == "compatible_on_disk") return "Helper compatibile su disco; avvio del servizio non provato";
                if (state == "missing") return "Helper non presente";
                if (state == "unverified") return "Compatibilità dell’helper non verificata";
            }
            if (name == "policy" && state == "observed")
            {
                string mode = NodeCatalog.Text(check, "mode");
                string profile = mode == "full" ? "Accesso completo" : mode == "ask" ? "Richiede approvazione" : "Profilo non riconosciuto";
                object enabled;
                string native = check.TryGetValue("native_enabled", out enabled) && enabled is bool
                    ? ((bool)enabled ? "abilitata" : "disabilitata") : "non verificata";
                return profile + "; esecuzione generale " + native;
            }
            if (name == "worker" && state == "unverified") return "Servizio autonomo non verificato";
            if (name == "desktop" && state == "unchecked") return "Consensi e sessione desktop non verificati";
            return "Non verificato";
        }

        internal void Submit()
        {
            if (IsDisposed || Disposing || running)
                return;
            if (owner.nodesBusy)
            {
                message.Text = "Attendi il completamento dell’operazione sul catalogo.";
                return;
            }
            var snapshot = owner.nodesSnapshot;
            var registry = snapshot == null || snapshot["registry"] == null ? null : NodeCatalog.Map(snapshot["registry"]);
            if (registry == null || !NodeCatalog.Items(registry["nodes"]).Select(NodeCatalog.Map).Any(node => NodeCatalog.Text(node, "id") == nodeId))
            {
                message.Text = "Il nodo non è più nel catalogo. Aggiorna l’elenco.";
                return;
            }
            requestRevision = NodeCatalog.Text(snapshot, "revision");
            originId = NodeCatalog.Text(registry, "local");
            running = true;
            checkButton.Enabled = false;
            foreach (var value in values.Values)
                value.Text = "In attesa della verifica";
            metadata.Text = "Revisione richiesta " + requestRevision.Substring(0, Math.Min(12, requestRevision.Length));
            message.Text = "Verifica in corso. Puoi chiudere questa finestra; lo stop della condivisione resta disponibile.";
            owner.StartCatalog(new Dictionary<string, object> { { "action", "enrollment_check" }, { "node_id", nodeId },
                { "expected_revision", requestRevision } }, failed: Failed, received: Finished);
        }

        private void Failed(string error)
        {
            if (IsDisposed || Disposing) return;
            running = false;
            checkButton.Enabled = true;
            foreach (var value in values.Values)
                value.Text = "Verifica non conclusa";
            if (error != null && error.Contains("75 secondi"))
                message.Text = "La verifica non ha risposto entro 75 secondi. Riprova; i permessi restano invariati.";
            else if (error != null && (error.Contains("node_probe.py") || error.Contains("node_enrollment.py")))
                message.Text = "Aggiorna il runtime Vincula per abilitare la verifica del nodo. Il catalogo manuale resta disponibile.";
            else
                message.Text = "Verifica non conclusa. Il catalogo può essere cambiato oppure il runtime richiede un aggiornamento. Aggiorna l’elenco e riprova; i permessi restano invariati.";
        }

        private void Finished(Dictionary<string, object> reply)
        {
            if (IsDisposed || Disposing) return;
            var result = reply.ContainsKey("node_check") ? NodeCatalog.Map(reply["node_check"]) : new Dictionary<string, object>();
            if (NodeCatalog.Text(result, "node_id") != nodeId || NodeCatalog.Text(result, "origin_id") != originId
                || NodeCatalog.Text(result, "revision") != requestRevision || NodeCatalog.Text(reply, "revision") != requestRevision)
            {
                Failed(null);
                return;
            }
            var checks = NodeCatalog.Map(result["checks"]);
            foreach (var entry in values)
                entry.Value.Text = Description(entry.Key, checks.ContainsKey(entry.Key) ? NodeCatalog.Map(checks[entry.Key]) : new Dictionary<string, object>());
            DateTimeOffset date;
            string checkedAt = DateTimeOffset.TryParse(NodeCatalog.Text(result, "checked_at"), out date)
                ? date.ToLocalTime().ToString("dd/MM/yyyy HH:mm:ss") : "Data non disponibile";
            string digest = NodeCatalog.Text(result, "route_digest");
            string route = digest.Length == 64 && digest.All(character => character >= '0' && character <= '9' || character >= 'a' && character <= 'f')
                ? digest.Substring(0, 12) : "non disponibile";
            metadata.Text = "Revisione " + requestRevision.Substring(0, Math.Min(12, requestRevision.Length))
                + " · Rotta " + route + "\r\nVerificato il " + checkedAt;
            running = false;
            checkButton.Enabled = true;
            message.Text = "Resoconto di questa identità e rotta. Permessi invariati. Helper su disco, servizio autonomo e desktop sono verifiche distinte.";
        }
    }

    private sealed class InvitationEditor : Form
    {
        private readonly DockWindow owner;
        private readonly Dictionary<string, object> invitation;
        private readonly object revision;
        private readonly Dictionary<string, TextBox> fields = new Dictionary<string, TextBox>();
        private readonly List<Dictionary<string, TextBox>> endpoints = new List<Dictionary<string, TextBox>>();
        private readonly TableLayoutPanel form = new TableLayoutPanel();
        private readonly TableLayoutPanel endpointForm = new TableLayoutPanel();
        private readonly ComboBox endpoint = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Top };
        private readonly CheckBox verified = new DockCheckBox { Text = "Ho confrontato l’impronta sul nuovo PC", AutoSize = true,
            Padding = new Padding(0, 0, 12, 0) };
        private readonly TextBox message = ConsoleBox();
        private readonly Button submit;
        private readonly Button nodeCheck;
        private bool saving;

        internal InvitationEditor(DockWindow parent, Dictionary<string, object> document)
        {
            owner = parent;
            invitation = document;
            revision = parent.nodesSnapshot["revision"];
            Text = document == null ? "Esporta invito" : "Importa invito";
            BackColor = Canvas;
            ForeColor = Ink;
            Font = parent.Font;
            AutoScaleMode = AutoScaleMode.Font;
            MinimumSize = new Size(540, 500);
            ClientSize = new Size(700, 740);
            StartPosition = FormStartPosition.CenterParent;
            var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(16) };
            body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 106));
            body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(body);
            var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
            form.Dock = DockStyle.Top;
            form.AutoSize = true;
            form.ColumnCount = 1;
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            viewport.Controls.Add(form);
            body.Controls.Add(viewport, 0, 0);
            var registry = NodeCatalog.Map(parent.nodesSnapshot["registry"]);
            if (document != null)
            {
                var node = NodeCatalog.Map(document["node"]);
                var key = NodeCatalog.Map(document["ssh_host_key"]);
                Note(NodeCatalog.Text(node, "label") + " · " + NodeCatalog.Text(node, "family")
                    + "\r\nUUID " + NodeCatalog.Text(node, "id") + "\r\nImpronta host SSH\r\n" + NodeCatalog.Text(key, "fingerprint"), 124);
                endpoint.BackColor = Inset;
                endpoint.ForeColor = Ink;
                endpoint.FlatStyle = FlatStyle.Flat;
                foreach (object item in NodeCatalog.Items(document["endpoints"]))
                {
                    var route = NodeCatalog.Map(item);
                    endpoint.Items.Add(NodeCatalog.Text(route, "user") + "@" + NodeCatalog.Text(route, "host")
                        + ":" + NodeCatalog.Text(route, "port") + " · " + NodeCatalog.Text(route, "network"));
                }
                endpoint.SelectedIndex = 0;
                AddControl(new Label { Text = "Indirizzo da usare da questo PC", AutoSize = true });
                AddControl(endpoint);
                var identities = new List<string[]>();
                foreach (object item in NodeCatalog.Items(registry["nodes"]))
                {
                    var nodeEntry = NodeCatalog.Map(item);
                    if (!nodeEntry.ContainsKey("ssh"))
                        continue;
                    var ssh = NodeCatalog.Map(nodeEntry["ssh"]);
                    string identity = NodeCatalog.Text(ssh, "identity_file");
                    string bind = NodeCatalog.Text(ssh, "bind_address");
                    if (identity.Length != 0 && !identities.Any(pair => pair[0] == identity && pair[1] == bind))
                        identities.Add(new[] { identity, bind });
                }
                AddField("identity_file", "Identità SSH su questo PC (solo percorso)", identities.Count == 1 ? identities[0][0] : "", true);
                AddField("bind_address", "Indirizzo locale di partenza (facoltativo)", identities.Count == 1 ? identities[0][1] : "");
                Note("La chiave pubblica di questa origine deve essere già autorizzata sul nuovo PC. Il contenuto dell’identità locale non viene letto. L’agent esplicito non è supportato su Windows.", 92);
                verified.MaximumSize = new Size(540, 0);
                verified.CheckedChanged += delegate { submit.Enabled = verified.Checked && !saving; };
                AddControl(verified);
            }
            else
            {
                var local = NodeCatalog.Items(registry["nodes"]).Select(NodeCatalog.Map)
                    .First(node => NodeCatalog.Text(node, "id") == NodeCatalog.Text(registry, "local"));
                Note("Invito pubblico di " + NodeCatalog.Text(local, "label") + "\r\nUUID persistente " + NodeCatalog.Text(local, "id")
                    + "\r\nIndica indirizzi raggiungibili e chiave host pubblica del listener SSH effettivo.", 114);
                endpointForm.Dock = DockStyle.Top;
                endpointForm.AutoSize = true;
                endpointForm.ColumnCount = 1;
                endpointForm.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
                AddControl(endpointForm);
                AddEndpoint();
                AddControl(ActionButton("Aggiungi indirizzo", AddEndpoint, false));
                AddField("public_host_key", "Chiave host SSH pubblica (.pub)", "", true, true);
                Note("Solo dati pubblici. Non esporta credenziali e non configura SSH, worker o desktop.", 72);
            }
            message.WordWrap = true;
            message.Font = parent.Font;
            message.Text = "La registrazione lascia SSH, runtime e desktop da verificare.";
            body.Controls.Add(message, 0, 1);
            var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = true };
            actions.Controls.Add(ActionButton("Chiudi", Close, false));
            submit = ActionButton(document == null ? "Genera e salva invito" : "Registra nodo", Submit);
            submit.Enabled = document == null;
            actions.Controls.Add(submit);
            if (document != null)
            {
                nodeCheck = ActionButton("Verifica nodo", () => parent.EditNodeCheck(NodeCatalog.Text(NodeCatalog.Map(document["node"]), "id")), false);
                nodeCheck.Enabled = false;
                actions.Controls.Add(nodeCheck);
            }
            body.Controls.Add(actions, 0, 2);
            FormClosing += delegate(object sender, FormClosingEventArgs args) { args.Cancel = saving; };
        }

        private void AddControl(Control control)
        {
            int row = form.RowCount++;
            form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            control.Margin = new Padding(0, 0, 0, 9);
            form.Controls.Add(control, 0, row);
        }

        private void Note(string text, int height)
        {
            var note = ConsoleBox();
            note.Font = owner.Font;
            note.Text = text;
            note.WordWrap = true;
            note.ForeColor = Muted;
            note.MinimumSize = new Size(0, height);
            note.Height = height;
            AddControl(note);
        }

        private void AddField(string key, string caption, string value, bool browse = false, bool publicKey = false)
        {
            var entry = new TextBox { Text = value };
            fields[key] = entry;
            Action choose = delegate {
                using (var dialog = new OpenFileDialog { Title = caption,
                    Filter = publicKey ? "Chiavi pubbliche SSH (*.pub)|*.pub" : "Tutti i file (*.*)|*.*" })
                    if (dialog.ShowDialog(this) == DialogResult.OK)
                        entry.Text = dialog.FileName;
            };
            AddControl(Field(caption, entry, browse ? choose : null));
        }

        private void AddEndpoint()
        {
            var group = new GroupBox { Text = "Indirizzo " + (endpoints.Count + 1), Dock = DockStyle.Top,
                AutoSize = true, ForeColor = Ink, BackColor = Surface, Padding = new Padding(10) };
            var grid = new TableLayoutPanel { ColumnCount = 1, Dock = DockStyle.Top, AutoSize = true };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            var entries = new Dictionary<string, TextBox>();
            string[] keys = { "host", "user", "port", "network" };
            string[] labels = { "Host / IP", "Utente SSH", "Porta", "Rete" };
            for (int index = 0; index < keys.Length; index++)
            {
                var entry = new TextBox { Text = keys[index] == "port" ? "22" : "" };
                entries[keys[index]] = entry;
                grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                grid.Controls.Add(Field(labels[index], entry), 0, index);
            }
            group.Controls.Add(grid);
            int row = endpointForm.RowCount++;
            endpointForm.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            endpointForm.Controls.Add(group, 0, row);
            endpoints.Add(entries);
        }

        private void Submit()
        {
            if (saving || owner.nodesBusy)
                return;
            try
            {
                Dictionary<string, object> payload;
                if (invitation != null)
                {
                    if (!verified.Checked)
                        return;
                    if (String.IsNullOrWhiteSpace(fields["identity_file"].Text))
                        throw new IOException("Indica il percorso dell’identità SSH locale.");
                    var connection = new Dictionary<string, object>();
                    foreach (var field in fields)
                        if (!String.IsNullOrWhiteSpace(field.Value.Text))
                            connection[field.Key] = field.Value.Text.Trim();
                    payload = new Dictionary<string, object> { { "action", "enrollment_import" }, { "invitation", invitation },
                        { "expected_revision", revision }, { "expected_fingerprint", NodeCatalog.Map(invitation["ssh_host_key"])["fingerprint"] },
                        { "endpoint_index", endpoint.SelectedIndex }, { "local_connection", connection } };
                }
                else
                {
                    var routes = new List<object>();
                    foreach (var entries in endpoints)
                    {
                        int port;
                        if (!Int32.TryParse(entries["port"].Text, out port))
                            throw new IOException("La porta SSH deve essere un numero intero.");
                        routes.Add(new Dictionary<string, object> { { "host", entries["host"].Text.Trim() },
                            { "user", entries["user"].Text.Trim() }, { "port", port }, { "network", entries["network"].Text.Trim() } });
                    }
                    string key = NodeCatalog.ReadPublicText(fields["public_host_key"].Text, 8192, true);
                    payload = new Dictionary<string, object> { { "action", "enrollment_export" }, { "endpoints", routes }, { "public_host_key", key } };
                }
                saving = true;
                form.Enabled = submit.Enabled = false;
                message.Text = "Validazione dell’invito…";
                owner.StartCatalog(payload, failed: Fail, received: Finish);
            }
            catch (Exception error) { Fail(error.Message); }
        }

        private void Fail(string error)
        {
            saving = false;
            form.Enabled = true;
            submit.Enabled = invitation == null || verified.Checked;
            message.Text = error;
        }

        private void Finish(Dictionary<string, object> reply)
        {
            saving = false;
            if (invitation != null)
            {
                object steps;
                string details = "";
                if (reply.TryGetValue("next_steps", out steps) && steps is object[])
                    details = String.Join(Environment.NewLine, ((object[])steps).Select(Convert.ToString));
                message.Text = "Nodo registrato. SSH, runtime e desktop NON verificati.\r\n"
                    + details;
                owner.nodesStatus.Text = "Nodo registrato; collegamento, runtime e desktop da verificare.";
                nodeCheck.Enabled = true;
                return;
            }
            try
            {
                using (var dialog = new SaveFileDialog { Filter = "Invito JSON (*.json)|*.json", FileName = "vincula-invito.json",
                    Title = "Salva invito pubblico", OverwritePrompt = true })
                {
                    if (dialog.ShowDialog(this) != DialogResult.OK)
                    {
                        message.Text = "Invito validato; salvataggio annullato.";
                        return;
                    }
                    File.WriteAllText(dialog.FileName, new JavaScriptSerializer().Serialize(reply["invitation"]), new UTF8Encoding(false));
                    message.Text = "Invito pubblico salvato. Confronta l’impronta sul PC che lo importerà.";
                }
            }
            catch (Exception error) { message.Text = "Impossibile salvare l’invito: " + error.Message; }
            finally
            {
                form.Enabled = submit.Enabled = true;
            }
        }
    }

    private sealed class DesktopEditor : Form
    {
        private sealed class Choice
        {
            internal string Id, Label;
            public override string ToString() { return Label + " · " + Id.Substring(0, 8); }
        }
        private sealed class Member
        {
            internal string Id, Label;
            internal CheckBox Selected;
            internal TextBox X, Y;
        }
        private readonly DockWindow owner;
        private readonly object revision;
        private readonly ComboBox primary = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Fill };
        private readonly CheckBox clipboard = new DockCheckBox { Text = "Condividi gli appunti tra i membri", AutoSize = true };
        private readonly ComboBox screenChoice = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 250 };
        private readonly List<Member> members = new List<Member>();
        private readonly List<Button> buttons = new List<Button>();
        private readonly List<Button> moveButtons = new List<Button>();
        private readonly TableLayoutPanel form = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
        private readonly TableLayoutPanel map = new TableLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        private readonly Label mapStatus = new Label { AutoSize = true, ForeColor = Muted };
        private readonly ToolTip tips = new ToolTip();
        private readonly TextBox preview = ConsoleBox();
        private readonly CheckBox details = new DockCheckBox { Text = "Dettagli della verifica", AutoSize = true };
        private readonly Label message = new Label { AutoSize = true };
        private bool saving, adjusting;
        private string currentPrimary;

        internal DesktopEditor(DockWindow parent)
        {
            owner = parent;
            revision = parent.nodesSnapshot["revision"];
            Text = "Configura gruppo di schermi";
            BackColor = Canvas;
            ForeColor = Ink;
            Font = parent.Font;
            MinimumSize = new Size(650, 520);
            ClientSize = new Size(810, 720);
            StartPosition = FormStartPosition.CenterParent;
            var registry = NodeCatalog.Map(parent.nodesSnapshot["registry"]);
            var profile = registry.ContainsKey("desktop") ? NodeCatalog.Map(registry["desktop"]) : null;
            string primaryId = profile == null ? NodeCatalog.Text(registry, "local") : NodeCatalog.Text(profile, "primary");
            currentPrimary = primaryId;
            clipboard.Checked = profile == null || (bool)profile["clipboard"];
            var positions = new Dictionary<string, Dictionary<string, object>>();
            if (profile != null)
                foreach (object item in NodeCatalog.Items(profile["members"]))
                {
                    var entry = NodeCatalog.Map(item);
                    positions[NodeCatalog.Text(entry, "node_id")] = entry;
                }
            var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(16) };
            body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(body);
            var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            viewport.Controls.Add(form);
            body.Controls.Add(viewport, 0, 0);
            var guide = new Label { Text = "Scegli il PC con mouse e tastiera, poi disponi gli schermi. Ogni PC rappresenta il suo desktop logico. Salva layout prepara la configurazione: non avvia la condivisione multinodo.", AutoSize = true, ForeColor = Muted };
            AddRow(guide);
            AddRow(new Label { Text = "Principale (mouse e tastiera collegati qui)", AutoSize = true });
            primary.BackColor = Inset;
            primary.ForeColor = Ink;
            primary.FlatStyle = FlatStyle.Flat;
            AddRow(primary);
            AddRow(clipboard);
            AddRow(new Label { Text = "Computer inclusi nel gruppo", AutoSize = true });
            var memberList = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
            memberList.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            AddRow(memberList);
            AddRow(new Label { Text = "Disposizione degli schermi · anteprima", AutoSize = true });
            var move = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true };
            screenChoice.BackColor = Inset;
            screenChoice.ForeColor = Ink;
            screenChoice.FlatStyle = FlatStyle.Flat;
            screenChoice.AccessibleName = "Schermo da spostare";
            move.Controls.Add(screenChoice);
            foreach (var entry in new[] { new { Text = "←", X = -1, Y = 0 }, new { Text = "→", X = 1, Y = 0 },
                new { Text = "↑", X = 0, Y = -1 }, new { Text = "↓", X = 0, Y = 1 } })
            {
                int dx = entry.X, dy = entry.Y;
                var button = ActionButton(entry.Text, () => MoveScreen(dx, dy), false);
                tips.SetToolTip(button, "Sposta lo schermo selezionato. Una cella occupata scambia i due schermi.");
                moveButtons.Add(button);
                move.Controls.Add(button);
            }
            AddRow(move);
            var mapViewport = new Panel { Dock = DockStyle.Top, Height = 205, AutoScroll = true, BackColor = Inset };
            mapViewport.Controls.Add(map);
            AddRow(mapViewport);
            AddRow(mapStatus);
            var grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 3, RowCount = 1 };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 70));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 70));
            grid.Controls.Add(new Label { Text = "Computer", AutoSize = true }, 0, 0);
            grid.Controls.Add(new Label { Text = "X", AutoSize = true }, 1, 0);
            grid.Controls.Add(new Label { Text = "Y", AutoSize = true }, 2, 0);
            var advancedToggle = new DockCheckBox { Text = "Coordinate avanzate", AutoSize = true };
            AddRow(advancedToggle);
            grid.Visible = false;
            advancedToggle.CheckedChanged += delegate { grid.Visible = advancedToggle.Checked; };
            AddRow(grid);
            foreach (object item in NodeCatalog.Items(registry["nodes"]))
            {
                var node = NodeCatalog.Map(item);
                string id = NodeCatalog.Text(node, "id");
                var choice = new Choice { Id = id, Label = NodeCatalog.Text(node, "label") };
                primary.Items.Add(choice);
                screenChoice.Items.Add(choice);
                if (id == primaryId)
                {
                    primary.SelectedItem = choice;
                    screenChoice.SelectedItem = choice;
                }
                Dictionary<string, object> position;
                positions.TryGetValue(id, out position);
                bool selected = position != null || profile == null && id == primaryId;
                var member = new Member { Id = id, Label = choice.Label,
                    Selected = new DockCheckBox { Text = choice.ToString(), Checked = selected, Dock = DockStyle.Fill,
                        Height = Font.Height + 10, AutoEllipsis = true },
                    X = new TextBox { Text = position == null ? "0" : NodeCatalog.Text(position, "x"), Dock = DockStyle.Fill, BackColor = Inset, ForeColor = Ink },
                    Y = new TextBox { Text = position == null ? "0" : NodeCatalog.Text(position, "y"), Dock = DockStyle.Fill, BackColor = Inset, ForeColor = Ink } };
                int row = grid.RowCount++;
                grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                var name = new Label { Text = member.Label, Dock = DockStyle.Fill, AutoEllipsis = true,
                    TextAlign = ContentAlignment.MiddleLeft, MinimumSize = new Size(0, Font.Height + 8) };
                tips.SetToolTip(name, choice.ToString());
                tips.SetToolTip(member.Selected, choice.ToString());
                grid.Controls.Add(name, 0, row);
                grid.Controls.Add(member.X, 1, row);
                grid.Controls.Add(member.Y, 2, row);
                int selectedRow = memberList.RowCount++;
                memberList.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                memberList.Controls.Add(member.Selected, 0, selectedRow);
                members.Add(member);
            }
            AddRow(details);
            preview.Dock = DockStyle.Top;
            preview.Height = 160;
            preview.Visible = false;
            details.CheckedChanged += delegate { preview.Visible = details.Checked; };
            AddRow(preview);
            message.Text = "Anteprima e controlli locali; accesso remoto e TLS restano da verificare.";
            message.ForeColor = Muted;
            message.Dock = DockStyle.Fill;
            message.Margin = new Padding(0, 10, 0, 10);
            body.Controls.Add(message, 0, 1);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = true, AutoSize = true };
            buttons.Add(ActionButton("Anteprima", () => Submit("desktop_preview")));
            buttons.Add(ActionButton("Salva layout", () => Submit("desktop"), false));
            buttons.Add(ActionButton("Rimuovi layout", () => Submit("desktop", true), false));
            buttons.Add(ActionButton("Chiudi", Close, false));
            foreach (var button in buttons)
                actions.Controls.Add(button);
            body.Controls.Add(actions, 0, 2);
            foreach (var member in members)
            {
                Member captured = member;
                member.Selected.CheckedChanged += delegate { SelectMember(captured); };
                member.X.TextChanged += delegate { UpdateMap(); };
                member.Y.TextChanged += delegate { UpdateMap(); };
            }
            primary.SelectedIndexChanged += delegate { ChangePrimary(); };
            screenChoice.SelectedIndexChanged += delegate { UpdateMap(); };
            viewport.Resize += delegate {
                int width = Math.Max(200, viewport.ClientSize.Width - SystemInformation.VerticalScrollBarWidth - 12);
                guide.MaximumSize = mapStatus.MaximumSize = new Size(width, 0);
                message.MaximumSize = new Size(Math.Max(200, body.ClientSize.Width - 32), 0);
            };
            FormClosed += delegate { tips.Dispose(); };
            UpdateMap();
            FormClosing += delegate(object sender, FormClosingEventArgs args) { args.Cancel = saving; };
        }

        private void AddRow(Control control)
        {
            int row = form.RowCount++;
            form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            control.Dock = DockStyle.Top;
            control.Margin = new Padding(0, 0, 0, 10);
            form.Controls.Add(control, 0, row);
        }

        private Dictionary<string, Point> Positions()
        {
            var result = new Dictionary<string, Point>();
            foreach (var member in members.Where(item => item.Selected.Checked))
            {
                int x, y;
                if (!Int32.TryParse(member.X.Text, out x) || !Int32.TryParse(member.Y.Text, out y))
                    throw new ArgumentException("Le coordinate X e Y devono essere numeri interi a 32 bit.");
                result[member.Id] = new Point(x, y);
            }
            return result;
        }

        private Point FreePosition(string excluded)
        {
            var occupied = new HashSet<Point>(Positions().Where(item => item.Key != excluded).Select(item => item.Value));
            if (occupied.Count == 0)
                return new Point(0, 0);
            foreach (var point in occupied.OrderBy(item => item.Y).ThenBy(item => item.X))
                foreach (var direction in new[] { new Point(1, 0), new Point(-1, 0), new Point(0, 1), new Point(0, -1) })
                {
                    long x = (long)point.X + direction.X, y = (long)point.Y + direction.Y;
                    if (x < Int32.MinValue || x > Int32.MaxValue || y < Int32.MinValue || y > Int32.MaxValue)
                        continue;
                    var next = new Point((int)x, (int)y);
                    if (!occupied.Contains(next))
                        return next;
                }
            throw new ArgumentException("Nessuna cella adiacente disponibile.");
        }

        private void SelectMember(Member member)
        {
            if (adjusting)
                return;
            if (member.Selected.Checked)
            {
                try
                {
                    Point position = FreePosition(member.Id);
                    adjusting = true;
                    member.X.Text = position.X.ToString();
                    member.Y.Text = position.Y.ToString();
                    screenChoice.SelectedItem = screenChoice.Items.Cast<Choice>().First(item => item.Id == member.Id);
                }
                catch (ArgumentException error)
                {
                    adjusting = true;
                    member.Selected.Checked = false;
                    message.Text = error.Message;
                }
                finally { adjusting = false; }
            }
            UpdateMap();
        }

        private void ChangePrimary()
        {
            if (adjusting)
                return;
            var choice = primary.SelectedItem as Choice;
            if (choice == null)
                return;
            var member = members.First(item => item.Id == choice.Id);
            member.Selected.Checked = true;
            if (!member.Selected.Checked)
            {
                adjusting = true;
                primary.SelectedItem = primary.Items.Cast<Choice>().First(item => item.Id == currentPrimary);
                adjusting = false;
                return;
            }
            currentPrimary = choice.Id;
            UpdateMap();
        }

        private void MoveScreen(int dx, int dy)
        {
            try
            {
                var positions = Positions();
                var choice = screenChoice.SelectedItem as Choice;
                if (choice == null || !positions.ContainsKey(choice.Id))
                    return;
                Point before = positions[choice.Id];
                var target = new Point(checked(before.X + dx), checked(before.Y + dy));
                string other = positions.Where(item => item.Value == target).Select(item => item.Key).FirstOrDefault();
                adjusting = true;
                foreach (var member in members)
                    if (member.Id == choice.Id || member.Id == other)
                    {
                        Point value = member.Id == choice.Id ? target : before;
                        member.X.Text = value.X.ToString();
                        member.Y.Text = value.Y.ToString();
                    }
            }
            catch (ArgumentException error) { message.Text = error.Message; }
            catch (OverflowException) { message.Text = "La posizione supera il limite delle coordinate."; }
            finally { adjusting = false; }
            UpdateMap();
        }

        private void UpdateMap()
        {
            if (adjusting)
                return;
            foreach (Control child in map.Controls.Cast<Control>().ToArray())
                child.Dispose();
            map.Controls.Clear();
            map.ColumnStyles.Clear();
            map.RowStyles.Clear();
            foreach (var member in members)
            {
                member.Selected.Enabled = member.Id != currentPrimary;
                member.X.Enabled = member.Y.Enabled = member.Selected.Checked;
            }
            Dictionary<string, Point> positions;
            try { positions = Positions(); }
            catch (ArgumentException error)
            {
                mapStatus.Text = error.Message;
                foreach (var button in moveButtons)
                    button.Enabled = false;
                return;
            }
            var choice = screenChoice.SelectedItem as Choice;
            foreach (var button in moveButtons)
                button.Enabled = choice != null && positions.ContainsKey(choice.Id);
            if (positions.Count == 0)
            {
                mapStatus.Text = "Seleziona almeno un computer per comporre il gruppo.";
                return;
            }
            var cells = new HashSet<Point>(positions.Values);
            bool duplicate = cells.Count != positions.Count;
            int left = cells.Min(point => point.X), top = cells.Min(point => point.Y);
            bool distant = (long)cells.Max(point => point.X) - left > 64 || (long)cells.Max(point => point.Y) - top > 64;
            var reached = new HashSet<Point>();
            var pending = new Queue<Point>();
            pending.Enqueue(cells.First());
            while (pending.Count != 0)
            {
                Point point = pending.Dequeue();
                if (!reached.Add(point))
                    continue;
                foreach (var next in cells)
                    if (Math.Abs((long)next.X - point.X) + Math.Abs((long)next.Y - point.Y) == 1)
                        pending.Enqueue(next);
            }
            int line = Font.Height + 5, cardWidth = 188, cardHeight = line * 3 + 24;
            map.ColumnCount = duplicate || distant ? 1 : cells.Max(point => point.X) - left + 1;
            map.RowCount = duplicate || distant ? positions.Count : cells.Max(point => point.Y) - top + 1;
            for (int column = 0; column < map.ColumnCount; column++)
                map.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, cardWidth + 10));
            for (int row = 0; row < map.RowCount; row++)
                map.RowStyles.Add(new RowStyle(SizeType.Absolute, cardHeight + 10));
            int index = 0;
            foreach (var member in members.Where(item => item.Selected.Checked))
            {
                Point position = positions[member.Id];
                bool isPrimary = member.Id == currentPrimary;
                var panel = new Panel { Size = new Size(cardWidth, cardHeight), Margin = new Padding(5), BackColor = Surface };
                panel.Paint += delegate(object sender, PaintEventArgs args) {
                    using (var pen = new Pen(isPrimary ? Cyan : Line))
                        args.Graphics.DrawRectangle(pen, 0, 0, panel.Width - 1, panel.Height - 1);
                };
                var name = new Label { Text = member.Label, Location = new Point(12, 9),
                    Size = new Size(cardWidth - 24, line), ForeColor = isPrimary ? Cyan : Ink, AutoEllipsis = true };
                tips.SetToolTip(name, member.Label + "\n" + member.Id);
                panel.Controls.Add(name);
                panel.Controls.Add(new Label { Text = isPrimary ? "PC principale" : "Schermo secondario",
                    Location = new Point(12, 9 + line), Size = new Size(cardWidth - 24, line), ForeColor = Muted });
                panel.Controls.Add(new Label { Text = "X " + position.X + " · Y " + position.Y,
                    Location = new Point(12, 9 + line * 2), Size = new Size(cardWidth - 24, line), ForeColor = Muted,
                    AutoEllipsis = true });
                map.Controls.Add(panel, duplicate || distant ? 0 : position.X - left,
                    duplicate || distant ? index : position.Y - top);
                index++;
            }
            mapStatus.Text = duplicate ? "Due schermi occupano la stessa cella. Correggi la posizione prima di salvare."
                : distant || reached.Count != cells.Count ? "Il gruppo ha un vuoto: avvicina gli schermi lungo un lato prima di salvare."
                : "Anteprima della disposizione. Le frecce spostano il PC selezionato o scambiano una cella occupata.";
        }

        private Dictionary<string, object> Profile()
        {
            var selected = new List<object>();
            foreach (var member in members)
                if (member.Selected.Checked)
                {
                    int x, y;
                    if (!Int32.TryParse(member.X.Text, out x) || !Int32.TryParse(member.Y.Text, out y))
                        throw new ArgumentException("Le coordinate X e Y devono essere numeri interi a 32 bit.");
                    selected.Add(new Dictionary<string, object> { { "node_id", member.Id }, { "x", x }, { "y", y } });
                }
            var choice = primary.SelectedItem as Choice;
            return new Dictionary<string, object> { { "primary", choice == null ? null : choice.Id },
                { "members", selected }, { "clipboard", clipboard.Checked } };
        }

        private void Finish(string error = null)
        {
            saving = false;
            form.Enabled = true;
            foreach (var button in buttons)
                button.Enabled = true;
            if (error != null)
            {
                message.Text = error;
                return;
            }
            var snapshot = owner.nodesSnapshot;
            if (!snapshot.ContainsKey("desktop_preview"))
            {
                Close();
                return;
            }
            var result = NodeCatalog.Map(snapshot["desktop_preview"]);
            var report = NodeCatalog.Map(result["preflight"]);
            var names = new Dictionary<string, string>();
            foreach (object item in NodeCatalog.Items(NodeCatalog.Map(snapshot["registry"])["nodes"]))
            {
                var node = NodeCatalog.Map(item);
                names[NodeCatalog.Text(node, "id")] = NodeCatalog.Text(node, "label");
            }
            var lines = new StringBuilder(NodeCatalog.Text(report, "summary") + "\r\n\r\n");
            foreach (object item in NodeCatalog.Items(report["checks"]))
            {
                var check = NodeCatalog.Map(item);
                string name;
                if (!names.TryGetValue(NodeCatalog.Text(check, "node_id"), out name))
                    name = "Vincula";
                lines.AppendLine("[" + NodeCatalog.Text(check, "status") + "] " + name + ": " + NodeCatalog.Text(check, "message"));
            }
            lines.AppendLine("\r\nLAYOUT DESKFLOW\r\n" + NodeCatalog.Text(result, "layout"));
            lines.AppendLine("OPZIONI PREVISTE (non avviabili)\r\n" + new JavaScriptSerializer().Serialize(result["server_options"]));
            preview.Text = lines.ToString();
            details.Checked = true;
            message.Text = "Layout valido. Condivisione multinodo non attiva: consulta i prerequisiti sopra.";
        }

        private void Submit(string action, bool clear = false)
        {
            if (saving || owner.nodesBusy)
                return;
            object profile;
            try { profile = clear ? null : Profile(); }
            catch (ArgumentException error) { message.Text = error.Message; return; }
            saving = true;
            form.Enabled = false;
            foreach (var button in buttons)
                button.Enabled = false;
            owner.StartCatalog(new Dictionary<string, object> { { "action", action }, { "desktop", profile },
                { "expected_revision", revision } }, () => Finish(), error => Finish(error));
        }
    }

    private sealed class NodeEditor : Form
    {
        private readonly DockWindow owner;
        private readonly Dictionary<string, object> node;
        private readonly Dictionary<string, TextBox> fields = new Dictionary<string, TextBox>();
        private readonly ComboBox family = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
        private readonly ComboBox route = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
        private readonly TableLayoutPanel form = new TableLayoutPanel();
        private readonly Label message = new Label();
        private readonly object revision;
        private readonly bool local;
        private readonly Button save;
        private bool saving;

        internal NodeEditor(DockWindow parent, Dictionary<string, object> existing)
        {
            owner = parent;
            node = existing;
            revision = parent.nodesSnapshot["revision"];
            var registry = parent.nodesSnapshot["registry"] == null ? null : NodeCatalog.Map(parent.nodesSnapshot["registry"]);
            local = registry == null || existing != null
                && NodeCatalog.Text(existing, "id") == NodeCatalog.Text(registry, "local");
            var current = existing ?? new Dictionary<string, object>();
            var ssh = current.ContainsKey("ssh") ? NodeCatalog.Map(current["ssh"]) : new Dictionary<string, object>();
            Text = existing == null ? "Aggiungi nodo" : "Modifica nodo";
            BackColor = Canvas;
            ForeColor = Ink;
            Font = parent.Font;
            MinimumSize = new Size(500, 520);
            ClientSize = new Size(580, 680);
            StartPosition = FormStartPosition.CenterParent;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(16) };
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 66));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
            Controls.Add(layout);
            var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
            form.Dock = DockStyle.Top;
            form.ColumnCount = 1;
            form.AutoSize = true;
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            viewport.Controls.Add(form);
            layout.Controls.Add(viewport, 0, 0);
            string suggestedId = "";
            if (local)
                suggestedId = Guid.NewGuid().ToString();
            AddField("id", "UUID del computer", NodeCatalog.Text(current, "id", suggestedId), existing == null);
            AddField("label", "Nome", NodeCatalog.Text(current, "label"));
            family.Items.AddRange(new object[] { "Linux", "Windows" });
            family.SelectedIndex = NodeCatalog.Text(current, "family", "windows") == "windows" ? 1 : 0;
            family.Enabled = !local;
            AddChoice("Sistema", family);
            if (local)
                route.Items.Add("Questo computer");
            else
            {
                route.Items.AddRange(new object[] { "Da configurare", "SSH esplicito" });
                if (current.ContainsKey("legacy_route"))
                    route.Items.Add("Profilo SSH esistente");
            }
            route.SelectedIndex = 0;
            if (!local && current.ContainsKey("ssh"))
                route.SelectedIndex = 1;
            if (!local && current.ContainsKey("legacy_route"))
                route.SelectedIndex = 2;
            AddChoice("Collegamento", route);
            AddField("host", "Indirizzo SSH", NodeCatalog.Text(ssh, "host"));
            AddField("port", "Porta SSH", NodeCatalog.Text(ssh, "port", "22"));
            AddField("user", "Utente SSH", NodeCatalog.Text(ssh, "user"));
            AddField("identity_file", "Chiave SSH su questo PC", NodeCatalog.Text(ssh, "identity_file"));
            AddField("known_hosts_file", "known_hosts su questo PC", NodeCatalog.Text(ssh, "known_hosts_file"));
            AddField("network", "Rete", NodeCatalog.Text(ssh, "network"));
            route.SelectedIndexChanged += delegate { SetRouteFields(); };
            SetRouteFields();
            message.Text = local ? "Registra l’identità di questo computer."
                : "Usa l’UUID verificato del computer remoto. Il salvataggio configura il percorso, senza collaudare SSH.";
            message.ForeColor = Muted;
            message.Dock = DockStyle.Fill;
            layout.Controls.Add(message, 0, 1);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false };
            actions.Controls.Add(ActionButton("Chiudi", Close, false));
            save = ActionButton("Salva nodo", Save);
            actions.Controls.Add(save);
            layout.Controls.Add(actions, 0, 2);
            FormClosing += delegate(object sender, FormClosingEventArgs args) { args.Cancel = saving; };
        }

        private void AddField(string key, string caption, string value, bool editable = true)
        {
            var entry = new TextBox { Text = value, ReadOnly = !editable };
            fields[key] = entry;
            AddControl(Field(caption, entry));
        }

        private void AddChoice(string caption, ComboBox selector)
        {
            var box = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
            box.Controls.Add(new Label { Text = caption, AutoSize = true }, 0, 0);
            selector.Dock = DockStyle.Top;
            selector.FlatStyle = FlatStyle.Flat;
            selector.BackColor = Inset;
            selector.ForeColor = Ink;
            selector.AccessibleName = caption;
            box.Controls.Add(selector, 0, 1);
            AddControl(box);
        }

        private void AddControl(Control control)
        {
            int row = form.RowCount++;
            form.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            control.Margin = new Padding(0, 0, 0, 8);
            form.Controls.Add(control, 0, row);
        }

        private void SetRouteFields()
        {
            bool explicitRoute = !local && route.SelectedIndex == 1;
            foreach (string key in new[] { "host", "port", "user", "identity_file", "known_hosts_file", "network" })
                fields[key].Enabled = explicitRoute;
        }

        private Dictionary<string, object> SshFields()
        {
            int port;
            if (!Int32.TryParse(fields["port"].Text, out port))
                throw new FormatException("La porta SSH deve essere un numero intero.");
            var ssh = node != null && node.ContainsKey("ssh")
                ? new Dictionary<string, object>(NodeCatalog.Map(node["ssh"])) : new Dictionary<string, object>();
            foreach (string key in new[] { "host", "user", "identity_file", "known_hosts_file" })
                ssh[key] = fields[key].Text.Trim();
            ssh["port"] = port;
            if (String.IsNullOrWhiteSpace(fields["network"].Text))
                ssh.Remove("network");
            else
                ssh["network"] = fields["network"].Text.Trim();
            return ssh;
        }

        private Dictionary<string, object> RequestPayload()
        {
            var changes = new Dictionary<string, object> {
                { "label", fields["label"].Text.Trim() }, { "family", family.SelectedIndex == 1 ? "windows" : "linux" }
            };
            if (!local && route.SelectedIndex == 1)
            {
                changes["ssh"] = SshFields();
                changes["legacy_route"] = null;
            }
            else if (!local && route.SelectedIndex == 0)
            {
                changes["ssh"] = null;
                changes["legacy_route"] = null;
            }
            var request = new Dictionary<string, object> { { "expected_revision", revision } };
            if (node == null)
            {
                var created = changes.Where(pair => pair.Value != null).ToDictionary(pair => pair.Key, pair => pair.Value);
                created["id"] = fields["id"].Text.Trim();
                bool legacy = local && NodeCatalog.HasLegacyProfile();
                created["aliases"] = legacy ? new[] { "windows" } : new string[0];
                request["action"] = "add";
                request["node"] = created;
                request["local"] = local;
            }
            else
            {
                request["action"] = "update";
                request["node_id"] = node["id"];
                request["changes"] = changes;
            }
            return request;
        }

        private void Save()
        {
            if (owner.nodesBusy)
            {
                message.Text = "Attendi il completamento dell’operazione sul catalogo.";
                return;
            }
            Dictionary<string, object> request;
            try { request = RequestPayload(); }
            catch (FormatException error) { message.Text = error.Message; return; }
            saving = true;
            form.Enabled = false;
            save.Enabled = false;
            message.Text = "Salvataggio del catalogo…";
            owner.StartCatalog(request, () => { saving = false; Close(); }, error => {
                saving = false;
                form.Enabled = true;
                save.Enabled = true;
                message.Text = error;
                message.ForeColor = Danger;
            });
        }

    }

    private TabPage BuildStatus()
    {
        var page = Page("Stato");
        var content = PageLayout(page, 3);
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 86));
        content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var heading = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        heading.Controls.Add(new Label { Text = "Stato di Ubuntu", Dock = DockStyle.Fill,
            Font = new Font("Segoe UI", 15, FontStyle.Bold), TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
        heading.Controls.Add(ActionButton("Aggiorna", RefreshStatus), 1, 0);
        content.Controls.Add(heading, 0, 0);
        var metrics = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1 };
        metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42));
        metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));
        metrics.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33));
        metrics.Controls.Add(MetricCard("Sistema", systemMetric), 0, 0);
        metrics.Controls.Add(MetricCard("Processore", cpuMetric), 1, 0);
        metrics.Controls.Add(MetricCard("Memoria", ramMetric), 2, 0);
        content.Controls.Add(metrics, 0, 1);
        content.Controls.Add(status, 0, 2);
        return page;
    }

    private static Label MetricValue()
    {
        return new Label { Text = "—", Dock = DockStyle.Fill, ForeColor = Ink,
            Font = new Font("Segoe UI", 13, FontStyle.Bold), TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = true };
    }

    private static Control MetricCard(string caption, Label value)
    {
        var card = new Panel { BackColor = Surface, Dock = DockStyle.Fill,
            Margin = new Padding(0, 0, 7, 8), Padding = new Padding(12, 7, 12, 5) };
        card.Controls.Add(value);
        card.Controls.Add(new Label { Text = caption, Dock = DockStyle.Top, ForeColor = Muted, Height = 23 });
        card.Paint += delegate(object sender, PaintEventArgs args) {
            using (var border = new Pen(Line))
                args.Graphics.DrawRectangle(border, 0, 0, card.ClientSize.Width - 1, card.ClientSize.Height - 1);
        };
        return card;
    }

    private void SetConnection(string text, bool online)
    {
        connection.ForeColor = online ? Online : Waiting;
        connection.BackColor = online ? Color.FromArgb(23, 51, 41) : Color.FromArgb(48, 39, 28);
        connection.Text = text;
    }

    private static string TransferSize(long value)
    {
        string[] units = { "B", "KiB", "MiB", "GiB", "TiB" };
        double size = value; int unit = 0;
        while (size >= 1024 && unit < units.Length - 1) { size /= 1024; unit++; }
        return size.ToString(unit == 0 ? "N0" : "N2") + " " + units[unit];
    }

    private void RenderTransfer()
    {
        string[] phases = { "Preparazione…", "Trasferimento in corso…", "Pubblicazione sul destinatario…",
                            "Completato · file disponibile sul destinatario", "Errore · trasferimento non confermato" };
        transferStatus.Text = phases[(int)transferState.Phase];
        transferStatus.ForeColor = transferState.Phase == TransferPhase.Completed ? Online
            : transferState.Phase == TransferPhase.Failed ? Danger : Waiting;
        int? percent = transferState.Percent;
        string total = transferState.Total.HasValue ? TransferSize(transferState.Total.Value) : "totale non disponibile";
        transferBytes.Text = (percent.HasValue ? percent.Value + "% · " : "")
            + (transferState.HasSample ? TransferSize(transferState.Bytes) : "Avanzamento in verifica") + " / " + total;
        if (transferState.HasSample && transferState.Total.HasValue)
            transferBytes.Text += "  (" + transferState.Bytes.ToString("N0") + " / " + transferState.Total.Value.ToString("N0") + " byte)";
        transferBar.Display(percent, !transferState.Terminal);
    }

    private void PostTransfer(long generation, TransferUpdate update)
    {
        if (IsDisposed || Disposing || !IsHandleCreated) return;
        try
        {
            BeginInvoke((Action)(() => {
                if (!IsDisposed && transferState.Accept(generation, update)) RenderTransfer();
            }));
        }
        catch (InvalidOperationException) { }
    }

    private void StartTransfer(bool upload, string source, string destination)
    {
        if (busy) return;
        long generation = transferState.Begin();
        RenderTransfer();
        TransferUpdate completed = null;
        Action<TransferUpdate> progress = update => {
            // Only the successful Task return commits 100%; a queued observation is not publication proof.
            if (update.Phase == TransferPhase.Completed) completed = update;
            else PostTransfer(generation, update);
        };
        Start(upload ? "Invio verso Linux…" : "Download verso Windows…", () => {
            if (upload) DockTransport.Upload(source, destination, progress);
            else DockTransport.Download(source, destination, progress);
            return destination;
        }, path => {
            if (completed != null && transferState.Accept(generation, completed)) RenderTransfer();
            return (upload ? "Inviato su Linux · " : "Salvato su Windows · ") + path;
        }, () => {
            var failed = new TransferUpdate(TransferPhase.Failed,
                transferState.HasSample ? (long?)transferState.Bytes : null, transferState.Total);
            if (transferState.Accept(generation, failed)) RenderTransfer();
        });
    }

    private TabPage BuildTransfers()
    {
        var page = Page("Trasferimenti");
        var outer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        outer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        page.Controls.Add(outer);
        var progress = FileCard("Avanzamento del trasferimento", transferStatus, transferBytes, transferBar);
        progress.Margin = new Padding(15, 10, 15, 0);
        outer.Controls.Add(progress, 0, 0);
        outer.SizeChanged += delegate {
            int width = Math.Max(140, outer.ClientSize.Width - 65);
            transferStatus.MaximumSize = transferBytes.MaximumSize = new Size(width, 0);
            transferBar.Height = Math.Max(26, Font.Height + 8);
        };
        var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
        var content = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 1, RowCount = 2,
            Padding = new Padding(15), AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        viewport.Controls.Add(content);
        outer.Controls.Add(viewport, 0, 1);
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var uploadWindows = new TextBox();
        var uploadLinux = new TextBox();
        var downloadLinux = new TextBox();
        var downloadWindows = new TextBox();

        var upload = FileCard("Windows → Linux",
            Field("File su Windows", uploadWindows, () => {
                if (Pick(uploadWindows, false) && String.IsNullOrWhiteSpace(uploadLinux.Text)
                    && linuxHome.Length != 0)
                    uploadLinux.Text = linuxHome.TrimEnd('/') + "/" + Path.GetFileName(uploadWindows.Text);
            }),
            Field("Destinazione su Linux", uploadLinux),
            ActionButton("Invia file", () => {
                string local = uploadWindows.Text, remote = uploadLinux.Text;
                StartTransfer(true, local, remote);
            }));
        content.Controls.Add(upload, 0, 0);

        var download = FileCard("Linux → Windows",
            Field("File su Linux", downloadLinux),
            Field("Destinazione su Windows", downloadWindows, () => Pick(downloadWindows, true)),
            ActionButton("Scarica file", () => {
                string remote = downloadLinux.Text, local = downloadWindows.Text;
                StartTransfer(false, remote, local);
            }));
        content.Controls.Add(download, 0, 1);
        return page;
    }

    private TabPage BuildScripts()
    {
        var page = Page("Bash");
        var content = PageLayout(page, 4);
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var file = new TextBox();
        content.Controls.Add(Field("Script .sh su Windows", file, () =>
            Pick(file, false, "Script Bash (*.sh)|*.sh")), 0, 0);
        content.Controls.Add(ActionButton("Esegui su Linux", () => {
            string path = file.Text;
            output.Clear();
            Start("Script in esecuzione su Linux…", () => DockTransport.Script(path), result => {
                output.Text = DisplayText("Codice di uscita: " + result.Code + "\n\n" + result.Output
                    + (String.IsNullOrWhiteSpace(result.Errors) ? "" : "\n\nErrori e diagnostica\n" + result.Errors));
                return "Script terminato · codice " + result.Code;
            });
        }), 0, 1);
        content.Controls.Add(new Label { Text = "Risultato", Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft }, 0, 2);
        content.Controls.Add(output, 0, 3);
        return page;
    }

    private TabPage BuildInput()
    {
        var page = Page("Mouse e tastiera");
        var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
        page.Controls.Add(viewport);
        var content = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true,
            ColumnCount = 1, RowCount = 11, Padding = new Padding(15) };
        content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        for (int i = 0; i < 11; i++)
            content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        viewport.Controls.Add(content);
        content.Controls.Add(new Label { Text = "Più PC · un gruppo di schermi", AutoSize = true,
            Font = new Font("Segoe UI", 12, FontStyle.Bold), Margin = new Padding(0, 0, 0, 10) }, 0, 0);
        var groupNote = new Label { Text = "Scegli il PC principale, disponi gli schermi e prepara gli appunti condivisi. "
            + "Salva layout conserva la disposizione; Avvia gruppo controlla i prerequisiti e richiede i consensi necessari.",
            AutoSize = true, ForeColor = Muted, Margin = new Padding(0, 0, 0, 10) };
        content.Controls.Add(groupNote, 0, 1);
        inputGroupButton = ActionButton("Configura gruppo di schermi", ConfigureDesktopGroup, false);
        inputGroupButton.Anchor = AnchorStyles.Left;
        var groupActions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true };
        groupActions.Controls.Add(inputGroupButton);
        groupButton = ActionButton("Avvia gruppo", ToggleManagedGroup);
        groupActions.Controls.Add(groupButton);
        content.Controls.Add(groupActions, 0, 2);
        groupStatus.Text = "Gruppo non attivo. Il motore e i consensi desktop devono essere disponibili sui membri.";
        groupStatus.Margin = new Padding(0, 8, 0, 24);
        content.Controls.Add(groupStatus, 0, 3);
        content.Controls.Add(new Label { Text = "Collegamento attuale a due PC · schermo Ubuntu", AutoSize = true,
            TextAlign = ContentAlignment.MiddleLeft, Margin = new Padding(0, 0, 0, 10) }, 0, 4);
        inputSide.Items.AddRange(new object[] { "A sinistra di Windows", "A destra di Windows",
            "Sopra Windows", "Sotto Windows" });
        inputSide.SelectedIndex = 0;
        inputSide.Dock = DockStyle.Top;
        inputSide.FlatStyle = FlatStyle.Flat;
        inputSide.BackColor = Inset;
        inputSide.ForeColor = Ink;
        inputSide.AccessibleName = "Posizione dello schermo Ubuntu";
        content.Controls.Add(inputSide, 0, 5);
        var guide = new Label { Text = "Porta il puntatore oltre il bordo scelto. Premi Ctrl+F12 per cambiare PC.",
            AutoSize = true, TextAlign = ContentAlignment.MiddleLeft, Margin = new Padding(0, 8, 0, 10) };
        content.Controls.Add(guide, 0, 6);
        content.Controls.Add(shareClipboard, 0, 7);
        content.Controls.Add(autoConnectInput, 0, 8);
        SetInputStatus("Disattivata", Muted);
        inputStatus.Dock = DockStyle.Fill;
        inputStatus.AutoSize = true;
        inputStatus.Margin = new Padding(0, 10, 0, 10);
        inputStatus.TextAlign = ContentAlignment.MiddleLeft;
        content.Controls.Add(inputStatus, 0, 9);
        inputButton = ActionButton("Attiva condivisione", ToggleInput);
        inputButton.Anchor = AnchorStyles.Left;
        content.Controls.Add(inputButton, 0, 10);
        viewport.Resize += delegate {
            int width = Math.Max(200, viewport.ClientSize.Width - SystemInformation.VerticalScrollBarWidth - 35);
            foreach (var label in new[] { groupNote, guide, inputStatus, groupStatus })
                label.MaximumSize = new Size(width, 0);
            shareClipboard.MaximumSize = autoConnectInput.MaximumSize = new Size(width, 0);
        };
        return page;
    }

    private void ToggleInput()
    {
        if (managedSession != null)
        {
            StopManagedGroup();
            return;
        }
        if (managedUncertain)
        {
            SetGroupStatus("Arresto del gruppo non confermato. Verifica i nodi prima di avviare altre condivisioni.", "failed");
            return;
        }
        if (inputSession != null)
        {
            StopInput();
            return;
        }
        if (desktopSession != null)
            return;
        automaticInputWanted = autoConnectInput.Checked;
        inputRetry.Enabled = automaticInputWanted;
        StartInput(false);
    }

    private void ChangeInputSide(object sender, EventArgs args)
    {
        if (inputSide.SelectedIndex < 0 || inputSession == null || inputSession.IsCancellationRequested || closePending)
            return;
        restartInputAfterSideChange = true;
        SetInputStatus("Cambio di lato…", Waiting);
        inputSession.Cancel();
    }

    private async void StartInput(bool automatic)
    {
        if (managedSession != null || managedUncertain)
            return;
        string direction = new[] { "left", "right", "up", "down" }[inputSide.SelectedIndex];
        bool clipboard = shareClipboard.Checked;
        var session = new CancellationTokenSource();
        inputSession = session;
        SetInputStatus("Connessione via Ethernet…", Waiting);
        SetInputButtons("Disattiva condivisione", true, stop: true);
        shareClipboard.Enabled = false;
        desktopButton.Enabled = false;
        bool stopConfirmed = true;
        string retryStatus = null;
        try
        {
            await Task.Run(() =>
            {
                if (automatic)
                    DockTransport.CheckInputConnection();
                if (session.IsCancellationRequested)
                    return;
                DockTransport.ShareInput(direction, clipboard, session.Token, () =>
                BeginInvoke((Action)(() =>
                {
                    if (inputSession == session && !session.IsCancellationRequested)
                        SetInputStatus("Condivisione attiva", Online);
                })));
            });
        }
        catch (InputStopException error)
        {
            stopConfirmed = false;
            SetAutomaticInput(false);
            closePending = false;
            MessageBox.Show(this, error.Message, "Arresto della condivisione non confermato",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        catch (Exception error)
        {
            if (automaticInputWanted && !session.IsCancellationRequested && !closePending)
            {
                retryStatus = "Nuovo tentativo tra pochi secondi. " + error.Message.Split('\n')[0].TrimEnd('\r');
            }
            else if (!session.IsCancellationRequested && !closePending)
                MessageBox.Show(this, error.Message, "Condivisione di mouse e tastiera",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            inputSession = null;
            session.Dispose();
            bool restart = restartInputAfterSideChange && stopConfirmed && !closePending;
            restartInputAfterSideChange = false;
            if (restart)
            {
                StartInput(false);
            }
            else
            {
                SetInputStatus(stopConfirmed ? retryStatus ?? "Disattivata" : "Arresto non confermato",
                    stopConfirmed ? (retryStatus == null ? Muted : Waiting) : Danger);
                shareClipboard.Enabled = true;
                desktopButton.Enabled = true;
                SetInputButtons("Attiva condivisione", true);
                FinishClose();
            }
        }
    }

    private void StopInput()
    {
        restartInputAfterSideChange = false;
        SetAutomaticInput(false);
        inputSession.Cancel();
        SetInputStatus("Chiusura…", Waiting);
        SetInputButtons("Disattiva condivisione", false, stop: true);
    }

    private void SetInputStatus(string text, Color color)
    {
        inputStatus.Text = text;
        inputStatus.ForeColor = color;
    }

    private void SetInputButtons(string caption, bool enabled, bool stop = false)
    {
        inputButton.Text = inputHeaderButton.Text = caption;
        inputButton.Enabled = inputHeaderButton.Enabled = enabled;
        StyleButton(inputButton, true, stop);
        StyleButton(inputHeaderButton, true, stop);
    }

    private void FinishClose()
    {
        if (closePending && !busy && !nodesBusy && inputSession == null && desktopSession == null && managedSession == null)
            Close();
    }

    private static string DisplayText(string value)
    {
        return value.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
    }

    private static TabPage Page(string title)
    {
        return new TabPage(title) { UseVisualStyleBackColor = false, BackColor = Canvas, ForeColor = Ink };
    }

    private static TableLayoutPanel PageLayout(TabPage page, int rows)
    {
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1,
            RowCount = rows, Padding = new Padding(15) };
        page.Controls.Add(layout);
        return layout;
    }

    private static Control FileCard(string title, Control first, Control second, Control action)
    {
        var group = new GroupBox { Text = title, Dock = DockStyle.Fill, AutoSize = true,
            BackColor = Surface, ForeColor = Ink,
            AutoSizeMode = AutoSizeMode.GrowAndShrink };
        var box = new TableLayoutPanel { Dock = DockStyle.Top, RowCount = 3,
            ColumnCount = 1, Padding = new Padding(8, 4, 8, 4),
            AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        box.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        box.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        box.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        box.Controls.Add(first, 0, 0);
        box.Controls.Add(second, 0, 1);
        box.Controls.Add(action, 0, 2);
        group.Controls.Add(box);
        return group;
    }

    private static Control Field(string caption, TextBox text, Action browse = null)
    {
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = browse == null ? 1 : 2,
            RowCount = 2, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var label = new Label { Text = caption, AutoSize = true };
        layout.Controls.Add(label, 0, 0);
        text.Anchor = AnchorStyles.Left | AnchorStyles.Right;
        text.BackColor = Inset;
        text.ForeColor = Ink;
        text.BorderStyle = BorderStyle.FixedSingle;
        text.AccessibleName = caption;
        layout.Controls.Add(text, 0, 1);
        if (browse != null)
        {
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.SetColumnSpan(label, 2);
            var button = new DockButton { Text = "Sfoglia…", AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink, Anchor = AnchorStyles.Right,
                Padding = new Padding(9, 3, 9, 3) };
            StyleButton(button, false);
            button.Click += delegate { browse(); };
            layout.Controls.Add(button, 1, 1);
        }
        return layout;
    }

    private bool Pick(TextBox field, bool save, string filter = "Tutti i file (*.*)|*.*")
    {
        using (FileDialog dialog = save ? (FileDialog)new SaveFileDialog
            { OverwritePrompt = false } : new OpenFileDialog())
        {
            dialog.Filter = filter;
            if (dialog.ShowDialog(this) != DialogResult.OK)
                return false;
            field.Text = dialog.FileName;
            return true;
        }
    }

    private static Button ActionButton(string text, Action action, bool primary = true)
    {
        var button = new DockButton { Text = text, AutoSize = true, Height = 31,
            Anchor = AnchorStyles.Top | AnchorStyles.Right, Padding = new Padding(7, 0, 7, 0) };
        StyleButton(button, primary);
        button.Click += delegate { action(); };
        return button;
    }

    private static void StyleButton(Button button, bool primary, bool stop = false)
    {
        button.UseVisualStyleBackColor = false;
        button.FlatStyle = FlatStyle.Flat;
        button.BackColor = stop ? Color.FromArgb(72, 35, 44) : primary ? Cyan : ControlFill;
        button.ForeColor = stop ? Danger : primary ? OnCyan : Ink;
        button.FlatAppearance.BorderSize = 1;
        button.FlatAppearance.BorderColor = stop ? Color.FromArgb(187, 102, 116) : primary ? Cyan : ControlBorder;
        button.FlatAppearance.MouseOverBackColor = ControlPaint.Light(button.BackColor, .18f);
        button.FlatAppearance.MouseDownBackColor = ControlPaint.Light(button.BackColor, .09f);
    }

    private static TextBox ConsoleBox()
    {
        return new TextBox { ReadOnly = true, Multiline = true, Dock = DockStyle.Fill,
            BackColor = Inset, ForeColor = Ink, WordWrap = false, BorderStyle = BorderStyle.FixedSingle,
            ScrollBars = ScrollBars.Both, Font = new Font("Consolas", 11) };
    }

    private void RefreshStatus()
    {
        Start("Lettura dello stato di Linux…", DockTransport.Status, report => {
            linuxHome = Regex.Match(report, @"(?m)^Home: (/[^\r\n]*)").Groups[1].Value;
            string system = Regex.Match(report, @"(?m)^Sistema: ([^\r\n]+)").Groups[1].Value;
            string cpu = Regex.Match(report, @"(?m)^CPU: ([^\r\n]+)").Groups[1].Value;
            Match memory = Regex.Match(report, @"(?m)^RAM: (\S+) usati di (\S+)");
            systemMetric.Text = system.Length == 0 ? "—" : system;
            cpuMetric.Text = cpu.Length == 0 ? "—" : cpu;
            ramMetric.Text = memory.Success ? memory.Groups[1].Value + " / " + memory.Groups[2].Value : "—";
            status.Text = DisplayText(Regex.Replace(report, @"(?m)^(Sistema|CPU|RAM): [^\r\n]*\r?\n?", ""));
            SetConnection("SSH attivo", true);
            return "Aggiornato alle " + DateTime.Now.ToString("HH:mm:ss");
        }, () => {
            SetConnection("SSH non verificato", false);
        });
    }

    private async void Start<T>(string progress, Func<T> job, Func<T, string> done, Action failed = null)
    {
        if (busy)
            return;
        busy = true;
        SetJobPagesEnabled(false);
        footer.ForeColor = Ink;
        footer.Text = progress;
        footer.AccessibleDescription = progress;
        try
        {
            footer.Text = done(await Task.Run(job));
            footer.AccessibleDescription = footer.Text;
        }
        catch (Exception error)
        {
            if (failed != null)
                failed();
            footer.ForeColor = Danger;
            footer.Text = error.Message.Replace("\r", "").Replace("\n", " · ");
            footer.AccessibleDescription = error.Message;
        }
        finally
        {
            busy = false;
            SetJobPagesEnabled(true);
            FinishClose();
        }
    }

    private void SetJobPagesEnabled(bool enabled)
    {
        foreach (TabPage page in tabs.TabPages)
            if (page != inputPage && page != nodesPage)
                SetJobControlsEnabled(page, enabled);
    }

    private static void SetJobControlsEnabled(Control container, bool enabled)
    {
        foreach (Control control in container.Controls)
        {
            if (control is Button || control is TextBox)
                control.Enabled = enabled;
            else
                SetJobControlsEnabled(control, enabled);
        }
    }

    private async void ToggleDesktop()
    {
        if (managedSession != null || managedUncertain)
        {
            SetGroupStatus("Ferma il gruppo prima di usare il collegamento desktop remoto.", "stopping");
            return;
        }
        if (desktopSession != null)
        {
            desktopSession.Cancel();
            return;
        }
        if (inputSession != null)
            return;
        var session = new CancellationTokenSource();
        desktopSession = session;
        SetInputButtons("Attiva condivisione", false);
        SetInputStatus("Chiudi il desktop remoto per usare questa modalità.", Waiting);
        desktopButton.Text = "Chiudi desktop remoto";
        StyleButton(desktopButton, false, stop: true);
        try
        {
            await Task.Run(() => DockTransport.Desktop(session.Token));
        }
        catch (Exception error)
        {
            if (!session.IsCancellationRequested)
                MessageBox.Show(this, error.Message, "Collegamento desktop remoto",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            desktopSession = null;
            desktopButton.Text = "Desktop remoto";
            StyleButton(desktopButton, false);
            SetInputButtons("Attiva condivisione", true);
            SetInputStatus("Disattivata", Muted);
            session.Dispose();
            FinishClose();
        }
    }

    [STAThread]
    private static void Main()
    {
        using (var instance = new Mutex(false, @"Local\Vincula.LinuxDock"))
        {
            bool ownsInstance;
            try { ownsInstance = instance.WaitOne(0); }
            catch (AbandonedMutexException) { ownsInstance = true; }
            if (!ownsInstance)
            {
                ActivateExistingWindow();
                return;
            }
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new DockWindow(true));
            }
            finally
            {
                instance.ReleaseMutex();
            }
        }
    }

    private static void ActivateExistingWindow()
    {
        using (Process current = Process.GetCurrentProcess())
        {
            Process[] processes = Process.GetProcessesByName(current.ProcessName);
            try
            {
                foreach (Process process in processes)
                {
                    if (process.Id == current.Id || process.SessionId != current.SessionId || process.HasExited)
                        continue;
                    IntPtr window = process.MainWindowHandle;
                    if (window == IntPtr.Zero)
                        continue;
                    if (IsIconic(window))
                        ShowWindowAsync(window, 9);
                    SetForegroundWindow(window);
                    return;
                }
            }
            finally
            {
                foreach (Process process in processes)
                    process.Dispose();
            }
        }
    }

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);
}
