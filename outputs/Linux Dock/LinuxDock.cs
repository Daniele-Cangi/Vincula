using System;
using System.Diagnostics;
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

    public static void Upload(string local, string remote)
    {
        local = Path.GetFullPath(local);
        if (!File.Exists(local))
            throw new FileNotFoundException("Il file su Windows non esiste.", local);
        remote = LinuxFile(remote);
        string stage = remote.Substring(0, remote.LastIndexOf('/') + 1)
            + ".linux-dock-" + Guid.NewGuid().ToString("N") + ".part";
        string target = BashName(remote);
        string temporary = BashName(stage);
        Success(Bash("if [ -e " + target + " ] || [ -L " + target
            + " ]; then echo 'Il file Linux di destinazione esiste gia.' >&2; exit 1; fi"));
        try
        {
            Sftp("put " + SftpName(local.Replace('\\', '/')) + " " + SftpName(stage));
            Success(Bash("python3 - " + temporary + " " + target + " <<'DOCK_PUBLISH'\n"
                + LinuxPublish + "DOCK_PUBLISH"));
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

    public static void Download(string remote, string local)
    {
        remote = LinuxFile(remote);
        local = Path.GetFullPath(local);
        if (File.Exists(local) || Directory.Exists(local))
            throw new IOException("Il file Windows di destinazione esiste già. Scegli un altro nome.");
        string temporary = Path.Combine(Path.GetDirectoryName(local),
            ".linux-dock-" + Guid.NewGuid().ToString("N") + ".part");
        try
        {
            Sftp("get " + SftpName(remote) + " " + SftpName(temporary.Replace('\\', '/')));
            File.Move(temporary, local);
        }
        finally
        {
            File.Delete(temporary);
        }
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

internal sealed class DockWindow : Form
{
    private const string InputSettings = @"HKEY_CURRENT_USER\Software\Vincula\Linux Dock";
    private static readonly Color Navy = Color.FromArgb(18, 36, 59);
    private static readonly Color Blue = Color.FromArgb(33, 95, 159);
    private static readonly Color Ink = Color.FromArgb(24, 40, 61);
    private readonly TabControl tabs = new TabControl();
    private readonly Label footer = new Label();
    private readonly Label connection = new Label();
    private readonly TextBox status = ConsoleBox();
    private readonly TextBox output = ConsoleBox();
    private bool busy;
    private readonly Button desktopButton;
    private readonly Button inputHeaderButton;
    private readonly ComboBox inputSide = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly CheckBox shareClipboard = new CheckBox {
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
    private CancellationTokenSource inputSession;
    private CancellationTokenSource desktopSession;
    private bool closePending;
    private bool automaticInputWanted;
    private string linuxHome = "";

    public DockWindow() : this(false) { }

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
        BackColor = Color.FromArgb(243, 245, 249);
        Font = new Font("Segoe UI", 10);
        ForeColor = Ink;
        FormClosing += delegate(object sender, FormClosingEventArgs args) {
            automaticInputWanted = false;
            inputRetry.Stop();
            if (inputSession != null)
                StopInput();
            if (desktopSession != null)
                desktopSession.Cancel();
            if (busy || desktopSession != null || inputSession != null)
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

        var hero = new Panel { BackColor = Navy, Dock = DockStyle.Fill, Margin = new Padding(0) };
        hero.Controls.Add(new Label { Text = "Linux Dock", AutoSize = true, ForeColor = Color.White,
            Font = new Font("Segoe UI", 19, FontStyle.Bold), Left = 20, Top = 13 });
        hero.Controls.Add(new Label { Text = "Ubuntu  ·  SSH via Ethernet",
            ForeColor = Color.LightSteelBlue, AutoSize = true, Left = 22, Top = 57 });
        connection.AutoSize = true;
        connection.ForeColor = Color.LightSteelBlue;
        connection.Text = "SSH da verificare";
        connection.Top = 22;
        hero.Controls.Add(connection);
        desktopButton = ActionButton("Desktop remoto", ToggleDesktop);
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
        tabs.TabPages.Add(BuildStatus());
        tabs.TabPages.Add(BuildTransfers());
        tabs.TabPages.Add(BuildScripts());
        inputPage = BuildInput();
        tabs.TabPages.Add(inputPage);
        layout.Controls.Add(tabs, 0, 1);

        footer.Dock = DockStyle.Fill;
        footer.TextAlign = ContentAlignment.MiddleLeft;
        footer.Padding = new Padding(15, 0, 0, 0);
        footer.AutoEllipsis = true;
        footer.Text = "Pronto";
        layout.Controls.Add(footer, 0, 2);
        if (useSavedInputSettings)
            InitializeInputSettings();
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
        if (automaticInputWanted && !closePending && !busy && inputSession == null && desktopSession == null)
            StartInput(true);
    }

    private TabPage BuildStatus()
    {
        var page = Page("Stato");
        var content = PageLayout(page, 2);
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 45));
        content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        content.Controls.Add(ActionButton("Aggiorna", RefreshStatus), 0, 0);
        content.Controls.Add(status, 0, 1);
        return page;
    }

    private TabPage BuildTransfers()
    {
        var page = Page("Trasferimenti");
        var viewport = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
        var content = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 1, RowCount = 2,
            Padding = new Padding(15), AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
        viewport.Controls.Add(content);
        page.Controls.Add(viewport);
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
                Start("Invio verso Linux…", () => { DockTransport.Upload(local, remote); return remote; },
                    path => "Inviato su Linux · " + path);
            }));
        content.Controls.Add(upload, 0, 0);

        var download = FileCard("Linux → Windows",
            Field("File su Linux", downloadLinux),
            Field("Destinazione su Windows", downloadWindows, () => Pick(downloadWindows, true)),
            ActionButton("Scarica file", () => {
                string remote = downloadLinux.Text, local = downloadWindows.Text;
                Start("Download verso Windows…", () => { DockTransport.Download(remote, local); return local; },
                    path => "Salvato su Windows · " + path);
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
        var content = PageLayout(page, 7);
        for (int i = 0; i < 3; i++)
            content.RowStyles.Add(new RowStyle(SizeType.Absolute, i == 2 ? 55 : 42));
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        content.Controls.Add(new Label { Text = "Posizione dello schermo Ubuntu",
            Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft,
            Font = new Font("Segoe UI", 12, FontStyle.Bold) }, 0, 0);
        inputSide.Items.AddRange(new object[] { "A sinistra di Windows", "A destra di Windows",
            "Sopra Windows", "Sotto Windows" });
        inputSide.SelectedIndex = 0;
        inputSide.Dock = DockStyle.Top;
        inputSide.AccessibleName = "Posizione dello schermo Ubuntu";
        content.Controls.Add(inputSide, 0, 1);
        content.Controls.Add(new Label { Text = "Porta il puntatore oltre il bordo scelto. "
            + "Premi Ctrl+F12 per cambiare PC.",
            Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 2);
        content.Controls.Add(shareClipboard, 0, 3);
        content.Controls.Add(autoConnectInput, 0, 4);
        inputStatus.Text = "Disattivata";
        inputStatus.Dock = DockStyle.Fill;
        inputStatus.TextAlign = ContentAlignment.MiddleLeft;
        content.Controls.Add(inputStatus, 0, 5);
        inputButton = ActionButton("Attiva condivisione", ToggleInput);
        content.Controls.Add(inputButton, 0, 6);
        return page;
    }

    private void ToggleInput()
    {
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

    private async void StartInput(bool automatic)
    {
        string direction = new[] { "left", "right", "up", "down" }[inputSide.SelectedIndex];
        bool clipboard = shareClipboard.Checked;
        var session = new CancellationTokenSource();
        inputSession = session;
        inputStatus.Text = "Connessione via Ethernet…";
        SetInputButtons("Disattiva condivisione", true);
        inputSide.Enabled = false;
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
                        inputStatus.Text = "Condivisione attiva";
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
            inputStatus.Text = stopConfirmed ? retryStatus ?? "Disattivata" : "Arresto non confermato";
            inputSide.Enabled = true;
            shareClipboard.Enabled = true;
            desktopButton.Enabled = true;
            SetInputButtons("Attiva condivisione", true);
            FinishClose();
        }
    }

    private void StopInput()
    {
        SetAutomaticInput(false);
        inputSession.Cancel();
        inputStatus.Text = "Chiusura…";
        SetInputButtons("Disattiva condivisione", false);
    }

    private void SetInputButtons(string caption, bool enabled)
    {
        inputButton.Text = inputHeaderButton.Text = caption;
        inputButton.Enabled = inputHeaderButton.Enabled = enabled;
    }

    private void FinishClose()
    {
        if (closePending && !busy && inputSession == null && desktopSession == null)
            Close();
    }

    private static string DisplayText(string value)
    {
        return value.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
    }

    private static TabPage Page(string title)
    {
        return new TabPage(title) { BackColor = Color.White, ForeColor = Ink };
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
        text.AccessibleName = caption;
        layout.Controls.Add(text, 0, 1);
        if (browse != null)
        {
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.SetColumnSpan(label, 2);
            var button = new Button { Text = "Sfoglia…", AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink, Anchor = AnchorStyles.Right,
                Padding = new Padding(9, 3, 9, 3) };
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

    private static Button ActionButton(string text, Action action)
    {
        var button = new Button { Text = text, AutoSize = true, Height = 31,
            Anchor = AnchorStyles.Top | AnchorStyles.Right, FlatStyle = FlatStyle.Flat,
            BackColor = Blue, ForeColor = Color.White };
        button.FlatAppearance.BorderSize = 0;
        button.Click += delegate { action(); };
        return button;
    }

    private static TextBox ConsoleBox()
    {
        return new TextBox { ReadOnly = true, Multiline = true, Dock = DockStyle.Fill,
            BackColor = Navy, ForeColor = Color.FromArgb(226, 237, 249), WordWrap = false,
            ScrollBars = ScrollBars.Both, Font = new Font("Consolas", 11) };
    }

    private void RefreshStatus()
    {
        Start("Lettura dello stato di Linux…", DockTransport.Status, report => {
            linuxHome = Regex.Match(report, @"(?m)^Home: (/[^\r\n]*)").Groups[1].Value;
            status.Text = DisplayText(report);
            connection.Text = "SSH attivo";
            connection.ForeColor = Color.PaleGreen;
            return "Aggiornato alle " + DateTime.Now.ToString("HH:mm:ss");
        }, () => {
            connection.Text = "SSH non verificato";
            connection.ForeColor = Color.LightSteelBlue;
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
            footer.ForeColor = Color.DarkRed;
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
            if (page != inputPage)
                page.Enabled = enabled;
    }

    private async void ToggleDesktop()
    {
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
        inputStatus.Text = "Chiudi il desktop remoto per usare questa modalità.";
        desktopButton.Text = "Chiudi desktop remoto";
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
            SetInputButtons("Attiva condivisione", true);
            inputStatus.Text = "Disattivata";
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
