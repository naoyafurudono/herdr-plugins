import fcntl
import os
import select
import signal
import termios
import time
import unittest

import spotlight
from spotlight import (
    BG_TOKEN,
    DEFAULT,
    HUE_TOKEN,
    PALETTE,
    assign_hues,
    columns,
    fork_watcher,
    ghostty_background,
    heal,
    plan,
    predict_color,
    repaints,
    split_from_resize,
    split_request,
    theme_of,
    write_bg,
)


def pane(pane_id, x, y, width, height):
    return {"pane_id": pane_id, "rect": {"x": x, "y": y, "width": width, "height": height}}


def split(direction, ratio, x, y, width, height):
    return {"direction": direction, "ratio": ratio, "rect": {"x": x, "y": y, "width": width, "height": height}}


AREA = {"x": 0, "y": 0, "width": 200, "height": 100}


class ColumnsTest(unittest.TestCase):
    def test_single_pane_is_one_column(self):
        layout = {"area": AREA, "panes": [pane("a", 0, 0, 200, 100)], "splits": []}
        self.assertEqual(columns(layout), [["a"]])

    def test_helper_below_belongs_to_the_same_column(self):
        layout = {
            "area": AREA,
            "panes": [pane("b2", 100, 50, 100, 50), pane("b1", 100, 0, 100, 50), pane("a", 0, 0, 100, 100)],
            "splits": [split("right", 0.5, 0, 0, 200, 100), split("down", 0.5, 100, 0, 100, 100)],
        }
        self.assertEqual(columns(layout), [["a"], ["b1", "b2"]])

    def test_left_right_split_inside_a_helper_does_not_make_a_column(self):
        # Splitting the bottom half of the right column left/right is not full height, so there are still 2 columns
        layout = {
            "area": AREA,
            "panes": [pane("a", 0, 0, 100, 100), pane("b1", 100, 0, 100, 50), pane("b2", 100, 50, 50, 50), pane("b3", 150, 50, 50, 50)],
            "splits": [split("right", 0.5, 0, 0, 200, 100), split("down", 0.5, 100, 0, 100, 100), split("right", 0.5, 100, 50, 100, 50)],
        }
        self.assertEqual(columns(layout), [["a"], ["b1", "b2", "b3"]])

    def test_three_columns_with_nested_splits(self):
        layout = {
            "area": AREA,
            "panes": [pane("a", 0, 0, 50, 100), pane("b", 50, 0, 50, 100), pane("c", 100, 0, 100, 100)],
            "splits": [split("right", 0.25, 0, 0, 200, 100), split("right", 0.3333, 50, 0, 150, 100)],
        }
        self.assertEqual(columns(layout), [["a"], ["b"], ["c"]])


    def test_top_bottom_split_at_the_root_is_one_column(self):
        # The top half is the full-width t, the bottom half is l and r side by side. No full-height left/right split, so 1 column
        layout = {
            "area": AREA,
            "panes": [pane("t", 0, 0, 200, 50), pane("l", 0, 50, 100, 50), pane("r", 100, 50, 100, 50)],
            "splits": [split("down", 0.5, 0, 0, 200, 100), split("right", 0.5, 0, 50, 200, 50)],
        }
        self.assertEqual(columns(layout), [["t", "l", "r"]])

    def test_area_with_an_offset_and_an_odd_width(self):
        # Showing the sidebar shifts the area's x. Splitting width 201 in half makes the left 101 (.5 rounds up)
        layout = {
            "area": {"x": 5, "y": 3, "width": 201, "height": 100},
            "panes": [pane("l", 5, 3, 101, 100), pane("r", 106, 3, 100, 100)],
            "splits": [split("right", 0.5, 5, 3, 201, 100)],
        }
        self.assertEqual(columns(layout), [["l"], ["r"]])


