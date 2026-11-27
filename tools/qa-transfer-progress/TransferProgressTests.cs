// SPDX-FileCopyrightText: 2026 Daniele Cangi
// SPDX-License-Identifier: GPL-3.0-or-later
// Compile with LinuxDock.cs and /main:TransferProgressTests. No real transport is invoked.
using System;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

public static class TransferProgressTests
{
    private static int checks;
    private static void Check(bool value, string name)
    { if (!value) throw new Exception(name); checks++; }
    private static void Models()
    {
        var state = new TransferProgressState(); long first = state.Begin();
        Check(state.Percent == null, "preparation is indeterminate");
        state.Accept(first, new TransferUpdate(TransferPhase.Transferring, 750489600, 1073741824));
        Check(state.Percent == 69, "observed bytes produce truthful percentage");
        state.Accept(first, new TransferUpdate(TransferPhase.Transferring, 1073741824, 1073741824));
        Check(state.Percent == 99, "data bytes alone never prove publication");
        state.Accept(first, new TransferUpdate(TransferPhase.Publishing, 1073741824, 1073741824));
        Check(!state.Accept(first, new TransferUpdate(TransferPhase.Transferring, 1, 1073741824)), "queued old phase ignored");
        state.Accept(first, new TransferUpdate(TransferPhase.Failed, 1073741824, 1073741824));
        Check(state.Percent == 99 && state.Terminal, "publish failure never displays 100");
        Check(!state.Accept(first, new TransferUpdate(TransferPhase.Completed, 1073741824, 1073741824)), "late completion cannot overwrite error");
        long second = state.Begin();
        Check(!state.Accept(first, new TransferUpdate(TransferPhase.Completed, 1, 1)), "previous transfer callbacks ignored");
        state.Accept(second, new TransferUpdate(TransferPhase.Transferring, -4, 100));
        Check(state.Bytes == 0 && state.Percent == 0, "negative observation clamped");
        state.Accept(second, new TransferUpdate(TransferPhase.Transferring, 500, 100));
        Check(state.Bytes == 100 && state.Percent == 99, "oversized observation clamped");
        state.Accept(second, new TransferUpdate(TransferPhase.Transferring, null, 100));
        Check(state.Percent == null && !state.HasSample, "failed observation becomes indeterminate");
        state.Accept(second, new TransferUpdate(TransferPhase.Transferring, 70, 100));
        Check(state.Bytes == 100, "late smaller observation cannot regress counters");
        state.Accept(second, new TransferUpdate(TransferPhase.Completed, 100, 100));
        Check(state.Percent == 100, "published completion is 100");
        Check(!state.Accept(second, new TransferUpdate(TransferPhase.Transferring, null, null)), "late sample cannot overwrite completion");
        long empty = state.Begin();
        state.Accept(empty, new TransferUpdate(TransferPhase.Transferring, 0, 0));
        Check(state.Percent == 0, "empty file not complete before publish");
        state.Accept(empty, new TransferUpdate(TransferPhase.Completed, 0, 0));
        Check(state.Percent == 100, "empty file complete after publish");
        long huge = state.Begin();
        state.Accept(huge, new TransferUpdate(TransferPhase.Transferring, Int64.MaxValue, Int64.MaxValue));
        Check(state.Percent == 99, "large byte counts do not overflow");
    }
    private static void Sampling()
    {
        using (var entered = new ManualResetEventSlim())
        using (var release = new ManualResetEventSlim())
        {
            int published = 0;
            var sampler = new TransferSampler(() => { entered.Set(); release.Wait(); return 1; }, x => Interlocked.Increment(ref published), 10);
            Check(entered.Wait(2000), "observer starts independently");
            Task disposal = Task.Run(() => sampler.Dispose());
            Thread.Sleep(80);
            Check(!disposal.IsCompleted, "dispose joins the outstanding observer");
            release.Set();
            Check(disposal.Wait(2000), "cancelled observer is joined");
            Check(published == 0, "cancelled outstanding observation never publishes");
        }
        using (var recovered = new ManualResetEventSlim())
        {
            int observed = 0, missing = 0, published = 0;
            var sampler = new TransferSampler(() => {
                if (Interlocked.Increment(ref observed) == 1) throw new IOException("synthetic stat failure");
                return 42;
            }, value => {
                Interlocked.Increment(ref published);
                if (!value.HasValue) Interlocked.Increment(ref missing);
                if (value == 42) recovered.Set();
            }, 15);
            Check(recovered.Wait(2000), "temporary metadata failure recovers");
            sampler.Dispose(); int final = published; Thread.Sleep(50);
            Check(missing == 1 && published == final, "unknown metadata reported; no callbacks after join");
        }
    }
    private static object Field(object target, string name)
    { return target.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).GetValue(target); }
    private static void Render(string directory, float font)
    {
        Type type = typeof(DockTransport).Assembly.GetType("DockWindow");
        using (var form = (Form)Activator.CreateInstance(type, BindingFlags.NonPublic | BindingFlags.Instance, null, new object[] { false }, null))
        {
            form.Font = new Font("Segoe UI", font); form.Size = new Size(900, 760);
            var tabs = (TabControl)Field(form, "tabs");
            tabs.SelectedTab = tabs.TabPages.Cast<TabPage>().Single(x => x.Text == "Trasferimenti");
            var model = (TransferProgressState)Field(form, "transferState"); long generation = model.Begin();
            model.Accept(generation, new TransferUpdate(TransferPhase.Transferring, 750489600, 1073741824));
            type.GetMethod("RenderTransfer", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(form, null);
            var sessionType = type.GetNestedType("ManagedGroupSession", BindingFlags.NonPublic);
            type.GetField("managedSession", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, Activator.CreateInstance(sessionType, true));
            type.GetMethod("GroupControls", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(form, null);
            type.GetField("busy", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            type.GetMethod("SetJobPagesEnabled", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(form, new object[] { false });
            // Host only the controls: never Show DockWindow, whose Shown event requests SSH status.
            // An offscreen QA Form creates real child handles and lays out visible controls for WM_PRINT.
            using (var host = new Form { ClientSize = form.ClientSize, Font = form.Font,
                BackColor = form.BackColor, ForeColor = form.ForeColor, ShowInTaskbar = false,
                StartPosition = FormStartPosition.Manual, Location = new Point(-32000, -32000) })
            {
                Control content = form.Controls[0]; form.Controls.Remove(content); host.Controls.Add(content);
                host.Show(); host.PerformLayout(); Application.DoEvents();
                using (var bitmap = new Bitmap(host.Width, host.Height))
                {
                    host.DrawToBitmap(bitmap, new Rectangle(Point.Empty, host.Size));
                    bitmap.Save(Path.Combine(directory, "transfer-" + font + ".png"));
                    int cyan = 0;
                    for (int x = 0; x < bitmap.Width; x += 3)
                        for (int y = 0; y < bitmap.Height; y += 3)
                        { Color pixel = bitmap.GetPixel(x, y); if (pixel.G > 100 && pixel.B > 100 && pixel.R < 80) cyan++; }
                    Check(cyan > 100, "render contains actual progress fill at font " + font);
                }
                host.Controls.Remove(content); form.Controls.Add(content);
            }
            var label = (Label)Field(form, "transferBytes"); var heading = (Label)Field(form, "transferStatus");
            Check(label.Height >= label.GetPreferredSize(new Size(label.Width, 0)).Height, "byte label wraps without clipping at font " + font);
            Check(heading.Bottom <= label.Top, "status and byte labels do not overlap at font " + font);
            Check(((Button)Field(form, "inputHeaderButton")).Enabled, "header stop remains enabled while transfer is busy at font " + font);
            // Direct Dispose never executes FormClosing and never starts/stops the synthetic session.
            type.GetField("managedSession", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, null);
        }
    }
    [STAThread]
    public static int Main(string[] args)
    {
        Models(); Sampling();
        if (args.Length == 1) { Directory.CreateDirectory(args[0]); Render(args[0], 11); Render(args[0], 18); }
        Console.WriteLine("PASS " + checks + " transfer progress checks; no SSH, SFTP, clipboard or real sharing invoked");
        return 0;
    }
}
