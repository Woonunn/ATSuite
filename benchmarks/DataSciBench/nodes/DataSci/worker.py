"""Python runs only in the hosted Tool's child, never in the Agent harness."""
import json
import os
import sys
from pathlib import Path

from implementation import DataSciWorkspace
from kernel import execute_code


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
            result = execute_code(payload["code"], namespace, payload["output_path"], namespace["__file__"])
            response.write(json.dumps(result) + "\n")


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
    execute_code(payload["code"], namespace, None, namespace["__file__"])
    workspace.sync_from_namespace(namespace)
    Path(sys.argv[2]).write_text(json.dumps({
        "variables": workspace.variables, "imported_modules": workspace.imported_modules,
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
