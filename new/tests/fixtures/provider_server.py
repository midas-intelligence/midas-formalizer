"""Local deterministic app-server peer used only by provider transport tests."""
import json
import os
from pathlib import Path
import sys
import subprocess
import time

mode = os.environ.get('FIXTURE_MODE', 'success')
log = Path(os.environ['FIXTURE_LOG'])


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
        result = {'config':{'model_providers':{'midas-openai':{'request_max_retries':
            1 if mode == 'retry' else 0, 'stream_max_retries':0,'requires_openai_auth':True,'wire_api':'responses'}}}}
    elif method == 'account/read':
        result = {'requiresOpenaiAuth':True,'account':{'type':'test'}}
    elif method == 'thread/start':
        assert params['ephemeral'] is True
        assert params['sandbox'] == 'read-only'
        assert params['approvalPolicy'] == 'never'
        result = {'thread':{'id':'thread-1'},'modelProvider':'midas-openai'}
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
