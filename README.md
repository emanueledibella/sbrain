# Second Brain — motore locale di memoria personale

Prima versione di un **motore nostro**: il codice decide come adottare, correggere, versionare e
recuperare i ricordi. Usa Neo4j come database, Ollama per inferenza locale e l'SDK ufficiale MCP
per comunicare con un agente. Non dipende da Graphiti, Mem0 o altri motori di memoria.

## Avvio

Richiede **Docker con Compose v2** avviato (Docker Desktop oppure OrbStack sul Mac).
Python, il motore MCP, Neo4j e Ollama girano nei container: non serve una `.venv` sul computer.
Dalla cartella del progetto:

```bash
./scripts/init.sh
docker compose up -d --build
docker compose ps -a
docker compose exec -T mcp second-brain status
```

`init.sh` usa un container Python temporaneo per creare il vault e una `.env` con password
casuale e permessi `600`. Conserva password e configurazione già esistenti e aggiunge UID/GID
dell'utente per scrivere nel vault senza creare file appartenenti a root. Si può rieseguire.
Le credenziali e il vault sono esclusi da Git e dal contesto di build delle immagini.

Compose avvia:

| Servizio | Funzione | Indirizzo sul computer |
| --- | --- | --- |
| `mcp` | Motore di memoria e MCP Streamable HTTP persistente | `http://127.0.0.1:8000/mcp` |
| `neo4j` | Grafo, ricerca testuale e vettoriale | Browser `http://127.0.0.1:7474`, Bolt `127.0.0.1:7687` |
| `ollama` | Estrazione locale e embedding | `http://127.0.0.1:11434` |
| `models-init` | Scarica i modelli mancanti, poi termina | Nessuna porta |

`mcp` parte quando Neo4j e Ollama sono sani e `models-init` ha terminato con successo.
È normale che `models-init` risulti **Exited (0)**. Al primo avvio il download dei modelli
può richiedere diversi minuti; quelli già presenti nel volume vengono riutilizzati.

```bash
docker compose logs -f models-init
docker compose logs --tail=100 mcp
curl http://127.0.0.1:8000/health
```

`/health` verifica che il trasporto HTTP risponda; `second-brain status` e `memory_status`
controllano vault, database e modelli. Tutte le porte sono pubblicate solo su `127.0.0.1`.
Nel container il motore usa `bolt://neo4j:7687` e `http://ollama:11434` tramite la rete Compose.
Non usare `localhost` per raggiungere un altro container.

I modelli predefiniti sono `qwen2.5:1.5b` per l'estrazione e `embeddinggemma` per gli embedding.
Nessuna chiave o API a pagamento è richiesta. I download iniziali richiedono internet;
elaborazione e recupero usano poi i servizi locali. Su Apple Silicon questa configurazione
Ollama in Docker usa la **CPU**. Il modello della chat in LM Studio è separato dai modelli
usati dal motore e può continuare a girare nell'applicazione nativa sul Mac.
Il modello piccolo serve per iniziare: correttezza semantica e qualità delle identità devono
essere valutate sui tuoi esempi. La validazione delle citazioni non verifica il significato
dell'interpretazione del modello.

I modelli e le risorse sono configurabili in `.env` (vedi [.env.example](.env.example)).
Su una nuova installazione si può copiare `.env.example` in `.env` **prima** di eseguire
`init.sh`, che sostituirà la password di esempio con una casuale. Con un database esistente,
modificare la `.env` corrente conservando `NEO4J_PASSWORD`.
`SB_VAULT_PATH` indica la cartella sul computer da montare in `/data/vault`: `init.sh` crea
`./vault`; se scegli un'altra cartella, creala prima dell'avvio. `SB_MCP_PORT` cambia la porta
HTTP pubblicata; in quel caso aggiornare anche la URL nella configurazione del client.
Per applicare modifiche a `.env` o al codice usare `docker compose up -d --build`.
Le immagini di base sono fissate ai digest e le dipendenze runtime in `requirements-runtime.lock`.
Il container MCP usa un utente senza privilegi e un filesystem di sistema in sola lettura.
I thread di inferenza sono limitati a quattro per evitare il forte rallentamento osservato
con tutti i core nel container su questo Mac; `SB_MODEL_THREADS` permette di modificarli.
Sono configurabili anche il contesto e il limite di generazione. Note molto lunghe o dense
possono superare questi limiti e restare in attesa: suddividerle prima dell'ingestione.

