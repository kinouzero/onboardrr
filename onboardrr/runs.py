"""Track provisioning runs and resource observations in SQLite."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from .inventory import NAS_PROFILE
from .storage import connect


STEPS = (
    "ssh",
    "komodo-registration",
    "goku-files",
    "linux",
    "periphery",
    "komodo-stacks",
    "pangolin",
    "verify",
)
NAS_STEPS = ("ssh", "nas-prepare", *STEPS[1:])

REFERENCE = re.compile(r"[-A-Za-z0-9_+./:@]{1,512}")


class ResourceKind(StrEnum):
    SSH_KEY = "ssh-key"
    GOKU_FILE = "goku-file"
    GOKU_DIRECTORY = "goku-directory"
    HOST_FILE = "host-file"
    MOUNT = "mount"
    PACKAGE = "package"
    DOCKER_NETWORK = "docker-network"
    KOMODO_SERVER = "komodo-server"
    KOMODO_STACK = "komodo-stack"
    KOMODO_VARIABLE = "komodo-variable"
    NEWT_CREDENTIALS = "newt-credentials"
    PANGOLIN_SITE = "pangolin-site"
    PANGOLIN_PRIVATE_ROUTE = "pangolin-private-route"


class Ownership(StrEnum):
    CREATED = "created"
    REUSED = "reused"
    MODIFIED = "modified"


class Failure(StrEnum):
    STEP_FAILED = "step-failed"
    SSH_KEY_INSTALL = "ssh-key-install"
    SSH_KEY_VERIFY = "ssh-key-verify"
    KOMODO_REGISTER = "komodo-register"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class Resource:
    kind: ResourceKind
    reference: str
    ownership: Ownership
    backup_id: UUID | None = None
    content_sha256: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, ResourceKind) or not isinstance(self.ownership, Ownership):
            raise ValueError("Use a supported resource kind and ownership.")
        if (
            not isinstance(self.reference, str)
            or not REFERENCE.fullmatch(self.reference)
            or "://" in self.reference
        ):
            raise ValueError("Use a non-secret resource name, path, or identifier.")
        if self.ownership is Ownership.MODIFIED:
            if not isinstance(self.backup_id, UUID):
                raise ValueError("A modified resource requires a backup ID.")
        elif self.backup_id is not None:
            raise ValueError("Only a modified resource can have a backup ID.")
        if self.content_sha256 is not None and (
            self.kind != ResourceKind.GOKU_FILE or self.ownership != Ownership.CREATED
            or not isinstance(self.content_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256)
        ):
            raise ValueError("A content digest must describe a created Goku file.")


def timestamp():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def device_id(connection, name):
    row = connection.execute(
        "SELECT id FROM devices WHERE name = ?", (name.lower(),)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown device. Register it with onboardrr add.")
    return row["id"]


def current_step(connection, run_id):
    return connection.execute(
        """
        SELECT name, last_failed_at FROM run_steps
        WHERE run_id = ? AND completed_at IS NULL
        ORDER BY position LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def run_summary(row, step):
    result = dict(row)
    if step is None:
        return {**result, "status": "completed", "next_step": None}
    if row["finished_at"] is not None:
        return {**result, "status": "superseded", "next_step": None}
    status = "failed" if step["last_failed_at"] else "pending"
    return {**result, "status": status, "next_step": step["name"]}


