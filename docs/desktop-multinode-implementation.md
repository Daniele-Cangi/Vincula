# Gruppo desktop: implementazione e prossimi collaudi

Stato del collaudo 27–28 novembre 2026: PR #26 in bozza. Motore gestito e helper sono
installati su Ubuntu, Windows e Kali. Nella sessione reale i tre supervisor
hanno riportato readiness recente contemporaneamente. L'operatore ha confermato
mouse, tastiera, click, rotella e copia/incolla nelle sei direzioni. I pannelli
QA non hanno registrato i sei token: questa verifica resta dichiaratamente
manuale, distinta dalle attestazioni e dai test automatici. La PR #25 è già su main.

La disposizione scelta dall'operatore è **Ubuntu — Windows — Kali**, con
Windows principale. L'interfaccia permette di includere altri nodi registrati,
cambiare principale e spostare gli schermi con frecce o coordinate, anche su
più righe. Il primo backend gestito supporta l'avvio soltanto con Windows
principale; salvare un'altra disposizione non certifica il supporto del backend.

## Interfaccia e ciclo di vita

La pagina Mouse e tastiera contiene la configurazione del gruppo e i comandi di
avvio/arresto. Salva layout modifica il catalogo; non avvia programmi.
Il nuovo percorso e la condivisione precedente sono alternativi. Un arresto non
confermato deve restare visibile, senza riattivare automaticamente la condivisione.

`desktop_app.py run` è il processo proprietario del gruppo. Le app leggono eventi
JSONL di stato e inviano `{"action":"stop"}` su stdin. Anche EOF ferma il gruppo.
Il percorso dello stop è indipendente dai trasferimenti, dalle modifiche al
catalogo e dai heartbeat in attesa. Solo snapshot privati recenti del motore,
con peer autenticati e consensi reali, possono produrre lo stato pronto.
Lo stop attende i terminali dei supervisor originali; la sola risposta
`stopping` non viene presentata come cleanup completato.

`desktop_node.py` espone soltanto inspect/provision/configure all'account
autenticato via SSH. Gli eseguibili e i percorsi arrivano dall'installazione
locale approvata. I certificati privati sono generati sul nodo e non viaggiano
nel protocollo. Il manifest comune lega UUID, sessione, layout, appunti, pin
distinti e revisioni locali. L'aggiornamento del solo gruppo nei cataloghi usa
CAS; un cambiamento concorrente invalida la preparazione.

La configurazione locale `Vincula-Desktop/deployment.json` deve essere privata e
abilitata dall'operatore. Se manca, il pulsante non installa o sostituisce
silenziosamente il motore: mostra il prerequisito mancante e termina senza
avviare il gruppo. L'[installer verificato](desktop-install.md) e il servizio
grafico opzionale sono disponibili; il Core rimane separato.

## Motore e limiti operativi

La [patch Deskflow](../third_party/deskflow/README.md) usa profili e certificati
monouso, pipe private, pin TLS associati all'UUID sullo stesso stream e controllo
del loop eventi reale. Clipboard disabilitata non attiva i relativi provider.
Revoca, disconnessione dopo l'adozione e snapshot scaduto sono terminali.
Il guardian conserva sempre la terminazione del gruppo/process tree originale.

Core/MCP rimane separato: una sessione grafica è necessaria per usare il desktop,
non per i lavori headless. Il profilo MCP `full` non sostituisce il consenso del
portale Wayland. Il deployment desktop è un'autorizzazione locale distinta.

## Verifiche e punto di ripresa

Il controllo SSH della PR #17 è stato collaudato separatamente sul sorgente
`6c3455f57b6cf00abec86922a08d2b103ca1dcfb`, con tre origini, audit e cleanup
passati. Da Windows lo stop della sessione sintetica è arrivato in 0,598 secondi
durante un trasferimento SFTP di 16 MiB e con heartbeat bloccato. Il file finale
ha mantenuto lo SHA256 atteso. Questo prova il controllo e la separazione dal
trasferimento; **non prova l'input o gli appunti del nuovo motore**.
La [ricevuta sanitizzata](collaudi/2026-11-26-desktop-ssh.json) conserva hash,
transazione, audit e cleanup. Il sorgente della nuova integrazione è diverso
dal sorgente congelato usato in quel collaudo.

La fixture GTK isolata supera sette scenari del protocollo e dei pulsanti:
[comando riproducibile](../tools/qa-desktop-ui/README.md). La GUI C# aggiornata è
avviata nella vera sessione Windows; le GUI GTK sono avviate nelle sessioni
grafiche Ubuntu/Kali. L'operatore ha confermato layout e pulsanti leggibili.
La matrice funzionale Windows/Linux con Python 3.10/3.14 è passata sul commit
`aa96e35`. La suite desktop locale conta 187 test: 180 passati, sei esclusi
perché richiedono Windows e uno perché seleziona un parser nativo esterno.
Quest'ultimo è stato eseguito separatamente con tutti i 20 test dell'adapter
passati, senza esclusioni. Comprende stop con RPC pendente, scadenza della readiness prima
della lease, errori di avvio, installazione, privacy e chiusura delle pipe.

