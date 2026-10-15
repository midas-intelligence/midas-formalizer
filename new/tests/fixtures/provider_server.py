"""Local deterministic app-server peer used only by provider transport tests."""
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import tomllib

mode = os.environ.get('FIXTURE_MODE', 'success')
log = Path(os.environ['FIXTURE_LOG'])
providers = {}
for index, arg in enumerate(sys.argv[:-1]):
    if arg != '-c':
        continue
    setting = sys.argv[index + 1]
    if setting.startswith('model_provider='):
        provider = tomllib.loads(setting)['model_provider']
    elif setting.startswith('model_providers.'):
        for name, fields in tomllib.loads(setting)['model_providers'].items():
            providers.setdefault(name, {}).update(fields)


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method, params = message['method'], message.get('params', {})
    with log.open('a') as out:
        out.write(method + '\n')
    if method == 'initialized':
        continue
    result = {}
    if method == 'config/read':
        effective = providers[provider]
        if mode == 'retry':
            effective['request_max_retries'] += 1
        elif mode == 'stream_retry':
            effective['stream_max_retries'] += 1
        elif mode == 'missing_retry':
            del effective['request_max_retries']
        elif mode == 'invalid_retry':
            effective['stream_max_retries'] = bool(effective['stream_max_retries'])
        result = {'config': {'model_providers': providers}}
    elif method == 'account/read':
        result = {'requiresOpenaiAuth':True,'account':{'type':'test'}}
    elif method == 'thread/start':
        assert params['ephemeral'] is True
        assert params['sandbox'] == 'read-only'
        assert params['approvalPolicy'] == 'never'
        result = {'thread':{'id':'thread-1'},'modelProvider':provider}
    elif method == 'turn/start':
        if mode == 'stderr':
            sys.stderr.write('x' * (4 * 1048576 + 1))
            sys.stderr.flush()
            time.sleep(10)
        if mode == 'child':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
            log.with_suffix('.pid').write_text(str(child.pid))
            time.sleep(.08)
        if mode == 'hang':
            time.sleep(10)
        send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1',
              'tokenUsage':{'total':{'inputTokens':12,'outputTokens':4,'cachedInputTokens':3}}}})
        send({'id':message['id'],'result':{'turn':{'id':'turn-1'}}})
        if mode == 'tool':
            send({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn-1',
                  'item':{'type':'commandExecution','id':'shell-1'}}})
        if mode == 'web':
            for event in ('item/started','item/completed'):
                send({'method':event,'params':{'threadId':'thread-1','turnId':'turn-1','item':{
                    'type':'webSearch','id':'web-1','action':{'type':'search','query':'math'}}}})
        send({'method':'item/completed','params':{'threadId':'foreign','item':{'type':'agentMessage','text':'wrong'}}})
        send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1',
              'item':{'type':'agentMessage','text':'{"proof":"by trivial"}'}}})
        send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})
        continue
    elif method == 'thread/read':
        send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1',
              'tokenUsage':{'total':{'inputTokens':12,'outputTokens':5,'cachedInputTokens':3}}}})
    send({'id':message['id'],'result':result})