class AssignHuesTest(unittest.TestCase):
    def test_new_columns_get_different_hues_from_the_left(self):
        self.assertEqual(assign_hues([["a"], ["b"]], {}, 6), {"a": 0, "b": 1})

    def test_helper_inherits_the_hue_of_its_column(self):
        self.assertEqual(assign_hues([["a"], ["b1", "b2"]], {"a": 0, "b1": 4}, 6), {"a": 0, "b1": 4, "b2": 4})

    def test_new_column_avoids_hues_in_use(self):
        # Adding a column on the right after closing the left one does not clash with the remaining column's hue (1)
        self.assertEqual(assign_hues([["b"], ["c"]], {"b": 1}, 6), {"b": 1, "c": 0})

    def test_pane_moved_on_top_of_another_column_does_not_steal_its_old_hue(self):
        # m moved from the right column (hue 2) to the top of the left column. The left column keeps its hue 1, the right column keeps 2
        known = {"m": 2, "o": 1, "x": 2}
        self.assertEqual(assign_hues([["m", "o"], ["x"]], known, 6), {"m": 1, "o": 1, "x": 2})

    def test_hue_stays_with_the_column_that_has_more_panes_of_it(self):
        # a2 moved out to the left on its own. Hue 0 belongs to the right column, which still has 2 panes of it. a2 gets a new hue not used by its neighbours
        self.assertEqual(assign_hues([["a2"], ["a1", "a3"]], {"a1": 0, "a2": 0, "a3": 0}, 6), {"a2": 1, "a1": 0, "a3": 0})

    def test_column_that_lost_its_hue_takes_its_other_hue(self):
        # Hue 0 belongs to the right column, which has 2 panes of it. The left column gives up a1's 0 and takes x1 and x2's 3
        known = {"a1": 0, "a2": 0, "a3": 0, "x1": 3, "x2": 3}
        self.assertEqual(assign_hues([["a1", "x1", "x2"], ["a2", "a3"]], known, 6), {"a1": 3, "x1": 3, "x2": 3, "a2": 0, "a3": 0})

    def test_two_columns_holding_the_same_two_hues(self):
        # Both columns have one pane each of 0 and 1. The left column gets its top pane's 0, the right column the remaining 1
        known = {"p": 0, "q": 1, "r": 1, "s": 0}
        self.assertEqual(assign_hues([["p", "q"], ["r", "s"]], known, 6), {"p": 0, "q": 0, "r": 1, "s": 1})

    def test_columns_keep_a_shared_hue_when_hues_run_out(self):
        # 7 columns, 6 hues. a and f are both 4 and not adjacent. There are not enough hues, so f keeps 4
        known = {"a": 4, "b": 5, "c": 1, "d": 2, "e": 0, "f": 4, "g": 3}
        self.assertEqual(assign_hues([[c] for c in "abcdefg"], known, 6), known)

    def test_new_column_avoids_the_hues_of_its_neighbours(self):
        # The 7th column (f) after all 6 hues are used. Every hue is used once, but it avoids its neighbours' 0 and 3 and gets 1
        known = {"a": 1, "b": 5, "c": 4, "d": 2, "e": 0, "g": 3}
        self.assertEqual(assign_hues([["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"]], known, 6)["f"], 1)

    def test_pane_moved_out_to_its_own_column_gets_a_new_hue(self):
        # a2 moved out of a1's column on its own. Its old hue (0) is used by a1's column, so it gets another hue
        self.assertEqual(assign_hues([["a1"], ["a2"]], {"a1": 0, "a2": 0}, 6), {"a1": 0, "a2": 1})

    def test_pane_moved_into_a_column_takes_the_hue_of_the_top_pane(self):
        self.assertEqual(assign_hues([["a"], ["b1", "x"]], {"a": 0, "b1": 1, "x": 0}, 6), {"a": 0, "b1": 1, "x": 1})

    def test_column_keeps_its_hue_when_a_pane_from_the_left_column_is_swapped_to_its_top(self):
        # Even when pane x from the left column (0) lands at the top of the right column, the right column keeps c2's hue (2)
        known = {"a1": 0, "c1": 2, "x": 0, "c2": 2}
        self.assertEqual(assign_hues([["a1", "c1"], ["x", "c2"]], known, 6), {"a1": 0, "c1": 0, "x": 2, "c2": 2})

    def test_new_pane_on_top_inherits_the_hue_of_the_pane_below(self):
        self.assertEqual(assign_hues([["new", "b2"]], {"b2": 4}, 6), {"new": 4, "b2": 4})

    def test_hues_are_reused_evenly_when_columns_outnumber_them(self):
        self.assertEqual(assign_hues([["a"], ["b"], ["c"]], {}, 2), {"a": 0, "b": 1, "c": 0})


