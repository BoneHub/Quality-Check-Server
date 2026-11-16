"""The administrator's approval, the one step that writes into the dataset, and the other
things the administrator does with a scan in progress.

Requirements under test: "in admin panel, admin can approve all the submission and only after
admin approval, things are overwritten in the bonehub dataset"; "in admin panel, i would like
to have filter to filter the submission having a specific keyword in them, and then maybe i
want to approve all those scans with a remark, so that the remark get written in the
Subject_info_xxx.json for those filtered and approved scans" -- in the BoneHub format, the
scan's scan.json.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from bonehub_format import validate

from qc_server import workflow
from qc_server.config import QCServerConfig
from qc_server.models import EDITOR, REVIEWER
from qc_server.store import QCError

from tests.support import QCTestCase, labels_in_mask, write_mask
from tests.test_api import ApiTestCase

KEY = "ds-0001/sub-000001/scan-01"


class ApprovalTestCase(QCTestCase):
    """One scan whose two labels nobody has reviewed yet, a reviewer and an editor."""

    config: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.prepare()
        self.store = self.make_store(**self.config)
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        self.eddie = self.store.create_user("eddie", roles=[EDITOR])[0]
        self.original = self.builder.segmentation_file(1, 1).read_bytes()

    def prepare(self) -> None:
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"])

    def origins(self, subject_id: int = 1) -> dict:
        """The labels scan.json gives the scan's segmentation, with their origins."""
        return self.builder.origins(1, subject_id)

    def remarks(self, subject_id: int = 1) -> str | None:
        return self.builder.scan_info(1, subject_id).get("remarks")

    def given_remarks(self, remarks: str) -> None:
        """Remarks in scan.json already, as a converter writes them."""
        path = self.builder.scan_folder(1, 1) / "scan.json"
        info = json.loads(path.read_text(encoding="utf-8"))
        info["remarks"] = remarks
        path.write_text(json.dumps(info, indent=4), encoding="utf-8")

    def correct_right_femur(self, store=None) -> None:
        """rita rejects the right femur, eddie corrects it, and rita accepts the correction."""
        store = store or self.store
        self.review(store, self.rita, rejected=["FEMUR_RIGHT"])
        self.edit(store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"], comment="head redrawn")
        self.review(store, self.rita)


class ApproveAcceptedSubjectTests(ApprovalTestCase):
    def test_accepted_labels_become_reviewed_and_the_files_stay_as_they_are(self):
        self.review(self.store, self.rita)
        before = self.dataset_state()
        outcome = self.store.approve(KEY)
        self.assertEqual(outcome.updated_labels, {}, "scan.json has nothing to change")
        self.assertEqual(outcome.reviewed_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertFalse(outcome.segmentation_written)
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})
        self.assertEqual(self.dataset_state()[0], before[0], "neither the segmentation nor scan.json is written")
        self.assertEqual(self.store.case_of(KEY).stage, "applied")

    def test_the_approved_database_is_valid_in_the_bonehub_format(self):
        """A correction, a bone added, a bone taken out and a remark: what the approval leaves
        passes the format's own validator, logs and the server's folder included."""
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        store = self.make_store()
        self.review(store, self.rita, rejected=["FEMUR_RIGHT", "TIBIA_LEFT"], missing=["SACRUM"])
        self.edit(store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"], grown=["FEMUR_RIGHT"])
        self.review(store, self.rita)
        store.approve(KEY, remark="Hip implant")
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual", "SACRUM": "manual"})
        self.assertEqual(validate(self.dataset_root), [])

    def test_the_approval_is_logged_in_the_databases_logs(self):
        self.review(self.store, self.rita, comment="clean")
        self.store.approve(KEY)
        self.assertEqual(self.builder.dataset_log(1), self.dataset_root / "logs" / "ds-0001_qualitycheck.log")
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn(f"Scan {KEY} approved", log)
        self.assertIn("accepted by 'rita'", log)

    def test_the_audit_trail_says_who_stood_behind_each_label(self):
        self.correct_right_femur()
        self.store.approve(KEY)
        entry = self.store.audit.read_recent(kind="approved")[0]
        self.assertEqual(entry["verdicts"]["FEMUR_LEFT"]["by"], "rita")
        self.assertEqual(entry["verdicts"]["FEMUR_RIGHT"]["edited_by"], "eddie")
        self.assertTrue(entry["segmentation_written"])

    def test_other_scans_and_fields_are_left_as_they_were(self):
        """The subject's other scan, its subject.json, and the fields of scan.json besides the labels."""
        self.builder.add_scan(1, 1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"], age=44, subject={"gender": "F"})
        self.builder.add_scan(1, 1, 2, segmentation=["FEMUR_LEFT"], age=45)
        other = self.builder.scan_folder(1, 1, 2)
        before = {path.name: path.read_bytes() for path in other.iterdir()}
        subject_json = (other.parent / "subject.json").read_bytes()
        store = self.make_store()
        self.correct_right_femur(store)
        store.approve(KEY, remark="Hip implant")
        self.assertEqual({path.name: path.read_bytes() for path in other.iterdir()}, before)
        self.assertEqual((other.parent / "subject.json").read_bytes(), subject_json)
        info = self.builder.scan_info(1, 1)
        self.assertEqual((info["age"], info["modality"], info["source_scan_id"]), (44, "CT", "case_1/scan_1"))
        self.assertEqual(self.reviewed(store, 1, 1, 2), set(), "the other scan is not reviewed")

    def test_mesh_and_nurbs_made_from_a_label_the_approval_redrew_are_reported(self):
        """They were made from the old segmentation; the server leaves them, and says so."""
        self.builder.add_scan(
            1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"],
            mesh={"FEMUR_LEFT": "derived:bonehub-mesher@1.0", "FEMUR_RIGHT": "derived:bonehub-mesher@1.0"},
            nurbs={"FEMUR_RIGHT": "derived:mesh2nurbs-rhino3d@1.0"},
        )
        store = self.make_store()
        self.correct_right_femur(store)
        outcome = store.approve(KEY)
        self.assertEqual(outcome.outdated, {"mesh": ["FEMUR_RIGHT"], "nurbs": ["FEMUR_RIGHT"]})
        self.assertIn("out of date", outcome.message)
        self.assertIn("the mesh of FEMUR_RIGHT", self.builder.dataset_log(1).read_text(encoding="utf-8"))
        self.assertEqual(self.builder.scan_info(1, 1)["mesh"]["FEMUR_RIGHT"], "derived:bonehub-mesher@1.0")


class LabelOriginTests(QCTestCase):
    """What an approval writes for each kind of label (more in test_submission's OriginsOnApprovalTests)."""

    def approve(self, segmentation, painted=None, **config):
        self.builder.add_scan(1, 1, segmentation=segmentation, painted=painted)
        store = self.make_store(**config)
        rita = store.create_user("rita", roles=[REVIEWER])[0]
        self.review(store, rita)
        return store, store.approve(KEY)

    def test_a_label_not_under_review_is_left_as_it_was(self):
        """Only model predictions are queued on; the source's femur is neither reviewed nor changed."""
        store, outcome = self.approve(
            {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "model:TotalSegmentator@2.4.0"}, eligible_origins=["model"]
        )
        self.assertEqual(outcome.reviewed_labels, ["FEMUR_RIGHT"])
        self.assertEqual(
            self.builder.origins(1, 1), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "model:TotalSegmentator@2.4.0"}
        )

    def test_a_listed_label_that_is_not_painted_is_taken_out(self):
        store, outcome = self.approve(["FEMUR_LEFT", "FEMUR_RIGHT"], painted=["FEMUR_LEFT"])
        self.assertEqual(outcome.updated_labels, {"FEMUR_RIGHT": None})
        self.assertEqual(self.builder.origins(1, 1), {"FEMUR_LEFT": "source"})
        self.assertEqual(validate(self.dataset_root), [], "scan.json and the segmentation agree again")


class ApproveCorrectionTests(ApprovalTestCase):
    def test_the_correction_replaces_the_datasets_segmentation(self):
        staged = self.state_dir / "staged" / KEY / "segmentation.seg.nrrd"
        self.correct_right_femur()
        corrected = staged.read_bytes()
        outcome = self.store.approve(KEY)
        self.assertTrue(outcome.segmentation_written)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), corrected)
        self.assertFalse(staged.exists(), "the correction moved into the dataset")
        self.assertEqual(outcome.updated_labels, {"FEMUR_RIGHT": "manual"})
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual"})
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_the_datasets_segmentation_is_backed_up_first(self):
        self.correct_right_femur()
        outcome = self.store.approve(KEY)
        self.assertEqual(Path(outcome.backup_path).read_bytes(), self.original)
        self.assertEqual(Path(outcome.backup_path).parent, self.state_dir / "backups" / KEY)
        self.assertTrue(Path(outcome.backup_path).name.startswith("segmentation_"))
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_backups_can_be_switched_off(self):
        store = self.make_store(keep_segmentation_backups=False)
        self.correct_right_femur(store)
        self.assertIsNone(store.approve(KEY).backup_path)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_a_label_the_editor_removed_is_taken_out_of_scan_json(self):
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.edit(self.store, self.eddie, ["FEMUR_LEFT"])
        self.review(self.store, self.rita)  # agrees to the removal
        self.store.approve(KEY)
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source"})
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT"})
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT"})

    def test_a_segmentation_made_where_there_was_none_is_written(self):
        self.builder.add_scan(1, 2, segmentation=None)
        store = self.make_store(include_scans_without_segmentation=True, edits_need_review=False)
        self.edit(store, self.eddie, ["FEMUR_LEFT"])
        self.assertEqual(store.case_of("ds-0001/sub-000002/scan-01").stage, "approval")
        outcome = store.approve("ds-0001/sub-000002/scan-01")
        self.assertIsNone(outcome.backup_path, "there was nothing to back up")
        self.assertEqual(self.builder.labels_in_segmentation(1, 2), {"FEMUR_LEFT"})
        self.assertEqual(self.origins(2), {"FEMUR_LEFT": "manual"})
        self.assertEqual(self.reviewed(store, 1, 2), {"FEMUR_LEFT"}, "the editor vouched for it")


