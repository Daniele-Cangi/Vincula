# Collaudo della verifica dei nodi

Data: 25 novembre 2026. Incremento basato sulla PR #23 (`de4e3c7`), con CLI `enrollment_check` e finestra **Verifica nodo** nelle due GUI. Il [contratto](node-enrollment.md) distingue la richiesta gestita dai singoli esiti: un nodo irraggiungibile non diventa disponibile perché la risposta JSON contiene `ok: true`.

Il nuovo controllo legge identità persistente, hash degli helper e policy attraverso una rotta SSH esplicita e autenticata. Non avvia job, cambia permessi o installa componenti. Worker e desktop rimangono rispettivamente `unverified` e `unchecked`. Le GUI operative non sono state aggiornate da questo collaudo.

## Prove automatiche

La snapshot pulita della PR #23 con i sette file di prodotto/test del nuovo incremento supera **385 test del runtime**, con 15 esclusioni di piattaforma, più due test del benchmark e lo smoke MCP isolato. DeprecationWarning e ResourceWarning sono trattati come errori per il runtime.

Sul PC Windows reale, Python 3.14.7 supera **48 test mirati**, con tre esclusioni: FIFO POSIX e due prove che richiedono la creazione di symlink, non disponibile all’account del collaudo. Il job `5851c289-f1ef-4f01-b765-de68b9fc84c8` termina con esito confermato e codice zero. Una prima esecuzione aveva individuato un errore nella fixture: `write_text` convertiva LF in CRLF mentre il digest atteso usava LF. La correzione scrive gli stessi byte UTF-8 con `write_bytes`, conservando tutte le asserzioni e il codice del prodotto.

La sorgente WinForms è compilata nativamente nel job `45b495b4-a458-4ec2-9b79-692fc8bf8044`, senza avviare l’app operativa. Il collaudo GTK usa proprietario nascosto e corpo della finestra renderizzato fuori schermo: verifica stati, dimensioni di testo e pulsanti, conflitti, risposta tardiva, callback dopo chiusura e stop indipendente durante verifica e trasferimento simulato. Un secondo caso usa davvero CLI e backend su un profilo locale sintetico, con file e policy invariati. Queste prove non equivalgono a uno stop durante un trasferimento reale o a un desktop condiviso sui tre PC.

Il primo harness WinForms non aveva un ciclo messaggi persistente: dopo `DoEvents` il secondo invio acquisiva un contesto generico, producendo timeout del callback e un errore di cleanup. La diagnostica conserva il primo errore. La correzione riguarda il solo harness, usa `Application.Run` e aggiunge verifiche di thread e contesto senza togliere le asserzioni esistenti. Il collaudo finale nativo `80af0769-e81e-4bdd-a61b-90348a2fd676` termina con esito confermato e codice zero: passa le stesse prove di stati, callback, conflitto, timeout, dimensioni e stop durante trasferimento simulato; cleanup senza errori. La sorgente e l’assembly del prodotto restano identici.

## Cataloghi reali

Da Ubuntu il nuovo CLI osserva l’identità locale corretta e autentica Windows, con UUID corrispondente, helper compatibili su disco e policy `full` con esecuzione generale abilitata. Dopo il primo timeout e il successivo recupero del collegamento, le stesse osservazioni riescono anche verso Kali. Il catalogo Core conserva lo stesso digest prima e dopo i tre controlli.

Da Windows il CLI osserva identità, helper e policy del nodo locale. La rotta Ubuntu del catalogo corrente è legacy: questo nuovo controllo la lascia esplicitamente `unchecked`, mentre le funzioni legacy esistenti conservano il loro percorso. La rotta Kali non è raggiungibile nel collaudo. Il catalogo Windows resta identico. Due primi tentativi del programma di collaudo invocavano il file CLI con `-I`, rimuovendo il suo percorso dagli import; sono fallimenti del programma di prova, corretti prima dell’esecuzione riportata, senza modificare il prodotto.

Kali è stato riavviato manualmente dall’operatore dopo autorizzazione esplicita. Il nuovo boot è `6681a602-552e-432d-b052-591899560938`. L’accesso da Ubuntu riprende e il job `3a251579-4955-4ad5-9121-9895a1fd7104` termina0 nel worker di sistema con UID1000. I tempi monotoni mostrano Core attivo a29.553s e SSH a36.497s, prima della sessione X11 a49.402s. È però presente un login grafico: questa prova non soddisfa il requisito rigoroso senza login.

Windows continua a osservare timeout: il job diagnostico `9b3cc542-b721-4860-ab11-683935b0764e` rileva OpenSSH exit255 con categoria `connect_timeout`, senza rifiuto delle chiavi. Anche il vero Codex Windows, job `2e57decf-9834-4fc5-b7e3-489e2388dcac`, fallisce l’oracolo: due chiamate MCP di lettura con errore di connessione, nessuna esecuzione sul destinatario. CLI e configurazione del client restano invariate. L’uscita0 della CLI non viene presentata come successo della prova MCP.

La diagnosi rileva su Kali l’assenza di `192.168.1.0/24` da tutte le tabelle di routing: Windows viene raggiunto tramite il router, mentre per Ubuntu esiste una cache `redirected`. L’indirizzo sorgente Windows105 e le regole SSH per Ubuntu117/Windows105 sono corretti. La rotta mancante è un difetto osservato; non è ancora dimostrato che sia l’unica causa dei timeout. È preparata una riapplicazione del solo profilo attivo `wlan0` sotto checkpoint NetworkManager, con autenticazione amministrativa locale e rollback automatico; nessuna modifica è già dichiarata applicata. Le due schede hanno profili attivi distinti, che non vanno confusi con un diverso profilo USB salvato.

Restano distinti: migrazione esplicita dei cataloghi Core/Desktop, autorizzazione iniziale della chiave dell’origine, servizio e runtime effettivamente in esecuzione, riavvio senza GUI e collaudo desktop multinodo. Gli hash e le ricevute applicabili sono raccolti nel [riepilogo JSON](collaudi/2026-11-25-node-check.json); i programmi di prova riproducibili sono in [tools/qa-node-check](../tools/qa-node-check/README.md).
