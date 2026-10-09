#!/usr/bin/env python3
"""Broom: prune node_modules from idle projects and worktrees. Preview by default."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import config as configuration


def git(path, *args):
    result = subprocess.run(
        ['git', '-C', str(path), *args], capture_output=True,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=15,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace').strip())
    return result.stdout.decode(errors='surrogateescape').strip()


def walk(path):
    def fail(error):
        raise error
    return os.walk(path, followlinks=False, onerror=fail)


def discover(roots, exclusions=()):
    """Discover repositories without walking dependency or generated directories."""
    found = set()
    for base in sorted(roots, key=lambda p: len(p.parts)):
        if not base.exists():
            continue
        for root, dirs, files in walk(base):
            path = Path(root)
            if configuration.excluded(path, exclusions):
                dirs[:] = []
                continue
            repository = '.git' in files + dirs
            owned = any(p in path.parents for p in found)
            if repository or ('package.json' in files and not owned):
                found.add(path.resolve())
            dirs[:] = [d for d in dirs if d not in {
                '.git', 'node_modules', 'target', 'vendor', '.next', '.nuxt',
                'dist', 'build', '.cache', '.turbo', '.venv',
            } and not (path / d).is_symlink()]
    return sorted(found)


def linked_git_dir(path):
    if not (path / '.git').is_file() or (path / '.git').is_symlink():
        raise RuntimeError('not a linked worktree')
    admin = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve()
    common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if admin == common or admin.parent != common / 'worktrees':
        raise RuntimeError('not a linked worktree')
    backpointer = admin / 'gitdir'
    if Path(backpointer.read_text().strip()).resolve() != path / '.git':
        raise RuntimeError('Git worktree registration does not match')
    return admin


def project_kind(path):
    """Validate repository metadata, or recognize a standalone Node project."""
    marker = path / '.git'
    if marker.is_symlink():
        raise RuntimeError('symlinked Git metadata is not supported')
    if marker.is_file():
        admin = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve()
        common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        if admin != common:
            return 'worktree', linked_git_dir(path)
        # A submodule or checkout with separate metadata uses the project policy.
        if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
            raise RuntimeError('Git project root does not match')
        return 'project', admin
    if marker.is_dir():
        admin = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve()
        if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
            raise RuntimeError('Git project root does not match')
        return 'project', admin
    if (path / 'package.json').is_file() and not (path / 'package.json').is_symlink():
        return 'project', None
    raise RuntimeError('not a recognized project')


def inspect(path, cutoff=None, exclusions=()):
    """Scan source, dependencies, and Git files belonging to this project."""
    kind, admin = project_kind(path)
    if configuration.excluded(path, exclusions):
        raise RuntimeError('project is excluded')
    latest = 0.0
    modules = []
    for root, dirs, files in walk(path):
        here = Path(root)
        if configuration.excluded(here, exclusions):
            dirs[:] = []
            continue
        if here != path and '.git' in files + dirs:
            dirs[:] = []  # A nested repository owns its own dependencies.
            continue
        latest = max(latest, here.stat().st_mtime)
        for name in files:
            latest = max(latest, (here / name).lstat().st_mtime)
        kept = []
        for name in dirs:
            child = here / name
            if configuration.excluded(child, exclusions):
                continue
            if name == 'node_modules' and any(child in p.parents for p in exclusions):
                continue
            if child.is_symlink():
                latest = max(latest, child.lstat().st_mtime)
            elif name == '.git':
                continue
            elif name == 'node_modules':
                modules.append(child)
            else:
                kept.append(name)
        dirs[:] = kept
    for root, dirs, files in walk(admin) if admin else []:
        here = Path(root)
        # Shared objects, other worktrees, and remote refs are not this project's activity.
        dirs[:] = [d for d in dirs if d not in {'objects', 'worktrees', 'modules', 'remotes'}
                   and not (here / d).is_symlink()]
        # Status checks create/remove locks, changing directory timestamps.
        # Background fetches also do not mean this worktree was used.
        for name in files:
            if name in {'FETCH_HEAD', 'gitdir', 'commondir'} or name.endswith('.lock'):
                continue
            latest = max(latest, (here / name).lstat().st_mtime)
    if cutoff is not None and latest >= cutoff:
        return {'path': str(path), 'kind': kind, 'last_activity': latest,
                'node_modules': [str(p) for p in modules], 'estimated_bytes': None}
    size = 0
    seen = set()
    for module in modules:
        for root, dirs, files in walk(module):
            for entry in [Path(root)] + [Path(root) / n for n in files]:
                stat = entry.lstat()
                latest = max(latest, stat.st_mtime)
                inode = (stat.st_dev, stat.st_ino)
                if inode not in seen:
                    size += stat.st_blocks * 512
                    seen.add(inode)
            for name in dirs:
                latest = max(latest, (Path(root) / name).lstat().st_mtime)
    return {'path': str(path), 'kind': kind, 'last_activity': latest,
            'node_modules': [str(p) for p in modules], 'estimated_bytes': size}


def open_paths():
    """Check current-user open files and working directories without reading content."""
    executable = shutil.which('lsof') or '/usr/sbin/lsof'
    result = subprocess.run([executable, '-nP', '-u', str(os.getuid()), '-Fn'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError('cannot reliably check open files: ' + result.stderr.strip())
    paths = []
    for line in result.stdout.splitlines():
        if line.startswith('n/'):
            paths.append(Path(line[1:].removesuffix(' (deleted)')))
    return paths


def is_open(path, opened):
    return any(p == path or path in p.parents for p in opened)


def available_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def space_summary(rows, apply):
    eligible = [r for r in rows if r['status'] in ('candidate', 'removed', 'cleanup failed')]
    removed = [r for r in rows if r['status'] == 'removed']
    attempts = [r for r in rows if 'available_space_change_bytes' in r]
    measured = all(r['available_space_change_bytes'] is not None for r in attempts)
    return {
        'eligible_projects': len(eligible),
        'freeable_bytes_estimate': sum(r['estimated_bytes'] for r in eligible),
        'removed_projects': len(removed),
        'freed_bytes_estimate': sum(r['estimated_bytes'] for r in removed),
        'available_space_change_bytes': (
            sum(r['available_space_change_bytes'] for r in attempts)
            if apply and measured else None),
    }


def remove_modules(path, cutoff, exclusions=(), expected_kind=None):
    # Repeat the full activity scan immediately before changing anything.
    current = inspect(path, cutoff, exclusions)
    if expected_kind is not None and current['kind'] != expected_kind:
        raise RuntimeError('project kind changed during the scan')
    if current['last_activity'] >= cutoff or is_open(path, open_paths()):
        raise RuntimeError('project became active during the scan')
    removed = []
    for value in current['node_modules']:
        target = Path(value)
        if (target.name != 'node_modules' or target.is_symlink() or path not in target.parents
                or configuration.excluded(target, exclusions)):
            raise RuntimeError('dependency directory changed during the scan')
        # Do not resolve a path through a newly introduced symlink.
        if target.resolve() != target:
            raise RuntimeError('dependency path now contains a symlink')
        staged = target.with_name('.worktree-cleanup-' + uuid.uuid4().hex)
        target.rename(staged)
        try:
            shutil.rmtree(staged)
        except BaseException:
            if staged.exists() and not target.exists():
                staged.rename(target)
            raise
        removed.append(value)
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=configuration.DEFAULT_CONFIG,
                        help='TOML config, defaults to broom.toml beside this script')
    parser.add_argument('--root', action='append', type=Path,
                        help='discovery root, repeatable; replaces default roots')
    parser.add_argument('--days', type=float, help='override both retention thresholds for this run')
    parser.add_argument('--apply', action='store_true', help='delete the listed dependency directories')
    parser.add_argument('--scheduled', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--json', action='store_true', help='emit a machine-readable report')
    args = parser.parse_args()
    try:
        config = configuration.load(args.config)
        if args.scheduled:
            if not config['schedule']['enabled']:
                print(json.dumps({'mode': 'disabled', 'projects': [], 'errors': []})
                      if args.json else 'Scheduled cleanup disabled in config.')
                return 0
            args.apply = config['schedule']['mode'] == 'apply'
        if args.days is not None:
            configuration.days(args.days, '--days')
            for policy in config['policies'].values():
                policy['idle_days'] = args.days
            for rule in config['directories']:
                rule.pop('worktree_days', None)
                rule.pop('project_days', None)
        if args.root:
            config['directories'] = [{'path': p.expanduser().resolve()} for p in args.root]
            if any(r['path'] in (Path('/'), Path.home().resolve()) for r in config['directories']):
                raise ValueError('choose project folders instead of / or your entire home directory')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    roots = [r['path'] for r in config['directories']]
    exclusions = config['exclude']
    now = time.time()
    rows = []
    errors = []
    try:
        opened = open_paths()
        found = discover(roots, exclusions)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Scan stopped: {error}\n')
    for path in found:
        row = None
        try:
            print(f'Checking {path}', file=sys.stderr, flush=True)
            kind, _ = project_kind(path)
            policy = configuration.policy_for(path, kind, config)
            if not policy['enabled']:
                rows.append({'path': str(path), 'kind': kind, 'status': 'policy disabled',
                             'idle_days': policy['idle_days'], 'estimated_bytes': None,
                             'last_activity': None, 'node_modules': []})
                continue
            cutoff = now - policy['idle_days'] * 86400
            row = inspect(path, cutoff, exclusions)
            row['idle_days'] = policy['idle_days']
            row['status'] = ('no dependencies' if not row['node_modules'] else
                             'recent activity' if row['last_activity'] >= cutoff else
                             'open in a process' if is_open(path, opened) else 'candidate')
            rows.append(row)
            if args.apply and row['status'] == 'candidate':
                before = available_bytes(path)
                try:
                    row['removed'] = remove_modules(path, cutoff, exclusions, kind)
                    row['status'] = 'removed'
                finally:
                    after = available_bytes(path)
                    row['available_space_change_bytes'] = (
                        after - before if before is not None and after is not None else None)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            if row is not None and row['status'] == 'candidate':
                row['status'] = 'cleanup failed'
            errors.append({'path': str(path), 'error': str(error)})
    summary = space_summary(rows, args.apply)
    report = {'mode': 'apply' if args.apply else 'preview', 'config': config['config'],
              'policies': config['policies'], 'roots': [str(p) for p in roots],
              'projects': rows, 'errors': errors, 'summary': summary}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in rows:
            age = (time.time() - row['last_activity']) / 86400 if row['last_activity'] else 0
            size = '      ?' if row['estimated_bytes'] is None else f"{row['estimated_bytes'] / 2**30:7.2f}"
            print(f"{row['status']:20} {size} GiB  "
                  f"{age:6.1f} days  {row['kind']:8} policy={row['idle_days']:g}d  {row['path']}")
        for error in errors:
            print(f"Skipped {error['path']}: {error['error']}", file=sys.stderr)
        print(f"\nFreeable space, estimated: {summary['freeable_bytes_estimate'] / 2**30:.2f} GiB "
              f"across {summary['eligible_projects']} projects/worktrees.")
        if args.apply:
            print(f"Freed space, estimated: {summary['freed_bytes_estimate'] / 2**30:.2f} GiB "
                  f"across {summary['removed_projects']} completed cleanups.")
            change = summary['available_space_change_bytes']
            print('Available disk space change: unavailable.' if change is None else
                  f'Available disk space change during cleanup: {change / 2**30:+.2f} GiB.')
            print('Shared files and other disk activity can affect the space actually reclaimed.')
        else:
            print('Preview only. Use --apply to delete.')
    return bool(errors)


if __name__ == '__main__':
    # Share one lock across manual and scheduled runs.
    with open(Path(tempfile.gettempdir()) / f'worktree-cleanup-{os.getuid()}.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Another cleanup is already running.')
        sys.exit(main())
