"""The storage contract, as behaviour. Every `Backend` implementation must pass this.

Written against the interface, never against `LocalBackend`: point `backend_impl` at
another implementation - parametrise the fixture, or override it in a `conftest.py` of
your own - and these assertions are what says the swap is safe. `docs/internals.md` is
the prose half; nothing is asserted here that is not promised there.
"""

import subprocess
import threading
from pathlib import Path

import pytest

from software_factory.backend import LocalBackend


@pytest.fixture
def backend_impl(tmp_path):
    """An empty backend. Override or parametrise this to run the suite against yours."""
    return LocalBackend(tmp_path / "state", tmp_path / "worktrees")


def test_backend_round_trips_an_item(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    assert item["status"] == "queued" and item["passes"] == 1
    item["passes"] = 2
    backend_impl.save(item)
    assert backend_impl.load(item["id"])["passes"] == 2
    assert [i["id"] for i in backend_impl.all()] == [item["id"]]


def test_backend_claim_is_compare_and_set(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    assert backend_impl.claim(item) is True
    assert backend_impl.load(item["id"])["status"] == "running"
    assert backend_impl.claim(backend_impl.load(item["id"])) is False, "only one engine wins"
    # Releasing it (any non-running status) makes it claimable again.
    item["status"] = "queued"
    backend_impl.save(item)
    assert backend_impl.claim(item) is True


def test_backend_claim_lets_exactly_one_thread_through(backend_impl):
    """The guarantee the whole multi-engine story rests on, run as an actual race."""
    item = backend_impl.create("do a thing", "t", "a")
    start = threading.Barrier(8)
    won = []

    def race():
        start.wait()
        if backend_impl.claim(backend_impl.load(item["id"])):
            won.append(threading.current_thread().name)

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1, "eight engines raced, %d were told yes" % len(won)


def test_backend_ids_are_unpadded_and_stay_in_numeric_order(backend_impl):
    ids = [backend_impl.create("do a thing", "t", "a")["id"] for _ in range(10)]
    assert ids[0] == "1" and ids[-1] == "10", "an id is a plain integer, no leading zeros"
    # `all()` feeds every listing, so it has to sort 10 after 2, not before it.
    assert [i["id"] for i in backend_impl.all()] == ids


def test_backend_ids_are_unique_under_concurrent_creates(backend_impl):
    """Two `sf submit` calls at once must not be handed the same id."""
    start = threading.Barrier(8)
    ids = []

    def race():
        start.wait()
        ids.append(backend_impl.create("do a thing", "t", "a")["id"])

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(ids, key=int) == [str(n) for n in range(1, 9)]
    assert len(backend_impl.all()) == 8, "every winner kept its own record"


def test_backend_ids_are_not_reused_after_a_delete(backend_impl):
    """A deleted id stays spent - its branch and its commits are still out there."""
    ids = [backend_impl.create("do a thing", "t", "a")["id"] for _ in range(3)]
    backend_impl.delete(ids[-1])
    assert backend_impl.create("another thing", "t", "a")["id"] not in ids


def test_backend_save_is_atomic_for_a_concurrent_reader(backend_impl):
    """A reader mid-save sees the old item or the new one, never half of either."""
    item = backend_impl.create("do a thing", "t", "a")
    stop = threading.Event()
    seen, errors = [], []

    def read():
        while not stop.is_set():
            try:
                seen.append(backend_impl.load(item["id"])["passes"])
            except Exception as exc:  # a torn read surfaces as a parse or missing-file error
                errors.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for n in range(2, 200):
            item["passes"] = n
            backend_impl.save(item)
    finally:
        stop.set()
        reader.join()
    assert not errors, errors
    assert set(seen) <= set(range(1, 200)) and seen, "every read was a whole item"


def test_backend_round_trips_artifacts_by_reference(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    ref = backend_impl.write_artifact(item["id"], 1, "plan", "input.md", "the prompt")
    # The engine only ever stores the reference, so the format is the backend's business.
    assert isinstance(ref, str)
    assert backend_impl.read_artifact(item["id"], ref) == "the prompt"
    # Same file name, a different step: two references, two artifacts.
    other = backend_impl.write_artifact(item["id"], 2, "code", "input.md", "the diff")
    assert other != ref and backend_impl.read_artifact(item["id"], other) == "the diff"


def test_backend_workspace_is_a_local_directory(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    ws = backend_impl.workspace(item["id"], repo="")
    assert Path(ws).is_dir(), "agents are local processes; they need a real directory"
    # The guarantee in full: a subprocess can cd into it. A URL would not do.
    assert subprocess.run(["pwd"], cwd=str(ws), capture_output=True).returncode == 0
    assert backend_impl.workspace(item["id"], repo="") == ws, "stable across calls"


def test_backend_delete_removes_the_item_and_its_artifacts(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    ref = backend_impl.write_artifact(item["id"], 1, "plan", "output.md", "the plan")
    backend_impl.delete(item["id"])
    assert backend_impl.all() == []
    # The exception type is the backend's own - only that it refuses is promised.
    with pytest.raises(Exception):
        backend_impl.read_artifact(item["id"], ref)
    backend_impl.delete(item["id"])  # idempotent: deleting twice is not an error
