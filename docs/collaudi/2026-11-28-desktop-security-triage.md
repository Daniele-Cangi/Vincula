# PR #26: triage statico delle segnalazioni automatiche

Riferimento: `d3295364d486327c2be7243dbb934e5f4db8057c`.
I quattro alert sono stati letti tramite l'API GitHub e confrontati con le
catene reali di chiamata. Nessun alert è stato archiviato o modificato.
Questo documento non dichiara verde il Quality Gate Sonar.

Il controllo successivo dell'API pubblica Sonar sul commit `d74b30b` ha
incluso tutti i 69 issue nuovi: 4 vulnerabilità, 1 bug e 64 code smell.
Il gate di quell'analisi era `ERROR`, con Reliability C, Security E e
Maintainability A. La review Codex senza rilievi non sostituisce questo esito.

| Alert | Valutazione nel contratto attuale |
| --- | --- |
| [20](https://github.com/Daniele-Cangi/Vincula/security/code-scanning/20), lettura `desktop_ssh.bounded_file` | Falso positivo: lettura bounded di metadata locali scelti dal chiamante OS. Il bootstrap remoto è costante, il wire non ammette selettori di percorso e risposte/errori sono filtrati. Vedere `agent/desktop_ssh.py:74`, `:154`, `:171`, `:208`, `:333`, `:563`. |
| [22](https://github.com/Daniele-Cangi/Vincula/security/code-scanning/22), `Store.open` | Falso positivo: destinazioni derivate dalla root privata, versione pinned e manifest validato. Pubblicazione con staging UUID esclusivo, controlli di identità e assenza di sovrascrittura. Vedere `agent/desktop_install.py:205`, `:312`, `:337`, `:422`. |
| [23](https://github.com/Daniele-Cangi/Vincula/security/code-scanning/23), `input_descriptor` | Falso positivo: apertura in sola lettura preceduta da validazione di percorsi assoluti/parent reali, inventario e nomi relativi del manifest; identità del descrittore e hash vengono confrontati durante la copia. Vedere `agent/desktop_install.py:61`, `:222`, `:228`, `:316` e `agent/desktop_backend.py:143`. |
| [21](https://github.com/Daniele-Cangi/Vincula/security/code-scanning/21), `deterministic_tar` | Falso positivo: è un writer TAR, senza estrazione né nomi provenienti da membri di archivi ostili. I nomi sono generati da `root.name` e `path.relative_to(root)` dopo validazione delle destinazioni e del manifest. Vedere `tools/build-managed-desktop-linux/build.py:174`, `:209`, `:236`, `:249`. |

Il confine è quello di CLI owner-local, eseguiti con i permessi dell'account
autenticato. Questi helper non sono endpoint privilegiati che accettano percorsi
arbitrari da un tool MCP. Un futuro cambiamento di questo confine richiede un
nuovo esame; la conclusione non è trasferibile a un servizio elevato generico.

Copertura pertinente già esistente: campi wire e risposte riservate nei test
`test_desktop_ssh.py`; pin esterno, traversal/alias/link, rollback e sostituzione
degli inode in `test_desktop_install.py` e `test_desktop_backend.py`; inventario e
hash nel QA del pacchetto Linux. Il triage non ha rilanciato queste suite e non
esiste un test unitario dedicato ai nomi TAR. La valutazione qui è statica.

## Rilievi aggiuntivi e correzioni

Il bug S7497 in `desktop_app.run` era riproducibile: cancellando il task
dall'esterno, il cleanup terminava ma il chiamante riceveva un ritorno normale.
Gli Stop richiesti dall'owner ora usano un'eccezione privata; la vera
`CancelledError` attraversa il `finally` dopo lo stop dei nodi e la chiusura
del trasporto. Due regressioni sintetiche coprono la cancellazione durante
inspect e heartbeat, oltre alla prova già esistente dello Stop con Event.
Non vengono aperti SSH o provider desktop da queste fixture.

La regressione completa ha inoltre esposto due fixture di installazione che
leggevano lo stato del supervisor reale quando una condivisione era attiva.
L'installazione veniva rifiutata prima della pubblicazione. Le fixture ora
usano anche un controllo locale temporaneo e una guardia contro RPC reali;
un endpoint sintetico attivo verifica separatamente il rifiuto senza modifiche
ai file o all'autostart.

S8786 riguarda il parsing del risultato `readelf` nel QA del pacchetto Linux,
non il trasporto desktop. La regex è sostituita da un parser per riga con
`partition`, che rifiuta tag di ricerca privi delle parentesi del valore.
Le fixture coprono RPATH/RUNPATH, output troncato e una lunga riga estranea;
il QA aggiornato passa anche sui pacchetti reali Ubuntu e Kali: 239 file,
118 ELF e 111 SONAME, con provisioning privato headless e riuso rifiutato.

Tre code smell sono contestuali: i due callback GTK S3516 devono ritornare
`False` per rimuovere la sorgente GLib; il corpo S108 drena intenzionalmente
stderr senza pubblicarne contenuti. Le altre 60 note riguardano complessità,
costanti duplicate, refactor e struttura dei test. Restano debito di
manutenzione: questo esame non ha dimostrato ulteriori bug funzionali e non
le archivia come falsi positivi.

La nuova analisi Sonar sul commit `986a665` è completata il 28 novembre alle
07:56 UTC: zero bug, Reliability A, Maintainability A e 63 code smell.
Il gate resta `ERROR` per Security E e le quattro vulnerabilità già esaminate.
Gli stati remoti degli issue non sono stati modificati. La matrice funzionale
Windows/Linux Python 3.10/3.14 passa nel
[run 46123446463](https://github.com/Daniele-Cangi/Vincula/actions/runs/46123446463);
la build nativa Windows passa nel
[run 46123446459](https://github.com/Daniele-Cangi/Vincula/actions/runs/46123446459),
con sei suite CTest riuscite. La review Codex dello stesso commit è conclusa
senza rilievi; lo stato della security review Codex è separato e non risulta
eseguita in questo ciclo.
