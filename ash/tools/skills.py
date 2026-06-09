"""Skill compiler and dynamic loader (Sprint 14 / V7).

Two skill formats are supported, both loaded by
:class:`ash.tools.registry.ToolRegistry`:

* **Python skill files** — a ``.py`` file with a docstring header
  (``name:``, ``description:``, ``trigger:``) and an
  ``async def execute(context, **kwargs) -> str`` coroutine. This is
  the format sketched in ASH_MASTER_PLAN_V2.md V7.
* **Markdown recipe files** — a ``.md`` file with optional YAML
  frontmatter plus an ``## Args`` section and a fenced Python
  ``## Code`` block. The compiler generates a Pydantic
  :class:`~ash.tools.base.BaseTool` subclass on the fly whose
  ``run()`` body is the markdown's code block.

A :class:`SkillContext` is passed to the execute() function so the
skill can call other tools (``run_command``, ``read_file``) without
importing them directly. Self-extension is handled by
:func:`write_python_skill` which writes a new file and asks the
registry to reload it.
"""

from __future__ import annotations

import ast
import re
import textwrap
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, create_model

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.registry import SkillIndexEntry


# --- SkillContext ----------------------------------------------------------


class SkillContext:
    """
    Runtime context passed to a skill's ``execute()`` function.

    The context exposes a small, opinionated surface area so skills
    don't reach into the broader codebase. ``run_command`` and
    ``read_file`` delegate through the supplied tool registry; the
    other helpers return structured data.
    """

    def __init__(
        self,
        safety_guard: SafetyGuard,
        tools: Mapping[str, BaseTool],
        project_root: Path,
    ) -> None:
        self.safety_guard = safety_guard
        self._tools = tools
        self.project_root = project_root

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    async def run_command(
        self, command_line: str, *, cwd: str | None = None, timeout: int = 60
    ) -> str:
        tool = self._tools.get("run_command")
        if tool is None:
            from ash.tools.command import RunCommandTool

            tool = RunCommandTool(self.safety_guard)
        result: ToolResult = await tool.run(
            command_line=command_line, cwd=cwd, timeout_seconds=timeout
        )
        if not result.success:
            raise SkillExecutionError(result.error or "command failed", result=result)
        return result.output

    async def read_file(
        self, file_path: str, *, start_line: int = 1, end_line: int | None = None
    ) -> str:
        tool = self._tools.get("read_file")
        if tool is None:
            from ash.tools.filesystem import ReadFileTool

            tool = ReadFileTool(self.safety_guard)
        result: ToolResult = await tool.run(
            file_path=file_path, start_line=start_line, end_line=end_line
        )
        if not result.success:
            raise SkillExecutionError(result.error or "read failed", result=result)
        return result.output

    async def write_file(
        self, file_path: str, content: str, *, overwrite: bool = False
    ) -> None:
        tool = self._tools.get("write_file")
        if tool is None:
            from ash.tools.filesystem import WriteFileTool

            tool = WriteFileTool(self.safety_guard)
        result: ToolResult = await tool.run(
            file_path=file_path, content=content, overwrite=overwrite
        )
        if not result.success:
            raise SkillExecutionError(result.error or "write failed", result=result)


class SkillExecutionError(RuntimeError):
    """Raised when a skill cannot complete its task."""

    def __init__(self, message: str, *, result: ToolResult | None = None) -> None:
        super().__init__(message)
        self.result = result


# --- markdown recipe parser ----------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```python\s*\n(?P<body>.*?)```", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class _MarkdownSkill:
    name: str
    description: str
    trigger: str
    args: tuple[tuple[str, str, str, str], ...]  # (name, type, default, description)
    code: str


def parse_markdown_skill(path: Path) -> _MarkdownSkill:
    """Parse a markdown recipe file into a structured skill."""

    text = path.read_text(encoding="utf-8")
    front: dict[str, str] = {}
    fm_match = _FRONTMATTER_RE.match(text)
    body = text
    if fm_match is not None:
        for line in fm_match.group("body").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                front[key.strip().lower()] = value.strip()
        body = text[fm_match.end() :]

    name = front.get("name") or _first_h1(body) or path.stem
    description = front.get("description") or _section_after(
        body, "Description", fallback=""
    )
    trigger = front.get("trigger", "")
    args = _parse_args_section(body)
    code_match = _CODE_FENCE_RE.search(body)
    if code_match is None:
        raise SkillParseError(f"No Python code fence found in {path}")
    code = textwrap.dedent(code_match.group("body")).strip()
    return _MarkdownSkill(
        name=name,
        description=description,
        trigger=trigger,
        args=args,
        code=code,
    )


