"""Frozen policy-observation contract.

The card feature schema describes the columns inside a card vector.  This
module separately versions the Gym Dict that arranges those vectors and the
other public game-state fields consumed by a policy.  Its identity belongs in
run lineage because either schema can invalidate a checkpoint.
"""

from __future__ import annotations

import hashlib
import json

from .archetypes import (
    CLASSIFIER_VERSION,
    PRIMARY_ARCHETYPES,
    PROFILE_VECTOR_SIZE,
    STRATEGY_AXES,
    STRATEGY_TAGS,
    TAXONOMY_VERSION,
    classifier_identity,
    taxonomy_identity,
)


OBSERVATION_SCHEMA_KIND = "playersim_policy_observation"
OBSERVATION_SCHEMA_VERSION = 6
SEMANTIC_IDENTITY_VOCAB_SIZE = 65_536
SEMANTIC_IDENTITY_MAX = SEMANTIC_IDENTITY_VOCAB_SIZE - 1

# The observer's own decklist is public information to that observer, so its
# starting-deck identities and remaining-library composition are legitimate
# observations.  The full list is exposed as an order-free multiset (the
# cards you own), never the live library order (your hidden draw order), and
# only for the observing player -- the opponent's decklist stays concealed.
MAX_DECK_OBSERVATION_SIZE = 60

# These are stable canonical registry indices encoded as:
#   0 = padded slot, 1 = visible object with unknown/unsupported identity,
#   canonical registry index N = N + 2.
# The extractor routes every field through a learned categorical embedding;
# they must never be treated as ordinal continuous values.
SEMANTIC_IDENTITY_FIELDS = (
    "my_hand_card_identity",
    "my_battlefield_card_identity",
    "opp_battlefield_card_identity",
    "my_graveyard_card_identity",
    "opp_graveyard_card_identity",
    "my_exile_card_identity",
    "opp_exile_card_identity",
    "stack_card_identity",
    "target_card_identity",
    "choice_card_identity",
    # v4: the observer's own full starting decklist (order-free multiset).
    "my_deck_card_identity",
)

# The action mask is consumed by MaskablePPO rather than the feature extractor.
# Runtime target IDs prove selection-page protocol continuity but are unstable
# per-game object handles and therefore never enter the network.
EXTERNAL_POLICY_FIELDS = ("action_mask", "target_card_ids")

REMOVED_V1_FIELDS = (
    "phase_onehot",              # exact duplicate of the phase embedding
    "p1_life", "p2_life",      # absolute-seat duplicates of my/opp life
    "p1_battlefield", "p2_battlefield",
    "p1_bf_count", "p2_bf_count",
    "hand_performance",         # initialized to the same 0.5 for every card
    "my_battlefield_keywords",  # already present in card feature vectors
    "my_tapped_permanents",     # already my_battlefield_flags[:, 0]
    "my_mana",                  # sum(my_mana_pool)
    "remaining_mana_sources",   # exact duplicate of untapped_land_count
    "graveyard_key_cards", "exile_key_cards",
    "memory_suggested_action",  # online per-env heuristic, not game state
    "suggestion_matches_recommendation",
    "recommended_action",       # disabled constant; stochastic if enabled
    "recommended_action_confidence",
    "estimated_opponent_hand",  # fake exact cards ranked from hidden runtime DB
)

ADDED_V2_FIELDS = (
    *SEMANTIC_IDENTITY_FIELDS,
    "my_battlefield_count", "opp_battlefield_count",
    "my_library_count", "opp_library_count",
    "my_exile_count", "opp_exile_count",
    "my_player_counters", "opp_player_counters",
    "my_player_status", "opp_player_status",
    "opp_mana_pool", "my_snow_mana_pool", "opp_snow_mana_pool",
    "my_restricted_mana_pool", "opp_restricted_mana_pool",
    "my_graveyard_cards", "opp_graveyard_cards",
    "my_exile_cards", "opp_exile_cards",
    "my_exile_card_visibility", "opp_exile_card_visibility",
    "my_permanent_counters", "opp_permanent_counters",
    "my_damage_marked", "opp_damage_marked",
    "my_attachment_targets", "opp_attachment_targets",
    "my_attachment_counts", "opp_attachment_counts",
    "stack_cards", "stack_object_kinds", "stack_target_counts",
    "stack_mode_counts", "combat_attack_targets",
    "combat_blocker_assignments",
)

