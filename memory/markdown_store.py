"""Plain Markdown file memory store."""

from pathlib import Path


class MarkdownMemoryStore:
    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, content: str) -> None:
        (self.memory_dir / f"{key}.md").write_text(content)

    def load(self, key: str) -> str | None:
        path = self.memory_dir / f"{key}.md"
        return path.read_text() if path.exists() else None

    def list_keys(self) -> list[str]:
        return [p.stem for p in self.memory_dir.glob("*.md")]