class RepaintsTest(unittest.TestCase):
    def test_unfocused_pane_is_painted_with_the_dim_color_of_its_hue(self):
        self.assertEqual(repaints({"a": 2}, {}, "light", []), {"a": PALETTE["light"][2]})

    def test_pane_in_the_focused_column_gets_the_default_background(self):
        self.assertEqual(repaints({"a1": 0, "a2": 0, "b": 1}, {}, "light", ["a1", "a2"]), {"a1": DEFAULT, "a2": DEFAULT, "b": PALETTE["light"][1]})

    def test_pane_already_painted_with_the_right_color_is_left_alone(self):
        self.assertEqual(repaints({"a": 0, "b": 1}, {"a": DEFAULT, "b": PALETTE["light"][1]}, "light", ["a"]), {})

    def test_moving_focus_repaints_only_the_two_columns_involved(self):
        applied = {"a": DEFAULT, "b": PALETTE["light"][1], "c": PALETTE["light"][2]}
        self.assertEqual(repaints({"a": 0, "b": 1, "c": 2}, applied, "light", ["b"]), {"a": PALETTE["light"][0], "b": DEFAULT})

    def test_focused_column_is_repainted_first(self):
        # Focus moved from a to b. First restore b, the one now being looked at, to the plain background, then dim a
        applied = {"a": DEFAULT, "b": PALETTE["light"][1]}
        self.assertEqual(list(repaints({"a": 0, "b": 1}, applied, "light", ["b"])), ["b", "a"])

    def test_theme_switch_repaints_with_the_dark_color(self):
        self.assertEqual(repaints({"a": 2}, {"a": PALETTE["light"][2]}, "dark", []), {"a": PALETTE["dark"][2]})

    def test_force_repaints_everything(self):
        self.assertEqual(repaints({"a": 2}, {"a": PALETTE["light"][2]}, "light", [], force=True), {"a": PALETTE["light"][2]})


class PlanTest(unittest.TestCase):
    # Width 200 split into a (left) and b (right). b has the focus
    LAYOUT = {
        "area": AREA,
        "focused_pane_id": "b",
        "panes": [pane("a", 0, 0, 100, 100), pane("b", 100, 0, 100, 100)],
        "splits": [split("right", 0.5, 0, 0, 200, 100)],
    }
    LIGHT = PALETTE["light"]

    def test_hues_come_back_from_the_memo_when_tokens_are_gone(self):
        # Right after a herdr restart. There are no tokens, but the memo has a = 4, b = 2
        self.assertEqual(plan(self.LAYOUT, {}, {"a": 4, "b": 2}, "light"), ({"a": 4, "b": 2}, {"b": DEFAULT, "a": self.LIGHT[4]}))

    def test_token_wins_over_the_memo(self):
        hues, _ = plan(self.LAYOUT, {"a": {HUE_TOKEN: "3"}}, {"a": 4, "b": 2}, "light")
        self.assertEqual(hues, {"a": 3, "b": 2})

    def test_broken_hue_tokens_are_ignored(self):
        # An out-of-range 9 and a non-numeric x. Both count as unassigned and get 0, 1 from the left
        hues, _ = plan(self.LAYOUT, {"a": {HUE_TOKEN: "9"}, "b": {HUE_TOKEN: "x"}}, {}, "light")
        self.assertEqual(hues, {"a": 0, "b": 1})

    def test_panes_already_painted_are_left_alone(self):
        tokens = {"a": {HUE_TOKEN: "0", BG_TOKEN: self.LIGHT[0]}, "b": {HUE_TOKEN: "1", BG_TOKEN: DEFAULT}}
        self.assertEqual(plan(self.LAYOUT, tokens, {}, "light"), ({"a": 0, "b": 1}, {}))

    def test_pane_whose_background_is_unknown_is_repainted(self):
        # a has no recorded color (BG_TOKEN). It is rewritten even if it should already be the right color
        tokens = {"a": {HUE_TOKEN: "0"}, "b": {HUE_TOKEN: "1", BG_TOKEN: DEFAULT}}
        self.assertEqual(plan(self.LAYOUT, tokens, {}, "light")[1], {"a": self.LIGHT[0]})


