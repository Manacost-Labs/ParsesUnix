"""Breaking a page on purpose, to find out whether the profile would notice.

A passing test suite proves the profile works on pages that have not changed.
It says nothing about the failure that actually happens, which is that the site
changes and the extractor keeps returning something — usually ``None``, quietly,
for months.

So each mutation takes a fixture the profile passes, damages it in one specific
way a real redesign would, and asserts the profile reacts *as its own field
importance says it should*:

.. code-block:: text

    optional field removed        -> a warning, and the run continues
    critical field removed        -> failure. The record is not a record
    a CSS class renamed           -> pass IF another source still supplies it
    a DOM node wrapped or moved   -> pass IF the path was not positional
    a JSON key renamed            -> schema drift, revalidation required
    a list returns []             -> empty result, not an error
    pagination cursor removed     -> incomplete, never "finished"

The point is not the mutation. It is that the *expected reaction* is written
down per field, so "the extractor broke and nothing failed" becomes a test
result instead of a discovery three months later.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MutationKind(StrEnum):
    """One way a page can change under a working profile."""

    REMOVE_OPTIONAL_FIELD = "remove_optional_field"
    REMOVE_CRITICAL_FIELD = "remove_critical_field"
    RENAME_CSS_CLASS = "rename_css_class"
    WRAP_DOM_NODE = "wrap_dom_node"
    MOVE_ELEMENT = "move_element"
    RENAME_JSON_KEY = "rename_json_key"
    EMPTY_COLLECTION = "empty_collection"
    CHANGE_FIELD_TYPE = "change_field_type"
    REMOVE_PAGINATION_CURSOR = "remove_pagination_cursor"


class Expectation(StrEnum):
    """What a correct profile does when it meets the mutation."""

    #: The value survives — another source supplied it, or the path did not
    #: depend on what changed. This is the outcome a second source buys.
    SURVIVES = "SURVIVES"
    #: The run continues, having said out loud that something is missing.
    WARNS = "WARNS"
    #: The record cannot be produced. Failing here is correct behaviour.
    FAILS = "FAILS"
    #: The shape changed. The route needs re-validating before it is trusted.
    DRIFT = "DRIFT"
    #: There is nothing to return, and that is a fact rather than an error.
    EMPTY = "EMPTY"
    #: The crawl cannot claim it saw everything.
    INCOMPLETE = "INCOMPLETE"


#: What each mutation must produce. Read as: "if this happens to the page, a
#: profile that is telling the truth about its own fields does THIS".
EXPECTED: Mapping[MutationKind, Expectation] = {
    MutationKind.REMOVE_OPTIONAL_FIELD: Expectation.WARNS,
    MutationKind.REMOVE_CRITICAL_FIELD: Expectation.FAILS,
    MutationKind.RENAME_CSS_CLASS: Expectation.SURVIVES,
    MutationKind.WRAP_DOM_NODE: Expectation.SURVIVES,
    MutationKind.MOVE_ELEMENT: Expectation.SURVIVES,
    MutationKind.RENAME_JSON_KEY: Expectation.DRIFT,
    MutationKind.EMPTY_COLLECTION: Expectation.EMPTY,
    MutationKind.CHANGE_FIELD_TYPE: Expectation.DRIFT,
    MutationKind.REMOVE_PAGINATION_CURSOR: Expectation.INCOMPLETE,
}


#: How loud each reaction is. A mutation passes when the profile reacted at
#: least as loudly as required — a renamed critical key that FAILS the record
#: rather than merely flagging DRIFT has done better than asked, not worse.
LOUDNESS: Mapping[Expectation, int] = {
    Expectation.SURVIVES: 0,
    Expectation.WARNS: 1,
    Expectation.EMPTY: 2,
    Expectation.INCOMPLETE: 2,
    Expectation.DRIFT: 2,
    Expectation.FAILS: 3,
}

#: Mutations where the point is that the value SURVIVES. Reacting loudly here is
#: a failure: the whole reason for a second source is that a class rename should
#: change nothing.
SURVIVAL_MUTATIONS = frozenset(
    {
        MutationKind.RENAME_CSS_CLASS,
        MutationKind.WRAP_DOM_NODE,
        MutationKind.MOVE_ELEMENT,
    }
)

#: Mutations whose failure is a finding rather than a blocker. A profile cannot
#: notice a number quietly becoming a string unless it declares field types, and
#: not every dataset needs that. Reporting it is useful; refusing to certify
#: over it would make the check something people delete.
ADVISORY_MUTATIONS = frozenset(
    {
        MutationKind.CHANGE_FIELD_TYPE,
        MutationKind.RENAME_JSON_KEY,
        MutationKind.EMPTY_COLLECTION,
        MutationKind.REMOVE_OPTIONAL_FIELD,
    }
)


@dataclass(frozen=True)
class Mutation:
    """One damage to apply, and what it is meant to prove."""

    kind: MutationKind
    target: str = ""
    note: str = ""

    @property
    def expectation(self) -> Expectation:
        return EXPECTED[self.kind]

    @property
    def name(self) -> str:
        return f"{self.kind.value}:{self.target}" if self.target else self.kind.value

    @property
    def is_advisory(self) -> bool:
        return self.kind in ADVISORY_MUTATIONS


def apply_to_html(body: bytes, mutation: Mutation) -> bytes:
    """Damage an HTML fixture. Text-level on purpose — no parser dependency.

    Crude, and that is appropriate: the mutations imitate what a redesign does
    to markup, and a redesign does not politely rebuild the tree either.
    """

    text = body.decode("utf-8", errors="ignore")
    target = mutation.target

    if mutation.kind is MutationKind.RENAME_CSS_CLASS and target:
        return re.sub(rf"\b{re.escape(target)}\b", f"{target}-v2", text).encode()

    if mutation.kind is MutationKind.WRAP_DOM_NODE and target:
        # A wrapper is the commonest invisible change: a layout tweak adds one
        # <div> and every direct-child selector underneath it stops matching.
        pattern = re.compile(rf"(<[a-zA-Z]+[^>]*\b{re.escape(target)}\b[^>]*>)", re.I)
        return pattern.sub(r'<div class="layout-wrapper">\1', text, count=1).encode()

    if mutation.kind is MutationKind.MOVE_ELEMENT and target:
        # Reordering siblings: fatal to :nth-child, invisible to anything named.
        matches = re.findall(r"<li[^>]*>.*?</li>", text, re.S)
        if len(matches) >= 2:
            swapped = text.replace(matches[0], "__TMP__", 1)
            swapped = swapped.replace(matches[1], matches[0], 1)
            return swapped.replace("__TMP__", matches[1], 1).encode()
        return text.encode()

    if (
        mutation.kind
        in {
            MutationKind.REMOVE_CRITICAL_FIELD,
            MutationKind.REMOVE_OPTIONAL_FIELD,
        }
        and target
    ):
        # Every source, not one. Deleting the DOM node while JSON-LD still
        # carries the value proves the fixture had two sources, which was never
        # in question — and it lets a profile that would lose the field pass.
        text = _strip_from_json_ld(text, target)
        pattern = re.compile(
            rf"<[a-zA-Z]+[^>]*\b{re.escape(target)}\b[^>]*>.*?</[a-zA-Z]+>", re.S | re.I
        )
        return pattern.sub("", text, count=1).encode()

    return text.encode()


#: What JSON-LD calls the fields a profile knows by simpler names. Removing
#: "title" has to remove "headline" too, or the mutation removes nothing.
_JSON_LD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("headline", "name", "title"),
    "author": ("author",),
    "description": ("description", "abstract"),
    "price": ("price",),
}


def _strip_from_json_ld(text: str, field_name: str) -> str:
    """Remove a field from every JSON-LD block on the page."""

    names = set(_JSON_LD_ALIASES.get(field_name, ())) | {field_name}

    def scrub(match: re.Match[str]) -> str:
        try:
            payload = json.loads(match.group(2))
        except (json.JSONDecodeError, ValueError):
            return match.group(0)
        return match.group(1) + json.dumps(_drop_keys(payload, tuple(names))) + match.group(3)

    return re.sub(
        r"(<script[^>]*application/ld\+json[^>]*>)(.*?)(</script>)",
        scrub,
        text,
        flags=re.S | re.I,
    )


def apply_to_json(body: bytes, mutation: Mutation) -> bytes:
    """Damage a JSON fixture, in the ways an API version bump actually does."""

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    target = mutation.target

    if mutation.kind is MutationKind.EMPTY_COLLECTION:
        emptied = _empty_collections(payload, target)
        return json.dumps(emptied).encode()

    if mutation.kind is MutationKind.RENAME_JSON_KEY and target:
        return json.dumps(_rename_key(payload, target, f"{target}_v2")).encode()

    if mutation.kind is MutationKind.CHANGE_FIELD_TYPE and target:
        return json.dumps(_retype(payload, target)).encode()

    if mutation.kind is MutationKind.REMOVE_PAGINATION_CURSOR:
        return json.dumps(_drop_keys(payload, _CURSOR_KEYS if not target else (target,))).encode()

    if (
        mutation.kind
        in {
            MutationKind.REMOVE_CRITICAL_FIELD,
            MutationKind.REMOVE_OPTIONAL_FIELD,
        }
        and target
    ):
        return json.dumps(_drop_keys(payload, (target,))).encode()

    return body


#: The names a cursor hides behind. Removing whichever is present is how a
#: crawl finds out it was relying on "there is always a next page" being stated.
_CURSOR_KEYS = ("next", "next_cursor", "cursor", "next_page", "after", "continuation")


def _empty_collections(value: Any, key: str = "") -> Any:
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        if key and key in value and isinstance(value[key], list):
            return {**value, key: []}
        return {k: ([] if isinstance(v, list) else v) for k, v in value.items()}
    return value


def _rename_key(value: Any, old: str, new: str) -> Any:
    if isinstance(value, list):
        return [_rename_key(item, old, new) for item in value]
    if isinstance(value, dict):
        return {(new if k == old else k): _rename_key(v, old, new) for k, v in value.items()}
    return value


def _retype(value: Any, key: str) -> Any:
    if isinstance(value, list):
        return [_retype(item, key) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == key:
                # A number becoming a string is the classic silent break: it
                # keeps flowing through, compares wrong, and sorts wrong.
                out[k] = str(v) if isinstance(v, int | float) else {"value": v}
            else:
                out[k] = _retype(v, key)
        return out
    return value


def _drop_keys(value: Any, keys: Sequence[str]) -> Any:
    if isinstance(value, list):
        return [_drop_keys(item, keys) for item in value]
    if isinstance(value, dict):
        return {k: _drop_keys(v, keys) for k, v in value.items() if k not in keys}
    return value


def mutate(body: bytes, mutation: Mutation, *, is_json: bool) -> bytes:
    return apply_to_json(body, mutation) if is_json else apply_to_html(body, mutation)


@dataclass(frozen=True)
class MutationRun:
    """One mutation applied, and what the profile did about it."""

    mutation: Mutation
    observed: Expectation
    detail: str = ""

    @property
    def passed(self) -> bool:
        expected = self.mutation.expectation
        if self.observed is expected:
            return True
        if self.mutation.kind in SURVIVAL_MUTATIONS:
            # Only survival counts. A profile that fails when a CSS class is
            # renamed has no second source, which is the thing being tested.
            return False
        if expected is Expectation.WARNS and self.observed is Expectation.SURVIVES:
            # An optional field that came from somewhere else is better than
            # required, not worse. Demanding a warning here would push profiles
            # towards having only one source, which is the opposite of the goal.
            return True
        return LOUDNESS[self.observed] >= LOUDNESS[expected]

    @property
    def is_advisory(self) -> bool:
        return self.mutation.is_advisory

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation": self.mutation.name,
            "expected": self.mutation.expectation.value,
            "observed": self.observed.value,
            "passed": self.passed,
            "advisory": self.is_advisory,
            "detail": self.detail,
        }


def run_mutations(
    body: bytes,
    mutations: Sequence[Mutation],
    *,
    is_json: bool,
    evaluate: Callable[[bytes, Mutation], Expectation],
) -> list[MutationRun]:
    """Apply each mutation to the same starting fixture and record the reaction.

    ``evaluate`` is supplied by the caller — the profile's own extraction and
    triage — so this module never learns how a profile works, only how to break
    a page and how to read the answer. It receives the mutation as well as the
    damaged bytes, because the same verdict means different things depending on
    what was broken: a failed JSON path after a key rename is schema drift, and
    after a field removal it is a missing field.
    """

    out: list[MutationRun] = []
    for mutation in mutations:
        damaged = mutate(body, mutation, is_json=is_json)
        if damaged == body:
            out.append(
                MutationRun(
                    mutation,
                    Expectation.SURVIVES,
                    detail="the mutation changed nothing in this fixture",
                )
            )
            continue
        out.append(MutationRun(mutation, evaluate(damaged, mutation)))
    return out


def default_mutations(
    *,
    critical_fields: Sequence[str] = (),
    optional_fields: Sequence[str] = (),
    css_classes: Sequence[str] = (),
    has_pagination: bool = False,
    is_json: bool = False,
) -> list[Mutation]:
    """The mutations worth running for one URL class, and no others.

    Generated from what the profile actually declares. Running a JSON key rename
    against an HTML page proves nothing, and a checklist that demands it teaches
    people to write mutations that always pass.
    """

    out: list[Mutation] = []
    for name in critical_fields:
        out.append(Mutation(MutationKind.REMOVE_CRITICAL_FIELD, name))
    for name in optional_fields:
        out.append(Mutation(MutationKind.REMOVE_OPTIONAL_FIELD, name))
    if is_json:
        for name in critical_fields:
            out.append(Mutation(MutationKind.RENAME_JSON_KEY, name))
            out.append(Mutation(MutationKind.CHANGE_FIELD_TYPE, name))
        out.append(Mutation(MutationKind.EMPTY_COLLECTION))
    else:
        for css in css_classes:
            out.append(Mutation(MutationKind.RENAME_CSS_CLASS, css))
            out.append(Mutation(MutationKind.WRAP_DOM_NODE, css))
        out.append(Mutation(MutationKind.MOVE_ELEMENT, "li"))
    if has_pagination:
        out.append(Mutation(MutationKind.REMOVE_PAGINATION_CURSOR))
    return out
