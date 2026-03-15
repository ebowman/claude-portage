"""Tests for claude-portage."""

import json
import os
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase, main

import claude_portage


class TestEncodePath(TestCase):
    """Test path encoding matches Claude Code's scheme."""

    def test_simple_path(self):
        # Claude encodes /Users/alice/src/foo as -Users-alice-src-foo
        encoded = claude_portage.encode_path("/Users/alice/src/foo")
        self.assertEqual(encoded, "-Users-alice-src-foo")

    def test_root_path(self):
        encoded = claude_portage.encode_path("/")
        self.assertEqual(encoded, "-")

    def test_dots_replaced(self):
        # Claude Code replaces dots with hyphens in encoded paths
        encoded = claude_portage.encode_path("/tmp/eric.bowman/src/foo")
        self.assertEqual(encoded, "-private-tmp-eric-bowman-src-foo")

    def test_no_trailing_slash(self):
        a = claude_portage.encode_path("/tmp/test")
        b = claude_portage.encode_path("/tmp/test/")
        self.assertEqual(a, b)

    def test_known_real_paths(self):
        # Validate against actual Claude-generated encoded paths
        home = str(Path.home())
        if home == "/Users/ebowman":
            encoded = claude_portage.encode_path("/Users/ebowman/src/claude-portage")
            self.assertEqual(encoded, "-Users-ebowman-src-claude-portage")


class TestRewritePaths(TestCase):
    """Test path rewriting logic."""

    def test_longest_first_ordering(self):
        replacements = claude_portage.build_replacement_map(
            source_project_path="/Users/alice/src/my-project",
            target_project_path="/home/bob/work/my-project",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/home/bob/.claude",
        )
        # The longest source string should come first
        lengths = [len(old) for old, _ in replacements]
        self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_rewrite_line(self):
        replacements = claude_portage.build_replacement_map(
            source_project_path="/Users/alice/src/foo",
            target_project_path="/home/bob/foo",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/home/bob/.claude",
        )
        line = '{"cwd": "/Users/alice/src/foo", "path": "/Users/alice/.claude/projects/-Users-alice-src-foo/x.jsonl"}'
        result = claude_portage.rewrite_line(line, replacements)
        self.assertIn("/home/bob/foo", result)
        self.assertIn("-home-bob-foo", result)
        self.assertNotIn("/Users/alice", result)

    def test_no_partial_match(self):
        # Ensure encoded path replacement doesn't corrupt longer paths
        replacements = claude_portage.build_replacement_map(
            source_project_path="/Users/alice/src/foo",
            target_project_path="/tmp/foo",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/tmp/.claude",
        )
        line = "/Users/alice/src/foo/subdir/file.txt"
        result = claude_portage.rewrite_line(line, replacements)
        self.assertEqual(result, "/tmp/foo/subdir/file.txt")

    def test_no_replacements_when_same_path(self):
        # Use realpath to avoid symlink differences (e.g., /tmp -> /private/tmp)
        src = os.path.realpath("/tmp/foo")
        replacements = claude_portage.build_replacement_map(
            source_project_path=src,
            target_project_path=src,
            source_claude_dir=os.path.realpath("/Users/alice/.claude"),
            target_claude_dir=os.path.realpath("/Users/alice/.claude"),
        )
        self.assertEqual(replacements, [])


