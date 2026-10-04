"""The study server's client API: the review page's endpoints, for study readings.

The paths are the quality check's (see :mod:`qc_server.api`), so the review page works on a
study server as it does on a quality-check server:

1. ``GET  /api/v1/ping``                          - the key, and the rater's code and progress
2. ``POST /api/v1/subjects/next``                 - the rater's next reading
3. ``GET  /api/v1/assignments/{id}/image``        - its image, from the dataset
4. ``GET  /api/v1/assignments/{id}/segmentation`` - its segmentation: the study's copy
5. ``POST /api/v1/assignments/{id}/submit``       - the verdict, as in the quality check

Only the review page, in the reviewer role, works with a study server: there is nothing for
3D Slicer to do here. A submission is the review page's ``metadata`` part, read as a
:class:`~qc_server.models.SubmissionRequest`, as a reviewer's verdict in the quality check:
every bone accepted or rejected, or the subject rejected as a whole. Reporting a bone missing
and uploading a segmentation are refused.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse

from bonehub_data_schema import SEGMENTATION_SUFFIX, VALID_LABEL_VALUES, __version__ as SCHEMA_VERSION

from .. import __version__
from ..api import ROLE_HEADER, _read_in_chunks
from ..config import STUDY_MODE
from ..models import REVIEWER, HandoutLabel, HandoutSegment, SubmissionRequest, User
from ..segmentation import SegmentationError, read_segment_table
from ..store import LABEL_NAME_TO_VALUE, QCError, QCStore
from .models import HeldReading, ReadingInfo, ReadingResult, StudyHandout
from .store import StudyStore

router = APIRouter(prefix="/api/v1", tags=["study client"])


def get_store(request: Request) -> QCStore:
    store: QCStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise QCError("The server is not initialised.", status_code=503)
    return store


def get_study(request: Request) -> StudyStore:
    study: StudyStore | None = getattr(request.app.state, "study", None)
    if study is None:
        raise QCError("The server is not initialised.", status_code=503)
    return study


def get_rater(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_client_role: str | None = Header(None, alias=ROLE_HEADER),
) -> User:
    """The user behind the key, working on the review page. The key is checked first."""
    user = get_store(request).authenticate(x_api_key)
    if x_client_role != REVIEWER:
        raise QCError(
            "This server runs a reliability study of the reviewers, on the review page; there is nothing to correct "
            f"in 3D Slicer. Open {request.base_url}review in a browser instead.",
            status_code=403 if x_client_role else 400,
        )
    if not user.is_reviewer:
        raise QCError(
            f"'{user.name}' is not a reviewer, so cannot sign in to the review page. Ask the administrator.",
            status_code=403,
        )
    return user


@router.get("/ping")
def ping(request: Request, user: User = Depends(get_rater)) -> dict:
    """Confirm the key works, and tell the page about the study and the rater's place in it."""
    return {
        "status": "ok",
        "server": "qc-server",
        "mode": STUDY_MODE,
        "server_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "user": user.name,
        "role": REVIEWER,
        "roles": user.roles,
        "allowed_dataset_ids": user.allowed_dataset_ids,
        "data_access": user.data_access,
        "edits_need_review": True,
        "mark_removed_labels_absent": False,
        "lease_ttl_seconds": None,
        "study": get_study(request).rater_status(user),
    }


@router.get("/labels")
def labels(user: User = Depends(get_rater)) -> dict:
    """The BoneHub label map and label statuses, as on a quality-check server."""
    return {
        "schema_version": SCHEMA_VERSION,
        "label_name_to_value": LABEL_NAME_TO_VALUE,
        "label_status_values": {str(k): v for k, v in VALID_LABEL_VALUES.items()},
        "segmentation_suffix": SEGMENTATION_SUFFIX,
    }


@router.post("/subjects/next", response_model=StudyHandout)
def next_reading(request: Request, user: User = Depends(get_rater)) -> StudyHandout:
    """The rater's next reading. A rater holds one at a time: asking again before submitting
    it gives the same one back."""
    study = get_study(request)
    return _handout(get_store(request), study, study.next_reading(user), user)


@router.get("/assignments")
def my_readings(request: Request, user: User = Depends(get_rater)) -> list[dict]:
    """The reading this rater has open, if any, so that the page can pick it up again."""
    return [
        {"assignment_id": held.assignment_id, "assigned_at": held.handed_at, "position": held.position}
        for held in get_study(request).held_by(user)
    ]


@router.get("/assignments/{assignment_id}", response_model=StudyHandout)
def reading_detail(assignment_id: str, request: Request, user: User = Depends(get_rater)) -> StudyHandout:
    study = get_study(request)
    return _handout(get_store(request), study, study.held_reading(assignment_id, user), user)


@router.get("/assignments/{assignment_id}/image")
def download_image(assignment_id: str, request: Request, user: User = Depends(get_rater)) -> StreamingResponse:
    store, study = get_store(request), get_study(request)
    the_study, _, subject = study.reading_context(study.held_reading(assignment_id, user))
    path = store.image_path(subject.dataset_id, subject.subject_id)
    if not path.exists():
        raise QCError("The image of this reading is missing on the server. Tell the administrator.", status_code=404)
    name = path.name if the_study.settings.show_subject_id else "reading.nii.gz"
    return _send(path, "application/gzip", name)


