// SPDX-FileCopyrightText: 2026 Daniele Cangi
// SPDX-License-Identifier: GPL-3.0-or-later

// UI-only QA: separate assembly, hidden owner, synthetic Python runtime and no network.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public static class NativeNodeCheckQa
{
    private const BindingFlags Instance = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
    private const BindingFlags Static = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static int uiThreadId;

    private static void AssertUiContext()
    {
        Require(Thread.CurrentThread.ManagedThreadId == uiThreadId
            && Application.MessageLoop && SynchronizationContext.Current is WindowsFormsSynchronizationContext,
            "The QA UI thread lost its persistent WinForms message loop or synchronization context");
    }

    private static void PumpUi()
    {
        Application.DoEvents();
        AssertUiContext();
    }

    private static object Field(object target, string name)
    {
        return target.GetType().GetField(name, Instance).GetValue(target);
    }

    private static void Set(object target, string name, object value)
    {
        target.GetType().GetField(name, Instance).SetValue(target, value);
    }

    private static object Invoke(object target, string name, params object[] arguments)
    {
        AssertUiContext();
        try { return target.GetType().GetMethod(name, Instance).Invoke(target, arguments); }
        catch (TargetInvocationException error)
        {
            ExceptionDispatchInfo.Capture(error.InnerException ?? error).Throw();
            throw;
        }
    }

    private static Dictionary<string, object> Map(object value)
    {
        return (Dictionary<string, object>)value;
    }

    private static Dictionary<string, object> Copy(Dictionary<string, object> value)
    {
        return Map(Json.DeserializeObject(Json.Serialize(value)));
    }

    private static Dictionary<string, object> State(string state)
    {
        return new Dictionary<string, object> { { "state", state } };
    }

    private static Dictionary<string, Label> Values(Form dialog)
    {
        return (Dictionary<string, Label>)Field(dialog, "values");
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static Dictionary<string, object> ExceptionDetails(Exception error)
    {
        return new Dictionary<string, object> { { "type", error.GetType().FullName }, { "message", error.Message },
            { "stack", error.StackTrace }, { "details", error.ToString() } };
    }

    private static Dictionary<string, object> ControlState(Form control)
    {
        if (control == null) return null;
        // Never access Handle here: diagnostics must not initiate or resume CreateHandle.
        return new Dictionary<string, object> { { "type", control.GetType().FullName },
            { "is_handle_created", control.IsHandleCreated }, { "is_disposed", control.IsDisposed },
            { "disposing", control.Disposing }, { "visible", control.Visible } };
    }

    private static void WriteDiagnostic(string output, string name, object value)
    {
        try { File.WriteAllText(Path.Combine(output, name), Json.Serialize(value), new UTF8Encoding(false)); }
        catch (Exception error)
        {
            Console.Error.WriteLine("Native node-check QA diagnostic write failed (" + error.GetType().Name + ").");
        }
    }

    private static void Cleanup(string name, Action action, List<Dictionary<string, object>> failures)
    {
        try { action(); }
        catch (Exception error)
        {
            failures.Add(new Dictionary<string, object> { { "step", name }, { "exception", ExceptionDetails(error) } });
            Console.Error.WriteLine("Native node-check QA cleanup failed at " + name + " (" + error.GetType().Name + ").");
        }
    }

    private static IEnumerable<Control> Controls(Control parent)
    {
        yield return parent;
        foreach (Control child in parent.Controls)
            foreach (Control descendant in Controls(child))
                yield return descendant;
    }

    private static void Wait(Form owner)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(20);
        while ((bool)Field(owner, "nodesBusy") && DateTime.UtcNow < deadline)
        {
            PumpUi();
            Thread.Sleep(10);
        }
        PumpUi();
        Require(!(bool)Field(owner, "nodesBusy"), "Node-check callback did not finish");
        Require(!owner.Visible, "The DockWindow owner was unexpectedly shown");
    }

    private static void Render(Form dialog, string destination, bool bottom, Action<string> progress)
    {
        progress("show_dialog");
        if (!dialog.Visible)
        {
            dialog.StartPosition = FormStartPosition.Manual;
            dialog.Location = new Point(-32000, -32000);
            dialog.ShowInTaskbar = false;
            dialog.Opacity = 0;
            dialog.Show(); // Only the isolated dialog; it has no Shown network handler.
        }
        progress("pump_dialog_layout");
        for (int index = 0; index < 15; index++) { PumpUi(); Thread.Sleep(10); }
        var viewport = Controls(dialog).OfType<Panel>().First(control => control.GetType() == typeof(Panel) && control.AutoScroll);
        progress("scroll_dialog");
        viewport.AutoScrollPosition = bottom ? new Point(0, viewport.DisplayRectangle.Height) : Point.Empty;
        PumpUi();
        progress("check_button_bounds");
        foreach (Button button in Controls(dialog).OfType<Button>())
        {
            Require(button.Width >= button.PreferredSize.Width - 2 && button.Height >= button.PreferredSize.Height - 2,
                "Button cropped: " + button.Text + " " + button.Bounds);
            Require(button.ForeColor != button.BackColor, "Button text has no color contrast");
        }
        progress("check_label_bounds");
        foreach (Label label in Controls(dialog).OfType<Label>())
        {
            Size preferred = label.GetPreferredSize(new Size(label.Width, 0));
            Require(!label.AutoEllipsis && label.Height >= preferred.Height - 2,
                "Label cropped: " + label.Text + " " + label.Bounds);
        }
        progress("check_checkbox_bounds");
        foreach (CheckBox check in Controls(dialog).OfType<CheckBox>())
            using (Graphics graphics = check.CreateGraphics())
            {
                int glyph = CheckBoxRenderer.GetGlyphSize(graphics,
                    System.Windows.Forms.VisualStyles.CheckBoxState.UncheckedNormal).Width;
                int text = TextRenderer.MeasureText(graphics, check.Text, check.Font,
                    new Size(Int32.MaxValue, Int32.MaxValue), TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix).Width;
                Require(check.ClientSize.Width >= text + glyph + 4 - 2,
                    "CheckBox cropped: " + check.Text + "; width=" + check.Width + "; text=" + text + "; glyph=" + glyph);
            }
        progress("check_metadata_bounds");
        var metadata = (TextBox)Field(dialog, "metadata");
        Require(metadata.Width > 100 && metadata.Height >= metadata.Font.Height * 2 && metadata.WordWrap,
            "Metadata is cropped or cannot wrap");
        progress("draw_dialog_bitmap");
        using (var bitmap = new Bitmap(dialog.Width, dialog.Height))
        {
            dialog.DrawToBitmap(bitmap, new Rectangle(Point.Empty, bitmap.Size));
            bitmap.Save(destination, System.Drawing.Imaging.ImageFormat.Png);
        }
    }

    private static void PrepareMock(string root, Dictionary<string, object> snapshot, Dictionary<string, object> report)
    {
        Directory.CreateDirectory(Path.Combine(root, "agent"));
        foreach (string name in new[] { "node_registry.py", "desktop_layout.py", "node_enrollment.py", "node_probe.py" })
            File.WriteAllText(Path.Combine(root, "agent", name), "# UI fixture; never imports real worker code.\n");
        File.WriteAllText(Path.Combine(root, "snapshot.json"), Json.Serialize(snapshot));
        File.WriteAllText(Path.Combine(root, "report.json"), Json.Serialize(report));
        File.WriteAllText(Path.Combine(root, "mode.txt"), "ok");
        File.WriteAllText(Path.Combine(root, "agent", "nodes.py"), @"import json
from pathlib import Path
import sys
import time
root = Path(__file__).resolve().parent.parent
request = json.load(sys.stdin)
with root.joinpath('requests.jsonl').open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(request) + '\n')
deadline = time.monotonic() + 10
while root.joinpath('gate').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if root.joinpath('gate').exists():
    raise SystemExit(2)
if root.joinpath('mode.txt').read_text() == 'conflict':
    print(json.dumps({'ok': False, 'error': {'code': 'conflict', 'message': 'fixture conflict'}}))
    raise SystemExit(1)
snapshot = json.loads(root.joinpath('snapshot.json').read_text())
if request['action'] == 'enrollment_check':
    report = json.loads(root.joinpath('report.json').read_text())
    report['node_id'] = request['node_id']
    report['revision'] = request['expected_revision']
    snapshot['node_check'] = report
print(json.dumps(snapshot))
", new UTF8Encoding(false));
    }

    public static string Run(string appAssembly, string runtimeRoot, string outputDirectory)
    {
        Require(Thread.CurrentThread.GetApartmentState() == ApartmentState.STA, "Run this harness on an STA thread");
        string result = null;
        ExceptionDispatchInfo failure = null;
        // An STA PowerShell host has no persistent WinForms message loop. DoEvents alone
        // uninstalls its temporary WindowsFormsSynchronizationContext after each pump.
        // Run the unchanged test body inside a real, form-less loop, like the application.
        using (var loop = new ApplicationContext())
        using (var start = new System.Windows.Forms.Timer { Interval = 1 })
        {
            start.Tick += delegate {
                start.Stop();
                try { result = RunCore(appAssembly, runtimeRoot, outputDirectory); }
                catch (Exception error) { failure = ExceptionDispatchInfo.Capture(error); }
                finally { loop.ExitThread(); }
            };
            start.Start();
            Application.Run(loop);
        }
        if (failure != null) failure.Throw();
        return result;
    }

    private static string RunCore(string appAssembly, string runtimeRoot, string outputDirectory)
    {
        uiThreadId = Thread.CurrentThread.ManagedThreadId;
        Require(File.Exists(Path.Combine(runtimeRoot, "agent", "nodes.py")), "Provide the runtime root for provenance");
        Directory.CreateDirectory(outputDirectory);
        string fixture = Path.Combine(outputDirectory, "mock-runtime-" + Guid.NewGuid().ToString("N"));
        string oldRuntime = Environment.GetEnvironmentVariable("VINCULA_RUNTIME");
        string oldRegistry = Environment.GetEnvironmentVariable("VINCULA_NODE_REGISTRY");
        SynchronizationContext oldContext = SynchronizationContext.Current;
        bool oldCrossThreadChecks = Control.CheckForIllegalCrossThreadCalls;
        string phase = "initialize";
        Exception primaryException = null;
        Dictionary<string, object> primaryFailure = null;
        var progress = new List<Dictionary<string, object>>();
        var cleanupFailures = new List<Dictionary<string, object>>();
        var threadErrors = new List<string>();
        var asyncErrors = new List<Dictionary<string, object>>();
        ThreadExceptionEventHandler threadError = (sender, error) => {
            threadErrors.Add(error.Exception.GetType().Name);
            asyncErrors.Add(new Dictionary<string, object> { { "phase", phase }, { "exception", ExceptionDetails(error.Exception) } });
            WriteDiagnostic(outputDirectory, "windows-mock-async-errors.json", asyncErrors);
        };
        Form owner = null, dialog = null, late = null;
        CancellationTokenSource input = null;
        Action<string> mark = name => {
            phase = name;
            progress.Add(new Dictionary<string, object> { { "phase", name }, { "utc", DateTime.UtcNow.ToString("o") },
                { "thread_id", Thread.CurrentThread.ManagedThreadId }, { "apartment", Thread.CurrentThread.GetApartmentState().ToString() },
                { "message_loop", Application.MessageLoop },
                { "synchronization_context", SynchronizationContext.Current == null ? null : SynchronizationContext.Current.GetType().FullName },
                { "owner", ControlState(owner) }, { "dialog", ControlState(dialog) }, { "late_dialog", ControlState(late) } });
            WriteDiagnostic(outputDirectory, "windows-mock-progress.json", progress);
        };
        try
        {
            mark("assert_persistent_ui_loop");
            AssertUiContext();
            Control.CheckForIllegalCrossThreadCalls = true;
            mark("prepare_fixture_data");
            string local = Guid.NewGuid().ToString(), peer = Guid.NewGuid().ToString(), revision = new String('a', 64);
            var snapshot = new Dictionary<string, object> { { "ok", true }, { "revision", revision }, { "registry",
                new Dictionary<string, object> { { "local", local }, { "nodes", new object[] {
                    new Dictionary<string, object> { { "id", local }, { "label", "Origine fixture" }, { "family", "windows" } },
                    new Dictionary<string, object> { { "id", peer }, { "label", "Nodo fixture · caffè" }, { "family", "linux" } }
                } } } } };
            var policy = State("observed"); policy["mode"] = "full"; policy["native_enabled"] = true;
            var report = new Dictionary<string, object> { { "node_id", peer }, { "origin_id", local }, { "revision", revision },
                { "route_digest", new String('b', 64) }, { "checked_at", "2026-11-25T12:00:00+00:00" }, { "elapsed_seconds", 0.1 },
                { "checks", new Dictionary<string, object> { { "ssh", State("authenticated") }, { "identity", State("match") },
                    { "helper", State("compatible_on_disk") }, { "policy", policy }, { "worker", State("unverified") }, { "desktop", State("unchecked") } } } };
            mark("prepare_mock_runtime");
            PrepareMock(fixture, snapshot, report);
            mark("install_fixture_environment");
            Environment.SetEnvironmentVariable("VINCULA_RUNTIME", fixture);
            Environment.SetEnvironmentVariable("VINCULA_NODE_REGISTRY", Path.Combine(fixture, "never-real-nodes.json"));
            mark("install_winforms_context.before");
            SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
            mark("install_winforms_context.after");
            Application.ThreadException += threadError;
            mark("load_app_assembly");
            var assembly = Assembly.LoadFrom(appAssembly);
            var windowType = assembly.GetType("DockWindow", true);
            var dialogType = windowType.GetNestedType("NodeCheckEditor", BindingFlags.NonPublic);
            mark("owner_constructor.before");
            owner = (Form)Activator.CreateInstance(windowType, Instance, null, new object[] { false }, null);
            mark("owner_constructor.after");
            mark("owner_handle.before");
            IntPtr hiddenOwner = owner.Handle;
            mark("owner_handle.after");
            Require(!owner.Visible, "DockWindow(false) was shown");
            mark("owner_catalog.before");
            Invoke(owner, "ShowCatalog", snapshot);
            mark("owner_catalog.after");
            var peerNode = Map(((object[])Map(snapshot["registry"])["nodes"])[1]);
            mark("check_constructor.before");
            dialog = (Form)Activator.CreateInstance(dialogType, Instance, null, new object[] { owner, peerNode }, null);
            mark("check_constructor.after");
            Require(!File.Exists(Path.Combine(fixture, "requests.jsonl")), "Node-check constructor performed I/O");
            string gate = Path.Combine(fixture, "gate");
            File.WriteAllText(gate, "hold");
            Set(owner, "busy", true); // Simulated transfer; no transfer process is started.
            mark("async_check_submit.before");
            Invoke(dialog, "Submit");
            mark("async_check_submit.after");
            Require((bool)Field(owner, "nodesBusy") && !((Button)Field(dialog, "checkButton")).Enabled, "Check is not asynchronous");
            Render(dialog, Path.Combine(outputDirectory, "windows-check-pending.png"), false, step => mark("pending_render." + step));
            input = new CancellationTokenSource();
            Set(owner, "inputSession", input); // No sharing process is started.
            mark("sharing_stop.before");
            Invoke(owner, "StopInput");
            mark("sharing_stop.after");
            Require(input.IsCancellationRequested && File.Exists(gate), "Stop waited for the check or transfer");
            Set(owner, "inputSession", null); input.Dispose(); input = null;
            Set(owner, "busy", false);
            File.Delete(gate);
            mark("async_check_wait.before");
            Wait(owner);
            mark("async_check_wait.after");
            mark("assert_observed_result");
            Require(Values(dialog)["ssh"].Text == "Autenticazione SSH riuscita", "Wrong SSH label");
            Require(Values(dialog)["identity"].Text.Contains("corrispondente"), "Wrong identity label");
            Require(Values(dialog)["helper"].Text.Contains("su disco"), "Helper was confused with a worker");
            Require(Values(dialog)["policy"].Text.Contains("Accesso completo"), "Policy full was not preserved");
            Require(Values(dialog)["worker"].Text == "Servizio autonomo non verificato", "Worker readiness was invented");
            Require(Values(dialog)["desktop"].Text.Contains("non verificati"), "Desktop readiness was invented");
            string metadata = ((TextBox)Field(dialog, "metadata")).Text;
            Require(metadata.Contains("aaaaaaaaaaaa") && metadata.Contains("bbbbbbbbbbbb"), "Result lacks revision/route binding");
            Render(dialog, Path.Combine(outputDirectory, "windows-check-result-top.png"), false, step => mark("result_top_render." + step));
            Render(dialog, Path.Combine(outputDirectory, "windows-check-result-bottom.png"), true, step => mark("result_bottom_render." + step));
            mark("assert_result_identity_binding");
            var reply = Copy(snapshot); reply["node_check"] = report;
            foreach (string key in new[] { "node_id", "origin_id", "revision" })
            {
                var wrong = Copy(reply); Map(wrong["node_check"])[key] = key == "revision" ? new String('c', 64) : Guid.NewGuid().ToString();
                Invoke(dialog, "Finished", wrong);
                Require(Values(dialog).Values.All(value => value.Text == "Verifica non conclusa"), "Mismatched result was accepted: " + key);
            }
            mark("assert_state_descriptions");
            var describe = dialogType.GetMethod("Description", Static);
            foreach (string name in new[] { "ssh", "identity", "helper", "policy" })
                Require((string)describe.Invoke(null, new object[] { name, new Dictionary<string, object> {
                    { "state", "unchecked" }, { "reason", "termination_unconfirmed" } } }) == "Arresto della verifica non confermato",
                    "Unconfirmed termination was not made explicit");
            foreach (var entry in new Dictionary<string, string[]> { { "ssh", new[] { "authenticated", "failed", "unchecked" } },
                { "identity", new[] { "match", "mismatch", "ambiguous", "unavailable", "unchecked" } },
                { "helper", new[] { "compatible_on_disk", "unverified", "missing", "unchecked" } },
                { "policy", new[] { "observed", "unchecked" } }, { "worker", new[] { "unverified" } }, { "desktop", new[] { "unchecked" } } })
                foreach (string state in entry.Value)
                    Require(!String.IsNullOrWhiteSpace((string)describe.Invoke(null, new object[] { entry.Key, State(state) })), "Empty state label");
            mark("assert_failure_messages");
            Invoke(dialog, "Failed", "Il runtime non ha risposto entro 75 secondi");
            Require(((Label)Field(dialog, "message")).Text.Contains("75 secondi"), "Timeout was not identified");
            Invoke(dialog, "Failed", "/private/diagnostic-must-not-display");
            Require(!((Label)Field(dialog, "message")).Text.Contains("/private/"), "Private diagnostics were displayed");
            File.WriteAllText(Path.Combine(fixture, "mode.txt"), "conflict");
            mark("conflict_check.before");
            Invoke(dialog, "Submit"); Wait(owner);
            mark("conflict_check.after");
            Require(Values(dialog).Values.All(value => value.Text == "Verifica non conclusa"), "Conflict did not reset the report");
            File.WriteAllText(Path.Combine(fixture, "mode.txt"), "ok");
            File.WriteAllText(gate, "hold");
            mark("late_constructor.before");
            late = (Form)Activator.CreateInstance(dialogType, Instance, null, new object[] { owner, peerNode }, null);
            mark("late_constructor.after");
            mark("late_submit.before");
            Invoke(late, "Submit");
            mark("late_dispose.before");
            late.Dispose();
            mark("late_dispose.after");
            File.Delete(gate); Wait(owner);
            mark("late_wait.after");
            Require(threadErrors.Count == 0 && !owner.Visible, "Late callback touched disposed controls or showed the owner");
            mark("assert_request_payloads");
            var requests = File.ReadAllLines(Path.Combine(fixture, "requests.jsonl")).Select(line => Map(Json.DeserializeObject(line))).ToArray();
            Require(requests.All(request => Convert.ToString(request["action"]) == "enrollment_check"
                && Convert.ToString(request["node_id"]) == peer && Convert.ToString(request["expected_revision"]) == revision), "Wrong UUID/revision request payload");
            string result = Json.Serialize(new Dictionary<string, object> { { "ok", true }, { "mode", "mock" }, { "network", "none" },
                { "checks", new[] { "hidden DockWindow(false), no Shown", "constructor without I/O", "six semantic report states",
                    "UUID/origin/revision binding", "policy full unchanged", "stop during async check plus simulated transfer",
                    "conflict reset and timeout", "late disposed callback", "offscreen label/button/metadata bounds" } } });
            mark("write_success_result");
            File.WriteAllText(Path.Combine(outputDirectory, "windows-mock-result.json"), result + Environment.NewLine, new UTF8Encoding(false));
            return result;
        }
        catch (Exception error)
        {
            primaryException = error;
            primaryFailure = new Dictionary<string, object> { { "ok", false }, { "phase", phase },
                { "exception", ExceptionDetails(error) }, { "progress", progress },
                { "owner", ControlState(owner) }, { "dialog", ControlState(dialog) }, { "late_dialog", ControlState(late) },
                { "async_errors", asyncErrors }, { "cleanup_failures", cleanupFailures } };
            // Persist before disposing anything, so a CreateHandle-related cleanup failure cannot replace this evidence.
            WriteDiagnostic(outputDirectory, "windows-mock-failure.json", primaryFailure);
            Console.Error.WriteLine("Native node-check QA failed at " + phase + " (" + error.GetType().Name + ").");
            throw;
        }
        finally
        {
            mark("cleanup.begin");
            Cleanup("release_fixture_gate", () => File.Delete(Path.Combine(fixture, "gate")), cleanupFailures);
            Cleanup("clear_owner_mock_state", () => {
                if (owner != null) { Set(owner, "inputSession", null); Set(owner, "busy", false); }
            }, cleanupFailures);
            Cleanup("dispose_input_token", () => { if (input != null) input.Dispose(); }, cleanupFailures);
            Cleanup("dispose_late_dialog", () => { if (late != null) late.Dispose(); }, cleanupFailures);
            Cleanup("dispose_check_dialog", () => { if (dialog != null) dialog.Dispose(); }, cleanupFailures);
            Cleanup("dispose_owner", () => { if (owner != null) owner.Dispose(); }, cleanupFailures);
            Cleanup("remove_thread_error_handler", () => Application.ThreadException -= threadError, cleanupFailures);
            Cleanup("restore_synchronization_context", () => SynchronizationContext.SetSynchronizationContext(oldContext), cleanupFailures);
            Cleanup("restore_cross_thread_checks", () => Control.CheckForIllegalCrossThreadCalls = oldCrossThreadChecks, cleanupFailures);
            Cleanup("restore_runtime_environment", () => Environment.SetEnvironmentVariable("VINCULA_RUNTIME", oldRuntime), cleanupFailures);
            Cleanup("restore_registry_environment", () => Environment.SetEnvironmentVariable("VINCULA_NODE_REGISTRY", oldRegistry), cleanupFailures);
            WriteDiagnostic(outputDirectory, "windows-mock-cleanup.json", cleanupFailures);
            mark(cleanupFailures.Count == 0 ? "cleanup.completed" : "cleanup.completed_with_failures");
            if (primaryFailure != null)
                WriteDiagnostic(outputDirectory, "windows-mock-failure.json", primaryFailure);
            if (primaryException == null && cleanupFailures.Count != 0)
            {
                var cleanupFailure = new Dictionary<string, object> { { "ok", false }, { "phase", "cleanup" },
                    { "tests_passed_before_cleanup", true }, { "cleanup_failures", cleanupFailures }, { "progress", progress } };
                WriteDiagnostic(outputDirectory, "windows-mock-failure.json", cleanupFailure);
                WriteDiagnostic(outputDirectory, "windows-mock-result.json", cleanupFailure);
                throw new InvalidOperationException("Native node-check QA cleanup failed; inspect its private diagnostic report.");
            }
        }
    }
}
