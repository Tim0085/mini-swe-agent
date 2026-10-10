"""Observe only this evaluation's log files, without changing the harness."""
import codecs
from pathlib import Path

from .common import now

EVALUATION_TIMEOUT=1800
READ_SIZE=65536
LINE_LIMIT=4096


def stage_for(line: str) -> str | None:
    latest=(-1,None)
    for marker,stage in [('Creating container','创建评测容器'),('now applying','应用补丁'),('copying to container','准备并执行测试'),
                         ('Eval script','准备并执行测试'),('Test runtime:','测试执行结束'),('Grading answer','汇总测试结果'),
                         ('Result for','评测结果已生成')]:
        if line.rfind(marker)>latest[0]:
            latest=(line.rfind(marker),stage)
    return latest[1]


class Progress:
    def __init__(self,folder: Path,emit,timeout=EVALUATION_TIMEOUT):
        self.folder=folder
        self.emit=emit
        self.positions={}
        self.pending={}
        self.state={'stage':'准备评测容器','last_output_at':None,'test_timeout_seconds':int(timeout),
                    'note':f'测试输出可能在测试结束后才写入；阶段日志会持续更新。单题测试超时 {int(timeout)} 秒，不包含前后准备和清理。'}

    def feed(self, pending, text, final):
        lines=text.split('\n')
        for index,line in enumerate(lines):
            combined=pending['tail']+line
            stage=stage_for(combined)
            if stage and stage!=self.state['stage']:
                self.state['stage']=stage
                self.emit('phase',stage)
            pending['tail']=combined[-32:]
            display=pending['text']+line
            pending['text']=display[:LINE_LIMIT]
            pending['truncated']|=len(display)>LINE_LIMIT
            if index<len(lines)-1 or final and (pending['text'] or pending['truncated']):
                self.emit('log','[评测阶段日志] '+pending['text'].rstrip('\r')+('… [单行过长，完整内容保留在日志文件]' if pending['truncated'] else ''))
                pending.update(text='',tail='',truncated=False)

    def scan(self,final=False):
        changed=False
        for path in self.folder.glob('logs/run_evaluation/**/run_instance.log'):
            if path.is_symlink() or not path.resolve().is_relative_to(self.folder.resolve()):
                continue
            position=self.positions.get(path,0)
            try:
                handle=path.open('rb')
            except OSError:
                continue
            with handle:
                try:
                    size=path.stat().st_size
                    if size<position:
                        position=0
                        self.pending.pop(path,None)
                    pending=self.pending.setdefault(path,{'decoder':codecs.getincrementaldecoder('utf-8')(errors='replace'),
                                                         'text':'','tail':'','truncated':False})
                    handle.seek(position)
                except OSError:
                    continue
                while position<size:
                    try:
                        content=handle.read(min(READ_SIZE,size-position))
                    except OSError:
                        break
                    if not content:break
                    position+=len(content)
                    self.positions[path]=position
                    self.feed(pending,pending['decoder'].decode(content),False)
                    self.state['last_output_at']=now()
                    changed=True
                    if not final:break
                if final:
                    self.feed(pending,pending['decoder'].decode(b'',final=True),True)
                    pending['decoder']=codecs.getincrementaldecoder('utf-8')(errors='replace')
        if changed:
            self.emit('evaluation_progress',self.state.copy())

    def watch(self,stop):
        self.emit('evaluation_progress',self.state.copy())
        while not stop.wait(1):
            self.scan()
        self.scan(final=True)
