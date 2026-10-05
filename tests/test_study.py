"""A reliability study server, driven over HTTP: set up, readings, end, and results.

A study server (``BONEHUB_QC_MODE=study``) hands the same subjects to several raters, each
several times, blind, and measures how far their verdicts agree. It must leave the dataset --
and any quality-check server on it -- alone, and its results must name no rater.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import unittest
import zipfile

from fastapi.testclient import TestClient

from qc_server.__main__ import main
from qc_server.app import create_app
from qc_server.config import ENV_PREFIX
from qc_server.models import EDITOR, REVIEWER, SubmissionRequest
from qc_server.review import STATIC_DIR
from qc_server.study.app import create_study_app
from qc_server.study.report import readings_csv
from qc_server.study.store import StudyStore

from tests.support import QCTestCase, write_mask

#: The bones of every fixture subject.
BONES = ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"]

#: Six subjects with a segmentation, in dataset 1.
GOOD = [f"001_{n:06d}" for n in range(1, 7)]


class StudyTestCase(QCTestCase):
    """A study server over a temporary dataset, with three raters."""

    raters = ("alice", "bob", "carol")

    def setUp(self) -> None:
        super().setUp()
        self.prepare_dataset()
        self.app = self.build_app()
        self.admin = {"X-Admin-Key": self.store.admin_key}
        self.keys = {name: self.create_rater(name) for name in self.raters}

    def prepare_dataset(self) -> None:
        for subject_id in range(1, 7):
            self.builder.add_subject(1, subject_id, segmentation=dict.fromkeys(BONES, 1))

    def build_app(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_study_app(dataset_root=self.dataset_root, credentials_dir=self.credentials_dir)
        self.store = self.track(app.state.store)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        return app

    @property
    def study(self) -> StudyStore:
        return self.app.state.study

    def create_rater(self, name: str, **payload) -> str:
        payload = {"name": name, "roles": [REVIEWER], **payload}
        response = self.client.post("/admin/api/users", json=payload, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["api_key"]

    def settings(self, **changes) -> dict:
        return {
            "name": "Reliability test",
            "subject_mode": "list",
            "subject_keys": list(GOOD),
            "seed": 1234,
            "raters": list(self.raters),
            "readings_per_rater": 2,
            **changes,
        }

    def preview(self, **changes):
        return self.client.post("/admin/api/study/preview", json=self.settings(**changes), headers=self.admin)

    def start(self, **changes) -> dict:
        response = self.client.post("/admin/api/study/start", json=self.settings(**changes), headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def headers(self, name: str, role: str = REVIEWER) -> dict:
        return {"X-API-Key": self.keys[name], "X-Client-Role": role}

    def next_reading(self, name: str):
        return self.client.post("/api/v1/subjects/next", headers=self.headers(name))

    def handout(self, name: str) -> dict:
        response = self.next_reading(name)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, name: str, handout: dict, rejected=(), files=None, **metadata):
        bones = [label["name"] for label in handout["labels"]]
        body = {
            "quality_check_confirmed": True,
            "use_stored_segmentation": True,
            "confirmed_labels": [bone for bone in bones if bone not in rejected],
            "rejected_labels": list(rejected),
            **metadata,
        }
        return self.client.post(
            f"/api/v1/assignments/{handout['assignment_id']}/submit",
            files={"metadata": (None, json.dumps(body)), **(files or {})},
            headers=self.headers(name),
        )

    def read_all(self, name: str, reject=lambda handout, bone: False) -> int:
        """``name`` reads their whole list; ``reject`` decides each bone. Returns the count."""
        count = 0
        while (response := self.next_reading(name)).status_code == 200:
            handout = response.json()
            rejected = [label["name"] for label in handout["labels"] if reject(handout, label["name"])]
            submitted = self.submit(name, handout, rejected)
            self.assertEqual(submitted.status_code, 200, submitted.text)
            count += 1
        self.assertEqual(response.status_code, 404, response.text)
        return count

    def dataset_files(self) -> dict:
        """Every file under the dataset root, hidden ones included, with its bytes."""
        return {
            path.relative_to(self.dataset_root).as_posix(): path.read_bytes()
            for path in sorted(self.dataset_root.rglob("*"))
            if path.is_file()
        }


# -------------------------------------------------------------------- the server
class ModeTests(QCTestCase):
    def test_the_mode_in_the_environment_makes_a_study_server(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}MODE"] = "study"
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_app()
        self.track(app.state.store)
        self.assertIsInstance(app.state.study, StudyStore)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json()["mode"], "study")

    def test_an_unknown_mode_stops_the_server(self):
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}MODE"] = "studdy"
        with self.assertRaises(RuntimeError):
            create_app()

    def test_the_cli_of_a_study_server_keeps_its_users_off_the_dataset(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        os.environ[f"{ENV_PREFIX}MODE"] = "study"
        self.addCleanup(self.close_cli_logs)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["add-user", "--dataset-root", str(self.dataset_root), "--name", "alice"]), 0)
        self.assertFalse((self.dataset_root / ".bonehub_qc").exists())
        self.assertTrue((self.credentials_dir / "state" / "qc_test_server" / "session.json").exists())

    def close_cli_logs(self) -> None:
        """The CLI opens a store of its own; its log files must be closed for Windows to delete them."""
        import logging

        prefix = f"qc_server.{(self.credentials_dir / 'state').resolve().as_posix()}"
        for name in [n for n in list(logging.Logger.manager.loggerDict) if n.startswith(prefix)]:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logging.Logger.manager.loggerDict.pop(name, None)


class ServerTests(StudyTestCase):
    def test_the_state_is_kept_with_the_credentials(self):
        self.assertEqual(self.store.state_dir, self.credentials_dir / "state" / "qc_test_server")
        self.assertFalse((self.dataset_root / ".bonehub_qc").exists())

    def test_the_quality_checks_endpoints_are_not_there(self):
        for path in ("/admin/api/cases", "/admin/api/stats", "/admin/api/assignments", "/admin/api/config"):
            self.assertEqual(self.client.get(path, headers=self.admin).status_code, 404, path)

    def test_the_session_says_study_mode(self):
        session = self.client.get("/admin/api/session", headers=self.admin).json()
        self.assertEqual(session["mode"], "study")

    def test_the_admin_key_guards_the_study(self):
        self.assertEqual(self.client.get("/admin/api/study").status_code, 401)
        self.assertEqual(self.client.post("/admin/api/study/start", json=self.settings()).status_code, 401)

    def test_a_quality_check_server_has_no_study(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_app(dataset_root=self.dataset_root, credentials_dir=self.make_credentials_dir("qc", "qc_other"))
        self.track(app.state.store)
        with TestClient(app) as client:
            headers = {"X-Admin-Key": app.state.store.admin_key}
            self.assertEqual(client.get("/admin/api/study", headers=headers).status_code, 404)
            self.assertEqual(client.get("/admin/api/session", headers=headers).json()["mode"], "qc")

    def test_3d_slicer_is_turned_away(self):
        response = self.client.get("/api/v1/ping", headers=self.headers("alice", EDITOR))
        self.assertEqual(response.status_code, 403)
        self.assertIn("review", response.json()["detail"])

    def test_ping_tells_a_rater_their_code_once_the_study_runs(self):
        before = self.client.get("/api/v1/ping", headers=self.headers("alice")).json()
        self.assertEqual((before["mode"], before["study"]["code"]), ("study", None))
        self.start()
        after = self.client.get("/api/v1/ping", headers=self.headers("alice")).json()["study"]
        self.assertIn(after["code"], {"A", "B", "C"})
        self.assertEqual((after["readings_done"], after["readings_total"]), (0, 12))


# ------------------------------------------------------------------ setting up
class SetupTests(StudyTestCase):
    def prepare_dataset(self) -> None:
        super().prepare_dataset()
        # 7: no segmentation. 8: a segmentation off the image's voxel grid.
        self.builder.add_subject(1, 7, segmentation={"FEMUR_LEFT": 0})
        self.builder.add_subject(1, 8, segmentation=dict.fromkeys(BONES, 1), write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 8), BONES, spacing=(2.0, 1.0, 1.0))
        for subject_id in range(1, 4):
            self.builder.add_subject(2, subject_id, segmentation=dict.fromkeys(BONES, 1))

    def test_a_preview_says_what_the_settings_make_of_the_study(self):
        preview = self.preview().json()
        self.assertEqual([s["subject_key"] for s in preview["subjects"]], GOOD)
        self.assertEqual({s["bones"] for s in preview["subjects"]}, {3})
        self.assertEqual(sorted(r["code"] for r in preview["raters"]), ["A", "B", "C"])
        self.assertEqual((preview["readings_each"], preview["readings_total"], preview["min_gap"]), (12, 36, 3))

    def test_a_preview_saves_the_draft(self):
        self.preview(name="Draft study")
        overview = self.client.get("/admin/api/study", headers=self.admin).json()
        self.assertEqual((overview["state"], overview["settings"]["name"]), ("draft", "Draft study"))

    def test_typed_subjects_without_a_segmentation_or_off_the_grid_are_refused_with_the_reason(self):
        response = self.preview(subject_keys=[*GOOD, "1_7", "001_000008", "1_99"])
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("001_000007: it has no segmentation", detail)
        self.assertIn("001_000008: its segmentation is not on the image's voxel grid", detail)
        self.assertIn("001_000099: it is not in the dataset's Subject_info", detail)

    def test_a_random_pick_leaves_out_what_cannot_be_read(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                preview = self.preview(
                    subject_mode="random", subject_keys=[], random_count=6, random_dataset_ids=[1], seed=seed
                ).json()
                picked = {s["subject_key"] for s in preview["subjects"]}
                self.assertEqual(picked, set(GOOD), "the six usable subjects of dataset 1, and not 7 or 8")
                for skipped in preview["skipped"]:
                    self.assertEqual(skipped["subject_key"], "001_000008")
                    self.assertIn("voxel grid", skipped["reason"])

    def test_the_same_seed_picks_the_same_subjects(self):
        first = self.preview(subject_mode="random", subject_keys=[], random_count=4, seed=7).json()
        again = self.preview(subject_mode="random", subject_keys=[], random_count=4, seed=7).json()
        self.assertEqual(first["subjects"], again["subjects"])
        self.assertEqual(first["raters"], again["raters"])

    def test_a_pick_larger_than_the_datasets_is_refused(self):
        response = self.preview(subject_mode="random", subject_keys=[], random_count=20)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Only 9 subject(s)", response.json()["detail"])

    def test_a_gap_the_lists_cannot_keep_is_refused(self):
        response = self.preview(min_gap=6)
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 5", response.json()["detail"])

    def test_one_rater_reading_once_has_nothing_to_compare(self):
        response = self.preview(raters=["alice"], readings_per_rater=1)
        self.assertEqual(response.status_code, 400)

    def test_users_who_cannot_be_raters_are_refused(self):
        self.create_rater("eddie", roles=[EDITOR])
        self.create_rater("sam", data_access="segmentation")
        self.create_rater("kim", allowed_dataset_ids="2")
        response = self.preview(raters=["alice", "eddie", "sam", "kim", "nobody"])
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        for expected in ("'eddie' is not a reviewer", "'sam' is sent the segmentation only", "'kim' may not see dataset(s) 1",
                         "'nobody' is not a user"):
            self.assertIn(expected, detail)

    def test_settings_that_are_not_valid_are_explained(self):
        response = self.preview(seed="abc", readings_per_rater=0, subject_keys=["12"])
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("seed", detail)
        self.assertIn("readings_per_rater", detail)
        self.assertIn("'12' is not a subject id", detail)

    def test_the_settings_are_fixed_once_started(self):
        self.start()
        self.assertEqual(self.preview().status_code, 409)
        response = self.client.post("/admin/api/study/start", json=self.settings(), headers=self.admin)
        self.assertEqual(response.status_code, 409)

    def test_start_copies_the_segmentations_and_fixes_the_lists(self):
        overview = self.start()
        self.assertEqual(overview["state"], "running")
        self.assertEqual([s["subject_key"] for s in overview["subjects"]], GOOD)
        study = self.study.study()
        for subject in study.subjects:
            self.assertTrue(self.study.copy_path(subject.subject_key).is_file())
            self.assertEqual(subject.bones, BONES)
        for rater in study.raters:
            self.assertEqual(sorted(rater.order), sorted((key, n) for key in GOOD for n in (1, 2)))


# --------------------------------------------------------------------- reading
class ReadingTests(StudyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start()

    def test_a_rater_is_handed_their_list_in_order(self):
        rater = self.study.study().rater("alice")
        for position in range(1, 4):
            handout = self.handout("alice")
            self.assertEqual(handout["study"]["position"], position)
            self.assertEqual(handout["study"]["total"], 12)
            self.assertEqual(self.submit("alice", handout).status_code, 200)
        readings = self.study.readings()
        self.assertEqual([(r.subject_key, r.reading) for r in readings], [tuple(item) for item in rater.order[:3]])

    def test_a_rater_holds_one_reading_and_asking_again_gives_it_back(self):
        first = self.handout("alice")
        self.assertEqual(self.handout("alice")["assignment_id"], first["assignment_id"])
        held = self.client.get("/api/v1/assignments", headers=self.headers("alice")).json()
        self.assertEqual([h["assignment_id"] for h in held], [first["assignment_id"]])

    def test_release_gives_the_same_reading_back(self):
        first = self.handout("alice")
        response = self.client.post(f"/api/v1/assignments/{first['assignment_id']}/release", headers=self.headers("alice"))
        self.assertEqual(response.status_code, 200)
        again = self.handout("alice")
        self.assertEqual(again["study"]["position"], 1)
        self.assertNotEqual(again["assignment_id"], first["assignment_id"])

    def test_several_raters_read_the_same_subject_at_once(self):
        self.client.delete("/admin/api/study", headers=self.admin)
        self.start(subject_keys=["001_000001"])
        handouts = {name: self.handout(name) for name in self.raters}
        self.assertEqual(len({handout["assignment_id"] for handout in handouts.values()}), 3)
        for name, handout in handouts.items():
            self.assertEqual(self.subject_of(name, handout), "001_000001")
        for name, handout in handouts.items():
            self.assertEqual(self.submit(name, handout).status_code, 200)

    def subject_of(self, name: str, handout: dict) -> str:
        """The subject behind a reading, as the server knows it."""
        held = self.study.held_reading(handout["assignment_id"], self.store.authenticate(self.keys[name]))
        return self.study.reading_context(held)[2].subject_key

    def test_a_reviewer_who_is_not_a_rater_gets_nothing(self):
        self.keys["dave"] = self.create_rater("dave")
        response = self.next_reading("dave")
        self.assertEqual(response.status_code, 403)
        self.assertIn("not a rater", response.json()["detail"])

    def test_a_rater_finishes_their_list(self):
        self.assertEqual(self.read_all("alice"), 12)
        response = self.next_reading("alice")
        self.assertIn("finished all 12", response.json()["detail"])

    def test_another_raters_reading_is_out_of_reach(self):
        handout = self.handout("alice")
        response = self.client.get(f"/api/v1/assignments/{handout['assignment_id']}/image", headers=self.headers("bob"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.submit("bob", handout).status_code, 403)


class BlindTests(StudyTestCase):
    def test_a_reading_shows_no_history_and_no_earlier_verdict(self):
        """Not even Subject_info's: a bone it lists as reviewed (2), or a remark, would say what
        someone made of the subject before."""
        statuses = {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1, "TIBIA_LEFT": 1}
        self.builder.add_subject(1, 9, segmentation=statuses, remarks="QC: hip implant")
        self.start(subject_keys=["001_000009"])
        handout = self.handout("alice")
        self.assertEqual((handout["history"], handout["requests"], handout["subject_info"]), ([], [], {}))
        self.assertNotIn("implant", json.dumps(handout))
        self.assertEqual([label["name"] for label in handout["labels"]], BONES)
        for label in handout["labels"]:
            self.assertEqual((label["state"], label["dataset_status"], label["by"]), ("pending", None, None))

    def test_the_subject_id_is_hidden_unless_the_study_shows_it(self):
        self.start(show_subject_id=False)
        handout = self.handout("alice")
        text = json.dumps(handout)
        self.assertNotIn("001_0000", text)
        self.assertIsNone(handout["subject_key"])
        self.assertEqual(handout["study"]["title"], "Reading 1")
        self.assertEqual(handout["dataset_info"], {"modality": "CT"}, "the dataset is not named either")
        for part, name in (("image", "reading.nii.gz"), ("segmentation", "reading.seg.nrrd")):
            response = self.client.get(f"/api/v1/assignments/{handout['assignment_id']}/{part}", headers=self.headers("alice"))
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'filename="{name}"', response.headers["content-disposition"])

    def test_the_study_can_show_the_subject_id(self):
        self.start(show_subject_id=True)
        handout = self.handout("alice")
        self.assertIn(handout["subject_key"], GOOD)
        self.assertEqual(handout["study"]["title"], handout["subject_key"])

    def test_every_reading_is_served_the_studys_copy(self):
        """The dataset's segmentation may change while the study runs; the raters' does not."""
        self.start()
        copies = {s.subject_key: s.segmentation_sha256 for s in self.study.study().subjects}
        for subject_id in range(1, 7):
            write_mask(self.builder.segmentation_file(1, subject_id), ["SACRUM"])
        handout = self.handout("alice")
        served = self.client.get(f"/api/v1/assignments/{handout['assignment_id']}/segmentation", headers=self.headers("alice"))
        held = self.study.held_reading(handout["assignment_id"], self.store.authenticate(self.keys["alice"]))
        key = self.study.reading_context(held)[2].subject_key
        self.assertEqual(hashlib.sha256(served.content).hexdigest(), copies[key])
        self.assertEqual([label["name"] for label in handout["labels"]], BONES)


class VerdictTests(StudyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start()
        self.reading = self.handout("alice")

    def assert_refused(self, response, words: str, status: int = 400):
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn(words, response.json()["detail"])
        self.assertEqual(self.study.readings(), [], "nothing was recorded")

    def test_every_bone_needs_a_verdict(self):
        self.assert_refused(self.submit("alice", self.reading, confirmed_labels=["FEMUR_LEFT"]), "These have none")

    def test_a_bone_is_accepted_or_rejected_not_both(self):
        response = self.submit("alice", self.reading, rejected=["FEMUR_LEFT"], confirmed_labels=BONES)
        self.assert_refused(response, "both accepted and rejected")

    def test_a_bone_not_in_the_segmentation_is_refused(self):
        self.assert_refused(self.submit("alice", self.reading, rejected=["SACRUM"]), "not bones of this segmentation")

    def test_missing_bones_are_not_part_of_the_study(self):
        self.assert_refused(self.submit("alice", self.reading, missing_labels=["SACRUM"]), "no reports of missing bones")

    def test_rejecting_the_subject_as_a_whole_rejects_every_bone(self):
        response = self.submit("alice", self.reading, quality_check_confirmed=False, comment="hip implant")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["subject_rejected"])
        [reading] = self.study.readings()
        self.assertTrue(reading.subject_rejected)
        self.assertEqual(reading.verdicts, dict.fromkeys(BONES, "reject"))
        self.assertEqual(reading.comment, "hip implant")

    def test_the_readings_csv_shows_rejected_subjects(self):
        self.submit("alice", self.reading, quality_check_confirmed=False, comment="hip implant")
        self.submit("alice", self.handout("alice"), rejected=["TIBIA_LEFT"])
        rows = list(csv.DictReader(io.StringIO(readings_csv(self.study.study(), self.study.readings()))))
        first = [(row["bone"], row["verdict"], row["subject_rejected"]) for row in rows if row["position"] == "1"]
        second = [(row["bone"], row["verdict"], row["subject_rejected"]) for row in rows if row["position"] == "2"]
        self.assertEqual(first, [(bone, "reject", "true") for bone in BONES])
        self.assertEqual(
            second,
            [("FEMUR_LEFT", "accept", "false"), ("FEMUR_RIGHT", "accept", "false"), ("TIBIA_LEFT", "reject", "false")],
        )

    def test_a_reading_uploads_nothing(self):
        upload = {"segmentation": ("seg.seg.nrrd", b"not a file", "application/octet-stream")}
        self.assert_refused(self.submit("alice", self.reading, files=upload), "uploads nothing")

    def test_a_reading_is_recorded_once(self):
        response = self.submit("alice", self.reading, rejected=["TIBIA_LEFT"], comment="  odd tibia ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readings_done"], 1)
        [reading] = self.study.readings()
        self.assertEqual(reading.verdicts, {"FEMUR_LEFT": "accept", "FEMUR_RIGHT": "accept", "TIBIA_LEFT": "reject"})
        self.assertEqual(reading.comment, "odd tibia")
        self.assertEqual(self.submit("alice", self.reading).status_code, 409)

    def test_the_page_and_the_server_agree_on_the_submission(self):
        """The review page sends the quality check's submission; the study reads the same names."""
        script = (STATIC_DIR / "review.js").read_text(encoding="utf-8")
        for field in SubmissionRequest.model_fields:
            self.assertIn(field, script)


