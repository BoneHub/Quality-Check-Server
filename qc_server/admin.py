"""Admin panel: users and their keys, the queue, and the approval of subjects into the dataset.

Reviewers' and editors' verdicts wait in the server's state folder. The administrator
approves a subject whose labels are all accepted, which writes it into the dataset, or sends
it back to the reviewers or the editors, or closes it without writing its labels -- several
subjects at once, each time with a remark for their Subject_info if they like, such as why a
reviewer rejected one as a whole.

Authentication is the server's admin key, sent as an ``X-Admin-Key`` header. The panel at
``/admin`` is a single static page that asks for the key once and keeps it in the
browser's session storage, so the server stores no sessions of its own.

Two routers. :data:`panel_router` is what a study server's panel has too (see
:mod:`qc_server.study`): the page itself, the user accounts and the recent activity.
:data:`router` is the quality check's own: the queue, the subjects' cases and the policy.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse

from .config import QC_MODE
from .models import DEFAULT_DATA_ACCESS, DEFAULT_ROLES, User
from .store import UNSET, QCError, QCStore

panel_router = APIRouter(prefix="/admin", tags=["admin"])
router = APIRouter(prefix="/admin", tags=["admin"])

STATIC_DIR = Path(__file__).parent / "static"


def get_store(request: Request) -> QCStore:
    store: QCStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise QCError("The server is not initialised.", status_code=503)
    return store


def require_admin(request: Request, x_admin_key: str | None = Header(None, alias="X-Admin-Key")) -> QCStore:
    store = get_store(request)
    if not store.is_admin_key(x_admin_key):
        raise QCError("Invalid or missing admin key. Send it in the 'X-Admin-Key' header.", status_code=401)
    return store


@panel_router.get("", include_in_schema=False)
@panel_router.get("/", include_in_schema=False)
def admin_panel() -> FileResponse:
    """The page itself is public; every action on it needs the admin key."""
    return FileResponse(STATIC_DIR / "admin.html", media_type="text/html")


@router.get("/api/session")
def check_session(store: QCStore = Depends(require_admin)) -> dict:
    """Used by the panel to validate the key the administrator typed in. ``mode`` tells the
    panel which sections to show."""
    return {
        "status": "ok",
        "mode": QC_MODE,
        "server_id": store.server_id,
        "dataset_root": str(store.dataset_root),
        "state_dir": str(store.state_dir),
        "config": store.config.model_dump(),
        "sessions": store.sessions(),
    }


@panel_router.get("/api/users")
def list_users(store: QCStore = Depends(require_admin)) -> list[dict]:
    return store.list_users()


@panel_router.post("/api/users")
def create_user(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Create a user, with their API key in the response. ``roles`` defaults to both, reviewer and editor."""
    name = str(payload.get("name", "")).strip()
    allowed = _parse_dataset_ids(payload.get("allowed_dataset_ids"))
    note = str(payload.get("note", "") or "")
    data_access = str(payload.get("data_access") or DEFAULT_DATA_ACCESS)
    roles = DEFAULT_ROLES if payload.get("roles") is None else _parse_roles(payload["roles"])
    user, api_key = store.create_user(
        name=name, allowed_dataset_ids=allowed, note=note, data_access=data_access, roles=roles
    )
    return {
        "user": user.public_dict(),
        "api_key": api_key,
        "warning": "Send this key to the user privately.",
    }


