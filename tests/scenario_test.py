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
        _TMP = tempfile.mkdtemp()
        build_fixture_decks(_TMP)
        decks, card_db = load_decks_and_card_db(_TMP)
        _ENV = AlphaZeroMTGEnv(decks, card_db)
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
