# Sessioni MCP aggiornate sui tre nodi

Il 22 novembre 2026 Ubuntu, Windows e Kali sono stati aggiornati alla release `6ac46058c6245b18cc46af7b066366b15c3b316c` e verificati con tre sessioni Codex native nuove. Le **39 letture MCP** sono riuscite: ogni origine vede e interroga tutti e tre i computer, riconoscendo correttamente il proprio nodo locale.

| Origine | CLI Codex | Nodo locale | Nodi disponibili | Letture riuscite |
| --- | --- | --- | --- | --- |
| ASUS Ubuntu | 0.169 | ASUS | ASUS, Lenovo, Kali | 13 |
| Lenovo Windows | 0.171 | Lenovo | ASUS, Lenovo, Kali | 13 |
| Kali X260 | 0.171 | Kali | ASUS, Lenovo, Kali | 13 |

Ogni agente ha eseguito `list_nodes` una volta e, per ciascun UUID, `get_node_status`, `list_jobs`, `list_file_roots` e `get_process_execution_info`. Il collector controlla i veri eventi MCP completati e i risultati strutturati del JSONL originale. Un exit zero o il solo riepilogo del modello non costituiscono un successo. I tentativi falliti restano separati; le prove riuscite Ubuntu e Windows non sono state ripetute per completare quella Kali.

Il [riepilogo pubblico verificabile](collaudi/2026-11-22-mcp-triade.json) contiene esiti e SHA-256 dei tre JSONL originali. Gli eventi completi, le configurazioni e le ricevute private restano sui PC del collaudo; non vengono pubblicati dati di autenticazione.

## Installazione e audit dopo tutte le chiamate

La release contiene 66 file di payload verificati contro manifest; Windows aggiunge il manifest e il proprio EXE compilato, per 68 file controllati. Il manifest canonico ha SHA-256 `3a78e0b3d9142aa593103ab5609c810778d81535a0b7c9ea8ece2f598c65b422`.

L’aggiornamento ha ritirato i client con server MCP obsoleti, fermato soltanto i worker, salvato snapshot SQLite consistenti, installato i sei helper e riavviato le stesse unità systemd e lo stesso task Windows. Gli inventari successivi provano il rinnovo dei processi e gli hash del codice. `get_process_execution_info` descrive il protocollo del helper; da solo non certifica il codice già caricato da un worker rimasto attivo.

Dopo tutte le 39 chiamate sono stati raccolti nuovi audit in sola lettura sui tre host. Confermano release e sei helper corretti, gli stessi worker aggiornati, grant e dati preesistenti preservati, zero lavori attivi. Gli storici conservano 15 lavori su Ubuntu, 14 su Windows e 13 su Kali. La modalità resta `ask`, il protocollo nativo è 1 e l’esecuzione nativa generale resta disabilitata: questo collaudo non amplia i permessi dell’operatore.

## Correzioni necessarie nei client

**Windows.** Lo staging elevato aveva prodotto directory leggibili da Administrators e SYSTEM, prive di un’ACE esplicita per l’utente normale. La release è stata corretta con lettura e avvio per lo stesso account, mantenendo il codice non scrivibile dal token Medium. Il test reale `Interactive/Limited` verifica identità del token, tutti i 68 file, EXE, import del server e lettura di helper, configurazione e SQLite; non avvia lavori. La successiva sessione Codex Windows ha completato le sue 13 chiamate con lo stesso token non elevato. I futuri installer devono superare questo gate prima di attivare configurazioni MCP o icone.

**Kali.** Codex 0.171 filtra l’ambiente dei server STDIO. Mancava il contesto necessario al launcher per raggiungere l’agente SSH desktop: il test manuale riusciva, mentre il client nativo chiudeva l’handshake. La sola modifica di configurazione inoltra esplicitamente i nomi `SSH_AUTH_SOCK`, `XDG_RUNTIME_DIR` e `DBUS_SESSION_BUS_ADDRESS` tramite `env_vars`, senza salvare valori o percorsi delle credenziali.

Il fallback sul bus utente è stato verificato anche con `SSH_AUTH_SOCK` assente nel processo chiamante: il server seleziona l’agente del gestore utente e ne verifica la capacità di firma con un nonce sintetico. La sessione Codex corretta ha poi completato 13 vere chiamate, senza iniezioni manuali nell’ambiente, override del modello, feature, trust o catalogo. Anche il daemon esistente espone 23 strumenti senza errori: non è servito un ulteriore riavvio. Il digest della sola tabella Vincula Kali cambia intenzionalmente per l’aggiunta dell’allowlist; gli altri campi restano invariati.

## GUI e ambito della prova

Le icone Ubuntu e Windows puntano alla release versionata, con backup dei collegamenti. L’attivazione Windows segue il gate Medium riuscito. Le finestre già aperte sono rimaste attive: il target aggiornato di un collegamento non dimostra che una GUI esistente sia stata sostituita.

Questa verifica certifica scoperta, stato, consultazione e continuità dei dati via MCP. La condivisione desktop multinodo richiede ancora backend Deskflow, TLS, consenso grafico e prove effettive di input e appunti. Il suo avanzamento è distinto nel [piano infrastruttura](piano-infrastruttura-multinodo.md).

## Evidenza precedente

Il primo collaudo della revisione `b470517f9c8b1c4e8cd3ddfff5879a44290c36ab` aveva eseguito dieci letture native per origine, trenta complessive. Resta una prova storica della prima installazione; non viene riutilizzato o attribuito alla release aggiornata. L’aggiornamento della CLI Windows da 0.161 a 0.171 era avvenuto prima di quella prova iniziale.
