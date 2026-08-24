"""
cut a release on a fork of tach that other projects can pin to.

builds wheels, creates a GitHub release on the fork, attaches the wheels to it,
and prints the `[tool.uv.sources]` block for pinning to them.

the version must carry a PEP 440 local segment (the part after `+`). that keeps
fork builds obviously distinct from upstream releases, and PyPI refuses local
versions outright, so a fork build can never be published there by accident.

    ./pw uv run scripts/release_fork.py 0.35.0+deadcode.1

wheels are built for the host platform only. pushing the tag also starts
.github/workflows/fork-release-linux.yml, which builds the Linux wheels and
attaches them to the same release — so a full release needs Actions enabled on
the fork. wheels built anywhere else can be attached directly:

    ./pw uv run scripts/release_fork.py 0.35.0+deadcode.1 \
        --wheel dist/tach-0.35.0+deadcode.1-cp37-abi3-manylinux_2_17_x86_64.whl
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).parent.parent
WHEEL_DIR = REPO_ROOT / "target" / "wheels"
UPSTREAM = "tach-org/tach"

# version = "..." on its own line, in each file that has to agree. the version
# consistency test in python/tests/test_version.py enforces that they do.
VERSION_FILES: list[tuple[Path, re.Pattern[str], str]] = [
    (REPO_ROOT / "Cargo.toml", re.compile(r'^version = ".*"$', re.M), 'version = "{}"'),
    (
        REPO_ROOT / "pyproject.toml",
        re.compile(r'^version = ".*"$', re.M),
        'version = "{}"',
    ),
    (
        REPO_ROOT / "python" / "tach" / "__init__.py",
        re.compile(r'^__version__: str = ".*"$', re.M),
        '__version__: str = "{}"',
    ),
]

# enough of PEP 440 to catch typos, and to require the local segment.
VERSION_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:[a-z]+\d+)?\+[a-z0-9]+(?:\.[a-z0-9]+)*$"
)

# wheel platform tag -> the environment it should be installed in.
PLATFORM_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"macosx_.*_arm64"),
        "sys_platform == 'darwin' and platform_machine == 'arm64'",
    ),
    (
        re.compile(r"macosx_.*_x86_64"),
        "sys_platform == 'darwin' and platform_machine == 'x86_64'",
    ),
    (
        re.compile(r"(many|musl)linux.*_x86_64"),
        "sys_platform == 'linux' and platform_machine == 'x86_64'",
    ),
    (
        re.compile(r"(many|musl)linux.*_aarch64"),
        "sys_platform == 'linux' and platform_machine == 'aarch64'",
    ),
    (re.compile(r"win_amd64"), "sys_platform == 'win32'"),
]


def run(
    command: list[str], *, capture: bool = False, dry_run: bool = False
) -> str | None:
    printable = " ".join(command)
    if dry_run:
        print(f"  would run: {printable}")
        return None
    print(f"  {printable}")
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=capture,
        encoding="utf8",
    )
    return result.stdout.strip() if capture else None


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def fork_repo() -> str:
    """The `owner/name` of the `origin` remote, which must not be upstream."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        encoding="utf8",
    ).stdout.strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        fail(f"could not read a repository out of the origin remote: {url}")
        raise AssertionError  # unreachable, keeps the type checker happy
    repo = match.group(1)
    if repo.lower() == UPSTREAM.lower():
        fail(
            f"origin is {UPSTREAM}. publishing a release there triggers the PyPI"
            " upload in .github/workflows/publish.yml; point origin at a fork."
        )
    return repo


def set_version(version: str, *, dry_run: bool) -> None:
    for path, pattern, replacement in VERSION_FILES:
        content = path.read_text()
        updated, count = pattern.subn(replacement.format(version), content, count=1)
        if count != 1:
            fail(f"could not find the version line in {path}")
        relative = path.relative_to(REPO_ROOT)
        if dry_run:
            print(f"  would set version in {relative}")
        else:
            print(f"  version -> {version} in {relative}")
            path.write_text(updated)


