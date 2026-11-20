"""Private event channel between the controller CLI and its terminal interface."""

import json
import os
import stat
import sys
from contextlib import contextmanager
from contextvars import ContextVar


EVENT_FD = "ONBOARDRR_TUI_EVENT_FD"
event_fd = ContextVar("onboardrr_event_fd", default=None)


@contextmanager
def terminal_events():
    value = os.environ.pop(EVENT_FD, None)
    if value is None:
        yield
        return
    descriptor = int(value)
    if descriptor < 3 or not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        raise ValueError("The terminal event channel requires a private pipe.")
    os.set_inheritable(descriptor, False)
    token = event_fd.set(descriptor)
    try:
        yield
    finally:
        event_fd.reset(token)
        os.close(descriptor)


def emit(event, **fields):
    descriptor = event_fd.get()
    if descriptor is not None:
        os.write(descriptor, (json.dumps({"event": event, **fields}) + "\n").encode())


def request_input(field):
    if event_fd.get() is None:
        return None
    emit("input", field=field)
    response = sys.stdin.readline(4098)
    if not response.endswith("\n") or not response.strip("\r\n"):
        raise EOFError("Credential entry was canceled.")
    return response.rstrip("\r\n")
