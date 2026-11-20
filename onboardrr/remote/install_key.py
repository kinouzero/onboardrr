"""Add one SSH public key as the authenticated account."""

import fcntl
import os
import secrets
import shlex
import stat
import sys
from pathlib import Path


def contains_key(lines, key):
    for line in lines.splitlines():
        line = line.lstrip()
        if not line or line.startswith(b"#"):
            continue
        lexer = shlex.shlex(line.decode(errors="surrogateescape"), posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            first, second = lexer.get_token(), lexer.get_token()
            if (first, second) == key:
                return True
            if second == key[0] and lexer.get_token() == key[1]:
                return True
        except ValueError:
            continue
    return False


def install(key, home):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory_fd)
        if directory_info.st_uid != os.getuid() or directory_info.st_mode & 0o022:
            raise ValueError("Unsafe SSH directory.")
        with os.fdopen(
            os.open(
                "authorized_keys",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            ),
            "r+b",
        ) as keys:
            fcntl.flock(keys, fcntl.LOCK_EX)
            info = os.fstat(keys.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("Unsafe authorized_keys file.")
            if info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise ValueError("Unsupported authorized_keys file.")
            current = os.stat("authorized_keys", dir_fd=directory_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("The authorized_keys file changed. Retry enrollment.")
            existing = keys.read()
            if contains_key(existing, tuple(key.split())):
                return "reused"
            separator = b"\n" if existing and not existing.endswith(b"\n") else b""
            keys.write(separator + key.encode("ascii") + b"\n")
            keys.flush()
            os.fsync(keys.fileno())
            return "created"
    finally:
        os.close(directory_fd)


def remove(key, home):
    try:
        directory_fd = os.open(home / ".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "absent"
    try:
        directory = os.fstat(directory_fd)
        if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
            raise ValueError("Unsafe SSH directory.")
        try:
            fd = os.open("authorized_keys", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return "absent"
        with os.fdopen(fd, "rb") as keys:
            fcntl.flock(keys, fcntl.LOCK_EX)
            info = os.fstat(keys.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 1024 * 1024):
                raise ValueError("Unsafe authorized_keys file.")
            existing = keys.read()
            plain = key.encode("ascii")
            lines = existing.splitlines(keepends=True)
            matches = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == plain]
            if not matches:
                if contains_key(existing, tuple(key.split())):
                    raise ValueError("The recorded SSH key entry differs from the installed entry.")
                return "absent"
            updated = b"".join(line for index, line in enumerate(lines) if index != matches[0])
            if len(matches) != 1 or contains_key(updated, tuple(key.split())):
                raise ValueError("Several authorized entries match the recorded SSH key.")
            temporary = ".onboardrr-authorized-keys-" + secrets.token_hex(12)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(temporary, flags, 0o600, dir_fd=directory_fd), "wb") as output:
                try:
                    output.write(updated)
                    output.flush()
                    os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
                    os.fsync(output.fileno())
                    current = os.stat("authorized_keys", dir_fd=directory_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino, info.st_ctime_ns) != (
                        current.st_dev, current.st_ino, current.st_ctime_ns
                    ):
                        raise ValueError("The authorized_keys file changed. Retry retirement.")
                    os.replace(temporary, "authorized_keys", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
            return "removed"
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    action = remove if sys.argv[1:] == ["--remove"] else install
    try:
        print(action(sys.stdin.read().strip(), Path.home()))
    except (OSError, ValueError):
        print("error")
        sys.exit(1)