class TestPackUnpackRoundtrip(TestCase):
    """Integration test: pack a synthetic project, unpack to a different path."""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create a synthetic project
            src_project = tmpdir / "source" / "my-project"
            src_project.mkdir(parents=True)
            (src_project / "hello.py").write_text("print('hello')\n")
            (src_project / "sub").mkdir()
            (src_project / "sub" / "data.txt").write_text("data\n")

            src_project_path = str(src_project)
            src_encoded = claude_portage.encode_path(src_project_path)

            # Create synthetic Claude metadata
            src_claude = tmpdir / "source-claude"
            src_project_meta = src_claude / "projects" / src_encoded
            src_project_meta.mkdir(parents=True)

            session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

            # sessions JSONL with path references
            jsonl_content = json.dumps({
                "type": "message",
                "cwd": src_project_path,
                "sessionId": session_id,
                "data": {"text": f"Working in {src_project_path}"},
            }) + "\n"
            (src_project_meta / f"{session_id}.jsonl").write_text(jsonl_content)

            # subagents dir
            subagents_dir = src_project_meta / session_id / "subagents"
            subagents_dir.mkdir(parents=True)
            (subagents_dir / "agent-test.jsonl").write_text(
                json.dumps({"cwd": src_project_path}) + "\n"
            )

            # tool-results dir
            tool_results_dir = src_project_meta / session_id / "tool-results"
            tool_results_dir.mkdir(parents=True)
            (tool_results_dir / "result.txt").write_text(f"Output from {src_project_path}\n")

            # memory dir
            memory_dir = src_project_meta / "memory"
            memory_dir.mkdir()
            (memory_dir / "note.md").write_text(f"Project at {src_project_path}\n")

            # file-history
            fh_dir = src_claude / "file-history" / session_id
            fh_dir.mkdir(parents=True)
            (fh_dir / "snapshot.py").write_text("# source code snapshot\n")

            # session-env
            se_dir = src_claude / "session-env" / session_id
            se_dir.mkdir(parents=True)
            (se_dir / "env.json").write_text(json.dumps({"PATH": "/usr/bin"}) + "\n")

            # todos
            todos_dir = src_claude / "todos"
            todos_dir.mkdir(parents=True)
            (todos_dir / f"{session_id}-agent-{session_id}.json").write_text(
                json.dumps({"items": [], "project": src_project_path}) + "\n"
            )

            # --- Pack ---
            archive_path = tmpdir / "test.portage.tar.gz"

            # We need to mock default_claude_dir to use our synthetic one
            original_default = claude_portage.default_claude_dir
            claude_portage.default_claude_dir = lambda: src_claude

            try:
                import argparse
                pack_args = argparse.Namespace(
                    project_dir=str(src_project),
                    output=str(archive_path),
                    no_project_files=False,
                    include_debug=False,
                    verbose=False,
                )
                rc = claude_portage.cmd_pack(pack_args)
                self.assertEqual(rc, 0)
                self.assertTrue(archive_path.exists())
            finally:
                claude_portage.default_claude_dir = original_default

            # --- Unpack ---
            dst_project = tmpdir / "dest" / "unpacked-project"
            dst_claude = tmpdir / "dest-claude"
            claude_portage.default_claude_dir = lambda: dst_claude

            try:
                unpack_args = argparse.Namespace(
                    archive=str(archive_path),
                    target_dir=str(dst_project),
                    claude_dir=str(dst_claude),
                    verbose=False,
                )
                rc = claude_portage.cmd_unpack(unpack_args)
                self.assertEqual(rc, 0)
            finally:
                claude_portage.default_claude_dir = original_default

            # --- Verify ---
            # Project files copied
            self.assertTrue((dst_project / "hello.py").exists())
            self.assertEqual((dst_project / "hello.py").read_text(), "print('hello')\n")
            self.assertTrue((dst_project / "sub" / "data.txt").exists())

            # Claude metadata placed correctly
            dst_encoded = claude_portage.encode_path(str(dst_project))
            dst_meta = dst_claude / "projects" / dst_encoded
            self.assertTrue(dst_meta.is_dir())

            # JSONL paths rewritten
            dst_jsonl = dst_meta / f"{session_id}.jsonl"
            self.assertTrue(dst_jsonl.exists())
            content = dst_jsonl.read_text()
            self.assertIn(str(dst_project), content)
            self.assertNotIn(src_project_path, content)

            # Subagent paths rewritten
            dst_subagent = dst_meta / session_id / "subagents" / "agent-test.jsonl"
            self.assertTrue(dst_subagent.exists())
            sa_content = dst_subagent.read_text()
            self.assertIn(str(dst_project), sa_content)
            self.assertNotIn(src_project_path, sa_content)

            # Memory paths rewritten
            dst_memory = dst_meta / "memory" / "note.md"
            self.assertTrue(dst_memory.exists())
            self.assertIn(str(dst_project), dst_memory.read_text())

            # file-history copied (no rewriting needed for source snapshots)
            dst_fh = dst_claude / "file-history" / session_id / "snapshot.py"
            self.assertTrue(dst_fh.exists())

            # todos rewritten
            dst_todo = dst_claude / "todos" / f"{session_id}-agent-{session_id}.json"
            self.assertTrue(dst_todo.exists())
            self.assertIn(str(dst_project), dst_todo.read_text())


