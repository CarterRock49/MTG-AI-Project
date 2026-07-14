"""Deterministic, optional strategic-action memory.

Strategy memory is an advisory subsystem, not part of the policy observation.
It records observer-relative state patterns and action outcomes for diagnostics
or explicitly enabled planners.  It deliberately has no random exploration,
wall-clock weighting, background writer, or cross-environment shared file.
"""

from __future__ import annotations

import logging
import math
import os
import pickle
import tempfile
import threading

import numpy as np


STRATEGY_MEMORY_SCHEMA_VERSION = 2


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _card_stat(card, attribute):
    """Return a finite numeric card characteristic for strategy features."""
    return _finite_number(getattr(card, attribute, 0) if card else 0)


def _category(delta, strong_threshold):
    """Bucket a signed difference into the stable -2..2 vocabulary."""
    delta = _finite_number(delta)
    if delta > strong_threshold:
        return 2
    if delta > 0:
        return 1
    if delta < -strong_threshold:
        return -2
    if delta < 0:
        return -1
    return 0


class StrategyMemory:
    """Record deterministic action values for observer-relative patterns.

    The file is owned by one environment. Callers must give each vector worker
    a distinct path, as the training environment factory does. Legacy files are
    loaded conservatively: aggregate pattern statistics survive, but they do
    not invent action evidence that the old format never recorded.
    """

    def __init__(self, memory_file="strategy_memory.pkl", max_size=50_000,
                 auto_save_interval=1_024, min_action_count=2):
        self.memory_file = os.fspath(memory_file) if memory_file else None
        self.max_size = max(1, int(max_size))
        self.auto_save_interval = max(0, int(auto_save_interval))
        self.min_action_count = max(1, int(min_action_count))
        self.strategies = {}
        self.action_sequences = []
        self.logical_update = 0
        self.dirty = False
        self._lock = threading.RLock()
        self.load_memory()

    @staticmethod
    def _empty_entry():
        return {
            "count": 0,
            "reward": 0.0,
            "success_rate": 0.0,
            "last_update": 0,
            "actions": {},
        }

    @staticmethod
    def _normalize_pattern(pattern):
        if isinstance(pattern, np.ndarray):
            pattern = pattern.tolist()
        if not isinstance(pattern, (tuple, list)):
            raise TypeError("strategy pattern must be a tuple or list")
        normalized = []
        for value in pattern:
            if isinstance(value, (np.integer, int, bool)):
                normalized.append(int(value))
            elif isinstance(value, (np.floating, float)):
                normalized.append(_finite_number(value))
            else:
                raise TypeError(
                    f"unsupported strategy-pattern value {value!r}")
        return tuple(normalized)

    @classmethod
    def _normalize_stats(cls, raw):
        if not isinstance(raw, dict):
            return cls._empty_entry()
        count = max(0, int(raw.get("count", 0) or 0))
        entry = {
            "count": count,
            "reward": _finite_number(raw.get("reward", 0.0)),
            "success_rate": min(
                1.0, max(0.0, _finite_number(
                    raw.get("success_rate", 0.0)))),
            "last_update": max(0, int(
                raw.get("last_update", 0) or 0)),
            "actions": {},
        }
        raw_actions = raw.get("actions", {})
        if isinstance(raw_actions, dict):
            for action, action_raw in raw_actions.items():
                try:
                    action_index = int(action)
                except (TypeError, ValueError):
                    continue
                if action_index < 0:
                    continue
                stats = cls._normalize_stats(action_raw)
                stats.pop("actions", None)
                if stats["count"]:
                    entry["actions"][action_index] = stats
        return entry

    def load_memory(self):
        """Load one versioned snapshot, failing closed on malformed data."""
        if not self.memory_file:
            return False
        if not os.path.exists(self.memory_file):
            logging.info(
                "No strategy memory file found at %s; starting empty",
                self.memory_file)
            return False
        try:
            with open(self.memory_file, "rb") as handle:
                data = pickle.load(handle)
            if not isinstance(data, dict):
                raise ValueError("strategy-memory root must be a dictionary")

            strategies = {}
            for raw_pattern, raw_entry in data.get("strategies", {}).items():
                try:
                    pattern = self._normalize_pattern(raw_pattern)
                    strategies[pattern] = self._normalize_stats(raw_entry)
                except (TypeError, ValueError):
                    logging.warning(
                        "Ignoring malformed strategy pattern %r", raw_pattern)

            sequences = []
            for item in data.get("action_sequences", []):
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    continue
                sequence, reward = item
                if isinstance(sequence, (tuple, list)):
                    sequences.append((list(sequence), _finite_number(reward)))

            with self._lock:
                self.strategies = strategies
                self.action_sequences = sequences[:self.max_size]
                recorded_update = max(
                    (entry["last_update"] for entry in strategies.values()),
                    default=0)
                self.logical_update = max(
                    recorded_update,
                    int(data.get("logical_update", 0) or 0))
                self.dirty = False
            source_version = int(data.get("schema_version", 1) or 1)
            logging.info(
                "Loaded %s strategy patterns (schema v%s) from %s",
                len(strategies), source_version, self.memory_file)
            return True
        except Exception as error:
            logging.error(
                "Could not load strategy memory %s: %s; starting empty",
                self.memory_file, error)
            with self._lock:
                self.strategies = {}
                self.action_sequences = []
                self.logical_update = 0
                self.dirty = False
            return False

    def _snapshot(self):
        return {
            "schema_version": STRATEGY_MEMORY_SCHEMA_VERSION,
            "logical_update": self.logical_update,
            "strategies": self.strategies,
            "action_sequences": self.action_sequences,
        }

    def save_memory(self):
        """Atomically persist a deterministic snapshot."""
        if not self.memory_file:
            with self._lock:
                self.dirty = False
            return True
        temp_path = None
        try:
            with self._lock:
                directory = os.path.dirname(os.path.abspath(self.memory_file))
                os.makedirs(directory, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                        mode="wb", delete=False, dir=directory,
                        prefix="strategy_memory_", suffix=".tmp") as handle:
                    temp_path = handle.name
                    pickle.dump(
                        self._snapshot(), handle,
                        protocol=pickle.HIGHEST_PROTOCOL)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.memory_file)
                temp_path = None
                self.dirty = False
            logging.info(
                "Saved %s strategy patterns to %s",
                len(self.strategies), self.memory_file)
            return True
        except Exception as error:
            logging.error(
                "Could not save strategy memory %s: %s",
                self.memory_file, error)
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def save_memory_async(self):
        """Compatibility API: deterministic persistence is intentionally sync."""
        return self.save_memory()

    def _save_memory_worker(self):
        """Compatibility target retained for older callers."""
        return self.save_memory()

    @staticmethod
    def _update_stats(stats, reward, logical_update):
        old_count = int(stats.get("count", 0) or 0)
        new_count = old_count + 1
        old_reward = _finite_number(stats.get("reward", 0.0))
        old_success = _finite_number(stats.get("success_rate", 0.0))
        success = 1.0 if reward > 0 else 0.0
        stats.update({
            "count": new_count,
            "reward": old_reward + (reward - old_reward) / new_count,
            "success_rate": (
                old_success + (success - old_success) / new_count),
            "last_update": logical_update,
        })

    def update_strategy(self, pattern, reward, action_idx=None):
        """Record one outcome, optionally assigning it to a concrete action."""
        try:
            pattern = self._normalize_pattern(pattern)
        except (TypeError, ValueError) as error:
            logging.error("Ignoring invalid strategy pattern: %s", error)
            return False
        reward = _finite_number(reward)
        action = None
        if action_idx is not None:
            try:
                action = int(action_idx)
            except (TypeError, ValueError):
                logging.error("Ignoring invalid strategy action %r", action_idx)
                return False
            if action < 0:
                return False

        should_save = False
        with self._lock:
            self.logical_update += 1
            entry = self.strategies.setdefault(
                pattern, self._empty_entry())
            self._update_stats(entry, reward, self.logical_update)
            if action is not None:
                action_stats = entry["actions"].setdefault(action, {
                    "count": 0, "reward": 0.0, "success_rate": 0.0,
                    "last_update": 0,
                })
                self._update_stats(
                    action_stats, reward, self.logical_update)
            self.dirty = True
            if len(self.strategies) > self.max_size:
                self.prune_memory()
            should_save = bool(
                self.auto_save_interval
                and self.logical_update % self.auto_save_interval == 0)
        if should_save:
            self.save_memory()
        return True

    @staticmethod
    def _normalized_sequence(action_sequence):
        result = []
        for item in action_sequence or ():
            if isinstance(item, dict):
                copied = dict(item)
                if "action_idx" in copied:
                    try:
                        copied["action_idx"] = int(copied["action_idx"])
                    except (TypeError, ValueError):
                        continue
                result.append(copied)
            else:
                try:
                    result.append({"action_idx": int(item)})
                except (TypeError, ValueError):
                    continue
        return result

    def record_action_sequence(self, action_sequence, reward, game_state=None):
        """Retain a bounded deterministic episode trace for diagnostics."""
        sequence = self._normalized_sequence(action_sequence)
        if not sequence:
            return False
        with self._lock:
            self.action_sequences.append(
                (sequence, _finite_number(reward)))
            self.dirty = True
            self.prune_memory()
        return True

    def identify_strategic_concepts(self):
        """Summarize broad concepts in retained positive-reward traces."""
        concepts = {
            name: {"count": 0, "reward": 0.0, "avg_reward": 0.0}
            for name in ("aggro", "control", "midrange", "tempo", "combo")
        }
        for sequence, reward in self.action_sequences:
            if reward <= 0:
                continue
            scores = {name: 0 for name in concepts}
            turn_counts = {}
            for action in sequence:
                action_type = str(action.get("action_type", "")).upper()
                context = action.get("board_context", {}) or {}
                turn = context.get("turn", action.get("turn"))
                if turn is not None:
                    turn_counts[turn] = turn_counts.get(turn, 0) + 1
                if "ATTACK" in action_type or "PLAY_CREATURE" in action_type:
                    scores["aggro"] += 1
                if any(term in action_type for term in
                       ("COUNTER", "DESTROY", "EXILE")):
                    scores["control"] += 1
                if ("PLAY_CREATURE" in action_type
                        and _finite_number(turn) >= 3):
                    scores["midrange"] += 1
                if "RETURN" in action_type or "TAP" in action_type:
                    scores["tempo"] += 1
            if any(count >= 3 for count in turn_counts.values()):
                scores["combo"] += 1
            if not any(scores.values()):
                continue
            concept = max(scores, key=lambda name: (scores[name], name))
            concepts[concept]["count"] += 1
            concepts[concept]["reward"] += reward
        for values in concepts.values():
            if values["count"]:
                values["avg_reward"] = values["reward"] / values["count"]
        return concepts

    def extract_strategy_pattern(self, game_state, detailed=False):
        """Extract the stable observer-relative 14-field state abstraction."""
        try:
            gs = game_state
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opponent = gs.p2 if gs.agent_is_p1 else gs.p1

            def cards_of_type(player, card_type):
                result = []
                for card_id in player.get("battlefield", ()):
                    card = gs._safe_get_card(card_id)
                    types = {
                        str(value).lower()
                        for value in getattr(card, "card_types", ())}
                    type_line = str(
                        getattr(card, "type_line", "")).lower()
                    if card and (card_type in types or card_type in type_line):
                        result.append(card_id)
                return result

            my_creatures = cards_of_type(me, "creature")
            opp_creatures = cards_of_type(opponent, "creature")
            my_lands = cards_of_type(me, "land")
            opp_lands = cards_of_type(opponent, "land")
            my_power = sum(
                _card_stat(gs._safe_get_card(card_id), "power")
                for card_id in my_creatures)
            my_toughness = sum(
                _card_stat(gs._safe_get_card(card_id), "toughness")
                for card_id in my_creatures)
            opp_power = sum(
                _card_stat(gs._safe_get_card(card_id), "power")
                for card_id in opp_creatures)

            turn = int(getattr(gs, "turn", 0) or 0)
            game_stage = 2 if turn >= 8 else 1 if turn >= 4 else 0
            phase = getattr(gs, "phase", 0)
            combat_phases = {
                getattr(gs, "PHASE_DECLARE_ATTACKERS", object()),
                getattr(gs, "PHASE_DECLARE_BLOCKERS", object()),
                getattr(gs, "PHASE_COMBAT_DAMAGE", object()),
            }
            end_phases = {
                getattr(gs, "PHASE_END_STEP", object()),
                getattr(gs, "PHASE_CLEANUP", object()),
            }
            phase_category = (
                1 if phase in combat_phases else
                2 if phase in end_phases else 0)

            stack_status = 0
            if getattr(gs, "stack", None):
                stack_status = 1
                top = gs.stack[-1]
                if (isinstance(top, tuple) and len(top) >= 3
                        and top[2] is me):
                    stack_status = 2

            have_removal = False
            have_combat_trick = False
            have_big_threat = False
            for card_id in me.get("hand", ()):
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                text = str(getattr(card, "oracle_text", "")).lower()
                types = {
                    str(value).lower()
                    for value in getattr(card, "card_types", ())}
                have_removal |= any(
                    term in text for term in
                    ("destroy", "exile", "damage to"))
                have_combat_trick |= bool(
                    "instant" in types
                    and any(term in text for term in
                            ("gets +", "target creature")))
                have_big_threat |= bool(
                    "creature" in types and _card_stat(card, "power") >= 4)

            potential_damage = sum(
                _card_stat(gs._safe_get_card(card_id), "power")
                for card_id in opp_creatures)
            threatening = sum(
                _card_stat(gs._safe_get_card(card_id), "power") >= 3
                for card_id in opp_creatures)
            threat_level = 2 if threatening >= 2 else 1 if threatening else 0
            my_life = _finite_number(me.get("life", 0))
            if potential_damage >= my_life:
                threat_level = 3
            elif potential_damage >= my_life / 2:
                threat_level = max(threat_level, 2)

            pattern = (
                game_stage,
                _category(len(my_creatures) - len(opp_creatures), 2),
                _category(my_power - opp_power, 5),
                _category(my_life - _finite_number(opponent.get("life", 0)), 10),
                _category(len(me.get("hand", ()))
                          - len(opponent.get("hand", ())), 2),
                _category(len(my_lands) - len(opp_lands), 2),
                phase_category,
                stack_status,
                min(len(my_creatures), 5),
                min(len(opp_creatures), 5),
                int(have_removal),
                int(have_combat_trick),
                int(have_big_threat),
                threat_level,
            )
            if not detailed:
                return pattern
            return pattern + (
                my_life,
                _finite_number(opponent.get("life", 0)),
                len(me.get("hand", ())),
                len(opponent.get("hand", ())),
                len(my_lands),
                my_power,
                my_toughness,
                turn,
            )
        except Exception as error:
            logging.error(
                "Error extracting strategy pattern: %s", error,
                exc_info=True)
            return (0,) * (22 if detailed else 14)

    def _pattern_similarity(self, pattern1, pattern2, tolerance=0.7):
        """Return a deterministic weighted similarity in the range 0..1."""
        try:
            first = self._normalize_pattern(pattern1)
            second = self._normalize_pattern(pattern2)
        except (TypeError, ValueError):
            return 0.0
        if len(first) != len(second) or not first:
            return 0.0
        weights = (2.0, 1.5, 1.5, 1.8, 1.2, 1.0, 1.0,
                   1.0, 1.5, 1.2, 0.8, 0.8, 0.8, 1.4)
        similarities = []
        for index, (left, right) in enumerate(zip(first, second)):
            if left == right:
                similarities.append(1.0)
            elif index == 0:
                similarities.append(0.5 if abs(left - right) == 1 else 0.0)
            elif index in (1, 2, 3, 4, 5):
                same_sign = ((left > 0 and right > 0)
                             or (left < 0 and right < 0))
                similarities.append(
                    0.8 if abs(left - right) == 1 and same_sign
                    else 0.5 if same_sign
                    else 0.25 if left == 0 or right == 0
                    else 0.0)
            elif index in (8, 9, 13):
                difference = abs(left - right)
                similarities.append(
                    0.8 if difference == 1 else
                    0.5 if difference == 2 else 0.0)
            else:
                similarities.append(0.0)
        total_weight = sum(
            weights[index] if index < len(weights) else 1.0
            for index in range(len(similarities)))
        return sum(
            similarity * (weights[index] if index < len(weights) else 1.0)
            for index, similarity in enumerate(similarities)) / total_weight

    @staticmethod
    def _valid_action_list(valid_actions):
        if valid_actions is None:
            return []
        array = np.asarray(valid_actions)
        if array.ndim == 0:
            array = array.reshape(1)
        if array.dtype == np.bool_:
            values = np.flatnonzero(array).tolist()
        else:
            values = array.reshape(-1).tolist()
        result = []
        for value in values:
            try:
                action = int(value)
            except (TypeError, ValueError):
                continue
            if action >= 0 and action not in result:
                result.append(action)
        return sorted(result)

    def get_suggested_action(self, game_state, valid_actions,
                             exploration_rate=None, for_mcts=False):
        """Choose the highest-evidence valid action without random fallback."""
        valid = self._valid_action_list(valid_actions)
        if not valid:
            return None
        pattern = self.extract_strategy_pattern(game_state)
        candidates = {}

        def add_actions(entry, similarity):
            for action, stats in entry.get("actions", {}).items():
                if action not in valid:
                    continue
                count = int(stats.get("count", 0) or 0)
                if count < self.min_action_count:
                    continue
                quality = (
                    _finite_number(stats.get("reward", 0.0))
                    + 0.25 * _finite_number(
                        stats.get("success_rate", 0.0)))
                weight = similarity * math.sqrt(count)
                total, total_weight, observations = candidates.get(
                    action, (0.0, 0.0, 0))
                candidates[action] = (
                    total + quality * weight,
                    total_weight + weight,
                    observations + count,
                )

        exact = self.strategies.get(pattern)
        if exact:
            add_actions(exact, 1.0)
        if not candidates:
            similar = []
            for stored_pattern, entry in self.strategies.items():
                if stored_pattern == pattern:
                    continue
                similarity = self._pattern_similarity(stored_pattern, pattern)
                if similarity > 0.7:
                    similar.append((similarity, stored_pattern, entry))
            similar.sort(key=lambda item: (-item[0], repr(item[1])))
            for similarity, _, entry in similar[:5]:
                add_actions(entry, similarity)
        if not candidates:
            return None

        scored = []
        for action, (total, weight, count) in candidates.items():
            score = total / weight if weight else float("-inf")
            scored.append((score, count, action))
        score, _, action = max(
            scored, key=lambda item: (item[0], item[1], -item[2]))
        logging.debug(
            "Strategy memory suggested action %s with score %.4f",
            action, score)
        if for_mcts:
            value = 0.5 + 0.5 * math.tanh(score)
            return action, min(1.0, max(0.0, value))
        return action

    @staticmethod
    def _entry_value(item):
        pattern, entry = item
        action_evidence = sum(
            int(stats.get("count", 0) or 0)
            for stats in entry.get("actions", {}).values())
        return (
            action_evidence,
            int(entry.get("count", 0) or 0),
            _finite_number(entry.get("success_rate", 0.0)),
            _finite_number(entry.get("reward", 0.0)),
            int(entry.get("last_update", 0) or 0),
            repr(pattern),
        )

    def prune_memory(self):
        """Apply deterministic, value-ranked capacity bounds."""
        with self._lock:
            if len(self.strategies) > self.max_size:
                ranked = sorted(
                    self.strategies.items(),
                    key=self._entry_value, reverse=True)
                self.strategies = dict(ranked[:self.max_size])
                self.dirty = True
            if len(self.action_sequences) > self.max_size:
                self.action_sequences.sort(
                    key=lambda item: (
                        -abs(_finite_number(item[1])),
                        -_finite_number(item[1]),
                        repr(item[0])))
                del self.action_sequences[self.max_size:]
                self.dirty = True

    def _enhance_strategy_memory(self):
        """Compatibility API: consolidation is deterministic capacity pruning."""
        self.prune_memory()
        return len(self.strategies)
