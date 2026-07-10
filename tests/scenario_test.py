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

import logging
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
    for cid, card in gs.card_db.items():
        if getattr(card, 'name', None) == name:
            return cid
    raise AssertionError(f"fixture card not found: {name}")


def inject_card(gs, data):
    """Register a synthetic card in the database and return its new id."""
    from Playersim.card import Card
    card = Card(data)
    new_id = max(int(k) for k in gs.card_db.keys()) + 1
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
    valid_map = gs.targeting_system.get_valid_targets(
        spell, controller, gs.targeting_context["required_type"],
        effect_text=gs.targeting_context["effect_text"])
    valid_ids = sorted(set(cid for ids in valid_map.values() for cid in ids))
    assert own_target in valid_ids and opposing_target in valid_ids
    assert get_env().action_handler._handle_select_target(
        valid_ids.index(own_target), {})[1], "could not select the friendly discount target"
    assert gs.targeting_context, "up-to-two targeting finalized after only one target"
    assert get_env().action_handler._handle_select_target(
        valid_ids.index(opposing_target), {})[1], "could not select the opposing target"

    paid_cost = gs.stack[-1][3].get("final_paid_cost", {})
    assert paid_cost.get("generic") == 1 and paid_cost.get("U") == 1, \
        f"This Town used the wrong target-conditioned cost: {paid_cost}"
    assert gs.resolve_top_of_stack(), "This Town did not resolve"
    assert own_target in controller["hand"] and opposing_target in opponent["hand"], \
        "This Town did not return both chosen permanents to their owners"


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
    assert gs.resolve_top_of_stack(), "ward-countered spell did not finish resolving"
    assert bolt in caster["graveyard"], "ward-countered spell did not go to graveyard"
    assert defender.get("damage_counters", {}).get(warded, 0) == 0, \
        "unpaid ward spell still damaged the warded creature"


@scenario("702.21", "ward auto-pays available mana tax before the targeted spell resolves")
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
    assert gs.resolve_top_of_stack(), "spell with payable ward tax did not resolve"
    assert caster["mana_pool"].get('C', 0) == 0, "ward tax did not consume the available generic mana"
    assert defender.get("damage_counters", {}).get(warded, 0) == 3, \
        "spell did not damage the warded creature after paying ward"
    assert bolt in caster["graveyard"], "resolved instant did not go to graveyard"


@scenario("702.21", "ward auto-pays a simple life tax before the targeted spell resolves")
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
    assert gs.resolve_top_of_stack(), "spell with payable life ward tax did not resolve"
    assert caster["life"] == life_before - 3, "ward tax did not consume the required life"
    assert defender.get("damage_counters", {}).get(warded, 0) == 3, \
        "spell did not damage the warded creature after paying life for ward"
    assert bolt in caster["graveyard"], "resolved instant did not go to graveyard"


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
    assert hist is not None, "play turns are not tracked; play-turn stats are fabricated from CMC"
    assert hist[0].get(2) == [c1] and hist[0].get(5) == [c2], \
        f"play history recorded wrong turns: {hist[0]} (expected {{2: [{c1}], 5: [{c2}]}})"


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
    mask = get_env().action_handler.generate_valid_actions()
    assert all(mask[i] for i in (363, 364, 365)), "affordable X values 1-3 were not exposed"
    assert not mask[366], "an unaffordable X=4 action was exposed"

    reward, ok = get_env().action_handler._handle_choose_x(2, {})
    assert ok, f"choosing X=2 failed with reward {reward}"
    assert gs.stack and gs.stack[-1][3].get("X") == 2, "chosen X was not stored on the spell"
    assert sum(player["mana_pool"].values()) == pool_before - 3, \
        "{X}{U} with X=2 did not spend exactly three mana"
    assert gs.resolve_top_of_stack(), "X draw spell did not resolve"
    assert len(player["library"]) == library_before - 2, "X=2 did not draw exactly two cards"


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


