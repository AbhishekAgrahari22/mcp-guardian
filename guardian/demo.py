"""A deliberately small, sandboxed MCP server for testing both transports.
No arbitrary paths, shell access, database access or network tools.
Implements MCP 2025-11-25 / 2025-06-18 / 2025-03-26 JSON responses.
"""
import re
import sys
import threading
from pathlib import Path
from aiohttp import web
from .protocol import MAX_MESSAGE, classify, decode, encode, error

VERSIONS = ['2025-11-25','2025-06-18','2025-03-26']
NAME = {'type':'string','pattern':r'^[A-Za-z0-9_-]{1,60}\.txt$','description':'Simple .txt filename inside the demo sandbox'}
TOOLS = [
    {'name':'create_file','description':'Create a new text file in the interview demo sandbox (no overwrite).',
     'inputSchema':{'type':'object','properties':{'name':NAME,'content':{'type':'string','maxLength':10000}},'required':['name','content'],'additionalProperties':False},'annotations':{'destructiveHint':False}},
    {'name':'read_file','description':'Read a text file in the demo sandbox.',
     'inputSchema':{'type':'object','properties':{'name':NAME},'required':['name'],'additionalProperties':False},'annotations':{'readOnlyHint':True}},
    {'name':'delete_file','description':'Delete a text file in the demo sandbox.',
     'inputSchema':{'type':'object','properties':{'name':NAME},'required':['name'],'additionalProperties':False},'annotations':{'destructiveHint':True}},
]


class Demo:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock = threading.Lock()

    def tool(self,name,args):
        if name not in {t['name'] for t in TOOLS}: raise ValueError('Unknown demo tool')
        expected = {'name','content'} if name == 'create_file' else {'name'}
        if set(args) != expected: raise ValueError('Unexpected or missing arguments')
        filename = args['name']
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}\.txt',filename):
            raise ValueError('Only simple .txt filenames allowed')
        path = self.directory / filename
        with self.lock:
            if path.is_symlink(): raise ValueError('Symlinks are not permitted')
            if name == 'create_file':
                content = args['content']
                if not isinstance(content,str) or len(content)>10000: raise ValueError('Content must be text up to 10000 characters')
                with path.open('x',encoding='utf-8') as f: f.write(content)
                return f'Created {filename}'
            if not path.is_file(): raise ValueError('File not found')
            if name == 'read_file':
                with path.open(encoding='utf-8') as f: return f.read(10001)
            path.unlink(); return f'Deleted {filename}'

    def dispatch(self,msg):
        kind = classify(msg)
        if kind != 'request': return None
        rid, method, params = msg['id'],msg['method'],msg.get('params',{})
        if method == 'initialize':
            version = params.get('protocolVersion')
            result = {'protocolVersion':version if version in VERSIONS else VERSIONS[0],
                      'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'guardian-demo','version':'1.0.0'}}
        elif method == 'ping': result = {}
        elif method == 'tools/list': result = {'tools':TOOLS}
        elif method == 'tools/call':
            try: result = {'content':[{'type':'text','text':self.tool(params['name'],params.get('arguments',{}))}]}
            except (ValueError,OSError,UnicodeError): result = {'content':[{'type':'text','text':'Demo operation failed (invalid input, existing/missing file, or I/O error).'}],'isError':True}
        else: return error(rid,-32601,'Method not supported by demo server')
        return {'jsonrpc':'2.0','id':rid,'result':result}


def run_stdio(directory):
    demo = Demo(directory)
    while True:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE+2)
        if not raw: return
        try: response = demo.dispatch(decode(raw))
        except (ValueError,TypeError,RecursionError): response = error(None,-32600,'Invalid request')
        if response is not None:
            sys.stdout.buffer.write(encode(response)+b'\n'); sys.stdout.buffer.flush()


def create_app(directory):
    demo = Demo(directory)
    app = web.Application(client_max_size=MAX_MESSAGE)
    async def endpoint(req):
        if req.method != 'POST': return web.Response(status=405,headers={'Allow':'POST'})
        try: response = demo.dispatch(decode(await req.read()))
        except (ValueError,TypeError,RecursionError): return web.json_response(error(None,-32600,'Invalid request'),status=400)
        return web.Response(status=202) if response is None else web.json_response(response)
    app.router.add_route('*','/mcp',endpoint)
    return app


def run_http(directory,port):
    web.run_app(create_app(directory),host='127.0.0.1',port=port,access_log=None)
