"""A scan's way through the quality check, from its first review to the administrator's approval.

Each scan a user gives a verdict on gets a *case* on this server (``models.Case``), kept in
the server's state folder together with any corrected segmentation. The dataset is left alone
until the administrator approves the case: only then is a corrected segmentation written in,
with the origins of its labels in the scan's ``scan.json``.

A case is at one *stage*::

    review     a label waits for a reviewer's verdict          -> handed to reviewers
    edit       a reviewer rejected a label                     -> handed to editors
    approval   every label is accepted, kept or removed        -> waits for the administrator
    escalated  an editor could not correct it                  -> waits for the administrator
    rejected   a reviewer rejected the scan as a whole         -> waits for the administrator
    applied    approved and written into the dataset           (finished)
    closed     closed by the administrator, nothing written    (finished, until reopened)

and each of its labels in one *state*, painted in the segmentation under review or not::

    pending    waits for a reviewer: the server queues it (see below), or an editor changed it.
               Not painted, it is an editor's removal waiting for a reviewer to agree.
    accepted   a reviewer accepted it -- or its editor, when edits need no review
    rejected   a reviewer rejected it: an editor corrects it or takes it out. Not painted, it
               was reported missing: it should be there and is not.
    removed    not in the segmentation, and nobody need look at it again; taken out of
               scan.json on approval
    kept       not under review -- reviewed already, or of an origin the server does not
               queue -- and unchanged

The BoneHub format records where each label of a scan comes from -- its *origin* in scan.json,
``source``, ``manual``, ``model:<name>@<version>`` or ``derived:<tool>@<version>`` -- but not
whether anybody reviewed it. That is the servers' own record: a label is *reviewed* when a
server on the database approved a case that left it reviewed (see :func:`reviewed_after`), and
the scan's segmentation is still the one that approval left. A painted label is under review
when its origin is one the server queues and its review state is one the server queues
(:func:`under_review`); a painted label scan.json does not list is always under review, since
nobody can say where it comes from.

A reviewer's verdict accepts or rejects each label under review, and may reject any other
too, or report a bone the segmentation lacks as missing. A rejected label needs a correction.
A reviewer may instead reject the scan as a whole, when no correction of its segmentation
would help -- the image is poor or cropped, or shows an implant: no editor sees it, its labels
stay as they were, and the administrator decides what to record about it.

An editor's upload replaces the segmentation. A label whose voxels it changed, one it added,
and one a reviewer had rejected are *edited*: they go back to ``pending`` -- or, when edits
need no review, to ``accepted`` if the editor vouches for them. A label whose voxels it changed
or added is also *drawn*: approved, it is recorded as ``manual``. A label the upload left alone
keeps its state, so a correction that spills into an accepted neighbour takes the neighbour's
acceptance away, and one that does not leaves it -- except that, when edits need no review,
one still ``pending`` is accepted if the editor vouches for it: nobody else has given it a
verdict. A label the upload takes away is removed; when edits need review, a reviewer must
agree first. So when edits need no review, a correction goes straight to approval, unless the
editor declines to vouch for a label.

The administrator may approve a case at any stage, not only once it waits for approval: the
labels a reviewer accepted then become reviewed, and the labels nobody accepted stay as they
were -- reviewed, if they were and nobody redrew them. An approval may add a remark to the
scan's remarks in scan.json, tagged ``QC: `` so that it can be told apart from the converters'
remarks, and so may closing a case, which writes nothing else -- how a rejected scan is
recorded.

Everything here works on the case alone; the store reads and writes the files.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import EDITED_ORIGIN, NOT_REVIEWED, REVIEWED, origin_matches
from .models import EDITOR, REVIEWER, Case, CaseEvent, CaseLabel, CaseRequest, FileFingerprint

REVIEW, EDIT, APPROVAL, ESCALATED, APPLIED, CLOSED = "review", "edit", "approval", "escalated", "applied", "closed"

#: The stage of a scan rejected as a whole. A label state shares the word (``REJECTED``).
REJECTED_SCAN = "rejected"

#: Stages in which a case is still in progress on this server.
OPEN_STAGES: tuple[str, ...] = (REVIEW, EDIT, APPROVAL, ESCALATED, REJECTED_SCAN)

#: Stages in which a case is finished; a closed one can be reopened.
FINISHED_STAGES: tuple[str, ...] = (APPLIED, CLOSED)

PENDING, ACCEPTED, REJECTED, REMOVED, KEPT = "pending", "accepted", "rejected", "removed", "kept"

#: The stage at which each role is handed a scan.
STAGE_OF_ROLE: dict[str, str] = {REVIEWER: REVIEW, EDITOR: EDIT}

#: Who acts for the administrator in a case's history.
ADMIN = "admin"

#: What begins a remark an approval adds to scan.json, to tell it from the converters' remarks.
REMARK_TAG = "QC:"

#: What separates the remarks of one scan in scan.json.
REMARK_SEPARATOR = "; "


class UnknownOrigin(ValueError):
    """Labels an approval would record in scan.json, but whose origin nobody can tell."""

    def __init__(self, labels: list[str]):
        super().__init__(", ".join(labels))
        self.labels = labels


# ----------------------------------------------------------------------- queries
def labels_in(case: Case, *states: str) -> list[str]:
    """The case's labels in any of these states, by name."""
    return sorted(name for name, label in case.labels.items() if label.state in states)


