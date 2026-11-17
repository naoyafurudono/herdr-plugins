package main

// Rect is a pane or tab rectangle in terminal cells, as reported by pane.layout.
type Rect struct {
	X      int `json:"x"`
	Y      int `json:"y"`
	Width  int `json:"width"`
	Height int `json:"height"`
}

// Pane is one pane in a pane.layout result.
type Pane struct {
	PaneID string `json:"pane_id"`
	Rect   Rect   `json:"rect"`
}

// Layout is the pane arrangement of one tab, as reported by pane.layout.
type Layout struct {
	Area   Rect   `json:"area"`
	Panes  []Pane `json:"panes"`
	Zoomed bool   `json:"zoomed"`
}

// Direction is a focus movement direction.
type Direction string

const (
	Left  Direction = "left"
	Right Direction = "right"
	Up    Direction = "up"
	Down  Direction = "down"
)

func (d Direction) Valid() bool {
	return d == Left || d == Right || d == Up || d == Down
}

func (d Direction) opposite() Direction {
	switch d {
	case Left:
		return Right
	case Right:
		return Left
	case Up:
		return Down
	default:
		return Up
	}
}

func (d Direction) horizontal() bool { return d == Left || d == Right }

// side returns the coordinate of r's edge on the d side.
func side(r Rect, d Direction) int {
	switch d {
	case Left:
		return r.X
	case Right:
		return r.X + r.Width
	case Up:
		return r.Y
	default:
		return r.Y + r.Height
	}
}

// across returns r's extent [start, end) perpendicular to d.
func across(r Rect, d Direction) (int, int) {
	if d.horizontal() {
		return r.Y, r.Y + r.Height
	}
	return r.X, r.X + r.Width
}

// distance returns how far point2 (a doubled coordinate) lies outside [start, end); 0 if inside.
func distance(start, end, point2 int) int {
	switch {
	case point2 < 2*start:
		return 2*start - point2
	case point2 >= 2*end:
		return point2 - 2*end + 1
	default:
		return 0
	}
}

// WrapTarget returns the pane to wrap to and true when paneID sits on the d edge of the tab.
// It returns "" and false when paneID is not on that edge.
// The target is the pane on the opposite edge that lines up with the center of paneID.
func WrapTarget(lay Layout, paneID string, d Direction) (string, bool) {
	var cur *Rect
	for i := range lay.Panes {
		if lay.Panes[i].PaneID == paneID {
			cur = &lay.Panes[i].Rect
		}
	}
	if cur == nil || side(*cur, d) != side(lay.Area, d) {
		return "", false
	}
	back := d.opposite()
	s, e := across(*cur, d)
	center2 := s + e // doubled center, so comparisons stay in integers
	best, bestDist := "", -1
	for _, p := range lay.Panes {
		if side(p.Rect, back) != side(lay.Area, back) {
			continue
		}
		ps, pe := across(p.Rect, d)
		if dist := distance(ps, pe, center2); bestDist < 0 || dist < bestDist {
			best, bestDist = p.PaneID, dist
		}
	}
	return best, true
}
