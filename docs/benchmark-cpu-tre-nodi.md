# Prima misura CPU distribuita su tre nodi

Il collaudo del 22 novembre 2026 confronta lo stesso carico seriale su ASUS Ubuntu, Lenovo Windows e Kali X260, poi ripartisce il lavoro sui tre PC tramite MCP. Con gli input inizialmente su Ubuntu, il totale mediano distribuito è **24,637 secondi**, contro **20,491 secondi** del seriale locale. Lo speedup totale osservato è **0,832×**, con un aumento del tempo del **20,2%**.

Il calcolo critico distribuito richiede **8,799 secondi**, contro **11,144 secondi** del seriale più rapido, Windows, con un rapporto di **1,267×**. Dopo che gli ingressi sono disponibili, l’esecuzione MCP distribuita richiede **12,405 secondi**, contro **14,132 secondi** della modalità seriale migliore, con un rapporto di **1,139×**. Il costo degli ingressi remoti determina quindi la scelta conveniente per questo carico.

## Ambiente e carico

Server MCP e gestori erano congelati al commit `b470517f9c8b1c4e8cd3ddfff5879a44290c36ab`. Il preflight ha verificato gli hash di `agent/server.py` e del gestore installato, lo stato attivo dei worker, la raggiungibilità dei tre nodi e la modalità `ask`.

| Nodo | Sistema rilevato dal programma | Python |
| --- | --- | --- |
| ASUS | Linux x86_64, kernel 7.0.0-44-generic | 3.14.4 |
| Lenovo | Windows 11, build 26300 | 3.14.7 |
| Kali X260 | Linux x86_64, kernel 7.2.15+kali-amd64 | 3.14.10 |

Il programma conta i numeri primi tramite divisioni con divisori dispari fino alla radice quadrata. Ripete il conteggio due volte per ogni parte. Ogni job usa un processo e un thread applicativo. Il riferimento indipendente usa il crivello di Eratostene.

| Parte | Intervallo | Primi | Assegnazione distribuita |
| --- | --- | --- | --- |
| 0 | `[2, 1.000.000)` | 78.498 | ASUS Ubuntu |
| 1 | `[1.000.000, 2.000.000)` | 70.435 | Lenovo Windows |
| 2 | `[2.000.000, 3.000.000)` | 67.883 | Kali X260 |

Il seriale esegue tutte e tre le parti in un job. Il distribuito esegue un job per parte, con questa assegnazione fissa in tutte le ripetizioni. Ogni parte legge un file JSON sintetico da 262.144 byte contenente i parametri e padding deterministico. Gli ingressi remoti vengono trasferiti attraverso MCP in percorsi nuovi per ciascun caso.

Sono stati eseguiti tre campioni per ogni modalità, ruotando l’ordine delle modalità tra le ripetizioni. Ogni caso aveva un limite di 180 secondi. I dodici casi hanno restituito codice 0 e lo stesso risultato canonico, con 216.816 primi. Il digest SHA-256 dei risultati ordinati è `70d528230c461d8beafa417a8390755735c142a8f9fccd662b4e43478c7f75f7`.

## Tempi osservati

Le colonne riportano mediane di tre campioni. Il calcolo critico è il massimo dei tempi interni dei job concorrenti. L’esecuzione MCP parte dal submit del batch, dopo i trasferimenti, e comprende approvazioni tecniche, avvio, attesa, polling e lettura dei risultati. Il residuo MCP è questa esecuzione meno il calcolo critico.

| Modalità | Calcolo critico | Trasferimento input | Esecuzione MCP | Residuo MCP | Totale |
| --- | --- | --- | --- | --- | --- |
| Seriale ASUS | 19,974 s | 0,000 s | 20,489 s | 0,693 s | 20,491 s |
| Seriale Lenovo | 11,144 s | 14,048 s | 14,132 s | 3,004 s | 28,191 s |
| Seriale Kali | 20,427 s | 18,545 s | 22,938 s | 2,427 s | 41,490 s |
| Distribuito sui tre PC | 8,799 s | 12,106 s | 12,405 s | 3,524 s | 24,637 s |

| Modalità | Minimo totale | Massimo totale |
| --- | --- | --- |
| Seriale ASUS | 19,743 s | 20,729 s |
| Seriale Lenovo | 28,127 s | 28,245 s |
| Seriale Kali | 38,158 s | 46,229 s |
| Distribuito sui tre PC | 22,235 s | 26,509 s |

La preparazione delle fixture, copie del programma, grant e registrazioni è durata 18,165 secondi. Il rapporto grezzo conserva separatamente preparazione del riferimento, apertura della sessione, submit, approvazioni CLI, polling, lettura dei risultati e tempi interni dei worker. La durata dell’esperimento valido, compresi preflight e pulizia, è stata 388,734 secondi.

## Metodo di riproduzione

Il carico versionabile è [tools/benchmark_cpu.py](../tools/benchmark_cpu.py). Usa solo la standard library e accetta una directory di input e gli indici delle parti. Il suo AST coincide con quello del programma eseguito nel collaudo. Per preparare gli stessi tre file, esegui questo frammento in una directory di prova dedicata.

