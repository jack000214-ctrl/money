"""Link the canonical workflow into the user's Codex skill directory."""

import os
from pathlib import Path


def install(codex_home: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    destination = codex_home / "skills" / source.name
    if destination.is_symlink() and destination.resolve() == source:
        return destination
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"기존 스킬을 덮어쓰지 않습니다: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source, target_is_directory=True)
    return destination


if __name__ == "__main__":
    print(install(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")))
