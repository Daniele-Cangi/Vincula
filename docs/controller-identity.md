# Controller Linux: credenziale di sistema

Un nodo che riceve lavori usa SSH in ingresso e il worker Core. Non deve caricare chiavi SSH verso altri computer. Un nodo che avvia connessioni ha invece bisogno di un'identità in uscita: il provider locale `agent/controller_identity.py` prepara un'identità dedicata, caricata da un servizio OpenSSH indipendente dal desktop. Le richieste dell'agente AI contengono gli identificativi dei nodi e le operazioni; non contengono credenziali.

Questo incremento implementa il provider e il collegamento al registro. L'installazione reale, l'enrollment e la verifica dopo boot delle origini Linux restano da collaudare. Le precedenti prove Windows → Kali riguardano il Core ricevente, non certificano questo provider.

## Requisiti e protezione

- Linux con manager systemd di sistema, `systemd-creds` e systemd almeno 250; strumenti OpenSSH e Python 3 disponibili nei percorsi OS `/usr/bin`.
- Account locale scelto esplicitamente dall'operatore. L'installazione richiede root locale una volta; i processi dell'agente non vengono elevati da questo provider.
- Provider di cifratura esplicito: `host` oppure `host+tpm2`. Il secondo richiede TPM e una policy compatibile con l'avvio. La presenza/versione degli strumenti non prova che il TPM o la decifratura al boot funzionino: devono passare l'installazione e il relativo collaudo OS.
- Nessuna modifica a chiavi personali, passphrase, pin host, account dei destinatari, firewall o policy `ask/full`.

La nuova chiave dedicata non ha una passphrase SSH interattiva. `ssh-keygen` la genera in una directory privata sotto `/run`; `systemd-creds` ne produce l'involucro cifrato e il temporaneo viene rimosso. Il codice Python non legge i byte privati in chiaro, né li restituisce tramite MCP o diagnostica. Questa rimozione non è una garanzia di cancellazione forense. L'eventuale segreto host creato da systemd appartiene al sistema e non viene eliminato dal rollback Vincula.

Il servizio consegna la credenziale tramite `LoadCredentialEncrypted` ed esegue `ssh-agent` come l'account selezionato. Il loader usa `ssh-add` senza terminale o askpass e verifica la firma con il solo pubblico. Ad ogni restart del servizio, il caricamento viene ripetuto.

`LoadCredentialEncrypted` usa il valore letterale `ID:/percorso/assoluto`, con `%` raddoppiati per gli specifier systemd. Il formatter non applica le virgolette o gli escape di argv: il parser conserva le virgolette attorno a questa direttiva. I comandi `Exec*` mantengono il proprio quoting distinto.

**Confine OS:** la copia in chiaro della credenziale resta nel contesto del servizio mentre è attivo, accessibile al relativo utente e a root. Anche il socket può essere usato da quell'account e da root. Non c'è isolamento da codice autorizzato a operare con gli stessi privilegi. La cifratura `host` non protegge da root o da un accesso sufficiente al medesimo sistema di file. Il provider evita la gestione dei segreti nel normale flusso AI/MCP; non promette una barriera contro il proprietario dell'account.

Per consegnare questa copia, systemd può mantenere root come proprietario e concedere una ACL read-only al solo UID del servizio. In quel caso `stat` mostra `0440`: il bit di gruppo rappresenta la maschera ACL, mentre il gruppo non riceve accesso. Il loader legge soltanto i metadati del file e della ACL POSIX Linux e accetta esclusivamente questa forma canonica, con lettura per root/UID selezionato, nessun accesso effettivo di gruppo/altri e nessuna scrittura aggiunta. ACL malformate, sconosciute, default o con altri utenti/gruppi sono rifiutate. In assenza di ACL, anche su filesystem che non le supportano, resta il precedente requisito senza permessi group/other. Nessun permesso della credenziale viene modificato e nessun suo byte viene letto da Python.

