#!/usr/bin/env python3
"""Stand-in for xdotool, xprop, xfconf-query and xdpyinfo in the unit tests. It is installed under those
names in a temporary directory that is put first on PATH; the desktop it pretends to be is the JSON file
named by FAKE_X_STATE: {"display": ":9", "desktop": 0, "count": 4, "names": [...], "windows": {"<id>":
{"class": "", "classname": "", "role": "", "desktop": 0, "pid": 1}}, "panel": {"/plugins/plugin-1": "tasklist"},
"props": {}, "log": []}."""
import json
import os
import re
import sys

state_path = os.environ["FAKE_X_STATE"]
state = json.load(open(state_path))
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
state["log"].append([tool] + args)


def save():
    json.dump(state, open(state_path, "w"))


def finish(code=0, out=""):
    save()
    sys.stdout.write(out)
    sys.exit(code)


if tool == "xdpyinfo":
    finish(0 if args[:2] == ["-display", state["display"]] else 1)
if os.environ.get("DISPLAY") != state["display"] and tool != "xfconf-query":
    finish(1)
if tool == "xprop":
    window = state["windows"].get(args[1])
    if window is None or not window["role"]:
        finish(0, "WM_WINDOW_ROLE:  not found.\n")
    finish(0, 'WM_WINDOW_ROLE(STRING) = "%s"\n' % window["role"])
if tool == "xdotool":
    if args[0] == "get_desktop":
        finish(0, "%d\n" % state["desktop"])
    if args[0] == "get_num_desktops":
        finish(0, "%d\n" % state["count"])
    if args[0] == "get_desktop_for_window":
        finish(0, "%d\n" % state["windows"][args[1]]["desktop"])
    if args[0] == "search":
        field = "class" if args[1] == "--class" else "classname"
        found = [w for w, v in state["windows"].items() if re.search(args[2], v[field])]
        finish(0 if found else 1, "".join(w + "\n" for w in found))
    if args[0] == "set_desktop_for_window":
        state["windows"][args[1]]["desktop"] = int(args[2])
        finish()
    if args[0] == "getwindowpid":
        finish(0, "%d\n" % state["windows"][args[1]]["pid"])
if tool == "xfconf-query":
    if args == ["-c", "xfce4-panel", "-lv"]:
        finish(0, "".join("%s  %s\n" % (k, v) for k, v in state["panel"].items()))
    channel, prop = args[1], args[3]
    rest = args[4:]
    if prop == "/general/workspace_count":
        if rest[:1] == ["-s"]:
            state["count"] = int(rest[1])
            finish()
        finish(0, "%d\n" % state["count"])
    if prop == "/general/workspace_names":
        if rest[:1] == ["-n"]:
            state["names"] = [rest[i + 1] for i, a in enumerate(rest) if a == "-s"]
            finish()
        finish(0, "Value is an array with %d items:\n\n" % len(state["names"]) + "".join(n + "\n" for n in state["names"]))
    state["props"][channel + prop] = rest
    finish()
finish(2, "fake_x: unhandled %s %s\n" % (tool, args))
