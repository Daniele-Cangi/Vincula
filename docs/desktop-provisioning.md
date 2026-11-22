# Provisioning desktop multinodo: piano verificato sui tre PC

Stato del 22 novembre 2026: inventario in sola lettura e preparazione di asset nella cartella `work/desktop-provision-readonly`. Nessun pacchetto è stato installato sui nodi, nessuna chiave o regola firewall è stata modificata e la condivisione legacy Windows–Ubuntu è rimasta attiva. Il supervisor del commit `13dd` è una base locale collaudata: coordinatore, provisioning e collegamenti GUI restano da implementare.

## Inventario e layout proposto

| Nodo | Identità | Stato rilevato |
| --- | --- | --- |
| ASUS TUF / Ubuntu | `0d97d16f-1624-4467-b834-f0592a4d29ab` | GNOME Wayland; client Deskflow `clipboard-release` 1.27.0, protocollo 1.8; Qt 6.11.2; dipendenze risolte. Portali InputCapture v1, RemoteDesktop v2 e Clipboard v1 presenti. La loro presenza non certifica il consenso di una nuova sessione. |
| Lenovo / Windows | `a197f389-14b2-4716-86a2-70314e2ee4e2` | Portable upstream 1.27.0, verificato byte per byte rispetto all’asset ufficiale; Qt 6.11.3, OpenSSL 3.6.5. Server legacy attivo nella sessione interattiva 9; porta `127.0.0.1:24800` occupata. |
| PC Kali | `12365c6d-f1c3-41ad-a4cb-458363b2fae5` | Kali Rolling 2026.3, x86_64, XFCE X11 `DISPLAY=:0`; Deskflow e Flatpak assenti. Qt 6.11.2, glibc 2.43 e plugin Qt xcb già presenti. |

Il controllo `--version` Ubuntu ha restituito 1.27.0/protocollo 1.8. Su Windows il tentativo diagnostico in Qt offscreen è scaduto; è stato terminato soltanto quel processo diagnostico. La versione Windows è attestata dal PE e dall’identità SHA-256 con l’asset upstream, non da un banner riuscito. Il processo legacy è rimasto lo stesso.

Il layout soltanto proposto è Ubuntu `(-1,0)`, Windows principale `(0,0)`, Kali `(1,0)`, appunti abilitati. È stato compilato tramite `desktop_preview`: nomi `vincula-<UUID>`, collegamenti reciproci, `launchable:false`, `active:false`. Nessun registro è stato salvato. Questo gruppo riguarda mouse, tastiera e appunti; il video del desktop remoto richiede una capacità separata.

## Trasporto raccomandato: Deskflow con TLS diretto

La prima implementazione può usare le due reti già autorizzate:

| Client | Sorgente | Destinazione Windows | Interfaccia Windows |
| --- | --- | --- | --- |
| Ubuntu | `192.168.77.1` | `192.168.77.2:24800` | `Ethernet 4` |
| Kali | `192.168.1.137` | `192.168.1.106:24800` | `Wi-Fi` |

Ogni client avvia un solo Deskflow foreground; Windows avvia un solo server. SSH resta il canale di controllo e MCP, su connessioni diverse da SFTP. Questo evita account Windows aggiuntivi, forwarding e modifiche all’sshd di Kali.

