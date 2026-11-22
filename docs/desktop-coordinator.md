# Coordinatore delle sessioni desktop

`agent/desktop_coordinator.py` coordina una singola sessione sopra il catalogo/layout e il supervisor locale. Questo incremento non installa o attiva Deskflow, non modifica firewall, certificati, GUI, configurazioni MCP o livelli `ask`/`full`. L’ammissione dei nodi e il provisioning restano nel [piano di enrollment](node-enrollment.md).

## Piano e trasporto

`build_plan(registry, revisions, commands, session_id=None)` compila il gruppo desktop connesso e congela un piano per ciascun UUID. Ogni nodo riceve lo stesso UUID di sessione fresco, principale e membri; `local_id`, revisione del proprio registro e componenti autorizzati sono specifici del destinatario. Le revisioni possono differire: i registri locali contengono route e identità locali differenti. Non riutilizzare UUID di sessione o tentare di riprendere uno storico.

Il coordinatore verifica sintassi e limiti JSON, senza interrogare dal computer d’origine i percorsi degli eseguibili remoti. Esistenza, accesso, proprietà, permessi e consenso devono essere verificati sul destinatario. Un hash di revisione non costituisce approvazione. I comandi devono essere già autorizzati; questo modulo non scopre o inventa comandi desktop.

`DesktopCoordinator(registry, revisions, commands, transport, ...)` riceve un callable **asincrono** `await transport(node_id, request)`. Il trasporto associa UUID a un servizio autenticato già ammesso; non accetta indirizzi o credenziali dal piano e gestisce privatamente i token locali. Deve mantenere distinti controllo e traffico dati, avere I/O cancellabile/limitato e non bloccare l’event loop con un trasferimento.

| Richiesta JSON | Risposta |
|---|---|
| `{"action":"prepare","plan":...}` | `{"ok":true,"session":...}` dopo piano accettato |
| `{"action":"commit","session_id":"..."}` | Stato `starting` o `running` della stessa sessione |
| `{"action":"heartbeat","session_id":"..."}` | Stato attivo e lease rinnovata della stessa sessione |
| `{"action":"status"}` | Stato corrente, verificato contro la sessione attesa |
| `{"action":"stop_session","session_id":"..."}` | Arresto atomico della sola sessione indicata |

Gli errori usano `{"ok":false,"error":"..."}`; un errore di trasporto o timeout lascia gli effetti **non confermati**, non dimostra che il comando non sia stato eseguito. Non sono previsti retry automatici di `prepare` o `commit`.

Ogni ack deve attestare `session_id`, `instance`, `revision`, `local_id`, `primary`, `members`, `state`, `desktop_ready` e, per gli stati attivi, `lease_remaining`. Il cambio di istanza o identità viene rifiutato; non vengono adottati processi o sessioni di un nuovo supervisor. Le risposte attive tardive non possono resuscitare una sessione già osservata terminale.

## Barriere, readiness e arresto

`await start()` invia `prepare` a tutti i membri. Solo dopo **tutti** gli ack validi parte il fan-out `commit`; dopo tutti i commit serve anche uno stato fresco dei destinatari. `ready:true` richiede contemporaneamente tutti i commit confermati, stato `running`, `desktop_ready:true` effettivo su ogni nodo e lease ancora valida. PID o semplice `running` non bastano. La perdita di readiness dopo una sessione pronta provoca rollback e richiede una nuova sessione esplicita.

Ogni errore operativo avvia tentativi di `stop_session` su **tutti** i nodi, inclusi quelli con ack perso o risultati incerti. Un nodo irraggiungibile non interrompe gli altri tentativi. Il rapporto conserva errori per nodo e separa gli errori della fase dagli errori di cleanup. `failed` non significa che ogni arresto sia confermato: controllare `cleanup_complete`. Uno stop che risponde `stopping` richiede successivi `status` per confermare il termine; un peer offline rimane non confermato finché non è osservabile.

`stop()` può essere ripetuto esplicitamente per riprovare il cleanup di peer tornati raggiungibili; mantiene sempre lo stesso UUID e non prepara o riavvia programmi. Un piano preparato dopo un ack tardivo, senza alcun commit, resta comunque soggetto alla propria lease.

`stop()` è indipendente dalle RPC di avvio o heartbeat bloccate. Non usa mai lo stop globale o `shutdown`: un arresto di un’origine precedente non deve fermare un gruppo creato nel frattempo da un’altra origine. Le risposte tardive non riabilitano una sessione fermata. `report()` legge soltanto la cache locale e ricontrolla per ogni peer commit, stato `running`, readiness e deadline rispetto al clock corrente. Se una lease nota è scaduta o un aggiornamento parziale ha già osservato un peer non pronto, restituisce `waiting_ready` e `ready:false` anche quando l’ultima fase completata era pronta. Non apre connessioni, non rinnova lease e non esegue cleanup: sono le operazioni asincrone a rilevare l’errore e avviare l’arresto.

