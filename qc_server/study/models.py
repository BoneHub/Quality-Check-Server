"""What a study server keeps and sends: the study's settings, the study itself, and each reading."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import REVIEWER, DataAccess, HandoutLabel, HandoutSegment, Role
from .schedule import normalized_scan_key

#: ``draft`` while the settings can still change, ``running`` from Start, ``ended`` from End.
StudyState = Literal["draft", "running", "ended"]

#: How the study's scans are chosen: a list the administrator types, or a random pick.
ScanMode = Literal["list", "random"]

#: A rater's verdict on one bone. Rejecting the scan as a whole rejects each of its bones.
Verdict = Literal["accept", "reject"]

ACCEPT: Verdict = "accept"
REJECT: Verdict = "reject"

#: What the statistics count for each bone of a scan rejected as a whole. It is a verdict of
#: its own, not a reject of each bone: a rejected bone can be corrected, a rejected scan is
#: not used at all.
SCAN_REJECTED = "scan_rejected"

#: The verdicts the statistics count, in the order they number them.
VERDICTS: tuple[str, ...] = (ACCEPT, REJECT, SCAN_REJECTED)

#: The most readings of each scan a rater can be given.
MAX_READINGS_PER_RATER = 20

#: Bounds of the number of bootstrap samples behind each confidence interval.
MIN_BOOTSTRAP_SAMPLES = 100
MAX_BOOTSTRAP_SAMPLES = 100_000

#: The largest seed: a browser holds integers exactly only up to here, and the admin panel is one.
MAX_SEED = 2**53 - 1


class StudySettings(BaseModel):
    """Everything the administrator decides before a study starts. Fixed from Start on."""

    name: str = Field(..., description="Names the study in the report")
    scan_mode: ScanMode = Field(..., description="'list': the scans typed in; 'random': a random pick")
    scan_keys: list[str] = Field(
        default_factory=list,
        description="The study's scans, as keys such as 'ds-0027/sub-000001/scan-01' (or '27/1/1'), for 'list'",
    )
    random_count: int | None = Field(None, ge=1, description="How many scans a random pick takes")
    random_dataset_ids: list[int] | None = Field(
        None, description="The datasets a random pick takes them from; None means every dataset"
    )
    seed: int = Field(
        ...,
        ge=0,
        le=MAX_SEED,
        description="Fixes the random pick, the rater codes, each rater's order and the confidence intervals",
    )
    raters: list[str] = Field(..., description="The names of the users who read the study's scans")
    readings_per_rater: int = Field(
        2, ge=1, le=MAX_READINGS_PER_RATER, description="How many times each rater reads each scan"
    )
    min_gap: int | None = Field(
        None,
        ge=0,
        description=(
            "At least this many other readings between a rater's two readings of one scan. None means half "
            "the number of study scans."
        ),
    )
    show_scan_id: bool = Field(False, description="Whether raters see which scan they are reading")
    bootstrap_samples: int = Field(
        2000,
        ge=MIN_BOOTSTRAP_SAMPLES,
        le=MAX_BOOTSTRAP_SAMPLES,
        description="How many resamples of the scans each 95% confidence interval is computed from",
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

    @field_validator("scan_keys")
    @classmethod
    def _each_scan_once(cls, keys: list[str]) -> list[str]:
        """Keys as the database names the scans, 'ds-0027/sub-000012/scan-01', however they were
        typed: '27/12/1' too."""
        normalized = [normalized_scan_key(key) for key in keys]
        twice = sorted({key for key in normalized if normalized.count(key) > 1})
        if twice:
            raise ValueError(f"these scans are listed twice: {', '.join(twice)}")
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
    def _scans_given(self) -> "StudySettings":
        if self.scan_mode == "list" and not self.scan_keys:
            raise ValueError("list the study's scans, or pick them at random")
        if self.scan_mode == "random" and self.random_count is None:
            raise ValueError("say how many scans to pick at random")
        return self


class StudyScan(BaseModel):
    """One scan of the study, and the bones in it that every reading judges."""

    scan_key: str
    dataset_id: int
    subject_id: int
    scan_id: int
    bones: list[str] = Field(..., description="The labels of the study's copy of the segmentation, in label order")
    segmentation_sha256: str = Field(..., description="Of the study's copy of the segmentation")


class StudyRater(BaseModel):
    """A rater, the code that stands for them in every result, and the readings they are given."""

    name: str
    code: str
    order: list[tuple[str, int]] = Field(
        ..., description="(scan key, reading number of that scan), in the order the rater is given them"
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
    scans: list[StudyScan] = Field(default_factory=list, description="Fixed at Start")
    raters: list[StudyRater] = Field(default_factory=list, description="Fixed at Start")

    model_config = ConfigDict(extra="forbid")

    def rater(self, name: str) -> StudyRater | None:
        return next((rater for rater in self.raters if rater.name == name), None)

    def scan(self, scan_key: str) -> StudyScan | None:
        return next((scan for scan in self.scans if scan.scan_key == scan_key), None)


class HeldReading(BaseModel):
    """The reading a rater has open: the next one in their list."""

    assignment_id: str
    rater: str
    position: int = Field(..., description="1-based, in the rater's list")
    handed_at: str

    model_config = ConfigDict(extra="forbid")


class Reading(BaseModel):
    """A reading as it was submitted: a verdict on every bone of the scan. Rejecting the
    scan as a whole rejects every bone, which the statistics count as ``scan_rejected``."""

    assignment_id: str
    rater: str
    code: str
    position: int = Field(..., description="1-based, in the rater's list")
    scan_key: str
    reading: int = Field(..., description="Which reading of this scan by this rater: 1, 2, ...")
    handed_at: str
    submitted_at: str
    verdicts: dict[str, Verdict]
    scan_rejected: bool = Field(False, description="The scan was rejected as a whole")
    comment: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReadingInfo(BaseModel):
    """What the review page is told about the reading it shows."""

    code: str = Field(..., description="The rater's code, which stands for them in the results")
    position: int = Field(..., description="Which reading of the rater's list this is, from 1")
    total: int = Field(..., description="How many readings the rater's list holds")
    title: str = Field(..., description="What the page calls the reading: the scan, or 'Reading n'")
    show_scan_id: bool


class StudyHandout(BaseModel):
    """A reading as the review page receives it: the scan as if nobody had judged it yet.

    It has the fields of a quality check's handout that the review page reads, so the page
    works unchanged, and ``study`` besides. There is no history and no earlier verdict, nothing
    of scan.json but the modality (the origins of its labels say which were corrected), and,
    unless the study shows it, nothing that names the scan.
    """

    assignment_id: str
    scan_key: str | None = Field(None, description="None unless the study shows the scan id")
    dataset_id: int | None = None
    subject_id: int | None = None
    scan_id: int | None = None
    expires_at: str | None = Field(None, description="Always None: a study reading has no lease time")
    role: Role = REVIEWER
    stage: Literal["review"] = "review"
    data_access: DataAccess
    has_image: bool
    has_segmentation: bool
    segmentation_source: Literal["dataset"] = "dataset"
    segmentation_labels: dict[str, str] = Field(default_factory=dict)
    labels: list[HandoutLabel] = Field(default_factory=list, description="Every bone, none judged")
    requests: list = Field(default_factory=list)
    history: list = Field(default_factory=list)
    segments: list[HandoutSegment] = Field(default_factory=list)
    stored_segmentation_issue: str | None = None
    scan_info: dict = Field(default_factory=dict, description="Only the modality")
    subject_info: dict = Field(default_factory=dict)
    dataset_info: dict = Field(default_factory=dict, description="Empty unless the study shows the scan id")
    image_url: str | None = None
    segmentation_url: str | None = None
    study: ReadingInfo


class ReadingResult(BaseModel):
    """What the review page is told after a reading is submitted."""

    assignment_id: str
    accepted_labels: list[str]
    rejected_labels: list[str]
    scan_rejected: bool = False
    readings_done: int
    readings_total: int
    message: str
