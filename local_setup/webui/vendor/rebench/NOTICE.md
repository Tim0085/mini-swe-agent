# SWE-rebench official harness subset

Source: https://github.com/SWE-rebench/SWE-bench-fork

Pinned commit: `e4907b7a90eafaa1f0a6428fd04fe31cdd8b4284` (upstream package version 4.0.3).

The accompanying MIT LICENSE applies to upstream files. `manifest.json` lists the SHA-256 of each upstream and distributed file and the downloaded archive. Local evaluation reuses the existing Python interpreter and dependencies; it does not replace the installed SWE-bench 5.0.2 package. Cloud/collection/CLI entrypoints are omitted.

Integration changes are limited to:

- Package initializers avoid unused collection and cloud imports.
- `eval.py` writes patch and shell script as UTF-8 with LF on Windows.
- `docker_build.py::build_container` requires an existing image, never builds or pulls.
- `docker_utils.py` stops only the owned Docker container, never a host PID reported by the Docker VM. The timeout reader is bounded during cleanup.

Official grading, log parsers and test-script generators are unchanged. The workbench runner assembles the official TestSpec for the existing image, using the same official evaluation-script generator and remote-image compatibility function. Build-only environment/repository scripts are omitted because they are never executed for a cached official image; this also avoids unnecessary network retrieval of requirements files. The official `run_instance` and `make_run_report` produce both per-instance and aggregate reports.

The runner must be invoked in its own process. It never imports this package into the web server or the model worker, and never submits gold patches to a model.
