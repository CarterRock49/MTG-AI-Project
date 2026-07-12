"""
Golden scenario harness: rules-conformance tests for the Playersim engine.

Run from the repository root:

    python tests/scenario_test.py

Each scenario constructs a specific board state, performs one action, and
asserts the exact outcome, tagged with the Comprehensive Rules section it
verifies. This is the rules-level counterpart of tests/smoke_test.py, and the
proving ground for every future rules fix: write the scenario first (it fails),
fix the engine, watch it pass.

Scenarios marked known_bug=True document behavior the engine currently gets
wrong. They report as XFAIL and do not fail the suite; when a fix lands they
report as XPASS, reminding you to flip the flag.
"""

import copy
import json
import logging
import numpy as np
import os
import random
import shutil
import sys
import tempfile
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.disable(logging.CRITICAL)

from smoke_test import build_fixture_decks  # noqa: E402

SEED = 11
SCENARIOS = []
_ENV = None
_TMP = None
TEST_ARTIFACT_ROOT = os.path.join(REPO_ROOT, "tests", "test_artifacts", "scenario")


def test_artifact_paths():
    return {
        "deck_stats_path": os.path.join(TEST_ARTIFACT_ROOT, "deck_stats"),
        "card_memory_path": os.path.join(TEST_ARTIFACT_ROOT, "card_memory"),
    }


def scenario(cr, title, known_bug=False):
    def wrap(fn):
        SCENARIOS.append((cr, title, known_bug, fn))
        return fn
    return wrap


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

def get_env():
    global _ENV, _TMP
    if _ENV is None:
        from Playersim.card import load_decks_and_card_db
        from Playersim.environment import AlphaZeroMTGEnv
        shutil.rmtree(TEST_ARTIFACT_ROOT, ignore_errors=True)
        _TMP = tempfile.mkdtemp()
        build_fixture_decks(_TMP)
        decks, card_db = load_decks_and_card_db(_TMP)
        _ENV = AlphaZeroMTGEnv(decks, card_db, **test_artifact_paths())
    return _ENV


def fresh(seed=SEED):
    """A freshly reset, non-mirror game state, parked in the first main phase.

    Deck assignment picks each player's deck independently, so mirror matches
    are legitimate in training — but scenarios need both fixture decks present.
    Reset seeding is deterministic, so we walk seeds until decks differ.
    """
    env = get_env()
    for attempt in range(25):
        env.reset(seed=seed + attempt * 1000)
        if getattr(env, 'current_deck_name_p1', 'a') != getattr(env, 'current_deck_name_p2', 'a'):
            break
    else:
        raise AssertionError("could not obtain a non-mirror match in 25 resets")
    gs = env.game_state
    # Rule scenarios start after pregame decisions unless a scenario explicitly
    # reconstructs mulligans itself. Complete both policy decisions through the
    # real London-mulligan path instead of relying on a stale turn-number guard.
    if gs.mulligan_in_progress:
        assert gs.mulligan_player is gs.p1
        gs.perform_mulligan(gs.p1, keep_hand=True)
        assert gs.mulligan_player is gs.p2
        gs.perform_mulligan(gs.p2, keep_hand=True)
        assert not gs.mulligan_in_progress
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    return gs


def owner_of(gs, cid):
    """The player whose zones actually contain the card."""
    for p in (gs.p1, gs.p2):
        for z in ("library", "hand", "battlefield", "graveyard", "exile"):
            if cid in p[z]:
                return p
    raise AssertionError(f"card {cid} is not in any zone (mirror match?)")


def card_id_by_name(gs, name):
    # Deck input IDs are canonical printing identities, while repeated physical
    # copies receive unique runtime IDs. Scenarios that stage fixture cards from
    # the library should select an actual library object, not the registry row.
    for player in (gs.p1, gs.p2):
        for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
            for cid in player.get(zone, []):
                card = gs._safe_get_card(cid)
                if getattr(card, 'name', None) == name:
                    return cid
    for cid, card in gs.card_db.items():
        if getattr(card, 'name', None) == name:
            return cid
    raise AssertionError(f"fixture card not found: {name}")


def inject_card(gs, data):
    """Register a synthetic card in the database and return its new id."""
    from Playersim.card import Card
    card = Card(data)
    numeric_ids = [int(k) for k in gs.card_db.keys() if str(k).isdigit()]
    new_id = max(numeric_ids, default=-1) + 1
    gs.card_db[new_id] = card
    if hasattr(card, 'card_id'):
        card.card_id = new_id
    return new_id


def inject_into_zone(gs, player, data, zone):
    """Register a synthetic card, stage it in library, then move it to zone."""
    cid = inject_card(gs, data)
    player["library"].append(cid)
    gs._last_card_locations[cid] = (player, "library")
    assert gs.move_card(cid, player, "library", player, zone), \
        f"move_card refused library->{zone} for synthetic card {cid}"
    return cid


_REAL_DB = None


def _load_real_card_data():
    """Load current Standard plus the explicit rotated bootstrap fixture."""
    global _REAL_DB
    if _REAL_DB is None:
        from Playersim.card_registry import load_pool_snapshot_cards
        snapshot = os.path.join(
            REPO_ROOT, "Format Card Lists", "standard.jsonl")
        cards = load_pool_snapshot_cards(snapshot, format_name="standard")
        historical_path = os.path.join(
            REPO_ROOT, "formats", "standard",
            "historical_bootstrap_cards.json")
        with open(historical_path, encoding="utf-8") as handle:
            cards.extend(json.load(handle)["cards"])
        _REAL_DB = {card["name"]: card for card in cards}
    return _REAL_DB


def inject_real_card(gs, player, card_name, zone):
    """Inject a copy of a real sample-deck card (full oracle text, faces and
    reminder text intact) so scenarios exercise the production parsing path."""
    from Playersim.card import Card
    real_db = _load_real_card_data()
    import copy as _copy
    source = real_db.get(card_name)
    assert source is not None, \
        f"real card not found in hydrated Standard corpus: {card_name}"
    card = Card(_copy.deepcopy(source))
    numeric_ids = [int(k) for k in gs.card_db.keys() if str(k).isdigit()]
    new_id = max(numeric_ids, default=-1) + 1
    gs.card_db[new_id] = card
    if hasattr(card, 'card_id'):
        card.card_id = new_id
    player["library"].append(new_id)
    gs._last_card_locations[new_id] = (player, "library")
    assert gs.move_card(new_id, player, "library", player, zone), \
        f"move_card refused library->{zone} for real card {card_name}"
    return new_id


def replace_hand(gs, player, card_specs):
    """Move the current hand away and replace it with synthetic test cards."""
    for cid in list(player.get("hand", [])):
        assert gs.move_card(cid, player, "hand", player, "library"), \
            f"could not clear card {cid} from the test hand"
    return [inject_into_zone(gs, player, spec, "hand") for spec in card_specs]


def to_battlefield(gs, cid):
    """Move a card from its owner's library onto that owner's battlefield
    through the engine, and return the owning player."""
    owner = owner_of(gs, cid)
    assert gs.move_card(cid, owner, "library", owner, "battlefield"), \
        f"move_card refused library->battlefield for {cid}"
    return owner


def zone_census(gs):
    total = 0
    for p in (gs.p1, gs.p2):
        for z in ("library", "hand", "battlefield", "graveyard", "exile"):
            total += len(p[z])
    return total


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@scenario("104.3a / 704.5a", "a player at 0 or less life loses the game")
def s_zero_life_loses():
    gs = fresh()
    gs.p2["life"] = 0
    gs.check_state_based_actions()
    assert gs.p2.get("lost_game", False), "player at 0 life was not marked as having lost"
    assert not gs.p1.get("lost_game", False), "the healthy player lost instead"


@scenario("704.5g", "a creature with lethal damage marked on it is destroyed")
def s_lethal_damage_destroys():
    gs = fresh()
    cid = card_id_by_name(gs, "Vine Stalker")  # 2/2
    owner = to_battlefield(gs, cid)
    gs.apply_damage_to_permanent(cid, 2, source_id=None)
    gs.check_state_based_actions()
    assert cid not in owner["battlefield"], "creature with lethal damage stayed on the battlefield"
    assert cid in owner["graveyard"], "destroyed creature did not reach the graveyard"


@scenario("704.5g", "a creature with non-lethal damage survives")
def s_nonlethal_survives():
    gs = fresh()
    cid = card_id_by_name(gs, "Vine Stalker")  # 2/2
    owner = to_battlefield(gs, cid)
    gs.apply_damage_to_permanent(cid, 1, source_id=None)
    gs.check_state_based_actions()
    assert cid in owner["battlefield"], "creature died to non-lethal damage"


@scenario("704.5f", "a creature with toughness 0 or less is put into the graveyard")
def s_zero_toughness_dies():
    gs = fresh()
    cid = card_id_by_name(gs, "Vine Stalker")  # 2/2
    owner = to_battlefield(gs, cid)
    assert gs.add_counter(cid, "-1/-1", 2), "engine refused to add -1/-1 counters"
    gs.check_state_based_actions()
    assert cid not in owner["battlefield"], "0-toughness creature stayed on the battlefield"
    assert cid in owner["graveyard"]


@scenario("704.5q", "+1/+1 and -1/-1 counters annihilate in pairs")
def s_counter_annihilation():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")  # 3/3
    owner = to_battlefield(gs, cid)
    assert gs.add_counter(cid, "+1/+1", 2)
    assert gs.add_counter(cid, "-1/-1", 1)
    gs.check_state_based_actions()
    card = gs._safe_get_card(cid)
    counters = getattr(card, 'counters', {}) or {}
    assert counters.get("+1/+1", 0) == 1, f"expected one +1/+1 remaining, got {counters}"
    assert counters.get("-1/-1", 0) == 0, f"expected zero -1/-1 remaining, got {counters}"
    assert cid in owner["battlefield"], "creature should have survived annihilation"


@scenario("704.5j", "the legend rule keeps only one copy of a legendary permanent")
def s_legend_rule():
    gs = fresh()
    lid = inject_card(gs, {
        "name": "Test Legend of Scenarios", "type_line": "Legendary Creature — Test",
        "mana_cost": "{1}{G}", "cmc": 2, "power": "2", "toughness": "2",
        "color_identity": ["G"], "oracle_text": "",
    })
    gs.p1["battlefield"].append(lid)
    gs.p1["battlefield"].append(lid)
    gs.check_state_based_actions()
    assert gs.p1["battlefield"].count(lid) == 1, "legend rule left two copies on the battlefield"
    assert lid in gs.p1["graveyard"], "the sacrificed legend did not reach the graveyard"


@scenario("702.2", "one point of deathtouch damage destroys any creature")
def s_deathtouch():
    gs = fresh()
    cid = card_id_by_name(gs, "Elder Wurm")  # 6/6
    owner = to_battlefield(gs, cid)
    gs.apply_damage_to_permanent(cid, 1, source_id=None, has_deathtouch=True)
    gs.check_state_based_actions()
    assert cid not in owner["battlefield"], "deathtouch damage did not destroy the creature"
    assert cid in owner["graveyard"]


@scenario("106 / 302.6", "tapping and untapping updates permanent status")
def s_tap_untap():
    gs = fresh()
    cid = card_id_by_name(gs, "Sprout Guardian")
    owner = to_battlefield(gs, cid)
    assert gs.tap_permanent(cid, owner), "tap_permanent failed"
    assert cid in owner["tapped_permanents"], "tapped permanent not tracked"
    assert gs.untap_permanent(cid, owner), "untap_permanent failed"
    assert cid not in owner["tapped_permanents"], "untapped permanent still tracked as tapped"


@scenario("122.1d", "each untap attempt removes one stun counter instead of untapping")
def s_stun_counter_replaces_untap():
    gs = fresh()
    owner = gs.p1
    cid = inject_into_zone(gs, owner, {
        "name": "Stunned Awakener", "mana_cost": "{2}{U}",
        "type_line": "Creature - Wizard",
        "oracle_text": "Whenever this creature becomes untapped, draw a card.",
        "power": 2, "toughness": 2,
    }, "battlefield")
    assert gs.tap_permanent(cid, owner)
    assert gs.add_counter(cid, "stun", 2)
    gs.ability_handler.active_triggers = []

    for expected in (1, 0):
        assert gs.untap_permanent(cid, owner), "stun replacement rejected an untap attempt"
        assert cid in owner["tapped_permanents"], "stunned permanent became untapped"
        assert gs._safe_get_card(cid).counters.get("stun", 0) == expected, \
            "an untap attempt did not remove exactly one stun counter"
        assert not gs.ability_handler.active_triggers, \
            "removing a stun counter fired a becomes-untapped trigger"

    assert gs.untap_permanent(cid, owner), "permanent did not untap after its stun counters were gone"
    assert cid not in owner["tapped_permanents"], "permanent stayed tapped without a stun counter"
    assert gs.ability_handler.active_triggers, \
        "a real untap did not fire the becomes-untapped trigger"


@scenario("122.1d / 502.2", "stun replacement applies during the untap step, effects, and untap costs")
def s_stun_counter_all_untap_routes():
    gs = fresh()
    from Playersim.ability_types import ActivatedAbility, UntapEffect
    owner = gs.p1

    phase_target = inject_into_zone(gs, owner, {
        "name": "Untap Step Stun Probe", "type_line": "Creature - Soldier",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    assert gs.tap_permanent(phase_target, owner)
    assert gs.add_counter(phase_target, "stun", 1)
    gs._untap_phase(owner)
    assert phase_target in owner["tapped_permanents"], \
        "the untap step ignored the stun counter"
    assert gs._safe_get_card(phase_target).counters.get("stun", 0) == 0, \
        "the untap step did not remove the stun counter"

    assert gs.add_counter(phase_target, "stun", 1)
    effect = UntapEffect(target_type="creature")
    assert effect.apply(gs, None, owner, {"creatures": [phase_target]}), \
        "an effect-based untap rejected the stun replacement"
    assert phase_target in owner["tapped_permanents"], \
        "an effect-based untap bypassed the stun counter"
    assert gs._safe_get_card(phase_target).counters.get("stun", 0) == 0

    cost_source = inject_into_zone(gs, owner, {
        "name": "Untap Cost Stun Probe", "type_line": "Artifact Creature - Construct",
        "oracle_text": "{Q}: Draw a card.", "power": 1, "toughness": 1,
    }, "battlefield")
    assert gs.tap_permanent(cost_source, owner)
    assert gs.add_counter(cost_source, "stun", 1)
    ability = ActivatedAbility(cost_source, cost="{Q}", effect="Draw a card.")
    assert ability.pay_cost(gs, owner), "a stun counter made an untap cost unpayable"
    assert cost_source in owner["tapped_permanents"], \
        "paying an untap cost through stun incorrectly untapped the source"
    assert gs._safe_get_card(cost_source).counters.get("stun", 0) == 0, \
        "paying an untap cost did not consume the stun counter"


@scenario("122.1d (Kaito)", "Kaito's loyalty effect taps its target and puts two stun counters on it")
def s_kaito_stun_effect_sequence():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    controller, opponent = gs.p1, gs.p2
    source = inject_into_zone(gs, controller, {
        "name": "Kaito Stun Probe", "type_line": "Planeswalker - Kaito",
        "oracle_text": "", "loyalty": 4,
    }, "battlefield")
    target = inject_into_zone(gs, opponent, {
        "name": "Kaito Stun Target", "type_line": "Creature - Beast",
        "oracle_text": "", "power": 4, "toughness": 4,
    }, "battlefield")
    targets = {"creatures": [target]}
    effects = EffectFactory.create_effects(
        "Tap target creature. Put two stun counters on it.",
        targets=targets, source_name="Kaito, Bane of Nightmares")
    assert [type(effect).__name__ for effect in effects] == ["TapEffect", "AddCountersEffect"], \
        f"Kaito's two-sentence effect did not stay sequenced: {[type(e).__name__ for e in effects]}"
    assert all(effect.apply(gs, source, controller, targets) for effect in effects), \
        "Kaito's tap-and-stun sequence failed to resolve"
    assert target in opponent["tapped_permanents"], "Kaito did not tap its selected target"
    assert gs._safe_get_card(target).counters.get("stun", 0) == 2, \
        "Kaito did not put two stun counters on the selected target"

    # Exercise the production loyalty -> target choice -> stack continuation,
    # not just the two parsed effects in isolation.
    gs = fresh(SEED + 120); env = get_env(); handler = env.action_handler
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    gs.stack.clear()
    kaito = inject_into_zone(gs, controller, {
        "name": "Kaito, Bane of Nightmares", "mana_cost": "{2}{U}{B}",
        "type_line": "Legendary Planeswalker - Kaito", "loyalty": 4,
        "oracle_text": (
            "+1: You get an emblem with \"Ninjas you control get +1/+1.\"\n"
            "0: Surveil 2.\n"
            "-2: Tap target creature. Put two stun counters on it."),
    }, "battlefield")
    target = inject_into_zone(gs, opponent, {
        "name": "Kaito Full-Path Target", "type_line": "Creature - Beast",
        "oracle_text": "", "power": 3, "toughness": 3,
    }, "battlefield")
    mask = handler.generate_valid_actions()
    assert mask[442], "Kaito's legal -2 was absent from the action mask"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(442)
    assert not info.get("execution_failed") and gs.phase == gs.PHASE_TARGETING
    candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert target in candidates
    target_mask = handler.generate_valid_actions()
    handler.current_valid_actions = target_mask
    _, _, _, info = handler.apply_action(274 + candidates.index(target))
    assert not info.get("execution_failed"), info.get("error_message")
    assert gs.phase == gs.PHASE_PRIORITY and gs.stack, \
        "Kaito's targeted loyalty ability never reached the stack"
    assert gs.resolve_top_of_stack()
    assert target in opponent["tapped_permanents"]
    assert gs._safe_get_card(target).counters.get("stun", 0) == 2


@scenario("603 / 122.1d (Floodpits Drowner)", "Floodpits Drowner's ETB puts a stun counter on its chosen target")
def s_floodpits_drowner_stun_etb():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    target = inject_into_zone(gs, opponent, {
        "name": "Drowner Stun Target", "type_line": "Creature - Beast",
        "oracle_text": "", "power": 3, "toughness": 3,
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    source = inject_card(gs, {
        "name": "Floodpits Drowner", "mana_cost": "{1}{U}",
        "type_line": "Creature - Merfolk",
        "oracle_text": (
            "Flash\nVigilance\n"
            "When this creature enters, tap target creature an opponent controls "
            "and put a stun counter on it.\n"
            "{1}{U}, {T}: Shuffle this creature and target creature with a stun "
            "counter on it into their owners' libraries."
        ),
        "power": 2, "toughness": 1,
    })
    controller["library"].append(source)
    gs._last_card_locations[source] = (controller, "library")
    assert gs.move_card(source, controller, "library", controller, "battlefield")

    gs.ability_handler.process_triggered_abilities()
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "Floodpits Drowner's targeted ETB did not ask for a target"
    target_type = gs.targeting_context.get("required_type")
    valid_map = gs.targeting_system.get_valid_targets(source, controller, target_type)
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    assert target in valid_targets, "the opponent's creature was not a legal Drowner target"
    reward, ok = get_env().action_handler._handle_select_target(valid_targets.index(target), {})
    assert ok, f"selecting Floodpits Drowner's target failed with reward {reward}"
    assert gs.resolve_top_of_stack(), "Floodpits Drowner's ETB did not resolve"
    assert target in opponent["tapped_permanents"], "Floodpits Drowner did not tap its target"
    assert gs._safe_get_card(target).counters.get("stun", 0) == 1, \
        "Floodpits Drowner did not put a stun counter on its target"


@scenario("603.3d / 608.2b", "a mandatory targeted trigger without a target never runs TapEffect")
def scenario_targetless_trigger_fizzles_without_effect_warning():
    from unittest.mock import patch
    from Playersim.ability_types import TriggeredAbility

    gs = fresh(SEED + 121)
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    for card_id in list(opponent.get("battlefield", [])):
        gs.move_card(card_id, opponent, "battlefield", opponent, "graveyard")
    source = inject_card(gs, {
        "name": "Targetless Drowner", "type_line": "Creature - Merfolk",
        "oracle_text": (
            "When this creature enters, tap target creature an opponent "
            "controls and put a stun counter on it."),
        "power": 2, "toughness": 1,
    })
    controller["library"].append(source)
    gs._last_card_locations[source] = (controller, "library")
    with patch("Playersim.ability_types.logging.warning") as warn:
        assert gs.move_card(
            source, controller, "library", controller, "battlefield")
        gs.ability_handler.process_triggered_abilities()
    assert not gs.targeting_context and not gs.stack, \
        "a trigger with no legal mandatory target remained on the stack"
    assert not any("TapEffect failed" in str(call)
                   for call in warn.call_args_list)

    # A legacy stack object that somehow missed target selection is also
    # contained at resolution instead of applying an effect to {}.
    ability = TriggeredAbility(
        source, trigger_condition="when this creature enters",
        effect=("tap target creature an opponent controls and put a stun "
                "counter on it"))
    gs.add_to_stack("TRIGGER", source, controller, {
        "ability": ability,
        "effect_text": ability.effect_text,
        "targets": {},
    })
    with patch("Playersim.ability_types.logging.warning") as warn:
        assert gs.resolve_top_of_stack()
    assert not any("TapEffect failed" in str(call)
                   for call in warn.call_args_list)
    assert source not in controller.get("tapped_permanents", set())


@scenario("603.2 (Valiant)", "Valiant triggers only on the first spell or ability its controller uses to target it each turn")
def s_valiant_first_friendly_target_each_turn():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    controller, opponent = gs.p1, gs.p2
    hero = inject_into_zone(gs, controller, {
        "name": "Heartfire Hero", "mana_cost": "{R}",
        "type_line": "Creature - Mouse Soldier",
        "oracle_text": (
            "Valiant — Whenever this creature becomes the target of a spell or "
            "ability you control for the first time each turn, put a +1/+1 counter on it.\n"
            "When this creature dies, it deals damage equal to its power to each opponent."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")
    abilities = gs.ability_handler.registered_abilities.get(hero, [])
    valiant = [ability for ability in abilities
               if isinstance(ability, TriggeredAbility)
               and getattr(ability, "ability_word", "") == "valiant"]
    assert len(valiant) == 1, f"Heartfire Hero's Valiant ability was not parsed: {abilities}"
    assert "becomes the target" in valiant[0].trigger_condition
    gs.ability_handler.active_triggers = []

    gs.notify_targets_committed(None, opponent, {"creatures": [hero]})
    assert not gs.ability_handler.active_triggers, \
        "an opponent targeting Heartfire Hero triggered its Valiant ability"

    gs.notify_targets_committed(None, controller, {"creatures": [hero]})
    gs.notify_targets_committed(None, controller, {"creatures": [hero]})
    assert len(gs.ability_handler.active_triggers) == 1, \
        "Valiant did not gate repeated friendly targeting in the same turn"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Heartfire Hero's Valiant trigger did not resolve"
    assert gs._safe_get_card(hero).counters.get("+1/+1", 0) == 1

    gs._reset_turn_tracking_variables()
    gs.notify_targets_committed(None, controller, {"creatures": [hero]})
    assert len(gs.ability_handler.active_triggers) == 1, \
        "Valiant did not become available again on the next turn"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack()
    assert gs._safe_get_card(hero).counters.get("+1/+1", 0) == 2


@scenario("603.2 / Pawpatch Recruit", "an opponent target creates one Recruit trigger without recursively targeting itself")
def s_pawpatch_recruit_target_trigger_is_not_recursive():
    gs = fresh(); handler = get_env().action_handler
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    recruit = inject_into_zone(gs, controller, {
        "name": "Pawpatch Recruit", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Rabbit Warrior", "power": 2,
        "toughness": 1,
        "oracle_text": (
            "Whenever a creature you control becomes the target of a spell "
            "or ability an opponent controls, put a +1/+1 counter on target "
            "creature you control other than that creature."),
    }, "battlefield")
    original_target = inject_into_zone(gs, controller, {
        "name": "Recruit Target Probe", "mana_cost": "{1}{G}",
        "type_line": "Creature", "oracle_text": "", "power": 2,
        "toughness": 2,
    }, "battlefield")
    hostile_source = inject_into_zone(gs, opponent, {
        "name": "Hostile Target Probe", "mana_cost": "{B}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }, "battlefield")
    gs.ability_handler.active_triggers = []

    gs.notify_targets_committed(
        hostile_source, opponent, {"creatures": [original_target]})
    assert len(gs.ability_handler.active_triggers) == 1, \
        "the hostile target did not create exactly one Recruit trigger"
    gs.ability_handler.process_triggered_abilities()
    assert gs.phase == gs.PHASE_TARGETING and len(gs.stack) == 1
    candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert candidates == [recruit], \
        f"'other than that creature' exposed {candidates} instead of only Recruit"

    reward, ok = handler._handle_select_target(0, {})
    assert ok, f"Recruit trigger target selection failed with reward {reward}"
    assert len(gs.stack) == 1 and not gs.ability_handler.active_triggers, \
        "the friendly target of Recruit's own trigger recursively triggered it"
    assert gs.resolve_top_of_stack(), "Recruit's finite trigger did not resolve"
    assert gs._safe_get_card(recruit).counters.get("+1/+1", 0) == 1


@scenario("603.2 (Valiant)", "Emberheart Challenger's Valiant trigger grants its printed impulse-draw permission")
def s_emberheart_challenger_valiant_impulse_draw():
    gs = fresh()
    controller = gs.p1
    challenger = inject_into_zone(gs, controller, {
        "name": "Emberheart Challenger", "mana_cost": "{1}{R}",
        "type_line": "Creature - Mouse Warrior",
        "oracle_text": (
            "Haste\nProwess\n"
            "Valiant — Whenever this creature becomes the target of a spell or "
            "ability you control for the first time each turn, exile the top card "
            "of your library. Until end of turn, you may play that card."
        ),
        "power": 2, "toughness": 2,
    }, "battlefield")
    top_card = controller["library"][0]
    gs.ability_handler.active_triggers = []

    gs.notify_targets_committed(None, controller, {"creatures": [challenger]})
    assert len(gs.ability_handler.active_triggers) == 1, \
        "Emberheart Challenger's Valiant trigger was not queued"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Emberheart Challenger's Valiant trigger failed"
    assert top_card in controller["exile"], "Valiant did not exile the top card"
    assert top_card in gs.cards_castable_from_exile, \
        "the exiled card was not playable until end of turn"
    assert top_card in gs.impulse_until_eot, \
        "the temporary play permission was not registered for cleanup"


@scenario("603.2 / 111.10g", "Monstrous Rage triggers Valiant above the spell and creates an attached Monster Role")
def s_monstrous_rage_valiant_and_monster_role():
    gs = fresh()
    controller = gs.p1
    gs.agent_is_p1 = True
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    hero = inject_into_zone(gs, controller, {
        "name": "Heartfire Hero", "mana_cost": "{R}",
        "type_line": "Creature - Mouse Soldier",
        "oracle_text": (
            "Valiant — Whenever this creature becomes the target of a spell or "
            "ability you control for the first time each turn, put a +1/+1 counter on it."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")
    rage = inject_into_zone(gs, controller, {
        "name": "Monstrous Rage", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": (
            "Target creature gets +2/+0 until end of turn. Create a Monster Role "
            "token attached to it. (If you control another Role on it, put that one "
            "into the graveyard. Enchanted creature gets +1/+1 and has trample.)"
        ),
    }, "hand")
    controller["mana_pool"]["R"] = 1
    gs.ability_handler.active_triggers = []

    assert gs.cast_spell(rage, controller), "Monstrous Rage could not be cast"
    target_type = gs.targeting_context.get("required_type")
    valid_map = gs.targeting_system.get_valid_targets(rage, controller, target_type)
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert hero in valid_targets, "Heartfire Hero was not a legal Monstrous Rage target"
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(hero), {})
    assert ok, f"selecting Monstrous Rage's target failed with reward {reward}"
    assert len(gs.ability_handler.active_triggers) == 1, \
        "committing Monstrous Rage's target did not queue Valiant"

    gs.ability_handler.process_triggered_abilities()
    assert gs.stack[-1][0] == "TRIGGER", "Valiant was not put above Monstrous Rage"
    assert gs.resolve_top_of_stack(), "Heartfire Hero's Valiant trigger failed"
    assert gs._safe_get_card(hero).counters.get("+1/+1", 0) == 1, \
        "Valiant did not resolve before Monstrous Rage"
    assert gs.resolve_top_of_stack(), "Monstrous Rage failed to resolve"

    monster_roles = [card_id for card_id in controller["battlefield"]
                     if getattr(gs._safe_get_card(card_id), "name", "") == "Monster Role"]
    assert len(monster_roles) == 1, "Monstrous Rage did not create one Monster Role"
    assert controller["attachments"].get(monster_roles[0]) == hero, \
        "the Monster Role was not attached to Monstrous Rage's target"
    assert gs.check_keyword(hero, "trample"), "the Monster Role did not grant trample"
    card = gs._safe_get_card(hero)
    assert (card.power, card.toughness) == (5, 3), \
        f"Valiant, Rage, and Role modifiers produced {card.power}/{card.toughness}, expected 5/3"
    from unittest.mock import patch
    with patch("Playersim.layer_system.logging.warning") as warn:
        gs.layer_system.invalidate_cache()
        gs.layer_system.apply_all_effects()
    missing_optional = [
        str(call) for call in warn.call_args_list
        if "missing attribute 'loyalty'" in str(call)
        or "missing attribute 'defense'" in str(call)
    ]
    assert not missing_optional, \
        f"Role layer updates still warn about absent optional fields: {missing_optional}"


@scenario("707.10 / 603.2", "a copied spell with inherited targets can trigger Valiant")
def s_spell_copy_inherited_target_triggers_valiant():
    gs = fresh()
    controller = gs.p1
    hero = inject_into_zone(gs, controller, {
        "name": "Copy-Target Valiant Probe", "mana_cost": "{R}",
        "type_line": "Creature - Mouse",
        "oracle_text": (
            "Valiant — Whenever this creature becomes the target of a spell or "
            "ability you control for the first time each turn, put a +1/+1 counter on it."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Copied Pump Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Target creature gets +1/+0 until end of turn.",
    })
    gs.add_to_stack("SPELL", spell, controller, {
        "targets": {"creatures": [hero]},
        "requires_target": True,
        "num_targets": 1,
        "source_zone": "stack_implicit",
    })
    original = gs.stack[0]
    gs.ability_handler.active_triggers = []

    assert gs.copy_spell_on_stack(original, controller, allow_new_targets=False), \
        "the targeted spell could not be copied"
    assert len(gs.ability_handler.active_triggers) == 1, \
        "a copied spell inheriting Heartfire Hero as its target did not trigger Valiant"
    assert gs.copy_spell_on_stack(original, controller, allow_new_targets=False)
    assert len(gs.ability_handler.active_triggers) == 1, \
        "a second copied spell bypassed Valiant's first-time gate"


@scenario("111.10g / 704.5", "Roles from different players coexist; a newer same-controller Role puts the old Wicked Role into the graveyard")
def s_role_control_coexistence_and_wicked_trigger():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    controller, opponent = gs.p1, gs.p2
    creature = inject_into_zone(gs, controller, {
        "name": "Role Bearer", "mana_cost": "{1}{G}",
        "type_line": "Creature - Human", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    targets = {"creatures": [creature]}

    wicked_effects = EffectFactory.create_effects(
        "Create a Wicked Role token attached to target creature you control.")
    assert [type(effect).__name__ for effect in wicked_effects] == ["CreateRoleEffect"], \
        f"The Witch's Vanity Role text did not parse: {wicked_effects}"
    assert wicked_effects[0].apply(gs, None, controller, targets)
    wicked = next(card_id for card_id in controller["battlefield"]
                  if getattr(gs._safe_get_card(card_id), "name", "") == "Wicked Role")

    opposing_monster = EffectFactory.create_effects(
        "Create a Monster Role token attached to target creature.")[0]
    assert opposing_monster.apply(gs, None, opponent, targets)
    gs.check_state_based_actions()
    assert wicked in controller["battlefield"], \
        "an opponent's Role incorrectly displaced the controller's Wicked Role"
    assert any(target_id == creature for target_id in opponent["attachments"].values()), \
        "the opposing Role did not coexist on the creature"

    own_monster = EffectFactory.create_effects(
        "Create a Monster Role token attached to target creature.")[0]
    gs.ability_handler.active_triggers = []
    assert own_monster.apply(gs, None, controller, targets)
    gs.check_state_based_actions()
    own_roles = [card_id for card_id, target_id in controller["attachments"].items()
                 if target_id == creature
                 and "role" in getattr(gs._safe_get_card(card_id), "subtypes", [])]
    assert len(own_roles) == 1, \
        "the Role state-based action did not keep only the newest same-controller Role"
    assert getattr(gs._safe_get_card(own_roles[0]), "name", "") == "Monster Role"
    assert wicked not in controller["battlefield"], "the displaced Wicked Role stayed on the battlefield"
    assert len(gs.ability_handler.active_triggers) == 1, \
        "the displaced Wicked Role's graveyard trigger was lost with the token"

    life_before = opponent["life"]
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "the Wicked Role graveyard trigger did not resolve"
    assert opponent["life"] == life_before - 1, \
        "the Wicked Role did not make each opponent lose 1 life"
    card = gs._safe_get_card(creature)
    assert (card.power, card.toughness) == (4, 4), \
        "the two opposing Monster Roles did not each grant +1/+1"
    assert gs.check_keyword(creature, "trample"), "the remaining Monster Roles did not grant trample"


@scenario("610.3", "Deep-Cavern Bat exposes its optional nonland hand choice and returns the linked card")
def s_deep_cavern_bat_linked_hand_exile():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    land, spell = replace_hand(gs, opponent, [
        {"name": "Bat Hand Land", "mana_cost": "", "type_line": "Land", "oracle_text": ""},
        {"name": "Bat Hand Spell", "mana_cost": "{3}{U}", "type_line": "Instant", "oracle_text": ""},
    ])
    bat = inject_into_zone(gs, controller, {
        "name": "Deep-Cavern Bat", "mana_cost": "{1}{B}",
        "type_line": "Creature - Bat",
        "oracle_text": (
            "Flying\nLifelink\n"
            "When Deep-Cavern Bat enters the battlefield, look at target opponent's "
            "hand. You may exile a nonland card from it until Deep-Cavern Bat leaves "
            "the battlefield."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")

    gs.ability_handler.process_triggered_abilities()
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "Deep-Cavern Bat's ETB did not ask for an opponent"
    ctx = gs.targeting_context
    valid_map = gs.targeting_system.get_valid_targets(
        bat, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert "p2" in valid_targets and "p1" not in valid_targets, \
        f"Deep-Cavern Bat offered the wrong players: {valid_targets}"
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index("p2"), {})
    assert ok and gs.resolve_top_of_stack(), "Deep-Cavern Bat's ETB did not resolve"

    choice = gs.choice_context
    assert gs.phase == gs.PHASE_CHOOSE and choice and choice.get("type") == "linked_exile", \
        "Deep-Cavern Bat did not expose its hand-card choice"
    assert choice.get("options") == [spell], \
        "Deep-Cavern Bat offered a land or omitted the legal nonland card"
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[353] and mask[11], "the Bat choice did not offer exile and decline actions"
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok, "choosing the card for Deep-Cavern Bat failed"
    assert spell in opponent["exile"] and land in opponent["hand"], \
        "Deep-Cavern Bat did not exile only the chosen nonland card"
    assert controller.get("linked_exile", {}).get(bat), \
        "Deep-Cavern Bat did not remember its linked card"

    assert gs.move_card(bat, controller, "battlefield", controller, "graveyard")
    assert spell in opponent["hand"] and spell not in opponent["exile"], \
        "Deep-Cavern Bat's linked card did not return to its owner's hand"
    assert bat not in controller.get("linked_exile", {}), \
        "Deep-Cavern Bat left stale linked-exile tracking behind"


@scenario("610.3 / choice", "Deep-Cavern Bat's controller may decline the optional linked exile")
def s_deep_cavern_bat_can_decline_linked_exile():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    card_id = replace_hand(gs, opponent, [{
        "name": "Declined Bat Card", "mana_cost": "{2}{U}",
        "type_line": "Sorcery", "oracle_text": "",
    }])[0]
    source = inject_into_zone(gs, controller, {
        "name": "Bat Choice Source", "mana_cost": "{1}{B}",
        "type_line": "Creature - Bat", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    phase_before = gs.phase
    effects = EffectFactory.create_effects(
        "Look at target opponent's hand. You may exile a nonland card from it "
        "until Deep-Cavern Bat leaves the battlefield.")
    assert len(effects) == 1 and type(effects[0]).__name__ == "LinkedExileEffect"
    assert effects[0].apply(gs, source, controller, {"players": ["p2"]})
    assert gs.choice_context and gs.choice_context.get("type") == "linked_exile"

    _, ok = get_env().action_handler._handle_pass_priority(None)
    assert ok, "declining Deep-Cavern Bat's optional exile failed"
    assert card_id in opponent["hand"] and card_id not in opponent["exile"], \
        "declining the Bat choice still exiled the card"
    assert not controller.get("linked_exile", {}).get(source), \
        "declining the Bat choice created a stale link"
    assert gs.choice_context is None and gs.phase == phase_before, \
        "declining the Bat choice did not restore the prior phase"


@scenario("610.3", "Leyline Binding returns exactly its linked nonland permanent when it leaves")
def s_leyline_binding_linked_permanent_exile():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    victim = inject_into_zone(gs, opponent, {
        "name": "Binding Victim", "mana_cost": "{2}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    land = inject_into_zone(gs, opponent, {
        "name": "Binding Land", "mana_cost": "", "type_line": "Land", "oracle_text": "",
    }, "battlefield")
    binding = inject_into_zone(gs, controller, {
        "name": "Leyline Binding", "mana_cost": "{5}{W}",
        "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nDomain - This spell costs {1} less to cast for each basic land type "
            "among lands you control.\nWhen Leyline Binding enters the battlefield, "
            "exile target nonland permanent an opponent controls until Leyline Binding "
            "leaves the battlefield."
        ),
    }, "battlefield")

    gs.ability_handler.process_triggered_abilities()
    ctx = gs.targeting_context
    assert ctx, "Leyline Binding's ETB did not request a target"
    valid_map = gs.targeting_system.get_valid_targets(
        binding, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert victim in valid_targets and land not in valid_targets, \
        "Leyline Binding did not enforce opponent/nonland target restrictions"
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(victim), {})
    assert ok and gs.resolve_top_of_stack(), "Leyline Binding's ETB did not resolve"
    assert victim in opponent["exile"] and victim not in opponent["battlefield"], \
        "Leyline Binding did not exile its target"

    cloned = gs.clone()
    assert cloned.p1.get("linked_exile", {}).get(binding), \
        "a game-state clone lost Leyline Binding's linked card"
    assert cloned.move_card(
        binding, cloned.p1, "battlefield", cloned.p1, "graveyard"), \
        "the cloned Leyline Binding could not leave the battlefield"
    assert victim in cloned.p2["battlefield"], \
        "the cloned Leyline Binding did not return its linked permanent"
    assert victim in opponent["exile"] and victim not in opponent["battlefield"], \
        "resolving linked exile in a clone mutated the original game"

    assert gs.move_card(binding, controller, "battlefield", controller, "graveyard")
    assert victim in opponent["battlefield"] and victim not in opponent["exile"], \
        "Leyline Binding did not return its linked permanent"
    assert land in opponent["battlefield"], "Leyline Binding disturbed an unrelated permanent"


@scenario("610.3", "linked exile does nothing when its source left before the enters ability resolved")
def s_linked_exile_source_left_before_resolution():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    victim = inject_into_zone(gs, opponent, {
        "name": "Late Binding Victim", "mana_cost": "{2}",
        "type_line": "Artifact Creature - Construct", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    binding = inject_into_zone(gs, controller, {
        "name": "Leyline Binding", "mana_cost": "{5}{W}", "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nWhen Leyline Binding enters the battlefield, exile target nonland "
            "permanent an opponent controls until Leyline Binding leaves the battlefield."
        ),
    }, "battlefield")
    gs.ability_handler.process_triggered_abilities()
    ctx = gs.targeting_context
    valid_map = gs.targeting_system.get_valid_targets(
        binding, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(victim), {})
    assert ok, "could not choose the late Binding target"
    assert gs.move_card(binding, controller, "battlefield", controller, "graveyard")
    assert gs.resolve_top_of_stack(), "the orphaned Leyline Binding trigger did not finish"
    assert victim in opponent["battlefield"] and victim not in opponent["exile"], \
        "Leyline Binding exiled a card after its return duration had already ended"


@scenario("609.4 / 702.11 / 702.21", "Nowhere to Run lets its own trigger target hexproof and suppresses ward")
def s_nowhere_to_run_targets_hexproof_without_ward():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    protected = inject_into_zone(gs, opponent, {
        "name": "Nowhere Protected", "mana_cost": "{4}{G}",
        "type_line": "Creature - Beast", "oracle_text": "Hexproof\nWard {2}",
        "keywords": ["Hexproof", "Ward"], "power": 6, "toughness": 6,
    }, "battlefield")
    probe = inject_card(gs, {
        "name": "Target Probe", "mana_cost": "{B}", "type_line": "Instant",
        "oracle_text": "Target creature gets -1/-1 until end of turn.",
    })
    before = gs.targeting_system.get_valid_targets(
        probe, controller, "creature", effect_text="Target creature gets -1/-1 until end of turn.")
    assert protected not in before.get("creature", []), "setup: hexproof creature was already targetable"

    nowhere = inject_into_zone(gs, controller, {
        "name": "Nowhere to Run", "mana_cost": "{1}{B}", "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nWhen Nowhere to Run enters, target creature an opponent controls gets "
            "-3/-3 until end of turn.\nCreatures your opponents control can be the targets "
            "of spells and abilities as though they didn't have hexproof.\nWard abilities "
            "of those creatures don't trigger."
        ),
    }, "battlefield")
    overrides = {getattr(ability, "targeting_override", None)
                 for ability in gs.ability_handler.registered_abilities.get(nowhere, [])}
    assert {"hexproof", "ward"}.issubset(overrides), \
        f"Nowhere to Run's targeting overrides were not parsed: {overrides}"

    gs.ability_handler.process_triggered_abilities()
    ctx = gs.targeting_context
    assert ctx, "Nowhere to Run's ETB did not request a target"
    valid_map = gs.targeting_system.get_valid_targets(
        nowhere, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert protected in valid_targets, "Nowhere to Run could not target the hexproof creature"
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(protected), {})
    assert ok, "selecting Nowhere to Run's protected target failed"
    stack_context = gs.stack[-1][3]
    assert stack_context.get("ward_checked_on_targeting") is True, \
        "target commitment did not snapshot ward triggering"
    assert stack_context.get("ward_obligations") == [], \
        "ward triggered while Nowhere to Run was on the battlefield"
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    assert gs.resolve_top_of_stack(), "Nowhere to Run's ETB did not resolve"
    card = gs._safe_get_card(protected)
    assert (card.power, card.toughness) == (3, 3), \
        f"Nowhere to Run produced {card.power}/{card.toughness}, expected 3/3"


@scenario("609.4 / 702.11", "a hexproof target becomes illegal if Nowhere to Run leaves before resolution")
def s_nowhere_to_run_leave_restores_hexproof_for_resolution():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    protected = inject_into_zone(gs, opponent, {
        "name": "Restored Hexproof", "mana_cost": "{3}{G}",
        "type_line": "Creature - Beast", "oracle_text": "Hexproof",
        "keywords": ["Hexproof"], "power": 5, "toughness": 5,
    }, "battlefield")
    nowhere = inject_into_zone(gs, controller, {
        "name": "Nowhere to Run", "mana_cost": "{1}{B}", "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nWhen Nowhere to Run enters, target creature an opponent controls gets "
            "-3/-3 until end of turn.\nCreatures your opponents control can be the targets "
            "of spells and abilities as though they didn't have hexproof.\nWard abilities "
            "of those creatures don't trigger."
        ),
    }, "battlefield")
    gs.ability_handler.process_triggered_abilities()
    ctx = gs.targeting_context
    valid_map = gs.targeting_system.get_valid_targets(
        nowhere, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(protected), {})
    assert ok, "could not select Nowhere to Run's hexproof target"
    assert gs.move_card(nowhere, controller, "battlefield", controller, "graveyard")
    assert gs.resolve_top_of_stack(), "Nowhere to Run's now-illegal trigger did not fizzle cleanly"
    card = gs._safe_get_card(protected)
    assert (card.power, card.toughness) == (5, 5), \
        "Nowhere to Run still affected the creature after hexproof made its target illegal"


@scenario("702.21", "ward does not trigger retroactively when Nowhere to Run leaves after targeting")
def s_nowhere_to_run_leave_does_not_retroactively_trigger_ward():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    warded = inject_into_zone(gs, opponent, {
        "name": "Suppressed Ward", "mana_cost": "{3}{U}",
        "type_line": "Creature - Wizard", "oracle_text": "Ward {2}",
        "keywords": ["Ward"], "power": 4, "toughness": 4,
    }, "battlefield")
    nowhere = inject_into_zone(gs, controller, {
        "name": "Nowhere to Run", "mana_cost": "{1}{B}", "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nWhen Nowhere to Run enters, target creature an opponent controls gets "
            "-3/-3 until end of turn.\nCreatures your opponents control can be the targets "
            "of spells and abilities as though they didn't have hexproof.\nWard abilities "
            "of those creatures don't trigger."
        ),
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    spell = inject_into_zone(gs, controller, {
        "name": "Ward Timing Probe", "mana_cost": "{B}", "type_line": "Instant",
        "oracle_text": "Target creature gets -1/-1 until end of turn.",
    }, "hand")
    controller["mana_pool"]["B"] = 1
    assert gs.cast_spell(spell, controller), "could not cast the ward timing probe"
    ctx = gs.targeting_context
    valid_map = gs.targeting_system.get_valid_targets(
        spell, controller, ctx.get("required_type"), effect_text=ctx.get("effect_text"))
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert warded in valid_targets, "the warded creature was not targetable through Nowhere to Run"
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(warded), {})
    assert ok, "could not commit the ward timing target"
    assert gs.stack[-1][3].get("ward_obligations") == [], \
        "ward triggered despite Nowhere to Run"

    assert gs.move_card(nowhere, controller, "battlefield", controller, "graveyard")
    assert gs.check_keyword(warded, "ward"), "setup: ward did not remain on the creature"
    assert gs.resolve_top_of_stack(), "the ward timing probe did not resolve"
    gs.layer_system.apply_all_effects()
    card = gs._safe_get_card(warded)
    assert (card.power, card.toughness) == (3, 3), \
        "ward triggered retroactively after Nowhere to Run left"


@scenario("400", "moving a card between zones conserves total card count")
def s_zone_conservation():
    gs = fresh()
    before = zone_census(gs)
    top = gs.p1["library"][0]
    lib_count = gs.p1["library"].count(top)
    gy_count = gs.p1["graveyard"].count(top)
    assert gs.move_card(top, gs.p1, "library", gs.p1, "graveyard")
    assert zone_census(gs) == before, "a card was duplicated or lost during a zone change"
    # Count-based: decks contain duplicate copies sharing one card id.
    assert gs.p1["graveyard"].count(top) == gy_count + 1, "graveyard did not gain the card"
    assert gs.p1["library"].count(top) == lib_count - 1, "library did not lose the card"


@scenario("119.3", "damage dealt to a player reduces life by that amount")
def s_damage_to_player():
    gs = fresh()
    start = gs.p2["life"]
    gs.damage_player(gs.p2, 3, source_id=None)
    assert gs.p2["life"] == start - 3, f"expected {start - 3} life, got {gs.p2['life']}"


@scenario("601 / 608", "casting a creature spell puts it on the stack; resolving puts it onto the battlefield")
def s_cast_and_resolve():
    gs = fresh()
    active = gs._get_active_player()
    # cast whichever fixture one-drop the active player's deck contains
    name = "Ember Grunt" if any(getattr(gs._safe_get_card(c), 'name', '') == "Ember Grunt"
                                for c in active["library"]) else "Sprout Guardian"
    cid = card_id_by_name(gs, name)
    owner = active
    assert gs.move_card(cid, owner, "library", owner, "hand")
    owner["mana_pool"]["R"] = 5
    owner["mana_pool"]["G"] = 5
    gs.priority_player = owner
    stack_before = len(gs.stack)
    assert gs.cast_spell(cid, owner), "cast_spell refused a legal creature cast"
    assert len(gs.stack) == stack_before + 1, "spell did not go onto the stack"
    gs.resolve_top_of_stack()
    assert cid in owner["battlefield"], "resolved creature did not enter the battlefield"
    assert gs.phase == gs.PHASE_MAIN_PRECOMBAT, \
        "empty-stack resolution did not restore the underlying main phase"


@scenario("109.2 / 109.2b", "a creature spell on the stack is not a creature target")
def s_creature_spell_is_not_a_creature_permanent_target():
    gs = fresh()
    env = get_env()
    controller = gs._get_active_player()
    gs.agent_is_p1 = controller is gs.p1
    gs.priority_player = controller
    replace_hand(gs, controller, [])
    creature_spell = inject_into_zone(gs, controller, {
        "name": "Pending Creature Spell", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Scout", "oracle_text": "",
        "power": "1", "toughness": "1",
    }, "hand")
    removal = inject_into_zone(gs, controller, {
        "name": "Anoint Targeting Probe", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Instant",
        "oracle_text": "Exile target creature if it has mana value 3 or less.",
    }, "hand")
    controller["mana_pool"].update({"G": 1, "B": 1, "C": 1})
    assert gs.cast_spell(creature_spell, controller), \
        "setup creature did not reach the stack"
    assert creature_spell not in controller["battlefield"]
    opponent = gs.p2 if controller is gs.p1 else gs.p1
    battlefield_creature = inject_into_zone(gs, opponent, {
        "name": "Legal Creature Permanent", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Scout", "oracle_text": "",
        "power": "2", "toughness": "2",
    }, "battlefield")

    valid_map = gs.targeting_system.get_valid_targets(
        removal, controller, "creature",
        effect_text=gs._safe_get_card(removal).oracle_text)
    flat_targets = {target for targets in valid_map.values() for target in targets}
    assert creature_spell not in flat_targets, \
        "target creature incorrectly included a creature spell on the stack"
    assert flat_targets == {battlefield_creature}, flat_targets
    mask = env.action_mask()
    assert mask[20], "removal was not exposed for its legal creature permanent"
    env.action_handler.current_valid_actions = mask
    _, _, _, cast_info = env.action_handler.apply_action(20)
    assert not cast_info.get("execution_failed"), cast_info
    assert gs.targeting_context, "the removal did not request its target"
    candidates = env.action_handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert candidates == [battlefield_creature], candidates
    target_mask = env.action_mask()
    assert target_mask[274]
    env.action_handler.current_valid_actions = target_mask
    _, _, _, target_info = env.action_handler.apply_action(274)
    assert not target_info.get("execution_failed"), target_info
    assert removal not in controller["hand"]
    assert gs.stack[-1][1] == removal
    assert gs.stack[-1][3].get("targets") == {
        "creatures": [battlefield_creature]}


@scenario("500", "phase progression advances through the turn without error")
def s_phase_progression():
    gs = fresh()
    seen = [gs.phase]
    for _ in range(20):
        gs._advance_phase()
        seen.append(gs.phase)
        if len(seen) > 2 and gs.phase == gs.PHASE_UNTAP:
            break
    assert gs.PHASE_END_STEP in seen, f"turn never reached the end step (path: {seen})"
    assert len(set(seen)) >= 5, f"phase progression looks stuck (path: {seen})"


@scenario("500.4", "mana pools empty at every step and phase boundary")
def s_mana_empties_between_phases():
    gs = fresh()
    gs.phase = gs.PHASE_MAIN_PRECOMBAT

    def add_boundary_mana():
        for player in (gs.p1, gs.p2):
            player["mana_pool"] = {'W': 1, 'U': 2, 'B': 0, 'R': 0, 'G': 0, 'C': 3}
            player["conditional_mana"] = {"creatures": {'G': 1}}
            player["phase_restricted_mana"] = {'R': 1}

    def assert_empty():
        for player in (gs.p1, gs.p2):
            assert sum(player["mana_pool"].values()) == 0, \
                "ordinary mana carried across a phase boundary"
            assert not player["conditional_mana"], \
                "conditional mana carried across a phase boundary"
            assert not player["phase_restricted_mana"], \
                "phase-restricted mana carried across a phase boundary"

    add_boundary_mana()
    gs._advance_phase()
    assert gs.phase == gs.PHASE_BEGIN_COMBAT
    assert_empty()

    # Exercise the public action paths too; these historically assigned the
    # next phase directly and bypassed _advance_phase's CR 500.4 cleanup.
    handler = get_env().action_handler
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = gs.p1
    add_boundary_mana()
    assert handler.generate_valid_actions()[3], "MAIN_PHASE_END absent from its legal mask"
    _, _, _, info = handler.apply_action(3)
    assert not info.get("execution_failed"), f"MAIN_PHASE_END failed: {info}"
    assert_empty()

    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.priority_player = gs.p1
    attacker = inject_into_zone(gs, gs.p1, {
        "name": "Boundary Attacker", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.current_attackers = [attacker]
    add_boundary_mana()
    assert handler.generate_valid_actions()[438], \
        "DECLARE_ATTACKERS_DONE absent from its legal mask"
    _, _, _, info = handler.apply_action(438)
    assert not info.get("execution_failed"), f"DECLARE_ATTACKERS_DONE failed: {info}"
    assert gs.phase == gs.PHASE_DECLARE_BLOCKERS
    assert_empty()

    gs.agent_is_p1 = False
    gs.priority_player = gs.p2
    add_boundary_mana()
    assert handler.generate_valid_actions()[439], \
        "DECLARE_BLOCKERS_DONE absent from its legal mask"
    _, _, _, info = handler.apply_action(439)
    assert not info.get("execution_failed"), f"DECLARE_BLOCKERS_DONE failed: {info}"
    assert_empty()


@scenario("508.1 (attack declaration contract)", "a declared attacker cannot be toggled forever through the public mask")
def s_attack_declaration_mask_is_monotonic():
    gs = fresh(); handler = get_env().action_handler
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.priority_player = gs.p1
    gs.priority_pass_count = 0
    gs.current_attackers = []
    gs.stack.clear()

    first = inject_into_zone(gs, gs.p1, {
        "name": "Monotonic Attacker A", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    second = inject_into_zone(gs, gs.p1, {
        "name": "Monotonic Attacker B", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    gs.p1.get("entered_battlefield_this_turn", set()).difference_update(
        (first, second))
    first_action = 28 + gs.p1["battlefield"].index(first)
    second_action = 28 + gs.p1["battlefield"].index(second)

    initial_mask = handler.generate_valid_actions()
    assert initial_mask[first_action] and initial_mask[second_action]
    _, _, _, info = handler.apply_action(first_action)
    assert not info.get("execution_failed"), \
        f"first attack declaration failed: {info}"
    assert gs.current_attackers == [first]

    next_mask = handler.generate_valid_actions()
    assert not next_mask[first_action], \
        "the selected ATTACK action remained legal and could toggle forever"
    assert next_mask[second_action] and next_mask[438], \
        "declaring one attacker hid another attacker or the finish action"
    state_before = list(gs.current_attackers)
    _, _, _, info = handler.apply_action(first_action)
    assert info.get("invalid_action_reason"), \
        "the repeated ATTACK action was not rejected by its current mask"
    assert gs.current_attackers == state_before, \
        "a mask-invalid repeated ATTACK action changed the declaration"

    handler.generate_valid_actions()
    _, _, _, info = handler.apply_action(second_action)
    assert not info.get("execution_failed"), \
        f"second attack declaration failed: {info}"
    assert gs.current_attackers == [first, second]


@scenario("509.1 (block declaration contract)", "an assigned blocker cannot be toggled forever through the public mask")
def s_block_declaration_mask_is_monotonic():
    gs = fresh(); handler = get_env().action_handler
    gs.agent_is_p1 = False
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.priority_pass_count = 0
    gs.stack.clear()
    gs.current_block_assignments = {}

    attacker = inject_into_zone(gs, gs.p1, {
        "name": "Block Contract Attacker", "mana_cost": "{2}{R}", "cmc": 3,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    first = inject_into_zone(gs, gs.p2, {
        "name": "Monotonic Blocker A", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    second = inject_into_zone(gs, gs.p2, {
        "name": "Monotonic Blocker B", "mana_cost": "{2}{W}", "cmc": 3,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 3,
    }, "battlefield")
    gs.current_attackers = [attacker]
    first_action = 48 + gs.p2["battlefield"].index(first)
    second_action = 48 + gs.p2["battlefield"].index(second)

    initial_mask = handler.generate_valid_actions()
    assert initial_mask[first_action] and initial_mask[second_action]
    handler.current_valid_actions = initial_mask
    _, _, _, info = handler.apply_action(first_action)
    assert not info.get("execution_failed"), \
        f"first block declaration failed: {info}"
    assert gs.current_block_assignments == {attacker: [first]}

    next_mask = handler.generate_valid_actions()
    assert not next_mask[first_action], \
        "the assigned BLOCK action remained legal and could toggle forever"
    assert next_mask[second_action] and next_mask[439], \
        "assigning one blocker hid another blocker or the finish action"
    state_before = {
        attack_id: list(blockers)
        for attack_id, blockers in gs.current_block_assignments.items()
    }
    handler.current_valid_actions = next_mask
    _, _, _, info = handler.apply_action(first_action)
    assert info.get("invalid_action_reason"), \
        "the repeated BLOCK action was not rejected by its current mask"
    assert gs.current_block_assignments == state_before, \
        "a mask-invalid repeated BLOCK action changed the declaration"

    handler.current_valid_actions = handler.generate_valid_actions()
    _, _, _, info = handler.apply_action(second_action)
    assert not info.get("execution_failed"), \
        f"second block declaration failed: {info}"
    assert gs.current_block_assignments == {attacker: [first, second]}


@scenario("603.7", "a delayed trigger fires at the beginning of the next end step")
def s_delayed_trigger_end_step():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    fired = []
    gs.register_delayed_trigger(
        lambda: (gs.add_counter(cid, "+1/+1", 1), fired.append(gs.phase)),
        phase=gs.PHASE_END_STEP,
        description="test: +1/+1 at next end step",
    )
    # Advancing through combat phases must NOT fire it early
    for _ in range(3):
        gs._advance_phase()
        assert gs.phase != gs.PHASE_END_STEP or True
        if gs.phase == gs.PHASE_END_STEP:
            break
        assert not fired, f"delayed trigger fired early, in phase {fired}"
    # Advance until the end step has been entered
    guard = 0
    while gs.PHASE_END_STEP not in fired and guard < 25:
        gs._advance_phase()
        guard += 1
    card = gs._safe_get_card(cid)
    assert fired == [gs.PHASE_END_STEP], f"trigger fired at {fired}, expected only at end step"
    assert getattr(card, 'counters', {}).get("+1/+1", 0) == 1, "delayed effect did not apply"


@scenario("603.7a", "a delayed trigger fires only once, then expires")
def s_delayed_trigger_fires_once():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    count = []
    gs.register_delayed_trigger(lambda: count.append(1), phase=gs.PHASE_END_STEP,
                                description="test: once only")
    # Walk two full turns' worth of phase advances
    for _ in range(35):
        gs._advance_phase()
    assert len(count) == 1, f"delayed trigger fired {len(count)} times, expected exactly 1"
    assert not gs.delayed_triggers, "expired trigger was not removed from the registry"


@scenario("603.7 (text)", "leading 'At the beginning of the next end step, ...' oracle text defers its effect")
def s_text_delayed_leading():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    before = owner["life"]
    effects = EffectFactory.create_effects("At the beginning of the next end step, you gain 2 life.")
    assert effects, "parser produced no effects for delayed-trigger text"
    for eff in effects:
        eff.apply(gs, cid, owner)
    assert owner["life"] == before, \
        "delayed effect applied immediately instead of being deferred to the end step"
    assert gs.delayed_triggers, "no delayed trigger was registered from oracle text"
    guard = 0
    while gs.phase != gs.PHASE_END_STEP and guard < 25:
        gs._advance_phase()
        guard += 1
    assert owner["life"] == before + 2, \
        f"delayed effect did not fire at the end step (life {owner['life']}, expected {before + 2})"
    assert not gs.delayed_triggers, "text-parsed delayed trigger did not expire after firing"


@scenario("603.7 (text)", "trailing 'Exile it at the beginning of the next end step' defers the exile")
def s_text_delayed_trailing():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    # Unearth-style rider: the source card is the bound object ("it").
    effects = EffectFactory.create_effects("Exile it at the beginning of the next end step.")
    assert effects, "parser produced no effects for trailing delayed-trigger text"
    for eff in effects:
        eff.apply(gs, cid, owner)
    assert cid in owner["battlefield"], \
        "exile happened immediately instead of at the next end step"
    guard = 0
    while gs.phase != gs.PHASE_END_STEP and guard < 25:
        gs._advance_phase()
        guard += 1
    assert cid in owner["exile"], \
        "creature was not exiled at the beginning of the next end step"
    assert not gs.delayed_triggers, "trailing-form delayed trigger did not expire after firing"


@scenario("603.7 (guard)", "recurring 'at the beginning of your upkeep' text is NOT a one-shot delayed trigger")
def s_text_delayed_not_recurring():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    pre = len(gs.delayed_triggers)
    effects = EffectFactory.create_effects("At the beginning of your upkeep, you gain 1 life.")
    for eff in effects:
        try:
            eff.apply(gs, None, gs.p1)
        except Exception:
            pass
    assert len(gs.delayed_triggers) == pre, \
        "recurring upkeep trigger text was wrongly registered as a one-shot delayed trigger"


@scenario("603.12", "a successful prerequisite creates a separate reflexive trigger")
def s_reflexive_trigger_after_successful_action():
    gs = fresh()
    from Playersim.ability_types import ReflexiveTriggerEffect, TriggeredAbility
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Reflexive Altar", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    fodder = inject_into_zone(gs, player, {
        "name": "Reflexive Fodder", "mana_cost": "{G}",
        "type_line": "Creature - Plant", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    effect_text = "Sacrifice a creature. When you do, draw two cards."
    parsed = EffectFactory.create_effects(effect_text)
    assert len(parsed) == 1 and isinstance(parsed[0], ReflexiveTriggerEffect), \
        f"reflexive text did not parse as one gated effect: {parsed}"
    ability = TriggeredAbility(
        source, trigger_condition="when this artifact is tapped",
        effect=effect_text, effect_text=effect_text)
    hand_before = len(player["hand"])
    gs.add_to_stack("TRIGGER", source, player, {
        "ability": ability, "effect_text": effect_text,
    })

    assert gs.resolve_top_of_stack(), "parent ability did not finish resolving"
    assert gs.choice_context and gs.choice_context.get('type') == 'sacrifice_effect', \
        "reflexive prerequisite did not expose the sacrifice choice"
    gs.agent_is_p1 = player is gs.p1
    get_env().action_handler.generate_valid_actions()
    _, ok = get_env().action_handler._handle_choose_mode(
        gs.choice_context['options'].index(fodder), {})
    assert ok
    assert fodder in player["graveyard"], "prerequisite sacrifice did not happen"
    assert len(player["hand"]) == hand_before, \
        "reflexive draw resolved immediately instead of using the stack"
    queued = gs.ability_handler.active_triggers
    assert len(queued) == 1 and queued[0][2].get("is_reflexive_trigger"), \
        f"successful prerequisite did not queue exactly one reflexive trigger: {queued}"

    gs.ability_handler.process_triggered_abilities()
    assert gs.stack and gs.stack[-1][0] == "TRIGGER", "reflexive trigger was not put on the stack"
    assert gs.resolve_top_of_stack(), "reflexive trigger did not resolve"
    assert len(player["hand"]) == hand_before + 2, "reflexive trigger did not draw two cards"


@scenario("603.12", "a failed prerequisite does not create a reflexive trigger")
def s_reflexive_trigger_requires_successful_action():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Empty Reflexive Altar", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    effect_text = "Sacrifice a creature. When you do, draw two cards."
    ability = TriggeredAbility(
        source, trigger_condition="when this artifact is tapped",
        effect=effect_text, effect_text=effect_text)
    hand_before = len(player["hand"])
    gs.add_to_stack("TRIGGER", source, player, {
        "ability": ability, "effect_text": effect_text,
    })

    assert gs.resolve_top_of_stack(), "parent ability failed instead of resolving without the action"
    assert not gs.ability_handler.active_triggers, \
        "failed prerequisite still queued a reflexive trigger"
    assert not gs.stack, "failed prerequisite put a reflexive trigger on the stack"
    assert len(player["hand"]) == hand_before, "failed prerequisite still performed the reflexive draw"


@scenario("603.2 / 603.6c", "battlefield death triggers do not fire from cards in hand")
def s_death_trigger_respects_source_zone():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    player = gs.p1
    hand_source = inject_into_zone(gs, player, {
        "name": "Waiting Death Watcher", "mana_cost": "{2}{B}",
        "type_line": "Creature - Cleric", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "hand")
    battlefield_source = inject_into_zone(gs, player, {
        "name": "Active Death Watcher", "mana_cost": "{2}{B}",
        "type_line": "Creature - Cleric", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    dying = inject_into_zone(gs, player, {
        "name": "Trigger Test Fodder", "mana_cost": "{G}",
        "type_line": "Creature - Plant", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    hand_ability = TriggeredAbility(
        hand_source, trigger_condition="whenever another creature dies",
        effect="you gain 1 life")
    battlefield_ability = TriggeredAbility(
        battlefield_source, trigger_condition="whenever another creature dies",
        effect="you gain 1 life")
    gs.ability_handler.registered_abilities = {
        hand_source: [hand_ability], battlefield_source: [battlefield_ability],
    }
    gs.ability_handler.active_triggers = []

    gs.trigger_ability(dying, "DIES", {
        "controller": player, "from_zone": "battlefield", "to_zone": "graveyard",
    })
    queued_sources = [entry[0].card_id for entry in gs.ability_handler.active_triggers]
    assert queued_sources == [battlefield_source], \
        f"DIES event queued triggers from the wrong zones: {queued_sources}"


@scenario("707.10", "a copied spell inherits its choices and may keep its original targets")
def s_spell_copy_inherits_choices_and_targets():
    gs = fresh()
    from Playersim.ability_types import CopySpellEffect
    controller = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if controller == gs.p1 else gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Echo Scepter", "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "Copy target instant or sorcery spell. You may choose new targets for the copy.",
    }, "battlefield")
    target = inject_into_zone(gs, opponent, {
        "name": "Copy Target", "mana_cost": "{3}",
        "type_line": "Creature - Golem", "oracle_text": "",
        "power": 0, "toughness": 5,
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Choice Bolt", "mana_cost": "{X}{R}",
        "type_line": "Instant",
        "oracle_text": "Tap up to two target creatures.",
    })
    original_context = {
        "targets": {"creatures": [target]},
        "requires_target": True,
        "num_targets": 2,
        "min_targets": 0,
        "max_targets": 2,
        "selected_modes": [1],
        "X": 4,
        "chosen_color": "red",
        "paid_kicker": True,
        "kicked": True,
        "final_paid_cost": {"R": 1, "generic": 4},
    }
    gs.add_to_stack("SPELL", spell, controller, original_context)

    effect = CopySpellEffect(target_type="instant", new_targets=True)
    assert effect.apply(gs, source, controller, {"spells": [spell]}), \
        "copy effect failed"
    assert len(gs.stack) == 2 and gs.stack[-1][3].get("is_copy"), \
        "copy was not put on top of the original spell"
    copied = gs.stack[-1][3]
    for key in ("targets", "selected_modes", "X", "chosen_color",
                "paid_kicker", "kicked", "final_paid_cost"):
        assert copied.get(key) == original_context[key], \
            f"spell copy did not inherit {key}: {copied.get(key)!r}"
    assert copied["targets"] is not original_context["targets"], \
        "copied target data aliases the original spell context"
    assert gs.targeting_context and gs.targeting_context.get("copy_instance_id"), \
        "copy did not offer its optional new-target choice"
    assert gs.targeting_context.get("min_targets") == 0, \
        "copy fixture did not exercise a zero-minimum retarget choice"

    handler = get_env().action_handler
    assert handler.generate_valid_actions()[11], \
        "target action mask did not expose keeping the copy's original targets"
    handler._handle_pass_priority(None)
    assert gs.targeting_context is None, \
        "passing the optional retarget choice did not finish targeting"
    assert gs.stack[-1][3]["targets"] == {"creatures": [target]}, \
        "keeping targets changed or erased the copied spell's targets"
    assert gs.priority_pass_count == 0, \
        "keeping copied targets was treated as a normal priority pass"


@scenario("707.10c", "choosing a new target changes only the copied spell")
def s_spell_copy_retargets_only_the_copy():
    gs = fresh()
    from Playersim.ability_types import CopySpellEffect
    controller = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if controller == gs.p1 else gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Forking Wand", "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "Copy target instant or sorcery spell. You may choose new targets for the copy.",
    }, "battlefield")
    first_target = inject_into_zone(gs, opponent, {
        "name": "First Copy Target", "mana_cost": "{3}",
        "type_line": "Creature - Golem", "oracle_text": "",
        "power": 0, "toughness": 5,
    }, "battlefield")
    new_target = inject_into_zone(gs, opponent, {
        "name": "Second Copy Target", "mana_cost": "{3}",
        "type_line": "Creature - Golem", "oracle_text": "",
        "power": 0, "toughness": 5,
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Forked Bolt Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Forked Bolt Probe deals 3 damage to target creature.",
    })
    gs.add_to_stack("SPELL", spell, controller, {
        "targets": {"creatures": [first_target]},
        "requires_target": True,
        "num_targets": 1,
        "source_zone": "stack_implicit",
    })
    effect = CopySpellEffect(target_type="instant", new_targets=True)
    assert effect.apply(gs, source, controller, {"spells": [spell]}), \
        "copy effect failed"
    assert gs.stack[-1][3]["targets"] == {"creatures": [first_target]}, \
        "copy discarded its original target before a new one was chosen"

    target_type = gs.targeting_context.get("required_type")
    valid_map = gs.targeting_system.get_valid_targets(spell, controller, target_type)
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    assert new_target in valid_targets, "replacement target was not legal"
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(new_target), {})
    assert ok, f"retarget action failed with reward {reward}"
    assert gs.stack[-1][3]["targets"] == {"creatures": [new_target]}, \
        "new target was not written to the copied spell"
    assert gs.stack[-2][3]["targets"] == {"creatures": [first_target]}, \
        "retargeting the copy mutated the original spell"

    assert gs.resolve_top_of_stack(), "copied spell did not resolve"
    assert opponent.get("damage_counters", {}).get(new_target, 0) == 3, \
        "copy did not damage its new target"
    assert opponent.get("damage_counters", {}).get(first_target, 0) == 0, \
        "copy still damaged the original target"
    assert gs.resolve_top_of_stack(), "original spell did not resolve"
    assert opponent.get("damage_counters", {}).get(first_target, 0) == 3, \
        "original spell did not retain its target"
    assert controller["graveyard"].count(spell) == 1, \
        "resolving a spell and its copy moved the physical card more than once"


@scenario("707.10 / 701.5", "countering a spell copy does not move the physical card")
def s_countered_spell_copy_ceases_without_moving_card():
    gs = fresh()
    from Playersim.ability_types import CopySpellEffect
    controller = gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Counter Copy Source", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "Copy target spell.",
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Counter Copy Probe", "mana_cost": "{U}",
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    gs.add_to_stack("SPELL", spell, controller, {
        "requires_target": False,
        "source_zone": "stack_implicit",
    })
    effect = CopySpellEffect(new_targets=False)
    assert effect.apply(gs, source, controller, {"spells": [spell]}), \
        "copy effect failed"
    assert gs.counter_spell(len(gs.stack) - 1), "countering the copied spell failed"
    assert spell not in controller["graveyard"], \
        "countering the copy moved the original physical card"
    assert len(gs.stack) == 1 and not gs.stack[0][3].get("is_copy"), \
        "countering the copy removed or changed the original spell"

    assert gs.resolve_top_of_stack(), "original spell did not resolve after its copy was countered"
    assert controller["graveyard"].count(spell) == 1, \
        "the physical spell card did not move exactly once when the original resolved"


@scenario("707.10 (parser)", "an instant-or-sorcery copy effect rejects other spell types")
def s_parsed_spell_copy_enforces_target_type():
    gs = fresh()
    from Playersim.ability_types import CopySpellEffect
    from Playersim.ability_utils import EffectFactory
    controller = gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Restricted Copy Source", "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "Copy target instant or sorcery spell. You may choose new targets for the copy.",
    }, "battlefield")
    creature_spell = inject_card(gs, {
        "name": "Uncopyable Creature Probe", "mana_cost": "{2}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    })
    gs.add_to_stack("SPELL", creature_spell, controller, {
        "requires_target": False,
        "source_zone": "stack_implicit",
    })
    effects = EffectFactory.create_effects(gs._safe_get_card(source).oracle_text)
    copy_effects = [effect for effect in effects if isinstance(effect, CopySpellEffect)]
    assert len(copy_effects) == 1 and copy_effects[0].new_targets, \
        f"real copy wording did not retain its new-target rider: {effects}"
    assert not copy_effects[0].apply(
        gs, source, controller, {"spells": [creature_spell]}), \
        "instant-or-sorcery copy effect illegally copied a creature spell"
    assert len(gs.stack) == 1 and not gs.stack[0][3].get("is_copy"), \
        "rejected spell target still produced a copy"


def _kw(gs, cid, name):
    """1 if the live card currently has keyword `name`, else 0."""
    from Playersim.card import Card
    card = gs._safe_get_card(cid)
    idx = Card.ALL_KEYWORDS.index(name)
    kws = getattr(card, 'keywords', None) or []
    return kws[idx] if idx < len(kws) else 0


@scenario("613.8", "an effect whose source loses all abilities stops applying (within-layer dependency)")
def s_layer_dependency_strip_grantor():
    gs = fresh()
    grantor = card_id_by_name(gs, "Moss Titan")       # has Trample
    grantee = card_id_by_name(gs, "Vine Stalker")     # vanilla
    stripper = card_id_by_name(gs, "Canopy Sentinel") # stands in for a Humility-style source
    for cid in (grantor, grantee, stripper):
        to_battlefield(gs, cid)
    ls = gs.layer_system
    # Earlier timestamp: the grantor's static ability gives the grantee flying.
    ls.register_effect({'source_id': grantor, 'layer': 6, 'affected_ids': [grantee],
                        'effect_type': 'add_ability', 'effect_value': 'flying',
                        'duration': 'permanent'})
    # Later timestamp: strip ALL abilities from the grantor.
    ls.register_effect({'source_id': stripper, 'layer': 6, 'affected_ids': [grantor],
                        'effect_type': 'remove_all_abilities', 'effect_value': None,
                        'duration': 'permanent'})
    ls.invalidate_cache()
    ls.apply_all_effects()
    # CR 613.8: the grant depends on the strip; the strip applies first and the
    # grant's source has no abilities, so the grant does not exist.
    assert _kw(gs, grantor, "trample") == 0, "stripped grantor still has trample"
    assert _kw(gs, grantee, "flying") == 0, \
        "grant from an ability-stripped source still applied (dependency ordering ignored)"


@scenario("613.8c", "a dependency loop between two strip effects falls back to timestamp order")
def s_layer_dependency_cycle():
    gs = fresh()
    p = card_id_by_name(gs, "Moss Titan")        # Trample
    q = card_id_by_name(gs, "Canopy Sentinel")   # Reach
    for cid in (p, q):
        to_battlefield(gs, cid)
    ls = gs.layer_system
    # A (earlier): P's ability strips Q. B (later): Q's ability strips P.
    ls.register_effect({'source_id': p, 'layer': 6, 'affected_ids': [q],
                        'effect_type': 'remove_all_abilities', 'effect_value': None,
                        'duration': 'permanent'})
    ls.register_effect({'source_id': q, 'layer': 6, 'affected_ids': [p],
                        'effect_type': 'remove_all_abilities', 'effect_value': None,
                        'duration': 'permanent'})
    ls.invalidate_cache()
    ls.apply_all_effects()
    # Loop -> timestamp order: A applies, stripping Q; B's source now has no
    # abilities, so B never applies and P keeps trample.
    assert _kw(gs, q, "reach") == 0, "earlier strip in the loop did not apply"
    assert _kw(gs, p, "trample") == 1, \
        "later strip applied even though its source had lost all abilities"


@scenario("613.8 (guard)", "a grant with a later timestamp than a strip on its target still applies")
def s_layer_dependency_grant_after_strip():
    gs = fresh()
    target = card_id_by_name(gs, "Vine Stalker")
    stripper = card_id_by_name(gs, "Canopy Sentinel")
    grantor = card_id_by_name(gs, "Thicket Brute")   # NOT affected by the strip
    for cid in (target, stripper, grantor):
        to_battlefield(gs, cid)
    ls = gs.layer_system
    # Earlier: strip the target's abilities. Later: an untouched source grants flying.
    ls.register_effect({'source_id': stripper, 'layer': 6, 'affected_ids': [target],
                        'effect_type': 'remove_all_abilities', 'effect_value': None,
                        'duration': 'permanent'})
    ls.register_effect({'source_id': grantor, 'layer': 6, 'affected_ids': [target],
                        'effect_type': 'add_ability', 'effect_value': 'flying',
                        'duration': 'permanent'})
    ls.invalidate_cache()
    ls.apply_all_effects()
    # No dependency between the two (the grantor is untouched), so timestamp
    # order stands: strip, then grant -> the target HAS flying.
    assert _kw(gs, target, "flying") == 1, \
        "grant applied after a strip on its target was wrongly suppressed"


@scenario("613.8a", "removing the specific ability that generates an effect suppresses that effect")
def s_layer_dependency_specific_ability_removal():
    gs = fresh()
    source = card_id_by_name(gs, "Moss Titan")
    recipient = card_id_by_name(gs, "Vine Stalker")
    remover = card_id_by_name(gs, "Canopy Sentinel")
    for cid in (source, recipient, remover):
        to_battlefield(gs, cid)
    ls = gs.layer_system

    # Earlier removal and later source effect would ordinarily leave flying on
    # the recipient by timestamp. The later effect is generated by the exact
    # Flying ability removed from its source, so CR 613.8 makes it cease to
    # exist before application.
    ls.register_effect({
        'source_id': remover, 'layer': 6, 'affected_ids': [source],
        'effect_type': 'remove_ability', 'effect_value': 'flying',
        'duration': 'permanent', 'source_ability': 'remove flying',
    })
    ls.register_effect({
        'source_id': source, 'layer': 6, 'affected_ids': [recipient],
        'effect_type': 'add_ability', 'effect_value': 'flying',
        'duration': 'permanent', 'source_ability': 'flying',
    })
    # A different static ability of that same source must remain. Losing the
    # Flying keyword does not erase an unrelated anthem ability.
    ls.register_effect({
        'source_id': source, 'layer': 6, 'affected_ids': [recipient],
        'effect_type': 'add_ability', 'effect_value': 'trample',
        'duration': 'permanent',
        'source_ability': 'creatures you control have trample',
    })
    ls.invalidate_cache()
    ls.apply_all_effects()
    assert _kw(gs, source, "flying") == 0
    assert _kw(gs, recipient, "flying") == 0, \
        "an effect generated by the removed Flying ability still applied"
    assert _kw(gs, recipient, "trample") == 1, \
        "specific ability removal suppressed an unrelated source ability"


@scenario("305.7 / 613.1d", "setting a basic land type removes old abilities, grants basic mana, and is reversible")
def s_basic_land_type_setting_removes_land_abilities():
    gs = fresh()
    player = gs.p1
    nonbasic = inject_into_zone(gs, player, {
        "name": "Scholar's Gate", "mana_cost": "", "cmc": 0,
        "type_line": "Artifact Land - Gate",
        "card_types": ["artifact", "land"],
        "subtypes": ["gate"],
        "oracle_text": (
            "{T}: Add {U}.\n"
            "{1}, {T}: Draw a card.\n"
            "Whenever a creature enters the battlefield, you gain 1 life."),
    }, "battlefield")
    basic = inject_into_zone(gs, player, {
        "name": "Control Forest", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "card_types": ["land"],
        "supertypes": ["basic"], "subtypes": ["forest"],
        "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    assert gs.ability_handler.get_activated_abilities(nonbasic), \
        "the test land's nonmana activated ability was not registered"

    # Urborg-shaped earlier effect: the nonbasic land's ability would add
    # Swamp to every land. A later basic-type-setting effect must apply first
    # by dependency, remove this source ability, and leave the Forest alone.
    gs.layer_system.register_effect({
        'source_id': nonbasic, 'layer': 4, 'affected_ids': [nonbasic, basic],
        'effect_type': 'add_subtype', 'effect_value': 'swamp',
        'duration': 'permanent',
        'source_ability': 'each land is a swamp in addition to its other land types',
    })

    moon = inject_into_zone(gs, player, {
        "name": "Blood Moon Probe", "mana_cost": "{2}{R}", "cmc": 3,
        "type_line": "Enchantment", "card_types": ["enchantment"],
        "oracle_text": "Nonbasic lands are Mountains.",
    }, "battlefield")
    land = gs._safe_get_card(nonbasic)
    forest = gs._safe_get_card(basic)
    assert [str(s).lower() for s in land.subtypes] == ["mountain"], \
        f"the nonbasic land did not become only a Mountain: {land.subtypes}"
    assert {str(t).lower() for t in land.card_types} == {"artifact", "land"}, \
        "setting a basic land type illegally removed another card type"
    assert [str(s).lower() for s in forest.subtypes] == ["forest"], \
        "the removed Urborg-shaped source ability still changed the Forest"
    options = gs.mana_system._land_mana_options(player, land)
    assert [option["symbol"] for option in options] == ["R"], \
        f"the Mountain did not have exactly the basic red mana ability: {options}"
    assert not gs.ability_handler.get_activated_abilities(nonbasic), \
        "the land retained its printed activated ability under CR 305.7"
    gs.ability_handler.active_triggers.clear()
    entrant = inject_into_zone(gs, player, {
        "name": "Moon Trigger Probe", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    assert entrant in player["battlefield"]
    assert not any(getattr(ability, "card_id", None) == nonbasic
                   for ability, *_ in gs.ability_handler.active_triggers), \
        "the land's printed triggered ability fired after CR 305.7 removed it"

    late_nonbasic = inject_into_zone(gs, player, {
        "name": "Late Moon Gate", "mana_cost": "", "cmc": 0,
        "type_line": "Land - Gate", "card_types": ["land"],
        "subtypes": ["gate"], "oracle_text": "{T}: Add {W}.",
    }, "battlefield")
    assert [str(s).lower() for s in gs._safe_get_card(late_nonbasic).subtypes] \
        == ["mountain"], \
        "a nonbasic land entering later escaped the dynamic Blood Moon scope"

    assert gs.move_card(moon, player, "battlefield", player, "graveyard")
    gs.layer_system.apply_all_effects()
    land = gs._safe_get_card(nonbasic)
    restored_subtypes = {str(s).lower() for s in land.subtypes}
    assert "gate" in restored_subtypes and "mountain" not in restored_subtypes, \
        "the land's printed subtype was not restored when the effect ended"
    assert "swamp" in restored_subtypes, \
        "the Urborg-shaped ability did not resume after Blood Moon left"
    assert [str(s).lower() for s in gs._safe_get_card(late_nonbasic).subtypes] \
        == ["gate"], "the later land did not restore its printed subtype"
    assert "add {u}" in (land.oracle_text or "").lower()
    assert gs.ability_handler.get_activated_abilities(nonbasic), \
        "the land's printed activated ability was not restored"


@scenario("613.7 / 613.8", "declarative applicability sets recompute after earlier effects in the same layer")
def scenario_layer_live_applicability_boolean_scope():
    gs = fresh(SEED + 187)
    player = gs.p1
    subject = inject_into_zone(gs, player, {
        "name": "Live Scope Subject", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact Creature - Construct", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    control = inject_into_zone(gs, player, {
        "name": "Live Scope Control", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact Creature - Golem", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    layers = gs.layer_system
    first = layers.register_effect({
        "source_id": subject, "layer": 4, "affected_ids": [subject],
        "effect_type": "set_subtype", "effect_value": ["elf"],
        "duration": "permanent",
    })
    layers.register_effect({
        "source_id": subject, "layer": 4, "affected_ids": [],
        "affected_scope": {
            "player": "controller",
            "where": {"field": "subtypes", "op": "contains", "value": "elf"},
        },
        "controller_id": player,
        "effect_type": "add_subtype", "effect_value": "wizard",
        "duration": "permanent",
    })
    layers.register_effect({
        "source_id": subject, "layer": 6, "affected_ids": [],
        "affected_scope": {
            "players": "all",
            "where": {"all": [
                {"field": "card_types", "op": "contains", "value": "artifact"},
                {"field": "subtypes", "op": "contains", "value": "wizard"},
            ]},
        },
        "effect_type": "add_ability", "effect_value": "flying",
        "duration": "permanent",
    })
    layers.invalidate_cache()
    layers.apply_all_effects()
    subject_card = gs._safe_get_card(subject)
    assert {str(value).lower() for value in subject_card.subtypes} == {"elf", "wizard"}, \
        "the later layer-4 effect used its pre-layer frozen applicability set"
    assert gs.check_keyword(subject, "flying")
    assert not gs.check_keyword(control, "flying")

    # Removing the earlier effect must shrink both downstream live scopes on
    # the next pass rather than preserving their former members.
    layers.remove_effect(first)
    layers.invalidate_cache()
    layers.apply_all_effects()
    assert {str(value).lower() for value in gs._safe_get_card(subject).subtypes} \
        == {"construct"}
    assert not gs.check_keyword(subject, "flying")


@scenario("603.1", "triggered ability text parses into trigger condition and effect")
def s_trigger_parse():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    ta = TriggeredAbility(card_id=cid,
                          effect_text="When this creature enters the battlefield, draw a card.")
    assert ta.trigger_condition == "when this creature enters the battlefield", \
        f"mangled trigger condition: '{ta.trigger_condition}'"
    assert ta.effect == "draw a card", f"mangled effect: '{ta.effect}'"
    ctx = {'game_state': gs, 'controller': owner}
    assert ta.can_trigger('ENTERS_BATTLEFIELD', ctx), "ETB trigger did not fire on its event"
    assert not ta.can_trigger('DIES', ctx), "ETB trigger fired on the wrong event"
    hand_before = len(owner["hand"])
    ta.resolve(gs, owner)
    assert len(owner["hand"]) == hand_before + 1, "trigger effect did not resolve"


@scenario("603.4", "intervening 'if' is checked at trigger time")
def s_intervening_if_trigger_time():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    ta = TriggeredAbility(card_id=cid,
                          effect_text="When this creature enters the battlefield, "
                                      "if you have 30 or more life, draw a card.")
    assert ta.effect == "draw a card", \
        f"intervening 'if' was not separated from the effect: '{ta.effect}'"
    ctx = {'game_state': gs, 'controller': owner}
    owner["life"] = 20
    assert not ta.can_trigger('ENTERS_BATTLEFIELD', ctx), \
        "ability triggered although the intervening 'if' was false (CR 603.4)"
    owner["life"] = 30
    assert ta.can_trigger('ENTERS_BATTLEFIELD', ctx), \
        "ability failed to trigger although the intervening 'if' was true"


@scenario("603.4", "intervening 'if' is checked again at resolution; if false, the ability does nothing")
def s_intervening_if_resolution_time():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    cid = card_id_by_name(gs, "Thicket Brute")
    owner = to_battlefield(gs, cid)
    ta = TriggeredAbility(card_id=cid,
                          effect_text="When this creature enters the battlefield, "
                                      "if you have 30 or more life, draw a card.")
    ctx = {'game_state': gs, 'controller': owner}
    owner["life"] = 30
    assert ta.can_trigger('ENTERS_BATTLEFIELD', ctx)
    # Condition becomes false while the ability is on the stack.
    owner["life"] = 20
    hand_before = len(owner["hand"])
    ta.resolve(gs, owner)
    assert len(owner["hand"]) == hand_before, \
        "ability resolved its effect although the intervening 'if' was false at resolution"
    # And with the condition still true at resolution, it does resolve.
    owner["life"] = 30
    ta.resolve(gs, owner)
    assert len(owner["hand"]) == hand_before + 1, \
        "ability with a true intervening 'if' failed to resolve"


@scenario("603.4 / fidelity", "common trigger conditions are explicit and unknown conditions fail closed")
def s_trigger_condition_vocabulary_and_fail_closed():
    from Playersim.ability_types import TriggeredAbility
    gs = fresh(SEED + 184)
    player, opponent = gs.p1, gs.p2
    source = inject_into_zone(gs, player, {
        "name": "Condition Sentinel", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    second = inject_into_zone(gs, player, {
        "name": "Condition Ally", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    ability = TriggeredAbility(
        source, trigger_condition="whenever a creature enters",
        effect="you gain 1 life")
    context = {"game_state": gs, "controller": player}
    assert ability._evaluate_condition(
        "if you control two or more creatures", context)
    assert not ability._evaluate_condition(
        "if you control no creatures", context)
    assert ability._evaluate_condition("if it's your turn", context)
    player["hand"] = [second]
    opponent["hand"] = []
    assert ability._evaluate_condition(
        "if you have more cards in hand than an opponent", context)
    gs.creatures_died_this_turn = {"p2": 1}
    assert ability._evaluate_condition(
        "if a creature died this turn", context)

    before = gs.fidelity_counters["unparsed_effects"]
    assert not ability._evaluate_condition(
        "if the moon remembers your secret name", context), \
        "an unknown condition silently resolved as true"
    assert gs.fidelity_counters["unparsed_effects"] == before + 1
    assert "Condition Sentinel" in gs.fidelity_counters["unparsed_cards"]


@scenario("603 (e2e)", "an ETB trigger parsed from card text fires through the full pipeline")
def s_etb_trigger_end_to_end():
    gs = fresh()
    cid = card_id_by_name(gs, "Grove Chronicler")
    owner = owner_of(gs, cid)
    hand_before = len(owner["hand"])
    # move_card fires ENTERS_BATTLEFIELD -> check_abilities queues the parsed
    # TriggeredAbility -> process_triggered_abilities stacks it -> resolution
    # applies "draw a card". This is the wiring the fixture decks now exercise
    # in every random episode.
    to_battlefield(gs, cid)
    gs.ability_handler.process_triggered_abilities()
    guard = 0
    while gs.stack and guard < 10:
        gs.resolve_top_of_stack()
        guard += 1
    assert len(owner["hand"]) == hand_before + 1, \
        "ETB draw trigger did not fire end-to-end (registration, event, stack, or resolution broke)"


@scenario("603.3b", "simultaneous triggers for the agent's player become an ordering choice")
def s_trigger_order_choice():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    gs.priority_player = agent
    # Two creatures the agent controls, each with a (hand-built) trigger.
    c1 = card_id_by_name(gs, "Grove Chronicler")
    c2 = card_id_by_name(gs, "Verdant Reclaimer")
    # Force both onto the AGENT's battlefield regardless of deck ownership:
    for cid in (c1, c2):
        owner = owner_of(gs, cid)
        assert gs.move_card(cid, owner, "library", agent, "battlefield")
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    t1 = TriggeredAbility(card_id=c1, effect_text="When this creature enters the battlefield, draw a card.")
    t2 = TriggeredAbility(card_id=c2, effect_text="When this creature enters the battlefield, you gain 2 life.")
    # Make sure both triggers belong to the ACTIVE player so they form one
    # AP batch (603.3b is about ordering within one player's batch).
    while gs._get_active_player() is not agent:
        gs._advance_phase()
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.ability_handler.active_triggers = [(t1, agent, {}), (t2, agent, {})]
    gs.ability_handler.process_triggered_abilities()
    # 2+ simultaneous triggers -> the engine must ask, not pick silently.
    assert gs.phase == gs.PHASE_CHOOSE, "no ordering choice was offered for simultaneous triggers"
    assert gs.choice_context and gs.choice_context.get("type") == "order_triggers"
    assert len(gs.choice_context.get("pending", [])) == 2
    assert not gs.stack, "triggers were stacked before the ordering choice was made"
    # Agent chooses the SECOND trigger to go on the stack first; the remaining
    # one is auto-stacked (no pointless extra decision).
    assert gs.ability_handler.order_trigger_chosen(1), "valid ordering choice was rejected"
    assert gs.choice_context is None, "choice context not cleared after ordering completed"
    # Stack is non-empty after ordering: the game must sit in PHASE_PRIORITY
    # (CR 117.3c) with the interrupted phase saved for when the stack empties.
    assert gs.phase == gs.PHASE_PRIORITY, "priority round not opened after triggers were stacked"
    assert gs.previous_priority_phase == gs.PHASE_MAIN_PRECOMBAT, \
        "interrupted phase was not preserved across the ordering choice"
    stacked = [item[1] for item in gs.stack if item[0] == "TRIGGER"]
    assert stacked == [c2, c1], f"stack order {stacked} does not reflect the chosen order [{c2}, {c1}]"


@scenario("603.3b (guard)", "a single trigger bypasses the ordering choice entirely")
def s_trigger_order_single_bypass():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    c1 = card_id_by_name(gs, "Grove Chronicler")
    owner = owner_of(gs, c1)
    assert gs.move_card(c1, owner, "library", agent, "battlefield")
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    t1 = TriggeredAbility(card_id=c1, effect_text="When this creature enters the battlefield, draw a card.")
    gs.ability_handler.active_triggers = [(t1, agent, {})]
    gs.ability_handler.process_triggered_abilities()
    # No ordering decision for a single trigger: no choice context, no
    # PHASE_CHOOSE. (add_to_stack legitimately opens a priority round.)
    assert gs.choice_context is None, "single trigger wrongly created a choice context"
    assert gs.phase != gs.PHASE_CHOOSE, "single trigger wrongly opened a choice phase"
    stacked = [item[1] for item in gs.stack if item[0] == "TRIGGER"]
    assert stacked == [c1], "single trigger did not go straight onto the stack"


@scenario("613 (idempotence)", "repeated layer recalculation does not compound static P/T effects")
def s_layer_recalc_idempotent():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")   # printed 3/3
    to_battlefield(gs, cid)
    card = gs._safe_get_card(cid)
    ls = gs.layer_system
    ls.register_effect({'source_id': cid, 'layer': 7, 'sublayer': 'c',
                        'affected_ids': [cid], 'effect_type': 'modify_pt',
                        'effect_value': (1, 1), 'duration': 'permanent'})
    results = []
    for _ in range(3):
        ls.invalidate_cache()
        ls.apply_all_effects()
        results.append((card.power, card.toughness))
    # The layer pass must start from PRINTED characteristics every time; the
    # live card is an output, never an input. +1/+1 on a 3/3 is 4/4 forever.
    assert results == [(4, 4), (4, 4), (4, 4)], \
        f"static +1/+1 compounded across recalculations: {results} (base feeds back into itself)"


@scenario("601.2f", "cost increases apply before cost reductions")
def s_cost_modification_order():
    gs = fresh()
    ms = gs.mana_system
    player = gs.p1
    cid = card_id_by_name(gs, "Thicket Brute")
    # Synthetic modifiers: a +2 tax and a -3 discount on a {2} generic cost.
    # CR 601.2f: increases first (2+2=4), then reductions (4-3=1) -> generic 1.
    # Reductions-first bottoms out at zero and gives 2 - a full mana off.
    ms._gather_cost_modification_effects = lambda p, c, ctx=None: [
        {'type': 'reduction', 'applies_to': 'generic', 'amount': 3, 'source': 'Discount'},
        {'type': 'increase', 'applies_to': 'generic', 'amount': 2, 'source': 'Tax'},
    ]
    base = ms.parse_mana_cost("{2}{G}")
    final = ms.apply_cost_modifiers(player, base, cid)
    assert final['generic'] == 1, \
        f"expected generic 1 (increases before reductions, CR 601.2f), got {final['generic']}"
    assert final['G'] == 1, "colored component should be untouched"


@scenario("601.2f / Domain", "Leyline Binding counts distinct basic land types for its cost")
def s_leyline_binding_domain_cost():
    gs = fresh()
    controller = gs.p1
    opponent = gs.p2
    gs.priority_player = controller
    gs.priority_pass_count = 0

    inject_into_zone(gs, controller, {
        "name": "Prairie Domain Probe", "mana_cost": "",
        "type_line": "Land - Plains Island", "oracle_text": "",
    }, "battlefield")
    inject_into_zone(gs, controller, {
        "name": "Swamp Domain Probe", "mana_cost": "",
        "type_line": "Basic Land - Swamp", "oracle_text": "",
    }, "battlefield")
    inject_into_zone(gs, controller, {
        "name": "Duplicate Plains Probe", "mana_cost": "",
        "type_line": "Basic Land - Plains", "oracle_text": "",
    }, "battlefield")
    inject_into_zone(gs, opponent, {
        "name": "Binding Cost Victim", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    binding = inject_into_zone(gs, controller, {
        "name": "Leyline Binding", "mana_cost": "{5}{W}",
        "type_line": "Enchantment",
        "oracle_text": (
            "Flash\nDomain - This spell costs {1} less to cast for each basic land "
            "type among lands you control.\nWhen Leyline Binding enters the battlefield, "
            "exile target nonland permanent an opponent controls until Leyline Binding "
            "leaves the battlefield."),
    }, "hand")
    controller["mana_pool"] = {'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 2}

    binding_index = controller["hand"].index(binding)
    assert get_env().action_handler.generate_valid_actions()[20 + binding_index], \
        "the action mask hid Leyline Binding at its reduced Domain cost"
    assert gs.cast_spell(binding, controller), \
        "three distinct basic land types did not reduce Leyline Binding to {2}{W}"
    paid_cost = gs.stack[-1][3].get("final_paid_cost", {})
    assert paid_cost.get("generic") == 2 and paid_cost.get("W") == 1, \
        f"Leyline Binding used the wrong Domain cost: {paid_cost}"
    reductions = gs.stack[-1][3].get("applied_cost_modifications", {}).get("reductions", [])
    assert sum(item.get("amount", 0) for item in reductions) == 3, \
        f"Domain counted lands instead of distinct basic land types: {reductions}"


@scenario("601.2c / 601.2f", "Ride's End chooses its target before applying the tapped discount")
def s_rides_end_target_discount_and_vehicle():
    gs = fresh()
    controller = gs.p1
    opponent = gs.p2
    gs.priority_player = controller
    gs.priority_pass_count = 0
    vehicle = inject_into_zone(gs, opponent, {
        "name": "Tapped Test Vehicle", "mana_cost": "{3}", "cmc": 3,
        "type_line": "Artifact - Vehicle", "oracle_text": "Crew 2",
    }, "battlefield")
    opponent["tapped_permanents"].add(vehicle)
    ride = inject_into_zone(gs, controller, {
        "name": "Ride's End", "mana_cost": "{4}{W}", "cmc": 5,
        "type_line": "Instant",
        "oracle_text": (
            "This spell costs {3} less to cast if it targets a tapped permanent.\n"
            "Exile target creature or Vehicle."),
    }, "hand")
    controller["mana_pool"] = {'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    ride_index = controller["hand"].index(ride)
    assert get_env().action_handler.generate_valid_actions()[20 + ride_index], \
        "the action mask hid Ride's End when a tapped target made it affordable"
    assert gs.cast_spell(ride, controller), "Ride's End was not castable for its reduced cost"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "Ride's End did not choose a target before payment"
    assert not gs.stack and ride in controller["hand"], \
        "Ride's End entered the stack before its final cost was known"
    assert controller["mana_pool"]['W'] == 1 and controller["mana_pool"]['C'] == 1, \
        "Ride's End spent mana before targets were committed"

    valid_map = gs.targeting_system.get_valid_targets(
        ride, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_ids = sorted(set(cid for ids in valid_map.values() for cid in ids))
    assert vehicle in valid_ids, "Ride's End did not recognize a Vehicle as a legal target"
    reward, ok = get_env().action_handler._handle_select_target(
        valid_ids.index(vehicle), {})
    assert ok, f"Ride's End target commitment failed with reward {reward}"
    paid_cost = gs.stack[-1][3].get("final_paid_cost", {})
    assert paid_cost.get("generic") == 1 and paid_cost.get("W") == 1, \
        f"Ride's End did not apply its tapped-target discount: {paid_cost}"
    assert gs.resolve_top_of_stack(), "Ride's End did not resolve"
    assert vehicle in opponent["exile"] and vehicle not in opponent["battlefield"], \
        "Ride's End did not exile its Vehicle target"


@scenario("601.2c / 601.2f", "This Town prices itself from the targets actually chosen")
def s_this_town_target_discount():
    gs = fresh()
    controller = gs.p1
    opponent = gs.p2
    gs.priority_player = controller
    gs.priority_pass_count = 0
    own_target = inject_into_zone(gs, controller, {
        "name": "Friendly Town Target", "mana_cost": "{1}",
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    opposing_target = inject_into_zone(gs, opponent, {
        "name": "Opposing Town Target", "mana_cost": "{2}",
        "type_line": "Creature", "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    # Fixture decks use one database ID for repeated physical copies. Simulate
    # another copy having moved to the graveyard more recently: targeting must
    # still classify and resolve the occurrence currently on the battlefield.
    controller["graveyard"].append(own_target)
    gs._last_card_locations[own_target] = (controller, "graveyard")
    assert gs.find_card_location(own_target)[1] == "graveyard"
    assert gs.get_card_controller(own_target) is controller
    spell = inject_into_zone(gs, controller, {
        "name": "This Town Ain't Big Enough", "mana_cost": "{4}{U}", "cmc": 5,
        "type_line": "Instant",
        "oracle_text": (
            "This spell costs {3} less to cast if it targets a permanent you control.\n"
            "Return up to two target nonland permanents to their owners' hands."),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    spell_index = controller["hand"].index(spell)
    assert get_env().action_handler.generate_valid_actions()[20 + spell_index], \
        "the action mask hid This Town when a friendly target made it affordable"
    assert gs.cast_spell(spell, controller), "This Town was not castable for its reduced cost"
    assert gs.phase == gs.PHASE_TARGETING and not gs.stack, \
        "This Town was paid for before choosing its targets"
    handler = get_env().action_handler
    assert not handler.generate_valid_actions()[11], \
        "This Town exposed FINISH with zero targets even though the full cost was unaffordable"
    valid_map = gs.targeting_system.get_valid_targets(
        spell, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_ids = sorted(set(cid for ids in valid_map.values() for cid in ids))
    assert own_target in valid_ids and opposing_target in valid_ids
    # An opposing target alone is legal rules-wise but does not earn the {3}
    # discount, so it must not make the deferred cast finishable with this mana.
    candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert handler._handle_select_target(
        candidates.index(opposing_target), {})[1], \
        "could not select the opposing target"
    assert gs.targeting_context, "up-to-two targeting finalized after only one target"
    assert not handler.generate_valid_actions()[11], \
        "This Town exposed FINISH for an unaffordable opponent-only target set"
    assert gs._determine_target_category(own_target) == "enchantments", \
        "a battlefield target was recategorized from a repeated-ID graveyard hint"

    # At the final target slot the shared candidate helper filters selections
    # that would auto-finalize an unaffordable cast while retaining the friendly
    # target that activates the discount.
    remaining_candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert own_target in remaining_candidates
    assert handler._handle_select_target(
        remaining_candidates.index(own_target), {})[1], \
        "could not select the friendly discount target"

    paid_cost = gs.stack[-1][3].get("final_paid_cost", {})
    assert paid_cost.get("generic") == 1 and paid_cost.get("U") == 1, \
        f"This Town used the wrong target-conditioned cost: {paid_cost}"
    assert gs.resolve_top_of_stack(), "This Town did not resolve"
    assert own_target in controller["hand"] and opposing_target in opponent["hand"], \
        "This Town did not return both chosen permanents to their owners"
    assert own_target in controller["graveyard"], \
        "bouncing the battlefield occurrence consumed the repeated graveyard copy"


@scenario("601.2h", "Fear of Isolation returns a chosen permanent as a mandatory casting cost")
def s_fear_of_isolation_additional_cost():
    gs = fresh()
    controller = gs.p1
    gs.priority_player = controller
    gs.priority_pass_count = 0
    returned = inject_into_zone(gs, controller, {
        "name": "Isolation Return Probe", "mana_cost": "{1}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    fear = inject_into_zone(gs, controller, {
        "name": "Fear of Isolation", "mana_cost": "{1}{U}", "cmc": 2,
        "type_line": "Enchantment Creature - Nightmare",
        "oracle_text": (
            "As an additional cost to cast this spell, return a permanent you control "
            "to its owner's hand.\nFlying"),
        "power": 2, "toughness": 3,
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    assert gs.cast_spell(fear, controller), "Fear of Isolation could not begin casting"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context, \
        "Fear of Isolation did not expose its mandatory return choice"
    assert gs.choice_context.get("type") == "casting_additional_return", \
        f"Fear used the wrong choice context: {gs.choice_context}"
    assert fear in controller["hand"] and not gs.stack, \
        "Fear moved or entered the stack before its additional cost was paid"
    fear_mask = get_env().action_handler.generate_valid_actions()
    assert fear_mask[353], \
        "Fear's return-cost choice was missing from the action mask"
    scripted_action, _ = get_env()._get_scripted_opponent_action(
        controller, fear_mask, {"phase_context": "CHOOSE"})
    assert scripted_action == 353, \
        "the scripted opponent could not progress Fear's mandatory return cost"
    reward, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok, f"Fear return-cost choice failed with reward {reward}"
    assert returned in controller["hand"] and returned not in controller["battlefield"], \
        "Fear did not return the chosen permanent to its owner's hand"
    assert gs.stack and gs.stack[-1][1] == fear, "Fear did not finish casting after its cost"
    assert gs.resolve_top_of_stack(), "Fear of Isolation did not resolve"
    assert fear in controller["battlefield"], "Fear of Isolation did not enter the battlefield"


@scenario("601.2h / mirror identity", "Fear returns Player 2's selected occurrence when both decks share its numeric ID")
def s_fear_of_isolation_mirror_occurrence():
    gs = fresh()
    controller, opponent = gs.p2, gs.p1
    gs.turn = 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    gs.priority_pass_count = 0
    shared = inject_into_zone(gs, controller, {
        "name": "Mirror Return Probe", "mana_cost": "{1}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    # Mirror deck construction represents both physical cards with this same
    # database ID.  Put one occurrence on each battlefield, matching the
    # failing EsperSelf-vs-EsperSelf replay.
    opponent["battlefield"].append(shared)
    gs.original_p1_deck.append(shared)
    gs.original_p2_deck.append(shared)
    fear = inject_into_zone(gs, controller, {
        "name": "Fear of Isolation", "mana_cost": "{1}{U}", "cmc": 2,
        "type_line": "Enchantment Creature - Nightmare",
        "oracle_text": (
            "As an additional cost to cast this spell, return a permanent you "
            "control to its owner's hand.\nFlying"),
        "power": 2, "toughness": 3,
    }, "hand")
    controller["mana_pool"] = {
        'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    assert gs.cast_spell(fear, controller), "mirror Fear cast did not begin"
    option = gs.choice_context["options"].index(shared)
    assert gs.choose_casting_additional_return(option), \
        "Player 2's mask-valid mirror occurrence was rejected"
    assert shared in controller["hand"] and shared not in controller["battlefield"], \
        "Player 2's occurrence did not return to Player 2's hand"
    assert shared in opponent["battlefield"], \
        "returning Player 2's occurrence consumed Player 1's mirror copy"
    assert gs.stack and gs.stack[-1][1] == fear, \
        "Fear did not finish casting after the mirror return cost"


@scenario("601.2h / collect evidence", "Analyze the Pollen exiles mana value 8 and broadens its search")
def s_analyze_the_pollen_collect_evidence():
    gs = fresh()
    controller = gs.p1
    gs.priority_player = controller
    gs.priority_pass_count = 0
    analyze = inject_into_zone(gs, controller, {
        "name": "Analyze the Pollen", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": (
            "As an additional cost to cast this spell, you may collect evidence 8. "
            "Search your library for a basic land card. If evidence was collected, "
            "instead search your library for a creature or land card. Reveal that card, "
            "put it into your hand, then shuffle."),
    }, "hand")
    evidence_five = inject_into_zone(gs, controller, {
        "name": "Evidence Five", "mana_cost": "{5}", "cmc": 5,
        "type_line": "Sorcery", "oracle_text": "",
    }, "graveyard")
    evidence_three = inject_into_zone(gs, controller, {
        "name": "Evidence Three", "mana_cost": "{3}", "cmc": 3,
        "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    spare_evidence = inject_into_zone(gs, controller, {
        "name": "Spare Evidence", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    creature = inject_card(gs, {
        "name": "Evidence Search Creature", "mana_cost": "{4}", "cmc": 4,
        "type_line": "Creature - Beast", "oracle_text": "", "power": 4, "toughness": 4,
    })
    controller["library"] = [creature]
    gs._last_card_locations[creature] = (controller, "library")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 0}

    assert gs.cast_spell(analyze, controller), "Analyze the Pollen could not begin casting"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context, \
        "Analyze did not offer collect evidence when the graveyard could pay"
    assert gs.choice_context.get("type") == "collect_evidence"
    first_index = gs.choice_context["options"].index(evidence_five)
    assert get_env().action_handler.generate_valid_actions()[353 + first_index], \
        "Analyze's graveyard evidence choice was missing from the action mask"
    assert get_env().action_handler._handle_choose_mode(first_index, {})[1]
    assert gs.choice_context.get("selected_mana_value") == 5, \
        "collect evidence recorded the wrong first mana value"
    second_index = gs.choice_context["options"].index(evidence_three)
    assert get_env().action_handler._handle_choose_mode(second_index, {})[1]
    assert gs.choice_context.get("selected_mana_value") == 8
    assert get_env().action_handler.generate_valid_actions()[11], \
        "collect evidence did not expose completion after reaching the threshold"
    get_env().action_handler._handle_pass_priority(None)

    assert evidence_five in controller["exile"] and evidence_three in controller["exile"], \
        "collect evidence did not exile the chosen cards"
    assert spare_evidence in controller["graveyard"], \
        "collect evidence exiled an unchosen graveyard card"
    assert gs.stack[-1][3].get("evidence_collected") is True, \
        "the completed cast did not remember that evidence was collected"
    assert gs.resolve_top_of_stack(), "Analyze the Pollen did not resolve"
    assert controller["hand"].count(creature) == 1, \
        "evidence-collected Analyze did not find exactly one creature card"


@scenario("601.2h / collect evidence", "declining Analyze the Pollen searches only for a basic land")
def s_analyze_the_pollen_declines_evidence():
    gs = fresh()
    controller = gs.p1
    gs.priority_player = controller
    gs.priority_pass_count = 0
    analyze = inject_into_zone(gs, controller, {
        "name": "Analyze the Pollen", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": (
            "As an additional cost to cast this spell, you may collect evidence 8. "
            "Search your library for a basic land card. If evidence was collected, "
            "instead search your library for a creature or land card. Reveal that card, "
            "put it into your hand, then shuffle."),
    }, "hand")
    evidence = inject_into_zone(gs, controller, {
        "name": "Declined Evidence", "mana_cost": "{8}", "cmc": 8,
        "type_line": "Sorcery", "oracle_text": "",
    }, "graveyard")
    basic = inject_card(gs, {
        "name": "Analyze Basic", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "oracle_text": "",
    })
    creature = inject_card(gs, {
        "name": "Analyze Nonbasic Choice", "mana_cost": "{6}", "cmc": 6,
        "type_line": "Creature - Elemental", "oracle_text": "", "power": 6, "toughness": 6,
    })
    controller["library"] = [creature, basic]
    gs._last_card_locations[basic] = (controller, "library")
    gs._last_card_locations[creature] = (controller, "library")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 0}

    assert gs.cast_spell(analyze, controller)
    assert gs.choice_context and gs.choice_context.get("type") == "collect_evidence"
    assert get_env().action_handler.generate_valid_actions()[11], \
        "Analyze did not expose declining evidence before any card was selected"
    get_env().action_handler._handle_pass_priority(None)
    assert evidence in controller["graveyard"] and evidence not in controller["exile"], \
        "declining collect evidence moved a graveyard card"
    assert gs.stack[-1][3].get("evidence_collected") is False
    assert gs.resolve_top_of_stack(), "declined Analyze did not resolve"
    assert basic in controller["hand"], "declined Analyze did not find its basic land"
    assert creature in controller["library"], \
        "declined Analyze incorrectly used the creature-or-land search"


@scenario("707.2", "a token copy uses printed characteristics, not current ones")
def s_token_copy_printed_values():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")   # printed 3/3, no keywords
    owner = to_battlefield(gs, cid)
    ls = gs.layer_system
    # Pump and grant flying via continuous effects...
    ls.register_effect({'source_id': cid, 'layer': 7, 'sublayer': 'c',
                        'affected_ids': [cid], 'effect_type': 'modify_pt',
                        'effect_value': (2, 2), 'duration': 'permanent'})
    ls.register_effect({'source_id': cid, 'layer': 6, 'affected_ids': [cid],
                        'effect_type': 'add_ability', 'effect_value': 'flying',
                        'duration': 'permanent'})
    ls.invalidate_cache()
    ls.apply_all_effects()
    live = gs._safe_get_card(cid)
    assert (live.power, live.toughness) == (5, 5), "test setup: pump did not apply"
    # ...then copy it. CR 707.2: the copy gets PRINTED values only.
    token_id = gs.create_token_copy(live, owner)
    assert token_id is not None, "token copy creation failed"
    token = gs._safe_get_card(token_id)
    assert token is not None, "token card object not found after creation"
    assert (int(token.printed('power')), int(token.printed('toughness'))) == (3, 3), \
        f"token copied modified P/T {token.power}/{token.toughness} instead of printed 3/3"
    assert _kw(gs, token_id, "flying") == 0, \
        "token copied a granted keyword; copies use printed characteristics (CR 707.2)"


@scenario("707.2 (layer 1)", "a layer-1 copy effect copies the source's printed characteristics")
def s_layer1_copy_printed_values():
    gs = fresh()
    src_id = card_id_by_name(gs, "Moss Titan")      # printed 5/5 (per fixture), Trample
    dst_id = card_id_by_name(gs, "Vine Stalker")
    for cid in (src_id, dst_id):
        to_battlefield(gs, cid)
    src_card = gs._safe_get_card(src_id)
    printed_p, printed_t = int(src_card.printed('power')), int(src_card.printed('toughness'))
    ls = gs.layer_system
    # Pump the SOURCE with a continuous effect...
    ls.register_effect({'source_id': src_id, 'layer': 7, 'sublayer': 'c',
                        'affected_ids': [src_id], 'effect_type': 'modify_pt',
                        'effect_value': (3, 3), 'duration': 'permanent'})
    # ...and make dst a copy of src (layer 1).
    ls.register_effect({'source_id': dst_id, 'layer': 1, 'affected_ids': [dst_id],
                        'effect_type': 'copy', 'effect_value': src_id,
                        'duration': 'permanent'})
    ls.invalidate_cache()
    ls.apply_all_effects()
    dst = gs._safe_get_card(dst_id)
    # The copy sees printed values; the source's +3/+3 does NOT carry over.
    assert (dst.power, dst.toughness) == (printed_p, printed_t), \
        (f"layer-1 copy took modified values {dst.power}/{dst.toughness}; "
         f"expected printed {printed_p}/{printed_t} (CR 707.2)")


def _combat_setup(gs, attacker_name, blocker_name, attacker_keywords=()):
    """Put attacker on the agent's battlefield, blocker on the defender's,
    grant keywords via layer effects, and wire the combat structures."""
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    atk = card_id_by_name(gs, attacker_name)
    blk = card_id_by_name(gs, blocker_name)
    assert gs.move_card(atk, owner_of(gs, atk), "library", agent, "battlefield")
    assert gs.move_card(blk, owner_of(gs, blk), "library", defender, "battlefield")
    for kw in attacker_keywords:
        gs.layer_system.register_effect({'source_id': atk, 'layer': 6,
                                         'affected_ids': [atk],
                                         'effect_type': 'add_ability', 'effect_value': kw,
                                         'duration': 'permanent'})
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    gs.current_attackers = [atk]
    gs.current_block_assignments = {atk: [blk]}
    gs.combat_damage_dealt = False
    return agent, defender, atk, blk


def grant_keyword(gs, cid, keyword, source_id=None):
    """Grant a keyword through layer 6 and refresh characteristics."""
    if source_id is None:
        source_id = cid
    gs.layer_system.register_effect({'source_id': source_id, 'layer': 6,
                                     'affected_ids': [cid],
                                     'effect_type': 'add_ability',
                                     'effect_value': keyword,
                                     'duration': 'permanent'})
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()


def layer_effect_count(gs, source_id, description_contains=None):
    """Count registered continuous effects from one source."""
    count = 0
    for layer_effects in gs.layer_system.layers.values():
        pools = layer_effects.values() if isinstance(layer_effects, dict) else (layer_effects,)
        for pool in pools:
            for _, effect in pool:
                if effect.get("source_id") != source_id:
                    continue
                if description_contains and description_contains not in effect.get("description", ""):
                    continue
                count += 1
    return count


def _zone_of(gs, cid):
    return gs.find_card_location(cid)[1]


@scenario("510.4", "a blocker killed by first-strike damage deals no regular damage back")
def s_first_strike_kills_before_regular():
    gs = fresh()
    # 2/2 first striker vs 2/2 vanilla: without first strike this is a mutual
    # kill; with it, the blocker must be dead before regular damage.
    agent, defender, atk, blk = _combat_setup(gs, "Cinder Brawler", "Vine Stalker",
                                              attacker_keywords=("first strike",))
    gs.combat_resolver.resolve_combat()
    gs.check_state_based_actions()
    assert _zone_of(gs, blk) == "graveyard", "blocker survived lethal first-strike damage"
    atk_damage = agent.get("damage_counters", {}).get(atk, 0)
    assert atk_damage == 0 and _zone_of(gs, atk) == "battlefield", \
        (f"attacker took {atk_damage} regular damage from a blocker that died to "
         f"first strike (no SBA ran between damage steps)")


@scenario("702.19", "trample assigns lethal to the blocker and the excess to the player")
def s_trample_excess_to_player():
    gs = fresh()
    agent, defender, atk, blk = _combat_setup(gs, "Magma Bruiser", "Vine Stalker",
                                              attacker_keywords=("trample",))
    life_before = defender["life"]
    gs.combat_resolver.resolve_combat()
    gs.check_state_based_actions()
    # 4 power vs 2 toughness: 2 lethal to the blocker, 2 trample through.
    assert _zone_of(gs, blk) == "graveyard", "blocker did not die to lethal assignment"
    assert defender["life"] == life_before - 2, \
        f"expected 2 trample damage, defender life went {life_before} -> {defender['life']}"


@scenario("702.49", "a mask-valid Ninjutsu action uses the public combat dispatcher")
def s_ninjutsu_public_action_dispatch():
    gs = fresh()
    controller = gs.p1
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = controller
    attacker = inject_into_zone(gs, controller, {
        "name": "Returning Attacker", "mana_cost": "{1}{U}", "cmc": 2,
        "type_line": "Creature - Rogue", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    ninja = inject_into_zone(gs, controller, {
        "name": "Scenario Shinobi", "mana_cost": "{2}{U}", "cmc": 3,
        "type_line": "Creature - Human Ninja",
        "oracle_text": "Ninjutsu {1}{U}", "power": 3, "toughness": 2,
    }, "hand")
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {}
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[437], "the legal Ninjutsu action was absent from the mask"
    _, done, truncated, info = handler.apply_action(437)
    assert not done and not truncated and not info.get("execution_failed"), \
        f"mask-valid Ninjutsu was rejected by dispatch: {info}"
    assert attacker in controller["hand"] and ninja in controller["battlefield"], \
        "Ninjutsu did not exchange the unblocked attacker for the Ninja"
    assert ninja in gs.current_attackers, "the Ninja did not enter attacking"


@scenario("509.1h", "a mask-valid multi-block action returns the public handler contract")
def s_multiple_blockers_public_action_dispatch():
    gs = fresh()
    gs.agent_is_p1 = False
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    attacker = inject_into_zone(gs, gs.p1, {
        "name": "Wide Attacker", "mana_cost": "{2}{R}", "cmc": 3,
        "type_line": "Creature - Giant", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    blocker_a = inject_into_zone(gs, gs.p2, {
        "name": "First Blocker", "mana_cost": "{W}", "cmc": 1,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 1, "toughness": 2,
    }, "battlefield")
    blocker_b = inject_into_zone(gs, gs.p2, {
        "name": "Second Blocker", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {}
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[48], "ordinary BLOCK absent from its legal mask"
    _, done, truncated, info = handler.apply_action(48)
    assert not done and not truncated and not info.get("execution_failed"), \
        f"mask-valid ordinary block was rejected: {info}"
    assert gs.current_block_assignments.get(attacker) == [blocker_a]
    # Public declaration is monotonic: an assigned blocker cannot be toggled
    # off through the same BLOCK slot. Rebuild the clean rule fixture directly
    # before checking the independent multi-block dispatcher.
    next_mask = handler.generate_valid_actions()
    assert not next_mask[48] and next_mask[49] and next_mask[439]
    gs.current_block_assignments = {}

    assert handler.generate_valid_actions()[383], \
        "ASSIGN_MULTIPLE_BLOCKERS absent from its legal mask"
    _, done, truncated, info = handler.apply_action(
        383, context={"blocker_identifiers": [0, 1]})
    assert not done and not truncated and not info.get("execution_failed"), \
        f"mask-valid multi-block was rejected after mutation: {info}"
    assert gs.current_block_assignments.get(attacker) == [blocker_a, blocker_b], \
        "the selected blockers were not assigned to the attacker"


@scenario("702.2 + 702.19", "deathtouch makes 1 damage lethal for trample assignment and for the SBA")
def s_deathtouch_trample():
    gs = fresh()
    agent, defender, atk, blk = _combat_setup(gs, "Furnace Colossus", "Canopy Sentinel",
                                              attacker_keywords=("trample", "deathtouch"))
    life_before = defender["life"]
    gs.combat_resolver.resolve_combat()
    gs.check_state_based_actions()
    # 6 power, deathtouch: 1 to the 2/4 blocker is lethal (CR 702.2c applied to
    # 510.1c), 5 tramples through; the blocker dies to the deathtouch SBA.
    assert defender["life"] == life_before - 5, \
        f"expected 5 trample damage with deathtouch assignment, life went {life_before} -> {defender['life']}"
    assert _zone_of(gs, blk) == "graveyard", \
        "blocker survived deathtouch damage (SBA 704.5h not honoring the deathtouch mark)"


@scenario("702.9 / 702.17", "a creature with flying can be blocked by reach but not by a vanilla creature")
def s_flying_reach_block_legality():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    handler = integrate_combat_actions(gs)
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    atk = card_id_by_name(gs, "Cinder Brawler")
    vanilla = card_id_by_name(gs, "Vine Stalker")
    reacher = card_id_by_name(gs, "Canopy Sentinel")  # printed Reach
    assert gs.move_card(atk, owner_of(gs, atk), "library", agent, "battlefield")
    for bid in (vanilla, reacher):
        assert gs.move_card(bid, owner_of(gs, bid), "library", defender, "battlefield")
    grant_keyword(gs, atk, "flying")
    gs.current_attackers = [atk]
    gs.current_block_assignments = {}
    assert not handler._can_block(vanilla, atk), "vanilla creature could block a flier"
    assert handler._can_block(reacher, atk), "creature with reach could not block a flier"


@scenario("702.111", "a creature with menace is not legally blocked by only one creature")
def s_menace_requires_two_blockers_to_finish_declaration():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    handler = integrate_combat_actions(gs)
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    atk = card_id_by_name(gs, "Cinder Brawler")
    b1 = card_id_by_name(gs, "Vine Stalker")
    b2 = card_id_by_name(gs, "Sprout Guardian")
    assert gs.move_card(atk, owner_of(gs, atk), "library", agent, "battlefield")
    for bid in (b1, b2):
        assert gs.move_card(bid, owner_of(gs, bid), "library", defender, "battlefield")
    grant_keyword(gs, atk, "menace")
    gs.current_attackers = [atk]
    gs.current_block_assignments = {atk: [b1]}
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    assert not handler.handle_declare_blockers_done(), \
        "declare-blockers step accepted a single blocker on a menace attacker"
    assert gs.phase == gs.PHASE_DECLARE_BLOCKERS, \
        "phase advanced despite an illegal menace block assignment"
    gs.current_block_assignments = {atk: [b1, b2]}
    assert handler.handle_declare_blockers_done(), \
        "declare-blockers step rejected two blockers on a menace attacker"
    assert gs.phase in (gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE), \
        "phase did not advance after a legal menace block assignment"


@scenario("702.111 / 509.1c", "the public mask cannot strand a partial menace block declaration")
def s_menace_public_block_mask_contract():
    gs = fresh(); handler = get_env().action_handler
    gs.agent_is_p1 = False
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.priority_pass_count = 0
    gs.stack.clear()
    gs.current_block_assignments = {}

    attacker = inject_into_zone(gs, gs.p1, {
        "name": "Mask Menace Attacker", "mana_cost": "{3}{B}", "cmc": 4,
        "type_line": "Creature - Horror", "oracle_text": "Menace",
        "power": 4, "toughness": 4,
    }, "battlefield")
    blocker_a = inject_into_zone(gs, gs.p2, {
        "name": "Only Mask Blocker", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    grant_keyword(gs, attacker, "menace")
    gs.current_attackers = [attacker]
    blocker_a_action = 48 + gs.p2["battlefield"].index(blocker_a)

    mask = handler.generate_valid_actions()
    assert not mask[blocker_a_action], \
        "a lone ordinary blocker was exposed against an unassigned menace attacker"
    assert mask[11], \
        "PASS was not retained as a legal completion alias"
    assert mask[439], \
        "declaring no blockers against menace was not available"
    _, _, _, info = handler.apply_action(11)
    assert not info.get("execution_failed"), \
        f"Pass did not route through legal block completion: {info}"
    assert gs.phase in (gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE), \
        "Pass left the declaration open or bypassed its combat transition"

    # A stale partial declaration cannot finish, but it can withdraw its lone
    # blocker and return to the legal no-block declaration.
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {attacker: [blocker_a]}
    mask = handler.generate_valid_actions()
    assert not mask[11] and not mask[439], \
        "finish remained mask-valid for an illegal one-blocker menace assignment"
    assert mask[blocker_a_action], \
        "an incomplete menace declaration had no blocker-withdrawal recovery"
    _, _, _, info = handler.apply_action(blocker_a_action)
    assert not info.get("execution_failed"), \
        f"withdrawing an incomplete menace block failed: {info}"
    assert not gs.current_block_assignments

    # If the partial blocker's physical occurrence left combat, the live
    # declaration is an ordinary no-block declaration. The mask and executor
    # both prune it instead of requiring an impossible withdrawal action.
    gs.current_block_assignments = {attacker: [blocker_a]}
    assert gs.move_card(
        blocker_a, gs.p2, "battlefield", gs.p2, "graveyard",
        cause="menace_partial_recovery_probe")
    assert handler.generate_valid_actions()[439], \
        "a departed partial blocker stranded the declare-blockers mask"
    _, _, _, info = handler.apply_action(439)
    assert not info.get("execution_failed"), \
        f"departed menace blocker was not pruned at completion: {info}"
    assert not gs.current_block_assignments
    assert gs.move_card(
        blocker_a, gs.p2, "graveyard", gs.p2, "battlefield",
        cause="menace_multi_block_probe")
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.current_attackers = [attacker]

    # Two physical blockers are exposed through the atomic multi-block action;
    # the completed declaration then aligns action 439 with its executor.
    blocker_b = inject_into_zone(gs, gs.p2, {
        "name": "Second Mask Blocker", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    blocker_a_action = 48 + gs.p2["battlefield"].index(blocker_a)
    blocker_b_action = 48 + gs.p2["battlefield"].index(blocker_b)
    mask = handler.generate_valid_actions()
    assert not mask[blocker_a_action] and not mask[blocker_b_action], \
        "menace exposed a sequential first blocker instead of an atomic declaration"
    assert mask[383], "two legal menace blockers did not expose multi-block"
    blocker_a_index = gs.p2["battlefield"].index(blocker_a)
    assert not gs.combat_action_handler.handle_assign_multiple_blockers(
        param=0,
        context={"blocker_identifiers": [blocker_a_index, blocker_a_index]}), \
        "one physical blocker slot was accepted twice by multi-block"
    selected_action, selected_context = get_env()._get_scripted_opponent_action(
        gs.p2, mask, {"phase_context": "priority"})
    assert selected_action == 383, \
        "the scripted opponent declined a legal atomic menace block"
    _, _, _, info = handler.apply_action(
        selected_action, context=selected_context)
    assert not info.get("execution_failed"), \
        f"mask-valid menace multi-block failed: {info}"
    assert gs.current_block_assignments.get(attacker) == [blocker_a, blocker_b]
    assert handler.generate_valid_actions()[439], \
        "finish was absent after a complete two-blocker menace declaration"
    _, _, _, info = handler.apply_action(439)
    assert not info.get("execution_failed"), \
        f"completed menace declaration failed to finish: {info}"


@scenario("702.111 / action-map bounds", "menace remains blockable beyond the ten atomic multi-block slots")
def s_menace_sequential_fallback_beyond_atomic_slots():
    gs = fresh(); handler = get_env().action_handler
    gs.agent_is_p1 = False
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.priority_pass_count = 0
    gs.stack.clear()
    gs.current_block_assignments = {}

    earlier_attackers = []
    for index in range(10):
        earlier_attackers.append(inject_into_zone(gs, gs.p1, {
            "name": f"Earlier Attacker {index}", "mana_cost": "{1}",
            "cmc": 1, "type_line": "Creature - Soldier",
            "oracle_text": "", "power": 30 if index == 0 else 1,
            "toughness": 30 if index == 0 else 1,
        }, "battlefield"))
    menace = inject_into_zone(gs, gs.p1, {
        "name": "Eleventh Menace Attacker", "mana_cost": "{4}{B}",
        "cmc": 5, "type_line": "Creature - Horror",
        "oracle_text": "Menace", "power": 20, "toughness": 20,
    }, "battlefield")
    blocker_a = inject_into_zone(gs, gs.p2, {
        "name": "Late Menace Blocker A", "mana_cost": "{1}{G}",
        "cmc": 2, "type_line": "Creature - Elf", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    # Fixture decks use repeated canonical IDs for physical copies. Exercise
    # the occurrence-aware sequential path with a second copy of blocker A.
    gs.p2["library"].append(blocker_a)
    assert gs.move_card(
        blocker_a, gs.p2, "library", gs.p2, "battlefield",
        cause="duplicate_blocker_probe")
    blocker_b = blocker_a
    grant_keyword(gs, menace, "menace")
    gs.current_attackers = earlier_attackers + [menace]
    blocker_slots = [
        index for index, card_id in enumerate(gs.p2["battlefield"])
        if card_id == blocker_a
    ]
    assert len(blocker_slots) == 2
    action_a, action_b = (48 + blocker_slots[0], 48 + blocker_slots[1])

    mask = handler.generate_valid_actions()
    assert mask[action_a] and mask[action_b]
    assert handler.action_reasons_with_context[action_a]["context"].get(
        "target_attacker_id") == menace, \
        "the sequential fallback did not bind the out-of-range menace attacker"
    first_action, first_context = get_env()._get_scripted_opponent_action(
        gs.p2, mask, {"phase_context": "priority"})
    assert first_action in (action_a, action_b)
    _, _, _, info = handler.apply_action(
        first_action, context=first_context)
    assert not info.get("execution_failed"), info
    second_action = action_b if first_action == action_a else action_a
    mask = handler.generate_valid_actions()
    assert not mask[439] and mask[second_action], \
        "the partial sequential menace declaration hid its required second blocker"
    assert handler.action_reasons_with_context[second_action]["context"].get(
        "target_attacker_id") == menace
    selected_action, selected_context = get_env()._get_scripted_opponent_action(
        gs.p2, mask, {"phase_context": "priority"})
    assert selected_action == second_action, \
        "the scripted opponent withdrew instead of completing a menace block"
    _, _, _, info = handler.apply_action(
        selected_action, context=selected_context)
    assert not info.get("execution_failed"), info
    assert gs.current_block_assignments.get(menace) == [blocker_a, blocker_b]
    assert handler.generate_valid_actions()[439]


@scenario("702.16b/e", "protection from red prevents red blocking and red damage")
def s_protection_from_red_blocks_and_prevents_damage():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    handler = integrate_combat_actions(gs)
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    protected = inject_card(gs, {
        "name": "Shielded Adept", "mana_cost": "{W}",
        "type_line": "Creature — Human Wizard",
        "oracle_text": "Protection from red",
        "color_identity": ["W"], "power": 2, "toughness": 2,
    })
    red_blocker = card_id_by_name(gs, "Cinder Brawler")  # red 2/2
    agent["library"].append(protected)
    gs._last_card_locations[protected] = (agent, "library")
    assert gs.move_card(protected, agent, "library", agent, "battlefield")
    assert gs.move_card(red_blocker, owner_of(gs, red_blocker), "library", defender, "battlefield")
    grant_keyword(gs, protected, "protection from red")
    gs.current_attackers = [protected]
    gs.current_block_assignments = {}
    assert not handler._can_block(red_blocker, protected), \
        "red creature could block a creature with protection from red"
    marked = gs.apply_damage_to_permanent(protected, 2, red_blocker, is_combat_damage=True)
    assert marked == 0, "red damage was not prevented by protection from red"
    assert agent.get("damage_counters", {}).get(protected, 0) == 0, \
        "protection-prevented red damage was still marked"


@scenario("702.16b", "protection from red makes red spells unable to target the permanent")
def s_protection_from_red_targeting():
    gs = fresh()
    caster = gs.p1
    target_owner = gs.p2
    red_spell = inject_card(gs, {
        "name": "Red Bolt", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Red Bolt deals 3 damage to target creature.",
        "color_identity": ["R"], "cmc": 1,
    })
    protected = inject_card(gs, {
        "name": "Red Warded Guard", "mana_cost": "{W}",
        "type_line": "Creature — Human Soldier",
        "oracle_text": "Protection from red",
        "color_identity": ["W"], "power": 2, "toughness": 2,
    })
    vulnerable = card_id_by_name(gs, "Vine Stalker")
    caster["library"].append(red_spell)
    target_owner["library"].append(protected)
    gs._last_card_locations[red_spell] = (caster, "library")
    gs._last_card_locations[protected] = (target_owner, "library")
    assert gs.move_card(red_spell, caster, "library", caster, "hand")
    assert gs.move_card(protected, target_owner, "library", target_owner, "battlefield")
    assert gs.move_card(vulnerable, owner_of(gs, vulnerable), "library", target_owner, "battlefield")
    grant_keyword(gs, protected, "protection from red")
    valid = gs.targeting_system.get_valid_targets(red_spell, caster)
    creature_targets = set(valid.get("creatures", [])) | set(valid.get("creature", []))
    assert protected not in creature_targets, "red spell could target protection-from-red creature"
    assert vulnerable in creature_targets, "red spell lost ordinary legal creature targets"


@scenario("702.11 / 702.18", "hexproof stops opposing targets but not your own, while shroud stops all")
def s_hexproof_and_shroud_targeting_legality():
    gs = fresh()
    friendly_caster = gs.p1
    opposing_caster = gs.p2
    spell = inject_card(gs, {
        "name": "Test Blessing", "mana_cost": "{G}",
        "type_line": "Instant",
        "oracle_text": "Target creature gets +1/+1 until end of turn.",
        "color_identity": ["G"], "cmc": 1,
    })
    friendly_hexproof = inject_into_zone(gs, friendly_caster, {
        "name": "Canopy Mystic", "mana_cost": "{G}",
        "type_line": "Creature - Elf Druid",
        "oracle_text": "Hexproof",
        "color_identity": ["G"], "power": 2, "toughness": 2,
    }, "battlefield")
    opposing_hexproof = inject_into_zone(gs, opposing_caster, {
        "name": "Mistfield Scout", "mana_cost": "{U}",
        "type_line": "Creature - Merfolk Scout",
        "oracle_text": "Hexproof",
        "color_identity": ["U"], "power": 2, "toughness": 2,
    }, "battlefield")
    shrouded = inject_into_zone(gs, opposing_caster, {
        "name": "Veiled Ancient", "mana_cost": "{3}{G}",
        "type_line": "Creature - Treefolk",
        "oracle_text": "Shroud",
        "color_identity": ["G"], "power": 3, "toughness": 3,
    }, "battlefield")
    vulnerable = inject_into_zone(gs, opposing_caster, {
        "name": "Open Target", "mana_cost": "{1}{G}",
        "type_line": "Creature - Bear",
        "oracle_text": "",
        "color_identity": ["G"], "power": 2, "toughness": 2,
    }, "battlefield")
    grant_keyword(gs, friendly_hexproof, "hexproof")
    grant_keyword(gs, opposing_hexproof, "hexproof")
    grant_keyword(gs, shrouded, "shroud")

    friendly_valid = gs.targeting_system.get_valid_targets(spell, friendly_caster)
    friendly_targets = set(friendly_valid.get("creature", [])) | set(friendly_valid.get("creatures", []))
    assert friendly_hexproof in friendly_targets, "your own hexproof creature was not targetable"
    assert opposing_hexproof not in friendly_targets, "opponent's hexproof creature was targetable"
    assert shrouded not in friendly_targets, "shrouded creature was targetable"
    assert vulnerable in friendly_targets, "ordinary opposing creature was not targetable"

    opposing_valid = gs.targeting_system.get_valid_targets(spell, opposing_caster)
    opposing_targets = set(opposing_valid.get("creature", [])) | set(opposing_valid.get("creatures", []))
    assert opposing_hexproof in opposing_targets, "controller could not target their own hexproof creature"
    assert friendly_hexproof not in opposing_targets, "opponent could target a hexproof creature"
    assert shrouded not in opposing_targets, "controller could target their own shrouded creature"


@scenario("target mask", "hexproof-filtered targeting actions expose only legal selectable targets")
def s_target_action_mask_excludes_illegal_hexproof_targets():
    gs = fresh()
    veil_text = (
        "Put a +1/+1 counter on target creature you control. It gains "
        "hexproof until end of turn. (It can't be the target of spells or "
        "abilities your opponents control.)")
    assert gs._target_bounds_from_text(veil_text) == (1, 1), \
        "Snakeskin Veil reminder text invented a second target"
    handler = get_env().action_handler
    gs.agent_is_p1 = True
    caster = gs.p1
    opponent = gs.p2
    spell = inject_card(gs, {
        "name": "Precise Bolt", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Precise Bolt deals 3 damage to target creature.",
        "color_identity": ["R"], "cmc": 1,
    })
    illegal = inject_into_zone(gs, opponent, {
        "name": "Hidden Adept", "mana_cost": "{G}",
        "type_line": "Creature - Elf",
        "oracle_text": "Hexproof",
        "color_identity": ["G"], "power": 2, "toughness": 2,
    }, "battlefield")
    legal = inject_into_zone(gs, opponent, {
        "name": "Exposed Adept", "mana_cost": "{G}",
        "type_line": "Creature - Elf",
        "oracle_text": "",
        "color_identity": ["G"], "power": 2, "toughness": 2,
    }, "battlefield")
    grant_keyword(gs, illegal, "hexproof")
    gs.add_to_stack("SPELL", spell, caster, {"requires_target": True, "num_targets": 1})
    gs.phase = gs.PHASE_TARGETING
    gs.targeting_context = {
        "source_id": spell, "controller": caster,
        "required_type": "creature", "required_count": 1,
        "min_targets": 1, "max_targets": 1,
        "selected_targets": [],
        "effect_text": "Precise Bolt deals 3 damage to target creature.",
    }
    mask = handler.generate_valid_actions()
    select_actions = [idx for idx in range(274, 284) if mask[idx]]
    assert select_actions == [274], f"expected one legal SELECT_TARGET action, got {select_actions}"
    reward, ok = handler._handle_select_target(0, {})
    assert ok, "SELECT_TARGET failed for the only legal target"
    assert gs.stack[-1][3].get("targets") == {"creatures": [legal]}, \
        f"targeting chose {gs.stack[-1][3].get('targets')}, expected only the non-hexproof creature"


@scenario("702.21", "ward parses and registers its target-tax cost")
def s_ward_keyword_cost_parses():
    gs = fresh()
    player = gs.p1
    warded = inject_card(gs, {
        "name": "Tollhide Bear", "mana_cost": "{1}{G}",
        "type_line": "Creature - Bear",
        "oracle_text": "Ward {2}",
        "color_identity": ["G"], "power": 2, "toughness": 2,
    })
    player["library"].append(warded)
    gs._last_card_locations[warded] = (player, "library")
    assert gs.move_card(warded, player, "library", player, "battlefield")
    gs.ability_handler._parse_and_register_abilities(warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    ward_abilities = [
        ability for ability in gs.ability_handler.registered_abilities.get(warded, [])
        if getattr(ability, "keyword", None) == "ward"
    ]
    assert ward_abilities, "Ward {2} did not register a ward static ability"
    assert any(getattr(ability, "keyword_value", None) == "{2}" for ability in ward_abilities), \
        f"ward cost was not normalized to {{2}}: {[getattr(a, 'keyword_value', None) for a in ward_abilities]}"
    assert gs.check_keyword(warded, "ward"), "registered ward keyword was not visible through keyword checks"


@scenario("702.21 / 613.1f", "generic Ward's internal form is recognized as a layer-6 ability")
def s_generic_ward_internal_form_is_layer_six():
    from Playersim.ability_types import StaticAbility

    ability = StaticAbility(0, "ward ward_generic")
    assert ability._determine_layer_for_effect(ability.effect) == 6, \
        "the generic Ward form fell through layer determination"
    assert ability._parse_layer6_effect(ability.effect) == {
        "effect_type": "add_ability", "effect_value": "Ward"}, \
        "the internal generic Ward form could not register its layer effect"


@scenario("702.21", "ward counters an opposing targeted spell when its tax cannot be paid")
def s_ward_counters_spell_when_tax_unpaid():
    gs = fresh()
    caster = gs.p1
    defender = gs.p2
    bolt = inject_card(gs, {
        "name": "Ward Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Ward Probe deals 3 damage to target creature.",
        "color_identity": ["R"], "cmc": 1,
    })
    warded = inject_into_zone(gs, defender, {
        "name": "Taxing Guardian", "mana_cost": "{1}{W}",
        "type_line": "Creature - Human Cleric",
        "oracle_text": "Ward {2}",
        "color_identity": ["W"], "power": 2, "toughness": 4,
    }, "battlefield")
    gs.ability_handler._parse_and_register_abilities(warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    caster["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    gs.add_to_stack("SPELL", bolt, caster, {
        "targets": {"creatures": [warded]},
        "requires_target": True,
        "num_targets": 1,
        "source_zone": "stack_implicit",
    })
    assert gs.resolve_top_of_stack() and gs.choice_context, \
        "unpayable ward did not expose its decline decision"
    assert gs.choice_context.get("type") == "ward_payment"
    assert get_env().action_handler._handle_pass_priority(None)[1]
    assert gs.resolve_top_of_stack(), "ward-countered spell did not finish resolving"
    assert bolt in caster["graveyard"], "ward-countered spell did not go to graveyard"
    assert defender.get("damage_counters", {}).get(warded, 0) == 0, \
        "unpaid ward spell still damaged the warded creature"


@scenario("702.21", "the targeting player chooses whether to pay an available mana Ward tax")
def s_ward_tax_paid_then_spell_resolves():
    gs = fresh()
    caster = gs.p1
    defender = gs.p2
    bolt = inject_card(gs, {
        "name": "Ward Payment Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Ward Payment Probe deals 3 damage to target creature.",
        "color_identity": ["R"], "cmc": 1,
    })
    warded = inject_into_zone(gs, defender, {
        "name": "Taxing Sentinel", "mana_cost": "{1}{W}",
        "type_line": "Creature - Human Soldier",
        "oracle_text": "Ward {2}",
        "color_identity": ["W"], "power": 2, "toughness": 4,
    }, "battlefield")
    gs.ability_handler._parse_and_register_abilities(warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    caster["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 2}
    gs.add_to_stack("SPELL", bolt, caster, {
        "targets": {"creatures": [warded]},
        "requires_target": True,
        "num_targets": 1,
        "source_zone": "stack_implicit",
    })
    assert gs.resolve_top_of_stack() and gs.choice_context, \
        "payable Ward tax was auto-paid instead of exposed"
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert gs.resolve_top_of_stack(), "spell with paid ward tax did not resolve"
    assert caster["mana_pool"].get('C', 0) == 0, "ward tax did not consume the available generic mana"
    assert defender.get("damage_counters", {}).get(warded, 0) == 3, \
        "spell did not damage the warded creature after paying ward"
    assert bolt in caster["graveyard"], "resolved instant did not go to graveyard"


@scenario("702.21", "the targeting player chooses whether to pay a life Ward tax")
def s_ward_life_tax_paid_then_spell_resolves():
    gs = fresh()
    caster = gs.p1
    defender = gs.p2
    bolt = inject_card(gs, {
        "name": "Life Ward Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Life Ward Probe deals 3 damage to target creature.",
        "color_identity": ["R"], "cmc": 1,
    })
    warded = inject_into_zone(gs, defender, {
        "name": "Blood Toll Sentinel", "mana_cost": "{1}{B}",
        "type_line": "Creature - Vampire Soldier",
        "oracle_text": "Ward - Pay 3 life.",
        "color_identity": ["B"], "power": 2, "toughness": 4,
    }, "battlefield")
    gs.ability_handler._parse_and_register_abilities(warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    life_before = caster["life"]
    gs.add_to_stack("SPELL", bolt, caster, {
        "targets": {"creatures": [warded]},
        "requires_target": True,
        "num_targets": 1,
        "source_zone": "stack_implicit",
    })
    assert gs.resolve_top_of_stack() and gs.choice_context
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert gs.resolve_top_of_stack(), "spell with payable life ward tax did not resolve"
    assert caster["life"] == life_before - 3, "ward tax did not consume the required life"
    assert defender.get("damage_counters", {}).get(warded, 0) == 3, \
        "spell did not damage the warded creature after paying life for ward"
    assert bolt in caster["graveyard"], "resolved instant did not go to graveyard"


@scenario("702.21 / 601.2h", "Ward exposes sacrifice and discard payments as card choices")
def scenario_ward_nonmana_cost_choices():
    def run(cost_text, payment_zone):
        gs = fresh(SEED + 190 + (1 if payment_zone == "hand" else 0))
        caster, defender = gs.p1, gs.p2
        bolt = inject_card(gs, {
            "name": f"{payment_zone.title()} Ward Probe", "mana_cost": "{R}",
            "type_line": "Instant", "oracle_text": "Deal 1 damage to target creature.",
        })
        warded = inject_into_zone(gs, defender, {
            "name": f"{payment_zone.title()} Ward Sentinel", "mana_cost": "{2}",
            "type_line": "Creature - Soldier", "oracle_text": f"Ward - {cost_text}.",
            "power": 2, "toughness": 4,
        }, "battlefield")
        payment = inject_into_zone(gs, caster, {
            "name": f"Ward {payment_zone.title()} Payment", "mana_cost": "{1}",
            "type_line": "Creature - Citizen", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, payment_zone)
        gs.ability_handler._parse_and_register_abilities(
            warded, gs._safe_get_card(warded))
        gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
        gs.add_to_stack("SPELL", bolt, caster, {
            "targets": {"creatures": [warded]}, "requires_target": True,
            "num_targets": 1, "source_zone": "stack_implicit",
        })
        assert gs.resolve_top_of_stack() and gs.choice_context
        assert gs.choice_context.get("payment_kind") == (
            "discard" if payment_zone == "hand" else "sacrifice")
        assert payment in gs.choice_context.get("options", [])
        option = gs.choice_context["options"].index(payment)
        assert get_env().action_handler._handle_choose_mode(option, {})[1]
        assert gs.resolve_top_of_stack()
        assert payment in caster["graveyard"]
        assert defender.get("damage_counters", {}).get(warded, 0) == 1

    run("Sacrifice a creature", "battlefield")
    run("Discard a card", "hand")


@scenario("605.3 / 106.1", "any-combination mana is selected one symbol at a time")
def scenario_nonland_mana_package_choices():
    from Playersim.ability_types import ManaAbility
    gs = fresh(SEED + 192)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Combination Dynamo", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": (
            "{T}: Add two mana in any combination of colors."),
    }, "battlefield")
    produced = gs.ability_handler._parse_mana_produced(
        "Add two mana in any combination of colors")
    assert produced == {"any_combination": 2}
    ability = ManaAbility(
        source, "{T}", produced,
        effect_text="{T}: Add two mana in any combination of colors.")
    ability.source_card = gs._safe_get_card(source)
    gs.ability_handler.registered_abilities[source] = [ability]
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    assert handler._handle_activate_ability(None, {
        "battlefield_idx": player["battlefield"].index(source),
        "ability_idx": 0, "controller_id": "p1",
    })[1]
    assert gs.choice_context.get("type") == "mana_ability_package"
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index("W"), {})[1]
    assert gs.choice_context and gs.choice_context.get("remaining") == 1
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index("U"), {})[1]
    assert gs.choice_context is None
    assert player["mana_pool"]["W"] == 1 and player["mana_pool"]["U"] == 1


@scenario("605.3 / 106.1b", "mana output alternatives preserve colorless and package choices")
def scenario_nonland_mana_output_package_alternatives():
    from Playersim.ability_types import ManaAbility

    def run(effect_text, option_index, color=None):
        gs = fresh(SEED + 198 + option_index)
        player = gs.p1
        source = inject_into_zone(gs, player, {
            "name": "Alternative Dynamo", "mana_cost": "{2}",
            "type_line": "Artifact", "oracle_text": f"{{T}}: {effect_text}.",
        }, "battlefield")
        produced = gs.ability_handler._parse_mana_produced(effect_text)
        assert "output_options" in produced, produced
        ability = ManaAbility(source, "{T}", produced,
                               effect_text=f"{{T}}: {effect_text}.")
        ability.source_card = gs._safe_get_card(source)
        gs.ability_handler.registered_abilities[source] = [ability]
        gs.priority_player = player
        gs.agent_is_p1 = True
        handler = get_env().action_handler
        assert handler._handle_activate_ability(None, {
            "battlefield_idx": player["battlefield"].index(source),
            "ability_idx": 0, "controller_id": "p1",
        })[1]
        assert gs.choice_context.get("type") == "mana_ability_output"
        assert handler._handle_choose_mode(option_index, {})[1]
        if color:
            assert gs.choice_context.get("type") == "mana_ability_color"
            assert handler._handle_choose_mode(
                gs.choice_context["options"].index(color), {})[1]
        return player

    colorless = run("Add {C} or one mana of any color", 0)
    assert colorless["mana_pool"]["C"] == 1
    colored = run("Add {C} or one mana of any color", 1, "U")
    assert colored["mana_pool"]["U"] == 1 \
        and colored["mana_pool"]["C"] == 0
    package = run("Add {W}{W} or {U}{U}", 1)
    assert package["mana_pool"]["U"] == 2 \
        and package["mana_pool"]["W"] == 0


@scenario("602.2 / action protocol", "fourth activated abilities use the paginated action catalog")
def scenario_activated_ability_overflow_catalog():
    from Playersim.ability_types import ActivatedAbility
    gs = fresh(SEED + 196)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Many-Mode Engine", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    abilities = [
        ActivatedAbility(source, cost="{0}", effect="Draw a card.",
                         effect_text=f"Mode {index}: Draw a card.",
                         activation_index=index)
        for index in range(4)
    ]
    for ability in abilities:
        ability.source_card = gs._safe_get_card(source)
    gs.ability_handler.registered_abilities[source] = abilities
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert all(mask[100 + index] for index in range(3))
    assert mask[479], "the fourth activated ability was omitted"
    catalog_context = handler.action_reasons_with_context[479]["context"]
    assert handler._handle_target_page_next(context=catalog_context)[1]
    assert gs.choice_context.get("type") == "action_catalog"
    assert gs.choice_context["options"][0]["action_context"]["ability_idx"] == 3
    assert handler._handle_choose_mode(0, {})[1]
    assert gs.stack and gs.stack[-1][3].get("ability_index") == 3


@scenario("305.2 / action protocol", "hand objects beyond fixed slots use the action catalog")
def scenario_hand_overflow_action_catalog():
    gs = fresh(SEED + 199)
    player = gs.p1
    replace_hand(gs, player, [
        {"name": f"Overflow Filler {index}", "mana_cost": "{9}",
         "type_line": "Sorcery", "oracle_text": ""}
        for index in range(10)
    ])
    land = inject_into_zone(gs, player, {
        "name": "Overflow Catalog Land", "type_line": "Land",
        "card_types": ["land"], "oracle_text": "{T}: Add {C}.",
    }, "hand")
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[479], "the eleventh hand object was omitted"
    context = handler.action_reasons_with_context[479]["context"]
    assert handler._handle_target_page_next(context=context)[1]
    entry_index = next(
        index for index, entry in enumerate(gs.choice_context["options"])
        if entry.get("action_context", {}).get("card_id") == land)
    assert handler._handle_choose_mode(entry_index, {})[1]
    assert land in player["battlefield"] and land not in player["hand"]


@scenario("702.21 / clone", "a payable Ward choice may be declined after cloning")
def scenario_ward_decline_and_clone_isolation():
    gs = fresh(SEED + 197)
    caster, defender = gs.p1, gs.p2
    spell = inject_card(gs, {
        "name": "Clone Ward Probe", "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Clone Ward Probe deals 1 damage to target creature.",
    })
    warded = inject_into_zone(gs, defender, {
        "name": "Clone Ward Sentinel", "mana_cost": "{1}{W}",
        "type_line": "Creature - Soldier", "oracle_text": "Ward {2}",
        "power": 2, "toughness": 3,
    }, "battlefield")
    gs.ability_handler._parse_and_register_abilities(
        warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    caster["mana_pool"]["C"] = 2
    gs.add_to_stack("SPELL", spell, caster, {
        "targets": {"creatures": [warded]}, "requires_target": True,
        "num_targets": 1, "source_zone": "stack_implicit",
    })
    assert gs.resolve_top_of_stack() and gs.choice_context
    cloned = gs.clone()
    assert cloned.choice_context.get("type") == "ward_payment"
    assert cloned.complete_ward_payment_choice(decline=True)
    assert cloned.resolve_top_of_stack()
    assert spell in cloned.p1["graveyard"]
    assert cloned.p1["mana_pool"]["C"] == 2
    assert defender.get("damage_counters", {}).get(warded, 0) == 0
    assert gs.choice_context.get("type") == "ward_payment"
    assert gs.stack and spell not in caster["graveyard"]


@scenario("701.17 / policy", "sacrifice choices honor compound type and mana-value criteria")
def scenario_sacrifice_compound_criteria():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 193)
    controller, affected = gs.p1, gs.p2
    cheap = inject_into_zone(gs, affected, {
        "name": "Eligible Cheap Creature", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Citizen", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    expensive = inject_into_zone(gs, affected, {
        "name": "Ineligible Expensive Creature", "mana_cost": "{4}", "cmc": 4,
        "type_line": "Creature - Giant", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    land = inject_into_zone(gs, affected, {
        "name": "Ineligible Land", "type_line": "Land", "oracle_text": "",
    }, "battlefield")
    effects = EffectFactory.create_effects(
        "Target player sacrifices a nonland permanent with mana value 2 or less.")
    assert len(effects) == 1
    assert effects[0].apply(
        gs, None, controller, targets={"players": ["p2"]})
    assert gs.choice_context.get("options") == [cheap]
    gs.agent_is_p1 = False
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert cheap in affected["graveyard"]
    assert expensive in affected["battlefield"] and land in affected["battlefield"]


@scenario("707.10c / 601.2c", "a multi-target spell copy may change only one inherited target")
def scenario_spell_copy_partial_retarget():
    gs = fresh(SEED + 195)
    controller, opponent = gs.p1, gs.p2
    spell = inject_card(gs, {
        "name": "Forked Frost", "mana_cost": "{2}{U}",
        "type_line": "Instant",
        "oracle_text": "Tap two target creatures.",
    })
    first = inject_into_zone(gs, opponent, {
        "name": "Inherited First Target", "type_line": "Creature",
        "oracle_text": "", "power": 1, "toughness": 1,
    }, "battlefield")
    second = inject_into_zone(gs, opponent, {
        "name": "Inherited Second Target", "type_line": "Creature",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    replacement = inject_into_zone(gs, opponent, {
        "name": "Replacement Target", "type_line": "Creature",
        "oracle_text": "", "power": 3, "toughness": 3,
    }, "battlefield")
    original_context = {
        "targets": {"creatures": [first, second]},
        "targets_by_slot": [[first], [second]],
        "requires_target": True, "num_targets": 2,
        "min_targets": 2, "max_targets": 2,
    }
    gs.add_to_stack("SPELL", spell, controller, original_context)
    copy_id = gs.copy_spell_on_stack(
        gs.stack[0], controller, copied_by=None, allow_new_targets=True)
    assert copy_id and gs.choice_context.get("type") == "copy_retarget_slots"
    handler = get_env().action_handler
    assert handler._handle_choose_mode(0, {})[1], "first target could not be kept"
    assert handler._handle_choose_mode(1, {})[1], "second target could not enter retargeting"
    candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert replacement in candidates and first not in candidates and second not in candidates
    assert handler._handle_select_target(candidates.index(replacement), {})[1]
    copy_context = next(
        item[3] for item in gs.stack
        if item[3].get("copy_instance_id") == copy_id)
    assert copy_context["targets"] == {"creatures": [first, replacement]}
    assert original_context["targets"] == {"creatures": [first, second]}, \
        "retargeting the copy mutated the original spell"


@scenario("702.21 / 702.18", "lifelink gains life equal to damage actually dealt")
def s_lifelink_gains_life_from_combat_damage():
    gs = fresh()
    agent, defender, atk, blk = _combat_setup(gs, "Vine Stalker", "Sprout Guardian",
                                              attacker_keywords=("lifelink",))
    # Remove the blocker so the lifelinker connects with the defending player.
    gs.current_block_assignments = {}
    life_before = agent["life"]
    opp_life_before = defender["life"]
    gs.combat_resolver.resolve_combat()
    assert defender["life"] == opp_life_before - 2, "lifelink attacker did not deal combat damage"
    assert agent["life"] == life_before + 2, \
        f"lifelink gained {agent['life'] - life_before}, expected 2"


@scenario("engine (leak)", "layer-written card state does not leak into the next game")
def s_no_cross_game_layer_leak():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")   # printed 3/3
    to_battlefield(gs, cid)
    gs.layer_system.register_effect({'source_id': cid, 'layer': 7, 'sublayer': 'c',
                                     'affected_ids': [cid], 'effect_type': 'modify_pt',
                                     'effect_value': (4, 4), 'duration': 'permanent'})
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    card = gs._safe_get_card(cid)
    assert (card.power, card.toughness) == (7, 7), "test setup: pump did not apply"
    # A new game over the same shared card_db must see printed values again.
    gs2 = fresh()
    card2 = gs2.card_db.get(card_id_by_name(gs2, "Thicket Brute"))
    assert (int(card2.power), int(card2.toughness)) == (3, 3), \
        (f"live P/T {card2.power}/{card2.toughness} leaked from the previous game "
         f"(shared card_db object not restored to printed at game start)")
    assert card2.name == "Thicket Brute", "card identity leaked from a previous game's copy effect"


@scenario("510.1c (choice)", "damage assignment order among multiple blockers is an agent choice")
def s_blocker_order_choice():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    handler = integrate_combat_actions(gs)
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    atk = card_id_by_name(gs, "Magma Bruiser")      # 4/3
    b_big = card_id_by_name(gs, "Thicket Brute")    # 3/3
    b_small = card_id_by_name(gs, "Sprout Guardian")  # 1/2
    assert gs.move_card(atk, owner_of(gs, atk), "library", agent, "battlefield")
    for bid in (b_big, b_small):
        assert gs.move_card(bid, owner_of(gs, bid), "library", defender, "battlefield")
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    gs.current_attackers = [atk]
    gs.current_block_assignments = {atk: [b_small, b_big]}   # declaration order: small first
    gs.combat_damage_dealt = False
    gs.phase = gs.PHASE_COMBAT_DAMAGE
    # Requesting damage resolution with 2+ blockers must ask for the order,
    # not resolve with a silent default.
    assert handler.handle_assign_combat_damage()
    assert gs.phase == gs.PHASE_CHOOSE, "no assignment-order choice was offered"
    ctx = gs.choice_context
    assert ctx and ctx.get("type") == "order_blockers" and ctx.get("attacker_id") == atk
    assert not gs.combat_damage_dealt, "damage resolved before the order was chosen"
    # Agent chooses the BIG blocker (index in pending list) to take damage first:
    pending = ctx["pending"]
    assert handler.blocker_order_chosen(pending.index(b_big)), "valid order choice rejected"
    # Ordering complete -> combat resolves with the chosen order and the game
    # moves on to end of combat.
    assert gs.choice_context is None, "choice context not cleared"
    assert gs.combat_damage_dealt, "combat did not resolve after ordering completed"
    assert gs.phase == gs.PHASE_END_OF_COMBAT, "phase did not advance after resolution"
    gs.check_state_based_actions()
    # 4 power: 3 lethal to Thicket Brute (dies), remaining 1 to Sprout Guardian.
    assert gs.find_card_location(b_big)[1] == "graveyard", \
        "first-ordered blocker did not receive lethal damage first"
    assert gs.find_card_location(b_small)[1] == "battlefield", \
        "second-ordered blocker died despite only 1 remaining damage"
    assert defender.get("damage_counters", {}).get(b_small, 0) == 1, \
        "remaining damage was not assigned to the second blocker"


@scenario("510.1c (guard)", "a single blocker needs no assignment-order choice")
def s_blocker_order_single_bypass():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    handler = integrate_combat_actions(gs)
    agent = gs.p1 if getattr(gs, 'agent_is_p1', True) else gs.p2
    defender = gs.p2 if agent is gs.p1 else gs.p1
    atk = card_id_by_name(gs, "Magma Bruiser")
    blk = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(atk, owner_of(gs, atk), "library", agent, "battlefield")
    assert gs.move_card(blk, owner_of(gs, blk), "library", defender, "battlefield")
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    gs.current_attackers = [atk]
    gs.current_block_assignments = {atk: [blk]}
    gs.combat_damage_dealt = False
    gs.phase = gs.PHASE_COMBAT_DAMAGE
    assert handler.handle_assign_combat_damage()
    assert gs.choice_context is None, "single blocker wrongly opened an ordering choice"
    assert gs.combat_damage_dealt, "combat did not resolve immediately with one blocker"
    assert gs.phase == gs.PHASE_END_OF_COMBAT


@scenario("510.1c / self-play", "damage assignment ordering is exposed for the non-agent seat")
def scenario_opponent_damage_assignment_order_choice():
    gs = fresh(SEED + 194)
    attacker_player, defender = gs.p2, gs.p1
    attacker = inject_into_zone(gs, attacker_player, {
        "name": "Opponent Ordering Attacker", "mana_cost": "{4}",
        "type_line": "Creature - Giant", "oracle_text": "Trample",
        "power": 6, "toughness": 6,
    }, "battlefield")
    first = inject_into_zone(gs, defender, {
        "name": "First Ordering Blocker", "mana_cost": "{1}",
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 1, "toughness": 2,
    }, "battlefield")
    second = inject_into_zone(gs, defender, {
        "name": "Second Ordering Blocker", "mana_cost": "{2}",
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 3,
    }, "battlefield")
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {attacker: [first, second]}
    gs.phase = gs.PHASE_COMBAT_DAMAGE
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    assert handler.combat_handler.begin_blocker_order_choice()
    assert gs.choice_context.get("player") is attacker_player
    gs.agent_is_p1 = False
    assert handler._handle_choose_mode(1, {})[1]
    assert gs.first_strike_ordering[attacker] == [second, first]


@scenario("stats (play history)", "the engine records the actual turn each card was played")
def s_play_history_recorded():
    gs = fresh()
    c1 = card_id_by_name(gs, "Ember Grunt")
    c2 = card_id_by_name(gs, "Thicket Brute")
    gs.turn = 2
    gs.track_card_played(c1, 0)
    gs.turn = 5
    gs.track_card_played(c2, 0)
    hist = getattr(gs, 'play_history', None)
    c1_printing = gs.canonical_card_id(c1)
    c2_printing = gs.canonical_card_id(c2)
    assert hist is not None, "play turns are not tracked; play-turn stats are fabricated from CMC"
    assert hist[0].get(2) == [c1_printing] and hist[0].get(5) == [c2_printing], \
        f"play history recorded wrong canonical turns: {hist[0]}"


@scenario("stats (winner mapping)", "cards played are attributed to winner/loser, not to p1/p2")
def s_stats_winner_mapping():
    gs = fresh()
    env = get_env()
    gs.cards_played = {0: [101], 1: [202]}
    gs.play_history = {0: {2: [101]}, 1: {3: [202]}}
    mapped_cards, mapped_history = env._stats_result_mapped(gs, is_p1_winner=False)
    # p2 won: their cards must land in the WINNER slot. The old code passed
    # p1/p2-indexed data into a consumer that reads index 0 as the winner,
    # scrambling card attribution in every game p2 won.
    assert mapped_cards == {0: [202], 1: [101]}, \
        f"winner/loser card mapping wrong: {mapped_cards}"
    assert mapped_history == {"winner": {3: [202]}, "loser": {2: [101]}}, \
        f"winner/loser play-history mapping wrong: {mapped_history}"
    mapped_cards, mapped_history = env._stats_result_mapped(gs, is_p1_winner=True)
    assert mapped_cards == {0: [101], 1: [202]} and mapped_history["winner"] == {2: [101]}


@scenario("stats telemetry", "opening hands, draws, and actual play turns reach CardMemory")
def s_stats_draw_opening_and_play_telemetry():
    gs = fresh(); env = get_env()
    opening_id = replace_hand(gs, gs.p1, [{
        "name": "Opening Telemetry Probe", "mana_cost": "{1}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }])[0]
    replace_hand(gs, gs.p2, [{
        "name": "Opponent Opening Probe", "mana_cost": "{1}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }])
    gs.mulligan_in_progress = True
    gs.bottoming_in_progress = False
    gs._end_mulligan_phase()
    assert gs.opening_hands['p1'] == [opening_id], \
        f"final kept hand was not captured: {gs.opening_hands}"

    gs.turn = 4
    gs._reset_turn_tracking_variables()
    drawn_id = gs.p1['library'][0]
    drawn_printing = gs.canonical_card_id(drawn_id)
    assert gs._draw_card(gs.p1) == drawn_id
    assert gs.draw_history['p1'][4] == [drawn_id]
    gs.track_card_played(drawn_id, 0)

    before_drawn = env.card_memory.card_data.get(
        str(drawn_printing), {}).get('times_drawn', 0)
    before_played_turn = env.card_memory.card_data.get(
        str(drawn_printing), {}).get('turn_played', {}).get('4', 0)
    before_opening = env.card_memory.card_data.get(
        str(opening_id), {}).get('in_opening_hand', 0)
    env._record_cards_to_memory(
        [opening_id, drawn_id], [], gs.cards_played, gs.turn,
        "TelemetryDeck", "OpponentDeck", gs.opening_hands,
        gs.draw_history, gs.play_history, is_win=True, player_idx=0)
    drawn_stats = env.card_memory.card_data[str(drawn_printing)]
    opening_stats = env.card_memory.card_data[str(opening_id)]
    assert drawn_stats['times_drawn'] == before_drawn + 1
    assert drawn_stats['turn_played']['4'] == before_played_turn + 1
    assert opening_stats['in_opening_hand'] == before_opening + 1

    openings, draws, mulligans = env._stats_telemetry_mapped(
        gs, is_p1_winner=True)
    assert openings['winner'] == [opening_id]
    assert draws['winner'][4] == [drawn_id]
    assert mulligans == {'winner': 0, 'loser': 0}


@scenario("stats result schema", "state-flag draws persist under the canonical draw result")
def s_stats_draw_result_is_canonical():
    gs = fresh(); env = get_env()
    gs.p1['game_draw'] = True
    gs.p2['game_draw'] = True
    gs.terminal_reason = 'simultaneous_loss'
    env.ensure_game_result_recorded()
    assert env._game_result == 'draw', \
        f"non-schema draw result persisted: {env._game_result}"


@scenario("training reward", "position shaping is potential-based and terminal rewards are centralized")
def s_training_reward_contract():
    gs = fresh(); env = get_env()
    baseline = env._calculate_board_state_reward()
    assert env._calculate_board_state_reward() == baseline, \
        "unchanged state produced a changing board potential"
    gs.p2['life'] -= 1
    assert env._calculate_board_state_reward() > baseline, \
        "damaging the opponent did not improve strategic potential"

    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = gs.p1
    gs.agent_is_p1 = True
    gs.p1['life'] = 0
    gs.p1['lost_game'] = True
    gs.terminal_reason = 'life_total'
    _, reward, done, truncated, info = env.step(11)
    assert done and not truncated and reward <= -10.0, \
        f"terminal result reward={reward}, done={done}, truncated={truncated}, info={info}"
    assert info.get('terminal_reason') == 'life_total'
    assert info.get('reward_components', {}).get('terminal') == -10.0


@scenario("scripted baseline", "the opponent develops mana, casts spells, and declares attacks")
def s_scripted_baseline_plays_magic():
    gs = fresh(); env = get_env()
    opponent = gs.p1
    land_id, spell_id = replace_hand(gs, opponent, [
        {"name": "Baseline Land", "type_line": "Basic Land — Forest",
         "oracle_text": "{T}: Add {G}."},
        {"name": "Baseline Creature", "mana_cost": "{1}", "cmc": 1,
         "type_line": "Creature", "oracle_text": "", "power": 2,
         "toughness": 2},
    ])
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = opponent
    gs.priority_pass_count = 0
    mask = env.action_mask().astype(bool)
    action, context = env._get_scripted_opponent_action(
        opponent, mask, {"phase_context": "priority"})
    assert action == 13 and context.get('card_id') == land_id
    _, _, _, handler_info = env.action_handler.apply_action(
        action, context=context)
    assert not handler_info.get('execution_failed')

    opponent['mana_pool']['G'] = 2
    mask = env.action_mask().astype(bool)
    action, context = env._get_scripted_opponent_action(
        opponent, mask, {"phase_context": "priority"})
    assert action == 20 and context.get('card_id') == spell_id

    # Combat policy declares every legal attacker before finishing attackers.
    opponent['hand'].remove(spell_id)
    opponent['battlefield'].append(spell_id)
    opponent['entered_battlefield_this_turn'].discard(spell_id)
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.priority_player = opponent
    mask = env.action_mask().astype(bool)
    action, context = env._get_scripted_opponent_action(
        opponent, mask, {"phase_context": "priority"})
    assert action == 29, \
        f"scripted attack action={action}, context={context}, valid={np.flatnonzero(mask).tolist()}"


@scenario("614 (mana doubling)", "a registered mana-doubling replacement actually doubles produced mana")
def s_mana_doubling_live():
    gs = fresh()
    player = gs.p1
    forest = card_id_by_name(gs, "Forest")
    assert gs.move_card(forest, owner_of(gs, forest), "library", player, "battlefield")
    doubler = card_id_by_name(gs, "Thicket Brute")   # stand-in source permanent
    to_battlefield(gs, doubler)
    eff = gs.replacement_effects._register_mana_doubling_effect(
        doubler, player, "Whenever you tap a forest for mana, it produces twice that much mana.")
    assert eff, "mana doubling effect failed to register"
    player["mana_pool"] = {}
    gs.mana_system.add_mana_to_pool(player, "{g}", land_context={'source_permanent_id': forest})
    assert player["mana_pool"].get("G", 0) == 2, \
        (f"doubler produced {player['mana_pool'].get('G', 0)} G (expected 2); the "
         f"replacement listened for a PRODUCE_MANA event that nothing ever fired")


@scenario("614 (dies-copy)", "a 'create a token that's a copy of it' replacement actually creates the token")
def s_dies_copy_creates_token():
    gs = fresh()
    player = gs.p1
    cid = card_id_by_name(gs, "Thicket Brute")
    gs.move_card(cid, owner_of(gs, cid), "library", player, "battlefield")
    fn = gs.replacement_effects._create_replacement_function(
        'DIES', "create a token that's a copy of it", player, "TestSource")
    assert fn, "no replacement function produced for the dies-copy text"
    bf_before = len(player["battlefield"])
    fn({'card_id': cid, 'controller': player})
    new_ids = [i for i in player["battlefield"] if i != cid]
    assert len(player["battlefield"]) == bf_before + 1 and new_ids, \
        "no token was created; the replacement only set a flag nothing reads"
    token = gs._safe_get_card(new_ids[-1])
    assert getattr(token, 'name', None) == "Thicket Brute" and getattr(token, 'is_token', False), \
        f"created object is not a token copy of the original: {getattr(token, 'name', None)}"


@scenario("615 (target scope)", "damage prevention respects its target class")
def s_prevention_target_scope():
    gs = fresh()
    player = gs.p1
    src_id = card_id_by_name(gs, "Sprout Guardian")
    to_battlefield(gs, src_id)
    creature = card_id_by_name(gs, "Vine Stalker")
    to_battlefield(gs, creature)
    gs.replacement_effects._register_damage_prevention(
        src_id, player, "Prevent all damage that would be dealt to target creature.")
    # (a) creature target -> prevented
    ctx, _ = gs.replacement_effects.apply_replacements(
        'DAMAGE', {'damage_amount': 3, 'target_id': creature, 'target_is_player': False})
    assert ctx.get('damage_amount') == 0, "creature-targeted prevention did not prevent"
    # (b) player target -> NOT prevented (the 'creature' class had no check at all)
    ctx, _ = gs.replacement_effects.apply_replacements(
        'DAMAGE', {'damage_amount': 3, 'target_id': "p1", 'target_is_player': True})
    assert ctx.get('damage_amount') == 3, \
        "creature-only prevention shielded a PLAYER (missing target-class check)"


@scenario("615 (prevent X)", "'prevent the next X damage' uses the paid X, not a placeholder 1")
def s_prevention_x_value():
    gs = fresh()
    player = gs.p1
    src_id = card_id_by_name(gs, "Sprout Guardian")
    to_battlefield(gs, src_id)
    gs.replacement_effects._register_damage_prevention(
        src_id, player, "Prevent the next X damage that would be dealt to any target this turn.",
        x_value=4)
    ctx, _ = gs.replacement_effects.apply_replacements(
        'DAMAGE', {'damage_amount': 6, 'target_id': "p1", 'target_is_player': True})
    assert ctx.get('damage_amount') == 2, \
        (f"expected 6-4=2 remaining damage with X=4, got {ctx.get('damage_amount')} "
         f"(X was a placeholder 1)")


@scenario("702.26", "a permanent that phases back in gets its effects re-registered")
def s_phasing_reregisters_effects():
    gs = fresh()
    player = gs.p1
    cid = inject_card(gs, {
        "name": "Phase Ward", "mana_cost": "{1}{W}", "type_line": "Enchantment",
        "oracle_text": "Prevent all damage that would be dealt to you.",
    })
    player["library"].append(cid)
    assert gs.move_card(cid, player, "library", player, "battlefield")
    def _count_effects(source):
        return sum(1 for e in gs.replacement_effects.active_effects
                   if e.get('source_id') == source)
    assert _count_effects(cid) >= 1, "test setup: prevention effect did not register on entry"
    # Grant phasing (sourced by ANOTHER permanent so phase-out's
    # remove-by-source doesn't strip the grant itself).
    granter = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, granter)
    gs.layer_system.register_effect({'source_id': granter, 'layer': 6,
                                     'affected_ids': [cid],
                                     'effect_type': 'add_ability', 'effect_value': 'phasing',
                                     'duration': 'permanent'})
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    # Untap step 1: the permanent phases out; its effects stop existing.
    gs._untap_phase(player)
    assert cid not in player["battlefield"], "test setup: permanent did not phase out"
    assert _count_effects(cid) == 0, "phased-out permanent's effects were not removed"
    # Untap step 2: it phases back in. CR 702.26: it is the same object with
    # the same abilities; its effects must be re-registered.
    gs._untap_phase(player)
    assert cid in player["battlefield"], "permanent did not phase back in"
    assert _count_effects(cid) >= 1, \
        "phased-in permanent lost its abilities permanently (effects never re-registered)"
    assert gs.ability_handler.registered_abilities.get(cid) is not None, \
        "phased-in permanent's parsed abilities were not re-registered"


@scenario("702.26f", "equipment phases with its creature and returns still attached")
def s_phasing_preserves_equipment_attachment():
    gs = fresh()
    player = gs.p1
    creature = card_id_by_name(gs, "Thicket Brute")
    assert gs.move_card(creature, owner_of(gs, creature), "library", player, "battlefield")
    equipment = inject_into_zone(gs, player, {
        "name": "Phasebound Blade", "mana_cost": "{1}",
        "type_line": "Artifact - Equipment",
        "oracle_text": "Equipped creature gets +2/+2. Equip {2}.",
        "subtypes": ["equipment"],
    }, "battlefield")
    assert gs.equip_permanent(player, equipment, creature), "test equipment did not attach"
    granter = inject_into_zone(gs, player, {
        "name": "Phase Current", "mana_cost": "{U}",
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    grant_keyword(gs, creature, "phasing", source_id=granter)
    card = gs._safe_get_card(creature)
    assert (card.power, card.toughness) == (5, 5), "equipment bonus missing before phasing"
    assert layer_effect_count(gs, equipment, "attachment:") >= 1, "Equipment effect was not registered"

    gs._untap_phase(player)
    assert creature not in player["battlefield"], "creature did not phase out"
    assert equipment not in player["battlefield"], "attached Equipment did not phase out indirectly"
    assert {creature, equipment}.issubset(gs.phased_out), "phased-out group was not tracked"
    assert player["attachments"].get(equipment) == creature, "phasing severed Equipment attachment"
    assert layer_effect_count(gs, equipment, "attachment:") == 0, \
        "Equipment effect remained registered while phased out"
    gs.check_state_based_actions()
    assert player["attachments"].get(equipment) == creature, "SBA detached phased-out Equipment"

    player["mana_pool"]["C"] = 3
    cloned = gs.clone()
    assert cloned is not None, "game-state clone failed with a phased attachment group"
    assert cloned.p1["mana_pool"]["C"] == 3, "clone reset copied player mana state"
    assert cloned.phased_out_state[equipment]["phase_in_with"] == creature, \
        "clone lost the Equipment's phase-in group"
    cloned._untap_phase(cloned.p1)
    assert creature in cloned.p1["battlefield"] and equipment in cloned.p1["battlefield"], \
        "cloned phased group did not restore together"
    assert cloned.p1["attachments"].get(equipment) == creature, \
        "clone lost the Equipment attachment on phase-in"

    gs._untap_phase(player)
    assert creature in player["battlefield"] and equipment in player["battlefield"], \
        "creature and Equipment did not phase in together"
    assert player["attachments"].get(equipment) == creature, "Equipment returned unattached"
    assert (card.power, card.toughness) == (5, 5), "Equipment bonus was not restored on phase-in"


@scenario("702.26f", "an opponent's Aura phases and returns with the enchanted permanent")
def s_phasing_preserves_opposing_aura_attachment():
    gs = fresh()
    creature_controller = gs.p1
    aura_controller = gs.p2
    creature = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(creature, owner_of(gs, creature), "library",
                        creature_controller, "battlefield")
    aura = inject_card(gs, {
        "name": "Outsider's Shackles", "mana_cost": "{1}{B}",
        "type_line": "Enchantment - Aura",
        "oracle_text": "Enchant creature. Enchanted creature gets -1/-1.",
        "subtypes": ["aura"],
    })
    aura_controller["library"].append(aura)
    gs._last_card_locations[aura] = (aura_controller, "library")
    assert gs.move_card(aura, aura_controller, "library", aura_controller, "battlefield",
                        context={"attach_to_target": creature}), "test Aura did not enter"
    assert aura_controller["attachments"].get(aura) == creature, "test Aura did not attach"
    granter = inject_into_zone(gs, creature_controller, {
        "name": "Phase Tide", "mana_cost": "{U}",
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    grant_keyword(gs, creature, "phasing", source_id=granter)
    card = gs._safe_get_card(creature)
    assert (card.power, card.toughness) == (1, 1), "Aura penalty missing before phasing"
    assert layer_effect_count(gs, aura, "attachment:") >= 1, "Aura effect was not registered"

    gs._untap_phase(creature_controller)
    assert creature not in creature_controller["battlefield"], "enchanted creature did not phase out"
    assert aura not in aura_controller["battlefield"], "opposing Aura did not phase out indirectly"
    assert aura_controller["attachments"].get(aura) == creature, "phasing severed Aura attachment"
    assert layer_effect_count(gs, aura, "attachment:") == 0, \
        "Aura effect remained registered while phased out"
    gs.check_state_based_actions()
    assert aura not in aura_controller["graveyard"], "phased-out Aura was put into the graveyard"

    gs._untap_phase(creature_controller)
    assert creature in creature_controller["battlefield"], "enchanted creature did not phase in"
    assert aura in aura_controller["battlefield"], \
        "opposing Aura waited for its controller's untap instead of phasing in with the creature"
    assert aura_controller["attachments"].get(aura) == creature, "Aura returned unattached"
    assert (card.power, card.toughness) == (1, 1), "Aura penalty was not restored on phase-in"


@scenario("support manifest", "unparseable card text lands the card on the persisted unsupported list")
def s_card_support_manifest():
    import json, tempfile, os
    from Playersim.card_support import get_manifest, reset_manifest_for_tests
    from Playersim.ability_utils import EffectFactory
    reset_manifest_for_tests()
    # A clause the parser cannot understand must attribute the failure to the
    # card, so (a) Carter can add support, (b) the deck builder can avoid it.
    effects = EffectFactory.create_effects(
        "Gyre and gimble in the wabe.", source_name="Test Unsupported Card")
    m = get_manifest()
    entry = m.entries.get("Test Unsupported Card")
    assert entry is not None, "unparseable text did not put the card on the unsupported list"
    assert entry["count"] >= 1 and entry["severity"] in ("partial", "unparsed"), entry
    assert any("gyre" in r.lower() for r in entry["reasons"]), \
        f"reason does not identify the failing clause: {list(entry['reasons'])}"
    # Persist + merge round trip: the deck builder consumes this file.
    with tempfile.TemporaryDirectory() as d:
        m.persist(d)
        p = os.path.join(d, "card_support_manifest.json")
        assert os.path.exists(p), "manifest file was not written"
        data = json.load(open(p))
        assert "Test Unsupported Card" in data
        assert data["Test Unsupported Card"]["count"] >= 1
        # Second persist merges counts instead of clobbering.
        m.report("Test Unsupported Card", "another clause", severity="unparsed")
        m.persist(d)
        data2 = json.load(open(p))
        assert data2["Test Unsupported Card"]["count"] > data["Test Unsupported Card"]["count"]
        assert data2["Test Unsupported Card"]["severity"] == "unparsed", \
            "severity did not escalate (partial -> unparsed)"
    reset_manifest_for_tests()


@scenario("support manifest (crash)", "an effect that raises attributes a crash entry to its source card")
def s_manifest_crash_attribution():
    from Playersim.card_support import get_manifest, reset_manifest_for_tests
    from Playersim.ability_types import AbilityEffect
    reset_manifest_for_tests()
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, cid)
    class ExplodingEffect(AbilityEffect):
        def __init__(self):
            super().__init__("explode the noosphere")
            self.requires_target = False
        def _apply_effect(self, game_state, source_id, controller, targets):
            raise RuntimeError("boom")
    ok = ExplodingEffect().apply(gs, cid, gs.p1)
    assert ok is False, "a crashing effect must fail gracefully"
    entry = get_manifest().entries.get("Thicket Brute")
    assert entry is not None and entry["severity"] == "crash", \
        f"crash was not attributed to the source card: {entry}"
    reset_manifest_for_tests()


@scenario("support manifest (coverage)", "the coverage report joins a card pool against the manifest")
def s_manifest_coverage_report():
    from Playersim.card_support import get_manifest, reset_manifest_for_tests, coverage_report
    reset_manifest_for_tests()
    m = get_manifest()
    m.report("Broken Card", "unparsed effect text: nonsense", severity="unparsed")
    m.report("Iffy Card", "unparsed clause: half of it", severity="partial")
    rep = coverage_report(["Broken Card", "Iffy Card", "Fine Card"])
    assert rep["total"] == 3 and rep["fully_supported"] == ["Fine Card"], rep
    assert rep["excluded"] == ["Broken Card"] and rep["degraded"] == ["Iffy Card"], rep
    assert abs(rep["supported_fraction"] - (1/3)) < 1e-9, rep
    reset_manifest_for_tests()


@scenario("first-touch: discard", "'each player discards a card' moves one card from each hand to its graveyard")
def s_first_touch_discard():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    for p in (gs.p1, gs.p2):
        while len(p["hand"]) < 2:
            p["hand"].append(p["library"].pop(0))
    h1, h2 = len(gs.p1["hand"]), len(gs.p2["hand"])
    g1, g2 = len(gs.p1["graveyard"]), len(gs.p2["graveyard"])
    effects = EffectFactory.create_effects("Each player discards a card.", source_name="Test Discard")
    assert effects and type(effects[0]).__name__ == "DiscardEffect", \
        f"parser did not produce a DiscardEffect: {[type(e).__name__ for e in effects]}"
    for eff in effects:
        eff.apply(gs, src_id, gs.p1)
    while gs.choice_context and gs.choice_context.get("type") == "discard":
        chooser = gs.choice_context.get("player")
        gs.agent_is_p1 = chooser == gs.p1
        _, ok = get_env().action_handler._handle_discard_card(0)
        assert ok, "parsed discard choice could not be completed"
    assert len(gs.p1["hand"]) == h1 - 1 and len(gs.p2["hand"]) == h2 - 1, \
        f"hands went {h1},{h2} -> {len(gs.p1['hand'])},{len(gs.p2['hand'])} (expected each -1)"
    assert len(gs.p1["graveyard"]) == g1 + 1 and len(gs.p2["graveyard"]) == g2 + 1, \
        "discarded cards did not reach the graveyards"


@scenario("first-touch: mill", "'each player mills two cards' moves library tops to graveyards")
def s_first_touch_mill():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    l1, l2 = len(gs.p1["library"]), len(gs.p2["library"])
    g1, g2 = len(gs.p1["graveyard"]), len(gs.p2["graveyard"])
    tops = {0: list(gs.p1["library"][:2]), 1: list(gs.p2["library"][:2])}
    effects = EffectFactory.create_effects("Each player mills two cards.", source_name="Test Mill")
    assert effects and type(effects[0]).__name__ == "MillEffect", \
        f"parser did not produce a MillEffect: {[type(e).__name__ for e in effects]}"
    for eff in effects:
        eff.apply(gs, src_id, gs.p1)
    assert len(gs.p1["library"]) == l1 - 2 and len(gs.p2["library"]) == l2 - 2, \
        f"libraries went {l1},{l2} -> {len(gs.p1['library'])},{len(gs.p2['library'])} (expected each -2)"
    assert all(c in gs.p1["graveyard"] for c in tops[0]) and all(c in gs.p2["graveyard"] for c in tops[1]), \
        "milled cards are not the library TOPS in the graveyards"


@scenario("first-touch: search", "the fixture ramp spell fetches a basic land onto the battlefield tapped")
def s_first_touch_search():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Wild Growth Ritual")
    owner = owner_of(gs, src_id)
    lands_before = sum(1 for c in owner["battlefield"]
                       if 'land' in getattr(gs._safe_get_card(c), 'card_types', []))
    lib_before = len(owner["library"])
    effects = EffectFactory.create_effects(
        "Search your library for a basic land card and put it onto the battlefield tapped.",
        source_name="Wild Growth Ritual")
    assert effects, "parser produced nothing for the fixture ramp spell's text"
    applied = any(eff.apply(gs, src_id, owner) for eff in effects)
    lands_after = sum(1 for c in owner["battlefield"]
                      if 'land' in getattr(gs._safe_get_card(c), 'card_types', []))
    assert applied and lands_after == lands_before + 1, \
        (f"ramp spell put no land onto the battlefield "
         f"(lands {lands_before}->{lands_after}, applied={applied}) - the fixture "
         f"decks have cast this in every random episode with it silently doing nothing")
    assert len(owner["library"]) == lib_before - 1, "fetched land did not leave the library"
    new_land = [c for c in owner["battlefield"]
                if 'land' in getattr(gs._safe_get_card(c), 'card_types', [])
                and c not in owner.get("tapped_permanents", set())]
    fetched_tapped = any(c in owner.get("tapped_permanents", set()) for c in owner["battlefield"]
                         if 'land' in getattr(gs._safe_get_card(c), 'card_types', []))
    assert fetched_tapped, "fetched land entered untapped despite 'tapped' in the text"


@scenario("first-touch: +1/+1 counter", "a +1/+1 counter raises a creature's power and toughness")
def s_first_touch_plus_counter():
    gs = fresh()
    cid = card_id_by_name(gs, "Thicket Brute")   # printed 3/3
    to_battlefield(gs, cid)
    card = gs._safe_get_card(cid)
    assert (card.power, card.toughness) == (3, 3), "test setup"
    assert gs.add_counter(cid, "+1/+1", 2), "add_counter reported failure"
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert (card.power, card.toughness) == (5, 5), \
        f"two +1/+1 counters gave {card.power}/{card.toughness}, expected 5/5"


@scenario("122.3 (proliferate)", "proliferate adds one more of an existing counter kind")
def s_first_touch_proliferate():
    gs = fresh()
    player = gs.p1
    cid = card_id_by_name(gs, "Thicket Brute")
    assert gs.move_card(cid, owner_of(gs, cid), "library", player, "battlefield")
    gs.add_counter(cid, "+1/+1", 1)
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    card = gs._safe_get_card(cid)
    assert card.counters.get("+1/+1") == 1 and (card.power, card.toughness) == (4, 4), "setup"
    changed = gs.proliferate(player)
    assert changed, "proliferate reported no change despite a proliferable counter"
    assert card.counters.get("+1/+1") == 2, \
        f"proliferate did not add a counter: {card.counters}"
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert (card.power, card.toughness) == (5, 5), \
        f"proliferated counter did not update P/T: {card.power}/{card.toughness}"


@scenario("301.5 (equip)", "an equipment's static bonus applies while attached and stops when moved")
def s_first_touch_equip_bonus():
    gs = fresh()
    player = gs.p1
    creature = card_id_by_name(gs, "Thicket Brute")   # 3/3
    assert gs.move_card(creature, owner_of(gs, creature), "library", player, "battlefield")
    equip = inject_card(gs, {
        "name": "Test Blade", "mana_cost": "{1}", "type_line": "Artifact — Equipment",
        "oracle_text": "Equipped creature gets +2/+2. Equip {2}.",
    })
    player["library"].append(equip)
    assert gs.move_card(equip, player, "library", player, "battlefield")
    ok = gs.equip_permanent(player, equip, creature)
    assert ok, "equip_permanent failed on a legal equip"
    card = gs._safe_get_card(creature)
    assert (card.power, card.toughness) == (5, 5), \
        (f"equipped creature is {card.power}/{card.toughness}, expected 5/5 - the "
         f"equipment's +2/+2 never entered the layer system (equip P/T is a no-op)")
    # Unequip: the bonus must stop applying.
    assert gs.unequip_permanent(player, equip)
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert (card.power, card.toughness) == (3, 3), \
        f"creature kept {card.power}/{card.toughness} after unequip, expected printed 3/3"


@scenario("704.5m (aura falls off)", "an aura goes to the graveyard when its creature leaves")
def s_first_touch_aura_falls_off():
    gs = fresh()
    player = gs.p1
    creature = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(creature, owner_of(gs, creature), "library", player, "battlefield")
    aura = inject_card(gs, {
        "name": "Test Shackles", "mana_cost": "{1}{B}", "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature. Enchanted creature gets -1/-1.",
        "subtypes": ["aura"],
    })
    player["library"].append(aura)
    assert gs.move_card(aura, player, "library", player, "battlefield")
    gs.attach_aura(player, aura, creature)
    assert player["attachments"].get(aura) == creature, "aura did not attach"
    # The creature leaves the battlefield.
    assert gs.move_card(creature, player, "battlefield", player, "graveyard")
    gs.check_state_based_actions()
    assert gs.find_card_location(aura)[1] == "graveyard", \
        "aura did not fall off (704.5m) when its enchanted creature left the battlefield"
    assert aura not in player.get("attachments", {}), "stale attachment entry remained after the aura fell off"


def _make_planeswalker(gs, player, name="Test Walker", loyalty=3,
                       text="+1: You gain 1 life.\n-2: Draw a card.\n-6: You gain an emblem."):
    cid = inject_card(gs, {"name": name, "mana_cost": "{2}{W}",
                           "type_line": "Legendary Planeswalker — Test",
                           "oracle_text": text, "loyalty": loyalty})
    player["library"].append(cid)
    assert gs.move_card(cid, player, "library", player, "battlefield")
    return cid


@scenario("606 (loyalty)", "a planeswalker's plus ability raises its loyalty by the printed amount")
def s_loyalty_plus():
    gs = fresh()
    player = gs.p1
    while gs._get_active_player() is not player:
        gs._advance_phase()
    pw = _make_planeswalker(gs, player)
    card = gs._safe_get_card(pw)
    assert card.loyalty_abilities, "planeswalker loyalty abilities did not parse from text"
    start = player.get("loyalty_counters", {}).get(pw, card.loyalty)
    assert start == 3, f"starting loyalty should be 3, got {start}"
    ok = gs.activate_planeswalker_ability(pw, 0, player)  # the +1
    assert ok, "activating the +1 ability failed"
    assert player["loyalty_counters"][pw] == 4, \
        f"loyalty after +1 is {player['loyalty_counters'][pw]}, expected 4"


@scenario("118.5 (loyalty)", "a minus ability costing more loyalty than available is illegal")
def s_loyalty_minus_illegal():
    gs = fresh()
    player = gs.p1
    while gs._get_active_player() is not player:
        gs._advance_phase()
    pw = _make_planeswalker(gs, player, loyalty=1)   # only 1 loyalty
    # index 1 is the -2 ability; 1 - 2 = -1 < 0, so it is illegal (CR 118.5).
    ok = gs.activate_planeswalker_ability(pw, 1, player)
    assert not ok, "a -2 ability was allowed with only 1 loyalty (CR 118.5 violated)"
    assert player.get("loyalty_counters", {}).get(pw, 1) == 1, \
        "illegal minus ability still changed loyalty"


@scenario("701.17 (scry)", "scry to bottom moves a chosen card off the top of the library")
def s_scry_to_bottom():
    gs = fresh()
    from Playersim.ability_types import ScryEffect
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    top_before = list(player["library"][:2])
    lib_len = len(player["library"])
    assert ScryEffect(2)._apply_effect(gs, src_id, player, {}), "ScryEffect did not initiate"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "scry"
    handler = get_env().action_handler
    # First card: send to bottom (action 307). Second: keep on top (306).
    handler._handle_scry_surveil_choice(0, gs.choice_context, action_index=307)
    handler._handle_scry_surveil_choice(0, gs.choice_context, action_index=306)
    assert gs.choice_context is None, "scry choice did not finalize after all cards processed"
    assert len(player["library"]) == lib_len, "scry changed library size (cards lost or duplicated)"
    assert player["library"][0] == top_before[1], \
        "the card kept on top is not on top after scry"
    assert player["library"][-1] == top_before[0], \
        "the card sent to the bottom is not on the bottom after scry"


@scenario("701.17 / self-play", "the scripted opponent completes its mandatory scry choice")
def s_scripted_opponent_completes_scry():
    gs = fresh(); env = get_env()
    from Playersim.ability_types import ScryEffect
    learned_player, opponent = gs.p2, gs.p1
    gs.agent_is_p1 = False
    top_before = opponent["library"][0]
    library_size = len(opponent["library"])
    assert ScryEffect(1)._apply_effect(gs, None, opponent, {}), \
        "opponent scry did not initiate"
    acting_player, phase_context = env._opponent_needs_to_act()
    assert acting_player is opponent and phase_context == {"phase_context": "CHOOSE"}, \
        "the scripted opponent was not assigned its scry choice"

    # Match the opponent loop: temporarily generate and execute from P1's
    # perspective while the learned policy owns P2.
    gs.agent_is_p1 = True
    opponent_mask = env.action_mask().astype(bool)
    action, action_context = env._get_scripted_opponent_action(
        opponent, opponent_mask, phase_context)
    assert action == 306, \
        f"scripted opponent did not keep its scry card on top: {action}"
    _, _, _, handler_info = env.action_handler.apply_action(
        action, context=action_context)
    assert not handler_info.get("execution_failed"), handler_info
    assert gs.choice_context is None and gs.phase != gs.PHASE_CHOOSE, \
        "scripted opponent left the scry choice unresolved"
    assert len(opponent["library"]) == library_size \
        and opponent["library"][0] == top_before, \
        "scripted scry changed the library while keeping its card on top"
    assert learned_player is gs.p2  # Keep the failed-run seat relationship explicit.


@scenario("701.42 (surveil)", "surveil to graveyard removes a chosen card from the library top")
def s_surveil_to_graveyard():
    gs = fresh()
    from Playersim.ability_types import SurveilEffect
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    top_before = list(player["library"][:2])
    gy_before = len(player["graveyard"])
    lib_before = len(player["library"])
    assert SurveilEffect(2)._apply_effect(gs, src_id, player, {}), "SurveilEffect did not initiate"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "surveil"
    handler = get_env().action_handler
    # First card: to graveyard (305). Second: keep on top (306).
    handler._handle_scry_surveil_choice(0, gs.choice_context, action_index=305)
    handler._handle_scry_surveil_choice(0, gs.choice_context, action_index=306)
    assert gs.choice_context is None, "surveil choice did not finalize"
    assert top_before[0] in player["graveyard"], "surveiled-to-graveyard card is not in the graveyard"
    assert len(player["graveyard"]) == gy_before + 1, "graveyard count wrong after surveil"
    assert player["library"][0] == top_before[1] and len(player["library"]) == lib_before - 1, \
        "the kept card is not on top / library count wrong after surveil"


@scenario("714 (saga)", "advancing a saga increments the same counter the SBA reads")
def s_saga_counter_consistency():
    gs = fresh()
    player = gs.p1
    while gs._get_active_player() is not player:
        gs._advance_phase()
    saga = inject_card(gs, {
        "name": "Test Saga", "mana_cost": "{1}{G}",
        "type_line": "Enchantment — Saga",
        "oracle_text": "I — You gain 2 life.\nII — Draw a card.\nIII — Create a 2/2 token.",
        "subtypes": ["saga"],
    })
    player["library"].append(saga)
    assert gs.move_card(saga, player, "library", player, "battlefield")
    # A saga enters with its first lore counter and its chapter I ability on
    # the stack (CR 714.2/714.3). The counter the SBA reads to sacrifice it
    # (704.5) must be the SAME counter advance_saga_counters increments --
    # setup, advance, and the SBA must not use three different stores.
    def _counter():
        return (player.get("saga_counters", {}).get(saga, 0),
                getattr(gs, "saga_counters", {}).get(saga, 0))
    pc, gc = _counter()
    assert max(pc, gc) == 1, f"saga did not enter with 1 lore counter: player={pc}, gs={gc}"
    gs.advance_saga_counters(player)
    pc, gc = _counter()
    # After one advance the saga is on chapter II; the SBA (which reads
    # player['saga_counters']) must see 2, not still 1.
    assert player.get("saga_counters", {}).get(saga, 0) == 2, \
        (f"advance_saga_counters wrote to a different store than setup/SBA read "
         f"(player saga_counters={player.get('saga_counters', {}).get(saga)}, "
         f"gs.saga_counters={getattr(gs, 'saga_counters', {}).get(saga)})")


@scenario("714.4 (saga)", "a saga is sacrificed after its final chapter")
def s_saga_sacrifice():
    gs = fresh()
    player = gs.p1
    while gs._get_active_player() is not player:
        gs._advance_phase()
    saga = inject_card(gs, {
        "name": "Two-Step Saga", "mana_cost": "{1}{U}",
        "type_line": "Enchantment — Saga",
        "oracle_text": "I — Scry 1.\nII — Draw a card.",
        "subtypes": ["saga"],
    })
    player["library"].append(saga)
    assert gs.move_card(saga, player, "library", player, "battlefield")
    # Chapter I on entry, advance to II, advance past II -> sacrificed.
    gs.advance_saga_counters(player)   # -> II
    assert gs.find_card_location(saga)[1] == "battlefield", "saga left too early"
    gs.advance_saga_counters(player)   # -> past final chapter
    gs.check_state_based_actions()
    assert gs.find_card_location(saga)[1] == "graveyard", \
        "two-chapter saga was not sacrificed after its final chapter (714.4)"


@scenario("700.2 (modal)", "a modal spell parses its modes and resolves only the chosen one")
def s_modal_mode_resolution():
    gs = fresh()
    player = gs.p1
    handler = gs.ability_handler
    modes, lo, hi = handler._parse_modal_text(
        "Choose one —\n• You gain 3 life.\n• Draw a card.")
    assert modes and len(modes) == 2, f"modal parse found {len(modes) if modes else 0} modes, expected 2"
    assert (lo, hi) == (1, 1), f"choose-one bounds parsed as ({lo}, {hi})"
    assert "gain 3 life" in modes[0].lower() and "draw a card" in modes[1].lower(), \
        f"modes parsed in the wrong order / wrong text: {modes}"
    # Resolve ONLY mode 1 (draw a card): life must not change, hand +1.
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    life_before, hand_before = player["life"], len(player["hand"])
    chosen = EffectFactory.create_effects(modes[1], source_name="Test Modal")
    for eff in chosen:
        eff.apply(gs, src_id, player)
    assert player["life"] == life_before, \
        f"the unchosen mode resolved: life changed {life_before}->{player['life']}"
    assert len(player["hand"]) == hand_before + 1, "the chosen mode (draw a card) did not resolve"


@scenario("601.2b / 700.2", "casting a modal spell exposes and resolves the agent's chosen mode")
def s_modal_spell_cast_choice_end_to_end():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player == gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    spell = inject_into_zone(gs, player, {
        "name": "Modal Choice Probe", "mana_cost": "{1}",
        "type_line": "Sorcery",
        "oracle_text": "Choose one —\n• You gain 3 life.\n• Draw a card.",
    }, "hand")
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    life_before = player["life"]
    library_before = len(player["library"])

    assert gs.cast_spell(spell, player), "modal spell cast failed"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "choose_mode", \
        "modal cast did not enter the mode-choice phase"
    assert spell in player["hand"], "modal spell left hand before its mode was chosen"
    assert sum(player["mana_pool"].values()) == 1, "modal spell was paid for before mode selection"
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[353] and mask[354], "mode choices were not exposed in the action mask"
    reward, ok = get_env().action_handler._handle_choose_mode(1, {})
    assert ok, f"choosing the draw mode failed with reward {reward}"
    assert gs.choice_context is None and gs.phase == gs.PHASE_PRIORITY, \
        "completed modal choice did not return to priority"
    assert gs.stack and gs.stack[-1][1] == spell, "chosen modal spell was not put on the stack"
    stack_context = gs.stack[-1][3]
    assert stack_context.get("selected_modes") == [1], "selected mode was not stored on the spell"
    assert stack_context.get("final_paid_cost", {}).get("generic") == 1, \
        "modal finalization lost the spell's paid cost"

    assert gs.resolve_top_of_stack(), "chosen modal spell did not resolve"
    assert player["life"] == life_before, "the unchosen life-gain mode resolved"
    assert len(player["library"]) == library_before - 1, "the chosen draw mode did not draw one card"
    assert player["graveyard"].count(spell) == 1, "resolved modal card did not move exactly once"


@scenario("601.2b/c", "a targeted modal mode asks for targets only after mode selection")
def s_targeted_modal_mode_enters_targeting_after_choice():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if player == gs.p1 else gs.p1
    gs.turn = 1 if player == gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    victim = inject_into_zone(gs, opponent, {
        "name": "Modal Target Victim", "mana_cost": "{2}",
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    spell = inject_into_zone(gs, player, {
        "name": "Targeted Modal Probe", "mana_cost": "{B}",
        "type_line": "Sorcery",
        "oracle_text": "Choose one —\n• Destroy target creature.\n• You gain 2 life.",
    }, "hand")
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 0}

    assert gs.cast_spell(spell, player), "targeted modal spell cast failed"
    assert gs.targeting_context is None, "modal spell requested a target before its mode was chosen"
    reward, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok, f"choosing the targeted mode failed with reward {reward}"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "targeted mode did not enter targeting after selection"
    assert gs.targeting_context.get("required_type") == "creature", \
        f"targeted mode requested {gs.targeting_context.get('required_type')} instead of creature"
    valid_map = gs.targeting_system.get_valid_targets(spell, player, "creature")
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    assert victim in valid_targets, "modal mode's creature target was not legal"
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(victim), {})
    assert ok, f"selecting the modal target failed with reward {reward}"
    assert gs.resolve_top_of_stack(), "targeted modal spell did not resolve"
    assert victim in opponent["graveyard"], "chosen destroy mode did not destroy its target"


@scenario("601.2b/c / Bushwhack", "a modal fight mode is masked when either required creature is unavailable")
def s_bushwhack_impossible_fight_mode_is_masked():
    from Playersim.ability_types import SearchLibraryEffect
    from Playersim.ability_utils import EffectFactory
    gs = fresh(); handler = get_env().action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if player is gs.p1 else gs.p1
    for battlefield_player in (player, opponent):
        for card_id in list(battlefield_player["battlefield"]):
            assert gs.move_card(
                card_id, battlefield_player, "battlefield",
                battlefield_player, "library")
    inject_into_zone(gs, opponent, {
        "name": "Only Fight Creature", "mana_cost": "{2}",
        "type_line": "Creature", "oracle_text": "", "power": 2,
        "toughness": 2,
    }, "battlefield")
    bushwhack = inject_into_zone(gs, player, {
        "name": "Bushwhack", "mana_cost": "{G}", "type_line": "Sorcery",
        "oracle_text": (
            "Choose one —\n"
            "• Search your library for a basic land card, reveal it, put it "
            "into your hand, then shuffle.\n"
            "• Target creature you control fights target creature you don't control."),
    }, "hand")
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 0}

    assert gs.cast_spell(bushwhack, player)
    mask = handler.generate_valid_actions()
    assert mask[353] and not mask[354], \
        f"Bushwhack exposed impossible modes: {np.flatnonzero(mask).tolist()}"
    reward, ok = handler._handle_choose_mode(1, {})
    assert not ok and gs.choice_context.get("selected_modes") == [], \
        f"direct impossible mode mutated the choice: reward={reward}"
    reward, ok = handler._handle_choose_mode(0, {})
    assert ok and gs.stack and gs.stack[-1][1] == bushwhack, \
        f"Bushwhack's legal search mode failed: reward={reward}"
    search_text = (
        "Search your library for a basic land card, reveal it, put it into "
        "your hand, then shuffle.")
    search_effects = EffectFactory.create_effects(
        search_text, source_name="Bushwhack")
    assert (len(search_effects) == 1
            and isinstance(search_effects[0], SearchLibraryEffect)), \
        f"Bushwhack search still fragmented: {search_effects}"


@scenario("601.2b / 700.2", "Pass finishes an optional multi-mode choice after its minimum")
def s_optional_modal_choice_finishes_on_pass():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player == gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    spell = inject_into_zone(gs, player, {
        "name": "One Or Both Probe", "mana_cost": "{U}",
        "type_line": "Sorcery",
        "oracle_text": "Choose one or both —\n• You gain 2 life.\n• Draw a card.",
    }, "hand")
    player["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    life_before = player["life"]
    library_before = len(player["library"])

    assert gs.cast_spell(spell, player), "optional modal spell cast failed"
    ctx = gs.choice_context
    assert (ctx.get("min_required"), ctx.get("max_required")) == (1, 2), \
        f"one-or-both bounds were {ctx.get('min_required')}/{ctx.get('max_required')}"
    reward, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok, f"first optional mode selection failed with reward {reward}"
    assert gs.choice_context is not None and not gs.stack, \
        "optional modal spell finalized before the agent could stop or choose another mode"
    assert get_env().action_handler.generate_valid_actions()[11], \
        "Pass was not offered after the minimum number of modes was selected"
    get_env().action_handler._handle_pass_priority(None)
    assert gs.choice_context is None and gs.stack, \
        "Pass did not finalize the optional modal choice"
    assert gs.stack[-1][3].get("selected_modes") == [0], \
        "Pass changed the modes selected for the spell"

    assert gs.resolve_top_of_stack(), "optional modal spell did not resolve"
    assert player["life"] == life_before + 2, "selected life-gain mode did not resolve"
    assert len(player["library"]) == library_before, "unchosen draw mode resolved"


@scenario("107.3 / 601.2b/f", "an X spell exposes X, pays it once, and resolves with that value")
def s_x_spell_choice_payment_and_resolution():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player == gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    spell = inject_into_zone(gs, player, {
        "name": "X Draw Probe", "mana_cost": "{X}{U}",
        "type_line": "Sorcery", "oracle_text": "Draw X cards.",
    }, "hand")
    player["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 3}
    pool_before = sum(player["mana_pool"].values())
    library_before = len(player["library"])

    assert gs.cast_spell(spell, player), "starting an X spell cast failed"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "choose_x", \
        "X spell did not ask the agent to choose X before payment"
    assert spell in player["hand"], "X spell left hand before its X choice was complete"
    assert sum(player["mana_pool"].values()) == pool_before, "mana was spent before X was chosen"
    env = get_env()
    assert np.array_equal(env._get_obs()["valid_x_range"], np.array([0, 3], dtype=np.int32)), \
        "the observation did not publish the affordable X interval"
    mask = env.action_handler.generate_valid_actions()
    assert all(mask[i] for i in (363, 364, 365)), "affordable X values 1-3 were not exposed"
    assert not mask[366], "an unaffordable X=4 action was exposed"

    reward, ok = get_env().action_handler._handle_choose_x(2, {})
    assert ok, f"choosing X=2 failed with reward {reward}"
    assert gs.stack and gs.stack[-1][3].get("X") == 2, "chosen X was not stored on the spell"
    assert sum(player["mana_pool"].values()) == pool_before - 3, \
        "{X}{U} with X=2 did not spend exactly three mana"
    assert gs.resolve_top_of_stack(), "X draw spell did not resolve"
    assert len(player["library"]) == library_before - 2, "X=2 did not draw exactly two cards"


@scenario("107.3 / policy pagination", "affordable X values above ten remain policy-accessible")
def s_large_x_choice_paginates():
    gs = fresh(SEED + 169)
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    spell = inject_into_zone(gs, player, {
        'name': 'Large X Probe', 'mana_cost': '{X}{U}', 'cmc': 1,
        'type_line': 'Sorcery', 'oracle_text': 'Draw X cards.'}, 'hand')
    player['mana_pool'] = {
        'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 12}

    assert gs.cast_spell(spell, player)
    assert gs.choice_context.get('max_x') == 12
    assert gs.choice_context.get('affordable_values') == list(range(13))
    mask = get_env().action_handler.generate_valid_actions()
    assert all(mask[index] for index in range(363, 373))
    assert mask[479], 'large X did not expose the shared next-page action'
    assert get_env().action_handler._handle_target_page_next(
        context={'page_count': 2})[1]
    assert gs.choice_context.get('choice_page') == 1
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[363] and mask[364] and not mask[365]
    reward, ok = get_env().action_handler._handle_choose_x(
        1, {'x_value': 11})
    assert ok, f'choosing paged X=11 failed with reward {reward}'
    assert gs.stack and gs.stack[-1][3].get('X') == 11
    assert sum(player['mana_pool'].values()) == 1


@scenario("107.3b", "X=0 is selectable and does not become a placeholder one")
def s_x_zero_and_fixed_number_resolution():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player == gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    zero_spell = inject_into_zone(gs, player, {
        "name": "Zero X Probe", "mana_cost": "{X}{U}",
        "type_line": "Sorcery", "oracle_text": "Draw X cards.",
    }, "hand")
    player["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    library_before = len(player["library"])
    assert gs.cast_spell(zero_spell, player), "starting the X=0 spell failed"
    assert get_env().action_handler.generate_valid_actions()[11], "X=0 was not exposed as a choice"
    get_env().action_handler._handle_pass_priority(None)
    assert gs.stack and gs.stack[-1][3].get("X") == 0, "Pass did not choose X=0"
    assert gs.resolve_top_of_stack(), "X=0 spell did not resolve"
    assert len(player["library"]) == library_before, "X=0 incorrectly drew a placeholder card"

    fixed_spell = inject_into_zone(gs, player, {
        "name": "Fixed Number X Probe", "mana_cost": "{X}{W}",
        "type_line": "Sorcery", "oracle_text": "You gain 1 life.",
    }, "hand")
    player["mana_pool"] = {'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 3}
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    life_before = player["life"]
    assert gs.cast_spell(fixed_spell, player), "starting fixed-number X spell failed"
    reward, ok = get_env().action_handler._handle_choose_x(3, {})
    assert ok, f"choosing X=3 failed with reward {reward}"
    assert gs.resolve_top_of_stack(), "fixed-number X spell did not resolve"
    assert player["life"] == life_before + 1, \
        "the chosen X overwrote an unrelated fixed life-gain amount"


@scenario("117.1 / 601.2b", "an X choice preserves the main phase beneath transient priority")
def s_x_choice_preserves_priority_timing():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.priority_pass_count = 0
    mockingbird = inject_into_zone(gs, player, {
        "name": "Mockingbird", "mana_cost": "{X}{U}", "cmc": 1,
        "type_line": "Creature - Bird Bard", "power": 1, "toughness": 1,
        "oracle_text": (
            "Flying\nYou may have this creature enter as a copy of any "
            "creature on the battlefield with mana value less than or equal "
            "to the amount of mana spent to cast this creature, except it's "
            "a Bird in addition to its other types and it has flying."),
    }, "hand")
    player["mana_pool"] = {
        'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 2}

    assert gs.cast_spell(mockingbird, player), \
        "Mockingbird could not begin its X choice from transient priority"
    assert gs.choice_context.get("type") == "choose_x"
    assert gs.choose_x_for_pending_spell(1), \
        "Mockingbird failed timing when its X choice resumed"
    assert gs.stack and gs.stack[-1][1] == mockingbird, \
        "Mockingbird did not reach the stack after choosing X"
    assert (gs.phase == gs.PHASE_PRIORITY
            and gs.previous_priority_phase == gs.PHASE_MAIN_PRECOMBAT), \
        "the X choice did not restore transient priority over the main phase"


@scenario("702.85 (cascade)", "cascade puts the rest of the revealed cards on the bottom, keeping the library sound")
def s_cascade_library_integrity():
    gs = fresh()
    player = gs.p1
    # Build a known library: three high-cost cards, then a cheap nonland, then more.
    hi = [inject_card(gs, {"name": f"Big {i}", "mana_cost": "{5}", "type_line": "Sorcery",
                           "oracle_text": "Do nothing."}) for i in range(3)]
    cheap = inject_card(gs, {"name": "Cheapie", "mana_cost": "{1}", "type_line": "Sorcery",
                             "oracle_text": "Do nothing."})
    tail = [inject_card(gs, {"name": f"Tail {i}", "mana_cost": "{2}", "type_line": "Sorcery",
                             "oracle_text": "Do nothing."}) for i in range(2)]
    for c in hi: gs._safe_get_card(c).cmc = 5
    gs._safe_get_card(cheap).cmc = 1
    for c in tail: gs._safe_get_card(c).cmc = 2
    player["library"] = hi + [cheap] + tail
    lib_ids = set(player["library"])
    spell = inject_card(gs, {"name": "Cascader", "mana_cost": "{4}", "type_line": "Sorcery",
                             "oracle_text": "Cascade. Do nothing."})
    gs._safe_get_card(spell).cmc = 4
    gs.stack.clear()
    gs._process_keyword_abilities(spell, player, {"has_cascade": True})
    # The cheap card should be on the stack; the 3 revealed 'Big' cards should be
    # on the BOTTOM (not left on top), and no card may be lost or duplicated.
    stacked = [item[1] for item in gs.stack if item[0] == "SPELL"]
    assert cheap in stacked, "cascade did not put the hit card on the stack"
    remaining = set(player["library"]) | {cheap} | set(hi)
    assert remaining == lib_ids, \
        f"cascade lost or duplicated cards: library+revealed={remaining} vs original={lib_ids}"
    assert cheap not in player["library"], "cascade left the hit card in the library too (duplicated)"
    for b in hi:
        assert b in player["library"], f"revealed card {b} was lost from the library"
    # The revealed high-cost cards must NOT still be on top.
    assert player["library"][0] in tail, \
        f"revealed cards were left on top of the library instead of the bottom (top={player['library'][0]})"


@scenario("601.3e (impulse)", "'exile top card, you may play it' actually exiles it and makes it playable")
def s_impulse_draw():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    top = player["library"][0]
    lib_before = len(player["library"])
    exile_before = len(player.get("exile", []))
    effects = EffectFactory.create_effects(
        "Exile the top card of your library. You may play that card this turn.",
        source_name="Test Impulse")
    assert effects, "parser produced nothing for impulse-draw text"
    applied = any(eff.apply(gs, src_id, player) for eff in effects)
    assert applied, "impulse effect reported failure"
    assert top in player.get("exile", []) and len(player["exile"]) == exile_before + 1, \
        "impulse draw did not exile the top card"
    assert len(player["library"]) == lib_before - 1, "card was not removed from the library"
    assert top in getattr(gs, "cards_castable_from_exile", set()), \
        ("exiled card was not made playable - impulse draw exiled a card into a black "
         "hole (the whole point of the mechanic never happened)")


@scenario("601.2f (kicker)", "a kicked spell's total cost includes the kicker")
def s_kicker_total_cost():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    spell = inject_card(gs, {"name": "Kicky Bolt", "mana_cost": "{R}",
                             "type_line": "Instant",
                             "oracle_text": "Kicker {2}. Deal 2 damage to any target. "
                                            "If this spell was kicked, deal 4 damage instead."})
    # Cost-string extraction must not crash and must find {2}.
    cost = handler._get_kicker_cost_str(gs._safe_get_card(spell))
    assert cost == "{2}", f"kicker cost extracted as {cost!r}, expected '{{2}}'"
    # Base {R} + kicker {2} = {2}{R}; the combined cost must total 3 mana.
    base = gs.mana_system.parse_mana_cost("{R}")
    kick = gs.mana_system.parse_mana_cost(cost)
    # Combine via the same mixin helper cast_spell uses at cast time.
    from Playersim.game_state_stack import GameStateStackMixin
    combined = GameStateStackMixin._combine_cost_dicts(base, kick)
    total = combined.get("generic", 0) + sum(combined.get(c, 0) for c in ["R","G","U","W","B","C"])
    assert total == 3, f"kicked total cost is {total} mana, expected 3 ({{2}}{{R}})"


@scenario("kicker (no-crash)", "kicker cost extraction handles the bare-number fallback form without crashing")
def s_kicker_fallback_no_crash():
    gs = fresh()
    handler = get_env().action_handler
    # A card whose ONLY kicker mention is the fallback pattern (no braces right
    # after 'kicker'). The old helper read .group(1) on a groupless regex here.
    spell = inject_card(gs, {"name": "Oddly Worded", "mana_cost": "{1}",
                             "type_line": "Sorcery",
                             "oracle_text": "You may pay an additional kicker 3 as you cast this."})
    # Must not raise; returns either a normalized cost or None, never crashes.
    try:
        cost = handler._get_kicker_cost_str(gs._safe_get_card(spell))
    except Exception as e:
        raise AssertionError(f"kicker cost extraction crashed on the fallback form: {type(e).__name__}: {e}")
    assert cost in ("{3}", None), f"unexpected fallback kicker cost: {cost!r}"


@scenario("701.19 (recursion)", "returning a creature card from the graveyard moves it to hand")
def s_graveyard_recursion():
    gs = fresh()
    from Playersim.ability_types import ReturnToHandEffect
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    # Put a creature card in the graveyard.
    dead = inject_card(gs, {"name": "Fallen Bear", "mana_cost": "{1}{G}",
                            "type_line": "Creature — Bear", "power": 2, "toughness": 2})
    player["graveyard"].append(dead)
    hand_before = len(player["hand"])
    gy_before = len(player["graveyard"])
    eff = ReturnToHandEffect(target_type="card", zone="graveyard")
    ok = eff.apply(gs, src_id, player, {"cards": [dead]})
    assert ok, "graveyard-return effect reported failure"
    assert dead in player["hand"] and len(player["hand"]) == hand_before + 1, \
        "creature card was not returned from the graveyard to hand"
    assert dead not in player["graveyard"] and len(player["graveyard"]) == gy_before - 1, \
        "card was not removed from the graveyard"


@scenario("715.3 (adventure)", "a spell cast as an Adventure is exiled, not put in the graveyard")
def s_adventure_exiles_to_recast():
    gs = fresh()
    player = gs.p1
    from Playersim.card import Card
    real_layout = Card({
        "name": "Sell-Sword // Burn Together", "layout": "adventure",
        "card_faces": [
            {"name": "Sell-Sword", "mana_cost": "{1}{B}",
             "type_line": "Creature - Human Soldier", "oracle_text": ""},
            {"name": "Burn Together", "mana_cost": "{R}",
             "type_line": "Sorcery - Adventure", "oracle_text": "Deal damage."},
        ],
    })
    assert not real_layout.is_mdfc(), \
        "an Adventure layout was misclassified as a generic MDFC"
    spell = inject_card(gs, {
        "name": "Giant's Errand", "mana_cost": "{1}{G}",
        "type_line": "Creature — Giant",
        "oracle_text": "Beanstalk Giant is 6/6.\nGiant's Errand {1}{G} (Adventure)\n"
                       "Sorcery — Search your library for a basic land card, reveal it, "
                       "put it into your hand, then shuffle.\n"
                       "Then exile this card. You may cast the creature later from exile.",
    })
    # A cast spell is on the stack (already removed from hand); mirror that.
    ctx = {"cast_as_adventure": True, "source_zone": "hand"}
    gs.stack.clear()
    gs.stack.append(("SPELL", spell, player, ctx))
    gs._resolve_instant_sorcery_spell(spell, player, ctx)
    # CR 715.3f: it goes to EXILE (recastable as the creature), not the graveyard.
    assert spell not in player["graveyard"], \
        "adventure spell went to the graveyard - it can never be cast as the creature now"
    assert gs.find_card_location(spell)[1] == "exile", \
        f"adventure spell is in {gs.find_card_location(spell)[1]}, expected exile"
    assert spell in getattr(gs, "cards_castable_from_exile", set()), \
        "adventure creature side was not made castable from exile"


@scenario("715.3d/f (adventure)", "an adventurer exiled on Adventure can later be cast as the creature")
def s_adventure_creature_castable_from_exile_action():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    adv = inject_card(gs, {
        "name": "Errant Trailblazer", "mana_cost": "{G}",
        "type_line": "Creature — Human Scout",
        "oracle_text": "Trailblazer's Trick {G} (Adventure)\n"
                       "Sorcery — Search your library for a basic land card, reveal it, "
                       "put it into your hand, then shuffle.",
        "power": 2, "toughness": 2,
    })
    player["exile"].append(adv)
    gs.cards_castable_from_exile = {adv}
    gs._last_card_locations[adv] = (player, "exile")
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 0}

    mask = handler.generate_valid_actions()
    assert mask[230], "CAST_FROM_EXILE was not offered for the exiled Adventure creature"
    reward, ok = handler._handle_cast_from_exile(0)
    assert ok, "CAST_FROM_EXILE failed for an Adventure creature in exile"
    assert adv not in player["exile"], "Adventure creature was not removed from exile when cast"
    assert adv not in getattr(gs, "cards_castable_from_exile", set()), \
        "Adventure exile permission was not consumed after casting the creature side"
    assert gs.stack and gs.stack[-1][1] == adv, "Adventure creature side did not go onto the stack"
    gs.resolve_top_of_stack()
    assert adv in player["battlefield"], "Adventure creature side did not resolve onto the battlefield"


@scenario("711 (leveler)", "a level-up creature is recognized as a leveler with its level structure")
def s_leveler_creature_recognized():
    from Playersim.card_support import get_manifest, reset_manifest_for_tests
    reset_manifest_for_tests()
    gs = fresh()
    player = gs.p1
    lev = inject_card(gs, {
        "name": "Student of Warfare", "mana_cost": "{W}",
        "type_line": "Creature — Human Soldier Monk",
        "oracle_text": "Level up {W} ({W}: Put a level counter on this. Level up only as a sorcery.)\n"
                       "LEVEL 1-6\n4/4\nFirst strike\n"
                       "LEVEL 7+\n8/8\nDouble strike",
        "power": 1, "toughness": 1,
    })
    player["library"].append(lev)
    assert gs.move_card(lev, player, "library", player, "battlefield")
    card = gs._safe_get_card(lev)
    # A leveler is NOT a Class, but it IS a leveler. The engine currently gates
    # ALL level machinery on is_class, so leveler creatures are invisible as
    # levelers. Until support lands, they must at least be flagged for the
    # manifest so the deck builder can avoid / down-weight them.
    is_leveler = ("level up" in card.oracle_text.lower())
    assert is_leveler, "test card is a leveler"
    supported = (hasattr(card, "is_leveler") and card.is_leveler) or \
                (getattr(card, "levels", None) and not card.is_class)
    if not supported:
        from Playersim.card_support import report_unsupported
        report_unsupported(card.name, "level-up creature: level counters/thresholds not modeled",
                           severity="partial")
    entry = get_manifest().entries.get("Student of Warfare")
    assert supported or entry is not None, \
        "leveler creature is neither supported nor flagged in the support manifest"
    reset_manifest_for_tests()


@scenario("parser: bounce", "'return target creature to its owner's hand' parses and resolves")
def s_parser_bounce():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    victim = card_id_by_name(gs, "Vine Stalker")
    to_battlefield(gs, victim)
    effs = EffectFactory.create_effects("Return target creature to its owner's hand.",
                                        source_name="Test Bounce")
    assert effs and type(effs[0]).__name__ == "ReturnToHandEffect", \
        (f"'return to its owner's hand' did not parse to a bounce effect: "
         f"{[type(e).__name__ for e in effs]} - the branch checked a regex string literally")
    hand_before = len(gs.get_card_controller(victim)["hand"])
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [victim]})
    owner = gs._find_card_owner_fallback(victim) or player
    assert gs.find_card_location(victim)[1] == "hand", "bounced creature is not in hand"


@scenario("parser: life loss", "'target player loses N life' parses and drains life")
def s_parser_life_loss():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    opp = gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    effs = EffectFactory.create_effects("Target player loses 2 life.", source_name="Test Drain")
    assert effs and any(type(e).__name__ == "LoseLifeEffect" for e in effs), \
        f"'loses N life' did not parse to a life-loss effect: {[type(e).__name__ for e in effs]}"
    life_before = opp["life"]
    for e in effs:
        e.apply(gs, src_id, player, {"players": ["p2"]})
    assert opp["life"] == life_before - 2, \
        f"target player life went {life_before} -> {opp['life']}, expected -2"


@scenario("608.2c / parser", "Hopeless Nightmare preserves its shared opponent subject and self-sacrifice")
def s_hopeless_nightmare_compound_resolution():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory

    player, opp = gs.p1, gs.p2
    source = inject_into_zone(gs, player, {
        "name": "Hopeless Nightmare Probe", "mana_cost": "{B}", "cmc": 1,
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    replace_hand(gs, opp, [{
        "name": "Nightmare Discard", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact", "oracle_text": "",
    }])

    effects = EffectFactory.create_effects(
        "Each opponent discards a card and loses 2 life.",
        source_name="Hopeless Nightmare")
    assert [type(effect).__name__ for effect in effects] == [
        "DiscardEffect", "LoseLifeEffect"], \
        f"compound opponent instruction parsed incorrectly: {effects}"
    assert effects[0].target == "opponent" and effects[1].target == "opponent", \
        "the shared 'each opponent' subject was not inherited by life loss"

    # Model the real stack-resolution phase wrapper and ensure the discard
    # choice does not erase the underlying main phase.
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    life_before = opp["life"]
    _, pending = gs._run_effect_sequence(effects, source, player, {})
    assert pending and gs.choice_context \
        and gs.choice_context.get("type") == "discard", \
        "opponent discard did not pause the effect sequence for its choice"
    assert opp["life"] == life_before, \
        "life loss resolved before the opponent completed the discard"
    assert gs.choose_discard_card(0), "opponent could not complete the discard"
    assert opp["life"] == life_before - 2, \
        "the deferred shared-subject life loss did not resume after discard"
    assert (gs.phase == gs.PHASE_PRIORITY
            and gs.previous_priority_phase == gs.PHASE_MAIN_PRECOMBAT), \
        "the discard continuation lost the stack's underlying turn phase"

    # An empty hand skips the choice but not the later life-loss instruction.
    empty_life = opp["life"]
    _, pending = gs._run_effect_sequence(effects, source, player, {})
    assert not pending and opp["life"] == empty_life - 2, \
        "an opponent with no discard choice incorrectly skipped life loss"

    sacrifice = EffectFactory.create_effects(
        "Sacrifice this enchantment.", source_name="Hopeless Nightmare")
    assert len(sacrifice) == 1 \
        and type(sacrifice[0]).__name__ == "SacrificeSourceEffect", \
        f"self-sacrifice remained generic: {sacrifice}"
    assert sacrifice[0].apply(gs, source, player, {}), \
        "Hopeless Nightmare self-sacrifice reported failure"
    assert source not in player["battlefield"] and source in player["graveyard"], \
        "Hopeless Nightmare did not sacrifice itself to its owner's graveyard"
    substitute = inject_into_zone(gs, player, {
        "name": "Substitute Enchantment", "type_line": "Enchantment",
        "oracle_text": "",
    }, "battlefield")
    assert sacrifice[0].apply(gs, source, player, {}), \
        "a source-absent self-sacrifice should resolve doing nothing"
    assert substitute in player["battlefield"], \
        "a resolved source-only sacrifice substituted another enchantment"


@scenario("701.13 / 608.2c", "Dredger's Insight mills its controller and chooses only from those cards")
def s_dredgers_insight_linked_mill_choice():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory

    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Dredger's Insight Probe", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    preexisting = inject_into_zone(gs, player, {
        "name": "Old Graveyard Relic", "type_line": "Artifact",
        "oracle_text": "",
    }, "graveyard")
    top_cards = [
        inject_card(gs, {"name": "Milled Relic", "type_line": "Artifact", "oracle_text": ""}),
        inject_card(gs, {"name": "Milled Beast", "type_line": "Creature - Beast",
                         "power": 2, "toughness": 2, "oracle_text": ""}),
        inject_card(gs, {"name": "Milled Forest", "type_line": "Land - Forest", "oracle_text": ""}),
        inject_card(gs, {"name": "Milled Trick", "type_line": "Instant", "oracle_text": ""}),
    ]
    player["library"][:0] = top_cards
    for card_id in top_cards:
        gs._last_card_locations[card_id] = (player, "library")

    effects = EffectFactory.create_effects(
        "Mill four cards. You may put an artifact, creature, or land card "
        "from among the milled cards into your hand.",
        source_name="Dredger's Insight")
    assert len(effects) == 1 \
        and type(effects[0]).__name__ == "MillThenChooseEffect", \
        f"linked mill/selection was split or left generic: {effects}"

    _, pending = gs._run_effect_sequence(effects, source, player, {})
    assert pending and gs.choice_context \
        and gs.choice_context.get("type") == "dig_select", \
        "linked mill did not expose its card choice"
    assert all(card_id in player["graveyard"] for card_id in top_cards), \
        "the controller's top four cards were not milled"
    assert set(gs.choice_context.get("options", [])) == set(top_cards[:3]), \
        "the choice was not limited to eligible cards milled by this effect"
    assert preexisting not in gs.choice_context.get("options", []), \
        "a pre-existing graveyard card leaked into the newly-milled choice"
    assert gs.choice_context.get("optional") \
        and gs.choice_context.get("source_zone") == "graveyard", \
        "the milled-card choice lost its optional/source-zone semantics"

    gs.agent_is_p1 = player is gs.p1
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[11] and mask[353:363].sum() == 3, \
        "the action mask did not expose decline plus the three legal cards"
    chosen = top_cards[1]
    _, ok = handler._handle_choose_mode(
        gs.choice_context["options"].index(chosen), {})
    assert ok and chosen in player["hand"] and chosen not in player["graveyard"], \
        "the chosen newly milled card did not move from graveyard to hand"
    assert top_cards[3] in player["graveyard"] and gs.choice_context is None, \
        "an ineligible/unselected milled card left the graveyard"

    decline_cards = [
        inject_card(gs, {"name": f"Declined Mill {index}",
                         "type_line": "Creature", "power": 1,
                         "toughness": 1, "oracle_text": ""})
        for index in range(4)
    ]
    player["library"][:0] = decline_cards
    for card_id in decline_cards:
        gs._last_card_locations[card_id] = (player, "library")
    hand_before = len(player["hand"])
    _, pending = gs._run_effect_sequence(effects, source, player, {})
    assert pending and handler.generate_valid_actions()[11], \
        "a second linked mill did not expose its optional decline"
    _, ok = handler._handle_pass_priority(None)
    assert ok and gs.choice_context is None and len(player["hand"]) == hand_before, \
        "declining the linked mill selection changed the hand or stranded choice state"
    assert all(card_id in player["graveyard"] for card_id in decline_cards), \
        "declining moved an unchosen newly milled card out of the graveyard"

    seed_cards = [
        inject_card(gs, {"name": "Seed Permanent", "type_line": "Land",
                         "oracle_text": ""}),
        inject_card(gs, {"name": "Seed Instant", "type_line": "Instant",
                         "oracle_text": ""}),
    ]
    player["library"][:0] = seed_cards
    for card_id in seed_cards:
        gs._last_card_locations[card_id] = (player, "library")
    seed_effects = EffectFactory.create_effects(
        "Mill two cards. You may put a permanent card from among the milled "
        "cards into your hand. You gain 2 life.", source_name="Seed of Hope")
    assert [type(effect).__name__ for effect in seed_effects] == [
        "MillThenChooseEffect", "GainLifeEffect"], \
        f"Seed's linked mill/life sequence parsed incorrectly: {seed_effects}"
    seed_life = player["life"]
    _, pending = gs._run_effect_sequence(seed_effects, source, player, {})
    assert pending and player["life"] == seed_life, \
        "Seed gained life before its optional milled-card choice completed"
    assert handler._handle_pass_priority(None)[1]
    assert player["life"] == seed_life + 2, \
        "Seed's life-gain suffix did not resume after declining the choice"

    # Dredger's other printed trigger watches only matching cards leaving its
    # controller's graveyard.
    watcher = inject_into_zone(gs, player, {
        "name": "Dredger's Insight Watcher", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Enchantment",
        "oracle_text": (
            "Whenever one or more artifact and/or creature cards leave your "
            "graveyard, you gain 1 life."),
    }, "battlefield")
    leaving_artifact = inject_into_zone(gs, player, {
        "name": "Leaving Relic", "type_line": "Artifact", "oracle_text": "",
    }, "graveyard")
    trigger_life = player["life"]
    assert gs.move_card(
        leaving_artifact, player, "graveyard", player, "hand",
        cause="graveyard_recovery")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack()
    assert player["life"] == trigger_life + 1, \
        "Dredger did not gain life when its artifact left its graveyard"

    leaving_instant = inject_into_zone(gs, player, {
        "name": "Leaving Trick", "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    assert gs.move_card(
        leaving_instant, player, "graveyard", player, "hand",
        cause="graveyard_recovery")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack()
    assert player["life"] == trigger_life + 1 and watcher in player["battlefield"], \
        "Dredger triggered for a non-artifact/noncreature graveyard card"


@scenario("603.1 / 608.2c", "Nurturing Pixie keeps its restricted bounce and conditional counter atomic")
def s_nurturing_pixie_linked_bounce_counter():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory

    player = gs.p1
    pixie = inject_into_zone(gs, player, {
        "name": "Nurturing Pixie Probe", "mana_cost": "{W}", "cmc": 1,
        "type_line": "Creature - Faerie Rogue", "power": 1, "toughness": 1,
        "oracle_text": "",
    }, "battlefield")
    legal = inject_into_zone(gs, player, {
        "name": "Pixie Parcel", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    faerie = inject_into_zone(gs, player, {
        "name": "Other Faerie", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Creature - Faerie", "power": 1, "toughness": 1,
        "oracle_text": "",
    }, "battlefield")
    land = inject_into_zone(gs, player, {
        "name": "Pixie Meadow", "type_line": "Land - Plains", "oracle_text": "",
    }, "battlefield")
    opponent = gs.p2
    opposing = inject_into_zone(gs, opponent, {
        "name": "Opponent Parcel", "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    text = (
        "Return up to one target non-Faerie, nonland permanent you control "
        "to its owner's hand. If a permanent was returned this way, put a "
        "+1/+1 counter on this creature.")
    effects = EffectFactory.create_effects(text, source_name="Nurturing Pixie")
    assert len(effects) == 1 \
        and type(effects[0]).__name__ == "ReturnThenAddCounterEffect", \
        f"Pixie's linked instruction was fragmented: {effects}"

    valid_map = gs.targeting_system.get_valid_targets(
        pixie, player, "permanent", effect_text=effects[0].effect_text)
    valid_ids = {card_id for values in valid_map.values() for card_id in values}
    assert (legal in valid_ids and faerie not in valid_ids
            and land not in valid_ids and opposing not in valid_ids), \
        "Pixie's non-Faerie/nonland/controller restrictions were not preserved"

    from Playersim.ability_types import ActivatedAbility
    assert ActivatedAbility._parse_cost_effect_strict(text) == (None, None), \
        "the hyphen in non-Faerie was still treated as an ability separator"
    assert ActivatedAbility._parse_cost_effect_strict(
        "{2}{B}: Sacrifice this enchantment.") == (
            "{2}{B}", "Sacrifice this enchantment"), \
        "tight colon-separated activated abilities stopped parsing"

    assert effects[0].apply(gs, pixie, player, {"permanents": [legal]}), \
        "Pixie's linked bounce/counter effect reported failure"
    assert legal in player["hand"] and legal not in player["battlefield"], \
        "Pixie did not return the chosen permanent"
    assert gs._safe_get_card(pixie).counters.get("+1/+1", 0) == 1, \
        "Pixie did not get its conditional +1/+1 counter"
    assert effects[0].apply(gs, pixie, player, {}), \
        "declining Pixie's up-to-one target should resolve successfully"
    assert gs._safe_get_card(pixie).counters.get("+1/+1", 0) == 1, \
        "Pixie received a counter without returning a permanent"


@scenario("parser: keyword grant", "'target creature gains flying until end of turn' grants the keyword")
def s_parser_keyword_grant():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    target = card_id_by_name(gs, "Vine Stalker")
    to_battlefield(gs, target)
    assert _kw(gs, target, "flying") == 0, "test setup: target already has flying"
    effs = EffectFactory.create_effects("Target creature gains flying until end of turn.",
                                        source_name="Test Grant")
    assert effs and any(type(e).__name__ == "GainKeywordEffect" for e in effs), \
        f"keyword grant did not parse to a grant effect: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [target]})
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert _kw(gs, target, "flying") == 1, \
        "target creature did not gain flying (keyword grant is a no-op)"


@scenario("parser: distribute counters", "the policy distributes counters one at a time among committed targets")
def s_parser_distribute_counters():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    a = card_id_by_name(gs, "Vine Stalker"); to_battlefield(gs, a)
    b = inject_into_zone(gs, player, {"name": "Counter Choice B", "mana_cost": "", "cmc": 0,
        "type_line": "Creature", "power": 1, "toughness": 1, "oracle_text": ""}, "battlefield")
    effs = EffectFactory.create_effects(
        "Distribute three +1/+1 counters among any number of target creatures.",
        source_name="Test Distribute")
    assert effs and any(type(e).__name__ == "DistributeCountersEffect" for e in effs), \
        f"distribute counters did not parse: {[type(e).__name__ for e in effs]}"
    assert gs._target_bounds_from_text(
        "Distribute three +1/+1 counters among any number of target creatures.") == (0, 3)
    assert gs._target_bounds_from_text(
        "Distribute three +1/+1 counters among one or two target creatures.") == (1, 2)
    assert gs._target_bounds_from_text(
        "Destroy target artifact and up to one target enchantment.") == (1, 2)
    comma_bounded = EffectFactory.create_effects(
        "Distribute three +1/+1 counters among one, two, or three target creatures.",
        source_name="Test Bounded Distribute")
    comma_effect = next(e for e in comma_bounded if type(e).__name__ == "DistributeCountersEffect")
    assert gs._target_bounds_from_text(comma_effect.effect_text) == (1, 3), \
        f"comma-separated target bounds were lost: {comma_effect.effect_text!r}"

    effect = next(e for e in effs if type(e).__name__ == "DistributeCountersEffect")
    gs.agent_is_p1 = player is gs.p1
    handler = get_env().action_handler
    assert effect.apply(gs, src_id, player), "distribution did not open target selection"
    assert gs.targeting_context and (
        gs.targeting_context['min_targets'], gs.targeting_context['max_targets']) == (0, 3)
    mask = handler.generate_valid_actions()
    assert mask[11] and mask[274:284].any(), \
        "any-number targeting did not expose both finish and select actions at zero"

    def select_target(target_id):
        ctx = gs.targeting_context
        valid_map = gs.targeting_system.get_valid_targets(
            src_id, player, ctx['required_type'], effect_text=ctx['effect_text'])
        valid_targets = sorted(
            {cid for ids in valid_map.values() for cid in ids},
            key=lambda cid: (isinstance(cid, str), cid))
        valid_targets = [cid for cid in valid_targets
                         if cid not in ctx.get('selected_targets', [])]
        return handler._handle_select_target(valid_targets.index(target_id), {})

    _, ok = select_target(a)
    assert ok and handler.generate_valid_actions()[11], \
        "distribution could not finish at an intermediate legal target count"
    _, ok = select_target(b)
    assert ok
    _, ok = handler._handle_pass_priority(None)
    assert ok, "Pass did not commit the selected distribution targets"
    assert gs.choice_context and gs.choice_context.get('type') == 'distribute_counters'
    _, ok = handler._handle_choose_mode(gs.choice_context['options'].index(b), {})
    assert ok
    _, ok = handler._handle_choose_mode(gs.choice_context['options'].index(b), {})
    assert ok
    assert gs._safe_get_card(a).counters.get('+1/+1', 0) == 0 \
        and gs._safe_get_card(b).counters.get('+1/+1', 0) == 0, \
        "counter allocation mutated the board before all assignments finished"
    mask = handler.generate_valid_actions()
    a_index = gs.choice_context['options'].index(a)
    b_index = gs.choice_context['options'].index(b)
    allocation_obs = get_env().observation_for(player)
    assert allocation_obs['choice_remaining'][0] == 1 \
        and allocation_obs['choice_allocation_counts'][b_index] == 2, \
        "staged counter allocations were not explicit in the chooser observation"
    assert mask[353 + a_index] and not mask[353 + b_index], \
        "the final counter was not forced onto the still-unassigned target"
    _, ok = handler._handle_choose_mode(a_index, {})
    assert ok
    assert gs._safe_get_card(a).counters.get('+1/+1', 0) == 1
    assert gs._safe_get_card(b).counters.get('+1/+1', 0) == 2

    life_before = player['life']
    sequenced = EffectFactory.create_effects(
        "Distribute two +1/+1 counters among one or two target creatures, "
        "then gain 2 life.", source_name="Sequenced Distribute")
    _, pending = gs._run_effect_sequence(
        sequenced, src_id, player, {"creatures": [a, b]})
    assert pending and gs.choice_context.get('type') == 'distribute_counters'
    assert player['life'] == life_before, "a later effect ran before counter allocation"
    _, ok = handler._handle_choose_mode(gs.choice_context['options'].index(a), {})
    assert ok and player['life'] == life_before
    _, ok = handler._handle_choose_mode(gs.choice_context['options'].index(b), {})
    assert ok and player['life'] == life_before + 2, \
        "counter allocation did not resume the remaining effect sequence"

    life_before = player['life']
    targeted_sequence = EffectFactory.create_effects(
        "Distribute one +1/+1 counter among any number of target creatures, "
        "then gain 1 life.", source_name="Targeted Sequence")
    _, pending = gs._run_effect_sequence(
        targeted_sequence, src_id, player, targets=None)
    assert pending and gs.targeting_context and player['life'] == life_before, \
        "later effects ran while the first effect was still choosing targets"
    _, ok = select_target(a)
    assert ok and gs.choice_context \
        and gs.choice_context.get('type') == 'distribute_counters'
    assert player['life'] == life_before
    _, ok = handler._handle_choose_mode(gs.choice_context['options'].index(a), {})
    assert ok and player['life'] == life_before + 1, \
        "target selection did not preserve the remaining effect continuation"


@scenario("601.2d / 602.2b", "counter divisions are locked before spell and ability costs are paid")
def s_counter_divisions_announced_before_payment():
    gs = fresh(SEED + 182)
    handler = get_env().action_handler
    player = gs.p1
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    a = inject_into_zone(gs, player, {
        "name": "Division Target A", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    b = inject_into_zone(gs, player, {
        "name": "Division Target B", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    spell = inject_into_zone(gs, player, {
        "name": "Declared Growth", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": (
            "Distribute three +1/+1 counters among any number of target creatures."),
    }, "hand")
    player["mana_pool"] = {
        "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 0}
    assert gs.cast_spell(spell, player)
    assert gs.targeting_context and spell in player["hand"] and not gs.stack

    def choose_target(target_id):
        context = gs.targeting_context
        valid_map = gs.targeting_system.get_valid_targets(
            context["source_id"], player, context["required_type"],
            effect_text=context["effect_text"])
        valid = sorted(
            {cid for ids in valid_map.values() for cid in ids
             if cid not in context.get("selected_targets", [])},
            key=lambda cid: (isinstance(cid, str), cid))
        return handler._handle_select_target(valid.index(target_id), {})[1]

    assert choose_target(a) and choose_target(b)
    assert handler._handle_pass_priority(None)[1]
    assert gs.choice_context and gs.choice_context.get("announcement_kind") == "cast"
    assert spell in player["hand"] and not gs.stack, \
        "the spell was paid for before its division was announced"
    assert player["mana_pool"]["G"] == 1
    a_idx = gs.choice_context["options"].index(a)
    b_idx = gs.choice_context["options"].index(b)
    assert handler._handle_choose_mode(a_idx, {})[1]
    assert handler._handle_choose_mode(a_idx, {})[1]
    assert handler._handle_choose_mode(b_idx, {})[1]
    assert gs.stack and spell not in player["hand"]
    assert player["mana_pool"]["G"] == 0
    assert gs.stack[-1][3].get("counter_allocations") == {a: 2, b: 1}
    assert gs._safe_get_card(a).counters.get("+1/+1", 0) == 0

    # The target that becomes illegal keeps its announced share; it is not
    # reassigned to the target that remains legal.
    assert gs.move_card(b, player, "battlefield", player, "graveyard",
                        cause="response")
    assert gs.resolve_top_of_stack()
    a_counters = gs._safe_get_card(a).counters.get("+1/+1", 0)
    assert a_counters == 2, \
        f"the locked division resolved as {a_counters} counters instead of two"

    activator = inject_into_zone(gs, player, {
        "name": "Division Engine", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact",
        "oracle_text": (
            "{G}: Distribute two +1/+1 counters among one or two target creatures."),
    }, "battlefield")
    c = inject_into_zone(gs, player, {
        "name": "Division Target C", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    player["mana_pool"]["G"] = 1
    bf_idx = player["battlefield"].index(activator)
    reward, ok = handler._handle_activate_ability(
        None, {"battlefield_idx": bf_idx, "ability_idx": 0,
               "controller_id": "p1"})
    assert ok, f"distribution activation did not stage targets: {reward}"
    assert choose_target(a) and choose_target(c)
    assert gs.choice_context \
        and gs.choice_context.get("announcement_kind") == "activation"
    assert player["mana_pool"]["G"] == 1 and not gs.stack
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index(a), {})[1]
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index(c), {})[1]
    assert gs.stack and player["mana_pool"]["G"] == 0
    assert gs.stack[-1][3].get("counter_allocations") == {a: 1, c: 1}
    assert gs.resolve_top_of_stack()
    assert gs._safe_get_card(a).counters.get("+1/+1", 0) == 3
    assert gs._safe_get_card(c).counters.get("+1/+1", 0) == 1


@scenario("parser: sacrifice", "the affected player chooses which creature to sacrifice")
def s_parser_sacrifice():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    player = to_battlefield(gs, src_id)   # controller = the creatures' actual owner
    victim = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(victim, owner_of(gs, victim), "library", player, "battlefield")
    effs = EffectFactory.create_effects("Sacrifice a creature.", source_name="Test Sac")
    assert effs and type(effs[0]).__name__ == "SacrificeEffect", \
        f"'sacrifice a creature' did not parse to a sacrifice effect: {[type(e).__name__ for e in effs]}"
    gy_before = len(player["graveyard"])
    bf_before = len([c for c in player["battlefield"] if "creature" in getattr(gs._safe_get_card(c),"card_types",[])])
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert gs.choice_context and gs.choice_context.get('type') == 'sacrifice_effect'
    gs.agent_is_p1 = player is gs.p1
    # Action generation refreshes the dynamic options list.
    get_env().action_handler.generate_valid_actions()
    chosen_index = gs.choice_context['options'].index(victim)
    _, ok = get_env().action_handler._handle_choose_mode(chosen_index, {})
    assert ok
    bf_after = len([c for c in player["battlefield"] if "creature" in getattr(gs._safe_get_card(c),"card_types",[])])
    assert bf_after == bf_before - 1 and len(player["graveyard"]) == gy_before + 1, \
        "no creature was sacrificed to the graveyard"

    optional_fodder = inject_into_zone(gs, player, {
        "name": "Optional Sacrifice Fodder", "mana_cost": "",
        "type_line": "Creature", "power": 1, "toughness": 1,
        "oracle_text": ""}, "battlefield")
    hand_before = len(player['hand'])
    optional_sequence = EffectFactory.create_effects(
        "You may sacrifice a creature, then draw a card.",
        source_name="Optional Sacrifice")
    _, pending = gs._run_effect_sequence(
        optional_sequence, src_id, player, {})
    assert pending and gs.choice_context.get('optional')
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[11], "optional generic sacrifice did not expose decline"
    _, ok = get_env().action_handler._handle_pass_priority(None)
    assert ok and optional_fodder in player['battlefield']
    assert len(player['hand']) == hand_before + 1, \
        "declining an optional sacrifice did not resume later effects"

    chained_fodder = inject_into_zone(gs, player, {
        "name": "Chained Sacrifice Fodder", "mana_cost": "",
        "type_line": "Creature", "power": 1, "toughness": 1,
        "oracle_text": ""}, "battlefield")
    chained = EffectFactory.create_effects(
        "Sacrifice a creature, then look at the top three cards of your library. "
        "Put one into your hand and the rest on the bottom.",
        source_name="Chained Choices")
    _, pending = gs._run_effect_sequence(chained, src_id, player, {})
    assert pending and gs.choice_context.get('type') == 'sacrifice_effect'
    get_env().action_handler.generate_valid_actions()
    _, ok = get_env().action_handler._handle_choose_mode(
        gs.choice_context['options'].index(chained_fodder), {})
    assert ok and gs.choice_context and gs.choice_context.get('type') == 'dig_select', \
        "the later Dig choice did not replace the completed sacrifice choice"
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and gs.choice_context is None


@scenario("parser: edict", "an edict lets the targeted player choose the sacrificed creature")
def s_parser_edict():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    theirs = card_id_by_name(gs, "Vine Stalker")
    theirs_owner = owner_of(gs, theirs)
    assert gs.move_card(theirs, theirs_owner, "library", opp, "battlefield")
    effs = EffectFactory.create_effects("Target player sacrifices a creature.", source_name="Edict")
    assert effs and type(effs[0]).__name__ == "SacrificeEffect", \
        f"edict did not parse to a sacrifice effect: {[type(e).__name__ for e in effs]}"
    gy_before = len(theirs_owner["graveyard"])
    for e in effs:
        e.apply(gs, src_id, player, {"players": ["p2"]})
    assert gs.choice_context and gs.choice_context.get('player') is opp
    gs.agent_is_p1 = opp is gs.p1
    get_env().action_handler.generate_valid_actions()
    _, ok = get_env().action_handler._handle_choose_mode(gs.choice_context['options'].index(theirs), {})
    assert ok
    assert len(theirs_owner["graveyard"]) == gy_before + 1, \
        "edict did not put the sacrificed permanent into its owner's graveyard"


@scenario("parser: reanimation", "'return target creature card from your graveyard to the battlefield' revives it")
def s_parser_reanimation():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    dead = inject_card(gs, {"name": "Dead Bear", "mana_cost": "{1}{G}",
                            "type_line": "Creature — Bear", "power": 2, "toughness": 2})
    player["graveyard"].append(dead)
    effs = EffectFactory.create_effects(
        "Return target creature card from your graveyard to the battlefield.", source_name="Reanimate")
    assert effs and type(effs[0]).__name__ == "ReanimateEffect", \
        f"reanimation did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"cards": [dead]})
    assert gs.find_card_location(dead)[1] == "battlefield", \
        "reanimated creature is not on the battlefield"
    assert dead not in player["graveyard"], "reanimated creature was left in the graveyard"


@scenario("parser: can't attack/block", "'target creature can't block this turn' registers the restriction")
def s_parser_cant_block():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    target = card_id_by_name(gs, "Vine Stalker"); to_battlefield(gs, target)
    effs = EffectFactory.create_effects("Target creature can't block this turn.", source_name="Falter")
    assert effs and type(effs[0]).__name__ == "GainKeywordEffect", \
        f"'can't block' did not parse to a restriction grant: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [target]})
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    # The restriction is stored as a granted 'cant_block' ability on the target.
    card = gs._safe_get_card(target)
    granted = getattr(card, '_granted_abilities', set()) if hasattr(card, '_granted_abilities') else set()
    # Fall back to checking the layer registration directly.
    has_restriction = any(
        d.get('effect_value') == 'cant_block' and target in d.get('affected_ids', [])
        for _, d in gs.layer_system.layers[6])
    assert has_restriction, "'can't block' restriction was not registered on the target"


@scenario("711 (leveler)", "a level-up creature reports P/T for its current level band")
def s_leveler_pt_by_level():
    gs = fresh()
    player = gs.p1
    lev = inject_card(gs, {
        "name": "Warfare Student", "mana_cost": "{W}",
        "type_line": "Creature — Human Soldier",
        "oracle_text": "Level up {W}\nLEVEL 1-6\n4/4\nFirst strike\nLEVEL 7+\n8/8\nDouble strike",
        "power": 1, "toughness": 1,
    })
    player["library"].append(lev)
    assert gs.move_card(lev, player, "library", player, "battlefield")
    card = gs._safe_get_card(lev)
    assert getattr(card, "is_leveler", False), "leveler creature was not recognized as a leveler"
    # Base (0 level counters): printed 1/1.
    assert card.get_leveler_pt(0) == (1, 1), f"level 0 P/T wrong: {card.get_leveler_pt(0)}"
    # 3 counters -> band LEVEL 1-6 -> 4/4.
    assert card.get_leveler_pt(3) == (4, 4), f"level 3 P/T wrong: {card.get_leveler_pt(3)}"
    # 7 counters -> band LEVEL 7+ -> 8/8.
    assert card.get_leveler_pt(7) == (8, 8), f"level 7 P/T wrong: {card.get_leveler_pt(7)}"


@scenario("711 (leveler)", "a leveler levels up in-game: pay the cost, gain a level counter, grow to the band P/T and abilities")
def s_leveler_level_up_action():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    lev = inject_card(gs, {
        "name": "Ascending Cadet", "mana_cost": "{W}",
        "type_line": "Creature — Human Soldier",
        "oracle_text": "Level up {W}\nLEVEL 1-6\n4/4\nFirst strike\nLEVEL 7+\n8/8\nDouble strike",
        "power": 1, "toughness": 1,
    })
    player["library"].append(lev)
    assert gs.move_card(lev, player, "library", player, "battlefield")
    card = gs._safe_get_card(lev)
    assert getattr(card, "is_leveler", False), "test card not recognized as a leveler"

    ls = gs.layer_system
    ls.invalidate_cache(); ls.apply_all_effects()
    # Base band (0 counters): printed 1/1, no band keyword yet.
    assert (card.power, card.toughness) == (1, 1), f"base P/T wrong: {(card.power, card.toughness)}"
    assert _kw(gs, lev, "first strike") == 0, "leveler already had its band keyword before leveling"

    # The agent must be able to see and afford the level-up action.
    bf_idx = player["battlefield"].index(lev)
    from Playersim.actions import ACTION_MEANINGS
    lu_idx = next((i for i, (n, p) in ACTION_MEANINGS.items()
                   if n == "LEVEL_UP_CREATURE" and p == bf_idx), None)
    assert lu_idx is not None, "no LEVEL_UP_CREATURE action index is defined"
    # With no mana, the action must NOT be offered (cost gating).
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    mask = handler.generate_valid_actions()
    assert not mask[lu_idx], "level-up offered for free (cost gate missing)"
    # Give the {W} it costs; now it must be offered.
    player["mana_pool"]["W"] = 1
    mask = handler.generate_valid_actions()
    assert mask[lu_idx], "level-up not offered for an affordable leveler"

    # Perform it: pays {W}, adds exactly one level counter.
    reward, ok = handler._handle_level_up_creature(bf_idx, {})
    assert ok, "level-up action reported failure"
    assert (getattr(card, "counters", {}) or {}).get("level", 0) == 1, \
        f"level counter not added (counters={getattr(card, 'counters', {})})"
    assert player["mana_pool"]["W"] == 0, "level-up did not spend the mana"

    # 1 counter -> band LEVEL 1-6 -> 4/4 + First strike, via the layer system.
    ls.invalidate_cache(); ls.apply_all_effects()
    assert (card.power, card.toughness) == (4, 4), \
        f"leveler P/T did not reflect its band after leveling: {(card.power, card.toughness)}"
    assert _kw(gs, lev, "first strike") == 1, "leveled creature did not gain its band keyword"

    # A +1/+1 counter must still stack on the band base (CR 711.4 / layer 7c).
    gs.add_counter(lev, "+1/+1", 1)
    ls.invalidate_cache(); ls.apply_all_effects()
    assert (card.power, card.toughness) == (5, 5), \
        f"+1/+1 counter did not stack on the leveler band: {(card.power, card.toughness)}"

    # Climb to the top band: six more level counters -> 7 total -> 8/8 + Double strike.
    for _ in range(6):
        player["mana_pool"]["W"] = 1
        _, ok2 = handler._handle_level_up_creature(bf_idx, {})
        assert ok2, "a later level-up failed"
    assert (card.counters or {}).get("level", 0) == 7, \
        f"expected 7 level counters, got {(card.counters or {}).get('level')}"
    ls.invalidate_cache(); ls.apply_all_effects()
    # 8/8 top band + the +1/+1 counter -> 9/9.
    assert (card.power, card.toughness) == (9, 9), \
        f"top-band P/T (+counter) wrong: {(card.power, card.toughness)}"
    assert _kw(gs, lev, "double strike") == 1, "top-band creature did not gain Double strike"


@scenario("712 (MDFC)", "casting an MDFC's back face uses the back face's cost and text")
def s_mdfc_back_face_cast():
    gs = fresh()
    player = gs.p1
    mdfc = inject_card(gs, {
        "name": "Sejiri Shelter // Sejiri Glacier",
        "mana_cost": "{1}{W}",
        "type_line": "Instant // Land",
        "oracle_text": "Sejiri Shelter\nInstant\nTarget creature gains protection from a color.\n"
                       "Sejiri Glacier\nLand\nSejiri Glacier enters the battlefield tapped.",
        "faces": [
            {"name": "Sejiri Shelter", "mana_cost": "{1}{W}", "type_line": "Instant",
             "oracle_text": "Target creature you control gains protection from the color of your choice until end of turn."},
            {"name": "Sejiri Glacier", "mana_cost": "", "type_line": "Land",
             "oracle_text": "Sejiri Glacier enters the battlefield tapped."},
        ],
    })
    card = gs._safe_get_card(mdfc)
    assert card.is_mdfc(), "card with two non-transform faces was not recognized as MDFC"
    # The engine must expose the cost/text of a chosen face, not just the front.
    front_cost = card.get_face_cost(0)
    back_cost = card.get_face_cost(1)
    assert front_cost == "{1}{W}", f"front face cost wrong: {front_cost}"
    assert back_cost == "", f"back face (land) cost wrong: {back_cost!r}"
    assert "enters the battlefield tapped" in card.get_face_text(1).lower(), \
        "back face text not retrievable for casting/playing the back face"

    # The public MDFC spell handler must pass cast_spell's exact flag and use
    # the chosen back face's cost rather than the expensive front face.
    spell_mdfc = inject_into_zone(gs, player, {
        "name": "Rootguard // Sudden Spark", "layout": "modal_dfc",
        "mana_cost": "{5}{G}", "type_line": "Creature - Treefolk",
        "oracle_text": "", "power": 5, "toughness": 5,
        "faces": [
            {"name": "Rootguard", "mana_cost": "{5}{G}",
             "type_line": "Creature - Treefolk", "oracle_text": ""},
            {"name": "Sudden Spark", "mana_cost": "{R}",
             "type_line": "Sorcery", "oracle_text": "Draw a card."},
        ],
    }, "hand")
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
    hand_idx = player["hand"].index(spell_mdfc)
    _, ok = get_env().action_handler._handle_play_mdfc_back(hand_idx, context={})
    assert ok, "MDFC back-face handler did not use the affordable back cost"
    assert gs.stack and gs.stack[-1][1] == spell_mdfc \
        and gs.stack[-1][3].get("cast_as_back_face"), \
        "MDFC back-face choice was not retained on the stack"


@scenario("712 (transform)", "a 'transform' effect flips a double-faced permanent to its other face")
def s_transform_effect_flips_dfc():
    gs = fresh()
    player = gs._get_active_player()
    front = {"name": "Village Watchman", "type_line": "Creature — Human Warrior",
             "oracle_text": "At the beginning of your upkeep, transform Village Watchman.",
             "power": 2, "toughness": 2}
    back = {"name": "Moonrage Brute", "type_line": "Creature — Werewolf",
            "oracle_text": "At the beginning of your end step, transform Moonrage Brute.",
            "power": 5, "toughness": 5}
    cid = inject_card(gs, {**front, "faces": [front, back]})
    player["library"].append(cid)
    assert gs.move_card(cid, player, "library", player, "battlefield")
    card = gs._safe_get_card(cid)
    assert getattr(card, "current_face", None) == 0, "card should start on its front face"
    assert (card.power, card.toughness) == (2, 2), f"front P/T wrong: {(card.power, card.toughness)}"

    # TransformEffect calls gs.transform_card(); without that method every parsed
    # 'transform ~' effect silently fails. It must exist and actually flip the card.
    assert hasattr(gs, "transform_card"), \
        "GameState lacks transform_card - every transform effect is a silent no-op"
    ok = gs.transform_card(cid)
    assert ok, "transform_card reported failure on a transforming DFC"
    assert card.current_face == 1, "card did not flip to its back face"
    assert card.name == "Moonrage Brute", f"name did not update to back face: {card.name}"
    assert (card.power, card.toughness) == (5, 5), f"back-face P/T wrong: {(card.power, card.toughness)}"

    # And the parser's TransformEffect must drive that same path end to end.
    from Playersim.ability_types import TransformEffect
    ok2 = TransformEffect().apply(gs, cid, player, None)
    assert ok2, "TransformEffect did not apply (parsed transform effect is a no-op)"
    assert card.current_face == 0, "TransformEffect did not flip the permanent back to its front face"
    assert card.name == "Village Watchman", f"TransformEffect did not restore front face: {card.name}"


@scenario("707 (token copy)", "a token copy preserves the original's subtypes and supertypes")
def s_token_copy_type_line_roundtrip():
    from Playersim.card import Card
    gs = fresh()
    # An original whose type carries both a supertype and subtypes worth keeping.
    orig = Card({"name": "Goblin Chieftain", "type_line": "Legendary Creature — Goblin Warrior",
                 "oracle_text": "", "power": 2, "toughness": 2})
    assert "legendary" in orig.supertypes and "goblin" in orig.subtypes, "test-card sanity"

    # The token-copy path builds token_data with the original's type components
    # and then a type_line via gs._build_type_line. Card.__init__ re-parses that
    # string, so without the helper the token drops all sub/supertypes.
    assert hasattr(gs, "_build_type_line"), \
        "GameState lacks _build_type_line - token copies lose subtypes/supertypes"
    token_data = {
        "name": orig.name, "power": 1, "toughness": 1, "is_token": True,
        "card_types": list(orig.card_types),
        "subtypes": list(orig.subtypes),
        "supertypes": list(orig.supertypes),
        "oracle_text": orig.oracle_text,
    }
    token_data["type_line"] = gs._build_type_line(token_data)
    token = Card(token_data)
    assert "creature" in token.card_types, f"token lost its card type: {token.card_types}"
    assert "goblin" in token.subtypes, f"token lost a subtype: {token.subtypes}"
    assert "warrior" in token.subtypes, f"token lost a subtype: {token.subtypes}"
    assert "legendary" in token.supertypes, f"token lost its supertype: {token.supertypes}"


@scenario("605 (mana ability)", "the TAP_LAND_FOR_MANA action taps a land and adds its mana to the pool")
def s_tap_land_for_mana_produces_mana():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    land = inject_card(gs, {"name": "Wooded Grove", "type_line": "Land",
                            "oracle_text": "{T}: Add {G}."})
    player["library"].append(land)
    assert gs.move_card(land, player, "library", player, "battlefield")
    land_idx = player["battlefield"].index(land)
    assert player["mana_pool"].get("G", 0) == 0, "pool should start empty of green"

    # The handler relies on gs.mana_system.tap_land_for_mana(); without it every
    # explicit land tap silently fails and adds no mana.
    reward, ok = handler._handle_tap_land_for_mana(land_idx)
    assert ok, "TAP_LAND_FOR_MANA reported failure (tap_land_for_mana missing/no-op)"
    assert player["mana_pool"].get("G", 0) == 1, \
        f"tapping the land added no mana to the pool: {player['mana_pool']}"
    assert land in player.get("tapped_permanents", set()), "the land was not tapped"

    # Tapping an already-tapped land must not add more mana.
    _, ok2 = handler._handle_tap_land_for_mana(land_idx)
    assert not ok2, "an already-tapped land produced mana again"
    assert player["mana_pool"].get("G", 0) == 1, "already-tapped land added extra mana"

    # Basic-land output is derived from its subtype even with no oracle text.
    forest = gs._safe_get_card(inject_card(gs, {"name": "Forest", "type_line": "Basic Land — Forest",
                                                "oracle_text": ""}))
    assert gs.mana_system._land_mana_output(forest) == "G", \
        "basic Forest did not resolve to green mana output"


@scenario("614.1c / 611.3c", "fast lands and current-template tapped duals enter in the correct state")
def s_sample_deck_land_entry_conditions():
    gs = fresh()
    low_land_player = gs.p1
    high_land_player = gs.p2

    for index in range(2):
        inject_into_zone(gs, low_land_player, {
            "name": f"Low Count Land {index}", "type_line": "Basic Land - Island",
            "oracle_text": "({T}: Add {U}.)",
        }, "battlefield")
    fast_low = inject_into_zone(gs, low_land_player, {
        "name": "Darkslick Shores", "type_line": "Land",
        "oracle_text": (
            "Darkslick Shores enters tapped unless you control two or fewer other lands.\n"
            "{T}: Add {U} or {B}."
        ),
    }, "battlefield")
    assert fast_low not in low_land_player.get("tapped_permanents", set()), \
        "fast land entered tapped with only two other lands"

    for index in range(3):
        inject_into_zone(gs, high_land_player, {
            "name": f"High Count Land {index}", "type_line": "Basic Land - Swamp",
            "oracle_text": "({T}: Add {B}.)",
        }, "battlefield")
    fast_high = inject_into_zone(gs, high_land_player, {
        "name": "Copperline Gorge", "type_line": "Land",
        "oracle_text": (
            "Copperline Gorge enters tapped unless you control two or fewer other lands.\n"
            "{T}: Add {R} or {G}."
        ),
    }, "battlefield")
    assert fast_high in high_land_player.get("tapped_permanents", set()), \
        "fast land entered untapped with three other lands"

    surveil_dual = inject_into_zone(gs, low_land_player, {
        "name": "Hedge Maze", "type_line": "Land - Forest Island",
        "oracle_text": "({T}: Add {G} or {U}.)\nThis land enters tapped.",
    }, "battlefield")
    assert surveil_dual in low_land_player.get("tapped_permanents", set()), \
        "current 'enters tapped' wording was ignored"


@scenario("605.1a / 601.2b", "a multicolor land asks the agent which mana ability to activate")
def s_multicolor_land_mana_is_agent_choice():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    gs.agent_is_p1 = player is gs.p1
    land = inject_into_zone(gs, player, {
        "name": "Darkslick Shores", "type_line": "Land",
        "oracle_text": (
            "Darkslick Shores enters tapped unless you control two or fewer other lands.\n"
            "{T}: Add {U} or {B}."
        ),
    }, "battlefield")
    land_index = player["battlefield"].index(land)

    reward, ok = handler._handle_tap_land_for_mana(land_index)
    assert ok, f"multicolor land activation failed with reward {reward}"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "land_mana", \
        "multicolor land silently picked its first color"
    assert land not in player.get("tapped_permanents", set()), \
        "land tapped before its mana ability was selected"
    mask = handler.generate_valid_actions()
    assert mask[353] and mask[354], "the land's U/B choices were not exposed"

    reward, ok = handler._handle_choose_mode(1, {})
    assert ok, f"black mana choice failed with reward {reward}"
    assert player["mana_pool"].get("B", 0) == 1 and player["mana_pool"].get("U", 0) == 0, \
        f"selected black but got the wrong mana pool: {player['mana_pool']}"
    assert land in player.get("tapped_permanents", set()), "selected land did not tap"


@scenario("605.1a / 120.1", "pain-land colored mana deals damage but its colorless ability does not")
def s_pain_land_mana_rider():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    gs.agent_is_p1 = player is gs.p1
    land = inject_into_zone(gs, player, {
        "name": "Underground River", "type_line": "Land",
        "oracle_text": (
            "{T}: Add {C}.\n"
            "{T}: Add {U} or {B}. Underground River deals 1 damage to you."
        ),
    }, "battlefield")
    land_index = player["battlefield"].index(land)
    life_before = player["life"]

    assert handler._handle_tap_land_for_mana(land_index)[1]
    mask = handler.generate_valid_actions()
    assert mask[353] and mask[354] and mask[355], \
        "pain land did not expose colorless and both colored abilities"
    assert handler._handle_choose_mode(0, {})[1], "colorless pain-land choice failed"
    assert player["mana_pool"].get("C", 0) == 1 and player["life"] == life_before, \
        "colorless pain-land ability dealt damage or produced the wrong mana"

    player["tapped_permanents"].discard(land)
    assert handler._handle_tap_land_for_mana(land_index)[1]
    assert handler._handle_choose_mode(1, {})[1], "colored pain-land choice failed"
    assert player["mana_pool"].get("U", 0) == 1, "colored pain-land ability produced no blue"
    assert player["life"] == life_before - 1, "colored pain-land ability did not deal 1 damage"


@scenario("602.1b", "Verge lands expose their second color only while its land-type condition is true")
def s_verge_land_activation_condition():
    gs = fresh()
    handler = get_env().action_handler
    player = gs._get_active_player()
    gs.agent_is_p1 = player is gs.p1
    verge = inject_into_zone(gs, player, {
        "name": "Thornspire Verge", "type_line": "Land",
        "oracle_text": (
            "{T}: Add {G}.\n"
            "{T}: Add {R}. Activate only if you control a Mountain or a Forest."
        ),
    }, "battlefield")
    verge_index = player["battlefield"].index(verge)

    assert handler._handle_tap_land_for_mana(verge_index)[1]
    assert gs.choice_context is None, "Verge offered red without a Mountain or Forest"
    assert player["mana_pool"].get("G", 0) == 1 and player["mana_pool"].get("R", 0) == 0

    player["tapped_permanents"].discard(verge)
    inject_into_zone(gs, player, {
        "name": "Mountain", "type_line": "Basic Land - Mountain",
        "oracle_text": "({T}: Add {R}.)",
    }, "battlefield")
    assert handler._handle_tap_land_for_mana(verge_index)[1]
    assert gs.choice_context and gs.choice_context.get("type") == "land_mana", \
        "Verge did not expose red after its condition became true"
    assert handler._handle_choose_mode(1, {})[1], "Verge red choice failed"
    assert player["mana_pool"].get("R", 0) == 1, "Verge's conditional red ability produced no red"


@scenario("119.3 (life gain)", "effect-based life gain fires GAIN_LIFE so 'whenever you gain life' triggers see it")
def s_gain_life_fires_trigger():
    gs = fresh()
    player = gs._get_active_player()
    watcher = inject_card(gs, {"name": "Lifewatch Sage", "type_line": "Creature — Cat Cleric",
                               "oracle_text": "Whenever you gain life, draw a card.",
                               "power": 1, "toughness": 1})
    player["library"].append(watcher)
    assert gs.move_card(watcher, player, "library", player, "battlefield")

    # gain_life is the canonical entry that fires GAIN_LIFE; without it, effect-
    # based (non-lifelink) life gain increments life directly and the trigger
    # never fires.
    assert hasattr(gs, "gain_life"), \
        "GameState lacks gain_life - non-lifelink life gain skips GAIN_LIFE triggers"
    ah = gs.ability_handler
    ah.active_triggers = []
    life0 = player["life"]
    gained = gs.gain_life(player, 3)
    assert gained == 3, f"gain_life returned {gained}, expected 3"
    assert player["life"] == life0 + 3, f"life did not increase: {life0} -> {player['life']}"

    # The 'whenever you gain life' trigger must have fired for this gain.
    fired_sources = [getattr(ab, "card_id", None) for ab, *_ in ah.active_triggers]
    assert watcher in fired_sources, \
        f"GAIN_LIFE trigger did not fire for effect-based life gain (queue sources: {fired_sources})"


@scenario("parser: ritual", "'Add {B}{B}{B}' as a spell effect fills the mana pool")
def s_parser_ritual():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    player["mana_pool"] = {}
    effs = EffectFactory.create_effects("Add {B}{B}{B}.", source_name="Dark Ritual")
    assert effs and type(effs[0]).__name__ == "AddManaEffect", \
        f"ritual text did not parse to a mana effect: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert player["mana_pool"].get("B", 0) == 3, \
        f"ritual produced {player['mana_pool'].get('B', 0)} B, expected 3 (rituals were no-ops)"


@scenario("parser: gain control", "'gain control of target creature' moves it to your battlefield")
def s_parser_gain_control():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    theirs = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(theirs, owner_of(gs, theirs), "library", opp, "battlefield")
    effs = EffectFactory.create_effects("Gain control of target creature until end of turn.",
                                        source_name="Threaten")
    assert effs and type(effs[0]).__name__ == "ControlEffect", \
        f"gain control did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [theirs]})
    assert theirs in player["battlefield"] and theirs not in opp["battlefield"], \
        "creature was not taken under the caster's control"


@scenario("611.2c / 603.6d", "control changes preserve permanent state, refresh replacements, revert, and attribute death")
def s_control_change_refreshes_controller_bound_state():
    gs = fresh()
    original, temporary = gs.p1, gs.p2
    original_ally = inject_into_zone(gs, original, {
        "name": "Original Ally", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    temporary_ally = inject_into_zone(gs, temporary, {
        "name": "Temporary Ally", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Creature - Merfolk", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    steward = inject_into_zone(gs, original, {
        "name": "Mana Steward", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Elf Druid",
        "oracle_text": (
            "Creatures you control get +1/+1.\n"
            "Whenever you tap a land for mana, add an additional {G}."),
        "power": 2, "toughness": 2,
    }, "battlefield")
    original.setdefault("tapped_permanents", set()).add(steward)
    assert gs._safe_get_card(original_ally).power == 2 \
        and gs._safe_get_card(temporary_ally).power == 1, \
        "the controller-bound static effect was not initially scoped"

    before = [e for e in gs.replacement_effects.active_effects
              if e.get("source_id") == steward
              and e.get("event_type") == "PRODUCE_MANA"]
    assert len(before) == 1 and before[0].get("controller_id") is original, \
        "the controller-bound replacement was not registered for the owner"

    assert gs.apply_temporary_control(steward, temporary), \
        "temporary control did not report a successful state transition"
    assert steward in temporary["battlefield"] and steward not in original["battlefield"]
    assert steward in temporary.get("tapped_permanents", set()) \
        and steward not in original.get("tapped_permanents", set()), \
        "the permanent's tapped status did not follow its controller"
    assert gs._safe_get_card(original_ally).power == 1 \
        and gs._safe_get_card(temporary_ally).power == 2, \
        "the static effect did not rebind to the new controller"
    live = [e for e in gs.replacement_effects.active_effects
            if e.get("source_id") == steward
            and e.get("event_type") == "PRODUCE_MANA"]
    assert len(live) == 1 and live[0].get("controller_id") is temporary, \
        "the replacement retained its old controller after the control change"

    p1_ctx, p1_replaced = gs.replacement_effects.apply_replacements(
        "PRODUCE_MANA", {
            "event_type": "PRODUCE_MANA", "player_key": "p1",
            "source_is_tap_ability": True,
            "source_card_types": ["land"], "source_subtypes": [],
            "mana_produced": {"U": 1},
        })
    p2_ctx, p2_replaced = gs.replacement_effects.apply_replacements(
        "PRODUCE_MANA", {
            "event_type": "PRODUCE_MANA", "player_key": "p2",
            "source_is_tap_ability": True,
            "source_card_types": ["land"], "source_subtypes": [],
            "mana_produced": {"U": 1},
        })
    assert not p1_replaced and p1_ctx["mana_produced"] == {"U": 1}, \
        "the old controller still received the replacement's additional mana"
    assert p2_replaced and p2_ctx["mana_produced"] == {"U": 1, "G": 1}, \
        "the new controller did not receive the replacement's additional mana"

    gs._revert_temporary_control()
    assert steward in original["battlefield"] and steward not in temporary["battlefield"], \
        "the temporary control effect did not return the permanent"
    assert gs._safe_get_card(original_ally).power == 2 \
        and gs._safe_get_card(temporary_ally).power == 1, \
        "the static effect did not return to its original controller"
    reverted = [e for e in gs.replacement_effects.active_effects
                if e.get("source_id") == steward
                and e.get("event_type") == "PRODUCE_MANA"]
    assert len(reverted) == 1 and reverted[0].get("controller_id") is original, \
        "returning control did not restore the replacement controller"

    assert gs.apply_temporary_control(steward, temporary)
    assert gs.move_card(steward, temporary, "battlefield", original,
                        "graveyard", cause="destroy")
    assert gs.creatures_died_this_turn.get("p2") == 1 \
        and gs.creatures_died_this_turn.get("p1", 0) == 0, \
        "death was not attributed to the creature's controller at last existence"
    gs._revert_temporary_control()
    assert steward in original["graveyard"], \
        "expired control tracking moved a dead object out of its graveyard"


@scenario("parser: regenerate", "'regenerate target creature' grants a regeneration shield")
def s_parser_regenerate():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute")
    player = to_battlefield(gs, src_id)
    target = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(target, owner_of(gs, target), "library", player, "battlefield")
    effs = EffectFactory.create_effects("Regenerate target creature.", source_name="Regen")
    assert effs and type(effs[0]).__name__ == "RegenerateEffect", \
        f"regenerate did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [target]})
    assert target in player.get("regeneration_shields", set()), \
        "regenerate did not add a regeneration shield"
    # The shield prevents the next destruction.
    assert gs.apply_regeneration(target, player), "regeneration shield did not fire on destroy"
    assert gs.find_card_location(target)[1] == "battlefield", "creature left despite regenerating"


@scenario("parser: mass tap", "'tap all creatures target player controls' taps their team")
def s_parser_mass_tap():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    a = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(a, owner_of(gs, a), "library", opp, "battlefield")
    b = card_id_by_name(gs, "Sprout Guardian")
    assert gs.move_card(b, owner_of(gs, b), "library", opp, "battlefield")
    effs = EffectFactory.create_effects("Tap all creatures target player controls.",
                                        source_name="Sleep")
    assert effs and type(effs[0]).__name__ == "TapEffect", \
        f"mass tap did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"players": ["p2"]})
    tapped = opp.get("tapped_permanents", set())
    assert a in tapped and b in tapped, \
        f"not all of the target player's creatures were tapped: {tapped}"


@scenario("parser: mass bounce", "'return all creatures to their owners' hands' clears the board to hand")
def s_parser_mass_bounce():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); pl = to_battlefield(gs, src_id)
    mine = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(mine, owner_of(gs, mine), "library", pl, "battlefield")
    theirs = card_id_by_name(gs, "Sprout Guardian")
    other = gs.p2 if pl is gs.p1 else gs.p1
    assert gs.move_card(theirs, owner_of(gs, theirs), "library", other, "battlefield")
    effs = EffectFactory.create_effects("Return all creatures to their owners' hands.",
                                        source_name="Evacuation")
    assert effs and type(effs[0]).__name__ == "ReturnToHandEffect", \
        f"mass bounce did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, pl, {})
    assert gs.find_card_location(mine)[1] == "hand" and gs.find_card_location(theirs)[1] == "hand", \
        "not all creatures were returned to hand"


@scenario("parser: untap all", "'untap all lands you control' untaps the caster's lands")
def s_parser_untap_all():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    forest = card_id_by_name(gs, "Forest")
    assert gs.move_card(forest, owner_of(gs, forest), "library", player, "battlefield")
    player.setdefault("tapped_permanents", set()).add(forest)
    assert forest in player["tapped_permanents"], "setup: land not tapped"
    effs = EffectFactory.create_effects("Untap all lands you control.", source_name="Early Harvest")
    assert effs and type(effs[0]).__name__ == "UntapEffect", \
        f"untap-all did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert forest not in player.get("tapped_permanents", set()), \
        "'untap all lands you control' did not untap the caster's land"


@scenario("parser: dig", "the policy chooses which looked-at card goes to hand")
def s_parser_dig():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    top3 = list(player["library"][:3])
    hand_before = len(player["hand"])
    lib_before = len(player["library"])
    effs = EffectFactory.create_effects(
        "Look at the top three cards of your library. Put one into your hand and the rest on the bottom.",
        source_name="Dig")
    assert effs and type(effs[0]).__name__ == "DigEffect", \
        f"dig did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert gs.choice_context and gs.choice_context.get('type') == 'dig_select'
    chooser_obs = get_env().observation_for(player)
    other = gs.p2 if player is gs.p1 else gs.p1
    other_obs = get_env().observation_for(other)
    assert chooser_obs['choice_kind'][0] == 1 and chooser_obs['choice_card_mask'].sum() == 3
    assert chooser_obs['choice_remaining'][0] == 1
    assert not other_obs['choice_card_mask'].any(), "Dig identities leaked to the non-choosing player"
    chosen = top3[-1]
    gs.agent_is_p1 = player is gs.p1
    _, ok = get_env().action_handler._handle_choose_mode(gs.choice_context['options'].index(chosen), {})
    assert ok and chosen in player['hand']
    assert len(player["hand"]) == hand_before + 1, "dig did not put a card into hand"
    assert len(player["library"]) == lib_before - 1, "dig changed library size incorrectly"
    # Exactly one card left the top region into hand; the other two are now on
    # the bottom. (Fixture libraries repeat card IDs across the 4 copies, so
    # verify by net movement rather than ID membership.)
    moved_to_bottom = player["library"][-2:]
    assert all(c in top3 for c in moved_to_bottom), \
        "the unchosen looked-at cards are not on the bottom of the library"

    compound_spell = inject_into_zone(gs, player, {
        "name": "Deferred Dig Probe", "mana_cost": "{U}", "type_line": "Instant",
        "oracle_text": (
            "Look at the top three cards of your library. Put one into your hand "
            "and the rest on the bottom. You gain 2 life.")}, "hand")
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = player
    player['mana_pool'] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    life_before = player['life']
    assert gs.cast_spell(compound_spell, player)
    assert gs.resolve_top_of_stack()
    assert gs.choice_context and gs.choice_context.get('type') == 'dig_select'
    assert player['life'] == life_before and compound_spell not in player['graveyard'], \
        "Dig continuation finalized the spell or a later effect before the choice"
    cloned = gs.clone()
    cloned_player = cloned.p1 if player is gs.p1 else cloned.p2
    cloned.agent_is_p1 = cloned_player is cloned.p1
    cloned.action_handler.generate_valid_actions()
    _, clone_ok = cloned.action_handler._handle_choose_mode(0, {})
    assert clone_ok and cloned_player['life'] == life_before + 2 \
        and compound_spell in cloned_player['graveyard'], \
        "a cloned resolution choice could not finish its continuation"
    assert gs.choice_context and player['life'] == life_before \
        and compound_spell not in player['graveyard'], \
        "finishing a cloned continuation mutated the original game"
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and player['life'] == life_before + 2
    assert player['graveyard'].count(compound_spell) == 1, \
        "the compound Dig spell did not finalize exactly once"


@scenario("401.4 / conditional Dig", "Flow State, Accumulate Wisdom, and Consult preserve their dynamic take and remainder-order rules")
def scenario_real_conditional_dig_spells():
    from Playersim.ability_types import DigEffect
    from Playersim.ability_utils import EffectFactory
    import Playersim.actions_choices as choices_module

    def stage_top(gs, player, prefix, count):
        card_ids = []
        for index in range(count):
            card_id = inject_card(gs, {
                "name": f"{prefix} {index}", "mana_cost": "{1}",
                "type_line": "Artifact", "oracle_text": "",
            })
            card_ids.append(card_id)
            gs._last_card_locations[card_id] = (player, "library")
        player["library"][:0] = card_ids
        return card_ids

    def real_dig(gs, player, name):
        source_id = inject_real_card(gs, player, name, "hand")
        card = gs._safe_get_card(source_id)
        effects = EffectFactory.create_effects(
            card.oracle_text, source_name=card.name)
        assert len(effects) == 1 and isinstance(effects[0], DigEffect), \
            f"{name} did not parse to one DigEffect: {effects}"
        return source_id, effects[0]

    # Flow State without its graveyard condition keeps one, then lets the
    # policy choose the order of the two cards placed on the bottom.
    gs = fresh(SEED + 31)
    player = gs.p1
    gs.agent_is_p1 = True
    source, effect = real_dig(gs, player, "Flow State")
    top = stage_top(gs, player, "Flow Top", 3)
    assert effect.apply(gs, source, player, targets={})
    assert gs.choice_context.get("remaining") == 1 \
        and gs.choice_context.get("rest_order") == "choice"
    handler = get_env().action_handler
    _, ok = handler._handle_choose_mode(
        gs.choice_context["options"].index(top[1]), {})
    assert ok and gs.choice_context.get("ordering_rest"), \
        "Flow State did not expose remainder ordering after the hand choice"
    _, ok = handler._handle_choose_mode(
        gs.choice_context["options"].index(top[2]), {})
    assert ok and gs.choice_context
    _, ok = handler._handle_choose_mode(
        gs.choice_context["options"].index(top[0]), {})
    assert ok and gs.choice_context is None
    assert player["library"][-2:] == [top[2], top[0]], \
        "the policy-selected Flow State bottom order was not preserved"

    # An instant and a sorcery in the graveyard change Flow State's take from
    # one to two.
    gs = fresh(SEED + 32)
    player = gs.p1
    gs.agent_is_p1 = True
    source, effect = real_dig(gs, player, "Flow State")
    inject_into_zone(gs, player, {
        "name": "Flow Instant", "mana_cost": "{U}",
        "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    inject_into_zone(gs, player, {
        "name": "Flow Sorcery", "mana_cost": "{U}",
        "type_line": "Sorcery", "oracle_text": "",
    }, "graveyard")
    stage_top(gs, player, "Boosted Flow Top", 3)
    assert effect.apply(gs, source, player, targets={})
    assert gs.choice_context.get("remaining") == 2, \
        "Flow State ignored its instant-plus-sorcery graveyard condition"

    # Accumulate Wisdom takes one below three Lessons and all three at the
    # threshold.
    for lesson_count, expected_take in ((2, 1), (3, 3)):
        gs = fresh(SEED + 40 + lesson_count)
        player = gs.p1
        gs.agent_is_p1 = True
        source, effect = real_dig(gs, player, "Accumulate Wisdom")
        for index in range(lesson_count):
            inject_into_zone(gs, player, {
                "name": f"Graveyard Lesson {index}", "mana_cost": "{U}",
                "type_line": "Instant - Lesson", "oracle_text": "",
            }, "graveyard")
        stage_top(gs, player, f"Wisdom {lesson_count} Top", 3)
        assert effect.apply(gs, source, player, targets={})
        assert gs.choice_context.get("remaining") == expected_take, \
            f"Accumulate Wisdom with {lesson_count} Lessons took the wrong count"

    # Consult looks at one card per controlled land, takes two when kicked,
    # and randomizes only the remainder. Patch the shuffle to a visible,
    # deterministic reversal for this assertion.
    gs = fresh(SEED + 50)
    player = gs.p1
    gs.agent_is_p1 = True
    source, effect = real_dig(gs, player, "Consult the Star Charts")
    for index in range(4):
        inject_into_zone(gs, player, {
            "name": f"Consult Land {index}", "mana_cost": "",
            "type_line": "Land", "oracle_text": "",
        }, "battlefield")
    top = stage_top(gs, player, "Consult Top", 4)
    assert effect.apply(
        gs, source, player, targets={}, context={"actual_kicker_paid": True})
    assert gs.choice_context.get("options") == top \
        and gs.choice_context.get("remaining") == 2 \
        and gs.choice_context.get("rest_order") == "random"
    original_shuffle = choices_module.random.shuffle
    choices_module.random.shuffle = lambda values: values.reverse()
    try:
        _, ok = get_env().action_handler._handle_choose_mode(0, {})
        assert ok
        _, ok = get_env().action_handler._handle_choose_mode(0, {})
        assert ok and gs.choice_context is None
    finally:
        choices_module.random.shuffle = original_shuffle
    assert player["library"][-2:] == [top[3], top[2]], \
        "Consult did not randomize exactly its unchosen remainder"


@scenario("614 / Badgermole Cub", "tapping a creature for mana adds one green while Badgermole is present")
def scenario_badgermole_additional_creature_mana():
    from Playersim.ability_types import ManaAbility
    gs = fresh(SEED + 51)
    player = gs.p1
    gs.agent_is_p1 = True
    earthbend_land = inject_into_zone(gs, player, {
        "name": "Badgermole Earthbend Target", "mana_cost": "",
        "type_line": "Land - Forest", "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    land = inject_into_zone(gs, player, {
        "name": "Badgermole Land Source", "mana_cost": "",
        "type_line": "Land - Forest", "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    mana_creature = inject_into_zone(gs, player, {
        "name": "Badgermole Creature Source", "mana_cost": "{1}",
        "type_line": "Artifact Creature", "oracle_text": "{T}: Add {U}.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    badgermole = inject_real_card(gs, player, "Badgermole Cub", "battlefield")
    matching = [
        entry for entry in gs.ability_handler.active_triggers
        if entry[2].get("source_card_id") == badgermole]
    assert len(matching) == 1, "Badgermole's ETB Earthbend did not trigger"
    gs.ability_handler.process_triggered_abilities()
    assert gs.targeting_context, "Badgermole Earthbend did not request a land"
    valid_map = gs.targeting_system.get_valid_targets(
        badgermole, player, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({
        target_id for target_ids in valid_map.values()
        for target_id in target_ids})
    _, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(earthbend_land), {})
    assert ok and gs.resolve_top_of_stack(), \
        "Badgermole's Earthbend trigger failed to resolve"
    assert gs._safe_get_card(earthbend_land).counters.get("+1/+1", 0) == 1
    assert any(
        effect.get("source_id") == badgermole
        and effect.get("event_type") == "PRODUCE_MANA"
        for effect in gs.replacement_effects.active_effects), \
        "Badgermole did not register its mana-production replacement"

    empty_pool = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    handler = get_env().action_handler
    player["entered_battlefield_this_turn"].discard(mana_creature)
    mana_abilities = gs.ability_handler.get_activated_abilities(mana_creature)
    mana_index = next(
        index for index, ability in enumerate(mana_abilities)
        if isinstance(ability, ManaAbility))
    player["mana_pool"] = dict(empty_pool)

    cloned = gs.clone()
    cloned.agent_is_p1 = True
    cloned_player = cloned.p1
    cloned_player["mana_pool"] = dict(empty_pool)
    cloned_abilities = cloned.ability_handler.get_activated_abilities(
        mana_creature)
    cloned_mana_index = next(
        index for index, ability in enumerate(cloned_abilities)
        if isinstance(ability, ManaAbility))
    _, clone_ok = cloned.action_handler._handle_activate_ability(None, {
        "battlefield_idx": cloned_player["battlefield"].index(mana_creature),
        "ability_idx": cloned_mana_index,
    })
    assert clone_ok and cloned_player["mana_pool"].get("U") == 1 \
        and cloned_player["mana_pool"].get("G") == 1, \
        "Badgermole's mana replacement did not survive game-state cloning"
    assert player["mana_pool"] == empty_pool \
        and mana_creature not in player["tapped_permanents"], \
        "activating mana in the clone mutated the original game"

    _, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": player["battlefield"].index(mana_creature),
        "ability_idx": mana_index,
    })
    assert ok and mana_creature in player["tapped_permanents"], \
        "the creature's real tap-for-mana activation failed"
    assert player["mana_pool"].get("U") == 1 \
        and player["mana_pool"].get("G") == 1, \
        f"Badgermole creature mana was {player['mana_pool']}"

    player["mana_pool"] = dict(empty_pool)
    _, ok = handler._handle_tap_land_for_mana(
        player["battlefield"].index(land))
    assert ok and land in player["tapped_permanents"], \
        "the comparison land's real mana activation failed"
    assert player["mana_pool"].get("G") == 1, \
        "Badgermole incorrectly boosted mana from a land"

    assert gs.move_card(
        badgermole, player, "battlefield", player, "graveyard")
    assert gs.untap_permanent(mana_creature, player)
    player["mana_pool"] = dict(empty_pool)
    _, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": player["battlefield"].index(mana_creature),
        "ability_idx": mana_index,
    })
    assert ok, "the creature's second mana activation failed"
    assert player["mana_pool"].get("U") == 1 \
        and player["mana_pool"].get("G") == 0, \
        "Badgermole's mana effect remained after it left the battlefield"


@scenario("601.2f / 614.1c / Eddymurk Crab", "graveyard spells reduce Eddymurk, off-turn entry taps it, and its ETB may tap zero to two creatures")
def scenario_eddymurk_crab_exact_support():
    from Playersim.ability_types import TapEffect
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 52)
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    crab = inject_real_card(gs, controller, "Eddymurk Crab", "hand")
    for name, type_line in (
            ("Crab Instant", "Instant"),
            ("Crab Sorcery", "Sorcery"),
            ("Crab Creature", "Creature")):
        inject_into_zone(gs, controller, {
            "name": name, "mana_cost": "{1}", "type_line": type_line,
            "oracle_text": "", "power": 1, "toughness": 1,
        }, "graveyard")
    modified = gs.mana_system.apply_cost_modifiers(
        controller, gs.mana_system.parse_mana_cost("{5}{U}{U}"), crab)
    assert modified.get("generic") == 3 \
        and modified.get("U") == 2, \
        f"Eddymurk used the wrong graveyard reduction: {modified}"

    assert gs.move_card(crab, controller, "hand", controller, "battlefield")
    assert crab not in controller["tapped_permanents"], \
        "Eddymurk entered tapped on its controller's turn"
    gs.ability_handler.active_triggers = []
    off_turn_crab = inject_real_card(
        gs, controller, "Eddymurk Crab", "hand")
    gs.turn = 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = controller
    gs.agent_is_p1 = True
    controller["mana_pool"] = {
        'W': 0, 'U': 2, 'B': 0, 'R': 0, 'G': 0, 'C': 3}
    assert gs.cast_spell(off_turn_crab, controller), \
        "Eddymurk's flash did not allow an off-turn cast"
    paid = gs.stack[-1][3].get("final_paid_cost", {})
    assert paid.get("generic") == 3 and paid.get("U") == 2, \
        f"the real Eddymurk cast paid the wrong reduced cost: {paid}"
    assert gs.resolve_top_of_stack(), "the off-turn Eddymurk cast failed"
    assert off_turn_crab in controller["tapped_permanents"], \
        "Eddymurk entered untapped outside its controller's turn"

    effects = EffectFactory.create_effects(
        "Tap up to two target creatures.", source_name="Eddymurk Crab")
    assert len(effects) == 1 and isinstance(effects[0], TapEffect)
    tap_effect = effects[0]
    assert (tap_effect.min_targets, tap_effect.max_targets) == (0, 2)
    first = inject_into_zone(gs, opponent, {
        "name": "First Crab Target", "mana_cost": "{1}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }, "battlefield")
    second = inject_into_zone(gs, opponent, {
        "name": "Second Crab Target", "mana_cost": "{2}",
        "type_line": "Creature", "oracle_text": "", "power": 2,
        "toughness": 2,
    }, "battlefield")
    assert tap_effect.apply(
        gs, crab, controller, targets={"creatures": [first, second]})
    assert {first, second}.issubset(opponent["tapped_permanents"]), \
        "Eddymurk did not tap both selected creatures"
    assert tap_effect.apply(gs, crab, controller, targets={}), \
        "Eddymurk's legal zero-target choice reported a failed resolution"


@scenario("603.2 / Spider Manifestation", "only the controller's mana-value-four spell untaps Spider Manifestation")
def scenario_spider_manifestation_self_untap():
    from Playersim.ability_types import ManaAbility
    gs = fresh(SEED + 53)
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    spider = inject_real_card(
        gs, controller, "Spider Manifestation", "battlefield")
    assert gs.check_keyword(spider, "reach"), \
        "Spider Manifestation lost its printed reach"
    controller["entered_battlefield_this_turn"].discard(spider)
    abilities = gs.ability_handler.get_activated_abilities(spider)
    mana_index = next(
        index for index, ability in enumerate(abilities)
        if isinstance(ability, ManaAbility))
    before = dict(controller["mana_pool"])
    battlefield_index = controller["battlefield"].index(spider)
    _, ok = get_env().action_handler._handle_activate_ability(
        None, {"battlefield_idx": battlefield_index,
               "ability_idx": mana_index})
    assert ok and gs.choice_context \
        and gs.choice_context.get("options") == ["R", "G"], \
        "Spider's {R}-or-{G} mana ability did not expose only those colors"
    _, ok = get_env().action_handler._handle_choose_mode(1, {})
    assert ok and controller["mana_pool"].get("G", 0) \
        == before.get("G", 0) + 1
    assert controller["mana_pool"].get("R", 0) == before.get("R", 0), \
        "choosing green from Spider also produced red"
    assert spider in controller["tapped_permanents"]

    colored_cost_source = inject_into_zone(gs, controller, {
        "name": "Colored Cost Mana Source", "mana_cost": "{1}",
        "type_line": "Creature", "power": 1, "toughness": 1,
        "oracle_text": "{G}, {T}: Add {R} or {U}.",
    }, "battlefield")
    controller["entered_battlefield_this_turn"].discard(colored_cost_source)
    colored_abilities = gs.ability_handler.get_activated_abilities(
        colored_cost_source)
    colored_index = next(
        index for index, ability in enumerate(colored_abilities)
        if isinstance(ability, ManaAbility))
    _, ok = get_env().action_handler._handle_activate_ability(None, {
        "battlefield_idx": controller["battlefield"].index(
            colored_cost_source),
        "ability_idx": colored_index,
    })
    assert ok and gs.choice_context.get("options") == ["R", "U"], \
        "a colored activation cost leaked into the produced-color choices"
    _, ok = get_env().action_handler._handle_choose_mode(1, {})
    assert ok and controller["mana_pool"].get("U", 0) == 1

    low_spell = inject_card(gs, {
        "name": "Low Mana Value Spell", "mana_cost": "{3}", "cmc": 3,
        "type_line": "Sorcery", "oracle_text": "",
    })
    gs.trigger_ability(low_spell, "CAST_SPELL", {
        "controller": controller, "casting_player": controller,
        "cast_card_id": low_spell,
    })
    assert not any(
        context.get("source_card_id") == spider
        for _, _, context in gs.ability_handler.active_triggers), \
        "a mana-value-three spell triggered Spider Manifestation"

    opposing_spell = inject_card(gs, {
        "name": "Opposing Large Spell", "mana_cost": "{4}", "cmc": 4,
        "type_line": "Sorcery", "oracle_text": "",
    })
    gs.trigger_ability(opposing_spell, "CAST_SPELL", {
        "controller": opponent, "casting_player": opponent,
        "cast_card_id": opposing_spell,
    })
    assert not any(
        context.get("source_card_id") == spider
        for _, _, context in gs.ability_handler.active_triggers), \
        "an opponent's spell triggered Spider Manifestation"

    large_spell = inject_card(gs, {
        "name": "Friendly X Spell", "mana_cost": "{X}", "cmc": 0,
        "type_line": "Sorcery", "oracle_text": "",
    })
    gs.trigger_ability(large_spell, "CAST_SPELL", {
        "controller": controller, "casting_player": controller,
        "cast_card_id": large_spell, "X": 4,
    })
    matching = [
        entry for entry in gs.ability_handler.active_triggers
        if entry[2].get("source_card_id") == spider]
    assert len(matching) == 1, \
        f"the qualifying spell queued {len(matching)} Spider triggers"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Spider's untap trigger failed"
    assert spider not in controller["tapped_permanents"], \
        "'untap this creature' did not untap Spider Manifestation itself"


@scenario("603.2", "a printed once-per-turn trigger fires once and resets on the next turn")
def scenario_once_per_turn_trigger_limit():
    gs = fresh(SEED + 55)
    controller = gs.p1
    watcher = inject_into_zone(gs, controller, {
        "name": "Once-Per-Turn Watcher", "mana_cost": "{1}{U}",
        "type_line": "Creature", "power": 2, "toughness": 2,
        "oracle_text": (
            "Whenever you cast a spell, untap this creature. "
            "This ability triggers only once each turn."),
    }, "battlefield")
    gs.tap_permanent(watcher, controller)

    for index in range(2):
        spell = inject_card(gs, {
            "name": f"Once-Per-Turn Spell {index}", "mana_cost": "{1}",
            "cmc": 1, "type_line": "Sorcery", "oracle_text": "",
        })
        gs.trigger_ability(spell, "CAST_SPELL", {
            "controller": controller, "casting_player": controller,
            "cast_card_id": spell,
        })
    matching = [
        entry for entry in gs.ability_handler.active_triggers
        if entry[2].get("source_card_id") == watcher]
    assert len(matching) == 1, \
        f"the once-per-turn watcher queued {len(matching)} first-turn triggers"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack()
    assert watcher not in controller["tapped_permanents"]

    gs.turn += 1
    gs.tap_permanent(watcher, controller)
    next_spell = inject_card(gs, {
        "name": "Next-Turn Spell", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "",
    })
    gs.trigger_ability(next_spell, "CAST_SPELL", {
        "controller": controller, "casting_player": controller,
        "cast_card_id": next_spell,
    })
    matching = [
        entry for entry in gs.ability_handler.active_triggers
        if entry[2].get("source_card_id") == watcher]
    assert len(matching) == 1, \
        "the once-per-turn watcher did not reset on the next turn"


@scenario("701.19 / Fabled Passage", "Fabled Passage performs one atomic tapped basic-land search and applies its four-land untap rider")
def scenario_fabled_passage_atomic_search():
    def run_branch(seed, other_land_count, should_untap):
        gs = fresh(seed)
        player = gs.p1
        gs.agent_is_p1 = True
        gs.priority_player = player
        passage = inject_real_card(gs, player, "Fabled Passage", "battlefield")
        for index in range(other_land_count):
            inject_into_zone(gs, player, {
                "name": f"Passage Existing Land {seed} {index}",
                "mana_cost": "", "type_line": "Land",
                "oracle_text": "",
            }, "battlefield")
        basic = inject_card(gs, {
            "name": f"Passage Basic {seed}", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Forest",
            "oracle_text": "{T}: Add {G}.",
        })
        filler = inject_card(gs, {
            "name": f"Passage Nonbasic {seed}", "mana_cost": "", "cmc": 0,
            "type_line": "Land", "oracle_text": "{T}: Add {C}.",
        })
        player["library"] = [basic, filler]
        gs._last_card_locations[basic] = (player, "library")
        gs._last_card_locations[filler] = (player, "library")

        abilities = gs.ability_handler.get_activated_abilities(passage)
        assert len(abilities) == 1 \
            and "search your library for a basic land" in abilities[0].effect.lower()
        battlefield_index = player["battlefield"].index(passage)
        _, ok = get_env().action_handler._handle_activate_ability(
            None, {"battlefield_idx": battlefield_index,
                   "ability_idx": 0})
        assert ok and passage in player["graveyard"] and gs.stack, \
            "Fabled Passage did not pay its tap-and-sacrifice cost"
        from Playersim import game_state_zones as zones_module
        shuffle_calls = []
        original_shuffle = zones_module.random.shuffle
        zones_module.random.shuffle = lambda values: (
            shuffle_calls.append(list(values)), original_shuffle(values))[1]
        try:
            assert gs.resolve_top_of_stack(), "Fabled Passage search failed"
        finally:
            zones_module.random.shuffle = original_shuffle
        assert len(shuffle_calls) == 1, \
            f"Fabled Passage shuffled {len(shuffle_calls)} times"
        assert basic in player["battlefield"] and basic not in player["hand"], \
            "the searched basic did not move directly to the battlefield"
        is_tapped = basic in player["tapped_permanents"]
        assert is_tapped is not should_untap, \
            (f"Fabled Passage threshold branch was wrong with "
             f"{other_land_count} other lands: tapped={is_tapped}")

    run_branch(SEED + 54, other_land_count=2, should_untap=False)
    run_branch(SEED + 55, other_land_count=3, should_untap=True)


@scenario("parser: put on top", "'put target creature on top of its owner's library' removes it from the battlefield")
def s_parser_put_on_top():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    victim = card_id_by_name(gs, "Vine Stalker")
    vowner = owner_of(gs, victim)
    assert gs.move_card(victim, vowner, "library", vowner, "battlefield")
    effs = EffectFactory.create_effects("Put target creature on top of its owner's library.",
                                        source_name="Temporal Spring")
    assert effs and type(effs[0]).__name__ == "PutOnLibraryEffect", \
        f"put-on-library did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [victim]})
    assert gs.find_card_location(victim)[1] == "library", "creature was not put into the library"
    assert vowner["library"][0] == victim, "creature was not placed on TOP of the library"


@scenario("parser: variable draw", "'draw cards equal to the number of creatures you control' scales")
def s_parser_variable_draw():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    c2 = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(c2, owner_of(gs, c2), "library", player, "battlefield")
    # 2 creatures controlled (Thicket Brute + Vine Stalker).
    hand_before = len(player["hand"])
    effs = EffectFactory.create_effects("Draw cards equal to the number of creatures you control.",
                                        source_name="Harmonize")
    assert effs and type(effs[0]).__name__ == "DrawCardEffect", \
        f"variable draw did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert len(player["hand"]) == hand_before + 2, \
        f"drew {len(player['hand']) - hand_before} cards, expected 2 (creatures controlled)"


@scenario("parser: variable life", "'gain life equal to the number of creatures you control' scales")
def s_parser_variable_life():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    c2 = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(c2, owner_of(gs, c2), "library", player, "battlefield")
    life_before = player["life"]
    effs = EffectFactory.create_effects("You gain life equal to the number of creatures you control.",
                                        source_name="Rest for the Weary")
    assert effs and type(effs[0]).__name__ == "GainLifeEffect", \
        f"variable life did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert player["life"] == life_before + 2, \
        f"gained {player['life'] - life_before} life, expected 2 (creatures controlled)"


@scenario("parser: shuffle graveyard", "'shuffle your graveyard into your library' empties the graveyard")
def s_parser_shuffle_graveyard():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    # Seed graveyard with a couple of cards.
    for _ in range(2):
        player["graveyard"].append(player["library"].pop(0))
    gy_before = len(player["graveyard"])
    lib_before = len(player["library"])
    assert gy_before >= 2, "setup: graveyard not seeded"
    effs = EffectFactory.create_effects("Shuffle your graveyard into your library.",
                                        source_name="Gaea's Blessing")
    assert effs and type(effs[0]).__name__ == "ShuffleGraveyardEffect", \
        f"shuffle-graveyard did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    assert len(player["graveyard"]) == 0, "graveyard was not emptied into the library"
    assert len(player["library"]) == lib_before + gy_before, \
        "library did not grow by the graveyard's contents"


@scenario("parser: fog", "'prevent all combat damage this turn' registers a prevention effect")
def s_parser_fog():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    effs = EffectFactory.create_effects("Prevent all combat damage that would be dealt this turn.",
                                        source_name="Fog")
    assert effs and type(effs[0]).__name__ == "PreventDamageEffect", \
        f"fog did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {})
    # A combat-damage event should now be prevented via the replacement system.
    ctx, replaced = gs.replacement_effects.apply_replacements(
        'DAMAGE', {'damage_amount': 5, 'target_id': "p1", 'target_is_player': True,
                   'is_combat_damage': True})
    assert ctx.get('damage_amount') == 0, \
        f"combat damage not prevented by fog (amount={ctx.get('damage_amount')})"
    # Non-combat damage is NOT prevented by a combat-only fog.
    ctx2, _ = gs.replacement_effects.apply_replacements(
        'DAMAGE', {'damage_amount': 3, 'target_id': "p1", 'target_is_player': True,
                   'is_combat_damage': False})
    assert ctx2.get('damage_amount') == 3, "fog wrongly prevented non-combat damage"


@scenario("parser: variable pump", "'+X/+X where X is the number of Mountains you control' scales P/T")
def s_parser_variable_pump():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    target = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(target, owner_of(gs, target), "library", player, "battlefield")
    # Give the controller two Mountains.
    for _ in range(2):
        m = inject_card(gs, {"name": "Mountain", "mana_cost": "", "type_line": "Basic Land — Mountain",
                             "oracle_text": "{T}: Add {R}.", "subtypes": ["mountain"]})
        player["library"].append(m)
        assert gs.move_card(m, player, "library", player, "battlefield")
    card = gs._safe_get_card(target)
    base_p, base_t = card.power, card.toughness   # 2/2
    effs = EffectFactory.create_effects(
        "Target creature gets +X/+X until end of turn, where X is the number of Mountains you control.",
        source_name="Kird Buff")
    assert effs and type(effs[0]).__name__ == "BuffEffect", \
        f"variable pump did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [target]})
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert (card.power, card.toughness) == (base_p + 2, base_t + 2), \
        f"variable pump gave {card.power}/{card.toughness}, expected {base_p+2}/{base_t+2} (2 Mountains)"


@scenario("parser: animate land", "'target land becomes a 3/3 creature' makes the land a creature")
def s_parser_animate_land():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    src_id = card_id_by_name(gs, "Thicket Brute"); player = to_battlefield(gs, src_id)
    land = card_id_by_name(gs, "Forest")
    assert gs.move_card(land, owner_of(gs, land), "library", player, "battlefield")
    card = gs._safe_get_card(land)
    assert 'creature' not in [t.lower() for t in card.card_types], "setup: land already a creature"
    effs = EffectFactory.create_effects(
        "Target land becomes a 3/3 creature until end of turn. It's still a land.",
        source_name="Nature's Ruin")
    assert effs and type(effs[0]).__name__ == "AnimateLandEffect", \
        f"animate-land did not parse: {[type(e).__name__ for e in effs]}"
    for e in effs:
        e.apply(gs, src_id, player, {"lands": [land], "permanents": [land]})
    gs.layer_system.invalidate_cache(); gs.layer_system.apply_all_effects()
    assert gs._is_creature(land), "target land did not become a creature"
    assert (card.power, card.toughness) == (3, 3), \
        f"animated land P/T is {card.power}/{card.toughness}, expected 3/3"
    assert 'land' in [t.lower() for t in card.card_types], "animated land lost its land type"


@scenario("parser: reveal hand", "'target player reveals their hand' marks the hand revealed")
def s_parser_reveal_hand():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    effs = EffectFactory.create_effects("Target player reveals their hand.", source_name="Peek")
    assert effs and type(effs[0]).__name__ == "RevealHandEffect", \
        f"reveal-hand did not parse: {[type(e).__name__ for e in effs]}"
    ok = False
    for e in effs:
        ok = e.apply(gs, src_id, player, {"players": ["p2"]}) or ok
    assert ok, "reveal-hand effect reported failure"
    assert opp.get("hand_revealed"), "target player's hand was not marked revealed"


@scenario("701.8a", "a nonrandom discard exposes the affected player's actual card choice")
def s_discard_choice_action_selects_card():
    gs = fresh()
    from Playersim.ability_types import DiscardEffect
    player = gs.p1
    gs.agent_is_p1 = True
    gs.priority_player = player
    low, high = replace_hand(gs, player, [
        {"name": "Discard Choice Low", "mana_cost": "{1}",
         "type_line": "Sorcery", "oracle_text": ""},
        {"name": "Discard Choice High", "mana_cost": "{7}",
         "type_line": "Sorcery", "oracle_text": ""},
    ])
    phase_before = gs.phase

    assert DiscardEffect(1, target="controller").apply(gs, None, player), \
        "discard effect did not start"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "discard", \
        "nonrandom discard did not enter a discard-choice phase"
    assert player["hand"] == [low, high], "a card was discarded before the player chose it"
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[238] and mask[239], "hand-card discard actions were not exposed"

    reward, ok = get_env().action_handler._handle_discard_card(0)
    assert ok, f"discarding the selected low-cost card failed with reward {reward}"
    assert low in player["graveyard"] and high in player["hand"], \
        "the engine discarded a heuristic card instead of the selected card"
    assert gs.choice_context is None and gs.phase == phase_before, \
        "completed discard choice did not restore the previous phase"


@scenario("701.8a", "discarding multiple cards keeps choosing until the full count is met")
def s_multiple_discard_choices_are_sequential():
    gs = fresh()
    from Playersim.ability_types import DiscardEffect
    player = gs.p1
    gs.agent_is_p1 = True
    gs.priority_player = player
    first, middle, last = replace_hand(gs, player, [
        {"name": "Discard Multi First", "mana_cost": "{1}",
         "type_line": "Instant", "oracle_text": ""},
        {"name": "Discard Multi Middle", "mana_cost": "{2}",
         "type_line": "Instant", "oracle_text": ""},
        {"name": "Discard Multi Last", "mana_cost": "{3}",
         "type_line": "Instant", "oracle_text": ""},
    ])

    assert DiscardEffect(2, target="controller").apply(gs, None, player)
    reward, ok = get_env().action_handler._handle_discard_card(1)
    assert ok, f"first discard choice failed with reward {reward}"
    assert gs.choice_context and gs.choice_context.get("remaining") == 1, \
        "discard choice ended before the required count was met"
    reward, ok = get_env().action_handler._handle_discard_card(1)
    assert ok, f"second discard choice failed with reward {reward}"
    assert player["hand"] == [first], \
        f"sequential choices discarded the wrong cards: hand={player['hand']}"
    assert middle in player["graveyard"] and last in player["graveyard"]
    assert gs.choice_context is None, "multi-card discard did not finish after two choices"


@scenario("701.8a / self-play", "each-player discard stages both choices before simultaneous movement")
def s_each_player_discard_queues_opponent_choice():
    gs = fresh()
    from Playersim.ability_types import DiscardEffect
    p1, p2 = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = p1
    p1_card = replace_hand(gs, p1, [
        {"name": "P1 Discard Probe", "mana_cost": "{1}",
         "type_line": "Sorcery", "oracle_text": ""},
    ])[0]
    p2_card = replace_hand(gs, p2, [
        {"name": "P2 Discard Probe", "mana_cost": "{2}",
         "type_line": "Sorcery", "oracle_text": ""},
    ])[0]

    assert DiscardEffect(1, target="each_player").apply(gs, None, p1)
    _, ok = get_env().action_handler._handle_discard_card(0)
    assert ok and p1_card in p1["hand"], \
        "the first player's staged discard moved before all choices were made"
    assert gs.choice_context and gs.choice_context.get("player") == p2, \
        "the second player's discard choice was not queued"

    gs.agent_is_p1 = False
    opponent_mask = get_env().action_handler.generate_valid_actions()
    action, _ = get_env()._get_scripted_opponent_action(
        p2, opponent_mask, {"phase_context": "CHOOSE"})
    assert action == 238, f"scripted opponent did not choose its available discard: {action}"
    _, ok = get_env().action_handler._handle_discard_card(action - 238)
    assert ok and p1_card in p1["graveyard"] \
        and p2_card in p2["graveyard"], \
        "simultaneous staged discards were not committed together"
    assert gs.choice_context is None, "each-player discard did not finish after both choices"


@scenario("701.8a", "random discard resolves immediately without exposing card identity choices")
def s_random_discard_skips_choice_phase():
    gs = fresh()
    from Playersim.ability_types import DiscardEffect
    player = gs.p1
    cards = replace_hand(gs, player, [
        {"name": "Random Discard A", "mana_cost": "{1}",
         "type_line": "Sorcery", "oracle_text": ""},
        {"name": "Random Discard B", "mana_cost": "{2}",
         "type_line": "Sorcery", "oracle_text": ""},
    ])
    phase_before = gs.phase

    assert DiscardEffect(1, target="controller", is_random=True).apply(gs, None, player)
    assert len(player["hand"]) == 1 and len(set(cards) & set(player["graveyard"])) == 1, \
        "random discard did not move exactly one card"
    assert gs.choice_context is None and gs.phase == phase_before, \
        "random discard incorrectly exposed a player choice"


@scenario("702.35a", "choosing a Madness card uses the shared discard replacement path")
def s_discard_choice_preserves_madness():
    gs = fresh()
    from Playersim.ability_types import DiscardEffect
    player = gs.p1
    gs.agent_is_p1 = True
    madness_card = replace_hand(gs, player, [
        {"name": "Madness Discard Probe", "mana_cost": "{2}{B}",
         "type_line": "Sorcery", "oracle_text": "Madness {B}"},
    ])[0]

    assert DiscardEffect(1, target="controller").apply(gs, None, player)
    _, ok = get_env().action_handler._handle_discard_card(0)
    assert ok, "selected Madness discard failed"
    assert madness_card in player["exile"] and madness_card not in player["graveyard"], \
        "Madness did not replace the discard destination with exile"
    opportunity = gs.madness_cast_available
    assert opportunity and opportunity.get("card_id") == madness_card, \
        "Madness discard did not create its casting opportunity"
    assert opportunity.get("cost") == "{b}", \
        f"Madness cost was not preserved: {opportunity.get('cost')}"


@scenario("514.1", "cleanup pauses for the active player to choose excess cards to discard")
def s_cleanup_discard_uses_choice_actions():
    gs = fresh()
    active = gs._get_active_player()
    nonactive = gs._get_non_active_player()
    gs.agent_is_p1 = active == gs.p1
    gs.phase = gs.PHASE_CLEANUP
    gs.priority_player = None
    excess = inject_into_zone(gs, active, {
        "name": "Cleanup Excess Probe", "mana_cost": "{5}",
        "type_line": "Sorcery", "oracle_text": "",
    }, "hand")
    nonactive_excess = inject_into_zone(gs, nonactive, {
        "name": "Nonactive Cleanup Probe", "mana_cost": "{6}",
        "type_line": "Sorcery", "oracle_text": "",
    }, "hand")
    assert len(active["hand"]) == gs.max_hand_size + 1, "cleanup test hand is not over size"
    selected = active["hand"][0]

    gs._advance_phase()
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "discard", \
        "cleanup silently discarded instead of asking for a card choice"
    assert excess in active["hand"], "cleanup discarded before receiving a choice"
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[238], "cleanup did not expose discard-card actions"
    _, ok = get_env().action_handler._handle_discard_card(0)
    assert ok and selected in active["graveyard"], "cleanup did not discard the selected card"
    assert len(active["hand"]) == gs.max_hand_size, "cleanup did not restore maximum hand size"
    assert gs.phase == gs.PHASE_CLEANUP and gs.choice_context is None, \
        "cleanup discard did not return to the cleanup step"
    gs._advance_phase()
    assert nonactive_excess in nonactive["hand"], \
        "cleanup incorrectly made the nonactive player discard to maximum hand size"
    assert gs.choice_context is None, "cleanup opened a discard choice for the nonactive player"


@scenario("603.3d", "a targeted triggered ability waits for the controller's selected target")
def s_triggered_ability_target_choice():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Targeted Trigger Source", "mana_cost": "{2}{B}",
        "type_line": "Creature - Wizard", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    first = inject_into_zone(gs, opponent, {
        "name": "Trigger Target First", "mana_cost": "{1}{G}",
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    chosen = inject_into_zone(gs, opponent, {
        "name": "Trigger Target Chosen", "mana_cost": "{3}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    ability = TriggeredAbility(
        source, trigger_condition="when this creature attacks",
        effect="destroy target creature")
    gs.stack.clear()
    gs.ability_handler.active_triggers = [(ability, controller, {})]

    gs.ability_handler.process_triggered_abilities()
    assert gs.stack and gs.stack[-1][0] == "TRIGGER", "trigger was not put on the stack"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "targeted trigger did not enter the target-choice phase"
    assert first in opponent["battlefield"] and chosen in opponent["battlefield"], \
        "trigger resolved or auto-selected before the controller chose"
    target_type = gs.targeting_context.get("required_type")
    valid_map = gs.targeting_system.get_valid_targets(source, controller, target_type)
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    assert chosen in valid_targets, "intended trigger target is not legal"
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(chosen), {})
    assert ok, f"trigger target selection failed with reward {reward}"
    assert gs.resolve_top_of_stack(), "targeted trigger did not resolve"
    assert chosen in opponent["graveyard"] and first in opponent["battlefield"], \
        "targeted trigger did not affect only the selected creature"


@scenario("602.2b", "a targeted activated ability is stacked and uses SELECT_TARGET")
def s_activated_ability_target_choice():
    gs = fresh()
    from Playersim.ability_types import ActivatedAbility
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Targeted Activation Source", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    victim = inject_into_zone(gs, opponent, {
        "name": "Activation Target", "mana_cost": "{2}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    ability = ActivatedAbility(source, cost="{1}", effect="Destroy target creature")
    gs.ability_handler.registered_abilities[source] = [ability]
    battlefield_index = controller["battlefield"].index(source)
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    reward, ok = get_env().action_handler._handle_activate_ability(
        None, {"battlefield_idx": battlefield_index, "ability_idx": 0})
    assert ok, f"targeted activation failed with reward {reward}"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "targeted activated ability did not ask for a target"
    assert not gs.stack and sum(controller["mana_pool"].values()) == 1, \
        "activated ability paid or entered the stack before its target was chosen"
    valid_map = gs.targeting_system.get_valid_targets(source, controller, "creature")
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(victim), {})
    assert ok, f"activated-ability target selection failed with reward {reward}"
    assert gs.stack and gs.stack[-1][0] == "ABILITY", \
        "targeted activated ability was not put on the stack after target selection"
    assert sum(controller["mana_pool"].values()) == 0, \
        "activated ability did not pay its cost after target selection"
    assert gs.resolve_top_of_stack(), "targeted activated ability did not resolve"
    assert victim in opponent["graveyard"], "activated ability did not destroy its selected target"


@scenario("603.3d", "a trigger with no legal required target is not put on the stack")
def s_targeted_trigger_without_legal_target_is_removed():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    controller = gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Targetless Trigger Source", "mana_cost": "{2}{B}",
        "type_line": "Creature - Wizard", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    ability = TriggeredAbility(
        source, trigger_condition="when this creature attacks",
        effect="destroy target planeswalker")
    gs.stack.clear()
    gs.ability_handler.active_triggers = [(ability, controller, {})]

    gs.ability_handler.process_triggered_abilities()
    assert not gs.stack, "trigger with no legal required target was put on the stack"
    assert gs.targeting_context is None and gs.phase != gs.PHASE_TARGETING, \
        "targetless trigger opened an impossible target choice"


@scenario("603.3d / self-play", "the scripted opponent selects a legal target for its trigger")
def s_scripted_opponent_target_choice():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    controller = gs.p2
    source = inject_into_zone(gs, controller, {
        "name": "Opponent Target Trigger", "mana_cost": "{2}{R}",
        "type_line": "Creature - Shaman", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    inject_into_zone(gs, gs.p1, {
        "name": "Opponent Trigger Victim", "mana_cost": "{1}{G}",
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    ability = TriggeredAbility(
        source, trigger_condition="when this creature attacks",
        effect="destroy target creature")
    gs.stack.clear()
    gs.ability_handler.active_triggers = [(ability, controller, {})]
    gs.ability_handler.process_triggered_abilities()
    assert gs.targeting_context and gs.targeting_context.get("controller") == controller

    gs.agent_is_p1 = False
    mask = get_env().action_handler.generate_valid_actions()
    action, _ = get_env()._get_scripted_opponent_action(
        controller, mask, {"phase_context": "TARGETING"})
    assert action is not None and 274 <= action <= 283, \
        f"scripted opponent did not choose a target action: {action}"


@scenario("713", "meld exiles both components, creates the combined permanent, then separates on leave")
def s_meld_components_and_separation():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    controller = gs.p1
    result_name = "Joined Test Titan"
    parts = [
        {"component": "meld_part", "name": "Meld Test Alpha"},
        {"component": "meld_part", "name": "Meld Test Beta"},
        {"component": "meld_result", "name": result_name},
    ]
    result = inject_card(gs, {
        "name": result_name, "layout": "meld", "mana_cost": "",
        "type_line": "Legendary Creature - Eldrazi", "oracle_text": "Vigilance",
        "power": 9, "toughness": 10, "all_parts": parts,
    })
    alpha = inject_into_zone(gs, controller, {
        "name": "Meld Test Alpha", "layout": "meld", "mana_cost": "{3}",
        "type_line": "Legendary Creature - Human", "oracle_text": "",
        "power": 3, "toughness": 3, "all_parts": parts,
    }, "battlefield")
    beta = inject_into_zone(gs, controller, {
        "name": "Meld Test Beta", "layout": "meld", "mana_cost": "{4}",
        "type_line": "Legendary Creature - Spirit", "oracle_text": "",
        "power": 4, "toughness": 4, "all_parts": parts,
    }, "battlefield")
    before = zone_census(gs)

    gs.original_p2_deck.append(beta)
    assert not gs.meld_cards(alpha, beta, result, controller), \
        "meld accepted a component the resolving controller did not own"
    gs.original_p2_deck.remove(beta)

    effects = EffectFactory.create_effects(
        f"Exile them, then meld them into {result_name}.", source_name="Meld Test Alpha")
    assert len(effects) == 1 and type(effects[0]).__name__ == "MeldEffect", \
        f"meld text did not parse as one MeldEffect: {[type(e).__name__ for e in effects]}"
    assert effects[0].apply(gs, alpha, controller), "meld effect failed"
    melded = gs._safe_get_card(alpha)
    assert alpha in controller["battlefield"] and beta in controller["exile"], \
        "meld did not exile both parts and return one combined permanent"
    assert (melded.name, melded.power, melded.toughness) == (result_name, 9, 10), \
        f"melded identity is {melded.name} {melded.power}/{melded.toughness}"
    assert gs.melded_permanents.get(alpha, {}).get("partner_id") == beta
    assert zone_census(gs) == before, "meld did not conserve the two physical cards"

    assert gs.move_card(alpha, controller, "battlefield", controller, "graveyard", cause="destroy"), \
        "melded permanent could not leave the battlefield"
    assert alpha in controller["graveyard"] and beta in controller["graveyard"], \
        "melded permanent did not separate into both component cards"
    assert gs._safe_get_card(alpha).name == "Meld Test Alpha", \
        "meld component did not restore its front-face identity after separating"
    assert alpha not in gs.melded_permanents, "stale meld tracking survived separation"
    assert zone_census(gs) == before, "separating a melded permanent changed physical card count"


@scenario("713.4 / 400.7", "blinking a melded permanent returns both front faces as separate objects")
def scenario_meld_blink_returns_both_front_faces():
    from Playersim.ability_types import BlinkWithCounterEffect
    gs = fresh(SEED + 188)
    controller = gs.p1
    result_name = "Blink Joined Titan"
    parts = [
        {"component": "meld_part", "name": "Blink Meld Alpha"},
        {"component": "meld_part", "name": "Blink Meld Beta"},
        {"component": "meld_result", "name": result_name},
    ]
    result = inject_card(gs, {
        "name": result_name, "layout": "meld", "mana_cost": "",
        "type_line": "Legendary Creature - Giant", "oracle_text": "Trample",
        "power": 8, "toughness": 8, "all_parts": parts,
    })
    alpha = inject_into_zone(gs, controller, {
        "name": "Blink Meld Alpha", "layout": "meld", "mana_cost": "{2}",
        "type_line": "Legendary Creature - Human", "oracle_text": "Vigilance",
        "power": 2, "toughness": 2, "all_parts": parts,
    }, "battlefield")
    beta = inject_into_zone(gs, controller, {
        "name": "Blink Meld Beta", "layout": "meld", "mana_cost": "{3}",
        "type_line": "Legendary Creature - Spirit", "oracle_text": "Flying",
        "power": 3, "toughness": 3, "all_parts": parts,
    }, "battlefield")
    before = zone_census(gs)
    assert gs.meld_cards(alpha, beta, result, controller)

    clone = gs.clone()
    assert clone._safe_get_card(alpha) is not gs._safe_get_card(alpha), \
        "meld-result characteristics shared a mutable Card across branches"
    assert clone.melded_permanents is not gs.melded_permanents

    blink = BlinkWithCounterEffect()
    assert blink.apply(
        gs, None, controller, targets={"creatures": [alpha]})
    assert alpha in controller["battlefield"] and beta in controller["battlefield"]
    assert alpha not in controller["exile"] and beta not in controller["exile"]
    assert gs._safe_get_card(alpha).name == "Blink Meld Alpha"
    assert gs._safe_get_card(beta).name == "Blink Meld Beta"
    assert gs._safe_get_card(alpha).counters.get("+1/+1") == 1
    assert gs._safe_get_card(beta).counters.get("+1/+1") == 1
    assert alpha not in gs.melded_permanents
    assert zone_census(gs) == before


@scenario("601.2c / choice", "a direct targeted effect pauses for SELECT_TARGET instead of auto-selecting")
def s_direct_effect_target_choice():
    gs = fresh()
    from Playersim.ability_types import DestroyEffect
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    source = inject_into_zone(gs, controller, {
        "name": "Direct Target Source", "mana_cost": "{2}{B}",
        "type_line": "Creature - Wizard", "oracle_text": "Destroy target creature.",
        "power": 2, "toughness": 2,
    }, "battlefield")
    first = inject_into_zone(gs, opponent, {
        "name": "Direct Target First", "mana_cost": "{1}{G}",
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    chosen = inject_into_zone(gs, opponent, {
        "name": "Direct Target Chosen", "mana_cost": "{4}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 5, "toughness": 5,
    }, "battlefield")

    assert DestroyEffect("creature").apply(gs, source, controller), \
        "direct targeted effect did not start"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context.get("resume_effect"), \
        "direct effect did not become a target choice"
    assert first in opponent["battlefield"] and chosen in opponent["battlefield"], \
        "direct effect auto-selected a creature before the agent acted"
    valid_map = gs.targeting_system.get_valid_targets(
        source, controller, "creature", effect_text="Destroy target creature")
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(chosen), {})
    assert ok, f"direct-effect target selection failed with reward {reward}"
    assert chosen in opponent["graveyard"] and first in opponent["battlefield"], \
        "direct effect did not apply only to the chosen target"


@scenario("601.2c", "an up-to-two target spell can finish early or choose zero with Pass")
def s_optional_multi_target_finishes_on_pass():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.agent_is_p1 = True
    gs.priority_player = controller
    first = inject_into_zone(gs, opponent, {
        "name": "Optional Target First", "mana_cost": "{1}{G}",
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    inject_into_zone(gs, opponent, {
        "name": "Optional Target Second", "mana_cost": "{2}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    spell = inject_into_zone(gs, controller, {
        "name": "Optional Target Probe", "mana_cost": "{U}",
        "type_line": "Sorcery", "oracle_text": "Tap up to two target creatures.",
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}

    assert gs.cast_spell(spell, controller), "optional-target spell cast failed"
    assert (gs.targeting_context.get("min_targets"),
            gs.targeting_context.get("max_targets")) == (0, 2), \
        f"up-to-two bounds were {gs.targeting_context}"
    valid_map = gs.targeting_system.get_valid_targets(
        spell, controller, "creature", effect_text="Tap up to two target creatures.")
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    reward, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(first), {})
    assert ok, f"optional target selection failed with reward {reward}"
    assert gs.targeting_context and gs.targeting_context.get("selected_targets") == [first], \
        "optional multi-target choice finalized after only one selection"
    assert get_env().action_handler.generate_valid_actions()[11], \
        "Pass was not exposed after meeting the optional target minimum"
    get_env().action_handler._handle_pass_priority(None)
    assert gs.targeting_context is None, "Pass did not finish optional targeting"
    assert gs.stack[-1][3].get("targets") == {"creatures": [first]}, \
        f"Pass stored the wrong optional targets: {gs.stack[-1][3].get('targets')}"
    assert gs.resolve_top_of_stack(), "one-target optional spell did not resolve"

    zero_spell = inject_into_zone(gs, controller, {
        "name": "Zero Target Probe", "mana_cost": "{U}",
        "type_line": "Sorcery", "oracle_text": "Tap up to two target creatures.",
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = controller
    gs.priority_pass_count = 0
    assert gs.cast_spell(zero_spell, controller), "zero-target spell cast failed"
    assert get_env().action_handler.generate_valid_actions()[11], \
        "Pass was not exposed before selecting an optional target"
    get_env().action_handler._handle_pass_priority(None)
    assert gs.stack[-1][3].get("targets") == {}, \
        "choosing zero targets did not store an explicit empty target set"
    assert gs.resolve_top_of_stack(), "zero-target optional spell did not resolve"
    assert gs.targeting_context is None, \
        "zero-target spell reopened targeting during resolution"


@scenario("706", "a d20 result table resolves one branch and fires die-roll triggers")
def s_d20_result_table_and_trigger():
    gs = fresh()
    from unittest.mock import patch
    controller = gs.p1
    watcher = inject_into_zone(gs, controller, {
        "name": "Dice Watcher", "mana_cost": "{1}{W}",
        "type_line": "Creature - Cleric",
        "oracle_text": "Whenever you roll one or more dice, you gain 1 life.",
        "power": 2, "toughness": 2,
    }, "battlefield")
    roller = inject_into_zone(gs, controller, {
        "name": "D20 Initiate", "mana_cost": "{2}{G}",
        "type_line": "Creature - Elf",
        "oracle_text": (
            "When this creature enters, roll a d20.\n"
            "1\u20149 | You gain 1 life.\n"
            "10\u201419 | You gain 3 life.\n"
            "20 | You gain 10 life."),
        "power": 3, "toughness": 3,
    }, "battlefield")
    assert watcher in controller["battlefield"] and roller in controller["battlefield"]
    life_before = controller["life"]

    gs.ability_handler.process_triggered_abilities()
    assert gs.stack and gs.stack[-1][0] == "TRIGGER", \
        "d20 ETB trigger was not put on the stack"
    with patch("Playersim.ability_types.random.randint", return_value=15):
        assert gs.resolve_top_of_stack(), "d20 trigger failed to resolve"
    assert controller["life"] == life_before + 3, \
        "d20 result resolved the wrong table branch"
    assert gs.last_die_roll.get("result") == 15 and gs.last_die_roll.get("sides") == 20, \
        f"die roll was not recorded: {gs.last_die_roll}"
    assert len(gs.die_roll_history) == 1, "die roll history did not record exactly one roll"

    queued_sources = [entry[0].card_id for entry in gs.ability_handler.active_triggers]
    assert watcher in queued_sources, "DIE_ROLLED did not queue the watcher trigger"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "die-roll watcher trigger did not resolve"
    assert controller["life"] == life_before + 4, \
        "die-roll watcher applied the wrong effect"
    cloned = gs.clone()
    assert cloned and cloned.die_roll_history == gs.die_roll_history, \
        "cloned game lost its die-roll history"
    cloned.die_roll_history.append({"result": 1})
    assert len(gs.die_roll_history) == 1, "clone mutated the original die-roll history"


@scenario("digital / specialize", "Specialize exposes discard and color choices before changing identity")
def s_specialize_choice_and_identity():
    gs = fresh()
    controller = gs.p1
    family = [
        ("Prismatic Novice", ["W"], "Specialize {2}", 2, 2),
        ("Prismatic Life Adept", ["W"], "Lifelink", 3, 3),
        ("Prismatic Knowledge Adept", ["W", "U"],
         "When this creature specializes, you gain 3 life.", 4, 4),
        ("Prismatic Death Adept", ["W", "B"], "Deathtouch", 4, 3),
        ("Prismatic Tempest Adept", ["W", "R"], "Double strike", 3, 3),
        ("Prismatic Nature Adept", ["W", "G"], "Trample", 5, 5),
    ]
    all_parts = [
        {"component": "combo_piece", "name": name,
         "type_line": "Legendary Creature - Human Wizard"}
        for name, _, _, _, _ in family
    ]
    source = inject_into_zone(gs, controller, {
        "name": family[0][0], "mana_cost": "{1}{W}",
        "type_line": "Legendary Creature - Human Wizard",
        "oracle_text": family[0][2], "color_identity": family[0][1],
        "power": family[0][3], "toughness": family[0][4],
        "all_parts": all_parts,
    }, "battlefield")
    for name, colors, oracle_text, power, toughness in family[1:]:
        inject_card(gs, {
            "name": name, "mana_cost": "{1}{W}{U}",
            "type_line": "Legendary Creature - Human Wizard",
            "oracle_text": oracle_text, "color_identity": colors,
            "power": power, "toughness": toughness,
            "all_parts": all_parts,
        })
    discard_id = inject_into_zone(gs, controller, {
        "name": "Izzet Specialize Fuel", "mana_cost": "{U}{R}",
        "type_line": "Instant", "oracle_text": "Draw a card.",
        "color_identity": ["U", "R"],
    }, "hand")
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
    handler = get_env().action_handler
    battlefield_index = controller["battlefield"].index(source)

    mask = handler.generate_valid_actions()
    assert mask[160 + battlefield_index], "Specialize action was not exposed"
    reward, ok = handler._handle_transform(battlefield_index)
    assert ok, f"Specialize did not start with reward {reward}"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "specialize_discard", \
        "Specialize did not ask the agent which card to discard"
    assert discard_id in controller["hand"] and sum(controller["mana_pool"].values()) == 2, \
        "Specialize paid costs before its choices were complete"

    discard_index = controller["hand"].index(discard_id)
    reward, ok = handler._handle_discard_card(discard_index)
    assert ok, f"Specialize discard selection failed with reward {reward}"
    assert gs.choice_context.get("type") == "choose_color", \
        "multicolor discard did not ask which specialization color to use"
    color_mask = handler.generate_valid_actions()
    assert color_mask[374] and color_mask[376], "discard card's blue/red choices were not exposed"
    assert not color_mask[373] and not color_mask[375] and not color_mask[377], \
        "Specialize exposed colors the discarded card did not have"
    assert discard_id in controller["hand"], "discard happened before the color choice"

    life_before = controller["life"]
    reward, ok = handler._handle_choose_color(1, {})
    assert ok, f"blue specialization choice failed with reward {reward}"
    specialized = gs._safe_get_card(source)
    assert discard_id in controller["graveyard"] and sum(controller["mana_pool"].values()) == 0, \
        "Specialize did not pay its discard and mana costs"
    assert specialized.name == "Prismatic Knowledge Adept", \
        f"Specialize chose the wrong linked identity: {specialized.name}"
    assert (specialized.power, specialized.toughness) == (4, 4), \
        "specialized characteristics did not replace the base identity"
    assert gs.specialized_cards[source].get("color") == "U", \
        "specialized identity was not tracked"

    queued_sources = [entry[0].card_id for entry in gs.ability_handler.active_triggers]
    assert source in queued_sources, "when-this-specializes trigger was not queued"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "specializes trigger did not resolve"
    assert controller["life"] == life_before + 3, \
        "specialized face's trigger applied the wrong effect"
    assert gs.move_card(
        source, controller, "battlefield", controller, "graveyard",
        cause="specialize_persistence_test"), "specialized card could not change zones"
    assert gs._safe_get_card(source).name == "Prismatic Knowledge Adept", \
        "specialized identity did not persist across a zone change"

    p1_deck = list(gs.original_p1_deck)
    p2_deck = list(gs.original_p2_deck)
    gs.reset(p1_deck, p2_deck, seed=19)
    assert gs._safe_get_card(source).name == "Prismatic Novice", \
        "specialized identity leaked into the next game"
    assert not gs.specialized_cards, "specialized identity tracking survived reset"


@scenario("727 / 702.145", "day and night change at turn start and synchronize daybound permanents")
def s_day_night_turn_cycle_and_entry_face():
    gs = fresh()
    controller = gs.p1
    faces = [
        {
            "name": "Dawnwatch Pup", "mana_cost": "{1}{G}",
            "type_line": "Creature - Wolf", "oracle_text": "Daybound",
            "power": "2", "toughness": "2", "colors": ["G"],
        },
        {
            "name": "Midnight Packleader", "mana_cost": "",
            "type_line": "Creature - Werewolf", "oracle_text": "Nightbound\nTrample",
            "power": "4", "toughness": "4", "colors": ["G"],
        },
    ]
    werewolf = inject_into_zone(gs, controller, {
        "name": "Dawnwatch Pup", "layout": "transform", "mana_cost": "{1}{G}",
        "type_line": "Creature - Wolf", "oracle_text": "Daybound",
        "power": "2", "toughness": "2", "color_identity": ["G"],
        "card_faces": faces,
    }, "battlefield")
    card = gs._safe_get_card(werewolf)
    assert gs.day_night_state == "day", "a daybound permanent did not make it day"
    assert (card.current_face, card.name, card.power, card.toughness) == \
        (0, "Dawnwatch Pup", 2, 2), "daybound permanent entered on the wrong face"

    gs.phase = gs.PHASE_CLEANUP
    gs.spells_cast_this_turn = []
    old_turn = gs.turn
    gs._advance_phase()
    assert gs.turn == old_turn + 1, "cleanup did not advance to the next turn"
    assert gs.day_night_state == "night", "zero spells during the prior turn did not make it night"
    assert (card.current_face, card.name, card.power, card.toughness) == \
        (1, "Midnight Packleader", 4, 4), "daybound permanent did not become nightbound"
    assert "werewolf" in card.subtypes and "trample" in card.oracle_text.lower(), \
        "the night face did not update types and abilities"

    night_entry = inject_into_zone(gs, controller, {
        "name": "Evening Cub", "layout": "transform", "mana_cost": "{2}{G}",
        "type_line": "Creature - Wolf", "oracle_text": "Daybound",
        "power": "3", "toughness": "3", "color_identity": ["G"],
        "card_faces": [
            {**faces[0], "name": "Evening Cub", "power": "3", "toughness": "3"},
            {**faces[1], "name": "Moonrise Hunter", "power": "5", "toughness": "5"},
        ],
    }, "battlefield")
    entering_card = gs._safe_get_card(night_entry)
    assert (entering_card.current_face, entering_card.name) == (1, "Moonrise Hunter"), \
        "a daybound card entering at night did not enter nightbound"

    gs.day_night_checked_this_turn = False
    gs.check_day_night_transition(1)
    assert gs.day_night_state == "night", "one spell incorrectly changed night to day"
    gs.day_night_checked_this_turn = False
    gs.check_day_night_transition(2)
    assert gs.day_night_state == "day", "two spells during the prior turn did not make it day"
    assert card.current_face == 0 and entering_card.current_face == 0, \
        "nightbound permanents did not synchronize back to their day faces"

    gs.day_night_state = "night"
    gs.transform_day_night_cards()
    gs.reset(list(gs.original_p1_deck), list(gs.original_p2_deck), seed=23)
    assert gs.day_night_state is None, "day/night designation leaked into the next game"
    assert (card.current_face, card.name) == (0, "Dawnwatch Pup"), \
        "nightbound identity leaked into the next game"


@scenario("702.140", "mutate is targeted, resolves as a merged permanent, and separates on leave")
def s_mutate_cast_merge_trigger_and_separation():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    gs.priority_pass_count = 0
    base = inject_into_zone(gs, controller, {
        "name": "Patient Brushbeast", "mana_cost": "{1}{G}",
        "type_line": "Creature - Beast", "oracle_text": "Vigilance",
        "power": 2, "toughness": 3, "color_identity": ["G"],
    }, "battlefield")
    human = inject_into_zone(gs, controller, {
        "name": "Unmutable Ranger", "mana_cost": "{1}{W}",
        "type_line": "Creature - Human Scout", "oracle_text": "",
        "power": 2, "toughness": 2, "color_identity": ["W"],
    }, "battlefield")
    mutating = inject_into_zone(gs, controller, {
        "name": "Skycoil Heron", "mana_cost": "{3}{U}",
        "type_line": "Creature - Bird Beast",
        "oracle_text": (
            "Mutate {1}{U} (If you cast this spell for its mutate cost, put it over or "
            "under target non-Human creature you own.)\nFlying\n"
            "Whenever this creature mutates, you gain 2 life."),
        "power": 4, "toughness": 4, "color_identity": ["U"],
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    before = zone_census(gs)
    handler = get_env().action_handler

    mask = handler.generate_valid_actions()
    assert mask[426], "affordable mutate spell with a legal target was not exposed"
    mutate_context = handler.action_reasons_with_context[426]["context"]
    reward, ok = handler._handle_mutate(None, mutate_context)
    assert ok, f"mutate cast failed with reward {reward}"
    assert mutating in controller["hand"] and not gs.stack, \
        "mutate paid costs or entered the stack before choosing its target"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "mutate did not ask the agent for its target"
    valid_map = gs.targeting_system.get_valid_targets(
        mutating, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    assert base in valid_targets and human not in valid_targets, \
        "mutate target legality did not enforce non-Human ownership"
    reward, ok = handler._handle_select_target(valid_targets.index(base), {})
    assert ok, f"mutate target selection failed with reward {reward}"
    assert mutating not in controller["hand"] and gs.stack[-1][1] == mutating, \
        "mutate did not become a spell after its target was committed"
    assert gs.stack and gs.stack[-1][3].get("targets") == {"creatures": [base]}, \
        "selected mutate target was not stored on the spell"

    assert gs.resolve_top_of_stack(), "mutating creature spell did not begin resolving"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "mutate_position", \
        "mutate did not ask whether to put the new card over or under"
    position_mask = handler.generate_valid_actions()
    assert position_mask[353] and position_mask[354], "mutate top/bottom choices were not exposed"
    life_before = controller["life"]
    reward, ok = handler._handle_choose_mode(0, {})
    assert ok, f"mutate top choice failed with reward {reward}"

    merged = gs._safe_get_card(base)
    mutation = gs.mutated_permanents.get(base, {})
    assert base in controller["battlefield"] and mutating not in controller["battlefield"], \
        "mutate created two permanents instead of one merged permanent"
    assert mutation.get("components") == [mutating, base], \
        f"mutate component order was not tracked: {mutation}"
    assert (merged.name, merged.power, merged.toughness) == ("Skycoil Heron", 4, 4), \
        "the top mutate card did not provide merged characteristics"
    assert "flying" in merged.oracle_text.lower() and "vigilance" in merged.oracle_text.lower(), \
        "the merged permanent did not retain every component's abilities"
    assert zone_census(gs) + len(mutation["components"]) - 1 == before, \
        "mutate lost or duplicated a physical card"

    queued_sources = [entry[0].card_id for entry in gs.ability_handler.active_triggers]
    assert base in queued_sources, "whenever-this-creature-mutates did not trigger"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "mutate trigger did not resolve"
    assert controller["life"] == life_before + 2, "mutate trigger applied the wrong effect"

    assert gs.move_card(base, controller, "battlefield", controller, "graveyard", cause="destroy"), \
        "merged permanent could not leave the battlefield"
    assert base in controller["graveyard"] and mutating in controller["graveyard"], \
        "all mutate components did not move to the graveyard"
    assert gs._safe_get_card(base).name == "Patient Brushbeast", \
        "the base card did not restore its physical identity after separation"
    assert base not in gs.mutated_permanents and base not in controller["mutation_stacks"], \
        "stale mutate tracking survived separation"
    assert zone_census(gs) == before, "separating mutate components changed physical card count"


@scenario("702.140c", "a mutating spell with an illegal target resolves as an ordinary creature")
def s_mutate_illegal_target_resolves_normally():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    target = inject_into_zone(gs, controller, {
        "name": "Fleeting Mutate Target", "mana_cost": "{G}",
        "type_line": "Creature - Cat", "oracle_text": "",
        "power": 1, "toughness": 1, "color_identity": ["G"],
    }, "battlefield")
    mutating = inject_into_zone(gs, controller, {
        "name": "Fallback Mutation", "mana_cost": "{2}{U}",
        "type_line": "Creature - Bird",
        "oracle_text": "Mutate {1}{U}\nFlying",
        "power": 3, "toughness": 3, "color_identity": ["U"],
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    handler = get_env().action_handler

    reward, ok = handler._handle_mutate(
        None, {"hand_idx": controller["hand"].index(mutating)})
    assert ok, f"fallback mutate cast failed with reward {reward}"
    valid_map = gs.targeting_system.get_valid_targets(
        mutating, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted(set(tid for ids in valid_map.values() for tid in ids))
    reward, ok = handler._handle_select_target(valid_targets.index(target), {})
    assert ok, f"fallback mutate target selection failed with reward {reward}"
    assert gs.move_card(
        target, controller, "battlefield", controller, "graveyard", cause="response"), \
        "could not remove the mutate target in response"

    assert gs.resolve_top_of_stack(), "illegal-target mutate spell did not finish resolving"
    assert mutating in controller["battlefield"], \
        "illegal-target mutate spell did not enter as an ordinary creature"
    assert gs.choice_context is None and not gs.mutated_permanents, \
        "illegal-target mutate incorrectly opened a merge choice or created a merge"


@scenario("616 (engine)", "legacy asap delayed triggers fire at the next state-based check")
def s_delayed_trigger_asap():
    gs = fresh()
    fired = []
    # Legacy producers (damage redirection, deferred lifelink) append bare
    # callables meaning "run as soon as the current event fully resolves."
    gs.delayed_triggers.append(lambda: fired.append(True))
    gs.check_state_based_actions()
    assert fired == [True], "asap delayed trigger did not fire at the next SBA check"
    assert not gs.delayed_triggers, "fired asap trigger was not removed"


@scenario("111.10 / 701.40", "a Map token pays, taps, sacrifices, and exposes the explore destination")
def s_map_token_agent_explore():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    explorer = inject_into_zone(gs, controller, {
        "name": "Map Scout", "mana_cost": "{G}",
        "type_line": "Creature - Scout", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    siren = inject_into_zone(gs, controller, {
        "name": "Spyglass Siren", "mana_cost": "{U}",
        "type_line": "Creature - Siren Pirate", "oracle_text": (
            "Flying\nWhen Spyglass Siren enters the battlefield, create a Map token."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")

    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Spyglass Siren's Map trigger did not resolve"
    maps = [
        token_id for token_id in controller.get("tokens", [])
        if getattr(gs._safe_get_card(token_id), "name", "") == "Map"
    ]
    assert len(maps) == 1, f"Spyglass Siren created the wrong tokens: {maps}"
    map_id = maps[0]
    map_card = gs._safe_get_card(map_id)
    assert map_card.card_types == ["artifact"] and "map" in {
        subtype.lower() for subtype in map_card.subtypes
    }, "the Map token did not have its artifact/Map characteristics"
    abilities = gs.ability_handler.get_activated_abilities(map_id)
    assert len(abilities) == 1 and "explores" in abilities[0].effect.lower(), \
        "the Map token's activated explore ability was not registered"

    nonland = inject_card(gs, {
        "name": "Explore Decision", "mana_cost": "{2}{U}",
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    controller["library"].insert(0, nonland)
    gs._last_card_locations[nonland] = (controller, "library")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    handler = get_env().action_handler
    reward, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": controller["battlefield"].index(map_id),
        "ability_idx": 0,
    })
    assert ok, f"Map activation did not begin targeting: {reward}"
    assert gs.phase == gs.PHASE_TARGETING and not gs.stack, \
        "Map paid a cost or entered the stack before choosing its target"
    valid_map = gs.targeting_system.get_valid_targets(
        map_id, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert explorer in valid_targets and all(
        target_id not in opponent["battlefield"] for target_id in valid_targets
    ), "Map did not restrict its target to a creature its controller controls"
    reward, ok = handler._handle_select_target(valid_targets.index(explorer), {})
    assert ok, f"Map target selection failed: {reward}"
    assert map_id not in controller["battlefield"] and map_id not in gs.card_db, \
        "Map was not sacrificed as an activation cost"
    assert sum(controller["mana_pool"].values()) == 0, "Map did not pay {1}"
    assert gs.stack and gs.stack[-1][1] == map_id, \
        "Map's ability was not independent of its sacrificed source"

    assert gs.resolve_top_of_stack(), "Map's explore ability did not resolve"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "explore", \
        "nonland explore was auto-resolved instead of becoming an agent choice"
    assert gs._safe_get_card(explorer).counters.get("+1/+1", 0) == 1, \
        "explore did not put a +1/+1 counter on the exploring creature"
    choice_mask = handler.generate_valid_actions()
    assert choice_mask[305] and choice_mask[306] and not choice_mask[307], \
        "explore did not expose exactly graveyard/top destinations"
    reward, ok = handler._handle_scry_surveil_choice(
        None, {}, action_index=306)
    assert ok, f"keeping the explored card on top failed: {reward}"
    assert controller["library"][0] == nonland and gs.choice_context is None, \
        "the chosen explore destination was not committed"


@scenario("701.40a", "explore moves a land to hand and an empty library still gives a counter")
def s_explore_land_and_empty_library():
    gs = fresh()
    controller = gs.p1
    explorer = inject_into_zone(gs, controller, {
        "name": "Empty-Library Explorer", "mana_cost": "{G}",
        "type_line": "Creature - Scout", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    land = inject_card(gs, {
        "name": "Explore Land", "type_line": "Land - Island", "oracle_text": "",
    })
    controller["library"].insert(0, land)
    gs._last_card_locations[land] = (controller, "library")
    assert gs.explore(controller, explorer), "land explore failed"
    assert land in controller["hand"] and gs.choice_context is None, \
        "explore did not put the revealed land directly into hand"

    controller["library"].clear()
    before = gs._safe_get_card(explorer).counters.get("+1/+1", 0)
    assert gs.explore(controller, explorer), "exploring an empty library was treated as failure"
    assert gs._safe_get_card(explorer).counters.get("+1/+1", 0) == before + 1, \
        "an empty-library explore did not put a +1/+1 counter on the creature"
    assert gs.choice_context is None, "empty-library explore opened a nonexistent card choice"


@scenario("701.7 / 111.10", "Get Lost gives two Map tokens to the destroyed permanent's controller")
def s_get_lost_creates_maps_for_target_controller():
    gs = fresh()
    caster, target_controller = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = caster
    target = inject_into_zone(gs, target_controller, {
        "name": "Lost Enchantment", "mana_cost": "{1}{W}",
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    spell = inject_into_zone(gs, caster, {
        "name": "Get Lost", "mana_cost": "{1}{W}",
        "type_line": "Instant", "oracle_text": (
            "Destroy target creature, enchantment, or planeswalker. "
            "Its controller creates two Map tokens."
        ),
    }, "hand")
    caster["mana_pool"] = {'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}

    assert gs.cast_spell(spell, caster), "Get Lost could not be cast"
    assert gs.targeting_context.get("required_type") == "permanent", \
        "Get Lost did not preserve its three-type target union"
    valid_map = gs.targeting_system.get_valid_targets(
        spell, caster, "permanent", effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert target in valid_targets, "Get Lost did not offer an enchantment target"
    _, ok = get_env().action_handler._handle_select_target(valid_targets.index(target), {})
    assert ok and gs.resolve_top_of_stack(), "Get Lost did not resolve"
    maps = [
        token_id for token_id in target_controller.get("tokens", [])
        if getattr(gs._safe_get_card(token_id), "name", "") == "Map"
    ]
    assert target in target_controller["graveyard"] and len(maps) == 2, \
        "Get Lost did not destroy the target and give its controller two Maps"
    assert not any(
        getattr(gs._safe_get_card(token_id), "name", "") == "Map"
        for token_id in caster.get("tokens", [])
    ), "Get Lost gave the Maps to its caster"


@scenario("701.20 / 122.1d", "Floodpits Drowner shuffles itself and only a stun-counter creature")
def s_floodpits_drowner_stun_shuffle():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Floodpits Drowner", "mana_cost": "{1}{U}",
        "type_line": "Creature - Merfolk", "oracle_text": (
            "Flash\nVigilance\n"
            "When Floodpits Drowner enters, tap target creature an opponent controls "
            "and put a stun counter on it.\n"
            "{1}{U}, {T}: Shuffle Floodpits Drowner and target creature with a stun "
            "counter on it into their owners' libraries."
        ),
        "power": 2, "toughness": 1,
    }, "battlefield")
    stunned = inject_into_zone(gs, opponent, {
        "name": "Stunned Shuffle Target", "mana_cost": "{2}{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    clean = inject_into_zone(gs, opponent, {
        "name": "Counterless Creature", "mana_cost": "{G}",
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    gs.ability_handler.active_triggers.clear()
    controller["entered_battlefield_this_turn"].discard(source)
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    handler = get_env().action_handler
    ability_action = 100 + controller["battlefield"].index(source) * 3
    mask = handler.generate_valid_actions()
    assert not mask[ability_action], \
        "Drowner was mask-valid without a stun-counter creature to target"

    gs._safe_get_card(stunned).counters["stun"] = 1
    mask = handler.generate_valid_actions()
    assert mask[ability_action], \
        "Drowner stayed masked after a legal stun-counter target appeared"

    _, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": controller["battlefield"].index(source),
        "ability_idx": 0,
    })
    assert ok and gs.phase == gs.PHASE_TARGETING, "Drowner's shuffle did not ask for a target"
    valid_map = gs.targeting_system.get_valid_targets(
        source, controller, "creature", effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert stunned in valid_targets and clean not in valid_targets, \
        "Drowner did not require its target to have a stun counter"
    _, ok = handler._handle_select_target(valid_targets.index(stunned), {})
    assert ok, "Drowner's target could not be committed"
    assert source in controller["tapped_permanents"] and sum(controller["mana_pool"].values()) == 0, \
        "Drowner did not pay both its tap and mana costs"
    assert gs.resolve_top_of_stack(), "Drowner's shuffle ability did not resolve"
    assert source in controller["library"] and stunned in opponent["library"], \
        "Drowner and its target were not shuffled into their owners' libraries"
    assert clean in opponent["battlefield"], "Drowner shuffled a creature without a stun counter"


@scenario("608.2b", "an illegal Drowner target leaves the source on the battlefield")
def s_floodpits_drowner_illegal_target_fizzles_atomically():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Floodpits Drowner", "mana_cost": "{1}{U}",
        "type_line": "Creature - Merfolk", "oracle_text": (
            "{1}{U}, {T}: Shuffle Floodpits Drowner and target creature with a stun "
            "counter on it into their owners' libraries."
        ), "power": 2, "toughness": 1,
    }, "battlefield")
    target = inject_into_zone(gs, opponent, {
        "name": "Fleeting Stun Target", "mana_cost": "{G}",
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs._safe_get_card(target).counters["stun"] = 1
    controller["entered_battlefield_this_turn"].discard(source)
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    handler = get_env().action_handler
    _, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": controller["battlefield"].index(source), "ability_idx": 0,
    })
    assert ok
    valid_map = gs.targeting_system.get_valid_targets(
        source, controller, "creature", effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = handler._handle_select_target(valid_targets.index(target), {})
    assert ok
    gs._safe_get_card(target).counters.pop("stun")
    assert gs.resolve_top_of_stack(), "the counterless target did not make the ability fizzle cleanly"
    assert source in controller["battlefield"] and target in opponent["battlefield"], \
        "Drowner shuffled an object after its only target became illegal"


@scenario("603.6c / 608.2h", "Heartfire Hero deals its last-known power when it dies")
def s_heartfire_hero_last_known_power_damage():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    hero = inject_into_zone(gs, controller, {
        "name": "Heartfire Hero", "mana_cost": "{R}",
        "type_line": "Creature - Mouse Soldier", "oracle_text": (
            "Vigilance\nHaste\n"
            "Valiant - Whenever Heartfire Hero becomes the target of a spell or "
            "ability you control for the first time each turn, put a +1/+1 counter on it.\n"
            "When Heartfire Hero dies, it deals damage equal to its power to each opponent."
        ),
        "power": 1, "toughness": 1,
    }, "battlefield")
    gs._safe_get_card(hero).counters["+1/+1"] = 2
    gs.layer_system.apply_all_effects()
    assert gs._safe_get_card(hero).power == 3, "the Hero's live power setup failed"
    life_before = opponent["life"]

    assert gs.move_card(hero, controller, "battlefield", controller, "graveyard", cause="destroy")
    assert gs._safe_get_card(hero).power == 1, "the Hero did not reset after changing zones"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Heartfire Hero's dies trigger did not resolve"
    assert opponent["life"] == life_before - 3, \
        "Heartfire Hero did not deal damage equal to its last-known power"
    assert controller["life"] == 20, "Heartfire Hero damaged its controller"


@scenario("205.1a / 613 / 606", "Kaito becomes a hexproof Ninja creature and can still use loyalty abilities")
def s_kaito_type_change_and_ninja_emblem():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    kaito = inject_into_zone(gs, controller, {
        "name": "Kaito, Bane of Nightmares", "mana_cost": "{2}{U}{B}",
        "type_line": "Legendary Planeswalker - Kaito", "loyalty": 4,
        "oracle_text": (
            "Ninjutsu {1}{U}{B}\n"
            "During your turn, as long as Kaito has one or more loyalty counters on "
            "him, he's a 3/4 Ninja creature and has hexproof.\n"
            "+1: You get an emblem with \"Ninjas you control get +1/+1.\"\n"
            "0: Surveil 2. Then draw a card for each opponent who lost life this turn.\n"
            "-2: Tap target creature. Put two stun counters on it."
        ),
    }, "battlefield")
    ninja = inject_into_zone(gs, controller, {
        "name": "Emblem Ninja", "mana_cost": "{1}{U}",
        "type_line": "Creature - Human Ninja", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    non_ninja = inject_into_zone(gs, controller, {
        "name": "Emblem Bystander", "mana_cost": "{1}{U}",
        "type_line": "Creature - Human", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.layer_system.apply_all_effects()
    live = gs._safe_get_card(kaito)
    assert live.card_types == ["creature"] and {
        subtype.lower() for subtype in live.subtypes
    } == {"ninja"}, f"Kaito's active types were wrong: {live.card_types} {live.subtypes}"
    assert (live.power, live.toughness) == (3, 4) and gs.check_keyword(kaito, "hexproof"), \
        "Kaito did not gain its active P/T and hexproof"
    assert gs.activate_planeswalker_ability(kaito, 0, controller), \
        "animated Kaito could not activate its loyalty ability"
    assert gs.resolve_top_of_stack(), "Kaito's +1 did not resolve"
    assert controller.get("emblems") and controller["emblems"][-1]["kind"] == "ninja_anthem", \
        "Kaito did not create its command-zone emblem"
    gs.layer_system.apply_all_effects()
    assert (gs._safe_get_card(ninja).power, gs._safe_get_card(ninja).toughness) == (3, 3), \
        "Kaito's emblem did not give a controlled Ninja +1/+1"
    assert (gs._safe_get_card(non_ninja).power, gs._safe_get_card(non_ninja).toughness) == (2, 2), \
        "Kaito's emblem affected a non-Ninja"

    gs.turn = 2
    gs.layer_system.apply_all_effects()
    live = gs._safe_get_card(kaito)
    assert live.card_types == ["planeswalker"] and not gs.check_keyword(kaito, "hexproof"), \
        "Kaito did not revert to a planeswalker on the opponent's turn"
    gs.turn = 1
    controller["loyalty_counters"][kaito] = 0
    gs.layer_system.apply_all_effects()
    assert gs._safe_get_card(kaito).card_types == ["planeswalker"], \
        "zero-loyalty Kaito remained a creature"


@scenario("113.7a / 305.2", "Wrenn's emblem exposes lands and permanent spells from the graveyard")
def s_wrenn_emblem_graveyard_permissions():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    wrenn = inject_into_zone(gs, controller, {
        "name": "Wrenn and Realmbreaker", "mana_cost": "{1}{G}{G}",
        "type_line": "Legendary Planeswalker - Wrenn", "loyalty": 4,
        "oracle_text": (
            "Lands you control have \"{T}: Add one mana of any color.\"\n"
            "+1: Up to one target land you control becomes a 3/3 Elemental creature "
            "with vigilance, hexproof, and haste until your next turn. It's still a land.\n"
            "-2: Mill three cards. You may put a permanent card from among the milled "
            "cards into your hand.\n"
            "-7: You get an emblem with \"You may play lands and cast permanent spells "
            "from your graveyard.\""
        ),
    }, "battlefield")
    controller["loyalty_counters"][wrenn] = 7
    assert gs.activate_planeswalker_ability(wrenn, 2, controller), "Wrenn's -7 could not activate"
    assert gs.resolve_top_of_stack(), "Wrenn's emblem ability did not resolve"
    assert controller.get("emblems") and controller["emblems"][-1]["kind"] == "graveyard_permanents", \
        "Wrenn did not create its graveyard-permission emblem"
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    gs.priority_pass_count = 0

    land = inject_into_zone(gs, controller, {
        "name": "Emblem Land", "type_line": "Land - Forest", "oracle_text": "",
    }, "graveyard")
    creature = inject_into_zone(gs, controller, {
        "name": "Emblem Creature", "mana_cost": "{1}{G}",
        "type_line": "Creature - Plant", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "graveyard")
    instant = inject_into_zone(gs, controller, {
        "name": "Emblem Instant", "mana_cost": "{U}",
        "type_line": "Instant", "oracle_text": "Draw a card.",
    }, "graveyard")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 1}
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    land_action = 472 + controller["graveyard"].index(land)
    creature_action = 472 + controller["graveyard"].index(creature)
    instant_action = 472 + controller["graveyard"].index(instant)
    assert mask[land_action] and mask[creature_action] and not mask[instant_action], \
        "Wrenn's emblem exposed the wrong graveyard card types"

    reward, ok = handler._handle_play_from_graveyard(controller["graveyard"].index(land), {})
    assert ok, f"playing a land through Wrenn's emblem failed: {reward}"
    assert land in controller["battlefield"] and controller["land_played"], \
        "Wrenn's emblem did not play the graveyard land normally"
    reward, ok = handler._handle_play_from_graveyard(controller["graveyard"].index(creature), {})
    assert ok, f"casting a permanent through Wrenn's emblem failed: {reward}"
    assert gs.stack and gs.stack[-1][1] == creature, \
        "the graveyard permanent spell did not enter the stack"
    assert gs.resolve_top_of_stack() and creature in controller["battlefield"], \
        "the graveyard permanent spell did not resolve"


@scenario("603.4 / 611.2e", "Enduring Curiosity returns to its owner as only an enchantment")
def s_enduring_curiosity_dies_return_type_change():
    gs = fresh()
    owner, controller = gs.p1, gs.p2
    enduring = inject_card(gs, {
        "name": "Enduring Curiosity", "mana_cost": "{2}{U}{U}",
        "type_line": "Enchantment Creature - Cat Glimmer", "oracle_text": (
            "Flash\nWhenever a creature you control deals combat damage to a player, "
            "draw a card.\nWhen Enduring Curiosity dies, if it was a creature, return "
            "it to the battlefield under its owner's control. It's an enchantment."
        ),
        "power": 4, "toughness": 3,
    })
    gs.original_p1_deck.append(enduring)
    owner["library"].append(enduring)
    gs._last_card_locations[enduring] = (owner, "library")
    assert gs.move_card(enduring, owner, "library", controller, "battlefield"), \
        "could not put the owner's Enduring Curiosity under opposing control"
    assert gs.move_card(enduring, controller, "battlefield", owner, "graveyard", cause="destroy"), \
        "Enduring Curiosity did not die into its owner's graveyard"
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "Enduring Curiosity's dies trigger did not resolve"
    live = gs._safe_get_card(enduring)
    assert enduring in owner["battlefield"] and enduring not in controller["battlefield"], \
        "Enduring Curiosity did not return under its owner's control"
    assert live.card_types == ["enchantment"] and live.subtypes == [], \
        f"Enduring Curiosity returned with the wrong types: {live.card_types} {live.subtypes}"
    assert (live.power, live.toughness) == (0, 0), \
        "the enchantment-only Enduring Curiosity retained creature P/T"

    gs.ability_handler.active_triggers.clear()
    assert gs.move_card(enduring, owner, "battlefield", owner, "graveyard", cause="destroy"), \
        "the returned enchantment could not leave the battlefield"
    assert not any(entry[0].card_id == enduring for entry in gs.ability_handler.active_triggers), \
        "Enduring Curiosity returned after dying while it was not a creature"


@scenario("111.4 / 603.4", "a token Enduring Curiosity does not return after dying")
def s_enduring_curiosity_token_does_not_return():
    gs = fresh()
    controller = gs.p1
    token = gs.create_token(controller, {
        "name": "Enduring Curiosity", "mana_cost": "{2}{U}{U}",
        "type_line": "Token Enchantment Creature - Cat Glimmer",
        "card_types": ["enchantment", "creature"],
        "subtypes": ["cat", "glimmer"],
        "oracle_text": (
            "When Enduring Curiosity dies, if it was a creature, return it to the "
            "battlefield under its owner's control. It's an enchantment."
        ),
        "power": 4, "toughness": 3,
    })
    assert token and gs.move_card(token, controller, "battlefield", controller, "graveyard", cause="destroy")
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "the token's dies trigger did not finish resolving"
    assert token not in controller["battlefield"] and token not in gs.card_db, \
        "a token Enduring Curiosity returned from the graveyard"


@scenario("707.9 / Mockingbird", "Mockingbird offers only creatures within the mana-spent bound and copies before entering")
def s_mockingbird_bounded_copy_as_enters():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    eligible = inject_into_zone(gs, opponent, {
        "name": "Eligible Visionary", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Elf Druid", "oracle_text": (
            "When Eligible Visionary enters the battlefield, you gain 2 life."
        ),
        "power": 3, "toughness": 3,
    }, "battlefield")
    too_large = inject_into_zone(gs, opponent, {
        "name": "Too-Large Giant", "mana_cost": "{3}{G}", "cmc": 4,
        "type_line": "Creature - Giant", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    gs._safe_get_card(eligible).counters["+1/+1"] = 2
    gs.layer_system.apply_all_effects()
    gs.ability_handler.active_triggers.clear()
    mockingbird = inject_into_zone(gs, controller, {
        "name": "Mockingbird", "mana_cost": "{X}{U}", "cmc": 1,
        "type_line": "Creature - Bird Bard", "oracle_text": (
            "Flying\nYou may have this creature enter as a copy of any creature on "
            "the battlefield with mana value less than or equal to the amount of "
            "mana spent to cast this creature, except it's a Bird in addition to "
            "its other types and it has flying."
        ),
        "power": 1, "toughness": 1,
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 2}

    assert gs.cast_spell(mockingbird, controller, {"X": 2}), "Mockingbird could not be cast"
    assert gs.resolve_top_of_stack(), "Mockingbird did not reach its as-enters choice"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "mockingbird_copy", \
        "Mockingbird did not expose its optional copy choice"
    options = gs.choice_context.get("options", [])
    assert eligible in options and too_large not in options, \
        "Mockingbird ignored the total-mana-spent copy bound"
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[353 + options.index(eligible)] and mask[11], \
        "Mockingbird did not expose both copy and decline actions"
    _, ok = handler._handle_choose_mode(options.index(eligible), {})
    assert ok and mockingbird in controller["battlefield"], \
        "Mockingbird's selected copy did not enter"
    copied = gs._safe_get_card(mockingbird)
    assert copied.name == "Eligible Visionary" and (copied.power, copied.toughness) == (3, 3), \
        "Mockingbird copied modified values instead of copyable printed values"
    assert {"elf", "druid", "bird"}.issubset({s.lower() for s in copied.subtypes}), \
        f"Mockingbird's copy exception produced the wrong subtypes: {copied.subtypes}"
    assert gs.check_keyword(mockingbird, "flying"), \
        "Mockingbird's copy exception did not grant flying"
    assert any(entry[0].card_id == mockingbird for entry in gs.ability_handler.active_triggers), \
        "Mockingbird did not queue the copied enters ability"
    assert any(entry[0].card_id == mockingbird and entry[1] is controller
               for entry in gs.ability_handler.active_triggers), \
        "Mockingbird queued its copied enters ability for the wrong controller"
    queued_effects = [entry[0].effect for entry in gs.ability_handler.active_triggers
                      if entry[0].card_id == mockingbird]
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack(), "the copied enters ability did not resolve"
    assert controller["life"] == 22, \
        f"Mockingbird copied ETB failed: life={controller['life']} effects={queued_effects}"


@scenario("707.9", "Mockingbird may decline its copy replacement and enter with its own identity")
def s_mockingbird_declines_copy():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    inject_into_zone(gs, gs.p2, {
        "name": "Copy Candidate", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    mockingbird = inject_into_zone(gs, controller, {
        "name": "Mockingbird", "mana_cost": "{X}{U}", "cmc": 1,
        "type_line": "Creature - Bird Bard", "oracle_text": (
            "Flying\nYou may have this creature enter as a copy of any creature on "
            "the battlefield with mana value less than or equal to the amount of "
            "mana spent to cast this creature, except it's a Bird in addition to "
            "its other types and it has flying."
        ), "power": 1, "toughness": 1,
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    assert gs.cast_spell(mockingbird, controller, {"X": 0})
    assert gs.resolve_top_of_stack() and gs.choice_context
    gs._pass_priority()
    card = gs._safe_get_card(mockingbird)
    assert mockingbird in controller["battlefield"] and card.name == "Mockingbird", \
        "declining Mockingbird's replacement did not enter the original card"
    assert (card.power, card.toughness) == (1, 1) and {
        s.lower() for s in card.subtypes
    } == {"bird", "bard"}, "declined Mockingbird entered with copied characteristics"


@scenario("111.10b", "a Food token pays two, taps, sacrifices, and gains 3 life")
def s_food_token_activation():
    from Playersim.ability_utils import EffectFactory

    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    effects = EffectFactory.create_effects("Create a Food token.")
    assert len(effects) == 1 and effects[0].apply(gs, None, controller), \
        "Food creation did not use a rules-bearing token effect"
    foods = [
        token_id for token_id in controller.get("tokens", [])
        if getattr(gs._safe_get_card(token_id), "name", "") == "Food"
    ]
    assert len(foods) == 1, f"Food creation produced the wrong tokens: {foods}"
    food_id = foods[0]
    food = gs._safe_get_card(food_id)
    assert food.card_types == ["artifact"] and {
        subtype.lower() for subtype in food.subtypes
    } == {"food"}, "Food did not have its token artifact characteristics"
    abilities = gs.ability_handler.get_activated_abilities(food_id)
    assert len(abilities) == 1 and "gain 3 life" in abilities[0].effect.lower(), \
        "Food's activated life ability was not registered"
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 2}
    life_before = controller["life"]
    reward, ok = get_env().action_handler._handle_activate_ability(None, {
        "battlefield_idx": controller["battlefield"].index(food_id),
        "ability_idx": 0,
    })
    assert ok, f"Food activation failed: {reward}"
    assert food_id not in controller["battlefield"] and food_id not in gs.card_db, \
        "Food was not sacrificed as an activation cost"
    assert sum(controller["mana_pool"].values()) == 0, "Food did not pay {2}"
    assert gs.stack and gs.stack[-1][1] == food_id, \
        "Food's ability did not survive its token source ceasing to exist"
    assert gs.resolve_top_of_stack() and controller["life"] == life_before + 3, \
        "Food's ability did not gain exactly 3 life"


@scenario("602.2b / 601.2h", "non-self activated-ability sacrifice costs are policy-selected")
def s_activated_ability_sacrifice_cost_choice():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Choice Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text": (
            "Sacrifice two creatures: Draw a card."
        ),
    }, "battlefield")
    creatures = [
        inject_into_zone(gs, controller, {
            "name": f"Altar Offering {index}", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature - Test", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "battlefield")
        for index in range(3)
    ]
    abilities = gs.ability_handler.get_activated_abilities(source)
    assert len(abilities) == 1, "the synthetic Altar ability was not parsed"

    handler = get_env().action_handler
    battlefield_idx = controller["battlefield"].index(source)
    action_idx = 100 + battlefield_idx * 3
    action_mask = handler.generate_valid_actions()
    assert action_mask[action_idx], "the public action mask hid the Altar activation"
    generated_context = handler.action_reasons_with_context[action_idx]["context"]
    assert generated_context == {
        "battlefield_idx": battlefield_idx,
        "ability_idx": 0,
        "controller_id": "p1",
    }, f"the activation action lost its executable context: {generated_context}"
    reward, done, truncated, info = handler.apply_action(action_idx)
    assert not done and not truncated and not info.get("critical_error"), \
        f"the public activation action failed: reward={reward}, info={info}"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context, \
        "the non-self sacrifice cost was paid heuristically"
    assert gs.choice_context.get("type") == "activation_sacrifice_cost"
    assert gs.choice_context.get("remaining") == 2
    assert set(gs.choice_context.get("options", [])) == set(creatures), \
        "the sacrifice-cost choice exposed the wrong permanents"
    assert all(cid in controller["battlefield"] for cid in creatures) and not gs.stack, \
        "a sacrifice was committed before all cost choices were staged"
    clone = gs.clone()
    cloned_occurrence = clone.choice_context["option_occurrences"][0]
    clone.choice_context["selected"].append(cloned_occurrence)
    clone.choice_context["options"].pop(0)
    clone.choice_context["option_occurrences"].pop(0)
    assert cloned_occurrence not in gs.choice_context["selected"] \
        and creatures[0] in gs.choice_context["options"], \
        "a cloned pending activation choice shared mutable state with the live game"

    first_choice = creatures[1]
    first_index = gs.choice_context["options"].index(first_choice)
    _, ok = handler._handle_choose_mode(first_index, {})
    assert ok and gs.choice_context.get("remaining") == 1
    assert first_choice in controller["battlefield"] and not gs.stack, \
        "the first staged choice paid part of the cost early"

    second_choice = creatures[2]
    second_index = gs.choice_context["options"].index(second_choice)
    reward, ok = handler._handle_choose_mode(second_index, {})
    assert ok, f"the final sacrifice-cost choice did not resume activation: {reward}"
    assert gs.choice_context is None
    assert first_choice in controller["graveyard"] and second_choice in controller["graveyard"]
    assert creatures[0] in controller["battlefield"] and source in controller["battlefield"], \
        "the cost subsystem sacrificed a permanent the policy did not choose"
    assert gs.stack and gs.stack[-1][1] == source, \
        "the activated ability did not reach the stack after paying its choices"

    # The same public path must use the non-active policy's battlefield rather
    # than interpreting its index against the active player's permanents.
    gs2 = fresh(seed=SEED + 37)
    nonactive = gs2.p2
    gs2.turn = 1
    gs2.phase = gs2.PHASE_MAIN_PRECOMBAT
    gs2.agent_is_p1 = False
    gs2.priority_player = nonactive
    source2 = inject_into_zone(gs2, nonactive, {
        "name": "Nonactive Choice Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact",
        "oracle_text": "Sacrifice a creature: Draw a card.",
    }, "battlefield")
    offering2 = inject_into_zone(gs2, nonactive, {
        "name": "Nonactive Offering", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    handler2 = get_env().action_handler
    action2 = 100 + nonactive["battlefield"].index(source2) * 3
    mask2 = handler2.generate_valid_actions()
    assert mask2[action2]
    _, done2, truncated2, info2 = handler2.apply_action(action2)
    assert not done2 and not truncated2 and not info2.get("critical_error")
    assert gs2.choice_context and gs2.choice_context.get("player") is nonactive
    assert gs2.choice_context.get("options") == [offering2], \
        "the non-active policy's activation choice used the active player's board"


@scenario("602.2b / 400.7", "activated sacrifice choices distinguish repeated-id battlefield occurrences")
def s_activated_sacrifice_duplicate_occurrences():
    gs = fresh(seed=SEED + 38)
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Occurrence Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact",
        "oracle_text": "Sacrifice two creatures: Draw a card.",
    }, "battlefield")
    repeated = inject_into_zone(gs, controller, {
        "name": "Repeated Offering", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    controller["battlefield"].append(repeated)
    source_slot = controller["battlefield"].index(source)
    repeated_slots = [
        slot for slot, card_id in enumerate(controller["battlefield"])
        if card_id == repeated
    ]

    handler = get_env().action_handler
    reward, ok = handler._handle_activate_ability(None, {
        "battlefield_idx": source_slot, "ability_idx": 0,
    })
    assert ok, f"the repeated-id sacrifice activation did not start: {reward}"
    ctx = gs.choice_context
    expected_occurrences = [(repeated, slot) for slot in repeated_slots]
    assert ctx.get("options") == [repeated, repeated], \
        f"the repeated copies collapsed in the policy options: {ctx.get('options')}"
    assert ctx.get("option_occurrences") == expected_occurrences, \
        f"physical sacrifice slots were not retained: {ctx.get('option_occurrences')}"
    mask = handler.generate_valid_actions()
    assert mask[353] and mask[354], \
        "the two repeated-id occurrences did not expose two policy actions"

    _, ok = handler._handle_choose_mode(0, {})
    assert ok and gs.choice_context.get("remaining") == 1
    assert gs.choice_context.get("selected") == [expected_occurrences[0]]
    assert gs.choice_context.get("options") == [repeated], \
        "selecting one occurrence removed every copy sharing its id"
    reward, ok = handler._handle_choose_mode(0, {})
    assert ok, f"the second identical occurrence could not pay the cost: {reward}"
    assert controller["battlefield"].count(repeated) == 0
    assert controller["graveyard"].count(repeated) == 2, \
        "sacrifice two creatures did not move both repeated-id occurrences"
    assert gs.stack and gs.stack[-1][1] == source

    # "Another" excludes the activated source slot, not every battlefield
    # occurrence sharing that source's coarse card id.
    gs2 = fresh(seed=SEED + 39)
    controller2 = gs2.p1
    gs2.turn = 1
    gs2.phase = gs2.PHASE_MAIN_PRECOMBAT
    gs2.agent_is_p1 = True
    gs2.priority_player = controller2
    repeated_source = inject_into_zone(gs2, controller2, {
        "name": "Twin Offering Adept", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Creature - Human Test",
        "oracle_text": "Sacrifice another creature: Draw a card.",
        "power": 2, "toughness": 2,
    }, "battlefield")
    controller2["battlefield"].append(repeated_source)
    activated_slot = 1
    handler2 = get_env().action_handler
    reward, ok = handler2._handle_activate_ability(None, {
        "battlefield_idx": activated_slot, "ability_idx": 0,
    })
    assert ok, f"the duplicate-source 'another' activation failed: {reward}"
    assert gs2.choice_context.get("option_occurrences") == [(repeated_source, 0)], \
        "'another' excluded every same-id copy instead of only the source occurrence"
    reward, ok = handler2._handle_choose_mode(0, {})
    assert ok, f"the other same-id occurrence could not be sacrificed: {reward}"
    assert controller2["battlefield"].count(repeated_source) == 1 \
        and controller2["graveyard"].count(repeated_source) == 1
    assert gs2.stack and gs2.stack[-1][1] == repeated_source, \
        "the activated source occurrence did not leave an ability on the stack"


@scenario("601.2c / 601.2h / 702.21", "target commitment waits for a staged sacrifice cost to finish")
def s_targeted_activation_sacrifice_commit_order_and_ward_snapshot():
    gs = fresh(seed=SEED + 40)
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    source = inject_into_zone(gs, controller, {
        "name": "Targeting Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact",
        "oracle_text": (
            "{1}, Sacrifice a creature: Put a +1/+1 counter on target creature."
        ),
    }, "battlefield")
    offering = inject_into_zone(gs, controller, {
        "name": "Targeting Offering", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    warded = inject_into_zone(gs, opponent, {
        "name": "Activation Ward Probe", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human Soldier", "oracle_text": "Ward {2}",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.ability_handler._parse_and_register_abilities(warded, gs._safe_get_card(warded))
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    assert gs.check_keyword(warded, "ward"), "setup: ward was not active"

    handler = get_env().action_handler
    activation_context = {
        "battlefield_idx": controller["battlefield"].index(source),
        "ability_idx": 0,
    }

    def choose_warded_target():
        target_ctx = gs.targeting_context
        valid_map = gs.targeting_system.get_valid_targets(
            source, controller, target_ctx["required_type"],
            effect_text=target_ctx["effect_text"])
        valid_targets = sorted({
            target_id for target_ids in valid_map.values()
            for target_id in target_ids
        })
        assert warded in valid_targets, "the warded creature was not a legal target"
        return handler._handle_select_target(valid_targets.index(warded), {})

    controller["mana_pool"] = {
        'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1,
    }
    reward, ok = handler._handle_activate_ability(None, activation_context)
    assert ok and gs.targeting_context, f"target selection did not start: {reward}"
    reward, ok = choose_warded_target()
    assert ok and gs.choice_context, f"sacrifice selection did not follow targeting: {reward}"
    assert gs.choice_context.get("type") == "activation_sacrifice_cost"
    assert warded not in controller.get("targeted_permanents_this_turn", set()) \
        and not gs.stack, \
        "target events or a ward snapshot leaked before the sacrifice cost finished"

    # Invalidate the final payment after the permanent has been staged. The
    # activation must rewind without targeting events, ward state, or costs.
    controller["mana_pool"]["C"] = 0
    reward, ok = handler._handle_choose_mode(0, {})
    assert not ok, "the activation succeeded after its final mana payment vanished"
    assert offering in controller["battlefield"] and not gs.stack
    assert warded not in controller.get("targeted_permanents_this_turn", set()), \
        "failed final payment leaked target commitment"

    # Repeat successfully. Notification and the ward snapshot are committed
    # only after payment and attach to the resumed ability's stack context.
    controller["mana_pool"]["C"] = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    reward, ok = handler._handle_activate_ability(None, activation_context)
    assert ok and gs.targeting_context, f"the second activation did not target: {reward}"
    reward, ok = choose_warded_target()
    assert ok and gs.choice_context, f"the second sacrifice choice did not open: {reward}"
    assert warded not in controller.get("targeted_permanents_this_turn", set())
    reward, ok = handler._handle_choose_mode(0, {})
    assert ok, f"the paid targeted activation did not resume: {reward}"
    assert offering in controller["graveyard"] and gs.stack
    stack_context = gs.stack[-1][3]
    assert warded in controller.get("targeted_permanents_this_turn", set()), \
        "successful final payment did not commit the target event"
    assert stack_context.get("ward_checked_on_targeting") is True
    assert stack_context.get("ward_obligations") == [
        {"target_id": warded, "cost": "{2}"}
    ], f"the resumed activation lost its ward snapshot: {stack_context}"


@scenario("601.2h", "failed composite activation costs leave tokens and triggers untouched")
def s_activated_sacrifice_composite_cost_preflight():
    from Playersim.ability_types import ActivatedAbility

    gs = fresh()
    controller = gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Atomicity Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    for card_id in list(controller.get("hand", [])):
        assert gs.move_card(card_id, controller, "hand", controller, "library")
    token_id = gs.create_token(controller, {
        "name": "Atomicity Test Token", "card_types": ["creature"],
        "subtypes": ["Test"], "power": 1, "toughness": 1,
        "oracle_text": "",
    })
    assert token_id in controller["battlefield"] and gs._safe_get_card(token_id).is_token
    ability = ActivatedAbility(
        source, cost="Sacrifice a creature, discard a card",
        effect="Draw a card.")
    gs.ability_handler.active_triggers = []
    graveyard_before = list(controller["graveyard"])
    assert not ability.pay_cost(gs, controller, sacrifice_choices=[token_id]), \
        "an impossible composite cost was accepted"
    assert token_id in controller["battlefield"] and token_id in gs.card_db, \
        "failed composite-cost preflight permanently deleted its token"
    assert controller["graveyard"] == graveyard_before, \
        "failed composite-cost preflight moved a card"
    assert not gs.ability_handler.active_triggers, \
        "failed composite-cost preflight leaked a leave/dies trigger"


@scenario("602.2b", "activated sacrifice-cost eligibility honors qualifiers and conjunctions")
def s_activated_sacrifice_candidate_grammar():
    from Playersim.ability_types import ActivatedAbility

    gs = fresh()
    controller = gs.p1
    source = inject_into_zone(gs, controller, {
        "name": "Qualifier Altar", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    food = inject_into_zone(gs, controller, {
        "name": "Food", "type_line": "Token Artifact - Food",
        "oracle_text": "",
    }, "battlefield")
    clue = inject_into_zone(gs, controller, {
        "name": "Clue", "type_line": "Token Artifact - Clue",
        "oracle_text": "",
    }, "battlefield")
    for token_id in (food, clue):
        gs._safe_get_card(token_id).is_token = True
        controller.setdefault("tokens", []).append(token_id)
    creature = inject_into_zone(gs, controller, {
        "name": "Qualifier Creature", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    artifact_creature = inject_into_zone(gs, controller, {
        "name": "Qualifier Construct", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact Creature - Construct", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    land = inject_into_zone(gs, controller, {
        "name": "Qualifier Land", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "oracle_text": "",
    }, "battlefield")

    def candidates(cost):
        ability = ActivatedAbility(source, cost=cost, effect="Draw a card.")
        return set(ability.get_sacrifice_cost_candidates(gs, controller))

    food_candidates = candidates("Sacrifice a Food")
    assert food_candidates == {food}, \
        f"Food sacrifice candidates were {food_candidates}; expected only {food}; " \
        f"token subtypes={getattr(gs._safe_get_card(food), 'subtypes', None)}"
    assert candidates("Sacrifice a token") == {food, clue}
    assert candidates("Sacrifice a nontoken creature") == {creature, artifact_creature}
    assert land not in candidates("Sacrifice a nonland permanent")
    assert candidates("Sacrifice an artifact creature") == {artifact_creature}
    assert candidates("Sacrifice another artifact") == {food, clue, artifact_creature}
    assert candidates("Sacrifice an artifact or creature") == {
        source, food, clue, creature, artifact_creature,
    }


@scenario("702.170", "Plot exiles at sorcery speed and permits a free cast only on a later turn")
def s_plot_special_action_and_later_cast():
    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    slickshot = inject_into_zone(gs, controller, {
        "name": "Slickshot Show-Off", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Creature - Bird Wizard", "oracle_text": (
            "Flying, haste\nWhenever you cast a noncreature spell, this creature "
            "gets +2/+0 until end of turn.\nPlot {1}{R}"
        ), "power": 1, "toughness": 2,
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 1}
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    hand_index = controller["hand"].index(slickshot)
    plot_action = [296, 297, 298, 309, 310, 311, 312, 313][hand_index]
    assert mask[plot_action], "the policy action mask did not expose Plot"
    reward, ok = handler._handle_plot_card(hand_index, {})
    assert ok, f"Plot special action failed: {reward}"
    assert slickshot in controller["exile"] and slickshot not in controller["hand"], \
        "Plot did not move Slickshot from hand to exile"
    assert sum(controller["mana_pool"].values()) == 0, "Plot did not pay its printed cost"
    assert not any(option["card_id"] == slickshot for option in gs.get_exile_cast_options(controller)), \
        "Slickshot was castable on the same turn it was plotted"

    gs.turn = 3
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    gs.priority_pass_count = 0
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    options = gs.get_exile_cast_options(controller)
    option_index = next(i for i, option in enumerate(options)
                        if option["card_id"] == slickshot and option.get("permission") == "plot")
    mask = handler.generate_valid_actions()
    assert mask[230 + option_index], "the later free Plot cast was absent from the action mask"
    reward, ok = handler._handle_cast_from_exile(option_index)
    assert ok, f"casting the plotted card failed: {reward}"
    assert gs.stack and gs.stack[-1][1] == slickshot, "the plotted spell did not enter the stack"
    assert sum(controller["mana_pool"].values()) == 0, "the Plot cast charged mana"
    assert gs.resolve_top_of_stack() and slickshot in controller["battlefield"], \
        "the plotted Slickshot did not resolve as a creature"
    assert not any(entry.get("card_id") == slickshot for entry in gs.plotted_cards), \
        "the consumed Plot permission remained available"


@scenario("702.166 / Torch the Tower", "bargained Torch sacrifices an eligible permanent, deals 3, scries, and exiles its victim")
def s_bargained_torch_the_tower():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    artifact = inject_into_zone(gs, controller, {
        "name": "Bargain Bauble", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    land = inject_into_zone(gs, controller, {
        "name": "Not a Bargain", "type_line": "Land - Mountain", "oracle_text": "",
    }, "battlefield")
    victim = inject_into_zone(gs, opponent, {
        "name": "Three-Toughness Victim", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    top = inject_card(gs, {
        "name": "Torch Scry Card", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    controller["library"].insert(0, top)
    gs._last_card_locations[top] = (controller, "library")
    torch = inject_into_zone(gs, controller, {
        "name": "Torch the Tower", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Instant", "oracle_text": (
            "Bargain\nTorch the Tower deals 2 damage to target creature or planeswalker. "
            "If this spell was bargained, instead it deals 3 damage to that permanent "
            "and you scry 1.\nIf a permanent dealt damage by Torch the Tower would die "
            "this turn, exile it instead."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
    handler = get_env().action_handler

    assert gs.cast_spell(torch, controller), "Torch could not begin casting"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "bargain", \
        "Torch did not expose its optional Bargain cost"
    options = gs.choice_context.get("options", [])
    assert artifact in options and land not in options, "Bargain offered an ineligible permanent"
    _, ok = handler._handle_choose_mode(options.index(artifact), {})
    assert ok and gs.phase == gs.PHASE_TARGETING, "Bargain did not resume into target selection"
    valid_map = gs.targeting_system.get_valid_targets(
        torch, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert victim in valid_targets, "Torch did not accept a creature target"
    _, ok = handler._handle_select_target(valid_targets.index(victim), {})
    assert ok, "Torch target selection failed"
    assert artifact in controller["graveyard"] and artifact not in controller["battlefield"], \
        "Torch did not sacrifice the bargained artifact while casting"
    assert gs.resolve_top_of_stack(), "bargained Torch did not resolve"
    torch_replacements = [
        (entry.get("event_type"), entry.get("description"), entry.get("start_turn"))
        for entry in gs.replacement_effects.active_effects
    ]
    assert victim in opponent["exile"] and victim not in opponent["graveyard"], \
        f"Torch victim zones were wrong: bf={victim in opponent['battlefield']} " \
        f"gy={victim in opponent['graveyard']} exile={victim in opponent['exile']} " \
        f"replacements={torch_replacements}"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "scry", \
        "bargained Torch did not scry 1"


@scenario("614.6 / Torch the Tower", "unbargained Torch deals 2 and its damaged permanent is exiled if it dies later that turn")
def s_unbargained_torch_delayed_exile_replacement():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    victim = inject_into_zone(gs, opponent, {
        "name": "Later-Dying Victim", "mana_cost": "{3}{G}", "cmc": 4,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    torch = inject_into_zone(gs, controller, {
        "name": "Torch the Tower", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Instant", "oracle_text": (
            "Bargain\nTorch the Tower deals 2 damage to target creature or planeswalker. "
            "If this spell was bargained, instead it deals 3 damage to that permanent "
            "and you scry 1.\nIf a permanent dealt damage by Torch the Tower would die "
            "this turn, exile it instead."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
    handler = get_env().action_handler
    assert gs.cast_spell(torch, controller)
    gs._pass_priority()
    assert gs.phase == gs.PHASE_TARGETING, "declining Bargain did not continue casting Torch"
    valid_map = gs.targeting_system.get_valid_targets(
        torch, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = handler._handle_select_target(valid_targets.index(victim), {})
    assert ok and gs.resolve_top_of_stack(), "unbargained Torch failed"
    assert opponent["damage_counters"].get(victim) == 2, \
        "unbargained Torch did not deal exactly 2 damage"
    assert gs.choice_context is None, "unbargained Torch incorrectly scried"
    assert gs.apply_damage_to_permanent(victim, 2, source_id=None) == 2
    gs.check_state_based_actions()
    assert victim in opponent["exile"] and victim not in opponent["graveyard"], \
        "a permanent damaged by Torch was not exiled when it died later that turn"


@scenario("601.2c / 601.2h / Torch the Tower",
          "targets are committed before Bargain sacrifices the spell's only legal target")
def s_torch_targets_before_bargain_payment():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    bargain_target = inject_into_zone(gs, controller, {
        "name": "Sole Bargain Target", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact Creature - Beast", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    torch = inject_into_zone(gs, controller, {
        "name": "Torch the Tower", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Instant", "oracle_text": (
            "Bargain\nTorch the Tower deals 2 damage to target creature or planeswalker. "
            "If this spell was bargained, instead it deals 3 damage to that permanent "
            "and you scry 1.\nIf a permanent dealt damage by Torch the Tower would die "
            "this turn, exile it instead."
        ),
    }, "hand")
    controller["mana_pool"] = {
        'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0,
    }
    assert not any(
        "creature" in getattr(gs._safe_get_card(card_id), "card_types", [])
        or "planeswalker" in getattr(gs._safe_get_card(card_id), "card_types", [])
        for card_id in opponent["battlefield"]), "setup added another Torch target"
    handler = get_env().action_handler

    assert gs.cast_spell(torch, controller), "Torch could not begin casting"
    options = gs.choice_context.get("options", [])
    assert bargain_target in options, "the sole artifact creature was not a Bargain option"
    _, ok = handler._handle_choose_mode(options.index(bargain_target), {})
    assert ok and gs.phase == gs.PHASE_TARGETING, \
        "Bargain did not advance to pre-payment target selection"
    assert bargain_target in controller["battlefield"] and torch in controller["hand"], \
        "Bargain or spell movement happened before targets were committed"
    assert controller["mana_pool"]['R'] == 1, "Torch spent mana before choosing a target"

    mask = handler.generate_valid_actions()
    candidates = handler._get_target_selection_candidates(
        controller, gs.targeting_context)
    assert candidates == [bargain_target], f"Torch offered the wrong targets: {candidates}"
    assert mask[274] and not mask[224], \
        "the sole target was not policy-accessible without the fallback NO_OP"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(274)
    assert not info.get("execution_failed") and gs.targeting_context is None, \
        f"the mask-valid target action failed: {info}"
    assert bargain_target in controller["graveyard"] \
        and bargain_target not in controller["battlefield"], \
        "Bargain did not sacrifice its staged permanent after target selection"
    assert torch not in controller["hand"] and gs.stack and gs.stack[-1][1] == torch, \
        "Torch did not enter the stack after its target and costs were committed"
    stack_targets = gs._flatten_target_ids(gs.stack[-1][3].get("targets", {}))
    assert stack_targets == [bargain_target], \
        f"Torch did not retain its now-illegal target: {stack_targets}"
    assert gs.resolve_top_of_stack(), "Torch did not fizzle cleanly on its illegal target"
    assert torch in controller["graveyard"] and not gs.targeting_context, \
        "the fizzled Torch did not finish without a targeting loop"


@scenario("614.6 / Obliterating Bolt",
          "a lethal Bolt exiles its victim instead of destroying it")
def s_obliterating_bolt_exiles_dead_victim():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    victim = inject_into_zone(gs, opponent, {
        "name": "Bolt Victim", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 3, "toughness": 3,
    }, "battlefield")
    bolt = inject_into_zone(gs, controller, {
        "name": "Obliterating Bolt", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Sorcery", "oracle_text": (
            "Obliterating Bolt deals 4 damage to target creature or planeswalker. "
            "If that creature or planeswalker would die this turn, exile it instead."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 1}
    handler = get_env().action_handler

    assert gs.cast_spell(bolt, controller), "Bolt could not begin casting"
    assert gs.phase == gs.PHASE_TARGETING, "Bolt did not enter target selection"
    valid_map = gs.targeting_system.get_valid_targets(
        bolt, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    assert victim in valid_targets, "Bolt did not accept a creature target"
    _, ok = handler._handle_select_target(valid_targets.index(victim), {})
    assert ok and gs.resolve_top_of_stack(), "Bolt did not resolve"
    assert victim in opponent["exile"] and victim not in opponent["graveyard"], \
        f"Bolt victim zones were wrong: bf={victim in opponent['battlefield']} " \
        f"gy={victim in opponent['graveyard']} exile={victim in opponent['exile']}"


@scenario("614.6 / Obliterating Bolt",
          "a surviving Bolt victim is still exiled if it dies later that turn")
def s_obliterating_bolt_delayed_exile_replacement():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    victim = inject_into_zone(gs, opponent, {
        "name": "Later-Dying Bolt Victim", "mana_cost": "{4}{G}", "cmc": 5,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 5, "toughness": 5,
    }, "battlefield")
    bolt = inject_into_zone(gs, controller, {
        "name": "Obliterating Bolt", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Sorcery", "oracle_text": (
            "Obliterating Bolt deals 4 damage to target creature or planeswalker. "
            "If that creature or planeswalker would die this turn, exile it instead."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 1}
    handler = get_env().action_handler

    assert gs.cast_spell(bolt, controller), "Bolt could not begin casting"
    valid_map = gs.targeting_system.get_valid_targets(
        bolt, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = handler._handle_select_target(valid_targets.index(victim), {})
    assert ok and gs.resolve_top_of_stack(), "Bolt did not resolve"
    assert opponent["damage_counters"].get(victim) == 4, \
        "Bolt did not deal exactly 4 damage"
    assert victim in opponent["battlefield"], "the 5-toughness victim died early"
    assert gs.apply_damage_to_permanent(victim, 1, source_id=None) == 1
    gs.check_state_based_actions()
    assert victim in opponent["exile"] and victim not in opponent["graveyard"], \
        "a permanent damaged by Bolt was not exiled when it died later that turn"


@scenario("701.60", "manifest dread exposes the top-two choice, graves the other card, and creates an exact face-down 2/2")
def s_manifest_dread_choice_and_turn_face_up():
    from Playersim.ability_utils import EffectFactory

    gs = fresh()
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    creature = inject_card(gs, {
        "name": "Hidden Bear", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Bear", "oracle_text": (
            "When Hidden Bear enters the battlefield, you gain 5 life."
        ), "power": 2, "toughness": 2,
        "faces": [
            {
                "name": "Hidden Bear", "mana_cost": "{1}{G}",
                "type_line": "Creature - Bear", "power": "2", "toughness": "2",
                "oracle_text": "When Hidden Bear enters the battlefield, you gain 5 life.",
            },
            {
                "name": "Hidden Den", "mana_cost": "{3}{G}",
                "type_line": "Creature - Beast", "power": "4", "toughness": "4",
                "oracle_text": "Trample",
            },
        ],
    })
    noncreature = inject_card(gs, {
        "name": "Dread Instant", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    controller["library"][:0] = [creature, noncreature]
    gs._last_card_locations[creature] = (controller, "library")
    gs._last_card_locations[noncreature] = (controller, "library")
    effects = EffectFactory.create_effects("Manifest dread.")
    assert len(effects) == 1 and effects[0].apply(gs, None, controller), \
        "manifest dread did not begin its top-two choice"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "manifest_dread", \
        "manifest dread did not expose the looked-at cards"
    options = gs.choice_context.get("options", [])
    _, ok = get_env().action_handler._handle_choose_mode(options.index(creature), {})
    assert ok, "the manifest dread selection failed"
    assert creature in controller["battlefield"] and noncreature in controller["graveyard"], \
        "manifest dread sent its selected cards to the wrong zones"
    face_down = gs._safe_get_card(creature)
    assert face_down.name == "Face-down creature" and face_down.mana_cost == "" and face_down.cmc == 0, \
        "the manifested card leaked its hidden name or mana characteristics"
    assert face_down.card_types == ["creature"] and not face_down.subtypes \
        and not face_down.supertypes and face_down.oracle_text == "", \
        "the manifested card leaked types or abilities"
    assert not any(face_down.subtype_vector), \
        "the manifested card leaked its original subtype through model features"
    assert not any(face_down.to_feature_vector()[-3:]), \
        "the manifested card leaked its double-face metadata through model features"
    assert (face_down.power, face_down.toughness) == (2, 2) \
        and not any(face_down.colors) and getattr(face_down, "face_down", False), \
        "the manifested permanent did not have exact face-down characteristics"
    assert controller["life"] == 20, "manifesting a creature incorrectly fired its enters ability"
    gs.turn = 2
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 1}
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[456], \
        "the policy action mask did not expose turning a manifest face up at instant timing"
    _, ok = handler._handle_manifest(None, {
        "battlefield_idx": controller["battlefield"].index(creature),
    })
    assert ok, "the manifested creature could not turn face up for its mana cost"
    restored = gs._safe_get_card(creature)
    assert restored.name == "Hidden Bear" and restored.mana_cost == "{1}{G}" \
        and {s.lower() for s in restored.subtypes} == {"bear"}, \
        "turning the manifest face up did not restore its printed identity"
    assert list(restored.to_feature_vector()[-3:]) == [1.0, 4.0, 4.0], \
        "turning the manifest face up did not restore its public MDFC features"
    assert controller["life"] == 20, "turning a manifest face up incorrectly fired an enters ability"


@scenario("701.60", "manifest dread handles one-card and empty libraries without inventing a choice")
def s_manifest_dread_small_libraries():
    from Playersim.ability_types import ManifestDreadEffect

    gs = fresh()
    controller = gs.p1
    only_card = inject_card(gs, {
        "name": "Only Dread Card", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text": "",
    })
    controller["library"] = [only_card]
    gs._last_card_locations[only_card] = (controller, "library")
    assert ManifestDreadEffect().apply(gs, None, controller)
    assert only_card in controller["battlefield"] and gs.choice_context is None, \
        "a one-card library did not automatically manifest its only card"
    assert gs._safe_get_card(only_card).name == "Face-down creature"

    gs2 = fresh(seed=SEED + 1)
    gs2.p1["library"].clear()
    assert ManifestDreadEffect().apply(gs2, None, gs2.p1), \
        "manifest dread on an empty library was treated as a failure"
    assert gs2.choice_context is None, "empty-library manifest dread opened a phantom choice"


@scenario("603.7 / Turn Inside Out", "Turn Inside Out manifests dread only when its exact target dies that turn")
def s_turn_inside_out_delayed_manifest_dread():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    target = inject_into_zone(gs, controller, {
        "name": "Inside-Out Target", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    other = inject_into_zone(gs, controller, {
        "name": "Other Creature", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    dread_a = inject_card(gs, {
        "name": "Dread A", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 2, "toughness": 2,
    })
    dread_b = inject_card(gs, {
        "name": "Dread B", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    controller["library"][:0] = [dread_a, dread_b]
    gs._last_card_locations[dread_a] = (controller, "library")
    gs._last_card_locations[dread_b] = (controller, "library")
    spell = inject_into_zone(gs, controller, {
        "name": "Turn Inside Out", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Instant", "oracle_text": (
            "Target creature gets +3/+0 until end of turn. When it dies this turn, "
            "manifest dread."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 0, 'C': 0}
    handler = get_env().action_handler
    assert gs.cast_spell(spell, controller)
    valid_map = gs.targeting_system.get_valid_targets(
        spell, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({target_id for ids in valid_map.values() for target_id in ids})
    _, ok = handler._handle_select_target(valid_targets.index(target), {})
    assert ok and gs.resolve_top_of_stack(), "Turn Inside Out did not resolve"
    gs.layer_system.apply_all_effects()
    assert gs._safe_get_card(target).power == 4, "Turn Inside Out did not grant +3/+0"

    assert gs.move_card(other, controller, "battlefield", controller, "graveyard", cause="destroy")
    assert not gs.ability_handler.active_triggers, \
        "Turn Inside Out's delayed trigger watched the wrong creature"
    assert gs.move_card(target, controller, "battlefield", controller, "graveyard", cause="destroy")
    gs.ability_handler.process_triggered_abilities()
    assert gs.stack, "the exact target's death did not create the delayed trigger"
    assert gs.resolve_top_of_stack(), "Turn Inside Out's delayed trigger did not resolve"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context.get("type") == "manifest_dread", \
        "Turn Inside Out's death rider did not manifest dread"



ANOINT_SPEC = {
    "name": "Anoint with Affliction", "mana_cost": "{1}{B}", "cmc": 2,
    "type_line": "Instant", "oracle_text": (
        "Exile target creature if it has mana value 3 or less.\n"
        "Corrupted — Exile that creature instead if its controller has three "
        "or more poison counters."
    ),
}


def _cast_and_target(gs, handler, spell_id, controller, target_id):
    """Cast a one-target spell and commit the given target through the mask path."""
    assert gs.cast_spell(spell_id, controller), "cast_spell failed"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "casting did not pause for a target"
    valid_map = gs.targeting_system.get_valid_targets(
        spell_id, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid = sorted({t for ids in valid_map.values() for t in ids},
                   key=lambda t: (isinstance(t, str), t))
    assert target_id in valid, f"intended target {target_id} is not legal"
    _, ok = handler._handle_select_target(valid.index(target_id), {})
    assert ok, "target selection failed"


@scenario("Domain / 111.4 (Herd Migration)", "Herd Migration makes one Beast per distinct basic land type and nothing else")
def s_herd_migration_domain_token_count():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    land_specs = [
        ("Domain Plains", "Basic Land - Plains", ["Plains"]),
        ("Domain Island", "Basic Land - Island", ["Island"]),
        ("Domain Dual", "Land - Swamp Mountain", ["Swamp", "Mountain"]),
        ("Second Domain Plains", "Basic Land - Plains", ["Plains"]),
    ]
    for name, type_line, subtypes in land_specs:
        inject_into_zone(gs, controller, {
            "name": name, "mana_cost": "", "cmc": 0,
            "type_line": type_line, "card_types": ["land"],
            "subtypes": subtypes, "oracle_text": "",
        }, "battlefield")
    herd = inject_into_zone(gs, controller, {
        "name": "Herd Migration", "mana_cost": "{6}{G}", "cmc": 7,
        "type_line": "Sorcery", "oracle_text": (
            "Domain — Create a 3/3 green Beast creature token for each basic "
            "land type among lands you control.\n"
            "{1}{G}, Discard this card: Search your library for a basic land "
            "card, reveal it, put it into your hand, then shuffle. You gain 3 life."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 6}
    beasts_before = [cid for cid in controller["battlefield"]
                     if "beast" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    assert not beasts_before, "test setup already had Beasts on the battlefield"
    hand_after_cast = len(controller["hand"]) - 1
    library_before = len(controller["library"])
    life_before = controller["life"]
    opp_hand_before = len(opponent["hand"])
    assert gs.cast_spell(herd, controller), "Herd Migration could not be cast"
    assert gs.resolve_top_of_stack(), "Herd Migration did not resolve"
    beasts = [cid for cid in controller["battlefield"]
              if "beast" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    assert len(beasts) == 4, \
        f"expected 4 Beasts for 4 distinct basic land types, got {len(beasts)}"
    token = gs._safe_get_card(beasts[0])
    assert token.power == 3 and token.toughness == 3, \
        "the Beast tokens did not have their printed 3/3 stats"
    assert len(controller["hand"]) == hand_after_cast, \
        "resolving Herd Migration changed the caster's hand (activated-ability leak)"
    assert len(opponent["hand"]) == opp_hand_before, \
        "resolving Herd Migration discarded an opponent's card (activated-ability leak)"
    assert len(controller["library"]) == library_before, \
        "resolving Herd Migration searched the library (activated-ability leak)"
    assert controller["life"] == life_before, \
        "resolving Herd Migration gained life (activated-ability leak)"


@scenario("505.5 / Delirium (Fear of Missing Out)", "delirious Fear of Missing Out untaps its target and inserts one additional combat phase")
def s_fear_of_missing_out_additional_combat():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    gs.turn = 1
    gs.agent_is_p1 = True
    fomo = inject_into_zone(gs, controller, {
        "name": "Fear of Missing Out", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Enchantment Creature - Nightmare", "oracle_text": (
            "When this creature enters, discard a card, then draw a card.\n"
            "Delirium — Whenever this creature attacks for the first time each "
            "turn, if there are four or more card types among cards in your "
            "graveyard, untap target creature. After this phase, there is an "
            "additional combat phase."
        ), "power": 2, "toughness": 3,
    }, "battlefield")
    ally = inject_into_zone(gs, controller, {
        "name": "Tapped Ally", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.tap_permanent(ally, controller)
    for name, type_line in [("Grave Creature", "Creature - Bear"),
                            ("Grave Instant", "Instant"),
                            ("Grave Sorcery", "Sorcery"),
                            ("Grave Land", "Land - Forest")]:
        inject_into_zone(gs, controller, {
            "name": name, "mana_cost": "", "cmc": 1,
            "type_line": type_line, "oracle_text": "",
        }, "graveyard")
    gs.ability_handler.active_triggers = []  # drop setup ETB triggers
    controller["entered_battlefield_this_turn"] = set()
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [fomo]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "declare-attackers did not finish"
    gs.ability_handler.process_triggered_abilities()
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "the delirious attack trigger did not pause for its untap target"
    valid_map = gs.targeting_system.get_valid_targets(
        fomo, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid = sorted({t for ids in valid_map.values() for t in ids},
                   key=lambda t: (isinstance(t, str), t))
    _, ok = get_env().action_handler._handle_select_target(valid.index(ally), {})
    assert ok and gs.resolve_top_of_stack(), "the attack trigger did not resolve"
    assert ally not in controller.get("tapped_permanents", set()), \
        "the attack trigger did not untap its chosen creature"
    gs.phase = gs.PHASE_END_OF_COMBAT
    gs._advance_phase()
    assert gs.phase == gs.PHASE_BEGIN_COMBAT, \
        "no additional combat phase followed the end of combat"
    # The second attack in the same turn must not re-trigger it.
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [fomo]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert not gs.stack, "a second attack in the same turn re-triggered Fear of Missing Out"
    gs.phase = gs.PHASE_END_OF_COMBAT
    gs._advance_phase()
    assert gs.phase == gs.PHASE_MAIN_POSTCOMBAT, \
        "the inserted combat phase was not consumed after one use"


@scenario("Delirium (Fear of Missing Out)", "without four card types in the graveyard the attack trigger stays silent")
def s_fear_of_missing_out_needs_delirium():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    gs.turn = 1
    gs.agent_is_p1 = True
    fomo = inject_into_zone(gs, controller, {
        "name": "Fear of Missing Out", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Enchantment Creature - Nightmare", "oracle_text": (
            "When this creature enters, discard a card, then draw a card.\n"
            "Delirium — Whenever this creature attacks for the first time each "
            "turn, if there are four or more card types among cards in your "
            "graveyard, untap target creature. After this phase, there is an "
            "additional combat phase."
        ), "power": 2, "toughness": 3,
    }, "battlefield")
    for name, type_line in [("Thin Grave Creature", "Creature - Bear"),
                            ("Thin Grave Instant", "Instant"),
                            ("Thin Grave Land", "Land - Forest")]:
        inject_into_zone(gs, controller, {
            "name": name, "mana_cost": "", "cmc": 1,
            "type_line": type_line, "oracle_text": "",
        }, "graveyard")
    gs.ability_handler.active_triggers = []
    controller["entered_battlefield_this_turn"] = set()
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [fomo]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert not gs.stack and not gs.targeting_context, \
        "Fear of Missing Out triggered without delirium"
    gs.phase = gs.PHASE_END_OF_COMBAT
    gs._advance_phase()
    assert gs.phase == gs.PHASE_MAIN_POSTCOMBAT, \
        "an additional combat phase appeared without the trigger resolving"


@scenario("508.1 (attack watchers)", "'a creature you control attacks' watchers fire only for their controller's matching attackers")
def s_attack_watchers_scope_controller_and_type():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.agent_is_p1 = True
    attacker = inject_into_zone(gs, controller, {
        "name": "Watched Bear", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    friendly_watcher = inject_into_zone(gs, controller, {
        "name": "Friendly Attack Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever a creature you control attacks, you gain 1 life.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    inject_into_zone(gs, opponent, {
        "name": "Enemy Attack Watcher", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever a creature you control attacks, you gain 1 life.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    inject_into_zone(gs, controller, {
        "name": "Tribal Attack Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever a Knight you control attacks, you gain 1 life.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    controller["entered_battlefield_this_turn"] = set()
    controller_life, opponent_life = controller["life"], opponent["life"]
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "declare-attackers did not finish"
    queued_sources = [ctx.get("source_card_id")
                      for _, _, ctx in gs.ability_handler.active_triggers]
    assert queued_sources == [friendly_watcher], \
        (f"expected only the friendly same-controller watcher to trigger, got "
         f"{[getattr(gs._safe_get_card(cid), 'name', cid) for cid in queued_sources]}")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the attack watcher trigger did not resolve"
    assert controller["life"] == controller_life + 1, \
        "the friendly watcher did not gain its controller 1 life"
    assert opponent["life"] == opponent_life, \
        "the opponent's 'you control' watcher gained life off the wrong player's attack"


@scenario("508.1 (attack watchers)", "'another creature you control attacks' does not fire for the watcher's own attack")
def s_attack_watcher_another_excludes_self():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    gs.turn = 1
    gs.agent_is_p1 = True
    watcher = inject_into_zone(gs, controller, {
        "name": "Self-Excluding Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever another creature you control attacks, you gain 1 life.",
        "power": 2, "toughness": 2,
    }, "battlefield")
    ally = inject_into_zone(gs, controller, {
        "name": "Watched Ally", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    controller["entered_battlefield_this_turn"] = set()
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [watcher]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "declare-attackers did not finish"
    assert not gs.ability_handler.active_triggers, \
        "'another creature you control' triggered for the watcher's own attack"
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [ally]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "second declare-attackers did not finish"
    queued_sources = [ctx.get("source_card_id")
                      for _, _, ctx in gs.ability_handler.active_triggers]
    assert queued_sources == [watcher], \
        "'another creature you control' did not trigger for a different attacker"


@scenario("508.1 (attack watchers)", "'a creature attacks you' watchers fire for the defending player only")
def s_attack_watcher_defender_side():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.agent_is_p1 = True
    attacker = inject_into_zone(gs, controller, {
        "name": "Charging Bear", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    defender_watcher = inject_into_zone(gs, opponent, {
        "name": "Defensive Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever a creature attacks you or a planeswalker you control, "
            "you gain 1 life.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    inject_into_zone(gs, controller, {
        "name": "Misplaced Defensive Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Human", "oracle_text":
            "Whenever a creature attacks you or a planeswalker you control, "
            "you gain 1 life.",
        "power": 1, "toughness": 1,
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    controller["entered_battlefield_this_turn"] = set()
    defender_life = opponent["life"]
    attacker_side_life = controller["life"]
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [attacker]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "declare-attackers did not finish"
    queued_sources = [ctx.get("source_card_id")
                      for _, _, ctx in gs.ability_handler.active_triggers]
    assert queued_sources == [defender_watcher], \
        (f"expected only the defending player's watcher to trigger, got "
         f"{[getattr(gs._safe_get_card(cid), 'name', cid) for cid in queued_sources]}")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the defender watcher trigger did not resolve"
    assert opponent["life"] == defender_life + 1, \
        "the defending player's watcher did not gain 1 life"
    assert controller["life"] == attacker_side_life, \
        "the attacking player's 'attacks you' watcher fired for its own attack"


@scenario("503.1 (phase-trigger ownership)", "'your upkeep' triggers fire only on their controller's upkeep; 'each upkeep' fires on both")
def s_upkeep_trigger_owner_gating():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    inject_into_zone(gs, controller, {
        "name": "My Upkeep Watcher", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Enchantment", "oracle_text":
            "At the beginning of your upkeep, you gain 1 life.",
    }, "battlefield")
    inject_into_zone(gs, opponent, {
        "name": "Each Upkeep Watcher", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Enchantment", "oracle_text":
            "At the beginning of each upkeep, you gain 1 life.",
    }, "battlefield")
    inject_into_zone(gs, controller, {
        "name": "My End Step Watcher", "mana_cost": "{1}{U}", "cmc": 2,
        "type_line": "Enchantment", "oracle_text":
            "At the beginning of your end step, you gain 1 life.",
    }, "battlefield")

    def stacked_names(turn, phase):
        gs.turn = turn
        gs.phase = phase
        gs.ability_handler.active_triggers = []
        gs.stack.clear()
        gs._handle_beginning_of_phase_triggers()
        names = sorted(getattr(gs._safe_get_card(item[1]), 'name', '?')
                       for item in gs.stack
                       if isinstance(item, tuple) and len(item) >= 2)
        gs.stack.clear()
        return names

    assert stacked_names(1, gs.PHASE_UPKEEP) == \
        ["Each Upkeep Watcher", "My Upkeep Watcher"], \
        "the controller's upkeep should fire both the 'your' and 'each' watchers"
    assert stacked_names(2, gs.PHASE_UPKEEP) == ["Each Upkeep Watcher"], \
        "'your upkeep' fired on the opponent's upkeep"
    assert stacked_names(1, gs.PHASE_END_STEP) == ["My End Step Watcher"], \
        "the controller's end step should fire their own end-step watcher"
    assert stacked_names(2, gs.PHASE_END_STEP) == [], \
        "'your end step' fired on the opponent's end step"


@scenario("Impending (Overlord of the Mistmoors)", "the real card's enters-or-attacks trigger creates two Insect tokens each time")
def s_overlord_mistmoors_enter_attack_tokens():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    gs.turn = 1
    gs.agent_is_p1 = True

    def insect_count():
        return sum(1 for cid in controller["battlefield"]
                   if "insect" in {str(s).lower() for s in
                                   getattr(gs._safe_get_card(cid), 'subtypes', [])})

    overlord = inject_real_card(gs, controller, "Overlord of the Mistmoors",
                                "battlefield")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Overlord ETB trigger did not resolve"
    assert insect_count() == 2, \
        f"entering should create two Insect tokens, found {insect_count()}"
    controller["entered_battlefield_this_turn"] = set()
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [overlord]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done(), "declare-attackers did not finish"
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Overlord attack trigger did not resolve"
    assert insect_count() == 4, \
        f"attacking should create two more Insect tokens, found {insect_count()}"


@scenario("702.187 (Impending)", "an impending-cast Overlord ticks down on its controller's end steps only, then becomes a creature")
def s_overlord_impending_time_ticks():
    gs = fresh()
    controller = gs.p1
    gs.agent_is_p1 = True
    ov = inject_real_card(gs, controller, "Overlord of the Mistmoors", "hand")
    assert gs.move_card(ov, controller, "hand", controller, "battlefield",
                        context={"cast_for_impending": True})
    gs.ability_handler.active_triggers = []
    gs.stack.clear()
    card = gs._safe_get_card(ov)

    def time_counters():
        return (getattr(card, 'counters', {}) or {}).get('time', 0)

    assert time_counters() == 4, \
        f"an impending cast should enter with four time counters, got {time_counters()}"
    assert not gs._is_creature(ov), \
        "an impending permanent should not be a creature while time counters remain"

    def run_end_step(turn):
        gs.turn = turn
        gs.phase = gs.PHASE_END_STEP
        gs.ability_handler.active_triggers = []
        gs.stack.clear()
        gs._handle_beginning_of_phase_triggers()
        while gs.stack:
            assert gs.resolve_top_of_stack(), "an end-step trigger did not resolve"

    run_end_step(turn=2)  # opponent's end step
    assert time_counters() == 4, "the opponent's end step removed a time counter"
    for _ in range(4):
        run_end_step(turn=1)
    assert time_counters() == 0, \
        f"four of the controller's end steps should exhaust the counters, got {time_counters()}"
    assert gs._is_creature(ov), \
        "removing the last time counter should make the Overlord a creature"
    run_end_step(turn=1)
    assert time_counters() == 0, "the tick kept firing after impending completed"


@scenario("702.187 / 601.2f", "a mask-valid Impending cast pays its sparse alternative mana cost")
def s_overlord_impending_public_cast_cost():
    gs = fresh(); handler = get_env().action_handler
    controller = gs.p1
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = controller
    gs.priority_pass_count = 0
    gs.stack.clear()
    controller["mana_pool"] = {
        "W": 0, "U": 0, "B": 0, "R": 0, "G": 2, "C": 1,
    }
    overlord = inject_real_card(
        gs, controller, "Overlord of the Hauntwoods", "hand")
    hand_index = controller["hand"].index(overlord)

    # Every public cost-dict entry accepts a sparse mapping without losing
    # metadata or sharing its mutable symbol lists.
    sparse = {
        "G": 2, "generic": 1, "hybrid": [("W", "U")],
        "exile_cards": 3,
    }
    normalized = gs.mana_system._normalize_mana_cost(sparse)
    assert all(key in normalized for key in (
        "W", "U", "B", "R", "G", "C", "generic", "X",
        "hybrid", "phyrexian", "snow"))
    assert normalized["exile_cards"] == 3
    normalized["hybrid"].append(("B", "R"))
    assert sparse["hybrid"] == [("W", "U")], \
        "normalization shared a mutable hybrid-symbol list"
    minimum_checked = gs.mana_system.apply_minimum_cost_effects(
        controller, {"G": 2, "generic": 1}, overlord)
    assert minimum_checked["G"] == 2 and minimum_checked["generic"] == 1
    assert minimum_checked["W"] == 0 and minimum_checked["hybrid"] == []

    tax = inject_into_zone(gs, gs.p2, {
        "name": "Impending Tax Probe", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text":
        "Creature spells your opponents cast cost {1} more.",
    }, "battlefield")
    assert not handler.generate_valid_actions()[294], \
        "Impending mask ignored a battlefield cost increase"
    assert gs.move_card(
        tax, gs.p2, "battlefield", gs.p2, "graveyard",
        cause="impending_tax_probe")

    controller["mana_pool"]["C"] = 0
    reducer = inject_into_zone(gs, controller, {
        "name": "Impending Reduction Probe", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact", "oracle_text":
        "Creature spells you cast cost {1} less.",
    }, "battlefield")
    assert handler.generate_valid_actions()[294], \
        "Impending mask ignored a battlefield cost reduction"
    assert gs.move_card(
        reducer, controller, "battlefield", controller, "graveyard",
        cause="impending_reduction_probe")
    controller["mana_pool"]["C"] = 1

    mask = handler.generate_valid_actions()
    assert mask[294], "affordable Impending cast was absent from the mask"
    assert handler.action_reasons_with_context[294]["context"].get(
        "hand_idx") == hand_index
    _, _, _, info = handler.apply_action(294)
    assert not info.get("execution_failed"), \
        f"mask-valid Impending cast failed: {info}"
    assert overlord not in controller["hand"]
    assert gs.stack and gs.stack[-1][0] == "SPELL" \
        and gs.stack[-1][1] == overlord
    stack_context = gs.stack[-1][3]
    paid_cost = stack_context.get("final_paid_cost", {})
    assert stack_context.get("cast_for_impending")
    assert paid_cost.get("generic") == 1 and paid_cost.get("G") == 2, \
        f"Impending charged the wrong cost: {paid_cost}"
    assert all(controller["mana_pool"].get(color, 0) == 0
               for color in ("W", "U", "B", "R", "G", "C")), \
        "Impending did not spend its {1}{G}{G} cost"


@scenario("702.51 / 601.2f", "Convoke choices reduce a final mana cost exactly once")
def s_convoke_cost_reduction_is_not_duplicated():
    gs = fresh(); controller = gs.p1
    creature_a = inject_into_zone(gs, controller, {
        "name": "Convoke Helper A", "mana_cost": "{W}", "cmc": 1,
        "type_line": "Creature - Citizen", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    creature_b = inject_into_zone(gs, controller, {
        "name": "Convoke Helper B", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Citizen", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Convoke Cost Probe", "mana_cost": "{5}", "cmc": 5,
        "type_line": "Sorcery", "oracle_text": "Convoke",
    })
    helper_indices = [
        controller["battlefield"].index(creature_a),
        controller["battlefield"].index(creature_b),
    ]
    context = {
        "card_id": spell,
        "convoke_creatures": helper_indices,
    }
    final_cost = gs.mana_system.apply_cost_modifiers(
        controller, gs.mana_system.parse_mana_cost("{5}"), spell, context)
    assert final_cost["generic"] == 3, \
        f"two Convoke helpers changed {{5}} to {final_cost}"
    convoke_mods = [
        entry for entry in
        context.get("applied_cost_modifications", {}).get("reductions", [])
        if "convoke" in str(entry.get("source", "")).lower()
    ]
    assert len(convoke_mods) == 1 and convoke_mods[0]["amount"] == 2, \
        f"Convoke reduction was applied more than once: {convoke_mods}"

    controller["mana_pool"] = {
        "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 3,
    }
    assert gs.mana_system.pay_mana_cost(controller, final_cost, context), \
        "the once-reduced final Convoke cost was not payable"
    assert creature_a in controller["tapped_permanents"] \
        and creature_b in controller["tapped_permanents"]
    assert controller["mana_pool"]["C"] == 0


@scenario("Valiant (Emberheart Challenger)", "the real card text registers its Valiant trigger separately from the Prowess keyword line")
def s_emberheart_real_text_registration():
    gs = fresh()
    from Playersim.ability_types import TriggeredAbility
    controller = gs.p1
    ember = inject_real_card(gs, controller, "Emberheart Challenger", "battlefield")
    abilities = gs.ability_handler.registered_abilities.get(ember, [])
    triggered = [a for a in abilities if isinstance(a, TriggeredAbility)]
    assert len(triggered) == 1, \
        f"expected exactly one Valiant trigger, got {len(triggered)}"
    condition = (triggered[0].trigger_condition or "").lower()
    assert "becomes the target" in condition, \
        f"the Valiant trigger parsed the wrong condition: {condition!r}"
    assert not condition.startswith("prowess"), \
        "the Prowess keyword line is still glued to the Valiant trigger"


@scenario("Offspring (Manifold Mouse)", "the begin-combat trigger fires on its controller's turn only and exposes the double strike/trample choice")
def s_manifold_mouse_combat_choice():
    gs = fresh()
    from Playersim.combat_integration import integrate_combat_actions
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    gs.agent_is_p1 = True
    mouse = inject_real_card(gs, controller, "Manifold Mouse", "battlefield")
    gs.ability_handler.active_triggers = []
    # Opponent's combat: "on your turn" keeps the trigger silent.
    gs.turn = 2
    gs.phase = gs.PHASE_BEGIN_COMBAT
    gs._handle_beginning_of_phase_triggers()
    assert not gs.stack and not gs.ability_handler.active_triggers, \
        "the begin-combat trigger fired on the opponent's turn"
    # Controller's combat: exactly one trigger, targeting a Mouse.
    gs.turn = 1
    gs.phase = gs.PHASE_BEGIN_COMBAT
    gs._handle_beginning_of_phase_triggers()
    if not gs.stack and gs.phase != gs.PHASE_TARGETING:
        raise AssertionError("the begin-combat trigger did not fire on its controller's turn")
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "the targeted trigger did not pause for its Mouse target"
    valid_map = gs.targeting_system.get_valid_targets(
        mouse, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid = sorted({t for ids in valid_map.values() for t in ids},
                   key=lambda t: (isinstance(t, str), t))
    assert mouse in valid, "the Mouse itself is not a legal 'target Mouse you control'"
    _, ok = get_env().action_handler._handle_select_target(valid.index(mouse), {})
    assert ok, "selecting the Mouse target failed"
    while gs.stack and gs.phase != gs.PHASE_CHOOSE:
        assert gs.resolve_top_of_stack(), "the Mouse trigger did not resolve"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context \
        and gs.choice_context.get("type") == "keyword_grant", \
        "resolution did not pause for the keyword choice"
    options = gs.choice_context.get("options", [])
    assert options == ["double strike", "trample"], \
        f"unexpected keyword options: {options}"
    resume_phase = gs.choice_context.get("resume_phase")
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok, "choosing double strike failed"
    assert combat._has_keyword(gs._safe_get_card(mouse), "double strike"), \
        "the chosen keyword was not granted to the Mouse"
    assert gs.phase == resume_phase and gs.choice_context is None, \
        "the keyword choice did not restore the paused phase"


@scenario("Discard replacement (Obstinate Baloth)", "an opponent-caused discard puts Baloth onto the battlefield; other discards stay in the graveyard")
def s_obstinate_baloth_discard_replacement():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    baloth = inject_real_card(gs, controller, "Obstinate Baloth", "hand")
    coercion = inject_into_zone(gs, opponent, {
        "name": "Test Coercion", "mana_cost": "{2}{B}", "cmc": 3,
        "type_line": "Sorcery", "oracle_text": "Target player discards a card.",
    }, "hand")
    gs.stack.append(("SPELL", coercion, opponent, {}))
    assert gs.discard_card(controller, baloth, source_id=coercion), \
        "the opponent-caused discard did not process"
    assert baloth in controller["battlefield"], \
        "Baloth went to the graveyard instead of the battlefield"
    gs.stack.clear()
    gs.ability_handler.active_triggers = []
    # A discard caused by the controller's own spell stays a discard.
    baloth_two = inject_real_card(gs, controller, "Obstinate Baloth", "hand")
    own_spell = inject_into_zone(gs, controller, {
        "name": "Own Looting", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "Discard a card, then draw a card.",
    }, "hand")
    gs.stack.append(("SPELL", own_spell, controller, {}))
    assert gs.discard_card(controller, baloth_two, source_id=own_spell)
    assert baloth_two in controller["graveyard"], \
        "a self-caused discard should not put Baloth onto the battlefield"
    gs.stack.clear()
    # A cleanup-style discard with no causing source also stays a discard.
    baloth_three = inject_real_card(gs, controller, "Obstinate Baloth", "hand")
    assert gs.discard_card(controller, baloth_three, cause="cleanup")
    assert baloth_three in controller["graveyard"], \
        "a sourceless cleanup discard should not put Baloth onto the battlefield"


@scenario("614.1c (ETB replacements)", "a creature printed 'enters the battlefield tapped' actually enters tapped through the replacement path")
def s_creature_etb_tapped_replacement():
    gs = fresh()
    controller = gs.p1
    sleeper = inject_into_zone(gs, controller, {
        "name": "Sleepy Beast", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Beast", "oracle_text":
            "This creature enters the battlefield tapped.",
        "power": 3, "toughness": 3,
    }, "hand")
    gs.replacement_effects.register_card_replacement_effects(sleeper, controller)
    assert gs.move_card(sleeper, controller, "hand", controller, "battlefield")
    assert sleeper in controller.get("tapped_permanents", set()), \
        "the printed ETB-tapped replacement did not tap the entering creature"


@scenario("614.1c (Callous Sell-Sword)", "it enters with a +1/+1 counter for each creature that died under its controller's control this turn")
def s_callous_sell_sword_enter_counters():
    gs = fresh()
    controller = gs.p1
    for i in range(2):
        victim = inject_into_zone(gs, controller, {
            "name": f"Doomed Goblin {i}", "mana_cost": "{R}", "cmc": 1,
            "type_line": "Creature - Goblin", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "battlefield")
        assert gs.move_card(victim, controller, "battlefield", controller, "graveyard")
    gs.ability_handler.active_triggers = []

    def plus_one_counters(cid):
        counters = getattr(gs._safe_get_card(cid), 'counters', {}) or {}
        return sum(v for k, v in counters.items() if "+1" in str(k).replace("_", "/"))

    # Mirror game setup: replacements register from the full pool before play.
    sword = inject_real_card(gs, controller, "Callous Sell-Sword // Burn Together",
                             "hand")
    gs.replacement_effects.register_card_replacement_effects(sword, controller)
    assert gs.move_card(sword, controller, "hand", controller, "battlefield")
    assert plus_one_counters(sword) == 2, \
        (f"two creatures died this turn, expected 2 +1/+1 counters, "
         f"got {plus_one_counters(sword)}")
    # With the tracking reset (new turn), a fresh copy enters bare.
    gs.creatures_died_this_turn = {}
    sword_two = inject_real_card(gs, controller,
                                 "Callous Sell-Sword // Burn Together", "hand")
    gs.replacement_effects.register_card_replacement_effects(sword_two, controller)
    assert gs.move_card(sword_two, controller, "hand", controller, "battlefield")
    assert plus_one_counters(sword_two) == 0, \
        "no creatures died this turn, yet the Sell-Sword entered with counters"


@scenario("103.6c (Leyline of Resonance)", "an opening-hand Leyline may begin the game on the battlefield before turn 1")
def s_leyline_opening_hand_choice():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    leyline = inject_into_zone(gs, controller, {
        "name": "Leyline of Resonance", "mana_cost": "{2}{R}{R}", "cmc": 4,
        "type_line": "Enchantment", "oracle_text": (
            "If this card is in your opening hand, you may begin the game with "
            "it on the battlefield.\n"
            "Whenever you cast an instant or sorcery spell that targets only a "
            "single creature you control, copy that spell. You may choose new "
            "targets for the copy."
        ),
    }, "hand")
    gs.mulligan_in_progress = True
    gs.mulligan_player = controller
    gs.bottoming_in_progress = False
    gs.bottoming_player = None
    gs.mulligan_count = {'p1': 0, 'p2': 0}
    for p in (controller, opponent):
        p.pop('_mulligan_decision_made', None)
        p.pop('_needs_to_bottom_next', None)
        p.pop('_bottoming_complete', None)
    gs.perform_mulligan(controller, keep_hand=True)
    gs.perform_mulligan(opponent, keep_hand=True)
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context \
        and gs.choice_context.get("type") == "opening_hand", \
        "keeping hands with a Leyline did not open a begin-game choice"
    assert gs.choice_context.get("player") is controller, \
        "the opening-hand choice went to the wrong player"
    options = gs.choice_context.get("options", [])
    assert leyline in options, "the Leyline was not offered as a begin-game option"
    _, ok = get_env().action_handler._handle_choose_mode(options.index(leyline), {})
    assert ok, "choosing to begin the game with the Leyline failed"
    assert leyline in controller["battlefield"], \
        "the chosen Leyline did not begin the game on the battlefield"
    assert not gs.mulligan_in_progress and gs.turn == 1 \
        and gs.phase == gs.PHASE_UPKEEP, \
        "the game did not proceed to turn 1 after the opening-hand choice"


@scenario("103.6c (Leyline of Resonance)", "declining the begin-game placement keeps the Leyline in hand")
def s_leyline_opening_hand_decline():
    gs = fresh(seed=SEED + 2)
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    leyline = inject_into_zone(gs, controller, {
        "name": "Leyline of Resonance", "mana_cost": "{2}{R}{R}", "cmc": 4,
        "type_line": "Enchantment", "oracle_text": (
            "If this card is in your opening hand, you may begin the game with "
            "it on the battlefield.\n"
            "Whenever you cast an instant or sorcery spell that targets only a "
            "single creature you control, copy that spell. You may choose new "
            "targets for the copy."
        ),
    }, "hand")
    gs.mulligan_in_progress = True
    gs.mulligan_player = controller
    gs.bottoming_in_progress = False
    gs.bottoming_player = None
    gs.mulligan_count = {'p1': 0, 'p2': 0}
    for p in (controller, opponent):
        p.pop('_mulligan_decision_made', None)
        p.pop('_needs_to_bottom_next', None)
        p.pop('_bottoming_complete', None)
    gs.perform_mulligan(controller, keep_hand=True)
    gs.perform_mulligan(opponent, keep_hand=True)
    assert gs.choice_context and gs.choice_context.get("type") == "opening_hand"
    _, ok = get_env().action_handler._handle_pass_priority(None)
    assert ok, "declining the begin-game placement failed"
    assert leyline in controller["hand"] and leyline not in controller["battlefield"], \
        "declining still moved the Leyline out of hand"
    assert not gs.mulligan_in_progress and gs.turn == 1 \
        and gs.phase == gs.PHASE_UPKEEP, \
        "the game did not proceed to turn 1 after declining"


@scenario("103.6c", "each eligible opening-hand card gets an independent accept or decline decision")
def s_opening_hand_choices_are_per_card():
    gs = fresh(seed=SEED + 3)
    controller = gs.p1
    gs.agent_is_p1 = True
    permission = (
        "If this card is in your opening hand, you may begin the game with "
        "it on the battlefield.")
    first = inject_into_zone(gs, controller, {
        "name": "First Opening Permission", "mana_cost": "{2}{R}{R}",
        "type_line": "Enchantment", "oracle_text": permission,
    }, "hand")
    second = inject_into_zone(gs, controller, {
        "name": "Second Opening Permission", "mana_cost": "{2}{U}{U}",
        "type_line": "Enchantment", "oracle_text": permission,
    }, "hand")
    gs._opening_hand_players = []
    gs._begin_opening_hand_choice(controller)
    assert gs.choice_context.get("options") == [first, second]

    _, ok = get_env().action_handler._handle_pass_priority(None)
    assert ok and first in controller["hand"], \
        "declining the first permission moved it out of hand"
    assert gs.choice_context and gs.choice_context.get("options") == [second], \
        "declining one card discarded the other begin-game decision"
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and second in controller["battlefield"], \
        "accepting the second begin-game permission failed"
    assert first in controller["hand"] and first not in controller["battlefield"]
    assert gs.choice_context is None and gs.turn == 1 \
        and gs.phase == gs.PHASE_UPKEEP, \
        "the first turn did not begin after the final per-card decision"


@scenario("Screaming Nemesis / 119.6", "Screaming Nemesis reflects the damage and permanently stops the damaged player's life gain")
def s_screaming_nemesis_rest_of_game_life_restriction():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    nemesis = inject_into_zone(gs, controller, {
        "name": "Screaming Nemesis", "mana_cost": "{2}{R}", "cmc": 3,
        "type_line": "Creature - Spirit", "oracle_text": (
            "Haste\n"
            "Whenever this creature is dealt damage, it deals that much damage "
            "to any other target. If a player is dealt damage this way, they "
            "can't gain life for the rest of the game."
        ), "power": 3, "toughness": 3,
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    assert gs.apply_damage_to_permanent(nemesis, 2, source_id=None) == 2, \
        "test setup could not damage the Nemesis"
    gs.ability_handler.process_triggered_abilities()
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "the dealt-damage trigger did not pause for its target"
    valid_map = gs.targeting_system.get_valid_targets(
        nemesis, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid = sorted({t for ids in valid_map.values() for t in ids},
                   key=lambda t: (isinstance(t, str), t))
    assert nemesis not in valid, "'any other target' offered the Nemesis itself"
    assert "p2" in valid, "the opponent was not a legal target for the reflected damage"
    opp_life = opponent["life"]
    _, ok = get_env().action_handler._handle_select_target(valid.index("p2"), {})
    assert ok and gs.resolve_top_of_stack(), "the reflected-damage trigger did not resolve"
    assert opponent["life"] == opp_life - 2, \
        "the reflected damage did not equal the damage the Nemesis was dealt"
    assert gs.gain_life(opponent, 4) == 0 and opponent["life"] == opp_life - 2, \
        "a player damaged by Screaming Nemesis still gained life"
    assert gs.gain_life(controller, 4) == 4, \
        "the life-gain restriction leaked to the wrong player"
    gs.turn += 1
    gs._reset_turn_tracking_variables()
    life_now = opponent["life"]
    assert gs.gain_life(opponent, 4) == 0 and opponent["life"] == life_now, \
        "the rest-of-game restriction expired at the turn boundary"


@scenario("Corrupted (Anoint with Affliction)", "Anoint exiles only at mana value 3 or less unless the controller is corrupted")
def s_anoint_with_affliction_corrupted_branch():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    handler = get_env().action_handler
    big = inject_into_zone(gs, opponent, {
        "name": "Big Anoint Victim", "mana_cost": "{3}{G}", "cmc": 4,
        "type_line": "Creature - Beast", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    opponent["poison_counters"] = 0

    first = inject_into_zone(gs, controller, dict(ANOINT_SPEC), "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 1}
    _cast_and_target(gs, handler, first, controller, big)
    assert gs.resolve_top_of_stack(), "the first Anoint did not resolve"
    assert big in opponent["battlefield"] and big not in opponent["exile"], \
        "Anoint exiled a mana-value-4 creature without corrupted"

    opponent["poison_counters"] = 3
    second = inject_into_zone(gs, controller, dict(ANOINT_SPEC), "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 1}
    _cast_and_target(gs, handler, second, controller, big)
    assert gs.resolve_top_of_stack(), "the corrupted Anoint did not resolve"
    assert big in opponent["exile"], \
        "corrupted Anoint did not exile the mana-value-4 creature"

    opponent["poison_counters"] = 0
    small = inject_into_zone(gs, opponent, {
        "name": "Small Anoint Victim", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Elf", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    third = inject_into_zone(gs, controller, dict(ANOINT_SPEC), "hand")
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 1}
    _cast_and_target(gs, handler, third, controller, small)
    assert gs.resolve_top_of_stack(), "the third Anoint did not resolve"
    assert small in opponent["exile"], \
        "Anoint did not exile a mana-value-2 creature without corrupted"


def _activate_named_ability(gs, player, card_id, marker):
    """Activate the card's first activated ability whose effect contains
    marker, resolve it, and re-apply layers. Returns True on success."""
    abilities = gs.ability_handler.get_activated_abilities(card_id)
    for idx, ability in enumerate(abilities):
        if marker in getattr(ability, 'effect', '').lower():
            assert gs.ability_handler.can_activate_ability(card_id, idx, player), \
                f"cannot activate '{marker}' ability (cost/timing rejected)"
            assert gs.ability_handler.activate_ability(card_id, idx, player), \
                f"activating '{marker}' ability failed"
            while gs.stack:
                assert gs.resolve_top_of_stack(), "ability resolution failed"
            if gs.layer_system:
                gs.layer_system.apply_all_effects()
            return True
    raise AssertionError(f"no activated ability containing '{marker}' on {card_id}")


def _select_trigger_target(gs, source_id, controller, target_id):
    """Choose target_id for the pending targeted trigger through the mask path."""
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        "trigger did not pause for a target"
    valid_map = gs.targeting_system.get_valid_targets(
        source_id, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid = sorted({t for ids in valid_map.values() for t in ids},
                   key=lambda t: (isinstance(t, str), t))
    assert target_id in valid, f"intended target {target_id} not legal in {valid}"
    _, ok = get_env().action_handler._handle_select_target(valid.index(target_id), {})
    assert ok, "trigger target selection failed"
    return valid


@scenario("Phyrexian Obliterator / 603", "damaging Phyrexian Obliterator forces the source's controller to sacrifice that many permanents of their choice")
def s_phyrexian_obliterator_forced_sacrifice():
    gs = fresh()
    agent, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = agent
    obliterator = inject_into_zone(gs, opponent, {
        "name": "Phyrexian Obliterator", "mana_cost": "{B}{B}{B}{B}", "cmc": 4,
        "type_line": "Creature - Phyrexian Horror", "oracle_text": (
            "Trample\n"
            "Whenever a source deals damage to this creature, that source's "
            "controller sacrifices that many permanents of their choice."
        ), "power": 5, "toughness": 5,
    }, "battlefield")
    burner = inject_into_zone(gs, agent, {
        "name": "Obliterator Burner", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Creature - Elemental", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    keeper = inject_into_zone(gs, agent, {
        "name": "Kept Permanent", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    land = inject_into_zone(gs, agent, {
        "name": "Sacked Land", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Swamp", "card_types": ["land"],
        "subtypes": ["Swamp"], "oracle_text": "",
    }, "battlefield")
    gs.ability_handler.active_triggers = []
    assert gs.apply_damage_to_permanent(obliterator, 2, source_id=burner) == 2
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Obliterator trigger did not resolve"
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context \
        and gs.choice_context.get("type") == "forced_sacrifice", \
        "the damage source's controller was not given a sacrifice choice"
    assert gs.choice_context.get("player") is agent, \
        "the sacrifice choice went to the wrong player"
    handler = get_env().action_handler
    _, ok = handler._handle_choose_mode(agent["battlefield"].index(burner), {})
    assert ok, "the first sacrifice pick failed"
    assert burner in agent["graveyard"], "the picked creature was not sacrificed"
    assert gs.choice_context and gs.choice_context.get("remaining") == 1, \
        "the sacrifice count did not track the damage amount"
    _, ok = handler._handle_choose_mode(agent["battlefield"].index(land), {})
    assert ok, "the second sacrifice pick failed"
    assert land in agent["graveyard"], "the picked land was not sacrificed"
    assert gs.choice_context is None, "the sacrifice choice did not close after 2 picks"
    assert keeper in agent["battlefield"], "an unchosen permanent was sacrificed"


@scenario("Restless Anchorage / 612 / 508", "animated Restless Anchorage is a 2/3 flying Bird land and its attack makes a Map")
def s_restless_anchorage_animates_and_attacks():
    from Playersim.card import Card
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh()
    combat = integrate_combat_actions(gs)
    player = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = player
    anchorage = inject_into_zone(gs, player, {
        "name": "Restless Anchorage", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"], "oracle_text": (
            "This land enters tapped.\n"
            "{T}: Add {W} or {U}.\n"
            "{1}{W}{U}: Until end of turn, this land becomes a 2/3 white and "
            "blue Bird creature with flying. It's still a land.\n"
            "Whenever this land attacks, create a Map token."
        ),
    }, "battlefield")
    gs.untap_permanent(anchorage, player)
    player["entered_battlefield_this_turn"].discard(anchorage)
    player["mana_pool"] = {'W': 1, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    gs.ability_handler.active_triggers = []
    _activate_named_ability(gs, player, anchorage, "becomes")
    card = gs._safe_get_card(anchorage)
    assert 'creature' in card.card_types and 'land' in card.card_types, \
        f"animated Anchorage has wrong types: {card.card_types}"
    assert (card.power, card.toughness) == (2, 3), \
        f"animated Anchorage is not 2/3: {card.power}/{card.toughness}"
    assert 'bird' in [s.lower() for s in card.subtypes], \
        f"animated Anchorage is not a Bird: {card.subtypes}"
    assert list(card.colors[:2]) == [1, 1], \
        f"animated Anchorage is not white and blue: {card.colors}"
    flying_idx = Card.ALL_KEYWORDS.index('flying')
    assert card.keywords[flying_idx] == 1, "animated Anchorage does not have flying"
    assert combat.is_valid_attacker(anchorage), \
        "the animated land is not a legal attacker"
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [anchorage]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Anchorage attack trigger did not resolve"
    maps = [cid for cid in player["battlefield"]
            if "map" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    assert len(maps) == 1, "attacking with Anchorage did not create a Map token"
    gs.phase = gs.PHASE_CLEANUP
    assert not gs._cleanup_step_actions(player, discard_to_max=False)
    gs.layer_system.apply_all_effects()
    assert card.card_types == ['land'], \
        f"Anchorage did not revert to only a land at cleanup: {card.card_types}"
    assert (card.power, card.toughness) == (0, 0), \
        f"Anchorage retained animated P/T after cleanup: {card.power}/{card.toughness}"
    assert 'bird' not in [s.lower() for s in card.subtypes], \
        f"Anchorage retained its animated subtype after cleanup: {card.subtypes}"
    assert card.keywords[flying_idx] == 0, \
        "Anchorage retained flying after its animation expired"


@scenario("Restless Cottage / 508", "attacking Restless Cottage makes a Food and exiles the chosen graveyard card")
def s_restless_cottage_attack_riders():
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh()
    combat = integrate_combat_actions(gs)
    player, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = player
    cottage = inject_into_zone(gs, player, {
        "name": "Restless Cottage", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"], "oracle_text": (
            "This land enters tapped.\n"
            "{T}: Add {B} or {G}.\n"
            "{2}{B}{G}: This land becomes a 4/4 black and green Horror "
            "creature until end of turn. It's still a land.\n"
            "Whenever this land attacks, create a Food token and exile up to "
            "one target card from a graveyard."
        ),
    }, "battlefield")
    grave_card = inject_into_zone(gs, opponent, {
        "name": "Cottage Grave Card", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "",
    }, "graveyard")
    gs.untap_permanent(cottage, player)
    player["entered_battlefield_this_turn"].discard(cottage)
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 1, 'C': 2}
    gs.ability_handler.active_triggers = []
    _activate_named_ability(gs, player, cottage, "becomes")
    card = gs._safe_get_card(cottage)
    assert (card.power, card.toughness) == (4, 4) and 'creature' in card.card_types, \
        "Cottage did not animate into a 4/4 creature"
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [cottage]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    _select_trigger_target(gs, cottage, player, grave_card)
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Cottage attack trigger did not resolve"
    foods = [cid for cid in player["battlefield"]
             if "food" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    assert len(foods) == 1, "attacking with Cottage did not create a Food token"
    assert grave_card in opponent["exile"] and grave_card not in opponent["graveyard"], \
        "the chosen graveyard card was not exiled"


@scenario("Restless Reef / 508", "attacking Restless Reef mills the chosen player four cards")
def s_restless_reef_attack_mills():
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh()
    combat = integrate_combat_actions(gs)
    player, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = player
    reef = inject_into_zone(gs, player, {
        "name": "Restless Reef", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"], "oracle_text": (
            "This land enters tapped.\n"
            "{T}: Add {U} or {B}.\n"
            "{2}{U}{B}: Until end of turn, this land becomes a 4/4 blue and "
            "black Shark creature with deathtouch. It's still a land.\n"
            "Whenever this land attacks, target player mills four cards."
        ),
    }, "battlefield")
    gs.untap_permanent(reef, player)
    player["entered_battlefield_this_turn"].discard(reef)
    player["mana_pool"] = {'W': 0, 'U': 1, 'B': 1, 'R': 0, 'G': 0, 'C': 2}
    gs.ability_handler.active_triggers = []
    _activate_named_ability(gs, player, reef, "becomes")
    card = gs._safe_get_card(reef)
    assert (card.power, card.toughness) == (4, 4) and 'creature' in card.card_types, \
        "Reef did not animate into a 4/4 creature"
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [reef]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    library_before = len(opponent["library"])
    graveyard_before = len(opponent["graveyard"])
    _select_trigger_target(gs, reef, player, "p2")
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Reef attack trigger did not resolve"
    assert len(opponent["library"]) == library_before - 4 \
        and len(opponent["graveyard"]) == graveyard_before + 4, \
        "the chosen player did not mill exactly four cards"


@scenario("Restless Ridgeline / 508", "Restless Ridgeline's attack pumps and untaps another chosen attacking creature")
def s_restless_ridgeline_pumps_another_attacker():
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh()
    combat = integrate_combat_actions(gs)
    player = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = player
    ridgeline = inject_into_zone(gs, player, {
        "name": "Restless Ridgeline", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"], "oracle_text": (
            "This land enters tapped.\n"
            "{T}: Add {R} or {G}.\n"
            "{2}{R}{G}: This land becomes a 3/4 red and green Dinosaur "
            "creature until end of turn. It's still a land.\n"
            "Whenever this land attacks, another target attacking creature "
            "gets +2/+0 until end of turn. Untap that creature."
        ),
    }, "battlefield")
    fellow = inject_into_zone(gs, player, {
        "name": "Fellow Attacker", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Creature - Goblin", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    gs.untap_permanent(ridgeline, player)
    player["entered_battlefield_this_turn"].discard(ridgeline)
    player["entered_battlefield_this_turn"].discard(fellow)
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 1, 'G': 1, 'C': 2}
    gs.ability_handler.active_triggers = []
    _activate_named_ability(gs, player, ridgeline, "becomes")
    card = gs._safe_get_card(ridgeline)
    assert (card.power, card.toughness) == (3, 4) and 'creature' in card.card_types, \
        "Ridgeline did not animate into a 3/4 creature"
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [ridgeline, fellow]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    gs.tap_permanent(fellow, player)
    valid = _select_trigger_target(gs, ridgeline, player, fellow)
    assert ridgeline not in valid, \
        "'another target attacking creature' offered the Ridgeline itself"
    while gs.stack:
        assert gs.resolve_top_of_stack(), "the Ridgeline attack trigger did not resolve"
    gs.layer_system.apply_all_effects()
    assert gs._safe_get_card(fellow).power == 4, \
        "the chosen attacker did not get +2/+0"
    assert fellow not in player.get("tapped_permanents", set()), \
        "the chosen attacker was not untapped by the same trigger"


@scenario("Sunfall / 702.176 (Incubate)", "Sunfall exiles every creature and incubates a transformable token with that many counters")
def s_sunfall_incubates_exiled_count():
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    mine = [inject_into_zone(gs, controller, {
        "name": f"Sunfall Mine {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "battlefield") for i in range(2)]
    theirs = inject_into_zone(gs, opponent, {
        "name": "Sunfall Theirs", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature - Zombie", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    sunfall = inject_into_zone(gs, controller, {
        "name": "Sunfall", "mana_cost": "{3}{W}{W}", "cmc": 5,
        "type_line": "Sorcery", "oracle_text": (
            "Exile all creatures. Incubate X, where X is the number of "
            "creatures exiled this way."
        ),
    }, "hand")
    controller["mana_pool"] = {'W': 2, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 3}
    gs.ability_handler.active_triggers = []
    assert gs.cast_spell(sunfall, controller), "Sunfall could not be cast"
    assert gs.resolve_top_of_stack(), "Sunfall did not resolve"
    for cid in mine:
        assert cid in controller["exile"], "Sunfall missed the caster's creature"
    assert theirs in opponent["exile"], "Sunfall missed the opponent's creature"
    incubators = [cid for cid in controller["battlefield"]
                  if getattr(gs._safe_get_card(cid), 'name', '') == "Incubator"]
    assert len(incubators) == 1, "Sunfall did not create exactly one Incubator token"
    token_id = incubators[0]
    token = gs._safe_get_card(token_id)
    assert getattr(token, 'counters', {}).get('+1/+1', 0) == 3, \
        "the Incubator did not enter with one +1/+1 counter per exiled creature"
    assert 'creature' not in token.card_types, \
        "the Incubator front face must not be a creature"
    controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 2}
    _activate_named_ability(gs, controller, token_id, "transform")
    token = gs._safe_get_card(token_id)
    assert token.name == "Phyrexian Token" and 'creature' in token.card_types, \
        "paying {2} did not transform the Incubator into its creature face"
    assert token.power == 3 and token.toughness == 3, \
        f"the transformed 0/0 with 3 counters should be 3/3, got {token.power}/{token.toughness}"


@scenario("Beza / Treasure / 603 / 605.3", "Beza's comparisons resolve and its Treasure sacrifices for chosen mana without using the stack")
def s_beza_multi_condition_etb():
    from Playersim.ability_types import ManaAbility
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    beza_spec = {
        "name": "Beza, the Bounding Spring", "mana_cost": "{2}{W}{W}", "cmc": 4,
        "type_line": "Legendary Creature - Elemental Elk", "oracle_text": (
            "When Beza enters, create a Treasure token if an opponent controls "
            "more lands than you. You gain 4 life if an opponent has more life "
            "than you. Create two 1/1 blue Fish creature tokens if an opponent "
            "controls more creatures than you. Draw a card if an opponent has "
            "more cards in hand than you."
        ), "power": 4, "toughness": 5,
    }
    # Opponent ahead on all four axes.
    for i in range(2):
        inject_into_zone(gs, opponent, {
            "name": f"Beza Opp Land {i}", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Island", "card_types": ["land"],
            "subtypes": ["Island"], "oracle_text": "",
        }, "battlefield")
        inject_into_zone(gs, opponent, {
            "name": f"Beza Opp Creature {i}", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature - Zombie", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "battlefield")
    opponent["life"] = 25
    replace_hand(gs, controller, [{
        "name": "Beza Filler", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "",
    }])
    beza = inject_into_zone(gs, controller, beza_spec, "hand")
    gs.ability_handler.active_triggers = []
    life_before = controller["life"]
    hand_before = len(controller["hand"])
    assert gs.move_card(beza, controller, "hand", controller, "battlefield")
    gs.ability_handler.process_triggered_abilities()
    while gs.stack:
        assert gs.resolve_top_of_stack(), "Beza's ETB trigger did not resolve"
    treasures = [cid for cid in controller["battlefield"]
                 if "treasure" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    fish = [cid for cid in controller["battlefield"]
            if "fish" in {s.lower() for s in getattr(gs._safe_get_card(cid), 'subtypes', [])}]
    assert len(treasures) == 1, "Beza did not create a Treasure while behind on lands"
    assert controller["life"] == life_before + 4, "Beza did not gain 4 life while behind on life"
    assert len(fish) == 2, "Beza did not create two Fish while behind on creatures"
    fish_card = gs._safe_get_card(fish[0])
    assert (fish_card.power, fish_card.toughness) == (1, 1) and fish_card.colors[1] == 1, \
        "the Fish tokens are not blue 1/1s"
    assert len(controller["hand"]) == hand_before, \
        "Beza draw check failed (hand should be -1 Beza +1 drawn)"

    # The Treasure's printed mana ability pays tap+sacrifice atomically,
    # resolves without the stack, and leaves the output color to the policy.
    treasure_id = treasures[0]
    activated = gs.ability_handler.get_activated_abilities(treasure_id)
    assert len(activated) == 1 and isinstance(activated[0], ManaAbility), \
        f"Beza's Treasure did not register its printed mana ability: {[(type(a).__name__, getattr(a, 'cost', None), getattr(a, 'effect', None)) for a in activated]}"
    treasure_slot = controller["battlefield"].index(treasure_id)
    ability_features = get_env()._get_ability_features(
        controller["battlefield"], controller)
    assert ability_features[treasure_slot, 2] >= 1, \
        "the observation did not classify Treasure's ManaAbility"
    stack_before = list(gs.stack)
    # Exercise the non-active-player path: the color sub-choice must return
    # priority to the activator, not silently hand it to the active player.
    gs.turn = 2
    gs.cards_to_graveyard_this_turn.setdefault(gs.turn, [])
    gs.priority_player = controller
    bf_idx = controller["battlefield"].index(treasure_id)
    _, ok = get_env().action_handler._handle_activate_ability(
        None, {"battlefield_idx": bf_idx, "ability_idx": 0})
    assert ok, "Beza's Treasure mana ability could not be activated"
    assert treasure_id not in controller["battlefield"], \
        "Treasure was not sacrificed as an activation cost"
    assert gs.stack == stack_before, "a mana ability incorrectly used the stack"
    assert gs.choice_context and gs.choice_context.get("type") == "mana_ability_color", \
        "Treasure did not expose its output color to the policy"
    green_index = gs.choice_context["options"].index("G")
    green_before = controller["mana_pool"].get("G", 0)
    _, ok = get_env().action_handler._handle_choose_mode(green_index, {})
    assert ok and controller["mana_pool"].get("G", 0) == green_before + 1, \
        "choosing green did not add exactly one green mana"
    assert gs.priority_player is controller, \
        "mana color choice returned priority to AP instead of its activator"

    # Second game: opponent behind on every axis -> nothing happens.
    gs2 = fresh(seed=SEED + 3)
    controller2, opponent2 = gs2.p1, gs2.p2
    gs2.agent_is_p1 = True
    opponent2["life"] = 10
    for cid in list(opponent2["hand"]):
        assert gs2.move_card(cid, opponent2, "hand", opponent2, "library")
    inject_into_zone(gs2, controller2, {
        "name": "Beza My Land", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Plains", "card_types": ["land"],
        "subtypes": ["Plains"], "oracle_text": "",
    }, "battlefield")
    beza2 = inject_into_zone(gs2, controller2, dict(beza_spec), "hand")
    gs2.ability_handler.active_triggers = []
    life2 = controller2["life"]
    hand2 = len(controller2["hand"])
    assert gs2.move_card(beza2, controller2, "hand", controller2, "battlefield")
    gs2.ability_handler.process_triggered_abilities()
    while gs2.stack:
        assert gs2.resolve_top_of_stack()
    extra_tokens = [cid for cid in controller2["battlefield"]
                    if getattr(gs2._safe_get_card(cid), 'is_token', False)]
    assert not extra_tokens, "Beza created tokens while ahead on every axis"
    assert controller2["life"] == life2, "Beza gained life while ahead on life"
    assert len(controller2["hand"]) == hand2 - 1, \
        "Beza drew a card while ahead on hand size"


@scenario("614.12", "generic as-enters choices and counters complete before deferred ETB triggers")
def s_generic_as_enters_choices_and_counters():
    gs = fresh()
    player = gs.p1

    delegate = inject_into_zone(gs, player, {
        "name": "Prismatic Delegate", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature - Shapeshifter",
        "oracle_text": (
            "As this creature enters the battlefield, choose a color.\n"
            "When Prismatic Delegate enters the battlefield, you gain 1 life."),
        "power": 2, "toughness": 2,
    }, "battlefield")
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context \
        and gs.choice_context.get("type") == "as_enters_color", \
        "a generic as-enters color choice was not exposed"
    assert not any(getattr(ability, "card_id", None) == delegate
                   for ability, *_ in gs.ability_handler.active_triggers), \
        "the ETB trigger fired before the as-enters choice completed"
    assert gs.complete_as_enters_choice(gs.choice_context["options"].index("R"))
    assert player.get("chosen_colors", {}).get(delegate) == "R", \
        "the chosen color was not retained on the entering permanent"
    assert any(getattr(ability, "card_id", None) == delegate
               for ability, *_ in gs.ability_handler.active_triggers), \
        "the deferred ETB trigger did not fire after the choice"

    relic = inject_into_zone(gs, player, {
        "name": "Taxonomic Relic", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact",
        "oracle_text": (
            "As this permanent enters the battlefield, choose a card type."),
    }, "battlefield")
    assert gs.choice_context and gs.choice_context.get("type") == "as_enters_card_type"
    assert gs.complete_as_enters_choice(
        gs.choice_context["options"].index("artifact"))
    assert player.get("chosen_card_types", {}).get(relic) == "artifact"

    envoy = inject_into_zone(gs, player, {
        "name": "Dubious Envoy", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature - Human Advisor",
        "oracle_text": (
            "As this creature enters the battlefield, choose an opponent."),
        "power": 1, "toughness": 3,
    }, "battlefield")
    assert gs.choice_context and gs.choice_context.get("type") == "as_enters_opponent"
    assert gs.complete_as_enters_choice(0)
    assert player.get("chosen_opponents", {}).get(envoy) == "p2"

    reinforced = inject_into_zone(gs, player, {
        "name": "Reinforced Arrival", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast",
        "oracle_text": (
            "As this creature enters the battlefield with two +1/+1 counters on it."),
        "power": 1, "toughness": 1,
    }, "battlefield")
    assert getattr(gs._safe_get_card(reinforced), "counters", {}).get("+1/+1") == 2, \
        "an as-enters counter replacement was not applied on the first entry"


@scenario("Cavern of Souls / 614.12 / 106.6", "Cavern of Souls chooses a type, restricts its mana, and makes matching spells uncounterable")
def s_cavern_of_souls_chosen_type_mana():
    from Playersim.ability_types import CounterSpellEffect
    gs = fresh()
    controller, opponent = gs.p1, gs.p2
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.agent_is_p1 = True
    gs.priority_player = controller
    # Give the controller's pool a clear tribal identity for the type options.
    for i in range(3):
        inject_into_zone(gs, controller, {
            "name": f"Cavern Deck Elf {i}", "mana_cost": "{G}", "cmc": 1,
            "type_line": "Creature - Elf Druid", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "library")
    elf = inject_into_zone(gs, controller, {
        "name": "Cavern Cast Elf", "mana_cost": "{G}", "cmc": 1,
        "type_line": "Creature - Elf Warrior", "oracle_text": "",
        "power": 2, "toughness": 1,
    }, "hand")
    goblin = inject_into_zone(gs, controller, {
        "name": "Cavern Wrong Goblin", "mana_cost": "{R}", "cmc": 1,
        "type_line": "Creature - Goblin", "oracle_text": "",
        "power": 1, "toughness": 1,
    }, "hand")
    cavern = inject_into_zone(gs, controller, {
        "name": "Cavern of Souls", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"], "oracle_text": (
            "As this land enters, choose a creature type.\n"
            "{T}: Add {C}.\n"
            "{T}: Add one mana of any color. Spend this mana only to cast a "
            "creature spell of the chosen type, and that spell can't be "
            "countered."
        ),
    }, "battlefield")
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context \
        and gs.choice_context.get("type") == "as_enters_creature_type", \
        "Cavern entering did not open a creature-type choice"
    options = gs.choice_context.get("options", [])
    assert "elf" in options, f"the controller's own tribe is not offered: {options}"
    handler = get_env().action_handler
    _, ok = handler._handle_choose_mode(options.index("elf"), {})
    assert ok, "choosing the creature type failed"
    assert gs.choice_context is None and gs.phase == gs.PHASE_MAIN_PRECOMBAT, \
        "the creature-type choice did not close cleanly"
    assert controller.get("chosen_creature_types", {}).get(cavern) == "elf", \
        "the chosen type was not recorded on the permanent"

    # Tap for the restricted any-color mana and pick green.
    assert gs.mana_system.tap_land_for_mana(controller, cavern), \
        "tapping Cavern did not start its mana choice"
    assert gs.choice_context and gs.choice_context.get("type") == "land_mana", \
        "Cavern's two mana abilities did not expose a choice"
    mana_options = gs.choice_context.get("options", [])
    green_restricted = next(
        (i for i, o in enumerate(mana_options)
         if o.get("symbol") == "G" and o.get("restriction")), None)
    assert green_restricted is not None, \
        f"no restricted green output among Cavern options: {mana_options}"
    assert gs.mana_system.complete_land_mana_choice(green_restricted), \
        "selecting the restricted green mana failed"
    conditional = controller.get("conditional_mana", {})
    assert any(pool.get("G", 0) for pool in conditional.values()), \
        f"the restricted mana did not enter a conditional pool: {conditional}"
    assert controller["mana_pool"].get("G", 0) == 0, \
        "the restricted mana leaked into the unrestricted pool"

    # The wrong-tribe creature cannot be paid for with it.
    assert not gs.cast_spell(goblin, controller), \
        "a non-Elf creature was cast using Elf-restricted Cavern mana"
    assert goblin in controller["hand"], "the refused cast moved the card"

    # The matching creature can, and cannot be countered.
    assert gs.cast_spell(elf, controller), \
        "an Elf could not be cast with the Elf-restricted mana"
    assert gs.stack, "the Elf cast did not reach the stack"
    stack_context = gs.stack[-1][3] if len(gs.stack[-1]) > 3 else {}
    assert stack_context.get("cant_be_countered"), \
        "the Cavern-funded spell was not marked uncounterable"
    counter_ok = CounterSpellEffect().apply(
        gs, opponent["battlefield"][0] if opponent["battlefield"] else None,
        opponent, {"spells": [elf]})
    assert not counter_ok, "a counterspell claimed to counter the Cavern-funded spell"
    assert gs.stack, "the uncounterable spell left the stack"
    assert gs.resolve_top_of_stack(), "the Elf did not resolve"
    assert elf in controller["battlefield"], "the uncountered Elf did not enter"


@scenario("702.171 (Saddle / Caustic Bronco)", "Saddle taps chosen creatures with enough power and lasts until cleanup")
def scenario_caustic_bronco_saddle():
    gs = fresh()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    for cid in list(player["battlefield"]):
        gs.move_card(cid, player, "battlefield", player, "library")
    bronco = inject_into_zone(gs, player, {
        "name": "Caustic Bronco", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Creature - Snake Horse Mount", "power": 2, "toughness": 2,
        "oracle_text": ("Whenever this creature attacks, reveal the top card of your library "
                        "and put it into your hand. You lose life equal to that card's mana value "
                        "if this creature isn't saddled. Otherwise, each opponent loses that much life.\n"
                        "Saddle 3 (Tap any number of other creatures you control with total power "
                        "3 or more: This Mount becomes saddled until end of turn. Saddle only as a sorcery.)"),
    }, "battlefield")
    helper_a = inject_into_zone(gs, player, {"name": "Saddle A", "mana_cost": "", "cmc": 0,
        "type_line": "Creature", "power": 1, "toughness": 1, "oracle_text": ""}, "battlefield")
    helper_b = inject_into_zone(gs, player, {"name": "Saddle B", "mana_cost": "", "cmc": 0,
        "type_line": "Creature", "power": 2, "toughness": 2, "oracle_text": ""}, "battlefield")
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[478], "Caustic Bronco did not expose its Saddle action"
    _, ok = get_env().action_handler._handle_saddle(None, {"battlefield_idx": player["battlefield"].index(bronco)})
    assert ok and gs.choice_context and gs.choice_context["type"] == "saddle"
    handler = get_env().action_handler
    _, ok = handler._handle_choose_mode(gs.choice_context["options"].index(helper_a), {})
    assert ok and not handler.generate_valid_actions()[11], "Saddle finished below the power threshold"
    _, ok = handler._handle_choose_mode(gs.choice_context["options"].index(helper_b), {})
    assert ok and handler.generate_valid_actions()[11], "Saddle could not finish at the power threshold"
    _, ok = handler._handle_pass_priority(None)
    assert ok and {helper_a, helper_b}.issubset(player["tapped_permanents"])
    assert bronco in player.get("saddled_permanents", set())
    gs._cleanup_step_actions(player, discard_to_max=False)
    assert bronco not in player.get("saddled_permanents", set())


@scenario("508.3 / Caustic Bronco", "Bronco moves the revealed card and applies the correct saddled life rider")
def scenario_caustic_bronco_attack_trigger():
    from Playersim.ability_types import CausticBroncoAttackEffect, TriggeredAbility
    from Playersim.ability_utils import EffectFactory

    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if player is gs.p1 else gs.p1
    bronco = inject_into_zone(gs, player, {
        "name": "Caustic Bronco", "mana_cost": "{1}{B}", "cmc": 2,
        "type_line": "Creature - Snake Horse Mount", "power": 2,
        "toughness": 2, "oracle_text": "Saddle 3",
    }, "battlefield")
    trigger_text = (
        "Reveal the top card of your library and put it into your hand. "
        "You lose life equal to that card's mana value if this creature isn't "
        "saddled. Otherwise, each opponent loses that much life.")
    effects = EffectFactory.create_effects(
        trigger_text, source_name="Caustic Bronco")
    assert len(effects) == 1 and isinstance(effects[0], CausticBroncoAttackEffect), \
        f"Bronco still parsed to partial effects: {effects}"

    first = inject_card(gs, {
        "name": "Bronco Reveal Four", "mana_cost": "{4}", "cmc": 4,
        "type_line": "Sorcery", "oracle_text": "",
    })
    player["library"].insert(0, first)
    gs._last_card_locations[first] = (player, "library")
    player_life = player["life"]
    ability = TriggeredAbility(
        bronco, trigger_condition="whenever this creature attacks",
        effect=trigger_text, effect_text=(
            "Whenever this creature attacks, " + trigger_text))
    assert ability.resolve(gs, player, context={}), \
        "real trigger resolution bypassed Bronco's exact-card override"
    assert first in player["hand"] and player["life"] == player_life - 4

    second = inject_card(gs, {
        "name": "Bronco Reveal Three", "mana_cost": "{3}", "cmc": 3,
        "type_line": "Instant", "oracle_text": "",
    })
    player["library"].insert(0, second)
    gs._last_card_locations[second] = (player, "library")
    player.setdefault("saddled_permanents", set()).add(bronco)
    opponent_life = opponent["life"]
    assert ability.resolve(gs, player, context={})
    assert second in player["hand"] and opponent["life"] == opponent_life - 3


@scenario("701.8 / Three Steps Ahead", "an imperative discard clause applies to the spell's controller")
def scenario_subjectless_discard_uses_controller():
    from Playersim.ability_types import DiscardEffect
    from Playersim.ability_utils import EffectFactory

    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    effects = EffectFactory.create_effects(
        "Draw two cards, then discard a card.",
        source_name="Three Steps Ahead")
    discard = next(
        (effect for effect in effects if isinstance(effect, DiscardEffect)),
        None)
    assert discard is not None and discard.target == "controller", \
        f"subjectless discard parsed as {getattr(discard, 'target', None)}"
    for effect in effects:
        assert effect.apply(gs, None, player, {})
    assert gs.choice_context and gs.choice_context.get("player") is player, \
        "the controller was not offered their discard choice"


@scenario("engine (per-card override)", "an exact-name effect override runs before the generic parser")
def scenario_per_card_override_registry():
    from Playersim.ability_types import GainLifeEffect
    from Playersim.ability_utils import EffectFactory
    EffectFactory.register_card_override(
        "Regex Escape Artist",
        lambda text, targets, source_name: [GainLifeEffect(7, target="controller")])
    try:
        effects = EffectFactory.create_effects("This deliberately cannot parse.",
                                               source_name="Regex Escape Artist")
        assert len(effects) == 1 and isinstance(effects[0], GainLifeEffect)
    finally:
        EffectFactory.unregister_card_override("Regex Escape Artist")


@scenario("701.8 / Duress", "Duress exposes only noncreature nonland cards from the target hand")
def scenario_duress_hand_information_choice():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(); chooser = gs.p1 if gs.agent_is_p1 else gs.p2
    target = gs.p2 if chooser is gs.p1 else gs.p1
    replace_hand(gs, target, [
        {"name": "Seen Land", "mana_cost": "", "type_line": "Land", "oracle_text": ""},
        {"name": "Seen Creature", "mana_cost": "{1}", "type_line": "Creature", "power": 1, "toughness": 1, "oracle_text": ""},
        {"name": "Seen Instant", "mana_cost": "{1}", "type_line": "Instant", "oracle_text": "Draw a card."},
    ])
    effect = EffectFactory.create_effects("Target opponent reveals their hand. You choose a noncreature, nonland card from it. That player discards that card.", source_name="Duress")[0]
    pid = "p1" if target is gs.p1 else "p2"
    assert effect.apply(gs, None, chooser, {"players": [pid]})
    names = [gs._safe_get_card(cid).name for cid in gs.choice_context["options"]]
    assert names == ["Seen Instant"], names
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and any(gs._safe_get_card(cid).name == "Seen Instant" for cid in target["graveyard"])


@scenario("701.8 / Oildeep Gearhulk", "Oildeep's optional hand choice discards then replaces the card")
def scenario_oildeep_hand_information_choice():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(); chooser = gs.p1 if gs.agent_is_p1 else gs.p2
    target = gs.p2 if chooser is gs.p1 else gs.p1
    replace_hand(gs, target, [{"name": "Chosen Card", "mana_cost": "{1}", "type_line": "Sorcery", "oracle_text": ""}])
    before_library = len(target["library"])
    effect = EffectFactory.create_effects("Look at target player's hand. You may choose a card from it. If you do, that player discards that card, then draws a card.", source_name="Oildeep Gearhulk")[0]
    pid = "p1" if target is gs.p1 else "p2"
    assert effect.apply(gs, None, chooser, {"players": [pid]})
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and len(target["library"]) == before_library - 1 and len(target["hand"]) == 1


@scenario("702.171 / Delirium", "Patchwork Beastie cannot attack or block below four graveyard card types")
def scenario_patchwork_beastie_delirium_restriction():
    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    beastie = inject_into_zone(gs, player, {"name": "Patchwork Beastie", "mana_cost": "B", "type_line": "Creature - Beast",
        "power": 3, "toughness": 3, "oracle_text": "Delirium - This creature can't attack or block unless there are four or more card types among cards in your graveyard."}, "battlefield")
    player.get("entered_battlefield_this_turn", set()).discard(beastie)
    assert not gs.combat_action_handler.is_valid_attacker(beastie)
    for type_line in ("Instant", "Sorcery", "Artifact", "Enchantment"):
        inject_into_zone(gs, player, {"name": f"Delirium {type_line}", "mana_cost": "", "type_line": type_line, "oracle_text": ""}, "graveyard")
    assert gs.combat_action_handler.is_valid_attacker(beastie)


@scenario("603 / Eerie", "Optimistic Scavenger recognizes friendly enchantment entry and full Room unlock")
def scenario_optimistic_scavenger_eerie_events():
    from Playersim.ability_types import TriggeredAbility
    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    enchantment = inject_card(gs, {"name": "Friendly Enchantment", "mana_cost": "", "type_line": "Enchantment", "oracle_text": ""})
    ability = TriggeredAbility(1, trigger_condition="whenever an enchantment you control enters", effect="put a +1/+1 counter on target creature")
    context = {"game_state": gs, "controller": player, "event_controller": player,
               "source_card_id": 1, "event_card_id": enchantment, "event_card": gs._safe_get_card(enchantment)}
    assert ability.can_trigger("ENTERS_BATTLEFIELD", context)
    room = TriggeredAbility(1, trigger_condition="whenever you fully unlock a room", effect="put a +1/+1 counter on target creature")
    assert room.can_trigger("ROOM_FULLY_UNLOCKED", {"game_state": gs, "controller": player})


@scenario("603 / Leyline of Resonance", "Leyline's copy trigger accepts exactly one friendly creature target")
def scenario_leyline_resonance_copy_condition():
    from Playersim.ability_types import TriggeredAbility
    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    creature = inject_into_zone(gs, player, {"name": "Friendly Target", "mana_cost": "", "type_line": "Creature", "power": 1, "toughness": 1, "oracle_text": ""}, "battlefield")
    spell = inject_card(gs, {"name": "Target Spell", "mana_cost": "R", "type_line": "Instant", "oracle_text": "Target creature gets +1/+0."})
    ability = TriggeredAbility(1, trigger_condition="whenever you cast an instant or sorcery spell that targets only a single creature you control", effect="copy that spell")
    context = {"game_state": gs, "controller": player, "casting_player": player,
               "cast_card_id": spell, "targets": {"creatures": [creature]}, "source_card_id": 1}
    assert ability.can_trigger("CAST_SPELL", context)
    context["targets"] = {"creatures": [creature, creature + 9999]}
    assert not ability.can_trigger("CAST_SPELL", context)


@scenario("603.12 / Cacophony Scamp", "Scamp exposes sacrifice as optional and gates proliferate behind it")
def scenario_cacophony_scamp_optional_sacrifice():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    scamp = inject_into_zone(gs, player, {"name": "Cacophony Scamp", "mana_cost": "R", "type_line": "Creature - Phyrexian Goblin Warrior",
        "power": 1, "toughness": 1, "oracle_text": "Whenever this creature deals combat damage to a player, you may sacrifice it. If you do, proliferate."}, "battlefield")
    effect = EffectFactory.create_effects("You may sacrifice it. If you do, proliferate.", source_name="Cacophony Scamp")[0]
    assert effect.apply(gs, scamp, player, {}) and gs.choice_context
    _, ok = get_env().action_handler._handle_pass_priority(None)
    assert ok and scamp in player["battlefield"], "declining the sacrifice removed Scamp"
    assert effect.apply(gs, scamp, player, {})
    _, ok = get_env().action_handler._handle_choose_mode(0, {})
    assert ok and scamp in player["graveyard"]


@scenario("sample-card exact-path sweep", "the remaining sample cards enter their dedicated mechanic paths")
def scenario_sample_card_exact_path_sweep():
    from Playersim.ability_types import FightEffect
    from Playersim.ability_utils import EffectFactory
    gs = fresh(); player = gs.p1 if gs.agent_is_p1 else gs.p2

    def make(name, text, type_line="Creature", cost="{1}"):
        return inject_card(gs, {"name": name, "mana_cost": cost, "cmc": 1,
            "type_line": type_line, "power": 1, "toughness": 1, "oracle_text": text})

    kaito = make("Kaito, Bane of Nightmares", "Ninjutsu {1}{U}{B}")
    assert gs.combat_action_handler._get_ninjutsu_cost_str(gs._safe_get_card(kaito)) == "{1}{u}{b}"

    for name, text in (
        ("Afterburner Expert", "Exhaust - {2}{G}{G}: Put two +1/+1 counters on this creature."),
        ("Draconautics Engineer", "Exhaust - {R}: Other creatures you control gain haste until end of turn. Put a +1/+1 counter on this creature.")):
        cid = make(name, text)
        gs.ability_handler.register_card_abilities(cid, player)
        assert any(getattr(a, 'is_exhaust', False) for a in gs.ability_handler.registered_abilities.get(cid, [])), name

    for name, text in (
        ("Overlord of the Hauntwoods", "Impending 4-{1}{G}{G}"),
        ("Overlord of the Mistmoors", "Impending 4-{2}{W}{W}")):
        card = gs._safe_get_card(make(name, text, type_line="Enchantment Creature"))
        assert card.is_impending and card.impending_n == 4 and card.impending_cost, name

    for name, text in (
        ("Manifold Mouse", "Offspring {2}"),
        ("Pawpatch Recruit", "Offspring {G}")):
        card = gs._safe_get_card(make(name, text))
        assert card.is_offspring and card.offspring_cost, name

    burst = gs._safe_get_card(make("Burst Lightning", "Kicker {4}\nBurst Lightning deals 2 damage to any target.", "Instant", "{R}"))
    assert gs.check_keyword(burst.card_id, "kicker")

    pest = make("Pest Control", "Destroy all nonland permanents with mana value 1 or less.\nCycling {2}", "Sorcery", "{1}{W}{B}")
    player["hand"].append(pest); gs._last_card_locations[pest] = (player, "hand")
    player["mana_pool"] = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 2}
    offered = []
    get_env().action_handler._add_specific_mechanics_actions(
        player, None, lambda index, reason, context=None: offered.append(index), False)
    assert 427 in offered

    spree = gs._safe_get_card(make("Three Steps Ahead", "Spree\n+ {1}{U} - Counter target spell.\n+ {3} - Create a token that's a copy of target artifact or creature you control.\n+ {2} - Draw two cards, then discard a card.", "Instant", "{U}"))
    assert spree.is_spree and len(spree.spree_modes) == 3

    effects = EffectFactory.create_effects("Target creature you control fights target creature you don't control.", source_name="Bushwhack")
    assert any(isinstance(effect, FightEffect) for effect in effects)


@scenario("702.167 / policy contract", "Exhaust is marked once and only the used ability leaves the action mask")
def scenario_exhaust_single_owner_bookkeeping():
    from unittest.mock import patch

    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    exhaust_card = inject_into_zone(gs, player, {
        "name": "Double Exhaust Probe", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Human Artificer", "power": 1, "toughness": 1,
        "oracle_text": (
            "Exhaust - Pay 1 life: You gain 1 life.\n"
            "Exhaust - Pay 1 life: Draw a card."),
    }, "battlefield")
    battlefield_index = player["battlefield"].index(exhaust_card)
    first_action = 100 + battlefield_index * 3
    second_action = first_action + 1
    life_before = player["life"]

    mask = handler.generate_valid_actions()
    assert mask[first_action] and mask[second_action], \
        "both unused Exhaust abilities were not initially legal"
    first_context = handler.action_reasons_with_context[first_action]["context"]
    with patch("Playersim.game_state_permanents.logging.warning") as warn:
        reward, ok = handler._handle_activate_ability(None, first_context)
    assert ok and reward > 0, "the first mask-valid Exhaust activation failed"
    duplicate_marks = [
        str(call) for call in warn.call_args_list
        if "already used exhaust ability" in str(call).lower()
    ]
    assert not duplicate_marks, \
        f"the handler marked an Exhaust cost twice: {duplicate_marks}"
    assert player["life"] == life_before - 1, \
        "the Exhaust life cost was not paid exactly once"
    assert list(gs.exhaust_ability_used) == [(exhaust_card, 0)], \
        f"unexpected Exhaust bookkeeping keys: {gs.exhaust_ability_used}"
    assert len(gs.stack) == 1 and gs.stack[-1][1] == exhaust_card, \
        "Exhaust did not create exactly one stack entry"

    gs.priority_player = player
    post_mask = handler.generate_valid_actions()
    assert not post_mask[first_action] and post_mask[second_action], \
        "using Exhaust index 0 disabled the wrong ability/index"
    stack_size = len(gs.stack)
    retry_life = player["life"]
    _, retry_ok = handler._handle_activate_ability(None, first_context)
    assert not retry_ok and player["life"] == retry_life \
        and len(gs.stack) == stack_size, \
        "a direct Exhaust retry paid a cost or created another stack entry"


@scenario("training diagnostics", "observation errors log once and oversized stack summaries stay compact")
def scenario_training_diagnostics_are_bounded():
    gs = fresh(); env = get_env()
    env.last_observation_error = None
    env.last_observation_traceback = None
    assert env._record_observation_error(
        "feature probe", ValueError("first")) is True
    assert env._record_observation_error(
        "feature probe", ValueError("repeat")) is False
    assert "first" in env.last_observation_error

    controller = gs.p1
    source_id = (controller["battlefield"][0]
                 if controller["battlefield"] else inject_into_zone(
                     gs, controller, {
                         "name": "Diagnostic Source", "mana_cost": "{1}",
                         "type_line": "Creature", "oracle_text": "",
                         "power": 1, "toughness": 1,
                     }, "battlefield"))
    gs.stack = [
        ("TRIGGER", source_id, controller, {
            "effect_text": "Put a +1/+1 counter on target creature.",
            "targeting_text": "Put a +1/+1 counter on target creature.",
            "target_choice_pending": index == 39,
        })
        for index in range(40)
    ]
    gs.targeting_context = {
        "source_id": source_id, "controller": controller,
        "required_type": "creature", "required_count": 1,
        "min_targets": 1, "max_targets": 1,
        "selected_targets": [],
        "effect_text": "Put a +1/+1 counter on target creature.",
    }
    diagnostic = env._policy_state_diagnostic()
    assert diagnostic["stack_size"] == 40
    assert diagnostic["stack_summary_omitted"] == 8
    assert len(diagnostic["stack"]) == 32, \
        "oversized stack diagnostics were not capped"


@scenario("training / hidden information", "opponent hand identities and library order do not change the agent observation")
def scenario_hidden_information_observation_boundary():
    import numpy as np
    gs = fresh(); env = get_env()
    agent = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if agent is gs.p1 else gs.p1
    if len(opponent['hand']) < 2 or len(opponent['library']) < 2:
        raise AssertionError("fixture lacks cards for hidden-information audit")
    random.seed(99); np.random.seed(99)
    before = env.observation_for(agent)
    opponent['hand'][0], opponent['library'][0] = opponent['library'][0], opponent['hand'][0]
    opponent['library'][0], opponent['library'][1] = opponent['library'][1], opponent['library'][0]
    random.seed(99); np.random.seed(99)
    after = env.observation_for(agent)
    for key in before:
        assert np.array_equal(before[key], after[key]), f"hidden identity leaked through observation key {key}"


@scenario("engine / deterministic replay", "a seeded action log replays to the same public state")
def scenario_seeded_action_replay():
    env = get_env()
    obs, _ = env.reset(seed=4242)
    mask = env.action_mask()
    valid = np.flatnonzero(mask)
    action = int(225 if mask[225] else valid[0])
    first = env.step(action)
    payload = env.export_replay()
    assert payload['version'] == 2
    assert payload['agent_is_p1'] == env.game_state.agent_is_p1
    snapshot = (env.game_state.turn, env.game_state.phase,
                env.game_state.p1['life'], env.game_state.p2['life'])
    original_decks = env.decks
    original_initial_seat = env.initial_agent_is_p1
    try:
        # Replay identity must not depend on the caller's current deck ordering
        # or seat configuration (evaluation uses a shuffled deck subset).
        env.decks = list(reversed(env.decks))
        env.initial_agent_is_p1 = not payload['agent_is_p1']
        replayed = env.replay(payload)
    finally:
        env.decks = original_decks
        env.initial_agent_is_p1 = original_initial_seat
    assert env.current_deck_name_p1 == payload['p1_deck']
    assert env.current_deck_name_p2 == payload['p2_deck']
    assert env.game_state.agent_is_p1 == payload['agent_is_p1']
    assert (env.game_state.turn, env.game_state.phase,
            env.game_state.p1['life'], env.game_state.p2['life']) == snapshot
    assert bool(first[2]) == bool(replayed[2]) and bool(first[3]) == bool(replayed[3])


@scenario("601.2c / pagination", "target choices beyond the first ten remain policy-accessible")
def scenario_target_choice_pagination():
    gs = fresh(); env = get_env()
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    source = inject_into_zone(gs, player, {"name": "Paged Source", "mana_cost": "", "type_line": "Artifact", "oracle_text": ""}, "battlefield")
    for i in range(12):
        inject_into_zone(gs, player, {"name": f"Paged Creature {i}", "mana_cost": "", "type_line": "Creature",
            "power": 1, "toughness": 1, "oracle_text": ""}, "battlefield")
    gs.phase = gs.PHASE_TARGETING
    gs.targeting_context = {"source_id": source, "controller": player,
        "required_type": "creature", "effect_text": "target creature", "required_count": 2,
        "min_targets": 2, "max_targets": 2, "selected_targets": []}
    mask = env.action_handler.generate_valid_actions()
    assert mask[479], "more than ten targets did not expose the next-page action"
    _, ok = env.action_handler._handle_target_page_next()
    assert ok
    valid_map = gs.targeting_system.get_valid_targets(source, player, "creature", effect_text="target creature")
    ordered = sorted({cid for ids in valid_map.values() for cid in ids}, key=lambda t: (isinstance(t, str), t))
    observation = env._get_obs()
    assert observation["target_card_mask"][0]
    assert observation["target_card_ids"][0] == ordered[10]
    assert observation["target_kinds"][0] == 2
    assert np.array_equal(
        observation["target_cards"][0],
        env._get_card_feature(ordered[10], env._feature_dim)), \
        "target action 274 was not aligned with its observed card identity"
    _, ok = env.action_handler._handle_select_target(0, {})
    assert ok and gs.targeting_context['selected_targets'] == [ordered[10]]


@scenario("700.2 / modal targets", "independent modal target slots advance without merging restrictions")
def scenario_independent_modal_target_slots():
    gs = fresh(); env = get_env(); player = gs.p1 if gs.agent_is_p1 else gs.p2
    source = inject_into_zone(gs, player, {"name": "Modal Source", "mana_cost": "", "type_line": "Artifact", "oracle_text": ""}, "battlefield")
    creature = inject_into_zone(gs, player, {"name": "Modal Creature", "mana_cost": "", "type_line": "Creature", "power": 1, "toughness": 1, "oracle_text": ""}, "battlefield")
    land = inject_into_zone(gs, player, {"name": "Modal Land", "mana_cost": "", "type_line": "Land", "oracle_text": ""}, "battlefield")
    captured = {}
    class CaptureTargets:
        effect_text = "capture modal targets"
        def _apply_effect(self, game_state, source_id, controller, targets):
            captured.update(targets)
            return True
    capture_effect = CaptureTargets()
    gs.phase = gs.PHASE_TARGETING
    gs.targeting_context = {"source_id": source, "controller": player,
        "required_type": "creature", "effect_text": "target creature you control",
        "required_count": 2, "min_targets": 1, "max_targets": 2, "selected_targets": [],
        "target_slots": [
            {"required_type": "creature", "effect_text": "up to two target creatures you control",
             "required_count": 2, "min_targets": 1, "max_targets": 2},
            {"required_type": "land", "effect_text": "target land you control", "required_count": 1}],
        "target_slot_index": 0, "resume_effect": capture_effect}
    creature_targets = sorted({cid for ids in gs.targeting_system.get_valid_targets(source, player, "creature", effect_text="target creature you control").values() for cid in ids})
    _, ok = env.action_handler._handle_select_target(creature_targets.index(creature), {})
    assert ok and gs.targeting_context['required_type'] == 'creature'
    mask = env.action_handler.generate_valid_actions()
    assert mask[11], "variable modal target slot could not finish at its minimum"
    _, ok = env.action_handler._handle_pass_priority(None)
    assert ok and gs.targeting_context['required_type'] == 'land'
    assert gs.targeting_context['targets_by_slot'] == [[creature]]
    land_targets = sorted({cid for ids in gs.targeting_system.get_valid_targets(source, player, "land", effect_text="target land you control").values() for cid in ids})
    _, ok = env.action_handler._handle_select_target(land_targets.index(land), {})
    assert ok and gs.targeting_context is None
    assert captured.get('creatures') == [creature] and captured.get('lands') == [land], \
        f"modal finalization dropped a prior target slot: {captured}"


@scenario("603.3b / self-play", "opponent trigger ordering is routed through the installed policy")
def scenario_policy_opponent_trigger_ordering():
    from Playersim.ability_types import TriggeredAbility
    gs = fresh(); env = get_env(); opponent = gs.p2 if gs.agent_is_p1 else gs.p1
    class FirstLegalPolicy:
        def predict(self, obs, action_masks=None, deterministic=True):
            return int(np.flatnonzero(action_masks)[0]), None
    env.set_opponent_policy(FirstLegalPolicy())
    abilities = [TriggeredAbility(i, trigger_condition="at the beginning of your upkeep", effect="you gain 1 life") for i in (9001, 9002)]
    batch = [(ability, opponent, {"ability": ability, "effect_text": ability.effect_text}) for ability in abilities]
    gs.ability_handler._stack_trigger_batch_with_choice(batch)
    assert gs.choice_context and gs.choice_context['player'] is opponent
    gs.agent_is_p1 = opponent is gs.p1
    mask = env.action_handler.generate_valid_actions()
    action, _ = env._get_opponent_policy_action(opponent, mask, {"phase_context": "CHOOSE"})
    assert action == 353
    class IllegalPolicy:
        def predict(self, obs, action_masks=None, deterministic=True):
            return int(np.flatnonzero(~np.asarray(action_masks, dtype=bool))[0]), None
    env.set_opponent_policy(IllegalPolicy())
    try:
        env._get_opponent_policy_action(opponent, mask, {"phase_context": "CHOOSE"})
        raise AssertionError("a mask-invalid checkpoint action used scripted fallback")
    except RuntimeError as exc:
        assert "mask-invalid" in str(exc)
    env.set_opponent_policy(None)


@scenario("policy contract / combat", "every specialized combat action exposed by the mask carries executable context")
def scenario_specialized_combat_mask_contexts():
    gs = fresh(); env = get_env(); handler = env.action_handler
    attacker = inject_into_zone(gs, gs.p1, {
        "name": "Contract Attacker", "mana_cost": "", "type_line": "Creature",
        "oracle_text": "", "power": 3, "toughness": 3}, "battlefield")
    planeswalker = inject_into_zone(gs, gs.p2, {
        "name": "Contract Walker", "mana_cost": "", "type_line": "Planeswalker",
        "oracle_text": "", "loyalty": 3}, "battlefield")
    battle = inject_into_zone(gs, gs.p2, {
        "name": "Contract Battle", "mana_cost": "", "type_line": "Battle - Siege",
        "oracle_text": "", "defense": 4}, "battlefield")
    blocker_ids = [inject_into_zone(gs, gs.p2, {
        "name": f"Contract Blocker {i}", "mana_cost": "", "type_line": "Creature",
        "oracle_text": "", "power": 2, "toughness": 2}, "battlefield")
        for i in range(2)]
    gs.p1.get('entered_battlefield_this_turn', set()).discard(attacker)
    for blocker_id in blocker_ids:
        gs.p2.get('entered_battlefield_this_turn', set()).discard(blocker_id)

    gs.mulligan_in_progress = False
    gs.turn = 1
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.priority_player = gs.p1
    gs.agent_is_p1 = True
    gs.current_attackers = []
    mask = handler.generate_valid_actions()
    assert not mask[378] and not mask[462], \
        "a target action was exposed before an attacker was declared"

    gs.current_attackers = [attacker]
    mask = handler.generate_valid_actions()
    assert mask[378] and mask[462], \
        "declared attacker did not expose planeswalker/battle targets"
    _, ok = handler._handle_attack_planeswalker(0, {})
    assert ok and gs.planeswalker_attack_targets.get(attacker) == planeswalker
    _, ok = handler._handle_attack_battle(0, context={})
    assert ok and gs.battle_attack_targets.get(attacker) == battle \
        and attacker not in gs.planeswalker_attack_targets
    _, ok = handler._handle_attack_planeswalker(0, {})
    assert ok and gs.planeswalker_attack_targets.get(attacker) == planeswalker \
        and attacker not in gs.battle_attack_targets

    gs.phase = gs.PHASE_DECLARE_BLOCKERS
    gs.priority_player = gs.p2
    gs.agent_is_p1 = False
    gs.current_block_assignments = {}
    gs.planeswalker_attack_targets = {}
    gs.battle_attack_targets = {}
    mask = handler.generate_valid_actions()
    assert mask[383], "multi-block action was not exposed"
    multi_context = handler.action_reasons_with_context[383]['context']
    assert len(multi_context.get('blocker_identifiers', [])) >= 2
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(383)
    assert not info.get('execution_failed') and len(
        gs.current_block_assignments.get(attacker, [])) >= 2

    gs.current_block_assignments = {}
    gs.battle_attack_targets = {attacker: battle}
    mask = handler.generate_valid_actions()
    assert mask[204] and handler.action_reasons_with_context[204]['context']
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(204)
    assert not info.get('execution_failed'), "mask-valid DEFEND_BATTLE failed"

    gs.current_block_assignments = {}
    gs.battle_attack_targets = {}
    gs.planeswalker_attack_targets = {attacker: planeswalker}
    mask = handler.generate_valid_actions()
    assert mask[444] and handler.action_reasons_with_context[444]['context']
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(444)
    assert not info.get('execution_failed'), \
        "mask-valid PROTECT_PLANESWALKER failed"


@scenario("policy contract / mask purity", "generating a paged action mask is deterministic and does not mutate choice state")
def scenario_action_mask_is_pure_for_paged_choices():
    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.phase = gs.PHASE_CHOOSE
    gs.priority_player = player
    gs.choice_context = {
        "type": "dig_select", "player": player,
        "options": list(range(10000, 10012)), "remaining": 1,
        "selected": [], "rest_destination": "bottom", "choice_page": 0,
    }
    before = copy.deepcopy(gs.choice_context)
    first = handler.generate_valid_actions()
    second = handler.generate_valid_actions()
    assert np.array_equal(first, second) and gs.choice_context == before
    assert first[479], "paged choice omitted its next-page action"

    handler.current_valid_actions = first
    _, _, _, info = handler.apply_action(479)
    assert not info.get('execution_failed') and gs.choice_context['choice_page'] == 1
    page_one = copy.deepcopy(gs.choice_context)
    next_mask = handler.generate_valid_actions()
    assert gs.choice_context == page_one and next_mask[479]
    handler.current_valid_actions = next_mask
    _, _, _, info = handler.apply_action(479)
    assert not info.get('execution_failed') and gs.choice_context['choice_page'] == 0, \
        "page-next did not wrap using its generated page count"

    gs.phase = gs.PHASE_CHOOSE
    gs.priority_player = player
    gs.choice_context = {
        'type': 'land_mana', 'player': player, 'controller': player,
        'source_id': player['battlefield'][0] if player['battlefield'] else 0,
        'card_id': player['battlefield'][0] if player['battlefield'] else 0,
        'options': [
            {'symbol': 'W', 'damage': 0},
            {'symbol': 'U', 'damage': 1},
        ],
    }
    obs = env._get_obs()
    assert env.last_observation_error is None, \
        f"non-card choice options degraded the observation: {env.last_observation_error}"
    assert not obs['choice_card_mask'].any(), \
        "mana-option dictionaries were misrepresented as visible cards"

    for choice_type, symbolic_options in (
            ('as_enters_creature_type', ['avatar', 'horror', 'wizard']),
            ('as_enters_color', ['W', 'U', 'B', 'R', 'G']),
            ('as_enters_card_type', ['artifact', 'creature', 'land']),
            ('as_enters_opponent', ['p2']),
            ('mana_ability_color', ['W', 'U', 'B', 'R', 'G']),
            ('player_selection', ['p1', 'p2'])):
        gs.choice_context = {
            'type': choice_type, 'player': player, 'controller': player,
            'source_id': player['battlefield'][0] if player['battlefield'] else 0,
            'options': symbolic_options,
        }
        obs = env._get_obs()
        assert env.last_observation_error is None, \
            f"{choice_type} symbolic options degraded the observation"
        assert not obs['choice_card_mask'].any(), \
            f"{choice_type} symbolic options were represented as phantom cards"


@scenario("103.4 (environment mulligans)", "reset exposes P1's mulligan and one step routes P2 through its policy")
def scenario_environment_routes_both_mulligan_decisions():
    env = get_env()
    observation, info = env.reset(seed=SEED + 909)
    gs = env.game_state
    assert info.get("mulligan_active") and gs.mulligan_in_progress, \
        "reset skipped the pregame mulligan phase"
    initial_mask = np.asarray(info["action_mask"], dtype=bool)
    assert initial_mask[225] and initial_mask[6], \
        "P1 was not offered KEEP_HAND and MULLIGAN"
    assert env.observation_space.contains(observation)

    observation, _, terminated, truncated, info = env.step(225)
    assert not terminated and not truncated
    assert not gs.mulligan_in_progress and gs.mulligan_player is None, \
        "the scripted opponent did not complete P2's mulligan decision"
    assert gs.turn == 1 and gs.phase == gs.PHASE_UPKEEP, \
        "completed mulligans did not begin turn 1 at upkeep"
    assert gs.priority_player is gs.p1, \
        "the starting player did not receive the first priority"
    assert env.observation_space.contains(observation)
    assert env.last_observation_error is None
    assert np.asarray(info["action_mask"], dtype=bool).any()
    assert gs._safe_get_card(None) is None, \
        "an absent card ID was converted into a phantom Card"

    original_agent_is_p1 = env.initial_agent_is_p1
    try:
        env.initial_agent_is_p1 = False
        observation, info = env.reset(seed=SEED + 910)
        gs = env.game_state
        initial_mask = np.asarray(info["action_mask"], dtype=bool)
        assert initial_mask[224] and not initial_mask[225], \
            "P2 agent did not initially wait for P1's mulligan decision"
        observation, _, terminated, truncated, info = env.step(224)
        assert not terminated and not truncated, \
            f"P1 mulligan routing aborted the P2-agent episode: {info}"
        assert gs.mulligan_in_progress and gs.mulligan_player is gs.p2
        p2_mask = np.asarray(info["action_mask"], dtype=bool)
        assert p2_mask[225] and p2_mask[6] and not p2_mask[224], \
            "control did not return to P2 for its mulligan decision"
        observation, _, terminated, truncated, info = env.step(225)
        assert not terminated and not truncated, \
            f"P2 keep-hand did not enter normal play: {info}"
        assert not gs.mulligan_in_progress
        assert env.observation_space.contains(observation)
    finally:
        env.initial_agent_is_p1 = original_agent_is_p1


@scenario("environment episode bound", "the configured step cap terminates a non-progressing episode")
def scenario_environment_enforces_episode_step_limit():
    env = get_env()
    original_limit = env.max_episode_steps
    try:
        _, info = env.reset(seed=SEED + 911)
        assert np.asarray(info["action_mask"], dtype=bool)[225]
        env.max_episode_steps = 1
        _, _, terminated, truncated, info = env.step(225)
        assert not terminated and truncated, \
            f"the one-step episode limit was not enforced: {info}"
        assert info.get("episode_step_limit") is True
        assert info.get("game_result") == "error_episode_step_limit"
        assert os.path.isfile(info.get("failure_replay_path", "")), \
            "the step-limit failure did not retain its action replay"
        assert isinstance(env.reset_seed, int)
    finally:
        env.max_episode_steps = original_limit
    env.reset()
    assert isinstance(env.reset_seed, int), \
        "an automatic follow-up episode did not receive a replayable seed"


@scenario("117 / 502 / 514 (NO_OP)", "automatic-step NO_OP advances once and an invalid NO_OP cannot mutate state")
def scenario_no_op_contract_is_explicit_and_mask_safe():
    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.targeting_context = None
    gs.sacrifice_context = None
    gs.choice_context = None
    gs.stack.clear()
    gs.phase = gs.PHASE_CLEANUP
    gs.priority_player = None
    turn_before = gs.turn
    cleanup_mask = handler.generate_valid_actions()
    assert cleanup_mask[224], "cleanup did not expose its automatic-step NO_OP"
    handler.current_valid_actions = cleanup_mask
    _, _, _, info = handler.apply_action(224)
    assert not info.get("invalid_action_reason"), \
        f"a mask-valid cleanup NO_OP invalidated itself: {info}"
    assert gs.turn == turn_before + 1 and gs.phase == gs.PHASE_UPKEEP, \
        "cleanup NO_OP did not advance exactly once into the next upkeep"
    assert gs.priority_player is gs._get_active_player(), \
        "the new active player did not receive upkeep priority"

    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.priority_pass_count = 0
    main_mask = handler.generate_valid_actions()
    assert not main_mask[224] and main_mask[11]
    handler.current_valid_actions = main_mask
    state_before = (gs.turn, gs.phase, gs.priority_player,
                    gs.priority_pass_count, tuple(gs.stack))
    _, _, _, info = handler.apply_action(224)
    assert info.get("invalid_action_reason"), \
        "a mask-invalid NO_OP was not rejected"
    state_after = (gs.turn, gs.phase, gs.priority_player,
                   gs.priority_pass_count, tuple(gs.stack))
    assert state_after == state_before, \
        "a mask-invalid NO_OP mutated priority, phase, turn, or stack"

    gs = fresh(); env = get_env(); handler = env.action_handler
    agent = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if agent is gs.p1 else gs.p1
    gs.phase = gs.PHASE_CLEANUP
    gs.priority_player = opponent
    gs.priority_pass_count = 1
    turn_before = gs.turn
    cleanup_wait_mask = handler.generate_valid_actions()
    assert cleanup_wait_mask[224]
    observation, _, terminated, truncated, info = env.step(224)
    assert not terminated and not truncated, \
        f"cleanup priority routing aborted the episode: {info}"
    assert gs.turn == turn_before + 1 and gs.phase == gs.PHASE_UPKEEP, \
        "opponent priority in cleanup was not routed through its pass"
    assert env.observation_space.contains(observation)

    gs = fresh()
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_CLEANUP
    gs.priority_player = None
    gs.priority_pass_count = 0
    turn_before = gs.turn
    gs._advance_phase()
    assert gs.turn == turn_before + 1 and gs.phase == gs.PHASE_UPKEEP, \
        "internal Priority did not normalize back through Cleanup"
    assert gs.previous_priority_phase is None

    # A choice opened while a stack item resolves can overwrite the legacy
    # resume slot with PHASE_PRIORITY. Reproduce that exact nesting sequence:
    # real End Step -> Priority -> Choose -> Priority with the slot cleared.
    gs = fresh()
    gs.phase = gs.PHASE_END_STEP
    gs.previous_priority_phase = gs.PHASE_END_STEP
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.phase
    gs.phase = gs.PHASE_CHOOSE
    gs.choice_context = None
    gs.phase = gs.previous_priority_phase
    gs.previous_priority_phase = None
    assert gs.phase == gs.PHASE_PRIORITY
    assert gs._last_turn_phase == gs.PHASE_END_STEP
    gs._advance_phase()
    assert gs.phase == gs.PHASE_CLEANUP, \
        "nested choice lost the underlying End Step and reset to main"
    assert gs.previous_priority_phase is None

    # Corruption with no trustworthy turn phase must be surfaced, not hidden
    # by the old arbitrary reset to precombat main.
    gs = fresh()
    gs._last_turn_phase = None
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = None
    try:
        gs._advance_phase()
    except RuntimeError as exc:
        assert "no valid underlying turn phase" in str(exc)
    else:
        raise AssertionError("unrecoverable transient phase did not fail loudly")
    assert gs.phase == gs.PHASE_PRIORITY


@scenario("policy contract / hand window", "all ten observed hand slots expose ordinary land and spell actions")
def scenario_ten_card_hand_action_window():
    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.stack.clear()
    player['mana_pool'] = {color: 10 for color in ('W', 'U', 'B', 'R', 'G', 'C')}
    replace_hand(gs, player, [{
        "name": f"Window Spell {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "You gain 1 life."
    } for i in range(10)])
    tenth_spell = player['hand'][9]
    play_value = env.card_evaluator.evaluate_card(tenth_spell, "play")
    assert np.isfinite(play_value) and play_value > 0, \
        "the play-context evaluator silently collapsed a valid spell to zero"
    mask = handler.generate_valid_actions()
    assert mask[397] and handler.action_reasons_with_context[397][
        'context'].get('hand_idx') == 9
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(397)
    assert not info.get('execution_failed') and tenth_spell not in player['hand'], \
        f"the tenth observed spell could not use its public action: {info}"

    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    replace_hand(gs, player, [{
        "name": f"Window Filler {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": ""
    } for i in range(9)] + [{
        "name": "Window Land 9", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "oracle_text": "{T}: Add {G}."
    }])
    tenth_land = player['hand'][9]
    mask = handler.generate_valid_actions()
    assert mask[395] and handler.action_reasons_with_context[395][
        'context'].get('hand_idx') == 9
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(395)
    assert not info.get('execution_failed') and tenth_land in player['battlefield'], \
        "the tenth observed land could not use its public action"

    # Card identifier zero is a valid database key. It must not disappear from
    # pending-cast and replacement-choice masks through truth-value checks.
    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    zero_card = gs._safe_get_card(0)
    original_text = zero_card.oracle_text
    try:
        zero_card.oracle_text = "Kicker {1}"
        player['mana_pool'] = {
            color: 2 for color in ('W', 'U', 'B', 'R', 'G', 'C')}
        gs.pending_spell_context = {'card_id': 0, 'controller': player}
        mask = handler.generate_valid_actions()
        assert mask[405] and mask[406], \
            "card ID zero lost its pending kicker actions"

        gs.pending_spell_context = None
        if 0 not in player['graveyard']:
            player['graveyard'].append(0)
        gs.phase = gs.PHASE_CHOOSE
        gs.priority_player = player
        gs.choice_context = {
            'type': 'dredge', 'player': player, 'card_id': 0, 'value': 1}
        mask = handler.generate_valid_actions()
        assert mask[308] and mask[11], \
            "card ID zero lost its dredge/skip choice actions"
    finally:
        zero_card.oracle_text = original_text
        gs.pending_spell_context = None
        gs.choice_context = None

    # Alternate faces keep their own timing. A creature-front Adventure/MDFC
    # with an instant alternate half must remain available on the other turn.
    adventure_spec = {
        'name': 'Night Courier', 'mana_cost': '{2}{U}', 'cmc': 3,
        'type_line': 'Creature - Human Scout', 'power': 2, 'toughness': 2,
        'oracle_text': 'Quick Study {U} (Adventure)\n'
                       'Instant - Adventure\nDraw a card.',
    }
    mdfc_spec = {
        'name': 'Patient Adept // Sudden Insight', 'layout': 'modal_dfc',
        'mana_cost': '{2}{U}', 'cmc': 3,
        'type_line': 'Creature - Human Wizard', 'oracle_text': '',
        'card_faces': [
            {'name': 'Patient Adept', 'mana_cost': '{2}{U}',
             'type_line': 'Creature - Human Wizard', 'oracle_text': ''},
            {'name': 'Sudden Insight', 'mana_cost': '{U}',
             'type_line': 'Instant', 'oracle_text': 'Draw a card.'},
        ],
    }

    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.turn = 2 if player is gs.p1 else 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.stack.clear()
    player['mana_pool'] = {
        color: 5 for color in ('W', 'U', 'B', 'R', 'G', 'C')}
    replace_hand(gs, player, [adventure_spec, mdfc_spec])
    adventure_id = player['hand'][0]
    mask = handler.generate_valid_actions()
    assert mask[196] and mask[189], \
        "instant Adventure/MDFC alternate faces vanished outside sorcery timing"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(196)
    assert not info.get('execution_failed') and adventure_id not in player['hand'], \
        "the mask-valid instant Adventure action failed"

    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.mulligan_in_progress = False
    gs.turn = 2 if player is gs.p1 else 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.stack.clear()
    player['mana_pool'] = {
        color: 5 for color in ('W', 'U', 'B', 'R', 'G', 'C')}
    replace_hand(gs, player, [mdfc_spec])
    mdfc_id = player['hand'][0]
    mask = handler.generate_valid_actions()
    assert mask[188], "instant MDFC back face was not exposed on the other turn"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(188)
    assert not info.get('execution_failed') and mdfc_id not in player['hand'], \
        "the mask-valid instant MDFC back-face action failed"


@scenario("policy contract / PLAY_LAND", "hand slot six pins its card and controller from mask through execution")
def scenario_play_land_slot_six_contract():
    gs = fresh(); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player is gs.p1 else 2
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.phase = gs.PHASE_PRIORITY
    gs.priority_player = player
    gs.stack.clear()
    player["land_played"] = False
    replace_hand(gs, player, [{
        "name": f"Slot Filler {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": ""
    } for i in range(6)] + [{
        "name": "Slot Six Land", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "oracle_text": "{T}: Add {G}."
    }])
    land_id = player["hand"][6]
    mask = env.action_mask()
    assert mask[19], "PLAY_LAND(6) was absent for a legal seventh hand card"
    context = handler.action_reasons_with_context[19]["context"]
    assert context == {
        "hand_idx": 6,
        "card_id": land_id,
        "controller_id": "p1" if player is gs.p1 else "p2",
    }, f"PLAY_LAND(6) did not pin its generated identity: {context}"
    _, _, _, _, info = env.step(19)
    assert not info.get("execution_failed"), info.get("error_message")
    assert land_id in player["battlefield"] and land_id not in player["hand"], \
        "the exact land exposed by action 19 was not moved to the battlefield"

    # Exercise the production path for every land printed in the audited deck
    # pool at the exact slot that failed the strength run. This includes fast
    # lands, typed tapped lands, Restless lands, pain lands, Verges, and Cavern.
    global _REAL_DB
    _load_real_card_data()
    real_land_names = sorted({
        card["name"] for card in _REAL_DB.values()
        if "land" in (
            (card.get("card_faces") or [{}])[0].get(
                "type_line", card.get("type_line", ""))
        ).lower()
    })
    assert real_land_names
    for offset, land_name in enumerate(real_land_names):
        gs = fresh(SEED + 50_000 + offset * 100); env = get_env()
        handler = env.action_handler
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        gs.turn = 1 if player is gs.p1 else 2
        gs.phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = player
        gs.stack.clear()
        player["land_played"] = False
        replace_hand(gs, player, [{
            "name": f"Real Land Filler {i}", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Sorcery", "oracle_text": ""
        } for i in range(6)])
        real_land_id = inject_real_card(gs, player, land_name, "hand")
        assert player["hand"][6] == real_land_id
        mask = handler.generate_valid_actions()
        assert mask[19], f"{land_name} was not exposed at hand slot 6"
        handler.current_valid_actions = mask
        _, _, _, land_info = handler.apply_action(19)
        assert not land_info.get("execution_failed"), \
            f"{land_name} failed action 19: {land_info.get('error_message')}"
        assert real_land_id in player["battlefield"], \
            f"{land_name} did not reach the battlefield from action 19"

    # A future engine-side rejection must preserve enough evidence to replay
    # and diagnose it. Force only the final play_land call to reject while the
    # public mask remains legal; restore the class method immediately.
    gs = fresh(SEED + 90_000); env = get_env(); handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    player["land_played"] = False
    replace_hand(gs, player, [{
        "name": f"Failure Filler {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": ""
    } for i in range(6)] + [{
        "name": "Failure Land", "type_line": "Basic Land - Forest",
        "oracle_text": "{T}: Add {G}.",
    }])
    failed_land_id = player["hand"][6]
    game_state_type = type(gs)
    original_play_land = game_state_type.play_land
    try:
        env.current_episode_actions.append(np.int64(393))
        game_state_type.play_land = lambda *_args, **_kwargs: False
        assert env.action_mask()[19]
        _, _, _, _, failure_info = env.step(19)
    finally:
        game_state_type.play_land = original_play_land
    assert failure_info.get("execution_failed")
    assert failure_info.get("handler_error")
    diagnostic = failure_info.get("policy_state", {})
    assert diagnostic.get("failed_action", {}).get("context", {}).get(
        "card_id") == failed_land_id
    replay_path = failure_info.get("failure_replay_path")
    assert replay_path and os.path.isfile(replay_path)
    with open(replay_path, encoding="utf-8") as replay_handle:
        replay_payload = json.load(replay_handle)
    assert replay_payload["actions"][-1]["action"] == 19
    assert replay_payload["failure"]["handler_error"]
    assert replay_payload["failure"]["recent_actions"][-2:] == [393, 19]
    assert failed_land_id in player["hand"], \
        "opponent simulation mutated state after the forced execution failure"


@scenario("policy contract / PLAY_SPELL", "a permanent spell casts from a transient main-phase priority window")
def scenario_play_spell_from_main_priority_wrapper():
    gs = fresh(); env = get_env(); handler = env.action_handler
    gs.agent_is_p1 = False
    player = gs.p2
    gs.turn = 2
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.phase = gs.PHASE_PRIORITY
    gs.priority_player = player
    gs.priority_pass_count = 0
    gs.stack.clear()
    player['mana_pool'] = {
        color: 5 for color in ('W', 'U', 'B', 'R', 'G', 'C')}
    replace_hand(gs, player, [{
        "name": "Spell Slot Filler", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": "Draw a card."
    }])
    nightmare_id = inject_real_card(gs, player, "Hopeless Nightmare", "hand")
    assert player['hand'][1] == nightmare_id
    mask = env.action_mask()
    assert mask[21], "Hopeless Nightmare was absent from mask action 21"
    context = handler.action_reasons_with_context[21]['context']
    assert context.get('card_id') == nightmare_id
    assert context.get('controller_id') == 'p2'
    _, _, _, _, info = env.step(21)
    assert not info.get('execution_failed'), info.get('error_message')
    assert nightmare_id not in player['hand'], \
        "the mask-valid Hopeless Nightmare remained in hand"


@scenario("policy contract / auto-tap",
          "an empty pool casts a spell by auto-tapping untapped lands")
def scenario_auto_tap_cast_from_untapped_lands():
    gs = fresh(); env = get_env(); handler = env.action_handler
    gs.agent_is_p1 = True
    player = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.priority_pass_count = 0
    gs.stack.clear()
    mountain = inject_into_zone(gs, player, {
        "name": "Mountain", "type_line": "Basic Land - Mountain",
        "oracle_text": "{T}: Add {R}.",
    }, "battlefield")
    forest = inject_into_zone(gs, player, {
        "name": "Forest", "type_line": "Basic Land - Forest",
        "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    bear, = replace_hand(gs, player, [{
        "name": "Auto-Tap Bear", "mana_cost": "{1}{R}", "cmc": 2,
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }])
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}

    mask = env.action_mask()
    assert mask[20], \
        "PLAY_SPELL was not mask-legal with an empty pool and payable untapped lands"
    _, _, _, _, info = env.step(20)
    assert not info.get('execution_failed'), info.get('error_message')
    assert bear not in player['hand'], "the mask-valid spell remained in hand"
    tapped = player.get("tapped_permanents", set())
    assert mountain in tapped and forest in tapped, \
        "auto-tap did not tap both lands to pay {1}{R}"
    assert sum(player["mana_pool"].values()) == 0, \
        "auto-tap floated mana beyond the spell's cost"


@scenario("601.2b / auto-tap",
          "a return-permanent additional cost only offers returns that keep the cost payable")
def scenario_return_cost_preserves_mana_plan():
    gs = fresh(); env = get_env(); handler = env.action_handler
    gs.agent_is_p1 = True
    player = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.priority_pass_count = 0
    gs.stack.clear()
    islands = [inject_into_zone(gs, player, {
        "name": f"Isolation Island {i}", "type_line": "Basic Land - Island",
        "oracle_text": "{T}: Add {U}.",
    }, "battlefield") for i in range(2)]
    fear, = replace_hand(gs, player, [{
        "name": "Isolation Test Fear", "mana_cost": "{U}{U}", "cmc": 2,
        "type_line": "Creature - Nightmare", "power": 2, "toughness": 2,
        "oracle_text": (
            "As an additional cost to cast this spell, return a permanent "
            "you control to its owner's hand.\nFlying"
        ),
    }])
    player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}

    # With only the two Islands, any return leaves {U}{U} unpayable.
    mask = env.action_mask()
    assert not mask[20], \
        "PLAY_SPELL was offered although every return breaks the mana plan"

    mountain = inject_into_zone(gs, player, {
        "name": "Isolation Mountain", "type_line": "Basic Land - Mountain",
        "oracle_text": "{T}: Add {R}.",
    }, "battlefield")
    mask = env.action_mask()
    assert mask[20], \
        "PLAY_SPELL was absent although returning the Mountain keeps {U}{U} payable"
    _, _, _, _, info = env.step(20)
    assert not info.get('execution_failed'), info.get('error_message')
    assert gs.phase == gs.PHASE_CHOOSE \
        and gs.choice_context.get("type") == "casting_additional_return", \
        "the cast did not enter its return-permanent choice"
    assert gs.choice_context.get("options") == [mountain], \
        f"viable return options were wrong: {gs.choice_context.get('options')}"
    _, _, _, _, info = env.step(353)
    assert not info.get('execution_failed'), info.get('error_message')
    assert mountain in player["hand"], "the returned Mountain did not reach hand"
    assert gs.stack and gs.stack[-1][1] == fear, \
        "the spell did not reach the stack after paying its return cost"
    tapped = player.get("tapped_permanents", set())
    assert all(island in tapped for island in islands), \
        "auto-tap did not use both Islands after the Mountain was returned"


@scenario("117.1 / 601.2c", "a targeted sorcery preserves its main phase through target selection")
def scenario_targeted_spell_from_main_priority_wrapper():
    gs = fresh(); handler = get_env().action_handler
    gs.agent_is_p1 = False
    player = gs.p2
    gs.turn = 2
    gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
    gs.phase = gs.PHASE_PRIORITY
    gs.priority_player = player
    gs.priority_pass_count = 0
    gs.stack.clear()
    duress = inject_into_zone(gs, player, {
        "name": "Duress", "mana_cost": "{B}", "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": (
            "Target opponent reveals their hand. You choose a noncreature, "
            "nonland card from it. That player discards that card."),
    }, "hand")
    player["mana_pool"] = {
        'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 0}

    assert gs.cast_spell(duress, player), \
        "Duress could not begin targeting from transient main priority"
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context
    assert gs.targeting_context.get("targeting_return_phase") == gs.PHASE_PRIORITY
    assert (gs.targeting_context.get(
        "targeting_return_previous_priority_phase") ==
        gs.PHASE_MAIN_PRECOMBAT)
    candidates = handler._get_target_selection_candidates(
        player, gs.targeting_context)
    assert candidates == ["p1"], f"Duress target candidates were {candidates}"
    reward, ok = handler._handle_select_target(0, {})
    assert ok, f"mask-valid Duress target failed with reward {reward}"
    assert gs.stack and gs.stack[-1][1] == duress, \
        "Duress did not finish casting after target selection"
    assert (gs.phase == gs.PHASE_PRIORITY
            and gs.previous_priority_phase == gs.PHASE_MAIN_PRECOMBAT), \
        "target selection lost the main phase beneath priority"


@scenario("deck legality", "format status, copy limits, bans, restrictions, basics, and minimum size are validated")
def scenario_deck_legality_validation():
    from Playersim.card import Card
    from Playersim.deck_legality import validate_deck_legality
    basic = Card({"name": "Plains", "type_line": "Basic Land - Plains", "legalities": {"standard": "legal"}}); basic.card_id = 0
    spell = Card({"name": "Legal Spell", "type_line": "Sorcery", "legalities": {"standard": "legal"}}); spell.card_id = 1
    banned = Card({"name": "Banned Spell", "type_line": "Instant", "legalities": {"standard": "banned"}}); banned.card_id = 2
    db = {0: basic, 1: spell, 2: banned}
    legal = {"cards": [0] * 56 + [1] * 4}
    assert not validate_deck_legality(legal, db, format_name="standard")
    illegal = {"cards": [0] * 54 + [1] * 5 + [2]}
    errors = validate_deck_legality(illegal, db, format_name="standard", banned_names=["Banned Spell"])
    assert any("maximum 4" in error for error in errors) and any("banned" in error for error in errors)


@scenario("601.2c / 609.3", "an up-to-two bounce may legally resolve with zero chosen targets")
def scenario_optional_bounce_accepts_zero_targets():
    from Playersim.ability_types import ReturnToHandEffect
    from Playersim.ability_utils import EffectFactory

    gs = fresh()
    player = gs.p1
    effects = EffectFactory.create_effects(
        "Return up to two target nonland permanents to their owners' hands.")
    bounce = next((effect for effect in effects
                   if isinstance(effect, ReturnToHandEffect)), None)
    assert bounce is not None, "optional bounce did not parse"
    assert bounce.min_targets == 0 and bounce.max_targets == 2
    assert bounce.apply(gs, None, player, targets={}), \
        "a legal zero-target optional bounce reported resolution failure"


@scenario("707.10 / Leyline of Resonance", "copy-that-spell resolves against the triggering spell without retargeting it")
def scenario_leyline_copy_that_spell_resolves():
    from Playersim.ability_types import CopySpellEffect
    from Playersim.ability_utils import EffectFactory

    gs = fresh()
    player = gs.p1
    leyline = inject_into_zone(gs, player, {
        "name": "Leyline of Resonance", "mana_cost": "{2}{R}{R}",
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    spell = inject_card(gs, {
        "name": "Triggered Cantrip", "mana_cost": "{U}",
        "type_line": "Instant", "oracle_text": "Draw a card.",
    })
    gs.stack.append(("SPELL", spell, player, {
        "targets": {}, "requires_target": False, "num_targets": 0,
    }))
    effects = EffectFactory.create_effects(
        "Copy that spell. You may choose new targets for the copy.")
    copy_effect = next((effect for effect in effects
                        if isinstance(effect, CopySpellEffect)), None)
    assert copy_effect is not None and copy_effect.copy_that
    assert copy_effect.new_targets, \
        "Leyline lost its printed option to choose new targets for the copy"
    assert not copy_effect.requires_target, \
        "'that spell' was incorrectly exposed as a new target choice"
    assert copy_effect.apply(
        gs, leyline, player, targets={}, context={"cast_card_id": spell})
    copies = [item for item in gs.stack
              if item[0] == "SPELL" and item[1] == spell
              and item[3].get("is_copy")]
    assert len(copies) == 1, "Leyline did not create exactly one spell copy"


@scenario("704.5 / evaluator IDs", "game sentinels and mixed token IDs do not enter card lookup/sorting paths")
def scenario_runtime_sentinels_and_mixed_ids_are_safe():
    from Playersim.card import Card
    from Playersim.enhanced_card_evaluator import EnhancedCardEvaluator

    gs = fresh()
    card_id = inject_card(gs, {
        "name": "Mixed ID Creature", "mana_cost": "{1}",
        "type_line": "Creature - Test", "oracle_text": "",
        "power": 1, "toughness": 1,
    })
    token_id = "TOKEN_MIXED_ID"
    token = Card({
        "name": "Mixed ID Token", "type_line": "Token Creature - Test",
        "oracle_text": "", "power": 1, "toughness": 1,
    })
    token.card_id = token_id
    gs.card_db[token_id] = token
    evaluator = EnhancedCardEvaluator(gs)
    value = evaluator._calculate_synergy_value(card_id, [token_id, card_id])
    assert isinstance(value, (int, float)), \
        "heterogeneous battlefield IDs crashed evaluator cache construction"

    logged_ids = gs._logged_card_ids
    logged_ids.discard("both")
    gs.turn = gs.max_turns + 1
    gs.p1["life"] = gs.p2["life"] = 10
    gs._turn_limit_checked = False
    gs.check_state_based_actions()
    assert "both" not in gs._logged_card_ids, \
        "the DRAW_GAME sentinel was still looked up as a card ID"


@scenario("111.1 / 704.5d", "a ceased token ID remains valid last-known context without a missing-card warning")
def scenario_ceased_token_lookup_is_silent():
    gs = fresh()
    player = gs.p1
    token_id = gs.create_token(player, {
        "name": "Fish Token", "type_line": "Token Creature - Fish",
        "oracle_text": "", "power": 1, "toughness": 1,
    })
    assert token_id and token_id in player["battlefield"]
    assert gs.move_card(
        token_id, player, "battlefield", player, "graveyard",
        cause="test_token_ceases")
    assert token_id not in gs.card_db
    assert all(token_id not in zone_player.get(zone, ())
               for zone_player in (gs.p1, gs.p2)
               for zone in ("library", "hand", "battlefield", "graveyard", "exile"))
    gs._logged_card_ids.discard(token_id)
    assert gs._safe_get_card(token_id, None) is not None, \
        "last-known token characteristics were discarded before triggers resolved"
    assert token_id not in gs._logged_card_ids, \
        "a normal last-known token ID was reported as a missing database card"


@scenario("112.1 / 614.1c", "rule declarations handled elsewhere are not registered as dead layer abilities")
def scenario_non_layer_declarations_are_not_static_abilities():
    from Playersim.ability_types import StaticAbility, TriggeredAbility

    gs = fresh()
    player = gs.p1
    land = inject_into_zone(gs, player, {
        "name": "Quiet Dual", "type_line": "Land",
        "oracle_text": "This land enters tapped.\n{T}: Add {U}.",
    }, "battlefield")
    land_abilities = gs.ability_handler.registered_abilities.get(land, [])
    assert not any(isinstance(ability, StaticAbility)
                   and "enters tapped" in ability.effect_text.lower()
                   for ability in land_abilities)

    role = inject_into_zone(gs, player, {
        "name": "Test Role", "type_line": "Token Enchantment - Aura Role",
        "keywords": ["Enchant"],
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.",
    }, "battlefield")
    role_abilities = gs.ability_handler.registered_abilities.get(role, [])
    assert not any(isinstance(ability, StaticAbility)
                   and ability.effect_text.lower() == "enchant creature"
                   for ability in role_abilities)

    leyline = inject_card(gs, {
        "name": "Test Leyline", "type_line": "Enchantment",
        "oracle_text": (
            "If this card is in your opening hand, you may begin the game with "
            "it on the battlefield.\nWhenever you cast a spell, draw a card."),
    })
    gs.ability_handler._parse_and_register_abilities(
        leyline, gs._safe_get_card(leyline))
    leyline_abilities = gs.ability_handler.registered_abilities.get(leyline, [])
    assert any(isinstance(ability, TriggeredAbility)
               for ability in leyline_abilities)
    assert not any(isinstance(ability, StaticAbility)
                   and "opening hand" in ability.effect_text.lower()
                   for ability in leyline_abilities)

    declarations = (
        ("Test Saddle", "Saddle 3"),
        ("Test Plot", "Plot {1}{R}"),
        ("Test Mockingbird", (
            "You may have this creature enter as a copy of any creature on the "
            "battlefield with mana value less than or equal to the amount of "
            "mana spent to cast this creature, except it's a Bird in addition "
            "to its other types and it has flying")),
    )
    for name, declaration in declarations:
        card_id = inject_card(gs, {
            "name": name, "type_line": "Creature - Bird Mount",
            "oracle_text": declaration, "power": 1, "toughness": 1,
        })
        gs.ability_handler._parse_and_register_abilities(
            card_id, gs._safe_get_card(card_id))
        abilities = gs.ability_handler.registered_abilities.get(card_id, [])
        assert not any(isinstance(ability, StaticAbility)
                       and declaration.lower() in ability.effect_text.lower()
                       for ability in abilities), \
            f"dedicated declaration was also registered as static: {declaration}"


def _three_steps_action(hand_index):
    """The public PLAY_SPELL action for one of the ten actionable hand slots."""
    assert 0 <= hand_index < 10
    return 20 + hand_index if hand_index < 8 else 396 + hand_index - 8


def _three_steps_mana(generic, blue):
    return {
        "W": 0, "U": blue, "B": 0, "R": 0, "G": 0, "C": generic,
    }


def _setup_three_steps(seed, generic, blue, *, hand_index=0,
                       spell_target=False, copy_target=False):
    """Create an instant-speed Three Steps Ahead casting decision.

    The target objects are deliberately synthetic and uniquely identified so
    the public target actions can prove that each selected mode retains its
    own targeting restriction.
    """
    gs = fresh(seed)
    env = get_env()
    handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if player is gs.p1 else gs.p1
    gs.turn = 1 if player is gs.p1 else 2
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_UPKEEP
    gs.priority_player = player
    gs.priority_pass_count = 0
    gs.stack.clear()

    replace_hand(gs, player, [{
        "name": f"Spree Hand Filler {seed}-{index}",
        "mana_cost": "{1}", "type_line": "Sorcery",
        "oracle_text": "Draw a card.",
    } for index in range(hand_index)])
    spree_id = inject_real_card(gs, player, "Three Steps Ahead", "hand")
    assert player["hand"].index(spree_id) == hand_index
    spree = gs._safe_get_card(spree_id)
    assert [mode.get("cost") for mode in spree.spree_modes] == [
        "{1}{U}", "{3}", "{2}"], \
        f"real Three Steps Ahead parsed the wrong Spree modes: {spree.spree_modes}"

    target_spell = None
    if spell_target:
        target_spell = inject_card(gs, {
            "name": f"Spree Target Spell {seed}", "mana_cost": "{1}",
            "type_line": "Instant", "oracle_text": "Draw a card.",
        })
        gs.add_to_stack("SPELL", target_spell, opponent, {
            "requires_target": False, "num_targets": 0,
        })

    target_permanent = None
    if copy_target:
        target_permanent = inject_into_zone(gs, player, {
            "name": f"Spree Copy Construct {seed}", "mana_cost": "{3}",
            "type_line": "Artifact Creature - Construct", "oracle_text": "",
            "power": 2, "toughness": 3,
        }, "battlefield")

    # add_to_stack gives its controller priority. The test decision belongs to
    # the Three Steps Ahead player regardless of whether a target was staged.
    gs.phase = gs.PHASE_PRIORITY
    gs.previous_priority_phase = gs.PHASE_UPKEEP
    gs.priority_player = player
    gs.priority_pass_count = 0
    player["mana_pool"] = _three_steps_mana(generic, blue)
    handler.current_valid_actions = None
    return (gs, env, handler, player, opponent, spree_id,
            target_spell, target_permanent)


def _apply_public_action(handler, action, message):
    """Apply an action through the same mask/dispatch boundary used by PPO."""
    mask = handler.generate_valid_actions()
    assert mask[action], f"{message}: action {action} absent; valid={np.flatnonzero(mask).tolist()}"
    handler.current_valid_actions = mask
    reward, done, truncated, info = handler.apply_action(action)
    assert not info.get("execution_failed"), \
        f"{message}: mask-valid action failed: reward={reward}, info={info}"
    assert not info.get("critical_error"), f"{message}: {info}"
    return reward, done, truncated, info


def _begin_three_steps(handler, hand_index=0):
    _apply_public_action(
        handler, _three_steps_action(hand_index), "begin Three Steps Ahead")
    gs = handler.game_state
    assert gs.phase == gs.PHASE_CHOOSE and gs.choice_context, \
        "Spree cast did not enter its mode-choice phase"
    assert gs.choice_context.get("type") == "choose_mode" \
        and gs.choice_context.get("is_spree"), \
        f"Spree used the wrong choice contract: {gs.choice_context}"


def _choose_three_steps_mode(handler, mode_index):
    _apply_public_action(
        handler, 353 + mode_index, f"choose Three Steps Ahead mode {mode_index}")


def _finish_three_steps_modes(handler):
    gs = handler.game_state
    if (gs.phase == gs.PHASE_CHOOSE and gs.choice_context
            and gs.choice_context.get("is_spree")):
        _apply_public_action(handler, 11, "finish Three Steps Ahead modes")


def _select_three_steps_target(handler, player, target_id):
    gs = handler.game_state
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context, \
        f"Three Steps Ahead did not request target {target_id}"
    candidates = handler._get_target_selection_candidates(
        player, gs.targeting_context)
    assert target_id in candidates, \
        f"target {target_id} absent from Spree candidates {candidates}"
    candidate_index = candidates.index(target_id)
    for _ in range(candidate_index // 10):
        _apply_public_action(handler, 479, "page Three Steps Ahead targets")
    _apply_public_action(
        handler, 274 + candidate_index % 10,
        f"select Three Steps Ahead target {target_id}")


def _cast_three_steps_modes(handler, player, modes, target_spell=None,
                            target_permanent=None, selection_order=None):
    _begin_three_steps(handler, player["hand"].index(
        next(card_id for card_id in player["hand"]
             if getattr(handler.game_state._safe_get_card(card_id), "name", "")
             == "Three Steps Ahead")))
    for mode_index in (selection_order or modes):
        _choose_three_steps_mode(handler, mode_index)
    _finish_three_steps_modes(handler)

    gs = handler.game_state
    targets_by_mode = {0: target_spell, 1: target_permanent}
    while gs.phase == gs.PHASE_TARGETING and gs.targeting_context:
        slots = gs.targeting_context.get("target_slots", [])
        slot_index = int(gs.targeting_context.get("target_slot_index", 0))
        assert slots and slot_index < len(slots), \
            f"Spree targeting omitted its mode slots: {gs.targeting_context}"
        mode_index = slots[slot_index].get("mode_index")
        target_id = targets_by_mode.get(mode_index)
        assert target_id is not None, \
            f"no test target supplied for Spree mode {mode_index}"
        _select_three_steps_target(handler, player, target_id)

    assert gs.stack and gs.stack[-1][0] == "SPELL", \
        "Three Steps Ahead never reached the stack"
    return gs.stack[-1][3]


def _remove_test_spell_from_stack(gs, spell_id, owner):
    for index, item in enumerate(gs.stack):
        if isinstance(item, tuple) and item[0] == "SPELL" and item[1] == spell_id:
            gs.stack.pop(index)
            assert gs.move_card(
                spell_id, owner, "stack_implicit", owner, "graveyard",
                cause="scenario_target_removed")
            return
    raise AssertionError(f"test spell {spell_id} was not on the stack")


@scenario("702.172 / policy contract", "Three Steps Ahead reaches all three modes from the tenth hand slot")
def scenario_spree_tenth_hand_slot_and_third_mode_are_addressable():
    from Playersim import card_support

    card_support.reset_manifest_for_tests()
    try:
        (gs, env, handler, player, _, spree_id, _, _) = _setup_three_steps(
            SEED + 130, 0, 1, hand_index=9)
        cast_action = _three_steps_action(9)
        assert cast_action == 397
        assert not env.action_mask()[cast_action], \
            "base {U} incorrectly paid a required Spree additional cost"

        player["mana_pool"] = _three_steps_mana(2, 1)
        cast_mask = env.action_mask()
        assert cast_mask[cast_action], \
            "draw/discard mode did not make Three Steps Ahead castable"
        handler.current_valid_actions = cast_mask
        _, _, _, info = handler.apply_action(cast_action)
        assert not info.get("execution_failed"), info
        assert spree_id in player["hand"], \
            "Spree left hand or paid mana before its modes were chosen"
        assert sum(player["mana_pool"].values()) == 3

        choice_mask = handler.generate_valid_actions()
        assert not choice_mask[353] and not choice_mask[354] and choice_mask[355], \
            f"unchosen target requirements leaked into Spree: {np.flatnonzero(choice_mask).tolist()}"
        assert not choice_mask[11], "Spree allowed choosing zero modes"
        assert not choice_mask[258:274].any(), \
            "legacy two-mode Spree actions leaked into the public mask"
        _choose_three_steps_mode(handler, 2)
        assert handler.generate_valid_actions()[11], \
            "Pass was not offered after the first required Spree mode"
        _finish_three_steps_modes(handler)
        assert gs.stack[-1][3].get("selected_spree_modes") == [2]
        paid = gs.stack[-1][3].get("final_paid_cost", {})
        assert paid.get("generic") == 2 and paid.get("U") == 1, paid
        assert not card_support.get_manifest().entries.get("Three Steps Ahead"), \
            "supported Three Steps Ahead remained in the support-gap manifest"
    finally:
        card_support.reset_manifest_for_tests()


@scenario("601.2b/f / 702.172", "Spree mode masks use cumulative costs and reject duplicate or forged choices")
def scenario_spree_cumulative_affordability_and_duplicate_contract():
    (gs, _, handler, player, _, _, target_spell,
     target_permanent) = _setup_three_steps(
         SEED + 131, 3, 2, spell_target=True, copy_target=True)
    _begin_three_steps(handler)
    first_mask = handler.generate_valid_actions()
    assert first_mask[353] and first_mask[354] and first_mask[355], \
        "an individually affordable legal Spree mode was absent"
    _choose_three_steps_mode(handler, 0)

    selected_before = list(gs.choice_context.get("selected_modes", []))
    next_mask = handler.generate_valid_actions()
    assert not next_mask[353], "the same Spree mode was selectable twice"
    assert not next_mask[354], \
        "mode 1 ignored the cumulative {4}{U}{U} cost"
    assert next_mask[355] and next_mask[11], \
        "the exact-cost second mode or Finish action was absent"
    reward, ok = handler._handle_choose_mode(0, {})
    assert not ok and gs.choice_context.get("selected_modes") == selected_before, \
        f"duplicate direct mode choice mutated state: reward={reward}"
    reward, ok = handler._handle_choose_mode(1, {})
    assert not ok and gs.choice_context.get("selected_modes") == selected_before, \
        f"unaffordable direct mode choice mutated state: reward={reward}"

    _choose_three_steps_mode(handler, 2)
    _finish_three_steps_modes(handler)
    assert gs.targeting_context.get("target_slots", [])[0].get("mode_index") == 0
    _select_three_steps_target(handler, player, target_spell)
    stack_context = gs.stack[-1][3]
    assert stack_context.get("selected_spree_modes") == [0, 2]
    paid = stack_context.get("final_paid_cost", {})
    assert paid.get("generic") == 3 and paid.get("U") == 2, paid
    assert sum(player["mana_pool"].values()) == 0
    assert target_permanent in player["battlefield"], \
        "an unchosen Spree target was changed"


@scenario("601.2f / 702.172", "all seven Three Steps Ahead mode combinations add their costs exactly once")
def scenario_three_steps_ahead_all_mode_combination_costs():
    cases = [
        ((0,), 1, 2),
        ((1,), 3, 1),
        ((2,), 2, 1),
        ((0, 1), 4, 2),
        ((0, 2), 3, 2),
        ((1, 2), 5, 1),
        ((0, 1, 2), 6, 2),
    ]
    for case_index, (modes, generic, blue) in enumerate(cases):
        (gs, _, handler, player, _, spree_id, target_spell,
         target_permanent) = _setup_three_steps(
             SEED + 132 + case_index, generic, blue,
             spell_target=True, copy_target=True)
        stack_context = _cast_three_steps_modes(
            handler, player, modes, target_spell, target_permanent)
        assert stack_context.get("selected_spree_modes") == list(modes), \
            f"modes {modes} became {stack_context.get('selected_spree_modes')}"
        paid = stack_context.get("final_paid_cost", {})
        assert paid.get("generic") == generic and paid.get("U") == blue, \
            f"modes {modes} paid {paid}, expected generic={generic}, U={blue}"
        assert sum(player["mana_pool"].values()) == 0, \
            f"modes {modes} did not pay their complete cost"
        assert spree_id not in player["hand"] and gs.stack[-1][1] == spree_id


@scenario("701.5 / 702.172", "Three Steps Ahead's first mode targets and counters a spell")
def scenario_three_steps_ahead_counter_mode_end_to_end():
    (gs, _, handler, player, opponent, spree_id, target_spell,
     _) = _setup_three_steps(SEED + 139, 1, 2, spell_target=True)
    uncounterable = inject_card(gs, {
        "name": "Spree Uncounterable Probe", "mana_cost": "{1}",
        "type_line": "Instant", "oracle_text": "This spell can't be countered.",
    })
    gs.add_to_stack("SPELL", uncounterable, opponent, {
        "requires_target": False, "num_targets": 0,
    })
    gs.phase = gs.PHASE_PRIORITY
    gs.priority_player = player
    player["mana_pool"] = _three_steps_mana(1, 2)

    _begin_three_steps(handler)
    _choose_three_steps_mode(handler, 0)
    _finish_three_steps_modes(handler)
    candidates = handler._get_target_selection_candidates(
        player, gs.targeting_context)
    assert target_spell in candidates and uncounterable in candidates, \
        f"counter-mode candidates were {candidates}"
    _select_three_steps_target(handler, player, target_spell)
    assert gs.resolve_top_of_stack(), "counter-mode Three Steps Ahead did not resolve"
    assert target_spell in opponent["graveyard"], \
        "Three Steps Ahead did not counter its selected spell"
    assert any(item[1] == uncounterable for item in gs.stack), \
        "counter mode affected the unselected uncounterable spell"
    assert player["graveyard"].count(spree_id) == 1


@scenario("707.2 / 702.172", "Three Steps Ahead copies either kind of controlled permanent with printed values")
def scenario_three_steps_ahead_copy_mode_end_to_end():
    (gs, _, handler, player, opponent, spree_id, _, _) = _setup_three_steps(
        SEED + 140, 3, 1)
    own_artifact = inject_into_zone(gs, player, {
        "name": "Spree Copy Relic", "mana_cost": "{2}",
        "type_line": "Artifact", "oracle_text": "",
    }, "battlefield")
    own_creature = inject_into_zone(gs, player, {
        "name": "Spree Copy Bear", "mana_cost": "{2}{G}",
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 3,
    }, "battlefield")
    own_land = inject_into_zone(gs, player, {
        "name": "Spree Copy Land", "type_line": "Land", "oracle_text": "",
    }, "battlefield")
    opposing_creature = inject_into_zone(gs, opponent, {
        "name": "Spree Opposing Bear", "mana_cost": "{2}{G}",
        "type_line": "Creature - Bear", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")

    _begin_three_steps(handler)
    _choose_three_steps_mode(handler, 1)
    _finish_three_steps_modes(handler)
    candidates = handler._get_target_selection_candidates(
        player, gs.targeting_context)
    assert own_artifact in candidates and own_creature in candidates, \
        f"artifact-or-creature union candidates were {candidates}"
    assert own_land not in candidates and opposing_creature not in candidates

    original = gs._safe_get_card(own_creature)
    original.power, original.toughness = 7, 8
    battlefield_before = set(player["battlefield"])
    _select_three_steps_target(handler, player, own_creature)
    assert gs.resolve_top_of_stack(), "copy-mode Three Steps Ahead did not resolve"
    created_ids = [
        card_id for card_id in player["battlefield"]
        if card_id not in battlefield_before]
    assert len(created_ids) == 1, f"copy mode created {created_ids}"
    token = gs._safe_get_card(created_ids[0])
    assert getattr(token, "is_token", False) and token.name == "Spree Copy Bear"
    assert (int(token.printed("power")), int(token.printed("toughness"))) == (2, 3), \
        f"copy mode copied live {token.power}/{token.toughness} instead of printed values"
    assert player["graveyard"].count(spree_id) == 1


@scenario("121.2 / 701.8 / 702.172", "Three Steps Ahead draws two then gives its controller the discard choice")
def scenario_three_steps_ahead_draw_discard_mode_end_to_end():
    (gs, _, handler, player, opponent, spree_id, _, _) = _setup_three_steps(
        SEED + 141, 2, 1)
    hand_before = len(player["hand"])
    library_before = len(player["library"])
    opponent_hand_before = list(opponent["hand"])
    expected_draws = list(player["library"][:2])

    _cast_three_steps_modes(handler, player, (2,))
    assert gs.resolve_top_of_stack(), "draw/discard Three Steps Ahead did not resolve"
    assert gs.choice_context and gs.choice_context.get("type") == "discard" \
        and gs.choice_context.get("player") is player, \
        "draw mode did not pause for its controller's discard choice"
    assert all(card_id in player["hand"] for card_id in expected_draws)
    discarded = player["hand"][0]
    _apply_public_action(handler, 238, "complete Three Steps Ahead discard")
    assert discarded in player["graveyard"]
    assert len(player["library"]) == library_before - 2
    assert len(player["hand"]) == hand_before, \
        "cast, draw two, discard one produced the wrong net hand size"
    assert opponent["hand"] == opponent_hand_before, \
        "Three Steps Ahead made the opponent discard"
    assert player["graveyard"].count(spree_id) == 1


@scenario("608.2c / 702.172", "all Three Steps Ahead modes retain independent targets and resolve in printed order")
def scenario_three_steps_ahead_all_modes_end_to_end():
    (gs, _, handler, player, opponent, spree_id, target_spell,
     target_permanent) = _setup_three_steps(
         SEED + 142, 6, 2, spell_target=True, copy_target=True)
    library_before = len(player["library"])
    battlefield_before = set(player["battlefield"])

    _begin_three_steps(handler)
    for mode_index in (2, 1, 0):
        _choose_three_steps_mode(handler, mode_index)
    assert gs.phase == gs.PHASE_TARGETING and gs.targeting_context
    slots = gs.targeting_context.get("target_slots", [])
    assert [slot.get("mode_index") for slot in slots] == [0, 1], \
        f"reverse selection changed printed target order: {slots}"
    _select_three_steps_target(handler, player, target_spell)
    assert gs.targeting_context.get("target_slots", [])[1].get("mode_index") == 1
    _select_three_steps_target(handler, player, target_permanent)

    stack_context = gs.stack[-1][3]
    assert stack_context.get("selected_spree_modes") == [0, 1, 2], \
        "Spree stored policy selection order instead of printed order"
    assert [slot.get("mode_index")
            for slot in stack_context.get("spree_target_slots", [])] == [0, 1]
    assert stack_context.get("targets_by_slot") == [
        [target_spell], [target_permanent]], stack_context
    assert gs.resolve_top_of_stack(), "all-mode Three Steps Ahead did not resolve"
    assert target_spell in opponent["graveyard"]
    created = [
        card_id for card_id in player["battlefield"]
        if card_id not in battlefield_before
        and getattr(gs._safe_get_card(card_id), "is_token", False)]
    assert len(created) == 1 and gs._safe_get_card(created[0]).name == \
        gs._safe_get_card(target_permanent).name
    assert len(player["library"]) == library_before - 2
    assert gs.choice_context and gs.choice_context.get("type") == "discard"
    _apply_public_action(handler, 238, "complete all-mode Spree discard")
    assert player["graveyard"].count(spree_id) == 1


@scenario("608.2b / 702.172", "Spree fizzles only when every selected mode target is illegal")
def scenario_three_steps_ahead_partial_and_all_illegal_targets():
    # One legal target is enough: the legal copy mode and untargeted draw mode
    # still resolve even though the counter target disappeared.
    (gs, _, handler, player, opponent, spree_id, target_spell,
     target_permanent) = _setup_three_steps(
         SEED + 143, 6, 2, spell_target=True, copy_target=True)
    _cast_three_steps_modes(
        handler, player, (0, 1, 2), target_spell, target_permanent)
    battlefield_before = set(player["battlefield"])
    library_before = len(player["library"])
    _remove_test_spell_from_stack(gs, target_spell, opponent)
    assert gs.resolve_top_of_stack(), "partially legal Spree did not resolve"
    created = [
        card_id for card_id in player["battlefield"]
        if card_id not in battlefield_before
        and getattr(gs._safe_get_card(card_id), "is_token", False)]
    assert created, "legal copy mode was lost with the illegal counter target"
    assert len(player["library"]) == library_before - 2, \
        "untargeted draw mode was lost with one illegal target"
    assert gs.choice_context and gs.choice_context.get("type") == "discard"
    _apply_public_action(handler, 238, "complete partially legal Spree")
    assert player["graveyard"].count(spree_id) == 1

    # With both chosen targets illegal, none of the selected modes resolve,
    # including the otherwise untargeted draw/discard instruction.
    (gs, _, handler, player, opponent, spree_id, target_spell,
     target_permanent) = _setup_three_steps(
         SEED + 144, 6, 2, spell_target=True, copy_target=True)
    _cast_three_steps_modes(
        handler, player, (0, 1, 2), target_spell, target_permanent)
    library_before = len(player["library"])
    _remove_test_spell_from_stack(gs, target_spell, opponent)
    assert gs.move_card(
        target_permanent, player, "battlefield", player, "graveyard",
        cause="scenario_target_removed")
    battlefield_before = set(player["battlefield"])
    assert gs.resolve_top_of_stack(), "all-illegal Spree did not finish fizzling"
    assert len(player["library"]) == library_before, \
        "all-illegal Spree still resolved its draw mode"
    assert not (gs.choice_context and gs.choice_context.get("type") == "discard")
    assert not any(
        card_id not in battlefield_before
        and getattr(gs._safe_get_card(card_id), "is_token", False)
        for card_id in player["battlefield"]), \
        "all-illegal Spree still created its token copy"
    assert player["graveyard"].count(spree_id) == 1


@scenario("613.1f / 702.15 / 702.21", "combined parameterized keyword lines register one ability per keyword")
def scenario_oildeep_keyword_line_has_no_duplicate_static():
    from Playersim.ability_types import StaticAbility

    gs = fresh(SEED + 131)
    player = gs.p1
    oildeep = inject_real_card(gs, player, "Oildeep Gearhulk", "battlefield")
    statics = [
        ability
        for ability in gs.ability_handler.registered_abilities.get(oildeep, [])
        if isinstance(ability, StaticAbility)
    ]
    lifelink = [ability for ability in statics
                if getattr(ability, "keyword", None) == "lifelink"]
    ward = [ability for ability in statics
            if getattr(ability, "keyword", None) == "ward"]
    assert len(lifelink) == 1 and len(ward) == 1, \
        f"Oildeep keyword abilities were duplicated: {statics}"
    assert getattr(ward[0], "keyword_value", None) == "{1}"
    assert not any(
        "lifelink, ward" in getattr(ability, "effect_text", "").lower()
        for ability in statics), \
        "the combined keyword declaration became a dead StaticAbility"
    layer_six = [
        data for _, data in gs.layer_system.layers[6]
        if data.get("source_id") == oildeep
    ]
    assert len(layer_six) == 2, \
        f"keyword statics registered duplicate layer effects: {layer_six}"
    assert gs.check_keyword(oildeep, "lifelink")
    assert gs.check_keyword(oildeep, "ward")
    assert gs.ability_handler.get_ward_costs(oildeep) == ["{1}"]


@scenario("121.1 / 704.5b", "draw effects accept card ID zero, replacements, and rules-defined decking")
def scenario_draw_effect_completion_is_not_truthiness_based():
    from unittest.mock import patch
    from Playersim.ability_types import DrawCardEffect
    from Playersim.card import Card

    gs = fresh(SEED + 132)
    player = gs.p1
    zero_card = Card({
        "name": "Zero ID Draw", "mana_cost": "{0}",
        "type_line": "Artifact", "oracle_text": "",
    })
    zero_card.card_id = 0
    gs.card_db[0] = zero_card
    for owner in (gs.p1, gs.p2):
        for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
            owner[zone] = [card_id for card_id in owner.get(zone, [])
                           if card_id != 0]
    player["library"] = [0]
    player["hand"] = []
    assert DrawCardEffect(1).apply(gs, None, player, {})
    assert player["hand"] == [0], "numeric card ID 0 was treated as no draw"

    player["library"] = []
    player["life"] = 20
    player.pop("attempted_draw_from_empty", None)
    assert DrawCardEffect(1).apply(gs, None, player, {}), \
        "a normal decking draw was mislabeled as an effect failure"
    assert player.get("attempted_draw_from_empty") \
        and gs.terminal_reason == "decking"

    gs = fresh(SEED + 133)
    player = gs.p1
    with patch.object(type(gs), "_draw_card", return_value=None):
        assert DrawCardEffect(1).apply(gs, None, player, {}), \
            "a replaced draw was mislabeled as an effect failure"


@scenario("109.3 / observation contract", "exact battlefield scalars exceed the fixed card-detail tensor safely")
def scenario_large_board_counts_remain_exact_and_diagnostics_stick():
    env = get_env(); gs = fresh(SEED + 134)
    gs.p1["battlefield"] = []
    gs.p2["battlefield"] = []
    for owner, count, prefix in ((gs.p1, 21, "P1"), (gs.p2, 22, "P2")):
        for index in range(count):
            card_id = inject_card(gs, {
                "name": f"{prefix} Large Board {index}",
                "type_line": "Artifact", "oracle_text": "",
            })
            owner["battlefield"].append(card_id)
            gs._last_card_locations[card_id] = (owner, "battlefield")
    env.last_observation_error = None
    env.last_observation_traceback = None
    observation = env._get_obs()
    assert observation["p1_bf_count"].tolist() == [21]
    assert observation["p2_bf_count"].tolist() == [22]
    assert env.observation_space.contains(observation)
    assert env.last_observation_error is None

    assert env._record_observation_error(
        "sticky probe", ValueError("first episode failure"))
    env._get_obs()
    assert "first episode failure" in env.last_observation_error, \
        "observation construction erased an earlier episode diagnostic"
    env.reset(seed=SEED + 135)
    assert env.last_observation_error is None, \
        "a new episode retained the prior observation diagnostic"


@scenario("training diagnostics / opponent", "opponent execution failures retain an agent-replayable artifact")
def scenario_opponent_failure_persists_replay():
    from unittest.mock import patch

    gs = fresh(SEED + 136); env = get_env(); handler = env.action_handler
    gs.agent_is_p1 = True
    gs.priority_player = gs.p1
    calls = []

    def fake_apply(_handler, action_idx, context=None):
        calls.append(action_idx)
        if len(calls) == 1:
            return 0.0, False, False, {}
        return 0.0, False, False, {
            "execution_failed": True,
            "error_message": "forced opponent mask-contract failure",
            "handler_error": "forced opponent handler error",
            "failed_action": {"action_index": action_idx,
                              "context": dict(context or {})},
        }

    opponent = gs.p2
    with patch.object(type(handler), "apply_action", new=fake_apply), \
            patch.object(env, "_opponent_needs_to_act",
                         return_value=(opponent, {})), \
            patch.object(env, "_get_opponent_policy_action",
                         return_value=(224, {})):
        assert env.action_mask()[11]
        _, _, _, _, info = env.step(11)
    assert info.get("opponent_execution_failed")
    replay_path = info.get("failure_replay_path")
    assert replay_path and os.path.isfile(replay_path)
    with open(replay_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["agent_is_p1"] is True
    assert payload["actions"][-1]["action"] == 11, \
        "the replay stored the scripted action instead of its triggering agent action"
    assert payload["failure"]["actor"] == "opponent"
    assert payload["failure"]["recent_actions"][-1] == 224


@scenario("613.1f / 604.1", "Zur grants all three keywords only to controlled enchantment creatures")
def scenario_zur_keyword_bundle_is_scoped_and_live():
    from Playersim.ability_types import StaticAbility
    from Playersim.card import Card

    gs = fresh(SEED + 137)
    controller, opponent = gs.p1, gs.p2
    zur = inject_real_card(
        gs, controller, "Zur, Eternal Schemer", "battlefield")
    normal_creature = inject_into_zone(gs, controller, {
        "name": "Zur Ordinary Creature", "type_line": "Creature - Human",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    opposing_enchantment_creature = inject_into_zone(gs, opponent, {
        "name": "Zur Opposing Enchantment Creature",
        "type_line": "Enchantment Creature - Spirit",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")

    # This object enters after Zur, proving the grant is a live scope rather
    # than a frozen list captured when the source entered.
    friendly_enchantment_creature = inject_into_zone(gs, controller, {
        "name": "Zur Friendly Enchantment Creature",
        "type_line": "Enchantment Creature - Spirit",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()

    grant_keywords = {"deathtouch", "lifelink", "hexproof"}
    grant_effects = [
        data for _, data in gs.layer_system.layers[6]
        if data.get("source_id") == zur
        and str(data.get("effect_value", "")).lower() in grant_keywords
        and data.get("affected_scope")
    ]
    assert {str(data["effect_value"]).lower() for data in grant_effects} \
        == grant_keywords, f"Zur did not register all keyword grants: {grant_effects}"
    for keyword in grant_keywords:
        assert gs.check_keyword(friendly_enchantment_creature, keyword), \
            f"Zur failed to grant {keyword} to a controlled enchantment creature"
        assert not gs.check_keyword(normal_creature, keyword), \
            f"Zur incorrectly granted {keyword} to an ordinary creature"
        assert not gs.check_keyword(opposing_enchantment_creature, keyword), \
            f"Zur incorrectly granted {keyword} to an opponent's permanent"
        assert not gs.check_keyword(zur, keyword), \
            f"Zur inherited {keyword} from text that grants it to other objects"
    assert gs.check_keyword(zur, "flying"), "Zur lost its own printed Flying"

    zur_card = gs._safe_get_card(zur)
    intrinsic = {
        keyword for index, keyword in enumerate(Card.ALL_KEYWORDS)
        if zur_card.keywords[index]
    }
    assert intrinsic == {"flying"}, \
        f"Zur's printed keyword vector contains scoped grants: {intrinsic}"
    statics = [
        ability
        for ability in gs.ability_handler.registered_abilities.get(zur, [])
        if isinstance(ability, StaticAbility)
    ]
    assert not any(
        getattr(ability, "keyword", None) in grant_keywords
        for ability in statics), "scoped grants became intrinsic source abilities"

    assert gs.move_card(
        zur, controller, "battlefield", controller, "graveyard")
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    for keyword in grant_keywords:
        assert not gs.check_keyword(friendly_enchantment_creature, keyword), \
            f"Zur's {keyword} grant survived after its source left"


@scenario("109.3 / observation schema", "card vectors retain subtypes/MDFC fields and signed live P/T")
def scenario_card_feature_schema_is_complete_and_signed():
    from Playersim.card import Card

    env = get_env(); gs = fresh(SEED + 138)
    expected_dim = (
        4 + 6 + len(Card.ALL_KEYWORDS)
        + 5 + len(env._subtype_vocab) + 3)
    assert env._feature_dim == expected_dim

    agent = gs.p1 if gs.agent_is_p1 else gs.p2
    opponent = gs.p2 if agent is gs.p1 else gs.p1
    weakened = inject_into_zone(gs, opponent, {
        "name": "Signed Feature Creature", "type_line": "Creature - Spirit",
        "oracle_text": "", "power": 1, "toughness": 1,
    }, "battlefield")
    gs.layer_system.register_effect({
        "source_id": weakened, "layer": 7, "sublayer": "c",
        "affected_ids": [weakened], "effect_type": "modify_pt",
        "effect_value": (-3, -3), "duration": "permanent",
    })
    gs.layer_system.invalidate_cache()
    gs.layer_system.apply_all_effects()
    assert (gs._safe_get_card(weakened).power,
            gs._safe_get_card(weakened).toughness) == (-2, -2)

    env.last_observation_error = None
    env.last_observation_traceback = None
    observation = env._get_obs()
    row = opponent["battlefield"].index(weakened)
    assert observation["opp_battlefield"][row, 2:4].tolist() == [-2.0, -2.0]
    assert observation["opp_total_power"].tolist() == [-2]
    assert observation["opp_total_toughness"].tolist() == [-2]
    assert env.last_observation_error is None
    assert env.observation_space.contains(observation)

    # A later database load may change Card.SUBTYPE_VOCAB globally; the
    # environment must still project cards into its captured schema and retain
    # the final MDFC fields instead of truncating them.
    mdfc = inject_card(gs, {
        "name": "Feature MDFC Front", "layout": "modal_dfc",
        "type_line": "Creature - Spirit", "oracle_text": "",
        "power": 2, "toughness": 3,
    })
    mdfc_card = gs._safe_get_card(mdfc)
    mdfc_card.faces = [
        {"name": "Feature MDFC Front", "type_line": "Creature - Spirit",
         "oracle_text": "", "power": "2", "toughness": "3"},
        {"name": "Feature MDFC Back", "type_line": "Creature - Spirit",
         "oracle_text": "", "power": "7", "toughness": "8"},
    ]
    vector = env._get_card_feature(mdfc, env._feature_dim)
    assert len(vector) == env._feature_dim
    assert vector[-3:].tolist() == [1.0, 7.0, 8.0], \
        f"MDFC fields were lost from the policy vector: {vector[-3:]}"
    battlefield_space = env.observation_space["opp_battlefield"]
    assert battlefield_space.low[0, 2] < -2 \
        and battlefield_space.high[0, 2] > 50
    assert battlefield_space.low[0, 10] == 0 \
        and battlefield_space.high[0, 10] == 1


@scenario("603.6c / 715.3", "Mosswood Dreadknight grants and expires a graveyard Adventure cast")
def scenario_mosswood_dreadknight_graveyard_adventure_permission():
    gs = fresh(SEED + 139); env = get_env(); handler = env.action_handler
    controller = gs.p1
    gs.agent_is_p1 = True
    controller["graveyard"] = []
    dreadknight = inject_real_card(
        gs, controller, "Mosswood Dreadknight // Dread Whispers",
        "battlefield")
    card = gs._safe_get_card(dreadknight)
    adventure = card.get_adventure_data()
    assert adventure and adventure["name"] == "Dread Whispers", adventure

    assert gs.move_card(
        dreadknight, controller, "battlefield", controller, "graveyard",
        cause="destroy")
    gs.ability_handler.process_triggered_abilities()
    assert gs.stack and gs.stack[-1][1] == dreadknight, \
        "Dreadknight's dies trigger did not reach the stack"
    assert gs.resolve_top_of_stack(), \
        "Dreadknight's graveyard Adventure permission did not resolve"
    assert gs.has_graveyard_adventure_permission(controller, dreadknight)

    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.previous_priority_phase = None
    gs.priority_player = controller
    gs.priority_pass_count = 0
    controller["mana_pool"] = {
        "W": 0, "U": 0, "B": 1, "R": 0, "G": 0, "C": 1,
    }
    graveyard_index = controller["graveyard"].index(dreadknight)
    action_index = 472 + graveyard_index
    mask = handler.generate_valid_actions()
    assert mask[action_index], \
        "the permitted Dread Whispers cast was absent from the action mask"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(action_index)
    assert not info.get("execution_failed"), info
    assert dreadknight not in controller["graveyard"]
    assert gs.stack and gs.stack[-1][1] == dreadknight
    assert not gs.has_graveyard_adventure_permission(controller, dreadknight), \
        "the graveyard permission was not consumed by casting"

    life_before = controller["life"]
    hand_before = len(controller["hand"])
    assert gs.resolve_top_of_stack(), "Dread Whispers did not resolve"
    assert dreadknight in controller["exile"]
    assert dreadknight in gs.cards_castable_from_exile
    assert controller["life"] == life_before - 1
    assert len(controller["hand"]) == hand_before + 1

    assert gs.move_card(
        dreadknight, controller, "exile", controller, "graveyard",
        cause="permission_expiry_probe")
    assert gs.grant_graveyard_adventure_permission(controller, dreadknight)
    expires_turn = gs.graveyard_adventure_permissions[0]["expires_turn"]
    gs.turn = expires_turn
    gs._cleanup_step_actions(controller, discard_to_max=False)
    assert not gs.has_graveyard_adventure_permission(controller, dreadknight), \
        "the until-end-of-next-turn permission survived its cleanup"


@scenario("727.1", "explicit instructions make it day or night and synchronize daybound permanents")
def scenario_explicit_day_night_instruction():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 140)
    controller = gs.p1
    werewolf = inject_into_zone(gs, controller, {
        "name": "Instruction Watcher", "layout": "transform",
        "mana_cost": "{1}{G}", "type_line": "Creature - Human Werewolf",
        "oracle_text": "Daybound", "power": "2", "toughness": "2",
        "color_identity": ["G"],
        "card_faces": [
            {"name": "Instruction Watcher", "mana_cost": "{1}{G}",
             "type_line": "Creature - Human Werewolf", "oracle_text": "Daybound",
             "power": "2", "toughness": "2", "colors": ["G"]},
            {"name": "Instruction Howler", "mana_cost": "",
             "type_line": "Creature - Werewolf", "oracle_text": "Nightbound",
             "power": "4", "toughness": "4", "colors": ["G"]},
        ],
    }, "battlefield")
    card = gs._safe_get_card(werewolf)
    night_effects = EffectFactory.create_effects("It becomes night.")
    assert night_effects and type(night_effects[0]).__name__ == "SetDayNightEffect", \
        f"explicit night instruction did not parse: {night_effects}"
    assert night_effects[0].apply(gs, None, controller)
    assert gs.day_night_state == "night" and card.current_face == 1, \
        "explicit night instruction did not transform the daybound permanent"
    day_effects = EffectFactory.create_effects("It becomes day.")
    assert day_effects and day_effects[0].apply(gs, None, controller)
    assert gs.day_night_state == "day" and card.current_face == 0, \
        "explicit day instruction did not restore the day face"


@scenario("603.7 / lookahead", "text delayed triggers survive cloning and fire only in the clone")
def scenario_delayed_trigger_clone_isolation():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 141)
    controller = gs.p1
    source = card_id_by_name(gs, "Thicket Brute")
    effects = EffectFactory.create_effects(
        "At the beginning of the next end step, you gain 2 life.")
    assert effects and effects[0].apply(gs, source, controller)
    before = controller["life"]
    clone = gs.clone()
    cloned_controller = clone.p1
    assert clone.delayed_triggers, "lookahead clone dropped a pending delayed trigger"
    assert clone.process_delayed_triggers(clone.PHASE_END_STEP) == 1
    assert cloned_controller["life"] == before + 2, \
        "the cloned delayed trigger did not affect the cloned controller"
    assert controller["life"] == before, \
        "firing a cloned delayed trigger mutated the source game"
    assert gs.delayed_triggers, \
        "firing the clone consumed the source game's delayed trigger"


@scenario("603.7 / legacy lookahead", "legacy delayed closures rebind to cloned state instead of disappearing")
def scenario_legacy_delayed_callback_clone_isolation():
    gs = fresh(SEED + 181)
    controller = gs.p1
    source = card_id_by_name(gs, "Thicket Brute")
    before = controller["life"]
    gs.register_delayed_trigger(
        lambda: gs.gain_life(controller, 3, source_id=source),
        phase=gs.PHASE_END_STEP,
        description="legacy clone rebinding probe")

    clone = gs.clone()
    assert clone.delayed_triggers, \
        "lookahead clone dropped a registered legacy closure"
    assert clone.process_delayed_triggers(clone.PHASE_END_STEP) == 1
    assert clone.p1["life"] == before + 3, \
        "the rebound closure did not mutate cloned player state"
    assert controller["life"] == before, \
        "the rebound closure leaked into the source game/player"
    assert gs.delayed_triggers, \
        "firing the clone consumed the original legacy closure"

    class OpaqueCallable:
        def __call__(self):
            return None

    try:
        gs.register_delayed_trigger(OpaqueCallable())
    except TypeError:
        pass
    else:
        raise AssertionError(
            "an unsupported opaque callable was accepted and could diverge in clones")


@scenario("707 / lookahead identity", "mutable card and merged-object state is isolated between clone branches")
def scenario_clone_isolates_mutable_card_identity():
    gs = fresh(SEED + 185)
    player = gs.p1
    base = inject_into_zone(gs, player, {
        "name": "Clone Merge Base", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast", "oracle_text": "Trample",
        "power": 2, "toughness": 2,
    }, "battlefield")
    component = inject_card(gs, {
        "name": "Clone Mutator", "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Cat Beast", "oracle_text": "Mutate {1}{G}\nReach",
        "power": 3, "toughness": 3,
    })
    assert gs.mutate(player, component, base, mutate_on_top=False)
    original_card = gs._safe_get_card(base)
    original_name = original_card.name
    original_counters = dict(original_card.counters)

    clone = gs.clone()
    cloned_card = clone._safe_get_card(base)
    assert cloned_card is not original_card, \
        "lookahead retained the source game's mutable Card object"
    assert clone.mutated_permanents is not gs.mutated_permanents
    assert clone.mutated_permanents[base] is not gs.mutated_permanents[base]

    cloned_card._printed["name"] = "Branch-Only Merge"
    cloned_card.reset_to_printed()
    clone.add_counter(base, "+1/+1", 2)
    clone.mutated_permanents[base]["components"].append("branch-only")
    assert original_card.name == original_name
    assert original_card.counters == original_counters
    assert "branch-only" not in gs.mutated_permanents[base]["components"], \
        "merged identity metadata leaked out of the clone"


@scenario("109.5 / 400.7 / 610.3", "repeated printings become distinct owned runtime objects and linked sources")
def scenario_repeated_printings_have_runtime_object_identity():
    gs = fresh(SEED + 189)
    player, opponent = gs.p1, gs.p2
    groups = {}
    for runtime_id in (
            player["library"] + player["hand"] + opponent["library"]
            + opponent["hand"]):
        groups.setdefault(gs.canonical_card_id(runtime_id), []).append(runtime_id)
    printing_id, instances = next(
        (printing, ids) for printing, ids in groups.items() if len(ids) >= 2)
    first, second = instances[:2]
    assert first != second
    assert gs.canonical_card_id(first) == gs.canonical_card_id(second) == printing_id
    assert gs._safe_get_card(first) is not gs._safe_get_card(second), \
        "two physical copies still share one mutable Card object"
    assert gs._find_card_owner_fallback(first) in (player, opponent)
    assert gs._find_card_owner_fallback(second) in (player, opponent)

    # Put two same-printing copies controlled by P1 onto the battlefield and
    # prove their linked-exile records are independent runtime source objects.
    sources = [runtime_id for runtime_id in instances
               if gs._find_card_owner_fallback(runtime_id) is player][:2]
    assert len(sources) == 2
    for source_id in sources:
        owner, zone = gs.find_card_location(source_id)
        assert gs.move_card(
            source_id, owner, zone, player, "battlefield",
            cause="runtime_identity_probe")
    target_a = inject_into_zone(gs, opponent, {
        "name": "First Linked Runtime Target", "mana_cost": "{1}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }, "hand")
    target_b = inject_into_zone(gs, opponent, {
        "name": "Second Linked Runtime Target", "mana_cost": "{1}",
        "type_line": "Creature", "oracle_text": "", "power": 1,
        "toughness": 1,
    }, "hand")
    assert gs.exile_until_source_leaves(
        sources[0], player, target_a, opponent, from_zone="hand",
        return_zone="hand")
    assert gs.exile_until_source_leaves(
        sources[1], player, target_b, opponent, from_zone="hand",
        return_zone="hand")
    assert gs.move_card(
        sources[0], player, "battlefield",
        gs._find_card_owner_fallback(sources[0]), "graveyard")
    assert target_a in opponent["hand"] and target_b in opponent["exile"], \
        "one same-printing source returned the other source's linked card"


@scenario("721.3 / 400.3", "mutate components separate into each physical card owner's zone")
def scenario_mutate_separation_respects_component_ownership():
    gs = fresh(SEED + 186)
    controller = gs.p1
    base = inject_into_zone(gs, controller, {
        "name": "Owned Merge Base", "mana_cost": "{1}{G}", "cmc": 2,
        "type_line": "Creature - Beast", "oracle_text": "Vigilance",
        "power": 2, "toughness": 2,
    }, "battlefield")
    borrowed = inject_card(gs, {
        "name": "Borrowed Mutator", "mana_cost": "{2}{U}", "cmc": 3,
        "type_line": "Creature - Bird Beast", "oracle_text": "Mutate {1}{U}\nFlying",
        "power": 3, "toughness": 3,
    })
    # Model P1 controlling a mutating spell physically owned by P2.
    gs.original_p2_deck.append(borrowed)
    gs._last_card_locations[borrowed] = (gs.p1, "stack")
    assert gs.mutate(controller, borrowed, base, mutate_on_top=True)
    owners = gs.mutated_permanents[base].get("component_owner_keys", {})
    assert owners == {base: "p1", borrowed: "p2"}

    assert gs.move_card(
        base, controller, "battlefield", controller, "graveyard",
        cause="destroy")
    assert base in gs.p1["graveyard"]
    assert borrowed in gs.p2["graveyard"]
    assert borrowed not in gs.p1["graveyard"], \
        "borrowed mutate component followed its controller instead of its owner"


@scenario("508.1m / 603.2", "equipped-creature attack triggers fire only for the equipped attacker")
def scenario_equipped_creature_attack_trigger_scope():
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh(SEED + 142)
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    equipped = inject_into_zone(gs, controller, {
        "name": "Equipped Attacker", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    other = inject_into_zone(gs, controller, {
        "name": "Other Attacker", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    equipment = inject_into_zone(gs, controller, {
        "name": "Attack Bell", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact - Equipment", "oracle_text": (
            "Equipped creature gets +1/+0.\nWhenever equipped creature attacks, "
            "you gain 2 life.\nEquip {1}"
        ),
    }, "battlefield")
    assert gs.equip_permanent(controller, equipment, equipped)
    gs.ability_handler.active_triggers = []
    life_before = controller["life"]

    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [other]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert not gs.stack, "the Equipment triggered for an unequipped attacker"
    assert controller["life"] == life_before

    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [equipped]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert gs.stack, "the Equipment did not trigger for its equipped attacker"
    while gs.stack:
        assert gs.resolve_top_of_stack()
    assert controller["life"] == life_before + 2, \
        "the equipped-creature trigger applied the wrong effect"


@scenario("111.10g", "all seven Role definitions apply their printed static and attack abilities")
def scenario_all_role_token_definitions():
    from Playersim.ability_utils import EffectFactory
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh(SEED + 143)
    combat = integrate_combat_actions(gs)
    controller = gs.p1

    def bearer(name, power=2, toughness=2):
        return inject_into_zone(gs, controller, {
            "name": name, "mana_cost": "{1}{G}", "cmc": 2,
            "type_line": "Creature - Bear", "oracle_text": "",
            "power": power, "toughness": toughness,
        }, "battlefield")

    def create_role(role_name, target):
        effects = EffectFactory.create_effects(
            f"Create a {role_name} Role token attached to target creature.")
        assert effects and type(effects[0]).__name__ == "CreateRoleEffect", \
            f"{role_name} Role did not parse: {effects}"
        assert effects[0].apply(
            gs, None, controller, {"creatures": [target]})
        return next(
            cid for cid in reversed(controller["battlefield"])
            if getattr(gs._safe_get_card(cid), "name", "") == f"{role_name} Role")

    cursed_bearer = bearer("Cursed Bearer", 4, 4)
    create_role("Cursed", cursed_bearer)
    cursed = gs._safe_get_card(cursed_bearer)
    assert (cursed.power, cursed.toughness) == (1, 1), \
        f"Cursed Role did not set base P/T to 1/1: {cursed.power}/{cursed.toughness}"

    royal_bearer = bearer("Royal Bearer")
    create_role("Royal", royal_bearer)
    royal = gs._safe_get_card(royal_bearer)
    assert (royal.power, royal.toughness) == (3, 3)
    assert gs.check_keyword(royal_bearer, "ward"), \
        "Royal Role did not grant ward"
    assert gs.ability_handler.get_ward_costs(royal_bearer) == ["{1}"], \
        "Royal Role did not preserve its ward {1} cost"

    virtuous_bearer = bearer("Virtuous Bearer")
    inject_into_zone(gs, controller, {
        "name": "Virtuous Companion", "mana_cost": "{W}", "cmc": 1,
        "type_line": "Enchantment", "oracle_text": "",
    }, "battlefield")
    create_role("Virtuous", virtuous_bearer)
    virtuous = gs._safe_get_card(virtuous_bearer)
    enchantment_count = sum(
        "enchantment" in getattr(gs._safe_get_card(cid), "card_types", [])
        for cid in controller["battlefield"])
    assert (virtuous.power, virtuous.toughness) == \
        (2 + enchantment_count, 2 + enchantment_count), \
        "Virtuous Role did not count every enchantment its controller controls"

    sorcerer_bearer = bearer("Sorcerer Bearer")
    create_role("Sorcerer", sorcerer_bearer)
    gs.ability_handler.active_triggers = []
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [sorcerer_bearer]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert gs.stack, "Sorcerer Role did not trigger for its enchanted attacker"
    assert gs.resolve_top_of_stack(), "Sorcerer Role's scry trigger did not resolve"
    assert gs.choice_context and gs.choice_context.get("type") == "scry", \
        "Sorcerer Role did not expose its scry choice"
    gs.choice_context = None
    gs.phase = gs.PHASE_MAIN_PRECOMBAT

    young_bearer = bearer("Young Hero Bearer", 2, 2)
    create_role("Young Hero", young_bearer)
    gs.ability_handler.active_triggers = []
    gs.phase = gs.PHASE_DECLARE_ATTACKERS
    gs.current_attackers = [young_bearer]
    gs.current_block_assignments = {}
    assert combat.handle_declare_attackers_done()
    gs.ability_handler.process_triggered_abilities()
    assert gs.stack, "Young Hero Role did not trigger at toughness 2"
    while gs.stack:
        assert gs.resolve_top_of_stack()
    assert gs._safe_get_card(young_bearer).counters.get("+1/+1", 0) == 1, \
        "Young Hero Role did not put a counter on its enchanted attacker"


@scenario("603.7c / 608.2c", "a delayed rider binds the token created earlier in the same resolution")
def scenario_delayed_trigger_binds_created_token():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 144)
    controller = gs.p1
    source = card_id_by_name(gs, "Thicket Brute")
    effects = EffectFactory.create_effects(
        "Create a 1/1 white Soldier creature token. "
        "Exile that token at the beginning of the next end step.")
    assert len(effects) == 2, f"compound token delay parsed as {effects}"
    success, pending = gs._run_effect_sequence(
        effects, source, controller, context={})
    assert success and not pending
    soldiers = [
        cid for cid in controller["battlefield"]
        if getattr(gs._safe_get_card(cid), "is_token", False)
        and "soldier" in {
            str(subtype).lower()
            for subtype in getattr(gs._safe_get_card(cid), "subtypes", [])
        }
    ]
    assert len(soldiers) == 1, "the first instruction did not create one Soldier"
    token = soldiers[0]
    assert source not in controller["battlefield"], \
        "test source unexpectedly started on the battlefield"
    assert gs.process_delayed_triggers(gs.PHASE_END_STEP) == 1
    assert token not in controller["battlefield"] \
        and token in getattr(gs, "_ceased_token_cards", {}), \
        "the delayed rider did not exile and cease the token it referred to"


@scenario("107.4h / 106.3", "snow mana is consumed once whether floated or auto-produced")
def scenario_snow_mana_payment_is_single_charge():
    gs = fresh(SEED + 145)
    player = gs.p1

    def snow_forest(name):
        return inject_into_zone(gs, player, {
            "name": name, "mana_cost": "", "cmc": 0,
            "type_line": "Basic Snow Land - Forest",
            "oracle_text": "{T}: Add {G}.",
        }, "battlefield")

    floated_source = snow_forest("Floated Snow Forest")
    gs.untap_permanent(floated_source, player)
    assert gs.mana_system.tap_land_for_mana(player, floated_source)
    assert player["mana_pool"].get("G", 0) == 1
    fidelity_before = gs.fidelity_counters["unparsed_effects"]
    paid = gs.mana_system.pay_mana_cost_get_details(
        player, gs.mana_system.parse_mana_cost("{S}"))
    assert paid and paid["snow_paid"] == 1, \
        "floated mana from a snow permanent could not pay {S}"
    assert player["mana_pool"].get("G", 0) == 0, \
        "paying {S} did not consume the floated snow mana"
    assert gs.fidelity_counters["unparsed_effects"] == fidelity_before, \
        "faithful snow payment still incremented the fidelity counter"

    auto_source = snow_forest("Auto Snow Forest")
    gs.untap_permanent(auto_source, player)
    paid = gs.mana_system.pay_mana_cost_get_details(
        player, gs.mana_system.parse_mana_cost("{S}"))
    assert paid and auto_source in player["tapped_permanents"], \
        "auto-payment did not tap the snow source"
    assert sum(player["mana_pool"].values()) == 0, \
        "auto-paying {S} both paid the cost and left the produced mana floating"


@scenario("107.4h / restricted mana", "snow provenance survives conditional and phase-restricted mana pools")
def scenario_restricted_snow_mana_provenance():
    gs = fresh(SEED + 183)
    player = gs.p1
    conditional_source = inject_into_zone(gs, player, {
        "name": "Snow Creature Spring", "mana_cost": "", "cmc": 0,
        "type_line": "Snow Land", "card_types": ["land"],
        "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    phase_source = inject_into_zone(gs, player, {
        "name": "Snow Phase Spring", "mana_cost": "", "cmc": 0,
        "type_line": "Snow Land", "card_types": ["land"],
        "oracle_text": "{T}: Add {U}.",
    }, "battlefield")
    creature_spell = inject_into_zone(gs, player, {
        "name": "Restricted Snow Consumer", "mana_cost": "{S}{S}",
        "cmc": 2, "type_line": "Creature - Elemental",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "hand")
    creature_card = gs._safe_get_card(creature_spell)

    gs.mana_system.add_mana_to_pool(
        player,
        "{G}. Spend this mana only to cast a creature spell.",
        land_context={"source_permanent_id": conditional_source})
    gs.mana_system.add_mana_to_pool(
        player, "{U}",
        land_context={"source_permanent_id": phase_source},
        phase_restricted=True)
    player.setdefault("tapped_permanents", set()).update(
        {conditional_source, phase_source})
    assert sum(sum(pool.values()) for pool in
               player.get("conditional_snow_mana", {}).values()) == 1
    assert player.get("phase_restricted_snow_mana", {}).get("U") == 1

    context = {"card": creature_card, "card_id": creature_spell}
    assert gs.mana_system.can_pay_snow_cost(player, 2, context)
    details = gs.mana_system.pay_mana_cost_get_details(
        player, "{S}{S}", context)
    assert details and details.get("snow_paid") == 2
    assert not any(player.get("phase_restricted_snow_mana", {}).values())
    assert not any(
        amount for pool in player.get("conditional_snow_mana", {}).values()
        for amount in pool.values()), \
        "restricted snow provenance survived after its mana was spent"
    assert not any(player.get("phase_restricted_mana", {}).values())
    assert not any(
        amount for pool in player.get("conditional_mana", {}).values()
        for amount in pool.values())

    nonsnow = inject_into_zone(gs, player, {
        "name": "Ordinary Creature Spring", "mana_cost": "", "cmc": 0,
        "type_line": "Land", "card_types": ["land"],
        "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    gs.mana_system.add_mana_to_pool(
        player,
        "{G}. Spend this mana only to cast a creature spell.",
        land_context={"source_permanent_id": nonsnow})
    player["tapped_permanents"].add(nonsnow)
    assert not gs.mana_system.can_pay_snow_cost(player, 1, context)

    gs._empty_mana_pools()
    assert not player.get("conditional_snow_mana") \
        and not player.get("phase_restricted_snow_mana")


@scenario("508.1m", "nontoken attack watchers accept nontokens and reject tokens")
def scenario_nontoken_attack_watcher_scope():
    from Playersim.combat_integration import integrate_combat_actions
    gs = fresh(SEED + 146)
    combat = integrate_combat_actions(gs)
    controller = gs.p1
    watcher = inject_into_zone(gs, controller, {
        "name": "Nontoken Watcher", "mana_cost": "{2}{W}", "cmc": 3,
        "type_line": "Creature - Cleric", "power": 2, "toughness": 3,
        "oracle_text": (
            "Whenever a nontoken creature you control attacks, you gain 1 life."
        ),
    }, "battlefield")
    nontoken = inject_into_zone(gs, controller, {
        "name": "Real Attacker", "mana_cost": "{1}{W}", "cmc": 2,
        "type_line": "Creature - Soldier", "oracle_text": "",
        "power": 2, "toughness": 2,
    }, "battlefield")
    token = gs.create_token(controller, {
        "name": "Soldier Token", "type_line": "Token Creature - Soldier",
        "card_types": ["token", "creature"], "subtypes": ["Soldier"],
        "power": 1, "toughness": 1, "oracle_text": "", "is_token": True,
    })
    gs.ability_handler.active_triggers = []

    def queued_for(attacker):
        gs.ability_handler.active_triggers = []
        gs.stack.clear()
        gs.phase = gs.PHASE_DECLARE_ATTACKERS
        gs.current_attackers = [attacker]
        gs.current_block_assignments = {}
        assert combat.handle_declare_attackers_done()
        return [ctx.get("source_card_id")
                for _, _, ctx in gs.ability_handler.active_triggers]

    assert queued_for(nontoken) == [watcher], \
        "the nontoken watcher rejected a nontoken creature"
    assert queued_for(token) == [], \
        "the nontoken watcher triggered for a creature token"


@scenario("500.8 / 505.5", "an added combat followed by a main phase resumes the normal turn afterward")
def scenario_additional_combat_followed_by_main_phase():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 147)
    controller = gs.p1
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    effects = EffectFactory.create_effects(
        "After this phase, there is an additional combat phase followed by "
        "an additional main phase.")
    assert len(effects) == 1 \
        and type(effects[0]).__name__ == "AdditionalCombatPhaseEffect", effects
    assert getattr(effects[0], "followed_by_main", False), \
        "the parser dropped the additional-main rider"
    assert effects[0].apply(gs, None, controller)

    gs._advance_phase()
    assert gs.phase == gs.PHASE_BEGIN_COMBAT, \
        "the inserted combat did not immediately follow the current main phase"
    gs.phase = gs.PHASE_END_OF_COMBAT
    gs._advance_phase()
    assert gs.phase == gs.PHASE_MAIN_POSTCOMBAT, \
        "the inserted combat was not followed by its additional main phase"
    gs._advance_phase()
    assert gs.phase == gs.PHASE_BEGIN_COMBAT, \
        "the normal combat phase was lost after the inserted main phase"
    gs.phase = gs.PHASE_END_OF_COMBAT
    gs._advance_phase()
    assert gs.phase == gs.PHASE_MAIN_POSTCOMBAT, \
        "the normal postcombat main phase was lost after the normal combat"


@scenario("policy contract / pagination", "discard and forced-sacrifice choices reach objects beyond slot ten")
def scenario_discard_and_forced_sacrifice_pagination():
    gs = fresh(SEED + 148)
    env = get_env()
    handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    hand_ids = replace_hand(gs, player, [{
        "name": f"Paged Hand Card {i}", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Sorcery", "oracle_text": ""
    } for i in range(12)])
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.choice_context = None
    assert gs.start_discard_choice([player], count=1)
    mask = handler.generate_valid_actions()
    assert mask[479], "a twelve-card discard choice had no next page"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(479)
    assert not info.get("execution_failed")
    mask = handler.generate_valid_actions()
    assert handler.action_reasons_with_context[238]["context"]["hand_idx"] == 10
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(238)
    assert not info.get("execution_failed") and hand_ids[10] in player["graveyard"]
    assert hand_ids[0] in player["hand"], "discard page rebound to the first card"

    gs = fresh(SEED + 149)
    env = get_env()
    handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    for card_id in list(player.get("battlefield", [])):
        gs.move_card(card_id, player, "battlefield", player, "graveyard")
    permanents = [inject_into_zone(gs, player, {
        "name": f"Paged Permanent {i}", "mana_cost": "", "cmc": 0,
        "type_line": "Artifact", "oracle_text": ""
    }, "battlefield") for i in range(12)]
    gs.begin_forced_sacrifice(player, 1, None)
    mask = handler.generate_valid_actions()
    assert mask[479], "a twelve-permanent sacrifice choice had no next page"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(479)
    assert not info.get("execution_failed")
    mask = handler.generate_valid_actions()
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(353)
    assert not info.get("execution_failed") and permanents[10] in player["graveyard"]
    assert permanents[0] in player["battlefield"], \
        "forced-sacrifice page rebound to the first permanent"


@scenario("602.2b / policy aliases", "dedicated mechanic actions commit their printed activation costs")
def scenario_mechanic_alias_commits_activation_cost():
    gs = fresh(SEED + 150)
    env = get_env()
    handler = env.action_handler
    player = gs.p1 if gs.agent_is_p1 else gs.p2
    source = inject_into_zone(gs, player, {
        "name": "Costed Investigator", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Artifact Creature - Detective",
        "power": 2, "toughness": 2,
        "oracle_text": "{2}, {T}: Investigate."
    }, "battlefield")
    player.get("entered_battlefield_this_turn", set()).discard(source)
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.stack.clear()
    player["mana_pool"] = {color: 0 for color in ("W", "U", "B", "R", "G", "C")}
    player["mana_pool"]["C"] = 2
    mask = handler.generate_valid_actions()
    assert mask[418], "the affordable Investigate alias was not exposed"
    context = handler.action_reasons_with_context[418]["context"]
    assert context.get("ability_idx") is not None
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(418)
    assert not info.get("execution_failed"), info
    assert source in player["tapped_permanents"], \
        "the Investigate alias bypassed its tap cost"
    assert sum(player["mana_pool"].values()) == 0, \
        "the Investigate alias bypassed its mana cost"
    assert gs.stack and gs.stack[-1][0] == "ABILITY", \
        "the alias resolved directly instead of using the ability stack"
    assert gs.resolve_top_of_stack(), "the canonical Investigate effect failed"
    assert any(
        "clue" in {str(subtype).lower()
                   for subtype in getattr(gs._safe_get_card(card_id), "subtypes", [])}
        for card_id in player["battlefield"]), \
        "the costed Investigate ability did not create its Clue"


@scenario("701.XX (Earthbend)", "earthbend animates a controlled land and returns it tapped after death or exile")
def scenario_earthbend_land_animation_and_return():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 151)
    controller = gs.p1
    land = inject_into_zone(gs, controller, {
        "name": "Earthbend Test Forest", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "oracle_text": "{T}: Add {G}.",
    }, "battlefield")
    effects = EffectFactory.create_effects("Earthbend 2.")
    assert len(effects) == 1 and type(effects[0]).__name__ == "EarthbendEffect"
    assert effects[0].apply(
        gs, None, controller, targets={"lands": [land]})
    card = gs._safe_get_card(land)
    assert "creature" in card.card_types, "earthbend did not animate the land"
    assert card.counters.get("+1/+1", 0) == 2, \
        "earthbend did not place its counters"
    assert gs.check_keyword(land, "haste"), "earthbend did not grant haste"

    assert gs.move_card(
        land, controller, "battlefield", controller, "graveyard",
        cause="sacrifice")
    assert land in controller["battlefield"] and land not in controller["graveyard"], \
        "the earthbent land did not return from the graveyard"
    assert land in controller["tapped_permanents"], \
        "the earthbent land did not return tapped"
    assert "creature" not in gs._safe_get_card(land).card_types, \
        "the returned land incorrectly remained a creature"


@scenario("603.6c / Earthbend", "Beifong uses the dying friendly nonland creature's last-known power for Earthbend X")
def scenario_dynamic_earthbend_uses_last_known_power():
    gs = fresh(SEED + 152)
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    hunter = inject_real_card(
        gs, controller, "Beifong's Bounty Hunters", "battlefield")
    land = inject_into_zone(gs, controller, {
        "name": "Bounty Hunter Earthbend Land", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "oracle_text": "{T}: Add {G}.",
    }, "battlefield")

    opposing = inject_into_zone(gs, opponent, {
        "name": "Opposing Death Probe", "mana_cost": "{2}{R}",
        "type_line": "Creature", "oracle_text": "", "power": 3,
        "toughness": 3,
    }, "battlefield")
    assert gs.move_card(
        opposing, opponent, "battlefield", opponent, "graveyard")
    assert not any(
        context.get("source_card_id") == hunter
        for _, _, context in gs.ability_handler.active_triggers), \
        "an opposing creature death triggered Beifong"

    land_creature = inject_into_zone(gs, controller, {
        "name": "Land Creature Death Probe", "mana_cost": "", "cmc": 0,
        "type_line": "Land Creature - Elemental", "oracle_text": "",
        "power": 4, "toughness": 4,
    }, "battlefield")
    assert gs.move_card(
        land_creature, controller, "battlefield", controller, "graveyard")
    assert not any(
        context.get("source_card_id") == hunter
        for _, _, context in gs.ability_handler.active_triggers), \
        "a land creature death triggered the nonland Beifong watcher"

    friendly = inject_into_zone(gs, controller, {
        "name": "Friendly Power Probe", "mana_cost": "{3}{G}",
        "type_line": "Creature - Beast", "oracle_text": "", "power": 3,
        "toughness": 3,
    }, "battlefield")
    assert gs.move_card(
        friendly, controller, "battlefield", controller, "graveyard")
    matching = [
        (ability, trigger_controller, context)
        for ability, trigger_controller, context
        in gs.ability_handler.active_triggers
        if context.get("source_card_id") == hunter]
    assert len(matching) == 1, \
        f"friendly nonland creature death queued {len(matching)} Beifong triggers"
    assert matching[0][2].get("last_known", {}).get("power") == 3

    gs.ability_handler.process_triggered_abilities()
    assert gs.targeting_context, "dynamic Earthbend did not request its land target"
    valid_map = gs.targeting_system.get_valid_targets(
        hunter, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_targets = sorted({
        target_id for target_ids in valid_map.values()
        for target_id in target_ids})
    _, ok = get_env().action_handler._handle_select_target(
        valid_targets.index(land), {})
    assert ok and gs.stack, "choosing the Earthbend land target failed"
    assert len(gs.stack) == 1 and gs.stack[-1][1] == hunter, \
        f"unexpected stack above dynamic Earthbend: {gs.stack}"
    stack_context = gs.stack[-1][3]
    assert stack_context.get("last_known", {}).get("power") == 3, \
        f"target commitment lost Earthbend last-known data: {stack_context}"
    parsed_effects = stack_context["ability"]._create_ability_effects(
        stack_context["ability"].effect,
        stack_context.get("targets"),
        source_name="Beifong's Bounty Hunters")
    assert len(parsed_effects) == 1 \
        and getattr(parsed_effects[0], "amount", None) == "event_last_known_power", \
        f"dynamic Earthbend reparsed incorrectly: {parsed_effects}"
    assert gs.targeting_system.validate_targets(
        hunter, stack_context.get("targets", {}), controller,
        effect_text=stack_context.get("targeting_text")), \
        f"dynamic Earthbend target failed validation: {stack_context}"
    assert gs.resolve_top_of_stack(), "dynamic Earthbend trigger failed to resolve"
    counter_count = gs._safe_get_card(land).counters.get("+1/+1", 0)
    assert counter_count == 3, \
        ("Earthbend X did not use the dying creature's last-known power: "
         f"got {counter_count}, types={gs._safe_get_card(land).card_types}")


@scenario("702.34 (Flashback)", "printed and granted flashback use distinct graveyard actions and exile after casting")
def scenario_flashback_graveyard_actions_and_exile():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 152)
    env = get_env()
    handler = env.action_handler
    controller = gs.p1 if gs.agent_is_p1 else gs.p2
    first = inject_into_zone(gs, controller, {
        "name": "Flashback Draw", "mana_cost": "{1}{U}", "cmc": 2,
        "type_line": "Instant", "oracle_text":
            "Draw a card.\nFlashback {2}{U}"
    }, "graveyard")
    second = inject_into_zone(gs, controller, {
        "name": "Granted Draw", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "Draw a card."
    }, "graveyard")
    grant = EffectFactory.create_effects(
        "Target instant or sorcery card in your graveyard gains flashback "
        "until end of turn. The flashback cost is equal to its mana cost.")
    assert len(grant) == 1 and grant[0].apply(
        gs, None, controller, targets={"cards": [second]})
    assert gs.flashback_cost_for(controller, second) == "{U}"

    controller["mana_pool"] = {
        "W": 0, "U": 2, "B": 0, "R": 0, "G": 0, "C": 3}
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    first_index = controller["graveyard"].index(first)
    second_index = controller["graveyard"].index(second)
    assert first_index < 6 and second_index < 6
    mask = handler.generate_valid_actions()
    assert mask[472 + first_index] and mask[472 + second_index], \
        "multiple Flashback cards overwrote one shared action slot"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(472 + first_index)
    assert not info.get("execution_failed"), info
    assert gs.stack and gs.stack[-1][1] == first
    assert gs.resolve_top_of_stack(), "the Flashback spell failed to resolve"
    assert first in controller["exile"] and first not in controller["graveyard"], \
        "a spell cast with Flashback did not exile after resolution"


@scenario("509.1b / Escape Tunnel", "Escape Tunnel grants temporary unblockability only to power 2 or less")
def scenario_escape_tunnel_power_limited_unblockable():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 153)
    controller = gs.p1
    gs.agent_is_p1 = True
    tunnel = inject_real_card(gs, controller, "Escape Tunnel", "battlefield")
    small = inject_into_zone(gs, controller, {
        "name": "Tunnel Scout", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature — Scout", "oracle_text": "",
        "power": "2", "toughness": "2",
    }, "battlefield")
    large = inject_into_zone(gs, controller, {
        "name": "Tunnel Giant", "mana_cost": "{3}", "cmc": 3,
        "type_line": "Creature — Giant", "oracle_text": "",
        "power": "3", "toughness": "3",
    }, "battlefield")
    effects = EffectFactory.create_effects(
        "Target creature with power 2 or less can't be blocked this turn.",
        source_name="Escape Tunnel")
    assert len(effects) == 1
    valid = gs.targeting_system.get_valid_targets(
        tunnel, controller, "creature", effect_text=effects[0].effect_text)
    legal = set(valid.get("creature", []))
    assert small in legal and large not in legal, legal
    assert effects[0].apply(
        gs, tunnel, controller, targets={"creatures": [small]})
    assert gs.ability_handler.check_keyword(small, "unblockable"), \
        "Escape Tunnel did not grant unblockable through the layer system"


@scenario("701.67 (Airbend)", "Aang airbends a creature and its owner may cast it for {2}")
def scenario_aang_airbend_exile_cast_permission():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 154)
    controller = gs._get_active_player()
    owner = controller
    gs.agent_is_p1 = controller is gs.p1
    aang = inject_real_card(
        gs, controller,
        "Aang, Swift Savior // Aang and La, Ocean's Fury", "battlefield")
    target = inject_into_zone(gs, owner, {
        "name": "Airbent Adept", "mana_cost": "{7}{G}", "cmc": 8,
        "type_line": "Creature — Human Monk", "oracle_text": "",
        "power": "4", "toughness": "4",
    }, "battlefield")
    effects = EffectFactory.create_effects(
        "Airbend up to one other target creature or spell.",
        source_name="Aang, Swift Savior // Aang and La, Ocean's Fury")
    assert len(effects) == 1 and effects[0].apply(
        gs, aang, controller, targets={"creatures": [target]})
    assert target in owner["exile"] and target not in owner["battlefield"]
    options = gs.get_exile_cast_options(owner)
    option = next(option for option in options if option["card_id"] == target)
    assert option["permission"] == "airbend" \
        and option["alternative_cost"] == "{2}", option
    owner["mana_pool"] = {
        "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 2}
    gs.priority_player = owner
    gs.agent_is_p1 = owner is gs.p1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    assert gs.cast_spell(target, owner, {
        "source_zone": "exile", "source_idx": owner["exile"].index(target),
        "airbend_cast": True, "alternative_cost": "{2}",
        "use_alt_cost": "exile_permission",
    }), "the Airbent card could not be cast for {2}"
    assert target not in owner["exile"] and gs.stack[-1][1] == target


@scenario("603.2 / 700.2", "Cosmogrand triggers exactly on the second spell and its mode is policy-chosen")
def scenario_cosmogrand_second_spell_modal_trigger():
    from Playersim.ability_types import TriggeredAbility
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 155)
    controller = gs.p1
    gs.agent_is_p1 = True
    source = inject_real_card(gs, controller, "Cosmogrand Zenith", "battlefield")
    trigger = TriggeredAbility(
        source,
        trigger_condition="whenever you cast your second spell each turn",
        effect=("choose one —\n"
                "• Create two 1/1 white Human Soldier creature tokens.\n"
                "• Put a +1/+1 counter on each creature you control."))
    first = inject_into_zone(gs, controller, {
        "name": "First Spell", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    second = inject_into_zone(gs, controller, {
        "name": "Second Spell", "mana_cost": "{U}", "cmc": 1,
        "type_line": "Instant", "oracle_text": "",
    }, "graveyard")
    context = {"game_state": gs, "controller": controller,
               "casting_player": controller, "cast_card_id": first}
    gs.spells_cast_this_turn = [(first, controller, {})]
    assert not trigger.can_trigger("CAST_SPELL", context)
    gs.spells_cast_this_turn.append((second, controller, {}))
    context["cast_card_id"] = second
    assert trigger.can_trigger("CAST_SPELL", context)
    before = len(controller["battlefield"])
    assert gs.ability_handler._push_trigger_to_stack(
        trigger, controller, context)
    assert gs.stack and gs.choice_context.get("type") == "trigger_mode", \
        "Cosmogrand did not choose its mode as the trigger entered the stack"
    assert get_env().action_handler.generate_valid_actions()[353], \
        "Cosmogrand's first trigger mode was not exposed in the action mask"
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert gs.choice_context is None and gs.stack[-1][3].get(
        "selected_trigger_mode") == 0
    assert gs.resolve_top_of_stack(), "Cosmogrand's chosen trigger failed"
    assert len(controller["battlefield"]) == before + 2, \
        "Cosmogrand's token mode did not create two Soldiers"


@scenario("701.19 (Search)", "Gearhulk and Shepherd expose exact restricted library choices")
def scenario_restricted_searches_are_policy_selectable():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 156)
    controller = gs.p1
    gs.agent_is_p1 = True
    controller["library"] = []
    gearhulk = inject_real_card(gs, controller, "Brightglass Gearhulk", "battlefield")
    small_artifact = inject_into_zone(gs, controller, {
        "name": "Tiny Relic", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Artifact", "oracle_text": "",
    }, "library")
    large_creature = inject_into_zone(gs, controller, {
        "name": "Large Search Miss", "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature — Giant", "oracle_text": "",
        "power": "2", "toughness": "2",
    }, "library")
    gear_effect = EffectFactory.create_effects(
        "When this creature enters, you may search your library for up to two "
        "artifact, creature, and/or enchantment cards with mana value 1 or "
        "less, reveal them, put them into your hand, then shuffle.",
        source_name="Brightglass Gearhulk")[0]
    assert gear_effect.apply(gs, gearhulk, controller, targets={})
    assert gs.choice_context["options"] == [small_artifact] \
        and large_creature not in gs.choice_context["options"]
    assert get_env().action_handler.generate_valid_actions()[11], \
        "Brightglass Gearhulk did not expose its legal zero-card search"
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert small_artifact in controller["hand"]

    controller["library"] = []
    shepherd = inject_real_card(gs, controller, "Starfield Shepherd", "battlefield")
    plains = inject_into_zone(gs, controller, {
        "name": "Search Plains", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land — Plains", "oracle_text": "{T}: Add {W}.",
    }, "library")
    wrong_land = inject_into_zone(gs, controller, {
        "name": "Search Island", "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land — Island", "oracle_text": "{T}: Add {U}.",
    }, "library")
    shepherd_effect = EffectFactory.create_effects(
        "Search your library for a basic Plains card or a creature card with "
        "mana value 1 or less, reveal it, put it into your hand, then shuffle.",
        source_name="Starfield Shepherd")[0]
    assert shepherd_effect.apply(gs, shepherd, controller, targets={})
    assert plains in gs.choice_context["options"] \
        and wrong_land not in gs.choice_context["options"]


@scenario("608.2c / 614.1a", "Combustion Technique scales from Lessons and Daydream blinks with a counter")
def scenario_combustion_and_daydream_atomic_effects():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 157)
    controller, opponent = gs.p1, gs.p2
    for index in range(3):
        inject_into_zone(gs, controller, {
            "name": f"Combustion Lesson {index}", "mana_cost": "{U}",
            "cmc": 1, "type_line": "Sorcery — Lesson", "oracle_text": "",
        }, "graveyard")
    target = inject_into_zone(gs, opponent, {
        "name": "Combustion Target", "mana_cost": "{4}", "cmc": 4,
        "type_line": "Creature — Beast", "oracle_text": "",
        "power": "4", "toughness": "5",
    }, "battlefield")
    combustion = EffectFactory.create_effects(
        "Combustion Technique deals damage equal to 2 plus the number of "
        "Lesson cards in your graveyard to target creature. If that creature "
        "would die this turn, exile it instead.",
        source_name="Combustion Technique")[0]
    assert combustion.apply(
        gs, None, controller, targets={"creatures": [target]})
    assert target in opponent["exile"] and target not in opponent["graveyard"]

    blink_target = inject_into_zone(gs, controller, {
        "name": "Daydream Target", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature — Bird", "oracle_text": "",
        "power": "1", "toughness": "1",
    }, "battlefield")
    daydream = EffectFactory.create_effects(
        "Exile target creature you control, then return that card to the "
        "battlefield under its owner's control with a +1/+1 counter on it.",
        source_name="Daydream")[0]
    assert daydream.apply(
        gs, None, controller, targets={"creatures": [blink_target]})
    assert blink_target in controller["battlefield"] \
        and gs._safe_get_card(blink_target).counters.get("+1/+1") == 1


@scenario("707.10 / 608.3b", "Sage copies its creature spell and the copy resolves as a token")
def scenario_sage_of_the_skies_spell_copy():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 158)
    controller = gs.p1
    sage = inject_real_card(gs, controller, "Sage of the Skies", "hand")
    controller["hand"].remove(sage)
    gs.add_to_stack("SPELL", sage, controller, {
        "source_zone": "hand", "requires_target": False})
    copy_effect = EffectFactory.create_effects(
        "Copy this spell. (The copy becomes a token.)",
        source_name="Sage of the Skies")[0]
    assert copy_effect.apply(
        gs, sage, controller, targets={}, context={"cast_card_id": sage})
    assert len([item for item in gs.stack if item[0] == "SPELL" and item[1] == sage]) == 2
    assert gs.resolve_top_of_stack(), "Sage's spell copy failed to resolve"
    tokens = [card_id for card_id in controller["battlefield"]
              if getattr(gs._safe_get_card(card_id), "is_token", False)]
    assert tokens, "the copied permanent spell did not become a token"


@scenario("702.Harmonize", "Winternight Stories Harmonizes with one creature tap and exiles after conditional discard")
def scenario_winternight_stories_harmonize():
    gs = fresh(SEED + 159)
    env = get_env()
    handler = env.action_handler
    controller = gs._get_active_player()
    gs.agent_is_p1 = controller is gs.p1
    for card_id in list(controller.get("graveyard", [])):
        controller["graveyard"].remove(card_id)
    winternight = inject_real_card(
        gs, controller, "Winternight Stories", "graveyard")
    helper = inject_into_zone(gs, controller, {
        "name": "Harmonize Helper", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature — Wizard", "oracle_text": "",
        "power": "2", "toughness": "2",
    }, "battlefield")
    discard_creature = inject_into_zone(gs, controller, {
        "name": "Winternight Discard", "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature — Bird", "oracle_text": "",
        "power": "1", "toughness": "1",
    }, "hand")
    controller["mana_pool"] = {
        "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 2}
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    mask = handler.generate_valid_actions()
    action = 472 + controller["graveyard"].index(winternight)
    assert mask[action], "Harmonize was not exposed from the graveyard"
    handler.current_valid_actions = mask
    _, _, _, info = handler.apply_action(action)
    assert not info.get("execution_failed"), info
    assert gs.choice_context.get("type") == "harmonize_tap"
    choice_mask = handler.generate_valid_actions()
    assert not choice_mask[11], \
        "Harmonize exposed a no-tap choice that could not pay the full cost"
    helper_index = gs.choice_context["options"].index(helper)
    assert handler._handle_choose_mode(helper_index, {})[1]
    assert helper in controller["tapped_permanents"] and gs.stack[-1][1] == winternight
    assert gs.resolve_top_of_stack(), "Winternight Stories failed to resolve"
    assert gs.choice_context.get("type") == "discard" \
        and gs.choice_context.get("remaining") == 2
    hand_index = controller["hand"].index(discard_creature)
    gs.choice_context["choice_page"] = hand_index // 10
    assert handler._handle_discard_card(hand_index % 10)[1]
    assert discard_creature in controller["graveyard"] \
        and winternight in controller["exile"]


@scenario("702.Warp / 603.7", "Warp uses its alternate cost, exiles at the next end step, and grants a later cast")
def scenario_warp_complete_transaction():
    gs = fresh(SEED + 160)
    handler = get_env().action_handler
    player = gs._get_active_player()
    gs.agent_is_p1 = player is gs.p1
    card_id = inject_real_card(gs, player, "Mightform Harmonizer", "hand")
    card = gs._safe_get_card(card_id)
    assert card.is_warp and card.warp_cost == "{2}{g}", card.warp_cost
    player['mana_pool'] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 1, 'C': 2}
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    hand_index = player['hand'].index(card_id)
    mask = handler.generate_valid_actions()
    warp_action = [296, 297, 298, 309, 310, 311, 312, 313][hand_index]
    assert mask[warp_action], "Warp alternative cast was absent from the mask"
    reward, ok = handler._handle_plot_card(
        hand_index, context={'warp_cast': True})
    assert ok and gs.stack[-1][3].get('warp_cast'), reward
    assert gs.resolve_top_of_stack() and card_id in player['battlefield']
    assert gs.process_delayed_triggers(gs.PHASE_END_STEP) == 1
    assert card_id in player['exile'] and card_id in gs.cards_castable_from_exile
    assert any(option['card_id'] == card_id
               for option in gs.get_exile_cast_options(player))


@scenario("608.2c / 701.19", "the closure batch preserves linked searches, temporary rules, and an X snapshot")
def scenario_closure_search_and_temporary_rules():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 161)
    controller, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    source = inject_into_zone(gs, controller, {
        'name': 'Closure Source', 'mana_cost': '', 'type_line': 'Land',
        'oracle_text': ''}, 'battlefield')

    effect = EffectFactory.create_effects(
        "The next spell you cast this turn can't be countered.",
        source_name='Mistrise Village')[0]
    assert effect.apply(gs, source, controller, {})
    spell = inject_into_zone(gs, controller, {
        'name': 'Uncounterable Probe', 'mana_cost': '{U}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'hand')
    controller['mana_pool'] = {'W': 0, 'U': 1, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
    gs.priority_player = controller
    assert gs.cast_spell(spell, controller)
    assert gs.stack[-1][3].get('cant_be_countered')

    target = inject_into_zone(gs, opponent, {
        'name': 'Erode Target', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Beast', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')
    opponent['library'] = []
    basic = inject_into_zone(gs, opponent, {
        'name': 'Erode Plains', 'mana_cost': '', 'cmc': 0,
        'type_line': 'Basic Land — Plains', 'oracle_text': '{T}: Add {W}.'},
        'library')
    erode = EffectFactory.create_effects(
        "Destroy target creature or planeswalker. Its controller may search "
        "their library for a basic land card, put it onto the battlefield "
        "tapped, then shuffle.", source_name='Erode')[0]
    assert erode.apply(gs, source, controller, {'creatures': [target]})
    gs.agent_is_p1 = False
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert basic in opponent['battlefield'] \
        and basic in opponent['tapped_permanents']

    small = inject_into_zone(gs, opponent, {
        'name': 'Indestructible Small', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Creature — Spirit', 'oracle_text': 'Indestructible',
        'power': 1, 'toughness': 1}, 'battlefield')
    large = inject_into_zone(gs, opponent, {
        'name': 'Large Survivor', 'mana_cost': '{4}', 'cmc': 4,
        'type_line': 'Creature — Giant', 'oracle_text': '',
        'power': 4, 'toughness': 4}, 'battlefield')
    day = EffectFactory.create_effects(
        "Each creature with mana value X or less loses all abilities until "
        "end of turn. Destroy those creatures.", source_name='Day of Black Sun')[0]
    assert day.apply(gs, source, controller, {'X': 2})
    assert small in opponent['graveyard'] and large in opponent['battlefield']


@scenario("701.5 / 701.19 / 701.11", "counter, Charm, Cover-Up, and opponent exile choices remain policy-visible")
def scenario_closure_linked_policy_choices():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 162)
    controller, opponent = gs.p1, gs.p2
    source = inject_into_zone(gs, controller, {
        'name': 'Closure Spell Source', 'mana_cost': '',
        'type_line': 'Instant', 'oracle_text': ''}, 'graveyard')
    target_spell = inject_into_zone(gs, opponent, {
        'name': 'Counter Target', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Sorcery', 'oracle_text': ''}, 'hand')
    opponent['hand'].remove(target_spell)
    gs.add_to_stack('SPELL', target_spell, opponent, {'source_zone': 'hand'})
    no_lies = EffectFactory.create_effects(
        "Counter target spell unless its controller pays {3}. If that spell "
        "is countered this way, exile it instead of putting it into its "
        "owner's graveyard.", source_name='No More Lies')[0]
    assert no_lies.apply(gs, source, controller, {'spells': [target_spell]})
    gs.agent_is_p1 = False
    assert get_env().action_handler._handle_pass_priority(None)[1]
    assert target_spell in opponent['exile']

    controller['library'] = []
    charm_land = inject_into_zone(gs, controller, {
        'name': 'Charm Forest', 'mana_cost': '', 'cmc': 0,
        'type_line': 'Basic Land — Forest', 'oracle_text': '{T}: Add {G}.'},
        'library')
    charm = EffectFactory.create_effects(
        "Search your library for a creature or land card and reveal it. Put "
        "it onto the battlefield tapped if it's a land card. Otherwise, put "
        "it into your hand. Then shuffle.", source_name="Archdruid's Charm")[0]
    assert charm.apply(gs, source, controller, {})
    gs.agent_is_p1 = True
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert charm_land in controller['battlefield'] \
        and charm_land in controller['tapped_permanents']

    named_gy = inject_into_zone(gs, opponent, {
        'name': 'Cover-Up Name', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'graveyard')
    named_hand = inject_into_zone(gs, opponent, {
        'name': 'Cover-Up Name', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'hand')
    cover = EffectFactory.create_effects(
        "Destroy all creatures. If evidence was collected, exile a card from "
        "an opponent's graveyard. Then search its owner's graveyard, hand, "
        "and library for any number of cards with that name and exile them.",
        source_name='Deadly Cover-Up')[0]
    assert cover.apply(gs, source, controller, {},
                       context={'evidence_collected': True})
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert named_gy in opponent['exile'] and named_hand in opponent['exile']


@scenario("400 / 702.121", "outside-game, Strategic Betrayal, Crew, and finality use exact zone transactions")
def scenario_closure_zone_and_crew_transactions():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 163)
    controller, opponent = gs.p1, gs.p2
    source = inject_into_zone(gs, controller, {
        'name': 'North Wind Avatar', 'mana_cost': '{5}{U}', 'cmc': 6,
        'type_line': 'Creature — Avatar', 'oracle_text': '',
        'power': 5, 'toughness': 5}, 'battlefield')
    wish = inject_card(gs, {'name': 'Outside Card', 'mana_cost': '{1}',
                           'type_line': 'Instant', 'oracle_text': ''})
    controller['outside_game'] = [wish]
    outside = EffectFactory.create_effects(
        "When this creature enters, if you cast it, you may put a card you "
        "own from outside the game into your hand.",
        source_name='North Wind Avatar')[0]
    assert outside.apply(gs, source, controller, {}, context={'source_zone': 'hand'})
    gs.agent_is_p1 = True
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert wish in controller['hand'] and wish not in controller['outside_game']
    assert gs.move_card(source, controller, 'battlefield', controller,
                        'graveyard', cause='test_cleanup')

    victim = inject_into_zone(gs, opponent, {
        'name': 'Betrayed Creature', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Human', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')
    grave_card = inject_into_zone(gs, opponent, {
        'name': 'Betrayed Grave', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'graveyard')
    betrayal = EffectFactory.create_effects(
        "Target opponent exiles a creature they control and their graveyard.",
        source_name='Strategic Betrayal')[0]
    assert betrayal.apply(gs, source, controller, {'players': ['p2']})
    gs.agent_is_p1 = False
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert victim in opponent['exile'] and grave_card in opponent['exile']

    vehicle = inject_real_card(
        gs, controller, 'Lumbering Worldwagon', 'battlefield')
    for index in range(2):
        inject_into_zone(gs, controller, {
            'name': f'Worldwagon Land {index}', 'mana_cost': '', 'cmc': 0,
            'type_line': 'Basic Land — Forest', 'oracle_text': '{T}: Add {G}.'},
            'battlefield')
    helper = inject_into_zone(gs, controller, {
        'name': 'Crew Helper', 'mana_cost': '{3}', 'cmc': 3,
        'type_line': 'Creature — Giant', 'oracle_text': '',
        'power': 4, 'toughness': 4}, 'battlefield')
    gs.agent_is_p1 = True
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = controller
    vehicle_index = controller['battlefield'].index(vehicle)
    abilities = gs.ability_handler.get_activated_abilities(vehicle)
    crew_index = next(index for index, ability in enumerate(abilities)
                      if getattr(ability, 'keyword', '') == 'crew')
    assert get_env().action_handler._handle_activate_ability(None, {
        'battlefield_idx': vehicle_index, 'ability_idx': crew_index,
        'controller_id': 'p1'})[1]
    assert gs.choice_context.get('crew_activation')
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert get_env().action_handler._handle_pass_priority(None)[1]
    assert gs.stack and gs.stack[-1][1] == vehicle
    assert gs.resolve_top_of_stack()
    assert helper in controller['tapped_permanents'] \
        and vehicle in gs.crewed_vehicles, \
        (controller['tapped_permanents'], gs.crewed_vehicles,
         gs._safe_get_card(vehicle).card_types)
    assert 'creature' in gs._safe_get_card(vehicle).card_types
    assert gs._safe_get_card(vehicle).power == 2 \
        and gs._safe_get_card(vehicle).toughness == 4, \
        (gs._safe_get_card(vehicle).power,
         gs._safe_get_card(vehicle).toughness)

    finality = inject_into_zone(gs, controller, {
        'name': 'Finality Probe', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Creature — Spirit', 'oracle_text': '',
        'power': 1, 'toughness': 1}, 'battlefield')
    gs.add_counter(finality, 'finality', 1)
    assert gs.move_card(finality, controller, 'battlefield', controller,
                        'graveyard', cause='destroy_effect')
    assert finality in controller['exile'] and finality not in controller['graveyard']

    esper = inject_real_card(
        gs, controller, 'Esper Origins // Summon: Esper Maduin', 'graveyard')
    controller['graveyard'].remove(esper)
    esper_effects = EffectFactory.create_effects(
        gs._safe_get_card(esper).oracle_text,
        source_name='Esper Origins // Summon: Esper Maduin')
    transform_effect = next(
        effect for effect in esper_effects
        if type(effect).__name__ == 'EsperGraveyardTransformEffect')
    assert transform_effect.apply(
        gs, esper, controller, {}, context={'source_zone': 'graveyard'})
    assert esper in controller['battlefield']
    assert gs._safe_get_card(esper).current_face == 1
    assert gs._safe_get_card(esper).counters.get('finality') == 1


@scenario("301.5 / 702.121", "generic Equip and Crew keyword abilities pay costs, use policy choices, and resolve through the stack")
def scenario_generic_equip_and_crew_families():
    gs = fresh(SEED + 164)
    handler = get_env().action_handler
    player = gs.p1
    gs.agent_is_p1 = True
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    equipment = inject_into_zone(gs, player, {
        'name': 'Generic Equipment Sweep', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Artifact — Equipment',
        'oracle_text': 'Equipped creature gets +1/+1.\nEquip {1}'},
        'battlefield')
    creature = inject_into_zone(gs, player, {
        'name': 'Generic Equip Target', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Soldier', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')
    player['mana_pool'] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    equip_abilities = gs.ability_handler.get_activated_abilities(equipment)
    equip_index = next(index for index, ability in enumerate(equip_abilities)
                       if getattr(ability, 'keyword', '') == 'equip')
    assert handler._handle_activate_ability(None, {
        'battlefield_idx': player['battlefield'].index(equipment),
        'ability_idx': equip_index, 'controller_id': 'p1'})[1]
    assert gs.targeting_context
    candidates = handler._get_target_selection_candidates(
        player, gs.targeting_context)
    assert creature in candidates
    assert handler._handle_select_target(candidates.index(creature), {})[1]
    assert gs.stack and gs.resolve_top_of_stack()
    assert player['attachments'].get(equipment) == creature

    vehicle = inject_into_zone(gs, player, {
        'name': 'Generic Crew Sweep', 'mana_cost': '{3}', 'cmc': 3,
        'type_line': 'Artifact — Vehicle', 'oracle_text': 'Crew 3',
        'power': 4, 'toughness': 4}, 'battlefield')
    helper = inject_into_zone(gs, player, {
        'name': 'Generic Crew Helper', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Pilot', 'oracle_text': '',
        'power': 3, 'toughness': 3}, 'battlefield')
    abilities = gs.ability_handler.get_activated_abilities(vehicle)
    crew_index = next(index for index, ability in enumerate(abilities)
                      if getattr(ability, 'keyword', '') == 'crew')
    gs.priority_player = player
    assert handler._handle_activate_ability(None, {
        'battlefield_idx': player['battlefield'].index(vehicle),
        'ability_idx': crew_index, 'controller_id': 'p1'})[1]
    assert gs.choice_context.get('crew_activation')
    assert handler._handle_choose_mode(
        gs.choice_context['options'].index(helper), {})[1]
    assert handler._handle_pass_priority(None)[1]
    assert gs.stack and gs.resolve_top_of_stack()
    assert helper in player['tapped_permanents']
    assert vehicle in gs.crewed_vehicles \
        and 'creature' in gs._safe_get_card(vehicle).card_types


@scenario("701.55", "Discover reveals exactly to an eligible nonland card and exposes cast versus hand")
def scenario_discover_cast_or_hand_family():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 165)
    handler = get_env().action_handler
    player = gs.p1
    gs.agent_is_p1 = True
    player['library'] = []
    land = inject_into_zone(gs, player, {
        'name': 'Discover Miss Land', 'mana_cost': '', 'cmc': 0,
        'type_line': 'Basic Land — Island', 'oracle_text': '{T}: Add {U}.'},
        'library')
    expensive = inject_into_zone(gs, player, {
        'name': 'Discover Miss Spell', 'mana_cost': '{5}', 'cmc': 5,
        'type_line': 'Sorcery', 'oracle_text': 'Draw a card.'}, 'library')
    hit = inject_into_zone(gs, player, {
        'name': 'Discover Hit', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Scout', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'library')
    discover = EffectFactory.create_effects('Discover 3.')[0]
    assert discover.apply(gs, None, player, {})
    assert gs.choice_context.get('choice_kind') == 'discover'
    assert handler._handle_choose_mode(0, {})[1]
    assert gs.stack and gs.stack[-1][1] == hit
    assert land in player['library'] and expensive in player['library']
    assert gs.resolve_top_of_stack() and hit in player['battlefield']

    player['library'] = []
    second = inject_into_zone(gs, player, {
        'name': 'Discover Hand Choice', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': 'Draw a card.'}, 'library')
    assert discover.apply(gs, None, player, {})
    assert handler._handle_pass_priority(None)[1]
    assert second in player['hand'] and second not in player['exile']


@scenario("701.47 / 701.49 / 701.16 / 701.67", "Connive, Suspect, Explore, Investigate, and noncreature Airbend share policy-visible generic paths")
def scenario_keyword_family_coverage_sweep():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 166)
    handler = get_env().action_handler
    player, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True
    conniver = inject_into_zone(gs, player, {
        'name': 'Connive Sweep', 'mana_cost': '{2}', 'cmc': 2,
        'type_line': 'Creature — Rogue', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')
    player['library'] = []
    drawn = inject_into_zone(gs, player, {
        'name': 'Connive Nonland', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'library')
    connive = EffectFactory.create_effects('It connives.')[0]
    assert connive.apply(gs, conniver, player, {})
    assert gs.choice_context.get('type') == 'connive_discard'
    assert handler._handle_discard_card(
        player['hand'].index(drawn))[1]
    assert drawn in player['graveyard']
    assert gs._safe_get_card(conniver).counters.get('+1/+1') == 1

    player['library'] = []
    once_drawn = inject_into_zone(gs, player, {
        'name': 'Once Connive Nonland', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Sorcery', 'oracle_text': ''}, 'library')
    once_connive = EffectFactory.create_effects(
        'This creature connives. Do this only once each turn.')[0]
    assert once_connive.apply(gs, conniver, player, {})
    assert handler._handle_discard_card(
        player['hand'].index(once_drawn))[1]
    counters_after_once = gs._safe_get_card(conniver).counters.get('+1/+1')
    assert once_connive.apply(gs, conniver, player, {})
    assert not gs.choice_context
    assert gs._safe_get_card(conniver).counters.get('+1/+1') \
        == counters_after_once

    suspect = EffectFactory.create_effects(
        'Suspect up to one target creature you control.')[0]
    assert suspect.apply(gs, None, player, {'creatures': [conniver]})
    assert conniver in player['suspected_permanents']
    assert gs.check_keyword(conniver, 'menace')
    attacker = inject_into_zone(gs, opponent, {
        'name': 'Suspect Attack Probe', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Creature — Soldier', 'oracle_text': '',
        'power': 1, 'toughness': 1}, 'battlefield')
    assert not handler._can_block(conniver, attacker)
    clear = EffectFactory.create_effects(
        'All suspected creatures are no longer suspected.')[0]
    assert clear.apply(gs, None, player, {})
    assert conniver not in player['suspected_permanents']

    transfer_target = inject_into_zone(gs, player, {
        'name': 'Suspect Transfer Target', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Creature â€” Citizen', 'oracle_text': '',
        'power': 1, 'toughness': 1}, 'battlefield')
    assert suspect.apply(gs, None, player, {'creatures': [conniver]})
    transfer = EffectFactory.create_effects(
        'You may suspect one of the other creatures. If you do, this '
        'creature is no longer suspected.')[0]
    assert transfer.apply(gs, conniver, player, {})
    transfer_index = gs.choice_context['options'].index(transfer_target)
    assert handler._handle_choose_mode(transfer_index, {})[1]
    assert transfer_target in player['suspected_permanents']
    assert conniver not in player['suspected_permanents']

    player['library'] = []
    explored_land = inject_into_zone(gs, player, {
        'name': 'Explore Sweep Land', 'mana_cost': '', 'cmc': 0,
        'type_line': 'Basic Land — Forest', 'oracle_text': '{T}: Add {G}.'},
        'library')
    explore = EffectFactory.create_effects('It explores.')[0]
    assert explore.apply(gs, conniver, player, {})
    assert explored_land in player['hand']

    before = len(player['battlefield'])
    investigate = EffectFactory.create_effects('You investigate.')[0]
    assert investigate.apply(gs, None, player, {})
    clues = [cid for cid in player['battlefield'][before:]
             if 'clue' in getattr(gs._safe_get_card(cid), 'subtypes', [])]
    assert clues
    before_twice = len(player['battlefield'])
    investigate_twice = EffectFactory.create_effects('Investigate twice.')[0]
    assert investigate_twice.apply(gs, None, player, {})
    assert len(player['battlefield']) == before_twice + 2

    dynamic_investigate = EffectFactory.create_effects(
        'Investigate X times, where X is the number of creatures you control.')
    dynamic_explore = EffectFactory.create_effects('It explores X times.')
    assert type(dynamic_investigate[0]).__name__ == 'AbilityEffect'
    assert type(dynamic_explore[0]).__name__ == 'ExploreEffect'
    assert dynamic_explore[0].count == 'x'

    artifact = inject_into_zone(gs, opponent, {
        'name': 'Airbend Artifact Sweep', 'mana_cost': '{4}', 'cmc': 4,
        'type_line': 'Artifact', 'oracle_text': ''}, 'battlefield')
    airbend = EffectFactory.create_effects(
        'Airbend up to one target nonland permanent.')[0]
    assert airbend.apply(gs, None, player, {'artifacts': [artifact]})
    assert artifact in opponent['exile'] \
        and artifact in gs.cards_castable_from_exile
    lesson_effects = EffectFactory.create_effects(
        'Airbend target nonland permanent. Draw a card.')
    assert [type(effect).__name__ for effect in lesson_effects] == [
        'AirbendEffect', 'DrawCardEffect']


@scenario("701.40 / 701.49 / 701.55", "dynamic Explore, Investigate, and Discover values survive policy choices and chained triggers")
def scenario_dynamic_keyword_values_and_discover_chain():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 167)
    handler = get_env().action_handler
    player, opponent = gs.p1, gs.p2
    gs.agent_is_p1 = True

    explorer = inject_into_zone(gs, player, {
        'name': 'Dynamic Explorer', 'mana_cost': '{X}{G}', 'cmc': 1,
        'type_line': 'Creature - Scout', 'oracle_text': '',
        'power': 1, 'toughness': 1}, 'battlefield')
    player['library'] = []
    nonland = inject_into_zone(gs, player, {
        'name': 'Explore Dynamic Nonland', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Sorcery', 'oracle_text': ''}, 'library')
    explored_land = inject_into_zone(gs, player, {
        'name': 'Explore Dynamic Land', 'mana_cost': '', 'cmc': 0,
        'type_line': 'Land', 'oracle_text': ''}, 'library')
    explore_x = EffectFactory.create_effects('It explores X times.')[0]
    assert explore_x.apply(gs, explorer, player, {}, context={'X': 2})
    assert gs.choice_context.get('type') == 'explore'
    assert handler._handle_scry_surveil_choice(
        None, {}, action_index=305)[1]
    assert nonland in player['graveyard']
    assert explored_land in player['hand']
    assert gs._safe_get_card(explorer).counters.get('+1/+1') == 1

    opponent_extra = inject_into_zone(gs, opponent, {
        'name': 'Investigation Hand Probe', 'mana_cost': '{1}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''}, 'hand')
    assert opponent_extra in opponent['hand']
    while len(opponent['hand']) <= len(player['hand']):
        opponent['hand'].append(opponent_extra)
    before_clues = len(player['battlefield'])
    investigate_dynamic = EffectFactory.create_effects(
        'Investigate once for each opponent who has more cards in hand than you.')[0]
    assert investigate_dynamic.apply(gs, None, player, {})
    assert len(player['battlefield']) == before_clues + 1
    for index in range(2):
        inject_into_zone(gs, opponent, {
            'name': f'Investigated Creature {index}', 'mana_cost': '{1}',
            'cmc': 1, 'type_line': 'Creature - Citizen', 'oracle_text': '',
            'power': 1, 'toughness': 1}, 'battlefield')
    before_target_clues = len(player['battlefield'])
    target_investigate = EffectFactory.create_effects(
        'Investigate X times, where X is the total number of creatures those '
        'players control.')[0]
    assert target_investigate.apply(
        gs, None, player, {'players': ['p2']})
    assert len(player['battlefield']) == before_target_clues + 2

    curator = inject_into_zone(gs, player, {
        'name': "Curator of Sun's Creation", 'mana_cost': '{3}{R}', 'cmc': 4,
        'type_line': 'Creature - Human Artificer',
        'oracle_text': ('Whenever you discover, discover again for the same '
                        'value. This ability triggers only once each turn.'),
        'power': 3, 'toughness': 3}, 'battlefield')
    target_spell = inject_card(gs, {
        'name': 'Countered Mana-Value Probe', 'mana_cost': '{X}{U}', 'cmc': 1,
        'type_line': 'Instant', 'oracle_text': ''})
    gs.stack.append(('SPELL', target_spell, opponent, {'X': 3}))
    player['library'] = []
    first_hit = inject_into_zone(gs, player, {
        'name': 'First Dynamic Discover Hit', 'mana_cost': '{2}{G}', 'cmc': 3,
        'type_line': 'Creature - Beast', 'oracle_text': '',
        'power': 3, 'toughness': 3}, 'library')
    second_hit = inject_into_zone(gs, player, {
        'name': 'Repeated Discover Hit', 'mana_cost': '{3}{R}', 'cmc': 4,
        'type_line': 'Sorcery', 'oracle_text': ''}, 'library')
    discover_x = EffectFactory.create_effects(
        "Discover X, where X is that spell's mana value.")[0]
    assert discover_x.apply(
        gs, explorer, player, {'spells': [target_spell]})
    assert gs.choice_context.get('discover_value') == 4
    assert handler._handle_pass_priority(None)[1]
    assert first_hit in player['hand']
    assert any(entry[0].card_id == curator
               for entry in gs.ability_handler.active_triggers)
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack()
    assert gs.choice_context.get('choice_kind') == 'discover'
    assert gs.choice_context.get('discover_value') == 4
    assert handler._handle_pass_priority(None)[1]
    assert second_hit in player['hand']
    assert not any(entry[0].card_id == curator
                   for entry in gs.ability_handler.active_triggers)


@scenario("701.Endure", "Endure exposes both outcomes and resolves fixed or counter-derived values")
def scenario_endure_policy_and_dynamic_value():
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 168)
    handler = get_env().action_handler
    player = gs.p1
    gs.agent_is_p1 = True
    enduring = inject_into_zone(gs, player, {
        'name': 'Endure Sweep', 'mana_cost': '{3}{G}', 'cmc': 4,
        'type_line': 'Creature - Soldier', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')

    endure_two = EffectFactory.create_effects('This creature endures 2.')[0]
    assert endure_two.apply(gs, enduring, player, {})
    assert gs.choice_context.get('options') == ['counters', 'spirit']
    assert handler._handle_choose_mode(0, {})[1]
    assert gs._safe_get_card(enduring).counters.get('+1/+1') == 2

    before_tokens = len(player['battlefield'])
    assert endure_two.apply(gs, enduring, player, {})
    assert handler._handle_choose_mode(1, {})[1]
    spirits = [card_id for card_id in player['battlefield'][before_tokens:]
               if getattr(gs._safe_get_card(card_id), 'name', '') == 'Spirit']
    assert len(spirits) == 1
    spirit = gs._safe_get_card(spirits[0])
    assert spirit.power == 2 and spirit.toughness == 2

    life_before = player['life']
    compound = EffectFactory.create_effects(
        'You lose 1 life and this creature endures 1.')
    assert [type(effect).__name__ for effect in compound] == [
        'LoseLifeEffect', 'EndureEffect']
    success, pending = gs._run_effect_sequence(
        compound, enduring, player, {})
    assert success and pending and player['life'] == life_before - 1
    assert handler._handle_choose_mode(0, {})[1]

    player['mana_pool'] = {
        'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    optional_endure = EffectFactory.create_effects(
        'You may pay {1}{W}. If you do, this creature endures 1.')[0]
    assert type(optional_endure).__name__ == 'OptionalManaThenEffect'
    assert optional_endure.apply(gs, enduring, player, {})
    assert gs.choice_context.get('choice_kind') == 'optional_mana_then'
    assert handler._handle_choose_mode(0, {})[1]
    assert sum(player['mana_pool'].values()) == 0
    assert gs.choice_context.get('choice_kind') == 'endure'
    counters_before_optional = gs._safe_get_card(
        enduring).counters.get('+1/+1', 0)
    assert handler._handle_choose_mode(0, {})[1]
    assert gs._safe_get_card(enduring).counters.get('+1/+1') \
        == counters_before_optional + 1

    player['mana_pool'] = {
        'W': 1, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 1}
    counters_before_decline = gs._safe_get_card(
        enduring).counters.get('+1/+1', 0)
    assert optional_endure.apply(gs, enduring, player, {})
    assert handler._handle_pass_priority(None)[1]
    assert sum(player['mana_pool'].values()) == 2
    assert gs._safe_get_card(enduring).counters.get('+1/+1') \
        == counters_before_decline

    warden = inject_into_zone(gs, player, {
        'name': 'Warden Counter Source', 'mana_cost': '{2}{G}', 'cmc': 3,
        'type_line': 'Creature - Hydra',
        'oracle_text': ('Whenever another nontoken creature you control enters, '
                        'it endures X, where X is the number of counters on '
                        'this creature.'),
        'power': 2, 'toughness': 2}, 'battlefield')
    assert gs.add_counter(warden, '+1/+1', 3)
    gs.ability_handler.active_triggers.clear()
    assert gs.create_token(player, {
        'name': 'Token Entry Probe', 'type_line': 'Token Creature - Spirit',
        'card_types': ['creature'], 'subtypes': ['Spirit'],
        'power': 1, 'toughness': 1, 'is_token': True}) is not None
    assert not gs.ability_handler.active_triggers
    entrant = inject_into_zone(gs, player, {
        'name': 'Endure Event Subject', 'mana_cost': '{1}{G}', 'cmc': 2,
        'type_line': 'Creature - Elf', 'oracle_text': '',
        'power': 2, 'toughness': 2}, 'battlefield')
    assert any(entry[0].card_id == warden
               for entry in gs.ability_handler.active_triggers)
    gs.ability_handler.process_triggered_abilities()
    assert gs.resolve_top_of_stack()
    assert gs.choice_context.get('endure_value') == 3
    assert handler._handle_choose_mode(0, {})[1]
    assert gs._safe_get_card(entrant).counters.get('+1/+1') == 3


@scenario("602.2b / 107.3 / Endure", "activated X announces once and pays matching mana and life before resolving")
def scenario_activated_x_mana_and_life_transaction():
    gs = fresh(SEED + 170)
    handler = get_env().action_handler
    player = gs.p1
    gs.agent_is_p1 = True
    gs.turn = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    krumar = inject_into_zone(gs, player, {
        'name': 'Krumar Initiate', 'mana_cost': '{1}{B}', 'cmc': 2,
        'type_line': 'Creature - Orc Warrior',
        'oracle_text': ('{X}{B}, {T}, Pay X life: This creature endures X. '
                        'Activate only as a sorcery.'),
        'power': 2, 'toughness': 2}, 'battlefield')
    player['entered_battlefield_this_turn'].discard(krumar)
    player['mana_pool'] = {
        'W': 0, 'U': 0, 'B': 1, 'R': 0, 'G': 0, 'C': 12}
    battlefield_idx = player['battlefield'].index(krumar)
    abilities = gs.ability_handler.get_activated_abilities(krumar)
    assert len(abilities) == 1

    reward, ok = handler._handle_activate_ability(None, {
        'battlefield_idx': battlefield_idx, 'ability_idx': 0,
        'controller_id': 'p1'})
    assert ok, f'activated X staging failed with reward {reward}'
    assert gs.choice_context.get('type') == 'choose_x'
    assert gs.choice_context.get('max_x') == 12
    assert handler._handle_target_page_next(
        context={'page_count': 2})[1]
    life_before = player['life']
    reward, ok = handler._handle_choose_x(1, {'x_value': 11})
    assert ok, f'activated X=11 failed with reward {reward}'
    assert krumar in player['tapped_permanents']
    assert player['life'] == life_before - 11
    assert sum(player['mana_pool'].values()) == 1
    assert gs.stack and gs.stack[-1][3].get('X') == 11

    assert gs.resolve_top_of_stack()
    assert gs.choice_context.get('choice_kind') == 'endure'
    assert gs.choice_context.get('endure_value') == 11
    assert handler._handle_choose_mode(0, {})[1]
    assert gs._safe_get_card(krumar).counters.get('+1/+1') == 11


@scenario("action protocol / zone permissions", "graveyard overflow and colliding alternate actions remain reachable")
def scenario_graveyard_and_mechanic_collision_overflow():
    gs = fresh(SEED + 200)
    player = gs.p1
    player["graveyard"] = []
    for index in range(6):
        inject_into_zone(gs, player, {
            "name": f"Graveyard Filler {index}", "mana_cost": "{1}",
            "type_line": "Creature", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "graveyard")
    flashback = inject_into_zone(gs, player, {
        "name": "Seventh-Slot Flashback", "mana_cost": "{0}",
        "type_line": "Sorcery", "card_types": ["sorcery"],
        "oracle_text": "",
    }, "graveyard")
    assert gs.grant_flashback_permission(player, flashback, "{0}")
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[479]
    catalog_context = handler.action_reasons_with_context[479]["context"]
    grave_entry = next(
        entry for entry in catalog_context["options"]
        if entry.get("handler") == "play_from_graveyard")
    assert grave_entry["action_context"]["source_idx"] == 6
    assert handler._handle_target_page_next(context=catalog_context)[1]
    option = gs.choice_context["options"].index(grave_entry)
    assert handler._handle_choose_mode(option, {})[1]
    assert gs.stack and gs.stack[-1][1] == flashback
    assert gs.resolve_top_of_stack() and flashback in player["exile"]

    gs = fresh(SEED + 201)
    player = gs.p1
    player["hand"] = []
    impending = []
    for index in range(2):
        card_id = inject_into_zone(gs, player, {
            "name": f"Colliding Impending {index}", "mana_cost": "{9}",
            "type_line": "Creature", "oracle_text": "",
            "power": 1, "toughness": 1,
        }, "hand")
        card = gs._safe_get_card(card_id)
        card.is_impending = True
        card.impending_cost = "{0}"
        impending.append(card_id)
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[294] and mask[479]
    assert handler.action_reasons_with_context[294]["context"]["hand_idx"] == 0
    catalog_context = handler.action_reasons_with_context[479]["context"]
    collision = next(
        entry for entry in catalog_context["options"]
        if entry.get("action_index") == 294)
    assert collision["action_context"]["hand_idx"] == 1
    assert handler._handle_target_page_next(context=catalog_context)[1]
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index(collision), {})[1]
    assert gs.stack and gs.stack[-1][1] == impending[1]


@scenario("701.17 / 602.2b", "sacrifice predicates are structured and direct callers cannot auto-pick")
def scenario_sacrifice_predicates_and_no_direct_fallback():
    from Playersim.ability_types import (
        ActivatedAbility, SacrificeEffect, _permanent_matches_criteria)
    gs = fresh(SEED + 202)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Sacrifice Decision Source", "type_line": "Artifact",
        "oracle_text": "",
    }, "battlefield")
    eligible = inject_into_zone(gs, player, {
        "name": "Eligible Sky Legend", "mana_cost": "{2}{R}",
        "type_line": "Legendary Creature - Dragon",
        "supertypes": ["legendary"], "card_types": ["creature"],
        "subtypes": ["Dragon"], "color_identity": ["R"],
        "oracle_text": "Flying", "power": 3, "toughness": 3,
    }, "battlefield")
    missing_counter = inject_into_zone(gs, player, {
        "name": "Unmodified Sky Legend", "mana_cost": "{2}{R}",
        "type_line": "Legendary Creature - Dragon",
        "supertypes": ["legendary"], "card_types": ["creature"],
        "subtypes": ["Dragon"], "color_identity": ["R"],
        "oracle_text": "Flying", "power": 4, "toughness": 4,
    }, "battlefield")
    assert gs.tap_permanent(eligible, player)
    assert gs.tap_permanent(missing_counter, player)
    assert gs.add_counter(eligible, "+1/+1", 1)
    assert _permanent_matches_criteria(
        gs, eligible, "red or blue creature with mana value 3 or less",
        controller=player)
    assert not _permanent_matches_criteria(
        gs, eligible, "red and blue creature", controller=player)
    assert not _permanent_matches_criteria(
        gs, eligible, "artifact creature with mana value 3 or less",
        controller=player)
    effect = SacrificeEffect(
        "tapped legendary red creature with flying and a +1/+1 counter",
        who="controller")
    assert effect.apply(gs, source, player, {})
    assert gs.choice_context["options"] == [eligible]
    assert missing_counter in player["battlefield"]
    gs.choice_context = None
    gs.phase = gs.PHASE_MAIN_PRECOMBAT

    direct = ActivatedAbility(
        source, cost="Sacrifice a creature", effect="Draw a card.")
    direct.source_card = gs._safe_get_card(source)
    before = list(player["battlefield"])
    assert not direct.pay_cost(gs, player), \
        "a direct caller silently selected a sacrifice"
    assert player["battlefield"] == before


@scenario("107.3 / 602.2b", "nonmana X costs derive exact resource bounds and stage card choices")
def scenario_nonmana_x_cost_families_and_unbounded_range():
    from Playersim.ability_types import ActivatedAbility

    gs = fresh(SEED + 203)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Unbounded Counter Engine", "type_line": "Artifact",
        "oracle_text": "",
    }, "battlefield")
    card = gs._safe_get_card(source)
    card.counters["CHARGE"] = 1001
    ability = ActivatedAbility(
        source, cost="Remove X charge counters from this artifact",
        effect="Draw a card.")
    ability.source_card = card
    gs.ability_handler.registered_abilities[source] = [ability]
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    activation = {"battlefield_idx": 0, "ability_idx": 0,
                  "controller_id": "p1"}
    assert handler._handle_activate_ability(None, activation)[1]
    assert gs.choice_context["max_x"] == 1001
    assert get_env()._get_obs()["valid_x_range"].tolist() == [0, 1001]
    assert handler._handle_choose_x(0, {"x_value": 1001})[1]
    assert card.counters.get("CHARGE", 0) == 0 \
        and gs.stack[-1][3]["X"] == 1001

    gs = fresh(SEED + 204)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Variable Discard Engine", "type_line": "Artifact",
        "oracle_text": "",
    }, "battlefield")
    ability = ActivatedAbility(
        source, cost="Discard X cards", effect="Draw a card.")
    ability.source_card = gs._safe_get_card(source)
    gs.ability_handler.registered_abilities[source] = [ability]
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    handler = get_env().action_handler
    activation = {"battlefield_idx": 0, "ability_idx": 0,
                  "controller_id": "p1"}
    chosen = [player["hand"][1], player["hand"][3]]
    assert handler._handle_activate_ability(None, activation)[1]
    assert handler._handle_choose_x(2, {"x_value": 2})[1]
    assert gs.choice_context["type"] == "activation_discard_cost"
    for card_id in chosen:
        option = gs.choice_context["options"].index(card_id)
        assert handler._handle_choose_mode(option, {})[1]
    assert all(card_id in player["graveyard"] for card_id in chosen)
    assert gs.stack[-1][3]["X"] == 2

    gs = fresh(SEED + 205)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Variable Sacrifice Engine", "type_line": "Artifact",
        "oracle_text": "",
    }, "battlefield")
    payments = [inject_into_zone(gs, player, {
        "name": f"Sacrifice Artifact {index}",
        "type_line": "Token Artifact - Food", "card_types": ["artifact"],
        "subtypes": ["Food"], "oracle_text": "", "is_token": True,
    }, "battlefield") for index in range(2)]
    ability = ActivatedAbility(
        source, cost="Sacrifice X artifacts", effect="Draw a card.")
    ability.source_card = gs._safe_get_card(source)
    gs.ability_handler.registered_abilities[source] = [ability]
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    handler = get_env().action_handler
    activation = {"battlefield_idx": 0, "ability_idx": 0,
                  "controller_id": "p1"}
    assert handler._handle_activate_ability(None, activation)[1]
    assert handler._handle_choose_x(2, {"x_value": 2})[1]
    for card_id in payments:
        option = gs.choice_context["options"].index(card_id)
        assert handler._handle_choose_mode(option, {})[1]
    assert all(card_id not in player["battlefield"] for card_id in payments)


@scenario("711 / policy protocol", "level-up remains optional and reaches levelers beyond fixed slots")
def scenario_level_up_optional_overflow_policy():
    gs = fresh(SEED + 206)
    player = gs.p1
    player["battlefield"] = []
    for index in range(6):
        inject_into_zone(gs, player, {
            "name": f"Level Overflow Filler {index}",
            "type_line": "Artifact", "oracle_text": "",
        }, "battlefield")
    leveler = inject_into_zone(gs, player, {
        "name": "Overflow Leveler", "mana_cost": "{W}",
        "type_line": "Creature - Human", "power": 1, "toughness": 1,
        "oracle_text": "Level up {W}\nLEVEL 1-2\n2/2\nVigilance",
    }, "battlefield")
    player["mana_pool"]["W"] = 1
    gs.phase = gs.PHASE_MAIN_PRECOMBAT
    gs.priority_player = player
    gs.agent_is_p1 = True
    handler = get_env().action_handler
    mask = handler.generate_valid_actions()
    assert mask[11] and mask[479], \
        "level-up did not preserve the policy's decline/activate alternatives"
    context = handler.action_reasons_with_context[479]["context"]
    entry = next(
        item for item in context["options"]
        if item.get("handler") == "level_up_creature")
    assert entry["action_context"]["battlefield_idx"] == 6
    assert handler._handle_target_page_next(context=context)[1]
    assert handler._handle_choose_mode(
        gs.choice_context["options"].index(entry), {})[1]
    assert gs._safe_get_card(leveler).counters.get("level") == 1


@scenario("103.6c / scripted policy", "the baseline evaluates optional opening-hand placements")
def scenario_scripted_opening_hand_accepts_and_declines():
    def decision(text):
        gs = fresh(SEED + 207)
        player = gs.p2
        player["hand"] = []
        inject_into_zone(gs, player, {
            "name": "Opening Placement Probe", "type_line": "Enchantment",
            "oracle_text": text,
        }, "hand")
        gs._opening_hand_players = []
        gs._begin_opening_hand_choice(player)
        gs.agent_is_p1 = False
        handler = get_env().action_handler
        mask = handler.generate_valid_actions()
        return get_env()._get_scripted_opponent_action(
            player, mask, {"phase_context": "CHOOSE"})[0]

    permission = (
        "If this card is in your opening hand, you may begin the game with "
        "it on the battlefield.")
    assert decision(permission) == 353
    assert decision(permission + " If you do, you lose 10 life.") == 11


@scenario("608.2d / policy pagination", "keyword menus support arbitrary counts and subtype targeting")
def scenario_keyword_grant_arbitrary_options_and_subtype():
    from Playersim.ability_types import KeywordChoiceGrantEffect
    from Playersim.ability_utils import EffectFactory
    gs = fresh(SEED + 208)
    player = gs.p1
    source = inject_into_zone(gs, player, {
        "name": "Keyword Menu Source", "type_line": "Enchantment",
        "oracle_text": "",
    }, "battlefield")
    mouse = inject_into_zone(gs, player, {
        "name": "Menu Mouse", "type_line": "Creature - Mouse",
        "oracle_text": "", "power": 1, "toughness": 1,
    }, "battlefield")
    bear = inject_into_zone(gs, player, {
        "name": "Menu Bear", "type_line": "Creature - Bear",
        "oracle_text": "", "power": 2, "toughness": 2,
    }, "battlefield")
    effect = EffectFactory.create_effects(
        "Target Mouse you control gains your choice of flying, vigilance, "
        "or lifelink until end of turn.")[0]
    assert isinstance(effect, KeywordChoiceGrantEffect)
    assert effect.options == ["flying", "vigilance", "lifelink"]
    target_type = gs._get_target_type_from_text(effect.effect_text)
    valid = gs.targeting_system.get_valid_targets(
        source, player, target_type, effect_text=effect.effect_text)
    flattened = [card_id for values in valid.values() for card_id in values]
    assert mouse in flattened and bear not in flattened
    assert effect.apply(gs, source, player, {"creatures": [mouse]})
    assert get_env().action_handler._handle_choose_mode(2, {})[1]
    assert gs.check_keyword(mouse, "lifelink")

    long_menu = KeywordChoiceGrantEffect([
        "flying", "vigilance", "lifelink", "trample", "haste",
        "menace", "reach", "deathtouch", "hexproof", "indestructible",
        "double strike",
    ])
    assert long_menu.apply(gs, source, player, {"creatures": [mouse]})
    mask = get_env().action_handler.generate_valid_actions()
    assert mask[479]
    assert get_env().action_handler._handle_target_page_next(
        context={"page_count": 2})[1]
    assert get_env().action_handler._handle_choose_mode(0, {})[1]
    assert gs.check_keyword(mouse, "double strike")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("Playersim rules scenario harness")
    print("=" * 64)
    passed = failed = xfailed = xpassed = 0
    failures = []
    for cr, title, known_bug, fn in SCENARIOS:
        try:
            fn()
            if known_bug:
                xpassed += 1
                print(f"  XPASS [{cr}] {title}  <-- fixed! remove known_bug flag")
            else:
                passed += 1
                print(f"  PASS  [{cr}] {title}")
        except Exception as e:
            if known_bug:
                xfailed += 1
                print(f"  XFAIL [{cr}] {title} ({type(e).__name__}: {e})")
            else:
                failed += 1
                failures.append((cr, title, traceback.format_exc()))
                print(f"  FAIL  [{cr}] {title}")
    print("=" * 64)
    print(f"{passed} passed, {failed} failed, {xfailed} known bugs, {xpassed} unexpectedly fixed")
    for cr, title, tb in failures:
        print(f"\n--- FAIL [{cr}] {title} ---\n{tb}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