## Persistenza, arresto e aggiornamento

Il vault è un bind mount della cartella `vault/`, quindi Markdown e registro restano accessibili
dal computer. Neo4j e Ollama usano i volumi `second-brain_neo4j_data`,
`second-brain_neo4j_logs` e `second-brain_ollama_models`. Il nome del progetto Compose rimane
`second-brain`: vengono riutilizzati anche i dati e i modelli della configurazione precedente.

```bash
# Arresto e successivo avvio, conservando file e volumi.
docker compose down
docker compose up -d --build

# Dopo una modifica al codice del motore.
docker compose up -d --build mcp
```

Per un arresto normale non usare `docker compose down -v`, che rimuove i volumi di Neo4j
e i modelli scaricati. Prima di un backup del vault, arrestare i processi che possono scrivere
(inclusi i client stdio); salvare l'intera cartella, compresa `.memory/`.
Se una porta è occupata da un altro servizio, risolvere il conflitto prima dell'avvio.

## Primo utilizzo

Esempio inventato, da tenere nell'ambito `demo`:

```bash
docker compose exec -T mcp second-brain remember --file examples/nota.md --title "Gita di esempio" --scope demo
docker compose exec -T mcp second-brain recall "Chi viene in gita?" --scope demo
```

Per usare un'estrazione esplicita e riproducibile, senza inferenza del modello chat:

```bash
docker compose exec -T mcp second-brain remember --file examples/nota.md --scope demo \
  --extraction examples/estrazione.json --request-id esempio-1
```

`request-id` rende sicuro riprovare la stessa operazione. Un riutilizzo con contenuti diversi
viene rifiutato. I risultati contengono gli ID delle fonti e dei ricordi, citazioni, conflitti e
avvisi. La CLI restituisce `0` al successo, `2` quando ci sono avvisi/elaborazione incompleta,
`1` per errori. `stored_pending_extraction` significa che il testo è già salvato ma il motore
non ha adottato l'estrazione. Non confonderlo con `indexed`.

La dimostrazione completa registra ricordi fittizi nell'ambito `demo`, cambia la destinazione
di una gita e corregge la partecipazione di Marco:

```bash
docker compose exec -T mcp second-brain demo
```

Per provare una demo isolata senza avviare Neo4j/Ollama, dopo il bootstrap e la build:

```bash
docker compose run --rm --no-deps mcp --vault /tmp/second-brain-demo --offline demo
```

I percorsi passati alla CLI sono quelli **interni al container**. Gli esempi sono inclusi
nell'immagine in `/app/examples`; il vault del computer è accessibile in `/data/vault`.
Per importare un'altra nota dal computer, passarla in stdin a un file temporaneo del container:

```bash
docker compose exec -T mcp sh -c 'cat > /tmp/nota.md' < /PERCORSO/nota.md
docker compose exec -T mcp second-brain remember --file /tmp/nota.md --scope personal
docker compose exec -T mcp rm /tmp/nota.md
```

## Come è organizzata la memoria

```text
Agente / LM Studio
        ⇅ MCP HTTP oppure stdio tramite docker compose exec
Container mcp: MemoryEngine
        ├── /data/vault ⇄ ./vault: MD originali + registro JSONL
        ├── Container ollama: estrazione JSON + embedding
        └── Container neo4j: grafo + indici testuali e vettoriali
```

```text
vault/
  episodes/<source-id>.md                    # copie modificabili delle note
  entities/<entity-id>.md                    # profili derivati, rigenerabili
  .memory/
    vault-id                                # namespace stabile del vault
    writer.lock                             # un solo scrittore alla volta
    events.jsonl                            # operazioni adottate, con catena SHA-256
    source-versions/<source-id>/<rev>-<hash>.md  # originali immutabili
    recovery/                               # eventuali frammenti di append interrotti
```

