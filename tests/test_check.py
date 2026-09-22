"""The seven rules, each against a tree written for it.

The check reads source rather than running it, so it sees a log line in an
`except` branch no test exercises — which is the whole reason it is not a unit
test of the emitting code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dspt_logging.check import check_catalogue, main

# No fields: the shared-field rule is exercised by its own fixtures, and a
# default that declared one would make every other test trip it by accident.
CATALOGUE = """
from dspt_logging import Event

SENT = Event(name="thing.item.sent", message="Sent")
"""


def _pkg(tmp_path: Path, name: str, catalogue: str = CATALOGUE, **modules: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "events.py").write_text(catalogue)
    for filename, body in modules.items():
        (root / f"{filename}.py").write_text(body)
    return root


def _messages(problems: list) -> str:  # type: ignore[type-arg]
    return "\n".join(str(problem) for problem in problems)


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
from dspt_logging import log_event
from alpha.events import SENT

def go(logger):
    log_event(logger, SENT)
""",
    )
    assert check_catalogue([pkg]) == []


# --- rule 1: a name written by hand ------------------------------------------


def test_a_hand_written_event_name_is_refused(tmp_path: Path) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
def go(logger):
    logger.info("Sent", extra={"event": "thing.item.sent", "item_id": 1})
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 1
    assert "thing.item.sent" in problem.message
    assert problem.path.endswith("service.py")
    assert problem.lineno == 3


def test_a_name_handed_to_a_helper_is_refused_too(tmp_path: Path) -> None:
    """The string reaches the formatter through a helper rather than as an
    `extra={"event": ...}` pair, which a grep for `extra=` cannot see."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
def _log_thing(event, item_id):
    ...

def go():
    _log_thing("thing.item.sent", 1)
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 1


def test_a_dotted_string_that_is_not_an_emission_is_left_alone(tmp_path: Path) -> None:
    """Foreign keys, logger names, legal references and file names are
    lowercase and dotted too. Matching the shape flagged 156 strings in one
    repository; matching the position flagged 85, and every one was real."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
import logging

FK = "users.id"
NORM = "decreto.legislativo"
FILE = "spec.yaml"
logger = logging.getLogger("uvicorn.error")
""",
    )
    assert check_catalogue([pkg]) == []


# --- rule 2: one name, one meaning -------------------------------------------


def test_two_packages_declaring_the_same_event_are_refused(tmp_path: Path) -> None:
    a = _pkg(tmp_path, "alpha")
    b = _pkg(tmp_path, "beta")
    (problem,) = check_catalogue([a, b])
    assert problem.rule == 2
    assert "thing.item.sent" in problem.message
    assert "alpha" in problem.message and "beta" in problem.path


def test_redeclaring_a_platform_event_is_refused(tmp_path: Path) -> None:
    """The package emits `http.access` itself; a second declaration would file
    half the fleet's access lines under another meaning."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
ACCESS = Event(name="http.access", message="Mine")
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 2
    assert "dspt_logging.events" in problem.message


# --- rule 3: a field two packages share --------------------------------------


def test_a_field_two_packages_share_must_be_declared_shared(tmp_path: Path) -> None:
    """The check that would have caught `method` meaning two things."""
    a = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
SENT = Event(name="alpha.item.sent", message="Sent", required=frozenset({"weight"}))
""",
    )
    b = _pkg(
        tmp_path,
        "beta",
        catalogue="""
from dspt_logging import Event
GONE = Event(name="beta.item.gone", message="Gone", optional=frozenset({"weight"}))
""",
    )
    (problem,) = check_catalogue([a, b])
    assert problem.rule == 3
    assert "weight" in problem.message
    assert "SHARED_FIELDS" in problem.message


def test_a_field_already_in_the_vocabulary_may_be_reused(tmp_path: Path) -> None:
    a = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
SENT = Event(name="alpha.item.sent", message="Sent", required=frozenset({"duration_ms"}))
""",
    )
    b = _pkg(
        tmp_path,
        "beta",
        catalogue="""
from dspt_logging import Event
GONE = Event(name="beta.item.gone", message="Gone", optional=frozenset({"duration_ms"}))
""",
    )
    assert check_catalogue([a, b]) == []


def test_a_field_used_twice_inside_one_package_needs_no_declaration(tmp_path: Path) -> None:
    """It cannot collide with anything a dashboard filters across events."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
SENT = Event(name="alpha.item.sent", message="Sent", required=frozenset({"weight"}))
GONE = Event(name="alpha.item.gone", message="Gone", required=frozenset({"weight"}))
""",
    )
    assert check_catalogue([pkg]) == []


# --- rule 4: the shape of a name ---------------------------------------------


def test_a_badly_shaped_name_is_refused(tmp_path: Path) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
SENT = Event(name="Sent", message="Sent")
""",
    )
    problems = check_catalogue([pkg])
    assert [problem.rule for problem in problems] == [4]


# --- rule 5: a field the formatter owns --------------------------------------


def test_a_field_the_formatter_owns_is_refused(tmp_path: Path) -> None:
    """`request_id` declared for a vendor's identifier is a field the contract
    accepts and the formatter then overwrites the HTTP id with."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
SENT = Event(name="alpha.item.sent", message="Sent", required=frozenset({"request_id"}))
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 5
    assert "request_id" in problem.message


# --- rule 6: a catalogue the check cannot read -------------------------------


def test_a_field_set_held_in_a_constant_or_a_union_is_read(tmp_path: Path) -> None:
    """The forms a real catalogue uses: a module-level frozenset shared by
    several events, and a `|` that extends it."""
    a = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
