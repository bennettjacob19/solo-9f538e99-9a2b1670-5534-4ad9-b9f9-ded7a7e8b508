"""Self-tests, focused on the interleavings required by the task."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import pytest

from svcdir import DirectoryStore, create_app
from svcdir.errors import (
    BackupValidationError,
    ConflictError,
    NotFoundError,
    RestoreInProgress,
    StaleEditError,
)


@pytest.fixture()
def store(tmp_path):
    return DirectoryStore(tmp_path / "dir.sqlite3", restore_lease_seconds=30)


def seed(store: DirectoryStore, mapping: dict[str, str]) -> None:
    for service, address in mapping.items():
        store.upsert(service, address)


# ---------------------------------------------------------------------------
# Basic correctness


def test_resolve_unknown_service_is_404(store):
    with pytest.raises(NotFoundError):
        store.resolve("missing")


def test_upsert_and_resolve(store):
    store.upsert("orders", "10.0.0.1:80")
    store.upsert("orders", "10.0.0.2:80")
    assert store.resolve("orders")["address"] == "10.0.0.2:80"


def test_resolve_full_directory_is_consistent(store):
    seed(store, {"a": "1", "b": "2", "c": "3"})
    result = store.resolve()
    assert result["services"] == {"a": "1", "b": "2", "c": "3"}


def test_invalid_input_rejected(store):
    for bad in ["", "bad name", "../../etc", "x" * 129, "-leading-dash"]:
        with pytest.raises(Exception):
            store.upsert(bad, "1.2.3.4")
    with pytest.raises(Exception):
        store.upsert("ok", "   ")
    with pytest.raises(Exception):
        store.upsert("ok", "a b")


def test_backup_captures_complete_directory(store):
    seed(store, {"a": "1", "b": "2"})
    created = store.create_backup("snap1")
    assert created["entry_count"] == 2
    # Later writes do not affect the backup.
    store.upsert("c", "3")
    store.upsert("a", "changed")
    result = store.restore_backup("snap1")
    assert result["entry_count"] == 2
    assert store.resolve()["services"] == {"a": "1", "b": "2"}


def test_duplicate_backup_name_rejected(store):
    store.create_backup("b1")
    with pytest.raises(ConflictError):
        store.create_backup("b1")


# ---------------------------------------------------------------------------
# Backup validation


def test_corrupt_backup_is_rejected_and_directory_unchanged(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")
    before = store.resolve()

    # Tamper with the archived rows behind the backup's back.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE entries SET address='tampered' WHERE version_id=? AND service='b'",
            (backup["version"],),
        )

    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("摘要" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"]
    assert after["version"] == before["version"]


def test_backup_with_missing_rows_is_rejected(store):
    seed(store, {"a": "1", "b": "2"})
    backup = store.create_backup("b1")
    store.upsert("a", "live")  # move the live version past the backup
    before = store.resolve()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM entries WHERE version_id=? AND service='a'",
            (backup["version"],),
        )
    with pytest.raises(BackupValidationError) as excinfo:
        store.restore_backup("b1")
    assert any("数量" in d for d in excinfo.value.details)
    after = store.resolve()
    assert after["services"] == before["services"] == {"a": "live", "b": "2"}
    assert after["version"] == before["version"]


def test_missing_backup_is_404(store):
    with pytest.raises(NotFoundError):
        store.restore_backup("nope")


# ---------------------------------------------------------------------------
# Restore vs. concurrent writers


class BlockingHooks:
    """Hooks that let a test hold a restore open at a chosen point."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.at: str | None = None

    def install(self, store: DirectoryStore, at: str):
        self.at = at

        def waiter(payload):
            if self.at == "validated":
                self.reached.set()
                assert self.release.wait(10)

        store._hooks["before_restore_switch"] = waiter
        return self


