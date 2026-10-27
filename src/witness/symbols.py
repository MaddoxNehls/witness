"""Find the functions and classes that enclose changed lines, using tree-sitter."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from typing import Any

from tree_sitter_language_pack import get_parser

from witness.git import LineRange

# tree-sitter grammar name per file suffix.
GRAMMARS = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".mts": "typescript"}

# Node types that count as a "definition" worth showing whole.
DEFINITIONS = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "lexical_declaration",  # const f = () => ...
    },
}
DEFINITIONS["tsx"] = DEFINITIONS["typescript"]


@dataclass(frozen=True)
class Symbol:
    path: str
    kind: str
    name: str
    start_line: int  # 1-based, inclusive
    end_line: int
    source: str

    def render(self) -> str:
        lines = self.source.splitlines()
        width = len(str(self.end_line))
        numbered = "\n".join(
            f"{self.start_line + i:>{width}} | {text}" for i, text in enumerate(lines)
        )
        header = f"{self.path}: {self.kind} {self.name} (lines {self.start_line}-{self.end_line})"
        return f"{header}\n{numbered}"


def grammar_for(path: str) -> str | None:
    return GRAMMARS.get(PurePosixPath(path).suffix)


@cache
def _parser(grammar: str) -> Any:
    return get_parser(grammar)


def enclosing_symbols(path: str, source: str, ranges: Iterable[LineRange]) -> list[Symbol]:
    """The innermost definition around each changed range, outermost duplicates removed.

    Changes at module level (imports, constants) have no enclosing definition and are
    already visible in the diff, so they contribute nothing here.
    """
    grammar = grammar_for(path)
    if grammar is None:
        return []
    data = source.encode("utf-8")
    root = _parser(grammar).parse(data).root_node
    kinds = DEFINITIONS[grammar]

    chosen: dict[tuple[int, int], Any] = {}
    for changed in ranges:
        for node in _definitions_for(root, changed, kinds):
            chosen[(node.start_byte, node.end_byte)] = node

    # A selected method inside a selected class is redundant: the class already shows it.
    spans = sorted(chosen)
    kept = [
        span
        for span in spans
        if not any(o != span and o[0] <= span[0] and span[1] <= o[1] for o in spans)
    ]
    return [_to_symbol(path, chosen[span], data) for span in kept]


def _definitions_for(root: Any, changed: LineRange, kinds: set[str]) -> list[Any]:
    """The innermost definition containing ``changed``, or, when the change straddles
    several definitions (e.g. it adds two functions), every definition it touches."""
    definitions = [node for node in _walk(root) if node.type in kinds]
    containing = [
        node
        for node in definitions
        if node.start_point[0] + 1 <= changed.start and changed.end <= node.end_point[0] + 1
    ]
    if containing:
        found = [containing[-1]]  # _walk is pre-order, so the last match is the deepest
    else:
        found = [
            node
            for node in definitions
            if changed.overlaps(node.start_point[0] + 1, node.end_point[0] + 1)
        ]
    return [_with_decorators(node) for node in found]


def _with_decorators(node: Any) -> Any:
    parent = node.parent
    return parent if parent is not None and parent.type == "decorated_definition" else node


def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _to_symbol(path: str, node: Any, data: bytes) -> Symbol:
    target = node
    if node.type == "decorated_definition":
        target = node.child_by_field_name("definition") or node
    name_node = target.child_by_field_name("name")
    if name_node is None and target.type == "lexical_declaration":
        declarator = next((c for c in target.children if c.type == "variable_declarator"), None)
        name_node = declarator.child_by_field_name("name") if declarator else None
    name = data[name_node.start_byte : name_node.end_byte].decode() if name_node else "<anonymous>"
    kind = "class" if "class" in target.type else "function"
    # Slice from the start of the line so the first line keeps its indentation.
    line_start = data.rfind(b"\n", 0, node.start_byte) + 1
    return Symbol(
        path=path,
        kind=kind,
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source=data[line_start : node.end_byte].decode("utf-8", errors="replace"),
    )
