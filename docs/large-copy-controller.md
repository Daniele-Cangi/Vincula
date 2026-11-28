# Controller Core per le copie autonome

Questo documento descrive il prossimo incremento del [piano dei dataset
grandi](large-file-transfers.md). È un progetto: nel protocollo 2 attuale la
consegna e il coordinamento dei blocchi dipendono dalle chiamate del client a
`advance_large_transfer`. Preparazione dell'export e pubblicazione finale sono
già job Core autonomi. Non esiste ancora un controller dell'intera copia
installato sui nodi.

Il risultato richiesto è una copia che continui dopo la chiusura del client MCP.
Un agente su un altro computer deve ritrovare lo stesso trasferimento, leggerne
progresso ed esito e poterlo fermare. Non deve mantenere un ciclo di avanzamento
né gestire credenziali SSH.

## Incremento implementato: preparazione dei metadati

Questo branch introduce il ledger additivo di intenti, decisioni e outbox,
il loader esplicito del catalogo Core e una facade locale. Non lancia controller,
non invia binding e non prenota o pulisce spazio relay. La policy locale parte
da `{"enabled": false, "max_pending_enrollments": 64}`. In `ask` la decisione
vincola il digest esatto dell'intento; in `full` una nuova proposta è
preautorizzata, mantenendo opt-in e pin della storia locale.

L'inizializzazione distribuisce insieme i 17 helper. Il worker aggiornato
attesta la presenza dell'adapter metadati: un helper nuovo sul disco non rende
valida l'attestazione del vecchio worker. `get_large_file_info` aggiunge
`copy_metadata`, con configurazione, policy e snapshot del worker; i tool MCP
restano 34. `runtime_implemented`, `launchable` e `operational` rimangono sempre
`false`, così come la verifica del provider e del trasporto.

Il file locale `large-copy-controller.json`, nella cartella di stato del Core,
contiene esattamente `version` (intero 1), `node_id` (UUID locale canonico) e
`registry_path` (percorso assoluto del catalogo di quel servizio). Il catalogo
deve confermare lo stesso UUID e la famiglia OS reale. Non vengono consultati
cataloghi dell'agente o rotte legacy come fallback. Un peer ancora senza SSH
può restare registrato: questo non lo rende utilizzabile dal futuro controller.
Configurazione e catalogo vengono letti fuori dalle transazioni SQLite.

La facade interna `copy-metadata` ammette soltanto `info`, `prepare`, `read` e
`stop`, con protocollo 1 e campi chiusi. `prepare` usa il manifest già presente
sull'autorità, il suo digest e il pin della storia, verificando gli UUID nel
catalogo. `proposed_endpoint_epochs` indica una proposta di intento: non è
un'attestazione dei peer o una barriera di cleanup. Le due righe di outbox
restano pendenti. Nessuna operazione wire approva intenti, attesta worker,
conferma binding/cleanup, riprende o avvia copie.

Lo Stop della facade può concludere soltanto i metadati mai inviati: nessuna
generazione/run assegnata, entrambe le righe correnti ancora pendenti e nessun
invio storico. Se il ledger è incompleto o una richiesta è stata registrata,
resta `stopping` senza inventare una prova di cleanup. Non è una prova di arresto
di processi o rilascio di quote del futuro runtime.

Le CLI owner-local aggiunte sono:

- `large-copy-policy FILE [--expected-revision DIGEST]`;
- `large-copy-pending`;
- `large-copy-approve UUID --sha256 DIGEST --epoch N --state-id UUID`, oppure
  `large-copy-deny` con gli stessi pin;
- `large-copy-emergency-stop UUID`, che applica soltanto il fence metadati;
- `large-copy-metadata FILE`, per una richiesta JSON della facade.

La suite locale passa 698 test con 20 esclusioni di piattaforma. I nuovi test
coprono ledger, catalogo, facade, CLI con helper installati e discovery MCP
stdio in ambienti temporanei. Non qualificano ancora il controller autonomo,
le credenziali dei servizi o un nuovo rollout sui tre PC.

## Autorità e processo

Il Core sull'autorità immutabile del manifest possiede controller, ledger e cache
relay. Un'altra origine comanda quel controller usando lo stesso UUID e la stessa
autorità. Se l'autorità non risponde, le repliche consentono di leggere il piano;
non autorizzano una seconda copia o l'elezione automatica di un nuovo writer.

Il controller usa una corsia I/O con limite locale distinto dagli slot dei
calcoli. Con `max_jobs=1`, prepare e commit degli endpoint devono poter partire
mentre il controller attende. Si riusano guardian e ricevute di cleanup del Core,
con ownership distinta per controller e figli SSH.

Il guardian mantiene il lock OS del trasferimento fino alla conferma di arresto
dell'intero albero posseduto. Un heartbeat scaduto, un PID assente o un lock
disponibile non dimostrano il cleanup. Prima di una nuova generazione servono la
ricevuta originale e le barriere Stop/resume sugli endpoint. Un peer assente
mantiene esplicita l'incertezza e conserva le prenotazioni.

## Collegamento agli endpoint

Un vecchio client manuale non deve scrivere contemporaneamente al controller.
Gli endpoint gestiti conservano un binding con digest del manifest, identità
della storia dell'autorità, UUID del controller e sua epoch. Le operazioni di
consegna e modifica richiedono il binding corrente, oltre ai pin già esistenti.
È fencing cooperativo degli strumenti Vincula: non introduce un'autorizzazione
SSH né isola agenti con accesso completo allo stesso account OS.

