"""Mask/execution parity for casting targeted spells from exile.

A round-7.96 evaluation died on a strict fidelity failure: the exile-cast
mask offered No More Lies (a counterspell exiled with an "ordinary" cast
permission) because it was affordable, but the stack was empty, so cast_spell
rejected it ("Not enough valid targets: 0/1"). Unlike the hand-cast path, the
exile-cast mask never checked target availability. It now shares the same
`_targets_available` and modal-mode gate the other cast paths use, so an
exile cast is offered only when the spell can actually be cast.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_into_zone  # noqa: E402


class ExileCastTargetMaskRegressionTest(unittest.TestCase):
    COUNTER = {
        "name": "No More Lies",
        "mana_cost": "{W}{U}", "cmc": 2,
        "type_line": "Instant",
        "oracle_text": (
            "Counter target spell unless its controller pays {3}. If that "
            "spell is countered this way, exile it instead of putting it "
            "into its owner's graveyard."),
        "keywords": [], "color_identity": ["W", "U"],
    }

    NONTARGET = {
        "name": "Exile Cantrip", "mana_cost": "{W}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "Draw a card.",
        "keywords": [], "color_identity": ["W"],
    }

    def _setup(self, seed, card_data):
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
        card_id = inject_into_zone(
            game_state, controller, dict(card_data), "exile")
        game_state.cards_castable_from_exile = {card_id}
        # {W}{U} available: a Plains and an Island.
        for data in (
            {"name": "Test Plains", "type_line": "Basic Land - Plains",
             "oracle_text": "{T}: Add {W}.", "mana_cost": "", "cmc": 0,
             "colors": [1, 0, 0, 0, 0], "keywords": [], "color_identity": []},
            {"name": "Test Island", "type_line": "Basic Land - Island",
             "oracle_text": "{T}: Add {U}.", "mana_cost": "", "cmc": 0,
             "colors": [0, 1, 0, 0, 0], "keywords": [], "color_identity": []},
        ):
            inject_into_zone(game_state, controller, data, "battlefield")
        handler.current_valid_actions = None
        handler.generate_valid_actions()
        exile_action = None
        for action, entry in handler.action_reasons_with_context.items():
            ctx = entry.get("context", {}) if isinstance(entry, dict) else {}
            if (ctx.get("source_zone") == "exile"
                    and ctx.get("card_id") == card_id):
                exile_action = action
                break
        return game_state, handler, controller, card_id, exile_action

    def test_counterspell_from_exile_not_masked_with_empty_stack(self):
        game_state, handler, controller, counter, exile_action = self._setup(
            46101, self.COUNTER)
        mask = handler.generate_valid_actions()
        # No spell to counter: the exile cast must not be offered, and forcing
        # the cast fails closed -- mask and execution agree it is uncastable.
        offered = exile_action is not None and bool(mask[exile_action])
        self.assertFalse(
            offered,
            "exile mask offered a counterspell with no spell to target")
        self.assertFalse(game_state.cast_spell(
            counter, controller, context={
                "source_zone": "exile",
                "source_idx": controller["exile"].index(counter),
                "exile_permission": "ordinary"}))

    def test_nontargeted_exile_cast_is_still_masked_and_casts(self):
        # The fix must not over-restrict: a non-targeted spell exiled with
        # cast permission is still offered and casts cleanly.
        game_state, handler, controller, cantrip, exile_action = self._setup(
            46102, self.NONTARGET)
        self.assertIsNotNone(
            exile_action, "exile mask hid an ordinary non-targeted cast")
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[exile_action])
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(exile_action)
        self.assertFalse(info.get("execution_failed"), info)
        self.assertFalse(info.get("critical_error"), info)
        self.assertEqual(game_state.stack[-1][1], cantrip)


if __name__ == "__main__":
    unittest.main()
