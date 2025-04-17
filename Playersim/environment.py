
import random
import logging
import re
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
from .deck_stats_tracker import DeckStatsTracker
from .card_memory import CardMemory

try:
    # Create a dummy card to get feature dimension
    logging.info("Creating dummy card for feature dimension calculation...")
    dummy_card_data = {"name": "Dummy", "type_line": "Creature", "mana_cost": "{1}"}
    dummy_card = Card(dummy_card_data)
    logging.info(f"Successfully created dummy card: {dummy_card}")
    
    # Try to get the feature vector with detailed debugging
    logging.info("Attempting to get feature vector from dummy card...")
    feature_vector = dummy_card.to_feature_vector()
    logging.info(f"Got feature vector with type: {type(feature_vector)}, shape: {len(feature_vector)}")
    
    # Set the dimension
    FEATURE_DIM = len(feature_vector)
    logging.info(f"Successfully determined FEATURE_DIM dynamically: {FEATURE_DIM}")
except Exception as e:
    import traceback
    logging.error(f"Error determining FEATURE_DIM dynamically: {e}")
    logging.error(traceback.format_exc())
    logging.warning("Using fallback dimension value of 223")
    FEATURE_DIM = 223  # Fallback
    
class AlphaZeroMTGEnv(gym.Env):
    """
    An example Magic: The Gathering environment that uses the Gymnasium (>= 0.26) API.
    Updated for improved reward shaping, richer observations, modularity, and detailed logging.
    """
    ACTION_SPACE_SIZE = 480 # Moved constant here

    def __init__(self, decks, card_db, max_turns=20, max_hand_size=7, max_battlefield=20):
        logging.info("Initializing AlphaZeroMTGEnv...")
        super().__init__()
        self.decks = decks
        self.card_db = card_db
        self.max_turns = max_turns
        self.max_hand_size = max_hand_size
        self.max_battlefield = max_battlefield
        self.strategy_memory = StrategyMemory()
        self.current_episode_actions = []
        self.current_analysis = None
        self._feature_dim = FEATURE_DIM if 'FEATURE_DIM' in globals() else 223  # Store determined feature dim with fallback
        logging.info(f"Using feature dimension: {self._feature_dim}")

        # Initialize deck statistics tracker (Corrected class name usage)
        try:
            self.stats_tracker = DeckStatsTracker() # Use the imported tracker
            self.has_stats_tracker = True
        except (ImportError, ModuleNotFoundError, NameError):
            logging.warning("DeckStatsTracker not available, statistics will not be recorded")
            self.stats_tracker = None
            self.has_stats_tracker = False

        # --- ADDED: Initialize Card Memory ---
        try:
            self.card_memory = CardMemory() # Initialize CardMemory
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

        # Initialize action handler AFTER GameState
        self.action_handler = ActionHandler(self.game_state)

        # GameState initializes its own subsystems now
        self.combat_resolver = getattr(self.game_state, 'combat_resolver', None) # Get ref if needed
        integrate_combat_actions(self.game_state) # Integrate after GS subsystems are ready

        # Feature dimension determined dynamically above
        MAX_PHASE = self.game_state.PHASE_CLEANUP # Use PHASE_CLEANUP as highest constant now

        self.action_memory_size = 80

        # Correct keyword size based on Card class
        keyword_dimension = len(Card.ALL_KEYWORDS)
        logging.info(f"Using keyword dimension: {keyword_dimension}")

        # --- UPDATED: Observation Space with Context Facilitation Fields ---
        self.observation_space = spaces.Dict({
            # --- Existing Fields (mostly unchanged shapes) ---
            "phase": spaces.Box(low=0, high=MAX_PHASE, shape=(1,), dtype=np.int32),
            "phase_onehot": spaces.Box(low=0, high=1, shape=(MAX_PHASE + 1,), dtype=np.float32),
            "turn": spaces.Box(low=0, high=self.max_turns, shape=(1,), dtype=np.int32),
            "is_my_turn": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "my_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "opp_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "life_difference": spaces.Box(low=-40, high=40, shape=(1,), dtype=np.int32),
            "p1_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p2_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "my_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, self._feature_dim), dtype=np.float32),
            "my_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "opp_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "hand_playable": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_card_types": spaces.Box(low=0, high=1, shape=(self.max_hand_size, 5), dtype=np.float32),
            "hand_performance": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_synergy_scores": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "opportunity_assessment": spaces.Box(low=0, high=10, shape=(self.max_hand_size,), dtype=np.float32),
            "my_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "my_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "my_battlefield_keywords": spaces.Box(low=0, high=1, shape=(self.max_battlefield, keyword_dimension), dtype=np.float32),
            "my_tapped_permanents": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=bool),
            "opp_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "opp_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "p1_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "p2_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, self._feature_dim), dtype=np.float32),
            "p1_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "p2_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
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
            "my_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "my_mana": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "total_available_mana": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "untapped_land_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "remaining_mana_sources": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "turn_vs_mana": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "my_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "graveyard_key_cards": spaces.Box(low=-1, high=50, shape=(10, self._feature_dim), dtype=np.float32),
            "exile_key_cards": spaces.Box(low=-1, high=50, shape=(10, self._feature_dim), dtype=np.float32),
            "stack_count": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "stack_controller": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.int32),
            "stack_card_types": spaces.Box(low=0, high=1, shape=(5, 5), dtype=np.float32),
            "attackers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "blockers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "potential_combat_damage": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "ability_features": spaces.Box(low=0, high=10, shape=(self.max_battlefield, 5), dtype=np.float32),
            "ability_timing": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "planeswalker_activations": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activation_counts": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "previous_actions": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE, shape=(self.action_memory_size,), dtype=np.int32),
            "previous_rewards": spaces.Box(low=-10, high=10, shape=(self.action_memory_size,), dtype=np.float32),
            "phase_history": spaces.Box(low=-1, high=MAX_PHASE, shape=(5,), dtype=np.int32),
            "action_mask": spaces.Box(low=0, high=1, shape=(self.ACTION_SPACE_SIZE,), dtype=bool),
            "recommended_action": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE - 1, shape=(1,), dtype=np.int32),
            "recommended_action_confidence": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "memory_suggested_action": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE - 1, shape=(1,), dtype=np.int32),
            "suggestion_matches_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "optimal_attackers": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "attacker_values": spaces.Box(low=-10, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "ability_recommendations": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5, 2), dtype=np.float32),
            "strategic_metrics": spaces.Box(low=-1, high=1, shape=(10,), dtype=np.float32),
            "position_advantage": spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32),
            "estimated_opponent_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, self._feature_dim), dtype=np.float32),
            "deck_composition_estimate": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "opponent_archetype": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "future_state_projections": spaces.Box(low=-1, high=1, shape=(7,), dtype=np.float32),
            "multi_turn_plan": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_viability": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_timings": spaces.Box(low=0, high=self.max_turns + 1, shape=(6,), dtype=np.float32),
            "resource_efficiency": spaces.Box(low=0, high=1, shape=(3,), dtype=np.float32),
            "mulligan_in_progress": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "mulligan_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "mulligan_reason_count": spaces.Box(low=0, high=5, shape=(1,), dtype=np.int32),
            "mulligan_reasons": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "targetable_permanents": spaces.Box(low=-1, high=500, shape=(self.max_battlefield * 2,), dtype=np.int32),
            "targetable_players": spaces.Box(low=-1, high=1, shape=(2,), dtype=np.int32),
            "targetable_spells_on_stack": spaces.Box(low=-1, high=20, shape=(5,), dtype=np.int32),
            "targetable_cards_in_graveyards": spaces.Box(low=-1, high=200, shape=(10 * 2,), dtype=np.int32),
            "sacrificeable_permanents": spaces.Box(low=-1, high=self.max_battlefield, shape=(self.max_battlefield,), dtype=np.int32),
            "selectable_modes": spaces.Box(low=-1, high=10, shape=(10,), dtype=np.int32),
            "selectable_colors": spaces.Box(low=-1, high=4, shape=(5,), dtype=np.int32),
            "valid_x_range": spaces.Box(low=0, high=100, shape=(2,), dtype=np.int32),
            "bottomable_cards": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=bool),
            "dredgeable_cards_in_gy": spaces.Box(low=-1, high=100, shape=(6,), dtype=np.int32),
        })
        # --- End Observation Space Update ---
        self.action_space = spaces.Discrete(self.ACTION_SPACE_SIZE)
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
        # Added attribute tracking phase/choice context specifically
        self.current_choice_context = None

        

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
        try:
            # Check if CardMemory system exists AND if the flag is set
            if not hasattr(self, 'has_card_memory') or not self.has_card_memory or not self.card_memory: return

            player_key = player_idx
            opponent_key = 1 - player_key

            player_played = cards_played_data.get(player_key, [])
            player_opening = opening_hands_data.get(f'p{player_key+1}', [])
            player_draws = draw_history_data.get(f'p{player_key+1}', {})

            player_turn_played = {}
            for card_id in player_played:
                 for turn, cards in player_draws.items():
                     if card_id in cards:
                         player_turn_played[card_id] = int(turn) + 1; break

            for card_id in set(player_deck):
                card = self.game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'name'): continue

                perf_data = {
                    'is_win': not is_draw,
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

            for card_id in set(opponent_deck):
                 card = self.game_state._safe_get_card(card_id)
                 if card and hasattr(card, 'name'):
                     self.card_memory.register_card(card_id, card.name, {
                          'cmc': getattr(card, 'cmc', 0),
                          'types': getattr(card, 'card_types', []),
                          'colors': getattr(card, 'colors', []) })

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
        Action execution and game state progression are handled by ActionHandler.apply_action.

        Args:
            action_idx: Index of the action selected by the agent.
            context: Optional dictionary with additional context for complex actions.
                    This context will be merged with relevant game state context by ActionHandler.

        Returns:
            tuple: (observation, reward, done, truncated, info)
        """
        gs = self.game_state
        action_context = {} # Local dict for passing context
        if context: action_context.update(context)

        # Ensure initial mask is generated for info dict even if action invalidates immediately
        if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None:
            logging.warning("current_valid_actions missing or invalid at step start. Regenerating.")
            self.current_valid_actions = self.action_mask()

        initial_mask = self.current_valid_actions.astype(bool) if self.current_valid_actions is not None else np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
        info = {"action_mask": initial_mask, "game_result": "undetermined"}

        try:
            # --- Action Validation ---
            if not (0 <= action_idx < self.ACTION_SPACE_SIZE):
                logging.error(f"Action index {action_idx} is out of bounds (0-{self.ACTION_SPACE_SIZE-1}).")
                obs = self._get_obs_safe() # Get safe obs
                info["critical_error"] = True
                info["error_message"] = "Action index out of bounds"
                # Use initial mask from above
                return obs, -0.5, False, False, info # Return negative reward, not done

            # Validate against the *current* action mask
            current_mask = self.action_mask() # Re-fetch mask just before check
            if not current_mask[action_idx]:
                logging.warning(f"Step {self.current_step}: Invalid action {action_idx} selected (Mask False). Reason: {self.action_handler.action_reasons.get(action_idx, 'Not valid')}. Available: {np.where(current_mask)[0]}")
                self.invalid_action_count += 1
                self.episode_invalid_actions += 1
                step_reward = -0.1 # Standard penalty

                # Update history even for invalid mask selection
                if hasattr(self, 'last_n_actions'): self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                if hasattr(self, 'last_n_rewards'): self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
                if hasattr(self, 'current_episode_actions'): self.current_episode_actions.append(action_idx)

                done, truncated = False, False
                if self.invalid_action_count >= self.invalid_action_limit:
                    logging.error(f"Exceeded invalid action limit ({self.invalid_action_count}). Terminating episode.")
                    done, truncated, step_reward = True, True, -2.0

                obs = self._get_obs_safe()
                info["action_mask"] = current_mask.astype(bool) # Update mask in info
                info["invalid_action"] = True
                info["invalid_action_reason"] = self.action_handler.action_reasons.get(action_idx, 'Not valid')
                if done: self.ensure_game_result_recorded() # Record if terminated due to invalid limit
                return obs, step_reward, done, truncated, info

            # Reset invalid action counter on valid action
            self.invalid_action_count = 0

            # --- Execute Action using ActionHandler ---
            # Increment step counter only AFTER validation
            self.current_step += 1

            # ActionHandler.apply_action handles the game logic, internal loops, SBAs, stack, rewards etc.
            if not hasattr(self, 'action_handler') or self.action_handler is None:
                raise RuntimeError("ActionHandler is not initialized.")

            # --- Pass CONTEXT to ActionHandler ---
            # ActionHandler will merge relevant GameState contexts (targeting, sacrifice etc.)
            # with the context passed from the agent here.
            obs, reward, done, truncated, info = self.action_handler.apply_action(
                action_idx, context=action_context
            )

            # --- Post-Action Environment Updates ---
            # Update environment-level history/tracking
            if hasattr(self, 'last_n_actions'):
                self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
            if hasattr(self, 'last_n_rewards'):
                self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = reward
            if hasattr(self, 'current_episode_actions'):
                self.current_episode_actions.append(action_idx)
            if hasattr(self, 'episode_rewards'):
                self.episode_rewards.append(reward)

            # Ensure game result is recorded if 'done' flag is set
            if done and not getattr(self, '_game_result_recorded', False):
                self.ensure_game_result_recorded()

            # Final Action Mask in Info should come from the observation generated by ActionHandler
            # apply_action should already include the latest mask in its returned info dict.
            if "action_mask" not in info:
                logging.warning("Info dict returned by ActionHandler.apply_action is missing 'action_mask'.")
                # Regenerate as fallback
                info["action_mask"] = self.action_mask().astype(bool)


            # Add detailed logging if enabled
            if self.detailed_logging:
                action_type, param = self.action_handler.get_action_info(action_idx)
                logging.info(f"--- Env Step {self.current_step} ---")
                logging.info(f"Action Taken: {action_idx} ({action_type}({param})) Context: {action_context}")
                logging.info(f"Returned State: Turn {gs.turn}, Phase {self.game_state._PHASE_NAMES.get(gs.phase, gs.phase)}, Prio {getattr(gs.priority_player,'name','None')}")
                logging.info(f"Reward: {reward:.4f}, Done: {done}, Truncated: {truncated}")
                # Log key observation parts maybe? e.g. life totals, board sizes

            # Final verification for critical observation keys
            for critical_key in ["ability_features", "ability_recommendations"]:
                if critical_key not in obs:
                    logging.critical(f"{critical_key} missing in step return observation! Adding default.")
                    if hasattr(self, 'observation_space') and critical_key in self.observation_space.spaces:
                        space = self.observation_space.spaces[critical_key] 
                        obs[critical_key] = np.zeros(space.shape, dtype=space.dtype)
                    else:
                        # Fallback shapes if observation space is inconsistent
                        if critical_key == "ability_features":
                            obs[critical_key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                        elif critical_key == "ability_recommendations":
                            obs[critical_key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)

            return obs, reward, done, truncated, info

        except Exception as e:
            # --- Critical Error Handling within Step ---
            logging.critical(f"CRITICAL error in step function (Action {action_idx}): {str(e)}", exc_info=True)
            # Get safe observation
            obs = self._get_obs_safe()
            # Create minimal safe info with an error flag
            mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); mask[11] = True; mask[12] = True # Pass/Concede
            final_info = {
                "action_mask": mask,
                "critical_error": True,
                "error_message": f"Unhandled Exception: {str(e)}"
            }
            # End the episode immediately due to critical failure
            self.ensure_game_result_recorded() # Ensure some result is recorded, even if error
            return obs, -5.0, True, False, final_info
        
    def _get_obs_safe(self):
        """Return a minimal, safe observation dictionary in case of errors. (Reinforced)"""
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
            critical_keys = ["ability_features", "ability_recommendations"]
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
            for key in ["ability_features", "ability_recommendations"]:
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
                        "p1_life": np.array([0], dtype=np.int32),
                        "p2_life": np.array([0], dtype=np.int32),
                        "my_hand_count": np.array([0], dtype=np.int32),
                        "opp_hand_count": np.array([0], dtype=np.int32),
                        "p1_bf_count": np.array([0], dtype=np.int32),
                        "p2_bf_count": np.array([0], dtype=np.int32),
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
                    "phase": np.array([0], dtype=np.int32)
                }
        
        # Final verification before returning
        for key in ["ability_features", "ability_recommendations"]:
            if key not in obs:
                logging.critical(f"{key} STILL MISSING after all fixes! Adding as final solution!")
                if key == "ability_features":
                    obs[key] = np.zeros((self.max_battlefield, 5), dtype=np.float32)
                elif key == "ability_recommendations":
                    obs[key] = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
            
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
            # 0. Ensure layer effects are applied first
            if hasattr(self, 'layer_system') and self.layer_system:
                try:
                    self.layer_system.apply_all_effects()
                except Exception as layer_e:
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
                     logging.error(f"Error generating action mask in _get_obs: {mask_e}", exc_info=True)
                     # Use default mask with pass/concede if generation fails
                     current_mask[11] = True; current_mask[12] = True;
            else:
                 logging.warning("ActionHandler not available in _get_obs, using default mask.")
                 current_mask[11] = True; current_mask[12] = True;
            obs["action_mask"] = current_mask.astype(bool) # Assign the potentially corrected mask


            # --- 3. Populate Basic Game State Info ---
            is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
            current_phase = getattr(gs, 'phase', 0)
            current_turn = getattr(gs, 'turn', 1)
            keyword_dimension = len(Card.ALL_KEYWORDS) # Ensure this matches

            obs["phase"] = np.array([current_phase], dtype=np.int32)
            obs["phase_onehot"] = self._phase_to_onehot(current_phase)
            obs["turn"] = np.array([current_turn], dtype=np.int32)
            obs["is_my_turn"] = np.array([int(is_my_turn)], dtype=np.int32)
            obs["my_life"] = np.array([agent_player_obj.get('life', 0)], dtype=np.int32)
            obs["opp_life"] = np.array([opp.get('life', 0)], dtype=np.int32)
            obs["life_difference"] = np.array([agent_player_obj.get('life', 0) - opp.get('life', 0)], dtype=np.int32)
            obs["p1_life"] = np.array([getattr(gs.p1, 'life', 0)], dtype=np.int32)
            obs["p2_life"] = np.array([getattr(gs.p2, 'life', 0)], dtype=np.int32)

            # --- 4. Populate Zone Features and Related Info ---
            # Wrap potentially complex population blocks in try/excepts to prevent
            # errors in one section from stopping others, and ensuring defaults remain.
            try:
                # Hand state
                obs["my_hand"] = self._get_zone_features(agent_player_obj.get("hand", []), self.max_hand_size)
                obs["my_hand_count"] = np.array([len(agent_player_obj.get("hand", []))], dtype=np.int32)
                obs["opp_hand_count"] = np.array([len(opp.get("hand", []))], dtype=np.int32)
                obs["hand_card_types"] = self._get_hand_card_types(agent_player_obj.get("hand", []))
                obs["hand_playable"] = self._get_hand_playable(agent_player_obj.get("hand", []), agent_player_obj, is_my_turn)
                obs["hand_performance"] = self._get_hand_performance(agent_player_obj.get("hand", []))
                obs["hand_synergy_scores"] = self._get_hand_synergy_scores(agent_player_obj.get("hand", []), agent_player_obj.get("battlefield", []))
                obs["opportunity_assessment"] = self._get_opportunity_assessment(agent_player_obj.get("hand", []), agent_player_obj)
            except Exception as e:
                logging.error(f"Error populating hand features in _get_obs: {e}", exc_info=True)
                # Keep default zero values initialized earlier

            try:
                # Battlefield state
                obs["my_battlefield"] = self._get_zone_features(agent_player_obj.get("battlefield", []), self.max_battlefield)
                obs["my_battlefield_flags"] = self._get_battlefield_flags(agent_player_obj.get("battlefield", []), agent_player_obj, self.max_battlefield)
                obs["my_battlefield_keywords"] = self._get_battlefield_keywords(agent_player_obj.get("battlefield", []), keyword_dimension)
                obs["my_tapped_permanents"] = self._get_tapped_mask(agent_player_obj.get("battlefield", []), agent_player_obj.get("tapped_permanents", set()), self.max_battlefield)
                obs["opp_battlefield"] = self._get_zone_features(opp.get("battlefield", []), self.max_battlefield)
                obs["opp_battlefield_flags"] = self._get_battlefield_flags(opp.get("battlefield", []), opp, self.max_battlefield)
                obs["p1_battlefield"] = self._get_zone_features(gs.p1.get("battlefield", []), self.max_battlefield)
                obs["p2_battlefield"] = self._get_zone_features(gs.p2.get("battlefield", []), self.max_battlefield)
                obs["p1_bf_count"] = np.array([len(gs.p1.get("battlefield", []))], dtype=np.int32)
                obs["p2_bf_count"] = np.array([len(gs.p2.get("battlefield", []))], dtype=np.int32)
            except Exception as e:
                logging.error(f"Error populating battlefield features in _get_obs: {e}", exc_info=True)

            try:
                # Creature stats
                my_creature_stats = self._get_creature_stats(agent_player_obj.get("battlefield", []))
                opp_creature_stats = self._get_creature_stats(opp.get("battlefield", []))
                obs["my_creature_count"] = np.array([my_creature_stats['count']], dtype=np.int32)
                obs["opp_creature_count"] = np.array([opp_creature_stats['count']], dtype=np.int32)
                obs["my_total_power"] = np.array([my_creature_stats['power']], dtype=np.int32)
                obs["my_total_toughness"] = np.array([my_creature_stats['toughness']], dtype=np.int32)
                obs["opp_total_power"] = np.array([opp_creature_stats['power']], dtype=np.int32)
                obs["opp_total_toughness"] = np.array([opp_creature_stats['toughness']], dtype=np.int32)
                obs["power_advantage"] = np.array([my_creature_stats['power'] - opp_creature_stats['power']], dtype=np.int32)
                obs["toughness_advantage"] = np.array([my_creature_stats['toughness'] - opp_creature_stats['toughness']], dtype=np.int32)
                obs["creature_advantage"] = np.array([my_creature_stats['count'] - opp_creature_stats['count']], dtype=np.int32)
                obs["threat_assessment"] = self._get_threat_assessment(opp.get("battlefield", []))
                obs["card_synergy_scores"] = self._calculate_card_synergies(agent_player_obj.get("battlefield", []))
            except Exception as e:
                logging.error(f"Error populating creature stats in _get_obs: {e}", exc_info=True)

            try:
                # Mana state
                obs["my_mana_pool"] = np.array([agent_player_obj.get("mana_pool", {}).get(c, 0) for c in ['W', 'U', 'B', 'R', 'G', 'C']], dtype=np.int32)
                obs["my_mana"] = np.array([sum(agent_player_obj.get("mana_pool", {}).values())], dtype=np.int32)
                obs["untapped_land_count"] = np.array([sum(1 for cid in agent_player_obj.get("battlefield", []) if self._is_land(cid) and cid not in agent_player_obj.get("tapped_permanents", set()))], dtype=np.int32)
                obs["total_available_mana"] = np.array([sum(agent_player_obj.get("mana_pool", {}).values()) + obs["untapped_land_count"][0]], dtype=np.int32) # Simplified total available
                obs["turn_vs_mana"] = np.array([min(1.0, len([cid for cid in agent_player_obj.get("battlefield",[]) if self._is_land(cid)]) / max(1.0, float(current_turn)))], dtype=np.float32)
                obs["remaining_mana_sources"] = np.array([obs["untapped_land_count"][0]], dtype=np.int32) # Same as untapped lands for now
            except Exception as e:
                logging.error(f"Error populating mana features in _get_obs: {e}", exc_info=True)

            try:
                # Graveyard/Exile state
                obs["my_graveyard_count"] = np.array([len(agent_player_obj.get("graveyard", []))], dtype=np.int32)
                obs["opp_graveyard_count"] = np.array([len(opp.get("graveyard", []))], dtype=np.int32)
                obs["my_dead_creatures"] = np.array([sum(1 for cid in agent_player_obj.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32)
                obs["opp_dead_creatures"] = np.array([sum(1 for cid in opp.get("graveyard", []) if self._is_creature(cid))], dtype=np.int32)
                obs["graveyard_key_cards"] = self._get_zone_features(agent_player_obj.get("graveyard", []), 10)
                obs["exile_key_cards"] = self._get_zone_features(agent_player_obj.get("exile", []), 10)
            except Exception as e:
                logging.error(f"Error populating graveyard/exile features in _get_obs: {e}", exc_info=True)

            try:
                # Stack state
                stack_controllers, stack_types = self._get_stack_info(gs.stack, agent_player_obj)
                obs["stack_count"] = np.array([len(gs.stack)], dtype=np.int32)
                obs["stack_controller"] = stack_controllers
                obs["stack_card_types"] = stack_types
            except Exception as e:
                logging.error(f"Error populating stack features in _get_obs: {e}", exc_info=True)

            try:
                # Combat state
                obs["attackers_count"] = np.array([len(getattr(gs, 'current_attackers', []))], dtype=np.int32)
                obs["blockers_count"] = np.array([sum(len(b) for b in getattr(gs, 'current_block_assignments', {}).values())], dtype=np.int32)
                obs["potential_combat_damage"] = np.zeros(1, dtype=np.int32) # Placeholder
            except Exception as e:
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
                     logging.error(f"CRITICAL: _get_ability_features returned invalid result! "
                                  f"Got type {type(ability_features_result)}, shape {getattr(ability_features_result, 'shape', 'N/A')}, dtype {getattr(ability_features_result, 'dtype', 'N/A')}. "
                                  f"Expected shape {expected_space.shape}, dtype {expected_space.dtype}. Resetting to zeros.")
                     obs[ability_features_key] = np.zeros(expected_space.shape, dtype=expected_space.dtype)
                else:
                    obs[ability_features_key] = ability_features_result

            except Exception as ab_feat_e:
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
                logging.error(f"Error populating post-ability features: {e}", exc_info=True)


            # --- 6. Populate History & Planning Features (inside try/except) ---
            try:
                phase_hist = getattr(self,'_phase_history',[]) # Use getattr
                phase_hist_len = len(phase_hist)
                phase_hist_arr = np.full(5, -1, dtype=np.int32)
                if phase_hist_len > 0: phase_hist_arr[-min(phase_hist_len, 5):] = phase_hist[-min(phase_hist_len, 5):] # Fill from end
                obs["phase_history"] = phase_hist_arr
                obs["previous_actions"] = np.array(self.last_n_actions if hasattr(self, 'last_n_actions') else [-1]*self.action_memory_size, dtype=np.int32)
                obs["previous_rewards"] = np.array(self.last_n_rewards if hasattr(self, 'last_n_rewards') else [0.0]*self.action_memory_size, dtype=np.float32)

                # Planning Features
                obs["strategic_metrics"] = np.zeros(10, dtype=np.float32) # Default
                obs["position_advantage"] = np.array([self._calculate_position_advantage()], dtype=np.float32)
                obs["estimated_opponent_hand"] = self._estimate_opponent_hand()
                obs["deck_composition_estimate"] = self._get_deck_composition(agent_player_obj)
                obs["opponent_archetype"] = np.zeros(6, dtype=np.float32) # Default
                obs["future_state_projections"] = np.zeros(7, dtype=np.float32) # Default
                obs["multi_turn_plan"] = np.zeros(6, dtype=np.float32) # Default
                obs["win_condition_viability"] = np.zeros(6, dtype=np.float32) # Default
                obs["win_condition_timings"] = np.zeros(6, dtype=np.float32) # Default
                obs["resource_efficiency"] = self._get_resource_efficiency(agent_player_obj, current_turn)

                # Recommendations (Defaults assigned earlier)
                # Action Mask already assigned

                # Mulligan state (Defaults assigned earlier)
                obs["mulligan_in_progress"] = np.array([int(getattr(gs, 'mulligan_in_progress', False))], dtype=np.int32)

                # Context Features (Defaults assigned earlier)
                obs["targetable_permanents"] = self._get_potential_targets_vector('permanent')
                obs["targetable_players"] = self._get_potential_targets_vector('player')
                obs["targetable_spells_on_stack"] = self._get_potential_targets_vector('spell')
                obs["targetable_cards_in_graveyards"] = self._get_potential_targets_vector('graveyard_card')
                obs["sacrificeable_permanents"] = self._get_potential_sacrifices()
                obs["selectable_modes"] = self._get_available_choice_options('mode')
                obs["selectable_colors"] = self._get_available_choice_options('color')
                obs["valid_x_range"] = self._get_available_choice_options('x_range')
                obs["bottomable_cards"] = self._get_bottoming_mask(agent_player_obj)
                obs["dredgeable_cards_in_gy"] = self._get_dredge_options(agent_player_obj)

            except Exception as e:
                logging.error(f"Error populating history/planning/context features in _get_obs: {e}", exc_info=True)


            # --- 7. Populate Dynamic/Planner Dependent Features (wrapped) ---
            # (Keep existing planner population logic, wrapped in try/except)
            try:
                if hasattr(self, 'strategic_planner') and self.strategic_planner:
                    # (Planner population logic - remains the same as previous version)
                    # ... (fill strategic_metrics, opponent_archetype, recommendations etc.) ...
                    analysis = getattr(self, 'current_analysis', None)
                    if not analysis or analysis.get("game_info", {}).get("turn") != current_turn:
                         analysis = self.strategic_planner.analyze_game_state()
                         self.current_analysis = analysis

                    if analysis:
                        obs["strategic_metrics"][:10] = self._analysis_to_metrics(analysis)[:10]
                        obs["position_advantage"][0] = analysis.get("position", {}).get("score", 0)

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
                    obs["win_condition_viability"][:wc_viab_len] = np.array([win_cons.get(k, {}).get("score", 0.0) for k in wc_keys[:wc_viab_len]], dtype=np.float32)
                    obs["win_condition_timings"][:wc_time_len] = np.array([min(self.max_turns + 1, win_cons.get(k, {}).get("turns_to_win", 99)) for k in wc_keys[:wc_time_len]], dtype=np.float32)

                    plan_len = min(len(obs["multi_turn_plan"]), 6)
                    obs["multi_turn_plan"][:plan_len] = self._get_multi_turn_plan_metrics()[:plan_len]

                    bf_ids_opp = opp.get("battlefield", [])
                    threat_assessment_values = self._get_threat_assessment(bf_ids_opp)
                    copy_len_threat = min(len(obs["threat_assessment"]), len(threat_assessment_values))
                    obs["threat_assessment"][:copy_len_threat] = threat_assessment_values[:copy_len_threat]

                    valid_actions_list = np.where(current_mask)[0].tolist()
                    rec_action, rec_conf, mem_action, matches = self._get_recommendations(valid_actions_list)
                    obs["recommended_action"][0] = rec_action
                    obs["recommended_action_confidence"][0] = rec_conf
                    obs["memory_suggested_action"][0] = mem_action
                    obs["suggestion_matches_recommendation"][0] = matches

                    bf_ids_agent = agent_player_obj.get("battlefield", [])
                    if hasattr(self.strategic_planner,'find_optimal_attack'):
                        optimal_ids = self.strategic_planner.find_optimal_attack()
                        optimal_mask = np.zeros(self.max_battlefield, dtype=np.float32)
                        for i, cid in enumerate(bf_ids_agent[:self.max_battlefield]):
                            if cid in optimal_ids: optimal_mask[i] = 1.0
                        if obs["optimal_attackers"].shape == optimal_mask.shape: obs["optimal_attackers"][:] = optimal_mask

                        attacker_values = self._get_attacker_values(bf_ids_agent, agent_player_obj)
                        if obs["attacker_values"].shape == attacker_values.shape: obs["attacker_values"][:] = attacker_values

                    ability_recs = self._get_ability_recommendations(bf_ids_agent, agent_player_obj)
                    if obs["ability_recommendations"].shape == ability_recs.shape: obs["ability_recommendations"][:,:,:] = ability_recs

            except Exception as planner_e:
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
                 logging.error(f"Error populating mulligan features in _get_obs: {e}", exc_info=True)


            # --- 9. FINAL VALIDATION AND RETURN ---
            logging.debug(f"_get_obs: Populated keys: {list(obs.keys())}")
            # Explicitly check the problematic key *just before returning*
            # Special handling for critical 'ability_features' key that's causing issues
            ability_features_key = "ability_features"
            if ability_features_key in self.observation_space.spaces:
                expected_space = self.observation_space.spaces[ability_features_key]
                if ability_features_key not in obs:
                    logging.critical(f"_get_obs_safe: Critical key '{ability_features_key}' is missing! Creating default zeros.")
                    obs[ability_features_key] = np.zeros(expected_space.shape, dtype=expected_space.dtype)
                elif not isinstance(obs[ability_features_key], np.ndarray):
                    logging.critical(f"_get_obs_safe: Critical key '{ability_features_key}' is not an ndarray! Recreating.")
                    obs[ability_features_key] = np.zeros(expected_space.shape, dtype=expected_space.dtype)
                elif obs[ability_features_key].shape != expected_space.shape:
                    logging.critical(f"_get_obs_safe: Critical key '{ability_features_key}' has wrong shape! Got {obs[ability_features_key].shape}, expected {expected_space.shape}. Recreating.")
                    obs[ability_features_key] = np.zeros(expected_space.shape, dtype=expected_space.dtype)

            # Optional: Full validation loop (can be verbose)
            # if not self._validate_obs(obs): # Check if final obs is valid
            #      logging.error("Observation failed validation in _get_obs after all population. Returning safe observation.")
            #      return self._get_obs_safe() # This safe obs also needs validation

            return obs

        # --- Main Exception Handling for _get_obs ---
        except Exception as e:
            logging.critical(f"CRITICAL error during _get_obs execution: {str(e)}", exc_info=True)
            # Attempt to return safe observation
            try:
                 safe_obs = self._get_obs_safe()
                 # Double-check the problematic key in the safe obs
                 ability_features_key = "ability_features"
                 if ability_features_key not in safe_obs:
                    logging.error(f"CRITICAL: _get_obs_safe also failed to include '{ability_features_key}'. Manually adding.")
                    safe_obs[ability_features_key] = np.zeros(self.observation_space[ability_features_key].shape, dtype=self.observation_space[ability_features_key].dtype)
                 elif safe_obs[ability_features_key].shape != self.observation_space[ability_features_key].shape:
                    logging.error(f"CRITICAL: _get_obs_safe produced wrong shape for '{ability_features_key}'. Correcting.")
                    safe_obs[ability_features_key] = np.zeros(self.observation_space[ability_features_key].shape, dtype=self.observation_space[ability_features_key].dtype)
                 return safe_obs
            except Exception as safe_e:
                 logging.critical(f"CRITICAL error generating SAFE observation: {safe_e}", exc_info=True)
                 # Last resort: manually create a dict with zeros and valid mask
                 obs = {k: np.zeros(space.shape, dtype=space.dtype) for k, space in self.observation_space.spaces.items()}
                 obs['action_mask'] = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool); obs['action_mask'][11]=True; obs['action_mask'][12]=True;
                 # Ensure the key exists even in this ultimate fallback
                 obs['ability_features'] = np.zeros(self.observation_space['ability_features'].shape, dtype=self.observation_space['ability_features'].dtype)
                 return obs

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
        # Fallback: Return zeros if planner is not available
        recs = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32) # shape (bf_size, max_abilities, [recommend, conf])
        if not self.strategic_planner or not self.action_handler.game_state.ability_handler:
            return recs

        gs = self.game_state
        ability_handler = gs.ability_handler
        if not ability_handler: return recs # Double check handler exists

        for i, card_id in enumerate(bf_ids):
             if i >= self.max_battlefield: break
             abilities = ability_handler.get_activated_abilities(card_id)
             for j, ability in enumerate(abilities):
                  if j >= 5: break # Max 5 abilities per card
                  try:
                      # Check if actually activatable first
                      can_activate = ability_handler.can_activate_ability(card_id, j, player)
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
    
    def _get_potential_targets_vector(self, target_kind):
        """Helper to get INDICES for targetable entities of a specific kind. Returns np.array of indices padded with -1."""
        gs = self.game_state
        agent_player_obj = gs.p1 if gs.agent_is_p1 else gs.p2
        valid_targets_info = [] # Store tuples (target_id, index)
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
            source_id = gs.targeting_context["source_id"]
            controller = gs.targeting_context["controller"]
            if controller == agent_player_obj: # Only show targets if it's agent's turn to target
                valid_targets_map = {}
                if gs.targeting_system:
                     valid_targets_map = gs.targeting_system.get_valid_targets(source_id, controller)
                else: logging.warning("Targeting system unavailable in _get_potential_targets")

                # Flatten the map into a list of IDs while preserving order for indexing
                flat_valid_target_ids = []
                if target_kind == 'permanent':
                    for cat in ["creatures", "artifacts", "enchantments", "lands", "planeswalkers", "battles", "permanents"]:
                        flat_valid_target_ids.extend(valid_targets_map.get(cat, []))
                elif target_kind == 'player':
                    # Represent players by index 0 (P1) and 1 (P2)
                    player_indices = []
                    if "p1" in valid_targets_map.get("players", []): player_indices.append(0)
                    if "p2" in valid_targets_map.get("players", []): player_indices.append(1)
                    flat_valid_target_ids.extend(player_indices)
                elif target_kind == 'spell':
                     # Use stack index as the reference for spells/abilities
                     for stack_idx, item in enumerate(gs.stack):
                         if isinstance(item, tuple) and len(item) > 3 and item[0] == "SPELL":
                              spell_id_on_stack = item[1]
                              if spell_id_on_stack in valid_targets_map.get("spells",[]):
                                  flat_valid_target_ids.append(stack_idx) # Add stack index
                elif target_kind == 'graveyard_card':
                     # Use graveyard index relative to owner's graveyard
                     p1_gy = gs.p1.get("graveyard", [])
                     p2_gy = gs.p2.get("graveyard", [])
                     valid_gy_cards = valid_targets_map.get("cards", [])
                     for card_id in valid_gy_cards:
                         gy_idx = -1
                         if card_id in p1_gy: gy_idx = p1_gy.index(card_id)
                         elif card_id in p2_gy: gy_idx = p2_gy.index(card_id)
                         if gy_idx != -1: flat_valid_target_ids.append(gy_idx) # Add graveyard index
                else: # Other specific types
                    cat_key = target_kind + "s" if not target_kind.endswith('s') else target_kind
                    flat_valid_target_ids.extend(valid_targets_map.get(cat_key,[]))

                # Assign indices based on the flattened list order
                for i, target_identifier in enumerate(list(set(flat_valid_target_ids))): # Use unique IDs
                    valid_targets_info.append((target_identifier, i))

        # Encode Indices (pad/truncate)
        encoded_indices = np.full(max_size, -1, dtype=dtype_for_space)
        for i, (target_id, index) in enumerate(valid_targets_info):
             if i >= max_size: break
             # Use the calculated index `i` which corresponds to the agent's choice parameter
             encoded_indices[i] = i

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
            obs_key = f"selectable_{choice_kind}s" if choice_kind in ['mode', 'color'] else f"valid_{choice_kind}_range"
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
                    max_selectable = context.get("max_modes", 1)
                    selected_already = context.get("selected_modes", [])
                    can_select_more = len(selected_already) < max_selectable

                    if can_select_more:
                        available_mode_indices = []
                        for i in range(num_choices):
                            # Mode is represented by its index (0, 1, 2...)
                            # Avoid selecting duplicates if not allowed (most cases)
                            if max_selectable == 1 and i in selected_already: continue # Don't show selected if only choosing 1
                            # Add logic here if multiple different modes CAN be selected
                            if i not in selected_already:
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
        mask = np.zeros(self.max_hand_size, dtype=bool)
        if getattr(gs, 'bottoming_in_progress', False) and getattr(gs, 'bottoming_player', None) == player:
            for i in range(len(player.get("hand", []))):
                if i < self.max_hand_size: mask[i] = True
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
        # Fallback: Return zeros if strategic planner is not available
        if not self.strategic_planner:
            return np.zeros(10, dtype=np.float32)

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
        # Fallback: Return zeros if strategic planner is not available
        if not self.strategic_planner:
            return np.zeros(self.max_battlefield, dtype=np.float32)

        threats = np.zeros(self.max_battlefield, dtype=np.float32)
        if self.strategic_planner and hasattr(self.strategic_planner, 'assess_threats'):
            threat_list = self.strategic_planner.assess_threats() # Get list of dicts
            threat_map = {t['card_id']: t['level'] for t in threat_list}
            for i, card_id in enumerate(opp_bf_ids):
                 if i >= self.max_battlefield: break
                 threats[i] = threat_map.get(card_id, 0.0) / 10.0 # Normalize
        return threats

    def _get_opportunity_assessment(self, hand_ids, player):
        """Assess opportunities presented by cards in hand."""
        # Fallback: Return zeros if card evaluator is not available
        if not self.card_evaluator:
            return np.zeros(self.max_hand_size, dtype=np.float32)

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
                    is_mana = isinstance(ability, gs.ability_handler.ManaAbility) or ("add mana" in effect_text or "add {" in effect_text and "target" not in effect_text)
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

    def _get_recommendations(self, valid_actions_list): # Renamed argument
        """Get action recommendations from planner and memory."""
        rec_action = -1
        rec_conf = 0.0
        mem_action = -1
        matches = 0

        # Planner Recommendation (Check existence first)
        if self.strategic_planner and hasattr(self.strategic_planner, 'recommend_action'):
             try:
                 rec_action = self.strategic_planner.recommend_action(valid_actions_list) # Pass the list
                 # Crude confidence based on position
                 analysis = getattr(self.strategic_planner, 'current_analysis', {})
                 score = analysis.get('position', {}).get('score', 0)
                 rec_conf = 0.5 + abs(score) * 0.4 # Map score [-1, 1] to confidence [0.5, 0.9]
             except Exception: pass # Ignore errors

        # Memory Recommendation (Check existence first)
        if self.strategy_memory and hasattr(self.strategy_memory, 'get_suggested_action'):
            try: mem_action = self.strategy_memory.get_suggested_action(self.game_state, valid_actions_list) # Pass list
            except Exception: pass

        if rec_action is not None and rec_action == mem_action:
             matches = 1

        # Ensure -1 if None
        rec_action = -1 if rec_action is None else rec_action
        mem_action = -1 if mem_action is None else mem_action

        return rec_action, rec_conf, mem_action, matches

    def _get_attacker_values(self, bf_ids, player):
        """Evaluate the strategic value of attacking with each potential attacker."""
        # Fallback: Return zeros if planner is not available
        if not self.strategic_planner or not hasattr(self.strategic_planner, 'evaluate_attack_action'):
             return np.zeros(self.max_battlefield, dtype=np.float32)

        values = np.zeros(self.max_battlefield, dtype=np.float32)
        # Use self.action_handler for attacker check
        if not self.action_handler: return values # Need action handler for validation

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
