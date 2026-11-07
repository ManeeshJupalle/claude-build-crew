---
description: Cut a release — code computes the version, you approve the notes, /ship merges, then tag + GitHub release. Usage: /release [version]
argument-hint: [version]
---

Cut a release. Requested version (optional): $ARGUMENTS

1. Run the `build-crew:release-manager` subagent in mode `prepare`, passing the version if one was given. If it returns STOPPED, show the reason and END.
2. **Human.** Show the version change and why, plus the new CHANGELOG section. Ask "Release v<x>? (yes / no / edit)" and wait.
   - no: switch back to the default branch, run `git branch -D chore/release-v<x>`, and END.
   - edit: apply the edits and ask again.
3. Run the `/ship` flow on the release branch with the PR title `chore(release): v<x>`. Unless `merge_gate` printed `"ok": true`, END without tagging, even if the PR shows as merged.
4. Run the `build-crew:release-manager` subagent in mode `publish`.
5. Report the tag and release URL, then: "Run `/deploy`."
