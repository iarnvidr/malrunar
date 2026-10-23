"""Unit tests of the session kit (standard library only; no X server, no Docker):

    python3 -m unittest discover -s session-kit/test

The X tools are replaced by test/fake_x.py on PATH, so the tests run on any host and inside the image.
"""
import contextlib
import ctypes
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent
sys.path.insert(0, str(COMPONENT / "lib"))
import session_kit as sk  # noqa: E402

BIN = COMPONENT / "bin/malrunar-session"


def mozlz4(document):
    library = ctypes.CDLL("liblz4.so.1")
    raw = json.dumps(document).encode()
    buffer = ctypes.create_string_buffer(len(raw) + len(raw) // 255 + 32)
    size = library.LZ4_compress_default(raw, buffer, len(raw), len(buffer))
    return b"mozLz40\0" + len(raw).to_bytes(4, "little") + buffer.raw[:size]


def session(*tabs):
    return {"windows": [{"tabs": [{"index": 1, "entries": [{"url": url}]} for url in tabs]}]}


class Env(unittest.TestCase):
    """Isolated environment: own HOME, a fake X desktop on PATH, MALRUNAR_SESSION_KIT_* cleared."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.home = self.tmp / "home"
        self.home.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in ("xdotool", "xprop", "xfconf-query", "xdpyinfo"):
            (bindir / tool).write_text(f"#!{sys.executable}\n" + (HERE / "fake_x.py").read_text())
            (bindir / tool).chmod(0o755)
        self.state_file = self.tmp / "state.json"
        self.windows = {}
        self.write_state()
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in [k for k in os.environ if k.startswith("MALRUNAR_")]:
            del os.environ[key]
        os.environ.update(HOME=str(self.home), PATH=f"{bindir}:{os.environ['PATH']}", FAKE_X_STATE=str(self.state_file),
                          DISPLAY=":9", MALRUNAR_SESSION_KIT_POLL_SECONDS="0.01")
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            os.environ.pop(key, None)

    def write_state(self, **changes):
        state = {"display": ":9", "desktop": 0, "count": 1, "names": ["x"], "windows": self.windows,
                 "panel": {}, "props": {}, "log": []}
        if self.state_file.exists():
            state.update(json.loads(self.state_file.read_text()))
        state.update(changes)
        self.state_file.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_file.read_text())

    def set(self, **settings):
        for key, value in settings.items():
            os.environ["MALRUNAR_SESSION_KIT_" + key] = value

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = sk.main(list(argv))
        return status, out.getvalue(), err.getvalue()


class ParsingTest(unittest.TestCase):
    def test_workspaces(self):
        self.assertEqual(sk.parse_workspaces(" Work, Mail ,,Desktop 3"), ["Work", "Mail", "Desktop 3"])
        self.assertEqual(sk.parse_workspaces(None), [])
        with self.assertRaises(sk.ConfigError):
            sk.parse_workspaces("a,a")

    def test_rules(self):
        rules = sk.parse_rules("classname=Navigator,role=browser@1; class=Mail@Mail")
        self.assertEqual([(m.field, m.pattern, m.role, w) for m, w in rules],
                         [("classname", "Navigator", "browser", "1"), ("class", "Mail", None, "Mail")])
        for bad in ("classname=Navigator", "role=x@1", "classname=A,classname=B@1", "classname=a b;c@1",
                    "classname=A,extra=1@1", "classname=@1", "classname=A@"):
            with self.assertRaises(sk.ConfigError, msg=bad):
                sk.parse_rules(bad)

    def test_resolve_workspace(self):
        names = ["Work", "Mail"]
        self.assertEqual(sk.resolve_workspace("1", names), 0)
        self.assertEqual(sk.resolve_workspace("Mail", names), 1)
        for bad in ("0", "3", "Other"):
            with self.assertRaises(sk.ConfigError):
                sk.resolve_workspace(bad, names)
        self.assertEqual(sk.resolve_workspace("7", []), 6)   # no names: numbers are not range-checked

    def test_window_checks(self):
        checks = sk.parse_window_checks("classname=Navigator,role=browser,count=2;class=Foo")
        self.assertEqual([(m.pattern, c) for m, c in checks], [("Navigator", 2), ("Foo", 1)])
        with self.assertRaises(sk.ConfigError):
            sk.parse_window_checks("class=Foo,count=x")


class WorkspacesTest(Env):
    def setUp(self):
        super().setUp()
        self.windows.update({
            "100": {"class": "firefox", "classname": "Navigator", "role": "browser", "desktop": 3, "pid": 1},
            "101": {"class": "firefox", "classname": "Navigator", "role": "Toolbox", "desktop": 3, "pid": 1},
            "102": {"class": "Mail", "classname": "mail", "role": "main", "desktop": 3, "pid": 1},
        })
        self.write_state(windows=self.windows)

    def test_nothing_configured_is_a_no_op(self):
        status, _, err = self.call("workspaces", "--once")
        self.assertEqual(status, 0)
        self.assertIn("nothing to do", err)
        self.assertEqual(self.state()["log"], [])

    def test_names_count_and_placement(self):
        self.set(WORKSPACES="Work,Mail,Three", PLACE="classname=Navigator,role=browser@1;class=Mail@Mail")
        status, _, err = self.call("workspaces", "--once")
        self.assertEqual(status, 0, err)
        state = self.state()
        self.assertEqual((state["count"], state["names"]), (3, ["Work", "Mail", "Three"]))
        self.assertEqual([state["windows"][w]["desktop"] for w in ("100", "101", "102")], [0, 3, 1])

    def test_placement_is_once_per_window(self):
        self.set(PLACE="classname=Navigator,role=browser@2")
        seen = set()
        rules = [(m, sk.resolve_workspace(w, [])) for m, w in sk.parse_rules(os.environ["MALRUNAR_SESSION_KIT_PLACE"])]
        self.assertEqual(sk.place_windows(rules, seen), ["100"])
        self.windows["100"]["desktop"] = 3           # the user moves it
        self.write_state(windows={**self.state()["windows"], "100": {**self.state()["windows"]["100"], "desktop": 3}})
        self.assertEqual(sk.place_windows(rules, seen), [])
        self.assertEqual(self.state()["windows"]["100"]["desktop"], 3)

    def test_panel_tweaks_only_when_asked(self):
        self.write_state(panel={"/plugins/plugin-1": "tasklist", "/plugins/plugin-2": "pager", "/panels": "x y z"})
        self.set(PANEL_TWEAKS="1")
        self.assertEqual(self.call("workspaces", "--once")[0], 0)
        props = self.state()["props"]
        self.assertEqual(props["xfce4-panel/plugins/plugin-1/include-all-workspaces"], ["-n", "-t", "bool", "-s", "false"])
        self.assertEqual(props["xfce4-panel/plugins/plugin-2/rows"], ["-n", "-t", "uint", "-s", "1"])

    def test_missing_xdotool_names_it(self):
        self.set(WORKSPACES="a,b")
        os.environ["PATH"] = str(self.tmp / "nowhere")
        status, _, err = self.call("workspaces", "--once")
        self.assertEqual(status, 78)
        self.assertIn("xdpyinfo", err)

    def test_missing_xdotool_only(self):
        self.set(WORKSPACES="a,b")
        (self.tmp / "bin/xdotool").unlink()
        os.environ["PATH"] = str(self.tmp / "bin")
        status, _, err = self.call("workspaces", "--once")
        self.assertEqual((status, "xdotool" in err), (78, True))

    def test_bad_rule_is_a_config_error(self):
        self.set(PLACE="classname=Navigator@9", WORKSPACES="a,b")
        self.assertEqual(self.call("workspaces", "--once")[0], 78)

    def test_no_session_times_out(self):
        self.set(WORKSPACES="a,b", WAIT_SECONDS="1", DISPLAY=":42")
        os.environ["DISPLAY"] = ":42"
        with self.assertRaises(SystemExit) as caught:
            self.call("workspaces", "--once")
        self.assertIn("no desktop session", str(caught.exception))


class SessionStoreTest(Env):
    def profile(self, name, document, mtime=None, relative="sessionstore-backups/recovery.jsonlz4"):
        profile = self.tmp / name
        (profile / relative).parent.mkdir(parents=True, exist_ok=True)
        (profile / relative).write_bytes(mozlz4(document))
        if mtime:
            os.utime(profile / relative, (mtime, mtime))
        return profile

    def test_urls_in_order_and_only_web_schemes(self):
        profile = self.profile("p", session("https://a.example/", "about:blank", "http://b.example/x", "file:///etc/passwd"))
        self.assertEqual(sk.session_urls(profile), ["https://a.example/", "http://b.example/x"])

    def test_uses_current_history_entry_and_newest_file(self):
        document = {"windows": [{"tabs": [{"index": 2, "entries": [{"url": "https://old.example/"}, {"url": "https://now.example/"}]}]}]}
        profile = self.profile("p", document, mtime=2000)
        (profile / "sessionstore.jsonlz4").write_bytes(mozlz4(session("https://older-file.example/")))
        os.utime(profile / "sessionstore.jsonlz4", (1000, 1000))
        self.assertEqual(sk.session_urls(profile), ["https://now.example/"])

    def test_corrupt_files_are_skipped(self):
        profile = self.tmp / "p"
        (profile / "sessionstore-backups").mkdir(parents=True)
        (profile / "sessionstore-backups/recovery.jsonlz4").write_bytes(b"not a session")
        (profile / "sessionstore.jsonlz4").write_bytes(b"mozLz40\0" + (10).to_bytes(4, "little") + b"garbage")
        self.assertEqual(sk.session_urls(profile), [])

    def test_restore_tabs_prints_nul_separated(self):
        profile = self.profile("p", session("https://a.example/", "https://b.example/"))
        result = subprocess.run([sys.executable, "-I", str(COMPONENT / "lib/session_kit.py"), "restore-tabs", str(profile)],
                                capture_output=True, env=os.environ)
        self.assertEqual(result.stdout, b"https://a.example/\0https://b.example/\0")

    def test_fallback_is_opt_in(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.call("restore-tabs", str(empty)), (0, "", ""))
        self.set(FALLBACK_URL="about:blank")
        self.assertEqual(sk.restore_list([str(empty)])[1], ["about:blank"])

    def test_profile_needs_an_argument_a_setting_or_the_component(self):
        self.set(WORK_BROWSER_DIR=str(self.tmp / "absent"))
        status, _, err = self.call("restore-tabs")
        self.assertEqual(status, 78)
        self.assertIn("work-browser", err)
        profile = self.profile("p", session("https://a.example/"))
        self.set(PROFILE_DIR=str(profile))
        self.assertEqual(sk.restore_list([])[1], ["https://a.example/"])


class WorkBrowserGateTest(Env):
    def component(self, record):
        root = self.tmp / "wb"
        (root / "bin").mkdir(parents=True)
        for name in ("malrunar-work-browser", "malrunar-work-browser-open"):
            (root / "bin" / name).write_text(f"#!/bin/sh\necho \"$@\" >> {record}\nexit ${{OPEN_STATUS:-0}}\n")
            (root / "bin" / name).chmod(0o755)
        return root

    def test_open_tabs_without_the_component(self):
        self.set(WORK_BROWSER_DIR=str(self.tmp / "absent"), PROFILE_DIR=str(self.tmp))
        status, _, err = self.call("open-tabs")
        self.assertEqual(status, 78)
        self.assertIn("work-browser component", err)

    def test_launchers_without_a_command_or_the_component(self):
        self.set(WORK_BROWSER_DIR=str(self.tmp / "absent"))
        status, _, err = self.call("launchers")
        self.assertEqual(status, 78)
        self.assertIn("work-browser", err)
        self.assertFalse((self.home / ".config/xfce4/helpers.rc").exists())

    def test_open_tabs_hands_each_tab_to_the_helper(self):
        record = self.tmp / "record"
        profile = self.tmp / "p"
        (profile / "sessionstore-backups").mkdir(parents=True)
        (profile / "sessionstore-backups/recovery.jsonlz4").write_bytes(mozlz4(session("https://a.example/", "https://b.example/")))
        self.set(WORK_BROWSER_DIR=str(self.component(record)))
        self.assertEqual(self.call("open-tabs", str(profile))[0], 0)
        self.assertEqual(record.read_text().split(), ["https://a.example/", "https://b.example/"])

    def test_open_tabs_default_profile_is_the_work_browsers(self):
        record = self.tmp / "record"
        default = self.home / ".config/malrunar-work-browser/profile"
        default.mkdir(parents=True)
        (default / "sessionstore.jsonlz4").write_bytes(mozlz4(session("https://c.example/")))
        self.set(WORK_BROWSER_DIR=str(self.component(record)))
        self.assertEqual(self.call("open-tabs")[0], 0)
        self.assertEqual(record.read_text().split(), ["https://c.example/"])
        os.environ["MALRUNAR_WORK_BROWSER_PROFILE_DIR"] = str(self.tmp / "elsewhere")
        self.assertEqual(sk.work_browser_profile(), self.tmp / "elsewhere")

    def test_open_tabs_reports_a_browser_that_is_not_running(self):
        record = self.tmp / "record"
        profile = self.tmp / "p"
        profile.mkdir()
        (profile / "sessionstore.jsonlz4").write_bytes(mozlz4(session("https://a.example/")))
        self.set(WORK_BROWSER_DIR=str(self.component(record)))
        os.environ["OPEN_STATUS"] = "3"
        status, _, err = self.call("open-tabs", str(profile))
        self.assertEqual(status, 3)
        self.assertIn("https://a.example/", err)

    def test_launchers_default_to_the_component_and_keep_other_settings(self):
        record = self.tmp / "record"
        rc = self.home / ".config/xfce4/helpers.rc"
        rc.parent.mkdir(parents=True)
        rc.write_text("MailReader=x\nWebBrowser=other\n")
        self.set(WORK_BROWSER_DIR=str(self.component(record)))
        self.assertEqual(self.call("launchers")[0], 0)
        opener = self.tmp / "wb/bin/malrunar-work-browser-open"
        self.assertEqual(rc.read_text(), "MailReader=x\nWebBrowser=malrunar-session-kit-browser\n")
        entry = (self.home / ".local/share/applications/malrunar-session-kit-browser.desktop").read_text()
        self.assertIn(f"Exec={opener} %u", entry)
        helper = (self.home / ".local/share/xfce4/helpers/malrunar-session-kit-browser.desktop").read_text()
        self.assertIn(f'X-XFCE-CommandsWithParameter={opener} "%s";', helper)

    def test_launchers_work_without_the_component_when_given_a_command(self):
        self.set(WORK_BROWSER_DIR=str(self.tmp / "absent"), BROWSER_COMMAND="/usr/bin/my-browser",
                 LAUNCHER_ID="my-browser", LAUNCHER_NAME="My Browser")
        self.assertEqual(self.call("launchers")[0], 0)
        self.assertIn("WebBrowser=my-browser", (self.home / ".config/xfce4/helpers.rc").read_text())
        self.assertIn("Name=My Browser", (self.home / ".local/share/applications/my-browser.desktop").read_text())

    def test_launchers_reject_bad_input(self):
        self.set(BROWSER_COMMAND="cmd\nExec=evil")
        self.assertEqual(self.call("launchers")[0], 78)
        self.set(BROWSER_COMMAND="cmd", LAUNCHER_ID="../x")
        self.assertEqual(self.call("launchers")[0], 78)


class CheckTest(Env):
    def setUp(self):
        super().setUp()
        self.profile = self.home / ".config/malrunar-work-browser/profile"
        self.profile.mkdir(parents=True)
        self.set(WORK_BROWSER_DIR=str(self.tmp / "wb"))
        (self.tmp / "wb/bin").mkdir(parents=True)
        (self.tmp / "wb/bin/malrunar-work-browser-open").write_text("#!/bin/sh\n")
        (self.tmp / "wb/bin/malrunar-work-browser-open").chmod(0o755)
        self.children = []
        self.addCleanup(lambda: [(c.kill(), c.wait()) for c in self.children])

    def browser_with_args(self, profile, *extra):
        # A process whose /proc/<pid>/cmdline is: python3 <script> -profile <profile> [extra]
        script = self.tmp / "fake-firefox"
        script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        script.chmod(0o755)
        child = subprocess.Popen([str(script), "-profile", str(profile), *extra])
        self.children.append(child)
        return child

    def lock(self, pid):
        os.symlink(f"127.0.0.1:+{pid}", self.profile / "lock")

    def test_display_workspaces_and_windows(self):
        self.write_state(count=2, names=["Work", "Mail"])
        self.windows["1"] = {"class": "x", "classname": "Navigator", "role": "browser", "desktop": 0, "pid": 5}
        self.write_state(windows=self.windows)
        self.set(WORKSPACES="Work,Mail", CHECK_WINDOWS="classname=Navigator,role=browser,count=1")
        status, out, err = self.call("check")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("ok: 2 workspaces: Work, Mail", out)
        self.set(WORKSPACES="Work,Mail,Three")
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("workspaces are 2", err)
        self.set(WORKSPACES="Work,Mail", CHECK_WINDOWS="classname=Navigator,role=browser,count=2")
        self.assertEqual(self.call("check")[0], 1)

    def test_no_display(self):
        self.write_state(display=":1")
        os.environ["DISPLAY"] = ":9"
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("no X display", err)

    def test_addons(self):
        (self.profile / "extensions.json").write_text(json.dumps({"addons": [
            {"id": "a@example.invalid", "active": True}, {"id": "b@example.invalid", "active": False}]}))
        self.set(CHECK_ADDONS="a@example.invalid")
        self.assertEqual(self.call("check")[0], 0)
        self.set(CHECK_ADDONS="a@example.invalid,b@example.invalid")
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("b@example.invalid is not active", err)

    def test_work_browser_checks_need_the_component(self):
        self.set(CHECK_WORK_BROWSER="1", WORK_BROWSER_DIR=str(self.tmp / "absent"))
        status, _, err = self.call("check")
        self.assertEqual(status, 78)
        self.assertIn("work-browser component", err)

    def test_work_browser_not_running_then_running(self):
        self.set(CHECK_WORK_BROWSER="1", CHECK_BROWSER_WINDOWS="any")
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("not running", err)
        child = self.browser_with_args(self.profile)
        self.lock(child.pid)
        status, out, err = self.call("check")
        self.assertEqual((status, "ok: work browser" in out), (0, True), err)

    def test_work_browser_webdriver_and_windows(self):
        self.set(CHECK_WORK_BROWSER="1")
        child = self.browser_with_args(self.profile, "--marionette")
        self.lock(child.pid)
        self.windows["1"] = {"class": "f", "classname": "Navigator", "role": "browser", "desktop": 0, "pid": child.pid}
        self.write_state(windows=self.windows)
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("WebDriver", err)
        self.set(CHECK_ALLOW_WEBDRIVER="1")
        self.assertEqual(self.call("check")[0], 0)
        self.windows["2"] = dict(self.windows["1"], pid=child.pid + 1)
        self.write_state(windows=self.windows)
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("expected 1 browser window", err)

    def test_wrong_profile_on_the_browser(self):
        self.set(CHECK_WORK_BROWSER="1", CHECK_BROWSER_WINDOWS="any")
        child = self.browser_with_args(self.tmp / "other")
        self.lock(child.pid)
        status, _, err = self.call("check")
        self.assertEqual(status, 1)
        self.assertIn("was not started with -profile", err)


class SuperShortcutsTest(Env):
    def test_removes_only_super_bindings(self):
        config = self.home / ".config"
        path = config / "xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml"
        path.parent.mkdir(parents=True)
        path.write_text('<?xml version="1.0"?><channel name="x"><property name="commands"><property name="default">'
                        '<property name="&lt;Super&gt;r" type="string" value="appfinder"/>'
                        '<property name="&lt;Primary&gt;&lt;Alt&gt;t" type="string" value="terminal"/></property></property>'
                        '<property name="xfwm4"><property name="&lt;Super&gt;Tab" type="string" value="cycle"/></property></channel>')
        self.assertEqual(self.call("clear-super-shortcuts")[0], 0)
        text = path.read_text()
        self.assertNotIn("appfinder", text)
        self.assertIn("terminal", text)
        self.assertIn("cycle", text)          # window-manager bindings stay


class EntryPointTest(Env):
    def test_usage_and_dispatcher(self):
        self.assertEqual(self.call()[0], 2)
        self.assertEqual(self.call("nonsense")[0], 2)

    def test_started_as_root_needs_a_user(self):
        if os.getuid() != 0:
            self.skipTest("not root")
        status, _, err = self.call("launchers")
        self.assertEqual(status, 78)
        self.assertIn("MALRUNAR_SESSION_KIT_USER", err)

    def test_dispatcher_without_python_names_desktop_base(self):
        bash = Path("/bin/bash")
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "dirname").symlink_to("/usr/bin/dirname")
        result = subprocess.run([str(bash), str(BIN), "check"], env={"PATH": str(empty)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 78)
        self.assertIn("desktop-base", result.stderr)

    def test_shim_runs_the_module(self):
        result = subprocess.run([str(BIN)], capture_output=True, text=True, env=os.environ)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: malrunar-session", result.stderr)


if __name__ == "__main__":
    unittest.main()
