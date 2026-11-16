"""The policy file and its environment overrides, which is how the container is steered."""

from __future__ import annotations

import json
import os
import unittest

from qc_server.config import (
    DEFAULT_CREDENTIALS_DIR,
    ENV_PREFIX,
    QCServerConfig,
    origin_matches,
    resolve_credentials_dir,
    resolve_dataset_root,
    resolve_state_root,
)

from tests.support import QCTestCase, clear_qc_env


class ConfigDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_qc_env()
        self.addCleanup(clear_qc_env)

    def test_defaults_match_the_intended_policy(self):
        config = QCServerConfig()
        # Labels nobody has reviewed yet, whatever their origin, are what needs reviewing.
        self.assertEqual(config.eligible_review_states, ["not_reviewed"])
        self.assertIsNone(config.eligible_origins)
        self.assertFalse(config.include_scans_without_segmentation)
        self.assertTrue(config.edits_need_review, "an editor's correction goes back to a reviewer")
        self.assertIsNone(config.allowed_dataset_ids)
        self.assertEqual(config.assignment_strategy, "sequential")

    def test_rejects_a_review_state_that_does_not_exist(self):
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_review_states=["half_reviewed"])
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_review_states=[2])

    def test_rejects_an_empty_list_of_review_states(self):
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_review_states=[])

    def test_review_states_are_de_duplicated_and_in_order(self):
        config = QCServerConfig(eligible_review_states=["reviewed", "not_reviewed", "reviewed"])
        self.assertEqual(config.eligible_review_states, ["not_reviewed", "reviewed"])

    def test_origins_are_kinds_models_or_full_origins(self):
        config = QCServerConfig(eligible_origins=["model", " source", "model:TotalSegmentator@2.4.0", "model"])
        self.assertEqual(config.eligible_origins, ["model", "model:TotalSegmentator@2.4.0", "source"])
        for wrong in (["models"], ["model:"], ["source:x"], ["manual@1"], []):
            with self.subTest(origins=wrong), self.assertRaises(ValueError):
                QCServerConfig(eligible_origins=wrong)

    def test_an_origin_matches_its_kind_its_model_and_itself(self):
        origin = "model:TotalSegmentator@2.4.0"
        self.assertTrue(origin_matches(origin, None))
        self.assertTrue(origin_matches(origin, ["model"]))
        self.assertTrue(origin_matches(origin, ["model:TotalSegmentator"]))
        self.assertTrue(origin_matches(origin, [origin]))
        self.assertFalse(origin_matches(origin, ["model:TotalSeg"]))
        self.assertFalse(origin_matches(origin, ["source", "manual"]))
        self.assertTrue(origin_matches("manual", ["source", "manual"]))
        self.assertFalse(origin_matches(None, ["source"]), "a label scan.json does not list has no origin")

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            QCServerConfig(not_a_real_field=1)

    def test_lease_ttl_has_a_floor(self):
        with self.assertRaises(ValueError):
            QCServerConfig(lease_ttl_seconds=5)


class ConfigPersistenceTests(QCTestCase):
    def test_save_then_load_round_trips(self):
        path = self.tmp_path / "config.json"
        original = QCServerConfig(
            eligible_review_states=["not_reviewed", "reviewed"],
            eligible_origins=["model", "source"],
            allowed_dataset_ids=[3, 7],
            assignment_strategy="random",
            edits_need_review=False,
        )
        original.save(path)
        self.assertEqual(QCServerConfig.load(path).model_dump(), original.model_dump())

    def test_a_setting_an_older_server_saved_is_dropped_on_loading(self):
        """A server updated in place still starts on the config.json its old version wrote."""
        path = self.tmp_path / "config.json"
        path.write_text(
            json.dumps({"lease_ttl_seconds": 600, "max_concurrent_assignments_per_user": 3}), encoding="utf-8"
        )
        config = QCServerConfig.load(path)
        self.assertEqual(config.lease_ttl_seconds, 600)
        config.save(path)
        self.assertNotIn("max_concurrent_assignments_per_user", json.loads(path.read_text(encoding="utf-8")))

    def test_load_of_a_missing_file_gives_defaults(self):
        config = QCServerConfig.load(self.tmp_path / "nope.json")
        self.assertEqual(config.model_dump(), QCServerConfig().model_dump())

    def test_save_is_atomic_and_leaves_no_temporary_file(self):
        path = self.tmp_path / "state" / "config.json"
        QCServerConfig().save(path)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        json.loads(path.read_text(encoding="utf-8"))


