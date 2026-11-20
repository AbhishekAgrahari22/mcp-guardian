"""macOS native confirmation via Apple's osascript, with a fail-closed FIFO queue."""
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from .broker import ApprovalStore, make_server
from .config import STATE

log = logging.getLogger(__name__)
SCRIPT = Path(__file__).with_name('mac_dialog.applescript')
MAX_DIALOG_TEXT = 1500


def message(item):
    # Do not truncate arguments and then invite an uninformed approval.
    text = ('Review this request before execution.\n\n'
            f"Source: {item['source']}\nServer: {item['server']}\n"
            f"Method: {item['method']}\nTool: {item['tool'] or '—'}\n"
            f"Request ID: {item['request_id']}\n\nParameters:\n"
            + json.dumps(item['params'], ensure_ascii=True, indent=2))
    if len(text) > MAX_DIALOG_TEXT or any(ord(c) < 32 and c not in '\n\t' for c in text):
        raise ValueError('Request too large or unsafe for native dialog; use --ui tk to review large requests')
    return text


def terminate(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()


def review(store, item, stop):
    key = item['id']
    proc = None
    # Private temporary file, not command-line arguments or executable script text.
    try:
        text = message(item)
        with tempfile.TemporaryDirectory(prefix='guardian-dialog-', dir=STATE) as directory:
            path = Path(directory) / 'request.txt'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            seconds = max(1, math.ceil(item['deadline'] - time.monotonic()))
            if stop.is_set() or store.status(key) != 'pending':
                return
            log.info('Displaying native macOS approval %s', key)
            proc = subprocess.Popen(['/usr/bin/osascript', str(SCRIPT), str(path), str(seconds)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                while not stop.is_set() and store.status(key) == 'pending':
                    try:
                        out, err = proc.communicate(timeout=.2)
                    except subprocess.TimeoutExpired:
                        continue
                    decision = out.strip()
                    if proc.returncode == 0 and decision == 'allow' and not stop.is_set():
                        store.decide(key, 'allow')
                    elif proc.returncode == 0 and decision == 'timeout':
                        # Broker is the authoritative clock: let it finish timeout.
                        while not stop.wait(.05) and store.status(key) == 'pending':
                            pass
                    else:
                        if proc.returncode != 0:
                            log.error('Native dialog failed (exit %s); request denied', proc.returncode)
                        store.decide(key, 'block')
                    break
            finally:
                terminate(proc)
    except Exception as exc:
        log.error('Native approval failed: %s', type(exc).__name__)
        store.decide(key, 'block')
    finally:
        log.info('Approval %s completed: %s', key, store.status(key))


def run(cfg):
    store = ApprovalStore(cfg['timeout'], cfg['max_pending'], STATE / 'audit.sqlite3')
    try:
        server = make_server(store, cfg['broker_port'], cfg['broker_key'])
    except Exception:
        store.close(); raise
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info('Approval agent listening on 127.0.0.1:%s (macOS native dialog)', cfg['broker_port'])
    try:
        while not stop.is_set():
            items = store.pending()
            if items:
                review(store, items[0], stop)
            else:
                stop.wait(.1)
    finally:
        server.shutdown(); server.server_close(); thread.join(); store.close()
