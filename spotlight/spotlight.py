#!/usr/bin/env python3
"""Dim the panes of the columns (= jobs) that are not focused in herdr, and give each column its own tint.

Layout convention: one column = one job. Vertical splits inside a column are that job's helpers.
The focused column keeps the plain background. Every other column is dimmed and gets a tint of its own.
This replaces tmux's window-style / window-active-style.

herdr has no per-pane color setting, so this writes a background color escape sequence (OSC 11) to each pane's tty.
Which pane has which color is kept in herdr's pane metadata (tokens).
Tokens are lost when the herdr server restarts, so the column hues are also saved to a file under STATE,
so that the same job gets the same color after a restart.
Called from the event hooks in herdr-plugin.toml. With `--force`, panes that are already painted are repainted too.

Hooks alone are not enough, so when called from a hook this starts one watcher process in the background.
The watcher repaints based on these five things:
  1. herdr events. It also subscribes to layout.updated (such as swapping panes within a tab), which hooks never receive
  2. The layout of the visible tab. herdr delivers events only in 100ms steps, so right after keyboard or mouse input
     it asks for the layout every POLL seconds, and while there is no input but panes are producing output
     (an agent is running, for example) once every few of those, so it follows focus moves, zoom and new panes right away.
     When it finds a change, it first writes the backgrounds using the tokens it remembers (early_paint),
     then paints all tabs properly (paint_tabs)
  3. Whether the terminal background is light or dark (decided from Ghostty's config and the macOS appearance).
     When it flips, every pane gets the new colors
  4. Forks of the herdr server. Kernel notifications tell it the moment a new pane's shell starts,
     so it paints before herdr draws the first frame
  5. Output to dimmed panes. The program in the pane may have reset the background, so it rewrites it once output pauses (heal)
When the herdr server stops, the watcher keeps trying to reconnect for a while and then exits.
When this file is modified, the watcher restarts itself with the new contents.
"""
import fcntl
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import sys
import termios
import time

# Data definitions
#   Rect   = {"x": int, "y": int, "width": int, "height": int}
#   Layout = {"area": Rect, "panes": [{"pane_id": str, "rect": Rect}], "splits": [Split]}  the layout of one tab
#   Split  = {"direction": "right" | "down", "ratio": float, "rect": Rect}
#   Column = [pane_id]  the panes of one column, top to bottom
#   Hue    = int        an index into PALETTE. The kind of color that stands for a column (job)
#   Theme  = "light" | "dark"
#   Color  = "#rrggbb" | DEFAULT
#   Memo   = {pane_id: Hue}  the column hues saved to a file. herdr pane IDs survive restarts and are never reused
#   Tokens = {pane_id: {token name: str}}  pane metadata held by herdr. Holds HUE_TOKEN and the like

# Backgrounds of unfocused columns. Darker than the author's plain backgrounds (light: #f9f9f9, dark: tokyo-night #1a1b26),
# with a slight tint (the author's tmux window-style was colour254 = #e4e4e4). The same index has the same hue in both themes
PALETTE = {
    "light": ["#dde4f0", "#dde9df", "#eee5d8", "#e6dff0", "#efdfe5", "#d9e8e7"],
    "dark": ["#11172a", "#0f1a14", "#1d1610", "#181227", "#1e1118", "#0d1a1c"],
}
DEFAULT = "default"  # the plain background (the focused column)
SOURCE = "spotlight"
PLUGIN_ID = "naoyafurudono.spotlight"
HERE = os.path.dirname(os.path.abspath(__file__))  # this installation's plugin directory
HUE_TOKEN = "spotlight_hue"  # the Hue assigned to the pane's column
BG_TOKEN = "spotlight_bg"  # the Color last written to the tty
TTY_TOKEN = "spotlight_tty"  # the path of the pane's tty. It does not change while the pane lives, so remember it
INO_TOKEN = "spotlight_ino"  # the inode of that tty. tty path numbers are reused by other terminals after closing, so check this before writing to make sure it is the same terminal
WATCHED = ["layout.updated", "pane.created", "pane.moved", "pane.closed", "pane.exited", "pane.focused", "tab.focused"]
POLL = 0.01  # how often the watcher asks for the layout of the visible tab (seconds)
# Ask only within this many seconds of the last input or the last pane output. While neither a person nor an agent is active, herdr is not queried.
# Input is measured for the whole Mac, so querying continues while you operate apps other than herdr. The environment variable is for testing
ACTIVE = float(os.environ.get("SPOTLIGHT_ACTIVE", 2.0))
# Delay before the watcher retries when some panes could not be painted (seconds). Right after a shell starts it becomes paintable quickly, so the first steps are short
RETRY_AFTER = [0.02, 0.03, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 1.0, 1.0]
QUIET = 0.1  # seconds without output before a pane counts as quiet. Then panes whose background may have been reset are rewritten (heal)
HEAL_EVERY = 2.0  # minimum interval between rewrites of one pane (seconds)
OUTPUT_POLL = 3  # while there is no input but panes produce output, ask for the layout once every this many POLLs
RESIZE_WAIT = 0.012  # upper bound on waiting, after a new pane's shell starts, for the split pane's terminal to shrink (seconds). Measured: 3 to 8ms
FORK_WAIT = 0.004  # upper bound on waiting for a child forked by the herdr server to get a controlling terminal (seconds). Measured: about 1.5ms
RECONNECT = 60  # seconds the watcher keeps trying to reconnect after the herdr server stops
SLOW_POLL = 0.5  # how often the watcher checks whether this file was modified (seconds)
VERSION = os.stat(__file__).st_mtime  # the modification time of this file when it was loaded. Used by the watcher to notice edits
# What this process remembers: "memo" (Memo), "tokens" and "layouts" (the Tokens and all tabs' Layouts seen by the last paint_tabs), "paint_lock",
# "written" ({tty: (Color written, the tty's modification time at that point, the tty's inode)}. Used by heal and output_recent),
# "seen" and "healed" (heal's bookkeeping)
CACHE = {}
SOCKET = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def columns(layout):
    """Layout -> [Column]. Split a tab into columns, left to right."""
    area = layout["area"]
    # column boundaries = positions of full-height left/right splits
    bounds = sorted(
        s["rect"]["x"] + int(s["rect"]["width"] * s["ratio"] + 0.5)  # round .5 up, as herdr does
        for s in layout["splits"]
        if s["direction"] == "right" and s["rect"]["y"] == area["y"] and s["rect"]["height"] == area["height"]
    )
    cols = [[] for _ in range(len(bounds) + 1)]
    for p in sorted(layout["panes"], key=lambda p: (p["rect"]["y"], p["rect"]["x"])):
        center = p["rect"]["x"] + p["rect"]["width"] / 2
        cols[sum(1 for b in bounds if b <= center)].append(p["pane_id"])
    return [c for c in cols if c]


