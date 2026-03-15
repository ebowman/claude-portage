#!/usr/bin/env python3
"""claude-portage: Portable Claude Code workspace archives.

Bundles a project + its Claude Code metadata (~/.claude/) into a portable
archive that can be unpacked anywhere with automatic path rewriting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

__version__ = "0.2.1"

# ---------------------------------------------------------------------------
# Path encoding / decoding
# ---------------------------------------------------------------------------

def encode_path(path: str) -> str:
    """Encode an absolute path into Claude's directory-name scheme.

    Claude Code replaces each ``/`` and ``.`` with ``-`` in the resolved
    absolute path.
    e.g. ``/Users/alice.name/src/foo`` → ``-Users-alice-name-src-foo``
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    # Replace path separators and dots with hyphens (matches Claude Code behaviour)
    encoded = resolved.replace(os.sep, "-").replace(".", "-")
    return encoded


def default_claude_dir() -> Path:
    """Return the default ``~/.claude`` directory."""
    return Path.home() / ".claude"


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def discover_session_ids(project_meta_dir: Path) -> List[str]:
    """Discover session IDs from a project's Claude metadata directory.

    Sessions are identified by ``<uuid>.jsonl`` files in the project dir.
    """
    session_ids: List[str] = []
    if not project_meta_dir.is_dir():
        return session_ids
    for item in project_meta_dir.iterdir():
        if item.suffix == ".jsonl" and item.stem != "sessions-index":
            session_ids.append(item.stem)
    return sorted(session_ids)


def discover_session_files(
    claude_dir: Path,
    project_meta_dir: Path,
    session_ids: List[str],
    include_debug: bool = False,
) -> Dict[str, List[Path]]:
    """Discover all files related to sessions.

    Returns a dict mapping category names to lists of absolute paths.
    """
    files: Dict[str, List[Path]] = {
        "project-meta": [],
        "file-history": [],
        "session-env": [],
        "todos": [],
        "plans": [],
        "debug": [],
    }

    # Everything under the project meta dir (JSONL, subagents, tool-results, memory)
    if project_meta_dir.is_dir():
        for root, dirs, filenames in os.walk(project_meta_dir):
            for fn in filenames:
                files["project-meta"].append(Path(root) / fn)

    # Per-session directories in other top-level Claude dirs
    for sid in session_ids:
        # file-history/<sessionId>/
        fh_dir = claude_dir / "file-history" / sid
        if fh_dir.is_dir():
            for root, dirs, filenames in os.walk(fh_dir):
                for fn in filenames:
                    files["file-history"].append(Path(root) / fn)

        # session-env/<sessionId>/
        se_dir = claude_dir / "session-env" / sid
        if se_dir.is_dir():
            for root, dirs, filenames in os.walk(se_dir):
                for fn in filenames:
                    files["session-env"].append(Path(root) / fn)

        # debug/<sessionId>.txt
        if include_debug:
            debug_file = claude_dir / "debug" / f"{sid}.txt"
            if debug_file.is_file():
                files["debug"].append(debug_file)

        # todos/<sessionId>-*.json
        todos_dir = claude_dir / "todos"
        if todos_dir.is_dir():
            for todo_file in todos_dir.glob(f"{sid}-*.json"):
                files["todos"].append(todo_file)

    # Discover referenced plans from JSONL content
    plans_dir = claude_dir / "plans"
    if plans_dir.is_dir():
        # Collect all plan slugs referenced in session JSONL files
        plan_slugs: Set[str] = set()
        for sid in session_ids:
            jsonl_file = project_meta_dir / f"{sid}.jsonl"
            if jsonl_file.is_file():
                try:
                    with open(jsonl_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            # Look for plan references like "plans/some-slug.md"
                            for match in re.finditer(r'plans/([a-zA-Z0-9_-]+\.md)', line):
                                plan_slugs.add(match.group(1))
                except OSError:
                    pass

        for slug in plan_slugs:
            plan_file = plans_dir / slug
            if plan_file.is_file():
                files["plans"].append(plan_file)

    return files


def collect_project_files(project_dir: Path) -> List[Path]:
    """Collect project files, preferring git ls-files if available."""
    project_dir = project_dir.resolve()
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(project_dir),
            capture_output=True,
            text=False,
        )
        if result.returncode == 0 and result.stdout:
            paths = []
            for entry in result.stdout.split(b"\x00"):
                if entry:
                    paths.append(project_dir / entry.decode("utf-8", errors="replace"))
            return sorted(paths)
    except FileNotFoundError:
        pass  # git not installed

    # Fallback: os.walk, skipping hidden dirs and common junk
    skip_dirs = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".tox", ".venv", "venv"}
    paths = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            paths.append(Path(root) / fn)
    return sorted(paths)


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

