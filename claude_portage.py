#!/usr/bin/env python3
"""claude-portage: Portable Claude Code workspace archives.

Bundles a project + its Claude Code metadata (~/.claude/) into a portable
archive that can be unpacked anywhere with automatic path rewriting.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

__version__ = "0.2.5"

_TEXT_SUFFIXES = frozenset({
    ".json", ".jsonl", ".txt", ".md", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".log", ".csv",
})

_WALK_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".tox", ".venv", "venv",
})


# ---------------------------------------------------------------------------
# Path encoding
# ---------------------------------------------------------------------------

def encode_path(path: Path | str) -> str:
    """Encode an absolute path into Claude's directory-name scheme.

    Claude Code replaces each ``/``, ``.``, and `` `` with ``-`` in the
    resolved absolute path, e.g. ``/Users/alice/src/foo`` → ``-Users-alice-src-foo``
    and ``/foo/01 - Bar`` → ``-foo-01---Bar``
    """
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    return resolved.replace(os.sep, "-").replace(".", "-").replace(" ", "-")


def default_claude_dir() -> Path:
    return Path.home() / ".claude"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _collect_files(directory: Path) -> list[Path]:
    """Collect all files under a directory tree."""
    if not directory.is_dir():
        return []
    return [
        Path(root) / fn
        for root, _, filenames in os.walk(directory)
        for fn in filenames
    ]


def discover_session_ids(project_meta_dir: Path) -> list[str]:
    """Return session IDs from <uuid>.jsonl files in the project meta dir."""
    if not project_meta_dir.is_dir():
        return []
    return sorted(
        p.stem for p in project_meta_dir.iterdir()
        if p.suffix == ".jsonl" and p.stem != "sessions-index"
    )


def discover_session_files(
    claude_dir: Path, project_meta_dir: Path,
    session_ids: list[str], include_debug: bool = False,
) -> list[Path]:
    """Return all metadata files related to the given sessions."""
    files = _collect_files(project_meta_dir)

    for sid in session_ids:
        files.extend(_collect_files(claude_dir / "file-history" / sid))
        files.extend(_collect_files(claude_dir / "session-env" / sid))
        if include_debug:
            debug = claude_dir / "debug" / f"{sid}.txt"
            if debug.is_file():
                files.append(debug)
        todos = claude_dir / "todos"
        if todos.is_dir():
            files.extend(todos.glob(f"{sid}-*.json"))

    plans_dir = claude_dir / "plans"
    if plans_dir.is_dir():
        slugs: set[str] = set()
        for sid in session_ids:
            jsonl = project_meta_dir / f"{sid}.jsonl"
            if not jsonl.is_file():
                continue
            try:
                text = jsonl.read_text(encoding="utf-8", errors="replace")
                slugs.update(m.group(1) for m in re.finditer(r'plans/([a-zA-Z0-9_-]+\.md)', text))
            except OSError:
                pass
        for slug in slugs:
            plan = plans_dir / slug
            if plan.is_file():
                files.append(plan)

    return files


def collect_project_files(project_dir: Path) -> list[Path]:
    """Collect project files, preferring git ls-files when available."""
    project_dir = project_dir.resolve()
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(project_dir),
            capture_output=True, text=False,
        )
        if result.returncode == 0 and result.stdout:
            return sorted(
                project_dir / e.decode("utf-8", errors="replace")
                for e in result.stdout.split(b"\x00") if e
            )
    except FileNotFoundError:
        pass

    paths: list[Path] = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP_DIRS and not d.startswith(".")]
        paths.extend(Path(root) / fn for fn in filenames)
    return sorted(paths)


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

def build_replacement_map(
    source_project_path: str, target_project_path: str,
    source_claude_dir: str, target_claude_dir: str,
) -> list[tuple[str, str]]:
    """Build a replacement map sorted longest-first to avoid partial matches."""
    source_encoded = encode_path(source_project_path)
    target_encoded = encode_path(target_project_path)

    pairs = [
        (source_project_path, target_project_path),
        (source_claude_dir, target_claude_dir),
        (source_encoded, target_encoded),
    ]
    # Handle symlink variants (e.g., macOS /var → /private/var)
    real_proj = os.path.realpath(source_project_path)
    real_claude = os.path.realpath(source_claude_dir)
    if real_proj != source_project_path:
        pairs.append((real_proj, target_project_path))
        pairs.append((encode_path(real_proj), target_encoded))
    if real_claude != source_claude_dir:
        pairs.append((real_claude, target_claude_dir))

    return _dedupe_and_sort(pairs)


def _dedupe_and_sort(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Deduplicate and sort longest-first."""
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for old, new in pairs:
        if old != new and old not in seen:
            seen.add(old)
            unique.append((old, new))
    unique.sort(key=lambda x: len(x[0]), reverse=True)
    return unique


