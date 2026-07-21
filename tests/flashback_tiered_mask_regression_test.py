"""Mask/execution parity for flashback casts of Tiered spells.

A round-7.96 training run died on a strict fidelity failure: the graveyard
flashback mask offered Thunder Magic (a Tiered instant granted flashback for
{R}) because the base {R} cost was affordable, but cast_spell rejected it with
"no legal affordable Tiered mode" — the only affordable tier ({R}) had no
legal target. The mask now shares cast_spell's CR 601.2b modal gate
(`tiered_mode_is_selectable`), so it offers the flashback only when a mode is
actually castable.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_real_card,
    inject_into_zone,
)


class FlashbackTieredMaskRegressionTest(unittest.TestCase):
    THUNDER = "Thunder Magic"  # Tiered instant, {R}; each tier targets a creature.

    def _setup(self, seed, *, with_target_creature):
        game_state = fresh(seed)
        env = get_env()
        handler = env.action_handler
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        for player in (controller, opponent):
            player["hand"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["mana_pool"] = {c: 0 for c in ("W", "U", "B", "R", "G", "C")}
        thunder = inject_real_card(
            game_state, controller, self.THUNDER, "graveyard")
        self.assertTrue(
            game_state.grant_flashback_permission(
                controller, thunder, cost="{R}"))
        # A single red source: affords only the {R} tier (tier 0).
        inject_real_card(game_state, controller, "Mountain", "battlefield")
        if with_target_creature:
            inject_into_zone(game_state, opponent, {
                "name": "Thunder Target",
                "mana_cost": "{1}", "cmc": 1,
                "type_line": "Creature - Bear",
                "oracle_text": "", "power": "2", "toughness": "2",
                "keywords": [], "color_identity": [],
            }, "battlefield")
        handler.current_valid_actions = None
        gy_index = controller["graveyard"].index(thunder)
        return game_state, handler, controller, thunder, 472 + gy_index

    def test_no_legal_tier_target_is_not_masked_and_matches_execution(self):
        # No creature anywhere: the affordable {R} tier has no legal target,
        # and the higher tiers are unaffordable. cast_spell would fail, so the
        # mask must not offer the flashback.
        game_state, handler, controller, thunder, action = self._setup(
            45201, with_target_creature=False)
        mask = handler.generate_valid_actions()
        self.assertFalse(
            mask[action],
            "mask offered a flashback whose only affordable tier has no "
            "legal target")
        # Execution parity: forcing the cast fails closed, so mask and
        # execution agree that it is uncastable.
        self.assertFalse(game_state.cast_spell(
            thunder, controller, context={
                "source_zone": "graveyard",
                "source_idx": controller["graveyard"].index(thunder),
                "flashback_cast": True, "flashback_cost": "{R}",
                "use_alt_cost": "flashback"}))

    def test_affordable_tier_with_legal_target_is_masked_and_casts(self):
        # A legal creature target exists, so the {R} tier is selectable: the
        # mask offers the flashback and execution succeeds (opens the mode
        # choice) rather than failing.
        game_state, handler, controller, thunder, action = self._setup(
            45202, with_target_creature=True)
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action],
            "mask hid a flashback with a selectable, targetable tier")
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(info.get("execution_failed"), info)
        self.assertFalse(info.get("critical_error"), info)
        self.assertIsNotNone(
            game_state.choice_context,
            "a castable Tiered flashback should open a mode choice")
        self.assertEqual(
            game_state.choice_context.get("type"), "choose_mode")


class HarmonizeTargetMaskRegressionTest(unittest.TestCase):
    """Harmonize is an alternative graveyard-cast path that shared the same
    missing target check: it verified payability but not a legal target. It
    now applies the hand-cast `_targets_available` gate."""

    def _state(self, seed):
        game_state = fresh(seed)
        env = get_env()
        handler = env.action_handler
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        for player in (controller, opponent):
            player["hand"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["tapped_permanents"] = set()
            player["mana_pool"] = {c: 0 for c in ("W", "U", "B", "R", "G", "C")}
        # A Harmonize spell that destroys a target artifact.
        harmonize = inject_into_zone(game_state, controller, {
            "name": "Harmonize Shatter", "mana_cost": "{2}{R}", "cmc": 3,
            "type_line": "Sorcery",
            "oracle_text": "Harmonize {1}\nDestroy target artifact.",
            "keywords": [], "color_identity": ["R"]}, "graveyard")
        # An untapped creature to crew the Harmonize cost, plus {1}.
        inject_into_zone(game_state, controller, {
            "name": "Harmonize Tapper", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature - Elf", "oracle_text": "",
            "power": "3", "toughness": "3",
            "keywords": [], "color_identity": []}, "battlefield")
        inject_real_card(game_state, controller, "Mountain", "battlefield")
        handler.current_valid_actions = None
        gy_index = controller["graveyard"].index(harmonize)
        return game_state, handler, controller, opponent, 472 + gy_index

    def test_harmonize_not_masked_without_a_legal_target(self):
        # No artifact on the battlefield: Harmonize is payable but the spell
        # has no legal target, so the mask must not offer it.
        game_state, handler, controller, opponent, action = self._state(47401)
        mask = handler.generate_valid_actions()
        self.assertFalse(
            mask[action],
            "Harmonize mask offered a targeted spell with no legal target")

    def test_harmonize_masked_with_a_legal_target(self):
        # An artifact exists, so the fix must still offer the Harmonize cast.
        game_state, handler, controller, opponent, action = self._state(47402)
        inject_into_zone(game_state, opponent, {
            "name": "Target Artifact", "mana_cost": "{2}", "cmc": 2,
            "type_line": "Artifact", "oracle_text": "",
            "keywords": [], "color_identity": []}, "battlefield")
        handler.current_valid_actions = None
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action], "Harmonize mask hid a cast with a legal target")


if __name__ == "__main__":
    unittest.main()