Deskflow 1.27 espone un solo `core.interface`. Per ricevere sulle due NIC il caso semplice è un listener IPv4 `0.0.0.0:24800` con due regole firewall precise. Non equivale a un bind nativo su una lista di due indirizzi. Se la politica richiede socket legati esclusivamente ai due IP, occorrono due proxy TCP supervisionati davanti al server loopback; è un adapter ulteriore, non una funzione già presente. Le opzioni di rete e sicurezza sono documentate nella [configurazione upstream 1.27](https://github.com/deskflow/deskflow/blob/v1.27.0/docs/Configuration.md).

I client devono indicare un solo indirizzo server e vincolare `core.interface` al proprio IP selezionato: Ubuntu al cavo, Kali al Wi-Fi. Non si configura una lista di fallback che sposti silenziosamente Ubuntu sul Wi-Fi. Un cambiamento DHCP o di interfaccia produce un nuovo preflight, non un’autorizzazione più ampia.

### Firewall Windows da preparare dopo il GO

Le due regole devono vincolare contemporaneamente programma Deskflow verificato, TCP 24800, indirizzo locale, indirizzo remoto e interfaccia della tabella. Nomi deterministici per UUID permettono di aggiornare o rimuovere soltanto regole possedute da Vincula. Il piano prepara inizialmente regole disabilitate; l’attivazione appartiene al passaggio concordato della sessione. Non si aggiunge una regola generale per sottorete o programma su tutte le interfacce. I filtri disponibili sono descritti da [New-NetFirewallRule](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule).

L’inventario attuale rileva firewall abilitato e ingresso predefinito bloccato su tutti i profili, nessuna regola esplicita Deskflow. Le allow apparentemente generiche delle app Windows sono limitate da `PackageFamilyName`, un filtro che questo build espone sull’oggetto regola. Esiste inoltre una regola Codex Sandbox Offline limitata al proprio SID locale: resta intatta e non va trasformata in un’eccezione desktop. Il solo conteggio delle regole o la sola proprietà `Program=Any` non dimostrano l’applicabilità a Deskflow.

Il preflight definitivo deve riesaminare filtri applicazione/pacchetto, utenti, profili, porte, indirizzi, interfacce e block applicabili. Dopo l’attivazione servono sia le due prove positive sia quella negativa Ubuntu Wi-Fi → Windows Wi-Fi: quest’ultima sorgente non è autorizzata dalla tabella, anche se appartiene a un nodo con certificato noto. La porta non deve risultare aperta su IPv6 o altre NIC per effetto di ulteriori listener o eccezioni.

### Alternativa con tunnel SSH

Il modello client-initiated `ssh -N -L 127.0.0.1:<porta-client>:127.0.0.1:24800` mantiene il server loopback e richiede due processi per client. Una chiave dedicata con `from`, `restrict`, `port-forwarding` e `permitopen="127.0.0.1:24800"` limita le destinazioni di `-L`, ma **non garantisce da sola il divieto di `-R`**: `port-forwarding` riabilita il forwarding disabilitato da `restrict`. Le distinzioni sono esplicite nei manuali [sshd](https://man.openbsd.org/sshd.8) e [sshd_config](https://man.openbsd.org/sshd_config.5).

Per una garanzia solo-L occorre un ambito separato con `AllowTcpForwarding local`, `PermitOpen` limitato e shell vietata, per esempio un account Windows dedicato con un proprio `Match User`. Non si applica quel blocco all’account MCP esistente: cambierebbe anche altre sessioni e il comportamento legacy. Il server Windows corrente consente forwarding in entrambe le direzioni; sshd Kali può restare senza forwarding perché è il client a iniziare il tunnel. L’opzione TLS diretto evita questo nuovo account e la relativa modifica/reload del servizio.

## TLS, identità e enrollment

Le impronte rilevate dei certificati pubblici legacy sono:

- Ubuntu: `cb4a6428d9183b1454a396bc01e5dedb7322dc583dc2cd861dc92a220098f579`.
- Windows: `7bbbc833b74e65d38ff66290f84e77c3b37096b218831e7f4343d06d0eb73de4`.

I database pubblici di fiducia esistenti contengono le impronte reciproche corrette. Il PEM Ubuntu è `0600`; il PEM Windows concede accesso solo a utente, SYSTEM e Administrators. Non sono state stampate o esportate chiavi private. Nella preparazione locale sono stati esportati soltanto i certificati pubblici.

La prima sessione multinodo può riferire i PEM già presenti su Ubuntu e Windows senza spostarli e con database di fiducia nuovi e isolati. Kali deve generare la propria identità TLS localmente dopo il GO. Si esporta soltanto il suo certificato pubblico. I pin devono essere associati all’UUID attraverso un collegamento SSH con chiave host già verificata, con controllo del digest ricevuto; nessuna accettazione TOFU al primo collegamento Deskflow. L’impronta TLS autentica il certificato Deskflow e rimane distinta dalla chiave host SSH.

Windows deve fidarsi dei due certificati client; ogni client soltanto del certificato del principale. I profili devono impostare TLS e verifica peer in entrambi i ruoli. I database di fiducia legacy non vengono ampliati automaticamente. La rotazione di un certificato richiede un aggiornamento esplicito del descrittore pubblico e dei pin interessati, senza condividere il relativo PEM privato.

Il registro attuale è **locale a ciascun host**: GUI e server MCP dello stesso PC condividono il file e rilevano gli aggiornamenti. **Aggiungi nodo non propaga identità, percorsi o credenziali agli altri PC.** I tre registri odierni hanno gli stessi UUID ma differenti nodi locali e route; i loro SHA-256 non possono essere usati come un’unica revisione globale.

Il flusso successivo **Aggiungi nodo** deve quindi:

1. Acquisire dal nodo il suo UUID e un descrittore pubblico autenticato: label, OS, architettura, versioni runtime/Deskflow, certificato pubblico e digest, capacità e requisiti grafici.
2. Costruire e collaudare separatamente la route da ogni origine autorizzata. Percorsi delle chiavi, known_hosts e bind address rimangono specifici dell’origine; non vengono copiati fra PC.
3. Generare eventuali credenziali di controllo su ciascuna origine e distribuire soltanto le chiavi pubbliche. Le chiavi MCP esistenti restano separate. Per il controllo desktop usare un helper forzato, `restrict` e `from` coerenti con la route; il trasporto TLS diretto non richiede chiavi SSH dati.
4. Applicare il catalogo e il gruppo ai nodi autorizzati con revisioni CAS per host, e mostrare gli ack di enrollment. Un nodo elencato non è ancora un nodo desktop pronto.
5. Conservare un manifest comune di sessione basato su UUID, layout, principale, appunti e pin TLS, con un digest comune e una mappa delle revisioni locali. Fino a un’evoluzione del protocollo, il coordinatore tiene questa mappa e passa a ogni supervisor la propria revisione locale.

I livelli agente `ask` e `full` restano applicati separatamente per nodo. Il consenso desktop e quello imposto dal sistema grafico sono capacità distinte: `full` non simula né aggira un consenso Wayland assente.

## Kali userspace senza sudo

Il cache APT di Kali offre già `deskflow 1.27.0+dfsg-1`, proveniente da kali-rolling. Sono stati scaricati su Ubuntu e verificati rispetto agli SHA-256 dell’indice i seguenti pacchetti:

| Pacchetto | Versione | SHA-256 |
| --- | --- | --- |
| deskflow | `1.27.0+dfsg-1` | `6a40ad2cd5d6b45cb7c9cbbf8f24502de540b8a15a974c22bb66b9dd18f17008` |
| libportal1 | `0.11.0-1` | `9caffa6d16ff2922125afeaf1228625ba5d0196c0c3a267eeeb1243e3e9aea5b` |
| libei1 | `1.5.0-3+b1` | `663cbfd1e9d1f35672a5017a2f73d234b2a2e2383b1cb5db7674788b176c12c3` |
| libxkbregistry0 | `1.13.1-1` | `8c61e0c9a950574d5788a193d2e7bdb4ced05b17d5e9d55172facf0458d996df` |

La sola estrazione in `work` occupa circa 4,6 MiB. I requisiti rimanenti risultano già presenti: Qt 6.11.2, glibc 2.43, libstdc++ 16.2, GLib 2.90.1, OpenSSL 3.6.5, libxml2 2.15.4 e librerie X11. Anche su X11 servono libportal/libei come dipendenze dinamiche del binario; non occorre installare un servizio portal per usarne il backend X11.

La destinazione proposta su Kali è una versione privata sotto `~/.local/share/vincula/engines/deskflow/kali-1.27.0/`, con estrazione `dpkg-deb -x` in `root/`, verifica hash e manifest. Questo non registra pacchetti con dpkg, non esegue maintainer script e non richiede sudo. L’estrazione è documentata da [dpkg-deb](https://manpages.debian.org/trixie/dpkg/dpkg-deb.1.en.html); la fonte distribuzione resta il [repository ufficiale Kali](https://www.kali.org/docs/general-use/kali-apt-sources/).

Il launcher deve impostare per quel processo `LD_LIBRARY_PATH` alla sola `root/usr/lib/x86_64-linux-gnu`, usare i plugin Qt di sistema già installati e `QT_QPA_PLATFORM=xcb`. Non si copiano Qt Ubuntu su Kali né si cambia l’ambiente globale. `DISPLAY`, `XAUTHORITY` e il bus appartengono alla sessione locale autorizzata. Dopo il GO la prima prova è `--version` senza modalità client/server e risoluzione di tutte le librerie; soltanto dopo si prova la sessione grafica. La compatibilità è motivata da metadati e analisi ELF, non ancora da un’esecuzione del nuovo binario su Kali.

Sono stati confrontati anche gli asset upstream [v1.27.0](https://github.com/deskflow/deskflow/releases/tag/v1.27.0) Debian forky e trixie, entrambi verificati con il digest ufficiale. Il pacchetto Kali è preferibile per questa macchina perché corrisponde al suo insieme di librerie e mantiene il manifest della distribuzione; gli asset upstream rimangono un’alternativa esplicita da ricollaudare.

## Coordinatore, lease e stop

Il coordinatore deve usare una connessione SSH persistente dedicata per ogni nodo remoto, con protocollo JSONL limitato e helper locale che parla al supervisor loopback. Le connessioni di controllo non condividono processo, code o multiplexing SSH con SFTP. I log viaggiano in finestre piccole e limitate; il rinnovo della lease ha un percorso indipendente dalla lettura di log o trasferimenti.

Ogni supervisor parte nella sessione interattiva locale: Windows non deve creare Deskflow nella sessione SSH al posto della sessione desktop; Ubuntu e Kali devono usare l’ambiente della propria sessione grafica. Avvio automatico e installer utente vanno preparati come componenti espliciti. Il controllo remoto non esporta il token loopback: l’helper lo legge soltanto sul nodo.

Il protocollo distribuito proposto è:

1. Congelare manifest comune e revisioni locali; verificare ruoli, pin, rete, ACL, binari, dipendenze, sessione e autorizzazioni di tutti i membri.
2. `prepare` su tutti i nodi. I heartbeat partono dall’ack di ogni singolo nodo, anche mentre si attende il resto del gruppo o il consenso locale.
3. Concordare il passaggio dal profilo legacy: la porta Windows 24800 è occupata. Se il proprietario non è la sessione Vincula attesa, non si termina quel processo. Nessun cutover è stato eseguito durante questo inventario.
4. Avviare il server e poi i client con profili isolati. Tenere stato `starting`: `Popen` riuscito, porta in ascolto o TLS generico non bastano.
5. Dichiarare `ready` soltanto dopo l’handshake Deskflow di ogni UUID, verifica del pin su entrambe le parti e consenso grafico richiesto. I segnali vanno ottenuti da IPC o parser di log versionato, riferiti ai processi e alla sessione posseduti. Le attuali risposte `desktop_ready:false` del supervisor non cambiano automaticamente questa condizione.
6. Al fallimento di un membro, arrestare il gruppo e smettere di rinnovare tutte le lease. Nessuna riconfigurazione automatica del layout o elezione di un nuovo principale senza un nuovo piano autorizzato.

Questo fornisce un commit coordinato con rollback, non promette atomicità hardware nativa: Deskflow collega i client progressivamente. Un eventuale requisito di input/appunti completamente sospesi fino all’ultimo ack richiede una barriera applicativa verificata nell’adapter; non va simulata con uno stato UI. Le opzioni di lock del cursore non dimostrano da sole la sospensione degli appunti.

Ogni GUI e launcher locale deve chiamare il proprio stop loopback senza rete, anche se l’origine del lavoro è offline. Il canale di emergenza non attende approvazioni, trasferimenti o log. La perdita del coordinatore smette di rinnovare le lease; la perdita del supervisor chiude la pipe o i heartbeat del guardiano. I soli processi della sessione vengono terminati: trasferimenti, calcolo distribuito e altri lavori MCP restano separati. Su Linux rimane il limite già documentato del guardiano senza cgroup; un comando che daemonizza o un guardiano ucciso con SIGKILL richiedono un contenimento ulteriore prima di promettere quella copertura.

## Incrementi e collaudo dopo il GO

Prima implementare e revisionare enrollment pubblico, adapter Deskflow, protocollo di controllo persistente e coordinatore con processi fittizi. Poi preparare il runtime Kali e i profili senza attivarli, verificare i pin e produrre le due regole firewall disabilitate. La modifica del firewall e il passaggio dalla sessione legacy richiedono il momento di attivazione concordato.

Il collaudo reale deve coprire:

- Percorsi corretti: Ubuntu solo Ethernet, Kali solo Wi-Fi; prova negativa della sorgente Ubuntu Wi-Fi verso Windows Wi-Fi.
- Handshake mutuo e identità di tutti i membri; nessun peer senza pin accettato e nessun `ready` con un nodo mancante.
- Consenso Ubuntu Wayland e accesso Kali X11; transizioni su tutti i bordi del layout.
- Appunti sintetici nelle sei direzioni fra i tre PC, con autorizzazione al test; nessuna copia di chiavi o token nel controllo.
- Stop locale da ciascun PC durante un trasferimento SFTP separato: stop desktop tempestivo, trasferimento e relativo hash preservati.
- Perdita del solo controllo/origine, scadenza delle lease, cleanup e riavvio senza adottare PID storici; nessun riavvio nascosto della condivisione dopo la riconnessione.
- Regressione del profilo legacy e di `ask`/`full`; disabilitazione o rollback limitato alle sole risorse nuove possedute da Vincula.

Fino a queste prove il catalogo e il layout sono configurabili, mentre la condivisione su tre schermi non è dichiarata attiva.