Una nota può descrivere molte entità e ricordi: non occorre creare un file per ogni attributo.
Gli ID delle entità sono stabili; i tipi sono stringhe aperte (`person`, `event`, `project`…)
e i predicati sono estendibili. Per riusare un'identità il modello deve indicare `existing_id`;
il motore verifica ambito, nome e tipo. Un nome uguale da solo non produce una fusione.
Questa prudenza può creare duplicati: una gestione guidata di alias/fusioni è lavoro successivo.
Il prompt include al massimo cinquanta entità dello stesso ambito, dando precedenza ai nomi
presenti nel testo. La risoluzione delle identità dovrà diventare una ricerca dedicata per
archivi con molte persone/progetti.

Ogni ricordo contiene:

- soggetto, predicato, valore e un eventuale oggetto collegato;
- ambito (`personal`, `work`, `demo`…) e contesto specifico;
- stato epistemico: `explicit`, `proposed`, `inferred`;
- intervallo di validità e momento di registrazione;
- fonte, revisione, hash e citazione con offset in caratteri Unicode;
- embedding locale e identità del modello, se disponibili.

`explicit` significa che la fonte lo dichiara, non che sia verificato nella realtà.
Proposte e inferenze sono escluse dal recupero normale; si includono con `--include-uncertain`.
Il modello propone dati JSON; il motore controlla schema, citazioni, riferimenti e intervalli.
Una citazione inesistente o ambigua rifiuta l'intero lotto, lasciando disponibile l'originale.
Per un errore di schema/citazione/riferimento l'estrattore locale prova una sola riparazione
con il messaggio di validazione; se fallisce, il lotto resta in attesa senza essere adottato.
Nuovi fatti discordanti coesistono e sono segnalati come conflitti: non vengono sovrascritti
automaticamente. Il rilevamento usa lo stesso soggetto/predicato/contesto; non è un confronto
semantico universale tra frasi.

Il grafo contiene `Entity`, `SourceRevision`, `Assertion` (versioni), `MemoryChange` e
`MemoryMeta`. Le relazioni sono `ABOUT`, `OBJECT`, `SUPPORTED_BY`, `TARGETS` e `REPLACEMENT`.
Un ricordo è un nodo intermedio per poter collegare al fatto stesso fonti, tempo e correzioni.

Esempio logico:

```text
[Marco] ← ABOUT ─ [«Marco partecipa alla gita»] ─ OBJECT → [Gita]
                              │
                         SUPPORTED_BY
                              ↓
                     [Nota MD, revisione 1]

[Correzione] ─ TARGETS → [vecchia affermazione]
      ├── REPLACEMENT → [nuova affermazione]
      └── SUPPORTED_BY → [MD con la correzione esplicita]
```

## Correzioni e tempo

Sono due operazioni diverse, entrambe appendono nuovi dati:

```bash
# Avevo ricordato male: correggere anche la ricostruzione del passato.
docker compose exec -T mcp second-brain correct CLAIM_ID \
  --value "Marco: sì" --statement "Marco partecipa alla gita." \
  --reason "La precedente interpretazione era errata"

# Il programma è cambiato realmente da un certo momento.
docker compose exec -T mcp second-brain revise CLAIM_ID \
  --value Catania --statement "La destinazione della gita è Catania." \
  --effective-at "2026-10-14T10:00:00+02:00" --reason "Nuova decisione"
```

`correct` ritira la vecchia affermazione dal momento in cui la correzione viene registrata
e crea una sostituzione con lo stesso intervallo di validità, salvo parametri espliciti.
`revise` chiude invece la validità del vecchio fatto alla data del cambiamento e introduce
il nuovo. La versione precedente del vecchio intervallo resta consultabile.
`retract CLAIM_ID --reason ...` ritira senza sostituire; **non cancella** gli originali.
Le operazioni agiscono sul singolo ricordo, non sull'intera entità.

Due assi temporali, con intervalli inclusivi all'inizio ed esclusivi alla fine:

- `valid_from / valid_to`: quando valeva il fatto;
- `recorded_from / recorded_to`: quando il sistema conservava quella versione.

