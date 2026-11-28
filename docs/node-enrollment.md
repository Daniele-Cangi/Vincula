# Aggiungere un computer a Vincula

Il catalogo locale supporta ora inviti pubblici e registrazione con verifica esplicita della chiave host SSH, dalla CLI e dalle due interfacce desktop. L’importazione salva identità, rotta e chiave host verificata: il risultato `registered` non dimostra che il computer sia raggiungibile, autenticato o pronto per il desktop. Le verifiche restituite dall’importazione sono `ssh: unchecked`, `runtime: unchecked` e `desktop: unchecked`. L’azione separata `enrollment_check` osserva poi connessione autenticata, identità persistente, helper installati e policy, senza dichiarare pronto il worker o il desktop.

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

### Verificare il nodo registrato

L’azione `enrollment_check` accetta esattamente `action`, `node_id` e `expected_revision`. L’UUID deve essere canonico e registrato; gli alias non sono ammessi. La revisione è obbligatoria e deve essere la stringa SHA256 di 64 caratteri esadecimali restituita da una lettura corrente `{"action":"list"}`. Nell’esempio seguente UUID e revisione sono illustrativi e vanno sostituiti:

```json
{
  "action": "enrollment_check",
  "node_id": "2b52a018-2e4d-441d-af33-f963af87ad64",
  "expected_revision": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

Salvare la richiesta in `richiesta-verifica.json` ed eseguirla con lo stesso registro selezionato in precedenza:

```bash
python3 -S agent/nodes.py < richiesta-verifica.json
```

Il risultato contiene `ok`, `revision` e `registry`, più l’oggetto `node_check`:

| Campo di `node_check` | Significato |
| --- | --- |
| `node_id`, `origin_id` | UUID del nodo verificato e dell’origine del registro |
| `revision` | Revisione locale alla quale si riferisce l’osservazione |
| `route_digest` | SHA256 della voce locale e dell’osservazione del pin |
| `host_pin_sha256` | SHA256 dell’intero file pubblico known_hosts osservato, oppure `null` se non applicabile o non leggibile |
| `checked_at` | Data e ora UTC in formato ISO 8601 |
| `elapsed_seconds` | Durata complessiva osservata, incluse le attese locali |
| `checks` | Oggetti separati `ssh`, `identity`, `helper`, `policy`, `worker` e `desktop` |

`host_pin_sha256` è il digest del file locale, distinto dall’impronta OpenSSH `SHA256:...` della singola chiave host confrontata durante l’importazione.

| Verifica | Esiti e limiti |
| --- | --- |
| `ssh` | `authenticated` dopo risposta valida con nonce sulla rotta OpenSSH configurata, con pin rigoroso, autenticazione pubblica e forwarding disattivato. Errori di connessione o risposta producono `failed` con motivo sanitizzato. |
| `identity` | `match`, `mismatch`, `ambiguous`, `unavailable` oppure `unchecked`. Sul destinatario confronta gli UUID locali dei due cataloghi previsti Core/Desktop; cataloghi discordanti o parzialmente illeggibili non ricevono una priorità silenziosa. |
| `helper` | `compatible_on_disk` con `protocol_version` solo quando tutti i file helper richiesti coincidono byte per byte con le sorgenti del runtime locale fidato. File differenti producono `unverified`; tutti assenti o illeggibili producono `missing`. Prima della sonda può restare `unchecked`. |
| `policy` | `observed` con `mode: ask|full` e `native_enabled` booleano solo con helper verificati e database esistente valido. Negli altri casi resta `unchecked` con motivo. |
| `worker` | Sempre `unverified`: il confronto dei file non certifica il codice di un processo già avviato né un servizio operativo. |
| `desktop` | Sempre `unchecked`: non esegue verifiche di sessione grafica o input condiviso. |

`ok: true` significa che la richiesta di osservazione è stata gestita: anche un collegamento fallito viene descritto nei singoli `checks`. Non è un indicatore globale di disponibilità del computer.

Per il nodo locale, `ssh` resta `unchecked` con motivo `local_node`; la sonda locale osserva realmente cataloghi, helper e policy e include anche il catalogo selezionato dalla configurazione. Quel percorso locale non viene inviato a un peer. Una voce `legacy_route` restituisce `ssh: unchecked` con motivo `legacy_route_requires_explicit`: la verifica non tratta la rotta fittizia usata dal validatore come un indirizzo eseguibile. Anche rotta esplicita, pin o identità locale mancanti mantengono la verifica SSH `unchecked` con un motivo distinto.

La sonda usa direttamente `python3 -IBS -` su Linux o `py.exe -3 -IBS -` su Windows, con sorgente fissa inviata su stdin. Non passa da ConsoleHost, non importa gli helper del destinatario e non chiama il dispatcher dei lavori. La policy viene letta con SQLite `mode=ro`, `query_only` e budget di un secondo, senza inizializzazione, migrazione o modifiche di modalità. Questa è sola lettura logica: SQLite può usare i propri file ausiliari di memoria condivisa in un database WAL già esistente.

La raccolta applica limiti effettivi di 16 KiB alla sorgente inviata, 16 KiB alla risposta e 4 KiB alla diagnostica. Il budget di raccolta e cleanup è di 12 secondi e include una riserva per terminare e raccogliere esclusivamente il processo `Popen` avviato dalla verifica. Una terminazione non confermata produce `termination_unconfirmed`, mai un successo. Il budget non comprende le attese dei mutex del registro e non è una garanzia temporale assoluta sulle chiamate di creazione o terminazione del sistema operativo. La diagnostica remota non viene riportata negli errori pubblici.

Gli snapshot iniziale e finale usano soltanto brevi sezioni di sincronizzazione; nessun mutex viene mantenuto durante la rete. La verifica non crea un registro o un lock file se mancano. Una revisione differente, un nodo rimosso, una rotta modificata o il contenuto del pin cambiato durante la sonda producono `conflict`, anche se il solo file known_hosts cambia senza aggiornare il JSON. In tal caso rileggere il registro e ripetere la richiesta.

L’origine deve eseguire il runtime Vincula completo, inclusi `node_probe.py` e i moduli adiacenti richiesti: copiare solo `nodes.py` non basta. La sonda necessita di OpenSSH e dell’interprete Python con stdlib SQLite sul destinatario. Il confronto degli helper usa `jobs.py`, `vincula_files.py`, `vincula_capabilities.py`, `plan_journal.py`, `wire_framing.py`, `direct_execution.py`, `resource_policy.py`, `worker_resources.py` ed `execution_plans.py`; per leggere la policy serve anche il database già inizializzato dall’operatore. File mancanti o differenti non vengono installati, sostituiti o aggiornati dalla verifica.

## Conferma nelle due app

Entrambe le interfacce acquisiscono il file pubblico, chiedono al backend la preview e mostrano identità, endpoint e impronta. La conferma esplicita dell’operatore deve avvenire dopo il confronto dell’impronta; i percorsi dell’identità e l’eventuale bind sono inseriti sull’origine. Le app usano le stesse azioni CLI, senza una seconda implementazione del formato di invito. La registrazione non attiva automaticamente mouse, tastiera, appunti o accesso agente.

## Pubblicazione, conflitti e recupero

Il registro resta locale a ogni origine: lo stesso invito può selezionare endpoint e identità differenti da Windows, Ubuntu o un altro nodo. Copiare lo stesso registro tra sistemi non costituisce una sincronizzazione corretta.

L’importazione mantiene lock, revisione attesa e sostituzione atomica del registro. Pubblica prima il pin dedicato in `enrollment-known-hosts/<UUID>.known_hosts`, accanto al registro, usando l’alias SSH `vincula-<UUID>`. Conserva gli altri pin e l’eventuale file Core `known_hosts`; rifiuta link e reparse point, comprese le junction NTFS, per la directory dedicata e il pin.

Una ripetizione identica non riscrive i file e può riuscire anche con una vecchia revisione **solo** se voce registrata e pin coincidono già esattamente. Metadati, rotta o chiave differenti per lo stesso UUID producono conflitto: l’importazione non ruota silenziosamente le chiavi. Un pin mancante su un UUID già registrato richiede un ripristino esplicito, non una nuova fiducia implicita.

Un errore dopo il commit può aver perso soltanto la conferma. Il cleanup non risolve né esplora i percorsi contenuti nelle altre rotte del registro. Se esiste una rotta SSH esplicita o legacy, conserva il pin: un alias o un riferimento implicito potrebbe ancora usarlo. Può quindi restare un pin pubblico verificato orfano anche dopo un errore prima del commit. Un pin orfano identico può essere riutilizzato con impronta verificata e revisione corrente. La rimozione automatica resta limitata ai casi senza rotte e con proprietà e contenuto del pin ancora verificati. Su filesystem che non supportano la pubblicazione atomica tramite hard link, l’operazione restituisce errore prima del salvataggio del registro.

## Passi successivi e permessi

La verifica autenticata di connessione, UUID e helper su disco è disponibile con `enrollment_check`. Restano separati e non vengono eseguiti automaticamente:

- l’autorizzazione della chiave pubblica dell’origine sul destinatario;
- l’installazione o l’aggiornamento del runtime e degli helper del destinatario;
- l’attestazione del worker vivo e il collaudo di un lavoro autorizzato con risultato confermato;
- la verifica operativa delle singole capacità e dei programmi disponibili;
- il provisioning e il collaudo desktop, inclusi TLS, sessione grafica, portali, layout e stop locale.

Un destinatario che riceve soltanto lavori non richiede una propria identità SSH in uscita. I permessi del destinatario restano `ask` oppure `full`, con abilitazione separata dell’esecuzione generale. Importare un invito non modifica policy, grant o privilegi dell’account. Il consenso desktop resta distinto dai permessi agente.

I test di registrazione usano fixture con un quarto UUID arbitrario e due origini: coprono rotte differenti, formato pubblico, confronto dell’impronta, CAS, concorrenza, conflitti, retry e guasti durante la pubblicazione. I test della verifica aggiungono processi locali reali, timeout e limiti di cattura, nonce e schema non validi, policy in sola lettura e conflitti durante la rete, inclusa la rotazione del pin. Queste prove non dimostrano che sia stato aggiunto un nuovo PC reale né sostituiscono il collaudo di un worker operativo o del desktop. Per i prerequisiti della triade e le fasi ancora aperte vedere il [piano di provisioning desktop](desktop-provisioning.md) e il [piano infrastruttura](piano-infrastruttura-multinodo.md).
