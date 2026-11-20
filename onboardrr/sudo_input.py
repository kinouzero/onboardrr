"""Pass one operation's sudo password through an inherited, private pipe."""

import getpass
import os
import stat
import sys
from contextlib import contextmanager

from .progress import request_input


MAX_PASSWORD_BYTES = 512


class SudoInput:
    def __init__(self, supplied=None):
        self.supplied = supplied
        self.password = supplied

    def __call__(self):
        if self.password is None:
            self.password = request_input("sudo-password")
            if self.password is None:
                if not sys.stdin.isatty():
                    raise ValueError("Use a terminal to enter the sudo password securely.")
                self.password = getpass.getpass("Target sudo password: ")
            encode_password(self.password)
        return self.password


def read_password(descriptor):
    if descriptor < 3:
        raise ValueError("The sudo password requires a private pipe descriptor.")
    with os.fdopen(descriptor, "rb") as pipe:
        if not stat.S_ISFIFO(os.fstat(pipe.fileno()).st_mode):
            raise ValueError("The sudo password requires a private pipe descriptor.")
        data = pipe.read(MAX_PASSWORD_BYTES + 1)
    try:
        password = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("The sudo password has an invalid encoding.") from None
    encode_password(password)
    return password


def encode_password(password):
    payload = password.encode("utf-8")
    if not payload or len(payload) > MAX_PASSWORD_BYTES or any(char in password for char in "\r\n\0"):
        raise ValueError("Enter a single-line sudo password of at most 512 bytes.")
    return payload


@contextmanager
def password_pipe(password):
    if password is None:
        yield None
        return
    payload = encode_password(password)
    reader, writer = os.pipe()
    try:
        try:
            os.write(writer, payload)
        finally:
            os.close(writer)
        yield reader
    finally:
        os.close(reader)
