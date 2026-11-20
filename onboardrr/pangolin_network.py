"""Reconcile private host routes between the organization's Newt sites."""

import socket
from contextlib import nullcontext
from ipaddress import IPv4Address

from .host_resolution import resolve_addresses
from .inventory import HOST_LABEL
from .secrets import stored_host_ipv4
from .storage import provisioning_lock


def destination_ipv4(device, ipv4):
    if ipv4 is not None:
        return str(IPv4Address(ipv4))
    host = device["host"]
    try:
        return str(IPv4Address(host))
    except ValueError:
        pass
    addresses = resolve_addresses(host, socket.AF_INET)
    if not addresses:
        stored = stored_host_ipv4(device["name"])
        if stored is not None:
            return stored
    if len(addresses) != 1:
        raise ValueError("Provide --ipv4 when the device does not resolve to exactly one IPv4 address.")
    return str(addresses.pop())


def list_records(api, route, field, id_field):
    records = {}
    page = 1
    while True:
        data = api.request("GET", f"{route}?page={page}&pageSize=100", f"list {route}")
        batch = data.get(field)
        pagination = data.get("pagination")
        if not isinstance(batch, list) or not isinstance(pagination, dict):
            raise ValueError(f"Pangolin returned an invalid {route} page.")
        total, size = pagination.get("total"), pagination.get("pageSize")
        if (type(total) is not int or total < 0 or type(size) is not int or size < 1
                or pagination.get("page") != page or len(batch) != min(size, max(0, total - len(records)))):
            raise ValueError(f"Pangolin returned inconsistent {route} pagination.")
        for record in batch:
            identifier = record.get(id_field) if isinstance(record, dict) else None
            if type(identifier) is not int or identifier < 1 or identifier in records:
                raise ValueError(f"Pangolin returned an invalid or duplicate {route} identifier.")
            records[identifier] = record
        if len(records) == total:
            return records
        page += 1


def assigned_sites(resource):
    site_ids = resource.get("siteIds")
    if not isinstance(site_ids, list) or any(type(value) is not int or value < 1 for value in site_ids):
        raise ValueError("Pangolin returned invalid private host site assignments.")
    return set(site_ids)


def update_private_host_sites(api, identifier, resource, site_ids):
    # Pangolin 1.26 requires a resource field alongside site assignments.
    api.request_path("POST", f"/v1/private-resource/{identifier}", "update private host sites", {
        "name": resource["name"], "siteIds": sorted(site_ids),
    })


def private_route_reference(api, site_id, resource_id, destination):
    return f"{api.organization}/{site_id}/{resource_id}/{destination}"


def note_remaining_private_hosts(api, owned_routes, resources=None):
    if not owned_routes:
        return
    if resources is None:
        resources = list_records(api, "private-resources", "siteResources", "siteResourceId")
    for reference, action in owned_routes.items():
        identifier = int(reference.split("/")[2])
        if action["status"] == "pending":
            action["status"] = "preserved" if identifier in resources else "absent"


def detach_private_host_routes(api, name, site_id, expected_count, *, owned_routes=None):
    resources = list_records(api, "private-resources", "siteResources", "siteResourceId")
    attached = {}
    for identifier, resource in resources.items():
        sites = assigned_sites(resource)
        if site_id in sites:
            attached[identifier] = sites
    if len(attached) != expected_count:
        return False
    deletions = {}
    for identifier, sites in attached.items():
        resource = resources[identifier]
        label, alias = resource.get("name"), resource.get("alias")
        if (resource.get("mode") != "host" or not isinstance(label, str)
                or not HOST_LABEL.fullmatch(label.casefold()) or not isinstance(alias, str)
                or alias.casefold() != f"{label.casefold()}.pang"):
            return False
        if sites == {site_id}:
            reference = private_route_reference(api, site_id, identifier, resource.get("destination"))
            if not owned_routes or reference not in owned_routes or label != name or alias != f"{name}.pang":
                return False
            deletions[identifier] = reference
    for identifier, sites in attached.items():
        if identifier in deletions:
            result = api.service.request(
                "DELETE", f"/v1/private-resource/{identifier}", headers=api.headers, operation="retire owned private host",
            )
            if result.get("success") is not True:
                raise ValueError("Pangolin did not confirm removal of the owned private host.")
        else:
            update_private_host_sites(api, identifier, resources[identifier], sites - {site_id})
    confirmed = list_records(api, "private-resources", "siteResources", "siteResourceId")
    for identifier, sites in attached.items():
        if identifier in deletions:
            if identifier in confirmed:
                raise ValueError("The owned private host is still present in Pangolin.")
            owned_routes[deletions[identifier]]["status"] = "removed"
        elif identifier not in confirmed or assigned_sites(confirmed[identifier]) != sites - {site_id}:
            raise ValueError("Pangolin did not confirm detachment from the shared private host routes.")
    note_remaining_private_hosts(api, owned_routes, confirmed)
    return True


