"""A tiny stdlib HTML DOM and a minimal CSS-selector subset.

No third-party parser is available, so this builds a lightweight tree with
``html.parser`` and supports exactly the selector shapes site profiles use:

    tag            .class        #id        tag.class      tag#id
    a b c          (descendant)  tag::text  tag::attr(name)

That is enough for ``h1::text`` and ``time::attr(datetime)`` without pulling in
lxml/bs4. Anything fancier should be expressed as a JSON-LD / app-state route.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "template"})


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None
    text_parts: list[str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: Node) -> None:
            parts.extend(node.text_parts)
            for child in node.children:
                if child.tag not in _SKIP_TEXT_TAGS:
                    walk(child)

        walk(self)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def descendants(self) -> Iterator[Node]:
        for child in self.children:
            yield child
            yield from child.descendants()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#root")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
        node = Node(tag=tag, attrs={k: (v or "") for k, v in attrs}, parent=self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):  # type: ignore[no-untyped-def]
        node = Node(tag=tag, attrs={k: (v or "") for k, v in attrs}, parent=self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):  # type: ignore[no-untyped-def]
        self._stack[-1].text_parts.append(data)


def parse_html(body: bytes | str) -> Node:
    text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body
    builder = _TreeBuilder()
    builder.feed(text)
    return builder.root


@dataclass(frozen=True)
class _SimpleSelector:
    tag: str | None
    id: str | None
    classes: tuple[str, ...]

    def matches(self, node: Node) -> bool:
        if node.tag == "#root":
            return False
        if self.tag and node.tag != self.tag:
            return False
        if self.id and node.attrs.get("id") != self.id:
            return False
        return all(cls in node.classes for cls in self.classes)


_SIMPLE_RE = re.compile(r"([a-zA-Z0-9\-]+)?(#[a-zA-Z0-9\-_]+)?((?:\.[a-zA-Z0-9\-_]+)*)")


def _parse_simple(token: str) -> _SimpleSelector:
    match = _SIMPLE_RE.fullmatch(token)
    if not match:
        return _SimpleSelector(tag=token or None, id=None, classes=())
    tag, id_part, class_part = match.groups()
    classes = tuple(c for c in class_part.split(".") if c) if class_part else ()
    return _SimpleSelector(tag=tag or None, id=id_part[1:] if id_part else None, classes=classes)


def _split_pseudo(selector: str) -> tuple[str, str, str | None]:
    """Return (css, mode, attr): mode is 'text' | 'attr' | 'node'."""

    attr_match = re.search(r"::attr\(([^)]+)\)\s*$", selector)
    if attr_match:
        return selector[: attr_match.start()].strip(), "attr", attr_match.group(1)
    if selector.rstrip().endswith("::text"):
        return selector.rsplit("::text", 1)[0].strip(), "text", None
    return selector.strip(), "node", None


def select(root: Node, css: str) -> list[Node]:
    """Descendant-combinator selection over the simple-selector subset."""

    parts = [p for p in css.split() if p]
    if not parts:
        return []
    current = [root]
    for part in parts:
        simple = _parse_simple(part)
        matched: list[Node] = []
        for node in current:
            matched.extend(d for d in node.descendants() if simple.matches(d))
        current = matched
        if not current:
            return []
    return current


def query_value(root: Node, selector: str) -> str | None:
    """Return the text or attribute value for a ``tag::text`` / ``tag::attr(x)`` selector."""

    css, mode, attr = _split_pseudo(selector)
    nodes = select(root, css)
    if not nodes:
        return None
    node = nodes[0]
    if mode == "attr" and attr is not None:
        return node.attrs.get(attr)
    if mode == "text":
        return node.text() or None
    return node.text() or None
