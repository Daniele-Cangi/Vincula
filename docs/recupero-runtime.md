# Recupero dei lavori e dei piani

Il runtime conserva sui destinatari un manifest immutabile prima di avviare le parti di un batch o preparare gli input di una capability. Un agente su un altro nodo può usare lo stesso UUID con `get_batch` o `get_capability_run` per ritrovare i lavori originali, anche quando l’origine non è raggiungibile.

## Manifest e lettura da un altro nodo

Il journal SQLite contiene versione del formato, tipo, UUID, origine, richiesta e piano risolto. Il piano conserva UUID dei lavori, destinazioni e, per le capability, revisioni dei provider, inbox, parametri e indici degli input. Il digest SHA-256 viene ricalcolato sia alla scrittura sia alla lettura. Un manifest differente per la stessa coppia tipo/UUID produce un conflitto.

La pubblicazione richiede una ricevuta con il digest atteso. Un destinatario indisponibile o una risposta persa viene riportato come errore della sua parte. Le parti con replica confermata possono procedere. Un conflitto o una ricevuta invalida interrompe il submit prima degli avvii di quel tentativo. Una ripetizione dall’origine conserva gli UUID già assegnati.

Quando il piano manca nel database del coordinatore, i tool di lettura interrogano i journal dei nodi registrati. Repliche raggiungibili differenti producono un errore esplicito. La lettura mantiene l’origine originale e interroga i lavori sui destinatari senza ricrearli. Per le capability consulta soltanto le richieste export/import già persistite e verifica che UUID, operazione, root e percorso corrispondano al manifest. Non prepara trasferimenti, non mette parti in coda e non riapprova richieste.

Un input ancora in attesa, una parte non accodata, un provider revocato e un nodo offline rimangono condizioni visibili nel risultato. Per proseguire un piano non ancora preparato serve il normale percorso di submit dall’origine. Il recupero in lettura non assume automaticamente il ruolo di nuovo coordinatore.

I piani precedenti privi di journal restano leggibili dall’origine. Per recuperarli da un altro nodo è necessaria una pubblicazione con questo runtime. La creazione simultanea dello stesso UUID da origini differenti, prima che esse conoscano una replica, richiede ancora un’autorità globale o un protocollo di consenso. La proprietà garantita da questo incremento riguarda la lettura dei lavori originali dalle repliche disponibili.

## Trasporto dei manifest

Il manifest ha un limite di 256 KiB. I piccoli RPC mantengono il protocollo precedente. I journal più grandi usano stdin con limiti dedicati e un comando SSH di lunghezza costante. Su Windows un bridge PowerShell inoltra i byte al gestore Python senza inserire il manifest nella riga di comando. Il trasporto usa file temporanei regolari, per evitare pipe ereditate da OpenSSH che trattengano la fine della risposta.

JSON invalido, input eccessivo, risposta eccessiva e timeout restituiscono un errore. Una risposta parziale prodotta prima del timeout non vale come ricevuta di replica. Se la scrittura remota è invece riuscita ma la conferma è andata persa, una ripetizione dello stesso manifest è idempotente.

## Crash del worker Linux

Ogni programma Linux viene avviato da un supervisore separato. Il kernel segnala al supervisore la morte del worker. Il supervisore arresta il gruppo del programma, adotta e raccoglie anche i discendenti che hanno cambiato sessione e conserva il lock del worker fino alla pulizia. Un nuovo worker può acquisire il lock soltanto dopo questa fase.

Le conferme di avvio e di uscita sono distinte. Un errore confermato prima dell’avvio produce `failed`. Se manca una conferma affidabile dopo un possibile avvio, il lavoro rimane `unconfirmed`. Il riavvio e la ripetizione dello stesso UUID non lo eseguono nuovamente. Codice di uscita e log restano separati dallo stato del supervisore.

Il servizio systemd conserva anche il contenimento del cgroup. Questo è necessario per una garanzia forte quando vengono uccisi insieme worker e supervisore o quando i discendenti assumono privilegi differenti. I controlli `ask` e `full` sul destinatario mantengono il loro comportamento.

## Verifica

Le suite dedicate coprono crash con figli normali e separati tramite `setsid`, pulizia dopo uscita, lock e riavvio concorrente, perdita delle conferme, repliche corrotte o in conflitto, origine offline, approvazioni pendenti e recupero capability senza effetti. Il test Windows del bridge PowerShell esegue un manifest oltre i limiti della riga di comando in un profilo temporaneo.

Esegui la suite con l’interprete del runtime e le dipendenze installate.

```sh
python -W error::DeprecationWarning -W error::ResourceWarning -m unittest discover -s agent -p 'test_*.py' -v
```