def assign_hues(cols, known, n):
    """[Column], {pane_id: Hue}, number of hues -> {pane_id: Hue}. Choose a Hue per column, keeping already assigned column hues where possible."""
    # An assigned hue goes to the column that has the most panes of that hue. Each hue goes to only one column.
    # On ties, prefer the column with no other candidate hue, then the left column, then the hue of the higher pane in the column
    claims = []  # (priority, column index, Hue)
    for i, col in enumerate(cols):
        col_known = [known[p] for p in col if p in known]
        for hue in dict.fromkeys(col_known):
            claims.append(((-col_known.count(hue), len(set(col_known)), i, col_known.index(hue)), i, hue))
    col_hues = [None] * len(cols)
    for _, i, hue in sorted(claims):
        if col_hues[i] is None and hue not in col_hues:
            col_hues[i] = hue
    # When there are more columns than hues, several columns share a hue. A column keeps the hue it had if it does not clash with a neighbour
    if len(cols) > n:
        for i, col in enumerate(cols):
            neighbours = col_hues[max(i - 1, 0) : i + 2]
            if col_hues[i] is None:
                col_hues[i] = next((known[p] for p in col if p in known and known[p] not in neighbours), None)
    # hue of a new column = the least used hue among those different from both neighbours
    for i, hue in enumerate(col_hues):
        if hue is None:
            neighbours = col_hues[max(i - 1, 0) : i + 2]
            col_hues[i] = min(range(n), key=lambda h: (h in neighbours, col_hues.count(h), h))
    return {p: hue for col, hue in zip(cols, col_hues) for p in col}


def repaints(hues, applied, theme, focused, force=False):
    """{pane_id: Hue}, {pane_id: Color}, Theme, focused Column -> {pane_id: Color}. Return the panes whose background must be rewritten, with their colors.

    The panes of the focused column come first, so the column you are looking at changes first.
    """
    colors = {p: DEFAULT if p in focused else PALETTE[theme][hue] for p, hue in hues.items()}
    stale = [p for p, c in colors.items() if force or applied.get(p) != c]
    return {p: colors[p] for p in sorted(stale, key=lambda p: p not in focused)}


def request(method, **params):
    """Send a request to herdr's socket API and return the socket without waiting for the response. Read the response with response."""
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(5)
        sock.connect(SOCKET)
        sock.sendall((json.dumps({"id": SOURCE, "method": method, "params": params}) + "\n").encode())
    except OSError:
        sock.close()
        raise
    return sock


def response(sock):
    """Read one response line from a socket returned by request, return it, and close the socket."""
    with sock:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    return buf


def call(method, **params):
    """Call herdr's socket API once and return the raw response line. Talks to the socket directly because it is faster than starting the CLI."""
    return response(request(method, **params))


def herdr(method, **params):
    """Call herdr's socket API once and return the result."""
    res = json.loads(call(method, **params))
    if "error" in res:
        raise RuntimeError(f"{method}: {res['error']}")
    return res["result"]


def appearance_reader():
    """Return a () -> Theme function. The returned function returns the macOS appearance (light / dark) as a Theme.

    The watcher calls it every POLL seconds, so it reads the setting directly through CoreFoundation instead of starting
    the defaults command. Where that cannot be loaded, it falls back to defaults.
    """
    try:
        import ctypes
        import ctypes.util

        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        ptr, utf8 = ctypes.c_void_p, 0x08000100
        cf.CFStringCreateWithCString.restype, cf.CFStringCreateWithCString.argtypes = ptr, [ptr, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFPreferencesCopyAppValue.restype, cf.CFPreferencesCopyAppValue.argtypes = ptr, [ptr, ptr]
        cf.CFPreferencesAppSynchronize.argtypes = [ptr]
        cf.CFStringGetCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ptr]
        key = cf.CFStringCreateWithCString(None, b"AppleInterfaceStyle", utf8)
        domain = ptr.in_dll(cf, "kCFPreferencesAnyApplication")

        def read():
            cf.CFPreferencesAppSynchronize(domain)  # pick up changes written by other processes
            value = cf.CFPreferencesCopyAppValue(key, domain)
            if not value:  # the key is absent in light mode
                return "light"
            buf = ctypes.create_string_buffer(32)
            cf.CFStringGetCString(value, buf, len(buf), utf8)
            cf.CFRelease(value)
            return "dark" if buf.value == b"Dark" else "light"

        read()
        return read
    except Exception:

        def read():
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
            return "dark" if r.stdout.strip() == "Dark" else "light"

        return read


