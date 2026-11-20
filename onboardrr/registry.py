"""Load Docker Hub credentials supplied to device preparation."""

import re

from .http_json import HttpStatusError
from .secrets import configured_openbao


def docker_hub_credentials():
    bao = configured_openbao(mount="services")
    if bao is None:
        return None
    values = []
    for key in ("USERNAME", "TOKEN"):
        try:
            value, = bao.read(f"dockerhub/{key}", "value")
        except HttpStatusError as error:
            if error.status != 404:
                raise
            value = None
        values.append(value)
    username, token = values
    if username is None and token is None:
        return None
    if (not username or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", username)
            or not token or len(token) > 8192 or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ValueError("Configure valid Docker Hub USERNAME and TOKEN values in OpenBao.")
    return {"username": username, "token": token}
