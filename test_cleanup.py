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
import activity


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        state_default = patch.object(configuration, 'DEFAULT_STATE', self.base / 'broom-state.json')
        state_default.start()
        self.addCleanup(state_default.stop)
        original_load = configuration.load
        def isolated_config(*args, **kwargs):
            result = original_load(*args, **kwargs)
            result['state_file'] = self.base / 'broom-state.json'
            return result
        isolated_loader = patch.object(configuration, 'load', side_effect=isolated_config)
        isolated_loader.start()
        self.addCleanup(isolated_loader.stop)
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
                entry = Path(root) / name
                if '.git' in entry.parts and 'logs' in entry.parts and entry.is_file():
                    lines = []
                    for line in entry.read_text().splitlines():
                        header, separator, message = line.partition('\t')
                        fields = header.rsplit(' ', 2)
                        if len(fields) == 3:
                            header = fields[0] + ' ' + str(int(self.old)) + ' ' + fields[2]
                        lines.append(header + separator + message)
                    entry.write_text('\n'.join(lines) + '\n')
                os.utime(entry, (self.old, self.old), follow_symlinks=False)
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
        self.assertEqual(report['projects'][1]['status'], 'recent source activity')
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
        self.run_git(self.worktree, 'checkout', '-qb', 'new-branch')
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
        row = cleanup.inspect(self.worktree, self.cutoff)
        self.assertLess(row['last_activity'], self.cutoff)
        self.assertEqual(row['artifacts'][0]['status'], 'recent artifact')
        self.assertFalse(row['artifacts'][0]['eligible'])

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
             '--root', str(self.base), '--state-file', str(self.base / 'broom-state.json'), '--apply', '--json'],
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
        self.assertEqual({r['type'] for r in row['artifacts']}, set(expected) | {'node_modules'})
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

    def test_recent_disabled_artifact_does_not_protect_other_artifacts(self):
        self.native_fixture()
        (self.worktree / 'native/rust/target/generated').write_text('recent build')
        row = cleanup.inspect(self.worktree, enabled=['node_modules'])
        self.assertLess(row['last_activity'], self.cutoff)
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_modules(self.worktree, self.cutoff)
        self.assertTrue((self.worktree / 'native/rust/target').exists())

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
            if args and args[0] == 'ls-files' and '-z' not in args:
                calls += 1
                if calls >= 1:
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
             '--artifact', 'rust', '--state-file', str(self.base / 'broom-state.json'), '--apply', '--json'],
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

    def test_recent_install_keeps_only_that_artifact(self):
        expected, _ = self.native_fixture()
        os.utime(self.worktree / 'node_modules', None)
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['rust', 'node_modules'])
        by_type = {r['type']: r for r in row['artifacts']}
        self.assertLess(row['last_activity'], self.cutoff)
        self.assertFalse(by_type['node_modules']['eligible'])
        self.assertTrue(by_type['rust']['eligible'])
        summary = cleanup.space_summary([{**row, 'status': 'candidate'}], False)
        self.assertEqual(set(summary['by_artifact']), {'rust'})
        with patch.object(cleanup, 'open_paths', return_value=[]):
            deleted = cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['rust', 'node_modules'])
        self.assertEqual(deleted, expected['rust'])
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_directory_timestamps_and_index_refresh_do_not_count(self):
        state = activity.ActivityStore(self.base / 'state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, state=state)
        admin = cleanup.linked_git_dir(self.worktree)
        for path in (self.worktree, admin / 'index', admin / 'HEAD', admin / 'logs/HEAD'):
            os.utime(path, None)
        after = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(before['last_activity'], after['last_activity'])
        self.assertTrue(after['artifacts'][0]['eligible'])

    def test_cleanup_does_not_reset_source_retention(self):
        self.native_fixture()
        state = activity.ActivityStore(self.base / 'state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, enabled=['rust', 'node_modules'], state=state)
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['node_modules'], state=state)
        state.save()
        after = cleanup.inspect(self.worktree, self.cutoff, enabled=['rust'],
                                state=activity.ActivityStore(state.filename))
        self.assertEqual(before['last_activity'], after['last_activity'])
        self.assertTrue(after['artifacts'][0]['eligible'])

    def test_untracked_source_deletion_detected_once_and_persists(self):
        file = self.worktree / 'draft.ts'
        file.write_text('// source')
        self.age()
        state = activity.ActivityStore(self.base / 'state.json')
        initial = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertLess(initial['last_activity'], self.cutoff)
        file.unlink()
        deleted = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertGreater(deleted['last_activity'], self.cutoff)
        self.assertIn('deleted', deleted['activity_reason'])
        state.save()
        again = cleanup.inspect(self.worktree, self.cutoff, state=activity.ActivityStore(state.filename))
        self.assertEqual(deleted['last_activity'], again['last_activity'])

    def test_ignored_generated_file_changes_do_not_count_as_source(self):
        (self.worktree / '.gitignore').write_text('noise/\n')
        folder = self.worktree / 'noise'
        folder.mkdir()
        (folder / 'log').write_text('old log')
        self.age()
        state = activity.ActivityStore(self.base / 'state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, state=state)
        (folder / 'log').write_text('new log')
        (self.worktree / '.DS_Store').write_text('Finder metadata')
        after = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(before['last_activity'], after['last_activity'])

    def test_source_changes_with_preserved_mtime_still_count_after_baseline(self):
        state = activity.ActivityStore(self.base / 'state.json')
        cleanup.inspect(self.worktree, self.cutoff, state=state)
        file = self.worktree / 'package.json'
        old = file.stat().st_mtime_ns
        file.write_text('{"changed":true}')
        os.utime(file, ns=(old, old))
        row = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertGreater(row['last_activity'], self.cutoff)

    def test_missing_tracked_source_first_scan_protects_once(self):
        (self.worktree / 'package.json').unlink()
        state = activity.ActivityStore(self.base / 'state.json')
        first = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(first['activity_reason'], 'missing tracked source first observed')
        next_scan = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(first['last_activity'], next_scan['last_activity'])

    def test_git_branch_change_persists_activity_in_state(self):
        state = activity.ActivityStore(self.base / 'state.json')
        cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.run_git(self.worktree, 'checkout', '-qb', 'another-branch')
        row = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertGreater(row['last_activity'], self.cutoff)
        self.assertIn('Git', row['activity_reason'])

    def test_state_file_inside_project_never_counts_as_source(self):
        state = activity.ActivityStore(self.worktree / 'broom-state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, state=state)
        state.save()
        after = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(before['last_activity'], after['last_activity'])
        raw = state.filename.read_text()
        self.assertNotIn(str(self.worktree), raw)
        self.assertNotIn('package.json', raw)

    def test_corrupt_activity_state_stops_scan(self):
        state_path = self.base / 'state.json'
        state_path.write_text('{"version":1,"projects":{"bad":{}}}')
        with self.assertRaises(ValueError):
            activity.ActivityStore(state_path)

    def test_cli_partial_cleanup_keeps_recent_install_and_reports_only_old_output(self):
        expected, _ = self.native_fixture()
        os.utime(self.worktree / 'node_modules', None)
        result = subprocess.run(
            [sys.executable, str(Path(cleanup.__file__).resolve()), '--root', str(self.base),
             '--artifact', 'rust', '--artifact', 'node_modules', '--apply', '--json',
             '--state-file', str(self.base / 'state.json')],
            capture_output=True, text=True, check=True, env={**os.environ, 'TMPDIR': str(self.base)},
        )
        report = json.loads(result.stdout)
        self.assertEqual(set(report['summary']['by_artifact']), {'rust'})
        self.assertEqual(report['projects'][0]['removed'], expected['rust'])
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_open_process_is_reported_on_each_artifact(self):
        with patch.object(sys, 'argv', ['cleanup.py', '--root', str(self.base), '--json']), \
                patch.object(cleanup, 'open_paths', return_value=[self.worktree / 'package.json']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(cleanup.main())
        report = json.loads(output.getvalue())
        self.assertEqual(report['projects'][0]['status'], 'open in a process')
        self.assertFalse(report['projects'][0]['artifacts'][0]['eligible'])
        self.assertEqual(report['summary']['freeable_bytes_estimate'], 0)

    def test_no_op_git_reset_does_not_count_as_activity(self):
        state = activity.ActivityStore(self.base / 'state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.run_git(self.worktree, 'reset', '--mixed', 'HEAD')
        after = cleanup.inspect(self.worktree, self.cutoff, state=state)
        self.assertEqual(before['last_activity'], after['last_activity'])

    def test_changing_exclusions_does_not_look_like_source_deletion(self):
        folder = self.worktree / 'excluded'
        folder.mkdir()
        (folder / 'draft.ts').write_text('// old source')
        self.age()
        state = activity.ActivityStore(self.base / 'state.json')
        before = cleanup.inspect(self.worktree, self.cutoff, state=state)
        after = cleanup.inspect(self.worktree, self.cutoff, exclusions=[folder], state=state)
        self.assertEqual(before['last_activity'], after['last_activity'])


    def store_fixture(self):
        (self.worktree / 'pnpm-lock.yaml').write_text('lockfileVersion: 9.0')
        store = self.worktree / '.pnpm-store'
        (store / 'v11/files').mkdir(parents=True)
        (store / 'v11/files/unused').write_text('cache')
        self.age()
        return store

    def fake_pnpm(self):
        executable = self.base / 'pnpm-test'
        executable.write_text('#!' + sys.executable + '\n' + r"""
import json, pathlib, sys
store = pathlib.Path(sys.argv[sys.argv.index('--store-dir') + 1])
with (store.parent.parent / 'pnpm-calls.jsonl').open('a') as log:
    log.write(json.dumps({'args': sys.argv[1:], 'cwd': str(pathlib.Path.cwd())}) + '\n')
if sys.argv[-1] == 'path':
    print(store / 'v11')
else:
    (store / 'v11/files/unused').unlink()
    print('Pruned unused package')
""")
        executable.chmod(0o755)
        return executable

    def test_pnpm_preview_selects_entire_stale_worktree_store_for_removal(self):
        store = self.store_fixture()
        with patch.object(cleanup, 'prune_pnpm_store') as prune:
            row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
        prune.assert_not_called()
        self.assertGreater(row['estimated_bytes'], 0)
        artifact = row['artifacts'][0]
        self.assertEqual(artifact['path'], str(store))
        self.assertEqual(artifact['action'], 'remove')
        self.assertGreater(artifact['store_bytes'], 0)
        self.assertTrue(artifact['eligible'])

    def test_pnpm_prune_uses_explicit_store_and_isolated_cwd(self):
        store = self.store_fixture()
        (self.worktree / 'package.json').touch()
        executable = self.fake_pnpm()
        actions = []
        with patch.object(cleanup.shutil, 'which', return_value=str(executable)), \
                patch.object(cleanup, 'open_paths', return_value=[]):
            removed = cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['pnpm'], completed=actions)
        self.assertEqual(removed, [])
        self.assertTrue(store.exists())
        self.assertFalse((store / 'v11/files/unused').exists())
        self.assertEqual(actions[0]['status'], 'pruned')
        calls = [json.loads(line) for line in (self.base / 'pnpm-calls.jsonl').read_text().splitlines()]
        self.assertEqual([c['args'] for c in calls], [
            ['--store-dir', str(store), 'store', 'path'],
            ['--store-dir', str(store), 'store', 'prune']])
        self.assertTrue(all(c['cwd'] != str(self.worktree) for c in calls))

    def test_pnpm_incompatible_version_and_missing_executable_leave_store_intact(self):
        store = self.store_fixture()
        with patch.object(cleanup.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                cleanup.prune_pnpm_store(store)
        (store / 'v11').rename(store / 'v10')
        with patch.object(cleanup.shutil, 'which', return_value=str(self.fake_pnpm())):
            with self.assertRaisesRegex(RuntimeError, 'different store version'):
                cleanup.prune_pnpm_store(store)
        self.assertTrue((store / 'v10/files/unused').exists())
        calls = (self.base / 'pnpm-calls.jsonl').read_text().splitlines()
        self.assertEqual(len(calls), 1)

    def test_pnpm_open_recent_excluded_and_tracked_stores_are_not_pruned(self):
        store = self.store_fixture()
        with patch.object(cleanup, 'prune_pnpm_store') as prune:
            with patch.object(cleanup, 'open_paths', return_value=[store / 'v11/files/unused']):
                with self.assertRaisesRegex(RuntimeError, 'became active'):
                    cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['pnpm'])
            row = cleanup.inspect(self.worktree, self.cutoff, exclusions=[store / 'v11/files'], enabled=['pnpm'])
            self.assertEqual(row['artifacts'], [])
            (store / 'v11/files/unused').touch()
            row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
            self.assertTrue(row['artifacts'][0]['eligible'])
            self.assertEqual(row['artifacts'][0]['action'], 'prune')
            self.run_git(self.worktree, 'add', '-f', str(store / 'v11/files/unused'))
            self.age()
            row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
            self.assertEqual(row['artifacts'], [])
        prune.assert_not_called()

    def test_pnpm_symlinked_storage_is_rejected(self):
        store = self.store_fixture()
        (store / 'v11/files').rename(store / 'v11/elsewhere')
        (store / 'v11/files').symlink_to(store / 'v11/elsewhere', target_is_directory=True)
        with patch.object(cleanup.shutil, 'which', return_value='/fake/pnpm'):
            with self.assertRaisesRegex(RuntimeError, 'symlinked storage'):
                cleanup.prune_pnpm_store(store)

    def test_pnpm_apply_reports_pruned_not_removed_and_no_full_store_savings(self):
        store = self.store_fixture()
        (self.worktree / 'package.json').touch()
        filename = self.write_config(extra='[artifacts]\nnode_modules = false\npnpm = true')
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--apply', '--json']), \
                patch.object(cleanup.shutil, 'which', return_value=str(self.fake_pnpm())), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cleanup.main())
        report = json.loads(out.getvalue())
        row = next(r for r in report['projects'] if r['path'] == str(self.worktree))
        self.assertEqual(row['status'], 'cleaned')
        self.assertEqual(row['artifacts'][0]['status'], 'pruned')
        self.assertEqual(row['removed'], [])
        self.assertEqual(report['summary']['freed_bytes_estimate'], 0)
        self.assertEqual(report['summary']['pruned_stores'], 1)
        self.assertTrue(store.exists())

    def test_failed_prune_reports_preceding_deletions_without_full_store_savings(self):
        store = self.store_fixture()
        nested = self.worktree / 'packages/app'
        nested.mkdir(parents=True)
        (nested / 'package.json').write_text('{}')
        (nested / 'pnpm-lock.yaml').write_text('lockfileVersion: 9.0')
        store.rename(nested / '.pnpm-store')
        store = nested / '.pnpm-store'
        self.age()
        (store / 'v11/files/unused').touch()
        filename = self.write_config(extra='[artifacts]\npnpm = true')
        with patch.object(sys, 'argv', ['cleanup.py', '--config', str(filename), '--apply', '--json']), \
                patch.object(cleanup, 'prune_pnpm_store', side_effect=RuntimeError('prune failed')), \
                patch.object(cleanup, 'open_paths', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(cleanup.main())
        report = json.loads(out.getvalue())
        row = next(r for r in report['projects'] if r['path'] == str(self.worktree))
        self.assertEqual(row['status'], 'cleanup failed')
        self.assertFalse((self.worktree / 'node_modules').exists())
        self.assertTrue(store.exists())
        self.assertGreater(report['summary']['freed_bytes_estimate'], 0)
        self.assertEqual(report['summary']['pruned_stores'], 0)
        self.assertEqual(report['summary']['by_artifact']['pnpm']['freed_bytes_estimate'], 0)

    def test_pnpm_full_removal_needs_no_pnpm_and_leaves_code(self):
        store = self.store_fixture()
        with patch.object(cleanup.shutil, 'which', return_value=None), \
                patch.object(cleanup, 'open_paths', return_value=[]):
            removed = cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['pnpm'])
        self.assertEqual(removed, [str(store)])
        self.assertFalse(store.exists())
        self.assertTrue((self.worktree / 'pnpm-lock.yaml').exists())
        self.assertTrue((self.worktree / 'node_modules').exists())

    def test_pnpm_recent_source_prunes_store_without_deleting_other_artifacts(self):
        store = self.store_fixture()
        (self.worktree / 'package.json').touch()
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm', 'node_modules'])
        by_type = {a['type']: a for a in row['artifacts']}
        self.assertEqual(by_type['pnpm']['action'], 'prune')
        self.assertTrue(by_type['pnpm']['eligible'])
        self.assertFalse(by_type['node_modules']['eligible'])
        self.assertEqual(row['estimated_bytes'], 0)

    def test_pnpm_regular_project_store_is_pruned_even_when_old(self):
        store = self.store_fixture()
        store.rename(self.repo / '.pnpm-store')
        (self.worktree / 'pnpm-lock.yaml').rename(self.repo / 'pnpm-lock.yaml')
        self.age()
        row = cleanup.inspect(self.repo, self.cutoff, enabled=['pnpm'])
        self.assertEqual(row['artifacts'][0]['action'], 'prune')

    def test_pnpm_shared_virtual_store_and_external_registry_prevent_removal(self):
        store = self.store_fixture()
        links = store / 'v11/links'
        links.mkdir()
        self.age()
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
        self.assertEqual(row['artifacts'][0]['action'], 'prune')
        links.rmdir()
        registry = store / 'v11/projects'
        registry.mkdir()
        (registry / 'other').symlink_to(self.repo, target_is_directory=True)
        self.age()
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
        self.assertEqual(row['artifacts'][0]['action'], 'prune')
        self.assertIn('another project', row['artifacts'][0]['action_reason'])

    def test_pnpm_preflight_rejects_changed_action(self):
        store = self.store_fixture()
        (self.worktree / 'package.json').touch()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'action changed'):
                cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['pnpm'],
                                         expected_actions={str(store): 'remove'})
        self.assertTrue(store.exists())

    def test_pnpm_pruning_bookkeeping_does_not_extend_store_retention(self):
        store = self.store_fixture()
        (self.worktree / 'package.json').touch()
        with patch.object(cleanup.shutil, 'which', return_value=str(self.fake_pnpm())), \
                patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['pnpm'])
        (store / 'v11/index.db').write_text('prune updated index')
        source = self.worktree / 'package.json'
        os.utime(source, (self.old, self.old))
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['pnpm'])
        self.assertEqual(row['artifacts'][0]['action'], 'remove')

    def terraform_fixture(self):
        parent = self.worktree / 'infrastructure'
        parent.mkdir()
        (parent / 'main.tf').write_text('terraform {}')
        (parent / '.terraform.lock.hcl').write_text('locked providers')
        (parent / 'terraform.tfstate').write_text('infrastructure state')
        data = parent / '.terraform'
        (data / 'providers/pkg').mkdir(parents=True)
        (data / 'providers/pkg/provider').write_text('provider binary')
        (data / 'modules').mkdir()
        (data / 'modules/modules.json').write_text('{}')
        (data / 'terraform.tfstate').write_text('backend metadata')
        (data / 'environment').write_text('production')
        self.age()
        return parent, data

    def test_terraform_cleans_only_providers_preserving_metadata_and_state(self):
        parent, data = self.terraform_fixture()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            removed = cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['terraform'])
        self.assertEqual(removed, [str(data / 'providers')])
        self.assertFalse((data / 'providers').exists())
        for p in [parent / '.terraform.lock.hcl', parent / 'terraform.tfstate',
                  data / 'terraform.tfstate', data / 'environment', data / 'modules/modules.json']:
            self.assertTrue(p.is_file(), p)

    def test_terraform_provider_symlink_does_not_delete_shared_cache(self):
        parent, data = self.terraform_fixture()
        shared = self.base / 'shared-provider'
        shared.write_text('shared binary')
        (data / 'providers/shared').symlink_to(shared)
        self.age()
        with patch.object(cleanup, 'open_paths', return_value=[]):
            cleanup.remove_artifacts(self.worktree, self.cutoff, enabled=['terraform'])
        self.assertTrue(shared.is_file())

    def test_terraform_requires_configuration_and_does_not_discover_module_dependencies(self):
        parent, data = self.terraform_fixture()
        dependency = data / 'modules/downloaded'
        dependency.mkdir()
        (dependency / 'main.tf').write_text('downloaded module')
        self.assertNotIn(dependency, cleanup.discover([self.base]))
        self.assertEqual(catalog.classify(parent, 'providers', []), None)
        (parent / 'main.tf').unlink()
        self.assertEqual(cleanup.inspect(self.worktree, enabled=['terraform'])['artifacts'], [])

    def test_terraform_recent_provider_does_not_protect_other_artifacts(self):
        _, data = self.terraform_fixture()
        (data / 'providers/pkg/provider').touch()
        row = cleanup.inspect(self.worktree, self.cutoff, enabled=['terraform', 'node_modules'])
        self.assertLess(row['last_activity'], self.cutoff)
        by_type = {a['type']: a for a in row['artifacts']}
        self.assertFalse(by_type['terraform']['eligible'])
        self.assertTrue(by_type['node_modules']['eligible'])


if __name__ == '__main__':
    unittest.main()
