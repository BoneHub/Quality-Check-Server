"""A reliability study of the reviewers, on a server of its own.

A server started with ``BONEHUB_QC_MODE=study`` runs no quality check. It hands the same
subjects to several reviewers -- the *raters* -- each of them several times, and measures how
far their verdicts agree:

* **intra-rater** reliability: does a rater give a bone the same verdict when they read it
  again?
* **inter-rater** reliability: do different raters give a bone the same verdict?

The administrator fixes the study in the admin panel (:mod:`.admin`): its subjects, typed in
or picked at random with a seed; its raters; how often each reads each subject; and the gap
between two readings of one subject. At Start the server copies the subjects' segmentations
into its own folder, so every reading sees the very same file, gives each rater a code that
stands for them in the results, and fixes each rater's list (:mod:`.schedule`).

Raters work on the review page as reviewers do, through the same endpoints (:mod:`.api`).
Each reading is blind -- no history, no earlier verdict, and the subject's id only if the
study shows it -- and gives every bone one of two verdicts, accept or reject. A rater holds
one reading at a time, the next in their list; several raters can read one subject at once.

The results (:mod:`.reliability`, :mod:`.report`) are % agreement, Krippendorff's alpha and
Gwet's AC1 with 95% bootstrap intervals, as a report with figures and as CSV files, with
codes in place of names throughout.

A study server never writes into the dataset, which it can mount read-only. What it keeps
besides its credentials is in the credentials volume too::

    /var/lib/bonehub-qc/state/<server id>/
    |-- (session.json, config.json, server.log, submissions.jsonl: as a QC server's)
    `-- study/
        |-- study.json        the settings, and from Start the subjects, codes and lists
        |-- held.json         the reading each rater has open
        |-- readings.jsonl    every reading submitted, a line each
        `-- segmentations/    the copies of the study subjects' segmentations

so a quality-check server on the same dataset never sees it.
"""
