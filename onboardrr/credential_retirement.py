"""Remove owned device variables and Newt credentials after detachment."""

import json
import re

from .pangolin import newt_record, newt_reference, read_newt_record
from .retirement import ensure_stacks_detached, retirement_plan
from .runs import ResourceKind
from .storage import device_lock


def retire_credentials(database, device, komodo, pangolin, bao, *, apply=False):
    name = device["name"]
    prefix = name.upper().replace("-", "_")
    expected = {f"{prefix}_HOSTNAME": name, f"{prefix}_IP": device["host"]}
    fields = (f"{prefix}_NEWT_ID", f"{prefix}_NEWT_SECRET")
    with device_lock(database, device):
        actions = retirement_plan(database, name)["actions"]
        names = {
            item["reference"] for item in actions if item["kind"] == ResourceKind.KOMODO_VARIABLE
            and item["action"] == "remove" and item["reference"] in expected
        }
        owned = {
            item["reference"] for item in actions if item["kind"] == ResourceKind.NEWT_CREDENTIALS
            and item["action"] == "remove"
        }
        result = {"device": name, "variables": dict.fromkeys(sorted(names), "pending"),
                  "newt": "pending" if owned else "ownership-unrecorded"}
        if not apply or not (names or owned):
            return {"mode": "apply" if apply else "preview", **result}

        ensure_stacks_detached(komodo, name, actions)
        server_ids = {item["reference"] for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER}
        servers = komodo._request("/read", "ListServers", {"limit": 0})
        if any(server["id"] in server_ids or server["name"] == name for server in servers):
            raise ValueError("Remove the device's Komodo registration before removing its credentials.")
        if pangolin.site(name, optional=True) or any(
            pangolin.site_by_id(item["reference"]) for item in actions if item["kind"] == ResourceKind.PANGOLIN_SITE
        ):
            raise ValueError("Remove the recorded Pangolin site before removing its credentials.")

        # An admin-only read confirms that the dependency scan is not filtered
        # by the caller's resource permissions.
        komodo._request("/read", "ListOnboardingKeys")
        checked = names | (set(fields) if owned else set())
        references = referenced_names(komodo, checked)
        variables = {item["name"]: item.get("value") for item in komodo._request("/read", "ListVariables")}
        for key, value in variables.items():
            if key not in checked and isinstance(value, str):
                references.update(field for field in checked if f"[[{field}]]" in value)
        for variable in sorted(names):
            if variable not in variables:
                result["variables"][variable] = "absent"
            elif variable in references:
                result["variables"][variable] = "referenced"
            elif variables[variable] != expected[variable]:
                result["variables"][variable] = "value-changed"
            else:
                deleted = komodo._request("/write", "DeleteVariable", {"name": variable})
                if deleted.get("name") != variable:
                    raise ValueError("Komodo did not confirm removal of the expected variable.")
                result["variables"][variable] = "removed"
        if owned:
            if references.intersection(fields):
                result["newt"] = "referenced"
            else:
                path, keys = newt_record(bao, name)
                values, version = read_newt_record(bao, path, keys)
                if not values:
                    result["newt"] = "absent"
                elif not all(isinstance(values.get(key), str) and values[key] for key in keys):
                    result["newt"] = "incomplete-pair"
                elif newt_reference(*(values[key] for key in keys)) not in owned:
                    result["newt"] = "value-changed"
                else:
                    bao.patch(path, dict.fromkeys(keys), version=version)
                    remaining, _ = read_newt_record(bao, path, keys)
                    if remaining:
                        raise ValueError("OpenBao did not confirm removal of the Newt pair.")
                    result["newt"] = "removed"
        return {"mode": "apply", **result}


def referenced_names(komodo, names):
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in sorted(names)) + r")\b")
    # Onboardrr's variables are referenced by named Komodo resources. Read their
    # definitions instead of relying on the attached server or current state.
    groups = {
        "Stacks": ("Stack", "stack"), "Deployments": ("Deployment", "deployment"),
        "Servers": ("Server", "server"), "Builders": ("Builder", "builder"),
        "Builds": ("Build", "build"), "Repos": ("Repo", "repo"),
        "Actions": ("Action", "action"), "ResourceSyncs": ("ResourceSync", "sync"),
        "Procedures": ("Procedure", "procedure"),
    }
    referenced = set()
    for group, (kind, argument) in groups.items():
        for item in komodo._request("/read", f"List{group}", {"limit": 0}):
            config = komodo._request("/read", f"Get{kind}", {argument: item["id"]})["config"]
            referenced.update(pattern.findall(json.dumps(config)))
    return referenced
