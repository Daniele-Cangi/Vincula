# Calcolo distribuito con Vincula

Un lotto raggruppa fino a 12 lavori indipendenti. Ogni lavoro richiama un
programma registrato sul nodo di destinazione. Su ciascun PC il worker
continua a eseguire un lavoro alla volta, mentre i due PC possono lavorare
contemporaneamente sullo stesso lotto.

## Preparazione

Aggiorna il checkout su entrambi i PC, ripeti `python agent/jobs.py init`
e riavvia i worker. Il comando aggiunge la tabella dei lotti al database
esistente. Registrazione, avvio dei worker e permessi seguono la
[guida ai lavori autorizzati](lavori-mcp.md).

Per avviare automaticamente i lavori su entrambi i PC, scegli **Accesso
completo ai programmi autorizzati** su entrambi i nodi. In modalità
**Richiedi approvazione**, ogni lavoro attende una decisione sul suo nodo.

## Invio e recupero

Il client genera un UUID e chiama `submit_batch`. Per esempio:

```json
{
  "batch_id": "4700f21b-8400-46da-a9b9-7a9411d2ba2b",
  "tasks": [
    {"program": "vincula-primes-a", "node_id": "ubuntu"},
    {"program": "vincula-primes-b", "node_id": "windows"}
  ]
}
```

Ometti `node_id` per lasciare che Vincula scelga, tra i nodi raggiungibili
che registrano quel programma, quello con meno lavori già assegnati
nel lotto. Specifica entrambi i nodi quando il lavoro richiede una
ripartizione precisa. L’assegnazione resta fissa dopo il primo invio.

| Operazione | Risultato |
| --- | --- |
| `submit_batch(batch_id, tasks)` | Salva il piano sul PC d’origine prima dell’invio. Restituisce nodo, UUID del lavoro ed esito dell’invio per ogni attività. |
| `get_batch(batch_id)` | Da quel PC consulta i lavori sui due nodi e recupera stati e codici di uscita nell’ordine originale. |
| `read_job(node_id, job_id)` | Legge stdout e stderr di una parte. Il client può combinare gli output secondo la procedura di calcolo. |
| `stop_job(node_id, job_id)` | Ferma la singola parte sul nodo assegnato. |

Se un nodo non risponde, il rapporto mostra l’errore per le sue parti
e conserva gli esiti dell’altro nodo. Per ritentare, usa `submit_batch`
con lo stesso UUID e gli stessi elementi nello stesso ordine. Un invio
già ricevuto mantiene il suo UUID e il suo esito. Per cambiare i lavori
o le assegnazioni genera un nuovo lotto.

## Collaudo ripetibile

Su Ubuntu registra la prima metà della prova con
`python agent/check_batch.py ubuntu`. Su Windows registra la seconda
metà con `python agent/check_batch.py windows`. Con entrambi i worker
attivi e la modalità completa selezionata, esegui
`python agent/check_batch.py check` a turno da ciascun PC.

Il test ripartisce il conteggio dei numeri primi sotto 500.000, osserva i
due lavori in esecuzione contemporaneamente e confronta la somma con un
calcolo locale. Chiude e riapre il client MCP durante il lotto e verifica
che un nuovo invio conservi gli stessi UUID e tempi di creazione.
Alla fine rimuovi le registrazioni di prova nei rispettivi nodi e ripristina
il livello dei permessi desiderato.

La prova con PAW ha ripartito quattro messaggi sintetici in due gruppi
da due. Ogni nodo ha eseguito l’analisi statica offline con
`paw full --no-egress` e ha verificato i sigilli. Gli SHA-256 degli
originali, i punteggi e le decisioni coincidono con il riferimento
calcolato interamente su Ubuntu. [Primo collaudo PAW con Vincula](casi-duso/paw-windows-linux.md)
