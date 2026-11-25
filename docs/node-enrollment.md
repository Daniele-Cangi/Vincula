# Aggiungere un computer a Vincula

Il catalogo locale supporta ora inviti pubblici e registrazione con verifica esplicita della chiave host SSH, dalla CLI e dalle due interfacce desktop. Questa prima fase salva identità, rotta e chiave host verificata: il risultato `registered` non dimostra che il computer sia raggiungibile, autenticato o pronto per il desktop. Le verifiche restituite sono `ssh: unchecked`, `runtime: unchecked` e `desktop: unchecked`.

## Identità e invito pubblico

L’esportazione usa l’UUID locale già persistito nel registro selezionato. Non genera un altro UUID. Il computer deve quindi essere già registrato come nodo locale; un’installazione clonata richiede un’identità distinta prima di essere aggiunta come altro computer.

Il formato v1 contiene soltanto questi campi:

| Campo | Contenuto |
| --- | --- |
| `format` | `vincula-node-invitation` |
| `version` | Intero `1` |
| `node` | Oggetto con `id` UUID canonico, `label` e `family` (`linux` oppure `windows`) |
| `endpoints` | Da 1 a 32 oggetti con `host`, `user`, `port` e `network` |
| `ssh_host_key` | Oggetto con `type: ssh-ed25519`, `key` nella codifica base64 SSH e `fingerprint` SHA256 |

La chiave pubblica deve avere esattamente il formato wire SSH Ed25519: tipo e lunghezze verificati, 32 byte pubblici, 51 byte complessivi. L’impronta viene ricalcolata dal contenuto. Campi aggiuntivi, password, token e percorsi di identità nell’invito vengono rifiutati. Il commento di una riga pubblica OpenSSH non viene esportato.

L’operatore confronta l’impronta con quella mostrata direttamente sul computer destinatario tramite un canale già fidato. La sola coerenza tra chiave e impronta nell’invito non autentica il computer. Una scansione della rete non rende automaticamente fidata una chiave.

## Uso headless tramite JSON

`agent/nodes.py` legge una richiesta JSON da stdin e restituisce un risultato JSON. Non richiede MCP né una sessione grafica. Eseguire i comandi dalla radice della repository, usando il registro locale corretto tramite `VINCULA_NODE_REGISTRY`; il registro del Core e quello usato dall’app possono avere percorsi differenti. Non copiare un registro di un’altra origine per selezionarlo.

Su Linux, per esempio:

```bash
export VINCULA_NODE_REGISTRY=/percorso/locale/nodes.json
python3 -S agent/nodes.py < richiesta.json
```

Su Windows PowerShell:

```powershell
$env:VINCULA_NODE_REGISTRY = 'C:\percorso\locale\nodes.json'
Get-Content -Raw .\richiesta.json | py -3 -S .\agent\nodes.py
```

I percorsi e gli indirizzi negli esempi vanno sostituiti con quelli dell’origine e del destinatario. `192.0.2.44` è un indirizzo di documentazione.

### Esportare sul nuovo computer

L’azione `enrollment_export` richiede `endpoints` e `public_host_key`. Quest’ultimo è il contenuto di una sola riga **pubblica** OpenSSH Ed25519, con eventuale commento. Per costruire una richiesta senza leggere chiavi private, su Linux:

```bash
python3 -S - <<'CODE' > richiesta-export.json
import json
from pathlib import Path
print(json.dumps({
    "action": "enrollment_export",
    "endpoints": [{"host": "192.0.2.44", "user": "worker", "port": 22, "network": "Wi-Fi"}],
    "public_host_key": Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().strip()
}))
CODE
python3 -S agent/nodes.py < richiesta-export.json > esportazione.json
```

Su Windows la sorgente pubblica OpenSSH è normalmente `C:\ProgramData\ssh\ssh_host_ed25519_key.pub`; l’API riceve comunque il contenuto, non il percorso. Dal risultato riuscito `{ "ok": true, "invitation": ... }` estrarre il solo oggetto `invitation` in `invito.json` e trasferire quel file pubblico all’origine che deve aggiungere il nodo:

```bash
python3 -S - <<'CODE'
import json
from pathlib import Path
result = json.loads(Path("esportazione.json").read_text())
if not result.get("ok"):
    raise SystemExit(result)
Path("invito.json").write_text(json.dumps(result["invitation"], indent=2) + "\n", encoding="utf-8")
CODE
```

### Validare sull’origine

`enrollment_preview` accetta il testo JSON pubblico in `invitation_text`. La preview rifiuta anche campi JSON duplicati e costanti numeriche non valide; non scrive il registro o i pin e non apre connessioni:

```bash
python3 -S - <<'CODE' > richiesta-preview.json
import json
from pathlib import Path
print(json.dumps({"action": "enrollment_preview", "invitation_text": Path("invito.json").read_text(encoding="utf-8")}))
CODE
python3 -S agent/nodes.py < richiesta-preview.json
```

Il risultato contiene l’invito validato e la sua impronta. Confrontarla sul destinatario prima dell’importazione.

### Importare dopo il confronto

