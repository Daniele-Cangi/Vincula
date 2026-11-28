# Piani nativi e ammissione delle risorse

Questo incremento permette all’agente di proporre invocazioni diverse per ogni
nodo, scegliere una destinazione mediante le risorse osservate ed eseguire lavori
indipendenti senza registrare nuovi comandi. Usa il trasporto Core esistente;
non richiede GUI, Deskflow, nuovi account o credenziali gestite dall’agente.

Le quote sono **ammissione cooperativa delle richieste dichiarate**. Non impongono
limiti OS a CPU, memoria o GPU del programma. `hard_limits_enforced` riporta tre
valori `false`; una dichiarazione inesatta non viene trasformata in un limite
fisico. Cgroup, Windows Job resource limits e isolamento dei device richiedono
un incremento separato. L’account del worker conserva i propri permessi OS.

## Osservazioni e politica locale

`get_node_resources(node_id)` legge capacità CPU, carico CPU, RAM, GPU NVIDIA
quando disponibili, quote dell’operatore, prenotazioni, coda, generazione del
worker e identità del database (`state_id`). Una capacità ignota rimane `null`;
non viene inventata una GPU o una quantità di memoria. Una GPU usa il suo UUID
stabile `GPU-…` o `MIG-…`, non l’indice di enumerazione.

La versione dell’helper non certifica un worker avviato: solo il worker pubblica
la propria generazione e heartbeat. Le osservazioni scadono dopo cinque secondi.
Il destinatario restituisce la durata di validità residua; il client sottrae
l’intero tempo della RPC, senza confrontare gli orologi dei due computer.
Stop e letture di lavori esistenti non richiedono dati hardware freschi.

La configurazione locale predefinita mantiene un solo lavoro alla volta.
L’operatore può fornire un file JSON al runtime installato:

```json
{
  "max_jobs": 2,
  "cpu_millis": 2000,
  "memory_bytes": 2147483648,
  "gpus": [],
  "legacy_defaults": null
}
```

```bash
python /percorso/runtime/agent/jobs.py resource-status
python /percorso/runtime/agent/jobs.py resource-policy /percorso/quote.json
```

Usare l’interprete e il contesto del worker configurato sul nodo; su Windows è
il percorso locale equivalente. L’opzione `--expected-revision DIGEST` della
seconda operazione evita di sovrascrivere una modifica concorrente. I tool MCP
non consentono di modificare queste quote.

`cpu_millis: 1000` dichiara un core logico; RAM e VRAM sono byte. Un budget
`null` usa la capacità osservata, senza promettere una misura quando manca.
`gpus: []` esclude GPU dalle richieste esplicite; `null` permette quelle
osservate. Questi campi non impediscono tecnicamente a un processo di usare un
device. `legacy_defaults: null` rende esclusivi i lavori senza dichiarazione,
compresi programmi registrati, shell, capability e protocollo nativo precedente.
Per consentire sovrapposizione anche a questi lavori l’operatore deve dichiarare
requisiti comuni adatti al loro carico. Tutti i percorsi condividono lo stesso
ledger; non esiste una coda separata che possa aggirare il numero massimo.

Il controllo usa la minore fra capacità osservata e quota configurata. Per RAM
e VRAM tiene conto anche dello spazio libero e delle dichiarazioni già prenotate:
è conservativo e può contare due volte la memoria dei propri processi già
visibile nelle misure OS. Non è un misuratore del consumo effettivo di ogni job.

## Processo singolo

`execute_process` conserva la richiesta precedente quando `resources` è omesso.
Con `resources` usa il protocollo 2 e include la dichiarazione nel manifest e
nel digest di approvazione:

```json
{
  "node_id": "UUID-del-nodo",
  "job_id": "UUID-nuovo-del-lavoro",
  "argv": ["python3", "-c", "print('pronto')"],
  "resources": {"cpu_millis": 1000, "memory_bytes": 67108864, "gpus": []},
  "expected_state_id": "UUID-letto-da-get_node_resources"
}
```

