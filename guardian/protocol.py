"""JSON-RPC validation and authorization policy shared by both transports."""
import json

MAX_MESSAGE = 1024 * 1024
SAFE_METHODS = {'initialize', 'ping', 'tools/list', 'resources/list', 'resources/templates/list', 'prompts/list'}
SAFE_NOTIFICATIONS = {'notifications/initialized', 'notifications/cancelled', 'notifications/progress', 'notifications/roots/list_changed'}


def encode(obj):
    return json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')


def decode(raw):
    def reject_constant(value):
        raise ValueError('Nonstandard JSON number')
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError('Duplicate JSON key')
            result[k] = v
        return result
    if len(raw) > MAX_MESSAGE:
        raise ValueError('Message too large')
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique)


def valid_id(value):
    return type(value) in (str, int)


def request_key(value):
    return (type(value).__name__, value)


def classify(msg):
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
        raise ValueError('Expected a single JSON-RPC 2.0 message')
    if 'method' in msg:
        if not isinstance(msg['method'], str) or not msg['method'] or 'result' in msg or 'error' in msg:
            raise ValueError('Invalid method')
        if 'params' in msg and not isinstance(msg['params'], dict):
            raise ValueError('Invalid params')
        if 'id' not in msg:
            return 'notification'
        if not valid_id(msg['id']):
            raise ValueError('Invalid request ID')
        if msg['method'] == 'tools/call' and (not isinstance(msg.get('params', {}).get('name'), str) or not isinstance(msg.get('params', {}).get('arguments', {}), dict)):
            raise ValueError('Invalid tool call')
        return 'request'
    if 'id' in msg and valid_id(msg['id']) and (('result' in msg) != ('error' in msg)):
        return 'response'
    raise ValueError('Invalid JSON-RPC envelope')


def error(rid, code, message):
    return {'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':message}}


def denied(msg, reason='Request blocked by user or approval unavailable'):
    return error(msg['id'], -32001, reason)


def requires_approval(msg, policy):
    return classify(msg) == 'request' and (policy == 'strict' or msg['method'] not in SAFE_METHODS)


def describe(msg, source, server):
    return {'source':source, 'server':server, 'method':msg['method'],
            'request_id':msg['id'], 'tool':msg.get('params', {}).get('name', ''),
            'params':msg.get('params', {})}


def cancel_id(msg):
    value = msg.get('params', {}).get('requestId')
    return request_key(value) if valid_id(value) else None