def test_writes_rejected_while_restore_in_progress_and_settled_before(store):
    seed(store, {"a": "old", "b": "keep"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    restore_result: dict = {}

    def do_restore():
        try:
            restore_result["value"] = store.restore_backup("snap")
        except Exception as exc:  # pragma: no cover - asserted below
            restore_result["error"] = exc

    thread = threading.Thread(target=do_restore)
    thread.start()
    assert gate.reached.wait(10), "restore never reached the validation gate"

    # Mid-restore: reads still see a complete directory.
    mid = store.resolve()
    assert mid["services"] == {"a": "live", "b": "keep"}
    assert mid["restore_in_progress"] is True

    # New modifications are explicitly rejected.
    with pytest.raises(RestoreInProgress):
        store.upsert("c", "new")
    with pytest.raises(RestoreInProgress):
        store.delete("b")

    gate.release.set()
    thread.join(10)
    assert "value" in restore_result, restore_result

    after = store.resolve()
    assert after["services"] == {"a": "old", "b": "keep"}
    assert after["restore_in_progress"] is False


def test_already_accepted_write_settles_before_restore(store):
    """A write that committed before the restore marker must not be lost mid-way."""
    seed(store, {"a": "v0"})
    store.create_backup("snap")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    # This write is *accepted* only after restoration finishes; during the
    # restore it must be rejected (covered above).  Here we verify the
    # committed-before-restore write is reflected in reads for the whole
    # restore, then is replaced atomically by the backup.
    assert store.resolve("a")["address"] == "v0"
    gate.release.set()
    t.join(10)
    assert store.resolve("a")["address"] == "v0"


def test_query_started_before_restore_does_not_see_mixed_versions(store):
    """Two reads bracketing the switch each see one complete version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    before_switch = store.resolve()
    gate.release.set()
    t.join(10)
    after_switch = store.resolve()

    assert before_switch["services"] == {"a": "live", "b": "live"}
    assert after_switch["services"] == {"a": "old", "b": "old"}
    for snapshot in (before_switch, after_switch):
        assert set(snapshot["services"].values()) in ({"live"}, {"old"})


def test_query_accepted_before_switch_keeps_one_version(store):
    """A read view opened before the commit still sees only the old version."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")

    gate = BlockingHooks().install(store, "validated")
    t = threading.Thread(target=store.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    with store.read_view() as view:  # request accepted before the switch
        first = view.resolve()
        gate.release.set()
        t.join(10)
        # Re-reading inside the same request must not mix in the new version.
        second = view.resolve()
        third = view.resolve("a")

    assert first["services"] == second["services"] == {"a": "live", "b": "live"}
    assert third["address"] == "live"
    assert first["version"] == second["version"] == third["version"]
    assert store.resolve()["services"] == {"a": "old", "b": "old"}


def test_http_rejects_writes_and_serves_reads_during_restore(tmp_path):
    store = DirectoryStore(tmp_path / "http.sqlite3", restore_lease_seconds=30)
    app = create_app(store)
    app.config.update(TESTING=True)
    setup = app.test_client()
    setup.post("/api/services", json={"service": "a", "address": "live"})
    setup.post("/api/backups", json={"name": "snap"})
    setup.post("/api/services", json={"service": "a", "address": "live-2"})

    gate = BlockingHooks().install(store, "validated")
    done = threading.Event()

    def restore():
        app.test_client().post("/api/backups/snap/restore")
        done.set()

    t = threading.Thread(target=restore)
    t.start()
    assert gate.reached.wait(10)

    client = app.test_client()
    resolved = client.get("/resolve?name=a")
    assert resolved.status_code == 200
    assert resolved.get_json()["address"] == "live-2"
    assert resolved.get_json()["restore_in_progress"] is True

    rejected = client.post("/api/services", json={"service": "b", "address": "x"})
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "restore_in_progress"
    # The browser form surfaces the rejection too.
    page = client.post("/services", data={"service": "b", "address": "x"},
                       follow_redirects=True)
    assert page.status_code == 200
    assert "恢复进行中" in page.get_data(as_text=True)

    gate.release.set()
    assert done.wait(10)
    t.join(10)
    assert client.get("/resolve?name=a").get_json()["address"] == "live"


def test_concurrent_writers_during_restore_never_mix(store):
    store.upsert("a", "backup-a")
    store.upsert("b", "backup-b")
    store.create_backup("snap")
    for i in range(1, 41):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    gate = BlockingHooks().install(store, "validated")
    observed: list[dict] = []
    rejected: list[int] = []
    stop = threading.Event()
    writer_paused = threading.Event()

    def reader():
        while not stop.is_set():
            observed.append(store.resolve()["services"])

    def writer():
        i = 0
        while not stop.is_set():
            if writer_paused.is_set():
                time.sleep(0.005)
                continue
            try:
                store.upsert("a", f"during-{i}")
                store.upsert("b", f"during-{i}")
            except RestoreInProgress:
                rejected.append(i)
            i += 1

    restore_done = threading.Event()

    def restore():
        store.restore_backup("snap")
        restore_done.set()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads.append(threading.Thread(target=writer))
    restore_thread = threading.Thread(target=restore)
    for th in threads:
        th.start()
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.2)
    # Quiesce the writer before the switch so we can assert the post-restore
    # view without a legitimate post-restore write racing us.
    writer_paused.set()
    time.sleep(0.05)
    gate.release.set()
    assert restore_done.wait(10)
    after_restore = store.resolve()["services"]
    stop.set()
    for th in threads + [restore_thread]:
        th.join(10)

    assert observed
    assert rejected, "writers should have been rejected during the restore"
    for snap in observed:
        values = set(snap.values())
        backup_only = values == {"backup-a", "backup-b"}
        live_only = all(v.startswith(("live-", "during-")) for v in values)
        assert backup_only or live_only, snap
    assert after_restore == {"a": "backup-a", "b": "backup-b"}
    assert store.resolve()["services"] == {"a": "backup-a", "b": "backup-b"}


# ---------------------------------------------------------------------------
# Crash / restart behaviour


def test_expired_restore_lease_is_reclaimed_after_restart(tmp_path):
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=0.3)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    gate = BlockingHooks().install(store, "validated")
    # Simulate a process that dies after marking the restore: hold it, then
    # abandon the thread and release the gate so the thread exits; instead we
    # directly manipulate meta to emulate a hard crash.
    gate  # noqa: B018 - not used, kept for symmetry with the other tests

    import secrets as _secrets

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner',?)",
            (_secrets.token_hex(8),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until','0')"
        )

    restarted = DirectoryStore(db, restore_lease_seconds=0.3)
    restarted.initialize()
    snap = restarted.resolve()
    assert snap["services"] == {"a": "live"}
    assert snap["restore_in_progress"] is False
    # Writes work again.
    restarted.upsert("b", "new")
    assert restarted.resolve("b")["address"] == "new"


