# Verifica delle finestre «Verifica nodo»

Questi programmi di collaudo non avviano le app operative, condivisione o trasferimenti reali. I casi predefiniti usano runtime e cataloghi sintetici, senza rete. Lo stop viene verificato mentre un trasferimento è simulato. Il [resoconto](../../docs/collaudo-verifica-nodi.md) separa queste prove dai controlli sui computer reali.

## GTK

Richiede un Python con PyGObject/GTK3 e un display di prova o una sessione Linux disponibile. Dalla radice del repository:

```bash
python3 -B tools/qa-node-check/check-gtk.py --output /percorso/privato/qa-mock
python3 -B tools/qa-node-check/check-gtk.py --real-local --output /percorso/privato/qa-local
```

Il proprietario dell’app resta nascosto; solo il corpo della finestra viene renderizzato in un `Gtk.OffscreenWindow`. `--real-local` usa CLI e backend veri contro un HOME temporaneo, con un solo UUID locale senza rotta SSH e una policy SQLite sintetica. Controlla gli esiti e che i byte della fixture restino identici. Le dipendenze GTK di sistema possono avere avvisi propri; non vanno attribuiti al runtime Vincula o soppressi nei suoi test.

## Windows nativo

Compila prima `outputs/Linux Dock/LinuxDock.cs` con i riferimenti indicati nella README principale in una directory privata dedicata al collaudo, senza avviare l’eseguibile. Serve una checkout completa con `agent/nodes.py` e un Python disponibile come `py -3`, oppure `VINCULA_PYTHON` impostata. Non usare l’assembly di una finestra operativa già aperta.

In un nuovo **Windows PowerShell con `-STA`**, assegna percorsi assoluti per sorgente QA, assembly appena compilato, radice della checkout e directory di output privata nuova:

```powershell
$refs = @('System.dll','System.Core.dll','System.Windows.Forms.dll',
          'System.Drawing.dll','System.Web.Extensions.dll')
Add-Type -Path $harness -ReferencedAssemblies $refs
[NativeNodeCheckQa]::Run($appAssembly, $runtimeRoot, $freshOutput)
```

Il harness esegue un vero ciclo messaggi WinForms STA senza form principale. Crea un proprietario `DockWindow(false)` nascosto e mostra soltanto dialoghi sintetici trasparenti fuori schermo, usando un runtime Python fittizio. Verifica sei esiti distinti, UUID/revisione, errori e conflitti, callback dopo chiusura, dimensioni dei controlli e stop indipendente. Ogni fase controlla thread, ciclo messaggi e contesto WinForms; gli accessi fra thread sono trattati come errori.

Esito e tre PNG sono salvati in output. In caso di errore, `windows-mock-failure.json`, `windows-mock-progress.json`, eventuali errori asincroni e `windows-mock-cleanup.json` conservano la causa prima del cleanup. Un errore di cleanup impedisce il successo. Gli override d’ambiente, handler e controlli creati dal harness vengono ripristinati o chiusi; nessuna finestra o servizio operativo viene terminato.