class ApprovalRefusalTests(ApprovalTestCase):
    def assert_refused(self, fragment: str, status: int = 409) -> None:
        before = self.dataset_state()
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY)
        self.assertEqual(ctx.exception.status_code, status)
        self.assertIn(fragment, ctx.exception.message)
        self.assertEqual(self.dataset_state(), before)

    def test_a_subject_not_waiting_for_approval_is_refused(self):
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.assert_refused("an editor")

    def test_a_subject_nobody_judged_is_unknown(self):
        self.assert_refused("Nobody", status=404)

    def test_approving_twice_is_refused(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assert_refused("approved")

    def test_a_segmentation_written_meanwhile_is_not_overwritten(self):
        """Another server or a converter wrote the dataset's file after the review began."""
        self.review(self.store, self.rita)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_LEFT"])
        self.assert_refused("changed after")
        self.assertEqual(self.store.case_of(KEY).stage, "approval")

    def test_a_segmentation_only_touched_meanwhile_is_still_approved(self):
        """Copied, or restored with a new time: the same content is the same file."""
        self.review(self.store, self.rita)
        path = self.builder.segmentation_file(1, 1)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))
        self.store.approve(KEY)
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_a_dataset_converted_again_in_another_format_version_is_not_written(self):
        self.review(self.store, self.rita)
        self.builder.add_dataset(1, schema_version="2.0")
        self.assert_refused("2.0")

    def test_a_failed_scan_json_write_puts_the_segmentation_back(self):
        """The dataset is not left half approved: the correction goes back to wait."""
        self.correct_right_femur()
        staged = self.state_dir / "staged" / KEY / "segmentation.seg.nrrd"
        corrected = staged.read_bytes()
        with mock.patch.object(self.store, "_write_scan_info", side_effect=OSError("the share went away")):
            with self.assertRaises(OSError):
                self.store.approve(KEY)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)
        self.assertEqual(staged.read_bytes(), corrected)
        self.assertEqual(self.store.case_of(KEY).stage, "approval")
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "source"})
        self.assertEqual(self.reviewed(self.store), set())
        self.store.approve(KEY)  # and once the share is back, it goes through
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), corrected)
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual"})


