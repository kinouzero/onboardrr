"""Open and migrate the controller's local SQLite state."""

import fcntl
import os
import sqlite3
from contextlib import closing, contextmanager


@contextmanager
def provisioning_lock(database, scope, *, shared=False):
    path = database.with_name(f"{database.name}-{scope}.lock")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("A provisioning step is already running for this scope.") from error
        yield
    finally:
        os.close(fd)


def device_lock(database, device):
    return provisioning_lock(database, device["id"])


MIGRATIONS = (
    (
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            ssh_user TEXT NOT NULL,
            ssh_port INTEGER NOT NULL CHECK (ssh_port BETWEEN 1 AND 65535),
            profile TEXT NOT NULL,
            UNIQUE (host, ssh_port)
        )
        """,
    ),
    (
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY NOT NULL,
            device_id TEXT NOT NULL REFERENCES devices(id),
            opened_at TEXT NOT NULL,
            finished_at TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX one_open_run_per_device
        ON runs (device_id) WHERE finished_at IS NULL
        """,
        "CREATE INDEX runs_by_device ON runs (device_id, opened_at, id)",
        """
        CREATE TABLE run_steps (
            run_id TEXT NOT NULL REFERENCES runs(id),
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            completed_at TEXT,
            last_failed_at TEXT,
            PRIMARY KEY (run_id, name),
            UNIQUE (run_id, position)
        )
        """,
        """
        CREATE TABLE run_resources (
            run_id TEXT NOT NULL,
            step TEXT NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL,
            ownership TEXT NOT NULL CHECK (ownership IN ('created', 'reused', 'modified')),
            backup_id TEXT,
            FOREIGN KEY (run_id, step) REFERENCES run_steps(run_id, name),
            PRIMARY KEY (run_id, kind, reference),
            CHECK ((ownership = 'modified') = (backup_id IS NOT NULL))
        )
        """,
    ),
    ("ALTER TABLE run_steps ADD COLUMN last_failure TEXT",),
    ("ALTER TABLE run_resources ADD COLUMN content_sha256 TEXT",),
    (
        """
        CREATE TABLE monitoring_resources (
            device_id TEXT PRIMARY KEY REFERENCES devices(id),
            registration TEXT NOT NULL
        )
        """,
    ),
    ("ALTER TABLE run_resources ADD COLUMN retired_at TEXT",),
    (
        """
        CREATE TABLE automatic_components (
            run_id TEXT NOT NULL REFERENCES runs(id),
            component TEXT NOT NULL,
            PRIMARY KEY (run_id, component)
        )
        """,
    ),
    (
        """CREATE TEMP TABLE nas_upgrade AS
           SELECT runs.id FROM runs JOIN devices ON devices.id = runs.device_id
           WHERE devices.profile = 'nas'
           AND NOT EXISTS (SELECT 1 FROM run_steps WHERE run_id = runs.id AND name = 'komodo-registration')
           AND (runs.finished_at IS NULL OR NOT EXISTS (
               SELECT 1 FROM run_steps WHERE run_id = runs.id AND completed_at IS NULL
           ))
           AND NOT EXISTS (
               SELECT 1 FROM runs newer WHERE newer.device_id = runs.device_id
               AND (newer.opened_at, newer.id) > (runs.opened_at, runs.id)
           )""",
        """UPDATE run_steps SET position = 8, completed_at = NULL, last_failed_at = NULL, last_failure = NULL
           WHERE name = 'verify' AND run_id IN (SELECT id FROM nas_upgrade)""",
        """INSERT INTO run_steps (run_id, position, name)
           SELECT nas_upgrade.id, step.position, step.name FROM nas_upgrade CROSS JOIN (
               SELECT 2 AS position, 'komodo-registration' AS name UNION ALL
               SELECT 3, 'goku-files' UNION ALL SELECT 4, 'linux' UNION ALL SELECT 5, 'periphery' UNION ALL
               SELECT 6, 'komodo-stacks' UNION ALL SELECT 7, 'pangolin'
           ) step""",
        "UPDATE runs SET finished_at = NULL WHERE id IN (SELECT id FROM nas_upgrade)",
        "DROP TABLE nas_upgrade",
    ),
    (
        """CREATE TABLE primary_storage (
           state TEXT PRIMARY KEY NOT NULL CHECK (state IN ('active', 'pending')),
           device_id TEXT NOT NULL REFERENCES devices(id),
           ipv4 TEXT NOT NULL
        )""",
    ),
)


def migrate(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= len(MIGRATIONS):
        raise ValueError("Unsupported Onboardrr database schema version.")

    for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {number}")


@contextmanager
def connect(database, *, write=False):
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                migrate(connection)

        with connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
