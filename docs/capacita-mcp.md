# Capacità esterne via MCP

Una capacità espone un’operazione che un agente può scoprire e ripetere sui
nodi Vincula. Per aggiungerla basta installare il software, preparare un
adattatore che lo invoca e registrarlo sui nodi desiderati. Il contratto è
comune ai nodi. La relativa implementazione può chiamare eseguibili o API
diversi su Windows e Ubuntu.

## Registrazione sul nodo

Se stai aggiornando un’installazione, ferma il worker, esegui
`python agent/jobs.py init` e riavvia worker e server MCP. La cronologia
dei lavori resta consultabile con `read_job`. Le vecchie richieste
specializzate in attesa o in coda vengono annullate durante l’aggiornamento.

Salva adattatore e manifest fuori dai sorgenti Vincula. Esempio di manifest
per un’operazione che accetta file CSV:

```json
{
  "id": "dati.riepilogo",
  "version": "1",
  "description": "Produce una sintesi dei file CSV selezionati",
  "python": "/percorso/assoluto/venv/bin/python",
  "adapter": "/percorso/assoluto/riepilogo.py",
  "inbox": "scambio",
  "max_files": 4,
  "max_input_bytes": 4194304,
  "extensions": [".csv"],
  "parameters": {
    "separatore": {"type": "string", "required": true, "enum": [",", ";"]}
  }
}
```

Usa i percorsi locali del nodo. Puoi omettere `inbox` per una capacità
senza input da trasferire e lasciare `max_files` a zero per una capacità
che lavora senza file. `extensions` vuoto ammette qualunque estensione.
Per ricevere file dal secondo nodo, configura l’inbox come [cartella con
diritto di scrittura](file-mcp.md). I parametri ammettono stringhe, interi,
numeri finiti e booleani. Ogni parte accetta al massimo quattro file
e 512 byte di parametri in JSON.

L’adattatore implementa `run(inputs, parameters, workspace)`. Riceve per
ciascun input un indice nell’ordine del piano, una copia temporanea del
file (`path`, oggetto `Path`) e il digest SHA-256. Riceve i parametri
dichiarati e una cartella temporanea in cui lavorare. Può chiamare un
eseguibile locale o usare dipendenze installate nell’interprete indicato.
Restituisce un valore JSON fino a 3072 byte. Scrivi i log e instrada
l’output dei programmi invocati su stderr, oppure catturalo nell’adattatore.
Per conservare artefatti oltre la durata del lavoro, l’adattatore può
scriverli in una cartella concordata e renderli accessibili tramite gli
strumenti file di Vincula.

Su Ubuntu passa il manifest su standard input:

```sh
python agent/jobs.py capability-register < manifest.json
```

Su Windows esegui da PowerShell:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding UTF8 .\manifest.json | py -3 agent/jobs.py capability-register
```

Vincula conserva una copia del codice registrato. L’adattatore è codice
fidato ed esegue con i permessi dell’account del worker. Registra solo
codice e dipendenze che vuoi autorizzare sul nodo. Modificando
l’implementazione o il manifest, la nuova registrazione annulla i lavori
in attesa o in coda legati alla revisione precedente. Per revocarla usa
`python agent/jobs.py capability-revoke dati.riepilogo`. Un lavoro già in
esecuzione si può fermare con `stop_job`.

## Uso da parte dell’agente

1. Usa `discover_capabilities` su ciascun nodo per leggere capacità,
   versioni, tipi dei parametri e limiti degli input.
2. Passa a `submit_capability` un nuovo `run_id` (UUID), il
   `capability_id` e una parte per ogni nodo da usare. Per ciascuna parte
   scegli i parametri e i riferimenti ai file nelle cartelle autorizzate.
   Per distribuire lo stesso lavoro, scegli nodi con la stessa versione
   e lo stesso contratto.
3. In modalità **Richiedi approvazione**, approva gli eventuali
   trasferimenti e ripeti l’invio con lo stesso UUID e lo stesso piano
   per mettere i lavori in coda di approvazione. In modalità **Accesso
   completo**, i lavori partono secondo i permessi delle cartelle e le
   capacità registrate sul nodo.
4. Leggi gli esiti per nodo con `get_capability_run` dalla macchina
   che ha inviato il piano, anche dopo aver riaperto il client MCP. Ciascun
   esito contiene l’ID del lavoro, lo stato e il risultato JSON dell’adattatore.

La disponibilità di un programma dipende dall’account e dalla sessione che
eseguono il worker. Per un programma esclusivamente grafico, l’adattatore
deve avere un modo per pilotarne l’interfaccia nella sessione desktop
appropriata.
