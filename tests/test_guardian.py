"""Integration tests use real proxies and a headless approval broker, never auto-approve production."""
import asyncio
import json
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from guardian.broker import ApprovalStore, make_server
from guardian.protocol import classify, decode, requires_approval, encode
from guardian.demo import Demo, create_app as demo_app
from guardian.http_gateway import create_app as gateway_app

ROOT = Path(__file__).resolve().parents[1]


def req(i, method='tools/call', **args):
    params = {'name':'create_file','arguments':{'name':f'test{i}.txt','content':'hello'}} if method=='tools/call' else {}
    params.update(args)
    return {'jsonrpc':'2.0','id':i,'method':method,'params':params}


@pytest.fixture
def broker(tmp_path):
    cfg = {'broker_key':'L'*40,'web_key':'W'*40,'timeout':3,'max_pending':8}
    store = ApprovalStore(cfg['timeout'], cfg['max_pending'],tmp_path/'audit.sqlite')
    server = make_server(store,0,cfg['broker_key']); cfg['broker_port']=server.server_port
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    (tmp_path/'config.json').write_text(json.dumps(cfg))
    yield cfg,store
    server.shutdown(); server.server_close(); thread.join(); store.close()


def wait_pending(store,n=1):
    deadline = time.monotonic()+2
    while time.monotonic()<deadline:
        items=store.pending()
        if len(items)>=n: return items
        time.sleep(.01)
    raise AssertionError('No pending approval')


def test_protocol():
    assert requires_approval(req(1,'tools/list'),'strict')
    assert not requires_approval(req(1,'tools/list'),'actions')
    assert requires_approval(req(1,'future/dangerous'),'actions')
    for invalid in [b'[]',b'{"jsonrpc":"2.0","id":true,"method":"ping"}',b'{"method":"ping","method":"tools/call"}']:
        with pytest.raises(ValueError): classify(decode(invalid))


def test_broker_once_timeout_and_queue(tmp_path):
    store=ApprovalStore(.04,1,tmp_path/'db')
    info={'source':'test','server':'demo','method':'ping','tool':'','request_id':1,'params':{}}
    try:
        a=store.create(info)
        with pytest.raises(RuntimeError): store.create(info)
        assert store.decide(a,'block') and not store.decide(a,'allow')
        b=store.create(info); time.sleep(.06)
        assert not store.decide(b,'allow') and store.status(b)=='timeout'
        c=store.create(info); store.close(); assert store.status(c)=='shutdown'
    finally: store.close()


def test_sandbox(tmp_path):
    demo=Demo(tmp_path)
    with pytest.raises(ValueError): demo.tool('create_file',{'name':'../escape.txt','content':'x'})
    demo.tool('create_file',{'name':'ok.txt','content':'yes'})
    assert demo.tool('read_file',{'name':'ok.txt'})=='yes'
    with pytest.raises(FileExistsError): demo.tool('create_file',{'name':'ok.txt','content':'no'})