class TerminalThemeTest(unittest.TestCase):
    """The plain pane background is set by the terminal's color scheme. Read the background color from Ghostty's config and decide whether it is light or dark."""

    THEMES = {"Atom One Light": "background = #f9f9f9\nforeground = #2a2c33\n", "Atom One Dark": "palette = 0=#21252b\nbackground = #21252b\n"}

    def background(self, config, appearance):
        return ghostty_background(config, appearance, self.THEMES.get)

    def test_fixed_light_theme_stays_light_even_when_macos_is_dark(self):
        self.assertEqual(self.background("theme = Atom One Light\n", "dark"), "#f9f9f9")

    def test_theme_pair_follows_the_macos_appearance(self):
        config = "theme = light:Atom One Light,dark:Atom One Dark\n"
        self.assertEqual(self.background(config, "light"), "#f9f9f9")
        self.assertEqual(self.background(config, "dark"), "#21252b")

    def test_explicit_background_wins_over_the_theme(self):
        self.assertEqual(self.background("theme = Atom One Light\nbackground = 000000\n", "light"), "#000000")

    def test_quotes_and_comments_are_ignored(self):
        self.assertEqual(self.background('# theme = Atom One Dark\ntheme = "Atom One Light"\n', "light"), "#f9f9f9")

    def test_unknown_when_the_config_names_no_readable_theme(self):
        self.assertIsNone(self.background("font-size = 14\n", "light"))
        self.assertIsNone(self.background("theme = No Such Theme\n", "light"))

    def test_theme_of_a_background_color(self):
        self.assertEqual(theme_of("#f9f9f9"), "light")
        self.assertEqual(theme_of("#21252b"), "dark")
        self.assertEqual(theme_of("#1a1b26"), "dark")


class TtyTest(unittest.TestCase):
    """On a real pseudo-terminal, check that the background is never written to the wrong terminal, and check the conditions for rewriting."""

    DIM = PALETTE["light"][0]
    OSC = f"\033]11;{DIM}\a".encode()

    def setUp(self):
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        self.tty = os.ttyname(self.slave)
        spotlight.CACHE.clear()
        self.saved = spotlight.QUIET, spotlight.HEAL_EVERY
        spotlight.QUIET, spotlight.HEAL_EVERY = 0.05, 0.0

    def tearDown(self):
        spotlight.QUIET, spotlight.HEAL_EVERY = self.saved
        spotlight.CACHE.clear()
        os.close(self.master)
        os.close(self.slave)

    def shown(self):
        """Return the output that reached the terminal."""
        try:
            return os.read(self.master, 4096)
        except BlockingIOError:
            return b""

    def program_writes(self, data):
        """The program in the pane writes data, we watch once, and then it stays silent for longer than QUIET seconds."""
        os.write(self.slave, data)
        heal()
        self.shown()
        time.sleep(0.08)

    def test_write_bg_returns_the_inode_of_the_tty(self):
        self.assertEqual(write_bg(self.tty, self.DIM), os.stat(self.tty).st_ino)
        self.assertEqual(self.shown(), self.OSC)

    def test_write_bg_refuses_a_tty_that_is_not_the_one_remembered(self):
        # The tty numbers of closed panes are reused by other terminals. Do not write if the inode differs from the remembered one
        self.assertIsNone(write_bg(self.tty, self.DIM, os.stat(self.tty).st_ino + 1))
        self.assertEqual(self.shown(), b"")

    def test_heal_leaves_a_pane_without_output_alone(self):
        write_bg(self.tty, self.DIM)
        self.shown()
        heal()
        time.sleep(0.08)
        heal()
        self.assertEqual(self.shown(), b"")

    def test_heal_rewrites_a_dim_pane_after_its_output_stops(self):
        write_bg(self.tty, self.DIM)
        self.program_writes(b"\033]111\a")  # reset the background to plain
        heal()
        self.assertEqual(self.shown(), self.OSC)

    def test_heal_does_not_write_while_the_program_is_still_writing(self):
        write_bg(self.tty, self.DIM)
        self.shown()
        os.write(self.slave, b"frame 1")
        heal()
        os.write(self.slave, b"frame 2")  # the next output before QUIET seconds pass
        heal()
        self.assertEqual(self.shown(), b"frame 1frame 2")

    def test_heal_rewrites_at_most_once_per_interval(self):
        # Do not flood a pane that keeps producing output with rewrites. After one rewrite, do not write for HEAL_EVERY seconds
        spotlight.HEAL_EVERY = 60.0
        write_bg(self.tty, self.DIM)
        self.program_writes(b"\033]111\a")
        heal()
        self.assertEqual(self.shown(), self.OSC)
        self.program_writes(b"\033]111\a")
        heal()
        self.assertEqual(self.shown(), b"")

    def test_heal_leaves_the_focused_pane_alone(self):
        write_bg(self.tty, DEFAULT)
        self.program_writes(b"hello")
        heal()
        self.assertEqual(self.shown(), b"")