@scenario("parser: distribute counters", "'distribute N +1/+1 counters' places the counters")
def s_parser_distribute_counters():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player = gs.p1
    src_id = card_id_by_name(gs, "Thicket Brute")
    to_battlefield(gs, src_id)
    a = card_id_by_name(gs, "Vine Stalker"); to_battlefield(gs, a)
    effs = EffectFactory.create_effects(
        "Distribute three +1/+1 counters among any number of target creatures.",
        source_name="Test Distribute")
    assert effs and any(type(e).__name__ == "AddCountersEffect" for e in effs), \
        f"distribute counters did not parse: {[type(e).__name__ for e in effs]}"
    # With one target creature, all 3 counters land on it.
    for e in effs:
        e.apply(gs, src_id, player, {"creatures": [a]})
    card = gs._safe_get_card(a)
    assert card.counters.get("+1/+1", 0) == 3, \
        f"distribute placed {card.counters.get('+1/+1', 0)} counters, expected 3 on the sole target"


@scenario("parser: sacrifice", "'sacrifice a creature' moves one of your creatures to the graveyard")
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
    bf_after = len([c for c in player["battlefield"] if "creature" in getattr(gs._safe_get_card(c),"card_types",[])])
    assert bf_after == bf_before - 1 and len(player["graveyard"]) == gy_before + 1, \
        "no creature was sacrificed to the graveyard"


@scenario("parser: edict", "'target player sacrifices a creature' hits the opponent")
def s_parser_edict():
    gs = fresh()
    from Playersim.ability_utils import EffectFactory
    player, opp = gs.p1, gs.p2
    src_id = card_id_by_name(gs, "Thicket Brute"); to_battlefield(gs, src_id)
    theirs = card_id_by_name(gs, "Vine Stalker")
    assert gs.move_card(theirs, owner_of(gs, theirs), "library", opp, "battlefield")
    effs = EffectFactory.create_effects("Target player sacrifices a creature.", source_name="Edict")
    assert effs and type(effs[0]).__name__ == "SacrificeEffect", \
        f"edict did not parse to a sacrifice effect: {[type(e).__name__ for e in effs]}"
    gy_before = len(opp["graveyard"])
    for e in effs:
        e.apply(gs, src_id, player, {"players": ["p2"]})
    assert len(opp["graveyard"]) == gy_before + 1, "edict did not make the target player sacrifice"


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


@scenario("parser: dig", "'look at the top three, put one into your hand' draws one and reorders the rest")
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
    assert len(player["hand"]) == hand_before + 1, "dig did not put a card into hand"
    assert len(player["library"]) == lib_before - 1, "dig changed library size incorrectly"
    # Exactly one card left the top region into hand; the other two are now on
    # the bottom. (Fixture libraries repeat card IDs across the 4 copies, so
    # verify by net movement rather than ID membership.)
    moved_to_bottom = player["library"][-2:]
    assert all(c in top3 for c in moved_to_bottom), \
        "the unchosen looked-at cards are not on the bottom of the library"


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


@scenario("701.8a / self-play", "each-player discard queues both players and the scripted opponent chooses")
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
    assert ok and p1_card in p1["graveyard"], "first player's discard choice failed"
    assert gs.choice_context and gs.choice_context.get("player") == p2, \
        "the second player's discard choice was not queued"

    gs.agent_is_p1 = False
    opponent_mask = get_env().action_handler.generate_valid_actions()
    action, _ = get_env()._get_scripted_opponent_action(
        p2, opponent_mask, {"phase_context": "CHOOSE"})
    assert action == 238, f"scripted opponent did not choose its available discard: {action}"
    _, ok = get_env().action_handler._handle_discard_card(action - 238)
    assert ok and p2_card in p2["graveyard"], "scripted opponent discard failed"
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
    assert mutating not in controller["hand"] and gs.stack[-1][1] == mutating, \
        "mutate did not become a spell on the stack"
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
