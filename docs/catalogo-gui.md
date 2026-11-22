# Catalogo dei nodi nelle applicazioni

La scheda **Nodi** legge e modifica il registro comune di Vincula. Il salvataggio aggiorna l’identità e il percorso SSH del computer; non collauda la connessione. **Layout desktop e prerequisiti** configura il gruppo e mostra l’anteprima descritta sotto. Le altre schede continuano a usare il profilo legacy a due PC.

Il pannello mostra nome, famiglia, UUID e descrizione del collegamento. **Aggiungi**, **Modifica** e **Rimuovi** usano il CLI JSON `agent/nodes.py`. Il modulo contiene UUID, nome, famiglia, tipo di collegamento, indirizzo, porta, utente, file identità, file known_hosts e rete. Per aggiungere un computer remoto usa il suo UUID Vincula verificato. L’UUID di un nodo già registrato è immutabile. I campi non esposti dal modulo, inclusi alias e indirizzo di bind, vengono conservati.

Ogni modifica invia la revisione letta con `expected_revision`. Quando un altro client aggiorna il registro, la GUI segnala il conflitto e propone di aggiornare l’elenco e riaprire il modulo. Il runtime protegge il nodo locale e i riferimenti del layout desktop durante la rimozione.

Le operazioni del catalogo e i moduli di modifica sono asincroni e modeless. Lo stop della condivisione resta accessibile durante un salvataggio o un trasferimento. Alla chiusura dell’app, un salvataggio già avviato completa la sua risposta prima della chiusura della finestra.

## Individuazione del runtime

`VINCULA_RUNTIME` indica la radice di un runtime completo, contenente `agent/nodes.py`, `agent/node_registry.py` e `agent/desktop_layout.py`. Un override esplicito viene verificato e produce un errore chiaro se il runtime manca.

La ricerca automatica usa la cartella dell’app, la radice della checkout ricavata dalla posizione dell’app e il percorso standard della piattaforma.

| Piattaforma | Runtime standard | Interprete predefinito |
| --- | --- | --- |
| Linux | `~/.local/share/vincula/runtime` | Lo stesso Python della GUI |
| Windows | `%LOCALAPPDATA%\Vincula\runtime` | `py -3` |

`VINCULA_PYTHON` può indicare il percorso di un interprete Python dedicato. Il CLI del catalogo usa soltanto la libreria standard. Il registro segue `VINCULA_NODE_REGISTRY` se impostata, altrimenti i percorsi descritti in `registro-nodi.md`.

Per una distribuzione minima del pannello si possono copiare `nodes.py`, `node_registry.py` e `desktop_layout.py` in una cartella `agent` accanto all’app. Il runtime completo condiviso con MCP è il percorso di installazione raccomandato per gli incrementi successivi.

## Distribuzione delle GUI

Su Linux mantieni `node_catalog.py` accanto a `windows-dock.py`, insieme ai moduli già richiesti dall’app. La GUI richiede GTK 3 e Python 3.10 o successivo per il runtime del catalogo. In una checkout la ricerca del runtime trova automaticamente la cartella `agent` della radice.

Su Windows la build WinForms richiede anche `System.Web.Extensions.dll` per la serializzazione JSON. Il comando nella README include il riferimento. Il PC deve avere `py -3` con Python 3.10 o successivo disponibile oppure `VINCULA_PYTHON` configurata. L’app può essere compilata direttamente in `outputs/Linux Dock` per usare la ricerca della checkout, oppure distribuita con il runtime in uno dei percorsi indicati.

## Compatibilità e passaggi successivi

Quando il registro manca, la scheda mostra il profilo a due PC e offre **Registra questo PC**. Questa operazione crea l’identità locale con un UUID. Se esiste un profilo legacy configurato, il nodo locale conserva il relativo alias storico. Le installazioni nuove iniziano con alias vuoti.

Un profilo SSH legacy di un nodo già registrato rimane selezionabile nel modulo. Per passare a un percorso esplicito si sceglie **SSH esplicito** e si compilano i campi. **Da configurare** rimuove il percorso del nodo dal registro.

## Layout desktop e controlli preliminari

Il modulo modeless permette di scegliere il principale (PC con mouse e tastiera), i membri, gli appunti e le coordinate X/Y di ogni computer. X cresce verso destra, Y verso il basso. Ogni nodo rappresenta il desktop logico di quel PC, anche se contiene più monitor fisici. Il principale deve essere incluso tra i membri.

Le celle devono essere uniche e formare un gruppo connesso tramite lati adiacenti: `(0,0)`, `(1,0)`, `(2,0)` è una fila valida; `(0,0)` e `(2,0)` da sole hanno un vuoto; due celle diagonali non sono collegate. Per questo primo compilatore non esistono schermi con larghezze variabili, collegamenti parziali o salti sopra celle vuote. Un layout non connesso viene rifiutato anche al salvataggio, preservando il file precedente.

**Anteprima** usa l’azione JSON read-only `desktop_preview`, con `desktop` opzionale e `expected_revision`. Restituisce lo snapshot corrente e `desktop_preview` con `layout`, `names`, `links`, opzioni del principale e `preflight`. Non salva file, avvia programmi o apre connessioni. I nomi Deskflow derivano esclusivamente da `vincula-<UUID>`; etichette, alias e sistema operativo non entrano nel testo della configurazione. L’ordine è stabile e tutti i collegamenti sono reciproci.

Le opzioni previste richiedono TLS e verifica delle impronte. Gli appunti producono `server.enableClipboard` e `clipboardSize` nelle impostazioni Deskflow moderne; il testo del layout contiene schermi e collegamenti. I percorsi del certificato e del layout e la lista dei peer fidati verranno completati dal provisioning. Queste opzioni sono un’anteprima strutturata, non un file di avvio pronto. La sintassi segue la documentazione ufficiale [Command Line](https://github.com/deskflow/deskflow/wiki/Command-Line), [Server Text Config](https://github.com/deskflow/deskflow/wiki/Server-Text-Config) e [GUI Settings](https://github.com/deskflow/deskflow/wiki/GUI-Settings); il controllo della versione effettivamente installata resta un prerequisito.

Il preflight inventaria localmente il binario Deskflow e l’esistenza dei file SSH. **Presente** non significa collaudato. Le capacità dei nodi remoti, autenticazione, forwarding, certificati TLS e consenso grafico sono indicati come mancanti o da verificare. Una chiave host SSH verificata non autentica il certificato TLS Deskflow. Su Wayland occorre verificare i portali InputCapture/RemoteDesktop e Clipboard e il relativo consenso locale; su X11 occorre l’accesso alla sessione grafica. Il preflight non richiede né sostituisce quel consenso e non modifica i livelli agente `ask`/`full`.

L’anteprima restituisce sempre `launchable:false`, `preflight.ready:false` e `active:false`: supervisor, lease, provisioning e trasporti multinodo non sono ancora implementati. In particolare il collegamento MCP di Kali non abilita automaticamente il forwarding desktop: servono credenziali dedicate con permessi limitati e una verifica effettiva. Il salvataggio del layout non altera una condivisione legacy già attiva.

L’instradamento delle altre schede verso il nodo selezionato e l’attivazione Deskflow multinodo appartengono all’incremento successivo.
