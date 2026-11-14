"""Per-user Windows startup registration for NEXUS Playback Bar."""
import subprocess
import sys
import winreg
from pathlib import Path

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "NexusPlaybackBar"


def enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN) as key:
            winreg.QueryValueEx(key, NAME)
            return True
    except FileNotFoundError:
        return False


def set_enabled(value, script):
    if value:
        python = Path(sys.executable).with_name("pythonw.exe")
        command = subprocess.list2cmdline([str(python), str(Path(script).resolve())])
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN, access=winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, NAME)
        except FileNotFoundError:
            pass
