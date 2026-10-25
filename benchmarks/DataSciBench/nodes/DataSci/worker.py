"""Python runs only in the hosted Tool's child, never in the Agent harness."""
import json
import os
import sys
import traceback
from pathlib import Path

from implementation import DataSciWorkspace


def resident_loop(response_fd):
    # The dedicated RPC fd cannot be mixed with user prints or native library output.
    namespace = None
    with os.fdopen(response_fd, "w", encoding="utf-8", buffering=1) as response:
        for line in sys.stdin:
            payload = json.loads(line)
            if namespace is None:
                workspace = DataSciWorkspace()
                workspace.variables = payload["variables"]
                workspace.imported_modules = payload["imported_modules"]
                namespace = workspace.namespace()
                namespace["__file__"] = str(Path.cwd() / "__session__.py")
            status, error = "ok", ""
            sys.stdout.flush()
            sys.stderr.flush()
            saved = os.dup(1), os.dup(2)
            with open(payload["output_path"], "w+b") as capture:
                try:
                    os.dup2(capture.fileno(), 1)
                    os.dup2(capture.fileno(), 2)
                    exec(compile(payload["code"].strip(), namespace["__file__"], "exec"), namespace)
                except BaseException:
                    status, error = "error", traceback.format_exc()
                    print(error, file=sys.stderr, end="")
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    for source, target in zip(saved, (1, 2)):
                        os.dup2(source, target)
                        os.close(source)
                capture.seek(0)
                output = capture.read().decode("utf-8", errors="replace")
            response.write(json.dumps({"status": status, "error": error, "output": output}) + "\n")


def main():
    if sys.argv[1] == "--resident":
        resident_loop(int(sys.argv[2]))
        return
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    workspace = DataSciWorkspace()
    workspace.variables = payload["variables"]
    workspace.imported_modules = payload["imported_modules"]
    namespace = workspace.namespace()
    namespace["__file__"] = str(Path.cwd() / "__session__.py")
    exec(compile(payload["code"].strip(), namespace["__file__"], "exec"), namespace)
    workspace.sync_from_namespace(namespace)
    Path(sys.argv[2]).write_text(json.dumps({
        "variables": workspace.variables, "imported_modules": workspace.imported_modules,
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
