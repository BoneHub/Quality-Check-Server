"""What a study server keeps and sends: the study's settings, the study itself, and each reading."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import REVIEWER, DataAccess, HandoutLabel, HandoutSegment, Role
from .schedule import normalized_subject_key

#: ``draft`` while the settings can still change, ``running`` from Start, ``ended`` from End.
StudyState = Literal["draft", "running", "ended"]

#: How the study's subjects are chosen: a list the administrator types, or a random pick.
SubjectMode = Literal["list", "random"]

#: A rater's verdict on one bone.
Verdict = Literal["accept", "reject"]

ACCEPT: Verdict = "accept"
REJECT: Verdict = "reject"

#: The verdicts, in the order the statistics number them.
VERDICTS: tuple[Verdict, ...] = (ACCEPT, REJECT)

#: The most readings of each subject a rater can be given.
MAX_READINGS_PER_RATER = 20

#: Bounds of the number of bootstrap samples behind each confidence interval.
MIN_BOOTSTRAP_SAMPLES = 100
MAX_BOOTSTRAP_SAMPLES = 100_000

#: The largest seed: a browser holds integers exactly only up to here, and the admin panel is one.
MAX_SEED = 2**53 - 1


class StudySettings(BaseModel):
    """Everything the administrator decides before a study starts. Fixed from Start on."""

    name: str = Field(..., description="Names the study in the report")
    subject_mode: SubjectMode = Field(..., description="'list': the subjects typed in; 'random': a random pick")
    subject_keys: list[str] = Field(
        default_factory=list, description="The study's subjects, as '<dataset>_<subject>' keys, for 'list'"
    )
    random_count: int | None = Field(None, ge=1, description="How many subjects a random pick takes")
    random_dataset_ids: list[int] | None = Field(
        None, description="The datasets a random pick takes them from; None means every dataset"
    )
    seed: int = Field(
        ...,
        ge=0,
        le=MAX_SEED,
        description="Fixes the random pick, the rater codes, each rater's order and the confidence intervals",
    )
    raters: list[str] = Field(..., description="The names of the users who read the study's subjects")
    readings_per_rater: int = Field(
        2, ge=1, le=MAX_READINGS_PER_RATER, description="How many times each rater reads each subject"
    )
    min_gap: int | None = Field(
        None,
        ge=0,
        description=(
            "At least this many other readings between a rater's two readings of one subject. None means half "
            "the number of study subjects."
        ),
    )
    show_subject_id: bool = Field(False, description="Whether raters see which subject they are reading")
    bootstrap_samples: int = Field(
        2000,
        ge=MIN_BOOTSTRAP_SAMPLES,
        le=MAX_BOOTSTRAP_SAMPLES,
        description="How many resamples of the subjects each 95% confidence interval is computed from",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _named(cls, name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("the study needs a name")
        if len(name) > 200:
            raise ValueError("the study's name is at most 200 characters long")
        return name

    @field_validator("subject_keys")
    @classmethod
    def _each_subject_once(cls, keys: list[str]) -> list[str]:
        """Keys as the dataset writes them, '001_000012', however they were typed: '1_12' too."""
        normalized = [normalized_subject_key(key) for key in keys]
        twice = sorted({key for key in normalized if normalized.count(key) > 1})
        if twice:
            raise ValueError(f"these subjects are listed twice: {', '.join(twice)}")
        return sorted(normalized)

    @field_validator("random_dataset_ids")
    @classmethod
    def _datasets_once(cls, ids: list[int] | None) -> list[int] | None:
        return sorted(set(ids)) if ids else None

    @field_validator("raters")
    @classmethod
    def _each_rater_once(cls, raters: list[str]) -> list[str]:
        names = [name.strip() for name in raters if name.strip()]
        if not names:
            raise ValueError("the study needs at least one rater")
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValueError(f"these raters are listed twice: {', '.join(twice)}")
        return sorted(names)

    @model_validator(mode="after")
    def _subjects_given(self) -> "StudySettings":
        if self.subject_mode == "list" and not self.subject_keys:
            raise ValueError("list the study's subjects, or pick them at random")
        if self.subject_mode == "random" and self.random_count is None:
            raise ValueError("say how many subjects to pick at random")
        return self


class StudySubject(BaseModel):
    """One subject of the study, and the bones in it that every reading judges."""

    subject_key: str
    dataset_id: int
    subject_id: int
    bones: list[str] = Field(..., description="The labels of the study's copy of the segmentation, in label order")
    segmentation_sha256: str = Field(..., description="Of the study's copy of the segmentation")


class StudyRater(BaseModel):
    """A rater, the code that stands for them in every result, and the readings they are given."""

    name: str
    code: str
    order: list[tuple[str, int]] = Field(
        ..., description="(subject key, reading number of that subject), in the order the rater is given them"
    )


class Study(BaseModel):
    """The study a study server runs. One at a time."""

    study_id: str
    settings: StudySettings
    state: StudyState
    created_at: str
    updated_at: str
    started_at: str | None = None
    ended_at: str | None = None
    min_gap: int | None = Field(None, description="The gap the raters' lists keep, once started")
    subjects: list[StudySubject] = Field(default_factory=list, description="Fixed at Start")
    raters: list[StudyRater] = Field(default_factory=list, description="Fixed at Start")

    model_config = ConfigDict(extra="forbid")

    def rater(self, name: str) -> StudyRater | None:
        return next((rater for rater in self.raters if rater.name == name), None)

    def subject(self, subject_key: str) -> StudySubject | None:
        return next((subject for subject in self.subjects if subject.subject_key == subject_key), None)


class HeldReading(BaseModel):
    """The reading a rater has open: the next one in their list."""

    assignment_id: str
    rater: str
    position: int = Field(..., description="1-based, in the rater's list")
    handed_at: str

    model_config = ConfigDict(extra="forbid")


class Reading(BaseModel):
    """A reading as it was submitted: a verdict on every bone of the subject."""

    assignment_id: str
    rater: str
    code: str
    position: int = Field(..., description="1-based, in the rater's list")
    subject_key: str
    reading: int = Field(..., description="Which reading of this subject by this rater: 1, 2, ...")
    handed_at: str
    submitted_at: str
    verdicts: dict[str, Verdict]
    comment: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReadingInfo(BaseModel):
    """What the review page is told about the reading it shows."""

    code: str = Field(..., description="The rater's code, which stands for them in the results")
    position: int = Field(..., description="Which reading of the rater's list this is, from 1")
    total: int = Field(..., description="How many readings the rater's list holds")
    title: str = Field(..., description="What the page calls the reading: the subject, or 'Reading n'")
    show_subject_id: bool


class StudyHandout(BaseModel):
    """A reading as the review page receives it: the subject as if nobody had judged it yet.

    It has the fields of a quality check's handout that the review page reads, so the page
    works unchanged, and ``study`` besides. There is no history and no earlier verdict, no
    Subject_info entry (its label statuses say what was reviewed already), and, unless the
    study shows it, nothing that names the subject.
    """

    assignment_id: str
    subject_key: str | None = Field(None, description="None unless the study shows the subject id")
    dataset_id: int | None = None
    subject_id: int | None = None
    expires_at: str | None = Field(None, description="Always None: a study reading has no lease time")
    role: Role = REVIEWER
    stage: Literal["review"] = "review"
    data_access: DataAccess
    has_image: bool
    has_segmentation: bool
    segmentation_source: Literal["dataset"] = "dataset"
    segmentation_labels: dict[str, int] = Field(default_factory=dict)
    labels: list[HandoutLabel] = Field(default_factory=list, description="Every bone, none judged")
    requests: list = Field(default_factory=list)
    history: list = Field(default_factory=list)
    segments: list[HandoutSegment] = Field(default_factory=list)
    stored_segmentation_issue: str | None = None
    subject_info: dict = Field(default_factory=dict)
    dataset_info: dict = Field(default_factory=dict, description="Only the modality unless the subject id is shown")
    image_url: str | None = None
    segmentation_url: str | None = None
    study: ReadingInfo


class ReadingResult(BaseModel):
    """What the review page is told after a reading is submitted."""

    assignment_id: str
    accepted_labels: list[str]
    rejected_labels: list[str]
    readings_done: int
    readings_total: int
    message: str
