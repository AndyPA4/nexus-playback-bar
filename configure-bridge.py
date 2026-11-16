"""Create a private per-install pairing key for the Windows app and Chrome."""
import json
from pathlib import Path
import secrets
import sys


def configure(app_directory, chrome_directory):
    key = secrets.token_hex(32)
    app_key = app_directory / "bridge-key.txt"
    config = chrome_directory / "bridge-config.js"
    app_pending = app_key.with_suffix(".tmp")
    config_pending = config.with_suffix(".tmp")
    app_pending.write_text(key + "\n", encoding="ascii")
    config_pending.write_text(
        "globalThis.NEXUS_BRIDGE_KEY = " + json.dumps(key) + ";\n", encoding="ascii")
    config_pending.replace(config)
    app_pending.replace(app_key)


if __name__ == "__main__":
    configure(Path(sys.argv[1]), Path(sys.argv[2]))