I due binding sono CAS locali, non una transazione distribuita. L'autorità
registra prima la proposta e un'outbox durabile. Una conferma mancante lascia
`binding_partial`; il recupero rilegge i peer e completa la stessa proposta.
Nessun controller parte prima di due binding confermati e dell'attestazione del
worker corrente. Un conflitto non produce fallback all'avanzamento manuale.

I trasferimenti non gestiti conservano il comportamento attuale. Per adottarne
uno attivo bisogna prima fermare e riconciliare il precedente I/O. Un risultato
già pubblicato restituisce la sua ricevuta, senza un nuovo processo di copia.

## Identità e consenso

Il controller usa catalogo e provider OS del proprio servizio Core. Verifica
rotte, UUID e impronte degli endpoint senza dipendere dal desktop o dall'agente
SSH personale. Un nodo ricevente non acquisisce per questo chiavi in uscita.

L'idoneità a ospitare il controller va osservata. Il primo incremento può usare
autorità Linux con provider di servizio già verificato, lasciando Windows come
client o endpoint. Il provider Windows headless rimane un requisito distinto;
il funzionamento del suo MCP personale non lo certifica.

La nuova funzione è disabilitata per impostazione predefinita. In `ask` il
consenso vincola manifest, autorità e storia, endpoint, binding e budget, oltre
agli intent e al contenuto già richiesti dal protocollo 2. In `full` il Core
procede entro grant, opt-in e quote locali. Revoca, cambio di grant o passaggio
a `ask` vengono ricontrollati prima degli effetti.

## Relay e risorse

Il relay conserva una finestra di un blocco da 4 MiB, con hash e fsync prima del
checkpoint. Il blocco viene ritirato soltanto dopo la conferma del checkpoint
destinatario. Il recupero verifica i byte persistiti; cursor e operazioni in
volo risiedono nel ledger, non nella memoria del server MCP.
La finestra non elimina l'export completo immutabile sulla sorgente né i costi
di spool e assembly dell'import sulla destinazione.

La policy separata limita controller I/O, spazio relay per volume, margine libero
e banda cooperativa. Admission degli endpoint e del relay sommano gli stessi
claim per volume in transazioni brevi. Una prenotazione parziale resta visibile;
la scadenza non libera quote con I/O incerto.

Il budget comprende blocco, staging, frame e log limitati. Il pump legge header e
payload con limiti durante la ricezione: controllare lo stdout temporaneo soltanto
dopo la risposta non costituisce una quota fisica. Un frame eccedente ferma solo
il processo SSH posseduto e lascia esplicito l'eventuale effetto remoto.

Un rate limiter condiviso ammette un burst limitato per frame e attende credito
prima di aprire l'exchange. L'attesa consuma la scadenza del trasferimento e non
allunga silenziosamente il timeout della RPC. Un timeout non dimostra che il peer
non abbia scritto: prima della ripresa servono barriere e verifica idempotente.
Non si promettono QoS OS, isolamento di CPU/RAM o aumenti di throughput.

## Controllo

Le operazioni MCP previste sono brevi: ensure idempotente della gestione, lettura
dello stato e controllo Stop/resume/cleanup con epoch attesa. Il ciclo di copia
appartiene al Core. Dopo l'adozione, `advance_large_transfer` legge o instrada la
gestione autonoma; gli endpoint rifiutano comunque writer privi del binding.

Lo Stop registra il fence senza attendere il lock I/O e interrompe soltanto i
processi posseduti. `stopped` richiede conferme locali e remote; altrimenti lo
stato resta `stopping_unconfirmed`. Un permit di pubblicazione già registrato
significa che il finale può esistere e deve essere riconciliato.

L'operatore conserva uno **Stop di emergenza locale** su ogni Core ed endpoint,
anche senza autorità, rotte o binding recuperabile. Questo comando può fermare e
fence i processi posseduti; non annulla binding, elegge controller, riprende copie,
libera quote incerte o elimina il file finale.

## Incrementi e criteri di accettazione

1. Ledger additivo, policy disabilitata, outbox e binding CAS. Separare il flow
   file dal server MCP, conservando manifest, consensi e dati preesistenti.
2. Corsia I/O supervisionata, lock custodito dal guardian e ownership registrata
   prima del lancio. Attestare la capacità del worker effettivamente attivo.
3. Cache relay, admission condivisa, pump limitato, rate limiter e riconciliazione
   delle operazioni incerte.
4. API MCP e CLI di policy/approvazione, poi qualificazione su runtime isolati.

Le cinque prove richieste coprono:

- chiusura reale del client MCP e recupero da un altro processo/origine, con
  file oltre 64 MiB, hash finale e sorgente limitata a un solo slot di calcolo;
- crash del controller con figlio SSH, due resume concorrenti, autorità assente
  e storia sostituita, senza nuova elezione o rilascio di quote tramite TTL;
- binding parziale, risposta persa, `ask`/`full`, revoca e rifiuto del vecchio
  writer manuale, conservando i trasferimenti non gestiti e legacy;
- quote aggregate relay/endpoint sullo stesso volume, banda, ENOSPC e frame
  eccedente, senza finale parziale né rilascio prematuro;
- Stop da Windows e Stop locale senza autorità, perdita del peer e pubblicazione
  prima/dopo il permit, senza fermare calcoli estranei o cancellare il finale.

Queste prove richiedono processi, file e database temporanei posseduti. Non
autorizzano riavvii, restart globali o interruzioni degli altri agenti. Qualsiasi
riavvio di un computer va concordato prima con l'operatore.
