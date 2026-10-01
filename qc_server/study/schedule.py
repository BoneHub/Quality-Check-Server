"""Which subjects a study is about, the codes that stand for its raters, and each rater's list.

Everything random here is drawn from the study's seed, each draw from a stream of its own --
the pick of subjects, the codes, and each rater's order -- so the same settings on the same
dataset always give the same study, and changing one rater does not reshuffle the others.

A rater reads the study's subjects in rounds: in each round every subject once, in a random
order. Their list is the rounds one after another. A subject read at the end of one round
could come straight back at the start of the next, and the rater would remember their first
verdict. So each round after the first keeps a *gap*: between a rater's two readings of a
subject there are at least that many readings of other subjects. The gap can be at most the
number of subjects minus one; with that, every round repeats the order of the first.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable

from bonehub_data_schema.bonehub_dataset_io import DATASET_ZFILL, SUBJECT_ZFILL

_SUBJECT_KEY = re.compile(r"^\s*(\d+)\s*[_/-]\s*(\d+)\s*$")


def normalized_subject_key(text: str) -> str:
    """A subject key as the dataset writes it, '001_000012', from '001_000012' or '1_12'."""
    match = _SUBJECT_KEY.match(str(text))
    if match is None:
        raise ValueError(f"'{text}' is not a subject id such as 001_000012 (dataset 1, subject 12)")
    dataset_id, subject_id = (int(part) for part in match.groups())
    return f"{str(dataset_id).zfill(DATASET_ZFILL)}_{str(subject_id).zfill(SUBJECT_ZFILL)}"


def subject_ids(subject_key: str) -> tuple[int, int]:
    """``(dataset_id, subject_id)`` of a subject key."""
    dataset_id, subject_id = normalized_subject_key(subject_key).split("_")
    return int(dataset_id), int(subject_id)


def pick_subjects(
    candidates: Iterable[str], count: int, seed: int, problem_of: Callable[[str], str | None]
) -> tuple[list[str], list[tuple[str, str]]]:
    """A random pick of ``count`` subjects among ``candidates``, each of them fit for the study.

    The candidates are shuffled with the seed and taken in that order; one that
    ``problem_of`` finds a problem with is skipped, with the problem. Returns the picked
    subjects, sorted, and the skipped ones. Fewer than ``count`` are picked when the
    candidates run out.
    """
    order = sorted(set(candidates))
    random.Random(f"{seed}:subjects").shuffle(order)
    picked: list[str] = []
    skipped: list[tuple[str, str]] = []
    for key in order:
        if len(picked) == count:
            break
        problem = problem_of(key)
        if problem:
            skipped.append((key, problem))
        else:
            picked.append(key)
    return sorted(picked), skipped


def code_of(index: int) -> str:
    """The code of the rater at ``index``: A, B, ..., Z, then AA, AB, ..."""
    code = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        code = chr(ord("A") + rest) + code
    return code


def rater_codes(names: Iterable[str], seed: int) -> dict[str, str]:
    """``{name: code}``: the codes handed out in a random order, so that no code says anything
    about the name behind it -- not even where it comes in the alphabet."""
    order = sorted(set(names))
    random.Random(f"{seed}:codes").shuffle(order)
    return {name: code_of(index) for index, name in enumerate(order)}


def default_gap(n_subjects: int) -> int:
    """Half the number of study subjects."""
    return n_subjects // 2


def gap_problem(gap: int, n_subjects: int, readings: int) -> str | None:
    """Why a rater's list cannot keep this gap, or None when it can."""
    if readings < 2 or gap <= n_subjects - 1:
        return None
    return (
        f"A minimum gap of {gap} cannot be kept with {n_subjects} study subject(s): there are only "
        f"{n_subjects - 1} other subject(s) to put between two readings of one. Use a gap of at most "
        f"{n_subjects - 1}, or more subjects."
    )


def reading_orders(
    subject_keys: Iterable[str], codes: dict[str, str], readings: int, gap: int, seed: int
) -> dict[str, list[tuple[str, int]]]:
    """Each rater's list, by name: ``(subject key, reading number of that subject)`` in order."""
    keys = sorted(set(subject_keys))
    return {
        name: reading_order(keys, readings, gap, random.Random(f"{seed}:order:{code}"))
        for name, code in sorted(codes.items())
    }


def reading_order(keys: list[str], readings: int, gap: int, rng: random.Random) -> list[tuple[str, int]]:
    """``readings`` rounds of ``keys``, each in a random order, keeping ``gap`` between the
    readings of a subject in consecutive rounds."""
    problem = gap_problem(gap, len(keys), readings)
    if problem:
        raise ValueError(problem)
    order: list[tuple[str, int]] = []
    previous: dict[str, int] | None = None
    for reading in range(1, readings + 1):
        if previous is None:
            this_round = list(keys)
            rng.shuffle(this_round)
        else:
            this_round = _next_round(keys, previous, gap, rng)
        previous = {key: position for position, key in enumerate(this_round)}
        order.extend((key, reading) for key in this_round)
    return order


def _next_round(keys: list[str], previous: dict[str, int], gap: int, rng: random.Random) -> list[str]:
    """A random order of ``keys`` that puts each at least ``gap`` readings after its place in
    the previous round.

    A subject at position p of the previous round (from 0) has n - 1 - p readings after it
    there, so it can come no earlier than gap - (n - 1 - p) here. Positions are filled from the
    last: each with a subject drawn at random, except where a subject can go no later, which
    then takes it. Two subjects never share that last position, since it differs with p, so
    this always succeeds.
    """
    n = len(keys)
    earliest = {key: max(0, gap - (n - 1 - previous[key])) for key in keys}
    remaining = sorted(keys)
    this_round: list[str] = [""] * n
    for position in range(n - 1, -1, -1):
        due = [key for key in remaining if earliest[key] >= position]
        key = due[0] if due else rng.choice(remaining)
        this_round[position] = key
        remaining.remove(key)
    return this_round
