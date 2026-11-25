"""Read the host's dedicated Compose AppRole and configure OpenBao Agent."""

import base64
import json
import os
import re
from ipaddress import IPv4Address
from pathlib import Path

from .http_json import HttpStatusError
from .inventory import HOST_LABEL
from .remote.compose_update import PROJECT
from .secrets import PATH_SEGMENT, OpenBao, configured_openbao, deployment_kv_path, deployment_layout, token_file


APPROLE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
MANAGED_AUTH = "onboardrr-compose"
MANAGED_POLICY = "onboardrr-compose-host"
KOMODO_AUTH = "onboardrr-komodo"
BOOTSTRAP_FIELDS = (
    "role_id", "secret_id", "projects", "docker_api_port", "host_ip", "komodo_role_id",
    "komodo_secret_id", "komodo_common_mount", "komodo_newt_mount", "auth_mount",
    "periphery_cache", "hostname_cache", "legacy_templates", "compose_config_variables",
    "komodo_auth_mount", "komodo_template",
)
COMPOSE_VARIABLES = r'''{{- if sprig_hasKey .Data.data "variables" -}}
{{- $values := .Data.data.variables -}}
{{- if not (sprig_kindIs "map" $values) -}}{{ parseJSON "expected a Compose variables map" }}{{- end -}}
{{- range $key, $value := $values -}}
{{- if or (not (sprig_regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $key)) (ne (printf "%T" $value) "string") -}}
{{- parseJSON "expected a valid Compose variable name and string value" -}}
{{- end -}}
{{- printf "\n%s=%s\n" $key (sprig_toRawJson (replaceAll "$" "$$" $value)) -}}
{{- end -}}
{{- end -}}'''
KOMODO_NEWT_SECRETS = '''{{- range $alias, $_ := $variables -}}
{{- if sprig_regexMatch "^[A-Z0-9_]+_NEWT_(ID|SECRET)$" $alias -}}
{{- $_ := sprig_unset $variables $alias -}}
{{- end -}}
{{- end -}}
{{- range $host := secrets "services/metadata/pangolin/newt/" -}}
{{- if sprig_regexMatch "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" $host -}}
{{- with secret (printf "services/data/pangolin/newt/%s" $host) -}}
{{- $record := .Data.data -}}
{{- if or (sprig_hasKey $record "ID") (sprig_hasKey $record "SECRET") -}}
{{- $id := index $record "ID" -}}
{{- $secret := index $record "SECRET" -}}
{{- if or (ne (printf "%T" $id) "string") (ne (printf "%T" $secret) "string") (not $id) (not $secret) -}}
{{- parseJSON "expected a complete Newt credential pair" -}}
{{- end -}}
{{- $prefix := replaceAll "-" "_" (toUpper $host) -}}
{{- $_ := sprig_set $variables (printf "%s_NEWT_ID" $prefix) $id -}}
{{- $_ := sprig_set $variables (printf "%s_NEWT_SECRET" $prefix) $secret -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
'''
KOMODO_SECRETS = '''{{ with secret "komodo/data/variables" -}}
{{ if not .Data.data }}{{ parseJSON "expected a nonempty Komodo secrets map" }}{{ end -}}
{{ $variables := sprig_merge (sprig_dict) .Data.data -}}
[secrets]
{{ range $key, $value := (sprig_omit $variables "USER") -}}
{{ if ne (printf "%T" $value) "string" }}{{ parseJSON "expected a string for every Komodo secret" }}{{ end -}}
{{ toJSON $key }} = {{ toJSON $value }}
{{ end -}}
{{ end }}'''


def agent_template(contents, destination):
    return {"contents": contents, "destination": destination, "perms": "0600",
            "backup": False, "error_on_missing_key": True}


def valid_template_source(contents):
    return isinstance(contents, str) and bool(contents.strip()) and len(contents) <= 32768


def agent_credentials(url, auth_mount, role_id, secret_id, templates):
    if not all(isinstance(value, str) and APPROLE_ID.fullmatch(value) for value in (role_id, secret_id)):
        raise ValueError("OpenBao Agent requires valid AppRole IDs.")
    config = {
        "vault": {"address": url},
        "auto_auth": {"method": [{
            "type": "approle",
            "mount_path": f"auth/{auth_mount}",
            "config": {
                "role_id_file_path": "/agent/role-id",
                "secret_id_file_path": "/agent/secret-id",
                "remove_secret_id_file_after_reading": False,
            },
        }]},
        "template_config": {"static_secret_render_interval": "30s"},
        "template": templates,
    }
    return {
        "role_id": role_id,
        "secret_id": secret_id,
        "config_b64": base64.b64encode(json.dumps(config, indent=2).encode()).decode("ascii"),
    }


