"""Tree-sitter based parsing for non-Python source files.

The parser returns the same normalized shape used by the Python AST parser.
Language packages are optional at import time so Python-only installations
continue to work and unsupported files remain available as file context.
"""

from __future__ import annotations

import importlib
import json
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Iterator


SUPPORTED_EXTENSIONS = {
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "c_sharp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".ts": "typescript",
    ".tsx": "typescript",
}

LANGUAGE_MODULES = {
    "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
}

FUNCTION_NODE_TYPES = {
    "function_definition", "function_declaration", "function_item",
    "method_definition", "method_declaration", "constructor_declaration",
    "function_signature",
}
CLASS_NODE_TYPES = {
    "class_specifier", "struct_specifier", "class_declaration",
    "interface_declaration", "enum_declaration", "struct_declaration",
    "object_declaration", "record_declaration",
}
IMPORT_NODE_TYPES = {
    "preproc_include", "using_declaration", "using_directive",
    "import_declaration", "import_statement", "import_header",
}
CALL_NODE_TYPES = {
    "call_expression", "method_invocation", "invocation_expression",
    "call", "function_call_expression",
}
METHOD_NODE_TYPES = {"method_definition", "method_declaration", "constructor_declaration"}
IDENTIFIER_NODE_TYPES = {
    "identifier", "field_identifier", "property_identifier",
    "type_identifier", "namespace_identifier", "simple_identifier",
}


class ParserUnavailable(RuntimeError):
    """Raised when a supported language grammar is not installed."""


def language_for_path(path: Path) -> str | None:
    """Return the normalized language name for a source path."""
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def is_supported_source(path: Path | str) -> bool:
    """Return whether the path is a source file DocDNA can index."""
    return language_for_path(Path(path)) is not None or Path(path).suffix.lower() == ".py"


def _load_parser(language_name: str):
    """Loads and configures a Tree-sitter parser for a specified language."""
    try:
        from tree_sitter import Language, Parser
    except ImportError as exc:
        raise ParserUnavailable(
            "Tree-sitter is not installed; install the DocDNA parser dependencies"
        ) from exc

    module_name, factory_name = LANGUAGE_MODULES[language_name]
    try:
        grammar = importlib.import_module(module_name)
    except ImportError as exc:
        raise ParserUnavailable(
            f"Tree-sitter grammar is not installed for {language_name}"
        ) from exc

    factory = getattr(grammar, factory_name, None)
    if factory is None:
        raise ParserUnavailable(f"Tree-sitter grammar has no {factory_name}() factory for {language_name}")

    language = Language(factory())
    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        parser.set_language(language)
        return parser


def file_metadata(path: Path, rel_path: str, content: str | None = None) -> dict:
    """Return metadata even when a language grammar is unavailable."""
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")
    language = language_for_path(path) or "unknown"
    return {
        "path": rel_path,
        "line_count": len(content.splitlines()),
        "docstring": None,
        "module_name": path.stem,
        "purpose": f"{language} source file: {path.stem}",
        "language": language,
        "content_hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
    }


def _walk(node, ancestors: tuple = ()) -> Iterator[tuple]:
    """Recursively yields nodes and their ancestors during traversal."""
    yield node, ancestors
    next_ancestors = ancestors + (node,)
    for child in node.children:
        yield from _walk(child, next_ancestors)


def _text(node, source: bytes) -> str:
    """Extracts text from a node using its byte range in source code."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_descendant(node, types: set[str]):
    """Searches for the first descendant of a node matching specified types."""
    for child, _ in _walk(node):
        if child.type in types:
            return child
    return None


def _name_node(node):
    """Identifies the named child of a node, searching specific fields first."""
    named = node.child_by_field_name("name")
    if named is not None:
        return named
    return _first_descendant(node, IDENTIFIER_NODE_TYPES)


def _node_name(node, source: bytes) -> str:
    """Extracts the name of a node, or returns "<anonymous>" if none found."""
    named = _name_node(node)
    return _text(named, source).strip() if named is not None else "<anonymous>"


def _parameter_node(node):
    """Finds the appropriate parameter node within a given node."""
    for field in ("parameters", "parameter", "declarator"):
        candidate = node.child_by_field_name(field)
        if candidate is not None and (
            "parameter" in candidate.type or field == "parameters"
        ):
            return candidate
    return _first_descendant(node, {"parameter_list", "formal_parameters", "parameters"})


def _split_parameters(text: str) -> list[str]:
    """Splits parameter text into individual parameters."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    if not text.strip():
        return []
    result, current, depth = [], [], 0
    for char in text:
        if char in "([{<":
            depth += 1
        elif char in ")]}>" and depth:
            depth -= 1
        if char == "," and depth == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        result.append("".join(current).strip())
    return [item for item in result if item]


