"""Maintain DNS servers advertised by the operator router over DHCPv4."""

import hashlib
import hmac
import ipaddress
import json
import os
import ssl
import sys
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .http_json import MAX_RESPONSE
from .secrets import token_file


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Freebox:
    APP_ID = "io.github.kinouzero.onboardrr"
    DEFAULT_URL = "https://mafreebox.freebox.fr"

    def __init__(self, origin, state):
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("Use a Freebox HTTPS origin without credentials or a path.")
        self.origin = origin.rstrip("/")
        tls = ssl.create_default_context(cafile=Path(__file__).with_name("freebox-ca.pem"))
        # Freebox's vendor CA predates OpenSSL's strict extension checks.
        tls.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=tls))
        identity = self.request("/api_version")
        uid = identity.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ValueError("Freebox OS did not report a router identifier.")
        key = hashlib.sha256(f"{self.origin.casefold()}\0{uid}".encode()).hexdigest()
        self.credential = state / f"freebox-{key}.token"

    def request(self, path, method="GET", *, payload=None, session=None):
        headers = {"Content-Type": "application/json"}
        if session:
            headers["X-Fbx-App-Auth"] = session
        req = Request(self.origin + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError
            data = json.loads(body)
        except (OSError, URLError, UnicodeError, ValueError):
            raise ValueError("Freebox OS did not return a usable response over verified HTTPS.") from None
        if not isinstance(data, dict):
            raise ValueError("Freebox OS returned an invalid response.")
        if path == "/api_version":
            return data
        if data.get("error_code") == "insufficient_rights":
            raise ValueError("Enable the settings permission for Onboardrr in Freebox OS.")
        if data.get("error_code") in {"auth_required", "invalid_token", "invalid_app"}:
            raise ValueError("Freebox OS rejected the session. Pair Onboardrr with the router again.")
        if data.get("success") is not True:
            raise ValueError(f"Freebox OS refused {method} {path}.")
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("Freebox OS returned an invalid result.")
        return result

    def pair(self):
        authorization = self.request("/api/latest/login/authorize/", "POST", payload={
            "app_id": self.APP_ID, "app_name": "Onboardrr", "app_version": "1.0", "device_name": "Onboardrr",
        })
        track = authorization.get("track_id")
        token = authorization.get("app_token")
        if type(track) is not int or track < 0 or not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(
            not 33 <= ord(character) <= 126 for character in token
        ):
            raise ValueError("Freebox OS returned invalid pairing data.")
        print("Approve Onboardrr on the Freebox display, then enable its settings permission in Freebox OS.",
              file=sys.stderr, flush=True)
        deadline = monotonic() + 125
        while monotonic() < deadline:
            status = self.request(f"/api/latest/login/authorize/{track}").get("status")
            if status == "granted":
                self.credential.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.credential.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(token.encode("ascii") + b"\n")
                        stream.flush()
                        os.replace(temporary, self.credential)
                    finally:
                        temporary.unlink(missing_ok=True)
                return {"status": "paired", "provider": "freebox", "url": self.origin}
            if status != "pending":
                break
            sleep(2)
        raise ValueError("Freebox pairing did not complete. Try again and confirm on the router display.")

    @contextmanager
    def session(self):
        try:
            token = token_file(self.credential)
        except FileNotFoundError:
            raise ValueError("Pair Onboardrr with this Freebox before accessing DHCP.") from None
        challenge = self.request("/api/latest/login/").get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise ValueError("Freebox OS did not send a login challenge.")
        proof = hmac.new(token.encode(), challenge.encode(), hashlib.sha1).hexdigest()
        result = self.request("/api/latest/login/session/", "POST", payload={
            "app_id": self.APP_ID, "password": proof,
        })
        session = result.get("session_token")
        if not isinstance(session, str) or not session or any(not 33 <= ord(c) <= 126 for c in session):
            raise ValueError("Freebox OS returned an invalid session.")
        try:
            yield session
        finally:
            with suppress(ValueError):
                self.request("/api/latest/login/logout/", "POST", payload={}, session=session)

    def configure_dns(self, desired, apply):
        path = "/api/latest/dhcp/config/"
        with self.session() as session:
            before = self.request(path, session=session)
            slots = before.get("dns")
            if not isinstance(slots, list) or not slots or not all(isinstance(slot, str) for slot in slots):
                raise ValueError("Freebox OS did not report the DHCP IPv4 DNS slots.")
            current = [server for server in slots if server]
            proposed = current if desired is None else desired
            if len(proposed) > len(slots):
                raise ValueError(f"This Freebox supports up to {len(slots)} DNS servers.")
            changed = proposed != current
            if apply and changed:
                values = proposed + [""] * (len(slots) - len(proposed))
                self.request(path, "PUT", payload={"dns": values}, session=session)
                after = self.request(path, session=session)
                if after.get("dns") != values:
                    raise ValueError("The Freebox did not confirm the requested DNS values.")
                if {k: v for k, v in before.items() if k != "dns"} != {
                    k: v for k, v in after.items() if k != "dns"
                }:
                    raise ValueError("Other DHCP settings changed during the update. Review Freebox OS.")
        return {"status": "applied" if apply and changed else "unchanged" if apply else "preview",
                "provider": "freebox", "url": self.origin, "current_dns": current, "proposed_dns": proposed}


def dns_servers(argument):
    try:
        addresses = [ipaddress.IPv4Address(value.strip()) for value in argument.split(",")]
    except ipaddress.AddressValueError:
        raise ValueError("Enter comma-separated IPv4 DNS addresses.") from None
    if len(set(addresses)) != len(addresses) or any(
        address.is_unspecified or address.is_multicast or address.is_loopback or int(address) == 0xFFFFFFFF
        for address in addresses
    ):
        raise ValueError("Enter distinct unicast IPv4 addresses reachable by LAN clients.")
    return [str(address) for address in addresses]


def maintain(database, *, servers=None, apply=False, pair=False):
    desired = dns_servers(servers) if servers is not None else None
    if apply and desired is None:
        raise ValueError("Specify the DNS addresses before applying.")
    provider = os.environ.get("ONBOARDRR_ROUTER_PROVIDER", "freebox").strip().lower()
    if provider != "freebox":
        raise ValueError("Unsupported router provider. Onboardrr currently supports Freebox OS.")
    router = Freebox(os.environ.get("ONBOARDRR_ROUTER_URL", Freebox.DEFAULT_URL), database.parent / "routers")
    if pair:
        return router.pair()
    return router.configure_dns(desired, apply)
