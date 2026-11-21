# Scoperta degli strumenti e sessioni interattive via MCP

Da Windows e da Ubuntu puoi interrogare uno dei due nodi e aprire una
sessione interattiva per eseguire programmi del suo catalogo. Aggiorna il
checkout su entrambi i PC, esegui `python agent/jobs.py init` e riavvia i
worker come indicato nella [guida ai lavori autorizzati](lavori-mcp.md).

## Scoprire i comandi

`discover_tools(node_id, prefix, offset, limit)` elenca fino a 50 nomi
eseguibili presenti sul `PATH` dell’ultimo worker avviato. Prima del
primo avvio usa il `PATH` del processo che risponde alla richiesta.
`prefix` filtra i nomi senza distinzione tra maiuscole e minuscole.
Per proseguire usa `next_offset` della risposta con lo stesso filtro.

Il catalogo autorizzato si consulta con `list_jobs(node_id)`. Per esporre
un comando scoperto, l’operatore registra sul nodo il percorso assoluto
dell’eseguibile e gli argomenti con `python agent/jobs.py register`.

## Usare una sessione

Una sessione esegue una voce del catalogo per volta. Ogni invio avvia un
processo con gli argomenti registrati e conserva il suo esito nella
cronologia. Le variabili di shell e la directory corrente appartengono
al singolo processo. Gli script Bash o PowerShell con una configurazione
fissa possono essere registrati come qualsiasi altro programma.

| Strumento MCP | Uso |
| --- | --- |
| `open_shell(node_id, session_id)` | Crea o recupera la sessione. Il client genera l’UUID prima dell’invio e lo conserva per le riconnessioni. |
| `send_shell_command(node_id, session_id, job_id, program)` | Invia un nome del catalogo. Usa un nuovo UUID per ciascun comando e lo stesso UUID per riprovare dopo una risposta persa. |
| `read_shell(node_id, session_id, job_id, after)` | Consulta fino a 50 comandi e il loro stato. Con `job_id` legge anche stdout, stderr e codice di uscita del comando indicato. Usa `next_after` per continuare la cronologia. |
| `close_shell(node_id, session_id)` | Chiude la sessione e chiede l’arresto del comando attivo. La cronologia resta consultabile. |

Con **Richiedi approvazione**, il programma attende nel pannello locale
del nodo. Con **Accesso completo ai programmi autorizzati**, la stessa
voce entra subito in coda. Il livello viene controllato a ogni nuovo
comando. La cronologia risiede sul nodo di destinazione e rimane
accessibile dopo aver scollegato il client su uno dei due PC.

## Collaudo

Su ciascun nodo, con il suo interprete dell’ambiente virtuale, esegui
`python agent/check_shell.py register`. Con entrambi i worker attivi e
in modalità completa, esegui `python agent/check_shell.py windows`
da Ubuntu e `python agent/check_shell.py ubuntu` da Windows.
Il test cerca comandi nativi sul `PATH`, esegue una voce autorizzata,
cambia sessione MCP, ne recupera l’output e verifica l’arresto alla
chiusura. Al termine rimuovi le registrazioni temporanee
`vincula-shell-version` e `vincula-shell-wait`.

Per provare la modalità di approvazione seleziona **Richiedi approvazione**
sul nodo e aggiungi `--approval-job UUID` al comando di test.
Usa un nuovo UUID e approva la richiesta sul nodo quando appare.
