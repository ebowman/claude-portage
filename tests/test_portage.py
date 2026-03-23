"""Tests for claude-portage."""

import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase, main

import claude_portage
from claude_portage import (
    build_replacement_map,
    encode_path,
    inspect_archive,
    is_text_file,
    pack,
    rename,
    rewrite_line,
    unpack,
    _extract_display_text,
    _format_size,
    _parse_timestamp_ms,
)


class TestEncodePath(TestCase):
    """Test path encoding matches Claude Code's scheme."""

    def test_simple_path(self):
        self.assertEqual(encode_path("/Users/alice/src/foo"), "-Users-alice-src-foo")

    def test_root_path(self):
        self.assertEqual(encode_path("/"), "-")

    def test_dots_replaced(self):
        self.assertEqual(encode_path("/tmp/eric.bowman/src/foo"), "-private-tmp-eric-bowman-src-foo")

    def test_no_trailing_slash(self):
        expected = "-private-tmp-test"
        self.assertEqual(encode_path("/tmp/test"), expected)
        self.assertEqual(encode_path("/tmp/test/"), expected)

    def test_spaces_replaced(self):
        self.assertEqual(
            encode_path("/Users/foo/01 - Projects/01 - My Project"),
            "-Users-foo-01---Projects-01---My-Project",
        )

    def test_known_real_paths(self):
        if str(Path.home()) == "/Users/ebowman":
            self.assertEqual(
                encode_path("/Users/ebowman/src/claude-portage"),
                "-Users-ebowman-src-claude-portage",
            )


class TestRewritePaths(TestCase):
    """Test path rewriting logic."""

    def test_longest_first_ordering(self):
        replacements = build_replacement_map(
            source_project_path="/Users/alice/src/my-project",
            target_project_path="/home/bob/work/my-project",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/home/bob/.claude",
        )
        lengths = [len(old) for old, _ in replacements]
        self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_rewrite_line(self):
        replacements = build_replacement_map(
            source_project_path="/Users/alice/src/foo",
            target_project_path="/home/bob/foo",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/home/bob/.claude",
        )
        line = '{"cwd": "/Users/alice/src/foo", "path": "/Users/alice/.claude/projects/-Users-alice-src-foo/x.jsonl"}'
        result = rewrite_line(line, replacements)
        self.assertIn("/home/bob/foo", result)
        self.assertIn("-home-bob-foo", result)
        self.assertNotIn("/Users/alice", result)

    def test_no_partial_match(self):
        replacements = build_replacement_map(
            source_project_path="/Users/alice/src/foo",
            target_project_path="/tmp/foo",
            source_claude_dir="/Users/alice/.claude",
            target_claude_dir="/tmp/.claude",
        )
        line = "/Users/alice/src/foo/subdir/file.txt"
        result = rewrite_line(line, replacements)
        self.assertEqual(result, "/tmp/foo/subdir/file.txt")

    def test_no_replacements_when_same_path(self):
        src = os.path.realpath("/tmp/foo")
        replacements = build_replacement_map(
            source_project_path=src,
            target_project_path=src,
            source_claude_dir=os.path.realpath("/Users/alice/.claude"),
            target_claude_dir=os.path.realpath("/Users/alice/.claude"),
        )
        self.assertEqual(replacements, [])

    def test_empty_line_unchanged(self):
        self.assertEqual(rewrite_line("", [("/old", "/new")]), "")

    def test_multiple_occurrences_in_line(self):
        line = "/old/path and /old/path again"
        self.assertEqual(rewrite_line(line, [("/old/path", "/new/path")]),
                         "/new/path and /new/path again")


