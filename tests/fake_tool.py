#!/usr/bin/env python3
"""A stand-in for npm, pip-audit and the virtualenv's python, driven by a JSON file (FAKE_TOOL_STATE).

The file maps "outdated", "audit", "pip list --not-required", "pip list --outdated", "site" and
"pip-audit" to {"stdout", "stderr", "code"}.
"""
import json
import os
import sys

state = json.load(open(os.environ["FAKE_TOOL_STATE"], encoding="utf-8"))
args = [a for a in sys.argv[1:] if a != "-m"]
key = ("pip list " + args[2] if args[0] == "pip" else
       "site" if args[0] == "-c" else
       "pip-audit" if args[0] == "--path" else args[0])
r = state.get(key, {"code": 99, "stderr": f"fake_tool: no response for {key}"})
out = r.get("stdout", "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stderr.write(r.get("stderr", ""))
sys.exit(r.get("code", 0))
