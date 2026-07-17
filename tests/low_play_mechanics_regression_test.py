import unittest

from Playersim.ability_types import ReflectDamageEffect, SearchLibraryEffect
from Playersim.ability_utils import EffectFactory
from tests.scenario_test import (
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


def creature(name, power=3, toughness=3):
    return {
        "name": name,
        "mana_cost": "{2}",
        "cmc": 2,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "power": power,
        "toughness": toughness,
    }


def land(name):
    return {
        "name": name,
        "mana_cost": "",
        "cmc": 0,
        "type_line": "Basic Land - Forest",
        "oracle_text": "{T}: Add {G}.",
    }


class EarthbenderAscensionRegressionTest(unittest.TestCase):
    def test_parent_landfall_triggers_before_threshold(self):
        game_state = fresh(99501)
        player = game_state.p1
        ascension = inject_real_card(
            game_state, player, "Earthbender Ascension", "battlefield")
        source = game_state._safe_get_card(ascension)
        source.counters["quest"] = 0
        game_state.ability_handler.active_triggers.clear()

        entering = inject_into_zone(
            game_state, player, land("Ascension Land"), "hand")
        self.assertTrue(game_state.move_card(
            entering, player, "hand", player, "battlefield",
            cause="land_play"))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertIn("land you control enters", queued[0][0].trigger_condition)

        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(source.counters.get("quest"), 1)
        self.assertFalse(game_state.ability_handler.active_triggers)


class SurrakTargetRegressionTest(unittest.TestCase):
    def setUp(self):
        self.game_state = fresh(99510)
        self.player = self.game_state.p1
        self.opponent = self.game_state.p2
        self.surrak = inject_real_card(
            self.game_state, self.player,
            "Surrak, Elusive Hunter", "battlefield")
        self.own_creature = inject_into_zone(
            self.game_state, self.player,
            creature("Surrak Ally"), "battlefield")
        self.enemy_creature = inject_into_zone(
            self.game_state, self.opponent,
            creature("Enemy Creature"), "battlefield")
        self.own_artifact = inject_into_zone(
            self.game_state, self.player, {
                "name": "Surrak Artifact", "mana_cost": "{1}",
                "cmc": 1, "type_line": "Artifact", "oracle_text": "",
            }, "battlefield")
        self.game_state.ability_handler.active_triggers.clear()

    def _hostile_target(self, target_id, category="creatures"):
        self.game_state.notify_targets_committed(
            None, self.opponent, {category: [target_id]})
        count = len(self.game_state.ability_handler.active_triggers)
        self.game_state.ability_handler.active_triggers.clear()
        return count

    def test_union_scope_accepts_only_controlled_creatures(self):
        self.assertEqual(self._hostile_target(self.own_creature), 1)
        self.assertEqual(self._hostile_target(self.own_artifact, "artifacts"), 0)
        self.assertEqual(self._hostile_target(self.enemy_creature), 0)

        self.game_state.notify_targets_committed(
            None, self.player, {"creatures": [self.own_creature]})
        self.assertFalse(self.game_state.ability_handler.active_triggers)

    def test_opponent_targeting_controlled_creature_spell_triggers(self):
        spell = inject_into_zone(
            self.game_state, self.player,
            creature("Creature Spell Target"), "hand")
        self.player["hand"].remove(spell)
        self.game_state.add_to_stack("SPELL", spell, self.player, {})

        self.game_state.notify_targets_committed(
            None, self.opponent, {"spells": [spell]})
        self.assertEqual(
            len(self.game_state.ability_handler.active_triggers), 1)

        self.game_state.ability_handler.active_triggers.clear()
        self.game_state.notify_targets_committed(
            None, self.player, {"spells": [spell]})
        self.assertFalse(self.game_state.ability_handler.active_triggers)


class SheHulkRegressionTest(unittest.TestCase):
    def _setup_back_face(self, seed):
        game_state = fresh(seed)
        player, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        jennifer = inject_real_card(
            game_state, player,
            "Jennifer Walters // The Sensational She-Hulk", "battlefield")
        self.assertTrue(game_state.transform_card(jennifer))
        ally = inject_into_zone(
            game_state, player,
            creature("She-Hulk Ally", 5, 5), "battlefield")
        enemy = inject_into_zone(
            game_state, opponent,
            creature("She-Hulk Enemy", 5, 5), "battlefield")
        game_state.ability_handler.active_triggers.clear()
        return game_state, player, opponent, jennifer, ally, enemy

    def _stack_and_target_opponent(self, game_state, player):
        handler = get_env().action_handler
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn("p2", candidates)
        self.assertTrue(handler._handle_select_target(
            candidates.index("p2"), {})[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        return handler

    def test_control_scope_optional_choice_and_once_per_turn(self):
        game_state, player, opponent, she_hulk, ally, enemy = \
            self._setup_back_face(99520)

        game_state.apply_damage_to_permanent(enemy, 1, None)
        self.assertFalse(game_state.ability_handler.active_triggers)

        game_state.apply_damage_to_permanent(ally, 2, None)
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        handler = self._stack_and_target_opponent(game_state, player)
        self.assertEqual(
            game_state.choice_context.get("choice_kind"), "reflect_damage")
        life_before = opponent["life"]

        # Declining does not consume the once-per-turn permission.
        self.assertTrue(handler._handle_pass_priority(None)[1])
        self.assertEqual(opponent["life"], life_before)
        self.assertFalse(player.get("reflect_damage_once_each_turn", {}))

        game_state.apply_damage_to_permanent(ally, 1, None)
        handler = self._stack_and_target_opponent(game_state, player)
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertEqual(opponent["life"], life_before - 1)

        # A later trigger still stacks and chooses its target, but accepting a
        # prior trigger makes its resolution a clean no-op this turn.
        game_state.apply_damage_to_permanent(ally, 1, None)
        handler = self._stack_and_target_opponent(game_state, player)
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(opponent["life"], life_before - 1)

    def test_any_target_does_not_exclude_she_hulk(self):
        game_state, player, _opponent, she_hulk, ally, _enemy = \
            self._setup_back_face(99521)
        game_state.apply_damage_to_permanent(ally, 1, None)
        game_state.ability_handler.process_triggered_abilities()
        candidates = get_env().action_handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn(she_hulk, candidates)

        effects = EffectFactory.create_effects(
            "you may have the sensational she-hulk deal that much damage "
            "to any target. do this only once each turn.",
            source_name="The Sensational She-Hulk")
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], ReflectDamageEffect)
        self.assertTrue(effects[0].optional)
        self.assertTrue(effects[0].once_each_turn)
        self.assertFalse(effects[0].exclude_source)


class OuroboroidRegressionTest(unittest.TestCase):
    def test_uses_last_known_layered_power_after_source_leaves(self):
        game_state = fresh(99530)
        player = game_state.p1
        ouroboroid = inject_real_card(
            game_state, player, "Ouroboroid", "battlefield")
        survivor = inject_into_zone(
            game_state, player,
            creature("Ouroboroid Survivor", 2, 2), "battlefield")
        self.assertTrue(game_state.add_counter(ouroboroid, "+1/+1", 2))
        game_state.layer_system.apply_all_effects()
        game_state.ability_handler.active_triggers.clear()

        game_state.trigger_ability(
            ouroboroid, "BEGINNING_OF_COMBAT", {})
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.move_card(
            ouroboroid, player, "battlefield", player, "graveyard",
            cause="destroy"))
        self.assertEqual(
            game_state.stack[-1][3]["source_last_known"]["power"], 3)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            game_state._safe_get_card(survivor).counters.get("+1/+1"), 3)


