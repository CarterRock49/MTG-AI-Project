"""Focused stack-to-zone conservation regressions.

Run from the repository root with::

    python tests/stack_integrity_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from scenario_test import fresh, inject_real_card  # noqa: E402


class StackIntegrityTest(unittest.TestCase):
    def test_permanent_trigger_modes_are_not_creature_spell_modes(self):
        game_state = fresh(181_929)
        controller = game_state.p1
        game_state.agent_is_p1 = True
        spell_id = inject_real_card(
            game_state, controller, "Cosmogrand Zenith", "hand")
        controller["mana_pool"] = {
            "W": 1, "U": 0, "B": 0, "R": 0, "G": 0, "C": 2,
        }
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0

        self.assertTrue(game_state.cast_spell(
            spell_id, controller, {
                "source_zone": "hand",
                "source_idx": controller["hand"].index(spell_id),
            }))
        self.assertFalse(
            game_state.choice_context
            and game_state.choice_context.get("type") == "choose_mode",
            "Cosmogrand's triggered-ability modes were requested while casting")
        self.assertTrue(game_state.stack)
        self.assertEqual(game_state.stack[-1][1], spell_id)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(spell_id, controller["battlefield"])
        self.assertNotIn(spell_id, controller["graveyard"])
        self.assertEqual(game_state._physical_occurrence_count(spell_id), 1)


if __name__ == "__main__":
    unittest.main()