Per ciascun UUID un lock distinto serializza l’intero contatto `prepare`/`commit`/`status`/`heartbeat`: RPC, validazione dell’ack e aggiornamento della cache. Non basta ordinare gli invii o numerare le richieste, perché il solo numero locale non dimostra l’ordine di esecuzione remoto. Uno stato vecchio non può quindi arrivare dopo una risposta più recente dello stesso peer e sostituirla nella cache. Gli altri peer proseguono in parallelo. `stop_session` evita sempre questi lock, anche con una RPC pendente e un heartbeat in coda; la prova terminale resta conservata quando arrivano ack attivi tardivi. Non vengono inviate nuove operazioni mutanti accodate dopo stop o fallimento.

## Lease e perdita dell’origine

Non esiste un task interno che rinnova automaticamente le lease. L’origine deve chiamare `heartbeat()` da un task separato dall’avvio e dai trasferimenti, interrompendolo quando termina il proprio controllo. Durante `prepare` rinnova soltanto i destinatari di cui è già arrivato un ack valido. Dopo lo stop o un errore non può rinnovare o riprendere la sessione.

Il timeout è per singolo contatto e comprende attesa del lock del peer più RPC (fan-out parallelo). Un contatto scaduto in coda non viene inviato; un timeout dopo l’invio lascia invece gli effetti incerti. Il trasporto deve rispettare la cancellazione. La verifica conservativa della lease usa l’inizio effettivo della RPC e la durata residua restituita: attesa in coda e ritardo della risposta non estendono la lease.

La coda di un peer può ritardarne l’heartbeat, anche mentre quelli degli altri peer procedono: non esiste una priorità implicita del rinnovo. Intervallo heartbeat, contatti già accodati, tempo di rete/elaborazione e margine di scheduling devono rientrare nelle lease dei destinatari. Evitare poller concorrenti senza limite; un peer bloccato oltre questo budget deve fallire e scadere, non essere dichiarato pronto dalla cache. Anche il budget di avvio deve rispettare questi limiti. Se l’origine muore o non rinnova più, sono i monitor autonomi dei supervisor a far scadere le sessioni; non viene tentato un resume automatico. La cancellazione esplicita di `start()` tenta il rollback prima di propagare la cancellazione, mentre una morte improvvisa resta coperta dalle lease.

## Arresto scoped sul canale IPC locale

L’endpoint locale `stop_session` accetta il campo esatto `session_id` con UUID canonico, dopo autenticazione con il token privato. Il `Handler` chiama direttamente `Supervisor.stop(session_id)`: identità e arresto sono verificati sotto lo stesso lock. Non è una traduzione in `status` seguito da `stop()` globale, tra i quali potrebbe partire una sessione di un’altra origine. Una sessione sconosciuta o diversa produce errore senza effetti sul nuovo gruppo. Ogni futuro trasporto remoto deve preservare questa operazione atomica, senza fallback a uno stop globale.

I test d’integrazione attraversano tre `Server`/`Handler` reali su porte loopback distinte, ciascuno in un namespace TEMP privato. Il trasporto invoca `desktop_supervisor.request` tramite `asyncio.to_thread`: è il client locale standard a leggere il token privato del destinatario; il coordinatore non lo riceve, stampa o conserva. Le sue operazioni non bypassano il protocollo chiamando direttamente `Supervisor.stop`. Token errati vengono rifiutati su tutti e tre gli endpoint.

Questo verifica il canale IPC locale autenticato. Non implementa ancora un trasporto SSH tra computer, distribuzione di credenziali, adapter Deskflow o attivazione delle GUI. Le API Direct e il registro non vengono modificati da queste prove.

## Evidenza e limiti

I test locali avviano tre supervisor e i relativi endpoint IPC reali in directory TEMP private, con soli programmi sintetici. Il loro `desktop_ready` è sempre `false`: il coordinatore resta correttamente non pronto anche quando tutti i processi sono `running`. La cessazione degli heartbeat fa scadere i tre supervisor; una richiesta di stop con vecchio UUID, inviata sul protocollo reale, non arresta una nuova sessione preparata su un destinatario.

Una prova perde deliberatamente l’ack nella scrittura della risposta a un commit già accettato dal vero `Handler`: il coordinatore segnala l’errore e arresta i tre endpoint con richieste scoped. Un’altra chiude il listener IPC di un nodo: i peer raggiungibili ricevono il cleanup, mentre il supervisor rimasto isolato scade autonomamente. Il rapporto non dichiara confermato l’arresto del peer irraggiungibile; la sua scadenza viene osservata soltanto dalla fixture locale. I test arrestano server e processi sintetici, attendono i thread e verificano la rimozione del namespace TEMP.

Le fixture con `desktop_ready:true` provano soltanto il contratto del futuro backend, non un desktop funzionante. Fake clock e trasporto verificano barriera prepare/commit, ack persi, timeout e attesa in coda, errori parziali, identità divergenti, cancellazione, cache scaduta o parzialmente aggiornata, serializzazione degli ack, heartbeat degli altri peer e stop concorrente che evita i lock. Prima dell’attivazione reale mancano ancora trasporto SSH autenticato scoped, adapter Deskflow, enrollment, TLS e consenso grafico effettivo. Nessuna di queste prove certifica input, schermo o clipboard multinodo in produzione.
