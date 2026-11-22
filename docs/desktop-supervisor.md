# Supervisor locale per le sessioni desktop

Questo incremento implementa il ciclo di vita dei processi locali, con prove isolate su componenti fittizi. Non è collegato alle GUI o al server MCP, non installa software, non apre tunnel e non avvia Deskflow. `desktop_ready` rimane sempre `false`, anche quando i processi della sessione sono `running`.

## Avvio e protocollo

Il runtime richiede soltanto Python 3.10 o successivo e la libreria standard. `agent/desktop_supervisor.py`, `agent/desktop_process.py` e `agent/desktop_security.py` devono restare nella stessa cartella.

```sh
python agent/desktop_supervisor.py serve --directory /percorso/privato/sessione
```

La cartella contiene il controllo di una sola istanza. Su Linux deve appartenere all’utente ed essere privata (`0700`). Su Windows le nuove cartelle di controllo ricevono una DACL protetta con accesso soltanto al SID dell’utente corrente, SYSTEM e Administrators. Le ACL sono lette tramite WinAPI e SID, senza parsing di nomi localizzati. Directory o token esistenti con owner non fidato, DACL nulla, ACE non supportate o permessi concessi ad altri SID vengono rifiutati; il runtime non ripara automaticamente quelle ACL. Le entry di diniego non concedono diritti e sono ammesse. Il controllo viene ripetuto prima di `prepare` e `commit`.

I file temporanei atomici ricevono la DACL protetta quando sono ancora vuoti, prima della scrittura del token o del piano; la sostituzione conserva quella protezione. Questo richiede un filesystem Windows con ACL supportate: un errore di ispezione o impostazione blocca l’avvio. Le API seguono la documentazione Microsoft su [GetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getnamedsecurityinfow) e [GetAce](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getace).

Il server ascolta soltanto su `127.0.0.1`, su una porta assegnata dal sistema. Il file privato `endpoint.json` contiene porta, token casuale e UUID dell’istanza. Il token non viene stampato dal comando. Un lock mantenuto dal processo impedisce a una seconda istanza di sovrascriverlo.

Il client legge il token dalla cartella e invia una richiesta JSON:

```sh
echo '{"action":"status"}' | python agent/desktop_supervisor.py request --directory /percorso/privato/sessione
echo '{"action":"stop"}' | python agent/desktop_supervisor.py request --directory /percorso/privato/sessione
```

Le azioni sono `prepare`, `commit`, `heartbeat`, `status`, `stop`, `shutdown`. Ogni risposta contiene `ok` e `session`, oppure un errore. Ogni connessione gestisce una richiesta, con limite di 64 KiB e timeout di lettura. Le richieste incomplete occupano soltanto il proprio handler; il monitor delle lease rimane separato.

`prepare` riceve un oggetto `plan` con campi esatti:

| Campo | Contenuto |
| --- | --- |
| `session_id` | Nuovo UUID canonico della sessione, comune ai futuri coordinatori |
| `revision` | SHA-256 della revisione del registro, 64 caratteri esadecimali |
| `local_id`, `primary` | UUID canonici del nodo locale e del principale |
| `members` | Elenco unico di UUID comprendente locale e principale |
| `commands` | Da uno a otto componenti foreground, con `name`, `argv` e `cwd` opzionale |

L’eseguibile iniziale di ogni `argv` deve essere un file con percorso assoluto. Non viene invocata implicitamente una shell. `prepare` valida e congela il piano, crea lo storico e non avvia alcun programma. `commit` richiede il suo `session_id` e avvia i componenti in un worker. Lo stato `running` significa che i guardiani hanno confermato lo spawn; non conferma input, TLS, portali o desktop funzionanti. La validazione del contenuto del registro, delle capacità e dei permessi appartiene al futuro coordinatore: il solo hash non dimostra che queste verifiche siano avvenute.

`heartbeat` richiede lo stesso UUID e rinnova soltanto la sessione corrente. La durata predefinita è cinque secondi, configurabile da 0,5 a 60 con `--lease-seconds`. `commit` e `heartbeat` controllano la deadline sotto lock: una sessione scaduta non può essere riattivata anche se il monitor non ha ancora osservato la scadenza. Il controllo viene ripetuto dopo operazioni di I/O preliminari e prima dello spawn, così un disco lento non riattiva un piano scaduto.

