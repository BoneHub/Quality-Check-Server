"""The study a study server runs: its settings, its start and end, and every reading.

The study server's :class:`~qc_server.store.QCStore` keeps the user accounts and reads the
dataset; this store keeps the study, in a folder of the server's state (see
:mod:`qc_server.study`). A re-entrant lock serialises it. Copying the subjects' segmentations
at Start runs outside the lock, so that raters' requests are not held up by it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path

from bonehub_data_schema import SEGMENTATION_SUFFIX, SubjectInfo, is_compatible_schema_version

from ..audit import utc_now_iso
from ..models import DATA_ACCESS_DESCRIPTIONS, DEFAULT_DATA_ACCESS, User
from ..segmentation import SegmentationError, check_stored_geometry, read_segment_table
from ..store import QCError, QCStore, _atomic_write_json, _read_json, file_fingerprint, subject_key_of
from .models import (
    ACCEPT,
    REJECT,
    HeldReading,
    Reading,
    ReadingResult,
    Study,
    StudyRater,
    StudySettings,
    StudySubject,
)
from .schedule import default_gap, gap_problem, pick_subjects, rater_codes, reading_orders, subject_ids

STUDY_DIR_NAME = "study"
STUDY_FILE_NAME = "study.json"
HELD_FILE_NAME = "held.json"
READINGS_FILE_NAME = "readings.jsonl"
COPIES_DIR_NAME = "segmentations"

#: At most this many skipped subjects are listed by a preview.
MAX_SKIPPED_LISTED = 50


class StudyStore:
    """The study of one study server, and the readings of its raters."""

    def __init__(self, store: QCStore):
        self.store = store
        self.folder = store.state_dir / STUDY_DIR_NAME
        self._lock = threading.RLock()
        #: Set while Start copies the segmentations, so nothing else changes the study meanwhile.
        self._starting = False
        self._study: Study | None = self._load_study()
        self._held: dict[str, HeldReading] = self._load_held()
        self._readings: list[Reading] = self._load_readings()

    # ------------------------------------------------------------------ files
    @property
    def study_path(self) -> Path:
        return self.folder / STUDY_FILE_NAME

    @property
    def held_path(self) -> Path:
        return self.folder / HELD_FILE_NAME

    @property
    def readings_path(self) -> Path:
        return self.folder / READINGS_FILE_NAME

    @property
    def copies_dir(self) -> Path:
        return self.folder / COPIES_DIR_NAME

    def copy_path(self, subject_key: str) -> Path:
        """Where the study keeps its copy of a subject's segmentation."""
        return self.copies_dir / f"{subject_key}{SEGMENTATION_SUFFIX}"

    def _load_study(self) -> Study | None:
        raw = _read_json(self.study_path)
        return Study(**raw) if isinstance(raw, dict) else None

    def _load_held(self) -> dict[str, HeldReading]:
        raw = _read_json(self.held_path)
        return {entry["rater"]: HeldReading(**entry) for entry in raw} if isinstance(raw, list) else {}

    def _load_readings(self) -> list[Reading]:
        if not self.readings_path.exists():
            return []
        readings = []
        with open(self.readings_path, "r", encoding="utf-8") as f:
            for number, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    readings.append(Reading(**json.loads(line)))
                except ValueError as exc:  # a line cut short by a crash, or edited by hand
                    self.store.audit.event(
                        f"Line {number} of {READINGS_FILE_NAME} could not be read and was skipped: {exc}",
                        level=logging.WARNING,
                    )
        return readings

    def _save_study(self) -> None:
        """Caller holds the lock."""
        _atomic_write_json(self.study_path, self._study.model_dump() if self._study else None)

    def _save_held(self) -> None:
        """Caller holds the lock."""
        _atomic_write_json(self.held_path, [held.model_dump() for held in self._held.values()])

    def _append_reading(self, reading: Reading) -> None:
        """Caller holds the lock. A reading is the study's data: it is on disk before it counts."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.readings_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(reading.model_dump(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ------------------------------------------------------------- reading it
    def study(self) -> Study | None:
        """A copy of the study, or None when there is none."""
        with self._lock:
            return self._study.model_copy(deep=True) if self._study else None

    def readings(self) -> list[Reading]:
        """Every reading submitted, in the order they came."""
        with self._lock:
            return [reading.model_copy(deep=True) for reading in self._readings]

    def overview(self) -> dict:
        """The study as the admin panel shows it, with each rater's progress."""
        with self._lock:
            study = self._study
            if study is None:
                return {"state": "none"}
            done = self._done_counts()
            last: dict[str, str] = {}
            for reading in self._readings:
                last[reading.rater] = reading.submitted_at
            return {
                "state": study.state,
                "study_id": study.study_id,
                "created_at": study.created_at,
                "updated_at": study.updated_at,
                "started_at": study.started_at,
                "ended_at": study.ended_at,
                "settings": study.settings.model_dump(),
                "min_gap": study.min_gap,
                "subjects": [
                    {
                        "subject_key": subject.subject_key,
                        "dataset_id": subject.dataset_id,
                        "subject_id": subject.subject_id,
                        "bones": len(subject.bones),
                    }
                    for subject in study.subjects
                ],
                "raters": [
                    {
                        "name": rater.name,
                        "code": rater.code,
                        "done": done.get(rater.name, 0),
                        "total": len(rater.order),
                        "holding": self._held[rater.name].position if rater.name in self._held else None,
                        "last_reading_at": last.get(rater.name),
                    }
                    for rater in sorted(study.raters, key=lambda rater: (len(rater.code), rater.code))
                ],
                "readings": len(self._readings),
            }

    def _done_counts(self) -> dict[str, int]:
        """Caller holds the lock. Readings submitted, by rater."""
        done: dict[str, int] = {}
        for reading in self._readings:
            done[reading.rater] = done.get(reading.rater, 0) + 1
        return done

    # --------------------------------------------------- preview, start, end
    def preview(self, settings: StudySettings) -> dict:
        """Save the settings as the study's draft, and say what they make of the study: its
        subjects, the raters' codes, and how many readings each rater has. Changes nothing
        once the study has started."""
        with self._lock:
            self._require_draft()
            now = utc_now_iso()
            if self._study is None:
                self._study = Study(
                    study_id=uuid.uuid4().hex, settings=settings, state="draft", created_at=now, updated_at=now
                )
            else:
                self._study.settings = settings
                self._study.updated_at = now
            self._save_study()
        return self._plan(settings).summary()

    def start(self, settings: StudySettings) -> dict:
        """Start the study with these settings: fix its subjects and copy their segmentations,
        give each rater a code and a list, and begin handing out readings."""
        with self._lock:
            self._require_draft()
            self._starting = True
        try:
            plan = self._plan(settings)
            subjects = self._copy_segmentations(plan)
            codes = plan.codes
            keys = [subject.subject_key for subject in subjects]
            orders = reading_orders(keys, codes, settings.readings_per_rater, plan.gap, settings.seed)
            now = utc_now_iso()
            with self._lock:
                previous = self._study
                self._study = Study(
                    study_id=previous.study_id if previous else uuid.uuid4().hex,
                    settings=settings,
                    state="running",
                    created_at=previous.created_at if previous else now,
                    updated_at=now,
                    started_at=now,
                    min_gap=plan.gap,
                    subjects=subjects,
                    raters=[StudyRater(name=name, code=codes[name], order=orders[name]) for name in sorted(codes)],
                )
                self._held = {}
                self._readings = []
                self.readings_path.unlink(missing_ok=True)
                self._save_held()
                self._save_study()
        finally:
            with self._lock:
                self._starting = False
        self.store.audit.record(
            "study_started",
            {
                "study": settings.name,
                "subjects": [subject.subject_key for subject in subjects],
                "raters": {name: codes[name] for name in sorted(codes)},
                "readings_per_rater": settings.readings_per_rater,
                "min_gap": plan.gap,
                "seed": settings.seed,
            },
            summary=(
                f"Study '{settings.name}' started: {len(subjects)} subject(s), {len(codes)} rater(s), "
                f"{settings.readings_per_rater} reading(s) each, minimum gap {plan.gap}, seed {settings.seed}."
            ),
        )
        return self.overview()

    def end(self) -> dict:
        """Stop handing out readings. A reading that is open is not recorded any more."""
        with self._lock:
            study = self._study
            if study is None or study.state != "running":
                raise QCError("No study is running, so there is nothing to end.", status_code=409)
            now = utc_now_iso()
            study.state = "ended"
            study.ended_at = now
            study.updated_at = now
            dropped = sorted(self._held)
            self._held = {}
            self._save_held()
            self._save_study()
            count = len(self._readings)
        self.store.audit.record(
            "study_ended",
            {"study": study.settings.name, "readings": count, "open_readings_dropped": dropped},
            summary=f"Study '{study.settings.name}' ended with {count} reading(s).",
        )
        return self.overview()

    def delete(self) -> None:
        """Remove the study, its readings and its copies, so another can be set up."""
        with self._lock:
            if self._starting:
                raise QCError("The study is being started; wait until it has.", status_code=409)
            if self._study is None:
                raise QCError("There is no study to delete.", status_code=404)
            name, state, count = self._study.settings.name, self._study.state, len(self._readings)
            # The study file first: without it, whatever else is left is no study after a restart.
            self.study_path.unlink(missing_ok=True)
            shutil.rmtree(self.folder, ignore_errors=True)
            self._study = None
            self._held = {}
            self._readings = []
        self.store.audit.record(
            "study_deleted",
            {"study": name, "state": state, "readings": count},
            summary=f"Study '{name}' deleted ({state}, {count} reading(s)).",
        )

    def _require_draft(self) -> None:
        """Caller holds the lock. Settings change only before Start."""
        if self._starting:
            raise QCError("The study is being started; wait until it has.", status_code=409)
        if self._study is not None and self._study.state != "draft":
            raise QCError(
                f"The study '{self._study.settings.name}' has started already, so its settings are fixed. "
                + ("End it and delete it" if self._study.state == "running" else "Delete it")
                + " to set up another.",
                status_code=409,
            )

    # ------------------------------------------------------------ the plan
    def _plan(self, settings: StudySettings) -> "_Plan":
        """What the settings make of the study on the dataset as it is now; QCError when they
        cannot make one."""
        reader = _DatasetReader(self.store)
        skipped: list[tuple[str, str]] = []
        if settings.subject_mode == "list":
            problems = [(key, reader.problem_of(key)) for key in settings.subject_keys]
            problems = [(key, problem) for key, problem in problems if problem]
            if problems:
                raise QCError(
                    "These subjects cannot be in the study: "
                    + "; ".join(f"{key}: {problem}" for key, problem in problems)
                    + "."
                )
            keys = list(settings.subject_keys)
        else:
            for dataset_id in settings.random_dataset_ids or []:
                problem = reader.dataset_problem(dataset_id)
                if problem:
                    raise QCError(f"Dataset {dataset_id} cannot be used: {problem}.")
            candidates = reader.candidates(settings.random_dataset_ids)
            keys, skipped = pick_subjects(candidates, settings.random_count, settings.seed, reader.problem_of)
            if len(keys) < settings.random_count:
                where = (
                    f"dataset(s) {', '.join(map(str, settings.random_dataset_ids))}"
                    if settings.random_dataset_ids
                    else "the datasets"
                )
                raise QCError(
                    f"Only {len(keys)} subject(s) of {where} can be in the study, with an image and a segmentation "
                    f"on its voxel grid; {settings.random_count} were asked for."
                )

        n = len(keys)
        gap = default_gap(n) if settings.min_gap is None else settings.min_gap
        problem = gap_problem(gap, n, settings.readings_per_rater)
        if problem:
            raise QCError(problem)
        if len(settings.raters) < 2 and settings.readings_per_rater < 2:
            raise QCError(
                "With one rater who reads each subject once there is nothing to compare. Choose two raters or more, "
                "or two readings per rater or more."
            )

        dataset_ids = sorted({subject_ids(key)[0] for key in keys})
        users = {user["name"]: user for user in self.store.list_users()}
        rater_problems = [
            problem for name in settings.raters if (problem := _rater_problem(name, users.get(name), dataset_ids))
        ]
        if rater_problems:
            raise QCError("These users cannot be raters: " + "; ".join(rater_problems) + ".")

        return _Plan(
            settings=settings,
            subject_keys=keys,
            bones={key: reader.bones_of(key) for key in keys},
            skipped=skipped,
            gap=gap,
            codes=rater_codes(settings.raters, settings.seed),
        )

    def _copy_segmentations(self, plan: "_Plan") -> list[StudySubject]:
        """Copy each study subject's segmentation into the study's folder, and check the copy:
        what the raters see is this copy, whatever happens to the dataset meanwhile."""
        if self.copies_dir.exists():
            shutil.rmtree(self.copies_dir)
        self.copies_dir.mkdir(parents=True)
        subjects = []
        for key in plan.subject_keys:
            dataset_id, subject_id = subject_ids(key)
            target = self.copy_path(key)
            partial = target.with_name(target.name + ".tmp")
            shutil.copyfile(self.store.segmentation_path(dataset_id, subject_id), partial)
            os.replace(partial, target)
            problem = _segmentation_problem(target, self.store.image_path(dataset_id, subject_id))
            if problem:
                raise QCError(f"Subject {key} cannot be in the study: {problem}. Nothing was started.")
            subjects.append(
                StudySubject(
                    subject_key=key,
                    dataset_id=dataset_id,
                    subject_id=subject_id,
                    bones=_bones_of(target),
                    segmentation_sha256=file_fingerprint(target).sha256,
                )
            )
        return subjects

    # -------------------------------------------------------------- raters
    def rater_status(self, user: User) -> dict:
        """What the review page is told about the study at sign-in."""
        with self._lock:
            study = self._study
            rater = study.rater(user.name) if study else None
            return {
                "state": study.state if study else None,
                "code": rater.code if rater else None,
                "readings_done": self._done_counts().get(user.name, 0) if rater else 0,
                "readings_total": len(rater.order) if rater else 0,
                "show_subject_id": bool(study and study.settings.show_subject_id),
            }

    def next_reading(self, user: User) -> HeldReading:
        """The rater's next reading: the one they hold, or else the next in their list."""
        _require_reader(user)
        with self._lock:
            study, rater = self._running_study_of(user)
            held = self._held.get(user.name)
            if held is not None:
                return held
            done = self._done_counts().get(user.name, 0)
            if done >= len(rater.order):
                raise QCError(
                    f"You have finished all {len(rater.order)} of your study readings. Thank you.", status_code=404
                )
            subject = study.subject(rater.order[done][0])
            _require_dataset(user, subject.dataset_id)
            held = HeldReading(
                assignment_id=uuid.uuid4().hex, rater=user.name, position=done + 1, handed_at=utc_now_iso()
            )
            self._held[user.name] = held
            self._save_held()
        self.store.audit.record(
            "study_handed_out",
            {
                "assignment_id": held.assignment_id,
                "user": user.name,
                "code": rater.code,
                "position": held.position,
                "subject_key": subject.subject_key,
            },
            summary=f"Study reading {held.position} of {len(rater.order)} handed to '{user.name}' ({subject.subject_key}).",
        )
        return held

    def held_by(self, user: User) -> list[HeldReading]:
        """The reading the user has open, if any."""
        with self._lock:
            held = self._held.get(user.name)
            return [held] if held is not None else []

    def held_reading(self, assignment_id: str, user: User) -> HeldReading:
        """The open reading of that id, which must be this user's."""
        with self._lock:
            held = next((held for held in self._held.values() if held.assignment_id == assignment_id), None)
            if held is None:
                if any(reading.assignment_id == assignment_id for reading in self._readings):
                    raise QCError("This reading was submitted already.", status_code=409)
                raise QCError(
                    "This reading is not open: it was released, the study ended, or it never existed. Ask for the "
                    "next subject.",
                    status_code=404,
                )
            if held.rater != user.name:
                raise QCError("This reading belongs to another rater.", status_code=403)
            return held

    def reading_context(self, held: HeldReading) -> tuple[Study, StudyRater, StudySubject]:
        """The study, rater and subject of an open reading."""
        with self._lock:
            study = self._study
            if study is None:
                raise QCError("There is no study on this server.", status_code=404)
            rater = study.rater(held.rater)
            subject = study.subject(rater.order[held.position - 1][0])
            return study.model_copy(deep=True), rater.model_copy(deep=True), subject.model_copy(deep=True)

    def release(self, assignment_id: str, user: User) -> HeldReading:
        """Hand an open reading back. It is the rater's next reading all the same: readings
        cannot be skipped."""
        with self._lock:
            held = self.held_reading(assignment_id, user)
            del self._held[user.name]
            self._save_held()
        self.store.audit.record(
            "study_released",
            {"assignment_id": assignment_id, "user": user.name, "position": held.position},
            summary=f"'{user.name}' released study reading {held.position}; it comes back to them next.",
        )
        return held

    def submit(
        self,
        assignment_id: str,
        user: User,
        accepted: list[str] | None,
        rejected: list[str] | None,
        comment: str | None,
        subject_rejected: bool = False,
    ) -> ReadingResult:
        """Record a reading: a verdict on every bone of the subject, accept or reject -- or the
        subject rejected as a whole, which rejects every bone and sets the rest aside."""
        _require_reader(user)
        rejected = list(rejected or [])
        if subject_rejected:
            accepted, rejected = [], []
        elif accepted is None:
            raise QCError("Name the bones you accept in confirmed_labels, and those you reject in rejected_labels.")
        for name, given in (("accepted", accepted), ("rejected", rejected)):
            twice = sorted({bone for bone in given if given.count(bone) > 1})
            if twice:
                raise QCError(f"These bones are {name} twice: {', '.join(twice)}.")
        comment = (comment or "").strip() or None
        with self._lock:
            study, rater = self._running_study_of(user, submitting=True)
            held = self.held_reading(assignment_id, user)
            subject_key, reading_number = rater.order[held.position - 1]
            bones = set(study.subject(subject_key).bones)
            if subject_rejected:
                rejected = sorted(bones)
            both = sorted(set(accepted) & set(rejected))
            unknown = sorted((set(accepted) | set(rejected)) - bones)
            unjudged = sorted(bones - set(accepted) - set(rejected))
            if both:
                raise QCError(f"These bones are both accepted and rejected: {', '.join(both)}.")
            if unknown:
                raise QCError(f"These are not bones of this segmentation: {', '.join(unknown)}.")
            if unjudged:
                raise QCError(f"Give every bone a verdict, accept or reject. These have none: {', '.join(unjudged)}.")
            reading = Reading(
                assignment_id=assignment_id,
                rater=user.name,
                code=rater.code,
                position=held.position,
                subject_key=subject_key,
                reading=reading_number,
                handed_at=held.handed_at,
                submitted_at=utc_now_iso(),
                verdicts={bone: (REJECT if bone in rejected else ACCEPT) for bone in sorted(bones)},
                subject_rejected=subject_rejected,
                comment=comment,
            )
            self._append_reading(reading)
            self._readings.append(reading)
            del self._held[user.name]
            self._save_held()
            done = self._done_counts()[user.name]
            total = len(rater.order)
        self.store.audit.record(
            "study_reading",
            {
                "assignment_id": assignment_id,
                "user": user.name,
                "code": rater.code,
                "position": reading.position,
                "subject_key": subject_key,
                "reading": reading_number,
                "accepted": len(bones) - len(rejected),
                "rejected": len(rejected),
                "subject_rejected": subject_rejected,
                "comment": comment,
            },
            summary=(
                f"Study reading {reading.position} of {total} by '{user.name}' ({subject_key}): "
                + (
                    "the subject rejected."
                    if subject_rejected
                    else f"{len(bones) - len(rejected)} accepted, {len(rejected)} rejected."
                )
            ),
        )
        return ReadingResult(
            assignment_id=assignment_id,
            accepted_labels=sorted(bones - set(rejected)),
            rejected_labels=sorted(rejected),
            subject_rejected=subject_rejected,
            readings_done=done,
            readings_total=total,
            message=(
                f"Reading {reading.position} of {total} recorded"
                + (": the subject rejected." if subject_rejected else ".")
                + (" That was your last one. Thank you." if done == total else "")
            ),
        )

    def _running_study_of(self, user: User, submitting: bool = False) -> tuple[Study, StudyRater]:
        """Caller holds the lock. The running study, and the user as one of its raters."""
        study = self._study
        if study is None or study.state == "draft":
            raise QCError("No study is running on this server yet.", status_code=404)
        if study.state == "ended":
            raise QCError(
                "The study has ended" + ("; this reading was not recorded." if submitting else ". Thank you."),
                status_code=409 if submitting else 404,
            )
        rater = study.rater(user.name)
        if rater is None:
            raise QCError(f"'{user.name}' is not a rater in this study.", status_code=403)
        return study, rater


