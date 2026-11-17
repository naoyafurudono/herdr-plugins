# torus-nav

Move herdr pane focus with wraparound. Moving right from the rightmost pane focuses the leftmost pane,
moving down from the bottom pane focuses the top pane, and so on: the pane layout of a tab becomes a torus.
Away from the edges it behaves exactly like herdr's built-in `focus_pane_*` keys.

When wrapping, it picks the pane on the opposite edge that lines up with the center of the current pane
(same height for left/right, same column position for up/down).

## Install

Requires herdr 0.9.5 or later and Go (the plugin is built during install).

```sh
herdr plugin install naoyafurudono/herdr-plugins/torus-nav
```

Then replace the built-in focus keys in `~/.config/herdr/config.toml`:

```toml
[keys]
focus_pane_left = ""
focus_pane_down = ""
focus_pane_up = ""
focus_pane_right = ""

[[keys.command]]
key = "prefix+h"
type = "plugin_action"
command = "naoyafurudono.torus-nav.left"

[[keys.command]]
key = "prefix+j"
type = "plugin_action"
command = "naoyafurudono.torus-nav.down"

[[keys.command]]
key = "prefix+k"
type = "plugin_action"
command = "naoyafurudono.torus-nav.up"

[[keys.command]]
key = "prefix+l"
type = "plugin_action"
command = "naoyafurudono.torus-nav.right"
```

and reload with `herdr server reload-config`.

## Latency

A focus key should feel instant. `bench/torus-nav-bench.py` measures the time from the key press until
herdr's focus changes, for the built-in keys and for torus-nav, in a throwaway session that does not
touch your config or plugins:

```sh
cd torus-nav
go build -o torus-nav .
python3 bench/torus-nav-bench.py
```

The target is a p90 of 33ms or less: under half of the 69ms just-noticeable latency difference for
direct-touch taps, and about a third of the 96ms one for indirect taps
([Deber et al., CHI 2015](https://doi.org/10.1145/2702123.2702300)).
On the author's Mac it measures about 20 to 30ms (median), versus about 150ms for a naive
`type = "shell"` script. The design choices behind that:

- It runs as a plugin action. `type = "shell"` keys always go through `/bin/sh -lc`, which costs tens of
  milliseconds per press; plugin actions exec the binary directly.
- It is a small Go binary that talks to the herdr socket directly instead of invoking the `herdr` CLI.
  One move makes two socket calls (`pane.layout`, then `pane.focus` or `pane.focus_direction`).
- It writes each request immediately after connecting. herdr 0.9.5 sleeps for 100ms when it accepts a
  connection whose request has not arrived yet (`CONNECTION_POLL_INTERVAL` in `src/api/server.rs`).

Most of the remaining time is the cost of starting one process.

## Notes

- While a pane is zoomed, the keys fall back to herdr's built-in movement.
- `pane.focus` (focus by pane ID) is used from the socket API; the herdr CLI does not expose it.
