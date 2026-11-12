"""Keep the complete last worker exception for the existing diagnostic export."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import traceback

_lock = threading.Lock()


def last_error_path():
    return Path.home()/'.flowdesk'/'last_runtime_error.json'


def record_exception(context, error):
    try:
        value = {'at': datetime.now(timezone.utc).isoformat(), 'context': context,
                 'type': type(error).__name__, 'message': str(error),
                 'traceback': ''.join(traceback.format_exception(error))}
        with _lock:
            path = last_error_path(); path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            temp.replace(path)
    except Exception:
        pass


def last_exception():
    try:
        return json.loads(last_error_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
