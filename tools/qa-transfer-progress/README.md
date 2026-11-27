# Windows transfer progress checks

This fixture compiles the real WinForms source with a separate test entry point.
It checks byte/percentage boundaries, publication versus completion, stale
callbacks, cancellation and metadata failures. Rendering uses its own offscreen
form at 11 and 18 pt; it never shows `DockWindow`, whose normal `Shown` handler
would request SSH status. It also checks that the group Stop button stays enabled
while the transfer controls are busy.

No SSH, SFTP, clipboard, input provider or real sharing is invoked. No running
Vincula window is automated. The generated screenshots contain synthetic values.
These checks do not replace a live transfer or group-stop test.

Run from the repository root in Windows PowerShell with the installed .NET
Framework compiler (no extra package installation):

```powershell
$ErrorActionPreference = 'Stop'
$qaStage = Join-Path ([IO.Path]::GetTempPath()) ('vincula-progress-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qaStage | Out-Null
$provider = [CodeDom.Compiler.CodeDomProvider]::CreateProvider('CSharp')
$options = New-Object CodeDom.Compiler.CompilerParameters
$options.GenerateExecutable = $true
$options.OutputAssembly = Join-Path $qaStage 'TransferProgressTests.exe'
$options.CompilerOptions = '/target:exe /main:TransferProgressTests'
$options.ReferencedAssemblies.AddRange([string[]]@(
  'System.dll', 'System.Core.dll', 'System.Drawing.dll',
  'System.Windows.Forms.dll', 'System.Web.Extensions.dll'))
$sources = [string[]]@(
  [IO.File]::ReadAllText((Join-Path $PWD 'outputs/Linux Dock/LinuxDock.cs')),
  [IO.File]::ReadAllText((Join-Path $PWD 'tools/qa-transfer-progress/TransferProgressTests.cs')))
$result = $provider.CompileAssemblyFromSource($options, $sources)
if ($result.Errors.HasErrors) { throw ($result.Errors | Out-String) }
& $options.OutputAssembly $qaStage
if ($LASTEXITCODE -ne 0) { throw 'Transfer progress checks failed' }
```

The fixture prints the check count and writes `transfer-11.png` and
`transfer-18.png` in the fresh output directory. Inspect both images. All output
is disposable QA data; existing user files and application settings are preserved.

Live progress uses real observations, not elapsed-time estimates: upload samples
only its own remote staging file, while download samples its local temporary
file. Missing metadata produces an indeterminate bar. Bytes alone are capped at
99%; 100% requires successful publication and successful return of the transfer
operation. The sampler and its shutdown run on the transfer worker, separately
from the UI thread and the group Stop channel.
