# Changelog

Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e le
versioni [SemVer](https://semver.org/lang/it/). Le applicazioni si appuntano a
un tag, quindi ogni voce qui è una cosa che cambia quando qualcuno alza il tag.

## [0.1.2] - 2026-09-22

### Aggiunto
- fixture pytest `log_lines`: le righe come le vedrebbe Fluent Bit (chiavi
  fisse, contesto, campi, `message` reso), per lo smoke test attraverso
  l'applicazione vera; `log_records` resta per asserire sui record.
- `log_event(..., exc_info=exc)` accetta l'eccezione stessa, come `logging`:
  un helper che la riceve come argomento non deve rilanciarla per passare
  `True`.

### Corretto
- la regola 7 del controllo vede anche `Adapter(logger, ctx).error(...)`: il
  ricevente della chiamata era una chiamata a sua volta e nessun controllo
  guardava quella riga.

## [0.1.1] - 2026-09-22

### Aggiunto
- `install_request_logging(..., silent_user_agents=(...))`: nessuna riga di
  accesso per le richieste il cui user agent inizia con uno dei prefissi dati,
  qualunque sia la risposta. Serve a un'applicazione che sonda una propria
  route pubblica (l'handshake del webhook WhatsApp, ogni due minuti per
  tenant): non puo' dichiararla probe, perche' la usano anche i chiamanti veri,
  e allora si firma nello user agent.

## [0.1.0] - 2026-09-22

Prima versione: il contratto estratto dai due backend che lo avevano gia',
dove era scritto due volte, messo in un posto solo.

### Aggiunto

- `configure_logging(service, tenant=None, level=None)`: un handler JSON su
  stdout, idempotente; livello da argomento, `LOG_LEVEL` o INFO; access log dei
  server spento, narrazione di avvio a WARNING, traceback ASGI duplicato
  scartato, librerie chiacchierone a WARNING, boilerplate Alembic scartato e
  migrazione nominata, warning Python in JSON.
- `JsonFormatter` e `MergingLoggerAdapter`.
- `dspt_logging.context`: sei campi chiusi (`request_id`, `user_id`, `tenant`,
  `job`, `thread_id`, `turn_id`), `bind`/`unbind`/`bound`/`current`.
- `dspt_logging.events`: `Event`, `log_event`, `SHARED_FIELDS`,
  `RESERVED_FIELDS` e i quattro eventi di piattaforma (`http.access`,
  `api.unhandled`, `python.warning`, `migration.applied`).
- `dspt_logging.asgi`: `RequestContextMiddleware`, `install_request_logging`,
  `request_identity` — request id riusato ed echeggiato, IP del chiamante,
  user agent troncato, route e path mascherato, durata, probe silenziose
  quando rispondono 2xx, 404 da scanner a DEBUG, eccezione non gestita
  nominata `api.unhandled` e rilanciata.
- `python -m dspt_logging.check <root>...`: sette regole sul catalogo.
- `dspt_logging.testing`: fixture `log_records` (plugin pytest registrato) e
  `assert_no_secret`.

[0.1.0]: https://github.com/Digitale-Semplice-Per-Tutti/dspt-logging/releases/tag/v0.1.0
