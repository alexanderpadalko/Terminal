"""Small local UI preferences; no indicator settings or market history."""
import json
import re
from pathlib import Path


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path or Path.home() / '.flowdesk' / 'ui_state.json')

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if (isinstance(value, dict) and value.get('board') in ('TQBR', 'RFUD') and isinstance(value.get('symbol'), str)
                    and re.fullmatch(r'[A-Z0-9._-]{1,32}', value['symbol'])
                    and value.get('tf') in ('M1', 'M5', 'H1', 'H4', 'D1', 'W1', 'MN1')):
                return value
        except (OSError, ValueError, TypeError):
            pass
        return {'symbol': 'SBER', 'board': 'TQBR', 'tf': 'H1'}

    def write(self, symbol, board, tf):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({'symbol': symbol, 'board': board, 'tf': tf}), encoding='utf-8')
            temp.replace(self.path)
        except OSError:
            pass