class TestIsTextFile(TestCase):
    """Test text file detection heuristic."""

    def test_known_text_suffix(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            self.assertTrue(is_text_file(Path(f.name)))

    def test_binary_content(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02\x03")
            f.flush()
            self.assertFalse(is_text_file(Path(f.name)))
        os.unlink(f.name)

    def test_text_content_unknown_suffix(self):
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"just plain text\n")
            f.flush()
            self.assertTrue(is_text_file(Path(f.name)))
        os.unlink(f.name)

    def test_nonexistent_file(self):
        self.assertFalse(is_text_file(Path("/nonexistent/file.xyz")))


class TestFormatSize(TestCase):
    """Test human-readable size formatting."""

    def test_bytes(self):
        self.assertEqual(_format_size(0), "0 B")
        self.assertEqual(_format_size(512), "512 B")
        self.assertEqual(_format_size(1023), "1023 B")

    def test_kilobytes(self):
        self.assertEqual(_format_size(1024), "1.0 KB")
        self.assertEqual(_format_size(1536), "1.5 KB")

    def test_megabytes(self):
        self.assertEqual(_format_size(1048576), "1.0 MB")
        self.assertEqual(_format_size(2621440), "2.5 MB")


class TestDisplayTextExtraction(TestCase):
    """Test message display text extraction."""

    def test_list_message(self):
        self.assertEqual(_extract_display_text([{"type": "text", "text": "Hello world"}]),
                         "Hello world")

    def test_string_message(self):
        self.assertEqual(_extract_display_text("Hello"), "Hello")

    def test_truncation(self):
        self.assertEqual(len(_extract_display_text("x" * 200)), 100)

    def test_none_message(self):
        self.assertEqual(_extract_display_text(None), "(migrated session)")

    def test_empty_list(self):
        self.assertEqual(_extract_display_text([]), "(migrated session)")


class TestTimestampParsing(TestCase):
    """Test ISO timestamp parsing."""

    def test_valid_timestamp(self):
        self.assertGreater(_parse_timestamp_ms("2026-03-10T12:00:00.000Z"), 0)

    def test_empty_string(self):
        self.assertEqual(_parse_timestamp_ms(""), 0)

    def test_invalid_string(self):
        self.assertEqual(_parse_timestamp_ms("not-a-date"), 0)


class TestPackUnpackRoundtrip(TestCase):
    """Integration test: pack a synthetic project, unpack to a different path."""

    def _create_synthetic_project(self, base: Path):
        """Create a synthetic project with Claude metadata for testing.

        Returns (project_dir, claude_dir, session_id).
        """
        project = base / "source" / "my-project"
        project.mkdir(parents=True)
        (project / "hello.py").write_text("print('hello')\n")
        (project / "sub").mkdir()
        (project / "sub" / "data.txt").write_text("data\n")

        project_path = str(project)
        encoded = encode_path(project_path)

        claude = base / "source-claude"
        meta = claude / "projects" / encoded
        meta.mkdir(parents=True)

        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        jsonl = (
            json.dumps({"type": "message", "cwd": project_path, "sessionId": session_id,
                        "data": {"text": f"Working in {project_path}"}}) + "\n"
            + json.dumps({"type": "user", "cwd": project_path, "sessionId": session_id,
                          "timestamp": "2026-03-10T12:00:00.000Z",
                          "message": [{"type": "text", "text": "Hello from test"}]}) + "\n"
        )
        (meta / f"{session_id}.jsonl").write_text(jsonl)

        subagents = meta / session_id / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-test.jsonl").write_text(json.dumps({"cwd": project_path}) + "\n")

        tool_results = meta / session_id / "tool-results"
        tool_results.mkdir(parents=True)
        (tool_results / "result.txt").write_text(f"Output from {project_path}\n")

        memory = meta / "memory"
        memory.mkdir()
        (memory / "note.md").write_text(f"Project at {project_path}\n")

        fh = claude / "file-history" / session_id
        fh.mkdir(parents=True)
        (fh / "snapshot.py").write_text("# source code snapshot\n")

        se = claude / "session-env" / session_id
        se.mkdir(parents=True)
        (se / "env.json").write_text(json.dumps({"PATH": "/usr/bin"}) + "\n")

        todos = claude / "todos"
        todos.mkdir(parents=True)
        (todos / f"{session_id}-agent-{session_id}.json").write_text(
            json.dumps({"items": [], "project": project_path}) + "\n"
        )

        return project, claude, session_id

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            src_project, src_claude, session_id = self._create_synthetic_project(base)
            src_project_path = str(src_project)

            # Pack
            archive_path = base / "test.portage.tar.gz"
            rc = pack(
                project_path=src_project.resolve(),
                claude_dir=src_claude,
                output=archive_path,
            )
            self.assertEqual(rc, 0)
            self.assertTrue(archive_path.exists())

            # Unpack
            dst_project = base / "dest" / "unpacked-project"
            dst_claude = base / "dest-claude"
            rc = unpack(
                archive_path=archive_path,
                target_dir=dst_project,
                claude_dir=dst_claude,
            )
            self.assertEqual(rc, 0)

            # Verify project files
            self.assertTrue((dst_project / "hello.py").exists())
            self.assertEqual((dst_project / "hello.py").read_text(), "print('hello')\n")
            self.assertTrue((dst_project / "sub" / "data.txt").exists())

            # Verify metadata placed correctly
            dst_encoded = encode_path(str(dst_project))
            dst_meta = dst_claude / "projects" / dst_encoded
            self.assertTrue(dst_meta.is_dir())

            # Verify JSONL paths rewritten
            content = (dst_meta / f"{session_id}.jsonl").read_text()
            self.assertIn(str(dst_project), content)
            self.assertNotIn(src_project_path, content)

            # Verify subagent paths rewritten
            sa_content = (dst_meta / session_id / "subagents" / "agent-test.jsonl").read_text()
            self.assertIn(str(dst_project), sa_content)
            self.assertNotIn(src_project_path, sa_content)

            # Verify memory paths rewritten
            self.assertIn(str(dst_project), (dst_meta / "memory" / "note.md").read_text())

            # Verify file-history copied
            self.assertTrue((dst_claude / "file-history" / session_id / "snapshot.py").exists())

            # Verify todos rewritten
            self.assertIn(
                str(dst_project),
                (dst_claude / "todos" / f"{session_id}-agent-{session_id}.json").read_text(),
            )

            # Verify history.jsonl
            history = dst_claude / "history.jsonl"
            self.assertTrue(history.exists())
            entry = json.loads(history.read_text().strip())
            self.assertEqual(entry["sessionId"], session_id)
            self.assertEqual(entry["project"], str(dst_project.resolve()))
            self.assertEqual(entry["display"], "Hello from test")
            self.assertGreater(entry["timestamp"], 0)


class TestInspect(TestCase):
    """Test the inspect command."""

    def test_inspect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "test.portage.tar.gz"
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
                    tar, "test.portage/manifest.json", json.dumps(manifest).encode())
                claude_portage._add_bytes_to_tar(
                    tar, "test.portage/project/hello.py", b"print('hello')\n")
                claude_portage._add_bytes_to_tar(
                    tar, "test.portage/claude-meta/projects/-tmp-test/abc-123.jsonl",
                    b'{"cwd": "/tmp/test"}\n')

            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = inspect_archive(archive_path)

            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("/tmp/test", output)
            self.assertIn("abc-123", output)
            self.assertIn("3 file(s)", output)

    def test_inspect_missing_archive(self):
        rc = inspect_archive(Path("/nonexistent/archive.tar.gz"))
        self.assertEqual(rc, 1)