def parse_markdown_skill_index(path: Path) -> SkillIndexEntry | None:
    """Build a :class:`SkillIndexEntry` from a markdown file without compiling it."""

    try:
        skill = parse_markdown_skill(path)
    except SkillParseError:
        return None
    return SkillIndexEntry(
        name=skill.name,
        description=skill.description or "(no description)",
        source="markdown",
        path=str(path),
        trigger=skill.trigger,
    )


def _first_h1(body: str) -> str | None:
    match = _H1_RE.search(body)
    if match is None:
        return None
    return match.group(1).strip()


def _section_after(body: str, heading: str, *, fallback: str = "") -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    if match is None:
        return fallback
    return match.group("body").strip()


def _parse_args_section(body: str) -> tuple[tuple[str, str, str, str], ...]:
    block = _section_after(body, "Args")
    if not block:
        return ()
    args: list[tuple[str, str, str, str]] = []
    bullet_re = re.compile(
        r"^\s*-\s*`?(?P<name>[A-Za-z_][A-Za-z0-9_]*)`?\s*:\s*(?P<rest>.+?)\s*$",
        re.MULTILINE,
    )
    for line_match in bullet_re.finditer(block):
        name = line_match.group("name")
        rest = line_match.group("rest")
        # The rest looks like: <type> [= default] - description
        type_match = re.match(r"`?([A-Za-z_][A-Za-z0-9_\[\], ]*)`?", rest)
        py_type = (type_match.group(1) if type_match else "str").strip()
        default = ""
        default_match = re.search(
            r"=\s*(\[[^\]]*\]|'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?|True|False|None)", rest
        )
        if default_match:
            default = default_match.group(1)
        # Description is everything after the type (and optional default).
        desc = rest
        if default_match:
            desc = (desc[: default_match.start()] + desc[default_match.end() :]).strip()
        if type_match:
            desc = desc[type_match.end() :].lstrip(" -:").strip()
        args.append((name, py_type, default, desc))
    return tuple(args)


# --- Python skill parser --------------------------------------------------


@dataclass(frozen=True)
class _PythonSkill:
    name: str
    description: str
    trigger: str
    code: str
    source_path: Path | None = None


_PY_DOCSTRING_META_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_]+)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE
)


