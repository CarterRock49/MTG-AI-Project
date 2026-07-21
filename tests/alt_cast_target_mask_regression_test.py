"""Mask/execution parity across the alternative-cost cast paths.

Two successive round-7.96 runs died on dormant mask/execution divergences
(flashback of a Tiered spell; a counterspell cast from exile) whose root was
one design gap: the alternative cast paths offered a spell on affordability
alone, without the target/mode legality the hand path enforces. Every cast
generator now routes through the shared `_cast_is_legal_for_mask` predicate.
These scenarios pin the representative hand alternative-cost mechanics (Warp,
Delve); Jump-start, Escape, Madness, and Emerge share the identical guard.
"""

from __future__ import annotations

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
    inject_into_zone,
    inject_real_card,
)

WARP_SLOTS = (296, 297, 298, 309, 310, 311, 312, 313)


class AltCastTargetMaskRegressionTest(unittest.TestCase):
    def _clean(self, seed):
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
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _warp_offered(self, handler):
        return any(
            handler.current_valid_actions[a]
            and handler.action_reasons_with_context.get(a, {}).get(
                "context", {}).get("warp_cast")
            for a in WARP_SLOTS)

    def test_warp_targeted_spell_masked_only_with_a_legal_target(self):
        game_state, handler, controller, opponent = self._clean(48001)
        inject_into_zone(game_state, controller, {
            "name": "Warp Bolt", "mana_cost": "{2}{R}", "cmc": 3,
            "type_line": "Sorcery",
            "oracle_text": ("Warp {R} (You may cast this card from your hand "
                            "for its warp cost.)\nWarp Bolt deals 3 damage to "
                            "target creature."),
            "keywords": [], "color_identity": ["R"]}, "hand")
        inject_real_card(game_state, controller, "Mountain", "battlefield")

        handler.current_valid_actions = handler.generate_valid_actions()
        self.assertFalse(
            self._warp_offered(handler),
            "Warp offered a targeted spell with no legal target")

        inject_into_zone(game_state, opponent, {
            "name": "Warp Target", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature - Bear", "oracle_text": "",
            "power": "2", "toughness": "2",
            "keywords": [], "color_identity": []}, "battlefield")
        handler.current_valid_actions = handler.generate_valid_actions()
        self.assertTrue(
            self._warp_offered(handler),
            "Warp hid a targeted spell that has a legal target")

    def test_delve_targeted_spell_masked_only_with_a_legal_target(self):
        game_state, handler, controller, opponent = self._clean(48002)
        inject_into_zone(game_state, controller, {
            "name": "Delve Bolt", "mana_cost": "{4}{B}", "cmc": 5,
            "type_line": "Sorcery",
            "oracle_text": ("Delve (Each card you exile from your graveyard "
                            "while casting this spell pays for {1}.)\nDestroy "
                            "target creature."),
            "keywords": [], "color_identity": ["B"]}, "hand")
        for _ in range(4):
            inject_into_zone(game_state, controller, {
                "name": "GY Fuel", "mana_cost": "{1}", "cmc": 1,
                "type_line": "Sorcery", "oracle_text": "",
                "keywords": [], "color_identity": []}, "graveyard")
        for _ in range(5):
            inject_real_card(game_state, controller, "Swamp", "battlefield")

        handler.current_valid_actions = handler.generate_valid_actions()
        self.assertFalse(
            handler.current_valid_actions[404],
            "Delve offered a targeted spell with no legal target")

        inject_into_zone(game_state, opponent, {
            "name": "Delve Target", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature - Bear", "oracle_text": "",
            "power": "2", "toughness": "2",
            "keywords": [], "color_identity": []}, "battlefield")
        handler.current_valid_actions = handler.generate_valid_actions()
        self.assertTrue(
            handler.current_valid_actions[404],
            "Delve hid a targeted spell that has a legal target")


if __name__ == "__main__":
    unittest.main()
