# Registro dei nodi Vincula

Il server MCP legge il registro locale all’avvio. Ogni macchina ha un
UUID stabile, un nome leggibile e una famiglia di sistema operativo.
Ogni origine può configurare percorsi SSH diversi per raggiungere gli
stessi UUID. Gli strumenti MCP accettano sia l’UUID sia gli alias
dichiarati dall’operatore.

Su Linux salva il registro in `~/.config/vincula/nodes.json`, o sotto
`XDG_CONFIG_HOME` se impostato. Su Windows usa
`%LOCALAPPDATA%\Vincula\nodes.json`. Puoi indicare un altro file con
`VINCULA_NODE_REGISTRY`. Riavvia il client MCP dopo le modifiche.
In assenza del registro, il server usa le configurazioni Ethernet
esistenti e gli ID `ubuntu` e `windows`.

## Esempio da ASUS TUF

Genera un UUID per ciascun computer e riusalo nelle configurazioni degli
altri PC. Questo esempio riusa la connessione Ethernet già configurata
verso Lenovo e configura un percorso SSH indipendente verso il nodo Kali.

```json
{
  "local": "ad8a4ff4-2a3f-4048-b924-b565c33b704b",
  "nodes": [
    {
      "id": "ad8a4ff4-2a3f-4048-b924-b565c33b704b",
      "label": "ASUS TUF",
      "family": "linux",
      "aliases": ["ubuntu", "tuf"]
    },
    {
      "id": "3952a565-619c-4e92-b508-39a14459e95a",
      "label": "Lenovo",
      "family": "windows",
      "aliases": ["windows", "lenovo"],
      "legacy_route": "windows"
    },
    {
      "id": "153c7541-13c4-458d-819d-05e8f8ec0d76",
      "label": "PC Kali",
      "family": "linux",
      "aliases": ["kali"],
      "ssh": {
        "host": "192.0.2.30",
        "port": 22,
        "user": "vincula",
        "identity_file": "/home/utente/.ssh/id_ed25519_kali",
        "known_hosts_file": "/home/utente/.ssh/vincula_known_hosts",
        "network": "Wi-Fi"
      }
    }
  ]
}
```

Da Lenovo cambia `local` nell’UUID di Lenovo, togli il percorso SSH
dalla voce locale e usa `"legacy_route": "ubuntu"` per ASUS se riusi
l’alias SSH di Linux Dock. Imposta le credenziali di questo PC nella
voce di Kali. Puoi anche fornire una route `ssh` esplicita per qualunque
nodo. `network` è una descrizione. Per scegliere l’interfaccia di uscita
puoi impostare `bind_address` con il suo indirizzo IP locale.

Per una route esplicita, inserisci in `known_hosts_file` **la chiave host
Ed25519 verificata dalla console del nodo**. OpenSSH la cerca sotto
`vincula-<UUID del nodo>`, così puoi aggiornare l’IP senza perdere
l’identità verificata. Il formato è:

```text
vincula-153c7541-13c4-458d-819d-05e8f8ec0d76 ssh-ed25519 <chiave pubblica host verificata>
```

Mantieni gli alias `ubuntu` e `windows` accanto ai rispettivi UUID
quando migri un’installazione esistente. I piani precedenti possono
contenere quegli identificativi. Un nodo remoto privo di route compare
come `not_configured`. Un nodo configurato che non risponde compare come
`unavailable` e conserva il suo ID.

## Lavori e trasferimenti

Installa il [worker](lavori-mcp.md) con l’account raggiunto via SSH
su ogni nodo che esegue lavori. Le modalità di approvazione, le cartelle
e gli adattatori si configurano sul rispettivo nodo. Se sorgente e
destinazione sono entrambe remote rispetto all’agente, il server MCP
riceve il file in un’area temporanea privata e lo inoltra via SFTP
solo dopo aver verificato entrambe le autorizzazioni e il digest SHA-256.

`get_node_status` restituisce sistema operativo, utilizzo e numero
di CPU logiche, RAM, GPU e volumi. La VRAM è esposta quando il driver
fornisce contatori utilizzabili. Sui sistemi Linux vengono sondate la
radice e la home se occupano volumi distinti. Su Windows vengono rilevate
le unità locali. `null` indica una metrica indisponibile.
