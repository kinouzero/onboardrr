"""List, mount, or explicitly format USB storage on a Raspberry Pi."""

import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


BY_ID = Path("/dev/disk/by-id")
FSTAB = Path("/etc/fstab")
BOOT_CMDLINES = (Path("/boot/firmware/cmdline.txt"), Path("/boot/cmdline.txt"))
ACTIVE_CMDLINE = Path("/proc/cmdline")
UAS_FALLBACK_BRIDGE = "152d:1561"
USB_QUIRKS = "usb-storage.quirks="
ALIAS = re.compile(r"/dev/disk/by-id/usb-[A-Za-z0-9_.:+-]+")
TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ABSOLUTE_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+)+")
UUID = re.compile(r"[A-Fa-f0-9-]{4,64}")
FILESYSTEMS = {"ext4", "exfat", "xfs", "btrfs", "vfat"}
EFI_TYPES = {"c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef"}
SYSTEM_PATHS = ("/boot", "/etc", "/usr", "/var", "/home")


def run(*args, timeout=20, env=None):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(f"Could not run {args[0]} on the Raspberry Pi.") from None
    if result.returncode:
        detail = result.stderr.splitlines()
        raise ValueError(f"{args[0]} failed: {detail[-1]}" if detail else f"{args[0]} failed.")
    return result.stdout


def tree():
    columns = "NAME,TYPE,TRAN,SIZE,FSTYPE,UUID,LABEL,MOUNTPOINTS,RO,PARTTYPE,PTTYPE,MAJ:MIN"
    return json.loads(run("lsblk", "--json", "--paths", "--output", columns))["blockdevices"]


def walk(node):
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def mounts(node):
    return [value for value in node.get("mountpoints") or [] if value]


def system_disk(disk, home=None):
    for node in walk(disk):
        for mount in mounts(node):
            if mount in {"/", "[SWAP]", str(home), *SYSTEM_PATHS}:
                return True
            if home is not None and Path(mount).parent == home and TARGET_NAME.fullmatch(Path(mount).name):
                continue
            if any(mount.startswith(prefix + "/") for prefix in SYSTEM_PATHS):
                return True
    return False


def candidates(devices, home=None):
    aliases = {str(path.resolve()): str(path) for path in sorted(BY_ID.glob("usb-*"))
               if path.is_symlink() and ALIAS.fullmatch(str(path))}
    for disk in devices:
        if disk["type"] != "disk" or disk["tran"] != "usb" or system_disk(disk, home):
            continue
        nodes = disk.get("children") or ([disk] if not disk.get("pttype") else [])
        for node in nodes:
            alias = aliases.get(node["name"])
            if (alias and node["type"] in {"part", "disk"} and not node.get("children")
                    and str(node.get("parttype")).lower() not in EFI_TYPES):
                yield alias, node


def assert_current(alias, node):
    try:
        info = Path(alias).stat()
        number = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    except OSError:
        raise ValueError("The selected USB device was disconnected.") from None
    if not stat.S_ISBLK(info.st_mode) or number != node["maj:min"]:
        raise ValueError("The selected USB device changed. Load the USB devices again.")


def disable_uas_in_cmdline(contents):
    line = contents.rstrip("\r\n")
    if not line.strip() or "\n" in line or "\r" in line or len(contents.splitlines()) != 1:
        raise ValueError("The Raspberry Pi boot command line must contain one nonempty line.")
    matches = list(re.finditer(r"(?<!\S)" + re.escape(USB_QUIRKS) + r"\S*", line))
    if len(matches) > 1:
        raise ValueError("Review the duplicate usb-storage.quirks settings in the Raspberry Pi boot command line.")
    if not matches:
        return line.rstrip() + f" {USB_QUIRKS}{UAS_FALLBACK_BRIDGE}:u" + contents[len(line):]
    match = matches[0]
    entries = match.group().removeprefix(USB_QUIRKS).split(",")
    if not all(re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}:[a-zA-Z]*", entry) for entry in entries):
        raise ValueError("Review the existing usb-storage.quirks format in the Raspberry Pi boot command line.")
    matching = [index for index, entry in enumerate(entries) if entry.lower().startswith(UAS_FALLBACK_BRIDGE + ":")]
    if len(matching) > 1:
        raise ValueError(f"Review the duplicate USB quirks for bridge {UAS_FALLBACK_BRIDGE}.")
    if not matching:
        entries.append(UAS_FALLBACK_BRIDGE + ":u")
    else:
        index = matching[0]
        if "u" in entries[index].rsplit(":", 1)[1]:
            return contents
        entries[index] += "u"
    return contents[:match.start()] + USB_QUIRKS + ",".join(entries) + contents[match.end():]


