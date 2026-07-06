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
