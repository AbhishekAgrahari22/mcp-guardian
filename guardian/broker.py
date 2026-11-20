"""Thread-safe approval state and authenticated loopback-only HTTP broker."""
import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .protocol import decode, encode, MAX_MESSAGE


log = logging.getLogger(__name__)


class ApprovalStore:
    def __init__(self, timeout, limit, audit_path):
        self.timeout, self.limit = timeout, limit
        self.lock = threading.RLock()
        self.items = {}
        self.closed = False
        self.db = sqlite3.connect(audit_path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, time REAL, source TEXT, server TEXT, method TEXT, decision TEXT)')
        self.db.commit()

    def _sweep(self):
        now = time.monotonic()
        for key, item in list(self.items.items()):
            if item['decision'] == 'pending' and now >= item['deadline']:
                self._finish(key, 'timeout')
            if item['decision'] != 'pending' and now >= item['deadline'] + 60:
                del self.items[key]

    def _finish(self, key, decision):
        item = self.items.get(key)
        if not item or item['decision'] != 'pending':
            return False
        item['decision'] = decision
        try:
            self.db.execute('INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)',
                            (key, time.time(), item['source'], item['server'], item['method'], decision))
            self.db.commit()
        except sqlite3.Error:
            # Audit failure never turns a denial into an approval.
            if decision == 'allow':
                item['decision'] = 'audit_failure'
        item.pop('params', None)
        log.info('Approval %s resolved: %s', key, item['decision'])
        return True

    def create(self, info):
        with self.lock:
            self._sweep()
            if self.closed or sum(i['decision']=='pending' for i in self.items.values()) >= self.limit or len(self.items) >= self.limit * 16:
                raise RuntimeError('Approval queue unavailable or full')
            key = uuid.uuid4().hex
            self.items[key] = {**info, 'id':key, 'decision':'pending', 'deadline':time.monotonic()+self.timeout}
            log.info('Approval %s queued', key)
            return key

    def pending(self):
        with self.lock:
            self._sweep()
            return [dict(i) for i in self.items.values() if i['decision']=='pending']

    def status(self, key):
        with self.lock:
            self._sweep()
            return self.items.get(key, {}).get('decision', 'unknown')

    def decide(self, key, decision):
        if decision not in {'allow','block','cancelled','shutdown'}:
            raise ValueError('Unknown decision')
        with self.lock:
            if self.closed: return False
            self._sweep()
            return self._finish(key, decision)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for key in self.items:
                self._finish(key, 'shutdown')
            self.db.close()


def make_server(store, port, secret):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Do not leak request data or credentials.

        def reply(self, status, data):
            body = encode(data)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            value = self.headers.get('X-Guardian-Local-Key', '')
            if not hmac.compare_digest(value.encode(), secret.encode()):
                self.reply(403, {'error':'Forbidden'})
                return False
            return True

        def do_POST(self):
            if not self.authenticated(): return
            if self.path != '/requests': return self.reply(404, {})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= MAX_MESSAGE * 2: raise ValueError()
                info = json.loads(self.rfile.read(n))
                if not isinstance(info, dict) or set(info) != {'source','server','method','request_id','tool','params'}: raise ValueError()
                if not all(isinstance(info[k], str) for k in ('source','server','method','tool')) or not isinstance(info['params'], dict): raise ValueError()
                return self.reply(201, {'id':store.create(info)})
            except (ValueError, TypeError, KeyError): return self.reply(400, {'error':'Invalid approval request'})
            except RuntimeError: return self.reply(503, {'error':'Queue unavailable'})

        def do_GET(self):
            if not self.authenticated(): return
            if not self.path.startswith('/requests/'): return self.reply(404, {})
            return self.reply(200, {'decision':store.status(self.path.split('/')[-1])})

        def do_DELETE(self):
            if not self.authenticated(): return
            if not self.path.startswith('/requests/'): return self.reply(404, {})
            key = self.path.split('/')[-1]
            store.decide(key, 'cancelled')
            return self.reply(200, {'ok':True})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

    return Server(('127.0.0.1', port), Handler)