class ConfigEnvironmentOverrideTests(QCTestCase):
    """``BONEHUB_QC_*`` wins over the stored file, which is how docker-compose configures."""

    def test_int_bool_and_list_overrides_are_parsed(self):
        path = self.tmp_path / "config.json"
        QCServerConfig().save(path)

        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = "600"
        os.environ[f"{ENV_PREFIX}EDITS_NEED_REVIEW"] = "false"
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "1, 4, 9"
        os.environ[f"{ENV_PREFIX}ASSIGNMENT_STRATEGY"] = "random"

        config = QCServerConfig.load(path)
        self.assertEqual(config.lease_ttl_seconds, 600)
        self.assertFalse(config.edits_need_review)
        self.assertEqual(config.allowed_dataset_ids, [1, 4, 9])
        self.assertEqual(config.assignment_strategy, "random")

    def test_allowed_dataset_ids_can_be_cleared_back_to_every_dataset(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(allowed_dataset_ids=[1]).save(path)
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "none"
        self.assertIsNone(QCServerConfig.load(path).allowed_dataset_ids)

    def test_an_empty_variable_is_ignored_rather_than_parsed(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(lease_ttl_seconds=999).save(path)
        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = ""
        self.assertEqual(QCServerConfig.load(path).lease_ttl_seconds, 999)

    def test_review_states_and_origins_are_lists(self):
        path = self.tmp_path / "config.json"
        os.environ[f"{ENV_PREFIX}ELIGIBLE_REVIEW_STATES"] = "reviewed"
        os.environ[f"{ENV_PREFIX}ELIGIBLE_ORIGINS"] = "model:TotalSegmentator, source"
        config = QCServerConfig.load(path)
        self.assertEqual(config.eligible_review_states, ["reviewed"])
        self.assertEqual(config.eligible_origins, ["model:TotalSegmentator", "source"])

    def test_a_setting_of_the_label_statuses_stops_the_server_with_what_replaced_it(self):
        """A .env written for BoneHub data schema 0.3 must not be half obeyed: a second-review
        server would silently review the labels nobody has reviewed instead."""
        path = self.tmp_path / "config.json"
        for name, said in (
            ("ELIGIBLE_LABEL_VALUES", "ELIGIBLE_REVIEW_STATES"),
            ("INCLUDE_SUBJECTS_WITHOUT_SEGMENTATION", "INCLUDE_SCANS_WITHOUT_SEGMENTATION"),
            ("MARK_REMOVED_LABELS_ABSENT", "scan.json"),
        ):
            with self.subTest(variable=name):
                clear_qc_env()
                os.environ[f"{ENV_PREFIX}{name}"] = "2"
                with self.assertRaises(ValueError) as ctx:
                    QCServerConfig.load(path)
                self.assertIn(f"{ENV_PREFIX}{name}", str(ctx.exception))
                self.assertIn(said, str(ctx.exception))
        clear_qc_env()
        os.environ[f"{ENV_PREFIX}ELIGIBLE_LABEL_VALUES"] = ""
        QCServerConfig.load(path)  # left blank, as an old .env.example has it: no harm done

    def test_falsey_words_all_turn_a_flag_off(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(edits_need_review=True).save(path)
        for word in ["false", "0", "no", "off"]:
            os.environ[f"{ENV_PREFIX}EDITS_NEED_REVIEW"] = word
            self.assertFalse(QCServerConfig.load(path).edits_need_review, word)


class PathResolutionTests(QCTestCase):
    def test_dataset_root_comes_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        self.assertEqual(resolve_dataset_root(), self.dataset_root)

    def test_a_missing_dataset_root_is_a_clear_startup_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_dataset_root()
        self.assertIn("DATASET_ROOT", str(ctx.exception))

    def test_state_root_defaults_to_a_hidden_folder_inside_the_dataset(self):
        self.assertEqual(resolve_state_root(self.dataset_root), self.dataset_root / ".bonehub_qc")

    def test_state_root_name_is_overridable(self):
        os.environ[f"{ENV_PREFIX}STATE_DIR_NAME"] = "_qc"
        self.assertEqual(resolve_state_root(self.dataset_root), self.dataset_root / "_qc")

    def test_the_credentials_folder_comes_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(self.tmp_path / "keys")
        self.assertEqual(resolve_credentials_dir(), self.tmp_path / "keys")

    def test_the_credentials_folder_defaults_to_the_containers_own(self):
        del os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"]
        self.assertEqual(resolve_credentials_dir().as_posix(), DEFAULT_CREDENTIALS_DIR)


if __name__ == "__main__":
    unittest.main()
