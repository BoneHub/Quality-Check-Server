"""The study server's admin endpoints: the study's settings, its start and end, and its results.

The admin panel is the quality check's own page (:mod:`qc_server.admin`), which shows its Study
section, and hides the quality check's, when ``/admin/api/session`` says the server is in study
mode. Users and the recent activity use the quality check's endpoints unchanged.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from ..admin import require_admin
from ..audit import utc_now_iso
from ..config import STUDY_MODE
from ..store import QCError, QCStore
from .api import get_study
from .models import StudySettings
from .report import build_report, results_zip
from .store import StudyStore

router = APIRouter(prefix="/admin", tags=["study admin"])


@router.get("/api/session")
def check_session(request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """Used by the panel to validate the key the administrator typed in. ``mode`` tells the
    panel to show the Study section."""
    return {
        "status": "ok",
        "mode": STUDY_MODE,
        "server_id": store.server_id,
        "dataset_root": str(store.dataset_root),
        "state_dir": str(store.state_dir),
        "dataset_writable": os.access(store.dataset_root, os.W_OK),
        "config": store.config.model_dump(),
    }


@router.get("/api/study")
def study_overview(request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """The study, its settings, and each rater's progress; ``{"state": "none"}`` without one."""
    return get_study(request).overview()


@router.post("/api/study/preview")
def preview(payload: dict, request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """Save the settings as the study's draft, and say what they make of it: the subjects, the
    raters' codes and how many readings each has. Refused once the study has started."""
    return get_study(request).preview(_settings(payload))


@router.post("/api/study/start")
def start(payload: dict, request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """Start the study with these settings, which are fixed from then on."""
    return get_study(request).start(_settings(payload))


@router.post("/api/study/end")
def end(request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """Stop handing out readings; the results become final."""
    return get_study(request).end()


@router.delete("/api/study")
def delete(request: Request, store: QCStore = Depends(require_admin)) -> dict:
    """Remove the study and its readings, to set up another. Download the results first."""
    get_study(request).delete()
    return {"status": "deleted"}


@router.get("/api/study/report", response_class=HTMLResponse)
def report(request: Request, store: QCStore = Depends(require_admin)) -> HTMLResponse:
    """The report, as one page with its figures in it."""
    files = build_report(*_study_and_readings(get_study(request)), generated_at=utc_now_iso())
    return HTMLResponse(files.html)


@router.get("/api/study/results")
def results(request: Request, store: QCStore = Depends(require_admin)) -> Response:
    """The results as a zip: the report, its figures as SVG and PNG, and the readings and the
    numbers as CSV files. Codes stand for the raters throughout."""
    study, readings = _study_and_readings(get_study(request))
    files = build_report(study, readings, generated_at=utc_now_iso())
    name = re.sub(r"[^A-Za-z0-9]+", "_", study.settings.name).strip("_") or "study"
    return Response(
        results_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}_results.zip"'},
    )


def _study_and_readings(study: StudyStore):
    found = study.study()
    if found is None or found.state == "draft":
        raise QCError("The study has not started, so there are no results yet.", status_code=404)
    return found, study.readings()


def _settings(payload: dict) -> StudySettings:
    """The settings in the body, or a QCError that says what is wrong with them in words."""
    if not isinstance(payload, dict):
        raise QCError("Send the study's settings as a JSON object.")
    try:
        return StudySettings(**payload)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "invalid")).removeprefix("Value error, ")
            problems.append(f"{where}: {message}" if where else message)
        raise QCError("The study's settings are not valid: " + "; ".join(problems) + ".") from exc
