# Gruppo desktop: implementazione e prossimi collaudi

Stato del 26 novembre 2026: codice in sviluppo, **non distribuito sui tre PC**.
La PR #25 è stata unita su main. La condivisione precedente rimane quella
installata; questo documento non attesta l'attivazione del nuovo gruppo.

La disposizione scelta dall'operatore è **Ubuntu — Windows — Kali**, con
Windows principale. L'interfaccia permette di includere altri nodi registrati,
cambiare principale e spostare gli schermi con frecce o coordinate, anche su
più righe. Il primo backend gestito supporta l'avvio soltanto con Windows
principale; salvare un'altra disposizione non certifica il supporto del backend.

## Interfaccia e ciclo di vita

La pagina Mouse e tastiera contiene la configurazione del gruppo e i comandi di
avvio/arresto. Salva layout modifica il catalogo; non avvia programmi.
Il nuovo percorso e la condivisione precedente sono alternativi. Un arresto non
confermato deve restare visibile, senza riattivare automaticamente la condivisione.

`desktop_app.py run` è il processo proprietario del gruppo. Le app leggono eventi
JSONL di stato e inviano `{"action":"stop"}` su stdin. Anche EOF ferma il gruppo.
Il percorso dello stop è indipendente dai trasferimenti, dalle modifiche al
catalogo e dai heartbeat in attesa. Solo snapshot privati recenti del motore,
con peer autenticati e consensi reali, possono produrre lo stato pronto.
Lo stop attende i terminali dei supervisor originali; la sola risposta
`stopping` non viene presentata come cleanup completato.

`desktop_node.py` espone soltanto inspect/provision/configure all'account
autenticato via SSH. Gli eseguibili e i percorsi arrivano dall'installazione
locale approvata. I certificati privati sono generati sul nodo e non viaggiano
nel protocollo. Il manifest comune lega UUID, sessione, layout, appunti, pin
distinti e revisioni locali. L'aggiornamento del solo gruppo nei cataloghi usa
CAS; un cambiamento concorrente invalida la preparazione.

La configurazione locale `Vincula-Desktop/deployment.json` deve essere privata e
abilitata dall'operatore. Se manca, il pulsante non installa o sostituisce
silenziosamente il motore: mostra il prerequisito mancante e termina senza
avviare il gruppo. Mancano ancora l'installer verificato e il rollout.

## Motore e limiti operativi

La [patch Deskflow](../third_party/deskflow/README.md) usa profili e certificati
monouso, pipe private, pin TLS associati all'UUID sullo stesso stream e controllo
del loop eventi reale. Clipboard disabilitata non attiva i relativi provider.
Revoca, disconnessione dopo l'adozione e snapshot scaduto sono terminali.
Il guardian conserva sempre la terminazione del gruppo/process tree originale.

Core/MCP rimane separato: una sessione grafica è necessaria per usare il desktop,
non per i lavori headless. Il profilo MCP `full` non sostituisce il consenso del
portale Wayland. Il deployment desktop è un'autorizzazione locale distinta.

## Verifiche e punto di ripresa

Il controllo SSH della PR #17 è stato collaudato separatamente sul sorgente
`6c3455f57b6cf00abec86922a08d2b103ca1dcfb`, con tre origini, audit e cleanup
passati. Da Windows lo stop della sessione sintetica è arrivato in 0,598 secondi
durante un trasferimento SFTP di 16 MiB e con heartbeat bloccato. Il file finale
ha mantenuto lo SHA256 atteso. Questo prova il controllo e la separazione dal
trasferimento; **non prova l'input o gli appunti del nuovo motore**.
La [ricevuta sanitizzata](collaudi/2026-11-26-desktop-ssh.json) conserva hash,
transazione, audit e cleanup. Il sorgente della nuova integrazione è diverso
dal sorgente congelato usato in quel collaudo.

La fixture GTK isolata supera sette scenari del protocollo e dei pulsanti:
[comando riproducibile](../tools/qa-desktop-ui/README.md). La GUI C# aggiornata
compila su Windows; il suo eseguibile QA non è stato avviato né distribuito.
La suite Python desktop aggiornata comprende 142 test: 138 passati e quattro
esclusi perché richiedono Windows. Comprende stop con RPC pendente, scadenza della readiness
prima della lease, errori di avvio e chiusura delle pipe.

Prima del rilascio restano:

1. Review del nuovo codice e build/test nativi Windows del motore modificato.
2. Pacchetti con dipendenze, licenze e manifest verificati per entrambi i Linux
   e Windows; installer delle approvazioni locali e dei supervisor grafici.
3. Avvio del supervisor nella vera sessione utente: task Interactive/Limited
   Windows e ambiente della sessione grafica Linux. SSH non sostituisce quel
   contesto. Nessun riavvio di Kali è necessario per la preparazione.
4. Cutover esplicito dalla condivisione precedente e consenso Wayland, seguito
   da mouse/tastiera e testi sintetici negli appunti nelle sei direzioni.
5. Ripetere lo stop da Windows durante un trasferimento con il motore reale,
   revoca/disconnessione e verifica dell'assenza di riavvii automatici.

La pausa richiesta dall'operatore precede questi interventi sui PC.
