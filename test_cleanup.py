import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cleanup
import config as configuration
import schedule


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'main'
        self.repo.mkdir()
        self.run_git(self.repo, 'init', '-q')
        self.run_git(self.repo, 'config', 'user.email', 'test@example.com')
        self.run_git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'package.json').write_text('{}')
        self.run_git(self.repo, 'add', '.')
        self.run_git(self.repo, 'commit', '-qm', 'Initial')
        self.worktree = self.base / 'linked'
        self.run_git(self.repo, 'worktree', 'add', '-qb', 'linked', str(self.worktree))
        for p in [self.repo, self.worktree]:
            (p / 'node_modules/pkg').mkdir(parents=True)
            (p / 'node_modules/pkg/index.js').write_text('dependency')
        self.old = time.time() - 9 * 86400
        self.cutoff = time.time() - 7 * 86400
        self.age()

    def run_git(self, path, *args):
        subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True)

    def age(self):
        for root, dirs, files in os.walk(self.base):
            for name in dirs + files:
                os.utime(Path(root) / name, (self.old, self.old), follow_symlinks=False)
        os.utime(self.base, (self.old, self.old))

    def test_discovery_distinguishes_main_checkout_and_worktree(self):
        self.assertEqual(cleanup.discover([self.base]), [self.worktree, self.repo])
        self.assertEqual(cleanup.inspect(self.repo)['kind'], 'project')
        self.assertEqual(cleanup.inspect(self.worktree)['kind'], 'worktree')

    def test_preview_does_not_delete(self):
        with patch.object(sys, 'argv', ['cleanup.py', '--root', str(self.base), '--json']), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        report = json.loads(out.getvalue())
        self.assertEqual(report['projects'][0]['status'], 'candidate')
        self.assertEqual(report['projects'][1]['status'], 'recent activity')
        self.assertEqual(report['summary']['eligible_projects'], 1)
        self.assertEqual(report['summary']['freeable_bytes_estimate'], report['projects'][0]['estimated_bytes'])
        self.assertEqual(report['summary']['freed_bytes_estimate'], 0)
        self.assertIsNone(report['summary']['available_space_change_bytes'])
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_apply_removes_root_and_workspace_modules_only(self):
        nested = self.worktree / 'packages/web/node_modules'
        nested.mkdir(parents=True)
        (nested / 'dep').write_text('dep')
        self.age()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            deleted = cleanup.remove_modules(self.worktree, self.cutoff)
        self.assertEqual(len(deleted), 2)
        self.assertFalse(nested.exists())
        self.assertFalse((self.worktree / 'node_modules').exists())
        self.assertTrue((self.worktree / 'package.json').exists())
        self.assertTrue((self.worktree / '.git').exists())
        self.assertTrue((self.repo / 'node_modules/pkg/index.js').exists())
        self.run_git(self.worktree, 'status', '--porcelain')

    def test_recent_source_and_untracked_files_protect_worktree(self):
        (self.worktree / 'new.txt').write_text('unsaved work')
        self.assertGreater(cleanup.inspect(self.worktree)['last_activity'], self.cutoff)
        with patch.object(cleanup, 'open_paths', return_value=[]), self.assertRaises(RuntimeError):
            cleanup.remove_modules(self.worktree, self.cutoff)

    def test_git_activity_protects_worktree(self):
        admin = cleanup.linked_git_dir(self.worktree)
        os.utime(admin / 'HEAD', None)
        self.assertGreater(cleanup.inspect(self.worktree)['last_activity'], self.cutoff)

    def test_git_housekeeping_does_not_reset_retention(self):
        admin = cleanup.linked_git_dir(self.worktree)
        (admin / 'FETCH_HEAD').write_text('background fetch')
        (admin / 'index.lock').write_text('background status')
        (admin / 'index.lock').unlink()
        self.assertLess(cleanup.inspect(self.worktree)['last_activity'], self.cutoff)

    def test_recent_source_skips_dependency_size_scan(self):
        (self.worktree / 'new.txt').write_text('recent source')
        row = cleanup.inspect(self.worktree, self.cutoff)
        self.assertIsNone(row['estimated_bytes'])
        self.assertEqual(row['node_modules'], [str(self.worktree / 'node_modules')])

    def test_recent_dependency_change_protects_worktree(self):
        os.utime(self.worktree / 'node_modules/pkg/index.js', None)
        self.assertGreater(cleanup.inspect(self.worktree)['last_activity'], self.cutoff)

    def test_symlinked_modules_do_not_target_external_data(self):
        external = self.base / 'external'
        external.mkdir()
        (external / 'keep').write_text('keep')
        link = self.worktree / 'packages/node_modules'
        link.parent.mkdir()
        link.symlink_to(external, target_is_directory=True)
        self.age()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_modules(self.worktree, self.cutoff)
        self.assertTrue((external / 'keep').exists())
        self.assertTrue(link.is_symlink())

    def test_open_process_protects_worktree(self):
        with patch.object(cleanup, 'open_paths', return_value=[self.worktree / 'node_modules/pkg/index.js']), \
                self.assertRaises(RuntimeError):
            cleanup.remove_modules(self.worktree, self.cutoff)
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_nested_repository_dependencies_are_kept(self):
        nested = self.worktree / 'embedded'
        nested.mkdir()
        self.run_git(nested, 'init', '-q')
        (nested / 'node_modules').mkdir()
        (nested / 'node_modules/keep').write_text('keep')
        self.age()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_modules(self.worktree, self.cutoff)
        self.assertTrue((nested / 'node_modules/keep').exists())

    def test_real_lsof_detects_process_working_directory(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=self.worktree)
        try:
            self.assertTrue(cleanup.is_open(self.worktree, cleanup.open_paths()))
        finally:
            process.terminate()
            process.wait()

    def test_real_cli_apply(self):
        result = subprocess.run(
            [sys.executable, str(Path(cleanup.__file__).resolve()),
            '--root', str(self.base), '--apply', '--json'],
            capture_output=True, text=True, check=True,
            env={**os.environ, 'TMPDIR': str(self.base)},
        )
        report = json.loads(result.stdout)
        self.assertEqual(report['projects'][0]['status'], 'removed')
        self.assertEqual(report['summary']['removed_projects'], 1)
        self.assertGreater(report['summary']['freed_bytes_estimate'], 0)
        self.assertIsInstance(report['summary']['available_space_change_bytes'], int)
        self.assertFalse((self.worktree / 'node_modules').exists())
        self.assertTrue((self.worktree / 'package.json').exists())
        self.assertTrue((self.repo / 'node_modules/pkg/index.js').exists())

    def write_config(self, extra='', policies=''):
        filename = self.base / 'broom.toml'
        filename.write_text(f'version = 1\n{extra}\n[[directories]]\npath = "{self.base}"\n{policies}')
        return filename

    def test_standalone_project_and_workspace_ownership(self):
        project = self.base / 'standalone'
        nested = project / 'packages/web'
        nested.mkdir(parents=True)
        for p in (project, nested):
            (p / 'package.json').write_text('{}')
            (p / 'node_modules').mkdir()
            (p / 'node_modules/dep').write_text('dep')
        self.old = time.time() - 25 * 86400
        self.age()
        found = cleanup.discover([self.base])
        self.assertIn(project, found)
        self.assertNotIn(nested, found)
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_modules(project, time.time() - 21 * 86400)
        self.assertTrue((project / 'package.json').exists())
        self.assertFalse((nested / 'node_modules').exists())

    def test_old_main_checkout_is_cleaned_with_project_policy(self):
        self.old = time.time() - 25 * 86400
        self.age()
        filename = self.write_config()
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--apply', '--json']), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        rows = json.loads(out.getvalue())['projects']
        self.assertEqual({r['status'] for r in rows}, {'removed'})
        self.assertEqual({r['idle_days'] for r in rows}, {7, 21})
        self.assertTrue((self.repo / '.git').is_dir())
        self.assertTrue((self.repo / 'package.json').exists())
        self.run_git(self.repo, 'status', '--porcelain')

    def test_shared_worktree_metadata_does_not_protect_main_checkout(self):
        admin = cleanup.linked_git_dir(self.worktree)
        os.utime(admin / 'HEAD', None)
        self.assertLess(cleanup.inspect(self.repo)['last_activity'], self.cutoff)

    def test_exclusions_protect_dependencies_inside_parent_project(self):
        filename = self.write_config(extra=f'exclude = ["{self.repo / "node_modules"}"]')
        config = configuration.load(filename)
        self.assertEqual(cleanup.inspect(self.repo, exclusions=config['exclude'])['node_modules'], [])
        self.assertTrue((self.repo / 'node_modules/pkg/index.js').exists())
        self.assertNotIn(self.repo, cleanup.discover([self.base], [self.repo]))

    def test_exclusion_inside_modules_protects_entire_installation(self):
        row = cleanup.inspect(self.repo, exclusions=[self.repo / 'node_modules/pkg'])
        self.assertEqual(row['node_modules'], [])

    def test_directory_override_and_disabled_policy(self):
        filename = self.write_config(policies='project_days = 28\n[policies.worktrees]\nenabled = false\n')
        config = configuration.load(filename)
        self.assertEqual(configuration.policy_for(self.repo, 'project', config)['idle_days'], 28)
        self.assertFalse(configuration.policy_for(self.worktree, 'worktree', config)['enabled'])
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--json']), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        self.assertEqual(json.loads(out.getvalue())['projects'][0]['status'], 'policy disabled')

    def test_invalid_config_is_rejected_before_scanning(self):
        cases = ['[policies.projects]\nidle_days = -1\n',
                 '[policies.projects]\nidle_days = true\n',
                 '[policies.projects]\nidle_day = 2\n',
                 '[schedule]\ntime = "25:00"\n',
                 '[schedule]\nweekdays = ["monday"]\n',
                 '[schedule]\nenabled = "yes"\n']
        for policy in cases:
            with self.subTest(policy=policy):
                filename = self.write_config(policies=policy)
                with self.assertRaises(ValueError):
                    configuration.load(filename)

    def test_schedule_uses_config_and_supports_weekly_preview(self):
        filename = self.write_config(policies='[schedule]\ntime = "03:15"\nweekdays = ["mon", "fri"]\nmode = "preview"\n')
        job = schedule.definition(filename)
        self.assertEqual(job['StartCalendarInterval'], [
            {'Hour': 3, 'Minute': 15, 'Weekday': 1},
            {'Hour': 3, 'Minute': 15, 'Weekday': 5},
        ])
        self.assertNotIn('--apply', job['ProgramArguments'])
        self.assertIn('--scheduled', job['ProgramArguments'])
        self.assertIn(str(filename), job['ProgramArguments'])
        self.assertFalse(job['RunAtLoad'])

    def test_disabled_schedule_stops_before_scan(self):
        filename = self.write_config()
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--scheduled', '--json']), \
                patch.object(cleanup, 'open_paths') as processes, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        processes.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())['mode'], 'disabled')

    def test_scheduled_mode_reads_current_config(self):
        filename = self.write_config(policies='[schedule]\nenabled = true\nmode = "preview"\n')
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--scheduled', '--apply', '--json']), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        self.assertEqual(json.loads(out.getvalue())['mode'], 'preview')
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_policy_changes_are_read_by_each_run(self):
        filename = self.write_config(policies='[policies.projects]\nidle_days = 30\n')
        self.assertEqual(configuration.load(filename)['policies']['projects']['idle_days'], 30)
        filename.write_text(filename.read_text().replace('30', '14'))
        self.assertEqual(configuration.load(filename)['policies']['projects']['idle_days'], 14)

    def test_space_summary_excludes_failed_deletions_from_freed_estimate(self):
        rows = [
            {'status': 'removed', 'estimated_bytes': 100, 'available_space_change_bytes': 60},
            {'status': 'cleanup failed', 'estimated_bytes': 200, 'available_space_change_bytes': -10},
            {'status': 'recent activity', 'estimated_bytes': None},
        ]
        result = cleanup.space_summary(rows, True)
        self.assertEqual(result['freeable_bytes_estimate'], 300)
        self.assertEqual(result['freed_bytes_estimate'], 100)
        self.assertEqual(result['available_space_change_bytes'], 50)
        rows[1]['available_space_change_bytes'] = None
        self.assertIsNone(cleanup.space_summary(rows, True)['available_space_change_bytes'])


if __name__ == '__main__':
    unittest.main()
