package main

import (
	"bufio"
	"fmt"
	"io"
	"strings"
	"sync"
)

// watchForCompletion mirrors IAPTool's output and reports whether it announced a
// finished transfer. The line is IAPTool's own success marker, so this stays
// honest even if the process exits 0 for some other reason.
//
// sending, completed and ended, when non-nil, are closed the first time IAPTool
// reports a data chunk, the first time it announces a finished transfer, and
// when its output stops. They exist so a case can act at a known point in the
// upload instead of guessing with a sleep: an ethernet upload is four phases
// (sign, a preflight connection, a gap, then the data connection), and a fixed
// delay lands in a different one on every run.
//
// ended is separate on purpose. Closing sending or completed when the process
// merely exits would make "IAPTool died before it sent anything" look identical
// to "the transfer finished" -- which is exactly how this file reported a run
// that never got started, on 2026-09-18.
func watchForCompletion(r io.Reader, done chan<- bool, sending, completed, ended chan<- struct{}) {
	found := false
	var onceSending, onceCompleted sync.Once

	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := scanner.Text()
		fmt.Printf("    [IAPTool] %s\n", line)
		if strings.Contains(line, "Sent ") && sending != nil {
			onceSending.Do(func() { close(sending) })
		}
		if strings.Contains(line, "File transfer complete") ||
			strings.Contains(line, "File transfer completed") {
			found = true
			if completed != nil {
				onceCompleted.Do(func() { close(completed) })
			}
		}
	}

	if ended != nil {
		close(ended)
	}
	done <- found
}

// finishedBeforeKnock reports whether IAPTool has already announced a completed
// transfer. Used to tell "there was nothing to knock against" apart from "the
// board mishandled the knock" -- only the second is a verdict on R1-18.
func finishedBeforeKnock(completed <-chan struct{}) bool {
	select {
	case <-completed:
		return true
	default:
		return false
	}
}
