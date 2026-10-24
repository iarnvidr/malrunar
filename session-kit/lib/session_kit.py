#!/usr/bin/env python3
"""malrunar session-kit: small tools for an XFCE desktop session (standard library only).

Run through bin/malrunar-session. Every setting is an environment variable named
MALRUNAR_SESSION_KIT_*; nothing about a user, path, site or product is built in (README.md).

Subcommands: workspaces, restore-tabs, open-tabs, check, launchers, clear-super-shortcuts.
"""
import ctypes
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

PREFIX = "MALRUNAR_SESSION_KIT_"
EX_CONFIG = 78
DEPENDENCY_HINT = ("session-kit runs on desktop-base (python3, the X11 utilities and XFCE): "
                   "build your image FROM ghcr.io/iarnvidr/desktop-base")
XDOTOOL_HINT = "install the xdotool package in your own image layer (desktop-base does not ship it)"
WORK_BROWSER_HINT = ("this needs the optional work-browser component: add "
                     "'COPY --link --from=ghcr.io/iarnvidr/work-browser:<tag> / /' to your image, "
                     "or set MALRUNAR_SESSION_KIT_WORK_BROWSER_DIR to where it is installed")
DEFAULT_WORK_BROWSER_DIR = "/opt/malrunar/work-browser"
MAX_SESSION_BYTES = 32 * 1024 * 1024
FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_. -]+$")
SCHEMES = ("https://", "http://")


class ConfigError(Exception):
    """The image or the settings cannot run what was asked (exit status 78)."""


def log(message):
    print(f"malrunar-session-kit: {message}", file=sys.stderr, flush=True)


def setting(name, default=None):
    value = os.environ.get(PREFIX + name)
    return default if value is None else value


# --------------------------------------------------------------------------- running as the desktop user

def drop_privileges():
    """A program started as root (supervisord) switches to MALRUNAR_SESSION_KIT_USER; anyone else keeps their user."""
    if os.getuid() != 0:
        return
    user = setting("USER", "")
    if not user:
        raise ConfigError("started as root: set MALRUNAR_SESSION_KIT_USER to the desktop user "
                          "(create that user in your own image layer)")
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise ConfigError(f"MALRUNAR_SESSION_KIT_USER='{user}' does not exist in this image "
                          "(create the user in your own image layer)") from None
    os.initgroups(user, entry.pw_gid)
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    os.environ.update(HOME=entry.pw_dir, USER=user, LOGNAME=user)


def home():
    value = os.environ.get("HOME")
    if not value:
        value = pwd.getpwuid(os.getuid()).pw_dir
        os.environ["HOME"] = value
    return Path(value)


def require_command(name, hint=DEPENDENCY_HINT):
    if shutil.which(name) is None:
        raise ConfigError(f"'{name}' is missing. {hint}")


def run(args, **kwargs):
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 30)
    return subprocess.run(args, **kwargs)


# --------------------------------------------------------------------------- display and session bus

