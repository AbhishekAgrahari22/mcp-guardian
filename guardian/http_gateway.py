"""Authenticated Streamable HTTP reverse proxy for a fixed upstream MCP endpoint."""
import asyncio
import base64
import hmac
import os
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web
from .approval import AsyncApprovalClient
from .protocol import (MAX_MESSAGE, SAFE_NOTIFICATIONS, classify, decode, error, denied,
                       describe, request_key, cancel_id, requires_approval)

# Explicit allowlist prevents forwarding hop-by-hop headers, client secrets or cookies.
REQUEST_HEADERS = ('Accept','Content-Type','Mcp-Session-Id','Mcp-Protocol-Version','Last-Event-ID',
                   'Mcp-Method','Mcp-Name')
FORWARD = web.AppKey('forward', ClientSession)
APPROVALS = web.AppKey('approvals', AsyncApprovalClient)
RESPONSE_HEADERS = ('Content-Type','Mcp-Session-Id','Mcp-Protocol-Version','Cache-Control','Retry-After','Mcp-Request-Id')


def create_app(cfg, upstream, server='remote-mcp', policy='strict', public_origin=None):
    parsed = urlsplit(upstream)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Upstream must be a fixed HTTP(S) URL without embedded credentials')
    app = web.Application(client_max_size=MAX_MESSAGE)
    pending = {}
    open_count = 0

    async def resources(app):
        async with ClientSession(timeout=ClientTimeout(total=None, connect=10, sock_read=120),
                                 auto_decompress=False, trust_env=False, cookie_jar=DummyCookieJar()) as forward, ClientSession(trust_env=False, auto_decompress=True) as broker:
            app[FORWARD] = forward
            app[APPROVALS] = AsyncApprovalClient(cfg, broker)
            yield

    app.cleanup_ctx.append(resources)

    async def relay(req, body):
        headers = {name:req.headers[name] for name in REQUEST_HEADERS if name in req.headers}
        headers['Accept-Encoding'] = 'identity'
        token = os.environ.get('GUARDIAN_UPSTREAM_BEARER')
        if token: headers['Authorization'] = 'Bearer '+token
        try:
            async with req.app[FORWARD].request(req.method, upstream, data=body, headers=headers, allow_redirects=False) as res:
                if 300 <= res.status < 400:
                    return web.json_response({'error':'Upstream redirects are not followed'},status=502)
                out_headers = {name:res.headers[name] for name in RESPONSE_HEADERS if name in res.headers}
                if res.headers.get('Content-Encoding'):
                    out_headers['Content-Encoding'] = res.headers['Content-Encoding']
                response = web.StreamResponse(status=res.status, headers=out_headers)
                await response.prepare(req)
                try:
                    async for chunk in res.content.iter_chunked(16384): await response.write(chunk)
                    await response.write_eof()
                except (OSError, asyncio.TimeoutError):
                    response.force_close()
                return response
        except (OSError, asyncio.TimeoutError):
            return web.json_response({'error':'Upstream unavailable'}, status=502)
        except Exception:
            return web.json_response({'error':'Upstream communication failed'}, status=502)

    async def endpoint(req):
        nonlocal open_count
        if not hmac.compare_digest(req.headers.get('X-Guardian-Key','').encode(), cfg['web_key'].encode()):
            return web.json_response({'error':'Missing or invalid Guardian key'},status=401)
        origin = req.headers.get('Origin')
        if origin and origin != public_origin:
            return web.json_response({'error':'Origin not allowed'},status=403)
        if req.method not in ('GET','POST','DELETE'):
            return web.Response(status=405, headers={'Allow':'GET, POST, DELETE'})
        if open_count >= cfg['max_pending']*4:
            return web.json_response({'error':'Gateway busy'},status=503)
        open_count += 1
        key = None
        event = None
        try:
            if req.method == 'DELETE':
                session = req.headers.get('Mcp-Session-Id')
                if session:
                    for k, ev in list(pending.items()):
                        if k[0] == session: ev.set()
                return await relay(req, None)
            if req.method == 'GET': return await relay(req, None)
            if req.content_type != 'application/json': return web.Response(status=415)
            if req.headers.get('Content-Encoding','identity').lower() != 'identity': return web.Response(status=415)
            body = await req.read()
            try:
                msg = decode(body); kind = classify(msg)
            except (ValueError,TypeError,RecursionError):
                return web.json_response(error(None,-32600,'Invalid JSON-RPC request'),status=400)
            # The 2025 transport is the tested scope. Reject unvalidated custom
            # mirrored parameter headers rather than misrepresent a request.
            if any(h.lower().startswith('mcp-param-') for h in req.headers):
                return web.json_response(error(msg.get('id'),-32020,'Mirrored parameter headers are not supported'),status=400)
            method_header = req.headers.get('Mcp-Method')
            name_header = req.headers.get('Mcp-Name')
            try:
                if name_header and name_header.startswith('=?base64?') and name_header.endswith('?='):
                    name_header = base64.b64decode(name_header[9:-2],validate=True).decode('utf-8')
                expected_name = msg.get('params',{}).get('uri' if msg.get('method')=='resources/read' else 'name')
                if (method_header is not None and method_header != msg.get('method')) or (name_header is not None and name_header != expected_name):
                    raise ValueError('Header mismatch')
            except (ValueError,TypeError):
                return web.json_response(error(msg.get('id'),-32020,'Header mismatch'),status=400)
            session = req.headers.get('Mcp-Session-Id','')
            if kind == 'notification':
                if msg['method'] not in SAFE_NOTIFICATIONS: return web.Response(status=403)
                if msg['method'] == 'notifications/cancelled':
                    ev = pending.get((session,cancel_id(msg)))
                    if ev:
                        ev.set(); return web.Response(status=202)
            if kind == 'request':
                new_key = (session,request_key(msg['id']))
                if new_key in pending:
                    return web.json_response(error(msg['id'],-32600,'Duplicate pending request ID'))
                if requires_approval(msg,policy):
                    if len(pending) >= cfg['max_pending']:
                        return web.json_response(denied(msg,'Approval queue full'))
                    key = new_key; event = asyncio.Event(); pending[key] = event
                    ok = await req.app[APPROVALS].wait(describe(msg,'Claude Web / HTTP',server),event)
                    if not ok or event.is_set(): return web.json_response(denied(msg))
                    # Remove as soon as released so later cancellation reaches upstream.
                    pending.pop(key,None); key = None
            return await relay(req,body)
        finally:
            if event: event.set()
            if key is not None: pending.pop(key,None)
            open_count -= 1

    app.router.add_route('*','/mcp',endpoint)
    return app


def run(cfg, upstream, server, policy, port, public_origin):
    web.run_app(create_app(cfg,upstream,server,policy,public_origin),host='127.0.0.1',port=port,
                access_log=None, handler_cancellation=True)
