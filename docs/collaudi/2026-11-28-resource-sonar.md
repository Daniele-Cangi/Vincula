# Triage Sonar dell’incremento risorse

La prima analisi della [PR #27](https://github.com/Daniele-Cangi/Vincula/pull/27),
commit `f39e8aa5cfd6f7b1d399b0204fdb2656ecba053b`, è del 28 novembre 2026,
09:46:30 UTC. Le API pubbliche Sonar riportano gate **OK**, zero nuovi bug,
vulnerabilità e hotspot, con **36 rilievi di manutenzione**. Questo risultato
non certifica l’assenza generale di difetti e non sostituisce i test nativi.

| Rilievi | Numero | Seguito previsto |
| --- | ---: | --- |
| Complessità S3776 | 16 | Estrarre fasi preservando ordine, transazioni e ownership del cleanup |
| Test di eccezioni S5778 | 9 | Preparare gli argomenti fuori da `assertRaises` |
| Valori ripetuti S1192 | 4 | Estrarre costanti senza cambiare marker o query |
| Condizioni S3358 | 3 | Espandere mantenendo semantica delle piattaforme e di `None` |
| Catch ridondanti S5713 | 2 | `UnicodeError` è già compreso in `ValueError` |
| Suggerimenti fuori contesto S7504/S6659 | 2 | Conservare gli invarianti verificati sotto |

In `jobs.py` il ciclo elimina elementi da `active`: lo snapshot
`list(active.items())` è necessario. Rimuoverlo produce `RuntimeError` quando
il dizionario cambia dimensione. In `worker_resources.py` il confronto UUID
canonico non è equivalente a `endswith`: quest’ultimo accetterebbe anche
`GPU-urn:uuid:11111111-2222-4333-8444-555555555555`, oggi rifiutato.
I due controesempi sono stati riprodotti con sole strutture dati locali.

I 34 interventi di manutenzione restano follow-up espliciti, separati dalla
qualifica del nuovo modello di processi. Nessun rilievo Sonar è stato soppresso,
accettato o riclassificato. La [ricevuta JSON](2026-11-28-resource-sonar.json)
conserva ID, regole, file e azioni proposte dell’analisi indicata; analisi
successive possono avere conteggi e righe differenti.