Leggere prima la revisione corrente con la richiesta `{"action":"list"}` e conservarne il risultato in `registro-prima.json`. Poi costruire la richiesta seguente sostituendo l’impronta con il valore confrontato indipendentemente e il percorso con l’identità SSH **dell’origine**:

```bash
python3 -S - <<'CODE' > richiesta-import.json
import json
from pathlib import Path
print(json.dumps({
    "action": "enrollment_import",
    "invitation": json.loads(Path("invito.json").read_text(encoding="utf-8")),
    "expected_revision": json.loads(Path("registro-prima.json").read_text())["revision"],
    "expected_fingerprint": "SHA256:SOSTITUIRE_CON_IMPRONTA_VERIFICATA",
    "endpoint_index": 0,
    "local_connection": {"identity_file": "/percorso/locale/identita-ssh"}
}))
CODE
python3 -S agent/nodes.py < richiesta-import.json
```

`endpoint_index` seleziona un endpoint dell’invito partendo da zero. `local_connection` ammette soltanto `identity_file`, l’eventuale `identity_agent` e l’eventuale `bind_address`. I percorsi devono essere assoluti e propri dell’origine; non sono trasferiti dall’invito. `identity_agent` è attualmente supportato solo per un controller Linux con identità `.pub` e socket locale esplicito. La registrazione non legge il contenuto della chiave privata e non richiede che il collegamento sia già funzionante.

Un successo restituisce lo snapshot del registro con `revision` e `registry`, più `node_id`, `state: registered`, le tre verifiche `unchecked` e `next_steps`. Gli errori CLI distinguono `invalid_request`, `conflict` e `io_error`.

## Conferma nelle due app

Entrambe le interfacce acquisiscono il file pubblico, chiedono al backend la preview e mostrano identità, endpoint e impronta. La conferma esplicita dell’operatore deve avvenire dopo il confronto dell’impronta; i percorsi dell’identità e l’eventuale bind sono inseriti sull’origine. Le app usano le stesse azioni CLI, senza una seconda implementazione del formato di invito. La registrazione non attiva automaticamente mouse, tastiera, appunti o accesso agente.

## Pubblicazione, conflitti e recupero

Il registro resta locale a ogni origine: lo stesso invito può selezionare endpoint e identità differenti da Windows, Ubuntu o un altro nodo. Copiare lo stesso registro tra sistemi non costituisce una sincronizzazione corretta.

L’importazione mantiene lock, revisione attesa e sostituzione atomica del registro. Pubblica prima il pin dedicato in `enrollment-known-hosts/<UUID>.known_hosts`, accanto al registro, usando l’alias SSH `vincula-<UUID>`. Conserva gli altri pin e l’eventuale file Core `known_hosts`; rifiuta link e reparse point, comprese le junction NTFS, per la directory dedicata e il pin.

Una ripetizione identica non riscrive i file e può riuscire anche con una vecchia revisione **solo** se voce registrata e pin coincidono già esattamente. Metadati, rotta o chiave differenti per lo stesso UUID producono conflitto: l’importazione non ruota silenziosamente le chiavi. Un pin mancante su un UUID già registrato richiede un ripristino esplicito, non una nuova fiducia implicita.

Un errore dopo il commit può aver perso soltanto la conferma. Il cleanup non risolve né esplora i percorsi contenuti nelle altre rotte del registro. Se esiste una rotta SSH esplicita o legacy, conserva il pin: un alias o un riferimento implicito potrebbe ancora usarlo. Può quindi restare un pin pubblico verificato orfano anche dopo un errore prima del commit. Un pin orfano identico può essere riutilizzato con impronta verificata e revisione corrente. La rimozione automatica resta limitata ai casi senza rotte e con proprietà e contenuto del pin ancora verificati. Su filesystem che non supportano la pubblicazione atomica tramite hard link, l’operazione restituisce errore prima del salvataggio del registro.

## Passi successivi e permessi

Questo incremento non esegue ancora automaticamente:

- l’autorizzazione della chiave pubblica dell’origine sul destinatario;
- il preflight reale SSH su account, porta, rotta e chiave host fissata;
- la verifica del runtime, della versione, dei protocolli e delle capacità effettivamente disponibili;
- il provisioning e il collaudo desktop, inclusi TLS, sessione grafica, portali, layout e stop locale.

Un destinatario che riceve soltanto lavori non richiede una propria identità SSH in uscita. I permessi del destinatario restano `ask` oppure `full`, con abilitazione separata dell’esecuzione generale. Importare un invito non modifica policy, grant o privilegi dell’account. Il consenso desktop resta distinto dai permessi agente.

I test di registrazione usano fixture con un quarto UUID arbitrario e due origini: coprono rotte differenti, formato pubblico, confronto dell’impronta, CAS, concorrenza, conflitti, retry e guasti durante la pubblicazione. Queste prove non dimostrano che sia stato aggiunto un nuovo PC reale né sostituiscono il collaudo SSH/runtime/desktop. Per i prerequisiti della triade e le fasi ancora aperte vedere il [piano di provisioning desktop](desktop-provisioning.md) e il [piano infrastruttura](piano-infrastruttura-multinodo.md).
