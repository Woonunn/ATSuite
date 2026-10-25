import hashlib
import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

from experiments.exp1.data.import_prompts import REPOSITORY, extract_metadata, _import_source_prompts as import_prompts, import_prompts as import_catalog, select_catalog, VERIFIED_INPUT_PROMPTS, VERIFIED_SOURCE_COMMIT, write_manifest


class PromptImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/"upstream"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", REPOSITORY+".git")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, tasks):
        for task_id, payload in tasks.items():
            path = self.repo/"data"/task_id/"prompt.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=True).encode())
        self.git("add", "data")
        self.git("-c", "user.name=Importer Test", "-c", "user.email=importer@example.invalid", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_exact_text_file_hashes_commit_and_dirty_worktree_reproducibility(self):
        prompt = "原始 Prompt\r\n  保留空白\n"
        raw = json.dumps({"prompt": prompt, "data_source_type": "1=no dependency"}, ensure_ascii=True, indent=2).encode()+b"\r\n"
        commit = self.commit({"human_1": raw})
        manifest = import_prompts(self.repo, commit)
        task = manifest["tasks"][0]
        self.assertEqual(manifest["source"]["commit_sha"], commit)
        self.assertEqual(task["prompt"], prompt)
        self.assertEqual(task["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(task["prompt_file_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(task["prompt_sha256"], task["prompt_file_sha256"])
        (self.repo/"data/human_1/prompt.json").write_text("dirty and invalid JSON")
        (self.repo/"data/local_only").mkdir()
        (self.repo/"data/local_only/prompt.json").write_text("{}")
        self.assertEqual(import_prompts(self.repo, commit), manifest)
        first, second = self.root/"first.json", self.root/"second.json"
        write_manifest(manifest, first)
        write_manifest(import_prompts(self.repo, commit), second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_strict_json_and_invalid_schema_never_execute_input(self):
        sentinel = self.root/"executed"
        malicious = f"__import__('pathlib').Path({str(sentinel)!r}).touch()".encode()
        for raw in (malicious, b'{"prompt":"x", "data_source_type":NaN}',
                    b'{"prompt":"first", "prompt":"second", "data_source_type":"1"}',
                    b'{"prompt":42,"data_source_type":"1"}', b'{"prompt":"x","data_source_type":null}',
                    b'{"prompt":"", "data_source_type":"1"}'):
            with self.subTest(raw=raw):
                # Each malformed blob is a committed source artifact, never a Python expression to execute.
                commit = self.commit({"human_1": raw})
                with self.assertRaisesRegex(ValueError, "Invalid Prompt JSON"):
                    import_prompts(self.repo, commit)
                self.assertFalse(sentinel.exists())
        code_prompt = f"```python\nimport os\n__import__('pathlib').Path({str(sentinel)!r}).touch()\n```"
        commit = self.commit({"human_1": {"prompt": code_prompt, "data_source_type": "1=no dependency"}})
        task = import_prompts(self.repo, commit)["tasks"][0]
        self.assertEqual(task["prompt"], code_prompt)
        self.assertEqual(task["dependencies"]["python_imports"], ["os"])
        self.assertFalse(sentinel.exists())

    def test_external_inputs_intermediates_and_only_explicit_file_dependencies(self):
        prompt = """1. Preparation
- Input: 'raw.csv'
- Output: './clean.csv'
2. Analysis
- Input: 'clean.csv'
- Output: 'analysis.csv'
3. Independent plot
- Input: 'second.csv'
- Output: 'plot.png'
"""
        metadata = extract_metadata("csv_excel_34", prompt, {"data/csv_excel_34/raw.csv"})
        self.assertEqual(metadata["input_files"], ["raw.csv", "second.csv"])
        self.assertEqual(metadata["intermediate_files"], ["clean.csv"])
        self.assertEqual(metadata["subtasks"][1]["depends_on"], ["1"])
        self.assertEqual(metadata["subtasks"][2]["depends_on"], [])
        self.assertEqual(metadata["dependencies"]["missing_from_upstream"], ["second.csv"])

    def test_real_prompt_formats_ranges_continued_outputs_and_imports(self):
        prompt = """Use PyTorch.
1. Data
- **Input**: Excel files in '..data/' directory.
- **Input filename**: from `data_1.xlsx` to `data_5.xlsx`
- **Output Filenames:**
  - `scatter.png`
  - `heatmap.png`
  - `cluster.png`
2. Code
```python
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
pd.read_csv('extra.csv')
print(iris.data)
with open('result.pkl', 'wb') as output:
    pass
```
Download source: https://example.org/data/remote.csv
Maybe refer to `ambiguous.txt`.
"""
        task = extract_metadata("dl_0", prompt, set())
        self.assertEqual(task["input_files"], [*[f"data_{i}.xlsx" for i in range(1,6)], "extra.csv"])
        self.assertEqual(task["output_files"], ["cluster.png", "heatmap.png", "result.pkl", "scatter.png"])
        self.assertEqual(task["dependencies"]["python_imports"], ["pandas", "sklearn.preprocessing"])
        self.assertIn("PyTorch", task["dependencies"]["library_mentions"])
        self.assertEqual(task["dependencies"]["data_urls"], ["https://example.org/data/remote.csv"])
        self.assertEqual(task["metadata_extraction"]["unresolved_file_references"], ["ambiguous.txt"])
        self.assertFalse(task["metadata_extraction"]["complete"])
        self.assertTrue(task["metadata_extraction"]["requires_review"])

    def test_prose_inputs_and_quoted_sentence_punctuation(self):
        task = extract_metadata("csv_excel_41", 'Given a file named "data.csv,". Save the result in "updated_data.csv."', set())
        self.assertEqual(task["input_files"], ["data.csv"])
        self.assertEqual(task["output_files"], ["updated_data.csv"])
        task = extract_metadata("csv_excel_9", "The file 'prices.csv' contains prices, and the file 'info.csv' contains stock metadata.", set())
        self.assertEqual(task["input_files"], ["info.csv", "prices.csv"])
        task = extract_metadata("csv_excel_37", "I currently have a dataset named date.csv with six columns.", set())
        self.assertEqual(task["input_files"], ["date.csv"])
        task = extract_metadata("human_1", "```python\nunknown_function('generated.csv')\npd.read_csv('generated.csv')\n```", set())
        self.assertEqual(task["input_files"], [])
        self.assertIn("generated.csv", task["metadata_extraction"]["unresolved_file_references"])

    def test_known_tasks_selection_unknown_ids_and_source_validation(self):
        commit = self.commit({"human_2": {"prompt":"Input: 'missing.csv'", "data_source_type":"2=has dependency"},
                              "bcb10": {"prompt":"Return the sum.", "data_source_type":"1_bcb"}})
        manifest = import_prompts(self.repo, commit, ["bcb10"])
        self.assertEqual([task["task_id"] for task in manifest["tasks"]], ["bcb10"])
        self.assertEqual(manifest["tasks"][0]["task_type"], "bcb")
        with self.assertRaisesRegex(ValueError, "Unknown task IDs"):
            import_prompts(self.repo, commit, ["missing"])
        self.git("remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(ValueError, "THUDM/DataSciBench"):
            import_prompts(self.repo, commit)

    def test_symlink_prompt_is_rejected(self):
        path = self.repo/"data/human_1/prompt.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(self.root/"outside.json")
        self.git("add", "data")
        self.git("-c", "user.name=Importer Test", "-c", "user.email=importer@example.invalid", "commit", "-qm", "symlink")
        with self.assertRaisesRegex(ValueError, "regular file"):
            import_prompts(self.repo)

    def test_missing_input_metadata_is_unknown_and_output_write_is_atomic(self):
        commit = self.commit({"dl_3": {"prompt":"Analyze the supplied images.", "data_source_type":"3=human written data"}})
        task = import_prompts(self.repo, commit)["tasks"][0]
        self.assertTrue(task["metadata_extraction"]["requires_review"])
        self.assertEqual(task["input_files"], [])
        output = self.root/"manifest.json"
        output.write_text("previous valid result")
        with self.assertRaises(ValueError):
            write_manifest({"invalid": float("nan")}, output)
        self.assertEqual(output.read_text(), "previous valid result")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["manifest.json", "upstream"])


    def test_source_import_preserves_raw_evidence_before_screening(self):
        self.commit({"human_12": {"prompt": "Input: 'adult.data'\nhttps://archive.ics.uci.edu/", "data_source_type": "2=has dependency"}})
        result = import_prompts(self.repo)
        self.assertEqual(result["task_count"], 1)
        self.assertEqual(result["tasks"][0]["task_id"], "human_12")
        self.assertIn("adult.data", result["tasks"][0]["dependencies"]["missing_from_upstream"])

    def test_cli_screens_by_default_and_preserves_catalog_when_selection_is_empty(self):
        self.commit({"human_2": {"prompt": "Input: 'missing.csv'", "data_source_type": "2=has dependency"},
                     "bcb10": {"prompt": "Return the sum.", "data_source_type": "1_bcb"}})
        output = self.root/"catalog.json"
        command = [sys.executable, "-m", "experiments.exp1.data.import_prompts", "--repo", str(self.repo), "--output", str(output)]
        subprocess.run(command, check=True, capture_output=True)
        selected = json.loads(output.read_text())
        self.assertEqual(import_catalog(self.repo), selected)
        self.assertEqual([t["task_id"] for t in selected["tasks"]], ["bcb10"])
        self.assertEqual(selected["selection"]["excluded_task_count"], 1)
        before = output.read_bytes()
        result = subprocess.run([*command, "--task-id", "human_2"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), before)


class CatalogSelectionTests(unittest.TestCase):
    def source(self):
        def task(name, review=False, missing=False, prompt_hash="fixture"):
            return {"task_id": name, "prompt_sha256": prompt_hash,
                    "metadata_extraction": {"requires_review": review},
                    "dependencies": {"missing_from_upstream": ["raw.csv"] if missing else []}}
        return {"source": {"commit_sha": VERIFIED_SOURCE_COMMIT}, "task_count": 4+len(VERIFIED_INPUT_PROMPTS), "tasks": [
            task("clean"), task("review", review=True), task("missing", missing=True),
            task("both", review=True, missing=True),
            *(task(name, missing=True, prompt_hash=value) for name, value in VERIFIED_INPUT_PROMPTS.items())]}

    def test_excludes_flagged_union_once_and_keeps_verified_exceptions(self):
        source = self.source()
        result = select_catalog(source)
        self.assertEqual(result["task_count"], 1+len(VERIFIED_INPUT_PROMPTS))
        self.assertEqual({t["task_id"] for t in result["tasks"]}, {"clean", *VERIFIED_INPUT_PROMPTS})
        self.assertEqual(result["selection"]["unflagged_task_count"], 1)
        self.assertEqual(result["selection"]["excluded_task_count"], 3)
        self.assertEqual(set(result["selection"]["restored_verified_tasks"]), set(VERIFIED_INPUT_PROMPTS))
        self.assertEqual(source["task_count"], 4+len(VERIFIED_INPUT_PROMPTS))

    def test_changed_source_commit_cannot_reuse_verified_exceptions(self):
        source = self.source(); source["source"]["commit_sha"] = "changed"
        result = select_catalog(source)
        self.assertEqual([t["task_id"] for t in result["tasks"]], ["clean"])
        self.assertEqual(result["selection"]["restored_verified_tasks"], [])

    def test_changed_prompt_hash_cannot_reuse_verified_exception(self):
        source = self.source()
        name = next(iter(VERIFIED_INPUT_PROMPTS))
        next(t for t in source["tasks"] if t["task_id"] == name)["prompt_sha256"] = "changed"
        result = select_catalog(source)
        self.assertNotIn(name, {t["task_id"] for t in result["tasks"]})
        self.assertEqual(result["task_count"], len(VERIFIED_INPUT_PROMPTS))


if __name__ == "__main__":
    unittest.main()
