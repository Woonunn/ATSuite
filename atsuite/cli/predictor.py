from __future__ import annotations

import argparse
import json
from pathlib import Path

from atsuite.prediction.dataset import Dataset
from atsuite.prediction.evaluate import evaluate
from atsuite.prediction.train import train_model


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train or evaluate the ATSuite predictor")
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train"); train.add_argument("--dataset", required=True); train.add_argument("--output", required=True)
    holdout = sub.add_parser("evaluate"); holdout.add_argument("--model", required=True); holdout.add_argument("--dataset", required=True); holdout.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "train":
        train_model(Dataset.load(args.dataset)).save(args.output)
    else:
        from atsuite.prediction.registry import ModelRegistry
        model = ModelRegistry(Path(args.model).parent).load(Path(args.model).stem)
        result = evaluate(__import__("atsuite.prediction", fromlist=["Predictor"]).Predictor(model), Dataset.load(args.dataset))
        text = json.dumps(result, indent=2) + "\n"
        if args.output: Path(args.output).write_text(text, encoding="utf-8")
        else: print(text, end="")
    return 0
