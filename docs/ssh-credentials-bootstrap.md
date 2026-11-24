# Credenziali SSH dopo il login

`agent/credential_bootstrap.py` prepara le credenziali nell’agente SSH locale. Non apre connessioni, modifica indirizzi o abilita funzioni remote. È un componente Linux standalone, separato dalla release MCP installata: può essere distribuito in una propria directory versionata senza cambiare il runtime immutabile.

La CLI legge le `identity_file` dei nodi remoti dal registro dell’origine. Non contiene UUID dei PC. Per una voce `legacy_route` serve una corrispondenza nel profilo locale; quel nome non viene interpretato come percorso, comando o programma da importare. I profili e i percorsi non si copiano fra PC.

Esempio di profilo locale, da adattare e salvare con proprietario dell’utente e modo `0600`:

```json
{
  "version": 1,
  "registry": "/home/utente/.config/vincula/nodes.json",
  "agent_source": "openssh",
  "askpass": "/usr/libexec/seahorse/ssh-askpass",
  "legacy_routes": {
    "windows": {
      "identity_file": "/home/utente/.ssh/id_ed25519_windows_dock"
    }
  }
}
```

`openssh` seleziona esattamente `$XDG_RUNTIME_DIR/openssh_agent`, coerente con il socket MCP Ubuntu attuale. `user-manager` legge invece il `SSH_AUTH_SOCK` del gestore utente desktop, come il bootstrap Kali. La configurazione deve scegliere il medesimo agente usato dal client MCP; non si tenta un altro socket se la chiave manca. Il runtime deve essere una directory privata dell’utente; il socket deve essere reale e dello stesso UID. Se il socket OpenSSH manca, `unlock` avvia soltanto l’unità utente esistente `ssh-agent.socket` e attende al massimo due secondi la sua comparsa; non crea agenti nuovi. `status` non avvia unità. Il modo `user-manager` non attiva o cambia l’agente selezionato. La presenza del socket o una lista di chiavi non bastano: per ciascuna `.pub` viene eseguita una prova di firma `ssh-add -T`.