def host_routes(resources, site_names, name, destination):
    alias = f"{name}.pang"
    target = None
    peers = {}
    for identifier, resource in resources.items():
        resource_alias = resource.get("alias")
        resource_name = resource.get("name")
        normalized_alias = resource_alias.casefold() if isinstance(resource_alias, str) else None
        label = resource_name.casefold() if isinstance(resource_name, str) else None
        if normalized_alias == alias:
            if (target is not None or resource.get("mode") != "host" or label != name
                    or resource.get("destination") != destination or resource.get("enabled") is not True):
                raise ValueError(f"The Pangolin alias {alias} is already in use by a conflicting resource.")
            target = identifier
        elif not (resource.get("mode") == "host" and label in site_names and normalized_alias == f"{label}.pang"):
            continue
        peers[identifier] = assigned_sites(resource)
    return target, peers


def configure_private_network(api, device, site, *, database, ipv4=None, apply=False, record_created=None):
    name = device["name"]
    site_id = site["siteId"]
    destination = destination_ipv4(device, ipv4)
    alias = f"{name}.pang"
    lock = provisioning_lock(database, f"pangolin-{api.organization}") if apply else nullcontext()
    with lock:
        sites = list_records(api, "sites", "sites", "siteId")
        newt_sites = {identifier: item for identifier, item in sites.items() if item.get("type") == "newt"}
        if site_id not in newt_sites:
            raise ValueError("The device must have a Newt site in this Pangolin organization.")
        site_names = {
            label.casefold()
            for item in newt_sites.values()
            for label in (item.get("name"), item.get("niceId"))
            if isinstance(label, str) and HOST_LABEL.fullmatch(label.casefold())
        }
        resources = list_records(api, "private-resources", "siteResources", "siteResourceId")
        target, routes = host_routes(resources, site_names, name, destination)
        peer_ids = [identifier for identifier, ids in routes.items() if identifier != target and site_id not in ids]
        required_sites = set(newt_sites) | routes.get(target, set())
        if apply:
            created_id = None
            if target is None:
                created = api.request("PUT", "private-resource", "create private host resource", {
                    "name": name, "mode": "host", "alias": alias, "destination": destination,
                    "siteIds": sorted(required_sites), "roleIds": [], "userIds": [], "clientIds": [],
                    "tcpPortRangeString": "*", "udpPortRangeString": "*",
                })
                created_id = created.get("siteResourceId")
                if type(created_id) is not int or created_id < 1:
                    raise ValueError("Pangolin did not return a valid private host identifier.")
                if record_created is not None:
                    record_created(private_route_reference(api, site_id, created_id, destination))
            updates = {identifier: routes[identifier] | {site_id} for identifier in peer_ids}
            if target is not None and required_sites != routes[target]:
                updates[target] = required_sites
            for identifier, ids in updates.items():
                update_private_host_sites(api, identifier, resources[identifier], ids)
            if target is None or updates:
                resources = list_records(api, "private-resources", "siteResources", "siteResourceId")
                confirmed, current = host_routes(resources, site_names, name, destination)
                if (confirmed != (target if target is not None else created_id)
                        or not required_sites <= current[confirmed]
                        or any(not ids <= current.get(identifier, set()) for identifier, ids in updates.items())):
                    raise ValueError("Pangolin did not confirm the private host site assignments.")
        return {
            "alias": alias, "destination": destination, "site_ids": sorted(required_sites),
            "action": "create" if target is None else ("update" if required_sites != routes[target] else "reuse"),
            "peer_resources_to_update": peer_ids,
        }