def painted_labels(case: Case) -> list[str]:
    """The labels in the case's segmentation."""
    return sorted(name for name, label in case.labels.items() if label.painted)


def derived_stage(case: Case) -> str:
    """The stage a case's labels and requests call for.

    An editor comes first: any label rejected, or any request open, needs a correction before
    the rest is worth reviewing again. A case with nothing painted at all needs an editor too.
    """
    states = [label.state for label in case.labels.values()]
    if REJECTED in states or case.requests or not any(label.painted for label in case.labels.values()):
        return EDIT
    if PENDING in states:
        return REVIEW
    return APPROVAL


def under_review(
    origin: str | None, reviewed: bool, eligible_origins: list[str] | None, eligible_review_states: Iterable[str]
) -> bool:
    """Whether the server queues a label of the scan's segmentation for review: its origin in
    scan.json is one of ``eligible_origins`` (None: any), and its review state one of
    ``eligible_review_states``. A label scan.json does not list (``origin`` None) always is."""
    if origin is None:
        return True
    state = REVIEWED if reviewed else NOT_REVIEWED
    return origin_matches(origin, eligible_origins) and state in set(eligible_review_states)


# ------------------------------------------------------------------ transitions
def start_case(
    scan_key: str,
    ids: tuple[int, int, int],
    origins: dict[str, str],
    painted: Iterable[str],
    reviewed: Iterable[str],
    eligible_origins: list[str] | None,
    eligible_review_states: Iterable[str],
    base: FileFingerprint | None,
    now: str,
) -> Case:
    """The case a scan begins with, from the ``origins`` scan.json gives its segmentation's
    labels, the labels its segmentation ``painted``, and those of them ``reviewed`` already.

    A painted label is under review when the server queues it (:func:`under_review`); any other
    is kept as it is. A label scan.json lists that the segmentation does not paint is removed.
    ``ids`` are the scan's dataset, subject and scan ids.
    """
    painted = list(painted)
    reviewed = sorted(set(reviewed) & set(painted))
    eligible_review_states = list(eligible_review_states)
    labels: dict[str, CaseLabel] = {}
    for name in painted:
        queued = under_review(origins.get(name), name in reviewed, eligible_origins, eligible_review_states)
        labels[name] = CaseLabel(state=PENDING if queued else KEPT)
    for name in origins:
        if name not in labels:
            labels[name] = CaseLabel(state=REMOVED, painted=False)
    dataset_id, subject_id, scan_id = ids
    case = Case(
        scan_key=scan_key,
        dataset_id=dataset_id,
        subject_id=subject_id,
        scan_id=scan_id,
        stage=REVIEW,
        labels=dict(sorted(labels.items())),
        base=base,
        reviewed_before=reviewed,
        created_at=now,
        updated_at=now,
    )
    case.stage = derived_stage(case)
    return case


