"""Where work items live.

The engine knows nothing about storage except the `Backend` interface below.
`LocalBackend` is the only implementation today; a remote one drops in beside it
without the engine changing.
"""

import json
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Every state an item can be in, in lifecycle order. The engine and `factory cancel`
# set them; `factory prune` validates what it was asked to clear against them.
STATUSES = ("queued", "running", "done", "failed", "needs_human", "cancelled")
# One request, one branch. Defined once because `workspace` creates it and the CLI's
# `delete` removes it, and the two drifting apart deletes the wrong branch.
BRANCH = "sf/%s"


def _id_number(path):
    """The id in an item filename, as an int. split("-")[-1] also parses legacy
    "req-0NN" stems, so old ids still count and still sort with the new ones."""
    return int(path.stem.split("-")[-1])


class Backend(ABC):
    """The storage contract.

    A remote implementation must honour four guarantees, because the engine
    relies on them rather than on any locking of its own:

    * ``save`` is atomic - a concurrent reader never sees a half-written item.
    * ``claim`` is a compare-and-set - when several engines race for the same
      queued item, exactly one wins and the rest are told no.
    * ``create`` allocates an id no other caller can be given, ever - including
      the ids of deleted items, which stay spent.
    * ``workspace`` returns a path on the **local** filesystem. Agents are local
      processes editing real files; a remote backend syncs that path rather than
      pretending it can live elsewhere. Artifacts have no such constraint - they
      are passed as content, so a remote backend can put them anywhere.
    """

    @abstractmethod
    def create(self, request, pipeline, start_stage, repo, name=""):
        """Allocate an id and persist a new queued work item. Returns the item.

        ``repo`` is the git repository this request will be worked in. It belongs to
        the item, not to the factory: one installation serves many repos at once.
        ``name`` is a human label shown in listings, distinct from the generated ``id``.
        """

    @abstractmethod
    def load(self, item_id):
        """Return one item."""

    @abstractmethod
    def save(self, item):
        """Persist an item atomically. Returns the item."""

    @abstractmethod
    def all(self):
        """Return every item, oldest first."""

    @abstractmethod
    def claim(self, item):
        """Compare-and-set queued -> running. True if this caller won the race."""

    @abstractmethod
    def workspace(self, item_id, repo):
        """A local directory the agents work in. Created on first call.

        Worktrees are centralised rather than kept beside the item, so they can be
        found by repo without knowing request ids.
        """

    @abstractmethod
    def write_artifact(self, item_id, step_no, slug, name, text):
        """Store one artifact of one step execution. Returns a reference to record.

        The backend owns the reference format; the engine only stores it and hands
        it back to ``read_artifact``.
        """

    @abstractmethod
    def read_artifact(self, item_id, ref):
        """Read back an artifact by the reference ``write_artifact`` returned."""

    @abstractmethod
    def delete(self, item_id):
        """Remove an item and everything this backend stored for it.

        Idempotent: deleting an id that is not there is not an error, because a
        remote backend cannot promise the caller saw the latest state.
        """


