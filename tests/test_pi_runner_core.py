import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pi_runner.core import (
    Config,
    IGNORED_DIRECTORIES,
    RunnerError,
    changed_files,
    sandbox_command,
    snapshot,
)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'

    def config(self, **overrides):
        data = dict(agent_id='developer', project=str(self.repo), state_dir=str(self.state),
                    pi_command=['/usr/bin/true'], runtime_paths=[], model='test', provider='test')
        data.update(overrides)
        p = self.root / 'config.json'
        p.write_text(json.dumps(data))
        return Config.load(p)

    def test_state_cannot_be_exposed_through_project(self):
        with self.assertRaises(RunnerError):
            self.config(state_dir=str(self.repo / 'state'))

    def test_only_explicit_mounts_and_clean_environment(self):
        c = self.config()
        home = self.state / 'home'
        session = self.state / 'sessions' / 'one'
        argv = sandbox_command(c, home, session)
        self.assertIn('--clearenv', argv)
        self.assertIn('--unshare-pid', argv)
        self.assertIn('--die-with-parent', argv)
        self.assertNotIn('/', [argv[i+1] for i,x in enumerate(argv[:-1]) if x == '--ro-bind'])

    def test_git_metadata_is_read_only(self):
        (self.repo / '.git').mkdir()
        argv = sandbox_command(self.config(), self.state / 'home', self.state / 'session')
        i = argv.index(str(self.repo / '.git'))
        self.assertEqual(argv[i-1], '--ro-bind')
        self.assertEqual(argv[i+1], '/workspace/.git')

    def test_read_only_project_mode(self):
        c = self.config(project_read_only=True)
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        self.assertEqual(argv[argv.index(str(self.repo))-1], '--ro-bind')

    def test_snapshot_detects_edits_deletions_and_ignored_files(self):
        (self.repo / 'deleted').write_text('before')
        (self.repo / 'edited').write_text('before')
        before = snapshot(self.repo)
        (self.repo / 'deleted').unlink()
        (self.repo / 'edited').write_text('after')
        (self.repo / 'new').write_text('new')
        self.assertEqual(changed_files(before, snapshot(self.repo)), ['deleted', 'edited', 'new'])

    def test_snapshot_ignores_heavy_and_service_directories(self):
        for name in sorted(IGNORED_DIRECTORIES):
            directory = self.repo / name
            directory.mkdir(exist_ok=True)
            (directory / 'artifact').write_text('ignored')
        nested = self.repo / 'src' / 'node_modules' / 'package'
        nested.mkdir(parents=True)
        (nested / 'index.js').write_text('ignored')
        kept = self.repo / 'kept.txt'
        kept.write_text('tracked')
        stat = kept.stat()

        self.assertEqual(snapshot(self.repo), {
            'kept.txt': {
                'type': 'file',
                'st_size': stat.st_size,
                'st_mtime_ns': stat.st_mtime_ns,
                'st_ctime_ns': stat.st_ctime_ns,
                'st_mode': stat.st_mode & 0o777,
            },
        })

    def test_current_snapshot_is_stat_only(self):
        import pi_runner.core as core

        tracked = self.repo / 'tracked.txt'
        tracked.write_text('unchanged')
        stable = self.repo / 'stable.txt'
        stable.write_text('stable')

        with patch('pi_runner.core._sha256', wraps=core._sha256) as digest:
            before = snapshot(self.repo)
            self.assertNotIn('sha256', before['tracked.txt'])
            self.assertEqual(before['tracked.txt']['st_size'], len('unchanged'))
            self.assertEqual(before['tracked.txt']['st_mtime_ns'], tracked.stat().st_mtime_ns)
            self.assertEqual(before['tracked.txt']['st_ctime_ns'], tracked.stat().st_ctime_ns)
            self.assertEqual(digest.call_count, 0)

            # An unchanged follow-up snapshot performs no content reads either.
            after_unchanged = snapshot(self.repo, previous=before)
            self.assertEqual(digest.call_count, 0)
            self.assertNotIn('sha256', after_unchanged['tracked.txt'])
            self.assertNotIn('sha256', after_unchanged['stable.txt'])
            self.assertEqual(changed_files(before, after_unchanged), [])

    def test_ctime_detects_same_size_change_after_mtime_is_restored(self):
        import os
        import time

        tracked = self.repo / 'tracked.txt'
        tracked.write_bytes(b'original')
        before = snapshot(self.repo)
        previous = tracked.stat()
        time.sleep(0.02)

        with patch('pi_runner.core._sha256', side_effect=AssertionError('unexpected content read')):
            tracked.write_bytes(b'modified')
            os.utime(tracked, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            current = tracked.stat()
            after = snapshot(self.repo, previous=before)

        self.assertEqual(current.st_size, previous.st_size)
        self.assertEqual(current.st_mtime_ns, previous.st_mtime_ns)
        self.assertNotEqual(current.st_ctime_ns, previous.st_ctime_ns)
        self.assertNotIn('sha256', after['tracked.txt'])
        self.assertEqual(changed_files(before, after), ['tracked.txt'])

    def test_legacy_string_snapshot_remains_compatible(self):
        import hashlib
        import os
        import time

        tracked = self.repo / 'tracked.txt'
        tracked.write_bytes(b'original')
        mode = tracked.stat().st_mode & 0o777
        digest = hashlib.sha256(b'original').hexdigest()
        legacy = {'tracked.txt': f'{digest}:{oct(mode)}'}

        unchanged = snapshot(self.repo, previous=legacy)
        self.assertEqual(changed_files(legacy, unchanged), [])

        previous = tracked.stat()
        time.sleep(0.02)
        tracked.write_bytes(b'modified')
        os.utime(tracked, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        updated = snapshot(self.repo, previous=legacy)

        self.assertNotEqual(updated['tracked.txt']['sha256'], digest)
        self.assertEqual(changed_files(legacy, updated), ['tracked.txt'])

    def test_inspect_uses_legacy_before_snapshot(self):
        import hashlib
        import os
        import time
        from pi_runner.cli import inspect

        tracked = self.repo / 'tracked.txt'
        tracked.write_bytes(b'original')
        mode = tracked.stat().st_mode & 0o777
        legacy = {
            'tracked.txt': f'{hashlib.sha256(b"original").hexdigest()}:{oct(mode)}',
        }
        session = self.state / 'sessions' / 'legacy'
        attempt = session / 'attempts' / '1'
        attempt.mkdir(parents=True)
        (attempt / 'before.json').write_text(json.dumps(legacy))
        (session / 'result.json').write_text(json.dumps({
            'status': 'running',
            'runner_pid': 999999,
            'runner_stamp': 'gone',
            'attempt': 1,
            'project': str(self.repo),
            'changed_files': [],
        }))

        previous = tracked.stat()
        time.sleep(0.02)
        tracked.write_bytes(b'modified')
        os.utime(tracked, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        result = inspect(session)

        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['changed_files'], ['tracked.txt'])

    def test_execute_and_inspect_detect_change_with_preserved_mtime(self):
        import os
        import time
        from pi_runner.cli import execute, inspect

        tracked = self.repo / 'tracked.txt'
        tracked.write_bytes(b'original')
        original_mtime = tracked.stat().st_mtime_ns
        fake_pi = self.root / 'fake_pi.py'
        fake_pi.write_text(
            'import json, os, time\n'
            f'target = {str(tracked)!r}\n'
            'time.sleep(0.02)\n'
            "open(target, 'wb').write(b'modified')\n"
            f'os.utime(target, ns=({original_mtime}, {original_mtime}))\n'
            'print(json.dumps({"type":"message_end","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"Done"}],"stopReason":"stop"}}), flush=True)\n'
        )
        fake_sandbox = self.root / 'fake_sandbox.py'
        fake_sandbox.write_text(
            '#!/usr/bin/python3\n'
            'import os, sys\n'
            "command = sys.argv[sys.argv.index('--') + 1:]\n"
            'os.execv(command[0], command)\n'
        )
        fake_sandbox.chmod(0o755)
        self.config(
            pi_command=['/usr/bin/python3', str(fake_pi)],
            runtime_paths=[str(fake_pi)],
        )
        task = self.root / 'task.md'
        task.write_text('change the tracked file')

        with patch('pi_runner.cli.sandbox_command', return_value=[str(fake_sandbox)]):
            result = execute(self.root / 'config.json', task)

        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['changed_files'], ['tracked.txt'])
        self.assertEqual(tracked.stat().st_mtime_ns, original_mtime)
        session = self.state / 'sessions' / result['session_id']
        self.assertEqual(inspect(session)['changed_files'], ['tracked.txt'])

        # Simulate a runner that died after changing the file again.  inspect()
        # must recompute against before.json, including the ctime fingerprint.
        time.sleep(0.02)
        tracked.write_bytes(b'tampered')
        os.utime(tracked, ns=(original_mtime, original_mtime))
        stored = json.loads((session / 'result.json').read_text())
        stored.update(status='running', runner_pid=999999, runner_stamp='gone')
        (session / 'result.json').write_text(json.dumps(stored))
        interrupted = inspect(session)

        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertEqual(interrupted['changed_files'], ['tracked.txt'])

    def test_inspect_skips_snapshot_for_read_only_session(self):
        from pi_runner.cli import inspect

        session = self.state / 'sessions' / 'read-only'
        attempt = session / 'attempts' / '1'
        attempt.mkdir(parents=True)
        (session / 'result.json').write_text(json.dumps({
            'status': 'running',
            'runner_pid': 999999,
            'runner_stamp': 'gone',
            'attempt': 1,
            'project': str(self.repo),
            'project_read_only': True,
            'changed_files': ['stale'],
        }))

        with patch('pi_runner.cli.snapshot') as snap:
            result = inspect(session)

        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['changed_files'], [])
        snap.assert_not_called()

    def test_read_only_execute_skips_before_and_after_snapshots(self):
        from pi_runner.cli import execute

        self.config(project_read_only=True)
        config_path = self.root / 'config.json'
        task = self.root / 'task.md'
        task.write_text('read-only task')

        with patch('pi_runner.cli.sandbox_command', side_effect=RunnerError('not started')), \
                patch('pi_runner.cli.snapshot') as snap:
            result = execute(config_path, task)

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['changed_files'], [])
        snap.assert_not_called()
        session = self.state / 'sessions' / result['session_id']
        self.assertFalse((session / 'attempts' / '1' / 'before.json').exists())

    def test_readonly_dependency_cannot_expose_home(self):
        with self.assertRaises(RunnerError):
            self.config(readonly_paths=[str(Path.home())])

    def test_workdir_cannot_escape_via_symlink(self):
        (self.repo / 'outside').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RunnerError):
            self.config(workdir='outside')

    def test_linked_worktree_refuses_implicit_external_mount(self):
        (self.repo / '.git').write_text('gitdir: /private/project/.git/worktrees/work\n')
        with self.assertRaises(RunnerError):
            sandbox_command(self.config(), self.state / 'home', self.state / 'session')

    def test_allow_git_commit_mounts_git_readwrite(self):
        (self.repo / '.git').mkdir()
        c = self.config(allow_git_commit=True, skills=['codespaces'])
        self.assertEqual(c.skills, ['codespaces'])
        self.assertTrue(c.allow_git_commit)
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        i = argv.index(str(self.repo / '.git'))
        self.assertEqual(argv[i-1], '--bind')
        self.assertEqual(argv[i+1], '/workspace/.git')

    def test_explicit_inbox_and_attachment_mounts(self):
        legacy = self.repo / 'inbox'
        legacy.mkdir()
        (legacy / 'photo_photo_xyz.jpg').write_bytes(b'legacy')
        view = self.root / 'turn-view'
        view.mkdir()
        store = self.root / 'conversation-store'
        store.mkdir()
        outbox = self.root / 'turn-outbox'
        outbox.mkdir()
        c = self.config(inbox_dir=str(view), attachments_dir=str(store), outbox_dir=str(outbox))
        self.assertEqual(c.inbox_dir, view.resolve())
        self.assertEqual(c.attachments_dir, store.resolve())
        self.assertEqual(c.outbox_dir, outbox.resolve())
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        self.assertEqual(argv[argv.index(str(view.resolve())) - 1], '--ro-bind')
        self.assertEqual(argv[argv.index(str(view.resolve())) + 1], '/inbox')
        self.assertEqual(argv[argv.index(str(store.resolve())) - 1], '--ro-bind')
        self.assertEqual(argv[argv.index(str(store.resolve())) + 1], '/attachments')
        self.assertEqual(argv[argv.index(str(outbox.resolve())) - 1], '--bind')
        self.assertEqual(argv[argv.index(str(outbox.resolve())) + 1], '/outbox')

    def test_explicit_inbox_must_exist(self):
        with self.assertRaises(RunnerError):
            self.config(inbox_dir=str(self.root / 'absent'))

    def test_inbox_dir_must_not_contain_project(self):
        with self.assertRaises(RunnerError):
            self.config(inbox_dir=str(self.root))

    def test_inbox_file_args_uses_selected_directory(self):
        from pi_runner.cli import inbox_file_args

        legacy = self.repo / 'inbox'
        legacy.mkdir()
        (legacy / 'legacy.jpg').write_bytes(b'legacy')
        current = self.root / 'current'
        current.mkdir()
        (current / 'current.jpg').write_bytes(b'current')

        file_args, archive = inbox_file_args(current)
        self.assertEqual(file_args, ['@/inbox/current.jpg'])
        self.assertEqual([item.name for item in archive], ['current.jpg'])
        # The legacy project inbox is not consulted when an explicit view is
        # selected; the caller passes the conversation-scoped directory.
        self.assertNotIn('legacy.jpg', ' '.join(file_args))

    def test_resolve_inbox_args_when_inbox_dir_is_none_with_repo_inbox(self):
        """When inbox_dir is None, file_args and files_to_archive are strictly empty,

        even if the project repository contains an inbox/ directory with files.
        Repository files must never be passed to pi or moved to archive/.
        """
        from pi_runner.cli import resolve_inbox_args

        # Create repository inbox with images and documents
        repo_inbox = self.repo / 'inbox'
        repo_inbox.mkdir(parents=True, exist_ok=True)
        repo_img = repo_inbox / 'design.png'
        repo_img.write_bytes(b'PNG_IMAGE_DATA')
        repo_txt = repo_inbox / 'notes.txt'
        repo_txt.write_text('Repository notes', encoding='utf-8')

        # Config with inbox_dir=None
        c = self.config()
        self.assertIsNone(c.inbox_dir)

        file_args, files_to_archive = resolve_inbox_args(c)

        # Invariants:
        # 1. file_args must be strictly empty
        self.assertEqual(file_args, [])
        # 2. files_to_archive must be strictly empty
        self.assertEqual(files_to_archive, [])
        # 3. Repository files must remain intact
        self.assertTrue(repo_img.is_file())
        self.assertTrue(repo_txt.is_file())
        # 4. No archive directory is created in repository
        self.assertFalse((repo_inbox / 'archive').exists())

    def test_resolve_inbox_args_when_inbox_dir_is_provided(self):
        """When inbox_dir is provided, file_args contains @/inbox/<name> and

        files_to_archive is strictly empty (external attachments are never archived).
        Any repository inbox directory is ignored.
        """
        from pi_runner.cli import resolve_inbox_args

        # External turn-scoped inbox
        turn_inbox = self.root / 'turn-inbox'
        turn_inbox.mkdir(parents=True, exist_ok=True)
        turn_doc = turn_inbox / 'task_attachment.pdf'
        turn_doc.write_bytes(b'%PDF_DATA')
        turn_img = turn_inbox / 'screenshot.png'
        turn_img.write_bytes(b'PNG_DATA')

        # Internal repository inbox
        repo_inbox = self.repo / 'inbox'
        repo_inbox.mkdir(parents=True, exist_ok=True)
        repo_file = repo_inbox / 'repo_internal.txt'
        repo_file.write_text('internal repo doc', encoding='utf-8')

        c = self.config(inbox_dir=str(turn_inbox))
        self.assertEqual(c.inbox_dir, turn_inbox.resolve())

        file_args, files_to_archive = resolve_inbox_args(c)

        # Invariants:
        # 1. file_args contains @/inbox/<name> for turn_inbox files
        self.assertEqual(file_args, ['@/inbox/screenshot.png', '@/inbox/task_attachment.pdf'])
        # 2. files_to_archive must be strictly empty
        self.assertEqual(files_to_archive, [])
        # 3. Repository inbox files are not included in file_args
        self.assertNotIn('@/inbox/repo_internal.txt', file_args)
        # 4. Turn inbox files are intact
        self.assertTrue(turn_doc.is_file())
        self.assertTrue(turn_img.is_file())
        self.assertFalse((turn_inbox / 'archive').exists())
        # 5. Repo inbox files are intact
        self.assertTrue(repo_file.is_file())
        self.assertFalse((repo_inbox / 'archive').exists())

    def test_inbox_not_scanned_when_inbox_dir_is_none(self):
        """When inbox_dir is None, sandbox_command does not mount /inbox."""
        legacy = self.repo / 'inbox'
        legacy.mkdir()
        (legacy / 'repo_doc.png').write_bytes(b'PNG_BYTES')
        c = self.config(inbox_dir=None)
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        self.assertNotIn('/inbox', argv)

    def test_sandbox_command_mounts_inbox_and_outbox_at_sandbox_root(self):
        """inbox_dir and outbox_dir are mounted at /inbox and /outbox, unshadowing /workspace."""
        view = self.root / 'turn-inbox'
        view.mkdir()
        out = self.root / 'turn-outbox'
        out.mkdir()
        c = self.config(inbox_dir=str(view), outbox_dir=str(out))
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')

        # Check /inbox mount
        self.assertIn(str(view.resolve()), argv)
        inbox_idx = argv.index(str(view.resolve()))
        self.assertEqual(argv[inbox_idx - 1], '--ro-bind')
        self.assertEqual(argv[inbox_idx + 1], '/inbox')

        # Check /outbox mount
        self.assertIn(str(out.resolve()), argv)
        outbox_idx = argv.index(str(out.resolve()))
        self.assertEqual(argv[outbox_idx - 1], '--bind')
        self.assertEqual(argv[outbox_idx + 1], '/outbox')

        # Ensure /workspace/inbox and /workspace/outbox are NEVER mounted
        self.assertNotIn('/workspace/inbox', argv)
        self.assertNotIn('/workspace/outbox', argv)

    def test_inbox_file_args_formats_root_inbox_paths(self):
        """inbox_file_args formats files as @/inbox/<name> instead of @/workspace/inbox/<name>."""
        from pi_runner.cli import inbox_file_args

        inbox_dir = self.root / 'turn-inbox'
        inbox_dir.mkdir()
        (inbox_dir / 'doc.txt').write_text('document')
        (inbox_dir / 'photo.png').write_bytes(b'png-bytes')

        file_args, archive = inbox_file_args(inbox_dir)
        self.assertEqual(file_args, ['@/inbox/doc.txt', '@/inbox/photo.png'])
        self.assertEqual([item.name for item in archive], ['doc.txt', 'photo.png'])
        for arg in file_args:
            self.assertTrue(arg.startswith('@/inbox/'))
            self.assertNotIn('/workspace/inbox', arg)

    def test_process_pi_stream_with_auto_retry_success(self):
        from pi_runner.cli import process_pi_stream

        lines = [
            json.dumps({"type": "session", "id": "pi-sess-1"}),
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "Connection error.",
                },
            }),
            json.dumps({
                "type": "auto_retry_start",
                "attempt": 1,
                "maxAttempts": 3,
                "delayMs": 2000,
                "errorMessage": "Connection error.",
            }),
            json.dumps({"type": "auto_retry_end", "success": True, "attempt": 1}),
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "All tasks completed successfully."}],
                    "usage": {"input": 100, "output": 50},
                },
            }),
        ]
        result = {}
        errors = []
        transient_errors = []
        usage = {}
        summary, final_response = process_pi_stream(
            lines, result, errors, transient_errors, usage
        )
        self.assertTrue(final_response)
        self.assertEqual(summary, "All tasks completed successfully.")
        self.assertEqual(errors, [])
        self.assertEqual(transient_errors, ["Connection error."])
        self.assertEqual(result.get("pi_session_id"), "pi-sess-1")
        self.assertEqual(usage.get("input"), 100)

    def test_process_pi_stream_fatal_error(self):
        from pi_runner.cli import process_pi_stream

        lines = [
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "Rate limit exceeded permanently.",
                },
            }),
        ]
        result = {}
        errors = []
        transient_errors = []
        usage = {}
        summary, final_response = process_pi_stream(
            lines, result, errors, transient_errors, usage
        )
        self.assertFalse(final_response)
        self.assertEqual(errors, ["Rate limit exceeded permanently."])
        self.assertEqual(transient_errors, ["Rate limit exceeded permanently."])