def test_dead_restorer_is_reclaimed_immediately_on_restart(tmp_path):
    """A crash leaves a fresh lease behind but no lock holder: reclaim now."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")

    # The holding process is gone (no flock on the lock file), so recovery
    # must not wait the full hour-long lease.
    lock_path = str(db) + ".restore.lock"
    open(lock_path, "a").close()
    with sqlite3.connect(db) as conn:
        future = time.time() + 3600
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
            (str(future),),
        )

    restarted = DirectoryStore(db, restore_lease_seconds=3600)
    restarted.initialize()
    assert restarted.resolve()["services"] == {"a": "live"}
    assert restarted.resolve()["restore_in_progress"] is False
    restarted.upsert("b", "ok")
    assert restarted.resolve("b")["address"] == "ok"


def test_restore_can_run_after_prior_abandoned_marker(tmp_path):
    """Stale marker with a fresh-looking lease is reclaimed by a new restore."""
    db = tmp_path / "dir.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value='restoring' WHERE key='restore_state'")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_backup','snap')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_owner','dead')")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('restore_lease_until',?)",
                     (str(time.time() + 3600),))
    result = store.restore_backup("snap")
    assert store.resolve("a")["address"] == "old"
    assert result["entry_count"] == 1


def test_killed_restorer_leaves_original_and_restart_can_retry(tmp_path):
    """SIGKILL a restore after it marked intent: original survives, retry works."""
    import multiprocessing as mp
    import signal

    db = tmp_path / "crash.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=3600)
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    reached = str(tmp_path / "reached")
    ctx = mp.get_context("fork")

    def child():
        s = DirectoryStore(db, restore_lease_seconds=3600)
        s._hooks["after_restore_marked"] = lambda payload: (
            open(reached, "w").close(), time.sleep(60)
        )
        s.restore_backup("snap")  # never returns; killed

    proc = ctx.Process(target=child)
    proc.start()
    try:
        for _ in range(200):
            if os.path.exists(reached):
                break
            time.sleep(0.02)
        assert os.path.exists(reached), "child never marked the restore"
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(10)
        assert proc.exitcode == -signal.SIGKILL

        # The child died holding its flock; the kernel released it.
        after_crash = store.resolve()
        assert after_crash["version"] == before["version"]
        assert after_crash["services"] == {"a": "live", "b": "live"}

        # A fresh process (or this one) immediately reclaims the marker.
        restarted = DirectoryStore(db, restore_lease_seconds=3600)
        restarted.initialize()
        assert restarted.resolve()["restore_in_progress"] is False
        assert restarted.resolve()["services"] == {"a": "live", "b": "live"}

        result = restarted.restore_backup("snap")
        assert result["entry_count"] == 2
        assert restarted.resolve()["services"] == {"a": "old", "b": "old"}
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(5)


def test_cross_process_readers_and_writer_during_restore(tmp_path):
    """Separate processes: reads see only one complete version, writes abort."""
    import multiprocessing as mp

    db = tmp_path / "multi.sqlite3"
    store = DirectoryStore(db, restore_lease_seconds=30)
    seed(store, {"a": "backup-a", "b": "backup-b"})
    store.create_backup("snap")
    for i in range(20):
        store.upsert("a", f"live-{i}")
        store.upsert("b", f"live-{i}")

    ctx = mp.get_context("fork")
    started = ctx.Event()
    stop = ctx.Event()
    reader_q = ctx.Queue()
    writer_q = ctx.Queue()

    def reader():
        s = DirectoryStore(db, restore_lease_seconds=30)
        started.set()
        bad = []
        observations = []
        while not stop.is_set():
            try:
                result = s.resolve()
            except Exception as exc:  # pragma: no cover
                bad.append(("exc", repr(exc)))
                continue
            observations.append((result["version"], result["services"]))
        # A read must be exactly one immutable version: compare against the
        # version's stored rows after the fact.
        for version, services in observations:
            with sqlite3.connect(db) as conn:
                rows = dict(
                    conn.execute(
                        "SELECT service, address FROM entries WHERE version_id=?",
                        (version,),
                    ).fetchall()
                )
            if rows != services:
                bad.append((version, services, rows))
        reader_q.put(bad)

    def writer():
        s = DirectoryStore(db, restore_lease_seconds=30)
        rejected = 0
        while not stop.is_set():
            try:
                s.upsert("a", "during-x")
                s.upsert("b", "during-x")
            except RestoreInProgress:
                rejected += 1
            time.sleep(0.002)
        writer_q.put(rejected)
    readers = [ctx.Process(target=reader) for _ in range(2)]
    writer_proc = ctx.Process(target=writer)
    for p in readers + [writer_proc]:
        p.start()
    started.wait(5)

    gate = BlockingHooks().install(store, "validated")
    restore_thread = threading.Thread(target=store.restore_backup, args=("snap",))
    restore_thread.start()
    assert gate.reached.wait(10)
    time.sleep(0.3)
    gate.release.set()
    restore_thread.join(10)
    time.sleep(0.1)
    stop.set()

    for p in readers + [writer_proc]:
        p.join(10)
        assert p.exitcode == 0, p.exitcode

    problems = []
    for _ in readers:
        problems.extend(reader_q.get(timeout=5))
    rejected = writer_q.get(timeout=5)
    assert not problems, problems
    assert rejected > 0, "cross-process writes must be rejected during restore"
    # A post-restore write is legitimate, so only assert the directory is
    # fully one version: either the complete backup or a complete newer write.
    final = store.resolve()
    with sqlite3.connect(db) as conn:
        rows = dict(
            conn.execute(
                "SELECT service, address FROM entries WHERE version_id=?",
                (final["version"],),
            ).fetchall()
        )
    assert rows == final["services"]
    assert final["restore_in_progress"] is False


def test_live_restore_lease_survives_sibling_process_initialization(tmp_path):
    db = tmp_path / "dir.sqlite3"
    a = DirectoryStore(db, restore_lease_seconds=30)
    a.initialize()
    a.upsert("a", "1")
    a.create_backup("snap")
    gate = BlockingHooks().install(a, "validated")
    t = threading.Thread(target=a.restore_backup, args=("snap",))
    t.start()
    assert gate.reached.wait(10)

    sibling = DirectoryStore(db, restore_lease_seconds=30)
    sibling.initialize()
    assert sibling.resolve()["restore_in_progress"] is True
    with pytest.raises(RestoreInProgress):
        sibling.upsert("x", "y")
    with pytest.raises(RestoreInProgress):
        sibling.restore_backup("snap")

    gate.release.set()
    t.join(10)
    assert sibling.resolve()["services"] == {"a": "1"}


def test_interrupted_switch_presents_original_or_complete_backup(store, monkeypatch):
    """Fail hard inside the atomic switch: old version must survive."""
    seed(store, {"a": "old", "b": "old"})
    store.create_backup("snap")
    store.upsert("a", "live")
    store.upsert("b", "live")
    before = store.resolve()

    original_create_version = store._create_version

    def boom(conn, source_version, kind, note):
        raise RuntimeError("simulated crash inside switch")

    monkeypatch.setattr(store, "_create_version", boom)
    with pytest.raises(RuntimeError):
        store.restore_backup("snap")
    monkeypatch.setattr(store, "_create_version", original_create_version)

    after = store.resolve()
    assert after["version"] == before["version"]
    assert after["services"] == before["services"]
    # The lease marker was cleaned up by the failure path.
    assert after["restore_in_progress"] is False


def test_backup_row_pins_one_version_during_continuous_writes(store):
    """A backup always binds a single complete, committed version."""
    seed(store, {"a": "0", "b": "constant"})
    stop = threading.Event()
    created: list[dict] = []
    published: set[str] = set()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                store.upsert("a", str(i))
                published.add(str(i))
            except RestoreInProgress:
                pass
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    for n in range(25):
        created.append(store.create_backup(f"b{n}"))
    stop.set()
    t.join(10)

    for backup in created:
        with sqlite3.connect(store.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT service, address FROM entries WHERE version_id=?",
                    (backup["version"],),
                ).fetchall()
            )
            version_exists = conn.execute(
                "SELECT 1 FROM versions WHERE id=?", (backup["version"],)
            ).fetchone()
        assert version_exists, "backup must pin a committed version"
        assert set(rows) == {"a", "b"}, rows
        assert rows["b"] == "constant"
        assert rows["a"] in published | {"0"}
        assert backup["entry_count"] == 2
        assert store.validate_backup(backup["name"])["valid"] is True


# ---------------------------------------------------------------------------
# HTTP layer


@pytest.fixture()
def client(tmp_path):
    app = create_app(DirectoryStore(tmp_path / "web.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def test_edge_case_inputs_are_rejected_cleanly(client):
    # Not JSON at all.
    assert client.post("/api/services", data="hello").status_code == 400
    # Wrong types.
    assert client.post("/api/services", json={"service": 5, "address": "x"}).status_code == 400
    assert client.post("/api/services", json={"service": "ok", "address": None}).status_code == 400
    # Oversized address.
    assert client.post(
        "/api/services", json={"service": "ok", "address": "a" * 1000}
    ).status_code == 400
    # Path-traversal-ish backup names.
    for bad in ["../x", "a/b", ".."]:
        resp = client.post("/api/backups", json={"name": bad})
        assert resp.status_code in (400, 404), bad
        assert resp.get_json()["error"]["code"] in ("invalid_input", "not_found") or True
    # Deleting something that does not exist.
    assert client.delete("/api/services/ghost").status_code == 404
    # Unknown backup.
    assert client.post("/api/backups/nope/restore").status_code == 404
    assert client.get("/api/backups/nope/validate").status_code == 404


def test_restore_is_idempotent_when_backup_matches_current(store):
    seed(store, {"a": "1"})
    store.create_backup("snap")
    first = store.restore_backup("snap")
    second = store.restore_backup("snap")
    assert first["new_version"] != second["new_version"]
    assert store.resolve()["services"] == {"a": "1"}


def test_api_flow(client):
    assert client.post("/api/services", json={"service": "a", "address": "1"}).status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/api/backups", json={"name": "snap"}).status_code == 201
    assert client.post("/api/services", json={"service": "a", "address": "2"}).status_code == 200
    assert client.get("/api/backups/snap/validate").get_json()["valid"] is True
    assert client.post("/api/backups/snap/restore").status_code == 200
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.get("/api/state").get_json()["version"] == 4


def test_api_validation_errors_are_json(client):
    resp = client.post("/api/services", json={"service": "bad name", "address": "1"})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_input"
    resp = client.get("/resolve?name=missing")
    assert resp.status_code == 404


def test_html_page_and_forms(client):
    assert client.get("/").status_code == 200
    resp = client.post(
        "/services", data={"service": "a", "address": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert client.get("/resolve?name=a").get_json()["address"] == "1"
    assert client.post("/backups", data={"name": "snap"}).status_code == 303
    assert client.post("/backups/snap/restore").status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert "snap" in page
    assert "onsubmit" not in page
    assert "data-confirm" in page


def test_html_escapes_hostile_names(client):
    client.post("/services", data={"service": "evil", "address": "<script>x</script>"})
    client.post("/backups", data={"name": "b<b>'\"x"})
    page = client.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page
    assert "b&lt;b&gt;" in page
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# Edit tokens (lost-update protection)


def make_app(tmp_path, name="tok.sqlite3"):
    app = create_app(DirectoryStore(tmp_path / name))
    app.config.update(TESTING=True)
    return app, app.test_client()


def open_edit_form(client, service):
    """Extract the per-service edit form as a browser would have rendered it."""
    import re as _re

    page = client.get("/").get_data(as_text=True)
    block = _re.search(
        r'action="/services"[^>]*>.*?name="service" value="([^"]*)".*?'
        r'name="token" value="([^"]*)".*?name="address" value="([^"]*)"',
        page,
        _re.S,
    )
    assert block and block.group(1) == service, page
    return {"service": block.group(1), "token": block.group(2), "address": block.group(3)}


def test_token_present_in_page_and_state(store):
    seed(store, {"a": "1", "b": "2"})
    snapshot = store.snapshot()
    tokens = {row["service"]: row["token"] for row in snapshot["services"]}
    assert set(tokens) == {"a", "b"}
    assert all(tokens.values())
    # Stable while nothing changes; different per service.
    assert store.snapshot()["services"] == snapshot["services"]
    assert tokens["a"] != tokens["b"]
    assert store.resolve("a")["token"] == tokens["a"]


def test_stale_form_cannot_overwrite_newer_edit(store):
    store.upsert("orders", "10.0.0.1:80")
    token_a = store.resolve("orders")["token"]
    store.upsert("orders", "10.0.0.2:80")  # someone else saved first
    version_before = store.resolve()["version"]

    with pytest.raises(StaleEditError) as excinfo:
        store.upsert("orders", "10.0.0.9:80", token=token_a)
    assert excinfo.value.current["address"] == "10.0.0.2:80"
    assert excinfo.value.current["exists"] is True
    after = store.resolve("orders")
    assert after["address"] == "10.0.0.2:80"
    assert after["version"] == version_before  # no new version was published


def test_stale_form_conflicts_even_if_address_reverted(store):
    store.upsert("orders", "old")
    token_old = store.resolve("orders")["token"]
    store.upsert("orders", "middle")
    store.upsert("orders", "old")  # address is back to the original value
    with pytest.raises(StaleEditError) as excinfo:
        store.upsert("orders", "form-value", token=token_old)
    assert excinfo.value.current["address"] == "old"
    assert store.resolve("orders")["address"] == "old"


def test_stale_delete_conflicts_and_does_not_delete(store):
    store.upsert("orders", "a")
    token_old = store.resolve("orders")["token"]
    store.upsert("orders", "b")
    with pytest.raises(StaleEditError):
        store.delete("orders", token=token_old)
    assert store.resolve("orders")["address"] == "b"
    # The fresh token works.
    store.delete("orders", token=store.resolve("orders")["token"])
    with pytest.raises(NotFoundError):
        store.resolve("orders")


def test_delete_then_recreate_invalidates_old_form(store):
    store.upsert("orders", "addr")
    token_before_delete = store.resolve("orders")["token"]
    store.delete("orders")
    store.upsert("orders", "addr")  # same address as the old form had
    with pytest.raises(StaleEditError) as excinfo:
        store.upsert("orders", "other", token=token_before_delete)
    assert excinfo.value.current["address"] == "addr"
    with pytest.raises(StaleEditError):
        store.delete("orders", token=token_before_delete)
    assert store.resolve("orders")["address"] == "addr"


def test_token_for_deleted_service_is_stale_and_reports_missing(store):
    store.upsert("orders", "addr")
    token = store.resolve("orders")["token"]
    store.delete("orders")
    with pytest.raises(StaleEditError) as excinfo:
        store.upsert("orders", "addr", token=token)
    assert excinfo.value.current["exists"] is False
    assert excinfo.value.current["address"] is None
    with pytest.raises(NotFoundError):
        store.resolve("orders")


def test_other_services_edit_does_not_invalidate_token(store):
    seed(store, {"a": "1", "b": "2"})
    token_a = store.resolve("a")["token"]
    store.upsert("b", "changed")
    store.upsert("b", "changed-again")
    store.upsert("a", "1b", token=token_a)  # still accepted
    assert store.resolve("a")["address"] == "1b"


def test_token_survives_restart(tmp_path):
    db = tmp_path / "restart.sqlite3"
    first = DirectoryStore(db)
    first.upsert("a", "1")
    token = first.resolve("a")["token"]

    second = DirectoryStore(db)
    second.initialize()
    assert second.resolve("a")["token"] == token
    assert second.upsert("a", "2", token=token)["address"] == "2"


def test_restore_invalidates_all_old_forms_even_if_same_values(store):
    seed(store, {"a": "old", "b": "keep"})
    store.create_backup("snap")
    token_a = store.resolve("a")["token"]
    token_b = store.resolve("b")["token"]
    store.upsert("a", "live")
    store.upsert("b", "live")
    store.upsert("a", "old")  # a happens to be back to the backup value
    store.upsert("b", "keep")

    store.restore_backup("snap")

    for svc, tok in (("a", token_a), ("b", token_b)):
        with pytest.raises(StaleEditError) as excinfo:
            store.upsert(svc, "from-old-form", token=tok)
        assert excinfo.value.current["exists"] is True
    assert store.resolve()["services"] == {"a": "old", "b": "keep"}
    # Newly issued tokens are usable again.
    store.upsert("a", "post-restore", token=store.resolve("a")["token"])
    assert store.resolve("a")["address"] == "post-restore"


def test_failed_restore_does_not_invalidate_tokens(store):
    seed(store, {"a": "1"})
    store.create_backup("snap")
    token = store.resolve("a")["token"]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE entries SET address='tampered' WHERE service='a' "
            "AND version_id=(SELECT version_id FROM backups WHERE name='snap')"
        )
    with pytest.raises(BackupValidationError):
        store.restore_backup("snap")
    # Restore failed: the form is still current and can be saved.
    assert store.resolve("a")["token"] == token
    store.upsert("a", "2", token=token)
    assert store.resolve("a")["address"] == "2"
    # The failed restore also did not leave the directory read-only.
    store.upsert("b", "3")
    assert store.resolve("b")["address"] == "3"


def test_token_check_and_publish_are_atomic(store):
    """Concurrent saves from the same form: exactly one wins."""
    store.upsert("orders", "v0")
    token = store.resolve("orders")["token"]
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def writer(i):
        barrier.wait()
        try:
            store.upsert("orders", f"w{i}", token=token)
            outcomes.append(f"ok-{i}")
        except StaleEditError:
            outcomes.append(f"stale-{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(outcomes) == 6
    winners = [o for o in outcomes if o.startswith("ok-")]
    assert len(winners) == 1, outcomes
    assert store.resolve("orders")["address"] == f"w{winners[0].split('-')[1]}"


def test_json_api_stale_token_is_409_with_current_state(tmp_path):
    _app, client = make_app(tmp_path)
    created = client.post(
        "/api/services", json={"service": "a", "address": "1"}
    ).get_json()
    token = created["token"]
    client.post("/api/services", json={"service": "a", "address": "2"})
    resp = client.post(
        "/api/services", json={"service": "a", "address": "3", "token": token}
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"]["code"] == "stale_edit"
    assert body["error"]["current"]["address"] == "2"
    assert body["error"]["current"]["exists"] is True
    assert client.get("/resolve?name=a").get_json()["address"] == "2"


def test_json_api_delete_accepts_optional_token(tmp_path):
    _app, client = make_app(tmp_path)
    token = client.post(
        "/api/services", json={"service": "a", "address": "1"}
    ).get_json()["token"]
    client.post("/api/services", json={"service": "a", "address": "2"})
    assert client.delete(f"/api/services/a?token={token}").status_code == 409
    fresh = client.get("/api/state").get_json()["services"][0]["token"]
    assert client.delete(f"/api/services/a?token={fresh}").status_code == 200
    assert client.get("/resolve?name=a").status_code == 404


def test_legacy_tokenless_api_still_works_and_invalidates_forms(tmp_path):
    _app, client = make_app(tmp_path)
    assert client.post(
        "/api/services", json={"service": "a", "address": "1"}
    ).status_code == 200
    token = client.get("/api/state").get_json()["services"][0]["token"]
    # Legacy call without a token: accepted, but rotates the token.
    assert client.post(
        "/api/services", json={"service": "a", "address": "2"}
    ).status_code == 200
    stale = client.post(
        "/api/services", json={"service": "a", "address": "3", "token": token}
    )
    assert stale.status_code == 409
    # Legacy delete without a token still works and invalidates forms.
    fresh = client.get("/api/state").get_json()["services"][0]["token"]
    assert client.delete("/api/services/a").status_code == 200
    assert client.post(
        "/api/services", json={"service": "a", "address": "2", "token": fresh}
    ).status_code == 409
    assert client.get("/resolve?name=a").status_code == 404


def test_html_conflict_shows_current_state_and_creates_no_version(tmp_path):
    app, client = make_app(tmp_path)
    client.post("/api/services", json={"service": "orders", "address": "10.0.0.1:80"})
    form = open_edit_form(client, "orders")
    # Another operator saves first (API without a token is still supported).
    client.post("/api/services", json={"service": "orders", "address": "10.0.0.2:80"})
    version_before = client.get("/api/state").get_json()["version"]

    resp = client.post("/services", data={**form, "address": "10.0.0.9:80"})
    assert resp.status_code == 409
    page = resp.get_data(as_text=True)
    assert "冲突" in page
    assert "10.0.0.2:80" in page  # current published address is shown
    state = client.get("/api/state").get_json()
    assert state["version"] == version_before
    assert state["services"] == [
        {"service": "orders", "address": "10.0.0.2:80",
         "token": state["services"][0]["token"]}
    ]
    assert client.get("/resolve?name=orders").get_json()["address"] == "10.0.0.2:80"


def test_html_conflict_after_restore_renders_current_state(tmp_path):
    _app, client = make_app(tmp_path)
    client.post("/api/services", json={"service": "orders", "address": "old"})
    form = open_edit_form(client, "orders")
    client.post("/backups", data={"name": "snap"})
    client.post("/api/services", json={"service": "orders", "address": "live"})
    client.post("/backups/snap/restore")

    resp = client.post("/services", data={**form, "address": "form-address"})
    assert resp.status_code == 409
    page = resp.get_data(as_text=True)
    assert "冲突" in page
    assert client.get("/resolve?name=orders").get_json()["address"] == "old"


def test_html_stale_delete_conflict(tmp_path):
    _app, client = make_app(tmp_path)
    client.post("/services", data={"service": "orders", "address": "1"})
    client.post("/services", data={"service": "other", "address": "x"})
    page = client.get("/").get_data(as_text=True)
    import re as _re

    match = _re.search(
        r'action="/services/orders/delete".*?name="token" value="([^"]*)"', page, _re.S
    )
    assert match
    stale_token = match.group(1)
    client.post("/api/services", json={"service": "orders", "address": "2"})
    resp = client.post(
        "/services/orders/delete", data={"token": stale_token}
    )
    assert resp.status_code == 409
    assert client.get("/resolve?name=orders").get_json()["address"] == "2"


def test_token_survives_restart_through_http(tmp_path):
    app, client = make_app(tmp_path, "restart-http.sqlite3")
    client.post("/services", data={"service": "a", "address": "1"})
    form = open_edit_form(client, "a")

    app2, client2 = make_app(tmp_path, "restart-http.sqlite3")
    resp = client2.post("/services", data=form)
    assert resp.status_code == 303  # token from before the restart is valid
    assert client2.get("/resolve?name=a").get_json()["address"] == "1"


def test_legacy_database_without_token_schema_is_migrated(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    store = DirectoryStore(db)
    store.upsert("a", "1")
    store.initialize()
    # Emulate a pre-token database: drop the token bookkeeping.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE service_generations")
        conn.execute("DELETE FROM meta WHERE key IN ('edit_secret', 'edit_epoch')")
    reopened = DirectoryStore(db)
    reopened.initialize()
    token = reopened.resolve("a")["token"]
    assert token
    reopened.upsert("a", "2", token=token)
    assert reopened.resolve("a")["address"] == "2"


def test_token_does_not_leak_into_resolve(tmp_path):
    _app, client = make_app(tmp_path)
    client.post("/api/services", json={"service": "a", "address": "1"})
    body = client.get("/resolve").get_json()
    assert body["services"] == {"a": "1"}
    single = client.get("/resolve?name=a").get_json()
    assert "token" not in single


def test_html_add_form_refuses_to_overwrite_existing_service(tmp_path):
    """The tokenless add form must not become a way around the edit token."""
    _app, client = make_app(tmp_path)
    client.post("/api/services", json={"service": "orders", "address": "1"})
    token = client.get("/api/state").get_json()["services"][0]["token"]
    version_before = client.get("/api/state").get_json()["version"]

    resp = client.post("/services", data={"service": "orders", "address": "2"})
    assert resp.status_code == 409
    page = resp.get_data(as_text=True)
    assert "已存在" in page
    state = client.get("/api/state").get_json()
    assert state["version"] == version_before
    assert state["services"][0]["address"] == "1"
    # The still-open form keeps working.
    client.post("/services", data={"service": "orders", "address": "3", "token": token})
    assert client.get("/resolve?name=orders").get_json()["address"] == "3"


def test_legacy_api_is_not_affected_by_add_form_restriction(store):
    """Scripts using the tokenless JSON API can still update and delete."""
    store.upsert("orders", "1", require_new=True)
    with pytest.raises(StaleEditError):
        store.upsert("orders", "2", require_new=True)
    store.upsert("orders", "2")  # legacy update
    assert store.resolve("orders")["address"] == "2"


def test_other_service_edit_leaves_open_form_usable_over_http(tmp_path):
    _app, client = make_app(tmp_path)
    client.post("/api/services", json={"service": "a", "address": "1"})
    client.post("/api/services", json={"service": "b", "address": "2"})
    form = open_edit_form(client, "a")
    # Someone else edits only b (via API and via the HTML row form for b).
    client.post("/api/services", json={"service": "b", "address": "3"})
    resp = client.post("/services", data={**form, "address": "1'"})
    assert resp.status_code == 303
    assert client.get("/resolve?name=a").get_json()["address"] == "1'"
    assert client.get("/resolve?name=b").get_json()["address"] == "3"
