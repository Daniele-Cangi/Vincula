# Piano Vincula per nodi agenti e desktop

L’evoluzione di Vincula usa su ciascun host lo stesso catalogo locale per agenti, lavori distribuiti e desktop condiviso. L’aggiunta di un computer deve registrare la sua identità, verificare le connessioni e abilitare le funzioni scelte dall’operatore. Gli indirizzi di rete e il sistema operativo sono proprietà del nodo. L’UUID identifica il computer in tutti i componenti.

Il piano parte da `main` dopo la PR #8, commit `b470517`. I collaudi su ASUS Ubuntu, Lenovo Windows e Kali X260 hanno verificato inventario, lavori, trasferimenti e perdita della rete di Kali. La verifica del crash del worker richiede una prova distinta. Le valutazioni del testo allegato vengono confrontate con riproduzioni e misure prima di scegliere la soluzione.

## Stato corrente: 28 novembre 2026

Le sezioni cronologiche successive conservano gli esiti delle singole revisioni;
questa tabella distingue ciò che è disponibile dai requisiti ancora aperti.
La base di questo incremento Core è `main` `be3989356c23`.

| Componente | Stato verificato | Lavoro ancora necessario |
| --- | --- | --- |
| MCP multinodo ed esecuzione generale | Sessioni dalle tre origini; opt-in nativo e `ask`/`full`; nove lavori reali del rollout con log ed exit code | Conservare i collaudi a ogni rollout che modifica il runtime |
| Core senza desktop | Ricevente Kali qualificato dopo boot e sblocco disco, senza login grafico; controller Linux con identità di servizio | Controller Windows indipendente dal login; qualificazione post-boot distinta per ciascun ruolo controller |
| Aggiungi nodo | Catalogo comune, inviti con UUID e confronto impronta, GUI/CLI e verifica autenticata degli helper | Autorizzazione iniziale, installazione e verifica del worker effettivamente attivo; il solo invito resta `registered`; rotta legacy Windows→Ubuntu da integrare nella verifica |
| Desktop su tre PC | [PR #26](https://github.com/Daniele-Cangi/Vincula/pull/26), ancora in bozza: una sola app Windows coordina Ubuntu—Windows—Kali; input e sei direzioni appunti confermati dall’operatore | Revisione e merge separati; i quattro alert Sonar sono triagiati e conservati come limite, non eliminati |
| Stop del desktop | Nella PR #26 stop durante SFTP, perdita del collegamento desktop Kali e revoca reale del consenso GNOME; il Core continua a eseguire lavori | Mantenere distinzione fra test manuali, osservazioni automatiche e revisioni installate |
| Crash worker e recupero piani | Prove isolate Linux con discendenti; recupero batch/capability da altre origini; CI Windows/Linux | Contenimento forte se viene ucciso anche il guardian; i test isolati non equivalgono a SIGKILL del servizio produttivo |
| Benchmark | Dodici casi, diciotto lavori exit 0, digest uguali; totale mediano distribuito 24,637 s contro locale 20,491 s | Ripetere le misure quando cambiano scheduling e protocollo dei trasferimenti |
| Scheduler e concorrenza | [PR #27](https://github.com/Daniele-Cangi/Vincula/pull/27) in bozza; [suite native e piano su tre receiver reali temporanei](collaudo-scheduler-risorse.md) qualificati, CI/review dell’head `85f559a` passate | Rollout e benchmark aggiornato; quote cooperative, senza limiti OS CPU/RAM/GPU; un timeout intermittente osservato resta dichiarato |
| Dataset grandi | [Primo incremento protocollo 2](large-file-protocol.md), disabilitato per impostazione predefinita: 137 controlli nativi per PC e [sei direzioni SSH reali](collaudo-file-grandi.md) con file oltre 64 MiB, hash, exit code e cleanup corretti; CI/review `e52914e` passate | Review dell’head documentale finale, crash reale del coordinatore e ripresa da altra origine fisica; controller automatico e relay con quote persistenti restano nel [piano completo](large-file-transfers.md). Nessun rollout; i tool legacy conservano 64 MiB |

Il test GUI con file da 1 GiB non modifica il protocollo MCP dei file. Il recupero
di un piano da un’altra origine non implica l’esecuzione automatica degli effetti
ancora mancanti: i piani batch/capability esistenti restano di lettura. Il nuovo
tipo nativo introduce separatamente un replay esplicito con autorità fissa,
stessi UUID e controlli della cronologia sul destinatario. Nuovi riavvii di Kali
richiedono accordo con l’operatore; nessun riavvio è necessario ai test isolati.

## Core e Desktop separati

Vincula Core comprende MCP locale, worker sui destinatari, connessioni autenticate e policy locali. Un destinatario che riceve lavori non richiede chiavi SSH in uscita, Codex aperto o una sessione grafica. Le credenziali del controller sono gestite dal runtime e protette dal sistema operativo; l’agente indica operazioni e nodi. In `full`, con esecuzione generale abilitata dal proprietario, può utilizzare argv, interpreti e programmi compatibili entro i permessi dell’account, senza preregistrare ogni comando.

Vincula Desktop aggiunge facoltativamente sessione grafica, condivisione di input e appunti e relativi consensi. La sua disponibilità non determina quella dei lavori Core. Il [servizio ricevente Linux](core-service.md) e il [controller Linux dedicato](controller-identity.md) sono uniti su `main` nelle PR #21 e #22. Il [collaudo headless](collaudo-core-headless.md) verifica un nuovo lavoro del vero Codex Windows su Kali dopo riavvio e sblocco locale del disco, senza login grafico dell’utente, con log ed exit code zero. Ubuntu recupera lo stesso lavoro dopo la chiusura del client. Il [rollout del 26 novembre](collaudo-rollout-core.md) aggiunge il controller Kali e verifica nove esecuzioni dalle tre origini con la release aggiornata, senza agente SSH personale sul client Kali. Il provider Windows headless e l’automazione completa dell’enrollment restano incrementi distinti; queste prove non certificano un boot cifrato completamente automatico.

## Sequenza e risultati verificabili

| Fase | Risultato | Criterio di completamento |
| --- | --- | --- |
| 1 | Sessioni MCP aggiornate | Configurazione dei client sui tre PC, avvio del runtime verificato e uso dei tre UUID da ciascuna origine. Registrare percorso, versione installata e risultati delle chiamate. |
| 2 | Aggiunta di un nodo nel catalogo comune | CLI JSON e due interfacce grafiche usano la stessa validazione e la stessa scrittura atomica. Aggiungere e aggiornare nodi senza cambiare il codice, mantenendo gli alias e le installazioni precedenti. |
| 2b | Esecuzione generale via MCP | Argv, directory, ambiente e input iniziale immutabili, con approvazione della richiesta esatta oppure avvio preautorizzato. Utilizzare nuovi software senza preregistrare ogni invocazione. Distinguere esecuzione non interattiva e terminale PTY. |
| 2c | Core disponibile senza desktop | Worker ricevente avviato dal sistema, autenticazione gestita dal runtime e dall'OS. Dopo il riavvio di Kali senza login grafico, Codex sul Lenovo esegue via MCP un nuovo argv autorizzato e legge log ed exit code. |
| 3 | Mouse tastiera e appunti su più computer | Un principale esplicito, membri identificati per UUID e layout degli schermi. Configurazione Deskflow generata per il gruppo, trasporti SSH e TLS verificati. Collaudo Windows principale con ASUS e Kali, anche durante un trasferimento. |
| 4 | Recupero dopo crash Linux | SIGKILL del worker durante un programma con figli. Arresto dei processi controllati, riavvio del worker, esito riconciliabile e ripetizione dello stesso UUID senza nuovo avvio. |
| 5 | Recupero dei piani da un’altra origine | Manifest durabile con UUID di piano e lavori, recuperabile su un altro computer quando l’origine perde disponibilità. Risultati recuperati e nuove chiamate idempotenti. Coprire lotti e workflow di capacità. |
| 6 | CI funzionale Windows e Linux | Matrice Python minimo supportato e versione dei PC. Suite con deprecazioni fatali e smoke MCP isolato, eseguiti su ogni PR. |
| 7 | Prestazioni misurate sui tre PC | Stesso risultato su singolo nodo e lavoro ripartito. Registrare tempi di preparazione, copie, esecuzione, attesa e ricomposizione, ripetizioni e dispersione. |
| 8 | Scheduler e concorrenza basati sulle risorse | Requisiti del lavoro e capacità del nodo, limiti locali di CPU RAM GPU e numero di lavori. Verificare rispetto delle quote, approvazioni e comportamento quando un nodo diventa indisponibile. |
| 9 | Trasferimenti per dataset grandi | Budget di disco e rete, avanzamento, cancellazione e ripresa. Superare i limiti correnti mediante un protocollo verificabile, con gestione di file parziali e spazio insufficiente. |

Le fasi 2 e 3 condividono schema e interfacce. Il recupero del runtime e la CI possono procedere in parallelo alla progettazione del desktop. Le misure della fase 7 guidano i valori iniziali dello scheduler e le priorità dei trasferimenti.

## Aggiungere un nodo

Il flusso operativo parte da «Aggiungi nodo» nelle app oppure dal comando locale di configurazione per installazioni headless. L’operatore inserisce o importa UUID, nome, famiglia del sistema e coordinate di rete. Il runtime locale verifica i campi e mostra l’identità SSH da confermare mediante un dato ottenuto sul nuovo computer. Le chiavi private rimangono sulla loro origine.

La connessione verificata permette di rilevare sistema, risorse, interprete, programmi e funzioni disponibili. L’operatore sceglie l’accesso agente e le funzioni desktop da abilitare. I prerequisiti mancanti hanno uno stato esplicito e un’azione per completarli. L’interfaccia pubblica lo stato del nodo solo dopo la verifica del relativo servizio.

Il registro mantiene gli UUID e gli alias già usati. Un blocco desktop opzionale riferisce gli stessi UUID e contiene il principale, i membri, la posizione degli schermi e la scelta sugli appunti. Il salvataggio usa lock, revisione attesa e sostituzione atomica. La rimozione di un nodo richiede la risoluzione dei suoi riferimenti nel layout e nei servizi attivi.

Il catalogo condiviso è locale al singolo host. L’aggiunta da Windows non replica automaticamente identità e percorsi SSH negli altri computer. L’enrollment dovrà distribuire i riferimenti pubblici verificati e configurare le rotte di ogni origine autorizzata. Le chiavi private rimangono su chi le usa; il file del registro non va copiato tra host con percorsi locali differenti.

Il [contratto di enrollment](node-enrollment.md) definisce identità, invito pubblico, verifica delle rotte e abilitazione separata delle funzioni.

Le due GUI chiamano un runtime locale Python con richieste e risposte JSON. La GUI Ubuntu può usarne direttamente le funzioni. La GUI Windows usa un processo locale senza comporre comandi shell dai dati dell’utente. Il runtime riusa il registro, la validazione e i trasporti esistenti.

## Esecuzione generale

La shell MCP attuale conserva una cronologia di programmi registrati con argomenti fissi. La scoperta del software non ne autorizza automaticamente l’esecuzione. Un adapter generico installato una volta ha già dimostrato comandi parametrizzati differenti tramite le capability, in `ask` e `full`, senza nuove registrazioni per variante. Rimangono il bootstrap del provider e i limiti dei suoi parametri e risultati.

L’API diretta espone `execute_process` e `read_process` con richiesta immutabile e digest. L’operatore abilita la nuova funzione sul nodo; le installazioni esistenti la mantengono disabilitata, così l’aggiornamento non amplia il precedente consenso a `full`. In `ask` la preview mostra esattamente la richiesta approvata; in `full` l’avvio procede con i permessi dell’account. Non introduce elevazione automatica o un isolamento del filesystem del processo. I grant dei tool file conservano la loro funzione nei workflow strutturati.

La prima versione usa input iniziale seguito da EOF e non dichiara un terminale interattivo. PTY/ConPTY e controllo GUI richiedono backend propri. Il requisito di esecuzione generale precede lo scheduler delle risorse, perché l’agente deve poter preparare invocazioni compatibili sui computer scelti.

## Desktop condiviso

Deskflow fornisce un server sul principale e un client su ciascun altro nodo. Le configurazioni correnti delle app generano una sola coppia `dock-windows` e `dock-ubuntu`. Il generatore nuovo usa nomi derivati dagli UUID e l’intero layout del gruppo. Il principale viene scelto esplicitamente e può essere Windows, Ubuntu o un altro nodo compatibile.

Il controllo resta su SSH; il trasporto desktop viene verificato separatamente. L’inventario della triade propone TLS reciproco diretto, con regole firewall limitate a programma, porta, indirizzi e interfacce autorizzati. Gli inoltri SSH restano un’alternativa da configurare esplicitamente: le chiavi ristrette di Kali non consentono attualmente il forwarding. L’enrollment deve distinguere la connessione MCP dalla disponibilità desktop, verificare le impronte TLS e impedire di dichiarare pronto un collegamento non collaudato. Il [piano di provisioning](desktop-provisioning.md) raccoglie prerequisiti, scelte e prove ancora da eseguire.

Su Wayland l’avvio e le autorizzazioni dipendono dai portali e dalla sessione grafica. Il collaudo deve verificare il consenso sul desktop, lo stop locale da ciascun computer, la disconnessione del principale, il ritorno di mouse e tastiera alla postazione e gli appunti nelle due direzioni. La scelta `ask` o `full` per i lavori agente e il consenso desktop hanno controlli distinti.

## Affidabilità dei lavori e dei piani

La riproduzione isolata ha confermato che SIGKILL di un worker Linux avviato direttamente può lasciare vivi il programma e un figlio. Il servizio systemd contiene già i processi nel proprio cgroup. Il percorso CLI richiede una supervisione equivalente per i gruppi di processi che avvia.

La correzione conserva `unconfirmed` dopo un’interruzione senza ricevuta finale. Il nuovo worker deve attendere la pulizia prima di acquisire il lavoro successivo. I test coprono avvio fallito, uscita normale, stop, crash, riavvio concorrente e ripetizione dello stesso UUID. Il supervisore adotta e pulisce anche discendenti che cambiano sessione. Il contenimento mediante cgroup resta necessario per la garanzia forte se il supervisore stesso riceve SIGKILL, anche quando il worker rimane vivo, o quando i discendenti assumono privilegi diversi.

Per i piani serve un manifest durabile con nodi assegnati, UUID dei lavori e trasferimenti, revisione del formato e digest. La replica deve precedere gli effetti che renderebbe irrecuperabili. Un’origine diversa deve poter ritrovare gli identificativi e leggere gli esiti dai worker. L’adozione di un piano preserva le approvazioni sul destinatario e verifica la compatibilità con piani precedenti e ripetizioni concorrenti.

## Misure e pianificazione delle risorse

Il benchmark distingue tempo totale percepito e tempo di calcolo. Usa un carico CPU suddivisibile e dati sintetici riproducibili, verifica un digest del risultato e confronta il singolo nodo più rapido con la ripartizione sui tre computer. Rileva l’effetto del Wi-Fi, del relay e delle differenze di prestazioni tra i PC. Un carico GPU va aggiunto quando lo stesso algoritmo è effettivamente disponibile sui nodi confrontati.

Lo scheduler userà requisiti dichiarati dal lavoro e capacità rilevate. Carico istantaneo, memoria libera e disponibilità GPU informano l’assegnazione, mentre quote locali e prenotazioni limitano l’esecuzione concorrente. I lavori già approvati mantengono la propria identità durante i cambi di coordinatore.

I trasferimenti legacy hanno un limite di 64 MiB e il relay usa disco temporaneo sull’origine. Il primo incremento per dataset grandi aggiunge quote agli endpoint, spazio libero, checkpoint e verifica finale. Non include ancora un controller durevole o quote persistenti per il relay. La scelta fra relay e collegamento diretto dipenderà dalle rotte autorizzate e dalle misure, con verifiche di integrità per entrambe le modalità.

Il [piano dei dataset grandi](large-file-transfers.md) separa manifest, prenotazioni, copie fuori dalle transazioni, ripresa verificata e pubblicazione finale.

## Stato di avanzamento

Il piano è stato avviato il 22 novembre 2026. Il primo collaudo della fase 1 è stato completato il 22 novembre sulla release `6ac4605`: tre sessioni Codex native hanno eseguito **39 letture MCP riuscite**, tredici da ciascuna origine, con tutti i nodi disponibili e una sola identità locale corretta. Gli audit raccolti dopo tutte le chiamate confermano sei helper corretti, gli stessi worker aggiornati, dati e grant preservati, `ask` e opt-in nativo disabilitato. Il [collaudo delle sessioni](collaudo-sessioni-agenti.md) e il [riepilogo pubblico con hash dei JSONL](collaudi/2026-11-22-mcp-triade.json) distinguono questa prova dalla precedente revisione `b470517`. Codex Windows era stato aggiornato da 0.161 a 0.171. La revisione installata è verificata mediante avvio, hash e ricevute dei processi; il solo stato hardware non la certifica.

Il [benchmark sui tre PC](benchmark-cpu-tre-nodi.md) è completato con dodici casi validi. Il totale mediano distribuito a freddo è 24,637 secondi, contro 20,491 secondi del seriale locale. Il risultato impone di includere località dei dati e costo del coordinamento nella scelta dei nodi. Le modalità `ask` sono conservate e le fixture sono state rimosse.

Le PR #10, #11, #12, #13 e #14 erano già unite nella revisione `6ac4605`. Comprendono CI, stato Windows corretto, catalogo JSON e GUI, supervisione Linux, recupero batch/capability, supervisore desktop autonomo ed esecuzione nativa MCP. Le matrici hosted Windows/Linux con Python 3.10 e 3.14 passano, così come i controlli Sonar. La snapshot di quel collaudo del runtime contava 165 test, con esclusioni specifiche della piattaforma, più due test del benchmark. Il [collaudo dei tre worker isolati](collaudo-recupero-triade.md) verifica anche trasferimenti reali e recupero da Windows e Kali senza nuovi effetti.

La PR #12 contiene il pannello Nodi e l’anteprima del layout nelle due GUI. Il conflitto fra lettura e sostituzione atomica del registro Windows è corretto e verificato con cento round concorrenti e con i token SSH elevato e desktop non elevato. La PR #13 contiene il supervisore desktop autonomo, con prove su Windows e Linux. I rilievi automatici sui percorsi sono risolti mediante pipe private e output fissi; la review indipendente ha verificato anche lo stop durante la preparazione. Il componente non avvia ancora Deskflow e non è integrato nelle app.

La PR #14 aggiunge l’esecuzione generale via MCP. Le richieste grandi hanno rivelato un blocco del ponte stdin PowerShell; il percorso Python nativo passa tre ripetizioni SSH reali per richieste di processo da 180 KiB e journal da 189 KiB, senza cambiare la configurazione SSH. La verifica riguarda Windows OpenSSH con shell predefinita `cmd.exe`; un wrapper PowerShell provato separatamente non supera lo stesso collaudo e non è dichiarato compatibile. La review è conclusa senza rilievi aperti, comprese le ricevute di errore Unicode limitate in byte.

Il [coordinatore desktop](desktop-coordinator.md) e il collegamento al suo IPC autenticato sono uniti su `main` nelle PR #15 e #16. Prepara tutti i nodi prima dei commit, controlla identità e lease, ordina le RPC per nodo e mantiene lo stop circoscritto indipendente. Ventidue test del coordinatore, compresi tre endpoint TCP locali reali, verificano anche token errato, ack perso e nodo offline. I supervisor reali restano `desktop_ready:false`: il trasporto SSH e il backend Deskflow devono ancora essere collegati. Le fixture pronte verificano il contratto, senza certificare un desktop operativo. I cinque rilievi Sonar di manutenzione della PR #15 restano follow-up espliciti; la sostituzione suggerita di `wait_for` richiede di conservare Python 3.10 e il budget totale di attesa e RPC. Il rilievo sulla complessità del Handler della PR #16 è risolto; la sua matrice Windows/Linux e i controlli Sonar passano senza nuovi issue.

La PR #17 contiene il [ponte SSH desktop](https://github.com/Daniele-Cangi/Vincula/pull/17), con ventiquattro test mirati, review indipendente e sei check verdi. La prova Windows reale verifica lo stop e lo stato terminale mentre l’editor tiene il catalogo bloccato, poi la pubblicazione riuscita. Lo stop richiede l’istanza già attestata e non apre il catalogo mutabile. La PR resta in bozza per il collaudo SSH sui tre PC, incluso lo stop esplicito da Windows durante SFTP e una RPC ancora pendente. Le prove sintetiche e la sola presenza del motore non autorizzano `desktop_ready:true`.

Nell’aggiornamento del 22 novembre il runtime operativo e i sei helper sono stati aggiornati sui tre PC alla revisione `6ac4605`, dopo il ritiro dei vecchi processi MCP. Sono stati conservati backup SQLite e configurazioni, controllati i dati preesistenti e riavviati gli stessi worker. Policy `ask` e abilitazione nativa disattivata sono rimaste invariate. Il collaudo nativo è concluso su tutte le origini. Windows è stato verificato anche nel token Medium; su Kali l’inoltro esplicito di tre nomi d’ambiente risolve il bootstrap dell’agente SSH e il daemon esistente espone 23 strumenti senza un ulteriore riavvio.

La PR #19, unita su main, aggiunge i [contratti del backend e il compiler dei profili Deskflow](desktop-backend.md). Sono anteprime in memoria e controlli preliminari: `launchable` e `desktop_ready` restano sempre false. Non includono ancora engine gestito, attestazione dei peer e dei consensi o integrazione nelle GUI.

La PR #20, unita su main `f304b91` e installata sui tre PC, aggiunge il [bootstrap locale delle credenziali SSH](ssh-credentials-bootstrap.md) per le sessioni utente che lo utilizzano. Questo componente rimane separato dal Core headless. Le PR #21 e #22 sono unite su `main` `f463b360`: il ricevente Kali usa il worker di sistema e il controller Ubuntu l’identità dedicata protetta dall’OS. Il primo [collaudo Core](collaudo-core-ricevente.md) conserva le prove iniziali; il successivo [collaudo headless](collaudo-core-headless.md) supera il criterio senza login grafico dopo riavvio autorizzato e sblocco locale del disco. Il programma riporta il cgroup del worker di sistema, mentre le osservazioni OS distinguono il greeter dal login dell’utente. Nuovi riavvii di Kali richiedono l’approvazione dell’operatore.

Le icone Ubuntu e Windows puntano alla release installata, indipendente dal checkout di sviluppo. Prima dell’attivazione Windows è stato corretto e verificato l’accesso in lettura e avvio per l’utente non elevato, conservando il codice non scrivibile. Le finestre già aperte non sono state terminate; l’attivazione delle nuove GUI al successivo avvio e il loro collaudo restano verifiche separate.

Il primo incremento del [flusso «Aggiungi nodo»](node-enrollment.md) implementa export, preview e import di inviti pubblici nelle due GUI e nella CLI locale. L’UUID proviene dal nuovo computer, l’impronta richiede confronto esplicito e le rotte restano proprie dell’origine; il salvataggio è atomico e ripetibile. Il [collaudo degli inviti](collaudo-inviti-nodi.md) copre un quarto UUID sintetico da due origini, Windows nativo e le finestre GTK/WinForms. Non è stato aggiunto un quarto computer reale: il risultato resta `registered`, con SSH, runtime e desktop da verificare.

Il [controller Core Ubuntu](collaudo-core-controller-ubuntu.md) è installato e l’app Codex ricaricata usa il profilo verificato verso tutti e tre i nodi. I destinatari sono ora `full` con esecuzione generale abilitata per scelta esplicita dell’operatore. Il flusso degli inviti conserva i due livelli e non cambia questi permessi. Il collaudo Kali dopo boot senza login grafico è superato per il ruolo ricevente, dopo sblocco locale del disco; il provider controller Kali e quello Windows rimangono da completare e collaudare.

Il successivo incremento **Verifica nodo** aggiunge osservazioni autenticate di SSH, identità persistente, helper su disco e policy, mediante CLI comune e finestre modeless nelle due app. Il [collaudo](collaudo-verifica-nodi.md) distingue il collegamento reale Ubuntu→Windows, i profili locali e le prove GUI isolate; worker e desktop restano non verificati. Il resoconto conserva il precedente tentativo post-boot fallito e il tentativo di checkpoint senza esito certificato. Con il solo wlan0 e la rotta di subnet ripristinata, i successivi controlli autenticati da Ubuntu e Windows passano; dopo il riavvio autorizzato è superato anche il [collaudo Core senza login grafico](collaudo-core-headless.md), dopo sblocco locale del disco. Questa prova separata del worker non cambia gli esiti conservativi della nuova azione di verifica.

Autorizzazione iniziale delle chiavi pubbliche, verifica del runtime effettivamente in esecuzione, migrazione dei cataloghi Core/Desktop, provisioning e attivazione del desktop multinodo con input e appunti sui tre PC restano da completare. Lo scheduler e i trasferimenti per dataset grandi sono incrementi successivi. Ogni incremento viene verificato con test mirati, review e collaudi applicabili prima della pubblicazione.

## Aggiornamento operativo del 26 novembre

Il [collaudo del rollout Core e dei cataloghi](collaudo-rollout-core.md) documenta la release installata sui tre PC, i puntatori aggiornati delle due app e l’uso del catalogo Core anche dal launcher Ubuntu. Il controller Kali usa ora un’identità di servizio dedicata autorizzata su Ubuntu e Windows e un profilo MCP senza bootstrap grafico. Nove esecuzioni dalle tre origini verificano log e codice di uscita; le limitazioni del primo tentativo SSH Windows, del test senza nuovo boot e delle GUI rimaste aperte sono conservate nel resoconto. Le indicazioni precedenti sul controller Kali e sulla migrazione dei cataloghi descrivono lo stato prima di questo rollout. Restano da completare l’autorizzazione iniziale automatizzata di nuovi nodi, il provider Windows headless e la fase desktop multinodo.