def build_replacement_map(
    source_project_path: str,
    target_project_path: str,
    source_claude_dir: str,
    target_claude_dir: str,
) -> List[Tuple[str, str]]:
    """Build a replacement map sorted longest-first to avoid partial matches.

    Includes both resolved (realpath) and unresolved variants to handle
    symlinks (e.g., macOS /var → /private/var).
    """
    source_encoded = encode_path(source_project_path)
    target_encoded = encode_path(target_project_path)

    replacements = [
        (source_project_path, target_project_path),
        (source_claude_dir, target_claude_dir),
        (source_encoded, target_encoded),
    ]

    # Also add unresolved variants if they differ from resolved
    source_project_real = os.path.realpath(source_project_path)
    source_claude_real = os.path.realpath(source_claude_dir)
    if source_project_real != source_project_path:
        replacements.append((source_project_real, target_project_path))
        replacements.append((encode_path(source_project_real), target_encoded))
    if source_claude_real != source_claude_dir:
        replacements.append((source_claude_real, target_claude_dir))

    # Deduplicate and sort longest-first
    seen = set()
    unique = []
    for old, new in replacements:
        if old != new and old not in seen:
            seen.add(old)
            unique.append((old, new))

    unique.sort(key=lambda x: len(x[0]), reverse=True)
    return unique


def rewrite_line(line: str, replacements: List[Tuple[str, str]]) -> str:
    """Apply all replacements to a single line (longest-first)."""
    for old, new in replacements:
        line = line.replace(old, new)
    return line