La data di una gita futura può essere un suo attributo: non è automaticamente la data
da cui diventa valida una decisione presa oggi. I timestamp richiedono un fuso orario.

```bash
docker compose exec -T mcp second-brain recall "destinazione gita" \
  --valid-at "2026-10-13T12:00:00+02:00" \
  --known-at "2026-10-15T12:00:00+02:00"
docker compose exec -T mcp second-brain explain CLAIM_ID
docker compose exec -T mcp second-brain history --scope personal
docker compose exec -T mcp second-brain read-source SOURCE_ID --revision 1
```

Modificare una nota in `episodes/` non cambia silenziosamente il ricordo. Registrare la
modifica con `refresh SOURCE_ID`, oppure `remember --source-id SOURCE_ID --file ...`.
Il nuovo MD diventa una nuova revisione. L'estrazione di una revisione nuova non ritira da sola
i fatti delle precedenti: usare `correct`/`revise`/`retract` per decidere cosa sostituire.
`entities/` è una vista generata dell'audit; usare `recall` per lo stato valido in un momento.

## Recupero, disponibilità e ricostruzione

Il recupero combina ricerca testuale, semantica e i collegamenti al grafo. I candidati sono
filtrati rispetto a entrambi gli assi temporali prima di formare il contesto per l'agente.
Un calcolo cosine sulle sole versioni visibili evita che molti nodi storici nascondano i
ricordi corretti nel limite dei candidati dell'indice vettoriale. Senza servizi viene usata
la ricerca testuale canonica con un avviso. Il budget è stimato a quattro caratteri per token:
le citazioni di un ricordo non vengono troncate; un ricordo troppo grande può essere escluso.
La ricerca Neo4j espande anche i candidati attraverso entità condivise (`ABOUT`/`OBJECT`),
così una domanda su una persona può recuperare il contesto dell'evento a cui partecipa.

Il commit avviene nel registro JSONL **prima** dell'aggiornamento del grafo. Non c'è una
transazione distribuita fra filesystem e Neo4j. File lock, fsync, scritture atomiche e ID
idempotenti proteggono i retry. Una scrittura interrotta nell'ultima riga viene salvata in
`recovery/` prima di ripristinare il confine dell'ultimo evento completo; una riga completa
corrotta o una catena spezzata fermano il motore. La catena rileva alterazioni accidentali;
non è una firma crittografica contro chi può riscrivere l'intero vault.

Neo4j conserva una proiezione identificata da `vault_id` e hash del registro. Una proiezione
nuova si sostituisce in una sola transazione Neo4j; altre memorie o dati del DB restano distinti.
Un errore lascia la proiezione precedente e viene segnalato. Per questa prima versione ogni
nuovo commit ricostruisce la proiezione del vault: è semplice ma costa O(N) e andrà reso
incrementale per archivi grandi. Anche replay e ricerca canonica caricano il registro in RAM.

```bash
# Nessuna nuova estrazione AI: usa i dati già adottati nel registro.
docker compose exec -T mcp second-brain replay

# Dopo un guasto di Ollama, riprova una fonte già salvata.
docker compose exec -T mcp second-brain extract SOURCE_ID --revision 1

# Se gli embedding mancavano o cambia/si aggiorna il modello o la dimensione.
docker compose exec -T mcp second-brain reembed
```

Riassunti e contesti vengono composti dai ricordi recuperati, senza memorizzare una copia
indipendente che potrebbe diventare obsoleta dopo una correzione. La v1 non ha ancora una
cache di riassunti generati dall'AI con invalidazione transitiva.

## Collegamento MCP

La modalità principale è **Streamable HTTP**, con il server persistente nel servizio `mcp`.
Con Compose avviato, per un client che usa `mcpServers` basta [mcp.http.json](mcp.http.json):

