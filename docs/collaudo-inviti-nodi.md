# Collaudo degli inviti per aggiungere nodi

Il 25 novembre 2026 il primo incremento di enrollment è stato verificato su Ubuntu e Windows con registri temporanei. Un quarto UUID sintetico è stato aggiunto da due origini senza cambiare codice, con endpoint e identità locali differenti. Questo collaudo non ha aggiunto un quarto computer reale né modificato registri, chiavi autorizzate, policy o condivisioni operative.

Il parent è la PR #22, `b4b3a8c6688cca947a673c8f9545ab1c1aa38eb2`. L'incremento aggiunge inviti pubblici v1, le tre azioni CLI e i moduli nelle due app. Mantiene le funzioni manuali precedenti e i due livelli `ask/full`. I digest dei sorgenti e le ricevute dei lavori nativi sono nel [riepilogo del collaudo](collaudi/2026-11-25-enrollment.json).

| Verifica | Esito e limiti |
| --- | --- |
| Runtime su snapshot pulita | 354 test, 15 esclusioni previste; nessun errore, deprecazioni e resource warning fatali. Due fixture benchmark e smoke MCP isolato passati. |
| Backend e CLI su Windows reale | Python 3.14.7, 45 test, 4 esclusioni: tre richiedono privilegi per symlink e una riguarda soltanto il socket Linux. Junction NTFS, nomi brevi, mutex abbandonato e round trip con TEMP/TMP 8.3 verificati. |
| Build WinForms | Compilazione nativa del sorgente finale; nessun avvio dell'app operativa. |
| Moduli GTK | Proprietario nascosto e soli widget offscreen; gate dell'impronta, endpoint, identità e socket locali, import `registered/unchecked`, export validato e screenshot. Nessun accesso al contenuto dell'identità. |
| Moduli Windows | Proprietario `DockWindow(false)` nascosto, senza evento Shown/status o condivisione; dialoghi trasparenti fuori schermo, import reale nella fixture, endpoint e identità locali, stato non verificato. Export validato dal runtime; il selettore nativo di salvataggio non è automatizzato. |
| Review indipendente | Corretti i rilievi su junction NTFS, probing di percorsi nel cleanup e bit eseguibile GTK. Nessun P1/P2 aperto nelle review indipendenti sul delta corretto; review automatica e CI vengono riconfermate sul nuovo commit. |

La prima CI hosted ha inoltre rilevato una fixture che passava un alias TEMP 8.3 contenente `~` come percorso di identità. La fixture usa ora il percorso fisico; un test Windows ripete il round trip con TEMP/TMP in formato 8.3, senza allentare i vincoli dei percorsi runtime. La pubblicazione conserva il modo eseguibile originale del launcher GTK. Il cleanup conserva i pin ambigui senza esplorare percorsi derivati dal registro.

La verifica Windows delle dimensioni ha rilevato una conferma troncata quando il modulo diventava disabilitato. La frase è stata accorciata nelle due GUI e il solo controllo Windows ha ricevuto spazio aggiuntivo. Il test controlla anche larghezza del testo e glifo della checkbox; non modifica il renderer condiviso dell'app. I risultati della build e del modulo finale sono distinti dai tentativi del harness precedenti, falliti durante raccolta o preparazione dei dati.

## Riprodurre le verifiche

La matrice CI esegue l'intera suite e compila l'app Windows. Per il solo contratto degli inviti:

```bash
cd agent
python -B -m unittest test_node_enrollment test_nodes test_enrollment_cli
```

Su Linux con GTK 3 e una sessione grafica disponibile, dalla radice della repository:

```bash
python3 -B tools/qa-node-enrollment/check-gtk.py
```

Il programma usa un registro temporaneo e salva le immagini in `work/node-enrollment`. Su Windows `tools/qa-node-enrollment/NativeEnrollmentQa.cs` espone `Run(assemblyPath, runtimeRoot, outputDirectory)`, da invocare su thread STA dopo aver compilato l'app e il harness con i riferimenti `System`, `System.Core`, `System.Drawing`, `System.Windows.Forms` e `System.Web.Extensions`. Usare una cartella QA nuova: il harness crea le proprie fixture, conserva e ripristina gli override d'ambiente e non mostra la finestra principale.

## Passi ancora aperti

Un invito valido fissa l'identità host confrontata dall'operatore e registra la rotta; non autorizza la chiave dell'origine sul peer. SSH, runtime e desktop restano `unchecked`. Restano l'autorizzazione iniziale, il preflight autenticato, il controllo dei protocolli e il provisioning desktop con TLS, consensi e stop locale durante trasferimenti. GUI e MCP devono selezionare esplicitamente lo stesso catalogo locale quando si unificano profili Desktop/Core preesistenti. Non viene eseguita una migrazione implicita.

Lo sblocco e il ricaricamento del Core Ubuntu sono documentati nel [collaudo del controller](collaudo-core-controller-ubuntu.md). Il boot Kali senza login grafico resta una prova separata, subordinata a nuova autorizzazione al riavvio.
