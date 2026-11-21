# Scoperta e stato dei nodi via MCP

Per scoprire i nodi e leggerne lo stato, Vincula espone due strumenti in sola lettura. Il client MCP avvia
`agent/server.py` sul proprio PC tramite standard input e output. Il server
legge lo stato locale sul posto e raggiunge l’altro PC con OpenSSH sul
collegamento Ethernet. La sessione MCP usa i permessi dell’account che
la avvia.

## Configurazione

Usa un checkout del repository sul PC da cui parte l’agente, con Python
3.10 o successivo e la [configurazione SSH di Vincula](../README.md#configurazione)
già funzionante.

- Su Ubuntu il server riusa l’identità e il file delle chiavi host
  verificato di Windows Dock. Se la chiave è protetta da passphrase,
  caricala nell’agente SSH e passa `SSH_AUTH_SOCK` nell’ambiente del
  server MCP se il client filtra le variabili ereditate. Se hai configurato
  variabili `VINCULA_*` per Windows Dock, passa anche quelle al server.
- Su Windows il server usa OpenSSH e l’alias `linux-dock` nel file
  `%LOCALAPPDATA%\Linux Dock\ssh_config`. Sul PC Ubuntu remoto deve essere
  disponibile `python3` (3.10 o successivo) per leggere lo stato.

Su Ubuntu, dalla radice del repository:

```sh
python3 -m venv work/vincula-mcp
work/vincula-mcp/bin/python -m pip install -r agent/requirements.txt
work/vincula-mcp/bin/python agent/check.py
```

Su Windows, da PowerShell nella radice del repository:

```powershell
py -3 -m venv work\vincula-mcp
.\work\vincula-mcp\Scripts\python.exe -m pip install -r agent\requirements.txt
.\work\vincula-mcp\Scripts\python.exe agent\check.py
```

Nel client MCP configura come comando il percorso assoluto del Python
dell’ambiente virtuale e come unico argomento il percorso assoluto di
`agent/server.py`. Usa il trasporto `stdio`.

Per avviare, ritrovare e fermare programmi consulta la [guida ai lavori autorizzati](lavori-mcp.md).
Per scoprire i comandi presenti sul nodo e inviarli uno alla volta consulta la
[guida alle sessioni interattive](sessioni-mcp.md).
Per sfogliare, leggere e trasferire contenuti consulta la [guida ai file](file-mcp.md).

## Strumenti di stato

| Strumento | Risultato |
| --- | --- |
| `list_nodes` | I nodi `ubuntu` e `windows` in ordine stabile. Per ogni nodo indica se è quello locale e l’esito della lettura di stato. |
| `get_node_status` | Accetta `node_id` e restituisce lo stesso identificatore, il ruolo locale o remoto e le metriche del nodo scelto. |

Entrambi riportano `available` quando la lettura riesce, `not_configured`
quando mancano i file SSH richiesti per il peer e `unavailable` quando
la lettura fallisce. Un errore sul peer lascia disponibile lo stato del
nodo locale. Un nodo indisponibile ha `metrics: null`. Gli identificatori
diversi da `ubuntu` e `windows` vengono rifiutati.

Le metriche comprendono il sistema operativo, la versione o il numero di
build, il tempo di attività in secondi, l’uso della CPU in percentuale
quando disponibile e la memoria fisica in byte. Il campo della memoria
disponibile usa `MemAvailable` su Ubuntu e `FreePhysicalMemory` su Windows.

`agent/check.py` apre una vera sessione MCP, scopre entrambi i nodi e
valida lo stato di ciascuno. Ripete la prova con un ambiente temporaneo
privo dei file SSH del peer per controllare che il nodo locale rimanga
consultabile.