def apply_review(
    case: Case,
    reviewer: str,
    accepted: Iterable[str],
    rejected: Iterable[str],
    comment: str | None,
    assignment_id: str | None,
    now: str,
) -> dict:
    """A reviewer's verdict. The caller has checked the names against the case.

    An accepted label in the segmentation is accepted; accepting one that is not agrees to its
    removal. A rejected label goes to the editors, who correct it or take it out; a rejected
    one the segmentation lacks is a bone reported missing, which the case may not have known,
    for them to add.
    """
    accepted = sorted(set(accepted))
    rejected = sorted(set(rejected))
    for name in accepted:
        label = case.labels[name]
        state = ACCEPTED if label.painted else REMOVED
        case.labels[name] = CaseLabel(
            state=state, painted=label.painted, by=reviewer, at=now, edited_by=label.edited_by, drawn=label.drawn
        )
    for name in rejected:
        label = case.labels.get(name)
        case.labels[name] = CaseLabel(
            state=REJECTED,
            painted=label is not None and label.painted,
            by=reviewer,
            at=now,
            edited_by=label.edited_by if label is not None else None,
            drawn=label is not None and label.drawn,
        )
    case.labels = dict(sorted(case.labels.items()))
    details = {
        "accepted": accepted,
        "rejected": [name for name in rejected if case.labels[name].painted],
        "missing": [name for name in rejected if not case.labels[name].painted],
    }
    _step(case, reviewer, REVIEWER, "review", derived_stage(case), comment, assignment_id, now, details)
    return details


def reject_scan(case: Case, reviewer: str, comment: str | None, assignment_id: str | None, now: str) -> dict:
    """A reviewer rejected the scan as a whole: no correction of its segmentation would help.
    Its labels stay as they were; the administrator decides what becomes of it, and what its
    scan.json says about it."""
    _step(case, reviewer, REVIEWER, "reject", REJECTED_SCAN, comment, assignment_id, now, {})
    return {}


def apply_edit(
    case: Case,
    editor: str,
    present: Iterable[str],
    changed: set[str] | None,
    vouched: Iterable[str],
    edits_need_review: bool,
    comment: str | None,
    assignment_id: str | None,
    now: str,
) -> dict:
    """An editor's corrected segmentation, which holds the labels ``present``.

    ``changed`` are the labels whose voxels the upload changed, or None when that could not
    be told, and every label counts as changed. ``vouched`` are the labels the editor vouches
    for, which counts only when edits need no review, and only for labels they edited or
    that nobody has given a verdict on. Requests are resolved: the editor has seen them,
    whatever they did about them.
    """
    present = set(present)
    vouched = set(vouched) if not edits_need_review else set()
    edited, accepted, removed = [], [], []
    for name in sorted(present):
        before = case.labels.get(name)
        # Changed by this upload; drawn by it or by an earlier one of this case.
        changed_now = before is None or not before.painted or changed is None or name in changed
        drawn = changed_now or before.drawn
        touched = changed_now or before.state == REJECTED
        if not touched:
            if before.state == PENDING and name in vouched:
                # Nobody has given it a verdict: the editor's word is the one it gets.
                case.labels[name] = CaseLabel(
                    state=ACCEPTED, by=editor, at=now, edited_by=before.edited_by, drawn=before.drawn
                )
                accepted.append(name)
            continue
        edited.append(name)
        if name in vouched:
            case.labels[name] = CaseLabel(state=ACCEPTED, by=editor, at=now, edited_by=editor, drawn=drawn)
            accepted.append(name)
        else:
            case.labels[name] = CaseLabel(state=PENDING, at=now, edited_by=editor, drawn=drawn)
    for name, before in sorted(case.labels.items()):
        if name in present:
            continue
        if before.painted:
            # Taken away: a reviewer must agree, when edits need review.
            removed.append(name)
            state = PENDING if edits_need_review else REMOVED
            case.labels[name] = CaseLabel(state=state, painted=False, by=editor if state == REMOVED else None,
                                          at=now, edited_by=editor)
        elif before.state in (REJECTED, PENDING):
            # Reported missing, or its removal waits for a reviewer, and not in the upload: a
            # reviewer must see that the editor left it out, when edits need review.
            state = PENDING if edits_need_review else REMOVED
            case.labels[name] = CaseLabel(state=state, painted=False, by=editor if state == REMOVED else None,
                                          at=now, edited_by=editor)
    case.labels = dict(sorted(case.labels.items()))
    resolved = [request.model_dump() for request in case.requests]
    case.requests = []
    case.staged = True
    case.edited_by = editor
    details = {
        "edited": edited,
        "accepted": accepted,
        "removed": removed,
        "pending": labels_in(case, PENDING),
        "resolved_requests": resolved,
    }
    _step(case, editor, EDITOR, "edit", derived_stage(case), comment, assignment_id, now, details)
    return details


