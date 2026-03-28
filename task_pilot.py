#!/usr/bin/env python3
import asyncio
import argparse
import contextlib
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional


DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_IDLE_THRESHOLD_SECONDS = 10.0
DEFAULT_MAX_CONTINUE_COUNT = 5
DEFAULT_HISTORY_LINES = 300
MAX_CODEX_ANALYSIS_RETRIES = 3
SESSION_STATE_DIR = Path(".task_pilot_sessions")
DEFAULT_CONTINUE_PROMPT = (
    "Please continue the current task. If the goal is not finished yet, "
    "keep going and do not stop early."
)
CONFIG_VALUE_KEYS = (
    "session",
    "decision_mode",
    "max_continue",
    "idle_threshold",
    "poll_interval",
    "history_lines",
    "rule_continue_prompt",
)
CONFIG_BOOL_KEYS = ("dry_run",)

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def log_status(message: str, *args: object) -> None:
    logger.info("[status] " + message, *args)


def log_action(message: str, *args: object) -> None:
    logger.info("[action] " + message, *args)


def log_error(message: str, *args: object) -> None:
    logger.error("[error] " + message, *args)


def log_done(message: str, *args: object) -> None:
    logger.info("[done] " + message, *args)


@dataclass
class PilotState:
    last_snapshot_hash: Optional[str] = None
    last_changed_at: float = 0.0
    continue_count: int = 0
    started: bool = False
    waiting_for_snapshot_change: bool = False
    session_missing: bool = False
    restart_on_session_return: bool = False
    analyzer_session_id: str = ""


@dataclass(frozen=True)
class PilotConfig:
    session_name: str
    decision_mode: str
    max_continue_count: int
    idle_threshold: float
    poll_interval: float
    history_lines: int
    rule_continue_prompt: str
    dry_run: bool


@dataclass(frozen=True)
class RuntimeCommand:
    name: str
    known: bool = True


@dataclass(frozen=True)
class PilotObservation:
    snapshot: str
    idle_for: float
    continue_count: int


@dataclass(frozen=True)
class PilotDecision:
    action: str
    next_continue_count: Optional[int] = None
    prompt: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class SessionSnapshot:
    text: str
    hash: str
    captured_at: float


@dataclass(frozen=True)
class CodexAnalysisResult:
    resolved_session_id: str
    last_message: str