class _Plan:
    """What a study's settings make of it, before it starts."""

    def __init__(self, settings, subject_keys, bones, skipped, gap, codes):
        self.settings = settings
        self.subject_keys = subject_keys
        self.bones = bones
        self.skipped = skipped
        self.gap = gap
        self.codes = codes

    def summary(self) -> dict:
        n = len(self.subject_keys)
        readings = self.settings.readings_per_rater
        return {
            "settings": self.settings.model_dump(),
            "subjects": [
                {
                    "subject_key": key,
                    "dataset_id": subject_ids(key)[0],
                    "subject_id": subject_ids(key)[1],
                    "bones": len(self.bones[key]),
                }
                for key in self.subject_keys
            ],
            "skipped": [{"subject_key": key, "reason": reason} for key, reason in self.skipped[:MAX_SKIPPED_LISTED]],
            "skipped_count": len(self.skipped),
            "raters": [
                {"name": name, "code": code}
                for name, code in sorted(self.codes.items(), key=lambda item: (len(item[1]), item[1]))
            ],
            "readings_per_rater": readings,
            "min_gap": self.gap,
            "readings_each": n * readings,
            "readings_total": n * readings * len(self.codes),
        }


class _DatasetReader:
    """The dataset as one preview or start reads it: each file read once."""

    def __init__(self, store: QCStore):
        self.store = store
        self._dataset_problems: dict[int, str | None] = {}
        self._subjects: dict[int, dict[int, SubjectInfo]] = {}
        self._bones: dict[str, list[str]] = {}

    def dataset_problem(self, dataset_id: int) -> str | None:
        """Why a dataset cannot be used, or None."""
        if dataset_id not in self._dataset_problems:
            self._dataset_problems[dataset_id] = self._find_dataset_problem(dataset_id)
        return self._dataset_problems[dataset_id]

    def _find_dataset_problem(self, dataset_id: int) -> str | None:
        allowed = self.store.config.allowed_dataset_ids
        if allowed is not None and dataset_id not in allowed:
            return "this server is restricted to other datasets (ALLOWED_DATASET_IDS)"
        if not self.store.dataset_path(dataset_id).is_dir():
            return "there is no such dataset"
        try:
            with open(self.store.dataset_info_path(dataset_id), "r", encoding="utf-8") as f:
                version = json.load(f).get("schema_version")
        except (OSError, ValueError, AttributeError) as exc:
            return f"its Dataset_info file could not be read ({exc})"
        if not is_compatible_schema_version(version):
            return f"it is written with schema version {version or '(not recorded)'}, which this server does not read"
        if self.subjects(dataset_id) is None:
            return "its Subject_info file could not be read"
        return None

    def subjects(self, dataset_id: int) -> dict[int, SubjectInfo] | None:
        """The dataset's subjects by id, or None when its Subject_info cannot be read."""
        if dataset_id not in self._subjects:
            try:
                with open(self.store.subject_info_path(dataset_id), "r", encoding="utf-8") as f:
                    entries = [SubjectInfo(**entry) for entry in json.load(f)]
            except (OSError, ValueError, TypeError):
                self._subjects[dataset_id] = None
            else:
                self._subjects[dataset_id] = {s.subject_id: s for s in entries if s.subject_id is not None}
        return self._subjects[dataset_id]

    def candidates(self, dataset_ids: list[int] | None) -> list[str]:
        """The subjects a random pick draws from: those that Subject_info says have an image and
        a segmentation, in the usable datasets among ``dataset_ids`` (None: every dataset)."""
        keys = []
        for folder in sorted(self.store.dataset_root.glob("Dataset_*")):
            try:
                dataset_id = int(folder.name.split("_")[1])
            except (IndexError, ValueError):
                continue
            if dataset_ids is not None and dataset_id not in dataset_ids:
                continue
            if not folder.is_dir() or self.dataset_problem(dataset_id):
                continue
            for subject_id, subject in sorted(self.subjects(dataset_id).items()):
                if subject.image and subject.available_labels("segmentation"):
                    keys.append(subject_key_of(dataset_id, subject_id))
        return keys

    def problem_of(self, subject_key: str) -> str | None:
        """Why a subject cannot be in the study, or None: it needs an image, and a segmentation
        on the image's voxel grid that holds a bone."""
        dataset_id, subject_id = subject_ids(subject_key)
        problem = self.dataset_problem(dataset_id)
        if problem:
            return f"dataset {dataset_id} cannot be used: {problem}"
        if subject_id not in self.subjects(dataset_id):
            return "it is not in the dataset's Subject_info"
        image = self.store.image_path(dataset_id, subject_id)
        if not image.exists():
            return "its image file is missing"
        segmentation = self.store.segmentation_path(dataset_id, subject_id)
        if not segmentation.exists():
            return "it has no segmentation"
        return _segmentation_problem(segmentation, image)

    def bones_of(self, subject_key: str) -> list[str]:
        """The bones of a subject's segmentation, as its header lists them."""
        if subject_key not in self._bones:
            self._bones[subject_key] = _bones_of(self.store.segmentation_path(*subject_ids(subject_key)))
        return self._bones[subject_key]


