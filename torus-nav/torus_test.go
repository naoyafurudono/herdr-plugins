package main

import "testing"

func pane(id string, x, y, w, h int) Pane { return Pane{id, Rect{x, y, w, h}} }

// A | B / C  (A is a full-height left column; the right column is split into B on top and C below)
var layout = Layout{
	Area:  Rect{0, 0, 100, 60},
	Panes: []Pane{pane("A", 0, 0, 50, 60), pane("B", 50, 0, 50, 30), pane("C", 50, 30, 50, 30)},
}

func TestWrapTarget(t *testing.T) {
	cases := []struct {
		name   string
		pane   string
		dir    Direction
		want   string
		atEdge bool
	}{
		{"not on the edge", "A", Right, "", false},
		{"right edge wraps to left edge", "B", Right, "A", true},
		{"left edge wraps to the pane at the same height", "A", Left, "C", true}, // A's center y=30 is inside C
		{"bottom edge wraps to top edge", "C", Down, "B", true},
		{"full-height pane wraps to itself", "A", Up, "A", true},
	}
	for _, c := range cases {
		got, atEdge := WrapTarget(layout, c.pane, c.dir)
		if got != c.want || atEdge != c.atEdge {
			t.Errorf("%s: WrapTarget(%s, %s) = (%q, %v), want (%q, %v)", c.name, c.pane, c.dir, got, atEdge, c.want, c.atEdge)
		}
	}
}
