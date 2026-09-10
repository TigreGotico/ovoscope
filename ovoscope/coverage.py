# Copyright 2024 Jarbas AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Ecosystem-wide E2E test coverage scanner for ovoscope.

Scans a workspace root for Python packages that expose OVOS plugin entry
points and reports which ones have ``test/end2end/`` directories with at
least one ``.py`` test file.

Example::

    from ovoscope.coverage import scan_workspace

    report = scan_workspace("/path/to/OpenVoiceOS Workspace/")
    report.print_table()
    # → prints a table of repos, types, entry-points, and coverage status

CLI usage::

    ovoscope coverage /path/to/OpenVoiceOS/
    ovoscope coverage /path/to/OpenVoiceOS/ --format json
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ovos_utils.log import LOG


# Entry-point groups that indicate the repo type.
# Includes both legacy `ovos.plugin.*` groups (used in setup.py repos) and
# newer `opm.*` groups (used in pyproject.toml repos).
_GROUP_TO_TYPE: Dict[str, str] = {
    # Legacy groups (setup.py)
    "ovos.plugin.skill": "skill",
    "ovos.plugin.phal": "phal",
    "ovos.plugin.phal.admin": "phal",
    "ovos.plugin.tts": "tts",
    "ovos.plugin.stt": "stt",
    "ovos.plugin.audio": "audio",
    # Modern OPM groups (pyproject.toml)
    "opm.skill": "skill",
    "opm.pipeline": "pipeline",
    "opm.phal": "phal",
    "opm.plugin.tts": "tts",
    "opm.plugin.stt": "stt",
    "opm.plugin.audio": "audio",
    "opm.common_play": "ocp",
    "opm.solver": "solver",
}


@dataclass
class RepoCoverage:
    """Coverage information for a single repository.

    Attributes:
        path: Absolute path to the repository root.
        name: Repository name (derived from the directory name).
        repo_type: Plugin/skill type (``skill``, ``pipeline``, ``phal``, etc.).
        has_e2e_tests: True if ``test/end2end/`` contains at least one ``.py`` file.
        fixture_count: Number of ``.json`` fixture files in ``test/end2end/`` and ``test/fixtures/``.
        entry_points: List of entry-point IDs declared in ``pyproject.toml``.
    """

    path: str
    name: str
    repo_type: str
    has_e2e_tests: bool
    fixture_count: int = 0
    entry_points: List[str] = field(default_factory=list)


@dataclass
class EcosystemCoverageReport:
    """Aggregated coverage report for an entire workspace.

    Attributes:
        repos: Per-repository coverage entries.
        scan_root: The workspace root that was scanned.
    """

    repos: List[RepoCoverage] = field(default_factory=list)
    scan_root: str = ""
    # (path, reason) for every manifest that could not be parsed. A malformed
    # pyproject.toml used to be swallowed, which silently understated coverage.
    parse_errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        """Percentage of repos that have at least one E2E test.

        Returns:
            Float in [0.0, 100.0].
        """
        if not self.repos:
            return 0.0
        covered = sum(1 for r in self.repos if r.has_e2e_tests)
        return round(covered / len(self.repos) * 100, 1)

    def print_table(self) -> None:
        """Print a formatted coverage table to stdout."""
        col_name = max((len(r.name) for r in self.repos), default=20)
        col_name = max(col_name, 20)
        col_type = 10
        col_eps = max((len(", ".join(r.entry_points)) for r in self.repos), default=30)
        col_eps = min(max(col_eps, 30), 60)

        header = (
            f"{'Repository':<{col_name}}  {'Type':<{col_type}}  "
            f"{'Entry Points':<{col_eps}}  {'E2E Tests':>9}  {'Fixtures':>8}"
        )
        print(f"\nWorkspace: {self.scan_root}")
        print(header)
        print("-" * len(header))

        for r in sorted(self.repos, key=lambda x: (x.repo_type, x.name)):
            eps = ", ".join(r.entry_points) or "—"
            if len(eps) > col_eps:
                eps = eps[: col_eps - 3] + "..."
            status = "YES" if r.has_e2e_tests else "NO"
            print(
                f"{r.name:<{col_name}}  {r.repo_type:<{col_type}}  "
                f"{eps:<{col_eps}}  {status:>9}  {r.fixture_count:>8}"
            )

        print("-" * len(header))
        print(f"Total: {len(self.repos)} repos  |  Coverage: {self.coverage_pct}%")
        covered = sum(1 for r in self.repos if r.has_e2e_tests)
        print(f"With E2E tests: {covered}/{len(self.repos)}")

    def to_json(self) -> Dict[str, Any]:
        """Serialise the report to a JSON-compatible dict.

        Returns:
            Dictionary with ``scan_root``, ``coverage_pct``, and ``repos``.
        """
        return {
            "scan_root": self.scan_root,
            "coverage_pct": self.coverage_pct,
            "repos": [
                {
                    "path": r.path,
                    "name": r.name,
                    "repo_type": r.repo_type,
                    "has_e2e_tests": r.has_e2e_tests,
                    "fixture_count": r.fixture_count,
                    "entry_points": r.entry_points,
                }
                for r in self.repos
            ],
        }


