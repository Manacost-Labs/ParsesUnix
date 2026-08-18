"""Strict subset-YAML loader used when PyYAML is unavailable.

Supports what site profiles actually need: nested mappings, block lists,
inline lists of scalars, quoted and plain scalars, and comments.
Anchors, aliases, tags, block scalars, flow mappings, and multi-document
streams are rejected with a clear error so a profile that relies on them
fails loudly instead of being silently misread.
"""

from __future__ import annotations

import json
import re
from typing import Any

_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+):(?:\s+(.*))?$")
_UNSUPPORTED_LEAD = ("&", "*", "|", ">", "!", "%")


class YamlishError(ValueError):
    def __init__(self, message: str, line_no: int) -> None:
        super().__init__(f"line {line_no}: {message}")
        self.line_no = line_no


#: Characters after which a quote begins a quoted scalar (value/item/flow start).
_VALUE_START_CHARS = frozenset({":", ",", "[", "-", "{"})


def _strip_comment(line: str, line_no: int) -> str:
    quote: str | None = None
    prev_significant: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            # A quote only opens a quoted scalar at a value/item boundary; a
            # quote in the middle of a plain scalar (e.g. "it's fine") is literal.
            if prev_significant is None or prev_significant in _VALUE_START_CHARS:
                quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
        if char not in " \t":
            prev_significant = char
    if quote:
        raise YamlishError("unterminated quoted string", line_no)
    return line


def _split_inline_list(inner: str, line_no: int) -> list[str]:
    """Split on commas at the top level only.

    Depth matters: in ``{kind: css, fields: {title: a, date: b}}`` the comma
    inside ``fields`` belongs to the nested mapping, and splitting on it would
    tear the entry in half.
    """

    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char in "{[":
            depth += 1
            current.append(char)
        elif char in "}]":
            depth -= 1
            if depth < 0:
                raise YamlishError("unbalanced closing bracket", line_no)
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote:
        raise YamlishError("unterminated quoted string in inline list", line_no)
    if depth:
        raise YamlishError("unbalanced bracket in inline collection", line_no)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item]


def _parse_scalar(scalar: str, line_no: int) -> Any:
    if scalar.startswith('"'):
        try:
            return json.loads(scalar)
        except json.JSONDecodeError as exc:
            raise YamlishError(f"invalid double-quoted string: {exc}", line_no) from exc
    if scalar.startswith("'"):
        if len(scalar) < 2 or not scalar.endswith("'"):
            raise YamlishError("unterminated single-quoted string", line_no)
        return scalar[1:-1].replace("''", "'")
    if scalar.startswith("["):
        if not scalar.endswith("]"):
            raise YamlishError("inline list must close on the same line", line_no)
        return [_parse_scalar(item, line_no) for item in _split_inline_list(scalar[1:-1], line_no)]
    if scalar.startswith("{"):
        # Single-line flow mappings of scalars, e.g. `{type: rss, level: L0}`.
        # Extremely common in route and field lists, and unambiguous enough to
        # parse exactly; anything nested still has to be written in block style.
        if not scalar.endswith("}"):
            raise YamlishError("flow mapping must close on the same line", line_no)
        inner = scalar[1:-1].strip()
        if not inner:
            return {}
        mapping: dict[str, Any] = {}
        for item in _split_inline_list(inner, line_no):
            key, separator, value = item.partition(":")
            if not separator:
                raise YamlishError(f"flow mapping entry {item!r} is missing ':'", line_no)
            name = key.strip().strip("\"'")
            if not name:
                raise YamlishError("flow mapping key must not be empty", line_no)
            if name in mapping:
                raise YamlishError(f"duplicate key {name!r} in flow mapping", line_no)
            mapping[name] = _parse_scalar(value.strip(), line_no)
        return mapping
    if scalar.startswith(_UNSUPPORTED_LEAD):
        raise YamlishError(
            f"unsupported YAML feature at {scalar[:10]!r} (anchors, tags, and block scalars "
            "are not part of the profile subset)",
            line_no,
        )
    if scalar in {"null", "~"}:
        return None
    if scalar == "true":
        return True
    if scalar == "false":
        return False
    if re.fullmatch(r"-?\d+", scalar):
        return int(scalar)
    if re.fullmatch(r"-?\d+\.\d*(?:[eE][+-]?\d+)?", scalar):
        return float(scalar)
    return scalar


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines: list[tuple[int, str, int]] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            without_comment = _strip_comment(raw, line_no)
            if not without_comment.strip():
                continue
            stripped = without_comment.lstrip(" ")
            indent = len(without_comment) - len(stripped)
            if "\t" in without_comment[:indent] or stripped.startswith("\t"):
                raise YamlishError("tabs are not allowed in indentation", line_no)
            if stripped.rstrip() == "---" and not self.lines:
                continue
            if stripped.rstrip() in {"---", "..."}:
                raise YamlishError("multi-document streams are not supported", line_no)
            self.lines.append((indent, stripped.rstrip(), line_no))

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value, index = self._parse_block(0, self.lines[0][0])
        if index != len(self.lines):
            _, _, line_no = self.lines[index]
            raise YamlishError("unexpected dedent or stray content", line_no)
        return value

    def _parse_block(self, index: int, indent: int) -> tuple[Any, int]:
        _, content, _ = self.lines[index]
        if content == "-" or content.startswith("- "):
            return self._parse_list(index, indent)
        return self._parse_mapping(index, indent)

    def _parse_mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(self.lines):
            item_indent, content, line_no = self.lines[index]
            if item_indent < indent:
                break
            if item_indent > indent:
                raise YamlishError("unexpected indent", line_no)
            if content == "-" or content.startswith("- "):
                raise YamlishError("list item where a mapping key was expected", line_no)
            match = _KEY_RE.match(content)
            if not match:
                raise YamlishError(f"cannot parse mapping entry {content!r}", line_no)
            key, rest = match.group(1), match.group(2)
            if key in mapping:
                raise YamlishError(f"duplicate key {key!r}", line_no)
            index += 1
            if rest is None or not rest.strip():
                if index < len(self.lines) and self.lines[index][0] > indent:
                    value, index = self._parse_block(index, self.lines[index][0])
                else:
                    value = None
            else:
                value = _parse_scalar(rest.strip(), line_no)
            mapping[key] = value
        return mapping, index

    def _parse_list(self, index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(self.lines):
            item_indent, content, line_no = self.lines[index]
            if item_indent < indent:
                break
            if item_indent > indent:
                raise YamlishError("unexpected indent inside a list", line_no)
            if content != "-" and not content.startswith("- "):
                break
            rest = content[1:].lstrip()
            if not rest:
                index += 1
                if index < len(self.lines) and self.lines[index][0] > indent:
                    value, index = self._parse_block(index, self.lines[index][0])
                else:
                    value = None
                items.append(value)
                continue
            if _KEY_RE.match(rest):
                # A mapping opened inline in the list item: re-anchor the line at
                # the column just past "- " and parse it as a normal mapping block.
                self.lines[index] = (indent + 2, rest, line_no)
                value, index = self._parse_mapping(index, indent + 2)
                items.append(value)
                continue
            items.append(_parse_scalar(rest, line_no))
            index += 1
        return items, index


def loads(text: str) -> Any:
    return _Parser(text).parse()
