# Vincula Core: ricevente Linux al boot

`agent/core_service.py` prepara localmente un ricevente Linux già configurato. Il worker viene avviato da systemd con l'account scelto esplicitamente dall'operatore; non dipende da login grafico, app Desktop, server MCP locale o chiave SSH in uscita. L'agente controller continua a utilizzare SSH e MCP esistenti. L'autenticazione SSH in ingresso deve essere già predisposta dall'operatore.

Questa procedura è un comando locale di configurazione del sistema, non uno strumento MCP. Non installa un nuovo protocollo, non legge o copia chiavi e non cambia firewall, porte, host pin o autorizzazioni SSH.

## Prerequisiti e scelte

Servono Linux con systemd, Python compatibile con il runtime e un account locale scelto esplicitamente. Il runtime di rilascio e lo stato del worker devono essere già inizializzati nella home di quell'account: database `~/.local/share/vincula-jobs/jobs.sqlite3` e sei helper corrispondenti al rilascio. Non vengono inizializzati altri database né copiati helper dalla procedura di configurazione. Il worker avviato mantiene il proprio normale percorso di inizializzazione idempotente.

L'operatore deve fornire la directory assoluta del rilascio, il suo interprete Python e un digest esternamente verificato del manifest `_release-manifest.json`. Il digest è SHA256 dei byte UTF-8 di `json.dumps(manifest, indent=2)`, senza newline finale; il manifest deve indicare una revisione Git completa e i digest dei sei helper. Il digest è un pin scelto fuori da questa procedura, non un'attestazione prodotta dal ricevente.

Ogni `plan` e `install` richiede entrambe le scelte esplicite:

- `--access ask|full`: policy del proprietario del nodo.
- `--native-execution enable|disable`: consenso persistente per l'esecuzione diretta di programmi tramite gli strumenti Core esistenti.

Nessuna scelta viene dedotta dall'assenza di una GUI. Se l'operatore seleziona esplicitamente root, runtime, stato, helper, interprete e relativi antenati devono appartenere a UID 0 e non essere scrivibili da group/other; non sono ammessi symlink per stato e runtime. Per l'interprete OS sono ammessi symlink root-owned, con destinazione e antenati verificati in sola lettura. Anche eventuali directory `__pycache__` e tutte le entry preesistenti accanto agli helper devono soddisfare questa verifica: `-B` impedisce la scrittura del bytecode, non la sua lettura. Non è possibile eseguire come root un runtime modificabile da un altro account. `full` con esecuzione nativa abilitata consente lavori autonomi entro i privilegi dell'account selezionato. `ask` conserva il flusso di approvazione esatto esistente, utilizzabile dal proprietario attraverso i comandi locali anche senza Desktop. L'installazione non approva lavori pendenti.

## Autorizzare l'agente una volta

L'agente usa gli strumenti MCP: riceve identificativi dei nodi, capacità ed esiti e non deve conoscere password, passphrase o contenuti delle chiavi SSH. Vincula e il sistema operativo gestiscono la connessione. La configurazione iniziale del servizio richiede l'autorizzazione amministrativa locale; i successivi lavori `full` non richiedono quella password.

L'autorizzazione del nodo e quella del client AI sono due controlli distinti. Per esempio, Codex può rifiutare una chiamata MCP che richiede conferma anche se il nodo è già `full`; `approval_policy="never"` non preautorizza automaticamente tali chiamate. Se l'operatore vuole delegare a Vincula le richieste verso i nodi già configurati, può aggiungere alla **tabella Vincula esistente** nel proprio `config.toml`:

```toml
[mcp_servers.vincula]
default_tools_approval_mode = "approve"
```

Conservare i campi esistenti di comando, argomenti e ambiente. Questa è un'impostazione del solo server MCP Vincula, descritta nella [documentazione ufficiale Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Non cambia il modello, l'autenticazione del client, la sandbox locale o gli altri server MCP. Le policy del destinatario continuano a valere: un nodo `ask` mantiene l'approvazione esatta e un nodo `full` esegue entro i privilegi del proprio worker. Eventuali requisiti amministrati del client rimangono vincolanti. Altri client MCP hanno le proprie impostazioni di preautorizzazione.

Su un ricevente puro non serve avviare il bootstrap grafico delle chiavi SSH in uscita. Se una precedente installazione lo avvia al login, la relativa voce autostart può essere disabilitata conservandone una copia e mantenendo protette le chiavi. Il provisioning delle credenziali per un controller Linux è un componente distinto. Anche lo sblocco di un disco di sistema cifrato precede l'avvio dell'OS ed è separato dall'autenticazione dell'agente: questo installer non rimuove la cifratura.

## Uso

Esempio con segnaposto da sostituire con dati verificati localmente:

```sh
python3 agent/core_service.py status --owner nomeutente

python3 agent/core_service.py plan \
  --owner nomeutente --release /percorso/assoluto/del/rilascio \
  --manifest-sha256 DIGEST_VERIFICATO --python /usr/bin/python3 \
  --access ask --native-execution disable

sudo python3 agent/core_service.py install \
  --owner nomeutente --release /percorso/assoluto/del/rilascio \
  --manifest-sha256 DIGEST_VERIFICATO --python /usr/bin/python3 \
  --access ask --native-execution disable
```

`plan` e `status` sono utilizzabili senza privilegi amministrativi se l'account chiamante può leggere lo stato del proprietario. Il piano mostra revisione, policy, stato corrente e percorsi/digest dei file OS previsti. `install` richiede root locale; non tenta elevazione e non apre dialoghi. Per un account che non ha ancora memorizzato un PATH del worker serve `--worker-path` esplicito. In assenza di `--worker-path` viene conservato il PATH esistente. Un override esplicito è ammesso: tutte le entry devono essere assolute e prive di caratteri di controllo.