def _comment_before(node, lines: list[str]) -> str | None:
    """Retrieves comments preceding a node in a list of lines."""
    line = node.start_point[0] - 1
    comments = []
    while line >= 0:
        value = lines[line].strip()
        if not value:
            if comments:
                break
            line -= 1
            continue
        if value.startswith(("//", "///", "/*", "*", "#")):
            comments.append(value.lstrip("/#* ").rstrip("*/ "))
            line -= 1
            continue
        break
    return " ".join(reversed(comments)).strip() or None


def _purpose(name: str, documentation: str | None) -> str:
    """Determines purpose of a symbol from its name and documentation."""
    if documentation:
        return documentation.splitlines()[0].strip()
    return name.replace("_", " ").strip() or "Unnamed symbol"


def _calls_for(node, source: bytes) -> list[str]:
    """Recursively finds calls made from a given node in source code."""
    calls = []
    for child, ancestors in _walk(node):
        if child is node or child.type not in CALL_NODE_TYPES:
            continue
        if any(ancestor is not node and ancestor.type in FUNCTION_NODE_TYPES
               for ancestor in ancestors):
            continue
        target = child.child_by_field_name("function") or child.child_by_field_name("name")
        if target is None:
            target = _first_descendant(child, IDENTIFIER_NODE_TYPES)
        if target is not None:
            value = _text(target, source).strip()
            if value and value not in calls:
                calls.append(value)
    return calls


def parse_multilang_file(path: Path, rel_path: str) -> dict:
    """Parse a non-Python file into DocDNA's normalized project model."""
    language = language_for_path(path)
    if not language:
        raise ValueError(f"Unsupported source file: {path}")

    content = path.read_bytes()
    source = content.decode("utf-8", errors="replace")
    lines = source.splitlines()
    parser = _load_parser(language)
    tree = parser.parse(content)

    functions = {}
    classes = {}
    imports = []
    call_graph = {}
    function_nodes = []

    for node, ancestors in _walk(tree.root_node):
        if node.type in CLASS_NODE_TYPES:
            name = _node_name(node, content)
            line = node.start_point[0] + 1
            classes[f"{rel_path}:{line}:{name}"] = {
                "name": name,
                "file": rel_path,
                "line": line,
                "bases": [],
                "docstring": _comment_before(node, lines),
                "methods": [],
                "language": language,
            }
        elif node.type in FUNCTION_NODE_TYPES:
            function_nodes.append((node, ancestors))
        elif node.type in IMPORT_NODE_TYPES:
            imports.append({
                "module": _text(node, content).strip(),
                "alias": None,
                "from_module": None,
                "line": node.start_point[0] + 1,
            })

    for node, ancestors in function_nodes:
        name = _node_name(node, content)
        line = node.start_point[0] + 1
        symbol_id = f"{rel_path}:{line}:{name}"
        documentation = _comment_before(node, lines)
        parameters = _parameter_node(node)
        args = _split_parameters(_text(parameters, content)) if parameters else []
        is_method = int(node.type in METHOD_NODE_TYPES or any(
            ancestor.type in CLASS_NODE_TYPES for ancestor in ancestors
        ))
        functions[symbol_id] = {
            "name": name,
            "file": rel_path,
            "line": line,
            "args_json": json.dumps(args),
            "docstring": documentation,
            "is_method": is_method,
            "return_type": None,
            "inferred_purpose": _purpose(name, documentation),
            "decorators": "[]",
            "language": language,
        }
        calls = _calls_for(node, content)
        if calls:
            call_graph[symbol_id] = {
                "name": name,
                "line": line,
                "calls": calls,
                "file": rel_path,
                "symbol_id": symbol_id,
                "language": language,
            }

    metadata = file_metadata(path, rel_path, source)
    metadata["language"] = language
    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "call_graph": call_graph,
        "code_lines": lines,
        "file_meta": metadata,
    }
