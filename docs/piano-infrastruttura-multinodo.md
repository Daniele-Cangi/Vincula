# Piano Vincula per nodi agenti e desktop

Vincula usa un unico catalogo di computer per l’accesso degli agenti, i lavori distribuiti e il desktop condiviso. L’aggiunta di un computer registra la sua identità, verifica le connessioni disponibili e abilita le funzioni scelte dall’operatore. Gli indirizzi di rete e il sistema operativo sono proprietà del nodo. L’UUID identifica il computer in tutti i componenti.

Il piano parte da `main` dopo la PR #8, commit `b470517`. I collaudi su ASUS Ubuntu, Lenovo Windows e Kali X260 hanno verificato inventario, lavori, trasferimenti e perdita della rete di Kali. La verifica del crash del worker richiede una prova distinta. Le valutazioni del testo allegato vengono confrontate con riproduzioni e misure prima di scegliere la soluzione.

## Sequenza e risultati verificabili

| Fase | Risultato | Criterio di completamento |
| --- | --- | --- |
| 1 | Sessioni MCP aggiornate | Configurazione dei client sui tre PC, avvio del runtime verificato e uso dei tre UUID da ciascuna origine. Registrare percorso, versione installata e risultati delle chiamate. |
| 2 | Aggiunta di un nodo nel catalogo comune | CLI JSON e due interfacce grafiche usano la stessa validazione e la stessa scrittura atomica. Aggiungere e aggiornare nodi senza cambiare il codice, mantenendo gli alias e le installazioni precedenti. |
| 3 | Mouse tastiera e appunti su più computer | Un principale esplicito, membri identificati per UUID e layout degli schermi. Configurazione Deskflow generata per il gruppo, trasporti SSH e TLS verificati. Collaudo Windows principale con ASUS e Kali, anche durante un trasferimento. |
| 4 | Recupero dopo crash Linux | SIGKILL del worker durante un programma con figli. Arresto dei processi controllati, riavvio del worker, esito riconciliabile e ripetizione dello stesso UUID senza nuovo avvio. |
| 5 | Recupero dei piani da un’altra origine | Manifest durabile con UUID di piano e lavori, recuperabile su un altro computer quando l’origine perde disponibilità. Risultati recuperati e nuove chiamate idempotenti. Coprire lotti e workflow di capacità. |
| 6 | CI funzionale Windows e Linux | Matrice Python minimo supportato e versione dei PC. Suite con deprecazioni fatali e smoke MCP isolato, eseguiti su ogni PR. |
| 7 | Prestazioni misurate sui tre PC | Stesso risultato su singolo nodo e lavoro ripartito. Registrare tempi di preparazione, copie, esecuzione, attesa e ricomposizione, ripetizioni e dispersione. |
| 8 | Scheduler e concorrenza basati sulle risorse | Requisiti del lavoro e capacità del nodo, limiti locali di CPU RAM GPU e numero di lavori. Verificare rispetto delle quote, approvazioni e comportamento quando un nodo diventa indisponibile. |
| 9 | Trasferimenti per dataset grandi | Budget di disco e rete, avanzamento, cancellazione e ripresa. Superare i limiti correnti mediante un protocollo verificabile, con gestione di file parziali e spazio insufficiente. |

Le fasi 2 e 3 condividono schema e interfacce. Il recupero del runtime e la CI possono procedere in parallelo alla progettazione del desktop. Le misure della fase 7 guidano i valori iniziali dello scheduler e le priorità dei trasferimenti.

## Aggiungere un nodo

Il flusso operativo parte da «Aggiungi nodo» nelle app. L’operatore inserisce o importa UUID, nome, famiglia del sistema e coordinate di rete. Il runtime locale verifica i campi e mostra l’identità SSH da confermare mediante un dato ottenuto sul nuovo computer. Le chiavi private rimangono sulla loro origine.

La connessione verificata permette di rilevare sistema, risorse, interprete, programmi e funzioni disponibili. L’operatore sceglie l’accesso agente e le funzioni desktop da abilitare. I prerequisiti mancanti hanno uno stato esplicito e un’azione per completarli. L’interfaccia pubblica lo stato del nodo solo dopo la verifica del relativo servizio.

Il registro mantiene gli UUID e gli alias già usati. Un blocco desktop opzionale riferisce gli stessi UUID e contiene il principale, i membri, la posizione degli schermi e la scelta sugli appunti. Il salvataggio usa lock, revisione attesa e sostituzione atomica. La rimozione di un nodo richiede la risoluzione dei suoi riferimenti nel layout e nei servizi attivi.

Le due GUI chiamano un runtime locale Python con richieste e risposte JSON. La GUI Ubuntu può usarne direttamente le funzioni. La GUI Windows usa un processo locale senza comporre comandi shell dai dati dell’utente. Il runtime riusa il registro, la validazione e i trasporti esistenti.

## Desktop condiviso

Deskflow fornisce un server sul principale e un client su ciascun altro nodo. Le configurazioni correnti delle app generano una sola coppia `dock-windows` e `dock-ubuntu`. Il generatore nuovo usa nomi derivati dagli UUID e l’intero layout del gruppo. Il principale viene scelto esplicitamente e può essere Windows, Ubuntu o un altro nodo compatibile.