Fonti primarie: [credenziali systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials), [systemd-creds](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html), [ssh-agent](https://man.openbsd.org/ssh-agent.1).

### Directory pubbliche e segnalazione Sonar S2612

Il `0755` di `_create_directory` riguarda soltanto directory amministrative nuove: i parent del codice condiviso, delle unità e della configurazione pubblicata. Consente agli account dei servizi di attraversarle e leggere codice, chiave pubblica e ricevuta; non concede scrittura a gruppo o altri. Proprietario e inode sono verificati attraverso un descrittore `O_DIRECTORY|O_NOFOLLOW` prima e dopo il cambio di modalità. Le directory preesistenti non vengono allargate.

| Materiale | Protezione distinta |
| --- | --- |
| Chiave privata temporanea | Directory privata `0700` sotto `/run`, eliminata dopo la cifratura |
| Involucro cifrato `identity.cred` | Root `0600`, impostato prima della pubblicazione e verificato anche dal piano senza lettura del contenuto |
| Pubblico, ricevuta, loader e unità | Root `0644`, nelle directory condivise `0755` |
| Socket dell'account | Directory runtime `0700` gestita da systemd |
| Credenziale decifrata del servizio | Lettura per root/UID selezionato, con i controlli di modalità e ACL descritti sopra |

La possibilità di elencare nomi e metadati pubblici non rende leggibile il file `0600` né consente di sostituirlo. I byte privati non sono pubblicati nei file `0644`. Rendere indistintamente queste directory `0700` impedirebbe al loader non root di avviarsi, in particolare dopo installazione con umask `077`.

La segnalazione S2612 sul `fchmod(0755)` va quindi valutata come falso positivo di contesto per questo uso preciso, mantenendo attiva la regola. Il 25 novembre 2026 la [issue della PR #22](https://sonarcloud.io/project/issues?id=Daniele-Cangi_Vincula&pullRequest=22&issues=AaIS2jqnPN1eE77QDBhs&open=AaIS2jqnPN1eE77QDBhs) è stata classificata in Sonar come **False positive**, con motivazione nella sua cronologia e senza esclusioni o `NOSONAR`. Una nota nel codice o nella documentazione non chiude da sola una issue: la classificazione è una decisione esplicita del progetto. Questa valutazione non si estende a futuri file privati in chiaro pubblicati nello stesso percorso, né isola la credenziale da root o dal proprietario del servizio.

Le regressioni verificano umask `077` e `000`, directory private `0700`, involucro `0600` già prima che sia raggiungibile dal nome pubblico, conservazione dei permessi preesistenti e rifiuto di un involucro con lettura o scrittura aggiuntiva. Le fixture di installazione usano materiale sintetico e non modificano credenziali o servizi reali.

## Pianificare e installare

Sostituire `nomeutente` con l'account locale scelto. Dal checkout verificato:

```bash
python3 -I -B agent/controller_identity.py plan --owner nomeutente --encryption host
python3 -I -B agent/controller_identity.py status --owner nomeutente
```

`plan` e `status` non elevano privilegi, non generano chiavi, non avviano unità e non modificano il registro. Il piano su un'installazione esistente, eseguito senza root, verifica i metadati dell'involucro protetto e dichiara che non ne ha verificato il digest. Durante l'installazione root vengono verificati anche i byte cifrati.

Per `install` usare una copia del modulo posseduta da root, con tutti gli antenati protetti da scrittura degli altri utenti; il normale checkout utente resta sufficiente per `plan/status`. Il setup ripubblica il proprio sorgente come loader OS: anche dopo una verifica iniziale del digest, una copia modificabile durante l'esecuzione non soddisfa questo requisito. Un launcher può leggere i byte del modulo, confrontarli con il digest della release e copiarli in una directory root privata prima di eseguire quei medesimi byte. Non deve eseguire direttamente un percorso utente modificabile. La verifica e la copia del codice pubblico non richiedono la lettura di chiavi private.

Solo dopo aver verificato codice e piano, dal checkout protetto l'operatore esegue localmente:

```bash
sudo python3 -I -B agent/controller_identity.py install --owner nomeutente --encryption host
```

Il comando richiede l'autorizzazione amministrativa locale, non le password degli altri computer. Crea:

| Risorsa | Percorso |
| --- | --- |
| Chiave pubblica | `/etc/vincula/controller/UID/identity.pub` |
| Involucro cifrato, root `0600` | `/etc/vincula/controller/UID/identity.cred` |
| Ricevuta con digest e fingerprint | `/etc/vincula/controller/UID/provider.json` |
| Loader OS, root `0644` | `/usr/local/libexec/vincula/controller-identity.py` |
| Unità di sistema | `vincula-controller-UID.service` |
| Socket nella directory privata dell'account | `/run/vincula-identity-UID/agent.sock` |

Il loader è condiviso tra account e deve corrispondere esattamente al sorgente selezionato. Un lock di installazione comune serializza le pubblicazioni. I file nuovi vengono pubblicati senza sovrascrivere file preesistenti; i descrittori originali rimangono aperti fino al completamento. La procedura verifica la sintassi dell'unità, abilita il servizio persistentemente e controlla processo, socket e firma. Un'installazione ripetuta riusa la medesima identità e non la ruota.

Se la procedura fallisce, pulisce soltanto file nuovi di cui conserva ancora l'identità. Se il servizio potrebbe essere attivo e lo stop non è confermato, oppure un file è stato sostituito, conserva la configurazione per il recupero locale. Un arresto improvviso prima della ricevuta completa richiede ispezione locale: il comando rifiuta di sovrascrivere lo stato parziale o generare un'altra chiave al suo posto. Non esiste ancora un comando di upgrade, rotazione o disinstallazione automatica.

## Autorizzare i destinatari e collegare il registro

L'operatore autorizza una volta il solo pubblico sui destinatari scelti, verificando fingerprint e account. L'installazione del provider non modifica `authorized_keys` e non dà accesso a nuovi nodi automaticamente. L'eventuale revoca richiede la rimozione selettiva del pubblico dai destinatari; fermare il servizio non revoca una chiave già autorizzata.

Dopo enrollment e verifica, nelle route locali del [registro nodi](registro-nodi.md) si impostano i due percorsi pubblici, sostituendo `UID` con l'identificativo numerico dell'account:

```json
{
  "identity_file": "/etc/vincula/controller/UID/identity.pub",
  "identity_agent": "/run/vincula-identity-UID/agent.sock"
}
```

Sono i campi da aggiungere alla route completa con host, account e `known_hosts_file`, non un registro completo. Il file `.pub` seleziona la chiave privata già caricata nell'agente esplicito, con `IdentitiesOnly=yes`; non è richiesta una copia privata nel registro. OpenSSH applica `IdentityAgent` anche se `SSH_AUTH_SOCK` indica il desktop. Vedere [IdentityFile e IdentityAgent](https://man.openbsd.org/ssh_config).

Il socket non è un file statico richiesto per considerare la route configurata. La disponibilità si misura con una firma `ssh-add -T` nel socket scelto e con le successive connessioni autenticate. Se il servizio o la chiave non è disponibile, non si ripiega sull'agente grafico e non compare un popup per caricare la chiave di servizio. Nei registri misti il bootstrap personale continua a gestire soltanto le chiavi legacy che lo richiedono.

L'attuale provider riguarda Linux. Le route Windows esistenti continuano a usare il trasporto precedente; il nuovo campo non abilita un provider Windows non ancora collaudato. `ask/full` e l'opt-in di esecuzione generale rimangono decisioni locali di ciascun destinatario.

## Prove e prossimo collaudo

I test includono pubblicazione e rollback, descriptor pin, sostituzioni, conservazione dell'identità già esistente, errore di stop e assenza di fallback. Le regressioni ACL usano soltanto file non segreti: verificano anche il mask `0440` prodotto dal kernel, il rifiuto di permessi aggiuntivi e la chiusura del descrittore senza lettura dei contenuti. Una prova OpenSSH isolata genera una chiave sintetica tramite gli strumenti OS, carica e verifica la firma, ferma e riavvia il processo originale e ricarica la stessa chiave. Il parser systemd viene esercitato senza avviare servizi. Una regressione legge inoltre le proprietà parse di unità utente temporanee con avvio manuale rifiutato e sorgenti inesistenti: verifica ID e percorso esatti, inclusi spazi, `%`, quote, backslash e `$`, poi rimuove soltanto i file di unità posseduti. Se bus/manager utente o binding OS non sono disponibili, questa prova è dichiarata skipped. Non vengono create o decifrate credenziali. Il setup della credenziale cifrata è simulato nei test di transazione: questi test non certificano la decifratura nel manager systemd reale.

Il collaudo successivo deve verificare installazione OS, enrollment, crash/restart con ricaricamento e un vero agente MCP da ciascuna origine scelta, con desktop/variabile `SSH_AUTH_SOCK` inutilizzabili, log e codice di uscita verificati. La prova al boot senza login grafico rimane distinta. Nessun nuovo riavvio di Kali è autorizzato da questa procedura: serve prima l'approvazione esplicita dell'operatore.
