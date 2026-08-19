"""A small, honest subset of JSON path traversal.

Deliberately not JSONPath. The full standard has filters, recursive descent,
slices and script expressions, and every one of them is a way for a profile to
express something nobody can predict the cost or result of. What a Site Profile
actually needs is short and testable:

.. code-block:: text

    data.character.name      nested objects
    data.rows.0.name         array index
    data.items               a whole list, returned as a list
    data.players[*].name     one field from every element

Values come back as the Python types they are. A score of ``93`` stays an int, a
list stays a list, ``false`` stays ``False``. Stringifying at extraction time is
how a numeric field ends up compared as text three layers downstream.
"""

from __future__ import annotations

import re
from typing import Any

#: ``players[*]`` — a wildcard over one array.
_WILDCARD = re.compile(r"^(?P<name>[^\[\]]*)\[\*\]$")

#: Ceiling on how many elements a wildcard may expand to. A page of ten thousand
#: rows should not silently become ten thousand extracted values because a
#: profile said ``[*]``.
DEFAULT_MAX_WILDCARD = 1000


class JsonPathError(ValueError):
    """The path is not expressible in this subset."""


def walk(value: Any, path: str, *, max_wildcard: int = DEFAULT_MAX_WILDCARD) -> Any:
    """Resolve ``path`` against ``value``. Missing anywhere means ``None``.

    ``None`` is returned for a missing path rather than raising, because a field
    absent from one record among thousands is ordinary, and an exception per
    record would turn a data question into a control-flow one. A *malformed*
    path does raise: that is a profile bug, and it should surface at validation
    rather than as a column of nulls.
    """

    if not path:
        raise JsonPathError("empty path")

    current: Any = value
    for part in path.split("."):
        if current is None:
            return None
        wildcard = _WILDCARD.match(part)
        if wildcard:
            current = _expand(current, wildcard.group("name"), max_wildcard)
            continue
        current = _step(current, part)
    return current


def _step(current: Any, part: str) -> Any:
    """One ordinary segment: a dict key or a list index."""

    if isinstance(current, list):
        # A bare number indexes; anything else applied to a list is a mistake
        # the profile author should hear about, not a silent None.
        try:
            index = int(part)
        except ValueError:
            return None
        return current[index] if -len(current) <= index < len(current) else None
    if isinstance(current, dict):
        return current.get(part)
    return None


def _expand(current: Any, name: str, limit: int) -> list[Any]:
    """Enter an array and continue the remaining path across every element."""

    target = current
    if name:
        target = _step(current, name)
    if not isinstance(target, list):
        return []
    return list(target[:limit])


def walk_many(value: Any, path: str, *, max_wildcard: int = DEFAULT_MAX_WILDCARD) -> Any:
    """Resolve a path that may contain a wildcard, keeping the list shape.

    ``data.players[*].name`` returns ``["A", "B", "C"]`` rather than the first
    match: a wildcard asking for every player's name that answered with one
    name would be quietly wrong in a way nobody notices until the counts are
    compared.
    """

    if "[*]" not in path:
        return walk(value, path, max_wildcard=max_wildcard)

    head, _, tail = path.partition("[*]")
    head = head.rstrip(".")
    tail = tail.lstrip(".")

    array = walk(value, head, max_wildcard=max_wildcard) if head else value
    if not isinstance(array, list):
        return []
    bounded = array[:max_wildcard]
    if not tail:
        return bounded
    return [walk_many(item, tail, max_wildcard=max_wildcard) for item in bounded]


def validate_path(path: str) -> None:
    """Reject a path this subset cannot express, at profile-validation time."""

    if not path or not path.strip():
        raise JsonPathError("empty path")
    for part in path.split("."):
        if part.count("[*]") > 1:
            raise JsonPathError(f"more than one wildcard in one segment: {part!r}")
        if "[" in part and not _WILDCARD.match(part):
            raise JsonPathError(
                f"unsupported bracket syntax in {part!r}; this subset supports only [*]"
            )
    if path.count("[*]") > 2:
        # Two nested wildcards already produce a list of lists. A third produces
        # a shape nobody reading the profile will predict.
        raise JsonPathError("more than two wildcards in one path")
