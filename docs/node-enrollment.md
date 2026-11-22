# Aggiungere un computer a Vincula

Questo documento definisce il flusso successivo al catalogo locale delle PR #11 e #12. Il catalogo permette già di registrare UUID e percorsi SSH; l’enrollment qui descritto deve ancora essere implementato. Il salvataggio di una riga non dimostra che il computer sia raggiungibile o che il desktop sia pronto.

## Identità e scambio iniziale

Il nuovo computer mantiene un UUID persistente, indipendente da sistema operativo, nome e indirizzo. Un cambio di rete aggiorna le rotte, conservando l’identità. Un’installazione clonata richiede un’identità distinta prima di essere aggiunta come altro computer.

Il comando locale di preparazione dovrà produrre un invito pubblico con versione del formato, UUID, nome, famiglia del sistema, indirizzi proposti, chiave host SSH pubblica e impronta. Non deve includere chiavi private, token, password o percorsi di credenziali dell’account. L’operatore confronta l’impronta con il dato mostrato sul nuovo computer prima della prima connessione. Una chiave ottenuta con una scansione della rete non diventa automaticamente fidata.

Ogni origine autorizzata usa la propria identità SSH. Il computer destinatario riceve soltanto le chiavi pubbliche approvate per quell’origine. L’enrollment non copia i file privati fra computer e non modifica la fiducia globale di SSH. Le chiavi host verificate vanno nel known_hosts dedicato, associate all’UUID del nodo e con controllo rigoroso durante le connessioni.

## Un unico percorso nelle app

| Passaggio | Risultato da mostrare | Verifica necessaria |
| --- | --- | --- |
| Identità | UUID e invito acquisiti | Formato, unicità, confronto dell’impronta |
| Collegamento | SSH autenticato | Chiave host fissata, account, porta e rotta effettivamente usati |
| Runtime | Versione e protocolli disponibili | Manifest della release e hash, interprete, risposta del destinatario |
| Funzioni agente | Stato, programmi e file disponibili | Policy e grant del destinatario, separati per funzione |
| Desktop | Principale, membri e layout salvati | Deskflow, TLS reciproco, rete e sessione grafica verificati |
| Attivazione | Stato della sessione su tutti i membri | Prepare, commit e ricevute di disponibilità di ogni nodo |

Gli errori sono associati al passaggio e al computer. Una funzione non pronta resta disabilitata con un motivo concreto; le funzioni già verificate restano utilizzabili. Una ripetizione riprende la verifica dello stesso nodo e non genera un nuovo UUID.

## Registri locali e permessi

Il registro è locale a ciascuna origine. L’enrollment distribuisce riferimenti pubblici verificati e prepara una rotta su ogni origine scelta dall’operatore. Percorsi di identità, known_hosts, interpreti e indirizzi di bind rimangono propri di quell’origine. Copiare lo stesso JSON su Windows e Linux non costituisce una sincronizzazione corretta.

La pubblicazione del registro conserva revisione attesa, lock e sostituzione atomica. Un conflitto richiede una nuova lettura; non sovrascrive la modifica di un’altra app. Prima di rimuovere un nodo occorre risolvere riferimenti nei gruppi desktop e lavori attivi. Gli storici mantengono gli UUID originali.

L’account destinatario sceglie `ask` oppure `full`. L’esecuzione nativa generale richiede anche l’abilitazione esplicita della funzione; gli aggiornamenti non ampliano il consenso già dato. Il consenso ai portali desktop è distinto dai permessi agente. L’enrollment non introduce elevazioni automatiche.

## Aggiornamenti e collaudi

Le release vengono installate in directory versionate e verificate prima dell’attivazione. La prima migrazione deve ritirare i vecchi server MCP che ricopiano automaticamente helper obsoleti, fermare soltanto il worker e conservare backup privati di configurazione e database. Una nuova sessione di collaudo non dimostra che un client già aperto abbia ricaricato il server.

Il collaudo minimo aggiunge un quarto UUID a una fixture senza cambiare il codice: registra le rotte differenti da due origini, verifica l’identità, scopre capacità e policy, esegue una richiesta consentita e rimuove il nodo dopo lo stop. Le prove negative coprono impronta diversa, invito duplicato o non valido, percorso locale non portabile, revisione cambiata, runtime incompatibile e nodo non raggiungibile. Il collaudo desktop aggiunge perdita di un membro, stop locale durante un trasferimento e ritorno del controllo alla postazione.

Per la triade reale, la scelta dei collegamenti e i prerequisiti rilevati sono nel [piano di provisioning desktop](desktop-provisioning.md). La sequenza complessiva e lo stato delle implementazioni sono nel [piano infrastruttura](piano-infrastruttura-multinodo.md).
