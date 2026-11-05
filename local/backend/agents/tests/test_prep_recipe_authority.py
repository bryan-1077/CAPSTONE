import unittest

from agents.openai_prep_review import merge_prep_review_results


RECIPE = "memory_controller_impl_bundle"
WRONG_RECIPE_ERROR = (
    "Approved-recipe completeness failure (blocking): "
    f"{RECIPE} requires rtl/MemoryController.sv in filelist_genus.f. "
    "Prep must block signoff unless this build is explicitly opted out."
)


class PrepRecipeAuthorityTests(unittest.TestCase):
    def review(self, *, applicable=(), chosen="", ai_errors=(), local_errors=(), fixes=()):
        return merge_prep_review_results(
            local_review={
                "pass": not local_errors,
                "errors": list(local_errors),
                "context": {"synthesis_top_checks": {
                    "selected_top_module": "ddr4_controller_top",
                    "applicable_recipe_ids": list(applicable),
                    "chosen_recipe_id": chosen,
                    "adaptation_hints": {"project_recipe_requirements": {
                        RECIPE: {"top_modules": ["MemoryController"]},
                    }},
                }},
            },
            ai_review={
                "errors": list(ai_errors),
                "suggested_fixes": list(fixes),
                "summary": "Prep cannot be approved.",
            },
        )

    def test_unrelated_recipe_cannot_block_or_drive_repairs(self):
        result = self.review(
            ai_errors=[WRONG_RECIPE_ERROR],
            fixes=[f"Apply {RECIPE} and change the top to MemoryController."],
        )
        self.assertTrue(result["pass"])
        self.assertEqual(result["suggested_fixes"], [])
        self.assertIn(WRONG_RECIPE_ERROR, result["warnings"][0])
        self.assertNotIn("cannot be approved", result["summary"])

    def test_unnamed_recipe_cannot_invent_opt_out_requirement(self):
        result = self.review(ai_errors=["Approved recipe requires an explicit opt-out."])
        self.assertTrue(result["pass"])

    def test_authorized_recipe_findings_still_block(self):
        for authority in ({"applicable": [RECIPE]}, {"chosen": RECIPE}):
            with self.subTest(authority=authority):
                result = self.review(ai_errors=[WRONG_RECIPE_ERROR], **authority)
                self.assertFalse(result["pass"])
                self.assertIn(WRONG_RECIPE_ERROR, result["errors"])

    def test_unrelated_catalog_recipe_blocked_even_when_another_recipe_applies(self):
        result = self.review(
            applicable=["generic_interface_top_to_impl"],
            ai_errors=[WRONG_RECIPE_ERROR],
        )
        self.assertTrue(result["pass"])

    def test_real_ai_and_local_blockers_are_preserved(self):
        for source in ("ai_errors", "local_errors"):
            with self.subTest(source=source):
                error = "Missing required prep artifact: filelist_genus.f"
                result = self.review(**{source: [error]})
                self.assertFalse(result["pass"])
                self.assertIn(error, result["errors"])

    def test_local_recipe_failure_cannot_be_overruled(self):
        result = self.review(local_errors=[WRONG_RECIPE_ERROR])
        self.assertFalse(result["pass"])
        self.assertIn(WRONG_RECIPE_ERROR, result["errors"])

    def test_discarding_recipe_claim_preserves_other_ai_errors(self):
        error = "Missing required prep artifact: top_module.txt"
        result = self.review(ai_errors=[WRONG_RECIPE_ERROR, error])
        self.assertEqual(result["errors"], [error])
        self.assertFalse(result["pass"])


if __name__ == "__main__":
    unittest.main()
