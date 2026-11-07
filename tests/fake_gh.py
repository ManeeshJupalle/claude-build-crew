#!/usr/bin/env python3
"""A tiny stand-in for the gh CLI, driven by a JSON state file (FAKE_GH_STATE)."""
import json
import os
import sys

state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)


def save():
    json.dump(state, open(state_path, "w"))


def fail(msg):
    save()
    sys.stderr.write(msg + "\n")
    sys.exit(1)


if args[:2] == ["repo", "view"]:
    print(json.dumps({"defaultBranchRef": {"name": "main"}}))
elif args == ["api", "user"]:
    print(json.dumps({"login": state.get("me", "maneesh")}))
elif args[:1] == ["api"] and "/compare/" in args[1]:
    print(state.get("behind_by", 0))
elif args[:2] == ["pr", "view"]:
    if state.get("view_error"):
        fail(state["view_error"])
    print(json.dumps(state["pr"]))
elif args[:2] == ["pr", "checks"]:
    if state.get("checks") is None:
        fail("no checks reported on the 'feat/x' branch")
    print(json.dumps(state["checks"]))
elif args[:2] == ["pr", "merge"]:
    # what the PR looks like afterwards: merged, unless a test says otherwise
    state["pr"].update(state.get("pr_after_merge", {"state": "MERGED", "mergeCommit": {"oid": "f" * 40}}))
    state["view_error"] = state.get("view_error_after_merge")
    if state.get("merge_error"):
        fail(state["merge_error"])
    print("merged")
else:
    fail(f"fake gh: unsupported {args}")
save()