def parse_python_skill(path: Path) -> _PythonSkill:
    """Parse a Python skill file with the V7 docstring convention."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_doc = ast.get_docstring(tree) or ""

    name = path.stem
    description = ""
    trigger = ""
    for line_match in _PY_DOCSTRING_META_RE.finditer(module_doc):
        key = line_match.group("key").lower()
        value = line_match.group("value")
        if key == "name":
            name = value
        elif key == "description":
            description = value
        elif key == "trigger":
            trigger = value

    # Find an `async def execute(...)` (or sync `def execute`) function.
    execute_node = None
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "execute"
        ):
            execute_node = node
            break
    if execute_node is None:
        raise SkillParseError(f"No execute() function found in {path}")

    # Re-emit the function so it can be re-defined in our dynamically
    # generated module; the rest of the file is also copied verbatim.
    return _PythonSkill(
        name=name,
        description=description or "Python skill",
        trigger=trigger,
        code=source,
        source_path=path,
    )


def parse_python_skill_index(path: Path) -> SkillIndexEntry | None:
    try:
        skill = parse_python_skill(path)
    except SkillParseError:
        return None
    return SkillIndexEntry(
        name=skill.name,
        description=skill.description,
        source="python",
        path=str(path),
        trigger=skill.trigger,
    )


def build_tool_from_python_module(
    module: types.ModuleType,
    safety_guard: SafetyGuard,
    *,
    source_path: Path | None = None,
    parsed_name: str | None = None,
    parsed_description: str | None = None,
    parsed_trigger: str | None = None,
) -> BaseTool:
    """Wrap a Python skill module's ``execute`` function in a :class:`BaseTool`.

    ``parsed_name`` / ``parsed_description`` / ``parsed_trigger`` are
    forwarded from :func:`parse_python_skill` so the docstring-driven
    V7 metadata is honoured even when the module does not set the
    matching ``__ash_name__`` attribute.
    """

    execute = getattr(module, "execute", None)
    if execute is None or not callable(execute):
        raise SkillParseError("module is missing an execute() function")
    tool_name = (
        parsed_name
        or getattr(module, "__ash_name__", None)
        or getattr(execute, "__name__", "skill")
    )
    if parsed_description is not None:
        tool_description: str = parsed_description
    else:
        fallback = getattr(module, "__ash_description__", None)
        if not fallback:
            doc_first_line = (execute.__doc__ or "").strip().splitlines()[:1]
            fallback = doc_first_line[0] if doc_first_line else ""
        tool_description = fallback or "Python skill"

    tool_args_schema = _build_args_schema_from_signature(execute)

    class _PythonSkillTool(BaseTool):
        name = tool_name  # type: ignore[assignment,misc]
        description = tool_description  # type: ignore[assignment,misc]
        args_schema = tool_args_schema

        async def run(self, **kwargs: Any) -> ToolResult:
            context = SkillContext(
                safety_guard=self.safety_guard,
                tools={t.name: t for t in _get_registry_tools()},
                project_root=_get_project_root(),
            )
            try:
                result = await execute(context, **kwargs)  # type: ignore[misc]
            except SkillExecutionError as exc:
                return ToolResult(success=False, output="", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                return ToolResult(
                    success=False, output="", error=f"skill raised: {exc}"
                )
            if isinstance(result, ToolResult):
                return result
            return ToolResult(success=True, output=str(result))

    # Mark metadata for the registry to pick up.
    module.__ash_name__ = tool_name  # type: ignore[attr-defined]
    module.__ash_description__ = tool_description  # type: ignore[attr-defined]
    if parsed_trigger:
        module.__ash_trigger__ = parsed_trigger  # type: ignore[attr-defined]
    if source_path is not None:
        module.__ash_source_path__ = str(source_path)  # type: ignore[attr-defined]

    return _PythonSkillTool(safety_guard)


# --- compiler dispatch ----------------------------------------------------


class SkillParseError(ValueError):
    """Raised when a skill file cannot be parsed."""


def compile_skill(path: Path, safety_guard: SafetyGuard) -> BaseTool:
    """Compile a skill file (Python or markdown) into a :class:`BaseTool`."""

    if path.suffix == ".py":
        # Load as a module, then hand it to the module wrapper.
        import importlib.util
        import sys

        module_name = f"_ash_skill_compile_{path.stem}_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise SkillParseError(f"Could not load Python module from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise SkillParseError(f"Failed to import {path}: {exc}") from exc
        # Re-parse so docstring metadata (name/description/trigger) is
        # honoured even when the module did not set __ash_name__.
        parsed = parse_python_skill(path)
        return build_tool_from_python_module(
            module,
            safety_guard,
            source_path=path,
            parsed_name=parsed.name,
            parsed_description=parsed.description,
            parsed_trigger=parsed.trigger,
        )

    if path.suffix == ".md":
        return _compile_markdown_skill(path, safety_guard)

    raise SkillParseError(f"Unsupported skill extension: {path.suffix}")


def _compile_markdown_skill(path: Path, safety_guard: SafetyGuard) -> BaseTool:
    skill = parse_markdown_skill(path)
    args_model = _build_args_model(skill.args, default_name=skill.name)
    captured_code = skill.code
    captured_name = skill.name
    captured_description = skill.description
    captured_path = str(path)

    async def _executor(**kwargs: Any) -> Any:
        globals_ns: dict[str, Any] = {
            "SkillContext": SkillContext,
            "SkillExecutionError": SkillExecutionError,
            "ToolResult": ToolResult,
        }
        locals_ns: dict[str, Any] = dict(kwargs)
        exec(  # noqa: S102 — explicit user-authored skill code
            compile(captured_code, captured_path, "exec"),
            globals_ns,
            locals_ns,
        )
        execute_fn = locals_ns.get("execute")
        if not callable(execute_fn):
            raise SkillParseError(
                f"Markdown skill {captured_name!r} must define an `execute` function"
            )
        context = SkillContext(
            safety_guard=safety_guard,
            tools={t.name: t for t in _get_registry_tools()},
            project_root=_get_project_root(),
        )
        if asyncio_is_coroutine(execute_fn):
            return await execute_fn(context, **kwargs)
        return execute_fn(context, **kwargs)

    async def run(self: BaseTool, **kwargs: Any) -> ToolResult:  # noqa: ARG001
        try:
            validated = args_model(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, output="", error=f"invalid args: {exc}")
        # When the schema is the empty marker, dump() returns an empty
        # dict so we don't pollute the executor with phantom fields.
        passed_kwargs = (
            {}
            if type(validated).__name__.endswith("Args") and not validated.model_fields
            else validated.model_dump()
        )
        try:
            result = await _executor(**passed_kwargs)
        except SkillExecutionError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, output="", error=f"skill raised: {exc}")
        if isinstance(result, ToolResult):
            return result
        return ToolResult(success=True, output=str(result))

    tool_cls = type(
        f"_MarkdownSkillTool_{captured_name}",
        (BaseTool,),
        {
            "name": captured_name,
            "description": captured_description or "Markdown skill",
            "args_schema": args_model,
            "run": run,
        },
    )
    return tool_cls(safety_guard)


def _build_args_model(
    args: Sequence[tuple[str, str, str, str]],
    *,
    default_name: str,
) -> type[BaseModel]:
    """Build a Pydantic model from the markdown's ``## Args`` declarations."""

    fields: dict[str, tuple[Any, Any]] = {}
    for name, py_type, default_value, _desc in args:
        annotation = _resolve_python_type(py_type)
        if default_value:
            fields[name] = (annotation, _coerce_default(default_value, annotation))
        else:
            fields[name] = (annotation, ...)
    model_name = _safe_model_name(default_name)
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def _build_args_schema_from_signature(fn: Callable[..., Any]) -> type[BaseModel]:
    """Build an args schema from a Python skill's ``execute`` signature.

    When the function takes no real arguments (besides ``context``),
    returns an empty ``BaseModel`` subclass so the runtime can still
    instantiate the schema (Pydantic forbids instantiating BaseModel
    itself, so a subclass marker is required).
    """

    import inspect

    sig = inspect.signature(fn)
    fields: dict[str, tuple[Any, Any]] = {}
    for pname, param in sig.parameters.items():
        if pname == "context":
            continue
        annotation = (
            param.annotation if param.annotation is not inspect.Parameter.empty else str
        )
        if param.default is inspect.Parameter.empty:
            fields[pname] = (annotation, ...)
        else:
            fields[pname] = (annotation, param.default)
    if not fields:
        return create_model(  # type: ignore[return-value]
            _safe_model_name(getattr(fn, "__name__", "skill") + "Args")
        )
    model_name = _safe_model_name(getattr(fn, "__name__", "skill"))
    return create_model(model_name, **fields)  # type: ignore[call-overload]