def escalate(case: Case, editor: str, comment: str | None, assignment_id: str | None, now: str) -> dict:
    """An editor could not correct the scan: the administrator decides what becomes of it."""
    _step(case, editor, EDITOR, "escalate", ESCALATED, comment, assignment_id, now, {})
    return {}


def return_to_review(
    case: Case, comment: str | None, now: str, by: str = ADMIN, remark: str | None = None, remark_added: bool = False
) -> dict:
    """Send every verdict back to the reviewers: the labels someone accepted, rejected or
    removed wait for a review again, and open requests are dropped. Reopens a closed case.
    ``remark`` is as for :func:`close`."""
    returned = sorted(
        name
        for name, label in case.labels.items()
        if label.state in (ACCEPTED, REJECTED) or (label.state == REMOVED and label.by is not None)
    )
    for name in returned:
        label = case.labels[name]
        case.labels[name] = CaseLabel(
            state=PENDING, painted=label.painted, at=now, edited_by=label.edited_by, drawn=label.drawn
        )
    case.requests = []
    details = {"to": REVIEW, "labels": returned, **_remark_details(remark, remark_added)}
    _step(case, by, ADMIN, "return", derived_stage(case), comment, None, now, details)
    return details


def return_to_edit(
    case: Case, comment: str | None, now: str, by: str = ADMIN, remark: str | None = None, remark_added: bool = False
) -> dict:
    """Send the scan to the editors, with the administrator's word. Reopens a closed case.
    ``remark`` is as for :func:`close`."""
    case.requests.append(CaseRequest(by=by, role=ADMIN, at=now, comment=comment or None))
    details = {"to": EDIT, **_remark_details(remark, remark_added)}
    _step(case, by, ADMIN, "return", derived_stage(case), comment, None, now, details)
    return details


def close(
    case: Case, comment: str | None, now: str, by: str = ADMIN, remark: str | None = None, remark_added: bool = False
) -> dict:
    """Finish the case without writing its labels or segmentation into the dataset: only the
    ``remark`` asked for, added to the scan's remarks or found there already."""
    details = _remark_details(remark, remark_added)
    _step(case, by, ADMIN, "close", CLOSED, comment, None, now, details)
    return details


def mark_applied(
    case: Case,
    origins: dict[str, str],
    written: dict[str, str | None],
    result: FileFingerprint | None,
    backup_path: str | None,
    now: str,
    by: str = ADMIN,
    remark: str | None = None,
    remark_added: bool = False,
    outdated: dict[str, list[str]] | None = None,
) -> dict:
    """The case was approved and written into the dataset, with the ``remark`` asked for as its
    comment: added to the scan's remarks, or found there already.

    ``origins`` are the labels of the scan's segmentation as scan.json now lists them, and
    ``written`` what the approval changed of them: label -> its new origin, None where it was
    taken out. ``result`` is the scan's segmentation as the approval left it, which the labels
    it leaves reviewed (:func:`reviewed_after`) are reviewed with. ``outdated`` are the mesh
    and NURBS labels made from a segmentation the approval changed.
    """
    reviewed = reviewed_after(case)
    details = {
        "origins": dict(sorted(origins.items())),
        "updated_labels": dict(sorted(written.items())),
        "reviewed_labels": reviewed,
        "segmentation_written": case.staged,
        "backup_path": backup_path,
        "not_accepted": labels_in(case, PENDING, REJECTED),
        "outdated": outdated or {},
        "remark": remark,
        "remark_added": remark_added,
    }
    case.applied_at = now
    case.backup_path = backup_path
    case.result = result
    case.reviewed_labels = reviewed
    case.staged = False
    _step(case, by, ADMIN, "approve", APPLIED, remark, None, now, details)
    return details


