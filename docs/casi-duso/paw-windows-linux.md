# Primo caso d’uso di Vincula Agent Infrastructure

Il 21 novembre 2026 Codex, avviato su Ubuntu, ha usato il collegamento
Ethernet di Vincula per verificare un risultato di PAW su un PC Windows
fisico. La domanda era se la wheel di PAW e il modello Horizon Q8
producessero su Windows gli stessi risultati già misurati su Linux per
un messaggio italiano sintetico. La prova riguarda la revisione PAW
`be7d382`. [PR PAW #197](https://github.com/NOEMETRA/PAW--Phishing-Attribution-Workbench--/pull/197)
· [Revisione collaudata](https://github.com/NOEMETRA/PAW--Phishing-Attribution-Workbench--/commit/be7d382da1e5795f958bde5f2f5b797d8d7fe233)

## Come si è svolta la prova

1. Da Ubuntu, uno script ha usato il trasporto SSH di Vincula per creare
   una cartella temporanea su Windows e trasferirvi la wheel, il modello
   ONNX quantizzato, il tokenizer e lo script di verifica.
2. Sul PC Windows, PowerShell ha confrontato gli SHA-256 dei quattro file
   con quelli calcolati su Ubuntu. Ha poi installato la wheel con le
   dipendenze per l’analisi del testo in un ambiente Python temporaneo.
3. Sul caso sigillato, in un percorso contenente caratteri Unicode,
   PAW ha eseguito `paw assess-text` con il modello locale. Ubuntu ha
   ricevuto il rapporto JSON attraverso Vincula e lo ha confrontato con i
   rapporti Linux. Il passaggio finale ha verificato la rimozione della
   cartella temporanea su Windows.

## Risultato osservato

| Ambiente | Python | ONNX Runtime | Token | Originale e sigillo verificati |
| --- | --- | --- | ---: | --- |
| Windows nativo | 3.14.7 | 1.32.0 | 23 | Sì |
| Linux | 3.12.3 | 1.32.0 | 23 | Sì |
| Linux | 3.14.9 | 1.32.0 | 23 | Sì |

Tutti e tre i rapporti riportano lo stesso SHA-256 del testo normalizzato,
`3c926f4d7f2859fbc149b726bb30769217fee97686f67fed070f8fc148cb1f33`.
Coincidono anche i tre punteggi registrati per questo messaggio.

| Campo PAW | Valore comune |
| --- | ---: |
| `legitimate` | 0.999721348285675 |
| `spam` | 0.00013801311433780938 |
| `fraud` | 0.000140576230478473 |

## Dallo script a un’interfaccia per agenti

Lo script conosceva il trasporto di Vincula e i percorsi predisposti per il
collaudo. Questo primo caso dimostra la direzione Ubuntu → Windows.
La milestone proposta è ripetere lo stesso lavoro con un client MCP
avviato a turno su ciascun PC. Con gli stessi permessi, il client deve
poter scoprire i due nodi e le operazioni disponibili, scegliere dove
eseguire la verifica e recuperare un rapporto confrontabile con questi
valori. Gli indirizzi SSH e i percorsi specifici restano nella
configurazione di Vincula.
