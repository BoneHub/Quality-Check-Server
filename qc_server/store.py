"""The quality-check store: scan index, users, assignments, cases and dataset writes.

The server keeps its credentials apart from everything else. The credentials folder is
inside the container, never on the dataset share::

    /var/lib/bonehub-qc/      (BONEHUB_QC_CREDENTIALS_DIR, a Docker volume)
    |-- server_id             this server's name; a new credentials folder is a new server
    |-- server_private_key    generated on first start, unless BONEHUB_QC_PRIVATE_KEY is set
    |-- admin_key             generated on first start, unless BONEHUB_QC_ADMIN_KEY is set
    `-- users.json            users, their roles, their API keys and the keys' digests;
                              BONEHUB_QC_USERS can define users and their keys

Everything else is on the share, in a folder of this server's own, so that several
servers -- each with its own admin -- can work on one database without overwriting each
other's state. The BoneHub format leaves ``.bonehub_qc`` out of what is published::

    <dataset_root>/.bonehub_qc/<server_id>/
    |-- session.json          which server this is, when it was created and last started
    |-- config.json           policy, see config.QCServerConfig
    |-- assignments.json      which scan is with whom, and how it ended
    |-- cases.json            the scans in progress, with every verdict so far (see workflow)
    |-- cases_done.jsonl      the scans approved or closed, a line each
    |-- staged/               editors' corrected segmentations, waiting for approval,
    |                         by scan: ds-NNNN/sub-NNNNNN/scan-NN/segmentation.seg.nrrd
    |-- submissions.jsonl     append-only audit trail
    |-- server.log
    |-- backups/              the scans' segmentations, kept before an approval overwrote them
    `-- tmp/                  uploads being validated

The verdicts of reviewers and editors go into this folder only. Nothing in the dataset
changes until the administrator approves a scan (:meth:`QCStore.approve`): only then is a
corrected segmentation moved in, and the origins of its labels written into the scan's
``scan.json`` -- ``manual`` for those an editor drew.

The BoneHub format does not record whether a label was reviewed, so the servers do: the
approved cases in every server's ``cases_done.jsonl`` say which labels each approval left
reviewed, and with which segmentation. A label is reviewed while the scan's segmentation is
still that one (see :meth:`QCStore.reviewed_labels`), whichever server approved it.

A scan that another server has leased, or has in progress, is not handed out here, so two
servers never work on the same scan at the same time.

Only datasets written in this server's major version of the BoneHub format are served: in
another, the layout, scan.json and segmentation format may differ. A state folder kept for
an earlier version of the format is refused, rather than its cases read as if about this one.

A re-entrant lock serialises the server's state; run the server with one worker. The writes
into the dataset have a lock of their own, taken before the first when both are needed, so
that copying a segmentation onto the share holds up nobody's lease. User accounts have a
lock of their own too, taken inside the first when both are needed, so that checking a key
-- which every request does -- never waits for a write to the share.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from bonehub_format import (
    SCHEMA_VERSION,
    SEGMENTATION_SUFFIX,
    BoneLabelMap,
    DatasetInfo,
    ScanInfo,
    SubjectInfo,
    is_compatible_schema_version,
    layout,
)

from . import __version__ as SERVER_VERSION
from . import auth, workflow
from .audit import AuditLog, utc_now_iso
from .config import CONFIG_FILE_NAME, DEFAULT_STATE_DIR_NAME, EDITED_ORIGIN, QCServerConfig
from .models import (
    DATA_ACCESS_DESCRIPTIONS,
    DEFAULT_DATA_ACCESS,
    DEFAULT_ROLES,
    EDITOR,
    REVIEWER,
    ROLES,
    Assignment,
    Case,
    FileFingerprint,
    QueueStats,
    User,
)
from .segmentation import (
    SegmentationError,
    SegmentDescription,
    UploadedSegmentation,
    changed_labels,
    check_stored_geometry,
    read_segment_table,
    read_segmentation_upload,
    write_segmentation,
)

USERS_FILE_NAME = "users.json"
ASSIGNMENTS_FILE_NAME = "assignments.json"
CASES_FILE_NAME = "cases.json"
DONE_CASES_FILE_NAME = "cases_done.jsonl"
SESSION_FILE_NAME = "session.json"
BACKUP_DIR_NAME = "backups"
STAGED_DIR_NAME = "staged"
TMP_DIR_NAME = "tmp"

#: Marker for an argument that was not supplied, where ``None`` is itself a valid value.
UNSET = object()

#: Every label a segment can be, which leaves out BACKGROUND.
LABEL_NAME_TO_VALUE: dict[str, int] = {label.name: label.value for label in BoneLabelMap if label.value != 0}

#: How much of a file is read at a time to fingerprint it.
_HASH_CHUNK_BYTES = 2 * 1024 * 1024

#: The longest remark an approval adds to a scan's remarks in scan.json, its tag included.
MAX_REMARK_LENGTH = 500

#: What each stage waits for, in messages.
_STAGE_WAITS_FOR = {
    workflow.REVIEW: "a reviewer",
    workflow.EDIT: "an editor",
    workflow.APPROVAL: "the administrator's approval",
    workflow.ESCALATED: "the administrator",
    workflow.REJECTED_SCAN: "the administrator, as a reviewer rejected the scan",
    workflow.APPLIED: "nothing: it is approved and in the dataset",
    workflow.CLOSED: "nothing: the administrator closed it",
}


class QCError(Exception):
    """Something the caller did wrong. Carries the HTTP status the API should return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ScanRef:
    """One indexed scan that is a candidate for quality check."""

    scan_key: str
    has_image: bool
    has_segmentation: bool

    @property
    def ids(self) -> tuple[int, int, int]:
        """The scan's dataset, subject and scan ids."""
        return layout.parse_scan_key(self.scan_key)

    @property
    def dataset_id(self) -> int:
        return self.ids[0]

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return self.ids


@dataclass
class Approval:
    """What the latest approval of a scan on any server of the database left reviewed."""

    applied_at: str
    result: FileFingerprint | None
    reviewed: frozenset[str]


def scan_ids(scan_key: str) -> tuple[int, int, int]:
    """``(dataset_id, subject_id, scan_id)`` of a scan key such as 'ds-0027/sub-000001/scan-01'.
    Raises :class:`QCError` for anything that is not one."""
    try:
        return layout.parse_scan_key(scan_key)
    except (TypeError, ValueError) as exc:
        raise QCError(f"'{scan_key}' is not a scan, such as ds-0027/sub-000001/scan-01.", status_code=404) from exc


