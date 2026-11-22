# Collaudo del recupero distribuito — 22 novembre 2026

Il collaudo ha usato Ubuntu, Windows e Kali reali con tre worker temporanei. Ogni host aveva sorgenti, profilo, configurazione e database separati in un namespace QA. Gli ambienti virtuali esistenti fornivano soltanto l'interprete: sorgenti, configurazioni e worker di produzione non sono stati aggiornati o fermati.

L’**harness SSH/MCP isolato con ambiente adattato** indirizzava le RPC verso HOME/LOCALAPPDATA QA e verificava che anche gli staging SFTP appartenessero alle cartelle temporanee. Riutilizzava le route SSH autorizzate e gli agenti di firma esistenti, incluso il socket della sessione utente Kali.

## Risultati

- Snapshot del collaudo con **88 test nella suite**. Windows ha eseguito con successo 77 test e ne ha esclusi 11 specifici Linux; Kali ne ha eseguiti 86 e ne ha esclusi 2 specifici Windows. `DeprecationWarning` e `ResourceWarning` erano errori fatali. Superati anche smoke MCP, catalogo CLI e bridge PowerShell reale con manifest intermedi e grandi.
- Una capability ha elaborato file sui tre worker. Sono stati completati **3 trasferimenti reali in 4 passaggi SFTP**, includendo una staffetta tra due nodi remoti.
- Un lotto distribuito è terminato sui tre PC. Nei soli profili QA, Windows è poi passato ad `ask`, mentre Ubuntu e Kali mantenevano `full`: i nuovi lavori Windows sono rimasti in attesa, così come un'importazione che richiedeva approvazione.
- Dopo la chiusura della sessione MCP iniziale, nuove sessioni fisiche su Kali e Windows hanno recuperato lotti e capability conservando identificativi originali, timestamp di creazione, risultati e approvazioni pendenti. Anche gli identificativi dei trasferimenti sono rimasti invariati.
- L'audit ha verificato **79 RPC remote**, tutte nei namespace QA. Le letture di recupero hanno prodotto 32 richieste alle destinazioni da Windows e 36 da Kali, limitate a lettura di manifest, stato, provider e trasferimenti esistenti. Nessun nuovo avvio, accodamento, staging, commit, trasferimento o accesso SFTP è stato richiesto dal recupero. Le righe dei database risultavano identiche prima e dopo le letture.
- La revoca esplicita di un provider QA è stata segnalata senza nuovi effetti. Tutti i worker temporanei sono stati arrestati e attesi; cartelle QA rimosse e assenza di processi QA residui verificata.

## Problemi individuati e corretti

1. Il journal trattava la revisione del provider come un digest SHA256. Le revisioni reali sono valori `UUID.hex` di 32 caratteri: ora hanno un validatore distinto dai digest di 64 caratteri, con regressione sul percorso registrazione → piano → journal.
2. I manifest Windows intermedi potevano superare il limite della riga di comando dopo l'espansione base64/UTF-16. Tutte le nuove operazioni journal `store` su Windows usano ora stdin limitato e un comando di lunghezza costante; letture journal e piccoli RPC precedenti restano compatibili.

## Limiti della prova

L'indisponibilità dell'origine era **emulata nel harness**, dopo la chiusura della sua sessione MCP: nessuna rete reale è stata interrotta. Il recupero remoto richiede un manifest già pubblicato; i piani storici locali restano leggibili. Invii simultanei dello stesso identificativo da origini indipendenti, senza una replica nota, non dispongono ancora di autorità globale o CAS: il protocollo non garantisce esecuzione globale exactly-once.
