# dspt-logging

Il contratto di logging condiviso dai backend DSPT: una riga JSON per record,
un contesto di richiesta che segue il lavoro, un catalogo di eventi che un
controllo automatico tiene onesto.

Sta in un pacchetto solo, versionato, perché la stessa cosa scritta a mano in
sei repository diverge in una settimana: quattro nomi evento diversi per lo
stesso fatto, tre varianti del formatter, due meccanismi di controllo. Qui una
regola nuova si scrive una volta e arriva ovunque con un bump di versione.

- Python ≥ 3.11.
- **Zero dipendenze a runtime**: solo libreria standard. Il middleware è ASGI
  puro, non importa Starlette né FastAPI.
- Tipizzato (`py.typed`).

## Installare

Nel `pyproject.toml` dell'applicazione, appuntato a un tag:

```toml
dependencies = [
    "dspt-logging @ git+https://github.com/Digitale-Semplice-Per-Tutti/dspt-logging@v0.1.1",
]
```

`uv` risolve da git, senza registry. Si aggiorna cambiando il tag: è
volontario, così una regola nuova non arriva in produzione di sorpresa.

## Montarlo in un'applicazione: tre righe

```python
from dspt_logging import configure_logging, install_request_logging

configure_logging("esempio-backend", tenant=TENANT_SLUG)  # all'avvio del processo
app = FastAPI()
install_request_logging(app, quiet_404_outside=("/api", "/static"))
```

E nell'entrypoint del server: `uvicorn ... --no-access-log` (la riga di accesso
la scrive il middleware, che conosce durata, request id, IP e route).

Le opzioni di `install_request_logging`: `probe_paths` (prefissi delle probe,
silenziose quando rispondono 2xx), `masked_params` (nomi dei parametri di
route da mascherare in `path`), `quiet_404_outside` (i 404 fuori da questi
prefissi vanno a DEBUG: scanner), `silent_user_agents` (prefissi di user agent
che non lasciano mai una riga: la probe interna di un'app verso una propria
route pubblica, che non si può dichiarare probe perché la usano anche i
chiamanti veri).

Chi esegue Alembic nello stesso processo passa
`fileConfig(..., disable_existing_loggers=False)`, altrimenti la
configurazione di Alembic spegne questa.

## La riga

Chiavi fisse, sempre presenti, in quest'ordine:

| Chiave | Cosa dice |
| --- | --- |
| `timestamp` | UTC, millisecondi, suffisso `Z` |
| `level` | `INFO`, `WARNING`, `ERROR`, ... |
| `logger` | il modulo che ha emesso |
| `service` | il nome del processo (`esempio-backend`) |
| `event` | il nome dell'evento, `application.log` se nessuno l'ha dato |
| `message` | una frase fissa, non testo composto |
| `tenant` | lo slug del comune servito, o `null` |

Poi i campi del contesto legati alla richiesta (`request_id`, `user_id`,
`tenant`, `job`, `thread_id`, `turn_id`), poi i campi dell'evento in ordine
alfabetico. Un `extra` esplicito vince sul contesto. Su eccezione arrivano
`error_type` (il nome della classe) e `stack_trace` (i soli frame).

Valori: interi e float restano numeri, `None` resta `null`, dizionari e liste
sono ricorsivi, tutto il resto diventa stringa. **Una misura che nessuno ha
preso si omette**, mai `0` né `null`: lo zero è un numero plausibile e viene
creduto, e una dashboard lo media in un percentile o lo somma in un totale.

Per la riga di accesso il `message` è reso (`"GET /api/ping 200 12ms"`), perché
le viste testuali, gli export CSV e le regole di alert vedono solo quello.

### Cosa non può finire in un log

Le righe sono conservate due anni. Mai il nome o il codice fiscale di un
cittadino, il corpo di una richiesta, un prompt, una risposta generata, uno
username, la risposta grezza di un fornitore, la query string.

In pratica: mai f-string con dati dentro, mai `str(e)`, mai `response.text`;
conteggi al posto delle liste; le eccezioni con `exc_info=True`, che il
formatter riduce a tipo più frame.

### I livelli

| Livello | Significato |
| --- | --- |
| `ERROR` | qualcuno deve guardare adesso |
| `WARNING` | non ha funzionato, il sistema ha retto (un 5xx, un login fallito, una chiamata al fornitore fallita, la readiness giù) |
| `INFO` | un fatto da contare, incluse le azioni amministrative legittime |

