"""MCP Guardian CLI. Run python main.py --help for commands."""
import argparse
import json
import logging
import sys
from pathlib import Path
from guardian import config


def main():
    parser = argparse.ArgumentParser(description='Human-in-the-loop MCP firewall')
    sub = parser.add_subparsers(dest='mode',required=True)
    sub.add_parser('setup',help='Generate local and web authentication keys')
    agent = sub.add_parser('agent',help='Run the desktop approval agent')
    agent.add_argument('--ui', choices=['auto','native','tk'], default='auto')
    sub.add_parser('approval-test', help='Show a test approval without executing a tool')
    sub.add_parser('desktop-install', help='Safely merge the demo into Claude Desktop config')
    sub.add_parser('desktop-remove', help='Remove only guardian-demo from Claude Desktop config')
    sub.add_parser('desktop-config',help='Print Claude Desktop demo configuration')
    sub.add_parser('web-key',help='Print the secret custom connector header value')
    def policy(p): p.add_argument('--policy',choices=['strict','actions'],default='strict',help='strict: every client request; actions: skip discovery/handshake')
    stdio = sub.add_parser('stdio',help='Proxy any local newline-delimited MCP server')
    stdio.add_argument('--name',default='local-mcp'); policy(stdio)
    stdio.add_argument('command',nargs=argparse.REMAINDER)
    http = sub.add_parser('http',help='Proxy a fixed remote Streamable HTTP server')
    http.add_argument('--upstream',default='http://127.0.0.1:9001/mcp')
    http.add_argument('--name',default='demo-http'); http.add_argument('--port',type=int,default=8000)
    http.add_argument('--origin',help='Optional exact allowed Origin header'); policy(http)
    for name in ('demo-stdio','demo-http'):
        p = sub.add_parser(name); p.add_argument('--directory',type=Path,default=config.STATE/'demo-files')
        if name=='demo-http': p.add_argument('--port',type=int,default=9001)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,stream=sys.stderr,format='%(levelname)s: %(message)s')
    if args.mode=='setup':
        config.initialize(); print(f'Configuration ready: {config.STATE / "config.json"}'); return
    if args.mode in ('demo-stdio','demo-http'):
        from guardian import demo
        return demo.run_stdio(args.directory) if args.mode=='demo-stdio' else demo.run_http(args.directory,args.port)
    if args.mode in ('desktop-install', 'desktop-remove'):
        from guardian.desktop_config import update
        return update(remove=args.mode=='desktop-remove')
    cfg = config.load()
    if args.mode=='agent':
        if args.ui=='native' or (args.ui=='auto' and sys.platform=='darwin'):
            if sys.platform!='darwin': raise ValueError('Native dialog is available on macOS only')
            from guardian.mac_ui import run
        else:
            from guardian.ui import run
        return run(cfg)
    if args.mode=='approval-test':
        import threading
        from guardian.approval import ApprovalClient
        print('Sending a test request. Choose Allow once or Block. No files will be changed.', flush=True)
        result = ApprovalClient(cfg).wait({'source':'Manual approval test','server':'demo-files','method':'tools/call','request_id':999,'tool':'create_file','params':{'name':'test.txt','content':'Popup test only'}}, threading.Event())
        print('Approved:', result)
        return

    if args.mode=='web-key': print(cfg['web_key']); return
    if args.mode=='desktop-config':
        from guardian.desktop_config import generated
        print(json.dumps(generated(), indent=2)); return
    if args.mode=='stdio':
        from guardian.stdio import run
        command = args.command[1:] if args.command[:1]==['--'] else args.command
        return run(cfg,command,args.name,args.policy)
    if args.mode=='http':
        from guardian.http_gateway import run
        return run(cfg,args.upstream,args.name,args.policy,args.port,args.origin)


if __name__=='__main__':
    try: raise SystemExit(main())
    except (ValueError,FileNotFoundError,OSError) as exc:
        logging.error('%s',exc); raise SystemExit(1)
