"""The backend contract.

Every implementation must pass these. When a remote backend lands, parametrise the fixture
over both and the same assertions guard the swap.
"""

import subprocess

import pytest

from software_factory.__main__ import main
from software_factory.backend import Backend, LocalBackend, open_backend

from conftest import a_repo


@pytest.fixture
def backend_impl(tmp_path):
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


def test_backend_ids_are_unpadded_and_stay_in_numeric_order(backend_impl):
    ids = [backend_impl.create("do a thing", "t", "a")["id"] for _ in range(10)]
    assert ids[0] == "1" and ids[-1] == "10", "an id is a plain integer, no leading zeros"
    # `all()` feeds every listing, so it has to sort 10 after 2, not before it.
    assert [i["id"] for i in backend_impl.all()] == ids


def test_backend_workspace_is_a_local_directory(backend_impl):
    backend_impl.create("do a thing", "t", "a")
    ws = backend_impl.workspace("1", repo="")
    assert ws.is_dir(), "agents are local processes; they need a real directory"


def test_backend_round_trips_artifacts_by_reference(backend_impl):
    backend_impl.create("do a thing", "t", "a")
    ref = backend_impl.write_artifact("1", 1, "plan", "input.md", "the prompt")
    assert backend_impl.read_artifact("1", ref) == "the prompt"
    # The engine only ever stores the reference, so the format is the backend's business.
    assert isinstance(ref, str)


def test_backend_delete_removes_the_item_and_its_artifacts(backend_impl):
    item = backend_impl.create("do a thing", "t", "a")
    backend_impl.write_artifact(item["id"], 1, "plan", "output.md", "the plan")
    backend_impl.delete(item["id"])
    assert backend_impl.all() == []
    with pytest.raises(FileNotFoundError):
        backend_impl.read_artifact(item["id"], "steps/001-plan/output.md")
    backend_impl.delete(item["id"])  # idempotent: deleting twice is not an error


def test_local_backend_is_the_default_url_scheme(tmp_path):
    assert isinstance(open_backend(str(tmp_path)), Backend)
    assert isinstance(open_backend("file://%s" % tmp_path), LocalBackend)
    with pytest.raises(ValueError, match="no backend for scheme 'https"):
        open_backend("https://factory.example.com")


def test_worktree_survives_a_reused_branch_name(installation, tmp_path):
    """Request ids restart when an installation is re-created; the branch must not collide."""
    repo = a_repo(tmp_path / "myproject")
    subprocess.run(["git", "-C", str(repo), "branch", "sf/1"], check=True, capture_output=True)

    backend = LocalBackend(installation / "state", installation / "worktrees")
    backend.create("r", "t", "a", repo=str(repo))
    ws = backend.workspace("1", str(repo))

    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout
    if str(ws.resolve()) not in listed:
        # The fallback in `workspace()` swallows git's stderr, so a failure here says only
        # that something went wrong. Ask git the same question again and quote it.
        why = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-B", "sf/probe", str(tmp_path / "probe")],
            capture_output=True, text=True,
        )
        pytest.fail("a real worktree, not a silent fallback to a clone.\n"
                    "worktree list:\n%s\nthe same add, again: rc=%d\n%s%s"
                    % (listed, why.returncode, why.stdout, why.stderr))



def test_an_unknown_backend_scheme_is_an_error_not_a_traceback(installation, capsys):
    assert main(["--backend", "https://factory.example.com", "status"]) == 1
    assert "known schemes: file://" in capsys.readouterr().err


# --- replay and inspection -----------------------------------------------------