# --------------------------------------------------------------------- approval
def approval_origins(case: Case, origins: dict[str, str]) -> dict[str, str]:
    """The labels of an approved case's segmentation, each with the origin scan.json is to list
    it with, from the ``origins`` it lists now.

    A label an editor drew or changed is ``manual``; any other keeps its origin. A label no
    longer painted is left out, as scan.json lists exactly the labels of the segmentation.
    Raises :class:`UnknownOrigin` for a painted label that scan.json does not list and no editor
    drew: nobody can say where it comes from.
    """
    approved: dict[str, str] = {}
    unknown: list[str] = []
    for name, label in sorted(case.labels.items()):
        if not label.painted:
            continue
        if label.drawn:
            approved[name] = EDITED_ORIGIN
        elif name in origins:
            approved[name] = origins[name]
        else:
            unknown.append(name)
    if unknown:
        raise UnknownOrigin(unknown)
    return approved


def changed_origins(before: dict[str, str], after: dict[str, str]) -> dict[str, str | None]:
    """What an approval changes of scan.json's labels: label -> its new origin, None where it is
    taken out."""
    changes: dict[str, str | None] = {name: origin for name, origin in after.items() if before.get(name) != origin}
    changes.update({name: None for name in before if name not in after})
    return dict(sorted(changes.items()))


def reviewed_after(case: Case) -> list[str]:
    """The labels an approved case leaves reviewed: those painted that someone accepted, and those
    reviewed before the case began that no editor redrew since. A label nobody accepted in this
    case does not become reviewed, but one that was stays so."""
    return sorted(
        name
        for name, label in case.labels.items()
        if label.painted
        and (label.state == ACCEPTED or (name in case.reviewed_before and not label.drawn))
    )


def tagged_remark(text: str | None) -> str | None:
    """The administrator's remark as scan.json records it, ``QC: <text>`` on one line, or
    None when there is none. A tag typed in already is not doubled."""
    text = " ".join((text or "").split())
    if text[: len(REMARK_TAG)].casefold() == REMARK_TAG.casefold():
        text = text[len(REMARK_TAG) :].strip()
    return f"{REMARK_TAG} {text}" if text else None


def with_remark(remarks: str | None, remark: str) -> str | None:
    """A scan's ``remarks`` with ``remark`` added at the end, or None when one of them says
    so already, ignoring case and spacing. The remarks before it are never changed."""
    remarks = (remarks or "").strip()
    if not remarks:
        return remark
    if _remark_parts(remark) in _remark_parts(remarks):
        return None
    return f"{remarks}{REMARK_SEPARATOR}{remark}"


def _remark_parts(remarks: str) -> str:
    """Remarks as ``;one;two;``, in lower case with single spaces, to find one among others."""
    return ";" + ";".join(" ".join(part.split()) for part in remarks.split(";")).casefold() + ";"


# ---------------------------------------------------------------------- helpers
def _remark_details(remark: str | None, remark_added: bool) -> dict:
    """What a step records of the remark it asked for: added to the scan's remarks, or found
    there already. Nothing when there is none."""
    return {"remark": remark, "remark_added": remark_added} if remark else {}


def _step(
    case: Case,
    by: str,
    role: str,
    action: str,
    stage: str,
    comment: str | None,
    assignment_id: str | None,
    now: str,
    details: dict,
) -> None:
    """Move the case to ``stage`` and record the step in its history."""
    case.stage = stage
    case.revision += 1
    case.updated_at = now
    case.events.append(
        CaseEvent(
            at=now,
            by=by,
            role=role,
            action=action,
            stage=stage,
            comment=comment or None,
            assignment_id=assignment_id,
            details=details,
        )
    )
