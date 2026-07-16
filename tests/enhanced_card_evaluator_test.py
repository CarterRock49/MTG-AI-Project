import math
import unittest
from types import SimpleNamespace

from Playersim.enhanced_card_evaluator import EnhancedCardEvaluator


def make_card(name, *, types=("creature",), cmc=2, power=2,
              toughness=2, text="", colors=(0, 0, 0, 0, 0),
              subtypes=("test",)):
    return SimpleNamespace(
        name=name,
        card_types=list(types),
        cmc=cmc,
        power=power,
        toughness=toughness,
        oracle_text=text,
        colors=list(colors),
        subtypes=list(subtypes),
    )


def make_player(name):
    return {
        "name": name,
        "life": 20,
        "hand": [],
        "battlefield": [],
        "graveyard": [],
        "tapped_permanents": set(),
        "entered_battlefield_this_turn": set(),
    }


class FakeTargeting:
    def __init__(self):
        self.denied = set()

    def check_can_be_blocked(self, attacker_id, blocker_id):
        return (attacker_id, blocker_id) not in self.denied


class FakeGameState:
    PHASE_MAIN_PRECOMBAT = "main_precombat"

    def __init__(self, cards):
        self.cards = cards
        self.p1 = make_player("p1")
        self.p2 = make_player("p2")
        self.agent_is_p1 = True
        self.turn = 3
        self.phase = self.PHASE_MAIN_PRECOMBAT
        self.current_attackers = []
        self.phased_out = set()
        self.targeting_system = FakeTargeting()
        self.card_instance_printings = {}

    def _safe_get_card(self, card_id):
        return self.cards.get(card_id)

    def check_keyword(self, card_id, keyword):
        card = self._safe_get_card(card_id)
        return keyword.lower() in str(
            getattr(card, "oracle_text", "") or "").lower()

    def canonical_card_id(self, card_id):
        return self.card_instance_printings.get(card_id, card_id)


class MutableMemory:
    def __init__(self, effectiveness=0.5, optimal_turn=0,
                 has_entry=True, raise_on_read=False, games_played=5,
                 archetype=None, archetype_games=0):
        self.effectiveness = effectiveness
        self.optimal_turn = optimal_turn
        self.has_entry = has_entry
        self.raise_on_read = raise_on_read
        self.games_played = games_played
        self.archetype = archetype
        self.archetype_games = archetype_games
        self.recorded = None
        self.queried_ids = []
        self.stats_queried_ids = []

    def get_card_stats(self, card_id):
        self.stats_queried_ids.append(card_id)
        if self.raise_on_read:
            raise RuntimeError("memory unavailable")
        if not self.has_entry:
            return {}
        stats = {
            "games_played": self.games_played,
            "effectiveness_rating": self.effectiveness,
        }
        if self.archetype is not None:
            stats["archetype_performance"] = {
                self.archetype: {"games": self.archetype_games}}
        return stats

    def get_effectiveness_for_archetype(self, card_id, archetype):
        self.queried_ids.append(card_id)
        return self.effectiveness

    def get_optimal_play_turn(self, card_id):
        return self.optimal_turn

    def update_card_performance(self, card_id, result):
        self.recorded = (card_id, result)


class FakeStats:
    def __init__(self, card_stats=None, deck_stats=None):
        self.card_stats = card_stats or {}
        self.deck_stats = deck_stats or {}
        self.requested_deck_key = None
        self.requested_card_id = None

    def get_card_stats(self, card_id):
        self.requested_card_id = card_id
        return dict(self.card_stats)

    def get_deck_fingerprint(self, deck):
        return "deck-fingerprint"

    def get_deck_stats(self, deck_key):
        self.requested_deck_key = deck_key
        return dict(self.deck_stats)


