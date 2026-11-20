"""Request JSON from an explicitly configured HTTPS origin."""

import http.client
import json
from urllib.parse import urlsplit


MAX_RESPONSE = 4 * 1024 * 1024


class HttpStatusError(ValueError):
    def __init__(self, service, operation, status):
        self.status = status
        super().__init__(f"{service} {operation} failed with HTTP {status}.")


class JsonService:
    def __init__(self, name, url):
        origin = urlsplit(url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            raise ValueError(f"Use a {name} HTTPS origin without credentials or a path.")
        self.name = name
        self.host = origin.hostname
        self.port = origin.port

    def request(self, method, path, *, headers, operation, data=None):
        if any(any(not 32 <= ord(character) <= 126 for character in value) for value in headers.values()):
            raise ValueError(f"{self.name} authentication header has an invalid format.")
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=10)
        try:
            body = json.dumps(data) if data is not None else None
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise HttpStatusError(self.name, operation, response.status)
            content = response.read(MAX_RESPONSE + 1)
            if response.status == 204 and not content:
                return None
            if len(content) > MAX_RESPONSE:
                raise ValueError(f"{self.name} {operation} returned too much data.")
            try:
                return json.loads(content)
            except ValueError as error:
                raise ValueError(f"{self.name} {operation} returned invalid JSON.") from error
        except (OSError, http.client.HTTPException) as error:
            raise ValueError(f"{self.name} {operation} could not complete over HTTPS.") from error
        finally:
            connection.close()
