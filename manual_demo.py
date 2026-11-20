"""Send real, authenticated HTTP MCP requests to a locally running Guardian gateway."""
import argparse
import asyncio
import uuid
from aiohttp import ClientSession, ClientTimeout
from guardian.config import load, STATE


async def run(delete):
    cfg = load()
    headers = {'X-Guardian-Key':cfg['web_key'],'Accept':'application/json, text/event-stream'}
    tag = uuid.uuid4().hex[:8]
    async with ClientSession(headers=headers, timeout=ClientTimeout(total=150)) as s:
        async def send(method, params):
            payload={'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':method,'params':params}
            async with s.post('http://127.0.0.1:8000/mcp',json=payload) as r:
                print(method, params.get('name',''),r.status,await r.text())
        await send('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'manual-demo','version':'1'}})
        async with s.post('http://127.0.0.1:8000/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'}) as r: await r.read()
        await send('tools/list',{})
        names=[f'alpha-{tag}.txt',f'beta-{tag}.txt']
        print('Review both popups, allow one and block the other:',names)
        await asyncio.gather(*(send('tools/call',{'name':'create_file','arguments':{'name':n,'content':'Hello Guardian'}}) for n in names))
        if delete:
            for n in names:
                if (STATE/'demo-files'/n).exists(): await send('tools/call',{'name':'delete_file','arguments':{'name':n}})
        for n in names: print(n, 'exists:',(STATE/'demo-files'/n).exists())


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--delete',action='store_true'); args=p.parse_args()
    asyncio.run(run(args.delete))
