#!/usr/bin/env python3
import asyncio

from task_pilot import PilotConfig, PilotObservation, PilotState, run_codex_analysis


SAMPLES = [
    (
        "complete",
        (
            "OpenAI Codex\n\n"
            "› Reply with exactly DONE and then stop.\n\n"
            "• DONE\n\n"
            "› Run /review on my current changes"
        ),
        "wait",
    ),
    (
        "incomplete",
        (
            "OpenAI Codex\n\n"
            "› Fix the failing tests and verify them.\n\n"
            "• I found the failing file and will inspect the test output next.\n\n"
            "› Run /review on my current changes"
        ),
        "continue",
    ),
    (
        "interrupted_draft",
        (
            "OpenAI Codex\n\n"
            "■ Conversation interrupted - tell the model what to do differently.\n"
            "Something went wrong? Hit `/feedback` to report the issue.\n\n"
            "› Summarize recent commits\n\n"
            "  gpt-5.4 medium · 98% left · ~/test/chart-by-ta/chart_script · 5h 84% · weekly 63%\n"
        ),
        "wait",
    ),
    (
        "progress_update_needs_continue",
        (
            "OpenAI Codex\n\n"
            "  The final answer was my execution mistake, not ambiguity in the agreement itself.\n\n"
            "  The root causes were:\n\n"
            "  - I confused a progress update with task completion.\n"
            "  - I did not continue pushing the main task after a local fix.\n\n"
            "  From now on I will:\n\n"
            "  - not treat a local fix as final completion\n"
            "  - only finish when the task is complete, blocked, or needs a decision\n"
            "  - otherwise keep going\n\n"
            "  If you agree, I will stop explaining and continue pushing the main task from the current state.\n\n"
            "› Improve documentation in @filename\n"
        ),
        "continue",
    )
]


def build_config(session_name: str) -> PilotConfig:
    return PilotConfig(
        session_name=session_name,
        decision_mode="codex",
        max_continue_count=5,
        idle_threshold=10.0,
        poll_interval=2.0,
        history_lines=300,
        rule_continue_prompt=(
            "Please continue the current task. If the goal is not finished yet, "
            "keep going and do not stop early."
        ),
    )


def main() -> int:
    all_ok = True
    for name, snapshot, expected_action in SAMPLES:
        config = build_config(f"sample_{name}")
        state = PilotState()
        observation = PilotObservation(
            snapshot=snapshot,
            idle_for=12.0,
            continue_count=0,
        )
        decision = asyncio.run(run_codex_analysis(config, state, observation))
        ok = decision.action == expected_action
        all_ok = all_ok and ok
        print(
            f"{name}: expected={expected_action} actual={decision.action} ok={ok}"
        )

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