Il destinatario risolve l’eseguibile e autorizza la richiesta. In `ask` rimane
`pending` fino all’approvazione dell’esatto digest; in `full`, con esecuzione
generale già abilitata dal proprietario, entra in coda senza un’altra conferma.
Il worker prenota atomicamente slot e risorse prima dell’avvio. L’attesa in coda
non consuma il timeout del programma. La disabilitazione generale, il cambio di
modalità e la risoluzione dell’eseguibile mantengono i controlli preesistenti.

Il marker `@process-v2` con argv nullo evita che un vecchio worker interpreti
il lavoro come un comando registrato. Una richiesta nuova richiede un worker
compatibile attivo. La ripetizione dello stesso UUID e della stessa richiesta
ritrova il manifest già risolto; non riavvia un lavoro concluso.

## Piano eseguibile senza registrazioni

`submit_execution_plan` riceve `plan_id`, `authority_node_id` e da uno a dodici
task indipendenti. Ogni task propone da uno a sessantaquattro candidati:

```json
{
  "plan_id": "UUID-nuovo-del-piano",
  "authority_node_id": "UUID-autorità-stabile",
  "tasks": [{
    "candidates": [{
      "node_id": "UUID-linux",
      "process": {
        "argv": ["python3", "/dati/lavoro.py", "--parte", "0"],
        "cwd": "/dati",
        "timeout_seconds": 300,
        "resources": {"cpu_millis": 1000, "memory_bytes": 268435456, "gpus": []}
      },
      "estimated_seconds": 20,
      "setup_seconds": 0
    }, {
      "node_id": "UUID-windows",
      "process": {
        "argv": ["python.exe", "C:\\Dati\\lavoro.py", "--parte", "0"],
        "cwd": "C:\\Dati",
        "timeout_seconds": 300,
        "resources": {"cpu_millis": 1000, "memory_bytes": 268435456, "gpus": []}
      },
      "estimated_seconds": 12,
      "setup_seconds": 15
    }]
  }]
}
```

L’agente scopre programmi e file, prepara versioni compatibili e stima i costi.
Il piano non installa software, trasferisce input o interpreta automaticamente
codice per un’altra piattaforma. `setup_seconds` deve includere i costi noti di
preparazione e località dei dati. Non sono necessarie nuove registrazioni per
variare programmi, file, parametri o ripartizione in un piano nuovo.

La scelta usa compatibilità della piattaforma, capacità e quote, prenotazioni,
coda, carico CPU, lavoro già assegnato nel piano e stime del chiamante. Aggiunge
sei volte la latenza osservata della RPC come costo indicativo del controllo.
Non promette durata né vantaggio computazionale. Il benchmark a freddo della
triade ha misurato 24,637 secondi distribuiti contro 20,491 locali: evitare copie
e coordinamento inutili è parte della decisione, non un’accelerazione presunta.

La risposta contiene per ogni task candidato scelto, UUID del job, stato
atteso del destinatario, motivi e costi. Le osservazioni non sono prenotazioni;
due origini possono scegliere lo stesso nodo e il suo worker applicherà le
quote locali al momento dell’ammissione. Nessun task approvato viene spostato
silenziosamente su un altro computer quando il carico cambia.

## Autorità, ripetizione e cambio di origine

Conservare **entrambi** `plan_id` e `authority_node_id`. Il secondo è l’autorità
stabile del manifest e non cambia quando cambia il coordinatore MCP. Il piano
viene fissato con inserimento atomico sul suo database prima di ogni avvio,
quindi replicato sull’origine e sui destinatari assegnati. Proposte concorrenti
per la stessa richiesta adottano il piano già conservato; una richiesta diversa
o repliche in conflitto vengono rifiutate. Un ack perso si risolve ripetendo lo
stesso handle, non scegliendo un nuovo UUID.