```python
import json
from pathlib import Path

folder = Path("cpu-bench-input")
folder.mkdir()
bounds = [(2, 1000000), (1000000, 2000000), (2000000, 3000000)]
for index, (start, end) in enumerate(bounds):
    data = {"index": index, "start": start, "end": end, "passes": 2, "padding": ""}
    header_size = len(json.dumps(data, separators=(",", ":")).encode())
    data["padding"] = "x" * (262144 - header_size)
    raw = json.dumps(data, separators=(",", ":")).encode()
    assert len(raw) == 262144
    (folder / f"input-{index}.json").write_bytes(raw)
```

Un’esecuzione seriale diretta del programma usa il comando seguente.

```sh
python tools/benchmark_cpu.py --data cpu-bench-input --parts 0 1 2
```

Per la misura attraverso Vincula, su ogni nodo prepara una cartella sintetica dedicata, copia il programma e registrala con `agent/jobs.py files-grant ALIAS PERCORSO --write`. Registra il programma seriale con i tre indici e il programma della parte assegnata con un solo indice, usando percorsi assoluti dell’interprete, dello script e della directory dei dati.

Dal client MCP del coordinatore applica questa sequenza.

1. Chiama `list_nodes`, `get_node_status` e `list_jobs`. Verifica identità, stato del worker dalla console e disponibilità dei profili di prova. Mantieni `ask` e usa un prefisso casuale per le sole registrazioni del collaudo.
2. Avvia quattro modalità per ripetizione. Gli ordini sono ASUS, Lenovo, Kali, distribuito nella prima, Lenovo, Kali, distribuito, ASUS nella seconda e Kali, distribuito, ASUS, Lenovo nella terza.
3. Per ogni caso, prepara nuovi percorsi di input. Usa `transfer_file` per gli ingressi remoti, approva soltanto gli UUID export/import del collaudo e verifica dimensione e SHA-256 della ricevuta. Nel caso distribuito la parte ASUS usa il file locale e vengono copiate una parte su Lenovo e una su Kali.
4. Chiama `submit_batch` con un task per il seriale o tre task per il distribuito, indicando esplicitamente gli UUID dei nodi. Approva soltanto i job restituiti dal batch sui rispettivi destinatari. Registra i tempi di submit e approvazione separatamente.
5. Interroga `get_batch` fino al completamento, poi `read_job` per ogni task. Verifica codice 0, log completi, digest degli ingressi e risultati ordinati identici al riferimento. Registra i tempi interni del programma e il totale monotono del coordinatore.
6. Ripeti tre volte senza adattare la partizione ai risultati precedenti. Alla fine rimuovi solo programmi, alias e file del collaudo e controlla la modalità `ask` sui tre nodi.

I [dodici record pubblicabili](benchmarks/2026-11-22-cpu.json) conservano tempi, esiti e digest senza coordinate personali. Il controller specifico dell’hardware, i record completi e i riferimenti locali sono conservati sotto `work/benchmark-triad/`. La procedura richiede le normali configurazioni SSH e MCP dei nodi scelti. Il formato JSON prodotto dal workload è utilizzabile da un controller parametrico senza aggiungere funzioni alla piattaforma.

## Interpretazione e limiti

Questa prova misura un algoritmo Python CPU con una partizione fissa e tre campioni per modalità. Le versioni Python e le condizioni delle sessioni operative sono registrate per ciascun job. GPU, scheduler basato sulle risorse e altre partizioni richiedono prove dedicate.

Gli ingressi nascono su ASUS e il caso a freddo comprende i loro trasferimenti. I tempi di trasferimento includono apertura SSH/SFTP, autorizzazioni tecniche e controlli di integrità. Il numero di byte e i singoli tempi sono conservati nel rapporto. La convenienza di un piano dipende quindi anche dalla località iniziale dei dati e dal costo di coordinamento.

Le approvazioni della prova sono state eseguite automaticamente attraverso le CLI autorizzate, limitatamente agli UUID sintetici. Il tempo di decisione e interazione umana richiede una misura separata. La differenza tra calcolo critico ed esecuzione MCP comprende anche l’avvio sfalsato dei job, la coda e l’osservazione del completamento.

Un tentativo preliminare ha trovato il worker ASUS già inattivo prima del calcolo. Il suo unico job è stato cancellato mentre era queued, le fixture sono state ripulite e quel tentativo è escluso dalle statistiche. La causa del precedente TERM del servizio rimane non accertata. Dopo verifica dell’hash baseline e dell’assenza di altri lavori attivi, il coordinatore ha autorizzato l’avvio del solo worker ASUS. Kali e Lenovo erano già attivi.

Tutti i campioni riportati appartengono alla successiva esecuzione completa. La pulizia delle registrazioni e delle cartelle sintetiche è confermata, e le politiche finali sono `ask` sui tre PC.