REMOVED_V3_FIELDS = (
    "resource_efficiency",  # duplicated/fabricated heuristic summary
)

# v4 exposes the observer's own decklist, which the policy previously never
# saw (library was count-only and deck_composition_estimate summarized only
# already-revealed cards).  Control decks are unplayable without it, and the
# mulligan decision is uninformed without knowing your own land/spell counts.
ADDED_V4_FIELDS = (
    # Full starting decklist as canonical identities (observer-own multiset).
    "my_deck_card_identity",
    # Remaining-library composition: type counts, mana-curve buckets, color
    # availability, and the remaining count -- the live "what's left to draw"
    # signal for draw planning and keep/mulligan decisions.
    "my_library_composition",
)

CORRECTED_V4_SEMANTICS = (
    # deck_composition_estimate now summarizes the observer's full 60-card
    # starting deck, not only the cards already revealed this game.
    "deck_composition_estimate_uses_full_own_decklist",
    # All decklist-derived features are observer-own only; the opponent's
    # decklist and library composition are never exposed.
    "decklist_features_are_observer_own_only",
    # The decklist is an order-free multiset (cards owned), never the live
    # library order (the observer's own draw order remains hidden).
    "deck_card_identities_are_order_free_multiset",
)

# v5 exposes producible mana by color.  The policy previously saw each card's
# cost broken out by color but only a single colorless total_available_mana
# scalar for what it could produce -- a color-blind view of a colored game
# that particularly handicaps reactive decks deciding whether to hold up a
# colored answer.
ADDED_V5_FIELDS = (
    # Per-color (WUBRG) mana the observer can produce now: untapped
    # mana-source color access plus floating mana. Observer-own is exact.
    "my_producible_mana",
    # The opponent's producible mana by color from its visible untapped
    # sources -- public information (lands are face-up), so this is a legal
    # observation, an estimate of what colored responses they can pay for.
    "opp_producible_mana",
)

CORRECTED_V5_SEMANTICS = (
    # A dual/any-color source counts toward each color it can produce (color
    # access), not simultaneous availability; floating mana adds on top.
    "producible_mana_is_per_color_source_access_plus_floating",
    # Only visible untapped mana sources contribute, so hidden information is
    # never leaked and tapped sources are excluded.
    "producible_mana_uses_visible_untapped_sources_only",
)

# v6 gives the policy an explicit, centralized encoding of the observing
# player's exact full-deck strategy profile.  The field is deliberately named
# ``my_...`` and selected only after the observer-relative perspective is set:
# there is no corresponding opponent-exact field.  Public opponent inference
# remains the independent six-value ``opponent_archetype`` observation.
EXACT_OWN_STRATEGY_PROFILE_FIELD = "my_exact_deck_strategy_profile"
EXACT_OWN_STRATEGY_PROFILE_SIZE = PROFILE_VECTOR_SIZE
EXACT_OWN_STRATEGY_PROFILE_ORDER = (
    *(f"primary_one_hot:{name}" for name in PRIMARY_ARCHETYPES),
    *(f"secondary_one_hot:{name}" for name in PRIMARY_ARCHETYPES),
    *(f"tag:{name}" for name in STRATEGY_TAGS),
    *(f"axis:{name}" for name in STRATEGY_AXES),
    "confidence",
)
if len(EXACT_OWN_STRATEGY_PROFILE_ORDER) != EXACT_OWN_STRATEGY_PROFILE_SIZE:
    raise RuntimeError("exact-own strategy profile schema width drifted")

ADDED_V6_FIELDS = (EXACT_OWN_STRATEGY_PROFILE_FIELD,)

CORRECTED_V6_SEMANTICS = (
    "exact_strategy_profile_is_observer_own_only",
    "opponent_exact_deck_profile_is_never_observed",
    "opponent_archetype_remains_public_inference_only",
    "strategy_profile_uses_reviewed_or_deterministic_inferred_contract",
)

