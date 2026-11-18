#!/usr/bin/env python3
"""Read and enforce the user's explicit proprietary-component acceptance."""
import argparse
import os
from pathlib import Path
import stat
import sys

EULA_VERSION = 1
ACCEPTANCE_SETTING = "PROPRIETARY_COMPONENTS_ACCEPTED"
VERSION_SETTING = "EULA_VERSION"


def acceptance_status(root):
    path = Path(root) / "EULA.md"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False, "EULA.md is missing"
    except OSError as error:
        return False, f"EULA.md cannot be opened safely: {error.strerror}"
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            return False, "EULA.md must be a regular file"
        try:
            lines = source.read().decode("utf-8").splitlines()
        except UnicodeError:
            return False, "EULA.md is not valid UTF-8"
    versions = [line for line in lines if line.startswith(VERSION_SETTING)]
    acceptances = [line for line in lines if line.startswith(ACCEPTANCE_SETTING)]
    if versions != [f"{VERSION_SETTING} = {EULA_VERSION}"]:
        return False, "EULA.md is missing the current, unique EULA version"
    if acceptances != [f"{ACCEPTANCE_SETTING} = true"]:
        return False, "the proprietary-component acceptance is not enabled"
    return True, ""


def require_acceptance(root):
    accepted, reason = acceptance_status(root)
    if not accepted:
        raise RuntimeError(
            "Proprietary component downloads and use are disabled: "
            + reason
            + ". Review EULA.md and manually enable its acceptance setting. "
            "Microsoft sign-in and ownership checks still apply."
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parent
    if default_root.name == "client":
        default_root = default_root.parent
    parser.add_argument("--root", type=Path, default=default_root)
    args = parser.parse_args(argv)
    try:
        require_acceptance(args.root)
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Proprietary component acceptance is enabled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