def rewrite_line(line: str, replacements: list[tuple[str, str]]) -> str:
    """Apply all replacements to a single line."""
    for old, new in replacements:
        line = line.replace(old, new)
    return line


def is_text_file(path: Path) -> bool:
    """Heuristic: known text suffix, or no null bytes in first 8KB."""
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    try:
        return b"\x00" not in path.read_bytes()[:8192]
    except OSError:
        return False


def _rewrite_text_file(
    src: Path, dst: Path, replacements: list[tuple[str, str]],
) -> bool:
    """Copy src to dst, rewriting paths. Preserves mtime. Returns True if changed."""
    try:
        changed = False
        with open(src, "r", encoding="utf-8", errors="replace") as fin, \
             open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                new_line = rewrite_line(line, replacements)
                if new_line != line:
                    changed = True
                fout.write(new_line)
        st = os.stat(str(src))
        os.utime(str(dst), (st.st_atime, st.st_mtime))
        return changed
    except OSError:
        shutil.copy2(str(src), str(dst))
        return False


def _rewrite_in_place(path: Path, replacements: list[tuple[str, str]]) -> bool:
    """Rewrite a text file in place. Returns True if any changes were made."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return False
    new_lines = [rewrite_line(line, replacements) for line in lines]
    if new_lines == lines:
        return False
    path.write_text("".join(new_lines), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Tar helpers
# ---------------------------------------------------------------------------

def _add_bytes_to_tar(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Add raw bytes as a file to a tar archive."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _add_files_to_tar(
    tar: tarfile.TarFile, files: list[Path], base_dir: Path,
    prefix: str, verbose: bool,
) -> int:
    """Add files to tar relative to base_dir. Returns count added."""
    count = 0
    for fp in files:
        try:
            tar.add(str(fp), arcname=f"{prefix}/{fp.relative_to(base_dir)}")
            count += 1
        except (ValueError, OSError) as e:
            if verbose:
                print(f"  Skipping {fp}: {e}", file=sys.stderr)
    return count


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def pack(
    project_path: Path, claude_dir: Path, *,
    output: Path | None = None, include_project_files: bool = True,
    include_debug: bool = False, verbose: bool = False,
    project_path_unresolved: Path | None = None,
) -> int:
    """Pack a project and its Claude metadata into a portable archive."""
    encoded = encode_path(project_path)
    meta_dir = claude_dir / "projects" / encoded

    if not project_path.is_dir():
        print(f"Error: project directory not found: {project_path}", file=sys.stderr)
        return 1
    if not meta_dir.is_dir():
        print(f"Error: no Claude metadata at {meta_dir}\n"
              f"  (encoded: {encoded})", file=sys.stderr)
        return 1

    source_path = str(project_path)
    unresolved = (
        str(project_path_unresolved)
        if project_path_unresolved and str(project_path_unresolved) != source_path
        else None
    )

    session_ids = discover_session_ids(meta_dir)
    meta_files = discover_session_files(claude_dir, meta_dir, session_ids,
                                        include_debug=include_debug)
    project_files = collect_project_files(project_path) if include_project_files else []

    manifest: dict[str, Any] = {
        "version": 1, "portage_version": __version__,
        "source_project_path": source_path,
        "source_claude_dir": str(claude_dir),
        "source_encoded_path": encoded,
        "session_ids": session_ids,
        "includes_project_files": include_project_files,
        "includes_debug": include_debug,
    }
    if unresolved:
        manifest["source_project_path_unresolved"] = unresolved

    output_path = output or (Path.cwd() / f"{project_path.name}.portage.tar.gz")
    prefix = f"{project_path.name}.portage"

    if verbose:
        print(f"Project: {project_path}  Claude: {claude_dir}  Sessions: {len(session_ids)}")

    with tarfile.open(str(output_path), "w:gz") as tar:
        _add_bytes_to_tar(tar, f"{prefix}/manifest.json",
                          json.dumps(manifest, indent=2).encode("utf-8"))
        file_count = (_add_files_to_tar(tar, project_files, project_path,
                                        f"{prefix}/project", verbose)
                      if include_project_files else 0)
        meta_count = _add_files_to_tar(tar, meta_files, claude_dir,
                                       f"{prefix}/claude-meta", verbose)

    print(f"Packed {output_path}")
    print(f"  Sessions: {len(session_ids)}  Project files: {file_count}  Metadata: {meta_count}")
    return 0


def unpack(
    archive_path: Path, target_dir: Path, claude_dir: Path, *,
    verbose: bool = False,
) -> int:
    """Unpack a portage archive to a target directory with path rewriting."""
    if not archive_path.is_file():
        print(f"Error: archive not found: {archive_path}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(str(archive_path), "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=tmpdir, filter="data")
            else:
                tar.extractall(path=tmpdir)

        roots = [d for d in Path(tmpdir).iterdir() if d.is_dir()]
        if len(roots) != 1:
            print(f"Error: expected one root in archive, found {len(roots)}", file=sys.stderr)
            return 1

        manifest_path = roots[0] / "manifest.json"
        if not manifest_path.is_file():
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        target_path = str(target_dir)
        target_encoded = encode_path(target_path)
        source_encoded = manifest["source_encoded_path"]

        # Build replacements including unresolved path variants
        replacements = build_replacement_map(
            manifest["source_project_path"], target_path,
            manifest["source_claude_dir"], str(claude_dir),
        )
        unresolved = manifest.get("source_project_path_unresolved")
        if unresolved and unresolved != manifest["source_project_path"]:
            replacements.extend([
                (unresolved, target_path),
                (encode_path(unresolved), target_encoded),
            ])
            replacements = _dedupe_and_sort(replacements)

        if verbose:
            print(f"Source: {manifest['source_project_path']} → {target_path}")
            for old, new in replacements:
                print(f"  {old} → {new}")

        # Copy project files
        file_count = 0
        project_src = roots[0] / "project"
        if project_src.is_dir():
            target_dir.mkdir(parents=True, exist_ok=True)
            for src in _collect_files(project_src):
                dst = target_dir / src.relative_to(project_src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                file_count += 1

        # Copy metadata with path rewriting
        meta_count, rewritten = 0, 0
        meta_src = roots[0] / "claude-meta"
        if meta_src.is_dir():
            for src in _collect_files(meta_src):
                rel = str(src.relative_to(meta_src))
                if source_encoded in rel:
                    rel = rel.replace(source_encoded, target_encoded)
                dst = claude_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if replacements and is_text_file(src):
                    if _rewrite_text_file(src, dst, replacements):
                        rewritten += 1
                else:
                    shutil.copy2(str(src), str(dst))
                meta_count += 1

    history = _register_sessions_in_history(
        claude_dir, target_encoded, target_path,
        manifest.get("session_ids", []), verbose,
    )

    print(f"Unpacked to {target_dir}")
    print(f"  Project files: {file_count}  Metadata: {meta_count}  "
          f"Rewritten: {rewritten}  History: {history}")
    return 0


def inspect_archive(archive_path: Path, verbose: bool = False) -> int:
    """Print manifest and file listing of a portage archive."""
    if not archive_path.is_file():
        print(f"Error: archive not found: {archive_path}", file=sys.stderr)
        return 1

    with tarfile.open(str(archive_path), "r:gz") as tar:
        manifest = None
        for member in tar.getmembers():
            if member.name.endswith("/manifest.json"):
                f = tar.extractfile(member)
                if f:
                    manifest = json.loads(f.read().decode("utf-8"))
                    break
        if manifest is None:
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1

        print("=== Manifest ===")
        print(f"  Portage version:  {manifest.get('portage_version', 'unknown')}")
        print(f"  Source path:      {manifest['source_project_path']}")
        print(f"  Claude dir:       {manifest['source_claude_dir']}")
        print(f"  Encoded path:     {manifest['source_encoded_path']}")
        sids = manifest.get("session_ids", [])
        print(f"  Sessions:         {len(sids)}")
        for sid in sids:
            print(f"    - {sid}")
        print(f"  Project files:    {'yes' if manifest.get('includes_project_files') else 'no'}")
        print(f"  Debug logs:       {'yes' if manifest.get('includes_debug') else 'no'}")

        print("\n=== Archive Contents ===")
        categories: dict[str, list[str]] = {}
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/", 2)
            cat = parts[1] if len(parts) >= 2 else "(root)"
            categories.setdefault(cat, []).append(member.name)

        total = 0
        for cat in sorted(categories):
            cat_files = categories[cat]
            total += len(cat_files)
            print(f"  {cat}: {len(cat_files)} file(s)")
            if verbose:
                for fn in sorted(cat_files)[:20]:
                    print(f"    {fn}")
                if len(cat_files) > 20:
                    print(f"    ... and {len(cat_files) - 20} more")
        print(f"  Total: {total} file(s)")

    print(f"  Archive size: {_format_size(archive_path.stat().st_size)}")
    return 0


def rename(
    old_path: Path, new_path: Path, claude_dir: Path, *,
    verbose: bool = False,
) -> int:
    """Rewrite Claude metadata after moving/renaming a project directory."""
    if old_path == new_path:
        print("Error: old and new paths are the same", file=sys.stderr)
        return 1

    old_encoded = encode_path(old_path)
    new_encoded = encode_path(new_path)
    old_meta = claude_dir / "projects" / old_encoded
    new_meta = claude_dir / "projects" / new_encoded

    if not old_meta.is_dir():
        print(f"Error: no Claude metadata for {old_path}\n"
              f"  (looked for {old_meta})", file=sys.stderr)
        return 1
    if new_meta.exists():
        print(f"Error: metadata already exists at {new_meta}\n"
              f"  Remove it first or choose a different target.", file=sys.stderr)
        return 1

    replacements = build_replacement_map(
        str(old_path), str(new_path), str(claude_dir), str(claude_dir),
    )

    session_ids = discover_session_ids(old_meta)
    all_files = discover_session_files(claude_dir, old_meta, session_ids, include_debug=True)

    if verbose:
        print(f"Renaming {old_path} → {new_path}  Sessions: {len(session_ids)}")

    rewritten = sum(
        1 for fp in all_files
        if is_text_file(fp) and _rewrite_in_place(fp, replacements)
    )
    shutil.move(str(old_meta), str(new_meta))

    print(f"Renamed {old_path} → {new_path}")
    print(f"  Sessions: {len(session_ids)}  Files: {len(all_files)}  Rewritten: {rewritten}")
    return 0


# ---------------------------------------------------------------------------
# History registration
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _parse_timestamp_ms(ts: str) -> int:
    """Parse an ISO timestamp string to epoch milliseconds."""
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, OSError):
        return 0


def _extract_display_text(message: Any) -> str:
    """Extract display text from a Claude message field."""
    if isinstance(message, list):
        for block in message:
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"][:100]
    elif isinstance(message, str):
        return message[:100]
    return "(migrated session)"


def _session_display_info(session_file: Path) -> tuple[str, int]:
    """Extract (display_text, timestamp_ms) from the first user message."""
    try:
        with open(session_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "user":
                    return (_extract_display_text(record.get("message")),
                            _parse_timestamp_ms(record.get("timestamp", "")))
    except OSError:
        pass
    return "(migrated session)", 0


def _register_sessions_in_history(
    claude_dir: Path, target_encoded: str, target_project_path: str,
    session_ids: list[str], verbose: bool,
) -> int:
    """Append entries to ~/.claude/history.jsonl for migrated sessions."""
    if not session_ids:
        return 0

    history_path = claude_dir / "history.jsonl"
    project_dir = claude_dir / "projects" / target_encoded

    existing: set[str] = set()
    if history_path.is_file():
        try:
            with open(history_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            existing.add(json.loads(line).get("sessionId", ""))
                        except (json.JSONDecodeError, KeyError):
                            pass
        except OSError:
            pass

    entries: list[str] = []
    for sid in session_ids:
        if sid in existing:
            continue
        session_file = project_dir / f"{sid}.jsonl"
        if not session_file.is_file():
            continue
        display, ts = _session_display_info(session_file)
        entries.append(json.dumps({
            "display": display, "pastedContents": {},
            "timestamp": ts, "project": target_project_path, "sessionId": sid,
        }, ensure_ascii=False))

    if entries:
        try:
            with open(history_path, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(entry + "\n")
            if verbose:
                print(f"Added {len(entries)} entries to {history_path}")
        except OSError as e:
            print(f"Warning: could not update history: {e}", file=sys.stderr)

    return len(entries)


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

    p = sub.add_parser("pack", help="Pack project + Claude metadata into archive")
    p.add_argument("project_dir", help="Path to the project directory")
    p.add_argument("-o", "--output", help="Output archive path")
    p.add_argument("--no-project-files", action="store_true", help="Exclude project files")
    p.add_argument("--include-debug", action="store_true", help="Include debug logs")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("unpack", help="Unpack archive to target directory")
    p.add_argument("archive", help="Path to .portage.tar.gz archive")
    p.add_argument("target_dir", help="Target directory")
    p.add_argument("--claude-dir", help="Claude config dir (default: ~/.claude)")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("inspect", help="Inspect archive contents")
    p.add_argument("archive", help="Path to .portage.tar.gz archive")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("rename", help="Rewrite metadata after moving a project")
    p.add_argument("old_path", help="Original project directory path")
    p.add_argument("new_path", help="New project directory path")
    p.add_argument("-v", "--verbose", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "pack":
        raw = Path(args.project_dir).expanduser().absolute()
        resolved = raw.resolve()
        return pack(
            project_path=resolved, claude_dir=default_claude_dir(),
            output=Path(args.output) if args.output else None,
            include_project_files=not args.no_project_files,
            include_debug=args.include_debug, verbose=args.verbose,
            project_path_unresolved=raw if raw != resolved else None,
        )

    if args.command == "unpack":
        return unpack(
            archive_path=Path(args.archive).resolve(),
            target_dir=Path(args.target_dir).resolve(),
            claude_dir=(Path(args.claude_dir).resolve() if args.claude_dir
                        else default_claude_dir()),
            verbose=args.verbose,
        )

    if args.command == "inspect":
        return inspect_archive(Path(args.archive).resolve(), args.verbose)

    if args.command == "rename":
        return rename(
            old_path=Path(args.old_path).expanduser().resolve(),
            new_path=Path(args.new_path).expanduser().resolve(),
            claude_dir=default_claude_dir(), verbose=args.verbose,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