def write_boot_cmdline(path, original, updated, metadata):
    with tempfile.NamedTemporaryFile(prefix=f"{path.name}.onboardrr-before-uas-", dir=path.parent,
                                     delete=False) as backup:
        backup.write(original)
        backup.flush()
        os.fsync(backup.fileno())
    atomic_write(path, updated, metadata, expected=original)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return backup.name


def prepare_usb_transport(request, alias, node):
    output = run("udevadm", "info", "--query=property", "--name", node["name"])
    properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    bridge = f"{properties.get('ID_USB_VENDOR_ID', '')}:{properties.get('ID_USB_MODEL_ID', '')}".lower()
    if bridge != UAS_FALLBACK_BRIDGE:
        return None
    boot = next((path for path in BOOT_CMDLINES if path.parent.is_mount() and path.is_file()), None)
    if boot is None:
        raise ValueError("Mount the Raspberry Pi boot partition before configuring the USB bridge fallback.")
    metadata = boot.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("The Raspberry Pi boot command line must be a regular file.")
    original = boot.read_bytes()
    updated = disable_uas_in_cmdline(original.decode()).encode()
    active_cmdline = ACTIVE_CMDLINE.read_text()
    active = disable_uas_in_cmdline(active_cmdline) == active_cmdline
    if active and properties.get("ID_USB_DRIVER") != "usb-storage":
        raise ValueError(f"Bridge {bridge} is not using usb-storage. Check the active USB driver before retrying.")
    if active and updated == original:
        return None

    report = {"status": "preview", "usb_device": alias, "mountpoint": request["mountpoint"],
              "usb_bridge": bridge, "reboot_required": not active}
    if not request["apply"]:
        if active:
            return None
        message = "Reboot the Raspberry Pi and retry this step" if updated == original else (
            "Apply to configure the USB bridge fallback, then reboot the Raspberry Pi and retry this step"
        )
        return {**report, "blockers": [f"{message}."]}
    assert_current(alias, node)
    if updated != original:
        report["boot_backup"] = write_boot_cmdline(boot, original, updated, metadata)
    if active:
        return None
    return {**report, "status": "reboot-required",
            "blockers": ["Reboot the Raspberry Pi to enable the USB bridge fallback, then retry this step."]}


