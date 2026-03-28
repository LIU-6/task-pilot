# task-pilot

`task_pilot.py` 是一个面向现有 `tmux` agent 会话的 AI 任务推进器。

当会话进入 idle 状态时，它会分析当前上下文，并决定如何继续推进未完成任务。

## 用途

很多交互式 CLI agent 会在阶段性结果、中断提示或进展说明后停下来。`task_pilot.py` 的作用是在现有 `tmux` 会话外再加一层轻量监督，让任务继续推进，而不是每次都靠人工补一句“继续执行”。

## 运行要求

- Python 3.10+
- `tmux`
- 一个已经在 `tmux` 会话里运行的交互式 CLI

不需要额外安装第三方 Python 依赖。

## 快速开始

先在 `tmux` 里启动目标 CLI：

```bash
tmux new-session -s codex_task 'codex'
```

然后在另一个 shell 里启动 `task_pilot.py`：

```bash
python3 task_pilot.py --session codex_task
```

启动后，进程会先等待一条运行时命令：

```text
start
```

也可以使用配置文件：

```bash
python3 task_pilot.py --config config.example.json
```

一个最小完整流程：

```bash
tmux new-session -s codex_task 'codex'
python3 task_pilot.py --session codex_task --decision-mode codex
# 输入：start
```

如果你只想验证监督和分析流程、但不希望真的向 tmux 会话发送内容：

```bash
python3 task_pilot.py --session codex_task --decision-mode codex --dry-run
```

## 运行时命令

- `start`：开始自动监督
- `stop`：停止自动监督，但进程继续运行
- `status`：打印当前运行状态

退出使用 `Ctrl+C`。

## 关键参数

- `--config`：JSON 配置文件
- `--session`：已有 `tmux` 会话名
- `--decision-mode`：`rule` 或 `codex`
- `--max-continue`：最大自动推进轮数
- `--idle-threshold`：输出持续不变多久算 idle
- `--poll-interval`：轮询间隔秒数
- `--history-lines`：每次轮询抓取的 `tmux` 历史行数
- `--rule-continue-prompt`：`rule` 模式使用的固定提示
- `--dry-run`：照常分析，但不向 `tmux` 发送提示

精确默认值请看：

```bash
python3 task_pilot.py --help
```

## 决策模式

### `rule`

当会话持续 idle 达到阈值时，`task_pilot.py` 会把配置好的 `rule_continue_prompt` 发送回被监控会话。

### `codex`

当会话持续 idle 达到阈值时，`task_pilot.py` 会先抓取快照，再内部调用 `codex exec --json` 进行分析。

分析器只会返回：

- `continue`：发送一条后续提示回被监控会话
- `wait`：不发送任何内容，等快照变化后才允许再次分析

## 会话状态

分析器上下文会按被监控会话分别保存在：

```text
.task_pilot_sessions/
```

每个文件保存对应的会话名和分析器 session id。

## 说明

- 一个 `task_pilot.py` 进程只面向一个 `tmux` 会话。
- 如果要监督多个会话，请启动多个 `task_pilot.py` 进程。
- 这个工具不会替你创建 `tmux` 会话，也不会替你启动 CLI。
- `codex` 模式要求本机可以直接运行 `codex exec`。
- `--dry-run` 模式下，continue 决策只会记录日志，不会真正写回被监控会话。
