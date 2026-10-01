"""The study server's FastAPI application: the review page, the admin panel, and the study."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from bonehub_data_schema import __version__ as SCHEMA_VERSION

from .. import __version__, admin, auth, review
from ..config import STUDY_MODE, STUDY_STATE_DIR_NAME, QCServerConfig, resolve_credentials_dir, resolve_dataset_root
from ..store import QCError, QCStore
from . import admin as study_admin
from . import api as study_api
from .store import StudyStore

#: How an administrator reaches the CLI of the running container.
EXEC_CLI = "docker compose exec bonehub-qc-server bonehub-qc-server"


def create_study_app(
    dataset_root: Path | None = None,
    credentials_dir: Path | None = None,
    config: QCServerConfig | None = None,
) -> FastAPI:
    """Build a study server around one dataset root, which it only reads.

    Its user accounts, and everything else it keeps, are in the credentials folder
    (``BONEHUB_QC_CREDENTIALS_DIR``), under ``state/<server id>/``: nothing goes into the
    dataset, so a quality-check server on the same dataset never sees the study.
    """
    dataset_root = Path(dataset_root) if dataset_root else resolve_dataset_root()
    credentials_dir = Path(credentials_dir) if credentials_dir else resolve_credentials_dir()

    app = FastAPI(
        title="BoneHub Quality Check · reliability study",
        version=__version__,
        description=(
            "A reliability study of the reviewers: raters judge the same subjects, each several times, on the "
            "browser review page, and the server measures how far their verdicts agree. It never writes into the "
            "dataset."
        ),
    )
    store = QCStore(
        dataset_root=dataset_root,
        credentials_dir=credentials_dir,
        config=config,
        state_root=credentials_dir / STUDY_STATE_DIR_NAME,
    )
    store.mark_started()
    app.state.store = store
    app.state.study = StudyStore(store)
    app.state.mode = STUDY_MODE

    app.include_router(study_api.router)
    app.include_router(admin.panel_router)
    app.include_router(study_admin.router)
    app.include_router(review.router)
    app.mount("/static", StaticFiles(directory=review.STATIC_DIR), name="static")

    @app.exception_handler(QCError)
    async def handle_qc_error(request: Request, exc: QCError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/admin")

    @app.get("/health", tags=["server"])
    def health() -> dict:
        """Unauthenticated liveness probe, used by the container healthcheck."""
        return {
            "status": "ok",
            "mode": STUDY_MODE,
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "server_id": store.server_id,
            "dataset_root": str(store.dataset_root),
        }

    _announce(app)
    return app


def _announce(app: FastAPI) -> None:
    """Print the startup banner, with the admin key when this server has just generated it."""
    store: QCStore = app.state.store
    study = app.state.study.study()
    writable = os.access(store.dataset_root, os.W_OK)
    details = [
        f"server id    : {store.server_id}" + (" (new server)" if store.server_created else ""),
        f"dataset root : {store.dataset_root} ("
        + ("read-write: BONEHUB_DATASET_ACCESS=ro mounts it read-only" if writable else "read-only")
        + ")",
        f"state folder : {store.state_dir} (inside the container, never in the dataset)",
        f"users        : {len(store.list_users())}",
        "study        : " + (f"'{study.settings.name}', {study.state}" if study else "none set up yet"),
        f"data schema  : {SCHEMA_VERSION}",
    ]
    lines = [
        "BoneHub Quality Check -- reliability study server",
        *(f"  {line}" for line in details),
        "  admin panel  : /admin",
        "  review page  : /review",
    ]
    if os.environ.get(auth.ENV_ADMIN_KEY):
        lines.append("  admin key    : BONEHUB_QC_ADMIN_KEY from .env")
    elif store.admin_key_generated:
        lines += [
            "",
            "  A new admin key was generated for this server:",
            f"      {store.admin_key}",
            f"  It is kept inside the container, in '{store.credentials_dir / auth.ADMIN_KEY_FILE_NAME}', and is",
            "  not printed again. To print it later:",
            f"      {EXEC_CLI} show-admin-key",
        ]
    else:
        lines.append(f"  admin key    : kept inside the container; `{EXEC_CLI} show-admin-key` prints it")
    print("\n".join(lines), flush=True)
    store.audit.event("Study server started. " + " | ".join(details))
