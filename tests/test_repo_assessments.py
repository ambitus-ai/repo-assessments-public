import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "repo-assessments" / "scripts" / "repo_assessments.py"


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def init_repo(path):
    path.mkdir()
    run("git", "init", "-b", "main", cwd=path)
    run("git", "config", "user.name", "Fixture Author", cwd=path)
    run("git", "config", "user.email", "fixture@example.com", cwd=path)


def commit_all(path, subject, body=None):
    run("git", "add", ".", cwd=path)
    command = ["git", "commit", "-m", subject]
    if body:
        command += ["-m", body]
    run(*command, cwd=path)
    return run("git", "rev-parse", "HEAD", cwd=path).stdout.strip()


class RepoAssessmentsCliTest(unittest.TestCase):
    def make_rich_repo(self, root):
        repo = root / "rich-repo"
        init_repo(repo)
        write(repo / "src" / "calc.py", "def divide(a, b):\n    return a / b\n")
        write(repo / "tests" / "test_calc.py", "from src.calc import divide\n\ndef test_divide():\n    assert divide(4, 2) == 2\n")
        write(repo / "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
        write(repo / "uv.lock", "version = 1\n")
        write(repo / "Dockerfile", "FROM python:3.12-slim\nWORKDIR /work\n")
        write(repo / ".github" / "workflows" / "test.yml", "jobs:\n  test:\n    steps: []\n")
        write(
            repo / "LICENSE",
            "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n",
        )
        commit_all(repo, "initial implementation")

        write(
            repo / "src" / "calc.py",
            "def divide(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n",
        )
        write(
            repo / "tests" / "test_calc.py",
            "import pytest\nfrom src.calc import divide\n\ndef test_divide():\n    assert divide(4, 2) == 2\n\ndef test_zero():\n    with pytest.raises(ValueError):\n        divide(1, 0)\n",
        )
        fix_sha = commit_all(
            repo,
            "fix division-by-zero failure",
            "Fixes #12 by rejecting a zero denominator with a stable public error.",
        )
        write(repo / "README.md", "# Calculator\n\nA tiny fixture.\n")
        commit_all(repo, "document calculator")
        return repo, fix_sha

    def make_sparse_repo(self, root):
        repo = root / "sparse-repo"
        init_repo(repo)
        write(repo / "src" / "value.py", "VALUE = 1\n")
        commit_all(repo, "initial source")
        write(repo / "src" / "value.py", "VALUE = 2\n")
        commit_all(repo, "adjust value")
        return repo

    def test_multi_repository_outputs_and_direct_commit_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rich_repo, fix_sha = self.make_rich_repo(root)
            sparse_repo = self.make_sparse_repo(root)
            output = root / "assessment"

            result = run(
                sys.executable,
                str(SCRIPT),
                str(rich_repo),
                str(sparse_repo),
                "--out",
                str(output),
                "--max-commits",
                "50",
            )
            status = json.loads(result.stdout)
            self.assertEqual(status["repositories"], 2)
            self.assertGreaterEqual(status["candidates"], 2)
            self.assertEqual(
                sorted(p.name for p in output.iterdir()),
                ["assessment.md", "candidate_changes.csv", "portfolio_assessment.json", "repository_summary.csv"],
            )

            portfolio = json.loads((output / "portfolio_assessment.json").read_text())
            reports = {report["repo_name"]: report for report in portfolio["repositories"]}
            rich = reports["rich-repo"]
            sparse = reports["sparse-repo"]
            candidates = rich["candidate_history"]["candidates"]
            fix = next(candidate for candidate in candidates if candidate["sha"] == fix_sha)

            self.assertFalse(fix["is_merge"], "ordinary commits must remain discoverable")
            self.assertTrue(fix["touches_tests"])
            self.assertTrue(fix["references_issue"])
            self.assertTrue(fix["corrective_intent"])
            self.assertEqual(fix["dynamic_validation"]["status"], "not-run")
            self.assertIn(fix["base_sha"], fix["dynamic_validation"]["suggested_command"])
            self.assertIn(fix["sha"], fix["dynamic_validation"]["suggested_command"])
            self.assertIn("repository_evidence", rich)
            self.assertGreater(
                rich["candidate_history"]["candidates_touching_tests_pct"],
                sparse["candidate_history"]["candidates_touching_tests_pct"],
            )

            with (output / "candidate_changes.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            fix_row = next(row for row in rows if row["head_sha"] == fix_sha)
            self.assertEqual(fix_row["repository"], "rich-repo")
            self.assertEqual(fix_row["touches_tests"], "True")
            self.assertEqual(fix_row["dynamic_validation_status"], "not-run")
            self.assertIn("verify_env.py", fix_row["verification_command"])

            markdown = (output / "assessment.md").read_text()
            self.assertIn("Portfolio comparison", markdown)
            self.assertIn("dynamic validation: not run", markdown)

    def test_invalid_repository_is_rejected_without_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "assessment"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(root / "missing"), "--out", str(output)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not local git repositories", result.stderr)
            self.assertFalse(output.exists())

    def test_first_parent_history_avoids_merge_double_counting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "merge-repo"
            init_repo(repo)
            write(repo / "src" / "value.py", "VALUE = 1\n")
            write(repo / "tests" / "test_value.py", "def test_value():\n    assert True\n")
            write(repo / "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
            commit_all(repo, "initial source")
            run("git", "checkout", "-b", "fix-value", cwd=repo)
            write(repo / "src" / "value.py", "VALUE = 2\n")
            write(repo / "tests" / "test_value.py", "def test_value():\n    assert 2 == 2\n")
            branch_sha = commit_all(repo, "fix incorrect value", "Fixes #8 with a regression test.")
            run("git", "checkout", "main", cwd=repo)
            run("git", "merge", "--no-ff", "fix-value", "-m", "Merge fix for #8", cwd=repo)
            merge_sha = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
            output = root / "assessment"

            run(sys.executable, str(SCRIPT), str(repo), "--out", str(output))
            portfolio = json.loads((output / "portfolio_assessment.json").read_text())
            history = portfolio["repositories"][0]["candidate_history"]
            candidate_shas = [candidate["sha"] for candidate in history["candidates"]]

            self.assertEqual(history["commits_analyzed"], 1)
            self.assertEqual(candidate_shas, [merge_sha])
            self.assertNotIn(branch_sha, candidate_shas)

    def test_empty_candidate_inventory_still_has_csv_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "docs-only"
            init_repo(repo)
            write(repo / "README.md", "# Notes\n")
            commit_all(repo, "initial notes")
            write(repo / "README.md", "# Notes\n\nMore notes.\n")
            commit_all(repo, "expand notes")
            output = root / "assessment"

            run(sys.executable, str(SCRIPT), str(repo), "--out", str(output))
            with (output / "candidate_changes.csv").open(newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, [
                    "repository", "repo_path", "base_sha", "head_sha", "authored_at",
                    "subject", "is_merge", "touches_tests", "references_issue", "corrective_intent",
                    "metadata_word_count", "mechanical_change_signal", "revert_signal",
                    "files_changed", "source_files_changed", "test_files_changed", "changed_lines",
                    "source_paths", "test_paths", "blockers", "dynamic_validation_status", "verification_command",
                ])
                self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()
