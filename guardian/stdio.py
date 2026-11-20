"""Transparent newline-delimited JSON-RPC proxy, including bidirectional traffic."""
import concurrent.futures
import logging
import os
import subprocess
import sys
import threading
from .approval import ApprovalClient
from .protocol import (MAX_MESSAGE, SAFE_NOTIFICATIONS, classify, decode, encode, error,
                       denied, describe, request_key, cancel_id, requires_approval)

LOG = logging.getLogger(__name__)


def run(cfg, command, server, policy):
    if not command: raise ValueError('Missing upstream command after --')
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
    client = ApprovalClient(cfg)
    pending = {}
    lock = threading.RLock()
    output_lock = threading.Lock()
    stop = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(cfg['max_pending'],32))

    def lines(fd):
        # Use raw descriptors so shutdown cannot deadlock on a daemon thread
        # holding Python's buffered stdin lock (notably on Windows/macOS).
        buf = bytearray()
        while not stop.is_set():
            chunk = os.read(fd, 8192)
            if not chunk: break
            buf.extend(chunk)
            while b'\n' in buf:
                end = buf.index(b'\n') + 1
                yield bytes(buf[:end]); del buf[:end]
            if len(buf) > MAX_MESSAGE+1: raise ValueError('Oversized line')

    def send_client(raw):
        with output_lock:
            view = memoryview(raw + b'\n')
            while view:
                view = view[os.write(sys.stdout.fileno(), view):]

    def send_upstream(raw):
        if stop.is_set(): raise BrokenPipeError('Proxy closing')
        proc.stdin.write(raw + b'\n'); proc.stdin.flush()

    def worker(msg, raw, key, cancelled):
        try:
            ok = client.wait(describe(msg, 'Claude Desktop / STDIO', server), cancelled)
            with lock:
                if stop.is_set(): return
                if ok and not cancelled.is_set():
                    send_upstream(raw)
                else:
                    send_client(encode(denied(msg)))
        except Exception:
            if not stop.is_set():
                try: send_client(encode(error(msg['id'], -32002, 'Upstream unavailable')))
                except Exception: pass
        finally:
            with lock: pending.pop(key, None)

    def read_client():
        try:
            for raw in lines(sys.stdin.fileno()):
                if len(raw.rstrip(b'\r\n')) > MAX_MESSAGE or not raw.endswith(b'\n'):
                    LOG.warning('Oversized or incomplete STDIO line; closing'); break
                raw = raw.rstrip(b'\r\n')
                try:
                    msg = decode(raw); kind = classify(msg)
                except (ValueError, TypeError, RecursionError):
                    send_client(encode(error(None,-32600,'Invalid request'))); continue
                with lock:
                    if kind == 'notification':
                        if msg['method'] not in SAFE_NOTIFICATIONS: continue
                        if msg['method'] == 'notifications/cancelled':
                            event = pending.get(cancel_id(msg))
                            if event is not None:
                                event.set(); continue
                        send_upstream(raw)
                    elif kind == 'request':
                        key = request_key(msg['id'])
                        if key in pending:
                            send_client(encode(error(msg['id'],-32600,'Duplicate pending request ID')))
                        elif requires_approval(msg, policy):
                            if len(pending) >= cfg['max_pending']:
                                send_client(encode(denied(msg,'Approval queue full'))); continue
                            event = threading.Event(); pending[key] = event
                            pool.submit(worker,msg,raw,key,event)
                        else: send_upstream(raw)
                    else: send_upstream(raw)
        except (OSError, ValueError):
            LOG.info('Client disconnected or upstream closed')
        finally: stop.set()

    def read_server():
        try:
            for raw in lines(proc.stdout.fileno()):
                if len(raw.rstrip(b'\r\n')) > MAX_MESSAGE or not raw.endswith(b'\n'): break
                # Server-to-client requests, responses and notifications are relayed unchanged.
                send_client(raw.rstrip(b'\r\n'))
        except (OSError, ValueError): pass
        finally: stop.set()

    threading.Thread(target=read_client, daemon=True).start()
    threading.Thread(target=read_server, daemon=True).start()
    try:
        while not stop.wait(.15):
            if proc.poll() is not None: break
    except KeyboardInterrupt: pass
    finally:
        stop.set()
        # Terminate first, so a thread blocked on a full upstream pipe is released.
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        with lock:
            for event in pending.values(): event.set()
        pool.shutdown(wait=True, cancel_futures=True)
        # Do not join daemon readers: stdin can remain open indefinitely in a host.
    return 0
