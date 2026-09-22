"""The fitness function that keeps the event catalogue honest.

Run it in CI beside the linters. Like them it **reads source rather than
running it**, which is the whole point: it sees the log line in an ``except``
branch no test exercises, and the one in a module nothing imports yet.

It knows nothing about an application's layout — it is handed the package roots
to read::

    python -m dspt_logging.check src/

Seven things are refused:

1. **An event name written by hand**, anywhere outside a catalogue. Matched by
   *position*, not by shape: a string that is the value of an ``"event"`` key,
   or that is handed to a logging helper. The shape — a lowercase dotted string
   — was tried first and flagged 156 strings in one repository, most of them
   foreign keys (``users.id``), logger names (``uvicorn.error``), legal
   references and file names. Position flagged 85, and every one was real. A
   check with a long allow-list is a check that gets silenced.
2. **The same event name declared twice.** Whichever won, half the lines would
   be filed under the other one's meaning. The five events this package emits
   count as declared, so an application neither redeclares nor rewrites them.
3. **A field name used by events in more than one package** that is not in
   ``SHARED_FIELDS``. This is the check the catalogue exists for: ``method``
   meant the request the API served in one package and the request it made in
   another, and a panel counting our 404s counted a vendor's too. Mechanical —
   is the name in the dict — but its effect is human, because reusing a name
   means going to read what it already means.
4. **A name that is not ``domain.object.action``.**
5. **A field the formatter already owns** (``RESERVED_FIELDS``): ``request_id``
   declared for a vendor's identifier would pass the contract and overwrite the
   HTTP request id on every line.
6. **A catalogue the check cannot read.** It reads source, so an event built by
   a helper (``_security(name, message)``) or a field set it cannot resolve to
   strings would silently fall out of rules 2, 3 and 5 — which is what happened
   to twelve security events and nineteen field sets before this rule existed.
   A module-level constant (``_KEY_FIELDS = frozenset({...})``) and a union of
   them (``_DRAFT | {"sections"}``) are resolved; anything else is refused. An
   ``Event(...)`` outside a catalogue is refused for the same reason.
7. **An ERROR, WARNING, EXCEPTION or CRITICAL line with no ``event``.** Without
   a name an error reaches the log platform as free text: you can read it if
   you already know what to look for, but you cannot count it, group it or
   alert on it. It holds for warnings too, because a warning is by definition
   something someone must be able to count. INFO is deliberately out: there the
   rule is not absolute, and a checker cannot make the call for you.

A field used twice inside a single package needs no declaration: it cannot
collide with anything a dashboard filters across events.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dspt_logging import events as _events
from dspt_logging.events import (
    NAME_PATTERN,
    PLATFORM_EVENTS,
    RESERVED_FIELDS,
    SHARED_FIELDS,
)

#: Callables whose string arguments would reach the formatter as an event name.
_HELPER_PREFIX: Final[str] = "_log"
_HELPERS: Final[frozenset[str]] = frozenset({"log_event"})

#: The four levels a line has to be named at (rule 7).
_NAMED_LEVELS: Final[frozenset[str]] = frozenset({"error", "warning", "exception", "critical"})

#: Directories that are never application source: a test fixture logs on
#: purpose, a migration is a one-off script, and a virtualenv is somebody
#: else's code.
SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", ".git", "alembic", "migrations", "tests"}
)

_REMEDY: Final[str] = (
    "declare it in the package's events.py as an Event and emit it with log_event()"
)

_PLATFORM_SOURCE: Final[str] = "dspt_logging.events"

#: The real file behind that name, so that a tree which happens to contain this
#: package's own catalogue is not told it duplicates itself.
_PLATFORM_FILE: Final[Path] = Path(str(_events.__file__)).resolve()


@dataclass(frozen=True)
class Finding:
    """One refusal: where it is, which rule, and what to do about it."""

    path: str
    lineno: int
    rule: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: [rule {self.rule}] {self.message}"


def _is_catalogue(path: Path) -> bool:
    return path.name == "events.py" or path.name.endswith("_events.py")


class _Unresolvable(Exception):
    """A field set the check cannot reduce to strings by reading the source."""


def _constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``NAME = <expr>`` assignments, by name."""
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            out[node.target.id] = node.value
    return out