CORRECTED_V3_SEMANTICS = (
    "snow_mana_pool_includes_restricted_snow_provenance",
    "total_available_mana_excludes_snow_provenance_duplicates",
    "combat_advice_only_during_live_attack_declaration",
    "future_state_projections_are_observer_antisymmetric",
    "multi_turn_plan_respects_live_land_drop_allowance",
    "multi_turn_plan_uses_live_spendable_mana",
    "opponent_threats_use_opponent_win_conditions",
    "specialized_archetype_profiles_are_preserved",
    "strategic_resource_advantages_preserve_magnitude",
    "win_condition_viability_excludes_nonviable_paths",
    "turn_vs_mana_uses_observer_turns_received",
    "default_evaluator_advice_excludes_adaptive_history",
    "adaptive_history_uses_player_relative_turns",
    "targetable_vectors_match_active_target_instruction",
)


def _schema_payload() -> dict:
    return {
        "kind": OBSERVATION_SCHEMA_KIND,
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "perspective": "observer-relative",
        "semantic_identity_encoding": {
            "padding": 0,
            "unknown_visible": 1,
            "canonical_offset": 2,
            "maximum_encoded_identity": SEMANTIC_IDENTITY_MAX,
            "extractor": "shared_categorical_embedding",
        },
        "semantic_identity_fields": list(SEMANTIC_IDENTITY_FIELDS),
        "external_policy_fields": list(EXTERNAL_POLICY_FIELDS),
        "strategy_memory": {
            "policy_observation": "excluded",
            "default_runtime_mode": "disabled",
            "optional_storage_schema": 2,
            "selection": "deterministic_action_evidence",
        },
        "planner_observation": {
            "observer_information_only": True,
            "observation_rng_consumption": "forbidden",
            "future_draw_projection": "expected_value",
            "wide_attack_candidates": "deterministic_bounded_combinations",
            "strategic_metric_count": 7,
        },
        "removed_v1_fields": list(REMOVED_V1_FIELDS),
        "added_v2_fields": list(ADDED_V2_FIELDS),
        "removed_v3_fields": list(REMOVED_V3_FIELDS),
        "corrected_v3_semantics": list(CORRECTED_V3_SEMANTICS),
        "added_v4_fields": list(ADDED_V4_FIELDS),
        "corrected_v4_semantics": list(CORRECTED_V4_SEMANTICS),
        "max_deck_observation_size": MAX_DECK_OBSERVATION_SIZE,
        "added_v5_fields": list(ADDED_V5_FIELDS),
        "corrected_v5_semantics": list(CORRECTED_V5_SEMANTICS),
        "added_v6_fields": list(ADDED_V6_FIELDS),
        "corrected_v6_semantics": list(CORRECTED_V6_SEMANTICS),
        "exact_own_strategy_profile": {
            "field": EXACT_OWN_STRATEGY_PROFILE_FIELD,
            "dtype": "float32",
            "shape": [EXACT_OWN_STRATEGY_PROFILE_SIZE],
            "bounds": [0.0, 1.0],
            "component_order": list(EXACT_OWN_STRATEGY_PROFILE_ORDER),
            "component_encoding": {
                "primary": "closed_vocabulary_one_hot",
                "secondary": "closed_vocabulary_one_hot_or_all_zero",
                "tags": "closed_vocabulary_multi_hot",
                "axes": "integer_0_to_100_divided_by_100",
                "confidence":
                    "basis_points_0_to_10000_divided_by_10000",
            },
            "taxonomy_version": TAXONOMY_VERSION,
            "taxonomy_sha256": taxonomy_identity()["sha256"],
            "classifier_version": CLASSIFIER_VERSION,
            "classifier_sha256": classifier_identity()["sha256"],
            "own_profile_source": "reviewed_or_deterministic_full_deck_inference",
            "opponent_profile_source": "public_inference_only",
        },
    }


def observation_schema_identity() -> dict:
    """Return the stable identity recorded in training/Harvest lineage."""
    payload = _schema_payload()
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return {
        "kind": OBSERVATION_SCHEMA_KIND,
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


OBSERVATION_SCHEMA_SHA256 = observation_schema_identity()["sha256"]
