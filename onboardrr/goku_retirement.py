"""Purge recorded Goku directories after verifying that the device is detached."""

import errno
import json
import os
import re
import shutil
import stat
import sys
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .inventory import HOST_LABEL
from .periphery import COMPOSE, COMPOSE_ROOT, TEMPLATE
from .retirement import ensure_stacks_detached, retirement_plan, set_initial_status
from .runs import ResourceKind
from .storage import device_lock


DATA_ROOT = "/share/docker/komodo/data"


def check_host_mounts(device, known_hosts, public_key, mounts, *, include_compose=False):
    from . import ssh

    key = ssh.load_public_key(public_key)
    with ssh.agent_identity(key) as agent, ssh.connect(device, known_hosts, key=agent) as client:
        request = json.dumps({"mounts": mounts, "mount_check_only": True,
                              "include_compose": include_compose}).encode()
        response = json.loads(ssh.script(client, "verify.py", request, timeout=60))
    if include_compose:
        compose = PurePosixPath(response.get("compose_mount", ""))
        if not compose.is_absolute() or compose.parts[-2:] != (".docker", "compose") or ".." in compose.parts:
            return False
        mounts = [*mounts, str(compose)]
    return response.get("detached") == dict.fromkeys(mounts, True)


def mount_points():
    if sys.platform.startswith("linux"):
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        return {
            Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
            for line in mounts
        }
    if sys.platform == "darwin":
        return darwin_mount_points()
    raise ValueError("This controller cannot verify filesystem mount boundaries.")


def darwin_mount_points():
    import ctypes

    # Match the 64-bit struct statfs layout in Darwin's sys/mount.h.
    class StatFS(ctypes.Structure):
        _fields_ = [
            ("block_size", ctypes.c_uint32), ("io_size", ctypes.c_int32),
            ("blocks", ctypes.c_uint64 * 5), ("filesystem_id", ctypes.c_int32 * 2),
            ("flags_and_owner", ctypes.c_uint32 * 4), ("filesystem_type", ctypes.c_char * 16),
            ("mount_path", ctypes.c_char * 1024), ("source", ctypes.c_char * 1024),
            ("reserved", ctypes.c_uint32 * 8),
        ]

    library = ctypes.CDLL(None, use_errno=True)
    getfsstat = getattr(library, "getfsstat$INODE64", None) or library.getfsstat
    getfsstat.argtypes = [ctypes.POINTER(StatFS), ctypes.c_int, ctypes.c_int]
    getfsstat.restype = ctypes.c_int
    nowait = 2  # MNT_NOWAIT uses cached statistics even for disconnected shares.
    for _ in range(3):
        count = getfsstat(None, 0, nowait)
        if count <= 0:
            break
        mounts = (StatFS * (count + 8))()
        observed = getfsstat(mounts, ctypes.sizeof(mounts), nowait)
        if 0 < observed < len(mounts):
            paths = {Path(os.fsdecode(item.mount_path)) for item in mounts[:observed]}
            if Path("/") in paths and all(path.is_absolute() for path in paths):
                return paths
            break
    raise ValueError("Could not verify the macOS mount table.")


def purge_directory(root, name):
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("Use the recorded device directory name.")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("This controller does not support symlink-safe directory removal.")
    root = root.resolve(strict=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        target = root / name
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != os.fstat(descriptor).st_dev:
            raise ValueError("The purge target is not a directory on the selected Goku filesystem.")
        if any(point == target or point.is_relative_to(target) for point in mount_points()):
            raise ValueError("The purge target contains an active filesystem mount.")
        shutil.rmtree(name, dir_fd=descriptor)
        return "removed"
    finally:
        os.close(descriptor)


def recorded_file(directory_fd, filename, expected):
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return "absent", None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return "changed", None
        raise
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_dev != os.fstat(directory_fd).st_dev or not 0 < metadata.st_size <= 1048576):
            return "changed", None
        if sha256(stream.read(1048577)).hexdigest() != expected:
            return "changed", None
    return "eligible", (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)


