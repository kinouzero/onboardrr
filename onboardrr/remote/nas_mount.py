"""Mount the primary NAS shares and restore them through QTS cron."""

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


ROOT = Path("/etc/config/onboardrr")
CRONTAB = Path("/etc/config/crontab")
QTS_CRONTAB = "/usr/bin/crontab"
RUNNER = ROOT / "restore-mounts.py"
CONFIG = ROOT / "mounts.json"
RESTORE_STATE = ROOT / "restore-status"
SCHEDULE = f"* * * * * /bin/sh -c 'PATH=/opt/bin:/usr/bin:/usr/sbin:/bin:/sbin python3 {RUNNER} --restore' >/dev/null 2>&1"


def mount_table():
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields = before.split()
        if len(fields) > 4 and after:
            kind, source, *_ = after.split()
            mounts[Path(unescape(fields[4]))] = (kind, unescape(source))
    return mounts


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def run(args):
    try:
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise ValueError(f"QTS timed out running {Path(args[0]).name}.") from None
    except subprocess.CalledProcessError as error:
        raise ValueError(f"QTS {Path(args[0]).name} failed (exit {error.returncode}).") from None
    except OSError:
        raise ValueError(f"QTS could not start {Path(args[0]).name}.") from None


def target_directory(path, root, *, owner=None):
    if not root.is_dir():
        raise ValueError("The NAS mount point's parent share is unavailable.")
    parents = path.relative_to(root).parts
    for part in parents:
        root /= part
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("NAS mount points must be real directories.")
        if not root.exists():
            root.mkdir(mode=0o770)
            if owner is not None:
                os.chown(root, *owner)


def mount_shares(config, *, docker=Path("/share/docker")):
    home = Path(config["home"])
    name = config["device"]
    uid, gid = config["uid"], config["gid"]
    if (not home.is_absolute() or home == Path("/") or ".." in home.parts
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name)
            or any(type(number) is not int or number < 0 for number in (uid, gid))):
        raise ValueError("Invalid NAS mount configuration.")
    cifs, nfs = config["cifs"], config["nfs"]
    if (not re.fullmatch(r"//[a-zA-Z0-9.-]+/[a-zA-Z0-9_.-]+", cifs)
            or not re.fullmatch(r"[a-zA-Z0-9.-]+:/[a-zA-Z0-9_./-]+", nfs)
            or ".." in nfs.split("/")):
        raise ValueError("Invalid SMB or NFS source for QTS.")
    user, password = config["username"], config["password"]
    if not all(isinstance(value, str) and value and not any(char in value for char in "\r\n\0")
               for value in (user, password)):
        raise ValueError("Invalid SMB credentials.")
    compose = home / ".docker/compose"
    data = docker / "komodo" / "data" / name
    target_directory(compose, home, owner=(uid, gid))
    target_directory(data, docker)
    entries = [(compose, cifs, {"cifs", "smb3"}), (data, nfs, {"nfs", "nfs4"})]
    pending = []
    current = mount_table()
    for target, source, types in entries:
        existing = current.get(target.resolve())
        if existing and existing[0] in types and existing[1] == source:
            continue
        if existing or any(target.iterdir()):
            raise ValueError(f"QTS will not hide existing content or replace a different mount at {target}.")
        pending.append((target, source, types))
    mounted = []
    try:
        for target, source, types in pending:
            if "cifs" in types:
                with NamedTemporaryFile(mode="w", dir=ROOT) as credentials:
                    credentials.write(f"username={user}\npassword={password}\n")
                    credentials.flush()
                    options = f"credentials={credentials.name},vers=3.0,uid={uid},gid={gid},file_mode=0660,dir_mode=0770"
                    run(["mount.cifs", source, str(target), "-o", options])
            else:
                run(["mount", "-t", "nfs", "-o", "nfsvers=4,rw", source, str(target)])
            mounted.append(target)
            existing = mount_table().get(target.resolve())
            if not existing or existing[0] not in types or existing[1] != source:
                raise ValueError(f"QTS did not confirm the expected mount at {target}.")
    except (OSError, ValueError):
        for target in reversed(mounted):
            try:
                run(["umount", str(target)])
            except ValueError:
                pass
        raise


def regular_stat(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise ValueError(f"QTS file {path.name} must be a regular file owned by the administrator.")
    return info


def private_text(path):
    if stat.S_IMODE(regular_stat(path).st_mode) != 0o600:
        raise ValueError(f"QTS file {path.name} must have permissions 0600.")
    return path.read_text()


def atomic_write(path, contents, mode, *, expected=None):
    try:
        current = regular_stat(path)
    except FileNotFoundError:
        current = None
    if current is not None and stat.S_IMODE(current.st_mode) == mode and path.read_text() == contents:
        return
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
            if expected is not None and path.read_text() != expected:
                raise ValueError("The QTS crontab changed concurrently. Retry configuring the mounts.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def recovery_is_scheduled():
    active = subprocess.run([QTS_CRONTAB, "-l"], capture_output=True, text=True, timeout=10)
    return active.returncode == 0 and active.stdout.splitlines().count(SCHEDULE) == 1


def configure(config, runner):
    if not CRONTAB.is_file() or not shutil.which(QTS_CRONTAB):
        raise ValueError("QTS's persistent crontab is unavailable.")
    metadata = regular_stat(CRONTAB)
    original = CRONTAB.read_text()
    matches = [line for line in original.splitlines() if str(RUNNER) in line]
    if matches and matches != [SCHEDULE]:
        raise ValueError("Review the existing Onboardrr mount entry in the QTS crontab.")
    mount_shares(config)
    atomic_write(RUNNER, runner, 0o600)
    atomic_write(CONFIG, json.dumps(config) + "\n", 0o600)
    if not matches:
        atomic_write(CRONTAB, original.rstrip("\n") + "\n" + SCHEDULE + "\n",
                     stat.S_IMODE(metadata.st_mode), expected=original)
    if not recovery_is_scheduled():
        run([QTS_CRONTAB, str(CRONTAB)])
        run(["/etc/init.d/crond.sh", "restart"])
        if not recovery_is_scheduled():
            raise ValueError("QTS did not load the Onboardrr mount recovery job after restarting cron.")


def log_restore(message):
    try:
        previous = private_text(RESTORE_STATE).strip()
    except FileNotFoundError:
        previous = None
    if previous == message:
        return
    logger = shutil.which("logger")
    if logger:
        try:
            subprocess.run([logger, "-t", "onboardrr-mounts", message], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return
    atomic_write(RESTORE_STATE, message + "\n", 0o600)


def restore():
    try:
        mount_shares(json.loads(private_text(CONFIG)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        log_restore(str(error) if type(error) is ValueError else "QTS could not restore the primary NAS mounts.")
        raise
    log_restore("Primary NAS mounts verified.")


if __name__ == "__main__":
    try:
        if os.geteuid() != 0:
            raise ValueError("QTS mount configuration requires sudo.")
        parent = ROOT.parent.stat()
        if parent.st_uid != 0 or parent.st_mode & 0o022:
            raise ValueError("The QTS configuration directory must be protected.")
        ROOT.mkdir(mode=0o700, exist_ok=True)
        if ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
            raise ValueError("The QTS Onboardrr configuration directory must be private and owned by root.")
        with (ROOT / "mounts.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if sys.argv[1:] == ["--restore"]:
                restore()
            else:
                values = json.load(sys.stdin)
                configure(values["config"], values["runner"])
            print("ready=yes")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("error=" + (str(error) if type(error) is ValueError else "NAS storage configuration failed."))
