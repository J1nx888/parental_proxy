//go:build linux

package ipc

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// peerAllowed checks the connecting peer's UID via SO_PEERCRED. This
// is what makes the controller<->worker socket trust the actual
// process identity, not just "whoever can open this path" -- see
// docs/design/phase3-technical-design.md section 4.
//
// NOT VERIFIED against a real build (no Go toolchain in this dev
// environment -- see the design doc's header note). unix.GetsockoptUcred
// is written from memory of golang.org/x/sys/unix's documented shape;
// confirm the exact signature once `go get golang.org/x/sys` has run.
func peerAllowed(conn *net.UnixConn, allowedUID uint32) (ok bool, peerUID uint32, err error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return false, 0, fmt.Errorf("syscall conn: %w", err)
	}

	var ucred *unix.Ucred
	var sockErr error
	ctrlErr := raw.Control(func(fd uintptr) {
		ucred, sockErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	})
	if ctrlErr != nil {
		return false, 0, ctrlErr
	}
	if sockErr != nil {
		return false, 0, sockErr
	}

	return ucred.Uid == allowedUID, ucred.Uid, nil
}

func unixRemoveStale(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