def is_text_file(path: Path) -> bool:
    """Heuristic check if a file is text (for path rewriting)."""
    text_suffixes = {
        ".json", ".jsonl", ".txt", ".md", ".yaml", ".yml",
        ".toml", ".cfg", ".ini", ".log", ".csv",
    }
    if path.suffix.lower() in text_suffixes:
        return True
    # Try reading a small chunk
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return False
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def create_manifest(
    source_project_path: str,
    source_claude_dir: str,
    session_ids: List[str],
    include_project_files: bool,
    include_debug: bool,
    source_project_path_unresolved: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the archive manifest."""
    m: Dict[str, Any] = {
        "version": 1,
        "portage_version": __version__,
        "source_project_path": source_project_path,
        "source_claude_dir": source_claude_dir,
        "source_encoded_path": encode_path(source_project_path),
        "session_ids": session_ids,
        "includes_project_files": include_project_files,
        "includes_debug": include_debug,
    }
    # Store unresolved path if it differs (e.g., /var vs /private/var on macOS)
    if source_project_path_unresolved and source_project_path_unresolved != source_project_path:
        m["source_project_path_unresolved"] = source_project_path_unresolved
    return m


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------

def cmd_pack(args: argparse.Namespace) -> int:
    """Pack a project and its Claude metadata into a portable archive."""
    project_dir_raw = Path(args.project_dir).expanduser().absolute()
    project_dir = project_dir_raw.resolve()
    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        return 1

    claude_dir = default_claude_dir()
    source_project_path = str(project_dir)
    # Track unresolved path for symlink handling (e.g., /var vs /private/var)
    source_project_path_unresolved = str(project_dir_raw) if str(project_dir_raw) != source_project_path else None
    source_encoded = encode_path(source_project_path)

    project_meta_dir = claude_dir / "projects" / source_encoded
    if not project_meta_dir.is_dir():
        print(
            f"Error: no Claude metadata found at {project_meta_dir}\n"
            f"  (encoded path: {source_encoded})",
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        print(f"Project dir:   {project_dir}")
        print(f"Claude dir:    {claude_dir}")
        print(f"Encoded path:  {source_encoded}")
        print(f"Metadata dir:  {project_meta_dir}")

    # Discover sessions
    session_ids = discover_session_ids(project_meta_dir)
    if args.verbose:
        print(f"Sessions:      {len(session_ids)}")

    # Discover all related files
    include_debug = getattr(args, "include_debug", False)
    session_files = discover_session_files(
        claude_dir, project_meta_dir, session_ids, include_debug=include_debug,
    )

    # Collect project files
    include_project = not getattr(args, "no_project_files", False)
    project_files = collect_project_files(project_dir) if include_project else []

    # Build manifest
    manifest = create_manifest(
        source_project_path=source_project_path,
        source_claude_dir=str(claude_dir),
        session_ids=session_ids,
        include_project_files=include_project,
        include_debug=include_debug,
        source_project_path_unresolved=source_project_path_unresolved,
    )

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path.cwd() / f"{project_dir.name}.portage.tar.gz"

    if args.verbose:
        print(f"Output:        {output_path}")

    # Build archive
    archive_prefix = f"{project_dir.name}.portage"

    with tarfile.open(str(output_path), "w:gz") as tar:
        # Write manifest
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        _add_bytes_to_tar(tar, f"{archive_prefix}/manifest.json", manifest_bytes)

        # Write project files
        file_count = 0
        if include_project:
            for pf in project_files:
                try:
                    rel = pf.relative_to(project_dir)
                    arcname = f"{archive_prefix}/project/{rel}"
                    tar.add(str(pf), arcname=arcname)
                    file_count += 1
                except (ValueError, OSError) as e:
                    if args.verbose:
                        print(f"  Skipping {pf}: {e}", file=sys.stderr)

        # Write Claude metadata
        meta_count = 0
        for category, paths in session_files.items():
            for fp in paths:
                try:
                    rel = fp.relative_to(claude_dir)
                    arcname = f"{archive_prefix}/claude-meta/{rel}"
                    tar.add(str(fp), arcname=arcname)
                    meta_count += 1
                except (ValueError, OSError) as e:
                    if args.verbose:
                        print(f"  Skipping {fp}: {e}", file=sys.stderr)

    total_meta = sum(len(v) for v in session_files.values())
    print(f"Packed {output_path}")
    print(f"  Sessions:       {len(session_ids)}")
    print(f"  Project files:  {file_count}")
    print(f"  Metadata files: {meta_count}")
    return 0


def _add_bytes_to_tar(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Add raw bytes as a file to a tar archive."""
    import io
    import time

    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------
# Unpack
# ---------------------------------------------------------------------------

def cmd_unpack(args: argparse.Namespace) -> int:
    """Unpack a portage archive to a target directory with path rewriting."""
    archive_path = Path(args.archive).resolve()
    if not archive_path.is_file():
        print(f"Error: archive not found: {archive_path}", file=sys.stderr)
        return 1

    target_dir = Path(args.target_dir).resolve()
    claude_dir = Path(args.claude_dir) if args.claude_dir else default_claude_dir()
    claude_dir = claude_dir.resolve()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Extract archive
        if args.verbose:
            print(f"Extracting to temp dir...")

        with tarfile.open(str(archive_path), "r:gz") as tar:
            # Use data filter on Python 3.12+ to avoid deprecation warning
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=str(tmpdir_path), filter="data")
            else:
                tar.extractall(path=str(tmpdir_path))

        # Find the archive root (first directory)
        archive_roots = [d for d in tmpdir_path.iterdir() if d.is_dir()]
        if len(archive_roots) != 1:
            print(f"Error: expected one root directory in archive, found {len(archive_roots)}", file=sys.stderr)
            return 1
        archive_root = archive_roots[0]

        # Read manifest
        manifest_path = archive_root / "manifest.json"
        if not manifest_path.is_file():
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        source_project_path = manifest["source_project_path"]
        source_claude_dir = manifest["source_claude_dir"]
        source_encoded = manifest["source_encoded_path"]
        target_project_path = str(target_dir)
        target_encoded = encode_path(target_project_path)

        if args.verbose:
            print(f"Source path:   {source_project_path}")
            print(f"Target path:   {target_project_path}")
            print(f"Source encoded: {source_encoded}")
            print(f"Target encoded: {target_encoded}")

        # Build replacement map
        replacements = build_replacement_map(
            source_project_path=source_project_path,
            target_project_path=target_project_path,
            source_claude_dir=source_claude_dir,
            target_claude_dir=str(claude_dir),
        )

        # Add unresolved path variant if present in manifest
        unresolved = manifest.get("source_project_path_unresolved")
        if unresolved and unresolved != source_project_path:
            extra = [
                (unresolved, target_project_path),
                (encode_path(unresolved), target_encoded),
            ]
            seen = {old for old, _ in replacements}
            for old, new in extra:
                if old != new and old not in seen:
                    replacements.append((old, new))
                    seen.add(old)
            replacements.sort(key=lambda x: len(x[0]), reverse=True)

        if args.verbose:
            print(f"Replacements ({len(replacements)}):")
            for old, new in replacements:
                print(f"  {old} → {new}")

        # Extract project files
        project_src = archive_root / "project"
        file_count = 0
        if project_src.is_dir():
            target_dir.mkdir(parents=True, exist_ok=True)
            for root, dirs, filenames in os.walk(project_src):
                for fn in filenames:
                    src_file = Path(root) / fn
                    rel = src_file.relative_to(project_src)
                    dst_file = target_dir / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_file), str(dst_file))
                    file_count += 1

        # Place Claude metadata with path rewriting
        meta_src = archive_root / "claude-meta"
        meta_count = 0
        rewritten_count = 0

        if meta_src.is_dir():
            for root, dirs, filenames in os.walk(meta_src):
                for fn in filenames:
                    src_file = Path(root) / fn
                    # Compute the relative path within claude-meta
                    rel = src_file.relative_to(meta_src)
                    rel_str = str(rel)

                    # Rewrite the directory name for the encoded path
                    if source_encoded in rel_str:
                        rel_str = rel_str.replace(source_encoded, target_encoded)

                    dst_file = claude_dir / rel_str
                    dst_file.parent.mkdir(parents=True, exist_ok=True)

                    # Rewrite text file contents
                    if replacements and is_text_file(src_file):
                        did_rewrite = _copy_with_rewrite(src_file, dst_file, replacements)
                        if did_rewrite:
                            rewritten_count += 1
                    else:
                        shutil.copy2(str(src_file), str(dst_file))

                    meta_count += 1

    print(f"Unpacked to {target_dir}")
    print(f"  Project files:  {file_count}")
    print(f"  Metadata files: {meta_count}")
    print(f"  Files rewritten: {rewritten_count}")
    print(f"  Claude dir:     {claude_dir}")
    return 0


