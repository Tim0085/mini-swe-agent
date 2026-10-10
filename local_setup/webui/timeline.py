"""Bounded read-only message pages; raw events remain on disk."""
import json
from collections import deque
from pathlib import Path

from .common import redact


def message_preview(message: dict) -> dict:
    message=redact(message)
    remaining=4000
    truncated=False
    def clip(value):
        nonlocal remaining,truncated
        if isinstance(value,str):
            if len(value)>remaining:
                truncated=True
            result=value[:remaining]
            remaining-=len(result)
            return result
        if isinstance(value,list):
            if len(value)>50:
                truncated=True
            return [clip(x) for x in value[:50]]
        if isinstance(value,dict):
            return {k:clip(v) for k,v in value.items() if k in {'role','type','content','output','text','extra','actions','command','tool_calls','function','name','arguments'}}
        return value
    result=clip(message)
    for key in ['role','type']:
        if key in message:
            result[key]=message[key]
    result['display_truncated']=truncated
    return result


def event_page(path: Path, before: int | None = None, limit: int = 50) -> dict:
    limit=max(1,min(limit,50))
    messages=deque(maxlen=limit)
    logs=deque()
    log_chars=0
    count=0
    last=0
    if path.is_file():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if not line.endswith('\n'):
                    break
                try:
                    event=json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event,dict) or not isinstance(event.get('id'),int) or 'kind' not in event:
                    continue
                if event['kind']=='message' and not isinstance(event.get('data'),dict):
                    continue
                last=max(last,event['id'])
                if event['kind']=='message' and event['data'].get('role')!='system' and (before is None or event['id']<before):
                    messages.append({'id':event['id'],'message':message_preview(event['data'])})
                    count+=1
                if before is None and event['kind'] in {'log','error'}:
                    value=str(redact(event['data']))[-200000:]
                    logs.append(value)
                    log_chars+=len(value)
                    while log_chars>200000 and len(logs)>1:
                        log_chars-=len(logs.popleft())
    return {'items':list(messages),'has_older':count>len(messages),'before':messages[0]['id'] if messages else None,
            'last_event_id':last,'logs':'\n'.join(logs)[-200000:]}