def display_answers(display):
    try:
        return run(["xdpyinfo", "-display", display], timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_display():
    """MALRUNAR_SESSION_KIT_DISPLAY, else $DISPLAY, else the newest X socket that answers; None if none does."""
    explicit = setting("DISPLAY", "")
    if explicit:
        return explicit
    current = os.environ.get("DISPLAY")
    if current and display_answers(current):
        return current
    try:
        sockets = sorted(Path("/tmp/.X11-unix").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        sockets = []
    for socket in sockets:
        match = re.fullmatch(r"X(\d+)", socket.name)
        if match and display_answers(":" + match.group(1)):
            return ":" + match.group(1)
    return None


def find_bus():
    explicit = setting("DBUS_ADDRESS", "")
    if explicit:
        return explicit
    current = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if current:
        return current
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    socket = Path(runtime) / "bus"
    return f"unix:path={socket}" if socket.exists() else None


def use_session():
    """Point DISPLAY and DBUS_SESSION_BUS_ADDRESS at the running session; False if there is none yet."""
    display = find_display()
    if display is None:
        return False
    os.environ["DISPLAY"] = display
    bus = find_bus()
    if bus:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = bus
    return True


# --------------------------------------------------------------------------- settings: workspaces and rules

def parse_workspaces(value):
    names = [name.strip() for name in (value or "").split(",") if name.strip()]
    if len(set(names)) != len(names):
        raise ConfigError(f"MALRUNAR_SESSION_KIT_WORKSPACES='{value}': workspace names must be different")
    if len(names) > 32:
        raise ConfigError("MALRUNAR_SESSION_KIT_WORKSPACES: at most 32 workspaces")
    return names


class Match:
    """A window selector: class= or classname= (WM_CLASS), optionally role= (WM_WINDOW_ROLE)."""

    def __init__(self, field, pattern, role=None):
        self.field, self.pattern, self.role = field, pattern, role

    def __repr__(self):
        return f"Match({self.field}={self.pattern}, role={self.role})"


def parse_match(parts, where):
    """parts: ['classname=Navigator', 'role=browser', ...] -> (Match, {other key: value})."""
    field = pattern = role = None
    extra = {}
    for part in parts:
        key, sep, value = part.strip().partition("=")
        if not sep or not value:
            raise ConfigError(f"{where}: '{part}' is not key=value")
        if key in ("class", "classname"):
            if field is not None:
                raise ConfigError(f"{where}: give one of class= and classname=")
            field, pattern = key, value
        elif key == "role":
            role = value
        else:
            extra[key] = value
    if field is None:
        raise ConfigError(f"{where}: expected class=<WM_CLASS> or classname=<WM_CLASS instance>")
    for text in (pattern, role):
        if text is not None and not FIELD_PATTERN.match(text):
            raise ConfigError(f"{where}: '{text}' may only contain letters, digits, space, '_', '.' and '-'")
    return Match(field, pattern, role), extra


def parse_rules(value):
    """MALRUNAR_SESSION_KIT_PLACE: 'classname=Navigator,role=browser@1;class=Mail@Mail' -> [(Match, workspace)].
    The workspace is a 1-based number or one of the MALRUNAR_SESSION_KIT_WORKSPACES names."""
    rules = []
    for entry in (value or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        where = f"MALRUNAR_SESSION_KIT_PLACE entry '{entry}'"
        selector, sep, workspace = entry.rpartition("@")
        if not sep or not workspace.strip():
            raise ConfigError(f"{where}: expected <selector>@<workspace>")
        match, extra = parse_match(selector.split(","), where)
        if extra:
            raise ConfigError(f"{where}: unknown key {', '.join(sorted(extra))}")
        rules.append((match, workspace.strip()))
    return rules


def resolve_workspace(workspace, names):
    """0-based index of a workspace given as a 1-based number or a name."""
    if workspace.isdigit():
        index = int(workspace) - 1
        if index < 0 or (names and index >= len(names)):
            raise ConfigError(f"workspace {workspace} is outside 1..{len(names)}")
        return index
    if workspace in names:
        return names.index(workspace)
    raise ConfigError(f"workspace '{workspace}' is not a number and not one of MALRUNAR_SESSION_KIT_WORKSPACES")


def parse_window_checks(value):
    """MALRUNAR_SESSION_KIT_CHECK_WINDOWS: 'classname=Navigator,role=browser,count=1;class=Foo' -> [(Match, count)]."""
    checks = []
    for entry in (value or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        where = f"MALRUNAR_SESSION_KIT_CHECK_WINDOWS entry '{entry}'"
        match, extra = parse_match(entry.split(","), where)
        count = extra.pop("count", "1")
        if extra or not count.isdigit():
            raise ConfigError(f"{where}: only class=, classname=, role= and count=<number> are known")
        checks.append((match, int(count)))
    return checks


# --------------------------------------------------------------------------- windows (xdotool, xprop)

def window_regex(pattern):
    return "^" + pattern.replace(".", r"\.") + "$"


def window_role(window):
    out = run(["xprop", "-id", window, "WM_WINDOW_ROLE"])
    found = re.search(r'"([^"]*)"', out.stdout)
    return found.group(1) if out.returncode == 0 and found else None


def find_windows(match):
    flag = "--class" if match.field == "class" else "--classname"
    out = run(["xdotool", "search", flag, window_regex(match.pattern)])
    windows = out.stdout.split()
    if match.role is not None:
        windows = [w for w in windows if window_role(w) == match.role]
    return windows


# --------------------------------------------------------------------------- workspaces

def xfconf(*args):
    return run(["xfconf-query", *args], check=True).stdout.strip()


def session_ready():
    try:
        xfconf("-c", "xfwm4", "-p", "/general/workspace_count")
        run(["xdotool", "get_desktop"], check=True)
        return True
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False


def wait_for_session(wait_seconds, poll):
    deadline = None if wait_seconds <= 0 else time.monotonic() + wait_seconds
    while True:
        if use_session() and session_ready():
            return
        if deadline is not None and time.monotonic() > deadline:
            raise SystemExit("malrunar-session-kit: no desktop session (display, window manager, settings daemon) "
                             f"appeared within {wait_seconds} s")
        time.sleep(poll)


def wait_for_desktops(count, timeout=15.0):
    """The window manager applies a new workspace count asynchronously; placing a window on a workspace
    that does not exist yet would be refused."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = run(["xdotool", "get_num_desktops"])
        if out.returncode == 0 and out.stdout.strip() == str(count):
            return True
        time.sleep(0.2)
    return False


def apply_workspaces(names):
    xfconf("-c", "xfwm4", "-p", "/general/workspace_count", "-s", str(len(names)))
    args = ["-c", "xfwm4", "-p", "/general/workspace_names", "-n", "-a"]
    for name in names:
        args += ["-t", "string", "-s", name]
    xfconf(*args)


def apply_panel_tweaks():
    """Task list shows the current workspace's windows only, and the pager is one row of names (the existing
    panel layout is kept)."""
    for line in xfconf("-c", "xfce4-panel", "-lv").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].startswith("/plugins/plugin-"):
            continue
        if parts[1] == "tasklist":
            for key, kind, value in (("grouping", "uint", "0"), ("include-all-workspaces", "bool", "false")):
                xfconf("-c", "xfce4-panel", "-p", f"{parts[0]}/{key}", "-n", "-t", kind, "-s", value)
        elif parts[1] == "pager":
            xfconf("-c", "xfce4-panel", "-p", f"{parts[0]}/rows", "-n", "-t", "uint", "-s", "1")
            xfconf("-c", "xfce4-panel", "-p", f"{parts[0]}/miniature-view", "-n", "-t", "bool", "-s", "false")


def place_windows(rules, seen):
    """Move every window that matches a rule to its workspace, once per window. Returns the windows placed now."""
    placed = []
    for match, index in rules:
        for window in find_windows(match):
            if window in seen:
                continue
            try:
                run(["xdotool", "set_desktop_for_window", window, str(index)], check=True)
            except subprocess.CalledProcessError:
                continue
            seen.add(window)
            placed.append(window)
    return placed


def idle_until_stopped():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(3600)


def cmd_workspaces(args):
    names = parse_workspaces(setting("WORKSPACES"))
    rules = [(match, resolve_workspace(workspace, names)) for match, workspace in parse_rules(setting("PLACE"))]
    panel = setting("PANEL_TWEAKS", "0") == "1"
    once = "--once" in args
    if not names and not rules and not panel:
        log("nothing to do: set MALRUNAR_SESSION_KIT_WORKSPACES, MALRUNAR_SESSION_KIT_PLACE or "
            "MALRUNAR_SESSION_KIT_PANEL_TWEAKS")
        if once:
            return 0
        idle_until_stopped()
    drop_privileges()
    for command in ("xdpyinfo", "xprop", "xfconf-query"):
        require_command(command)
    require_command("xdotool", XDOTOOL_HINT)
    poll = float(setting("POLL_SECONDS", "2"))
    wait_for_session(int(setting("WAIT_SECONDS", "120" if once else "0")), poll)
    if names:
        apply_workspaces(names)
        if not wait_for_desktops(len(names)):
            log(f"the window manager did not report {len(names)} workspaces in time; continuing")
        log(f"{len(names)} workspaces: {', '.join(names)}")
    if panel:
        apply_panel_tweaks()
    seen = set()
    while True:
        for window in place_windows(rules, seen):
            log(f"placed window {window}")
        if once or not rules:
            return 0 if once else idle_until_stopped()
        time.sleep(poll)


# --------------------------------------------------------------------------- Firefox session store

def liblz4():
    for name in ("liblz4.so.1", "liblz4.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise ConfigError(f"liblz4.so.1 is missing. {DEPENDENCY_HINT}")


def read_mozlz4(path):
    """Decode a Firefox mozLz4 file (sessionstore.jsonlz4, recovery.jsonlz4); None if it is not a valid one."""
    data = path.read_bytes()
    if data[:8] != b"mozLz40\0":
        return None
    length = int.from_bytes(data[8:12], "little")
    if not 0 < length <= MAX_SESSION_BYTES:
        return None
    library = liblz4()
    decoded = ctypes.create_string_buffer(length)
    if library.LZ4_decompress_safe(data[12:], decoded, len(data) - 12, length) != length:
        return None
    return decoded.raw


def session_urls(profile, max_tabs=200):
    """http(s) URLs of the tabs of the newest readable session store of a profile, in window and tab order."""
    candidates = [profile / "sessionstore.jsonlz4", profile / "sessionstore-backups/recovery.jsonlz4",
                  profile / "sessionstore-backups/recovery.baklz4"]
    existing = sorted((p for p in candidates if p.exists()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in existing:
        try:
            raw = read_mozlz4(path)
            if raw is None:
                continue
            urls = []
            for window in json.loads(raw)["windows"]:
                for tab in window["tabs"]:
                    entries = tab.get("entries", [])
                    if entries:
                        url = entries[tab.get("index", 1) - 1]["url"]
                        if url.startswith(SCHEMES):
                            urls.append(url)
            return urls[:max_tabs]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
    return []


# --------------------------------------------------------------------------- the optional work-browser component

def work_browser_dir():
    return Path(setting("WORK_BROWSER_DIR", DEFAULT_WORK_BROWSER_DIR))


def work_browser_open_helper():
    helper = work_browser_dir() / "bin/malrunar-work-browser-open"
    if not os.access(helper, os.X_OK):
        raise ConfigError(f"{helper} not found: {WORK_BROWSER_HINT}")
    return helper


def work_browser_profile():
    """The profile the work-browser component keeps: its own setting, else its documented default."""
    explicit = os.environ.get("MALRUNAR_WORK_BROWSER_PROFILE_DIR")
    return Path(explicit) if explicit else home() / ".config/malrunar-work-browser/profile"


def source_profile(args):
    """Profile whose last session to read: the argument, MALRUNAR_SESSION_KIT_PROFILE_DIR, or (only when the
    work-browser component is present) the work browser's profile."""
    positional = [a for a in args if not a.startswith("-")]
    if positional:
        return Path(positional[0])
    explicit = setting("PROFILE_DIR", "")
    if explicit:
        return Path(explicit)
    if (work_browser_dir() / "bin/malrunar-work-browser").exists():
        return work_browser_profile()
    raise ConfigError("no profile to read: pass a profile directory, set MALRUNAR_SESSION_KIT_PROFILE_DIR, "
                      f"or add the work-browser component whose profile is the default ({WORK_BROWSER_HINT})")


def restore_list(args):
    drop_privileges()
    profile = source_profile(args)
    urls = session_urls(profile, int(setting("MAX_TABS", "200")))
    fallback = setting("FALLBACK_URL", "")
    if not urls and fallback:
        urls = [fallback]
    return profile, urls


def cmd_restore_tabs(args):
    _, urls = restore_list(args)
    for url in urls:
        sys.stdout.buffer.write(url.encode() + b"\0")
    return 0


def cmd_open_tabs(args):
    # Resolve the optional component first so a missing one is reported before anything is read.
    drop_privileges()
    helper = work_browser_open_helper()
    profile, urls = restore_list(args)
    if not urls:
        log(f"no saved tabs in {profile}")
        return 0
    timeout = float(setting("OPEN_TIMEOUT", "30"))
    for url in urls:
        result = run([str(helper), url], timeout=timeout)
        if result.returncode != 0:
            log(f"the work browser did not take {url} (exit status {result.returncode}): {result.stderr.strip()}")
            return result.returncode
    log(f"opened {len(urls)} tab(s) from {profile}")
    return 0


# --------------------------------------------------------------------------- check

def parse_workspace_names(output):
    lines = output.splitlines()
    if lines and lines[0].startswith("Value is an array"):
        return [line for line in lines[1:] if line.strip()]
    return [line for line in lines if line.strip()]


def lock_owner(profile):
    try:
        target = os.readlink(profile / "lock")
    except OSError:
        return None
    found = re.search(r":\+(\d+)$", target)
    return int(found.group(1)) if found else None


def check_work_browser(problems, ok):
    work_browser_open_helper()  # raises ConfigError naming the component when it is absent
    profile = work_browser_profile()
    if not profile.is_dir() or not os.access(profile, os.W_OK):
        problems.append(f"work browser profile {profile} is not a writable directory")
        return
    pid = lock_owner(profile)
    try:
        if pid is None:
            raise ProcessLookupError
        os.kill(pid, 0)
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except (ProcessLookupError, OSError):
        problems.append(f"the work browser is not running on {profile}")
        return
    if "-profile" not in args or args[args.index("-profile") + 1] != str(profile):
        problems.append(f"the browser that holds {profile} was not started with -profile {profile}")
    if setting("CHECK_ALLOW_WEBDRIVER", "0") != "1" and any(
            a.startswith(("--remote-debugging-port", "--marionette")) for a in args):
        problems.append("the work browser runs with a WebDriver or debugging port "
                        "(MALRUNAR_SESSION_KIT_CHECK_ALLOW_WEBDRIVER=1 allows it)")
    expected = setting("CHECK_BROWSER_WINDOWS", "1")
    if expected != "any":
        windows = find_windows(Match("classname", "Navigator", "browser"))
        owned = [w for w in windows if run(["xdotool", "getwindowpid", w]).stdout.strip() == str(pid)]
        if len(windows) != int(expected) or len(owned) != len(windows):
            problems.append(f"expected {expected} browser window(s), all owned by the work browser (pid {pid}); "
                            f"found {len(windows)}, {len(owned)} of them owned by it")
    if not problems:
        ok.append(f"work browser (pid {pid}) runs on {profile}")


def check_addons(problems, ok, ids, profile):
    try:
        addons = json.loads((profile / "extensions.json").read_text())["addons"]
    except (OSError, ValueError, KeyError):
        problems.append(f"cannot read the add-on list of {profile}")
        return
    for identifier in ids:
        if any(a.get("id") == identifier and a.get("active") for a in addons):
            ok.append(f"add-on {identifier} is active")
        else:
            problems.append(f"add-on {identifier} is not active in {profile}")


def cmd_check(args):
    drop_privileges()
    for command in ("xdpyinfo", "xprop"):
        require_command(command)
    require_command("xdotool", XDOTOOL_HINT)
    problems, ok = [], []
    if not use_session():
        print("check failed: no X display answers", file=sys.stderr)
        return 1
    ok.append(f"display {os.environ['DISPLAY']} answers")
    names = parse_workspaces(setting("WORKSPACES"))
    if names:
        require_command("xfconf-query")
        try:
            count = int(xfconf("-c", "xfwm4", "-p", "/general/workspace_count"))
            actual = parse_workspace_names(xfconf("-c", "xfwm4", "-p", "/general/workspace_names"))
        except (subprocess.CalledProcessError, ValueError) as error:
            problems.append(f"cannot read the workspace settings: {error}")
        else:
            if count != len(names) or actual[:len(names)] != names:
                problems.append(f"workspaces are {count} {actual}, expected {len(names)} {names}")
            else:
                ok.append(f"{count} workspaces: {', '.join(names)}")
    for match, expected in parse_window_checks(setting("CHECK_WINDOWS")):
        found = len(find_windows(match))
        if found == expected:
            ok.append(f"{expected} window(s) {match.field}={match.pattern}")
        else:
            problems.append(f"expected {expected} window(s) {match.field}={match.pattern}"
                            f"{',role=' + match.role if match.role else ''}, found {found}")
    if setting("CHECK_WORK_BROWSER", "0") == "1":
        check_work_browser(problems, ok)
    addons = [a.strip() for a in setting("CHECK_ADDONS", "").split(",") if a.strip()]
    if addons:
        profile = Path(setting("PROFILE_DIR", "")) if setting("PROFILE_DIR") else work_browser_profile()
        check_addons(problems, ok, addons, profile)
    for line in ok:
        print(f"ok: {line}")
    for line in problems:
        print(f"FAILED: {line}", file=sys.stderr)
    return 1 if problems else 0


# --------------------------------------------------------------------------- launchers

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name={name}
GenericName=Web Browser
Icon={icon}
Exec={command} %u
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
"""

XFCE_HELPER = """[Desktop Entry]
Version=1.0
Type=X-XFCE-Helper
Name={name}
Icon={icon}
StartupNotify=false
X-XFCE-Category=WebBrowser
X-XFCE-Commands={command};
X-XFCE-CommandsWithParameter={command} "%s";
"""


def launcher_command():
    command = setting("BROWSER_COMMAND", "")
    if command:
        return command
    return str(work_browser_open_helper())


def cmd_launchers(args):
    drop_privileges()
    command = launcher_command()
    name = setting("LAUNCHER_NAME", "Web Browser")
    identifier = setting("LAUNCHER_ID", "malrunar-session-kit-browser")
    icon = setting("LAUNCHER_ICON", "internet-web-browser")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
        raise ConfigError("MALRUNAR_SESSION_KIT_LAUNCHER_ID may only contain letters, digits, '.', '_' and '-'")
    for label, text in (("MALRUNAR_SESSION_KIT_BROWSER_COMMAND", command), ("MALRUNAR_SESSION_KIT_LAUNCHER_NAME", name),
                        ("MALRUNAR_SESSION_KIT_LAUNCHER_ICON", icon)):
        if "\n" in text or "\r" in text or "%" in text:
            raise ConfigError(f"{label} must be one line without '%'")
    base = home()
    data = Path(os.environ.get("XDG_DATA_HOME") or base / ".local/share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or base / ".config")
    files = {
        data / "applications" / f"{identifier}.desktop": DESKTOP_ENTRY.format(name=name, command=command, icon=icon),
        data / "xfce4/helpers" / f"{identifier}.desktop": XFCE_HELPER.format(name=name, command=command, icon=icon),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    helpers = config / "xfce4/helpers.rc"
    helpers.parent.mkdir(parents=True, exist_ok=True)
    kept = [line for line in (helpers.read_text().splitlines() if helpers.exists() else [])
            if not line.startswith("WebBrowser=")]
    temporary = helpers.with_name(helpers.name + ".new")
    temporary.write_text("\n".join(kept + [f"WebBrowser={identifier}"]) + "\n")
    temporary.replace(helpers)
    log(f"'{command}' is the web browser of {base} ({identifier})")
    return 0


# --------------------------------------------------------------------------- Super shortcuts

def cmd_clear_super_shortcuts(args):
    """Drop Xfce's <Super> application shortcuts for the user, before the session starts. A Mac client sends
    Cmd as Super, so Cmd+R would otherwise run Xfce's <Super>r binding. Window-manager bindings are kept."""
    drop_privileges()
    name = "xfce4-keyboard-shortcuts.xml"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home() / ".config")
    user = config / "xfce4/xfconf/xfce-perchannel-xml" / name
    system = Path("/etc/xdg/xfce4/xfconf/xfce-perchannel-xml") / name
    if not user.exists() and system.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(system, user)
    removed = 0
    if user.exists():
        tree = ET.parse(user)
        for section in tree.getroot().findall("./property[@name='commands']/property"):
            for binding in list(section):
                if "<Super>" in binding.get("name", ""):
                    section.remove(binding)
                    removed += 1
        if removed:
            tree.write(user, encoding="UTF-8", xml_declaration=True)
    log(f"removed {removed} <Super> shortcut(s)")
    return 0


# --------------------------------------------------------------------------- entry point

COMMANDS = {
    "workspaces": cmd_workspaces,
    "restore-tabs": cmd_restore_tabs,
    "open-tabs": cmd_open_tabs,
    "check": cmd_check,
    "launchers": cmd_launchers,
    "clear-super-shortcuts": cmd_clear_super_shortcuts,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print("usage: malrunar-session {" + "|".join(COMMANDS) + "} [args]  (see README.md)", file=sys.stderr)
        return 2
    try:
        return COMMANDS[argv[0]](argv[1:]) or 0
    except ConfigError as error:
        log(str(error))
        return EX_CONFIG
    except subprocess.TimeoutExpired as error:
        log(f"timed out: {error.cmd}")
        return 1
    except subprocess.CalledProcessError as error:
        log(f"{error.cmd[0]} failed (exit status {error.returncode}): {(error.stderr or '').strip()}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
