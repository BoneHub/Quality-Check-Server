"""What a submission is held to, and what it does.

Requirements under test: "if quality_check_confirmed = True is given by the client, the
labels of those bones must be marked as reviewed. quality_check_confirmed = False then
basically server must not make any changes", and "collect all the submissions in the server
created workspace folder [...] only after admin approval, things are overwritten in the
bonehub dataset". An editor's upload waits, in canonical form, in the server's state
folder, and a rejection changes nothing. What the approval writes is in ``test_approval.py``.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import SimpleITK as sitk

from bonehub_format import read_segmentation_labels
from qc_server import store as store_module
from qc_server import workflow
from qc_server.models import EDITOR, REVIEWER, Case, CaseLabel
from qc_server.store import QCError

from tests.support import LABEL_VALUE, QCTestCase, labels_in_mask, segment_header, write_image, write_raw_mask

KEY = "ds-0001/sub-000001/scan-01"


class EditorTestCase(QCTestCase):
    """A scan a reviewer sent back, in an editor's hands: rita rejected the right femur,
    and alice holds the scan in 3D Slicer."""

    config: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.store = self.make_store(**self.config)
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        self.alice = self.store.create_user("alice", roles=[EDITOR])[0]
        self.bob = self.store.create_user("bob", roles=[EDITOR])[0]
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.assignment = self.store.next_scan(self.alice, EDITOR)
        self.before = self.dataset_state()
        self.staged = self.state_dir / "staged" / KEY / "segmentation.seg.nrrd"

    def submit(self, upload, **kwargs):
        return self.store.submit(self.assignment.assignment_id, self.alice, True, upload, **kwargs)

    def assert_dataset_untouched(self) -> None:
        self.assertEqual(self.dataset_state(), self.before)


class StagedCorrectionTests(EditorTestCase):
    def test_the_correction_waits_in_the_state_folder_and_the_dataset_is_untouched(self):
        outcome = self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"]))
        self.assertTrue(outcome.segmentation_staged)
        self.assertEqual(labels_in_mask(self.staged), {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"})
        self.assert_dataset_untouched()

    def test_the_assignment_is_closed_with_what_the_upload_did(self):
        self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"]), comment="head redrawn")
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "submitted")
        self.assertTrue(assignment.quality_check_confirmed)
        self.assertTrue(assignment.segmentation_staged)
        self.assertEqual(assignment.edited_labels, ["FEMUR_RIGHT"])
        self.assertEqual((assignment.stage_after, assignment.comment), ("review", "head redrawn"))
        self.assertIsNotNone(assignment.submitted_at)

    def test_the_upload_itself_is_left_for_the_caller(self):
        """What waits is a rewritten copy; the API layer deletes its own spooled file."""
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.submit(upload)
        self.assertTrue(upload.exists())
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_a_second_correction_replaces_the_first(self):
        self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"]))
        self.review(self.store, self.rita, rejected=["TIBIA_LEFT"])
        outcome = self.edit(self.store, self.bob, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(outcome.removed_labels, ["TIBIA_LEFT"])
        self.assertEqual(labels_in_mask(self.staged), {"FEMUR_LEFT", "FEMUR_RIGHT"})
        self.assert_dataset_untouched()


class OriginsOnApprovalTests(QCTestCase):
    """What an approval records of each label: its origin in scan.json, and whether it is reviewed.

    A label an editor drew or changed is manual; any other keeps the origin it had. A label a
    reviewer accepted becomes reviewed; one reviewed already stays so while nobody redraws it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_scan(
            1, 1, segmentation={"FEMUR_LEFT": "source", "FEMUR_RIGHT": "model:TotalSegmentator@2.4.0"},
            reviewed=["FEMUR_LEFT"],
        )
        self.store = self.make_store(eligible_review_states=["not_reviewed", "reviewed"])
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        self.eddie = self.store.create_user("eddie", roles=[EDITOR])[0]

    def test_an_accepted_label_keeps_its_origin_and_becomes_reviewed(self):
        self.review(self.store, self.rita)
        outcome = self.store.approve(KEY)
        self.assertEqual(outcome.updated_labels, {})
        self.assertEqual(outcome.reviewed_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(
            self.builder.origins(1, 1), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "model:TotalSegmentator@2.4.0"}
        )
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_a_label_an_editor_redrew_is_manual(self):
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        self.review(self.store, self.rita)
        outcome = self.store.approve(KEY)
        self.assertEqual(outcome.updated_labels, {"FEMUR_RIGHT": "manual"})
        self.assertEqual(self.builder.origins(1, 1), {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual"})
        self.assertEqual(self.reviewed(self.store), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_a_rejected_label_the_editor_left_as_it_was_keeps_its_origin(self):
        """The editor looked at it and found nothing to change: the voxels are still the model's."""
        self.review(self.store, self.rita, rejected=["FEMUR_RIGHT"])
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(self.store.case_of(KEY).labels["FEMUR_RIGHT"].edited_by, "eddie")
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assertEqual(self.builder.origins(1, 1)["FEMUR_RIGHT"], "model:TotalSegmentator@2.4.0")

    def test_a_label_an_editor_added_is_manual(self):
        self.review(self.store, self.rita, missing=["SACRUM"])
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assertEqual(self.builder.origins(1, 1)["SACRUM"], "manual")
        self.assertIn("SACRUM", self.reviewed(self.store))

    def test_a_redrawn_label_nobody_accepted_is_manual_but_not_reviewed(self):
        """Approved before a reviewer saw the correction: drawn by hand, reviewed by nobody."""
        self.review(self.store, self.rita, rejected=["FEMUR_LEFT"])
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_LEFT"])
        outcome = self.store.approve(KEY, allow_unaccepted=True)
        self.assertEqual(self.builder.origins(1, 1)["FEMUR_LEFT"], "manual")
        self.assertEqual(outcome.reviewed_labels, ["FEMUR_RIGHT"])

    def test_a_label_reviewed_already_that_nobody_redrew_stays_reviewed(self):
        """Approving early never makes a label reviewed that nobody accepted, nor unreviews one."""
        self.review(self.store, self.rita, accepted=["FEMUR_RIGHT"], rejected=["FEMUR_LEFT"])
        outcome = self.store.approve(KEY, allow_unaccepted=True)
        self.assertEqual(outcome.reviewed_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])

    def test_a_label_reviewed_already_and_redrawn_must_be_accepted_again(self):
        self.store = self.make_store()  # FEMUR_LEFT, reviewed already, is not under review
        rita = self.store.create_user("rita2", roles=[REVIEWER])[0]
        eddie = self.store.create_user("eddie2", roles=[EDITOR])[0]
        self.review(self.store, rita, rejected=["FEMUR_RIGHT"])
        self.edit(self.store, eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_LEFT", "FEMUR_RIGHT"])
        case = self.store.case_of(KEY)
        self.assertEqual(case.labels["FEMUR_LEFT"].state, "pending", "the correction spilled into it")
        outcome = self.store.approve(KEY, allow_unaccepted=True)
        self.assertEqual(outcome.reviewed_labels, [])


class ApprovalOriginsTests(unittest.TestCase):
    """``workflow.approval_origins``, ``changed_origins`` and ``reviewed_after`` on their own."""

    def case(self, labels: dict, reviewed_before=()) -> Case:
        return Case(
            scan_key=KEY, dataset_id=1, subject_id=1, scan_id=1, stage="approval", labels=labels,
            reviewed_before=list(reviewed_before), created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )

    def test_scan_json_lists_exactly_the_painted_labels(self):
        case = self.case({
            "FEMUR_LEFT": CaseLabel(state="accepted"),
            "FEMUR_RIGHT": CaseLabel(state="accepted", drawn=True),
            "SACRUM": CaseLabel(state="removed", painted=False),
            "PATELLA_LEFT": CaseLabel(state="accepted", drawn=True),
        })
        before = {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "model:M@1", "SACRUM": "source"}
        after = workflow.approval_origins(case, before)
        self.assertEqual(after, {"FEMUR_LEFT": "source", "FEMUR_RIGHT": "manual", "PATELLA_LEFT": "manual"})
        self.assertEqual(
            workflow.changed_origins(before, after), {"FEMUR_RIGHT": "manual", "PATELLA_LEFT": "manual", "SACRUM": None}
        )

    def test_a_painted_label_nobody_drew_and_scan_json_does_not_list_has_no_origin(self):
        case = self.case({"FEMUR_LEFT": CaseLabel(state="accepted"), "SACRUM": CaseLabel(state="accepted")})
        with self.assertRaises(workflow.UnknownOrigin) as ctx:
            workflow.approval_origins(case, {"FEMUR_LEFT": "source"})
        self.assertEqual(ctx.exception.labels, ["SACRUM"])

    def test_what_an_approval_leaves_reviewed(self):
        case = self.case(
            {
                "FEMUR_LEFT": CaseLabel(state="accepted"),
                "FEMUR_RIGHT": CaseLabel(state="kept"),
                "TIBIA_LEFT": CaseLabel(state="kept"),
                "TIBIA_RIGHT": CaseLabel(state="pending", drawn=True),
                "SACRUM": CaseLabel(state="rejected"),
                "PATELLA_LEFT": CaseLabel(state="removed", painted=False),
            },
            reviewed_before=["FEMUR_RIGHT", "TIBIA_RIGHT", "SACRUM", "PATELLA_LEFT"],
        )
        self.assertEqual(workflow.reviewed_after(case), ["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])


class StoredFormatTests(EditorTestCase):
    """What waits for approval, and then lands in the dataset, is the canonical BoneHub
    segmentation, whatever was sent."""

    def staged_header(self) -> dict:
        reader = sitk.ImageFileReader()
        reader.SetFileName(str(self.staged))
        reader.ReadImageInformation()
        return {key: reader.GetMetaData(key) for key in reader.GetMetaDataKeys()}

    def test_the_staged_mask_is_a_bonehub_seg_nrrd(self):
        self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"]))
        self.assertTrue(self.staged.name.endswith(".seg.nrrd"))
        self.assertEqual(
            read_segmentation_labels(self.staged), ["SACRUM", "FEMUR_LEFT", "FEMUR_RIGHT"], "numbered in label map order"
        )
        header = self.staged_header()
        self.assertEqual(header["Segment1_Tags"], f"BoneHubLabel:FEMUR_LEFT|BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|")

    def test_an_upload_numbered_its_own_way_is_renumbered(self):
        """Segments named only, numbered 7 and 3, listed out of order: stored as 1 and 2."""
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 7
        numbers[3, 0:2, 0:2] = 3
        upload = write_raw_mask(
            self.upload_path(), numbers, segment_header((7, "FEMUR_RIGHT", None), (3, "FEMUR_LEFT", None))
        )
        self.submit(upload)

        header = self.staged_header()
        self.assertEqual((header["Segment0_Name"], header["Segment0_LabelValue"]), ("FEMUR_LEFT", "1"))
        self.assertEqual((header["Segment1_Name"], header["Segment1_LabelValue"]), ("FEMUR_RIGHT", "2"))
        self.assertEqual(labels_in_mask(self.staged), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_segments_of_one_label_are_merged(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        numbers[3, 0:2, 0:2] = 2
        copy_tags = f"BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"
        header = segment_header((1, "FEMUR_LEFT", None), (2, "FEMUR_LEFT_copy", copy_tags))
        self.submit(write_raw_mask(self.upload_path(), numbers, header))
        self.assertEqual(read_segmentation_labels(self.staged), ["FEMUR_LEFT"])

    def test_the_staged_mask_takes_the_images_exact_geometry(self):
        """A client that rounds the geometry within tolerance must not shift the dataset's mask."""
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        header = segment_header((1, "FEMUR_LEFT", None))
        self.submit(write_raw_mask(self.upload_path(), numbers, header, spacing=(1.0002, 1.0002, 1.0002)))

        staged = sitk.ReadImage(str(self.staged))
        image = sitk.ReadImage(str(self.builder.image_file(1, 1)))
        self.assertEqual(staged.GetSpacing(), image.GetSpacing())
        self.assertEqual(staged.GetOrigin(), image.GetOrigin())
        self.assertEqual(staged.GetDirection(), image.GetDirection())


class RejectedSubmissionTests(QCTestCase):
    """quality_check_confirmed = False changes nothing in the dataset, from a reviewer or an editor."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.before = self.dataset_state()

    def reject(self, role: str, upload=None, comment=None):
        assignment = self.store.next_scan(self.alice, role)
        return self.store.submit(assignment.assignment_id, self.alice, False, upload, comment=comment)

    def test_a_reviewers_rejection_changes_nothing_in_the_dataset(self):
        outcome = self.reject(REVIEWER, comment="too noisy")
        self.assertEqual(outcome.stage, "rejected")
        self.assertEqual(self.dataset_state(), self.before)

    def test_an_editors_rejection_changes_nothing_either(self):
        self.review(self.store, self.alice, rejected=["FEMUR_RIGHT"])
        outcome = self.reject(EDITOR, comment="cannot be corrected")
        self.assertEqual(outcome.stage, "escalated")
        self.assertEqual(self.dataset_state(), self.before)

    def test_an_uploaded_file_is_ignored_when_rejecting(self):
        self.review(self.store, self.alice, rejected=["FEMUR_RIGHT"])
        outcome = self.reject(EDITOR, upload=self.upload_file(["TIBIA_LEFT"]))
        self.assertFalse(outcome.segmentation_staged)
        self.assertEqual(list((self.state_dir / "staged").rglob("*.seg.nrrd")), [])
        self.assertEqual(self.dataset_state(), self.before)

    def test_no_backup_is_taken(self):
        self.reject(REVIEWER)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])

    def test_the_assignment_is_closed_with_the_comment(self):
        outcome = self.reject(REVIEWER, comment="too noisy")
        assignment = self.store.get_assignment(outcome.assignment.assignment_id)
        self.assertEqual(assignment.state, "submitted")
        self.assertFalse(assignment.quality_check_confirmed)
        self.assertEqual(assignment.comment, "too noisy")
        self.assertEqual(outcome.accepted_labels, [])


class SubmissionValidationTests(EditorTestCase):
    def assert_refused(self, upload: Path, *fragments: str) -> None:
        with self.assertRaises(QCError) as ctx:
            self.submit(upload)
        self.assertEqual(ctx.exception.status_code, 400)
        for fragment in fragments:
            self.assertIn(fragment, ctx.exception.message)
        self.assert_dataset_untouched()
        self.assertFalse(self.staged.exists())
        self.assertEqual(self.store.case_of(KEY).stage, "edit")

    def mask(self, *segments, numbers: np.ndarray | None = None) -> Path:
        if numbers is None:
            numbers = np.zeros((6, 6, 6), dtype=np.uint8)
            for index, (number, _name, _tags) in enumerate(segments):
                numbers[index, 0:2, 0:2] = number
        return write_raw_mask(self.upload_path(), numbers, segment_header(*segments))

    def test_confirming_without_a_file_is_refused(self):
        with self.assertRaises(QCError) as ctx:
            self.submit(None)
        self.assertIn("corrected segmentation file", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_an_empty_segmentation_cannot_be_confirmed(self):
        self.assert_refused(self.upload_file([]), "empty")

    def test_a_file_that_is_not_a_segmentation_is_refused(self):
        bad = self.tmp_path / "not_a_mask.seg.nrrd"
        bad.write_bytes(b"this is not a NRRD file")
        self.assert_refused(bad, ".seg.nrrd")

    def test_a_nifti_segmentation_is_refused(self):
        old = write_image(self.tmp_path / "old_client_upload.nii.gz")
        self.assert_refused(old, ".seg.nrrd")

    def test_a_segment_that_is_not_a_bonehub_label_is_refused(self):
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), (2, "MY_SCRATCH_SEGMENT", None)), "MY_SCRATCH_SEGMENT")

    def test_background_is_not_a_label_a_segment_can_be(self):
        self.assert_refused(self.mask((1, "BACKGROUND", None)), "BACKGROUND")

    def test_a_tag_naming_no_bonehub_label_is_refused(self):
        self.assert_refused(self.mask((1, "Segment_1", "BoneHubValue:31337|")), "31337")

    def test_a_tag_is_enough_to_name_a_segment(self):
        """As in the schema's reader: the BoneHubValue tag, else the segment name."""
        upload = self.mask((1, "Segment_1", f"BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"))
        outcome = self.submit(upload)
        self.assertTrue(outcome.segmentation_staged)
        self.assertEqual(labels_in_mask(self.staged), {"FEMUR_LEFT"})

    def test_a_segment_named_one_label_and_tagged_another_is_refused(self):
        tags = f"BoneHubLabel:FEMUR_LEFT|BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"
        self.assert_refused(self.mask((1, "FEMUR_RIGHT", tags)), "FEMUR_RIGHT", "FEMUR_LEFT")

    def test_voxels_the_header_does_not_describe_are_refused(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        numbers[1, 0:2, 0:2] = 5
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), numbers=numbers), "[5]")

    def test_two_labels_sharing_a_segment_number_are_refused(self):
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), (1, "FEMUR_RIGHT", None)), "segment number 1")

    def test_a_multi_layer_segmentation_is_refused(self):
        """Slicer stores overlapping segments as layers; the dataset format has one."""
        layers = sitk.Compose([sitk.GetImageFromArray(np.ones((6, 6, 6), dtype=np.uint8))] * 2)
        path = self.upload_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in segment_header((1, "FEMUR_LEFT", None)).items():
            layers.SetMetaData(key, str(value))
        sitk.WriteImage(layers, str(path), useCompression=True)
        self.assert_refused(path, "layers")

    def test_a_segmentation_whose_shape_differs_from_the_image_is_refused(self):
        self.assert_refused(self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5)), "does not match")

    def test_a_segmentation_whose_affine_differs_from_the_image_is_refused(self):
        self.assert_refused(self.upload_file(["FEMUR_LEFT"], spacing=(2.0, 2.0, 2.0)), "affine")

    def test_the_geometry_check_can_be_switched_off(self):
        store = self.make_store(require_geometry_match=False)
        outcome = store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5)))
        self.assertTrue(outcome.segmentation_staged)

    def test_an_unknown_confirmed_label_name_is_refused(self):
        with self.assertRaises(QCError) as ctx:
            self.submit(self.upload_file(["FEMUR_LEFT"]), confirmed_labels=["NOT_A_BONE"])
        self.assertIn("NOT_A_BONE", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_confirming_a_label_absent_from_the_upload_is_refused(self):
        with self.assertRaises(QCError) as ctx:
            self.submit(self.upload_file(["FEMUR_LEFT"]), confirmed_labels=["FEMUR_LEFT", "SACRUM"])
        self.assertIn("SACRUM", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_an_editor_cannot_submit_another_editors_assignment(self):
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.bob, False, None)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_submitting_twice_is_refused(self):
        self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))
        with self.assertRaises(QCError) as ctx:
            self.submit(self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_an_expired_assignment_can_still_be_submitted(self):
        """An editor who finishes late should not lose their work."""
        self.assignment.state = "expired"
        outcome = self.submit(self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))
        self.assertEqual(outcome.assignment.state, "submitted")

    def test_an_unknown_assignment_is_a_404(self):
        with self.assertRaises(QCError) as ctx:
            self.store.submit("nope", self.alice, False, None)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_dataset_regenerated_under_another_schema_is_refused(self):
        """The dataset changed format while the scan was out."""
        self.builder.add_dataset(1, schema_version="0.2.0")
        with self.assertRaises(QCError) as ctx:
            self.submit(self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("0.2.0", ctx.exception.message)
        self.assertFalse(self.staged.exists())


class PartialUploadTests(QCTestCase):
    """What an upload of only some of the bones does.

    A label missing from an editor's upload is taken out of the segmentation. A 3D Slicer
    client that exported only its visible segments would drop the rest -- so, unless a reviewer
    asked for a removal, a reviewer must agree to it first (with edits needing review, the
    default), and the administrator sees it in the approval list either way. Once approved,
    the labels are taken out of scan.json, which lists exactly the labels of the segmentation;
    the scan's previous file is kept in ``.bonehub_qc/<server>/backups/``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT", "SACRUM"])
        self.original = self.builder.segmentation_file(1, 1).read_bytes()

    def drop_all_but_the_left_femur(self, **config):
        store = self.make_store(**config)
        rita = store.create_user("rita", roles=[REVIEWER])[0]
        eddie = store.create_user("eddie", roles=[EDITOR])[0]
        self.review(store, rita, rejected=["FEMUR_RIGHT"])
        self.edit(store, eddie, ["FEMUR_LEFT"])
        return store, rita

    def test_bones_missing_from_the_upload_wait_for_a_reviewer_to_agree(self):
        store, _ = self.drop_all_but_the_left_femur()
        case = store.case_of(KEY)
        self.assertEqual(case.stage, "review")
        for name in ("FEMUR_RIGHT", "TIBIA_LEFT", "SACRUM"):
            self.assertEqual((case.labels[name].state, case.labels[name].painted), ("pending", False), name)

    def test_once_agreed_and_approved_they_are_taken_out_of_scan_json(self):
        store, rita = self.drop_all_but_the_left_femur()
        self.review(store, rita)
        outcome = store.approve(KEY)
        self.assertEqual(self.builder.origins(1, 1), {"FEMUR_LEFT": "source"})
        self.assertEqual(outcome.updated_labels, {"FEMUR_RIGHT": None, "TIBIA_LEFT": None, "SACRUM": None})
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT"})
        self.assertEqual(Path(outcome.backup_path).read_bytes(), self.original)
        self.assertEqual(self.reviewed(store), {"FEMUR_LEFT"})


class AuditTrailTests(QCTestCase):
    """Every submission is logged in the server's state folder, and what reaches the dataset
    is logged next to it, in the dataset folder: "every submission from the clients must be
    logged"."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.builder.add_scan(1, 2, segmentation=["FEMUR_LEFT"])
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]

    def submissions(self) -> list:
        path = self.state_dir / "submissions.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_verdict_is_logged_in_the_state_folder_and_not_next_to_the_dataset(self):
        self.review(self.store, self.alice, rejected=["FEMUR_RIGHT"], comment="head cut off")
        log = (self.state_dir / "server.log").read_text(encoding="utf-8")
        self.assertIn(f"Scan {KEY} reviewed by 'alice'", log)
        self.assertIn("accepted FEMUR_LEFT; rejected FEMUR_RIGHT.", log)
        self.assertIn("head cut off", log)
        self.assertFalse(self.builder.dataset_log(1).exists())

    def test_an_approval_is_written_into_the_dataset_folder(self):
        self.review(self.store, self.alice)
        self.store.approve(KEY)
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn(KEY, log)
        self.assertIn("approved", log)
        self.assertIn("alice", log)

    def test_the_machine_readable_trail_records_the_verdict(self):
        self.review(self.store, self.alice, rejected=["FEMUR_RIGHT"])
        entries = [e for e in self.submissions() if e["kind"] == "submission"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry["user"], entry["role"], entry["scan_key"]), ("alice", REVIEWER, KEY))
        self.assertTrue(entry["quality_check_confirmed"])
        self.assertEqual(entry["accepted_labels"], ["FEMUR_LEFT"])
        self.assertEqual(entry["rejected_labels"], ["FEMUR_RIGHT"])
        self.assertEqual(entry["stage"], "edit")
        self.assertIn("timestamp", entry)

    def test_assignments_are_recorded_as_well_as_submissions(self):
        assignment = self.store.next_scan(self.alice, REVIEWER)
        kinds = [entry["kind"] for entry in self.submissions()]
        self.assertIn("assigned", kinds)
        self.assertIn("user_created", kinds)
        self.store.release_assignment(assignment.assignment_id, self.alice)
        self.assertIn("released", [entry["kind"] for entry in self.submissions()])

    def test_the_trail_reads_back_newest_first_and_can_be_filtered(self):
        self.review(self.store, self.alice, rejected=["FEMUR_RIGHT"])
        self.review(self.store, self.alice)
        recent = self.store.audit.read_recent(limit=10, kind="submission")
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["scan_key"], "ds-0001/sub-000002/scan-01", "newest first")
        self.assertEqual(len(self.store.audit.read_recent(limit=1)), 1)

    def test_each_dataset_gets_its_own_log_next_to_the_data(self):
        self.builder.add_scan(2, 1, segmentation=["FEMUR_LEFT"])
        store = self.make_store()
        for _ in range(3):
            self.review(store, self.alice)
        store.approve_all()

        self.assertTrue(self.builder.dataset_log(1).exists())
        self.assertTrue(self.builder.dataset_log(2).exists())
        self.assertIn("ds-0002/sub-000001/scan-01", self.builder.dataset_log(2).read_text(encoding="utf-8"))
        self.assertNotIn("ds-0002/sub-000001/scan-01", self.builder.dataset_log(1).read_text(encoding="utf-8"))


