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
"""ovoscope-setup — install the ovoscope skill into AI coding assistants.

Supports Claude Code and Gemini CLI.  All skill files (SKILL.md, docs,
FAQ.md, QUICK_FACTS.md) are downloaded from the ovoscope GitHub repository
at install time.  The only thing generated locally is the tiny shell wrapper
that invokes the ``ovoscope`` CLI.

Nothing is bundled in the wheel beyond this module itself, so the package
stays small and the installed skill always reflects the current docs.

Usage::

    ovoscope-setup                     # auto-detect and install all
    ovoscope-setup --claude            # Claude Code only
    ovoscope-setup --gemini            # Gemini CLI only (project-level)
    ovoscope-setup --gemini --path /my/workspace
    ovoscope-setup --list              # show detected tools without installing
    ovoscope-setup --no-docs           # skip docs download (offline / CI)
    ovoscope-setup --uninstall --claude
"""
from __future__ import annotations

import argparse
import shutil
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# GitHub source URLs
# ---------------------------------------------------------------------------

#: Raw-content base for the ovoscope master branch.
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/TigreGotico/ovoscope/master"
)

#: SKILL.md is shared between Claude and Gemini — same file, same format.
_SKILL_MD_URL = f"{GITHUB_RAW_BASE}/SKILL.md"

#: Docs files to download into ``assets/docs/``.
_DOCS_FILES = [
    "docs/audio-testing.md",
    "docs/bus-coverage.md",
    "docs/capture-session.md",
    "docs/ci-integration.md",
    "docs/cli.md",
    "docs/e2e-pipeline-harness.md",
    "docs/end2end-test.md",
    "docs/gui-testing.md",
    "docs/index.md",
    "docs/intent-cases.md",
    "docs/listener.md",
    "docs/media-provider-testing.md",
    "docs/media-testing.md",
    "docs/minicroft.md",
    "docs/ocp.md",
    "docs/phal.md",
    "docs/pipeline.md",
    "docs/pydantic-integration.md",
    "docs/usage-guide.md",
    "docs/voice-loop.md",
]

#: Root-level files to download into ``assets/``.
_ASSET_FILES = [
    "FAQ.md",
    "QUICK_FACTS.md",
]

