// Isolated QA loaded separately from the frozen Vincula assembly.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public static class NativeEnrollmentQa
{
    private const BindingFlags Instance = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
    private const BindingFlags Static = BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public;
    private static Type catalog;

    private static object Invoke(object owner, string name, params object[] arguments)
    {
        try { return owner.GetType().GetMethod(name, Instance).Invoke(owner, arguments); }
        catch (TargetInvocationException error) { throw error.InnerException ?? error; }
    }

    private static object Field(object owner, string name)
    {
        return owner.GetType().GetField(name, Instance).GetValue(owner);
    }

    private static Dictionary<string, object> Map(object value)
    {
        return (Dictionary<string, object>)value;
    }

    private static Dictionary<string, object> Request(Dictionary<string, object> request)
    {
        try { return Map(catalog.GetMethod("Request", Static).Invoke(null, new object[] { request })); }
        catch (TargetInvocationException error) { throw error.InnerException ?? error; }
    }

    private static string PublicText(string path, int limit, bool hostKey)
    {
        try { return (string)catalog.GetMethod("ReadPublicText", Static).Invoke(null, new object[] { path, limit, hostKey }); }
        catch (TargetInvocationException error) { throw error.InnerException ?? error; }
    }

    private static Dictionary<string, object> Node(string id, string label)
    {
        return new Dictionary<string, object> { { "id", id }, { "label", label }, { "family", "windows" } };
    }

    private static Dictionary<string, TextBox> Fields(object editor)
    {
        return (Dictionary<string, TextBox>)Field(editor, "fields");
    }

    private static void Require(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
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
            Application.DoEvents();
            Thread.Sleep(10);
        }
        Application.DoEvents();
        Require(!(bool)Field(owner, "nodesBusy"), "GUI catalog request did not finish");
        Require(!owner.Visible, "The main window was unexpectedly shown");
    }

    private static void Render(Form editor, string destination, bool bottom)
    {
        // Only the dialog is mapped, fully transparent and outside the desktop.
        // The DockWindow owner is never shown: its Shown/status/network handlers cannot run.
        if (!editor.Visible)
        {
            editor.StartPosition = FormStartPosition.Manual;
            editor.Location = new Point(-32000, -32000);
            editor.ShowInTaskbar = false;
            editor.Opacity = 0;
            editor.Show();
        }
        for (int index = 0; index < 20; index++)
        {
            Application.DoEvents();
            Thread.Sleep(10);
        }
        if (bottom)
        {
            var viewport = Controls(editor).OfType<Panel>().First(control => control.GetType() == typeof(Panel) && control.AutoScroll);
            viewport.AutoScrollPosition = new Point(0, viewport.DisplayRectangle.Height);
            Application.DoEvents();
        }
        foreach (Button button in Controls(editor).OfType<Button>())
        {
            Require(button.Width >= button.PreferredSize.Width - 2, "Button width cropped: " + button.Text);
            Require(button.Height >= button.PreferredSize.Height - 2, "Button height cropped: " + button.Text);
        }
        foreach (Label label in Controls(editor).OfType<Label>())
            Require(!label.AutoEllipsis && label.Height >= label.PreferredHeight, "Label cropped: " + label.Text);
        foreach (CheckBox check in Controls(editor).OfType<CheckBox>())
        {
            using (Graphics graphics = check.CreateGraphics())
            {
                int glyph = CheckBoxRenderer.GetGlyphSize(graphics,
                    System.Windows.Forms.VisualStyles.CheckBoxState.UncheckedNormal).Width;
                int text = TextRenderer.MeasureText(graphics, check.Text, check.Font,
                    new Size(Int32.MaxValue, Int32.MaxValue), TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix).Width;
                Require(check.ClientSize.Width >= text + glyph + 4 - 2, "CheckBox text cropped: " + check.Text
                    + "; width=" + check.ClientSize.Width + "; height=" + check.ClientSize.Height
                    + "; textWidth=" + text + "; glyphWidth=" + glyph + "; enabled=" + check.Enabled
                    + "; requiredWidth=" + (text + glyph + 4) + "; padding=" + check.Padding
                    + "; font=" + check.Font.Name + " " + check.Font.SizeInPoints + "pt");
            }
        }
        using (var image = new Bitmap(editor.Width, editor.Height))
        {
            editor.DrawToBitmap(image, new Rectangle(Point.Empty, image.Size));
            image.Save(destination, System.Drawing.Imaging.ImageFormat.Png);
        }
    }

    public static string Run(string assemblyPath, string runtimeRoot, string outputDirectory)
    {
        Require(Thread.CurrentThread.GetApartmentState() == ApartmentState.STA, "Run this QA on an STA thread");
        Directory.CreateDirectory(outputDirectory);
        string fixture = Path.Combine(outputDirectory, "fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(fixture);
        string oldRuntime = Environment.GetEnvironmentVariable("VINCULA_RUNTIME");
        string oldRegistry = Environment.GetEnvironmentVariable("VINCULA_NODE_REGISTRY");
        SynchronizationContext oldContext = SynchronizationContext.Current;
        Form owner = null, import = null, export = null;
        try
        {
            var assembly = Assembly.LoadFrom(assemblyPath);
            catalog = assembly.GetType("NodeCatalog", true);
            var windowType = assembly.GetType("DockWindow", true);
            var dialogType = windowType.GetNestedType("InvitationEditor", BindingFlags.NonPublic);
            Environment.SetEnvironmentVariable("VINCULA_RUNTIME", runtimeRoot);
            string controller = Path.Combine(fixture, "controller", "nodes.json");
            string source = Path.Combine(fixture, "peer", "nodes.json");
            string localId = Guid.NewGuid().ToString(), knownId = Guid.NewGuid().ToString(), newId = Guid.NewGuid().ToString();
            Environment.SetEnvironmentVariable("VINCULA_NODE_REGISTRY", source);
            Request(new Dictionary<string, object> { { "action", "add" }, { "local", true }, { "node", Node(newId, "Nuovo computer · caffè") } });
            byte[] prefix = { 0, 0, 0, 11, 115, 115, 104, 45, 101, 100, 50, 53, 53, 49, 57, 0, 0, 0, 32 };
            byte[] wire = new byte[51];
            Buffer.BlockCopy(prefix, 0, wire, 0, prefix.Length);
            for (int index = prefix.Length; index < wire.Length; index++)
                wire[index] = 1;
            string keyFile = Path.Combine(fixture, "ssh_host_ed25519_key.pub");
            File.WriteAllText(keyFile, "ssh-ed25519 " + Convert.ToBase64String(wire) + " fixture\r\n", new UTF8Encoding(false));
            var endpoints = new object[] {
                new Dictionary<string, object> { { "host", "192.0.2.40" }, { "user", "worker" }, { "port", 22 }, { "network", "Ethernet" } },
                new Dictionary<string, object> { { "host", "peer.example" }, { "user", "operator" }, { "port", 2222 }, { "network", "Wi-Fi" } }
            };
            string keyText = PublicText(keyFile, 8192, true);
            Require(!keyText.EndsWith("\n"), "Public key terminal newline was not normalized");
            var invitation = Map(Request(new Dictionary<string, object> { { "action", "enrollment_export" },
                { "endpoints", endpoints }, { "public_host_key", keyText } })["invitation"]);
            string raw = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(invitation);
            invitation = Map(Request(new Dictionary<string, object> { { "action", "enrollment_preview" }, { "invitation_text", raw } })["invitation"]);
            Environment.SetEnvironmentVariable("VINCULA_NODE_REGISTRY", controller);
            Request(new Dictionary<string, object> { { "action", "add" }, { "local", true }, { "node", Node(localId, "Origine Windows") } });
            string identity = Path.Combine(fixture, "identity-never-read");
            var known = Node(knownId, "Nodo già registrato");
            known["ssh"] = new Dictionary<string, object> { { "host", "known.example" }, { "user", "worker" }, { "port", 22 },
                { "identity_file", identity }, { "bind_address", "192.0.2.10" }, { "known_hosts_file", Path.Combine(fixture, "existing-known-hosts") } };
            var snapshot = Request(new Dictionary<string, object> { { "action", "add" }, { "node", known } });
            SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
            owner = (Form)Activator.CreateInstance(windowType, Instance, null, new object[] { false }, null);
            IntPtr hiddenHandle = owner.Handle;
            Require(!owner.Visible, "DockWindow(false) is not hidden");
            Invoke(owner, "ShowCatalog", snapshot);
            import = (Form)Activator.CreateInstance(dialogType, Instance, null, new object[] { owner, invitation }, null);
            var submit = (Button)Field(import, "submit");
            var verified = (CheckBox)Field(import, "verified");
            Require(!submit.Enabled && !verified.Checked, "Fingerprint gate was not initially closed");
            Require(Fields(import)["identity_file"].Text == identity, "Unambiguous local identity was not prefilled");
            Require(!Fields(import).ContainsKey("identity_agent"), "Windows exposed an unsupported explicit identity agent");
            byte[] before = File.ReadAllBytes(controller);
            Invoke(import, "Submit");
            Require(!(bool)Field(owner, "nodesBusy") && before.SequenceEqual(File.ReadAllBytes(controller)), "Unverified submit changed the catalog");
            Render(import, Path.Combine(outputDirectory, "windows-import-unverified.png"), false);
            ((ComboBox)Field(import, "endpoint")).SelectedIndex = 1;
            verified.Checked = true;
            Require(submit.Enabled, "Fingerprint confirmation did not enable registration");
            Render(import, Path.Combine(outputDirectory, "windows-import-verified.png"), true);
            Invoke(import, "Submit");
            Wait(owner);
            var registered = Map(Field(owner, "nodesSnapshot"));
            Require(Convert.ToString(registered["state"]) == "registered", "Registration did not return registered state");
            Require(Map(registered["checks"]).Values.All(value => Convert.ToString(value) == "unchecked"), "Registration asserted an unverified capability");
            var route = Map(((object[])Map(registered["registry"])["nodes"]).Select(Map)
                .First(node => Convert.ToString(node["id"]) == newId)["ssh"]);
            Require(Convert.ToString(route["host"]) == "peer.example" && Convert.ToInt32(route["port"]) == 2222, "Wrong endpoint was saved");
            Require(Convert.ToString(route["identity_file"]) == identity && !File.Exists(identity), "Local identity path was changed or read-created");
            Require(((TextBox)Field(import, "message")).Text.Contains("NON verificati"), "Registered UI omitted unchecked state");
            Require(!submit.Enabled && !((Control)Field(import, "form")).Enabled, "Registered editor stayed editable");
            Render(import, Path.Combine(outputDirectory, "windows-import-registered.png"), true);
            import.Dispose();
            import = null;
            export = (Form)Activator.CreateInstance(dialogType, Instance, null, new object[] { owner, null }, null);
            var rows = (List<Dictionary<string, TextBox>>)Field(export, "endpoints");
            foreach (var entry in Map(endpoints[0]))
                rows[0][entry.Key].Text = Convert.ToString(entry.Value);
            Invoke(export, "AddEndpoint");
            foreach (var entry in Map(endpoints[1]))
                rows[1][entry.Key].Text = Convert.ToString(entry.Value);
            Fields(export)["public_host_key"].Text = keyFile;
            Render(export, Path.Combine(outputDirectory, "windows-export-top.png"), false);
            Render(export, Path.Combine(outputDirectory, "windows-export-bottom.png"), true);
            // Validate through the actual runtime without opening a native save picker.
            var exported = Map(Request(new Dictionary<string, object> { { "action", "enrollment_export" },
                { "endpoints", endpoints }, { "public_host_key", PublicText(keyFile, 8192, true) } })["invitation"]);
            Require(Convert.ToString(Map(exported["node"])["id"]) == localId, "Export regenerated the local UUID");
            Require(((object[])exported["endpoints"]).Length == 2, "Export lost an endpoint");
            string oversized = Path.Combine(fixture, "oversized.json");
            File.WriteAllBytes(oversized, new byte[32769]);
            bool bounded = false, privateNameRejected = false;
            try { PublicText(oversized, 32768, false); } catch (IOException) { bounded = true; }
            try { PublicText(identity, 8192, true); } catch (IOException) { privateNameRejected = true; }
            Require(bounded && privateNameRejected, "Public document input guards failed");
            Require(!owner.Visible, "The main window was shown during isolated QA");
            string result = "PASS: hidden DockWindow(false), no Shown/status/sharing; fingerprint gate, canonical preview, endpoint selection, local identity, registered/unchecked state, native dialog rendering, bounded public reads, runtime-validated export. Native file chooser was not automated.";
            File.WriteAllText(Path.Combine(outputDirectory, "windows-qa-result.txt"), result + Environment.NewLine);
            return result;
        }
        finally
        {
            if (import != null) import.Dispose();
            if (export != null) export.Dispose();
            if (owner != null) owner.Dispose();
            SynchronizationContext.SetSynchronizationContext(oldContext);
            Environment.SetEnvironmentVariable("VINCULA_RUNTIME", oldRuntime);
            Environment.SetEnvironmentVariable("VINCULA_NODE_REGISTRY", oldRegistry);
        }
    }
}
