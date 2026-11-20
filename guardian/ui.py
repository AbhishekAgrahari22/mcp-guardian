"""Native approval UI. Use a single main window to avoid hidden macOS child windows."""
import json
import logging
import signal
import threading
import time
import tkinter as tk
from tkinter import ttk
from .broker import ApprovalStore, make_server
from .config import STATE

log = logging.getLogger(__name__)


def run(cfg):
    root = tk.Tk()
    store = ApprovalStore(cfg['timeout'], cfg['max_pending'], STATE / 'audit.sqlite3')
    try:
        server = make_server(store, cfg['broker_port'], cfg['broker_key'])
    except Exception:
        store.close()
        root.destroy()
        raise
    threading.Thread(target=server.serve_forever, daemon=True).start()
    active = True
    current = None
    item = None
    panel = None
    countdown = None
    status = tk.StringVar(value='Agent running • 0 pending request(s)')

    def shutdown():
        nonlocal active
        if not active:
            return
        active = False
        store.close()
        server.shutdown()
        server.server_close()
        root.destroy()

    def raise_window(key):
        if not active or current != key:
            return
        root.deiconify()
        root.update_idletasks()
        root.lift()
        root.attributes('-topmost', True)
        root.focus_force()

    def replace_panel():
        nonlocal panel, countdown
        if panel is not None:
            panel.destroy()
        countdown = None
        panel = ttk.Frame(root, padding=18)
        panel.pack(fill='both', expand=True)
        return panel

    def show_idle():
        root.attributes('-topmost', False)
        root.title('MCP Guardian | Approval agent')
        root.minsize(400, 170)
        root.geometry('440x190')
        frame = replace_panel()
        ttk.Label(frame, text='MCP GUARDIAN', font=('Arial', 21, 'bold')).pack(pady=(2, 4))
        ttk.Label(frame, textvariable=status, wraplength=400).pack(pady=8)
        bar = ttk.Frame(frame)
        bar.pack(pady=10)
        ttk.Button(bar, text='Minimize', command=root.iconify).pack(side='left', padx=5)
        ttk.Button(bar, text='Exit — block pending', command=shutdown).pack(side='left', padx=5)

    def decide(key, value):
        if current != key:
            return
        if store.decide(key, value):
            log.info('Approval %s: %s', key, value)
        else:
            log.info('Approval %s no longer pending', key)

    def close_window():
        if current is not None:
            decide(current, 'block')
        else:
            root.iconify()

    def show_request(request):
        nonlocal countdown
        key = request['id']
        root.title('MCP Guardian — Approval required')
        root.minsize(480, 360)
        root.geometry('660x490')
        frame = replace_panel()
        ttk.Label(frame, text='Review MCP request', font=('Arial', 18, 'bold')).pack(anchor='w')
        details = (f"Source: {request['source']}\nServer: {request['server']}\n"
                   f"Method: {request['method']}\nTool: {request['tool'] or '—'}\n"
                   f"Request ID: {request['request_id']}")
        ttk.Label(frame, text=details, wraplength=590, justify='left').pack(anchor='w', pady=10)
        ttk.Label(frame, text='Parameters (untrusted request data):').pack(anchor='w')
        content = ttk.Frame(frame)
        content.pack(fill='both', expand=True, pady=6)
        text = tk.Text(content, wrap='word', height=9, font=('Courier', 11), takefocus=False)
        scroll = ttk.Scrollbar(content, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', json.dumps(request['params'], indent=2, ensure_ascii=True))
        text.configure(state='disabled')
        countdown = ttk.Label(frame, text='')
        countdown.pack(anchor='w')
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Block', command=lambda: decide(key, 'block')).pack(side='left', expand=True, fill='x', padx=5)
        ttk.Button(buttons, text='Allow once', command=lambda: decide(key, 'allow')).pack(side='left', expand=True, fill='x', padx=5)
        log.info('Displaying approval %s', key)
        raise_window(key)
        # Retry after the window manager has mapped the restored window.
        root.after(200, lambda: raise_window(key))
        root.after(700, lambda: raise_window(key))
        root.bell()

    def tick():
        nonlocal current, item
        if not active:
            return
        try:
            items = store.pending()
            status.set(f'Agent running • {len(items)} pending request(s)')
            if current is not None and store.status(current) != 'pending':
                log.info('Approval %s completed: %s', current, store.status(current))
                current = item = None
                show_idle()
            if current is None and items:
                item = items[0]
                current = item['id']
                show_request(item)
            if current is not None and countdown is not None:
                remaining = max(0, int(item['deadline'] - time.monotonic()))
                countdown.configure(text=f'Expires in {remaining}s • close to block')
        except Exception:
            log.exception('Approval UI error; denying current request')
            if current is not None:
                store.decide(current, 'block')
            current = item = None
            show_idle()
        finally:
            if active:
                root.after(100, tick)

    root.protocol('WM_DELETE_WINDOW', close_window)
    root.bind('<Escape>', lambda _: decide(current, 'block') if current is not None else None)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: root.after(0, shutdown))
    show_idle()
    log.info('Approval agent listening on 127.0.0.1:%s', cfg['broker_port'])
    root.after(100, tick)
    try:
        root.mainloop()
    finally:
        if active:
            shutdown()