def _copy_with_rewrite(
    src: Path, dst: Path, replacements: List[Tuple[str, str]]
) -> bool:
    """Copy a text file, rewriting paths line by line. Returns True if any changes made."""
    changed = False
    try:
        with open(src, "r", encoding="utf-8", errors="replace") as fin, \
             open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                new_line = rewrite_line(line, replacements)
                if new_line != line:
                    changed = True
                fout.write(new_line)
    except OSError as e:
        # Fall back to binary copy
        shutil.copy2(str(src), str(dst))
    return changed


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    """Print manifest and file listing of a portage archive."""
    archive_path = Path(args.archive).resolve()
    if not archive_path.is_file():
        print(f"Error: archive not found: {archive_path}", file=sys.stderr)
        return 1

    with tarfile.open(str(archive_path), "r:gz") as tar:
        # Find and read manifest
        manifest = None
        members = tar.getmembers()

        for member in members:
            if member.name.endswith("/manifest.json"):
                f = tar.extractfile(member)
                if f:
                    manifest = json.loads(f.read().decode("utf-8"))
                break

        if manifest is None:
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1

        # Print manifest
        print("=== Manifest ===")
        print(f"  Portage version:  {manifest.get('portage_version', 'unknown')}")
        print(f"  Source path:      {manifest['source_project_path']}")
        print(f"  Claude dir:       {manifest['source_claude_dir']}")
        print(f"  Encoded path:     {manifest['source_encoded_path']}")
        print(f"  Sessions:         {len(manifest.get('session_ids', []))}")
        for sid in manifest.get("session_ids", []):
            print(f"    - {sid}")
        print(f"  Project files:    {'yes' if manifest.get('includes_project_files') else 'no'}")
        print(f"  Debug logs:       {'yes' if manifest.get('includes_debug') else 'no'}")

        # File listing summary
        print()
        print("=== Archive Contents ===")
        categories: Dict[str, List[str]] = {}
        for member in members:
            if member.isfile():
                parts = member.name.split("/", 2)
                if len(parts) >= 2:
                    cat = parts[1]
                else:
                    cat = "(root)"
                categories.setdefault(cat, []).append(member.name)

        total = 0
        for cat in sorted(categories.keys()):
            files = categories[cat]
            total += len(files)
            print(f"  {cat}: {len(files)} file(s)")
            if args.verbose:
                for fn in sorted(files)[:20]:
                    print(f"    {fn}")
                if len(files) > 20:
                    print(f"    ... and {len(files) - 20} more")

        print(f"  Total: {total} file(s)")

        # Archive size
        size_bytes = archive_path.stat().st_size
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        print(f"  Archive size: {size_str}")

    return 0


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

