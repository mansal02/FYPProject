# UAT Report - 2026-05-12

## Scope
- UAT scenarios provided by user on 2026-05-12.
- Executed: assistant mode startup with auto-retry stability profiles.
- Skipped: interactive steps (voice, Live2D, heavy load, air-gap) due to GUI exit before interaction.

## Execution Summary
- Command: `python -m aiassistant.launchers.runsys --mode assistant`
- Result: GUI exited unexpectedly with exit code 3221226505; auto-restarted with stability profiles 1 and 2, then exited again.
- Diagnostic log: `cache/main_gui_boot.log`
- Status: UAT blocked by GUI crash; interactive scenarios not reachable.

## Scenario Results
1. Memory Stress Test: Not executed (GUI crashed before interaction).
2. Conflicting Goals Test: Not executed (GUI crashed before interaction).
3. Mood Swing Test (STT/TTS + Live2D): Not executed (GUI crashed before interaction).
4. Heavy Load Test: Not executed (GUI crashed before interaction).
5. Concise Assistant Test: Not executed (GUI crashed before interaction).
6. Air-Gap Test: Not executed (GUI crashed before interaction).

## Decision
- UAT Pass: No (GUI crash blocks interactive UAT).

## Notes
- GUI crash exit code observed: 3221226505.
- Auto-retry stability profiles: 1 (Live2D disabled), 2 (minimal runtime: Live2D/voice/TTS/screen/RAG disabled).
