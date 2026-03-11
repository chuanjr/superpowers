import json
from pathlib import Path

DEFAULT_STATE_PATH = Path("state.json")


def load_seen_ids(path: Path = DEFAULT_STATE_PATH) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return set(data.get("seen_ids", []))


def save_seen_ids(ids: set[str], path: Path = DEFAULT_STATE_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen_ids": list(ids)}, indent=2))
    tmp.replace(path)  # atomic on POSIX
