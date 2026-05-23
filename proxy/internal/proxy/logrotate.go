// Time-rotating log writer used as the destination for log.SetOutput.
//
// Mirrors the Python side's TimedRotatingFileHandler so both halves of
// the bot produce log files with the same "split into 10-minute chunks,
// keep the last N" shape. No external dependency -- the Go stdlib has
// no built-in rotator and pulling in lumberjack for ~40 lines of logic
// is overkill.
//
// On each Write we check whether we've crossed an `Interval` boundary
// from the file's open time; if so, close the current file, rename it
// with a "<base>.YYYY-MM-DD_HH-MM" suffix, and open a fresh `<base>`.
// Old rotations beyond `Backups` are pruned.

package proxy

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// RotatingWriter wraps a file in <Dir>/<Base> with timed rotation.
// Concurrent Write calls are serialized by Mu so log.Logger can use
// it directly as its io.Writer.
type RotatingWriter struct {
	Dir      string
	Base     string        // e.g. "proxy.log"
	Interval time.Duration // rotate every Interval (e.g. 10*time.Minute)
	Backups  int           // keep N rotated files (oldest pruned)
	Tee      io.Writer     // also forward writes here; nil disables.

	mu       sync.Mutex
	file     *os.File
	openedAt time.Time
}

// NewRotatingWriter opens (or creates) <dir>/<base> in append mode and
// returns a writer ready for log.SetOutput. Tee may be os.Stderr to
// keep live console output while persisting to disk.
func NewRotatingWriter(dir, base string, interval time.Duration, backups int, tee io.Writer) (*RotatingWriter, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("mkdir %s: %w", dir, err)
	}
	w := &RotatingWriter{
		Dir:      dir,
		Base:     base,
		Interval: interval,
		Backups:  backups,
		Tee:      tee,
	}
	if err := w.open(); err != nil {
		return nil, err
	}
	return w, nil
}

func (w *RotatingWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if time.Since(w.openedAt) >= w.Interval {
		if err := w.rotateLocked(); err != nil {
			// Rotation failure shouldn't lose the log line; keep
			// writing to the current file and surface on stderr.
			fmt.Fprintf(os.Stderr, "[logrotate] rotate failed: %v\n", err)
		}
	}
	n, err := w.file.Write(p)
	if w.Tee != nil {
		_, _ = w.Tee.Write(p)
	}
	return n, err
}

func (w *RotatingWriter) open() error {
	path := filepath.Join(w.Dir, w.Base)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	w.file = f
	w.openedAt = time.Now()
	return nil
}

func (w *RotatingWriter) rotateLocked() error {
	if w.file != nil {
		_ = w.file.Close()
	}
	stamp := w.openedAt.Format("2006-01-02_15-04")
	src := filepath.Join(w.Dir, w.Base)
	dst := filepath.Join(w.Dir, w.Base+"."+stamp)
	// Rename can fail if dst already exists (rare: two rotations in
	// the same minute). Suffix a counter so we never lose data.
	if _, err := os.Stat(dst); err == nil {
		for i := 1; ; i++ {
			cand := fmt.Sprintf("%s.%d", dst, i)
			if _, err := os.Stat(cand); os.IsNotExist(err) {
				dst = cand
				break
			}
		}
	}
	if err := os.Rename(src, dst); err != nil && !os.IsNotExist(err) {
		return err
	}
	w.pruneLocked()
	return w.open()
}

// pruneLocked deletes the oldest rotations until at most w.Backups remain.
// "Oldest" = lexicographic order on the timestamped suffix, which matches
// chronological order because our suffix is YYYY-MM-DD_HH-MM.
func (w *RotatingWriter) pruneLocked() {
	if w.Backups <= 0 {
		return
	}
	entries, err := os.ReadDir(w.Dir)
	if err != nil {
		return
	}
	prefix := w.Base + "."
	var names []string
	for _, e := range entries {
		n := e.Name()
		if e.IsDir() || !strings.HasPrefix(n, prefix) {
			continue
		}
		names = append(names, n)
	}
	if len(names) <= w.Backups {
		return
	}
	sort.Strings(names)
	for _, n := range names[:len(names)-w.Backups] {
		_ = os.Remove(filepath.Join(w.Dir, n))
	}
}
