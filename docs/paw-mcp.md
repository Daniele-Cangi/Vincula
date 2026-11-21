# Analisi offline di PAW via MCP

`paw.analyze_offline` riceve riferimenti a file EML e una ripartizione
fra i nodi. Il nodo esegue l’analisi statica `paw full --no-egress` e
verifica il sigillo di ogni caso. Il rapporto unisce i risultati
nell’ordine del piano, con SHA-256 dell’originale, punteggio e decisione.

## Configurazione una tantum

Aggiorna il checkout e ripeti `python agent/jobs.py init` su entrambi i PC.
Installa la stessa versione di PAW nei due ambienti Python e registra un
alias scrivibile per la cartella di ingresso tramite `files-grant`.
Poi esegui sul nodo `python agent/jobs.py paw-register PERCORSO_PYTHON ALIAS`.
Usa il percorso assoluto dell’interprete che ha PAW installato e riavvia
il worker di Vincula. [Configurazione dei worker](lavori-mcp.md) · [Cartelle autorizzate](file-mcp.md)

I messaggi in ingresso possono trovarsi in qualsiasi cartella leggibile
già autorizzata. Per cambiare o rimuovere l’ambiente PAW usa
`paw-register` o `paw-revoke` sul nodo. I lotti salvati tengono traccia
della registrazione usata.

## Formulare un’analisi

1. Chiama `discover_capabilities` sui due nodi per conoscere versione,
   estensione accettata e limiti. Ogni parte accetta da uno a quattro
   file EML, fino a 4 MiB ciascuno. I file vengono identificati da
   `node_id`, alias `root` e `path` relativo.
2. Invia a `paw.analyze_offline` un UUID `analysis_id` e fino a due
   `parts`, una per nodo di esecuzione. Ogni parte contiene il
   `node_id` scelto e la lista `files`. Se un messaggio si trova
   sull’altro PC, Vincula lo trasferisce nell’alias di ingresso
   registrato sul nodo assegnato e verifica il checksum prima dell’analisi.
3. Per riprendere dopo una risposta persa o completare le richieste
   di trasferimento, reinvia lo stesso UUID con lo stesso piano.
   Dopo che i lavori sono entrati in coda, usa `get_paw_analysis`
   dallo stesso PC d’origine per recuperare stati ed esiti.
   Per un insieme di file o una ripartizione diversa genera un nuovo UUID.

**Richiedi approvazione** applica le approvazioni di trasferimento sui
nodi coinvolti e poi mostra il piano di ogni lavoro da approvare sul
nodo di esecuzione. **Accesso completo a programmi e cartelle autorizzati**
mette in coda le parti appena gli input sono pronti, usando le cartelle
configurate. Si può usare `stop_job` con l’UUID della singola parte.

## Collaudo

Il test `agent/check_paw.py` prepara messaggi sintetici con `prepare`
su entrambi i nodi. Registra una volta la capacità PAW con
`paw-register` e l’alias di test `paw-inbox`. Esegui `full` da
Ubuntu con i due nodi in accesso completo e poi `ask` da Windows
con i due nodi in modalità approvazione. Il primo piano usa quattro
file divisi 2+2, il secondo usa quattro file diversi divisi 1+3.
Nel secondo passaggio approva prima i trasferimenti e poi i lavori.

Le varianti `reference-full` e `reference-ask` calcolano i riferimenti
sullo stesso PC usando il suo interprete con PAW installato. I rapporti
da confrontare sono gli array JSON con hash, punteggio, decisione e
verifica del sigillo.