Il [run nativo Windows](https://github.com/Daniele-Cangi/Vincula/actions/runs/46077829185)
ha superato 17 risultati Qt, senza esclusioni, e sei suite CTest. La stessa
patch è stata compilata e collaudata su Linux. I pacchetti comprendono
dipendenze, licenze e manifest con tutti gli hash; gli input per riprodurli
sono conservati separatamente dal runtime.

Il primo avvio reale ha scoperto un formato di configurazione superato:
Deskflow 1.27 ignora l'elenco nella sezione `screens` e legge invece
`[computer_<nome>]/name` dal file INI generale. L'adapter ora emette questi
gruppi e un layout con i soli collegamenti. Un
[probe con i veri Settings e Config](../tools/qa-desktop-config/README.md)
ha riletto tutti i nomi/vicini e respinto il vecchio formato, senza costruire
server, client o provider di input. I due tentativi falliti si sono fermati
con codice 4 sul principale; i processi originali dei client sono usciti prima
del nuovo tentativo con identità di sessione fresca.

La readiness EIS ora richiede keyboard, relative pointer e absolute pointer
tutti ripresi. Sono verificate le otto combinazioni e la revoca terminale alla
pausa di ciascun device; il test fallisce con il precedente predicato.
Il pacchetto Linux corretto è installato su entrambi i Linux. Il Windows
installato conserva il proprio artifact precedente, che non include EIS;
la nuova build Windows con la patch aggiornata è qualificata dalla CI citata.

L'Export XML del Task Scheduler reale omette `RunLevel=LeastPrivilege` e
normalizza il trigger da SID al nome dell'account. Il controllo accetta
queste due rappresentazioni solo se il livello resta limitato e il nome si
risolve nello stesso SID. Diciassette fixture native e il confronto con il
task reale sono passati senza modificare o avviare task durante la prova
del validatore. Successivamente il supervisor è stato avviato nella vera
sessione Windows interattiva. Entrambi i Linux usano il bus e l'ambiente
grafico già presenti nel rispettivo user manager.

La [ricevuta di installazione](collaudi/2026-11-27-desktop-install.json)
riporta i pin distinti del motore e del bundle di supporto. Il layout iniziale
è stato salvato con CAS sui tre cataloghi; le altre impostazioni sono rimaste
identiche. La regola firewall Windows limita TCP 24807 al programma installato
e agli indirizzi dei due client autorizzati.

Il [pannello QA](../tools/qa-desktop-live/README.md) verifica testi sintetici
solo su gesto dell'operatore. Il suo writer Windows crea file privati con
integrità Medium prima di scrivere: `mkstemp` di Python 3.14 introduceva
un'ACE aggiuntiva che il controllo privato rifiutava. Le sette fixture pure e
tre prove filesystem sono passate sul Windows reale senza accedere a GUI o
appunti durante quei test.

Lo stop reale dalla GUI Windows è stato osservato durante l'invio di un file
sintetico da 1 GiB: tutti i supervisor erano già `stopped` con il file temporaneo
ancora a 726.118.400 byte. Il trasferimento ha continuato fino a 1.073.741.824
byte, con SHA256 identico al sorgente. Un audit in sola lettura ha verificato
l'assenza dei PID originali di guardian e motori sui tre PC, senza adottarli o
terminare altri processi. Su Windows il codice di uscita del singolo motore
resta nullo perché il guardian termina l'intero Job, incluso se stesso.
Non è misurata la latenza dal click fisico. La
[ricevuta del test](collaudi/2026-11-27-desktop-transfer-stop.json)
distingue queste osservazioni dalla precedente sessione sintetica.

L'operatore ha segnalato che mancava una barra di avanzamento del trasferimento
nella GUI Windows. La nuova card fissa sopra i campi mostra percentuale,
byte/totale e risultato persistente. Metadati non disponibili producono una
barra indeterminata, non progressi stimati. Il 100% richiede pubblicazione e
ritorno riusciti; generazione e terminalità respingono callback tardive.
Trenta verifiche native includono rendering reale a 11/18 pt e Stop abilitato
durante il lavoro. Le API reali aggiornate hanno inviato e ricevuto 1 GiB con
12/14 callback, campioni intermedi e SHA256 identici: la
[ricevuta del progresso](collaudi/2026-11-28-windows-transfer-progress.json)
distingue questa prova del trasporto da quella della barra visibile. La nuova
GUI Windows è installata e avviata: collegamento, hash e finestra visibile nella
sessione interattiva sono stati verificati. Ubuntu e Kali mantengono la GUI
precedente con lo stesso bundle di supporto.

La review Codex del commit `d329536` è completata senza rilievi. Gli alert
automatici e il Quality Gate Sonar restano separati da questa review; il loro
triage è documentato e non comporta archiviazione automatica. I quattro file
sintetici da 1 GiB sono stati rimossi dopo controllo di hash e identità;
i pannelli QA sono stati fermati tramite i loro job dedicati.
La condivisione resta ferma su tutti i nodi; MCP è raggiungibile sui tre PC.

Alla ripresa del 28 novembre la condivisione era stata riattivata
dall'operatore. Il controllo Sonar completo ha identificato una cancellazione
esterna assorbita dal controller: ora propaga dopo il cleanup, mentre lo
Stop esplicito conserva l'esito normale. È corretto anche il parser `readelf`
del QA e sono isolate le fixture d'installazione dal supervisor reale.
La regressione locale passa 183 test desktop, con sette esclusioni native;
passano anche i cinque test tool e il QA headless dei 118 ELF su Ubuntu e Kali.
Il [triage aggiornato](collaudi/2026-11-28-desktop-security-triage.md)
distingue questi rilievi corretti dai quattro alert statici e dal debito di
manutenzione. L'aggiornamento delle app attende lo stop del gruppo attivo.

Prima del rilascio restano:

1. Completare la prova esplicita di revoca/disconnessione del nuovo motore.
2. Completare le review e autorizzare il merge della PR. Il
   [triage delle quattro segnalazioni automatiche](collaudi/2026-11-28-desktop-security-triage.md)
   documenta i confini esaminati; non modifica lo stato degli alert o il Quality Gate.

Questa preparazione non ha richiesto riavvii o cambiamenti alle identità Core.
