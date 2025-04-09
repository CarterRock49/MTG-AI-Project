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
from .deck_stats_tracker import DeckStatsCollector
from .card_memory import CardMemory

class AlphaZeroMTGEnv(gym.Env):
    """
    An example Magic: The Gathering environment that uses the Gymnasium (>= 0.26) API.
    Updated for improved reward shaping, richer observations, modularity, and detailed logging.
    """
    
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
        # Initialize deck statistics tracker
        try:
            from .deck_stats_tracker import DeckStatsTracker
            self.stats_tracker = DeckStatsTracker()
            self.has_stats_tracker = True
        except (ImportError, ModuleNotFoundError):
            logging.warning("DeckStatsTracker not available, statistics will not be recorded")
            self.stats_tracker = None
            self.has_stats_tracker = False
        try:
            from .card_memory import CardMemory
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
        self.game_state = GameState(card_db, max_turns, max_hand_size, max_battlefield)
        
        # Initialize action handler
        self.action_handler = ActionHandler(self.game_state)
        
        # Initialize combat resolver
        self.combat_resolver = ExtendedCombatResolver(self.game_state)
        # Integrate combat actions
        integrate_combat_actions(self.game_state)
        # Feature dimension for card vectors
        FEATURE_DIM = 223

        MAX_PHASE = self.game_state.PHASE_CLEANUP
        
        self.action_memory_size = 80
        
        try:
            # Create a dummy card to get feature dimension
            dummy_card_data = {"name": "Dummy", "type_line": "Creature", "mana_cost": "{1}"}
            FEATURE_DIM = len(Card(dummy_card_data).to_feature_vector())
            logging.info(f"Determined FEATURE_DIM dynamically: {FEATURE_DIM}")
        except Exception as e:
            logging.warning(f"Could not determine FEATURE_DIM dynamically, using fallback 223: {e}")
            FEATURE_DIM = 223 # Fallback

        self.observation_space = spaces.Dict({
            # --- Existing ---
            "recommended_action": spaces.Box(low=0, high=480, shape=(1,), dtype=np.int32),
            "recommended_action_confidence": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "memory_suggested_action": spaces.Box(low=0, high=480, shape=(1,), dtype=np.int32),
            "suggestion_matches_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "optimal_attackers": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "attacker_values": spaces.Box(low=-10, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activations": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activation_counts": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "ability_recommendations": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5, 2), dtype=np.float32), # Shape might need adjustment based on ability count per card
            "phase": spaces.Discrete(MAX_PHASE + 1),
            "mulligan_in_progress": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "mulligan_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "mulligan_reason_count": spaces.Box(low=0, high=5, shape=(1,), dtype=np.int32),
            "mulligan_reasons": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32), # Represent reasons numerically if possible
            "phase_onehot": spaces.Box(low=0, high=1, shape=(MAX_PHASE + 1,), dtype=np.float32),
            "turn": spaces.Box(low=0, high=self.max_turns, shape=(1,), dtype=np.int32),
            "p1_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p2_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p1_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32), # Use -1 for empty slots?
            "p2_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "p1_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "p2_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "my_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "my_mana": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32), # Sum of mana pool
            "my_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32), # WUBRGC
            "my_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, FEATURE_DIM), dtype=np.float32),
            "my_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "action_mask": spaces.Box(low=0, high=1, shape=(self.ACTION_SPACE_SIZE,), dtype=bool), # Use constant
            "stack_count": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "my_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "hand_playable": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_performance": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32), # Redundant?
            "tapped_permanents": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=bool), # Player specific needed?
            "phase_history": spaces.Box(low=-1, high=MAX_PHASE, shape=(5,), dtype=np.int32), # Use -1 for padding
            "remaining_mana_sources": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "is_my_turn": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "life_difference": spaces.Box(low=-40, high=40, shape=(1,), dtype=np.int32),
            "opp_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "card_synergy_scores": spaces.Box(low=-1, high=1, shape=(self.max_battlefield, self.max_battlefield), dtype=np.float32),
            "graveyard_key_cards": spaces.Box(low=-1, high=50, shape=(10, FEATURE_DIM), dtype=np.float32),
            "exile_key_cards": spaces.Box(low=-1, high=50, shape=(10, FEATURE_DIM), dtype=np.float32),
            "battlefield_keywords": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 15), dtype=np.float32), # Player specific needed? Increase keyword count?
            "position_advantage": spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32),
            "estimated_opponent_hand": spaces.Box(low=-1, high=50, shape=(self.max_hand_size, FEATURE_DIM), dtype=np.float32),
            "strategic_metrics": spaces.Box(low=-1, high=1, shape=(10,), dtype=np.float32), # Define specific metrics?
            "deck_composition_estimate": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Define components?
            "threat_assessment": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32), # Opponent battlefield specific?
            "opportunity_assessment": spaces.Box(low=0, high=10, shape=(self.max_hand_size,), dtype=np.float32),
            "resource_efficiency": spaces.Box(low=0, high=1, shape=(3,), dtype=np.float32), # Define components?
            "my_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "my_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32), # Tapped, Sick, Attacking, Blocking, Keywords?
            "opp_battlefield": spaces.Box(low=-1, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "opp_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "my_creature_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "opp_creature_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "my_total_power": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "my_total_toughness": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_total_power": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_total_toughness": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "power_advantage": spaces.Box(low=-100, high=100, shape=(1,), dtype=np.int32),
            "toughness_advantage": spaces.Box(low=-100, high=100, shape=(1,), dtype=np.int32),
            "creature_advantage": spaces.Box(low=-self.max_battlefield, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "hand_card_types": spaces.Box(low=0, high=1, shape=(self.max_hand_size, 5), dtype=np.float32), # Land, Creature, Instant, Sorcery, Other
            "opp_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "total_available_mana": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32), # Sum of pool + potential from untapped lands?
            "untapped_land_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "turn_vs_mana": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32), # Mana available / Turn number?
            "stack_controller": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.int32), # Top 5 stack items: 0=me, 1=opp, -1=empty
            "stack_card_types": spaces.Box(low=0, high=1, shape=(5, 5), dtype=np.float32), # Top 5 items: Creature, Inst, Sorc, Ability, Other
            "my_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "attackers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "blockers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "potential_combat_damage": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32), # Expected unblocked damage?
            "ability_features": spaces.Box(low=0, high=10, shape=(self.max_battlefield, 5), dtype=np.float32), # Count, Can Activate, Mana, Draw, Removal?
            "ability_timing": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32), # Appropriateness score per phase type?
            "previous_actions": spaces.Box(low=-1, high=self.ACTION_SPACE_SIZE, shape=(self.action_memory_size,), dtype=np.int32), # Use -1 padding
            "previous_rewards": spaces.Box(low=-10, high=10, shape=(self.action_memory_size,), dtype=np.float32),
            "hand_synergy_scores": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "opponent_archetype": spaces.Box(low=0, high=1, shape=(4,), dtype=np.float32), # Aggro, Control, Midrange, Combo?
            "future_state_projections": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.float32), # E.g. life diff, board diff in N turns
            "multi_turn_plan": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Encoded plan
            "win_condition_viability": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32), # Viability of common WCs
            "win_condition_timings": spaces.Box(low=0, high=self.max_turns, shape=(6,), dtype=np.float32), # Estimated turns for WCs
        })

        # Add memory for actions and rewards

        self.last_n_actions = np.zeros(self.action_memory_size, dtype=np.int32)
        self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)
        
        self.action_space = spaces.Discrete(480)
        self.invalid_action_limit = 150  # Max invalid actions before episode termination
        self.max_episode_steps = 2000
        
        # Episode metrics
        self.current_step = 0
        self.current_step = 0
        self.invalid_action_count = 0
        self.episode_rewards = []
        self.episode_invalid_actions = 0
        self.current_episode_actions = []  # Add this line
        self.detailed_logging = False
        
                # Initialize ability handler if available
        if hasattr(self, 'ability_handler'):
            # Only clear if we're going to create a new one
            if self.ability_handler is not None:
                logging.debug("Clearing existing ability handler")
                self.ability_handler = None
                
        # Initialize turn tracking for abilities
        if hasattr(self, 'ability_handler') and self.ability_handler:
            if hasattr(self.ability_handler, 'initialize_turn_tracking'):
                self.ability_handler.initialize_turn_tracking()
        
        # Valid actions
        self.current_valid_actions = np.zeros(480, dtype=bool)
        

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
        """Return the current action mask as boolean array"""
        # Ignore the env argument if passed
        
        # Check if the game state has changed in a way that would affect valid actions
        gs = self.game_state
        current_phase = gs.phase
        current_turn = gs.turn
        current_stack_size = len(gs.stack)
        current_attackers = len(gs.current_attackers)
        
        # Determine if we need to regenerate the action mask
        state_changed = not hasattr(self, '_last_action_mask_state') or \
                    self._last_action_mask_state['phase'] != current_phase or \
                    self._last_action_mask_state['turn'] != current_turn or \
                    self._last_action_mask_state['stack_size'] != current_stack_size or \
                    self._last_action_mask_state['attackers'] != current_attackers
        
        if state_changed or np.sum(self.current_valid_actions) == 0:
            try:
                self.current_valid_actions = self.action_handler.generate_valid_actions()
                
                # Update state tracking
                self._last_action_mask_state = {
                    'phase': current_phase,
                    'turn': current_turn,
                    'stack_size': current_stack_size,
                    'attackers': current_attackers
                }
            except Exception as e:
                logging.error(f"Error generating valid actions: {str(e)}")
                # Fallback to basic action mask if generation fails
                self.current_valid_actions = np.zeros(480, dtype=bool)  # Updated size to480
                self.current_valid_actions[0] = True  # Enable END_TURN as fallback
        
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

            # --- Reset GameState and Player Setup ---
            # Choose random decks
            p1_deck_data = random.choice(self.decks)
            p2_deck_data = random.choice(self.decks)
            self.current_deck_name_p1 = p1_deck_data["name"]
            self.current_deck_name_p2 = p2_deck_data["name"]
            self.original_p1_deck = p1_deck_data["cards"].copy() # Store original for memory
            self.original_p2_deck = p2_deck_data["cards"].copy()

            # Initialize GameState (creates players, resets turn/phase, etc.)
            # Make sure GameState's constructor doesn't auto-initialize subsystems we handle below
            self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
            # GameState's reset performs deck setup, shuffling, initial draw
            self.game_state.reset(p1_deck_data["cards"], p2_deck_data["cards"], seed)

            # --- Initialize & Link Subsystems to GameState ---
            # Call GameState's consolidated subsystem initialization FIRST
            # This should create ManaSystem, AbilityHandler, LayerSystem, ReplacementEffects, CombatResolver etc.
            # *inside* the GameState object
            self.game_state._init_subsystems() # Ensure this happens before env components need them

            # Initialize strategy memory early if others depend on it
            self.initialize_strategic_memory()
            if self.strategy_memory:
                 self.game_state.strategy_memory = self.strategy_memory

            # Initialize stats/card memory trackers if available and link to GS
            if self.has_stats_tracker:
                self.game_state.stats_tracker = self.stats_tracker
                self.stats_tracker.current_deck_name_p1 = self.current_deck_name_p1
                self.stats_tracker.current_deck_name_p2 = self.current_deck_name_p2
            if self.has_card_memory:
                self.game_state.card_memory = self.card_memory


            # --- Initialize Environment Components Using GameState Subsystems ---
            # Action Handler depends on GameState and its subsystems
            self.action_handler = ActionHandler(self.game_state)

            # Get references to components created by GameState for env use
            # These might be None if imports failed or init failed in GS
            self.combat_resolver = getattr(self.game_state, 'combat_resolver', None)
            self.card_evaluator = getattr(self.game_state, 'card_evaluator', None)
            self.strategic_planner = getattr(self.game_state, 'strategic_planner', None)
            self.mana_system = getattr(self.game_state, 'mana_system', None)
            self.ability_handler = getattr(self.game_state, 'ability_handler', None) # Ensure handler is linked
            self.layer_system = getattr(self.game_state, 'layer_system', None)
            self.replacement_effects = getattr(self.game_state, 'replacement_effects', None)
            self.targeting_system = getattr(self.game_state, 'targeting_system', None)


            # Ensure integration after handlers are created
            integrate_combat_actions(self.game_state) # Link ActionHandler's combat_handler
            # Combat setup needs to happen *after* combat_resolver is created in GameState
            if self.action_handler.combat_handler and self.combat_resolver:
                self.action_handler.combat_handler.setup_combat_systems()

            # Link strategy memory to planner AFTER planner is initialized in GS
            if self.strategy_memory and self.strategic_planner:
                self.strategic_planner.strategy_memory = self.strategy_memory

            # Link evaluator/resolver back to planner if they were created independently
            if self.strategic_planner:
                 if self.card_evaluator and not self.strategic_planner.card_evaluator:
                     self.strategic_planner.card_evaluator = self.card_evaluator
                 if self.combat_resolver and not self.strategic_planner.combat_resolver:
                     self.strategic_planner.combat_resolver = self.combat_resolver


            # --- Final Reset Steps ---
            # Reset mulligan state AFTER subsystems are ready
            gs = self.game_state # Alias for convenience
            gs.mulligan_in_progress = True
            gs.mulligan_player = gs.p1 # Start with P1's mulligan decision
            gs.mulligan_count = {'p1': 0, 'p2': 0} # Ensure mulligan counts are reset
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
            obs = self._get_obs_safe() # Use safe get obs
            info = {"action_mask": self.current_valid_actions.astype(bool)}

            logging.info(f"Environment {env_id} reset complete. Starting new episode (Turn {gs.turn}, Phase {gs.phase}).")
            logging.info(f"P1 Deck: {self.current_deck_name_p1}, P2 Deck: {self.current_deck_name_p2}")

            return obs, info

        except Exception as e:
            logging.error(f"CRITICAL error during environment reset: {str(e)}")
            logging.error(traceback.format_exc())

            # Emergency reset fallback (simplified)
            try:
                logging.warning("Attempting emergency fallback reset...")
                self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
                deck = self.decks[0]["cards"].copy() if self.decks else [0]*60
                self.game_state.reset(deck, deck.copy(), seed)
                # Minimal systems setup might be needed here depending on _get_obs_safe needs
                self.game_state._init_subsystems() # Re-run subsystem init on the new GS
                # Ensure action handler is recreated for the new GS
                self.action_handler = ActionHandler(self.game_state)

                self.current_valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                self.current_valid_actions[11] = True # PASS
                self.current_valid_actions[12] = True # CONCEDE

                obs = self._get_obs_safe()
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
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        opp = gs.p2 if gs.agent_is_p1 else gs.p1
        
        # Determine the winner based on game state
        if me["life"] <= 0:
            is_p1_winner = not gs.agent_is_p1
            winner_life = opp["life"]
        elif opp["life"] <= 0:
            is_p1_winner = gs.agent_is_p1
            winner_life = me["life"]
        else:  # Game ended due to turn limit or other reason
            is_p1_winner = me["life"] > opp["life"] if gs.agent_is_p1 else opp["life"] < me["life"]
            winner_life = me["life"] if is_p1_winner == gs.agent_is_p1 else opp["life"]
        
        # Record the game result
        if self.has_stats_tracker:
            self.record_game_result(is_p1_winner, gs.turn, winner_life)
            self._game_result_recorded = True
            
        if self.has_card_memory:
            is_draw = me["life"] == opp["life"]  # Determine if it's a draw
            if not is_draw:
                winner_deck = self.original_p1_deck if is_p1_winner else self.original_p2_deck
                loser_deck = self.original_p2_deck if is_p1_winner else self.original_p1_deck
                winner_archetype = self.current_deck_name_p1 if is_p1_winner else self.current_deck_name_p2
                loser_archetype = self.current_deck_name_p2 if is_p1_winner else self.current_deck_name_p1
                
                # Create minimal data for recording
                cards_played = getattr(self, 'cards_played', {0: [], 1: []})
                opening_hands = {}  # Would need to be tracked elsewhere
                draw_history = {}   # Would need to be tracked elsewhere
                
                self._record_cards_to_memory(winner_deck, loser_deck, cards_played, gs.turn,
                                            winner_archetype, loser_archetype,
                                            opening_hands, draw_history)
        
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
    
    def _record_cards_to_memory(self, winner_deck, loser_deck, cards_played, turn_count,
                        winner_archetype, loser_archetype, opening_hands, draw_history):
        """Record detailed card performance data to the card memory system."""
        try:
            if not self.has_card_memory or not self.card_memory:
                return
                
            # Process winner deck cards
            winner_played = cards_played.get(0, [])
            winner_opening = opening_hands.get("winner", [])
            winner_draws = draw_history.get("winner", {})
            
            # Create a mapping of when cards were played
            winner_turn_played = {}
            for card_id in winner_played:
                # Try to find when the card was played from draw history
                for turn, cards in winner_draws.items():
                    if card_id in cards:
                        # Assume card is played the turn after it's drawn
                        winner_turn_played[card_id] = int(turn) + 1
                        break
            
            # Process each card in winner's deck
            for card_id in set(winner_deck):
                # Get synergy partners (other cards played this game)
                synergy_partners = [cid for cid in winner_played if cid != card_id]
                
                # Record performance
                self.card_memory.update_card_performance(card_id, {
                    'is_win': True,
                    'is_draw': False,
                    'was_played': card_id in winner_played,
                    'was_drawn': any(card_id in cards for turn, cards in winner_draws.items()),
                    'turn_played': winner_turn_played.get(card_id, 0),
                    'in_opening_hand': card_id in winner_opening,
                    'game_duration': turn_count,
                    'deck_archetype': winner_archetype,
                    'opponent_archetype': loser_archetype,
                    'synergy_partners': synergy_partners
                })
                
                # Also register the card if it's not already in memory
                card = self.game_state._safe_get_card(card_id)
                if card and hasattr(card, 'name'):
                    self.card_memory.register_card(
                        card_id, 
                        card.name,
                        {
                            'cmc': card.cmc if hasattr(card, 'cmc') else 0,
                            'types': card.card_types if hasattr(card, 'card_types') else [],
                            'colors': card.colors if hasattr(card, 'colors') else []
                        }
                    )
            
            # Process loser deck cards
            loser_played = cards_played.get(1, [])
            loser_opening = opening_hands.get("loser", [])
            loser_draws = draw_history.get("loser", {})
            
            # Create a mapping of when cards were played
            loser_turn_played = {}
            for card_id in loser_played:
                # Try to find when the card was played from draw history
                for turn, cards in loser_draws.items():
                    if card_id in cards:
                        # Assume card is played the turn after it's drawn
                        loser_turn_played[card_id] = int(turn) + 1
                        break
            
            # Process each card in loser's deck
            for card_id in set(loser_deck):
                # Get synergy partners (other cards played this game)
                synergy_partners = [cid for cid in loser_played if cid != card_id]
                
                # Record performance
                self.card_memory.update_card_performance(card_id, {
                    'is_win': False,
                    'is_draw': False,
                    'was_played': card_id in loser_played,
                    'was_drawn': any(card_id in cards for turn, cards in loser_draws.items()),
                    'turn_played': loser_turn_played.get(card_id, 0),
                    'in_opening_hand': card_id in loser_opening,
                    'game_duration': turn_count,
                    'deck_archetype': loser_archetype,
                    'opponent_archetype': winner_archetype,
                    'synergy_partners': synergy_partners
                })
                
                # Also register the card if it's not already in memory
                card = self.game_state._safe_get_card(card_id)
                if card and hasattr(card, 'name'):
                    self.card_memory.register_card(
                        card_id, 
                        card.name,
                        {
                            'cmc': card.cmc if hasattr(card, 'cmc') else 0,
                            'types': card.card_types if hasattr(card, 'card_types') else [],
                            'colors': card.colors if hasattr(card, 'colors') else []
                        }
                    )
            
            # Save card memory async after each game
            self.card_memory.save_memory_async()
            
        except Exception as e:
            logging.error(f"Error recording cards to memory: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())

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
        # Ensure initial action mask is available if needed
        if not hasattr(self, 'current_valid_actions') or self.current_valid_actions is None:
             self.current_valid_actions = self.action_mask() # Should generate if None/missing
        # Start info dict with the mask valid *before* the action is taken
        info = {"action_mask": self.current_valid_actions.astype(bool) if self.current_valid_actions is not None else np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)}

        try:
            # --- Initial State & Setup ---
            self.current_step += 1
            step_reward = 0.0
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
                else:
                    # State didn't change, return current obs
                    obs = self._get_obs_safe() # Use safe get obs
                    info["invalid_action"] = True
                    info["action_mask"] = current_valid_actions.astype(bool) # Return the current valid mask
                    # Update action/reward history even for invalid mask selection
                    self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                    self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
                    return obs, step_reward, done, truncated, info

            self.invalid_action_count = 0 # Reset counter

            # --- Get Action Info & Pre-State ---
            action_type, param = self.action_handler.get_action_info(action_idx)
            if DEBUG_ACTION_STEPS: logging.info(f"Step {self.current_step}: Player {gs.priority_player['name']} trying {action_type}({param}) Context: {context}")
            self.current_episode_actions.append(action_idx) # Record valid action attempt

            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            prev_state = {
                "my_life": me["life"], "opp_life": opp["life"],
                "my_hand": len(me["hand"]), "opp_hand": len(opp["hand"]),
                "my_board": len(me["battlefield"]), "opp_board": len(opp["battlefield"]),
                "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me["battlefield"] if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp["battlefield"] if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
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
                try:
                    # Pass context, handle various return types
                    result = handler_func(param=param, context=context, action_type=action_type)
                    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], bool): action_reward, action_executed = result
                    elif isinstance(result, bool): action_reward, action_executed = (0.05, True) if result else (-0.1, False)
                    elif isinstance(result, (int, float)): action_reward, action_executed = float(result), True
                    else: action_reward, action_executed = 0.0, True # Assume success if None or other type returned
                    if action_reward is None: action_reward = 0.0

                except TypeError as te: # Fallback if handler doesn't take extra kwargs
                     if "unexpected keyword argument" in str(te):
                         try:
                              result = handler_func(param) # Call without extra kwargs
                              if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], bool): action_reward, action_executed = result
                              elif isinstance(result, bool): action_reward, action_executed = (0.05, True) if result else (-0.1, False)
                              elif isinstance(result, (int, float)): action_reward, action_executed = float(result), True
                              else: action_reward, action_executed = 0.0, True
                              if action_reward is None: action_reward = 0.0
                         except Exception as handler_e:
                              logging.error(f"Error executing handler {action_type} (fallback call): {handler_e}")
                              action_reward, action_executed = -0.2, False
                     else:
                         logging.error(f"TypeError executing handler {action_type}: {te}")
                         action_reward, action_executed = -0.2, False
                except Exception as handler_e:
                        logging.error(f"Error executing handler {action_type}: {handler_e}")
                        action_reward, action_executed = -0.2, False
            else:
                logging.warning(f"No handler implemented for action type: {action_type}")
                action_reward, action_executed = -0.05, False

            step_reward += action_reward
            info["action_reward"] = action_reward # Record action-specific reward

            # Handle execution failure immediately
            if not action_executed:
                logging.warning(f"Action {action_type}({param}) failed during execution.")
                self.invalid_action_count += 1
                self.episode_invalid_actions += 1
                step_reward = -0.15 # Execution failure penalty
                if self.invalid_action_count >= self.invalid_action_limit:
                    logging.error(f"Exceeded invalid action limit ({self.invalid_action_count}) after execution failure. Terminating episode.")
                    done, truncated, step_reward = True, True, -2.0
                # State might have partially changed, so get current obs
                obs = self._get_obs_safe()
                info["action_mask"] = self.action_mask().astype(bool) # Regenerate mask
                info["execution_failed"] = True
                self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
                return obs, step_reward, done, truncated, info

            # --- Main Game Loop (Post-Action Processing) ---
            # This loop continues until the AGENT gets priority OR the game ends.
            # -----------------------------------------------
            max_loops = 50 # Safety limit
            loop_count = 0
            while loop_count < max_loops:
                loop_count += 1
                state_changed_this_loop = False # Tracks if anything changed this iteration

                # 1. State-Based Actions (SBAs) - Repeat until stable
                # ----------------------------------------------------
                sbas_applied_in_cycle = True
                sba_cycles = 0
                while sbas_applied_in_cycle and sba_cycles < 10: # Inner loop limit for SBAs
                    # Layer effects applied *before* SBAs to ensure accurate state
                    if hasattr(gs, 'layer_system') and gs.layer_system:
                        gs.layer_system.apply_all_effects()

                    sbas_applied_in_cycle = gs.check_state_based_actions()
                    if sbas_applied_in_cycle:
                        state_changed_this_loop = True
                        # After SBAs apply, layers may need recalculation if state changed
                        if hasattr(gs, 'layer_system') and gs.layer_system:
                            gs.layer_system.apply_all_effects()
                    sba_cycles += 1
                if sba_cycles >= 10: logging.warning("Exceeded SBA cycle limit.")

                # 2. Check Game End from SBAs
                # ---------------------------
                # Check standard loss conditions
                if (me["life"] <= 0 or opp["life"] <= 0 or
                    me.get("attempted_draw_from_empty", False) or opp.get("attempted_draw_from_empty", False) or
                    me.get("poison_counters", 0) >= 10 or opp.get("poison_counters", 0) >= 10 or
                    me.get("lost_game", False) or opp.get("lost_game", False)):
                    done = True
                    logging.debug(f"Game ended due to SBAs (life/draw/poison/loss) in loop {loop_count}.")
                    break # Exit main loop
                # Check explicit win/draw conditions
                if me.get("won_game", False) or opp.get("won_game", False) or me.get("game_draw", False) or opp.get("game_draw", False):
                    done = True
                    logging.debug(f"Game ended due to explicit win/draw flag in loop {loop_count}.")
                    break

                # 3. Process Triggered Abilities (Put on Stack)
                # ---------------------------------------------
                if hasattr(gs.ability_handler, 'process_triggered_abilities'):
                    triggers_were_in_queue = bool(gs.ability_handler.active_triggers)
                    gs.ability_handler.process_triggered_abilities() # Adds triggers to gs.stack
                    if triggers_were_in_queue and not gs.ability_handler.active_triggers: # Check if queue emptied
                         state_changed_this_loop = True
                         gs.priority_player = gs._get_active_player() # Priority goes to AP after triggers are added
                         gs.priority_pass_count = 0
                         if DEBUG_ACTION_STEPS: logging.debug(f"Triggers added to stack, priority to {gs.priority_player['name']}")


                # 4. Check for Priority & Stack Resolution
                # ----------------------------------------
                current_priority_holder = gs.priority_player
                agent_player = gs.p1 if gs.agent_is_p1 else gs.p2

                # 4a. Check if Agent Regains Priority
                if current_priority_holder == agent_player:
                    # Agent has priority. Does agent need to act?
                    temp_mask = self.action_mask() # What can agent do now?
                    # Meaningful actions exclude PASS_PRIORITY (11) and CONCEDE (12)
                    meaningful_actions_exist = np.any(temp_mask & (np.arange(self.ACTION_SPACE_SIZE) != 11) & (np.arange(self.ACTION_SPACE_SIZE) != 12))

                    # Agent acts if stack is not empty OR if they have meaningful actions available
                    if gs.stack or meaningful_actions_exist:
                         if DEBUG_ACTION_STEPS: logging.debug(f"Agent ({agent_player['name']}) regains priority in phase {gs.phase}. Stack={len(gs.stack)}, Meaningful={meaningful_actions_exist}. Returning control.")
                         break # Return control to the agent
                    else:
                        # Agent has priority, stack empty, only Pass/Concede valid. Auto-pass.
                        if DEBUG_ACTION_STEPS: logging.debug(f"Agent ({agent_player['name']}) auto-passing priority (stack empty, only pass/concede).")
                        gs._pass_priority() # Let GameState handle the pass logic
                        state_changed_this_loop = True # Priority count/phase changed by _pass_priority
                        continue # Re-evaluate state after pass

                # 4b. Agent doesn't have priority - Simulate Opponent/Other Agent Pass
                else:
                    if DEBUG_ACTION_STEPS: logging.debug(f"Non-agent player ({current_priority_holder['name']}) holds priority. Auto-passing.")
                    gs._pass_priority() # Let GameState handle the pass logic
                    state_changed_this_loop = True # Priority/stack/phase might have changed
                    continue # Re-evaluate state after opponent pass

                # --- Loop break logic ---
                # Break if no state changes occurred this iteration AND agent has priority
                if not state_changed_this_loop and gs.priority_player == agent_player:
                     if DEBUG_ACTION_STEPS: logging.debug(f"Game state stabilized in loop {loop_count}, agent has priority.")
                     break

                # Safety Break (optional, if loop_count gets too high without state change)
                # if loop_count > max_loops - 5 and not state_changed_this_loop:
                #     logging.warning("Loop nearing limit with no state change. Potential stability issue?")
                #     break # Or force progress?

            # End of Main Loop Safety check
            if loop_count >= max_loops:
                logging.error(f"Exceeded max game loop iterations ({max_loops}). Potential infinite loop. Terminating.")
                done, truncated, step_reward = True, True, -3.0

            # --- Calculate Final Step Reward & Check Game End ---
            current_state = { # Gather final state
                 "my_life": me["life"], "opp_life": opp["life"], "my_hand": len(me["hand"]), "opp_hand": len(opp["hand"]), "my_board": len(me["battlefield"]), "opp_board": len(opp["battlefield"]),
                 "my_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in me["battlefield"] if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])),
                 "opp_power": sum(getattr(gs._safe_get_card(cid), 'power', 0) for cid in opp["battlefield"] if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])), }
            state_change_reward = self.action_handler._add_state_change_rewards(0.0, prev_state, current_state)
            step_reward += state_change_reward
            # Add board state reward component (call the reward function)
            step_reward += self._calculate_board_state_reward()
            info["state_reward"] = state_change_reward # Keep specific state change reward in info

            # Final Game End Checks (important after the loop)
            game_already_ended = done # Store if loop already ended game
            if not game_already_ended:
                 if opp["life"] <= 0: done, step_reward = True, step_reward + 10.0 + max(0, 20 - gs.turn) * 0.2; info["game_result"] = "win"
                 elif me["life"] <= 0: done, step_reward = True, step_reward - 10.0; info["game_result"] = "loss"
                 elif hasattr(gs, 'check_for_draw_conditions') and gs.check_for_draw_conditions(): done, step_reward = True, step_reward + 0.0; info["game_result"] = "draw"
                 elif me.get("lost_game"): done, step_reward = True, step_reward - 10.0; info["game_result"] = "loss" # Explicit loss flags
                 elif opp.get("lost_game"): done, step_reward = True, step_reward + 10.0 + max(0, 20 - gs.turn) * 0.2; info["game_result"] = "win" # Explicit win flags
                 elif me.get("won_game"): done, step_reward = True, step_reward + 10.0 + max(0, 20 - gs.turn) * 0.2; info["game_result"] = "win"
                 elif opp.get("won_game"): done, step_reward = True, step_reward - 10.0; info["game_result"] = "loss"
                 elif me.get("game_draw"): done, step_reward = True, step_reward + 0.0; info["game_result"] = "draw"
                 elif gs.turn > gs.max_turns: done, truncated = True, True; step_reward += (me["life"] - opp["life"]) * 0.1; info["game_result"] = "win" if (me["life"] > opp["life"]) else "loss" if (me["life"] < opp["life"]) else "draw"; logging.info("Turn limit reached.")
                 elif self.current_step >= self.max_episode_steps: done, truncated, step_reward = True, True, step_reward - 0.5; info["game_result"] = "truncated"; logging.info("Max episode steps reached.")


            # Final Log message for end of step
            if done or truncated:
                 logging.info(f"Game end condition met: done={done}, truncated={truncated}. Result: {info.get('game_result', 'unknown')}")

            # --- Finalize and Return ---
            self.episode_rewards.append(step_reward)
            if done: self.ensure_game_result_recorded() # Make sure result is saved

            obs = self._get_obs_safe() # Use safe version
            self.current_valid_actions = self.action_mask() # Final mask for agent's *next* decision
            info["action_mask"] = self.current_valid_actions.astype(bool)

            # Update action/reward history
            if hasattr(self, 'last_n_actions') and hasattr(self, 'last_n_rewards'):
                self.last_n_actions = np.roll(self.last_n_actions, 1); self.last_n_actions[0] = action_idx
                self.last_n_rewards = np.roll(self.last_n_rewards, 1); self.last_n_rewards[0] = step_reward
            else: # Initialize if missing
                self.last_n_actions = np.full(self.action_memory_size, -1, dtype=np.int32)
                self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)
                self.last_n_actions[0] = action_idx
                self.last_n_rewards[0] = step_reward


            # Update strategy memory (if used)
            if hasattr(gs, 'strategy_memory') and gs.strategy_memory and pre_action_pattern:
                 try: gs.strategy_memory.update_strategy(pre_action_pattern, step_reward)
                 except Exception as strategy_e: logging.error(f"Error updating strategy memory: {strategy_e}")

            if DEBUG_ACTION_STEPS:
                logging.debug(f"== STEP {self.current_step} END: reward={step_reward:.3f}, done={done}, truncated={truncated}, Phase={gs.phase}, Prio={gs.priority_player['name']} ==")
                # logging.debug(f"End state: P1 Life={gs.p1['life']}, P2 Life={gs.p2['life']}, Stack Size={len(gs.stack)}")

            return obs, step_reward, done, truncated, info

        except Exception as e:
            logging.error(f"CRITICAL error in step function (Action {action_idx}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            obs = self._get_obs_safe()
            mask = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
            mask[11] = True; mask[12] = True # Pass, Concede
            info = {"action_mask": mask, "critical_error": True, "error_message": str(e)}
            # Ensure game ends on critical error
            # Record result as loss due to error? Or leave neutral? Leave neutral for now.
            return obs, -5.0, True, False, info # Harsh penalty and end episode
        
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
        """Build an enhanced observation dictionary with comprehensive strategic information."""
        try:
            gs = self.game_state
            FEATURE_DIM = 223
            if hasattr(self, 'layer_system') and self.layer_system:
                self.layer_system.apply_all_effects()
            
            # Check if cached observation is valid
            if hasattr(self, '_cached_obs') and hasattr(self, '_cached_obs_turn') and hasattr(self, '_cached_obs_phase'):
                if self._cached_obs_turn == gs.turn and self._cached_obs_phase == gs.phase:
                    # Only regenerate if certain state has changed
                    key_state_changed = False
                    
                    # Check if battlefield changed
                    if (len(gs.p1["battlefield"]) != self._cached_battlefield_count_p1 or 
                        len(gs.p2["battlefield"]) != self._cached_battlefield_count_p2):
                        key_state_changed = True
                        
                    # Check if hand changed
                    elif (len(gs.p1["hand"]) != self._cached_hand_count_p1 or 
                        len(gs.p2["hand"]) != self._cached_hand_count_p2):
                        key_state_changed = True
                        
                    # Check if life totals changed
                    elif (gs.p1["life"] != self._cached_life_p1 or 
                        gs.p2["life"] != self._cached_life_p2):
                        key_state_changed = True
                        
                    # Check if stack changed
                    elif len(gs.stack) != self._cached_stack_size:
                        key_state_changed = True
                        
                    # Check if combat state changed
                    elif hasattr(self, '_cached_attackers_count') and len(gs.current_attackers) != self._cached_attackers_count:
                        key_state_changed = True
                        
                    if not key_state_changed:
                        return self._cached_obs
                    
            # Ensure strategic planner is initialized
            if not hasattr(self, 'strategic_planner') or not self.strategic_planner:
                # Try to initialize strategic planner
                if hasattr(gs, '_init_strategic_planner'):
                    gs._init_strategic_planner()
            
            # Initialize current_analysis if not present or None
            if not hasattr(self, 'current_analysis') or self.current_analysis is None:
                if hasattr(self, 'strategic_planner') and self.strategic_planner:
                    try:
                        self.current_analysis = self.strategic_planner.analyze_game_state()
                    except Exception as e:
                        logging.warning(f"Failed to generate initial game state analysis: {e}")
                        # Create a minimal analysis
                        self.current_analysis = {
                            "game_info": {
                                "turn": gs.turn,
                                "phase": gs.phase,
                                "game_stage": "early" if gs.turn <= 3 else "mid" if gs.turn <= 7 else "late"
                            },
                            "position": {
                                "overall": "even",
                                "score": 0.0
                            },
                            "life": {
                                "my_life": gs.p1["life"] if gs.agent_is_p1 else gs.p2["life"],
                                "opp_life": gs.p2["life"] if gs.agent_is_p1 else gs.p1["life"],
                                "life_diff": 0
                            }
                        }
                else:
                    # Minimal fallback analysis
                    self.current_analysis = {
                        "game_info": {
                            "turn": gs.turn,
                            "phase": gs.phase,
                            "game_stage": "early" if gs.turn <= 3 else "mid" if gs.turn <= 7 else "late"
                        },
                        "position": {
                            "overall": "even",
                            "score": 0.0
                        },
                        "life": {
                            "my_life": gs.p1["life"] if gs.agent_is_p1 else gs.p2["life"],
                            "opp_life": gs.p2["life"] if gs.agent_is_p1 else gs.p1["life"],
                            "life_diff": 0
                        }
                    }

            # Active player information
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1

            # Initialize the observation dictionary with all keys from observation_space
            obs = {k: np.zeros(space.shape, dtype=space.dtype) 
                for k, space in self.observation_space.spaces.items()}
            
            # Fill in basic observations
            obs["phase"] = gs.phase
            obs["phase_onehot"] = self._phase_to_onehot(gs.phase)
            obs["turn"] = np.array([gs.turn], dtype=np.int32)
            obs["is_my_turn"] = np.array([int(is_my_turn)], dtype=np.int32)
            obs["p1_life"] = np.array([gs.p1["life"]], dtype=np.int32)
            obs["p2_life"] = np.array([gs.p2["life"]], dtype=np.int32)
            obs["life_difference"] = np.array([me["life"] - opp["life"]], dtype=np.int32)
            obs["my_life"] = np.array([me["life"]], dtype=np.int32)
            obs["opp_life"] = np.array([opp["life"]], dtype=np.int32)

            # BATTLEFIELD observations
            my_bf = np.zeros((self.max_battlefield, FEATURE_DIM), dtype=np.float32)
            my_bf_flags = np.zeros((self.max_battlefield, 5), dtype=np.float32)
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                card = gs._safe_get_card(card_id)
                my_bf[i] = self._get_card_feature(card_id, FEATURE_DIM)
                my_bf_flags[i, 0] = float(card_id in me["tapped_permanents"])
                my_bf_flags[i, 1] = float(card_id in me["entered_battlefield_this_turn"])
                my_bf_flags[i, 2] = float(card_id in gs.current_attackers)
                my_bf_flags[i, 3] = float(any(card_id in blockers for blockers in gs.current_block_assignments.values()))
                my_bf_flags[i, 4] = float(sum(card.keywords) > 0 if card and hasattr(card, 'keywords') else 0)

            opp_bf = np.zeros((self.max_battlefield, FEATURE_DIM), dtype=np.float32)
            opp_bf_flags = np.zeros((self.max_battlefield, 5), dtype=np.float32)
            for i, card_id in enumerate(opp["battlefield"]):
                if i >= self.max_battlefield:
                    break
                card = gs._safe_get_card(card_id)
                opp_bf[i] = self._get_card_feature(card_id, FEATURE_DIM)
                opp_bf_flags[i, 0] = float(card_id in opp["tapped_permanents"])
                opp_bf_flags[i, 1] = float(card_id in opp["entered_battlefield_this_turn"])
                opp_bf_flags[i, 2] = float(card_id in gs.current_attackers)
                opp_bf_flags[i, 3] = float(any(card_id in blockers for blockers in gs.current_block_assignments.values()))
                opp_bf_flags[i, 4] = float(sum(card.keywords) > 0 if card and hasattr(card, 'keywords') else 0)

            # Assign to observation space keys
            obs["p1_battlefield"] = my_bf if gs.agent_is_p1 else opp_bf
            obs["p2_battlefield"] = opp_bf if gs.agent_is_p1 else my_bf
            obs["p1_bf_count"] = np.array([min(len(gs.p1["battlefield"]), self.max_battlefield)], dtype=np.int32)
            obs["p2_bf_count"] = np.array([min(len(gs.p2["battlefield"]), self.max_battlefield)], dtype=np.int32)
            obs["my_battlefield"] = my_bf
            obs["my_battlefield_flags"] = my_bf_flags
            obs["opp_battlefield"] = opp_bf
            obs["opp_battlefield_flags"] = opp_bf_flags
            
            # Creature stats
            my_creatures = [card_id for card_id in me["battlefield"] 
                        if gs._safe_get_card(card_id) and 
                        hasattr(gs._safe_get_card(card_id), 'card_types') and 
                        'creature' in gs._safe_get_card(card_id).card_types]

            opp_creatures = [card_id for card_id in opp["battlefield"] 
                            if gs._safe_get_card(card_id) and 
                            hasattr(gs._safe_get_card(card_id), 'card_types') and 
                            'creature' in gs._safe_get_card(card_id).card_types]
            obs["my_creature_count"] = np.array([len(my_creatures)], dtype=np.int32)
            obs["opp_creature_count"] = np.array([len(opp_creatures)], dtype=np.int32)
            my_power = sum(gs._safe_get_card(cid).power 
                        for cid in my_creatures 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))

            my_toughness = sum(gs._safe_get_card(cid).toughness 
                            for cid in my_creatures 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))

            opp_power = sum(gs._safe_get_card(cid).power 
                        for cid in opp_creatures 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))

            opp_toughness = sum(gs._safe_get_card(cid).toughness 
                            for cid in opp_creatures 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness'))
            obs["my_total_power"] = np.array([my_power], dtype=np.int32)
            obs["my_total_toughness"] = np.array([my_toughness], dtype=np.int32)
            obs["opp_total_power"] = np.array([opp_power], dtype=np.int32)
            obs["opp_total_toughness"] = np.array([opp_toughness], dtype=np.int32)
            obs["power_advantage"] = np.array([my_power - opp_power], dtype=np.int32)
            obs["toughness_advantage"] = np.array([my_toughness - opp_toughness], dtype=np.int32)
            obs["creature_advantage"] = np.array([len(my_creatures) - len(opp_creatures)], dtype=np.int32)

            # HAND information
            my_hand = np.zeros((self.max_hand_size, FEATURE_DIM), dtype=np.float32)
            hand_card_types = np.zeros((self.max_hand_size, 5), dtype=np.float32)  # [land, creature, instant, sorcery, other]
            hand_playable = np.zeros(self.max_hand_size, dtype=np.float32)
            
            # Resource efficiency calculation
            resource_efficiency = np.zeros(3, dtype=np.float32)
            
            planeswalker_activations = np.zeros((self.max_battlefield,), dtype=np.float32)
        
            # Find all planeswalkers on battlefield and their activation status
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                    # Check if this planeswalker has been activated
                    activated = card_id in me.get("activated_this_turn", set())
                    planeswalker_activations[i] = float(activated)
                    
                    # Also track in battlefield flags (existing array)
                    if activated and i < len(my_bf_flags):
                        # Use an existing flag slot for activated status
                        my_bf_flags[i, 3] = 1.0
            
            # Add to observation
            obs["planeswalker_activations"] = planeswalker_activations
            
            # Add per-planeswalker activation counts
            planeswalker_activation_counts = np.zeros((self.max_battlefield,), dtype=np.float32)
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                    
                # Get activation count for this planeswalker
                activation_count = me.get("pw_activations", {}).get(card_id, 0)
                planeswalker_activation_counts[i] = activation_count
                
            obs["planeswalker_activation_counts"] = planeswalker_activation_counts
            
            # Add strategic advice to observation
            strategic_recommendation = None
            strategy_suggestion = None

            # Get strategic advice and recommended action
            if hasattr(self, 'strategic_planner'):
                try:
                    strategic_advice = self._get_strategic_advice()
                    if strategic_advice and strategic_advice.get("recommended_action") is not None:
                        recommended_action = strategic_advice["recommended_action"]
                        if (recommended_action >= 0 and 
                            recommended_action < len(self.current_valid_actions) and 
                            self.current_valid_actions[recommended_action]):
                            strategic_recommendation = recommended_action
                            
                            # Add recommended action details
                            action_type, param = gs.action_handler.get_action_info(recommended_action)
                            obs["recommended_action"] = np.array([recommended_action], dtype=np.int32)
                            obs["recommended_action_type"] = action_type
                            obs["recommended_action_confidence"] = np.array([strategic_advice.get("position", {}).get("score", 0.5)], dtype=np.float32)
                            
                            # Add supplementary information
                            if "win_conditions" in strategic_advice:
                                primary_win_condition = None
                                primary_score = -1
                                
                                # Find primary win condition
                                for wc_name, wc_data in strategic_advice["win_conditions"].items():
                                    if wc_data.get("viable", False) and wc_data.get("score", 0) > primary_score:
                                        primary_win_condition = wc_name
                                        primary_score = wc_data.get("score", 0)
                                
                                if primary_win_condition:
                                    obs["primary_win_condition"] = primary_win_condition
                                    obs["win_condition_score"] = np.array([primary_score], dtype=np.float32)
                except Exception as e:
                    logging.warning(f"Error getting strategic recommendation: {e}")

            # Get strategy suggestion from memory
            if hasattr(self, 'strategy_memory') and self.strategy_memory:
                try:
                    strategy_suggestion = self.strategy_memory.get_suggested_action(gs, np.where(self.current_valid_actions)[0])
                    if strategy_suggestion is not None and self.current_valid_actions[strategy_suggestion]:
                        # Add to observation
                        obs["memory_suggested_action"] = np.array([strategy_suggestion], dtype=np.int32)
                        
                        # Check if this matches recommended action
                        obs["suggestion_matches_recommendation"] = np.array([int(strategy_suggestion == strategic_recommendation)], dtype=np.int32)
                except Exception as e:
                    logging.warning(f"Error getting strategy suggestion: {e}")
            
            # Add mulligan status and recommendation
            obs["mulligan_in_progress"] = np.array([1 if hasattr(gs, 'mulligan_in_progress') and gs.mulligan_in_progress else 0], dtype=np.int32)
            obs["mulligan_reasons"] = np.zeros(5, dtype=np.float32)

            # Get mulligan recommendation if in progress
            if hasattr(gs, 'mulligan_in_progress') and gs.mulligan_in_progress:
                # Only get recommendation for the current player's mulligans
                if hasattr(gs, 'mulligan_player') and gs.mulligan_player == me:
                    if hasattr(self, 'strategic_planner'):
                        try:
                            hand = me["hand"].copy()
                            decision = self.strategic_planner.suggest_mulligan_decision(
                                hand, 
                                deck_name=self.current_deck_name_p1 if gs.agent_is_p1 else self.current_deck_name_p2,
                                on_play=is_my_turn
                            )
                            obs["mulligan_recommendation"] = np.array([float(decision["keep"])], dtype=np.float32)
                            obs["mulligan_reason_count"] = np.array([min(len(decision["reasoning"]), 5)], dtype=np.int32)
                            
                            # Convert reasoning to numeric representation for observation
                            if "reasoning" in decision and decision["reasoning"]:
                                for i, reason in enumerate(decision["reasoning"][:5]):
                                    obs["mulligan_reasons"][i] = 1.0  # Mark that reason exists
                        except Exception as e:
                            logging.warning(f"Error getting mulligan recommendation: {e}")
                            obs["mulligan_recommendation"] = np.array([0.5], dtype=np.float32)
                            obs["mulligan_reason_count"] = np.array([0], dtype=np.int32)
                    else:
                        # No strategic planner to make recommendations
                        obs["mulligan_recommendation"] = np.array([0.5], dtype=np.float32)
                        obs["mulligan_reason_count"] = np.array([0], dtype=np.int32)
                else:
                    # Not this player's turn to mulligan
                    obs["mulligan_recommendation"] = np.array([0.5], dtype=np.float32)  # Neutral
                    obs["mulligan_reason_count"] = np.array([0], dtype=np.int32)
            else:
                # Mulligans not in progress
                obs["mulligan_recommendation"] = np.array([0.5], dtype=np.float32)  # Neutral
                obs["mulligan_reason_count"] = np.array([0], dtype=np.int32)
            
            # Populate resource_efficiency if possible
            try:
                # Mana efficiency: how many lands vs mana used
                available_mana = sum(me["mana_pool"].values())
                total_lands = len([cid for cid in me["battlefield"] if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).type_line])
                mana_efficiency = min(1.0, available_mana / max(1, total_lands)) if total_lands > 0 else 0.0
                
                # Card efficiency: hand size vs cards played this turn
                cards_played = len(self.cards_played.get(0, []))
                card_efficiency = min(1.0, cards_played / max(1, len(me["hand"])))
                
                # Tempo: board development relative to turn
                board_cmc = sum(gs._safe_get_card(cid).cmc for cid in me["battlefield"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'cmc'))
                tempo = min(1.0, board_cmc / max(1, gs.turn))
                
                # Ensure the array has exactly 3 elements
                resource_efficiency = np.array([mana_efficiency, card_efficiency, tempo], dtype=np.float32)
                if resource_efficiency.shape != (3,):
                    logging.warning(f"Resource efficiency shape mismatch: expected (3,), got {resource_efficiency.shape}")
                    resource_efficiency = np.zeros(3, dtype=np.float32)
            except Exception as e:
                logging.warning(f"Error populating resource_efficiency: {e}")
                # Ensure correct shape even on error
                resource_efficiency = np.zeros(3, dtype=np.float32)
            
            obs["resource_efficiency"] = resource_efficiency
            
            # Opportunity assessment for hand cards
            opportunity_assessment = np.zeros(self.max_hand_size, dtype=np.float32)
            
            # Populate opportunity_assessment if possible
            if hasattr(self, 'card_evaluator'):
                try:
                    # Use card evaluator to assess hand card opportunities
                    for i, card_id in enumerate(me["hand"]):
                        if i >= self.max_hand_size:
                            break
                        # Evaluate each card's play potential
                        opportunity = self.card_evaluator.evaluate_card(card_id, "play")
                        opportunity_assessment[i] = np.clip(opportunity, 0, 1)
                except Exception as e:
                    logging.warning(f"Error populating opportunity_assessment: {e}")
            
            obs["opportunity_assessment"] = opportunity_assessment
                        
                        # Add optimal attackers information to the observation
            optimal_attackers = np.zeros(self.max_battlefield, dtype=np.float32)
            attacker_values = np.zeros(self.max_battlefield, dtype=np.float32)

            # Fill with optimal attacker data if available
            if hasattr(gs, 'optimal_attackers') and gs.optimal_attackers:
                # Get the set of optimal attacker IDs for fast lookup
                optimal_attacker_set = set(gs.optimal_attackers)
                
                # Check if we have access to strategic planner for more detailed values
                if hasattr(gs, 'strategic_planner') and gs.strategic_planner:
                    # Evaluate each potential attacker
                    for i, card_id in enumerate(me["battlefield"]):
                        if i >= self.max_battlefield:
                            break
                            
                        # Check if this card is a creature that could attack
                        card = gs._safe_get_card(card_id)
                        is_valid_attacker = False
                        
                        if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                            # Check if it's tapped or has summoning sickness
                            is_tapped = card_id in me["tapped_permanents"]
                            has_sickness = card_id in me["entered_battlefield_this_turn"] and not (
                                hasattr(card, 'oracle_text') and "haste" in card.oracle_text.lower())
                            
                            is_valid_attacker = not is_tapped and not has_sickness
                        
                        # Mark as optimal if in the optimal set
                        if card_id in optimal_attacker_set:
                            optimal_attackers[i] = 1.0
                            
                        # Get attack value if this is a valid attacker
                        if is_valid_attacker:
                            # Try to get a strategic evaluation of this attacker
                            try:
                                # Get value of just this attacker
                                solo_value = gs.strategic_planner.evaluate_attack_action([card_id])
                                attacker_values[i] = solo_value
                            except Exception as e:
                                logging.warning(f"Error evaluating attacker {card_id}: {e}")
                                # Default to a simple power-based heuristic
                                if hasattr(card, 'power'):
                                    attacker_values[i] = min(card.power, 10.0)

            obs["optimal_attackers"] = optimal_attackers
            obs["attacker_values"] = attacker_values

            for i, card_id in enumerate(me["hand"]):
                if i >= self.max_hand_size:
                    break
                card = gs._safe_get_card(card_id)
                my_hand[i] = self._get_card_feature(card_id, FEATURE_DIM)
                
                # Add safety check before checking card properties
                if card and hasattr(card, 'type_line') and hasattr(card, 'card_types'):
                    # Make sure card is a valid Card object before checking properties
                    type_line = card.type_line if hasattr(card, 'type_line') else ""
                    card_types = card.card_types if hasattr(card, 'card_types') else []
                    
                    hand_card_types[i, 0] = float('land' in type_line)
                    hand_card_types[i, 1] = float('creature' in card_types)
                    hand_card_types[i, 2] = float('instant' in card_types)
                    hand_card_types[i, 3] = float('sorcery' in card_types)
                    hand_card_types[i, 4] = float(not any([hand_card_types[i, j] for j in range(4)]))
                    
                    if 'land' in type_line and not me["land_played"]:
                        hand_playable[i] = 1.0
                    elif 'land' not in type_line:
                        # Only call _can_afford_card if we have a valid card object
                        can_afford = hasattr(self, 'mana_system') and self.mana_system.can_pay_mana_cost(me, card) if hasattr(card, 'mana_cost') else False
                        is_main_phase = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]
                        # Rest of the logic...
                        stack_empty = len(gs.stack) == 0
                        sorcery_timing = is_main_phase and stack_empty and is_my_turn
                        if (('instant' in card_types) or ('flash' in card.name.lower() if hasattr(card, 'name') else False) or 
                            (sorcery_timing and 'sorcery' in card_types) or sorcery_timing):
                            hand_playable[i] = float(can_afford)

            obs["my_hand"] = my_hand
            obs["my_hand_count"] = np.array([len(me["hand"])], dtype=np.int32)
            obs["hand_card_types"] = hand_card_types
            obs["hand_playable"] = hand_playable

            # Add ability recommendations tensor
            ability_recommendations = np.zeros((self.max_battlefield, 5, 2), dtype=np.float32)
            for i, card_data in enumerate(active_abilities):
                if i >= self.max_battlefield:
                    break
                
                card_id = card_data["card_id"]
                abilities = card_data["abilities"]
                
                for j, ability in enumerate(abilities):
                    if j >= 5:  # Limit to 5 abilities per card
                        break
                        
                    ability_idx = ability.get("index", j)
                    
                    # Get ability recommendation if strategic planner is available
                    if hasattr(gs, 'strategic_planner') and gs.strategic_planner:
                        try:
                            recommended, confidence = gs.strategic_planner.recommend_ability_activation(card_id, ability_idx)
                            ability_recommendations[i, j, 0] = float(recommended)
                            ability_recommendations[i, j, 1] = confidence
                            
                            # Also add to the ability data for easy reference
                            ability["recommended"] = recommended
                            ability["confidence"] = confidence
                            
                        except Exception as e:
                            logging.warning(f"Error getting ability recommendation: {e}")
                            ability_recommendations[i, j, 0] = 0.0
                            ability_recommendations[i, j, 1] = 0.0
                            ability["recommended"] = False
                            ability["confidence"] = 0.0

            obs["ability_recommendations"] = ability_recommendations
            
            # Card performance ratings from the hand
            hand_performance = np.zeros(self.max_hand_size, dtype=np.float32)
            for i, card_id in enumerate(me["hand"]):
                if i >= self.max_hand_size:
                    break
                card = gs._safe_get_card(card_id)
                if card:
                    hand_performance[i] = card.performance_rating if hasattr(card, "performance_rating") else 0.5
            obs["hand_performance"] = hand_performance

            # Opponent hand count (cards remain hidden)
            obs["opp_hand_count"] = np.array([len(opp["hand"])], dtype=np.int32)

            # MANA information
            obs["my_mana_pool"] = np.array([me["mana_pool"][c] for c in ['W', 'U', 'B', 'R', 'G', 'C']], dtype=np.int32)
            obs["my_mana"] = np.array([sum(me["mana_pool"].values())], dtype=np.int32)
            obs["total_available_mana"] = np.array([sum(me["mana_pool"].values())], dtype=np.int32)
            untapped_lands = [card_id for card_id in me["battlefield"] 
                            if gs._safe_get_card(card_id) and hasattr(gs._safe_get_card(card_id), 'type_line') and 
                            'land' in gs._safe_get_card(card_id).type_line and card_id not in me["tapped_permanents"]]
            obs["untapped_land_count"] = np.array([len(untapped_lands)], dtype=np.int32)
            obs["turn_vs_mana"] = np.array([min(1.0, len(untapped_lands) / max(1, gs.turn))], dtype=np.float32)

            # STACK information
            obs["stack_count"] = np.array([len(gs.stack)], dtype=np.int32)
            stack_controller = np.zeros(5, dtype=np.int32)
            stack_card_types = np.zeros((5, 5), dtype=np.float32)
            if gs.stack:
                # Limit to last 5 stack items
                for i, item in enumerate(gs.stack[-5:]):
                    if isinstance(item, tuple) and len(item) >= 3:
                        spell_type, card_id, spell_caster = item
                        card = gs._safe_get_card(card_id)
                        
                        # Ensure we don't exceed array bounds
                        if i < 5:
                            stack_controller[i] = int(spell_caster == me)
                            if card and hasattr(card, 'card_types'):
                                card_types = card.card_types if isinstance(card.card_types, list) else [card.card_types]
                                stack_card_types[i, 0] = float('creature' in card_types)
                                stack_card_types[i, 1] = float('instant' in card_types)
                                stack_card_types[i, 2] = float('sorcery' in card_types)
                                stack_card_types[i, 3] = float(spell_type == "ABILITY")
                                stack_card_types[i, 4] = float(not any([stack_card_types[i, j] for j in range(4)]))
            
            # Make sure stack_controller has correct shape
            if stack_controller.shape != (5,):
                logging.warning(f"Stack controller shape mismatch: expected (5,), got {stack_controller.shape}")
                stack_controller = np.zeros(5, dtype=np.int32)
            
            # Make sure stack_card_types has correct shape
            if stack_card_types.shape != (5, 5):
                logging.warning(f"Stack card types shape mismatch: expected (5, 5), got {stack_card_types.shape}")
                stack_card_types = np.zeros((5, 5), dtype=np.float32)
                
            obs["stack_controller"] = stack_controller
            obs["stack_card_types"] = stack_card_types

            # GRAVEYARD information
            obs["my_graveyard_count"] = np.array([len(me["graveyard"])], dtype=np.int32)
            obs["opp_graveyard_count"] = np.array([len(opp["graveyard"])], dtype=np.int32)
            obs["graveyard_count"] = np.array([len(me["graveyard"]) + len(opp["graveyard"])], dtype=np.int32)
            my_creatures_in_gy = sum(1 for cid in me["graveyard"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') and 'creature' in gs._safe_get_card(cid).card_types)
            opp_creatures_in_gy = sum(1 for cid in opp["graveyard"] if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') and 'creature' in gs._safe_get_card(cid).card_types)
            obs["my_dead_creatures"] = np.array([my_creatures_in_gy], dtype=np.int32)
            obs["opp_dead_creatures"] = np.array([opp_creatures_in_gy], dtype=np.int32)

            # COMBAT information
            obs["attackers_count"] = np.array([len(gs.current_attackers)], dtype=np.int32)
            obs["blockers_count"] = np.array([sum(len(blockers) for blockers in gs.current_block_assignments.values())], dtype=np.int32)
            
            # Get potential damage safely
            potential_damage = 0
            try:
                if hasattr(self, 'combat_resolver') and hasattr(self.combat_resolver, 'simulate_combat'):
                    simulation = self.combat_resolver.simulate_combat()
                    if isinstance(simulation, dict) and "damage_to_player" in simulation:
                        potential_damage = simulation["damage_to_player"]
            except Exception as e:
                logging.warning(f"Error simulating combat: {e}")
            obs["potential_combat_damage"] = np.array([potential_damage], dtype=np.int32)

            # Phase history and tracking
            if not hasattr(gs, 'phase_history') or gs.phase_history.shape != (5,):
                gs.phase_history = np.zeros(5, dtype=np.int32)
            gs.phase_history = np.roll(gs.phase_history, 1)
            gs.phase_history[0] = gs.phase
            obs["phase_history"] = gs.phase_history

            # Tapped permanents tracking
            tapped_permanents = np.zeros(self.max_battlefield, dtype=bool)
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                if card_id in me["tapped_permanents"]:
                    tapped_permanents[i] = True
            obs["tapped_permanents"] = tapped_permanents

            # Remaining mana sources
            obs["remaining_mana_sources"] = np.array([len([c for c in me["battlefield"] 
                                                        if gs._safe_get_card(c) and hasattr(gs._safe_get_card(c), 'type_line') and
                                                        'land' in gs._safe_get_card(c).type_line and 
                                                        c not in me["tapped_permanents"]])], dtype=np.int32)
            
            # Use action_mask method instead of recalculating
            obs["action_mask"] = self.action_mask()

            # Add abilities tracking
            my_active_abilities = []
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                    
                # Get abilities from ability handler if available
                abilities = []
                if hasattr(gs, 'ability_handler') and gs.ability_handler:
                    abilities = gs.ability_handler.get_activated_abilities(card_id)
                    
                ability_data = []
                for j, ability in enumerate(abilities):
                    can_activate = gs.ability_handler.can_activate_ability(card_id, j, me)
                    
                    ability_info = {
                        "index": j,
                        "cost": ability.cost if hasattr(ability, 'cost') else "Unknown",
                        "effect": ability.effect if hasattr(ability, 'effect') else "Unknown",
                        "can_activate": can_activate
                    }
                    ability_data.append(ability_info)
                    
                my_active_abilities.append({
                    "card_id": card_id,
                    "card_name": card.name if hasattr(card, 'name') else "Unknown",
                    "abilities": ability_data
                })
            
            obs["my_active_abilities"] = my_active_abilities
                    
            active_abilities = []
            for i, card_id in enumerate(me["battlefield"]):
                if i >= self.max_battlefield:
                    break
                
                # Get abilities for this card
                card_abilities = []
                if hasattr(gs, 'ability_handler') and gs.ability_handler:
                    # Get activated abilities
                    activated_abilities = gs.ability_handler.get_activated_abilities(card_id)
                    for idx, ability in enumerate(activated_abilities):
                        # Track if ability can be activated
                        can_activate = gs.ability_handler.can_activate_ability(card_id, idx, me)
                        
                        # Classify ability type (using text analysis)
                        effect = ability.effect.lower() if hasattr(ability, 'effect') else ""
                        ability_type = "unknown"
                        if "draw" in effect:
                            ability_type = "card_draw"
                        elif "damage" in effect or "destroy" in effect:
                            ability_type = "removal"
                        elif "+1/+1" in effect or "gets +" in effect:
                            ability_type = "pump"
                        elif "add" in effect and any(mana in effect for mana in ["{w}", "{u}", "{b}", "{r}", "{g}", "{c}"]):
                            ability_type = "mana"
                        
                        # Add to tracked abilities
                        card_abilities.append({
                            "index": idx,
                            "can_activate": can_activate,
                            "cost": ability.cost if hasattr(ability, 'cost') else "",
                            "type": ability_type,
                            "times_used_this_turn": sum(1 for a in gs.abilities_activated_this_turn 
                                                    if a[0] == card_id and a[1] == idx) 
                                                if hasattr(gs, 'abilities_activated_this_turn') else 0
                        })
                
                # Add card with its abilities
                card = gs._safe_get_card(card_id)
                active_abilities.append({
                    "card_id": card_id,
                    "card_name": card.name if hasattr(card, 'name') else "Unknown",
                    "abilities": card_abilities
                })
            
            # Convert to array representation for the observation space
            ability_features = np.zeros((self.max_battlefield, 5), dtype=np.float32)
            for i, card_data in enumerate(active_abilities):
                if i < self.max_battlefield:
                    # Features: [ability_count, can_activate_count, mana_abilities, draw_abilities, removal_abilities]
                    abilities = card_data["abilities"]
                    ability_features[i, 0] = len(abilities)  # Total ability count
                    ability_features[i, 1] = sum(1 for a in abilities if a["can_activate"])  # Activatable count
                    ability_features[i, 2] = sum(1 for a in abilities if a["type"] == "mana")  # Mana ability count
                    ability_features[i, 3] = sum(1 for a in abilities if a["type"] == "card_draw")  # Draw ability count 
                    ability_features[i, 4] = sum(1 for a in abilities if a["type"] == "removal")  # Removal ability count
            
            obs["ability_features"] = ability_features
            obs["active_abilities"] = active_abilities  # Full data for reference
            
            # Track timing/phase appropriateness of abilities
            ability_timing = np.zeros(5, dtype=np.float32)
            current_phase = gs.phase
            
            # Determine which ability types are good in current phase
            is_combat = current_phase in [gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, gs.PHASE_COMBAT_DAMAGE]
            is_main = current_phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]
            is_end_step = current_phase in [gs.PHASE_END_STEP]
            
            # Fill timing information
            ability_timing[0] = float(is_main)  # Card draw timing
            ability_timing[1] = float(is_combat)  # Combat trick timing
            ability_timing[2] = float(is_main)  # Mana ability timing
            ability_timing[3] = float(is_combat or is_end_step)  # Removal timing
            ability_timing[4] = float(is_main and gs.turn <= 4)  # Ramp timing (early game focus)
            
            # Make sure ability_timing has correct shape
            if ability_timing.shape != (5,):
                logging.warning(f"Ability timing shape mismatch: expected (5,), got {ability_timing.shape}")
                ability_timing = np.zeros(5, dtype=np.float32)
            
            obs["ability_timing"] = ability_timing
            
            # Add card synergy scores
            my_battlefield = list(me["battlefield"])
            obs["card_synergy_scores"] = self._calculate_card_synergies(my_battlefield)
            
            # Add battlefield keyword awareness
            battlefield_keywords = np.zeros((self.max_battlefield, 15), dtype=np.float32)
            for i, card_id in enumerate(my_battlefield):
                if i >= self.max_battlefield:
                    break
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'keywords'):
                    # Fill existing keywords
                    keyword_length = min(len(card.keywords), 15)
                    battlefield_keywords[i, :keyword_length] = card.keywords[:keyword_length]
            obs["battlefield_keywords"] = battlefield_keywords
            
            # Add position advantage
            position_advantage = self._calculate_position_advantage()
            obs["position_advantage"] = np.array([position_advantage], dtype=np.float32)
            
            # Add estimated opponent hand
            obs["estimated_opponent_hand"] = self._estimate_opponent_hand()
            
            # Add key cards from graveyard
            graveyard_key_cards = np.zeros((10, FEATURE_DIM), dtype=np.float32)
            # Sort graveyard cards by importance (using CMC as a simple proxy for importance)
            graveyard_cards = []
            for cid in me["graveyard"]:
                card = gs._safe_get_card(cid)
                if card and hasattr(card, 'cmc'):
                    graveyard_cards.append((cid, card.cmc))
                else:
                    graveyard_cards.append((cid, 0))
                    
            graveyard_cards.sort(key=lambda x: x[1], reverse=True)  # Sort by CMC descending
            
            for i, (card_id, _) in enumerate(graveyard_cards[:10]):
                if i < 10:  # Ensure we don't go out of bounds
                    graveyard_key_cards[i] = self._get_card_feature(card_id, FEATURE_DIM)
            obs["graveyard_key_cards"] = graveyard_key_cards
            
            # Add exile zone information
            exile_key_cards = np.zeros((10, FEATURE_DIM), dtype=np.float32)
            exile_cards = []
            for cid in me["exile"]:
                card = gs._safe_get_card(cid)
                if card and hasattr(card, 'cmc'):
                    exile_cards.append((cid, card.cmc))
                else:
                    exile_cards.append((cid, 0))
                    
            exile_cards.sort(key=lambda x: x[1], reverse=True)  # Sort by CMC descending
            
            for i, (card_id, _) in enumerate(exile_cards[:10]):
                if i < 10:  # Ensure we don't go out of bounds
                    exile_key_cards[i] = self._get_card_feature(card_id, FEATURE_DIM)
            obs["exile_key_cards"] = exile_key_cards
            
            # Add action and reward memory
            if not hasattr(self, 'last_n_actions'):
                self.last_n_actions = np.zeros(self.action_memory_size, dtype=np.int32)
                self.last_n_rewards = np.zeros(self.action_memory_size, dtype=np.float32)
            
            obs["previous_actions"] = self.last_n_actions
            obs["previous_rewards"] = self.last_n_rewards
            
            # STRATEGIC METRICS
            strategic_metrics = np.zeros(10, dtype=np.float32)
            if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'current_analysis'):
                analysis = self.strategic_planner.current_analysis
                if analysis is not None:  # Add this check to prevent NoneType error
                    # Board advantage
                    if 'board_state' in analysis:
                        strategic_metrics[0] = np.tanh(analysis['board_state'].get('board_advantage', 0))
                    # Card advantage
                    if 'resources' in analysis:
                        strategic_metrics[1] = np.tanh(analysis['resources'].get('card_advantage', 0) / 3)
                    # Mana advantage
                    if 'resources' in analysis:
                        strategic_metrics[2] = np.tanh(analysis['resources'].get('mana_advantage', 0) / 2)
                    # Life advantage
                    if 'life' in analysis:
                        strategic_metrics[3] = np.tanh(analysis['life'].get('life_diff', 0) / 10)
                    # Tempo advantage
                    if 'tempo' in analysis:
                        strategic_metrics[4] = np.tanh(analysis['tempo'].get('tempo_advantage', 0))
                    # Overall position score
                    if 'position' in analysis:
                        strategic_metrics[5] = np.tanh(analysis['position'].get('score', 0))
                    # Game stage indicator (early=0, mid=0.5, late=1)
                    if 'game_info' in analysis:
                        stage = analysis['game_info'].get('game_stage', 'mid')
                        strategic_metrics[6] = 0.0 if stage == 'early' else 0.5 if stage == 'mid' else 1.0
                    # Combat potential
                    strategic_metrics[7] = np.tanh(my_power - opp_power)
                    # Resource utilization
                    if sum(me["mana_pool"].values()) > 0:
                        strategic_metrics[8] = -1  # Penalty for unused mana
                    # Hand playability
                    playable_count = np.sum(hand_playable)
                    strategic_metrics[9] = playable_count / max(1, len(me["hand"]))
                
            obs["strategic_metrics"] = strategic_metrics

            # Deck composition estimate
            deck_composition = np.zeros(6, dtype=np.float32)
            if hasattr(self, 'strategic_planner'):
                # Estimate remaining deck composition
                all_cards = me["library"] + me["hand"] + me["battlefield"] + me["graveyard"]
                card_objects = [gs._safe_get_card(cid) for cid in all_cards]
                card_objects = [c for c in card_objects if c]
                
                # Count by card type
                type_counts = defaultdict(int)
                for card in card_objects:
                    if hasattr(card, 'card_types'):
                        for card_type in card.card_types:
                            type_counts[card_type] += 1
                
                # Calculate ratios for main card types
                total_cards = len(card_objects)
                if total_cards > 0:
                    deck_composition[0] = type_counts.get('creature', 0) / total_cards  # Creature ratio
                    deck_composition[1] = type_counts.get('instant', 0) / total_cards   # Instant ratio
                    deck_composition[2] = type_counts.get('sorcery', 0) / total_cards   # Sorcery ratio
                    deck_composition[3] = type_counts.get('artifact', 0) / total_cards  # Artifact ratio
                    deck_composition[4] = type_counts.get('enchantment', 0) / total_cards  # Enchantment ratio
                    deck_composition[5] = type_counts.get('land', 0) / total_cards      # Land ratio
                    
            obs["deck_composition_estimate"] = deck_composition

            # Threat assessment of opponent's permanents
            threat_assessment = np.zeros(self.max_battlefield, dtype=np.float32)
            for i, card_id in enumerate(opp["battlefield"]):
                if i >= self.max_battlefield:
                    break
                card = gs._safe_get_card(card_id)
                threat_level = 0
                
                if card and hasattr(card, 'card_types'):
                    # Base threat for creatures
                    if 'creature' in card.card_types:
                        if hasattr(card, 'power'):
                            threat_level += card.power * 1.5
                        
                        # Check keyword abilities
                        if hasattr(card, 'oracle_text'):
                            text = card.oracle_text.lower()
                            if "flying" in text: threat_level += 1
                            if "trample" in text: threat_level += 1
                            if "deathtouch" in text: threat_level += 2
                            if "lifelink" in text: threat_level += 1
                            if "double strike" in text: threat_level += 3
                    
                    # Planeswalkers are high threat
                    elif 'planeswalker' in card.card_types:
                        threat_level = 8
                        
                    # Other permanent types
                    elif 'artifact' in card.card_types or 'enchantment' in card.card_types:
                        threat_level = 3
                
                threat_assessment[i] = min(10.0, threat_level)  # Cap at 10
                
            obs["threat_assessment"] = threat_assessment

            if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'predict_opponent_archetype'):
                try:
                    # Ensure exactly 4 dimensions by truncating or padding
                    opp_archetype_probs = self.strategic_planner.predict_opponent_archetype()
                    if len(opp_archetype_probs) > 4:
                        opp_archetype_probs = opp_archetype_probs[:4]
                    elif len(opp_archetype_probs) < 4:
                        # Pad with zeros if less than 4
                        padded_probs = np.zeros(4, dtype=np.float32)
                        padded_probs[:len(opp_archetype_probs)] = opp_archetype_probs
                        opp_archetype_probs = padded_probs
                    
                    obs["opponent_archetype"] = np.array(opp_archetype_probs, dtype=np.float32)
                except Exception as e:
                    logging.warning(f"Error predicting opponent archetype: {e}")
                    obs["opponent_archetype"] = np.array([0.25, 0.25, 0.25, 0.25], dtype=np.float32)

            # NEW: Add future state projections
            if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'project_future_states'):
                try:
                    future_projections = self.strategic_planner.project_future_states(num_turns=5)
                    obs["future_state_projections"] = future_projections
                except Exception as e:
                    logging.warning(f"Error projecting future states: {e}")
                    obs["future_state_projections"] = np.zeros(5, dtype=np.float32)

            # NEW: Add multi-turn plan information
            if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'plan_multi_turn_sequence'):
                try:
                    turn_plans = self.strategic_planner.plan_multi_turn_sequence(depth=2)
                    # Convert complex plan structure to numeric representation
                    plan_metrics = np.zeros(6, dtype=np.float32)
                    
                    # Total plays planned
                    total_plays = sum(len(plan["plays"]) for plan in turn_plans)
                    plan_metrics[0] = min(1.0, total_plays / 10)  # Normalize planned play count
                    
                    # Land drops planned
                    land_plays = sum(1 for plan in turn_plans 
                                for play in plan["plays"] if play["type"] == "land")
                    plan_metrics[1] = min(1.0, land_plays / 2)  # Normalize land plays
                    
                    # Spells planned by CMC
                    low_cmc_spells = sum(1 for plan in turn_plans 
                                    for play in plan["plays"] 
                                    if play["type"] == "spell" and play["card"].cmc <= 3)
                    high_cmc_spells = sum(1 for plan in turn_plans 
                                        for play in plan["plays"] 
                                        if play["type"] == "spell" and play["card"].cmc > 3)
                    
                    plan_metrics[2] = min(1.0, low_cmc_spells / 5)  # Low CMC spell ratio
                    plan_metrics[3] = min(1.0, high_cmc_spells / 3)  # High CMC spell ratio
                    
                    # Expected mana curve
                    if turn_plans:
                        plan_metrics[4] = min(1.0, turn_plans[0]["expected_mana"] / 10)  # Current expected mana
                        if len(turn_plans) > 1:
                            plan_metrics[5] = min(1.0, turn_plans[1]["expected_mana"] / 10)  # Next turn expected mana
                    
                    obs["multi_turn_plan"] = plan_metrics
                    
                except Exception as e:
                    logging.warning(f"Error creating multi-turn plan metrics: {e}")
                    obs["multi_turn_plan"] = np.zeros(6, dtype=np.float32)

            if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'identify_card_synergies'):
                try:
                    # Analyze hand synergies with battlefield
                    hand_synergy_scores = np.zeros(self.max_hand_size, dtype=np.float32)
                    for i, card_id in enumerate(me["hand"]):
                        if i >= self.max_hand_size:
                            break
                        synergy_score = self.strategic_planner.identify_card_synergies(
                            card_id, me["hand"], me["battlefield"])
                        
                        # Ensure synergy score is a float and within [0, 1] range
                        hand_synergy_scores[i] = max(0.0, min(1.0, float(synergy_score)))
                    
                    obs["hand_synergy_scores"] = hand_synergy_scores
                except Exception as e:
                    logging.warning(f"Error calculating card synergies: {e}")
                    obs["hand_synergy_scores"] = np.zeros(self.max_hand_size, dtype=np.float32)


            if hasattr(self, 'strategic_planner') and self.strategic_planner and hasattr(self.strategic_planner, 'current_analysis'):
                win_conditions = self.strategic_planner.current_analysis
                if win_conditions is not None:
                    win_conditions = win_conditions.get("win_conditions", {})
                    
                    win_condition_viability = np.zeros(6, dtype=np.float32)
                    win_condition_timings = np.ones(6, dtype=np.float32) * 99  # Initialize with high values
                    
                    for i, condition in enumerate(["combat_damage", "card_advantage", "combo", "control", "mill", "alternate"]):
                        if condition in win_conditions:
                            win_condition_viability[i] = float(win_conditions[condition].get("viable", False))
                            if win_conditions[condition].get("viable", False):
                                win_condition_timings[i] = min(20, win_conditions[condition].get("turns_to_win", 99))
                    
                    obs["win_condition_viability"] = win_condition_viability
                    obs["win_condition_timings"] = win_condition_timings

            # Cache this observation
            self._cached_obs = obs
            self._cached_obs_turn = gs.turn
            self._cached_obs_phase = gs.phase
            self._cached_battlefield_count_p1 = len(gs.p1["battlefield"])
            self._cached_battlefield_count_p2 = len(gs.p2["battlefield"])
            self._cached_hand_count_p1 = len(gs.p1["hand"])
            self._cached_hand_count_p2 = len(gs.p2["hand"])
            self._cached_life_p1 = gs.p1["life"]
            self._cached_life_p2 = gs.p2["life"]
            self._cached_stack_size = len(gs.stack)
            self._cached_attackers_count = len(gs.current_attackers)

            return obs
        except Exception as e:
            logging.error(f"Error generating observation: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Create a minimal valid observation as fallback
            fallback_obs = {k: np.zeros(space.shape, dtype=space.dtype) 
                    for k, space in self.observation_space.spaces.items()}
            
            # Set minimal valid values
            fallback_obs["phase"] = gs.phase
            fallback_obs["turn"] = np.array([gs.turn], dtype=np.int32)
            fallback_obs["p1_life"] = np.array([gs.p1["life"]], dtype=np.int32)
            fallback_obs["p2_life"] = np.array([gs.p2["life"]], dtype=np.int32)
            
            return fallback_obs

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
