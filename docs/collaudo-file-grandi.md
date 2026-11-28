# Collaudo del primo protocollo per file grandi

Questo resoconto riguarda il [primo incremento](large-file-protocol.md),
sviluppato sopra l’head `85f559a` della PR #27. Non è un rollout produttivo né
il completamento del [piano per dataset grandi](large-file-transfers.md).

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

Prima del rollout servono CI e review dell’head finale e una prova dedicata di
trasferimento tra receiver temporanei su Ethernet/Wi-Fi, con coordinatore
interrotto e ripresa da un’altra origine. Le suite su tre PC non sostituiscono
questa prova. Controller durevole, relay con quote persistenti, pulizia esplicita
dei parziali abbandonati e benchmark restano separati. Nessun vantaggio di
throughput o di calcolo è dichiarato da questi test.