Ogni client usa una connessione locale verso un tunnel SSH dedicato. Le impronte TLS del motore desktop e le chiavi host SSH sono verificate separatamente. I profili di Kali predisposti per MCP consentono comandi e SFTP, mentre gli inoltri desktop richiedono configurazione e identità dedicate con porte loopback limitate. L’enrollment rileva questo requisito prima di tentare la condivisione.

Su Wayland l’avvio e le autorizzazioni dipendono dai portali e dalla sessione grafica. Il collaudo deve verificare il consenso sul desktop, lo stop locale da ciascun computer, la disconnessione del principale, il ritorno di mouse e tastiera alla postazione e gli appunti nelle due direzioni. La scelta `ask` o `full` per i lavori agente e il consenso desktop hanno controlli distinti.

## Affidabilità dei lavori e dei piani

La riproduzione isolata ha confermato che SIGKILL di un worker Linux avviato direttamente può lasciare vivi il programma e un figlio. Il servizio systemd contiene già i processi nel proprio cgroup. Il percorso CLI richiede una supervisione equivalente per i gruppi di processi che avvia.

La correzione conserva `unconfirmed` dopo un’interruzione senza ricevuta finale. Il nuovo worker deve attendere la pulizia prima di acquisire il lavoro successivo. I test coprono avvio fallito, uscita normale, stop, crash, riavvio concorrente e ripetizione dello stesso UUID. Il supervisore adotta e pulisce anche discendenti che cambiano sessione. Il contenimento mediante cgroup resta necessario per la garanzia forte quando vengono terminati insieme worker e supervisore, o quando i discendenti assumono privilegi diversi.

Per i piani serve un manifest durabile con nodi assegnati, UUID dei lavori e trasferimenti, revisione del formato e digest. La replica deve precedere gli effetti che renderebbe irrecuperabili. Un’origine diversa deve poter ritrovare gli identificativi e leggere gli esiti dai worker. L’adozione di un piano preserva le approvazioni sul destinatario e verifica la compatibilità con piani precedenti e ripetizioni concorrenti.

## Misure e pianificazione delle risorse

Il benchmark distingue tempo totale percepito e tempo di calcolo. Usa un carico CPU suddivisibile e dati sintetici riproducibili, verifica un digest del risultato e confronta il singolo nodo più rapido con la ripartizione sui tre computer. Rileva l’effetto del Wi-Fi, del relay e delle differenze di prestazioni tra i PC. Un carico GPU va aggiunto quando lo stesso algoritmo è effettivamente disponibile sui nodi confrontati.

Lo scheduler userà requisiti dichiarati dal lavoro e capacità rilevate. Carico istantaneo, memoria libera e disponibilità GPU informano l’assegnazione, mentre quote locali e prenotazioni limitano l’esecuzione concorrente. I lavori già approvati mantengono la propria identità durante i cambi di coordinatore.

I trasferimenti attuali hanno un limite di 64 MiB e il relay usa disco temporaneo sull’origine. Il protocollo per dataset grandi dovrà gestire quote, spazio libero, ripresa e verifica finale. La scelta fra relay e collegamento diretto dipenderà dalle rotte autorizzate e dalle misure, con verifiche di integrità per entrambe le modalità.

## Stato di avanzamento

Il goal è attivo dal 22 novembre 2026. Tre sessioni Codex nuove hanno usato MCP su Ubuntu, Windows e Kali, ciascuna con dieci chiamate riuscite e i tre nodi disponibili. Codex Windows è stato aggiornato da 0.161 a 0.171. La revisione installata è verificata mediante configurazione di avvio e SHA-256 dei sorgenti. I tool attuali espongono lo stato dei nodi e il sistema operativo, mentre la revisione del runtime resta un dato di installazione.

Il [benchmark sui tre PC](benchmark-cpu-tre-nodi.md) è completato con dodici casi validi. Il totale mediano distribuito a freddo è 24,637 secondi, contro 20,491 secondi del seriale locale. Il risultato impone di includere località dei dati e costo del coordinamento nella scelta dei nodi. Le modalità `ask` sono conservate e le fixture sono state rimosse.

Nel ramo di sviluppo sono implementati il runtime JSON del catalogo, il primo pannello Nodi nelle due GUI, la supervisione Linux e il recupero in lettura dei manifest batch e capability. La suite condivisa esegue 79 test con warning fatali su Linux. Proseguono acceptance Windows/Kali e review prima della distribuzione. Il runtime operativo sui tre PC resta congelato a `b470517`.

La PR #10 contiene CI e documentazione. La matrice hosted Linux passa con dipendenze bloccate e hash verificati. È in corso la diagnosi dello stato Windows nel profilo MCP isolato. Il provisioning desktop multinodo, lo scheduler e i trasferimenti per dataset grandi restano incrementi successivi. Ogni incremento viene verificato con test mirati, review e collaudi applicabili prima della pubblicazione.
