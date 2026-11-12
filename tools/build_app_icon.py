"""Generate the exact FlowDesk FD monogram as a Windows executable icon.

Run before PyInstaller:
    python tools/build_app_icon.py
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowdesk.app_icon import write_executable_icon

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "flowdesk" / "assets" / "flowdesk.ico"
    print("Windows FlowDesk icon:", write_executable_icon(target))
