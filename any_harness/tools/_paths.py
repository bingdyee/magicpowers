import os
from pathlib import Path


def resolve_working_path(dir_name: str | None = None) -> str:
    working_path = Path(os.environ.get("WORKING_ROOT", Path.cwd()))
    agent_name = os.environ.get("AGENT_NAME", "pardx")

    working_dir = Path(working_path / f".{agent_name}")
    if dir_name:
        working_dir = working_dir / dir_name

    working_dir.mkdir(parents=True, exist_ok=True)
    return working_dir.as_posix()
