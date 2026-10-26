#!/usr/bin/python3
import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def release(major, build):
    if major < 1 or build < 1:
        raise ValueError("major and build must be positive")
    return f"{major}.{build // 100}.{build % 100}"


def main():
    here = Path(__file__).resolve().parent
    major = 1  # Bump manually only for a major feature release.
    if sys.argv[1:] == ["--self-test"]:
        assert release(1, 6) == "1.0.6"
        assert release(1, 99) == "1.0.99"
        assert release(1, 100) == "1.1.0"
        assert release(1, 184) == "1.1.84"
        assert release(1, 199) == "1.1.99"
        assert release(1, 200) == "1.2.0"
        assert release(2, 11) == "2.0.11"
        return
    if len(sys.argv) != 2:
        raise SystemExit("usage: version.py --self-test | OUTPUT.swift")
    git_hash = subprocess.check_output(
        ["git", "-C", str(here.parent), "rev-parse", "HEAD"], text=True
    ).strip().lower()
    if len(git_hash) != 40 or any(c not in "0123456789abcdef" for c in git_hash):
        raise SystemExit("invalid source revision")
    git_hash = git_hash[:8]
    dirty = subprocess.check_output(
        ["git", "-C", str(here), "status", "--porcelain", "--",
         "App.swift", "brand.json", "build.sh", "version.py", "VPNHelperProtocol.swift",
         "VPNHelper.swift", "VPNHelperClient.swift", "UpdateCoordinator.swift", "update-config.json",
         "../helpers/updater.swift", "../helpers/updater-entitlements.plist",
         "../scripts/branding.py", "../scripts/prepare-sparkle.sh", "../scripts/configure-updates.py"], text=True
    ).strip()
    if dirty:
        git_hash += "-dirty"
    counter = here / "build-number"
    lock_path = here.parent / "dist" / ".version.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = counter.read_text().strip()
        if not raw.isdecimal():
            raise SystemExit("invalid build-number")
        build = int(raw) + 1
        version = release(major, build)
        fd, temp_path = tempfile.mkstemp(dir=here, prefix=".build-number-")
        try:
            with os.fdopen(fd, "w") as output:
                output.write(f"{build}\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, counter)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        Path(sys.argv[1]).write_text(
            f'enum BuildVersion {{ static let display = "{version}-{git_hash}"; '
            f'static let marketing = "{version}"; static let build = "{build}" }}\n'
        )


if __name__ == "__main__":
    main()