class RemarkTextTests(unittest.TestCase):
    """How a remark is written among a scan's remarks."""

    def test_a_remark_is_tagged_once_on_one_line(self):
        self.assertEqual(workflow.tagged_remark(" hip\n implant "), "QC: hip implant")
        self.assertEqual(workflow.tagged_remark("qc:Hip implant"), "QC: Hip implant")
        self.assertEqual(workflow.tagged_remark("QCT artefacts"), "QC: QCT artefacts")
        for nothing in (None, "", "  ", "QC: "):
            self.assertIsNone(workflow.tagged_remark(nothing), repr(nothing))

    def test_a_remark_goes_after_the_others_unless_it_is_one_of_them(self):
        self.assertEqual(workflow.with_remark(None, "QC: Hip implant"), "QC: Hip implant")
        self.assertEqual(
            workflow.with_remark("3 fractures at SACRUM", "QC: Hip implant"), "3 fractures at SACRUM; QC: Hip implant"
        )
        self.assertIsNone(workflow.with_remark("3 fractures at SACRUM;QC:  hip IMPLANT", "QC: Hip implant"))
        self.assertIsNone(workflow.with_remark("QC: a; b; QC: c", "QC: a; b"), "a remark of its own with a ';'")
        self.assertEqual(
            workflow.with_remark("QC: Hip implant, left", "QC: Hip implant"), "QC: Hip implant, left; QC: Hip implant"
        )


class RemarkTests(ApprovalTestCase):
    """A remark added to the scan's scan.json as it is approved."""

    def test_the_remark_is_written_with_its_tag(self):
        self.review(self.store, self.rita, comment="hip implant")
        outcome = self.store.approve(KEY, remark="Hip implant")
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "source"})
        self.assertTrue(outcome.remark_added)
        self.assertIn("'QC: Hip implant' added to its remarks", outcome.message)

    def test_the_remarks_the_converter_wrote_are_kept(self):
        self.given_remarks("Metal artifact present")
        self.review(self.store, self.rita)
        self.store.approve(KEY, remark="Hip implant")
        self.assertEqual(self.remarks(), "Metal artifact present; QC: Hip implant")

    def test_a_remark_there_already_is_not_added_again(self):
        self.given_remarks("Metal artifact present; qc:  hip IMPLANT")
        self.review(self.store, self.rita)
        outcome = self.store.approve(KEY, remark="Hip implant")
        self.assertFalse(outcome.remark_added)
        self.assertEqual(self.remarks(), "Metal artifact present; qc:  hip IMPLANT")
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"}, "the approval itself goes ahead")

    def test_a_blank_remark_leaves_the_remarks_alone(self):
        self.given_remarks("Metal artifact present")
        self.review(self.store, self.rita)
        self.store.approve(KEY, remark="   ")
        self.assertEqual(self.remarks(), "Metal artifact present")

    def test_a_remark_too_long_is_refused_and_nothing_is_written(self):
        self.review(self.store, self.rita)
        before = self.dataset_state()
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY, remark="x" * 600)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.dataset_state(), before)
        self.assertEqual(self.store.case_of(KEY).stage, "approval")

    def test_the_remark_is_recorded_with_the_approval(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY, remark="Hip implant")
        step = self.store.case_of(KEY).events[-1]
        self.assertEqual((step.action, step.comment, step.details["remark_added"]), ("approve", "QC: Hip implant", True))
        self.assertEqual(self.store.audit.read_recent(kind="approved")[0]["remark"], "QC: Hip implant")
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("'QC: Hip implant' added to its remarks", log)


class ApproveUnacceptedTests(ApprovalTestCase):
    """A scan approved before it waits for approval: what a reviewer accepted becomes reviewed,
    and every other label stays as it was."""

    def reject_right_femur(self, comment: str | None = "hip implant") -> None:
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"], comment=comment)

    def test_only_the_labels_a_reviewer_accepted_become_reviewed(self):
        self.reject_right_femur()
        outcome = self.store.approve(KEY, remark="Hip implant", allow_unaccepted=True)
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT"})
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(outcome.case.stage, "applied")
        self.assertEqual(outcome.case.events[-1].details["not_accepted"], ["FEMUR_RIGHT"])
        self.assertIn("1 label(s) nobody accepted are not reviewed", outcome.message)

    def test_it_is_refused_unless_asked_for(self):
        self.reject_right_femur()
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY, remark="Hip implant")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.reviewed(self.store), set())
        self.assertIsNone(self.remarks())

    def test_a_scan_rejected_as_a_whole_has_nothing_reviewed(self):
        assignment = self.store.next_scan(self.rita, REVIEWER)
        self.store.submit(assignment.assignment_id, self.rita, False, None, comment="hip implant")
        self.store.approve(KEY, remark="Hip implant", allow_unaccepted=True)
        self.assertEqual(self.reviewed(self.store), set())
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "source"})
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)

    def test_a_label_reviewed_already_is_never_unreviewed(self):
        """A label reviewed on the database already, and rejected in a second review, stays reviewed."""
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"], reviewed=["FEMUR_LEFT"])
        store = self.make_store(eligible_review_states=["not_reviewed", "reviewed"])
        self.review(store, self.rita)  # scan 1
        assignment = store.next_scan(self.rita, REVIEWER)
        store.submit(assignment.assignment_id, self.rita, False, None, comment="hip implant")
        store.approve("ds-0001/sub-000002/scan-01", allow_unaccepted=True)
        self.assertEqual(self.reviewed(store, 1, 2), {"FEMUR_LEFT"})

    def test_the_approval_says_what_nobody_accepted(self):
        self.reject_right_femur()
        self.store.approve(KEY, allow_unaccepted=True)
        entry = self.store.audit.read_recent(kind="approved")[0]
        self.assertEqual((entry["from_stage"], entry["not_accepted"]), ("edit", ["FEMUR_RIGHT"]))
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("while it waited for an editor", log)
        self.assertIn("nobody accepted them: FEMUR_RIGHT (rejected, by 'rita')", log)

    def test_a_correction_nobody_reviewed_is_written_but_not_marked_reviewed(self):
        self.reject_right_femur()
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        self.assertEqual(self.store.case_of(KEY).stage, "review")
        corrected = (self.state_dir / "staged" / KEY / "segmentation.seg.nrrd").read_bytes()
        outcome = self.store.approve(KEY, allow_unaccepted=True)
        self.assertTrue(outcome.segmentation_written)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), corrected)
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual"})
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT"})

    def test_a_subject_an_editor_gave_up_on_can_be_approved(self):
        self.reject_right_femur()
        assignment = self.store.next_scan(self.eddie, EDITOR)
        self.store.submit(assignment.assignment_id, self.eddie, False, None, comment="an implant, nothing to fix")
        self.store.approve(KEY, remark="Hip implant", allow_unaccepted=True)
        self.assertEqual(self.store.case_of(KEY).stage, "applied")
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT"})

    def test_a_closed_subject_can_be_approved(self):
        self.reject_right_femur()
        self.store.close_case(KEY, "implant")
        self.store.approve(KEY, allow_unaccepted=True)
        self.assertEqual(self.make_store().case_of(KEY).stage, "applied", "and stays approved after a restart")

    def test_a_subject_in_somebodys_hands_is_not_approved(self):
        self.reject_right_femur()
        self.store.next_scan(self.eddie, EDITOR)
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY, allow_unaccepted=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("eddie", ctx.exception.message)
        self.assertEqual(self.reviewed(self.store), set())

    def test_a_subject_that_changed_since_it_was_listed_is_not_approved(self):
        self.reject_right_femur()
        seen = self.store.case_of(KEY).revision
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY, allow_unaccepted=True, revision=seen)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("changed after you listed it", ctx.exception.message)
        self.assertEqual(self.store.case_of(KEY).stage, "review")

    def test_a_subject_being_approved_is_handed_to_nobody(self):
        self.reject_right_femur()
        self.store._applying.add(KEY)
        with self.assertRaises(QCError) as ctx:
            self.store.next_scan(self.eddie, EDITOR)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_late_verdict_on_a_subject_being_approved_is_refused(self):
        """An editor whose lease ran out uploads while the approval is being written."""
        self.reject_right_femur()
        assignment = self.store.next_scan(self.eddie, EDITOR)
        assignment.expires_at = "2000-01-01T00:00:00Z"
        self.store.stats()  # the lease expires
        self.store._applying.add(KEY)
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, self.eddie, True, upload)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("approving", ctx.exception.message)
        self.assertEqual(self.store.case_of(KEY).stage, "edit")


class ApproveAllTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_scans=4)
        self.store = self.make_store()
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        for _ in range(4):
            self.review(self.store, self.rita)

    def test_the_subjects_named_are_approved_with_the_remark(self):
        results = self.store.approve_all(["ds-0001/sub-000001/scan-01", "ds-0001/sub-000003/scan-01"], remark="Hip implant")
        self.assertEqual([(r["approved"], r["remark_added"]) for r in results], [(True, True)] * 2)
        remarks = [self.builder.scan_info(1, subject_id).get("remarks") for subject_id in range(1, 5)]
        self.assertEqual(remarks, ["QC: Hip implant", None, "QC: Hip implant", None])
        self.assertEqual(self.store.case_of("ds-0001/sub-000002/scan-01").stage, "approval")

    def test_a_remark_too_long_approves_nothing(self):
        with self.assertRaises(QCError):
            self.store.approve_all(remark="x" * 600)
        self.assertEqual(self.store.stats().awaiting_approval, 4)

    def test_a_subject_changed_since_it_was_listed_is_left_out(self):
        seen = {key: self.store.case_of(key).revision for key in ("ds-0001/sub-000001/scan-01", "ds-0001/sub-000002/scan-01")}
        self.store.return_case("ds-0001/sub-000002/scan-01", "review")
        self.review(self.store, self.rita)  # waits for approval again, but not as it was listed
        results = {r["scan_key"]: r for r in self.store.approve_all(list(seen), revisions=seen)}
        self.assertTrue(results["ds-0001/sub-000001/scan-01"]["approved"])
        self.assertFalse(results["ds-0001/sub-000002/scan-01"]["approved"])
        self.assertIn("changed after you listed it", results["ds-0001/sub-000002/scan-01"]["message"])

    def test_every_subject_waiting_for_approval_is_approved(self):
        results = self.store.approve_all()
        self.assertEqual([r["approved"] for r in results], [True] * 4)
        for subject_id in range(1, 5):
            self.assertEqual(self.reviewed(self.store, 1, subject_id), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_one_that_cannot_be_approved_does_not_stop_the_others(self):
        write_mask(self.builder.segmentation_file(1, 2), ["FEMUR_LEFT"])
        results = {r["scan_key"]: r for r in self.store.approve_all()}
        self.assertFalse(results["ds-0001/sub-000002/scan-01"]["approved"])
        self.assertIn("changed after", results["ds-0001/sub-000002/scan-01"]["message"])
        self.assertEqual(sum(r["approved"] for r in results.values()), 3)

    def test_approvals_at_the_same_time_all_land(self):
        """Four scans approved from four threads, each with a remark for its scan.json."""
        start = threading.Barrier(4)

        def approve(key):
            start.wait(timeout=10)
            return self.store.approve(key, remark="Hip implant")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(approve, [f"ds-0001/sub-00000{i}/scan-01" for i in range(1, 5)]))
        for subject_id in range(1, 5):
            self.assertEqual(self.reviewed(self.store, 1, subject_id), {"FEMUR_LEFT", "FEMUR_RIGHT"})
            self.assertEqual(self.builder.scan_info(1, subject_id)["remarks"], "QC: Hip implant")
        self.assertEqual(len(self.make_store().approvals()), 4, "and the record of each survives a restart")


class ApprovedSubjectTests(ApprovalTestCase):
    def test_an_approved_scan_is_never_handed_out_again(self):
        """Not even when the server queues the labels reviewed already."""
        store = self.make_store(eligible_review_states=["not_reviewed", "reviewed"])
        self.review(store, self.rita)
        store.approve(KEY)
        store.refresh_index()
        with self.assertRaises(QCError) as ctx:
            store.next_scan(self.rita, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_another_server_on_the_database_takes_it_as_reviewed(self):
        """Its approval is the other servers' record of the review: none queues the scan again,
        but one set up for a second review does."""
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        other = self.make_store(credentials_dir=self.make_credentials_dir("other", "qc_other_server"))
        self.assertEqual(other.stats().eligible_scans, 0)
        self.assertEqual(self.reviewed(other), {"FEMUR_LEFT", "FEMUR_RIGHT"})
        second = self.make_store(
            QCServerConfig(eligible_review_states=["reviewed"]),
            credentials_dir=self.make_credentials_dir("second", "qc_second_review"),
        )
        reviewer = second.create_user("sam", roles=[REVIEWER])[0]
        case = second.handout_case(second.next_scan(reviewer, REVIEWER))
        self.assertEqual({label.state for label in case.labels.values()}, {"pending"})
        self.assertEqual(case.reviewed_before, ["FEMUR_LEFT", "FEMUR_RIGHT"])

    def test_a_finished_scan_moves_to_the_done_file_and_stays_finished(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assertEqual(json.loads((self.state_dir / "cases.json").read_text(encoding="utf-8")), [])
        done = [json.loads(line) for line in (self.state_dir / "cases_done.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([case["stage"] for case in done], ["applied"])
        self.assertEqual(done[0]["reviewed_labels"], ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(done[0]["result"]["sha256"], done[0]["base"]["sha256"], "the segmentation the approval left")
        self.assertEqual(self.make_store().case_of(KEY).stage, "applied")

    def test_the_stats_follow_each_stage(self):
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        self.builder.add_scan(1, 3, segmentation=["FEMUR_LEFT"])
        store = self.make_store()
        self.review(store, self.rita)  # scan 1 waits for approval
        self.review(store, self.rita, rejected=["FEMUR_LEFT"])  # scan 2 for an editor
        stats = store.stats()
        self.assertEqual(
            (stats.available, stats.to_review, stats.to_edit, stats.awaiting_approval, stats.applied), (1, 0, 1, 1, 0)
        )
        store.approve(KEY)
        store.next_scan(self.eddie, EDITOR)
        stats = store.stats()
        self.assertEqual((stats.assigned, stats.to_edit, stats.awaiting_approval, stats.applied), (1, 0, 0, 1))


class AdminActionTests(ApprovalTestCase):
    """Sending a scan back, and closing it."""

    def test_sent_back_to_review_every_verdict_is_reviewed_again(self):
        self.review(self.store, self.rita)
        case = self.store.return_case(KEY, "review", "look at the left femur again")
        self.assertEqual(case.stage, "review")
        self.assertEqual({label.state for label in case.labels.values()}, {"pending"})
        self.assertEqual(case.events[-1].comment, "look at the left femur again")
        self.assertEqual(self.store.next_scan(self.rita, REVIEWER).scan_key, KEY)

    def test_sent_back_to_the_editors_with_the_administrators_word(self):
        self.review(self.store, self.rita)
        case = self.store.return_case(KEY, "edit", "the femoral heads are too small")
        self.assertEqual(case.stage, "edit")
        self.assertEqual((case.requests[0].role, case.requests[0].comment), ("admin", "the femoral heads are too small"))
        assignment = self.store.next_scan(self.eddie, EDITOR)
        self.assertEqual(self.store.handout_case(assignment).requests[0].comment, "the femoral heads are too small")

    def test_a_subject_an_editor_gave_up_on_can_go_back(self):
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        assignment = self.store.next_scan(self.eddie, EDITOR)
        self.store.submit(assignment.assignment_id, self.eddie, False, None, comment="beyond me")
        self.assertEqual(self.store.case_of(KEY).stage, "escalated")
        self.assertEqual(self.store.return_case(KEY, "edit", "try the threshold tool").stage, "edit")

    def test_a_closed_subject_is_left_alone_and_nothing_is_written(self):
        before = self.dataset_state()
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        case = self.store.close_case(KEY, "not a usable scan")
        self.assertEqual(case.stage, "closed")
        self.assertEqual(self.dataset_state(), before)
        with self.assertRaises(QCError):
            self.store.next_scan(self.eddie, EDITOR)
        with self.assertRaises(QCError):
            self.store.next_scan(self.rita, REVIEWER)
        self.assertEqual(self.make_store().case_of(KEY).stage, "closed")

    def test_a_closed_subject_can_be_reopened(self):
        self.review(self.store, self.rita)
        self.store.close_case(KEY)
        self.store.return_case(KEY, "review")
        self.assertEqual(self.make_store().case_of(KEY).stage, "review", "the reopened case outlives a restart")
        self.assertEqual(self.store.next_scan(self.rita, REVIEWER).scan_key, KEY)

    def test_a_subject_in_somebodys_hands_is_left_to_them(self):
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.store.next_scan(self.eddie, EDITOR)
        for act in (lambda: self.store.return_case(KEY, "review"), lambda: self.store.close_case(KEY)):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("eddie", ctx.exception.message)

    def test_an_approved_subject_cannot_be_sent_back_or_closed(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        for act in (lambda: self.store.return_case(KEY, "review"), lambda: self.store.close_case(KEY)):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 409)

    def test_a_subject_goes_back_to_review_or_edit_only(self):
        self.review(self.store, self.rita)
        with self.assertRaises(QCError) as ctx:
            self.store.return_case(KEY, "approval")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_a_subject_sent_back_gets_the_remark_at_once_and_the_editors_the_comment(self):
        self.review(self.store, self.rita)
        case = self.store.return_case(KEY, "edit", "the femoral heads are too small", remark="Hip implant")
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(self.reviewed(self.store), set())
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)
        step = case.events[-1]
        self.assertEqual(step.comment, "the femoral heads are too small")
        self.assertEqual(step.details, {"to": "edit", "remark": "QC: Hip implant", "remark_added": True})
        self.assertEqual(case.requests[0].comment, "the femoral heads are too small")
        self.assertIn("'QC: Hip implant' added to its remarks", self.builder.dataset_log(1).read_text(encoding="utf-8"))

    def test_a_subject_that_changed_since_it_was_listed_is_left_alone(self):
        self.review(self.store, self.rita)
        listed = self.store.case_of(KEY).revision
        self.store.return_case(KEY, "review")
        for act in (
            lambda: self.store.close_case(KEY, remark="Hip implant", revision=listed),
            lambda: self.store.return_case(KEY, "edit", remark="Hip implant", revision=listed),
        ):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("changed after you listed it", ctx.exception.message)
        self.assertEqual(self.store.case_of(KEY).stage, "review")
        self.assertIsNone(self.remarks())


class TickedSubjectTests(ApprovalTestCase):
    """Closing and sending back several scans at once, as the admin panel's ticks do."""

    def prepare(self) -> None:
        super().prepare()
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])

    def setUp(self) -> None:
        super().setUp()
        self.review(self.store, self.rita)  # scan 1 waits for approval
        self.review(self.store, self.rita)  # and so does scan 2

    def test_each_is_closed_with_the_remark_and_one_that_cannot_be_is_reported(self):
        self.store.close_case("ds-0001/sub-000002/scan-01")
        results = self.store.close_all([KEY, "ds-0001/sub-000002/scan-01"], remark="Cropped")
        self.assertEqual([(r["scan_key"], r["closed"]) for r in results], [(KEY, True), ("ds-0001/sub-000002/scan-01", False)])
        self.assertTrue(results[0]["remark_added"])
        self.assertIn("closed already", results[1]["message"])
        self.assertEqual(self.remarks(), "QC: Cropped")
        self.assertIsNone(self.remarks(2))

    def test_each_is_sent_back_with_the_comment_and_the_remark(self):
        results = self.store.return_all([KEY, "ds-0001/sub-000002/scan-01"], "edit", "check the femoral heads", remark="Hip implant")
        self.assertEqual([(r["returned"], r["remark_added"]) for r in results], [(True, True), (True, True)])
        for key in (KEY, "ds-0001/sub-000002/scan-01"):
            case = self.store.case_of(key)
            self.assertEqual((case.stage, case.requests[0].comment), ("edit", "check the femoral heads"))
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(self.remarks(2), "QC: Hip implant")

    def test_one_that_changed_since_it_was_listed_is_left_out(self):
        listed = {key: self.store.case_of(key).revision for key in (KEY, "ds-0001/sub-000002/scan-01")}
        self.store.return_case("ds-0001/sub-000002/scan-01", "review")
        results = self.store.return_all([KEY, "ds-0001/sub-000002/scan-01"], "review", remark="Hip implant", revisions=listed)
        self.assertEqual([r["returned"] for r in results], [True, False])
        self.assertIn("changed after you listed it", results[1]["message"])
        self.assertIsNone(self.remarks(2))

    def test_what_is_asked_wrongly_is_refused_before_anything_is_done(self):
        for act in (
            lambda: self.store.return_all([KEY], "approval"),
            lambda: self.store.return_all([KEY], "edit", comment=5),
            lambda: self.store.return_all([KEY], "edit", remark="x" * 600),
            lambda: self.store.close_all([KEY], remark="x" * 600),
        ):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.store.case_of(KEY).stage, "approval")
        self.assertIsNone(self.remarks())


class RejectedSubjectTests(ApprovalTestCase):
    """A scan a reviewer rejected as a whole: the administrator records why."""

    def reject_scan(self, comment: str = "hip implant"):
        assignment = self.store.next_scan(self.rita, REVIEWER)
        return self.store.submit(assignment.assignment_id, self.rita, False, None, comment=comment)

    def test_it_waits_for_the_administrator(self):
        self.reject_scan()
        self.assertEqual([case["scan_key"] for case in self.store.cases(stages=["rejected"])], [KEY])
        self.assertEqual(self.store.stats().rejected_scans, 1)
        self.assertEqual(self.make_store().case_of(KEY).stage, "rejected", "and still does after a restart")

    def test_closing_it_writes_the_remark_and_nothing_else(self):
        self.reject_scan()
        case = self.store.close_case(KEY, remark="Hip implant")
        self.assertEqual(case.stage, "closed")
        self.assertEqual(self.remarks(), "QC: Hip implant")
        self.assertEqual(self.origins(), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "source"})
        self.assertEqual(self.reviewed(self.store), set())
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)
        step = case.events[-1]
        self.assertEqual((step.action, step.comment), ("close", "QC: Hip implant"))
        self.assertEqual(step.details, {"remark": "QC: Hip implant", "remark_added": True})
        self.assertIn("'QC: Hip implant' added to its remarks", self.builder.dataset_log(1).read_text(encoding="utf-8"))
        for user, role in ((self.rita, REVIEWER), (self.eddie, EDITOR)):
            with self.assertRaises(QCError):
                self.store.next_scan(user, role)

    def test_a_remark_there_already_is_not_written_again(self):
        self.given_remarks("Metal artifact present; QC: hip implant")
        path = self.builder.scan_folder(1, 1) / "scan.json"
        before = path.read_bytes()
        self.reject_scan()
        case = self.store.close_case(KEY, remark="Hip implant")
        self.assertFalse(case.events[-1].details["remark_added"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_remark_too_long_is_refused_and_the_subject_still_waits(self):
        self.reject_scan()
        with self.assertRaises(QCError) as ctx:
            self.store.close_case(KEY, remark="x" * 600)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.store.case_of(KEY).stage, "rejected")
        self.assertIsNone(self.remarks())

    def test_the_reviewer_can_be_overruled(self):
        self.reject_scan()
        self.assertEqual(self.store.return_case(KEY, "review", "the implant is in the other leg").stage, "review")
        self.assertEqual(self.store.next_scan(self.rita, REVIEWER).scan_key, KEY)


class AdminApiTests(ApiTestCase):
    """The approvals as the admin panel makes them."""

    n_scans = 2

    def accept(self, key: str) -> dict:
        """alice accepts every label of the next scan waiting for a review, on the review page."""
        handout = self.next_scan(key, REVIEWER)
        response = self.submit(key, handout["assignment_id"], True, role=REVIEWER, use_stored_segmentation=True)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_cases_are_listed_by_stage(self):
        self.accept(self.alice_key)
        listed = self.client.get("/admin/api/cases?stage=approval", headers=self.admin_headers).json()
        self.assertEqual([case["scan_key"] for case in listed], [KEY])
        self.assertIsNone(listed[0]["leased_to"])
        self.assertEqual(self.client.get("/admin/api/cases?stage=edit", headers=self.admin_headers).json(), [])
        self.assertEqual(self.client.get("/admin/api/cases", headers={}).status_code, 401)

    def test_a_subject_is_approved_from_the_panel(self):
        self.accept(self.alice_key)
        response = self.client.post(f"/admin/api/cases/{KEY}/approve", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated_labels"], {})
        self.assertEqual(response.json()["reviewed_labels"], ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})
        self.assertEqual(self.client.post(f"/admin/api/cases/{KEY}/approve", headers=self.admin_headers).status_code, 409)

    def test_every_subject_waiting_is_approved_at_once(self):
        self.accept(self.alice_key)
        self.accept(self.bob_key)
        body = self.client.post("/admin/api/cases/approve", json={}, headers=self.admin_headers).json()
        self.assertEqual(body["approved"], 2)
        named = self.client.post("/admin/api/cases/approve", json={"scan_keys": [KEY]}, headers=self.admin_headers)
        self.assertEqual(named.json()["results"][0]["approved"], False, "approved already")

    def test_a_subject_is_sent_back_or_closed_from_the_panel(self):
        self.accept(self.alice_key)
        sent = self.client.post(
            f"/admin/api/cases/{KEY}/return", json={"to": "edit", "comment": "redo"}, headers=self.admin_headers
        )
        self.assertEqual(sent.json()["stage"], "edit")
        closed = self.client.post(f"/admin/api/cases/{KEY}/close", json={}, headers=self.admin_headers)
        self.assertEqual(closed.json()["stage"], "closed")
        self.assertEqual(
            self.client.post(f"/admin/api/cases/{KEY}/return", json={"to": "nowhere"}, headers=self.admin_headers)
            .status_code,
            400,
        )

    def test_a_rejected_subject_is_closed_with_a_remark_from_the_panel(self):
        handout = self.next_scan(self.alice_key, REVIEWER)
        rejected = self.submit(self.alice_key, handout["assignment_id"], False, role=REVIEWER, comment="cropped")
        self.assertEqual(rejected.json()["stage"], "rejected")
        listed = self.client.get("/admin/api/cases?stage=rejected", headers=self.admin_headers).json()
        self.assertEqual([case["scan_key"] for case in listed], [KEY])
        self.assertEqual(self.client.get("/admin/api/stats", headers=self.admin_headers).json()["rejected_scans"], 1)
        closed = self.client.post(
            f"/admin/api/cases/{KEY}/close", json={"remark": "Cropped at the knee"}, headers=self.admin_headers
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["events"][-1]["details"]["remark_added"], True)
        self.assertEqual(self.builder.scan_info(1, 1)["remarks"], "QC: Cropped at the knee")
        self.assertEqual(self.reviewed(self.store), set())

    def test_the_segmentation_can_be_downloaded_to_look_at_first(self):
        self.accept(self.alice_key)
        response = self.client.get(f"/admin/api/cases/{KEY}/segmentation", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.builder.segmentation_file(1, 1).read_bytes())
        self.assertIn('filename="ds-0001_sub-000001_scan-01.seg.nrrd"', response.headers["content-disposition"])
        self.assertEqual(self.client.get("/admin/api/cases/ds-0001/sub-000099/scan-01", headers=self.admin_headers).status_code, 404)
        self.assertEqual(
            self.client.get("/admin/api/cases/ds-0001/sub-000099/scan-01/segmentation", headers=self.admin_headers).status_code,
            404,
        )
        encoded = self.client.get(f"/admin/api/cases/{KEY.replace('/', '%2F')}/segmentation", headers=self.admin_headers)
        self.assertEqual(encoded.status_code, 200, "a key sent with its slashes encoded, as encodeURIComponent does")

    def test_subjects_are_found_by_what_their_comments_say(self):
        self.judge(self.alice_key, comment="Hip  IMPLANT on the left")
        self.judge(self.bob_key, comment="clean", rejected_labels=["FEMUR_RIGHT"])
        listed = self.client.get("/admin/api/cases?comment=hip%20implant", headers=self.admin_headers).json()
        self.assertEqual([case["scan_key"] for case in listed], [KEY])
        self.assertEqual([step["comment"] for step in listed[0]["matches"]], ["Hip  IMPLANT on the left"])
        by_stage = self.client.get("/admin/api/cases?stage=edit&comment=implant", headers=self.admin_headers).json()
        self.assertEqual(by_stage, [])
        unfiltered = self.client.get("/admin/api/cases", headers=self.admin_headers).json()
        self.assertEqual(len(unfiltered), 2)
        self.assertNotIn("matches", unfiltered[0])

    def test_the_administrators_own_comments_are_searched_too(self):
        self.accept(self.alice_key)
        self.client.post(
            f"/admin/api/cases/{KEY}/return", json={"to": "review", "comment": "Look at the implant"},
            headers=self.admin_headers,
        )
        listed = self.client.get("/admin/api/cases?comment=implant", headers=self.admin_headers).json()
        self.assertEqual([(step["by"], step["action"]) for step in listed[0]["matches"]], [("admin", "return")])

    def test_ticked_subjects_are_approved_with_a_remark(self):
        self.accept(self.alice_key)
        self.accept(self.bob_key)
        revision = self.client.get(f"/admin/api/cases/{KEY}", headers=self.admin_headers).json()["revision"]
        body = self.client.post(
            "/admin/api/cases/approve",
            json={"scan_keys": [KEY], "remark": "Hip implant", "revisions": {KEY: revision}},
            headers=self.admin_headers,
        ).json()
        self.assertEqual((body["approved"], body["results"][0]["remark_added"]), (1, True))
        self.assertEqual(self.builder.scan_info(1, 1)["remarks"], "QC: Hip implant")
        self.assertNotIn("remarks", self.builder.scan_info(1, 2))
        other = self.client.get("/admin/api/cases/ds-0001/sub-000002/scan-01", headers=self.admin_headers).json()
        self.assertEqual(other["stage"], "approval")

    def test_ticked_subjects_are_sent_back_or_closed_with_a_remark(self):
        self.accept(self.alice_key)
        self.accept(self.bob_key)
        cases = self.client.get("/admin/api/cases", headers=self.admin_headers).json()
        listed = {case["scan_key"]: case["revision"] for case in cases}
        sent = self.client.post(
            "/admin/api/cases/return",
            json={"scan_keys": [KEY], "to": "edit", "comment": "redo", "remark": "Hip implant", "revisions": listed},
            headers=self.admin_headers,
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual((sent.json()["returned"], sent.json()["results"][0]["remark_added"]), (1, True))
        self.assertEqual(self.builder.scan_info(1, 1)["remarks"], "QC: Hip implant")
        closed = self.client.post(
            "/admin/api/cases/close",
            json={"scan_keys": [KEY, "ds-0001/sub-000002/scan-01"], "remark": "Cropped", "revisions": listed},
            headers=self.admin_headers,
        ).json()
        self.assertEqual(closed["closed"], 1)
        self.assertEqual([r["closed"] for r in closed["results"]], [False, True], "the first changed after it was listed")
        self.assertEqual(self.builder.scan_info(1, 2)["remarks"], "QC: Cropped")
        self.assertEqual(self.reviewed(self.store, 1, 2), set())

    def test_sending_back_or_closing_asked_for_wrongly_is_refused(self):
        self.accept(self.alice_key)
        for path, body in (
            ("close", {}),
            ("close", {"scan_keys": KEY}),
            ("close", {"scan_keys": [KEY], "revisions": [1]}),
            ("return", {"scan_keys": [KEY], "to": "nowhere"}),
            ("return", {"scan_keys": [KEY], "to": "edit", "comment": 5}),
            ("return", {"scan_keys": [KEY], "to": "edit", "remark": 5}),
        ):
            response = self.client.post(f"/admin/api/cases/{path}", json=body, headers=self.admin_headers)
            self.assertEqual(response.status_code, 400, (path, body))
        self.assertEqual(self.client.get(f"/admin/api/cases/{KEY}", headers=self.admin_headers).json()["stage"], "approval")

    def test_a_remark_is_found_like_a_comment(self):
        self.accept(self.alice_key)
        self.client.post(
            f"/admin/api/cases/{KEY}/return", json={"to": "edit", "comment": "redo the heads", "remark": "Hip implant"},
            headers=self.admin_headers,
        )
        listed = self.client.get("/admin/api/cases?comment=implant", headers=self.admin_headers).json()
        self.assertEqual(
            [(step["action"], step["details"]["remark"]) for step in listed[0]["matches"]], [("return", "QC: Hip implant")]
        )

    def test_a_subject_not_waiting_for_approval_is_approved_only_when_asked_for(self):
        self.judge(self.alice_key, rejected_labels=["FEMUR_RIGHT"], comment="hip implant")
        refused = self.client.post("/admin/api/cases/approve", json={"scan_keys": [KEY]}, headers=self.admin_headers)
        self.assertFalse(refused.json()["results"][0]["approved"])
        allowed = self.client.post(
            f"/admin/api/cases/{KEY}/approve", json={"remark": "Hip implant", "allow_unaccepted": True},
            headers=self.admin_headers,
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual((allowed.json()["reviewed_labels"], allowed.json()["remark_added"]), (["FEMUR_LEFT"], True))
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT"})

    def test_an_approval_asked_for_wrongly_is_refused(self):
        self.accept(self.alice_key)
        for body in ({"allow_unaccepted": "yes"}, {"revisions": [1]}, {"remark": 5}, {"scan_keys": KEY}):
            response = self.client.post("/admin/api/cases/approve", json=body, headers=self.admin_headers)
            self.assertEqual(response.status_code, 400, body)
        response = self.client.post(f"/admin/api/cases/{KEY}/approve", json={"revision": "3"}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reviewed(self.store), set())

    def test_the_editors_correction_is_what_is_downloaded(self):
        handout = self.next_scan(self.alice_key, REVIEWER)
        self.submit(self.alice_key, handout["assignment_id"], True, role=REVIEWER, use_stored_segmentation=True,
                    rejected_labels=["FEMUR_RIGHT"])
        handout = self.next_scan(self.bob_key, EDITOR)
        self.assertEqual(handout["stage"], "edit")
        self.submit(self.bob_key, handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        response = self.client.get(f"/admin/api/cases/{KEY}/segmentation", headers=self.admin_headers)
        staged = self.state_dir / "staged" / KEY / "segmentation.seg.nrrd"
        self.assertEqual(response.content, staged.read_bytes())
        self.assertEqual(labels_in_mask(staged), {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"})


if __name__ == "__main__":
    unittest.main()
