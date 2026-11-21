# Accesso ai file via MCP

Le operazioni usano gli alias delle cartelle configurate dall’operatore su
ciascun PC. Aggiorna il checkout su entrambi i nodi, ripeti
`python agent/jobs.py init` per aggiornare il database e il gestore SSH
e riavvia eventuali worker. I file viaggiano via SFTP usando il collegamento
SSH di Vincula. Le letture e i trasferimenti usano i permessi dell’account
che risponde sul nodo.

## Cartelle e approvazioni

Registra un percorso assoluto già esistente sul nodo, ad esempio
`python agent/jobs.py files-grant scambio /home/utente/Scambio`.
Aggiungi `--write` per consentire anche la ricezione di file in quella
cartella. Usa percorsi sotto il controllo dell’operatore e scegli root
separate per i dati che vuoi esporre. Per revocare una root usa
`python agent/jobs.py files-revoke scambio`.

- **Richiedi approvazione** mette in attesa le nuove richieste. Il
  pannello locale **Permessi dei lavori** mostra l’operazione, l’alias
  e il percorso richiesto. Per trasferire un file servono le
  autorizzazioni della sorgente e della destinazione.
- **Accesso completo a programmi e cartelle autorizzati** autorizza
  automaticamente le operazioni previste dai permessi delle root.
  Si applicano sempre i limiti di percorso e il diritto di scrittura
  configurato per la destinazione.

In alternativa al pannello, usa `python agent/jobs.py files-pending`,
`files-approve UUID` o `files-deny UUID` sul nodo, sempre tramite
`agent/jobs.py`.

## Strumenti MCP

| Strumento | Uso |
| --- | --- |
| `list_file_roots(node_id)` | Mostra gli alias e l’eventuale diritto di scrittura, senza pubblicare il percorso assoluto. |
| `request_file(node_id, request_id, operation, root, path, offset)` | Con `browse` elenca fino a 40 elementi a partire dall’offset. Con `read` legge fino a 8 KiB di testo UTF-8 a partire dall’offset in byte. |
| `get_file_request(node_id, request_id)` | Consulta la richiesta e restituisce il risultato dopo l’autorizzazione. |
| `transfer_file(transfer_id, source, destination)` | Copia un file binario o di testo fino a 64 MiB fra due nodi registrati e restituisce dimensione e SHA-256 della copia. |

Il client genera gli UUID delle richieste prima di inviarle. Specifica
percorsi relativi alla root con `/` come separatore. Per sfogliare la
root usa un percorso vuoto. Collegamenti simbolici e punti di reparse
sono esclusi. Ogni nuova pagina usa un nuovo UUID e il valore
`next_offset` della pagina precedente. I risultati di richieste già
completate restano associati al loro UUID.

Un trasferimento ha due endpoint composti da `node_id`, `root` e
`path`. Richiamalo con lo stesso UUID e gli stessi endpoint mentre
attende l’autorizzazione o per recuperare la ricevuta dopo una risposta
persa. La destinazione viene pubblicata solo dopo la verifica di SHA-256
e dimensione. Il filesystem di destinazione deve supportare hard link
per la pubblicazione atomica. Se esiste già un file con il nome scelto,
la copia esistente è protetta.

## Collaudo

Per preparare dati di prova usa `python agent/check_files.py fixture`
su ciascun nodo. Il comando stampa il percorso della cartella di prova
nella directory dati di Vincula. Registra quel percorso con alias `vincula-files-test`
e diritto di scrittura. In modalità completa, esegui
`python agent/check_files.py windows` da Ubuntu e
`python agent/check_files.py ubuntu` da Windows.

Il test verifica elenco, testo UTF-8 e copia di un file binario da 512 KiB
in entrambe le direzioni. In modalità approvazione aggiungi
`--approval-transfer UUID` e approva sul nodo sorgente e su quello di
destinazione. Per ripetere il collaudo rimuovi solo la cartella di prova
creata dal comando `fixture` e usa nuovi UUID.