def managed_bootstrap(bao, device, path, *, layout="legacy"):
    role = f"/v1/auth/{MANAGED_AUTH}/role/{device}"
    project_metadata = deployment_kv_path(bao.mount, f"deployments/compose/{device}", metadata=True, layout=layout)

    def request(method, route, data=None):
        return bao.service.request(method, route, headers=bao.headers, operation="Compose Agent bootstrap", data=data)

    try:
        projects = request("LIST", f"/v1/{project_metadata}")["data"]["keys"]
    except HttpStatusError as error:
        if error.status != 404:
            raise
        projects = []
    try:
        bao.read(f"deployments/periphery/{device}", "passkey")
        periphery_cache = "periphery" not in projects
    except HttpStatusError as error:
        if error.status != 404:
            raise
        periphery_cache = False
    hostname_cache = not projects and not periphery_cache
    if periphery_cache:
        projects.append("periphery")
    if hostname_cache:
        from .stacks import ensure_host_value

        ensure_host_value(configured_openbao(mount="hosts"), device, "HOSTNAME", device)
        projects.append("onboardrr")
    needs_config = "config" not in projects
    if needs_config:
        projects.append("config")
    validate_projects(projects)
    if needs_config:
        config_path = f"deployments/compose/{device}/config"
        try:
            bao.create(config_path, {"variables": {}})
        except HttpStatusError as error:
            if error.status not in (400, 409):
                raise
            existing_projects = request("LIST", f"/v1/{project_metadata}")
            if "config" not in existing_projects["data"]["keys"]:
                raise error
    settings = {
        "bind_secret_id": "true", "token_no_default_policy": "true", "token_policies": MANAGED_POLICY,
        "secret_id_num_uses": "0", "secret_id_ttl": "0", "token_num_uses": "0", "token_period": "3600",
    }
    try:
        identity = request("GET", f"{role}/role-id")
    except HttpStatusError as error:
        if error.status != 404:
            raise
        request("POST", role, settings)
        identity = request("GET", f"{role}/role-id")
    role_id = identity["data"]["role_id"]
    issued = request("POST", f"{role}/secret-id", {})["data"]
    credentials = {"role_id": role_id, "secret_id": issued["secret_id"]}
    auth = bao.service.request("POST", f"/v1/auth/{MANAGED_AUTH}/login", headers={},
                               operation="verify managed Compose identity", data=credentials)["auth"]
    if (auth.get("policies") != [MANAGED_POLICY] or auth.get("metadata", {}).get("role_name") != device):
        request("POST", f"{role}/secret-id-accessor/destroy",
                {"secret_id_accessor": issued["secret_id_accessor"]})
        raise ValueError("The managed OpenBao AppRole does not have the expected host policy.")
    bao.service.request("POST", "/v1/auth/token/revoke-self", headers={"X-Vault-Token": auth["client_token"]},
                        operation="revoke bootstrap verification token", data={})
    record = {
        **credentials, "projects": ",".join(sorted(projects)),
        "auth_mount": MANAGED_AUTH, "periphery_cache": periphery_cache, "hostname_cache": hostname_cache,
    }
    try:
        bao.create(path, record)
    except HttpStatusError as error:
        if error.status not in (400, 409):
            raise
        existing, _ = bao.fields_with_version(path, *BOOTSTRAP_FIELDS)
        if existing.get("secret_id") != record["secret_id"]:
            request("POST", f"{role}/secret-id-accessor/destroy",
                    {"secret_id_accessor": issued["secret_id_accessor"]})
        return existing
    return record


def validate_projects(projects):
    if (not projects or len(projects) > 32
            or {"onboardrr-openbao-agent", "onboardrr-openbao-agent-komodo"}.intersection(projects)
            or not all(isinstance(name, str) and PROJECT.fullmatch(name) and len(name) <= 128 for name in projects)):
        raise ValueError("OpenBao Compose Agent requires a list of valid Compose project names.")


def compose_controller():
    bao = configured_openbao()
    if bao is None:
        raise ValueError("Configure the Onboardrr OpenBao controller before provisioning an agent.")
    return bao


