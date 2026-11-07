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
    print(json.dumps(state["pr"]))
elif args[:2] == ["pr", "checks"]:
    if state.get("checks") is None:
        fail("no checks reported on the 'feat/x' branch")
    print(json.dumps(state["checks"]))
elif args[:2] == ["pr", "merge"]:
    if state.get("merge_error"):
        fail(state["merge_error"])
    state["pr"]["state"] = "MERGED"
    state["pr"]["mergeCommit"] = {"oid": "f" * 40}
    if state.get("cleanup_error"):  # gh merged, then failed to delete the local branch
        fail(state["cleanup_error"])
    print("merged")
else:
    fail(f"fake gh: unsupported {args}")
save()
