package main

import "testing"

func pane(id string, x, y, w, h int) Pane { return Pane{id, Rect{x, y, w, h}} }

// A | B / C  (A is a full-height left column; the right column is split into B on top and C below)
var layout = Layout{
	Area:  Rect{0, 0, 100, 60},
	Panes: []Pane{pane("A", 0, 0, 50, 60), pane("B", 50, 0, 50, 30), pane("C", 50, 30, 50, 30)},
}

// D / E | F  (left column split into D and E, right column F; 1-cell gaps between panes,
// as herdr draws them with pane_gaps = true and no borders)
var gapped = Layout{
	Area:  Rect{0, 0, 101, 61},
	Panes: []Pane{pane("D", 0, 0, 49, 30), pane("E", 0, 31, 49, 30), pane("F", 50, 0, 51, 61)},
}

// Three rows in the left column and one full-height right column, to pick among several candidates.
//
//	G | L
//	H | L
//	I | L
var rows = Layout{
	Area:  Rect{0, 0, 100, 90},
	Panes: []Pane{pane("G", 0, 0, 40, 30), pane("H", 0, 30, 40, 30), pane("I", 0, 60, 40, 30), pane("L", 40, 0, 60, 90)},
}

var single = Layout{Area: Rect{0, 0, 80, 24}, Panes: []Pane{pane("S", 0, 0, 80, 24)}}

func TestWrapTarget(t *testing.T) {
	cases := []struct {
		name   string
		lay    Layout
		pane   string
		dir    Direction
		want   string
		atEdge bool
	}{
		{"not on the edge", layout, "A", Right, "", false},
		{"right edge wraps to left edge", layout, "B", Right, "A", true},
		// A spans y 0..60, so its center y=30 is exactly the boundary between B (0..30) and C (30..60).
		// Ranges are half-open, so the pane below the boundary wins.
		{"center on a boundary picks the lower pane", layout, "A", Left, "C", true},
		{"bottom edge wraps to top edge", layout, "C", Down, "B", true},
		{"full-height pane wraps to itself", layout, "A", Up, "A", true},
		{"unknown pane is not on any edge", layout, "Z", Right, "", false},
		{"single pane wraps to itself", single, "S", Left, "S", true},
		// F's center y=30.5 falls in the gap row y=30 between D (0..30) and E (31..61); the nearest pane wins.
		{"center in a gap picks the nearest pane", gapped, "F", Right, "E", true},
		{"wrap up from the top picks the bottom pane", rows, "G", Up, "I", true},
		{"wrap down from the right column lands in the right column", rows, "L", Down, "L", true},
		{"wrap left from the middle row lands in the right column", rows, "H", Left, "L", true},
		{"wrap right from the right column lands in the middle row", rows, "L", Right, "H", true},
	}
	for _, c := range cases {
		got, atEdge := WrapTarget(c.lay, c.pane, c.dir)
		if got != c.want || atEdge != c.atEdge {
			t.Errorf("%s: WrapTarget(%s, %s) = (%q, %v), want (%q, %v)", c.name, c.pane, c.dir, got, atEdge, c.want, c.atEdge)
		}
	}
}
