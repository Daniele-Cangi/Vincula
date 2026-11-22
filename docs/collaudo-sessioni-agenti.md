# Sessioni agenti sui tre nodi

Il 22 novembre 2026 sono state avviate tre sessioni Codex effimere, una su ciascun PC, con il server MCP Vincula configurato sul rispettivo host. I runtime verificati corrispondono a `main` dopo la PR #8, revisione `b470517f9c8b1c4e8cd3ddfff5879a44290c36ab`.

| Origine | Nodo locale corretto | Nodi disponibili | Chiamate MCP riuscite |
| --- | --- | --- | --- |
| ASUS Ubuntu | ASUS | ASUS, Lenovo, Kali | 10 |
| Lenovo Windows | Lenovo | ASUS, Lenovo, Kali | 10 |
| Kali X260 | Kali | ASUS, Lenovo, Kali | 10 |

Ogni agente ha eseguito `list_nodes` e, per ciascun nodo, `get_node_status`, `list_jobs` e `list_file_roots`. Le risposte contenevano tre UUID distinti, un solo nodo locale e disponibilità dei tre computer. I registri degli eventi Codex riportano le chiamate MCP completate. Le modalità dei lavori erano `ask` e le root file di collaudo erano state rimosse dopo le prove precedenti.

Su Ubuntu e Windows è stata aggiunta la configurazione MCP Vincula che mancava nei client Codex. Su Kali l’avvio usa il launcher che recupera il socket dell’agente SSH della sessione desktop. La CLI Windows è stata aggiornata da 0.161 a 0.171 prima della prova riuscita.

La nuova installazione Ubuntu usa un runtime versionato indipendente dalla checkout di sviluppo. La connessione Ethernet tradizionale richiede anche il file locale con la chiave host Windows verificata. La prova iniziale ha rilevato la sua assenza nella copia del runtime. Il file è stato installato dopo il confronto dell’impronta, quindi la prova è stata ripetuta con esito positivo.

La revisione del runtime è controllata dalla configurazione di avvio e dal confronto SHA-256 dei file installati. Gli strumenti MCP attuali riportano sistema operativo, stato, lavori e cartelle. Le risposte degli agenti distinguono questi dati dalla revisione del codice, che rimane verificata dal processo di installazione.

Questa prova usa connessioni MCP nuove. Le sessioni interattive precedenti mantengono il proprio ciclo di connessione fino alla riapertura del relativo client. La modifica del registro nodi verrà resa visibile alle chiamate successive senza riavvio con il runtime del catalogo comune previsto nel piano.
