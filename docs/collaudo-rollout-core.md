# Rollout Core e cataloghi del 26 novembre 2026

La release `ed40e6fadf606c4a575abd0125ad95ec44848560`, già unita e revisionata nelle PR #21–24, è installata su Ubuntu, Windows e Kali. Il [resoconto strutturato](collaudi/2026-11-26-rollout-core.json) registra nove esecuzioni riuscite: ogni origine usa il proprio profilo MCP configurato e avvia un nuovo programma su ciascuno dei tre nodi. Ogni richiesta mantiene un UUID ripetibile; il log sintetico e il codice di uscita **7**, scelto intenzionalmente, coincidono con l'esito strutturato `reason: exit`, `started: true`.

| Origine | Ubuntu | Windows | Kali | Contesto verificato |
| --- | --- | --- | --- | --- |
| Ubuntu | Passato | Passato | Passato | Nuovo client MCP con il profilo installato |
| Windows | Passato | Passato | Passato | Task dell'utente `Interactive/Limited`, stesso profilo installato |
| Kali | Passato | Passato | Passato | Client MCP senza variabili grafiche e con socket SSH personale inesistente |

Il primo tentativo da Windows attraverso una sessione SSH entrante aveva raggiunto i nodi per leggere lo stato, ma la richiesta nativa verso Ubuntu era scaduta prima della creazione del lavoro. Il collaudo nella sessione utente normale ha poi completato le tre richieste con gli stessi UUID, senza modifiche al trasporto di prodotto. Questa prova non certifica il controller Windows headless né la risoluzione del timeout nel contesto SSH annidato.

## Distribuzione e cataloghi

I sorgenti provengono esclusivamente dall'albero Git indicato nel resoconto: 130 file tracciati, più il pin host pubblico esistente già verificato. Il compilato Windows proviene dal precedente collaudo nativo, con sorgente C# identico e digest/numero di byte confrontati prima della copia. Un gate con token Medium ha verificato lettura, import del server e scrittura negata del codice; nessun processo GUI è stato avviato da quel gate.

I puntatori MCP dei tre PC e le icone delle due app puntano alla nuova release. Su Ubuntu il launcher grafico usa per default lo stesso catalogo del Core, rispettando un eventuale `VINCULA_NODE_REGISTRY` esplicito. Su Windows MCP e app usavano già lo stesso catalogo locale; anche Kali mantiene il proprio catalogo comune. Le rotte restano specifiche dell'origine: i percorsi di una macchina non sono copiati sulle altre.

La CLI usata dalla GUI Ubuntu autentica Windows e Kali, confronta gli UUID, trova i sei helper compatibili su disco e osserva `full` con esecuzione generale abilitata. I risultati `worker: unverified` e `desktop: unchecked` restano corretti per questa azione: il funzionamento dei lavori è dimostrato dal collaudo MCP distinto. La rotta legacy Windows→Ubuntu resta operativa per MCP e funzioni esistenti; la nuova azione **Verifica nodo** richiede invece una rotta esplicita, come documentato nel catalogo GUI.

Le finestre operative già aperte sono rimaste in esecuzione per conservare la condivisione. La nuova GUI sarà caricata alla successiva apertura: questo collaudo non sostituisce la verifica visiva delle finestre aggiornate in produzione. Il primo cambio del collegamento Windows ha incontrato un errore nativo di accesso; il rollback ha preservato i puntatori. Una fixture su copie ha verificato la pubblicazione nello stesso volume con descriptor conservato prima dell'attivazione riuscita. Non sono stati cambiati i permessi per aggirare l'errore.

## Identità di servizio Kali

L'operatore ha autorizzato una sola installazione sudo locale del [controller Linux](controller-identity.md), usando il medesimo sorgente verificato di Ubuntu. Sono stati controllati metadati della credenziale systemd, copia del sorgente in directory root privata, unità abilitata e attiva, socket dell'account e firma della chiave pubblica.

La sola chiave pubblica di questo provider è stata aggiunta ai file autorizzati esistenti di Ubuntu e Windows. Le preimmagini sono state confrontate, salvate in backup e conservate come prefisso dei file; owner, ACL e autorizzazioni precedenti sono rimasti invariati. Nessuna chiave privata è stata letta da Python, copiata fra macchine o inserita nel resoconto.

Le due rotte in uscita di Kali usano ora `/etc/vincula/controller/1000/identity.pub` e `/run/vincula-identity-1000/agent.sock`. Il suo profilo MCP avvia direttamente il server e non passa più dal bootstrap dell'agente SSH grafico. Le connessioni preliminari e il collaudo MCP finale riescono con socket personale volutamente inesistente; nel client MCP sono state rimosse anche `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY` e `DBUS_SESSION_BUS_ADDRESS`. Non è stato necessario aprire popup o sbloccare le chiavi personali.

Non è stato eseguito un nuovo riavvio né un logout di Kali. L'abilitazione dell'unità e questo test senza ambiente grafico non sostituiscono una futura prova di boot del nuovo ruolo controller. Rimane valida, con il proprio ambito, la precedente [prova dopo boot del Core ricevente](collaudo-core-headless.md).

## Conservazione e lavoro rimanente

I sei helper erano già identici alla release distribuita: non è servita una migrazione del database o un riavvio dei worker. Il task worker Windows, trovato fermo, è stato avviato; alla fine non restano lavori di prova attivi. Le policy `full` e l'opt-in nativo sono invariati. I processi dell'app Windows e di Deskflow conservano gli stessi PID. Nessun riavvio di PC è stato eseguito.

Il codice degli inviti e della verifica dei nodi è ora disponibile nelle release installate. Restano distinti l'autorizzazione iniziale automatizzata di un nuovo computer, il provider Windows headless, la migrazione delle rotte legacy necessarie alla nuova verifica e il collaudo visivo delle GUI riaperte. Il desktop multinodo richiede ancora provisioning dei trasporti, fiducia TLS, consensi e integrazione del motore nelle app; questi risultati MCP non lo attivano.
