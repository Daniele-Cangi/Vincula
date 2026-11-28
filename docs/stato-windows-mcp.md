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

Le misure riguardano questi runner e questo hardware. Un successivo job Windows ha comunque superato i 12 secondi: l'osservatore della prima query originale ha registrato `TimeoutExpired` dopo 12,188 secondi, senza output. Questo conferma il timeout, senza attribuirne la causa a uno specifico provider CIM.

## Recupero delle metriche di base

Soltanto se la query PowerShell scade, Vincula tenta una lettura con le API native di Windows: RAM tramite `GlobalMemoryStatusEx`, uptime tramite `GetTickCount64`, CPU tramite due campioni `GetSystemTimes` distanziati di 50 ms e capacità dei soli dischi fissi. Non avvia un secondo PowerShell. Le [API di sistema Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes) includono il tempo inattivo nel contatore kernel; Vincula lo sottrae dal totale kernel e utente.

Il recupero locale avviene nel processo MCP. Quello remoto usa la stessa connessione SSH autenticata e invia un helper Python standard, con lettura di una lunghezza esatta, limite JSON di 16 KiB e budget di 3 secondi. Il limite originale di 12 secondi resta invariato. Serve il launcher Python `py.exe -3` sul nodo remoto, già richiesto dal runtime Windows.

La GPU resta sconosciuta in questa modalità, così come la CPU se i contatori non sono validi o la macchina ha più di 64 processori logici. Non si sostituiscono valori sconosciuti con zero. Errori diversi dal timeout non attivano il recupero; se anche la lettura nativa fallisce, il nodo resta esplicitamente `unavailable`.

Il [resoconto del collaudo](collaudi/2026-11-28-windows-native-status.json) conserva gli hash dei sorgenti e delle ricevute: 745 test locali, 90 test in copie temporanee su ciascuno dei tre PC e lettura nativa reale Ubuntu → Windows in 0,467 secondi. Nel test Windows il timeout PowerShell è sintetico, mentre i dati Win32 sono reali e l'avvio di processi è vietato. La matrice CI Windows/Linux con Python 3.10 e 3.14 passa; in quel run la query primaria ha risposto entro il limite. Nessuna installazione o sessione degli agenti è stata riavviata.
