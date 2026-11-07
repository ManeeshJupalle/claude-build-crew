#!/usr/bin/env python3
"""A stand-in for npm, pip-audit and the virtualenv's python, driven by a JSON file (FAKE_TOOL_STATE).

The file maps "prefix", "outdated", "audit", "pip list --not-required", "pip list --outdated", "pip list --uptodate",
"site" and "pip-audit" to {"stdout", "stderr", "code"}, and each call's arguments are appended to "calls".
"""
import json
import os
import sys

path = os.environ["FAKE_TOOL_STATE"]
state = json.load(open(path, encoding="utf-8"))
state.setdefault("calls", []).append(sys.argv[1:])
json.dump(state, open(path, "w", encoding="utf-8"))
args = [a for a in sys.argv[1:] if a != "-m"]
key = ("pip list " + args[2] if args[0] == "pip" else
       "site" if args[0] == "-c" else
       "pip-audit" if args[0] == "--path" else args[0])
r = state.get(key, {"code": 99, "stderr": f"fake_tool: no response for {key}"})
out = r.get("stdout", "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stderr.write(r.get("stderr", ""))
sys.exit(r.get("code", 0))
