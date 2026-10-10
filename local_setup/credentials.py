import getpass
import os
import subprocess
from pathlib import Path

KEY_FILE = Path(__file__).resolve().parent.parent / ".local" / "api-key.dpapi"


def _powershell(script: str, value: str) -> str:
    script = '[Console]::InputEncoding=[Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); ' + script
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=value, capture_output=True, text=True, encoding='utf-8', check=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=os.environ | {"PSModulePath": str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/Modules")},
    ).stdout.strip()


def load_key() -> str:
    if not KEY_FILE.is_file():
        return ""
    return _powershell(
        '$ErrorActionPreference="Stop"; '
        '$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); '
        '[Console]::Write([Net.NetworkCredential]::new("",$s).Password)',
        KEY_FILE.read_text(encoding="ascii"),
    )


def main() -> None:
    key = getpass.getpass("API Key (hidden; encrypted for this Windows user): ").strip()
    if not key:
        raise SystemExit("No key provided.")
    encrypted = _powershell(
        '$ErrorActionPreference="Stop"; '
        '$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()) -AsPlainText -Force; '
        '[Console]::Write((ConvertFrom-SecureString $s))',
        key,
    )
    KEY_FILE.parent.mkdir(exist_ok=True)
    KEY_FILE.write_text(encrypted, encoding="ascii")
    assert load_key() == key, "Encrypted credential round-trip failed."
    print("Encrypted credential saved and verified. No plaintext key was written.")


if __name__ == "__main__":
    main()
