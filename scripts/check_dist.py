"""Check a built wheel is one someone can actually install and use.

`uv build` succeeding only says a zip was produced. It says nothing about whether the
files the CLI needs at run time are inside it, and this package is mostly such files: the
built-in runners and the skill `sf --skill` prints. A wheel without them installs a CLI
that cannot resolve a single `uses:`.

Both of those have shipped broken here. `factory/pipelines/` went missing from the working
tree during a rename and nothing noticed until a test happened to load `dev`; the console
script pointed at `src.__main__:main` after another, and `sf` was simply not a command.
Hence this file rather than a comment asking people to remember.

Run against the newest wheel in dist/, or a path given as the first argument.
"""

import sys
import zipfile
from pathlib import Path

# What the CLI reads at run time, and the least that has to be there for it to work.
#
# No pipeline is listed here, deliberately: none ship. The factory is the engine and the
# process is the user's, so a wheel carrying a `dev.yaml` would be the bug. The runners
# are different - `uses: claude` has to resolve out of the box, so those are the engine.
REQUIRED = {
    "built-in runners": (lambda n: "/runners/" in n and n.endswith(".yaml"), 3),
    "the skill file": (lambda n: n.endswith("SKILL.md"), 1),
    "the entry point": (lambda n: n.endswith("entry_points.txt"), 1),
}
# And the other direction: a pipeline that sneaks into the wheel is a default nobody asked
# for, shipped to every repo. Cheaper to catch here than to explain later.
FORBIDDEN = {"a shipped pipeline": lambda n: "/pipelines/" in n}

# The name people type to install it. A wheel *filename* always writes it
# `software_factory-...whl` - the dash is the field separator there, so the distribution
# name is escaped - but the METADATA `Name:` is the real one, and it is what `pip install`
# resolves. Pinned here because a rename of the project would otherwise publish a package
# nobody's install line finds.
DIST_NAME = "software-factory"


def main(argv):
    if argv:
        wheel = Path(argv[0])
    else:
        wheels = sorted(Path("dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
        if not wheels:
            sys.exit("no wheel in dist/ - run `uv build` first")
        wheel = wheels[-1]

    names = zipfile.ZipFile(wheel).namelist()
    print("checking %s" % wheel.name)
    problems = []
    for what, (matches, least) in REQUIRED.items():
        found = [n for n in names if matches(n)]
        print("  %-18s %d" % (what, len(found)))
        if len(found) < least:
            problems.append("%s: found %d, expected at least %d" % (what, len(found), least))

    for what, matches in FORBIDDEN.items():
        found = [n for n in names if matches(n)]
        if found:
            problems.append("%s is in the wheel: %s" % (what, ", ".join(sorted(found)[:5])))

    meta = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
    if meta:
        name = next((line.split(": ", 1)[1] for line in
                     zipfile.ZipFile(wheel).read(meta).decode().splitlines()
                     if line.startswith("Name: ")), "")
        print("  %-18s %s" % ("the package name", name))
        if name != DIST_NAME:
            problems.append("package name is %r, not %r" % (name, DIST_NAME))
    else:
        problems.append("no METADATA in the wheel")

    # The console script is the difference between `pip install` giving you a command and
    # giving you an importable package nobody can run.
    entry = next((n for n in names if n.endswith("entry_points.txt")), None)
    if entry:
        text = zipfile.ZipFile(wheel).read(entry).decode()
        if "sf = software_factory.__main__:main" not in text:
            problems.append("entry point is not `sf = software_factory.__main__:main`:\n%s" % text)

    if problems:
        sys.exit("\n".join(["wheel is not installable as intended:"] + ["  - " + p for p in problems]))
    print("  ok")


if __name__ == "__main__":
    main(sys.argv[1:])