@dataclass(frozen=True)
class AnalyzerSessionStore:
    root: Path = SESSION_STATE_DIR

    def path_for(self, session_name: str) -> Path:
        return self.root / f"{session_name}.json"

    def load(self, session_name: str) -> str:
        try:
            path = self.path_for(session_name)
            if not path.exists():
                return ""
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(
                "analysis_session_state_load_failed session=%s", session_name
            )
            return ""
        if not isinstance(payload, dict):
            return ""
        value = payload.get("analyzer_session_id")
        if not isinstance(value, str):
            return ""
        return value

    def save(self, session_name: str, analyzer_session_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path_for(session_name).write_text(
            json.dumps(
                {"session": session_name, "analyzer_session_id": analyzer_session_id},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


@dataclass(frozen=True)
class TmuxSession:
    session_name: str
    history_lines: int

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            check=False,
            capture_output=True,
            text=True,
        )

    async def run_async(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(self.run, args)

    def exists(self) -> bool:
        return self.run(["has-session", "-t", self.session_name]).returncode == 0

    async def exists_async(self) -> bool:
        return (await self.run_async(["has-session", "-t", self.session_name])).returncode == 0

    def capture_snapshot(self) -> str:
        start = f"-{self.history_lines}"
        result = self.run(["capture-pane", "-t", self.session_name, "-p", "-S", start])
        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown tmux error"
            raise RuntimeError(f"tmux capture-pane failed: {stderr}")
        return result.stdout

    def capture_normalized_snapshot(self) -> str:
        return normalize_snapshot(self.capture_snapshot())

    async def capture_normalized_snapshot_async(self) -> str:
        return await asyncio.to_thread(self.capture_normalized_snapshot)

    def capture_session_snapshot(self) -> SessionSnapshot:
        text = self.capture_normalized_snapshot()
        return SessionSnapshot(text=text, hash=snapshot_hash(text), captured_at=time.monotonic())

    async def capture_session_snapshot_async(self) -> SessionSnapshot:
        return await asyncio.to_thread(self.capture_session_snapshot)

    def current_path(self) -> Path:
        result = self.run(
            ["display-message", "-p", "-t", self.session_name, "#{pane_current_path}"]
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown tmux error"
            raise RuntimeError(f"tmux display-message failed: {stderr}")
        path = result.stdout.strip()
        if not path:
            raise RuntimeError("tmux did not return pane_current_path")
        return Path(path).resolve()

    async def current_path_async(self) -> Path:
        return await asyncio.to_thread(self.current_path)

    async def send_prompt_async(self, prompt: str) -> None:
        literal_result = await self.run_async(["send-keys", "-t", self.session_name, "-l", prompt])
        if literal_result.returncode != 0:
            stderr = literal_result.stderr.strip() or "unknown tmux error"
            raise RuntimeError(f"tmux send-keys failed while sending prompt: {stderr}")
        await asyncio.sleep(0.1)
        enter_result = await self.run_async(["send-keys", "-t", self.session_name, "Enter"])
        if enter_result.returncode != 0:
            stderr = enter_result.stderr.strip() or "unknown tmux error"
            raise RuntimeError(f"tmux send-keys failed while sending Enter: {stderr}")


@dataclass
class PilotRuntime:
    config: PilotConfig
    state: PilotState
    tmux: TmuxSession
    session_store: AnalyzerSessionStore
    analysis_task: asyncio.Task[PilotDecision] | None = None
    analysis_cancel_reason: str = ""
    shutting_down: bool = False


def config_key_to_flag(key: str) -> str:
    return f"--{key}"


def normalize_legacy_flags(argv: list[str]) -> list[str]:
    legacy_flags = {
        "--decision-mode": "--decision_mode",
        "--max-continue": "--max_continue",
        "--idle-threshold": "--idle_threshold",
        "--poll-interval": "--poll_interval",
        "--history-lines": "--history_lines",
        "--rule-continue-prompt": "--rule_continue_prompt",
        "--dry-run": "--dry_run",
    }
    normalized: list[str] = []
    for arg in argv:
        if arg in legacy_flags:
            normalized.append(legacy_flags[arg])
            continue
        for legacy_flag, new_flag in legacy_flags.items():
            if arg.startswith(f"{legacy_flag}="):
                normalized.append(new_flag + arg[len(legacy_flag) :])
                break
        else:
            normalized.append(arg)
    return normalized


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise RuntimeError("Config file must contain a top-level JSON object")
    return data


def config_to_argv(config: dict) -> list[str]:
    argv: list[str] = []
    for key in CONFIG_VALUE_KEYS:
        if key in config and config[key] is not None:
            argv.extend([config_key_to_flag(key), str(config[key])])
    for key in CONFIG_BOOL_KEYS:
        if config.get(key) is True:
            argv.append(config_key_to_flag(key))
    return argv


def merge_config_args(argv: list[str]) -> list[str]:
    argv = normalize_legacy_flags(argv)
    config_path = None
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            config_path = argv[index + 1]
            break
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            break

    if not config_path:
        return argv

    config = load_config(config_path)
    config_argv = config_to_argv(config)
    return ["--config", config_path, *config_argv, *argv]


def normalize_snapshot(snapshot: str) -> str:
    lines = [line.rstrip() for line in snapshot.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def snapshot_hash(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def short_snapshot_hash(snapshot_hash_value: str, length: int = 12) -> str:
    return snapshot_hash_value[:length]


async def build_initial_state_async(
    config: PilotConfig,
    tmux: TmuxSession,
    session_store: AnalyzerSessionStore,
) -> tuple[PilotState, Optional[SessionSnapshot]]:
    initial_snapshot = None
    if await tmux.exists_async():
        initial_snapshot = await tmux.capture_session_snapshot_async()
    now = time.monotonic()
    state = PilotState(
        last_snapshot_hash=initial_snapshot.hash if initial_snapshot is not None else None,
        last_changed_at=now,
        session_missing=initial_snapshot is None,
        analyzer_session_id=session_store.load(config.session_name),
    )
    return state, initial_snapshot


def build_snapshot_preview(snapshot: str, max_lines: int = 20) -> str:
    lines = snapshot.split("\n")
    if len(lines) <= max_lines:
        return snapshot
    preview = "\n".join(lines[-max_lines:])
    return f"...\n{preview}"


def log_snapshot_preview(session_name: str, snapshot: str, preview_lines: int = 20) -> None:
    snapshot_hash_value = snapshot_hash(snapshot)
    log_status(
        "snapshot session=%s preview_lines=%s hash=%s\n==begin==\n%s\n==end==",
        session_name,
        preview_lines,
        short_snapshot_hash(snapshot_hash_value),
        build_snapshot_preview(snapshot, max_lines=preview_lines),
    )


def parse_runtime_command(raw: str) -> RuntimeCommand:
    normalized = raw.strip().lower()
    if normalized in {"start", "stop", "status"}:
        return RuntimeCommand(name=normalized)
    return RuntimeCommand(name=raw, known=False)


def build_runtime(config: PilotConfig) -> PilotRuntime:
    return PilotRuntime(
        config=config,
        state=PilotState(),
        tmux=TmuxSession(
            session_name=config.session_name,
            history_lines=config.history_lines,
        ),
        session_store=AnalyzerSessionStore(),
    )


def log_runtime_commands(session_name: str) -> None:
    log_status("runtime commands available commands=start,stop,status")
    log_status("waiting for start command session=%s", session_name)


def enter_started_state(state: PilotState, snapshot: str) -> None:
    state.started = True
    state.waiting_for_snapshot_change = False
    state.session_missing = False
    state.restart_on_session_return = False
    state.continue_count = 0
    state.last_snapshot_hash = snapshot_hash(snapshot)
    state.last_changed_at = time.monotonic()


def enter_stopped_state(state: PilotState) -> None:
    state.started = False
    state.waiting_for_snapshot_change = False
    state.restart_on_session_return = False


def enter_session_missing_state(config: PilotConfig, state: PilotState) -> None:
    if state.session_missing:
        return
    state.session_missing = True
    state.restart_on_session_return = state.started
    state.started = False
    log_status("session missing session=%s state=waiting", config.session_name)


def handle_session_return(config: PilotConfig, state: PilotState, snapshot: str) -> None:
    if not state.session_missing:
        return
    state.session_missing = False
    log_status("session detected session=%s", config.session_name)
    if state.restart_on_session_return:
        enter_started_state(state, snapshot)
        log_action("session resumed session=%s reason=session-return", config.session_name)
        log_runtime_status(config=config, state=state, now=time.monotonic())


def update_snapshot_state(state: PilotState, snapshot_hash_value: str, now: float) -> bool:
    if snapshot_hash_value == state.last_snapshot_hash:
        return False
    state.last_snapshot_hash = snapshot_hash_value
    state.last_changed_at = now
    state.waiting_for_snapshot_change = False
    return True


def idle_for_too_long(state: PilotState, now: float, idle_threshold: float) -> bool:
    if state.waiting_for_snapshot_change:
        return False
    return now - state.last_changed_at >= idle_threshold


def log_runtime_status(
    config: PilotConfig,
    state: PilotState,
    now: float,
) -> None:
    idle_duration = now - state.last_changed_at
    log_status(
        "runtime state session=%s started=%s idle_for=%.1f "
        "continue_count=%s wait_for_change=%s max_continue=%s idle_threshold=%s poll_interval=%s "
        "history_lines=%s decision_mode=%s dry_run=%s",
        config.session_name,
        state.started,
        idle_duration,
        state.continue_count,
        state.waiting_for_snapshot_change,
        config.max_continue_count,
        config.idle_threshold,
        config.poll_interval,
        config.history_lines,
        config.decision_mode,
        config.dry_run,
    )


async def send_continue_async(
    config: PilotConfig,
    state: PilotState,
    tmux: TmuxSession,
    next_continue_count: int,
    prompt: Optional[str] = None,
) -> None:
    resolved_prompt = prompt or config.rule_continue_prompt
    log_action(
        "send continue prompt session=%s next_continue_count=%s dry_run=%s prompt=%r",
        config.session_name,
        next_continue_count,
        config.dry_run,
        resolved_prompt,
    )
    if not config.dry_run:
        await tmux.send_prompt_async(resolved_prompt)
    state.continue_count += 1
    state.last_changed_at = time.monotonic()


def mark_continue_cycle_without_prompt(state: PilotState) -> None:
    state.waiting_for_snapshot_change = False
    state.continue_count += 1
    state.last_changed_at = time.monotonic()


def enter_wait_state(state: PilotState) -> None:
    state.waiting_for_snapshot_change = True


def build_observation(state: PilotState, snapshot: str, now: float) -> PilotObservation:
    return PilotObservation(
        snapshot=snapshot,
        idle_for=now - state.last_changed_at,
        continue_count=state.continue_count,
    )


def build_analysis_request(config: PilotConfig, observation: PilotObservation) -> str:
    payload = {
        "session": config.session_name,
        "continue_count": observation.continue_count,
        "max_continue": config.max_continue_count,
        "idle_for": observation.idle_for,
        "idle_threshold": config.idle_threshold,
        "rule_continue_prompt": config.rule_continue_prompt,
        "snapshot": observation.snapshot,
    }
    return (
        "You are the idle decision engine for task_pilot.\n\n"
        "A monitored interactive CLI session has been idle. Decide what task_pilot should do next.\n\n"
        "Return exactly one JSON object with:\n"
        '- "action": "continue" or "wait"\n'
        '- "prompt": optional string, only when action is "continue"\n'
        '- "reason": short string\n\n'
        "Decision rules:\n"
        "1. Focus on the latest completed assistant response, not on generic interface text.\n"
        "2. If the latest completed assistant response shows the task is finished, answered, or simply waiting for the next user request, choose \"wait\".\n"
        "3. If the latest completed assistant response is only a progress update, explanation, apology, self-correction, or a statement that it should keep working, and the real task is still unfinished, choose \"continue\".\n"
        "4. Treat UI hints, footer suggestions, slash-command suggestions, quick actions, and visible draft text in the input composer as weak evidence. They must not be the main reason for choosing \"continue\".\n"
        "5. Ignore visible draft text in the input composer. This is the user-input line shown at the bottom of the interface, often prefixed by a prompt marker such as '› ' or '> '. It is draft or interrupted input, not a completed request, and must not be used as a decision signal.\n"
        "6. Be conservative: if the session looks complete or is only waiting for the next user request, choose \"wait\".\n"
        "7. If you choose \"continue\", provide a short concrete prompt that moves the task forward.\n"
        "8. Do not wrap the JSON in markdown fences.\n\n"
        "Examples:\n"
        '{"action":"wait","reason":"The latest completed assistant response already finished the task and the session is idle."}\n'
        '{"action":"continue","prompt":"Continue from the current state and keep working until the task is actually finished.","reason":"The latest completed assistant response is only a progress update and the task remains unfinished."}\n\n'
        f"Observation JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


async def read_process_stream(
    stream: asyncio.StreamReader | None,
    session_name: str,
    stream_name: str,
) -> str:
    if stream is None:
        return ""
    chunks: list[str] = []
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="ignore").rstrip("\r\n")
        chunks.append(text)
        log_status("codex %s %s %s", session_name, stream_name, text)
    return "\n".join(chunks)


async def run_logged_process(
    cmd: list[str],
    session_name: str,
    cwd: Path,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout_task = asyncio.create_task(
        read_process_stream(process.stdout, session_name, "stdout")
    )
    stderr_task = asyncio.create_task(
        read_process_stream(process.stderr, session_name, "stderr")
    )
    try:
        returncode = await process.wait()
        stdout_text, stderr_text = await asyncio.gather(stdout_task, stderr_task)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    return returncode, stdout_text, stderr_text


def parse_codex_analysis_output(
    stdout_text: str,
    existing_session_id: str,
) -> CodexAnalysisResult:
    resolved_session_id = existing_session_id
    last_message = ""
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started" and not resolved_session_id:
            resolved_session_id = str(event.get("thread_id") or "")
        if event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                last_message = str(item.get("text") or "").strip()

    return CodexAnalysisResult(
        resolved_session_id=resolved_session_id,
        last_message=last_message,
    )


def parse_codex_decision_message(
    message: str,
    continue_count: int,
) -> PilotDecision:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"codex analysis returned non-JSON output: {message}"
        ) from exc

    action = str(payload.get("action") or "").strip()
    if action == "continue":
        return PilotDecision(
            action="continue",
            next_continue_count=continue_count + 1,
            prompt=str(payload.get("prompt") or "").strip() or None,
            reason=str(payload.get("reason") or "").strip(),
        )
    if action == "wait":
        return PilotDecision(
            action="wait",
            reason=str(payload.get("reason") or "").strip(),
        )
    raise RuntimeError(f"codex analysis returned invalid action: {action!r}")


async def run_codex_analysis(
    runtime: PilotRuntime,
    observation: PilotObservation,
) -> PilotDecision:
    config = runtime.config
    state = runtime.state
    request_text = build_analysis_request(config, observation)
    analysis_workdir = await runtime.tmux.current_path_async()
    cmd = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "-C",
        str(analysis_workdir),
    ]
    if state.analyzer_session_id:
        cmd.extend(["resume", state.analyzer_session_id, request_text])
    else:
        cmd.append(request_text)

    returncode, stdout_text, stderr_text = await run_logged_process(
        cmd=cmd,
        session_name=config.session_name,
        cwd=analysis_workdir,
    )
    if returncode != 0:
        message = stderr_text.strip() or stdout_text.strip() or f"codex exec failed with code {returncode}"
        raise RuntimeError(message)

    result = parse_codex_analysis_output(stdout_text, state.analyzer_session_id)

    if (
        result.resolved_session_id
        and result.resolved_session_id != state.analyzer_session_id
    ):
        state.analyzer_session_id = result.resolved_session_id
        runtime.session_store.save(config.session_name, result.resolved_session_id)

    if not result.last_message:
        raise RuntimeError("codex analysis returned an empty response")
    return parse_codex_decision_message(
        result.last_message,
        continue_count=observation.continue_count,
    )


def decide_with_rule_mode(observation: PilotObservation) -> PilotDecision:
    return PilotDecision(
        action="continue",
        next_continue_count=observation.continue_count + 1,
        reason="rule_continue",
    )


async def decide_with_codex_mode(
    runtime: PilotRuntime,
    observation: PilotObservation,
) -> PilotDecision:
    for attempt in range(1, MAX_CODEX_ANALYSIS_RETRIES + 1):
        try:
            return await run_codex_analysis(runtime, observation)
        except Exception as exc:
            log_error(
                "codex analysis failed session=%s attempt=%s/%s err=%s",
                runtime.config.session_name,
                attempt,
                MAX_CODEX_ANALYSIS_RETRIES,
                exc,
            )
    return PilotDecision(
        action="continue_without_prompt",
        next_continue_count=observation.continue_count + 1,
        reason="codex_analysis_failed",
    )


async def decide_on_idle(
    runtime: PilotRuntime,
    observation: PilotObservation,
) -> PilotDecision:
    if runtime.config.decision_mode == "codex":
        return await decide_with_codex_mode(runtime, observation)
    return decide_with_rule_mode(observation)


async def execute_decision(
    runtime: PilotRuntime,
    decision: PilotDecision,
) -> None:
    config = runtime.config
    state = runtime.state
    if decision.action == "continue":
        if decision.next_continue_count is None:
            raise RuntimeError("continue decision requires next_continue_count")
        await send_continue_async(
            config,
            state,
            runtime.tmux,
            decision.next_continue_count,
            prompt=decision.prompt,
        )
        return

    if decision.action == "wait":
        log_status(
            "decision wait session=%s continue_count=%s reason=%r",
            config.session_name,
            state.continue_count,
            decision.reason,
        )
        enter_wait_state(state)
        return

    if decision.action == "continue_without_prompt":
        if decision.next_continue_count is None:
            raise RuntimeError(
                "continue_without_prompt decision requires next_continue_count"
            )
        log_status(
            "decision continue_without_prompt session=%s next_continue_count=%s reason=%r",
            config.session_name,
            decision.next_continue_count,
            decision.reason,
        )
        mark_continue_cycle_without_prompt(state)
        return

    raise RuntimeError(f"Unknown decision action: {decision.action}")


async def should_discard_decision_after_recheck(
    runtime: PilotRuntime,
    state: PilotState,
    snapshot_before_analysis: str,
) -> bool:
    config = runtime.config
    if not await runtime.tmux.exists_async():
        enter_session_missing_state(config, state)
        return True
    try:
        snapshot_after_analysis = await runtime.tmux.capture_normalized_snapshot_async()
    except RuntimeError:
        if not await runtime.tmux.exists_async():
            enter_session_missing_state(config, state)
            return True
        raise
    hash_before = snapshot_hash(snapshot_before_analysis)
    hash_after = snapshot_hash(snapshot_after_analysis)
    if hash_before == hash_after:
        return False
    now = time.monotonic()
    update_snapshot_state(state, hash_after, now)
    log_status(
        "discard stale decision session=%s reason=snapshot_changed hash_before=%s hash_after=%s",
        config.session_name,
        short_snapshot_hash(hash_before),
        short_snapshot_hash(hash_after),
    )
    return True


def maybe_terminate_for_max_continue(
    config: PilotConfig,
    state: PilotState,
) -> bool:
    if state.continue_count < config.max_continue_count:
        return False
    log_done(
        "terminated by max_continue session=%s continue_count=%s",
        config.session_name,
        state.continue_count,
    )
    return True


def log_idle_observation(config: PilotConfig, observation: PilotObservation) -> None:
    log_status(
        "idle detected session=%s idle_for=%.1f idle_threshold=%s continue_count=%s max_continue=%s",
        config.session_name,
        observation.idle_for,
        config.idle_threshold,
        observation.continue_count,
        config.max_continue_count,
    )
    log_snapshot_preview(config.session_name, observation.snapshot, preview_lines=20)
    log_status(
        "analysis begin session=%s decision_mode=%s continue_count=%s",
        config.session_name,
        config.decision_mode,
        observation.continue_count,
    )


async def await_idle_decision(
    runtime: PilotRuntime,
    observation: PilotObservation,
) -> PilotDecision | None:
    config = runtime.config
    runtime.analysis_task = asyncio.create_task(decide_on_idle(runtime, observation))
    try:
        return await runtime.analysis_task
    except asyncio.CancelledError:
        if runtime.analysis_cancel_reason == "runtime_stop":
            log_status(
                "analysis cancelled session=%s reason=runtime_stop",
                config.session_name,
            )
            runtime.analysis_cancel_reason = ""
            runtime.analysis_task = None
            return None
        raise
    else:
        runtime.analysis_cancel_reason = ""
        runtime.analysis_task = None


async def process_idle_cycle(runtime: PilotRuntime, snapshot: SessionSnapshot) -> bool:
    config = runtime.config
    state = runtime.state
    if maybe_terminate_for_max_continue(config, state):
        return True

    observation = build_observation(state, snapshot.text, snapshot.captured_at)
    log_idle_observation(config, observation)
    decision = await await_idle_decision(runtime, observation)
    if decision is None:
        return False
    if config.decision_mode == "codex" and await should_discard_decision_after_recheck(
        runtime, state, observation.snapshot
    ):
        return False
    await execute_decision(runtime, decision)
    return False


async def apply_runtime_command(
    command: RuntimeCommand,
    runtime: PilotRuntime,
    current_snapshot: str,
) -> bool:
    config = runtime.config
    state = runtime.state
    if command.name == "start":
        enter_started_state(state, current_snapshot)
        return True

    if command.name == "stop":
        enter_stopped_state(state)
        if runtime.analysis_task is not None and not runtime.analysis_task.done():
            runtime.analysis_cancel_reason = "runtime_stop"
            runtime.analysis_task.cancel()
        return True

    if command.name == "status":
        return True

    if not command.known:
        log_status(
            "unknown runtime command session=%s command=%r",
            config.session_name,
            command.name,
        )
        return False

    return False


def should_log_runtime_command_action(command: RuntimeCommand) -> bool:
    return command.known and command.name in ("start", "stop")


async def handle_runtime_command(
    raw_command: str, runtime: PilotRuntime, current_snapshot: str
) -> None:
    command = parse_runtime_command(raw_command)
    should_log_status = await apply_runtime_command(command, runtime, current_snapshot)
    config = runtime.config
    state = runtime.state

    if should_log_runtime_command_action(command):
        log_action(
            "runtime command session=%s command=%s",
            config.session_name,
            command.name,
        )

    if should_log_status:
        log_runtime_status(config=config, state=state, now=time.monotonic())


async def stdin_loop(
    runtime: PilotRuntime,
    command_queue: asyncio.Queue[str],
) -> None:
    while not runtime.shutting_down:
        command = await command_queue.get()
        current_snapshot = ""
        if await runtime.tmux.exists_async():
            current_snapshot = await runtime.tmux.capture_normalized_snapshot_async()
        await handle_runtime_command(command, runtime, current_snapshot)


async def initialize_runtime(runtime: PilotRuntime) -> None:
    state, _ = await build_initial_state_async(
        runtime.config,
        runtime.tmux,
        runtime.session_store,
    )
    runtime.state = state


def enqueue_runtime_command(command_queue: asyncio.Queue[str]) -> None:
    line = sys.stdin.readline()
    if line == "":
        return
    command = line.strip()
    if command:
        command_queue.put_nowait(command)


async def poll_session_snapshot(runtime: PilotRuntime) -> SessionSnapshot | None:
    config = runtime.config
    state = runtime.state

    if not await runtime.tmux.exists_async():
        enter_session_missing_state(config, state)
        return None

    snapshot = await runtime.tmux.capture_session_snapshot_async()
    handle_session_return(config, state, snapshot.text)

    if not state.started:
        return None

    update_snapshot_state(state, snapshot.hash, snapshot.captured_at)
    if not idle_for_too_long(state, snapshot.captured_at, config.idle_threshold):
        return None
    return snapshot


async def run_pilot_loop(
    config: PilotConfig,
) -> int:
    runtime = build_runtime(config)
    await initialize_runtime(runtime)
    state = runtime.state
    command_queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    stdin_reader_registered = False

    log_status("startup ready session=%s", config.session_name)
    log_runtime_commands(config.session_name)
    if state.session_missing:
        log_status("session missing session=%s state=waiting", config.session_name)

    if sys.stdin.isatty():
        loop.add_reader(sys.stdin.fileno(), enqueue_runtime_command, command_queue)
        stdin_reader_registered = True

    stdin_task = asyncio.create_task(stdin_loop(runtime, command_queue))
    try:
        while True:
            snapshot = await poll_session_snapshot(runtime)
            if snapshot is not None and await process_idle_cycle(runtime, snapshot):
                return 0
            await asyncio.sleep(config.poll_interval)
    finally:
        runtime.shutting_down = True
        if stdin_reader_registered:
            loop.remove_reader(sys.stdin.fileno())
        if runtime.analysis_task is not None and not runtime.analysis_task.done():
            runtime.analysis_cancel_reason = "shutdown"
            runtime.analysis_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.analysis_task
        stdin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stdin_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor an existing tmux session that is running an interactive CLI. "
            "If the output stays unchanged for too long, decide whether to continue "
            "the current task or keep waiting."
        )
    )
    parser.add_argument(
        "--config",
        help="Path to a JSON config file",
    )
    parser.add_argument(
        "--session",
        required=True,
        help="Existing tmux session name to monitor",
    )
    parser.add_argument(
        "--decision_mode",
        choices=("rule", "codex"),
        default="rule",
        help="Decision mode; default rule",
    )
    parser.add_argument(
        "--max_continue",
        type=int,
        default=DEFAULT_MAX_CONTINUE_COUNT,
        help=f"Maximum automatic continue prompts; default {DEFAULT_MAX_CONTINUE_COUNT}",
    )
    parser.add_argument(
        "--idle_threshold",
        type=float,
        default=DEFAULT_IDLE_THRESHOLD_SECONDS,
        help=(
            "How long unchanged output must last before the session is considered idle; "
            f"default {DEFAULT_IDLE_THRESHOLD_SECONDS}"
        ),
    )
    parser.add_argument(
        "--poll_interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds; default {DEFAULT_POLL_INTERVAL_SECONDS}",
    )
    parser.add_argument(
        "--history_lines",
        type=int,
        default=DEFAULT_HISTORY_LINES,
        help=f"How many tmux history lines to capture per poll; default {DEFAULT_HISTORY_LINES}",
    )
    parser.add_argument(
        "--rule_continue_prompt",
        default=DEFAULT_CONTINUE_PROMPT,
        help="Fixed prompt text sent when the session appears idle",
    )
    parser.add_argument(
        "--dry_run",
        dest="dry_run",
        action="store_true",
        help="Run monitoring and analysis normally, but do not send prompts to tmux",
    )
    parser.set_defaults(dry_run=False)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.poll_interval <= 0:
        raise RuntimeError("--poll_interval must be greater than 0")
    if args.idle_threshold <= 0:
        raise RuntimeError("--idle_threshold must be greater than 0")
    if args.max_continue < 0:
        raise RuntimeError("--max_continue must be greater than or equal to 0")
    if args.history_lines <= 0:
        raise RuntimeError("--history_lines must be greater than 0")
    if not args.rule_continue_prompt.strip():
        raise RuntimeError("--rule_continue_prompt must not be empty")


def main() -> int:
    configure_logging()
    parser = build_parser()
    try:
        args = parser.parse_args(merge_config_args(sys.argv[1:]))
        validate_args(args)
        config = PilotConfig(
            session_name=args.session,
            decision_mode=args.decision_mode,
            max_continue_count=args.max_continue,
            idle_threshold=args.idle_threshold,
            poll_interval=args.poll_interval,
            history_lines=args.history_lines,
            rule_continue_prompt=args.rule_continue_prompt,
            dry_run=args.dry_run,
        )
        return asyncio.run(run_pilot_loop(config))
    except KeyboardInterrupt:
        log_done("interrupted by Ctrl+C")
        return 130
    except Exception as exc:
        log_error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