class LocalBackend(Backend):
    """Items as JSON files in a directory. Inspectable with `cat`."""

    def __init__(self, root, worktrees=None):
        self.root = Path(root)
        self.items = self.root / "items"
        self.items.mkdir(parents=True, exist_ok=True)
        self.worktrees = Path(worktrees) if worktrees else self.root / "worktrees"

    def __repr__(self):
        return "LocalBackend(%s)" % self.root

    def _path(self, item_id):
        return self.items / ("%s.json" % item_id)

    def _dir(self, item_id):
        return self.items / item_id

    def load(self, item_id):
        return json.loads(self._path(item_id).read_text())

    def save(self, item):
        path = self._path(item["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(item, indent=2))
        os.replace(tmp, path)  # atomic
        if item["status"] != "running":
            self._claim_marker(item["id"]).unlink(missing_ok=True)
        return item

    def all(self):
        # Ids are unpadded, so sort them as numbers - lexically "10" would precede "2".
        return [json.loads(p.read_text())
                for p in sorted(self.items.glob("*.json"), key=_id_number)]

    def create(self, request, pipeline, start_stage, repo="", name=""):
        while True:
            # Everything under items/ is named after an id - the item, its directory,
            # its claim marker, its tombstone - so the glob is every id ever handed
            # out. Tombstones count: reusing an id would let `worktree add -B` below
            # reset a deleted request's branch and eat its commits.
            taken = [_id_number(p) for p in self.items.glob("*")]
            item_id = str(max(taken, default=0) + 1)
            try:
                # mkdir is the compare-and-set claim() gets from O_EXCL: of two
                # concurrent submits one gets the directory, the other picks again.
                self._dir(item_id).mkdir()
                break
            except FileExistsError:
                continue
        return self.save(
            {
                "id": item_id,
                "request": request,
                "name": name,
                "repo": str(repo),
                "pipeline": pipeline,
                "stage": start_stage,
                "status": "queued",
                "passes": 1,  # 1-based: a new request is on its first pass
                "created": now(),
                "notes": [],
                "history": [],
            }
        )

    def _claim_marker(self, item_id):
        return self.items / ("%s.claim" % item_id)

    def claim(self, item):
        try:
            # O_EXCL create is the compare-and-set a local filesystem gives us.
            os.close(os.open(self._claim_marker(item["id"]), os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            return False
        item["status"] = "running"
        self.save(item)
        return True

    def _workspace_path(self, item_id, repo):
        # ponytail: keyed by basename. Two repos with the same name share a directory
        # but never a path, because request ids are unique across the installation.
        return self.worktrees / (Path(repo).name if repo else "_scratch") / item_id

    def workspace(self, item_id, repo):
        """A git worktree on its own branch, so concurrent agents never collide.

        Laid out as <worktrees>/<repo name>/<request id>, so everything in flight for
        one repo sits together and `git worktree list` in that repo agrees.
        """
        ws = self._workspace_path(item_id, repo)
        if ws.exists():
            return ws
        ws.parent.mkdir(parents=True, exist_ok=True)
        if not repo or not Path(repo).is_dir():
            ws.mkdir()  # no target repo: a plain scratch directory
            return ws
        # Drop registrations whose directories are gone, so a re-created installation
        # does not collide with its own history.
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
        # -B, not -b: create the branch or reset it to HEAD. `sf/<id>` is this
        # request's branch, and a request id is never reused, so resetting is correct.
        args = ["git", "-C", str(repo), "worktree", "add", "-B",
                BRANCH % item_id, str(ws.resolve())]
        if subprocess.run(args, capture_output=True, text=True).returncode != 0:
            # ponytail: fallback for a read-only or already-branched repo. Costs a copy
            # when state and the repo are different mounts; fine at PoC scale.
            subprocess.run(["git", "clone", "--local", str(repo), str(ws)], check=True, capture_output=True)
        return ws

    def write_artifact(self, item_id, step_no, slug, name, text):
        ref = "steps/%03d-%s/%s" % (step_no, slug, name)
        path = self._dir(item_id) / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return ref

    def read_artifact(self, item_id, ref):
        return (self._dir(item_id) / ref).read_text()

    def delete(self, item_id):
        try:
            repo = self.load(item_id).get("repo", "")
        except FileNotFoundError:
            repo = ""  # record already gone; still clean up whatever else is left
        ws = self._workspace_path(item_id, repo)
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
            if repo:
                # So `git worktree list` stops advertising a checkout that is gone.
                subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
        # The factory/<id> branch stays: cancelling drops the checkout, not the commits.
        # `factory prune` is what takes the branch too, once the work is finished with.
        shutil.rmtree(self._dir(item_id), ignore_errors=True)
        self._path(item_id).unlink(missing_ok=True)
        self._claim_marker(item_id).unlink(missing_ok=True)
        # A tombstone, so create() never hands this id out again. all() globs *.json,
        # so it stays invisible to `factory status` and a second delete is still a no-op.
        self._path(item_id).with_suffix(".deleted").touch()


def open_backend(url, worktrees=None):
    """Resolve a backend from a URL. Local paths and file:// today; http(s):// later."""
    if "://" not in url:
        return LocalBackend(url, worktrees)
    scheme, _, rest = url.partition("://")
    if scheme == "file":
        return LocalBackend(rest, worktrees)
    raise ValueError("no backend for scheme '%s://' - known schemes: file:// "
                     "and a bare local path" % scheme)