def ghostty_background(config, appearance, read_theme):
    """Ghostty config contents, macOS appearance, function from theme name to theme file contents | None -> Color | None. Return Ghostty's background color.

    `background = ` if present. Otherwise the background color of the `theme = ` theme. For the `light:A,dark:B` form,
    the macOS appearance picks one. None if it cannot be read.
    """

    def settings(text):
        found = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                found[key.strip()] = value.strip().strip('"')
        return found

    def color(value):
        value = value.lstrip("#").lower()
        return f"#{value}" if len(value) == 6 and all(c in "0123456789abcdef" for c in value) else None

    found = settings(config)
    if "background" in found:
        return color(found["background"])
    name = found.get("theme", "")
    variants = dict(part.split(":", 1) for part in name.split(",") if ":" in part)
    name = variants.get(appearance, name).strip()
    theme = read_theme(name) if name else None
    return color(settings(theme).get("background", "")) if theme else None


def theme_of(color):
    """Color ("#rrggbb") -> Theme. "light" if the background color is bright, "dark" if it is dark."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return "light" if 0.2126 * r + 0.7152 * g + 0.0722 * b >= 128 else "dark"


def read_ghostty_theme(name):
    """Ghostty theme name -> theme file contents | None."""
    dirs = [os.path.expanduser("~/.config/ghostty/themes"), "/Applications/Ghostty.app/Contents/Resources/ghostty/themes"]
    for path in [name] if os.path.isabs(name) else [os.path.join(d, name) for d in dirs]:
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            pass
    return None


def theme_reader():
    """Return a () -> Theme function. The returned function returns whether the plain pane background is light or dark, as a Theme.

    The plain pane background is the terminal's background color, not herdr's theme. If the terminal is Ghostty, the
    background color is read from its config (if the color scheme is pinned to a light theme, this is "light" even when
    macOS is dark). If that cannot be read, the macOS appearance is used.
    """
    appearance = appearance_reader()
    path = os.path.expanduser("~/.config/ghostty/config")
    seen = {}  # the last decision: "key" (the config's modification time and the macOS appearance), "theme"

    def read():
        now = appearance()
        try:
            key = (os.stat(path).st_mtime, now)
            if seen.get("key") != key:  # re-read only when the config or the appearance changed
                with open(path) as f:
                    background = ghostty_background(f.read(), now, read_ghostty_theme)
                seen.update(key=key, theme=theme_of(background) if background else now)
            return seen["theme"]
        except OSError:  # Ghostty is not in use
            return now

    return read


def current_theme():
    """Return the current Theme."""
    return theme_reader()()


def split_request(argv, env):
    """herdr command arguments, environment variables -> split request | None. None if it is not `herdr pane split`.

    split request = {"target": ID of the pane being split | None, "direction": "right" | "down", "focus": bool, "socket": socket of the target herdr server}
    """
    if "--machine" in argv or "--remote" in argv:
        return None
    try:
        at = next(i for i in range(len(argv) - 1) if argv[i] == "pane" and argv[i + 1] == "split")
    except StopIteration:
        return None
    options, target, rest = {}, None, argv[at + 2 :]
    while rest:
        arg = rest.pop(0)
        if arg in ("--pane", "--direction", "--ratio", "--cwd", "--env", "--right-click"):
            options[arg] = rest.pop(0) if rest else ""
        elif not arg.startswith("--"):
            target = arg
        else:
            options[arg] = True
    session = argv[argv.index("--session") + 1] if "--session" in argv[:at] else None
    socket_path = os.path.expanduser(f"~/.config/herdr/sessions/{session}/herdr.sock") if session else env.get("HERDR_SOCKET_PATH")
    return {
        "target": target or options.get("--pane") or env.get("HERDR_PANE_ID"),  # when omitted, the calling pane
        "direction": options.get("--direction"),
        "focus": "--focus" in options,
        "socket": socket_path or os.path.expanduser("~/.config/herdr/herdr.sock"),
    }


def predict_color(request, layouts, tokens, theme):
    """split request, [Layout], Tokens, Theme -> Color | None. Estimate the background color of the pane being added, before it is added. None if unknown.

    herdr draws a new pane's first frame immediately, so asking for the layout after the pane is added is too late.
    """
    if request["focus"]:
        return DEFAULT
    target = request["target"]
    layout = next((lay for lay in layouts if any(p["pane_id"] == target for p in lay["panes"])), None)
    if layout is None:
        return None
    cols = columns(layout)
    at = next(i for i, col in enumerate(cols) if target in col)
    rect, area = next(p["rect"] for p in layout["panes"] if p["pane_id"] == target), layout["area"]
    if request["direction"] == "right" and rect["y"] == area["y"] and rect["height"] == area["height"]:
        # splitting a full-height pane left/right makes a new column on the right
        known = {p: h for col in cols for p in col if (h := hue_of(tokens.get(p, {}))) is not None}
        new = "\0new"
        return PALETTE[theme][assign_hues(cols[: at + 1] + [[new]] + cols[at + 1 :], known, len(PALETTE[theme]))[new]]
    if layout.get("focused_pane_id") in cols[at]:
        return DEFAULT
    hue = hue_of(tokens.get(target, {}))
    return PALETTE[theme][hue] if hue is not None else None


def split_from_resize(before, after):
    """{pane_id: (rows, cols)}, {pane_id: (rows, cols)} -> (ID of the pane that was split, "down" | "right") | None.

    Read which pane was split in which direction from the change in terminal sizes. Only when exactly one pane shrank.
    """
    changed = [p for p in before if p in after and after[p] != before[p]]
    if len(changed) != 1:
        return None
    (rows, cols), (new_rows, new_cols) = before[changed[0]], after[changed[0]]
    if new_rows < rows and new_cols == cols:
        return changed[0], "down"
    if new_cols < cols and new_rows == rows:
        return changed[0], "right"
    return None


def resize_watcher(ttys):
    """{pane_id: tty} -> (() -> {pane_id: (rows, cols)}, () -> None) | None. Return a function that reads terminal sizes repeatedly, and a cleanup function."""
    fds = {}
    try:
        for pane_id, tty in ttys.items():
            fds[pane_id] = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        pass
    empty = b"\0" * 8

    def sizes():
        return {p: struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, empty))[:2] for p, fd in fds.items()}

    def close():
        for fd in fds.values():
            os.close(fd)

    return sizes, close


def command_scanner():
    """Return a () -> [(arguments, environment variables)] function. The returned function returns the herdr commands that appeared since the previous call. None where unavailable.

    Checking the names of all processes every time takes over 1ms, so pids already checked are remembered and only new pids are checked.
    """
    try:
        import ctypes
        import ctypes.util

        libproc = ctypes.CDLL(ctypes.util.find_library("proc"))
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        pids, name = (ctypes.c_int * 8192)(), ctypes.create_string_buffer(64)
        args = ctypes.create_string_buffer(1 << 18)
        libproc.proc_listallpids(pids, ctypes.sizeof(pids))
    except Exception:
        return None
    checked = set()

    def procargs(pid):
        size = ctypes.c_size_t(len(args))
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        if libc.sysctl(mib, 3, args, ctypes.byref(size), None, 0) != 0:
            return None
        raw = args.raw[: size.value]
        argc = struct.unpack_from("i", raw)[0]
        strings = [part.decode(errors="replace") for part in raw[4:].split(b"\0")[1:] if part]  # the first one is the executable path
        return strings[:argc], dict(item.split("=", 1) for item in strings[argc:] if "=" in item)

    def scan():
        nonlocal checked
        now = set(pids[: max(libproc.proc_listallpids(pids, ctypes.sizeof(pids)), 0)])
        found = []
        for pid in now - checked:
            if libproc.proc_name(pid, name, len(name)) > 0 and name.value == b"herdr":
                command = procargs(pid)
                if command:
                    found.append(command)
        checked = now  # forget the pids of finished processes (they get reused by other processes)
        return found

    scan()
    return scan


def fork_watcher(pid):
    """Watch process pid for forks. Return (kqueue, () -> [tty]). None where it cannot be watched.

    The kqueue becomes readable when pid forks. The returned function returns the ttys of the children added since the
    previous call that have a controlling terminal. Given the herdr server, this tells the tty of a new pane's shell
    before herdr draws the first frame.
    """
    try:
        import ctypes
        import ctypes.util

        libproc = ctypes.CDLL(ctypes.util.find_library("proc"))
        buf = (ctypes.c_int * 1024)()

        def children():
            n = libproc.proc_listchildpids(pid, buf, ctypes.sizeof(buf))
            return set(buf[: max(n, 0)])

        known = children()
        kq = select.kqueue()
        kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_FORK)], 0)
    except Exception:
        return None

    def new_ttys():
        nonlocal known
        kq.control(None, 16, 0)  # drain the notifications
        ttys = {}
        deadline = time.monotonic() + FORK_WAIT
        while True:
            now = children()
            for child in now - known:
                ttys[child] = ttys.get(child) or pid_tty(child, fallback=False)
            # A child gets its controlling terminal shortly after the fork. Some children (hooks, for example) never get one, so wait only briefly.
            # Return as soon as one is known (do not wait for terminal-less children started at the same time)
            if any(ttys.values()) or time.monotonic() >= deadline:
                break
            time.sleep(0.0002)
        known = now
        return [tty for tty in ttys.values() if tty]

    return kq, new_ttys


def pid_tty(pid, fallback=True):
    """Return the path of the process's controlling terminal, or None if unknown. If fallback is false, do not rely on ps (when a quick answer is needed)."""
    try:  # read it through libproc without starting ps, to paint new panes right away
        import ctypes
        import ctypes.util
        import struct

        buf = ctypes.create_string_buffer(136)  # struct proc_bsdinfo
        libproc = ctypes.CDLL(ctypes.util.find_library("proc"))
        if libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), buf, len(buf)) == len(buf):  # 3 = PROC_PIDTBSDINFO
            dev = struct.unpack_from("I", buf.raw, 108)[0]  # e_tdev
            path = f"/dev/ttys{dev & 0xFFFFFF:03d}"
            if dev != 0xFFFFFFFF and os.stat(path).st_rdev == dev:
                return path
    except Exception:
        pass
    if not fallback:
        return None
    tty = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return f"/dev/{tty}" if tty and tty != "??" else None