def format_volume(request, alias, node, devices):
    if node["ro"]:
        raise ValueError("The selected volume is read-only.")
    if node.get("fstype") and node["fstype"] not in FILESYSTEMS | {"ntfs", "ext2", "ext3", "hfsplus", "apfs"}:
        raise ValueError("The selected volume has a storage signature that must be handled manually.")
    if request["apply"] and request["confirm_device"] != alias:
        raise ValueError("Confirm formatting with the exact selected USB device path.")
    report = {"status": "preview", "usb_device": alias, "mountpoint": request["mountpoint"],
              "filesystem": "ext4", "size": node["size"], "previous_mountpoints": mounts(node)}
    if not request["apply"]:
        return report
    uuid = str(uuid4())
    if any(item.get("uuid") == uuid for disk in devices for item in walk(disk)):
        raise ValueError("The new filesystem UUID conflicts with a connected device. Try again.")
    formatted = {**node, "fstype": "ext4", "uuid": uuid, "mountpoints": []}
    mount_configuration(request, formatted, replacing=node)
    unmount_volume(request, alias, node)
    mount_configuration(request, formatted, replacing={**node, "mountpoints": []})
    assert_current(alias, node)
    # Some USB bridges lose the connection on discard or block zeroing requests.
    options = f"nodiscard,root_owner={request['uid']}:{request['gid']}"
    environment = {**os.environ, "UNIX_IO_NOZEROOUT": "1"}
    try:
        run("/usr/sbin/mkfs.ext4", "-F", "-U", uuid, "-E", options, alias, timeout=240, env=environment)
    except ValueError as error:
        try:
            assert_current(alias, node)
        except ValueError as device_error:
            blocker = f"{device_error} The filesystem may be incomplete. Check the USB connection and reload the devices."
        else:
            blocker = "Formatting failed. The filesystem may be incomplete. Check the host's disk logs."
        return {**report, "status": "format-incomplete", "mounted": False, "blockers": [blocker],
                "format_error": str(error)}
    report.update(status="formatted", uuid=uuid)
    try:
        metadata = fstab_metadata()
        original = FSTAB.read_text()
        cleaned, _ = remove_formatted_entries(original, node)
        if cleaned != original:
            write_fstab(cleaned, metadata)
            run("systemctl", "daemon-reload")
        assert_current(alias, formatted)
        run("udevadm", "trigger", "--action=change", "--settle", formatted["name"])
        configuration = mount_configuration(request, formatted)
        activate_mount(formatted, request["mountpoint"], configuration)
    except (OSError, ValueError) as error:
        return {**report, "mounted": False,
                "blockers": [f"The volume was formatted. Use Mount USB storage to retry mounting it: {error}"]}
    return {**report, "mounted": True}


def atomic_write(path, contents, metadata, *, expected=None):
    fd, temporary = tempfile.mkstemp(prefix=f".onboardrr-{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            temporary_metadata = os.fstat(stream.fileno())
            if (temporary_metadata.st_uid, temporary_metadata.st_gid) != (metadata.st_uid, metadata.st_gid):
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            if stat.S_IMODE(temporary_metadata.st_mode) != stat.S_IMODE(metadata.st_mode):
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None:
            current = path.lstat()
            if (not os.path.samestat(current, metadata) or current.st_ctime_ns != metadata.st_ctime_ns
                    or path.read_bytes() != expected):
                raise ValueError("The Raspberry Pi boot command line changed during preparation. Retry the step.")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_fstab(contents, metadata):
    atomic_write(FSTAB, contents.encode(), metadata)


def same_source(source, node):
    if "=" in source:
        tag, value = source.split("=", 1)
        if tag not in {"UUID", "PARTUUID", "LABEL", "PARTLABEL"}:
            return False
        value = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value.strip('"'))
        if not value or "/" in value or value in {".", ".."}:
            return False
        source = "/dev/disk/by-" + tag.lower() + "/" + value
    return source.startswith("/") and Path(source).resolve() == Path(node["name"])


def fstab_entries(contents):
    for row in contents.splitlines():
        fields = row.split()
        if len(fields) >= 2 and not fields[0].startswith("#"):
            yield fields


def managed_mount_entry(fields, node):
    return (len(fields) >= 8 and node.get("uuid") and fields[0] == f"UUID={node['uuid']}"
            and fields[-2:] == ["#", "onboardrr-usb"] and "x-systemd.automount" in fields[3].split(",")
            and ABSOLUTE_PATH.fullmatch(fields[1]) and ".." not in Path(fields[1]).parts)


def remove_formatted_entries(contents, node):
    kept, removed = [], set()
    for row in contents.splitlines(keepends=True):
        fields = next(fstab_entries(row), None)
        if fields and (fields[0] == f"UUID={node.get('uuid')}" or same_source(fields[0], node)):
            if not managed_mount_entry(fields, node):
                raise ValueError("Review the existing /etc/fstab entry for this volume before formatting.")
            removed.add(fields[1])
        else:
            kept.append(row)
    return "".join(kept), removed


