#!/usr/bin/env python3
"""Broom: prune dependencies and build artifacts from idle projects. Preview by default."""
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
import artifacts as catalog
import activity


def git(path, *args):
    result = subprocess.run(
        ['git', '-C', str(path), *args], capture_output=True,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_LITERAL_PATHSPECS': '1'}, timeout=15,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace').strip())
    data = result.stdout.decode(errors='surrogateescape')
    return data if '\0' in data else data.strip()


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
            if repository or (catalog.is_project(files, dirs) and not owned):
                found.add(path.resolve())
            dirs[:] = [d for d in dirs if d not in {
                '.git', 'node_modules', 'target', 'vendor', '.next', '.nuxt',
                'dist', 'build', '.cache', '.turbo', '.venv',
            } and not (path / d).is_symlink() and catalog.classify(path, d, files) is None]
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
    files = [p.name for p in path.iterdir() if p.is_file() and not p.is_symlink()]
    dirs = [p.name for p in path.iterdir() if p.is_dir() and not p.is_symlink()]
    if catalog.is_project(files, dirs):
        return 'project', None
    raise RuntimeError('not a recognized project')


def git_ignored(path, target):
    result = subprocess.run(['git', '-C', str(path), 'check-ignore', '--', str(target.relative_to(path))],
                            capture_output=True, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=15)
    if result.returncode not in (0, 1):
        raise RuntimeError('could not verify ignored build output')
    return result.returncode == 0


def git_activity(path, admin):
    if admin is None:
        return None, 0.0, None
    head = (admin / 'HEAD').read_text().strip()
    try:
        commit = git(path, 'rev-parse', '--verify', 'HEAD')
    except RuntimeError:
        commit = ''  # An initialized repository can have no commits yet.
    identity = activity.fingerprint(head + '\n' + commit)
    log = admin / 'logs/HEAD'
    timestamp = 0.0
    if log.is_file():
        lines = log.read_text(errors='surrogateescape').splitlines()
        for line in reversed(lines):
            header, _, message = line.partition('\t')
            identifiers = header.split(' ', 2)
            checkout = message.removeprefix('checkout: moving from ').split(' to ', 1)
            branch_changed = (message.startswith('checkout: moving from ') and len(checkout) == 2
                              and checkout[0] != checkout[1])
            if len(identifiers) < 2 or identifiers[0] == identifiers[1] and not branch_changed:
                continue
            try:
                timestamp = float(header.rsplit(' ', 2)[-2])
            except (ValueError, IndexError):
                raise RuntimeError('cannot parse Git HEAD activity')
            break
    return identity, timestamp, 'Git checkout or commit recorded in HEAD reflog' if timestamp else None


