If you are like me, you have a million project folders with another million linked worktrees, each with their own `node_modules`.
This can quickly get out of hand and in my case take up almost a 1/5 of my entire disk space.

Normal disk space cleaners wont catch this, as its "important user data".
Even amazing apps like [harry0703/MangoDisk](https://github.com/harry0703/MangoDisk)
that let you configure custom clean ups cant let you express the type of "stale worktree" pruning rules that we need.

# 🧹 Broom 

Broom to the rescue!

Prune `node_modules` from idle projects and linked Git worktrees. The defaults retain dependencies for seven days in worktrees and 21 days in regular projects. Source files, Git metadata, and lockfiles stay in place.

Requires Python 3.11 or newer, Git, and `lsof`. No packages to install. The scheduler requires macOS. The cleaner uses Unix filesystem metadata and `lsof`.

## Run it

```sh
git clone https://github.com/floydnant/broom.git
cd broom
cp broom.example.toml broom.toml
python3 cleanup.py
python3 cleanup.py --apply
```

The first command previews candidates. The second deletes their dependency directories. Reinstall dependencies with the project's normal package manager when you return to a cleaned project.

Each run ends with estimated freeable space and the number of eligible projects. An apply run also reports estimated space removed by completed cleanups and the observed change in available disk space during cleanup. Failed cleanups do not count toward the removed-space estimate. Shared files and other apps writing to the disk can make the observed change differ from the estimate. JSON output includes these byte counts under `summary`.

## Configure it

Edit `broom.toml` beside the scripts. Every run reads it again. Use another config with `--config /path/to/broom.toml` on either script. Relative paths resolve against the config's directory, and `~` expands to your home directory. Unknown keys and invalid values stop the run.

The config controls:

| Setting | Meaning |
| --- | --- |
| `[[directories]]` with `path` | Folders to search recursively for projects |
| `worktree_days` or `project_days` in a directory entry | Retention overrides for projects under that directory |
| `exclude` at the top of the file | Paths whose dependencies must stay, including everything beneath them |
| `[policies.worktrees]` | `enabled` and `idle_days` for linked Git worktrees, default seven days |
| `[policies.projects]` | `enabled` and `idle_days` for regular projects, default 21 days |
| `[schedule]` | `enabled`, local `time`, `weekdays`, and `mode` |

The example scan directories are `~/projects`, `~/.t3/worktrees`, and `~/.codex/worktrees`. Change them to match your setup. Regular projects include main Git checkouts, Git submodules, and standalone folders containing `package.json`. Workspace packages belong to their containing project. Nested Git repositories have their own policy and activity checks.

For example, keep dependencies under one project and give another scan directory longer retention:

```toml
# Put exclude at the top level, before any table headers.
exclude = ["~/projects/always-keep"]

[[directories]]
path = "~/projects/work"
worktree_days = 10
project_days = 28
```

Add that exclusion to the existing list and change or add the relevant directory entry. When scan directories overlap, the most specific directory's overrides apply. An exclusion inside a `node_modules` directory protects that entire installation. Disabling a policy skips projects of that kind.

One-time command options are also available:

```sh
python3 cleanup.py --root ~/projects/work
python3 cleanup.py --days 14
python3 cleanup.py --json > report.json
```

Repeated `--root` arguments replace configured scan directories for that run. `--days` overrides both retention policies and any directory retention overrides. Policy enable switches and exclusions still apply. JSON reports use `projects` for both project kinds, with a `kind` and `idle_days` field on each row.

## Schedule it

Configure the schedule in `broom.toml`:

```toml
[schedule]
enabled = true
time = "03:15"
weekdays = ["mon", "wed", "fri"]
mode = "apply"
```

Use an empty `weekdays` list to run daily. `mode = "preview"` logs candidates without deleting dependencies.

Apply the schedule to macOS:

```sh
python3 schedule.py --sync
```

This installs or updates an enabled schedule and unloads a disabled one. `python3 schedule.py` prepares the plist without installing it. `--install` also works when `schedule.enabled` is true. `--uninstall` removes the job regardless of the config.

Retention, scan directories, exclusions, schedule mode, and the enable switch take effect on the next scheduled run because the job reads the config each time. Changing the clock time or weekdays requires `--sync` to update macOS. Setting `enabled = false` also makes any still-loaded job skip cleanup. Run `--sync` to unload it.

Installation does not immediately run cleanup. macOS can run a missed calendar job after waking. The job runs while you are logged in and writes logs under `~/Library/Logs/worktree-cleanup`. Keep the scripts and config in place while installed. The current config leaves scheduling disabled.

## What counts as activity

The cleaner checks modification times across source files, untracked files, dependency contents, build output, and relevant Git files. The newest timestamp must be older than the applicable retention policy. Git administration directory timestamps, transient locks, registration pointers, background fetch records, shared objects, remote refs, and other worktrees' metadata do not count.

It also skips projects with files or working directories open in a current-user process, then repeats the activity check before deletion. Opening or reading a file does not reliably change its modification time. To extend retention, run `touch /path/to/project/.keep-dependencies`. A future modification time also keeps the project.

It verifies Git project roots and linked worktree registration, skips symlinked dependency directories, and does not follow symlinks. An unreadable path or failed activity check skips cleanup for that project. An unavailable process check stops the run. Simultaneous runs share a lock.

Deletion renames each dependency directory before removing it. It cannot eliminate races with other software, so close editors, dev servers, and package managers before a manual cleanup. An interrupted deletion can leave a partial dependency directory. Reinstall it.

Space figures estimate allocated blocks. A question mark means source or Git activity already protects the project, so dependency measurement was unnecessary. Hard links shared with other projects or package stores and APFS clones can reduce actual space reclaimed. Large scans can take several minutes.

## Validation

```sh
python3 -m unittest -v test_cleanup.py
```

Tests use temporary real Git repositories and linked worktrees. They check different retention policies, deletion of old main checkouts, source preservation, standalone projects, workspace ownership, nested repositories, exclusions, policy switches and overrides, invalid config, schedule generation, live config changes, symlinks, and process detection with a real `lsof` call.

## Local data

Personal configuration, generated schedules, scan reports, logs, and caches stay out of Git. Reports and logs can contain absolute paths and project names. Review them before sharing.

The repository includes `broom.example.toml`. Copy it to `broom.toml` and edit your local copy. If that local config is absent, the scripts use the example config.