@router.get("/assignments/{assignment_id}/segmentation")
def download_segmentation(assignment_id: str, request: Request, user: User = Depends(get_rater)) -> StreamingResponse:
    """The study's copy of the subject's segmentation, the same file at every reading."""
    study = get_study(request)
    the_study, _, subject = study.reading_context(study.held_reading(assignment_id, user))
    path = study.copy_path(subject.subject_key)
    if not path.exists():
        raise QCError("The segmentation of this reading is missing on the server. Tell the administrator.", status_code=404)
    name = path.name if the_study.settings.show_subject_id else f"reading{SEGMENTATION_SUFFIX}"
    return _send(path, "application/octet-stream", name)


@router.post("/assignments/{assignment_id}/extend")
def extend(assignment_id: str, request: Request, user: User = Depends(get_rater)) -> dict:
    """Nothing to extend: a study reading stays with its rater until they submit it."""
    held = get_study(request).held_reading(assignment_id, user)
    return {"assignment_id": held.assignment_id, "expires_at": None}


@router.post("/assignments/{assignment_id}/release")
def release(assignment_id: str, request: Request, user: User = Depends(get_rater)) -> dict:
    """Hand the reading back. It stays the rater's next one: readings cannot be skipped."""
    held = get_study(request).release(assignment_id, user)
    return {"assignment_id": held.assignment_id, "state": "released", "position": held.position}


@router.post("/assignments/{assignment_id}/submit", response_model=ReadingResult)
def submit(
    assignment_id: str,
    request: Request,
    metadata: str = Form(..., description="JSON body matching SubmissionRequest"),
    segmentation: UploadFile | None = File(None, description="Refused: a study reading uploads nothing"),
    user: User = Depends(get_rater),
) -> ReadingResult:
    """Record the reading: every bone of the segmentation accepted (``confirmed_labels``) or
    rejected (``rejected_labels``); or, with ``quality_check_confirmed=false``, the subject
    rejected as a whole."""
    try:
        payload = SubmissionRequest(**json.loads(metadata))
    except json.JSONDecodeError as exc:
        raise QCError(f"The 'metadata' part is not valid JSON: {exc}") from exc
    except Exception as exc:
        raise QCError(f"Invalid submission metadata: {exc}") from exc
    if segmentation is not None:
        raise QCError("A study reading uploads nothing: give each bone a verdict, accept or reject.")
    if payload.missing_labels:
        raise QCError("The study takes no reports of missing bones: accept or reject each bone of the segmentation.")
    if payload.quality_check_confirmed and not payload.use_stored_segmentation:
        raise QCError("A study reading judges the segmentation as it is: set use_stored_segmentation.")
    return get_study(request).submit(
        assignment_id,
        user,
        payload.confirmed_labels,
        payload.rejected_labels,
        payload.comment,
        subject_rejected=not payload.quality_check_confirmed,
    )


# --------------------------------------------------------------------- helpers
def _handout(store: QCStore, study: StudyStore, held: HeldReading, user: User) -> StudyHandout:
    """A reading as the review page shows it: every bone, none judged, and nothing that says
    what anyone made of it before."""
    the_study, rater, subject = study.reading_context(held)
    show = the_study.settings.show_subject_id
    copy = study.copy_path(subject.subject_key)
    try:
        segments = read_segment_table(copy)
    except (SegmentationError, OSError):
        segments = []
    info = store.dataset_info(subject.dataset_id)
    base = f"/api/v1/assignments/{held.assignment_id}"
    has_image = store.image_path(subject.dataset_id, subject.subject_id).exists()
    return StudyHandout(
        assignment_id=held.assignment_id,
        subject_key=subject.subject_key if show else None,
        dataset_id=subject.dataset_id if show else None,
        subject_id=subject.subject_id if show else None,
        data_access=user.data_access,
        has_image=has_image,
        has_segmentation=copy.exists(),
        labels=[
            HandoutLabel(name=bone, value=LABEL_NAME_TO_VALUE.get(bone), state="pending", painted=True)
            for bone in subject.bones
        ],
        segments=[
            HandoutSegment(
                number=segment.number,
                label=segment.label,
                value=segment.value,
                color=list(segment.color),
                extent=list(segment.extent) if segment.extent else None,
            )
            for segment in segments
        ],
        dataset_info=info if show else {key: info[key] for key in ("modality",) if key in info},
        image_url=f"{base}/image" if has_image else None,
        segmentation_url=f"{base}/segmentation",
        study=ReadingInfo(
            code=rater.code,
            position=held.position,
            total=len(rater.order),
            title=subject.subject_key if show else f"Reading {held.position}",
            show_subject_id=show,
        ),
    )


def _send(path, media_type: str, filename: str) -> StreamingResponse:
    """A file, read the way the quality check sends files from the share (see
    :func:`qc_server.api._send_from_share`), under the name given."""
    return StreamingResponse(
        _read_in_chunks(path),
        media_type=media_type,
        headers={
            "Content-Length": str(path.stat().st_size),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
