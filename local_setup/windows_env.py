import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from minisweagent.environments.local import LocalEnvironment


def find_git_bash() -> Path:
    if configured := os.getenv("MINI_GIT_BASH"):
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise FileNotFoundError("MINI_GIT_BASH does not point to bash.exe")
    if git := shutil.which("git"):
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Git Bash was not found. Set MINI_GIT_BASH to its bash.exe path.")


class WindowsLocalEnvironment(LocalEnvironment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bash = find_git_bash()

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        process = subprocess.Popen(
            [str(self.bash), "--noprofile", "--norc", "-c", action.get("command", "")],
            cwd=cwd or self.config.cwd or os.getcwd(),
            env=os.environ | self.config.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, _ = process.communicate(timeout=timeout or self.config.timeout)
        except subprocess.TimeoutExpired:
            # Only terminate the process tree created for this action.
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            stdout, _ = process.communicate()
            return {"output": stdout, "returncode": -1, "exception_info": "Command timed out."}
        output = {"output": stdout, "returncode": process.returncode, "exception_info": ""}
        self._check_finished(output)
        return output