def fstab_update(original, node, target, line):
    existing = False
    for fields in fstab_entries(original):
        if not existing and fields == line.split():
            existing = True
            continue
        if fields[1] == target or same_source(fields[0], node) or fields[0] == f"UUID={node['uuid']}":
            raise ValueError("The volume or mount point already has an entry in /etc/fstab.")
    if existing:
        return original
    separator = "\n" if original and not original.endswith("\n") else ""
    return original + separator + line + "\n"


def mount_types(target):
    target = target.translate(str.maketrans({"\\": r"\134", " ": r"\040", "\t": r"\011", "\n": r"\012"}))
    return [line.split(" - ", 1)[1].split()[0] for line in Path("/proc/self/mountinfo").read_text().splitlines()
            if line.split()[4] == target]


def fstab_metadata():
    metadata = FSTAB.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("/etc/fstab must be a regular file.")
    return metadata


def mount_configuration(request, node, *, replacing=None):
    target = request["mountpoint"]
    filesystem, uuid = node["fstype"], node["uuid"]
    path = Path(target)
    if path.parent.resolve() != path.parent or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("Choose a mount point and parent that are real directories.")
    metadata = fstab_metadata()
    options = "defaults,nodev,nosuid,nofail,x-systemd.automount,x-systemd.device-timeout=10s"
    if filesystem in {"exfat", "vfat"}:
        options += f",uid={int(request['uid'])},gid={int(request['gid'])},umask=027"
    fsck = 2 if filesystem == "ext4" else 0
    line = f"UUID={uuid}\t{target}\t{filesystem}\t{options}\t0\t{fsck} # onboardrr-usb"
    original = FSTAB.read_text()
    contents, replaced = remove_formatted_entries(original, replacing) if replacing is not None else (original, set())
    updated = fstab_update(contents, node, target, line)
    if target not in mounts(replacing if replacing is not None else node):
        mounted = mount_types(target)
        managed_autofs = mounted == ["autofs"] and (updated == original or target in replaced)
        if mounted and not managed_autofs:
            raise ValueError("Another filesystem is already mounted at the selected path.")
        if not managed_autofs and path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError("Choose an empty mount point to avoid hiding local data.")
    return original, updated, metadata


def activate_mount(node, target, configuration):
    original, updated, metadata = configuration
    path = Path(target)
    if not path.exists():
        path.mkdir(mode=0o755)
    changed = updated != original
    if changed:
        write_fstab(updated, metadata)
    try:
        if changed:
            run("systemctl", "daemon-reload")
        if target not in mounts(node):
            unit = run("systemd-escape", "--path", "--suffix=mount", target).strip()
            run("systemctl", "start", unit, timeout=30)
    except ValueError:
        if changed:
            write_fstab(original, metadata)
            run("systemctl", "daemon-reload")
        raise


def mount_volume(request, alias, node, devices):
    target = request["mountpoint"]
    filesystem, uuid = node.get("fstype"), node.get("uuid")
    if filesystem not in FILESYSTEMS or not uuid or not UUID.fullmatch(uuid):
        raise ValueError("Select a volume with a supported filesystem and UUID, or format it separately.")
    uuid_devices = {item["name"] for disk in devices for item in walk(disk) if item.get("uuid") == uuid}
    if len(uuid_devices) != 1:
        raise ValueError("The volume UUID is also used by another connected device.")
    report = {"usb_device": alias, "mountpoint": target, "filesystem": filesystem, "uuid": uuid,
              "current_mountpoints": mounts(node)}
    other_mounts = [path for path in mounts(node) if path != target]
    blocker = f"Unmount the selected volume from {', '.join(other_mounts)} before applying."
    if not request["apply"]:
        return {"status": "preview", **report, "blockers": [blocker] if other_mounts else []}
    if other_mounts:
        raise ValueError(blocker)
    configuration = mount_configuration(request, node)
    assert_current(alias, node)
    activate_mount(node, target, configuration)
    return {"status": "mounted", **report}