if __name__ == '__main__':
    unittest.main()


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'

    def config(self, **overrides):
        data = dict(agent_id='developer', project=str(self.repo), state_dir=str(self.state),
                    pi_command=['/usr/bin/true'], runtime_paths=[], model='test', provider='test')
        data.update(overrides)
        p = self.root / 'config.json'
        p.write_text(json.dumps(data))
        return Config.load(p)

    def test_runtime_paths_land_on_path(self):
        # An ro-bound uv binary is unreachable in the cleared sandbox
        # environment unless its own directory joins PATH.
        uv = self.root / 'tools' / 'bin' / 'uv'
        uv.parent.mkdir(parents=True)
        uv.write_text('#!/bin/sh\n')
        prefix = self.root / 'python-prefix'
        (prefix / 'bin').mkdir(parents=True)
        argv = sandbox_command(
            self.config(runtime_paths=[str(uv), str(prefix)]),
            self.state / 'home',
            self.state / 'session',
        )
        path = argv[argv.index('PATH') + 1]
        parts = path.split(':')
        self.assertIn(str(uv.parent), parts)
        self.assertIn(str(prefix / 'bin'), parts)
        self.assertIn(str(prefix), parts)
        self.assertIn('/usr/bin', parts)
        # No duplicates pollute the value.
        self.assertEqual(len(parts), len(set(parts)))
