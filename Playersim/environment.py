import random
import logging
import numpy as np
import gymnasium as gym
from gymnasium import spaces
from .card import load_decks_and_card_db, Card
from .game_state import GameState
from .actions import ActionHandler # Assuming ActionHandler is now lean
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions, apply_combat_action
from .enhanced_mana_system import EnhancedManaSystem
from .enhanced_card_evaluator import EnhancedCardEvaluator
from .strategic_planner import MTGStrategicPlanner
# Ensure DEBUG_MODE exists or default it
try:
    from .debug import DEBUG_MODE, DEBUG_ACTION_STEPS
except ImportError:
    DEBUG_MODE = False
    DEBUG_ACTION_STEPS = False
import time
from .strategy_memory import StrategyMemory
from collections import defaultdict
from .layer_system import LayerSystem
from .replacement_effects import ReplacementEffectSystem
from .deck_stats_tracker import DeckStatsCollector # NOTE: Renamed? DeckStatsTracker seems more likely based on usage. Assume Tracker.
try:
    from .deck_stats_tracker import DeckStatsTracker
except ImportError:
    # Fallback if DeckStatsTracker doesn't exist
    class DeckStatsTracker:
        def record_game(self, *args, **kwargs): pass
        def save_updates_sync(self): pass

from .card_memory import CardMemory

# Define FEATURE_DIM more safely or pass it around
# Attempt to determine dynamically first
try:
    # Create a dummy card to get feature dimension
    dummy_card_data = {"name": "Dummy", "type_line": "Creature", "mana_cost": "{1}"}
    FEATURE_DIM = len(Card(dummy_card_data).to_feature_vector())
    logging.info(f"Determined FEATURE_DIM dynamically: {FEATURE_DIM}")
except Exception as e:
    logging.warning(f"Could not determine FEATURE_DIM dynamically, using fallback 223: {e}")
    FEATURE_DIM = 223 # Fallback