def build_wheel(*, dry_run: bool) -> list[Path]:
    before = set(WHEEL_DIR.glob("*.whl")) if WHEEL_DIR.exists() else set()
    run(["maturin", "build", "--release"], dry_run=dry_run)
    if dry_run:
        return []
    built = sorted(set(WHEEL_DIR.glob("*.whl")) - before)
    if not built:
        fail(f"maturin reported success but produced no new wheel in {WHEEL_DIR}")
    return built


def publish_release(
    repo: str, tag: str, notes: str, wheels: list[Path], *, dry_run: bool
) -> None:
    """Create the release and attach `wheels`, or attach to it if it exists.

    Pushing the tag starts the Linux wheel workflow, which creates the release
    if it gets there first, so this cannot assume it is the one creating it.
    """
    assets = [str(wheel) for wheel in wheels]
    exists = not dry_run and (
        subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        == 0
    )
    if exists:
        print(f"  release {tag} already exists, attaching wheels to it")
        run(
            ["gh", "release", "upload", tag, "--repo", repo, "--clobber", *assets],
            dry_run=dry_run,
        )
        return
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--title",
            tag,
            "--notes",
            notes,
            *assets,
        ],
        dry_run=dry_run,
    )


def uv_source_block(repo: str, tag: str, wheels: list[Path]) -> str:
    """The `[tool.uv.sources]` entry pinning tach to this release's wheels."""
    # GitHub percent-encodes the '+' of a local version in its download URLs,
    # in both the tag and the file name.
    base = f"https://github.com/{repo}/releases/download/{quote(tag)}"
    entries: list[str] = []
    for wheel in wheels:
        marker = next(
            (m for pattern, m in PLATFORM_MARKERS if pattern.search(wheel.name)), None
        )
        url = f"{base}/{quote(wheel.name)}"
        if marker is None:
            entries.append(f'  {{ url = "{url}" }},')
        else:
            entries.append(f'  {{ url = "{url}", marker = "{marker}" }},')
    return "[tool.uv.sources]\ntach = [\n" + "\n".join(entries) + "\n]"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "version",
        help="release version, which must include a local segment, e.g. 0.35.0+deadcode.1",
    )
    _ = parser.add_argument(
        "--wheel",
        action="append",
        default=[],
        metavar="path",
        type=Path,
        help="additional prebuilt wheel to attach, for platforms this host cannot build."
        " May be repeated.",
    )
    _ = parser.add_argument(
        "--notes", default=None, help="release notes body (default: the tag name)"
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without writing, tagging, or publishing",
    )
    args = parser.parse_args()

    version: str = args.version
    extra_wheels: list[Path] = args.wheel
    dry_run: bool = args.dry_run

    if not VERSION_PATTERN.match(version):
        fail(
            f"'{version}' is not a fork version. it needs a local segment so it cannot"
            " be confused with, or uploaded as, an upstream release — e.g."
            " 0.35.0+deadcode.1"
        )
    for wheel in extra_wheels:
        if not wheel.is_file():
            fail(f"no such wheel: {wheel}")

    repo = fork_repo()
    tag = f"v{version}"
    print(f"releasing {version} as {tag} on {repo}")

    if subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        encoding="utf8",
    ).stdout.strip():
        fail("the working tree has uncommitted changes; commit or stash them first")

    set_version(version, dry_run=dry_run)
    # keep Cargo.lock's record of the workspace version in step with Cargo.toml
    run(["cargo", "metadata", "--format-version", "1", "--quiet"], dry_run=dry_run)

    wheels = build_wheel(dry_run=dry_run) + extra_wheels
    for wheel in wheels:
        print(f"  wheel: {wheel.name}")

    run(["git", "commit", "--all", "--message", f"release {version}"], dry_run=dry_run)
    run(["git", "tag", tag], dry_run=dry_run)
    run(["git", "push", "origin", "HEAD", tag], dry_run=dry_run)
    publish_release(repo, tag, args.notes or tag, wheels, dry_run=dry_run)

    print(f"\nreleased: https://github.com/{repo}/releases/tag/{tag}")
    if wheels:
        print("\npin to it by adding this to the consuming project's pyproject.toml:\n")
        print(uv_source_block(repo, tag, wheels))


if __name__ == "__main__":
    main()
