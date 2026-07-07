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
