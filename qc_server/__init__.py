"""
BoneHub Quality Check - server.

Points at a database in the BoneHub format (``ds-NNNN/sub-NNNNNN/scan-NN``), hands its scans
out to authenticated users -- reviewers, who accept or reject each label on the browser review
page at ``/review``, and editors, who correct the rejected ones in 3D Slicer -- and keeps
their verdicts, with corrected segmentations (``.seg.nrrd``), in its own state folder. When
the administrator approves a scan, a corrected segmentation replaces the scan's, the labels
an editor drew are recorded as ``manual`` in its ``scan.json``, and the labels a reviewer
accepted count as reviewed for every server on the database.

The server's major version is the major version of the BoneHub format it reads: server 1.x
reads databases in format 1.x (see ``config.WRITTEN_FOR_FORMAT``).
"""

# Before the imports: config reads it.
__version__ = "1.0.0"
__author__ = "Hamid Alavi"

from .config import QCServerConfig  # noqa: E402
from .models import Assignment, AssignmentState, Role, SubmissionResult, User  # noqa: E402

__all__ = [
    "QCServerConfig",
    "Assignment",
    "AssignmentState",
    "Role",
    "SubmissionResult",
    "User",
]