def provision_core_identity(bao, device, path, record, version):
    role_name = f"{device}-komodo-core"
    policy = "onboardrr-komodo-core"
    role = f"/v1/auth/{KOMODO_AUTH}/role/{role_name}"

    def request(method, route, data=None):
        return bao.service.request(method, route, headers=bao.headers, operation="Komodo Agent bootstrap", data=data)

    try:
        identity = request("GET", role + "/role-id")
    except HttpStatusError as error:
        if error.status != 404:
            raise
        request("POST", role, {"bind_secret_id": True, "token_no_default_policy": True,
                               "token_policies": policy, "token_period": "3600"})
        identity = request("GET", role + "/role-id")
    issued = request("POST", role + "/secret-id", {})["data"]
    credentials = {"role_id": identity["data"]["role_id"], "secret_id": issued["secret_id"]}
    saved = False
    try:
        auth = bao.service.request("POST", f"/v1/auth/{KOMODO_AUTH}/login", headers={},
                                   operation="verify Komodo Agent identity", data=credentials)["auth"]
        bao.service.request("POST", "/v1/auth/token/revoke-self", headers={"X-Vault-Token": auth["client_token"]},
                            operation="revoke bootstrap verification token", data={})
        if auth.get("policies") != [policy] or auth.get("metadata", {}).get("role_name") != role_name:
            raise ValueError("The Komodo OpenBao AppRole does not have the expected dedicated policy.")
        fields = {"komodo_role_id": credentials["role_id"], "komodo_secret_id": credentials["secret_id"],
                  "komodo_auth_mount": KOMODO_AUTH}
        bao.patch(path, fields, version=version)
        saved = True
        return {**record, **fields}
    finally:
        if not saved:
            request("POST", role + "/secret-id-accessor/destroy", {"secret_id_accessor": issued["secret_id_accessor"]})