Il modo `user-manager` richiede `/usr/bin/busctl` con supporto JSON: legge la proprietà D-Bus [`Manager.Environment`](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html) tramite [`busctl get-property --json=short`](https://www.freedesktop.org/software/systemd/man/latest/busctl.html). L’array di stringhe conserva il percorso letterale, inclusi spazi, Unicode, virgolette e caratteri speciali della shell. Il codice non interpreta né esegue la rappresentazione shell di `systemctl show-environment`; risultati malformati o più socket selezionati vengono rifiutati senza pubblicare l’ambiente.

Le chiavi private vengono soltanto controllate nei metadati: file regolari, dello stesso utente, senza accesso di altri utenti. Il codice legge solo la `.pub`; il caricamento è affidato a `/usr/bin/ssh-add` con argomenti separati e al prompt locale selezionato: askpass grafico standard oppure terminale esplicitamente abilitato. Non legge, esporta o salva passphrase e non modifica il formato, la cifratura o i permessi delle chiavi.

## Uso e risultati

```bash
python3 -I -B /percorso/credential_bootstrap.py --config /percorso/credentials.json --action status
python3 -I -B /percorso/credential_bootstrap.py --config /percorso/credentials.json --action unlock
```

`status` non richiede lo sblocco. `unlock` chiede l’input solo se una chiave configurata non firma. Se tutte firmano, riesce anche senza askpass, TTY o sessione grafica. Il campo `credentials_ready` significa soltanto che queste chiavi firmano nel socket selezionato al momento del controllo: non certifica rete, policy del destinatario, stato MCP, versione del worker o desktop.

I codici distinguono `agent_missing`, `agent_invalid`, `key_locked`, `key_invalid`, `graphical_session_missing`, `terminal_missing`, `unsupported`, `unlock_incomplete`, `unlock_timeout`, `busy` ed errori di input. `key_locked` significa che la chiave non può firmare attraverso quell’agente: non dimostra da solo che il privato sia cifrato. Lo stato dell’ultima richiesta viene scritto atomicamente in `$XDG_RUNTIME_DIR/vincula-credentials/status.json`, modo `0600`, sotto lock; è una misura datata, non una garanzia persistente. La verifica e il modo askpass producono solo un JSON sanitizzato; niente ambiente completo o contenuti delle chiavi. Nel modo terminale `ssh-add` usa direttamente i tre flussi TTY per il proprio prompt e i messaggi standard, poi la CLI stampa il JSON finale: per un consumatore automatico usare `status`, che non legge dal terminale.

L’avvio automatico aggiunge `--automatic` a `unlock`. Verifica con `loginctl show-session` il `XDG_SESSION_ID`, oppure usa `auto` quando la variabile manca: ID reale, stesso UID, tipo X11/Wayland, sessione locale e stato active/online. Se non può identificarla, non apre popup automatici; l’invocazione esplicita resta disponibile. Registra il tentativo **prima** del popup. Cancellazione, timeout o errore non provocano un altro popup automatico nello stesso login; un invio esplicito può riprovare. Un solo processo può chiedere lo sblocco alla volta. In modo askpass un timeout termina il gruppo di processi posseduto dal comando, incluso il suo prompt. In modo terminale termina soltanto il figlio `ssh-add`, mai il gruppo condiviso con il terminale. Prima del caricamento salva gli attributi termios del terminale di controllo e li ripristina in `finally` su successo, timeout, interruzione o errore: la terminazione forzata del figlio non lascia ECHO disabilitato. Questo controllo non legge input o passphrase. Durante un’interruzione il cleanup controlla se `Popen.wait` ha già raccolto il leader: in quel caso non invia segnali al suo vecchio PID/PGID, che potrebbe essere stato riutilizzato. La CLI gestisce anche SIGTERM, SIGHUP e SIGINT: registra il primo segnale e annulla l’attesa entro circa 100 ms, dopo aver acquisito il figlio. Termina e raccoglie soltanto gli helper posseduti, ripristina il terminale e rilascia il lock prima di uscire con codice `128 + segnale`; segnali ripetuti non interrompono il cleanup. SIGKILL non consente alcun cleanup applicativo. Il login seguente può eseguire un nuovo tentativo; le firme vengono comunque ricontrollate.

## Installazione locale reversibile

```bash
python3 -I -B /percorso/credential_bootstrap.py --config /percorso/credentials.json --action install-plan
python3 -I -B /percorso/credential_bootstrap.py --config /percorso/credentials.json --action render-autostart
```

Entrambe le azioni producono un piano o testo, senza installare nulla. Il piano identifica il sorgente e il suo SHA-256, una destinazione separata sotto `~/.local/share/vincula/credential-bootstrap/<sha256>/`, il contenuto `.desktop` e il rollback. L’Exec usa percorsi assoluti, quoting Desktop Entry e nessuna shell. Il piano legge il profilo locale già esistente: `prompt_mode=terminal` produce `Terminal=true`, mentre il default askpass conserva `Terminal=false`. La scelta del programma terminale resta al desktop; il codice non lancia QTerminal o altri emulatori direttamente.

Prima dell’installazione: congelare e rivedere hash e profilo, creare directory private `0700`, copiare solo il modulo standalone (`0600`) e verificarne il digest. Salvare l’eventuale `~/.config/autostart/vincula-credentials.desktop`, quindi pubblicare atomicamente il renderer verificato. Avviare prima `status`, poi un singolo `unlock` nella vera sessione grafica locale. Rollback: ripristinare il file autostart precedente o rimuovere la sola nuova voce; rimuovere soltanto la directory di questa versione quando non è usata. Non svuotare l’agente condiviso e non cambiare configurazione MCP, runtime, servizi SSH o policy remote.

Su Ubuntu serve un askpass grafico locale funzionante e il bus della sessione; un aggiornamento del sistema può cambiarne il percorso. Su Kali è disponibile QTerminal ma non un askpass configurato: il profilo può scegliere esplicitamente `"prompt_mode": "terminal"` e `"agent_source": "user-manager"`, omettendo `askpass`. In tale modo lo sblocco richiede stdin, stdout e stderr TTY e un terminale di controllo locale; un contesto SSH viene rifiutato. `ssh-add` eredita il terminale, con `SSH_ASKPASS` rimosso e `SSH_ASKPASS_REQUIRE=never`: nessun prompt personalizzato o lettura delle passphrase nel codice. Timeout e interruzioni fermano soltanto il figlio posseduto, senza segnali al gruppo del terminale o a un figlio già raccolto. L’automatico conserva la verifica del login grafico e un tentativo per sessione. Senza opt-in resta il default askpass: se manca, restituisce `unsupported`; non c’è un fallback implicito. Le chiavi già firmanti restano verificabili anche senza TTY. Il bootstrap non cambia Windows: il servizio `ssh-agent` e le sue credenziali rimangono amministrati dal sistema.

I test `agent/test_credential_bootstrap.py` usano file sintetici, socket Unix di fixture e helper simulati; una regressione avvia soltanto un figlio Python sintetico e registra zero segnali dopo che è stato raccolto. Due prove acquisiscono una PTY isolata come terminale di controllo, fanno disabilitare ECHO al figlio sintetico e verificano il ripristino di tutti gli attributi dopo timeout e interruzione, senza usare terminali reali o SSH. Coprono selezione dell’agente, firme vere richieste dal contratto, retry, concorrenza, timeout e cleanup, protezione dei file, privacy e renderer; non certificano ancora il login reale o i popup delle piattaforme.