class SplitRequestTest(unittest.TestCase):
    """Read which pane is split and how from the arguments and environment variables of a running `herdr pane split` command."""

    ENV = {"HERDR_PANE_ID": "w1:p2", "HERDR_SOCKET_PATH": "/run/herdr.sock"}

    def test_current_pane_without_focus(self):
        argv = ["herdr", "pane", "split", "--current", "--direction", "down", "--cwd", "/tmp", "--no-focus"]
        self.assertEqual(split_request(argv, self.ENV), {"target": "w1:p2", "direction": "down", "focus": False, "socket": "/run/herdr.sock"})

    def test_explicit_pane_with_focus(self):
        argv = ["herdr", "pane", "split", "w1:p5", "--direction", "right", "--cwd", "/tmp", "--focus"]
        self.assertEqual(split_request(argv, self.ENV), {"target": "w1:p5", "direction": "right", "focus": True, "socket": "/run/herdr.sock"})

    def test_pane_option_and_default_focus(self):
        # With neither --focus nor --no-focus, focus does not move. When the target is omitted, it is the calling pane
        self.assertEqual(split_request(["herdr", "pane", "split", "--pane", "w1:p7", "--direction", "down"], self.ENV)["target"], "w1:p7")
        self.assertEqual(split_request(["herdr", "pane", "split", "--direction", "down"], self.ENV), {"target": "w1:p2", "direction": "down", "focus": False, "socket": "/run/herdr.sock"})

    def test_named_session_uses_its_own_socket(self):
        argv = ["herdr", "--session", "exp", "pane", "split", "w1:p5", "--direction", "down"]
        self.assertEqual(split_request(argv, {})["socket"], os.path.expanduser("~/.config/herdr/sessions/exp/herdr.sock"))

    def test_other_commands_are_not_split_requests(self):
        self.assertIsNone(split_request(["herdr", "pane", "list"], self.ENV))
        self.assertIsNone(split_request(["herdr", "server"], self.ENV))
        self.assertIsNone(split_request(["herdr", "--machine", "m1", "pane", "split", "--direction", "down"], self.ENV))


