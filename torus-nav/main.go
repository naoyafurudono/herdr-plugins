// torus-nav moves herdr pane focus in a direction and wraps around at the edges of the tab,
// treating the pane layout as a torus.
//
// Usage: torus-nav left|right|up|down
//
// It is meant to run as an action of the naoyafurudono.torus-nav herdr plugin, bound to keys with
// type = "plugin_action". Away from the edges it behaves like herdr's built-in focus movement.
//
// Latency rules (measure with bench/torus-nav-bench.py):
//   - Run as a plugin action, not a type = "shell" key. Shell keys always go through /bin/sh -lc,
//     which costs tens of milliseconds per key press.
//   - Talk to the herdr socket directly instead of the herdr CLI. One move makes two socket calls.
//   - Write the request right after connecting. When the herdr server accepts a connection whose
//     request has not arrived yet, it sleeps for 100ms (CONNECTION_POLL_INTERVAL in
//     src/api/server.rs). So the request is encoded before connecting.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

func socketPath() string {
	if p := os.Getenv("HERDR_SOCKET_PATH"); p != "" {
		return p
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config/herdr/herdr.sock")
}

// call sends one request to the herdr socket and decodes its result into out.
// herdr serves one request per connection.
func call(method string, params map[string]string, out any) error {
	req, err := json.Marshal(map[string]any{"id": "torus-nav", "method": method, "params": params})
	if err != nil {
		return err
	}
	req = append(req, '\n')
	fd, err := syscall.Socket(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		return err
	}
	defer syscall.Close(fd)
	if err := syscall.Connect(fd, &syscall.SockaddrUnix{Name: socketPath()}); err != nil {
		return err
	}
	if _, err := syscall.Write(fd, req); err != nil {
		return err
	}
	line, err := readLine(fd)
	if err != nil {
		return err
	}
	var resp struct {
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(line, &resp); err != nil {
		return err
	}
	if resp.Error != nil {
		return fmt.Errorf("%s: %s", method, resp.Error)
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(resp.Result, out)
}

// readLine reads from fd up to the first newline.
func readLine(fd int) ([]byte, error) {
	var buf []byte
	chunk := make([]byte, 64*1024)
	for {
		n, err := syscall.Read(fd, chunk)
		if err == syscall.EINTR {
			continue
		}
		if err != nil {
			return nil, err
		}
		if n == 0 {
			return nil, fmt.Errorf("connection closed before response")
		}
		buf = append(buf, chunk[:n]...)
		if i := bytes.IndexByte(buf, '\n'); i >= 0 {
			return buf[:i], nil
		}
	}
}

// activePane returns the focused pane ID passed by herdr: HERDR_PANE_ID for plugin actions,
// HERDR_ACTIVE_PANE_ID for type = "shell" keys.
func activePane() string {
	if p := os.Getenv("HERDR_PANE_ID"); p != "" {
		return p
	}
	return os.Getenv("HERDR_ACTIVE_PANE_ID")
}

func run(d Direction) error {
	pane := activePane()
	params := map[string]string{}
	if pane != "" {
		params["pane_id"] = pane
	}
	var res struct {
		Layout struct {
			Layout
			FocusedPaneID string `json:"focused_pane_id"`
		} `json:"layout"`
	}
	if err := call("pane.layout", params, &res); err != nil {
		return err
	}
	if pane == "" {
		pane = res.Layout.FocusedPaneID
	}
	lay := res.Layout.Layout
	if !lay.Zoomed {
		if target, atEdge := WrapTarget(lay, pane, d); atEdge {
			if target == pane {
				return nil
			}
			return call("pane.focus", map[string]string{"pane_id": target}, nil)
		}
	}
	return call("pane.focus_direction", map[string]string{"pane_id": pane, "direction": string(d)}, nil)
}

func main() {
	if len(os.Args) != 2 || !Direction(os.Args[1]).Valid() {
		fmt.Fprintln(os.Stderr, "usage: torus-nav left|right|up|down")
		os.Exit(2)
	}
	if err := run(Direction(os.Args[1])); err != nil {
		fmt.Fprintln(os.Stderr, "torus-nav:", err)
		os.Exit(1)
	}
}
