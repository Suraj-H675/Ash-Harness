from __future__ import annotations

import ast
import inspect
from pathlib import Path

import ash.cli as cli_module


def test_every_cli_subparser_declares_discovery_help() -> None:
    source_path = Path(inspect.getsourcefile(cli_module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    missing: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_parser":
            continue
        if any(keyword.arg == "help" for keyword in node.keywords):
            continue
        name = ast.unparse(node.args[0]) if node.args else "<unknown>"
        missing.append(f"line {node.lineno}: {name}")

    assert missing == []


def test_every_top_level_cli_command_has_a_dispatch_handler() -> None:
    source_path = Path(inspect.getsourcefile(cli_module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    registered: set[str] = set()
    handled: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subparsers"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            registered.add(node.args[0].value)
            continue
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (
            isinstance(left, ast.Attribute)
            and isinstance(left.value, ast.Name)
            and left.value.id == "args"
            and left.attr == "command"
        ):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                handled.add(comparator.value)
            elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                handled.update(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                )

    assert registered - handled == set()
