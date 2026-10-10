"""Keep Linux evaluation artifacts in UTF-8 with LF when running on Windows."""

from importlib import metadata, util
from pathlib import Path


def main() -> None:
    if metadata.version("swebench") != "5.0.2":
        raise SystemExit("This fix is verified for swebench 5.0.2 only.")
    path = Path(util.find_spec("swebench").origin).parent / "harness/run_evaluation.py"
    original = path.read_bytes()
    source = original.decode("utf-8")
    replacements = {
        'patch_file.write_text(pred["model_patch"] or "")':
            'patch_file.write_text(pred["model_patch"] or "", encoding="utf-8", newline="\\n")',
        'eval_file.write_text(_inject_asset_restore(test_spec.eval_script, restore_cmds))':
            'eval_file.write_text(_inject_asset_restore(test_spec.eval_script, restore_cmds), encoding="utf-8", newline="\\n")',
    }
    for old, new in replacements.items():
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise SystemExit("Evaluator source differs from expected; no changes written.")
        source = source.replace(old, new, 1)
    if source == original.decode("utf-8"):
        print("LF/UTF-8 fix already applied.")
        return
    compile(source, str(path), "exec")
    backup = Path(__file__).resolve().parent.parent / ".local/swebench-windows-fix/run_evaluation.py.original"
    backup.parent.mkdir(exist_ok=True)
    if backup.exists():
        if backup.read_bytes() != original:
            raise SystemExit("An existing backup differs; no changes written.")
    else:
        backup.write_bytes(original)
    path.write_text(source, encoding="utf-8", newline="\n")
    print("Applied UTF-8/LF writing to patch.diff and eval.sh; original evaluator backed up.")


if __name__ == "__main__":
    main()