def pane_tty(pane_id):
    """Return the path of the pane's tty, or None if unknown."""
    pid = herdr("pane.process_info", pane_id=pane_id)["process_info"].get("shell_pid")
    return pid_tty(pid) if pid else None


def write_bg(tty, color, ino=None):
    """Set the background of the pane on tty to color. Return the tty's inode if written, None otherwise.

    With ino, write only if tty is the terminal with that inode. The tty numbers of closed panes are reused by other
    terminals, so always pass it when writing to a remembered tty. The check uses the opened fd, so the terminal
    cannot be swapped between the check and the write.
    """
    seq = "\033]111\a" if color == DEFAULT else f"\033]11;{color}\a"
    # So as not to block on a pane whose output is stopped (Ctrl-S and the like), skip it if it cannot be written and try next time
    fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        if ino is not None and os.fstat(fd).st_ino != ino:
            return None
        os.write(fd, seq.encode())
        st = os.fstat(fd)
        CACHE.setdefault("written", {})[tty] = (color, st.st_mtime, st.st_ino)
        return st.st_ino
    except BlockingIOError:
        return None
    finally:
        os.close(fd)


def paint_pane(pane_id, tokens, color):
    """Set the pane's background to color. tokens are that pane's tokens. Return (tty, inode) if written, None otherwise."""
    tty, ino = tokens.get(TTY_TOKEN), tokens.get(INO_TOKEN, "")
    if tty and ino.isdecimal():
        try:
            if write_bg(tty, color, int(ino)):
                return tty, int(ino)
        except OSError:  # the remembered tty is gone. Look it up again below
            pass
    # A pane that was just added, or one whose remembered tty is now a different terminal. Look it up again from the shell's pid
    tty = pane_tty(pane_id)
    ino = write_bg(tty, color) if tty else None
    return (tty, ino) if ino else None


