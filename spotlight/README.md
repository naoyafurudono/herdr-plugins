# spotlight

Dim the panes of the herdr columns you are not working in, and give each column its own tint.
The focused column keeps the terminal's normal background; every other column gets a darker, slightly tinted
background, with a different hue per column. It is a replacement for tmux's `window-style` / `window-active-style`.

Colors follow columns, not panes: panes stacked inside a column share its hue, and a column keeps its hue as panes
are added, closed, or moved, as long as it still holds most of the panes of that hue. Hues are kept across herdr
server restarts. There are six hues; with more than six columns, hues repeat but adjacent columns stay different.

## Install

Requires herdr 0.9.5 or later, macOS, and Python 3.9 or later as `python3` on `PATH` (standard library only, no build
step; the macOS system Python works).

```sh
herdr plugin install naoyafurudono/herdr-plugins/spotlight
```

The plugin paints all panes when the herdr server starts and repaints on pane and tab events.
To repaint every pane by hand (for example after something reset the colors):

```sh
herdr plugin action invoke naoyafurudono.spotlight.repaint
```

To turn it off:

```sh
herdr plugin disable naoyafurudono.spotlight
```

Run it from a herdr pane, so the running server sees the change. Within about a second the watcher restores the
plain background of the dimmed panes and exits.

## How it works

herdr has no per-pane color setting, so spotlight writes the background color escape sequence (OSC 11, or OSC 111
to restore the default) directly to each pane's tty.

Which hue and background each pane has is kept in herdr's per-pane metadata tokens (`spotlight_hue`, `spotlight_bg`,
`spotlight_tty`, `spotlight_ino`). Tokens are lost when the herdr server restarts, so the column hues are also saved
to a state file.

The event hooks alone react too late (herdr delivers events in 100ms steps, and some layout changes never reach hooks),
so the first hook starts one background watcher process per herdr server. The watcher:

- subscribes to herdr events, including `layout.updated`;
- polls the visible tab's layout every 10ms for 2 seconds after keyboard or mouse input, and about every 30ms while
  panes are producing output; it does not poll otherwise;
- watches the herdr server for forks, so a new pane is painted before herdr draws its first frame;
- rewrites a dimmed pane's background after its output pauses, in case the program inside reset it;
- follows changes of the light/dark appearance;
- checks every 0.5 seconds whether the plugin is still enabled: after `herdr plugin disable` or `uninstall` it restores
  the plain background of the panes it dimmed and exits, and after `herdr plugin update` it re-executes itself from the
  new installation (herdr itself does not stop or restart detached processes);
- keeps reconnecting for 60 seconds after the herdr server stops, then exits.

State lives in `$XDG_STATE_HOME/herdr-spotlight/` (default `~/.local/state/herdr-spotlight/`), one set of files per
herdr server socket, prefixed with a hash of the socket path:

- `<hash>-hues.json`: the column hue of each pane
- `<hash>-paint.lock`, `<hash>-watch.lock`: locks that serialize painting and keep the watcher to one process
- `<hash>-watch.log`: the watcher's errors (removed when it grows past 200KB)

## Design assumptions

These are part of the specification.

1. Layout convention: one column is one job, and vertical splits inside a column are that job's helpers.
   A column is the area between two full-height left/right splits; left/right splits that are not full height
   stay inside their column. Coloring is per column, and focus is tracked per column: every pane in the focused
   column keeps the normal background.
2. The plain background is the terminal's own background, and spotlight only needs to know whether it is light or
   dark to pick the light or dark palette. It decides this from Ghostty's config `~/.config/ghostty/config`:
   an explicit `background =` wins; otherwise the `theme =` theme's background is read from
   `~/.config/ghostty/themes/` or `/Applications/Ghostty.app/Contents/Resources/ghostty/themes/`, and a
   `theme = light:A,dark:B` pair is resolved with the macOS light/dark appearance.
   If that file does not exist, or no background color can be read from it, the macOS light/dark appearance is used
   directly. With other terminals this is the fallback that applies, unless a Ghostty config is also present,
   in which case that config is still read.