@panel_router.patch("/api/users/{name}")
def update_user(name: str, payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Change a user. A field absent from the body is left exactly as it was."""
    allowed = _parse_dataset_ids(payload["allowed_dataset_ids"]) if "allowed_dataset_ids" in payload else UNSET
    note = payload.get("note")
    data_access = payload.get("data_access")
    roles = payload.get("roles")
    user = store.update_user(
        name,
        allowed,
        None if note is None else str(note),
        data_access=None if data_access is None else str(data_access),
        roles=None if roles is None else _parse_roles(roles),
    )
    return user.public_dict()


@panel_router.post("/api/users/{name}/rotate-key")
def rotate_key(name: str, store: QCStore = Depends(require_admin)) -> dict:
    api_key = store.rotate_user_key(name)
    return {
        "name": name,
        "api_key": api_key,
        "warning": "The previous key stopped working.",
    }


@panel_router.get("/api/users/{name}/key")
def show_key(name: str, store: QCStore = Depends(require_admin)) -> dict:
    """A user's current API key, shown again."""
    return {"name": name, "api_key": store.user_key(name)}


@panel_router.post("/api/users/{name}/active")
def set_active(name: str, payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    user: User = store.set_user_active(name, bool(payload.get("active", True)))
    return user.public_dict()


@panel_router.delete("/api/users/{name}")
def delete_user(name: str, store: QCStore = Depends(require_admin)) -> dict:
    store.delete_user(name)
    return {"status": "deleted", "name": name}


@router.get("/api/stats")
def stats(store: QCStore = Depends(require_admin)) -> dict:
    return store.stats().model_dump()


@router.get("/api/assignments")
def assignments(limit: int = 200, state: str | None = None, store: QCStore = Depends(require_admin)) -> list[dict]:
    states = [s.strip() for s in state.split(",")] if state else None
    return store.all_assignments(limit=limit, states=states)


@router.post("/api/assignments/{assignment_id}/release")
def release(assignment_id: str, store: QCStore = Depends(require_admin)) -> dict:
    """Take a subject back from a user who is not going to finish it."""
    return store.release_assignment(assignment_id).model_dump()


@panel_router.get("/api/submissions")
def submissions(limit: int = 100, kind: str | None = None, store: QCStore = Depends(require_admin)) -> list[dict]:
    return store.audit.read_recent(limit=limit, kind=kind)


@router.get("/api/cases")
def cases(
    stage: str | None = None, limit: int = 500, comment: str | None = None, store: QCStore = Depends(require_admin)
) -> list[dict]:
    """The subjects with a verdict on this server, most recently changed first. ``stage`` takes
    one stage or several, comma-separated: review, edit, approval, escalated, rejected, applied, closed.
    ``comment`` keeps the subjects with a comment that contains it, ignoring case, each with the
    steps whose comment does in ``matches``."""
    stages = [s.strip() for s in stage.split(",") if s.strip()] if stage else None
    return store.cases(stages=stages, limit=limit, comment=comment)


@router.post("/api/cases/approve")
def approve_all(payload: dict | None = None, store: QCStore = Depends(require_admin)) -> dict:
    """Approve the subjects named in ``subject_keys``, or every subject waiting for approval.
    One that cannot be approved is reported, and the others go ahead.

    ``remark`` is added to each subject's remarks in Subject_info, as ``QC: <remark>``.
    ``allow_unaccepted`` approves subjects that do not wait for approval too: their labels
    nobody accepted keep their status. ``revisions`` maps a subject key to the revision the
    subject was seen at; one that changed since is not approved.
    """
    payload = payload or {}
    keys = payload.get("subject_keys")
    results = store.approve_all(
        _parse_keys(keys) if keys is not None else None,
        remark=payload.get("remark"),
        allow_unaccepted=_parse_flag(payload, "allow_unaccepted"),
        revisions=_parse_revisions(payload.get("revisions")),
    )
    return {"approved": sum(1 for r in results if r["approved"]), "results": results}


@router.post("/api/cases/return")
def return_all(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Send the subjects named in ``subject_keys`` back, ``to`` "review" or "edit", with the
    ``comment`` for whoever gets them. One that cannot be sent back is reported, and the others
    go ahead. ``remark`` and ``revisions`` are as for approving several at once."""
    results = store.return_all(
        _parse_keys(payload.get("subject_keys")),
        str(payload.get("to", "")),
        payload.get("comment"),
        remark=payload.get("remark"),
        revisions=_parse_revisions(payload.get("revisions")),
    )
    return {"returned": sum(1 for r in results if r["returned"]), "results": results}


@router.post("/api/cases/close")
def close_all(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Close the subjects named in ``subject_keys``. One that cannot be closed is reported, and
    the others go ahead. ``remark`` and ``revisions`` are as for approving several at once."""
    results = store.close_all(
        _parse_keys(payload.get("subject_keys")),
        remark=payload.get("remark"),
        revisions=_parse_revisions(payload.get("revisions")),
    )
    return {"closed": sum(1 for r in results if r["closed"]), "results": results}


@router.get("/api/cases/{subject_key}")
def case(subject_key: str, store: QCStore = Depends(require_admin)) -> dict:
    found = store.case_of(subject_key)
    if found is None:
        raise QCError(f"Nobody has given a verdict on subject {subject_key} on this server.", status_code=404)
    return found.model_dump()


@router.get("/api/cases/{subject_key}/segmentation")
def case_segmentation(subject_key: str, store: QCStore = Depends(require_admin)) -> FileResponse:
    """The segmentation a subject's verdicts are about, to look at in 3D Slicer before approving:
    its editor's correction, or the dataset's own."""
    path = store.case_segmentation_path(subject_key)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/api/cases/{subject_key}/approve")
def approve(subject_key: str, payload: dict | None = None, store: QCStore = Depends(require_admin)) -> dict:
    """Write the subject into the dataset: its accepted labels become reviewed (2), and an
    editor's correction replaces the dataset's segmentation. ``remark``, ``allow_unaccepted``
    and ``revision`` are as for approving several at once."""
    payload = payload or {}
    outcome = store.approve(
        subject_key,
        remark=payload.get("remark"),
        allow_unaccepted=_parse_flag(payload, "allow_unaccepted"),
        revision=_parse_revision(payload.get("revision")),
    )
    return {
        "case": outcome.case.model_dump(),
        "updated_labels": outcome.updated_labels,
        "segmentation_written": outcome.segmentation_written,
        "backup_path": outcome.backup_path,
        "remark": outcome.remark,
        "remark_added": outcome.remark_added,
        "message": outcome.message,
    }


@router.post("/api/cases/{subject_key}/return")
def return_case(subject_key: str, payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Send a subject back: ``{"to": "review"}`` has every verdict reviewed again, ``{"to": "edit"}``
    hands it to the editors with the ``comment``. Reopens a closed subject. ``remark`` is added
    to its remarks in Subject_info, as ``QC: <remark>``; ``revision`` is as for approving it."""
    return store.return_case(
        subject_key,
        str(payload.get("to", "")),
        payload.get("comment"),
        remark=payload.get("remark"),
        revision=_parse_revision(payload.get("revision")),
    ).model_dump()


@router.post("/api/cases/{subject_key}/close")
def close_case(subject_key: str, payload: dict | None = None, store: QCStore = Depends(require_admin)) -> dict:
    """Finish a subject's quality check without writing its labels or segmentation into the
    dataset. ``remark`` is added to its remarks in Subject_info, as ``QC: <remark>``: how a
    subject a reviewer rejected is recorded. ``revision`` is as for approving it."""
    payload = payload or {}
    return store.close_case(
        subject_key,
        payload.get("comment"),
        remark=payload.get("remark"),
        revision=_parse_revision(payload.get("revision")),
    ).model_dump()


@router.post("/api/refresh-index")
def refresh_index(store: QCStore = Depends(require_admin)) -> dict:
    """Re-scan the dataset folder, for when subjects were added outside the server."""
    store.refresh_index()
    return store.stats().model_dump()


@router.get("/api/config")
def get_config(store: QCStore = Depends(require_admin)) -> dict:
    return store.config.model_dump()


@router.put("/api/config")
def put_config(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Update the queue policy and rebuild the index so the change takes effect at once."""
    current = store.config.model_dump()
    unknown = [key for key in payload if key not in current]
    if unknown:
        raise QCError(f"Unknown configuration fields: {unknown}.")
    current.update(payload)
    try:
        store.config = type(store.config)(**current)
    except Exception as exc:
        raise QCError(f"Invalid configuration: {exc}") from exc
    store.config.save(store.config_path)
    store.refresh_index()
    store.audit.event(f"Configuration updated: {sorted(payload)}")
    return store.config.model_dump()


def _parse_dataset_ids(raw) -> list[int] | None:
    """Accept a list, a comma-separated string, or nothing at all."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise QCError("allowed_dataset_ids must be a list of dataset ids or a comma-separated string.")
    if not items:
        return None
    try:
        return sorted({int(item) for item in items})
    except (TypeError, ValueError) as exc:
        raise QCError(f"allowed_dataset_ids must contain integers: {exc}") from exc


def _parse_flag(payload: dict, name: str) -> bool:
    """A true or false in the body; absent or null is false."""
    value = payload.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise QCError(f"{name} must be true or false.")
    return value


def _parse_keys(raw) -> list[str]:
    """The subjects a request names."""
    if not isinstance(raw, list):
        raise QCError("subject_keys must be a list of subject keys, such as ['001_000001'].")
    return [str(key) for key in raw]


def _parse_revision(raw) -> int | None:
    """The revision the administrator saw a subject at, as the case listing gives it."""
    if raw is not None and (not isinstance(raw, int) or isinstance(raw, bool)):
        raise QCError("revision must be the whole number the case listing gives the subject.")
    return raw


def _parse_revisions(raw) -> dict[str, int] | None:
    """Subject key -> the revision the administrator saw the subject at, as the case listing gives it."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in raw.values()
    ):
        raise QCError("revisions must map subject keys to the revisions the case listing gives them.")
    return {str(key): value for key, value in raw.items()}


def _parse_roles(raw) -> list[str]:
    """Accept a list, or a comma-separated string; the store checks the roles themselves."""
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw]
    raise QCError("roles must be a list of roles or a comma-separated string: reviewer, editor, or both.")
