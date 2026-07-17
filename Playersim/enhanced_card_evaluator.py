import json
import logging
import math
import re
import numpy as np
from typing import Dict, List, Any, Tuple


def _finite_number(value, default=0.0):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value, lower, upper, default=0.0):
    return max(lower, min(upper, _finite_number(value, default)))

class EnhancedCardEvaluator:
    """Advanced card evaluation system for Magic: The Gathering."""

    DIAGNOSTIC_SCHEMA_VERSION = 1
    DEFAULT_DIAGNOSTIC_MAX_EVENTS = 256
    DEFAULT_DIAGNOSTIC_MAX_TOTAL_EVENTS = 4096
    DEFAULT_DIAGNOSTIC_MAX_TOTAL_BYTES = 2 * 1024 * 1024
    DIAGNOSTIC_MAX_EVENT_BYTES = 64 * 1024
    DIAGNOSTIC_MAX_DEPTH = 6
    DIAGNOSTIC_MAX_NODES = 4096
    DIAGNOSTIC_MAX_CONTAINER_ITEMS = 64
    DIAGNOSTIC_MAX_STRING_LENGTH = 512
    DIAGNOSTIC_MAX_KEY_LENGTH = 256
    
    def __init__(self, game_state, stats_tracker=None, card_memory=None):
        """
        Initialize the card evaluator.
        
        Args:
            game_state: The current game state
            stats_tracker: Optional DeckStatsTracker for performance-based evaluation
            card_memory: Optional CardMemory for historical card performance
        """
        self.game_state = game_state
        self.stats_tracker = stats_tracker
        self.card_memory = card_memory
        
        # Card type weights for evaluation
        self.type_weights = {
            'creature': 1.0,
            'instant': 0.9,
            'sorcery': 0.85,
            'artifact': 0.8,
            'enchantment': 0.8,
            'planeswalker': 1.2,
            'land': 0.7
        }
        
        # Keyword weights for evaluation
        self.keyword_weights = {
            'flying': 0.3,
            'trample': 0.25,
            'deathtouch': 0.35,
            'lifelink': 0.3,
            'first strike': 0.25,
            'double strike': 0.45,
            'vigilance': 0.2,
            'haste': 0.3,
            'hexproof': 0.4,
            'indestructible': 0.5,
            'menace': 0.25,
            'reach': 0.15,
            'flash': 0.25,
            'defender': -0.1,
            'protection': 0.35,
            'ward': 0.3,
            'prowess': 0.2,
            'unblockable': 0.4
        }
        
        # Live-board synergy cache. This is transient derived state, not the
        # persisted empirical history owned by CardMemory.
        self._board_synergy_cache = {}
        
        # Only static card-characteristic scores are cached. Full evaluations
        # depend on mutable board state, perspective, turn, memory, and stats.
        self.evaluation_cache = {}
        self.cache_hits = 0
        self.cache_misses = 0

        # Evaluation explainability is deliberately opt-in.  Normal training
        # calls only pay one boolean check and retain the historical scalar-only
        # behavior.  Attributable fixed evaluations enable this recorder and
        # drain it at atomic action boundaries into the debug sidecar trace.
        self._diagnostics_enabled = False
        self._diagnostic_max_events = self.DEFAULT_DIAGNOSTIC_MAX_EVENTS
        self._diagnostic_max_total_events = \
            self.DEFAULT_DIAGNOSTIC_MAX_TOTAL_EVENTS
        self._diagnostic_max_total_bytes = \
            self.DEFAULT_DIAGNOSTIC_MAX_TOTAL_BYTES
        self._diagnostic_serialized_bytes = 0
        self._diagnostic_events = []
        self._diagnostic_event_keys = {}
        self._diagnostic_sequence = 0
        self._diagnostic_cache_hits_baseline = 0
        self._diagnostic_cache_misses_baseline = 0
        self._diagnostic_window_deduplicated = 0
        self._diagnostic_window_dropped = 0
        self._diagnostic_totals = {
            "calls": 0,
            "recorded": 0,
            "deduplicated": 0,
            "dropped": 0,
            "exceptions": 0,
            "fallbacks": 0,
            "sanitization_omissions": 0,
            "serialization_errors": 0,
        }

    def set_diagnostics_enabled(
            self, enabled: bool, *, max_events: int = None,
            max_total_events: int = None, max_total_bytes: int = None,
            reset: bool = True) -> None:
        """Enable bounded evaluation-call capture for attributed eval games.

        This is an instrumentation boundary, not a scoring mode: enabling it
        must never change the scalar returned by :meth:`evaluate_card`.
        """
        if max_events is not None:
            try:
                resolved_limit = int(max_events)
            except (TypeError, ValueError) as error:
                raise ValueError("max_events must be a positive integer") from error
            if resolved_limit <= 0:
                raise ValueError("max_events must be a positive integer")
            self._diagnostic_max_events = resolved_limit
        if max_total_events is not None:
            try:
                resolved_total_limit = int(max_total_events)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "max_total_events must be a positive integer") from error
            if resolved_total_limit <= 0:
                raise ValueError(
                    "max_total_events must be a positive integer")
            self._diagnostic_max_total_events = resolved_total_limit
        if max_total_bytes is not None:
            try:
                resolved_byte_limit = int(max_total_bytes)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "max_total_bytes must be a positive integer") from error
            if resolved_byte_limit <= 0:
                raise ValueError(
                    "max_total_bytes must be a positive integer")
            self._diagnostic_max_total_bytes = resolved_byte_limit
        self._diagnostics_enabled = bool(enabled)
        if reset:
            self._diagnostic_events = []
            self._diagnostic_event_keys = {}
            self._diagnostic_sequence = 0
            self._diagnostic_cache_hits_baseline = int(self.cache_hits)
            self._diagnostic_cache_misses_baseline = int(self.cache_misses)
            self._diagnostic_window_deduplicated = 0
            self._diagnostic_window_dropped = 0
            self._diagnostic_serialized_bytes = 0
            self._diagnostic_totals = {
                "calls": 0,
                "recorded": 0,
                "deduplicated": 0,
                "dropped": 0,
                "exceptions": 0,
                "fallbacks": 0,
                "sanitization_omissions": 0,
                "serialization_errors": 0,
            }
        elif not self._diagnostics_enabled:
            self._diagnostic_events = []
            self._diagnostic_event_keys = {}
            self._diagnostic_window_deduplicated = 0
            self._diagnostic_window_dropped = 0

    @classmethod
    def _diagnostic_sanitize(cls, value, *, initial_depth=0,
                             max_bytes=None):
        """Return strict deterministic JSON plus bounded-sanitizer metadata."""
        state = {"nodes": 0, "omissions": 0, "errors": 0, "active": set()}

        def type_name(item):
            item_type = type(item)
            return f"{item_type.__module__}.{item_type.__qualname__}"

        def omission(reason, item=None, **details):
            state["omissions"] += 1
            payload = {"reason": reason}
            if item is not None:
                payload["type"] = type_name(item)
            payload.update(details)
            return {"__diagnostic_omitted__": payload}

        def safe_key(key):
            try:
                if key is None:
                    text = "null"
                elif isinstance(key, (bool, str, int, np.integer)):
                    if isinstance(key, (int, np.integer)) \
                            and int(key).bit_length() > 256:
                        text = f"<int:{int(key).bit_length()}-bits>"
                    else:
                        text = str(key)
                elif isinstance(key, (float, np.floating)):
                    number = float(key)
                    text = repr(number) if math.isfinite(number) \
                        else "nonfinite"
                else:
                    text = f"<{type_name(key)}>"
            except Exception:
                state["errors"] += 1
                text = "<unprintable-key>"
            if len(text) > cls.DIAGNOSTIC_MAX_KEY_LENGTH:
                state["omissions"] += 1
                text = text[:cls.DIAGNOSTIC_MAX_KEY_LENGTH - 3] + "..."
            return text

        def walk(item, depth):
            state["nodes"] += 1
            if state["nodes"] > cls.DIAGNOSTIC_MAX_NODES:
                return omission("node_budget", item)
            if item is None or isinstance(item, bool):
                return item
            if isinstance(item, str):
                if len(item) > cls.DIAGNOSTIC_MAX_STRING_LENGTH:
                    state["omissions"] += 1
                    return item[:cls.DIAGNOSTIC_MAX_STRING_LENGTH - 3] + "..."
                return item
            if isinstance(item, (int, np.integer)):
                number = int(item)
                if number.bit_length() > 256:
                    return omission(
                        "integer_bit_budget", item,
                        bit_length=number.bit_length())
                return number
            if isinstance(item, (float, np.floating)):
                number = float(item)
                return number if math.isfinite(number) else None
            if depth >= cls.DIAGNOSTIC_MAX_DEPTH:
                return omission("depth_budget", item, depth=depth)

            track_identity = isinstance(
                item, (dict, list, tuple, set, frozenset, np.ndarray))
            identity = id(item)
            if track_identity and identity in state["active"]:
                return omission("cycle", item)
            if track_identity:
                state["active"].add(identity)
            try:
                if isinstance(item, np.ndarray):
                    flat = item.reshape(-1)
                    limit = cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS
                    result = [walk(value, depth + 1)
                              for value in flat[:limit].tolist()]
                    if flat.size > limit:
                        result.append(omission(
                            "container_items", item,
                            omitted_items=int(flat.size - limit),
                            shape=list(item.shape)))
                    return result
                if isinstance(item, dict):
                    result = {}
                    omitted_items = max(
                        0, len(item) - cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS)
                    for index, (key, child) in enumerate(item.items()):
                        if index >= cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS:
                            break
                        output_key = safe_key(key)
                        base_key = output_key
                        suffix = 2
                        while output_key in result:
                            suffix_text = f"#{suffix}"
                            output_key = base_key[
                                :cls.DIAGNOSTIC_MAX_KEY_LENGTH
                                - len(suffix_text)] + suffix_text
                            suffix += 1
                        result[output_key] = walk(child, depth + 1)
                    if omitted_items:
                        result["__omitted_items__"] = omitted_items
                        state["omissions"] += 1
                    return result
                if isinstance(item, (list, tuple)):
                    result = [walk(child, depth + 1) for child in
                              item[:cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS]]
                    if len(item) > cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS:
                        result.append(omission(
                            "container_items", item,
                            omitted_items=len(item)
                            - cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS))
                    return result
                if isinstance(item, (set, frozenset)):
                    if len(item) > cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS:
                        return omission(
                            "set_items", item, item_count=len(item))
                    sanitized = [walk(child, depth + 1) for child in item]
                    sanitized.sort(key=lambda child: json.dumps(
                        child, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False))
                    return sanitized
                scalar = getattr(item, "item", None)
                if callable(scalar):
                    try:
                        resolved = scalar()
                    except Exception:
                        state["errors"] += 1
                    else:
                        if resolved is not item:
                            return walk(resolved, depth + 1)
                summary = {"type": type_name(item)}
                for attribute in ("card_id", "id", "name"):
                    try:
                        attribute_value = getattr(item, attribute, None)
                    except Exception:
                        state["errors"] += 1
                        continue
                    if isinstance(attribute_value, (
                            str, int, float, bool, np.generic)):
                        summary[attribute] = walk(
                            attribute_value, depth + 1)
                return summary
            except Exception:
                state["errors"] += 1
                return omission("sanitization_error", item)
            finally:
                if track_identity:
                    state["active"].discard(identity)

        safe = walk(value, initial_depth)
        byte_limit = int(max_bytes or cls.DIAGNOSTIC_MAX_EVENT_BYTES)
        try:
            encoded = json.dumps(
                safe, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
        except Exception:
            state["errors"] += 1
            safe = omission("json_encoding_error", value)
            encoded = json.dumps(
                safe, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > byte_limit:
            original_bytes = len(encoded)
            safe = omission(
                "serialized_byte_budget", value,
                byte_budget=byte_limit, original_bytes=original_bytes)
            encoded = json.dumps(
                safe, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
        return safe, {
            "nodes": state["nodes"],
            "omissions": state["omissions"],
            "errors": state["errors"],
            "serialized_bytes": len(encoded),
        }

    @classmethod
    def _diagnostic_json_safe(cls, value, depth=0):
        try:
            safe, _metadata = cls._diagnostic_sanitize(
                value, initial_depth=depth)
            return safe
        except Exception:
            value_type = type(value)
            return {"__diagnostic_omitted__": {
                "reason": "sanitizer_failure",
                "type": f"{value_type.__module__}.{value_type.__qualname__}",
            }}

    def _diagnostic_perspective_label(self, perspective):
        gs = self.game_state
        if perspective is getattr(gs, "p1", None):
            return "p1"
        if perspective is getattr(gs, "p2", None):
            return "p2"
        if isinstance(perspective, str):
            return perspective
        if isinstance(perspective, dict) and perspective.get("name"):
            return str(perspective["name"])
        return None

    def _start_evaluation_diagnostic(
            self, card_id, context, supplied_details):
        """Allocate a diagnostic event only on the explicit eval boundary."""
        self._diagnostic_totals["calls"] += 1
        return {
            "runtime_card_id": self._diagnostic_json_safe(card_id),
            # The scoring path resolves the canonical ID exactly once and
            # replaces this placeholder. Instrumentation must not duplicate a
            # potentially user-supplied canonicalizer call.
            "canonical_card_id": self._diagnostic_json_safe(card_id),
            "card_name": None,
            "context": str(context or "general").strip().lower(),
            "context_details": self._diagnostic_json_safe(
                dict(supplied_details or {})),
            "perspective": None,
            "components": {
                "base": 0.0,
                "context": 0.0,
                "history": 0.0,
                "stats": 0.0,
            },
            "history": {
                "source": "none",
                "overall_games": 0.0,
                "archetype": "unknown",
                "archetype_games": 0.0,
                "reliable": False,
                "fallback_reason": None,
            },
            "adjustments": {
                "weighted_score": 0.0,
                "game_stage": "mid",
                "stage_multiplier": 1.0,
                "position": "even",
                "position_multiplier": 1.0,
                "aggression_level": 0.5,
                "aggression_multiplier": 1.0,
                "pre_clamp": 0.0,
            },
            "final_score": 0.0,
            "flags": {
                "invalid": False,
                "invalid_reason": None,
                "fallback": False,
                "exception": False,
                "clamped": False,
            },
        }

    def _record_diagnostic(self, event) -> None:
        if not self._diagnostics_enabled or event is None:
            return
        try:
            raw_flags = event.get("flags", {}) \
                if isinstance(event, dict) else {}
            if raw_flags.get("exception"):
                self._diagnostic_totals["exceptions"] += 1
            if raw_flags.get("fallback"):
                self._diagnostic_totals["fallbacks"] += 1

            safe_event, sanitization = self._diagnostic_sanitize(
                event, max_bytes=self.DIAGNOSTIC_MAX_EVENT_BYTES)
            self._diagnostic_totals["sanitization_omissions"] += int(
                sanitization.get("omissions", 0))
            self._diagnostic_totals["serialization_errors"] += int(
                sanitization.get("errors", 0))
            if not isinstance(safe_event, dict) \
                    or "__diagnostic_omitted__" in safe_event:
                safe_event = {
                    "runtime_card_id": self._diagnostic_json_safe(
                        event.get("runtime_card_id")
                        if isinstance(event, dict) else None),
                    "canonical_card_id": self._diagnostic_json_safe(
                        event.get("canonical_card_id")
                        if isinstance(event, dict) else None),
                    "context": self._diagnostic_json_safe(
                        event.get("context")
                        if isinstance(event, dict) else None),
                    "final_score": self._diagnostic_json_safe(
                        event.get("final_score")
                        if isinstance(event, dict) else None),
                    "flags": self._diagnostic_json_safe(raw_flags),
                    "serialization": {
                        "omitted": True,
                        "reason": "event_serialized_byte_budget",
                        **sanitization,
                    },
                }
            elif sanitization.get("omissions") \
                    or sanitization.get("errors"):
                safe_event["serialization"] = {
                    "omitted": False,
                    **sanitization,
                }
            encoded_event = json.dumps(
                safe_event, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded_event) > self.DIAGNOSTIC_MAX_EVENT_BYTES:
                self._diagnostic_totals["sanitization_omissions"] += 1
                safe_event = {
                    "runtime_card_id": self._diagnostic_json_safe(
                        event.get("runtime_card_id")
                        if isinstance(event, dict) else None),
                    "canonical_card_id": self._diagnostic_json_safe(
                        event.get("canonical_card_id")
                        if isinstance(event, dict) else None),
                    "context": self._diagnostic_json_safe(
                        event.get("context")
                        if isinstance(event, dict) else None),
                    "final_score": self._diagnostic_json_safe(
                        event.get("final_score")
                        if isinstance(event, dict) else None),
                    "flags": self._diagnostic_json_safe(raw_flags),
                    "serialization": {
                        "omitted": True,
                        "reason": "event_serialized_byte_budget",
                        "original_bytes": len(encoded_event),
                        "byte_budget": self.DIAGNOSTIC_MAX_EVENT_BYTES,
                    },
                }
            key = json.dumps(
                safe_event, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False)
            existing_index = self._diagnostic_event_keys.get(key)
            if existing_index is not None:
                existing = self._diagnostic_events[existing_index]
                previous_count = existing["repeat_count"]
                existing["repeat_count"] = previous_count + 1
                self._diagnostic_window_deduplicated += 1
                self._diagnostic_totals["deduplicated"] += 1
                return
            if (self._diagnostic_totals["recorded"] >=
                    self._diagnostic_max_total_events):
                self._diagnostic_window_dropped += 1
                self._diagnostic_totals["dropped"] += 1
                return
            if len(self._diagnostic_events) >= self._diagnostic_max_events:
                self._diagnostic_window_dropped += 1
                self._diagnostic_totals["dropped"] += 1
                return
            safe_event["sequence"] = self._diagnostic_sequence
            safe_event["repeat_count"] = 1
            self._diagnostic_sequence += 1
            self._diagnostic_event_keys[key] = len(self._diagnostic_events)
            self._diagnostic_events.append(safe_event)
            self._diagnostic_totals["recorded"] += 1
        except Exception as error:
            # Instrumentation must never perturb action selection or rewards.
            self._diagnostic_window_dropped += 1
            self._diagnostic_totals["dropped"] += 1
            self._diagnostic_totals["serialization_errors"] += 1
            logging.warning(
                "Could not record EnhancedCardEvaluator diagnostic: %s", error)

    def drain_diagnostics(self):
        """Drain one bounded window for attachment to an atomic action."""
        if not self._diagnostics_enabled:
            return None
        if (not self._diagnostic_events
                and not self._diagnostic_window_deduplicated
                and not self._diagnostic_window_dropped):
            return None
        events = list(self._diagnostic_events)
        dropped_events = self._diagnostic_window_dropped
        payload = None
        while True:
            candidate = {
                "schema_version": self.DIAGNOSTIC_SCHEMA_VERSION,
                "capture_scope": "pre-and-during-atomic-action",
                "causal_attribution": False,
                "events": events,
                "deduplicated_events":
                    self._diagnostic_window_deduplicated,
                "dropped_events": dropped_events,
            }
            try:
                encoded = json.dumps(
                    candidate, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False).encode("utf-8")
            except Exception:
                self._diagnostic_totals["serialization_errors"] += 1
                encoded = b""
            remaining = max(
                0, self._diagnostic_max_total_bytes
                - self._diagnostic_serialized_bytes)
            if encoded and len(encoded) <= remaining:
                payload = candidate
                self._diagnostic_serialized_bytes += len(encoded)
                break
            if events:
                removed = events.pop()
                removed_calls = max(
                    1, int(removed.get("repeat_count", 1) or 1)) \
                    if isinstance(removed, dict) else 1
                dropped_events += removed_calls
                self._diagnostic_totals["dropped"] += removed_calls
                self._diagnostic_totals["recorded"] = max(
                    0, self._diagnostic_totals["recorded"] - 1)
                self._diagnostic_totals["sanitization_omissions"] += 1
                continue
            # Even an empty dropped-only envelope no longer fits. The game
            # summary still reports the exact omission and byte exhaustion.
            self._diagnostic_totals["serialization_errors"] += \
                int(not encoded)
            break
        self._diagnostic_events = []
        self._diagnostic_event_keys = {}
        self._diagnostic_window_deduplicated = 0
        self._diagnostic_window_dropped = 0
        return payload

    def diagnostic_totals(self):
        """Return game-level evaluator instrumentation and cache telemetry."""
        return {
            "schema_version": self.DIAGNOSTIC_SCHEMA_VERSION,
            **dict(self._diagnostic_totals),
            "event_budget": int(self._diagnostic_max_total_events),
            "serialized_bytes": int(self._diagnostic_serialized_bytes),
            "serialized_byte_budget": int(
                self._diagnostic_max_total_bytes),
            "event_byte_budget": int(self.DIAGNOSTIC_MAX_EVENT_BYTES),
            "sanitizer_limits": {
                "max_depth": int(self.DIAGNOSTIC_MAX_DEPTH),
                "max_nodes": int(self.DIAGNOSTIC_MAX_NODES),
                "max_container_items": int(
                    self.DIAGNOSTIC_MAX_CONTAINER_ITEMS),
                "max_string_length": int(
                    self.DIAGNOSTIC_MAX_STRING_LENGTH),
                "max_key_length": int(self.DIAGNOSTIC_MAX_KEY_LENGTH),
            },
            "cache_hits": max(
                0, int(self.cache_hits)
                - int(self._diagnostic_cache_hits_baseline)),
            "cache_misses": max(
                0, int(self.cache_misses)
                - int(self._diagnostic_cache_misses_baseline)),
            "pending": len(self._diagnostic_events),
        }

    def _abandon_diagnostic(self, stage, error):
        """Disable one explanation without changing the scalar scoring path."""
        self._diagnostic_totals["serialization_errors"] += 1
        self._diagnostic_totals["dropped"] += 1
        self._diagnostic_window_dropped += 1
        logging.warning(
            "EnhancedCardEvaluator diagnostic stage %s was omitted: %s",
            stage, error)
        return None
    
    def evaluate_card(self, card_id: int, context: str = "general", context_details: Dict[str, Any] = None) -> float:
        """
        Evaluate a card in the given context with enhanced historical performance data.
        
        Args:
            card_id: The ID of the card to evaluate
            context: The context for evaluation ("general", "play", "attack", "block", etc.)
            context_details: Additional context information for more nuanced evaluation
                
        Returns:
            float: The card's evaluation score
        """
        capture_diagnostic = bool(self._diagnostics_enabled)
        diagnostic = None
        if capture_diagnostic:
            try:
                diagnostic = self._start_evaluation_diagnostic(
                    card_id, context,
                    context_details
                    if isinstance(context_details, dict) else {})
            except Exception as error:
                # Explainability can fail closed without changing the score.
                self._diagnostic_totals["serialization_errors"] += 1
                self._diagnostic_totals["dropped"] += 1
                self._diagnostic_window_dropped += 1
                logging.warning(
                    "Could not start EnhancedCardEvaluator diagnostic: %s",
                    error)
        try:
            gs = self.game_state
            card = gs._safe_get_card(card_id)
            if not card:
                if diagnostic is not None:
                    diagnostic["flags"].update({
                        "invalid": True,
                        "invalid_reason": "card_not_found",
                    })
                    self._record_diagnostic(diagnostic)
                return 0.0

            supplied_details = dict(context_details or {})
            context_details = {
                "game_stage": "mid",
                "position": "even",
                "aggression_level": 0.5,
                "turn": getattr(gs, "turn", 0),
                "phase": getattr(gs, "phase", None),
                "deck_archetype": "unknown",
            }
            context_details.update(supplied_details)
            if "turn" not in supplied_details and "current_turn" in supplied_details:
                context_details["turn"] = supplied_details["current_turn"]
            if "phase" not in supplied_details and "current_phase" in supplied_details:
                context_details["phase"] = supplied_details["current_phase"]

            context = str(context or "general").strip().lower()
            perspective = self._resolve_perspective(context_details)
            analytics_card_id = self._analytics_card_id(card_id)
            if not context_details.get("turn_is_player_relative", False):
                context_details["turn"] = self._player_turn_number(
                    perspective, context_details.get("turn", 0))

            if diagnostic is not None:
                try:
                    diagnostic["canonical_card_id"] = \
                        self._diagnostic_json_safe(analytics_card_id)
                    diagnostic["card_name"] = str(
                        getattr(card, "name", "") or "") or None
                    diagnostic["context"] = context
                    diagnostic["context_details"] = \
                        self._diagnostic_json_safe(context_details)
                    diagnostic["perspective"] = \
                        self._diagnostic_perspective_label(perspective)
                except Exception as error:
                    diagnostic = self._abandon_diagnostic(
                        "resolved_context", error)
            
            # Calculate base value (static card evaluation)
            base_value = self._get_cached_base_value(card_id, card)
            if diagnostic is not None:
                diagnostic["components"]["base"] = base_value
            
            # Add context-specific value
            context_value = 0.0
            if context == "play":
                context_value = base_value
            elif context == "attack":
                context_value = self._evaluate_for_attack(card_id, perspective)
            elif context == "block":
                context_value = self._evaluate_for_block(card_id, perspective)
            elif context == "discard":
                context_value = self._evaluate_for_discard(card_id, perspective)
            if diagnostic is not None:
                diagnostic["components"]["context"] = context_value

            # Invalid combat choices must remain invalid after components are
            # combined; a large static score must not turn them positive.
            if context in {"attack", "block"} and context_value <= -5.0:
                if diagnostic is not None:
                    diagnostic["adjustments"].update({
                        "weighted_score": base_value * 0.6
                        + context_value * 0.25,
                        "pre_clamp": -5.0,
                    })
                    diagnostic["final_score"] = -5.0
                    diagnostic["flags"].update({
                        "invalid": True,
                        "invalid_reason": f"illegal_{context}",
                    })
                    self._record_diagnostic(diagnostic)
                return -5.0
            
            # CardMemory is the primary historical source. DeckStats tracks many
            # of the same game outcomes, so it is used only when CardMemory has
            # no entry or cannot be read rather than as an additive signal.
            history_value = 0.0
            stats_value = 0.0
            card_memory_used = False
            deck_archetype = str(
                context_details.get(
                    "deck_archetype", "unknown") or "unknown"
            ).strip().lower() or "unknown"
            if diagnostic is not None:
                diagnostic["history"]["archetype"] = deck_archetype

            if self.card_memory:
                try:
                    get_memory_stats = getattr(
                        self.card_memory, "get_card_stats", None)
                    memory_stats = (
                        get_memory_stats(analytics_card_id)
                        if callable(get_memory_stats) else None)
                    has_reliable_memory = not callable(get_memory_stats)
                    overall_effectiveness = None
                    if isinstance(memory_stats, dict) and memory_stats:
                        overall_games = _clamp(
                            memory_stats.get("games_played", 0),
                            0.0, 1e12, 0.0)
                        archetypes = memory_stats.get(
                            "archetype_performance", {})
                        archetype_stats = (
                            archetypes.get(deck_archetype)
                            if isinstance(archetypes, dict) else None)
                        archetype_game_count = (
                            _clamp(
                                archetype_stats.get("games", 0),
                                0.0, 1e12, 0.0)
                            if isinstance(archetype_stats, dict) else 0.0)
                        archetype_games = (
                            archetype_game_count >= 3
                            if isinstance(archetype_stats, dict) else False)
                        has_reliable_memory = bool(
                            archetype_games or overall_games >= 5)
                        if (not archetype_games and overall_games >= 5
                                and "effectiveness_rating" in memory_stats):
                            # A first sample in a new archetype must not make a
                            # mature overall record less useful. The archetype
                            # accessor intentionally returns neutral for sparse
                            # buckets, so read the reliable overall value here.
                            overall_effectiveness = _clamp(
                                memory_stats.get("effectiveness_rating"),
                                0.0, 1.0, 0.5)
                        if diagnostic is not None:
                            diagnostic["history"].update({
                                "overall_games": overall_games,
                                "archetype_games": archetype_game_count,
                                "reliable": has_reliable_memory,
                            })
                    elif diagnostic is not None:
                        diagnostic["history"]["fallback_reason"] = \
                            "card_memory_missing"
                    if has_reliable_memory:
                        memory_history_value = 0.0
                        effectiveness = (
                            overall_effectiveness
                            if overall_effectiveness is not None
                            else _clamp(
                                self.card_memory.get_effectiveness_for_archetype(
                                    analytics_card_id, deck_archetype),
                                0.0, 1.0, 0.5))
                        memory_history_value += (effectiveness - 0.5) * 1.5

                        optimal_turn = _clamp(
                            self.card_memory.get_optimal_play_turn(
                                analytics_card_id),
                            0.0, 1000.0, 0.0)
                        if optimal_turn > 0:
                            current_turn = _clamp(
                                context_details.get("turn", 0),
                                0.0, 1000.0, 0.0)
                            turn_proximity = 1.0 - min(
                                abs(current_turn - optimal_turn) / 3.0, 1.0)
                            memory_history_value += turn_proximity * 0.3

                        history_value = memory_history_value
                        card_memory_used = True
                        if diagnostic is not None:
                            diagnostic["history"].update({
                                "source": "card_memory",
                                "reliable": True,
                                "effectiveness": effectiveness,
                                "optimal_play_turn": optimal_turn,
                                "fallback_reason": None,
                            })
                    elif diagnostic is not None and diagnostic[
                            "history"].get("fallback_reason") is None:
                        diagnostic["history"]["fallback_reason"] = \
                            "card_memory_sparse"
                except Exception as e:
                    logging.warning(
                        "Error getting card memory data; falling back to "
                        f"DeckStats: {e}")
                    if diagnostic is not None:
                        diagnostic["history"].update({
                            "reliable": False,
                            "fallback_reason": "card_memory_error",
                            "card_memory_error":
                                f"{type(e).__name__}: {e}",
                        })
            elif diagnostic is not None:
                diagnostic["history"]["fallback_reason"] = \
                    "card_memory_unavailable"

            if not card_memory_used and self.stats_tracker:
                if diagnostic is not None:
                    stats_value, stats_evidence = self._get_stats_value(
                        card_id, include_evidence=True)
                    diagnostic["history"].update({
                        "source": "deck_stats",
                        "deck_stats_games": stats_evidence.get(
                            "games_played", 0.0),
                        "deck_stats_wins": stats_evidence.get("wins", 0.0),
                        "deck_stats_draws": stats_evidence.get("draws", 0.0),
                    })
                    diagnostic["flags"]["fallback"] = True
                else:
                    stats_value = self._get_stats_value(card_id)
            if diagnostic is not None:
                diagnostic["components"].update({
                    "history": history_value,
                    "stats": stats_value,
                })
            
            # Calculate total value with weighted components
            weighted_score = (
                base_value * 0.6 +     # 60% weight to base card evaluation
                context_value * 0.25 +  # 25% weight to context-specific evaluation
                history_value * 0.1 +   # 10% weight to primary card history
                stats_value * 0.05      # 5% DeckStats fallback only
            )
            total_value = weighted_score
            
            # Apply game stage multiplier
            stage_multipliers = {
                "early": 0.9,
                "mid": 1.0,
                "late": 1.1
            }
            game_stage = str(
                context_details.get("game_stage", "mid") or "mid").lower()
            stage_multiplier = stage_multipliers.get(game_stage, 1.0)
            total_value *= stage_multiplier
            
            # Apply position adjustment
            position_adjustments = {
                "dominating": 1.2,
                "ahead": 1.1,
                "even": 1.0,
                "behind": 0.9,
                "struggling": 0.8
            }
            position = str(
                context_details.get("position", "even") or "even").lower()
            position_multiplier = position_adjustments.get(position, 1.0)
            total_value *= position_multiplier
            
            # Apply aggression adjustment
            aggression_level = _clamp(
                context_details.get("aggression_level", 0.5), 0.0, 1.0, 0.5)
            aggression_multiplier = 1.0
            if self._is_type(card, "creature") and hasattr(card, 'power'):
                card_power = _finite_number(card.power)
                card_toughness = _finite_number(
                    getattr(card, 'toughness', 0))
                # More aggressive strategy values offensive creatures higher
                if card_power > 2:
                    aggression_multiplier = \
                        1.0 + (aggression_level - 0.5) * 0.4
                    total_value *= aggression_multiplier
                # Less aggressive strategy values defensive creatures higher
                elif card_toughness > card_power + 1:
                    aggression_multiplier = \
                        1.0 - (aggression_level - 0.5) * 0.2
                    total_value *= aggression_multiplier
            
            # Evaluator values feed both observation features and reward
            # shaping. Keep malformed/extreme card data from producing NaN or
            # unbounded rewards while preserving ordinary card ordering.
            final_score = _clamp(total_value, -5.0, 10.0, 0.0)
            if diagnostic is not None:
                diagnostic["adjustments"].update({
                    "weighted_score": weighted_score,
                    "game_stage": game_stage,
                    "stage_multiplier": stage_multiplier,
                    "position": position,
                    "position_multiplier": position_multiplier,
                    "aggression_level": aggression_level,
                    "aggression_multiplier": aggression_multiplier,
                    "pre_clamp": total_value,
                })
                diagnostic["final_score"] = final_score
                diagnostic["flags"]["clamped"] = (
                    not math.isfinite(_finite_number(total_value, float("nan")))
                    or final_score != total_value)
                self._record_diagnostic(diagnostic)
            return final_score
        except Exception as e:
            logging.error(f"Error evaluating card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            if diagnostic is not None:
                diagnostic["final_score"] = 0.0
                diagnostic["flags"]["exception"] = True
                diagnostic["error"] = f"{type(e).__name__}: {e}"
                self._record_diagnostic(diagnostic)
            return 0.0  # Default to neutral value on error
    
    def record_card_performance(self, card_id: int, game_result: Dict) -> None:
        """
        Compatibility wrapper for recording card performance.

        AlphaZeroMTGEnv is the production writer at game completion. Production
        callers must not also invoke this wrapper for the same result because
        CardMemory counters would be incremented twice.
        
        Args:
            card_id: ID of the card
            game_result: Dictionary with game result information
        """
        if self.card_memory:
            try:
                result = dict(game_result or {})

                # Get card name if available
                card = self.game_state._safe_get_card(card_id)
                if card and hasattr(card, 'name'):
                    result['card_name'] = card.name
                    
                    # Add CMC if available
                    if hasattr(card, 'cmc'):
                        result['cmc'] = card.cmc
                
                self.card_memory.update_card_performance(
                    self._analytics_card_id(card_id), result)
            except Exception as e:
                logging.error(f"Error recording card performance: {e}")

    @staticmethod
    def _is_type(card, card_type: str) -> bool:
        types = getattr(card, "card_types", ()) or ()
        if isinstance(types, str):
            types = (types,)
        return card_type.lower() in {
            str(value).lower() for value in types}

    @staticmethod
    def _value_signature(value):
        if isinstance(value, np.ndarray):
            value = value.tolist()
        if isinstance(value, set):
            return tuple(sorted(str(item).lower() for item in value))
        if isinstance(value, (list, tuple)):
            return tuple(str(item).lower() for item in value)
        return str(value or "").lower()

    def _card_signature(self, card):
        """Return the live characteristics that affect static evaluation."""
        return (
            str(getattr(card, "name", "") or ""),
            _finite_number(getattr(card, "cmc", 0)),
            self._value_signature(getattr(card, "card_types", ())),
            self._value_signature(getattr(card, "subtypes", ())),
            _finite_number(getattr(card, "power", 0)),
            _finite_number(getattr(card, "toughness", 0)),
            str(getattr(card, "oracle_text", "") or ""),
            self._value_signature(getattr(card, "colors", ())),
        )

    def _get_cached_base_value(self, card_id, card) -> float:
        cache_key = (
            "base", card_id, self._card_signature(card),
            tuple(sorted(self.type_weights.items())),
            tuple(sorted(self.keyword_weights.items())),
        )
        if cache_key in self.evaluation_cache:
            self.cache_hits += 1
            return self.evaluation_cache[cache_key]

        self.cache_misses += 1
        value = self._calculate_base_value(card)
        if len(self.evaluation_cache) >= 1000:
            self.evaluation_cache.clear()
        self.evaluation_cache[cache_key] = value
        return value

    def _resolve_perspective(self, context_details):
        gs = self.game_state
        for key in ("perspective_player", "perspective", "controller"):
            value = context_details.get(key)
            if value is gs.p1:
                return gs.p1
            if value is gs.p2:
                return gs.p2
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"p1", "player1", "player 1"}:
                    return gs.p1
                if normalized in {"p2", "player2", "player 2"}:
                    return gs.p2
        return gs.p1 if gs.agent_is_p1 else gs.p2

    def _analytics_card_id(self, card_id):
        """Return the stable printing ID used by statistics systems."""
        canonicalize = getattr(self.game_state, "canonical_card_id", None)
        if callable(canonicalize):
            try:
                return canonicalize(card_id)
            except Exception:
                pass
        return card_id

    def _player_turn_number(self, perspective, global_turn) -> int:
        """Translate the engine's alternating turn to this player's turn."""
        try:
            global_turn = int(global_turn)
        except (TypeError, ValueError, OverflowError):
            return 0
        if global_turn <= 0:
            return 0
        return ((global_turn + 1) // 2
                if perspective is self.game_state.p1 else global_turn // 2)

    def _has_keyword(self, card_id, card, keyword: str) -> bool:
        checker = getattr(self.game_state, "check_keyword", None)
        if callable(checker):
            try:
                return bool(checker(card_id, keyword))
            except Exception:
                pass
        return keyword.lower() in str(
            getattr(card, "oracle_text", "") or "").lower()

    def _can_block(self, blocker_id, attacker_id) -> bool:
        targeting = getattr(self.game_state, "targeting_system", None)
        checker = getattr(targeting, "check_can_be_blocked", None)
        if callable(checker):
            try:
                return bool(checker(attacker_id, blocker_id))
            except Exception:
                return False
        return True
    
    def _calculate_base_value(self, card) -> float:
        """
        Calculate the base value of a card with improved evaluation criteria.
        
        Args:
            card: The card object to evaluate
            
        Returns:
            float: Base value of the card
        """
        if not card:
            return 0.0
        
        value = 0.0
        
        # Value based on mana cost - refined curve
        cmc = _clamp(getattr(card, 'cmc', 0), 0.0, 100.0, 0.0)
        if hasattr(card, 'cmc'):
            # Cards with CMC 2-4 are generally most valuable
            if 2 <= cmc <= 4:
                value += cmc * 0.8
            elif cmc < 2:
                # Low cost cards - higher value for 1-drops than 0-drops
                if cmc == 1:
                    value += 0.9
                else:  # 0-cost cards
                    value += 0.5
            else:
                # Diminishing returns for high CMC, but still valuable
                # More granular scaling for high cost cards
                if cmc <= 6:
                    value += 4.0 + (cmc - 4) * 0.5  # 5 CMC = 4.5, 6 CMC = 5.0
                else:
                    value += 5.0 + (cmc - 6) * 0.3  # Slower increase beyond 6 CMC
        
        # Value based on card type - with refined weights
        card_types = getattr(card, 'card_types', ()) or ()
        if isinstance(card_types, str):
            card_types = (card_types,)
        normalized_types = tuple(str(value).lower() for value in card_types)
        if normalized_types:
            for card_type in normalized_types:
                value += self.type_weights.get(card_type.lower(), 0.0)

            # Bonus once for multitype cards (e.g., artifact creatures).
            if len(normalized_types) > 1:
                value += 0.2
        
        # Value based on creature stats with more nuanced evaluation
        if 'creature' in normalized_types:
            if hasattr(card, 'power') and hasattr(card, 'toughness'):
                # Basic stats value
                power = _clamp(card.power, 0.0, 100.0, 0.0)
                toughness = _clamp(card.toughness, 0.0, 100.0, 0.0)
                
                # Different formulas for different creature profiles
                if power >= 2 * toughness:  # Glass cannon
                    stats_value = power * 0.7 + toughness * 0.3
                elif toughness >= 2 * power:  # Wall/defensive
                    stats_value = power * 0.4 + toughness * 0.6
                else:  # Balanced
                    stats_value = (power + toughness) / 2
                
                # Additional value for efficient stat-to-cost ratio
                if cmc > 0:
                    efficiency = (power + toughness) / cmc
                    if efficiency > 2:  # Very efficient
                        stats_value *= 1.3
                    elif efficiency > 1:  # Good efficiency
                        stats_value *= 1.1
                
                # Special case for 0 power creatures
                if power == 0:
                    stats_value *= 0.5
                    # But if it has a good ability, don't penalize as much
                    if len(str(getattr(card, 'oracle_text', '') or '')) > 50:
                        stats_value *= 1.5  # Partial restoration of value
                
                value += stats_value
        
        # Value based on keywords with more comprehensive evaluation
        oracle_text = str(getattr(card, 'oracle_text', '') or '').lower()
        if oracle_text:
            
            # Count keywords for synergistic value
            keyword_count = 0
            keyword_value = 0
            
            for keyword, weight in self.keyword_weights.items():
                if keyword in oracle_text:
                    keyword_value += weight
                    keyword_count += 1
                    
                    # Special synergies between keywords
                    if keyword == 'deathtouch' and 'first strike' in oracle_text:
                        keyword_value += 0.3  # Bonus for deathtouch + first strike
                    elif keyword == 'trample' and 'double strike' in oracle_text:
                        keyword_value += 0.4  # Bonus for trample + double strike
            
            # Bonus for multiple keywords (synergy)
            if keyword_count > 1:
                keyword_value *= 1 + (keyword_count - 1) * 0.1
                
            value += keyword_value
        
        # Enhanced value for card advantage
        if oracle_text:
            card_text = oracle_text
            
            # Card draw effects with more granular evaluation
            if 'draw a card' in card_text:
                value += 0.5
                # Check if it's an easy condition
                if 'when' in card_text and 'enters the battlefield' in card_text:
                    value += 0.2  # Bonus for ETB draw
            elif 'draw two cards' in card_text:
                value += 1.0
                if 'you may' in card_text or 'if' in card_text:  # Conditional
                    value -= 0.3
            elif 'draw three cards' in card_text:
                value += 1.5
                if 'you may' in card_text or 'if' in card_text:  # Conditional
                    value -= 0.5
            
            # Value for "cantrips" (spells that replace themselves)
            if ('draw a card' in card_text and 
                any(t in normalized_types for t in ['instant', 'sorcery'])):
                value += 0.2
            
            # Card selection (scry, surveil, etc.)
            if 'scry' in card_text:
                # Extract scry amount
                scry_match = re.search(r'scry (\d+)', card_text)
                if scry_match:
                    scry_amount = int(scry_match.group(1))
                    value += min(0.4, scry_amount * 0.1)  # Cap at 0.4
            
            if 'surveil' in card_text:
                # Extract surveil amount
                surveil_match = re.search(r'surveil (\d+)', card_text)
                if surveil_match:
                    surveil_amount = int(surveil_match.group(1))
                    value += min(0.5, surveil_amount * 0.12)  # Slightly better than scry

            # Impulse draw is real card access even though it does not use the
            # word "draw". Longer permissions are modestly more flexible.
            impulse_match = re.search(
                r"exile the top\s+(?:(\d+|one|two|three|four|five)\s+)?card",
                card_text)
            if impulse_match and re.search(r"\bmay (?:play|cast)\b", card_text):
                word_counts = {
                    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
                raw_count = impulse_match.group(1)
                impulse_count = (
                    int(raw_count) if raw_count and raw_count.isdigit()
                    else word_counts.get(raw_count, 1))
                value += min(1.0, impulse_count * 0.45)
                if "end of your next turn" in card_text:
                    value += 0.1

            life_gain_match = re.search(r"\bgain(?:s)?\s+(\d+)\s+life\b", card_text)
            if life_gain_match:
                value += min(0.6, int(life_gain_match.group(1)) * 0.12)
                    
            # Removal effects with quality assessment
            removal_value = 0
            
            # Check for different types of removal
            if 'destroy target' in card_text:
                removal_value = 0.7
                
                # Check if it's conditional
                if 'if' in card_text or 'only if' in card_text or 'unless' in card_text:
                    removal_value *= 0.7
                    
                # Check targets
                if 'creature' in card_text:
                    if 'nonblack' in card_text or 'nonartifact' in card_text:
                        removal_value *= 0.8  # Restricted by color/type
                    elif 'tapped' in card_text or 'attacking' in card_text:
                        removal_value *= 0.9  # Restricted by state
                elif 'enchantment' in card_text or 'artifact' in card_text:
                    removal_value *= 0.8  # Not creature removal
                elif 'planeswalker' in card_text:
                    removal_value *= 1.1  # Premium for planeswalker removal
                
            # Check exile (better than destroy)
            elif 'exile target' in card_text:
                removal_value = 0.9
                
                # Similar conditions as destroy
                if 'if' in card_text or 'only if' in card_text or 'unless' in card_text:
                    removal_value *= 0.7
                    
                if 'creature' in card_text:
                    if 'nonblack' in card_text or 'nonartifact' in card_text:
                        removal_value *= 0.8
                elif 'enchantment' in card_text or 'artifact' in card_text:
                    removal_value *= 0.8
                elif 'planeswalker' in card_text:
                    removal_value *= 1.1
            
            # Damage-based removal
            elif re.search(
                    r"\bdeals\s+\d+\s+damage\s+to\s+"
                    r"(?:(?:any|up to one)\s+)?target\b", card_text):
                damage_match = re.search(r'deals (\d+) damage', card_text)
                if damage_match:
                    damage = int(damage_match.group(1))
                    removal_value = min(0.8, damage * 0.2)  # Value scales with damage
                    
                    if ('any target' in card_text
                            or ('creature' in card_text and 'player' in card_text)):
                        removal_value *= 1.2  # Flexible targeting is better
            
            # Return to hand (temporary removal)
            elif 'return target' in card_text and 'to its owner\'s hand' in card_text:
                removal_value = 0.5  # Temporary removal worth less
            
            # Add removal value
            value += removal_value
            
            # Board wipes (mass removal)
            if any(term in card_text for term in ['destroy all', 'all creatures get -', 'deal damage to all']):
                board_wipe_value = 1.0
                
                # Conditional board wipes
                if 'nonblack' in card_text or 'non-artifact' in card_text:
                    board_wipe_value *= 0.7
                    
                # One-sided board wipes are premium
                if 'you control' in card_text and 'doesn\'t' in card_text:
                    board_wipe_value *= 1.5
                    
                value += board_wipe_value
            
            # Counterspells
            if 'counter target spell' in card_text:
                counter_value = 0.8
                
                # Conditional counters
                if 'unless' in card_text:
                    counter_value *= 0.7
                    
                # Limited target counters
                if 'creature' in card_text or 'noncreature' in card_text:
                    counter_value *= 0.8
                    
                value += counter_value
            
            # Tutors (card search)
            if 'search your library for' in card_text:
                tutor_value = 0.8
                
                # Restricted tutors
                if 'basic land' in card_text:
                    tutor_value = 0.6  # Land tutors are good but more limited
                elif 'creature' in card_text or 'instant' in card_text or 'sorcery' in card_text:
                    tutor_value *= 0.8  # Type-restricted tutors
                    
                # Does it put directly into hand or battlefield?
                if 'put it onto the battlefield' in card_text:
                    tutor_value *= 1.4  # Premium for cheating mana cost
                elif 'put it into your hand' in card_text:
                    tutor_value *= 1.1  # Good but doesn't cheat mana
                    
                value += tutor_value
                
            # Protection effects
            if 'hexproof' in card_text or 'indestructible' in card_text:
                if 'gains' in card_text or 'gain' in card_text:
                    value += 0.6  # Giving protection to other permanents
                else:
                    value += 0.4  # Having protection itself
                    
            # Mana acceleration/ramp
            if ('add' in card_text and any(f"{{{c}}}" in card_text for c in ['w', 'u', 'b', 'r', 'g', 'c'])):
                # Check if it's a mana ability (not a land)
                if 'land' not in normalized_types:
                    ramp_value = 0.5
                    
                    # Multiple mana is better
                    if any(f"{{{c}}}{{{c}}}" in card_text for c in ['w', 'u', 'b', 'r', 'g', 'c']):
                        ramp_value += 0.2
                        
                    value += ramp_value
        
        return _clamp(value, 0.0, 100.0, 0.0)

    def _evaluate_for_play(self, card_id: int) -> float:
        """Evaluate the immediate value of playing or casting a card.

        The base evaluator already accounts for mana value, card type, stats,
        keywords, card advantage, interaction, and ramp.  Reusing that score
        here gives the play context a stable, card-specific signal without
        introducing mutable board state into the evaluator's cache.
        """
        card = self.game_state._safe_get_card(card_id)
        if not card:
            return 0.0
        return self._get_cached_base_value(card_id, card)
    
    def _evaluate_for_attack(self, card_id: int, perspective=None) -> float:
        """Evaluate a card for attacking with it."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = perspective or (gs.p1 if gs.agent_is_p1 else gs.p2)
        opp = gs.p2 if me is gs.p1 else gs.p1
        
        # Initialize value
        value = 0.0
        
        # Only creatures can attack
        if not self._is_type(card, 'creature'):
            return -5.0  # Strong negative to prevent non-creatures from attacking
        if card_id not in me.get("battlefield", ()):
            return -5.0

        card_power = _finite_number(getattr(card, 'power', 0))
        card_toughness = _finite_number(getattr(card, 'toughness', 0))
        
        # Can't attack if tapped or has summoning sickness
        if (card_id in me.get("tapped_permanents", ())
                or card_id in getattr(gs, "phased_out", ())
                or (card_id in me.get("entered_battlefield_this_turn", ())
                    and not self._has_keyword(card_id, card, "haste"))):
            return -5.0
        
        # Basic attack value based on power
        value += card_power * 0.5
        
        # Factor: Opponent's blockers
        potential_blockers = []
        for blocker_id in opp.get("battlefield", ()):
            blocker = gs._safe_get_card(blocker_id)
            if (not blocker or not self._is_type(blocker, "creature")
                    or blocker_id in opp.get("tapped_permanents", ())
                    or blocker_id in getattr(gs, "phased_out", ())):
                continue
            if self._can_block(blocker_id, card_id):
                potential_blockers.append(blocker_id)

        if potential_blockers:
            # Check for unfavorable blocks
            unfavorable_blocks = 0
            for blocker_id in potential_blockers:
                blocker = gs._safe_get_card(blocker_id)
                if not blocker or not hasattr(blocker, 'power') or not hasattr(blocker, 'toughness'):
                    continue
                    
                # Blocker can kill attacker without dying
                blocker_power = _finite_number(blocker.power)
                blocker_toughness = _finite_number(blocker.toughness)
                if (blocker_power >= card_toughness
                        and blocker_toughness > card_power):
                    unfavorable_blocks += 1
            
            # Severe penalty for unfavorable blocks
            if unfavorable_blocks > 0:
                value -= 0.7 * unfavorable_blocks
                
            # Check for favorable blocks
            favorable_blocks = 0
            for blocker_id in potential_blockers:
                blocker = gs._safe_get_card(blocker_id)
                if not blocker or not hasattr(blocker, 'power') or not hasattr(blocker, 'toughness'):
                    continue
                    
                # Attacker can kill blocker without dying
                blocker_power = _finite_number(blocker.power)
                blocker_toughness = _finite_number(blocker.toughness)
                if (card_power >= blocker_toughness
                        and card_toughness > blocker_power):
                    favorable_blocks += 1
            
            # Bonus for favorable blocks
            if favorable_blocks > 0:
                value += 0.5 * favorable_blocks
        else:
            # No blockers is great!
            value += 1.0
        
        # Factor: Special combat abilities
        if getattr(card, 'oracle_text', None):
            card_text = str(card.oracle_text).lower()
            
            # Deathtouch is great for attacking
            if "deathtouch" in card_text:
                value += 0.4
                
            # First strike / Double strike
            if "first strike" in card_text or "double strike" in card_text:
                value += 0.3
                
            # Trample helps push damage through
            if "trample" in card_text:
                value += 0.3
        
        # Factor: Life totals
        if opp["life"] <= card_power:
            # Could be lethal!
            value += 2.0
        elif opp["life"] <= 5:
            # Opponent at low life - attacking is good
            value += 0.7
            
        # Factor: Strategic considerations
        if self._player_turn_number(me, gs.turn) <= 3:
            # Early game - be more aggressive
            value += 0.2
        
        return value
    
    def _evaluate_for_block(self, card_id: int, perspective=None) -> float:
        """Evaluate a card for blocking with it."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = perspective or (gs.p1 if gs.agent_is_p1 else gs.p2)
        opp = gs.p2 if me is gs.p1 else gs.p1
        
        # Initialize value
        value = 0.0
        
        # Only creatures can block
        if not self._is_type(card, 'creature'):
            return -5.0  # Strong negative to prevent non-creatures from blocking
        if card_id not in me.get("battlefield", ()):
            return -5.0

        card_power = _finite_number(getattr(card, 'power', 0))
        card_toughness = _finite_number(getattr(card, 'toughness', 0))
        
        # Can't block if tapped
        if (card_id in me.get("tapped_permanents", ())
                or card_id in getattr(gs, "phased_out", ())):
            return -5.0
            
        # Defender is an asset when blocking.
        if "defender" in str(getattr(card, 'oracle_text', '') or '').lower():
            value += 0.5  # Defender is actually good for blocking
        
        # Basic block value based on toughness
        value += card_toughness * 0.4
        
        # No reason to evaluate further if no attackers
        if not gs.current_attackers:
            return -5.0
        
        # Factor: Creature matchups
        best_block_value = -float('inf')
        legal_attacker_found = False
        for attacker_id in gs.current_attackers:
            attacker = gs._safe_get_card(attacker_id)
            if not attacker or not hasattr(attacker, 'power') or not hasattr(attacker, 'toughness'):
                continue
            if not self._can_block(card_id, attacker_id):
                continue
            legal_attacker_found = True
                
            # Calculate block value for this attacker
            block_value = 0.0
            
            # Check if blocker can survive
            attacker_power = _finite_number(attacker.power)
            attacker_toughness = _finite_number(attacker.toughness)
            blocker_survives = card_toughness > attacker_power
            
            # Check if blocker can kill attacker
            kills_attacker = card_power >= attacker_toughness
            
            # Determine block quality
            if kills_attacker and blocker_survives:
                # Ideal: kill attacker and survive
                block_value = 2.0
            elif kills_attacker and not blocker_survives:
                # Trade: kill attacker but die
                block_value = 1.0
                
                # Base trade value on mana costs if available
                if hasattr(card, 'cmc') and hasattr(attacker, 'cmc'):
                    attacker_cmc = _finite_number(attacker.cmc)
                    blocker_cmc = _finite_number(card.cmc)
                    if attacker_cmc > blocker_cmc:
                        block_value += 0.5  # Good trade
                    elif attacker_cmc < blocker_cmc:
                        block_value -= 0.3  # Bad trade
            elif not kills_attacker and blocker_survives:
                # Chump with survival: don't kill attacker but survive
                block_value = 0.3
            else:
                # Chump block: die without killing attacker
                block_value = -0.2
                
                # Exception: high power attacker worth chump blocking
                if attacker_power >= 4:
                    block_value = 0.4  # Worth chump blocking a big threat
            
            # Special abilities consideration
            if getattr(card, 'oracle_text', None) or getattr(attacker, 'oracle_text', None):
                card_text = str(getattr(card, 'oracle_text', '') or '').lower()
                attacker_text = str(
                    getattr(attacker, 'oracle_text', '') or '').lower()
                
                # Deathtouch makes any block better
                if "deathtouch" in card_text:
                    block_value += 0.5
                
                # First strike is great for blocking
                if "first strike" in card_text and "first strike" not in attacker_text:
                    block_value += 0.4
                
                # Double strike on attacker is dangerous
                if "double strike" in attacker_text:
                    block_value -= 0.3
            
            # Update best block if this one is better
            if block_value > best_block_value:
                best_block_value = block_value

        if not legal_attacker_found:
            return -5.0
        
        # Add best block value to overall value
        if best_block_value > -float('inf'):
            value += best_block_value
        
        # Factor: Life totals
        life_difference = me["life"] - opp["life"]
        
        if me["life"] <= 5:
            # Critical life - block more aggressively
            value += 0.5
        elif life_difference < -10:
            # Far behind - need to preserve life
            value += 0.3
        elif life_difference > 10:
            # Far ahead - can take more risks
            value -= 0.2
        
        return value
    
    
    
    def _calculate_card_likelihood(self, card, color_count, visible_creatures, visible_instants, visible_artifacts):
        """Helper to calculate how likely a card is to be in opponent's hand"""
        gs = self.game_state
        weight = 1.0
        
        # Card must have required attributes
        if not card or not hasattr(card, 'colors') or not hasattr(card, 'card_types'):
            return 0.0
        
        # Color matching
        card_colors = np.zeros(5, dtype=float)
        observed_colors = np.zeros(5, dtype=float)
        for index, value in enumerate(list(card.colors)[:5]):
            card_colors[index] = _clamp(value, 0.0, 1.0, 0.0)
        for index, value in enumerate(list(color_count)[:5]):
            observed_colors[index] = max(0.0, _finite_number(value))
        color_match = float(np.dot(card_colors, observed_colors)) / max(
            float(np.sum(observed_colors)), 1e-6)
        weight *= (1.0 + color_match)
        
        # Card type matching
        if self._is_type(card, 'creature') and _finite_number(visible_creatures) > 0:
            weight *= 1.5
        if self._is_type(card, 'instant') and _finite_number(visible_instants) > 0:
            weight *= 1.2
        if self._is_type(card, 'artifact') and _finite_number(visible_artifacts) > 0:
            weight *= 1.3
            
        # Mana curve considerations - higher probability of having castable cards
        if hasattr(card, 'cmc'):
            cmc = _clamp(card.cmc, 0.0, 100.0, 0.0)
            turn = _clamp(getattr(gs, 'turn', 0), 0.0, 1000.0, 0.0)
            if cmc <= turn:
                weight *= 2.0
            elif cmc <= turn + 2:
                weight *= 1.0
            else:
                weight *= 0.5
        
        return _clamp(weight, 0.0, 10.0, 0.0)
    
    def _evaluate_for_discard(self, card_id: int, perspective=None) -> float:
        """Return keep value for a discard choice (lowest is best to discard)."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        me = perspective or (gs.p1 if gs.agent_is_p1 else gs.p2)
        
        # Initialize value (higher value = better to KEEP)
        value = 0.0
        
        # Factor: Mana cost relative to game stage
        if hasattr(card, 'cmc'):
            cmc = _clamp(card.cmc, 0.0, 100.0, 0.0)
            # Early game - favor lower cost cards
            player_turn = self._player_turn_number(me, gs.turn)
            if player_turn <= 4:
                if cmc <= 4:
                    value += (4 - cmc) * 0.2  # More value for cheap cards
                else:
                    value -= (cmc - 4) * 0.1  # Penalty for expensive cards
            # Mid game - balance
            elif player_turn <= 8:
                if 3 <= cmc <= 6:
                    value += 0.3  # Prefer mid-range cards
                elif cmc > 6:
                    value -= 0.2  # Still penalize very expensive cards
            # Late game - favor impact cards
            else:
                if cmc >= 5:
                    value += 0.3  # Prefer high-impact cards
                if cmc <= 2:
                    value -= 0.2  # Low-impact cards less valuable late
        
        # Factor: Card type
        if getattr(card, 'card_types', None):
            # Lands decrease in value as game progresses
            if self._is_type(card, 'land'):
                lands_in_play = len([cid for cid in me["battlefield"] 
                                   if self._is_type(gs._safe_get_card(cid), 'land')])
                
                if lands_in_play <= 3:
                    value += 1.0  # Critical to keep early lands
                elif lands_in_play <= 6:
                    value += 0.5  # Still valuable
                else:
                    value += 0.1  # Diminishing returns
            
            # Creatures maintain consistent value
            if self._is_type(card, 'creature'):
                value += 0.4
                
                # Higher value for powerful creatures
                if hasattr(card, 'power') and hasattr(card, 'toughness'):
                    if (_finite_number(card.power)
                            + _finite_number(card.toughness) >= 7):
                        value += 0.3
            
            # Instants have high value (flexible)
            if self._is_type(card, 'instant'):
                value += 0.5
        
        # Factor: Card advantage and removal maintain high value
        if getattr(card, 'oracle_text', None):
            card_text = str(card.oracle_text).lower()
            
            if 'draw' in card_text and 'card' in card_text:
                value += 0.5
                
            if any(term in card_text for term in ['destroy', 'exile', 'sacrifice', 'return to hand']):
                value += 0.4
        
        # Factor: Synergy with board
        synergy_value = self._calculate_synergy_value(card_id, me["battlefield"])
        value += synergy_value
        
        return _finite_number(value, 0.0)
    
    # Update in enhanced_card_evaluator.py
    def _calculate_synergy_value(self, card_id: int, board: List[int]) -> float:
        """Calculate how well a card synergizes with existing board."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return 0.0
        
        # Include live characteristics so transform/layer changes cannot reuse
        # a synergy score from an earlier state with the same permanent IDs.
        board_entries = []
        for board_id in board:
            board_card = gs._safe_get_card(board_id)
            board_entries.append((
                type(board_id).__name__, repr(board_id),
                self._card_signature(board_card) if board_card else None))
        board_key = tuple(sorted(board_entries, key=lambda item: item[:2]))
        cache_key = (card_id, self._card_signature(card), board_key)
        if cache_key in self._board_synergy_cache:
            return self._board_synergy_cache[cache_key]
        
        synergy_value = 0.0
        
        try:
            # Creature type synergy
            creature_types = set()
            for board_id in board:
                board_card = gs._safe_get_card(board_id)
                if board_card and getattr(board_card, 'subtypes', None):
                    creature_types.update(
                        str(value) for value in board_card.subtypes)
            
            if getattr(card, 'subtypes', None):
                shared_types = {
                    str(value) for value in card.subtypes}.intersection(
                        creature_types)
                synergy_value += len(shared_types) * 0.1
            
            # Ability synergy
            if getattr(card, 'oracle_text', None):
                card_text = str(card.oracle_text).lower()
                
                # Check for tribal synergies
                for creature_type in creature_types:
                    if creature_type.lower() in card_text:
                        synergy_value += 0.3
                        break
                
                # Check for keyword synergies
                synergy_keywords = {
                    '+1/+1 counter': ['counter', '+1/+1'],
                    'sacrifice': ['sacrifice'],
                    'discard': ['discard'],
                    'graveyard': ['graveyard', 'from your graveyard'],
                    'enchantment': ['enchantment'],
                    'artifact': ['artifact'],
                    'lifegain': ['gain life', 'life link']
                }
                
                # Count cards with each synergy on board
                synergy_counts = {k: 0 for k in synergy_keywords}
                
                for board_id in board:
                    board_card = gs._safe_get_card(board_id)
                    if not board_card or not getattr(board_card, 'oracle_text', None):
                        continue
                        
                    board_text = str(board_card.oracle_text).lower()
                    
                    for synergy_type, keywords in synergy_keywords.items():
                        if any(kw in board_text for kw in keywords):
                            synergy_counts[synergy_type] += 1
                
                # Check if this card matches any synergies
                for synergy_type, keywords in synergy_keywords.items():
                    if any(kw in card_text for kw in keywords):
                        # Value scales with number of synergistic cards
                        synergy_value += min(synergy_counts[synergy_type] * 0.15, 0.45)
            
            # Color synergy
            if getattr(card, 'colors', None) is not None:
                color_counts = np.zeros(5)
                
                for board_id in board:
                    board_card = gs._safe_get_card(board_id)
                    if board_card and getattr(board_card, 'colors', None) is not None:
                        for i, color in enumerate(board_card.colors[:5]):
                            color_counts[i] += _finite_number(color)
                
                # Calculate color synergy
                card_colors = [
                    _finite_number(value) for value in list(card.colors)[:5]]
                color_match = sum(
                    bool(a) and bool(b)
                    for a, b in zip(card_colors, color_counts > 0))
                colors_in_card = sum(bool(value) for value in card_colors)
                
                if colors_in_card > 0:
                    color_synergy = color_match / colors_in_card
                    synergy_value += color_synergy * 0.1
        except Exception as e:
            logging.error(f"Error calculating synergy for card {card_id}: {str(e)}")
            # Return 0 synergy on error
            return 0.0
        
        # Cache the result
        if len(self._board_synergy_cache) >= 1000:
            self._board_synergy_cache.clear()
        self._board_synergy_cache[cache_key] = _finite_number(
            synergy_value, 0.0)
        
        return _finite_number(synergy_value, 0.0)
    
    def _get_stats_value(
            self, card_id: int, *, include_evidence: bool = False):
        """Get value based on statistical performance."""
        evidence = ({"games_played": 0.0, "wins": 0.0, "draws": 0.0}
                    if include_evidence else None)
        if not self.stats_tracker:
            return (0.0, evidence) if include_evidence else 0.0
        
        # Get card stats
        card_stats = self.stats_tracker.get_card_stats(
            self._analytics_card_id(card_id))
        if not card_stats:
            return (0.0, evidence) if include_evidence else 0.0
        
        # Calculate win rate
        games_played = _clamp(
            card_stats.get("games_played", 0), 0.0, 1e12, 0.0)
        if include_evidence:
            evidence["games_played"] = games_played
        if games_played < 5:  # Need enough data
            return (0.0, evidence) if include_evidence else 0.0
            
        wins = _clamp(card_stats.get("wins", 0), 0.0, games_played, 0.0)
        draws = _clamp(
            card_stats.get("draws", 0), 0.0, games_played - wins, 0.0)
        if include_evidence:
            evidence.update({"wins": wins, "draws": draws})
        win_rate = (
            wins + 0.5 * draws
        ) / games_played if games_played > 0 else 0.0
        
        # Convert win rate to value (centered around 0.5 win rate)
        stats_value = (win_rate - 0.5) * 1.5
        
        result = _clamp(stats_value, -0.75, 0.75, 0.0)
        return (result, evidence) if include_evidence else result
    
    def get_card_rankings(self, card_ids: List[int], context: str = "general") -> List[Tuple[int, float]]:
        """
        Rank a list of cards based on their evaluation scores.
        
        Args:
            card_ids: List of card IDs to rank
            context: The context for evaluation
            
        Returns:
            List[Tuple[int, float]]: List of (card_id, score) pairs sorted by score
        """
        # Evaluate each card
        rankings = [(card_id, self.evaluate_card(card_id, context)) for card_id in card_ids]
        
        # Sort by score (descending)
        rankings.sort(key=lambda x: x[1], reverse=True)
        
        return rankings
    
    def evaluate_deck(self, deck: List[int]) -> Dict[str, Any]:
        """
        Evaluate the overall quality of a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dict: Evaluation results
        """
        gs = self.game_state
        
        # Basic stats
        card_count = len(deck)
        
        # Count card types
        type_counts = {
            'creature': 0,
            'instant': 0,
            'sorcery': 0,
            'artifact': 0,
            'enchantment': 0,
            'planeswalker': 0,
            'land': 0
        }
        
        # Count mana curve
        mana_curve = {
            '0': 0,
            '1': 0,
            '2': 0,
            '3': 0,
            '4': 0,
            '5': 0,
            '6+': 0
        }
        
        # Count colors
        color_counts = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        
        # Track card strengths
        card_strengths = []
        valid_card_count = 0
        nonland_count = 0
        
        for card_id in deck:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            valid_card_count += 1

            # Count card types
            raw_types = getattr(card, 'card_types', ()) or ()
            if isinstance(raw_types, str):
                raw_types = (raw_types,)
            normalized_types = {
                str(card_type).lower() for card_type in raw_types}
            for card_type in normalized_types:
                if card_type in type_counts:
                    type_counts[card_type] += 1
            is_land = 'land' in normalized_types
            if not is_land:
                nonland_count += 1
            
            # Lands do not belong in the spell mana curve.
            if not is_land:
                cmc = _clamp(getattr(card, 'cmc', 0), 0.0, 100.0, 0.0)
                bucket = str(int(cmc)) if cmc < 6 else '6+'
                mana_curve[bucket] += 1
            
            # Count colors
            if getattr(card, 'colors', None) is not None:
                colors = list(card.colors)[:5]
                for i, color in enumerate(['W', 'U', 'B', 'R', 'G']):
                    if i < len(colors) and bool(_finite_number(colors[i])):
                        color_counts[color] += 1
            
            # Evaluate card strength
            card_strength = self._calculate_base_value(card)
            card_strengths.append((card_id, card_strength))
        
        # Sort cards by strength
        card_strengths.sort(key=lambda x: x[1], reverse=True)
        
        # Calculate statistical metrics if available
        stats_metrics = {}
        if self.stats_tracker:
            analytics_deck = [
                ({**entry, "id": self._analytics_card_id(entry.get("id"))}
                 if isinstance(entry, dict) and "id" in entry
                 else self._analytics_card_id(entry))
                for entry in deck
            ]
            deck_key = analytics_deck
            fingerprint = getattr(self.stats_tracker, 'get_deck_fingerprint', None)
            if callable(fingerprint):
                deck_key = fingerprint(analytics_deck)
            deck_stats = self.stats_tracker.get_deck_stats(deck_key)
            if deck_stats:
                games_played = _clamp(
                    deck_stats.get("games", 0), 0.0, 1e12, 0.0)
                wins = _clamp(
                    deck_stats.get("wins", 0), 0.0, games_played, 0.0)
                draws = _clamp(
                    deck_stats.get("draws", 0), 0.0,
                    games_played - wins, 0.0)
                win_rate = (
                    wins + 0.5 * draws
                ) / games_played if games_played > 0 else 0.0
                
                stats_metrics = {
                    "games_played": games_played,
                    "wins": wins,
                    "draws": draws,
                    "losses": _clamp(
                        deck_stats.get("losses", 0), 0.0, games_played, 0.0),
                    "win_rate": win_rate,
                    "avg_game_length": _clamp(
                        deck_stats.get("avg_game_length", 0), 0.0, 1e6, 0.0)
                }
        
        # Calculate overall deck metrics
        deck_metrics = {
            "card_count": card_count,
            "valid_card_count": valid_card_count,
            "type_counts": type_counts,
            "mana_curve": mana_curve,
            "color_counts": color_counts,
            "avg_card_strength": sum(s for _, s in card_strengths) / len(card_strengths) if card_strengths else 0,
            "top_cards": [(cid, score) for cid, score in card_strengths[:10]]
        }
        
        # Calculate deck balance score
        balance_score = self._calculate_deck_balance(
            type_counts, mana_curve, color_counts,
            card_count=valid_card_count, nonland_count=nonland_count)
        
        # Compile evaluation results
        evaluation = {
            "deck_metrics": deck_metrics,
            "stats_metrics": stats_metrics,
            "balance_score": balance_score,
            "overall_rating": self._calculate_overall_rating(deck_metrics, stats_metrics, balance_score)
        }
        
        return evaluation
    
    def _calculate_deck_balance(self, type_counts, mana_curve, color_counts,
                                card_count=None, nonland_count=None):
        """Calculate how well-balanced a deck is."""
        if nonland_count is None:
            nonland_count = sum(
                max(0.0, _finite_number(value))
                for value in mana_curve.values())
        if card_count is None:
            card_count = max(
                0.0, _finite_number(type_counts.get('land', 0))) + nonland_count
        card_count = max(0.0, _finite_number(card_count))
        nonland_count = max(0.0, _finite_number(nonland_count))
        if card_count <= 0:
            return 0.0

        balance_score = 0.0
        
        # Scale composition targets to deck size (24/60 lands, 22/60 creatures).
        land_count = max(0.0, _finite_number(type_counts.get('land', 0)))
        land_ratio = land_count / card_count
        land_score = _clamp(
            1.0 - abs(land_ratio - 0.40) / 0.25, 0.0, 1.0)
        balance_score += land_score * 0.3
        
        creature_count = max(
            0.0, _finite_number(type_counts.get('creature', 0)))
        creature_ratio = creature_count / card_count
        creature_score = _clamp(
            1.0 - abs(creature_ratio - (22.0 / 60.0)) / 0.25,
            0.0, 1.0)
        balance_score += creature_score * 0.2
        
        # Check mana curve (should be bell-shaped centered on 2-3 CMC)
        curve_score = 0.0
        ideal_curve = {'0': 0.05, '1': 0.15, '2': 0.25, '3': 0.25, '4': 0.15, '5': 0.1, '6+': 0.05}
        
        if nonland_count > 0:
            total_distance = sum(
                abs(max(0.0, _finite_number(mana_curve.get(cmc, 0)))
                    / nonland_count - ideal_pct)
                for cmc, ideal_pct in ideal_curve.items())
            curve_score = _clamp(
                1.0 - total_distance / 2.0, 0.0, 1.0)
        balance_score += curve_score * 0.3
        
        # Check color balance
        color_balance = 0.0
        used_colors = sum(1 for c, count in color_counts.items() if count > 0)
        
        positive_counts = [
            max(0.0, _finite_number(count))
            for count in color_counts.values() if _finite_number(count) > 0]
        if used_colors == 0:
            color_balance = 1.0  # A colorless deck has no color mismatch.
        elif used_colors == 1:
            color_balance = 1.0
        elif used_colors == 2:
            color_balance = min(positive_counts) / max(positive_counts)
        else:
            total_colors = sum(positive_counts)
            probabilities = [count / total_colors for count in positive_counts]
            entropy = -sum(
                probability * math.log(probability)
                for probability in probabilities if probability > 0)
            color_balance = entropy / math.log(used_colors)
        
        balance_score += color_balance * 0.2

        return _clamp(balance_score, 0.0, 1.0, 0.0)
    
    def _calculate_overall_rating(self, deck_metrics, stats_metrics, balance_score):
        """Calculate an overall deck rating."""
        # Base rating from card strength
        avg_card_strength = max(
            0.0, _finite_number(deck_metrics.get("avg_card_strength", 0)))
        card_rating = 1.0 - math.exp(-avg_card_strength / 5.0)
        
        # Factor in balance score
        balance_rating = _clamp(balance_score, 0.0, 1.0, 0.0)
        
        # Factor in statistics if available
        stats_rating = 0.0
        confidence = 0.0
        games_played = _clamp(
            stats_metrics.get("games_played", 0), 0.0, 1e12, 0.0)
        has_stats = "win_rate" in stats_metrics and games_played > 0
        if has_stats:
            win_rate = _clamp(
                stats_metrics["win_rate"], 0.0, 1.0, 0.0)

            # Weight by confidence (more games = more confidence)
            confidence = min(games_played / 50, 1.0)
            stats_rating = win_rate

        # Blend toward the stats-aware formula as confidence grows. Merely
        # adding one tracked game must not discard the prior rating weights.
        prior_rating = 0.6 * card_rating + 0.4 * balance_rating
        if has_stats:
            stats_aware_rating = (
                0.4 * card_rating + 0.3 * balance_rating
                + 0.3 * stats_rating)
            overall_rating = (
                prior_rating * (1.0 - confidence)
                + stats_aware_rating * confidence)
        else:
            overall_rating = prior_rating
        
        # Scale to 0-10 range
        scaled_rating = _clamp(overall_rating * 10, 0.0, 10.0, 0.0)
        
        return round(scaled_rating, 1)