def inspect(path, cutoff=None, exclusions=(), enabled=None, state=None, observed_at=None):
    """Keep source activity separate from each selected artifact's activity."""
    kind, admin = project_kind(path)
    if configuration.excluded(path, exclusions):
        raise RuntimeError('project is excluded')
    now = time.time() if observed_at is None else observed_at
    enabled = set(enabled if enabled is not None else ['node_modules'])
    outputs, protected, source_files, excluded_trees = [], [], {}, []
    latest, reason, recent_path = 0.0, None, None
    listed = set(git(path, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')) if admin else None
    listed = {name for name in listed if name} if listed is not None else None

    def source(entry):
        nonlocal latest, reason, recent_path
        relative = str(entry.relative_to(path))
        if entry.name in {'.git', '.DS_Store', 'Thumbs.db'} or configuration.excluded(entry, exclusions) or (state and entry == state.filename):
            return
        if listed is not None and relative not in listed and entry.name != '.keep-dependencies':
            return
        try:
            stat = entry.lstat()
        except FileNotFoundError:
            source_files[activity.fingerprint(relative)] = None
            return
        if entry.is_dir() and not entry.is_symlink():
            return
        source_files[activity.fingerprint(relative)] = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]
        if stat.st_mtime > latest:
            latest, reason, recent_path = stat.st_mtime, 'source file modified', relative

    for root, dirs, files in walk(path):
        here = Path(root)
        if configuration.excluded(here, exclusions) or (here != path and '.git' in files + dirs):
            excluded_trees.append(here)
            dirs[:] = []
            continue
        for name in files:
            source(here / name)
        kept = []
        for name in dirs:
            child = here / name
            if configuration.excluded(child, exclusions):
                excluded_trees.append(child)
                continue
            if child.is_symlink():
                source(child)
            elif name == '.git':
                excluded_trees.append(child)
            else:
                artifact_type = catalog.classify(here, name, files)
                if artifact_type == 'web' and name == 'dist' and (admin is None or not git_ignored(path, child)):
                    artifact_type = None
                if artifact_type:
                    excluded_trees.append(child)
                    if any(child in p.parents for p in exclusions):
                        protected.append({'path': str(child), 'reason': 'contains an excluded path'})
                        continue
                    tracked = git(path, 'ls-files', '-z', '--', str(child.relative_to(path))) if admin else ''
                    if tracked:
                        protected.append({'path': str(child), 'reason': 'contains tracked files'})
                        for relative in tracked.split('\0'):
                            if relative:
                                source(path / relative)
                        continue
                    if artifact_type not in enabled:
                        continue
                    outputs.append({'path': str(child), 'type': artifact_type, 'selected': True,
                                    'eligible': False, 'status': 'recent source', 'estimated_bytes': None})
                else:
                    kept.append(name)
        dirs[:] = kept
    if listed is not None:
        # Missing tracked files are absent from os.walk, but belong in the inventory.
        for relative in listed:
            entry = path / relative
            if not any(tree == entry or tree in entry.parents for tree in excluded_trees):
                source(entry)
    identity, git_time, git_reason = git_activity(path, admin)
    if git_time > latest:
        latest, reason, recent_path = git_time, git_reason, None
    if state is not None:
        scope = activity.fingerprint('\0'.join(sorted(str(p) for p in exclusions if path in p.parents)))
        observed, observed_reason = state.observe(path, source_files, identity, now, scope)
        if observed > latest:
            latest, reason, recent_path = observed, observed_reason, None
    row = {'path': str(path), 'kind': kind, 'last_activity': latest, 'activity_reason': reason,
           'activity_path': recent_path,
           'node_modules': [r['path'] for r in outputs if r['type'] == 'node_modules'],
           'artifacts': outputs, 'protected_artifacts': protected, 'estimated_bytes': None}
    if cutoff is not None and latest >= cutoff:
        return row
    size, seen = 0, set()
    for artifact in outputs:
        artifact_latest, artifact_path, blocks, inodes = 0.0, None, 0, set()
        entries = []
        for root, dirs, files in walk(Path(artifact['path'])):
            for entry in [Path(root)] + [Path(root) / n for n in files + dirs]:
                stat = entry.lstat()
                if stat.st_mtime > artifact_latest:
                    artifact_latest, artifact_path = stat.st_mtime, str(entry.relative_to(path))
                inode = (stat.st_dev, stat.st_ino)
                if inode not in inodes:
                    inodes.add(inode)
                    entries.append((inode, stat.st_blocks * 512))
                    blocks += stat.st_blocks * 512
        artifact.update(last_activity=artifact_latest, activity_path=artifact_path,
                        estimated_bytes=blocks,
                        eligible=cutoff is None or artifact_latest < cutoff)
        artifact['status'] = 'candidate' if artifact['eligible'] else 'recent artifact'
        if artifact['eligible']:
            accounted = 0
            for inode, allocation in entries:
                if inode not in seen:
                    size += allocation
                    accounted += allocation
                    seen.add(inode)
            artifact['estimated_bytes'] = accounted
    row['estimated_bytes'] = size
    return row


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
    categories = {}
    for row in eligible:
        for artifact in row.get('artifacts', []):
            if not artifact.get('eligible', True):
                continue
            item = categories.setdefault(artifact['type'], {'directories': 0, 'freeable_bytes_estimate': 0,
                                                           'freed_bytes_estimate': 0})
            item['directories'] += 1
            item['freeable_bytes_estimate'] += artifact.get('estimated_bytes') or 0
            if row['status'] == 'removed':
                item['freed_bytes_estimate'] += artifact.get('estimated_bytes') or 0
    return {
        'eligible_projects': len(eligible),
        'freeable_bytes_estimate': sum(r['estimated_bytes'] for r in eligible),
        'removed_projects': len(removed),
        'freed_bytes_estimate': sum(r['estimated_bytes'] for r in removed),
        'by_artifact': categories,
        'available_space_change_bytes': (
            sum(r['available_space_change_bytes'] for r in attempts)
            if apply and measured else None),
    }


def remove_artifacts(path, cutoff, exclusions=(), expected_kind=None, enabled=None, expected_paths=None, state=None):
    # Repeat the full activity scan immediately before changing anything.
    current = inspect(path, cutoff, exclusions, enabled, state)
    if expected_kind is not None and current['kind'] != expected_kind:
        raise RuntimeError('project kind changed during the scan')
    if current['last_activity'] >= cutoff or is_open(path, open_paths()):
        raise RuntimeError('project became active during the scan')
    removed = []
    paths = [r['path'] for r in current['artifacts'] if r['eligible']]
    if expected_paths is not None and set(paths) != set(expected_paths):
        raise RuntimeError('artifact selection changed during the scan')
    for value in paths:
        target = Path(value)
        if (target.is_symlink() or path not in target.parents
                or configuration.excluded(target, exclusions)):
            raise RuntimeError('artifact directory changed during the scan')
        # Do not resolve a path through a newly introduced symlink.
        if target.resolve() != target:
            raise RuntimeError('artifact path now contains a symlink')
        _, admin = project_kind(path)
        if admin is not None and git(path, 'ls-files', '--', str(target.relative_to(path))):
            raise RuntimeError('artifact now contains tracked files')
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


def remove_modules(path, cutoff, exclusions=(), expected_kind=None):
    return remove_artifacts(path, cutoff, exclusions, expected_kind, ['node_modules'])


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
    parser.add_argument('--artifact', action='append', choices=catalog.TYPES,
                        help='artifact types to clean for this run; repeatable, replaces config selection')
    parser.add_argument('--state-file', type=Path, help='override the private activity state file')
    args = parser.parse_args()
    try:
        config = configuration.load(args.config)
        if args.state_file:
            config['state_file'] = args.state_file.expanduser().resolve()
        if args.artifact:
            config['artifacts'] = {name: name in args.artifact for name in catalog.TYPES}
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
    enabled = [name for name, selected in config['artifacts'].items() if selected]
    now = time.time()
    rows = []
    errors = []
    try:
        state = activity.ActivityStore(config['state_file'])
        state.save()
        opened = open_paths()
        found = discover(roots, exclusions)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
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
            row = inspect(path, cutoff, exclusions, enabled, state, now)
            row['idle_days'] = policy['idle_days']
            row['status'] = ('no artifacts' if not row['artifacts'] else
                             'recent source activity' if row['last_activity'] >= cutoff else
                             'open in a process' if is_open(path, opened) else
                             'candidate' if any(a['eligible'] for a in row['artifacts']) else 'recent artifacts')
            if row['status'] == 'open in a process':
                row['estimated_bytes'] = 0
                for artifact in row['artifacts']:
                    artifact.update(eligible=False, status='open in a process')
            rows.append(row)
            if args.apply and row['status'] == 'candidate':
                before = available_bytes(path)
                try:
                    state.save()
                    row['removed'] = remove_artifacts(path, cutoff, exclusions, kind, enabled,
                                                     [r['path'] for r in row['artifacts'] if r['eligible']], state)
                    row['status'] = 'removed'
                finally:
                    after = available_bytes(path)
                    row['available_space_change_bytes'] = (
                        after - before if before is not None and after is not None else None)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            if row is not None and row['status'] == 'candidate':
                row['status'] = 'cleanup failed'
            errors.append({'path': str(path), 'error': str(error)})
    try:
        state.save()
    except (OSError, ValueError) as error:
        errors.append({'path': str(config['state_file']), 'error': f'could not save activity state: {error}'})
    summary = space_summary(rows, args.apply)
    report = {'mode': 'apply' if args.apply else 'preview', 'config': config['config'],
              'policies': config['policies'], 'artifact_types': enabled, 'roots': [str(p) for p in roots],
              'projects': rows, 'errors': errors, 'summary': summary}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in rows:
            age = (time.time() - row['last_activity']) / 86400 if row['last_activity'] else 0
            size = '      ?' if row['estimated_bytes'] is None else f"{row['estimated_bytes'] / 2**30:7.2f}"
            print(f"{row['status']:20} {size} GiB  "
                  f"{age:6.1f} days  {row['kind']:8} policy={row['idle_days']:g}d  {row['path']}")
            for protected in row.get('protected_artifacts', []):
                print(f"  Kept {protected['path']}: {protected['reason']}")
            if row.get('activity_reason'):
                detail = f", {row['activity_path']}" if row.get('activity_path') else ''
                print(f"  Project activity: {row['activity_reason']}{detail}")
            for artifact in row.get('artifacts', []):
                label = 'removed' if artifact['path'] in row.get('removed', []) else artifact['status']
                print(f"  {artifact['type']}: {label}, {artifact['path']}")
        for error in errors:
            print(f"Skipped {error['path']}: {error['error']}", file=sys.stderr)
        print(f"\nFreeable space, estimated: {summary['freeable_bytes_estimate'] / 2**30:.2f} GiB "
              f"across {summary['eligible_projects']} projects/worktrees.")
        for name, item in sorted(summary['by_artifact'].items()):
            print(f"  {name}: {item['freeable_bytes_estimate'] / 2**30:.2f} GiB "
                  f"in {item['directories']} directories")
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
