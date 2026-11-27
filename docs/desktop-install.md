# Installare un artifact desktop approvato

`agent/desktop_install.py` installa file locali del motore opzionale nella root privata dell’account. Non esegue il motore, helper, servizi, SSH o comandi amministrativi; non cambia catalogo, layout, profili, certificati, policy Core o firewall. L’installazione dei servizi supervisor e dei helper è un passaggio distinto. Un’installazione riuscita continua a restituire `desktop_ready=false`, `engine_started=false` e `service_installed=false`.

La root è `~/.local/share/vincula-desktop` su Linux oppure `%LOCALAPPDATA%\Vincula-Desktop` su Windows. Non esiste un override CLI della destinazione. Il parent standard deve già esistere e superare i controlli di integrità; l’installer non ne cambia permessi o ACL. L’eventuale `VINCULA_DESKTOP_ARTIFACT_ROOT` deve coincidere con la sottodirectory `engines/approved` di questa installazione.

## Input e comandi

Il pacchetto è una directory già estratta, non un archivio o un installer da eseguire. Il manifest può trovarsi nella directory oppure essere un file separato. Ogni file del pacchetto, incluse dipendenze e testi di licenza distribuiti, deve essere dichiarato in `ArtifactManifest.files`, salvo il manifest stesso. Non vengono copiati file extra. Tutti i byte dei file dichiarati vengono verificati; non viene eseguita una risoluzione dinamica delle librerie del sistema.

Il digest approvato deve provenire dall’operatore o da una release già verificata, separatamente dai byte del pacchetto. Calcolarlo da un download non verificato e passarlo come approvazione non stabilisce fiducia. Per una directory e un manifest con percorsi assoluti:

```sh
python agent/desktop_install.py plan \
  --package /percorso/pacchetto \
  --manifest /percorso/manifest.json \
  --approved-sha256 DIGEST_CANONICO_APPROVATO \
  --enable --port 24807
```

`plan` non crea root, lock, directory o file. Indica cambiamenti, versioni già presenti, digest, dimensione totale e deployment proposto. Sostituire `plan` con `install` applica lo stesso piano. `--enable` è obbligatorio e seleziona esplicitamente `enabled=true`; la porta predefinita è 24807, con interi ammessi tra 1024 e 65535. Su Windows usare l’interprete dell’account con gli stessi argomenti e percorsi locali Windows. L’API `Installer(package, manifest_file, approved_sha256, enabled=True, port=24807)` espone gli stessi metodi `plan()` e `install()` alle interfacce locali.

## Verifica prima della pubblicazione

Il manifest deve avere il formato canonico e il pin esterno di `desktop_backend.verify_artifact_manifest`. Sono accettati soltanto Deskflow 1.27.0, repository e commit definiti in `desktop_deskflow`, patch SHA256 `b7c51d981fda6aee2e4e0bc7f05a4fb7de9281e62fa0d98093ad72d1261dd7dc` e le quattro capability gestite richieste. Il chiamante non può sostituire questi pin. Restano separate le licenze del [motore e della patch](../third_party/deskflow/README.md) e quelle del codice originale Vincula.

Piattaforma e architettura devono corrispondere al sistema chiamante; l’header ELF64 little-endian oppure PE deve dichiarare la stessa architettura x86_64/aarch64. Il controllo PE legge al massimo 1 MiB di header. Questo non certifica ABI, librerie OS, backend grafico, TLS, portali o funzionalità del programma.

Sono verificati tutti gli SHA256 dichiarati, al massimo 512 file, 512 MiB per file e 2 GiB totali. Sono rifiutati attraversamento dei parent, symlink/reparse point, hardlink, nomi ambigui o alias di maiuscole e minuscole, componenti Windows riservati e file/directory non dichiarati. Le aperture confrontano il file effettivamente aperto con l’identità osservata; la copia misura nuovamente i byte. L’inventario installato viene verificato prima dell’approvazione.

## Root privata e pubblicazione

Linux usa directory 0700, metadata/dipendenze 0600 e motore 0700, appartenenti all’account. Un parent non privato può essere leggibile, ma deve appartenere all’account o root e non essere scrivibile da altri. Oggetti preesistenti più esposti sono rifiutati; nessun chmod correttivo viene applicato.

