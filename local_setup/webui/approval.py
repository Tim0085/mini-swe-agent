"""One stdin reader per worker, with optional deadlines for command approvals."""
import json
import queue
import threading
import time
from typing import TextIO


class ApprovalInput:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.lines = queue.Queue()
        self.thread = None
        self.closed = False

    def _read(self):
        try:
            for line in self.stream:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.lines.put(None)

    def wait(self, ticket: str, deadline: float | None = None) -> dict:
        if self.thread is None:
            self.thread = threading.Thread(target=self._read, name='mini-approval-input', daemon=True)
            self.thread.start()
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('批准等待已达到任务时限。')
            if self.closed:
                raise RuntimeError('工作台连接已关闭。')
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('批准等待已达到任务时限。') from None
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('批准等待已达到任务时限。')
            if line is None:
                self.closed = True
                raise RuntimeError('工作台连接已关闭。')
            answer = json.loads(line)
            if isinstance(answer, dict) and answer.get('ticket') == ticket:
                return answer
