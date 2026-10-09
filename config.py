"""Read and validate Broom's TOML configuration without third-party packages."""
import math
from pathlib import Path
import tomllib

DEFAULT_CONFIG = Path(__file__).with_name('broom.toml')
WEEKDAYS = {'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6, 'sun': 0}


def keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f'{label}: expected a table with only {", ".join(allowed)}')


def days(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be a finite positive number')
    return value


def boolean(value, label):
    if not isinstance(value, bool):
        raise ValueError(f'{label} must be true or false')
    return value


def path(value, base):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('paths must be nonempty strings')
    result = Path(value).expanduser()
    return (result if result.is_absolute() else base / result).resolve()


def load(filename=DEFAULT_CONFIG):
    filename = Path(filename).expanduser().resolve()
    if filename == DEFAULT_CONFIG.resolve() and not filename.exists():
        filename = DEFAULT_CONFIG.with_name('broom.example.toml').resolve()
    with filename.open('rb') as source:
        raw = tomllib.load(source)
    keys(raw, ['version', 'directories', 'exclude', 'policies', 'schedule'], 'config')
    if type(raw.get('version')) is not int or raw['version'] != 1:
        raise ValueError('config version must be 1')
    policies = raw.get('policies', {})
    keys(policies, ['worktrees', 'projects'], 'policies')
    result = {'config': str(filename), 'policies': {}}
    for kind, default in [('worktrees', 7), ('projects', 21)]:
        policy = policies.get(kind, {})
        keys(policy, ['enabled', 'idle_days'], f'policies.{kind}')
        result['policies'][kind] = {
            'enabled': boolean(policy.get('enabled', True), f'{kind}.enabled'),
            'idle_days': days(policy.get('idle_days', default), f'{kind}.idle_days'),
        }
    directories = raw.get('directories')
    if not isinstance(directories, list) or not directories:
        raise ValueError('configure at least one [[directories]] entry')
    result['directories'] = []
    for entry in directories:
        keys(entry, ['path', 'worktree_days', 'project_days'], 'directories')
        root = path(entry.get('path'), filename.parent)
        if root in (Path('/'), Path.home().resolve()):
            raise ValueError('choose project folders instead of / or your entire home directory')
        rule = {'path': root}
        for key in ('worktree_days', 'project_days'):
            if key in entry:
                rule[key] = days(entry[key], key)
        if any(r['path'] == root for r in result['directories']):
            raise ValueError(f'duplicate directory: {root}')
        result['directories'].append(rule)
    excludes = raw.get('exclude', [])
    if not isinstance(excludes, list):
        raise ValueError('exclude must be a list of paths')
    result['exclude'] = [path(p, filename.parent) for p in excludes]
    schedule = raw.get('schedule', {})
    keys(schedule, ['enabled', 'time', 'weekdays', 'mode'], 'schedule')
    clock = schedule.get('time', '12:00')
    if not isinstance(clock, str) or len(clock) != 5 or clock[2] != ':' or not (clock[:2] + clock[3:]).isdigit():
        raise ValueError('schedule.time must be HH:MM')
    hour, minute = map(int, clock.split(':'))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('schedule.time must be a valid local time')
    weekdays = schedule.get('weekdays', [])
    if not isinstance(weekdays, list) or any(not isinstance(d, str) or d not in WEEKDAYS for d in weekdays) or len(set(weekdays)) != len(weekdays):
        raise ValueError('schedule.weekdays must contain unique mon, tue, wed, thu, fri, sat, or sun values')
    mode = schedule.get('mode', 'apply')
    if mode not in ('preview', 'apply'):
        raise ValueError('schedule.mode must be preview or apply')
    result['schedule'] = {'enabled': boolean(schedule.get('enabled', False), 'schedule.enabled'),
                          'hour': hour, 'minute': minute, 'weekdays': weekdays, 'mode': mode}
    return result


def excluded(path, exclusions):
    return any(path == p or p in path.parents for p in exclusions)


def policy_for(project, kind, config):
    policy = dict(config['policies']['worktrees' if kind == 'worktree' else 'projects'])
    matches = [r for r in config['directories'] if project == r['path'] or r['path'] in project.parents]
    if not matches:
        raise ValueError(f'project is outside configured directories: {project}')
    rule = max(matches, key=lambda r: len(r['path'].parts))
    key = 'worktree_days' if kind == 'worktree' else 'project_days'
    policy['idle_days'] = rule.get(key, policy['idle_days'])
    return policy