_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", "dist", "build"}


def _find_pyproject_tomls(root: str) -> List[str]:
    """Walk *root* and return all ``pyproject.toml`` paths (max depth 3).

    Args:
        root: Workspace root directory to scan.

    Returns:
        Sorted list of absolute ``pyproject.toml`` paths found.
    """
    results: List[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Limit depth to avoid traversing deep virtualenv trees
        rel = os.path.relpath(dirpath, root)
        depth = len(rel.split(os.sep)) if rel != "." else 0
        if depth > 3:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if "pyproject.toml" in filenames:
            results.append(os.path.join(dirpath, "pyproject.toml"))
    return sorted(results)


def _find_setup_pys(root: str) -> List[str]:
    """Walk *root* and return all ``setup.py`` paths that are NOT alongside a ``pyproject.toml``.

    This avoids double-counting repos that have both files.

    Args:
        root: Workspace root directory to scan.

    Returns:
        Sorted list of absolute ``setup.py`` paths found.
    """
    results: List[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = len(rel.split(os.sep)) if rel != "." else 0
        if depth > 3:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if "setup.py" in filenames and "pyproject.toml" not in filenames:
            results.append(os.path.join(dirpath, "setup.py"))
    return sorted(results)


def _parse_setup_py_entry_points(setup_py_path: str,
                                 errors: Optional[List[Tuple[str, str]]] = None) -> Dict[str, List[str]]:
    """Extract entry-point groups from a ``setup.py`` file via regex.

    Detects lines like::

        entry_points={'ovos.plugin.skill': ...}
        entry_points={'ovos.plugin.phal': ...}

    Because many OVOS ``setup.py`` files compute the entry-point ID
    dynamically from the GitHub URL (using f-strings), we cannot always
    evaluate the ID statically.  When the ID cannot be determined, the
    directory name is used as a fallback identifier.

    Args:
        setup_py_path: Absolute path to ``setup.py``.

    Returns:
        Mapping of entry-point group name → list of entry-point IDs.
    """
    import re

    eps: Dict[str, List[str]] = {}
    repo_name = os.path.basename(os.path.dirname(setup_py_path))

    try:
        with open(setup_py_path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        _record_error(errors, setup_py_path, f"unreadable: {exc}")
        return eps

    # --- Pass 1: collect literal string assignments for ENTRY_POINT variables ---
    ep_var_map: Dict[str, str] = {}
    for m in re.finditer(r"(\w+ENTRY_POINT\w*)\s*=\s*['\"]([^'\"]+)['\"]", source):
        varname, value = m.group(1), m.group(2)
        ep_var_map[varname] = value.split("=")[0].strip()

    # --- Pass 2: collect URL = '...' to derive skill name ---
    url_match = re.search(r"""URL\s*=\s*['"]([^'"]+)['"]""", source)
    skill_name_from_url: Optional[str] = None
    if url_match:
        url = url_match.group(1)
        # e.g. https://github.com/OpenVoiceOS/ovos-skill-alerts → ovos-skill-alerts
        parts = url.rstrip("/").split("/")
        if parts:
            skill_name_from_url = parts[-1].lower()

    # --- Pass 3: find entry_points={'group': ...} declarations ---
    for m in re.finditer(
        r"entry_points\s*=\s*\{['\"]([^'\"]+)['\"]\s*:\s*([^}]+)\}",
        source,
        re.DOTALL,
    ):
        group = m.group(1)
        value_expr = m.group(2).strip()

        ep_ids: List[str] = []

        # Direct literal string
        for s in re.finditer(r"['\"]([^'\"=]+=[^'\"]+)['\"]", value_expr):
            ep_id = s.group(1).split("=")[0].strip()
            if ep_id:
                ep_ids.append(ep_id)

        # Variable reference with known value
        for varname, ep_id in ep_var_map.items():
            if varname in value_expr and ep_id not in ep_ids:
                ep_ids.append(ep_id)

        # Fallback: derive from URL or directory name
        if not ep_ids:
            fallback = skill_name_from_url or repo_name
            ep_ids.append(fallback)

        eps.setdefault(group, []).extend(ep_ids)

    return eps


def _parse_entry_points(pyproject_path: str,
                        errors: Optional[List[Tuple[str, str]]] = None) -> Dict[str, List[str]]:
    """Parse entry-point groups from a ``pyproject.toml`` file.

    Uses stdlib ``tomllib`` (Python 3.11+) or falls back to line-by-line
    parsing to avoid adding a ``tomli`` dependency.

    Args:
        pyproject_path: Absolute path to ``pyproject.toml``.

    Returns:
        Mapping of entry-point group name → list of entry-point IDs.
    """
    eps: Dict[str, List[str]] = {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            with open(pyproject_path, "rb") as fh:
                data = tomllib.load(fh)
            # setuptools style
            for group, entries in data.get("project", {}).get(
                    "entry-points", {}).items():
                eps[group] = list(entries.keys())
            return eps
        except tomllib.TOMLDecodeError as exc:
            # A malformed manifest is a real problem — record it instead of
            # silently reporting the repo as having no entry points.
            _record_error(errors, pyproject_path, f"invalid TOML: {exc}")
        except OSError as exc:
            _record_error(errors, pyproject_path, f"unreadable: {exc}")
            return eps

    # Fallback: simple line-by-line scan for entry-point group headers
    eps = {}
    current_group: Optional[str] = None
    try:
        with open(pyproject_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("[project.entry-points."):
                    current_group = line.split('"')[1] if '"' in line else line.split("'")[1]
                    eps[current_group] = []
                elif current_group and "=" in line and not line.startswith("["):
                    key = line.split("=")[0].strip().strip('"')
                    if key:
                        eps[current_group].append(key)
                elif line.startswith("["):
                    current_group = None
    except (OSError, IndexError, UnicodeDecodeError) as exc:
        _record_error(errors, pyproject_path, f"fallback scan failed: {exc}")
    return eps


def _record_error(errors: Optional[List[Tuple[str, str]]],
                  path: str, reason: str) -> None:
    """Append a parse failure to *errors* and log it."""
    LOG.warning("ovoscope coverage: %s — %s", path, reason)
    if errors is not None:
        errors.append((path, reason))


def _has_e2e_tests(repo_root: str) -> bool:
    """Check if *repo_root* has ``test/end2end/`` with at least one ``.py`` file.

    Args:
        repo_root: Repository root directory.

    Returns:
        True if E2E test files exist, False otherwise.
    """
    candidates = [
        os.path.join(repo_root, "test", "end2end"),
        os.path.join(repo_root, "tests", "end2end"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            py_files = [f for f in os.listdir(candidate) if f.endswith(".py") and f != "__init__.py"]
            if py_files:
                return True
    return False


def _count_fixtures(repo_root: str) -> int:
    """Count ``.json`` fixture files in common fixture directories (recursive).

    Searches recursively under each candidate directory so that fixtures
    organised in sub-directories (e.g. ``test/end2end/skill_name/*.json``)
    are counted correctly.

    Args:
        repo_root: Repository root directory.

    Returns:
        Total count of JSON fixture files found.
    """
    from pathlib import Path
    count = 0
    candidate_names = [
        ("test", "end2end"),
        ("tests", "end2end"),
        ("test", "fixtures"),
        ("tests", "fixtures"),
    ]
    for parts in candidate_names:
        candidate = Path(repo_root).joinpath(*parts)
        if candidate.is_dir():
            count += sum(1 for _ in candidate.rglob("*.json"))
    return count


def _collect_repo(
    repo_root: str,
    entry_point_groups: Dict[str, List[str]],
) -> Optional[RepoCoverage]:
    """Build a :class:`RepoCoverage` entry if *entry_point_groups* contains known OVOS groups.

    Args:
        repo_root: Absolute path to the repository root.
        entry_point_groups: Parsed entry-point groups → IDs mapping.

    Returns:
        A :class:`RepoCoverage` instance, or ``None`` if no recognised group found.
    """
    repo_type: Optional[str] = None
    ep_ids: List[str] = []
    for group, ids in entry_point_groups.items():
        if group in _GROUP_TO_TYPE:
            repo_type = _GROUP_TO_TYPE[group]
            ep_ids.extend(ids)

    if repo_type is None:
        return None

    # Deduplicate entry-point IDs
    seen: set = set()
    unique_ids = [i for i in ep_ids if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]

    return RepoCoverage(
        path=repo_root,
        name=os.path.basename(repo_root),
        repo_type=repo_type,
        has_e2e_tests=_has_e2e_tests(repo_root),
        fixture_count=_count_fixtures(repo_root),
        entry_points=unique_ids,
    )


def scan_workspace(root: str) -> EcosystemCoverageReport:
    """Scan *root* for OVOS plugin repos and report E2E test coverage.

    Detection: any ``pyproject.toml`` or ``setup.py`` that declares at least
    one entry point in a recognised OVOS group is included in the report.
    Repos with both files are counted once (``pyproject.toml`` takes precedence).

    Args:
        root: Workspace root directory to scan.

    Returns:
        An :class:`EcosystemCoverageReport` with one entry per discovered repo.
    """
    root = os.path.abspath(root)
    report = EcosystemCoverageReport(scan_root=root)
    seen_roots: set = set()

    # --- pyproject.toml repos ---
    for pyproject_path in _find_pyproject_tomls(root):
        repo_root = os.path.dirname(pyproject_path)
        entry_point_groups = _parse_entry_points(pyproject_path,
                                                 report.parse_errors)
        cov = _collect_repo(repo_root, entry_point_groups)
        if cov is not None:
            report.repos.append(cov)
            seen_roots.add(repo_root)

    # --- setup.py repos (not already covered by pyproject.toml) ---
    for setup_py_path in _find_setup_pys(root):
        repo_root = os.path.dirname(setup_py_path)
        if repo_root in seen_roots:
            continue
        entry_point_groups = _parse_setup_py_entry_points(setup_py_path,
                                                         report.parse_errors)
        cov = _collect_repo(repo_root, entry_point_groups)
        if cov is not None:
            report.repos.append(cov)
            seen_roots.add(repo_root)

    return report
