"""Windows package test: post WM_CLOSE to the actual bundled Qt window."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    exe = Path(sys.argv[1]).resolve()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.EnumWindows.restype = user32.PostMessageW.restype = wintypes.BOOL
    results = []
    for delay in (0, .8, 3):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'USERPROFILE': directory, 'QT_QPA_PLATFORM': 'windows'}
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            process = subprocess.Popen([str(exe)], env=env, startupinfo=startup)
            try:
                window = []
                @callback_type
                def match(hwnd, value):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == process.pid:
                        title = ctypes.create_unicode_buffer(512)
                        user32.GetWindowTextW(hwnd, title, len(title))
                        if title.value.startswith('FlowDesk '):
                            window.append(hwnd)
                    return True
                deadline = time.monotonic() + 25
                while not window and time.monotonic() < deadline:
                    assert process.poll() is None, f'EXE ended before creating window: {process.returncode}'
                    user32.EnumWindows(match, 0)
                    time.sleep(.05)
                assert window, 'Packaged Qt window not found'
                time.sleep(delay)
                started = time.monotonic()
                assert user32.PostMessageW(window[0], 0x0010, 0, 0), ctypes.get_last_error()
                code = process.wait(timeout=5)
                elapsed = time.monotonic() - started
                assert code == 0, code
                results.append({'close_after_seconds': delay, 'exit_seconds': round(elapsed, 3), 'code': code})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    Path('shutdown-exe-result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('PACKAGED_EXIT_OK', json.dumps(results), flush=True)


if __name__ == '__main__':
    main()
