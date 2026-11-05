---
description: Plan a project — short interview, then docs/HOW_IT_WORKS.md (for humans) and docs/PLAN.md (phases). Usage: /kickoff [one-line idea]
argument-hint: [one-line idea]
---

Plan this project. Idea (may be empty): $ARGUMENTS

Don't write application code in this command.

1. **Look first, silently.** Read the README, manifests, folders, and any existing `docs/`. If `docs/HOW_IT_WORKS.md` exists, ask whether to update it or start fresh, and wait.
2. **Interview.** Send ONE message with at most 6 numbered questions. Skip anything the idea or repo already answers, and put a default in brackets so the user can reply "defaults":
   1. What does it do, and for whom?
   2. The 3–5 must-haves for v1?
   3. What's explicitly NOT in v1?
   4. Stack and hosting? [match the repo, else a boring mainstream choice]
   5. Auth, scale, integrations? [managed auth, < 1k users, none]
   6. Hard constraints (deadline, offline, compliance)?
   If a stack choice is genuinely unclear, offer once: "Want me to research <choice> first?" Only on a yes, run the `build-crew:researcher` subagent and pass its `DECISION_ROW` to the architect.
3. **Brief.** Combine everything into ≤ 20 bullets and show it in one short message.
4. Run the `build-crew:architect` subagent with the brief. If it returns STOPPED, ask its questions and re-run it once.
5. Run the `build-crew:planner` subagent. Give it the name and one-line description of each skill installed in this session, so it can name the ones that fit a phase.
6. **Finish.** Reply with the two file paths, the phase list (one line each), any open questions, and: "Skim HOW_IT_WORKS.md, then run `/build-phase 0`."