Il livello del processo viene dall'argomento `level`, altrimenti dalla
variabile d'ambiente `LOG_LEVEL`, altrimenti `INFO`. Un valore illeggibile
(`VERBOSE`) non ferma il processo, che si scrive a mano in un ConfigMap e un
refuso non deve buttare giù tutti i pod: il processo parte a `INFO` e lo dice
nella sua prima riga, a `WARNING`, con l'evento `logging.level.invalid`.

## Dichiarare un evento

Il catalogo **non** sta nel pacchetto: ogni applicazione ha il suo `events.py`.
Il pacchetto possiede il vocabolario dei campi condivisi, i nomi riservati e i
cinque eventi che emette da sé (`http.access`, `api.unhandled`, `logging.level.invalid`,
`python.warning`, `migration.applied`).

```python
# esempio/events.py
from typing import Final
import logging
from dspt_logging import Event

APPUNTAMENTO_PRENOTATO: Final = Event(
    name="appointment.booking.completed",  # domain.object.action
    message="Appointment booked",  # la frase, una sola volta
    required=frozenset({"appointment_id", "channel"}),
    optional=frozenset({"duration_ms"}),
)

APPUNTAMENTO_RIFIUTATO: Final = Event(
    name="appointment.booking.refused",
    message="Appointment refused",
    level=logging.WARNING,
    required=frozenset({"appointment_id", "channel", "reason"}),
)
```

Al punto di emissione non si scrive né il nome né la frase:

```python
from dspt_logging import log_event
from esempio.events import APPUNTAMENTO_PRENOTATO

log_event(logger, APPUNTAMENTO_PRENOTATO, appointment_id=identificativo, channel="web")
```

Una riga che rompe il proprio contratto **solleva sotto pytest e mai in
produzione**: là esce comunque, marcata con `contract_violation`, così il
difetto si vede sulla dashboard invece di costare una richiesta.

### Il contesto

```python
from dspt_logging import bound

with bound(tenant=slug, user_id=utente.id):
    ...
```

Sei nomi e non uno di più: `request_id`, `user_id`, `tenant`, `job`,
`thread_id`, `turn_id`. Un nome fuori lista è `TypeError` all'istante. Sono
`contextvars`, quindi seguono le task asyncio e reggono uno stream SSE che dura
minuti. Il `request_id` lo lega il middleware: nessuno deve ricordarsene.

## Far girare il controllo

```console
$ uv run python -m dspt_logging.check src/
Event catalogue: clean (1 package(s)).
```

Legge il sorgente (AST, non runtime), quindi vede anche la riga di log nel ramo
`except` che nessun test esercita. Esce con 1 e stampa `file:riga [regola N]`
per ogni problema. Le sette regole:

1. nessun nome evento scritto a mano fuori dal catalogo;
2. nessun nome dichiarato due volte (i cinque del pacchetto compresi);
3. un campo usato da eventi di più pacchetti deve stare in `SHARED_FIELDS`;
4. forma del nome: `domain.object.action`, minuscolo, almeno due segmenti;
5. nessun campo riservato dichiarato (`request_id`, `tenant`, `message`, ...);
6. un catalogo che il controllo non sa leggere (eventi costruiti da un helper,
   insiemi di campi non risolvibili, `Event(...)` fuori dal catalogo);
7. ogni `logger.error`/`.warning`/`.exception`/`.critical` ha un `event`.

Un passo in ogni CI. Cartelle saltate: `tests`, `alembic`, `migrations`,
`.venv`, `node_modules`, `__pycache__`.

## Nei test dell'applicazione

Il pacchetto si registra come plugin pytest: la fixture c'è senza dichiarare
nulla.

```python
from dspt_logging.testing import assert_no_secret


def test_la_risposta_non_finisce_nel_log(log_records):
    servizio.rispondi("Via Roma 1")
    assert_no_secret(log_records, "Via Roma 1")
```

`assert_no_secret` guarda il messaggio, tutti i campi strutturati e i frame
dello stack: "tanto sta solo nel traceback" non è un'attenuante.

## L'esempio

`examples/` è un'applicazione minima montata come sopra, con il suo catalogo.
La CI del pacchetto ci fa girare sopra il controllo, così una regressione del
checker si vede qui e non in un'applicazione.

## Sviluppo

```console
$ uv sync
$ uv run pytest
$ uv run ruff check . && uv run ruff format --check .
$ uv run mypy src
$ uv run python -m dspt_logging.check examples
```

## Licenza

MIT, Digitale Semplice Per Tutti.
