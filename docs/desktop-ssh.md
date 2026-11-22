# Controllo desktop su SSH

`agent/desktop_ssh.py` collega il coordinatore a supervisor locali già predisposti.
Usa gli UUID e le route SSH del catalogo. L’installazione dell’helper, l’avvio del
supervisor, il consenso desktop e l’autorizzazione dei comandi devono precedere
l’uso di questo componente. Questo incremento non installa o avvia servizi,
non espone tool MCP, non modifica configurazioni, firewall o livelli agente e non
attiva Deskflow.

## Collegamento al coordinatore

Il chiamante fidato costruisce `DesktopSSHTransport(registry, session_id,
revisions, helper_pins, instances=None, timeout=6, ...)` e lo passa come callable
asincrono a `DesktopCoordinator`. Le mappe delle revisioni e dei pin devono
contenere esattamente i membri del gruppo desktop. Le revisioni sono quelle del
catalogo **sul destinatario**, che possono essere diverse da quelle dell’origine.
`instances`, quando noto, contiene l’UUID dell’istanza supervisor già verificata.

Il pin è `implementation_digest(directory)` del bundle di rilascio ottenuto e
verificato da un canale fidato. Comprende bridge, supervisor, guardiano, sicurezza,
registro, layout e dipendenze di trasporto. Deve essere fornito dal chiamante;
non si apprende dalla prima risposta del computer remoto. Il pin del bundle
certifica i file controllati dall’helper. Non certifica quale codice abbia già
caricato un supervisor rimasto aperto: il provisioning deve avviare il supervisor
compatibile e verificarne la nuova istanza e `stop_session` prima di ammettere il
desktop. Questo modulo non riavvia o adotta un vecchio servizio.

L’uso di questa API presuppone un’applicazione locale fidata con un gruppo e
comandi desktop già autorizzati esplicitamente dall’operatore. La presenza del
blocco `desktop` o del suo digest non costituisce consenso per eseguire argv
arbitrari. `ask` e `full` dei lavori agente non autorizzano questa API e restano
indipendenti. Non va esposta direttamente a un agente senza il successivo livello
di enrollment e autorizzazione desktop.

L’API possiede una sola sessione non riprendibile. Per terminarla, chiamare
`coordinator.stop()`, confermare gli stati terminali tramite `coordinator.status()`
e chiudere le connessioni con `await transport.close()` in `finally`. Nessun task
interno rinnova le lease. Gli heartbeat devono provenire da un task separato dai
trasferimenti; timeout, costo dell’avvio SSH e margine di scheduling devono
rientrare nella lease configurata sul destinatario.

## Tre canali distinti

| Canale | Azioni ammesse | Durata |
| --- | --- | --- |
| `stdio` | `prepare`, `commit`, `heartbeat`, `status` | Persistente per nodo e sessione |
| `stop-stdio` | Esclusivamente `stop_session` | Nuovo processo e collegamento per ogni stop esplicito |
| `status-stdio` | Esclusivamente `status` della sessione vincolata | Nuovo collegamento per la lettura dopo il ritiro del controllo |

Lo stop non attende il lock del canale di controllo. Ritira il controllo di quel
nodo e impedisce nuovi prepare, commit o heartbeat, inclusi quelli ancora in
coda. La lettura successiva usa il canale separato e non rinnova la lease né
riapre il controllo. Un ack `stopping` non conferma la conclusione del cleanup:
serve uno stato terminale osservato. Lo stop può essere ripetuto **esplicitamente**
con lo stesso UUID; non ci sono retry automatici delle operazioni.

Il trasporto avvia OpenSSH senza shell locale, con le opzioni condivise di
`node_ssh`: host pin obbligatorio, autenticazione pubblica, agent forwarding e
inoltri disabilitati. Le credenziali e gli indirizzi provengono esclusivamente
dalla route locale già ammessa. Payload e piano non possono scegliere host,
account, file della chiave, comandi bootstrap o percorsi del controllo.

Il bootstrap remoto è costante. Linux usa
`$HOME/.local/share/vincula-desktop/desktop_ssh.py`; Windows usa
`%LOCALAPPDATA%\Vincula-Desktop\desktop_ssh.py` tramite `py.exe -3 -IB` e `runpy`,
senza PowerShell o espansione del JSON in argomenti. Il computer locale usa
direttamente l’interprete corrente e lo stesso helper, senza SSH. I percorsi locali
di catalogo e controllo possono essere forniti dal chiamante fidato; non fanno
parte del wire remoto.

Il layout di installazione atteso non viene creato da questo incremento.
Il bundle Python richiede tutti i file elencati in `BUNDLE_FILES` nella stessa
cartella. Il supervisor deve essere già in ascolto nella sottocartella privata
`control`, oppure nel percorso locale predisposto dall’operatore. Il supporto
effettivo a una configurazione diversa della shell SSH Windows richiede un
collaudo separato; non viene cambiata la shell di OpenSSH.

