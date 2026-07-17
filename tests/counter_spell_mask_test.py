"""COUNTER_SPELL (430) mask must honor targeting riders like Spell Snare's.

Regression for the round-7.91 v2 run-stopper (2026-07-16): the mask offered
Spell Snare against an opponent's Daydream (mana value 1) because it only
substring-matched "counter target spell" and found *any* opponent spell on
the stack.  cast_spell then correctly reported 0/1 legal targets ("Counter
target spell with mana value 2."), the mask-valid action failed execution,
and strict training aborted the run.  The mask now runs the same
targeting-system validation as the cast path and aims target_spell_idx at a
spell that is actually a legal target.
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


def snare(gs, player):
    return inject_into_zone(gs, player, {
        "name": "Snare Under Test", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant",
        "oracle_text": "Counter target spell with mana value 2.",
        "color_identity": ["U"],
    }, "hand")


def opponent_spell(gs, opponent, name, mana_cost, cmc, stack_context=None):
    spell_id = inject_into_zone(gs, opponent, {
        "name": name, "mana_cost": mana_cost, "cmc": cmc,
        "type_line": "Sorcery",
        "oracle_text": "Draw a card.",
        "color_identity": ["W"],
    }, "hand")
    opponent["hand"].remove(spell_id)
    context = {
        "card_id": spell_id, "controller_id": "p2", "was_cast": True,
    }
    context.update(stack_context or {})
    gs.stack.append(("SPELL", spell_id, opponent, context))
    return spell_id


def priority_with_untapped_island(gs, player):
    land_id = inject_into_zone(gs, player, {
        "name": "Test Island", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land — Island", "oracle_text": "",
        "color_identity": ["U"],
    }, "battlefield")
    player.setdefault("tapped_permanents", set()).discard(land_id)
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.priority_pass_count = 0


class CounterSpellMaskRiderTest(unittest.TestCase):
    def _staged(self, seed, opponent_spell_cmc):
        gs = fresh(seed)
        env = get_env()
        handler = env.action_handler
        me, opp = gs.p1, gs.p2
        gs.agent_is_p1 = True
        gs.stack.clear()
        snare(gs, me)
        # The rider card must sit in a maskable hand slot.
        spell_id = opponent_spell(
            gs, opp, "Rider Bait", "{W}" if opponent_spell_cmc == 1
            else "{1}{W}", opponent_spell_cmc)
        priority_with_untapped_island(gs, me)
        mask = np.asarray(handler.generate_valid_actions(), dtype=bool)
        return gs, handler, mask, spell_id

    def test_snare_is_not_masked_against_a_mana_value_one_spell(self):
        gs, handler, mask, _ = self._staged(97300, opponent_spell_cmc=1)
        self.assertFalse(
            mask[430],
            "Spell Snare was mask-valid with no mana-value-2 spell on the "
            "stack — the exact round-7.91 v2 run-stopper")

    def test_snare_is_masked_and_executes_against_a_mana_value_two_spell(self):
        gs, handler, mask, spell_id = self._staged(
            97301, opponent_spell_cmc=2)
        self.assertTrue(
            mask[430],
            "Spell Snare must stay castable against a mana-value-2 spell")
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(430)
        self.assertFalse(info.get("execution_failed", False), info)

    def test_context_skips_illegal_spells_for_a_legal_one(self):
        gs = fresh(97302)
        env = get_env()
        handler = env.action_handler
        me, opp = gs.p1, gs.p2
        gs.agent_is_p1 = True
        gs.stack.clear()
        snare(gs, me)
        opponent_spell(gs, opp, "Cheap Bait", "{W}", 1)
        legal_id = opponent_spell(gs, opp, "Costed Bait", "{1}{W}", 2)
        priority_with_untapped_island(gs, me)

        mask = np.asarray(handler.generate_valid_actions(), dtype=bool)
        self.assertTrue(mask[430])
        generated = handler.action_reasons_with_context.get(430, {})
        target_idx = generated.get("context", {}).get("target_spell_idx")
        self.assertIsNotNone(target_idx)
        self.assertEqual(
            gs.stack[target_idx][1], legal_id,
            "the mask context must aim at the legal target, not the first "
            "opponent spell on the stack")

    def test_snare_uses_the_announced_x_in_stack_mana_value(self):
        for seed, x_value, expected in (
                (97303, 0, False), (97304, 1, True), (97305, 2, False)):
            with self.subTest(X=x_value):
                gs = fresh(seed)
                env = get_env()
                handler = env.action_handler
                me, opp = gs.p1, gs.p2
                gs.agent_is_p1 = True
                gs.stack.clear()
                snare(gs, me)
                opponent_spell(
                    gs, opp, "Variable Bait", "{X}{U}", 1,
                    {"X": x_value})
                priority_with_untapped_island(gs, me)

                mask = np.asarray(
                    handler.generate_valid_actions(), dtype=bool)
                self.assertEqual(
                    bool(mask[430]), expected,
                    "Spell Snare must use printed mana value plus the value "
                    "chosen for X while the spell is on the stack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
