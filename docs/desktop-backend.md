# Contratti e anteprime del backend desktop

Questo incremento aggiunge un manifest comune, la verifica del manifest di un artefatto e un compiler puro dei profili Deskflow. Produce dati in memoria: non scrive profili, apre connessioni, avvia programmi o chiede autorizzazioni grafiche.

`launchable` e `desktop_ready` restano **sempre `false`**, anche quando tutti i prerequisiti dichiarati sono soddisfatti. Il motore gestito e il collegamento a guardian, supervisor, coordinator e SSH devono ancora essere implementati. Le app e la condivisione legacy non usano questi moduli.

## Manifest comune del gruppo

`agent/desktop_backend.py` espone `Screen` e `GroupManifest`, oggetti immutabili. Il documento v1 contiene:

- UUID della sessione e del principale;
- da due a 64 UUID con celle univoche di una griglia connessa;
- scelta esplicita degli appunti;
- un pin TLS SHA-256 distinto per ogni UUID;
- la mappa delle revisioni locali dei registri.

I membri, i pin e le revisioni sono ordinati prima della serializzazione canonica. L’ordine degli input non cambia il digest; cambiare layout, principale, appunti o revisione lo cambia. Il digest è comune ai nodi e non coincide con la revisione di un singolo registro.

Nel manifest non entrano percorsi locali, indirizzi di credenziali, argomenti di programmi o contenuti di certificati. Un quarto UUID usa lo stesso formato dei primi tre.

`GroupManifest.from_document()` richiede lo schema esatto e rifiuta campi extra, UUID non canonici, coordinate booleane, celle duplicate, griglie disconnesse e mappe incomplete. Il chiamante che legge JSON deve anche rifiutare chiavi duplicate prima di costruire il dizionario; questo metodo riceve già un oggetto Python.

## Provenienza degli artefatti

`ArtifactManifest` descrive backend, repository/commit upstream, versione, digest della patch, piattaforma/architettura, eseguibile relativo, hash dei file e capability. La verifica usa:

```python
verified = verify_artifact_manifest(
    manifest_bytes,
    approved_sha256=approved_release_manifest_sha256,
)
```

Il pin `approved_release_manifest_sha256` deve provenire da una ricevuta di installazione o da un’altra fonte approvata dal proprietario, separata dal manifest ricevuto. Calcolare il pin dagli stessi byte non attendibili non costituisce approvazione. `VerifiedArtifact` verifica coerenza e integrità dei dati, non rappresenta da solo un’autorizzazione ad eseguire software.

Il JSON v1 è canonico: chiavi ordinate, separatori compatti, escape ASCII, senza spazi aggiuntivi o newline finale. `ArtifactManifest.to_document()` e `canonical_json()` producono il formato ammesso. Le chiavi duplicate, gli extra, gli hash diversi, i file con traversal o selettori ambigui sono rifiutati. Su Windows vengono rifiutati anche alias per differenze di maiuscole, componenti riservati e nomi con punto/spazio finale. Un file non può essere contemporaneamente la directory di un altro file.

La verifica non apre l’artefatto o i file dichiarati. Nel preflight, gli hash realmente misurati devono essere forniti separatamente dal destinatario autorizzato. La presenza di capability nel manifest non sostituisce il pin esterno né l’attestazione del motore.

## Profili Deskflow in memoria

`agent/desktop_deskflow.py` implementa `compile_profile(group, node_id, artifact, paths, network)`. Il compiler ammette soltanto Deskflow 1.27.0 dal commit upstream `081f6478e654a112168879e67a41ce7d7e4ae3af`. `LocalPaths` contiene esclusivamente riferimenti del destinatario: root dell’artefatto, root separata del profilo e riferimento al certificato locale. Nessuna chiave privata viene letta o copiata.

`NetworkProfile` richiede indirizzi IPv4 letterali e una porta intera. Il principale specifica il bind; ogni client specifica un indirizzo sorgente concreto e un solo indirizzo del principale. Non ci sono fallback a DNS, liste di server o altre interfacce. Un bind `0.0.0.0` del principale richiede comunque una successiva verifica delle regole di rete effettive.

Il risultato `PreparedProfile` contiene:

| Dato | Principale | Client |
| --- | --- | --- |
| Argv | `(<core assoluto>, "server", "--settings", <Deskflow.conf assoluto>)` | `(<core assoluto>, "client", "--settings", <Deskflow.conf assoluto>)` |
| `Deskflow.conf` | Nome UUID, bind/porta, TLS/peer check, config esterna, protocollo Barrier e appunti espliciti | Nome UUID, bind/porta, TLS/peer check, unico server, protocollo Barrier e appunti espliciti |
| `layout.conf` | Griglia completa con nomi `vincula-<UUID>` e collegamenti reciproci | Assente |
| `tls/trusted-clients` | Solo i pin degli altri membri | Vuoto |
| `tls/trusted-servers` | Vuoto | Solo il pin del principale |

Le righe pubbliche di fiducia hanno formato `v2:sha256:<64hex>`. Gli appunti disabilitati producono `enableClipboard=false` e `clipboardSize=0` in entrambi i ruoli; quando richiesti, il limite del profilo v1 è 3 MiB. Comandi di entrata/uscita e log su file sono disabilitati. Le stringhe INI sono quotate ed escaped; l’argv resta una tupla senza shell.

