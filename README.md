# claude-portage

Portable Claude Code workspace archives.

Claude Code stores per-project session history, file snapshots, and metadata in `~/.claude/` using a path-encoding scheme (e.g., `/Users/alice/src/foo` → `-Users-alice-src-foo`). This data is tightly coupled to absolute paths, making it impossible to move a project to another machine or directory and use `claude --continue` or `claude --resume`.

**claude-portage** bundles a project + its Claude metadata into a portable archive that can be unpacked anywhere with automatic path rewriting. It also supports in-place renaming when you move a project directory locally.

## Installation

```bash
# Homebrew
brew tap ebowman/claude-portage
brew install claude-portage

# pip
pip install claude-portage

# Or run directly (zero dependencies)
python3 claude_portage.py <command>
```

## Usage

### Pack a project

```bash
claude-portage pack /path/to/my-project
# Creates my-project.portage.tar.gz

claude-portage pack /path/to/my-project -o /tmp/backup.portage.tar.gz
claude-portage pack /path/to/my-project --no-project-files  # metadata only
claude-portage pack /path/to/my-project --include-debug -v   # include debug logs
```

### Inspect an archive

```bash
claude-portage inspect my-project.portage.tar.gz
claude-portage inspect my-project.portage.tar.gz -v  # show individual files
```

### Unpack to a new location

```bash
claude-portage unpack my-project.portage.tar.gz /new/path/to/my-project
# Project files extracted to /new/path/to/my-project
# Claude metadata placed in ~/.claude/ with all paths rewritten
```

Then:
```bash
cd /new/path/to/my-project
claude --resume  # Sessions from the original machine appear
```

The project's entry in `~/.claude.json` (MCP servers, tool allowlists, trust flags) is captured on pack and restored on unpack, with a backup written to `~/.claude.json.portage-bak`.

### Rename a project directory

After moving/renaming a project directory, update Claude's metadata to match:

```bash
mv ~/src/foo ~/src/bar
claude-portage rename ~/src/foo ~/src/bar
```

This rewrites all paths in the session JSONL, subagent logs, todos, etc. and renames the metadata directory in `~/.claude/projects/`. Sessions will work seamlessly with `claude --resume` from the new location. It also migrates the project's entry in `~/.claude.json` (MCP servers, tool allowlists, trust flags) to the new path, backing up the original to `~/.claude.json.portage-bak`.

## How It Works

The core idea fits in ~20 lines of Python:

```python
import json, os, shutil, tarfile, tempfile
from pathlib import Path

def encode(p): return os.path.realpath(str(p)).replace("/", "-").replace(".", "-")

def pack(project, claude=Path.home()/".claude", out=None):
    meta = claude/"projects"/encode(project)
    out = out or Path(f"{project.name}.portage.tar.gz")
    manifest = {"source": str(project), "claude": str(claude), "encoded": encode(project)}
    with tarfile.open(str(out), "w:gz") as t:
        t.add(str(project), "pkg/project"); t.add(str(meta), "pkg/meta")
        info = tarfile.TarInfo("pkg/manifest.json"); data = json.dumps(manifest).encode()
        info.size = len(data); t.addfile(info, __import__("io").BytesIO(data))

def unpack(archive, target, claude=Path.home()/".claude"):
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(str(archive)) as t: t.extractall(tmp)
        m = json.loads((Path(tmp)/"pkg/manifest.json").read_text())
        reps = [(m["source"], str(target)), (m["encoded"], encode(target)),
                (m["claude"], str(claude))]
        shutil.copytree(f"{tmp}/pkg/project", str(target), dirs_exist_ok=True)
        dst = claude/"projects"/encode(target)
        shutil.copytree(f"{tmp}/pkg/meta", str(dst), dirs_exist_ok=True)
        for f in dst.rglob("*"):
            if f.is_file():
                try:
                    txt = f.read_text()
                    for old, new in reps: txt = txt.replace(old, new)
                    f.write_text(txt)
                except UnicodeDecodeError: pass
```

That's the whole idea: tar up a project and its `~/.claude/projects/<encoded>/` metadata, then string-replace old paths with new paths on unpack.

The actual tool is ~700 lines. Here's what the other ~670 lines handle:

