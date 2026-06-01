import unittest
from pathlib import Path
from unittest import mock

from pyartcd.pipelines.build_conforma_verify import (
    BuildConformaVerifyPipeline,
    ComponentMeta,
    _build_digest_to_name,
    _pullspec_lookup_keys,
)

SAMPLE_EC_LOG = """
✕ [Violation] base_image_registries.base_image_permitted
  ImageRef: quay.io/redhat-user-workloads/ocp-art-tenant/art-images@sha256:deadbeef
  Reason: Base image is not permitted
  Title: Base image registry is forbidden

✕ [Violation] base_image_registries.base_image_permitted
  ImageRef: quay.io/org/ose-5-0-ironic@sha256:abc123def
  Reason: Base image is not permitted
  Title: Base image registry is forbidden
"""


class TestPullspecLookupKeys(unittest.TestCase):
    def test_full_pullspec_and_digest_variants(self):
        pullspec = "quay.io/org/img@sha256:abc123def"
        keys = _pullspec_lookup_keys(pullspec)
        self.assertIn(pullspec, keys)
        self.assertIn("sha256:abc123def", keys)
        self.assertIn("abc123def", keys)

    def test_empty_pullspec(self):
        self.assertEqual(_pullspec_lookup_keys(""), [])


class TestBuildDigestToName(unittest.TestCase):
    def test_maps_all_digest_keys(self):
        batch = [
            {
                "name": "ose-5-0-ironic",
                "containerImage": "quay.io/org/ose-5-0-ironic@sha256:abc123def",
            }
        ]
        lookup = _build_digest_to_name(batch)
        self.assertEqual(lookup["sha256:abc123def"], "ose-5-0-ironic")
        self.assertEqual(lookup["abc123def"], "ose-5-0-ironic")


class TestParseViolationsFromLog(unittest.TestCase):
    def setUp(self):
        self.name_to_meta = {
            "ose-5-0-ironic": ComponentMeta(
                nvr="openshift-5.0-ironic-rhel-9.v5.0.0-20260601",
                konflux_name="ose-5-0-ironic",
                metadata_name="ironic",
                snapshot_pullspec="quay.io/org/ose-5-0-ironic@sha256:abc123def",
            ),
        }
        self.batch = [
            {
                "name": "ose-5-0-ironic",
                "containerImage": "quay.io/org/ose-5-0-ironic@sha256:abc123def",
            },
            {
                "name": "ose-5-0-other",
                "containerImage": "quay.io/org/art-images@sha256:feedface",
            },
        ]
        self.digest_to_name = _build_digest_to_name(self.batch)

    def test_resolved_violation_includes_nvr(self):
        violations = BuildConformaVerifyPipeline._parse_violations_from_log(
            SAMPLE_EC_LOG,
            self.digest_to_name,
            self.name_to_meta,
            batch_idx=3,
        )
        ironic = next(v for v in violations if v.get("nvr"))
        self.assertEqual(ironic["nvr"], "openshift-5.0-ironic-rhel-9.v5.0.0-20260601")
        self.assertEqual(ironic["konflux_name"], "ose-5-0-ironic")
        self.assertFalse(ironic["unresolved"])
        self.assertFalse(ironic["pullspec_mismatch"])

    def test_unresolved_art_images_violation(self):
        violations = BuildConformaVerifyPipeline._parse_violations_from_log(
            SAMPLE_EC_LOG,
            self.digest_to_name,
            self.name_to_meta,
            batch_idx=3,
        )
        unresolved = next(v for v in violations if v["unresolved"])
        self.assertIn("art-images", unresolved["image_ref"])
        self.assertIsNone(unresolved["nvr"])
        self.assertEqual(unresolved["batch"], 3)

    def test_pullspec_mismatch_when_resolved_name_differs_from_snapshot(self):
        violation = BuildConformaVerifyPipeline._enrich_violation(
            konflux_name="ose-5-0-ironic",
            image_ref="quay.io/org/ose-5-0-ironic@sha256:differentdigest",
            rule="base_image_registries.base_image_permitted",
            title="forbidden",
            reason="not allowed",
            name_to_meta=self.name_to_meta,
            batch_idx=1,
        )
        self.assertTrue(violation["pullspec_mismatch"])
        self.assertEqual(violation["nvr"], "openshift-5.0-ironic-rhel-9.v5.0.0-20260601")
        self.assertFalse(violation["unresolved"])


class TestLogViolationSummary(unittest.TestCase):
    def test_logs_nvr_lines(self):
        pipeline = BuildConformaVerifyPipeline(
            runtime=mock.MagicMock(dry_run=False, working_dir=Path("/tmp/conforma-verify-test")),
            version="5.0",
            assembly="stream",
            builds=[],
        )
        violations = {
            "openshift-5.0-ironic-rhel-9.v5.0.0-20260601": [
                {
                    "component_name": "ose-5-0-ironic",
                    "konflux_name": "ose-5-0-ironic",
                    "nvr": "openshift-5.0-ironic-rhel-9.v5.0.0-20260601",
                    "metadata_name": "ironic",
                    "snapshot_pullspec": "quay.io/org/ose-5-0-ironic@sha256:abc",
                    "image_ref": "quay.io/org/ose-5-0-ironic@sha256:abc",
                    "rule": "base_image_registries.base_image_permitted",
                    "title": "forbidden",
                    "reason": "not allowed",
                    "batch": 1,
                    "unresolved": False,
                    "pullspec_mismatch": False,
                }
            ]
        }
        failed_batches = [{"batch": 1, "plr_url": "https://example/plr"}]

        with self.assertLogs(pipeline.logger, level="ERROR") as captured:
            pipeline._log_violation_summary(violations, failed_batches)

        output = "\n".join(captured.output)
        self.assertIn("NVR: openshift-5.0-ironic-rhel-9.v5.0.0-20260601", output)
        self.assertIn("Component: ose-5-0-ironic", output)


if __name__ == "__main__":
    unittest.main()