class QCStore:
    """All server state and every write that touches the dataset folder."""

    def __init__(
        self,
        dataset_root: Path,
        credentials_dir: Path,
        config: QCServerConfig | None = None,
        state_root: Path | None = None,
    ):
        """``credentials_dir`` is inside the container; ``state_root`` is the folder on the
        share that holds one folder per server, ``<dataset_root>/.bonehub_qc`` by default."""
        if not dataset_root.is_dir():
            raise RuntimeError(f"Dataset root '{dataset_root}' does not exist or is not a directory.")

        self.dataset_root = dataset_root
        self.credentials_dir = Path(credentials_dir)
        self.state_root = Path(state_root) if state_root else dataset_root / DEFAULT_STATE_DIR_NAME
        if _is_within(self.credentials_dir, self.dataset_root):
            raise RuntimeError(
                f"The credentials folder '{self.credentials_dir}' is inside the dataset folder. Credentials must "
                "stay inside the container, off the shared dataset; set BONEHUB_QC_CREDENTIALS_DIR elsewhere."
            )

        auth.ensure_credentials_dir(self.credentials_dir)
        self.server_id, self.server_created = auth.load_or_create_server_id(self.credentials_dir)
        self.state_dir = self.state_root / self.server_id
        _check_state_format(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name in (BACKUP_DIR_NAME, STAGED_DIR_NAME, TMP_DIR_NAME):
            (self.state_dir / name).mkdir(exist_ok=True)

        self.audit = AuditLog(self.dataset_root, self.state_dir)

        self.config_path = self.state_dir / CONFIG_FILE_NAME
        self.config = config or QCServerConfig.load(self.config_path)
        self.config.save(self.config_path)

        self.private_key = auth.load_or_create_private_key(self.credentials_dir)
        self.admin_key, self.admin_key_generated = auth.load_or_create_admin_key(self.credentials_dir)

        self._lock = threading.RLock()
        self._users_lock = threading.RLock()
        #: Serialises the writes into the dataset, which only an approval makes.
        self._dataset_lock = threading.Lock()

        self._users: dict[str, User] = self._load_users()
        self._users_stamp = self._users_file_stamp()
        self._assignments: dict[str, Assignment] = self._load_assignments()
        self._cases: dict[str, Case] = self._load_cases()
        #: Scans whose approval is being written into the dataset right now.
        self._applying: set[str] = set()

        self._record_session()
        self._define_users_from_env()

        self._index: list[ScanRef] = []
        self._index_by_key: dict[str, ScanRef] = {}
        self._dataset_info: dict[int, dict] = {}
        self._index_built_at: datetime | None = None
        self._total_scans = 0
        #: The other servers' finished cases, by file, as last read: (size, mtime) and what they approved.
        self._approvals_read: dict[Path, tuple[tuple[int, int], dict[str, Approval]]] = {}
        #: Whether a segmentation is still the one an approval left, by (path, size, mtime, digest).
        self._still_approved: dict[tuple[str, int, int, str], bool] = {}
        self.refresh_index()

    # ------------------------------------------------------------------ paths
    def dataset_path(self, dataset_id: int) -> Path:
        return layout.dataset_path(self.dataset_root, dataset_id)

    def dataset_info_path(self, dataset_id: int) -> Path:
        return self.dataset_path(dataset_id) / layout.DATASET_JSON

    def scan_path(self, scan_key: str) -> Path:
        """The scan's folder in the database."""
        return layout.scan_path(self.dataset_root, *scan_ids(scan_key))

    def scan_info_path(self, scan_key: str) -> Path:
        return self.scan_path(scan_key) / layout.SCAN_JSON

    def subject_info_path(self, scan_key: str) -> Path:
        """The subject.json of the person the scan is of."""
        return self.scan_path(scan_key).parent / layout.SUBJECT_JSON

    def image_path(self, scan_key: str) -> Path:
        return self.scan_path(scan_key) / layout.IMAGE_FILE

    def segmentation_path(self, scan_key: str) -> Path:
        """Where the scan's segmentation belongs, whether or not it exists yet."""
        return self.scan_path(scan_key) / layout.SEGMENTATION_FILE

    def staged_path(self, scan_key: str) -> Path:
        """Where an editor's corrected segmentation of the scan waits for approval."""
        return self.state_dir / STAGED_DIR_NAME / layout.scan_key(*scan_ids(scan_key)) / layout.SEGMENTATION_FILE

    def current_segmentation_path(self, scan_key: str) -> Path | None:
        """The segmentation under quality check: an editor's correction waiting for approval,
        else the scan's own. None when there is neither."""
        with self._lock:
            case = self._cases.get(scan_key)
            staged = case is not None and case.staged
        path = self.staged_path(scan_key) if staged else self.segmentation_path(scan_key)
        return path if path.exists() else None

    # --------------------------------------------------------------- sessions
    def _record_session(self, started: bool = False) -> None:
        """Write this server's ``session.json``, so the servers of a dataset can be told apart.

        ``started`` marks a start of the server itself; the CLI, which opens the same state
        next to it, leaves the start time alone.
        """
        path = self.state_dir / SESSION_FILE_NAME
        previous = _read_json(path)
        previous = previous if isinstance(previous, dict) else {}
        now = utc_now_iso()
        _atomic_write_json(
            path,
            {
                "server_id": self.server_id,
                "created_at": previous.get("created_at") or now,
                "last_started_at": now if started else previous.get("last_started_at"),
                "host": socket.gethostname() if started else previous.get("host", socket.gethostname()),
                "server_version": SERVER_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
        )

    def mark_started(self) -> None:
        """Record that the server itself has started on this state."""
        self._record_session(started=True)

    def sessions(self) -> list[dict]:
        """Every server that has kept state in this database, this one included, oldest first."""
        found = []
        for path in sorted(self.state_root.glob(f"*/{SESSION_FILE_NAME}")):
            session = _read_json(path)
            if isinstance(session, dict):
                found.append({**session, "this_server": path.parent == self.state_dir})
        return sorted(found, key=lambda session: str(session.get("created_at", "")))

    def _claimed_by_other_sessions(self) -> set[str]:
        """Scans another server on this database has out right now, or has in progress.

        A scan in progress there carries verdicts that wait for that server's
        administrator, and are not in the dataset yet, so it must not be reviewed here too.
        """
        now = datetime.now(timezone.utc)
        claimed: set[str] = set()
        for path in self.state_root.glob(f"*/{ASSIGNMENTS_FILE_NAME}"):
            if path.parent == self.state_dir:
                continue
            entries = _read_json(path)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("state") != "assigned":
                    continue
                try:
                    if isinstance(entry.get("scan_key"), str) and _parse_iso(str(entry.get("expires_at"))) > now:
                        claimed.add(entry["scan_key"])
                except ValueError:
                    continue
        for path in self.state_root.glob(f"*/{CASES_FILE_NAME}"):
            if path.parent == self.state_dir:
                continue
            entries = _read_json(path)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and entry.get("stage") in workflow.OPEN_STAGES:
                    if isinstance(entry.get("scan_key"), str):
                        claimed.add(entry["scan_key"])
        return claimed

    # ------------------------------------------------------------------ index
    def refresh_index(self) -> None:
        """Rebuild the list of scans eligible for quality check from disk.

        Reads the dataset.json of each dataset folder (``ds-NNNN``) and the scan.json of each of
        its scans. A dataset written in another major version of the format is skipped, and so is
        a scan whose scan.json cannot be read; an eligible scan without an image, or whose
        subject has no subject.json -- a subject the converter has not finished -- is not handed out.
        """
        index: list[ScanRef] = []
        dataset_info: dict[int, dict] = {}
        total = 0
        approvals = self.approvals()

        for dataset_dir in sorted(self.dataset_root.glob("ds-*")):
            if not dataset_dir.is_dir():
                continue
            dataset_id = layout.parse_dataset_dir(dataset_dir.name)
            if dataset_id is None:
                self.audit.event(f"Skipping '{dataset_dir.name}': it is not a dataset folder (ds-NNNN).")
                continue
            if self.config.allowed_dataset_ids is not None and dataset_id not in self.config.allowed_dataset_ids:
                continue
            if not (dataset_dir / layout.DATASET_JSON).is_file():
                self.audit.event(f"Skipping '{dataset_dir.name}': it has no {layout.DATASET_JSON}.")
                continue
            try:
                info = self._read_dataset_info(dataset_id)
            except Exception as exc:
                self.audit.event(f"Skipping '{dataset_dir.name}': its {layout.DATASET_JSON} could not be read: {exc}")
                continue
            if not is_compatible_schema_version(info.get("schema_version")):
                self.audit.event(
                    f"Skipping '{dataset_dir.name}': it was written in version "
                    f"{info.get('schema_version') or '(not recorded)'} of the BoneHub format, but this server reads "
                    f"{SCHEMA_VERSION}. Convert it again with the current converters."
                )
                continue
            dataset_info[dataset_id] = info

            for subject_id, scan_id, scan_dir in _scan_folders(dataset_dir):
                key = layout.scan_key(dataset_id, subject_id, scan_id)
                try:
                    scan = ScanInfo.load(scan_dir / layout.SCAN_JSON)
                except FileNotFoundError:
                    continue  # a folder that is no scan (yet)
                except Exception as exc:
                    self.audit.event(f"Skipping scan {key}: its {layout.SCAN_JSON} could not be read: {exc}")
                    continue
                total += 1
                if not self._is_eligible(key, scan, approvals):
                    continue
                if not (scan_dir.parent / layout.SUBJECT_JSON).is_file():
                    self.audit.event(
                        f"Scan {key} is eligible, but its subject has no {layout.SUBJECT_JSON}: the converter has not "
                        "finished it, so it will not be handed out."
                    )
                    continue
                if not (scan_dir / layout.IMAGE_FILE).is_file():
                    self.audit.event(f"Scan {key} is eligible but its image file is missing; it will not be handed out.")
                    continue
                index.append(
                    ScanRef(
                        scan_key=key,
                        has_image=True,
                        has_segmentation=(scan_dir / layout.SEGMENTATION_FILE).is_file(),
                    )
                )

        index.sort(key=lambda ref: ref.sort_key)
        with self._lock:
            self._index = index
            self._index_by_key = {ref.scan_key: ref for ref in index}
            self._dataset_info = dataset_info
            self._total_scans = total
            self._index_built_at = datetime.now(timezone.utc)
        self.audit.event(f"Scan index rebuilt: {len(index)} eligible of {total} scans.")

    def _ensure_fresh_index(self) -> None:
        """Rebuild when the cached index has aged past ``index_refresh_seconds``."""
        ttl = self.config.index_refresh_seconds
        with self._lock:
            built_at = self._index_built_at
        if ttl == 0 or built_at is None:
            self.refresh_index()
            return
        if datetime.now(timezone.utc) - built_at > timedelta(seconds=ttl):
            self.refresh_index()

    def _is_eligible(self, scan_key: str, scan: ScanInfo, approvals: dict[str, Approval]) -> bool:
        """Whether a scan is queued: a label of its segmentation is under review (see
        ``workflow.under_review``), or it has no segmentation and the server queues those."""
        origins = scan.segmentation or {}
        if not origins:
            return self.config.include_scans_without_segmentation
        states = self.config.eligible_review_states
        if len(states) == 2:  # reviewed or not, every label of an eligible origin is
            reviewed: set[str] = set()
        else:
            reviewed = self.reviewed_labels(scan_key, approvals)
        return any(
            workflow.under_review(origin, name in reviewed, self.config.eligible_origins, states)
            for name, origin in origins.items()
        )

    # -------------------------------------------------------------- reviews
    def approvals(self) -> dict[str, Approval]:
        """The latest approval of each scan, on this server or any other on the database: what it
        left reviewed, and the segmentation it left. Another server's finished cases are read from
        its ``cases_done.jsonl``, again only once the file has changed."""
        found: dict[str, Approval] = {}

        def keep(key: str, approval: Approval) -> None:
            known = found.get(key)
            if known is None or approval.applied_at >= known.applied_at:
                found[key] = approval

        for path in sorted(self.state_root.glob(f"*/{DONE_CASES_FILE_NAME}")):
            if path.parent != self.state_dir:
                for key, approval in self._approvals_in(path).items():
                    keep(key, approval)
        with self._lock:
            own = [case for case in self._cases.values() if case.stage == workflow.APPLIED]
        for case in own:
            keep(case.scan_key, Approval(case.applied_at or "", case.result, frozenset(case.reviewed_labels or ())))
        return found

    def _approvals_in(self, path: Path) -> dict[str, Approval]:
        """The approvals another server recorded in its done file, as last read while unchanged.
        A line that is no approved case of this format -- cut short, or about another layout --
        is passed over."""
        try:
            stat = path.stat()
        except OSError:
            return {}
        stamp = (stat.st_size, stat.st_mtime_ns)
        cached = self._approvals_read.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        approvals: dict[str, Approval] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict) or entry.get("stage") != workflow.APPLIED:
                            continue
                        key, at, reviewed = entry["scan_key"], entry["applied_at"], entry.get("reviewed_labels") or []
                        result = FileFingerprint(**entry["result"]) if entry.get("result") else None
                    except (ValueError, KeyError, TypeError):
                        continue
                    if not isinstance(key, str) or not isinstance(at, str) or not isinstance(reviewed, list):
                        continue
                    known = approvals.get(key)
                    if known is None or at >= known.applied_at:
                        approvals[key] = Approval(at, result, frozenset(str(name) for name in reviewed))
        except OSError:
            return {}
        self._approvals_read[path] = (stamp, approvals)
        return approvals

    def reviewed_labels(self, scan_key: str, approvals: dict[str, Approval] | None = None) -> set[str]:
        """The labels of a scan that a server on this database has reviewed: those the scan's
        latest approval, on any server, left reviewed -- as long as the scan's segmentation is
        still the one that approval left. Converting the dataset again, or any other tool that
        rewrites the segmentation, so leaves every label of the scan to be reviewed again."""
        approval = (approvals if approvals is not None else self.approvals()).get(scan_key)
        if approval is None or not approval.reviewed:
            return set()
        if not self._is_still(approval.result, self.segmentation_path(scan_key)):
            return set()
        return set(approval.reviewed)

    def _is_still(self, fingerprint: FileFingerprint | None, path: Path) -> bool:
        """:func:`same_file`, remembering the answer for a file whose time moved, so that the
        index does not read it again at every rebuild."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return fingerprint is None
        if fingerprint is None or stat.st_size != fingerprint.size:
            return False
        if stat.st_mtime_ns == fingerprint.mtime_ns:
            return True
        memo = (str(path), stat.st_size, stat.st_mtime_ns, fingerprint.sha256)
        if memo not in self._still_approved:
            self._still_approved[memo] = _sha256(path) == fingerprint.sha256
        return self._still_approved[memo]

    # ------------------------------------------------------------------ users
    @property
    def users_path(self) -> Path:
        """User accounts hold keys, so they stay with the credentials, off the share."""
        return self.credentials_dir / USERS_FILE_NAME

    def _load_users(self) -> dict[str, User]:
        if not self.users_path.exists():
            return {}
        with open(self.users_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["name"]: User(**entry) for entry in raw}

    def _users_file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.users_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _refresh_users(self) -> None:
        """Pick up user accounts another process changed. Caller holds the users lock.

        The CLI runs inside the container next to the live server (``docker compose exec``),
        so an account it adds must reach the server, and must not be lost on its next save.
        """
        stamp = self._users_file_stamp()
        if stamp != self._users_stamp:
            self._users = self._load_users()
            self._users_stamp = stamp

    def _save_users(self) -> None:
        _atomic_write_json(self.users_path, [user.model_dump() for user in self._users.values()])
        self._users_stamp = self._users_file_stamp()

    def list_users(self) -> list[dict]:
        with self._users_lock:
            self._refresh_users()
            users = [user.public_dict() for user in self._users.values()]
        counts = self._assignment_counts_per_user()
        for user in users:
            user.update(counts.get(user["name"], {"open": 0, "reviewed": 0, "edited": 0}))
        return sorted(users, key=lambda u: u["name"].lower())

    def create_user(
        self,
        name: str,
        allowed_dataset_ids: list[int] | None = None,
        note: str = "",
        data_access: str = DEFAULT_DATA_ACCESS,
        roles: Iterable[str] = DEFAULT_ROLES,
        api_key: str | None = None,
    ) -> tuple[User, str]:
        """Create a user and return it together with its API key, a new one unless ``api_key`` is given.

        ``roles`` is what the user may do: ``reviewer``, ``editor``, or both, the default.
        """
        name = name.strip()
        if not name:
            raise QCError("A user name is required.")
        _check_data_access(data_access)
        roles = _check_roles(roles)
        with self._users_lock:
            self._refresh_users()
            if name in self._users:
                raise QCError(f"User '{name}' already exists.", status_code=409)
            api_key = api_key or auth.generate_api_key()
            user = User(
                name=name,
                key_prefix=auth.key_prefix(api_key),
                key_hash=auth.hash_api_key(api_key, self.private_key),
                api_key=api_key,
                created_at=utc_now_iso(),
                active=True,
                roles=roles,
                allowed_dataset_ids=allowed_dataset_ids,
                data_access=data_access,
                note=note,
            )
            self._users[name] = user
            self._save_users()
        self.audit.record(
            "user_created",
            {"user": name, "key_prefix": user.key_prefix, "roles": user.roles, "data_access": user.data_access},
            summary=f"Created user '{name}' ({_account(user)}).",
        )
        return user, api_key

    def rotate_user_key(self, name: str, api_key: str | None = None) -> str:
        """Give a user a new API key, ``api_key`` or a generated one, and invalidate the old one."""
        with self._users_lock:
            user = self._require_user(name)
            api_key = api_key or auth.generate_api_key()
            user.key_prefix = auth.key_prefix(api_key)
            user.key_hash = auth.hash_api_key(api_key, self.private_key)
            user.api_key = api_key
            self._save_users()
        self.audit.record("user_key_rotated", {"user": name}, summary=f"Rotated API key for user '{name}'.")
        return api_key

    def _define_users_from_env(self) -> None:
        """Create the users ``BONEHUB_QC_USERS`` defines, and give each the key it sets there.

        Only the name and the key come from the environment. Roles, what the user is sent and
        the rest are the defaults at first, and whatever the admin panel makes them afterwards.
        """
        for name, api_key in auth.users_from_env().items():
            with self._users_lock:
                self._refresh_users()
                user = self._users.get(name)
            if user is None:
                self.create_user(name, api_key=api_key)
            # The digest too: a new private key leaves a digest that no longer matches the key.
            elif user.api_key != api_key or user.key_hash != auth.hash_api_key(api_key, self.private_key):
                self.rotate_user_key(name, api_key)

    def user_key(self, name: str) -> str:
        """A user's current API key, for the admin panel to show again."""
        with self._users_lock:
            user = self._require_user(name)
        if user.api_key is None:
            raise QCError(
                f"The key of '{name}' was issued before the server kept keys, so it cannot be shown. "
                "Issue a new key to have one that can.",
                status_code=404,
            )
        return user.api_key

    def set_user_active(self, name: str, active: bool) -> User:
        with self._users_lock:
            user = self._require_user(name)
            user.active = active
            self._save_users()
        self.audit.record(
            "user_enabled" if active else "user_disabled",
            {"user": name},
            summary=f"{'Enabled' if active else 'Disabled'} user '{name}'.",
        )
        return user

    def update_user(
        self,
        name: str,
        allowed_dataset_ids=UNSET,
        note: str | None = None,
        data_access: str | None = None,
        roles: Iterable[str] | None = None,
    ) -> User:
        """Change a user. An argument left out is not touched.

        ``allowed_dataset_ids`` takes ``None`` to mean "every dataset the server serves",
        so it needs :data:`UNSET` to tell that apart from "leave the restriction alone" --
        otherwise editing only the note would quietly widen a user's access. A change of
        ``roles`` applies from the user's next request, in whichever client.
        """
        if data_access is not None:
            _check_data_access(data_access)
        if roles is not None:
            roles = _check_roles(roles)
        with self._users_lock:
            user = self._require_user(name)
            if allowed_dataset_ids is not UNSET:
                user.allowed_dataset_ids = allowed_dataset_ids
            if note is not None:
                user.note = note
            if data_access is not None:
                user.data_access = data_access
            if roles is not None:
                user.roles = roles
            self._save_users()
        self.audit.record(
            "user_updated",
            {"user": name, "roles": user.roles, "data_access": user.data_access},
            summary=f"Updated user '{name}' ({_account(user)}).",
        )
        return user

    def delete_user(self, name: str) -> None:
        """Remove a user and release whatever they were still holding."""
        with self._lock, self._users_lock:
            self._require_user(name)
            del self._users[name]
            self._save_users()
            for assignment in self._assignments.values():
                if assignment.user == name and assignment.state == "assigned":
                    assignment.state = "released"
            self._save_assignments()
        self.audit.record("user_deleted", {"user": name}, summary=f"Deleted user '{name}'.")

    def authenticate(self, api_key: str | None) -> User:
        if not api_key:
            raise QCError("Missing API key. Send it in the 'X-API-Key' header.", status_code=401)
        candidate = auth.hash_api_key(api_key, self.private_key)
        with self._users_lock:
            self._refresh_users()
            for user in self._users.values():
                if auth.keys_match(candidate, user.key_hash):
                    if not user.active:
                        raise QCError(f"User '{user.name}' is disabled.", status_code=403)
                    return user
        raise QCError("Invalid API key.", status_code=401)

    def is_admin_key(self, admin_key: str | None) -> bool:
        if not admin_key:
            return False
        # Compare digests rather than raw strings, so the comparison is constant-time over
        # a fixed length no matter how long the supplied key is.
        return auth.keys_match(
            auth.hash_api_key(admin_key, self.private_key),
            auth.hash_api_key(self.admin_key, self.private_key),
        )

    def _require_user(self, name: str) -> User:
        """Caller holds the users lock."""
        self._refresh_users()
        user = self._users.get(name)
        if user is None:
            raise QCError(f"User '{name}' does not exist.", status_code=404)
        return user

    # ------------------------------------------------------------ assignments
    @property
    def assignments_path(self) -> Path:
        return self.state_dir / ASSIGNMENTS_FILE_NAME

    def _load_assignments(self) -> dict[str, Assignment]:
        if not self.assignments_path.exists():
            return {}
        with open(self.assignments_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["assignment_id"]: Assignment(**entry) for entry in raw}

    def _save_assignments(self) -> None:
        _atomic_write_json(self.assignments_path, [a.model_dump() for a in self._assignments.values()])

    def _expire_stale_assignments(self) -> None:
        """Return leases whose TTL has passed to the queue. Caller holds the lock."""
        now = datetime.now(timezone.utc)
        changed = False
        for assignment in self._assignments.values():
            if assignment.state != "assigned":
                continue
            if _parse_iso(assignment.expires_at) < now:
                assignment.state = "expired"
                changed = True
                self.audit.event(
                    f"Assignment {assignment.assignment_id} ({assignment.scan_key}, {assignment.user}) expired."
                )
        if changed:
            self._save_assignments()

    def _assignment_counts_per_user(self) -> dict[str, dict]:
        counts: dict[str, dict] = {}
        with self._lock:
            for assignment in self._assignments.values():
                bucket = counts.setdefault(assignment.user, {"open": 0, "reviewed": 0, "edited": 0})
                if assignment.state == "assigned":
                    bucket["open"] += 1
                elif assignment.state == "submitted":
                    bucket["edited" if assignment.role == EDITOR else "reviewed"] += 1
        return counts

    def _leased_scans(self) -> set[str]:
        """Caller holds the lock and has expired stale leases. The scans out right now."""
        return {assignment.scan_key for assignment in self._assignments.values() if assignment.state == "assigned"}

    def _held(self, user_name: str, role: str | None) -> list[Assignment]:
        """Caller holds the lock. What a user holds, in ``role`` or in any."""
        return [
            a
            for a in self._assignments.values()
            if a.user == user_name and a.state == "assigned" and (role is None or a.role == role)
        ]

    def open_assignments_of(self, user_name: str, role: str | None = None) -> list[Assignment]:
        """The scans a user holds; with ``role``, those handed to them in that role, which
        is what each client lists."""
        with self._lock:
            self._expire_stale_assignments()
            return self._held(user_name, role)

    def next_scan(self, user: User, role: str) -> Assignment:
        """Pick a scan for this user, in the role of their client, and lease it to them.

        A reviewer is handed the scans waiting for a review: first those already in
        progress, then those nobody has looked at yet. An editor is handed the scans a
        reviewer sent back, and those without any segmentation. Nobody reviews their own
        correction, and nobody is handed a scan they could do nothing with. A user holds one
        scan at a time in each role, until they submit or release it.
        """
        if role not in ROLES:
            raise QCError(f"'{role}' is not a role. Use 'reviewer' or 'editor'.")
        if role not in user.roles:
            raise QCError(f"'{user.name}' is not {_a(role)}.", status_code=403)
        self._ensure_fresh_index()
        # Read from the share before the lock is taken: the other servers' state is another
        # process's, which no lock here holds still anyway.
        claimed_elsewhere = self._claimed_by_other_sessions()
        with self._lock:
            self._expire_stale_assignments()

            held = self._held(user.name, role)
            if held:
                # Hand back what they already hold rather than refusing outright, so a client
                # that lost its local copy can pick the same work back up.
                return min(held, key=lambda a: a.assigned_at)

            # A scan whose approval is being written is finished, whatever stage it was at.
            leased = self._leased_scans() | self._applying
            chosen = next(
                (
                    (ref, case)
                    for ref, case in self._candidates(user, role)
                    if ref.scan_key not in leased and ref.scan_key not in claimed_elsewhere
                ),
                None,
            )
            if chosen is None:
                waiting = "a review" if role == REVIEWER else "an editor"
                raise QCError(f"No scan is waiting for {waiting} right now.", status_code=404)
            ref, case = chosen
            dataset_id, subject_id, scan_id = ref.ids
            now = datetime.now(timezone.utc)
            assignment = Assignment(
                assignment_id=uuid.uuid4().hex,
                scan_key=ref.scan_key,
                dataset_id=dataset_id,
                subject_id=subject_id,
                scan_id=scan_id,
                user=user.name,
                role=role,
                case_revision=case.revision if case is not None else 0,
                state="assigned",
                assigned_at=_to_iso(now),
                expires_at=_to_iso(now + timedelta(seconds=self.config.lease_ttl_seconds)),
            )
            self._assignments[assignment.assignment_id] = assignment
            self._save_assignments()
            stage = case.stage if case is not None else workflow.STAGE_OF_ROLE[role]

        self.audit.record(
            "assigned",
            {
                "assignment_id": assignment.assignment_id,
                "user": user.name,
                "role": role,
                "stage": stage,
                "scan_key": ref.scan_key,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
                "scan_id": assignment.scan_id,
                "expires_at": assignment.expires_at,
            },
            summary=(
                f"Assigned scan {ref.scan_key} to '{user.name}' as {role} "
                f"(assignment {assignment.assignment_id})."
            ),
        )
        return assignment

    def _candidates(self, user: User, role: str) -> list[tuple[ScanRef, Case | None]]:
        """Caller holds the lock. The scans this user could be handed in this role, in order:
        those in progress at the role's stage, then those nobody has given a verdict on, then
        those this user released."""
        stage = workflow.STAGE_OF_ROLE[role]
        in_progress: list[tuple[ScanRef, Case | None]] = []
        for case in self._cases.values():
            if case.stage != stage or not self._user_may_access(user, case.dataset_id):
                continue
            if role == REVIEWER and case.edited_by == user.name:
                continue  # nobody reviews their own correction
            has_segmentation = case.staged or case.base is not None
            if not _could_work_on(user, role, has_segmentation):
                continue
            ref = ScanRef(case.scan_key, True, has_segmentation)
            in_progress.append((ref, case))
        in_progress.sort(key=lambda item: item[0].sort_key)

        # A scan nobody has looked at goes to the reviewers, unless it has no segmentation to review.
        fresh: list[tuple[ScanRef, Case | None]] = [
            (ref, None)
            for ref in self._index
            if ref.scan_key not in self._cases
            and self._user_may_access(user, ref.dataset_id)
            and ref.has_segmentation == (role == REVIEWER)
            and _could_work_on(user, role, ref.has_segmentation)
        ]
        if self.config.assignment_strategy == "random":
            random.shuffle(in_progress)
            random.shuffle(fresh)

        # What this user released goes last for them, so Release skips a scan rather than
        # handing it straight back. The sort is stable: the order above holds otherwise.
        released = {
            a.scan_key
            for a in self._assignments.values()
            if a.user == user.name and a.role == role and a.state == "released"
        }
        candidates = in_progress + fresh
        candidates.sort(key=lambda item: item[0].scan_key in released)
        return candidates

    def get_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise QCError(f"Assignment '{assignment_id}' does not exist.", status_code=404)
        if user is not None and assignment.user != user.name:
            raise QCError("This assignment belongs to another user.", status_code=403)
        return assignment

    def release_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        """Give a scan back without judging it, so somebody else can take it."""
        with self._lock:
            assignment = self.get_assignment(assignment_id, user)
            if assignment.state != "assigned":
                raise QCError(f"Assignment '{assignment_id}' is already {assignment.state}.", status_code=409)
            assignment.state = "released"
            self._save_assignments()
        self.audit.record(
            "released",
            {
                "assignment_id": assignment_id,
                "user": assignment.user,
                "role": assignment.role,
                "scan_key": assignment.scan_key,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
                "scan_id": assignment.scan_id,
            },
            summary=f"Released scan {assignment.scan_key} back to the queue.",
        )
        return assignment

    def extend_assignment(self, assignment_id: str, user: User) -> Assignment:
        """Push the lease out by another TTL while a user is still working."""
        with self._lock:
            assignment = self.get_assignment(assignment_id, user)
            if assignment.state != "assigned":
                raise QCError(f"Assignment '{assignment_id}' is already {assignment.state}.", status_code=409)
            assignment.expires_at = _to_iso(
                datetime.now(timezone.utc) + timedelta(seconds=self.config.lease_ttl_seconds)
            )
            self._save_assignments()
        return assignment

    def _user_may_access(self, user: User, dataset_id: int) -> bool:
        if self.config.allowed_dataset_ids is not None and dataset_id not in self.config.allowed_dataset_ids:
            return False
        if user.allowed_dataset_ids is None:
            return True
        return dataset_id in user.allowed_dataset_ids

    # ------------------------------------------------------------------ cases
    @property
    def cases_path(self) -> Path:
        return self.state_dir / CASES_FILE_NAME

    @property
    def done_cases_path(self) -> Path:
        return self.state_dir / DONE_CASES_FILE_NAME

    def _load_cases(self) -> dict[str, Case]:
        """Every case of this server. Where a scan is in both files -- finished, then reopened,
        or a crash between the two writes -- the case of the later revision is the one."""
        cases: dict[str, Case] = {}

        def keep(case: Case) -> None:
            known = cases.get(case.scan_key)
            if known is None or case.revision >= known.revision:
                cases[case.scan_key] = case

        if self.done_cases_path.exists():
            with open(self.done_cases_path, "r", encoding="utf-8") as f:
                for number, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        keep(Case(**json.loads(line)))
                    except ValueError as exc:  # a line cut short by a crash, or edited by hand
                        self.audit.event(
                            f"Line {number} of {DONE_CASES_FILE_NAME} could not be read and was skipped: {exc}",
                            level=logging.WARNING,
                        )
        if self.cases_path.exists():
            with open(self.cases_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    keep(Case(**entry))
        return cases

    def _save_cases(self) -> None:
        """Caller holds the lock. Only the cases in progress are rewritten; a finished case was
        appended to the done file as it finished, so this file stays as small as the work."""
        _atomic_write_json(
            self.cases_path,
            [case.model_dump() for case in self._cases.values() if case.stage not in workflow.FINISHED_STAGES],
        )

    def _archive(self, case: Case) -> None:
        """Caller holds the lock. Record a case that has just finished, then drop it from the
        cases in progress."""
        with open(self.done_cases_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
        self._save_cases()

    def case_of(self, scan_key: str) -> Case | None:
        """A copy of the scan's case, or None when nobody has given a verdict on it."""
        with self._lock:
            case = self._cases.get(scan_key)
            return case.model_copy(deep=True) if case is not None else None

    def handout_case(self, assignment: Assignment) -> Case:
        """The scan's case as it stands, or as it would begin, for a handout."""
        return self.case_of(assignment.scan_key) or self._new_case(assignment.scan_key, fingerprint=False)

    def _new_case(self, scan_key: str, fingerprint: bool = True) -> Case:
        """The case a scan nobody has given a verdict on begins with, read from the dataset: the
        origins scan.json gives its labels, the labels its segmentation paints, and those of them
        a server on this database has reviewed already.

        ``fingerprint`` records the scan's segmentation as it is now, so that approving the
        case can tell whether anything else wrote it meanwhile. That reads the whole file.
        """
        scan = self.scan_info(scan_key)
        path = self.segmentation_path(scan_key)
        painted = [segment.label for segment in self._segment_table_of(path)]
        origins = dict(scan.segmentation or {})
        unlisted = sorted(set(painted) - set(origins))
        if unlisted and fingerprint:
            self.audit.event(
                f"The segmentation of {scan_key} holds {', '.join(unlisted)}, which its {layout.SCAN_JSON} does not "
                "list; they are reviewed, but approving the scan needs their origin in scan.json.",
                level=logging.WARNING,
            )
        return workflow.start_case(
            scan_key,
            scan_ids(scan_key),
            origins,
            painted,
            self.reviewed_labels(scan_key),
            self.config.eligible_origins,
            self.config.eligible_review_states,
            file_fingerprint(path) if fingerprint else None,
            utc_now_iso(),
        )

    def cases(self, stages: Iterable[str] | None = None, limit: int = 500, comment: str | None = None) -> list[dict]:
        """The cases of this server, most recently changed first, each with ``leased_to``: who
        holds the scan right now, if anybody.

        ``comment`` keeps the cases with a comment that contains it, anywhere in their history
        and whoever wrote it, ignoring case and spacing; a remark the administrator added to
        scan.json counts as a comment. Each then carries ``matches``: the steps of its
        history whose comment or remark does.
        """
        wanted = set(stages) if stages else None
        needle = _searchable(comment)
        with self._lock:
            self._expire_stale_assignments()
            holders = {a.scan_key: a.user for a in self._assignments.values() if a.state == "assigned"}
            found: list[tuple[Case, list]] = []
            for case in self._cases.values():
                if wanted is not None and case.stage not in wanted:
                    continue
                if needle is None:
                    found.append((case, []))
                    continue
                matches = [
                    event
                    for event in case.events
                    if any(needle in (_searchable(text) or "") for text in (event.comment, event.details.get("remark")))
                ]
                if matches:
                    found.append((case, matches))
            found.sort(key=lambda item: item[0].updated_at, reverse=True)
            return [
                {
                    **case.model_dump(),
                    "leased_to": holders.get(case.scan_key),
                    **({"matches": [event.model_dump() for event in matches]} if needle is not None else {}),
                }
                for case, matches in found[:limit]
            ]

    def case_segmentation_path(self, scan_key: str) -> Path:
        """The segmentation a case is about: its editor's correction, or the scan's own."""
        case = self._require_case(scan_key)
        path = self.current_segmentation_path(case.scan_key)
        if path is None:
            raise QCError(f"Scan {scan_key} has no segmentation.", status_code=404)
        return path

    def _require_case(self, scan_key: str) -> Case:
        with self._lock:
            case = self._cases.get(scan_key)
        if case is None:
            raise QCError(f"Nobody has given a verdict on scan {scan_key} on this server.", status_code=404)
        return case

    # ------------------------------------------------------------ dataset i/o
    def _read_dataset_info(self, dataset_id: int) -> dict:
        """A dataset's dataset.json, as the format writes it; empty when there is none."""
        path = self.dataset_info_path(dataset_id)
        if not path.exists():
            return {}
        return DatasetInfo.load(path).to_dict()

    def dataset_info(self, dataset_id: int) -> dict:
        with self._lock:
            cached = self._dataset_info.get(dataset_id)
        return cached if cached is not None else self._read_dataset_info(dataset_id)

    def _require_compatible_dataset(self, dataset_id: int) -> None:
        """Refuse a dataset that was converted again, in another version of the format, meanwhile."""
        try:
            version = self._read_dataset_info(dataset_id).get("schema_version")
        except Exception:
            version = None
        if not is_compatible_schema_version(version):
            raise QCError(
                f"Dataset {layout.dataset_dir_name(dataset_id)} is now in version {version or '(not recorded)'} of "
                f"the BoneHub format, but this server writes {SCHEMA_VERSION}; nothing was recorded.",
                status_code=409,
            )

    def scan_info(self, scan_key: str) -> ScanInfo:
        """The scan's scan.json, read afresh: a long-running server never acts on a stale copy of
        a file another tool may have changed."""
        path = self.scan_info_path(scan_key)
        try:
            return ScanInfo.load(path)
        except FileNotFoundError as exc:
            raise QCError(f"Scan {scan_key} has no {layout.SCAN_JSON}.", status_code=404) from exc
        except Exception as exc:
            raise QCError(f"Could not read the {layout.SCAN_JSON} of scan {scan_key}: {exc}", status_code=500) from exc

    def subject_info(self, scan_key: str) -> dict:
        """The subject.json of the person the scan is of, for the clients to show; empty when it
        cannot be read."""
        try:
            return SubjectInfo.load(self.subject_info_path(scan_key)).to_dict()
        except Exception:
            return {}

    def _write_scan_info(self, scan_key: str, info: ScanInfo) -> None:
        """Write the scan's scan.json, as the BoneHub format writes it."""
        _write_metadata(self.scan_info_path(scan_key), info.to_dict())

    def segment_table(self, scan_key: str) -> list[SegmentDescription]:
        """The segments of the scan's segmentation under quality check, from its header alone.

        Empty when there is no segmentation, and when its header cannot be read: a client
        that downloads the file still gets its handout, and the problem is logged.
        """
        path = self.current_segmentation_path(scan_key)
        return self._segment_table_of(path) if path is not None else []

    def _segment_table_of(self, path: Path) -> list[SegmentDescription]:
        if not path.exists():
            return []
        try:
            return read_segment_table(path)
        except SegmentationError as exc:
            self.audit.event(
                f"The segment table of '{path}' could not be read: {exc}", level=logging.WARNING
            )
            return []

    def stored_segmentation_issue(self, scan_key: str) -> str | None:
        """Why the segmentation under quality check cannot be accepted as it is, or None.

        Checked from the file headers, so a client can be told before anyone spends time on a
        scan whose acceptance the server would refuse.
        """
        path = self.current_segmentation_path(scan_key)
        image_file = self.image_path(scan_key)
        if path is None:
            return "There is no stored segmentation."
        if not self.config.require_geometry_match or not image_file.exists():
            return None
        try:
            check_stored_geometry(path, image_file)
        except SegmentationError as exc:
            return str(exc)
        return None


    # ------------------------------------------------------------- submission
    def inspect_segmentation(self, seg_path: Path, scan_key: str) -> UploadedSegmentation:
        """Validate a ``.seg.nrrd`` for a scan; its ``labels`` are the labels it contains."""
        image_file = self.image_path(scan_key)
        check_against = image_file if self.config.require_geometry_match and image_file.exists() else None
        try:
            return read_segmentation_upload(seg_path, check_against)
        except SegmentationError as exc:
            raise QCError(str(exc)) from exc

    def submit(
        self,
        assignment_id: str,
        user: User,
        quality_check_confirmed: bool,
        segmentation_tmp_path: Path | None,
        confirmed_labels: list[str] | None = None,
        comment: str | None = None,
        use_stored_segmentation: bool = False,
        rejected_labels: list[str] | None = None,
        missing_labels: list[str] | None = None,
        role: str | None = None,
    ) -> "SubmissionOutcome":
        """Record a user's verdict on the scan they hold, in this server's state folder.

        Nothing in the dataset changes: the verdict moves the scan along its quality check
        (see :mod:`workflow`), and only the administrator's approval (:meth:`approve`) writes
        it into the dataset. A verdict is given in the role the scan was handed out in,
        which ``role``, the role of the client, must match:

        * a reviewer judges the segmentation as it is (``use_stored_segmentation``), label by
          label: ``confirmed_labels`` accepted, ``rejected_labels`` rejected, for an editor to
          correct or take out, and ``missing_labels`` reported missing, for an editor to add.
          ``quality_check_confirmed=False`` rejects the scan as a whole: it goes to the
          administrator, not to the editors, and its labels stay as they were.
        * an editor uploads the corrected segmentation (``segmentation_tmp_path``), vouching for
          ``confirmed_labels``. ``quality_check_confirmed=False`` sends the scan to the
          administrator.

        Nobody accepts or replaces a segmentation they have not been sent.
        """
        assignment = self.get_assignment(assignment_id, user)
        if assignment.state not in {"assigned", "expired"}:
            raise QCError(f"Assignment '{assignment_id}' was already submitted ({assignment.state}).", status_code=409)
        if quality_check_confirmed and use_stored_segmentation and segmentation_tmp_path is not None:
            raise QCError("Send a segmentation file or set use_stored_segmentation, not both.")
        comment = (comment or "").strip() or None
        rejected_labels = list(rejected_labels or [])
        missing_labels = list(missing_labels or [])
        as_role = self._verdict_role(
            assignment,
            user,
            role,
            quality_check_confirmed,
            segmentation_tmp_path,
            use_stored_segmentation,
            bool(rejected_labels or missing_labels),
        )
        if as_role == REVIEWER:
            return self._submit_review(
                assignment,
                user,
                quality_check_confirmed,
                segmentation_tmp_path,
                use_stored_segmentation,
                confirmed_labels,
                rejected_labels,
                missing_labels,
                comment,
            )
        return self._submit_edit(
            assignment,
            user,
            quality_check_confirmed,
            segmentation_tmp_path,
            use_stored_segmentation,
            confirmed_labels,
            bool(rejected_labels or missing_labels),
            comment,
        )

    def _verdict_role(
        self,
        assignment: Assignment,
        user: User,
        role: str | None,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        judges_labels: bool,
    ) -> str:
        """The role a verdict is given in: the one the scan was handed out in.

        What an account may do is checked against its roles first, whichever client the
        request says it comes from: only an editor uploads a segmentation, and only a reviewer
        judges the segmentation as it is.
        """
        if confirmed and upload is not None and not user.is_editor:
            raise QCError(
                f"'{user.name}' is not an editor, so cannot upload a segmentation: segmentations are corrected "
                "in 3D Slicer, by an editor. Accept or reject each label of the segmentation as it is instead.",
                status_code=403,
            )
        if (confirmed and use_stored or judges_labels) and not user.is_reviewer:
            raise QCError(
                f"'{user.name}' is not a reviewer, so cannot judge the stored segmentation as it is. Upload the "
                "corrected segmentation from 3D Slicer.",
                status_code=403,
            )
        handed_as = assignment.role
        if role is not None and role != handed_as:
            raise QCError(
                f"{assignment.scan_key} was handed to '{user.name}' as {_a(handed_as)}; submit it "
                f"{'on the review page' if handed_as == REVIEWER else 'from 3D Slicer'}.",
                status_code=409,
            )
        if handed_as not in user.roles:
            raise QCError(
                f"'{user.name}' is no longer {_a(handed_as)}, the role {assignment.scan_key} was handed to "
                "them in. Release it.",
                status_code=403,
            )
        return handed_as

    def _submit_review(
        self,
        assignment: Assignment,
        user: User,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        confirmed_labels: list[str] | None,
        rejected: list[str],
        missing: list[str],
        comment: str | None,
    ) -> "SubmissionOutcome":
        key, dataset_id = assignment.scan_key, assignment.dataset_id
        if confirmed and upload is not None:
            raise QCError(
                f"{key} was handed to '{user.name}' for review, which judges the segmentation as it is: send the "
                "verdict with use_stored_segmentation, and no file. Corrections are made in 3D Slicer, once a "
                "reviewer has rejected a label.",
                status_code=409,
            )
        case, revision = self._case_for_verdict(assignment, workflow.REVIEW)
        if not confirmed:
            return self._reject_scan(assignment, user, case, revision, comment)
        painted = set(workflow.painted_labels(case))
        if not use_stored:
            raise QCError(
                "A reviewer's verdict is about the segmentation as it is: set use_stored_segmentation. A "
                "confirmed submission with a corrected segmentation file takes an editor, in 3D Slicer."
            )
        _check_label_names([*(confirmed_labels or []), *rejected, *missing])
        already = sorted(name for name in missing if name in painted)
        if already:
            raise QCError(
                f"These labels are in the segmentation of {key} already: {already}. Reject them if something "
                "is wrong with them; 'missing' is for bones the segmentation lacks."
            )
        not_there = sorted(name for name in rejected if name not in painted)
        if not_there:
            raise QCError(
                f"These rejected labels are not in the segmentation of {key}: {not_there}. Report a bone the "
                "segmentation lacks as missing."
            )
        rejected = sorted({*rejected, *missing})
        if confirmed_labels is None:
            # Accepting "the scan" accepts what is under review, not the labels kept: reviewed
            # already, or of an origin the server does not queue.
            accepted = sorted(set(workflow.labels_in(case, workflow.PENDING)) - set(rejected))
        else:
            accepted = sorted(set(confirmed_labels))
        unknown = [name for name in accepted if name not in case.labels]
        if unknown:
            raise QCError(f"These confirmed labels are not present in the stored segmentation: {unknown}.")
        both = sorted(set(accepted) & set(rejected))
        if both:
            raise QCError(f"These labels are both accepted and rejected: {both}.")
        if not (accepted or rejected) and workflow.labels_in(case, workflow.PENDING):
            raise QCError(
                "The verdict judges no label: accept or reject the labels under review, or report a missing one."
            )

        # Accepting a painted label vouches for the segmentation as it is, so it is held to the
        # same rules as an upload. That reads the whole file, before the lock is taken.
        path = self.current_segmentation_path(key)
        if any(name in painted for name in accepted):
            if path is not None and not user.receives_segmentation:
                raise QCError(
                    f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so cannot accept labels of "
                    f"the segmentation of {key}, which they have not seen. Reject its labels with a comment instead, "
                    "or release it.",
                    status_code=403,
                )
            if path is None:
                raise QCError(f"Scan {key} has no segmentation to accept.", status_code=409)
            try:
                stored = self.inspect_segmentation(path, key)
            except QCError as exc:
                raise QCError(
                    f"The stored segmentation of {key} cannot be accepted as it is. {exc.message} An editor can "
                    "correct it in 3D Slicer, which writes it back on the image's voxel grid: reject its labels "
                    "with a comment.",
                    status_code=409,
                ) from exc
            if not stored.labels:
                raise QCError("The stored segmentation is empty; there is nothing to accept.")
            not_painted = [name for name in accepted if name in painted and name not in stored.labels]
            if not_painted:
                raise QCError(f"These confirmed labels are not present in the stored segmentation: {not_painted}.")
        self._require_compatible_dataset(dataset_id)

        now = utc_now_iso()
        with self._lock:
            live = self._check_turn(assignment, revision)
            case = live if live is not None else case
            details = workflow.apply_review(case, user.name, accepted, rejected, comment, assignment.assignment_id, now)
            self._cases[key] = case
            self._save_cases()
            self._finish_assignment(
                assignment,
                confirmed,
                comment,
                now,
                case.stage,
                accepted_labels=details["accepted"],
                rejected_labels=details["rejected"],
                missing_labels=details["missing"],
            )
            snapshot = case.model_copy(deep=True)

        outcome = SubmissionOutcome(
            assignment=assignment,
            case=snapshot,
            accepted_labels=details["accepted"],
            rejected_labels=details["rejected"],
            missing_labels=details["missing"],
            pending_labels=workflow.labels_in(snapshot, workflow.PENDING),
            message=_review_message(snapshot, details),
        )
        verdict = []
        if details["accepted"]:
            verdict.append(f"accepted {', '.join(details['accepted'])}")
        if details["rejected"]:
            verdict.append(f"rejected {', '.join(details['rejected'])}")
        if details["missing"]:
            verdict.append(f"reported missing {', '.join(details['missing'])}")
        self._record_submission(
            outcome,
            user,
            f"Scan {key} reviewed by '{user.name}': {'; '.join(verdict) or 'no label judged'}. "
            f"It now waits for {_STAGE_WAITS_FOR[snapshot.stage]}.",
            comment,
        )
        return outcome

    def _reject_scan(
        self, assignment: Assignment, user: User, case: Case, revision: int, comment: str | None
    ) -> "SubmissionOutcome":
        """A reviewer rejected the scan as a whole: no correction of its segmentation would
        help. It goes to the administrator, and its labels stay as they were."""
        key = assignment.scan_key
        now = utc_now_iso()
        with self._lock:
            live = self._check_turn(assignment, revision)
            case = live if live is not None else case
            workflow.reject_scan(case, user.name, comment, assignment.assignment_id, now)
            self._cases[key] = case
            self._save_cases()
            self._finish_assignment(assignment, False, comment, now, case.stage)
            snapshot = case.model_copy(deep=True)
        outcome = SubmissionOutcome(
            assignment=assignment,
            case=snapshot,
            pending_labels=workflow.labels_in(snapshot, workflow.PENDING),
            message=(
                "Scan rejected: it goes to the administrator with your comment, not to the editors. Nothing in "
                "the dataset changed."
            ),
        )
        self._record_submission(
            outcome, user, f"Scan {key} rejected as a whole by '{user.name}'; it goes to the administrator.", comment
        )
        return outcome

    def _submit_edit(
        self,
        assignment: Assignment,
        user: User,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        confirmed_labels: list[str] | None,
        judges_labels: bool,
        comment: str | None,
    ) -> "SubmissionOutcome":
        key, dataset_id = assignment.scan_key, assignment.dataset_id
        if judges_labels:
            raise QCError(
                f"{key} was handed to '{user.name}' for editing: an editor corrects the segmentation, and reviewers "
                "reject labels or report them missing.",
                status_code=409,
            )
        case, revision = self._case_for_verdict(assignment, workflow.EDIT)

        if not confirmed:
            # The editor could not, or would not, correct it: the administrator decides.
            now = utc_now_iso()
            with self._lock:
                live = self._check_turn(assignment, revision)
                case = live if live is not None else case
                workflow.escalate(case, user.name, comment, assignment.assignment_id, now)
                self._cases[key] = case
                self._save_cases()
                self._finish_assignment(assignment, False, comment, now, case.stage)
                snapshot = case.model_copy(deep=True)
            outcome = SubmissionOutcome(
                assignment=assignment,
                case=snapshot,
                message="Sent to the administrator with your comment. Nothing in the dataset changed.",
            )
            self._record_submission(
                outcome, user, f"Scan {key} sent to the administrator by '{user.name}', who could not correct it.", comment
            )
            return outcome

        if use_stored:
            raise QCError(
                f"{key} was handed to '{user.name}' for editing: upload the corrected segmentation from 3D Slicer -- "
                "unchanged, if it needs no correction.",
                status_code=409,
            )
        if upload is None:
            raise QCError("A confirmed submission must include the corrected segmentation file.")
        current = self.current_segmentation_path(key)
        if current is not None and not user.receives_segmentation:
            raise QCError(
                f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so cannot confirm or replace "
                f"the segmentation of {key}, which they have not seen. Reject the scan with a comment instead.",
                status_code=403,
            )

        # Checking the upload, comparing it with the segmentation it replaces and writing its
        # canonical form are the slow part, so they happen before the lock is taken.
        uploaded = self.inspect_segmentation(upload, key)
        present = uploaded.labels
        if not present:
            raise QCError("The uploaded segmentation is empty; there is nothing to confirm.")
        if confirmed_labels is None:
            vouched = set(present)
        else:
            _check_label_names(confirmed_labels)
            not_painted = [name for name in confirmed_labels if name not in present]
            if not_painted:
                raise QCError(f"These confirmed labels are not present in the uploaded segmentation: {not_painted}.")
            vouched = set(confirmed_labels)
        changed = changed_labels(current, uploaded) if current is not None else None
        self._require_compatible_dataset(dataset_id)

        staged_tmp = self.new_upload_path(prefix="staged")
        try:
            write_segmentation(uploaded, staged_tmp)
            now = utc_now_iso()
            with self._lock:
                live = self._check_turn(assignment, revision)
                case = live if live is not None else case
                target = self.staged_path(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace(staged_tmp, target)
                details = workflow.apply_edit(
                    case,
                    user.name,
                    present,
                    changed,
                    vouched,
                    self.config.edits_need_review,
                    comment,
                    assignment.assignment_id,
                    now,
                )
                self._cases[key] = case
                self._save_cases()
                self._finish_assignment(
                    assignment,
                    True,
                    comment,
                    now,
                    case.stage,
                    accepted_labels=details["accepted"],
                    edited_labels=details["edited"],
                    removed_labels=details["removed"],
                    segmentation_staged=True,
                )
                snapshot = case.model_copy(deep=True)
        finally:
            staged_tmp.unlink(missing_ok=True)

        outcome = SubmissionOutcome(
            assignment=assignment,
            case=snapshot,
            accepted_labels=details["accepted"],
            edited_labels=details["edited"],
            removed_labels=details["removed"],
            pending_labels=details["pending"],
            segmentation_staged=True,
            message=_edit_message(snapshot, details),
        )
        self._record_submission(
            outcome,
            user,
            f"Scan {key} corrected by '{user.name}': "
            + (f"changed or added {', '.join(details['edited'])}" if details["edited"] else "no label changed")
            + (f"; removed {', '.join(details['removed'])}" if details["removed"] else "")
            + f". The correction waits in '{self.staged_path(key)}', and the scan for "
            + f"{_STAGE_WAITS_FOR[snapshot.stage]}.",
            comment,
        )
        return outcome

    def _case_for_verdict(self, assignment: Assignment, stage: str) -> tuple[Case, int]:
        """A working copy of the scan's case, and the revision it was copied at: 0 for a
        scan nobody has given a verdict on, whose case begins with this one.

        Refuses a scan whose case has moved on from the stage the verdict belongs to.
        """
        with self._lock:
            live = self._cases.get(assignment.scan_key)
            case = live.model_copy(deep=True) if live is not None else None
        if case is None:
            return self._new_case(assignment.scan_key), 0
        if case.stage != stage:
            raise QCError(
                f"{assignment.scan_key} no longer waits for {_STAGE_WAITS_FOR[stage]}: it waits for "
                f"{_STAGE_WAITS_FOR[case.stage]}. The verdict was not recorded.",
                status_code=409,
            )
        return case, case.revision

    def _check_turn(self, assignment: Assignment, revision: int) -> Case | None:
        """Caller holds the lock. The scan's live case, once it is sure the verdict counts.

        A user whose lease ran out may still submit, so their work is not lost -- unless the
        scan has been handed to someone else since, or its case changed after the user was
        handed it, or after the verdict was checked against it, or the administrator is
        approving it right now.
        """
        key = assignment.scan_key
        if key in self._applying:
            raise QCError(
                f"The administrator is approving or closing {key} right now, so the verdict was not recorded. Ask "
                "for the next scan.",
                status_code=409,
            )
        if assignment.state == "expired":
            later = False
            for other in reversed(self._assignments.values()):
                if other is assignment:
                    break
                later = later or other.scan_key == key
            if later:
                raise QCError(
                    f"The lease of '{assignment.user}' on {key} ran out, and the scan has been handed out again "
                    "since; the verdict was not recorded.",
                    status_code=409,
                )
        live = self._cases.get(key)
        current = live.revision if live is not None else 0
        if current != revision or current != assignment.case_revision:
            raise QCError(
                f"{key} changed after it was handed out -- someone else gave a verdict on it, or the administrator "
                "acted on it -- so the verdict was not recorded. Ask for the next scan.",
                status_code=409,
            )
        return live

    def _finish_assignment(
        self, assignment: Assignment, confirmed: bool, comment: str | None, now: str, stage: str, **details
    ) -> None:
        """Caller holds the lock. Close the lease with what the verdict did."""
        assignment.state = "submitted"
        assignment.quality_check_confirmed = confirmed
        assignment.submitted_at = now
        assignment.comment = comment
        assignment.stage_after = stage
        for name, value in details.items():
            setattr(assignment, name, value)
        self._save_assignments()

    def _record_submission(self, outcome: "SubmissionOutcome", user: User, summary: str, comment: str | None) -> None:
        assignment = outcome.assignment
        self.audit.record(
            "submission",
            {
                "assignment_id": assignment.assignment_id,
                "user": assignment.user,
                "role": assignment.role,
                "scan_key": assignment.scan_key,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
                "scan_id": assignment.scan_id,
                "quality_check_confirmed": bool(assignment.quality_check_confirmed),
                "stage": outcome.case.stage,
                "accepted_labels": outcome.accepted_labels,
                "rejected_labels": outcome.rejected_labels,
                "missing_labels": outcome.missing_labels,
                "edited_labels": outcome.edited_labels,
                "removed_labels": outcome.removed_labels,
                "pending_labels": outcome.pending_labels,
                "segmentation_staged": outcome.segmentation_staged,
                "data_access": user.data_access,
                "comment": comment,
            },
            summary=summary + _seen(user) + (f" Comment: {comment}" if comment else ""),
        )

    def new_upload_path(self, prefix: str = "upload") -> Path:
        return self.state_dir / TMP_DIR_NAME / f"{prefix}_{uuid.uuid4().hex}{SEGMENTATION_SUFFIX}"

    # --------------------------------------------------------------- approval
    def approve(
        self,
        scan_key: str,
        remark: str | None = None,
        allow_unaccepted: bool = False,
        revision: int | None = None,
    ) -> "ApprovalOutcome":
        """Write an approved scan into the dataset.

        An editor's correction replaces the scan's segmentation, which is backed up first, and
        scan.json lists the labels of the segmentation with their origins: ``manual`` for those
        an editor drew or changed, the origin they had for the rest. The labels a reviewer
        accepted become reviewed, for every server on the database (see ``workflow.reviewed_after``).
        ``remark`` is added to the scan's remarks as ``QC: <remark>``, unless they say so already.

        A scan is approved once it waits for approval. ``allow_unaccepted`` approves one at
        any other stage too -- one a reviewer rejected, an editor gave up on, or the
        administrator closed -- and the labels nobody accepted do not become reviewed. Refused
        when somebody holds the scan, when its case changed after the administrator saw it at
        ``revision``, and when the scan's segmentation changed after its quality check began:
        another server or tool wrote it, and approving would overwrite what they wrote.
        """
        remark = _check_remark(remark)
        with self._lock:
            case = self._admin_case(scan_key)
            _check_revision(case, revision, "approving it")
            if case.stage != workflow.APPROVAL and not allow_unaccepted:
                raise QCError(
                    f"Scan {scan_key} is not waiting for approval: it waits for {_STAGE_WAITS_FOR[case.stage]}.",
                    status_code=409,
                )
            self._applying.add(scan_key)
            snapshot = case.model_copy(deep=True)
        try:
            with self._dataset_lock:
                written = self._write_approved(snapshot, remark)
                now = utc_now_iso()
                with self._lock:
                    case = self._cases[scan_key]
                    segmentation_written = case.staged
                    details = workflow.mark_applied(
                        case,
                        written.origins,
                        written.changes,
                        written.result,
                        str(written.backup_path) if written.backup_path else None,
                        now,
                        remark=remark,
                        remark_added=written.remark_added,
                        outdated=written.outdated,
                    )
                    self._archive(case)
                    # Keep the cached index honest until the next full rebuild.
                    self._index = [ref for ref in self._index if ref.scan_key != scan_key]
                    self._index_by_key.pop(scan_key, None)
                    applied = case.model_copy(deep=True)
        finally:
            with self._lock:
                self._applying.discard(scan_key)

        target = self.segmentation_path(scan_key)
        changes, reviewed, outdated = written.changes, details["reviewed_labels"], written.outdated
        redrawn = sorted(name for name, origin in changes.items() if origin == EDITED_ORIGIN)
        removed = sorted(name for name, origin in changes.items() if origin is None)
        not_accepted = details["not_accepted"]
        remark_added = written.remark_added
        self.audit.record(
            "approved",
            {
                "scan_key": scan_key,
                "dataset_id": applied.dataset_id,
                "subject_id": applied.subject_id,
                "scan_id": applied.scan_id,
                "from_stage": snapshot.stage,
                "updated_labels": changes,
                "reviewed_labels": reviewed,
                "not_accepted": not_accepted,
                "outdated": outdated,
                "remark": remark,
                "remark_added": remark_added,
                "segmentation_written": segmentation_written,
                "segmentation_path": str(target),
                "backup_path": applied.backup_path,
                "verdicts": _verdicts_of(applied),
            },
            dataset_id=applied.dataset_id,
            summary=(
                f"Scan {scan_key} approved"
                + (f" while it waited for {_STAGE_WAITS_FOR[snapshot.stage]}" if snapshot.stage != workflow.APPROVAL else "")
                + f": {len(reviewed)} label(s) reviewed"
                + (f" ({', '.join(reviewed)})" if reviewed else "")
                + (f"; {', '.join(redrawn)} recorded as {EDITED_ORIGIN} in {layout.SCAN_JSON}" if redrawn else "")
                + (f"; {', '.join(removed)} taken out of {layout.SCAN_JSON}" if removed else "")
                + (f"; not reviewed, as nobody accepted them: {_not_accepted_text(applied)}" if not_accepted else "")
                + (f"; '{remark}' added to its remarks" if remark_added else "")
                + (f"; its remarks said '{remark}' already" if remark and not remark_added else "")
                + (
                    f"; the corrected segmentation by '{snapshot.edited_by}' written to '{target}'"
                    if segmentation_written
                    else "; the segmentation is the scan's own, unchanged"
                )
                + (f"; {_outdated_text(outdated)} made from the old segmentation, and left as they were" if outdated else "")
                + ". "
                + _verdicts_text(applied)
            ),
        )
        return ApprovalOutcome(
            case=applied,
            updated_labels=changes,
            reviewed_labels=reviewed,
            segmentation_written=segmentation_written,
            backup_path=applied.backup_path,
            remark=remark,
            remark_added=remark_added,
            outdated=outdated,
            message=(
                f"Approved: {len(reviewed)} label(s) reviewed"
                + (" and the corrected segmentation written into the dataset" if segmentation_written else "")
                + (f"; {len(not_accepted)} label(s) nobody accepted are not reviewed" if not_accepted else "")
                + (f"; '{remark}' added to its remarks" if remark_added else "")
                + (f"; its remarks said '{remark}' already" if remark and not remark_added else "")
                + (f"; {_outdated_text(outdated)} made from the old segmentation are out of date" if outdated else "")
                + "."
            ),
        )

    def _write_approved(self, case: Case, remark: str | None = None) -> "_Written":
        """Caller holds the dataset lock. Write a case into the dataset: an editor's correction
        in place of the scan's segmentation, and the origins of its labels and ``remark`` into
        scan.json, which is left alone when neither changes it. Returns what was written.

        If scan.json cannot be written after the segmentation was, the segmentation is put back,
        so that the scan is not left half approved.
        """
        key = case.scan_key
        self._require_compatible_dataset(case.dataset_id)
        target = self.segmentation_path(key)
        if not same_file(case.base, target):
            raise QCError(
                f"The segmentation of {key} changed after its quality check began -- another server or tool "
                "wrote it -- so approving would overwrite that. Send the scan back to review.",
                status_code=409,
            )
        staged = self.staged_path(key) if case.staged else None
        if staged is not None:
            if not staged.exists():
                raise QCError(
                    f"The corrected segmentation of {key} is missing from '{staged.parent}'. Send the scan back "
                    "to the editors.",
                    status_code=409,
                )
            image_file = self.image_path(key)
            if self.config.require_geometry_match and image_file.exists():
                try:
                    check_stored_geometry(staged, image_file)
                except SegmentationError as exc:
                    raise QCError(
                        f"The corrected segmentation of {key} no longer fits its image: {exc} Send the scan back "
                        "to the editors.",
                        status_code=409,
                    ) from exc

        info = self.scan_info(key)
        before = dict(info.segmentation or {})
        try:
            origins = workflow.approval_origins(case, before)
        except workflow.UnknownOrigin as exc:
            raise QCError(
                f"The segmentation of {key} holds {', '.join(exc.labels)}, which its {layout.SCAN_JSON} does not "
                "list and no editor drew, so where it comes from is unknown. Add it to the segmentation in "
                f"'{self.scan_info_path(key)}' with its origin -- 'source', if it came with the source dataset -- "
                "and approve again.",
                status_code=409,
            ) from exc
        changes = workflow.changed_origins(before, origins)
        remarks = workflow.with_remark(info.remarks, remark) if remark else None
        redrawn = {name for name, label in case.labels.items() if label.painted and label.drawn}
        outdated = {
            kind: sorted(set(getattr(info, kind) or {}) & (redrawn | {n for n, o in changes.items() if o is None}))
            for kind in layout.MODEL_KINDS
        }
        outdated = {kind: labels for kind, labels in outdated.items() if labels}

        previous: Path | None = None
        if staged is not None:
            if target.exists():
                previous = self.new_upload_path(prefix="previous")
                shutil.copy2(target, previous)
            _replace(staged, target)
        try:
            if changes or remarks is not None:
                info.set_labels("segmentation", list(origins), origins)
                if remarks is not None:
                    info.remarks = remarks
                self._write_scan_info(key, info)
        except Exception:
            if staged is not None:
                _replace(target, staged)
                if previous is not None:
                    _replace(previous, target)
            raise

        if staged is not None:
            result = file_fingerprint(target)
        else:
            # Unchanged, as same_file found above: only its time may have moved since the case began.
            result = _restamped(case.base, target)
        backup_path = None
        if previous is not None:
            if self.config.keep_segmentation_backups:
                backup_path = self._backup_destination(key)
                _replace(previous, backup_path)
            else:
                previous.unlink(missing_ok=True)
        return _Written(origins, changes, result, backup_path, remarks is not None, outdated)

    def approve_all(
        self,
        scan_keys: Iterable[str] | None = None,
        remark: str | None = None,
        allow_unaccepted: bool = False,
        revisions: dict[str, int] | None = None,
    ) -> list[dict]:
        """Approve each scan named, or every one waiting for approval; one that cannot be
        approved is reported and the rest go ahead. ``remark`` and ``allow_unaccepted`` are as
        for :meth:`approve`, and ``revisions`` holds the revision each scan was seen at."""
        _check_remark(remark)  # a remark too long is refused before anything is approved
        if scan_keys is None:
            with self._lock:
                scan_keys = sorted(k for k, case in self._cases.items() if case.stage == workflow.APPROVAL)
        revisions = revisions or {}

        def approve(key: str) -> dict:
            outcome = self.approve(key, remark, allow_unaccepted, revisions.get(key))
            return {"message": outcome.message, "remark_added": outcome.remark_added}

        return self._for_each(scan_keys, "approved", "Approving", approve)

    def return_all(
        self,
        scan_keys: Iterable[str],
        to: str,
        comment: str | None = None,
        remark: str | None = None,
        revisions: dict[str, int] | None = None,
    ) -> list[dict]:
        """Send each scan named back, as :meth:`return_case` does; one that cannot be is
        reported and the rest go ahead. ``revisions`` is as for :meth:`approve_all`."""
        _check_destination(to)  # what is asked wrongly is refused before anything is sent back
        _check_comment(comment)
        _check_remark(remark)
        revisions = revisions or {}

        def send(key: str) -> dict:
            case = self.return_case(key, to, comment, remark=remark, revision=revisions.get(key))
            return {"remark_added": case.events[-1].details.get("remark_added", False)}

        return self._for_each(scan_keys, "returned", "Sending back", send)

    def close_all(
        self, scan_keys: Iterable[str], remark: str | None = None, revisions: dict[str, int] | None = None
    ) -> list[dict]:
        """Close each scan named, as :meth:`close_case` does; one that cannot be closed is
        reported and the rest go ahead. ``revisions`` is as for :meth:`approve_all`."""
        _check_remark(remark)  # a remark too long is refused before anything is closed
        revisions = revisions or {}

        def close(key: str) -> dict:
            case = self.close_case(key, remark=remark, revision=revisions.get(key))
            return {"remark_added": case.events[-1].details.get("remark_added", False)}

        return self._for_each(scan_keys, "closed", "Closing", close)

    def _for_each(self, scan_keys: Iterable[str], done: str, doing: str, act) -> list[dict]:
        """Do ``act`` to each scan: ``{"scan_key": ..., done: True}`` with what it returns,
        or, for one it fails for, ``done: False`` with the reason, and the rest go ahead."""
        results = []
        for key in scan_keys:
            try:
                results.append({"scan_key": key, done: True, **act(key)})
            except QCError as exc:
                results.append({"scan_key": key, done: False, "message": exc.message})
            except Exception as exc:  # the share, for one scan, need not stop the others
                self.audit.event(f"{doing} {key} failed: {exc!r}", level=logging.ERROR)
                results.append({"scan_key": key, done: False, "message": f"Could not be written: {exc}"})
        return results

    def return_case(
        self,
        scan_key: str,
        to: str,
        comment: str | None = None,
        remark: str | None = None,
        revision: int | None = None,
    ) -> Case:
        """Send a scan back, to the reviewers (every verdict is reviewed again) or to the
        editors (with the administrator's word, the ``comment``). Reopens a closed scan.
        ``remark`` is added to the scan's remarks in scan.json at once, and nothing else is
        written; ``revision`` is as for :meth:`approve`."""
        _check_destination(to)
        comment = _check_comment(comment)
        remark = _check_remark(remark)
        with self._lock:
            case = self._admin_case(scan_key)
            _check_revision(case, revision, "sending it back")
            self._applying.add(scan_key)
            dataset_id = case.dataset_id
        try:
            remark_added = self._add_remark(scan_key, remark)
            with self._lock:
                case = self._cases[scan_key]
                send = workflow.return_to_review if to == workflow.REVIEW else workflow.return_to_edit
                send(case, comment, utc_now_iso(), remark=remark, remark_added=remark_added)
                self._save_cases()
                snapshot = case.model_copy(deep=True)
        finally:
            with self._lock:
                self._applying.discard(scan_key)
        self.audit.record(
            "returned",
            {
                "scan_key": scan_key,
                "to": to,
                "stage": snapshot.stage,
                "comment": comment,
                "remark": remark,
                "remark_added": remark_added,
            },
            dataset_id=dataset_id if remark_added else None,
            summary=(
                f"Scan {scan_key} sent back to the {'reviewers' if to == workflow.REVIEW else 'editors'} by the "
                f"administrator; it waits for {_STAGE_WAITS_FOR[snapshot.stage]}."
                + (f" '{remark}' added to its remarks." if remark_added else "")
                + (f" Its remarks said '{remark}' already." if remark and not remark_added else "")
                + (f" Comment: {comment}" if comment else "")
            ),
        )
        return snapshot

    def close_case(
        self, scan_key: str, comment: str | None = None, remark: str | None = None, revision: int | None = None
    ) -> Case:
        """Finish a scan's quality check without writing its labels or segmentation into the
        dataset. ``remark`` is added to the scan's remarks in scan.json, as for an
        approval: how a rejected scan is recorded. The ``comment`` is kept in its history,
        the remark when there is none. It is not handed out again, unless it is sent back.
        ``revision`` is as for :meth:`approve`."""
        comment = _check_comment(comment)
        remark = _check_remark(remark)
        with self._lock:
            case = self._admin_case(scan_key)
            _check_revision(case, revision, "closing it")
            if case.stage == workflow.CLOSED:
                raise QCError(f"Scan {scan_key} is closed already.", status_code=409)
            self._applying.add(scan_key)
            dataset_id = case.dataset_id
        try:
            remark_added = self._add_remark(scan_key, remark)
            with self._lock:
                case = self._cases[scan_key]
                workflow.close(case, comment or remark, utc_now_iso(), remark=remark, remark_added=remark_added)
                self._archive(case)
                snapshot = case.model_copy(deep=True)
        finally:
            with self._lock:
                self._applying.discard(scan_key)
        if remark_added:
            written = f"; '{remark}' added to its remarks, and nothing else written into the dataset."
        elif remark:
            written = f"; its remarks said '{remark}' already, and nothing was written into the dataset."
        else:
            written = "; nothing was written into the dataset."
        self.audit.record(
            "closed",
            {"scan_key": scan_key, "comment": comment, "remark": remark, "remark_added": remark_added},
            dataset_id=dataset_id if remark_added else None,
            summary=f"Scan {scan_key} closed by the administrator{written}"
            + (f" Comment: {comment}" if comment else ""),
        )
        return snapshot

    def _add_remark(self, scan_key: str, remark: str | None) -> bool:
        """Add ``remark`` to the scan's remarks in scan.json; False when there is none or they
        say so already, and nothing is written."""
        if remark is None:
            return False
        with self._dataset_lock:
            self._require_compatible_dataset(scan_ids(scan_key)[0])
            info = self.scan_info(scan_key)
            remarks = workflow.with_remark(info.remarks, remark)
            if remarks is None:
                return False
            info.remarks = remarks
            self._write_scan_info(scan_key, info)
        return True

    def _admin_case(self, scan_key: str) -> Case:
        """Caller holds the lock. A case the administrator may act on: not approved, not being
        approved, and not in anybody's hands."""
        case = self._cases.get(scan_key)
        if case is None:
            raise QCError(f"Nobody has given a verdict on scan {scan_key} on this server.", status_code=404)
        if case.stage == workflow.APPLIED:
            raise QCError(f"Scan {scan_key} is approved and in the dataset already.", status_code=409)
        if scan_key in self._applying:
            raise QCError(f"Scan {scan_key} is being approved, closed or sent back right now.", status_code=409)
        self._expire_stale_assignments()
        holder = next(
            (a.user for a in self._assignments.values() if a.scan_key == scan_key and a.state == "assigned"),
            None,
        )
        if holder is not None:
            raise QCError(
                f"'{holder}' holds scan {scan_key} right now. Release their lease first (Assignments).",
                status_code=409,
            )
        return case

    def _backup_destination(self, scan_key: str) -> Path:
        """Where the segmentation an approval replaces is kept: under the scan's path in
        ``backups/``, named for when it was replaced."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.state_dir / BACKUP_DIR_NAME / layout.scan_key(*scan_ids(scan_key))
        backup_dir.mkdir(parents=True, exist_ok=True)
        stem = layout.SEGMENTATION_FILE[: -len(SEGMENTATION_SUFFIX)]
        return backup_dir / f"{stem}_{stamp}{SEGMENTATION_SUFFIX}"

    # ------------------------------------------------------------------ stats
    def stats(self) -> QueueStats:
        """Where every eligible scan stands; each counts once."""
        self._ensure_fresh_index()
        claimed_elsewhere = self._claimed_by_other_sessions()
        with self._lock:
            self._expire_stale_assignments()
            eligible = list(self._index)
            leased = self._leased_scans()
            stages = {stage: 0 for stage in (*workflow.OPEN_STAGES, *workflow.FINISHED_STAGES)}
            for case in self._cases.values():
                if case.stage in workflow.FINISHED_STAGES or case.scan_key not in leased:
                    stages[case.stage] += 1
            per_dataset: dict[int, int] = {}
            for ref in eligible:
                per_dataset[ref.dataset_id] = per_dataset.get(ref.dataset_id, 0) + 1
            eligible_keys = {ref.scan_key for ref in eligible}
            elsewhere = (claimed_elsewhere & eligible_keys) - leased
            available = len(eligible_keys - set(self._cases) - leased - elsewhere)
            built_at = self._index_built_at
            total = self._total_scans
        return QueueStats(
            total_scans=total,
            eligible_scans=len(eligible),
            available=available,
            assigned=len(leased),
            assigned_by_other_servers=len(elsewhere),
            to_review=stages[workflow.REVIEW],
            to_edit=stages[workflow.EDIT],
            awaiting_approval=stages[workflow.APPROVAL],
            escalated=stages[workflow.ESCALATED],
            rejected_scans=stages[workflow.REJECTED_SCAN],
            applied=stages[workflow.APPLIED],
            closed=stages[workflow.CLOSED],
            datasets=dict(sorted(per_dataset.items())),
            index_built_at=_to_iso(built_at) if built_at else None,
        )

    def all_assignments(self, limit: int = 200, states: Iterable[str] | None = None) -> list[dict]:
        with self._lock:
            self._expire_stale_assignments()
            assignments = list(self._assignments.values())
        if states:
            wanted = set(states)
            assignments = [a for a in assignments if a.state in wanted]
        assignments.sort(key=lambda a: a.assigned_at, reverse=True)
        return [a.model_dump() for a in assignments[:limit]]


@dataclass
class SubmissionOutcome:
    """What ``QCStore.submit`` did, on its way to the API response."""

    assignment: Assignment
    case: Case
    message: str
    accepted_labels: list[str] = field(default_factory=list)
    rejected_labels: list[str] = field(default_factory=list)
    missing_labels: list[str] = field(default_factory=list)
    edited_labels: list[str] = field(default_factory=list)
    removed_labels: list[str] = field(default_factory=list)
    pending_labels: list[str] = field(default_factory=list)
    segmentation_staged: bool = False

    @property
    def stage(self) -> str:
        return self.case.stage


@dataclass
class ApprovalOutcome:
    """What ``QCStore.approve`` wrote into the dataset."""

    case: Case
    #: What changed of the labels scan.json lists: label -> its new origin, None where taken out.
    updated_labels: dict[str, str | None]
    reviewed_labels: list[str]
    segmentation_written: bool
    backup_path: str | None
    message: str
    remark: str | None = None
    remark_added: bool = False
    outdated: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Written:
    """What ``QCStore._write_approved`` wrote."""

    origins: dict[str, str]
    changes: dict[str, str | None]
    result: FileFingerprint | None
    backup_path: Path | None
    remark_added: bool
    outdated: dict[str, list[str]]


# --------------------------------------------------------------------- helpers
def _could_work_on(user: User, role: str, has_segmentation: bool) -> bool:
    """Whether a user could do anything with a scan in this role: nobody is handed one
    they could not."""
    if role == REVIEWER:
        # A review judges a segmentation; without one there is nothing to judge.
        return has_segmentation
    if has_segmentation:
        # Correcting a segmentation takes seeing it.
        return user.receives_segmentation
    # A segmentation made from scratch is painted on the image.
    return user.receives_image


def _check_label_names(names: Iterable[str]) -> None:
    unknown = sorted({name for name in names if name not in LABEL_NAME_TO_VALUE})
    if unknown:
        raise QCError(f"Unknown label names: {unknown}. See 'bonehub_format/labelmap.py'.")


def _check_remark(remark: str | None) -> str | None:
    """The remark as scan.json records it (see ``workflow.tagged_remark``), or None."""
    if remark is not None and not isinstance(remark, str):
        raise QCError("A remark is text.")
    tagged = workflow.tagged_remark(remark)
    if tagged is not None and len(tagged) > MAX_REMARK_LENGTH:
        raise QCError(f"A remark is at most {MAX_REMARK_LENGTH} characters long; this one has {len(tagged)}.")
    return tagged


def _check_comment(comment: str | None) -> str | None:
    """The administrator's comment, trimmed, or None when blank."""
    if comment is not None and not isinstance(comment, str):
        raise QCError("A comment is text.")
    return (comment or "").strip() or None


def _check_destination(to: str) -> None:
    if to not in (workflow.REVIEW, workflow.EDIT):
        raise QCError(f"A scan goes back to 'review' or to 'edit', not to '{to}'.")


def _check_revision(case: Case, revision: int | None, doing: str) -> None:
    """Refuse to act on a case that changed after the administrator saw it at ``revision``."""
    if revision is not None and case.revision != revision:
        raise QCError(
            f"Scan {case.scan_key} changed after you listed it; it now waits for "
            f"{_STAGE_WAITS_FOR[case.stage]}. Look at it again before {doing}.",
            status_code=409,
        )


def _searchable(text: str | None) -> str | None:
    """Text as a search compares it: in lower case, with single spaces. None when blank."""
    return " ".join((text or "").split()).casefold() or None


def _not_accepted_text(case: Case) -> str:
    """The labels nobody accepted, and where each stood, for an approval that left them alone."""
    parts = []
    for name, label in case.labels.items():
        if label.state == workflow.PENDING:
            parts.append(f"{name} (waiting for a reviewer)")
        elif label.state == workflow.REJECTED:
            what = "rejected" if label.painted else "reported missing"
            parts.append(f"{name} ({what}, by '{label.by}')")
    return ", ".join(parts)


def _review_message(case: Case, details: dict) -> str:
    """What a reviewer is told after their verdict."""
    if case.stage == workflow.EDIT:
        parts = []
        if details["rejected"]:
            parts.append(f"{len(details['rejected'])} label(s) rejected")
        if details["missing"]:
            parts.append(f"{len(details['missing'])} reported missing")
        return (
            f"Sent to the editors ({', '.join(parts) or 'as the administrator asked'}). Nothing is written into the "
            "dataset before the administrator approves the scan."
        )
    if case.stage == workflow.APPROVAL:
        return (
            f"Accepted: {len(details['accepted'])} label(s). The scan waits for the administrator's approval; "
            "nothing is written into the dataset before then."
        )
    return (
        f"Recorded: {len(details['accepted'])} label(s) accepted. "
        f"{len(workflow.labels_in(case, workflow.PENDING))} still wait for a reviewer."
    )


def _edit_message(case: Case, details: dict) -> str:
    """What an editor is told after uploading a correction."""
    kept = "The correction waits on the server; nothing is written into the dataset before the administrator approves it."
    if case.stage == workflow.REVIEW:
        return f"Uploaded. {len(details['pending'])} label(s) go to a reviewer. {kept}"
    if case.stage == workflow.APPROVAL:
        return f"Uploaded. The corrected labels are accepted, and the scan waits for approval. {kept}"
    return f"Uploaded. The scan waits for {_STAGE_WAITS_FOR[case.stage]}. {kept}"


def _outdated_text(outdated: dict[str, list[str]]) -> str:
    """The mesh and NURBS files an approval left out of date, in words."""
    return " and ".join(f"the {kind} of {', '.join(labels)}" for kind, labels in sorted(outdated.items()))


def _verdicts_of(case: Case) -> dict[str, dict]:
    """Who stood behind each label of a case, for the audit trail."""
    return {
        name: {key: value for key, value in label.model_dump().items() if value is not None}
        for name, label in case.labels.items()
    }


def _verdicts_text(case: Case) -> str:
    """Who stood behind the accepted labels, in words."""
    by: dict[str, list[str]] = {}
    for name, label in case.labels.items():
        if label.state == workflow.ACCEPTED:
            who = f"accepted by '{label.by}'" + (f", corrected by '{label.edited_by}'" if label.edited_by else "")
            by.setdefault(who, []).append(name)
    return " ".join(f"{', '.join(names)}: {who}." for who, names in sorted(by.items()))


def _a(role: str) -> str:
    return "a reviewer" if role == REVIEWER else "an editor"


def _check_data_access(data_access: str) -> None:
    if data_access not in DATA_ACCESS_DESCRIPTIONS:
        raise QCError(
            f"Unknown data access '{data_access}'. Choose one of: {', '.join(DATA_ACCESS_DESCRIPTIONS)}."
        )


def _check_roles(roles: Iterable[str]) -> list[str]:
    """The roles each once, in the order of ``ROLES``, or a QCError that says what is wrong."""
    roles = [str(role) for role in roles]
    unknown = [role for role in roles if role not in ROLES]
    if unknown:
        raise QCError(f"Unknown role(s): {', '.join(unknown)}. A user is a reviewer, an editor, or both.")
    if not roles:
        raise QCError("A user needs a role: reviewer (the review page), editor (3D Slicer), or both.")
    return [role for role in ROLES if role in roles]


def _account(user: User) -> str:
    """What an account is, for the audit trail: 'reviewer and editor; receives ...'."""
    return f"{' and '.join(user.roles)}; receives {DATA_ACCESS_DESCRIPTIONS[user.data_access]}"


def _seen(user: User) -> str:
    """Audit note on what a verdict was based on, when that was less than the whole scan."""
    if user.data_access == DEFAULT_DATA_ACCESS:
        return ""
    return f" The user is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}."


def file_fingerprint(path: Path) -> FileFingerprint | None:
    """A file as it is now, or None when there is none. Reads the whole file."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=_sha256(path))


def same_file(fingerprint: FileFingerprint | None, path: Path) -> bool:
    """Whether a file is still the one fingerprinted: still missing, or of the same content.

    Its size and time answer for an untouched file without reading it; a file whose time moved
    -- copied, or restored -- is the same when its content is.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return fingerprint is None
    if fingerprint is None or stat.st_size != fingerprint.size:
        return False
    return stat.st_mtime_ns == fingerprint.mtime_ns or _sha256(path) == fingerprint.sha256


def _sha256(path: Path) -> str:
    """The file's SHA-256, read a piece at a time without read-ahead, as downloads are, so
    that reading it does not hold up everyone else's requests on the share."""
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        if hasattr(os, "posix_fadvise"):  # Linux, where the server runs; the tests also run on Windows
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _restamped(fingerprint: FileFingerprint | None, path: Path) -> FileFingerprint | None:
    """A fingerprint of a file known to have the fingerprinted content, with the file's time now."""
    if fingerprint is None:
        return None
    try:
        return fingerprint.model_copy(update={"mtime_ns": path.stat().st_mtime_ns})
    except FileNotFoundError:
        return None


def _write_metadata(path: Path, data: dict) -> None:
    """Write a metadata file of the BoneHub format -- as ``bonehub_format`` writes it, byte for
    byte -- through a temporary file in its folder, so a crash cannot truncate the original."""
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write("\n")
        _replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload) -> None:
    """Write JSON through a temporary file so a crash cannot truncate the original."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)
    _replace(tmp_path, path)


#: Pauses, in seconds, between attempts at a replace that is refused.
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def _replace(source: Path, target: Path) -> None:
    """``os.replace``, tried again for a moment when it is refused.

    On Windows a file that another process has open at that instant cannot be replaced -- a
    virus scanner or indexer looking at a file just written, or another client on an SMB
    share -- and the refusal clears within milliseconds. Failing at once would lose a
    user's submission for nothing.
    """
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def _scan_folders(dataset_dir: Path):
    """``(subject_id, scan_id, folder)`` of each scan folder of a dataset folder, in order: the
    folders named as the BoneHub format names them (sub-NNNNNN/scan-NN)."""
    with os.scandir(dataset_dir) as subjects:
        subject_dirs = sorted(
            (subject_id, Path(entry.path))
            for entry in subjects
            if (subject_id := layout.parse_subject_dir(entry.name)) is not None and entry.is_dir()
        )
    for subject_id, subject_dir in subject_dirs:
        with os.scandir(subject_dir) as scans:
            scan_dirs = sorted(
                (scan_id, Path(entry.path))
                for entry in scans
                if (scan_id := layout.parse_scan_dir(entry.name)) is not None and entry.is_dir()
            )
        for scan_id, scan_dir in scan_dirs:
            yield subject_id, scan_id, scan_dir


def _check_state_format(state_dir: Path) -> None:
    """Refuse a server folder kept for another major version of the BoneHub format: its cases and
    assignments name scans -- or, before version 1, subjects -- of another layout."""
    session = _read_json(state_dir / SESSION_FILE_NAME)
    recorded = session.get("schema_version") if isinstance(session, dict) else None
    older = recorded is not None and str(recorded).split(".")[0] != SCHEMA_VERSION.split(".")[0]
    if not older:
        # A folder whose session.json says nothing: its cases tell, by how they name what they are about.
        for name in (CASES_FILE_NAME, ASSIGNMENTS_FILE_NAME):
            entries = _read_json(state_dir / name)
            if isinstance(entries, list) and any(isinstance(e, dict) and "subject_key" in e for e in entries):
                older, recorded = True, recorded or "0.x"
                break
    if older:
        raise RuntimeError(
            f"This server's folder '{state_dir}' holds a quality check of a dataset in version {recorded} of the "
            f"BoneHub data format, but this server (version {SERVER_VERSION}) works on version {SCHEMA_VERSION}, "
            "whose datasets are laid out differently. Finish that quality check with the version of the server it "
            f"was made with. Then move the folder out of '{state_dir.parent}', keeping it as its history, and start "
            "this server again: it begins a new folder, with the same users and keys."
        )


def _read_json(path: Path):
    """A JSON file's content, or None when it is missing or unreadable -- another server may be
    halfway through replacing it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment
