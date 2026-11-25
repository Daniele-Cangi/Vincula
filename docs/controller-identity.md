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

**Confine OS:** la copia in chiaro della credenziale resta nel contesto del servizio mentre è attivo, accessibile al relativo utente e a root. Anche il socket può essere usato da quell'account e da root. Non c'è isolamento da codice autorizzato a operare con gli stessi privilegi. La cifratura `host` non protegge da root o da un accesso sufficiente al medesimo sistema di file. Il provider evita la gestione dei segreti nel normale flusso AI/MCP; non promette una barriera contro il proprietario dell'account.

Fonti primarie: [credenziali systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials), [systemd-creds](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html), [ssh-agent](https://man.openbsd.org/ssh-agent.1).

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

I test includono pubblicazione e rollback, descriptor pin, sostituzioni, conservazione dell'identità già esistente, errore di stop e assenza di fallback. Una prova OpenSSH isolata genera una chiave sintetica tramite gli strumenti OS, carica e verifica la firma, ferma e riavvia il processo originale e ricarica la stessa chiave. Il parser systemd viene esercitato senza avviare servizi. Il setup della credenziale cifrata è simulato nei test di transazione: questi test non certificano la decifratura nel manager systemd reale.

Il collaudo successivo deve verificare installazione OS, enrollment, crash/restart con ricaricamento e un vero agente MCP da ciascuna origine scelta, con desktop/variabile `SSH_AUTH_SOCK` inutilizzabili, log e codice di uscita verificati. La prova al boot senza login grafico rimane distinta. Nessun nuovo riavvio di Kali è autorizzato da questa procedura: serve prima l'approvazione esplicita dell'operatore.
