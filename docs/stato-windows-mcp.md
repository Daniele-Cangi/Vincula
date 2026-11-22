# Stato Windows nei client MCP

Il collaudo della CI ha riprodotto uno stato `unavailable` su Windows con SSH e sistema operativo funzionanti. Il limite della query era 12 secondi. Sono state corrette due ricerche lente nel processo PowerShell, mantenendo quel limite.

Quando un client MCP filtra `PSModulePath`, PowerShell ricostruisce percorsi di ricerca più ampi. La query locale ora imposta i soli moduli di sistema nel processo figlio e all’inizio dello script, prima di invocare cmdlet. Un valore esplicito già fornito dal chiamante viene conservato e l’ambiente Python non viene modificato. Il comportamento di ricostruzione è descritto nella [documentazione Microsoft](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-5.1).

La ricerca di `nvidia-smi.exe` tramite `Get-Command` risultava lenta anche limitando il tipo ad Application. Il codice ora controlla direttamente i file nei percorsi assoluti di PATH. Ignora voci vuote e relative, gestisce percorsi quotati e avvia l’eventuale eseguibile dal percorso trovato. In assenza di NVIDIA conserva i dati GPU disponibili tramite CIM.

## Misure su runner nuovi

Un job separato ha eseguito una sola query completa su un runner Windows nuovo, senza letture dello stato precedenti. I marker temporali usavano un cronometro .NET e non inizializzavano CIM prima della prova.

| Misura | Prima | Dopo la ricerca diretta |
| --- | --- | --- |
| Ricerca NVIDIA senza eseguibile | 10,247 s | 0,024 s |
| Query completa | 18,29 s | 10,08 s |

La matrice successiva ha superato lo smoke MCP con un profilo vuoto su Windows e Linux, Python 3.10 e 3.14. Non usa warmup o aumenti del timeout per rendere disponibile il nodo.

## Compatibilità SSH

Il controllo locale della CI non esercita il limite della shell predefinita di Windows OpenSSH. Una prova reale da Ubuntu ha individuato un comando codificato di 8.917 caratteri, oltre il limite di `cmd.exe`. Il wire format dello stato rimuove soltanto l’indentazione iniziale delle costanti statiche, conservando le nuove righe e i sorgenti leggibili. Gli script dell’utente restano invariati.

Il comando risultante misura 7.741 caratteri. Un test impone un budget di 8.000 caratteri. Parser PowerShell 5.1 e query SSH reale Ubuntu → Windows hanno verificato sintassi, risposta JSON, CPU, RAM e GPU Intel. I test del locator usano file vuoti che non vengono eseguiti e riconoscono anche alias Windows 8.3 mediante l’identità del file.

Le misure riguardano questi runner e questo hardware. Restano possibili timeout reali se un provider o la rete non risponde; in quel caso il tool conserva il comportamento di indisponibilità esplicita.