def heal():
    """Rewrite dimmed panes whose background may have been reset by the program inside. The watcher calls this every POLL seconds.

    When a program resets the background to plain with OSC 111 or the like, that cannot be read from outside. So for
    a pane that had output after we wrote to it, the same color is rewritten once output has stopped for QUIET seconds.
    Nothing is written in the middle of output (so as not to break into the program's escape sequences).
    To avoid flooding a pane that keeps producing output with rewrites, one pane is rewritten at most once per HEAL_EVERY seconds.
    Panes with the plain background (the focused column) are left alone even if a program colors them.

    A tty's modification time becomes the time of the first stat after output. This stats on every call, so if the
    modification time changed, there was output between the previous call and this one.
    """
    now = time.monotonic()
    seen = CACHE.setdefault("seen", {})  # {tty: (last modification time seen, when that modification time was first seen)}
    healed = CACHE.setdefault("healed", {})  # {tty: when it was last rewritten}
    for tty, (color, written_at, ino) in list(CACHE.get("written", {}).items()):
        if color == DEFAULT:
            continue
        try:
            st = os.stat(tty)
            if st.st_ino != ino:  # closed and now a different terminal
                raise OSError
            if seen.get(tty, (None,))[0] != st.st_mtime:  # there was output. Wait for it to stop
                seen[tty] = (st.st_mtime, now)
            elif st.st_mtime != written_at and now - seen[tty][1] >= QUIET and now - healed.get(tty, -HEAL_EVERY) >= HEAL_EVERY:
                if write_bg(tty, color, ino):
                    healed[tty] = now
        except OSError:  # a closed pane
            for record in (CACHE["written"], seen, healed):
                record.pop(tty, None)


def output_recent():
    """Return whether any remembered pane had output after we wrote to it and within the last ACTIVE seconds. Used as a sign that an agent or the like is running."""
    since = time.time() - ACTIVE
    for tty, (_, written_at, ino) in list(CACHE.get("written", {}).items()):
        try:
            st = os.stat(tty)
            if st.st_ino == ino and st.st_mtime > max(since, written_at):
                return True
        except OSError:
            pass
    return False


def hue_of(tokens):
    value = tokens.get(HUE_TOKEN, "")
    return int(value) if value.isdecimal() and int(value) < len(PALETTE["light"]) else None


