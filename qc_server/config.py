"""Server configuration, and where the server keeps things.

Two places, on purpose:

* the credentials folder, inside the container (``BONEHUB_QC_CREDENTIALS_DIR``): the
  server's id, private key, admin key and user accounts. Never on the dataset share.
* ``<dataset_root>/<state_dir_name>/<server_id>/`` on the share: this server's policy
  (``config.json``), assignments, the verdicts and corrected segmentations waiting for the
  administrator's approval, logs and backups. One folder per server, so servers with
  different admins working on one dataset do not overwrite each other.

Any config field can be overridden at startup through an environment variable named
``BONEHUB_QC_<FIELD>``, which is how the Docker image is configured.

A server in study mode (``BONEHUB_QC_MODE=study``, see :mod:`qc_server.study`) writes nothing
into the dataset: what it keeps besides its credentials goes into the credentials folder too,
under ``state/<server_id>/``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bonehub_format import SCHEMA_VERSION

from . import __version__ as SERVER_VERSION

ENV_PREFIX = "BONEHUB_QC_"
DEFAULT_STATE_DIR_NAME = ".bonehub_qc"
#: Inside the container; the Docker image and docker-compose.yml mount a volume here.
DEFAULT_CREDENTIALS_DIR = "/var/lib/bonehub-qc"
CONFIG_FILE_NAME = "config.json"
#: Settings an older server saved in ``config.json`` that no longer exist. They are dropped on
#: loading, so a server updated in place still starts.
RETIRED_FIELDS = ("max_concurrent_assignments_per_user",)

#: ``BONEHUB_QC_*`` variables of servers written for BoneHub data schema 0.3, with what took
#: their place. One still set stops the server, rather than being silently ignored.
RETIRED_ENV = {
    "ELIGIBLE_LABEL_VALUES": (
        "the label statuses are gone from the BoneHub format. Use ELIGIBLE_REVIEW_STATES instead: "
        "not_reviewed for status 1, reviewed for status 2"
    ),
    "INCLUDE_SUBJECTS_WITHOUT_SEGMENTATION": "it is now INCLUDE_SCANS_WITHOUT_SEGMENTATION",
    "MARK_REMOVED_LABELS_ABSENT": (
        "a label no longer in an approved segmentation is now always taken out of scan.json, which must "
        "list exactly the labels of the segmentation"
    ),
}

#: What the server is for, from ``BONEHUB_QC_MODE``: the quality check of the dataset, or a
#: reliability study of the reviewers, which runs on a server of its own.
QC_MODE = "qc"
STUDY_MODE = "study"
MODES = (QC_MODE, STUDY_MODE)

#: In study mode, the folder of the credentials folder that holds one sub-folder per server, in
#: place of ``<dataset_root>/.bonehub_qc``: a study server leaves the dataset alone.
STUDY_STATE_DIR_NAME = "state"

#: The major version of the BoneHub format whose layout, scan.json and segmentation format this
#: server implements: the server's own major version, so that server 1.x reads format 1.x. The
#: format is installed from its repository's main branch; a minor version only adds what older
#: datasets still satisfy, but a new major version must stop the server rather than have its
#: datasets read with this version's meaning -- until a server of that major version is released.
WRITTEN_FOR_FORMAT = SERVER_VERSION.split(".")[0]
if SCHEMA_VERSION.split(".")[0] != WRITTEN_FOR_FORMAT:
    raise ImportError(
        f"bonehub_format {SCHEMA_VERSION} is installed, but this server, version {SERVER_VERSION}, is written "
        f"for version {WRITTEN_FOR_FORMAT}.x of the BoneHub format, whose layout and scan.json may differ. "
        f"Install bonehub_format {WRITTEN_FOR_FORMAT}.x, or a server of version {SCHEMA_VERSION.split('.')[0]}.x."
    )

#: Whether a label has been reviewed, as the queue sees it. The BoneHub format does not record
#: reviews: a label is reviewed when a quality-check server on the database approved a reviewer's
#: (or a vouching editor's) acceptance of it, and the scan's segmentation is still the one approved.
NOT_REVIEWED = "not_reviewed"
REVIEWED = "reviewed"
REVIEW_STATES: dict[str, str] = {
    NOT_REVIEWED: "no server on this database has reviewed it",
    REVIEWED: "a server on this database has reviewed it, and its segmentation is unchanged since",
}

#: The kinds of origin a label has in scan.json (see ``bonehub_format.check_origin``), and what
#: each means.
ORIGIN_KINDS: dict[str, str] = {
    "source": "came with the source dataset",
    "manual": "drawn by hand for this dataset",
    "model": "predicted by a model (model:<name>@<version>)",
    "derived": "computed from another file of the same scan (derived:<tool>@<version>)",
}

#: The origin an approval gives a label an editor drew or changed.
EDITED_ORIGIN = "manual"

#: An entry of ``eligible_origins``: a kind of origin, a model or tool by name, or a full origin.
_ORIGIN_PATTERN = re.compile(r"source|manual|(model|derived)(:[^\s@:]+(@[^\s@]+)?)?")


def origin_matches(origin: str | None, patterns: list[str] | None) -> bool:
    """Whether a label's origin is one of ``patterns`` (None: any origin). ``model`` matches every
    ``model:<name>@<version>``, ``model:TotalSegmentator`` every version of that model, and a
    full origin itself. A label scan.json does not list has no origin, and matches only None."""
    if patterns is None:
        return True
    if origin is None:
        return False
    return any(origin == p or origin.startswith(f"{p}:") or origin.startswith(f"{p}@") for p in patterns)


class QCServerConfig(BaseModel):
    """Policy that decides which scans are handed out and what a submission does."""

    # --- queue policy -------------------------------------------------------
    eligible_review_states: list[Literal["not_reviewed", "reviewed"]] = Field(
        default_factory=lambda: [NOT_REVIEWED],
        description=(
            "A scan is queued for quality check when at least one label of its segmentation is in one of "
            "these review states, and the review takes those labels. 'not_reviewed' (the default): no server "
            "on this database has reviewed it. 'reviewed': a server has, and the segmentation is unchanged "
            "since -- a second review."
        ),
    )
    eligible_origins: list[str] | None = Field(
        None,
        description=(
            "Queue only the labels with one of these origins in scan.json: 'source', 'manual', 'model', "
            "'derived', a model or tool by name such as 'model:TotalSegmentator', or a full origin. None "
            "means every origin."
        ),
    )
    include_scans_without_segmentation: bool = Field(
        False,
        description=(
            "Also queue scans that have an image but no segmentation at all, so editors can create one "
            "in 3D Slicer. Reviewers are not handed them."
        ),
    )
    allowed_dataset_ids: list[int] | None = Field(
        None,
        description="Restrict the whole server to these dataset ids. None means every dataset under the root.",
    )

    # --- assignment policy --------------------------------------------------
    assignment_strategy: Literal["sequential", "random"] = Field(
        "sequential",
        description="'sequential' hands out the lowest free scan, 'random' picks a free scan at random.",
    )
    lease_ttl_seconds: int = Field(
        24 * 3600,
        ge=60,
        description="How long a user keeps a scan before it returns to the queue.",
    )
    index_refresh_seconds: int = Field(
        300,
        ge=0,
        description="How often the scan index is rebuilt from disk. 0 rebuilds on every request.",
    )

    # --- submission policy --------------------------------------------------
    edits_need_review: bool = Field(
        True,
        description=(
            "Send the labels an editor corrected back to the reviewers. Off, the labels the editor vouches for "
            "are accepted as they are and wait for the administrator's approval."
        ),
    )
    require_geometry_match: bool = Field(
        True,
        description="Reject a submitted segmentation whose voxel grid does not match the scan's image.",
    )
    keep_segmentation_backups: bool = Field(
        True,
        description="Copy the scan's segmentation into this server's folder before an approval overwrites it.",
    )
    max_upload_bytes: int = Field(
        512 * 1024 * 1024,
        ge=1,
        description="Largest segmentation upload accepted from a client.",
    )

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("eligible_review_states")
    @classmethod
    def check_eligible_review_states(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError(
                "eligible_review_states must not be empty: "
                + ", ".join(f"'{state}' ({meaning})" for state, meaning in REVIEW_STATES.items())
                + "."
            )
        return [state for state in REVIEW_STATES if state in values]

    @field_validator("eligible_origins")
    @classmethod
    def check_eligible_origins(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        values = sorted({value.strip() for value in values if value.strip()})
        if not values:
            raise ValueError("eligible_origins must not be empty; leave it out (None) to queue every origin.")
        for value in values:
            if not _ORIGIN_PATTERN.fullmatch(value):
                raise ValueError(
                    f"'{value}' is not an origin. Use "
                    + ", ".join(f"'{kind}'" for kind in ORIGIN_KINDS)
                    + ", a model or tool by name such as 'model:TotalSegmentator', or a full origin such as "
                    "'model:TotalSegmentator@2.4.0'."
                )
        return values

    # --- persistence --------------------------------------------------------
    @classmethod
    def load(cls, config_path: Path) -> "QCServerConfig":
        """Read the config file if it exists, then apply ``BONEHUB_QC_*`` overrides."""
        data: dict = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        for name in RETIRED_FIELDS:
            data.pop(name, None)
        data.update(cls._env_overrides())
        return cls(**data)

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4)
        os.replace(tmp_path, config_path)

    @classmethod
    def _env_overrides(cls) -> dict:
        """Collect ``BONEHUB_QC_<FIELD>`` variables that match a config field. A variable of an
        earlier version that is still set stops the server, saying what took its place."""
        retired = [
            f"{ENV_PREFIX}{name}: {replacement}."
            for name, replacement in RETIRED_ENV.items()
            if (os.environ.get(f"{ENV_PREFIX}{name}") or "").strip()
        ]
        if retired:
            raise ValueError(
                "These settings are set in the environment (.env), but no longer exist. Remove them:\n  "
                + "\n  ".join(retired)
            )
        overrides: dict = {}
        for name, field in cls.model_fields.items():
            raw = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
            if raw is None or raw == "":
                continue
            overrides[name] = _parse_env_value(raw, field.annotation)
        return overrides


def _parse_env_value(raw: str, annotation) -> object:
    """Turn an environment string into the type the config field expects."""
    text = raw.strip()
    annotation_text = str(annotation)
    if "bool" in annotation_text:
        return text.lower() in {"1", "true", "yes", "on"}
    if "list" in annotation_text:
        if text.lower() in {"none", "null"}:
            return None
        items = [item.strip() for item in text.split(",") if item.strip()]
        return [int(item) for item in items] if "int" in annotation_text else items
    if "int" in annotation_text:
        return int(text)
    return text


def resolve_dataset_root() -> Path:
    """Dataset root the server operates on, from ``BONEHUB_QC_DATASET_ROOT``."""
    raw = os.environ.get(f"{ENV_PREFIX}DATASET_ROOT")
    if not raw:
        raise RuntimeError(
            f"{ENV_PREFIX}DATASET_ROOT is not set. Point it at a database in the BoneHub format."
        )
    return Path(raw)


def resolve_state_root(dataset_root: Path) -> Path:
    """Folder inside the dataset root that holds one sub-folder per server."""
    name = os.environ.get(f"{ENV_PREFIX}STATE_DIR_NAME", DEFAULT_STATE_DIR_NAME)
    return dataset_root / name


def resolve_credentials_dir() -> Path:
    """Folder inside the container for the server's credentials, from ``BONEHUB_QC_CREDENTIALS_DIR``."""
    return Path(os.environ.get(f"{ENV_PREFIX}CREDENTIALS_DIR") or DEFAULT_CREDENTIALS_DIR)


def resolve_mode() -> str:
    """What the server is for, from ``BONEHUB_QC_MODE``: 'qc' (the default) or 'study'."""
    mode = (os.environ.get(f"{ENV_PREFIX}MODE") or QC_MODE).strip().lower()
    if mode not in MODES:
        raise RuntimeError(
            f"{ENV_PREFIX}MODE is '{mode}'. Leave it blank for the quality check, or set it to '{STUDY_MODE}' "
            "for a reliability study of the reviewers."
        )
    return mode


def resolve_server_state_root(dataset_root: Path, credentials_dir: Path) -> Path:
    """The folder that holds one sub-folder per server: in the dataset for the quality check, in
    the credentials folder for a study server, which writes nothing into the dataset."""
    if resolve_mode() == STUDY_MODE:
        return Path(credentials_dir) / STUDY_STATE_DIR_NAME
    return resolve_state_root(dataset_root)