- **Correctness**: Replacement pairs are sorted longest-first so `/Users/alice/src/foo` is replaced before `/Users/alice`. macOS symlinks (`/var` → `/private/var`) generate extra replacement pairs. Encoded directory names in file paths are rewritten during copy, not just inside file contents.
- **Completeness**: Claude scatters session data across 6+ directories — `projects/<encoded>/`, `file-history/<session>/`, `session-env/<session>/`, `todos/`, `plans/`, and `debug/`. The tool discovers all of them, plus subagent logs and tool results nested under each session. After unpack, sessions are registered in `~/.claude/history.jsonl` so `claude --resume` can find them.
- **Usability**: An argparse CLI with `pack`, `unpack`, `inspect`, and `rename` subcommands. Verbose mode. `--no-project-files` for metadata-only archives. `--include-debug` for debug logs. Git-aware file collection that respects `.gitignore`. Human-readable archive inspection. In-place rename for when you just move a directory locally.
- **Robustness**: Binary files are detected (null-byte heuristic + known-suffix allowlist) and copied without rewriting. File timestamps are preserved through rewriting. `tar.extractall` uses `data_filter` when available (Python 3.12+). Graceful error messages for missing projects, missing metadata, and conflicting targets.

## How This Was Built

This entire tool was built with Claude Code in 30 minutes — from `git init` (19:23) to v0.2.0 on PyPI (19:53). Here are the actual prompts used, in order:

**Prompt 1** — the idea (entered via `/feature-forge`, which generated the implementation plan):

> I want to create a tool that lets you run a command that takes a directory in which the user has been working with claude code. The tool takes that directory as well as any/all relevant metadata in ~/.claude to that directory, and bundles it up into an archive file that can be copied to another computer say (or the same computer at a different path). The tool then allows for unarchiving the data where the user wants it, and it hydrates the ~/.claude metadata correctly so that the user could do claude --continue or claude --resume and see the same thing they would see in the original location. When we're done we will open source it in my github account with MIT license.

**Prompt 2** — implement the plan (the plan from prompt 1 was fed back in — ~5400 chars of architecture, archive layout, CLI interface, and implementation steps):

> Implement the following plan: \<the generated plan>

**Prompt 3** — ship it:

> Ok, let's create the github repo, push to github, and get deployed to pip.

**Prompt 4** — add a feature:

> Let's add a feature to claude-portage for renaming a directory. Like I have ~/src/foo that I've been working with claude code in, and I want to rename it to ~/src/bar

**Prompt 5** — ship it again:

> yes

**Prompt 6** — Homebrew:

> How can we get this into homebrew?

That's it. Six prompts. The rest was Claude Code autonomously writing code, running tests, creating the GitHub repo, publishing to PyPI, and setting up the Homebrew tap.

### Debugging on a new machine (v0.2.1–v0.2.3)

After porting to a new Mac (where the home directory was `/Users/eric.bowman` instead of `/Users/ebowman`), three bugs surfaced. Here are the prompts used to find and fix them:

**Prompt 7** — the bug report:

> I installed this from homebrew (not run from this project). I tried to unpack flow-manifesto.portage.tar.gz into a new directory which seemed to work, but when I did claude --continue in that directory, I see "No conversation found to continue"

Claude discovered that `encode_path` only replaced `/` with `-`, but Claude Code also replaces `.` with `-`. So `/Users/eric.bowman/...` was encoded as `-Users-eric.bowman-...` instead of `-Users-eric-bowman-...`. → **v0.2.1**

**Prompt 8** — ship it:

> Great, let's commit, push, and do a release (pip and homebrew tap)

**Prompt 9** — the next bug:

> Ok, I just unpacked a couple of portage archives into ~/src dirs of the same name. It mostly worked, but claude --continue and claude --resume don't seem to be able to see the previous sessions.

Claude discovered that `claude --resume` uses `~/.claude/history.jsonl` as a session index, and `unpack` wasn't creating entries there. → **v0.2.2**

**Prompt 10** — try a fresh unpack:

> Maybe I should just try to unpack again?

**Prompt 11** — the dates are wrong:

> It would be nice if the dates were the same

The resume picker showed "1 minute ago" instead of the original session dates, because `_copy_with_rewrite` created new files with current timestamps instead of preserving the originals. → **v0.2.3**

## Known Limitations

- Path rewriting is string-based, not JSON-aware. This works because paths appear in many contexts (command strings, tool outputs, file paths) where structured rewriting would miss them.
- File-history snapshots (source code versions) are copied as-is without path rewriting, since they are project source code, not metadata.
- The archive does not include Claude's global config (`settings.json`, API keys, etc.) — only project-specific session data.
- Session UUIDs are preserved; if the same session ID already exists at the target, files will be overwritten.

## License

MIT