`get_execution_plan` recupera manifest, log e risultati senza assegnare lavori
o scrivere repliche. Un’altra origine può ripetere `submit_execution_plan` con
la richiesta originale per completare pubblicazioni o invii interrotti, anche
se l’autorità è diventata irraggiungibile, purché trovi una replica coerente.
Un piano sconosciuto richiede l’autorità online. Il catalogo della nuova origine
deve conoscere l’autorità e i destinatari da contattare; UUID di candidati
originali non assegnati possono essere assenti senza impedire il recupero.
La perdita di un destinatario produce un errore della sua parte, conservando
gli altri risultati e gli stessi identificativi. Non c’è sostituzione automatica
del computer o replica arbitraria del medesimo effetto.

Ogni destinazione ha un `state_id` persistente, distinto dalla generazione del
worker. Dopo perdita o sostituzione della cronologia, un nuovo database non
può accettare il replay di un job appartenente alla vecchia identità. Queue,
lettura e Stop del piano controllano questa identità sul destinatario nella
stessa transazione dell’operazione. Ripristinare un backup che contiene ancora
lo stesso `state_id` conserva quella cronologia: non è rilevabile come clone.

`stop_execution_plan` verifica identità e digest originali e ferma soltanto i
job assegnati a quel piano. Stop non richiede approvazione del lavoro e rimane
indipendente dalla coda e dalle osservazioni hardware.

## Pulizia e aggiornamento del worker

Un solo worker proprietario mantiene un pool di thread e il lock del runtime.
Popen, avvio, attese e pulizia non trattengono la transazione SQLite. Ogni
programma ha un guardian separato: su Linux usa la supervisione dei discendenti
e mantiene il lock; su Windows possiede il Job Object e osserva un handle
originale del worker. I processi non vengono adottati mediante PID salvati.

Una prenotazione si libera soltanto dopo pulizia confermata. Dopo crash il nuovo
worker verifica una ricevuta privata vincolata a job, generazione e nonce, oppure
la prova durabile che quel lancio non era ancora possibile. Un semplice errore
di rete, timeout del client, cambio di heartbeat o PID assente non libera quote.
Senza prova la capacità rimane prenotata e il lavoro è `unconfirmed`. Il modello
non garantisce la pulizia se viene ucciso anche il guardian; per una garanzia
forte Linux serve il contenimento di sistema già documentato.

Prima dell’aggiornamento di un worker precedente, drenare i lavori attivi e
conservare database e policy. Il nuovo worker rifiuta di prendere in carico
vecchi `running`/`stopping` privi di ownership del ledger, anziché dichiararne
la pulizia. Inizializzare e distribuire tutti gli helper insieme; la sola copia
di `jobs.py` non costituisce un aggiornamento compatibile.

## Verifiche e limiti dell’incremento

Le suite `test_resource_policy`, `test_worker_resources`, `test_execution_plans`
e `test_direct_resources` verificano validazione, GPU sintetiche, collocamento,
digest, concorrenza del ledger e compatibilità v1. `test_execution_recovery`
usa tre database temporanei e un trasporto simulato per autorità, replica,
risposte perse, cambio origine e sostituzione della cronologia.
`test_worker_concurrency` avvia alberi di processi isolati, verifica slot,
Stop, crash e pulizia. `test_process_mcp` usa client e server MCP stdio reali
con un worker privato: `ask`, due programmi sovrapposti, terzo in attesa,
Stop del piano e riuso degli stessi UUID senza ripetere gli effetti.

Questi livelli di prova restano distinti dalla CI Windows nativa e dal collaudo
della triade installata. Nessuna prova sintetica qualifica da sola un guardian
Windows o il servizio in produzione. L’incremento non sostituisce il protocollo
file Core, che conserva il limite di 64 MiB, né completa enrollment o controller
Windows headless. Il [piano dataset](large-file-transfers.md) rimane separato.