class EnhancedCardEvaluatorTests(unittest.TestCase):
    def test_attack_state_is_live_and_non_haste_is_not_always_sick(self):
        cards = {
            1: make_card("Attacker", power=3, toughness=3),
            2: make_card("Blocker", power=6, toughness=6),
        }
        gs = FakeGameState(cards)
        gs.p1["battlefield"].append(1)
        evaluator = EnhancedCardEvaluator(gs)

        open_score = evaluator.evaluate_card(1, "attack")
        self.assertGreater(open_score, -5.0)

        gs.p2["battlefield"].append(2)
        blocked_score = evaluator.evaluate_card(1, "attack")
        self.assertLess(blocked_score, open_score)

        gs.p1["entered_battlefield_this_turn"].add(1)
        self.assertEqual(evaluator.evaluate_card(1, "attack"), -5.0)
        cards[1].oracle_text = "Haste"
        self.assertGreater(evaluator.evaluate_card(1, "attack"), -5.0)

    def test_attack_and_block_require_the_requested_perspective(self):
        cards = {
            1: make_card("P2 Attacker"),
            2: make_card("P1 Blocker"),
        }
        gs = FakeGameState(cards)
        gs.p2["battlefield"].append(1)
        gs.p1["battlefield"].append(2)
        evaluator = EnhancedCardEvaluator(gs)

        self.assertEqual(evaluator.evaluate_card(1, "attack"), -5.0)
        self.assertGreater(evaluator.evaluate_card(
            1, "attack", {"perspective": "p2"}), -5.0)

        gs.current_attackers = [1]
        gs.targeting_system.denied.add((1, 2))
        self.assertEqual(evaluator.evaluate_card(
            2, "block", {"perspective": "p1"}), -5.0)

    def test_partial_context_uses_current_turn_and_history_is_not_cached(self):
        card = make_card("Timing Card")
        gs = FakeGameState({1: card})
        memory = MutableMemory(effectiveness=0.5, optimal_turn=3)
        evaluator = EnhancedCardEvaluator(gs, card_memory=memory)

        gs.turn = 5  # P1's third turn in the alternating global clock.
        on_curve = evaluator.evaluate_card(1, "general", {"position": "even"})
        gs.turn = 17  # P1's ninth turn.
        late = evaluator.evaluate_card(1, "general", {"position": "even"})
        self.assertGreater(on_curve, late)

        memory.effectiveness = 1.0
        refreshed = evaluator.evaluate_card(1, "general", {"position": "even"})
        self.assertGreater(refreshed, late)

    def test_discard_score_is_consistently_keep_value(self):
        cards = {
            1: make_card(
                "High Impact", cmc=5, power=6, toughness=6,
                text="When this enters the battlefield, draw a card."),
            2: make_card(
                "Narrow Expensive Spell", types=("sorcery",), cmc=9,
                power=0, toughness=0),
        }
        gs = FakeGameState(cards)
        gs.p1["hand"] = [1, 2]
        evaluator = EnhancedCardEvaluator(gs)

        self.assertGreater(
            evaluator._evaluate_for_discard(1),
            evaluator._evaluate_for_discard(2))
        self.assertGreater(
            evaluator.evaluate_card(1, "discard"),
            evaluator.evaluate_card(2, "discard"))

    def test_static_cache_tracks_live_characteristic_changes(self):
        card = make_card("Growing Creature", power=1, toughness=1)
        evaluator = EnhancedCardEvaluator(FakeGameState({1: card}))

        small = evaluator.evaluate_card(1)
        self.assertEqual(evaluator.evaluate_card(1), small)
        self.assertGreater(evaluator.cache_hits, 0)

        card.power = 7
        card.toughness = 7
        large = evaluator.evaluate_card(1)
        self.assertGreater(large, small)

    def test_runtime_ids_use_canonical_identity_for_analytics_only(self):
        runtime_id = 101
        gs = FakeGameState({runtime_id: make_card("Materialized Copy")})
        gs.card_instance_printings[runtime_id] = 7
        memory = MutableMemory()
        stats = FakeStats({"games_played": 5, "wins": 3})
        evaluator = EnhancedCardEvaluator(
            gs, stats_tracker=stats, card_memory=memory)

        self.assertGreater(evaluator.evaluate_card(runtime_id), 0.0)
        self.assertEqual(memory.stats_queried_ids, [7])
        self.assertEqual(memory.queried_ids, [])
        self.assertIsNone(stats.requested_card_id)
        evaluator.record_card_performance(runtime_id, {"is_win": True})
        self.assertEqual(memory.recorded[0], 7)

    def test_card_memory_prevents_additive_deck_stats_history(self):
        card = make_card("Primary History")
        memory = MutableMemory(effectiveness=1.0)
        stats = FakeStats({"games_played": 20, "wins": 0})
        evaluator = EnhancedCardEvaluator(
            FakeGameState({1: card}), stats_tracker=stats,
            card_memory=memory)
        without_stats = EnhancedCardEvaluator(
            FakeGameState({1: card}), card_memory=MutableMemory(
                effectiveness=1.0))

        self.assertEqual(
            evaluator.evaluate_card(1), without_stats.evaluate_card(1))
        self.assertIsNone(stats.requested_card_id)

    def test_deck_stats_fallback_for_missing_or_failed_card_memory(self):
        card = make_card("Fallback History")
        for memory in (
                MutableMemory(has_entry=False),
                MutableMemory(raise_on_read=True)):
            with self.subTest(memory=type(memory).__name__,
                              raises=memory.raise_on_read):
                stats = FakeStats({"games_played": 20, "wins": 20})
                evaluator = EnhancedCardEvaluator(
                    FakeGameState({101: card}), stats_tracker=stats,
                    card_memory=memory)
                evaluator.game_state.card_instance_printings[101] = 7

                value = evaluator.evaluate_card(101)

                self.assertGreater(value, 0.0)
                self.assertEqual(stats.requested_card_id, 7)

    def test_deck_stats_fallback_for_sparse_card_memory(self):
        card = make_card("Sparse History")
        memory = MutableMemory(effectiveness=1.0, games_played=1)
        stats = FakeStats({"games_played": 20, "wins": 0})
        evaluator = EnhancedCardEvaluator(
            FakeGameState({1: card}), stats_tracker=stats,
            card_memory=memory)

        evaluator.evaluate_card(1)

        self.assertEqual(stats.requested_card_id, 1)
        self.assertEqual(memory.queried_ids, [])

    def test_mature_overall_memory_survives_sparse_archetype_bucket(self):
        card = make_card("Mature Overall History")
        memory = MutableMemory(
            effectiveness=0.9, games_played=8,
            archetype="control", archetype_games=1)
        stats = FakeStats({"games_played": 20, "wins": 0})
        evaluator = EnhancedCardEvaluator(
            FakeGameState({1: card}), stats_tracker=stats,
            card_memory=memory)

        evaluator.evaluate_card(
            1, context_details={"deck_archetype": "CONTROL"})

        self.assertIsNone(stats.requested_card_id)
        self.assertEqual(memory.queried_ids, [])

    def test_board_synergy_cache_is_transient_and_explicitly_named(self):
        cards = {
            1: make_card("Synergy Card", subtypes=("wizard",)),
            2: make_card("Board Wizard", subtypes=("wizard",)),
        }
        evaluator = EnhancedCardEvaluator(FakeGameState(cards))

        evaluator._calculate_synergy_value(1, [2])

        self.assertTrue(evaluator._board_synergy_cache)
        self.assertFalse(hasattr(evaluator, "synergy_memory"))

    def test_nonfinite_history_stats_and_characteristics_are_bounded(self):
        card = make_card(
            "Malformed", cmc=float("inf"), power=float("nan"),
            toughness=float("inf"))
        memory = MutableMemory(
            effectiveness=float("nan"), optimal_turn=float("inf"))
        stats = FakeStats({"games_played": 10, "wins": float("inf")})
        evaluator = EnhancedCardEvaluator(
            FakeGameState({1: card}), stats_tracker=stats, card_memory=memory)

        value = evaluator.evaluate_card(
            1, "general", {"aggression_level": float("nan")})
        self.assertTrue(math.isfinite(value))
        self.assertGreaterEqual(value, -5.0)
        self.assertLessEqual(value, 10.0)

    def test_draw_only_card_and_deck_stats_are_neutral(self):
        cards = {1: make_card("Drawn Match")}
        stats = FakeStats(
            card_stats={"games_played": 10, "wins": 0, "draws": 10},
            deck_stats={
                "games": 10, "wins": 0, "losses": 0, "draws": 10})
        evaluator = EnhancedCardEvaluator(
            FakeGameState(cards), stats_tracker=stats)

        self.assertEqual(evaluator._get_stats_value(1), 0.0)
        deck_result = evaluator.evaluate_deck([1])
        self.assertEqual(deck_result["stats_metrics"]["draws"], 10)
        self.assertEqual(deck_result["stats_metrics"]["win_rate"], 0.5)

    def test_record_performance_does_not_mutate_callers_dictionary(self):
        memory = MutableMemory()
        evaluator = EnhancedCardEvaluator(
            FakeGameState({1: make_card("Recorded", cmc=4)}),
            card_memory=memory)
        original = {"won": True}

        evaluator.record_card_performance(1, original)

        self.assertEqual(original, {"won": True})
        self.assertEqual(memory.recorded[1]["card_name"], "Recorded")
        self.assertEqual(memory.recorded[1]["cmc"], 4)

    def test_deck_evaluation_excludes_lands_from_curve_and_uses_fingerprint(self):
        cards = {
            1: make_card(
                "Land", types=("land",), cmc=0, power=0, toughness=0,
                colors=(0, 0, 0, 0, 1)),
            2: make_card("Two Drop", cmc=2),
            3: make_card(
                "Five Mana Spell", types=("sorcery",), cmc=5,
                power=0, toughness=0),
        }
        stats = FakeStats(
            deck_stats={"games": 10, "wins": 0, "losses": 10})
        evaluator = EnhancedCardEvaluator(
            FakeGameState(cards), stats_tracker=stats)

        empty = evaluator.evaluate_deck([])
        result = evaluator.evaluate_deck([1, 2, 3])

        self.assertEqual(empty["balance_score"], 0.0)
        self.assertEqual(stats.requested_deck_key, "deck-fingerprint")
        self.assertEqual(sum(result["deck_metrics"]["mana_curve"].values()), 2)
        self.assertEqual(result["deck_metrics"]["mana_curve"]["0"], 0)
        self.assertGreaterEqual(result["balance_score"], 0.0)
        self.assertLessEqual(result["balance_score"], 1.0)
        self.assertGreaterEqual(result["overall_rating"], 0.0)
        self.assertLessEqual(result["overall_rating"], 10.0)

    def test_deck_rating_uses_stats_monotonically_and_by_confidence(self):
        evaluator = EnhancedCardEvaluator(FakeGameState({}))
        metrics = {"avg_card_strength": 5.0}
        balance = 0.7

        prior = evaluator._calculate_overall_rating(metrics, {}, balance)
        low_sample_loss = evaluator._calculate_overall_rating(
            metrics, {"games_played": 5, "win_rate": 0.0}, balance)
        low_sample_win = evaluator._calculate_overall_rating(
            metrics, {"games_played": 5, "win_rate": 1.0}, balance)
        proven_winner = evaluator._calculate_overall_rating(
            metrics, {"games_played": 50, "win_rate": 1.0}, balance)

        self.assertLess(low_sample_loss, low_sample_win)
        self.assertLess(abs(low_sample_loss - prior), 0.5)
        self.assertLess(abs(low_sample_win - prior), 0.5)
        self.assertGreater(proven_winner, low_sample_win)


if __name__ == "__main__":
    unittest.main()
