#!/usr/bin/env python3
"""Prepare or synchronize Broom's macOS schedule from its TOML config."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

import config as configuration

LABEL = 'local.worktree-node-modules-cleanup'


def definition(filename=configuration.DEFAULT_CONFIG):
    config = configuration.load(filename)
    schedule = config['schedule']
    python = shutil.which('python3')
    if not python:
        raise RuntimeError('python3 is not available')
    script = Path(__file__).resolve().with_name('cleanup.py')
    args = [python, str(script), '--config', config['config'], '--scheduled']
    interval = {'Hour': schedule['hour'], 'Minute': schedule['minute']}
    if schedule['weekdays']:
        interval = [{**interval, 'Weekday': configuration.WEEKDAYS[d]} for d in schedule['weekdays']]
    logs = Path.home() / 'Library/Logs/worktree-cleanup'
    return {
        'Label': LABEL,
        'ProgramArguments': args,
        'StartCalendarInterval': interval,
        'RunAtLoad': False,
        'ProcessType': 'Background',
        'StandardOutPath': str(logs / 'cleanup.log'),
        'StandardErrorPath': str(logs / 'cleanup-errors.log'),
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
    }


def disable(domain, installed):
    subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
    status = subprocess.run(['launchctl', 'print', f'{domain}/{LABEL}'], capture_output=True)
    if status.returncode == 0:
        raise RuntimeError('launchctl could not disable the existing cleanup job')
    installed.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=configuration.DEFAULT_CONFIG)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--install', action='store_true', help='install an enabled config schedule')
    action.add_argument('--sync', action='store_true', help='enable, update, or disable according to config')
    action.add_argument('--uninstall', action='store_true', help='disable and remove this job')
    args = parser.parse_args()
    installed = Path.home() / f'Library/LaunchAgents/{LABEL}.plist'
    domain = f'gui/{os.getuid()}'
    try:
        if args.uninstall:
            disable(domain, installed)
            print('Scheduled cleanup disabled.')
            return
        config = configuration.load(args.config)
        if args.sync and not config['schedule']['enabled']:
            disable(domain, installed)
            print('Scheduled cleanup disabled according to config.')
            return
        if args.install and not config['schedule']['enabled']:
            parser.error('set schedule.enabled = true in the config before installing')
        data = definition(args.config)
        install = args.install or args.sync
        destination = installed if install else Path(__file__).resolve().with_name(f'{LABEL}.plist')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if install:
            Path(data['StandardOutPath']).parent.mkdir(parents=True, exist_ok=True)
            disable(domain, installed)
        destination.write_bytes(plistlib.dumps(data))
        if install:
            subprocess.run(['launchctl', 'bootstrap', domain, str(destination)], check=True)
            print(f"Schedule enabled at {config['schedule']['hour']:02}:{config['schedule']['minute']:02}, "
                  f"mode={config['schedule']['mode']}. Config: {config['config']}")
        else:
            print(f'Prepared {destination}. Not installed. Set schedule.enabled = true and run --sync to enable.')
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Schedule failed: {error}\n')


if __name__ == '__main__':
    main()