class SplitFromResizeTest(unittest.TestCase):
    """When a pane is split, the split pane's terminal shrinks. Read which pane was split and how from the change in terminal size (rows, cols)."""

    BEFORE = {"a": (71, 123), "b": (71, 123), "c": (34, 123)}

    def test_pane_that_lost_rows_was_split_downwards(self):
        self.assertEqual(split_from_resize(self.BEFORE, {**self.BEFORE, "b": (35, 123)}), ("b", "down"))

    def test_pane_that_lost_columns_was_split_to_the_right(self):
        self.assertEqual(split_from_resize(self.BEFORE, {**self.BEFORE, "a": (71, 61)}), ("a", "right"))

    def test_nothing_changed_yet(self):
        self.assertIsNone(split_from_resize(self.BEFORE, dict(self.BEFORE)))

    def test_several_panes_changed_is_not_a_split(self):
        # when the whole screen was resized, for example
        self.assertIsNone(split_from_resize(self.BEFORE, {"a": (60, 100), "b": (60, 100), "c": (29, 100)}))

    def test_pane_that_grew_was_not_split(self):
        self.assertIsNone(split_from_resize(self.BEFORE, {**self.BEFORE, "c": (71, 123)}))


class PredictColorTest(unittest.TestCase):
    """Estimate the background color of a pane being added, before it is added."""

    # 3 columns in width 300. a (left, focused), b and its helper b2 (middle), c (right)
    LAYOUT = {
        "area": {"x": 0, "y": 0, "width": 300, "height": 100},
        "focused_pane_id": "a",
        "panes": [pane("a", 0, 0, 100, 100), pane("b", 100, 0, 100, 50), pane("b2", 100, 50, 100, 50), pane("c", 200, 0, 100, 100)],
        "splits": [split("right", 0.3333, 0, 0, 300, 100), split("right", 0.5, 100, 0, 200, 100), split("down", 0.5, 100, 0, 100, 100)],
    }
    TOKENS = {"a": {HUE_TOKEN: "0"}, "b": {HUE_TOKEN: "1"}, "b2": {HUE_TOKEN: "1"}, "c": {HUE_TOKEN: "2"}}
    LIGHT = PALETTE["light"]

    def predict(self, target, direction, focus=False):
        return predict_color({"target": target, "direction": direction, "focus": focus}, [self.LAYOUT], self.TOKENS, "light")

    def test_helper_under_a_dim_column_gets_the_color_of_that_column(self):
        self.assertEqual(self.predict("c", "down"), self.LIGHT[2])
        self.assertEqual(self.predict("b2", "down"), self.LIGHT[1])

    def test_helper_under_the_focused_column_stays_default(self):
        self.assertEqual(self.predict("a", "down"), DEFAULT)

    def test_pane_that_takes_the_focus_stays_default(self):
        self.assertEqual(self.predict("c", "down", focus=True), DEFAULT)

    def test_right_split_of_a_full_height_pane_makes_a_new_column_with_a_new_hue(self):
        # A new column right of c. Hues 0, 1 and 2 are in use and the neighbour c is 2. The new column gets 3
        self.assertEqual(self.predict("c", "right"), self.LIGHT[3])

    def test_right_split_of_a_helper_stays_in_its_column(self):
        self.assertEqual(self.predict("b2", "right"), self.LIGHT[1])

    def test_unknown_target_cannot_be_predicted(self):
        self.assertIsNone(self.predict("zzz", "down"))
        self.assertIsNone(self.predict(None, "down"))


class ForkWatcherTest(unittest.TestCase):
    """Learn from kernel notifications that the herdr server started a new pane's shell. Here this process stands in for the parent."""

    def test_reports_the_tty_of_a_new_child_with_a_terminal(self):
        kq, new_ttys = fork_watcher(os.getpid())
        master, slave = os.openpty()
        pid = os.fork()  # a child with a new pseudo-terminal as its controlling terminal (= a new pane's shell)
        if pid == 0:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            time.sleep(5)
            os._exit(0)
        try:
            self.assertTrue(select.select([kq], [], [], 2)[0])
            self.assertEqual(new_ttys(), [os.ttyname(slave)])
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(master)
            os.close(slave)
            kq.close()

    def test_ignores_a_child_without_a_terminal(self):
        kq, new_ttys = fork_watcher(os.getpid())
        pid = os.fork()  # a child without a terminal (= a hook process, for example)
        if pid == 0:
            os.setsid()
            time.sleep(5)
            os._exit(0)
        try:
            self.assertTrue(select.select([kq], [], [], 2)[0])
            self.assertEqual(new_ttys(), [])
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            kq.close()


if __name__ == "__main__":
    unittest.main()
