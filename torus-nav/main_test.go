package main

import (
	"encoding/json"
	"reflect"
	"testing"
)

type sent struct {
	method string
	params map[string]string
}

// fakeHerdr answers pane.layout with lay and records every request.
func fakeHerdr(lay Layout, focused string, log *[]sent) caller {
	return func(method string, params map[string]string, out any) error {
		*log = append(*log, sent{method, params})
		if method != "pane.layout" {
			return nil
		}
		b, _ := json.Marshal(map[string]any{"layout": map[string]any{
			"area": lay.Area, "panes": lay.Panes, "zoomed": lay.Zoomed, "focused_pane_id": focused,
		}})
		return json.Unmarshal(b, out)
	}
}

func TestRun(t *testing.T) {
	zoomed := layout
	zoomed.Zoomed = true
	cases := []struct {
		name string
		lay  Layout
		pane string
		dir  Direction
		want []sent
	}{
		{"away from the edge uses herdr's own movement", layout, "A", Right, []sent{
			{"pane.layout", map[string]string{"pane_id": "A"}},
			{"pane.focus_direction", map[string]string{"pane_id": "A", "direction": "right"}},
		}},
		{"on the edge focuses the wrap target", layout, "B", Right, []sent{
			{"pane.layout", map[string]string{"pane_id": "B"}},
			{"pane.focus", map[string]string{"pane_id": "A"}},
		}},
		{"wrapping to itself sends nothing more", layout, "A", Up, []sent{
			{"pane.layout", map[string]string{"pane_id": "A"}},
		}},
		{"zoomed falls back to herdr's own movement", zoomed, "B", Right, []sent{
			{"pane.layout", map[string]string{"pane_id": "B"}},
			{"pane.focus_direction", map[string]string{"pane_id": "B", "direction": "right"}},
		}},
		{"no pane given uses the focused pane", layout, "", Right, []sent{
			{"pane.layout", map[string]string{}},
			{"pane.focus", map[string]string{"pane_id": "A"}},
		}},
	}
	for _, c := range cases {
		var log []sent
		if err := run(fakeHerdr(c.lay, "B", &log), c.pane, c.dir); err != nil {
			t.Fatalf("%s: run returned %v", c.name, err)
		}
		if !reflect.DeepEqual(log, c.want) {
			t.Errorf("%s: sent %v, want %v", c.name, log, c.want)
		}
	}
}
