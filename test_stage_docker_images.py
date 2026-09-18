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

"""Validate artifact rejection and publication plans without accessing a registry."""

from argparse import Namespace
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

import stage_docker_images as stage


class DockerStagingTest(unittest.TestCase):
    def test_checksums_accept_gnu_and_bsd_but_reject_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tgz"
            archive.write_bytes(b"signed release bytes")
            digest = hashlib.sha512(archive.read_bytes()).hexdigest()
            checksum = Path(str(archive) + ".sha512")
            for text in (digest + "  release.tgz\n", "SHA512 (release.tgz) = " + digest):
                checksum.write_text(text)
                stage.verify_checksum(archive)
            archive.write_bytes(b"corrupted release bytes")
            with self.assertRaisesRegex(ValueError, "SHA-512 mismatch"):
                stage.verify_checksum(archive)

    def test_extract_keeps_gateway_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "gateway.tgz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("gateway/bin/fluss-gateway")
                member.mode = 0o755
                member.size = 6
                bundle.addfile(member, io.BytesIO(b"binary"))
            result = stage.extract_distribution(archive, root / "extract", "gateway")
            binary = result / "bin/fluss-gateway"
            self.assertEqual(binary.read_bytes(), b"binary")
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)

    def test_reject_wrong_root_traversal_and_links_before_extraction(self):
        for name, kind in (
            ("other/bin/server", tarfile.REGTYPE),
            ("gateway/../../outside", tarfile.REGTYPE),
            ("/gateway/bin/server", tarfile.REGTYPE),
            ("gateway/link", tarfile.SYMTYPE),
            ("gateway/link", tarfile.LNKTYPE),
        ):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "invalid.tgz"
                with tarfile.open(archive, "w:gz") as bundle:
                    valid = tarfile.TarInfo("gateway/bin/server")
                    bundle.addfile(valid)
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "../../outside"
                    bundle.addfile(member)
                with self.assertRaisesRegex(ValueError, "Unexpected archive member"):
                    stage.extract_distribution(archive, root / "extract", "gateway")
                self.assertEqual(list((root / "extract").iterdir()), [])

    def test_gateway_must_match_recorded_rc_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RELEASE_COMMIT").write_text("a" * 40 + "\n")
            stage.check_gateway_commit(root, "a" * 40)
            with self.assertRaisesRegex(ValueError, "Gateway commit mismatch"):
                stage.check_gateway_commit(root, "b" * 40)

    def test_single_platform_manifest_is_not_a_success(self):
        manifest = {"manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}},
        ]}
        with self.assertRaisesRegex(ValueError, "missing"):
            stage.verify_manifest(manifest)
        manifest["manifests"].append({"platform": {"os": "linux", "architecture": "arm64"}})
        stage.verify_manifest(manifest)

    def test_only_rc_tags_are_published_with_both_platforms(self):
        args = Namespace(
            version="1.0.0", rc=3, namespace="example", builder="release"
        )
        images = list(stage.image_commands(
            args, Path("/tmp/source"), Path("/tmp/output"), "a" * 40
        ))
        self.assertEqual([image for image, _, _ in images], [
            "example/fluss:1.0.0-rc3",
            "example/fluss-gateway:1.0.0-rc3",
            "example/fluss-quickstart-flink:1.20-1.0.0-rc3",
        ])
        for _, _, command in images:
            self.assertIn("--push", command)
            self.assertEqual(command[command.index("--platform") + 1], "linux/amd64,linux/arm64")
            self.assertEqual(command[command.index("--builder") + 1], "release")
        self.assertIn("VCS_REF=" + "a" * 40, images[1][2])


if __name__ == "__main__":
    unittest.main()
