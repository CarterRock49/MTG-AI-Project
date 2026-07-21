
import os
import copy
import json
import random
import logging
import re
import math
import hashlib
import tempfile
import threading
import uuid
import weakref
from contextlib import contextmanager
import numpy as np
import gymnasium as gym
from gymnasium import spaces
from .card import Card
from .game_state import GameState
from .actions import ActionHandler
from .combat_integration import integrate_combat_actions
from .strategic_planner import MTGStrategicPlanner
# Ensure DEBUG_MODE exists or default it
try:
    from .debug import DEBUG_MODE, DEBUG_ACTION_STEPS
except ImportError:
    DEBUG_MODE = False
    DEBUG_ACTION_STEPS = False
from .strategy_memory import StrategyMemory
from collections import defaultdict
from .deck_stats_tracker import DeckStatsTracker
from .card_memory import CardMemory
from .ability_types import ManaAbility
from .curriculum import CurriculumScheduler, OPPONENT_PROFILES, _stable_seed
from .observation_schema import (
    OBSERVATION_SCHEMA_SHA256, OBSERVATION_SCHEMA_VERSION,
    SEMANTIC_IDENTITY_MAX, MAX_DECK_OBSERVATION_SIZE,
)

# Remaining-library composition layout (v4): 8 card-type counts, 7 mana-curve
# buckets (cmc 0,1,2,3,4,5,6+), 5 color counts (WUBRG), and the total count.
LIBRARY_COMPOSITION_TYPES = (
    "creature", "instant", "sorcery", "artifact", "enchantment", "land",
    "planeswalker", "battle",
)
LIBRARY_COMPOSITION_SIZE = len(LIBRARY_COMPOSITION_TYPES) + 7 + 5 + 1


_STATS_ARTIFACT_LOCKS = weakref.WeakValueDictionary()
_STATS_ARTIFACT_LOCKS_GUARD = threading.Lock()


def _card_number(card, attribute, default=0.0):
    try:
        value = float(getattr(card, attribute, default) or 0)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default
    
class AlphaZeroMTGEnv(gym.Env):
    """
    An example Magic: The Gathering environment that uses the Gymnasium (>= 0.26) API.
    Updated for improved reward shaping, richer observations, modularity, and detailed logging.
    """
    ACTION_SPACE_SIZE = 480 # Moved constant here
    REWARD_CONTRACT_VERSION = "tempo-graded-potential-v1"
    DEFAULT_REWARD_DISCOUNT = 0.999
    DEFAULT_ACTION_REWARD_SCALE = 0.0
    DEFAULT_STATE_POTENTIAL_SCALE = 0.40
    DEFAULT_TIME_COST_PER_STEP = 0.005
    # Bounded decisive-win speed premium: +10 at the engine turn ceiling up
    # to +10+WIN_SPEED_BONUS for an immediate kill.  Kept well under the
    # win/loss band so speed can order wins but never outrank winning.
    WIN_SPEED_BONUS = 4.0
    # Turn-limit stalls grade continuously on opponent damage: -10 with an
    # untouched opponent up to -10+TIMEOUT_DAMAGE_CREDIT near lethal.  The
    # ceiling stays below DRAW_REWARD so stalling never beats a real draw.
    TIMEOUT_DAMAGE_CREDIT = 3.0
    DRAW_REWARD = -3.0
    # A mid-game life lead of at least this margin classifies the eventual
    # winner's position as "ahead" (snowball win) or "behind" (comeback win)
    # in the deck/card statistics; smaller leads record as "parity".
    MIDGAME_POSITION_LIFE_MARGIN = 5
    OBSERVATION_SCHEMA_VERSION = OBSERVATION_SCHEMA_VERSION
    OBSERVATION_SCHEMA_SHA256 = OBSERVATION_SCHEMA_SHA256
    EVALUATION_TRACE_MAX_EVENTS = 8_192
    EVALUATION_TRACE_MAX_BYTES = 8 * 1024 * 1024
    EVALUATION_TRACE_ENTRY_MAX_BYTES = 512 * 1024
    EVALUATION_REPLAY_MAX_EVENTS = 4_096
    EVALUATION_REPLAY_MAX_BYTES = 2 * 1024 * 1024
    EVALUATION_REPLAY_ENTRY_MAX_BYTES = 64 * 1024
    EVALUATION_DEBUG_MAX_BYTES = 12 * 1024 * 1024
    # One ordinary game has 120 materialized deck cards.  Leave ample room
    # for tokens/copies while keeping a rules bug from producing an unbounded
    # identity table in every evaluation sidecar.
    EVALUATION_CARD_CATALOG_MAX_ENTRIES = 512
    DIAGNOSTIC_MAX_DEPTH = 10
    DIAGNOSTIC_MAX_NODES = 32_768
    DIAGNOSTIC_MAX_CONTAINER_ITEMS = 512
    DIAGNOSTIC_MAX_STRING_LENGTH = 4_096
    DIAGNOSTIC_MAX_KEY_LENGTH = 256

    def __init__(self, decks, card_db, max_turns=30, max_hand_size=7, max_battlefield=20,
                 deck_stats_path="./deck_stats", card_memory_path="./card_memory",
                 agent_is_p1=True,
                 alternate_agent_seat=False, subtype_vocab=None,
                 strategy_memory_enabled=False,
                 reward_discount=DEFAULT_REWARD_DISCOUNT,
                 action_reward_scale=DEFAULT_ACTION_REWARD_SCALE,
                 state_potential_scale=DEFAULT_STATE_POTENTIAL_SCALE,
                 time_cost_per_step=DEFAULT_TIME_COST_PER_STEP,
                 curriculum=None, opponent_profile="scripted",
                 matchup_seed=None, matchup_weighting=False,
                 adaptive_decision_history_enabled=False,
                 stats_persistence_interval_games=1):
        logging.info("Initializing AlphaZeroMTGEnv...")
        super().__init__()
        self.decks = decks
        self.card_db = card_db
        self.deck_stats_path = deck_stats_path
        self.card_memory_path = card_memory_path
        self.adaptive_decision_history_enabled = bool(
            adaptive_decision_history_enabled)
        self.stats_persistence_interval_games = max(
            1, int(stats_persistence_interval_games))
        # Version stamp for recorded games: card values measured under a weak agent
        # are not card values. main.py sets this to the run id; update it at
        # checkpoints for finer granularity.
        self.agent_version = "unversioned"
        self.training_timestep = None
        self.evaluation_timestep = None
        self.evaluation_checkpoint_sha256 = None
        self._fidelity_agg = {"games_recorded": 0, "unimplemented_action": 0,
                              "unparsed_mana": 0, "unparsed_modal": 0,
                              "unparsed_effects": 0,
                              "effect_continuation_failures": 0,
                              "lost_spell_recoveries": 0,
                              "unparsed_cards": {}}
        self._stats_artifact_states = {}
        self._current_stats_artifact_entry = None
        self.max_turns = max_turns
        self.max_hand_size = max_hand_size
        self.reward_discount = float(reward_discount)
        self.action_reward_scale = float(action_reward_scale)
        self.state_potential_scale = float(state_potential_scale)
        self.time_cost_per_step = float(time_cost_per_step)
        if not 0.0 <= self.reward_discount <= 1.0:
            raise ValueError("reward_discount must be between 0 and 1")
        if (not math.isfinite(self.action_reward_scale)
                or self.action_reward_scale < 0.0):
            raise ValueError("action_reward_scale must be finite and nonnegative")
        if (not math.isfinite(self.state_potential_scale)
                or self.state_potential_scale < 0.0):
            raise ValueError(
                "state_potential_scale must be finite and nonnegative")
        if (not math.isfinite(self.time_cost_per_step)
                or self.time_cost_per_step < 0.0):
            raise ValueError(
                "time_cost_per_step must be finite and nonnegative")
        # The rules hand limit is seven, but the public action map exposes hand
        # slots 0-7 for casting and 0-9 for mandatory discards.  Keep the rules
        # limit on GameState while observing every directly actionable hand slot.
        self.hand_observation_size = max(10, max_hand_size)
        self.max_battlefield = max_battlefield
        # Strategy memory is an optional deterministic advisory subsystem. It
        # is disabled for policy training/evaluation and never enters the
        # learned observation. Explicit users still receive one file per env.
        self.strategy_memory_enabled = bool(strategy_memory_enabled)
        self.strategy_memory = (
            StrategyMemory(memory_file=self._strategy_memory_file())
            if self.strategy_memory_enabled else None)
        self.current_episode_actions = []
        self.replay_actions = []
        # Successful evaluation traces are intentionally opt-in.  A normal
        # training environment never pays the state-snapshot or serialization
        # cost; set_evaluation_checkpoint() is the enablement boundary.
        self.evaluation_action_trace = []
        self._reset_evaluation_capture_telemetry()
        self.reset_seed = None
        self.opponent_policy = None
        self._direct_opponent_policy = None
        # Round 7.98 self-play keeps at most one committed checkpoint policy
        # per worker.  A replacement is loaded and validated synchronously at
        # the staging boundary (so an env_method failure aborts training), but
        # does not become active until reset.  This prevents a callback from
        # changing the opponent halfway through an episode.
        self._resident_checkpoint_opponent_policy = None
        self._active_checkpoint_opponent = None
        self._pending_checkpoint_opponent_policy = None
        self._pending_checkpoint_opponent = None
        self._checkpoint_opponent_pending_set = False
        self._checkpoint_opponent_probability = 0.0
        self._checkpoint_opponent_seed = 0
        self._checkpoint_opponent_rng = random.Random(0)
        self._current_checkpoint_opponent = None
        self._checkpoint_opponent_last_error = None
        if opponent_profile not in OPPONENT_PROFILES:
            raise ValueError(f"Unknown opponent profile: {opponent_profile}")
        self.default_opponent_profile = opponent_profile
        self.active_opponent_profile = opponent_profile
        # Annealed opponent strength: the trainer stages (epsilon, profiles)
        # via set_opponent_handicap and the value commits at the next reset so
        # opponent strength never changes mid-episode.
        self.active_opponent_handicap = 0.0
        self._pending_opponent_handicap = 0.0
        self._pending_handicap_profiles = frozenset()
        self._opponent_handicap_rng = random.Random(0)
        self.curriculum = curriculum
        # Opt-in matchup weighting only biases training deck selection through
        # the scheduler; fixed evaluation uses an explicit schedule and is
        # unaffected.
        self.matchup_weighting = bool(matchup_weighting)
        self.curriculum_scheduler = (
            CurriculumScheduler(
                curriculum, matchup_seed,
                matchup_weighting=self.matchup_weighting)
            if curriculum is not None else None)
        self.matchup_seed = int(matchup_seed or 0)
        self._matchup_rng = random.Random(self.matchup_seed)
        self._engine_seed_rng = random.Random(
            self.matchup_seed ^ 0x9E3779B97F4A7C15)
        self._episode_schedule = []
        self._episode_schedule_index = 0
        self.current_curriculum_stage = None
        self.current_curriculum_stage_index = None
        self.current_matchup_episode_index = None
        self.current_agent_deck = None
        self.current_opponent_deck = None
        # Training can alternate the learned policy between seats without
        # changing the default behavior used by fixtures and direct callers.
        self.initial_agent_is_p1 = bool(agent_is_p1)
        self._episode_agent_is_p1 = self.initial_agent_is_p1
        self.alternate_agent_seat = bool(alternate_agent_seat)
        self._successful_reset_count = 0
        self.current_analysis = None
        # Card.SUBTYPE_VOCAB is populated by load_decks_and_card_db *after*
        # this module is imported.  The former import-time dummy therefore
        # measured a zero-subtype vector (177 fields) and silently truncated
        # every production vector (225 fields for the current pool).  Capture
        # this environment's vocabulary and build vectors against it so later
        # test/database loads cannot mutate the policy schema underneath us.
        if subtype_vocab is None:
            subtype_vocab = tuple(Card.SUBTYPE_VOCAB)
        else:
            # A frozen format schema may intentionally contain subtype columns
            # not represented by the selected deck corpus.  Carry that exact
            # ordered vocabulary into spawned workers instead of rebuilding it
            # from only their cards.
            subtype_vocab = tuple(subtype_vocab)
        if not subtype_vocab:
            subtype_vocab = tuple(sorted({
                str(subtype).lower()
                for card in card_db.values()
                for subtype in getattr(card, "subtypes", [])
            }))
        self._subtype_vocab = subtype_vocab
        self._feature_dim = (
            4 + 6 + len(Card.ALL_KEYWORDS) + 5 + len(self._subtype_vocab) + 3)
        # The observation contract gives every visible card slot a categorical semantic
        # identity.  Registry IDs may be sparse in a selected corpus, so size
        # the embedding namespace from the largest frozen canonical index,
        # not from len(card_db).  0 is padding, 1 is a visible unknown/token,
        # and canonical index N is encoded as N+2.
        deck_canonical_ids = {
            int(card_id)
            for deck in decks if isinstance(deck, dict)
            for card_id in deck.get("cards", ())
            if isinstance(card_id, (int, np.integer)) and int(card_id) >= 0
        }
        # A previously used shared card_db can still contain retired runtime
        # clones. Derive canonical entries from deck inputs so those per-game
        # IDs never become learned semantic categories in a later env.
        canonical_entries = [
            (card_id, card_db[card_id]) for card_id in deck_canonical_ids
            if card_id in card_db
        ]
        if not canonical_entries:
            canonical_entries = [
                (int(card_id), card) for card_id, card in card_db.items()
                if isinstance(card_id, (int, np.integer)) and int(card_id) >= 0
            ]
        self._canonical_card_id_max = max(
            (card_id for card_id, _ in canonical_entries), default=-1)
        if self._canonical_card_id_max + 2 > SEMANTIC_IDENTITY_MAX:
            raise ValueError(
                "Canonical card registry exceeds the observation contract's semantic "
                f"identity capacity ({SEMANTIC_IDENTITY_MAX - 1} cards)")
        self._canonical_card_ids = {
            card_id for card_id, _ in canonical_entries}
        self._semantic_identity_high = SEMANTIC_IDENTITY_MAX
        self._canonical_card_ids_by_name = {
            str(getattr(card, "name", "")).casefold(): card_id
            for card_id, card in canonical_entries
            if getattr(card, "name", None)
        }
        logging.info(
            "Using feature dimension %s (%s subtype fields), observation "
            "schema v%s (%s semantic identities)",
            self._feature_dim, len(self._subtype_vocab),
            self.OBSERVATION_SCHEMA_VERSION,
            self._semantic_identity_high + 1)

        # Initialize deck statistics tracker (Corrected class name usage)
        try:
            self.stats_tracker = DeckStatsTracker(
                storage_path=self.deck_stats_path,
                card_db=card_db,
                decks=decks,
                persistence_interval_games=
                    self.stats_persistence_interval_games)
            self.has_stats_tracker = True
        except (ImportError, ModuleNotFoundError, NameError):
            logging.warning("DeckStatsTracker not available, statistics will not be recorded")
            self.stats_tracker = None
            self.has_stats_tracker = False

        # --- ADDED: Initialize Card Memory ---
        try:
            self.card_memory = CardMemory(storage_path=self.card_memory_path) # Initialize CardMemory
            self.has_card_memory = True
            logging.debug("CardMemory system initialized successfully")
        except (ImportError, ModuleNotFoundError, NameError):
            logging.warning("CardMemory not available, card statistics will not be tracked dynamically")
            self.card_memory = None
            self.has_card_memory = False
        except Exception as e:
            logging.error(f"Error initializing CardMemory: {str(e)}")
            self.card_memory = None
            self.has_card_memory = False
        # --- END ADDED ---


        # Initialize game state manager
        self.game_state = GameState(self.card_db, max_turns, max_hand_size, max_battlefield)

        # The environment is the single owner of the agent layer (action handler,
        # card evaluator, strategic planner). GameState no longer constructs these
        # for the primary game — only clone() builds its own for MCTS simulations.
        self._build_agents()

        # Feature dimension determined dynamically above
        # BUGFIX: PHASE_CLEANUP (15) is NOT the highest phase constant; special phases
        # (FIRST_STRIKE_DAMAGE=16, TARGETING=17, SACRIFICE=18, CHOOSE=19) exceed it,
        # which made the declared space too small and zeroed their one-hot encoding.
        MAX_PHASE = max(v for k, v in vars(type(self.game_state)).items()
                        if k.startswith("PHASE_") and isinstance(v, int))

        self.action_memory_size = 80

        # Correct keyword size based on Card class
        keyword_dimension = len(Card.ALL_KEYWORDS)
        logging.info(f"Using keyword dimension: {keyword_dimension}")

        # Fixed-size card-detail tensors deliberately retain only the first
        # ``max_battlefield`` objects, but exact scalar counts describe the
        # complete rules state.  Magic has no 20-permanent ceiling, so those
        # scalars need independent bounds.
        count_observation_max = 1000
        combat_stat_observation_max = 1_000_000

        # Card features are heterogeneous.  P/T is signed and can legally
        # leave the old -1..50 range, while keyword/color/subtype flags remain
        # binary.  Use component-aware bounds for every card-detail tensor.
        card_feature_low = np.zeros(self._feature_dim, dtype=np.float32)
        card_feature_high = np.ones(self._feature_dim, dtype=np.float32)
        card_feature_high[0] = combat_stat_observation_max  # mana value
        card_feature_low[2:4] = -combat_stat_observation_max
        card_feature_high[2:4] = combat_stat_observation_max
        card_feature_high[4:10] = combat_stat_observation_max  # mana pips
        mdfc_offset = (
            4 + 6 + len(Card.ALL_KEYWORDS) + 5 + len(self._subtype_vocab))
        card_feature_low[mdfc_offset + 1:mdfc_offset + 3] = \
            -combat_stat_observation_max
        card_feature_high[mdfc_offset + 1:mdfc_offset + 3] = \
            combat_stat_observation_max

        def card_feature_space(rows):
            return spaces.Box(
                low=np.broadcast_to(card_feature_low, (rows, self._feature_dim)).copy(),
                high=np.broadcast_to(card_feature_high, (rows, self._feature_dim)).copy(),
                dtype=np.float32)

        def semantic_identity_space(rows):
            return spaces.Box(
                low=0, high=self._semantic_identity_high,
                shape=(rows,), dtype=np.int32)

        # --- UPDATED: Observation Space with Context Facilitation Fields ---
        self.observation_space = spaces.Dict({
            # --- Existing Fields (mostly unchanged shapes) ---
            "phase": spaces.Box(low=0, high=MAX_PHASE, shape=(1,), dtype=np.int32),
            # Turn-limit adjudication occurs after the counter advances past
            # max_turns, so the terminal observation legitimately sees +1.
            "turn": spaces.Box(low=0, high=self.max_turns + 1, shape=(1,), dtype=np.int32),
            "is_my_turn": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            # Life can exceed the starting total and may be negative in a terminal
            # state.  The former 0..40 bounds rejected legal observations.
            "my_life": spaces.Box(low=-10000, high=10000, shape=(1,), dtype=np.int32),
            "opp_life": spaces.Box(low=-10000, high=10000, shape=(1,), dtype=np.int32),
            "life_difference": spaces.Box(low=-20000, high=20000, shape=(1,), dtype=np.int32),
            "my_hand": card_feature_space(self.hand_observation_size),
            "my_hand_card_identity": semantic_identity_space(self.hand_observation_size),
            "my_hand_count": spaces.Box(low=0, high=1000, shape=(1,), dtype=np.int32),
            "opp_hand_count": spaces.Box(low=0, high=1000, shape=(1,), dtype=np.int32),
            "hand_playable": spaces.Box(low=0, high=1, shape=(self.hand_observation_size,), dtype=np.float32),
            "hand_card_types": spaces.Box(low=0, high=1, shape=(self.hand_observation_size, 5), dtype=np.float32),
            "hand_synergy_scores": spaces.Box(low=0, high=1, shape=(self.hand_observation_size,), dtype=np.float32),
            "opportunity_assessment": spaces.Box(low=0, high=10, shape=(self.hand_observation_size,), dtype=np.float32),
            "my_battlefield": card_feature_space(self.max_battlefield),
            "my_battlefield_card_identity": semantic_identity_space(self.max_battlefield),
            "my_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "opp_battlefield": card_feature_space(self.max_battlefield),
            "opp_battlefield_card_identity": semantic_identity_space(self.max_battlefield),
            "opp_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "my_battlefield_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "opp_battlefield_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            # Counter columns: +1/+1, -1/-1, loyalty, defense, lore, other.
            "my_permanent_counters": spaces.Box(low=0, high=combat_stat_observation_max, shape=(self.max_battlefield, 6), dtype=np.int32),
            "opp_permanent_counters": spaces.Box(low=0, high=combat_stat_observation_max, shape=(self.max_battlefield, 6), dtype=np.int32),
            "my_damage_marked": spaces.Box(low=0, high=combat_stat_observation_max, shape=(self.max_battlefield,), dtype=np.int32),
            "opp_damage_marked": spaces.Box(low=0, high=combat_stat_observation_max, shape=(self.max_battlefield,), dtype=np.int32),
            # Relative combined battlefield index: my rows first, then opp.
            "my_attachment_targets": spaces.Box(low=-1, high=self.max_battlefield * 2 - 1, shape=(self.max_battlefield,), dtype=np.int32),
            "opp_attachment_targets": spaces.Box(low=-1, high=self.max_battlefield * 2 - 1, shape=(self.max_battlefield,), dtype=np.int32),
            "my_attachment_counts": spaces.Box(low=0, high=count_observation_max, shape=(self.max_battlefield,), dtype=np.int32),
            "opp_attachment_counts": spaces.Box(low=0, high=count_observation_max, shape=(self.max_battlefield,), dtype=np.int32),
            "my_creature_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "opp_creature_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "my_total_power": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "my_total_toughness": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "opp_total_power": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "opp_total_toughness": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "creature_advantage": spaces.Box(low=-count_observation_max, high=count_observation_max, shape=(1,), dtype=np.int32),
            "power_advantage": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "toughness_advantage": spaces.Box(low=-combat_stat_observation_max, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "threat_assessment": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "card_synergy_scores": spaces.Box(low=-1, high=1, shape=(self.max_battlefield, self.max_battlefield), dtype=np.float32),
            "my_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "opp_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "my_snow_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "opp_snow_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "my_restricted_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "opp_restricted_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "total_available_mana": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            # v5: producible mana by color (WUBRG) from visible untapped
            # sources plus floating mana -- own is exact, opponent is the
            # public estimate from its face-up untapped lands.
            "my_producible_mana": spaces.Box(low=0, high=100, shape=(5,), dtype=np.float32),
            "opp_producible_mana": spaces.Box(low=0, high=100, shape=(5,), dtype=np.float32),
            "untapped_land_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "turn_vs_mana": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "my_library_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "opp_library_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            # Player counters: poison, energy, experience. Status: blessing, monarch.
            "my_player_counters": spaces.Box(low=0, high=count_observation_max, shape=(3,), dtype=np.int32),
            "opp_player_counters": spaces.Box(low=0, high=count_observation_max, shape=(3,), dtype=np.int32),
            "my_player_status": spaces.Box(low=0, high=1, shape=(2,), dtype=np.int32),
            "opp_player_status": spaces.Box(low=0, high=1, shape=(2,), dtype=np.int32),
            "my_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_exile_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "opp_exile_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "my_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_graveyard_cards": card_feature_space(10),
            "my_graveyard_card_identity": semantic_identity_space(10),
            "opp_graveyard_cards": card_feature_space(10),
            "opp_graveyard_card_identity": semantic_identity_space(10),
            "my_exile_cards": card_feature_space(10),
            "my_exile_card_identity": semantic_identity_space(10),
            "my_exile_card_visibility": spaces.Box(low=0, high=1, shape=(10,), dtype=bool),
            "opp_exile_cards": card_feature_space(10),
            "opp_exile_card_identity": semantic_identity_space(10),
            "opp_exile_card_visibility": spaces.Box(low=0, high=1, shape=(10,), dtype=bool),
            "stack_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "stack_controller": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.int32),
            "stack_card_types": spaces.Box(low=0, high=1, shape=(5, 5), dtype=np.float32),
            "stack_cards": card_feature_space(5),
            "stack_card_identity": semantic_identity_space(5),
            # 0 empty, 1 spell, 2 activated ability, 3 trigger, 4 other.
            "stack_object_kinds": spaces.Box(low=0, high=4, shape=(5,), dtype=np.int32),
            "stack_target_counts": spaces.Box(low=0, high=count_observation_max, shape=(5,), dtype=np.int32),
            "stack_mode_counts": spaces.Box(low=0, high=100, shape=(5,), dtype=np.int32),
            "attackers_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            "blockers_count": spaces.Box(low=0, high=count_observation_max, shape=(1,), dtype=np.int32),
            # Row = relative combined battlefield object. -2 means not
            # attacking, -1 is an off-window defender, 0 is the defending
            # player, and N+1 means public permanent N.
            "combat_attack_targets": spaces.Box(low=-2, high=self.max_battlefield * 2, shape=(self.max_battlefield * 2,), dtype=np.int32),
            # Row = blocker; value = relative combined attacker index or -1.
            "combat_blocker_assignments": spaces.Box(low=-1, high=self.max_battlefield * 2 - 1, shape=(self.max_battlefield * 2,), dtype=np.int32),
            "potential_combat_damage": spaces.Box(low=0, high=combat_stat_observation_max, shape=(1,), dtype=np.int32),
            "ability_features": spaces.Box(low=0, high=10, shape=(self.max_battlefield, 5), dtype=np.float32),
            "ability_timing": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "planeswalker_activations": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activation_counts": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "previous_actions": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE, shape=(self.action_memory_size,), dtype=np.int32),
            "previous_rewards": spaces.Box(low=-1000, high=1000, shape=(self.action_memory_size,), dtype=np.float32),
            "phase_history": spaces.Box(low=-1, high=MAX_PHASE, shape=(5,), dtype=np.int32),
            "action_mask": spaces.Box(low=0, high=1, shape=(self.ACTION_SPACE_SIZE,), dtype=bool),
            "optimal_attackers": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "attacker_values": spaces.Box(low=-10, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "ability_recommendations": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5, 2), dtype=np.float32),
            "strategic_metrics": spaces.Box(low=-1, high=1, shape=(7,), dtype=np.float32),
            "position_advantage": spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32),
            "deck_composition_estimate": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "opponent_archetype": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            # v4: the observer's own full starting decklist as canonical
            # identities (order-free multiset), and the remaining-library
            # composition.  Both are observer-own only; the opponent's deck is
            # never exposed.
            "my_deck_card_identity": semantic_identity_space(MAX_DECK_OBSERVATION_SIZE),
            "my_library_composition": spaces.Box(low=0, high=count_observation_max, shape=(LIBRARY_COMPOSITION_SIZE,), dtype=np.float32),
            "future_state_projections": spaces.Box(low=-1, high=1, shape=(7,), dtype=np.float32),
            "multi_turn_plan": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_viability": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_timings": spaces.Box(low=0, high=self.max_turns + 1, shape=(6,), dtype=np.float32),
            "mulligan_in_progress": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "mulligan_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "mulligan_reason_count": spaces.Box(low=0, high=5, shape=(1,), dtype=np.int32),
            "mulligan_reasons": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "targetable_permanents": spaces.Box(low=-1, high=np.iinfo(np.int32).max, shape=(self.max_battlefield * 2,), dtype=np.int32),
            "targetable_players": spaces.Box(low=-1, high=1, shape=(2,), dtype=np.int32),
            "targetable_spells_on_stack": spaces.Box(low=-1, high=np.iinfo(np.int32).max, shape=(5,), dtype=np.int32),
            "targetable_cards_in_graveyards": spaces.Box(low=-1, high=np.iinfo(np.int32).max, shape=(10 * 2,), dtype=np.int32),
            # Exact SELECT_TARGET page. Slot i describes action 274+i.
            "target_cards": card_feature_space(10),
            "target_card_identity": semantic_identity_space(10),
            "target_card_mask": spaces.Box(low=0, high=1, shape=(10,), dtype=bool),
            "target_card_ids": spaces.Box(low=-1, high=2147483647, shape=(10,), dtype=np.int64),
            "target_kinds": spaces.Box(low=0, high=6, shape=(10,), dtype=np.int32),
            "target_controllers": spaces.Box(low=-1, high=1, shape=(10,), dtype=np.int32),
            "target_zone_indices": spaces.Box(low=-1, high=1000000, shape=(10,), dtype=np.int32),
            "sacrificeable_permanents": spaces.Box(low=-1, high=self.max_battlefield, shape=(self.max_battlefield,), dtype=np.int32),
            "selectable_modes": spaces.Box(low=-1, high=10, shape=(10,), dtype=np.int32),
            "selectable_colors": spaces.Box(low=-1, high=4, shape=(5,), dtype=np.int32),
            "choice_cards": card_feature_space(10),
            "choice_card_identity": semantic_identity_space(10),
            "choice_card_mask": spaces.Box(low=0, high=1, shape=(10,), dtype=bool),
            "choice_kind": spaces.Box(low=0, high=16, shape=(1,), dtype=np.int32),
            "choice_remaining": spaces.Box(
                low=0, high=np.iinfo(np.int32).max,
                shape=(1,), dtype=np.int32),
            "choice_allocation_counts": spaces.Box(
                low=0, high=np.iinfo(np.int32).max,
                shape=(10,), dtype=np.int32),
            "valid_x_range": spaces.Box(
                low=-1, high=np.iinfo(np.int32).max,
                shape=(2,), dtype=np.int32),
            "bottomable_cards": spaces.Box(low=0, high=1, shape=(self.hand_observation_size,), dtype=bool),
            "dredgeable_cards_in_gy": spaces.Box(low=-1, high=100, shape=(6,), dtype=np.int32),
        })
        # *** End Observation Space Modification ***

        # Scalar game quantities (P/T, mana, life) have no rules-defined
        # ceiling; their declared bounds are deliberate saturation points.
        # Exceeding them (doubling combos reach 2^20 power) is expected in
        # degenerate games, so clipping these features must not warn or be
        # recorded as an observation fidelity error. Structural features
        # (masks, indices, phases) keep the hard bound check.
        self._saturating_features = frozenset({
            "my_life", "opp_life", "life_difference",
            "my_hand", "my_battlefield", "opp_battlefield",
            "my_graveyard_cards", "opp_graveyard_cards",
            "my_exile_cards", "opp_exile_cards", "stack_cards",
            "target_cards", "choice_cards",
            "my_total_power", "my_total_toughness", "opp_total_power",
            "opp_total_toughness", "power_advantage", "toughness_advantage",
            "potential_combat_damage", "my_mana_pool", "opp_mana_pool",
            "my_snow_mana_pool", "opp_snow_mana_pool",
            "my_restricted_mana_pool", "opp_restricted_mana_pool",
            "my_permanent_counters", "opp_permanent_counters",
            "my_damage_marked", "opp_damage_marked", "total_available_mana",
        })
        self.action_space = spaces.Discrete(self.ACTION_SPACE_SIZE)
        # Add memory for actions and rewards
        self.last_n_actions = np.full(self.action_memory_size, -1, dtype=np.int32) # Use -1 for padding
        self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)
        # Checkpoint opponents must never inherit the learned policy's private
        # reward/action history.  Keep an independent role-local stream while
        # preserving the legacy learned-role attributes above.
        self.opponent_last_n_actions = np.full(
            self.action_memory_size, -1, dtype=np.int32)
        self.opponent_last_n_rewards = np.zeros(
            self.action_memory_size, dtype=np.float32)

        self.invalid_action_limit = 150  # Max invalid actions before episode termination
        self.max_episode_steps = 2000

        # Episode metrics
        self.current_step = 0
        self.invalid_action_count = 0
        self.episode_rewards = []
        self.episode_invalid_actions = 0
        self.current_episode_actions = []
        self.detailed_logging = False

        # Valid actions mask
        self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
        self.last_observation_error = None
        self.last_observation_traceback = None
        # Added attribute tracking phase/choice context specifically
        self.current_choice_context = None

        

    def _strategy_memory_file(self):
        """Per-env strategy-memory location under this env's storage scope."""
        memory_dir = getattr(self, 'card_memory_path', None) or \
            getattr(self, 'deck_stats_path', None)
        if memory_dir:
            return os.path.join(memory_dir, "strategy_memory.pkl")
        return "strategy_memory.pkl"

    def initialize_strategic_memory(self):
        """Connect the optional memory without reconstructing it on reset."""
        try:
            if not self.strategy_memory_enabled:
                self.strategy_memory = None
                self.game_state.strategy_memory = None
                return
            if self.strategy_memory is None:
                self.strategy_memory = StrategyMemory(
                    memory_file=self._strategy_memory_file())
            self.game_state.strategy_memory = self.strategy_memory
            logging.debug("Strategic memory system initialized successfully")
        except ImportError as e:
            logging.warning(f"StrategyMemory not available: {e}")
            self.strategy_memory = None
        except Exception as e:
            logging.error(f"Error initializing strategy memory: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            self.strategy_memory = None
    

    def action_mask(self, env=None):
        """Return the current action mask as boolean array. NO CACHING."""
        # Regenerate the mask on every call
        self.last_action_mask_error = None
        try:
            # Ensure ActionHandler exists and is linked to the current GameState
            if not hasattr(self, 'action_handler') or self.action_handler is None or self.action_handler.game_state != self.game_state:
                # Attempt to re-link or re-create if missing
                if hasattr(self.game_state, 'action_handler') and self.game_state.action_handler:
                    self.action_handler = self.game_state.action_handler
                    logging.debug("Relinked ActionHandler from GameState.")
                else:
                    logging.warning("Recreating ActionHandler in action_mask.")
                    # Need ActionHandler class defined or imported
                    self.action_handler = ActionHandler(self.game_state)

            self.current_valid_actions = self.action_handler.generate_valid_actions()

            # Validate the generated mask
            if (self.current_valid_actions is None
                    or not isinstance(self.current_valid_actions, np.ndarray)
                    or self.current_valid_actions.shape != (self.ACTION_SPACE_SIZE,)
                    or not self.current_valid_actions.astype(bool).any()):
                raise ValueError(f"generate_valid_actions returned invalid mask: shape {getattr(self.current_valid_actions, 'shape', 'None')}, type {type(self.current_valid_actions)}")

        except Exception as e:
            self.last_action_mask_error = f"{type(e).__name__}: {e}"
            logging.error(f"Error generating valid actions in action_mask: {str(e)}")
            import traceback
            logging.error(f"{traceback.format_exc()}")
            # Preserve the degraded-mask signal for strict policy boundaries.
            # Returning PASS/CONCEDE remains a compatibility fallback for
            # ordinary diagnostic callers, but checkpoint self-play must not
            # mistake it for a real legal mask and silently skip a turn.
            if getattr(self, "action_handler", None) is not None:
                self.action_handler.last_mask_error = (
                    f"{type(e).__name__}: {e}")
            # Fallback to basic action mask if generation fails
            self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            self.current_valid_actions[11] = True # Enable PASS_PRIORITY
            self.current_valid_actions[12] = True  # Enable CONCEDE as fallback

        # Return bool version
        return self.current_valid_actions.astype(bool)

    def set_curriculum_timestep(self, timestep):
        """Select the stage that the next successful reset will commit."""
        if self.curriculum_scheduler is not None:
            self.curriculum_scheduler.set_timestep(timestep)

    def set_curriculum_stage(self, stage_index, timestep=None):
        """Apply a centrally coordinated mastery stage to future resets."""
        if self.curriculum_scheduler is not None:
            self.curriculum_scheduler.set_stage(stage_index, timestep)

    def set_opponent_handicap(self, epsilon, profiles=()):
        """Stage a pass-probability handicap for future scripted opponents.

        With probability ``epsilon`` a handicapped opponent takes the passive
        (goldfish) baseline for one priority decision instead of attacking,
        blocking, or developing.  The trainer anneals ``epsilon`` toward zero
        as the policy earns wins; the value commits at the next reset so an
        episode never changes opponent strength mid-game.
        """
        epsilon = float(epsilon)
        if not math.isfinite(epsilon) or not 0.0 <= epsilon <= 1.0:
            raise ValueError("Opponent handicap must be within [0, 1]")
        profiles = frozenset(str(profile) for profile in (profiles or ()))
        unknown = sorted(profiles - OPPONENT_PROFILES)
        if unknown:
            raise ValueError(f"Unknown handicap profiles: {unknown}")
        self._pending_opponent_handicap = epsilon
        self._pending_handicap_profiles = profiles

    def set_training_timestep(self, timestep):
        """Stamp subsequent game records with their rollout's lower bound."""
        self.training_timestep = int(timestep)

    def set_evaluation_checkpoint(self, timestep, checkpoint_sha256):
        """Attribute evaluator game records to the exact policy snapshot."""
        self.evaluation_timestep = int(timestep)
        self.evaluation_checkpoint_sha256 = str(checkpoint_sha256)
        evaluator = getattr(self, "card_evaluator", None)
        enable_diagnostics = getattr(
            evaluator, "set_diagnostics_enabled", None)
        if callable(enable_diagnostics):
            try:
                enable_diagnostics(True, reset=True)
            except Exception as error:
                logging.warning(
                    "Could not enable evaluation card diagnostics: %s", error)
                self._note_evaluation_capture_error(
                    "enable_evaluator_diagnostics", error, scope="trace")

    def set_episode_schedule(self, cases):
        """Install an exact reset schedule used by deterministic evaluation."""
        normalized = []
        for index, case in enumerate(cases or []):
            if not isinstance(case, dict):
                raise ValueError(f"Evaluation case {index} is not a mapping")
            required = {"seed", "p1_deck", "p2_deck", "agent_is_p1"}
            missing = sorted(required - set(case))
            if missing:
                raise ValueError(
                    f"Evaluation case {index} is missing fields: {missing}")
            item = dict(case)
            item.setdefault("opponent_profile", "scripted")
            if item["opponent_profile"] not in OPPONENT_PROFILES:
                raise ValueError(
                    f"Evaluation case {index} has an unknown opponent profile")
            normalized.append(item)
        self._episode_schedule = normalized
        self._episode_schedule_index = 0

    def reset_episode_schedule(self):
        self._episode_schedule_index = 0

    def _curriculum_reset_case(self, agent_is_p1):
        if self.curriculum_scheduler is None:
            return None
        return self.curriculum_scheduler.peek(agent_is_p1)

    def _episode_metadata(self):
        return {
            "episode_seed": getattr(self, "reset_seed", None),
            "p1_deck": getattr(self, "current_deck_name_p1", None),
            "p2_deck": getattr(self, "current_deck_name_p2", None),
            "agent_is_p1": bool(getattr(
                getattr(self, "game_state", None), "agent_is_p1", True)),
            "curriculum_stage": getattr(
                self, "current_curriculum_stage", None),
            "curriculum_stage_index": getattr(
                self, "current_curriculum_stage_index", None),
            "opponent_profile": getattr(
                self, "active_opponent_profile", "scripted"),
            "opponent_handicap": float(getattr(
                self, "active_opponent_handicap", 0.0) or 0.0),
            "opponent_policy_id": (
                (getattr(self, "_current_checkpoint_opponent", None) or {})
                .get("policy_id")),
            "opponent_checkpoint_sha256": (
                (getattr(self, "_current_checkpoint_opponent", None) or {})
                .get("sha256")),
            "max_turns": int(getattr(
                getattr(self, "game_state", None), "max_turns", None)
                or getattr(self, "max_turns", 0) or 0) or None,
            "agent_deck": getattr(self, "current_agent_deck", None),
            "opponent_deck": getattr(self, "current_opponent_deck", None),
            "matchup_episode_index": getattr(
                self, "current_matchup_episode_index", None),
            "training_timestep": getattr(
                self, "training_timestep", None),
            "evaluation_timestep": getattr(
                self, "evaluation_timestep", None),
            "evaluation_checkpoint_sha256":
                getattr(self, "evaluation_checkpoint_sha256", None),
        }

    def _finalize_previous_episode_artifacts(self):
        """Do not discard a terminal row merely because a new reset arrived."""
        if (getattr(self, '_game_result_recorded', False)
                and not getattr(self, '_game_logged', False)
                and not self._ensure_stats_artifacts_written()):
            raise RuntimeError(
                "Cannot reset while the previous game's provenance is pending")

    def reset(self, seed=None, options=None):
            """
            Reset the environment and return initial observation and info.
            Aligns with GameState starting in the Mulligan phase.

            Args:
                seed: Random seed
                options: Dictionary of options (Gymnasium API)

            Returns:
                tuple: Initial observation and info dictionary
            """
            self._finalize_previous_episode_artifacts()
            explicit_options = dict(options or {})
            scheduled_case = None
            scheduled_index = None
            if self._episode_schedule:
                scheduled_index = (
                    self._episode_schedule_index % len(self._episode_schedule))
                scheduled_case = dict(self._episode_schedule[scheduled_index])
                # Fixed evaluation takes absolute precedence over curriculum,
                # replay options, ordinary matchup RNG, and seat alternation.
                options = scheduled_case
                seed = int(scheduled_case["seed"])
            else:
                options = explicit_options

            # Matchmaking owns a separate RNG. Game randomness may consume the
            # global streams freely without changing a later deck/profile draw.
            if seed is None:
                seed = self._engine_seed_rng.randrange(0, 2**32)
            random.seed(seed)
            np.random.seed(seed % (2**32))
            self.reset_seed = int(seed)
            self._reset_fallback_active = False
            env_id = getattr(self, "env_id", id(self)) # For tracking
            try:
                # --- Pre-Reset Logging & Safety Checks ---
                logging.info(f"RESETTING environment {env_id}...")
                
                # Call parent reset method (Gymnasium handles seeding)
                super().reset(seed=seed, options=options)

                # --- Reset Internal Environment State ---
                self.current_step = 0
                self.invalid_action_count = 0
                self.episode_rewards = []
                self.episode_invalid_actions = 0
                self.current_episode_actions = []
                self.replay_actions = []
                self.evaluation_action_trace = []
                self._reset_evaluation_capture_telemetry()
                self._game_result_recorded = False
                self._matchup_result_recorded = False
                self._life_totals_by_turn = {}
                self._stats_result_record_attempted = False
                self._card_memory_result_record_attempted = False
                self._stats_result_record_accepted = None
                self._stats_result_flush_succeeded = None
                self._card_memory_result_record_accepted = None
                self._game_result_recording_failed = False
                self._game_result_flush_failed = False
                self._game_logged = False
                self._game_artifact_write_failed = False
                self._current_stats_artifact_entry = None
                self._logged_card_ids = set()
                self._logged_errors = set()
                self.last_observation_error = None
                self.last_observation_traceback = None
                self.current_analysis = None
                self.last_n_actions = np.full(self.action_memory_size, -1, dtype=np.int32)
                self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)
                self.opponent_last_n_actions = np.full(
                    self.action_memory_size, -1, dtype=np.int32)
                self.opponent_last_n_rewards = np.zeros(
                    self.action_memory_size, dtype=np.float32)
                self._observed_phase_history = []
                if hasattr(self, '_phase_history_counts'): self._phase_history_counts = defaultdict(int)
                if hasattr(self, '_last_phase_progressed'): self._last_phase_progressed = -1
                if hasattr(self, '_phase_stuck_count'): self._phase_stuck_count = 0

                # Commit a fully validated frozen checkpoint only at this
                # episode boundary, then independently choose checkpoint or
                # scripted play for the complete episode.
                self._commit_checkpoint_opponent_for_reset()

                # Resolve the learned seat before selecting semantic agent and
                # opponent decks. Selecting P1/P2 decks first silently swaps a
                # curriculum matchup every other episode.
                seat_is_p1 = self.initial_agent_is_p1
                if (self.alternate_agent_seat
                        and self._successful_reset_count % 2 == 1):
                    seat_is_p1 = not seat_is_p1
                requested_seat = (options or {}).get("agent_is_p1")
                if isinstance(requested_seat, bool):
                    seat_is_p1 = requested_seat

                curriculum_case = None
                has_explicit_matchup = any(
                    key in explicit_options and explicit_options.get(key) is not None
                    for key in ("p1_deck", "p2_deck"))
                if (scheduled_case is None and not has_explicit_matchup
                        and self.curriculum_scheduler is not None):
                    curriculum_case = self._curriculum_reset_case(seat_is_p1)
                    # Curriculum supplies defaults; explicit seat/profile
                    # options remain authoritative for replay/probes.
                    options = dict(curriculum_case)
                    options.update(explicit_options)

                requested_profile = (options or {}).get(
                    "opponent_profile", self.default_opponent_profile)
                if requested_profile not in OPPONENT_PROFILES:
                    raise ValueError(
                        f"Unknown opponent profile: {requested_profile}")
                self.active_opponent_profile = requested_profile
                # Fixed evaluation always faces full-strength opponents; the
                # annealed training handicap commits here so opponent strength
                # stays constant within an episode. A replay may instead pin
                # the exact recorded handicap without mutating the staged value
                # used by later training resets. The handicap RNG stream is
                # derived from the reset seed but independent of the game and
                # matchmaking streams.
                explicit_handicap = (options or {}).get(
                    "opponent_handicap") if scheduled_case is None else None
                if explicit_handicap is not None:
                    explicit_handicap = float(explicit_handicap)
                    if (not math.isfinite(explicit_handicap)
                            or not 0.0 <= explicit_handicap <= 1.0):
                        raise ValueError(
                            "Opponent handicap must be within [0, 1]")
                    self.active_opponent_handicap = explicit_handicap
                else:
                    self.active_opponent_handicap = (
                        self._pending_opponent_handicap
                        if (scheduled_case is None
                            and requested_profile
                            in self._pending_handicap_profiles)
                        else 0.0)
                self._opponent_handicap_rng = random.Random(
                    _stable_seed(seed, "opponent-handicap"))

                # --- Deck Selection ---
                if not self.decks:
                    logging.error("No decks available in environment! Using dummy deck.")
                    dummy_deck = [{"name": "Dummy Card", "type_line": "Creature", "mana_cost": "{1}", "card_id": "dummy_1"}] * 60
                    p1_deck_data = {"name": "Dummy Deck P1", "cards": dummy_deck}
                    p2_deck_data = {"name": "Dummy Deck P2", "cards": dummy_deck}
                else:
                    requested_p1 = (options or {}).get("p1_deck")
                    requested_p2 = (options or {}).get("p2_deck")

                    def deck_by_name(requested_name):
                        if requested_name is None:
                            return None
                        return next(
                            (deck for deck in self.decks
                             if isinstance(deck, dict)
                             and deck.get("name") == requested_name),
                            None)

                    p1_deck_data = deck_by_name(requested_p1)
                    p2_deck_data = deck_by_name(requested_p2)
                    if requested_p1 is not None and p1_deck_data is None:
                        raise ValueError(
                            f"Requested replay P1 deck is unavailable: {requested_p1}")
                    if requested_p2 is not None and p2_deck_data is None:
                        raise ValueError(
                            f"Requested replay P2 deck is unavailable: {requested_p2}")
                    if p1_deck_data is None:
                        p1_deck_data = self._matchup_rng.choice(self.decks)
                    if p2_deck_data is None:
                        p2_deck_data = self._matchup_rng.choice(self.decks)
                
                self.current_deck_name_p1 = p1_deck_data.get("name", "P1_Deck")
                self.current_deck_name_p2 = p2_deck_data.get("name", "P2_Deck")
                # Safely copy card lists (ensure they are lists of IDs)
                self.original_p1_deck = p1_deck_data.get("cards", []).copy()
                self.original_p2_deck = p2_deck_data.get("cards", []).copy()

                # --- Initialize GameState ---
                # Create fresh GameState instance
                self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
                
                # Reset GameState (Initializes players, hands, subsystems)
                self.game_state.reset(self.original_p1_deck, self.original_p2_deck, seed)
                gs = self.game_state # Alias
                gs.agent_is_p1 = seat_is_p1
                self._episode_agent_is_p1 = bool(seat_is_p1)
                gs.deck_archetypes = {
                    0: self._stats_archetype_label(self.original_p1_deck),
                    1: self._stats_archetype_label(self.original_p2_deck),
                }
                selected_case = (
                    scheduled_case or curriculum_case or explicit_options)
                self.current_curriculum_stage = selected_case.get("stage")
                self.current_curriculum_stage_index = selected_case.get(
                    "stage_index")
                self.current_matchup_episode_index = selected_case.get(
                    "matchup_episode_index", scheduled_index)
                self.current_agent_deck = (
                    selected_case.get("agent_deck")
                    or (self.current_deck_name_p1 if seat_is_p1
                        else self.current_deck_name_p2))
                self.current_opponent_deck = (
                    selected_case.get("opponent_deck")
                    or (self.current_deck_name_p2 if seat_is_p1
                        else self.current_deck_name_p1))
                # Curriculum stages may shorten episodes to buy more terminal
                # outcomes per timestep.  The environment ceiling still bounds
                # the observation space, so the stage limit never exceeds it.
                stage_max_turns = selected_case.get("max_turns")
                if stage_max_turns:
                    gs.max_turns = min(
                        int(stage_max_turns), int(self.max_turns))

                # --- Link Subsystems to Environment ---
                # 1. External Systems
                self.initialize_strategic_memory()
                if self.strategy_memory: 
                    gs.strategy_memory = self.strategy_memory
                    
                if self.has_stats_tracker and self.stats_tracker:
                    gs.stats_tracker = (
                        self.stats_tracker
                        if self.adaptive_decision_history_enabled else None)
                    self.stats_tracker.current_deck_name_p1 = self.current_deck_name_p1
                    self.stats_tracker.current_deck_name_p2 = self.current_deck_name_p2
                    
                if self.has_card_memory and self.card_memory:
                    gs.card_memory = (
                        self.card_memory
                        if self.adaptive_decision_history_enabled else None)

                # 2. Rules subsystems (Reflect from GameState to Env). The agent
                # layer is NOT reflected: the environment owns it and rebuilds it
                # below via _build_agents().
                subsystems = ['combat_resolver', 'mana_system', 'ability_handler',
                            'layer_system', 'replacement_effects', 'targeting_system']
                            
                for sys_name in subsystems:
                    instance = getattr(gs, sys_name, None)
                    setattr(self, sys_name, instance)
                    
                    # Verify back-link to CURRENT GameState
                    if instance and hasattr(instance, 'game_state') and instance.game_state != gs:
                        instance.game_state = gs
                        logging.debug(f"Relinked {sys_name} to new GameState instance.")

                # --- Agent layer: env is the single owner; rebuild for the fresh GameState ---
                self._build_agents()

                # --- Final Setup ---
                # Generate initial action mask for the Mulligan phase
                try:
                    self.current_valid_actions = self.action_mask()
                except Exception as mask_e:
                    logging.error(f"Error generating initial action mask: {mask_e}")
                    self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                    self.current_valid_actions[12] = True # Concede as failsafe

                # Get initial observation
                obs = self._get_obs_safe()

                # Prepare Info
                info = {
                    "action_mask": self.current_valid_actions.astype(bool),
                    "initial_state": True,
                    "mulligan_active": gs.mulligan_in_progress
                }
                info.update(self._episode_metadata())

                self._successful_reset_count += 1
                if curriculum_case is not None:
                    self.curriculum_scheduler.commit(
                        curriculum_case["stage_index"])
                if scheduled_case is not None:
                    self._episode_schedule_index += 1
                logging.info(f"Environment {env_id} reset complete. P1: {self.current_deck_name_p1} vs P2: {self.current_deck_name_p2}")
                return obs, info

            except Exception as e:
                # --- Critical Error Fallback ---
                logging.critical(f"CRITICAL error during environment reset: {str(e)}", exc_info=True)
                return self._emergency_fallback_reset()

    def _emergency_fallback_reset(self):
        """Provides a minimal valid state if the main reset fails."""
        try:
            logging.warning("Attempting emergency fallback reset...")
            self._reset_fallback_active = True
            # Create minimal GameState
            self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
            
            # Init minimal players
            dummy_card_id = next(iter(self.card_db.keys())) if self.card_db else "dummy"
            self.game_state.reset([dummy_card_id]*60, [dummy_card_id]*60)
            self.game_state.agent_is_p1 = self.initial_agent_is_p1
            self._episode_agent_is_p1 = self.initial_agent_is_p1

            # A fallback is still a new episode boundary. Never expose action,
            # reward, or phase history retained from the failed prior reset.
            self.last_n_actions = np.full(
                self.action_memory_size, -1, dtype=np.int32)
            self.last_n_rewards = np.zeros(
                self.action_memory_size, dtype=np.float32)
            self.opponent_last_n_actions = np.full(
                self.action_memory_size, -1, dtype=np.int32)
            self.opponent_last_n_rewards = np.zeros(
                self.action_memory_size, dtype=np.float32)
            self._observed_phase_history = []
            if hasattr(self, '_phase_history_counts'):
                self._phase_history_counts = defaultdict(int)
            if hasattr(self, '_last_phase_progressed'):
                self._last_phase_progressed = -1
            if hasattr(self, '_phase_stuck_count'):
                self._phase_stuck_count = 0

            # Never carry an agent-layer object or deck-derived observer cache
            # across a failed reset. Rebuild every reference against the fresh
            # emergency GameState before constructing its observation.
            self.action_handler = None
            self.card_evaluator = None
            self.combat_resolver = None
            self.strategic_planner = None
            self.current_analysis = None
            self._observer_strategy_profiles = {}

            # Set pointers
            self.game_state.turn = 1
            self.game_state.phase = self.game_state.PHASE_MAIN_PRECOMBAT
            self.game_state.mulligan_in_progress = False
            self._build_agents()
            if self.action_handler is None:
                raise RuntimeError(
                    "Emergency reset could not rebuild ActionHandler")
            
            # Basic Mask (Pass/Concede)
            self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            self.current_valid_actions[11] = True # Pass
            self.current_valid_actions[12] = True # Concede
            
            obs = self._get_obs_safe()
            info = {"action_mask": self.current_valid_actions.astype(bool), "error_reset": True}
            return obs, info
            
        except Exception as fallback_e:
            logging.critical(f"FALLBACK RESET FAILED: {fallback_e}", exc_info=True)
            raise fallback_e
        
    def _derive_midgame_position(self, winner_is_p1, final_turn):
        """Classify the winner's mid-game position from per-turn life totals.

        Uses the last life totals observed on or before the middle turn of
        the game, so the bucket answers "did the winner snowball or come
        from behind" rather than the trivial "the winner ended ahead".
        Returns one of the DeckStatsTracker GamePosition values.
        """
        history = getattr(self, '_life_totals_by_turn', None) or {}
        if not history:
            return "parity"
        mid_turn = max(1, int(final_turn) // 2)
        eligible = [turn for turn in history if turn <= mid_turn]
        snapshot_turn = max(eligible) if eligible else min(history)
        p1_life, p2_life = history[snapshot_turn]
        winner_lead = (p1_life - p2_life) if winner_is_p1 \
            else (p2_life - p1_life)
        if winner_lead >= self.MIDGAME_POSITION_LIFE_MARGIN:
            return "ahead"
        if winner_lead <= -self.MIDGAME_POSITION_LIFE_MARGIN:
            return "behind"
        return "parity"

    def ensure_game_result_recorded(self, forced_result=None):
        """Make sure game result is recorded if it hasn't been already (Added None checks)."""
        if getattr(self, '_game_result_recorded', False):
            self._ensure_stats_artifacts_written()
            return  # Analytics writers were already claimed for this game.

        gs = self.game_state
        # Ensure players exist
        if not hasattr(gs, 'p1') or not hasattr(gs, 'p2') or gs.p1 is None or gs.p2 is None:
            logging.error("Cannot record game result: Player data missing or not initialized.")
            return

        # Set _game_result attribute based on game state or forced result
        is_p1_winner = False
        winner_life = 0
        is_draw = False

        if forced_result == "error":
            logging.info("Game ended due to error.")
            is_draw = True # Treat errors as draws? Or a separate category? Draw is safer for stats.
            self._game_result = "error" # Store specific result string
            gs.terminal_reason = "error"
        elif forced_result == "invalid_limit":
             logging.info("Game ended due to invalid action limit.")
             is_draw = True
             self._game_result = "invalid_limit"
             gs.terminal_reason = "invalid_action_limit"
        elif (isinstance(forced_result, str)
              and forced_result.startswith(("error_", "invalid_"))):
            # Safety failures are not competitive draws, but the stats writer
            # still needs a stable result row paired with terminal_reason.
            is_draw = True
            self._game_result = forced_result
        elif forced_result in ("win", "loss", "draw"):
            # Result already adjudicated by the caller (e.g., turn-limit life
            # comparison in _check_game_end_conditions). Trust it: the flag-based
            # inference below cannot see turn-limit adjudication.
            self._game_result = forced_result
            if forced_result == "draw":
                is_draw = True
            else:
                agent_won = (forced_result == "win")
                is_p1_winner = (gs.agent_is_p1 == agent_won)
                winner = gs.p1 if is_p1_winner else gs.p2
                winner_life = winner.get("life", 0)
        else:
            # Determine the winner based on game state (use .get with defaults)
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1

            my_lost = me.get("lost_game", False) or me.get("life", 20) <= 0 or me.get("attempted_draw_from_empty", False) or me.get("poison_counters", 0) >= 10
            opp_lost = opp.get("lost_game", False) or opp.get("life", 20) <= 0 or opp.get("attempted_draw_from_empty", False) or opp.get("poison_counters", 0) >= 10

            if my_lost and opp_lost: # Both lost simultaneously
                is_draw = True; winner_life = me.get("life", 0); self._game_result = "draw_both_loss"
            elif my_lost:
                is_p1_winner = not gs.agent_is_p1; winner_life = opp.get("life", 0); self._game_result = "loss"
            elif opp_lost:
                is_p1_winner = gs.agent_is_p1; winner_life = me.get("life", 0); self._game_result = "win"
            elif me.get("won_game", False): # Explicit win flag
                 is_p1_winner = gs.agent_is_p1; winner_life = me.get("life", 0); self._game_result = "win"
            elif opp.get("won_game", False):
                 is_p1_winner = not gs.agent_is_p1; winner_life = opp.get("life", 0); self._game_result = "loss"
            elif me.get("game_draw", False) or opp.get("game_draw", False):
                is_draw = True; winner_life = me.get("life", 0); self._game_result = "draw"
            elif gs.turn > gs.max_turns:
                my_final_life = me.get("life", 0); opp_final_life = opp.get("life", 0)
                if my_final_life > opp_final_life:
                    is_p1_winner = gs.agent_is_p1; winner_life = my_final_life; self._game_result = "win_turn_limit"
                elif opp_final_life > my_final_life:
                    is_p1_winner = not gs.agent_is_p1; winner_life = opp_final_life; self._game_result = "loss_turn_limit"
                else: is_draw = True; winner_life = my_final_life; self._game_result = "draw_turn_limit"
            else:
                # No definitive end condition met - DO NOT RECORD yet.
                logging.debug("ensure_game_result_recorded called but no definitive game end condition met. Waiting.")
                return # Do not proceed with recording

        if not getattr(gs, 'terminal_reason', None):
            gs.terminal_reason = self._terminal_reason(
                {"game_result": self._game_result})

        # Opt-in matchup weighting: feed the agent's decisive-win outcome back
        # to the scheduler so future resets oversample the decks the agent is
        # losing with. Only a clean "win" counts as decisive; turn-limit and
        # draw outcomes leave the deck's win rate (and thus its weight) low.
        if (getattr(self, 'matchup_weighting', False)
                and getattr(self, 'curriculum_scheduler', None) is not None
                and not getattr(self, '_matchup_result_recorded', False)):
            self._matchup_result_recorded = True
            self.curriculum_scheduler.record_agent_result(
                getattr(self, 'current_agent_deck', None),
                self._game_result == "win")

        # Shared by the aggregate and card-memory writers. Keeping these
        # outside either optional subsystem also lets CardMemory operate when
        # aggregate statistics are unavailable.
        original_p1_deck = getattr(self, 'original_p1_deck', [])
        original_p2_deck = getattr(self, 'original_p2_deck', [])
        p1_name = getattr(self, 'current_deck_name_p1', "Unknown_P1")
        p2_name = getattr(self, 'current_deck_name_p2', "Unknown_P2")
        deck_archetypes = getattr(gs, 'deck_archetypes', {}) or {}
        p1_archetype = (
            deck_archetypes.get(0)
            or self._stats_archetype_label(original_p1_deck))
        p2_archetype = (
            deck_archetypes.get(1)
            or self._stats_archetype_label(original_p2_deck))

        # Claim each optional writer before invoking it. A failed or partial
        # writer must not count the same terminal game again on a later ensure.
        stats_available = bool(
            getattr(self, 'has_stats_tracker', False)
            and getattr(self, 'stats_tracker', None))
        card_memory_available = bool(
            getattr(self, 'has_card_memory', False)
            and getattr(self, 'card_memory', None))

        # --- Record the game result ---
        if (stats_available and not getattr(
                self, '_stats_result_record_attempted', False)):
            self._stats_result_record_attempted = True
            self._stats_result_record_accepted = False
            self._stats_result_flush_succeeded = None
            try:
                # Prepare arguments based on win/loss/draw
                if is_draw:
                    winner_deck_list, loser_deck_list = original_p1_deck, original_p2_deck
                    winner_name, loser_name = p1_name, p2_name
                    winner_archetype, loser_archetype = (
                        p1_archetype, p2_archetype)
                else:
                    winner_deck_list = original_p1_deck if is_p1_winner else original_p2_deck
                    loser_deck_list = original_p2_deck if is_p1_winner else original_p1_deck
                    winner_name = p1_name if is_p1_winner else p2_name
                    loser_name = p2_name if is_p1_winner else p1_name
                    winner_archetype = (
                        p1_archetype if is_p1_winner else p2_archetype)
                    loser_archetype = (
                        p2_archetype if is_p1_winner else p1_archetype)

                if (winner_deck_list is None or loser_deck_list is None
                        or winner_name is None or loser_name is None):
                    raise ValueError(
                        "Cannot record game result: deck list or name is None")

                _cards_mapped, _history_mapped = self._stats_result_mapped(
                    gs, True if is_draw else is_p1_winner)
                _opening_mapped, _draws_mapped, _mulligans_mapped = (
                    self._stats_telemetry_mapped(
                        gs, True if is_draw else is_p1_winner))
                stats_result = self.stats_tracker.record_game(
                    winner_deck=winner_deck_list,
                    loser_deck=loser_deck_list,
                    card_db=self.card_db,
                    turn_count=gs.turn,
                    winner_life=winner_life,
                    winner_deck_name=winner_name,
                    loser_deck_name=loser_name,
                    cards_played=_cards_mapped,
                    play_history=_history_mapped,
                    opening_hands=_opening_mapped,
                    draw_history=_draws_mapped,
                    mulligan_data=_mulligans_mapped,
                    # GameState always designates P1 as the starting player.
                    # Winner/loser slots above may swap P1 and P2.
                    play_order={
                        "first_player": "winner"
                        if (is_draw or is_p1_winner) else "loser"
                    },
                    # Without this the tracker's parity default left every
                    # ahead/behind bucket empty (0 of 960 card files after
                    # round-7.94). The winner slot is P1 in a draw, matching
                    # the winner/loser mapping above.
                    game_state=self._derive_midgame_position(
                        True if is_draw else is_p1_winner, gs.turn),
                    is_draw=is_draw,
                    winner_archetype=winner_archetype,
                    loser_archetype=loser_archetype,
                )
                self._stats_result_record_accepted = (
                    stats_result is not False)
                self._stats_result_flush_succeeded = getattr(
                    self.stats_tracker,
                    '_last_record_flush_succeeded', None)
                if not self._stats_result_record_accepted:
                    logging.error(
                        "DeckStatsTracker rejected the terminal game record")
                elif self._stats_result_flush_succeeded is False:
                    logging.error(
                        "DeckStatsTracker accepted the terminal game, but its "
                        "scheduled flush failed and remains queued")
            except Exception as stat_e:
                 logging.error(f"Error during stats_tracker.record_game: {stat_e}", exc_info=True)

        # Record cards to card memory system
        if (card_memory_available and not getattr(
                self, '_card_memory_result_record_attempted', False)):
            self._card_memory_result_record_attempted = True
            self._card_memory_result_record_accepted = False
            try:
                memory_results = []
                for player_idx, player_deck, player_archetype in (
                        (0, original_p1_deck, p1_archetype),
                        (1, original_p2_deck, p2_archetype)):
                    memory_results.append(self._record_cards_to_memory(
                        player_deck=player_deck,
                        cards_played_data=getattr(
                            gs, 'cards_played', {0: [], 1: []}),
                        player_archetype=player_archetype,
                        opening_hands_data=getattr(gs, 'opening_hands', {}),
                        draw_history_data=getattr(gs, 'draw_history', {}),
                        play_history_data=getattr(gs, 'play_history', {}),
                        is_draw=is_draw,
                        is_win=(
                            not is_draw
                            and is_p1_winner == (player_idx == 0)),
                        player_idx=player_idx,
                    ))
                self._card_memory_result_record_accepted = all(
                    result is not False for result in memory_results)
                if not self._card_memory_result_record_accepted:
                    logging.error(
                        "CardMemory rejected part of the terminal game record")
            except Exception as mem_e:
                 logging.error(f"Error recording cards to memory: {mem_e}", exc_info=True)

        # This is the at-most-once terminal-processing guard. Writer acceptance
        # is tracked separately because neither reducer is transactional and a
        # blind retry after a partial update can duplicate counters.
        self._game_result_recorded = bool(
            (not stats_available or getattr(
                self, '_stats_result_record_attempted', False))
            and (not card_memory_available or getattr(
                self, '_card_memory_result_record_attempted', False)))
        self._game_result_recording_failed = bool(
            (stats_available and not getattr(
                self, '_stats_result_record_accepted', False))
            or (card_memory_available and not getattr(
                self, '_card_memory_result_record_accepted', False)))
        self._game_result_flush_failed = bool(
            stats_available
            and getattr(self, '_stats_result_flush_succeeded', None) is False)
        if self._game_result_recording_failed:
            logging.error(
                "Terminal game analytics were only partially accepted: "
                "deck_stats=%r card_memory=%r",
                getattr(self, '_stats_result_record_accepted', None),
                getattr(self, '_card_memory_result_record_accepted', None))

        # Append provenance immediately. The stats tracker itself may batch its
        # compressed aggregate files for a small, configured episode window.
        if getattr(self, '_game_result_recorded', False):
            self._ensure_stats_artifacts_written()
            try:
                if getattr(self, 'stats_tracker', None):
                    # record_game() owns the tracker's batching cadence. Avoid a
                    # second event-loop/save pass per episode.
                    try:
                        from .card_support import get_manifest
                        get_manifest().persist(getattr(self.stats_tracker, 'base_path', './deck_stats'))
                    except Exception as _mf_e:
                        logging.error(f"Error persisting card support manifest: {_mf_e}")
                # CardMemory batches internally and close() performs the final
                # synchronous flush; do not rewrite its full gzip every game.
            except Exception as save_e:
                logging.error(f"Error persisting stats after game record: {save_e}")
                 
    def _refresh_observer_strategy_profiles(self):
        """Cache deck-derived planner inputs independently for P1 and P2.

        The planner's archetype detector reads its current seat's complete
        hand/library.  A shared learned-seat profile therefore cannot cross an
        observer boundary: doing so encodes the other deck's hidden contents
        into threat and planning features.  Profiles are derived once per
        reset, reduced to the planner's strategy inputs, and copied on use.
        """
        planner = getattr(self, "strategic_planner", None)
        gs = self.game_state
        profiles = {}
        if planner is None:
            self._observer_strategy_profiles = profiles
            return

        strategy_attributes = (
            "strategy_type", "strategy_params",
            "aggression_level", "risk_tolerance")
        original_perspective = bool(getattr(gs, "agent_is_p1", True))
        original_values = {
            attribute: copy.deepcopy(getattr(planner, attribute, None))
            for attribute in strategy_attributes
        }
        original_strategies = copy.deepcopy(
            getattr(planner, "strategies", {}))
        try:
            for seat_is_p1 in (True, False):
                gs.agent_is_p1 = seat_is_p1
                planner._detect_deck_archetype()
                profiles[seat_is_p1] = {
                    attribute: copy.deepcopy(
                        getattr(planner, attribute, None))
                    for attribute in strategy_attributes
                }
        except Exception as error:
            logging.error(
                "Could not derive observer-private strategy profiles: %s",
                error, exc_info=True)
            profiles = {}
        finally:
            gs.agent_is_p1 = original_perspective
            planner.strategies = original_strategies
            for attribute, value in original_values.items():
                setattr(planner, attribute, value)
        self._observer_strategy_profiles = profiles

    def _build_agents(self):
        """Construct and attach the agent layer for the current GameState.

        Single ownership: the environment builds these; GameState only holds
        references. ActionHandler creates/adopts the card evaluator and wires
        combat integration during its own __init__.
        """
        gs = self.game_state
        try:
            self.action_handler = ActionHandler(gs)
            gs.action_handler = self.action_handler
            logging.debug("ActionHandler built and linked to GameState.")
        except Exception as e:
            logging.error(f"Failed to initialize ActionHandler: {e}")
            self.action_handler = None
            gs.action_handler = None

        self.card_evaluator = getattr(gs, 'card_evaluator', None)
        if self.card_evaluator:
            self.card_evaluator.stats_tracker = getattr(gs, 'stats_tracker', None)
            self.card_evaluator.card_memory = getattr(gs, 'card_memory', None)
            enable_diagnostics = getattr(
                self.card_evaluator, "set_diagnostics_enabled", None)
            if callable(enable_diagnostics):
                try:
                    enable_diagnostics(
                        self._evaluation_trace_enabled(), reset=True)
                except Exception as error:
                    logging.warning(
                        "Could not configure evaluator diagnostics: %s", error)
                    self._note_evaluation_capture_error(
                        "configure_evaluator_diagnostics", error,
                        scope="trace")
        self.combat_resolver = getattr(gs, 'combat_resolver', None)

        self.strategic_planner = None
        try:
            self.strategic_planner = MTGStrategicPlanner(gs, self.card_evaluator, self.combat_resolver)
            gs.strategic_planner = self.strategic_planner
            if getattr(gs, 'strategy_memory', None):
                self.strategic_planner.strategy_memory = gs.strategy_memory
            if hasattr(self.strategic_planner, 'init_after_reset'):
                self.strategic_planner.init_after_reset()
            self._refresh_observer_strategy_profiles()
            logging.debug("StrategicPlanner built and linked to GameState.")
        except Exception as e:
            logging.error(f"Failed to initialize StrategicPlanner: {e}")
            self.strategic_planner = None
            self._observer_strategy_profiles = {}
            gs.strategic_planner = None

    def set_agent_version(self, version):
        """Stamp subsequent game records with the agent identity (e.g. run id or
        checkpoint tag). Call via vec_env.env_method("set_agent_version", tag) --
        vec_env.set_attr would set the attribute on the ActionMasker wrapper
        instead of this env, leaving records stamped "unversioned"."""
        self.agent_version = str(version)

    @staticmethod
    def _empty_fidelity_aggregate():
        return {
            "games_recorded": 0,
            "unimplemented_action": 0,
            "unparsed_mana": 0,
            "unparsed_modal": 0,
            "unparsed_effects": 0,
            "effect_continuation_failures": 0,
            "lost_spell_recoveries": 0,
            "unparsed_cards": {},
        }

    @classmethod
    def _apply_fidelity_to_aggregate(cls, aggregate, fidelity):
        """Fold one durable game row into a fidelity aggregate."""
        aggregate["games_recorded"] += 1
        for key in (
                "unimplemented_action", "unparsed_mana", "unparsed_modal",
                "unparsed_effects", "effect_continuation_failures",
                "lost_spell_recoveries"):
            try:
                count = int(fidelity.get(key, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                count = 0
            aggregate[key] += max(0, count)

        unparsed_cards = fidelity.get("unparsed_cards", ())
        if not isinstance(unparsed_cards, (list, tuple, set)):
            unparsed_cards = ()
        for name in unparsed_cards:
            name = str(name)
            aggregate["unparsed_cards"][name] = (
                aggregate["unparsed_cards"].get(name, 0) + 1)

    @staticmethod
    def _atomic_write_text(path, payload):
        """Replace a text file only after its temporary file is durable."""
        path = os.path.abspath(os.fspath(path))
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.", suffix=".tmp",
            dir=directory, text=True)
        try:
            handle = os.fdopen(
                descriptor, "w", encoding="utf-8", newline="")
            descriptor = None
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    @classmethod
    def _atomic_write_json(cls, path, payload):
        cls._atomic_write_text(
            path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _artifact_log_signature(log_path):
        try:
            stat = os.stat(log_path)
        except FileNotFoundError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    @staticmethod
    def _stats_artifact_lock(base):
        """Serialize commits from env objects sharing one artifact path."""
        key = os.path.abspath(os.fspath(base))
        with _STATS_ARTIFACT_LOCKS_GUARD:
            lock = _STATS_ARTIFACT_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _STATS_ARTIFACT_LOCKS[key] = lock
            return lock

    def _load_stats_artifact_state(self, base):
        """Load the JSONL source of truth once and repair a partial tail."""
        base = os.path.abspath(os.fspath(base))
        log_path = os.path.join(base, "game_log.jsonl")
        log_signature = self._artifact_log_signature(log_path)
        states = getattr(self, '_stats_artifact_states', None)
        if not isinstance(states, dict):
            states = {}
            self._stats_artifact_states = states
        cached = states.get(base)
        if (cached is not None
                and cached.get("log_signature") == log_signature):
            return cached

        aggregate = self._empty_fidelity_aggregate()
        game_ids = set()
        entries_by_id = {}
        valid_entries = []
        needs_repair = False
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            if (lines and lines[-1].strip()
                    and not lines[-1].endswith(("\n", "\r"))):
                # Appending to a valid legacy row without a final newline would
                # concatenate two JSON objects and corrupt both records.
                needs_repair = True
            nonempty_indexes = [
                index for index, line in enumerate(lines) if line.strip()]
            last_nonempty = nonempty_indexes[-1] if nonempty_indexes else -1
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    if index != last_nonempty:
                        raise ValueError(
                            f"Malformed game log row {index + 1}") from error
                    logging.warning(
                        "Discarding incomplete trailing game log row %d from %s",
                        index + 1, log_path)
                    needs_repair = True
                    continue
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"Game log row {index + 1} is not an object")

                game_id = entry.get("game_id")
                if game_id is not None:
                    game_id = str(game_id)
                    previous = entries_by_id.get(game_id)
                    if previous is not None:
                        if previous != entry:
                            raise ValueError(
                                f"Conflicting duplicate game_id {game_id}")
                        needs_repair = True
                        continue
                    game_ids.add(game_id)
                    entries_by_id[game_id] = entry
                valid_entries.append(entry)
                self._apply_fidelity_to_aggregate(
                    aggregate, entry.get("fidelity") or {})

            if needs_repair:
                repaired_payload = "".join(
                    json.dumps(entry, sort_keys=True) + "\n"
                    for entry in valid_entries)
                self._atomic_write_text(log_path, repaired_payload)
                log_signature = self._artifact_log_signature(log_path)

        state = {
            "aggregate": aggregate,
            "game_ids": game_ids,
            "entries_by_id": entries_by_id,
            "log_signature": log_signature,
        }
        states[base] = state
        return state

    def _commit_stats_artifact_entry(self, base, entry, generated_at):
        """Commit one idempotent row and its derived report under a path lock."""
        state = self._load_stats_artifact_state(base)
        game_id = str(entry["game_id"])
        log_path = os.path.join(base, "game_log.jsonl")
        if game_id not in state["game_ids"]:
            try:
                with open(
                        log_path, "a", encoding="utf-8", newline="") as handle:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                # A failed append may nevertheless have reached disk. Force a
                # rescan so the retry can recognize a complete row or repair a
                # partial trailing row instead of blindly appending again.
                self._stats_artifact_states.pop(base, None)
                raise
            state["game_ids"].add(game_id)
            state["entries_by_id"][game_id] = entry
            state["log_signature"] = self._artifact_log_signature(log_path)
            self._apply_fidelity_to_aggregate(
                state["aggregate"], entry.get("fidelity") or {})

        report = {
            **state["aggregate"],
            "unparsed_cards": dict(
                state["aggregate"].get("unparsed_cards", {})),
            "agent_version": getattr(
                self, 'agent_version', "unversioned"),
            "generated_at": generated_at,
        }
        self._atomic_write_json(
            os.path.join(base, "fidelity_report.json"), report)

        aggregate = getattr(self, '_fidelity_agg', None)
        if not isinstance(aggregate, dict):
            aggregate = {}
            self._fidelity_agg = aggregate
        aggregate.clear()
        aggregate.update({
            **state["aggregate"],
            "unparsed_cards": dict(
                state["aggregate"].get("unparsed_cards", {})),
        })

    def _ensure_stats_artifacts_written(self):
        """Write terminal provenance once, while allowing failed I/O to retry."""
        if getattr(self, '_game_logged', False):
            return True
        try:
            self._write_stats_artifacts()
            self._game_logged = True
            self._game_artifact_write_failed = False
            return True
        except Exception as error:
            self._game_artifact_write_failed = True
            logging.error(
                "Error writing game log/fidelity report: %s", error,
                exc_info=True)
            return False

    def _current_fidelity_counters(self):
        """Return the complete fidelity schema at every serialization boundary.

        Restored legacy/partial states can predate newly strict counters.  The
        GameState normalizer is therefore part of the write boundary: omission
        must never be interpreted downstream as a clean zero.
        """
        gs = getattr(self, "game_state", None)
        ensure = getattr(gs, "_ensure_fidelity_counters", None)
        counters = ensure() if callable(ensure) else getattr(
            gs, "fidelity_counters", None)
        return counters if isinstance(counters, dict) else {}

    def _write_stats_artifacts(self):
        """Append this game to the per-game log and refresh the fidelity report.

        These two files are the metadata contract for the downstream deck-builder:
        - game_log.jsonl: one JSON object per recorded game (result, decks, turn
          count, agent_version, per-game fidelity), for weighting and filtering.
        - fidelity_report.json: cumulative counts plus every card name whose text
          the engine could not fully parse -- stats for those cards are unreliable.
        """
        import time as _time
        gs = self.game_state
        base = (
            getattr(getattr(self, 'stats_tracker', None), 'base_path', None)
            or getattr(self, 'deck_stats_path', None)
            or "./deck_stats")
        base = os.path.abspath(os.fspath(base))
        os.makedirs(base, exist_ok=True)

        fc = self._current_fidelity_counters()
        per_game_fidelity = {k: (sorted(v) if isinstance(v, set) else v) for k, v in fc.items()}
        fallback_agent_deck = (
            getattr(self, "current_deck_name_p1", "Unknown_P1")
            if getattr(gs, "agent_is_p1", True)
            else getattr(self, "current_deck_name_p2", "Unknown_P2"))
        fallback_opponent_deck = (
            getattr(self, "current_deck_name_p2", "Unknown_P2")
            if getattr(gs, "agent_is_p1", True)
            else getattr(self, "current_deck_name_p1", "Unknown_P1"))
        episode_metadata = (
            self._episode_metadata()
            if callable(getattr(self, "_episode_metadata", None)) else {
                "curriculum_stage": getattr(
                    self, "current_curriculum_stage", None),
                "curriculum_stage_index": getattr(
                    self, "current_curriculum_stage_index", None),
                "opponent_profile": getattr(
                    self, "active_opponent_profile", "scripted"),
                "agent_deck": getattr(
                    self, "current_agent_deck", None) or fallback_agent_deck,
                "opponent_deck": getattr(
                    self, "current_opponent_deck", None)
                    or fallback_opponent_deck,
                "matchup_episode_index": getattr(
                    self, "current_matchup_episode_index", None),
            })

        pending = getattr(self, '_current_stats_artifact_entry', None)
        if pending is None:
            deck_attempted = getattr(
                self, '_stats_result_record_attempted', False)
            deck_accepted = getattr(
                self, '_stats_result_record_accepted', False)
            deck_flush = getattr(
                self, '_stats_result_flush_succeeded', None)
            memory_attempted = getattr(
                self, '_card_memory_result_record_attempted', False)
            memory_accepted = getattr(
                self, '_card_memory_result_record_accepted', False)
            entry = {
                "schema_version": 2,
                "game_id": uuid.uuid4().hex,
                "ts": _time.time(),
                "result": getattr(self, '_game_result', None),
                "terminal_reason": self._terminal_reason(
                    {"game_result": getattr(self, '_game_result', None)}),
                "turn_count": getattr(gs, 'turn', None),
                "p1_deck": getattr(
                    self, 'current_deck_name_p1', "Unknown_P1"),
                "p2_deck": getattr(
                    self, 'current_deck_name_p2', "Unknown_P2"),
                "agent_is_p1": getattr(gs, 'agent_is_p1', True),
                "agent_version": getattr(
                    self, 'agent_version', "unversioned"),
                # Acceptance and durability are separate: batched reducers can
                # accept a game even when a scheduled disk flush must retry.
                "analytics_recording": {
                    "deck_stats": (
                        "disabled" if not deck_attempted
                        else "accepted" if deck_accepted else "failed"),
                    "card_memory": (
                        "disabled" if not memory_attempted
                        else "accepted" if memory_accepted else "failed"),
                },
                "analytics_persistence_at_record": {
                    "deck_stats": (
                        "disabled" if not deck_attempted
                        else "not_recorded" if not deck_accepted
                        else "persisted" if deck_flush is True
                        else "flush_failed" if deck_flush is False
                        else "deferred"),
                    "card_memory": (
                        "disabled" if not memory_attempted
                        else "deferred" if memory_accepted
                        else "not_recorded"),
                },
                **episode_metadata,
                "fidelity": per_game_fidelity,
            }
            pending = {"base": base, "entry": entry}
            self._current_stats_artifact_entry = pending
        else:
            base = pending["base"]
            entry = pending["entry"]
            os.makedirs(base, exist_ok=True)

        with self._stats_artifact_lock(base):
            self._commit_stats_artifact_entry(
                base, entry, generated_at=_time.time())

    def close(self):
        """Persist all statistics before shutdown so no recorded game is lost."""
        failures = []

        def record_failure(message, error=None):
            detail = f"{message}: {error}" if error is not None else message
            failures.append(detail)
            logging.error("close(): %s", detail)

        try:
            # Eval environments may be constructed and then closed when a
            # training worker fails before the evaluator's first reset.  That
            # is a normal shutdown state, not a missing-player game error.
            gs = getattr(self, 'game_state', None)
            if (gs is not None and gs.p1 is not None and gs.p2 is not None):
                self.ensure_game_result_recorded()
        except Exception as error:
            record_failure("terminal result recording failed", error)
        try:
            if (getattr(self, 'stats_tracker', None)
                    and hasattr(self.stats_tracker, 'save_updates_sync')):
                if self.stats_tracker.save_updates_sync() is False:
                    record_failure("stats tracker reported an incomplete save")
                try:
                    from .card_support import get_manifest
                    get_manifest().persist(getattr(self.stats_tracker, 'base_path', './deck_stats'))
                except Exception as error:
                    record_failure("card support manifest save failed", error)
        except Exception as error:
            record_failure("stats tracker save failed", error)
        try:
            if (getattr(self, 'card_memory', None)
                    and hasattr(self.card_memory, 'save_all_card_data')):
                if self.card_memory.save_all_card_data() is False:
                    record_failure("card memory reported an incomplete save")
        except Exception as error:
            record_failure("card memory save failed", error)
        try:
            memory = getattr(self, 'strategy_memory', None)
            if memory is not None and getattr(memory, 'dirty', False):
                if memory.save_memory() is False:
                    record_failure("strategy memory reported an incomplete save")
        except Exception as error:
            record_failure("strategy memory save failed", error)

        if getattr(self, '_game_result_recording_failed', False):
            record_failure("terminal analytics were not fully accepted")
        if (getattr(self, '_game_result_recorded', False)
                and not getattr(self, '_game_logged', False)
                and not self._ensure_stats_artifacts_written()):
            record_failure("terminal provenance remains pending")

        try:
            super().close()
        except Exception as error:
            record_failure("base environment close failed", error)
        if failures:
            raise RuntimeError(
                "Environment close could not persist all analytics: "
                + "; ".join(failures))

    def _get_strategic_advice(self):
        """
        Get comprehensive strategic advice for the current game state.
        This integrates all the strategic planner capabilities.
        
        Returns:
            dict: Strategic advice and recommendations
        """
        if not hasattr(self, 'strategic_planner'):
            return None
        
        try:
            gs = self.game_state
            advice = {}
            
            # Get game state analysis
            advice["current_analysis"] = self.strategic_planner.analyze_game_state()
            
            # Adapt strategy parameters
            advice["strategy_params"] = self.strategic_planner.adapt_strategy()
            
            # Identify win conditions
            advice["win_conditions"] = self.strategic_planner.identify_win_conditions()
            
            # Get threat assessment
            advice["threats"] = self.strategic_planner.assess_threats()
            
            # Create multi-turn plan
            advice["turn_plans"] = self.strategic_planner.plan_multi_turn_sequence(depth=2)
            
            # Get suggested action
            valid_actions = np.where(self.current_valid_actions)[0]
            advice["recommended_action"] = self.strategic_planner.recommend_action(valid_actions)
            
            # Recommended action details
            if advice["recommended_action"] is not None:
                action_type, param = gs.action_handler.get_action_info(advice["recommended_action"])
                advice["action_details"] = {
                    "type": action_type,
                    "param": param
                }
            
            return advice
        
        except Exception as e:
            logging.error(f"Error getting strategic advice: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
        
    def _check_phase_progress(self):
        """
        Check if the game is potentially stuck in a phase based on action count.
        (Removed time-based check)

        Returns:
            bool: True if phase was forced to progress, False otherwise
        """
        gs = self.game_state

        # Check if we have phase history tracking
        if not hasattr(self, '_phase_history_counts'):
            self._phase_history_counts = defaultdict(int) # Store counts per phase
            self._last_phase_progressed = -1 # Track the last phase we were actually in
            self._phase_stuck_count = 0

        current_phase = gs.phase

        # If phase changed since last check, reset the counter for the new phase
        if current_phase != self._last_phase_progressed:
             self._phase_history_counts[current_phase] = 0
             self._last_phase_progressed = current_phase

        # Increment the counter for the current phase
        self._phase_history_counts[current_phase] += 1

        # Check for phase getting stuck based on action count
        stuck_threshold = 30  # Number of consecutive identical phases actions to consider "stuck"

        if self._phase_history_counts[current_phase] >= stuck_threshold:
            # Phase appears stuck based on count, force progression
            self._phase_stuck_count += 1

            # Log the issue
            logging.warning(f"Game potentially stuck in phase {current_phase} for {self._phase_history_counts[current_phase]} consecutive actions. Forcing progression. (Occurrence #{self._phase_stuck_count})")

            # Force phase transition based on current phase
            forced_phase = self._force_phase_transition(current_phase)

            # Set flag for reward penalty
            gs.progress_was_forced = True

            # Reset counter for the new phase we just forced
            self._phase_history_counts[forced_phase] = 0
            self._last_phase_progressed = forced_phase

            return True

        # No progression was forced
        return False
    
    def _stats_archetype_label(self, deck):
        """Return the one canonical analytics archetype for a deck."""
        identifier = getattr(
            getattr(self, 'stats_tracker', None), 'identify_archetype', None)
        if not callable(identifier):
            # CardMemory-only operation still needs a stable, queryable bucket.
            return "midrange"
        try:
            label = identifier(list(deck or []))
        except Exception as error:
            logging.warning("Could not identify deck archetype: %s", error)
            return "midrange"
        label = getattr(label, 'value', label)
        return str(label or "unknown").strip().lower() or "unknown"

    @staticmethod
    def _player_turn_number(global_turn, player_idx):
        """Translate global alternating turns to a player's received turns."""
        try:
            global_turn = int(global_turn)
        except (TypeError, ValueError, OverflowError):
            return 0
        if global_turn <= 0 or player_idx not in (0, 1):
            return 0
        return ((global_turn + 1) // 2
                if player_idx == 0 else global_turn // 2)

    def _stats_result_mapped(self, gs, is_p1_winner):
        """Map p1/p2-indexed play data into winner/loser order for the stats
        tracker, which reads cards_played index 0 as the WINNER.

        Triage fix (July 2026): raw gs.cards_played ({0: p1, 1: p2}) was passed
        straight through, so card-level win attribution was swapped in every
        game p2 won (~half of all games). Returns (cards_played_mapped,
        play_history_mapped) with play_history keyed 'winner'/'loser'.
        For draws, callers pass is_p1_winner=True (slot order is arbitrary).
        """
        raw_cards = getattr(gs, 'cards_played', {0: [], 1: []}) or {0: [], 1: []}
        raw_hist = getattr(gs, 'play_history', {0: {}, 1: {}}) or {0: {}, 1: {}}
        w, l = (0, 1) if is_p1_winner else (1, 0)
        cards_mapped = {0: list(raw_cards.get(w, [])), 1: list(raw_cards.get(l, []))}
        history_mapped = {"winner": dict(raw_hist.get(w, {})), "loser": dict(raw_hist.get(l, {}))}
        return cards_mapped, history_mapped

    def _stats_telemetry_mapped(self, gs, is_p1_winner):
        """Map opening, draw, and mulligan telemetry into winner/loser order."""
        w, l = (0, 1) if is_p1_winner else (1, 0)
        winner_key, loser_key = f'p{w + 1}', f'p{l + 1}'
        openings = getattr(gs, 'opening_hands', {}) or {}
        draws = getattr(gs, 'draw_history', {}) or {}
        mulligans = getattr(gs, 'mulligan_data', {}) or {}
        canonical = getattr(gs, 'canonical_card_id', lambda card_id: card_id)
        return (
            {
                'winner': [
                    canonical(card_id)
                    for card_id in openings.get(winner_key, [])],
                'loser': [
                    canonical(card_id)
                    for card_id in openings.get(loser_key, [])],
            },
            {
                'winner': {
                    int(turn): [canonical(card_id) for card_id in cards]
                    for turn, cards in draws.get(winner_key, {}).items()
                },
                'loser': {
                    int(turn): [canonical(card_id) for card_id in cards]
                    for turn, cards in draws.get(loser_key, {}).items()
                },
            },
            {
                'winner': int(mulligans.get(winner_key, 0)),
                'loser': int(mulligans.get(loser_key, 0)),
            },
        )

    def _record_cards_to_memory(
            self, player_deck, cards_played_data, player_archetype,
            opening_hands_data, draw_history_data, play_history_data, *,
            is_draw=False, is_win=True, player_idx=0):
        """Record detailed card performance data to the card memory system, handles draw."""
        try:
            # Check if CardMemory system exists AND if the flag is set
            if (not hasattr(self, 'has_card_memory')
                    or not self.has_card_memory or not self.card_memory):
                return False

            player_key = player_idx
            canonical = self.game_state.canonical_card_id
            success = True

            player_played = {
                canonical(card_id)
                for card_id in cards_played_data.get(player_key, [])
            }
            player_opening = {
                canonical(card_id)
                for card_id in opening_hands_data.get(
                    f'p{player_key+1}', [])
            }
            player_drawn = {
                canonical(card_id)
                for cards in draw_history_data.get(
                    f'p{player_key+1}', {}).values()
                for card_id in cards
            }
            player_plays = play_history_data.get(player_key, {})

            player_turn_played = {}
            for turn, cards in player_plays.items():
                player_turn = self._player_turn_number(turn, player_idx)
                if player_turn <= 0:
                    continue
                for card_id in cards:
                    player_turn_played.setdefault(
                        canonical(card_id), player_turn)

            synergy_partners = sorted(
                player_played,
                key=lambda card_id: (type(card_id).__name__, repr(card_id)))

            for card_id in set(canonical(card_id) for card_id in player_deck):
                card = self.game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'name'):
                    success = False
                    continue

                # Registration must precede the first performance update;
                # otherwise CardMemory drops the card's first observed game.
                if self.card_memory.register_card(card_id, card.name, {
                     'cmc': getattr(card, 'cmc', 0),
                     'types': getattr(card, 'card_types', []),
                     'colors': getattr(card, 'colors', []) }) is False:
                    success = False
                    continue
                perf_data = {
                    'is_win': bool(is_win) and not is_draw,
                    'is_draw': is_draw,
                    'was_played': card_id in player_played,
                    'was_drawn': card_id in player_drawn,
                    'turn_played': player_turn_played.get(card_id, 0),
                    'in_opening_hand': card_id in player_opening,
                    'deck_archetype': player_archetype,
                    'synergy_partners': [
                        partner_id for partner_id in synergy_partners
                        if partner_id != card_id
                    ]
                }
                if self.card_memory.update_card_performance(
                        card_id, perf_data) is False:
                    success = False

            return success

        except Exception as e:
            logging.error(f"Error recording cards to memory: {str(e)}")
            import traceback; logging.error(traceback.format_exc())
            return False

    def _force_phase_transition(self, current_phase):
        """
        Force a transition from the current phase to the next logical phase.
        
        Args:
            current_phase: The phase that appears to be stuck
            
        Returns:
            int: The new phase after forced transition
        """
        gs = self.game_state
        
        # Define phase transition mapping - complete mapping for all phases
        phase_transitions = {
            gs.PHASE_UNTAP: gs.PHASE_UPKEEP,
            gs.PHASE_UPKEEP: gs.PHASE_DRAW,
            gs.PHASE_DRAW: gs.PHASE_MAIN_PRECOMBAT,
            gs.PHASE_MAIN_PRECOMBAT: gs.PHASE_BEGINNING_OF_COMBAT,
            gs.PHASE_BEGINNING_OF_COMBAT: gs.PHASE_DECLARE_ATTACKERS,
            gs.PHASE_DECLARE_ATTACKERS: gs.PHASE_DECLARE_BLOCKERS,
            gs.PHASE_DECLARE_BLOCKERS: gs.PHASE_COMBAT_DAMAGE,
            gs.PHASE_COMBAT_DAMAGE: gs.PHASE_END_OF_COMBAT,
            gs.PHASE_END_OF_COMBAT: gs.PHASE_MAIN_POSTCOMBAT,
            gs.PHASE_MAIN_POSTCOMBAT: gs.PHASE_END_STEP,
            gs.PHASE_END_STEP: gs.PHASE_CLEANUP,
            gs.PHASE_PRIORITY: gs.PHASE_MAIN_POSTCOMBAT,  # Handle PRIORITY phase
            gs.PHASE_FIRST_STRIKE_DAMAGE: gs.PHASE_COMBAT_DAMAGE
        }
        
        # Special case for CLEANUP - advance to next turn
        if current_phase == gs.PHASE_CLEANUP:
            # Process end of turn effects
            current_player = gs.p1 if gs.agent_is_p1 else gs.p2
            gs._end_phase(current_player)
            
            # Next turn
            gs.turn += 1
            gs._empty_mana_pools()
            gs.phase = gs.PHASE_UNTAP
            
            logging.info(f"Forced turn advancement to Turn {gs.turn}, Phase UNTAP")
            return gs.PHASE_UNTAP
        
        # Regular phase transition
        if current_phase in phase_transitions:
            new_phase = phase_transitions[current_phase]
            gs._empty_mana_pools()
            gs.phase = new_phase
            
            # Execute any special phase entry logic
            if new_phase == gs.PHASE_UNTAP:
                current_player = gs.p1 if gs.agent_is_p1 else gs.p2
                gs._untap_phase(current_player)
            
            logging.info(f"Forced phase transition: {current_phase} -> {new_phase}")
            return new_phase
        
        # Fallback - just advance to MAIN_POSTCOMBAT as a safe option
        gs._empty_mana_pools()
        gs.phase = gs.PHASE_MAIN_POSTCOMBAT
        logging.warning(f"Unknown phase {current_phase} in force_phase_transition, defaulting to MAIN_POSTCOMBAT")
        return gs.PHASE_MAIN_POSTCOMBAT
    
    def _policy_state_diagnostic(self, *, recent_action=None, valid_mask=None):
        """Return a compact, process-safe policy-boundary state summary."""
        gs = self.game_state

        def player_label(player):
            if player is gs.p1:
                return "p1"
            if player is gs.p2:
                return "p2"
            return None

        choice = (getattr(gs, "targeting_context", None)
                  or getattr(gs, "sacrifice_context", None)
                  or getattr(gs, "choice_context", None)
                  or {})
        diagnostic = {
            "episode_step": int(self.current_step),
            "turn": int(getattr(gs, "turn", -1)),
            "phase": int(getattr(gs, "phase", -1)),
            "phase_name": getattr(gs, "_PHASE_NAMES", {}).get(
                getattr(gs, "phase", -1), "UNKNOWN"),
            "underlying_priority_phase": getattr(
                gs, "previous_priority_phase", None),
            "priority_player": player_label(
                getattr(gs, "priority_player", None)),
            "choice_type": choice.get("type"),
            "choice_player": player_label(
                choice.get("controller") or choice.get("player")),
            "stack_size": len(getattr(gs, "stack", ()) or ()),
            "agent_is_p1": bool(getattr(gs, "agent_is_p1", True)),
            "attacker_count": len(getattr(gs, "current_attackers", ()) or ()),
            "blocker_count": sum(
                len(blockers)
                for blockers in getattr(
                    gs, "current_block_assignments", {}).values()),
        }
        targeting = getattr(gs, "targeting_context", None)
        if targeting:
            source_id = targeting.get("source_id")
            source_card = gs._safe_get_card(source_id)
            raw_targets = {}
            candidates = []
            try:
                raw_targets = gs.targeting_system.get_valid_targets(
                    source_id,
                    targeting.get("controller"),
                    targeting.get("required_type", "target"),
                    effect_text=targeting.get("effect_text"),
                )
                raw_targets = {
                    str(category): sorted(
                        target_ids,
                        key=lambda target_id: (
                            isinstance(target_id, str), target_id),
                    )
                    for category, target_ids in raw_targets.items()
                }
                if self.action_handler:
                    candidates = self.action_handler._get_target_selection_candidates(
                        targeting.get("controller"), targeting)
            except Exception as target_error:
                raw_targets = {"diagnostic_error": str(target_error)}
            diagnostic["targeting"] = {
                "source_id": source_id,
                "source_name": getattr(source_card, "name", None),
                "effect_text": targeting.get("effect_text"),
                "required_type": targeting.get("required_type"),
                "min_targets": int(targeting.get("min_targets", 0)),
                "max_targets": int(targeting.get(
                    "max_targets", targeting.get("required_count", 0))),
                "selected_targets": list(
                    targeting.get("selected_targets", ())),
                "resume_effect": type(targeting.get("resume_effect")).__name__
                if targeting.get("resume_effect") is not None else None,
                "resume_cast": bool(targeting.get("resume_cast")),
                "raw_valid_targets": raw_targets,
                "selection_candidates": list(candidates),
            }
        # Stack provenance is useful for every policy-cycle failure, not only
        # failures that happen to have an active targeting context.
        stack_summary = []
        live_stack = list(getattr(gs, "stack", ()) or ())
        if len(live_stack) > 32:
            stack_indices = list(range(8)) + list(
                range(len(live_stack) - 24, len(live_stack)))
            diagnostic["stack_summary_omitted"] = len(live_stack) - 32
        else:
            stack_indices = list(range(len(live_stack)))
        for stack_index in stack_indices:
            item = live_stack[stack_index]
            if not (isinstance(item, tuple) and len(item) >= 3):
                stack_summary.append({
                    "index": stack_index,
                    "item_type": type(item).__name__,
                })
                continue
            item_context = (
                item[3] if len(item) > 3 and isinstance(item[3], dict)
                else {})
            ability = item_context.get("ability")
            item_card = gs._safe_get_card(item[1])
            stack_summary.append({
                "index": stack_index,
                "item_type": str(item[0]),
                "source_id": item[1],
                "source_name": getattr(item_card, "name", None),
                "context_keys": sorted(str(key) for key in item_context),
                "target_choice_pending": bool(
                    item_context.get("target_choice_pending")),
                "target_instance_id": item_context.get(
                    "target_instance_id"),
                "targeting_text": item_context.get("targeting_text"),
                "effect_text": item_context.get("effect_text"),
                "ability_type": type(ability).__name__
                if ability is not None else None,
                "trigger_condition": getattr(
                    ability, "trigger_condition", None),
                "ability_effect": getattr(ability, "effect", None),
            })
        diagnostic["stack"] = stack_summary
        mask_error = getattr(self.action_handler, "last_mask_error", None)
        if mask_error:
            diagnostic["last_mask_error"] = str(mask_error)
        recent_actions = [
            int(action) for action in self.current_episode_actions[-32:]]
        if recent_action is not None:
            recent_actions = recent_actions[-31:] + [int(recent_action)]
        diagnostic["recent_actions"] = recent_actions
        if valid_mask is not None:
            diagnostic["valid_actions"] = np.flatnonzero(valid_mask).tolist()
        return diagnostic

    def step(self, action_idx, context=None):
        """
        Execute the agent's action, simulate opponent actions until control returns
        or the game ends, and return the next state information. (Corrected Final Mask Generation)
        """
        gs = self.game_state
        action_context = {}
        if context: action_context.update(context)

        # Store initial agent perspective
        initial_agent_is_p1 = gs.agent_is_p1

        # Potential-based shaping is computed from the learned agent's fixed
        # perspective across the complete agent + scripted-opponent transition.
        previous_state_potential = self._calculate_state_potential()
        learned_trace_sequence = None
        strategy_pattern = None
        if self.strategy_memory is not None:
            try:
                strategy_pattern = \
                    self.strategy_memory.extract_strategy_pattern(gs)
            except Exception as memory_error:
                logging.error(
                    "Could not capture pre-action strategy pattern: %s",
                    memory_error)

        # --- Initialize Info Dict ---
        env_info = {
            "action_mask": None, # Will be set at the end or on error
            "game_result": "undetermined",
            "critical_error": False,
            "invalid_action": False,
            "invalid_action_reason": None
        }
        env_info.update(self._episode_metadata())
        if getattr(self, "_reset_fallback_active", False):
            env_info["error_reset"] = True

        try:
            self.current_step += 1 # Increment step counter

            # --- 1 & 2: Action Index and Mask Validation ---
            try:
                 # Regenerate mask FOR THE CURRENT AGENT perspective before validation
                 gs.agent_is_p1 = initial_agent_is_p1 # Ensure perspective is correct
                 current_mask = self.action_mask().astype(bool)
            except Exception as current_mask_e:
                logging.error(f"Error regenerating mask for validation: {current_mask_e}. Using fallback.", exc_info=True)
                current_mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); current_mask[11]=True; current_mask[12]=True

            if not (0 <= action_idx < self.ACTION_SPACE_SIZE):
                logging.error(f"Step {self.current_step}: Action index {action_idx} out of bounds.")
                gs.agent_is_p1 = initial_agent_is_p1 # Ensure perspective before safe obs
                obs = self._get_obs_safe() # Ensure perspective is agent's
                env_info["critical_error"] = True; env_info["error_message"] = "Action index OOB"
                env_info["action_mask"] = current_mask # Provide the mask that was current when error occurred
                return obs, -0.5, False, False, env_info # Fail step

            if not current_mask[action_idx]:
                invalid_reason = self.action_handler.action_reasons.get(action_idx, 'Not Valid / Unknown Reason')
                logging.warning(f"Step {self.current_step}: Invalid action {action_idx} selected (Mask False). Reason: [{invalid_reason}]")
                self.invalid_action_count += 1
                env_info["invalid_action"] = True
                env_info["invalid_action_reason"] = invalid_reason
                env_info["action_mask"] = current_mask # Return the *current* mask when action is invalid
                done, truncated = False, False
                step_reward = -0.1 # Apply penalty
                if self.invalid_action_count >= self.invalid_action_limit:
                     logging.warning(f"Invalid action limit ({self.invalid_action_limit}) reached. Terminating episode.")
                     done, truncated = True, True
                     env_info["game_result"] = "invalid_limit"
                     env_info["terminal_reason"] = "invalid_action_limit"
                     step_reward = self._failure_transition_reward(
                         env_info, previous_state_potential)
                     self.ensure_game_result_recorded(forced_result="invalid_limit") # Record specific result
                gs.agent_is_p1 = initial_agent_is_p1 # Ensure perspective before getting obs
                obs = self._get_obs_safe() # Get current observation
                self._attach_evaluation_terminal_debug(
                    env_info, step_reward, done, truncated)
                return obs, step_reward, done, truncated, env_info

            # Reset counter if action is valid
            self.invalid_action_count = 0

            # --- 3. Execute VALID AGENT's Action using ActionHandler ---
            # Set perspective correctly before applying agent action
            gs.agent_is_p1 = initial_agent_is_p1
            # --- Ensure action handler exists before calling apply_action ---
            if not self.action_handler:
                 logging.critical("ActionHandler is None in env.step before applying action! Cannot proceed.")
                 env_info["critical_error"] = True; env_info["error_message"] = "ActionHandler is None"
                 env_info["game_result"] = "error_action_handler_missing"
                 env_info["terminal_reason"] = "engine_error"
                 env_info["action_mask"] = current_mask
                 gs.agent_is_p1 = initial_agent_is_p1 # Ensure perspective before safe obs
                 obs = self._get_obs_safe()
                 self.ensure_game_result_recorded(forced_result="error")
                 step_reward = self._failure_transition_reward(
                      env_info, previous_state_potential)
                 self._attach_evaluation_terminal_debug(
                     env_info, step_reward, True, False)
                 return obs, step_reward, True, False, env_info # done=True

            learned_pre_state = (
                self._safe_evaluation_state_snapshot(
                    "learned_pre_state", valid_mask=current_mask)
                if self._evaluation_trace_enabled() else None)
            reward, done, truncated, handler_info = self.action_handler.apply_action(action_idx, context=action_context)
            if learned_pre_state is not None:
                learned_trace_sequence = self._record_evaluation_atomic_action(
                    actor="learned",
                    actor_is_p1=bool(initial_agent_is_p1),
                    action_idx=action_idx,
                    context=action_context,
                    pre_state=learned_pre_state,
                    post_state=self._safe_evaluation_state_snapshot(
                        "learned_post_state"),
                )
            env_info.update(handler_info) # Merge info

            if handler_info.get("execution_failed"):
                diagnostic = self._policy_state_diagnostic(
                    recent_action=action_idx, valid_mask=current_mask)
                diagnostic["failed_action"] = handler_info.get("failed_action")
                diagnostic["handler_error"] = handler_info.get("handler_error")
                env_info["policy_state"] = diagnostic
                env_info["error_message"] = (
                    f"{handler_info.get('error_message', 'Mask-valid action failed')}; "
                    f"state={diagnostic}")
                try:
                    env_info["failure_replay_path"] = \
                        self._persist_failure_replay(
                            diagnostic, action_idx,
                            handler_info.get("failed_action", {}).get(
                                "context", action_context))
                except Exception as replay_error:
                    logging.error(
                        "Could not persist execution-failure replay: %s",
                        replay_error)

            # --- 4. Opponent Simulation Loop (Only if agent's action was valid and game not over) ---
            opponent_loop_count = 0
            max_opponent_loops = 50 # Safety break
            while (not done and not truncated
                    and not env_info.get("execution_failed")
                    and opponent_loop_count < max_opponent_loops):
                opponent_loop_count += 1
                # --- a. Check if opponent needs to act ---
                opponent_player, opponent_context = self._opponent_needs_to_act()
                if not opponent_player:
                    # logging.debug(f"Opponent loop {opponent_loop_count}: No opponent action needed.")
                    break # Agent needs to act next, exit loop

                # --- b. Get Opponent's Valid Actions ---
                try:
                     opponent_mask = self.action_mask_for(opponent_player)
                except Exception as opp_mask_e:
                     gs.agent_is_p1 = initial_agent_is_p1
                     raise RuntimeError(
                         "Opponent action mask generation failed; refusing "
                         "to skip the opponent turn") from opp_mask_e
                # --- PERSPECTIVE RESTORED LATER ---

                # --- c. Choose Scripted Opponent Action ---
                opponent_action_idx, opponent_action_context = self._get_opponent_policy_action(
                    opponent_player, opponent_mask, opponent_context)
                if opponent_action_idx is None:
                     logging.warning(f"Scripted opponent couldn't choose valid action. Breaking opponent loop.")
                     gs.agent_is_p1 = initial_agent_is_p1 # Restore perspective before breaking
                     break

                # --- d. Apply Opponent's Action using ActionHandler ---
                # Apply action from the OPPONENT'S perspective (mask was generated above)
                # ---> Ensure action handler exists before opponent action <---
                if not self.action_handler:
                     logging.critical("ActionHandler became None before opponent action! Cannot proceed.")
                     env_info["critical_error"] = True; env_info["error_message"] = "ActionHandler became None mid-step"
                     done=True; truncated=True
                     gs.agent_is_p1 = initial_agent_is_p1 # Restore perspective
                     break # Exit opponent loop

                # Action application is the only portion that must execute as
                # the opponent. Observation and prediction establish their
                # own state-safe perspective and never depend on this mutation.
                gs.agent_is_p1 = (opponent_player is gs.p1)
                installed_opponent_mask = \
                    self._strict_action_mask_for_current_perspective(
                        "Opponent")
                if not np.array_equal(installed_opponent_mask, opponent_mask):
                    raise RuntimeError(
                        "Opponent legal mask changed between prediction and "
                        "action application")
                # ActionHandler.apply_action validates its own cache before
                # dispatch. The state-safe prediction boundary intentionally
                # restored the learned seat's cache, so explicitly install the
                # matching opponent mask and contexts for this atomic action.
                self.action_handler.current_valid_actions = (
                    installed_opponent_mask.copy())
                opponent_pre_state = (
                    self._safe_evaluation_state_snapshot(
                        "opponent_pre_state", valid_mask=opponent_mask)
                    if self._evaluation_trace_enabled() else None)
                opponent_reward, opp_done, opp_truncated, opp_handler_info = self.action_handler.apply_action(opponent_action_idx, context=opponent_action_context)
                if not opp_handler_info.get("execution_failed"):
                    self.opponent_last_n_actions = np.roll(
                        self.opponent_last_n_actions, 1)
                    self.opponent_last_n_actions[0] = opponent_action_idx
                    self.opponent_last_n_rewards = np.roll(
                        self.opponent_last_n_rewards, 1)
                    # Match the learned history's action-component scale.
                    # State-potential/time/terminal shaping spans the complete
                    # external transition and cannot be attributed to one
                    # atomic opponent action without leaking learned telemetry.
                    self.opponent_last_n_rewards[0] = (
                        self.action_reward_scale
                        * float(opponent_reward or 0.0))
                if opponent_pre_state is not None:
                    self._record_evaluation_atomic_action(
                        actor="opponent",
                        actor_is_p1=bool(opponent_player is gs.p1),
                        action_idx=opponent_action_idx,
                        context=opponent_action_context,
                        pre_state=opponent_pre_state,
                        post_state=self._safe_evaluation_state_snapshot(
                            "opponent_post_state"),
                    )

                # Check if the opponent's action ended the game
                done = done or opp_done # Update global done flag
                truncated = truncated or opp_truncated # Update global truncated flag
                if opp_done:
                    # ActionHandler reports win/loss from the acting seat. The
                    # scripted opponent temporarily owns ``agent_is_p1`` here,
                    # so translate its result before it reaches PPO.
                    env_info["game_result"] = self._result_for_fixed_agent(
                        opp_handler_info.get("game_result", "undetermined"),
                        actor_is_p1=(opponent_player is gs.p1),
                        agent_is_p1=initial_agent_is_p1,
                    )
                if opp_handler_info.get("critical_error"): # Propagate critical errors
                    env_info["critical_error"] = True
                    env_info["error_message"] = opp_handler_info.get("error_message", "Opponent action critical error")
                    logging.error(f"Critical error during opponent action {opponent_action_idx}. Ending step.")
                    done=True; truncated=True # Force end on opponent error
                    gs.agent_is_p1 = initial_agent_is_p1 # Restore perspective before breaking
                    break # Exit opponent loop on critical error
                if opp_handler_info.get("execution_failed"):
                    # A scripted action came from the generated legal mask, so
                    # rejection is an engine-contract failure. Surface it to
                    # strict callers (fixture harvests/fuzzers) instead of
                    # silently continuing from a potentially partial mutation.
                    env_info["execution_failed"] = True
                    env_info["opponent_execution_failed"] = True
                    diagnostic = self._policy_state_diagnostic(
                        recent_action=opponent_action_idx,
                        valid_mask=opponent_mask)
                    diagnostic["actor"] = "opponent"
                    diagnostic["failed_action"] = opp_handler_info.get(
                        "failed_action")
                    diagnostic["handler_error"] = opp_handler_info.get(
                        "handler_error")
                    env_info["policy_state"] = diagnostic
                    base_error = opp_handler_info.get(
                        "error_message",
                        f"Mask-valid opponent action {opponent_action_idx} failed")
                    env_info["error_message"] = (
                        f"{base_error}; state={diagnostic}")
                    logging.error(env_info["error_message"])
                    gs.agent_is_p1 = initial_agent_is_p1
                    try:
                        # Replay files contain agent decisions; replaying the
                        # current agent action deterministically re-enters this
                        # scripted-opponent loop and reproduces its failed
                        # action.  Persist only after restoring the recorded
                        # agent seat in the payload.
                        env_info["failure_replay_path"] = \
                            self._persist_failure_replay(
                                diagnostic, action_idx, action_context)
                    except Exception as replay_error:
                        logging.error(
                            "Could not persist opponent execution-failure "
                            "replay: %s", replay_error)
                    break

                # Restore perspective AFTER applying opponent action successfully
                gs.agent_is_p1 = initial_agent_is_p1

            # Safety break check for loop
            if opponent_loop_count >= max_opponent_loops:
                 logging.error(f"Opponent simulation loop exceeded max iterations ({max_opponent_loops}). Terminating episode.")
                 done = True; truncated = True # Mark as truncated due to loop limit
                 env_info["game_result"] = "error_opponent_loop" # Set specific result string
                 gs.agent_is_p1 = initial_agent_is_p1 # Restore perspective before exiting

            # ``max_episode_steps`` existed since the environment was created,
            # but was never enforced. A mask-valid policy cycle could therefore
            # block periodic evaluation forever without reaching the turn cap.
            if (not done and not truncated
                    and self.current_step >= self.max_episode_steps):
                diagnostic = self._policy_state_diagnostic(
                    recent_action=action_idx, valid_mask=current_mask)
                logging.error(
                    "Episode exceeded the %s-step safety limit. State: %s",
                    self.max_episode_steps, diagnostic)
                truncated = True
                env_info["episode_step_limit"] = True
                env_info["game_result"] = "error_episode_step_limit"
                gs.terminal_reason = "episode_step_limit"
                env_info["policy_state"] = diagnostic
                env_info["error_message"] = (
                    f"Episode exceeded {self.max_episode_steps} steps: "
                    f"{diagnostic}")
                try:
                    env_info["failure_replay_path"] = \
                        self._persist_failure_replay(
                            diagnostic, action_idx, action_context)
                except Exception as replay_error:
                    logging.error(
                        "Could not persist step-limit replay: %s", replay_error)


            # --- 5. Check Final Game End Conditions ---
            # Everything below this boundary is learned-agent telemetry.
            gs.agent_is_p1 = initial_agent_is_p1
            # Keep the last life totals seen on each turn so the terminal
            # stats record can classify the winner's mid-game position
            # (ahead/parity/behind) instead of the tracker's parity default.
            try:
                self._life_totals_by_turn[int(gs.turn)] = (
                    int(gs.p1.get("life", 0)), int(gs.p2.get("life", 0)))
            except (TypeError, ValueError, AttributeError):
                pass
            if not done:
                 game_ended_by_check = self._check_game_end_conditions(env_info)
                 done = done or game_ended_by_check
            if done:
                env_info["game_result"] = self._canonical_terminal_result(
                    env_info.get("game_result", "undetermined"),
                    agent_is_p1=initial_agent_is_p1,
                )

            # Direct action heuristics are retained as a weak tie-breaker, not
            # the main objective. Their previous scale made longer games pay
            # more regardless of result and produced unnecessarily large value
            # targets for the critic.
            raw_action_reward = float(reward)
            action_reward = self.action_reward_scale * raw_action_reward

            current_state_potential = self._calculate_state_potential()
            state_change_reward = self._state_potential_reward(
                previous_state_potential,
                current_state_potential,
                terminal=bool(done or truncated),
            )
            # Every decision costs a sliver of reward: given equal outcomes,
            # shorter games are strictly better, and turn-limit stalls (the
            # longest episodes) accrue the most.  Small enough that no
            # accumulated total can reorder win/draw/timeout/loss terminals.
            time_cost_reward = -self.time_cost_per_step
            step_reward = action_reward + state_change_reward \
                + time_cost_reward
            env_info["state_change_reward"] = state_change_reward
            env_info["state_potential"] = current_state_potential
            # Compatibility names for analysis scripts written against the old
            # board-potential implementation. These are diagnostics, not extra
            # reward components.
            env_info["board_state_reward"] = state_change_reward
            env_info["board_state_potential"] = current_state_potential

            terminal_reward = 0.0
            if done or truncated:
                terminal_reason = self._terminal_reason(env_info)
                env_info["terminal_reason"] = terminal_reason
                result = env_info.get("game_result", "draw")
                terminal_reward = self._terminal_outcome_reward(
                    terminal_reason, result)
                step_reward += terminal_reward

            env_info["reward_components"] = {
                "action": float(action_reward),
                "state_change": float(state_change_reward),
                "time": float(time_cost_reward),
                "terminal": float(terminal_reward),
            }
            env_info["reward_diagnostics"] = {
                "action_raw": raw_action_reward,
                "state_potential": float(current_state_potential),
                "state_potential_previous": float(previous_state_potential),
            }
            env_info["reward_contract"] = self.REWARD_CONTRACT_VERSION
            self._attach_evaluation_transition_reward(
                learned_trace_sequence, step_reward, env_info)

            # --- 7. Get Final Observation and Mask for the AGENT ---
            # *** Ensure perspective is set to agent BEFORE getting obs and mask ***
            gs.agent_is_p1 = initial_agent_is_p1
            # The returned next-state observation must include the action and
            # reward that produced it.  Updating these after observation building
            # left policy history permanently one transition behind.
            if hasattr(self, 'last_n_actions'):
                self.last_n_actions = np.roll(self.last_n_actions, 1)
                self.last_n_actions[0] = action_idx
            if hasattr(self, 'last_n_rewards'):
                self.last_n_rewards = np.roll(self.last_n_rewards, 1)
                self.last_n_rewards[0] = step_reward
            obs = self._get_obs_safe()
            # *** Regenerate mask AFTER perspective is confirmed ***
            # ---> ADDED LOGGING <---
            prio_player_before_final_mask = getattr(getattr(gs, 'priority_player', None), 'name', 'None')
            current_phase_name = gs._PHASE_NAMES.get(gs.phase, gs.phase) if gs and hasattr(gs, '_PHASE_NAMES') else "N/A" # Safe phase name access
            logging.debug(f"Env Step End: BEFORE final agent mask gen. Prio='{prio_player_before_final_mask}', Phase={current_phase_name}")
            # ---> END ADDED LOGGING <---
            try:
                final_agent_mask = self.action_mask().astype(bool)
            except Exception as final_mask_e:
                 logging.error(f"Error generating final agent mask: {final_mask_e}. Using fallback.", exc_info=True)
                 final_agent_mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); final_agent_mask[11]=True; final_agent_mask[12]=True
            env_info["action_mask"] = final_agent_mask # This mask is for the NEXT agent action

            # ---> ADDED LOGGING <---
            prio_player_after_final_mask = getattr(getattr(gs, 'priority_player', None), 'name', 'None')
            logging.debug(f"Env Step End: AFTER final agent mask gen. Prio='{prio_player_after_final_mask}'")
            # ---> END ADDED LOGGING <---

            # --- 8. Record History and Finalize ---
            if hasattr(self, 'current_episode_actions'): self.current_episode_actions.append(action_idx)
            self._record_learned_replay_action(
                action_idx, action_context,
                trace_sequence=learned_trace_sequence,
                step_reward=step_reward,
                done=done,
                truncated=truncated,
            )
            if hasattr(self, 'episode_rewards'): self.episode_rewards.append(step_reward)
            if self.strategy_memory is not None and strategy_pattern is not None:
                try:
                    self.strategy_memory.update_strategy(
                        strategy_pattern, step_reward, action_idx=action_idx)
                    if done or truncated:
                        self.strategy_memory.save_memory()
                except Exception as memory_error:
                    logging.error(
                        "Could not update strategy memory: %s", memory_error)

            # Record game result if ended
            if done and not getattr(self, '_game_result_recorded', False):
                final_result_string = env_info.get("game_result", "undetermined")
                self.ensure_game_result_recorded(forced_result=final_result_string)
                env_info["game_result"] = getattr(self, '_game_result', 'undetermined')

            # Log step summary
            if self.detailed_logging or DEBUG_ACTION_STEPS:
                action_type_log, param_log = self.action_handler.get_action_info(action_idx) if self.action_handler else ("N/A", "N/A")
                logging.info(f"--- Env Step {self.current_step} COMPLETE ---")
                logging.info(f"Agent Action: {action_idx} ({action_type_log}({param_log}))")
                logging.info(f"Opponent Loops: {opponent_loop_count}")
                final_prio_name = getattr(getattr(gs,'priority_player',None), 'name', 'None') if gs else 'None'
                final_phase_name = gs._PHASE_NAMES.get(gs.phase, gs.phase) if gs and hasattr(gs, '_PHASE_NAMES') else 'N/A' # Safe access
                logging.info(f"Final State: Turn {gs.turn if gs else 'N/A'}, Phase {final_phase_name}, Prio {final_prio_name}")
                logging.info(f"Returned Reward: {step_reward:.4f}, Done: {done}, Truncated: {truncated}")

            # BUGFIX: game results were never recorded on normal endings --
            # ensure_game_result_recorded existed but nothing called it, so the
            # stats pipeline (the whole point of the simulator) stayed empty.
            if done or truncated:
                try:
                    self.ensure_game_result_recorded(forced_result=env_info.get("game_result"))
                except Exception as rec_e:
                    logging.error(f"Failed to record game result: {rec_e}")
                fc = self._current_fidelity_counters()
                if fc:
                    env_info["fidelity"] = {k: (sorted(v) if isinstance(v, set) else v)
                                            for k, v in fc.items()}

            env_info.setdefault(
                "policy_state", self._policy_state_diagnostic())
            self._attach_evaluation_terminal_debug(
                env_info, step_reward, done, truncated)

            return obs, step_reward, done, truncated, env_info

        except Exception as e:
            # Critical error during step execution
            logging.critical(f"CRITICAL error in environment step {self.current_step} (Agent Action {action_idx}): {str(e)}", exc_info=True)
            gs.agent_is_p1 = initial_agent_is_p1 # Ensure perspective is agent before fallback obs
            obs = self._get_obs_safe()
            fallback_mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); fallback_mask[11] = True; fallback_mask[12] = True
            final_info = {
                "action_mask": fallback_mask,
                "critical_error": True,
                "error_message": f"Unhandled Exception in Environment Step: {str(e)}",
                "game_result": "error",
                "turn": gs.turn if hasattr(self,'game_state') and gs else -1,
                "phase": gs.phase if hasattr(self,'game_state') and gs else -1,
                "terminal_reason": "engine_error",
            }
            final_info.update(self._episode_metadata())
            self.ensure_game_result_recorded(forced_result="error") # Record error result
            step_reward = self._failure_transition_reward(
                final_info, previous_state_potential)
            self._attach_evaluation_terminal_debug(
                final_info, step_reward, True, False)
            return obs, step_reward, True, False, final_info # done=True, truncated=False

    # --- ADDED Helper Methods for Opponent Simulation ---

    def _opponent_needs_to_act(self):
            """Checks if the game state requires the opponent to act."""
            gs = self.game_state
            agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2
            opponent_player_obj = gs.p2 if gs.agent_is_p1 else gs.p1

            # Check if players exist
            if not agent_player_obj or not opponent_player_obj:
                return None, None # Error condition

            # Mulligan/Bottoming Phase
            mulligan_target = getattr(gs, 'mulligan_player', None)
            bottoming_target = getattr(gs, 'bottoming_player', None)
            if getattr(gs, 'mulligan_in_progress', False):
                if mulligan_target == opponent_player_obj:
                    return opponent_player_obj, {
                        "phase_context": "mulligan_decision"}
                if bottoming_target == opponent_player_obj:
                    return opponent_player_obj, {"phase_context": "bottoming"}
                # The learned seat owns the pending pregame decision. Do not
                # fall through to the conceptual Turn-1 priority fields.
                return None, None

            # Special Choice Phase (Targeting, Sacrifice, etc.)
            if gs.phase in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                context = None
                if gs.phase == gs.PHASE_TARGETING: context = getattr(gs, 'targeting_context', None)
                elif gs.phase == gs.PHASE_SACRIFICE: context = getattr(gs, 'sacrifice_context', None)
                elif gs.phase == gs.PHASE_CHOOSE: context = getattr(gs, 'choice_context', None)

                if context:
                    acting_player = context.get('controller') or context.get('player')
                    if acting_player == opponent_player_obj:
                        return opponent_player_obj, {
                            "phase_context": gs._PHASE_NAMES.get(gs.phase)}
                    # A real special choice always belongs to exactly one
                    # policy. If it is not the opponent's, return control to
                    # the learned seat.
                    return None, None
                # A transient phase with no matching context is an orphaned
                # wrapper, not a choice owned by the learned seat. Fall
                # through to ordinary priority routing so a future lifecycle
                # bug cannot strand the policy on NO_OP.

            # Priority Check (Outside Mulligan/Choice)
            priority_target = getattr(gs, 'priority_player', None)
            if priority_target == opponent_player_obj:
                # FIX: If opponent has priority, they MUST act, even if only Pass/Concede is available.
                # Cleanup normally has no priority, but CR 514.3 can grant it;
                # excluding the phase here left the learned seat in NO_OP forever.
                return opponent_player_obj, {"phase_context": "priority"}

            return None, None # Agent needs to act or game state error

    def _get_scripted_opponent_action(self, opponent_player, opponent_mask, opponent_context):
        """Simple scripted policy for opponent actions."""
        gs = self.game_state
        phase_ctx = opponent_context.get("phase_context")
        profile = getattr(self, "active_opponent_profile", "scripted")

        def choose(action_idx):
            generated = self.action_handler.action_reasons_with_context.get(
                action_idx, {}) if self.action_handler else {}
            return action_idx, dict(generated.get('context', {}) or {})

        def accept_opening_hand_placement(card_id):
            """Baseline policy for optional CR 103.6c placements."""
            card = gs._safe_get_card(card_id)
            text = str(getattr(card, "oracle_text", "") or "").lower()
            downside_phrases = (
                "you lose ", "you can't ", "you cannot ", "skip your ",
                "doesn't untap", "sacrifice it", "exile a card from your hand",
            )
            if any(phrase in text for phrase in downside_phrases):
                return False
            # Fixed evaluation environments still record outcomes for audit
            # artifacts, but their scripted decisions must not learn from
            # earlier checkpoints/cases in the same process.
            memory = (
                getattr(self, "card_memory", None)
                if self.adaptive_decision_history_enabled else None
            )
            stats = (getattr(memory, "card_data", {}) or {}).get(
                str(gs.canonical_card_id(card_id)), {}) if memory else {}
            samples = int(stats.get("in_opening_hand", 0) or 0)
            if samples >= 8:
                win_rate = float(stats.get("wins_in_opening_hand", 0) or 0) / samples
                if win_rate < 0.45:
                    return False
            evaluator = getattr(self.action_handler, "card_evaluator", None)
            return (not evaluator
                    or evaluator.evaluate_card(card_id, "play") >= 0.0)

        # 1. Handle Mulligan/Bottoming First
        if phase_ctx == "mulligan_decision":
            # Always Keep (simplest strategy for opponent simulation)
            if opponent_mask[225]: # KEEP_HAND
                logging.debug("Scripted Opponent: KEEP_HAND")
                return 225, {}
            elif opponent_mask[6]: # MULLIGAN (if keep not possible, unlikely)
                 logging.debug("Scripted Opponent: MULLIGAN (Forced)")
                 return 6, {}
            else: # Should not happen if mask is correct
                logging.warning("Scripted Opponent: No mulligan decision action valid!")
                return opponent_mask[11] if opponent_mask[11] else None, {} # Try Pass

        elif phase_ctx == "bottoming":
            # Bottom the first available card (index 0, maps to action 226)
            if opponent_mask[226]:
                 logging.debug("Scripted Opponent: BOTTOM_CARD (Index 0)")
                 return 226, {}
            else:
                 # If card 0 can't be bottomed, try Pass (should auto-advance if no more needed)
                 logging.warning("Scripted Opponent: Cannot bottom card 0, trying Pass.")
                 return opponent_mask[11] if opponent_mask[11] else None, {} # Try Pass


        # 2. Handle target/card selections before the generic choice fallback.
        if phase_ctx == "TARGETING":
            for action_idx in range(274, 284):
                if opponent_mask[action_idx]:
                    logging.debug(
                        f"Scripted Opponent: SELECT_TARGET (Index {action_idx - 274})")
                    return action_idx, {}
            if opponent_mask[11]:
                return 11, {}
            logging.warning("Scripted Opponent: No legal target action available.")
            return None, {}

        if (phase_ctx == "CHOOSE" and getattr(gs, "choice_context", None)
                and gs.choice_context.get("type") in ("discard", "specialize_discard")):
            for action_idx in range(238, 248):
                if opponent_mask[action_idx]:
                    logging.debug(
                        f"Scripted Opponent: DISCARD_CARD (Index {action_idx - 238})")
                    return action_idx, {}
            if opponent_mask[479]:
                return 479, {}
            logging.warning("Scripted Opponent: No discard-card action available.")
            return None, {}

        if phase_ctx == "CHOOSE" and getattr(gs, "choice_context", None):
            choice_type = gs.choice_context.get("type")
            if choice_type in ("scry", "surveil", "explore"):
                # The baseline policy is deliberately conservative: keep the
                # looked-at card on top. These choices do not expose Pass, so
                # falling through to the generic CHOOSE branch deadlocks the
                # opponent loop until strict cycle detection stops training.
                for action_idx in (306, 307, 305):
                    if opponent_mask[action_idx]:
                        logging.debug(
                            "Scripted Opponent: %s (action %s)",
                            choice_type.upper(), action_idx)
                        return action_idx, {}
                logging.warning(
                    "Scripted Opponent: No legal %s destination available.",
                    choice_type)
                return None, {}
            if choice_type in (
                    "choose_mode", "land_mana", "mana_ability_color",
                    "mana_ability_package", "mana_ability_output", "ward_payment",
                    "copy_retarget_slots", "action_catalog"):
                # Mandatory picks (spell modes, dual-land mana colors) expose
                # only CHOOSE_MODE-range actions; take the first legal one.
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        logging.debug(
                            "Scripted Opponent: %s (Index %s)",
                            choice_type.upper(), action_idx - 353)
                        return action_idx, {}
                if opponent_mask[11]:
                    return 11, {}
                return None, {}
            if choice_type in (
                    "sacrifice_effect", "activation_sacrifice_cost",
                    "activation_discard_cost", "distribute_counters",
                    "dig_select"):
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        return action_idx, {}
                if opponent_mask[479]:
                    return 479, {}
                return None, {}
            if choice_type in ("order_triggers", "order_blockers"):
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        return action_idx, {}
                return None, {}
            if choice_type == "opening_hand":
                options = gs.choice_context.get("options", [])
                current = options[0] if options else None
                if (current is not None
                        and accept_opening_hand_placement(current)):
                    for action_idx in range(353, 363):
                        generated = self.action_handler.action_reasons_with_context.get(
                            action_idx, {})
                        option_index = generated.get("context", {}).get(
                            "option_index", action_idx - 353)
                        if (opponent_mask[action_idx]
                                and 0 <= option_index < len(options)
                                and options[option_index] == current):
                            return choose(action_idx)
                if opponent_mask[11]:
                    return 11, {}
                return None, {}
            if (choice_type == "forced_sacrifice"
                    or choice_type.startswith("as_enters_")):
                # First-legal-action policy for mandatory or begin-game picks.
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        logging.debug(
                            f"Scripted Opponent: {choice_type.upper()} "
                            f"(Index {action_idx - 353})")
                        return action_idx, {}
                if opponent_mask[11]:
                    return 11, {}
                if opponent_mask[479]:
                    return 479, {}
                return None, {}
            if choice_type == "casting_additional_return":
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        logging.debug(
                            "Scripted Opponent: RETURN_FOR_ADDITIONAL_COST "
                            f"(Index {action_idx - 353})")
                        return action_idx, {}
                logging.warning(
                    "Scripted Opponent: No permanent return-cost action available.")
                return None, {}
            if choice_type == "collect_evidence":
                # The baseline policy conservatively declines this optional
                # cost. If a future policy has already staged cards, continue
                # until the threshold is legal, then finish with Pass.
                if opponent_mask[11]:
                    return 11, {}
                for action_idx in range(353, 363):
                    if opponent_mask[action_idx]:
                        return action_idx, {}
                return None, {}

        # 3. Handle Other Choice Phases (pass when optional, else first legal)
        if phase_ctx in ["TARGETING", "SACRIFICE", "CHOOSE"]:
            # Optional choices expose PASS; decline them (conservative baseline).
            if opponent_mask[11]:
                logging.debug(f"Scripted Opponent: PASS_PRIORITY (Finish {phase_ctx})")
                return 11, {}
            # Mandatory choices (bargain, choose_x, ...) expose only their
            # option actions; take the first legal one instead of stalling
            # the opponent loop.
            for action_idx, valid in enumerate(opponent_mask):
                if valid and action_idx not in (12, 224):
                    logging.debug(
                        "Scripted Opponent: first legal action %s during %s",
                        action_idx, phase_ctx)
                    return choose(action_idx)
            if opponent_mask[224]:
                return 224, {}
            logging.warning(f"Scripted Opponent: No legal action during {phase_ctx}?")
            return (12 if opponent_mask[12] else None), {}


        # 4. Handle Standard Priority
        if phase_ctx == "priority":
            # Annealed-strength opponents take the goldfish baseline for this
            # one priority decision with probability epsilon: no attacks,
            # blocks, or development.  Mandatory game transactions above stay
            # full fidelity, so the handicap can never deadlock a choice.
            handicap = float(getattr(
                self, "active_opponent_handicap", 0.0) or 0.0)
            if (handicap > 0.0 and profile != "passive"
                    and self._opponent_handicap_rng.random() < handicap):
                profile = "passive"
            # Complete combat declarations before ordinary priority choices.
            if gs.phase == gs.PHASE_DECLARE_ATTACKERS:
                if profile != "passive":
                    for action_idx in range(28, 48):
                        if opponent_mask[action_idx]:
                            return choose(action_idx)
                if opponent_mask[479]:
                    return choose(479)
                if opponent_mask[438]:
                    return choose(438)
            if gs.phase == gs.PHASE_DECLARE_BLOCKERS:
                if profile in ("passive", "novice"):
                    if opponent_mask[479]:
                        return choose(479)
                    if opponent_mask[439]:
                        return choose(439)
                # If a sequential declaration is incomplete, add the next
                # blocker before considering the withdrawal action occupying
                # an earlier slot. Otherwise the ascending first-action policy
                # can alternate assign/withdraw forever for out-of-range
                # menace attackers.
                live_assignments = (
                    self.action_handler.combat_handler
                    ._live_block_assignments())
                for action_idx in range(48, 68):
                    if not opponent_mask[action_idx]:
                        continue
                    generated = (
                        self.action_handler.action_reasons_with_context.get(
                            action_idx, {}))
                    target_attacker_id = generated.get(
                        'context', {}).get('target_attacker_id')
                    if len(live_assignments.get(
                            target_attacker_id, [])) == 1:
                        return choose(action_idx)
                for action_idx in range(48, 68):
                    if opponent_mask[action_idx]:
                        return choose(action_idx)
                # Menace blocks begin atomically so the sequential action API
                # never enters an illegal one-blocker intermediate state.
                # Prefer the first mask-valid attacker-specific multi-block
                # before declining blocks with DECLARE_BLOCKERS_DONE.
                for action_idx in range(383, 393):
                    if opponent_mask[action_idx]:
                        return choose(action_idx)
                if opponent_mask[479]:
                    return choose(479)
                if opponent_mask[439]:
                    return choose(439)

            # The goldfish profile completes mandatory game transactions but
            # never develops, attacks, or blocks. It teaches the learned seat
            # to produce a legal kill before defensive pressure is introduced.
            if profile == "passive":
                if opponent_mask[11]:
                    return choose(11)
                if opponent_mask[224]:
                    return choose(224)
                return choose(12) if opponent_mask[12] else (None, {})

            # Develop mana first, then cast the first affordable legal spell.
            # This remains intentionally simple, but is a real baseline rather
            # than an opponent that passes every game action.
            land_actions = list(range(13, 20)) + list(range(180, 188)) \
                + list(range(393, 396))
            for action_idx in land_actions:
                if opponent_mask[action_idx]:
                    return choose(action_idx)

            spell_actions = list(range(20, 28)) + list(range(188, 204)) \
                + list(range(396, 405)) + [445, 446, 447, 448]
            for action_idx in spell_actions:
                if opponent_mask[action_idx]:
                    return choose(action_idx)

            # Spell affordability is checked against floating mana only, so
            # tap lands during our main phase until a spell becomes castable.
            if (gs.phase in (gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT)
                    and gs._get_active_player() is opponent_player):
                for action_idx in range(68, 88):
                    if opponent_mask[action_idx]:
                        return choose(action_idx)

            if opponent_mask[11]:
                logging.debug("Scripted Opponent: PASS_PRIORITY")
                return choose(11)
            logging.warning("Scripted Opponent: No PASS_PRIORITY available?")
            if opponent_mask[224]:
                return choose(224)
            return choose(12) if opponent_mask[12] else (None, {})

        # 5. Fallback (If context unknown or logic missed)
        logging.warning(f"Scripted Opponent: Unknown phase context '{phase_ctx}', defaulting to PASS.")
        if opponent_mask[11]: return 11, {}
        if opponent_mask[224]: return 224, {}
        return opponent_mask[12] if opponent_mask[12] else None, {} # Concede last resort

    @contextmanager
    def _observer_policy_boundary(self, observer):
        """Temporarily expose one player's policy view without cache leaks.

        Legal-mask generation and strategic observation construction mutate
        environment/handler caches.  Saving only ``agent_is_p1`` therefore
        left a direct opponent prediction with the other seat's action
        contexts and planner analysis.  This boundary restores every mutable
        policy cache on success and on failure while leaving real game-rule
        mutations untouched.
        """
        gs = self.game_state
        if observer is not gs.p1 and observer is not gs.p2:
            raise ValueError("Observer must be the live P1 or P2 object")

        snapshots = []

        def remember(obj, attribute):
            if obj is None:
                return
            exists = hasattr(obj, attribute)
            snapshots.append((obj, attribute, exists,
                              getattr(obj, attribute, None)))

        for attribute in (
                "current_valid_actions", "current_analysis",
                "last_observation_error", "last_observation_traceback",
                "last_action_mask_error"):
            remember(self, attribute)
        handler = getattr(self, "action_handler", None)
        for attribute in (
                "current_valid_actions", "action_reasons",
                "action_reasons_with_context", "last_mask_error"):
            remember(handler, attribute)
        planner = getattr(self, "strategic_planner", None)
        for attribute in (
                "current_analysis", "opponent_archetype",
                "strategy_type", "strategy_params",
                "aggression_level", "risk_tolerance"):
            remember(planner, attribute)
        original_perspective = bool(gs.agent_is_p1)
        try:
            observer_is_p1 = observer is gs.p1
            gs.agent_is_p1 = observer_is_p1
            if (planner is not None
                    and observer_is_p1 != bool(getattr(
                        self, "_episode_agent_is_p1",
                        self.initial_agent_is_p1))):
                profile = getattr(
                    self, "_observer_strategy_profiles", {}).get(
                        observer_is_p1)
                if not profile:
                    raise RuntimeError(
                        "Observer-private strategic profile unavailable")
                for attribute, value in profile.items():
                    setattr(planner, attribute, copy.deepcopy(value))
            yield
        finally:
            gs.agent_is_p1 = original_perspective
            for obj, attribute, existed, value in reversed(snapshots):
                if existed:
                    setattr(obj, attribute, value)
                elif hasattr(obj, attribute):
                    delattr(obj, attribute)

    def _strict_action_mask_for_current_perspective(self, label):
        """Return a real policy mask, rejecting diagnostic fallbacks."""
        mask = self.action_mask().astype(bool)
        mask_error = (
            getattr(self, "last_action_mask_error", None)
            or getattr(
                getattr(self, "action_handler", None),
                "last_mask_error", None))
        if mask_error:
            raise RuntimeError(
                f"{label} action mask degraded: {mask_error}")
        return mask.copy()

    def action_mask_for(self, player):
        """Return a fresh legal mask for ``player`` without changing caller state."""
        with self._observer_policy_boundary(player):
            return self._strict_action_mask_for_current_perspective(
                "Opponent")

    def _policy_observation_for_current_perspective(self):
        """Build a strict observation isolated from prior-seat diagnostics."""
        self.last_observation_error = None
        self.last_observation_traceback = None
        observation = self._get_obs()
        if self.last_observation_error is not None:
            raise RuntimeError(
                "Opponent checkpoint observation degraded: "
                f"{self.last_observation_error}")
        mask_error = (
            getattr(self, "last_action_mask_error", None)
            or getattr(
                getattr(self, "action_handler", None),
                "last_mask_error", None))
        if mask_error:
            raise RuntimeError(
                "Opponent checkpoint observation degraded: action mask: "
                f"{mask_error}")
        return observation

    def set_opponent_policy(self, policy):
        """Immediately install a direct policy (legacy tests/direct callers).

        Production checkpoint rotation uses ``stage_checkpoint_opponent`` so
        a worker can never change policies in the middle of an episode.
        """
        if policy is not None and not callable(getattr(policy, "predict", None)):
            raise TypeError("Opponent policy must expose callable predict")
        self._direct_opponent_policy = policy
        self.opponent_policy = policy
        self._current_checkpoint_opponent = None

    def _get_opponent_policy_action(
            self, opponent_player, opponent_mask=None, opponent_context=None):
        """Choose from an explicit opponent view and restore the caller view."""
        with self._observer_policy_boundary(opponent_player):
            live_mask = self._strict_action_mask_for_current_perspective(
                "Opponent")
            if opponent_mask is not None:
                supplied = np.asarray(opponent_mask, dtype=bool)
                if supplied.shape != live_mask.shape:
                    raise RuntimeError(
                        "Opponent mask has an invalid shape: "
                        f"{supplied.shape}")
                if not np.array_equal(supplied, live_mask):
                    logging.warning(
                        "Discarding a stale caller-supplied opponent mask; "
                        "using the live perspective-correct mask")
            policy = getattr(self, 'opponent_policy', None)
            if policy is None:
                return self._get_scripted_opponent_action(
                    opponent_player, live_mask, opponent_context or {})
            observation = self._policy_observation_for_current_perspective()
            # _get_obs regenerates the same live mask and leaves the matching
            # dispatch contexts in ActionHandler for extraction below.
            policy_mask = np.asarray(
                observation["action_mask"], dtype=bool)
            if not np.array_equal(policy_mask, live_mask):
                raise RuntimeError(
                    "Opponent observation mask changed during prediction")
            result = policy.predict(
                observation, action_masks=policy_mask, deterministic=True)
            action = result[0] if isinstance(result, tuple) else result
            action = int(np.asarray(action).reshape(-1)[0])
            if 0 <= action < self.ACTION_SPACE_SIZE and policy_mask[action]:
                return action, getattr(
                    self.action_handler, "action_reasons_with_context", {}
                ).get(action, {}).get('context', {})
            raise RuntimeError(
                f"Opponent checkpoint returned mask-invalid action {action}")

    @staticmethod
    def _checkpoint_sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _normalize_checkpoint_opponent(self, checkpoint):
        if not isinstance(checkpoint, dict):
            raise TypeError("Checkpoint opponent must be a manifest dictionary")
        policy_id = str(checkpoint.get("policy_id", "")).strip()
        if not policy_id or len(policy_id) > 256:
            raise ValueError("Checkpoint policy_id must contain 1-256 characters")
        raw_path = checkpoint.get("path")
        if not isinstance(raw_path, (str, os.PathLike)):
            raise TypeError("Checkpoint path must be path-like")
        path = os.path.abspath(os.fspath(raw_path))
        if not os.path.isfile(path):
            raise ValueError(f"Checkpoint path is not a file: {path}")
        expected_sha256 = str(checkpoint.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("Checkpoint SHA-256 must be 64 hexadecimal characters")
        actual_sha256 = self._checkpoint_sha256(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                "Checkpoint SHA-256 mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}")
        return {
            "path": path,
            "policy_id": policy_id,
            "sha256": actual_sha256,
        }

    def _validate_checkpoint_opponent_policy(self, policy):
        if not callable(getattr(policy, "predict", None)):
            raise ValueError("Checkpoint policy does not expose callable predict")
        policy_observation_space = getattr(policy, "observation_space", None)
        if (policy_observation_space is not None
                and policy_observation_space != self.observation_space):
            raise ValueError(
                "Checkpoint policy observation space is incompatible with "
                "this environment")
        policy_action_space = getattr(policy, "action_space", None)
        if (policy_action_space is not None
                and policy_action_space != self.action_space):
            raise ValueError(
                "Checkpoint policy action space is incompatible with this "
                "environment")
        return policy

    @staticmethod
    def _freeze_checkpoint_opponent_policy(policy):
        set_training_mode = getattr(policy, "set_training_mode", None)
        if callable(set_training_mode):
            set_training_mode(False)
        eval_mode = getattr(policy, "eval", None)
        if callable(eval_mode):
            eval_mode()
        parameters = getattr(policy, "parameters", None)
        if callable(parameters):
            for parameter in parameters():
                requires_grad = getattr(parameter, "requires_grad_", None)
                if callable(requires_grad):
                    requires_grad(False)
                elif hasattr(parameter, "requires_grad"):
                    parameter.requires_grad = False
        if hasattr(policy, "optimizer"):
            # Prediction needs no optimizer; dropping it releases the largest
            # avoidable part of the PPO checkpoint in every worker.
            policy.optimizer = None
        return policy

    def _load_checkpoint_opponent_policy(self, path):
        """Load one CPU policy and discard PPO rollout/optimizer state."""
        from sb3_contrib import MaskablePPO

        algorithm = MaskablePPO.load(path, device="cpu")
        try:
            if algorithm.observation_space != self.observation_space:
                raise ValueError(
                    "Checkpoint algorithm observation space is incompatible "
                    "with this environment")
            if algorithm.action_space != self.action_space:
                raise ValueError(
                    "Checkpoint algorithm action space is incompatible with "
                    "this environment")
            policy = self._validate_checkpoint_opponent_policy(
                algorithm.policy)
            return self._freeze_checkpoint_opponent_policy(policy)
        finally:
            # The returned policy has its own reference. The algorithm and its
            # rollout/training state can be reclaimed after this call.
            del algorithm

    def stage_checkpoint_opponent(
            self, checkpoint, probability=1.0, seed=0):
        """Load and stage one frozen checkpoint for the next episode.

        This method is safe for ``VecEnv.env_method``: all arguments and the
        returned status are serializable, and corrupt/incompatible checkpoints
        raise synchronously before any pending assignment changes.
        """
        try:
            probability = float(probability)
            if (not math.isfinite(probability)
                    or not 0.0 <= probability <= 1.0):
                raise ValueError(
                    "Checkpoint opponent probability must be within [0, 1]")
            seed = int(seed)
            normalized = self._normalize_checkpoint_opponent(checkpoint)
            active = self._active_checkpoint_opponent
            pending = self._pending_checkpoint_opponent
            if active == normalized:
                loaded_policy = self._resident_checkpoint_opponent_policy
            elif pending == normalized:
                loaded_policy = self._pending_checkpoint_opponent_policy
            else:
                loaded_policy = self._load_checkpoint_opponent_policy(
                    normalized["path"])
            loaded_policy = self._validate_checkpoint_opponent_policy(
                loaded_policy)
        except Exception as error:
            self._checkpoint_opponent_last_error = (
                f"{type(error).__name__}: {error}")
            raise

        self._pending_checkpoint_opponent = normalized
        self._pending_checkpoint_opponent_policy = loaded_policy
        self._checkpoint_opponent_pending_set = True
        self._checkpoint_opponent_probability = probability
        self._checkpoint_opponent_seed = seed
        self._checkpoint_opponent_rng = random.Random(seed)
        self._checkpoint_opponent_last_error = None
        return self.checkpoint_opponent_status()

    def clear_checkpoint_opponent(self):
        """Stage a return to the scripted opponent at the next reset."""
        self._direct_opponent_policy = None
        self._pending_checkpoint_opponent = None
        self._pending_checkpoint_opponent_policy = None
        self._checkpoint_opponent_pending_set = True
        self._checkpoint_opponent_probability = 0.0
        self._checkpoint_opponent_last_error = None
        return self.checkpoint_opponent_status()

    def _commit_checkpoint_opponent_for_reset(self):
        if self._checkpoint_opponent_pending_set:
            self._resident_checkpoint_opponent_policy = (
                self._pending_checkpoint_opponent_policy)
            self._active_checkpoint_opponent = (
                dict(self._pending_checkpoint_opponent)
                if self._pending_checkpoint_opponent is not None else None)
            self._pending_checkpoint_opponent_policy = None
            self._pending_checkpoint_opponent = None
            self._checkpoint_opponent_pending_set = False

        use_checkpoint = (
            self._resident_checkpoint_opponent_policy is not None
            and self._checkpoint_opponent_rng.random()
            < self._checkpoint_opponent_probability)
        if use_checkpoint:
            self.opponent_policy = self._resident_checkpoint_opponent_policy
            self._current_checkpoint_opponent = dict(
                self._active_checkpoint_opponent)
        else:
            # Directly installed policies (notably checkpoint-vs-checkpoint
            # fixture harvests) persist across reset when no resident lease
            # owns opponent selection. A configured resident deliberately
            # overrides direct play even on its scripted-probability episodes.
            self.opponent_policy = (
                self._direct_opponent_policy
                if self._resident_checkpoint_opponent_policy is None
                else None)
            self._current_checkpoint_opponent = None

    def checkpoint_opponent_status(self):
        """Return serializable worker status for orchestration diagnostics."""
        pending = (
            dict(self._pending_checkpoint_opponent)
            if self._pending_checkpoint_opponent is not None else None)
        if self._checkpoint_opponent_pending_set:
            status = "staged" if pending is not None else "clear_staged"
        elif self._active_checkpoint_opponent is not None:
            status = "active"
        else:
            status = "scripted"
        return {
            "status": status,
            "active": (
                dict(self._active_checkpoint_opponent)
                if self._active_checkpoint_opponent is not None else None),
            "pending": pending,
            "pending_policy_id": (
                (pending or {}).get("policy_id")),
            "pending_checkpoint_sha256": (
                (pending or {}).get("sha256")),
            "pending_clear": bool(
                self._checkpoint_opponent_pending_set
                and self._pending_checkpoint_opponent is None),
            "probability": float(self._checkpoint_opponent_probability),
            "seed": int(self._checkpoint_opponent_seed),
            "current_policy_id": (
                (self._current_checkpoint_opponent or {}).get("policy_id")),
            "current_sha256": (
                (self._current_checkpoint_opponent or {}).get("sha256")),
            "using_checkpoint": bool(
                self._current_checkpoint_opponent is not None),
            "direct_policy_installed": bool(
                self._direct_opponent_policy is not None),
            "last_error": self._checkpoint_opponent_last_error,
        }

    def _evaluation_trace_enabled(self):
        """Whether this episode belongs to an attributable evaluation."""
        return (
            getattr(self, "evaluation_timestep", None) is not None
            and bool(getattr(self, "evaluation_checkpoint_sha256", None))
        )

    def _reset_evaluation_capture_telemetry(self):
        """Reset bounded debug-capture accounting for one episode."""
        self._evaluation_capture = {
            "schema_version": 1,
            "limits": {
                "trace_events": int(self.EVALUATION_TRACE_MAX_EVENTS),
                "trace_serialized_bytes": int(
                    self.EVALUATION_TRACE_MAX_BYTES),
                "trace_entry_serialized_bytes": int(
                    self.EVALUATION_TRACE_ENTRY_MAX_BYTES),
                "replay_events": int(self.EVALUATION_REPLAY_MAX_EVENTS),
                "replay_serialized_bytes": int(
                    self.EVALUATION_REPLAY_MAX_BYTES),
                "replay_entry_serialized_bytes": int(
                    self.EVALUATION_REPLAY_ENTRY_MAX_BYTES),
                "debug_payload_serialized_bytes": int(
                    self.EVALUATION_DEBUG_MAX_BYTES),
                "sanitizer": {
                    "max_depth": int(self.DIAGNOSTIC_MAX_DEPTH),
                    "max_nodes": int(self.DIAGNOSTIC_MAX_NODES),
                    "max_container_items": int(
                        self.DIAGNOSTIC_MAX_CONTAINER_ITEMS),
                    "max_string_length": int(
                        self.DIAGNOSTIC_MAX_STRING_LENGTH),
                    "max_key_length": int(self.DIAGNOSTIC_MAX_KEY_LENGTH),
                },
            },
            "trace": {
                "recorded_events": 0,
                "dropped_events": 0,
                # Compact JSON bytes for the complete trace list, including
                # its opening/closing brackets and inter-entry commas.
                "serialized_bytes": 2,
                "sanitization_omissions": 0,
                "serialization_errors": 0,
            },
            "replay": {
                "recorded_events": 0,
                "dropped_events": 0,
                "serialized_bytes": 2,
                "sanitization_omissions": 0,
                "serialization_errors": 0,
            },
            "terminal": {
                "serialized_bytes": 0,
                "sanitization_omissions": 0,
                "serialization_errors": 0,
            },
            "errors": [],
        }

    @staticmethod
    def _evaluation_error_text(error):
        error_type = type(error)
        type_name = f"{error_type.__module__}.{error_type.__qualname__}"
        try:
            message = str(error)
        except Exception:
            message = "unprintable error"
        if len(message) > 512:
            message = message[:509] + "..."
        return type_name, message

    @staticmethod
    def _evaluation_finite_number(value, default=0.0):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return float(default)
        return number if math.isfinite(number) else float(default)

    def _note_evaluation_capture_error(self, stage, error, *, scope="trace"):
        """Record a bounded structured omission without perturbing the game."""
        capture = getattr(self, "_evaluation_capture", None)
        if not isinstance(capture, dict):
            return
        scope_stats = capture.get(scope)
        if isinstance(scope_stats, dict):
            scope_stats["serialization_errors"] = int(
                scope_stats.get("serialization_errors", 0)) + 1
        errors = capture.setdefault("errors", [])
        if len(errors) >= 32:
            return
        error_type, message = self._evaluation_error_text(error)
        errors.append({
            "stage": str(stage)[:128],
            "scope": str(scope)[:32],
            "error_type": error_type,
            "message": message,
        })

    def _safe_evaluation_state_snapshot(self, stage, *, valid_mask=None):
        """Capture one state snapshot; return an omission marker on failure."""
        try:
            return self._evaluation_state_snapshot(valid_mask=valid_mask)
        except Exception as error:
            self._note_evaluation_capture_error(stage, error, scope="trace")
            return {
                "__diagnostic_omitted__": {
                    "reason": "state_snapshot_error",
                    "stage": str(stage)[:128],
                    "error_type": self._evaluation_error_text(error)[0],
                },
            }

    def _evaluation_player_label(self, player):
        gs = getattr(self, "game_state", None)
        if gs is not None:
            if player is getattr(gs, "p1", None):
                return "p1"
            if player is getattr(gs, "p2", None):
                return "p2"
        return None

    @staticmethod
    def _evaluation_zone_snapshot(player, zone_name, *, include_cards=True):
        """Return a bounded, JSON-oriented zone summary for one player."""
        raw_cards = player.get(zone_name, ()) if isinstance(player, dict) else ()
        try:
            cards = list(raw_cards or ())
        except TypeError:
            cards = []
        snapshot = {"count": len(cards)}
        if include_cards:
            # Ordinary games keep these zones small.  The cap prevents a rules
            # bug from turning one diagnostic trace into an unbounded artifact.
            snapshot["cards"] = cards[:128]
            if len(cards) > 128:
                snapshot["omitted"] = len(cards) - 128
        return snapshot

    @staticmethod
    def _evaluation_bounded_pool(raw_pool, limit=16):
        """Copy one small mana pool using deterministic bounded keys."""
        if not isinstance(raw_pool, dict):
            return {}
        items = sorted(raw_pool.items(), key=lambda item: str(item[0]))
        return {str(key): value for key, value in items[:limit]}

    def _evaluation_mana_snapshot(self, player):
        if not isinstance(player, dict):
            return {}

        def restricted_buckets(name):
            raw = player.get(name, {}) or {}
            if not isinstance(raw, dict):
                return {}
            items = sorted(raw.items(), key=lambda item: str(item[0]))[:32]
            return {
                str(restriction): self._evaluation_bounded_pool(pool)
                for restriction, pool in items if isinstance(pool, dict)
            }

        return {
            "normal": self._evaluation_bounded_pool(
                player.get("mana_pool")),
            "snow": self._evaluation_bounded_pool(
                player.get("snow_mana_pool")),
            "phase_restricted": self._evaluation_bounded_pool(
                player.get("phase_restricted_mana")),
            "phase_restricted_snow": self._evaluation_bounded_pool(
                player.get("phase_restricted_snow_mana")),
            "conditional": restricted_buckets("conditional_mana"),
            "conditional_snow": restricted_buckets(
                "conditional_snow_mana"),
        }

    def _evaluation_decision_context_snapshot(self):
        """Summarize active multi-step choice state without serializing code."""
        gs = getattr(self, "game_state", None)
        if gs is None:
            return None
        scalar_keys = (
            "type", "choice_kind", "stage", "optional", "required_type",
            "min_targets", "max_targets", "required_count", "outside_zone",
            "resume_phase", "source_id", "card_id", "target_id",
            "attacker_id", "blocker_id", "effect_text", "prompt", "reason",
            "targeting_text",
        )
        sequence_keys = (
            "options", "selected_targets", "valid_targets", "targets",
            "selected", "cards", "card_ids", "target_ids",
        )
        result = {}
        for label, attribute in (
                ("targeting", "targeting_context"),
                ("choice", "choice_context"),
                ("sacrifice", "sacrifice_context")):
            raw = getattr(gs, attribute, None)
            if not isinstance(raw, dict):
                continue
            summary = {
                key: raw.get(key) for key in scalar_keys
                if key in raw and raw.get(key) is not None
            }
            for controller_key in ("player", "controller", "target_player"):
                if controller_key not in raw:
                    continue
                controller = raw.get(controller_key)
                summary[f"{controller_key}_seat"] = (
                    self._evaluation_player_label(controller)
                    or (controller
                        if isinstance(controller, (str, int)) else None))
            for key in sequence_keys:
                if key not in raw:
                    continue
                value = raw.get(key)
                if isinstance(value, (list, tuple, set, frozenset)):
                    values = list(value)
                    summary[key] = values[:32]
                    summary[f"{key}_count"] = len(values)
                    if len(values) > 32:
                        summary[f"{key}_omitted"] = len(values) - 32
                elif isinstance(value, dict):
                    items = sorted(
                        value.items(), key=lambda item: str(item[0]))
                    summary[key] = {
                        str(key_item): value_item
                        for key_item, value_item in items[:32]
                    }
                    summary[f"{key}_count"] = len(items)
                    if len(items) > 32:
                        summary[f"{key}_omitted"] = len(items) - 32
            summary["raw_key_count"] = len(raw)
            result[label] = summary
        return result or None

    def _evaluation_state_snapshot(self, *, valid_mask=None):
        """Compact state at one atomic action boundary.

        The seed plus learned action/context replay remains authoritative.  The
        snapshot is deliberately diagnostic rather than a second GameState
        serializer: libraries retain counts, while the changing zones retain
        card IDs so a viewer can explain a transition without replaying it.
        """
        gs = getattr(self, "game_state", None)
        if gs is None:
            return {}

        def player_snapshot(player):
            if not isinstance(player, dict):
                return None
            battlefield = list(player.get("battlefield", ()) or ())[:128]
            tapped = list(player.get("tapped_permanents", ()) or ())
            damage_items = list(
                (player.get("damage_counters", {}) or {}).items())[:128]
            permanent_counters = []
            for card_id in battlefield:
                card = getattr(gs, "card_db", {}).get(card_id)
                counters = getattr(card, "counters", None)
                if isinstance(counters, dict) and counters:
                    permanent_counters.append({
                        "card_id": card_id,
                        "counters": self._evaluation_bounded_pool(
                            counters, limit=32),
                    })
            result = {
                "life": player.get("life"),
                "poison_counters": player.get("poison_counters", 0),
                "energy_counters": player.get("energy_counters", 0),
                "experience_counters": player.get(
                    "experience_counters", 0),
                "mana": self._evaluation_mana_snapshot(player),
                "lands_played_this_turn": player.get(
                    "lands_played_this_turn", 0),
                "land_played": bool(player.get("land_played", False)),
                "tapped_permanents": tapped[:128],
                "damage_marked": [
                    {"card_id": card_id, "amount": amount}
                    for card_id, amount in damage_items
                ],
                "permanent_counters": permanent_counters[:128],
                "zones": {
                    "library": self._evaluation_zone_snapshot(
                        player, "library", include_cards=False),
                    "hand": self._evaluation_zone_snapshot(player, "hand"),
                    "battlefield": self._evaluation_zone_snapshot(
                        player, "battlefield"),
                    "graveyard": self._evaluation_zone_snapshot(
                        player, "graveyard"),
                    "exile": self._evaluation_zone_snapshot(player, "exile"),
                    "outside_game": self._evaluation_zone_snapshot(
                        player, "outside_game"),
                    "sideboard": self._evaluation_zone_snapshot(
                        player, "sideboard"),
                },
            }
            if len(tapped) > 128:
                result["tapped_permanents_omitted"] = len(tapped) - 128
            raw_damage = player.get("damage_counters", {}) or {}
            if isinstance(raw_damage, dict) and len(raw_damage) > 128:
                result["damage_marked_omitted"] = len(raw_damage) - 128
            return result

        stack = []
        live_stack = list(getattr(gs, "stack", ()) or ())
        for item in live_stack[:32]:
            if isinstance(item, tuple) and len(item) >= 3:
                stack_item = {
                    "kind": str(item[0]),
                    "source_id": item[1],
                    "controller": self._evaluation_player_label(item[2]),
                }
                raw_context = item[3] \
                    if len(item) > 3 and isinstance(item[3], dict) else {}
                context = {}
                for key in (
                        "target_id", "target_card_id", "target_player",
                        "mode", "x_value", "effect_text", "targeting_text"):
                    context_value = raw_context.get(key)
                    if context_value is None:
                        continue
                    if isinstance(context_value, str):
                        context_value = context_value[:512]
                    context[key] = context_value
                for key in (
                        "target_ids", "selected_targets", "modes"):
                    context_value = raw_context.get(key)
                    if isinstance(
                            context_value,
                            (list, tuple, set, frozenset)):
                        context[key] = list(context_value)[:32]
                if context:
                    stack_item["context"] = context
                stack.append(stack_item)
            else:
                stack.append({"kind": type(item).__name__})

        live_attackers = list(
            getattr(gs, "current_attackers", ()) or ())
        raw_blocks = getattr(gs, "current_block_assignments", {}) or {}
        block_assignments = {}
        if isinstance(raw_blocks, dict):
            for attacker_id, blocker_ids in list(raw_blocks.items())[:64]:
                try:
                    blockers = list(blocker_ids or ())
                except TypeError:
                    blockers = []
                block_assignments[str(attacker_id)] = blockers[:32]

        def bounded_combat_targets(attribute):
            raw_targets = getattr(gs, attribute, {}) or {}
            if not isinstance(raw_targets, dict):
                return {}
            return {
                str(attacker_id): target_id
                for attacker_id, target_id
                in list(raw_targets.items())[:64]
            }

        try:
            active_player = self._evaluation_player_label(
                gs._get_active_player())
        except Exception:
            active_player = None

        phase = getattr(gs, "phase", None)
        snapshot = {
            "turn": getattr(gs, "turn", None),
            "phase": phase,
            "phase_name": getattr(gs, "_PHASE_NAMES", {}).get(
                phase, str(phase) if phase is not None else None),
            "priority_player": self._evaluation_player_label(
                getattr(gs, "priority_player", None)),
            "active_player": active_player,
            "p1": player_snapshot(getattr(gs, "p1", None)),
            "p2": player_snapshot(getattr(gs, "p2", None)),
            "stack": stack,
            "combat": {
                "attackers": live_attackers[:64],
                "block_assignments": block_assignments,
                "planeswalker_targets": bounded_combat_targets(
                    "planeswalker_attack_targets"),
                "battle_targets": bounded_combat_targets(
                    "battle_attack_targets"),
            },
        }
        decision_context = self._evaluation_decision_context_snapshot()
        if decision_context is not None:
            snapshot["decision_context"] = decision_context
        if valid_mask is not None:
            mask = np.asarray(valid_mask, dtype=bool).reshape(-1)
            valid_indices = np.flatnonzero(mask).tolist()
            limit = int(self.ACTION_SPACE_SIZE)
            snapshot["valid_actions"] = {
                "indices": valid_indices[:limit],
                "count": len(valid_indices),
                "omitted": max(0, len(valid_indices) - limit),
                "mask_size": int(mask.size),
            }
        if len(live_stack) > 32:
            snapshot["stack_omitted"] = len(live_stack) - 32
        if len(live_attackers) > 64:
            snapshot["combat"]["attackers_omitted"] = \
                len(live_attackers) - 64
        if isinstance(raw_blocks, dict) and len(raw_blocks) > 64:
            snapshot["combat"]["block_assignments_omitted"] = \
                len(raw_blocks) - 64
        return self._json_safe_replay_value(snapshot)

    def _evaluation_action_description(self, action_idx, context):
        """Return stable machine fields plus a concise operator-facing label."""
        action_type, parameter = "UNKNOWN", None
        handler = getattr(self, "action_handler", None)
        try:
            if handler is not None:
                action_type, parameter = handler.get_action_info(int(action_idx))
        except Exception:
            action_type, parameter = "UNKNOWN", None
        action_type = str(action_type)
        parameter = self._json_safe_replay_value(parameter)
        label = action_type if parameter is None else \
            f"{action_type} ({parameter})"
        if isinstance(context, dict):
            source_id = context.get("card_id", context.get("source_id"))
            card = None
            try:
                if source_id is not None:
                    card = self.game_state._safe_get_card(source_id)
            except Exception:
                card = None
            card_name = getattr(card, "name", None)
            if card_name:
                label = f"{label}: {card_name}"
        return {
            "action_type": action_type,
            "action_parameter": parameter,
            "label": label,
        }

    def _record_evaluation_atomic_action(
            self, *, actor, actor_is_p1, action_idx, context,
            pre_state, post_state):
        """Append one learned or opponent action to the contiguous trace."""
        if not self._evaluation_trace_enabled():
            return None
        trace_stats = self._evaluation_capture["trace"]
        try:
            trace = getattr(self, "evaluation_action_trace", None)
            if not isinstance(trace, list):
                trace = []
                self.evaluation_action_trace = trace
            if trace_stats["recorded_events"] \
                    >= self.EVALUATION_TRACE_MAX_EVENTS:
                trace_stats["dropped_events"] += 1
                return None
            sequence = len(trace)
            try:
                context_copy = dict(context or {})
            except Exception as error:
                self._note_evaluation_capture_error(
                    "trace_context_copy", error, scope="trace")
                context_copy = {
                    "__diagnostic_omitted__": {
                        "reason": "context_copy_error",
                        "error_type": self._evaluation_error_text(error)[0],
                    },
                }
            entry = {
                "sequence": sequence,
                "actor": str(actor)[:64],
                "actor_seat": "p1" if actor_is_p1 else "p2",
                "action": int(action_idx),
                "context": context_copy,
                **self._evaluation_action_description(action_idx, context),
                "pre": pre_state,
                "post": post_state,
            }
            evaluator_payload = self._drain_evaluator_diagnostics()
            if evaluator_payload is not None:
                entry["evaluator"] = evaluator_payload
            safe_entry, sanitization = self._sanitize_replay_value(
                entry, max_bytes=self.EVALUATION_TRACE_ENTRY_MAX_BYTES)
            trace_stats["sanitization_omissions"] += int(
                sanitization.get("omissions", 0))
            trace_stats["serialization_errors"] += int(
                sanitization.get("errors", 0))
            if not isinstance(safe_entry, dict) \
                    or "__diagnostic_omitted__" in safe_entry:
                safe_entry = {
                    "sequence": sequence,
                    "actor": str(actor)[:64],
                    "actor_seat": "p1" if actor_is_p1 else "p2",
                    "action": int(action_idx),
                    "context": {
                        "__diagnostic_omitted__": {
                            "reason": "trace_entry_byte_budget",
                        },
                    },
                    **self._evaluation_action_description(
                        action_idx, {}),
                    "pre": {"__diagnostic_omitted__": {
                        "reason": "trace_entry_byte_budget"}},
                    "post": {"__diagnostic_omitted__": {
                        "reason": "trace_entry_byte_budget"}},
                    "serialization": sanitization,
                }
            elif sanitization.get("omissions") \
                    or sanitization.get("errors"):
                safe_entry["serialization"] = sanitization
            encoded = json.dumps(
                safe_entry, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > self.EVALUATION_TRACE_ENTRY_MAX_BYTES:
                trace_stats["dropped_events"] += 1
                trace_stats["sanitization_omissions"] += 1
                return None
            added_bytes = len(encoded) + int(
                trace_stats["recorded_events"] > 0)
            if trace_stats["serialized_bytes"] + added_bytes \
                    > self.EVALUATION_TRACE_MAX_BYTES:
                trace_stats["dropped_events"] += 1
                return None
            trace.append(safe_entry)
            trace_stats["recorded_events"] += 1
            trace_stats["serialized_bytes"] += added_bytes
            return sequence
        except Exception as error:
            trace_stats["dropped_events"] += 1
            self._note_evaluation_capture_error(
                "record_atomic_action", error, scope="trace")
            return None

    def _drain_evaluator_diagnostics(self):
        """Return one non-causal evaluator activity window, if enabled."""
        evaluator = getattr(self, "card_evaluator", None)
        drain = getattr(evaluator, "drain_diagnostics", None)
        if not callable(drain):
            return None
        try:
            payload = drain()
        except Exception as error:
            logging.warning(
                "Could not drain evaluator diagnostics: %s", error)
            self._note_evaluation_capture_error(
                "evaluator_drain", error, scope="trace")
            return {
                "schema_version": 1,
                "events": [],
                "omission": {
                    "reason": "evaluator_drain_error",
                    "error_type": self._evaluation_error_text(error)[0],
                },
            }
        if payload is None:
            return None
        try:
            safe, metadata = self._sanitize_replay_value(
                payload, max_bytes=self.EVALUATION_TRACE_ENTRY_MAX_BYTES)
            trace_stats = self._evaluation_capture["trace"]
            trace_stats["sanitization_omissions"] += int(
                metadata.get("omissions", 0))
            trace_stats["serialization_errors"] += int(
                metadata.get("errors", 0))
            return safe
        except Exception as error:
            self._note_evaluation_capture_error(
                "evaluator_drain_sanitize", error, scope="trace")
            return {
                "schema_version": 1,
                "events": [],
                "omission": {
                    "reason": "evaluator_sanitization_error",
                    "error_type": self._evaluation_error_text(error)[0],
                },
            }

    def _evaluator_diagnostic_totals(self):
        evaluator = getattr(self, "card_evaluator", None)
        totals = getattr(evaluator, "diagnostic_totals", None)
        if not callable(totals):
            return None
        try:
            return self._json_safe_replay_value(totals())
        except Exception as error:
            logging.warning(
                "Could not summarize evaluator diagnostics: %s", error)
            self._note_evaluation_capture_error(
                "evaluator_summary", error, scope="terminal")
            return {
                "omission": {
                    "reason": "evaluator_summary_error",
                    "error_type": self._evaluation_error_text(error)[0],
                },
            }

    def _attach_evaluation_transition_reward(
            self, sequence, step_reward, env_info):
        """Attach PPO-facing reward data to the learned atomic action."""
        if sequence is None:
            return
        try:
            trace = getattr(self, "evaluation_action_trace", ())
            if not 0 <= int(sequence) < len(trace):
                return
            transition, metadata = self._sanitize_replay_value({
                    "reward": step_reward,
                    "components": env_info.get("reward_components"),
                    "diagnostics": env_info.get("reward_diagnostics"),
                    "reward_contract": env_info.get("reward_contract"),
                }, max_bytes=self.EVALUATION_TRACE_ENTRY_MAX_BYTES // 4)
            trace[int(sequence)]["learned_transition"] = transition
            trace_stats = self._evaluation_capture["trace"]
            trace_stats["sanitization_omissions"] += int(
                metadata.get("omissions", 0))
            trace_stats["serialization_errors"] += int(
                metadata.get("errors", 0))
            # Reward attachment happens after the initial entry accounting.
            # Recompute exactly so the whole-trace byte ceiling stays true.
            encoded = json.dumps(
                trace[int(sequence)], sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            without_transition = dict(trace[int(sequence)])
            without_transition.pop("learned_transition", None)
            prior_size = len(json.dumps(
                without_transition, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8"))
            delta = max(0, len(encoded) - prior_size)
            if len(encoded) > self.EVALUATION_TRACE_ENTRY_MAX_BYTES \
                    or trace_stats["serialized_bytes"] + delta \
                    > self.EVALUATION_TRACE_MAX_BYTES:
                trace[int(sequence)].pop("learned_transition", None)
                trace_stats["sanitization_omissions"] += 1
            else:
                trace_stats["serialized_bytes"] += delta
        except Exception as error:
            self._note_evaluation_capture_error(
                "attach_transition_reward", error, scope="trace")

    def _record_learned_replay_action(
            self, action_idx, context, *, trace_sequence, step_reward,
            done, truncated):
        """Record the learned decision path accepted by :meth:`replay`."""
        evaluation_enabled = self._evaluation_trace_enabled()
        try:
            try:
                context_copy = dict(context or {})
            except Exception as error:
                if not evaluation_enabled:
                    raise
                self._note_evaluation_capture_error(
                    "replay_context_copy", error, scope="replay")
                context_copy = {"__diagnostic_omitted__": {
                    "reason": "context_copy_error",
                    "error_type": self._evaluation_error_text(error)[0],
                }}
            entry = {"action": int(action_idx), "context": context_copy}
            if not evaluation_enabled:
                self.replay_actions.append(entry)
                return

            replay_stats = self._evaluation_capture["replay"]
            if replay_stats["recorded_events"] \
                    >= self.EVALUATION_REPLAY_MAX_EVENTS:
                replay_stats["dropped_events"] += 1
                return
            gs = getattr(self, "game_state", None)
            phase = getattr(gs, "phase", None)
            entry.update(self._evaluation_action_description(action_idx, context))
            entry["trace_sequence"] = trace_sequence
            entry["post_step"] = {
                "episode_step": int(getattr(self, "current_step", 0)),
                "turn": getattr(gs, "turn", None),
                "phase": phase,
                "phase_name": getattr(gs, "_PHASE_NAMES", {}).get(
                    phase, str(phase) if phase is not None else None),
                "reward": step_reward,
                "done": bool(done),
                "truncated": bool(truncated),
            }
            safe_entry, sanitization = self._sanitize_replay_value(
                entry, max_bytes=self.EVALUATION_REPLAY_ENTRY_MAX_BYTES)
            replay_stats["sanitization_omissions"] += int(
                sanitization.get("omissions", 0))
            replay_stats["serialization_errors"] += int(
                sanitization.get("errors", 0))
            if not isinstance(safe_entry, dict) \
                    or "__diagnostic_omitted__" in safe_entry:
                safe_entry = {
                    "action": int(action_idx),
                    "context": {"__diagnostic_omitted__": {
                        "reason": "replay_entry_byte_budget"}},
                    "trace_sequence": trace_sequence,
                    "post_step": {
                        "episode_step": int(getattr(self, "current_step", 0)),
                        "reward": self._json_safe_replay_value(step_reward),
                        "done": bool(done),
                        "truncated": bool(truncated),
                    },
                    "serialization": sanitization,
                }
            elif sanitization.get("omissions") \
                    or sanitization.get("errors"):
                safe_entry["serialization"] = sanitization
            encoded = json.dumps(
                safe_entry, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")
            added_bytes = len(encoded) + int(
                replay_stats["recorded_events"] > 0)
            if len(encoded) > self.EVALUATION_REPLAY_ENTRY_MAX_BYTES \
                    or replay_stats["serialized_bytes"] + added_bytes \
                    > self.EVALUATION_REPLAY_MAX_BYTES:
                replay_stats["dropped_events"] += 1
                return
            self.replay_actions.append(safe_entry)
            replay_stats["recorded_events"] += 1
            replay_stats["serialized_bytes"] += added_bytes
        except Exception as error:
            if evaluation_enabled:
                self._evaluation_capture["replay"]["dropped_events"] += 1
                self._note_evaluation_capture_error(
                    "record_replay_action", error, scope="replay")
                return
            raise

    def _evaluation_terminal_debug_payload(
            self, env_info, step_reward, done, truncated):
        """Build the successful-evaluation game artifact stored in history."""
        if not self._evaluation_trace_enabled():
            return None
        raw_terminal = {
            "game_result": env_info.get("game_result"),
            "terminal_reason": env_info.get("terminal_reason"),
            "reward": step_reward,
            "done": bool(done),
            "truncated": bool(truncated),
            "reward_components": env_info.get("reward_components"),
            "reward_diagnostics": env_info.get("reward_diagnostics"),
            "reward_contract": env_info.get("reward_contract"),
            "policy_state": env_info.get("policy_state"),
            "fidelity": env_info.get("fidelity"),
            "final_state": self._safe_evaluation_state_snapshot(
                "terminal_final_state"),
        }
        terminal, terminal_sanitization = self._sanitize_replay_value(
            raw_terminal, max_bytes=self.EVALUATION_TRACE_ENTRY_MAX_BYTES)
        terminal_stats = self._evaluation_capture["terminal"]
        terminal_stats["sanitization_omissions"] += int(
            terminal_sanitization.get("omissions", 0))
        terminal_stats["serialization_errors"] += int(
            terminal_sanitization.get("errors", 0))
        payload = {
            "schema_version": 1,
            "evaluation_timestep": self.evaluation_timestep,
            "evaluation_checkpoint_sha256":
                self.evaluation_checkpoint_sha256,
            "card_catalog": self._evaluation_card_catalog(),
            "replay": self.export_replay(),
            "trace": list(getattr(self, "evaluation_action_trace", ())),
            "terminal": terminal,
        }
        unattached = self._drain_evaluator_diagnostics()
        if unattached is not None:
            unattached["capture_scope"] = "post-terminal-observation"
        evaluator_summary = self._evaluator_diagnostic_totals()
        if evaluator_summary is not None or unattached is not None:
            payload["evaluator"] = {
                "summary": evaluator_summary,
                "unattached": unattached,
            }
        return self._enforce_evaluation_debug_payload_budget(payload)

    def _evaluation_card_catalog(self):
        """Return a bounded runtime-ID identity map for trace inspection.

        State snapshots intentionally store physical runtime IDs so repeated
        copies remain distinguishable.  Persisting the matching names here
        lets a debugger explain hand/zone/stack deltas without loading the
        full card database or guessing from evaluator calls.  Bounded printed
        characteristics support offline visual card labels while remaining
        immutable identity metadata.  This is diagnostic metadata only and
        never participates in replay.
        """
        gs = getattr(self, "game_state", None)
        if gs is None:
            return {
                "schema_version": 1,
                "entries": [],
                "recorded_entries": 0,
                "omitted_entries": 0,
            }

        printings = dict(
            getattr(gs, "card_instance_printings", {}) or {})
        owners = dict(getattr(gs, "card_instance_owners", {}) or {})
        card_db = getattr(gs, "card_db", {}) or {}
        ceased_tokens = getattr(gs, "_ceased_token_cards", {}) or {}

        limit = int(self.EVALUATION_CARD_CATALOG_MAX_ENTRIES)
        candidate_limit = limit * 4
        ordered_ids = []
        seen_ids = set()
        candidate_scan_truncated = False

        def add_id(card_id, *, force=False):
            nonlocal candidate_scan_truncated
            if isinstance(card_id, Card):
                card_id = getattr(card_id, "card_id", None)
            try:
                if card_id is None or card_id in seen_ids:
                    return
                resolvable = (
                    force or card_id in printings or card_id in card_db
                    or card_id in ceased_tokens)
            except (TypeError, ValueError):
                return
            if not resolvable:
                return
            if len(ordered_ids) >= candidate_limit:
                candidate_scan_truncated = True
                return
            seen_ids.add(card_id)
            ordered_ids.append(card_id)

        # Physical deck instances remain first so normal game cards cannot be
        # displaced by pathological choice/trace data.
        for card_id in printings:
            add_id(card_id, force=True)

        zone_names = (
            "library", "hand", "battlefield", "graveyard", "exile",
            "outside_game", "sideboard", "command_zone", "tokens")
        for player in (getattr(gs, "p1", None), getattr(gs, "p2", None)):
            if not isinstance(player, dict):
                continue
            for zone_name in zone_names:
                raw_cards = player.get(zone_name, ()) or ()
                if isinstance(raw_cards, dict):
                    raw_cards = raw_cards.keys()
                try:
                    for card_id in raw_cards:
                        add_id(card_id)
                except TypeError:
                    continue

        for item in list(getattr(gs, "stack", ()) or ())[:128]:
            if isinstance(item, tuple) and len(item) > 1:
                add_id(item[1])

        identity_keys = {
            "card_id", "source_id", "target_id", "target_card_id",
            # Canonical IDs are database identities, not physical runtime
            # references, and can numerically collide with runtime IDs.
            "runtime_card_id", "attacker_id",
            "blocker_id", "land_id", "permanent_id", "creature_id",
            "connive_creature_id", "discard_card_id", "sacrifice_card_id",
        }
        identity_collections = {
            # Generic options/targets are deliberately excluded: mode,
            # player, and battlefield-slot numbers can coincidentally equal a
            # live card ID.  Only collections whose key promises card IDs are
            # safe to resolve here; scalar source/target card-ID keys are
            # handled by ``identity_keys`` above.
            "cards", "card_ids", "target_ids", "selected_target_ids",
            "valid_target_ids", "tapped_permanents",
        }

        def collect_references(value, key_hint=None, depth=0):
            if depth > 8 or candidate_scan_truncated:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    key_text = str(key).casefold()
                    if (key_text in identity_keys
                            or (key_text != "canonical_card_id"
                                and key_text.endswith(
                                    ("_card_id", "_source_id")))):
                        if isinstance(item, (list, tuple, set, frozenset)):
                            for card_id in item:
                                add_id(card_id)
                        else:
                            add_id(item)
                    else:
                        collect_references(item, key_text, depth + 1)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    if key_hint in identity_collections \
                            and not isinstance(item, (dict, list, tuple,
                                                       set, frozenset)):
                        add_id(item)
                    else:
                        collect_references(item, key_hint, depth + 1)
                return
            if key_hint in identity_collections:
                add_id(value)

        # Earlier cards may no longer be in a live zone at terminal time.
        # Trace contexts/snapshots retain their IDs, so use them as a second
        # bounded source of identity candidates.
        collect_references(getattr(self, "evaluation_action_trace", ()))
        collect_references(getattr(gs, "targeting_context", None))
        collect_references(getattr(gs, "choice_context", None))
        collect_references(getattr(gs, "sacrifice_context", None))

        # Tokens are additive and include ceased objects so earlier trace
        # transitions remain nameable after state-based actions remove them.
        token_ids = {
            card_id for card_id in (*card_db.keys(), *ceased_tokens.keys())
            if isinstance(card_id, str) and card_id.startswith("TOKEN_")
        }
        for card_id in sorted(token_ids, key=str):
            add_id(card_id)
        selected_ids = ordered_ids[:limit]

        def player_owner(card_id):
            owner = owners.get(card_id)
            if owner in ("p1", "p2"):
                return owner
            for label, player in (
                    ("p1", getattr(gs, "p1", None)),
                    ("p2", getattr(gs, "p2", None))):
                if not isinstance(player, dict):
                    continue
                if any(card_id in (player.get(zone, ()) or ()) for zone in (
                        "library", "hand", "battlefield", "graveyard",
                        "exile", "outside_game", "sideboard",
                        "command_zone", "tokens")):
                    return label
            last_location = getattr(gs, "_last_card_locations", {}).get(
                card_id)
            if isinstance(last_location, tuple) and last_location:
                return self._evaluation_player_label(last_location[0])
            return None

        entries = []
        for runtime_id in selected_ids:
            card = card_db.get(runtime_id)
            if card is None:
                card = ceased_tokens.get(runtime_id)
            canonical_id = printings.get(runtime_id, runtime_id)
            printed = getattr(card, "_printed", None)
            if not isinstance(printed, dict):
                printed = {}
            entry = {
                "runtime_id": self._json_safe_replay_value(runtime_id),
                "canonical_id": self._json_safe_replay_value(canonical_id),
                "name": str(getattr(
                    card, "name", f"Unknown Card {runtime_id}")),
                "owner": player_owner(runtime_id),
            }
            type_line = printed.get(
                "type_line", getattr(card, "type_line", None))
            if type_line:
                entry["type_line"] = str(type_line)
            mana_cost = printed.get(
                "mana_cost", getattr(card, "mana_cost", None))
            if mana_cost:
                entry["mana_cost"] = str(mana_cost)
            oracle_text = printed.get(
                "oracle_text", getattr(card, "oracle_text", None))
            if oracle_text:
                entry["oracle_text"] = str(oracle_text)[:2048]
            type_text = str(type_line or "").casefold()
            characteristic_fields = []
            if "creature" in type_text:
                characteristic_fields.extend((
                    ("base_power", "power", "power"),
                    ("base_toughness", "toughness", "toughness"),
                ))
            if "planeswalker" in type_text:
                characteristic_fields.append(
                    ("base_loyalty", "loyalty", "loyalty"))
            if "battle" in type_text:
                characteristic_fields.append(
                    ("base_defense", "defense", "defense"))
            for output_key, printed_key, attribute in characteristic_fields:
                amount = printed.get(
                    printed_key, getattr(card, attribute, None))
                if amount is not None:
                    entry[output_key] = self._json_safe_replay_value(amount)
            colors = printed.get("colors", getattr(card, "colors", None))
            if isinstance(colors, (list, tuple)):
                entry["colors"] = self._json_safe_replay_value(
                    list(colors)[:5])
            entry["is_token"] = bool(
                isinstance(runtime_id, str)
                and runtime_id.startswith("TOKEN_"))
            entries.append(entry)
        return {
            "schema_version": 1,
            "entries": entries,
            "recorded_entries": len(entries),
            "omitted_entries": max(0, len(ordered_ids) - len(entries)),
            "entry_limit": limit,
            "candidate_limit": candidate_limit,
            "candidate_scan_truncated": candidate_scan_truncated,
        }

    @staticmethod
    def _compact_json_bytes(payload):
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")

    def _enforce_evaluation_debug_payload_budget(self, payload):
        """Publish exact capture accounting under one aggregate byte ceiling."""
        trace = payload.get("trace")
        replay = payload.get("replay")
        replay_actions = replay.get("actions") \
            if isinstance(replay, dict) else None
        trace_stats = self._evaluation_capture["trace"]
        replay_stats = self._evaluation_capture["replay"]
        terminal_stats = self._evaluation_capture["terminal"]

        def attach_capture_and_encode():
            capture, metadata = self._sanitize_replay_value(
                self._evaluation_capture,
                max_bytes=self.EVALUATION_TRACE_ENTRY_MAX_BYTES)
            terminal_stats["sanitization_omissions"] += int(
                metadata.get("omissions", 0))
            terminal_stats["serialization_errors"] += int(
                metadata.get("errors", 0))
            payload["capture"] = capture
            encoded = self._compact_json_bytes(payload)
            # The number of decimal digits in serialized_bytes can change the
            # size. Two passes reach a stable exact fixed point in practice.
            for _ in range(3):
                terminal_stats["serialized_bytes"] = len(encoded)
                capture["terminal"]["serialized_bytes"] = len(encoded)
                updated = self._compact_json_bytes(payload)
                if len(updated) == len(encoded):
                    encoded = updated
                    break
                encoded = updated
            return encoded

        encoded = attach_capture_and_encode()
        if len(encoded) <= self.EVALUATION_DEBUG_MAX_BYTES:
            return payload

        # Unattached evaluator observations are the least attributable data.
        # Remove them before dropping any action/replay event.
        evaluator = payload.get("evaluator")
        if isinstance(evaluator, dict) and evaluator.get("unattached") is not None:
            evaluator["unattached"] = {
                "omission": {"reason": "debug_payload_byte_budget"}}
            terminal_stats["sanitization_omissions"] += 1
            encoded = attach_capture_and_encode()

        while len(encoded) > self.EVALUATION_DEBUG_MAX_BYTES \
                and isinstance(replay_actions, list) and replay_actions:
            removed = replay_actions.pop()
            removed_size = len(self._compact_json_bytes(removed))
            separator_size = int(replay_stats["recorded_events"] > 1)
            replay_stats["recorded_events"] = max(
                0, replay_stats["recorded_events"] - 1)
            replay_stats["dropped_events"] += 1
            replay_stats["serialized_bytes"] = max(
                0, replay_stats["serialized_bytes"] - removed_size)
            replay_stats["serialized_bytes"] = max(
                2, replay_stats["serialized_bytes"] - separator_size)
            encoded = attach_capture_and_encode()

        while len(encoded) > self.EVALUATION_DEBUG_MAX_BYTES \
                and isinstance(trace, list) and trace:
            removed = trace.pop()
            removed_size = len(self._compact_json_bytes(removed))
            separator_size = int(trace_stats["recorded_events"] > 1)
            trace_stats["recorded_events"] = max(
                0, trace_stats["recorded_events"] - 1)
            trace_stats["dropped_events"] += 1
            trace_stats["serialized_bytes"] = max(
                0, trace_stats["serialized_bytes"] - removed_size)
            trace_stats["serialized_bytes"] = max(
                2, trace_stats["serialized_bytes"] - separator_size)
            encoded = attach_capture_and_encode()

        if len(encoded) > self.EVALUATION_DEBUG_MAX_BYTES:
            # Controlled fields alone should never reach this branch, but keep
            # the non-interference contract absolute if schemas grow later.
            terminal_stats["sanitization_omissions"] += 1
            payload = {
                "schema_version": 1,
                "evaluation_timestep": self.evaluation_timestep,
                "evaluation_checkpoint_sha256":
                    self.evaluation_checkpoint_sha256,
                "replay": {"version": 3, "actions": []},
                "trace": [],
                "terminal": {
                    "game_result": payload.get("terminal", {}).get(
                        "game_result"),
                    "terminal_reason": payload.get("terminal", {}).get(
                        "terminal_reason"),
                    "omission": {
                        "reason": "debug_payload_byte_budget"},
                },
                "capture": self._evaluation_capture,
            }
            encoded = self._compact_json_bytes(payload)
            terminal_stats["serialized_bytes"] = len(encoded)
            payload["capture"]["terminal"]["serialized_bytes"] = len(
                self._compact_json_bytes(payload))
        return payload

    def _safe_evaluation_terminal_debug_payload(
            self, env_info, step_reward, done, truncated):
        """Never let debug construction alter a completed game transition."""
        if not self._evaluation_trace_enabled():
            return None
        try:
            return self._evaluation_terminal_debug_payload(
                env_info, step_reward, done, truncated)
        except Exception as error:
            self._note_evaluation_capture_error(
                "terminal_debug_payload", error, scope="terminal")
            try:
                error_type = self._evaluation_error_text(error)[0]
                fallback = {
                    "schema_version": 1,
                    "evaluation_timestep": self.evaluation_timestep,
                    "evaluation_checkpoint_sha256":
                        self.evaluation_checkpoint_sha256,
                    "replay": {"version": 3, "actions": []},
                    "trace": [],
                    "terminal": {
                        "game_result": env_info.get("game_result"),
                        "terminal_reason": env_info.get("terminal_reason"),
                        "reward": self._evaluation_finite_number(step_reward),
                        "done": bool(done),
                        "truncated": bool(truncated),
                        "omission": {
                            "reason": "terminal_debug_capture_error",
                            "error_type": error_type,
                        },
                    },
                    "capture": self._evaluation_capture,
                }
                self._evaluation_capture["terminal"]["serialized_bytes"] = \
                    len(self._compact_json_bytes(fallback))
                return fallback
            except Exception:
                # Last-resort literals only; this branch still preserves the
                # already-computed environment result and reward.
                return {
                    "schema_version": 1,
                    "trace": [],
                    "replay": {"version": 3, "actions": []},
                    "terminal": {
                        "game_result": env_info.get("game_result"),
                        "terminal_reason": env_info.get("terminal_reason"),
                        "reward": self._evaluation_finite_number(step_reward),
                        "done": bool(done),
                        "truncated": bool(truncated),
                        "omission": {
                            "reason": "terminal_debug_capture_error"},
                    },
                    "capture": getattr(self, "_evaluation_capture", {
                        "errors": [{
                            "stage": "terminal_debug_payload",
                            "scope": "terminal",
                            "error_type": "unknown",
                        }],
                    }),
                }

    def _attach_evaluation_terminal_debug(
            self, env_info, step_reward, done, truncated):
        """Attach terminal telemetry without changing transition semantics."""
        if not (done or truncated) or not isinstance(env_info, dict):
            return env_info
        try:
            evaluation_debug = self._safe_evaluation_terminal_debug_payload(
                env_info, step_reward, done, truncated)
            if evaluation_debug is not None:
                env_info["evaluation_debug"] = evaluation_debug
        except Exception as error:
            # Telemetry is observational. Even failure of the final attachment
            # boundary must not alter reward/result/termination.
            try:
                self._note_evaluation_capture_error(
                    "attach_terminal_debug", error, scope="terminal")
            except Exception:
                pass
            logging.warning(
                "Could not attach evaluation terminal diagnostics: %s", error)
        return env_info

    def export_replay(self, path=None):
        """Return (and optionally persist) a deterministic episode replay."""
        payload = {
            "version": 3, "seed": self.reset_seed,
            "p1_deck": getattr(self, 'current_deck_name_p1', None),
            "p2_deck": getattr(self, 'current_deck_name_p2', None),
            "agent_is_p1": bool(getattr(self.game_state, 'agent_is_p1', True)),
            **self._episode_metadata(),
            "actions": list(self.replay_actions),
        }
        if path:
            header, header_metadata = self._sanitize_replay_value(
                {key: value for key, value in payload.items()
                 if key != "actions"},
                max_bytes=self.EVALUATION_REPLAY_ENTRY_MAX_BYTES)
            safe_actions = []
            action_bytes = 2
            dropped_actions = 0
            for action in payload["actions"]:
                if len(safe_actions) >= self.EVALUATION_REPLAY_MAX_EVENTS:
                    dropped_actions += 1
                    continue
                safe_action, _metadata = self._sanitize_replay_value(
                    action, max_bytes=self.EVALUATION_REPLAY_ENTRY_MAX_BYTES)
                encoded = self._compact_json_bytes(safe_action)
                added = len(encoded) + int(bool(safe_actions))
                if action_bytes + added > self.EVALUATION_REPLAY_MAX_BYTES:
                    dropped_actions += 1
                    continue
                safe_actions.append(safe_action)
                action_bytes += added
            safe_payload = dict(header) if isinstance(header, dict) else {
                "version": 3,
                "header_omission": header,
            }
            safe_payload["actions"] = safe_actions
            if dropped_actions or header_metadata.get("omissions") \
                    or header_metadata.get("errors"):
                safe_payload["serialization"] = {
                    "dropped_actions": dropped_actions,
                    "action_event_budget": self.EVALUATION_REPLAY_MAX_EVENTS,
                    "action_serialized_byte_budget":
                        self.EVALUATION_REPLAY_MAX_BYTES,
                    "header": header_metadata,
                }
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(
                    safe_payload, handle, indent=2, sort_keys=True,
                    allow_nan=False)
        return payload

    @classmethod
    def _sanitize_replay_value(cls, value, *, max_bytes=None):
        """Bound arbitrary diagnostic/replay values without address-based reprs."""
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
                return number if math.isfinite(number) else str(number)
            if isinstance(item, os.PathLike):
                try:
                    return walk(os.fspath(item), depth + 1)
                except Exception:
                    state["errors"] += 1
                    return omission("path_conversion_error", item)
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
                    result = [walk(child, depth + 1)
                              for child in flat[:limit].tolist()]
                    if flat.size > limit:
                        result.append(omission(
                            "container_items", item,
                            omitted_items=int(flat.size - limit),
                            shape=list(item.shape)))
                    return result
                if isinstance(item, dict):
                    result = {}
                    for index, (key, child) in enumerate(item.items()):
                        if index >= cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS:
                            result["__omitted_items__"] = max(
                                0, len(item)
                                - cls.DIAGNOSTIC_MAX_CONTAINER_ITEMS)
                            state["omissions"] += 1
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
                state["omissions"] += 1
                return summary
            except Exception:
                state["errors"] += 1
                return omission("sanitization_error", item)
            finally:
                if track_identity:
                    state["active"].discard(identity)

        safe = walk(value, 0)
        byte_limit = int(max_bytes or cls.EVALUATION_TRACE_ENTRY_MAX_BYTES)
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
            "nodes": int(state["nodes"]),
            "omissions": int(state["omissions"]),
            "errors": int(state["errors"]),
            "serialized_bytes": len(encoded),
        }

    @classmethod
    def _json_safe_replay_value(cls, value):
        safe, _metadata = cls._sanitize_replay_value(value)
        return safe

    def _persist_failure_replay(self, diagnostic, action_idx, context):
        """Atomically preserve the action path and terminal diagnostic."""
        replay_payload = self.export_replay()
        replay_payload["actions"].append({
            "action": int(action_idx),
            "context": dict(context or {}),
        })
        replay_payload["failure"] = diagnostic
        replay_path = os.path.join(self.deck_stats_path, "failure_replay.json")
        temporary_path = f"{replay_path}.tmp"
        os.makedirs(self.deck_stats_path, exist_ok=True)
        try:
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(
                    self._json_safe_replay_value(replay_payload), handle,
                    indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_path, replay_path)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise
        return replay_path

    def replay(self, payload):
        """Reset to a recorded seed and replay its agent action sequence."""
        if isinstance(payload, (str, os.PathLike)):
            with open(payload, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        try:
            replay_version = int(payload.get("version", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("Replay version must be an integer") from error
        if replay_version not in (1, 2, 3):
            raise ValueError(
                f"Unsupported replay version {replay_version}; supported "
                "versions are 1, 2, and 3")
        replay_seat = payload.get('agent_is_p1')
        if replay_seat is None:
            replay_seat = (payload.get('failure') or {}).get('agent_is_p1')
        obs, info = self.reset(
            seed=payload.get('seed'),
            options={
                'p1_deck': payload.get('p1_deck'),
                'p2_deck': payload.get('p2_deck'),
                'agent_is_p1': replay_seat,
                'opponent_profile': payload.get(
                    'opponent_profile', self.default_opponent_profile),
                'opponent_handicap': payload.get('opponent_handicap'),
                'max_turns': payload.get('max_turns'),
                'stage': payload.get('curriculum_stage'),
                'stage_index': payload.get('curriculum_stage_index'),
                'agent_deck': payload.get('agent_deck'),
                'opponent_deck': payload.get('opponent_deck'),
                'matchup_episode_index': payload.get(
                    'matchup_episode_index'),
            })
        if (payload.get('p1_deck') != self.current_deck_name_p1
                or payload.get('p2_deck') != self.current_deck_name_p2):
            raise ValueError("Replay deck selection does not match the recorded seed")
        result = (obs, 0.0, False, False, info)
        for entry in payload.get('actions', []):
            result = self.step(int(entry['action']), context=entry.get('context') or {})
            if result[2] or result[3]:
                break
        return result
        
    def _record_observation_error(self, section, error):
        """Preserve the first degraded feature so strict runs can reject it."""
        if self.last_observation_error is None:
            self.last_observation_error = (
                f"{section}: {type(error).__name__}: {error}")
            import traceback
            current_traceback = traceback.format_exc()
            if not current_traceback.startswith("NoneType: None"):
                self.last_observation_traceback = current_traceback
            return True
        return False

    def _coerce_observation(self, obs):
        """Return an observation that strictly conforms to ``observation_space``.

        Feature helpers are intentionally independent, so this is the final API
        boundary: malformed features are replaced, finite values are cast, and
        genuinely out-of-range values are saturated instead of leaking an invalid
        Gymnasium observation into a rollout.
        """
        normalized = {}
        source = obs if isinstance(obs, dict) else {}
        for key, space in self.observation_space.spaces.items():
            value = source.get(key)
            try:
                array = np.asarray(value)
                if array.shape != space.shape:
                    raise ValueError(f"shape {array.shape}, expected {space.shape}")
                if np.issubdtype(array.dtype, np.floating):
                    if not np.all(np.isfinite(array)):
                        self._record_observation_error(
                            f"feature {key}",
                            ValueError("value contained NaN or infinity"))
                    array = np.nan_to_num(array, nan=0.0)
                # Bound values before narrowing the dtype. Casting a large
                # int64 directly to int32 can wrap it into an apparently valid
                # (but false) value that clipping can no longer repair.
                bounded = np.clip(array, space.low, space.high)
                if not np.array_equal(array, bounded):
                    if key in getattr(self, '_saturating_features', ()):
                        # Expected saturation of an unbounded game quantity
                        # (huge P/T, big mana): clip silently by design.
                        logging.debug(
                            "Observation feature '%s' saturated at its "
                            "declared bound.", key)
                        normalized[key] = bounded.astype(space.dtype, copy=False)
                        continue
                    first_bound_error = self._record_observation_error(
                        f"feature {key}",
                        ValueError("value exceeded declared observation bounds"))
                    if first_bound_error:
                        violation_index = tuple(
                            int(index) for index in
                            np.argwhere(np.not_equal(array, bounded))[0])
                        logging.warning(
                            "Observation feature '%s' exceeded its declared bounds; "
                            "index=%s value=%s bounds=[%s, %s]; the public "
                            "value was clipped.",
                            key, violation_index, array[violation_index],
                            space.low[violation_index], space.high[violation_index])
                normalized[key] = bounded.astype(space.dtype, copy=False)
            except Exception as exc:
                self._record_observation_error(f"feature {key}", exc)
                logging.error(
                    "Observation feature '%s' was malformed (%s); using zeros.",
                    key, exc)
                normalized[key] = np.zeros(space.shape, dtype=space.dtype)
        return normalized

    def _bounded_int_array(self, key, values):
        """Clamp raw game integers to their declared bounds BEFORE ndarray
        construction. Doubling/big-mana combos produce Python ints beyond
        C-long range, and ``np.array(value, dtype=int32)`` raises
        OverflowError before the post-build saturation clip ever runs —
        which also aborted the surrounding population block and degraded
        unrelated features (July 14 reward-v3 stop). Saturation here is
        by design, exactly like the declared ``_saturating_features``."""
        space = self.observation_space.spaces[key]
        low = np.asarray(space.low).reshape(-1)
        high = np.asarray(space.high).reshape(-1)
        clamped = [
            min(max(int(value), int(low[min(i, low.size - 1)])),
                int(high[min(i, high.size - 1)]))
            for i, value in enumerate(values)
        ]
        return np.array(clamped, dtype=space.dtype)

    def _get_obs_safe(self):
        """Build the real policy observation, falling back only on failure."""
        # Record phase transitions at the one seam every observation passes
        # through. gs._phase_history was only written by dead code, so the
        # declared phase_history observation was a constant -1 (July 14
        # observation audit). Appending only on change keeps repeated calls
        # in the same state idempotent.
        current_phase = int(getattr(self.game_state, 'phase', -1))
        history = getattr(self, '_observed_phase_history', None)
        if history is None:
            history = []
            self._observed_phase_history = history
        if not history or history[-1] != current_phase:
            history.append(current_phase)
            del history[:-5]
        try:
            return self._get_obs()
        except Exception as exc:
            self._record_observation_error("policy observation", exc)
            logging.critical(
                "Failed to build the policy observation: %s", exc,
                exc_info=True)
            return self._get_obs_fallback()

    def _get_obs_fallback(self):
        """Return a minimal observation dictionary after observation failure."""
        self._record_observation_error(
            "observation fallback", RuntimeError("fallback observation used"))
        gs = self.game_state
        obs = {} # Initialize empty dict

        try:
            # --- STEP 1: Ensure observation_space is accessible ---
            if not hasattr(self, 'observation_space') or not isinstance(self.observation_space, spaces.Dict):
                logging.critical("_get_obs_safe: observation_space is missing or invalid!")
                # If space is missing, we cannot reliably create the obs dict.
                # Return a very basic dict that SB3 *might* tolerate or error on gracefully.
                obs['action_mask'] = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                obs['action_mask'][11] = True; obs['action_mask'][12] = True;
                # Always ensure ability_features exists with appropriate shape
                obs['ability_features'] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                # Add other critical keys
                obs['phase'] = np.array([0], dtype=np.int32)
                obs['turn'] = np.array([1], dtype=np.int32)
                obs["my_life"] = np.array([0], dtype=np.int32)
                obs["opp_life"] = np.array([0], dtype=np.int32)
                return obs # Return minimal dict

            # --- STEP 2: Initialize obs based on VALID observation_space ---
            obs = {k: np.zeros(space.shape, dtype=space.dtype)
                for k, space in self.observation_space.spaces.items()}

            # --- CRITICAL: Explicitly verify critical keys exist ---
            critical_keys = ["ability_features", "ability_recommendations", "ability_timing"]
            for key in critical_keys:
                if key not in obs or obs[key] is None:
                    logging.critical(f"Critical key '{key}' missing after initialization! Re-creating.")
                    if key in self.observation_space.spaces:
                        space = self.observation_space.spaces[key]
                        obs[key] = np.zeros(space.shape, dtype=space.dtype)
                    else:
                        # Fallback if space definition is missing
                        logging.critical(f"'{key}' missing from observation_space too! Using fallback shape.")
                        if key == "ability_features":
                            obs[key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                        elif key == "ability_recommendations":
                            obs[key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
                        elif key == "ability_timing":
                            obs[key] = np.zeros((5,), dtype=np.float32)

            # --- STEP 3: GUARANTEE ALL Keys Exist with Correct Shape/Dtype ---
            # This loop is crucial. It verifies every key defined in the observation space.
            for key, space in self.observation_space.spaces.items():
                if key not in obs:
                    logging.critical(f"_get_obs_safe: Key '{key}' WAS MISSING! Re-initializing.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                elif not isinstance(obs[key], np.ndarray):
                    logging.critical(f"_get_obs_safe: Key '{key}' is not ndarray ({type(obs[key])})! Re-initializing.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                elif obs[key].shape != space.shape:
                    logging.critical(f"_get_obs_safe: Key '{key}' has WRONG SHAPE! Got {obs[key].shape}, expected {space.shape}. Re-initializing.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                elif obs[key].dtype != space.dtype:
                    logging.critical(f"_get_obs_safe: Key '{key}' has WRONG DTYPE! Got {obs[key].dtype}, expected {space.dtype}. Trying safe cast, then re-initializing.")
                    try:
                        obs[key] = obs[key].astype(space.dtype) # Attempt safe cast first
                    except Exception as cast_e:
                        logging.error(f"_get_obs_safe: Cast failed for key '{key}'. Re-initializing. Error: {cast_e}")
                        obs[key] = np.zeros(space.shape, dtype=space.dtype)

            # Final special check for critical keys that are causing issues
            for key in ["ability_features", "ability_recommendations", "ability_timing"]:
                if key not in obs or obs[key] is None:
                    logging.critical(f"CRITICAL ERROR: '{key}' STILL missing after all validation! Re-creating.")
                    if key in self.observation_space.spaces:
                        space = self.observation_space.spaces[key]
                        obs[key] = np.zeros(space.shape, dtype=space.dtype)
                    else:
                        if key == "ability_features":
                            obs[key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                        elif key == "ability_recommendations":
                            obs[key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
                        elif key == "ability_timing":
                            obs[key] = np.zeros((5,), dtype=np.float32)

        except Exception as outer_safe_e:
            logging.critical(f"CRITICAL Error within _get_obs_safe execution itself: {outer_safe_e}", exc_info=True)
            # --- Last Resort Fallback ---
            # Re-initialize obs completely to ensure structure matches space as best as possible
            try:
                if hasattr(self, 'observation_space') and isinstance(self.observation_space, spaces.Dict):
                    obs = {k: np.zeros(space.shape, dtype=space.dtype)
                        for k, space in self.observation_space.spaces.items()}
                else:
                    # Observation space broken, use hardcoded minimal dict
                    obs = {
                        "action_mask": np.zeros(self.ACTION_SPACE_SIZE, dtype=bool),
                        "ability_features": np.zeros((self.max_battlefield, 5), dtype=np.float32),
                        "phase": np.array([0], dtype=np.int32),
                        "turn": np.array([1], dtype=np.int32),
                        "my_life": np.array([0], dtype=np.int32),
                        "opp_life": np.array([0], dtype=np.int32),
                        "my_hand_count": np.array([0], dtype=np.int32),
                        "opp_hand_count": np.array([0], dtype=np.int32),
                        "my_battlefield_count": np.array([0], dtype=np.int32),
                        "opp_battlefield_count": np.array([0], dtype=np.int32),
                    }
                # Ensure crucial keys are present
                obs["action_mask"][11] = True; obs["action_mask"][12] = True;
                
                # Explicit ability_features guarantee
                if "ability_features" not in obs:
                    logging.critical("FINAL RESORT: Setting ability_features manually!")
                    obs["ability_features"] = np.zeros((self.max_battlefield, 5), dtype=np.float32)

                # Final safety check for ALL expected keys in this ultimate fallback
                if hasattr(self, 'observation_space') and isinstance(self.observation_space, spaces.Dict):
                    for key, space in self.observation_space.spaces.items():
                        if key not in obs: 
                            obs[key] = np.zeros(space.shape, dtype=space.dtype)

            except Exception as final_fallback_e:
                logging.critical(f"Failed to even create ULTIMATE FALLBACK observation: {final_fallback_e}")
                # Return the simplest possible dictionary that includes critical keys
                return {
                    "action_mask": np.zeros(self.ACTION_SPACE_SIZE, dtype=bool),
                    "ability_features": np.zeros((self.max_battlefield, 5), dtype=np.float32),
                    "ability_recommendations": np.zeros((self.max_battlefield, 5, 2), dtype=np.float32),
                    "ability_timing": np.zeros((5,), dtype=np.float32),
                    "phase": np.array([0], dtype=np.int32)
                }
        
        # Final verification before returning
        for key in ["ability_features", "ability_recommendations", "ability_timing"]:
            if key not in obs:
                logging.critical(f"{key} STILL MISSING after all fixes! Adding as final solution!")
                if key == "ability_features":
                    obs[key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                elif key == "ability_recommendations":
                    obs[key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
                elif key == "ability_timing":
                    obs[key] = np.zeros((5,), dtype=np.float32)
        if "action_mask" in obs and len(obs["action_mask"]) == self.ACTION_SPACE_SIZE:
            try:
                # Even an observation fallback must preserve the public mask
                # contract.  Adding PASS/CONCEDE unconditionally can expose an
                # action that the current special phase will reject.
                obs["action_mask"] = self.action_mask().astype(bool)
            except Exception:
                obs["action_mask"] = np.zeros(
                    self.ACTION_SPACE_SIZE, dtype=bool)
                obs["action_mask"][12] = True
        return self._coerce_observation(obs)

    @staticmethod
    def _result_for_fixed_agent(result, actor_is_p1, agent_is_p1):
        """Translate an acting-seat result to the learned policy's seat."""
        if result == "draw":
            return "draw"
        if result not in ("win", "loss"):
            return result
        actor_won = result == "win"
        p1_won = bool(actor_is_p1) == actor_won
        return "win" if bool(agent_is_p1) == p1_won else "loss"

    def _canonical_terminal_result(self, fallback, agent_is_p1=None):
        """Return a terminal result from the learned policy's fixed seat.

        Action handlers describe results from their current acting seat. That
        seat is deliberately flipped during scripted-opponent simulation, so
        state flags are the source of truth at the environment boundary.
        ``fallback`` is already translated by the caller when the state has no
        independently adjudicable result (for example, a handler error).
        """
        gs = self.game_state
        agent_is_p1 = (gs.agent_is_p1 if agent_is_p1 is None
                       else bool(agent_is_p1))
        p1, p2 = gs.p1, gs.p2
        if not p1 or not p2:
            return fallback

        def lost(player):
            return bool(
                player.get("lost_game", False)
                or player.get("life", 20) <= 0
                or player.get("attempted_draw_from_empty", False)
                or player.get("poison_counters", 0) >= 10)

        p1_lost, p2_lost = lost(p1), lost(p2)
        if p1_lost and p2_lost:
            return "draw"
        if p1_lost:
            return "loss" if agent_is_p1 else "win"
        if p2_lost:
            return "win" if agent_is_p1 else "loss"
        if p1.get("game_draw", False) or p2.get("game_draw", False):
            return "draw"
        if p1.get("won_game", False) and p2.get("won_game", False):
            return "draw"
        if p1.get("won_game", False):
            return "win" if agent_is_p1 else "loss"
        if p2.get("won_game", False):
            return "loss" if agent_is_p1 else "win"
        if (getattr(gs, "terminal_reason", None) == "turn_limit"
                or gs.turn > gs.max_turns):
            my_life = p1.get("life", 0) if agent_is_p1 else p2.get("life", 0)
            opp_life = p2.get("life", 0) if agent_is_p1 else p1.get("life", 0)
            if my_life > opp_life:
                return "win"
            if my_life < opp_life:
                return "loss"
            return "draw"
        return fallback

    def _remaining_turn_fraction(self):
        """Fraction of the ENGINE turn budget left when the game ended.

        Measured against the stationary env ceiling, not the stage-limited
        ``gs.max_turns``, so the win speed premium means the same thing in
        every curriculum stage.
        """
        ceiling = max(1, int(self.max_turns))
        turn = int(getattr(self.game_state, "turn", ceiling) or ceiling)
        return float(np.clip((ceiling - turn) / ceiling, 0.0, 1.0))

    def _opponent_damage_progress(self):
        """How far the opponent moved toward lethal, 0.0 (untouched) to 1.0."""
        gs = self.game_state
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        if not opp:
            return 0.0
        return float(np.clip(
            (20.0 - float(opp.get("life", 20))) / 20.0, 0.0, 1.0))

    def _terminal_outcome_reward(self, terminal_reason, result):
        """Map a terminal category/result to contract tempo-graded-potential-v1.

        Rounds 7.88-7.93 all converged on timeout-dominant play (76-88% of
        episodes) under flat +-10 terminals: wins paid the same at turn 25 as
        at turn 8, and the binary turn-limit grade (-8 ahead / -10 behind)
        left no gradient across "how close to lethal you got".  This contract
        makes tempo part of the outcome itself:

        - decisive wins earn a bounded speed premium against the engine turn
          ceiling (faster kill, higher reward; never below +10);
        - turn-limit stalls grade continuously on opponent damage, capped
          strictly below a real draw, and ignore the result label so lifegain
          cannot cushion the penalty;
        - real draws pay a clearly negative -3 instead of the former near
          neutral -0.25, which had made "not losing" a refuge;
        - losses and engine safety terminations stay fail-closed at -10.
        """
        if terminal_reason == "turn_limit":
            return -10.0 + (
                self.TIMEOUT_DAMAGE_CREDIT * self._opponent_damage_progress())
        if terminal_reason == "episode_step_limit":
            return -10.0
        if str(result).startswith(("error", "invalid")):
            return -10.0
        if result == "win":
            return 10.0 + (
                self.WIN_SPEED_BONUS * self._remaining_turn_fraction())
        if result == "loss":
            return -10.0
        return self.DRAW_REWARD

    def _failure_transition_reward(
            self, info, previous_state_potential, raw_action_reward=0.0):
        """Apply the live reward contract to fail-closed early-return branches."""
        action_reward = self.action_reward_scale * float(raw_action_reward)
        try:
            current_state_potential = self._calculate_state_potential()
            state_change_reward = self._state_potential_reward(
                previous_state_potential, current_state_potential,
                terminal=True)
        except Exception as potential_error:
            logging.error(
                "Could not calculate terminal failure potential: %s",
                potential_error)
            current_state_potential = float(previous_state_potential)
            state_change_reward = 0.0
        time_cost_reward = -self.time_cost_per_step
        terminal_reason = (
            info.get("terminal_reason") or self._terminal_reason(info))
        result = info.get("game_result", "error")
        terminal_reward = self._terminal_outcome_reward(
            terminal_reason, result)
        info["terminal_reason"] = terminal_reason
        info["state_change_reward"] = float(state_change_reward)
        info["state_potential"] = float(current_state_potential)
        info["board_state_reward"] = float(state_change_reward)
        info["board_state_potential"] = float(current_state_potential)
        info["reward_components"] = {
            "action": float(action_reward),
            "state_change": float(state_change_reward),
            "time": float(time_cost_reward),
            "terminal": float(terminal_reward),
        }
        info["reward_diagnostics"] = {
            "action_raw": float(raw_action_reward),
            "state_potential": float(current_state_potential),
            "state_potential_previous": float(previous_state_potential),
        }
        info["reward_contract"] = self.REWARD_CONTRACT_VERSION
        return float(action_reward + state_change_reward
                     + time_cost_reward + terminal_reward)

    def _terminal_reason(self, info=None):
            """Return a stable terminal category for logs and reward policy."""
            gs = self.game_state
            if getattr(gs, 'terminal_reason', None):
                return gs.terminal_reason
            if (info or {}).get('episode_step_limit'):
                return "episode_step_limit"
            players = [gs.p1, gs.p2]
            if any(p and p.get('attempted_draw_from_empty') for p in players):
                return "decking"
            if any(p and p.get('poison_counters', 0) >= 10 for p in players):
                return "poison"
            if gs.turn > gs.max_turns:
                return "turn_limit"
            if any(p and p.get('game_draw') for p in players):
                return "draw_effect"
            if any(p and p.get('won_game') for p in players):
                return "alternate_win"
            if any(p and p.get('life', 20) <= 0 for p in players):
                return "life_total"
            if (info or {}).get('game_result') in ('win', 'loss', 'draw'):
                return "state_based_result"
            return "unknown"

    def _check_game_end_conditions(self, info):
            """Helper to check standard game end conditions and update info dict."""
            gs = self.game_state
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            done = False

            # Standard Loss Conditions
            if opp.get("lost_game", False):
                done = True; info["game_result"] = "win"
            elif me.get("lost_game", False):
                done = True; info["game_result"] = "loss"
            # Draw Conditions
            elif me.get("game_draw", False) or opp.get("game_draw", False):
                done = True; info["game_result"] = "draw"
            # Turn Limit
            elif gs.turn > gs.max_turns:
                # Mark as truncated? No, it's a game end condition.
                done = True
                # Determine result based on life
                info["game_result"] = "win" if (me["life"] > opp["life"]) else "loss" if (me["life"] < opp["life"]) else "draw"
                gs.terminal_reason = "turn_limit"
                logging.info(f"Turn limit ({gs.max_turns}) reached. Result: {info['game_result']}")

            # Explicit Win Flags (Alternative win conditions etc.)
            elif me.get("won_game", False):
                done = True; info["game_result"] = "win"
            elif opp.get("won_game", False):
                done = True; info["game_result"] = "loss"

            return done
        
    def get_strategic_recommendation(self):
        """
        Get a strategic action recommendation.
        This allows the agent to incorporate strategic planning into its decision making.
        
        Returns:
            Tuple of (action_idx, confidence) or None if no recommendation
        """
        if not hasattr(self, 'strategic_planner'):
            return None
            
        valid_actions = np.where(self.current_valid_actions)[0]
        if not valid_actions.size:
            return None
            
        # Get recommendation and confidence level
        try:
            action_idx = self.strategic_planner.recommend_action(valid_actions)
            
            # Estimate confidence based on position analysis
            confidence = 0.7  # Default confidence
            if hasattr(self.strategic_planner, 'current_analysis'):
                analysis = self.strategic_planner.current_analysis
                if analysis and 'position' in analysis:
                    position = analysis['position']['overall']
                    # Higher confidence when position is clear
                    if position in ['dominating', 'struggling']:
                        confidence = 0.9
                    elif position in ['ahead', 'behind']:
                        confidence = 0.8
                        
            return (action_idx, confidence)
        except Exception as e:
            logging.warning(f"Error getting strategic recommendation: {e}")
            return None

    def _calculate_state_potential(self):
        """Return the bounded strategic potential for the agent's position.

        Contract tempo-graded-potential-v1 (July 17): four straight runs
        (7.88-7.93) stalled into timeout-dominant play while the potential
        still paid for defensive accumulation.  The overhauled potential is
        an offense instrument:

        - the hand-size term is gone — card hoarding is not progress toward
          ending a game and taught exactly the wrong lesson to a staller;
        - the symmetric life-difference weight drops to 0.10 so lifegain
          defense no longer rivals dealing damage;
        - the convex damage ramp is the dominant term (weight 1.0, slope
          rising from 0.35x at full life to 1.65x near lethal), so the last
          points of damage are worth the most and the kill is never left on
          the table.

        The potential stays clipped at +-2 and is consumed only through the
        discounted potential-difference shaping, which telescopes and cannot
        change the optimal policy.
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        if not me or not opp:
            return 0.0

        def battlefield_value(player):
            value = 0.0
            for card_id in player.get('battlefield', []):
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                types = getattr(card, 'card_types', []) or []
                if 'creature' in types:
                    value += 1.0
                    value += 0.12 * float(getattr(card, 'power', 0) or 0)
                    value += 0.08 * float(getattr(card, 'toughness', 0) or 0)
                elif 'land' in types:
                    value += 0.35
                else:
                    value += 0.6
            return value

        life_component = 0.10 * (
            (me.get('life', 0) - opp.get('life', 0)) / 20.0)
        board_component = 0.15 * np.tanh(
            (battlefield_value(me) - battlefield_value(opp)) / 6.0)
        damage_taken = np.clip(
            (20.0 - float(opp.get('life', 20))) / 20.0, 0.0, 1.0)
        # Convex: slope grows from 0.35x at full life to 1.65x near lethal.
        damage_progress = 1.0 * (
            0.35 * damage_taken + 0.65 * damage_taken ** 2)
        return float(np.clip(
            life_component + board_component + damage_progress,
            -2.0, 2.0))

    def _state_potential_reward(self, previous, current, terminal=False):
        """Return discounted potential shaping without changing optimal policy."""
        next_potential = 0.0 if terminal else float(current)
        return float(self.state_potential_scale * (
            self.reward_discount * next_potential - float(previous)))

    def _calculate_board_state_reward(self):
        """Compatibility alias for the state potential used by older tooling."""
        return self._calculate_state_potential()

    def _legacy_absolute_board_state_reward(self):
        """Calculate a sophisticated MTG-specific board state reward with emphasis on early wins"""
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1

        # Safely get creatures with proper type checking
        my_creatures = []
        for cid in me["battlefield"]:
            card = gs._safe_get_card(cid)
            if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                my_creatures.append(cid)
                
        opp_creatures = []
        for cid in opp["battlefield"]:
            card = gs._safe_get_card(cid)
            if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                opp_creatures.append(cid)

        # Safely calculate power and toughness totals
        my_power = 0
        my_toughness = 0
        for cid in my_creatures:
            card = gs._safe_get_card(cid)
            if card and hasattr(card, 'power') and hasattr(card, 'toughness'):
                my_power += _card_number(card, 'power')
                my_toughness += _card_number(card, 'toughness')
                
        opp_power = 0
        opp_toughness = 0
        for cid in opp_creatures:
            card = gs._safe_get_card(cid)
            if card and hasattr(card, 'power') and hasattr(card, 'toughness'):
                opp_power += _card_number(card, 'power')
                opp_toughness += _card_number(card, 'toughness')

        my_creature_count = len(my_creatures)
        opp_creature_count = len(opp_creatures)
        my_cards_in_hand = len(me["hand"])
        opp_cards_in_hand = len(opp["hand"])
        my_cards_on_board = len(me["battlefield"])
        opp_cards_on_board = len(opp["battlefield"])
        
        # Card advantage metrics (fundamental MTG concept)
        card_advantage = (my_cards_in_hand + my_cards_on_board) - (opp_cards_in_hand + opp_cards_on_board)
        board_advantage = my_cards_on_board - opp_cards_on_board
        hand_advantage = my_cards_in_hand - opp_cards_in_hand

        my_lands = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        opp_lands = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        my_mana = len(my_lands)
        opp_mana = len(opp_lands)
        mana_advantage = my_mana - opp_mana

        life_difference = me["life"] - opp["life"]
        
        # Count evasive creatures (flying, unblockable, etc.)
        my_evasive = sum(1 for cid in my_creatures if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'oracle_text') and
                        any(keyword in gs._safe_get_card(cid).oracle_text.lower() 
                            for keyword in ['flying', 'can\'t be blocked']))
        opp_evasive = sum(1 for cid in opp_creatures if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'oracle_text') and
                        any(keyword in gs._safe_get_card(cid).oracle_text.lower() 
                            for keyword in ['flying', 'can\'t be blocked']))
        
        # Calculate combat potential
        my_combat_potential = my_power - min(opp_creature_count, my_creature_count) * 0.5
        opp_combat_potential = opp_power - min(opp_creature_count, my_creature_count) * 0.5
        
        # Calculate keyword advantage (flying, trample, etc.)
        my_keyword_count = sum(1 for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'keywords'))
        opp_keyword_count = sum(1 for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'keywords'))
        keyword_advantage = my_keyword_count - opp_keyword_count
        
        # Calculate average creature quality (power + toughness) for each player
        my_avg_creature_quality = (my_power + my_toughness) / max(1, my_creature_count)
        opp_avg_creature_quality = (opp_power + opp_toughness) / max(1, opp_creature_count)
        
        # MTG Tempo concept - measuring board development relative to mana invested
        my_tempo = sum(gs._safe_get_card(cid).cmc for cid in me["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')) / max(1, my_mana)
        opp_tempo = sum(gs._safe_get_card(cid).cmc for cid in opp["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')) / max(1, opp_mana)
        tempo_advantage = my_tempo - opp_tempo

        # Track life changes since last turn
        current_life_diff = me["life"] - opp["life"]
        prev_life_diff = getattr(self, "prev_life_diff", 0)
        life_diff_change = current_life_diff - prev_life_diff
        self.prev_life_diff = current_life_diff
        
        # Life threshold rewards - IMPROVED FOR FASTER WINS
        opponent_low_life = 0
        if opp["life"] <= 5:
            opponent_low_life = 0.5  # Significantly increased reward
        elif opp["life"] <= 10:
            opponent_low_life = 0.25  # Increased reward for getting opponent below 10
        elif opp["life"] <= 15:
            opponent_low_life = 0.15  # Moderate reward for getting below 15

        # MTG-specific board rewards with adjusted weights
        reward_components = {
            'power_diff': 0.008 * (my_power - opp_power),  # Increased weight
            'toughness_diff': 0.005 * (my_toughness - opp_toughness),  
            'creature_diff': 0.008 * (my_creature_count - opp_creature_count),  
            'evasive_diff': 0.015 * (my_evasive - opp_evasive),  # Increased weight for evasive creatures
            'card_advantage': 0.01 * card_advantage,  
            'mana_diff': 0.008 * mana_advantage, 
            'life_diff': 0.015 * life_difference,  # Increased weight for life difference
            'value_permanents': 0.008 * (sum(1 for cid in me["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'oracle_text') and 
                                        (':" ' in gs._safe_get_card(cid).oracle_text.lower() or 'activate' in gs._safe_get_card(cid).oracle_text.lower())) - 
                                sum(1 for cid in opp["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'oracle_text') and 
                                        (':" ' in gs._safe_get_card(cid).oracle_text.lower() or 'activate' in gs._safe_get_card(cid).oracle_text.lower()))),
            'combat_potential': 0.01 * (my_combat_potential - opp_combat_potential),  # Increased
            'keyword_advantage': 0.006 * keyword_advantage,
            'tempo_advantage': 0.01 * tempo_advantage,  # Increased weight
            'life_change_bonus': 0.08 * life_diff_change,  # Significantly increased for active damage
            'opponent_low_life': opponent_low_life,  # Higher value from earlier calculation
            'turn_progress': -0.02 * gs.turn  # NEW: Penalty for taking too many turns
        }
        
        # Calculate base board reward
        board_reward = sum(reward_components.values())

        # NEW: Early win multiplier - significantly scales up rewards when getting close to winning
        # This encourages faster wins
        early_turn_multiplier = max(1.0, 1.5 * (20 - gs.turn) / 10)  # Higher multiplier in early turns
        opp_life_factor = max(1.0, 2.0 * (20 - opp["life"]) / 20)  # Higher multiplier when opponent is low on life
        
        # Adjust for game phase
        if gs.turn <= 4:  # Early game
            # Early game: developing mana and board presence is crucial
            board_reward += 0.01 * min(my_mana, gs.turn)  # Reward for curve development
            if my_creature_count > 0:
                board_reward += 0.007 * min(my_creature_count, 3)  # Early creatures are valuable
                
            # Card draw is more valuable early
            if hand_advantage > 0:
                board_reward += 0.01 * hand_advantage
                
        elif 5 <= gs.turn <= 8:  # Mid game - shortened window
            # Mid game: board presence and card advantage become more important
            if my_creature_count >= 2:
                board_reward += 0.015  # Increased
                
            # Having better quality creatures matters
            if my_creature_count > 0 and opp_creature_count > 0:
                if my_avg_creature_quality > opp_avg_creature_quality:
                    board_reward += 0.015  # Increased
                    
            # Card advantage is critical in mid-game
            if card_advantage > 0:
                board_reward += 0.008 * card_advantage  # Increased
            
            # Added reward for reducing opponent's life total
            board_reward += 0.02 * (20 - opp["life"]) / 20  # More reward as opponent's life decreases
            
        else:  # Late game
            # Late game: big threats and life totals matter more
            high_power_count = sum(
                1 for cid in my_creatures if gs._safe_get_card(cid)
                and _card_number(gs._safe_get_card(cid), 'power') >= 4)
            if high_power_count > 0:
                board_reward += 0.015 * high_power_count  # Increased
                
            # Life difference more important late
            if life_difference > 0:
                board_reward += 0.01 * life_difference  # Increased
                
            # Being close to winning - SIGNIFICANTLY INCREASED
            if opp["life"] <= 5:
                board_reward += 0.12  # Doubled value for being close to winning
                    
            # Or being in danger of losing
            if me["life"] <= 5:
                board_reward -= 0.09  # Higher penalty for being close to losing
        
        # Mana curve consideration - penalize having lands but not using mana
        unused_mana = 0
        for color in ['W', 'U', 'B', 'R', 'G', 'C']:
            unused_mana += me["mana_pool"].get(color, 0)
        
        if gs.phase in [gs.PHASE_END_STEP, gs.PHASE_CLEANUP] and unused_mana > 2:
            waste_penalty = min(unused_mana * 0.015, 0.05)  # Increased penalty
            board_reward -= waste_penalty
            logging.debug(f"Wasted mana penalty: -{waste_penalty:.3f} for {unused_mana} unused mana")

        # NEW: Apply early win and life total multipliers
        board_reward *= (early_turn_multiplier * opp_life_factor)
        
        # Log detailed breakdown
        if abs(board_reward) > 0.01:  # Only log significant rewards
            logging.debug(f"Board state reward components: {reward_components}")
            logging.debug(f"Total board reward: {board_reward:.4f} (with multipliers: early turn {early_turn_multiplier:.2f}, opp life {opp_life_factor:.2f})")
            
        return board_reward

    def _calculate_card_synergies(self, player_cards):
        """Calculate synergy scores between cards in a player's control"""
        gs = self.game_state
        card_count = min(len(player_cards), self.max_battlefield)
        synergy_matrix = np.zeros((self.max_battlefield, self.max_battlefield), dtype=np.float32)
        
        for i, card1_id in enumerate(player_cards[:card_count]):
            card1 = gs._safe_get_card(card1_id)
            if not card1:
                continue
                
            for j, card2_id in enumerate(player_cards[:card_count]):
                if i == j:
                    continue
                    
                card2 = gs._safe_get_card(card2_id)
                if not card2:
                    continue
                
                # Creature type synergy
                shared_types = set(card1.subtypes).intersection(set(card2.subtypes))
                type_synergy = min(len(shared_types) * 0.2, 0.6)
                
                # Color synergy
                color_synergy = sum(c1 == c2 == 1 for c1, c2 in zip(card1.colors, card2.colors)) * 0.1
                
                # Ability synergy (check for complementary abilities)
                ability_synergy = 0
                if hasattr(card1, 'oracle_text') and hasattr(card2, 'oracle_text'):
                    # Deathtouch + first strike is powerful
                    if ("deathtouch" in card1.oracle_text.lower() and "first strike" in card2.oracle_text.lower()) or \
                    ("first strike" in card1.oracle_text.lower() and "deathtouch" in card2.oracle_text.lower()):
                        ability_synergy += 0.3
                    
                    # Flying + equipment/auras
                    if "flying" in card1.oracle_text.lower() and ("equip" in card2.oracle_text.lower() or "enchant creature" in card2.oracle_text.lower()):
                        ability_synergy += 0.2
                        
                    # Lifelink synergies
                    if "lifelink" in card1.oracle_text.lower() and "whenever you gain life" in card2.oracle_text.lower():
                        ability_synergy += 0.4
                
                synergy_matrix[i, j] = type_synergy + color_synergy + ability_synergy
                
        return synergy_matrix
    
    def _calculate_position_advantage(self):
        """Calculate overall position advantage considering multiple factors"""
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Card advantage (hand + battlefield)
        my_cards = len(me["hand"]) + len(me["battlefield"])
        opp_cards = len(opp["hand"]) + len(opp["battlefield"])
        card_advantage = (my_cards - opp_cards) / max(1, my_cards + opp_cards)
        
        # Mana advantage (both current and potential)
        my_lands = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        opp_lands = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
        mana_advantage = (len(my_lands) - len(opp_lands)) / max(1, len(my_lands) + len(opp_lands))
        
        # Board advantage
        my_creatures = [cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 
                    hasattr(gs._safe_get_card(cid), 'card_types') and 'creature' in gs._safe_get_card(cid).card_types]
        opp_creatures = [cid for cid in opp["battlefield"] if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 'creature' in gs._safe_get_card(cid).card_types]
        
        # Power/toughness advantage
        my_power = sum(
            _card_number(gs._safe_get_card(cid), 'power')
            for cid in my_creatures if gs._safe_get_card(cid))
        my_toughness = sum(
            _card_number(gs._safe_get_card(cid), 'toughness')
            for cid in my_creatures if gs._safe_get_card(cid))
        opp_power = sum(
            _card_number(gs._safe_get_card(cid), 'power')
            for cid in opp_creatures if gs._safe_get_card(cid))
        opp_toughness = sum(
            _card_number(gs._safe_get_card(cid), 'toughness')
            for cid in opp_creatures if gs._safe_get_card(cid))
        
        total_stats = max(1, my_power + my_toughness + opp_power + opp_toughness)
        board_advantage = (my_power + my_toughness - opp_power - opp_toughness) / total_stats
        
        # Life advantage
        life_advantage = (me["life"] - opp["life"]) / 40.0  # Normalize by total possible life
        
        # Quality of cards in hand (average mana value as a simple proxy)
        # Add more type checking to avoid attribute errors
        valid_hand_cards = [gs._safe_get_card(cid) for cid in me["hand"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')]
        my_hand_quality = np.mean([
            _card_number(card, 'cmc') for card in valid_hand_cards
        ]) if valid_hand_cards else 0
        opp_hand_quality = 3.0  # Assume average CMC for opponent's hand
        hand_quality_advantage = (my_hand_quality - opp_hand_quality) / max(1, my_hand_quality + opp_hand_quality)
        
        # Weighted overall advantage
        overall_advantage = (
            0.25 * card_advantage +
            0.15 * mana_advantage +
            0.30 * board_advantage +
            0.20 * life_advantage +
            0.10 * hand_quality_advantage
        )
        
        return np.clip(overall_advantage, -1.0, 1.0)
    
    def _log_episode_summary(self):
        logging.info(f"Episode ended with total reward: {sum(self.episode_rewards)} and {self.episode_invalid_actions} invalid actions.")
    
    def _get_card_feature(self, card_id, feature_dim):
        """
        Helper to safely retrieve a card's feature vector with proper dimensionality.
        If the card ID is invalid, returns a zero vector.
        """
        try:
            if (hasattr(self.game_state, "is_face_down_exile_card")
                    and self.game_state.is_face_down_exile_card(card_id)):
                return np.zeros(feature_dim, dtype=np.float32)
            # Use _safe_get_card instead of direct access
            card = self.game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'to_feature_vector'):
                return np.zeros(feature_dim, dtype=np.float32)
                
            # Get the feature vector
            feature_vector = card.to_feature_vector(
                subtype_vocab=self._subtype_vocab)
            
            # Ensure the vector has the expected dimension
            if len(feature_vector) != feature_dim:
                # If too short, pad with zeros
                if len(feature_vector) < feature_dim:
                    padded_vector = np.zeros(feature_dim, dtype=np.float32)
                    padded_vector[:len(feature_vector)] = feature_vector
                    return padded_vector
                # If too long, truncate
                else:
                    return feature_vector[:feature_dim]
                    
            return feature_vector
        except (KeyError, IndexError, AttributeError) as e:
            logging.error(f"Invalid card ID {card_id} or missing attribute: {str(e)}")
            return np.zeros(feature_dim, dtype=np.float32)
        
    def _get_obs(self):
        """Build the observation dictionary. Assumes helpers are implemented."""
        try:
            # 0. Ensure layer effects are applied first
            if hasattr(self, 'layer_system') and self.layer_system:
                try:
                    self.layer_system.apply_all_effects()
                except Exception as layer_e:
                    self._record_observation_error("layers", layer_e)
                    logging.error(f"Error applying layer effects in _get_obs: {layer_e}", exc_info=True)
                    # Continue generating obs, but layers might be inconsistent

            gs = self.game_state
            # Use GS helpers to get players
            agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1

            # 1. INITIALIZE obs with default values for ALL keys FIRST
            obs = {k: np.zeros(space.shape, dtype=space.dtype)
                for k, space in self.observation_space.spaces.items()}
            logging.debug(f"_get_obs: Initialized obs keys: {list(obs.keys())}") # Log initial keys

            # Ensure players are valid before proceeding with population
            if not agent_player_obj: raise ValueError("Agent player object is None in _get_obs")
            if not opp: raise ValueError("Opponent player object is None in _get_obs")

            # 2. Regenerate Action Mask (necessary for the observation itself)
            current_mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool) # Default mask
            if hasattr(self, 'action_handler') and self.action_handler is not None:
                try:
                    current_mask = self.action_mask()
                except Exception as mask_e:
                    self._record_observation_error("action mask", mask_e)
                    logging.error(f"Error generating action mask in _get_obs: {mask_e}", exc_info=True)
                    # Use default mask with pass/concede if generation fails
                    current_mask[11] = True; current_mask[12] = True;
            else:
                self._record_observation_error(
                    "action mask", RuntimeError("action handler unavailable"))
                logging.warning("ActionHandler not available in _get_obs, using default mask.")
                current_mask[11] = True; current_mask[12] = True;
            obs["action_mask"] = current_mask.astype(bool) # Assign the potentially corrected mask


            # --- 3. Populate Basic Game State Info ---
            is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
            current_phase = getattr(gs, 'phase', 0)
            current_turn = getattr(gs, 'turn', 1)
            keyword_dimension = len(Card.ALL_KEYWORDS) # Ensure this matches

            obs["phase"] = np.array([current_phase], dtype=np.int32)
            obs["turn"] = np.array([current_turn], dtype=np.int32)
            obs["is_my_turn"] = np.array([int(is_my_turn)], dtype=np.int32)
            obs["my_life"] = self._bounded_int_array("my_life", [agent_player_obj.get('life', 0)])
            obs["opp_life"] = self._bounded_int_array("opp_life", [opp.get('life', 0)])
            obs["life_difference"] = self._bounded_int_array("life_difference", [agent_player_obj.get('life', 0) - opp.get('life', 0)])

            # --- 4. Populate Zone Features and Related Info ---
            # Wrap potentially complex population blocks in try/excepts to prevent
            # errors in one section from stopping others, and ensuring defaults remain.
            try:
                # Hand state
                obs["my_hand"] = self._get_zone_features(agent_player_obj.get("hand", []), self.hand_observation_size)
                obs["my_hand_card_identity"] = self._get_zone_identities(
                    agent_player_obj.get("hand", []), self.hand_observation_size)
                obs["my_hand_count"] = np.array([len(agent_player_obj.get("hand", []))], dtype=np.int32)
                obs["opp_hand_count"] = np.array([len(opp.get("hand", []))], dtype=np.int32)
                obs["hand_card_types"] = self._get_hand_card_types(agent_player_obj.get("hand", []))
                obs["hand_playable"] = self._get_hand_playable(agent_player_obj.get("hand", []), agent_player_obj, is_my_turn)
                obs["hand_synergy_scores"] = self._get_hand_synergy_scores(agent_player_obj.get("hand", []), agent_player_obj.get("battlefield", []))
                obs["opportunity_assessment"] = self._get_opportunity_assessment(agent_player_obj.get("hand", []), agent_player_obj)
            except Exception as e:
                self._record_observation_error("hand features", e)
                logging.error(f"Error populating hand features in _get_obs: {e}", exc_info=True)
                # Keep default zero values initialized earlier

            try:
                # Battlefield state
                obs["my_battlefield"] = self._get_zone_features(agent_player_obj.get("battlefield", []), self.max_battlefield)
                obs["my_battlefield_card_identity"] = self._get_zone_identities(
                    agent_player_obj.get("battlefield", []), self.max_battlefield)
                obs["my_battlefield_flags"] = self._get_battlefield_flags(agent_player_obj.get("battlefield", []), agent_player_obj, self.max_battlefield)
                obs["opp_battlefield"] = self._get_zone_features(opp.get("battlefield", []), self.max_battlefield)
                obs["opp_battlefield_card_identity"] = self._get_zone_identities(
                    opp.get("battlefield", []), self.max_battlefield)
                obs["opp_battlefield_flags"] = self._get_battlefield_flags(opp.get("battlefield", []), opp, self.max_battlefield)
                obs["my_battlefield_count"] = self._bounded_int_array(
                    "my_battlefield_count",
                    [len(agent_player_obj.get("battlefield", []))])
                obs["opp_battlefield_count"] = self._bounded_int_array(
                    "opp_battlefield_count", [len(opp.get("battlefield", []))])
                (obs["my_permanent_counters"], obs["my_damage_marked"],
                 obs["my_attachment_targets"],
                 obs["my_attachment_counts"]) = self._get_permanent_public_state(
                    agent_player_obj, agent_player_obj, opp)
                (obs["opp_permanent_counters"], obs["opp_damage_marked"],
                 obs["opp_attachment_targets"],
                 obs["opp_attachment_counts"]) = self._get_permanent_public_state(
                    opp, agent_player_obj, opp)
            except Exception as e:
                self._record_observation_error("battlefield features", e)
                logging.error(f"Error populating battlefield features in _get_obs: {e}", exc_info=True)

            try:
                # Creature stats
                my_creature_stats = self._get_creature_stats(agent_player_obj.get("battlefield", []))
                opp_creature_stats = self._get_creature_stats(opp.get("battlefield", []))
                obs["my_creature_count"] = self._bounded_int_array("my_creature_count", [my_creature_stats['count']])
                obs["opp_creature_count"] = self._bounded_int_array("opp_creature_count", [opp_creature_stats['count']])
                obs["my_total_power"] = self._bounded_int_array("my_total_power", [my_creature_stats['power']])
                obs["my_total_toughness"] = self._bounded_int_array("my_total_toughness", [my_creature_stats['toughness']])
                obs["opp_total_power"] = self._bounded_int_array("opp_total_power", [opp_creature_stats['power']])
                obs["opp_total_toughness"] = self._bounded_int_array("opp_total_toughness", [opp_creature_stats['toughness']])
                obs["power_advantage"] = self._bounded_int_array("power_advantage", [my_creature_stats['power'] - opp_creature_stats['power']])
                obs["toughness_advantage"] = self._bounded_int_array("toughness_advantage", [my_creature_stats['toughness'] - opp_creature_stats['toughness']])
                obs["creature_advantage"] = self._bounded_int_array("creature_advantage", [my_creature_stats['count'] - opp_creature_stats['count']])
                obs["threat_assessment"] = self._get_threat_assessment(opp.get("battlefield", []))
                obs["card_synergy_scores"] = self._calculate_card_synergies(agent_player_obj.get("battlefield", []))
            except Exception as e:
                self._record_observation_error("creature features", e)
                logging.error(f"Error populating creature stats in _get_obs: {e}", exc_info=True)

            try:
                # Mana state
                obs["my_mana_pool"] = self._mana_pool_vector(
                    agent_player_obj, "mana_pool", "my_mana_pool")
                obs["opp_mana_pool"] = self._mana_pool_vector(
                    opp, "mana_pool", "opp_mana_pool")
                obs["my_snow_mana_pool"] = self._snow_mana_vector(
                    agent_player_obj, "my_snow_mana_pool")
                obs["opp_snow_mana_pool"] = self._snow_mana_vector(
                    opp, "opp_snow_mana_pool")
                obs["my_restricted_mana_pool"] = self._restricted_mana_vector(
                    agent_player_obj, "my_restricted_mana_pool")
                obs["opp_restricted_mana_pool"] = self._restricted_mana_vector(
                    opp, "opp_restricted_mana_pool")
                obs["untapped_land_count"] = np.array([sum(1 for cid in agent_player_obj.get("battlefield", []) if self._is_land(cid) and cid not in agent_player_obj.get("tapped_permanents", set()))], dtype=np.int32)
                # Snow mana is provenance attached to units already present
                # in the ordinary/restricted pools, not an additional pool.
                # Counting it here made one snow mana look like two units of
                # spendable mana to the policy.
                floating_mana = sum(
                    int(np.asarray(obs[key], dtype=np.int64).sum())
                    for key in ("my_mana_pool", "my_restricted_mana_pool"))
                obs["total_available_mana"] = self._bounded_int_array(
                    "total_available_mana",
                    [floating_mana + int(obs["untapped_land_count"][0])])
                # v5: producible mana by color (observer-own is exact; the
                # opponent value is the public estimate from its untapped
                # lands).
                obs["my_producible_mana"] = self._get_producible_mana(
                    agent_player_obj)
                obs["opp_producible_mana"] = self._get_producible_mana(opp)
                # ``turn`` is the global alternating turn number. Compare
                # land development with turns actually received by this
                # observer, otherwise an on-curve player trends toward 0.5.
                observer_turns = (
                    (int(current_turn) + 1) // 2
                    if gs.agent_is_p1 else int(current_turn) // 2)
                land_count = sum(
                    1 for card_id in agent_player_obj.get("battlefield", [])
                    if self._is_land(card_id))
                obs["turn_vs_mana"] = np.array([
                    min(1.0, land_count / max(1.0, float(observer_turns)))
                ], dtype=np.float32)
            except Exception as e:
                self._record_observation_error("mana features", e)
                logging.error(f"Error populating mana features in _get_obs: {e}", exc_info=True)

            try:
                # Graveyard/Exile state
                obs["my_library_count"] = self._bounded_int_array(
                    "my_library_count", [len(agent_player_obj.get("library", []))])
                obs["opp_library_count"] = self._bounded_int_array(
                    "opp_library_count", [len(opp.get("library", []))])
                obs["my_player_counters"] = self._player_counter_vector(
                    agent_player_obj)
                obs["opp_player_counters"] = self._player_counter_vector(opp)
                obs["my_player_status"] = self._player_status_vector(
                    agent_player_obj)
                obs["opp_player_status"] = self._player_status_vector(opp)
                obs["my_graveyard_count"] = np.array([len(agent_player_obj.get("graveyard", []))], dtype=np.int32)
                obs["opp_graveyard_count"] = np.array([len(opp.get("graveyard", []))], dtype=np.int32)
                obs["my_exile_count"] = self._bounded_int_array(
                    "my_exile_count", [len(agent_player_obj.get("exile", []))])
                obs["opp_exile_count"] = self._bounded_int_array(
                    "opp_exile_count", [len(opp.get("exile", []))])
                obs["my_dead_creatures"] = np.array([sum(1 for cid in agent_player_obj.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32)
                obs["opp_dead_creatures"] = np.array([sum(1 for cid in opp.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32)
                for prefix, player_obj, zone in (
                        ("my", agent_player_obj, "graveyard"),
                        ("opp", opp, "graveyard"),
                        ("my", agent_player_obj, "exile"),
                        ("opp", opp, "exile")):
                    card_ids = player_obj.get(zone, [])
                    obs[f"{prefix}_{zone}_cards"] = self._get_zone_features(
                        card_ids, 10, newest_first=True)
                    obs[f"{prefix}_{zone}_card_identity"] = \
                        self._get_zone_identities(
                            card_ids, 10, newest_first=True)
                    if zone == "exile":
                        obs[f"{prefix}_exile_card_visibility"] = \
                            self._get_zone_visibility(
                                card_ids, 10, newest_first=True)
            except Exception as e:
                self._record_observation_error("graveyard/exile features", e)
                logging.error(f"Error populating graveyard/exile features in _get_obs: {e}", exc_info=True)

            try:
                # Stack state
                stack_controllers, stack_types = self._get_stack_info(gs.stack, agent_player_obj)
                obs["stack_count"] = np.array([len(gs.stack)], dtype=np.int32)
                obs["stack_controller"] = stack_controllers
                obs["stack_card_types"] = stack_types
                obs.update(self._get_detailed_stack_observation(agent_player_obj))
            except Exception as e:
                self._record_observation_error("stack features", e)
                logging.error(f"Error populating stack features in _get_obs: {e}", exc_info=True)

            try:
                # Combat state
                obs["attackers_count"] = np.array([len(getattr(gs, 'current_attackers', []))], dtype=np.int32)
                obs["blockers_count"] = np.array([sum(len(b) for b in getattr(gs, 'current_block_assignments', {}).values())], dtype=np.int32)
                (obs["combat_attack_targets"],
                 obs["combat_blocker_assignments"]) = \
                    self._get_combat_assignment_observation(
                        agent_player_obj, opp)
                # Declared since the space existed but fed a constant zero.
                # Now the agent's on-demand attack output: total power of its
                # currently legal attackers.  Reuse the combat handler's
                # legality path so defender and dynamic can't-attack effects
                # cannot inflate the signal. Paired with opp_life it is the
                # direct "is lethal on board?" signal the stall-heavy runs
                # never had (7.80).
                potential_damage = 0
                for battlefield_card_id in agent_player_obj.get("battlefield", []):
                    battlefield_card = gs._safe_get_card(battlefield_card_id)
                    if (not battlefield_card
                            or 'creature' not in getattr(
                                battlefield_card, 'card_types', [])):
                        continue
                    if (not getattr(self, "action_handler", None)
                            or not self.action_handler.is_valid_attacker(
                                battlefield_card_id)):
                        continue
                    potential_damage += max(
                        0, int(getattr(battlefield_card, 'power', 0) or 0))
                obs["potential_combat_damage"] = self._bounded_int_array(
                    "potential_combat_damage", [potential_damage])
            except Exception as e:
                self._record_observation_error("combat features", e)
                logging.error(f"Error populating combat state features in _get_obs: {e}", exc_info=True)


            # --- *** 5. Explicitly Handle Ability Features with Logging *** ---
            ability_features_key = "ability_features"
            try:
                # logging.debug(f"Calling _get_ability_features for {agent_player_obj.get('name','unknown player')}")
                bf_ids_for_features = agent_player_obj.get("battlefield", [])
                ability_features_result = self._get_ability_features(bf_ids_for_features, agent_player_obj)

                # --->>> ADDED Check and Correction: <<<---
                expected_space = self.observation_space.spaces[ability_features_key]
                if not isinstance(ability_features_result, np.ndarray) or ability_features_result.shape != expected_space.shape or ability_features_result.dtype != expected_space.dtype:
                    self._record_observation_error(
                        "ability features",
                        ValueError("ability feature helper returned invalid shape or dtype"))
                    logging.error(f"CRITICAL: _get_ability_features returned invalid result! "
                                f"Got type {type(ability_features_result)}, shape {getattr(ability_features_result, 'shape', 'N/A')}, dtype {getattr(ability_features_result, 'dtype', 'N/A')}. "
                                f"Expected shape {expected_space.shape}, dtype {expected_space.dtype}. Resetting to zeros.")
                    obs[ability_features_key] = np.zeros(expected_space.shape, dtype=expected_space.dtype)
                else:
                    obs[ability_features_key] = ability_features_result

            except Exception as ab_feat_e:
                self._record_observation_error("ability features", ab_feat_e)
                logging.error(f"CRITICAL error during _get_ability_features call or assignment: {ab_feat_e}", exc_info=True)
                # Reset to zeros explicitly if any error occurred during processing
                expected_shape = self.observation_space[ability_features_key].shape
                obs[ability_features_key] = np.zeros(expected_shape, dtype=self.observation_space[ability_features_key].dtype)
                logging.error(f"Reset {ability_features_key} to zeros due to error.")
            # --- End Ability Features Handling ---

            # Populate other features AFTER the problematic one, if possible
            try:
                obs["ability_timing"] = self._get_ability_timing(current_phase)
                obs["planeswalker_activations"] = self._get_planeswalker_activation_flags(agent_player_obj.get("battlefield", []), agent_player_obj)
                obs["planeswalker_activation_counts"] = self._get_planeswalker_activation_counts(agent_player_obj.get("battlefield", []), agent_player_obj)
            except Exception as e:
                self._record_observation_error("post-ability features", e)
                logging.error(f"Error populating post-ability features: {e}", exc_info=True)


            # --- 6. Populate History & Planning Features (inside try/except) ---
            try:
                # Env-tracked phase transitions (see _get_obs_safe); the old
                # gs._phase_history source was written only by dead code.
                phase_hist = getattr(self, '_observed_phase_history', [])
                phase_hist_len = len(phase_hist)
                phase_hist_arr = np.full(5, -1, dtype=np.int32)
                if phase_hist_len > 0: phase_hist_arr[-min(phase_hist_len, 5):] = phase_hist[-min(phase_hist_len, 5):] # Fill from end
                obs["phase_history"] = phase_hist_arr
                observer_is_learned_role = (
                    bool(gs.agent_is_p1)
                    == bool(getattr(
                        self, "_episode_agent_is_p1",
                        self.initial_agent_is_p1)))
                history_actions = (
                    self.last_n_actions
                    if observer_is_learned_role
                    else self.opponent_last_n_actions)
                history_rewards = (
                    self.last_n_rewards
                    if observer_is_learned_role
                    else self.opponent_last_n_rewards)
                obs["previous_actions"] = np.array(
                    history_actions, dtype=np.int32)
                obs["previous_rewards"] = np.array(
                    history_rewards, dtype=np.float32)

                # Planning Features
                obs["strategic_metrics"] = np.zeros(7, dtype=np.float32) # Default
                obs["position_advantage"] = np.array([self._calculate_position_advantage()], dtype=np.float32)
                obs["deck_composition_estimate"] = self._get_deck_composition(agent_player_obj)
                # v4: the observer's own decklist and remaining-library
                # composition (observer-own; opponent deck never exposed).
                obs["my_deck_card_identity"] = self._get_deck_card_identities(agent_player_obj)
                obs["my_library_composition"] = self._get_library_composition(agent_player_obj)
                obs["opponent_archetype"] = np.zeros(6, dtype=np.float32) # Default
                obs["future_state_projections"] = np.zeros(7, dtype=np.float32) # Default
                obs["multi_turn_plan"] = np.zeros(6, dtype=np.float32) # Default
                obs["win_condition_viability"] = np.zeros(6, dtype=np.float32) # Default
                obs["win_condition_timings"] = np.zeros(6, dtype=np.float32) # Default

                # Recommendations (Defaults assigned earlier)
                # Action Mask already assigned

                # Mulligan state (Defaults assigned earlier)
                obs["mulligan_in_progress"] = np.array([int(getattr(gs, 'mulligan_in_progress', False))], dtype=np.int32)

                # Context Features (Defaults assigned earlier)
                obs["targetable_permanents"] = self._get_potential_targets_vector('permanent')
                obs["targetable_players"] = self._get_potential_targets_vector('player')
                obs["targetable_spells_on_stack"] = self._get_potential_targets_vector('spell')
                obs["targetable_cards_in_graveyards"] = self._get_potential_targets_vector('graveyard_card')
                target_page_observation = self._get_target_page_observation(
                    agent_player_obj)
                obs.update(target_page_observation)
                choice = getattr(gs, 'choice_context', None)
                if choice and choice.get('player') is agent_player_obj:
                    all_choice_options = choice.get(
                        'options', choice.get('cards', []))
                    choice_page = int(choice.get('choice_page', 0))
                    choice_options = all_choice_options[
                        choice_page * 10:(choice_page + 1) * 10]
                    choice_card_ids = []
                    for option in choice_options:
                        candidate_id = (
                            option.get('card_id', option.get('id'))
                            if isinstance(option, dict) else option)
                        try:
                            # Choice options are heterogeneous: some are card
                            # IDs, while others are creature subtypes, mana
                            # symbols, player selectors, or structured values.
                            # _safe_get_card intentionally returns a truthy
                            # synthetic Card for unknown IDs, so it cannot be
                            # used as a membership probe here without turning
                            # symbolic options into visible phantom cards.
                            candidate_card = gs.card_db.get(candidate_id)
                        except (KeyError, TypeError, ValueError):
                            candidate_card = None
                        choice_card_ids.append(
                            candidate_id if candidate_card is not None else None)
                    for option_index, candidate_id in enumerate(choice_card_ids):
                        if candidate_id is None:
                            continue
                        obs['choice_cards'][option_index] = self._get_card_feature(
                            candidate_id, self._feature_dim)
                        obs['choice_card_identity'][option_index] = \
                            self._semantic_card_index(candidate_id)
                        obs['choice_card_mask'][option_index] = True
                    obs['choice_remaining'] = np.array(
                        [max(0, int(choice.get('remaining', 0)))], dtype=np.int32)
                    allocations = choice.get('allocations', {})
                    for option_index, card_id in enumerate(choice_card_ids):
                        if card_id is None:
                            continue
                        try:
                            allocation_count = allocations.get(card_id, 0)
                        except TypeError:
                            allocation_count = 0
                        obs['choice_allocation_counts'][option_index] = max(
                            0, int(allocation_count))
                    choice_kinds = {
                        'dig_select': 1, 'sacrifice_effect': 2,
                        'distribute_counters': 3, 'manifest_dread': 4,
                        'hand_selection': 5, 'scry': 6, 'surveil': 7,
                        'activation_sacrifice_cost': 9,
                    }
                    obs['choice_kind'] = np.array(
                        [choice_kinds.get(choice.get('type'), 8)], dtype=np.int32)
                obs["sacrificeable_permanents"] = self._get_potential_sacrifices()
                obs["selectable_modes"] = self._get_available_choice_options('mode')
                obs["selectable_colors"] = self._get_available_choice_options('color')
                obs["valid_x_range"] = self._get_available_choice_options('x_range')
                obs["bottomable_cards"] = self._get_bottoming_mask(agent_player_obj)
                obs["dredgeable_cards_in_gy"] = self._get_dredge_options(agent_player_obj)

            except Exception as e:
                self._record_observation_error("history/planning/context features", e)
                logging.error(f"Error populating history/planning/context features in _get_obs: {e}", exc_info=True)


            # --- 7. Populate Dynamic/Planner Dependent Features (wrapped) ---
            # (Keep existing planner population logic, wrapped in try/except)
            try:
                if hasattr(self, 'strategic_planner') and self.strategic_planner:
                    # (Planner population logic - remains the same as previous version)
                    # ... (fill strategic_metrics, opponent_archetype, recommendations etc.) ...
                    # Analysis depends on the complete live board and on
                    # gs.agent_is_p1.  Caching it by turn alone made every
                    # same-turn transition stale and could feed the learned
                    # opponent an analysis from the other seat.  Refresh at
                    # the observation boundary; this also updates the
                    # planner's own current_analysis for downstream helpers.
                    analysis = self.strategic_planner.analyze_game_state()
                    self.current_analysis = analysis

                    if analysis:
                        obs["strategic_metrics"][:] = self._analysis_to_metrics(analysis)
                        obs["position_advantage"][0] = np.clip(
                            analysis.get("position", {}).get("score", 0),
                            -1.0, 1.0)

                    opp_arch = self.strategic_planner.predict_opponent_archetype()
                    arch_len = min(len(obs["opponent_archetype"]), len(opp_arch))
                    obs["opponent_archetype"][:arch_len] = opp_arch[:arch_len]

                    future_proj = self.strategic_planner.project_future_states(num_turns=7)
                    proj_len = min(len(obs["future_state_projections"]), len(future_proj))
                    obs["future_state_projections"][:proj_len] = future_proj[:proj_len]

                    win_cons = self.strategic_planner.identify_win_conditions()
                    wc_keys = ["combat_damage", "direct_damage", "card_advantage", "combo", "control", "alternate"]
                    wc_viab_len = min(len(obs["win_condition_viability"]), len(wc_keys))
                    wc_time_len = min(len(obs["win_condition_timings"]), len(wc_keys))
                    obs["win_condition_viability"][:wc_viab_len] = np.array([
                        np.clip(win_cons.get(k, {}).get("score", 0.0), 0.0, 1.0)
                        for k in wc_keys[:wc_viab_len]
                    ], dtype=np.float32)
                    obs["win_condition_timings"][:wc_time_len] = np.array([min(self.max_turns + 1, win_cons.get(k, {}).get("turns_to_win", 99)) for k in wc_keys[:wc_time_len]], dtype=np.float32)

                    bf_ids_opp = opp.get("battlefield", [])
                    threat_list = self.strategic_planner.assess_threats()
                    turn_plan = self.strategic_planner.plan_multi_turn_sequence(
                        depth=2,
                        analysis=analysis,
                        win_conditions=win_cons,
                        opponent_threats=threat_list,
                    )
                    plan_len = min(len(obs["multi_turn_plan"]), 6)
                    obs["multi_turn_plan"][:plan_len] = \
                        self._get_multi_turn_plan_metrics(
                            plan=turn_plan)[:plan_len]

                    threat_assessment_values = self._get_threat_assessment(
                        bf_ids_opp, threat_list=threat_list)
                    copy_len_threat = min(len(obs["threat_assessment"]), len(threat_assessment_values))
                    obs["threat_assessment"][:copy_len_threat] = threat_assessment_values[:copy_len_threat]

                    bf_ids_agent = agent_player_obj.get("battlefield", [])
                    # The old gate checked strategic_planner for
                    # find_optimal_attack, but the method belongs to the combat
                    # action handler, so both advisory features were silently
                    # dead. Compute individual values with the evaluator and
                    # the optimal combination with the real combat search,
                    # only while an attack decision is live.
                    if gs.phase == gs.PHASE_DECLARE_ATTACKERS:
                        attacker_values = self._get_attacker_values(
                            bf_ids_agent, agent_player_obj)
                        if obs["attacker_values"].shape == attacker_values.shape:
                            obs["attacker_values"][:] = attacker_values
                        optimal_ids = set(
                            self.action_handler.find_optimal_attack() or [])
                        for battlefield_index, battlefield_card_id in enumerate(
                                bf_ids_agent[:self.max_battlefield]):
                            if battlefield_card_id in optimal_ids:
                                obs["optimal_attackers"][battlefield_index] = 1.0

                    ability_recs = self._get_ability_recommendations(bf_ids_agent, agent_player_obj)
                    if obs["ability_recommendations"].shape == ability_recs.shape: obs["ability_recommendations"][:,:,:] = ability_recs

            except Exception as planner_e:
                self._record_observation_error("strategic planner features", planner_e)
                logging.warning(f"Error getting strategic info for observation: {planner_e}", exc_info=False)

            # --- 8. Populate Mulligan Info ---
            try:
                if getattr(gs, 'mulligan_in_progress', False) and getattr(gs, 'mulligan_player', None) == agent_player_obj:
                    mull_rec, mull_reasons, mull_count = self._get_mulligan_info(agent_player_obj)
                    obs["mulligan_recommendation"][0] = mull_rec
                    obs["mulligan_reason_count"][0] = mull_count
                    reason_len = min(len(obs["mulligan_reasons"]), len(mull_reasons))
                    obs["mulligan_reasons"][:reason_len] = mull_reasons[:reason_len]
            except Exception as e:
                self._record_observation_error("mulligan features", e)
                logging.error(f"Error populating mulligan features in _get_obs: {e}", exc_info=True)

            # --- 9. FINAL VALIDATION AND GUARANTEE OF ALL KEYS ---
            # Extra check to ensure all keys defined in observation_space are present
            for key, space in self.observation_space.spaces.items():
                if key not in obs:
                    self._record_observation_error(
                        f"feature {key}", KeyError("missing from final observation"))
                    logging.critical(f"Key '{key}' missing in final observation! Adding default.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                elif not isinstance(obs[key], np.ndarray):
                    self._record_observation_error(
                        f"feature {key}", TypeError("final value is not an ndarray"))
                    logging.critical(f"Key '{key}' is not a numpy array in final observation! Re-creating.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                elif obs[key].shape != space.shape:
                    self._record_observation_error(
                        f"feature {key}", ValueError(
                            f"shape {obs[key].shape}, expected {space.shape}"))
                    logging.critical(f"Key '{key}' has wrong shape in final observation! Expected {space.shape}, got {obs[key].shape}. Re-creating.")
                    obs[key] = np.zeros(space.shape, dtype=space.dtype)
                
            # Final guarantees for critical keys before returning
            critical_keys = ["action_mask", "ability_features", "ability_recommendations", "ability_timing", "phase", "turn"]
            for critical_key in critical_keys:
                if critical_key not in obs:
                    logging.critical(f"Critical key '{critical_key}' missing in final obs! Re-creating.")
                    if critical_key in self.observation_space.spaces:
                        space = self.observation_space.spaces[critical_key]
                        obs[critical_key] = np.zeros(space.shape, dtype=space.dtype)
                    else:
                        # Fallback shapes
                        if critical_key == "action_mask":
                            obs[critical_key] = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                            obs[critical_key][11] = True  # PASS
                            obs[critical_key][12] = True  # CONCEDE
                        elif critical_key == "ability_features":
                            obs[critical_key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                        elif critical_key == "ability_recommendations":
                            obs[critical_key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
                        elif critical_key == "ability_timing":
                            obs[critical_key] = np.zeros((5,), dtype=np.float32)
                        elif critical_key == "phase":
                            obs[critical_key] = np.array([0], dtype=np.int32)
                        elif critical_key == "turn":
                            obs[critical_key] = np.array([1], dtype=np.int32)

            return self._coerce_observation(obs)

        # --- Main Exception Handling for _get_obs ---
        except Exception as e:
            self._record_observation_error("observation builder", e)
            logging.critical(f"CRITICAL error during _get_obs execution: {str(e)}", exc_info=True)
            return self._get_obs_fallback()

    def observation_for(self, player):
        """Build one strict player observation without perspective/cache leaks."""
        with self._observer_policy_boundary(player):
            return self._policy_observation_for_current_perspective()

    def _get_planeswalker_activation_flags(self, battlefield_ids, player):
        """Helper for planeswalker activation flags."""
        flags = np.zeros(self.max_battlefield, dtype=np.float32)
        activated_set = player.get("activated_this_turn", set())
        for i, card_id in enumerate(battlefield_ids):
            if i >= self.max_battlefield: break
            card = self.game_state._safe_get_card(card_id)
            if card and 'planeswalker' in getattr(card, 'card_types', []):
                flags[i] = float(card_id in activated_set)
        return flags

    def _get_planeswalker_activation_counts(self, battlefield_ids, player):
        """Helper for planeswalker activation counts."""
        counts = np.zeros(self.max_battlefield, dtype=np.float32)
        activation_counts = player.get("pw_activations", {})
        for i, card_id in enumerate(battlefield_ids):
            if i >= self.max_battlefield: break
            counts[i] = float(activation_counts.get(card_id, 0))
        return counts

    def _get_fallback_analysis(self):
        """Provides a basic analysis structure if strategic planner fails."""
        gs = self.game_state
        return {
            "game_info": {"turn": gs.turn, "phase": gs.phase, "game_stage": "mid"},
            "position": {"overall": "even", "score": 0.0},
            "board_state": {"board_advantage": 0.0, "my_creatures":0, "opp_creatures":0},
            "resources": {"card_advantage": 0, "mana_advantage": 0},
            "life": {"life_diff": 0},
            "tempo": {"tempo_advantage": 0.0},
            "win_conditions": {}
        }

    def _validate_obs(self, obs):
        """
        Checks if the generated observation dictionary conforms to the defined observation space.
        Includes stricter dtype/shape checks and improved logging.
        """
        if not isinstance(obs, dict):
            logging.error("Observation Validation Error: Observation is not a dictionary.")
            return False
        if not hasattr(self, 'observation_space') or not isinstance(self.observation_space, spaces.Dict):
            logging.error("Observation Validation Error: Observation space is not defined or not a Dict space.")
            return False

        valid = True
        for key, space in self.observation_space.spaces.items():
            if key not in obs:
                logging.error(f"Observation Validation Error: Missing key '{key}'")
                valid = False
                continue

            value = obs[key]
            if not isinstance(value, np.ndarray):
                # Special case: Allow simple scalars if the space shape is () or (1,) - Gymnasium can handle this sometimes
                if (space.shape == () or space.shape == (1,)) and np.isscalar(value):
                    # Attempt to cast scalar to numpy array for further checks
                    try:
                        value = np.array([value], dtype=space.dtype) # Wrap in array matching space dtype
                        obs[key] = value # Update the observation dict for consistency if needed downstream
                    except Exception as e:
                         logging.error(f"Observation Validation Error: Key '{key}' is scalar but couldn't be cast to expected type {space.dtype}. Value: {value}. Error: {e}")
                         valid = False
                         continue # Skip further checks for this key
                else:
                    logging.error(f"Observation Validation Error: Key '{key}' is not a numpy array (type: {type(value)}) and not a compatible scalar.")
                    valid = False
                    continue

            # Shape check (allow broadcasting for single-element dimensions)
            expected_shape = space.shape
            actual_shape = value.shape
            shape_match = False
            if len(expected_shape) == len(actual_shape):
                 shape_match = all(exp_d == act_d or exp_d == 1 or act_d == 1 for exp_d, act_d in zip(expected_shape, actual_shape))
            # Handle case where space shape is () e.g. Box(0,1, shape=())
            elif expected_shape == () and (actual_shape == (1,) or actual_shape == ()):
                 shape_match = True # Allow shape (1,) for a shape () space
            elif actual_shape == () and (expected_shape == (1,)):
                shape_match = True

            if not shape_match:
                 logging.error(f"Observation Validation Error: Shape mismatch for '{key}'. Expected {expected_shape}, got {actual_shape}")
                 valid = False

            # Dtype check - Be stricter, check exact match or safe casting
            # Allow safe casting (e.g., int32 to int64, float32 to float64), but flag exact mismatches.
            if value.dtype != space.dtype:
                 # Check if casting is safe based on NumPy kinds
                 expected_kind = np.dtype(space.dtype).kind
                 actual_kind = value.dtype.kind
                 # Allow int->int, float->float, bool->bool if target bits >= source bits
                 can_safely_cast = False
                 if expected_kind == actual_kind:
                      if expected_kind == 'b': # Bool
                           can_safely_cast = True
                      elif expected_kind in 'iu': # Ints
                           can_safely_cast = np.dtype(space.dtype).itemsize >= value.dtype.itemsize
                      elif expected_kind == 'f': # Floats
                           can_safely_cast = np.dtype(space.dtype).itemsize >= value.dtype.itemsize
                 # Allow int to float conversion
                 elif expected_kind == 'f' and actual_kind in 'iu':
                      can_safely_cast = True
                 # Allow float to int conversion (often problematic, maybe warn?)
                 # elif expected_kind in 'iu' and actual_kind == 'f':
                 #     can_safely_cast = True # Needs explicit check for data loss

                 if not can_safely_cast:
                     logging.error(f"Observation Validation Error: Dtype mismatch for '{key}'. Expected {space.dtype} (kind '{expected_kind}'), got {value.dtype} (kind '{actual_kind}').")
                     valid = False
                 else:
                     # Optionally log a warning for safe casts if desired for debugging
                     pass # logging.debug(f"Observation Validation Warning: Safe dtype cast required for '{key}'. Expected {space.dtype}, got {value.dtype}")


            # Bounds check (only for Box spaces) - Improved handling for different shapes/NaN
            if isinstance(space, spaces.Box):
                try:
                    # Handle NaN values - skip bounds check for NaNs if space allows
                    allow_nan = False # Set to True if your space explicitly intends to use NaNs
                    if allow_nan:
                        nan_mask = np.isnan(value)
                        valid_non_nan = True
                        # Check bounds only for non-NaN values
                        if not np.all(nan_mask):
                             value_to_check = value[~nan_mask]
                             lower_bound = space.low[0] if space.low.size==1 else space.low[~nan_mask] # Align bounds
                             upper_bound = space.high[0] if space.high.size==1 else space.high[~nan_mask]
                             valid_non_nan = np.all(np.greater_equal(value_to_check, lower_bound)) and \
                                             np.all(np.less_equal(value_to_check, upper_bound))
                    else:
                        # No NaNs allowed, check directly
                        # Handle shape mismatch for bounds comparison if space bounds are scalar but value is array
                        low_bound_val = space.low if space.low.shape == value.shape else space.low[0]
                        high_bound_val = space.high if space.high.shape == value.shape else space.high[0]
                        valid_bounds = np.all(np.greater_equal(value, low_bound_val)) and \
                                       np.all(np.less_equal(value, high_bound_val))

                    if not valid_bounds and not allow_nan:
                         # Find first violation for logging
                         lower_violations = value < low_bound_val
                         upper_violations = value > high_bound_val
                         violation_indices = np.where(lower_violations | upper_violations)[0]
                         if violation_indices.size > 0:
                              first_violation_idx = violation_indices[0]
                              min_val = value[first_violation_idx]
                              logging.error(f"Observation Validation Error: Value out of bounds for '{key}'. Expected [{low_bound_val}, {high_bound_val}], got {min_val} at index {first_violation_idx}")
                              valid = False
                except Exception as bound_e:
                    logging.error(f"Observation Validation Error: Error checking bounds for '{key}' with value shape {value.shape} against space {space}. Error: {bound_e}", exc_info=True)
                    valid = False

        return valid

    def _get_hand_synergy_scores(self, hand_ids, bf_ids):
        """Calculate synergy for each card in hand with current board/hand state."""
        scores = np.zeros(self.hand_observation_size, dtype=np.float32)
        if not self.strategic_planner or not hasattr(self.strategic_planner, 'identify_card_synergies'):
            return scores

        current_hand_and_board = hand_ids + bf_ids
        for i, card_id in enumerate(hand_ids):
            if i >= self.hand_observation_size: break
            # Compare card i with all *other* cards currently available
            other_cards = [cid for cid in current_hand_and_board if cid != card_id]
            synergy_score, _ = self.strategic_planner.identify_card_synergies(card_id, other_cards, []) # Compare with hand+board
            scores[i] = np.clip(synergy_score / 5.0, 0.0, 1.0) # Normalize synergy score

        return scores

    def _get_ability_recommendations(self, bf_ids, player):
        """Populate the ability recommendations tensor."""
        # Fallback: Return zeros if planner is not available
        recs = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32) # shape (bf_size, max_abilities, [recommend, conf])
        if not hasattr(self, 'strategic_planner') or not self.strategic_planner or not hasattr(self.action_handler, 'game_state') or not self.action_handler.game_state.ability_handler:
            logging.debug("Planner or ability handler missing for ability recommendations.")
            return recs

        gs = self.game_state
        ability_handler = gs.ability_handler
        if not ability_handler:
            logging.warning("Ability handler is None inside _get_ability_recommendations.")
            return recs # Double check handler exists

        for i, card_id in enumerate(bf_ids):
             if i >= self.max_battlefield: break
             abilities = ability_handler.get_activated_abilities(card_id)
             for j, ability in enumerate(abilities):
                  if j >= 5: break # Max 5 abilities per card
                  try:
                      # Check if actually activatable first
                      can_activate = ability_handler.can_activate_ability(card_id, j, player)
                      if can_activate:
                           # Ensure planner method exists before calling
                           if hasattr(self.strategic_planner, 'recommend_ability_activation'):
                               recommended, confidence = self.strategic_planner.recommend_ability_activation(card_id, j)
                               recs[i, j, 0] = float(recommended)
                               recs[i, j, 1] = confidence
                           else: # Fallback if method missing on planner
                               logging.warning("Planner missing recommend_ability_activation method.")
                               recs[i, j, 0] = 0.5; recs[i, j, 1] = 0.5 # Default neutral
                      # else leave as 0.0, 0.0
                  except Exception as e:
                      logging.warning(f"Error getting ability rec for {card_id} ability {j}: {e}")
                      # Leave as zeros
        return recs
        
    # --- Observation Helper Methods ---

    def _get_target_page_observation(self, player):
        """Describe the exact candidates bound to actions 274 through 283."""
        gs = self.game_state
        result = {
            "target_cards": np.zeros((10, self._feature_dim), dtype=np.float32),
            "target_card_identity": np.zeros(10, dtype=np.int32),
            "target_card_mask": np.zeros(10, dtype=bool),
            "target_card_ids": np.full(10, -1, dtype=np.int64),
            "target_kinds": np.zeros(10, dtype=np.int32),
            "target_controllers": np.full(10, -1, dtype=np.int32),
            "target_zone_indices": np.full(10, -1, dtype=np.int32),
        }
        context = getattr(gs, "targeting_context", None)
        if (not context or context.get("controller") is not player
                or not getattr(self, "action_handler", None)):
            return result
        candidates = self.action_handler._get_target_selection_candidates(
            player, context)
        page = int(context.get("target_page", 0))
        for slot, target_id in enumerate(candidates[page * 10:(page + 1) * 10]):
            if target_id == "p1" or target_id == "p2":
                target_player = gs.p1 if target_id == "p1" else gs.p2
                result["target_kinds"][slot] = 1
                result["target_controllers"][slot] = (
                    0 if target_player is player else 1)
                continue

            owner = None
            zone = None
            zone_index = -1
            for controller_index, candidate_owner in enumerate((gs.p1, gs.p2)):
                for candidate_zone, kind in (
                        ("battlefield", 2), ("graveyard", 4), ("exile", 5)):
                    values = candidate_owner.get(candidate_zone, [])
                    if target_id in values:
                        owner, zone = candidate_owner, candidate_zone
                        zone_index = values.index(target_id)
                        result["target_kinds"][slot] = kind
                        result["target_controllers"][slot] = (
                            0 if candidate_owner is player else 1)
                        break
                if zone is not None:
                    break
            if zone is None:
                for stack_index, item in enumerate(gs.stack):
                    if (isinstance(item, tuple) and len(item) >= 3
                            and item[1] == target_id):
                        owner, zone, zone_index = item[2], "stack", stack_index
                        result["target_kinds"][slot] = 3
                        result["target_controllers"][slot] = (
                            0 if owner is player else
                            1 if owner in (gs.p1, gs.p2) else -1)
                        break
            result["target_zone_indices"][slot] = zone_index
            hidden_exile = bool(
                zone == "exile"
                and hasattr(gs, "is_face_down_exile_card")
                and gs.is_face_down_exile_card(target_id, owner))
            if isinstance(target_id, (int, np.integer)):
                if not hidden_exile:
                    result["target_card_ids"][slot] = int(target_id)
                if target_id in gs.card_db:
                    result["target_card_identity"][slot] = \
                        self._semantic_card_index(target_id)
                    if not hidden_exile:
                        result["target_cards"][slot] = self._get_card_feature(
                            target_id, self._feature_dim)
                    result["target_card_mask"][slot] = True
        return result
    
    def _get_potential_targets_vector(self, target_kind):
        """Return exact public-zone indices for targetable entities.

        Permanent and graveyard indices use the observer-relative flattened
        order ``my zone + opponent zone``; players use 0=me/1=opponent;
        spells use their real stack indices.
        """
        gs = self.game_state
        agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2
        opponent_player_obj = gs.p2 if gs.agent_is_p1 else gs.p1
        target_indices = []
        max_size = 0
        dtype_for_space = np.int32 # Use integers for indices

        # Determine max size based on observation space definition
        if target_kind == 'permanent': max_size = self.observation_space["targetable_permanents"].shape[0]
        elif target_kind == 'player': max_size = 2
        elif target_kind == 'spell': max_size = self.observation_space["targetable_spells_on_stack"].shape[0]
        elif target_kind == 'graveyard_card': max_size = self.observation_space["targetable_cards_in_graveyards"].shape[0]
        else: return np.full(1, -1, dtype=dtype_for_space) # Default if kind unknown

        # Only populate if targeting context is relevant
        if gs.phase == gs.PHASE_TARGETING and gs.targeting_context:
            context = gs.targeting_context
            source_id = context.get("source_id")
            controller = context.get("controller")
            if controller is agent_player_obj: # Only show targets if it's agent's turn to target
                candidate_ids = set()
                if (gs.targeting_system
                        and getattr(self, "action_handler", None)):
                    candidate_ids.update(
                        self.action_handler._get_target_selection_candidates(
                            controller, context))
                elif gs.targeting_system:
                    valid_targets_map = gs.targeting_system.get_valid_targets(
                        source_id, controller,
                        context.get("required_type", "target"),
                        effect_text=context.get("effect_text"))
                    candidate_ids.update(
                        target_id
                        for targets in valid_targets_map.values()
                        for target_id in targets)
                else:
                    logging.warning(
                        "Targeting system unavailable in _get_potential_targets")

                target_indices = []
                if target_kind == 'permanent':
                    flattened_battlefield = (
                        list(agent_player_obj.get("battlefield", []))
                        + list(opponent_player_obj.get("battlefield", [])))
                    target_indices = [
                        index for index, card_id in
                        enumerate(flattened_battlefield)
                        if card_id in candidate_ids]
                elif target_kind == 'player':
                    for target_name in candidate_ids:
                        target_player = (
                            gs.p1 if target_name == "p1" else
                            gs.p2 if target_name == "p2" else None)
                        if target_player is not None:
                            target_indices.append(
                                0 if target_player is agent_player_obj else 1)
                elif target_kind == 'spell':
                    target_indices = [
                        stack_index for stack_index, item in enumerate(gs.stack)
                        if (isinstance(item, tuple) and len(item) > 3
                            and item[0] == "SPELL"
                            and item[1] in candidate_ids)]
                elif target_kind == 'graveyard_card':
                    flattened_graveyards = (
                        list(agent_player_obj.get("graveyard", []))
                        + list(opponent_player_obj.get("graveyard", [])))
                    target_indices = [
                        index for index, card_id in
                        enumerate(flattened_graveyards)
                        if card_id in candidate_ids]

        # Encode public indices in canonical observer-relative order. Targeting
        # systems may return players or cards in seat/insertion order.
        target_indices = sorted(set(target_indices))

        # Encode Indices (pad/truncate)
        encoded_indices = np.full(max_size, -1, dtype=dtype_for_space)
        for output_index, target_index in enumerate(target_indices[:max_size]):
            encoded_indices[output_index] = target_index

        return encoded_indices

    def _get_potential_sacrifices(self):
            """Helper to get INDICES of permanents the agent can sacrifice. Returns np.array padded with -1."""
            gs = self.game_state
            agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2
            sacrificeable_info = [] # Store tuples (permanent_id, battlefield_index)
            max_size = self.observation_space["sacrificeable_permanents"].shape[0]
            dtype_for_space = np.int32

            # Check if sacrifice context is active for the agent
            context_active_for_agent = False
            if hasattr(gs, 'sacrifice_context') and gs.sacrifice_context:
                controller = gs.sacrifice_context.get("controller")
                if controller == agent_player_obj:
                    context_active_for_agent = True

            if context_active_for_agent:
                context = gs.sacrifice_context
                req_type = context.get('required_type') # e.g., 'creature', 'artifact', 'permanent'
                # Get permanents on the battlefield
                player_battlefield = agent_player_obj.get("battlefield", [])

                # Filter based on requirements
                for i, perm_id in enumerate(player_battlefield):
                    # Do not exceed observation space size
                    # if i >= max_size: break # Check later when populating array

                    perm_card = gs._safe_get_card(perm_id)
                    if not perm_card: continue

                    # Type Check
                    type_match = False
                    if not req_type or req_type == "permanent":
                        type_match = True
                    elif hasattr(perm_card, 'card_types') and req_type in perm_card.card_types:
                        type_match = True

                    # Additional Checks (can't be sacrificed, etc.) - TODO: Implement if needed
                    can_be_sacrificed = True # Placeholder

                    if type_match and can_be_sacrificed:
                        sacrificeable_info.append((perm_id, i)) # Store ID and its battlefield index

            # Encode Battlefield Indices (pad/truncate)
            encoded_indices = np.full(max_size, -1, dtype=dtype_for_space)
            # The action parameter the agent chooses (0 to k-1) corresponds to the k-th valid sacrifice option.
            # The observation array at index k should store the BATTLEFIELD index of that option.
            for k, (perm_id, bf_index) in enumerate(sacrificeable_info):
                if k >= max_size: break
                encoded_indices[k] = bf_index # Store the battlefield index

            return encoded_indices

    def _get_available_choice_options(self, choice_kind):
            """Helper to get available modes, colors, or X range based on active choice context."""
            gs = self.game_state
            agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2

            # Determine space properties dynamically
            if choice_kind in ['mode', 'color']:
                obs_key = f"selectable_{choice_kind}s"
            elif choice_kind == 'x_range':
                obs_key = "valid_x_range"
            else:
                obs_key = f"valid_{choice_kind}_range"
            space = self.observation_space.spaces.get(obs_key)
            if space is None:
                logging.warning(f"Observation space missing for choice kind '{choice_kind}'.")
                # Provide a default shape and type if space definition is missing
                default_shape = (10,) if choice_kind == 'mode' else (5,) if choice_kind == 'color' else (2,) if choice_kind == 'x_range' else (1,)
                dtype = np.int32
                max_size = default_shape[0]
            else:
                max_size = space.shape[0]
                dtype = space.dtype

            # Default result is padded array
            options_vector = np.full(max_size, -1, dtype=dtype) # Default to int, use -1 padding

            # Check if choice context is active for the AGENT
            context_active_for_agent = False
            if hasattr(gs, 'choice_context') and gs.choice_context:
                controller = gs.choice_context.get("player")
                if controller == agent_player_obj:
                    context_active_for_agent = True

            if context_active_for_agent:
                context = gs.choice_context
                current_choice_type = context.get("type")

                # Populate based on the specific choice type needed by the agent
                if choice_kind == 'mode' and current_choice_type == 'choose_mode':
                    num_choices = context.get("num_choices", 0)
                    max_selectable = context.get("max_required", 1)
                    selected_already = context.get("selected_modes", [])
                    can_select_more = len(selected_already) < max_selectable

                    if can_select_more:
                        available_mode_indices = []
                        for i in range(num_choices):
                            # Mode is represented by its index (0, 1, 2...)
                            # Avoid selecting duplicates if not allowed (most cases)
                            if (i not in selected_already
                                    and gs.modal_mode_is_selectable(context, i)):
                                available_mode_indices.append(i)

                        # Fill the vector with available mode indices
                        for k, mode_idx in enumerate(available_mode_indices):
                            if k >= max_size: break
                            options_vector[k] = mode_idx

                elif choice_kind == 'color' and current_choice_type == 'choose_color':
                    # Indices 0-4 represent WUBRG
                    # Assuming all 5 colors are always potential choices
                    valid_colors = np.arange(5, dtype=dtype)
                    len_to_copy = min(len(valid_colors), max_size)
                    options_vector[:len_to_copy] = valid_colors[:len_to_copy]

                elif choice_kind == 'x_range' and current_choice_type == 'choose_x':
                    # Expected shape is (2,) for [min_X, max_X]
                    min_x = context.get("min_x", 0)
                    max_x_calc = context.get("max_x", 0) # Max X calculated based on mana
                    # Ensure dtype compatibility if space expects float
                    if np.issubdtype(dtype, np.floating):
                        min_x = float(min_x)
                        max_x_calc = float(max_x_calc)
                    options_vector[0] = min_x
                    options_vector[1] = max_x_calc
                # Add logic for other choice kinds if introduced (e.g., choose target type)
                else:
                    # Kind doesn't match active context type, return padded default
                    pass

            # Return the populated (or default padded) vector
            return options_vector

    def _get_bottoming_mask(self, player):
        """Helper to get mask of cards available to bottom after mulligan."""
        gs = self.game_state
        mask = np.zeros(self.hand_observation_size, dtype=bool)
        if getattr(gs, 'bottoming_in_progress', False) and getattr(gs, 'bottoming_player', None) == player:
            for i in range(len(player.get("hand", []))):
                if i < self.hand_observation_size: mask[i] = True
        return mask


    def _get_dredge_options(self, player):
        """Helper to get IDs of dredgeable cards in graveyard."""
        gs = self.game_state
        options = np.full(6, -1, dtype=np.int32) # Action space has 6 GY indices for dredge
        max_size = 6

        # Only populate if a draw event might trigger dredge (or explicit choice phase)
        can_dredge_now = False
        if gs.phase == gs.PHASE_DRAW and gs.priority_player == player: can_dredge_now = True
        # Or if explicit dredge choice pending
        if getattr(gs, 'dredge_pending', None) and gs.dredge_pending['player'] == player: can_dredge_now = True

        if can_dredge_now:
            dredge_card_ids = []
            for i, card_id in enumerate(player.get("graveyard", [])[:max_size]): # Look at top N GY cards only
                card = gs._safe_get_card(card_id)
                if card and "dredge" in getattr(card, 'oracle_text', '').lower():
                    dredge_match = re.search(r"dredge (\d+)", card.oracle_text.lower())
                    if dredge_match:
                         dredge_value = int(dredge_match.group(1))
                         # Check if enough cards in library to dredge
                         if len(player.get("library", [])) >= dredge_value:
                              dredge_card_ids.append((i, card_id)) # Store GY index and ID

            # Populate the observation array with indices
            for k, (gy_index, _) in enumerate(dredge_card_ids[:max_size]):
                options[k] = gy_index # Store the index

        return options

    def _card_identity_is_hidden(self, card_id):
        """Whether a visible object must conceal its printed identity."""
        gs = self.game_state
        card = gs.card_db.get(card_id) if isinstance(gs.card_db, dict) else None
        if bool(getattr(card, "face_down", False)):
            observer = gs.p1 if gs.agent_is_p1 else gs.p2
            # A player may look at face-down permanents they control (manifest,
            # morph, disguise); opponents receive only public face-down state.
            if card_id not in observer.get("battlefield", ()):
                return True
        return bool(
            hasattr(gs, "is_face_down_exile_card")
            and gs.is_face_down_exile_card(card_id))

    def _semantic_card_index(self, card_id):
        """Map a runtime object to the observation contract's stable identity encoding."""
        if card_id is None:
            return 0
        if self._card_identity_is_hidden(card_id):
            return 1
        canonical_id = self.game_state.canonical_card_id(card_id)
        if (isinstance(canonical_id, (int, np.integer))
                and int(canonical_id) in self._canonical_card_ids):
            return int(canonical_id) + 2
        card = self.game_state.card_db.get(card_id)
        canonical_id = self._canonical_card_ids_by_name.get(
            str(getattr(card, "name", "")).casefold())
        if canonical_id is not None:
            return int(canonical_id) + 2
        # Generated tokens and cards outside the frozen registry are still
        # visible objects; keep them distinct from padding without inventing
        # an unstable per-game category.
        return 1

    @staticmethod
    def _zone_window(card_ids, max_size, newest_first=False):
        values = list(card_ids or ())
        if newest_first:
            return list(reversed(values[-max_size:]))
        return values[:max_size]

    def _get_zone_identities(self, card_ids, max_size, newest_first=False):
        identities = np.zeros(max_size, dtype=np.int32)
        for index, card_id in enumerate(self._zone_window(
                card_ids, max_size, newest_first=newest_first)):
            identities[index] = self._semantic_card_index(card_id)
        return identities

    def _get_zone_visibility(self, card_ids, max_size, newest_first=False):
        visibility = np.zeros(max_size, dtype=bool)
        for index, card_id in enumerate(self._zone_window(
                card_ids, max_size, newest_first=newest_first)):
            visibility[index] = not self._card_identity_is_hidden(card_id)
        return visibility

    def _get_zone_features(self, card_ids, max_size, newest_first=False):
        """Return public card vectors in deterministic slot order."""
        features = np.zeros((max_size, self._feature_dim), dtype=np.float32)
        for i, card_id in enumerate(self._zone_window(
                card_ids, max_size, newest_first=newest_first)):
            features[i] = self._get_card_feature(card_id, self._feature_dim)
        return features

    def _relative_battlefield_index(self, card_id, me, opponent):
        for offset, player in ((0, me), (self.max_battlefield, opponent)):
            battlefield = player.get("battlefield", [])
            try:
                index = battlefield.index(card_id)
            except ValueError:
                continue
            if index < self.max_battlefield:
                return offset + index
        return None

    @staticmethod
    def _public_count(value, maximum=1_000_000):
        try:
            return min(max(0, int(value or 0)), maximum)
        except (TypeError, ValueError, OverflowError):
            return 0

    def _get_permanent_public_state(self, player, me, opponent):
        """Return public counters, damage, and attachment relationships."""
        counters_result = np.zeros(
            (self.max_battlefield, 6), dtype=np.int32)
        damage_result = np.zeros(self.max_battlefield, dtype=np.int32)
        attachment_targets = np.full(
            self.max_battlefield, -1, dtype=np.int32)
        attachment_counts = np.zeros(self.max_battlefield, dtype=np.int32)
        gs = self.game_state

        all_attachments = {}
        for attachment_owner in (gs.p1, gs.p2):
            all_attachments.update(attachment_owner.get("attachments", {}) or {})
        attached_to_counts = defaultdict(int)
        for target_id in all_attachments.values():
            attached_to_counts[target_id] += 1

        recognized = {"+1/+1", "-1/-1", "loyalty", "defense", "lore"}
        for index, card_id in enumerate(
                player.get("battlefield", [])[:self.max_battlefield]):
            card = gs.card_db.get(card_id)
            raw_counters = getattr(card, "counters", {}) or {}
            normalized_counters = {
                str(name).strip().casefold(): self._public_count(count)
                for name, count in raw_counters.items()
            }
            counters_result[index, 0] = normalized_counters.get("+1/+1", 0)
            counters_result[index, 1] = normalized_counters.get("-1/-1", 0)
            counters_result[index, 2] = self._public_count(
                normalized_counters.get(
                    "loyalty", getattr(card, "loyalty", 0)))
            counters_result[index, 3] = self._public_count(
                normalized_counters.get(
                    "defense", getattr(gs, "battle_cards", {}).get(
                        card_id, getattr(card, "defense", 0))))
            counters_result[index, 4] = self._public_count(
                normalized_counters.get(
                    "lore", getattr(gs, "saga_counters", {}).get(card_id, 0)))
            counters_result[index, 5] = self._public_count(sum(
                count for name, count in normalized_counters.items()
                if name not in recognized))
            damage_result[index] = self._public_count(
                player.get("damage_counters", {}).get(card_id, 0))
            target_id = all_attachments.get(card_id)
            if target_id is not None:
                relative_target = self._relative_battlefield_index(
                    target_id, me, opponent)
                if relative_target is not None:
                    attachment_targets[index] = relative_target
            attachment_counts[index] = self._public_count(
                attached_to_counts.get(card_id, 0), maximum=1000)
        return (
            counters_result, damage_result,
            attachment_targets, attachment_counts,
        )

    def _mana_pool_vector(self, player, pool_name, observation_key):
        colors = ("W", "U", "B", "R", "G", "C")
        pool = player.get(pool_name, {}) or {}
        return self._bounded_int_array(
            observation_key, [pool.get(color, 0) for color in colors])

    def _snow_mana_vector(self, player, observation_key):
        """Aggregate snow provenance across ordinary and restricted pools."""
        colors = ("W", "U", "B", "R", "G", "C")
        totals = {color: 0 for color in colors}
        for pool_name in ("snow_mana_pool", "phase_restricted_snow_mana"):
            for color, amount in (player.get(pool_name, {}) or {}).items():
                if color in totals:
                    totals[color] += self._public_count(amount, maximum=100)
        for pool in (player.get("conditional_snow_mana", {}) or {}).values():
            if not isinstance(pool, dict):
                continue
            for color, amount in pool.items():
                if color in totals:
                    totals[color] += self._public_count(amount, maximum=100)
        return self._bounded_int_array(
            observation_key, [totals[color] for color in colors])

    def _restricted_mana_vector(self, player, observation_key):
        colors = ("W", "U", "B", "R", "G", "C")
        totals = {color: 0 for color in colors}
        for pool_name in ("phase_restricted_mana",):
            for color, amount in (player.get(pool_name, {}) or {}).items():
                if color in totals:
                    totals[color] += self._public_count(amount, maximum=100)
        for pool in (player.get("conditional_mana", {}) or {}).values():
            if not isinstance(pool, dict):
                continue
            for color, amount in pool.items():
                if color in totals:
                    totals[color] += self._public_count(amount, maximum=100)
        return self._bounded_int_array(
            observation_key, [totals[color] for color in colors])

    def _player_counter_vector(self, player):
        return np.array([
            self._public_count(player.get("poison_counters", 0), 1000),
            self._public_count(player.get("energy_counters", 0), 1000),
            self._public_count(player.get("experience_counters", 0), 1000),
        ], dtype=np.int32)

    @staticmethod
    def _player_status_vector(player):
        return np.array([
            int(bool(player.get("city_blessing", False))),
            int(bool(player.get("monarch", False))),
        ], dtype=np.int32)

    @staticmethod
    def _count_stack_targets(value):
        if value is None:
            return 0
        if isinstance(value, dict):
            return sum(
                AlphaZeroMTGEnv._count_stack_targets(item)
                for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return sum(
                AlphaZeroMTGEnv._count_stack_targets(item) for item in value)
        return 1

    def _get_detailed_stack_observation(self, me_player):
        result = {
            "stack_cards": np.zeros(
                (5, self._feature_dim), dtype=np.float32),
            "stack_card_identity": np.zeros(5, dtype=np.int32),
            "stack_object_kinds": np.zeros(5, dtype=np.int32),
            "stack_target_counts": np.zeros(5, dtype=np.int32),
            "stack_mode_counts": np.zeros(5, dtype=np.int32),
        }
        kind_codes = {
            "SPELL": 1, "ABILITY": 2, "ACTIVATED_ABILITY": 2,
            "TRIGGER": 3, "TRIGGERED_ABILITY": 3,
        }
        for index, item in enumerate(reversed(self.game_state.stack[-5:])):
            if not isinstance(item, tuple) or len(item) < 2:
                result["stack_object_kinds"][index] = 4
                continue
            item_type, card_id = item[:2]
            context = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
            result["stack_cards"][index] = self._get_card_feature(
                card_id, self._feature_dim)
            result["stack_card_identity"][index] = \
                self._semantic_card_index(card_id)
            result["stack_object_kinds"][index] = kind_codes.get(
                str(item_type).upper(), 4)
            target_payload = context.get(
                "targets_by_slot", context.get("targets"))
            result["stack_target_counts"][index] = self._public_count(
                self._count_stack_targets(target_payload), maximum=1000)
            selected_modes = context.get(
                "selected_spree_modes", context.get("selected_modes", ()))
            if not isinstance(selected_modes, (list, tuple, set)):
                selected_modes = () if selected_modes is None else (selected_modes,)
            result["stack_mode_counts"][index] = self._public_count(
                len(selected_modes), maximum=100)
        return result

    def _get_combat_assignment_observation(self, me, opponent):
        attack_targets = np.full(
            self.max_battlefield * 2, -2, dtype=np.int32)
        blocker_assignments = np.full(
            self.max_battlefield * 2, -1, dtype=np.int32)
        gs = self.game_state
        pw_targets = getattr(gs, "planeswalker_attack_targets", {}) or {}
        battle_targets = getattr(gs, "battle_attack_targets", {}) or {}
        for attacker_id in getattr(gs, "current_attackers", ()) or ():
            attacker_index = self._relative_battlefield_index(
                attacker_id, me, opponent)
            if attacker_index is None:
                continue
            target_id = pw_targets.get(
                attacker_id, battle_targets.get(attacker_id))
            if target_id is None:
                attack_targets[attacker_index] = 0
            else:
                target_index = self._relative_battlefield_index(
                    target_id, me, opponent)
                # -1 distinguishes an attacker whose public defender is beyond
                # the fixed detail window from a non-attacker (-2).
                attack_targets[attacker_index] = (
                    -1 if target_index is None else target_index + 1)
            for blocker_id in getattr(
                    gs, "current_block_assignments", {}).get(
                        attacker_id, ()) or ():
                blocker_index = self._relative_battlefield_index(
                    blocker_id, me, opponent)
                if blocker_index is not None:
                    blocker_assignments[blocker_index] = attacker_index
        return attack_targets, blocker_assignments

    def _get_battlefield_flags(self, card_ids, player, max_size):
        """Helper to get flags (tapped, sick, atk, block, keywords) for battlefield."""
        flags = np.zeros((max_size, 5), dtype=np.float32)
        tapped_set = player.get("tapped_permanents", set())
        sick_set = player.get("entered_battlefield_this_turn", set())
        attackers_set = set(getattr(self.game_state, 'current_attackers', []))
        blocking_set = set()
        gs = self.game_state
        for blockers in getattr(self.game_state, 'current_block_assignments', {}).values():
            blocking_set.update(blockers)

        for i, card_id in enumerate(card_ids):
            if i >= max_size: break
            card = gs._safe_get_card(card_id)
            flags[i, 0] = float(card_id in tapped_set)
            # AbilityHandler owns layer-aware keyword checks; the environment
            # must not call the similarly named ActionHandler mixin method.
            has_haste = bool(
                card and getattr(gs, 'ability_handler', None)
                and gs.ability_handler.check_keyword(card_id, "haste"))
            if card and not getattr(gs, 'ability_handler', None):
                has_haste = "haste" in getattr(card, "oracle_text", "").lower()
            is_sick = card_id in sick_set and not has_haste
            flags[i, 1] = float(is_sick)
            flags[i, 2] = float(card_id in attackers_set)
            flags[i, 3] = float(card_id in blocking_set)
            flags[i, 4] = float(sum(getattr(card, 'keywords', [])) > 0 if card else 0) # Simple keyword check
        return flags

    def _get_creature_stats(self, creature_ids):
        """Helper to get aggregated power/toughness/count."""
        count = 0
        power = 0
        toughness = 0
        gs = self.game_state
        for card_id in creature_ids:
             card = gs._safe_get_card(card_id)
             # Check if it's actually a creature (type might change post-layers)
             # LayerSystem should have been applied before calling _get_obs
             if card and 'creature' in getattr(card, 'card_types', []):
                 count += 1
                 power += _card_number(card, 'power')
                 toughness += _card_number(card, 'toughness')
        return {"count": count, "power": power, "toughness": toughness} # Return dict

    def _get_hand_card_types(self, hand_ids):
        """Helper to get one-hot encoding of card types in hand."""
        types = np.zeros((self.hand_observation_size, 5), dtype=np.float32)
        gs = self.game_state
        for i, card_id in enumerate(hand_ids):
            if i >= self.hand_observation_size: break
            card = gs._safe_get_card(card_id)
            if card:
                type_line = getattr(card, 'type_line', '').lower()
                card_types = getattr(card, 'card_types', [])
                types[i, 0] = float('land' in type_line)
                types[i, 1] = float('creature' in card_types)
                types[i, 2] = float('instant' in card_types)
                types[i, 3] = float('sorcery' in card_types)
                types[i, 4] = float(not any(types[i, :4])) # Other
        return types

    def _get_hand_playable(self, hand_ids, player, is_my_turn):
        """Helper to determine playability flags for hand cards."""
        playable = np.zeros(self.hand_observation_size, dtype=np.float32)
        gs = self.game_state
        # Use GameState method which checks phase, stack, priority correctly
        can_play_sorcery = hasattr(gs, '_can_act_at_sorcery_speed') and gs._can_act_at_sorcery_speed(player)

        for i, card_id in enumerate(hand_ids):
            if i >= self.hand_observation_size: break
            card = gs._safe_get_card(card_id)
            if card:
                is_land = 'land' in getattr(card, 'type_line', '').lower()
                # *** FIXED: Check action_handler for _has_flash, not self ***
                has_flash = False
                if self.action_handler and hasattr(self.action_handler, '_has_flash'):
                    has_flash = self.action_handler._has_flash(card_id)
                else:
                    # Fallback check if action_handler is missing method
                    has_flash = self._has_flash_text(getattr(card, 'oracle_text', ''))

                is_instant_speed = 'instant' in getattr(card, 'card_types', []) or has_flash

                can_afford = self._can_afford_card(player, card)

                if is_land:
                    # Use the live land-play allowance so additional-land
                    # permissions stay visible to the observation.
                    if (gs.can_play_land_this_turn(player)
                            and can_play_sorcery):
                        playable[i] = 1.0
                elif can_afford:
                    # Spells need correct timing
                    if is_instant_speed:
                        # Can cast if player has priority (assume valid timing if instant)
                        # Action mask generation actually handles priority check, so assume OK here if affordable.
                        playable[i] = 1.0
                    elif can_play_sorcery:
                        playable[i] = 1.0
        return playable
    
    def _can_afford_card(self, player, card_or_data, is_back_face=False, context=None):
        """Check affordability using ManaSystem, handling dict or Card object.

        Delegates to the action handler's affordability check — the same one
        the action mask uses — so the hand "playable" observation cannot
        disagree with the mask. The previous pool-only check here reported
        every spell as unaffordable unless mana was already floating.
        """
        handler = getattr(self, 'action_handler', None)
        if handler is not None and hasattr(handler, '_can_afford_card'):
            return handler._can_afford_card(
                player, card_or_data, is_back_face=is_back_face,
                context=context)
        gs = self.game_state
        if context is None: context = {}
        # *** FIXED: Check mana_system exists on gs first ***
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) > 0 # Basic check

        if isinstance(card_or_data, dict): # E.g., back face data
            cost_str = card_or_data.get('mana_cost', '')
            card_id = card_or_data.get('id') # Need ID for context
        elif isinstance(card_or_data, Card):
            cost_str = getattr(card_or_data, 'mana_cost', '')
            card_id = getattr(card_or_data, 'card_id', None)
        else:
            return False # Invalid input

        if not cost_str and not context.get('use_alt_cost'): return True # Free spell (unless alt cost used)

        try:
            parsed_cost = gs.mana_system.parse_mana_cost(cost_str)
            # Apply cost modifiers based on context (Kicker, Additional, Alternative)
            final_cost = gs.mana_system.apply_cost_modifiers(player, parsed_cost, card_id, context)
            return gs.mana_system.can_pay_mana_cost_with_lands(
                player, final_cost, context)
        except Exception as e:
            card_name = getattr(card_or_data, 'name', 'Unknown') if isinstance(card_or_data, Card) else card_or_data.get('name', 'Unknown')
            logging.warning(f"Error checking mana cost for '{card_name}': {e}")
            return False

    def _has_flash_text(self, oracle_text):
        """Check if oracle text contains flash keyword."""
        return oracle_text and 'flash' in oracle_text.lower()

    def _can_act_at_sorcery_speed(self, player):
        """Helper to determine if the player can currently act at sorcery speed."""
        gs = self.game_state
        is_my_turn = (gs.p1 == player and (gs.turn % 2 == 1) == gs.agent_is_p1) or \
                     (gs.p2 == player and (gs.turn % 2 == 0) == gs.agent_is_p1)
        return (is_my_turn and
                gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and
                not gs.stack and # Stack must be empty
                gs.priority_player == player) # Player must have priority

    def _get_stack_info(self, stack, me_player):
        """Helper to get controller and type info for top stack items."""
        gs = self.game_state
        controllers = np.full(5, -1, dtype=np.int32) # -1=Empty, 0=Me, 1=Opp
        types = np.zeros((5, 5), dtype=np.float32) # Creature, Inst, Sorc, Ability, Other
        for i, item in enumerate(reversed(stack[-5:])): # Top 5 items
            if isinstance(item, tuple) and len(item) >= 3:
                item_type, card_id, controller = item[:3]
                card = gs._safe_get_card(card_id)
                controllers[i] = 0 if controller == me_player else 1
                if card:
                    card_types = getattr(card, 'card_types', [])
                    types[i, 0] = float('creature' in card_types)
                    types[i, 1] = float('instant' in card_types)
                    types[i, 2] = float('sorcery' in card_types)
                types[i, 3] = float(item_type == "ABILITY" or item_type == "TRIGGER")
                types[i, 4] = float(not any(types[i, :4])) # Other
        return controllers, types

    def _is_land(self, card_id):
        """Check if card is a land."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        return card and 'land' in getattr(card, 'type_line', '').lower()

    def _is_creature(self, card_id):
        """Check if card is a creature."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        return card and 'creature' in getattr(card, 'card_types', [])

    def _analysis_to_metrics(self, analysis):
        """Convert strategic analysis dict to metrics vector."""
        # Fallback: Return zeros if strategic planner is not available or analysis is None
        if not hasattr(self, 'strategic_planner') or not self.strategic_planner or not analysis:
            return np.zeros(7, dtype=np.float32)

        metrics = np.zeros(7, dtype=np.float32)
        # --- Safely access analysis keys with .get() ---
        metrics[0] = analysis.get("position", {}).get("score", 0)
        metrics[1] = analysis.get("board_state", {}).get("board_advantage", 0)
        card_adv = analysis.get("resources", {}).get("card_advantage", 0)
        metrics[2] = np.tanh(card_adv / 3.0)
        mana_adv = analysis.get("resources", {}).get("mana_advantage", 0)
        metrics[3] = np.tanh(mana_adv / 3.0)
        life_diff = analysis.get("life", {}).get("life_diff", 0)
        metrics[4] = life_diff / 20.0 # Normalize by starting life
        metrics[5] = analysis.get("tempo", {}).get("tempo_advantage", 0)
        stage = analysis.get("game_info", {}).get("game_stage", 'mid')
        metrics[6] = 0.0 if stage == 'early' else 0.5 if stage == 'mid' else 1.0
        return np.clip(metrics, -1.0, 1.0)

    def _observer_original_deck(self, observer):
        """Return the observer's own full starting decklist (card-id list).

        A player's own decklist is public information to that player.  Keyed on
        the actual player object so it is correct for either seat and never
        returns the opponent's deck (Observation v4 hidden-information rule).
        """
        gs = self.game_state
        if gs is not None and observer is gs.p1:
            return list(getattr(self, "original_p1_deck", []) or [])
        if gs is not None and observer is gs.p2:
            return list(getattr(self, "original_p2_deck", []) or [])
        return []

    def _deck_identity_index(self, card_id):
        """Canonical identity for a card in the observer's own decklist.

        Skips the face-down/hidden check that public-zone identities use: a
        card in your own deck is always known to you.  0 is reserved for
        padding, 1 for cards outside the frozen registry.
        """
        if card_id is None:
            return 0
        canonical_id = self.game_state.canonical_card_id(card_id)
        if (isinstance(canonical_id, (int, np.integer))
                and int(canonical_id) in self._canonical_card_ids):
            return int(canonical_id) + 2
        card = self.game_state.card_db.get(card_id)
        by_name = self._canonical_card_ids_by_name.get(
            str(getattr(card, "name", "")).casefold())
        if by_name is not None:
            return int(by_name) + 2
        return 1

    def _get_deck_composition(self, player):
        """Card-type ratios of the observer's full starting deck (v4).

        Was a backward-looking estimate over already-revealed cards; now it
        summarizes the true 60-card decklist, which the observer legitimately
        knows.  Shape is unchanged (6): creature, instant, sorcery, artifact,
        enchantment, land ratios.
        """
        composition = np.zeros(6, dtype=np.float32)
        gs = self.game_state
        deck_ids = self._observer_original_deck(player)
        total = len(deck_ids)
        if total == 0:
            return composition

        counts = defaultdict(int)
        for card_id in deck_ids:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            card_types = getattr(card, 'card_types', [])
            if 'creature' in card_types: counts['creature'] += 1
            elif 'instant' in card_types: counts['instant'] += 1
            elif 'sorcery' in card_types: counts['sorcery'] += 1
            elif 'artifact' in card_types: counts['artifact'] += 1
            elif 'enchantment' in card_types: counts['enchantment'] += 1
            elif 'land' in getattr(card, 'type_line', '').lower(): counts['land'] += 1

        composition[0] = counts['creature'] / total
        composition[1] = counts['instant'] / total
        composition[2] = counts['sorcery'] / total
        composition[3] = counts['artifact'] / total
        composition[4] = counts['enchantment'] / total
        composition[5] = counts['land'] / total
        return composition

    def _get_deck_card_identities(self, observer):
        """The observer's full starting decklist as canonical identities.

        Sorted ascending so the field is an order-free multiset -- it exposes
        the cards you own, never your hidden library draw order.  Padded with
        0 to the fixed contract size; a longer deck is truncated (a larger
        corpus would already start a new registry/observation lineage).
        """
        identities = np.zeros(MAX_DECK_OBSERVATION_SIZE, dtype=np.int32)
        deck_ids = self._observer_original_deck(observer)
        if not deck_ids:
            return identities
        encoded = sorted(
            self._deck_identity_index(card_id) for card_id in deck_ids)
        encoded = encoded[:MAX_DECK_OBSERVATION_SIZE]
        identities[:len(encoded)] = encoded
        return identities

    def _get_library_composition(self, observer):
        """Composition of the observer's remaining library (v4).

        The remaining library is what you have left to draw; you know it
        because you know your deck and everything you have already seen.
        Layout: 8 type counts, 7 mana-curve buckets (cmc 0..6+), 5 color
        counts (WUBRG), and the total remaining count.
        """
        composition = np.zeros(LIBRARY_COMPOSITION_SIZE, dtype=np.float32)
        gs = self.game_state
        library = observer.get("library", []) if observer else []
        if not library:
            return composition
        type_base = 0
        curve_base = len(LIBRARY_COMPOSITION_TYPES)
        color_base = curve_base + 7
        total_base = color_base + 5
        for card_id in library:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            card_types = getattr(card, 'card_types', []) or []
            type_line = getattr(card, 'type_line', '') or ''
            for offset, type_name in enumerate(LIBRARY_COMPOSITION_TYPES):
                if type_name in card_types or (
                        type_name == 'land' and 'land' in type_line.lower()):
                    composition[type_base + offset] += 1.0
            try:
                cmc = int(getattr(card, 'cmc', 0) or 0)
            except (TypeError, ValueError):
                cmc = 0
            composition[curve_base + min(max(cmc, 0), 6)] += 1.0
            colors = getattr(card, 'colors', None)
            if isinstance(colors, (list, tuple)):
                for color_offset in range(min(5, len(colors))):
                    if colors[color_offset]:
                        composition[color_base + color_offset] += 1.0
        composition[total_base] = float(len(library))
        return composition

    def _get_producible_mana(self, player):
        """Per-color (WUBRG) mana the player can produce right now (v5).

        Counts each visible untapped land toward every color it can produce
        (a dual counts for both -- color access, not simultaneous
        availability) and adds floating mana of that color.  Tapped and
        hidden sources are excluded, so the opponent's value is the public
        estimate from its face-up untapped lands and never leaks hidden
        information.
        """
        producible = np.zeros(5, dtype=np.float32)
        gs = self.game_state
        tapped = player.get("tapped_permanents", set()) if player else set()
        for card_id in (player.get("battlefield", []) if player else []):
            if card_id in tapped:
                continue
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            if "land" not in str(getattr(card, "type_line", "") or "").lower():
                continue
            colors = getattr(card, "colors", None)
            if isinstance(colors, (list, tuple)):
                for color_index in range(min(5, len(colors))):
                    if colors[color_index]:
                        producible[color_index] += 1.0
        pool = player.get("mana_pool", {}) if player else {}
        for color_index, color in enumerate("WUBRG"):
            producible[color_index] += float(pool.get(color, 0) or 0)
        return producible

    def _get_threat_assessment(self, opp_bf_ids, threat_list=None):
        """Assess threat level of opponent's board."""
        # Fallback: Return zeros if strategic planner is not available
        if (threat_list is None
                and (not hasattr(self, 'strategic_planner')
                     or not self.strategic_planner)):
            return np.zeros(self.max_battlefield, dtype=np.float32)

        threats = np.zeros(self.max_battlefield, dtype=np.float32)
        if (threat_list is not None
                or hasattr(
                    getattr(self, 'strategic_planner', None),
                    'assess_threats')):
            try:
                if threat_list is None:
                    threat_list = self.strategic_planner.assess_threats()
                threat_map = {t['card_id']: t['level'] for t in threat_list}
                for i, card_id in enumerate(opp_bf_ids):
                    if i >= self.max_battlefield: break
                    # Threat levels are open-ended (power-based scores can
                    # exceed 100 with doubling effects); saturate at the
                    # declared observation bound instead of tripping the
                    # degraded-observation guard.
                    threats[i] = min(
                        10.0, threat_map.get(card_id, 0.0) / 10.0)
            except Exception as e:
                logging.warning(f"Error getting threat assessment from planner: {e}")
                # Return zeros on error
        return threats

    def _get_opportunity_assessment(self, hand_ids, player):
        """Assess opportunities presented by cards in hand."""
        # Fallback: Return zeros if card evaluator is not available
        gs = self.game_state
        if not self.card_evaluator:
            return np.zeros(self.hand_observation_size, dtype=np.float32)

        opportunities = np.zeros(self.hand_observation_size, dtype=np.float32)
        if self.card_evaluator:
             for i, card_id in enumerate(hand_ids):
                  if i >= self.hand_observation_size: break
                  card = gs._safe_get_card(card_id)
                  if card:
                      # Evaluate playability *and* potential impact
                      can_play = self._get_hand_playable([card_id], player, self.game_state.turn % 2 == 1)[0] > 0
                      value = self.card_evaluator.evaluate_card(card_id, "play") if can_play else 0
                      opportunities[i] = min(1.0, value / 5.0) # Normalize max value
        return opportunities


    def _get_ability_features(self, bf_ids, player):
        """Get features related to activatable abilities. Ensures correct shape is returned. (Reinforced)"""
        space = self.observation_space["ability_features"]
        max_bf_size = space.shape[0]
        num_ability_features = space.shape[1]
        dtype = space.dtype
        features = np.zeros((max_bf_size, num_ability_features), dtype=dtype)
        gs = self.game_state

        try:
            # Ensure ability handler exists
            if not hasattr(gs, 'ability_handler') or gs.ability_handler is None:
                #logging.debug("Ability handler not available in _get_ability_features, returning zeros.")
                # Ensure the zero array shape is correct before returning
                if features.shape != space.shape:
                     logging.error(f"Initial zero array shape error in _get_ability_features! Shape: {features.shape}, Expected: {space.shape}")
                     features = np.zeros(space.shape, dtype=dtype) # Recreate with correct shape
                return features # Return zeros if handler missing

            # Proceed with populating features
            for i, card_id in enumerate(bf_ids):
                if i >= max_bf_size: break # Respect observation space size
                card = gs._safe_get_card(card_id)
                if not card: continue

                abilities = gs.ability_handler.get_activated_abilities(card_id)
                if not abilities: continue

                # Limit checks to defined feature dimension
                features[i, 0] = min(len(abilities), num_ability_features -1) # Count excludes itself

                activatable_count, mana_count, draw_count, removal_count = 0, 0, 0, 0
                for j, ability in enumerate(abilities):
                    if not ability: continue
                    # --- Check Feature Bounds Explicitly ---
                    feature_idx_base = 1 # Start features from index 1

                    # Check activatability
                    can_activate = False
                    if player and hasattr(gs.ability_handler, 'can_activate_ability'):
                         try:
                              if gs.ability_handler.can_activate_ability(card_id, j, player):
                                   can_activate = True
                         except Exception as can_act_e: pass # Logged before, keep silent here
                    if can_activate and (feature_idx_base + 0 < num_ability_features): activatable_count += 1

                    # Analyze effect text
                    effect_text = getattr(ability, 'effect_text', '').lower()
                    if not effect_text: continue

                    # Check mana
                    is_mana = isinstance(ability, ManaAbility) or ("add mana" in effect_text or "add {" in effect_text and "target" not in effect_text)
                    if is_mana and (feature_idx_base + 1 < num_ability_features): mana_count += 1

                    # Check draw
                    if "draw" in effect_text and "card" in effect_text and (feature_idx_base + 2 < num_ability_features): draw_count += 1

                    # Check removal
                    is_removal = "destroy" in effect_text or "exile" in effect_text or ("deal" in effect_text and "damage" in effect_text)
                    if is_removal and (feature_idx_base + 3 < num_ability_features): removal_count += 1


                # Assign calculated features, respecting bounds
                if num_ability_features > 1: features[i, 1] = activatable_count
                if num_ability_features > 2: features[i, 2] = mana_count
                if num_ability_features > 3: features[i, 3] = draw_count
                if num_ability_features > 4: features[i, 4] = removal_count

        except Exception as e:
            # Log error and return the initialized zero array
            log_card_id = card_id if 'card_id' in locals() else 'unknown'
            logging.error(f"Error populating _get_ability_features for {log_card_id}: {e}", exc_info=True)
            # Ensure zero array shape is correct
            if features.shape != space.shape:
                 features = np.zeros(space.shape, dtype=dtype)
            return features

        # Final shape check before return
        if features.shape != space.shape:
            logging.error(f"_get_ability_features: FINAL Shape mismatch! Expected {space.shape}, got {features.shape}. Padding/truncating.")
            corrected_features = np.zeros(space.shape, dtype=dtype)
            copy_shape = (min(features.shape[0], max_bf_size), min(features.shape[1], num_ability_features))
            corrected_features[:copy_shape[0], :copy_shape[1]] = features[:copy_shape[0], :copy_shape[1]]
            return corrected_features

        return features

    def _get_ability_timing(self, phase):
        """Get appropriateness score for ability types based on phase."""
        timing = np.zeros(5, dtype=np.float32) # Mana, Draw, Removal, Combat, Setup
        gs = self.game_state
        is_main = phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]
        is_combat = phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE]
        is_eot = phase == gs.PHASE_END_STEP

        timing[0] = 1.0 if is_main else 0.7 # Mana (usually main, but ok instant speed)
        timing[1] = 1.0 if is_main and not gs.stack else 0.4 # Draw (best at sorcery speed)
        timing[2] = 1.0 if is_main or is_combat or is_eot else 0.5 # Removal (flexible)
        timing[3] = 1.0 if is_combat else 0.2 # Combat tricks
        timing[4] = 1.0 if is_main else 0.6 # Setup (Counters, Tapping etc)
        return timing

    def _get_multi_turn_plan_metrics(self, plan=None):
        """Convert strategic planner's multi-turn plan to metrics."""
        metrics = np.zeros(6, dtype=np.float32)

        def normalized_count(values, denominator):
            try:
                count = len(values or [])
            except TypeError:
                count = 0
            return min(1.0, max(0.0, count / denominator))

        def normalized_number(value, denominator):
            try:
                number = float(value or 0)
            except (TypeError, ValueError):
                return 0.0
            if not np.isfinite(number):
                return 0.0
            return min(1.0, max(0.0, number / denominator))

        if (plan is not None or (
                self.strategic_planner
                and hasattr(
                    self.strategic_planner, 'plan_multi_turn_sequence'))):
            try:
                if plan is None:
                    plan = self.strategic_planner.plan_multi_turn_sequence(
                        depth=2)
                if plan:
                    metrics[0] = normalized_count(
                        plan[0].get('plays'), 3.0) # Plays this turn
                    metrics[1] = float(plan[0].get('land_play') is not None) # Land this turn
                    metrics[2] = normalized_number(
                        plan[0].get('expected_mana'), 10.0) # Mana this turn
                    if len(plan) > 1:
                         metrics[3] = normalized_count(
                             plan[1].get('plays'), 3.0) # Plays next turn
                         metrics[4] = float(plan[1].get('land_play') is not None) # Land next turn
                         metrics[5] = normalized_number(
                             plan[1].get('expected_mana'), 10.0) # Mana next turn
            except Exception as plan_e:
                 logging.warning(f"Error getting multi-turn plan metrics: {plan_e}")
        return metrics

    def _get_mulligan_info(self, player):
        """Get mulligan recommendation and reasons."""
        # Fallback: Return defaults if planner is not available
        recommendation = 0.5 # Neutral default
        reasons_arr = np.zeros(5, dtype=np.float32)
        reason_count = 0
        if not self.strategic_planner or not hasattr(self.strategic_planner, 'suggest_mulligan_decision'):
             return recommendation, reasons_arr, reason_count

        gs = self.game_state
        if getattr(gs, 'mulligan_in_progress', False) and getattr(gs, 'mulligan_player', None) == player:
            try:
                 is_on_play = (gs.turn == 1 and gs.agent_is_p1) # Simplified on_play check
                 deck_name = self.current_deck_name_p1 if player == gs.p1 else self.current_deck_name_p2
                 decision = self.strategic_planner.suggest_mulligan_decision(player.get("hand",[]), deck_name, is_on_play)
                 recommendation = float(decision.get('keep', False))
                 # Map reasons to indices
                 reason_codes = {"Too few lands": 0, "Too many lands": 1, "No early plays": 2, "Too many expensive cards": 3, "Lacks interaction": 4}
                 for i, reason in enumerate(decision.get('reasoning', [])[:5]):
                     if reason in reason_codes:
                          reasons_arr[reason_codes[reason]] = 1.0 # Set flag for this reason
                     else: # If reason text not in map, use next available slot
                          if i < len(reasons_arr): reasons_arr[i] = 0.5 # Generic reason marker
                 reason_count = min(5, len(decision.get('reasoning', [])))
            except Exception as mull_e:
                 logging.warning(f"Error getting mulligan recommendation: {mull_e}")
        return recommendation, reasons_arr, reason_count


    def _get_attacker_values(self, bf_ids, player):
        """Evaluate the strategic value of attacking with each potential attacker."""
        # Fallback: Return zeros if strategic planner is not available
        gs = self.game_state
        if not hasattr(self, 'strategic_planner') or not self.strategic_planner or not hasattr(self.strategic_planner, 'evaluate_attack_action'):
            logging.debug("Strategic planner or evaluate_attack_action missing for attacker values.")
            return np.zeros(self.max_battlefield, dtype=np.float32)

        values = np.zeros(self.max_battlefield, dtype=np.float32)
        # Use self.action_handler for attacker check
        if not hasattr(self, 'action_handler') or not self.action_handler:
            logging.warning("Action handler missing for attacker check.")
            return values # Need action handler for validation

        for i, card_id in enumerate(bf_ids):
            if i >= self.max_battlefield: break
            # Check if valid attacker first
            if self.action_handler.is_valid_attacker(card_id):
                try:
                    value = self.strategic_planner.evaluate_attack_action([card_id]) # Evaluate attacking alone
                    values[i] = np.clip(value, -10.0, 10.0) # Clip to bounds
                except Exception as atk_eval_e:
                    logging.warning(f"Error evaluating single attacker {card_id}: {atk_eval_e}")
                    # Fallback value based on power?
                    card = gs._safe_get_card(card_id)
                    values[i] = _card_number(card, 'power') * 0.5
        return values

    def _phase_to_onehot(self, phase):
        """Convert phase to one-hot encoding for better RL learning"""
        # BUGFIX: size from the true highest PHASE_ constant, not PHASE_CLEANUP,
        # so special phases (16-19) get a real one-hot instead of all zeros.
        max_phase = max(v for k, v in vars(type(self.game_state)).items()
                        if k.startswith("PHASE_") and isinstance(v, int))
        onehot = np.zeros(max_phase + 1, dtype=np.float32)
        
        # Only set the element if it's within bounds
        if 0 <= phase <= max_phase:
            onehot[phase] = 1.0
        else:
            # Log warning if phase is out of expected range
            logging.warning(f"Phase {phase} is out of the expected range (0-{max_phase})")
        
        return onehot
