from __future__ import annotations

import argparse


def main(argv=None):
    from atsuite.config import load_local_env
    load_local_env()
    parser = argparse.ArgumentParser(description="Run the ATSuite recommender control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=":memory:")
    parser.add_argument("--artifact-root", default="results/recommendations")
    parser.add_argument("--mode", choices=("local_development", "production"), default="local_development")
    args = parser.parse_args(argv)
    from atsuite.server.auth import loopback
    if args.mode == "local_development" and not loopback(args.host):
        parser.error("local_development must bind a loopback address")
    import uvicorn
    from atsuite.server.app import create_app
    uvicorn.run(create_app(store_path=args.store, artifact_root=args.artifact_root, mode=args.mode), host=args.host, port=args.port,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()
