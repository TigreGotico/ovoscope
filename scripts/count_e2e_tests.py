#!/usr/bin/env python3
"""Deterministic static sweep of ovoscope end-to-end test functions across the
OpenVoiceOS ovos-skill-* fleet, for an NGI0 grant artifact.

COUNTING RULE (exact, no exceptions):

1. Repo set is enumerated FROM THE GITHUB API, not hardcoded: every
   non-archived repository in the OpenVoiceOS org whose name starts with
   "ovos-skill-". Fetched with:

     gh api --paginate 'orgs/OpenVoiceOS/repos?per_page=100' \
         -q '.[] | select(.archived==false) | select(.name|startswith("ovos-skill-")) | .name'

   fully paginated. The COUNT of repos swept is reported.

2. For each repo, the DEFAULT branch is read from the API
   (GET repos/OpenVoiceOS/<repo> -> .default_branch), and that branch's
   recursive git tree is fetched:

     gh api "repos/OpenVoiceOS/<repo>/git/trees/<default_branch>?recursive=1"

   Blob entries whose path contains "test/end2end/" and end in ".py" are
   selected as the primary end-to-end test files for that repo.

3. Each selected file's content is fetched via the contents API and
   base64-decoded. The number of end-to-end test FUNCTIONS in the file is
   the count of matches of the regex ``^\\s*def test_`` with re.MULTILINE
   over the decoded text -- this counts function/method DEFINITIONS
   (including methods nested in a class), not parametrized-test expansions,
   and not calls. Per-repo primary count is the sum over its test/end2end/
   files.

4. If a repo has NO test/end2end/ directory but has files elsewhere under
   test/ (or tests/) that reference ovoscope's CaptureSession or
   get_minicroft, that is recorded as an ALTERNATE path with its own
   "def test_" count, flagged separately. Alternate counts are NEVER folded
   into the primary total; the primary TOTAL sums test/end2end/ files only.

5. Output ordering is deterministic: repos are sorted by name, and each
   repo's matched file paths are sorted before content is fetched and
   summed.

Requires only the stdlib and the `gh` CLI (authenticated) on PATH. No pytest
import, no pip dependencies -- this is a static text/regex scan, not a test
collection run.

Usage:
    nice -n 19 ionice -c3 taskset -c 0,1 python3 count_e2e_tests.py

Writes:
    repos.json        -- raw repo list swept (name, default_branch)
    per_repo.json      -- full structured results
    report.md          -- the markdown table + footer required by the task
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

COUNTING_RULE = (
    "Counting rule: def test_ definitions under test/end2end/ on each repo's "
    "default branch (static scan)."
)

ORG = "OpenVoiceOS"
OUT_DIR = Path(__file__).resolve().parent
TEST_DEF_RE = re.compile(r"^\s*def test_", re.MULTILINE)
ALT_MARKERS = ("CaptureSession", "get_minicroft")


def run_gh(args: list[str]) -> str:
    """Run a gh CLI command capped by the caller's nice/ionice/taskset wrapper
    and return stdout. Raises on non-zero exit."""
    proc = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def list_skill_repos() -> list[str]:
    """Enumerate all non-archived ovos-skill-* repos in the org, fully paginated."""
    out = run_gh(
        [
            "api",
            "--paginate",
            f"orgs/{ORG}/repos?per_page=100",
            "-q",
            '.[] | select(.archived==false) | select(.name|startswith("ovos-skill-")) | .name',
        ]
    )
    names = sorted({line.strip() for line in out.splitlines() if line.strip()})
    return names


def get_default_branch(repo: str) -> str:
    out = run_gh(["api", f"repos/{ORG}/{repo}", "-q", ".default_branch"])
    branch = out.strip()
    if not branch:
        raise RuntimeError(f"could not determine default branch for {repo}")
    return branch


def get_tree(repo: str, branch: str) -> list[dict]:
    """Fetch the recursive git tree for the default branch. Returns [] if the
    branch/tree cannot be resolved (e.g. empty repo)."""
    try:
        out = run_gh(
            ["api", f"repos/{ORG}/{repo}/git/trees/{branch}?recursive=1"]
        )
    except RuntimeError:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data.get("tree", [])


def get_file_text(repo: str, path: str) -> str:
    out = run_gh(["api", f"repos/{ORG}/{repo}/contents/{path}", "-q", ".content"])
    b64 = "".join(out.split())  # gh may wrap/newline the base64
    if not b64:
        return ""
    raw = base64.b64decode(b64)
    return raw.decode("utf-8", errors="replace")


def count_test_defs(text: str) -> int:
    return len(TEST_DEF_RE.findall(text))


def analyze_repo(repo: str) -> dict:
    branch = get_default_branch(repo)
    tree = get_tree(repo, branch)

    blob_paths = sorted(
        e["path"] for e in tree if e.get("type") == "blob" and "path" in e
    )

    primary_paths = [
        p for p in blob_paths if "test/end2end/" in p and p.endswith(".py")
    ]

    primary_count = 0
    primary_files = []
    for p in primary_paths:
        text = get_file_text(repo, p)
        n = count_test_defs(text)
        primary_files.append({"path": p, "count": n})
        primary_count += n

    alt_path = None
    alt_count = None
    if not primary_paths:
        # look for test/ or tests/ files elsewhere that reference ovoscope
        candidate_paths = sorted(
            p
            for p in blob_paths
            if p.endswith(".py")
            and (p.startswith(("test/", "tests/")) or "/test/" in p or "/tests/" in p)
        )
        alt_files = []
        alt_total = 0
        for p in candidate_paths:
            text = get_file_text(repo, p)
            if any(marker in text for marker in ALT_MARKERS):
                n = count_test_defs(text)
                alt_files.append({"path": p, "count": n})
                alt_total += n
        if alt_files:
            # represent alt_path as the common directory, or list if mixed
            dirs = sorted({str(Path(f["path"]).parent) for f in alt_files})
            alt_path = ", ".join(dirs)
            alt_count = alt_total

    return {
        "repo": repo,
        "default_branch": branch,
        "path_scanned": "test/end2end/" if primary_paths else "(none)",
        "primary_files": primary_files,
        "e2e_test_count": primary_count,
        "alt_path": alt_path,
        "alt_count": alt_count,
    }


def build_report(repos: list[str], results: list[dict]) -> str:
    """Render the self-describing header + per-repo table + footer. Pure
    function of already-computed results -- never re-fetches."""
    results = sorted(results, key=lambda r: r["repo"])
    total = sum(r["e2e_test_count"] for r in results)
    swept = len(repos)
    run_date = datetime.now(timezone.utc).date().isoformat()

    lines = []
    lines.append(f"Run date (UTC): {run_date}")
    lines.append(f"Repos swept: {swept}")
    lines.append(COUNTING_RULE)
    lines.append("")
    lines.append("| repo | default_branch | path_scanned | e2e_test_count | alt_path (alt_count) |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        alt = f"{r['alt_path']} ({r['alt_count']})" if r.get("alt_path") else ""
        lines.append(
            f"| {r['repo']} | {r['default_branch']} | {r['path_scanned']} | {r['e2e_test_count']} | {alt} |"
        )
    lines.append("")
    lines.append(f"**TOTAL across test/end2end/: {total}**")
    lines.append("")
    lines.append(f"**REPOS SWEPT: {swept}**")

    return "\n".join(lines) + "\n"


def main() -> None:
    repos = list_skill_repos()
    results = []
    for repo in repos:
        print(f"scanning {repo} ...", file=sys.stderr)
        try:
            results.append(analyze_repo(repo))
        except RuntimeError as exc:
            print(f"  ERROR on {repo}: {exc}", file=sys.stderr)
            results.append(
                {
                    "repo": repo,
                    "default_branch": "ERROR",
                    "path_scanned": "ERROR",
                    "primary_files": [],
                    "e2e_test_count": 0,
                    "alt_path": None,
                    "alt_count": None,
                    "error": str(exc),
                }
            )

    results.sort(key=lambda r: r["repo"])

    (OUT_DIR / "repos.json").write_text(json.dumps(repos, indent=2) + "\n")
    (OUT_DIR / "per_repo.json").write_text(json.dumps(results, indent=2) + "\n")

    report = build_report(repos, results)
    (OUT_DIR / "report.md").write_text(report)
    print(report)
    total = sum(r["e2e_test_count"] for r in results)
    print(f"TOTAL={total} REPOS_SWEPT={len(repos)}", file=sys.stderr)


if __name__ == "__main__":
    main()