def require_current_step(connection, run_id, step):
    run = connection.execute("SELECT finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    current = current_step(connection, run_id) if run and run["finished_at"] is None else None
    if current is None or current["name"] != step:
        raise ValueError("The step is no longer pending for this run.")


def create_run(connection, identifier, profile):
    steps = NAS_STEPS if profile == NAS_PROFILE else STEPS
    run_id = str(uuid4())
    connection.execute(
        "INSERT INTO runs (id, device_id, opened_at) VALUES (?, ?, ?)",
        (run_id, identifier, timestamp()),
    )
    connection.executemany(
        "INSERT INTO run_steps (run_id, position, name) VALUES (?, ?, ?)",
        ((run_id, position, step) for position, step in enumerate(steps)),
    )
    return connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def change_run_workflow(connection, identifier, profile):
    previous = connection.execute(
        "SELECT id FROM runs WHERE device_id = ? ORDER BY opened_at DESC, id DESC LIMIT 1", (identifier,),
    ).fetchone()
    if previous is None:
        return
    connection.execute(
        "UPDATE runs SET finished_at = ? WHERE device_id = ? AND finished_at IS NULL", (timestamp(), identifier),
    )
    create_run(connection, identifier, profile)


class RunStore:
    def __init__(self, database):
        self.database = database

    def open(self, name):
        with connect(self.database, write=True) as connection:
            identifier = device_id(connection, name)
            row = connection.execute(
                "SELECT * FROM runs WHERE device_id = ? AND finished_at IS NULL",
                (identifier,),
            ).fetchone()
            if row is None:
                profile = connection.execute("SELECT profile FROM devices WHERE id = ?", (identifier,)).fetchone()["profile"]
                row = create_run(connection, identifier, profile)
            return run_summary(row, current_step(connection, row["id"]))

    def list(self, name):
        with connect(self.database) as connection:
            rows = connection.execute(
                """
                SELECT runs.*, step.name AS next_step, step.last_failed_at
                FROM runs
                LEFT JOIN run_steps AS step
                    ON step.run_id = runs.id AND step.position = (
                        SELECT MIN(position) FROM run_steps
                        WHERE run_id = runs.id AND completed_at IS NULL
                    )
                WHERE device_id = ?
                ORDER BY opened_at DESC, id DESC
                """,
                (device_id(connection, name),),
            ).fetchall()
            results = []
            for row in rows:
                fields = dict(row)
                next_step = fields.pop("next_step")
                failed_at = fields.pop("last_failed_at")
                step = {"name": next_step, "last_failed_at": failed_at} if next_step else None
                results.append(run_summary(fields, step))
            return results

    def show(self, run_id):
        with connect(self.database) as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown run.")
            steps = connection.execute(
                """
                SELECT name, completed_at, last_failed_at, last_failure FROM run_steps
                WHERE run_id = ? ORDER BY position
                """,
                (run_id,),
            ).fetchall()
            resources = connection.execute(
                """
                SELECT step, kind, reference, ownership, backup_id, content_sha256
                FROM run_resources AS resource
                JOIN run_steps AS checkpoint
                    ON checkpoint.run_id = resource.run_id AND checkpoint.name = resource.step
                WHERE resource.run_id = ? AND retired_at IS NULL
                ORDER BY checkpoint.position, kind, reference
                """,
                (run_id,),
            ).fetchall()
            return {
                **run_summary(row, current_step(connection, run_id)),
                "steps": [dict(step) for step in steps],
                "resources": [{key: value for key, value in dict(resource).items()
                               if key != "content_sha256" or value is not None} for resource in resources],
            }

    def record_resource(self, run_id, step, resource):
        with connect(self.database, write=True) as connection:
            require_current_step(connection, run_id, step)
            backup_id = str(resource.backup_id) if resource.backup_id is not None else None
            connection.execute(
                """
                INSERT INTO run_resources (run_id, step, kind, reference, ownership, backup_id, content_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, kind, reference) DO NOTHING
                """,
                (run_id, step, resource.kind, resource.reference, resource.ownership, backup_id,
                 resource.content_sha256),
            )
            row = connection.execute(
                """
                SELECT ownership, backup_id, content_sha256 FROM run_resources
                WHERE run_id = ? AND kind = ? AND reference = ?
                """,
                (run_id, resource.kind, resource.reference),
            ).fetchone()
            if tuple(row) != (resource.ownership, backup_id, resource.content_sha256):
                raise ValueError("Resource ownership already recorded with different metadata.")

    def component_completed(self, run_id, component):
        with connect(self.database) as connection:
            return connection.execute(
                "SELECT 1 FROM automatic_components WHERE run_id = ? AND component = ?", (run_id, component),
            ).fetchone() is not None

    def complete_component(self, run_id, component):
        with connect(self.database, write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO automatic_components VALUES (?, ?)", (run_id, component))

    def unregister_komodo(self, identifier, servers):
        with connect(self.database, write=True) as connection:
            changed = 0
            for server in servers:
                changed += connection.execute(
                    """UPDATE run_resources SET retired_at = ?
                    WHERE kind = 'komodo-server' AND reference = ? AND retired_at IS NULL
                    AND run_id IN (SELECT id FROM runs WHERE device_id = ?)""",
                    (timestamp(), server, identifier),
                ).rowcount
            if changed:
                run = connection.execute(
                    "SELECT id FROM runs WHERE device_id = ? ORDER BY rowid DESC LIMIT 1", (identifier,),
                ).fetchone()
                connection.execute("UPDATE runs SET finished_at = NULL WHERE id = ?", (run["id"],))
                connection.execute(
                    """UPDATE run_steps SET completed_at = NULL, last_failed_at = NULL, last_failure = NULL
                    WHERE run_id = ? AND name = 'komodo-registration'""", (run["id"],),
                )

    def mark_failed(self, run_id, step, reason=Failure.STEP_FAILED):
        if not isinstance(reason, Failure):
            raise ValueError("Use a supported, non-secret failure code.")
        with connect(self.database, write=True) as connection:
            require_current_step(connection, run_id, step)
            connection.execute(
                "UPDATE run_steps SET last_failed_at = ?, last_failure = ? WHERE run_id = ? AND name = ?",
                (timestamp(), reason, run_id, step),
            )

    def complete_step(self, run_id, step):
        with connect(self.database, write=True) as connection:
            require_current_step(connection, run_id, step)
            completed_at = timestamp()
            connection.execute(
                "UPDATE run_steps SET completed_at = ? WHERE run_id = ? AND name = ?",
                (completed_at, run_id, step),
            )
            if current_step(connection, run_id) is None:
                connection.execute(
                    "UPDATE runs SET finished_at = ? WHERE id = ?", (completed_at, run_id)
                )