# --------------------------------------------------------------------- helpers
def _rater_problem(name: str, user: dict | None, dataset_ids: list[int]) -> str | None:
    """Why a user, as the user list gives them, cannot rate subjects of these datasets, or None."""
    if user is None:
        return f"'{name}' is not a user of this server"
    if not user["active"]:
        return f"'{name}' is disabled"
    if "reviewer" not in user["roles"]:
        return f"'{name}' is not a reviewer"
    if user["data_access"] != DEFAULT_DATA_ACCESS:
        return (
            f"'{name}' is sent {DATA_ACCESS_DESCRIPTIONS[user['data_access']]}, but a rater must be sent "
            f"{DATA_ACCESS_DESCRIPTIONS[DEFAULT_DATA_ACCESS]}"
        )
    allowed = user["allowed_dataset_ids"]
    barred = [dataset_id for dataset_id in dataset_ids if allowed is not None and dataset_id not in allowed]
    if barred:
        return f"'{name}' may not see dataset(s) {', '.join(map(str, barred))}"
    return None


def _segmentation_problem(segmentation: Path, image: Path) -> str | None:
    """Why a segmentation cannot be read in the study, or None."""
    try:
        segments = read_segment_table(segmentation)
    except SegmentationError as exc:
        return f"its segmentation cannot be read: {exc}"
    if not segments:
        return "its segmentation holds no bones"
    try:
        check_stored_geometry(segmentation, image)
    except SegmentationError as exc:
        return f"its segmentation is not on the image's voxel grid: {exc}"
    return None


def _bones_of(segmentation: Path) -> list[str]:
    """The labels a segmentation's header lists, each once, in label order."""
    by_label = {segment.label: segment.value for segment in read_segment_table(segmentation)}
    return sorted(by_label, key=lambda label: by_label[label])


def _require_reader(user: User) -> None:
    """A reading shows the image and the segmentation, so a rater must be sent both."""
    if user.data_access != DEFAULT_DATA_ACCESS:
        raise QCError(
            f"Study readings show {DATA_ACCESS_DESCRIPTIONS[DEFAULT_DATA_ACCESS]}, but '{user.name}' is sent "
            f"{DATA_ACCESS_DESCRIPTIONS[user.data_access]}. Ask the administrator to send you both.",
            status_code=403,
        )


def _require_dataset(user: User, dataset_id: int) -> None:
    if user.allowed_dataset_ids is not None and dataset_id not in user.allowed_dataset_ids:
        raise QCError(
            f"'{user.name}' may not see dataset {dataset_id}, which your next reading is from. Ask the administrator.",
            status_code=403,
        )
