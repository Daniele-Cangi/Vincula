# Core Kali dopo riavvio, senza login grafico

Il 25 novembre 2026 il vero Codex sul Lenovo Windows ha eseguito via Vincula MCP un nuovo programma sul PC Kali dopo il riavvio, senza login grafico dell’utente sul destinatario. Ha letto il risultato e il codice di uscita `0`. Questo conclude il criterio di accettazione del Core ricevente della [PR #21](https://github.com/Daniele-Cangi/Vincula/pull/21), con il perimetro descritto qui e nelle [ricevute JSON](collaudi/2026-11-25-core-headless.json).

L’operatore ha autorizzato ed eseguito il riavvio, sbloccato localmente il disco cifrato e lasciato Kali alla schermata di accesso. **Lo sblocco del disco resta necessario per avviare questo sistema**: non è uno sblocco di chiavi SSH e non è un login grafico. Non sono state rimosse protezioni del disco o delle credenziali.

## Prova osservata

Il boot precedente era `6681a602-552e-432d-b052-591899560938`; quello verificato è `201d0c36-b26f-439d-956d-769514b226ad`. SSH e `vincula-core-worker.service` risultavano attivi e abilitati prima del lavoro. Il worker era già partito prima della prima sessione SSH di verifica.

Le letture OS prima e dopo il lavoro non mostrano sessioni utente X11 o Wayland. È presente il greeter LightDM, UID115 e classe `greeter`, oltre alle sessioni SSH remote e ai manager di servizio. Il greeter non è un desktop dell’utente; questa prova non afferma che Xorg sia assente o che il sistema non abbia pacchetti grafici installati.

Il client Codex Windows, CLI0.171.0, usa tre chiamate reali al solo MCP Vincula: `get_process_execution_info`, `execute_process`, `read_process`. Il destinatario conferma `full`, esecuzione generale abilitata e protocollo1. Il programma è un nuovo argv Python con nonce, non un comando preregistrato; nessuna conferma per quel lavoro viene richiesta.

| Ricevuta | Valore |
| --- | --- |
| Lavoro di collaudo Windows | `9dd3abe9-8757-4380-874c-3e9a74068cc1` |
| Lavoro eseguito su Kali | `dd3bc7e2-5bcd-454c-9e06-b67c4342cf24` |
| Manifest SHA256 del lavoro Kali | `0bf9c4e8a05c8eb51f6acec5b4d1c2b912ddde84dc87ca4af91d30c694297295` |
| Log eventi del client SHA256 | `ec0f85590903c16ee8cff67f5766793858f2ed101a0be37663b0526e22bc8da0` |

Il JSON emesso dal programma conferma il nuovo boot, UID1000 e cgroup `0::/system.slice/vincula-core-worker.service`. `DISPLAY`, `SSH_AUTH_SOCK` e `DBUS_SESSION_BUS_ADDRESS` sono assenti. Manifest originale, nonce, log completi e outcome del runtime sono verificati: programma avviato, conclusione confermata e codice zero. Dopo la chiusura del client Windows, l’agente su Ubuntu ha recuperato lo stesso lavoro, con identico manifest e nonce, senza rieseguirlo.

## Rete e precedenti tentativi

Durante i tentativi precedenti mancava su Kali la rotta di subnet `192.168.1.0/24`. Dopo il tentativo di riapplicazione con checkpoint e la successiva rimozione della seconda scheda Wi-Fi da parte dell’operatore, è rimasta connessa al Cube solo `wlan0`, indirizzo `.137`. La rotta diretta è presente anche dopo il riavvio e l’autenticazione da Ubuntu e Windows riesce. Non è dimostrato quale dei due interventi abbia ripristinato la rotta, né che due schede sullo stesso access point siano in generale incompatibili. Il tentativo di checkpoint precedente non viene dichiarato superato.

Sono conservati sia il primo errore di connessione del vero Codex Windows sia un successivo timeout preliminare, avvenuto prima dell’avvio dell’agente. Il launcher di collaudo è stato adattato soltanto nella cattura dei tre comandi preliminari: stdout su file privati, fasi identificabili, stessi timeout20/30/30s. Cinque test locali e una review indipendente verificano il delta; oracolo, richiesta del lavoro e gestione del processo Codex restano invariati. La causa del timeout precedente non è attribuita con certezza alle pipe. Modello, autenticazione, versione CLI e configurazione Vincula non sono stati cambiati.

La prova separata con il client SDK Windows vede tutti e tre i nodi ed esegue un altro lavoro. Le sue quattro chiamate non sono sommate alle tre del vero Codex e non ne sostituiscono la verifica.

## Perimetro

Il risultato riguarda il ricevente Kali e l’esecuzione generale preautorizzata entro i permessi dell’account `dac`. Non richiede Codex, Vincula Desktop, Deskflow o uno sblocco di chiavi SSH sul destinatario. Il [controller Ubuntu della PR #22](https://github.com/Daniele-Cangi/Vincula/pull/22) resta una componente distinta.

Non certifica un controller headless installato su Windows o Kali, la condivisione desktop sui tre PC o l’intero piano multinodo. Non modifica lo stato prudente `worker: unverified` del solo probe dei file: l’accettazione del servizio deriva da questo lavoro reale e dalle osservazioni OS. Il collaudo non distribuisce nuove GUI, non cambia policy o credenziali e non esegue merge.