def state_path(name):
    """Return the path of this plugin's state file name. Separate per herdr server."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdr-spotlight", f"{hashlib.sha1(SOCKET.encode()).hexdigest()[:12]}-{name}")


def load_memo():
    """Return the Memo. The file is read only once per process. If the file is missing or broken, return an empty Memo."""
    if "memo" not in CACHE:
        try:
            with open(state_path("hues.json")) as f:
                memo = json.load(f)
            CACHE["memo"] = {p: h for p, h in memo.items() if isinstance(h, int) and 0 <= h < len(PALETTE["light"])}
        except (OSError, ValueError, AttributeError):
            CACHE["memo"] = {}
    return CACHE["memo"]


def save_memo(memo):
    """Write the Memo to the file. Write under another name and then replace, so readers never see a partial write."""
    path = state_path("hues.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(memo, f)
    os.replace(tmp, path)
    CACHE["memo"] = memo


def plan(layout, tokens, memo, theme, force=False):
    """Layout, Tokens, Memo, Theme -> ({pane_id: Hue}, {pane_id: Color}). Return the column hues of one tab, and the panes whose background must be rewritten."""
    ids = [p["pane_id"] for p in layout["panes"]]
    recorded = {p: h for p in ids if (h := hue_of(tokens.get(p, {}))) is not None}
    known = {**{p: memo[p] for p in ids if p in memo}, **recorded}  # if the tokens are gone, restore from the memo
    applied = {p: tokens[p][BG_TOKEN] for p in ids if BG_TOKEN in tokens.get(p, {})}
    # While zoomed (full screen), the layout still comes back in its pre-zoom shape, so paint it the same way. The zoomed pane is focused, so it gets the plain background
    cols = columns(layout)
    hues = assign_hues(cols, known, len(PALETTE[theme]))
    focused = next((c for c in cols if layout.get("focused_pane_id") in c), [])
    return hues, repaints(hues, applied, theme, focused, force)


def early_paint(layout, theme, skip=()):
    """Write the backgrounds of the visible tab ahead of time, from the Tokens remembered by the last paint_tabs and the memo. Return the set of pane IDs written. Panes in skip are skipped.

    It does not wait for herdr's snapshot, so colors change right after a layout change is noticed.
    The written colors are not recorded. The caller passes the returned IDs to paint_tabs so those panes are always rewritten.
    """
    tokens = CACHE.get("tokens", {})
    _, todo = plan(layout, tokens, load_memo(), theme)
    written = set()
    # write the remembered panes first. Looking up the tty of a just-added pane takes a few ms
    for pane_id, color in sorted(todo.items(), key=lambda item: item[0] not in tokens):
        if pane_id in skip:
            continue
        try:
            if paint_pane(pane_id, tokens.get(pane_id, {}), color):
                written.add(pane_id)
        except Exception:  # a closed pane and the like. Leave it to the paint_tabs that follows
            pass
    return written


def paint_new_pane(tty, theme, layout=None):
    """Paint a just-added pane (whose shell's tty is tty) before herdr draws the first frame. Return the set of pane IDs painted.

    Look in the layout of the visible tab (layout; queried if omitted) for panes not yet remembered. If there is exactly one, it is the pane on tty.
    If it was added to a dimmed column, write the dim color. If it is in the focused column, the plain background is right, so do nothing.
    The written color is not recorded. The caller passes the returned IDs to paint_tabs so those panes are always rewritten.
    """
    if layout is None:
        layout = json.loads(call("pane.layout"))["result"]["layout"]  # the response does not come until herdr has finished adding the pane
    tokens = CACHE.get("tokens")
    if tokens is None:  # nothing has been painted yet. There is no telling which pane is new
        return set()
    fresh = [p["pane_id"] for p in layout["panes"] if p["pane_id"] not in tokens]
    if len(fresh) != 1:
        return set()
    color = plan(layout, tokens, load_memo(), theme)[1].get(fresh[0], DEFAULT)
    return {fresh[0]} if color != DEFAULT and write_bg(tty, color) else set()


def paint_tabs(theme=None, force=False, unrecorded=()):
    """Paint the panes of all tabs. If theme is omitted, it is decided from the terminal background (see current_theme). Return False if some panes could not be painted.

    unrecorded are panes whose background early_paint wrote without recording it. Their recorded color cannot be trusted, so they are always rewritten.
    """
    ok = True
    theme = theme or current_theme()
    snapshot = herdr("session.snapshot")["snapshot"]
    tokens = {p["pane_id"]: dict(p.get("tokens") or {}) for p in snapshot["panes"]}
    for pane_id in unrecorded:
        tokens.get(pane_id, {}).pop(BG_TOKEN, None)
    memo = load_memo()
    all_hues = {}
    updates = {}  # {pane_id: tokens to rewrite}
    for layout in snapshot["layouts"]:
        hues, todo = plan(layout, tokens, memo, theme, force)
        all_hues.update(hues)
        # write all the backgrounds first. Recording comes later, so the visible change is not delayed
        for pane_id, color in todo.items():
            try:
                written = paint_pane(pane_id, tokens.get(pane_id, {}), color)
                if written:
                    updates[pane_id] = {HUE_TOKEN: str(hues[pane_id]), BG_TOKEN: color, TTY_TOKEN: written[0], INO_TOKEN: str(written[1])}
                else:  # the shell has not started yet, and the like
                    ok = False
            except Exception as e:  # a closed pane and the like. One failure does not stop the remaining panes
                print(f"spotlight: {pane_id}: {e}", file=sys.stderr)
                ok = False
        # Panes whose background stays the same but whose column hue changed (moved into the focused column, for example). Fix only the record, for next time
        for pane_id, hue in hues.items():
            if pane_id not in todo and tokens.get(pane_id, {}).get(HUE_TOKEN) != str(hue):
                updates[pane_id] = {HUE_TOKEN: str(hue)}
    for pane_id, update in updates.items():
        try:
            herdr("pane.report_metadata", pane_id=pane_id, source=SOURCE, tokens=update)
            tokens.setdefault(pane_id, {}).update(update)
        except Exception as e:
            print(f"spotlight: {pane_id}: {e}", file=sys.stderr)
            ok = False
    CACHE["tokens"], CACHE["layouts"] = tokens, snapshot["layouts"]
    remember_written(tokens)
    if all_hues != memo:  # closed panes drop out of the memo here
        save_memo(all_hues)
    return ok


def remember_written(tokens):
    """Limit CACHE["written"] to the ttys of existing panes. Panes this process did not write are added from their token records.

    This lets heal and output_recent see the panes painted by the previous watcher, even right after the watcher restarted.
    """
    written = CACHE.get("written", {})
    kept = {}
    for t in tokens.values():
        tty, ino = t.get(TTY_TOKEN), t.get(INO_TOKEN, "")
        if not (tty and ino.isdecimal() and BG_TOKEN in t):
            continue
        if tty in written and written[tty][2] == int(ino):
            kept[tty] = written[tty]
        else:
            try:
                st = os.stat(tty)
                if st.st_ino == int(ino):
                    kept[tty] = (t[BG_TOKEN], st.st_mtime, st.st_ino)
            except OSError:
                pass
    CACHE["written"] = kept


def lock_file(role):
    """Open and return the lock file for role, per herdr server.

    It is not placed in the temporary directory. macOS deletes temporary files unused for a few days, so a held lock could vanish and two watchers could start.
    """
    path = state_path(f"{role}.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "w")


def paint_all(theme=None, force=False, unrecorded=()):
    """Paint the panes of all tabs. Runs against the same herdr server one at a time. Return False if some panes could not be painted.

    One operation fires several events at once. If they run side by side, the one that read an older layout may paint last and the colors disagree, so they take turns and each reads the latest layout.
    """
    if "paint_lock" not in CACHE:  # the watcher calls this many times, so keep it open
        CACHE["paint_lock"] = lock_file("paint")
    fcntl.flock(CACHE["paint_lock"], fcntl.LOCK_EX)
    try:
        return paint_tabs(theme, force, unrecorded)
    finally:
        fcntl.flock(CACHE["paint_lock"], fcntl.LOCK_UN)


def idle_seconds():
    """Return a () -> float function. The returned function returns the seconds since the last keyboard or mouse input. Where unavailable, it always returns 0 (= in use)."""
    try:
        import ctypes
        import ctypes.util

        f = ctypes.CDLL(ctypes.util.find_library("CoreGraphics")).CGEventSourceSecondsSinceLastEventType
        f.restype, f.argtypes = ctypes.c_double, [ctypes.c_int32, ctypes.c_uint32]
        f(0, 0xFFFFFFFF)
        return lambda: f(0, 0xFFFFFFFF)  # 0 = the whole login session, 0xFFFFFFFF = any kind of input
    except Exception:
        return lambda: 0.0


def watch(sock):
    """Watch a subscribed socket and repaint. Return when the server stops. When this file is modified, restart with the new contents."""
    idle, read_theme = idle_seconds(), theme_reader()
    theme = read_theme()
    seen = None  # the layout of the visible tab last seen
    dirty = True  # paint once right after starting
    unrecorded = set()  # panes whose background early_paint wrote without recording it. Carried over until paint_all succeeds
    checked = time.monotonic()  # when the appearance and this file's modification were last checked
    retries, retry_at = 0, 0.0  # remaining retries for panes that could not be painted, and when to try next
    tick = 0
    try:  # the peer of the connection = the herdr server's pid (0 = SOL_LOCAL, 2 = LOCAL_PEERPID)
        forks = fork_watcher(struct.unpack("i", sock.getsockopt(0, 2, 4))[0])
    except OSError:
        forks = None
    waiting = [sock, forks[0]] if forks else [sock]
    scan = command_scanner() if forks else None
    hint = {}  # the layout response received by paint_from_resize ("layout"). Used by the paint_new_pane right after

    def new_pane_ttys():
        """Return the ttys of new pane shells among the children started by the herdr server. If there is one, estimate its color and write it on the spot.

        The color is estimated from the arguments of the `herdr pane split` command that requested the split. herdr draws a
        new pane's first frame immediately, so writing after asking for the layout can be too late. If the estimate is
        wrong, the paint_new_pane and paint_tabs that follow fix it.
        """
        color = None
        try:
            requests = [r for r in (split_request(*command) for command in scan()) if r and r["socket"] == SOCKET]
            if len(requests) == 1:
                color = predict_color(requests[0], CACHE.get("layouts", []), CACHE.get("tokens", {}), theme)
        except Exception:
            pass
        ttys = forks[1]()
        try:
            if len(ttys) == 1 and color is None:
                paint_from_resize(ttys[0])
            elif len(ttys) == 1 and color != DEFAULT:
                write_bg(ttys[0], color)
        except OSError:
            pass
        return ttys

    def paint_from_resize(tty):
        """Paint the added pane (tty) when no command requesting the split is found (requested directly over the socket, by a herdr key binding, and so on).

        Just before finishing a split, herdr shrinks the terminal of the pane being split. This reads the sizes repeatedly
        and writes the moment it can tell which pane was split and how. That is earlier than waiting for the layout response.
        The margin before the first frame is only about 0.3ms, so colors are estimated for every case before waiting.
        Whether focus moves is unknown. Only when the added pane joins an existing column is that column's current color
        written (if wrong, it is in a spot that already had that color, so it does not stand out). When a new column is
        made, nothing is written here; it waits for the layout response.
        """
        tokens, layouts = CACHE.get("tokens", {}), CACHE.get("layouts", [])
        colors = {}  # {(pane being split, direction): escape sequence to write to the added pane}
        columns_new = {}  # cases that make a new column. {(pane being split, direction): (escape sequence, string that appears in the response if focus did not move)}
        for layout in layouts:
            area = layout["area"]
            unmoved = f'"focused_pane_id":"{layout.get("focused_pane_id")}"'.encode()
            for pane in layout["panes"]:
                pane_id, rect = pane["pane_id"], pane["rect"]
                for direction in ("down", "right"):
                    color = predict_color({"target": pane_id, "direction": direction, "focus": False}, [layout], tokens, theme)
                    if color in (None, DEFAULT):
                        continue
                    seq = f"\033]11;{color}\a".encode()
                    if direction == "right" and rect["y"] == area["y"] and rect["height"] == area["height"]:
                        columns_new[pane_id, direction] = (seq, unmoved)
                    else:
                        colors[pane_id, direction] = seq
        sizes, close = resize_watcher({p: t[TTY_TOKEN] for p, t in tokens.items() if t.get(TTY_TOKEN)})
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        # Ask for the layout at the same time. The response comes when herdr finishes the split. When a new column is made, paint from this response
        asked = request("pane.layout")
        try:
            before = sizes()
            deadline = time.monotonic() + RESIZE_WAIT
            while time.monotonic() < deadline:
                found = split_from_resize(before, sizes())
                if found and found in colors:
                    os.write(fd, colors[found])
                    return
                if found or select.select([asked], [], [], 0)[0]:
                    sock, asked = asked, None
                    raw = response(sock)
                    # A new column gets the plain background if focus moved, the dim color if not. Decide and write based only on whether the string is present, before parsing the response
                    if found in columns_new and columns_new[found][1] in raw:
                        os.write(fd, columns_new[found][0])
                    hint["layout"] = json.loads(raw)["result"]["layout"]
                    return
        except BlockingIOError:
            pass
        finally:
            os.close(fd)
            close()
            if asked:
                asked.close()

    while True:
        tick += 1
        if dirty or (retries and time.monotonic() >= retry_at):
            try:
                done = paint_all(theme, unrecorded=unrecorded)
                if done:
                    unrecorded = set()
            except Exception as e:
                print(f"{time.strftime('%F %T')} spotlight: {e!r}", file=sys.stderr, flush=True)
                done = False
            # Panes whose shell has not started yet and the like cannot be painted. No more events will come, so wait a little and retry
            retries = 0 if done else len(RETRY_AFTER) if dirty else retries - 1
            if retries:
                retry_at = time.monotonic() + RETRY_AFTER[-retries]
        dirty = False
        ready = select.select(waiting, [], [], POLL)[0]
        if forks and forks[0] in ready:
            # The herdr server started a child. If it is a new pane's shell, paint before herdr draws the first frame.
            # Ask for the layout only after the tty is known. Asking first can return the layout from before the pane was added
            ttys = new_pane_ttys()
            try:
                if len(ttys) == 1:
                    unrecorded |= paint_new_pane(ttys[0], theme, hint.pop("layout", None))
            except Exception:  # leave it to the paint_all that follows
                pass
            hint.clear()
            dirty = bool(ttys)
        if sock in ready:
            if not sock.recv(1 << 20):  # events that arrived together are handled with a single repaint
                return
            dirty = True
        elif not ready and (idle() < ACTIVE or (tick % OUTPUT_POLL == 0 and output_recent())):
            # A person is operating, or an agent or the like is writing to a pane. Either can change the layout
            ttys = []
            try:
                asked = request("pane.layout")
                # While herdr is adding a pane, the response does not come until it is done. If a fork notification arrives
                # meanwhile, look up the new pane's tty first so it can be painted as soon as the response comes
                if forks and forks[0] in select.select([asked, forks[0]], [], [], 5)[0]:
                    ttys = new_pane_ttys()
                layout = response(asked)
            except OSError:
                return
            if layout != seen:
                dirty, seen = True, layout
                try:
                    parsed = json.loads(layout)["result"]["layout"]
                    # If a pane was added, the fork notification should have arrived. Using its tty, the pane can be painted without a slow lookup
                    ttys = ttys or (new_pane_ttys() if forks else [])
                    fresh = paint_new_pane(ttys[0], theme, parsed) if len(ttys) == 1 else set()
                    unrecorded |= fresh | early_paint(parsed, theme, skip=fresh)
                except Exception:  # no workspace and the like. Leave it to the paint_all that follows
                    pass
        # Check the appearance every time (it only reads the setting directly, so it is cheap). herdr's theme switches first, and this shortens the time a light column stays on a dark screen
        now = read_theme()
        dirty, theme = dirty or now != theme, now
        heal()
        if time.monotonic() - checked < SLOW_POLL:
            continue
        checked = time.monotonic()
        if scan:
            scan()  # keep the record of checked pids fresh, so fewer need checking on a fork
        check_installation()
        try:
            changed = os.stat(__file__).st_mtime != VERSION
        except FileNotFoundError:  # an old installation was cleaned up; check_installation moves on next time
            changed = False
        if changed:
            # A linked plugin was edited in place. Restart with the same arguments. The lock fd is inherited across exec, so it stays the watcher, with the new contents
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def installation(plugins, here):
    """plugin.list's plugins, this file's plugin directory -> ("off", None) | ("moved", root) | ("current", None).

    "off": the plugin was disabled or uninstalled. "moved": `herdr plugin update` or a reinstall put it in root,
    so this process runs an old installation. herdr does not stop or restart detached processes, so the watcher checks.
    """
    mine = next((p for p in plugins if p.get("plugin_id") == PLUGIN_ID), None)
    if not mine or not mine.get("enabled"):
        return "off", None
    root = mine.get("plugin_root", "")
    if os.path.realpath(root) != os.path.realpath(here):
        return "moved", root
    return "current", None


def restore_all():
    """Put the plain background back on every pane this process dimmed. Used when the plugin is turned off."""
    for tty, (color, _, ino) in list(CACHE.get("written", {}).items()):
        if color != DEFAULT:
            try:
                write_bg(tty, DEFAULT, ino)
            except OSError:
                pass


def check_installation():
    """Exit after restoring backgrounds if the plugin was turned off; move to the new installation if it was updated."""
    try:
        state, root = installation(herdr("plugin.list")["plugins"], HERE)
    except (OSError, RuntimeError, KeyError, ValueError):
        return  # keep going; the next check retries
    if state == "off":
        restore_all()
        sys.exit(0)
    if state == "moved":
        # The lock fd is inherited across exec, so it stays the watcher, with the new installation's code
        os.execv(sys.executable, [sys.executable, os.path.join(root, os.path.basename(__file__)), *sys.argv[1:]])


def watch_lock():
    """Take the watcher lock and return it. Return None if a watcher exists, or another hook is starting one."""
    lock = lock_file("watch")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def spawn_watcher(lock):
    """Start the watcher in the background. lock is the lock taken by watch_lock."""
    log = state_path("watch.log")
    if os.path.exists(log) and os.path.getsize(log) > 200_000:  # do not let it grow even if failures continue
        os.remove(log)
    # Pass the taken lock to the child while it stays open. The lock lives as long as the child does, so two watchers never start.
    # It does not just fork, because forking after using CoreFoundation is not safe on macOS
    with open(log, "a") as err:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--watch", str(lock.fileno())],
            pass_fds=[lock.fileno()],
            start_new_session=True,  # detach from the herdr that called the hook
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,  # the watcher's failures go to watch.log in the state directory
        )
    lock.close()


def run_watcher():
    """Run as the watcher. Started by spawn_watcher with `--watch <lock fd>`.

    Even if the herdr server stops, reconnect every second for RECONNECT seconds, so painting resumes right after the server restarts or is replaced.
    """
    deadline = time.monotonic() + RECONNECT
    while time.monotonic() < deadline:
        connected = False
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(SOCKET)
                connected = True
                subs = [{"type": t} for t in WATCHED]
                sock.sendall((json.dumps({"id": SOURCE, "method": "events.subscribe", "params": {"subscriptions": subs}}) + "\n").encode())
                # It may be a different server. The remembered ttys may now belong to other terminals, so drop them
                for name in ("tokens", "written", "seen", "healed"):
                    CACHE.pop(name, None)
                watch(sock)
        except OSError:  # the server is not up yet, or the connection dropped mid-conversation
            pass
        if connected:  # it was connected. It stopped just now, so count again from here
            deadline = time.monotonic() + RECONNECT
        time.sleep(1)


if __name__ == "__main__":
    try:
        if "--watch" in sys.argv[1:]:
            run_watcher()
            sys.exit(0)
        force = "--force" in sys.argv[1:]
        # If a watcher exists, it has already picked up the event and painted. The hook is a fallback for when there is no watcher.
        # Only the hook that takes the lock paints and starts the watcher, so hooks running at the same time never start two
        lock = watch_lock()
        ok = paint_all(force=force) if force or lock else True
        if lock:
            spawn_watcher(lock)
        if not ok:
            sys.exit(1)
    except Exception as e:  # called from events, so failures only go to herdr's plugin log
        print(f"spotlight: {e}", file=sys.stderr)
        sys.exit(1)
