#!/usr/bin/env python3

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Stage all three RC Docker images from existing, signed release artifacts.

Requires Python 3.9+, git, curl, GnuPG, bash, shasum and Docker Buildx (for --push).
No Maven, JDK, Rust toolchain or signing private key is needed.
Without --push, only prepare and verify the three Docker build contexts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile


DIST = "https://dist.apache.org/repos/dist/dev/fluss"
KEYS = "https://dist.apache.org/repos/dist/release/fluss/KEYS"
GIT_URL = "https://github.com/apache/fluss.git"
FLINK_VERSION = "1.20"
EXTRA_JARS = ("fluss-flink-1.20", "fluss-flink-tiering")


def run(command, cwd=None, env=None, capture=False):
    command = [str(arg) for arg in command]
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(
        command, cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def download(url, destination):
    """Reuse completed downloads; callers must reverify signed cached artifacts."""
    if destination.is_file() and destination.stat().st_size:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    run([
        "curl", "--fail", "--location", "--show-error", "--silent",
        "--proto", "=https", "--proto-redir", "=https",
        "--retry", "3", "--connect-timeout", "30", "--max-time", "1800",
        "--output", partial, url,
    ])
    if not partial.stat().st_size:
        raise ValueError("Empty download: " + url)
    partial.replace(destination)
    return destination


def sha512(path):
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(archive):
    checksum = Path(str(archive) + ".sha512").read_text()
    hashes = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{128}(?![0-9a-fA-F])", checksum)
    if len(hashes) != 1 or hashes[0].lower() != sha512(archive):
        raise ValueError("SHA-512 mismatch: " + str(archive))


def verify_signature(artifact, env):
    run([
        "gpg", "--no-options", "--batch", "--verify",
        str(artifact) + ".asc", artifact,
    ], env=env)


def extract_distribution(archive, destination, root_name):
    """Extract regular files/directories only, preserving executable permissions."""
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] != root_name
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unexpected archive member: " + member.name)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
    root = destination / root_name
    if not root.is_dir():
        raise ValueError("Missing archive root: " + root_name)
    return root


def check_gateway_commit(distribution, expected):
    actual = (distribution / "RELEASE_COMMIT").read_text().strip()
    if actual != expected:
        raise ValueError("Gateway commit mismatch: " + actual + " != " + expected)


def copy_jar(source, repo, module):
    if not source.is_file():
        raise ValueError("Required RC JAR is missing: " + str(source))
    destination = repo / module / "target"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination / source.name)


def image_commands(args, repo, output, release_commit):
    suffix = f"{args.version}-rc{args.rc}"
    for context, name, tag in (
        ("fluss", "fluss", suffix),
        ("fluss-gateway", "fluss-gateway", suffix),
        ("quickstart-flink", "fluss-quickstart-flink", f"{FLINK_VERSION}-{suffix}"),
    ):
        image = f"{args.namespace}/{name}:{tag}"
        metadata = output / (name + ".metadata.json")
        command = ["docker", "buildx", "build"]
        if args.builder:
            command += ["--builder", args.builder]
        command += [
            "--push", "--platform", "linux/amd64,linux/arm64",
            "--tag", image, "--metadata-file", str(metadata),
        ]
        if name == "fluss-gateway":
            command += [
                "--build-arg", "FLUSS_VERSION=" + args.version,
                "--build-arg", "VCS_REF=" + release_commit,
            ]
        command += [str(repo / "docker" / context)]
        yield image, metadata, command


def verify_manifest(manifest):
    platforms = {
        (entry.get("platform", {}).get("os"),
         entry.get("platform", {}).get("architecture"))
        for entry in manifest.get("manifests", [])
    }
    if not {("linux", "amd64"), ("linux", "arm64")}.issubset(platforms):
        raise ValueError("Remote image index is missing linux/amd64 or linux/arm64")


def remote_manifest(image):
    command = ["docker", "buildx", "imagetools", "inspect", image, "--raw"]
    return json.loads(run(command, capture=True))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 1.0.0")
    parser.add_argument("--rc", required=True, type=int, help="RC number, e.g. 3")
    parser.add_argument(
        "--nexus", required=True,
        help="Exact closed RC staging URL, ending in orgapachefluss-NNNN",
    )
    parser.add_argument(
        "--work-dir", type=Path, help="Download cache and isolated run directories"
    )
    parser.add_argument("--builder", help="Existing multi-platform Buildx builder")
    parser.add_argument(
        "--namespace", default="apache", help="Docker repository namespace (default: apache)"
    )
    parser.add_argument(
        "--push", action="store_true", help="Build, push and verify all three images"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-incubating)?", args.version):
        parser.error("--version must be a release version, not a snapshot")
    if args.rc < 1:
        parser.error("--rc must be positive")
    args.nexus = args.nexus.rstrip("/")
    if not re.fullmatch(
        r"https://repository\.apache\.org/content/repositories/orgapachefluss-[0-9]+",
        args.nexus,
    ):
        parser.error("--nexus must identify one exact Apache Fluss staging repository")
    return args


