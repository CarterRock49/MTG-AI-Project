import random
import logging
import numpy as np
import gymnasium as gym
from gymnasium import spaces
from .card import load_decks_and_card_db, Card
from .game_state import GameState
from .actions import ActionHandler
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import integrate_combat_actions, apply_combat_action
from .enhanced_mana_system import EnhancedManaSystem
from .enhanced_card_evaluator import EnhancedCardEvaluator
from .strategic_planner import MTGStrategicPlanner
from .debug import DEBUG_MODE
import time
from .strategy_memory import StrategyMemory
from collections import defaultdict
from .layer_system import LayerSystem
from .replacement_effects import ReplacementEffectSystem
from .deck_stats_tracker import DeckStatsCollector
from .strategy_memory import StrategyMemory
import asyncio
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
        
        # Define observation space
        self.observation_space = spaces.Dict({
            # Add to the observation_space dictionary in __init__
            "recommended_action": spaces.Box(low=0, high=480, shape=(1,), dtype=np.int32),
            "recommended_action_confidence": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "memory_suggested_action": spaces.Box(low=0, high=480, shape=(1,), dtype=np.int32),
            "suggestion_matches_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "optimal_attackers": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "attacker_values": spaces.Box(low=-10, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activations": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=np.float32),
            "planeswalker_activation_counts": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "ability_recommendations": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5, 2), dtype=np.float32),
            "phase": spaces.Discrete(MAX_PHASE + 1),
            "mulligan_in_progress": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "mulligan_recommendation": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "mulligan_reason_count": spaces.Box(low=0, high=5, shape=(1,), dtype=np.int32),
            "mulligan_reasons": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "phase_onehot": spaces.Box(low=0, high=1, shape=(MAX_PHASE + 1,), dtype=np.float32),
            "turn": spaces.Box(low=0, high=self.max_turns, shape=(1,), dtype=np.int32),
            "p1_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p2_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "p1_battlefield": spaces.Box(low=0, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "p2_battlefield": spaces.Box(low=0, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "p1_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "p2_bf_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "my_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "my_mana": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "my_mana_pool": spaces.Box(low=0, high=100, shape=(6,), dtype=np.int32),
            "my_hand": spaces.Box(low=0, high=50, shape=(self.max_hand_size, FEATURE_DIM), dtype=np.float32),
            "my_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "action_mask": spaces.Box(low=0, high=1, shape=(480,), dtype=bool),
            "stack_count": spaces.Box(low=0, high=20, shape=(1,), dtype=np.int32),
            "my_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "hand_playable": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "hand_performance": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "graveyard_count": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "tapped_permanents": spaces.Box(low=0, high=1, shape=(self.max_battlefield,), dtype=bool),
            "phase_history": spaces.Box(low=0, high=self.game_state.PHASE_TARGETING, shape=(5,), dtype=np.int32),
            "remaining_mana_sources": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "is_my_turn": spaces.Box(low=0, high=1, shape=(1,), dtype=np.int32),
            "life_difference": spaces.Box(low=-40, high=40, shape=(1,), dtype=np.int32),
            "opp_life": spaces.Box(low=0, high=40, shape=(1,), dtype=np.int32),
            "card_synergy_scores": spaces.Box(low=-1, high=1, shape=(self.max_battlefield, self.max_battlefield), dtype=np.float32),
            "graveyard_key_cards": spaces.Box(low=0, high=1, shape=(10, FEATURE_DIM), dtype=np.float32),
            "exile_key_cards": spaces.Box(low=0, high=1, shape=(10, FEATURE_DIM), dtype=np.float32),
            "battlefield_keywords": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 15), dtype=np.float32),
            "position_advantage": spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32),
            "estimated_opponent_hand": spaces.Box(low=0, high=1, shape=(self.max_hand_size, FEATURE_DIM), dtype=np.float32),
            "strategic_metrics": spaces.Box(low=-1, high=1, shape=(10,), dtype=np.float32),
            "deck_composition_estimate": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "threat_assessment": spaces.Box(low=0, high=10, shape=(self.max_battlefield,), dtype=np.float32),
            "opportunity_assessment": spaces.Box(low=0, high=10, shape=(self.max_hand_size,), dtype=np.float32),
            "resource_efficiency": spaces.Box(low=0, high=1, shape=(3,), dtype=np.float32),
            
            # Additional observations returned by _get_obs but not defined in observation_space
            "my_battlefield": spaces.Box(low=0, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
            "my_battlefield_flags": spaces.Box(low=0, high=1, shape=(self.max_battlefield, 5), dtype=np.float32),
            "opp_battlefield": spaces.Box(low=0, high=50, shape=(self.max_battlefield, FEATURE_DIM), dtype=np.float32),
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
            "hand_card_types": spaces.Box(low=0, high=1, shape=(self.max_hand_size, 5), dtype=np.float32),
            "opp_hand_count": spaces.Box(low=0, high=self.max_hand_size, shape=(1,), dtype=np.int32),
            "total_available_mana": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "untapped_land_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "turn_vs_mana": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            "stack_controller": spaces.Box(low=0, high=1, shape=(5,), dtype=np.int32),
            "stack_card_types": spaces.Box(low=0, high=1, shape=(5, 5), dtype=np.float32),
            "my_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "opp_dead_creatures": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "attackers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "blockers_count": spaces.Box(low=0, high=self.max_battlefield, shape=(1,), dtype=np.int32),
            "potential_combat_damage": spaces.Box(low=0, high=100, shape=(1,), dtype=np.int32),
            "ability_features": spaces.Box(low=0, high=10, shape=(self.max_battlefield, 5), dtype=np.float32),
            "ability_timing": spaces.Box(low=0, high=1, shape=(5,), dtype=np.float32),
            "previous_actions": spaces.Box(low=0, high=480, shape=(self.action_memory_size,), dtype=np.int32),
            "previous_rewards": spaces.Box(low=-10, high=10, shape=(self.action_memory_size,), dtype=np.float32),
            "hand_synergy_scores": spaces.Box(low=0, high=1, shape=(self.max_hand_size,), dtype=np.float32),
            "opponent_archetype": spaces.Box(low=0, high=1, shape=(4,), dtype=np.float32),
            "future_state_projections": spaces.Box(low=-1, high=1, shape=(5,), dtype=np.float32),
            "multi_turn_plan": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_viability": spaces.Box(low=0, high=1, shape=(6,), dtype=np.float32),
            "win_condition_timings": spaces.Box(low=0, high=20, shape=(6,), dtype=np.float32),
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
        try:
            from Playersim.debug import log_reset  # Import at function level to avoid circular imports
            
            # Get environment ID for tracking
            env_id = getattr(self, "env_id", id(self))
            log_reset(env_id)
            
            # Ensure we don't reset multiple times in succession
            if hasattr(self, '_last_reset_time'):
                current_time = time.time()
                time_since_last_reset = current_time - self._last_reset_time
                if time_since_last_reset < 0.1:  # Less than 100ms since last reset
                    logging.warning(f"Multiple resets detected within {time_since_last_reset:.3f}s - investigating potential loop")
                    stack = traceback.format_stack()
                    logging.debug(f"Reset stack: {''.join(stack[-5:])}")
            
            self._last_reset_time = time.time()
            
            # Original reset code
            super().reset(seed=seed)
            
            # Reset episode metrics
            self.current_step = 0
            self.invalid_action_count = 0
            self.episode_rewards = []
            self.episode_invalid_actions = 0
            self.current_episode_actions = []
            
            # Reset cards played tracking
            self.cards_played = {0: [], 1: []}
            
            # Choose random decks
            p1_deck = random.choice(self.decks)
            p2_deck = random.choice(self.decks)
            self.current_deck_name_p1 = p1_deck["name"]
            self.current_deck_name_p2 = p2_deck["name"]
            # Reset turn tracking
            self.spells_cast_this_turn = []
            self.attackers_this_turn = set()
            self.damage_dealt_this_turn = {}
            self.cards_drawn_this_turn = {"p1": 0, "p2": 0}
            self.until_end_of_turn_effects = {}
            
            # Reset planeswalker activations
            for player in [self.game_state.p1, self.game_state.p2]:
                player["activated_this_turn"] = set()
                player["pw_activations"] = {}
            # Reset game state
            self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
            self.game_state.reset(p1_deck["cards"], p2_deck["cards"], seed)
            self.original_p1_deck = p1_deck["cards"].copy()
            self.original_p2_deck = p2_deck["cards"].copy()
            
            # Pass the stats tracker to the game state
            if self.has_stats_tracker:
                self.game_state.stats_tracker = self.stats_tracker
                self.stats_tracker.current_deck_name_p1 = self.current_deck_name_p1
                self.stats_tracker.current_deck_name_p2 = self.current_deck_name_p2
            
            # Sequential initialization with clear dependencies
            self.action_handler = ActionHandler(self.game_state)
            self.initialize_strategic_memory()
            self.game_state._init_rules_systems()
            
            # Register common continuous effects
            if hasattr(self.game_state, 'replacement_effects'):
                self.game_state.replacement_effects.register_common_effects()
            
            # Initialize targeting system
            self.game_state.initialize_targeting_system()
            
            # Initialize enhanced systems
            try:
                # Create mana system first (no dependencies)
                if hasattr(self, 'strategy_memory'):
                    self.game_state.strategy_memory = self.strategy_memory
                    
                from .enhanced_mana_system import EnhancedManaSystem
                self.mana_system = EnhancedManaSystem(self.game_state)
                self.game_state.mana_system = self.mana_system
                self.mana_system.mana_symbols = {'W', 'U', 'B', 'R', 'G', 'C'}
                
                # Combat resolver (doesn't depend on mana system)
                from .enhanced_combat import ExtendedCombatResolver
                self.combat_resolver = ExtendedCombatResolver(self.game_state)
                self.game_state.combat_resolver = self.combat_resolver
                # Integrate combat actions
                integrate_combat_actions(self.game_state)
                # Card evaluator (depends on stats_tracker but not other components)
                from .enhanced_card_evaluator import EnhancedCardEvaluator
                self.card_evaluator = EnhancedCardEvaluator(
                    self.game_state, 
                    self.stats_tracker if self.has_stats_tracker else None,
                    self.card_memory if self.has_card_memory else None
                )
                self.game_state.card_evaluator = self.card_evaluator
                
                # Strategic planner (depends on all other components)
                from .strategic_planner import MTGStrategicPlanner
                self.strategic_planner = MTGStrategicPlanner(
                    self.game_state, 
                    self.card_evaluator, 
                    self.combat_resolver
                )
                self.game_state.strategic_planner = self.strategic_planner
                
                if hasattr(self, 'strategy_memory') and self.strategic_planner:
                    self.strategic_planner.strategy_memory = self.strategy_memory
                    
                # Initialize mulligan state (but don't make decisions automatically)
                gs = self.game_state
                gs.mulligan_in_progress = True
                gs.mulligan_player = gs.p1  # Start with P1's mulligan decision
                
                # Ensure P2's mulligan will happen after P1 finishes
                if not hasattr(gs, 'next_mulligan_player'):
                    gs.next_mulligan_player = gs.p2
                
                # Analyze initial game state if strategic planner is initialized
                if hasattr(self, 'strategic_planner'):
                    self.strategic_planner.analyze_game_state()
                    
                logging.debug("Enhanced MTG components initialized successfully")
            except Exception as e:
                logging.warning(f"Could not initialize enhanced components: {e}")
                logging.warning(f"Error details: {str(e)}")
                # Fall back to standard components
                from .enhanced_combat import ExtendedCombatResolver
                self.combat_resolver = ExtendedCombatResolver(self.game_state)
                self.game_state.combat_resolver = self.combat_resolver
                # Integrate combat actions
                integrate_combat_actions(self.game_state, self.action_handler)
            # Generate valid actions
            self.current_valid_actions = self.action_handler.generate_valid_actions()
            
            # Get observation
            obs = self._get_obs()
            info = {"action_mask": self.current_valid_actions.astype(bool)}
            
            logging.debug(f"Environment {env_id} reset complete. Starting new episode.")
            return obs, info
        except Exception as e:
            logging.error(f"Error in reset method: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Emergency reset - create minimal valid state
            self.game_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
            
            # Choose any decks
            if not self.decks or len(self.decks) == 0:
                # Create simple backup deck
                backup_deck = [0] * 60
            else:
                backup_deck = self.decks[0] if len(self.decks) > 0 else [0] * 60
                
            self.game_state.reset(backup_deck, backup_deck, seed)
            
            # Minimal valid actions - just END_TURN
            self.current_valid_actions = np.zeros(480, dtype=bool)
            self.current_valid_actions[0] = True
            
            # Create minimal observation
            try:
                obs = self._get_obs()
            except:
                obs = {k: np.zeros(space.shape, dtype=space.dtype) 
                    for k, space in self.observation_space.spaces.items()}
            
            info = {"action_mask": self.current_valid_actions, 
                    "error_reset": True}
                    
            logging.info("Emergency reset completed with minimal state")
            return obs, info
        
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
    
    def step(self, action_idx):
        """
        Execute the action and get the next observation, reward and done status.
        
        Args:
            action_idx: Index of the action to execute
                    
        Returns:
            tuple: (observation, reward, done, truncated, info)
        """
        try:
            from Playersim.debug import DEBUG_ACTION_STEPS, log_exception
            
            info = {}
            gs = self.game_state
            current_turn = gs.turn  # Store the current turn
            current_phase = gs.phase  # Store the current phase
            
            # Store previous life totals to track changes
            prev_player_life = getattr(self, 'prev_player_life', None)
            prev_opponent_life = getattr(self, 'prev_opponent_life', None)
            
            # Get current player and opponent references
            me = gs.p1 if gs.agent_is_p1 else gs.p2
            opp = gs.p2 if gs.agent_is_p1 else gs.p1
            
            pre_action_pattern = None
            
            # Extract strategy pattern before action
            if hasattr(self, 'strategy_memory'):
                try:
                    pre_action_pattern = self.strategy_memory.extract_strategy_pattern(gs)
                    logging.debug(f"Pre-action strategy pattern: {pre_action_pattern}")
                except Exception as e:
                    log_exception(e, "Error extracting pre-action strategy pattern")
            
            if DEBUG_ACTION_STEPS:
                logging.debug(f"== STEP START: Action {action_idx} at TURN {current_turn}: Phase {current_phase} ==")
                logging.debug(f"P1 Life = {gs.p1['life']}, P2 Life = {gs.p2['life']}")
            
            # Check for phase progress to avoid getting stuck
            self._check_phase_progress()
            
            # AUTOMATIC PHASE HANDLING SECTION
            # UNTAP PHASE: if the environment is in the untap phase, process it automatically.
            if gs.phase == gs.PHASE_UNTAP:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    current_player = gs.p1 if gs.agent_is_p1 else gs.p2
                    gs._untap_phase(current_player)
                    # Transition automatically to the draw phase
                    gs.phase = gs.PHASE_DRAW
                    
                    # Check if turn has changed during automatic phase handling
                    if gs.turn != pre_phase_turn:
                        logging.warning(f"Turn changed during automatic UNTAP phase handling: {pre_phase_turn} -> {gs.turn}")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, f"Error generating valid actions during UNTAP phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[2] = True  # Enable DRAW_NEXT
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug("Automatic untap phase executed; transitioning to DRAW phase.")
                    logging.debug(f"Post-untap state: Mana pool: {current_player['mana_pool']}, Hand count: {len(current_player['hand'])}, Battlefield count: {len(current_player['battlefield'])}")
                    # Return a zero reward step
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic UNTAP phase processing")
                    # Continue with normal step logic instead of failing
            
            # BEGINNING_OF_COMBAT PHASE: transition automatically
            elif gs.phase == gs.PHASE_BEGINNING_OF_COMBAT:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    # Automatic advancement
                    gs.phase = gs.PHASE_BEGIN_COMBAT
                    
                    # Check if turn has changed during automatic phase handling
                    if gs.turn != pre_phase_turn:
                        logging.warning(f"Turn changed during automatic BEGINNING_OF_COMBAT phase handling: {pre_phase_turn} -> {gs.turn}")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, "Error generating valid actions during BEGINNING_OF_COMBAT phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[8] = True  # Enable BEGIN_COMBAT_END
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug("Automatic phase transition to BEGIN_COMBAT")
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic BEGINNING_OF_COMBAT phase processing")
                    # Continue with normal logic
            
            # END_OF_COMBAT PHASE: transition automatically
            elif gs.phase == gs.PHASE_END_OF_COMBAT:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    # Automatic advancement  
                    gs.phase = gs.PHASE_END_COMBAT
                    
                    # Check if turn has changed during automatic phase handling
                    if gs.turn != pre_phase_turn:
                        logging.warning(f"Turn changed during automatic END_OF_COMBAT phase handling: {pre_phase_turn} -> {gs.turn}")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, "Error generating valid actions during END_OF_COMBAT phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[9] = True  # Enable END_COMBAT
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug("Automatic phase transition to END_COMBAT")
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic END_OF_COMBAT phase processing")
                    # Continue with normal logic
            
            # CLEANUP PHASE: transition automatically
            elif gs.phase == gs.PHASE_CLEANUP:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    # Process cleanup effects
                    current_player = gs.p1 if gs.agent_is_p1 else gs.p2
                    gs._end_phase(current_player)
                    
                    # Discard to hand size
                    if len(current_player["hand"]) > 7:
                        discard_count = len(current_player["hand"]) - 7
                        logging.debug(f"Cleanup: Discarding {discard_count} cards to hand size")
                        
                        # Use card evaluator for better discard decisions if available
                        if hasattr(self, 'card_evaluator'):
                            # Sort cards by discard value (lower is better to discard)
                            discard_values = [(i, self.card_evaluator.evaluate_card(card_id, "discard")) 
                                            for i, card_id in enumerate(current_player["hand"])]
                            discard_values.sort(key=lambda x: x[1])
                            
                            # Discard the worst cards
                            for i in range(discard_count):
                                if i < len(discard_values):
                                    idx_to_discard = discard_values[i][0]
                                    discard_id = current_player["hand"].pop(idx_to_discard)
                                    current_player["graveyard"].append(discard_id)
                                    card = gs._safe_get_card(discard_id)
                                    logging.debug(f"Discarded {card.name if card else discard_id}")
                        else:
                            # Simple discard logic as before
                            for _ in range(discard_count):
                                min_value = float('inf')
                                worst_card_idx = 0
                                for i, card_id in enumerate(current_player["hand"]):
                                    card = gs._safe_get_card(card_id)
                                    if card and hasattr(card, 'cmc'):
                                        value = card.cmc  # Simple heuristic - higher cost cards are kept
                                        if value < min_value:
                                            min_value = value
                                            worst_card_idx = i
                                if current_player["hand"]:
                                    discard_id = current_player["hand"].pop(worst_card_idx)
                                    current_player["graveyard"].append(discard_id)
                    
                    # Remove damage from all permanents
                    for player in [gs.p1, gs.p2]:
                        player["damage_counters"] = {}
                    
                    # Remove "until end of turn" effects
                    for player in [gs.p1, gs.p2]:
                        if hasattr(player, "temp_buffs"):
                            player["temp_buffs"] = {k: v for k, v in player["temp_buffs"].items() 
                                                if not v.get("until_end_of_turn", False)}
                    
                    # Advance to next turn
                    prev_turn = gs.turn
                    gs.phase = gs.PHASE_UNTAP
                    gs.turn += 1
                    gs.combat_damage_dealt = False  # Reset for new turn
                    
                    # Check for unusual turn jump
                    if gs.turn != prev_turn + 1:
                        logging.warning(f"Unusual turn advancement detected in CLEANUP: {prev_turn} -> {gs.turn}")
                    else:
                        logging.info(f"=== ADVANCING FROM TURN {prev_turn} TO TURN {gs.turn} ===")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, "Error generating valid actions during CLEANUP phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[1] = True  # Enable UNTAP_NEXT
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug(f"Automatic CLEANUP phase executed; advancing to Turn {gs.turn}.")
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic CLEANUP phase processing")
                    # Continue with normal logic
            
            # Combat damage phase transitions
            elif gs.phase == gs.PHASE_COMBAT_DAMAGE and gs.combat_damage_dealt:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    # Automatically advance to end of combat after damage is dealt
                    gs.phase = gs.PHASE_END_OF_COMBAT
                    
                    # Check if turn has changed during automatic phase handling
                    if gs.turn != pre_phase_turn:
                        logging.warning(f"Turn changed during automatic COMBAT_DAMAGE phase handling: {pre_phase_turn} -> {gs.turn}")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, "Error generating valid actions during COMBAT_DAMAGE phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[10] = True  # Enable END_OF_COMBAT action
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug("Automatic transition to END_OF_COMBAT after damage dealt")
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic COMBAT_DAMAGE phase processing")
                    # Continue with normal logic
            
            # FIRST_STRIKE_DAMAGE PHASE: handle automatically if present
            elif gs.phase == gs.PHASE_FIRST_STRIKE_DAMAGE:
                try:
                    # Track current turn before automatic phase handling
                    pre_phase_turn = gs.turn
                    
                    # Process first strike damage using enhanced combat resolver if available
                    if hasattr(self, 'combat_resolver') and hasattr(self.combat_resolver, 'resolve_first_strike_damage'):
                        self.combat_resolver.resolve_first_strike_damage()
                    
                    # Advance to regular damage phase
                    gs.phase = gs.PHASE_COMBAT_DAMAGE
                    
                    # Check if turn has changed during automatic phase handling
                    if gs.turn != pre_phase_turn:
                        logging.warning(f"Turn changed during automatic FIRST_STRIKE_DAMAGE phase handling: {pre_phase_turn} -> {gs.turn}")
                    
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        log_exception(e, "Error generating valid actions during FIRST_STRIKE_DAMAGE phase")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[4] = True  # Enable COMBAT_DAMAGE
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    logging.debug("Automatic first strike damage processed; transitioning to regular COMBAT_DAMAGE")
                    return obs, 0.0, False, False, info
                except Exception as e:
                    log_exception(e, "Error in automatic FIRST_STRIKE_DAMAGE phase processing")
                    # Continue with normal logic
            
            # After attackers are declared but before blockers phase
            if gs.phase == gs.PHASE_DECLARE_ATTACKERS and len(gs.current_attackers) > 0:
                # Find optimal blocks for AI opponent
                try:
                    if hasattr(self, 'combat_resolver') and hasattr(self.combat_resolver, 'evaluate_potential_blocks'):
                        # Use the enhanced combat evaluation
                        block_assignments = {}
                        
                        for attacker_id in gs.current_attackers:
                            potential_blockers = [cid for cid in opp["battlefield"] 
                                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') and 
                                                'creature' in gs._safe_get_card(cid).card_types]
                            
                            # Get potential blocks for this attacker
                            block_options = self.combat_resolver.evaluate_potential_blocks(attacker_id, potential_blockers)
                            
                            # Choose the best option with value above threshold
                            if block_options and block_options[0]['value'] > 0:
                                block_assignments[attacker_id] = block_options[0]['blocker_ids']
                        
                        gs.current_block_assignments = block_assignments
                        logging.debug(f"AI computed optimal blocks: {block_assignments}")
                    else:
                        # Use the original function
                        optimal_blocks = self.action_handler.find_optimal_blocks()
                        gs.current_block_assignments = optimal_blocks
                        logging.debug(f"AI computed basic blocks: {optimal_blocks}")
                except Exception as e:
                    log_exception(e, "Error computing optimal blocks")
                    # Continue without blocking if there's an error
            
            # Apply layer effects if needed
            if hasattr(gs, 'layer_system') and gs.layer_system:
                try:
                    gs.layer_system.apply_all_effects()
                except Exception as e:
                    log_exception(e, "Error applying layer effects")
            
            # NORMAL ACTION PROCESSING
            self.current_step += 1
            logging.debug(f"Processing action index: {action_idx}")
            
            # Get action info with error handling
            try:
                action_type, param = self.action_handler.get_action_info(action_idx)
                logging.debug(f"Attempting action: {action_type} ({action_idx}) with param {param}")
            except Exception as e:
                log_exception(e, f"Error getting action info for action {action_idx}")
                # Default to safe values
                action_type, param = "INVALID", None
            
            # Check action validity
            if action_idx >= len(self.current_valid_actions) or not self.current_valid_actions[action_idx]:
                logging.debug(f"Invalid action index {action_idx} selected; applying penalty.")
                reward = -0.05  # Small penalty for invalid action
                done = False
                self.invalid_action_count += 1
                self.episode_invalid_actions += 1
                
                if self.invalid_action_count >= self.invalid_action_limit:
                    done = True
                    reward -= 1.0  # Extra penalty for too many invalid actions
                    logging.debug(f"Too many invalid actions ({self.invalid_action_count}) - ending episode")
                
                obs = self._get_obs()
                
                try:
                    self.current_valid_actions = self.action_handler.generate_valid_actions()
                except Exception as e:
                    log_exception(e, f"Error generating valid actions after invalid action {action_idx}")
                    # Keep using current valid actions or enable a safe fallback
                    if not np.any(self.current_valid_actions):
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[0] = True  # Enable at least END_TURN
                
                info["action_mask"] = self.current_valid_actions.astype(bool)
                truncated = self.current_step >= self.max_episode_steps
                
                # Update action and reward memory
                self.last_n_actions = np.roll(self.last_n_actions, 1)
                self.last_n_actions[0] = action_idx
                self.last_n_rewards = np.roll(self.last_n_rewards, 1)
                self.last_n_rewards[0] = reward
                
                if DEBUG_ACTION_STEPS:
                    logging.debug(f"== STEP END (Invalid): reward={reward}, done={done} ==")
                        
                return obs, reward, done, truncated, info
            
            # Valid action processing
            self.invalid_action_count = 0
            logging.debug(f"Executing action: {action_type} ({action_idx}) with param {param}")
            
            # Record this action
            self.current_episode_actions.append(action_idx)
            
            # Process specific action types
            # END_COMBAT action
            if action_type == "END_COMBAT":
                try:
                    logging.debug("Processing END_COMBAT action")
                    
                    # Store current phase for logging
                    current_phase = gs.phase
                    
                    # Check if we're in the right phase
                    if current_phase not in [gs.PHASE_END_COMBAT, gs.PHASE_END_OF_COMBAT]:
                        logging.warning(f"END_COMBAT action called from unexpected phase: {current_phase}")
                        
                        # Force transition to appropriate phase
                        gs.phase = gs.PHASE_MAIN_POSTCOMBAT
                    else:
                        # Normal transition
                        gs.phase = gs.PHASE_MAIN_POSTCOMBAT
                    
                    # Clean up combat state
                    gs.current_attackers = []
                    gs.current_block_assignments = {}
                    gs.combat_damage_dealt = False
                    
                    # Log phase transition
                    logging.debug(f"Combat ended: Transitioned from {current_phase} to {gs.phase}")
                    
                    # Apply small reward for proper progression
                    reward = 0.05  # Slight positive reward for normal phase progression
                    
                    # Generate new valid actions for post-combat main phase
                    try:
                        self.current_valid_actions = self.action_handler.generate_valid_actions()
                    except Exception as e:
                        logging.error(f"Error generating valid actions after END_COMBAT: {str(e)}")
                        # Fallback to basic actions
                        self.current_valid_actions = np.zeros(480, dtype=bool)
                        self.current_valid_actions[10] = True  # Enable MAIN_POSTCOMBAT action
                    
                    # Get updated observation
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    
                    # Update strategy memory
                    if hasattr(self, 'strategy_memory') and pre_action_pattern is not None:
                        try:
                            self.strategy_memory.update_strategy(pre_action_pattern, reward)
                        except Exception as e:
                            log_exception(e, "Error updating strategy memory")
                    
                    return obs, reward, False, False, info
                except Exception as e:
                    log_exception(e, "Error in END_COMBAT action processing")
                    # Continue with normal step logic
            
            # Handle DECLARE_BLOCKERS phase transition to damage phase
            elif gs.phase == gs.PHASE_DECLARE_BLOCKERS and action_type == "DECLARE_BLOCKER_DONE":
                try:
                    logging.debug("Blockers declared, advancing to combat damage")
                    
                    # Check for first strike creatures
                    has_first_strike = False
                    
                    # Check attackers for first strike
                    for card_id in gs.current_attackers:
                        card = gs._safe_get_card(card_id)
                        if card and hasattr(card, 'oracle_text') and 'first strike' in card.oracle_text.lower():
                            has_first_strike = True
                            break
                    
                    # Check blockers for first strike if not found in attackers
                    if not has_first_strike:
                        for blocker_list in gs.current_block_assignments.values():
                            for blocker_id in blocker_list:
                                card = gs._safe_get_card(blocker_id)
                                if card and hasattr(card, 'oracle_text') and 'first strike' in card.oracle_text.lower():
                                    has_first_strike = True
                                    break
                            if has_first_strike:
                                break
                    
                    # If any creature has first strike, we go to first strike damage
                    if has_first_strike:
                        gs.phase = gs.PHASE_FIRST_STRIKE_DAMAGE
                        logging.debug("First strike detected, advancing to FIRST_STRIKE_DAMAGE phase")
                    else:
                        # Otherwise straight to regular combat damage
                        gs.phase = gs.PHASE_COMBAT_DAMAGE
                        logging.debug("No first strike, advancing to COMBAT_DAMAGE phase")
                    
                    # Generate new valid actions
                    self.current_valid_actions = self.action_handler.generate_valid_actions()
                    
                    # Apply small reward for proper progression
                    reward = 0.05  # Slight positive reward for normal phase progression
                    
                    obs = self._get_obs()
                    info["action_mask"] = self.current_valid_actions.astype(bool)
                    
                    # Update strategy memory
                    if hasattr(self, 'strategy_memory') and pre_action_pattern is not None:
                        try:
                            self.strategy_memory.update_strategy(pre_action_pattern, reward)
                        except Exception as e:
                            log_exception(e, "Error updating strategy memory")
                    
                    return obs, reward, False, False, info
                except Exception as e:
                    log_exception(e, "Error advancing from blockers to damage: {str(e)}")
                    import traceback
                    logging.error(traceback.format_exc())
                    # Continue with normal step logic
            
            # Apply the action
            try:
                reward, done = self.action_handler.apply_action(action_type, param)
            except Exception as e:
                log_exception(e, f"Error applying action {action_type} with param {param}")
                reward = -0.1
                done = False
            
            # Apply replacement effects if available
            if hasattr(gs, 'replacement_effects') and gs.replacement_effects:
                try:
                    event_context = {
                        "action_type": action_type,
                        "card_id": param,
                        "controller": me
                    }
                    gs.replacement_effects.apply_replacements("ACTION", event_context)
                except Exception as e:
                    log_exception(e, "Error applying replacement effects")
            
            # Update strategy memory with action result
            if hasattr(self, 'strategy_memory'):
                try:
                    # Extract post-action pattern
                    post_action_pattern = self.strategy_memory.extract_strategy_pattern(gs)
                    
                    # Update strategy memory with the game's reward
                    if pre_action_pattern is not None:
                        self.strategy_memory.update_strategy(pre_action_pattern, reward)
                    
                    # Record the action sequence
                    if hasattr(self, 'current_episode_actions'):
                        self.strategy_memory.record_action_sequence(self.current_episode_actions, reward)
                    
                    # Periodic memory management
                    if random.random() < 0.1:  # 10% chance to save and prune memory
                        try:
                            self.strategy_memory.save_memory_async()
                            self.strategy_memory.prune_memory()
                        except Exception as e:
                            logging.warning(f"Non-critical memory management error: {e}")
                    
                    logging.debug(f"Strategy memory updated with reward: {reward}")
                except Exception as e:
                    logging.error(f"Error updating strategy memory: {str(e)}")
                    import traceback
                    logging.error(traceback.format_exc())
            
            # Layer system cleanup
            if hasattr(gs, 'layer_system') and gs.layer_system:
                try:
                    gs.layer_system.cleanup_expired_effects()
                except Exception as e:
                    log_exception(e, "Error cleaning up layer effects")
            
            # Replacement effects cleanup
            if hasattr(gs, 'replacement_effects') and gs.replacement_effects:
                try:
                    gs.replacement_effects.cleanup_expired_effects()
                except Exception as e:
                    log_exception(e, "Error cleaning up replacement effects")
            
            # Check if turn has changed
            if gs.turn != current_turn:
                turn_diff = gs.turn - current_turn
                if turn_diff > 1:
                    logging.warning(f"Detected unexpected turn jump from {current_turn} to {gs.turn}. This may indicate a bug.")
                logging.info(f"== ADVANCING TO TURN {gs.turn} ==")
                
                # Add this: Adapt strategy at the beginning of new turns
                if hasattr(gs, 'strategic_planner'):
                    try:
                        strategy_params = gs.strategic_planner.adapt_strategy()
                        logging.debug(f"Adapted strategy for turn {gs.turn}: {strategy_params}")
                    except Exception as e:
                        logging.error(f"Error adapting strategy: {e}")
            
            if not done:
                # Only add board reward when a new turn has begun or phase has changed substantially
                if gs.turn != current_turn or (current_phase != gs.phase and 
                                            (current_phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] or 
                                            gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT])):
                    try:
                        # Use enhanced board state evaluation if available
                        if hasattr(self, 'strategic_planner') and hasattr(self.strategic_planner, 'analyze_game_state'):
                            # Update analysis
                            analysis = self.strategic_planner.analyze_game_state()
                            position_score = analysis["position"]["score"]
                            
                            # Convert position score to reward
                            board_reward = position_score * 0.3
                            
                            # Apply strategy adjustments
                            strategy_params = self.strategic_planner.adapt_strategy()
                            adjusted_reward = board_reward * (1 + 0.2 * strategy_params["aggression"])
                            
                            reward += adjusted_reward
                            logging.debug(f"Added strategic board state reward: {adjusted_reward:.4f}")
                        else:
                            # Fall back to original board state reward
                            board_reward = self._calculate_board_state_reward()
                            reward += board_reward
                            logging.debug(f"Added basic board state reward: {board_reward:.4f}")
                    except Exception as e:
                        log_exception(e, "Error calculating board state reward")
            
            self.episode_rewards.append(reward)
            
            # Check for game end conditions with additional safeguards
            if gs.turn > self.max_turns:
                done = True
                logging.info(f"GAME OVER: Turn limit reached ({gs.turn} > {self.max_turns}) - P1 Life: {gs.p1['life']}, P2 Life: {gs.p2['life']}")
                
                # Determine winner by life total
                if me["life"] > opp["life"]:
                    reward += 3.0  # Big reward for winning
                    logging.info("Game ended due to turn limit - Player won by higher life total")
                    
                    # Set win/loss flags for state-based actions
                    if gs.agent_is_p1:
                        gs.p1["won_game"] = True
                        gs.p2["lost_game"] = True
                    else:
                        gs.p2["won_game"] = True
                        gs.p1["lost_game"] = True
                        
                    # Record game statistics
                    if self.has_stats_tracker:
                        is_p1_winner = gs.agent_is_p1
                        logging.info("Recording game statistics") 
                        self.record_game_result(is_p1_winner, gs.turn, me["life"])
                    
                elif me["life"] < opp["life"]:
                    reward -= 1.0  # Penalty for losing
                    logging.info("Game ended due to turn limit - Player lost by lower life total")
                    
                    # Set win/loss flags for state-based actions
                    if gs.agent_is_p1:
                        gs.p1["lost_game"] = True
                        gs.p2["won_game"] = True
                    else:
                        gs.p2["lost_game"] = True
                        gs.p1["won_game"] = True
                        
                    # Record game statistics
                    if self.has_stats_tracker:
                        is_p1_winner = not gs.agent_is_p1
                        logging.info("Recording game statistics") 
                        self.record_game_result(is_p1_winner, gs.turn, opp["life"])
                    
                else:
                    reward += 0.25  # Small reward for draw
                    logging.info("Game ended due to turn limit - Game ended in a draw")
                    
                    # Set draw flags
                    gs.p1["game_draw"] = True
                    gs.p2["game_draw"] = True
                        
                    # Record game statistics for draw
                    if self.has_stats_tracker:
                        logging.info("Recording game statistics") 
                        self.record_game_result(None, gs.turn, me["life"], is_draw=True)
            
            # Life-based game ending conditions
            if me["life"] <= 0:
                done = True
                reward -= 5.0  # Bigger penalty for losing by life total
                
                # Set win/loss flags for state-based actions
                if gs.agent_is_p1:
                    gs.p1["lost_game"] = True
                    gs.p2["won_game"] = True
                else:
                    gs.p2["lost_game"] = True
                    gs.p1["won_game"] = True
                    
                logging.debug("Player lost - life total reduced to zero or below")
                
                # Record game statistics
                if self.has_stats_tracker:
                    is_p1_winner = not gs.agent_is_p1
                    logging.info("Recording game statistics") 
                    self.record_game_result(is_p1_winner, gs.turn, opp["life"])
                
                # Log final game state
                my_creatures = [cid for cid in me["battlefield"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                            and 'creature' in gs._safe_get_card(cid).card_types]
                opp_creatures = [cid for cid in opp["battlefield"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                            and 'creature' in gs._safe_get_card(cid).card_types]
                logging.info(f"GAME OVER: Player lost with {me['life']} life vs opponent's {opp['life']} life")
                logging.info(f"GAME STATS: Turn {gs.turn}, Player creatures: {len(my_creatures)}, Opponent creatures: {len(opp_creatures)}")
            
            if opp["life"] <= 0:
                done = True
                reward += 10.0  # Base reward for reducing opponent to zero
                
                # Set win/loss flags for state-based actions
                if gs.agent_is_p1:
                    gs.p1["won_game"] = True
                    gs.p2["lost_game"] = True
                else:
                    gs.p2["won_game"] = True
                    gs.p1["lost_game"] = True
                    
                logging.debug("Player won - reduced opponent life to zero or below")
                
                # ENHANCED QUICK WIN BONUSES: Higher rewards for faster wins
                if gs.turn <= 4:
                    bonus = 15.0  # Massive bonus for extremely quick win
                    reward += bonus
                    logging.info(f"EXTREMELY FAST WIN BONUS: Victory in only {gs.turn} turns! (+{bonus})")
                elif gs.turn <= 7:
                    bonus = 8.0  # Large bonus for quick win
                    reward += bonus
                    logging.info(f"QUICK WIN BONUS: Victory in only {gs.turn} turns! (+{bonus})")
                elif gs.turn <= 10:
                    bonus = 5.0  # Moderate bonus for reasonably quick win
                    reward += bonus
                    logging.info(f"EFFICIENT WIN BONUS: Victory in {gs.turn} turns! (+{bonus})")
                
                # Record game statistics
                if self.has_stats_tracker:
                    is_p1_winner = gs.agent_is_p1
                    self.record_game_result(is_p1_winner, gs.turn, me["life"])
                
                # Log final game details
                logging.info(f"GAME OVER: Player WON with {me['life']} life vs opponent's {opp['life']} life")
                logging.info(f"GAME STATS: Turn {gs.turn}, Victory achieved")
            
            # Add life total difference tracking
            if prev_player_life is not None and prev_opponent_life is not None:
                player_life_change = me["life"] - prev_player_life
                opponent_life_change = opp["life"] - prev_opponent_life
                
                # Reward positive life swings
                if player_life_change > 0:
                    gain_reward = min(player_life_change * 0.1, 0.3)
                    reward += gain_reward
                    logging.debug(f"Life gain reward: +{gain_reward:.2f} for gaining {player_life_change} life")
                    
                if opponent_life_change < 0:
                    damage_reward = min(abs(opponent_life_change) * 0.15, 0.5)
                    reward += damage_reward
                    logging.debug(f"Damage reward: +{damage_reward:.2f} for dealing {abs(opponent_life_change)} damage")
            
            # Check if progress was forced by the game system and apply penalty if necessary
            if hasattr(gs, 'progress_was_forced') and gs.progress_was_forced:
                # Apply a significant penalty to discourage behaviors that lead to phase stagnation
                penalty = -1.0
                reward += penalty
                gs.progress_was_forced = False  # Reset the flag
                logging.info(f"Applied penalty of {penalty} for forcing game progress")
            
            # Store current life totals for next step
            self.prev_player_life = me["life"]
            self.prev_opponent_life = opp["life"]
            
            # Update action and reward memory
            self.last_n_actions = np.roll(self.last_n_actions, 1)
            self.last_n_actions[0] = action_idx
            self.last_n_rewards = np.roll(self.last_n_rewards, 1)
            self.last_n_rewards[0] = reward
            
            # Update enhanced components state if available
            if hasattr(self, 'strategic_planner'):
                try:
                    # Reanalyze game state after action
                    analysis = self.strategic_planner.analyze_game_state()
                    strategy_params = self.strategic_planner.adapt_strategy()
                    
                    # Add to info dictionary
                    info["position"] = analysis["position"]["overall"]
                    info["game_stage"] = analysis["game_info"]["game_stage"]
                    info["strategy"] = {
                        "aggression": strategy_params["aggression"],
                        "risk": strategy_params["risk"]
                    }
                except Exception as e:
                    log_exception(e, "Error updating strategic planner")
            
            # Get updated observation and regenerate action mask
            obs = self._get_obs()
            
            try:
                self.current_valid_actions = self.action_handler.generate_valid_actions()
            except Exception as e:
                log_exception(e, "Error generating valid actions at end of step")
                # Fallback to safe actions
                if not np.any(self.current_valid_actions):
                    self.current_valid_actions = np.zeros(480, dtype=bool)
                    self.current_valid_actions[0] = True  # Enable END_TURN as fallback
            
            info["action_mask"] = self.current_valid_actions.astype(bool)
            
            # Check for episode step limit
            truncated = False
            if self.current_step >= self.max_episode_steps:
                truncated = True
                done = True
                reward -= 1.0
                logging.debug("Max episode steps reached, ending episode.")
            
            # Detailed logging for substantial episodes
            if done and (sum(self.episode_rewards) > 5 or sum(self.episode_rewards) < -5):
                self.detailed_logging = True
                self._log_episode_summary()
                
            if done:
                try:
                    # Extract final strategy pattern
                    final_pattern = self.strategy_memory.extract_strategy_pattern(gs)
                    
                    # Update strategy with total episode reward
                    total_reward = sum(self.episode_rewards)
                    self.strategy_memory.update_strategy(final_pattern, total_reward)
                    
                    # Save memory at the end of an episode
                    if hasattr(self, 'strategy_memory'):
                        self.strategy_memory.save_memory_async()
                    
                    logging.info(f"Episode ended with total reward: {total_reward}")
                except Exception as e:
                    log_exception(e, "Error processing final strategy memory update")
        
            # Ensure game results are recorded when done=True
            if done:
                logging.info(f"Game ended with done=True. Final state: Turn {gs.turn}, Player Life: {me['life']}, Opponent Life: {opp['life']}")
                self.ensure_game_result_recorded()
            if done and self.has_card_memory:
                logging.info("Saving card memory data at the end of the game")
                self.card_memory.save_all_card_data()
                
            if DEBUG_ACTION_STEPS:
                logging.debug(f"== STEP END: reward={reward}, done={done}, truncated={truncated} ==")
                logging.debug(f"Post-step state: P1 Life={gs.p1['life']}, P2 Life={gs.p2['life']}, Phase={gs.phase}")
                
            return obs, reward, done, truncated, info
                    
        except Exception as e:
            # Use log_exception if available
            try:
                from Playersim.debug import log_exception
                log_exception(e, f"Unhandled exception in step method with action {action_idx}")
            except ImportError:
                logging.error(f"Unhandled exception in step method: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
            
            # Return a safe fallback state to prevent crashes
            try:
                obs = self._get_obs()
            except:
                # Create a minimal valid observation if _get_obs fails
                obs = {k: np.zeros(space.shape, dtype=space.dtype) 
                    for k, space in self.observation_space.spaces.items()}
                    
            self.current_valid_actions = np.zeros(480, dtype=bool)
            self.current_valid_actions[0] = True  # Enable at least one action
            info = {"action_mask": self.current_valid_actions.astype(bool),
                    "error_recovery": True}
            
            # Return negative reward and continue
            return obs, -0.1, True, False, info
        
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