#: Shell wrapper written into ``scripts/``.  Just delegates to the CLI.
_WRAPPER_SCRIPT = "#!/bin/bash\nexec ovoscope \"$@\"\n"


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _fetch(url: str, dest: Path, verbose: bool = True) -> bool:
    """Download *url* to *dest*.

    Args:
        url: Full URL to fetch.
        dest: Destination file path (parent must exist).
        verbose: Print per-file status.

    Returns:
        True on success, False on any error (warning is printed to stderr).
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            dest.write_bytes(resp.read())
        if verbose:
            print(f"  ↓ {dest.name}")
        return True
    except (urllib.error.URLError, OSError) as exc:
        print(f"  ! skipped {dest.name}: {exc}", file=sys.stderr)
        return False


def download_docs(assets_dir: Path, verbose: bool = True) -> int:
    """Download ovoscope docs from GitHub into *assets_dir*.

    Downloads :data:`_DOCS_FILES` into ``assets_dir/docs/`` and
    :data:`_ASSET_FILES` into ``assets_dir/``.  Individual failures are
    warned and skipped — they do not abort the install.

    Args:
        assets_dir: Destination ``assets/`` directory (created if absent).
        verbose: Print progress messages.

    Returns:
        Number of files successfully downloaded.
    """
    downloaded = 0

    docs_dst = assets_dir / "docs"
    docs_dst.mkdir(parents=True, exist_ok=True)
    for rel_path in _DOCS_FILES:
        if _fetch(f"{GITHUB_RAW_BASE}/{rel_path}", docs_dst / Path(rel_path).name, verbose):
            downloaded += 1

    assets_dir.mkdir(parents=True, exist_ok=True)
    for rel_path in _ASSET_FILES:
        if _fetch(f"{GITHUB_RAW_BASE}/{rel_path}", assets_dir / rel_path, verbose):
            downloaded += 1

    return downloaded


# ---------------------------------------------------------------------------
# Shared install helpers
# ---------------------------------------------------------------------------

def _install_skill(
    skill_dir: Path,
    tool_name: str,
    fetch_docs: bool,
    verbose: bool,
) -> bool:
    """Create a skill directory, download SKILL.md, write the wrapper script.

    Args:
        skill_dir: Target directory for the skill.
        tool_name: Display name for log messages (e.g. ``"claude"``).
        fetch_docs: Whether to download docs into ``assets/``.
        verbose: Print progress messages.

    Returns:
        True on success, False when SKILL.md could not be downloaded — the
        skill is useless without it, so the caller must report the failure
        instead of leaving a half-installed directory behind.
    """
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # SKILL.md — downloaded from GitHub
    skill_md = skill_dir / "SKILL.md"
    if verbose:
        print(f"[{tool_name}]  downloading SKILL.md …")
    if not _fetch(_SKILL_MD_URL, skill_md, verbose=False):
        print(f"[{tool_name}]  ERROR: could not download SKILL.md from "
              f"{_SKILL_MD_URL} — the skill was NOT installed.",
              file=sys.stderr)
        return False
    if verbose:
        print(f"[{tool_name}]  SKILL.md → {skill_md}")

    # Wrapper script — generated inline
    wrapper = scripts_dir / "ovoscope.sh"
    wrapper.write_text(_WRAPPER_SCRIPT)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if fetch_docs:
        if verbose:
            print(f"[{tool_name}]  downloading docs from GitHub …")
        n = download_docs(skill_dir / "assets", verbose=verbose)
        if verbose:
            print(f"[{tool_name}]  {n} doc files downloaded")

    if verbose:
        print(f"[{tool_name}]  installed → {skill_dir}")
    return True


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _claude_skill_dir() -> Path:
    """Return ``~/.claude/skills/ovoscope``."""
    return Path.home() / ".claude" / "skills" / "ovoscope"


def _claude_is_installed() -> bool:
    """Return True if the ``claude`` binary is on PATH."""
    return shutil.which("claude") is not None


def install_claude(fetch_docs: bool = True, verbose: bool = True) -> bool:
    """Install the ovoscope skill for Claude Code.

    Downloads ``SKILL.md`` from GitHub, writes the wrapper script, and
    optionally downloads docs into ``~/.claude/skills/ovoscope/assets/``.

    Args:
        fetch_docs: Download docs from GitHub (default True).
        verbose: Print progress messages.

    Returns:
        True on success.
    """
    return _install_skill(_claude_skill_dir(), "claude", fetch_docs, verbose)


def uninstall_claude(verbose: bool = True) -> bool:
    """Remove the ovoscope skill from Claude Code.

    Args:
        verbose: Print progress messages.

    Returns:
        True if removed, False if not found.
    """
    dst = _claude_skill_dir()
    if dst.exists():
        shutil.rmtree(dst)
        if verbose:
            print(f"[claude]  removed {dst}")
        return True
    if verbose:
        print(f"[claude]  not installed ({dst} not found)")
    return False


# ---------------------------------------------------------------------------
# Gemini CLI
# ---------------------------------------------------------------------------

def _gemini_skill_dir(project_path: Optional[Path] = None) -> Path:
    """Return ``<project>/.gemini/skills/ovoscope``.

    Args:
        project_path: Project root. Defaults to ``Path.cwd()``.
    """
    return (project_path or Path.cwd()) / ".gemini" / "skills" / "ovoscope"


def _gemini_is_installed() -> bool:
    """Return True if the ``gemini`` binary is on PATH."""
    return shutil.which("gemini") is not None


def install_gemini(
    project_path: Optional[Path] = None,
    fetch_docs: bool = True,
    verbose: bool = True,
) -> bool:
    """Install the ovoscope skill for Gemini CLI.

    Downloads ``SKILL.md`` from GitHub, writes the wrapper script, and
    optionally downloads docs into ``<project>/.gemini/skills/ovoscope/assets/``.

    Gemini skills are project-level.  Run from your workspace root or pass
    *project_path* explicitly.

    Args:
        project_path: Project root. Defaults to ``Path.cwd()``.
        fetch_docs: Download docs from GitHub (default True).
        verbose: Print progress messages.

    Returns:
        True on success.
    """
    return _install_skill(
        _gemini_skill_dir(project_path), "gemini", fetch_docs, verbose
    )


def uninstall_gemini(
    project_path: Optional[Path] = None,
    verbose: bool = True,
) -> bool:
    """Remove the ovoscope skill from Gemini CLI.

    Args:
        project_path: Project root. Defaults to ``Path.cwd()``.
        verbose: Print progress messages.

    Returns:
        True if removed, False if not found.
    """
    dst = _gemini_skill_dir(project_path)
    if dst.exists():
        shutil.rmtree(dst)
        if verbose:
            print(f"[gemini]  removed {dst}")
        return True
    if verbose:
        print(f"[gemini]  not installed ({dst} not found)")
    return False


# ---------------------------------------------------------------------------
# Auto-detect
# ---------------------------------------------------------------------------

def detect_tools() -> List[str]:
    """Return names of AI tools whose binaries are on PATH.

    Returns:
        Subset of ``["claude", "gemini"]``.
    """
    found = []
    if _claude_is_installed():
        found.append("claude")
    if _gemini_is_installed():
        found.append("gemini")
    return found


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``ovoscope-setup`` command.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 = success, 1 = nothing installed.
    """
    parser = argparse.ArgumentParser(
        prog="ovoscope-setup",
        description=(
            "Install the ovoscope skill into AI coding assistants.\n\n"
            "Everything (SKILL.md, docs, FAQ.md) is downloaded from GitHub\n"
            "at install time — nothing extra is bundled in the wheel.\n\n"
            "Run without flags to auto-detect and install for all tools on PATH.\n"
            "Gemini installs are project-level (current directory or --path)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="Install for Claude Code (~/.claude/skills/ovoscope/).",
    )
    parser.add_argument(
        "--gemini",
        action="store_true",
        help="Install for Gemini CLI (<path>/.gemini/skills/ovoscope/).",
    )
    parser.add_argument(
        "--path",
        metavar="DIR",
        default=None,
        help="Project root for Gemini install (default: current directory).",
    )
    parser.add_argument(
        "--no-docs",
        action="store_true",
        dest="no_docs",
        help="Skip downloading documentation from GitHub.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the skill instead of installing it.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="Show which tools are detected without installing anything.",
    )

    args = parser.parse_args(argv)
    project_path = Path(args.path).resolve() if args.path else None
    fetch_docs = not args.no_docs

    if args.list_only:
        found = detect_tools()
        print(
            "Detected AI tools on PATH: " + ", ".join(found)
            if found else
            "No supported AI tools detected on PATH (claude, gemini)."
        )
        return 0

    explicit = args.claude or args.gemini
    if not explicit:
        found = detect_tools()
        if not found:
            print(
                "No supported AI tools detected on PATH.\n"
                "Install claude or gemini first, or use --claude/--gemini explicitly."
            )
            return 1
        args.claude = "claude" in found
        args.gemini = "gemini" in found
        print(f"Auto-detected: {', '.join(found)}")

    if args.uninstall:
        if args.claude:
            uninstall_claude()
        if args.gemini:
            uninstall_gemini(project_path)
        return 0

    ok = True
    if args.claude:
        ok = install_claude(fetch_docs=fetch_docs) and ok
    if args.gemini:
        ok = install_gemini(project_path, fetch_docs=fetch_docs) and ok

    if not ok:
        print("\nInstallation FAILED. See the errors above.", file=sys.stderr)
        return 1

    print(
        "\nInstallation complete. Restart your AI assistant or open a new\n"
        "session for the ovoscope skill to be available."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