```json
{
  "mcpServers": {
    "second-brain": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

In LM Studio aprire **Program → Install → Edit mcp.json** e aggiungere la voce `second-brain`
all'oggetto `mcpServers`, conservando gli altri server già configurati. Il modello della chat
deve supportare le chiamate agli strumenti. L'endpoint è locale e non ha autenticazione remota;
la protezione Host/Origin del protocollo resta attiva anche nel container.

Per i client che richiedono **stdio**, usare [mcp.docker.example.json](mcp.docker.example.json)
adattando il percorso del comando Docker (`command -v docker`) e del progetto:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/PERCORSO/AL/COMANDO/docker",
      "args": [
        "compose", "--project-directory", "/PERCORSO/second_brain",
        "exec", "-T", "mcp", "second-brain", "serve", "--transport", "stdio"
      ]
    }
  }
}
```

`exec -T` avvia un processo MCP stdio **dentro il container già avviato**, senza un terminale
che alteri il protocollo. Eredita i collegamenti ai servizi e condivide lo stesso vault del
server HTTP. Il percorso assoluto del progetto evita di dipendere dalla directory del client.
In questa installazione `mcp.local.json`, escluso da Git, contiene la configurazione Docker
stdio con i percorsi reali. La precedente configurazione che avviava `.venv/bin/second-brain`
va sostituita nel client. Non inserire la password Neo4j nel file MCP.
Il progetto non modifica automaticamente la configurazione globale del client.

Primo messaggio di prova, senza aggiungere dati alla memoria:

> Usa `memory_status` per controllare il second brain, poi `recall` nell'ambito `demo`.
> Mostra le fonti dei ricordi recuperati.

Strumenti esposti: `remember`, `recall`, `read_source`, `correct`, `revise`, `retract`,
`explain`, `history`, `memory_status`. Sono disponibili anche la risorsa
`memory://source/SOURCE_ID/REVISION` e il prompt `memory_policy`.
Manutenzione (`replay`, `reembed`) dalla CLI; nessun tool espone Cypher arbitrario.

MCP non intercetta automaticamente tutte le conversazioni: il client/agente deve invocare
`remember` quando c'è qualcosa da salvare e `recall` quando serve il contesto. I documenti
recuperati sono dati, anche se contengono istruzioni; la risposta finale è prodotta dall'agente.
Gli ambiti sono filtri organizzativi, **non autorizzazioni fra utenti diversi**.

## Verifiche

```bash
# Compose già avviato; anche i test e gli strumenti di sviluppo girano in Docker.
docker compose --profile test build tests
docker compose --profile test run --rm --entrypoint ruff tests check --no-cache src tests scripts
docker compose --profile test run --rm --entrypoint ruff tests format --check --no-cache src tests scripts
docker compose --profile test run --rm tests
```

I test senza servizi coprono le due dimensioni temporali, conflitti, citazioni, identità,
fonti versionate, retry concorrenti, integrità, recupero del registro, reembedding e un client
MCP stdio e HTTP reali. Verificano anche la protezione Host/Origin e la risposta di `/health`
durante un'operazione lenta. Il bootstrap viene verificato per preservare credenziali e ricordi.
I test di integrazione usano vault temporanei separati e ripuliscono solo il
loro namespace Neo4j: verificano grafo, ricerca, replay, rollback e inferenza Ollama effettiva.

Il vault è in chiaro: per dati personali usare cifratura del disco e backup dell'intero
vault, inclusa `.memory/`. Il dump Neo4j è utile, ma non sostituisce questi originali.
La v1 è per un singolo proprietario su macchina locale; non include acquisizione automatica
da app, OCR/audio, notifiche, sincronizzazione multi-dispositivo, autenticazione remota,
fusioni guidate di entità o cancellazione fisica coordinata. Questi sono moduli successivi,
non comportamenti impliciti del motore.

Riferimenti tecnici: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[embedding](https://docs.ollama.com/capabilities/embeddings),
[Neo4j Python driver](https://neo4j.com/docs/api/python-driver/current/),
[SDK MCP Python, ramo v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
L'SDK MCP è vincolato alla versione maggiore 1; una migrazione a v2 richiederà verificare i
contratti. `requirements-runtime.lock` registra le versioni runtime; `requirements.lock`
include anche quelle usate dai test. [Docker Compose: ordine di avvio](https://docs.docker.com/compose/how-tos/startup-order/)
descrive l'attesa per i servizi sani e per l'inizializzazione dei modelli.