def _string_set(node: ast.AST, constants: dict[str, ast.expr]) -> set[str]:
    """The strings in a ``frozenset({...})``, a bare ``{...}``, a module-level
    constant holding one, or a ``|`` union of those. Anything else raises: a set
    the check cannot read is a set the shared-field rule cannot see."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"frozenset", "set"}:
        if not node.args:
            return set()
        return _string_set(node.args[0], constants)
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        out: set[str] = set()
        for element in node.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise _Unresolvable
            out.add(element.value)
        return out
    if isinstance(node, ast.Name) and node.id in constants:
        return _string_set(constants[node.id], constants)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _string_set(node.left, constants) | _string_set(node.right, constants)
    raise _Unresolvable


@dataclass(frozen=True)
class _Declared:
    name: str
    fields: frozenset[str]
    lineno: int


def _declared_events(tree: ast.Module, path: str) -> tuple[list[_Declared], list[Finding]]:
    """Every ``Event(...)`` in a catalogue, and every reason one could not be read."""
    events: list[_Declared] = []
    problems: list[Finding] = []
    constants = _constants(tree)

    for node in tree.body:
        value = getattr(node, "value", None)
        if not (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Call)):
            continue
        # A plain name only: `re.compile(...)` and the like are attribute calls
        # and build no event; a local helper (`_security(...)`) is a name.
        if not isinstance(value.func, ast.Name) or value.func.id in {"frozenset", "set"}:
            continue
        if value.func.id != "Event":
            problems.append(
                Finding(
                    path,
                    node.lineno,
                    6,
                    f"{ast.unparse(value)[:60]} — a catalogue declares its events with "
                    "`Event(...)` literally, so this check can read them; one built by a "
                    "helper falls out of the duplicate-name and shared-field rules",
                )
            )
            continue

        name = ""
        fields: set[str] = set()
        for keyword in value.keywords:
            if keyword.arg == "name":
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    name = keyword.value.value
                else:
                    problems.append(
                        Finding(path, node.lineno, 6, "an Event's name must be a string literal")
                    )
            elif keyword.arg in {"required", "optional"}:
                try:
                    fields |= _string_set(keyword.value, constants)
                except _Unresolvable:
                    problems.append(
                        Finding(
                            path,
                            node.lineno,
                            6,
                            f"{name or 'an event'}'s {keyword.arg} field set "
                            f"({ast.unparse(keyword.value)}) cannot be read from the source — "
                            "write it as `frozenset({...})`, a module-level constant holding "
                            "one, or a `|` of those",
                        )
                    )
        if not name:
            if not any(keyword.arg == "name" for keyword in value.keywords):
                problems.append(
                    Finding(path, node.lineno, 6, "an Event is declared without a `name=` keyword")
                )
            continue
        reserved = sorted(fields & RESERVED_FIELDS)
        if reserved:
            problems.append(
                Finding(
                    path,
                    node.lineno,
                    5,
                    f"event {name!r} declares {', '.join(reserved)}, which the formatter "
                    "already fills from the logging context or as the line's own keys — name "
                    "the thing you mean (`vendor_request_id`, `target_user_id`)",
                )
            )
        events.append(_Declared(name, frozenset(fields), node.lineno))
    return events, problems


def _handwritten(tree: ast.AST, path: str) -> list[Finding]:
    """Event names written as string literals where they would be emitted."""
    out: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "event"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    out.append(
                        Finding(
                            path,
                            value.lineno,
                            1,
                            f"event {value.value!r} is written by hand — {_REMEDY}",
                        )
                    )
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called.startswith(_HELPER_PREFIX) or called in _HELPERS:
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and "." in arg.value
                    ):
                        out.append(
                            Finding(
                                path,
                                arg.lineno,
                                1,
                                f"event {arg.value!r} is written by hand — {_REMEDY}",
                            )
                        )
    return out


def _declared_outside(tree: ast.AST, path: str) -> list[Finding]:
    """``Event(...)`` constructed anywhere but a catalogue."""
    out: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Event":
            names = [
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
            ]
            name = str(names[0]) if names else "<unnamed>"
            out.append(
                Finding(
                    path,
                    node.lineno,
                    6,
                    f"event {name!r} is declared outside a catalogue — the duplicate-name and "
                    "shared-field rules cannot see it there; move it to the package's events.py",
                )
            )
    return out


def _unnamed_severe_lines(tree: ast.AST, path: str) -> list[Finding]:
    """``logger.error``/``.warning``/``.exception``/``.critical`` with no ``event``."""
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _NAMED_LEVELS:
            continue
        receiver = node.func.value
        # `Adapter(logger, ctx).error(...)`: the receiver is itself a call, and
        # the name that says "this is a logger" is the one being called. One
        # such ERROR line hid from every check for months because only names
        # and attributes were looked at.
        if isinstance(receiver, ast.Call):
            receiver = receiver.func
        target = getattr(receiver, "id", None) or getattr(receiver, "attr", "")
        if "log" not in str(target).lower():
            continue
        extra = [keyword for keyword in node.keywords if keyword.arg == "extra"]
        if not extra:
            out.append(
                Finding(
                    path,
                    node.lineno,
                    7,
                    f"a {node.func.attr.upper()} line with no event — {_REMEDY}",
                )
            )
            continue
        value = extra[0].value
        if not isinstance(value, ast.Dict):
            continue  # `extra` passed as a variable: nothing to read here
        if any(key is None for key in value.keys):
            continue  # `extra={**fields, ...}`: the event may be in the fields
        if not any(isinstance(key, ast.Constant) and key.value == "event" for key in value.keys):
            out.append(
                Finding(
                    path,
                    node.lineno,
                    7,
                    f"a {node.func.attr.upper()} line with no event — {_REMEDY}",
                )
            )
    return out


def _sources(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not SKIPPED_DIRECTORIES & set(path.relative_to(root).parts[:-1])
    )


def check_catalogue(roots: Iterable[Path | str]) -> list[Finding]:
    """Every problem found. An empty list means the tree is clean."""
    problems: list[Finding] = []
    owner: dict[str, str] = {event.name: _PLATFORM_SOURCE for event in PLATFORM_EVENTS}
    field_packages: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)

    for root in roots:
        root_path = Path(root)
        package = root_path.name
        for path in _sources(root_path):
            shown = str(path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue

            problems.extend(_unnamed_severe_lines(tree, shown))

            if _is_catalogue(path):
                events, unreadable = _declared_events(tree, shown)
                problems.extend(unreadable)
                for event in events:
                    if not NAME_PATTERN.match(event.name):
                        problems.append(
                            Finding(
                                shown,
                                event.lineno,
                                4,
                                f"event name {event.name!r} is not domain.object.action "
                                "(lowercase words separated by dots, at least two)",
                            )
                        )
                    previous = owner.get(event.name)
                    if previous == _PLATFORM_SOURCE and path.resolve() == _PLATFORM_FILE:
                        continue
                    if previous is not None:
                        problems.append(
                            Finding(
                                shown,
                                event.lineno,
                                2,
                                f"event {event.name!r} is already declared in "
                                f"{previous} — one name, one meaning, one package",
                            )
                        )
                    else:
                        owner[event.name] = shown
                    for name in event.fields:
                        field_packages[name].setdefault(package, (shown, event.lineno))
                continue

            problems.extend(_handwritten(tree, shown))
            problems.extend(_declared_outside(tree, shown))

    for name, packages in sorted(field_packages.items()):
        if len(packages) > 1 and name not in SHARED_FIELDS:
            where, lineno = packages[sorted(packages)[-1]]
            problems.append(
                Finding(
                    where,
                    lineno,
                    3,
                    f"field {name!r} is used by events in {', '.join(sorted(packages))} but is "
                    "not in SHARED_FIELDS — add it there with what it means, so the next "
                    "package to reuse the name has to read it first",
                )
            )

    return sorted(problems, key=lambda finding: (finding.path, finding.lineno, finding.rule))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dspt_logging.check",
        description="Check the log event catalogue of one or more package roots.",
    )
    parser.add_argument("roots", nargs="+", type=Path, help="package roots to read")
    arguments = parser.parse_args(argv)

    roots = [root for root in arguments.roots if root.is_dir()]
    missing = [str(root) for root in arguments.roots if not root.is_dir()]
    for root in missing:
        print(f"Event catalogue: {root} is not a directory.")
    if missing:
        return 1

    problems = check_catalogue(roots)
    if not problems:
        print(f"Event catalogue: clean ({len(roots)} package(s)).")
        return 0
    print(f"Event catalogue: {len(problems)} problem(s).\n")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
