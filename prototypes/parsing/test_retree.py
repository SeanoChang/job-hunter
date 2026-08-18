#!/usr/bin/env python3
"""Unit tests for the deterministic parts of the tier-2 re-treeing pass."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retree  # noqa: E402


class ValidateJudgeOutput(unittest.TestCase):
    def test_valid_and_tree_passes(self):
        out = {"op": "AND", "atoms": [
            {"kind": "trait", "target": None, "text": "communicate clearly"},
            {"kind": "trait", "target": None, "text": "scope vague asks"}],
            "confidence": 0.9, "notes": ""}
        self.assertEqual(retree.validate(out), [])

    def test_single_with_two_atoms_is_error(self):
        out = {"op": "SINGLE", "atoms": [
            {"kind": "trait", "target": None, "text": "a"},
            {"kind": "trait", "target": None, "text": "b"}], "confidence": 0.5}
        self.assertTrue(any("SINGLE" in e for e in retree.validate(out)))

    def test_trait_must_not_carry_target(self):
        out = {"op": "SINGLE", "atoms": [
            {"kind": "trait", "target": "skill:python", "text": "loves python"}],
            "confidence": 0.5}
        self.assertTrue(any("target" in e for e in retree.validate(out)))

    def test_skill_target_must_be_known_concept(self):
        out = {"op": "SINGLE", "atoms": [
            {"kind": "skill", "target": "skill:made-up-thing", "text": "x"}],
            "confidence": 0.5}
        self.assertTrue(any("unknown" in e for e in retree.validate(out)))

    def test_known_skill_and_family_pass(self):
        out = {"op": "OR", "atoms": [
            {"kind": "family", "target": "family:python-async",
             "exemplars": ["skill:trio"], "open_class": False, "text": "async Python"},
            {"kind": "skill", "target": "skill:api-design", "text": "API design"}],
            "confidence": 0.8}
        self.assertEqual(retree.validate(out), [])

    def test_gold_id_format(self):
        self.assertEqual(retree.gold_id("greenhouse", "5186067008", 19), "gh:5186067008:n19")
        self.assertEqual(retree.gold_id("ashby", "4e64ab86-4e30-403b", 23), "as:4e64ab86:n23")


if __name__ == "__main__":
    unittest.main(verbosity=1)
