"""Which scans the server puts in the quality-check queue.

Requirements under test: the server reads a database that is already in the BoneHub format,
and "it must work on specific datasets not all datasets in the root".
"""

from __future__ import annotations

import json
import os
import unittest

from bonehub_format import SCHEMA_VERSION
from qc_server.config import QCServerConfig
from qc_server.models import REVIEWER

from tests.support import EARLIER_SERVER_ID, QCTestCase, write_mask


class IndexEligibilityTests(QCTestCase):
    def keys(self, store) -> list:
        return [ref.scan_key for ref in store._index]

    def test_only_segmentations_not_reviewed_yet_are_queued(self):
        self.builder.add_dataset(1)
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])  # nobody has reviewed it
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"], reviewed=["FEMUR_LEFT"])  # reviewed already
        self.builder.add_scan(1, 3, segmentation=[])  # no segmentation
        store = self.make_store()
        self.assertEqual(self.keys(store), ["ds-0001/sub-000001/scan-01"])
        self.assertEqual(store.stats().total_scans, 3)
        self.assertEqual(store.stats().eligible_scans, 1)

    def test_a_scan_with_any_label_not_reviewed_is_queued(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"], reviewed=["FEMUR_LEFT"])
        self.assertEqual(self.keys(self.make_store()), ["ds-0001/sub-000001/scan-01"])

    def test_every_scan_of_a_subject_is_queued_on_its_own(self):
        self.builder.add_scan(1, 1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 1, 2, segmentation=["FEMUR_LEFT"], reviewed=["FEMUR_LEFT"])
        self.builder.add_scan(1, 1, 3, segmentation=["TIBIA_LEFT"])
        store = self.make_store()
        self.assertEqual(self.keys(store), ["ds-0001/sub-000001/scan-01", "ds-0001/sub-000001/scan-03"])
        self.assertEqual(store.stats().total_scans, 3)

    def test_scans_without_a_segmentation_are_skipped_by_default(self):
        self.builder.add_scan(1, 1, segmentation=None)
        self.assertEqual(self.keys(self.make_store()), [])

    def test_scans_without_a_segmentation_can_be_queued_on_request(self):
        """So an editor can create a segmentation from scratch."""
        self.builder.add_scan(1, 1, segmentation=None)
        store = self.make_store(include_scans_without_segmentation=True)
        self.assertEqual(self.keys(store), ["ds-0001/sub-000001/scan-01"])
        self.assertFalse(store._index[0].has_segmentation)

    def test_a_scan_with_no_image_is_never_queued(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"], image=False)
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.keys(self.make_store()), ["ds-0001/sub-000002/scan-01"])

    def test_a_scan_of_a_subject_the_converter_has_not_finished_is_skipped(self):
        """The converter writes subject.json last: without it the subject's folder is incomplete."""
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        (self.builder.scan_folder(1, 2).parent / "subject.json").unlink()
        self.assertEqual(self.keys(self.make_store()), ["ds-0001/sub-000001/scan-01"])

    def test_reviewed_scans_can_be_queued_for_a_second_review(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"], reviewed=["FEMUR_LEFT"])
        second = self.make_store(eligible_review_states=["reviewed"])
        self.assertEqual(self.keys(second), ["ds-0001/sub-000002/scan-01"])
        both = self.make_store(eligible_review_states=["not_reviewed", "reviewed"])
        self.assertEqual(self.keys(both), ["ds-0001/sub-000001/scan-01", "ds-0001/sub-000002/scan-01"])

    def test_labels_are_queued_by_their_origin_when_the_server_says_so(self):
        self.builder.add_scan(1, 1, segmentation={"FEMUR_LEFT": "source"})
        self.builder.add_scan(1, 2, segmentation={"FEMUR_LEFT": "manual"})
        self.builder.add_scan(1, 3, segmentation={"FEMUR_LEFT": "source", "PATELLA_LEFT": "model:TotalSegmentator@2.4.0"})
        self.builder.add_scan(1, 4, segmentation={"FEMUR_LEFT": "model:Other@1.0"})
        self.assertEqual(len(self.keys(self.make_store())), 4, "every origin, by default")
        self.assertEqual(
            self.keys(self.make_store(eligible_origins=["model:TotalSegmentator"])), ["ds-0001/sub-000003/scan-01"]
        )
        self.assertEqual(
            self.keys(self.make_store(eligible_origins=["model"])),
            ["ds-0001/sub-000003/scan-01", "ds-0001/sub-000004/scan-01"],
        )
        self.assertEqual(self.keys(self.make_store(eligible_origins=["manual"])), ["ds-0001/sub-000002/scan-01"])

    def test_a_label_is_reviewed_only_while_its_segmentation_is_the_one_approved(self):
        """Converting the dataset again, or another tool rewriting the mask, leaves every label of
        the scan to be reviewed again."""
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"], reviewed=["FEMUR_LEFT", "FEMUR_RIGHT"])
        store = self.make_store()
        self.assertEqual(self.keys(store), [])
        self.assertEqual(self.reviewed(store), {"FEMUR_LEFT", "FEMUR_RIGHT"})

        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        store.refresh_index()
        self.assertEqual(self.keys(store), ["ds-0001/sub-000001/scan-01"])
        self.assertEqual(self.reviewed(store), set())

    def test_a_segmentation_copied_back_unchanged_is_still_the_one_approved(self):
        """A copy whose time moved -- restored from a backup, or moved to another disk -- is the same file."""
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"], reviewed=["FEMUR_LEFT"])
        path = self.builder.segmentation_file(1, 1)
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
        store = self.make_store()
        self.assertEqual(self.keys(store), [])
        self.assertEqual(self.reviewed(store), {"FEMUR_LEFT"})

    def test_the_latest_approval_of_a_scan_on_any_server_says_what_is_reviewed(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"], reviewed=["FEMUR_LEFT", "FEMUR_RIGHT"])
        done = self.dataset_root / ".bonehub_qc" / EARLIER_SERVER_ID / "cases_done.jsonl"
        later = json.loads(done.read_text(encoding="utf-8"))
        later.update(applied_at="2026-02-01T00:00:00Z", reviewed_labels=["FEMUR_LEFT"])
        (done.parent.parent / "qc_later_server").mkdir()
        (done.parent.parent / "qc_later_server" / "cases_done.jsonl").write_text(json.dumps(later) + "\n", encoding="utf-8")
        store = self.make_store()
        self.assertEqual(self.reviewed(store), {"FEMUR_LEFT"})
        self.assertEqual(self.keys(store), ["ds-0001/sub-000001/scan-01"])

    def test_a_done_file_of_another_layout_or_cut_short_is_passed_over(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"], reviewed=["FEMUR_LEFT"])
        done = self.dataset_root / ".bonehub_qc" / EARLIER_SERVER_ID / "cases_done.jsonl"
        with open(done, "a", encoding="utf-8") as f:
            f.write(json.dumps({"subject_key": "001_000001", "stage": "applied", "applied_at": "2027-01-01T00:00:00Z"}) + "\n")
            f.write('{"scan_key": "ds-0001/sub-000001/scan-01", "stage": "app')
        store = self.make_store()
        self.assertEqual(self.reviewed(store), {"FEMUR_LEFT"})
        self.assertEqual(self.keys(store), [])

    def test_the_queue_is_ordered_by_dataset_then_subject_then_scan(self):
        self.builder.add_scan(2, 5, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 9, 2, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 9, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        self.assertEqual(
            self.keys(self.make_store()),
            [
                "ds-0001/sub-000002/scan-01",
                "ds-0001/sub-000009/scan-01",
                "ds-0001/sub-000009/scan-02",
                "ds-0002/sub-000005/scan-01",
            ],
        )


class SchemaVersionTests(QCTestCase):
    """Only datasets written in this server's major version of the BoneHub format are served.

    In another major version the layout, scan.json and the segmentation format may all mean
    something else, so such a dataset is left alone entirely.
    """

    def server_log(self) -> str:
        return (self.state_dir / "server.log").read_text(encoding="utf-8")

    def test_a_dataset_of_the_current_format_is_served(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.make_store().stats().datasets, {1: 1})

    def test_a_dataset_of_an_older_minor_version_is_served(self):
        major = SCHEMA_VERSION.split(".")[0]
        self.builder.add_dataset(1, schema_version=f"{major}.0")
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.make_store().stats().eligible_scans, 1)

    def test_a_dataset_of_a_newer_minor_version_is_skipped(self):
        """It may hold what this server's format package cannot read."""
        major, minor = SCHEMA_VERSION.split(".")[:2]
        self.builder.add_dataset(1, schema_version=f"{major}.{int(minor) + 1}")
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.make_store().stats().eligible_scans, 0)

    def test_a_dataset_of_an_older_format_is_skipped(self):
        self.builder.add_dataset(1, schema_version="0.3.0")
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(2, 1, segmentation=["FEMUR_LEFT"])
        store = self.make_store()
        self.assertEqual(store.stats().datasets, {2: 1})
        self.assertEqual(store.stats().total_scans, 1)
        self.assertIn("ds-0001", self.server_log())
        self.assertIn("0.3.0", self.server_log())

    def test_a_dataset_that_records_no_format_version_is_skipped(self):
        self.builder.add_dataset(1, schema_version=None)
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.make_store().stats().eligible_scans, 0)
        self.assertIn("(not recorded)", self.server_log())

    def test_a_dataset_whose_info_file_is_broken_is_skipped(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(2, 1, segmentation=["FEMUR_LEFT"])
        (self.dataset_root / "ds-0002" / "dataset.json").write_text("{ truncated", encoding="utf-8")
        self.assertEqual(self.make_store().stats().datasets, {1: 1})

    def test_a_database_of_the_old_layout_offers_nothing(self):
        """Dataset_001 and its Subject_info file are BoneHub data schema 0.3: no dataset of this format."""
        old = self.dataset_root / "Dataset_001"
        old.mkdir(parents=True)
        (old / "Subject_info_001.json").write_text("[]", encoding="utf-8")
        self.builder.add_scan(2, 1, segmentation=["FEMUR_LEFT"])
        self.assertEqual(self.make_store().stats().datasets, {2: 1})


class DatasetScopeTests(QCTestCase):
    """"It must work on specific datasets, not all datasets in the root"."""

    def setUp(self) -> None:
        super().setUp()
        for dataset_id in (1, 2, 3):
            self.builder.add_scan(dataset_id, 1, segmentation=["FEMUR_LEFT"])

    def test_without_a_restriction_every_dataset_is_served(self):
        store = self.make_store()
        self.assertEqual(sorted(store.stats().datasets), [1, 2, 3])

    def test_the_server_can_be_pinned_to_chosen_datasets(self):
        store = self.make_store(allowed_dataset_ids=[1, 3])
        self.assertEqual(sorted(store.stats().datasets), [1, 3])
        self.assertEqual([ref.dataset_id for ref in store._index], [1, 3])

    def test_excluded_datasets_are_never_handed_out(self):
        store = self.make_store(allowed_dataset_ids=[2])
        _, key = store.create_user("alice")
        user = store.authenticate(key)
        self.assertEqual(store.next_scan(user, REVIEWER).dataset_id, 2)

    def test_excluded_scans_are_not_counted_as_total_either(self):
        store = self.make_store(allowed_dataset_ids=[1])
        self.assertEqual(store.stats().total_scans, 1)

    def test_a_reviewer_can_be_restricted_further_than_the_server(self):
        store = self.make_store(allowed_dataset_ids=[1, 2])
        store.create_user("alice", allowed_dataset_ids=[2])
        _, key = store.create_user("bob")
        alice = store._users["alice"]
        self.assertEqual(store.next_scan(alice, REVIEWER).dataset_id, 2)
        self.assertEqual(store.next_scan(store.authenticate(key), REVIEWER).dataset_id, 1)

    def test_a_reviewer_cannot_reach_beyond_the_servers_own_restriction(self):
        """A per-user allowance widens nothing: the server's list is the outer bound."""
        store = self.make_store(allowed_dataset_ids=[1])
        store.create_user("alice", allowed_dataset_ids=[2, 3])
        self.assertFalse(store._user_may_access(store._users["alice"], 2))
        with self.assertRaises(Exception):
            store.next_scan(store._users["alice"])


class MalformedDatasetTests(QCTestCase):
    """A broken dataset folder must not take the whole server down."""

    def test_a_folder_without_a_dataset_json_is_skipped(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        (self.dataset_root / "ds-0002").mkdir(parents=True)
        store = self.make_store()
        self.assertEqual(store.stats().eligible_scans, 1)

    def test_an_unparsable_scan_json_skips_that_scan_alone(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        (self.builder.scan_folder(1, 2) / "scan.json").write_text("{ truncated", encoding="utf-8")
        store = self.make_store()
        self.assertEqual([ref.scan_key for ref in store._index], ["ds-0001/sub-000001/scan-01"])
        self.assertIn("ds-0001/sub-000002/scan-01", (self.state_dir / "server.log").read_text(encoding="utf-8"))

    def test_folders_not_named_as_the_format_names_them_are_skipped(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        (self.dataset_root / "ds-notanumber").mkdir(parents=True)
        (self.dataset_root / "ds-0001" / "sub-1").mkdir()
        (self.dataset_root / "ds-0001" / "sub-000001" / "scan-1").mkdir()
        (self.dataset_root / "ds-0001" / "sub-000001" / "mesh").mkdir()
        self.assertEqual(self.make_store().stats().eligible_scans, 1)

    def test_unrelated_folders_in_the_root_are_ignored(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        (self.dataset_root / "scratch").mkdir(parents=True)
        (self.dataset_root / "README.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(self.make_store().stats().eligible_scans, 1)

    def test_a_dataset_root_that_does_not_exist_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.track(
                __import__("qc_server.store", fromlist=["QCStore"]).QCStore(
                    dataset_root=self.tmp_path / "nowhere",
                    credentials_dir=self.credentials_dir,
                )
            )


class IndexRefreshTests(QCTestCase):
    def test_scans_added_on_disk_appear_after_a_refresh(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        store = self.make_store(index_refresh_seconds=3600)
        self.assertEqual(store.stats().eligible_scans, 1)

        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        store.refresh_index()
        self.assertEqual(store.stats().eligible_scans, 2)

    def test_a_zero_ttl_rebuilds_the_index_on_every_request(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        store = self.make_store(index_refresh_seconds=0)
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        self.assertEqual(store.stats().eligible_scans, 2)


class StateFolderTests(QCTestCase):
    """The server's non-secret state lives in a folder of its own inside the database."""

    def test_the_state_folder_is_laid_out_on_first_start(self):
        self.default_dataset()
        store = self.make_store()
        self.assertEqual(store.state_dir, self.dataset_root / ".bonehub_qc" / "qc_test_server")
        for name in ["config.json", "session.json"]:
            self.assertTrue((self.state_dir / name).exists(), name)
        for name in ["backups", "tmp"]:
            self.assertTrue((self.state_dir / name).is_dir(), name)
        for name in ["server_private_key", "admin_key"]:
            self.assertTrue((self.credentials_dir / name).exists(), name)
            self.assertFalse((self.state_dir / name).exists(), f"{name} must not be on the share")

    def test_the_config_file_is_written_so_it_can_be_edited_later(self):
        self.default_dataset()
        self.make_store(lease_ttl_seconds=120)
        self.assertEqual(QCServerConfig.load(self.state_dir / "config.json").lease_ttl_seconds, 120)

    def test_the_state_folder_is_not_mistaken_for_a_dataset(self):
        self.default_dataset()
        store = self.make_store()
        self.assertEqual(sorted(store.stats().datasets), [1])


if __name__ == "__main__":
    unittest.main()
