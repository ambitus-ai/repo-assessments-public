---
name: repo-assessments
description: "Create evidence-based assessments for one or more local git repositories. Inventories ordinary and merge commits, records candidate base/head changes, measures tests and environment reproducibility, compares repositories, and identifies blockers and dynamic-validation next steps. Use when evaluating codebase suitability, estimating preparation effort, triaging candidate fixes, or comparing a repository portfolio."
---

# Repository assessments

Create an auditable assessment of repository evidence and likely preparation constraints. Keep
three claims separate:

1. **Repository evidence** — tests, dependency pinning, CI, execution isolation, history, and
   licensing signals.
2. **Candidate evidence** — bounded source changes, accompanying tests, recoverable problem
   context, and corrective intent.
3. **Dynamic validation** — an observed failure at the base revision and success at the fixed
   revision. The static assessment never claims this third result.

## Workflow

1. Use full local clones when possible. Note shallow or incomplete history before comparing
   repositories.

2. Start from a clean working tree, then run the offline assessment with the same history window
   for every repository in a comparison:

   ```bash
   python3 scripts/repo_assessments.py /path/to/repo-a /path/to/repo-b \
     --out ./assessment-report --max-commits 500
   ```

   Add `--since YYYY-MM-DD` when recency matters. Do not mix different windows in one comparison
   without labeling the difference.

3. Read the outputs in this order:

   | File | Use |
   |---|---|
   | `assessment.md` | Portfolio evidence, blockers, and candidate changes |
   | `repository_summary.csv` | One comparable evidence row per repository |
   | `candidate_changes.csv` | Base/head inventory for filtering and handoff |
   | `portfolio_assessment.json` | Full evidence, limitations, and candidate records |

4. Explain evidence, coverage limits, and blockers without ranking repositories or candidates.

5. Dynamically validate selected test-touching candidates before calling them reproducible. For
   each selected row, run the sibling verification harness with its exact base and head:

   ```bash
   python3 ../repo-verify/scripts/verify_env.py /path/to/repo \
     --base BASE_SHA --head HEAD_SHA
   ```

   Record environment failures separately from already-passing tests and genuine fail-to-pass
   transitions. Static evidence is prioritization input, not execution evidence.

## Reporting rules

- Lead with observed evidence, execution blockers, and coverage limitations.
- Preserve base and head SHAs so every candidate is auditable and reproducible.
- Treat issue linkage and descriptive commit text as lower bounds because local history may omit
  code-host discussions and review metadata.
- Separate candidates that require databases, browsers, credentials, network access, or other
  services from hermetic candidates.
- Treat licensing as a distribution constraint requiring review, not a judgment about code
  quality.
- Do not infer reproducibility from a changed test filename. Only an observed base-fail/head-pass
  execution establishes that transition.
