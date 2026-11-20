"""Manage USB storage over the enrolled Raspberry Pi SSH connection."""

import json
import shlex
from contextlib import nullcontext

from .inventory import PROFILE
from .storage import device_lock


def configure(database, device, known_hosts, public_key, *, usb_device=None, mountpoint=None,
              format_volume=False, unmount=False, confirm_device=None, apply=False, password_provider=lambda: None):
    from . import ssh

    if device["profile"] != PROFILE:
        raise ValueError("USB storage preparation is available for the Raspberry Pi profile.")
    if apply and not usb_device:
        raise ValueError("Select a USB storage device before applying.")
    if unmount and (format_volume or mountpoint is not None or confirm_device is not None):
        raise ValueError("Choose the USB volume to unmount without formatting or mount-point options.")
    if format_volume and apply and (not confirm_device or confirm_device != usb_device):
        raise ValueError("Confirm formatting with the exact selected USB device path.")
    request = {"usb_device": usb_device, "mountpoint": mountpoint, "operator": device["ssh_user"], "format": format_volume,
               "unmount": unmount, "confirm_device": confirm_device, "apply": apply}
    key = ssh.load_public_key(public_key)
    source = (ssh.REMOTE / "usb_storage.py").read_text()
    interpreter = "python3 -c " + shlex.quote(source)
    command = interpreter
    payload = json.dumps(request).encode()
    with (device_lock(database, device) if apply else nullcontext()), ssh.agent_identity(key) as agent, \
            ssh.connect(device, known_hosts, key=agent) as client:
        if apply:
            status, ids = ssh.command(client, "id -u && id -g")
            fields = ids.split()
            if status or len(fields) != 2 or not all(field.isdigit() for field in fields):
                raise ValueError("Could not resolve the operator's account on the Raspberry Pi.")
            request.update(uid=int(fields[0]), gid=int(fields[1]))
            payload = json.dumps(request).encode()
            if request["uid"] != 0:
                password = password_provider()
                if password is None:
                    command = "sudo -n -- " + command
                else:
                    command = (
                        'IFS= read -r password && request=$(cat) || exit 1; '
                        'printf "%s\\n" "$password" | sudo -S -k -p "" -- '
                        + command + ' "$request"'
                    )
                    payload = (password + "\n").encode() + payload
        timeout = 450 if format_volume else 110 if unmount else 70
        status, output = ssh.command(client, command, payload, timeout=timeout)
    if status:
        raise ValueError("Could not configure USB storage over SSH. Verify sudo access on the Raspberry Pi.")
    report = json.loads(output)
    if "error" in report:
        raise ValueError(report["error"])
    return {"device": device["name"], **report}