`stop` è il comando di emergenza dell’owner locale: non richiede l’UUID della sessione, risponde subito con lo stato di arresto e non attende la fine di altri lavori. Lo stato terminale viene osservato con `status`. `shutdown` esegue lo stesso arresto prima della chiusura del servizio; una volta autenticato e accettato viene completato anche se l’ack si perde o il client chiude il socket. Il client mantiene questi due comandi di emergenza utilizzabili se i permessi della cartella o del token sono stati ampliati mentre la sessione era attiva: continua a inviare il token per l’autenticazione, senza applicare il controllo ACL client che blocca le altre operazioni. L’eccezione permette soltanto l’arresto; `prepare` e `commit` vengono rifiutati dal server quando lo storage del controllo è insicuro.

## Stati, contenimento e recupero

La sequenza normale è `idle → prepared → starting → running → stopping → stopped`. La scadenza produce `expired`; la perdita di un componente produce `failed` e arresta gli altri. Anche un componente che termina con codice zero interrompe la sessione: questi componenti sono servizi foreground che devono rimanere presenti. Un errore durante l’avvio provoca il rollback di quelli già avviati. L’avvio è locale, non è ancora un commit distribuito fra macchine.

Ogni componente è figlio di un guardiano separato. Il programma riceve stdin nullo e scrive log su file, così nessun output deve essere drenato dal canale di stop. Il guardiano controlla due segnali indipendenti:

- EOF della pipe privata: il supervisor è terminato o ha richiesto lo stop.
- Heartbeat UDP locale con token distinto per il guardiano: il supervisor è bloccato o non rinnova il controllo. La durata predefinita è due secondi, configurabile da 0,5 a 10 con `--guardian-lease`.

Gli heartbeat sono datagrammi non bloccanti: un lettore fermo non può riempire una pipe e trattenere il lock del controllo. La pipe non trasporta payload o dati dei trasferimenti.

Su Linux il componente crea una propria sessione/gruppo di processi. Il guardiano mantiene il leader non raccolto tramite `waitid(..., WNOWAIT)` fino alla pulizia del gruppo, poi invia TERM/KILL e raccoglie il figlio. I processi foreground e i discendenti che restano nel gruppo vengono arrestati anche se muore il supervisor. Un programma che si stacca dal gruppo con una propria sessione non è contenuto; l’uccisione forzata del guardiano Linux stesso non ha la garanzia kernel di cleanup. Per coprire anche quel caso serve un successivo contenitore cgroup/systemd. Questo primo adapter dovrà quindi avviare Deskflow foreground, senza daemonizzazione.

Su Windows il guardiano si assegna a un Job Object con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` prima dello spawn. I discendenti entrano nel job alla creazione: non esiste una finestra tra spawn e assegnazione del programma. Il job termina l’albero anche se il guardiano muore. L’implementazione usa `ctypes`, senza pywin32; se il sistema impedisce l’assegnazione al job, l’avvio fallisce prima di eseguire il programma. Le primitive seguono la documentazione Microsoft su [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

Il supervisor controlla soltanto guardiani che ha creato e le relative pipe. I PID persistiti sono diagnostici: al riavvio non vengono adottati né terminati. Un nuovo token e un nuovo UUID identificano l’istanza; lo stato torna `idle` e segnala `previous_session`. I file `<session_id>/plan.json`, `state.json`, log e specifiche dei guardiani restano nello storico. Il codice d’uscita viene conservato anche se il messaggio viene letto dopo la terminazione del processo. Un UUID di sessione con una cartella già presente non viene riutilizzato.

## Prove e integrazione successiva

`agent/test_desktop_supervisor.py` avvia soltanto processi sintetici in cartelle temporanee e verifica autenticazione, stop con componenti bloccati e client lenti, estraneità dei processi non posseduti, heartbeat/scadenza, EOF dopo crash, cleanup dei discendenti, restart, esclusività del controllo, rollback e conservazione del codice d’uscita nello storico. Le stesse prove vengono eseguite su Linux e Windows, dove esercitano realmente il Job Object.

Prima dell’uso desktop servono ancora adapter Deskflow e attestazione delle capacità, provisioning delle chiavi desktop e del forwarding limitato, TLS con impronte verificate separatamente da SSH, consenso grafico e coordinamento prepare/commit/heartbeat tra i nodi. I livelli agente `ask`/`full` non sono modificati e non sostituiscono il consenso desktop. Le GUI continueranno a usare il percorso legacy finché questi collegamenti non saranno implementati e collaudati.