class AlphaZeroMTGEnv(gym.Env):
    """
    An example Magic: The Gathering environment that uses the Gymnasium (>= 0.26) API.
    Updated for improved reward shaping, richer observations, modularity, and detailed logging.
    """
    ACTION_SPACE_SIZE = 480 # Moved constant here

    def __init__(self, decks, card_db, max_turns=20, max_hand_size=7, max_battlefield=20):
        super().__init__()
        self.decks = decks
        self.card_db = card_db
        self.max_turns = max_turns
        self.max_hand_size = max_hand_size
        self.max_battlefield = max_battlefield
        self.strategy_memory = StrategyMemory()
        self.current_episode_actions = []
        self.current_analysis = None
        self._feature_dim = FEATURE_DIM # Store determined feature dim

        # Initialize deck statistics tracker
        try:
            # Use the imported DeckStatsTracker (fixed name)
            self.stats_tracker = DeckStatsTracker()
            self.has_stats_tracker = True
        except (ImportError, ModuleNotFoundError, NameError): # Added NameError
            logging.warning("DeckStatsTracker not available, statistics will not be recorded")
            self.stats_tracker = None
            self.has_stats_tracker = False
        try:
            self.card_memory = CardMemory()
            self.has_card_memory = True
            logging.info("Card memory system initialized successfully")
        except ImportError:
            logging.warning("CardMemory not available, historical card data will not be tracked")
            self.card_memory = None
            self.has_card_memory = False
        # Track cards played during the game
        self.cards_played = {0: [], 1: []}  # Player index -> list of card IDs

        # Initialize game state manager
        self.game_state = GameState(self.card_db, max_turns, max_hand_size, max_battlefield)

        # Initialize action handler AFTER GameState
        self.action_handler = ActionHandler(self.game_state)

        # GameState initializes its own subsystems now
        self.combat_resolver = getattr(self.game_state, 'combat_resolver', None) # Get ref if needed
        integrate_combat_actions(self.game_state) # Integrate after GS subsystems are ready

        # Feature dimension determined dynamically above
        MAX_PHASE = self.game_state.PHASE_CHOOSE # Use latest defined phase constant

        self.action_memory_size = 80

        # Correct keyword size based on Card class
        keyword_dimension = len(Card.ALL_KEYWORDS)
        logging.info(f"Using keyword dimension: {keyword_dimension}")

        # --- UPDATED: Refined Observation Space ---
        self.observation_space = spaces.Dict({
            "phase": spaces.Box(low=0, high=MAX_PHASE, shape=(1,), dtype=np.int32), # Changed from Discrete
            "phase_onehot": spaces.Box(low=0, high=1, shape=(MAX_PHASE + 1,), dtype=np.float32),
            "turn": spaces.Box(low=0, high=self.max_turns, shape=(1,), dtype=np.int32),
            "is_my_turn": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "my_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "opp_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "life_difference": spaces.Box(low=-40, high=40, shape=(1,), dtype=np.int32),
            # Player state (Absolute) - Kept for backward compatibility? Check usage.
            "p1_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p2_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            # Hand
            "my_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, self._feature_dim), dtype=np.float32),
            "my_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "opp_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "hand_playable": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_card_types": spaces.Box(low=0, high=1, shape=(self.max_hand_size, 5), dtype=np.float32),
            "hand_performance": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_synergy_scores": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "opportunity_assessment": spaces.Box(low=0, high=10, shape=(self.max_hand_size,), dtype=np.float32),
            # Battlefield
            "my_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "my_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "my_battlefield_keywords": spaces.Box(low=0, high=1, shape=(self.max_battlefield, keyword_dimension), dtype=np.float32),
            "my_tapped_permanents": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=bool), # Renamed
            "opp_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "opp_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            # Player state (Absolute) - Kept for potential model reliance
            "p1_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "p2_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "p1_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "p2_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            # Creature Stats
            "my_creature_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "opp_creature_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "my_total_power": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_total_toughness": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_total_power": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_total_toughness": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "creature_advantage": spaces.Box(low=-self.max_battlefield, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "power_advantage": spaces.Box(low=-100, high=100, shape=(1,), dtype=np.int32),
            "toughness_advantage": spaces.Box(low=-100, high=100, shape=(1,), dtype=np.int32),
            "threat_assessment": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "card_synergy_scores": spaces.Box(low=-1, high=1, shape=(self.max_battlefield, self.max_battlefield), dtype=np.float32),
            # Mana
            "my_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32), # WUBRGC
            "my_mana": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32), # Sum of mana pool - REMOVE? redundant? Keep for now.
            "total_available_mana": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "untapped_land_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "remaining_mana_sources": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32), # Maybe same as untapped_land_count? Revisit if needed.
            "turn_vs_mana": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            # Graveyard / Exile
            "my_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "graveyard_key_cards": spaces.Box(low=-1, high=50, shape=(10, self._feature_dim), dtype=np.float32),
            "exile_key_cards": spaces.Box(low=-1, high=50, shape=(10, self._feature_dim), dtype=np.float32),
            # Stack / Combat
            "stack_count": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "stack_controller": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.int32),
            "stack_card_types": spaces.Box(low=0, high=1, shape=(5, 5), dtype=np.float32),
            "attackers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "blockers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "potential_combat_damage": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            # Abilities
            "ability_features": spaces.Box(low=0, high=10, shape=(self.max_battlefield, 5), dtype=np.float32),
            "ability_timing": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "planeswalker_activations": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32), # Activated this turn
            "planeswalker_activation_counts": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32), # Total activations
            # History / Recommendations
            "previous_actions": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE, shape=(self.action_memory_size,), dtype=np.int32),
            "previous_rewards": spaces.Box(low=-10, high=10, shape=(self.action_memory_size,), dtype=np.float32),
            "phase_history": spaces.Box(low=-1, high=MAX_PHASE, shape=(5,), dtype=np.int32), # Last 5 phases
            "action_mask": spaces.Box(low=0, high=1, shape=(self.ACTION_SPACE_SIZE,), dtype=bool),
            "recommended_action": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE - 1, shape=(1,), dtype=np.int32), # Corrected upper bound, allow -1
            "recommended_action_confidence": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "memory_suggested_action": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE - 1, shape=(1,), dtype=np.int32), # Corrected upper bound, allow -1
            "suggestion_matches_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "optimal_attackers": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32), # Mask of optimal attackers
            "attacker_values": spaces.Box(low=-10, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "ability_recommendations": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5, 2), dtype=np.float32), # Recommend, Confidence per ability
            # Strategic / Meta
            "strategic_metrics": spaces.Box(low=-1, high=1, shape=(10,), dtype=np.float32),
            "position_advantage": spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32),
            "estimated_opponent_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, self._feature_dim), dtype=np.float32), # Needs opponent hand estimation model
            "deck_composition_estimate": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Estimate based on observed cards
            "opponent_archetype": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Prediction from planner
            "future_state_projections": spaces.Box(low=-1, high=1, shape=(7,), dtype=np.float32), # Projections from planner
            "multi_turn_plan": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Simplified plan metrics
            "win_condition_viability": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Scores for different win cons
            "win_condition_timings": spaces.Box(low=0, high=self.max_turns + 1, shape=(6,), dtype=np.float32), # Estimated turns for each win con
            "resource_efficiency": spaces.Box(low=0, high=1, shape=(3,), dtype=np.float32), # Mana, Card, Tempo
            # Mulligan
            "mulligan_in_progress": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "mulligan_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "mulligan_reason_count": spaces.Box(low=0, high=5, shape=(1,), dtype=np.int32),
            "mulligan_reasons": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32), # Top 5 reason flags/scores
        })
        # --- End Observation Space Update ---

        # Add memory for actions and rewards
        self.last_n_actions = np.full(self.action_memory_size, -1, dtype=np.int32) # Use -1 for padding
        self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)

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

        

    def initialize_strategic_memory(self):
        """Initialize and connect the strategy memory system."""
        try:
            from .strategy_memory import StrategyMemory
            self.strategy_memory = StrategyMemory()
            # Enable the strategy memory to access critical game state components
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
                    from .actions import ActionHandler # Import locally if needed
                    self.action_handler = ActionHandler(self.game_state)

            self.current_valid_actions = self.action_handler.generate_valid_actions()

            # Validate the generated mask
            if self.current_valid_actions is None or not isinstance(self.current_valid_actions, np.ndarray) or self.current_valid_actions.shape != (self.ACTION_SPACE_SIZE,):
                raise ValueError(f"generate_valid_actions returned invalid mask: shape {getattr(self.current_valid_actions, 'shape', 'None')}, type {type(self.current_valid_actions)}")

        except Exception as e:
            logging.error(f"Error generating valid actions in action_mask: {str(e)}")
            import traceback
            logging.error(f"{traceback.format_exc()}")
            # Fallback to basic action mask if generation fails
            self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            self.current_valid_actions[11] = True # Enable PASS_PRIORITY
            self.current_valid_actions[12] = True  # Enable CONCEDE as fallback

        # Return bool version
        return self.current_valid_actions.astype(bool)
    
    def reset(self, seed=None, **kwargs):
        """
        Reset the environment and return initial observation and info.

        Args:
            seed: Random seed
            **kwargs: Additional keyword arguments (required by Gymnasium API)

        Returns:
            tuple: Initial observation and info dictionary
        """
        env_id = getattr(self, "env_id", id(self)) # For tracking
        try:
            # --- Pre-Reset Logging & Safety Checks ---
            # Log the reset attempt
            logging.info(f"RESETTING environment {env_id}...")
            if DEBUG_MODE:
                import traceback
                logging.debug(f"Reset call stack (last 5 frames):\n{''.join(traceback.format_stack()[-6:-1])}")

            # Simple check for rapid resets
            current_time = time.time()
            if hasattr(self, '_last_reset_time') and current_time - self._last_reset_time < 0.1:
                logging.warning(f"Multiple resets detected within {current_time - self._last_reset_time:.3f}s!")
            self._last_reset_time = current_time

            # Call parent reset method (for seeding primarily)
            super().reset(seed=seed)

            # --- Reset Internal Environment State ---
            self.current_step = 0
            self.invalid_action_count = 0
            self.episode_rewards = []
            self.episode_invalid_actions = 0
            self.current_episode_actions = []
            self.cards_played = {0: [], 1: []}
            self.mulligan_data = {'p1': 0, 'p2': 0} # Reset mulligan stats
            self._game_result_recorded = False # Reset recording flag
            self._logged_card_ids = set() # Reset logging trackers
            self._logged_errors = set()   # Reset logging trackers
            self.last_n_actions = np.full(self.action_memory_size, -1, dtype=np.int32) # Reset action history
            self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32) # Reset reward history


            # --- Reset GameState and Player Setup ---
            # Choose random decks
            p1_deck_data = random.choice(self.decks)
            p2_deck_data = random.choice(self.decks)
            self.current_deck_name_p1 = p1_deck_data["name"]
            self.current_deck_name_p2 = p2_deck_data["name"]
            self.original_p1_deck = p1_deck_data["cards"].copy() # Store original for memory
            self.original_p2_deck = p2_deck_data["cards"].copy()

            # Initialize GameState (creates players, resets turn/phase, etc.)
            self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
            # GameState's reset performs deck setup, shuffling, initial draw
            self.game_state.reset(p1_deck_data["cards"], p2_deck_data["cards"], seed)

            # --- Initialize & Link Subsystems to GameState ---
            # GameState._init_subsystems() should handle this now.
            # We need references to these subsystems in the environment though.

            # Initialize strategy memory early if others depend on it
            self.initialize_strategic_memory()
            if self.strategy_memory:
                 self.game_state.strategy_memory = self.strategy_memory

            # Initialize stats/card memory trackers if available and link to GS
            if self.has_stats_tracker and self.stats_tracker:
                self.game_state.stats_tracker = self.stats_tracker
                self.stats_tracker.current_deck_name_p1 = self.current_deck_name_p1
                self.stats_tracker.current_deck_name_p2 = self.current_deck_name_p2
            if self.has_card_memory and self.card_memory:
                self.game_state.card_memory = self.card_memory


            # --- Initialize Environment Components Using GameState Subsystems ---
            # Action Handler depends on GameState and its subsystems
            self.action_handler = ActionHandler(self.game_state) # Recreate/link ActionHandler

            # Get references to components created by GameState for env use
            # Add checks to ensure subsystems were initialized in GameState
            subsystems_to_check = ['combat_resolver', 'card_evaluator', 'strategic_planner',
                                   'mana_system', 'ability_handler', 'layer_system',
                                   'replacement_effects', 'targeting_system']
            for system_name in subsystems_to_check:
                if hasattr(self.game_state, system_name):
                    setattr(self, system_name, getattr(self.game_state, system_name))
                    if getattr(self, system_name) is None:
                        logging.warning(f"GameState has attribute '{system_name}', but it is None after initialization.")
                else:
                    logging.warning(f"GameState is missing expected subsystem: '{system_name}'. Setting environment reference to None.")
                    setattr(self, system_name, None)


            # Ensure combat integration after handlers are created
            # Integrate combat actions checks if combat_resolver exists now
            integrate_combat_actions(self.game_state)
            # Ensure CombatActionHandler has link to GS components (safe access)
            if hasattr(self.action_handler, 'combat_handler') and self.action_handler.combat_handler:
                self.action_handler.combat_handler.game_state = self.game_state
                self.action_handler.combat_handler.card_evaluator = getattr(self.game_state, 'card_evaluator', None)
                self.action_handler.combat_handler.setup_combat_systems()


            # --- Final Reset Steps ---
            # Reset mulligan state AFTER subsystems are ready
            gs = self.game_state # Alias for convenience
            gs.mulligan_in_progress = True
            gs.mulligan_player = gs.p1 # Start with P1's mulligan decision
            gs.mulligan_count = {'p1': 0, 'p2': 0}
            gs.bottoming_in_progress = False
            gs.bottoming_player = None
            gs.cards_to_bottom = 0
            gs.bottoming_count = 0


            # Perform initial game state analysis if planner exists
            if self.strategic_planner:
                try:
                    self.strategic_planner.analyze_game_state()
                except Exception as analysis_e:
                    logging.warning(f"Error during initial game state analysis: {analysis_e}")

            # Initialize Action Mask for the starting player
            self.current_valid_actions = self.action_mask()

            # Get initial observation and info
            obs = self._get_obs_safe()
            info = {
                "action_mask": self.current_valid_actions.astype(bool),
                "initial_state": True
            }


            logging.info(f"Environment {env_id} reset complete. Starting new episode (Turn {gs.turn}, Phase {gs.phase}).")
            logging.info(f"P1 Deck: {self.current_deck_name_p1}, P2 Deck: {self.current_deck_name_p2}")

            return obs, info

        except Exception as e:
            logging.critical(f"CRITICAL error during environment reset: {str(e)}")
            logging.critical(traceback.format_exc())

            # Emergency reset fallback (simplified)
            try:
                logging.warning("Attempting emergency fallback reset...")
                # Basic GameState init
                self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
                deck = self.decks[0]["cards"].copy() if self.decks else [0]*60 # Use default card 0 if decks fail
                # Simplified reset, relies on _init_player and basic setup
                self.game_state.p1 = self.game_state._init_player(deck.copy())
                self.game_state.p2 = self.game_state._init_player(deck.copy())
                self.game_state.turn = 1
                self.game_state.phase = self.game_state.PHASE_MAIN_PRECOMBAT # Skip first phases
                self.game_state.mulligan_in_progress = False # Skip mulligans
                self.game_state.agent_is_p1 = True

                # Minimal subsystems (need at least ActionHandler)
                self.game_state._init_subsystems() # Try subsystem init
                self.action_handler = ActionHandler(self.game_state) # Ensure handler exists

                self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                self.current_valid_actions[11] = True # PASS
                self.current_valid_actions[12] = True # CONCEDE

                obs = self._get_obs_safe() # Attempt to get obs
                info = {"action_mask": self.current_valid_actions.astype(bool), "error_reset": True}
                logging.info("Emergency reset completed with minimal state.")
                return obs, info
            except Exception as fallback_e:
                 logging.critical(f"FALLBACK RESET FAILED: {fallback_e}")
                 # If even fallback fails, raise the error
                 raise fallback_e
        
    def ensure_game_result_recorded(self):
        """Make sure game result is recorded if it hasn't been already"""
        if getattr(self, '_game_result_recorded', False):
            return  # Already recorded

        gs = self.game_state
        # Ensure players exist
        if not hasattr(gs, 'p1') or not hasattr(gs, 'p2'):
            logging.error("Cannot record game result: Player data missing.")
            return

        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1

        is_p1_winner = False
        winner_life = 0
        is_draw = False

        # Determine the winner based on game state
        if me.get("lost_game", False) or me.get("life", 20) <= 0 or me.get("attempted_draw_from_empty", False) or me.get("poison_counters", 0) >= 10:
            is_p1_winner = not gs.agent_is_p1
            winner_life = opp.get("life", 0)
        elif opp.get("lost_game", False) or opp.get("life", 20) <= 0 or opp.get("attempted_draw_from_empty", False) or opp.get("poison_counters", 0) >= 10:
            is_p1_winner = gs.agent_is_p1
            winner_life = me.get("life", 0)
        elif me.get("won_game", False):
            is_p1_winner = gs.agent_is_p1
            winner_life = me.get("life", 0)
        elif opp.get("won_game", False):
            is_p1_winner = not gs.agent_is_p1
            winner_life = opp.get("life", 0)
        elif me.get("game_draw", False) or opp.get("game_draw", False) or me.get("life", 0) == opp.get("life", 0):
             is_draw = True
             winner_life = me.get("life", 0) # Record life even in draw
        else: # Game ended due to turn limit or other reason
            my_final_life = me.get("life", 0)
            opp_final_life = opp.get("life", 0)
            if my_final_life > opp_final_life:
                is_p1_winner = gs.agent_is_p1
                winner_life = my_final_life
            elif opp_final_life > my_final_life:
                is_p1_winner = not gs.agent_is_p1
                winner_life = opp_final_life
            else: # Draw by life at turn limit
                is_draw = True
                winner_life = my_final_life

        # Record the game result
        if self.has_stats_tracker and self.stats_tracker:
            try:
                self.stats_tracker.record_game(
                     winner_is_p1=is_p1_winner if not is_draw else None, # Pass None for draw
                     turn_count=gs.turn,
                     winner_life=winner_life if not is_draw else None,
                     is_draw=is_draw # Pass draw flag
                )
                self._game_result_recorded = True
            except Exception as stat_e:
                 logging.error(f"Error during stats_tracker.record_game: {stat_e}")

        if self.has_card_memory and self.card_memory:
            try:
                winner_deck = self.original_p1_deck if is_p1_winner else self.original_p2_deck
                loser_deck = self.original_p2_deck if is_p1_winner else self.original_p1_deck
                winner_archetype = self.current_deck_name_p1 if is_p1_winner else self.current_deck_name_p2
                loser_archetype = self.current_deck_name_p2 if is_p1_winner else self.current_deck_name_p1

                # Provide empty defaults if tracking data is missing
                cards_played_data = getattr(self, 'cards_played', {0: [], 1: []})
                opening_hands_data = getattr(self.game_state, 'opening_hands', {}) # Assuming GS tracks this
                draw_history_data = getattr(self.game_state, 'draw_history', {}) # Assuming GS tracks this

                # Handle draw case for memory recording
                if is_draw:
                     # Record stats for both decks as a draw
                     self._record_cards_to_memory(self.original_p1_deck, self.original_p2_deck, cards_played_data, gs.turn,
                                            self.current_deck_name_p1, self.current_deck_name_p2, opening_hands_data, draw_history_data, is_draw=True, player_idx=0) # P1 perspective
                     self._record_cards_to_memory(self.original_p2_deck, self.original_p1_deck, cards_played_data, gs.turn,
                                            self.current_deck_name_p2, self.current_deck_name_p1, opening_hands_data, draw_history_data, is_draw=True, player_idx=1) # P2 perspective
                else:
                     # Record winner/loser normally
                     self._record_cards_to_memory(winner_deck, loser_deck, cards_played_data, gs.turn,
                                                winner_archetype, loser_archetype, opening_hands_data, draw_history_data, is_draw=False, player_idx=(0 if is_p1_winner else 1))

            except Exception as mem_e:
                 logging.error(f"Error recording cards to memory: {mem_e}")
                 import traceback; logging.error(traceback.format_exc())
                 
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
        Check if the game is potentially stuck in a phase and force progression when necessary.
        This prevents the game from getting stuck in certain phases.
        
        Returns:
            bool: True if phase was forced to progress, False otherwise
        """
        gs = self.game_state
        
        # Check if we have phase history tracking
        if not hasattr(self, '_phase_history'):
            self._phase_history = []
            self._phase_timestamps = []
            self._phase_stuck_count = 0
        
        current_time = time.time()
        current_phase = gs.phase
        
        # Update phase history
        self._phase_history.append(current_phase)
        self._phase_timestamps.append(current_time)
        
        # Keep history limited to reasonable size
        max_history = 50
        if len(self._phase_history) > max_history:
            self._phase_history = self._phase_history[-max_history:]
            self._phase_timestamps = self._phase_timestamps[-max_history:]
        
        # Check for phase getting stuck
        stuck_threshold = 20  # Number of consecutive identical phases to consider "stuck"
        time_threshold = 10.0  # Seconds to consider a phase potentially stuck
        
        # Only check if we have enough history
        if len(self._phase_history) >= stuck_threshold:
            # Check if last N phases are identical
            recent_phases = self._phase_history[-stuck_threshold:]
            if all(phase == recent_phases[0] for phase in recent_phases):
                # Check time spent in this phase
                phase_time = current_time - self._phase_timestamps[-stuck_threshold]
                if phase_time > time_threshold:
                    # Phase appears stuck, force progression
                    self._phase_stuck_count += 1
                    
                    # Log the issue
                    logging.warning(f"Game potentially stuck in phase {current_phase} for {phase_time:.1f} seconds. Forcing progression. (Occurrence #{self._phase_stuck_count})")
                    
                    # Force phase transition based on current phase
                    forced_phase = self._force_phase_transition(current_phase)
                    
                    # Set flag for reward penalty
                    gs.progress_was_forced = True
                    
                    # Update phase history to reflect the forced change
                    self._phase_history[-1] = forced_phase
                    
                    return True
        
        # No progression was forced
        return False
    
    def _record_cards_to_memory(self, player_deck, opponent_deck, cards_played_data, turn_count,
                            player_archetype, opponent_archetype, opening_hands_data, draw_history_data, is_draw=False, player_idx=0):
        """Record detailed card performance data to the card memory system, handles draw."""
        # Renamed: winner/loser -> player/opponent for clarity, added is_draw flag
        try:
            if not self.has_card_memory or not self.card_memory: return

            # Determine player/opponent indices based on the perspective we are recording
            player_key = player_idx # 0 for P1, 1 for P2
            opponent_key = 1 - player_key

            player_played = cards_played_data.get(player_key, [])
            player_opening = opening_hands_data.get(f'p{player_key+1}', []) # Adjust key based on GS tracking
            player_draws = draw_history_data.get(f'p{player_key+1}', {}) # Adjust key

            # --- Process Player Deck ---
            player_turn_played = {}
            for card_id in player_played:
                 for turn, cards in player_draws.items():
                     if card_id in cards:
                         player_turn_played[card_id] = int(turn) + 1; break

            for card_id in set(player_deck):
                card = self.game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'name'): continue

                perf_data = {
                    'is_win': not is_draw, # Win if not a draw
                    'is_draw': is_draw,
                    'was_played': card_id in player_played,
                    'was_drawn': any(card_id in cards for turn, cards in player_draws.items()),
                    'turn_played': player_turn_played.get(card_id, 0),
                    'in_opening_hand': card_id in player_opening,
                    'game_duration': turn_count,
                    'deck_archetype': player_archetype,
                    'opponent_archetype': opponent_archetype,
                    'synergy_partners': [cid for cid in player_played if cid != card_id]
                }
                self.card_memory.update_card_performance(card_id, perf_data)
                self.card_memory.register_card(card_id, card.name, {
                     'cmc': getattr(card, 'cmc', 0),
                     'types': getattr(card, 'card_types', []),
                     'colors': getattr(card, 'colors', []) })

            # --- Process Opponent Deck (Only need to register cards) ---
            for card_id in set(opponent_deck):
                 card = self.game_state._safe_get_card(card_id)
                 if card and hasattr(card, 'name'):
                     self.card_memory.register_card(card_id, card.name, {
                          'cmc': getattr(card, 'cmc', 0),
                          'types': getattr(card, 'card_types', []),
                          'colors': getattr(card, 'colors', []) })

            # Save card memory async
            if hasattr(self.card_memory, 'save_memory_async'):
                 self.card_memory.save_memory_async()

        except Exception as e:
            logging.error(f"Error recording cards to memory: {str(e)}")
            import traceback; logging.error(traceback.format_exc())

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
            gs.phase = gs.PHASE_UNTAP
            
            logging.info(f"Forced turn advancement to Turn {gs.turn}, Phase UNTAP")
            return gs.PHASE_UNTAP
        
        # Regular phase transition
        if current_phase in phase_transitions:
            new_phase = phase_transitions[current_phase]
            gs.phase = new_phase
            
            # Execute any special phase entry logic
            if new_phase == gs.PHASE_UNTAP:
                current_player = gs.p1 if gs.agent_is_p1 else gs.p2
                gs._untap_phase(current_player)
            
            logging.info(f"Forced phase transition: {current_phase} -> {new_phase}")
            return new_phase
        
        # Fallback - just advance to MAIN_POSTCOMBAT as a safe option
        gs.phase = gs.PHASE_MAIN_POSTCOMBAT
        logging.warning(f"Unknown phase {current_phase} in force_phase_transition, defaulting to MAIN_POSTCOMBAT")
        return gs.PHASE_MAIN_POSTCOMBAT
    

    def step(self, action_idx, context=None):
        """
        Execute the action and run the game engine until control returns to the agent,
        or the game ends. Returns the next observation, reward, done status, and info.

        Args:
            action_idx: Index of the action selected by the agent.
            context: Optional dictionary with additional context for complex actions.

        Returns:
            tuple: (observation, reward, done, truncated, info)
        """
        gs = self.game_state
        if context is None: context = {}
        # Generate initial mask needed for info dict BEFORE action application
        # Regenerate action mask if not available or invalid (e.g., start of step or after error)
        if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None or not isinstance(self.current_valid_actions, np.ndarray):
            logging.warning("current_valid_actions missing or invalid at step start. Regenerating.")
            self.current_valid_actions = self.action_mask()

        current_mask_before_action = self.current_valid_actions
        info = {"action_mask": current_mask_before_action.astype(bool) if current_mask_before_action is not None else np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)}

        try:
            # --- Initial State & Setup ---
            self.current_step += 1
            reward = 0.0 # Use intermediate variable for step reward calculation
            done = False
            truncated = False
            pre_action_pattern = None

            # --- Action Validation ---
            if not (0 <= action_idx < self.ACTION_SPACE_SIZE):
                logging.error(f"Action index {action_idx} is out of bounds (0-{self.ACTION_SPACE_SIZE-1}).")
                raise IndexError(f"Action index {action_idx} is out of bounds.")

            current_valid_actions = self.action_mask() # Refresh mask just before check
            if not current_valid_actions[action_idx]:
                logging.warning(f"Step {self.current_step}: Invalid action {action_idx} selected (Mask False). Reason: {self.action_handler.action_reasons.get(action_idx, 'Not valid')}. Available: {np.where(current_valid_actions)[0]}")
                self.invalid_action_count += 1
                self.episode_invalid_actions += 1
                step_reward = -0.1 # Standard penalty for mask failure

                if self.invalid_action_count >= self.invalid_action_limit:
                    logging.error(f"Exceeded invalid action limit ({self.invalid_action_count}). Terminating episode.")
                    done, truncated, step_reward = True, True, -2.0 # Truncated due to limit
                # else: State didn't change, return current obs (handled below)

                # Even if invalid mask, get current state and return
                obs = self._get_obs_safe()
                info["invalid_action"] = True
                info["action_mask"] = current_valid_actions.astype(bool)
                # Update action/reward history even for invalid mask selection
                # Check if attributes exist before assigning
                if hasattr(self, 'last_n_actions'):
                    self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                if hasattr(self, 'last_n_rewards'):
                    self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
                return obs, step_reward, done, truncated, info

            self.invalid_action_count = 0 # Reset counter

            # --- Get Action Info & Pre-State ---
            action_type, param = self.action_handler.get_action_info(action_idx)
            if DEBUG_ACTION_STEPS: logging.info(f"Step {self.current_step}: Player {gs.priority_player['name']} trying {action_type}({param}) Context: {context}")
            self.current_episode_actions.append(action_idx) # Record valid action attempt

            # Use _get_active_player and _get_non_active_player helpers
            me = gs._get_active_player()
            opp = gs._get_non_active_player()
            prev_state = { # Capture state BEFORE action
                "my_life": me.get("life", 0), "opp_life": opp.get("life", 0),
                "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])),
                "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
            }
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory:
                 pre_action_pattern = gs.strategy_memory.extract_strategy_pattern(gs)

            # --- Execute Agent's Action ---
            action_reward = 0.0
            action_executed = False
            if not hasattr(self.action_handler, 'action_handlers'):
                raise AttributeError("ActionHandler is missing the 'action_handlers' dictionary.")
            handler_func = self.action_handler.action_handlers.get(action_type)

            if handler_func:
                 # Try executing handler, passing full context and action index
                try:
                    result = handler_func(param=param, context=context, action_type=action_type, action_index=action_idx) # Pass action_index
                    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], bool): action_reward, action_executed = result
                    elif isinstance(result, bool): action_reward, action_executed = (0.05, result) if result else (-0.1, result)
                    elif isinstance(result, (int, float)): action_reward, action_executed = float(result), True
                    else: action_reward, action_executed = 0.0, True # Assume success if None/other type
                    action_reward = action_reward or 0.0 # Ensure float
                except Exception as handler_e:
                    logging.error(f"Error executing handler {action_type} with param {param}, context {context}, index {action_idx}: {handler_e}", exc_info=True)
                    action_reward, action_executed = -0.2, False # Assign penalty and failure
            else: # Handler not found
                 logging.warning(f"No handler implemented for action type: {action_type} (Index: {action_idx})")
                 action_reward, action_executed = -0.05, False

            reward += action_reward # Add action result reward to step reward
            info["action_reward"] = action_reward

            if not action_executed:
                logging.warning(f"Action {action_type}({param}) failed execution (returned False).")
                # COMMENT: Rollback is complex. Assume handler didn't change critical state or handled its own rollback.
                # Potential state changes before handler returns False (e.g., mana pool change) are not rolled back here.
                # If an error occurred *during* execution, state might be inconsistent.
                self.invalid_action_count += 1; self.episode_invalid_actions += 1
                step_reward = -0.15
                if self.invalid_action_count >= self.invalid_action_limit: done, truncated, step_reward = True, True, -2.0
                obs = self._get_obs_safe()
                info["action_mask"] = self.action_mask().astype(bool) # Regenerate mask
                info["execution_failed"] = True
                # Update history even on execution failure
                if hasattr(self, 'last_n_actions'):
                    self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                if hasattr(self, 'last_n_rewards'):
                    self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
                return obs, step_reward, done, truncated, info

            # --- Main Game Loop (Post-Action Processing) ---
            max_loops = 50 # Safety limit
            loop_count = 0
            previous_priority_holder_name = None # Use name for easier logging/comparison

            while loop_count < max_loops:
                loop_count += 1
                state_changed_this_loop = False # Track changes within this loop
                loop_start_phase = gs.phase
                loop_start_prio_player_name = getattr(gs.priority_player, 'name', 'None') if gs.priority_player else 'None' # Handle None priority_player
                loop_start_stack_size = len(gs.stack)

                # 1. State-Based Actions (SBAs) - Repeat until stable
                sba_cycles = 0
                while sba_cycles < 10:
                    sba_cycles += 1
                    # Ensure Layers are applied before SBAs
                    if hasattr(gs, 'layer_system') and gs.layer_system: gs.layer_system.apply_all_effects() # Use GS attribute
                    # Perform SBA Check
                    sbas_applied = gs.check_state_based_actions() if hasattr(gs, 'check_state_based_actions') else False
                    if sbas_applied:
                         state_changed_this_loop = True
                         # Re-apply layers if SBAs caused state change
                         if hasattr(gs, 'layer_system') and gs.layer_system: gs.layer_system.apply_all_effects()
                    else: break # Stable state
                if sba_cycles >= 10: logging.warning("Exceeded SBA cycle limit.")

                # 2. Check Game End from SBAs
                if self._check_game_end_conditions(info):
                     done = True; break

                # 3. Process Triggered Abilities
                # Triggers are queued by game events (card move, damage etc) via gs.trigger_ability -> ability_handler.check_abilities
                # process_triggered_abilities just moves queued triggers to the stack.
                if hasattr(gs, 'ability_handler') and gs.ability_handler: # Use GS attribute
                    stack_before_triggers = len(gs.stack)
                    gs.ability_handler.process_triggered_abilities() # Adds to gs.stack
                    if len(gs.stack) > stack_before_triggers:
                        state_changed_this_loop = True # Stack changed
                        if DEBUG_ACTION_STEPS: logging.debug(f"Loop {loop_count}: Triggers added ({len(gs.stack)-stack_before_triggers}), priority reset to AP.")
                        # Priority is reset automatically by add_to_stack

                # 4. Check Priority & Stack Resolution
                current_priority_holder = gs.priority_player
                current_priority_holder_name = getattr(current_priority_holder, 'name', 'None') if current_priority_holder else 'None'
                agent_player = gs.p1 if gs.agent_is_p1 else gs.p2

                # Check if priority has physically changed player or phase changed
                if current_priority_holder_name != loop_start_prio_player_name or gs.phase != loop_start_phase:
                    state_changed_this_loop = True

                # --- Priority Logic ---
                # The core idea: If the current priority player is NOT the agent, auto-pass.
                # If it IS the agent, break the loop and return control.
                if current_priority_holder != agent_player:
                    # Opponent or None has priority. Auto-pass for them.
                    # Need to handle case where current_priority_holder is None (start of game/phase)
                    if current_priority_holder:
                         if DEBUG_ACTION_STEPS: logging.debug(f"Loop {loop_count}: Non-agent player ({current_priority_holder_name}) holds priority. Auto-passing.")
                         gs._pass_priority() # This handles stack resolution or phase advance
                         # Check if state changed AFTER passing
                         new_prio_holder = gs.priority_player
                         if new_prio_holder != current_priority_holder or gs.phase != loop_start_phase or len(gs.stack) != loop_start_stack_size:
                              state_changed_this_loop = True
                         # Continue loop to re-evaluate state
                    else:
                        # No one has priority (e.g., after phase change before triggers handled)
                        # This case shouldn't usually be hit here, as priority is set after phase change.
                        logging.warning("Game loop state: No player has priority. Assigning to AP.")
                        gs.priority_player = gs._get_active_player()
                        state_changed_this_loop = True # Priority assigned
                else:
                    # Agent has priority. Break the loop to return control.
                    if DEBUG_ACTION_STEPS: logging.debug(f"Loop {loop_count}: Agent ({agent_player['name']}) has priority in phase {gs.phase}. Stack={len(gs.stack)}. Returning control.")
                    break

                # Check game end again after priority pass / stack resolution
                if self._check_game_end_conditions(info):
                     done = True; break

                # Stall detection: If nothing changed in the loop AND priority holder didn't change meaningfully
                if not state_changed_this_loop and current_priority_holder_name == previous_priority_holder_name:
                     # Exception: Allow repeated passes if the stack is resolving
                     if len(gs.stack) == loop_start_stack_size:
                          logging.warning(f"Game state stalled in loop {loop_count}? Prio: {current_priority_holder_name}, Phase: {gs.phase}, Stack: {len(gs.stack)}. Breaking loop.")
                          break # Avoid potential infinite loops
                     else:
                          logging.debug(f"Loop {loop_count}: State stable but stack size changed, continuing resolution.")


                previous_priority_holder_name = current_priority_holder_name # Track for next iteration

            # --- Loop End ---
            if loop_count >= max_loops: logging.error(f"Exceeded max game loop iterations ({max_loops}). Terminating."); done, truncated, reward = True, True, -3.0

            # --- Calculate Final Step Reward & Check Game End ---
            current_state = { # Gather final state
                 "my_life": me.get("life", 0), "opp_life": opp.get("life", 0), "my_hand": len(me.get("hand", [])), "opp_hand": len(opp.get("hand", [])), "my_board": len(me.get("battlefield", [])), "opp_board": len(opp.get("battlefield", [])),
                 "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                 "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp.get("battlefield", []) if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])), }
            # Ensure _add_state_change_rewards exists
            if hasattr(self, '_add_state_change_rewards') and callable(self._add_state_change_rewards):
                 state_change_reward = self._add_state_change_rewards(0.0, prev_state, current_state)
                 reward += state_change_reward # Add state change reward
            else:
                 logging.warning("_add_state_change_rewards method not found or not callable.")

            # Add board state reward component
            if hasattr(self, '_calculate_board_state_reward') and callable(self._calculate_board_state_reward):
                reward += self._calculate_board_state_reward()
            else:
                 logging.warning("_calculate_board_state_reward method not found or not callable.")


            # Final Game End Checks (already done inside loop, check one last time)
            if not done and self._check_game_end_conditions(info): done = True
            if done and not truncated: # If game ended naturally
                 if info["game_result"] == "win": reward += 10.0 + max(0, gs.max_turns - gs.turn) * 0.1
                 elif info["game_result"] == "loss": reward -= 10.0
                 # No extra reward/penalty for draw

            # Check max steps AFTER natural end checks
            if not done and hasattr(self, 'current_step') and hasattr(self, 'max_episode_steps') and self.current_step >= self.max_episode_steps:
                 done, truncated = True, True; reward -= 0.5; info["game_result"] = "truncated"; logging.info("Max episode steps reached.")

            # --- Final Logging & Return ---
            self.episode_rewards.append(reward) # Store final reward for this step
            if done and not getattr(self, '_game_result_recorded', False):
                 self.ensure_game_result_recorded() # Record result if game ended

            obs = self._get_obs() # Get final observation
            # Action mask is regenerated based on the NEW state within _get_obs and returned in info
            # self.current_valid_actions = self.action_mask() # This generates the mask
            info["action_mask"] = obs["action_mask"] # Get the mask from the generated observation

            # Update action/reward history
            if hasattr(self, 'last_n_actions'):
                 self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
            if hasattr(self, 'last_n_rewards'):
                 self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = reward

            # Update strategy memory
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory and pre_action_pattern is not None:
                 gs.strategy_memory.update_strategy(pre_action_pattern, reward)

            if DEBUG_ACTION_STEPS: logging.info(f"== STEP {self.current_step} END: reward={reward:.3f}, done={done}, truncated={truncated}, Phase={gs.phase}, Prio={getattr(gs.priority_player,'name','None')} ==")

            return obs, reward, done, truncated, info

        except Exception as e:
            # --- Critical Error Handling ---
            logging.error(f"CRITICAL error in step function (Action {action_idx}): {str(e)}", exc_info=True)
            obs = self._get_obs_safe() # Use safe observation
            mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); mask[11] = True; mask[12] = True # Pass/Concede
            info = {"action_mask": mask, "critical_error": True, "error_message": str(e)}
            # Ensure game ends if critical error occurs
            return obs, -5.0, True, False, info
        
    def _get_obs_safe(self):
        """Return a minimal, safe observation dictionary in case of errors."""
        gs = self.game_state
        # Initialize with zeros based on the defined space
        obs = {k: np.zeros(space.shape, dtype=space.dtype)
               for k, space in self.observation_space.spaces.items()}
        try:
            # Fill minimal necessary fields, checking attribute existence
            obs["phase"] = np.array([getattr(gs, 'phase', 0)], dtype=np.int32)
            obs["turn"] = np.array([getattr(gs, 'turn', 1)], dtype=np.int32)
            # Ensure p1 and p2 exist before accessing life
            p1_life = getattr(gs, 'p1', {}).get('life', 0)
            p2_life = getattr(gs, 'p2', {}).get('life', 0)
            agent_is_p1 = getattr(gs, 'agent_is_p1', True)
            obs["p1_life"] = np.array([p1_life], dtype=np.int32)
            obs["p2_life"] = np.array([p2_life], dtype=np.int32)
            obs["my_life"] = np.array([p1_life if agent_is_p1 else p2_life], dtype=np.int32)
            obs["opp_life"] = np.array([p2_life if agent_is_p1 else p1_life], dtype=np.int32)
            # Safely generate action mask
            try:
                obs["action_mask"] = self.action_mask().astype(bool)
            except Exception:
                obs["action_mask"] = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                obs["action_mask"][11] = True # Pass priority
                obs["action_mask"][12] = True # Concede
            # Fill phase onehot safely
            max_phase = self.observation_space["phase_onehot"].shape[0] - 1
            current_phase = getattr(gs, 'phase', 0)
            if 0 <= current_phase <= max_phase:
                 obs["phase_onehot"][current_phase] = 1.0
            else:
                 obs["phase_onehot"][0] = 1.0 # Default to phase 0

        except Exception as safe_obs_e:
            logging.error(f"Error in _get_obs_safe itself: {safe_obs_e}")
            # Return the zero-initialized obs as a last resort
        return obs

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

    def _calculate_board_state_reward(self):
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
                my_power += card.power
                my_toughness += card.toughness
                
        opp_power = 0
        opp_toughness = 0
        for cid in opp_creatures:
            card = gs._safe_get_card(cid)
            if card and hasattr(card, 'power') and hasattr(card, 'toughness'):
                opp_power += card.power
                opp_toughness += card.toughness

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
            high_power_count = sum(1 for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power') and gs._safe_get_card(cid).power >= 4)
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
        my_power = sum(gs._safe_get_card(cid).power for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        my_toughness = sum(gs._safe_get_card(cid).toughness for cid in my_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        opp_power = sum(gs._safe_get_card(cid).power for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
        opp_toughness = sum(gs._safe_get_card(cid).toughness for cid in opp_creatures if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
        
        total_stats = max(1, my_power + my_toughness + opp_power + opp_toughness)
        board_advantage = (my_power + my_toughness - opp_power - opp_toughness) / total_stats
        
        # Life advantage
        life_advantage = (me["life"] - opp["life"]) / 40.0  # Normalize by total possible life
        
        # Quality of cards in hand (average mana value as a simple proxy)
        # Add more type checking to avoid attribute errors
        valid_hand_cards = [gs._safe_get_card(cid) for cid in me["hand"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc')]
        my_hand_quality = np.mean([card.cmc for card in valid_hand_cards]) if valid_hand_cards else 0
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
    
    def _calculate_card_likelihood(self, card, color_count, visible_creatures, visible_instants, visible_artifacts):
        """Helper to calculate how likely a card is to be in opponent's hand"""
        gs = self.game_state
        weight = 1.0
        
        # Card must have required attributes
        if not card or not hasattr(card, 'colors') or not hasattr(card, 'card_types'):
            return 0.0
        
        # Color matching
        card_colors = np.array(card.colors)
        color_match = np.sum(card_colors * color_count) / (np.sum(color_count) + 1e-6)
        weight *= (1.0 + color_match)
        
        # Card type matching
        if 'creature' in card.card_types and visible_creatures > 0:
            weight *= 1.5
        if 'instant' in card.card_types and visible_instants > 0:
            weight *= 1.2
        if 'artifact' in card.card_types and visible_artifacts > 0:
            weight *= 1.3
            
        # Mana curve considerations - higher probability of having castable cards
        if hasattr(card, 'cmc'):
            if card.cmc <= gs.turn:
                weight *= 2.0
            elif card.cmc <= gs.turn + 2:
                weight *= 1.0
            else:
                weight *= 0.5
        
        return weight
    
    def _estimate_opponent_hand(self):
        """Create a probabilistic model of opponent's hand based on known information"""
        gs = self.game_state
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Get known cards in opponent's deck
        known_deck_cards = set()
        for zone in ["battlefield", "graveyard", "exile"]:
            for card_id in opp[zone]:
                known_deck_cards.add(card_id)
        
        # Count cards by type in opponent's visible cards to infer deck strategy
        visible_creatures = 0
        visible_instants = 0
        visible_artifacts = 0
        color_count = np.zeros(5)  # WUBRG
        
        for card_id in known_deck_cards:
            card = gs._safe_get_card(card_id)
            if not card:
                continue
                
            if hasattr(card, 'card_types'):
                if 'creature' in card.card_types:
                    visible_creatures += 1
                if 'instant' in card.card_types:
                    visible_instants += 1
                if 'artifact' in card.card_types:
                    visible_artifacts += 1
                    
            if hasattr(card, 'colors'):
                for i, color in enumerate(card.colors):
                    color_count[i] += color
        
        # Create estimated hand based on deck profile
        estimated_hand = np.zeros((self.max_hand_size, 223), dtype=np.float32)
        
        # Create pool of likely cards based on observed deck profile
        likely_cards = []
        
        # Modified part: iterate over card_db differently depending on its type
        # If card_db is a list
        if isinstance(gs.card_db, dict):
            for card_id, card in gs.card_db.items():
                # Skip known cards
                if card_id in known_deck_cards:
                    continue
                
                # Card weighting logic
                weight = self._calculate_card_likelihood(card, color_count, visible_creatures, visible_instants, visible_artifacts)
                likely_cards.append((card_id, weight))
        else:
            logging.warning("Unexpected card_db format")
        
        # Sort by weight and select top cards
        likely_cards.sort(key=lambda x: x[1], reverse=True)
        
        # Fill estimated hand with top weighted cards
        hand_size = min(len(opp["hand"]), self.max_hand_size)
        for i in range(hand_size):
            if i < len(likely_cards):
                estimated_hand[i] = self._get_card_feature(likely_cards[i][0], 223)
        
        return estimated_hand
    
    def _log_episode_summary(self):
        logging.info(f"Episode ended with total reward: {sum(self.episode_rewards)} and {self.episode_invalid_actions} invalid actions.")
    
    def _get_card_feature(self, card_id, feature_dim):
        """
        Helper to safely retrieve a card's feature vector with proper dimensionality.
        If the card ID is invalid, returns a zero vector.
        """
        try:
            # Use _safe_get_card instead of direct access
            card = self.game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'to_feature_vector'):
                return np.zeros(feature_dim, dtype=np.float32)
                
            # Get the feature vector
            feature_vector = card.to_feature_vector()
            
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
        
    def record_game_result(self, winner_is_p1: bool, turn_count: int, winner_life: int, is_draw: bool = False):
        """
        Centralized helper to consistently record game statistics with improved game stage handling.
        """
        logging.debug(f"record_game_result called: winner_is_p1={winner_is_p1}, turn_count={turn_count}, winner_life={winner_life}, is_draw={is_draw}")
        
        # Mark that the game result has been recorded to avoid duplicate recording
        self._game_result_recorded = True
        
        if not self.has_stats_tracker:
            return
            
        try:
            gs = self.game_state
            
            # For draws, we don't have a winner, but we need to record both decks
            if is_draw:
                deck1 = getattr(self, 'original_p1_deck', gs.p1.get("library", []).copy())
                deck2 = getattr(self, 'original_p2_deck', gs.p2.get("library", []).copy())
                deck1_name = getattr(self, 'current_deck_name_p1', "Unknown Deck 1")
                deck2_name = getattr(self, 'current_deck_name_p2', "Unknown Deck 2")
                
                # Use player 0 and 1 for consistent keys regardless of who is agent
                game_cards_played = getattr(gs, 'cards_played', {0: [], 1: []})
                
                # Determine game stage based on turn count
                game_stage = "early"
                if turn_count >= 8:
                    game_stage = "late"
                elif turn_count >= 4:
                    game_stage = "mid"
                
                # Pass the draw flag explicitly
                self.stats_tracker.record_game(
                    winner_deck=deck1,  # In a draw, we pass both decks
                    loser_deck=deck2,   # but neither is really the winner/loser
                    card_db=self.card_db,
                    turn_count=turn_count,
                    winner_life=winner_life,
                    winner_deck_name=deck1_name,
                    loser_deck_name=deck2_name,
                    cards_played=game_cards_played,
                    game_stage=game_stage,
                    is_draw=True
                )
                
                # Force an immediate save
                if hasattr(self.stats_tracker, 'save_updates_sync'):
                    self.stats_tracker.save_updates_sync()
                
                logging.info(f"Game recorded: Draw between {deck1_name} and {deck2_name} in {turn_count} turns with equal life totals ({winner_life})")
            else:
                # Original non-draw logic
                winner_deck = getattr(self, 'original_p1_deck', gs.p1.get("library", []).copy()) if winner_is_p1 else getattr(self, 'original_p2_deck', gs.p2.get("library", []).copy())
                loser_deck = getattr(self, 'original_p2_deck', gs.p2.get("library", []).copy()) if winner_is_p1 else getattr(self, 'original_p1_deck', gs.p1.get("library", []).copy())
                winner_name = getattr(self, 'current_deck_name_p1', "Unknown Deck 1") if winner_is_p1 else getattr(self, 'current_deck_name_p2', "Unknown Deck 2")
                loser_name = getattr(self, 'current_deck_name_p2', "Unknown Deck 2") if winner_is_p1 else getattr(self, 'current_deck_name_p1', "Unknown Deck 1")
                
                # Get cards played from game state if available
                game_cards_played = getattr(gs, 'cards_played', {0: [], 1: []})
                
                # Determine game stage based on turn count
                game_stage = "early"
                if turn_count >= 8:
                    game_stage = "late"
                elif turn_count >= 4:
                    game_stage = "mid"
                
                # Pass game stage and all available information
                self.stats_tracker.record_game(
                    winner_deck=winner_deck,
                    loser_deck=loser_deck,
                    card_db=self.card_db,
                    turn_count=turn_count,
                    winner_life=winner_life,
                    winner_deck_name=winner_name,
                    loser_deck_name=loser_name,
                    cards_played=game_cards_played,
                    game_stage=game_stage,
                    is_draw=False
                )
                
                # Force an immediate save
                if hasattr(self.stats_tracker, 'save_updates_sync'):
                    self.stats_tracker.save_updates_sync()
                
                logging.info(f"Game recorded: {winner_name} defeated {loser_name} in {turn_count} turns with {winner_life} life remaining")
        except Exception as e:
            logging.error(f"Error recording game statistics: {e}")
            import traceback
            logging.error(traceback.format_exc())
            

    def _get_obs(self):
        """Build the observation dictionary. Assumes helpers are implemented."""
        try:
            if hasattr(self, 'layer_system') and self.layer_system:
                self.layer_system.apply_all_effects()

            gs = self.game_state
            # Use GS helpers to get players
            me = gs._get_active_player() if gs.priority_player == gs._get_active_player() else gs.priority_player if gs.priority_player else gs._get_active_player() # Player whose turn it is, or who has priority
            opp = gs._get_non_active_player() # Get the other player relative to active
            agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2 # The actual agent player object

            is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
            current_phase = getattr(gs, 'phase', 0)
            current_turn = getattr(gs, 'turn', 1)
            keyword_dimension = len(Card.ALL_KEYWORDS) # Ensure this matches

            # Regenerate action mask based on the current state
            current_mask = self.action_mask()

            obs = {
                "phase": np.array([current_phase], dtype=np.int32),
                "phase_onehot": self._phase_to_onehot(current_phase),
                "turn": np.array([current_turn], dtype=np.int32),
                "is_my_turn": np.array([int(is_my_turn)], dtype=np.int32),
                # Use agent player object for 'my_' stats
                "my_life": np.array([agent_player_obj.get('life', 0)], dtype=np.int32),
                "opp_life": np.array([opp.get('life', 0)], dtype=np.int32),
                "life_difference": np.array([agent_player_obj.get('life', 0) - opp.get('life', 0)], dtype=np.int32),
                "p1_life": np.array([getattr(gs.p1, 'life', 0)], dtype=np.int32), # Keep absolute refs if needed
                "p2_life": np.array([getattr(gs.p2, 'life', 0)], dtype=np.int32),

                # Hand state (from agent's perspective)
                "my_hand": self._get_zone_features(agent_player_obj.get("hand", []), self.max_hand_size),
                "my_hand_count": np.array([len(agent_player_obj.get("hand", []))], dtype=np.int32),
                "opp_hand_count": np.array([len(opp.get("hand", []))], dtype=np.int32),
                "hand_card_types": self._get_hand_card_types(agent_player_obj.get("hand", [])),
                "hand_playable": self._get_hand_playable(agent_player_obj.get("hand", []), agent_player_obj, is_my_turn),
                "hand_performance": self._get_hand_performance(agent_player_obj.get("hand", [])),
                "hand_synergy_scores": self._get_hand_synergy_scores(agent_player_obj.get("hand", []), agent_player_obj.get("battlefield", [])),
                "opportunity_assessment": self._get_opportunity_assessment(agent_player_obj.get("hand", []), agent_player_obj),

                # Battlefield state (from agent's perspective)
                "my_battlefield": self._get_zone_features(agent_player_obj.get("battlefield", []), self.max_battlefield),
                "my_battlefield_flags": self._get_battlefield_flags(agent_player_obj.get("battlefield", []), agent_player_obj, self.max_battlefield),
                "my_battlefield_keywords": self._get_battlefield_keywords(agent_player_obj.get("battlefield", []), keyword_dimension),
                "my_tapped_permanents": self._get_tapped_mask(agent_player_obj.get("battlefield", []), agent_player_obj.get("tapped_permanents", set()), self.max_battlefield),
                "opp_battlefield": self._get_zone_features(opp.get("battlefield", []), self.max_battlefield),
                "opp_battlefield_flags": self._get_battlefield_flags(opp.get("battlefield", []), opp, self.max_battlefield),
                # Absolute player battlefield state
                "p1_battlefield": self._get_zone_features(gs.p1.get("battlefield", []), self.max_battlefield),
                "p2_battlefield": self._get_zone_features(gs.p2.get("battlefield", []), self.max_battlefield),
                "p1_bf_count": np.array([len(gs.p1.get("battlefield", []))], dtype=np.int32),
                "p2_bf_count": np.array([len(gs.p2.get("battlefield", []))], dtype=np.int32),

                # Creature stats (using helper)
                "my_creature_count": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['count']], dtype=np.int32),
                "opp_creature_count": np.array([self._get_creature_stats(opp.get("battlefield", []))['count']], dtype=np.int32),
                "my_total_power": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['power']], dtype=np.int32),
                "my_total_toughness": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['toughness']], dtype=np.int32),
                "opp_total_power": np.array([self._get_creature_stats(opp.get("battlefield", []))['power']], dtype=np.int32),
                "opp_total_toughness": np.array([self._get_creature_stats(opp.get("battlefield", []))['toughness']], dtype=np.int32),
                "power_advantage": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['power'] - self._get_creature_stats(opp.get("battlefield", []))['power']], dtype=np.int32),
                "toughness_advantage": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['toughness'] - self._get_creature_stats(opp.get("battlefield", []))['toughness']], dtype=np.int32),
                "creature_advantage": np.array([self._get_creature_stats(agent_player_obj.get("battlefield", []))['count'] - self._get_creature_stats(opp.get("battlefield", []))['count']], dtype=np.int32),
                "threat_assessment": self._get_threat_assessment(opp.get("battlefield", [])), # Opponent threats
                "card_synergy_scores": self._calculate_card_synergies(agent_player_obj.get("battlefield", [])), # Agent's synergies

                # Mana state (agent's perspective)
                "my_mana_pool": np.array([agent_player_obj.get("mana_pool", {}).get(c, 0) for c in ['W', 'U', 'B', 'R', 'G', 'C']], dtype=np.int32),
                "my_mana": np.array([sum(agent_player_obj.get("mana_pool", {}).values())], dtype=np.int32), # Total mana in pool
                "untapped_land_count": np.array([sum(1 for cid in agent_player_obj.get("battlefield", []) if self._is_land(cid) and cid not in agent_player_obj.get("tapped_permanents", set()))], dtype=np.int32),
                "total_available_mana": np.array([sum(agent_player_obj.get("mana_pool", {}).values()) + sum(1 for cid in agent_player_obj.get("battlefield", []) if self._is_land(cid) and cid not in agent_player_obj.get("tapped_permanents", set()))], dtype=np.int32), # Pool + untapped basic lands approximation
                "turn_vs_mana": np.array([min(1.0, len([cid for cid in agent_player_obj.get("battlefield",[]) if self._is_land(cid)]) / max(1.0, float(current_turn)))], dtype=np.float32),
                "remaining_mana_sources": np.array([sum(1 for cid in agent_player_obj.get("battlefield", []) if self._is_land(cid) and cid not in agent_player_obj.get("tapped_permanents", set()))], dtype=np.int32),

                # Graveyard/Exile state
                "my_graveyard_count": np.array([len(agent_player_obj.get("graveyard", []))], dtype=np.int32),
                "opp_graveyard_count": np.array([len(opp.get("graveyard", []))], dtype=np.int32),
                "my_dead_creatures": np.array([sum(1 for cid in agent_player_obj.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32),
                "opp_dead_creatures": np.array([sum(1 for cid in opp.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32),
                "graveyard_key_cards": self._get_zone_features(agent_player_obj.get("graveyard", []), 10),
                "exile_key_cards": self._get_zone_features(agent_player_obj.get("exile", []), 10),

                # Stack state
                "stack_count": np.array([len(gs.stack)], dtype=np.int32),
                "stack_controller": self._get_stack_info(gs.stack, agent_player_obj)[0],
                "stack_card_types": self._get_stack_info(gs.stack, agent_player_obj)[1],

                # Combat state
                "attackers_count": np.array([len(gs.current_attackers)], dtype=np.int32),
                "blockers_count": np.array([sum(len(b) for b in gs.current_block_assignments.values())], dtype=np.int32),
                "potential_combat_damage": np.zeros(1, dtype=np.int32), # Populate if simulation ran

                # Ability states (agent's perspective)
                "ability_features": self._get_ability_features(agent_player_obj.get("battlefield", []), agent_player_obj),
                "ability_timing": self._get_ability_timing(current_phase),
                "planeswalker_activations": self._get_planeswalker_activation_flags(agent_player_obj.get("battlefield", []), agent_player_obj),
                "planeswalker_activation_counts": self._get_planeswalker_activation_counts(agent_player_obj.get("battlefield", []), agent_player_obj),

                # History
                "phase_history": np.array(self._phase_history[-5:] + [-1]*(5-len(self._phase_history)), dtype=np.int32), # Pad with -1
                "previous_actions": np.array(self.last_n_actions if hasattr(self, 'last_n_actions') else [-1]*self.action_memory_size, dtype=np.int32),
                "previous_rewards": np.array(self.last_n_rewards if hasattr(self, 'last_n_rewards') else [0.0]*self.action_memory_size, dtype=np.float32),

                # Strategy / Planning Features
                "strategic_metrics": np.zeros(10, dtype=np.float32), # Populated below
                "position_advantage": np.array([self._calculate_position_advantage()], dtype=np.float32),
                "estimated_opponent_hand": self._estimate_opponent_hand(),
                "deck_composition_estimate": self._get_deck_composition(agent_player_obj),
                "opponent_archetype": np.zeros(6, dtype=np.float32), # Populated below
                "future_state_projections": np.zeros(7, dtype=np.float32), # Populated below
                "multi_turn_plan": np.zeros(6, dtype=np.float32), # Populated below
                "win_condition_viability": np.zeros(6, dtype=np.float32), # Populated below
                "win_condition_timings": np.zeros(6, dtype=np.float32), # Populated below
                "resource_efficiency": self._get_resource_efficiency(agent_player_obj, current_turn),

                # Action Mask & Recommendations
                "action_mask": current_mask.astype(bool), # Use generated mask
                "recommended_action": np.array([-1], dtype=np.int32), # Populated below
                "recommended_action_confidence": np.zeros(1, dtype=np.float32), # Populated below
                "memory_suggested_action": np.array([-1], dtype=np.int32), # Populated below
                "suggestion_matches_recommendation": np.zeros(1, dtype=np.int32), # Populated below
                "optimal_attackers": np.zeros(self.max_battlefield, dtype=np.float32), # Populated below
                "attacker_values": np.zeros(self.max_battlefield, dtype=np.float32), # Populated below
                "ability_recommendations": np.zeros((self.max_battlefield, 5, 2), dtype=np.float32), # Populated below

                # Mulligan state (agent's perspective)
                "mulligan_in_progress": np.array([int(getattr(gs, 'mulligan_in_progress', False))], dtype=np.int32),
                "mulligan_recommendation": np.array([0.5], dtype=np.float32), # Populated below
                "mulligan_reason_count": np.zeros(1, dtype=np.int32), # Populated below
                "mulligan_reasons": np.zeros(5, dtype=np.float32), # Populated below
            }

            # --- Populate dynamic/planner-dependent parts ---
            if self.strategic_planner:
                try:
                    # Use cached analysis if valid, else re-analyze
                    analysis = getattr(self, 'current_analysis', None)
                    if not analysis or analysis.get("game_info", {}).get("turn") != current_turn:
                         analysis = self.strategic_planner.analyze_game_state()
                         self.current_analysis = analysis

                    if analysis:
                        obs["strategic_metrics"] = self._analysis_to_metrics(analysis)
                        obs["position_advantage"] = np.array([analysis.get("position", {}).get("score", 0)], dtype=np.float32)

                    opp_arch = self.strategic_planner.predict_opponent_archetype()
                    obs["opponent_archetype"][:len(opp_arch)] = opp_arch # Fill first 6

                    future_proj = self.strategic_planner.project_future_states(num_turns=7) # Request 7 turns
                    obs["future_state_projections"][:len(future_proj)] = future_proj # Fill first 7

                    win_cons = self.strategic_planner.identify_win_conditions()
                    wc_keys = ["combat_damage", "direct_damage", "card_advantage", "combo", "control", "alternate"]
                    obs["win_condition_viability"] = np.array([win_cons.get(k, {}).get("score", 0.0) for k in wc_keys], dtype=np.float32)
                    obs["win_condition_timings"] = np.array([min(self.max_turns + 1, win_cons.get(k, {}).get("turns_to_win", 99)) for k in wc_keys], dtype=np.float32)

                    obs["multi_turn_plan"] = self._get_multi_turn_plan_metrics() # Assuming this returns shape (6,)
                    obs["threat_assessment"] = self._get_threat_assessment(opp.get("battlefield", [])) # Pass opponent's battlefield

                    # Populate Recommendations
                    valid_actions_list = np.where(current_mask)[0].tolist() # Use current mask
                    rec_action, rec_conf, mem_action, matches = self._get_recommendations(valid_actions_list)
                    obs["recommended_action"] = np.array([rec_action], dtype=np.int32)
                    obs["recommended_action_confidence"] = np.array([rec_conf], dtype=np.float32)
                    obs["memory_suggested_action"] = np.array([mem_action], dtype=np.int32)
                    obs["suggestion_matches_recommendation"] = np.array([matches], dtype=np.int32)

                    # Populate Optimal Attackers / PW Activations
                    if hasattr(self.strategic_planner,'find_optimal_attack'):
                        optimal_ids = self.strategic_planner.find_optimal_attack()
                        bf_ids = agent_player_obj.get("battlefield", [])
                        optimal_mask = np.zeros(self.max_battlefield, dtype=np.float32)
                        for i, cid in enumerate(bf_ids[:self.max_battlefield]):
                            if cid in optimal_ids: optimal_mask[i] = 1.0
                        obs["optimal_attackers"] = optimal_mask
                        obs["attacker_values"] = self._get_attacker_values(bf_ids, agent_player_obj)
                    obs["ability_recommendations"] = self._get_ability_recommendations(agent_player_obj.get("battlefield", []), agent_player_obj)
                    # Note: PW activation flags are already populated based on player state

                except Exception as planner_e:
                     logging.warning(f"Error getting strategic info for observation: {planner_e}", exc_info=True) # Log traceback


            # Populate Mulligan info if applicable for the agent
            if getattr(gs, 'mulligan_in_progress', False) and getattr(gs, 'mulligan_player', None) == agent_player_obj:
                 mull_rec, mull_reasons, mull_count = self._get_mulligan_info(agent_player_obj)
                 obs["mulligan_recommendation"] = np.array([mull_rec], dtype=np.float32)
                 obs["mulligan_reason_count"] = np.array([mull_count], dtype=np.int32)
                 obs["mulligan_reasons"] = mull_reasons


            # Validate before returning
            self._validate_obs(obs)
            return obs

        except Exception as e:
            logging.critical(f"CRITICAL error generating observation: {str(e)}", exc_info=True)
            return self._get_obs_safe() # Return safe observation on error

    def _get_tapped_mask(self, battlefield_ids, tapped_set, max_size):
        """Helper to get a boolean mask for tapped permanents."""
        mask = np.zeros(max_size, dtype=bool)
        for i, card_id in enumerate(battlefield_ids):
            if i >= max_size: break
            mask[i] = card_id in tapped_set
        return mask

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
        """Checks if the generated observation dictionary conforms to the defined observation space."""
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
                logging.error(f"Observation Validation Error: Key '{key}' is not a numpy array (type: {type(value)})")
                valid = False
                continue

            # Shape check
            expected_shape = space.shape
            actual_shape = value.shape
            if actual_shape != expected_shape:
                # Allow broadcasting for single-element dimensions if types match
                is_broadcastable = True
                if len(expected_shape) != len(actual_shape):
                     is_broadcastable = False
                else:
                     for exp_d, act_d in zip(expected_shape, actual_shape):
                          if exp_d != act_d and exp_d != 1 and act_d != 1:
                               is_broadcastable = False; break
                if not is_broadcastable:
                    logging.error(f"Observation Validation Error: Shape mismatch for '{key}'. Expected {expected_shape}, got {actual_shape}")
                    valid = False

            # Dtype check
            if not np.can_cast(value.dtype, space.dtype, casting='safe'):
                # Allow float32 -> float64, int32 -> int64, etc.
                if not (np.issubdtype(value.dtype, np.number) and np.issubdtype(space.dtype, np.number)) and \
                   not (np.issubdtype(value.dtype, np.bool_) and np.issubdtype(space.dtype, np.bool_)):
                     logging.error(f"Observation Validation Error: Dtype mismatch for '{key}'. Expected {space.dtype}, got {value.dtype}")
                     valid = False

            # Bounds check (only for Box spaces)
            if isinstance(space, spaces.Box):
                # Need careful check due to potential broadcasting/NaN
                try:
                    # Check min bound
                    lower_bound = np.full(value.shape, space.low.item() if space.low.size == 1 else space.low, dtype=space.dtype)
                    if not np.all(np.greater_equal(value, lower_bound)):
                        min_val_idx = np.unravel_index(np.argmin(value), value.shape)
                        min_val = value[min_val_idx]
                        logging.error(f"Observation Validation Error: Value out of lower bound for '{key}'. Expected >= {lower_bound[min_val_idx]}, got {min_val} at index {min_val_idx}")
                        valid = False

                    # Check max bound
                    upper_bound = np.full(value.shape, space.high.item() if space.high.size == 1 else space.high, dtype=space.dtype)
                    if not np.all(np.less_equal(value, upper_bound)):
                        max_val_idx = np.unravel_index(np.argmax(value), value.shape)
                        max_val = value[max_val_idx]
                        logging.error(f"Observation Validation Error: Value out of upper bound for '{key}'. Expected <= {upper_bound[max_val_idx]}, got {max_val} at index {max_val_idx}")
                        valid = False
                except Exception as bound_e:
                    logging.error(f"Observation Validation Error: Error checking bounds for '{key}': {bound_e}")
                    valid = False

        return valid

    def _get_hand_synergy_scores(self, hand_ids, bf_ids):
        """Calculate synergy for each card in hand with current board/hand state."""
        scores = np.zeros(self.max_hand_size, dtype=np.float32)
        if not self.strategic_planner or not hasattr(self.strategic_planner, 'identify_card_synergies'):
            return scores

        current_hand_and_board = hand_ids + bf_ids
        for i, card_id in enumerate(hand_ids):
            if i >= self.max_hand_size: break
            # Compare card i with all *other* cards currently available
            other_cards = [cid for cid in current_hand_and_board if cid != card_id]
            synergy_score, _ = self.strategic_planner.identify_card_synergies(card_id, other_cards, []) # Compare with hand+board
            scores[i] = np.clip(synergy_score / 5.0, 0.0, 1.0) # Normalize synergy score

        return scores

    def _get_ability_recommendations(self, bf_ids, player):
        """Populate the ability recommendations tensor."""
        recs = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32) # shape (bf_size, max_abilities, [recommend, conf])
        gs = self.game_state
        if not hasattr(gs, 'ability_handler') or not self.strategic_planner: return recs

        for i, card_id in enumerate(bf_ids):
             if i >= self.max_battlefield: break
             abilities = gs.ability_handler.get_activated_abilities(card_id)
             for j, ability in enumerate(abilities):
                  if j >= 5: break # Max 5 abilities per card
                  try:
                      # Check if actually activatable first
                      can_activate = gs.ability_handler.can_activate_ability(card_id, j, player)
                      if can_activate:
                           recommended, confidence = self.strategic_planner.recommend_ability_activation(card_id, j)
                           recs[i, j, 0] = float(recommended)
                           recs[i, j, 1] = confidence
                      # else leave as 0.0, 0.0
                  except Exception as e:
                      logging.warning(f"Error getting ability rec for {card_id} ability {j}: {e}")
                      # Leave as zeros
        return recs

        
    # --- Observation Helper Methods ---

    def _get_zone_features(self, card_ids, max_size):
        """Helper to get feature vectors for cards in a zone, padded/truncated."""
        features = np.zeros((max_size, self._feature_dim), dtype=np.float32)
        for i, card_id in enumerate(card_ids):
            if i >= max_size: break
            features[i] = self._get_card_feature(card_id, self._feature_dim)
        return features

    def _get_battlefield_flags(self, card_ids, player, max_size):
        """Helper to get flags (tapped, sick, atk, block, keywords) for battlefield."""
        flags = np.zeros((max_size, 5), dtype=np.float32)
        tapped_set = player.get("tapped_permanents", set())
        sick_set = player.get("entered_battlefield_this_turn", set())
        attackers_set = set(getattr(self.game_state, 'current_attackers', []))
        blocking_set = set()
        for blockers in getattr(self.game_state, 'current_block_assignments', {}).values():
            blocking_set.update(blockers)

        for i, card_id in enumerate(card_ids):
            if i >= max_size: break
            card = self._safe_get_card(card_id)
            flags[i, 0] = float(card_id in tapped_set)
            # Check summoning sickness and haste using _has_haste helper
            has_haste = self._has_haste(card_id) if card else False
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
        for card_id in creature_ids:
             card = self._safe_get_card(card_id)
             # Check if it's actually a creature (type might change post-layers)
             # LayerSystem should have been applied before calling _get_obs
             if card and 'creature' in getattr(card, 'card_types', []):
                 count += 1
                 power += getattr(card, 'power', 0) or 0 # Ensure non-None value
                 toughness += getattr(card, 'toughness', 0) or 0 # Ensure non-None value
        return {"count": count, "power": power, "toughness": toughness} # Return dict

    def _get_hand_card_types(self, hand_ids):
        """Helper to get one-hot encoding of card types in hand."""
        types = np.zeros((self.max_hand_size, 5), dtype=np.float32)
        for i, card_id in enumerate(hand_ids):
            if i >= self.max_hand_size: break
            card = self._safe_get_card(card_id)
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
        playable = np.zeros(self.max_hand_size, dtype=np.float32)
        gs = self.game_state
        can_play_sorcery = is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack

        for i, card_id in enumerate(hand_ids):
            if i >= self.max_hand_size: break
            card = gs._safe_get_card(card_id)
            if card:
                is_land = 'land' in getattr(card, 'type_line', '').lower()
                is_instant_speed = 'instant' in getattr(card, 'card_types', []) or self._has_flash(card_id)

                can_afford = self._can_afford_card(player, card)

                if is_land:
                    if not player.get("land_played", False) and can_play_sorcery:
                        playable[i] = 1.0
                elif can_afford:
                    if is_instant_speed:
                         playable[i] = 1.0 # Can always cast if affordable and has priority
                    elif can_play_sorcery:
                         playable[i] = 1.0
        return playable

    def _get_stack_info(self, stack, me_player):
        """Helper to get controller and type info for top stack items."""
        controllers = np.full(5, -1, dtype=np.int32) # -1=Empty, 0=Me, 1=Opp
        types = np.zeros((5, 5), dtype=np.float32) # Creature, Inst, Sorc, Ability, Other
        for i, item in enumerate(reversed(stack[:5])): # Top 5 items
            if isinstance(item, tuple) and len(item) >= 3:
                item_type, card_id, controller = item[:3]
                card = self._safe_get_card(card_id)
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
        card = self._safe_get_card(card_id)
        return card and 'land' in getattr(card, 'type_line', '').lower()

    def _is_creature(self, card_id):
        """Check if card is a creature."""
        card = self._safe_get_card(card_id)
        return card and 'creature' in getattr(card, 'card_types', [])

    def _analysis_to_metrics(self, analysis):
        """Convert strategic analysis dict to metrics vector."""
        metrics = np.zeros(10, dtype=np.float32)
        if not analysis: return metrics
        metrics[0] = analysis.get("position", {}).get("score", 0)
        metrics[1] = analysis.get("board_state", {}).get("board_advantage", 0)
        metrics[2] = analysis.get("resources", {}).get("card_advantage", 0) / 5.0 # Normalize
        metrics[3] = analysis.get("resources", {}).get("mana_advantage", 0) / 3.0 # Normalize
        metrics[4] = analysis.get("life", {}).get("life_diff", 0) / 20.0 # Normalize
        metrics[5] = analysis.get("tempo", {}).get("tempo_advantage", 0)
        stage = analysis.get("game_info", {}).get("game_stage", 'mid')
        metrics[6] = 0.0 if stage == 'early' else 0.5 if stage == 'mid' else 1.0
        # Metrics 7, 8, 9 can be used for other aspects (e.g., threat level, combo proximity)
        # Placeholder: Add opponent archetype confidence if available
        # Needs archetype prediction logic to be added first. For now, keep as 0.
        metrics[7] = 0.0 # Opponent Archetype Confidence
        metrics[8] = 0.0 # Win Condition Proximity
        metrics[9] = 0.0 # Overall Threat Level
        return np.clip(metrics, -1.0, 1.0)

    def _get_hand_performance(self, hand_ids):
        """Get performance ratings for cards in hand."""
        perf = np.full(self.max_hand_size, 0.5, dtype=np.float32) # Default 0.5
        for i, card_id in enumerate(hand_ids):
            if i >= self.max_hand_size: break
            card = self._safe_get_card(card_id)
            if card and hasattr(card, 'performance_rating'):
                 perf[i] = card.performance_rating
        return perf

    def _get_battlefield_keywords(self, card_ids, keyword_dim):
        """Get keyword vectors for battlefield cards."""
        keywords = np.zeros((self.max_battlefield, keyword_dim), dtype=np.float32)
        for i, card_id in enumerate(card_ids):
             if i >= self.max_battlefield: break
             card = self._safe_get_card(card_id)
             if card and hasattr(card, 'keywords'):
                 kw_vector = np.array(getattr(card, 'keywords', []))
                 current_len = len(kw_vector)
                 if current_len == keyword_dim:
                      keywords[i, :] = kw_vector
                 elif current_len < keyword_dim:
                      keywords[i, :current_len] = kw_vector # Pad end
                 else: # current_len > keyword_dim
                      keywords[i, :] = kw_vector[:keyword_dim] # Truncate
        return keywords

    def _get_deck_composition(self, player):
        """Estimate deck composition based on known cards."""
        composition = np.zeros(6, dtype=np.float32)
        known_cards = player.get("hand", []) + player.get("battlefield", []) + player.get("graveyard", []) + player.get("exile", [])
        total_known = len(known_cards)
        if total_known == 0: return composition

        counts = defaultdict(int)
        for card_id in known_cards:
            card = self._safe_get_card(card_id)
            if card:
                 if 'creature' in getattr(card, 'card_types', []): counts['creature'] += 1
                 elif 'instant' in getattr(card, 'card_types', []): counts['instant'] += 1
                 elif 'sorcery' in getattr(card, 'card_types', []): counts['sorcery'] += 1
                 elif 'artifact' in getattr(card, 'card_types', []): counts['artifact'] += 1
                 elif 'enchantment' in getattr(card, 'card_types', []): counts['enchantment'] += 1
                 elif 'land' in getattr(card, 'type_line', '').lower(): counts['land'] += 1

        composition[0] = counts['creature'] / total_known
        composition[1] = counts['instant'] / total_known
        composition[2] = counts['sorcery'] / total_known
        composition[3] = counts['artifact'] / total_known
        composition[4] = counts['enchantment'] / total_known
        composition[5] = counts['land'] / total_known
        return composition

    def _get_threat_assessment(self, opp_bf_ids):
        """Assess threat level of opponent's board."""
        threats = np.zeros(self.max_battlefield, dtype=np.float32)
        if self.strategic_planner:
            threat_list = self.strategic_planner.assess_threats() # Get list of dicts
            threat_map = {t['card_id']: t['level'] for t in threat_list}
            for i, card_id in enumerate(opp_bf_ids):
                 if i >= self.max_battlefield: break
                 threats[i] = threat_map.get(card_id, 0.0) / 10.0 # Normalize
        return threats

    def _get_opportunity_assessment(self, hand_ids, player):
        """Assess opportunities presented by cards in hand."""
        opportunities = np.zeros(self.max_hand_size, dtype=np.float32)
        if self.card_evaluator:
             for i, card_id in enumerate(hand_ids):
                  if i >= self.max_hand_size: break
                  card = self._safe_get_card(card_id)
                  if card:
                      # Evaluate playability *and* potential impact
                      can_play = self._get_hand_playable([card_id], player, self.game_state.turn % 2 == 1)[0] > 0
                      value = self.card_evaluator.evaluate_card(card_id, "play") if can_play else 0
                      opportunities[i] = min(1.0, value / 5.0) # Normalize max value
        return opportunities

    def _get_resource_efficiency(self, player, turn):
        """Calculate resource efficiency metrics."""
        efficiency = np.zeros(3, dtype=np.float32)
        # Mana efficiency: % of lands tapped or mana used this turn? Complex.
        # Simple: Lands available vs turn number
        lands_in_play = sum(1 for cid in player.get("battlefield", []) if self._is_land(cid))
        efficiency[0] = min(1.0, lands_in_play / max(1, turn))
        # Card efficiency: Cards drawn vs turns passed?
        cards_drawn = getattr(self.game_state, 'cards_drawn_this_turn', {}).get('p1' if player==self.game_state.p1 else 'p2', 0)
        # Cumulative draw efficiency (crude)
        total_drawn = cards_drawn + 7 # Initial hand + draws
        efficiency[1] = min(1.0, total_drawn / max(7, turn + 6)) # Compare against expected cards drawn
        # Tempo: Avg CMC of permanents vs turn. Higher early CMC might be bad tempo unless ramp.
        cmc_sum = sum(getattr(self._safe_get_card(cid), 'cmc', 0) for cid in player.get("battlefield", []) if self._safe_get_card(cid))
        num_perms = len(player.get("battlefield", []))
        avg_cmc = cmc_sum / max(1, num_perms)
        efficiency[2] = min(1.0, max(0, 1.0 - abs(avg_cmc - turn / 2) / 5.0)) # Closer avg CMC to half turn num is better?
        return efficiency

    def _get_ability_features(self, bf_ids, player):
        """Get features related to activatable abilities."""
        features = np.zeros((self.max_battlefield, 5), dtype=np.float32)
        gs = self.game_state
        if not hasattr(gs, 'ability_handler'): return features

        for i, card_id in enumerate(bf_ids):
            if i >= self.max_battlefield: break
            abilities = gs.ability_handler.get_activated_abilities(card_id)
            if not abilities: continue
            features[i, 0] = len(abilities) # Ability count
            activatable_count = 0
            mana_count = 0
            draw_count = 0
            removal_count = 0
            for j, ability in enumerate(abilities):
                 if gs.ability_handler.can_activate_ability(card_id, j, player):
                      activatable_count += 1
                 effect_text = getattr(ability, 'effect', '').lower()
                 if "add mana" in effect_text or "add {" in effect_text: mana_count += 1
                 if "draw" in effect_text: draw_count += 1
                 if "destroy" in effect_text or "exile" in effect_text or "damage" in effect_text: removal_count += 1
            features[i, 1] = activatable_count
            features[i, 2] = mana_count
            features[i, 3] = draw_count
            features[i, 4] = removal_count
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

    def _get_multi_turn_plan_metrics(self):
        """Convert strategic planner's multi-turn plan to metrics."""
        metrics = np.zeros(6, dtype=np.float32)
        if self.strategic_planner and hasattr(self.strategic_planner, 'plan_multi_turn_sequence'):
            try:
                plan = self.strategic_planner.plan_multi_turn_sequence(depth=2)
                if plan:
                    metrics[0] = min(1.0, len(plan[0].get('plays',[])) / 3.0) # Plays this turn
                    metrics[1] = float(plan[0].get('land_play') is not None) # Land this turn
                    metrics[2] = min(1.0, plan[0].get('expected_mana', 0) / 10.0) # Mana this turn
                    if len(plan) > 1:
                         metrics[3] = min(1.0, len(plan[1].get('plays',[])) / 3.0) # Plays next turn
                         metrics[4] = float(plan[1].get('land_play') is not None) # Land next turn
                         metrics[5] = min(1.0, plan[1].get('expected_mana', 0) / 10.0) # Mana next turn
            except Exception as plan_e:
                 logging.warning(f"Error getting multi-turn plan metrics: {plan_e}")
        return metrics

    def _get_mulligan_info(self, player):
        """Get mulligan recommendation and reasons."""
        recommendation = 0.5 # Neutral default
        reasons_arr = np.zeros(5, dtype=np.float32)
        reason_count = 0
        gs = self.game_state
        if getattr(gs, 'mulligan_in_progress', False) and getattr(gs, 'mulligan_player', None) == player:
            if self.strategic_planner and hasattr(self.strategic_planner, 'suggest_mulligan_decision'):
                try:
                     is_on_play = (gs.turn == 1 and gs.agent_is_p1) # Simplified on_play check
                     deck_name = self.current_deck_name_p1 if player == gs.p1 else self.current_deck_name_p2
                     decision = self.strategic_planner.suggest_mulligan_decision(player.get("hand",[]), deck_name, is_on_play)
                     recommendation = float(decision.get('keep', False))
                     reason_codes = {"Too few lands": 0, "Too many lands": 1, "No early plays": 2, "Too many expensive cards": 3, "Lacks interaction": 4} # Example mapping
                     for i, reason in enumerate(decision.get('reasoning', [])[:5]):
                          # Simple representation: Set flag if reason category exists
                          # A more sophisticated approach could map specific reasons.
                          reasons_arr[i] = 1.0 # Just mark presence of a reason
                     reason_count = min(5, len(decision.get('reasoning', [])))
                except Exception as mull_e:
                     logging.warning(f"Error getting mulligan recommendation: {mull_e}")
        return recommendation, reasons_arr, reason_count

    def _get_recommendations(self, current_mask):
        """Get action recommendations from planner and memory."""
        rec_action = -1
        rec_conf = 0.0
        mem_action = -1
        matches = 0
        valid_list = np.where(current_mask)[0]

        if self.strategic_planner:
             try:
                 rec_action = self.strategic_planner.recommend_action(valid_list)
                 # Crude confidence based on position
                 analysis = getattr(self.strategic_planner, 'current_analysis', {})
                 score = analysis.get('position', {}).get('score', 0)
                 rec_conf = 0.5 + abs(score) * 0.4 # Map score [-1, 1] to confidence [0.5, 0.9]
             except Exception: pass # Ignore errors

        if self.strategy_memory:
            try: mem_action = self.strategy_memory.get_suggested_action(self.game_state, valid_list)
            except Exception: pass

        if rec_action is not None and rec_action == mem_action:
             matches = 1

        # Ensure -1 if None
        rec_action = -1 if rec_action is None else rec_action
        mem_action = -1 if mem_action is None else mem_action

        return rec_action, rec_conf, mem_action, matches

    def _get_attacker_values(self, bf_ids, player):
        """Evaluate the strategic value of attacking with each potential attacker."""
        values = np.zeros(self.max_battlefield, dtype=np.float32)
        if self.strategic_planner and hasattr(self.strategic_planner, 'evaluate_attack_action'):
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
                          card = self._safe_get_card(card_id)
                          values[i] = getattr(card, 'power', 0) * 0.5 if card else 0.0

        return values

    def _phase_to_onehot(self, phase):
        """Convert phase to one-hot encoding for better RL learning"""
        # Use the highest phase constant (PHASE_CLEANUP) to determine the size
        max_phase = self.game_state.PHASE_CLEANUP
        onehot = np.zeros(max_phase + 1, dtype=np.float32)
        
        # Only set the element if it's within bounds
        if 0 <= phase <= max_phase:
            onehot[phase] = 1.0
        else:
            # Log warning if phase is out of expected range
            logging.warning(f"Phase {phase} is out of the expected range (0-{max_phase})")
        
        return onehot
