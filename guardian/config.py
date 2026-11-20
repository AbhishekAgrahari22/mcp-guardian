"""Local configuration. Secrets are generated, never checked in."""
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('GUARDIAN_HOME', ROOT / '.guardian')).expanduser().resolve()


def initialize():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE / 'config.json'
    if not path.exists():
        data = {'broker_port': 8765, 'broker_key': secrets.token_urlsafe(32),
                'web_key': secrets.token_urlsafe(32), 'timeout': 45, 'max_pending': 32}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except FileExistsError:
            pass
    return load()


def load():
    data = json.loads((STATE / 'config.json').read_text(encoding='utf-8'))
    if not all(isinstance(data.get(k), str) and len(data[k]) >= 24 for k in ('broker_key','web_key')):
        raise ValueError('Invalid Guardian keys; run setup')
    if not 1 <= data['timeout'] <= 120 or not 1 <= data['max_pending'] <= 256:
        raise ValueError('Invalid timeout or max_pending')
    return data
