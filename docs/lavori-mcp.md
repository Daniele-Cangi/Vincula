# Lavori autorizzati sui nodi Vincula

Ogni PC ha un catalogo di programmi e un livello di accesso per l’account
che esegue il worker. Il client MCP può avviare un nome del catalogo, leggere
il lavoro o fermarlo. L’operatore registra sul nodo di destinazione il
percorso assoluto dell’eseguibile e tutti gli argomenti, compreso l’eventuale
script Bash o PowerShell. Chi può modificare questi file o usare direttamente
l’account del worker dispone dei permessi di quell’account. Registra percorsi
controllati dall’operatore.

| Livello scelto sul nodo | Effetto su una nuova richiesta |
| --- | --- |
| **Richiedi approvazione** (iniziale) | Il lavoro resta in attesa. Il pannello locale mostra il programma e permette di verificare gli argomenti prima di approvarlo o rifiutarlo. |
| **Accesso completo ai programmi autorizzati** | Il lavoro entra in coda e parte automaticamente, con i permessi del worker. |

Il passaggio a «Richiedi approvazione» rimette in attesa anche i lavori in
coda. Le richieste già in attesa conservano la decisione individuale quando
si passa ad «Accesso completo». Sostituire o rimuovere una registrazione
annulla le richieste di quel programma che devono ancora partire.

## Preparare ciascun PC

Prepara l’ambiente Python indicato nella [guida MCP](agent-mcp.md). Nei
comandi seguenti usa il suo interprete come `python` e parti dalla radice
del repository. Per le chiamate SSH verso Windows serve anche `py -3` sul
PATH dell’account SSH. L’account SSH sul nodo e quello del worker devono
accedere allo stesso profilo.

1. Esegui `python agent/jobs.py init`. Il comando prepara il database e
   installa la copia del gestore usata dalle richieste SSH. Ripetilo dopo
   un aggiornamento e riavvia il worker.
2. Registra ogni programma con `python agent/jobs.py register NOME ESEGUIBILE ARGOMENTI`.
   Ad esempio, su Ubuntu puoi usare `python agent/jobs.py register kernel /usr/bin/uname -r`.
   Su Windows, da PowerShell, puoi usare
   `python agent/jobs.py register os "$env:SystemRoot\System32\cmd.exe" /d /c ver`.
   Le richieste MCP specificano solo il nome registrato.
3. Sul nodo, avvia `python agent/jobs.py worker` in una sessione indipendente
   dal client MCP e dalla sua connessione SSH. Il worker esegue un lavoro
   alla volta. Puoi mantenerlo attivo come servizio utente su Ubuntu o come
   attività all’accesso su Windows, usando l’interprete dell’ambiente virtuale.
4. Apri **Permessi dei lavori** con `python agent/job_permissions.py`
   (su Ubuntu il pacchetto di sistema è `python3-tk`). Usa i due selettori per scegliere il livello. Il pannello
   mostra le richieste in attesa e può essere riaperto in seguito.

Da terminale puoi usare `jobs.py mode ask` o `jobs.py mode full`,
`jobs.py pending`, `jobs.py approve UUID`, `jobs.py deny UUID` e
`jobs.py remove NOME`, richiamando lo script con lo stesso interprete.
La politica è comune ai client che usano quel profilo Vincula.

## Contratto MCP e riconnessione

| Strumento | Uso |
| --- | --- |
| `list_jobs(node_id)` | Restituisce livello, programmi autorizzati e i 50 lavori più recenti. |
| `start_job(node_id, program, job_id)` | Il client genera un UUID prima dell’invio. Un nuovo tentativo con lo stesso nodo, programma e UUID ritrova il lavoro originale. |
| `read_job(node_id, job_id)` | Legge stato, tempi Unix, codice di uscita e gli ultimi 4096 byte di stdout e stderr. Il flag `truncated` identifica una coda parziale. |
| `stop_job(node_id, job_id)` | Annulla una richiesta in attesa o in coda. Per un processo attivo richiede l’arresto. Interroga di nuovo lo stato fino all’esito finale. |

Dopo la perdita del client o del collegamento, riapri MCP e consulta lo stesso
nodo e UUID. La coda e i log risiedono sul nodo, in
`~/.local/share/vincula-jobs` o `%LOCALAPPDATA%\Vincula-Jobs`. Il worker
prosegue anche mentre il client è scollegato. Un worker spento lascia i
nuovi lavori in attesa di esecuzione. Se il worker stesso si interrompe,
al riavvio marca i lavori precedentemente in corso come `unconfirmed`.
Prima di ripeterli occorre verificarne gli effetti sul nodo.

`completed` indica che il programma è terminato e il suo codice è disponibile,
anche se diverso da zero. `cancelled` indica un annullamento, `failed` un
errore del gestore. L’arresto usa il gruppo di processi su Ubuntu e un Job
Object su Windows. Un errore durante la verifica dell’arresto lascia il
lavoro in stato `unconfirmed`. I log completi restano nella directory del nodo e
richiedono spazio proporzionale all’output dei programmi registrati.

## Ripetere il collaudo

Usa un catalogo di test. Su ciascun nodo esegui `python agent/test_job_permissions.py`
per verificare approvazione, rifiuto, cambi di livello e revoca. Con il worker
attivo, esegui `python agent/check_jobs.py register` e seleziona il livello
completo per il collaudo bidirezionale.

Da Ubuntu esegui `python agent/check_jobs.py windows`. Da Windows esegui
`python agent/check_jobs.py ubuntu`. Ogni prova chiude il client MCP e apre
una nuova sessione mentre il programma gira, verifica UUID e log, recupera il
codice 7 del primo programma e verifica che l’arresto del secondo fermi anche
il suo processo figlio. Alla fine rimuovi le due
registrazioni di prova, `vincula-check-exit` e `vincula-check-stop`, e
ripristina il livello desiderato.

Per verificare il flusso di approvazione, seleziona «Richiedi approvazione»
sul nodo di destinazione. Ripeti il comando del client aggiungendo
`--await-approval UUID` con un nuovo UUID. Entro 90 secondi approva il
lavoro dal pannello sul nodo. Il test controlla che l’output sia vuoto
mentre attende e che il programma termini con codice 7 dopo l’approvazione.

Prima di un aggiornamento usa `python agent/jobs.py shutdown` sul nodo.
Il worker si chiude dopo il lavoro corrente, lasciando quelli in coda
per il suo prossimo avvio.
