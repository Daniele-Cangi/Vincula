# Collaudo del primo protocollo per file grandi

Questo resoconto riguarda il [primo incremento](large-file-protocol.md),
sviluppato sopra l’head `85f559a` della PR #27. Non è un rollout produttivo né
il completamento del [piano per dataset grandi](large-file-transfers.md).

## Risultati finali

La prima qualificazione sotto si riferisce al commit `755a5f3`. Il controllo
successivo della [PR #28](https://github.com/Daniele-Cangi/Vincula/pull/28) ha
individuato un P1 nel recupero e un problema CI Windows 3.10, corretti nei sorgenti
`3138c75c161647c912e94ccc1182f9b67115f694` e verificati nuovamente:

| Prova dopo le correzioni | Risultato | Durata |
| --- | --- | --- |
| Suite completa locale | 646 test, 20 esclusioni, zero errori/fallimenti | 98,834 s |
| Ubuntu nativo, Python 3.14.4 | 137 test, 2 casi Windows esclusi, zero errori/fallimenti | 22,097 s |
| Windows nativo, Python 3.14.7 | 137 test, 1 caso POSIX escluso, zero errori/fallimenti | 35,622 s |
| Kali nativo, Python 3.14.10 | 137 test, 2 casi Windows esclusi, zero errori/fallimenti | 41,270 s |

Il bundle successivo è `4eb33121a9befc7ffacb98f4b5e91fb8154b81bcad9ede46448dfbda6b658c33`;
le [nuove ricevute](collaudi/2026-11-28-large-files-review-fixes.json) verificano
90 file. Tutti i nuovi TEMP sono rimossi dopo codice 0 e cleanup confermato;
le due prove native Windows della nuova lettura degli ack sono eseguite, non
saltate. La verifica hosted Windows 3.10 dell’head aggiornato resta distinta.

Il P1 si verificava dopo la scrittura durabile di un blocco e prima del suo
checkpoint SQL: il coordinatore poteva saltare quel blocco senza riparare il
ledger. Ora una verifica positiva registra il checkpoint nella stessa transazione
dei controlli aggiornati di cronologia, epoch e grant. Tre prove riproducono
checkpoint perso, Stop fra verifica e checkpoint e sostituzione della cronologia;
la ripresa completa la pubblicazione senza conteggi duplicati.

La CI iniziale Windows 3.10 aveva rilevato un `PermissionError(errno=13)` durante
la lettura dell’ack, dopo l’esecuzione del programma. La nuova lettura usa
`CreateFileW` con condivisione read/write/delete e controllo reparse, conserva
il codice Win32 e trasferisce la proprietà dell’handle una volta sola. Il
contratto di retry resta limitato alla lettura per i codici 32/33; non accetta
generici errori di accesso né rilancia programmi. Questa correzione non dimostra
retroattivamente la causa dell’evento fisico precedente, la cui diagnosi era persa.

Il commit dei sorgenti è `755a5f3a1a58e1ad87e23903cd91bea4357821e7`.
Il bundle finale è `03601cf1bd80c8df8fff51e2e992c763c33fe61237146ea01dcb3af3258199b9`:
88 file verificati prima dell’invio. Le [ricevute pubbliche](collaudi/2026-11-28-large-files.json)
includono SHA-256 di ogni file e risultati privi dei percorsi locali privati.

| Prova | Risultato | Durata |
| --- | --- | --- |
| Suite completa locale, Python 3.14.4 | 630 test, 18 esclusioni, zero errori o fallimenti | 95,295 s |
| Suite nativa Ubuntu, Python 3.14.4 | 121 test, zero esclusioni/errori/fallimenti | 17,733 s |
| Suite nativa Windows, Python 3.14.7 | 121 test, zero esclusioni/errori/fallimenti | 24,439 s |
| Suite nativa Kali, Python 3.14.10 | 121 test, zero esclusioni/errori/fallimenti | 29,566 s |

Ogni run usa `-B -W error`. Per i tre run nativi sono confermati codice 0 del
processo di test e del trasporto, conclusione dei worker e rimozione del nuovo
TEMP. Le precedenti evidenze fallite sono conservate separatamente.
Passano anche i due test del benchmark esistente e lo smoke MCP con profili
privati legacy e registro di tre nodi. Una sessione MCP reale verifica i 34 tool,
le annotazioni di sola lettura, l’opt-in e l’attestazione del worker.

Per ripetere la suite mirata su ogni macchina, dalla directory `agent` con un
Python provvisto delle dipendenze del runtime:

```text
python -B -W error -m unittest test_transfer_state test_transfer_storage test_transfer_wire test_large_transfer_plans test_large_transfer_server test_large_files_runtime test_windows_launch_ack -v
```

Per le correzioni di review aggiungere i moduli `test_large_file_checkpoint` e
`test_launch_ack_io` allo stesso comando (137 metodi complessivi).

La CI e la review della PR rimangono controlli separati sull’head pubblicato.

## Verifiche e perimetro

Le suite usano file sintetici, cartelle e database temporanei, worker nativi
separati e processi creati dalla fixture. Non modificano servizi, credenziali,
configurazioni desktop o helper installati. I nodi di produzione restano sulla
versione precedente. Le suite native sono lanciate sui PC fisici mediante le
rotte Core già autorizzate; gli endpoint di ciascun trasferimento di test sono
nello stesso profilo temporaneo con identità distinte. **Non è una prova di
trasferimento di dati fra i tre PC tramite il nuovo protocollo SSH.**

Il caso completo crea un file sintetico oltre 64 MiB, ne prepara l’export con un
vero job, verifica e trasferisce 17 blocchi e pubblica il risultato con un altro
job. Il primo blocco passa in entrambe le direzioni attraverso la vera CLI
binaria da 4 MiB. Su Windows il test usa l’esatto comando `py.exe -3` del
trasporto; gli altri blocchi passano direttamente al dispatcher per isolare i
controlli del ledger. Hash complessivo, codici di uscita, cleanup dei processi
e conservazione del file finale dopo eliminazione dello staging sono verificati.

Sono coperti anche:

- `ask` con digest esatto dell’intento e del contenuto, `full`, cambio di grant,
  scadenza, quote e prenotazioni concorrenti;
- ripresa dei blocchi verificati, contenuto corrotto, sorgente cambiata, spazio
  insufficiente simulato, mancata sovrascrittura e ricevuta persa;
- Stop durante attività del pool, tentativo ancora in coda, vecchio job già
  concluso, ripresa esplicita e recupero del cleanup senza esito registrato;
- cambio della cronologia del nodo fra transazioni e fra argv del Core e helper;
- punto di pubblicazione durabile prima/dopo Stop, revoca e ricevuta tardiva;
- ACL applicate alla creazione, sostituzione della directory di pubblicazione,
  sidecar interrotto, cleanup per identità senza eliminare il file finale;
- limite comune per verifica e copia in una chiamata MCP, cursore lineare,
  riapertura del coordinatore, peer offline e repliche in conflitto;
- framing troncato, duplicati JSON, dimensioni e digest errati, operazioni non
  ammesse e lettura senza dipendere da EOF.

I test di esaurimento spazio, cambio origine, mancato ack e cambio cronologia
sono fixture controllate, non guasti fisici introdotti nei tre sistemi.
Il recupero del tentativo senza esito usa la prova Core di mancato avvio;
non introduce un nuovo SIGKILL del servizio produttivo. I test di sicurezza
Windows con mock non sostituiscono l’esecuzione nativa delle operazioni reali.

## Problema Windows riprodotto e correzione

Il primo bundle `438e292ee218b9b7e01512e6f5f65fa992041b886f99999a89f6e2d5abf70295`
ha superato 111 controlli su Ubuntu. Windows ha prodotto cinque errori e sei
fallimenti, tutti riconducibili al medesimo rifiuto dell’identità della sorgente
prima della preparazione. Il driver si è fermato senza avviare Kali; ha
conservato la fixture Windows e i diagnostici privati, senza ripetere la suite.

Un probe separato su un file sintetico da due byte, su Windows Python 3.14.7,
ha confermato che solo `ctime_ns` differiva: `lstat` restituiva il timestamp di
creazione, mentre `fstat` restituiva quello di modifica dei metadati. Identità
del volume, inode e dimensione erano uguali. Il comportamento corrisponde alla
[segnalazione CPython #157671](https://github.com/python/cpython/issues/157671).

La cattura iniziale usa ora lo stesso handle protetto e `fstat` delle verifiche
prima e dopo la copia. Non sono stati eliminati campi, arrotondati timestamp o
soppressi controlli. Le due regressioni distinguono la divergenza della sola API
dal cambiamento reale del timestamp dell’handle, che deve ancora negare l’export.
La nuova qualificazione usa un bundle diverso, verificato contro ogni file
corrente; non riutilizza né modifica la precedente evidenza fallita.

Il successivo bundle `050e676f75cbe8eb57d78177b2c211cd939b5d88c84a29fe63f7aa7b611652f1`
ha superato 113 test su Ubuntu. Su Windows sono rimasti un errore nel recupero
del sidecar e un avvio Core non confermato; anche questo run si è fermato prima
di Kali. Il recupero confrontava `fstat` con gli identificatori memorizzati da
`DirEntry.stat`, che su Windows sono nulli. Ora usa un nuovo `os.stat` senza
seguire link, mantenendo le verifiche di identità e proprietà; una regressione
simula gli identificatori nulli della cache.

L’avvio non confermato ha prodotto la sola diagnosi generica preesistente; la
causa esatta non era registrata e **non è dichiarata risolta**. Il Core registra
ora classe e codici numerici dell’errore, senza percorsi o testo arbitrario.
Soltanto errori Windows di sharing/lock 32 e 33 fanno riprovare la lettura dello
stesso ack privato entro il termine già esistente; nessun programma viene
rilanciato. JSON invalido, permessi negati e altre anomalie rimangono errori
incerti. I test di questa gestione usano un orologio controllato; non dimostrano
che l’evento osservato fosse uno sharing violation.

## Lavoro ancora necessario

Il primo collaudo SSH reale Windows→Kali ha consegnato tutti i 17 blocchi e
superato la ripresa emulata dopo otto blocchi, ma il driver ha segnalato un errore
mentre il job concludeva la pubblicazione. L’audit successivo, con database in
sola lettura e hash del file sintetico, conferma: file Kali pubblicato di
67.109.121 byte, SHA-256 uguale alla sorgente, job terminato con codice 0 e
cleanup attestato. Il coordinatore tentava di prenotare nuovamente l’endpoint
già terminale. La correzione `b8e512b` attende `publishing` e riconcilia soltanto
il rifiuto terminale esatto mediante una ricevuta valida, conservando il progresso.
Otto regressioni e la suite completa aggiornata (654 test, 20 esclusioni) passano.

Il secondo run, su nuove cartelle sintetiche, avanzava ma è stato interrotto dal
limite di osservazione del driver di 360 secondi. L’ultimo snapshot precedente
alla chiamata mostrava 15 blocchi e cursore 15; non viene presentato come conteggio
fisico finale. Serve misurare separatamente handshake, bootstrap e controlli
aggiuntivi della fixture prima di attribuire la durata al prodotto. Le
[ricevute della storia del collaudo](collaudi/2026-11-28-large-files-network-first.json)
distinguono dati consegnati, errore di controllo e mancato completamento del run.
I worker sono drenati, lo staging e le prenotazioni sono conservati; nessun
tentativo automatico e nessuna qualificazione delle sei direzioni sono dichiarati.

Prima del rollout servono CI e review dell’head finale e una prova dedicata di
trasferimento tra receiver temporanei su Ethernet/Wi-Fi, con coordinatore
interrotto e ripresa da un’altra origine. Le suite su tre PC non sostituiscono
questa prova. Controller durevole, relay con quote persistenti, pulizia esplicita
dei parziali abbandonati e benchmark restano separati. Nessun vantaggio di
throughput o di calcolo è dichiarato da questi test.
