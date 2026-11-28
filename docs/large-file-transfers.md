# Piano dei trasferimenti per dataset grandi

Questo è il progetto completo della fase 9. Il [primo incremento protocollo 2](large-file-protocol.md), separatamente disabilitato per impostazione predefinita, introduce prenotazioni agli endpoint, export immutabile, blocchi verificati e avanzamento MCP limitato. Il [collaudo nelle sei direzioni](collaudo-file-grandi.md) verifica la consegna SSH fra receiver temporanei reali. Controller durevole, relay con quota persistente e ripresa dopo un vero crash del coordinatore da un'altra origine fisica restano da realizzare; i requisiti sotto rimangono validi. Il protocollo legacy conserva il limite di 64 MiB, le copie complete e il relay temporaneo sull’origine quando entrambi gli endpoint sono remoti. Il trasporto SFTP legacy ha un timeout di 90 secondi.

## Contratto del trasferimento

Il nuovo protocollo deve avere una versione distinta e mantenere invariato quello esistente. La richiesta immutabile contiene UUID del trasferimento, endpoint identificati per UUID/root/percorso, dimensione massima autorizzata e politica di scadenza. Gli UUID di export e import derivano da quello del trasferimento. Il manifest del contenuto aggiunge dimensione effettiva, SHA-256 complessivo, dimensione dei blocchi e rispettivi digest.

L’autorizzazione `ask` deve mostrare endpoint e budget prima degli effetti, e vincolare la pubblicazione al manifest approvato. `full` rispetta i grant e i budget locali; non significa spazio o banda illimitati. Non si aumenta silenziosamente il limite delle installazioni precedenti. Se cambia la richiesta si usa un nuovo UUID.

La sorgente prepara un export immutabile. Un file modificato durante la preparazione produce un errore o una nuova preparazione esplicita, senza riutilizzare un manifest precedente. Il sistema non promette snapshot atomici di directory o dataset in modifica: richiedono cooperazione del programma sorgente o snapshot del filesystem.

## Risorse e ripresa

Prima di creare copie si prenota lo spazio necessario su sorgente, destinazione e relay. Le prenotazioni sono durabili e atomiche rispetto agli altri trasferimenti. Il controllo dello spazio libero integra la quota, ma non impedisce a programmi esterni di consumare il disco: `ENOSPC` deve lasciare un trasferimento recuperabile e nessun file finale parziale.

La copia e il calcolo dei digest avvengono fuori dalle transazioni SQLite. Ogni checkpoint registra brevemente il blocco verificato. Un lock per trasferimento impedisce due scrittori dello stesso UUID; un limite locale regola trasferimenti simultanei e spazio prenotato. La prenotazione non viene liberata mentre un processo di copia posseduto è ancora vivo.

La ripresa interroga i checkpoint dei destinatari e verifica i blocchi persistiti prima di saltarli. Non si considera valido un prefisso soltanto perché esiste o ha la lunghezza attesa. Blocchi mancanti o corrotti vengono ritrasmessi; una sorgente diversa o un manifest incompatibile fermano l’operazione. Un’altra origine può recuperare il manifest e proseguire mantenendo identità e approvazioni, usando le proprie rotte autorizzate.

Il relay diventa uno spool durabile riferito all’UUID, soggetto a quota e pulizia esplicita. La cancellazione di uno spool non cancella l’unica copia di un risultato già prodotto senza una ricevuta o una scelta dell’operatore. Le connessioni dirette fra endpoint restano un’ottimizzazione successiva e richiedono una rotta autorizzata distinta.

## Controllo e pubblicazione

Avvio, avanzamento, cancellazione e lettura dello stato sono operazioni separate. Ogni chiamata di controllo ha una durata limitata; la copia lunga appartiene a un processo supervisionato. Lo stop desktop usa il proprio canale e non attende il processo SFTP, il lock del trasferimento o una transazione di copia.

La cancellazione termina i processi posseduti prima di confermare lo stop; se un nodo è irraggiungibile, lo stato espone l’incertezza e la scadenza prevista. I parziali rimangono soltanto nelle directory private di staging. La pubblicazione finale verifica dimensione e digest e conserva il comportamento attuale di non sovrascrivere il destinatario. Una risposta persa dopo la pubblicazione viene riconciliata mediante ricevuta e identità del file.

## Incrementi e prove

1. Introdurre manifest, quote, prenotazioni e stato durabile senza modificare le copie legacy. Testare claim concorrenti, riavvio, scadenze e rilascio dopo cleanup.
2. Aggiungere export/import a blocchi e pubblicazione idempotente con hash, mantenendo il protocollo vecchio. Testare file sintetici oltre 64 MiB, blocchi corrotti e sorgente cambiata.
3. Aggiungere trasporto con avanzamento e cancellazione, poi relay riprendibile da un’altra origine. Interrompere copie reali Ethernet e Wi-Fi, riavviare il coordinatore e verificare che non riparta dai blocchi già verificati.
4. Collaudare spazio insufficiente, limiti concorrenti e stop locale Windows durante la copia. Misurare tempo totale, disco aggiuntivo e byte ritrasmessi, senza attribuire al calcolo i tempi dei trasferimenti.

Il [benchmark iniziale](benchmark-cpu-tre-nodi.md) mostra già il costo del coordinamento e delle copie. Scheduling e località dei dati devono usare queste misure insieme al [piano infrastruttura](piano-infrastruttura-multinodo.md).
