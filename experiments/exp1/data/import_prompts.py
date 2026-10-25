"""Import immutable DataSciBench Prompts and evidence-backed task metadata."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

REPOSITORY = "https://github.com/THUDM/DataSciBench"

VERIFIED_SOURCE_COMMIT = '84ef3d4d94d7362a5149cf14a73dc168fc4f2f33'
# These flagged inputs were prepared and passed three offline Docker runs.
VERIFIED_INPUT_PROMPTS = {
    "csv_excel_34": "5973c8a006c53dbf4cde1d3fc34cad61870c677a3dbeb17a550e6e5c84828dda",
    "human_12": "6957e18188b519ea68c962cd50162edc5455edab56d0b50f94f79456a35917e2",
    "human_142": "5ea3a897eb5a1e5f34c79ebd42e6513ac3a67c9f52fb6e0b2b1c70a01ec54746",
    "human_7": "9b84ae4068ab58d0727aebfda78b366b7521afc2b91b337e1393ceb0d7932251",
    "csv_excel_29": "1cb3c178d2fbadd65ba689edc5a33a22d57aa6b54bf2c1572164bac617332a74"
}

EXTENSIONS = r"csv|tsv|xlsx?|jsonl?|parquet|feather|txt|pdf|png|jpe?g|svg|pkl|pickle|npy|npz|h5|hdf5|pt|pth|zip|data|names|test|py|html|md|log"
QUOTED_FILE = re.compile(r"[`\"']+([^`\"'\r\n]+?\.(?:" + EXTENSIONS + r"))[.,;:]?[`\"']+", re.I)
BARE_FILE = re.compile(r"(?<![\w/{}.+-])(?:\.{1,2}/)?(?:[\w.+-]+/)*[\w.+-]+\.(?:" + EXTENSIONS + r")(?![\w/]|\.\w)", re.I)
LABEL = re.compile(r"\b(input(?:\s+file(?:names?)?)?|output(?:\s+file(?:names?)?)?|model\s+checkpoint)\s*:", re.I)
ROLE = re.compile(r"\b(?P<output>output|checkpoint|sav(?:e|ed|ing)|writ(?:e|ing)|to_csv|to_excel|savefig|dump|produce|generate|export(?:ed|ing)?)\b|\b(?P<input>input|read(?:_csv|_excel|_json|_parquet)?|load|use|using|provided|stored|given)\b", re.I)
URL = re.compile(r"https?://[^\s`\"'<>]+")
LIBRARY = re.compile(r"\b(?:pandas|numpy|scipy|scikit[- ]learn|sklearn|matplotlib|seaborn|pytorch|torch|tensorflow|keras|xgboost|lightgbm|nltk|gensim|reportlab|imbalanced-learn|opencv|pillow|plotly|statsmodels|networkx)\b", re.I)
STEP = re.compile(r"^\s*(\d+)[.)]\s+(.+)")
RANGE = re.compile(r"^(.*?)(\d+)(\.[^.]+)$")


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _reject_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _file_mentions(line, *, quoted_only=False):
    matches = [(match.start(1), match.end(1), match[1]) for match in QUOTED_FILE.finditer(line)]
    for match in ([] if quoted_only else BARE_FILE.finditer(line)):
        if not any(start <= match.start() < end for start, end, _ in matches):
            matches.append((match.start(), match.end(), match[0]))
    urls = list(URL.finditer(line))
    return sorted((start, end, name) for start, end, name in matches
                  if not any(url.start() <= start < url.end() for url in urls))


def _file_key(path):
    return re.sub(r"^(?:\./|\.\./)+", "", path)


def extract_metadata(task_id: str, prompt: str, tracked_paths: set[str]) -> dict:
    """Extract literal paths and imports without executing prompt code."""
    category = "bcb" if re.fullmatch(r"bcb\d+", task_id) else task_id.rsplit("_", 1)[0]
    steps, references, imports = [], {}, set()
    current_step, continuation, in_code = None, None, False
    warnings = []
    # shortcut: free-text extraction is conservative; unresolved paths require review before task selection.
    for number, original in enumerate(prompt.splitlines(), 1):
        line = original.replace("**", "")
        if line.strip().startswith("```"):
            in_code = not in_code
            continuation = None
            continue
        heading = STEP.match(line) if not in_code else None
        if heading:
            current_step = heading[1]
            if any(step["step_id"] == current_step for step in steps):
                warnings.append(f"Duplicate numbered section {current_step} at line {number}")
                current_step = f"{current_step}@{number}"
            steps.append({"step_id": current_step, "title": heading[2].strip(), "input_files": [], "output_files": [], "depends_on": []})
            continuation = None
        if not line.strip():
            continuation = None
            continue
        if in_code and re.match(r"\s*(?:import\s|from\s)", line):
            try:
                for node in ast.walk(ast.parse(line.strip())):
                    if isinstance(node, ast.Import):
                        imports.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module)
            except SyntaxError:
                warnings.append(f"Unparsed import at line {number}")
        mentions = _file_mentions(line, quoted_only=in_code)
        previous_end = 0
        previous_role = continuation if re.match(r"^\s*[-*`\"']", line) else None
        for index, (start, end, name) in enumerate(mentions):
            prefix = line[previous_end:start]
            signals = list(ROLE.finditer(prefix))
            role = signals[-1].lastgroup if signals else previous_role
            if role is None and not in_code and re.match(r"^\s*(?:I (?:currently )?have|The file)\b", prefix, re.I):
                role = "input"
            if re.search(r"open\s*\(\s*[`\"']*$", prefix):
                mode = re.match(r"[`\"']\s*,\s*[`\"']([rawx+bt]+)[`\"']", line[end:])
                role = "output" if mode and any(flag in mode[1] for flag in "wax+") else "input"
            names = [name]
            # Expand explicitly written bounded numeric ranges (e.g. data_1.xlsx to data_5.xlsx).
            if index:
                earlier = mentions[index-1]
                left, right = RANGE.match(earlier[2]), RANGE.match(name)
                if left and right and re.fullmatch(r"[`\"'\s]*(?:to|through|至|–|-)[`\"'\s]*", line[earlier[1]:start], re.I):
                    if left[1] == right[1] and left[3] == right[3] and 0 <= int(right[2])-int(left[2]) <= 1000:
                        names = [f"{left[1]}{str(value).zfill(len(left[2])) if left[2].startswith('0') else value}{left[3]}"
                                 for value in range(int(left[2])+1, int(right[2])+1)]
                    else:
                        warnings.append(f"Unresolved file range at line {number}")
            for filename in names:
                ref = references.setdefault(filename, {"path": filename, "mentions": []})
                ref["mentions"].append({"line": number, "role": role, "step_id": current_step, "text": original})
                if current_step and role:
                    field = "input_files" if role == "input" else "output_files"
                    if filename not in steps[-1][field]:
                        steps[-1][field].append(filename)
            previous_end, previous_role = end, role
        labels = list(LABEL.finditer(line))
        if labels:
            continuation = "input" if labels[-1][1].lower().startswith("input") else "output"
        elif not re.match(r"^\s*[-*`\"']", line):
            continuation = None

    inputs, outputs, intermediates, unresolved = [], [], [], []
    by_key = {}
    for name, ref in references.items():
        if any(item["role"] is None for item in ref["mentions"]):
            unresolved.append(name)
        for mention in ref["mentions"]:
            by_key.setdefault(_file_key(name), []).append({**mention, "path": name})
        upstream_path = f"data/{task_id}/{_file_key(name)}"
        ref["upstream_path"] = upstream_path if upstream_path in tracked_paths else None
    for mentions in by_key.values():
        mentions.sort(key=lambda item: item["line"])
        read = next((item for item in mentions if item["role"] == "input"), None)
        written = next((item for item in mentions if item["role"] == "output"), None)
        if written:
            outputs.append(written["path"])
        if read and mentions[0]["role"] == "input":
            inputs.append(read["path"])
        elif read and written and mentions.index(written) < mentions.index(read):
            intermediates.append(read["path"])
    producers = {}
    for step in steps:
        for name in step["input_files"]:
            producer = producers.get(_file_key(name))
            if producer and producer not in step["depends_on"]:
                step["depends_on"].append(producer)
        for name in step["output_files"]:
            producers[_file_key(name)] = step["step_id"]
    urls = sorted({match[0].rstrip(".,;:)") for match in URL.finditer(prompt)})
    missing = [name for name in inputs if references[name]["upstream_path"] is None]
    return {
        "task_type": category, "task_type_source": "task_id_prefix",
        "input_files": sorted(inputs), "output_files": sorted(outputs),
        "intermediate_files": sorted(intermediates), "subtasks": steps,
        "dependencies": {"python_imports": sorted(imports),
                         "library_mentions": sorted({match[0] for match in LIBRARY.finditer(prompt)}), "data_urls": urls,
                         "missing_from_upstream": sorted(missing)},
        "file_references": list(references.values()),
        "metadata_extraction": {"method": "literal_paths_labels_context_and_static_imports_v1",
                                "unresolved_file_references": sorted(unresolved), "warnings": warnings,
                                "requires_review": bool(unresolved or warnings), "complete": False},
    }


def _import_source_prompts(repo: Path, revision: str = "HEAD", task_ids: list[str] | None = None) -> dict:
    repo = Path(repo).resolve()
    origin = _git(repo, "remote", "get-url", "origin").decode().strip().removesuffix(".git").rstrip("/").lower()
    if origin not in {REPOSITORY.lower(), "git@github.com:thudm/datascibench"}:
        raise ValueError("Source origin must be THUDM/DataSciBench")
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
    tree = _git(repo, "ls-tree", "-r", "-z", commit, "--", "data").split(b"\0")
    entries = {}
    for entry in tree:
        if entry:
            metadata, path = entry.split(b"\t", 1)
            entries[path.decode("utf-8")] = metadata.decode().split()
    paths = sorted(path for path in entries if len(PurePosixPath(path).parts) == 3 and path.endswith("/prompt.json"))
    available = {PurePosixPath(path).parent.name for path in paths}
    if task_ids is not None:
        unknown = set(task_ids)-available
        if unknown:
            raise ValueError(f"Unknown task IDs at {commit}: {sorted(unknown)}")
        paths = [path for path in paths if PurePosixPath(path).parent.name in task_ids]
    if not paths:
        raise ValueError("No data/<task_id>/prompt.json files at selected revision")
    tasks = []
    for path in paths:
        if entries[path][0] not in {"100644", "100755"}:
            raise ValueError(f"Prompt must be a regular file: {path}")
        raw = _git(repo, "cat-file", "blob", entries[path][2])
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
            if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str) or not payload["prompt"].strip():
                raise ValueError("prompt must be a nonempty string")
            if not isinstance(payload.get("data_source_type"), str) or not payload["data_source_type"].strip():
                raise ValueError("data_source_type must be a nonempty string")
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"Invalid Prompt JSON at {path}: {exc}") from exc
        task_id = PurePosixPath(path).parent.name
        task = {"task_id": task_id, "prompt": payload["prompt"], "data_source_type": payload["data_source_type"],
                "prompt_sha256": hashlib.sha256(payload["prompt"].encode("utf-8")).hexdigest(),
                "prompt_file_sha256": hashlib.sha256(raw).hexdigest(),
                "source_path": path, "source_blob": entries[path][2]}
        task.update(extract_metadata(task_id, payload["prompt"], set(entries)))
        if not task["input_files"] and not payload["data_source_type"].startswith("1"):
            task["metadata_extraction"]["requires_review"] = True
            task["metadata_extraction"]["warnings"].append("Data dependency declared, but no concrete input filename extracted")
        tasks.append(task)
    return {"schema_version": 1, "source": {"repository": REPOSITORY, "commit_sha": commit, "content_source": "git_commit_blobs"},
            "hash_definitions": {"prompt_sha256": "SHA256 of original decoded prompt encoded as UTF-8, without normalization",
                                 "prompt_file_sha256": "SHA256 of exact prompt.json bytes at source commit"},
            "task_count": len(tasks), "tasks": tasks}


def import_prompts(repo: Path, revision: str = "HEAD", task_ids: list[str] | None = None) -> dict:
    return select_catalog(_import_source_prompts(repo, revision, task_ids))


def select_catalog(source: dict) -> dict:
    kept, excluded, restored = [], [], []
    for task in source["tasks"]:
        reasons = []
        if task["metadata_extraction"]["requires_review"]:
            reasons.append("metadata_requires_review")
        if task["dependencies"]["missing_from_upstream"]:
            reasons.append("missing_input_files_in_upstream")
        verified = (source["source"]["commit_sha"] == VERIFIED_SOURCE_COMMIT
                    and task["prompt_sha256"] == VERIFIED_INPUT_PROMPTS.get(task["task_id"]))
        if reasons and not verified:
            excluded.append({"task_id": task["task_id"], "reasons": reasons})
        else:
            kept.append(task)
            if reasons:
                restored.append(task["task_id"])
    return {**source, "task_count": len(kept), "tasks": kept,
            "selection": {"policy": "exclude_review_or_missing_inputs_except_verified_prompts",
                          "upstream_task_count": source["task_count"],
                          "unflagged_task_count": len(kept)-len(restored),
                          "restored_verified_tasks": sorted(restored),
                          "excluded_task_count": len(excluded), "excluded": excluded}}


def write_manifest(manifest: dict, output: Path):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(manifest, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Local clone of THUDM/DataSciBench; worktree changes are ignored")
    parser.add_argument("--revision", default="HEAD", help="Source commit or ref; resolved to full commit SHA before reading")
    parser.add_argument("--task-id", action="append", help="Select source IDs, then apply catalog screening")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/DataSciBench/tasks/catalog.json"))
    args = parser.parse_args()
    try:
        manifest = import_prompts(args.repo, args.revision, args.task_id)
        if not manifest["tasks"]:
            raise ValueError("No tasks remain after catalog screening; existing catalog is unchanged")
        write_manifest(manifest, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Prompt import failed: {exc}\n")
    print(json.dumps({"manifest": str(args.output), "commit_sha": manifest["source"]["commit_sha"],
                      "task_count": manifest["task_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
