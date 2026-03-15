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

### Rename a project directory

After moving/renaming a project directory, update Claude's metadata to match:

```bash
mv ~/src/foo ~/src/bar
claude-portage rename ~/src/foo ~/src/bar
```

This rewrites all paths in the session JSONL, subagent logs, todos, etc. and renames the metadata directory in `~/.claude/projects/`. Sessions will work seamlessly with `claude --resume` from the new location.

## How It Works

1. **Pack** discovers all Claude metadata for a project: session JSONL files, subagent logs, tool results, file-history snapshots, session environments, todos, plans, and memory.
2. The archive includes a `manifest.json` recording the source absolute paths.
3. **Unpack** extracts project files and places Claude metadata into `~/.claude/` on the target machine, performing line-by-line string replacement of all source paths with target paths.
4. Path rewriting handles the project path, the Claude config directory path, and the encoded directory name, applied longest-first to avoid partial matches.

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
