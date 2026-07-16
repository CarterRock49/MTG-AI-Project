"""Pending decision contexts must survive phase transitions.

Regression for the round-7.91 run-killer (2026-07-16, timestep ~223k): a
combat-damage trigger fetched Multiversal Passage onto the battlefield, the
entry opened an as-enters choice (phase CHOOSE, previous_priority_phase =
COMBAT_DAMAGE), and _finish_damage_step then overwrote gs.phase with
END_OF_COMBAT.  Choice actions are routed by phase, so the pending
as_enters_pay_life decision became unreachable: only PASS_PRIORITY stayed
mask-valid, nothing progressed, and the strict trainer correctly aborted the
run on a period-1 policy cycle.

Two layers are pinned here:
1. Root cause — the combat damage step must defer its phase transition when
   a decision context is pending, routing it through previous_priority_phase.
2. Self-heal — mask generation must restore the decision phase (and its
   chooser's priority) if any writer ever orphans a pending context again,
   so one engine edge case can no longer end a training run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_into_zone  # noqa: E402


def stage_pay_life_choice(gs, player, previous_phase):
    """Recreate the mid-combat Multiversal Passage entry state exactly as
    move_card and complete_as_enters_choice(basic_land_type) leave it."""
    land_id = inject_into_zone(gs, player, {
        "name": "Passage Under Test", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "oracle_text": (
            "As this land enters, choose a basic land type. "
            "Then you may pay 2 life. If you don't, it enters tapped."),
        "color_identity": [],
    }, "battlefield")
    gs.previous_priority_phase = previous_phase
    gs.phase = gs.PHASE_CHOOSE
    gs.choice_context = {
        "type": "as_enters_pay_life",
        "player": player,
        "card_id": land_id,
        "source_id": land_id,
        "resolved": False,
        "options": ["pay_2_life", "decline"],
        "enter_context": {},
    }
    gs.priority_player = player
    gs.priority_pass_count = 0
    return land_id


class FinishDamageStepChoiceTest(unittest.TestCase):
    def test_regular_damage_step_defers_around_a_pending_choice(self):
        gs = fresh(97200)
        env = get_env()
        combat = env.action_handler.combat_handler
        active = gs.p1
        gs.agent_is_p1 = True
        gs.combat_damage_dealt = True
        land_id = stage_pay_life_choice(gs, active, gs.PHASE_COMBAT_DAMAGE)

        self.assertTrue(combat._finish_damage_step(gs.PHASE_COMBAT_DAMAGE))
        self.assertEqual(
            gs.phase, gs.PHASE_CHOOSE,
            "the damage step overwrote a pending decision phase")
        self.assertEqual(
            gs.previous_priority_phase, gs.PHASE_END_OF_COMBAT,
            "the deferred transition must resume at end of combat")
        self.assertIsNotNone(gs.choice_context)

        # Completing the choice lands in END_OF_COMBAT, not back in the
        # already-resolved damage step.
        life_before = int(active.get("life", 0))
        self.assertTrue(gs.complete_as_enters_choice(0))  # pay_2_life
        self.assertEqual(int(active.get("life", 0)), life_before - 2)
        self.assertIsNone(gs.choice_context)
        self.assertEqual(gs.phase, gs.PHASE_END_OF_COMBAT)
        self.assertNotIn(
            land_id, active.get("tapped_permanents", set()),
            "paying 2 life must not leave the land tapped")

    def test_first_strike_step_defers_around_a_pending_choice(self):
        gs = fresh(97201)
        env = get_env()
        combat = env.action_handler.combat_handler
        active = gs.p1
        gs.agent_is_p1 = True
        gs.first_strike_damage_dealt = True
        stage_pay_life_choice(gs, active, gs.PHASE_FIRST_STRIKE_DAMAGE)

        self.assertTrue(
            combat._finish_damage_step(gs.PHASE_FIRST_STRIKE_DAMAGE))
        self.assertEqual(gs.phase, gs.PHASE_CHOOSE)
        self.assertEqual(
            gs.previous_priority_phase, gs.PHASE_COMBAT_DAMAGE,
            "first-strike completion must resume at the regular damage step")


class OrphanedDecisionSelfHealTest(unittest.TestCase):
    def test_mask_restores_an_orphaned_choice_context(self):
        gs = fresh(97202)
        env = get_env()
        handler = env.action_handler
        chooser = gs.p2  # the crash had the P2 seat holding the choice
        gs.agent_is_p1 = False
        land_id = stage_pay_life_choice(gs, chooser, gs.PHASE_COMBAT_DAMAGE)

        # Simulate any future writer clobbering the phase around the pending
        # decision, exactly as _finish_damage_step used to.
        gs.phase = gs.PHASE_END_OF_COMBAT
        gs.priority_player = gs.p1
        gs.previous_priority_phase = gs.PHASE_END_OF_COMBAT

        mask = np.asarray(handler.generate_valid_actions(), dtype=bool)
        self.assertEqual(
            gs.phase, gs.PHASE_CHOOSE,
            "mask generation did not restore the orphaned decision phase")
        self.assertIs(gs.priority_player, chooser)
        self.assertTrue(
            mask[353] and mask[354],
            "the pay-life options are not reachable after the restore")
        self.assertFalse(
            mask[11], "PASS must not be offered for a mandatory choice")

        # The restored state resolves end to end: decline taps the land and
        # play resumes where the clobbering transition pointed.
        self.assertTrue(gs.complete_as_enters_choice(1))
        self.assertIn(land_id, chooser.get("tapped_permanents", set()))
        self.assertEqual(gs.phase, gs.PHASE_END_OF_COMBAT)
        self.assertIsNone(gs.choice_context)

    def test_mask_leaves_healthy_decision_phases_untouched(self):
        gs = fresh(97203)
        env = get_env()
        handler = env.action_handler
        stage_pay_life_choice(gs, gs.p1, gs.PHASE_MAIN_PRECOMBAT)
        gs.agent_is_p1 = True

        mask = np.asarray(handler.generate_valid_actions(), dtype=bool)
        self.assertEqual(gs.phase, gs.PHASE_CHOOSE)
        self.assertEqual(
            gs.previous_priority_phase, gs.PHASE_MAIN_PRECOMBAT,
            "a healthy pending choice must keep its original resume phase")
        self.assertTrue(mask[353] and mask[354])


if __name__ == "__main__":
    unittest.main(verbosity=2)