_BASE = frozenset({"weight"})
SENT = Event(name="alpha.item.sent", message="Sent", required=_BASE | {"item_id"})
""",
    )
    b = _pkg(
        tmp_path,
        "beta",
        catalogue="""
from dspt_logging import Event
_BASE = frozenset({"weight"})
GONE = Event(name="beta.item.gone", message="Gone", optional=_BASE)
""",
    )
    (problem,) = check_catalogue([a, b])
    assert problem.rule == 3
    assert "weight" in problem.message


def test_an_event_built_by_a_helper_is_refused(tmp_path: Path) -> None:
    """Twelve events once built by a helper were invisible to the check, so a
    duplicate of any of them would have passed CI."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event

def _security(name, message):
    return Event(name=name, message=message, required=frozenset({"outcome"}))

LOGIN = _security("alpha.login.failed", "Login refused")
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 6
    assert "_security" in problem.message


def test_a_field_set_the_check_cannot_read_is_refused(tmp_path: Path) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        catalogue="""
from dspt_logging import Event
from somewhere import FIELDS
SENT = Event(name="alpha.item.sent", message="Sent", required=FIELDS)
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 6
    assert "cannot be read" in problem.message


def test_an_event_declared_outside_a_catalogue_is_refused(tmp_path: Path) -> None:
    """The duplicate-name rule reads catalogues only, so an `Event(...)` in a
    service module could carry a name another package already owns."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
from dspt_logging import Event, log_event
LOCAL = Event(name="thing.item.sent", message="Sent again")

def go(logger):
    log_event(logger, LOCAL)
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 6
    assert "outside a catalogue" in problem.message


# --- rule 7: every ERROR and every WARNING has a name ------------------------


def test_an_error_without_an_event_is_refused(tmp_path: Path) -> None:
    """Without a name an error arrives as free text: you can read it if you
    already know what to look for, but you cannot count it or alert on it."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
import logging
logger = logging.getLogger(__name__)

def go():
    logger.error("Sending failed")
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 7
    assert problem.lineno == 6


@pytest.mark.parametrize("level", ["error", "warning", "exception", "critical"])
def test_every_severe_level_is_covered(tmp_path: Path, level: str) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        service=f"""
import logging
logger = logging.getLogger(__name__)

def go():
    logger.{level}("Sending failed", extra={{"attempts": 3}})
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 7


def test_an_info_line_is_left_alone(tmp_path: Path) -> None:
    """There the rule is not absolute — an event or DEBUG — and a checker
    cannot make that call for us."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
import logging
logger = logging.getLogger(__name__)

def go():
    logger.info("nothing to see")
""",
    )
    assert check_catalogue([pkg]) == []


def test_a_named_error_and_one_emitted_through_log_event_pass(tmp_path: Path) -> None:
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
import logging
from dspt_logging import log_event
from alpha.events import SENT

logger = logging.getLogger(__name__)

def go(fields):
    logger.error("Sending failed", extra={"event": "alpha.item.failed"})
    logger.warning("Sending failed", extra=fields)
    logger.warning("Sending failed", extra={**fields, "attempts": 1})
    log_event(logger, SENT, level=logging.ERROR)
""",
    )
    problems = check_catalogue([pkg])
    assert [problem.rule for problem in problems] == [1]


# --- the command line --------------------------------------------------------


def test_the_command_exits_zero_on_a_clean_tree(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    pkg = _pkg(tmp_path, "alpha")
    assert main([str(pkg)]) == 0
    assert "clean" in capsys.readouterr().out


def test_the_command_prints_the_file_the_line_and_the_rule(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
def go(logger):
    logger.info("Sent", extra={"event": "thing.item.sent"})
""",
    )
    assert main([str(pkg)]) == 1
    out = capsys.readouterr().out
    assert "service.py:3" in out
    assert "[rule 1]" in out
    assert "log_event" in out


def test_a_missing_root_is_an_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([str(tmp_path / "nope")]) == 1


def test_tests_and_migrations_are_not_application_source(tmp_path: Path) -> None:
    """A test fixture logs on purpose and a migration is a one-off script."""
    pkg = _pkg(tmp_path, "alpha")
    for directory in ("tests", "alembic"):
        sub = pkg / directory
        sub.mkdir()
        (sub / "thing.py").write_text('import logging\nlogging.getLogger(__name__).error("boom")\n')
    assert check_catalogue([pkg]) == []


def test_the_packages_own_catalogue_does_not_duplicate_itself() -> None:
    """Pointing the check at a tree that contains this package is not an error:
    its four events are declared there, which is where they come from."""
    import dspt_logging

    root = Path(str(dspt_logging.__file__)).parent
    assert [problem for problem in check_catalogue([root]) if problem.rule == 2] == []


def test_an_error_on_an_adapter_built_in_the_same_expression_is_seen(tmp_path: Path) -> None:
    """`Adapter(logger, ctx).error(...)` is still an ERROR line: the call the
    rule inspects is the outer one, and its receiver is a call, not a name.
    One such line hid from every check for months."""
    pkg = _pkg(
        tmp_path,
        "alpha",
        service="""
import logging
from dspt_logging import MergingLoggerAdapter

logger = logging.getLogger(__name__)

def go(job_id):
    MergingLoggerAdapter(logger, {"job_id": job_id}).error("Cancel failed")
""",
    )
    (problem,) = check_catalogue([pkg])
    assert problem.rule == 7
