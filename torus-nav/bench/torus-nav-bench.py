#!/usr/bin/env python3
"""Measure the key-press-to-focus latency of torus-nav against herdr's built-in focus keys.

Usage: python3 bench/torus-nav-bench.py [--runs N]   (run from the torus-nav directory, after `go build`)

How it works:
  1. Create a throwaway XDG_CONFIG_HOME, link this plugin into it, and write two configs:
     "builtin" (herdr's own focus_pane_* keys) and "torus-nav" (plugin_action keys).
     Your own herdr config, plugins and sessions are not touched.
  2. For each config, start a throwaway session, attach a TUI client on a pseudo-terminal,
     and split the tab into 3 columns.
  3. Write the prefix (ctrl+space = \\x00) and `l` to the terminal, then poll pane.layout every 1ms
     until focused_pane_id changes. Moves from the right edge (wraps) are reported separately.
  Terminal rendering is not included; it costs the same for built-in and plugin keys.
  Do not use pane.focused event subscriptions for this: herdr batches events, so they arrive ~70ms late.
"""
import argparse
import fcntl
import json
import os
import pathlib
import pty
import shutil
import socket
import statistics
import struct
import subprocess
import tempfile
import termios
import threading
import time

SESSION = "torusnavbench"
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

# Target: p90 at or below 33ms.
# The smallest latency difference people notice (JND) is 96ms for indirect (touchpad) taps and
# 69ms for direct-touch taps (Deber et al., "How Much Faster is Fast Enough?", CHI 2015,
# https://doi.org/10.1145/2702123.2702300). A key press is closest to a tap; stay under half of
# the stricter 69ms.
TARGET_P90_MS = 33

COMMON = """onboarding = false

[keys]
prefix = "ctrl+space"
"""

BUILTIN = COMMON + """focus_pane_left = "prefix+h"
focus_pane_right = "prefix+l"
"""

TORUS_NAV = COMMON + """focus_pane_left = ""
focus_pane_down = ""
focus_pane_up = ""
focus_pane_right = ""
""" + "".join(
    f"""
[[keys.command]]
key = "prefix+{key}"
type = "plugin_action"
command = "naoyafurudono.torus-nav.{direction}"
"""
    for key, direction in [("h", "left"), ("j", "down"), ("k", "up"), ("l", "right")]
)


class Bench:
    def __init__(self, xdg):
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.env.update(XDG_CONFIG_HOME=str(xdg), TERM="xterm-256color")
        self.sock = xdg / "herdr/sessions" / SESSION / "herdr.sock"

    def herdr(self, *args, **extra_env):
        return subprocess.run(["herdr", *args], env={**self.env, **extra_env}, capture_output=True, text=True)

    def call(self, method, params):
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(self.sock))
        s.sendall((json.dumps({"id": "bench", "method": method, "params": params}) + "\n").encode())
        resp = json.loads(s.makefile().readline())
        s.close()
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["result"]

    def focused(self):
        """Return (focused pane ID, whether it is the rightmost pane)."""
        lay = self.call("pane.layout", {})["layout"]
        right = max(lay["panes"], key=lambda p: p["rect"]["x"])["pane_id"]
        return lay["focused_pane_id"], lay["focused_pane_id"] == right

    def stop(self):
        self.herdr("session", "stop", SESSION)
        self.herdr("session", "delete", SESSION)

    def start_client(self, config):
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("herdr", ["herdr", "--session", SESSION], {**self.env, "HERDR_CONFIG_PATH": str(config)})
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))

        def drain():
            try:
                while os.read(fd, 65536):
                    pass
            except OSError:
                pass

        threading.Thread(target=drain, daemon=True).start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if self.call("pane.layout", {})["layout"]["panes"]:
                    return pid, fd
            except (OSError, RuntimeError, KeyError):
                pass
            time.sleep(0.1)
        raise RuntimeError("bench session did not come up")

    def make_columns(self, n):
        for _ in range(n - 1):
            right = max(self.call("pane.layout", {})["layout"]["panes"], key=lambda p: p["rect"]["x"])
            r = self.herdr("pane", "split", right["pane_id"], "--direction", "right", "--no-focus",
                           HERDR_SOCKET_PATH=str(self.sock))
            if r.returncode != 0:
                raise RuntimeError(r.stderr)
        time.sleep(1)  # let the new shells start

    def press(self, fd, key, timeout_ms):
        """Press prefix+key; return ms until focus changes, or None if it does not within timeout_ms."""
        os.write(fd, b"\x00")
        time.sleep(0.03)  # deliver the prefix and the key as separate input
        before = self.focused()[0]
        t0 = time.perf_counter()
        os.write(fd, key)
        while (time.perf_counter() - t0) * 1000 < timeout_ms:
            if self.focused()[0] != before:
                return (time.perf_counter() - t0) * 1000
            time.sleep(0.001)
        return None

    def run(self, config, runs):
        """Press prefix+l runs times; return {"move": [ms], "wrap": [ms], "nowrap": count}.
        When a config does not wrap (built-in keys), press prefix+h to come back and continue."""
        self.stop()
        pid, fd = self.start_client(config)
        result = {"move": [], "wrap": [], "nowrap": 0}
        try:
            self.make_columns(3)
            for _ in range(runs):
                _, at_right = self.focused()
                ms = self.press(fd, b"l", 500)
                if ms is None:
                    result["nowrap"] += 1
                    self.press(fd, b"h", 500)
                else:
                    result["wrap" if at_right else "move"].append(ms)
                time.sleep(0.1)
            return result
        finally:
            self.stop()
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass


def p90(ms):
    ms = sorted(ms)
    return ms[min(len(ms) - 1, int(len(ms) * 0.9))]


def report(name, result):
    print(f"== {name}")
    for kind in ("move", "wrap"):
        ms = result[kind]
        if not ms:
            continue
        verdict = "OK" if p90(ms) <= TARGET_P90_MS else f"over the p90 target of {TARGET_P90_MS}ms"
        print(f"  {kind:5s} n={len(ms):3d}  median {statistics.median(ms):6.1f}ms  p90 {p90(ms):6.1f}ms"
              f"  max {max(ms):6.1f}ms  {verdict}")
    if result["nowrap"]:
        print(f"  did not wrap at the right edge: {result['nowrap']} times")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=60)
    args = ap.parse_args()
    if not (PLUGIN_DIR / "torus-nav").exists():
        raise SystemExit("build first: go build -o torus-nav .")
    # Unix socket paths are limited to 104 bytes, so keep the temporary directory short.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tnb", dir="/tmp"))
    try:
        (tmp / "herdr").mkdir()
        bench = Bench(tmp)
        r = bench.herdr("plugin", "link", str(PLUGIN_DIR))
        if r.returncode != 0:
            raise SystemExit(r.stderr)
        for name, text in [("builtin", BUILTIN), ("torus-nav", TORUS_NAV)]:
            config = tmp / f"{name}.toml"
            config.write_text(text)
            report(name, bench.run(config, args.runs))
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