class TestInspect(TestCase):
    """Test the inspect command output."""

    def test_inspect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create a minimal archive manually
            archive_path = tmpdir / "test.portage.tar.gz"
            manifest = {
                "version": 1,
                "portage_version": "0.1.0",
                "source_project_path": "/tmp/test",
                "source_claude_dir": "/Users/test/.claude",
                "source_encoded_path": "-tmp-test",
                "session_ids": ["abc-123"],
                "includes_project_files": True,
                "includes_debug": False,
            }

            with tarfile.open(str(archive_path), "w:gz") as tar:
                claude_portage._add_bytes_to_tar(
                    tar,
                    "test.portage/manifest.json",
                    json.dumps(manifest).encode(),
                )
                claude_portage._add_bytes_to_tar(
                    tar,
                    "test.portage/project/hello.py",
                    b"print('hello')\n",
                )
                claude_portage._add_bytes_to_tar(
                    tar,
                    "test.portage/claude-meta/projects/-tmp-test/abc-123.jsonl",
                    b'{"cwd": "/tmp/test"}\n',
                )

            import argparse
            import io
            from contextlib import redirect_stdout

            args = argparse.Namespace(archive=str(archive_path), verbose=False)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = claude_portage.cmd_inspect(args)

            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("/tmp/test", output)
            self.assertIn("abc-123", output)
            self.assertIn("3 file(s)", output)


class TestNoProjectFiles(TestCase):
    """Test --no-project-files flag."""

    def test_no_project_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create project with files
            src_project = tmpdir / "proj"
            src_project.mkdir()
            (src_project / "code.py").write_text("x = 1\n")

            src_project_path = str(src_project)
            src_encoded = claude_portage.encode_path(src_project_path)

            # Minimal Claude metadata
            src_claude = tmpdir / "claude"
            meta = src_claude / "projects" / src_encoded
            meta.mkdir(parents=True)
            (meta / "sess.jsonl").write_text('{"cwd": "' + src_project_path + '"}\n')

            archive_path = tmpdir / "meta-only.portage.tar.gz"

            original_default = claude_portage.default_claude_dir
            claude_portage.default_claude_dir = lambda: src_claude

            try:
                import argparse
                pack_args = argparse.Namespace(
                    project_dir=str(src_project),
                    output=str(archive_path),
                    no_project_files=True,
                    include_debug=False,
                    verbose=False,
                )
                rc = claude_portage.cmd_pack(pack_args)
                self.assertEqual(rc, 0)
            finally:
                claude_portage.default_claude_dir = original_default

            # Verify no project files in archive
            with tarfile.open(str(archive_path), "r:gz") as tar:
                names = tar.getnames()
                project_files = [n for n in names if "/project/" in n]
                self.assertEqual(project_files, [])
                # But metadata should be present
                meta_files = [n for n in names if "/claude-meta/" in n]
                self.assertGreater(len(meta_files), 0)

            # Verify manifest says no project files
            with tarfile.open(str(archive_path), "r:gz") as tar:
                for m in tar.getmembers():
                    if m.name.endswith("manifest.json"):
                        f = tar.extractfile(m)
                        manifest = json.loads(f.read())
                        self.assertFalse(manifest["includes_project_files"])
                        break


