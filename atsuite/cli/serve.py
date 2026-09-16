from __future__ import annotations

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the ATSuite recommender control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=":memory:")
    parser.add_argument("--artifact-root", default="results/recommendations")
    args = parser.parse_args(argv)
    import uvicorn
    from atsuite.server.app import create_app
    uvicorn.run(create_app(store_path=args.store, artifact_root=args.artifact_root), host=args.host, port=args.port,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()
