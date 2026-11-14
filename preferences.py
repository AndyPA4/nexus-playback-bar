"""Version-independent user preferences."""
import json
import os
from pathlib import Path

PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NexusPlaybackBar" / "settings.json"


def load():
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(PATH)
