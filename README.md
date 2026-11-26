# Vincula

Vincula collega due PC con un cavo Ethernet. **Windows Dock** gira su Ubuntu
e controlla Windows. **Linux Dock** gira su Windows e controlla Ubuntu.
Entrambe le app mostrano lo stato del PC remoto, trasferiscono file, eseguono
script ed espongono il desktop remoto e la condivisione di mouse e tastiera.

| App | Sorgenti | Guida |
| --- | --- | --- |
| Windows Dock per Ubuntu | [Python e GTK 3](outputs/Windows%20Dock) | [Avvio e uso](outputs/Windows%20Dock/LEGGIMI.txt) |
| Linux Dock per Windows | [C# e WinForms](outputs/Linux%20Dock) | [Avvio e uso](outputs/Linux%20Dock/LEGGIMI.txt) |

## Vincula Agent Infrastructure

Il [server MCP di Vincula](docs/agent-mcp.md) permette a un agente di scoprire
i nodi configurati e consultarne lo stato da Windows o Linux. Il [registro
dei nodi](docs/registro-nodi.md) consente di aggiungere altre macchine
raggiungibili via SSH.

Sul nodo di destinazione puoi scegliere **Richiedi approvazione** oppure
**Accesso completo**. Abilitando anche l'[esecuzione generale](docs/native-process-mcp.md),
l'agente può avviare nuovi programmi e argomenti senza registrarli uno per uno;
in accesso completo non richiede conferme per ogni lavoro. I processi usano
i privilegi dell'account del worker. I lavori continuano anche
quando il client MCP si scollega. [Configurazione e strumenti per i lavori](docs/lavori-mcp.md)

Il [servizio ricevente Linux di Vincula Core](docs/core-service.md) prepara
l'avvio del worker con il sistema, indipendente da login grafico, Codex locale
e condivisione del desktop. Un nodo che riceve soltanto lavori non richiede
chiavi SSH in uscita. L'agente controller usa il proprio MCP; autenticazione
e connessioni sono gestite da Vincula e dal sistema operativo.

I lotti di calcolo assegnano lavori indipendenti a Windows e Ubuntu,
li eseguono in parallelo e conservano gli identificatori per il recupero
dopo una riconnessione. [Calcolo distribuito via MCP](docs/calcolo-distribuito.md)

Un agente può cercare i comandi installati sul `PATH` dell’altro PC e
aprire una sessione interattiva per le voci del catalogo, con cronologia
e approvazioni sul nodo di destinazione. [Scoperta e sessioni MCP](docs/sessioni-mcp.md)

Via MCP l’agente può anche sfogliare, leggere e trasferire file tra i due PC
nelle cartelle abilitate dall’operatore. [Accesso ai file](docs/file-mcp.md)

Le capacità MCP permettono a un agente di scoprire e usare programmi
installati su Windows e Ubuntu, passando parametri e file e distribuendo
il lavoro tra i nodi. Per aggiungere una capacità, registra l’adattatore
sul nodo che la offre. [Capacità esterne via MCP](docs/capacita-mcp.md)

## Appunti condivisi

Apri **Linux Dock su Windows** per avviare la condivisione. Una volta
connesso, copia il testo, sposta il puntatore sull'altro schermo e incolla
con `Ctrl+V` o con il comando Incolla dell'app di destinazione.
Gli appunti si sincronizzano in entrambe le direzioni.

Nella scheda **Mouse e tastiera** puoi cambiare il lato dello schermo
Ubuntu anche con la condivisione attiva. Al cambio di lato la condivisione
si riconnette automaticamente. Linux Dock ricorda il lato scelto e le
preferenze per gli appunti condivisi e l'avvio automatico.
**Disattiva condivisione**, in alto accanto a **Desktop remoto**,
ferma la sessione e sospende i tentativi automatici fino alla prossima
apertura o alla riattivazione manuale. Per i singoli file usa
**Trasferimenti**.

Barrier 2.4 può svuotare gli appunti locali di Ubuntu all'avvio della sessione,
anche quando la casella è deselezionata.

## Configurazione

Il repository contiene il codice delle app e le icone. I binari dei componenti
esterni, le chiavi e i profili remoti vanno predisposti sui tuoi PC.

1. Assegna indirizzi alla rete Ethernet diretta. La configurazione di esempio
   usa `192.168.77.1/24` per Ubuntu e `192.168.77.2/24` per Windows.
2. Configura OpenSSH sui due PC con autenticazione a chiave e verifica le
   impronte delle chiavi host direttamente sui rispettivi PC. Sul lato Ubuntu
   usa un servizio SSH all'indirizzo del cavo sulla porta `2222`. Per Windows
   Dock salva la chiave host pubblica di Windows verificata in
   `outputs/Windows Dock/host.known_hosts`. Il formato è
   `192.168.77.2 ssh-ed25519 <chiave pubblica host>`. L'app cerca l'identità
   in `~/.ssh/id_ed25519_windows_dock`. La guida di Windows Dock elenca le
   variabili per personalizzare indirizzi e account.
3. Per Linux Dock, configura l'alias `linux-dock` in
   `%LOCALAPPDATA%\Linux Dock\ssh_config`. Imposta indirizzo, porta,
   account Linux, chiave di autenticazione e file delle chiavi host verificate.
   Usa `StrictHostKeyChecking yes`, `BatchMode yes` e `IdentitiesOnly yes`.
4. Per il desktop remoto, configura server VNC autenticati su loopback, porta
   `5909`, e installa i visualizzatori. Windows Dock cerca TigerVNC in
   `~/.local/share/linux-dock/vnc/bin/`. Per Ubuntu usa un visualizzatore
   compatibile con la versione del sistema installata.
   Per il controllo da Windows, Linux Dock cerca TightVNC Viewer sotto
   `%LOCALAPPDATA%\Linux Dock\vnc-package\PFiles\TightVNC\`.
   Prepara il profilo VNC locale di ciascuna app con accesso ristretto.
5. Per la condivisione da Windows, usa Deskflow 1.27 in
   `%LOCALAPPDATA%\Linux Dock\input-engine\deskflow-1.27.0-win-x64-portable`.
   Su Ubuntu, in una sessione X11, installa Barrier 2.4 e posiziona il client
   in `~/.local/share/linux-dock/vnc/bin/barrierc`. Installa
   [receive-input.sh](outputs/Linux%20Dock/receive-input.sh) come eseguibile
   `~/.local/share/linux-dock/vnc/receive-input`. Predisponi i profili TLS
   di Deskflow e Barrier con verifica reciproca delle impronte. Il server
   Deskflow ascolta su `127.0.0.1:24800` e l'inoltro SSH inverso espone la
   porta `24801` su loopback Ubuntu.
6. Per mouse, tastiera e appunti di testo su Wayland usa Deskflow con
   libportal 0.10 o successivo e autorizza l'accesso agli appunti in GNOME.
   Le guide delle due app descrivono il ricevitore e la modalità con Ubuntu
   come postazione principale. Il desktop remoto da Windows verso Ubuntu
   usa il server VNC X11.

## Avvio dai sorgenti

Su Ubuntu, installa Python 3, GTK 3 e il client OpenSSH. Poi avvia:

```sh
./"outputs/Windows Dock/windows-dock.py"
```

Su Windows, dalla directory del repository in **Windows PowerShell 5.1**:

```powershell
$dest = Join-Path $env:LOCALAPPDATA 'Linux Dock'
[void][IO.Directory]::CreateDirectory($dest)
$source = [IO.File]::ReadAllText((Resolve-Path 'outputs/Linux Dock/LinuxDock.cs').Path)
Add-Type -TypeDefinition $source -OutputType WindowsApplication `
  -ReferencedAssemblies 'System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Web.Extensions.dll' `
  -OutputAssembly (Join-Path $dest 'Linux Dock.exe')
Copy-Item 'outputs/Linux Dock/linux-dock.ico' $dest
Copy-Item 'outputs/Linux Dock/linux-dock-icon.png' $dest
& (Join-Path $dest 'Linux Dock.exe')
```

## Componenti esterni

Deskflow, Barrier, TigerVNC, TightVNC, x11vnc, OpenSSH e x2vnc si installano
separatamente. La [patch x2vnc](outputs/Windows%20Dock/x2vnc-no-clipboard.patch)
si applica a x2vnc 1.7.2. Il [testo GPL incluso](outputs/Windows%20Dock/x2vnc-COPYING.txt)
proviene dalla distribuzione x2vnc.
La [patch per gli appunti su GNOME 50](outputs/Windows%20Dock/deskflow-gnome50-clipboard.patch)
si applica a Deskflow 1.27.0 con la licenza di Deskflow.
Il [motore desktop multinodo gestito](third_party/deskflow/README.md) è un
incremento in sviluppo, con [stato e verifiche ancora necessarie](docs/desktop-multinode-implementation.md).
La patch comprende già quella GNOME 50; non va applicata due volte. Il nuovo
motore non è ancora il percorso distribuito sulle installazioni esistenti.

## Licenza

Il codice originale di Vincula è distribuito con la
[GNU General Public License v3.0 o successiva](LICENSE)
(`GPL-3.0-or-later`). Il testo integrale è disponibile nel file `LICENSE`.

La patch per x2vnc conserva la licenza `GPL-2.0-or-later`.
Il [testo della licenza di x2vnc](outputs/Windows%20Dock/x2vnc-COPYING.txt)
è incluso accanto alla patch.
