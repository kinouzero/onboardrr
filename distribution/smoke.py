"""Exercise a packaged command from outside the project in a real terminal."""

import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep


def expect_terminal(master, marker):
    deadline = monotonic() + 20
    output = bytearray()
    while monotonic() < deadline:
        if not select.select([master], [], [], min(0.2, max(0, deadline - monotonic())))[0]:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        output.extend(chunk)
        if marker in output:
            return
        del output[:-131072]
    raise RuntimeError(f"Terminal did not show {marker!r}. Tail: {bytes(output[-1200:])!r}")


def check_terminal(executable, directory, environment):
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(directory)
        fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 34, 110, 0, 0))
        os.execve(executable, [str(executable)], {**environment, "TERM": "xterm-256color"})
    status = None
    try:
        expect_terminal(master, b"ONBOARDRR")
        os.write(master, b"\x1bOP")  # F1 opens help for the selected command.
        expect_terminal(master, b"Done")
        os.write(master, b"\x11")  # Ctrl+Q restores the terminal.
        expect_terminal(master, b"\x1b[?1049l")
        deadline = monotonic() + 5
        while monotonic() < deadline:
            finished, result = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = result
                break
            sleep(0.02)
        if status is None:
            raise RuntimeError("The terminal interface did not finish after restoring the terminal.")
    finally:
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(master)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError(f"The terminal interface exited with status {os.waitstatus_to_exitcode(status)}.")


def smoke(executable):
    with TemporaryDirectory(prefix="onboardrr-smoke-") as folder:
        directory = Path(folder)
        environment = {
            **os.environ,
            "XDG_STATE_HOME": str(directory / "state"),
            "PATH": os.pathsep.join((folder, os.environ.get("PATH", os.defpath))),
        }
        environment.pop("PYTHONPATH", None)

        def run(*arguments):
            return subprocess.run(
                [str(executable), *arguments], cwd=directory, env=environment,
                text=True, stdout=subprocess.PIPE, check=True, timeout=30,
            ).stdout

        if not run("--version").startswith("onboardrr "):
            raise RuntimeError("The executable did not report its version.")
        run("add", "lab", "lab.example", "--user", "operator")
        if [host["name"] for host in json.loads(run("list"))] != ["lab"]:
            raise RuntimeError("The executable did not read back its inventory.")
        run("run", "open", "lab")

        # Check the packaged resources without contacting a remote host.
        known_hosts = directory / "known_hosts"
        known_hosts.touch()
        ansible = directory / "ansible-playbook"
        ansible.write_text(
            '#!/bin/sh\n'
            'test -r "$5" && test -r "$(dirname "$5")/roles/linux/tasks/main.yml"'
            ' && grep -q onboardrr_system_only\n'
        )
        ansible.chmod(0o700)
        result = json.loads(run("prepare", "lab", "--system-only", "--known-hosts", str(known_hosts)))
        if result != {"device": "lab", "mode": "check"}:
            raise RuntimeError("The executable did not read its packaged playbook.")
        check_terminal(executable, directory, environment)


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).absolute())
    print("Packaged CLI, playbook, and interactive terminal checks passed.")