def configured_compose_agent(device, *, ensure=False, require_core=False, render_project=None):
    credential_file = os.environ.get("ONBOARDRR_BAO_AGENT_TOKEN_FILE")
    if not credential_file and not ensure:
        return None
    url = os.environ.get("ONBOARDRR_BAO_URL", "").rstrip("/")
    mount = os.environ.get("ONBOARDRR_BAO_MOUNT", "secret")
    layout = deployment_layout()
    auth_mount = os.environ.get("ONBOARDRR_BAO_AGENT_AUTH_MOUNT", "approle")
    if not PATH_SEGMENT.fullmatch(auth_mount) or not HOST_LABEL.fullmatch(device):
        raise ValueError("Use a valid host name and OpenBao AppRole mount.")
    bao = (OpenBao(url, mount, token_file(Path(credential_file).expanduser()), layout=layout)
           if credential_file else compose_controller())
    path = f"deployments/compose-agent/{device}"
    try:
        record, version = bao.fields_with_version(path, *BOOTSTRAP_FIELDS)
    except HttpStatusError as error:
        if error.status != 404:
            raise
        if not ensure:
            raise ValueError(f"Configure the OpenBao Compose Agent bootstrap record for {device}.") from None
        try:
            record = managed_bootstrap(compose_controller() if credential_file else bao, device, path, layout=layout)
            version = None
            if require_core or render_project:
                record, version = bao.fields_with_version(path, *BOOTSTRAP_FIELDS)
        except HttpStatusError as failure:
            if failure.status in (403, 404):
                raise ValueError("Configure the shared managed Compose AppRole mount and controller policy in OpenBao.") from None
            raise
    auth_mount = record.get("auth_mount", auth_mount)
    if not isinstance(auth_mount, str) or not PATH_SEGMENT.fullmatch(auth_mount):
        raise ValueError("Use a valid OpenBao AppRole mount.")
    if not all(isinstance(record.get(key), str) for key in ("role_id", "secret_id", "projects")):
        raise ValueError("The OpenBao Compose Agent bootstrap record is incomplete.")
    role_id, secret_id = record["role_id"], record["secret_id"]
    projects = sorted({name.strip() for name in record["projects"].split(",")})
    if require_core and not (record.get("komodo_role_id") and record.get("komodo_secret_id")):
        try:
            record = provision_core_identity(compose_controller(), device, path, record, version)
        except HttpStatusError as error:
            if error.status in {403, 404}:
                raise ValueError("Configure the managed Komodo AppRole mount and controller policy in OpenBao.") from None
            raise
        _, version = bao.fields_with_version(path, *BOOTSTRAP_FIELDS)
    if render_project and render_project not in projects:
        controller = compose_controller()
        metadata = deployment_kv_path(controller.mount, f"deployments/compose/{device}", metadata=True, layout=layout)
        try:
            registered = controller.service.request("LIST", f"/v1/{metadata}", headers=controller.headers,
                                                    operation="List agent Compose projects")["data"]["keys"]
        except HttpStatusError as error:
            if error.status != 404:
                raise
            registered = []
        if render_project in registered:
            projects = sorted([*projects, render_project])
            validate_projects(projects)
            controller.patch(path, {"projects": ",".join(projects)}, version=version)
    validate_projects(projects)
    port = record.get("docker_api_port", os.environ.get("ONBOARDRR_DOCKER_API_PORT"))
    if port is not None:
        if not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
            raise ValueError("Set a valid Docker API port for the OpenBao Agent.")
        port = int(port)
    address = record.get("host_ip")
    if address is not None:
        try:
            if not isinstance(address, str):
                raise ValueError
            address = str(IPv4Address(address))
        except ValueError:
            raise ValueError("The OpenBao Agent bootstrap host_ip must be an IPv4 address.") from None
    templates = []
    legacy_templates = record.get("legacy_templates", {})
    if (not isinstance(legacy_templates, dict) or not legacy_templates.keys() <= set(projects)
            or not all(map(valid_template_source, legacy_templates.values()))):
        raise ValueError("The OpenBao Compose Agent bootstrap record has invalid legacy templates.")
    for project in projects:
        path = deployment_kv_path(mount, f"deployments/compose/{device}/{project}", layout=layout)
        open_record = '{{ with secret "' + path + '" -}}'
        content = (open_record + '{{ if sprig_hasKey .Data.data "dotenv" }}{{ .Data.data.dotenv }}'
                   r'{{ else }}{{ printf "# Managed by OpenBao Agent\n" }}{{ end }}'
                   + COMPOSE_VARIABLES + '{{- end }}')
        if project in legacy_templates:
            content = legacy_templates[project]
            if project == "config" and record.get("compose_config_variables") is True:
                content += open_record + COMPOSE_VARIABLES + '{{- end }}'
        elif project == "periphery" and record.get("periphery_cache") is True:
            path = deployment_kv_path(mount, f"deployments/periphery/{device}", layout=layout)
            content = '{{ with secret "' + path + '" }}PERIPHERY_PASSKEYS={{ toJSON .Data.data.passkey }}\n{{ end }}'
        elif project == "onboardrr" and record.get("hostname_cache") is True:
            path = f"hosts/data/{device}/HOSTNAME"
            content = '{{ with secret "' + path + '" }}HOSTNAME={{ toJSON .Data.data.value }}\n{{ end }}'
        if auth_mount == MANAGED_AUTH:
            content = content.rstrip() + '\n{{ with secret "hosts/data/common/USER" }}USER={{ toJSON .Data.data.value }}\n{{ end }}'
        templates.append(agent_template(content, f"/cache/{project}.env"))
    result = {
        **agent_credentials(url, auth_mount, role_id, secret_id, templates),
        "projects": projects,
        "url": url,
        "docker_api_port": port,
        "host_ip": address,
    }
    if legacy_templates:
        result["uses_legacy_templates"] = True
    if {"komodo_role_id", "komodo_secret_id", "komodo_common_mount", "komodo_newt_mount",
        "komodo_auth_mount", "komodo_template"}.intersection(record):
        komodo_role = record.get("komodo_role_id")
        komodo_mount = record.get("komodo_auth_mount", auth_mount)
        if not isinstance(komodo_mount, str) or not PATH_SEGMENT.fullmatch(komodo_mount):
            raise ValueError("Use a valid OpenBao AppRole mount for the Komodo Agent.")
        if not komodo_role or (komodo_mount, komodo_role) == (auth_mount, role_id):
            raise ValueError("Configure a separate AppRole for the Komodo OpenBao Agent.")
        contents = KOMODO_SECRETS
        if "komodo_newt_mount" in record:
            if record["komodo_newt_mount"] != "services":
                raise ValueError("Set the Komodo Newt mount to services.")
            contents = contents.replace("[secrets]", KOMODO_NEWT_SECRETS + "[secrets]", 1)
        contents += '\n{{ with secret "hosts/data/common/USER" -}}\nUSER = {{ toJSON .Data.data.value }}\n{{ end }}'
        if layout == "split":
            contents = contents.replace('sprig_omit $variables "USER"', 'sprig_omit $variables "USER" "STORAGE_IP"')
            contents += '\n{{ with secret "services/data/storage/IP" -}}\nSTORAGE_IP = {{ toJSON .Data.data.value }}\n{{ end }}'
        if "komodo_common_mount" in record:
            common_mount = record["komodo_common_mount"]
            if not isinstance(common_mount, str) or not PATH_SEGMENT.fullmatch(common_mount):
                raise ValueError("Set a valid OpenBao KV mount for Komodo's common configuration.")
            for key in (("ICHIGO_IP",) if layout == "split" else ("STORAGE_IP", "ICHIGO_IP")):
                contents += (
                    '\n{{ with secret "' + f'{common_mount}/data/config/common/{key}' + '" -}}\n'
                    '{{ if ne (printf "%T" .Data.data.value) "string" }}'
                    '{{ parseJSON "expected a string for the Komodo address" }}{{ end -}}\n'
                    + key + ' = {{ toJSON .Data.data.value }}\n{{ end }}'
                )
        if "komodo_template" in record:
            contents = record["komodo_template"]
            if not valid_template_source(contents):
                raise ValueError("The OpenBao Compose Agent bootstrap record has an invalid Komodo template.")
        result["komodo"] = agent_credentials(
            url, komodo_mount, komodo_role, record.get("komodo_secret_id"),
            [agent_template(contents, "/rendered/core.config.toml")],
        )
        if "komodo_template" in record:
            result["komodo"]["uses_legacy_template"] = True
    return result
