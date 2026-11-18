#!/usr/bin/env python3
"""Build a pinned x86_64 Wine dependency prefix in a new isolated directory.

Requires Python 3.12+, Xcode command-line tools, make, pkg-config and Rosetta.
Never installs into a system or live runtime. Retains exact source archives,
notices, build commands and hashes for corresponding-source preparation.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "config/native-dependencies.lock.json"
EXTRA_NOTICES = {"freetype": (
    "docs/FTL.TXT", "docs/GPLv2.TXT", "src/bdf/README", "src/pcf/README",
    "src/base/fthash.c", "include/freetype/internal/fthash.h", "src/gzip/zlib.h",
    "src/autofit/ft-hb-ft.c", "src/autofit/ft-hb-decls.h",
    "src/autofit/ft-hb-types.h", "src/autofit/hb-script-list.h",
)}


def require_build_path(path, label):
    """Keep Autoconf/make flag paths unambiguous, including symlink targets."""
    path = Path(path).expanduser().absolute()
    try:
        targets = (path, path.resolve())
    except (OSError, RuntimeError) as error:
        raise ValueError("Cannot resolve " + label + " path") from error
    if any(re.fullmatch(r"[A-Za-z0-9/._+-]+", str(target)) is None for target in targets):
        raise ValueError("Unsupported " + label + " path: use only ASCII letters, digits, / . _ + - "
                         "in build paths and their resolved targets; spaces and shell metacharacters "
                         "are not supported by the native build tools.")
    return path


def build_tools():
    pkg_config = shutil.which(os.environ.get("PKG_CONFIG", "pkg-config"))
    if not pkg_config:
        raise ValueError("pkg-config is required; set PKG_CONFIG to its executable before building")
    pkg_config = require_build_path(pkg_config, "pkg-config")
    sdk = require_build_path(subprocess.check_output(
        ["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip(), "macOS SDK")
    clang = require_build_path(subprocess.check_output(
        ["/usr/bin/xcrun", "--find", "clang"], text=True).strip(), "C compiler")
    return str(sdk), str(clang), str(pkg_config)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def inventory(prefix):
    result = {}
    for path in sorted(prefix.rglob("*")):
        relative = path.relative_to(prefix).as_posix()
        if relative == "dependency-manifest.json":
            continue
        if path.is_symlink():
            target = path.readlink()
            if target.is_absolute() or not path.resolve().is_relative_to(prefix.resolve()) or not path.exists():
                raise ValueError("Unsafe dependency link: " + relative)
            result[relative] = {"symlink": str(target)}
        elif path.is_file():
            result[relative] = {"sha256": digest(path)}
        elif not path.is_dir():
            raise ValueError("Unsupported dependency file: " + relative)
    return result


def verify(prefix):
    prefix = Path(prefix)
    if prefix.is_symlink():
        raise ValueError("Dependency prefix must not be a symlink")
    manifest = prefix / "dependency-manifest.json"
    if manifest.is_symlink():
        raise ValueError("Dependency manifest must not be a symlink")
    data = json.loads(manifest.read_text())
    if data.get("schema") != 1 or data.get("source_built") is not True or data.get("architecture") != "x86_64":
        raise ValueError("Unsupported dependency provenance")
    if data.get("lock_sha256") != digest(LOCK):
        raise ValueError("Dependency inputs differ from the reviewed source lock")
    if data.get("source_archives") != json.loads(LOCK.read_text())["components"]:
        raise ValueError("Dependency source inventory differs from the reviewed lock")
    if data.get("files") != inventory(prefix):
        raise ValueError("Dependency prefix differs from its manifest")
    for required in ("lib/libgnutls.30.dylib", "lib/libfreetype.6.dylib", "include/gnutls/gnutls.h", "include/freetype2/ft2build.h"):
        if not (prefix / required).is_file():
            raise ValueError("Missing dependency: " + required)
    return data


def _remove_owned_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        raise ValueError("Unsupported existing dependency path: " + str(path))


def copy_dependency_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.is_dir():
        _remove_owned_path(destination)
    if destination.is_file() and digest(source) == digest(destination):
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".dependency-copy-", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def copy_dependency_tree(source, destination):
    """Idempotently stage a verified tree, including symlinks, after interrupted copies."""
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Dependency source must be a real directory")
    if destination.is_symlink():
        raise ValueError("Dependency destination must not be a symlink")

    def copy_entry(origin, target):
        if origin.is_symlink():
            link = origin.readlink()
            if target.is_symlink() and target.readlink() == link:
                return
            if os.path.lexists(target):
                _remove_owned_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        elif origin.is_dir():
            if target.is_symlink() or (os.path.lexists(target) and not target.is_dir()):
                _remove_owned_path(target)
            target.mkdir(parents=True, exist_ok=True)
            for child in origin.iterdir():
                copy_entry(child, target / child.name)
        elif origin.is_file():
            copy_dependency_file(origin, target)
        else:
            raise ValueError("Unsupported dependency source path: " + str(origin))

    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        copy_entry(child, destination / child.name)


def run(command, log, env, cwd=None, live_output=False):
    with log.open("ab") as output:
        output.write(b"COMMAND " + json.dumps(command).encode("utf-8") + b"\n")
        output.flush()
        if not live_output:
            subprocess.run(command, cwd=cwd, env=env, stdout=output,
                           stderr=subprocess.STDOUT, check=True)
            return
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0)
        try:
            with process.stdout:
                while chunk := process.stdout.read(4096):
                    output.write(chunk)
                    output.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            code = process.wait()
        except BaseException:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        if code:
            raise subprocess.CalledProcessError(code, command)


def relocate(prefix):
    """Keep only system absolute loads and contained relative library loads."""
    libraries = prefix / "lib"
    for library in sorted(libraries.glob("*.dylib")):
        if library.is_symlink():
            continue
        architecture = subprocess.check_output(["lipo", "-archs", str(library)], text=True).strip()
        if architecture != "x86_64":
            raise ValueError("Unexpected library architecture: " + library.name)
        lines = subprocess.check_output(["otool", "-L", str(library)], text=True).splitlines()[2:]
        for line in lines:
            old = line.strip().split(" (")[0]
            if old.startswith(str(libraries) + "/"):
                new = "@loader_path/" + Path(old).name
                subprocess.run(["install_name_tool", "-change", old, new, str(library)], check=True)
            else:
                new = old
            if new.startswith("@loader_path/"):
                dependency = (library.parent / new[len("@loader_path/"):]).resolve()
                if not dependency.is_relative_to(libraries.resolve()) or not dependency.is_file():
                    raise ValueError("Escaping or missing library dependency: " + new)
            elif not new.startswith(("/System/", "/usr/lib/")):
                raise ValueError("Untracked library dependency: " + new)
        commands = subprocess.check_output(["otool", "-l", str(library)], text=True).splitlines()
        for index, line in enumerate(commands):
            if line.strip() == "cmd LC_RPATH":
                path = commands[index + 2].strip().removeprefix("path ").rsplit(" (offset", 1)[0]
                subprocess.run(["install_name_tool", "-delete_rpath", path, str(library)], check=True)
        subprocess.run(["install_name_tool", "-id", "@rpath/" + library.name, str(library)], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(library)], check=True, stderr=subprocess.DEVNULL)


def require_fresh_outputs(destination, sources, prefix):
    if (destination / "build-identity.json").exists() or any(sources.iterdir()) or any(prefix.iterdir()):
        raise ValueError("Existing build outputs are not reusable; use --verify or a new isolated destination")


def build_environment(destination, prefix, sdk, clang, pkg_config):
    # Autoconf honors numerous *_CFLAGS/*_LIBS and cache/site variables. Do not
    # inherit shell overrides or loader/preload settings into a source receipt.
    require_build_path(ROOT, "source checkout")
    destination = require_build_path(destination, "dependency destination")
    prefix = require_build_path(prefix, "dependency prefix")
    sdk = str(require_build_path(sdk, "macOS SDK"))
    clang = str(require_build_path(clang, "C compiler"))
    pkg_config = str(require_build_path(pkg_config, "pkg-config"))
    home, temporary = destination / "build-home", destination / "build-tmp"
    home.mkdir(exist_ok=True, mode=0o700)
    temporary.mkdir(exist_ok=True, mode=0o700)
    return dict(PATH="/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(home), TMPDIR=str(temporary),
        SHELL="/bin/sh", CONFIG_SITE="/dev/null", LC_ALL="C", TZ="UTC",
        CC=f"{clang} -arch x86_64 -isysroot {sdk}", CFLAGS="-O2 -mmacosx-version-min=15.0",
        CPPFLAGS=f"-I{prefix}/include", LDFLAGS=f"-L{prefix}/lib -Wl,-rpath,{prefix}/lib -mmacosx-version-min=15.0",
        MACOSX_DEPLOYMENT_TARGET="15.0", PKG_CONFIG=pkg_config,
        PKG_CONFIG_LIBDIR=str(prefix / "lib/pkgconfig"), PKG_CONFIG_PATH="",
        ZLIB_CFLAGS=f"-I{sdk}/usr/include", ZLIB_LIBS="-lz")


def check_source_lock(directory, lock_digest):
    if directory.is_symlink():
        raise ValueError("Source download cache must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "source-lock.sha256"
    if marker.is_symlink():
        raise ValueError("Source download cache marker must not be a symlink")
    if marker.exists():
        if marker.read_text().strip() != lock_digest:
            raise ValueError("Existing source download cache uses different pinned sources")
    elif any(directory.iterdir()):
        raise ValueError("Source download cache contains unmarked files; preserve it and choose a new cache directory")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".source-lock-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(lock_digest + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, marker)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def fetch_archive(component, downloads):
    archive = downloads / component["archive"]
    partial = downloads / (component["archive"] + ".part")
    for path in (archive, partial):
        if path.is_symlink():
            raise ValueError("Source archive path must not be a symlink: " + path.name)
        if path.exists() and not path.is_file():
            raise ValueError("Source archive path must be a regular file: " + path.name)
    expected = component["sha256"]
    if archive.exists():
        if digest(archive) == expected:
            print("Reusing verified source archive: " + component["name"], flush=True)
            return archive
        archive.unlink()
        print("Discarded a source archive with a checksum mismatch: " + component["name"], flush=True)

    def download(resume):
        command = ["/usr/bin/curl", "--disable", "--fail", "--location", "--proto", "=https",
                   "--proto-redir", "=https", "--retry", "3", "--progress-bar"]
        if resume:
            command.extend(["--continue-at", "-"])
        command.extend(["--output", str(partial), component["url"]])
        subprocess.run(command, check=True)

    resuming = partial.exists()
    if resuming:
        print("Resuming partial source download: " + component["name"], flush=True)
    else:
        print("Downloading source archive: " + component["name"], flush=True)
    download(resuming)
    if digest(partial) != expected:
        partial.unlink()
        if not resuming:
            raise ValueError("Source digest mismatch: " + component["name"])
        print("The resumed bytes did not match the pinned checksum; retrying the archive from the beginning.",
              flush=True)
        download(False)
        if digest(partial) != expected:
            partial.unlink()
            raise ValueError("Source digest mismatch: " + component["name"])
    partial.replace(archive)
    print("Verified and cached source archive: " + component["name"], flush=True)
    return archive


def build(destination, jobs, fetch_only=False, cache_dir=None, live_output=False):
    # Validate before creating a cache or downloading anything. Fetch-only does
    # not require a compiler/SDK, but its future build paths must still be safe.
    require_build_path(ROOT, "source checkout")
    destination = require_build_path(destination, "dependency destination")
    for name in ("downloads", "source", "prefix", "logs", "build-home", "build-tmp"):
        require_build_path(destination / name, "dependency " + name)
    separate_cache = cache_dir is not None
    cache_dir = destination if cache_dir is None else require_build_path(cache_dir, "source download cache")
    if separate_cache and (cache_dir == destination
                           or cache_dir in destination.parents
                           or destination in cache_dir.parents):
        raise ValueError("Source download cache must be separate from the build attempt")
    require_build_path(cache_dir / "downloads", "source download cache")
    if not hasattr(tarfile, "data_filter"):
        raise ValueError("Source extraction requires Python 3.12 or newer")
    if destination.is_symlink() or cache_dir.is_symlink():
        raise ValueError("Build destination must not be a symlink")
    tools = None if fetch_only else build_tools()
    destination.mkdir(parents=True, exist_ok=True)
    lock_digest = digest(LOCK)
    marker = destination / "source-lock.sha256"
    if marker.exists():
        if marker.read_text().strip() != lock_digest:
            raise ValueError("Existing build uses different pinned sources; choose a new directory")
    elif any(destination.iterdir()):
        raise ValueError("Use an empty isolated build destination")
    else:
        marker.write_text(lock_digest + "\n")
    if cache_dir != destination:
        check_source_lock(cache_dir, lock_digest)
    lock = json.loads(LOCK.read_text())
    downloads = cache_dir / "downloads"
    sources, prefix, logs = (destination / name for name in ("source", "prefix", "logs"))
    for path in (downloads, sources, prefix, logs):
        path.mkdir(exist_ok=True)
    for component in lock["components"]:
        fetch_archive(component, downloads)
    if fetch_only:
        return
    # Fetch-only caches may be reused after digest verification. Source/build
    # trees and installed outputs may not: a new manifest must never bless
    # edited cached headers, configure scripts or libraries.
    require_fresh_outputs(destination, sources, prefix)
    sdk, clang, pkg_config = tools
    env = build_environment(destination, prefix, sdk, clang, pkg_config)
    identity = {"builder_sha256": digest(Path(__file__)), "lock_sha256": digest(LOCK), "clang": subprocess.check_output([clang, "--version"], text=True).strip(), "sdk": sdk, "sdk_version": subprocess.check_output(["xcrun", "--show-sdk-version"], text=True).strip(), "pkg_config_version": subprocess.check_output([pkg_config, "--version"], text=True).strip(), "pkg_config_sha256": digest(Path(pkg_config).resolve()), "environment": env}
    identity_path = destination / "build-identity.json"
    identity_path.write_text(json.dumps(identity, indent=2) + "\n")
    commands = []
    for component in lock["components"]:
        print("Building dependency: " + component["name"], flush=True)
        source = sources / component["source_dir"]
        complete = logs / (component["name"] + ".complete")
        command = [str(source / "configure"), "--prefix=" + str(prefix), "--build=aarch64-apple-darwin", "--host=x86_64-apple-darwin", "--enable-shared", "--disable-static", *component["configure"]]
        commands.append(command)
        with tarfile.open(downloads / component["archive"]) as archive:
            archive.extractall(sources, filter="data")
        log = logs / (component["name"] + ".log")
        run(command, log, env, source, live_output)
        run(["make", "-j", str(jobs)], log, env, source, live_output)
        run(["make", "install"], log, env, source, live_output)
        complete.write_text(component["sha256"] + "\n")
    relocate(prefix)
    for component in lock["components"]:
        source = sources / component["source_dir"]
        for path in source.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.name.upper().startswith(("COPYING", "LICENSE", "NOTICE", "AUTHORS")):
                target = prefix / "notices" / component["name"] / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        for relative in EXTRA_NOTICES.get(component["name"], ()):
            target = prefix / "notices" / component["name"] / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
    (prefix / "notices/ATTRIBUTION.txt").write_text(
        "This software uses the FreeType library from the FreeType Project "
        "(https://freetype.org), under the FreeType License (FTL). "
        "Its full license, copyrights and contributed-code notices are retained "
        "in notices/freetype. Other libraries retain their accompanying terms.\n")
    manifest = {"schema": 1, "source_built": True, "architecture": "x86_64", "minimum_macos": "15.0", "lock_sha256": digest(LOCK), "source_archives": lock["components"], "features": lock["features"], "build_commands": commands, "build_identity": identity, "tool_versions": {"clang": identity["clang"], "sdk": identity["sdk_version"]}, "files": inventory(prefix)}
    (prefix / "dependency-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(prefix)
    print("Verified dependency prefix: " + str(prefix))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--verify", type=Path, metavar="PREFIX")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--cache-dir", type=Path,
                        help="Persistent source-download cache for interrupted build retries")
    parser.add_argument("--live-output", action="store_true",
                        help="Forward compiler output to a parent setup progress filter")
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    try:
        if args.verify:
            verify(args.verify)
            print("Verified dependency inputs")
        elif args.destination and 1 <= args.jobs <= 8:
            build(args.destination, args.jobs, args.fetch_only, args.cache_dir, args.live_output)
        else:
            parser.error("Use --verify PREFIX or --destination NEW_DIRECTORY with 1-8 jobs")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Dependency preparation stopped: {error}\n")


if __name__ == "__main__":
    main()