## Installazione e ripristino

La procedura verifica prima il pin, gli helper e l'assenza di lavori pending, queued, running o stopping. Le righe storiche unconfirmed vengono conservate senza inventare un esito e non sono conteggiate come lavoro attivo; il lock deve appartenere al worker corrente verificato oppure risultare libero. Un lock amministrativo impedisce due configurazioni simultanee. Crea file OS con pubblicazione atomica, proprietà amministrativa e directory non scrivibili da altri utenti; rifiuta configurazioni diverse già presenti e drop-in del worker non revisionati.

L'unità `/etc/systemd/system/vincula-core-worker.service` usa `User=nomeutente`, il PATH esistente e il rilascio selezionato. Rimuove dall'ambiente i riferimenti alla sessione grafica, D-Bus e SSH agent. Non aggiunge nuovi vincoli di sandbox ai programmi autorizzati.

Prima del passaggio vengono verificati import del runtime con l'interprete dell'account e sintassi systemd. Il vecchio worker utente, se presente, viene disabilitato e fermato attraverso il proprio arresto cooperativo; in assenza di conferma del drenaggio la procedura si ferma senza forzare la terminazione. Quando manca il manager utente/D-Bus, la procedura legge esplicitamente lo stato di abilitazione offline e usa `disable --no-reload` nell’ambito del solo account selezionato. Uno stato non determinabile viene rifiutato; non viene assunto disabled e non viene usato `--global`. Prima di abilitare il worker di sistema deve risultare libero il lock esclusivo del worker. La verifica del nuovo worker controlla PID, proprietario, l’intero argv esatto generato dall’unità e lock esclusivo.

Una copia SQLite consistente e i documenti di intent/receipt restano sotto `/var/lib/vincula-maintenance/receiver-…`, in directory root privata. Database operativo, programmi registrati, grant e risultati non vengono sostituiti dal backup. Le due scelte di policy sono applicate per ultime in una transazione che deve conservare tutte le altre tabelle; se un cambio di policy comporterebbe modifiche collaterali, la transazione viene rifiutata.

Il descrittore originale di ogni payload pubblicato rimane aperto fino alla conclusione dell'installazione o del rollback; il ledger usa `fstat` su quel descrittore per impedire il riuso dell'inode dopo unlink. Tutti i descrittori vengono chiusi nel `finally`, anche in caso di errore. Prima della conferma di policy, un errore verifica identità e digest dei file posseduti prima di intervenire sul lifecycle dell’unità e tenta il ripristino del worker utente originale e rimuove soltanto file OS creati da questa esecuzione con inode e digest ancora corrispondenti. Non ripristina automaticamente il database. Lavori comparsi nel frattempo, file alterati o esito di commit incerto richiedono verifica locale; la receipt lo segnala. Dopo il commit le scelte del proprietario vengono conservate anche se manca la conferma finale. Un'installazione già identica, attiva e abilitata persistentemente (`UnitFileState=enabled`) è idempotente; active con disabled o enabled-runtime richiede riparazione separata, senza riattivazione implicita; riparazioni, modifiche di rilascio e upgrade richiedono una procedura separata.

## Rete opzionale al boot

Se l'accesso SSH dipende da NetworkManager, si possono aggiungere insieme `--network-profile UUID` e `--expected-ipv4 INDIRIZZO`, eventualmente con `--ssh-unit sshd.service`. Il profilo deve essere già autorizzato dall'operatore, disponibile a livello di sistema e capace di attivarsi senza un agente grafico per i segreti. La procedura controlla soltanto UUID e campo pubblico `connection.permissions`; non legge o modifica credenziali del profilo.

Vengono creati un helper root-owned in `/usr/local/libexec/vincula/core-network.py` e un drop-in `ExecStartPre` dell'unità SSH selezionata. Al successivo avvio di SSH l'helper verifica l'IPv4 previsto e, se manca, attiva quel solo UUID. L'installazione non riavvia rete o SSH. Non cambia listen address, firewall o profili NetworkManager.

## Stato e limiti della verifica

`worker_running` conferma l'osservazione corrente del processo. `reboot_verified` e `desktop_ready` non vengono attestati dall'installazione. La prova richiesta dopo un riavvio deve avvenire dal controller reale, senza login grafico sul ricevente, eseguendo via MCP un lavoro autorizzato e verificandone log e codice di uscita.

Il [collaudo del 25 novembre](collaudo-core-ricevente.md) documenta installazione reale, avvio di sistema e due lavori MCP dal vero Codex Windows, inclusa l'esecuzione con le chiavi in uscita del ricevente bloccate. Distingue esplicitamente questi risultati dalla prova completa senza login grafico, ancora pendente.

Credenziali gestite per controller Linux, provider Windows, integrazione GUI e consenso alla condivisione schermo/input/clipboard sono componenti separate. Un ricevente Core non richiede una propria chiave in uscita. Le capacità che usano applicazioni grafiche possono richiedere una sessione grafica, indipendentemente dalla disponibilità del worker Core.

La suite `agent/test_core_service.py` usa database temporanei, la transazione reale estratta da `jobs.py` con `sqlite3.Row` e comandi finti: verifica pin, identità, policy, backup, cutover, rollback, lock e profili senza usare root, rete o un manager systemd live. Quando disponibile, esegue anche `systemd-analyze verify` nativo senza privilegi, avvio di unità o generatori, con home e rilascio contenenti spazi e percentuali. Il campo scalare `WorkingDirectory` usa il percorso letterale con `%` raddoppiato, separatamente dal quoting di argv e liste. Non costituisce un collaudo post-boot.
