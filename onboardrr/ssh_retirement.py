"""Revoke only enrolled SSH keys after verifying an independent login."""

import shlex

from .inventory import NAS_PROFILE
from .retirement import retirement_plan
from .runs import ResourceKind
from .storage import device_lock


def retire_key(database, device, known_hosts, enrolled_path, fallback_path, *, apply):
    from . import ssh

    enrolled = ssh.load_public_key(enrolled_path)
    fallback = ssh.load_public_key(fallback_path)
    if enrolled.asbytes() == fallback.asbytes():
        raise ValueError("The fallback SSH identity must differ from the enrolled key.")
    reference = ssh.fingerprint(enrolled)
    with device_lock(database, device):
        actions = retirement_plan(database, device["name"])["actions"]
        if not any(item["kind"] == ResourceKind.SSH_KEY and item["reference"] == reference
                   and item["action"] == "remove-after-fallback-ssh-check" for item in actions):
            raise ValueError("The selected key is not recorded as installed by Onboardrr.")
        with ssh.agent_identity(fallback) as agent, ssh.connect(device, known_hosts, key=agent) as client:
            if ssh.command(client, "true")[0] != 0:
                raise ValueError("The fallback SSH session could not execute a verification command.")
            if not apply:
                return {"device": device["name"], "mode": "preview", "fallback_verified": True}
            entware = device["profile"] == NAS_PROFILE
            source = (ssh.REMOTE / "install_key.py").read_text()
            interpreter = "PATH=/opt/bin:$PATH python3" if entware else "python3"
            payload = f"{enrolled.get_name()} {enrolled.get_base64()}".encode()
            status, output = ssh.command(client, f"{interpreter} -c {shlex.quote(source)} --remove", payload)
            outcome = output.decode().strip()
            if status != 0 or outcome not in {"removed", "absent"}:
                raise ValueError("The enrolled SSH key could not be safely removed.")
            try:
                ssh.verify_key(device, known_hosts, agent)
            except (ValueError, OSError) as original:
                if outcome == "removed":
                    try:
                        restored = ssh.script(client, "install_key.py", payload, entware=entware).strip()
                        if restored not in {b"created", b"reused"}:
                            raise ValueError("The host did not confirm SSH key restoration.")
                    except (ValueError, OSError) as error:
                        raise ValueError("Fallback SSH recheck failed and the enrolled key could not be restored.") from error
                    raise ValueError("Fallback SSH recheck failed. The enrolled key was restored.") from original
                raise
    return {"device": device["name"], "mode": "apply", "fallback_verified": True, "status": outcome}