## Identità, frame e credenziali

Il primo frame vincola protocollo intero `1`, scopo del canale, UUID di sessione e
nodo, revisione del catalogo, pin del bundle e istanza supervisor quando nota.
Il destinatario verifica identità locale, pin e catalogo. Il controllo ordinario
richiede il gruppo già configurato; `prepare` deve corrispondervi per principale
e membri. Prima dei suoi effetti verifica lo stato autenticato del supervisor.
Ogni risposta viene filtrata per istanza, nodo, revisione e UUID della sessione.
Il campo locale `status`, pur essendo globale nell’endpoint originale, non rende
leggibile lo stato di un’altra sessione tramite questo bridge. Non vengono
restituiti token, argomenti, percorsi dei log o errori grezzi del destinatario.

I frame usano otto cifre esadecimali minuscole, newline e l’esatto numero di byte
JSON UTF-8. Non attendono EOF per terminare una richiesta. Oggetti duplicati,
campi aggiuntivi, UUID non canonici e valori numerici non finiti vengono
rifiutati. Le richieste hanno un limite di 65.024 byte, lasciando spazio al token
che il client locale aggiunge; le risposte sono limitate a 65.536 byte.

Il token del supervisor resta sul destinatario. Solo
`desktop_supervisor.request()` lo legge e autentica il collegamento loopback.
Lo stop mantiene il bypass ACL di emergenza di quella API, limitato all’UUID
valido: la verifica del token lato server resta obbligatoria. Il canale di stop
può leggere l’identità del catalogo pubblicato atomicamente senza attendere il
mutex degli editor GUI; una revisione cambiata non deve impedire di fermare la
vecchia sessione. Richiede comunque catalogo leggibile e identità locale
corrispondente. Il bridge non ripara permessi, cataloghi o endpoint danneggiati.

L’istanza viene ricontrollata prima di ogni operazione e nelle risposte. Il
primo pin di istanza stabilito da un canale resta invariato anche se una risposta
di bind concorrente arriva più tardi con un’istanza diversa. Il protocollo locale
attuale non possiede un `prepare` che verifichi istanza e
operazione sotto lo stesso lock. Un restart tra controllo dell’istanza e invio può
quindi lasciare effetti incerti. La risposta con istanza diversa viene rifiutata,
senza adozione o retry; lo stop resta vincolato allo stesso UUID e la lease
autonoma resta la protezione finale. Una futura estensione del protocollo locale
potrà chiudere questa finestra atomicamente.

## EOF, cancellazione e limiti della prova

Quando può elaborare EOF o errore del canale persistente, l’helper sul destinatario
tenta soltanto `stop_session` per l’UUID vincolato e la stessa istanza. Un vecchio
canale non passa mai a stop globale e non ferma una sessione sostitutiva con
nuovo UUID. Se SSH è ancora bloccato o lo stop non è osservabile, il cleanup resta
non confermato fino alla scadenza o a una successiva lettura. Lo stderr dei
processi del bridge viene scartato; gli errori visibili sono costanti e non
includono diagnostica SSH o contenuto del token.

La cancellazione chiude soltanto le connessioni e i processi di trasporto creati
da questa istanza. Dopo EOF concede 0,3 secondi all’uscita, poi termina il proprio
processo, con budget di attesa limitati e kill finale se necessario. Non cerca
PID persistiti e non termina servizi remoti. Su perdita improvvisa della rete o
dell’origine il cleanup remoto immediato resta best effort; le lease del
supervisor e del guardiano non dipendono dal bridge.

`close()` impedisce tutte le nuove richieste e attende anche le aperture già in
corso. Un processo completato sul confine fra spawn e chiusura viene chiuso prima
del primo frame di bind; non può iniziare una sessione dopo il ritorno da close.
Stop e lettura terminale vanno quindi completati prima di chiudere il trasporto.

`agent/test_desktop_ssh.py` avvia helper stdio separati e tre supervisor TCP
autenticati veri, tutti in TEMP privato e con programmi sintetici. Copre pin,
protocollo, identità, catalogo, token errato, status scoped, stop durante RPC
bloccata, EOF, istanza cambiata, vecchio UUID contro sessione sostitutiva e lettura
terminale dopo stop. Su Linux include deriva dei permessi del token; le DACL
Windows restano coperte anche dalla suite nativa del supervisor. Le fixture
rimuovono processi e directory. Due fixture concorrenti deterministiche coprono
il primo pin di istanza e il completamento di uno spawn durante close.

Queste prove locali non certificano ancora SSH fra i tre PC, installazione del
bundle, desktop, input o appunti. `desktop_ready` resta `false`; adapter Deskflow,
TLS, portali, enrollment e integrazione nelle app restano incrementi successivi.