def retire_goku_files(database, device, komodo, *, compose_root, known_hosts, public_key, apply):
    name = device["name"]
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("Use the recorded device directory name.")
    with device_lock(database, device):
        plan = retirement_plan(database, name)
        allowed = {f"{COMPOSE_ROOT}/{name}/{filename}" for filename in (COMPOSE, TEMPLATE)}
        files = [item for item in plan["actions"] if item["kind"] == ResourceKind.GOKU_FILE
                 and item["reference"] in allowed]
        if not files:
            raise ValueError("The run history has no Periphery files under the device's Goku directory.")
        for item in files:
            item["status"] = "preserved" if item["action"] != "remove" else "unverified"
        candidates = [item for item in files if item["action"] == "remove" and item.get("content_sha256")]
        report = {"device": name, "mode": "apply" if apply else "preview", "files": files}
        if not candidates:
            return report
        root = compose_root.resolve(strict=True)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError:
                for item in candidates:
                    item["status"] = "absent"
                return report
            try:
                directory = root / name
                if os.fstat(directory_fd).st_dev != os.fstat(root_fd).st_dev or any(
                    point == directory or point.is_relative_to(directory) for point in mount_points()
                ):
                    raise ValueError("The device's Goku staging directory crosses a filesystem mount.")
                eligible = []
                for item in candidates:
                    filename = PurePosixPath(item["reference"]).name
                    item["status"], identity = recorded_file(directory_fd, filename, item["content_sha256"])
                    if item["status"] == "eligible":
                        eligible.append((item, filename, identity))
                if not apply or not eligible or any(item["status"] in {"changed", "unverified"} for item in files):
                    return report
                ensure_stacks_detached(komodo, name, plan["actions"])
                mounts = [item["reference"] for item in plan["actions"] if item["kind"] == ResourceKind.MOUNT]
                if not public_key or not check_host_mounts(
                    device, known_hosts, public_key, [*mounts, f"{DATA_ROOT}/{name}"], include_compose=True,
                ):
                    raise ValueError("Remove the device's Goku mounts and fstab entries before retiring its files.")
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                opened = os.fstat(directory_fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("The Goku staging directory changed during retirement.")
                for item, filename, identity in eligible:
                    status, observed = recorded_file(directory_fd, filename, item["content_sha256"])
                    if status != "eligible" or observed != identity:
                        raise ValueError("A Goku deployment file changed during retirement.")
                    os.unlink(filename, dir_fd=directory_fd)
                    item["status"] = "removed"
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)
    return report


def purge_goku(database, device, komodo, pangolin, *, roots, known_hosts, public_key):
    if roots.keys() - {COMPOSE_ROOT, DATA_ROOT}:
        raise ValueError("Use a supported Goku share root.")
    name = device["name"]
    with device_lock(database, device):
        result = retirement_plan(database, name, purge=True)
        actions = result["actions"]
        set_initial_status(actions)
        expected = {str(PurePosixPath(parent) / name): local for parent, local in roots.items()}
        candidates = [item for item in actions if item["kind"] == ResourceKind.GOKU_DIRECTORY
                      and item["action"] == "remove" and item["reference"] in expected]
        if not candidates:
            raise ValueError("The selected Goku roots contain no recorded device-owned directory to purge.")

        ensure_stacks_detached(komodo, name, actions)
        if pangolin.site(name, optional=True) or any(
            pangolin.site_by_id(item["reference"]) for item in actions if item["kind"] == ResourceKind.PANGOLIN_SITE
        ):
            raise ValueError("Detach the host's Pangolin site before purging Goku data.")
        mounts = [item["reference"] for item in actions if item["kind"] == ResourceKind.MOUNT]
        if not mounts or not check_host_mounts(device, known_hosts, public_key, mounts):
            raise ValueError("Remove the host's recorded mounts and their fstab entries before purging Goku data.")

        for item in candidates:
            directory = PurePosixPath(item["reference"])
            children = [child for child in actions if child is not item
                        and PurePosixPath(child["reference"]).is_relative_to(directory)]
            if any(child["action"] != "remove" for child in children):
                item["reason"] = "contains-preserved-resource"
                continue
            item["status"] = purge_directory(expected[item["reference"]], name)
            for child in children:
                child["status"] = "absent"
        return result
