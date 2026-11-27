# PR #26: triage statico delle segnalazioni automatiche

Riferimento: `d3295364d486327c2be7243dbb934e5f4db8057c`.
I quattro alert sono stati letti tramite l'API GitHub e confrontati con le
catene reali di chiamata. Nessun alert è stato archiviato o modificato.
Questo documento non dichiara verde il Quality Gate Sonar.

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
