import json
import tempfile
from pathlib import Path

from run import CONFIG_DIR, ROOT, prepare_environment

prepare_environment()

from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.utils.serialize import recursive_merge


def main() -> None:
    config = recursive_merge(
        get_config_from_spec(ROOT / "src/minisweagent/config/mini.yaml"),
        get_config_from_spec(ROOT / "local_setup/windows.yaml"),
    )
    assert config["agent"]["mode"] in {"confirm", "human", "yolo"}
    assert int(config["agent"]["step_limit"]) >= 0
    get_model("openai/offline-config-check", config=config["model"])
    with tempfile.TemporaryDirectory(prefix="smoke-中文-", dir=CONFIG_DIR) as folder:
        # Confirm this owned temporary directory is inside the installation before cleanup.
        assert Path(folder).resolve().is_relative_to(CONFIG_DIR.resolve())
        env = get_environment(config["environment"] | {"cwd": folder})
        result = env.execute({"command": "cat <<'EOF' > '中文 file.txt'\n你好 Bash\nEOF\ncat '中文 file.txt' | sed 's/Bash/OK/'"})
        assert result["returncode"] == 0 and "你好 OK" in result["output"], result
        result = env.execute({"command": "python -c 'import sys; print(sys.prefix)'"})
        assert result["returncode"] == 0 and str(ROOT / ".venv") in result["output"], result
        result = env.execute({"command": "printf failure; exit 7"})
        assert result["returncode"] == 7 and result["output"] == "failure", result
        result = env.execute({"command": "sleep 10"}, timeout=1)
        assert result["returncode"] == -1 and "timed out" in result["exception_info"], result

        trajectory = Path(folder) / "offline.traj.json"
        model = DeterministicModel(outputs=[
            make_output("offline shell check", [{"command": "printf agent-loop-ok"}], cost=0),
            make_output("finish", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}], cost=0),
        ], cost_per_call=0)
        agent = DefaultAgent(model, env, **(config["agent"] | {"output_path": trajectory, "step_limit": 4, "cost_limit": 0}))
        result = agent.run("Offline installation verification; no external model is called.")
        assert result["exit_status"] == "Submitted", result
        saved = json.loads(trajectory.read_text())
        assert saved["info"]["model_stats"]["api_calls"] == 2
        assert "agent-loop-ok" in trajectory.read_text()
    print("PASS: configuration, Git Bash, Unicode paths, pipes, heredoc, venv Python, exit codes, timeout, agent loop, trajectory")
    print("No external model requests were made.")


if __name__ == "__main__":
    main()
