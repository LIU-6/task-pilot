# task-pilot

`task_pilot.py` is an AI-driven task pilot that supervises an existing `tmux` agent session.

When the session goes idle, it analyzes the current state and decides how to keep unfinished tasks moving.

## Why

Interactive CLI agents often stop after a partial result, an interruption, or a progress-only reply. `task_pilot.py` adds a lightweight supervision loop on top of an existing `tmux` session so the task can keep moving without requiring constant manual nudges.

## Requirements

- Python 3.10+
- `tmux`
- one existing `tmux` session running an interactive CLI

No third-party Python packages are required.

## Quick Start

Start the target CLI inside `tmux` first:

```bash
tmux new-session -s codex_task 'codex'
```

Then run `task_pilot.py` in another shell:

```bash
python3 task_pilot.py --session codex_task
```

At startup, the process waits for a runtime command:

```text
start
```

You can also load settings from a config file:

```bash
python3 task_pilot.py --config config.example.json
```

Minimal full flow:

```bash
tmux new-session -s codex_task 'codex'
python3 task_pilot.py --session codex_task --decision-mode codex
# type: start
```

If you want to verify the supervision flow without sending anything back into tmux:

```bash
python3 task_pilot.py --session codex_task --decision-mode codex --dry-run
```

## Runtime Commands

- `start`: start automatic supervision
- `stop`: stop automatic supervision but keep the process running
- `status`: print the current runtime state

Use `Ctrl+C` to exit.

## Key Options

- `--config`: JSON config file
- `--session`: existing `tmux` session name
- `--decision-mode`: `rule` or `codex`
- `--max-continue`: maximum automatic continue cycles
- `--idle-threshold`: unchanged-output duration that counts as idle
- `--poll-interval`: polling interval in seconds
- `--history-lines`: number of `tmux` history lines captured per poll
- `--rule-continue-prompt`: fixed prompt used by `rule` mode
- `--dry-run`: analyze normally but never send prompts to `tmux`

For exact defaults:

```bash
python3 task_pilot.py --help
```

## Decision Modes

### `rule`

When the session stays idle long enough, `task_pilot.py` sends the configured `rule_continue_prompt` back into the monitored session.

### `codex`

When the session stays idle long enough, `task_pilot.py` captures a snapshot and calls internal `codex exec --json` analysis.

The analyzer returns:

- `continue`: send a follow-up prompt back into the monitored session
- `wait`: do not send anything; wait until the snapshot changes before analyzing again

## Session State

Analyzer context is stored per monitored session under:

```text
.task_pilot_sessions/
```

Each file contains the monitored session name and the analyzer session id.

## Notes

- One `task_pilot.py` process is intended to supervise one `tmux` session.
- If you want to monitor multiple sessions, run multiple `task_pilot.py` processes.
- The tool does not create `tmux` sessions or start CLIs for you.
- `codex` mode depends on a local `codex exec` command being available.
- In `--dry-run` mode, continue decisions are logged but not sent to the monitored session.
