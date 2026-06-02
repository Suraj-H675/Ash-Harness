# ASH Project Instructions

## Build and Run Commands
- Run Ash locally: `python -m ash`
- Configure environment: Create `.env` using templates in `.env.example`
- Local configuration file: `ash.toml` in workspace root

## Testing Commands
- Run all unit tests: `pytest tests/unit`
- Run integration tests: `pytest tests/integration`
- Run a specific test: `pytest tests/unit/test_config.py`
- Run with print outputs: `pytest -s`

## Code Quality & Style Guidelines
- Formatting and Linting: `ruff check` and `ruff format`
- Type checking: `mypy ash`
- Style conventions: Python 3.12+, PEP 8 compliance, strict type annotations, and docstrings for all classes and functions.
- Concurrency Rule: Initialize SQLite connections using `check_same_thread=False` and serialize write actions using `write_transaction` async locks.
- Security Rule: All file tools must route paths through `SafetyGuard.validate_path` before reading/writing.

## Development & AI Best Practices

### AI Code-Writing Rules
- **Test-Driven Design (TDD)**: Always write unit tests *concurrently* when writing new features or refactoring modules. Do not wait until the end of the sprint.
- **Ruff Enforcement**: Run `ruff check --fix` and `ruff format` before declaring a file complete. Never commit code with trailing spaces or unresolved formatting issues.
- **No Global Singletons**: Every manager class (e.g. `ToolRegistry`, `SessionStore`) must be instantiated at the loop layer and passed via Dependency Injection. Global state variables are strictly forbidden.
- **Documentation Preservation**: Maintain all docstrings and existing file comments. Do not delete explanatory notes unless explicitly instructed.
- **Agent Tool & Capability Freedom**: You are fully authorized and encouraged to utilize any custom plugins, local skills, search engines, or Model Context Protocol (MCP) servers connected to your host workspace to speed up coding, compile tests, or analyze bugs. Do not limit yourself if external capabilities can help you build the codebase more effectively.




### Karpathy-Style AI Directives
- **Think Before Coding**: Never make silent assumptions. If a request is ambiguous or lacks constraints, explain your assumptions or ask for clarification before writing any code.
- **Simplest Solution First**: Avoid overengineering and unnecessary abstractions. Prioritize simple, readable, and direct implementations that satisfy the requirement.
- **Surgical Changes**: Only modify the files and lines of code directly required to fulfill the goal. Do not perform "drive-by" refactorings of unrelated parts of the codebase.
- **Goal-Driven Execution**: Define clear success criteria (e.g. specific tests passing) and execute in a loop until those exact goals are met.

## Blueprint Reference Map
- Roadmap: [ASH_MASTER_PLAN_V2.md]
- Playbook Checklist: [GOAL_SPRINTS.md]
- Architectures Spec: [ARCHITECTURAL_SPECIFICATION.md]
- Tools Spec: [TOOL_SPECIFICATIONS.md]
- Context Spec: [CONTEXT_AND_MEMORY_SPECIFICATION.md]
- Prompts Spec: [SYSTEM_PROMPTS_AND_TEMPLATES.md]