# ---------------------------------------------------------------- end and after
class LifecycleTests(StudyTestCase):
    def test_end_stops_the_readings_and_drops_the_open_ones(self):
        self.start()
        open_reading = self.handout("alice")
        self.assertEqual(self.client.post("/admin/api/study/end", headers=self.admin).json()["state"], "ended")
        self.assertEqual(self.submit("alice", open_reading).status_code, 409)
        self.assertEqual(self.next_reading("alice").status_code, 404)
        self.assertEqual(self.study.readings(), [])

    def test_the_study_survives_a_restart(self):
        self.start()
        self.assertEqual(self.submit("alice", self.handout("alice")).status_code, 200)
        held = self.handout("bob")
        self.app = self.build_app()
        self.assertEqual(len(self.study.readings()), 1)
        self.assertEqual(self.handout("bob")["assignment_id"], held["assignment_id"])

    def test_delete_removes_the_study_and_its_files(self):
        self.start()
        self.read_all("alice")
        self.assertEqual(self.client.delete("/admin/api/study", headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get("/admin/api/study", headers=self.admin).json(), {"state": "none"})
        self.assertFalse(self.study.folder.exists())
        self.start(name="The next one")
        self.assertEqual(self.study.readings(), [])

    def test_no_results_before_the_study_starts(self):
        self.assertEqual(self.client.get("/admin/api/study/results", headers=self.admin).status_code, 404)


class IndependenceTests(StudyTestCase):
    def test_a_whole_study_leaves_the_dataset_as_it_was(self):
        before = self.dataset_files()
        self.start()
        for name in self.raters:
            self.read_all(name, reject=lambda handout, bone: bone == "TIBIA_LEFT")
        self.client.post("/admin/api/study/end", headers=self.admin)
        self.client.get("/admin/api/study/results", headers=self.admin)
        self.assertEqual(self.dataset_files(), before)

    def test_a_quality_check_server_on_the_dataset_does_not_see_the_study(self):
        self.start()
        self.handout("alice")  # a study reading open right now
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_app(dataset_root=self.dataset_root, credentials_dir=self.make_credentials_dir("qc", "qc_other"))
        qc = self.track(app.state.store)
        self.assertEqual([s["server_id"] for s in qc.sessions()], ["qc_other"])
        stats = qc.stats()
        self.assertEqual((stats.assigned_by_other_servers, stats.available), (0, 6))


# --------------------------------------------------------------------- results
class ResultsTests(StudyTestCase):
    # Names no figure, number or base64 run could hold by chance: a base64 run has no '_'.
    raters = ("zoltan_k", "ophelia_w", "quincy_v", "yvaine_p")

    def setUp(self) -> None:
        super().setUp()
        self.start(bootstrap_samples=200)
        # Each rater rejects bones by a rule of their own; the last accepts everything.
        rules = {
            "zoltan_k": lambda handout, bone: bone == "TIBIA_LEFT",
            "ophelia_w": lambda handout, bone: bone == "TIBIA_LEFT" and handout["study"]["position"] % 2 == 0,
            "quincy_v": lambda handout, bone: bone == "FEMUR_LEFT" and handout["study"]["position"] % 3 == 0,
            "yvaine_p": lambda handout, bone: False,
        }
        for name, rule in rules.items():
            self.read_all(name, reject=rule)
        self.client.post("/admin/api/study/end", headers=self.admin)

    def results(self) -> zipfile.ZipFile:
        response = self.client.get("/admin/api/study/results", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Reliability_test_results.zip", response.headers["content-disposition"])
        return zipfile.ZipFile(io.BytesIO(response.content))

    def test_the_zip_holds_the_report_the_figures_and_the_csv_files(self):
        names = set(self.results().namelist())
        expected = {"report.html", "readings.csv", "results.csv"}
        for stem in ("figure1_intra_rater", "figure2_inter_rater_pairs", "figure3_inter_rater_group"):
            expected |= {f"figures/{stem}.svg", f"figures/{stem}.png"}
        self.assertEqual(names, expected)

    def test_no_file_names_a_rater(self):
        archive = self.results()
        for name in archive.namelist():
            text = archive.read(name).decode("utf-8", errors="ignore").lower()
            for rater in self.raters:
                self.assertNotIn(rater, text, f"{name} names {rater}")
        report = self.client.get("/admin/api/study/report", headers=self.admin).text
        self.assertIn("Rater A", report)
        self.assertFalse(any(rater in report for rater in self.raters))

    def test_the_readings_csv_has_a_row_per_bone_per_reading(self):
        rows = list(csv.DictReader(io.StringIO(self.results().read("readings.csv").decode("utf-8"))))
        self.assertEqual(len(rows), 4 * 12 * 3)
        self.assertEqual(set(rows[0]), {"rater", "subject", "reading", "position", "bone", "verdict", "subject_rejected",
                                        "handed_out_at", "submitted_at", "comment"})
        self.assertEqual({row["rater"] for row in rows}, {"A", "B", "C", "D"})
        self.assertEqual({row["verdict"] for row in rows}, {"accept", "reject"})
        self.assertEqual({row["subject_rejected"] for row in rows}, {"false"})

    def test_the_results_csv_has_every_comparison(self):
        rows = list(csv.DictReader(io.StringIO(self.results().read("results.csv").decode("utf-8"))))
        kinds = [row["comparison"] for row in rows]
        self.assertEqual(kinds.count("intra-rater"), 4)
        self.assertEqual(kinds.count("intra-rater group"), 1)
        self.assertEqual(kinds.count("inter-rater pair"), 6)
        self.assertEqual(kinds.count("inter-rater group"), 1)
        code = self.study.study().rater("yvaine_p").code
        always_accepts = next(row for row in rows if row["comparison"] == "intra-rater" and row["raters"] == code)
        self.assertEqual(always_accepts["agreement_percent"], "100.0000")
        self.assertEqual(always_accepts["alpha"], "", "undefined: every verdict was the same")

    def test_intra_rater_for_all_raters_pools_every_raters_items(self):
        rows = list(csv.DictReader(io.StringIO(self.results().read("results.csv").decode("utf-8"))))
        each = [row for row in rows if row["comparison"] == "intra-rater"]
        group = next(row for row in rows if row["comparison"] == "intra-rater group")
        self.assertEqual(int(group["items"]), sum(int(row["items"]) for row in each))
        # Every rater judged the same items, so the pooled % agreement is the raters' mean.
        mean = sum(float(row["agreement_percent"]) for row in each) / len(each)
        self.assertAlmostEqual(float(group["agreement_percent"]), mean, places=3)
        self.assertNotEqual(group["ac1_low"], "", "the pooled numbers have intervals too")

    def test_both_sections_open_with_all_raters_together(self):
        report = self.client.get("/admin/api/study/report", headers=self.admin).text
        intra, inter = report.split("<h2>Intra-rater reliability</h2>")[1].split("<h2>Inter-rater reliability</h2>")
        for section in (intra, inter):
            self.assertIn("All raters together:", section)
            self.assertIn('class="tiles"', section)
            self.assertIn("<td>All raters</td>", section)

    def test_the_same_readings_give_the_same_numbers(self):
        first = self.results().read("results.csv")
        self.assertEqual(self.results().read("results.csv"), first)

    def test_the_figures_are_numbered_without_a_gap(self):
        # Two raters: no pair grid, so the inter-rater figure is the second.
        self.client.delete("/admin/api/study", headers=self.admin)
        self.start(raters=["zoltan_k", "ophelia_w"])
        self.read_all("zoltan_k")
        self.read_all("ophelia_w", reject=lambda handout, bone: bone == "TIBIA_LEFT")
        self.assertEqual(sorted(n for n in self.results().namelist() if n.endswith(".svg")),
                         ["figures/figure1_intra_rater.svg", "figures/figure2_inter_rater_group.svg"])
        report = self.client.get("/admin/api/study/report", headers=self.admin).text
        self.assertIn("<b>Figure 1.</b> Intra-rater", report)
        self.assertIn("<b>Figure 2.</b> Inter-rater", report)
        self.assertIn("(figure2_inter_rater_group.svg / .png)", report)
        self.assertNotIn("Figure 3", report)

    def test_the_report_is_provisional_while_the_study_runs(self):
        self.client.delete("/admin/api/study", headers=self.admin)
        self.start()
        self.read_all("zoltan_k")
        report = self.client.get("/admin/api/study/report", headers=self.admin).text
        self.assertIn("Provisional", report)
        self.assertIn("Figure 1.", report, "one rater who read everything twice can be compared with themselves")
        self.assertIn("Nothing to compare yet: no subject has been read by two raters", report)
        self.assertEqual([n for n in self.results().namelist() if n.startswith("figures/")],
                         ["figures/figure1_intra_rater.svg", "figures/figure1_intra_rater.png"])


class AdminPageTests(StudyTestCase):
    """The panel cannot import the server's names, so it must spell them as the server does."""

    def test_the_panel_uses_the_study_endpoints(self):
        page = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        for endpoint in ('"/admin/api/study"', '"/admin/api/study/preview"', '"/admin/api/study/start"',
                         '"/admin/api/study/end"', '"/admin/api/study/report"', '"/admin/api/study/results"'):
            self.assertIn(endpoint, page)

    def test_the_panel_sends_the_settings_the_server_reads(self):
        from qc_server.study.models import StudySettings

        page = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        for field in StudySettings.model_fields:
            self.assertIn(field, page)


if __name__ == "__main__":
    unittest.main()
