import unittest

from careeyes_runtime import PetProgress


class PetProgressTests(unittest.TestCase):
    def test_new_pet_starts_with_starter_outfit_and_level_one(self):
        progress = PetProgress()
        self.assertEqual(progress.level, 1)
        self.assertEqual(progress.experience, 0)
        self.assertEqual(progress.outfit, ("scarf", "sprout"))
        self.assertEqual(progress.next_unlock, ("round_glasses", 3))

    def test_each_completed_rest_gives_experience_and_two_level_up(self):
        progress = PetProgress()
        progress.complete_rest()
        self.assertEqual(progress.level, 1)
        self.assertEqual(progress.level_experience, 10)
        self.assertEqual(progress.experience_per_level, 20)
        progress.complete_rest()
        self.assertEqual(progress.level, 2)
        self.assertEqual(progress.level_experience, 0)
        self.assertEqual(progress.experience, 20)

    def test_reference_four_rests_have_level_three_and_star_progress(self):
        progress = PetProgress(4)
        self.assertEqual(progress.level, 3)
        self.assertEqual(progress.next_unlock, ("star_pin", 6))

    def test_rewards_unlock_once_at_exact_thresholds(self):
        progress = PetProgress(5)
        self.assertFalse(progress.is_unlocked("star_pin"))
        self.assertEqual(progress.complete_rest(), ("star_pin",))
        self.assertTrue(progress.is_unlocked("star_pin"))
        self.assertEqual(progress.next_unlock, ("heart_badge", 9))
        self.assertEqual(progress.complete_rest(), ())
        progress = PetProgress(11)
        self.assertEqual(progress.complete_rest(), ("night_cap",))
        self.assertEqual(progress.next_unlock, ("moon_charm", 15))
        self.assertEqual(progress.complete_rest(), ())

    def test_locked_and_unknown_items_cannot_be_equipped(self):
        progress = PetProgress()
        for decoration in ("star_pin", "night_cap", "unknown", [], None):
            with self.subTest(decoration=decoration):
                self.assertFalse(progress.toggle_decoration(decoration))
                self.assertEqual(progress.outfit, PetProgress.DEFAULT_OUTFIT)

    def test_independent_slots_can_be_worn_together(self):
        progress = PetProgress(6)
        self.assertTrue(progress.toggle_decoration("star_pin"))
        self.assertEqual(progress.outfit, ("scarf", "sprout", "star_pin"))
        self.assertTrue(progress.toggle_decoration("scarf"))
        self.assertEqual(progress.outfit, ("sprout", "star_pin"))

    def test_headwear_replaces_only_the_other_head_item(self):
        progress = PetProgress(12, ["scarf", "sprout", "star_pin"])
        progress.toggle_decoration("night_cap")
        self.assertEqual(progress.outfit, ("scarf", "star_pin", "night_cap"))
        progress.toggle_decoration("sprout")
        self.assertEqual(progress.outfit, ("scarf", "sprout", "star_pin"))

    def test_outfit_can_be_fully_removed_and_empty_selection_restored(self):
        progress = PetProgress()
        for decoration in PetProgress.DEFAULT_OUTFIT:
            progress.toggle_decoration(decoration)
        self.assertEqual(progress.outfit, ())
        self.assertEqual(PetProgress(0, []).outfit, ())

    def test_restore_removes_invalid_locked_and_duplicate_items(self):
        progress = PetProgress(0, ["scarf", "night_cap", {}, "unknown", "scarf"])
        self.assertEqual(progress.outfit, ("scarf",))
        self.assertEqual(
            PetProgress(12, ["sprout", "scarf", "night_cap"]).outfit,
            ("scarf", "night_cap"),
        )

    def test_earned_rewards_are_not_equipped_automatically(self):
        progress = PetProgress(5, [])
        progress.complete_rest()
        self.assertTrue(progress.is_unlocked("star_pin"))
        self.assertEqual(progress.outfit, ())

    def test_level_and_unlocks_derive_only_from_completed_rests(self):
        progress = PetProgress(24, ["star_pin", "night_cap"])
        self.assertEqual(progress.level, 13)
        self.assertEqual(progress.experience, 240)
        self.assertEqual(progress.outfit, ("star_pin", "night_cap"))
        self.assertIsNone(progress.next_unlock)

    def test_invalid_runtime_progress_is_rejected(self):
        for value in (-1, True, 2.5, "6", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PetProgress(value)
        with self.assertRaises(ValueError):
            PetProgress(outfit="scarf")


if __name__ == "__main__":
    unittest.main()