Windows riusa le verifiche SID di `desktop_security.WindowsSecurity`. Solo gli oggetti nuovi ricevono un descrittore con SID numerici dell’utente, SYSTEM e Administrators, DACL protetta e label Medium con no-write-up. Il descrittore dei nuovi oggetti non contiene il SID OWNER RIGHTS. La creazione riceve direttamente i SecurityAttributes; non scrive prima dati pubblici per correggerne poi l’ACL. Il parent Windows deve avere owner fidato e DACL presente. Le ACE efficaci di altri SID possono consentire lettura, attraversamento e sincronizzazione; vengono rifiutati write/append, modifica di attributi, delete/delete-child, WRITE_DAC, WRITE_OWNER e generic-write/all. Le ACE INHERIT_ONLY non controllano il parent e vengono ignorate in questo solo controllo. Sul parent, OWNER RIGHTS (S-1-3-4) rappresenta il proprietario attuale: viene trattato come fidato soltanto dopo aver verificato che tale owner sia già uno dei SID ammessi. Un owner estraneo rimane rifiutato, anche con questa ACE. È il caso della TemporaryDirectory di Python 3.14 su Windows, riprodotto su fixture isolata. La verifica della root privata non ammette questo alias e non cambia. [Microsoft: significato di OWNER RIGHTS](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-security-identifiers). ACE efficaci di tipo non gestito richiedono ispezione. La nuova root rimane invece privata e con DACL protetta: non eredita queste concessioni del parent. Non vengono cambiate ACL preesistenti. [Microsoft: regole di ereditarietà ACE](https://learn.microsoft.com/en-us/windows/win32/secauthz/ace-inheritance-rules). Medium permette l’accesso agli strumenti dello stesso account avviati normalmente e con token elevato, mantenendo i controlli del sistema. [Microsoft: integrity control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control), [security information](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-information).

Un lock esclusivo serializza gli installer. Ogni file viene scritto e sincronizzato in uno staging privato O_EXCL nella directory di destinazione, verificato e pubblicato senza sovrascrittura tramite hardlink sullo stesso filesystem; lo staging viene rimosso prima della verifica finale. Il filesystem deve supportare hardlink: non viene introdotto un fallback che sovrascrive file esistenti. I descrittori originali restano aperti fino alla conclusione per impedire il riuso dell’inode durante il cleanup.

La struttura è:

```text
Vincula-Desktop/
  .install.lock
  engines/
    MANIFEST_SHA256/
      artifact-manifest.json
      files/ ... file dichiarati ...
    approved/
      ENGINE_SHA256.json
  deployment.json
```

La root Linux ha il nome minuscolo indicato sopra. Il namespace della versione è basato sul digest, senza sovrascritture; non è una promessa di immutabilità del filesystem contro codice che possiede già lo stesso account.

La ricevuta in `engines/approved` contiene esattamente `protocol:1`, `binary_sha256`, `executable` assoluto e `artifact_manifest_sha256`, come richiede `desktop_managed.artifact_approval`. `deployment.json` contiene esattamente `protocol:1`, `artifact_root`, `artifact_manifest_file`, `artifact_manifest_sha256`, `port` ed `enabled:true`, come richiede `desktop_node.deployment`. Il deployment viene pubblicato per ultimo, dopo i controlli fallibili sul pacchetto completo.

## Ripetizione, errori e limiti

Una ripetizione identica verifica manifest, file, dipendenze, privacy e ricevute, senza sostituirli. Metadata diversi, versione manomessa o deployment incompleto già presente producono un conflitto; non vengono riparati o sovrascritti. Questo incremento non comprende la sostituzione di un deployment attivo con un’altra release: serve un aggiornamento separato con confronto dello stato atteso.

Prima della pubblicazione del deployment, gli errori tentano di rimuovere soltanto file nuovi con identità e digest ancora corrispondenti e directory nuove ancora vuote. Non usano cancellazioni ricorsive. File sostituiti, cambiati, incompleti o rimasti dopo un crash vengono conservati per ispezione; non diventano un deployment pronto. Root e lock privati possono rimanere. Un crash con versione e approvazione complete ma senza deployment può essere ripreso dalla stessa richiesta; staging o versioni incomplete richiedono ispezione locale.

Dopo che `deployment.json` è pubblicato, un errore di durabilità o ricevuta richiede lettura e riconciliazione: non viene cancellata automaticamente una versione che un altro componente potrebbe aver già consumato. Nessuna receipt mancante autorizza un retry cieco. Il lock è cooperativo e le directory private escludono altri account; il codice dello stesso account e gli amministratori conservano i propri privilegi OS.

I test di `agent/test_desktop_install.py` usano soltanto file e binari sintetici non eseguiti. Coprono lettori reali di deployment/approvazione, tutti i file/dipendenze, consenso e pin, limiti, alias/link, privacy, conservazione dei file esistenti, lock, rollback pre-commit, sostituzione di inode e conservazione post-commit. Il flusso Windows è verificato con mock su Linux e gli stessi test sono predisposti per le API native su Windows. Il controllo del parent è stato verificato in sola lettura sul Windows reale: due ACE di sola lettura/attraversamento producevano il rifiuto del precedente controllo privato; il controllo di integrità le ammette, mentre quello della root privata continua a rifiutarle. Nessuna ACL locale o installazione del motore è stata modificata dall’autore di questa verifica.