class TestRename(TestCase):
    """Test the rename command."""

    def test_rename_rewrites_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            old_project = tmpdir / "old-project"
            old_project.mkdir()
            old_project_path = str(old_project.resolve())
            old_encoded = claude_portage.encode_path(old_project_path)

            new_project = tmpdir / "new-project"
            new_project_path = str(new_project.resolve())
            new_encoded = claude_portage.encode_path(new_project_path)

            # Create synthetic Claude metadata
            fake_claude = tmpdir / "claude"
            old_meta = fake_claude / "projects" / old_encoded
            old_meta.mkdir(parents=True)

            session_id = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"

            # JSONL with path references
            jsonl_content = json.dumps({
                "type": "message",
                "cwd": old_project_path,
                "sessionId": session_id,
            }) + "\n"
            (old_meta / f"{session_id}.jsonl").write_text(jsonl_content)

            # subagents dir
            sub_dir = old_meta / session_id / "subagents"
            sub_dir.mkdir(parents=True)
            (sub_dir / "agent.jsonl").write_text(
                json.dumps({"cwd": old_project_path}) + "\n"
            )

            # file-history (content should also be rewritten if text)
            fh_dir = fake_claude / "file-history" / session_id
            fh_dir.mkdir(parents=True)
            (fh_dir / "log.txt").write_text(f"edited {old_project_path}/main.py\n")

            # session-env
            se_dir = fake_claude / "session-env" / session_id
            se_dir.mkdir(parents=True)
            (se_dir / "env.json").write_text(json.dumps({"cwd": old_project_path}) + "\n")

            # todos
            todos_dir = fake_claude / "todos"
            todos_dir.mkdir(parents=True)
            (todos_dir / f"{session_id}-agent-{session_id}.json").write_text(
                json.dumps({"project": old_project_path}) + "\n"
            )

            # Mock default_claude_dir
            original = claude_portage.default_claude_dir
            claude_portage.default_claude_dir = lambda: fake_claude

            try:
                import argparse
                rename_args = argparse.Namespace(
                    old_path=str(old_project),
                    new_path=str(new_project),
                    verbose=False,
                )
                rc = claude_portage.cmd_rename(rename_args)
                self.assertEqual(rc, 0)
            finally:
                claude_portage.default_claude_dir = original

            # Old metadata dir should be gone
            self.assertFalse(old_meta.exists())

            # New metadata dir should exist
            new_meta = fake_claude / "projects" / new_encoded
            self.assertTrue(new_meta.is_dir())

            # JSONL should have rewritten paths
            new_jsonl = new_meta / f"{session_id}.jsonl"
            content = new_jsonl.read_text()
            self.assertIn(new_project_path, content)
            self.assertNotIn(old_project_path, content)

            # Subagent should be rewritten
            sa_content = (new_meta / session_id / "subagents" / "agent.jsonl").read_text()
            self.assertIn(new_project_path, sa_content)
            self.assertNotIn(old_project_path, sa_content)

            # Satellite files should be rewritten
            fh_content = (fh_dir / "log.txt").read_text()
            self.assertIn(new_project_path, fh_content)
            self.assertNotIn(old_project_path, fh_content)

            se_content = (se_dir / "env.json").read_text()
            self.assertIn(new_project_path, se_content)

            todo_content = (todos_dir / f"{session_id}-agent-{session_id}.json").read_text()
            self.assertIn(new_project_path, todo_content)

    def test_rename_same_path_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            resolved = str(project.resolve())

            import argparse
            args = argparse.Namespace(
                old_path=resolved,
                new_path=resolved,
                verbose=False,
            )
            rc = claude_portage.cmd_rename(args)
            self.assertEqual(rc, 1)

    def test_rename_no_metadata_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            fake_claude = tmpdir / "claude"
            fake_claude.mkdir()

            original = claude_portage.default_claude_dir
            claude_portage.default_claude_dir = lambda: fake_claude

            try:
                import argparse
                args = argparse.Namespace(
                    old_path=str(tmpdir / "nonexistent"),
                    new_path=str(tmpdir / "other"),
                    verbose=False,
                )
                rc = claude_portage.cmd_rename(args)
                self.assertEqual(rc, 1)
            finally:
                claude_portage.default_claude_dir = original

    def test_rename_target_exists_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            old_project = tmpdir / "old"
            old_project.mkdir()
            new_project = tmpdir / "new"
            new_project.mkdir()

            fake_claude = tmpdir / "claude"
            old_encoded = claude_portage.encode_path(str(old_project.resolve()))
            new_encoded = claude_portage.encode_path(str(new_project.resolve()))

            # Create metadata for both
            (fake_claude / "projects" / old_encoded).mkdir(parents=True)
            (fake_claude / "projects" / new_encoded).mkdir(parents=True)

            original = claude_portage.default_claude_dir
            claude_portage.default_claude_dir = lambda: fake_claude

            try:
                import argparse
                args = argparse.Namespace(
                    old_path=str(old_project),
                    new_path=str(new_project),
                    verbose=False,
                )
                rc = claude_portage.cmd_rename(args)
                self.assertEqual(rc, 1)
            finally:
                claude_portage.default_claude_dir = original


if __name__ == "__main__":
    main()
