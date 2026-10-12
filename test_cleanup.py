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
import artifacts as catalog


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

    def native_fixture(self):
        cases = {
            'rust': ('Cargo.toml', '[package]\nname="demo"\nversion="0.1.0"\n', ['target']),
            'gradle': ('build.gradle', '// build script', ['build', '.gradle', '.cxx']),
            'flutter': ('pubspec.yaml', 'dependencies:\n  flutter:\n    sdk: flutter\n', ['build', '.dart_tool']),
            'ios_build': ('Podfile', '# pods', ['build', 'DerivedData']),
            'cocoapods': ('Podfile', '# pods', ['Pods']),
            'web': ('package.json', '{"dependencies":{"next":"*","expo":"*","nuxt":"*","turbo":"*"}}',
                    ['.next', '.expo', '.nuxt', '.output', '.turbo']),
            'dotnet': ('Demo.csproj', '<Project/>', ['bin', 'obj']),
            'swift': ('Package.swift', '// swift package', ['.build']),
            'cmake': ('CMakeLists.txt', '# cmake', ['build']),
        }
        expected = {}
        markers = []
        for kind, (marker, content, names) in cases.items():
            parent = self.worktree / 'native' / kind
            parent.mkdir(parents=True)
            (parent / marker).write_text(content)
            markers.append(parent / marker)
            if kind == 'cocoapods':
                (parent / 'Podfile.lock').write_text('locked pods')
                markers.append(parent / 'Podfile.lock')
            expected[kind] = []
            for name in names:
                target = parent / name
                target.mkdir()
                (target / 'generated').write_text('rebuildable')
                if kind == 'cmake':
                    (target / 'CMakeCache.txt').write_text('# generated cache')
                expected[kind].append(str(target))
        self.age()
        return expected, markers

    def test_native_artifacts_are_opt_in_and_all_types_clean_source_stays(self):
        expected, markers = self.native_fixture()
        default = cleanup.inspect(self.worktree)
        self.assertEqual({r['type'] for r in default['artifacts']}, {'node_modules'})
        row = cleanup.inspect(self.worktree, enabled=catalog.TYPES)
        self.assertEqual({r['type'] for r in row['artifacts']}, set(catalog.TYPES))
        summary = cleanup.space_summary([{**row, 'status': 'candidate'}], False)
        self.assertEqual(sum(r['freeable_bytes_estimate'] for r in summary['by_artifact'].values()), row['estimated_bytes'])
        with patch.object(cleanup, 'open_paths', return_value=[]):
            deleted = cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=catalog.TYPES)
        for paths in expected.values():
            for value in paths:
                self.assertIn(value, deleted)
                self.assertFalse(Path(value).exists())
        for marker in markers:
            self.assertTrue(marker.exists())
        self.assertTrue((self.worktree / '.git').exists())

    def test_native_category_selection(self):
        expected, _ = self.native_fixture()
        for kind in expected:
            with self.subTest(kind=kind):
                row = cleanup.inspect(self.worktree, enabled=[kind])
                self.assertEqual({r['path'] for r in row['artifacts']}, set(expected[kind]))

    def test_generic_names_are_not_artifacts_without_tool_evidence(self):
        source = self.worktree / 'source'
        for name in ('build', 'dist', 'bin', 'target', 'Pods', '.build'):
            (source / name).mkdir(parents=True)
            (source / name / 'keep').write_text('source')
        self.age()
        row = cleanup.inspect(self.worktree, enabled=catalog.TYPES)
        self.assertEqual({r['type'] for r in row['artifacts']}, {'node_modules'})

    def test_tracked_build_files_are_protected(self):
        self.native_fixture()
        tracked = self.worktree / 'native/rust/target/source.rs'
        tracked.write_text('source')
        self.run_git(self.worktree, 'add', str(tracked))
        self.age()
        row = cleanup.inspect(self.worktree, enabled=['rust'])
        self.assertEqual(row['artifacts'], [])
        self.assertEqual(row['protected_artifacts'][0]['reason'], 'contains tracked files')
        with patch.object(cleanup, 'open_paths', return_value=[]):
            self.assertEqual(cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['rust']), [])
        self.assertTrue(tracked.exists())

    def test_web_dist_requires_git_ignore(self):
        (self.worktree / 'package.json').write_text('{"scripts":{"build":"tsc"}}')
        dist = self.worktree / 'dist'
        dist.mkdir()
        (dist / 'generated').write_text('output')
        self.age()
        self.assertEqual(cleanup.inspect(self.worktree, enabled=['web'])['artifacts'], [])
        (self.worktree / '.gitignore').write_text('dist/\n')
        self.age()
        row = cleanup.inspect(self.worktree, enabled=['web'])
        self.assertEqual([r['path'] for r in row['artifacts']], [str(dist)])

    def test_artifact_tree_is_selected_once(self):
        self.native_fixture()
        nested = self.worktree / 'native/web/.next/standalone/node_modules'
        nested.mkdir(parents=True)
        (nested / 'dep').write_text('dep')
        self.age()
        row = cleanup.inspect(self.worktree, enabled=['web', 'node_modules'])
        paths = [r['path'] for r in row['artifacts']]
        self.assertNotIn(str(nested), paths)
        self.assertIn(str(nested.parents[1]), paths)

    def test_recent_disabled_artifact_protects_other_artifacts(self):
        self.native_fixture()
        (self.worktree / 'native/rust/target/generated').write_text('recent build')
        row = cleanup.inspect(self.worktree, enabled=['node_modules'])
        self.assertGreater(row['last_activity'], self.cutoff)
        with patch.object(cleanup, 'open_paths', return_value=[]), self.assertRaises(RuntimeError):
            cleanup.remove_modules(self.worktree, self.cutoff)

    def test_native_exclusion_protects_entire_artifact(self):
        self.native_fixture()
        target = self.worktree / 'native/rust/target'
        row = cleanup.inspect(self.worktree, exclusions=[target / 'generated'], enabled=['rust'])
        self.assertEqual(row['artifacts'], [])
        self.assertTrue((target / 'generated').exists())

    def test_new_tracked_file_during_preflight_blocks_deletion(self):
        self.native_fixture()
        original = cleanup.git
        calls = 0
        def changing_git(path, *args):
            nonlocal calls
            if args and args[0] == 'ls-files':
                calls += 1
                if calls >= 2:
                    return 'native/rust/target/new-source.rs'
            return original(path, *args)
        with patch.object(cleanup, 'git', side_effect=changing_git), \
                patch.object(cleanup, 'open_paths', return_value=[]), self.assertRaises(RuntimeError):
            cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['rust'])
        self.assertTrue((self.worktree / 'native/rust/target').exists())

    def test_standalone_native_projects_are_discovered(self):
        project = self.base / 'cargo-project'
        project.mkdir()
        (project / 'Cargo.toml').write_text('[package]\nname="demo"\nversion="0.1.0"')
        (project / 'target').mkdir()
        (project / 'target/generated').write_text('build')
        self.assertIn(project, cleanup.discover([self.base]))
        self.assertEqual(cleanup.project_kind(project), ('project', None))

    def test_config_artifact_switches_and_unknown_types(self):
        filename = self.write_config(policies='[artifacts]\nrust = true\nnode_modules = false\n')
        config = configuration.load(filename)
        self.assertTrue(config['artifacts']['rust'])
        self.assertFalse(config['artifacts']['node_modules'])
        self.assertFalse(config['artifacts']['cocoapods'])
        filename = self.write_config(policies='[artifacts]\neverything = true\n')
        with self.assertRaises(ValueError):
            configuration.load(filename)

    def test_native_cli_apply_reports_category_and_keeps_other_artifacts(self):
        expected, markers = self.native_fixture()
        result = subprocess.run(
            [sys.executable, str(Path(cleanup.__file__).resolve()), '--root', str(self.base),
             '--artifact', 'rust', '--apply', '--json'],
            capture_output=True, text=True, check=True,
            env={**os.environ, 'TMPDIR': str(self.base)},
        )
        report = json.loads(result.stdout)
        self.assertEqual(report['artifact_types'], ['rust'])
        self.assertGreater(report['summary']['by_artifact']['rust']['freed_bytes_estimate'], 0)
        self.assertFalse(Path(expected['rust'][0]).exists())
        self.assertTrue(Path(expected['cocoapods'][0]).exists())
        self.assertTrue((self.worktree / 'node_modules').exists())
        self.assertTrue(all(p.exists() for p in markers))

    def test_angular_cache_is_scoped_to_angular_project(self):
        parent = self.worktree / 'angular'
        target = parent / '.angular/cache'
        target.mkdir(parents=True)
        (target / 'generated').write_text('cache')
        self.age()
        self.assertEqual(cleanup.inspect(self.worktree, enabled=['web'])['artifacts'], [])
        (parent / 'angular.json').write_text('{}')
        self.age()
        row = cleanup.inspect(self.worktree, enabled=['web'])
        self.assertEqual([r['path'] for r in row['artifacts']], [str(target)])

    def test_standalone_native_project_deletion(self):
        project = self.base / 'swift-project'
        project.mkdir()
        (project / 'Package.swift').write_text('// swift package')
        output = project / '.build'
        output.mkdir()
        (output / 'object').write_text('build output')
        self.old = time.time() - 25 * 86400
        self.age()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            self.assertEqual(cleanup.remove_artifacts(project, time.time() - 21 * 86400,
                                                     enabled=['swift']), [str(output)])
        self.assertTrue((project / 'Package.swift').exists())

    def test_native_symlink_points_outside_project_are_kept(self):
        parent = self.worktree / 'rust-project'
        parent.mkdir()
        (parent / 'Cargo.toml').write_text('[package]')
        outside = self.base / 'shared-target'
        outside.mkdir()
        (outside / 'keep').write_text('shared build')
        (parent / 'target').symlink_to(outside, target_is_directory=True)
        self.age()
        self.assertEqual(cleanup.inspect(self.worktree, enabled=['rust'])['artifacts'], [])
        self.assertTrue((outside / 'keep').exists())

    def test_generated_dependency_checkouts_are_not_discovered_as_projects(self):
        self.native_fixture()
        checkout = self.worktree / 'native/swift/.build/checkouts/dependency'
        checkout.mkdir(parents=True)
        self.run_git(checkout, 'init', '-q')
        (checkout / 'Package.swift').write_text('// dependency')
        self.assertNotIn(checkout, cleanup.discover([self.base]))


if __name__ == '__main__':
    unittest.main()