class TestNoProjectFiles(TestCase):
    """Test --no-project-files flag."""

    def test_no_project_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            project = base / "proj"
            project.mkdir()
            (project / "code.py").write_text("x = 1\n")

            claude = base / "claude"
            encoded = encode_path(str(project))
            meta = claude / "projects" / encoded
            meta.mkdir(parents=True)
            (meta / "sess.jsonl").write_text('{"cwd": "' + str(project) + '"}\n')

            archive_path = base / "meta-only.portage.tar.gz"
            rc = pack(
                project_path=project,
                claude_dir=claude,
                output=archive_path,
                include_project_files=False,
            )
            self.assertEqual(rc, 0)

            with tarfile.open(str(archive_path), "r:gz") as tar:
                names = tar.getnames()
                self.assertEqual([n for n in names if "/project/" in n], [])
                self.assertGreater(len([n for n in names if "/claude-meta/" in n]), 0)

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
            base = Path(tmpdir)

            old_project = base / "old-project"
            old_project.mkdir()
            old_path = str(old_project.resolve())
            old_encoded = encode_path(old_path)

            new_project = base / "new-project"
            new_path = str(new_project.resolve())
            new_encoded = encode_path(new_path)

            claude = base / "claude"
            old_meta = claude / "projects" / old_encoded
            old_meta.mkdir(parents=True)

            session_id = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"

            (old_meta / f"{session_id}.jsonl").write_text(
                json.dumps({"type": "message", "cwd": old_path, "sessionId": session_id}) + "\n"
            )

            sub_dir = old_meta / session_id / "subagents"
            sub_dir.mkdir(parents=True)
            (sub_dir / "agent.jsonl").write_text(json.dumps({"cwd": old_path}) + "\n")

            fh_dir = claude / "file-history" / session_id
            fh_dir.mkdir(parents=True)
            (fh_dir / "log.txt").write_text(f"edited {old_path}/main.py\n")

            se_dir = claude / "session-env" / session_id
            se_dir.mkdir(parents=True)
            (se_dir / "env.json").write_text(json.dumps({"cwd": old_path}) + "\n")

            todos_dir = claude / "todos"
            todos_dir.mkdir(parents=True)
            (todos_dir / f"{session_id}-agent-{session_id}.json").write_text(
                json.dumps({"project": old_path}) + "\n"
            )

            rc = rename(
                old_path=old_project.resolve(),
                new_path=new_project.resolve(),
                claude_dir=claude,
            )
            self.assertEqual(rc, 0)

            self.assertFalse(old_meta.exists())

            new_meta = claude / "projects" / new_encoded
            self.assertTrue(new_meta.is_dir())

            content = (new_meta / f"{session_id}.jsonl").read_text()
            self.assertIn(new_path, content)
            self.assertNotIn(old_path, content)

            sa_content = (new_meta / session_id / "subagents" / "agent.jsonl").read_text()
            self.assertIn(new_path, sa_content)
            self.assertNotIn(old_path, sa_content)

            self.assertIn(new_path, (fh_dir / "log.txt").read_text())
            self.assertIn(new_path, (se_dir / "env.json").read_text())
            self.assertIn(new_path, (todos_dir / f"{session_id}-agent-{session_id}.json").read_text())

    def test_rename_same_path_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            rc = rename(
                old_path=project.resolve(),
                new_path=project.resolve(),
                claude_dir=Path(tmpdir) / "claude",
            )
            self.assertEqual(rc, 1)

    def test_rename_no_metadata_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            claude = base / "claude"
            claude.mkdir()
            rc = rename(
                old_path=(base / "nonexistent").resolve(),
                new_path=(base / "other").resolve(),
                claude_dir=claude,
            )
            self.assertEqual(rc, 1)

    def test_rename_target_exists_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            old_project = base / "old"
            old_project.mkdir()
            new_project = base / "new"
            new_project.mkdir()

            claude = base / "claude"
            old_encoded = encode_path(str(old_project.resolve()))
            new_encoded = encode_path(str(new_project.resolve()))
            (claude / "projects" / old_encoded).mkdir(parents=True)
            (claude / "projects" / new_encoded).mkdir(parents=True)

            rc = rename(
                old_path=old_project.resolve(),
                new_path=new_project.resolve(),
                claude_dir=claude,
            )
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    main()