def current_mounts(node):
    return {target for disk in tree() for item in walk(disk) if item["maj:min"] == node["maj:min"]
            for target in mounts(item)}


def unmount_volume(request, alias, node):
    targets = set(mounts(node))
    managed = {
        fields[1] for fields in fstab_entries(FSTAB.read_text())
        if managed_mount_entry(fields, node)
    }
    types = {target: mount_types(target) for target in targets | managed}
    automounts = {target for target, filesystems in types.items() if "autofs" in filesystems}
    if automounts - managed or any(
        len(filesystems) > int(target in targets) + int(target in automounts)
        for target, filesystems in types.items() if target in targets or target in automounts
    ):
        raise ValueError("An unrelated automount or filesystem occupies one of the mount points.")
    report = {"usb_device": alias, "current_mountpoints": sorted(targets), "active_automounts": sorted(automounts)}
    if not request["apply"]:
        return {"status": "preview", **report}
    if not targets and not automounts:
        return {"status": "already-unmounted", **report}
    assert_current(alias, node)
    for target in sorted(automounts):
        unit = run("systemd-escape", "--path", target).strip()
        run("systemctl", "stop", f"{unit}.mount", f"{unit}.automount", timeout=30)
    if current_mounts(node):
        assert_current(alias, node)
        run("umount", "--all-targets", "--", alias, timeout=30)
    if current_mounts(node) or any("autofs" in mount_types(target) for target in automounts):
        raise ValueError("The selected USB volume still has an active mount or automount.")
    return {"status": "unmounted", "usb_device": alias, "previous_mountpoints": sorted(targets),
            "current_mountpoints": [], "active_automounts": []}


def resolve_mountpoint(home, requested):
    if requested and requested.startswith("~/"):
        requested = f"{home}/{requested[2:]}"
    target = Path(requested) if requested else home / "ssd"
    if (not ABSOLUTE_PATH.fullmatch(str(target)) or ".." in target.parts
            or (target.parent == home and str(home) in {"/", *SYSTEM_PATHS})
            or target.parent not in {home, Path("/mnt")} or not TARGET_NAME.fullmatch(target.name)):
        raise ValueError("Choose a directory directly under the operator's home or /mnt.")
    return str(target)


def handle(request):
    try:
        home = Path(pwd.getpwnam(request["operator"]).pw_dir).resolve()
    except KeyError:
        raise ValueError("Could not resolve the operator's home on the Raspberry Pi.") from None
    if not request.get("unmount"):
        request = {**request, "mountpoint": resolve_mountpoint(home, request.get("mountpoint"))}
    devices = tree()
    volumes = dict(candidates(devices, home))
    alias = request.get("usb_device")
    if not alias:
        if request["apply"]:
            raise ValueError("Select a USB storage device before applying.")
        return {"status": "preview", "mountpoint": request.get("mountpoint"), "usb_devices": [
            {"path": path, "size": node["size"], "filesystem": node["fstype"], "label": node["label"],
             "mountpoints": mounts(node)}
            for path, node in volumes.items()
        ]}
    if alias not in volumes:
        raise ValueError("Select an eligible USB volume from the current device list.")
    node = volumes[alias]
    if request.get("unmount"):
        return unmount_volume(request, alias, node)
    if request["format"] and request["apply"] and request.get("confirm_device") != alias:
        raise ValueError("Confirm formatting with the exact selected USB device path.")
    transport = prepare_usb_transport(request, alias, node)
    if transport:
        return transport
    if request["format"]:
        return format_volume(request, alias, node, devices)
    return mount_volume(request, alias, node, devices)


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else json.load(sys.stdin)
        if request["apply"]:
            with open("/run/lock/onboardrr-usb-storage.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                run("udevadm", "settle", "--timeout=5")
                report = handle(request)
        else:
            report = handle(request)
        print(json.dumps(report))
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}))