def test_stdio_approve_block_cancel_and_concurrency(broker,tmp_path):
    cfg,store=broker; folder=tmp_path/'files'
    proc=subprocess.Popen([sys.executable,str(ROOT/'main.py'),'stdio','--policy','actions','--',sys.executable,str(ROOT/'main.py'),'demo-stdio','--directory',str(folder)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**os.environ,'GUARDIAN_HOME':str(tmp_path)})
    import queue
    results=queue.Queue()
    def reader():
        for line in proc.stdout: results.put(json.loads(line))
    threading.Thread(target=reader,daemon=True).start()
    def send(msg): proc.stdin.write(encode(msg)+b'\n'); proc.stdin.flush()
    try:
        send(req(1,'initialize',protocolVersion='2025-11-25'))
        assert results.get(timeout=4)['result']['capabilities']['tools']=={'listChanged':False}
        send(req(2)); send(req(3)); items=wait_pending(store,2)
        assert not (folder/'test2.txt').exists() and not (folder/'test3.txt').exists()
        for item in items: store.decide(item['id'],'allow' if item['request_id']==2 else 'block')
        r={x['id']:x for x in [results.get(timeout=4),results.get(timeout=4)]}
        assert 'result' in r[2] and r[3]['error']['code']==-32001
        assert (folder/'test2.txt').read_text()=='hello' and not (folder/'test3.txt').exists()
        send(req(4)); item=wait_pending(store)[0]
        send({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':4}})
        assert results.get(timeout=4)['error']['code']==-32001
        assert not (folder/'test4.txt').exists()
        send(req(5)); assert results.get(timeout=6)['error']['code']==-32001
        assert not (folder/'test5.txt').exists()
    finally:
        proc.stdin.close()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        assert proc.returncode==0, proc.stderr.read().decode()


def test_stdio_agent_unavailable(tmp_path):
    (tmp_path/'config.json').write_text(json.dumps({'broker_port':1,'broker_key':'a'*32,'web_key':'b'*32,'timeout':1,'max_pending':2}))
    p=subprocess.Popen([sys.executable,str(ROOT/'main.py'),'stdio','--policy','actions','--',sys.executable,str(ROOT/'main.py'),'demo-stdio','--directory',str(tmp_path/'files')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,env={**os.environ,'GUARDIAN_HOME':str(tmp_path)})
    import queue
    q=queue.Queue(); threading.Thread(target=lambda:q.put(p.stdout.readline()),daemon=True).start()
    try:
        p.stdin.write(encode(req(7))+b'\n'); p.stdin.flush(); assert json.loads(q.get(timeout=5))['error']['code']==-32001
        assert not (tmp_path/'files'/'test7.txt').exists()
    finally:
        p.stdin.close(); p.wait(timeout=5)


def test_http_approval_auth_and_concurrency(broker,tmp_path):
    cfg,store=broker; folder=tmp_path/'http-files'
    async def start(app):
        runner=web.AppRunner(app,handler_cancellation=True); await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp'
    async def run():
        upstream,url=await start(demo_app(folder)); proxy,gateway=await start(gateway_app(cfg,url,policy='strict'))
        headers={'X-Guardian-Key':cfg['web_key'],'Accept':'application/json, text/event-stream'}
        try:
            async with ClientSession() as session:
                async def post(msg):
                    async with session.post(gateway,json=msg,headers=headers) as r: return await r.json()
                async with session.post(gateway,json=req(1)) as r: assert r.status==401
                async with session.post(gateway,json=req(1),headers={**headers,'Origin':'https://evil.example'}) as r: assert r.status==403
                init=asyncio.create_task(post(req(1,'initialize',protocolVersion='2025-11-25')))
                item=(await asyncio.to_thread(wait_pending,store))[0]; assert not init.done(); store.decide(item['id'],'allow')
                assert (await init)['result']['protocolVersion']=='2025-11-25'
                a=asyncio.create_task(post(req(2))); b=asyncio.create_task(post(req(3)))
                items=await asyncio.to_thread(wait_pending,store,2)
                assert not (folder/'test2.txt').exists() and not (folder/'test3.txt').exists()
                for i in items: store.decide(i['id'],'allow' if i['request_id']==2 else 'block')
                assert 'result' in await a and (await b)['error']['code']==-32001
                assert (folder/'test2.txt').exists() and not (folder/'test3.txt').exists()
                c=asyncio.create_task(post(req(4))); await asyncio.to_thread(wait_pending,store)
                async with session.post(gateway,json={'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':4}},headers=headers) as r: assert r.status==202
                assert (await c)['error']['code']==-32001 and not (folder/'test4.txt').exists()
                d=asyncio.create_task(post(req(5))); await asyncio.to_thread(wait_pending,store); d.cancel()
                with pytest.raises(asyncio.CancelledError): await d
                for _ in range(30):
                    if not store.pending(): break
                    await asyncio.sleep(.05)
                assert not store.pending() and not (folder/'test5.txt').exists()
                assert (await post(req(6)))['error']['code']==-32001
                assert not (folder/'test6.txt').exists()
        finally: await proxy.cleanup(); await upstream.cleanup()
    asyncio.run(run())
