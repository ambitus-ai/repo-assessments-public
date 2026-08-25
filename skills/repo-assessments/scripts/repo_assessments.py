#!/usr/bin/env python3
"""Create evidence-based assessments for one or more repositories.

The assessment is deliberately static and offline. It inventories candidate changes from ordinary
and merge commits, records build/test reproducibility signals and blockers, and emits exact
base/head pairs that can be sent to a dynamic fail-to-pass verifier.

Usage:
    python3 repo_assessments.py REPO [REPO ...] --out ./assessment-report

Outputs:
    portfolio_assessment.json  complete evidence and limitations
    repository_summary.csv     one comparable row per repository
    candidate_changes.csv      auditable candidate-change inventory
    assessment.md              human-readable evidence summary and next actions

Requires Python 3.9+ and git. Nothing is uploaded and no repository code is executed.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


CODE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rb",
    ".java", ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".cs", ".php", ".rs", ".scala", ".m", ".mm", ".dart", ".ex", ".exs", ".erl",
    ".hrl", ".clj", ".cljs", ".lua", ".r", ".jl", ".sql", ".sh", ".bash", ".zsh",
    ".vue", ".svelte", ".fs", ".fsx", ".vb", ".sol",
}
TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|specs?|__tests__|e2e|integration[-_]?tests?|cypress|playwright)(/|$)"
    r"|([._-](test|spec)\.)|((^|/)(test|spec)_)|(_test\.(go|py|rb|js|jsx|ts|tsx|java|kt|c|cpp|rs)$)",
    re.IGNORECASE,
)
VENDOR_PARTS = {
    "node_modules", "vendor", "vendors", "third_party", "third-party", "dist", "build",
    "target", ".next", ".nuxt", ".venv", "venv", "site-packages", "Pods", "coverage",
    "__pycache__", "generated", "gen", "fixtures", "snapshots",
}
GENERATED_PATH = re.compile(
    r"(^|/)(generated|gen|dist|build|vendor|third[_-]?party|snapshots?|fixtures?)(/|$)"
    r"|\.(min\.(js|css)|map|lock)$",
    re.IGNORECASE,
)
ISSUE_REF = re.compile(
    r"(#\d{1,7}\b)|([A-Z][A-Z0-9]{1,11}-\d{1,7}\b)"
    r"|(\b(clos|fix|resolv)(e|es|ed|ing)?\b[:\s]+#?\d+)",
    re.IGNORECASE,
)
FIX_INTENT = re.compile(
    r"\b(fix|fixed|fixes|bug|regression|incorrect|crash|error|failure|broken|repair|resolve|prevent|"
    r"handle|edge case|race|leak|panic|exception|timeout|off[- ]by[- ]one)\b",
    re.IGNORECASE,
)
MECHANICAL = re.compile(
    r"\b(dependabot|renovate|bump|upgrade deps?|update lockfile|format(?:ting)?|lint(?:ing)?|"
    r"prettier|translation|i18n|l10n|chore(?:\(.+?\))?: release|generated?)\b",
    re.IGNORECASE,
)
REVERT = re.compile(r"^revert\b", re.IGNORECASE)
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "mix.exs", "Package.swift", "*.csproj",
}
LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock", "uv.lock",
    "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "mix.lock",
    "gradle.lockfile", "packages.lock.json",
}
CI_PREFIXES = (
    ".github/workflows/", ".circleci/", ".buildkite/", ".gitlab-ci.yml", "Jenkinsfile",
    "azure-pipelines.yml", ".travis.yml", "bitbucket-pipelines.yml",
)
REPRO_NAMES = {
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "flake.nix", "shell.nix", "Vagrantfile", "devbox.json", "mise.toml", ".tool-versions",
}
REPRO_PREFIXES = (".devcontainer/", "nix/", "docker/", "containers/")
SERVICE_PATH = re.compile(
    r"(^|/)(docker-compose|compose|k8s|kubernetes|helm|terraform|playwright|cypress|e2e)"
    r"([./_-]|$)|(^|/)(postgres|mysql|redis|mongo|kafka|elasticsearch)([./_-]|$)",
    re.IGNORECASE,
)
LICENSE_NAMES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING"}
COPYLEFT = re.compile(r"\b(AGPL|GPL|LGPL|SSPL|EUPL|CC-BY-SA|OSL)\b", re.IGNORECASE)
PERMISSIVE = re.compile(r"\b(MIT|Apache|BSD|ISC|MPL|Unlicense|Zlib|CC0)\b", re.IGNORECASE)
MAX_READ_BYTES = 500_000
CANDIDATE_COLUMNS = [
    "repository", "repo_path", "base_sha", "head_sha", "authored_at",
    "subject", "is_merge", "touches_tests", "references_issue", "corrective_intent",
    "metadata_word_count", "mechanical_change_signal", "revert_signal",
    "files_changed", "source_files_changed", "test_files_changed", "changed_lines",
    "source_paths", "test_paths", "blockers", "dynamic_validation_status",
    "verification_command",
]


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace"
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip().replace("\n", " ")[:300]
        raise RuntimeError("git {}: {}".format(" ".join(args[:4]), detail))
    return proc.stdout if proc.returncode == 0 else ""


def is_git_repo(path: Path) -> bool:
    return run_git(path, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"


def tracked_paths(repo: Path) -> list[Path]:
    return [Path(p) for p in run_git(repo, "ls-files").splitlines() if p]


def is_vendor(path: Path) -> bool:
    return any(part in VENDOR_PARTS for part in path.parts)


def is_test(path: Path) -> bool:
    return bool(TEST_PATH.search(path.as_posix()))


def is_code(path: Path) -> bool:
    return path.suffix.lower() in CODE_EXTENSIONS and not is_vendor(path)


def is_manifest(path: Path) -> bool:
    return path.name in MANIFEST_NAMES or path.suffix.lower() == ".csproj"


def bounded_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_READ_BYTES:
            return ""
        return path.read_text(errors="replace")
    except OSError:
        return ""


def classify_license(repo: Path, paths: Iterable[Path]) -> tuple[str, Optional[str]]:
    for rel in paths:
        if rel.name not in LICENSE_NAMES or len(rel.parts) != 1:
            continue
        text = bounded_text(repo / rel)[:20_000]
        low = text.lower()
        permissive_phrases = (
            "permission is hereby granted, free of charge",
            "redistribution and use in source and binary forms",
            "licensed under the apache license",
            "mozilla public license",
        )
        if COPYLEFT.search(text):
            return "copyleft", rel.as_posix()
        if PERMISSIVE.search(text) or any(p in low for p in permissive_phrases):
            return "permissive", rel.as_posix()
        return "custom-or-unclear", rel.as_posix()
    return "none-found", None


def detect_test_commands(repo: Path, paths: list[Path]) -> list[str]:
    names = {p.name for p in paths}
    path_set = {p.as_posix() for p in paths}
    commands = []

    def scoped(directory: Path, command: str) -> str:
        if directory == Path("."):
            return command
        return f"cd {shlex.quote(directory.as_posix())} && {command}"

    if {"pytest.ini", "conftest.py"} & names:
        commands.append("pytest")
    for pyproject_path in [p for p in paths if p.name == "pyproject.toml"][:20]:
        pyproject = bounded_text(repo / pyproject_path)
        command = scoped(pyproject_path.parent, "pytest")
        if "pytest" in pyproject.lower() and command not in commands:
            commands.append(command)
    for package_path in [p for p in paths if p.name == "package.json"][:20]:
        package = bounded_text(repo / package_path)
        if not package:
            continue
        try:
            scripts = json.loads(package).get("scripts", {})
            test_script = str(scripts.get("test", "")).strip()
            if test_script and "no test specified" not in test_script.lower():
                directory = package_path.parent
                prefix = "" if directory == Path(".") else directory.as_posix() + "/"
                if prefix + "pnpm-lock.yaml" in path_set:
                    commands.append(scoped(directory, "pnpm test"))
                elif prefix + "yarn.lock" in path_set:
                    commands.append(scoped(directory, "yarn test"))
                else:
                    commands.append(scoped(directory, "npm test"))
        except (ValueError, TypeError):
            pass
    for mod in [p for p in paths if p.name == "go.mod"][:20]:
        commands.append(scoped(mod.parent, "go test ./..."))
    for manifest in [p for p in paths if p.name == "Cargo.toml"][:20]:
        commands.append(scoped(manifest.parent, "cargo test"))
    for manifest in [p for p in paths if p.name == "pom.xml"][:20]:
        wrapper = manifest.parent / "mvnw"
        command = "./mvnw test" if wrapper.as_posix() in path_set else "mvn test"
        commands.append(scoped(manifest.parent, command))
    for manifest in [p for p in paths if p.name in {"build.gradle", "build.gradle.kts"}][:20]:
        wrapper = manifest.parent / "gradlew"
        command = "./gradlew test" if wrapper.as_posix() in path_set else "gradle test"
        commands.append(scoped(manifest.parent, command))
    for manifest in [p for p in paths if p.name == "Gemfile"][:20]:
        commands.append(scoped(manifest.parent, "bundle exec rspec"))
    for manifest in [p for p in paths if p.name == "mix.exs"][:20]:
        commands.append(scoped(manifest.parent, "mix test"))
    if any(p.suffix.lower() == ".sln" for p in paths):
        commands.append("dotnet test")
    for manifest in [p for p in paths if p.name == "Package.swift"][:20]:
        commands.append(scoped(manifest.parent, "swift test"))
    return list(dict.fromkeys(commands))


def analyze_readiness(repo: Path, paths: list[Path], commit_count: int) -> dict:
    source_files = [p for p in paths if is_code(p) and not is_test(p)]
    test_files = [p for p in paths if is_code(p) and is_test(p)]
    source_loc = sum(len(bounded_text(repo / p).splitlines()) for p in source_files)
    test_loc = sum(len(bounded_text(repo / p).splitlines()) for p in test_files)
    manifests = sorted(p.as_posix() for p in paths if is_manifest(p))
    lockfiles = sorted(p.as_posix() for p in paths if p.name in LOCK_NAMES)
    ci = sorted(p.as_posix() for p in paths if p.as_posix().startswith(CI_PREFIXES))
    reproducibility = sorted(
        p.as_posix()
        for p in paths
        if p.name in REPRO_NAMES or p.as_posix().startswith(REPRO_PREFIXES)
    )
    service_signals = sorted(p.as_posix() for p in paths if SERVICE_PATH.search(p.as_posix()))[:30]
    test_commands = detect_test_commands(repo, paths)
    license_class, license_file = classify_license(repo, paths)
    ratio = round(100 * test_loc / max(source_loc, 1), 1)

    blockers = []
    if not test_files:
        blockers.append("No tracked test files detected.")
    if not test_commands:
        blockers.append("No test command could be inferred; execution setup needs manual discovery.")
    if not lockfiles:
        blockers.append("No dependency lockfile detected; historical environments may drift.")
    if not ci:
        blockers.append("No CI configuration detected to reveal the canonical test path.")
    if service_signals:
        blockers.append("Service or end-to-end infrastructure signals may prevent hermetic execution.")
    if license_class in {"none-found", "custom-or-unclear"}:
        blockers.append("Repository licensing needs review before derived artifacts are distributed.")
    return {
        "source_files": len(source_files),
        "test_files": len(test_files),
        "source_loc": source_loc,
        "test_loc": test_loc,
        "test_to_source_ratio_pct": ratio,
        "test_commands": test_commands,
        "manifests": manifests,
        "lockfiles": lockfiles,
        "ci_configs": ci,
        "reproducibility_files": reproducibility,
        "service_or_e2e_signals": service_signals,
        "license": {"class": license_class, "file": license_file},
        "observations": {
            "tests_present": bool(test_files),
            "test_command_detected": bool(test_commands),
            "dependency_locking_present": bool(lockfiles),
            "ci_present": bool(ci),
            "reproducibility_configuration_present": bool(reproducibility),
            "service_or_e2e_review_needed": bool(service_signals),
            "history_commits_available": commit_count,
        },
        "blockers": blockers,
    }


def read_commits(repo: Path, max_commits: int, since: Optional[str]) -> list[dict]:
    # First-parent history represents changes as they landed on the checked-out branch. It keeps
    # direct squash/rebase commits while avoiding double-counting both a merge and its branch
    # commits as separate assessment candidates.
    args = ["log", "--first-parent", f"--max-count={max_commits + 1}"]
    if since:
        args.append(f"--since={since}")
    args.append("--format=%H%x00%P%x00%aI%x00%s%x00%b%x1e")
    raw = run_git(repo, *args)
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split("\x00", 4)
        if len(fields) != 5:
            continue
        sha, parents, authored_at, subject, body = fields
        parent_list = parents.split()
        if not parent_list:
            continue
        commits.append({
            "sha": sha, "parent": parent_list[0], "parents": parent_list,
            "authored_at": authored_at, "subject": subject.strip(), "body": body.strip(),
        })
    return commits[:max_commits]


def diff_stats(repo: Path, base: str, head: str) -> dict:
    raw = run_git(repo, "diff", "--no-renames", "--numstat", base, head)
    changed = []
    additions = deletions = binary_files = 0
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, name = parts
        path = Path(name)
        binary = not (added.isdigit() and deleted.isdigit())
        a = int(added) if added.isdigit() else 0
        d = int(deleted) if deleted.isdigit() else 0
        additions += a
        deletions += d
        binary_files += int(binary)
        changed.append({
            "path": path.as_posix(), "additions": a, "deletions": d, "binary": binary,
            "is_code": is_code(path), "is_test": is_code(path) and is_test(path),
            "is_generated_or_vendor": is_vendor(path) or bool(GENERATED_PATH.search(path.as_posix())),
            "is_docs": path.suffix.lower() in DOC_EXTENSIONS or path.parts[:1] == ("docs",),
            "is_manifest": is_manifest(path) or path.name in LOCK_NAMES,
        })
    return {
        "files": changed, "file_count": len(changed), "additions": additions,
        "deletions": deletions, "changed_lines": additions + deletions,
        "binary_files": binary_files,
    }


def candidate_evidence(commit: dict, stats: dict) -> Optional[dict]:
    files = stats["files"]
    source = [f for f in files if f["is_code"] and not f["is_test"] and not f["is_generated_or_vendor"]]
    tests = [f for f in files if f["is_test"] and not f["is_generated_or_vendor"]]
    if not source:
        return None
    generated = [f for f in files if f["is_generated_or_vendor"]]
    text = (commit["subject"] + "\n" + commit["body"]).strip()
    has_issue = bool(ISSUE_REF.search(text))
    has_fix_intent = bool(FIX_INTENT.search(text))
    mechanical = bool(MECHANICAL.search(commit["subject"]))
    metadata_word_count = len(re.findall(r"\b\w+\b", text))
    revert = bool(REVERT.search(commit["subject"]))
    blockers = []
    if not tests:
        blockers.append("No test files changed; a candidate-specific test may need to be authored.")
    if mechanical:
        blockers.append("Commit subject indicates a mechanical or dependency-only change.")
    if revert:
        blockers.append("Commit is a revert and needs manual interpretation.")
    if generated:
        blockers.append("Diff includes generated or vendor paths.")
    return {
        "sha": commit["sha"],
        "base_sha": commit["parent"],
        "is_merge": len(commit["parents"]) > 1,
        "authored_at": commit["authored_at"],
        "subject": commit["subject"],
        "body_excerpt": commit["body"][:400],
        "references_issue": has_issue,
        "corrective_intent": has_fix_intent,
        "metadata_word_count": metadata_word_count,
        "mechanical_change_signal": mechanical,
        "revert_signal": revert,
        "touches_tests": bool(tests),
        "source_files_changed": len(source),
        "test_files_changed": len(tests),
        "generated_or_vendor_files_changed": len(generated),
        "files_changed": stats["file_count"],
        "additions": stats["additions"],
        "deletions": stats["deletions"],
        "changed_lines": stats["changed_lines"],
        "source_paths": [f["path"] for f in source[:20]],
        "test_paths": [f["path"] for f in tests[:20]],
        "blockers": blockers,
        "dynamic_validation": {
            "status": "not-run",
            "reason": "Static evidence only; fail-to-pass behavior has not been executed.",
            "base_sha": commit["parent"],
            "head_sha": commit["sha"],
        },
    }


def analyze_candidates(repo: Path, commits: list[dict]) -> dict:
    candidates = []
    for index, commit in enumerate(commits, 1):
        if index == 1 or index % 50 == 0:
            print(f"    inspecting change {index}/{len(commits)}", file=sys.stderr)
        evidence = candidate_evidence(commit, diff_stats(repo, commit["parent"], commit["sha"]))
        if evidence:
            candidates.append(evidence)
    candidates.sort(key=lambda c: c["authored_at"], reverse=True)
    test_pct = round(100 * sum(c["touches_tests"] for c in candidates) / max(len(candidates), 1), 1)
    issue_pct = round(100 * sum(c["references_issue"] for c in candidates) / max(len(candidates), 1), 1)
    return {
        "commits_analyzed": len(commits),
        "eligible_changes": len(candidates),
        "candidate_yield_pct": round(100 * len(candidates) / max(len(commits), 1), 1),
        "candidates_touching_tests_pct": test_pct,
        "candidates_with_issue_refs_pct": issue_pct,
        "candidates": candidates,
    }


def assess_repo(repo: Path, max_commits: int, since: Optional[str]) -> dict:
    repo = repo.resolve()
    name = repo.name
    print(f"  [{name}] inventorying repository", file=sys.stderr)
    paths = tracked_paths(repo)
    commit_count = int(run_git(repo, "rev-list", "--count", "HEAD").strip() or 0)
    shallow = run_git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() == "true"
    dirty = bool(run_git(repo, "status", "--porcelain").strip())
    contributors = len([x for x in run_git(repo, "shortlog", "-sne", "HEAD").splitlines() if "\t" in x])
    first_date = run_git(repo, "log", "--reverse", "--format=%aI", "--max-parents=0").splitlines()
    last_date = run_git(repo, "log", "-1", "--format=%aI").strip()
    readiness = analyze_readiness(repo, paths, commit_count)
    commits = read_commits(repo, max_commits, since)
    print(f"  [{name}] analyzing {len(commits)} candidate-producing commits", file=sys.stderr)
    candidate_analysis = analyze_candidates(repo, commits)
    verifier = Path(__file__).resolve().parents[2] / "repo-verify" / "scripts" / "verify_env.py"
    for candidate in candidate_analysis["candidates"]:
        if candidate["touches_tests"]:
            argv = [
                "python3", str(verifier), str(repo), "--base", candidate["base_sha"],
                "--head", candidate["sha"],
            ]
            candidate["dynamic_validation"]["suggested_argv"] = argv
            candidate["dynamic_validation"]["suggested_command"] = shlex.join(argv)
        else:
            candidate["dynamic_validation"]["suggested_argv"] = None
            candidate["dynamic_validation"]["suggested_command"] = None
            candidate["dynamic_validation"]["reason"] += " No candidate-provided test change is available to verify."
    blockers = list(readiness["blockers"])
    if shallow:
        blockers.insert(0, "Clone is shallow; deepen it before comparing candidate yield.")
    if dirty:
        blockers.insert(
            0,
            "Working tree is dirty; tracked edits can affect readiness evidence and untracked files are excluded.",
        )
    if not candidate_analysis["eligible_changes"]:
        blockers.append("No source-changing candidates were found in the analyzed history window.")
    elif not any(c["touches_tests"] for c in candidate_analysis["candidates"]):
        blockers.append("No candidate changes modify tests, increasing follow-up preparation effort.")
    top_next = []
    if any(c["touches_tests"] for c in candidate_analysis["candidates"]):
        top_next.append("Dynamically validate selected test-touching base/head pairs.")
    if readiness["test_commands"]:
        top_next.append("Rehearse the detected test command in an isolated checkout at historical revisions.")
    else:
        top_next.append("Document a deterministic install and test command before assessing at scale.")
    if readiness["service_or_e2e_signals"]:
        top_next.append("Separate hermetic candidates from changes that require external services or browsers.")
    return {
        "repo_name": name,
        "repo_path": str(repo),
        "head_sha": run_git(repo, "rev-parse", "HEAD").strip(),
        "history": {
            "commits": commit_count,
            "contributors": contributors,
            "first_commit_date": min(first_date)[:10] if first_date else None,
            "last_commit_date": last_date[:10] if last_date else None,
            "is_shallow_clone": shallow,
            "tracked_worktree_dirty": dirty,
            "analysis_window": {"max_commits": max_commits, "since": since},
        },
        "repository_evidence": readiness,
        "candidate_history": candidate_analysis,
        "blockers": blockers,
        "recommended_next_steps": top_next,
        "limitations": [
            "Static analysis does not establish that a candidate test fails at the base revision and passes at the fix revision.",
            "Commit messages and local git topology may omit issue discussions, review context, and squash-merged pull-request metadata.",
            "Test-file and service detection are path heuristics; monorepos and custom harnesses need manual review.",
            "Candidate discovery follows first-parent history to avoid double-counting merged branch commits.",
            "The assessment reports evidence and limitations; it is not a quality judgment about the software.",
        ],
    }


def summary_row(report: dict) -> dict:
    ready = report["repository_evidence"]
    cand = report["candidate_history"]
    return {
        "repository": report["repo_name"],
        "path": report["repo_path"],
        "commits_total": report["history"]["commits"],
        "commits_analyzed": cand["commits_analyzed"],
        "eligible_changes": cand["eligible_changes"],
        "candidate_yield_pct": cand["candidate_yield_pct"],
        "candidates_touching_tests_pct": cand["candidates_touching_tests_pct"],
        "candidates_with_issue_refs_pct": cand["candidates_with_issue_refs_pct"],
        "test_files": ready["test_files"],
        "test_to_source_ratio_pct": ready["test_to_source_ratio_pct"],
        "test_commands": "; ".join(ready["test_commands"]),
        "lockfiles": len(ready["lockfiles"]),
        "ci_configs": len(ready["ci_configs"]),
        "reproducibility_files": len(ready["reproducibility_files"]),
        "license": ready["license"]["class"],
        "blockers": " | ".join(report["blockers"]),
    }


def candidate_rows(reports: list[dict]) -> Iterable[dict]:
    for report in reports:
        for candidate in report["candidate_history"]["candidates"]:
            yield {
                "repository": report["repo_name"],
                "repo_path": report["repo_path"],
                "base_sha": candidate["base_sha"],
                "head_sha": candidate["sha"],
                "authored_at": candidate["authored_at"],
                "subject": candidate["subject"],
                "is_merge": candidate["is_merge"],
                "touches_tests": candidate["touches_tests"],
                "references_issue": candidate["references_issue"],
                "corrective_intent": candidate["corrective_intent"],
                "metadata_word_count": candidate["metadata_word_count"],
                "mechanical_change_signal": candidate["mechanical_change_signal"],
                "revert_signal": candidate["revert_signal"],
                "files_changed": candidate["files_changed"],
                "source_files_changed": candidate["source_files_changed"],
                "test_files_changed": candidate["test_files_changed"],
                "changed_lines": candidate["changed_lines"],
                "source_paths": "; ".join(candidate["source_paths"]),
                "test_paths": "; ".join(candidate["test_paths"]),
                "blockers": " | ".join(candidate["blockers"]),
                "dynamic_validation_status": candidate["dynamic_validation"]["status"],
                "verification_command": candidate["dynamic_validation"]["suggested_command"] or "",
            }


def write_csv(path: Path, rows: list[dict], fieldnames: Optional[list[str]] = None) -> None:
    if not rows:
        if fieldnames:
            with path.open("w", newline="") as handle:
                csv.DictWriter(handle, fieldnames=fieldnames).writeheader()
        else:
            path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, reports: list[dict], top_candidates: int) -> None:
    lines = [
        "# Repository assessment", "",
        f"Generated {datetime.now(timezone.utc).isoformat()[:19]}Z from local git history. No repository code was executed.", "",
        "## Portfolio comparison", "",
        "| Repository | Commits analyzed | Candidate changes | Candidates with tests | Issue-linked candidates | Test files | Blockers |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        ready, cand = report["repository_evidence"], report["candidate_history"]
        lines.append(
            f"| {report['repo_name']} | {cand['commits_analyzed']} | {cand['eligible_changes']} | "
            f"{cand['candidates_touching_tests_pct']}% | {cand['candidates_with_issue_refs_pct']}% | "
            f"{ready['test_files']} | {len(report['blockers'])} |"
        )
    lines += ["", "The comparison reports observed evidence and blockers; it does not rank repositories or candidates.", ""]
    for report in reports:
        ready, cand = report["repository_evidence"], report["candidate_history"]
        lines += [
            f"## {report['repo_name']}", "",
            f"Analyzed {cand['commits_analyzed']} non-root commits and identified "
            f"{cand['eligible_changes']} source-changing candidates.", "",
            "### Repository evidence", "",
            f"- Tests: {ready['test_files']} files; test/source line ratio {ready['test_to_source_ratio_pct']}%.",
            f"- Inferred test commands: {', '.join('`' + c + '`' for c in ready['test_commands']) or 'none'}.",
            f"- Reproducibility: {len(ready['manifests'])} manifests, {len(ready['lockfiles'])} lockfiles, "
            f"{len(ready['ci_configs'])} CI files, {len(ready['reproducibility_files'])} environment files.",
            f"- Service/end-to-end path signals: {len(ready['service_or_e2e_signals'])}; license: {ready['license']['class']}.",
            "", "### Main blockers", "",
        ]
        lines += [f"- {item}" for item in report["blockers"]] or ["- No major static blockers detected."]
        lines += ["", "### Candidate changes (newest first)", ""]
        top = cand["candidates"][:top_candidates]
        if not top:
            lines.append("No source-changing candidates were found in the analysis window.")
        for candidate in top:
            flags = []
            if candidate["touches_tests"]:
                flags.append("changes tests")
            if candidate["references_issue"]:
                flags.append("issue-linked")
            if candidate["corrective_intent"]:
                flags.append("corrective intent")
            lines += [
                f"- **`{candidate['sha'][:10]}`** — {candidate['subject']} "
                f"({', '.join(flags) or 'limited context'}; "
                f"{candidate['files_changed']} files, {candidate['changed_lines']} changed lines)",
                f"  - Base/head: `{candidate['base_sha']}` → `{candidate['sha']}`; dynamic validation: not run.",
            ]
            if candidate["dynamic_validation"]["suggested_command"]:
                lines.append(f"  - Verify: `{candidate['dynamic_validation']['suggested_command']}`")
            else:
                lines.append("  - Verification is blocked until a candidate-specific test is supplied.")
        lines += ["", "### Recommended next steps", ""]
        lines += [f"- {step}" for step in report["recommended_next_steps"]]
        lines.append("")
    lines += [
        "## Interpretation limits", "",
        "- Static test-file changes do not prove a fail-to-pass transition; run candidates in isolated historical checkouts.",
        "- Local git history can understate issue and review context, especially after squash merges.",
        "- The reported evidence does not constitute a repository or maintainer quality judgment.",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="+", help="paths to local git repositories")
    parser.add_argument("--out", default="./assessment-report", help="output directory")
    parser.add_argument("--max-commits", type=int, default=500, help="most recent non-root commits to inspect per repository")
    parser.add_argument("--since", help="optional git date expression limiting history, for example 2024-01-01")
    parser.add_argument("--top-candidates", type=int, default=10, help="candidates shown per repository in assessment.md")
    args = parser.parse_args()
    if args.max_commits < 1:
        parser.error("--max-commits must be at least 1")
    if args.top_candidates < 1:
        parser.error("--top-candidates must be at least 1")

    repos = [Path(p).resolve() for p in args.repos]
    bad = [str(p) for p in repos if not is_git_repo(p)]
    if bad:
        parser.error("not local git repositories: " + ", ".join(bad))
    unborn = [str(p) for p in repos if not run_git(p, "rev-parse", "--verify", "HEAD", check=False).strip()]
    if unborn:
        parser.error("repositories have no commits: " + ", ".join(unborn))
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"Assessing {len(repos)} repository/repositories", file=sys.stderr)
    reports = [assess_repo(repo, args.max_commits, args.since) for repo in repos]
    generated = datetime.now(timezone.utc).isoformat()[:19] + "Z"
    portfolio = {
        "generated_at": generated,
        "analysis_type": "static-offline-repository-assessment",
        "repository_count": len(reports),
        "dynamic_verification_performed": False,
        "repositories": reports,
    }
    (out / "portfolio_assessment.json").write_text(json.dumps(portfolio, indent=2))
    write_csv(out / "repository_summary.csv", [summary_row(r) for r in reports])
    write_csv(out / "candidate_changes.csv", list(candidate_rows(reports)), CANDIDATE_COLUMNS)
    write_markdown(out / "assessment.md", reports, args.top_candidates)
    print(json.dumps({
        "out": str(out),
        "repositories": len(reports),
        "candidates": sum(r["candidate_history"]["eligible_changes"] for r in reports),
        "files": ["assessment.md", "candidate_changes.csv", "portfolio_assessment.json", "repository_summary.csv"],
    }, indent=2))


if __name__ == "__main__":
    main()
