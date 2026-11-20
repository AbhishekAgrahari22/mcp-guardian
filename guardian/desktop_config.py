"""Back up and merge only our own Claude Desktop MCP entry."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from .config import ROOT


def generated():
    script = str(ROOT / 'main.py')
    python = sys.executable
    return {'mcpServers': {'guardian-demo': {'command': python,
        'args': [script, 'stdio', '--name', 'demo-files', '--policy', 'actions', '--', python, script, 'demo-stdio']}}}


def config_path():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Claude/claude_desktop_config.json'
    if sys.platform == 'win32':
        return Path(os.environ['APPDATA']) / 'Claude/claude_desktop_config.json'
    raise ValueError('Use desktop-config and merge manually on this platform')


def update(remove=False, path=None):
    path = config_path() if path is None else Path(path)
    if remove and not path.exists():
        print('No Claude config found. Nothing to remove.'); return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('Unexpected Claude config format; no changes made')
    if remove:
        data.get('mcpServers', {}).pop('guardian-demo', None)
    else:
        data.setdefault('mcpServers', {}).update(generated()['mcpServers'])
    if path.exists():
        backup = path.with_name(path.name + f'.guardian-{time.time_ns()}.bak')
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        print('Backup:', backup)
    fd, tmp = tempfile.mkstemp(prefix='.guardian-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print('Updated:', path)
    print('Removed guardian-demo only.' if remove else 'Installed guardian-demo. Restart Claude Desktop.')
