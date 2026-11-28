# Trasferimenti grandi: protocollo 2, primo incremento

Il Core espone sette tool aggiuntivi per trasferire file oltre il precedente
limite di 64 MiB. La nuova funzione richiede un consenso iniziale dell’operatore
su ciascun endpoint ed è disabilitata per impostazione predefinita. I vecchi tool
e i loro limiti restano compatibili. Questo incremento dipende dai piani e dal
pool di worker della PR #27; non è stato installato sui nodi di produzione.
I risultati e i limiti delle prove sono nel [resoconto del collaudo](collaudo-file-grandi.md).

Su Windows anche il Python selezionato da `py.exe -3` per l’helper SSH deve
avere `pywin32`, già richiesto dal worker. Un venv separato del worker non
installa dipendenze nel Python globale. La CI seleziona esplicitamente il Python
della matrice anche per il launcher, secondo la [configurazione ufficiale](https://docs.python.org/3.14/using/windows.html#customization).

## Flusso dell’agente

1. Leggere `get_large_file_info` su sorgente e destinazione: politica, `state_id`,
   modalità e attestazione del worker devono permettere l’operazione.
2. Usare `start_large_transfer` con UUID nuovo, `authority_node_id` stabile,
   endpoint `{node_id, root, path}`, `max_bytes` esplicito e scadenza.
   I nodi devono avere identità UUID registrate, distinte. I percorsi sono
   relativi alle cartelle già autorizzate; il destinatario applica le regole OS.
3. Conservare UUID e autorità insieme. Ripetere la stessa richiesta recupera
   i medesimi endpoint; non può cambiare file, cartelle o budget del trasferimento.
4. Chiamare `advance_large_transfer`, con `max_blocks` da uno a otto, e leggere
   `get_large_transfer` fino alla ricevuta `completed`. Ogni chiamata controlla
   al massimo quel numero di blocchi; copia soltanto quelli mancanti o corrotti.
   La preparazione iniziale e la pubblicazione finale sono job del Core.
5. Dopo la ricevuta autenticata di pubblicazione, `cleanup_large_transfer`
   elimina lo staging privato e libera le prenotazioni; conserva il file finale.

Il manifest viene fissato dall’autorità prima degli effetti e replicato sui nodi
coinvolti. Un coordinatore diverso può recuperarlo e continuare con lo stesso
handle, senza le credenziali dell’agente e senza creare un altro trasferimento.
Un database destinatario con `state_id` diverso rifiuta il vecchio handle.
Un’autorità irraggiungibile impedisce un trasferimento sconosciuto; una replica
coerente consente il recupero di uno già noto. Repliche in conflitto sono errori.

`get_large_transfer` legge solo metadata. `bytes_copied_this_call` distingue i byte
copiati da `blocks_checked_this_call`; `coordinator_next_block` è un cursore del
coordinatore, non una prova di integrità. Nello stesso processo MCP evita di
rileggere tutto il prefisso a ogni chiamata. Dopo cambio processo, origine o
epoch, la verifica riparte dal primo blocco, sempre con chiamate limitate. La
pubblicazione ricontrolla tutti i blocchi, il digest complessivo e la dimensione.
Ogni endpoint espone anche il suo ultimo `job`, con UUID, stato, codice di uscita
e conferma del cleanup. I tool di lettura dei job del Core permettono di leggerne
i log usando quell’UUID e il nodo dell’endpoint.

## Politica dell’operatore

Usare il Python e il contesto del worker installato, su ciascun endpoint:

```json
{
  "enabled": true,
  "max_file_bytes": 1099511627776,
  "max_reserved_bytes": 2199023255552,
  "max_active_endpoints": 2,
  "max_pending_endpoints": 64,
  "margin_bytes": 67108864
}
```

```bash
python /percorso/runtime/jobs.py large-files-policy /percorso/politica.json
python /percorso/runtime/jobs.py large-files-pending
python /percorso/runtime/jobs.py large-files-approve UUID-ENDPOINT --sha256 DIGEST
python /percorso/runtime/jobs.py large-files-deny UUID-ENDPOINT
```

`large-files-policy` supporta `--expected-revision DIGEST`. I tool MCP non
modificano questa politica né le cartelle autorizzate. L’esempio è un limite
massimo, non uno spazio preallocato o raccomandato per ogni macchina.

In `ask` si approvano gli intenti di esportazione e importazione, poi sulla
destinazione l’esatto manifest dei contenuti. L’approvazione lega anche lo
snapshot della cartella e l’identità iniziale della sorgente. In `full` queste
operazioni sono preautorizzate entro cartelle, quote e permessi OS, senza una
conferma per blocco. Un passaggio a `ask` invalida il consenso automatico;
revocare o sostituire una cartella recinta gli endpoint e richiede lo stop dei
loro job. Non vengono trasferite nuove chiavi né aperte sessioni grafiche.

La sorgente prenota `max_bytes` per una copia immutabile privata. La destinazione
prenota `max_bytes` per i blocchi e altri `max_bytes` per l’assemblaggio sul volume
del file finale. Le prenotazioni sullo stesso volume si sommano, con margine
configurato e controlli sullo spazio libero osservato. Errori, scadenza,
disconnessione, stop e perdita del worker **non liberano** spazio impegnato.
Le operazioni interne dichiarano CPU e memoria al pool comune; questi valori
sono ammissione cooperativa, non limiti OS del processo o del filesystem.

## Dati, interruzione e pubblicazione

Il formato binario ha metadata fino a 16 KiB e un blocco fino a 4 MiB, con
lunghezze e SHA-256 verificati. Nessun contenuto del file entra in base64 nelle
richieste MCP. La richiesta SSH usa un helper fisso e le rotte già registrate.
Il manifest contiene dimensione, hash del file, numero di blocchi e hash della
sequenza dei loro digest; gli hash per blocco sono paginati dal ledger.

Copia, hashing, fsync, spazio libero e verifiche dei percorsi avvengono fuori
dalle transazioni SQLite. Uno stop può quindi recintare l’epoch e fermare il
job senza attendere che termini una copia grande. I blocchi sono scritti in
staging privato e verificati prima del checkpoint; una ripresa verifica il
prefisso presente. La sorgente originale viene controllata prima e dopo la
copia. Un file cambiato non sostituisce silenziosamente il contenuto approvato.

La pubblicazione usa un link senza sovrascrittura. Un permesso durabile, legato
a endpoint, epoch, tentativo e manifest, ordina il punto di pubblicazione rispetto
a Stop e revoca. Se Stop precede il permesso, il link viene negato. Se il permesso
precede Stop, il file finale può comparire anche se il job viene interrotto
immediatamente dopo. `publication_may_exist` rende esplicita questa possibilità:
assenza della ricevuta non significa assenza del file. Solo una ricevuta esatta
può confermare il contenuto. La riconciliazione dopo ripresa verifica nuovamente
file, link di staging e proprietà prima di registrare una ricevuta.

Stop non certifica che un collegamento di rete già in corso sia terminato.
L’epoch nega nuovi checkpoint obsoleti e lo staging rimane prenotato. Un peer
irraggiungibile non impedisce lo stop dell’altro; il report mantiene l’errore
specifico e non certifica lo stop del peer assente. La ripresa è esplicita e
attende la conferma di cleanup del precedente job del Core, senza adottare PID.
Un annullamento prima dell’ammissione conclude subito il tentativo mai avviato.
Dopo perdita del worker, solo la prova di mancato avvio o la ricevuta di cleanup
legata al claim originale permette di chiuderlo come `unconfirmed`; conserva lo
staging e richiede ripresa esplicita, senza rilanciare automaticamente il lavoro.

ACL Windows protette sono applicate alla creazione dei file privati. I percorsi
escludono symlink/reparse point; la directory di pubblicazione è mantenuta aperta
per evitare sostituzioni durante il commit. Il cleanup usa identità dei link di
staging e non cancella mai il file finale. Un’interruzione prima della creazione
del sidecar di proprietà può lasciare uno staging non attestabile: viene
conservato per intervento dell’operatore, non cancellato per supposizione.

## Confini dell’incremento

Il coordinatore copia solo mentre riceve chiamate `advance_large_transfer`.
Le copie già preparate e i checkpoint sopravvivono alla sua chiusura; il protocollo
non include ancora un controller durevole che continui autonomamente, un relay
con quote persistenti o il trasferimento automatico di directory e dataset.
Lo staging delle singole RPC è temporaneo sul coordinatore: i limiti del frame
sono limiti di accettazione, non un limite disco OS mentre SSH scrive la risposta.
Servono helper installati attendibili e identità host già verificate.

Il protocollo privilegia verificabilità e recupero; una RPC per blocco non è una
promessa di throughput. Stream di più blocchi, budget del relay, garbage
collection esplicita dei parziali abbandonati, benchmark sulla triade e rollout
restano incrementi successivi. Non sono stati dimostrati vantaggi computazionali
per un dataset grande né imposti limiti hard a CPU, RAM o disco.
