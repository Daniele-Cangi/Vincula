# Core Linux: avvio di sistema e lavoro da Windows

Il 25 novembre 2026 la CLI generica `agent/core_service.py` è stata installata su Kali e verificata con il vero Codex Windows. Due nuove invocazioni Python, con UUID e parametri differenti, sono state eseguite mediante MCP senza registrare programmi o approvare singoli lavori. Entrambe hanno restituito log completi e codice di uscita zero. Il [riepilogo pubblico](collaudi/2026-11-25-core-ricevente.json) conserva revisioni, risultati e digest dei JSONL; eventi integrali e ricevute rimangono privati sui PC.

La seconda invocazione è avvenuta dopo un riavvio, con entrambe le chiavi SSH in uscita di Kali ancora bloccate. Il controllo in sola lettura prima e dopo il lavoro le conferma non utilizzabili nell'agente locale; non è stato richiesto alcuno sblocco. Windows ha usato la propria identità già gestita dal servizio OpenSSH. Un ricevente non deve autenticarsi in uscita per ricevere un lavoro.

## Installazione verificata

Il servizio `vincula-core-worker.service` usa l'account scelto dall'operatore e il rilascio worker `f304b91`. La procedura reale ha verificato pin, interprete e sintassi dell'unità, salvato una copia SQLite consistente e drenato il worker utente. Dopo l'installazione il worker di sistema risulta unico, attivo e abilitato persistentemente, con argv e lock corrispondenti; la vecchia unità utente è disabilitata. I digest delle tabelle preesistenti sono rimasti uguali durante il passaggio. Non sono stati inizializzati altri database o copiati helper.

La scelta dell'operatore `full` con esecuzione generale abilitata è persistente. I processi usano UID 1000 e il cgroup del servizio di sistema, con i privilegi dell'account; non viene concessa elevazione automatica. I tre file OS creati risultano amministrativi e corrispondenti ai payload pianificati. L'installazione non ha riavviato SSH o la rete; il relativo hook è stato esercitato dai successivi avvii.

Il vecchio autostart grafico di sblocco delle chiavi è stato disabilitato sul ricevente, conservando una copia ripristinabile. Chiavi personali, passphrase e profili SSH sono rimasti invariati. Lo sblocco locale del disco di sistema cifrato precede l'avvio dell'OS e rimane separato da Vincula.

## Agente reale e preautorizzazione

Ogni prova riuscita usa una nuova sessione Codex CLI 0.171 su Windows e soltanto tre chiamate: `get_process_execution_info`, `execute_process` e `read_process`. Il collector verifica eventi MCP reali, richiesta originale e digest del manifest, risultato strutturato, marker e nonce, codice di uscita confermato e ownership del processo originale. Il solo riepilogo testuale del modello non vale come prova.

| Prova | Chiamate MCP | Esito | Chiavi in uscita del ricevente |
| --- | --- | --- | --- |
| Servizio di sistema | 3 | exit 0, log completi | Nessuno sblocco richiesto dalla prova |
| Nuovo avvio e nuova invocazione | 3 | exit 0, log completi | Entrambe bloccate prima e dopo |

Il primo tentativo era stato bloccato dal client Codex prima dell'invio: il nodo era `full`, ma la chiamata MCP richiedeva conferma e il client usava `approval_policy="never"`. Nessun lavoro era stato creato. È stata quindi preautorizzata la sola tabella MCP Vincula con l'[impostazione ufficiale del client](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), come descritto nella [guida Core](core-service.md#autorizzare-lagente-una-volta). Modello, autenticazione, comando/argomenti/ambiente Vincula, sandbox locale e altri server sono rimasti invariati. La policy dei destinatari continua a distinguere `ask` e `full`.

## Avvio e limite del collaudo grafico

Nell'ultimo avvio osservato il worker era attivo a circa 40,5 secondi dall'avvio del kernel; anche SSH era attivo prima del primo login grafico locale, registrato a circa 61,5 secondi. Il worker usa il cgroup di sistema e il programma non eredita `DISPLAY`, `SSH_AUTH_SOCK` o `DBUS_SESSION_BUS_ADDRESS`.

**Non è ancora certificato il collaudo completo senza login grafico:** nei cicli osservati è stato aperto il desktop prima dell'esecuzione dei lavori. L'assenza delle variabili nel processo dimostra il suo contesto di esecuzione, non l'assenza di sessioni grafiche sull'intero PC. La PR resta in bozza per questa verifica. Un eventuale nuovo riavvio di Kali richiede l'approvazione dell'operatore.

## Validazione del codice

Sul sorgente dell'installer con SHA-256 `9e501a15c845f2c57b8c086359c05c057de4c9e3479a9f7fc63784bfae9e591f` passano 39 test mirati, inclusi pin dei descrittori e rollback, e la suite pulita basata su main completa 311 test con 13 esclusioni previste. Passano due fixture del benchmark e lo smoke MCP isolato. La matrice GitHub Windows/Linux con Python 3.10 e 3.14 passa; la review Codex del commit `34756b0` non contiene rilievi. Sonar supera il Quality Gate, con due suggerimenti di stile nei test e nessun rilievo sul runtime.

Credenziali di servizio per controller Linux, provider di servizio Windows, enrollment e integrazione Desktop rimangono incrementi separati nel [piano infrastruttura](piano-infrastruttura-multinodo.md).
