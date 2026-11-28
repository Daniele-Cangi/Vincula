# Collaudo dei piani nativi e delle quote

Il 28 novembre 2026 l’incremento della [PR #27](https://github.com/Daniele-Cangi/Vincula/pull/27)
è stato qualificato con copie temporanee sui tre PC. La revisione del codice
è `211b69f6aaa3346fbd34a195ac7aa9c3d4eb46dc`; la [ricevuta JSON](collaudi/2026-11-28-resource-scheduling.json)
conserva hash dei nove helper, log e risultati. I servizi installati non sono
stati aggiornati. Non sono stati modificati credenziali, impostazioni SSH,
policy produttive, finestre o consensi desktop, e non sono stati riavviati PC.

## Risultati

| Livello | Esito |
| --- | --- |
| Suite completa locale, warning fatali | 508 test, 18 skip specifici di piattaforma, nessun errore |
| Ubuntu fisico, profili temporanei | 104 test, 5 skip, nessun errore |
| Kali fisico, profili temporanei | 104 test, 5 skip, nessun errore |
| Windows fisico, profili temporanei | 104 test, 3 skip, nessun errore |
| Windows, pool dopo l’allineamento del profilo temporaneo | 14 test passati; directory effettiva dei tre programmi verificata |
| Sei ripetizioni diagnostiche Windows del caso concorrente | Tutte passate |
| Piano su tre destinatari reali | Tre programmi nativi senza registrazioni, log attesi, exit code 0 |
| Quote e Stop nel piano reale | Due programmi in esecuzione, terzo in attesa; Stop cancella tutti e libera le prenotazioni dopo pulizia |
| Ripetizione del piano reale | Stessi UUID, numero di job invariato |
| Chiusura dei tre receiver di prova | Worker exit 0, nessuna prenotazione, stato fermo, cartelle temporanee rimosse |

Gli stessi test pubblici si eseguono con l’ambiente Python del progetto:

```bash
python -B -W error -m unittest discover -s agent -p 'test_*.py'
python -B -W error .github/scripts/mcp_smoke.py
python -B -W error -m unittest discover -s tools -p 'test_*.py'
```

La matrice hosted Windows/Linux, Python 3.10 e 3.14, è passata su `f831383`.
Quella revisione precede l’ultima correzione della directory legacy Windows:
i [check della PR](https://github.com/Daniele-Cangi/Vincula/pull/27/checks)
documentano separatamente il risultato sull’head finale. Il [resoconto Sonar](collaudi/2026-11-28-resource-sonar.md)
identifica la prima analisi e conserva i follow-up di manutenzione.

## Cosa è stato provato

Client e server MCP stdio reali, con worker privati, verificano `ask`, digest
esatto, `full`, due processi sovrapposti, terzo in coda, Stop circoscritto e replay
senza effetti duplicati. Le prove di crash usano soltanto worker temporanei:
Linux controlla i discendenti separati dalla sessione; Windows osserva gli
handle originali e i Job Object. Le ricevute vincolano job, generazione e nonce.

Per il piano fisico il server nuovo ha usato tre receiver temporanei: un
processo locale Ubuntu e RPC su collegamenti SSH autenticati e già fissati a
Windows e Kali. Il driver sostituisce soltanto il trasporto verso questi
receiver. Non è una sessione del vero Codex collegata al servizio installato.
Il secondo coordinatore è emulato su Ubuntu con un journal vuoto e un diverso
ID locale. L’indisponibilità dell’autorità blocca soltanto le sue richieste di
metadati nel driver; non scollega il Wi-Fi. Queste distinzioni restano esplicite
nella ricevuta.

## Correzioni e limiti osservati

Il primo giro CI ha rilevato race di teardown: le cartelle venivano rimosse
prima della pubblicazione delle ricevute o della chiusura dei guardian. Le
fixture ora attendono la lock Linux o gli handle Windows originali. Due vecchie
iniezioni Windows sono state adattate al nuovo confine dei processi: perdita
dell’ack dopo il contenimento, e file `ready` realmente aperto durante il
risultato nativo. Gli errori di pulizia non sono stati ignorati.

La CI su `08d4ea5` ha poi rilevato che la fixture del pool reimpostava `ROOT`
nel worker e nel guardian, mentre il launcher lo ricalcolava da `LOCALAPPDATA`.
La cartella dell’account esisteva sul PC fisico e mancava sui runner puliti:
il programma falliva prima dell’avvio con `WinError 267`. La fixture ora passa
`LOCALAPPDATA` temporaneo soltanto al processo figlio, e verifica la directory
effettiva di tutti e tre i programmi. L’ambiente del padre e le cartelle
dell’account reale non vengono modificati. La suite pool nativa passa tutti
i 14 test dopo questa correzione; il codice prodotto è invariato.

La review GitHub ha rilevato inoltre che i programmi registrati Windows
ereditavano la directory temporanea degli ack. La correzione conserva `ROOT`
per payload legacy e shell; le invocazioni native mantengono la directory
approvata. Il test reale usa script e input relativi, verifica exit code 7 e 17
e conserva gli output dopo la rimozione degli ack.

Un primo collaudo Windows da 102 test ha incontrato un timeout di avvio nel caso
con due alberi di processi concorrenti. La causa non è stata dimostrata. Dopo
l’aggiunta delle diagnostiche il caso ha superato sei ripetizioni e le suite
complete successive; il fallimento originale rimane registrato, senza
dichiararlo risolto per la sola assenza di nuove riproduzioni.

Le quote sono cooperative, senza hard cap OS CPU/RAM/GPU. Le GPU sono coperte
da fixture sintetiche, non da un benchmark GPU distribuito. Non è stata misurata
un’accelerazione del carico con questo incremento; rimane valido il benchmark
a freddo precedente, più lento del locale nel totale. Il protocollo file Core
resta limitato a 64 MiB. Il rollout dei nuovi tool nelle sessioni installate,
il benchmark aggiornato e gli altri incrementi del [piano](piano-infrastruttura-multinodo.md)
rimangono attività distinte.