Il compiler distingue i percorsi della piattaforma destinataria indipendentemente dal sistema del chiamante. Richiede percorsi assoluti locali, rifiuta traversal esplicito, controlli e sintassi Windows UNC/device/ADS/espansioni; il profilo non può sovrapporsi alla root immutabile dell’artefatto. Il rifiuto conservativo dei componenti Windows comprende i nomi di dispositivo con spazi prima dell’estensione, come `NUL .txt` e `COM1 .session`, oltre a `CONIN$` e `CONOUT$`.

Queste anteprime non rendono sicuro l’avvio del binario stock con `--settings`: il sorgente upstream inizializza anche impostazioni/state predefiniti e, su Windows, può usare percorsi globali per la fiducia. Il managed mode dovrà scegliere settings/state/TLS privati prima della prima istanza `Settings`, isolare IPC e applicare la scelta clipboard prima di qualsiasi richiesta al portale.

## Preflight e significato degli esiti

`preflight(..., approved_artifact_sha256=...)` riceve `PreflightFacts` espliciti, riferiti al medesimo UUID, piattaforma e contesto locale: `artifact_root`, `profile_root` e `certificate_reference` sono obbligatori e vengono confrontati con la richiesta usando i percorsi della piattaforma destinataria. Fatti relativi a un profilo o riferimento TLS diverso sono rifiutati, anche se l’artefatto coincide. Questi riferimenti rimangono locali e non entrano nel manifest comune. Il preflight non li rileva dal sistema e non si fida dei fatti dichiarati dall’artefatto stesso.

I controlli coprono pin esterno, sorgente/versione, patch e capability richieste, piattaforma/architettura, mappa degli hash misurati, eseguibile, profilo privato, pin del certificato locale, accesso privato al riferimento TLS, sessione interattiva, backend grafico, rete autorizzata e consenso del proprietario. Il consenso agli appunti viene richiesto soltanto quando il gruppo li abilita.

Gli stati sono:

- `ok`: il fatto fornito soddisfa quel prerequisito;
- `missing`: il fatto è sconosciuto o non fornito;
- `unsupported`: il fatto noto non soddisfa la configurazione, incluso un consenso negato.

`configuration_valid` significa che i prerequisiti dichiarati passano. Non prova che i fatti siano stati misurati da un’origine attendibile, che un portale abbia concesso dispositivi o che un peer sia collegato. L’integrazione futura dovrà acquisire i fatti dal processo/supervisor posseduto e verificarne provenienza e freschezza.

Un artefatto stock resta `unsupported` per `managed_engine`, anche se tutti i file e le altre proprietà coincidono. Un principale Wayland con appunti continui resta `unsupported`. InputCapture, RemoteDesktop, EIS, hook Windows, TLS reciproco e identità della sessione richiedono ancora attestazioni effettive. I livelli agente `ask` e `full` non vengono modificati e non sostituiscono il consenso desktop.

## Verifica e limiti

I test puri sono eseguibili dalla directory `agent` con:

```text
python3 -B -W error -m unittest test_desktop_backend test_desktop_deskflow
```

Coprono digest comune, quarto UUID, schema chiuso e limiti, pin esterno, JSON ambiguo, percorsi cross-platform, profili server/client/appunti, fiducia esatta, dati mancanti e negativi. Controllano che compiler e preflight non richiedano accessi a file, rete o processi e che nessun esito renda il desktop avviabile o pronto.

Le anteprime INI sono verificate come dati generati; non è stato eseguito un round trip con un runtime Qt/QSettings o un motore Deskflow. Tale verifica, insieme alla patch di isolamento, al canale privato di attestazione e ai collaudi sui desktop reali, resta necessaria prima di scrivere o usare questi profili. Non viene distribuito o attivato un binario gestito con questo incremento.

## Fonti fissate

Il compiler usa soltanto opzioni e chiavi presenti nel sorgente upstream fissato:

- [CoreArgs e parser](https://github.com/deskflow/deskflow/tree/081f6478e654a112168879e67a41ce7d7e4ae3af/src/apps/deskflow-core): ruolo posizionale e `--settings`.
- [Settings](https://github.com/deskflow/deskflow/blob/081f6478e654a112168879e67a41ce7d7e4ae3af/src/lib/common/Settings.h), [default e selezione dei percorsi](https://github.com/deskflow/deskflow/blob/081f6478e654a112168879e67a41ce7d7e4ae3af/src/lib/common/Settings.cpp): chiavi INI, limite clipboard e necessità dell’isolamento iniziale.
- [ClientApp](https://github.com/deskflow/deskflow/blob/081f6478e654a112168879e67a41ce7d7e4ae3af/src/lib/deskflow/ClientApp.cpp) e [ServerApp](https://github.com/deskflow/deskflow/blob/081f6478e654a112168879e67a41ce7d7e4ae3af/src/lib/deskflow/ServerApp.cpp): endpoint, bind e TLS.
- [Fingerprint](https://github.com/deskflow/deskflow/blob/081f6478e654a112168879e67a41ce7d7e4ae3af/src/lib/net/Fingerprint.cpp): formato delle righe pubbliche di fiducia.

I moduli Python originali mantengono `GPL-3.0-or-later`. Deskflow resta un componente separato con la licenza e l’eccezione OpenSSL presenti nei suoi sorgenti.
