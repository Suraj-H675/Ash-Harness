"""Run Ash with ``python -m ash``."""

from ash.cli import _build_tools, main

__all__ = ["_build_tools", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