def main():
    args = parse_args()
    for program in ["git", "curl", "gpg", "bash", "shasum"] + (["docker"] if args.push else []):
        if not shutil.which(program):
            raise ValueError("Required executable not found: " + program)
    if args.push:
        inspect = ["docker", "buildx", "inspect"]
        if args.builder:
            inspect.append(args.builder)
        run(inspect + ["--bootstrap"])

    tag = f"v{args.version}-rc{args.rc}"
    work = args.work_dir or Path(f"fluss-docker-{args.version}-rc{args.rc}")
    work = work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    cache = work / "downloads"
    output = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    print("Run directory: " + str(output), flush=True)
    env = os.environ.copy()
    keyring = output / "gnupg"
    keyring.mkdir(mode=0o700)
    env["GNUPGHOME"] = str(keyring)
    # Never inherit the development bypass from a caller's shell.
    env["SKIP_GPG"] = "false"
    env["RELEASE_VERSION"] = args.version
    keys = download(KEYS, output / "KEYS")
    run(["gpg", "--no-options", "--batch", "--import", keys], env=env)

    # A fresh checkout keeps existing workspaces and locally generated files out.
    repo = output / "source"
    run(["git", "init", "--quiet", repo])
    run(["git", "fetch", "--depth=1", GIT_URL, f"refs/tags/{tag}:refs/tags/{tag}"], cwd=repo)
    run(
        ["git", "-c", "gpg.format=openpgp", "-c", "gpg.program=gpg", "verify-tag", tag],
        cwd=repo, env=env,
    )
    release_commit = run(
        ["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], cwd=repo, capture=True
    )
    run(["git", "checkout", "--quiet", "--detach", release_commit], cwd=repo)
    env["RELEASE_COMMIT"] = release_commit
    print(f"Verified RC tag: {tag} -> {release_commit}", flush=True)

    # Include the resolved commit so a changed remote tag cannot reuse old inputs.
    identity = {
        "version": args.version,
        "rc": args.rc,
        "nexus": args.nexus,
        "tag": tag,
        "commit": release_commit,
    }
    marker = work / "inputs.json"
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise ValueError("Work directory belongs to different RC inputs; use another --work-dir")
    marker.write_text(json.dumps(identity, indent=2) + "\n")

    # These two artifacts are NOT included in the Java binary tarball.
    for artifact in EXTRA_JARS:
        name = f"{artifact}-{args.version}.jar"
        base = f"{args.nexus}/org/apache/fluss/{artifact}/{args.version}/{name}"
        jar = download(base, cache / name)
        download(base + ".asc", Path(str(jar) + ".asc"))
        verify_signature(jar, env)
        copy_jar(jar, repo, "fluss-flink/" + artifact)

    base_url = f"{DIST}/fluss-{args.version}-rc{args.rc}"
    names = [f"fluss-{args.version}-bin"] + [
        f"fluss-gateway-{args.version}-bin-linux-{arch}" for arch in ("amd64", "arm64")
    ]
    for name in names:
        archive = cache / (name + ".tgz")
        for suffix in ("", ".asc", ".sha512"):
            download(base_url + "/" + archive.name + suffix, Path(str(archive) + suffix))
        verify_checksum(archive)
        verify_signature(archive, env)
        if name == names[0]:
            java = extract_distribution(archive, output / "java", f"fluss-{args.version}")
            shutil.copytree(java, repo / "docker/fluss/build-target")
        else:
            arch = name.rsplit("-", 1)[1]
            gateway = extract_distribution(archive, output / arch, name)
            check_gateway_commit(gateway, release_commit)
            shutil.copytree(gateway, repo / "docker/fluss-gateway/build-target" / arch)

    # Populate only the module outputs consumed by the RC's own prepare script.
    # Hudi is currently checked as a prerequisite, although not copied to the image.
    for plugin, artifact, module in (
        ("s3", "fluss-fs-s3", "fluss-filesystems"),
        ("paimon", "fluss-lake-paimon", "fluss-lake"),
        ("iceberg", "fluss-lake-iceberg", "fluss-lake"),
        ("hudi", "fluss-lake-hudi", "fluss-lake"),
    ):
        source = java / "plugins" / plugin / f"{artifact}-{args.version}.jar"
        copy_jar(source, repo, module + "/" + artifact)
    quickstart = repo / "docker/quickstart-flink"
    dockerfile = (quickstart / "Dockerfile").read_text()
    if not re.search(r"^FROM flink:1\.20\.", dockerfile, re.MULTILINE):
        raise ValueError("Quickstart Flink base version changed; review the script before staging")
    # This script only copies existing JARs and downloads its pinned third-party dependencies.
    run(["bash", "./prepare_build.sh"], cwd=quickstart, env=env)

    inputs = {
        **identity,
        "sha512": {
            str(path.relative_to(repo)): sha512(path)
            for path in sorted((repo / "docker").rglob("*.jar"))
        },
    }
    (output / "prepared-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    images = list(image_commands(args, repo, output, release_commit))
    (output / "build-commands.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + "\n".join(shlex.join(command) for _, _, command in images) + "\n"
    )
    if not args.push:
        print("Prepared all three contexts. No Docker build or push was performed.")
        print("Review " + str(output / "build-commands.sh"))
        print("Re-run with --push to build, publish and verify the remote indexes.")
        return

    for image, metadata, command in images:
        run(command)
        digest = json.loads(metadata.read_text())["containerimage.digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Missing image index digest for " + image)
        # Verify both the immutable pushed index and the published RC tag.
        pinned = image.rsplit(":", 1)[0] + "@" + digest
        remote = remote_manifest(pinned)
        verify_manifest(remote)
        tagged = remote_manifest(image)
        if tagged != remote:
            raise ValueError("Remote RC tag differs from the pushed index: " + image)
        run(["docker", "buildx", "imagetools", "inspect", image])
        with (output / "image-digests.txt").open("a") as report:
            report.write(image + " " + digest + "\n")
        print("Verified: " + image + " -> " + digest, flush=True)
    print("Completed all three images. Digests: " + str(output / "image-digests.txt"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
