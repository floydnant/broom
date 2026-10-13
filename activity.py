"""Private source inventories for detecting changes without directory timestamps."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

DEFAULT_STATE = Path.home() / '.local/state/broom/activity.json'


def fingerprint(value):
    return hashlib.sha256(str(value).encode('utf-8', errors='surrogateescape')).hexdigest()


class ActivityStore:
    def __init__(self, filename):
        self.filename = Path(filename).expanduser().resolve()
        self.data = {'version': 1, 'projects': {}}
        if self.filename.exists():
            self.data = json.loads(self.filename.read_text())
            if not isinstance(self.data, dict) or self.data.get('version') != 1 or not isinstance(self.data.get('projects'), dict):
                raise ValueError('invalid activity state file; restore it or choose a new state_file')
            for record in self.data['projects'].values():
                if (not isinstance(record, dict) or set(record) - {'files', 'git_identity', 'observed_activity', 'reason', 'scope'}
                        or not {'files', 'git_identity', 'observed_activity', 'reason'} <= set(record)
                        or not isinstance(record['files'], dict)
                        or not isinstance(record['observed_activity'], (int, float))
                        or not math.isfinite(record['observed_activity']) or record['observed_activity'] < 0
                        or record['git_identity'] is not None and not isinstance(record['git_identity'], str)
                        or record['reason'] is not None and not isinstance(record['reason'], str)):
                    raise ValueError('invalid project record in activity state')

    def observe(self, project, files, git_identity, now, scope=None):
        key = fingerprint(project)
        previous = self.data['projects'].get(key)
        current = {'files': files, 'git_identity': git_identity, 'observed_activity': 0,
                   'reason': None, 'scope': scope}
        if previous is not None:
            current['observed_activity'] = previous['observed_activity']
            current['reason'] = previous['reason']
            if previous.get('scope') == scope and previous['files'] != files:
                current.update(observed_activity=now, reason='source files added, changed, or deleted since previous scan')
            if previous['git_identity'] != git_identity:
                current.update(observed_activity=now, reason='Git branch or commit changed since previous scan')
        elif any(value is None for value in files.values()):
            current.update(observed_activity=now, reason='missing tracked source first observed')
        self.data['projects'][key] = current
        return current['observed_activity'], current['reason']

    def save(self):
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.broom-state-', dir=self.filename.parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                json.dump(self.data, output)
            os.replace(temporary, self.filename)
        finally:
            Path(temporary).unlink(missing_ok=True)
