---
description: Build one phase from docs/PLAN.md. Code checks it may start and proves it's done. Usage: /build-phase <id>   (e.g. 0, 2a; empty = next)
argument-hint: <phase id>
---

Build phase **$ARGUMENTS**.

1. **Pick (Code).** If `$ARGUMENTS` is empty, run `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/phase_gate.py next` and use its `phase`.
2. **May it start? (Code)** Run `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/phase_gate.py check <id>`. If `"ok": false`, show the `error` to the user and STOP. Don't work around it. Its `skills` are the skills this phase names.
3. **Branch.** `git switch <default> && git pull --ff-only && git switch -c feat/phase-<id>-<kebab-name>`.
4. **Context.** Read the phase in `docs/PLAN.md`, the Decisions and Not doing sections of `docs/HOW_IT_WORKS.md`, and `docs/PROGRESS.md` gotchas if present. Then read the code you'll touch. **The code is the spec**: follow its existing patterns and conventions.
   Load each of the phase's `skills` with the Skill tool. If one isn't installed, build without it and list it under DEVIATIONS. Skills are advice: where one disagrees with Done when, `checks.py` or a Decision, those win.
5. **Build (AI).** Do the Goal. Stay in "Work in". Write tests alongside the code. Respect "Not in this phase". If something conflicts with a Decision, STOP and ask; don't improvise.
6. **Prove it (Code).** Run `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/checks.py` and `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/phase_gate.py verify <id>`. Fix and repeat, up to 5 rounds. Never delete or skip tests to get green. After 5 failed rounds, STOP and show the failing output.
7. **Human doc.** Only if user-visible behavior or a decision changed, update `docs/HOW_IT_WORKS.md`, keeping it ≤ 150 lines. Otherwise leave it alone.
8. **Mark done (Code).** `bash "${CLAUDE_PLUGIN_ROOT}/run.sh" gates/phase_gate.py done <id>`. It re-runs Done-when and ticks the box only if they pass.
9. **Report:**
```
PHASE: <id> — <name>
DONE_WHEN: <each command: PASS/FAIL, from phase_gate>
CHECKS: <checks.py summary>
SKILLS: <loaded, or "none">
DEVIATIONS: <or "none">
```
Then say: "Run `/ship`." Don't commit here; `/ship` does that.