class WorldwagonTriggerRegressionTest(unittest.TestCase):
    def test_composite_self_trigger_ignores_other_entries_and_attackers(self):
        game_state = fresh(99535)
        player = game_state.p1
        worldwagon = inject_real_card(
            game_state, player, "Lumbering Worldwagon", "battlefield")
        other = inject_into_zone(
            game_state, player,
            creature("Unrelated Entrant and Attacker"), "battlefield")
        ability = next(
            candidate
            for candidate in game_state.ability_handler.registered_abilities[
                worldwagon]
            if "this vehicle enters or attacks" in getattr(
                candidate, "trigger_condition", ""))

        base_context = {
            "game_state": game_state,
            "controller": player,
            "source_card_id": worldwagon,
            "source_card": game_state._safe_get_card(worldwagon),
        }
        self.assertFalse(ability.can_trigger(
            "ENTERS_BATTLEFIELD", {
                **base_context,
                "event_card_id": other,
                "event_card": game_state._safe_get_card(other),
                "event_controller": player,
            }))
        self.assertTrue(ability.can_trigger(
            "ENTERS_BATTLEFIELD", {
                **base_context,
                "event_card_id": worldwagon,
                "event_card": game_state._safe_get_card(worldwagon),
                "event_controller": player,
            }))
        self.assertFalse(ability.can_trigger(
            "ATTACKS", {
                **base_context,
                "event_card_id": other,
                "attacker_id": other,
            }))
        self.assertTrue(ability.can_trigger(
            "ATTACKS", {
                **base_context,
                "event_card_id": worldwagon,
                "attacker_id": worldwagon,
            }))


class SearchAndRegistrationRegressionTest(unittest.TestCase):
    def test_starfield_restricted_hidden_search_can_fail_to_find(self):
        effects = EffectFactory.create_effects(
            "Search your library for a basic Plains card or a creature card "
            "with mana value 1 or less, reveal it, put it into your hand, "
            "then shuffle.",
            source_name="Starfield Shepherd")
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], SearchLibraryEffect)
        self.assertTrue(effects[0].optional)

        game_state = fresh(99540)
        player = game_state.p1
        basic = inject_into_zone(
            game_state, player, {
                "name": "Searchable Plains", "mana_cost": "", "cmc": 0,
                "type_line": "Basic Land - Plains", "oracle_text": "",
            }, "hand")
        self.assertTrue(game_state.move_card(
            basic, player, "hand", player, "library"))
        self.assertTrue(effects[0].apply(game_state, None, player, {}))
        self.assertTrue(game_state.choice_context.get("optional"))
        game_state.agent_is_p1 = True
        self.assertTrue(get_env().action_handler.generate_valid_actions()[11])

    def test_badgermole_does_not_register_phase_skip_from_earthbend(self):
        game_state = fresh(99541)
        player = game_state.p1
        badger = inject_real_card(
            game_state, player, "Badgermole Cub", "battlefield")
        badger_effects = [
            effect for effect in game_state.replacement_effects.active_effects
            if effect.get("source_id") == badger]
        self.assertFalse([
            effect for effect in badger_effects
            if effect.get("event_type") == "PHASE_CHANGE"])

    def test_archdruids_charm_untargeted_mode_keeps_cast_action(self):
        game_state = fresh(99542)
        player = game_state.p1
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        charm = inject_real_card(
            game_state, player, "Archdruid's Charm", "hand")
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 3, "C": 0,
        }
        self.assertFalse(any(
            game_state._is_creature(card_id)
            for participant in (game_state.p1, game_state.p2)
            for card_id in participant["battlefield"]))
        hand_index = player["hand"].index(charm)
        self.assertTrue(
            get_env().action_handler.generate_valid_actions()[20 + hand_index])


if __name__ == "__main__":
    unittest.main()