def _resolve_python_type(type_name: str) -> Any:
    type_name = type_name.strip()
    mapping: dict[str, Any] = {
        "str": str,
        "string": str,
        "int": int,
        "integer": int,
        "float": float,
        "number": float,
        "bool": bool,
        "boolean": bool,
        "list": list,
        "dict": dict,
        "path": str,
    }
    if type_name in mapping:
        return mapping[type_name]
    if type_name.startswith("list["):
        return list
    if type_name.startswith("dict["):
        return dict
    return str


def _coerce_default(raw: str, annotation: Any) -> Any:
    if annotation in (int, float):
        try:
            return annotation(raw)
        except (TypeError, ValueError):
            return ...  # fall back to required
    if annotation is bool:
        return raw.lower() in {"true", "1", "yes"}
    if annotation in (list, dict):
        import json

        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    # Strip surrounding quotes for strings.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


def _safe_model_name(stem: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", stem)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"Skill_{cleaned}"
    return f"{cleaned}Args"


def asyncio_is_coroutine(fn: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


# --- self-extension ------------------------------------------------------


def write_python_skill(
    skill_dir: Path,
    *,
    name: str,
    description: str,
    trigger: str,
    body: str,
) -> Path:
    """Write a new Python skill file and return the path.

    The caller is expected to pass the returned path back to
    :meth:`ToolRegistry.reload_skill_module` so the new skill becomes
    active without restarting the agent.
    """

    if not name.isidentifier():
        raise ValueError(f"skill name must be a valid Python identifier, got {name!r}")
    skill_dir = skill_dir.expanduser()
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / f"{name}.py"
    body = textwrap.dedent(body).strip("\n")
    docstring = (
        f'"""\nname: {name}\ndescription: {description}\ntrigger: {trigger}\n"""'
    )
    contents = f"{docstring}\n\n{body}\n"
    path.write_text(contents, encoding="utf-8")
    return path


# --- hooks for runtime context ------------------------------------------


_TOOLS_PROVIDER: Callable[[], list[BaseTool]] | None = None
_ROOT_PROVIDER: Callable[[], Path] | None = None


def configure_runtime(
    *, tools_provider: Callable[[], list[BaseTool]], root_provider: Callable[[], Path]
) -> None:
    """Inject the runtime context used by compiled skills.

    Called by the loop / entry point so compiled markdown skills can
    access the tool registry and project root without importing the
    ash package directly (which would create circular imports).
    """

    global _TOOLS_PROVIDER, _ROOT_PROVIDER
    _TOOLS_PROVIDER = tools_provider
    _ROOT_PROVIDER = root_provider


def _get_registry_tools() -> list[BaseTool]:
    if _TOOLS_PROVIDER is None:
        return []
    return list(_TOOLS_PROVIDER())


def _get_project_root() -> Path:
    if _ROOT_PROVIDER is None:
        return Path.cwd()
    return _ROOT_PROVIDER()
