"""Fixtures shared by the tests: a throw-away database in the BoneHub format.

The tests never touch a real dataset. Every test case builds its own database in a
temporary folder, which is what the server is pointed at::

    <root>/ds-0001/dataset.json
    <root>/ds-0001/sub-000001/subject.json
    <root>/ds-0001/sub-000001/scan-01/scan.json
    <root>/ds-0001/sub-000001/scan-01/image.nii.gz
    <root>/ds-0001/sub-000001/scan-01/segmentation.seg.nrrd

and a credentials folder beside it -- not inside it -- standing in for the container's
credentials volume, with a fixed server id so the server's state folder on the "share" is
known in advance: ``<root>/.bonehub_qc/qc_test_server/``.

Metadata and masks are written and read with ``bonehub_format``'s own functions, so the
fixtures are in exactly the format the converters produce. A label that is reviewed already
is one that an earlier server's approval left reviewed (:meth:`DatasetBuilder.mark_reviewed`).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bonehub_format import (
    SCHEMA_VERSION,
    SEGMENTATION_SUFFIX,
    BoneLabelMap,
    ScanInfo,
    SubjectInfo,
    layout,
    read_segmentation,
    write_segmentation,
)

from qc_server import auth
from qc_server.audit import AuditLog, qualitycheck_log_name
from qc_server.config import ENV_PREFIX, QCServerConfig
from qc_server.models import EDITOR, REVIEWER
from qc_server.store import DONE_CASES_FILE_NAME, QCStore, file_fingerprint


def _silence_audit_console() -> None:
    """Keep the audit trail's console echo out of the test report.

    ``AuditLog`` logs to a file and to the console. The file is what the tests assert
    against; the console copy would bury the unittest output, and the store logs during
    its own construction, so the handler is dropped as it is added rather than after.
    """
    original_init = AuditLog.__init__

    def quiet_init(self, dataset_root, state_dir):
        original_init(self, dataset_root, state_dir)
        for handler in list(self.logger.handlers):
            if type(handler) is logging.StreamHandler:  # a FileHandler is a subclass
                self.logger.removeHandler(handler)

    AuditLog.__init__ = quiet_init


_silence_audit_console()

#: Voxel grid used for every fixture image, as a numpy (z, y, x) shape. Small enough that
#: a suite run stays fast.
SHAPE = (6, 6, 6)
SPACING = (1.0, 1.0, 1.0)

LABEL_VALUE = {label.name: label.value for label in BoneLabelMap}

#: The id of the server every test case runs, unless it makes another.
TEST_SERVER_ID = "qc_test_server"

#: The server whose approvals left the labels a fixture marks as reviewed already.
EARLIER_SERVER_ID = "qc_earlier_server"


def scan_key(dataset_id: int, subject_id: int, scan_id: int = 1) -> str:
    """The scan's path in the database, which names it: 'ds-0001/sub-000001/scan-01'."""
    return layout.scan_key(dataset_id, subject_id, scan_id)


def clear_qc_env() -> None:
    """Drop every ``BONEHUB_QC_*`` variable so a developer's shell cannot steer a test."""
    for name in [key for key in os.environ if key.startswith(ENV_PREFIX)]:
        del os.environ[name]


def reference_image(shape=SHAPE, spacing=SPACING) -> sitk.Image:
    """An empty CT-like image on the fixture grid."""
    image = sitk.GetImageFromArray(np.zeros(shape, dtype=np.int16))
    image.SetSpacing(spacing)
    return image


def write_image(path: Path, shape=SHAPE, spacing=SPACING) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(reference_image(shape, spacing), str(path))
    return path


#: The labels the fixtures paint in a place of their own, whatever else a mask holds, so that an
#: upload leaves a label's voxels alone unless a test changes them. Others follow, in name order.
SLOTS = ("FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT", "TIBIA_RIGHT", "SACRUM", "SKULL", "PATELLA_LEFT", "HUMERUS_LEFT")


