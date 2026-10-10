import getpass
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key

from credentials import load_key

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / ".local"


def prepare_environment() -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    settings = dotenv_values(CONFIG_DIR / ".env")
    load_dotenv(CONFIG_DIR / ".env", override=True)
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(CONFIG_DIR)
    os.environ["MSWEA_CONFIGURED"] = "true"
    os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "2")
    os.environ.setdefault("MSWEA_GLOBAL_CALL_LIMIT", "45")
    os.environ["OPENAI_API_BASE"] = os.getenv("MINI_API_BASE") or settings.get("OPENAI_API_BASE") or "https://api.gantim.pw/v1"
    os.environ["OPENAI_BASE_URL"] = os.environ["OPENAI_API_BASE"]
    # Never send an unrelated global OpenAI key to the configured relay.
    if key := os.getenv("MINI_API_KEY") or load_key() or settings.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = key
    else:
        os.environ.pop("OPENAI_API_KEY", None)
    os.environ["PATH"] = str(ROOT / ".venv" / "Scripts") + os.pathsep + os.environ["PATH"]


def main() -> None:
    prepare_environment()
    cli_args = sys.argv[1:]
    if "--help" not in cli_args:
        has_model_arg = any(arg in {"-m", "--model"} or arg.startswith("--model=") for arg in cli_args)
        if not has_model_arg and not os.getenv("MSWEA_MODEL_NAME"):
            if not sys.stdin.isatty():
                raise SystemExit("Model missing: set MSWEA_MODEL_NAME in .local/.env or pass -m openai/MODEL.")
            model = input("请输入中转服务的模型名（无需 openai/ 前缀）: ").strip()
            if not model:
                raise SystemExit("未填写模型名，已退出。")
            if not model.startswith("openai/"):
                model = "openai/" + model
            os.environ["MSWEA_MODEL_NAME"] = model
            set_key(CONFIG_DIR / ".env", "MSWEA_MODEL_NAME", model)
        if not os.getenv("OPENAI_API_KEY"):
            if not sys.stdin.isatty():
                raise SystemExit("API key missing: run in an interactive terminal for masked input.")
            key = getpass.getpass("请输入此中转服务的 API Key（隐藏输入，仅本次运行使用）: ").strip()
            if not key:
                raise SystemExit("未填写 API Key，已退出。")
            os.environ["OPENAI_API_KEY"] = key

    from minisweagent.run.mini import app

    app(args=["-c", str(ROOT / "src/minisweagent/config/mini.yaml"),
              "-c", str(ROOT / "local_setup/windows.yaml"), *cli_args])


if __name__ == "__main__":
    main()
