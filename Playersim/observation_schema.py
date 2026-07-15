"""Frozen policy-observation contract.

The card feature schema describes the columns inside a card vector.  This
module separately versions the Gym Dict that arranges those vectors and the
other public game-state fields consumed by a policy.  Its identity belongs in
run lineage because either schema can invalidate a checkpoint.
"""

from __future__ import annotations

import hashlib
import json


OBSERVATION_SCHEMA_KIND = "playersim_policy_observation"
OBSERVATION_SCHEMA_VERSION = 3
SEMANTIC_IDENTITY_VOCAB_SIZE = 65_536
SEMANTIC_IDENTITY_MAX = SEMANTIC_IDENTITY_VOCAB_SIZE - 1

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