def _rewrite_file_in_place(
    path: Path, replacements: List[Tuple[str, str]]
) -> bool:
    """Rewrite a text file in place, applying path replacements.

    Returns True if any changes were made.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return False

    changed = False
    new_lines = []
    for line in lines:
        new_line = rewrite_line(line, replacements)
        if new_line != line:
            changed = True
        new_lines.append(new_line)

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    return changed


def cmd_rename(args: argparse.Namespace) -> int:
    """Rewrite Claude metadata after moving/renaming a project directory."""
    old_dir = Path(args.old_path).expanduser().resolve()
    new_dir = Path(args.new_path).expanduser().resolve()

    if str(old_dir) == str(new_dir):
        print("Error: old and new paths are the same", file=sys.stderr)
        return 1

    claude_dir = default_claude_dir()
    old_encoded = encode_path(str(old_dir))
    new_encoded = encode_path(str(new_dir))
    old_meta = claude_dir / "projects" / old_encoded

    if not old_meta.is_dir():
        print(
            f"Error: no Claude metadata found for {old_dir}\n"
            f"  (looked for {old_meta})",
            file=sys.stderr,
        )
        return 1

    new_meta = claude_dir / "projects" / new_encoded
    if new_meta.exists():
        print(
            f"Error: metadata already exists for target path\n"
            f"  {new_meta}\n"
            f"  Remove it first or choose a different target.",
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        print(f"Old path:     {old_dir}")
        print(f"New path:     {new_dir}")
        print(f"Old encoded:  {old_encoded}")
        print(f"New encoded:  {new_encoded}")

    # Build replacement map (claude_dir stays the same)
    replacements = build_replacement_map(
        source_project_path=str(old_dir),
        target_project_path=str(new_dir),
        source_claude_dir=str(claude_dir),
        target_claude_dir=str(claude_dir),
    )

    if args.verbose:
        print(f"Replacements ({len(replacements)}):")
        for old, new in replacements:
            print(f"  {old} → {new}")

    # Discover sessions so we can rewrite satellite files
    session_ids = discover_session_ids(old_meta)
    session_files = discover_session_files(
        claude_dir, old_meta, session_ids, include_debug=True,
    )

    if args.verbose:
        print(f"Sessions:     {len(session_ids)}")

    # Rewrite all text files under the project metadata dir
    rewritten = 0
    for category, paths in session_files.items():
        for fp in paths:
            if is_text_file(fp):
                if _rewrite_file_in_place(fp, replacements):
                    rewritten += 1

    # Rename the project metadata directory
    shutil.move(str(old_meta), str(new_meta))

    total_files = sum(len(v) for v in session_files.values())
    print(f"Renamed {old_dir} → {new_dir}")
    print(f"  Sessions:        {len(session_ids)}")
    print(f"  Files processed: {total_files}")
    print(f"  Files rewritten: {rewritten}")
    print(f"  Metadata moved:  {old_meta.name} → {new_meta.name}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-portage",
        description="Portable Claude Code workspace archives",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # pack
    p_pack = sub.add_parser("pack", help="Pack project + Claude metadata into archive")
    p_pack.add_argument("project_dir", help="Path to the project directory")
    p_pack.add_argument("-o", "--output", help="Output archive path (default: <project>.portage.tar.gz)")
    p_pack.add_argument("--no-project-files", action="store_true", help="Exclude project files (metadata only)")
    p_pack.add_argument("--include-debug", action="store_true", help="Include debug logs")
    p_pack.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # unpack
    p_unpack = sub.add_parser("unpack", help="Unpack archive to target directory")
    p_unpack.add_argument("archive", help="Path to .portage.tar.gz archive")
    p_unpack.add_argument("target_dir", help="Target directory for project files")
    p_unpack.add_argument("--claude-dir", help="Claude config dir (default: ~/.claude)")
    p_unpack.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect archive contents")
    p_inspect.add_argument("archive", help="Path to .portage.tar.gz archive")
    p_inspect.add_argument("-v", "--verbose", action="store_true", help="Show individual files")

    # rename
    p_rename = sub.add_parser("rename", help="Rewrite Claude metadata after moving/renaming a project")
    p_rename.add_argument("old_path", help="Original project directory path")
    p_rename.add_argument("new_path", help="New project directory path")
    p_rename.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    commands = {
        "pack": cmd_pack,
        "unpack": cmd_unpack,
        "inspect": cmd_inspect,
        "rename": cmd_rename,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