def segmentation_array(labels, shape=SHAPE, grown=()) -> np.ndarray:
    """A volume of BoneLabelMap values holding one small block per label.

    Label number ``n`` of ``SLOTS`` is painted into slice ``n % depth``, in rows ``2 * (n // depth)``
    and the next, columns 0 and 1. A label in ``grown`` gets a voxel more, which is how a test
    makes an upload change it.
    """
    labels = sorted(labels)
    others = [label for label in labels if label not in SLOTS]
    data = np.zeros(shape, dtype=np.int32)
    for label in labels:
        slot = SLOTS.index(label) if label in SLOTS else len(SLOTS) + others.index(label)
        z, y = slot % shape[0], 2 * (slot // shape[0])
        if y + 2 > shape[1]:
            raise ValueError(f"The fixture volume {shape} has no room left for {label}.")
        data[z, y : y + 2, 0:2] = LABEL_VALUE[label]
        if label in grown:
            data[z, y, 2] = LABEL_VALUE[label]
    return data


def write_mask(path: Path, labels, shape=SHAPE, spacing=SPACING, grown=()) -> Path:
    """A ``.seg.nrrd`` holding the given labels, written the way the converters write one."""
    write_segmentation(segmentation_array(labels, shape, grown), reference_image(shape, spacing), path)
    return path


def write_raw_mask(path: Path, numbers: np.ndarray, header: dict, spacing=SPACING) -> Path:
    """A ``.seg.nrrd`` with arbitrary segment numbers and header, for uploads that are wrong."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = sitk.GetImageFromArray(numbers)
    image.SetSpacing(spacing)
    for key, value in header.items():
        image.SetMetaData(key, str(value))
    sitk.WriteImage(image, str(path), useCompression=True)
    return path


def segment_header(*segments) -> dict:
    """Header entries for ``(number, name, tags)`` segments; ``tags`` may be ``None``."""
    header = {}
    for i, (number, name, tags) in enumerate(segments):
        header[f"Segment{i}_ID"] = name
        header[f"Segment{i}_Name"] = name
        header[f"Segment{i}_LabelValue"] = number
        header[f"Segment{i}_Layer"] = 0
        if tags is not None:
            header[f"Segment{i}_Tags"] = tags
    return header


def labels_in_mask(path: Path) -> set:
    """Label names actually painted into a mask, read back through the schema."""
    # A copy, not a view: a view would outlive the image it points into.
    values = sitk.GetArrayFromImage(read_segmentation(path))
    return {BoneLabelMap(int(v)).name for v in np.unique(values) if int(v) != 0}


class DatasetBuilder:
    """Builds a database the server can be pointed at."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def add_dataset(
        self, dataset_id: int, name: str | None = None, schema_version: str | None = SCHEMA_VERSION
    ) -> "DatasetBuilder":
        """A dataset folder with its dataset.json; ``schema_version`` None leaves it unrecorded."""
        folder = layout.dataset_path(self.root, dataset_id)
        folder.mkdir(parents=True, exist_ok=True)
        info = {"name": name or f"Test dataset {dataset_id}", "license": "CC-BY-4.0"}
        if schema_version is not None:
            info["schema_version"] = schema_version
        (folder / layout.DATASET_JSON).write_text(json.dumps(info, indent=4), encoding="utf-8")
        return self

    def add_scan(
        self,
        dataset_id: int,
        subject_id: int,
        scan_id: int = 1,
        segmentation=None,
        painted=None,
        reviewed=(),
        image: bool = True,
        write_segmentation_file: bool | None = None,
        subject: dict | None = None,
        **scan_fields,
    ) -> "DatasetBuilder":
        """Add one scan, and by default the image and segmentation files that go with it.

        ``segmentation`` lists the labels scan.json gives the segmentation: names, each of
        origin ``source``, or a mapping of names to origins. The mask paints them, or the labels
        in ``painted`` when given, so a test can create a mask and a scan.json that disagree;
        ``write_segmentation_file`` False writes no mask at all. ``reviewed`` are the labels an
        earlier server's approval left reviewed. ``image`` False writes no image file. The
        subject's subject.json is written with the first of its scans, from ``subject``;
        ``scan_fields`` go into scan.json.
        """
        if not (layout.dataset_path(self.root, dataset_id) / layout.DATASET_JSON).exists():
            self.add_dataset(dataset_id)
        origins = _origins(segmentation)
        subject_json = layout.subject_path(self.root, dataset_id, subject_id) / layout.SUBJECT_JSON
        if not subject_json.exists():
            subject_info = {"source_subject_id": f"case_{subject_id}", **(subject or {})}
            SubjectInfo(**subject_info).save(subject_json)
        folder = layout.scan_path(self.root, dataset_id, subject_id, scan_id)
        info = ScanInfo(
            source_scan_id=f"case_{subject_id}/scan_{scan_id}",
            **{"modality": "CT", **scan_fields},
            segmentation=origins or None,
        )
        info.save(folder / layout.SCAN_JSON)

        if image:
            write_image(self.image_file(dataset_id, subject_id, scan_id))
        labels = sorted(origins) if painted is None else sorted(painted)
        if bool(labels) if write_segmentation_file is None else write_segmentation_file:
            write_mask(self.segmentation_file(dataset_id, subject_id, scan_id), labels)
        if reviewed:
            self.mark_reviewed(dataset_id, subject_id, scan_id, reviewed)
        return self

    def mark_reviewed(
        self, dataset_id: int, subject_id: int, scan_id: int, labels, server_id: str = EARLIER_SERVER_ID
    ) -> Path:
        """Record that another server approved the scan, leaving ``labels`` reviewed with its
        segmentation as it is now: a line of that server's done file, as the server writes it."""
        key = scan_key(dataset_id, subject_id, scan_id)
        result = file_fingerprint(self.segmentation_file(dataset_id, subject_id, scan_id))
        case = {
            "scan_key": key,
            "dataset_id": dataset_id,
            "subject_id": subject_id,
            "scan_id": scan_id,
            "stage": "applied",
            "revision": 3,
            "labels": {label: {"state": "accepted", "painted": True} for label in sorted(labels)},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "applied_at": "2026-01-01T00:00:00Z",
            "result": result.model_dump() if result else None,
            "reviewed_labels": sorted(labels),
            "events": [],
        }
        path = self.root / ".bonehub_qc" / server_id / DONE_CASES_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case) + "\n")
        return path

    # --- reading back, for assertions ---------------------------------------
    def scan_info(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> dict:
        """The scan's scan.json as it stands on disk right now."""
        path = layout.scan_path(self.root, dataset_id, subject_id, scan_id) / layout.SCAN_JSON
        return json.loads(path.read_text(encoding="utf-8"))

    def origins(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> dict:
        """The labels scan.json gives the scan's segmentation, with their origins."""
        return self.scan_info(dataset_id, subject_id, scan_id).get("segmentation") or {}

    def scan_folder(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> Path:
        return layout.scan_path(self.root, dataset_id, subject_id, scan_id)

    def segmentation_file(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> Path:
        return self.scan_folder(dataset_id, subject_id, scan_id) / layout.SEGMENTATION_FILE

    def image_file(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> Path:
        return self.scan_folder(dataset_id, subject_id, scan_id) / layout.IMAGE_FILE

    def dataset_log(self, dataset_id: int) -> Path:
        return self.root / layout.LOGS_DIR / qualitycheck_log_name(dataset_id)

    def labels_in_segmentation(self, dataset_id: int, subject_id: int, scan_id: int = 1) -> set:
        """Label names actually painted into the stored segmentation file."""
        return labels_in_mask(self.segmentation_file(dataset_id, subject_id, scan_id))


def _origins(segmentation) -> dict:
    """Label -> origin, from a mapping or from label names, each of origin ``source``."""
    if segmentation is None:
        return {}
    if isinstance(segmentation, dict):
        return dict(segmentation)
    return {label: "source" for label in segmentation}


def close_logging(store) -> None:
    """Release the log files a store opened.

    The audit log keeps a ``FileHandler`` per dataset for the lifetime of the process,
    which on Windows keeps the file open and blocks the temporary folder from being
    removed. Tests therefore tear the handlers down explicitly.
    """
    if store is None:
        return
    for logger in [store.audit.logger, *store.audit._dataset_loggers.values()]:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - a handler that was already closed
                pass
        logging.Logger.manager.loggerDict.pop(logger.name, None)


class QCTestCase(unittest.TestCase):
    """Base case: a temporary dataset root, torn down completely afterwards."""

    def setUp(self) -> None:
        clear_qc_env()
        self._tmp = tempfile.mkdtemp(prefix="bonehub_qc_test_")
        self.tmp_path = Path(self._tmp)
        self.dataset_root = self.tmp_path / "BoneHub_Dataset"
        self.credentials_dir = self.make_credentials_dir("credentials", TEST_SERVER_ID)
        # What the Docker image sets, so code that reads the environment finds the folder.
        os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(self.credentials_dir)
        self.state_root = self.dataset_root / ".bonehub_qc"
        self.state_dir = self.state_root / TEST_SERVER_ID
        self.builder = DatasetBuilder(self.dataset_root)
        self._stores: list = []
        self._upload_counter = itertools.count()
        self.addCleanup(self._cleanup)

    def make_credentials_dir(self, name: str, server_id: str | None = None) -> Path:
        """A credentials folder outside the dataset, as a container's volume would be."""
        folder = self.tmp_path / name
        folder.mkdir(parents=True)
        if server_id is not None:
            (folder / auth.SERVER_ID_FILE_NAME).write_text(server_id, encoding="utf-8")
        return folder

    def _cleanup(self) -> None:
        for store in self._stores:
            close_logging(store)
        clear_qc_env()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def track(self, store):
        """Register a store so its log files are closed during teardown."""
        self._stores.append(store)
        return store

    def make_store(
        self, config: QCServerConfig | None = None, credentials_dir: Path | None = None, **config_kwargs
    ) -> QCStore:
        """A server on the test dataset; ``credentials_dir`` gives another server than the default."""
        if config is None and config_kwargs:
            config = QCServerConfig(**config_kwargs)
        return self.track(
            QCStore(dataset_root=self.dataset_root, credentials_dir=credentials_dir or self.credentials_dir, config=config)
        )

    def default_dataset(self, n_scans: int = 3, dataset_id: int = 1) -> None:
        """One dataset of ``n_scans`` subjects, a scan each, whose segmentations came with the
        source and nobody has reviewed yet."""
        self.builder.add_dataset(dataset_id)
        for subject_id in range(1, n_scans + 1):
            self.builder.add_scan(dataset_id, subject_id, segmentation=["FEMUR_LEFT", "FEMUR_RIGHT"])

    def upload_path(self) -> Path:
        """A fresh path for an upload, outside the dataset folder."""
        return self.tmp_path / "uploads" / f"upload_{next(self._upload_counter)}{SEGMENTATION_SUFFIX}"

    def upload_file(self, labels, shape=SHAPE, spacing=SPACING, grown=()) -> Path:
        """A segmentation as a client would submit it, written outside the dataset folder.

        With the labels a scan was built with, it is the same as the stored one, voxel for
        voxel; ``grown`` labels are changed.
        """
        return write_mask(self.upload_path(), labels, shape, spacing, grown)

    # --- the workflow, one step at a time -----------------------------------
    def review(self, store, user, rejected=None, accepted=None, missing=None, comment=None):
        """``user`` leases the next scan waiting for a review and judges it, as the review
        page does: the ``rejected`` labels and the ``missing`` ones, the rest accepted unless
        ``accepted`` names the ones to accept."""
        assignment = store.next_scan(user, REVIEWER)
        return store.submit(
            assignment.assignment_id,
            user,
            True,
            None,
            confirmed_labels=accepted,
            comment=comment,
            use_stored_segmentation=True,
            rejected_labels=rejected,
            missing_labels=missing,
        )

    def edit(self, store, user, labels, grown=(), confirmed=None, comment=None):
        """``user`` leases the next scan waiting for an editor and uploads ``labels``, as 3D
        Slicer does; ``grown`` are the labels the correction changed."""
        assignment = store.next_scan(user, EDITOR)
        return store.submit(
            assignment.assignment_id,
            user,
            True,
            self.upload_file(labels, grown=grown),
            confirmed_labels=confirmed,
            comment=comment,
        )

    def dataset_state(self, dataset_id: int = 1) -> tuple:
        """Everything a verdict must leave alone until approval: the bytes of every file of the
        dataset -- each scan.json and segmentation among them -- and whether the dataset's
        quality-check log exists."""
        dataset = layout.dataset_path(self.dataset_root, dataset_id)
        files = {
            path.relative_to(self.dataset_root).as_posix(): path.read_bytes()
            for path in sorted(dataset.rglob("*"))
            if path.is_file()
        }
        return files, self.builder.dataset_log(dataset_id).exists()

    def reviewed(self, store, dataset_id: int = 1, subject_id: int = 1, scan_id: int = 1) -> set:
        """The labels of a scan that a server on the database has reviewed, as ``store`` reads them."""
        return store.reviewed_labels(scan_key(dataset_id, subject_id, scan_id))