class RefusedReplaceTests(QCTestCase):
    """Windows refuses, for a moment, to replace a file that another process has open.

    A virus scanner looking at a scan.json just written is enough, and so is another client
    that has the file open on an SMB share.
    """

    def test_a_briefly_refused_write_still_lands(self):
        self.builder.add_scan(1, 1, segmentation=["FEMUR_LEFT"])
        store = self.make_store()
        alice = store.create_user("alice")[0]
        self.review(store, alice)
        real_replace = os.replace
        refused = []

        def refuse_twice(source, target):
            if str(target).endswith("scan.json") and len(refused) < 2:
                refused.append(target)
                raise PermissionError(13, "Access is denied")
            real_replace(source, target)

        with mock.patch("qc_server.store.os.replace", side_effect=refuse_twice), mock.patch(
            "qc_server.store.time.sleep"
        ):
            store.approve(KEY, remark="Hip implant")
        self.assertEqual(len(refused), 2)
        self.assertEqual(self.builder.scan_info(1, 1)["remarks"], "QC: Hip implant")

    def test_a_refusal_that_lasts_is_still_reported(self):
        source = self.tmp_path / "new.json"
        source.write_text("{}", encoding="utf-8")
        with mock.patch(
            "qc_server.store.os.replace", side_effect=PermissionError(13, "Access is denied")
        ), mock.patch("qc_server.store.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                store_module._replace(source, self.tmp_path / "target.json")
        self.assertEqual(sleep.call_count, len(store_module._REPLACE_RETRY_DELAYS))


if __name__ == "__main__":
    unittest.main()
