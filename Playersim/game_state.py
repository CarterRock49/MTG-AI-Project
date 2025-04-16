import random
import logging
import numpy as np
import copy

from Playersim.ability_utils import EffectFactory # Import copy for deepcopy

# (Keep existing imports)
from .card import Card
from .debug import DEBUG_MODE
import re
from .ability_types import StaticAbility, TriggeredAbility
from collections import defaultdict


class GameState:

    # (Keep existing class variables like PHASE_ constants and __slots__)
    __slots__ = ["card_db", "max_turns", "max_hand_size", "max_battlefield", "day_night_checked_this_turn",
                 "phase_history", "stack", "priority_pass_count", "last_stack_size",
                 "turn", "phase", "agent_is_p1", "combat_damage_dealt", "day_night_state",
                 "current_attackers", "current_block_assignments", 'mulligan_data',
                 "current_spell_requires_target", "current_spell_card_id",
                 "optimal_attackers", "attack_suggestion_used", 'cards_played',
                 "p1", "p2", "ability_handler", "damage_dealt_this_turn",
                 "previous_priority_phase", "layer_system", "until_end_of_turn_effects",
                 "mana_system", "replacement_effects", "cards_drawn_this_turn",
                 "combat_resolver", "temp_control_effects", "abilities_activated_this_turn",
                 "card_evaluator", "spells_cast_this_turn", "_phase_history",
                 "strategic_planner", "attackers_this_turn", 'strategy_memory',
                 "_logged_card_ids", "_logged_errors", "targeting_system",
                 "_phase_action_count", "priority_player", "stats_tracker",
                 "card_memory", # Added from env
                 # Special card types
                 "adventure_cards", "saga_counters", "mdfc_cards", "battle_cards",
                 "cards_castable_from_exile", "cast_as_back_face",
                 # Additional slots for various tracking variables
                 "phased_out", # Keep for Phasing
                 "suspended_cards", # Keep for Suspend
                 "kicked_cards", "evoked_cards",
                 "foretold_cards", "blitz_cards", "dash_cards", "unearthed_cards",
                 "jump_start_cards", "buyback_cards", "flashback_cards",
                 "life_gained_this_turn", "damage_this_turn", "exile_at_end_of_combat",
                 "haste_until_eot", "has_haste_until_eot", "progress_was_forced",
                 "_turn_limit_checked", "miracle_card", "miracle_cost", "miracle_player",
                 "miracle_active", "miracle_card_id", "miracle_cost_parsed",
                 # New tracking variables
                 "split_second_active",
                 "rebounded_cards", # Keep for Rebound
                 "banding_creatures", # Keep for Banding (maybe handled differently)
                 "crewed_vehicles", "morphed_cards", "manifested_cards",
                 "cards_to_graveyard_this_turn",
                 "boast_activated", "forecast_used", "epic_spells", "city_blessing",
                 "myriad_tokens", "persist_returned", "undying_returned", "gravestorm_count",
                 "madness_cast_available", # Keep for Madness
                 # Context slots
                 "targeting_context", "sacrifice_context", "choice_context",
                 "mulligan_in_progress", "mulligan_player", "mulligan_count",
                 "bottoming_in_progress", "bottoming_player", "cards_to_bottom", "bottoming_count",
                 "spree_context",
                 "dredge_pending",
                 "madness_trigger", # Keep for Madness
                 "pending_spell_context", "clash_context",
                 "surveil_in_progress", "cards_being_surveiled", "surveiling_player", # Surveil state
                 "scry_in_progress", "scrying_cards", "scrying_player", "scrying_tops", "scrying_bottoms" # Scry state
                 ]
    # Define phase names consistently within the class
    # Updated with missing phases and explicit mappings
    PHASE_UNTAP = 0
    PHASE_UPKEEP = 1
    PHASE_DRAW = 2
    PHASE_MAIN_PRECOMBAT = 3
    PHASE_BEGIN_COMBAT = 4         # Renamed from BEGINNING_OF_COMBAT
    PHASE_DECLARE_ATTACKERS = 5
    PHASE_DECLARE_BLOCKERS = 6
    PHASE_FIRST_STRIKE_DAMAGE = 16 # Explicitly map to 16
    PHASE_COMBAT_DAMAGE = 7
    PHASE_END_OF_COMBAT = 8
    PHASE_MAIN_POSTCOMBAT = 9
    PHASE_END_STEP = 10
    PHASE_PRIORITY = 11           # Added for clarity, used internally
    PHASE_TARGETING = 17          # Assign new index
    PHASE_SACRIFICE = 18          # Assign new index
    PHASE_CHOOSE = 19             # Assign new index
    PHASE_CLEANUP = 15
    # LEGACY Mappings (if still used elsewhere) - Map to new constants
    # PHASE_BEGINNING_OF_COMBAT = PHASE_BEGIN_COMBAT # Map legacy name

    _PHASE_NAMES = {
        0: "UNTAP", 1: "UPKEEP", 2: "DRAW", 3: "MAIN_PRECOMBAT",
        4: "BEGIN_COMBAT", 5: "DECLARE_ATTACKERS", 6: "DECLARE_BLOCKERS",
        16: "FIRST_STRIKE_DAMAGE", 7: "COMBAT_DAMAGE", 8: "END_OF_COMBAT",
        9: "MAIN_POSTCOMBAT", 10: "END_STEP", 15: "CLEANUP",
        11: "PRIORITY", 17: "TARGETING", 18: "SACRIFICE", 19: "CHOOSE"
    }

    def __init__(self, card_db, max_turns=20, max_hand_size=7, max_battlefield=20):
        # ... (Keep basic param init) ...
        self.card_db = card_db
        self.max_turns = max_turns
        self.max_hand_size = max_hand_size
        self.max_battlefield = max_battlefield

        # Initialize base variables
        self.turn = 1
        self.phase = self.PHASE_UNTAP # Start at UNTAP
        self.agent_is_p1 = True
        self.combat_damage_dealt = False
        self.stack = []
        self.priority_pass_count = 0
        self.last_stack_size = 0
        self._phase_history = [] # Use internal list for history
        self._phase_action_count = 0
        self.priority_player = None # Will be set during reset or first phase

        # Combat state initialization
        self.current_attackers = []
        self.current_block_assignments = {}

        # Combat optimization variables
        self.optimal_attackers = None
        self.attack_suggestion_used = False

        # Player states (will be initialized in reset)
        self.p1 = None
        self.p2 = None

        # Initialize system references as None - These will be created by _init_subsystems
        self.mana_system = None
        self.combat_resolver = None
        self.card_evaluator = None
        self.strategic_planner = None
        self.strategy_memory = None # External system reference
        self.stats_tracker = None # External system reference
        self.card_memory = None # External system reference
        self.ability_handler = None
        self.layer_system = None
        self.replacement_effects = None
        self.targeting_system = None


        # Process card_db properly (ensure Card objects are correctly instantiated if needed)
        if isinstance(card_db, list):
            # Assuming card_db might contain dicts, convert them to Card objects if needed
            temp_db = {}
            for i, item in enumerate(card_db):
                 if isinstance(item, dict) and 'name' in item:
                      try:
                           card_obj = Card(item)
                           card_id = getattr(card_obj, 'card_id', f"card_{i}") # Use existing or generate
                           card_obj.card_id = card_id
                           temp_db[card_id] = card_obj
                      except Exception as e:
                           logging.error(f"Failed to create Card object from dict at index {i}: {e}")
                 elif isinstance(item, Card):
                     card_id = getattr(item, 'card_id', f"card_{i}")
                     item.card_id = card_id
                     temp_db[card_id] = item
                 # Skip other types or log warning
            self.card_db = temp_db
        elif isinstance(card_db, dict):
            # Ensure values are Card objects
             self.card_db = {k:v for k,v in card_db.items() if isinstance(v, Card)}
             # Assign card_id if missing
             for k,v in self.card_db.items():
                  if not hasattr(v, 'card_id') or v.card_id is None:
                       v.card_id = k
        else:
            self.card_db = {}
            logging.error(f"Invalid card database format: {type(card_db)}")

        # Contexts for multi-step actions
        self.targeting_context = None
        self.sacrifice_context = None
        self.choice_context = None
        self.mulligan_in_progress = False
        self.mulligan_player = None
        self.mulligan_count = {}
        self.bottoming_in_progress = False
        self.bottoming_player = None
        self.cards_to_bottom = 0
        self.bottoming_count = 0
        self.spree_context = None
        self.dredge_pending = None
        self.pending_spell_context = None
        self.clash_context = None

        # Surveil/Scry state
        self.surveil_in_progress = False
        self.cards_being_surveiled = []
        self.surveiling_player = None
        self.scry_in_progress = False
        self.scrying_cards = []
        self.scrying_player = None
        self.scrying_tops = []
        self.scrying_bottoms = []

        # Internal tracking/logging flags
        self._logged_card_ids = set()
        self._logged_errors = set()
        self.previous_priority_phase = None # Track phase before PRIORITY

        # Initialize all subsystems (called AFTER self.card_db is set)
        self._init_subsystems() # Centralized subsystem creation
        logging.info("GameState initialized.")
        
    def _init_subsystems(self):
        """Initialize game subsystems with error handling and correct dependencies."""
        # Layer System (Dependency for others)
        try:
            from .layer_system import LayerSystem
            self.layer_system = LayerSystem(self)
            logging.debug("Layer system initialized successfully.")
        except ImportError as e:
            logging.warning(f"Layer system module not available: {e}")
            self.layer_system = None
        except Exception as e:
            logging.error(f"Error initializing LayerSystem: {e}")
            self.layer_system = None

        # Replacement Effects (Depends on LayerSystem potentially)
        try:
            from .replacement_effects import ReplacementEffectSystem
            self.replacement_effects = ReplacementEffectSystem(self)
            logging.debug("Replacement effects system initialized successfully.")
        except ImportError as e:
            logging.warning(f"Replacement effects system module not available: {e}")
            self.replacement_effects = None
        except Exception as e:
            logging.error(f"Error initializing ReplacementEffectSystem: {e}")
            self.replacement_effects = None

        # Targeting System (Dependency for AbilityHandler)
        try:
            from .targeting import TargetingSystem
            self.targeting_system = TargetingSystem(self)
            logging.debug("Targeting system initialized successfully.")
        except ImportError as e:
            logging.warning(f"Targeting system module not available: {e}")
            self.targeting_system = None
        except Exception as e:
            logging.error(f"Error initializing TargetingSystem: {e}")
            self.targeting_system = None

        # Ability Handler (Needs TargetingSystem, link Layer/Replacement)
        try:
            from .ability_handler import AbilityHandler
            self.ability_handler = AbilityHandler(self) # Initializes its own targeting system ref now
            logging.debug("AbilityHandler initialized successfully.")
            # Link other systems if handler expects them (though dependencies should be via GameState now)
            # if self.targeting_system: self.ability_handler.targeting_system = self.targeting_system
        except ImportError as e:
            logging.warning(f"AbilityHandler module not available: {e}")
            self.ability_handler = None
        except Exception as e:
            logging.error(f"Error initializing AbilityHandler: {e}")
            self.ability_handler = None

        # Mana System
        try:
            from .enhanced_mana_system import EnhancedManaSystem
            self.mana_system = EnhancedManaSystem(self)
            logging.debug("Enhanced mana system initialized successfully.")
        except ImportError as e:
            logging.warning(f"Enhanced mana system module not available: {e}")
            self.mana_system = None
        except Exception as e:
            logging.error(f"Error initializing EnhancedManaSystem: {e}")
            self.mana_system = None

        # Combat Resolver (Needs Ability Handler potentially)
        try:
            from .enhanced_combat import ExtendedCombatResolver
            self.combat_resolver = ExtendedCombatResolver(self)
            # Link Ability Handler if needed by resolver
            if self.ability_handler and hasattr(self.combat_resolver, 'ability_handler'):
                 self.combat_resolver.ability_handler = self.ability_handler
            logging.debug("Combat resolver initialized successfully.")
        except ImportError as e:
            logging.warning(f"Combat resolver module not available: {e}")
            self.combat_resolver = None
        except Exception as e:
            logging.error(f"Error initializing CombatResolver: {e}")
            self.combat_resolver = None

        # Card Evaluator (Needs other systems)
        try:
            from .enhanced_card_evaluator import EnhancedCardEvaluator
            self.card_evaluator = EnhancedCardEvaluator(self, self.stats_tracker, self.card_memory)
            logging.debug("Card evaluator initialized successfully.")
        except ImportError as e:
            logging.warning(f"Card evaluator module not available: {e}")
            self.card_evaluator = None
        except Exception as e:
            logging.error(f"Error initializing EnhancedCardEvaluator: {e}")
            self.card_evaluator = None

        # Strategic Planner (Needs Evaluator, Resolver)
        try:
            from .strategic_planner import MTGStrategicPlanner
            self.strategic_planner = MTGStrategicPlanner(self, self.card_evaluator, self.combat_resolver)
            logging.debug("Strategic planner initialized successfully.")
        except ImportError as e:
            logging.warning(f"Strategic planner module not available: {e}")
            self.strategic_planner = None
        except Exception as e:
            logging.error(f"Error initializing MTGStrategicPlanner: {e}")
            self.strategic_planner = None

        # Init tracking variables AFTER subsystems that might reference them
        self._init_tracking_variables()
        self.initialize_day_night_cycle()

    def _init_tracking_variables(self):
        """Initialize all game state tracking variables with proper defaults."""
        # Player Independent Tracking
        self.day_night_state = None
        self.day_night_checked_this_turn = False
        self.split_second_active = False
        self.phased_out = set() # Stores IDs of phased-out permanents
        self.suspended_cards = {} # {card_id: {'player': P, 'counters': N, 'cost': STR}}
        self.rebounded_cards = {} # {card_id: {'owner': P, 'turn_exiled': T}}
        self.madness_cast_available = None # {card_id: {'player': P, 'cost': STR}} - holds ONE opportunity
        self.madness_trigger = None # Used internally during discard resolution
        self.miracle_card_id = None
        self.miracle_cost = None
        self.miracle_player = None
        self.miracle_active = False
        self.miracle_cost_parsed = None
        self.kicked_cards = set()
        self.evoked_cards = set()
        self.foretold_cards = {} # {card_id: {'turn': T}}
        self.blitz_cards = set()
        self.dash_cards = set()
        self.unearthed_cards = set()
        self.jump_start_cards = set()
        self.buyback_cards = set()
        self.flashback_cards = set()
        self.adventure_cards = set()
        self.exile_at_end_of_combat = []
        self.haste_until_eot = set() # Use only this one for consistency
        self.crewed_vehicles = set()
        self.morphed_cards = {}
        self.manifested_cards = {}
        self.epic_spells = {}
        self.myriad_tokens = []
        self.persist_returned = set()
        self.undying_returned = set()
        self.banding_creatures = set() # Track creatures currently in bands

        # Turn-based tracking (resets each turn usually)
        self.spells_cast_this_turn = []
        self.attackers_this_turn = set()
        self.damage_dealt_this_turn = {}
        self.cards_drawn_this_turn = {} # Initialize as empty, will be populated like {'p1': 0, 'p2': 0}
        self.life_gained_this_turn = {}
        self.damage_this_turn = {}
        self.cards_to_graveyard_this_turn = {} # {turn_num: [card_ids]}
        self.gravestorm_count = 0
        self.boast_activated = set()
        self.forecast_used = set()

        # Context slots (reset before action handling)
        self.targeting_context = None
        self.sacrifice_context = None
        self.choice_context = None
        self.pending_spell_context = None
        self.clash_context = None
        self.dredge_pending = None
        self.spree_context = None

        # Surveil/Scry state
        self.surveil_in_progress = False
        self.cards_being_surveiled = []
        self.surveiling_player = None
        self.scry_in_progress = False
        self.scrying_cards = []
        self.scrying_player = None
        self.scrying_tops = []
        self.scrying_bottoms = []

        # Game state flags (can be reset or carried over)
        self.combat_damage_dealt = False
        self.progress_was_forced = False
        self._turn_limit_checked = False

        # Internal tracking/logging flags (reset for new game)
        self._logged_card_ids = set()
        self._logged_errors = set()
        self.previous_priority_phase = None

        # Effect Tracking (can be reset)
        self.until_end_of_turn_effects = {} # Tracking specific effects
        self.temp_control_effects = {} # {card_id: original_controller}

        # Saga and Battle counters
        self.saga_counters = {} # {card_id: chapter_num}
        self.battle_cards = {} # {card_id: defense_counters}

        # Cast Tracking
        self.cards_castable_from_exile = set()
        self.cast_as_back_face = set()

        # Other state tracking
        self.mdfc_cards = set() # Tracks MDFCs on battlefield/stack?
        self.abilities_activated_this_turn = [] # List of (card_id, ability_idx) tuples

        # Player state based tracking (reset inside player dicts)
        for player in [self.p1, self.p2]:
            if player:
                 player["land_played"] = False
                 player["entered_battlefield_this_turn"] = set()
                 player["activated_this_turn"] = set()
                 player["pw_activations"] = {}
                 player["lost_life_this_turn"] = False
                 player["attempted_draw_from_empty"] = False
                 player["poison_counters"] = 0
                 player["experience_counters"] = 0
                 player["energy_counters"] = 0
                 player["city_blessing"] = False
                 player["monarch"] = False
                 player["damage_counters"] = {}
                 player["deathtouch_damage"] = set()
                 player["loyalty_counters"] = {}
                 # player["saga_counters"] = {} # Moved to game level
                 player["attachments"] = {}
                 player["championed_cards"] = {}
                 player["ciphered_spells"] = {}
                 player["haunted_by"] = {}
                 player["hideaway_cards"] = {}
                 player["mutation_stacks"] = {}
                 player["regeneration_shields"] = set()
                 player["lost_game"] = False
                 player["won_game"] = False
                 player["game_draw"] = False
                 player["skip_end_step_trigger"] = set()
                 player["phased_out_permanents"] = set()
                 # Reset mana pools
                 player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
                 player["conditional_mana"] = {}
                 player["phase_restricted_mana"] = {}

        logging.debug("Initialized/Reset all tracking variables")

    def initialize_day_night_cycle(self):
        """Initialize the day/night cycle state and tracking."""
        # Start with neither day nor night
        self.day_night_state = None
        # Track if we've already checked day/night transition this turn
        self.day_night_checked_this_turn = False
        logging.debug("Day/night cycle initialized (neither day nor night)")

    
    def reset(self, p1_deck, p2_deck, seed=None):
        """Reset the game state with new decks and initialize all subsystems"""
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        # Reset basic game state
        self.turn = 1
        self.phase = self.PHASE_UNTAP # Start at UNTAP phase
        self.combat_damage_dealt = False
        self.stack = []
        self.priority_pass_count = 0
        self.last_stack_size = 0
        self._phase_action_count = 0
        self._phase_history = [] # Explicit reset
        self.optimal_attackers = None
        self.attack_suggestion_used = False
        self.cards_played = {0: [], 1: []} # Explicit reset

        # Initialize player states AFTER resetting other state
        self.p1 = self._init_player(p1_deck)
        self.p2 = self._init_player(p2_deck)
        # Assign names after init
        if self.p1: self.p1['name'] = 'Player 1'
        if self.p2: self.p2['name'] = 'Player 2'

        # Set initial priority player and agent identity AFTER player init
        self.priority_player = self.p1 # P1 starts with priority
        self.agent_is_p1 = True # Assume agent is P1 by default unless configured otherwise

        # Initialize all tracking variables using the helper
        # This resets keyword states, contexts, turn trackers, etc.
        self._init_tracking_variables()

        # Initialize Day/Night Cycle
        self.initialize_day_night_cycle() # Call after tracking vars init

        # Initialize mulligan state correctly AFTER players exist
        self.mulligan_in_progress = True # Start with mulligan phase
        self.mulligan_player = self.p1 # P1 mulligans first
        self.mulligan_count = {'p1': 0, 'p2': 0} # Reset counts
        self.mulligan_data = {'p1': 0, 'p2': 0} # Reset separate tracker
        self.bottoming_in_progress = False
        self.bottoming_player = None
        self.cards_to_bottom = 0
        self.bottoming_count = 0

        # Initialize subsystems (must happen AFTER base state and players)
        # Create fresh instances linked to 'self' (this GameState instance)
        self._init_subsystems()

        # Link external systems if they were passed or previously set
        # Ensure subsystems have access to these if needed
        self.strategy_memory = getattr(self, 'strategy_memory', None)
        self.stats_tracker = getattr(self, 'stats_tracker', None)
        self.card_memory = getattr(self, 'card_memory', None)
        if self.card_evaluator:
             self.card_evaluator.stats_tracker = self.stats_tracker
             self.card_evaluator.card_memory = self.card_memory
        # Link Strategy Memory to Planner if both exist
        if self.strategic_planner and self.strategy_memory:
             self.strategic_planner.strategy_memory = self.strategy_memory

        # Final setup calls if subsystems exist
        if self.strategic_planner and hasattr(self.strategic_planner, 'init_after_reset'):
            self.strategic_planner.init_after_reset()

        if self.ability_handler and hasattr(self.ability_handler, '_initialize_abilities'):
             logging.debug("Initializing card abilities via AbilityHandler.")
             # Make sure card_db is properly set up before initializing abilities
             if isinstance(self.card_db, dict) and self.card_db:
                 self.ability_handler._initialize_abilities() # Parse abilities from DB
             else:
                 logging.error("Cannot initialize abilities: card_db is not a valid dictionary.")

        # Initial Layer application to set base characteristics correctly
        if self.layer_system:
            logging.debug("Applying initial layer effects after reset.")
            self.layer_system.apply_all_effects()

        logging.debug("GameState reset complete.")
    
    def _init_player(self, deck):
        """Initialize a player's state with a given deck and draw 7 cards for the starting hand."""
        import copy # Moved import inside

        if not deck:
            raise ValueError("Tried to initialize player with empty deck!")

        player = {
            "library": copy.deepcopy(deck), # Deep copy deck list
            "hand": [],
            "battlefield": [],
            "graveyard": [],
            "exile": [],
            "life": 20,
            "mana_pool": {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}, # Regular mana
            "conditional_mana": {}, # Restricted mana pools e.g. {'cast_creatures': {'G': 1}}
            "phase_restricted_mana": {}, # Mana that empties at phase end, not turn end
            "mana_production": {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0},
            "land_played": False,
            "tapped_permanents": set(), # Store IDs of tapped permanents
            "damage_counters": {}, # Track damage marked on creatures {card_id: amount}
            "plus_counters": defaultdict(int), # Track +1/+1 counters {card_id: count} - DEPRECATED, use card.counters
            "minus_counters": defaultdict(int), # Track -1/-1 counters {card_id: count} - DEPRECATED, use card.counters
            "deathtouch_damage": {}, # Track damage dealt by deathtouch sources {card_id: True}
            "entered_battlefield_this_turn": set(), # IDs of creatures that entered this turn
            "activated_this_turn": set(),         # IDs of cards whose abilities were activated this turn
            "pw_activations": {},                 # Track activations per planeswalker {card_id: count}
            "lost_life_this_turn": False,         # Flag if player lost life this turn (for Spectacle etc.)
            "attempted_draw_from_empty": False, # Flag if player tried to draw from empty library
            "poison_counters": 0, # For Infect/Poison mechanics
            "experience_counters": 0, # For experience counter mechanics
            "energy_counters": 0, # For energy mechanics
            "city_blessing": False, # For Ascend mechanic
            "monarch": False, # For Monarch mechanic
            "attachments": {}, # Track Equipment/Aura attachments {attach_id: target_id}
            "championed_cards": {}, # For Champion mechanic {champion_id: exiled_id}
            "ciphered_spells": {}, # For Cipher mechanic {creature_id: spell_id}
            "haunted_by": {}, # For Haunt mechanic {haunted_id: [haunter_id,...]}
            "hideaway_cards": {}, # For Hideaway mechanic {land_id: exiled_card_id}
            "mutation_stacks": {}, # For Mutate {base_creature_id: [top_card_id, ..., base_card_id]}
            "name": "Player?" # Placeholder name, will be set
            # ... other player-specific states ...
        }
        random.shuffle(player["library"])

        for _ in range(7):
            if player["library"]:
                player["hand"].append(player["library"].pop(0))
            else:
                logging.warning("Not enough cards in the deck to draw 7 cards!")
                break
        # Set name based on which player this is during reset
        if not hasattr(self, 'p1'): # If p1 doesn't exist yet, this is p1
             player["name"] = "Player 1"
        else: # Otherwise it's p2
             player["name"] = "Player 2"
        return player
        
    def track_card_played(self, card_id, player_idx):
        """Track when a card is played for statistics purposes"""
        # Create tracking dictionary if it doesn't exist
        if not hasattr(self, 'cards_played'):
            self.cards_played = {0: [], 1: []}
        
        # Add the card to the played list for the appropriate player
        player_idx = 0 if player_idx == self.p1 else 1
        self.cards_played[player_idx].append(card_id)
        
        # If stats tracker is available, inform it
        if hasattr(self, 'stats_tracker') and self.stats_tracker:
            # Just collect the data, actual stats will be processed at game end
            pass
    
    def initialize_turn_tracking(self):
        """Initialize turn phase tracking for keyword abilities"""
        gs = self
        
        # Create or reset turn tracking data
        gs.spells_cast_this_turn = []
        gs.attackers_this_turn = set()
        gs.damage_dealt_this_turn = {}
        gs.cards_drawn_this_turn = {gs.p1: 0, gs.p2: 0}
        
        # Reset any "until end of turn" effects tracking
        gs.until_end_of_turn_effects = {}
        
        logging.debug("Initialized turn tracking for keyword abilities")
        
    def track_mulligan(self, player, count=1):
        """Track mulligan decisions for statistics"""
        # Ensure mulligan_data exists
        if not hasattr(self, 'mulligan_data'):
            self.mulligan_data = {'p1': 0, 'p2': 0}
        
        # Update the appropriate counter
        if player == self.p1:
            self.mulligan_data['p1'] += count
        else:
            self.mulligan_data['p2'] += count
    
    def _untap_phase(self, player):
        """Reset mana and untap all permanents, handling Phasing."""
        # --- Phasing ---
        # 1. Phase In Permanents that should return
        if hasattr(self, 'phased_out'):
            permanents_phasing_in = []
            # Check player's phased-out permanents first
            player_phased_out = player.get("phased_out_permanents", set())
            for card_id in list(player_phased_out): # Iterate copy
                 if card_id in self.phased_out: # Confirm it's in global set
                      # Phase in logic: Remove from phased out, add to battlefield (untapped)
                      self.phased_out.remove(card_id)
                      player_phased_out.remove(card_id)
                      if card_id not in player.get("battlefield", []): # Avoid duplicates if already there somehow
                           player["battlefield"].append(card_id)
                           # Remove from tapped state (enters untapped)
                           player.get("tapped_permanents", set()).discard(card_id)
                           card = self._safe_get_card(card_id)
                           logging.debug(f"Phased in: {getattr(card, 'name', card_id)}")
                           self.trigger_ability(card_id, "PHASED_IN", {"controller": player})
                           permanents_phasing_in.append(card_id)

        # 2. Check Permanents with Phasing on Battlefield
        permanents_phasing_out = []
        for card_id in list(player.get("battlefield",[])): # Iterate copy
             card = self._safe_get_card(card_id)
             # Check keyword via Layer System result preferred
             if card and self.check_keyword(card_id, "phasing"):
                 permanents_phasing_out.append(card_id)

        # 3. Phase Out identified permanents
        if permanents_phasing_out:
            if not hasattr(self, 'phased_out'): self.phased_out = set() # Ensure set exists
            player_phased_out = player.setdefault("phased_out_permanents", set())
            for card_id in permanents_phasing_out:
                if card_id in player["battlefield"]: # Ensure it's still there
                    player["battlefield"].remove(card_id)
                    self.phased_out.add(card_id)
                    player_phased_out.add(card_id)
                    # Store state if needed (tapped, counters etc.) - simplified for now
                    card = self._safe_get_card(card_id)
                    logging.debug(f"Phased out: {getattr(card, 'name', card_id)}")
                    # Remove effects, etc.
                    if self.layer_system: self.layer_system.remove_effects_by_source(card_id)
                    if self.replacement_effects: self.replacement_effects.remove_effects_by_source(card_id)

        # --- Standard Untap Actions ---
        # Reset mana pools
        player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        player["conditional_mana"] = {}
        player["phase_restricted_mana"] = {}

        # Untap permanents *that did not phase out*
        untapped_ids = set()
        tapped_set = player.get("tapped_permanents", set())
        for card_id in list(player.get("battlefield", [])): # Iterate copy, only those currently on BF
            if card_id in tapped_set:
                 tapped_set.remove(card_id)
                 untapped_ids.add(card_id)
                 card = self._safe_get_card(card_id)
                 logging.debug(f"Untapped: {getattr(card, 'name', card_id)}")
                 self.trigger_ability(card_id, "UNTAPPED", {"controller": player})

        player["tapped_permanents"] = tapped_set # Update the set

        player["entered_battlefield_this_turn"] = set() # Clear sickness status
        player["land_played"] = False
        player["damage_counters"] = {} # Damage removed in Cleanup usually, but safe reset here? Rule 514.2. Okay.
        logging.debug(f"Untap Phase for {player['name']} complete.")
        
        



    def _draw_phase(self, player):
        """Draw a card from the library with replacement effect handling."""
        if player["library"]:
            # Create event context
            draw_context = {
                "player": player,
                "draw_count": 1,
                "card_id": player["library"][0] if player["library"] else None
            }
            
            # Apply replacement effects
            modified_context, was_replaced = self.apply_replacement_effect("DRAW", draw_context)
            
            if was_replaced:
                # Use the modified context
                # The replacement effect handler already performed the action
                pass
            else:
                # Normal draw
                card_id = player["library"].pop(0)
                player["hand"].append(card_id)
                
                # Get the card object properly using _safe_get_card
                card = self._safe_get_card(card_id)
                
                # Attempt to handle miracle if applicable
                miracle_handled = False
                if hasattr(self, 'handle_miracle_draw'):
                    miracle_handled = self.handle_miracle_draw(card_id, player)
                
                # Only attempt to log the card name if we got a valid card object
                if card:
                    logging.debug(f"Draw Phase: Drew {card.name}{' and cast for miracle cost' if miracle_handled else ''}")
                else:
                    logging.debug(f"Draw Phase: Drew card ID {card_id}")
        else:
            # Track attempted draw from empty
            player["attempted_draw_from_empty"] = True
            logging.warning("Draw Phase: No cards left in library. Player loses the game!")
            player["life"] = 0  # Losing condition: drawing from an empty library
            self.check_state_based_actions()

    
    def _end_phase(self, player):
        """Cleanup at end phase."""
        player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        # Enforce hand size limits, etc.
        if len(player["hand"]) > self.max_hand_size:
            player["hand"] = player["hand"][:self.max_hand_size]
        # Revert any temporary control effects at end of turn
        self._revert_temporary_control()
            
    def apply_layer_effects(self):
        """Apply all continuous effects in the proper layer order."""
        if self.layer_system:
            self.layer_system.apply_all_effects()
            self.check_state_based_actions()
            
    def apply_temporary_control(self, card_id, new_controller):
        """
        Grant temporary control of a card until end of turn.
        
        Args:
            card_id: ID of the card to control temporarily.
            new_controller: The player dictionary who will temporarily control the card.
        
        Returns:
            bool: True if the effect is applied successfully.
        """
        original_controller = self.find_card_location(card_id)
        if original_controller is None:
            logging.warning(f"Temporary control: Original owner not found for card {card_id}.")
            return False
        # Record the original controller if not already stored
        if card_id not in self.temp_control_effects:
            self.temp_control_effects[card_id] = original_controller
        # Remove the card from its current controller's battlefield
        for player in [self.p1, self.p2]:
            if card_id in player["battlefield"]:
                player["battlefield"].remove(card_id)
        # Add the card to the new controller's battlefield
        new_controller["battlefield"].append(card_id)
        logging.debug(f"Temporary control: {new_controller['name']} now controls {self._safe_get_card(card_id).name} until end of turn.")
        return
    
    def get_party_count(self, battlefield):
        """
        Calculate party count (Clerics, Rogues, Warriors, and Wizards).
        Used for the Party mechanic from Zendikar Rising.
        
        Args:
            battlefield: List of card IDs on battlefield to check for party members
            
        Returns:
            int: Number of different party classes (max 4)
        """
        party_classes = {"cleric", "rogue", "warrior", "wizard"}
        found_classes = set()
        
        for card_id in battlefield:
            card = self._safe_get_card(card_id)
            if not card or not hasattr(card, 'subtypes'):
                continue
                
            # Check if card is on battlefield and is a creature
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                # Check for party classes
                card_subtypes = {subtype.lower() for subtype in card.subtypes}
                found_party_classes = party_classes.intersection(card_subtypes)
                found_classes.update(found_party_classes)
        
        # Return the count of different party classes (max 4)
        return min(len(found_classes), 4)
    
    def get_all_creatures(self, player=None):
        """
        Get IDs of all creatures on the battlefield.
        If player is specified, only returns creatures that player controls.
        
        Args:
            player: Optional player to filter by controller
            
        Returns:
            list: IDs of creature cards
        """
        creature_ids = []
        
        if player:
            players = [player]
        else:
            players = [self.p1, self.p2]
            
        for p in players:
            for card_id in p.get("battlefield", []):
                card = self._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    creature_ids.append(card_id)
                    
        return creature_ids
    
    def find_card_location(self, card_id):
        """
        Find which player controls a card and in which zone it is.
        This is a unified method to be used by both GameState and LayerSystem.
        
        Args:
            card_id: ID of the card to locate
            
        Returns:
            tuple: (player, zone) or None if not found
        """
        zones = ["battlefield", "hand", "graveyard", "exile", "library"]
        
        for player in [self.p1, self.p2]:
            for zone in zones:
                if zone in player and card_id in player[zone]:
                    return player, zone
                    
        # Check special zones like the stack
        for item in self.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == card_id:
                return item[2], "stack"  # Return the controller and "stack" zone
        
        # Check other special tracking sets/dicts
        special_zones = [
            ("adventure_cards", "adventure_zone"),
            ("phased_out", "phased_out"),
            ("foretold_cards", "foretold_zone"),
            ("suspended_cards", "suspended")
        ]
        
        for attr_name, zone_name in special_zones:
            if hasattr(self, attr_name):
                attr = getattr(self, attr_name)
                if isinstance(attr, set) and card_id in attr:
                    # Try to determine the controller
                    for player in [self.p1, self.p2]:
                        # Check if player has this in any of their tracked special zones
                        if hasattr(player, attr_name) and card_id in getattr(player, attr_name):
                            return player, zone_name
                    # If we can't determine controller, return p1 as default
                    return self.p1, zone_name
                elif isinstance(attr, dict) and card_id in attr:
                    # For dict-based tracking, the value might contain controller info
                    if "controller" in attr[card_id]:
                        return attr[card_id]["controller"], zone_name
                    # If no controller info, default to p1
                    return self.p1, zone_name
                    
        return None
        
    def _revert_temporary_control(self):
        """
        Revert any temporary control effects, returning cards to their original controllers.
        This should be called at the end of the turn.
        """
        for card_id, original_controller in list(self.temp_control_effects.items()):
            current_controller = self.find_card_location(card_id)
            if current_controller and current_controller != original_controller:
                # Remove from current controller's battlefield
                if card_id in current_controller["battlefield"]:
                    current_controller["battlefield"].remove(card_id)
                # Return card to original controller's battlefield
                original_controller["battlefield"].append(card_id)
                logging.debug(f"Temporary control: Reverted control of {self._safe_get_card(card_id).name} back to {original_controller['name']}.")
            # Remove the effect record
            del self.temp_control_effects[card_id]
            
    def apply_replacement_effect(self, event_type, event_context):
        """
        Apply any applicable replacement effects to an event.
        
        Args:
            event_type: The type of event (e.g., 'DRAW', 'DAMAGE', 'DIES')
            event_context: Dictionary with event information
            
        Returns:
            tuple: (modified_context, was_replaced)
        """
        # If the game state doesn't have a replacement effect system, create one
        if not hasattr(self, 'replacement_effects') or self.replacement_effects is None:
            try:
                from .replacement_effects import ReplacementEffectSystem
                self.replacement_effects = ReplacementEffectSystem(self)
            except ImportError:
                # If module not available, return unmodified context
                return event_context, False
        
        # Apply replacement effects if available
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            return self.replacement_effects.apply_replacements(event_type, event_context)
        else:
            # If no replacement effects system, just return the original context
            return event_context, False
        
    def register_continuous_effect(self, effect_data):
        """Register a continuous effect with the layer system."""
        if self.layer_system:
            return self.layer_system.register_effect(effect_data)
        return None
        
    def register_replacement_effect(self, effect_data):
        """Register a replacement effect."""
        if self.replacement_effects:
            return self.replacement_effects.register_effect(effect_data)
        return None
    
    
    def add_defense_counter(self, card_id, count=1):
        """
        Add defense counters to a battle card.
        
        Args:
            card_id: ID of the battle card
            count: Number of counters to add (can be negative to remove)
            
        Returns:
            bool: Success status
        """
        # Find the card owner
        card_owner = None
        for player in [self.p1, self.p2]:
            if card_id in player["battlefield"]:
                card_owner = player
                break
        
        if not card_owner:
            logging.warning(f"Cannot add defense counter to card {card_id} - not on battlefield")
            return False
        
        # Get the card
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'type_line') or 'battle' not in card.type_line.lower():
            logging.warning(f"Card {card_id} is not a battle card")
            return False
        
        # Initialize battle defense tracking
        if not hasattr(self, 'battle_cards'):
            self.battle_cards = {}
        
        # Add or remove defense counters
        current_defense = self.battle_cards.get(card_id, 0)
        new_defense = max(0, current_defense + count)  # Cannot go below 0
        self.battle_cards[card_id] = new_defense
        
        logging.debug(f"Changed defense counters on battle card {card.name} by {count}, now has {new_defense}")
        
        # If defense reaches 0, sacrifice the battle
        if new_defense == 0:
            self.move_card(card_id, card_owner, "battlefield", card_owner, "graveyard")
            logging.debug(f"Battle card {card.name} lost all defense counters and was sacrificed")
        
        return True

    def reduce_battle_defense(self, card_id, amount=1):
        """
        Reduce defense counters on a battle card.
        
        Args:
            card_id: ID of the battle card
            amount: Number of defense counters to remove
            
        Returns:
            bool: Success status
        """
        return self.add_defense_counter(card_id, -amount)

    def perform_mulligan(self, player, keep_hand=False):
        """
        Implement the London Mulligan rule, allowing the AI to decide whether to keep or mulligan.
        
        Args:
            player: Player who is taking a mulligan
            keep_hand: Whether to keep the current hand or mulligan
            
        Returns:
            bool: Whether a mulligan was performed
        """
        # If the player decides to keep their hand
        if keep_hand:
            # If they've taken mulligans, they need to bottom cards
            mulligan_count = self.mulligan_count.get('p1' if player == self.p1 else 'p2', 0)
            
            if mulligan_count > 0:
                # Set state for bottoming decisions
                cards_to_bottom = mulligan_count
                self.bottoming_in_progress = True
                self.bottoming_player = player
                self.cards_to_bottom = cards_to_bottom
                self.bottoming_count = 0
                
                logging.debug(f"Player keeping hand after {mulligan_count} mulligan(s), needs to bottom {cards_to_bottom} card(s)")
                
                return False  # No mulligan performed, in bottoming phase now
            else:
                # First hand kept, no bottoming needed
                self.mulligan_in_progress = False
                logging.debug("Player kept initial hand, no mulligan taken")
                return False
        
        # Track mulligan in statistics
        self.track_mulligan(player)
        
        # Count current hand as mulligan number
        player_idx = 'p1' if player == self.p1 else 'p2'
        self.mulligan_count[player_idx] = self.mulligan_count.get(player_idx, 0) + 1
        mulligan_count = self.mulligan_count[player_idx]
        
        # Return current hand to library
        player["library"].extend(player["hand"])
        player["hand"] = []
        
        # Shuffle
        random.shuffle(player["library"])
        
        # Always draw 7 cards
        for _ in range(7):
            if player["library"]:
                player["hand"].append(player["library"].pop(0))
            else:
                logging.warning("Not enough cards in library to complete mulligan")
                break
        
        # Keep mulligan in progress
        self.mulligan_in_progress = True
        
        logging.debug(f"Player took mulligan #{mulligan_count}, drew new hand of {len(player['hand'])} cards")
        
        return True  # Mulligan was performed
    
    def _can_respond_to_stack(self, player=None):
        """
        Check if the player can respond to the stack, considering effects like Split Second.
        
        Args:
            player: The player who is trying to respond
            
        Returns:
            bool: Whether the player can respond to the stack
        """
        # If Split Second is active, no player can respond
        if hasattr(self, 'split_second_active') and self.split_second_active:
            return False
            
        # Otherwise, check normal priority rules
        return self.check_priority(player)

    def _pass_priority(self):
        """Handle passing priority between players or advancing state."""
        gs = self # Alias for convenience

        # Check if anyone actually has priority
        if gs.priority_player is None:
            # This might happen during resolution or untap/cleanup where no priority exists yet.
            # If stack empty, we might need to assign priority to AP to start the next step.
            if not gs.stack and gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]:
                 gs.priority_player = gs._get_active_player()
                 gs.priority_pass_count = 0 # Reset count when assigning priority
                 logging.debug(f"Assigned priority to AP ({gs.priority_player['name']}) as it was None.")
            else:
                 # If stack not empty, or in untap/cleanup, just return. State progresses differently.
                 logging.debug("Pass priority called when no player had priority (expected during resolution/untap/cleanup).")
                 return # Let other game loop mechanics handle state change

        # --- Standard Priority Passing ---
        gs.priority_pass_count += 1
        current_prio_player = gs.priority_player
        next_prio_player = gs._get_non_active_player() if current_prio_player == gs._get_active_player() else gs._get_active_player()
        gs.priority_player = next_prio_player
        logging.debug(f"Priority passed from {current_prio_player['name']} to {next_prio_player['name']} (Pass #{gs.priority_pass_count})")

        # --- Check for Stack Resolution or Phase Advance ---
        if gs.priority_pass_count >= 2:
            # --- Split Second Check ---
            if getattr(gs, 'split_second_active', False):
                # Find the Split Second spell
                split_second_item = None
                for item in gs.stack:
                    if isinstance(item, tuple) and len(item) > 3 and item[3].get("is_split_second", False):
                        split_second_item = item
                        break
                # If SS spell still exists, resolve it. Nothing else can be added.
                if split_second_item:
                    logging.debug("Split Second active: Resolving split second spell/ability.")
                    resolved = gs.resolve_top_of_stack() # Should resolve the SS item
                    # resolve_top_of_stack resets priority to AP. Split second ends AFTER resolution.
                    if resolved:
                        logging.debug("Split Second spell resolved.")
                        # Mark split second as no longer active AFTER resolution
                        gs.split_second_active = False
                    else:
                         logging.warning("Split second resolution failed. Game state might be inconsistent.")
                    # Do not proceed to normal stack/phase logic yet. Loop continues.
                else: # SS was active, but spell is gone? Error state. Clear flag.
                    logging.warning("Split Second was active, but no corresponding spell found on stack.")
                    gs.split_second_active = False
                    # Fall through to normal check? Risky. For now, clear pass count and return.
                    gs.priority_player = gs._get_active_player()
                    gs.priority_pass_count = 0
                    return
            # --- Regular Stack Resolution ---
            elif gs.stack:
                # Process triggers FIRST that might have been put on queue by passing priority (unlikely but possible)
                # Example: Ability says "Whenever a player passes priority..."
                if gs.ability_handler: triggers_processed = gs.ability_handler.process_triggered_abilities()
                else: triggers_processed = False

                # If triggers were added, reset priority
                if len(gs.stack) > gs.last_stack_size:
                    gs.priority_player = gs._get_active_player()
                    gs.priority_pass_count = 0
                    gs.last_stack_size = len(gs.stack)
                    logging.debug("Triggers added to stack after priority pass, priority back to AP.")
                else: # No new triggers, resolve the stack
                    logging.debug("Both passed, resolving stack...")
                    resolved = gs.resolve_top_of_stack() # This resets priority to AP internally
                    # SBAs are checked after resolution within the main loop, not here
                    # --- MODIFIED: Check if choice context still needs handling ---
                    if not gs.stack and (gs.targeting_context or gs.sacrifice_context or gs.choice_context):
                         logging.debug("Stack resolved, but special context remains. Priority to choosing player.")
                         if gs.targeting_context: gs.priority_player = gs.targeting_context.get("controller")
                         elif gs.sacrifice_context: gs.priority_player = gs.sacrifice_context.get("controller")
                         elif gs.choice_context: gs.priority_player = gs.choice_context.get("player")
                         else: gs.priority_player = gs._get_active_player() # Fallback
                         gs.priority_pass_count = 0 # Player needs to act on choice
                         self.phase = gs.targeting_context and self.PHASE_TARGETING or \
                                      gs.sacrifice_context and self.PHASE_SACRIFICE or \
                                      gs.choice_context and self.PHASE_CHOOSE or self.PHASE_PRIORITY
                    # --- END MODIFICATION ---
                    # else: resolve_top_of_stack handled phase return if needed
            # --- Advance Phase ---
            # --- ADDED CHECK FOR PENDING CHOICE BEFORE ADVANCING ---
            elif not (gs.targeting_context or gs.sacrifice_context or gs.choice_context):
            # --- END ADDED CHECK ---
                 logging.debug("Both passed with empty stack, advancing phase.")
                 gs._advance_phase()
                 # _advance_phase handles resetting priority_player and pass_count
            else:
                logging.debug("Both passed, but choice context pending. Waiting for action.")
                # Keep priority with the player who needs to choose
                if gs.targeting_context: gs.priority_player = gs.targeting_context.get("controller")
                elif gs.sacrifice_context: gs.priority_player = gs.sacrifice_context.get("controller")
                elif gs.choice_context: gs.priority_player = gs.choice_context.get("player")
                gs.priority_pass_count = 0 # Reset passes, player must act

        # else: # Only one pass, priority toggled, nothing else happens

    def move_card(self, card_id, from_player, from_zone, to_player, to_zone, cause=None, context=None):
        """Move a card between zones, applying replacement effects and triggering abilities, handling Madness."""
        if context is None: context = {}
        card = self._safe_get_card(card_id)
        card_name = getattr(card, 'name', f"Card {card_id}") if card else f"Card {card_id}"
        original_from_zone = from_zone # Keep original source for context

        # 1. Handle Implicit Zones & Validate Source
        source_list = None
        actual_from_zone = from_zone
        # Handle placeholder zones used when the exact zone is unknown/not needed for source removal
        if from_zone == "stack_implicit": actual_from_zone = "stack"; source_list = [] # Removed from stack by caller
        elif from_zone == "library_implicit": actual_from_zone = "library"; source_list = [] # Removed from library by caller
        elif from_zone == "hand_implicit": actual_from_zone = "hand"; source_list = [] # Removed from hand by caller
        elif from_zone == "nonexistent_zone": actual_from_zone = "nonexistent"; source_list = [] # Not actually removed from anywhere (e.g., token creation)
        else: # A specific, known zone
             source_list = from_player.get(actual_from_zone) if from_player else None
             if source_list is None:
                 logging.warning(f"Cannot move {card_name}: Invalid source zone '{actual_from_zone}'.")
                 return False
             # Check if actually present (handle list/set)
             is_present = False
             if isinstance(source_list, (list, set)) and card_id in source_list: is_present = True
             elif isinstance(source_list, dict) and card_id in source_list: is_present = True

             if not is_present:
                  current_location_player, current_location_zone = self.find_card_location(card_id)
                  if current_location_zone:
                      logging.warning(f"Cannot move {card_name}: Expected in {actual_from_zone} but found in {current_location_zone}.")
                  else:
                      logging.warning(f"Cannot move {card_name}: Not found in {from_player['name']}'s {actual_from_zone} or anywhere.")
                  return False

        # --- Apply Replacement Effects (including potential Madness redirect) ---
        final_destination_player = to_player
        final_destination_zone = to_zone
        event_context = {
             'card_id': card_id, 'card': card,
             'from_player': from_player, 'from_zone': actual_from_zone,
             'to_player': to_player, 'to_zone': to_zone,
             'cause': cause, **context
        }
        prevented = False
        madness_applied = False
        madness_cost_found = None

        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            # --- MADNESS CHECK (DISCARD) ---
            is_discard_event = (actual_from_zone == "hand" and to_zone == "graveyard" and cause == "discard")
            has_madness = card and "madness" in getattr(card, 'oracle_text', '').lower()
            # Madness replacement handling is now primarily within apply_replacements

            # --- Standard Leave/Enter Replacements ---
            # Apply LEAVE_<ZONE> replacement effects
            leave_event = f"LEAVE_{actual_from_zone.upper()}"
            modified_leave_ctx, replaced_leave = self.replacement_effects.apply_replacements(leave_event, event_context.copy())
            if replaced_leave:
                 event_context.update(modified_leave_ctx)
                 final_destination_player = event_context.get('to_player', final_destination_player)
                 final_destination_zone = event_context.get('to_zone', final_destination_zone)
                 prevented = event_context.get('prevented', False)
                 logging.debug(f"Leave replacement applied: New Dest: {final_destination_zone}, Prevented: {prevented}")

            # If not prevented, check ENTER_<ZONE> replacement effects for the *potential* destination
            if not prevented:
                 enter_event = f"ENTER_{final_destination_zone.upper()}"
                 modified_enter_ctx, replaced_enter = self.replacement_effects.apply_replacements(enter_event, event_context.copy())
                 if replaced_enter:
                      final_destination_player = modified_enter_ctx.get('to_player', final_destination_player)
                      final_destination_zone = modified_enter_ctx.get('to_zone', final_destination_zone)
                      prevented = modified_enter_ctx.get('prevented', False)
                      # Merge ETB flags/counters from context if they were added by the replacement
                      if 'enters_tapped' in modified_enter_ctx: context['enters_tapped'] = modified_enter_ctx['enters_tapped']
                      if 'enter_counters' in modified_enter_ctx: context.setdefault('enter_counters', []).extend(modified_enter_ctx['enter_counters'])
                      logging.debug(f"Enter replacement applied: Final Dest: {final_destination_zone}, Prevented: {prevented}")

        # --- Final Prevention Check ---
        if prevented:
            logging.debug(f"Movement of {card_name} from {actual_from_zone} to {final_destination_zone} prevented by replacement effect.")
            return False

        # --- Check if Madness was the outcome ---
        # Check if this specific card was flagged by a Madness replacement during apply_replacements
        # This requires apply_replacements to add information to the final context if Madness was chosen.
        if is_discard_event and has_madness and final_destination_zone == 'exile':
             # Double-check if madness state was correctly set up by replacement effect
             if hasattr(self,'madness_cast_available') and getattr(self,'madness_cast_available',{}).get('card_id') == card_id:
                 madness_applied = True
                 madness_cost_found = getattr(self,'madness_cast_available',{}).get('cost')

        # --- Perform Actual Move ---
        # 1. Remove from source zone (unless implicitly removed)
        if source_list is not None and original_from_zone not in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone"]:
            removed = False
            # Check actual source list stored in the player dictionary
            source_list_live = from_player.get(actual_from_zone)
            if source_list_live is not None:
                 if isinstance(source_list_live, list) and card_id in source_list_live: source_list_live.remove(card_id); removed = True
                 elif isinstance(source_list_live, set) and card_id in source_list_live: source_list_live.discard(card_id); removed = True
                 elif isinstance(source_list_live, dict) and card_id in source_list_live: del source_list_live[card_id]; removed = True

            #if not removed: Log warning only if removal failed unexpectedly

        # 2. Handle "leaves the battlefield" cleanup & triggers (This is the crucial part updated)
        if actual_from_zone == "battlefield" and from_player:
            # --- Start LTB Cleanup/Trigger Sequence ---
            ltb_trigger_context = {
                'controller': from_player, # Controller when it left
                'from_zone': actual_from_zone,
                'to_zone': final_destination_zone,
                'cause': cause, **context
            }
            # a. Trigger LTB abilities first (Rule 603.6c)
            self.trigger_ability(card_id, "LEAVE_BATTLEFIELD", ltb_trigger_context)

            # b. Cleanup State Associated with the Permanent (Immediately after LTB triggers)
            logging.debug(f"Cleaning up state for {card_name} ({card_id}) leaving battlefield.")
            from_player.get("tapped_permanents", set()).discard(card_id)
            from_player.get("entered_battlefield_this_turn", set()).discard(card_id) # Clear summoning sickness if it just entered

            # Cleanup attachments (what it's attached to, or what's attached to it)
            attachments = from_player.get("attachments")
            if attachments:
                # If it's an Equip/Aura/Fort itself
                if card_id in attachments: del attachments[card_id]
                # If other things were attached to it
                items_attached_to_it = [att_id for att_id, target_id in list(attachments.items()) if target_id == card_id]
                for att_id in items_attached_to_it: del attachments[att_id] # Unattach Aura/Equip/Fort

            # Remove other counters/statuses associated with the card ID
            if hasattr(from_player, 'loyalty_counters'): from_player['loyalty_counters'].pop(card_id, None)
            if hasattr(from_player, 'damage_counters'): from_player['damage_counters'].pop(card_id, None)
            if hasattr(from_player, 'deathtouch_damage'): from_player.get('deathtouch_damage', {}).pop(card_id, None)
            if hasattr(self, 'saga_counters'): self.saga_counters.pop(card_id, None)
            if hasattr(self, 'battle_cards'): self.battle_cards.pop(card_id, None) # Remove defense counters for battles
            if hasattr(from_player, 'regeneration_shields'): from_player['regeneration_shields'].discard(card_id)
            # Clear mutation stack if it was the base
            if hasattr(from_player, 'mutation_stacks') and card_id in from_player['mutation_stacks']: del from_player['mutation_stacks'][card_id]
            # Add other cleanups as needed (champion, cipher, haunt, hideaway etc.)

            # Remove continuous effects from this source (LayerSystem)
            if self.layer_system: self.layer_system.remove_effects_by_source(card_id)
            # Remove replacement effects from this source
            if self.replacement_effects: self.replacement_effects.remove_effects_by_source(card_id)
            # Unregister abilities associated with this card
            if self.ability_handler: self.ability_handler.unregister_card_abilities(card_id)

            # *** CRITICAL: Reset the card object state ***
            if card and hasattr(card, 'reset_state_on_zone_change'):
                 logging.debug(f"Calling reset_state_on_zone_change for {card_name} ({card_id}).")
                 card.reset_state_on_zone_change()
            # --- End LTB Cleanup/Trigger Sequence ---

        # 3. Add to destination zone
        destination_list = final_destination_player.get(final_destination_zone)
        if destination_list is None:
             logging.error(f"Invalid destination zone '{final_destination_zone}' for player {final_destination_player['name']}.")
             return False # Cannot recover card state easily
        # Check if list or set, prevent duplicates appropriately
        if isinstance(destination_list, list) and card_id not in destination_list:
            destination_list.append(card_id)
        elif isinstance(destination_list, set) and card_id not in destination_list:
             destination_list.add(card_id)
        elif not isinstance(destination_list, (list, set)): # Safety check for unexpected zone types
             logging.error(f"Destination zone '{final_destination_zone}' is not a list or set ({type(destination_list)}).")
             return False

        logging.debug(f"Moved {card_name} from {from_player['name'] if from_player else 'N/A'}'s {actual_from_zone} to {final_destination_player['name']}'s {final_destination_zone}{' (via Madness)' if madness_applied else ''}")

        # --- Trigger ENTER Abilities & Handle ETB Effects ---
        enter_trigger_context = {
            'controller': final_destination_player, # Controller *now*
            'from_zone': actual_from_zone,
            'to_zone': final_destination_zone,
            'cause': cause, **context
        }
        if final_destination_zone == "battlefield":
             # Ensure base P/T are set from DB or default before potential ETB counter effects
             # (Reset should handle this, but double check)
             if card and getattr(card, 'power', None) is None and 'creature' in getattr(card,'card_types',[]): card.power = 0
             if card and getattr(card, 'toughness', None) is None and 'creature' in getattr(card,'card_types',[]): card.toughness = 0

             final_destination_player.setdefault("entered_battlefield_this_turn", set()).add(card_id)
             enters_tapped = context.get('enters_tapped', False) or (hasattr(card, 'oracle_text') and "enters the battlefield tapped" in card.oracle_text.lower())
             if enters_tapped: final_destination_player.setdefault("tapped_permanents", set()).add(card_id)
             # Apply counters passed via context from replacement effects or "as enters"
             enter_counters = context.get('enter_counters')
             if enter_counters and isinstance(enter_counters, list):
                 for counter_info in enter_counters: self.add_counter(card_id, counter_info['type'], counter_info['count'])
             # Saga entry counters
             if card and 'saga' in getattr(card,'subtypes',[]): self.add_counter(card_id, "lore", 1)
             # Planeswalker entry loyalty
             if card and 'planeswalker' in getattr(card,'card_types',[]):
                  base_loyalty = getattr(card, 'loyalty', 0) # Get base from card object
                  # Check replacements that might modify starting loyalty? Rare.
                  final_destination_player.setdefault("loyalty_counters", {})[card_id] = base_loyalty
             # Battle entry defense
             if card and 'battle' in getattr(card,'type_line','').lower():
                  base_defense = getattr(card, 'defense', 0) # Get base defense
                  if not hasattr(self,'battle_cards'): self.battle_cards = {}
                  self.battle_cards[card_id] = base_defense


             # Register card effects (static, replacement) - Needs AbilityHandler
             if card and self.ability_handler:
                 self.ability_handler.register_card_abilities(card_id, final_destination_player) # Pass current controller

             # Trigger ETB abilities
             self.trigger_ability(card_id, "ENTERS_BATTLEFIELD", enter_trigger_context) # Standard ETB
             # Trigger Landfall if it's a land
             if card and 'land' in getattr(card,'card_types',[]):
                 # Trigger for player who played the land
                 self.trigger_ability(None, "LANDFALL", enter_trigger_context) # Trigger general landfall for player
                 # Trigger ability on the land itself if it has one
                 self.trigger_ability(card_id, "LANDFALL_SELF", enter_trigger_context) # Specific landfall for this land


             # Special ETB handlers
             if card and 'aura' in getattr(card, 'subtypes',[]):
                  # Handle attachment based on context target (or needs choice phase)
                  self._resolve_aura_attachment(card_id, final_destination_player, context)
                  # Ensure layer recalculation if Aura attached
                  if self.layer_system: self.layer_system.invalidate_cache()

        else: # Entering non-battlefield zone
             self.trigger_ability(card_id, f"ENTER_{final_destination_zone.upper()}", enter_trigger_context)
             # Track cards entering graveyard this turn
             if final_destination_zone == "graveyard":
                  turn_gy_list = self.cards_to_graveyard_this_turn.setdefault(self.turn, [])
                  turn_gy_list.append(card_id)
                  # Gravestorm count increases if card came from battlefield (per Rule 702.61a)
                  if actual_from_zone == "battlefield":
                      self.gravestorm_count = self.gravestorm_count + 1 if hasattr(self, 'gravestorm_count') else 1


        # --- Post-Move Cleanup ---
        # Handle Tokens Ceasing to Exist if moved off battlefield
        # Must use the card object BEFORE reset state was called if it's a token
        card_was_token = hasattr(card, 'is_token') and card.is_token # Check before potentially losing token status in reset?
        if card_was_token and actual_from_zone == "battlefield" and final_destination_zone != "battlefield":
            logging.debug(f"Token {card_name} ceased to exist after moving to {final_destination_zone}.")
            # Remove from destination zone list/set
            dest_list = final_destination_player.get(final_destination_zone)
            if dest_list and card_id in dest_list:
                if isinstance(dest_list, list): dest_list.remove(card_id)
                elif isinstance(dest_list, set): dest_list.discard(card_id)
            # Remove from card_db
            if card_id in self.card_db: del self.card_db[card_id]

        # Clear lingering Madness opportunity if card moved FROM exile for another reason
        if actual_from_zone == "exile" and not madness_applied and getattr(self, 'madness_cast_available', None) and self.madness_cast_available.get('card_id') == card_id:
            logging.debug(f"Clearing Madness opportunity for {card_name} as it moved from exile.")
            self.madness_cast_available = None

        # Trigger ability processing happens AFTER the action completes (in main loop)
        return True
    
    def _register_card_effects(self, card_id, card, player):
        """Register static and replacement effects originating from a card."""
        # Register static abilities via AbilityHandler if they exist
        if self.ability_handler:
            abilities = self.ability_handler.registered_abilities.get(card_id, [])
            for ability in abilities:
                if isinstance(ability, StaticAbility):
                     # StaticAbility.apply() handles registration with LayerSystem
                     ability.apply(self) # Pass GameState

        # Register replacement effects
        if self.replacement_effects:
            self.replacement_effects.register_card_replacement_effects(card_id, player)
    
    def record_strategy_pattern(self, action_idx, reward):
        """Record the current strategy pattern and action."""
        if hasattr(self, 'strategy_memory'):
            try:
                # Extract pattern
                pattern = self.strategy_memory.extract_strategy_pattern(self)
                
                # Update strategy with reward
                self.strategy_memory.update_strategy(pattern, reward)
                
                # Record action sequence
                if not hasattr(self, 'current_action_sequence'):
                    self.current_action_sequence = []
                    
                self.current_action_sequence.append(action_idx)
                
                # Periodically save strategy memory
                if random.random() < 0.1:  # 10% chance each time
                    self.strategy_memory.save_memory()
                    
            except Exception as e:
                logging.error(f"Error recording strategy pattern: {str(e)}")
                
    def handle_control_changing_effect(self, source_card_id, target_card_id, duration="end_of_turn"):
        """
        Implement a control-changing effect from source card to target card.
        
        Args:
            source_card_id: ID of the card creating the control effect
            target_card_id: ID of the card being controlled
            duration: How long the control effect lasts
            
        Returns:
            bool: Whether the control effect was successfully applied
        """
        # Find the controller of the source card
        source_controller = None
        for player in [self.p1, self.p2]:
            if source_card_id in player["battlefield"]:
                source_controller = player
                break
                
        if not source_controller:
            logging.warning(f"Control effect: Source card {source_card_id} not found on battlefield")
            return False
        
        # Find the current controller of the target
        target_controller = self.find_card_location(target_card_id)
        if not target_controller or target_controller == source_controller:
            return False  # Already controlled by source or not found
            
        # Apply the temporary control effect
        success = self.apply_temporary_control(target_card_id, source_controller)
        
        # Register in layer system if available
        if success and hasattr(self, 'layer_system') and self.layer_system:
            self.layer_system.register_effect({
                'source_id': source_card_id,
                'layer': 2,  # Control-changing effects are layer 2
                'affected_ids': [target_card_id],
                'effect_type': 'change_control',
                'effect_value': source_controller,
                'duration': duration,
                'start_turn': self.turn
            })
        
        return success

    def activate_planeswalker_ability(self, card_id, ability_idx, controller):
        """Activates a planeswalker ability: Pays cost, potentially enters targeting, adds ability to stack."""
        card = self._safe_get_card(card_id)
        # Ensure card exists, is a planeswalker, and is on the controller's battlefield
        if not card or 'planeswalker' not in getattr(card, 'card_types', []) or card_id not in controller.get('battlefield', []):
            logging.warning(f"Invalid attempt to activate PW ability: Card {card_id} invalid or not controlled PW.")
            return False

        # Check activation limit (only once per turn per PW)
        activated_this_turn_set = controller.setdefault("activated_this_turn", set())
        if card_id in activated_this_turn_set:
            logging.debug(f"Planeswalker {card.name} ({card_id}) already activated this turn.")
            return False

        abilities = getattr(card, 'loyalty_abilities', [])
        if not (0 <= ability_idx < len(abilities)):
            logging.warning(f"Invalid ability index {ability_idx} for {card.name}")
            return False

        ability = abilities[ability_idx]
        cost = ability.get('cost', 0)
        effect_text = ability.get("effect", "")

        # Check loyalty affordability (Rule 118.5)
        current_loyalty = controller.get("loyalty_counters", {}).get(card_id, getattr(card, 'loyalty', 0))
        if current_loyalty + cost < 0: # Rule 118.5: Cannot pay cost if loyalty would become < 0
             logging.debug(f"Cannot activate PW ability for {card.name}: Loyalty {current_loyalty} + Cost {cost} < 0")
             return False

        # --- Costs are paid upon ACTIVATION (Rule 601.2h) ---
        # Pay loyalty cost
        new_loyalty = current_loyalty + cost
        controller.setdefault("loyalty_counters", {})[card_id] = new_loyalty

        # Mark as activated this turn
        activated_this_turn_set.add(card_id)
        # Increment total activations if tracked
        controller.setdefault("pw_activations", {})[card_id] = controller.get("pw_activations", {}).get(card_id, 0) + 1

        logging.debug(f"Paid loyalty cost ({cost:+}) for PW ability {ability_idx} on {card.name}. Loyalty now {new_loyalty}")

        # --- Targeting Setup ---
        requires_target = "target" in effect_text.lower()
        if requires_target:
             # Ability needs targets, set up targeting phase
             logging.debug(f"Planeswalker ability requires target. Entering TARGETING phase.")
             self.previous_priority_phase = self.phase # Store current phase
             self.phase = self.PHASE_TARGETING
             # Create targeting context
             self.targeting_context = {
                  "source_id": card_id,
                  "controller": controller,
                  "ability_idx": ability_idx, # Store index if needed later
                  "effect_text": effect_text,
                  "required_type": self._get_target_type_from_text(effect_text), # Use helper
                  "required_count": 1, # Assume 1 target unless text specifies more
                  "min_targets": 1, # Assumes target is required if text says 'target'
                  "selected_targets": [],
                  # Store info needed to put on stack AFTER targeting
                  "stack_info": {
                       "item_type": "ABILITY",
                       "source_id": card_id,
                       "controller": controller,
                       "context": {
                            "ability_index": ability_idx, # Include original index if needed
                            "ability_cost": cost,
                            "effect_text": effect_text,
                            "targets": {} # To be filled by targeting resolution
                       }
                  }
             }
             # Do NOT add to stack yet. Targeting actions will lead to stack addition.
             logging.debug(f"Set up targeting for PW ability: {effect_text}")

        else:
             # No targets needed, add ability directly to stack
             stack_context = {
                  "ability_index": ability_idx,
                  "ability_cost": cost,
                  "effect_text": effect_text,
                  "targets": {} # Empty targets dict
             }
             self.add_to_stack("ABILITY", card_id, controller, stack_context)
             logging.debug(f"Added non-targeting PW ability {ability_idx} for {card.name} to stack.")

        # Check SBAs immediately after paying cost (e.g., PW died from low loyalty)
        self.check_state_based_actions()

        return True # Activation successful (cost paid, targeting started or added to stack)
    
    def _get_target_type_from_text(self, text):
         """Simple helper to guess target type."""
         text = text.lower()
         if "target creature" in text: return "creature"
         if "target player" in text: return "player"
         if "target artifact" in text: return "artifact"
         if "target enchantment" in text: return "enchantment"
         if "target land" in text: return "land"
         if "target permanent" in text: return "permanent"
         if "any target" in text: return "any"
         return "target" # Default
    
    def handle_card_type_specific_rules(self, card_id, zone, player):
        """
        Handle rules specific to different card types when they enter a zone.
        
        Args:
            card_id: ID of the card
            zone: The zone the card is entering ('battlefield', 'graveyard', etc.)
            player: The player who controls the card
            
        Returns:
            bool: Whether any special handling was performed
        """
        gs = self
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'card_types'):
            return False
        
        # Battlefield entry rules
        if zone == "battlefield":
            # Creatures enter with summoning sickness
            if 'creature' in card.card_types:
                player["entered_battlefield_this_turn"].add(card_id)
                
            # Planeswalkers enter with loyalty counters
            if 'planeswalker' in card.card_types:
                if not hasattr(player, "loyalty_counters"):
                    player["loyalty_counters"] = {}
                    
                base_loyalty = card.loyalty if hasattr(card, 'loyalty') else 3
                player["loyalty_counters"][card_id] = base_loyalty
                logging.debug(f"Planeswalker {card.name} entered with {base_loyalty} loyalty")
                
            # Saga enchantments enter with lore counters
            if 'enchantment' in card.card_types and hasattr(card, 'subtypes') and 'saga' in card.subtypes:
                if not hasattr(player, "saga_counters"):
                    player["saga_counters"] = {}
                    
                player["saga_counters"][card_id] = 1
                
                # Trigger first chapter ability
                self.trigger_ability(card_id, "SAGA_CHAPTER", {"chapter": 1})
                
            # Equipment enters unattached
            if 'artifact' in card.card_types and hasattr(card, 'subtypes') and 'equipment' in card.subtypes:
                if not hasattr(player, "attachments"):
                    player["attachments"] = {}
                    
                if card_id in player["attachments"]:
                    del player["attachments"][card_id]
                
            # Auras need a target when cast
            if 'enchantment' in card.card_types and hasattr(card, 'subtypes') and 'aura' in card.subtypes:
                if not hasattr(player, "attachments") or card_id not in player["attachments"]:
                    # In a real implementation, this would be handled during casting/resolution
                    # For simulation purposes, we'll just attach to a legal target if possible
                    target_found = False
                    
                    # Look for a creature to attach to
                    for p in [gs.p1, gs.p2]:
                        for target_id in p["battlefield"]:
                            target_card = self._safe_get_card(target_id)
                            if target_card and hasattr(target_card, 'card_types') and 'creature' in target_card.card_types:
                                if not hasattr(player, "attachments"):
                                    player["attachments"] = {}
                                    
                                player["attachments"][card_id] = target_id
                                target_found = True
                                logging.debug(f"Aura {card.name} attached to {target_card.name}")
                                break
                        
                        if target_found:
                            break
                    
                    if not target_found:
                        # If no valid target, Aura goes to graveyard
                        logging.debug(f"Aura {card.name} had no valid targets, moving to graveyard")
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        return True
        
        # Graveyard entry rules
        elif zone == "graveyard":
            # Check for death triggers
            if card_id in player["battlefield"]:
                # Card is moving from battlefield to graveyard (dying)
                if 'creature' in card.card_types:
                    self.trigger_ability(card_id, "DIES")
                    
                # Artifact going to graveyard
                elif 'artifact' in card.card_types:
                    self.trigger_ability(card_id, "ARTIFACT_PUT_INTO_GRAVEYARD")
        
        # Hand entry rules
        elif zone == "hand":
            # Cards returning to hand lose counters, attachments, etc.
            if card_id in player["battlefield"]:
                # Remove any counters
                if hasattr(card, "counters"):
                    card.counters = {}
                    
                # Remove any attachments
                if hasattr(player, "attachments"):
                    if card_id in player["attachments"]:
                        del player["attachments"][card_id]
                    
                    # Also remove this card as an attachment from other cards
                    attached_to = [aid for aid, target in player["attachments"].items() if target == card_id]
                    for aid in attached_to:
                        del player["attachments"][aid]
        
        # Exile entry rules
        elif zone == "exile":
            # Similar to graveyard, but different triggers
            if card_id in player["battlefield"]:
                self.trigger_ability(card_id, "EXILED")
                
                # Remove any attachments
                if hasattr(player, "attachments"):
                    if card_id in player["attachments"]:
                        del player["attachments"][card_id]
                    
                    # Also remove this card as an attachment from other cards
                    attached_to = [aid for aid, target in player["attachments"].items() if target == card_id]
                    for aid in attached_to:
                        del player["attachments"][aid]
        
        return True
                        
    def trigger_ability(self, card_id, event_type, context=None):
        """Forward ability triggering to the AbilityHandler"""
        if hasattr(self, 'ability_handler') and self.ability_handler:
            return self.ability_handler.trigger_ability(card_id, event_type, context)
        return []
        

    def add_to_stack(self, item_type, source_id, controller, context=None):
        """Add an item to the stack with context. (Revised Priority Reset)"""
        if context is None: context = {}
        # Ensure source_id is valid
        card = self._safe_get_card(source_id)
        card_name = getattr(card, 'name', source_id) if card else source_id

        stack_item = (item_type, source_id, controller, context)
        self.stack.append(stack_item)
        logging.debug(f"Added to stack: {item_type} {card_name} ({source_id}) with context keys: {context.keys()}")

        # Reset priority ONLY IF NOT in a special choice phase.
        # Priority automatically goes to the active player after something is added.
        if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
            self.priority_pass_count = 0
            self.last_stack_size = len(self.stack)
            self.priority_player = self._get_active_player()
            # If not already in priority phase, enter it
            if self.phase != self.PHASE_PRIORITY:
                 self.previous_priority_phase = self.phase # Store where we came from
                 self.phase = self.PHASE_PRIORITY
            logging.debug(f"Stack changed, priority to AP ({self.priority_player['name']})")
        else:
             # Still update stack size even if not resetting priority
             self.last_stack_size = len(self.stack)
             logging.debug("Added to stack during special choice phase, priority maintained.")
    


    def cast_spell(self, card_id, player, context=None):
        """Cast a spell: Pay costs -> Move to Stack -> Set up Targeting/Choices."""
        if context is None: context = {}
        card = self._safe_get_card(card_id)
        if not card:
             logging.error(f"Cannot cast spell: Invalid card_id {card_id}")
             return False

        source_zone = context.get("source_zone", "hand") # Default source
        source_list = player.get(source_zone)

        # Check if card is actually in the expected source zone
        card_in_source = False
        if isinstance(source_list, (list, set)) and card_id in source_list:
             card_in_source = True
        # Handle special source zones like Command Zone if needed

        if not card_in_source:
            logging.warning(f"Cannot cast {card.name}: Not found in {player['name']}'s {source_zone}.")
            return False

        if not self._can_cast_now(card_id, player): # Includes priority and timing check
            logging.warning(f"Cannot cast {card.name}: Invalid timing (Phase: {self.phase}, Priority: {getattr(self.priority_player,'name','None')}).")
            return False

        # Determine Final Cost (Handles alternative costs like Kicker via context flags)
        base_cost_str = getattr(card, 'mana_cost', '')
        # Handle alternative costs like Flashback, Escape, Overload etc. if flagged in context
        if context.get("use_alt_cost"):
            alt_cost_type = context["use_alt_cost"]
            final_cost_dict = self.mana_system.calculate_alternative_cost(card_id, player, alt_cost_type, context)
            if final_cost_dict is None: # Cannot use specified alt cost
                 logging.warning(f"Cannot use alternative cost '{alt_cost_type}' for {card.name}.")
                 return False
        else: # Use normal cost
            final_cost_dict = self.mana_system.parse_mana_cost(base_cost_str)

        # Apply Kicker cost if context['kicked'] is True
        if context.get('kicked'):
             kicker_cost_str = self._get_kicker_cost_str(card) # Helper needed
             if kicker_cost_str:
                 kicker_cost_dict = self.mana_system.parse_mana_cost(kicker_cost_str)
                 for key, val in kicker_cost_dict.items():
                      if key == 'hybrid' or key == 'phyrexian': final_cost_dict[key].extend(val)
                      elif key != 'conditional': final_cost_dict[key] = final_cost_dict.get(key, 0) + val
                 context['actual_kicker_paid'] = kicker_cost_str # Track kicker payment for resolution

        # Apply other additional costs (sacrifice, discard) if context['pay_additional'] is True
        # The non-mana parts of additional costs are handled during the pay_mana_cost step.
        # The mana parts need adding here.
        if context.get('pay_additional'):
             add_cost_info = self._get_additional_cost_info(card) # Needs helper
             if add_cost_info and 'mana_cost' in add_cost_info:
                  add_cost_dict = self.mana_system.parse_mana_cost(add_cost_info['mana_cost'])
                  for key, val in add_cost_dict.items():
                      if key == 'hybrid' or key == 'phyrexian': final_cost_dict[key].extend(val)
                      elif key != 'conditional': final_cost_dict[key] = final_cost_dict.get(key, 0) + val

        # Apply modifiers (reduction/increase) LAST, after base/alt/additional costs determined
        final_cost_dict = self.mana_system.apply_cost_modifiers(player, final_cost_dict, card_id, context)

        # Check Affordability of the final calculated cost
        if not self.mana_system.can_pay_mana_cost(player, final_cost_dict, context):
            cost_str_log = self._format_mana_cost_for_logging(final_cost_dict, context.get('X', 0) if 'X' in final_cost_dict else 0)
            logging.warning(f"Cannot cast {card.name}: Cannot afford final cost {cost_str_log}.")
            return False

        # Targeting Requirement Check (BEFORE Paying Costs)
        requires_target = "target" in getattr(card, 'oracle_text', '').lower()
        num_targets = getattr(card, 'num_targets', 1) if requires_target else 0
        # Add handling for "choose up to N targets" - makes targeting optional below minimum
        up_to_N = "up to" in getattr(card, 'oracle_text', '').lower()

        if requires_target and num_targets > 0:
             valid_targets_map = self.targeting_system.get_valid_targets(card_id, player) if self.targeting_system else {}
             total_valid_targets = sum(len(v) for v in valid_targets_map.values())
             if total_valid_targets < num_targets and not up_to_N: # Check minimum if not "up to"
                  logging.warning(f"Cannot cast {card.name}: Not enough valid targets available ({total_valid_targets}/{num_targets}).")
                  return False

        # Pay Costs (Includes non-mana from context)
        if not self.mana_system.pay_mana_cost(player, final_cost_dict, context):
            logging.warning(f"Failed to pay cost for {card.name}.")
            # ManaSystem's pay_mana_cost needs robust rollback for non-mana costs
            return False

        # --- Move Card and Add to Stack ---
        # Remove from source zone first (use GameState.move_card with implicit source)
        if not self.move_card(card_id, player, source_zone, player, "stack_implicit", cause="casting"):
             logging.error(f"Failed to implicitly remove {card.name} from {source_zone} during casting.")
             # Rollback costs? Critical error state.
             return False

        # Prepare stack context
        context["source_zone"] = source_zone
        context["final_paid_cost"] = final_cost_dict # Store final cost for resolution/copying
        context["requires_target"] = requires_target
        context["num_targets"] = num_targets
        # Add Kicker/Additional flags if they were set true
        context["kicked"] = context.get("kicked", False)
        context["paid_additional"] = context.get("pay_additional", False)
        # Add flags for Adventure/Back Face if cast that way
        if context.get("cast_as_adventure"): pass # Already in context
        if context.get("cast_back_face"): pass # Already in context

        # Add to stack
        self.add_to_stack("SPELL", card_id, player, context)

        # --- Set up Targeting Phase ---
        if requires_target and num_targets > 0:
             logging.debug(f"{card.name} requires target(s). Entering TARGETING phase.")
             self.previous_priority_phase = self.phase # Store current phase
             self.phase = self.PHASE_TARGETING
             self.targeting_context = {
                 "source_id": card_id,
                 "controller": player,
                 "required_type": getattr(card, 'target_type', 'target'), # Need card schema for this
                 "required_count": num_targets,
                 "min_targets": 0 if up_to_N else num_targets, # Min 0 if "up to N"
                 "max_targets": num_targets,
                 "selected_targets": [],
                 "effect_text": getattr(card, 'oracle_text', '')
             }
        else: # No targets, priority passes normally via add_to_stack
            pass

        # --- Track Cast & Trigger ---
        self.track_card_played(card_id, player_idx = 0 if player == self.p1 else 1)
        if not hasattr(self, 'spells_cast_this_turn'): self.spells_cast_this_turn = []
        self.spells_cast_this_turn.append((card_id, player))
        self.handle_cast_trigger(card_id, player, context=context)

        logging.info(f"Successfully cast spell: {card.name} ({card_id}) from {source_zone} onto stack.")
        return True

    def _can_cast_now(self, card_id, player):
        """
        Check if a spell can be cast at the current time based on phase, stack state, etc.
        
        Args:
            card_id: ID of the card to check
            player: Player attempting to cast
            
        Returns:
            bool: Whether the spell can be cast
        """
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'card_types'):
            return False
        
        # Check phase compatibility
        is_instant = 'instant' in card.card_types
        has_flash = hasattr(card, 'oracle_text') and 'flash' in card.oracle_text.lower()
        
        # Instants and cards with flash can be cast anytime player has priority
        if not (is_instant or has_flash):
            # Non-instant speed spells can only be cast in main phases with empty stack
            if self.phase not in [self.PHASE_MAIN_PRECOMBAT, self.PHASE_MAIN_POSTCOMBAT]:
                return False
            if self.stack:  # Can't cast sorcery-speed spells if stack isn't empty
                return False
        
        # Check if player has priority
        active_player = self._get_active_player()
        has_priority = (player == active_player and self.priority_pass_count == 0) or self.priority_player == player
        
        return has_priority
        
    def play_land(self, card_id, controller):
        """
        Play a land card from hand to battlefield, respecting the one-land-per-turn rule.
        
        Args:
            card_id: ID of the land card to play
            controller: Player dictionary of the player playing the land
            
        Returns:
            bool: Whether the land was successfully played
        """
        # Check if card exists in hand
        if card_id not in controller["hand"]:
            logging.warning(f"Land {card_id} not found in hand")
            return False
        
        # Check if the card is actually a land
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'type_line') or 'land' not in card.type_line.lower():
            logging.warning(f"Card {card_id} is not a land")
            return False
        
        # Check if player has already played a land this turn
        if controller.get("land_played", False):
            logging.warning(f"Player has already played a land this turn")
            return False
        
        # Check if it's a valid phase to play a land
        if self.phase not in [self.PHASE_MAIN_PRECOMBAT, self.PHASE_MAIN_POSTCOMBAT]:
            logging.warning(f"Cannot play a land during phase {self.phase}")
            return False
        
        # Check if the player has priority
        active_player = self._get_active_player()
        if controller != active_player:
            logging.warning(f"Player does not have priority to play a land")
            return False
        
        # Move the land from hand to battlefield
        result = self.move_card(card_id, controller, "hand", controller, "battlefield", cause="land_play")
        
        if result:
            # Mark that player has played a land this turn
            controller["land_played"] = True
            
            # Track the land play for statistics
            player_idx = 0 if controller == self.p1 else 1
            self.track_card_played(card_id, player_idx)
            
            # Handle entering-the-battlefield effects specific to lands
            card_name = card.name if hasattr(card, 'name') else f"Land {card_id}"
            logging.debug(f"Played land {card_name}")
            
            # Check if land enters tapped
            if hasattr(card, 'oracle_text') and "enters the battlefield tapped" in card.oracle_text.lower():
                if not hasattr(controller, "tapped_permanents"):
                    controller["tapped_permanents"] = set()
                controller["tapped_permanents"].add(card_id)
                logging.debug(f"Land {card_name} enters tapped")
        
        return result
    
    def tap_permanent(self, card_id, player):
        """Tap a permanent, triggering any appropriate abilities."""
        if card_id not in player.get("battlefield", []):
             logging.warning(f"Cannot tap {card_id}: Not on {player['name']}'s battlefield.")
             return False
        tapped_set = player.setdefault("tapped_permanents", set())
        if card_id in tapped_set:
             logging.debug(f"Permanent {card_id} is already tapped.")
             return True # Already tapped is not a failure
        tapped_set.add(card_id)
        card = self._safe_get_card(card_id)
        logging.debug(f"Tapped {getattr(card, 'name', card_id)}")
        self.trigger_ability(card_id, "TAPPED", {"controller": player})
        return True

    def untap_permanent(self, card_id, player):
        """Untap a permanent, triggering any appropriate abilities."""
        if card_id not in player.get("battlefield", []):
             # Check phased out zone?
             if card_id in getattr(self, 'phased_out', set()):
                  logging.debug(f"Cannot untap {card_id}: Currently phased out.")
             else:
                  logging.warning(f"Cannot untap {card_id}: Not on {player['name']}'s battlefield.")
             return False
        tapped_set = player.setdefault("tapped_permanents", set())
        if card_id not in tapped_set:
             logging.debug(f"Permanent {card_id} is already untapped.")
             return True # Already untapped is not a failure
        tapped_set.remove(card_id)
        card = self._safe_get_card(card_id)
        logging.debug(f"Untapped {getattr(card, 'name', card_id)}")
        self.trigger_ability(card_id, "UNTAPPED", {"controller": player})
        return True
    
    def _validate_targets_on_resolution(self, source_id, controller, targets, context=None):
        """Checks if the targets selected for a spell/ability are still valid upon resolution."""
        if context is None: context = {} # Ensure context is dict

        # Use TargetingSystem if available
        if hasattr(self, 'targeting_system') and self.targeting_system:
            card = self._safe_get_card(source_id)
            if not card: return False # Source disappeared?

            # --- Pass Effect Text and Context ---
            # Use specific effect text from context if available (e.g., chosen modal effect)
            # Otherwise, fallback to card's oracle text.
            effect_text = context.get('effect_text', getattr(card, 'oracle_text', None))

            # Validate using TargetingSystem
            if hasattr(self.targeting_system, 'validate_targets'):
                is_valid = self.targeting_system.validate_targets(source_id, targets, controller, effect_text=effect_text)
                if not is_valid:
                     logging.debug(f"Target validation failed for {getattr(card,'name',source_id)} using TargetingSystem.validate_targets.")
                return is_valid
            else:
                logging.warning("TargetingSystem missing 'validate_targets' method.")
                # Fallback? Re-evaluate get_valid_targets? Risky, assume true.
                return True
        else:
            logging.warning("Cannot validate targets: TargetingSystem not available.")
            return True # Assume valid if no system? Safer than failing spells.

    def bottom_card(self, player, hand_index_to_bottom):
        """
        Handle bottoming a card from hand during mulligan resolution.

        Args:
            player: The player performing the bottoming.
            hand_index_to_bottom: The index of the card in the player's hand to bottom.

        Returns:
            bool: True if the card was successfully bottomed.
        """
        # Validate state
        if not self.bottoming_in_progress or self.bottoming_player != player:
            logging.warning("Invalid state to bottom card.")
            return False
        if not (0 <= hand_index_to_bottom < len(player["hand"])):
            logging.warning(f"Invalid hand index to bottom: {hand_index_to_bottom}")
            return False

        # Move the card
        card_id = player["hand"].pop(hand_index_to_bottom)
        player["library"].append(card_id) # Add to bottom
        card = self._safe_get_card(card_id)
        logging.debug(f"Player {player['name']} bottomed {getattr(card, 'name', card_id)}.")
        self.bottoming_count += 1

        # Check if bottoming is complete
        if self.bottoming_count >= self.cards_to_bottom:
             logging.info(f"Bottoming complete for {player['name']}.")
             self.bottoming_in_progress = False
             self.bottoming_player = None
             # Game starts - Transition to first turn, first phase
             if self.turn == 0: self.turn = 1 # Ensure turn starts at 1
             self.phase = self.PHASE_UNTAP
             self.priority_player = self.p1 # P1 starts
             self.priority_pass_count = 0
             # Process untap immediately? No, let main loop handle phase progression.
        # Else, remain in bottoming state (implicit, no specific phase constant needed)
        # The action mask generator will continue to only allow BOTTOM_CARD or maybe PASS/CONCEDE.

        return True

    def _determine_target_category(self, target_id):
        """Helper to determine the primary category ('creatures', 'players', etc.) for logging/categorization."""
        # This can reuse the logic from the Environment's helper if preferred,
        # or keep a local version for GameState internal use.
        owner, zone = self.find_card_location(target_id)
        if zone == 'player': return 'players'
        if zone == 'stack':
            for item in self.stack:
                if isinstance(item, tuple) and item[1] == target_id:
                    return 'spells' if item[0] == 'SPELL' else 'abilities'
            return 'stack_items' # Generic if not found matching ID
        if zone in ['graveyard', 'exile', 'library']: return 'cards'
        if zone == 'battlefield':
             card = self._safe_get_card(target_id)
             if card:
                  types = getattr(card, 'card_types', [])
                  type_line = getattr(card, 'type_line', '').lower()
                  if 'creature' in types: return 'creatures'
                  if 'planeswalker' in types: return 'planeswalkers'
                  if 'battle' in type_line: return 'battles'
                  if 'land' in types: return 'lands'
                  if 'artifact' in types: return 'artifacts'
                  if 'enchantment' in types: return 'enchantments'
                  return 'permanents' # Default permanent
        return 'other' # Fallback

    def resolve_top_of_stack(self):
        """Resolve the top item of the stack."""
        if not self.stack: return False
        top_item = self.stack.pop()
        resolution_success = False
        new_special_phase_entered = False
        resolved_item_had_split_second = False # Track if the resolved item had split second
        try:
            if isinstance(top_item, tuple) and len(top_item) >= 3:
                item_type, item_id, controller = top_item[:3]
                context = top_item[3] if len(top_item) > 3 else {}
                # Check context for split second
                if context.get('is_split_second', False):
                    resolved_item_had_split_second = True
                targets_on_stack_raw = context.get("targets")

                logging.debug(f"Resolving stack item: {item_type} {item_id} with raw targets: {targets_on_stack_raw}")
                card = self._safe_get_card(item_id)
                card_name = getattr(card, 'name', f"Item {item_id}") if card else f"Item {item_id}"

                # TARGET VALIDATION STEP
                validation_targets = {}
                if isinstance(targets_on_stack_raw, dict):
                    validation_targets = targets_on_stack_raw
                elif isinstance(targets_on_stack_raw, list): # Handle potential flat list from simple targeting
                    validation_targets = {"chosen": targets_on_stack_raw}
                # Else: If not list or dict, keep empty dict

                # --- Pass full context to validation ---
                targets_still_valid = self._validate_targets_on_resolution(item_id, controller, validation_targets, context)

                if not targets_still_valid:
                    logging.info(f"Stack Item {item_type} {card_name} fizzled: All targets invalid.")
                    if item_type == "SPELL" and not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                        # Spell fizzles - move to GY unless it shouldn't move (e.g., rebound, flashback)
                        # Replacement effects can still apply here (e.g., exile instead of GY)
                        self.move_card(item_id, controller, "stack_implicit", controller, "graveyard", cause="spell_fizzle", context=context)
                    # If ability fizzles, it just leaves the stack.
                    resolution_success = True # Fizzling counts as resolution finishing
                else:
                    # --- Proceed with resolution ---
                    if item_type == "SPELL": resolution_success = self._resolve_spell(item_id, controller, context)
                    elif item_type == "ABILITY" or item_type == "TRIGGER":
                        if self.ability_handler:
                            # Pass full context, including potentially validated/updated targets
                            if targets_still_valid: context['targets'] = validation_targets # Update context with validated targets format
                            resolution_success = self.ability_handler.resolve_ability(item_type, item_id, controller, context)
                        else: resolution_success = False
                    else: logging.warning(f"Unknown stack item type: {item_type}"); resolution_success = False

                    # If resolution itself initiates a new choice phase, flag it
                    if resolution_success and self.phase in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                        new_special_phase_entered = True
                        logging.debug(f"Resolution of {card_name} led to new special phase: {self._PHASE_NAMES.get(self.phase)}")
            else:
                 logging.warning(f"Invalid stack item format: {top_item}")
                 resolution_success = False
        except Exception as e:
            logging.error(f"Error resolving stack item: {str(e)}", exc_info=True)
            resolution_success = False
        finally:
            # --- Post-Resolution Cleanup ---
            # Clear split second flag *after* resolution if it was the last one
            if resolved_item_had_split_second:
                any_other_ss_on_stack = any(isinstance(i,tuple) and len(i)>3 and i[3].get('is_split_second') for i in self.stack)
                if not any_other_ss_on_stack:
                    self.split_second_active = False
                    logging.info("Split Second is now INACTIVE.")

            # --- Reset Priority ---
            # Only reset priority if a *new* special phase wasn't entered AND
            # if the stack is now empty or the active player should get priority back.
            if not new_special_phase_entered:
                self.priority_player = self._get_active_player() # AP gets priority after resolution
                self.priority_pass_count = 0
                logging.debug(f"Finished resolving stack item. Priority to AP ({self.priority_player['name']})")
            else:
                # If a special phase was entered, priority logic is handled by that phase setup.
                logging.debug(f"Resolution led to special phase, priority already set.")

            # --- Update stack size tracking ---
            self.last_stack_size = len(self.stack)

        return resolution_success


        
    def _resolve_ability(self, ability_id, controller, context=None):
        """
        Resolve an activated ability.
        
        Args:
            ability_id: The ID of the card with the ability
            controller: The player activating the ability
            context: Additional ability context
        """
        if context is None:
            context = {}
                
        # Check if we have pre-created effects in the context (from modal abilities, etc.)
        if "effects" in context and context["effects"]:
            effects = context["effects"]
            targets = context.get("targets")
            
            # Apply each effect
            for effect in effects:
                effect.apply(self, ability_id, controller, targets)
            return
        
        # If we have an ability handler, use it
        if hasattr(self, 'ability_handler'):
            ability_index = context.get("ability_index", 0)
            
            # Get the activated ability
            activated_abilities = self.ability_handler.get_activated_abilities(ability_id)
            if 0 <= ability_index < len(activated_abilities):
                ability = activated_abilities[ability_index]
                
                # Handle targeting if needed
                targets = context.get("targets")
                if not targets and hasattr(self.ability_handler, 'targeting_system'):
                    targets = self.ability_handler.targeting_system.resolve_targeting_for_ability(
                        ability_id, ability.effect_text, controller)
                        
                # Resolve the ability
                ability.resolve_with_targets(self, controller, targets)
                return
        
        # Fallback for when we have ability_text but no pre-created effects
        if "ability_text" in context:
            ability_text = context["ability_text"]
            targets = context.get("targets")
            
            # Create effects from the text
            if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, '_create_ability_effects'):
                effects = self.ability_handler._create_ability_effects(ability_text, targets)
                for effect in effects:
                    effect.apply(self, ability_id, controller, targets)
                return
        
        logging.warning(f"Could not resolve ability for card {ability_id}")

    def _resolve_triggered_ability(self, trigger_id, controller, context=None):
        """
        Resolve a triggered ability.
        
        Args:
            trigger_id: The ID of the card with the triggered ability
            controller: The player controlling the ability
            context: Additional trigger context
        """
        if context is None:
            context = {}
            
        # If we have an ability handler, use it
        if hasattr(self, 'ability_handler'):
            # Find the triggered ability based on the context
            trigger_event = context.get("trigger_event")
            
            # Check each ability on the card
            card_abilities = self.ability_handler.registered_abilities.get(trigger_id, [])
            for ability in card_abilities:
                if isinstance(ability, TriggeredAbility) and ability.can_trigger(trigger_event, context):
                    # Handle targeting if needed
                    targets = context.get("targets")
                    if not targets and hasattr(self.ability_handler, 'targeting_system'):
                        targets = self.ability_handler.targeting_system.resolve_targeting_for_ability(
                            trigger_id, ability.effect_text, controller)
                        
                    # Resolve the triggered ability
                    ability.resolve_with_targets(self, controller, targets)
                    return
        self.check_state_based_actions()    
        logging.warning(f"Could not resolve triggered ability for card {trigger_id}")
        
    def _resolve_spell(self, spell_id, controller, context=None):
        """
        Resolve a spell with comprehensive handling for all spell types.
        
        Args:
            spell_id: The ID of the spell to resolve
            controller: The player casting the spell
            context: Additional context about the spell (e.g., if it's a copy)
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            logging.warning(f"Cannot resolve spell: card {spell_id} not found")
            return
        
        spell_name = spell.name if hasattr(spell, "name") else "Unknown"
        logging.debug(f"Resolving spell: {spell_name}")
        
        # Check if spell is countered (e.g., by a previous spell/ability)
        if context.get("countered"):
            logging.debug(f"Spell {spell_name} was countered - moving to graveyard")
            if not context.get("is_copy", False):
                controller["graveyard"].append(spell_id)
            return
        
        # Determine spell type based on card type
        if hasattr(spell, 'card_types'):
            # Modal spell handling
            if hasattr(spell, 'modal') and spell.modal:
                mode = context.get("mode")
                if mode is not None:
                    self._resolve_modal_spell(spell_id, controller, mode, context)
                else:
                    logging.warning(f"Modal spell {spell_name} has no mode specified")
                    if not context.get("is_copy", False):
                        controller["graveyard"].append(spell_id)
                return
            
            # Handle different card types
            if 'creature' in spell.card_types:
                self._resolve_creature_spell(spell_id, controller, context)
            elif 'planeswalker' in spell.card_types:
                self._resolve_planeswalker_spell(spell_id, controller, context)
            elif 'artifact' in spell.card_types or 'enchantment' in spell.card_types:
                self._resolve_permanent_spell(spell_id, controller, context)
            elif 'land' in spell.card_types:
                self._resolve_land_spell(spell_id, controller, context)
            elif 'instant' in spell.card_types or 'sorcery' in spell.card_types:
                self._resolve_instant_sorcery_spell(spell_id, controller, context)
            else:
                logging.warning(f"Unknown card type for {spell_name}: {spell.card_types}")
                if not context.get("is_copy", False):
                    controller["graveyard"].append(spell_id)
        else:
            logging.warning(f"Card {spell_name} has no card_types attribute")
            if not context.get("is_copy", False):
                controller["graveyard"].append(spell_id)
                
    def _resolve_modal_spell(self, spell_id, controller, mode, context=None):
        """
        Resolve a modal spell based on the chosen mode.
        
        Args:
            spell_id: The ID of the modal spell
            controller: The player casting the spell
            mode: The chosen mode index
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # Handle through ability handler if available
        if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, 'handle_modal_ability'):
            success = self.ability_handler.handle_modal_ability(spell_id, controller, mode)
            if success:
                # If this is not a copy, move to graveyard (unless it's a permanent)
                if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                    if not (hasattr(spell, 'card_types') and 
                            any(t in spell.card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land'])):
                        controller["graveyard"].append(spell_id)
                return
                
        # Fallback - parse modes from oracle text
        if hasattr(spell, 'oracle_text'):
            modes = self._parse_modes_from_text(spell.oracle_text)
            if modes and 0 <= mode < len(modes):
                mode_text = modes[mode]
                
                # Create context with targets for this specific mode
                mode_context = dict(context)
                mode_context["mode_text"] = mode_text
                
                # Resolve as if it were a regular spell with this mode's effect
                if 'instant' in spell.card_types or 'sorcery' in spell.card_types:
                    # Resolve mode effects
                    targets = context.get("targets")
                    if not targets and hasattr(self, 'targeting_system'):
                        targets = self.targeting_system.resolve_targeting_for_spell(spell_id, controller, mode_text)
                        
                    self._resolve_mode_effects(spell_id, controller, mode_text, targets, mode_context)
                    
                    # Move to graveyard if not a copy
                    if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                        controller["graveyard"].append(spell_id)
                else:
                    # For permanent modal spells, handle differently based on the mode
                    # This is more complex and depends on the specific card
                    logging.warning(f"Modal permanent spell {spell.name} resolution not fully implemented")
                    
                    # Default handling for permanents
                    if not context.get("is_copy", False):
                        controller["battlefield"].append(spell_id)
                        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
            else:
                logging.warning(f"Invalid mode {mode} for spell {spell.name}")
                # Move to graveyard if not a permanent and not a copy
                if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                    if not (hasattr(spell, 'card_types') and 
                            any(t in spell.card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land'])):
                        controller["graveyard"].append(spell_id)
        else:
            logging.warning(f"Modal spell {spell_id} has no oracle_text attribute")
            # Move to graveyard if not a copy
            if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                controller["graveyard"].append(spell_id)
                
    def _parse_modes_from_text(self, text):
        """Parse modes from card text for modal spells."""
        modes = []
        
        # Check for common modal text patterns
        if "choose one —" in text.lower():
            # Split after the "Choose one —" text
            parts = text.split("Choose one —", 1)[1]
            
            # Split by bullet points or similar indicators
            import re
            mode_parts = re.split(r'[•●]', parts)
            
            # Clean and add each mode
            for part in mode_parts:
                cleaned = part.strip()
                if cleaned:
                    modes.append(cleaned)
        
        # Also handle "Choose one or both —" pattern
        elif "choose one or both —" in text.lower():
            parts = text.split("Choose one or both —", 1)[1]
            import re
            mode_parts = re.split(r'[•●]', parts)
            
            for part in mode_parts:
                cleaned = part.strip()
                if cleaned:
                    modes.append(cleaned)
        
        return modes

    def create_token_copy(self, original_card, controller):
        """Create a token copy of a card, handles details like base P/T."""
        if not original_card: return None
        # Create token tracking if it doesn't exist
        if not hasattr(controller, "tokens"): controller["tokens"] = []

        token_id = f"TOKEN_COPY_{len(controller['tokens'])}_{original_card.name[:10].replace(' ','')}"

        # Use dict/copy.deepcopy to get copyable values
        import copy
        try:
            # Get copyable characteristics based on Rule 707.2
            copyable_values = {
                "name": original_card.name,
                "mana_cost": original_card.mana_cost,
                #"color": original_card.color, # Use color_identity?
                "color_identity": original_card.colors, # Store the 5-dim vector
                "card_types": copy.deepcopy(original_card.card_types),
                "subtypes": copy.deepcopy(original_card.subtypes),
                "supertypes": copy.deepcopy(original_card.supertypes),
                "oracle_text": original_card.oracle_text,
                # Base power/toughness/loyalty (not including counters/effects)
                "power": getattr(original_card, '_base_power', getattr(original_card, 'power', 0)), # Need base P/T logic
                "toughness": getattr(original_card, '_base_toughness', getattr(original_card, 'toughness', 0)),
                "loyalty": getattr(original_card, '_base_loyalty', getattr(original_card, 'loyalty', 0)),
                "keywords": copy.deepcopy(getattr(original_card,'keywords',[0]*11)), # Copy base keywords
                "faces": copy.deepcopy(getattr(original_card,'faces', None)), # Copy faces for DFCs
            }
            copyable_values["is_token"] = True # Mark as token
            copyable_values["type_line"] = original_card.type_line # Copy type line

        except Exception as e:
             logging.error(f"Error getting copyable values for {original_card.name}: {e}")
             return None

        try:
            token = Card(copyable_values)
            token.card_id = token_id # Assign the unique ID
        except Exception as e:
             logging.error(f"Error creating token copy Card object: {e} | Data: {copyable_values}")
             return None

        # Add to game
        self.card_db[token_id] = token
        # Use move_card to handle ETB triggers and effects
        success = self.move_card(token_id, controller, "nonexistent_zone", controller, "battlefield", cause="token_creation")
        if not success:
             # Clean up if move failed
             del self.card_db[token_id]
             return None

        controller["tokens"].append(token_id) # Add to token tracking list *after* successful entry

        logging.debug(f"Created token copy of {original_card.name} (ID: {token_id})")

        return token_id

    def _apply_planeswalker_uniqueness_rule(self, controller):
        """Apply the planeswalker uniqueness rule (legendary rule for planeswalkers)."""
        # Group planeswalkers by name
        planeswalkers_by_type = {}
        
        # Identify planeswalkers by type
        for card_id in controller["battlefield"]:
            card = self._safe_get_card(card_id)
            if not card or not hasattr(card, 'card_types') or 'planeswalker' not in card.card_types:
                continue
                
            # Group by planeswalker type
            planeswalker_type = None
            if hasattr(card, 'subtypes'):
                for subtype in card.subtypes:
                    if subtype.lower() != 'planeswalker':
                        planeswalker_type = subtype.lower()
                        break
            
            # If no subtype was found, use the card name as fallback
            if not planeswalker_type and hasattr(card, 'name'):
                planeswalker_type = card.name.lower()
            
            if planeswalker_type:
                if planeswalker_type not in planeswalkers_by_type:
                    planeswalkers_by_type[planeswalker_type] = []
                planeswalkers_by_type[planeswalker_type].append(card_id)
        
        # Check each group for duplicates
        for planeswalker_type, cards in planeswalkers_by_type.items():
            if len(cards) > 1:
                # Keep the newest one, sacrifice the rest
                newest = cards[-1]
                for old_pw in cards[:-1]:
                    self.move_card(old_pw, controller, "battlefield", controller, "graveyard")
                    logging.debug(f"Planeswalker uniqueness rule: Sacrificed duplicate planeswalker")
                
    def _resolve_creature_spell(self, spell_id, controller, context=None):
        """Resolve a creature spell - put it onto the battlefield using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell or ('creature' not in getattr(spell, 'card_types', [])):
             # Spell might have lost creature type? Or invalid ID?
             logging.warning(f"Attempted to resolve {spell_id} as creature, but it's not.")
             # Move to GY if not a copy
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        # Use move_card to handle ETB, replacements, static effects
        if context.get("is_copy", False):
            # Create a token copy on the battlefield
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Creature spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            # Move the actual card to the battlefield
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                 logging.debug(f"Resolved Creature spell {spell.name}")
            else: # Move failed
                 controller["graveyard"].append(spell_id)
            return success


    def _resolve_planeswalker_spell(self, spell_id, controller, context=None):
        """Resolve a planeswalker spell - put it onto the battlefield using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        # Ensure it's still a planeswalker upon resolution
        if not spell or ('planeswalker' not in getattr(spell, 'card_types', [])):
            logging.warning(f"Attempted to resolve {spell_id} as planeswalker, but it's not.")
            if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
            return False

        if context.get("is_copy", False):
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Planeswalker spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                logging.debug(f"Resolved Planeswalker spell {spell.name}")
                # Uniqueness rule checked via SBAs
            else: # Move failed
                controller["graveyard"].append(spell_id)
            return success

    def _resolve_permanent_spell(self, spell_id, controller, context=None):
        """Resolve other permanent spells (Artifact, Enchantment, Battle) using move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        valid_types = ['artifact', 'enchantment', 'battle']
        # Check if it's one of the expected permanent types
        if not spell or not any(t in getattr(spell, 'card_types', []) or t in getattr(spell, 'type_line', '').lower() for t in valid_types):
            logging.warning(f"Attempted to resolve {spell_id} as permanent, but type is invalid.")
            if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
            return False

        if context.get("is_copy", False):
            token_id = self.create_token_copy(spell, controller)
            logging.debug(f"Resolved copy of Permanent spell {spell.name} as token {token_id}.")
            return token_id is not None
        else:
            # Handle Aura attachment targeting specifically during resolution if needed
            if 'aura' in getattr(spell, 'subtypes', []):
                 # Targets should be in context['targets']['chosen'] from targeting phase
                 chosen_targets = context.get('targets',{}).get('chosen', [])
                 if not chosen_targets:
                      logging.warning(f"Aura {spell.name} resolving without target, fizzling to graveyard.")
                      controller["graveyard"].append(spell_id)
                      return False
                 target_id = chosen_targets[0] # Assume first chosen target
                 # Check if target is still valid *now*
                 target_card = self._safe_get_card(target_id)
                 target_owner, target_zone = self.find_card_location(target_id)
                 if not target_card or target_zone != 'battlefield': # Add legality check later
                      logging.warning(f"Target {target_id} for Aura {spell.name} no longer valid. Fizzling.")
                      controller["graveyard"].append(spell_id)
                      return False
                 # Store attachment intention for move_card/ETB handling
                 context['attach_to_target'] = target_id

            # Use move_card for ETB, replacements, etc.
            success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
            if success:
                logging.debug(f"Resolved Permanent spell {spell.name}")
                # If it was an Aura, move_card's ETB handling should call _resolve_aura_attachment
                # if context included 'attach_to_target'
            else: # Move failed
                controller["graveyard"].append(spell_id)
            return success
        
    def _resolve_aura_attachment(self, aura_id, controller, context):
        """Handles attaching an aura when it resolves or enters the battlefield."""
        aura_card = self._safe_get_card(aura_id)
        if not aura_card: return

        target_id = context.get('attach_to_target') # Get target decided during casting/ETB
        if target_id:
             # Verify target still valid
             target_card = self._safe_get_card(target_id)
             target_owner, target_zone = self.find_card_location(target_id)
             if target_card and target_zone == 'battlefield': # Add legality check
                 if hasattr(self, 'attach_aura') and self.attach_aura(controller, aura_id, target_id):
                     logging.debug(f"Aura {aura_card.name} resolved and attached to {target_card.name}")
                     return
             # Target invalid or attachment failed
             logging.warning(f"Target {target_id} for Aura {aura_card.name} invalid on resolution or attachment failed.")
             # Aura goes to graveyard if target invalid upon resolution (handled by SBA usually)
             # Move directly here for clarity
             if aura_id in controller["battlefield"]:
                  self.move_card(aura_id, controller, "battlefield", controller, "graveyard", cause="aura_fizzle")
        else:
             logging.warning(f"Aura {aura_card.name} resolving without a target specified in context.")
             # Goes to graveyard if it needed a target but didn't have one
             if aura_id in controller["battlefield"]:
                 self.move_card(aura_id, controller, "battlefield", controller, "graveyard", cause="aura_fizzle")

    def _resolve_land_spell(self, spell_id, controller, context=None):
        """Resolve a land spell (e.g., from effects like Dryad Arbor). Uses move_card."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell or ('land' not in getattr(spell, 'card_types', []) and 'land' not in getattr(spell,'type_line','').lower()):
             logging.warning(f"Attempted to resolve {spell_id} as land spell, but type is invalid.")
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        # Lands resolving as spells don't count towards land drop normally
        # Use move_card to handle ETB
        success = self.move_card(spell_id, controller, "stack_implicit", controller, "battlefield", cause="spell_resolution", context=context)
        if success:
             logging.debug(f"Resolved Land spell {spell.name}")
        else: # Move failed
             controller["graveyard"].append(spell_id)
        return success

    def _resolve_instant_sorcery_spell(self, spell_id, controller, context=None):
        """Resolve instant/sorcery. Applies effects then moves to appropriate zone."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        valid_types = ['instant', 'sorcery']
        if not spell or not any(t in getattr(spell, 'card_types', []) for t in valid_types):
             logging.warning(f"Attempted to resolve {spell_id} as instant/sorcery, but type is invalid.")
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        spell_name = getattr(spell, 'name', f"Spell {spell_id}")
        logging.debug(f"Resolving Instant/Sorcery: {spell_name}")

        # Apply effects using AbilityHandler or EffectFactory
        if hasattr(self, 'ability_handler'):
            # --- MODIFIED: Pass full context ---
            effects = EffectFactory.create_effects(getattr(spell, 'oracle_text', ''), context.get('targets'))
            for effect_obj in effects:
                effect_obj.apply(self, spell_id, controller, context.get('targets'))
            # --- END MODIFIED ---
        else:
            logging.warning("No ability handler found to resolve instant/sorcery effects.")

        # Determine final destination zone based on context (Flashback, Rebound etc.)
        final_zone = "graveyard"
        was_cast_from_hand = context.get('source_zone') == 'hand' # Need source zone info
        has_rebound = "rebound" in getattr(spell,'oracle_text','').lower()

        if context.get('cast_from_zone') == 'graveyard' and "flashback" in getattr(spell,'oracle_text','').lower():
            final_zone = "exile"
        # --- MODIFIED: Rebound Logic ---
        elif has_rebound and was_cast_from_hand:
            final_zone = "exile"
            if not hasattr(self, 'rebounded_cards'): self.rebounded_cards = {}
            self.rebounded_cards[spell_id] = {'owner': controller, 'turn_exiled': self.turn} # Track owner and turn
            logging.debug(f"{spell_name} exiled via Rebound.")
        # --- END MODIFIED ---

        # Handle copies (they cease to exist)
        if context.get("is_copy", False):
            logging.debug(f"Copy of {spell_name} resolved and ceased to exist.")
        elif context.get("skip_default_movement", False):
             logging.debug(f"Default movement skipped for {spell_name} (e.g., Buyback, Commander tax zone).")
        elif final_zone != "battlefield": # Ensure permanents aren't moved here
             # Use move_card to handle triggers etc.
             self.move_card(spell_id, controller, "stack_implicit", controller, final_zone, cause="spell_resolution", context=context)

        self.trigger_ability(spell_id, "SPELL_RESOLVED", {"controller": controller})
        return True
            
    def _word_to_number(self, word):
        """Convert word representation of number to int."""
        from .ability_utils import text_to_number
        return text_to_number(word)
    
    def _get_madness_cost_str_gs(self, card):
        """Helper to extract madness cost string from GameState context."""
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"madness\s+(\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
            if match:
                cost_str = match.group(1)
                if cost_str.isdigit(): return f"{{{cost_str}}}"
                return cost_str
        return None
    
    def _advance_phase(self):
        """Advance to the next phase in the turn sequence with improved progress detection and handling."""

        # Phase sequence definition
        phase_sequence = [
            self.PHASE_UNTAP,
            self.PHASE_UPKEEP,
            self.PHASE_DRAW,
            self.PHASE_MAIN_PRECOMBAT,
            self.PHASE_BEGIN_COMBAT,
            self.PHASE_DECLARE_ATTACKERS,
            self.PHASE_DECLARE_BLOCKERS,
            self.PHASE_FIRST_STRIKE_DAMAGE,
            self.PHASE_COMBAT_DAMAGE,
            self.PHASE_END_OF_COMBAT,
            self.PHASE_MAIN_POSTCOMBAT,
            self.PHASE_END_STEP,
            self.PHASE_CLEANUP
        ]

        old_phase = self.phase
        old_phase_name = self._PHASE_NAMES.get(old_phase, f"UNKNOWN({old_phase})")

        # --- Handle Special Phase Exits ---
        # --- ADDED CHECK FOR PENDING CHOICE ---
        if old_phase in [self.PHASE_PRIORITY, self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
            # Only proceed if stack is empty AND no choice context is active
            if not self.stack and not self.targeting_context and not self.sacrifice_context and not self.choice_context:
        # --- END ADDED CHECK ---
                # Retrieve the phase we were in before entering the special phase
                if hasattr(self, 'previous_priority_phase') and self.previous_priority_phase is not None:
                    self.phase = self.previous_priority_phase
                    self.previous_priority_phase = None # Clear after using
                    if self.phase != self.PHASE_CLEANUP:
                        self.priority_player = self._get_active_player()
                        self.priority_pass_count = 0
                        self.last_stack_size = len(self.stack) # Should be 0 here
                else: # Fallback if previous phase wasn't tracked
                    logging.warning(f"No previous_priority_phase tracked when exiting {old_phase_name}. Defaulting.")
                    self.phase = self.PHASE_MAIN_POSTCOMBAT if self.combat_damage_dealt else self.PHASE_MAIN_PRECOMBAT
                    self.priority_player = self._get_active_player()
                    self.priority_pass_count = 0
                new_phase_name = self._PHASE_NAMES.get(self.phase, '?')
                logging.debug(f"Returning from special phase {old_phase_name} to {new_phase_name}")
                self._phase_action_count = 0 # Reset phase action count when returning
                return # Phase transition handled
            else:
                 # Stay in the current special phase if stack/context still active
                 logging.debug(f"Staying in special phase {old_phase_name} (Stack/Choice Context still active).")
                 return # No phase advancement
        # --- END CONSOLIDATED CHECK ---


        # --- Cleanup Step Logic ---
        if old_phase == self.PHASE_CLEANUP:
            active_player = self._get_active_player()
            non_active_player = self._get_non_active_player()
            # 1. Discard down to hand size (Active player first)
            if len(active_player.get('hand', [])) > self.max_hand_size:
                 # Set up a discard choice state - This needs a specific phase or context handling
                 # For now, log and potentially auto-discard (simplification)
                 num_to_discard = len(active_player['hand']) - self.max_hand_size
                 logging.info(f"Player {active_player['name']} must discard {num_to_discard} cards in cleanup.")
                 # TODO: Implement cleanup discard choice/action phase (e.g., PHASE_CLEANUP_DISCARD)
                 # For now, we auto-discard highest CMC as placeholder
                 sorted_hand = sorted([(cid, getattr(self._safe_get_card(cid), 'cmc', 0)) for cid in active_player['hand']], key=lambda x: -x[1])
                 for _ in range(num_to_discard):
                     if sorted_hand:
                         discard_id, _ = sorted_hand.pop(0)
                         self.move_card(discard_id, active_player, "hand", active_player, "graveyard", cause="cleanup_discard")
                     else: break


            # 2. Remove damage from creatures
            for player in [active_player, non_active_player]:
                if player: # Ensure player exists
                    player['damage_counters'] = {}
                    player['deathtouch_damage'] = set() # Reset deathtouch marks

            # 3. End "until end of turn" and "this turn" effects
            # LayerSystem cleanup needs careful handling. Call helper?
            if self.layer_system and hasattr(self.layer_system, 'cleanup_expired_effects'):
                self.layer_system.cleanup_expired_effects()
            if self.replacement_effects and hasattr(self.replacement_effects, 'cleanup_expired_effects'):
                self.replacement_effects.cleanup_expired_effects()
            # Clear simple tracking sets/lists
            self.haste_until_eot.clear() if hasattr(self, 'haste_until_eot') else None
            self.exile_at_end_of_combat.clear() if hasattr(self, 'exile_at_end_of_combat') else None
            # Reset 'has_haste_until_eot' tracking set
            self.has_haste_until_eot = set() if hasattr(self, 'has_haste_until_eot') else set()
            self.until_end_of_turn_effects = {} # Explicit clear


            # Revert temporary control effects
            self._revert_temporary_control()

            logging.debug("Cleanup step actions completed.")
            # Check if state-based actions or triggered abilities happened during cleanup
            sbas_happened = self.check_state_based_actions()
            triggers_queued = False
            if self.ability_handler:
                triggers_were_present = bool(self.ability_handler.active_triggers)
                self.ability_handler.process_triggered_abilities() # Put on stack
                triggers_queued = len(self.stack) > 0 # Check stack directly after processing

            # If anything happened, players get priority again *in the cleanup step*
            if sbas_happened or triggers_queued:
                 logging.debug("SBAs/Triggers occurred during Cleanup. Players receive priority.")
                 # self.phase = self.PHASE_CLEANUP # Stay in Cleanup - This caused issues, cleanup should finish THEN priority if needed
                 self.phase = self.PHASE_PRIORITY # Transition to Priority to handle stack items
                 self.previous_priority_phase = self.PHASE_CLEANUP # Remember we came from cleanup
                 self.priority_player = active_player # AP gets priority first
                 self.priority_pass_count = 0
                 self.last_stack_size = len(self.stack)
                 self._phase_action_count = 0 # Reset phase action count
                 return # Do not advance to next turn yet
            # Else, if nothing happened, proceed to next turn

        # --- Regular Phase Advancement ---
        # Find current phase index
        try:
            current_idx = phase_sequence.index(old_phase)
        except ValueError:
            logging.error(f"Current phase {old_phase_name} not found in standard sequence.")
            # Force to a known state
            self.phase = self.PHASE_MAIN_PRECOMBAT
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
            self._phase_action_count = 0
            self.progress_was_forced = True # Indicate forced progression
            return

        next_idx = current_idx + 1

        # Determine next phase, potentially skipping First Strike
        new_phase = phase_sequence[next_idx % len(phase_sequence)]

        if new_phase == self.PHASE_FIRST_STRIKE_DAMAGE and not self._combat_has_first_strike():
            logging.debug("Skipping First Strike Damage phase.")
            next_idx += 1
            new_phase = phase_sequence[next_idx % len(phase_sequence)]

        # Handle turn transition (triggered after Cleanup resolution if no triggers occurred)
        if old_phase == self.PHASE_CLEANUP and new_phase == self.PHASE_UNTAP: # Checks if we are wrapping around
             prev_turn = self.turn
             self.turn += 1
             logging.info(f"=== ADVANCING FROM TURN {prev_turn} TO TURN {self.turn} ===")
             # Reset turn-based flags/tracking (CONSOLIDATED)
             self.combat_damage_dealt = False
             self.day_night_checked_this_turn = False
             self.spells_cast_this_turn = []
             self.attackers_this_turn = set()
             self.cards_to_graveyard_this_turn = {} # Clear old turns, initialize for current turn
             self.cards_to_graveyard_this_turn[self.turn] = []
             self.gravestorm_count = 0
             self.boast_activated = set()
             self.forecast_used = set()
             self.life_gained_this_turn = {} # Reset stats tracking
             self.damage_this_turn = {}
             self.cards_drawn_this_turn = {self.p1['name']: 0, self.p2['name']: 0} if self.p1 and self.p2 else {}

             # Reset player flags (only things that reset *every* turn)
             for player in [self.p1, self.p2]:
                 if player: # Check player exists
                     player["land_played"] = False
                     player["entered_battlefield_this_turn"] = set()
                     player["activated_this_turn"] = set()
                     player["lost_life_this_turn"] = False
                     player["damage_counters"] = {} # Cleared in Cleanup, but ensure reset
                     player["deathtouch_damage"] = set() # Cleared in Cleanup, but ensure reset

             # NOTE: until_end_of_turn effects cleared in Cleanup. Haste/temp buffs also cleared there.


             # Reset mana pools (happens during Untap phase itself, called later)
             # Reset other turn-specific counters/states
             # Example: Reset Storm count? Usually done per spell cast.

             # Check for game end due to turn limit AFTER incrementing turn
             if self.turn > self.max_turns and not getattr(self, '_turn_limit_checked', False):
                 logging.info(f"Turn limit ({self.max_turns}) reached! Ending game.")
                 self._turn_limit_checked = True
                 # Game result decided by life totals
                 if self.p1 and self.p2:
                     if self.p1["life"] > self.p2["life"]: self.p1["won_game"] = True; self.p2["lost_game"] = True
                     elif self.p2["life"] > self.p1["life"]: self.p2["won_game"] = True; self.p1["lost_game"] = True
                     else: self.p1["game_draw"] = True; self.p2["game_draw"] = True
                     # Check SBAs after setting win/loss flags
                     self.check_state_based_actions() # Game end is handled by main loop checking lost_game flags
                 else: # Cannot determine winner if players are missing
                      logging.error("Cannot determine game winner at turn limit - players missing.")

        # Set the new phase
        self.phase = new_phase
        new_phase_name = self._PHASE_NAMES.get(self.phase, f"UNKNOWN({self.phase})")
        logging.debug(f"Advanced from {old_phase_name} to {new_phase_name}")
        self._phase_action_count = 0 # Reset counter for new phase

        # --- Phase Start Actions & Trigger Checks ---
        # Note: Priority is only given *after* these turn-based actions and triggers resolve.
        active_player = self._get_active_player()
        non_active_player = self._get_non_active_player()
        self.priority_player = None # No priority during phase actions/trigger checks

        if self.phase == self.PHASE_UNTAP:
             # Phasing happens before other untap actions (Rule 502.1)
             if hasattr(self, 'phase_in_permanents'): self.phase_in_permanents(active_player)
             # Actual untap action
             self._untap_phase(active_player) # Resets mana pool etc.
             # SBAs are checked (Rule 502.4)
             self.check_state_based_actions()
             # NO player gets priority in Untap step (Rule 502.3)
             # Auto-advance to Upkeep
             self.phase = self.PHASE_UPKEEP
             new_phase_name = self._PHASE_NAMES.get(self.phase, '?')
             logging.debug(f"Auto-advanced from UNTAP to UPKEEP")
             # Fall through to Upkeep logic

        # --- Re-trigger Upkeep Logic If we fell through from UNTAP ---
        if self.phase == self.PHASE_UPKEEP:
            
            # Check Day/Night transition first if applicable (rules check needed - often here?)
            if hasattr(self, 'check_day_night_transition') and not self.day_night_checked_this_turn:
                self.check_day_night_transition() # Checks spell counts and sets state

            # Trigger "at the beginning of upkeep" (Rule 503.1a) - APNAP
            # Collect triggers first
            ap_triggers = []
            nap_triggers = []
            for p_trigger in [active_player, non_active_player]:
                if p_trigger: # Check player exists
                    for card_id in p_trigger.get("battlefield", []):
                        card = self._safe_get_card(card_id)
                        if card and hasattr(self.ability_handler, 'registered_abilities'): # Ensure handler exists
                            abilities = self.ability_handler.registered_abilities.get(card_id, [])
                            for ability in abilities:
                                # Check if ability has the correct trigger condition
                                if isinstance(ability, TriggeredAbility):
                                    cond = getattr(ability, 'trigger_condition', '').lower()
                                    if "beginning of" in cond and "upkeep" in cond:
                                        context = {"controller": self.get_card_controller(card_id)}
                                        # Pre-check ability conditions if possible (optional optimization)
                                        if ability.can_trigger("BEGINNING_OF_UPKEEP", context):
                                            if p_trigger == active_player: ap_triggers.append((card_id, ability))
                                            else: nap_triggers.append((card_id, ability))
                                        break # Assume only one relevant trigger per card for simplicity

            # Queue triggers using AbilityHandler (handles APNAP ordering)
            for card_id, ability_obj in ap_triggers + nap_triggers:
                # Pass ability object for efficiency
                context = {"controller": self.get_card_controller(card_id), "ability": ability_obj}
                self.trigger_ability(card_id, "BEGINNING_OF_UPKEEP", context) # Event origin is the card

            # SBAs checked (Rule 503.1b) after triggers added
            self.check_state_based_actions()


        elif self.phase == self.PHASE_DRAW:
             # Check replacement effects BEFORE draw (e.g., Dredge choice)
             # Player needs to choose *if* they want to dredge
             dredge_available = False
             if active_player:
                for card_id in active_player.get("graveyard",[]):
                     card = self._safe_get_card(card_id)
                     if card and "dredge" in getattr(card,'oracle_text','').lower():
                          dredge_match = re.search(r"dredge (\d+)", card.oracle_text.lower())
                          if dredge_match:
                               dredge_value = int(dredge_match.group(1))
                               if len(active_player.get("library", [])) >= dredge_value:
                                    dredge_available = True; break

             if dredge_available:
                  logging.debug("Dredge available. Entering CHOICE phase.")
                  # Need state to return to Draw phase AFTER dredge choice resolves or is skipped
                  self.previous_priority_phase = self.PHASE_DRAW # Remember we came from draw step setup
                  self.phase = self.PHASE_CHOOSE
                  self.choice_context = { 'type': 'dredge', 'player': active_player }
                  self.priority_player = active_player
                  self.priority_pass_count = 0
                  return # Stop phase advancement, enter choice


             # --- Proceed with normal draw ---
             # Draw step turn-based action (Rule 504.1)
             self._draw_phase(active_player)
             # SBAs checked (Rule 504.2)
             self.check_state_based_actions()
             # "At the beginning of draw step" triggers (Rule 504.3) - APNAP order
             ap_triggers = []
             nap_triggers = []
             for p_trigger in [active_player, non_active_player]:
                 if p_trigger: # Check player exists
                      for card_id in p_trigger.get("battlefield",[]):
                           if self.ability_handler:
                               abilities = self.ability_handler.registered_abilities.get(card_id, [])
                               for ability in abilities:
                                  if isinstance(ability, TriggeredAbility):
                                      cond = getattr(ability, 'trigger_condition', '').lower()
                                      if "beginning of" in cond and "draw step" in cond:
                                          context = {"controller": self.get_card_controller(card_id)}
                                          if ability.can_trigger("BEGINNING_OF_DRAW", context):
                                              if p_trigger == active_player: ap_triggers.append((card_id, ability))
                                              else: nap_triggers.append((card_id, ability))
                                          break # Assume one trigger per card
             # Queue triggers
             for card_id, ability_obj in ap_triggers + nap_triggers:
                 context = {"controller": self.get_card_controller(card_id), "ability": ability_obj}
                 self.trigger_ability(card_id, "BEGINNING_OF_DRAW", context) # Event origin is the card

        elif self.phase == self.PHASE_MAIN_PRECOMBAT:
            # Saga counter addition (Rule 714.2b - after draw step, before main phase actions)
            if hasattr(self, 'advance_saga_counters') and callable(self.advance_saga_counters):
                self.advance_saga_counters(active_player)


        elif self.phase == self.PHASE_BEGIN_COMBAT:
            # Trigger "at beginning of combat" (Rule 506.1a) - APNAP
            ap_triggers = []
            nap_triggers = []
            for p_trigger in [active_player, non_active_player]:
                 if p_trigger: # Check player exists
                     for card_id in p_trigger.get("battlefield",[]):
                          if self.ability_handler:
                              abilities = self.ability_handler.registered_abilities.get(card_id, [])
                              for ability in abilities:
                                  if isinstance(ability, TriggeredAbility):
                                      cond = getattr(ability, 'trigger_condition', '').lower()
                                      if "beginning of" in cond and ("combat" in cond or "each combat" in cond):
                                          context = {"controller": self.get_card_controller(card_id)}
                                          if ability.can_trigger("BEGINNING_OF_COMBAT", context):
                                              if p_trigger == active_player: ap_triggers.append((card_id, ability))
                                              else: nap_triggers.append((card_id, ability))
                                          break # Assume one trigger
            # Queue triggers
            for card_id, ability_obj in ap_triggers + nap_triggers:
                context = {"controller": self.get_card_controller(card_id), "ability": ability_obj}
                self.trigger_ability(card_id, "BEGINNING_OF_COMBAT", context)


        elif self.phase == self.PHASE_END_STEP:
             # Trigger "at the beginning of the end step" (Rule 513.1a) - APNAP
             # Check 'skip end step trigger' flag which might be set by SBAs
             skip_set = getattr(active_player, "skip_end_step_trigger", set()) if active_player else set()
             ap_triggers = []
             nap_triggers = []
             for p_trigger in [active_player, non_active_player]:
                  if p_trigger: # Check player exists
                      for card_id in p_trigger.get("battlefield",[]):
                          if card_id in skip_set: continue # Skip if marked by SBA 704.5u
                          if self.ability_handler:
                              abilities = self.ability_handler.registered_abilities.get(card_id, [])
                              for ability in abilities:
                                  if isinstance(ability, TriggeredAbility):
                                      cond = getattr(ability, 'trigger_condition', '').lower()
                                      if "beginning of" in cond and "end step" in cond:
                                          context = {"controller": self.get_card_controller(card_id)}
                                          if ability.can_trigger("BEGINNING_OF_END_STEP", context):
                                               if p_trigger == active_player: ap_triggers.append((card_id, ability))
                                               else: nap_triggers.append((card_id, ability))
                                          break # Assume one trigger
             # Clear skip set after checking
             if hasattr(active_player, "skip_end_step_trigger"): active_player["skip_end_step_trigger"] = set()
             # Queue triggers
             for card_id, ability_obj in ap_triggers + nap_triggers:
                 context = {"controller": self.get_card_controller(card_id), "ability": ability_obj}
                 self.trigger_ability(card_id, "BEGINNING_OF_END_STEP", context)

             # Handle "at end of combat" triggers / delayed triggers that wait for end step?
             # These should probably just resolve during the End Step if added to the stack.

        # --- Reset priority after handling phase start actions ---
        self.priority_player = active_player
        self.priority_pass_count = 0
        self.last_stack_size = len(self.stack) # Update stack size for priority logic

        # Process any triggers that were just added to the queue
        if self.ability_handler: self.ability_handler.process_triggered_abilities()

        # If triggers were added, priority stays with AP and pass count is 0 (set above)
        if len(self.stack) > self.last_stack_size:
             self.last_stack_size = len(self.stack)
             logging.debug(f"Triggers added to stack during phase transition. Priority remains with AP ({active_player['name']})")
        # Otherwise, AP has priority to start the new phase/step, also set above.

    def _get_active_player(self):
        """Returns the active player (whose turn it is)."""
        return self.p1 if (self.turn % 2 == 1) == self.agent_is_p1 else self.p2

    def _get_non_active_player(self):
        """Returns the non-active player."""
        return self.p2 if (self.turn % 2 == 1) == self.agent_is_p1 else self.p1
        
    def _check_phase_progress(self):
        """Ensure phase progression is happening correctly, forcing termination if needed."""
        # Add current phase to history (keeping only recent history)
        self._phase_history.append(self.phase)
        if len(self._phase_history) > 30:
            self._phase_history.pop(0)
        
        # Check for being stuck in the same phase
        if len(self._phase_history) >= 20 and all(p == self._phase_history[0] for p in self._phase_history):
            logging.warning(f"Detected potential phase stagnation in phase {self._phase_history[0]}")
            # Force advance to next turn as an escape mechanism
            if self.phase in [self.PHASE_PRIORITY, self.PHASE_END_STEP, self.PHASE_CLEANUP]:
                self.phase = self.PHASE_UNTAP
                self.turn += 1
                self._phase_history = []  # Reset history after forced progress
                self.progress_was_forced = True
                logging.warning(f"Force-advancing to turn {self.turn} to break potential stall")
                return True
        
        return False

    def clone(self):
        """Create a deep copy of the game state for lookahead simulation."""
        import copy # Ensure copy is imported

        # 1. Create a new instance with basic parameters (card_db is shared reference)
        cloned_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)

        # --- Deep Copy Attributes that change ---
        # Primitive types are fine with shallow copy / direct assignment
        cloned_state.turn = self.turn
        cloned_state.phase = self.phase
        cloned_state.agent_is_p1 = self.agent_is_p1
        cloned_state.combat_damage_dealt = self.combat_damage_dealt if hasattr(self, 'combat_damage_dealt') else False
        cloned_state.day_night_state = self.day_night_state if hasattr(self, 'day_night_state') else None
        cloned_state.day_night_checked_this_turn = self.day_night_checked_this_turn if hasattr(self, 'day_night_checked_this_turn') else False
        cloned_state.priority_pass_count = self.priority_pass_count if hasattr(self, 'priority_pass_count') else 0
        cloned_state.last_stack_size = self.last_stack_size if hasattr(self, 'last_stack_size') else 0
        cloned_state.previous_priority_phase = self.previous_priority_phase if hasattr(self, 'previous_priority_phase') else None
        # Copy other simple state variables
        cloned_state.mulligan_in_progress = self.mulligan_in_progress if hasattr(self, 'mulligan_in_progress') else False
        cloned_state.bottoming_in_progress = self.bottoming_in_progress if hasattr(self, 'bottoming_in_progress') else False
        cloned_state.cards_to_bottom = self.cards_to_bottom if hasattr(self, 'cards_to_bottom') else 0
        cloned_state.bottoming_count = self.bottoming_count if hasattr(self, 'bottoming_count') else 0
        cloned_state.split_second_active = self.split_second_active if hasattr(self, 'split_second_active') else False
        cloned_state.gravestorm_count = self.gravestorm_count if hasattr(self, 'gravestorm_count') else 0
        cloned_state.miracle_active = self.miracle_active if hasattr(self, 'miracle_active') else False
        cloned_state.miracle_card_id = self.miracle_card_id if hasattr(self, 'miracle_card_id') else None
        cloned_state.progress_was_forced = self.progress_was_forced if hasattr(self, 'progress_was_forced') else False
        cloned_state._turn_limit_checked = self._turn_limit_checked if hasattr(self, '_turn_limit_checked') else False

        # --- Deep Copy Player States ---
        # Player States (Critical to deep copy) - Need to handle potential None
        cloned_state.p1 = copy.deepcopy(self.p1) if self.p1 else None
        cloned_state.p2 = copy.deepcopy(self.p2) if self.p2 else None

        # --- Deep Copy Combat State ---
        cloned_state.current_attackers = self.current_attackers[:] if hasattr(self, 'current_attackers') else [] # Shallow copy list ok
        cloned_state.current_block_assignments = copy.deepcopy(self.current_block_assignments) if hasattr(self, 'current_block_assignments') else {}

        # --- Deep Copy Stack ---
        cloned_state.stack = copy.deepcopy(self.stack) if hasattr(self, 'stack') else []

        # --- Deep Copy effect tracking ---
        cloned_state.until_end_of_turn_effects = copy.deepcopy(self.until_end_of_turn_effects) if hasattr(self, 'until_end_of_turn_effects') else {}
        cloned_state.temp_control_effects = copy.deepcopy(self.temp_control_effects) if hasattr(self, 'temp_control_effects') else {}
        cloned_state.abilities_activated_this_turn = copy.deepcopy(self.abilities_activated_this_turn) if hasattr(self, 'abilities_activated_this_turn') else []
        cloned_state.spells_cast_this_turn = copy.deepcopy(self.spells_cast_this_turn) if hasattr(self, 'spells_cast_this_turn') else []
        cloned_state.attackers_this_turn = self.attackers_this_turn.copy() if hasattr(self, 'attackers_this_turn') else set() # Copy set
        cloned_state.cards_played = copy.deepcopy(self.cards_played) if hasattr(self, 'cards_played') else {}
        cloned_state.damage_dealt_this_turn = copy.deepcopy(self.damage_dealt_this_turn) if hasattr(self, 'damage_dealt_this_turn') else {}
        cloned_state.cards_drawn_this_turn = copy.deepcopy(self.cards_drawn_this_turn) if hasattr(self, 'cards_drawn_this_turn') else {}
        cloned_state.life_gained_this_turn = copy.deepcopy(self.life_gained_this_turn) if hasattr(self, 'life_gained_this_turn') else {}
        cloned_state.damage_this_turn = copy.deepcopy(self.damage_this_turn) if hasattr(self, 'damage_this_turn') else {}
        cloned_state.cards_to_graveyard_this_turn = copy.deepcopy(self.cards_to_graveyard_this_turn) if hasattr(self, 'cards_to_graveyard_this_turn') else {}

        # --- Deep Copy Special Card States ---
        cloned_state.adventure_cards = self.adventure_cards.copy() if hasattr(self, 'adventure_cards') else set()
        cloned_state.saga_counters = copy.deepcopy(self.saga_counters) if hasattr(self, 'saga_counters') else {}
        cloned_state.mdfc_cards = self.mdfc_cards.copy() if hasattr(self, 'mdfc_cards') else set()
        cloned_state.battle_cards = copy.deepcopy(self.battle_cards) if hasattr(self, 'battle_cards') else {}
        cloned_state.cards_castable_from_exile = self.cards_castable_from_exile.copy() if hasattr(self, 'cards_castable_from_exile') else set()
        cloned_state.cast_as_back_face = self.cast_as_back_face.copy() if hasattr(self, 'cast_as_back_face') else set()

        # --- Deep Copy Special Status Tracking ---
        cloned_state.phased_out = self.phased_out.copy() if hasattr(self, 'phased_out') else set()
        cloned_state.suspended_cards = copy.deepcopy(self.suspended_cards) if hasattr(self, 'suspended_cards') else {}
        cloned_state.rebounded_cards = copy.deepcopy(self.rebounded_cards) if hasattr(self, 'rebounded_cards') else {}
        cloned_state.kicked_cards = self.kicked_cards.copy() if hasattr(self, 'kicked_cards') else set()
        cloned_state.evoked_cards = self.evoked_cards.copy() if hasattr(self, 'evoked_cards') else set()
        cloned_state.foretold_cards = copy.deepcopy(self.foretold_cards) if hasattr(self, 'foretold_cards') else {} # Deepcopy dict
        cloned_state.blitz_cards = self.blitz_cards.copy() if hasattr(self, 'blitz_cards') else set()
        cloned_state.dash_cards = self.dash_cards.copy() if hasattr(self, 'dash_cards') else set()
        cloned_state.unearthed_cards = self.unearthed_cards.copy() if hasattr(self, 'unearthed_cards') else set()
        cloned_state.jump_start_cards = self.jump_start_cards.copy() if hasattr(self, 'jump_start_cards') else set()
        cloned_state.buyback_cards = self.buyback_cards.copy() if hasattr(self, 'buyback_cards') else set()
        cloned_state.flashback_cards = self.flashback_cards.copy() if hasattr(self, 'flashback_cards') else set()
        cloned_state.exile_at_end_of_combat = self.exile_at_end_of_combat[:] if hasattr(self, 'exile_at_end_of_combat') else []
        cloned_state.haste_until_eot = self.haste_until_eot.copy() if hasattr(self, 'haste_until_eot') else set()
        # Removed redundant haste tracker: cloned_state.has_haste_until_eot = self.has_haste_until_eot.copy() if hasattr(self, 'has_haste_until_eot') else set()
        cloned_state.banding_creatures = self.banding_creatures.copy() if hasattr(self, 'banding_creatures') else set()
        cloned_state.crewed_vehicles = self.crewed_vehicles.copy() if hasattr(self, 'crewed_vehicles') else set()
        cloned_state.morphed_cards = copy.deepcopy(self.morphed_cards) if hasattr(self, 'morphed_cards') else {}
        cloned_state.manifested_cards = copy.deepcopy(self.manifested_cards) if hasattr(self, 'manifested_cards') else {}
        cloned_state.boast_activated = self.boast_activated.copy() if hasattr(self, 'boast_activated') else set()
        cloned_state.forecast_used = self.forecast_used.copy() if hasattr(self, 'forecast_used') else set()
        cloned_state.epic_spells = copy.deepcopy(self.epic_spells) if hasattr(self, 'epic_spells') else {}
        # Note: city_blessing moved to player state
        cloned_state.myriad_tokens = self.myriad_tokens[:] if hasattr(self, 'myriad_tokens') else []
        cloned_state.persist_returned = self.persist_returned.copy() if hasattr(self, 'persist_returned') else set()
        cloned_state.undying_returned = self.undying_returned.copy() if hasattr(self, 'undying_returned') else set()

        # --- Deep Copy Miracle State ---
        # Correctly link cloned player objects if original players existed
        cloned_miracle_player = None
        if hasattr(self, 'miracle_player') and self.miracle_player:
            if self.miracle_player == self.p1: cloned_miracle_player = cloned_state.p1
            elif self.miracle_player == self.p2: cloned_miracle_player = cloned_state.p2
        cloned_state.miracle_player = cloned_miracle_player
        cloned_state.miracle_cost = self.miracle_cost if hasattr(self, 'miracle_cost') else None
        cloned_state.miracle_cost_parsed = copy.deepcopy(self.miracle_cost_parsed) if hasattr(self, 'miracle_cost_parsed') else None

        # --- Deep Copy Context Objects (Crucial for multi-step actions) ---
        cloned_state.targeting_context = copy.deepcopy(self.targeting_context)
        if cloned_state.targeting_context and 'controller' in cloned_state.targeting_context:
            if cloned_state.targeting_context['controller'] == self.p1: cloned_state.targeting_context['controller'] = cloned_state.p1
            elif cloned_state.targeting_context['controller'] == self.p2: cloned_state.targeting_context['controller'] = cloned_state.p2

        cloned_state.sacrifice_context = copy.deepcopy(self.sacrifice_context)
        if cloned_state.sacrifice_context and 'controller' in cloned_state.sacrifice_context:
            if cloned_state.sacrifice_context['controller'] == self.p1: cloned_state.sacrifice_context['controller'] = cloned_state.p1
            elif cloned_state.sacrifice_context['controller'] == self.p2: cloned_state.sacrifice_context['controller'] = cloned_state.p2

        cloned_state.choice_context = copy.deepcopy(self.choice_context)
        if cloned_state.choice_context and 'player' in cloned_state.choice_context:
            if cloned_state.choice_context['player'] == self.p1: cloned_state.choice_context['player'] = cloned_state.p1
            elif cloned_state.choice_context['player'] == self.p2: cloned_state.choice_context['player'] = cloned_state.p2

        cloned_state.spree_context = copy.deepcopy(self.spree_context)
        cloned_state.dredge_pending = copy.deepcopy(self.dredge_pending)
        if cloned_state.dredge_pending and 'player' in cloned_state.dredge_pending:
            if cloned_state.dredge_pending['player'] == self.p1: cloned_state.dredge_pending['player'] = cloned_state.p1
            elif cloned_state.dredge_pending['player'] == self.p2: cloned_state.dredge_pending['player'] = cloned_state.p2

        cloned_state.madness_cast_available = copy.deepcopy(self.madness_cast_available) if hasattr(self, 'madness_cast_available') else None
        if cloned_state.madness_cast_available and 'player' in cloned_state.madness_cast_available:
             if cloned_state.madness_cast_available['player'] == self.p1: cloned_state.madness_cast_available['player'] = cloned_state.p1
             elif cloned_state.madness_cast_available['player'] == self.p2: cloned_state.madness_cast_available['player'] = cloned_state.p2

        cloned_state.pending_spell_context = copy.deepcopy(self.pending_spell_context)
        cloned_state.clash_context = copy.deepcopy(self.clash_context)

        # --- Deep Copy Surveil State ---
        cloned_state.surveil_in_progress = self.surveil_in_progress if hasattr(self, 'surveil_in_progress') else False
        cloned_state.cards_being_surveiled = self.cards_being_surveiled[:] if hasattr(self, 'cards_being_surveiled') else []
        cloned_surveiling_player = None
        if hasattr(self, 'surveiling_player') and self.surveiling_player:
            if self.surveiling_player == self.p1: cloned_surveiling_player = cloned_state.p1
            elif self.surveiling_player == self.p2: cloned_surveiling_player = cloned_state.p2
        cloned_state.surveiling_player = cloned_surveiling_player

        # --- Deep Copy Scry State ---
        cloned_state.scry_in_progress = self.scry_in_progress if hasattr(self, 'scry_in_progress') else False
        cloned_state.scrying_cards = self.scrying_cards[:] if hasattr(self, 'scrying_cards') else []
        cloned_scrying_player = None
        if hasattr(self, 'scrying_player') and self.scrying_player:
             if self.scrying_player == self.p1: cloned_scrying_player = cloned_state.p1
             elif self.scrying_player == self.p2: cloned_scrying_player = cloned_state.p2
        cloned_state.scrying_player = cloned_scrying_player
        cloned_state.scrying_tops = self.scrying_tops[:] if hasattr(self, 'scrying_tops') else []
        cloned_state.scrying_bottoms = self.scrying_bottoms[:] if hasattr(self, 'scrying_bottoms') else []

        # --- Deep Copy Mulligan State ---
        cloned_mulligan_player = None
        if hasattr(self, 'mulligan_player') and self.mulligan_player:
             if self.mulligan_player == self.p1: cloned_mulligan_player = cloned_state.p1
             elif self.mulligan_player == self.p2: cloned_mulligan_player = cloned_state.p2
        cloned_state.mulligan_player = cloned_mulligan_player
        cloned_state.mulligan_count = copy.deepcopy(self.mulligan_count) if hasattr(self, 'mulligan_count') else {'p1':0, 'p2':0}
        cloned_state.mulligan_data = copy.deepcopy(self.mulligan_data) if hasattr(self, 'mulligan_data') else {'p1':0, 'p2':0}

        cloned_bottoming_player = None
        if hasattr(self, 'bottoming_player') and self.bottoming_player:
             if self.bottoming_player == self.p1: cloned_bottoming_player = cloned_state.p1
             elif self.bottoming_player == self.p2: cloned_bottoming_player = cloned_state.p2
        cloned_state.bottoming_player = cloned_bottoming_player


        # --- Restore priority player reference correctly ---
        cloned_priority_player = None
        if hasattr(self, 'priority_player') and self.priority_player:
             if self.priority_player == self.p1: cloned_priority_player = cloned_state.p1
             elif self.priority_player == self.p2: cloned_priority_player = cloned_state.p2
        cloned_state.priority_player = cloned_priority_player

        # --- Re-initialize Subsystems within the CLONED state ---
        # This step is crucial: create new instances of subsystems linked to the cloned GameState
        cloned_state._init_subsystems() # Re-populates subsystems on cloned_state

        # --- Deep Copy Ability Handler State (if needed) ---
        # registered_abilities cache is large, potentially re-parse instead of deep copy
        if cloned_state.ability_handler:
            # Option 1: Re-parse (Simpler, ensures links are correct, might be slow)
            cloned_state.ability_handler.registered_abilities = {}
            # We need to re-register static effects, replacements too
            for player in [cloned_state.p1, cloned_state.p2]:
                if player:
                    for card_id in list(player.get("battlefield", [])): # Check battlefield cards
                        card = cloned_state._safe_get_card(card_id)
                        if card:
                            cloned_state.ability_handler._parse_and_register_abilities(card_id, card) # Re-parse and apply static
                            if cloned_state.replacement_effects:
                                cloned_state.replacement_effects.register_card_replacement_effects(card_id, player) # Re-register replacements
            # Option 2: Attempt deep copy (Risky due to object references within Ability objects)
            # cloned_state.ability_handler.registered_abilities = copy.deepcopy(self.ability_handler.registered_abilities)
            # Fix source_card references in copied abilities (VERY complex)
            cloned_state.ability_handler.active_triggers = copy.deepcopy(self.ability_handler.active_triggers) if hasattr(self.ability_handler,'active_triggers') else []
            # Fix controller references in active_triggers
            if hasattr(cloned_state.ability_handler,'active_triggers'):
                for i in range(len(cloned_state.ability_handler.active_triggers)):
                    ability, controller_orig = cloned_state.ability_handler.active_triggers[i]
                    controller_cloned = cloned_state.p1 if controller_orig == self.p1 else cloned_state.p2 if controller_orig == self.p2 else None
                    cloned_state.ability_handler.active_triggers[i] = (ability, controller_cloned)

        # --- Deep Copy Layer System State ---
        if cloned_state.layer_system:
            # Need to deep copy the layers structure and update references in effect_data
            cloned_state.layers = {}
            for layer_num, effects_list_or_dict in self.layers.items():
                if isinstance(effects_list_or_dict, dict): # Layer 7
                    cloned_state.layers[layer_num] = {}
                    for sublayer, effects_list in effects_list_or_dict.items():
                        cloned_sublayer = []
                        for eid, data in effects_list:
                            copied_data = copy.deepcopy(data)
                            # Fix controller reference if present
                            if 'controller_id' in copied_data:
                                ctrl_orig = copied_data['controller_id']
                                ctrl_cloned = cloned_state.p1 if ctrl_orig == self.p1 else cloned_state.p2 if ctrl_orig == self.p2 else None
                                copied_data['controller_id'] = ctrl_cloned
                            # Fix condition reference (if it captures 'self') - Very hard
                            # Assume condition functions don't rely on implicit self for now
                            cloned_sublayer.append((eid, copied_data))
                        cloned_state.layers[layer_num][sublayer] = cloned_sublayer
                else: # Layers 1-6
                    cloned_layer = []
                    for eid, data in effects_list_or_dict:
                         copied_data = copy.deepcopy(data)
                         if 'controller_id' in copied_data:
                             ctrl_orig = copied_data['controller_id']
                             ctrl_cloned = cloned_state.p1 if ctrl_orig == self.p1 else cloned_state.p2 if ctrl_orig == self.p2 else None
                             copied_data['controller_id'] = ctrl_cloned
                         cloned_layer.append((eid, copied_data))
                    cloned_state.layers[layer_num] = cloned_layer
            cloned_state.timestamps = self.timestamps.copy()
            cloned_state.effect_counter = self.effect_counter
            cloned_state.dependencies = copy.deepcopy(self.dependencies) # Dependencies might be complex
            cloned_state._last_applied_state_hash = self._last_applied_state_hash # Can copy hash

        # --- Deep Copy Replacement Effects State ---
        if cloned_state.replacement_effects:
             cloned_state.replacement_effects.active_effects = []
             cloned_state.replacement_effects.effect_index = defaultdict(list)
             for data in self.replacement_effects.active_effects:
                 copied_data = copy.deepcopy(data)
                 # Fix player references
                 if 'controller_id' in copied_data:
                     ctrl_orig = copied_data['controller_id']
                     ctrl_cloned = cloned_state.p1 if ctrl_orig == self.p1 else cloned_state.p2 if ctrl_orig == self.p2 else None
                     copied_data['controller_id'] = ctrl_cloned
                 cloned_state.replacement_effects.active_effects.append(copied_data)
                 # Rebuild index
                 event_type = copied_data.get('event_type')
                 if event_type:
                     cloned_state.replacement_effects.effect_index[event_type].append(copied_data)
             cloned_state.replacement_effects.effect_counter = self.replacement_effects.effect_counter

        # --- Link External Systems (Shallow Copy/Reference) ---
        cloned_state.strategy_memory = self.strategy_memory  # Just reference
        cloned_state.stats_tracker = self.stats_tracker      # Just reference
        cloned_state.card_memory = self.card_memory          # Just reference

        # --- Ensure internal subsystem references point to the cloned state ---
        if cloned_state.mana_system: cloned_state.mana_system.game_state = cloned_state
        if cloned_state.combat_resolver: cloned_state.combat_resolver.game_state = cloned_state
        if cloned_state.card_evaluator: cloned_state.card_evaluator.game_state = cloned_state
        if cloned_state.targeting_system: cloned_state.targeting_system.game_state = cloned_state
        if cloned_state.ability_handler: cloned_state.ability_handler.game_state = cloned_state
        if cloned_state.layer_system: cloned_state.layer_system.game_state = cloned_state
        if cloned_state.replacement_effects: cloned_state.replacement_effects.game_state = cloned_state
        if cloned_state.strategic_planner: cloned_state.strategic_planner.game_state = cloned_state
        # Ensure ability_handler subsystems also point to cloned state subsystems
        if cloned_state.ability_handler and cloned_state.targeting_system:
            cloned_state.ability_handler.targeting_system = cloned_state.targeting_system

        logging.debug("GameState cloned successfully.")
        return cloned_state
        
    def _safe_get_card(self, card_id, default_value=None):
        """Safely get a card with proper error handling and type checking"""
        try:
            # Handle case where card_id is itself already a Card object
            if isinstance(card_id, Card):
                return card_id
                    
            # Use standardized dictionary format
            if card_id in self.card_db:
                return self.card_db[card_id]
                    
            # Log only on first occurrence
            if not hasattr(self, '_logged_card_ids'):
                type(self)._logged_card_ids = set()
            if card_id not in self._logged_card_ids:
                self._logged_card_ids.add(card_id)
                logging.warning(f"Failed to find card with ID {card_id}")
                    
            # Return default or create default Card
            return default_value or Card({
                "name": f"Unknown Card {card_id}", 
                "type_line": "unknown", 
                "oracle_text": "", 
                "power": "0", 
                "toughness": "0", 
                "mana_cost": "", 
                "cmc": 0, 
                "keywords": [0]*11, 
                "card_types": ["unknown"], 
                "colors": [0,0,0,0,0],
                "subtypes": []
            })
        except Exception as e:
            logging.warning(f"Error accessing card with ID {card_id}: {str(e)}")
            return default_value or Card({
                "name": f"Unknown Card {card_id}", 
                "type_line": "unknown", 
                "oracle_text": "", 
                "power": "0", 
                "toughness": "0", 
                "mana_cost": "", 
                "cmc": 0, 
                "keywords": [0]*11, 
                "card_types": ["unknown"], 
                "colors": [0,0,0,0,0],
                "subtypes": []
            })
            
    def resolve_spell_effects(self, spell_id, controller, targets=None, context=None):
        """
        Apply the effects of a spell using AbilityEffect objects.
        
        Args:
            spell_id: The ID of the spell to resolve
            controller: The player casting the spell
            targets: Dictionary of targets for the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            logging.warning(f"Cannot resolve spell effects: card {spell_id} not found")
            return
        
        # If ability_handler is available, use it to create effect objects
        if hasattr(self, 'ability_handler'):
            try:
                effect_text = spell.oracle_text if hasattr(spell, 'oracle_text') else ""
                
                # Use the ability handler to create effect objects
                if hasattr(self.ability_handler, '_create_ability_effects'):
                    effects = self.ability_handler._create_ability_effects(effect_text, targets)
                    
                    for effect in effects:
                        effect.apply(self, spell_id, controller, targets)
                        
                    logging.debug(f"Applied effects for {spell.name if hasattr(spell, 'name') else 'unknown spell'}")
                    
                    # Check state-based actions after resolution
                    self.check_state_based_actions()
                    
                    # Process additional keyword abilities after main effects
                    self._process_keyword_abilities(spell_id, controller, context)
                    return
            except Exception as e:
                logging.error(f"Error creating or applying effect objects: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
        
        # Process keyword abilities after other effects
        self._process_keyword_abilities(spell_id, controller, context)
        
        # Check state-based actions after resolution
        self.check_state_based_actions()
        
    def initialize_day_night_cycle(self):
        """Initialize the day/night cycle state and tracking."""
        # Start with neither day nor night
        self.day_night_state = None
        # Track if we've already checked day/night transition this turn
        self.day_night_checked_this_turn = False
        logging.debug("Day/night cycle initialized (neither day nor night)")

    def check_day_night_transition(self):
        """
        Check and update the day/night state based on spells cast this turn.
        This is called during the end step of each turn.
        """
        if self.day_night_checked_this_turn:
            return
            
        # Count spells cast by active player this turn
        active_player = self._get_active_player()
        spells_cast = sum(1 for spell in self.spells_cast_this_turn if isinstance(spell, tuple) 
                        and len(spell) >= 2 and spell[1] == active_player)
        
        old_state = self.day_night_state
        
        # Apply transition rules
        if self.day_night_state is None:
            # If neither day nor night, and no spells were cast, it becomes night
            if spells_cast == 0:
                self.day_night_state = "night"
                logging.debug("It becomes night (no spells cast)")
            elif spells_cast >= 1:
                self.day_night_state = "day"
                logging.debug(f"It becomes day (player cast {spells_cast} spells)")
        elif self.day_night_state == "day":
            # If day, and at least two spells were cast, it becomes night
            if spells_cast >= 2:
                self.day_night_state = "night"
                logging.debug(f"It becomes night (player cast {spells_cast} spells)")
        elif self.day_night_state == "night":
            # If night, and no spells were cast, it becomes day
            if spells_cast == 0:
                self.day_night_state = "day"
                logging.debug("It becomes day (no spells cast)")
        
        # If the state changed, transform all daybound/nightbound cards
        if self.day_night_state != old_state:
            self.transform_day_night_cards()
        
        self.day_night_checked_this_turn = True

    def transform_day_night_cards(self):
        """
        Transform all daybound/nightbound cards when day/night state changes.
        
        Returns:
            list: List of cards that were transformed
        """
        if not hasattr(self, 'day_night_state'):
            logging.warning("Day/night state not initialized")
            return []
            
        transformed_cards = []
        
        # Process all permanents in battlefield
        for player in [self.p1, self.p2]:
            for card_id in player["battlefield"]:
                card = self._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    continue
                
                oracle_text = card.oracle_text.lower()
                has_daybound = "daybound" in oracle_text
                has_nightbound = "nightbound" in oracle_text
                
                if not has_daybound and not has_nightbound:
                    continue
                    
                # Determine if card should transform
                should_transform = False
                if has_daybound and self.day_night_state == "night" and not getattr(card, "is_night_side", False):
                    should_transform = True
                elif has_nightbound and self.day_night_state == "day" and getattr(card, "is_night_side", True):
                    should_transform = True
                    
                # Apply transformation
                if should_transform and hasattr(card, "transform"):
                    # Transform the card
                    card.transform()
                    transformed_cards.append(card_id)
                    
                    # Create context for transformation triggers
                    context = {
                        "card": card,
                        "controller": player,
                        "from_state": "day" if has_daybound else "night",
                        "to_state": "night" if has_daybound else "day"
                    }
                    
                    # Trigger transformation ability
                    self.trigger_ability(card_id, "TRANSFORMED", context)
                    
                    # Also trigger day/night change ability
                    self.trigger_ability(card_id, "DAY_NIGHT_CHANGED", context)
                    
                    logging.debug(f"{card.name} transformed due to day/night change")
        
        return transformed_cards
        
    def _process_keyword_abilities(self, spell_id, controller, context):
        """
        Process additional keyword abilities after the main spell effect.
        
        Args:
            spell_id: The ID of the spell
            controller: The player casting the spell
            context: Additional context information
        """
        spell = self._safe_get_card(spell_id)
        if not spell or not hasattr(spell, 'oracle_text'):
            return
            
        effect_text = spell.oracle_text.lower()
        
        # Storm ability handling
        if context.get("has_storm", False) or "storm" in effect_text:
            # Count spells cast this turn before this one
            storm_count = len([s for s in self.spells_cast_this_turn if s[1] == controller])
            
            if storm_count > 0:
                # Create copies
                for _ in range(storm_count):
                    self.stack.append(("SPELL", spell_id, controller, {"is_copy": True}))
                
                logging.debug(f"Storm: Created {storm_count} copies of {spell.name if hasattr(spell, 'name') else 'spell'}")
        
        # Cascade ability handling
        if context.get("has_cascade", False) or "cascade" in effect_text:
            # Find a lower-cost spell in library
            if controller["library"]:
                cascade_cost = None
                if hasattr(spell, 'cmc'):
                    cascade_cost = spell.cmc
                
                # Find first card with lower mana value
                found_card = None
                found_idx = -1
                
                # Reveal cards until we find one with lower cost
                for idx, lib_card_id in enumerate(controller["library"]):
                    lib_card = self._safe_get_card(lib_card_id)
                    if lib_card and hasattr(lib_card, 'cmc') and lib_card.cmc < cascade_cost:
                        if not hasattr(lib_card, 'card_types') or \
                        ('land' not in lib_card.card_types):
                            found_card = lib_card
                            found_idx = idx
                            break
                
                if found_card and found_idx >= 0:
                    # Cast the found card for free
                    cascade_card_id = controller["library"].pop(found_idx)
                    self.stack.append(("SPELL", cascade_card_id, controller, {"is_free": True}))
                    logging.debug(f"Cascade: Cast {found_card.name} for free")
                    
                    # Put the rest on the bottom in random order
                    revealed_cards = controller["library"][:found_idx]
                    controller["library"] = controller["library"][found_idx:]
                    random.shuffle(revealed_cards)
                    controller["library"].extend(revealed_cards)
        
        # Flashback handling for exile instead of graveyard
        if hasattr(self, 'flashback_cards') and spell_id in self.flashback_cards:
            # Mark to prevent going to graveyard
            context["skip_default_movement"] = True
            
            # Move to exile
            controller["exile"].append(spell_id)
            self.flashback_cards.remove(spell_id)
            logging.debug(f"Flashback: Exiled {spell.name if hasattr(spell, 'name') else 'spell'} after resolution")
        
        # Buyback handling for return to hand instead of graveyard
        if context.get("buyback", False) or (hasattr(self, 'buyback_cards') and spell_id in self.buyback_cards):
            # Mark to prevent going to graveyard
            context["skip_default_movement"] = True
            
            # Return to hand
            controller["hand"].append(spell_id)
            if hasattr(self, 'buyback_cards') and spell_id in self.buyback_cards:
                self.buyback_cards.remove(spell_id)
            logging.debug(f"Buyback: Returned {spell.name if hasattr(spell, 'name') else 'spell'} to hand")
            

    def add_counter(self, card_id, counter_type, count=1):
        """Add counters to a permanent, including handling P/T updates and annihilation."""
        target_owner = self._find_card_controller(card_id) # Need controller
        if not target_owner:
            logging.warning(f"Cannot add counter to {card_id}: Not found on battlefield.")
            return False

        target_card = self._safe_get_card(card_id)
        if not target_card: return False

        if not hasattr(target_card, 'counters'): target_card.counters = {}

        # Modify count directly
        current_count = target_card.counters.get(counter_type, 0)
        new_count = current_count + count
        if new_count <= 0: # Removing counters
             if current_count >= abs(count): # Ensure we can remove that many
                  target_card.counters[counter_type] = new_count
                  if new_count == 0: del target_card.counters[counter_type]
             else: # Cannot remove more than available
                  del target_card.counters[counter_type]
                  count = -current_count # Adjust actual count removed
                  logging.warning(f"Tried to remove {abs(count)} {counter_type} counters from {target_card.name}, but only {current_count} were present.")
                  if current_count == 0: return False # Nothing to remove

        else: # Adding counters
            target_card.counters[counter_type] = new_count

        logging.debug(f"Updated {counter_type} counters on {target_card.name} by {count}. New count: {target_card.counters.get(counter_type, 0)}")

        # Handle +1/+1 and -1/-1 interactions and P/T updates
        # Note: Layer system SHOULD ideally handle P/T, but direct update is simpler here for SBAs
        plus_counters = target_card.counters.get('+1/+1', 0)
        minus_counters = target_card.counters.get('-1/-1', 0)
        original_power = getattr(target_card, 'power', 0)
        original_toughness = getattr(target_card, 'toughness', 0)


        # Annihilation Rule (704.5r)
        if plus_counters > 0 and minus_counters > 0:
            remove_amt = min(plus_counters, minus_counters)
            logging.debug(f"Annihilating {remove_amt} +1/+1 and -1/-1 counters on {target_card.name}")
            target_card.counters['+1/+1'] -= remove_amt
            target_card.counters['-1/-1'] -= remove_amt
            if target_card.counters['+1/+1'] == 0: del target_card.counters['+1/+1']
            if target_card.counters['-1/-1'] == 0: del target_card.counters['-1/-1']
            plus_counters -= remove_amt # Update local count for P/T calc
            minus_counters -= remove_amt

        # Recalculate P/T based ONLY on remaining +1/+1 and -1/-1 counters for SBA check
        # Base P/T should come from card data or Layer 7a effects
        # This part is complex due to layers. A simplified approach:
        if hasattr(target_card,'_base_power'): # Assuming Layer system stores base
            power_mod = plus_counters - minus_counters
            toughness_mod = plus_counters - minus_counters
            target_card.power = target_card._base_power + power_mod
            target_card.toughness = target_card._base_toughness + toughness_mod
        else: # Less accurate fallback if Layer system isn't setting base P/T
             if counter_type == '+1/+1':
                 if hasattr(target_card,'power'): target_card.power += count
                 if hasattr(target_card,'toughness'): target_card.toughness += count
             elif counter_type == '-1/-1':
                 if hasattr(target_card,'power'): target_card.power = max(0, target_card.power + count) # count is negative here
                 if hasattr(target_card,'toughness'): target_card.toughness = max(0, target_card.toughness + count)


        # Trigger counter addition/removal events
        if count > 0:
            self.trigger_ability(card_id, "COUNTER_ADDED", {"controller": target_owner, "counter_type": counter_type, "count": count})
        else:
             self.trigger_ability(card_id, "COUNTER_REMOVED", {"controller": target_owner, "counter_type": counter_type, "count": abs(count)})

        # SBAs check might be needed immediately after counter changes, especially -1/-1
        self.check_state_based_actions()
        return True
    
    def handle_miracle_draw(self, card_id, player):
        """
        Handle drawing a card with miracle, giving the player a chance to cast it for its miracle cost.
        
        Args:
            card_id: ID of the drawn card
            player: The player who drew the card
            
        Returns:
            bool: Whether the miracle was handled
        """
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text') or "miracle" not in card.oracle_text.lower():
            return False
                
        # Parse miracle cost
        import re
        match = re.search(r"miracle\s+([^\(]+)(?:\(|$)", card.oracle_text.lower())
        miracle_cost = match.group(1).strip() if match else None
        
        if not miracle_cost:
            logging.warning(f"Could not parse miracle cost for {card.name}")
            return False
                
        # Set up miracle window
        self.miracle_card = card_id
        self.miracle_cost = miracle_cost
        self.miracle_player = player
        
        # Track that this is the first card drawn this turn (to meet miracle conditions)
        if not hasattr(self, 'cards_drawn_this_turn'):
            self.cards_drawn_this_turn = {}
            
        player_key = "p1" if player == self.p1 else "p2"
        turn_key = self.turn
        if turn_key not in self.cards_drawn_this_turn:
            self.cards_drawn_this_turn[turn_key] = {}
        if player_key not in self.cards_drawn_this_turn[turn_key]:
            self.cards_drawn_this_turn[turn_key][player_key] = []
            
        # Check if this is the first card drawn this turn
        is_first_draw = len(self.cards_drawn_this_turn[turn_key].get(player_key, [])) == 0
        self.cards_drawn_this_turn[turn_key][player_key].append(card_id)
        
        # Only offer miracle if this is the first draw and player can afford
        if is_first_draw and hasattr(self, 'mana_system'):
            parsed_cost = self.mana_system.parse_mana_cost(miracle_cost)
            if self.mana_system.can_pay_mana_cost(player, parsed_cost):
                logging.debug(f"Miracle opportunity for {card.name}")
                
                # Set up the miracle state for action generation
                self.miracle_active = True
                self.miracle_card_id = card_id
                self.miracle_cost_parsed = parsed_cost
                
                # In a full implementation, we'd set a flag and let the agent choose 
                # whether to cast via miracle. For now, we'll just return True to
                # indicate the miracle was set up successfully.
                return True
        
        return False
    
    def surveil(self, player, count=1):
        """
        Implement the Surveil mechanic.
        Look at top N cards of library, put any number in graveyard and rest on top in any order.
        
        Args:
            player: Player dictionary
            count: Number of cards to surveil
            
        Returns:
            list: The cards that were surveiled
        """
        if not player["library"]:
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        
        if count <= 0:
            return []
        
        # Look at top N cards without removing them yet
        surveiled_cards = [player["library"][i] for i in range(count)]
        
        # Store the surveiling state for action generation
        self.surveil_in_progress = True
        self.cards_being_surveiled = surveiled_cards.copy()
        self.surveiling_player = player
        
        logging.debug(f"Started surveiling {count} cards - waiting for surveil actions")
        
        return surveiled_cards

    def scry(self, player, count=1):
        """
        Implement the Scry mechanic with better decision-making.
        Look at top N cards of library, put any number on bottom and rest on top in any order.
        
        Args:
            player: Player dictionary
            count: Number of cards to scry
            
        Returns:
            list: The cards that were scryed
        """
        if not player["library"]:
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        
        if count <= 0:
            return []
        
        # Look at top N cards without removing them yet
        scryed_cards = [player["library"][i] for i in range(count)]
        
        # Store the scrying state for action generation
        self.scry_in_progress = True
        self.scrying_cards = scryed_cards.copy()
        self.scrying_player = player
        self.scrying_tops = []
        self.scrying_bottoms = []
        
        logging.debug(f"Started scrying {count} cards - waiting for scry actions")
        
        return scryed_cards

    def check_priority(self, player=None):
        """
        Check if player has priority and can take actions.
        In Magic: The Gathering, priority determines which player can take game actions.
        """
        # If player is None, check active player
        if player is None:
            player = self._get_active_player()
        
        # In these phases, no player gets priority
        if self.phase in [self.PHASE_UNTAP, self.PHASE_CLEANUP]:
            return False
            
        # In general, active player gets priority first in each step
        active_player = self._get_active_player()
        
        # If stack is not empty, the player who last added to the stack passes priority
        if self.stack and hasattr(self, 'last_stack_actor'):
            return player != self.last_stack_actor
            
        # Otherwise active player has priority by default
        return player == active_player


    def advance_saga_counters(self, player):
        """
        Advance saga counters at the beginning of the main phase.
        This implements the rules for Saga enchantments from Dominaria.
        
        Args:
            player: The player whose Sagas to advance
            
        Returns:
            list: List of Sagas that were advanced
        """
        # Check if saga counters tracking exists
        if not hasattr(self, "saga_counters"):
            self.saga_counters = {}
        
        # Find all Sagas in the battlefield
        sagas = []
        for card_id in player["battlefield"]:
            card = self._safe_get_card(card_id)
            if (card and hasattr(card, 'card_types') and 'enchantment' in card.card_types
                and hasattr(card, 'subtypes') and 'saga' in [s.lower() for s in card.subtypes]):
                sagas.append(card_id)
        
        advanced_sagas = []
        
        # Process each Saga
        for saga_id in sagas:
            # Get current chapter
            current_chapter = self.saga_counters.get(saga_id, 0)
            
            # Advance to next chapter
            new_chapter = current_chapter + 1
            self.saga_counters[saga_id] = new_chapter
            advanced_sagas.append(saga_id)
            
            # Trigger chapter ability
            saga_card = self._safe_get_card(saga_id)
            context = {
                "card": saga_card,
                "controller": player,
                "chapter": new_chapter
            }
            
            self.trigger_ability(saga_id, "SAGA_CHAPTER", context)
            logging.debug(f"Saga {saga_card.name} advanced to chapter {new_chapter}")
            
            # Check if saga is completed (usually after chapter 3)
            chapter_count = 0
            if hasattr(saga_card, 'oracle_text'):
                # Count chapter abilities (look for "I", "II", "III", etc.)
                chapter_pattern = re.compile(r"(^|\n)([IVX]+) —", re.MULTILINE)
                chapter_matches = chapter_pattern.findall(saga_card.oracle_text)
                chapter_count = len(chapter_matches)
            
            # Default to 3 chapters if we couldn't determine count
            if chapter_count == 0:
                chapter_count = 3
            
            # If we're past the last chapter, sacrifice the saga
            if new_chapter > chapter_count:
                self.move_card(saga_id, player, "battlefield", player, "graveyard")
                self.trigger_ability(saga_id, "SAGA_SACRIFICED", {"chapter": new_chapter})
                logging.debug(f"Saga {saga_card.name} completed and sacrificed")
        
        return advanced_sagas

    def resolve_modal_spell(self, card_id, controller, modes=None, context=None):
        """
        Resolve a spell with multiple modes.
        
        Args:
            card_id: ID of the modal spell
            controller: The player who cast the spell
            modes: List of selected mode indices
            context: Additional context for resolution
            
        Returns:
            bool: Whether resolution was successful
        """
        if not context:
            context = {}
            
        card = self._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Parse modes from oracle text
        mode_texts = []
        
        # Look for standard bullet point modes
        bullet_modes = re.findall(r'[•\-−–—] (.*?)(?=[•\-−–—]|$)', oracle_text, re.DOTALL)
        if bullet_modes:
            mode_texts = bullet_modes
        
        # Look for numbered modes
        if not mode_texts:
            numbered_modes = re.findall(r'(\d+\. .*?)(?=\d+\. |$)', oracle_text, re.DOTALL)
            if numbered_modes:
                mode_texts = numbered_modes
        
        # Check for "choose one" or similar text
        choose_match = re.search(r'choose (one|two|up to two|up to three|one or more)', oracle_text)
        max_modes = 1
        if choose_match:
            choice_text = choose_match.group(1)
            if choice_text == "two":
                max_modes = 2
            elif choice_text == "up to two":
                max_modes = 2
            elif choice_text == "up to three":
                max_modes = 3
            elif choice_text == "one or more":
                max_modes = len(mode_texts)
        
        # Check for entwine
        has_entwine = "entwine" in oracle_text
        if has_entwine and "entwine" in context:
            # With entwine, we can choose all modes
            max_modes = len(mode_texts)
        
        # Check for kicker
        has_kicker = "kicker" in oracle_text
        if has_kicker and "kicked" in context:
            # Some kicked spells have additional effects
            kicked_modes = []
            for mode_text in mode_texts:
                if "if this spell was kicked" in mode_text:
                    kicked_modes.append(mode_text)
            
            # Add kicked modes to the selection
            if not modes:
                modes = []
            for i, mode_text in enumerate(mode_texts):
                if mode_text in kicked_modes:
                    modes.append(i)
        
        # If no modes specified, default to just the first mode
        if not modes and mode_texts:
            modes = [0]
        
        # Limit number of selected modes
        if len(modes) > max_modes:
            modes = modes[:max_modes]
        
        # Process each selected mode
        successful_modes = 0
        for mode_idx in modes:
            if 0 <= mode_idx < len(mode_texts):
                mode_text = mode_texts[mode_idx]
                
                # Process the effect based on the mode text
                # This would need more detailed implementation to handle all possible effects
                if "draw" in mode_text and "card" in mode_text:
                    # Draw cards effect
                    match = re.search(r'draw (\w+) cards?', mode_text)
                    count = 1
                    if match:
                        count_word = match.group(1)
                        if count_word.isdigit():
                            count = int(count_word)
                        elif count_word == "two":
                            count = 2
                        elif count_word == "three":
                            count = 3
                    
                    for _ in range(count):
                        self._draw_phase(controller)
                    
                    successful_modes += 1
                    logging.debug(f"Modal spell: Mode {mode_idx} - Drew {count} cards")
                    
                elif "destroy" in mode_text or "exile" in mode_text:
                    # Destruction/exile effect
                    # For simplicity, just destroy a creature
                    opponent = self.p2 if controller == self.p1 else self.p1
                    creatures = [cid for cid in opponent["battlefield"] 
                            if self._safe_get_card(cid) and hasattr(self._safe_get_card(cid), 'card_types') 
                            and 'creature' in self._safe_get_card(cid).card_types]
                    
                    if creatures:
                        target = creatures[0]  # Just take first one for simplicity
                        target_card = self._safe_get_card(target)
                        
                        if "exile" in mode_text:
                            self.move_card(target, opponent, "battlefield", opponent, "exile")
                        else:
                            self.move_card(target, opponent, "battlefield", opponent, "graveyard")
                        
                        successful_modes += 1
                        action = "Exiled" if "exile" in mode_text else "Destroyed"
                        logging.debug(f"Modal spell: Mode {mode_idx} - {action} {target_card.name}")
                
                elif "gain" in mode_text and "life" in mode_text:
                    # Life gain effect
                    match = re.search(r'gain (\w+) life', mode_text)
                    amount = 3  # Default
                    if match:
                        amount_word = match.group(1)
                        if amount_word.isdigit():
                            amount = int(amount_word)
                        elif amount_word == "two":
                            amount = 2
                        elif amount_word == "three":
                            amount = 3
                        elif amount_word == "four":
                            amount = 4
                    
                    controller["life"] += amount
                    successful_modes += 1
                    logging.debug(f"Modal spell: Mode {mode_idx} - Gained {amount} life")
                
                # Add more mode effect handlers as needed
        
        # Move the spell to the graveyard after resolution
        controller["graveyard"].append(card_id)
        
        return successful_modes > 0

    # --- Added method in GameState ---
    def perform_dredge(self, player, dredge_card_id):
        """Performs the dredge action after the player confirms."""
        dredge_info = getattr(self, 'dredge_pending', None)
        if not dredge_info or dredge_info['player'] != player or dredge_info['card_id'] != dredge_card_id:
            logging.warning("Invalid state for perform_dredge.")
            self.dredge_pending = None # Clear inconsistent state
            return False

        dredge_val = dredge_info['value']
        source_zone = dredge_info.get('source_zone', 'graveyard')

        # Double check card location and library size
        current_owner, current_zone = self.find_card_location(dredge_card_id)
        if current_owner != player or current_zone != source_zone:
            logging.warning(f"Dredge card {dredge_card_id} no longer in {player['name']}'s {source_zone}.")
            self.dredge_pending = None
            return False
        if len(player.get("library", [])) < dredge_val:
            logging.warning(f"Cannot dredge {dredge_card_id}: Not enough cards in library ({len(player['library'])}/{dredge_val}).")
            self.dredge_pending = None
            return False

        # Mill N cards
        milled_count = 0
        ids_to_mill = player["library"][:dredge_val]
        player["library"] = player["library"][dredge_val:] # Remove from library first

        for card_id_to_mill in ids_to_mill:
            # Use move_card to handle triggers for milling
            if self.move_card(card_id_to_mill, player, "library_implicit", player, "graveyard", cause="mill_dredge"):
                 milled_count += 1
            else:
                 logging.error(f"Failed to move {card_id_to_mill} to graveyard during dredge mill.")
                 # Should attempt to put back? State might be complex.

        # Return dredged card to hand
        success_move = self.move_card(dredge_card_id, player, source_zone, player, "hand", cause="dredge_return")

        # Clear pending state regardless of move success
        self.dredge_pending = None

        if success_move:
            card = self._safe_get_card(dredge_card_id)
            card_name = getattr(card, 'name', dredge_card_id)
            # Trigger DREDGED event
            self.trigger_ability(dredge_card_id, "DREDGED", {"controller": player, "milled": milled_count})
            logging.info(f"Performed dredge: Returned {card_name}, milled {milled_count}.")
            # Return to priority phase (since draw was replaced)
            self.phase = self.PHASE_PRIORITY
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0
            return True
        else:
            logging.error(f"Dredge failed during final move_card for {dredge_card_id}")
            # Attempt recovery? Put milled cards back? Very complex state.
            return False

    
    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()
        name = getattr(card, 'name', '').lower()

        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        if criteria == name: return True
        # Add checks for colors, CMC, P/T if needed for more complex searches
        return False
    
    def search_library_and_choose(self, player, criteria, ai_choice_context=None):
        """Search library for a card matching criteria and let AI choose one."""
        matches = []
        indices_to_remove = []
        for i, card_id in enumerate(player["library"]):
            card = self._safe_get_card(card_id)
            if self._card_matches_criteria(card, criteria): # Uses GameState's helper now
                 matches.append(card_id)
                 indices_to_remove.append(i) # Store index along with card_id

        if not matches:
            logging.debug(f"Search failed: No '{criteria}' found in library.")
            if hasattr(self, 'shuffle_library'): self.shuffle_library(player) # Shuffle even on fail
            return None

        # AI Choice - Use CardEvaluator if available, else first match
        chosen_id = None
        if hasattr(self, 'card_evaluator') and self.card_evaluator:
             best_choice_id = None
             best_score = -float('inf')
             # Add turn and phase to context
             eval_context = {"current_turn": self.turn, "current_phase": self.phase, "goal": criteria}
             if ai_choice_context: eval_context.update(ai_choice_context)

             for card_id in matches:
                  score = self.card_evaluator.evaluate_card(card_id, "search_find", context_details=eval_context)
                  if score > best_score:
                       best_score = score
                       best_choice_id = card_id
             chosen_id = best_choice_id if best_choice_id is not None else (matches[0] if matches else None)
        elif matches:
            chosen_id = matches[0] # Simple: Choose first match

        # Remove chosen card from library and move to hand (default)
        if chosen_id:
             # Find index to remove (important if library changed during evaluation?)
             original_index = -1
             try:
                 # Iterate through stored indices
                 for i in indices_to_remove:
                     if player["library"][i] == chosen_id:
                         original_index = i
                         break
             except IndexError: # Handle case where library might have changed mid-search? Unlikely here.
                 logging.warning("Library changed during search? Cannot find index.")
                 pass # Fallback to just removing by value if index fails

             if original_index != -1:
                 player["library"].pop(original_index)
             else: # Fallback remove by value
                 if chosen_id in player["library"]: player["library"].remove(chosen_id)
                 else: logging.error("Chosen card vanished from library!"); chosen_id = None # Cannot proceed

        # Perform move and shuffle if card was successfully found and removed
        if chosen_id:
            target_zone = "hand" # Default target zone for search
            success_move = self.move_card(chosen_id, player, "library_implicit", player, target_zone, cause="search") # Use implicit source
            if not success_move: chosen_id = None # Move failed

        # Shuffle library after search
        if hasattr(self, 'shuffle_library'): self.shuffle_library(player)
        else: random.shuffle(player["library"])

        if chosen_id:
            logging.debug(f"Search found: Moved '{self._safe_get_card(chosen_id).name}' matching '{criteria}' to {target_zone}.")
        return chosen_id # Return ID of chosen card
    
    def give_haste_until_eot(self, card_id):
        """Grant haste until end of turn."""
        if not hasattr(self, 'haste_until_eot'): self.haste_until_eot = set()
        self.haste_until_eot.add(card_id)
    
    def get_stack_item_controller(self, stack_item_id):
        """Find the controller of a spell or ability on the stack."""
        for item in self.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == stack_item_id:
                return item[2] # The controller is the 3rd element
        return None

    def shuffle_library(self, player):
        """Shuffles the player's library."""
        if player and "library" in player:
            random.shuffle(player["library"])
            logging.debug(f"{player['name']}'s library shuffled.")
            return True
        return False
        
    def venture(self, player):
        """Handle venture into the dungeon. Needs dungeon tracking."""
        if not hasattr(self, 'dungeons'):
             logging.warning("Venture called but dungeon system not implemented.")
             return False
        # TODO: Implement dungeon choice and room progression logic
        logging.debug("Venture placeholder.")
        return True

    def get_permanent_by_combined_index(self, combined_index):
        """Get permanent ID and owner by a combined index across both battlefields (P1 first)."""
        p1_bf_len = len(self.p1.get("battlefield", [])) # Use get for safety
        if 0 <= combined_index < p1_bf_len:
            card_id = self.p1["battlefield"][combined_index]
            return card_id, self.p1
        p2_bf_len = len(self.p2.get("battlefield", []))
        if p1_bf_len <= combined_index < p1_bf_len + p2_bf_len:
            card_id = self.p2["battlefield"][combined_index - p1_bf_len]
            return card_id, self.p2
        logging.warning(f"Invalid combined battlefield index: {combined_index}")
        return None, None # Return None if index is out of bounds
    
    def get_token_data_by_index(self, index):
        """Returns predefined token data for CREATE_TOKEN action."""
        # Example mapping - needs to be defined based on game needs
        token_map = {
            0: {"name": "Soldier", "type_line": "Token Creature — Soldier", "power": 1, "toughness": 1, "colors":[1,0,0,0,0]},
            1: {"name": "Spirit", "type_line": "Token Creature — Spirit", "power": 1, "toughness": 1, "colors":[1,0,0,0,0], "keywords":[1,0,0,0,0,0,0,0,0,0,0]}, # Flying
            2: {"name": "Goblin", "type_line": "Token Creature — Goblin", "power": 1, "toughness": 1, "colors":[0,0,0,1,0]},
            3: {"name": "Treasure", "type_line": "Token Artifact — Treasure", "card_types":["artifact"], "subtypes":["Treasure"], "oracle_text": "{T}, Sacrifice this artifact: Add one mana of any color."},
            4: {"name": "Clue", "type_line": "Token Artifact — Clue", "card_types": ["artifact"], "subtypes":["Clue"], "oracle_text": "{2}, Sacrifice this artifact: Draw a card."}
        }
        return token_map.get(index)

    def create_token(self, controller, token_data):
        """
        Create a token and add it to the battlefield.
        
        Args:
            controller: The player who will control the token
            token_data: Dictionary with token specifications (name, types, p/t, etc.)
            
        Returns:
            str: Token ID if successful, None otherwise
        """
        try:
            # Create token tracking if it doesn't exist
            if not hasattr(controller, "tokens"):
                controller["tokens"] = []
                
            # Generate token ID
            token_count = len(controller["tokens"])
            token_id = f"TOKEN_{token_count}_{token_data.get('name', 'Generic').replace(' ', '_')}"
            
            # Set default values if not provided
            if "power" not in token_data:
                token_data["power"] = 1
            if "toughness" not in token_data:
                token_data["toughness"] = 1
            if "card_types" not in token_data:
                token_data["card_types"] = ["creature"]
            if "subtypes" not in token_data:
                token_data["subtypes"] = []
            if "oracle_text" not in token_data:
                token_data["oracle_text"] = ""
            if "keywords" not in token_data:
                token_data["keywords"] = [0] * 11
            if "colors" not in token_data:
                token_data["colors"] = [0, 0, 0, 0, 0]  # Colorless by default
            
            # Create token Card object
            token = Card(token_data)
            
            # Add token to the card database
            self.card_db[token_id] = token
            
            # Add token to battlefield
            controller["battlefield"].append(token_id)
            controller["tokens"].append(token_id)
            
            # Mark as entering this turn (summoning sickness)
            if 'creature' in token_data["card_types"]:
                controller["entered_battlefield_this_turn"].add(token_id)
            
            # Trigger enters-the-battlefield abilities
            self.trigger_ability(token_id, "ENTERS_BATTLEFIELD")
            
            logging.debug(f"Created token: {token_data.get('name', 'Generic Token')}")
            return token_id
            
        except Exception as e:
            logging.error(f"Error creating token: {str(e)}")
            return None

    def handle_cast_trigger(self, card_id, controller, context=None):
        """Handle triggers that occur when a spell is cast."""
        if not context:
            context = {}
            
        # Add card type info to context
        card = self._safe_get_card(card_id)
        if card and hasattr(card, 'card_types'):
            context["card_types"] = card.card_types
            
        # Check for cast triggers on all permanents in play
        for player in [self.p1, self.p2]:
            for permanent_id in player["battlefield"]:
                self.trigger_ability(permanent_id, "SPELL_CAST", context)
                
                # Specific triggers for instant/sorcery casts
                if card and hasattr(card, 'card_types'):
                    if 'instant' in card.card_types or 'sorcery' in card.card_types:
                        self.trigger_ability(permanent_id, "CAST_NONCREATURE_SPELL", context)
                    elif 'creature' in card.card_types:
                        self.trigger_ability(permanent_id, "CAST_CREATURE_SPELL", context)
                
        # Process specific ability triggers like Storm
        if card and hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Storm ability
            if "storm" in oracle_text:
                # Count spells cast this turn
                if not hasattr(self, 'spells_cast_this_turn'):
                    self.spells_cast_this_turn = []
                    
                storm_count = len(self.spells_cast_this_turn)
                
                # Create copies
                for _ in range(storm_count):
                    self.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
                    
                logging.debug(f"Storm triggered: Created {storm_count} copies of {card.name}")
    
    def initialize_targeting_system(self):
        """Initialize the targeting system."""
        try:
            from .ability_handler import TargetingSystem
            self.targeting_system = TargetingSystem(self)
            logging.debug("TargetingSystem initialized successfully")
        except ImportError as e:
            logging.warning(f"TargetingSystem not available: {e}")
            self.targeting_system = None
        except Exception as e:
            logging.error(f"Error initializing targeting system: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            self.targeting_system = None
        
    def put_on_top(self, player, card_idx):
        """
        Put a card from hand on top of library.
        
        Args:
            player: Player dictionary
            card_idx: Index of card in hand to put on top
            
        Returns:
            bool: Whether the operation was successful
        """
        if 0 <= card_idx < len(player["hand"]):
            card_id = player["hand"].pop(card_idx)
            player["library"].insert(0, card_id)
            
            card = self._safe_get_card(card_id)
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Put {card_name} on top of library")
            return True
        
        logging.warning(f"Invalid card index {card_idx} for put_on_top")
        return False
        
    def put_on_bottom(self, player, card_idx):
        """
        Put a card from hand on bottom of library.
        
        Args:
            player: Player dictionary
            card_idx: Index of card in hand to put on bottom
            
        Returns:
            bool: Whether the operation was successful
        """
        if 0 <= card_idx < len(player["hand"]):
            card_id = player["hand"].pop(card_idx)
            player["library"].append(card_id)
            
            card = self._safe_get_card(card_id)
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Put {card_name} on bottom of library")
            return True
            
        logging.warning(f"Invalid card index {card_idx} for put_on_bottom")
        return False
    
    def check_keyword(self, card_id, keyword):
        """
        Checks if a card has a specific keyword, prioritizing Layer System results.
        This is the central point for keyword checks within GameState.

        Args:
            card_id (str): The ID of the card to check.
            keyword (str): The keyword to check for (e.g., 'flying', 'haste').

        Returns:
            bool: True if the card currently has the keyword, False otherwise.
        """
        card = self._safe_get_card(card_id)
        if not card:
            logging.debug(f"check_keyword: Card {card_id} not found.")
            return False

        keyword_lower = keyword.lower()

        # 1. Prefer Layer System Results (on the live card object)
        # The LayerSystem updates the card object directly with the final 'keywords' array.
        if hasattr(card, 'keywords') and isinstance(card.keywords, (list, np.ndarray)):
            try:
                # Ensure Card.ALL_KEYWORDS is available and populated
                if not hasattr(Card, 'ALL_KEYWORDS') or not Card.ALL_KEYWORDS:
                     # Attempt to load if missing (e.g., if Card class wasn't fully initialized)
                     if hasattr(Card, '_load_keywords'):
                         Card._load_keywords()
                     if not hasattr(Card, 'ALL_KEYWORDS') or not Card.ALL_KEYWORDS:
                          logging.error("Card.ALL_KEYWORDS is missing or empty. Cannot perform keyword check.")
                          return False # Cannot check without the list

                # Use the static list from Card class for consistency
                kw_list = [k.lower() for k in Card.ALL_KEYWORDS]
                idx = kw_list.index(keyword_lower)
                if idx < len(card.keywords):
                    has_keyword = bool(card.keywords[idx])
                    # Logging can be very verbose, disable or make conditional
                    # logging.debug(f"check_keyword (Layer): '{keyword_lower}' on '{card.name}' -> {has_keyword}")
                    return has_keyword
                else:
                    # Index is valid for the keyword list, but out of bounds for *this card's* keyword array
                    # This implies an issue with array initialization or keyword list mismatch.
                    logging.warning(f"check_keyword (Layer): Keyword index {idx} out of bounds for {card.name}'s keyword array (Len: {len(card.keywords)})")
                    return False # Treat as not having the keyword if array is wrong size
            except ValueError:
                 # Keyword is not in the standard Card.ALL_KEYWORDS list
                 # Could be a temporary/pseudo keyword like 'cant_attack' added by layers.
                 # How LayerSystem handles these needs clarification. If it adds them directly
                 # as attributes or modifies the 'keywords' array needs to be consistent.
                 # For now, assume standard keywords are in the array. Non-standard = False.
                 logging.debug(f"check_keyword (Layer): Keyword '{keyword_lower}' not in Card.ALL_KEYWORDS list.")
                 # Check if it exists as a direct attribute (less likely for LayerSystem)
                 # return getattr(card, keyword_lower, False)
                 return False
            except IndexError:
                 # Should be caught by the length check above, but safety catch.
                 logging.warning(f"check_keyword (Layer): Unexpected IndexError for keyword {keyword_lower} on {card.name}.")
                 return False
            except Exception as e:
                 logging.error(f"check_keyword (Layer): Error checking keyword array for {card.name}: {e}")
                 return False # Error implies uncertainty, assume false

        # 2. Fallback (Less Reliable): Check base card text IF no layer system active/result found
        # This is unreliable because layers can grant/remove keywords. Use with caution.
        elif not self.layer_system:
             logging.warning(f"check_keyword: LayerSystem inactive, falling back to basic text check for '{keyword_lower}' on {card.name}. This may be inaccurate.")
             if hasattr(card, 'oracle_text'):
                 # Simple text check (can be fooled by reminder text or unrelated mentions)
                 # Add word boundaries for more accuracy on single-word keywords
                 pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                 return bool(re.search(pattern, card.oracle_text.lower()))

        # If no LayerSystem result and no fallback text check done, assume False
        logging.debug(f"check_keyword: Keyword '{keyword_lower}' not found or verifiable for {card.name}.")
        return False
        
    def reveal_top(self, player, count=1):
        """
        Reveal the top N cards of library without changing their order.
        
        Args:
            player: Player dictionary
            count: Number of cards to reveal
            
        Returns:
            list: The revealed card objects
        """
        if not player["library"]:
            logging.debug("Cannot reveal - library is empty")
            return []
            
        # Limit to number of cards in library
        count = min(count, len(player["library"]))
        revealed_cards = []
        
        # Get top cards without changing their order
        for i in range(count):
            card_id = player["library"][i]
            card = self._safe_get_card(card_id)
            revealed_cards.append(card)
            
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Revealed {card_name} from top of library")
            
        return revealed_cards
        
    def check_state_based_actions(self):
        """
        Comprehensive state-based actions check following MTG rules 704.
        Repeats check until no SBAs are performed in an iteration.
        Returns True if any SBA was performed, False otherwise.
        """
        initial_actions_performed = False
        iteration_count = 0
        max_iterations = 10 # Safety limit

        while iteration_count < max_iterations:
            iteration_count += 1
            current_actions_performed = False
            if iteration_count > 1: # Only log repeats
                logging.debug(f"--- SBA Check Iteration {iteration_count} ---")

            # --- Layer Application ---
            # Ensure characteristics are up-to-date before checking SBAs
            if self.layer_system:
                self.layer_system.apply_all_effects()

            # --- Collect Potential Actions ---
            # Store as (priority, action_type, target_id, player, details)
            # Priority helps group similar actions (e.g., handle all player losses first)
            actions_to_take = []

            # --- 1. Check Player States ---
            players_to_check = [p for p in [self.p1, self.p2] if p] # Filter out None players
            for player in players_to_check:
                player_id = 'p1' if player == self.p1 else 'p2'
                player_name = player.get('name', player_id)

                # 704.5a: Player Loses (Life <= 0)
                if player.get("life", 0) <= 0 and not player.get("lost_game", False) and not player.get("won_game", False): # Check win flag too
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "life <= 0"}))

                # 704.5b: Player Loses (Draw Empty)
                elif player.get("attempted_draw_from_empty", False) and not player.get("lost_game", False) and not player.get("won_game", False):
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "draw_empty"}))

                # 704.5c: Player Loses (Poison >= 10)
                elif player.get("poison_counters", 0) >= 10 and not player.get("lost_game", False) and not player.get("won_game", False):
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "poison >= 10"}))

            # Check Turn Limit Draw/Loss
            if self.turn > self.max_turns and not getattr(self, '_turn_limit_checked', False):
                if self.p1 and self.p2:
                    if self.p1.get("life",0) == self.p2.get("life",0) and not self.p1.get("won_game") and not self.p2.get("won_game") and not self.p1.get("lost_game") and not self.p2.get("lost_game"):
                        actions_to_take.append((1, "DRAW_GAME", "both", None, {"reason": "turn_limit_equal_life"}))
                    # Loss handled by 704.5a after life comparison, no direct SBA needed here
                    self._turn_limit_checked = True  # Set flag to avoid repeated checks

            # --- 2. Check Permanent States ---
            # Get all permanents on battlefield for efficient checking
            all_permanents = []
            for player in players_to_check:
                all_permanents.extend([(card_id, player) for card_id in list(player.get("battlefield", []))]) # Iterate copy

            # Keep track of multiple legendaries/planeswalkers
            legendary_groups = defaultdict(list)
            world_permanents = []

            for card_id, player in all_permanents:
                card = self._safe_get_card(card_id)
                if not card: continue

                # --- Get current characteristics post-layers ---
                # Safely get characteristics using Layer System if available, else fallback to card object
                def get_char(cid, char_name, default):
                    if self.layer_system: return self.layer_system.get_characteristic(cid, char_name) or default
                    else: return getattr(self._safe_get_card(cid), char_name, default)

                current_types = get_char(card_id, 'card_types', [])
                current_subtypes = get_char(card_id, 'subtypes', [])
                current_supertypes = get_char(card_id, 'supertypes', [])
                current_toughness = get_char(card_id, 'toughness', 0)
                # Get PW loyalty correctly (can be modified)
                current_loyalty = player.get("loyalty_counters", {}).get(card_id, 0)
                # Also check base loyalty for entry into the tracking dict
                if 'planeswalker' in current_types and card_id not in player.get("loyalty_counters",{}):
                    # If PW just entered, its loyalty should be initialized
                    base_loyalty = getattr(card, 'loyalty', 0) # Get base from card object
                    player.setdefault("loyalty_counters", {})[card_id] = base_loyalty
                    current_loyalty = base_loyalty

                damage = player.get("damage_counters", {}).get(card_id, 0)
                deathtouch_flag = player.get("deathtouch_damage", {}).get(card_id, False)
                # Keywords obtained from layers should be on the card object
                is_indestructible = self.check_keyword(card_id, "indestructible") if hasattr(self,'check_keyword') else ('indestructible' in getattr(card,'oracle_text','').lower())

                # 704.5f: Creature with toughness <= 0 dies
                if 'creature' in current_types and current_toughness <= 0:
                    # Indestructible doesn't save from toughness <= 0
                    actions_to_take.append((2, "MOVE_TO_GY", card_id, player, {"reason": "toughness <= 0"}))

                # 704.5i: Planeswalker with 0 loyalty dies
                elif 'planeswalker' in current_types and current_loyalty <= 0:
                    actions_to_take.append((2, "MOVE_TO_GY", card_id, player, {"reason": "loyalty <= 0"}))

                # 704.5g/h: Creature with lethal damage or deathtouch damage is destroyed
                elif 'creature' in current_types and current_toughness > 0:
                    # Check if damage is >= toughness OR any deathtouch damage marked
                    is_lethal = (damage >= current_toughness) or deathtouch_flag
                    if is_lethal:
                        if not is_indestructible:
                            # Flag for potential destruction, replacements handled during application
                            actions_to_take.append((3, "CHECK_DESTROY", card_id, player, {"reason": "lethal_damage/deathtouch"}))
                        else:
                            # If indestructible but has lethal damage, remove the damage (Rule 704.5g implicitly requires this if destroy is skipped)
                            if damage > 0 and card_id in player.get("damage_counters",{}):
                                logging.debug(f"Removing lethal damage from indestructible creature {card.name}")
                                player["damage_counters"][card_id] = 0
                                # Clear deathtouch flag too if it triggered this
                                if card_id in player.get("deathtouch_damage",{}):
                                    del player["deathtouch_damage"][card_id]
                                # Need to mark action performed to trigger potential loop check/layer update
                                current_actions_performed = True

                # 704.5j: If an Aura is attached to an illegal object or player, or is not attached to an object or player, send to GY
                if 'aura' in current_subtypes:
                    attached_to = player.get("attachments", {}).get(card_id)
                    # Check if not attached OR if the target is illegal (incl. protection)
                    if attached_to is None or not self._is_legal_attachment(card_id, attached_to):
                        actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "aura_illegal_attachment"}))
                    
                # 704.5k: If an Equipment or Fortification is attached to an illegal permanent or player, it becomes unattached
                elif 'equipment' in current_subtypes or 'fortification' in current_subtypes:
                    attached_to = player.get("attachments", {}).get(card_id)
                    # Check if attached AND if the target is illegal
                    if attached_to and not self._is_legal_attachment(card_id, attached_to):
                        actions_to_take.append((4, "UNEQUIP", card_id, player, {"reason": "equip_illegal_attachment"}))

                # 704.5l: Legend Rule
                if 'legendary' in current_supertypes:
                    name = getattr(card, 'name', None)
                    if name: legendary_groups[name].append((card_id, player))

                # 704.5m: World Rule
                if 'world' in current_supertypes:
                    world_permanents.append((card_id, player))

                # 704.5p/q: +1/+1 vs -1/-1 Annihilation
                if hasattr(card, 'counters') and card.counters.get('+1/+1', 0) > 0 and card.counters.get('-1/-1', 0) > 0:
                    actions_to_take.append((5, "ANNIHILATE_COUNTERS", card_id, player, {}))

                # 704.5s: Battle with no defense counters is put into its owner's graveyard
                if 'battle' in getattr(card, 'type_line', '').lower() and getattr(self, 'battle_cards', {}).get(card_id, 0) <= 0:
                    actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "battle_no_defense"}))

                # 704.5u: Saga with final chapter completed
                if 'saga' in current_subtypes and player.get("saga_counters", {}).get(card_id, 0) > 0:
                    chapter_count = 0
                    if hasattr(card, 'oracle_text'):
                        chapter_pattern = re.compile(r"(^|\n)([IVX]+) —", re.MULTILINE)
                        chapter_matches = chapter_pattern.findall(card.oracle_text)
                        chapter_count = len(chapter_matches)
                    
                    if chapter_count > 0 and player.get("saga_counters", {}).get(card_id, 0) > chapter_count:
                        actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "saga_completed"}))

                # 704.5v: Permanent with phased-out status phased in since player's most recent turn began
                if card_id in getattr(self, 'phased_out', set()) and hasattr(player, 'phased_out_since_turn'):
                    if player.get('phased_out_since_turn', {}).get(card_id, 0) < self.turn:
                        actions_to_take.append((4, "PHASE_IN", card_id, player, {}))

                # 704.5w: Day/night state check if the permanent has a day/night transformation
                # This would typically be handled by a separate day/night mechanic function

            # --- Consolidate Legend Rule Checks ---
            # 704.5j: Legend Rule
            for name, permanents in legendary_groups.items():
                if len(permanents) > 1:
                    # Group by controller first (legends with same name are processed per-player)
                    by_controller = defaultdict(list)
                    for card_id, player in permanents:
                        by_controller[player].append(card_id)
                    
                    for player, legends in by_controller.items():
                        if len(legends) > 1:
                            # Owner chooses which one to keep - keep only the newest one (implementation choice)
                            to_keep = legends[-1]
                            for legend_id in legends[:-1]:
                                actions_to_take.append((4, "MOVE_TO_GY", legend_id, player, {"reason": "legend_rule"}))

            # --- Consolidate World Rule Check ---
            # 704.5m: World Rule
            if len(world_permanents) > 1:
                # Determine newest (using card_id as proxy timestamp)
                world_permanents.sort(key=lambda x: getattr(self._safe_get_card(x[0]),'_timestamp',x[0]))
                newest_id, newest_controller = world_permanents[-1]
                for world_id, world_player in world_permanents[:-1]:
                    actions_to_take.append((4, "MOVE_TO_GY", world_id, world_player, {"reason": "world_rule"}))

            # --- 3. Check for * in Power/Toughness without defining ability --- 
            # 704.5r: If creature has * in power/toughness and no ability defines it, set to 0
            for card_id, player in all_permanents:
                card = self._safe_get_card(card_id)
                if card and 'creature' in getattr(card, 'card_types', []):
                    # Check if power or toughness contains * and needs defining ability
                    power_str = str(getattr(card, 'power', '0'))
                    toughness_str = str(getattr(card, 'toughness', '0'))
                    
                    if ('*' in power_str or '*' in toughness_str) and not hasattr(card, '_characteristic_defining_abilities'):
                        # Set undefined * power/toughness to 0
                        if '*' in power_str: card.power = 0
                        if '*' in toughness_str: card.toughness = 0
                        current_actions_performed = True
                        logging.debug(f"SBA: Set undefined */* values to 0 for {card.name}")

            # --- 4. Token existence checks and copy existence checks ---
            # Check for tokens in non-battlefield zones (handled separately for clarity)
            tokens_ceased = self._check_and_remove_invalid_tokens()
            if tokens_ceased: 
                current_actions_performed = True

            # 704.5e: If a copy of a spell is in a zone other than the stack, it ceases to exist
            # 704.5d: If a token is in a zone other than the battlefield, it ceases to exist
            # These are best handled in the _check_and_remove_invalid_tokens method

            # --- 5. Apply Actions Simultaneously (Grouped by Type/Priority) ---
            # Process actions in priority order
            actions_to_take.sort(key=lambda x: x[0]) # Sort by priority
            processed_in_iteration = set()  # Track processed actions

            for priority, action_type, target, player_ref, details in actions_to_take:
                action_key = (action_type, target) # Unique key for this SBA application
                if action_key in processed_in_iteration: continue

                target_id = target if isinstance(target, str) else None # Extract ID if not a complex target
                target_card = self._safe_get_card(target_id) if target_id else None
                target_name = getattr(target_card, 'name', target_id) if target_card else str(target)

                logging.debug(f"SBA Checking: {action_type} on {target_name} for {player_ref['name'] if player_ref else 'Game'}")

                performed_this_action = False
                if action_type == "LOSE_GAME":
                    if not player_ref.get("lost_game", False):
                        player_ref["lost_game"] = True
                        logging.info(f"SBA Applied: {player_ref['name']} loses ({details['reason']})")
                        performed_this_action = True
                        current_actions_performed = True
                        
                elif action_type == "DRAW_GAME":
                    if not (self.p1 and self.p1.get("game_draw",False)) and not (self.p2 and self.p2.get("game_draw",False)):
                        if self.p1: self.p1["game_draw"] = True
                        if self.p2: self.p2["game_draw"] = True
                        logging.info(f"SBA Applied: Game draw ({details['reason']})")
                        performed_this_action = True
                        current_actions_performed = True

                elif action_type == "CHECK_DESTROY": # Lethal damage check
                    # Check replacements before moving to graveyard
                    destruction_replaced = False
                    replacement_details = None

                    # 1. Regeneration
                    if hasattr(self, 'apply_regeneration') and self.apply_regeneration(target_id, player_ref):
                        logging.info(f"SBA: {target_name} regenerated instead of being destroyed.")
                        destruction_replaced = True
                        replacement_details = "regenerated"
                        performed_this_action = True
                        
                    # 2. Totem Armor
                    elif not destruction_replaced and hasattr(self, 'apply_totem_armor') and self.apply_totem_armor(target_id, player_ref):
                        logging.info(f"SBA: Totem Armor saved {target_name} from destruction.")
                        destruction_replaced = True
                        replacement_details = "totem_armor"
                        performed_this_action = True
                        
                    # 3. Other "If X would be destroyed" replacements
                    elif not destruction_replaced and self.replacement_effects:
                        destroy_context = {'card_id': target_id, 'player': player_ref, 'cause': 'sba_damage', 'from_zone': 'battlefield'}
                        modified_context, replaced = self.replacement_effects.apply_replacements("DESTROYED", destroy_context)
                        if replaced:
                            destruction_replaced = True
                            replacement_details = modified_context.get('description', 'replaced')
                            logging.info(f"SBA: Destruction of {target_name} replaced ({replacement_details}).")
                            # Handle modified destination (e.g., exile)
                            final_dest = modified_context.get('to_zone')
                            if final_dest and final_dest != "battlefield":
                                if self.move_card(target_id, player_ref, "battlefield", player_ref, final_dest, cause="sba_replaced_destroy"):
                                    performed_this_action = True
                            elif modified_context.get('prevented'):
                                performed_this_action = True  # Action was "prevented" but still processed

                    # 4. If not replaced/prevented, perform move to GY
                    if not destruction_replaced:
                        if self.move_card(target_id, player_ref, "battlefield", player_ref, "graveyard", cause="sba_damage", context=details):
                            logging.info(f"SBA Applied: Moved {target_name} to graveyard (Lethal Damage)")
                            performed_this_action = True

                elif action_type == "MOVE_TO_GY": # Toughness, Loyalty, Aura, World Rule etc.
                    if self.move_card(target_id, player_ref, "battlefield", player_ref, "graveyard", cause="sba", context=details):
                        logging.info(f"SBA Applied: Moved {target_name} to graveyard ({details['reason']})")
                        performed_this_action = True

                elif action_type == "UNEQUIP":
                    if hasattr(self, 'unequip_permanent') and self.unequip_permanent(player_ref, target_id):
                        logging.info(f"SBA Applied: Unequipped {target_name} ({details['reason']})")
                        performed_this_action = True
                        
                elif action_type == "PHASE_IN":
                    if hasattr(self, 'phase_in_permanent') and self.phase_in_permanent(target_id, player_ref):
                        logging.info(f"SBA Applied: Phased in {target_name}")
                        performed_this_action = True
                    else:
                        # Simple fallback if phase_in_permanent doesn't exist
                        if hasattr(self, 'phased_out') and target_id in self.phased_out:
                            self.phased_out.remove(target_id)
                            if target_id not in player_ref.get("battlefield", []):
                                player_ref["battlefield"].append(target_id)
                            logging.info(f"SBA Applied: Phased in {target_name} (Fallback method)")
                            performed_this_action = True

                elif action_type == "ANNIHILATE_COUNTERS":
                    if target_card and hasattr(target_card, 'counters'):
                        plus_count = target_card.counters.get('+1/+1', 0)
                        minus_count = target_card.counters.get('-1/-1', 0)
                        remove_amount = min(plus_count, minus_count)
                        if remove_amount > 0:
                            # Use add_counter for consistency and triggers
                            if hasattr(self, 'add_counter'):
                                self.add_counter(target_id, '+1/+1', -remove_amount)
                                self.add_counter(target_id, '-1/-1', -remove_amount)
                            else:
                                # Fallback direct modification
                                target_card.counters['+1/+1'] -= remove_amount
                                if target_card.counters['+1/+1'] <= 0: 
                                    del target_card.counters['+1/+1']
                                target_card.counters['-1/-1'] -= remove_amount
                                if target_card.counters['-1/-1'] <= 0: 
                                    del target_card.counters['-1/-1']
                            
                            logging.info(f"SBA Applied: Annihilated {remove_amount} +/- counters on {target_name}")
                            performed_this_action = True

                # Mark as processed and update state
                processed_in_iteration.add(action_key)
                current_actions_performed = current_actions_performed or performed_this_action

            # --- End of Inner Action Loop ---

            # --- Update overall flag and break if stable ---
            initial_actions_performed = initial_actions_performed or current_actions_performed
            if not current_actions_performed:
                if iteration_count > 1: # Log stability only if it took more than one pass
                    logging.debug(f"--- SBA Check Stable after {iteration_count} iterations ---")
                break # Exit the while loop if no actions were performed this iteration

            # If game ended during this iteration, stop checking SBAs
            if any(p.get("lost_game") or p.get("won_game") or p.get("game_draw") for p in players_to_check if p):
                logging.debug("--- SBA Check: Game ended, stopping SBA loop ---")
                break

        if iteration_count >= max_iterations:
            logging.error("State-based actions check exceeded max iterations. Potential infinite loop.")

        # --- Final Layer Re-application ---
        if initial_actions_performed and self.layer_system:
            logging.debug("Re-applying layers after SBAs.")
            self.layer_system.apply_all_effects()

        return initial_actions_performed

    def _is_legal_attachment(self, attach_id, target_id):
        """Check if an Aura/Equipment/Fortification can legally be attached to the target."""
        attachment = self._safe_get_card(attach_id)
        target = self._safe_get_card(target_id)
        if not attachment or not target: return False

        _, target_zone = self.find_card_location(target_id)
        if target_zone != 'battlefield': return False

        # Check "enchant X", "equip X", "fortify X" restrictions
        attach_text = getattr(attachment, 'oracle_text', '').lower()
        target_types = getattr(target, 'card_types', [])
        target_subtypes = getattr(target, 'subtypes', [])

        if 'aura' in getattr(attachment, 'subtypes', []):
            if 'enchant creature' in attach_text and 'creature' not in target_types: return False
            if 'enchant artifact' in attach_text and 'artifact' not in target_types: return False
            if 'enchant land' in attach_text and 'land' not in target_types: return False
            if 'enchant permanent' in attach_text: pass # Always legal if target is permanent
            # Add more specific enchant checks (e.g., "enchant artifact or creature")
            # Regex might be needed: re.search(r"enchant ([\w\s]+)", attach_text)
        elif 'equipment' in getattr(attachment, 'subtypes', []):
            if 'creature' not in target_types: return False
        elif 'fortification' in getattr(attachment, 'subtypes', []):
            if 'land' not in target_types: return False

        # Check Protection
        if self.targeting_system and hasattr(self.targeting_system, '_has_protection_from'):
            # Need controllers. Assume attachment controlled by player who owns attachment dict.
            attach_player = self.get_card_controller(attach_id)
            target_player = self.get_card_controller(target_id)
            # Aura/Equip targets the permanent it's attached to
            if self.targeting_system._has_protection_from(target, attachment, target_player, attach_player):
                 return False

        return True # Assume legal if no specific restriction failed

    def _check_and_remove_invalid_tokens(self):
        """Check all zones for tokens that shouldn't exist there and remove them."""
        removed_token = False
        for player in [self.p1, self.p2]:
            if not player: continue
            tokens_in_non_bf_zones = []
            # Check zones other than battlefield
            for zone in ["hand", "graveyard", "exile", "library", "stack_implicit"]: # Check stack too implicitly
                zone_content = player.get(zone)
                if zone_content and isinstance(zone_content, (list, set)):
                    # Iterate over copy for removal
                    for card_id in list(zone_content):
                        card = self._safe_get_card(card_id)
                        if card and hasattr(card, 'is_token') and card.is_token:
                             tokens_in_non_bf_zones.append((card_id, zone))

            # Check stack explicitly
            for item in self.stack:
                if isinstance(item, tuple) and len(item)>1:
                     item_id = item[1]
                     item_card = self._safe_get_card(item_id)
                     if item_card and hasattr(item_card, 'is_token') and item_card.is_token:
                          tokens_in_non_bf_zones.append((item_id, "stack"))

            # Remove found tokens
            for card_id, zone_name in tokens_in_non_bf_zones:
                 # Remove from card_db
                 if card_id in self.card_db:
                      del self.card_db[card_id]
                      logging.debug(f"SBA: Token {card_id} ceased to exist in {zone_name}.")
                      removed_token = True
                 # Remove from player zone / stack
                 if zone_name == "stack":
                      self.stack = [item for item in self.stack if not (isinstance(item, tuple) and item[1] == card_id)]
                 elif zone_name != "stack_implicit" and zone_name in player and isinstance(player[zone_name],(list,set)) and card_id in player[zone_name]:
                      if isinstance(player[zone_name], list): player[zone_name].remove(card_id)
                      elif isinstance(player[zone_name], set): player[zone_name].discard(card_id)

        return removed_token
    
    def apply_regeneration(self, card_id, player):
        """Applies a regeneration shield if available, preventing destruction."""
        if card_id in player.get("regeneration_shields", set()):
            card = self._safe_get_card(card_id)
            # Verify card still exists and is on battlefield (might have been removed by other SBAs)
            current_controller, current_zone = self.find_card_location(card_id)
            if card and current_controller == player and current_zone == "battlefield":
                player["regeneration_shields"].remove(card_id)
                self.tap_permanent(card_id, player) # Tap the creature
                # Remove damage marked on creature
                if 'damage_counters' in player: player['damage_counters'].pop(card_id, None)
                if 'deathtouch_damage' in player: player.get('deathtouch_damage', {}).pop(card_id, None) # Clear deathtouch mark

                # Also remove from combat if attacking/blocking (Rule 614.8)
                if card_id in self.current_attackers: self.current_attackers.remove(card_id)
                for attacker_id, blockers in list(self.current_block_assignments.items()):
                    if card_id in blockers: blockers.remove(card_id)
                    if not blockers: del self.current_block_assignments[attacker_id] # Clean up if no blockers left

                logging.debug(f"Regeneration shield used for {card.name}. Creature tapped and removed from combat.")
                return True
            else:
                 # Shield exists but creature is gone or no longer controlled by player, remove stale shield
                 player.get("regeneration_shields", set()).discard(card_id)
                 logging.debug(f"Stale regeneration shield removed for {card_id}")

        return False

    def apply_totem_armor(self, card_id, player):
        """Applies totem armor if available, destroying the Aura instead."""
        totem_aura_id = None
        for aura_id in list(player.get("battlefield", [])): # Check player's battlefield for auras attached to the creature
            aura = self._safe_get_card(aura_id)
            if not aura: continue
            is_aura_with_totem = ('aura' in getattr(aura, 'subtypes', [])) and ("totem armor" in getattr(aura, 'oracle_text', '').lower())

            # Check if this aura is attached to the creature being destroyed
            if is_aura_with_totem and player.get("attachments", {}).get(aura_id) == card_id:
                totem_aura_id = aura_id
                break # Found one, apply it

        if totem_aura_id:
            aura_to_destroy = self._safe_get_card(totem_aura_id)
            creature_saved = self._safe_get_card(card_id)
            logging.debug(f"Totem armor: Destroying {getattr(aura_to_destroy,'name','Aura')} instead of {getattr(creature_saved,'name','Creature')}.")
            # Destroy the aura
            if self.move_card(totem_aura_id, player, "battlefield", player, "graveyard", cause="totem_armor"):
                 # Remove damage marked on the creature if destruction is prevented
                 if 'damage_counters' in player: player['damage_counters'].pop(card_id, None)
                 if 'deathtouch_damage' in player: player.get('deathtouch_damage', {}).pop(card_id, None) # Clear deathtouch mark
                 # Don't tap or remove from combat for totem armor
                 return True
            else:
                 logging.error(f"Failed to destroy totem armor aura {totem_aura_id}")
        return False
    
    def proliferate(self, player, targets="all"):
        """Apply proliferate effect."""
        proliferated_something = False
        valid_targets = []

        # Gather all players and permanents with counters
        for p in [self.p1, self.p2]:
            if p: # Check player exists
                if p.get("poison_counters", 0) > 0 or p.get("experience_counters", 0) > 0 or p.get("energy_counters", 0) > 0:
                     valid_targets.append(p)
                for card_id in p.get("battlefield", []):
                    card = self._safe_get_card(card_id)
                    # Include permanents (including PWs) with any type of counter
                    if card and hasattr(card, 'counters') and card.counters:
                         valid_targets.append(card_id)
                    # Include planeswalkers specifically for loyalty if not in card.counters yet
                    elif card and 'planeswalker' in getattr(card,'card_types',[]) and p.get('loyalty_counters',{}).get(card_id, 0) > 0:
                         valid_targets.append(card_id) # Add PW id if it has loyalty

        # Determine which targets to proliferate based on player choice
        # For AI, need a selection mechanism. Simple: Proliferate all valid targets.
        targets_to_proliferate = valid_targets # Simple: affect all valid

        if not targets_to_proliferate:
            logging.debug("Proliferate: No valid targets with counters found.")
            return False

        # --- AI Choice ---
        # More complex AI would choose which subset of valid_targets to affect.
        # Simple: proliferate all possible targets chosen by the player activating proliferate
        chosen_targets = [t for t in targets_to_proliferate if t == player or (isinstance(t, str) and self.get_card_controller(t) == player)]
        # If the effect specified 'opponent' or 'target', selection would differ.
        # Assuming "You choose..." - AI chooses based on strategy (e.g., buff self, poison opponent)
        # Simplification: affect everything controlled by the player + opponent players
        chosen_targets = []
        for target in valid_targets:
            if target == player: # Target self (player counters)
                chosen_targets.append(target)
            elif target == self._get_non_active_player() and target != player: # Target opponent (player counters)
                chosen_targets.append(target)
            elif isinstance(target, str): # Is a permanent ID
                card = self._safe_get_card(target)
                target_controller = self.get_card_controller(target)
                # Simple heuristic: proliferate own good counters, opponent's bad counters
                is_good_counter = any(ct in card.counters for ct in ["+1/+1", "lore", "loyalty"]) if hasattr(card, 'counters') else False
                is_bad_counter = any(ct in card.counters for ct in ["-1/-1", "poison"]) if hasattr(card, 'counters') else False
                if target_controller == player and is_good_counter: chosen_targets.append(target)
                if target_controller != player and is_bad_counter: chosen_targets.append(target)
                if target_controller != player and 'planeswalker' in getattr(card,'card_types',[]): chosen_targets.append(target) # Proliferate loyalty removal? Seems bad. Skip.

        # Fallback if heuristic finds nothing: proliferate own first valid target with counters.
        if not chosen_targets:
             for target in valid_targets:
                 if isinstance(target, str) and self.get_card_controller(target) == player:
                     chosen_targets.append(target); break

        logging.debug(f"Proliferate choosing targets: {chosen_targets}")


        # --- Apply Proliferation ---
        for target in chosen_targets:
            if isinstance(target, dict) and target in [self.p1, self.p2]: # Player target
                player_to_affect = target
                added_counter = False
                # Choose ONE type of counter the player has to increment
                counters_present = []
                if player_to_affect.get("poison_counters", 0) > 0: counters_present.append("poison")
                if player_to_affect.get("experience_counters", 0) > 0: counters_present.append("experience")
                if player_to_affect.get("energy_counters", 0) > 0: counters_present.append("energy")
                # AI Choice needed here which counter type to choose if multiple exist. Simple: First found.
                if counters_present:
                    chosen_counter_type = counters_present[0]
                    if chosen_counter_type == "poison": player_to_affect["poison_counters"] += 1; added_counter=True
                    elif chosen_counter_type == "experience": player_to_affect["experience_counters"] += 1; added_counter=True
                    elif chosen_counter_type == "energy": player_to_affect["energy_counters"] += 1; added_counter=True

                if added_counter:
                    logging.debug(f"Proliferated {chosen_counter_type} counter on player {player_to_affect['name']}")
                    proliferated_something = True

            elif isinstance(target, str): # Permanent card_id
                card = self._safe_get_card(target)
                target_controller = self.get_card_controller(target)
                if not card or not target_controller: continue

                # Choose ONE kind of counter already on the permanent to add another of.
                counters_present = []
                if hasattr(card, 'counters') and card.counters:
                    counters_present.extend(list(card.counters.keys()))
                # Check loyalty counters separately
                if 'planeswalker' in getattr(card,'card_types',[]) and target_controller.get('loyalty_counters',{}).get(target, 0) > 0:
                    counters_present.append('loyalty')

                if counters_present:
                    # AI Choice needed here which counter type to choose. Simple: First found.
                    chosen_counter_type = counters_present[0]
                    if chosen_counter_type == 'loyalty':
                        # Need method to add loyalty counter
                        current_loyalty = target_controller.get("loyalty_counters", {}).get(target, 0)
                        target_controller.setdefault("loyalty_counters", {})[target] = current_loyalty + 1
                        logging.debug(f"Proliferated loyalty counter on {card.name}")
                        proliferated_something = True
                    else:
                        # Use existing add_counter method
                        if self.add_counter(target, chosen_counter_type, 1):
                             # Logging handled by add_counter
                             proliferated_something = True


        # Check SBAs after proliferation might change things (e.g., PW death, -1/-1 kill)
        if proliferated_something: self.check_state_based_actions()
        return proliferated_something

    def mutate(self, player, mutating_card_id, target_id):
        """Handle the mutate mechanic."""
        target_card = self._safe_get_card(target_id)
        mutating_card = self._safe_get_card(mutating_card_id)
        if not target_card or not mutating_card: return False

        # Validation (non-human creature target)
        if 'creature' not in getattr(target_card, 'card_types', []) or 'Human' in getattr(target_card, 'subtypes', []):
             logging.warning(f"Mutate failed: Target {target_card.name} is not a non-Human creature.")
             return False

        # Decide top/bottom (AI choice, default: new card on top)
        mutate_on_top = True

        # Apply mutation based on top/bottom choice
        merged_card = None
        current_mutation_stack = getattr(player,"mutation_stacks", {}).get(target_id, [target_id])

        if mutate_on_top:
            merged_card = mutating_card # Top card defines name, types, P/T
            # Combine abilities/text (simplistic append)
            merged_card.oracle_text = getattr(target_card, 'oracle_text','') + "\n" + getattr(mutating_card, 'oracle_text','')
            # Keep counters, auras, equipment from target_card
            merged_card.counters = target_card.counters.copy() if hasattr(target_card, 'counters') else {}
            # Update the representation in card_db? Or just modify the live object? Modify live for now.
            target_card.name = merged_card.name
            target_card.power = merged_card.power
            target_card.toughness = merged_card.toughness
            target_card.card_types = merged_card.card_types
            target_card.subtypes = merged_card.subtypes
            target_card.oracle_text = merged_card.oracle_text
            # Keep target_card.counters
            current_mutation_stack.insert(0, mutating_card_id) # New card on top of stack list
        else: # Mutate under
            merged_card = target_card # Target defines name, types, P/T
            merged_card.oracle_text = getattr(target_card, 'oracle_text','') + "\n" + getattr(mutating_card, 'oracle_text','')
            # Keep target_card.counters
            current_mutation_stack.append(mutating_card_id) # New card at bottom of stack list

        # Track the mutation stack
        if not hasattr(player, "mutation_stacks"): player["mutation_stacks"] = {}
        player["mutation_stacks"][target_id] = current_mutation_stack

        # Mutating card leaves original zone (usually hand) implicitly handled by cast_spell
        # If cast from elsewhere, that needs handling too.

        # Trigger mutate ability (triggers for EACH card in the stack now)
        for card_in_stack_id in current_mutation_stack:
            self.trigger_ability(card_in_stack_id, "MUTATES", {"target_id": target_id, "top_card_id": current_mutation_stack[0]})

        logging.debug(f"{mutating_card.name} mutated {'onto' if mutate_on_top else 'under'} {target_card.name}. Result is now {merged_card.name}.")
        if self.layer_system: self.layer_system.apply_all_effects() # Re-apply layers
        return True

    def reanimate(self, player, gy_index):
        """Reanimate a permanent from graveyard."""
        if gy_index < len(player["graveyard"]):
            card_id = player["graveyard"][gy_index]
            card = self._safe_get_card(card_id)
            if card and any(t in getattr(card, 'card_types', []) for t in ["creature", "artifact", "enchantment", "planeswalker"]):
                return self.move_card(card_id, player, "graveyard", player, "battlefield")
        return False

    def flip_card(self, card_id):
        """Handle flipping a flip card. Assumes card object has flip logic."""
        card = self._safe_get_card(card_id)
        player = self.get_card_controller(card_id)
        if card and player and hasattr(card, 'flip'): # Assume a method exists
            if card.flip(): # Assume flip() returns True on success
                logging.debug(f"Flipped {card.name}")
                self.trigger_ability(card_id, "FLIPPED", {"controller": player})
                if self.layer_system: self.layer_system.apply_all_effects()
                return True
        return False


    def equip_permanent(self, player, equip_id, target_id):
        """Attach equipment, potentially replacing existing attachment."""
        equip_card = self._safe_get_card(equip_id)
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)

        # Basic validation
        if not equip_card or 'equipment' not in getattr(equip_card, 'subtypes', []) or \
           not target_card or 'creature' not in getattr(target_card, 'card_types', []) or \
           target_owner != player: # Can only equip to own creatures normally (Rule 301.5)
            logging.warning(f"Invalid equip: Eq:{equip_id} to Tgt:{target_id}. Target controller: {target_owner['name'] if target_owner else 'None'}")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        # Remove previous attachment of this equipment, if any
        if equip_id in player["attachments"]:
            logging.debug(f"Unequipping {equip_card.name} from previous target {player['attachments'][equip_id]}")
            del player["attachments"][equip_id]
        # Attach to new target
        player["attachments"][equip_id] = target_id
        logging.debug(f"Equipped {equip_card.name} to {target_card.name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(equip_id, "EQUIPPED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_EQUIPPED", {"equipment_id": equip_id})
        return True

    def unequip_permanent(self, player, equip_id):
        """Unequip an equipment."""
        if hasattr(player, "attachments") and equip_id in player["attachments"]:
            equip_name = getattr(self._safe_get_card(equip_id), 'name', equip_id)
            target_id = player["attachments"].pop(equip_id)
            logging.debug(f"Unequipped {equip_name} from {target_id}")
            if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
            # Trigger unequipped events? (Less common than equip)
            return True
        logging.debug(f"Cannot unequip {equip_id}: Not attached.")
        return False # Wasn't attached

    def attach_aura(self, player, aura_id, target_id):
        """Attach an aura. Assumes validation (legal target) happened before."""
        if not hasattr(player, "attachments"): player["attachments"] = {}
        aura_card = self._safe_get_card(aura_id)
        target_card = self._safe_get_card(target_id)
        aura_name = getattr(aura_card, 'name', aura_id)
        target_name = getattr(target_card, 'name', target_id)

        # Find target's actual location/controller for validation
        target_owner, target_zone = self.find_card_location(target_id)
        if target_zone != "battlefield":
             logging.warning(f"Cannot attach {aura_name}: Target {target_name} not on battlefield.")
             return False
        # TODO: Add "enchant <type>" validation and protection checks from TargetingSystem here if not done externally.

        if aura_id in player["attachments"]:
            logging.debug(f"Re-attaching {aura_name} from {player['attachments'][aura_id]} to {target_name}")
            del player["attachments"][aura_id]

        player["attachments"][aura_id] = target_id
        logging.debug(f"Attached {aura_name} to {target_name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(aura_id, "ATTACHED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_ENCHANTED", {"aura_id": aura_id})
        return True

    def fortify_land(self, player, fort_id, target_id):
        """Attach a fortification to a land."""
        fort_card = self._safe_get_card(fort_id)
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)

        # Validation
        if not fort_card or 'fortification' not in getattr(fort_card, 'subtypes', []) or \
           not target_card or 'land' not in getattr(target_card, 'card_types', []) or \
           target_owner != player: # Fortify requires controlling the land (Rule 301.6)
            logging.warning(f"Invalid fortify: Fort:{fort_id} to Land:{target_id}. Target controller: {target_owner['name'] if target_owner else 'None'}")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        if fort_id in player["attachments"]:
            logging.debug(f"Unequipping {fort_card.name} from previous land {player['attachments'][fort_id]}")
            del player["attachments"][fort_id]
        player["attachments"][fort_id] = target_id
        logging.debug(f"Fortified {target_card.name} with {fort_card.name}")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(fort_id, "FORTIFIED", {"target_id": target_id})
        self.trigger_ability(target_id, "BECAME_FORTIFIED", {"fortification_id": fort_id})
        return True

    def reconfigure_permanent(self, player, card_id):
        """Handle reconfigure. Assumes cost is paid."""
        card = self._safe_get_card(card_id)
        if not card or card_id not in player["battlefield"] or 'reconfigure' not in getattr(card, 'oracle_text', '').lower():
            logging.warning(f"Invalid reconfigure: Card {card_id} not found, not owned, or doesn't have reconfigure.")
            return False

        if not hasattr(player, "attachments"): player["attachments"] = {}
        is_attached = card_id in player["attachments"]
        # Must be a creature OR equipment to reconfigure
        can_reconfigure = 'creature' in getattr(card, 'card_types',[]) or 'equipment' in getattr(card, 'subtypes',[])

        if not can_reconfigure:
            logging.warning(f"Cannot reconfigure {card.name}, not a creature or equipment currently.")
            return False

        if is_attached: # Unattach: Becomes creature, loses equipment type
            target_id = player["attachments"].pop(card_id)
            if 'equipment' in getattr(card,'subtypes',[]): card.subtypes.remove('equipment')
            if 'creature' not in getattr(card, 'card_types',[]): card.card_types.append('creature')
            logging.debug(f"Reconfigured {card.name} to unattach from {self._safe_get_card(target_id).name}. It's now a creature.")
        else: # Attach: Becomes equipment, loses creature type
             # AI Choice needed for target. Simple: first valid owned creature.
             target_id = None
             for cid in player["battlefield"]:
                  if cid == card_id: continue
                  c = self._safe_get_card(cid)
                  if c and 'creature' in getattr(c, 'card_types', []):
                       target_id = cid; break
             if target_id:
                  player["attachments"][card_id] = target_id
                  if 'creature' in card.card_types: card.card_types.remove('creature')
                  if 'equipment' not in getattr(card, 'subtypes',[]): card.subtypes.append('equipment')
                  logging.debug(f"Reconfigured {card.name} to attach to {self._safe_get_card(target_id).name}. It's now an Equipment.")
             else:
                  logging.warning(f"Reconfigure failed for {card.name}: No valid target creature found.")
                  return False # No target

        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects()
        self.trigger_ability(card_id, "RECONFIGURED")
        return True

    def turn_face_up(self, player, card_id, pay_morph_cost=False, pay_manifest_cost=False):
        """Turn a face-down Morph or Manifest card face up."""
        card = self._safe_get_card(card_id)
        if not card or card_id not in player["battlefield"]: return False

        is_face_down = False
        original_info = None
        cost_to_pay_str = None
        source_mechanic = None

        # Check if manifested
        manifest_info = getattr(self, 'manifested_cards', {}).get(card_id)
        if manifest_info and manifest_info.get('face_down', True):
            is_face_down = True
            source_mechanic = "Manifest"
            if pay_manifest_cost:
                 original_info = manifest_info.get('original')
                 if original_info and 'creature' in original_info.get('card_types', []): # Only creatures can be turned up via manifest cost
                     cost_to_pay_str = original_info.get('mana_cost')
                 else:
                     logging.debug(f"Cannot turn up non-creature manifest {card_id} via cost.")
                     return False # Cannot turn non-creature manifest up this way

        # Check if morphed (if not already identified as manifest)
        morph_info = getattr(self, 'morphed_cards', {}).get(card_id)
        if not is_face_down and morph_info and morph_info.get('face_down', True):
             is_face_down = True
             source_mechanic = "Morph"
             if pay_morph_cost:
                  original_info = morph_info.get('original')
                  original_card_temp = Card(original_info) # Temporary card to parse cost
                  match = re.search(r"morph\s*(\{.*?\})", getattr(original_card_temp, 'oracle_text', '').lower())
                  if match: cost_to_pay_str = match.group(1)
                  else: logging.warning(f"Could not parse Morph cost for {original_info.get('name')}")

        # Check generic face-down attribute if specific tracking missed
        if not is_face_down and getattr(card, 'face_down', False):
             is_face_down = True
             source_mechanic = "Unknown Face-down" # Possibly from other effects
             # Cannot determine original info or cost for generic face-down easily
             logging.warning(f"Cannot turn face-down card {card_id} up: Unknown origin or cost.")
             return False


        if not is_face_down:
            logging.debug(f"Cannot turn {card.name} face up: Not face down.")
            return False

        if (pay_morph_cost or pay_manifest_cost):
            if not cost_to_pay_str:
                logging.debug(f"Cannot turn {card.name} face up: No valid cost found for {source_mechanic}.")
                return False
            # Check and Pay Cost
            if not self.mana_system.can_pay_mana_cost(player, cost_to_pay_str):
                 logging.debug(f"Cannot turn {card.name} face up: Cannot afford cost {cost_to_pay_str}.")
                 return False
            if not self.mana_system.pay_mana_cost(player, cost_to_pay_str):
                 logging.warning(f"Failed to pay cost {cost_to_pay_str} for turning {card.name} face up.")
                 return False

        # If cost paid or turning face up for other reason (e.g., effect):
        if not original_info: # If turning up a generic face-down, we might not have original info
             # Maybe check card's own definition if it wasn't morphed/manifested? Complex.
             logging.warning(f"Turning {card.name} face up, but original info unknown (not Morph/Manifest).")
             # Minimal change: just mark face up, assume current stats are correct.
             card.face_down = False
        else:
             # Restore original card properties from original_info dict
             original_card_temp = Card(original_info) # Create temp instance to avoid modifying original_info
             card.name = getattr(original_card_temp, 'name', card.name)
             card.power = getattr(original_card_temp, 'power', card.power)
             card.toughness = getattr(original_card_temp, 'toughness', card.toughness)
             card.card_types = getattr(original_card_temp, 'card_types', card.card_types).copy()
             card.subtypes = getattr(original_card_temp, 'subtypes', card.subtypes).copy()
             card.supertypes = getattr(original_card_temp, 'supertypes', card.supertypes).copy()
             card.oracle_text = getattr(original_card_temp, 'oracle_text', card.oracle_text)
             card.mana_cost = getattr(original_card_temp, 'mana_cost', card.mana_cost)
             card.cmc = getattr(original_card_temp, 'cmc', card.cmc)
             card.colors = getattr(original_card_temp, 'colors', card.colors).copy()
             card.keywords = getattr(original_card_temp, 'keywords', card.keywords).copy()
             card.type_line = getattr(original_card_temp, 'type_line', card.type_line)
             # Restore other necessary attributes

             card.face_down = False
             # Clear from morph/manifest tracking
             if source_mechanic == "Morph": self.morphed_cards.pop(card_id, None)
             if source_mechanic == "Manifest": self.manifested_cards.pop(card_id, None)

        logging.debug(f"Turned {card.name} face up.")
        self.trigger_ability(card_id, "TURNED_FACE_UP")
        if self.layer_system: self.layer_system.invalidate_cache(); self.layer_system.apply_all_effects() # Abilities might change
        return True

    def clash(self, player1, player2):
        """Perform clash."""
        # Ensure players are valid and have libraries
        if not player1 or not player2 or not player1.get("library") or not player2.get("library"):
             logging.warning("Clash cannot occur: Invalid players or empty library.")
             return None

        card1_id = player1["library"].pop(0)
        card2_id = player2["library"].pop(0)
        card1 = self._safe_get_card(card1_id)
        card2 = self._safe_get_card(card2_id)
        cmc1 = getattr(card1, 'cmc', -1) if card1 else -1
        cmc2 = getattr(card2, 'cmc', -1) if card2 else -1

        name1 = getattr(card1,'name','nothing')
        name2 = getattr(card2,'name','nothing')
        logging.debug(f"Clash: {player1['name']} revealed {name1} (CMC {cmc1}), {player2['name']} revealed {name2} (CMC {cmc2})")

        # AI Choice needed for top/bottom. Simple: put back on top for now.
        # Store revealed cards temporarily for potential choice phase
        self.clash_context = {'p1': (card1_id, card1), 'p2': (card2_id, card2)}
        # TODO: Implement PHASE_CHOOSE for clash result destination
        # Temporary: Put back on top
        if card1_id: player1["library"].insert(0, card1_id)
        if card2_id: player2["library"].insert(0, card2_id)

        # Trigger clash event
        self.trigger_ability(None, "CLASHED", {"player1": player1, "player2": player2, "card1_id": card1_id, "card2_id": card2_id})

        # Return winning player (or None for draw/neither)
        if cmc1 > cmc2:
            logging.debug(f"Clash result: {player1['name']} wins.")
            return player1
        elif cmc2 > cmc1:
            logging.debug(f"Clash result: {player2['name']} wins.")
            return player2
        else:
            logging.debug("Clash result: Draw.")
            return None
        
    def _find_card_in_hand(self, player, identifier):
        """Finds a card ID in the player's hand using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["hand"]):
                  return player["hand"][identifier]
        elif isinstance(identifier, str):
             if identifier in player["hand"]:
                  return identifier
        return None
        
    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["battlefield"]):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             # Check if it's a direct ID
             if identifier in player["battlefield"]:
                  return identifier
             # Could potentially add lookup by name here if needed, but ID/index preferred
        return None

    def conspire(self, player, spell_stack_idx, creature1_identifier, creature2_identifier):
        """Perform conspire."""
        if spell_stack_idx < 0 or spell_stack_idx >= len(self.stack) or self.stack[spell_stack_idx][0] != "SPELL":
             logging.warning("Invalid spell index for conspire.")
             return False

        spell_type, spell_id, controller, context = self.stack[spell_stack_idx]
        if controller != player: return False # Can only conspire own spells
        spell_card = self._safe_get_card(spell_id)
        if not spell_card: return False

        # --- Find Creatures ---
        c1_id = self._find_permanent_id(player, creature1_identifier)
        c2_id = self._find_permanent_id(player, creature2_identifier)

        if not c1_id or not c2_id or c1_id == c2_id:
             logging.warning("Invalid or duplicate creatures for conspire.")
             return False

        c1 = self._safe_get_card(c1_id)
        c2 = self._safe_get_card(c2_id)

        if not c1 or 'creature' not in getattr(c1, 'card_types', []) or c1_id in player.get("tapped_permanents", set()):
             logging.warning(f"Creature 1 ({getattr(c1,'name','N/A')}) invalid or tapped for conspire.")
             return False
        if not c2 or 'creature' not in getattr(c2, 'card_types', []) or c2_id in player.get("tapped_permanents", set()):
             logging.warning(f"Creature 2 ({getattr(c2,'name','N/A')}) invalid or tapped for conspire.")
             return False

        # Check color sharing
        if self._share_color(spell_card, c1) and self._share_color(spell_card, c2):
            success_tap1 = self.tap_permanent(c1_id, player)
            success_tap2 = self.tap_permanent(c2_id, player)
            if not success_tap1 or not success_tap2:
                 # Rollback taps if needed (simple untap here)
                 if success_tap1: self.untap_permanent(c1_id, player)
                 if success_tap2: self.untap_permanent(c2_id, player)
                 logging.warning("Failed to tap creatures for conspire.")
                 return False

            # Create copy
            new_context = context.copy()
            new_context["is_copy"] = True
            new_context["is_conspired"] = True
            # Conspire copy typically needs new targets
            # Set flag to re-target the copy on resolution? Or require target choice here?
            new_context["needs_new_targets"] = True
            self.add_to_stack(spell_type, spell_id, player, new_context)
            logging.debug(f"Conspired {spell_card.name}")
            return True
        else:
            logging.debug("Creatures do not share a color with conspired spell.")
            return False

    def manifest_card(self, player, count=1):
         """Manifest the top card(s) of the library."""
         manifested_ids = []
         for _ in range(count):
             if not player["library"]: break
             card_id = player["library"].pop(0)
             original_info = self._safe_get_card(card_id).__dict__.copy() # Store original data

             # Create face-down creature state
             manifest_data = {
                 "name": "Manifested Creature", # Generic name
                 "power": 2, "toughness": 2,
                 "card_types": ["creature"], "subtypes": [], "supertypes": [],
                 "colors": [0,0,0,0,0], # Colorless
                 "mana_cost": "", "cmc": 0,
                 "oracle_text": "Face-down creature (2/2). Can be turned face up.",
                 "face_down": True
             }
             # Create a new Card object for the face-down state *or* modify existing?
             # Modifying existing is simpler for tracking, but needs careful state management.
             # Let's modify the existing card object in card_db.
             manifested_card = self._safe_get_card(card_id)
             if manifested_card:
                 manifested_card.power = manifest_data["power"]
                 manifested_card.toughness = manifest_data["toughness"]
                 manifested_card.card_types = manifest_data["card_types"]
                 manifested_card.subtypes = manifest_data["subtypes"]
                 # Keep original name/mana cost/etc hidden but associated? Use tracking dict.
                 if not hasattr(self, 'manifested_cards'): self.manifested_cards = {}
                 self.manifested_cards[card_id] = {'original': original_info, 'face_down': True} # Store original
                 manifested_card.face_down = True # Set flag on card object too

                 # Move to battlefield
                 success = self.move_card(card_id, player, "library_implicit", player, "battlefield")
                 if success: manifested_ids.append(card_id)
                 else: # Failed move, undo?
                      player["library"].insert(0, card_id) # Put back
                      if card_id in self.manifested_cards: del self.manifested_cards[card_id] # Clean up tracking
                      manifested_card.face_down = False # Reset flag
         if manifested_ids:
             logging.debug(f"Manifested {len(manifested_ids)} card(s).")
             return manifested_ids
         return None
     

    def damage_planeswalker(self, planeswalker_id, amount, source_id):
        """Deal damage to a planeswalker (removes loyalty counters). Returns actual damage dealt."""
        pw_card = self._safe_get_card(planeswalker_id)
        owner = self.get_card_controller(planeswalker_id)
        if not pw_card or not owner or 'planeswalker' not in getattr(pw_card, 'card_types', []):
            return 0 # Indicate no damage applied

        # Apply damage replacement effects targeting this planeswalker
        damage_context = { "source_id": source_id, "target_id": planeswalker_id, "target_obj": pw_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": False } # Assume non-combat unless context passed
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                 logging.debug(f"Damage to PW {pw_card.name} prevented or reduced to 0.")
                 return 0 # No damage applied
            # TODO: Handle redirection if target changes

        if actual_damage > 0:
            # Use a dedicated method to remove loyalty counters
            counters_removed = self._remove_loyalty_counters(planeswalker_id, owner, actual_damage)

            if counters_removed > 0:
                source_name = getattr(self._safe_get_card(source_id),'name',source_id)
                current_loyalty = owner.get("loyalty_counters", {}).get(planeswalker_id, 0)
                logging.debug(f"{source_name} dealt {counters_removed} damage to {pw_card.name}. Loyalty now {current_loyalty}")
                self.trigger_ability(planeswalker_id, "DAMAGED", {"amount": counters_removed, "source_id": source_id})
                self.check_state_based_actions() # PW might die
                return counters_removed # Return damage actually applied as counter removal
        return 0 # No damage applied or counters removed
    
    def _remove_loyalty_counters(self, planeswalker_id, owner, amount):
        """Removes loyalty counters from a planeswalker. Returns amount removed."""
        if amount <= 0: return 0
        pw_card = self._safe_get_card(planeswalker_id)
        current_loyalty = owner.get("loyalty_counters", {}).get(planeswalker_id, getattr(pw_card, 'loyalty', 0) if pw_card else 0)
        amount_to_remove = min(amount, current_loyalty) # Cannot remove more than current loyalty
        new_loyalty = current_loyalty - amount_to_remove
        owner.setdefault("loyalty_counters", {})[planeswalker_id] = new_loyalty
        return amount_to_remove

    def damage_battle(self, battle_id, amount, source_id):
        """Deal damage to a battle (removes defense counters). Returns actual damage dealt."""
        battle_card = self._safe_get_card(battle_id)
        owner = self.get_card_controller(battle_id)
        if not battle_card or not owner or 'battle' not in getattr(battle_card, 'type_line', ''):
            return 0 # Indicate no damage applied

        # Apply damage replacement effects targeting this battle
        damage_context = { "source_id": source_id, "target_id": battle_id, "target_obj": battle_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": False } # Assume non-combat unless context passed
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
             # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                 logging.debug(f"Damage to Battle {battle_card.name} prevented or reduced to 0.")
                 return 0 # No damage applied
            # TODO: Handle redirection

        if actual_damage > 0:
            # Use add_defense_counter with negative amount
            success = self.add_defense_counter(battle_id, -actual_damage)
            if success:
                source_name = getattr(self._safe_get_card(source_id),'name',source_id)
                current_defense = getattr(self,'battle_cards',{}).get(battle_id,0) # Read current defense
                logging.debug(f"{source_name} dealt {actual_damage} damage to {battle_card.name}. Defense now {current_defense}")
                self.trigger_ability(battle_id, "DAMAGED", {"amount": actual_damage, "source_id": source_id})
                # SBA check for battle defeat handled within add_defense_counter or separate SBA check
                self.check_state_based_actions()
                return actual_damage # Return damage successfully applied
        return 0 # No damage applied
    
    def damage_player(self, player, amount, source_id, is_combat_damage=False):
        """Deals damage to a player, applying replacements. Returns actual damage dealt."""
        if not player or amount <= 0: return 0

        player_id = "p1" if player == self.p1 else "p2"
        player_name = player.get('name', player_id)

        damage_context = { "source_id": source_id, "target_id": player_id, "target_obj": player, "target_is_player": True, "damage_amount": amount, "is_combat_damage": is_combat_damage }
        actual_damage = amount

        # Apply replacements
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            if actual_damage <= 0 or modified_context.get("prevented"):
                logging.debug(f"Damage to player {player_name} prevented or reduced to 0.")
                return 0

        # Apply damage (life loss)
        if actual_damage > 0:
            player['life'] -= actual_damage
            logging.debug(f"Player {player_name} took {actual_damage} damage. Life now {player['life']}.")
            # Track damage this turn
            self.damage_dealt_this_turn[player_id] = self.damage_dealt_this_turn.get(player_id, 0) + actual_damage
            player['lost_life_this_turn'] = True
            # Trigger "damaged" or "lost life" events
            self.trigger_ability(None, "PLAYER_DAMAGED", {"player": player, "amount": actual_damage, "source_id": source_id})
            self.trigger_ability(None, "LOSE_LIFE", {"player": player, "amount": actual_damage, "source_id": source_id})
            self.check_state_based_actions() # Player might lose
            return actual_damage
        return 0
    
    def handle_lifelink_gain(self, source_id, player_gaining_life, damage_dealt):
        """Handles life gain specifically from lifelink, applying replacements."""
        if damage_dealt <= 0 or not player_gaining_life: return

        gain_context = {'player': player_gaining_life, 'life_amount': damage_dealt, 'source_id': source_id, 'source_type': 'lifelink'}
        final_life_gain = damage_dealt

        # Apply LIFE_GAIN replacement effects
        if hasattr(self, 'replacement_effects'):
            modified_gain_context, gain_replaced = self.replacement_effects.apply_replacements("LIFE_GAIN", gain_context)
            final_life_gain = modified_gain_context.get('life_amount', 0)
            if final_life_gain <= 0 or modified_gain_context.get('prevented'):
                 logging.debug(f"Lifelink gain from {source_id} prevented or reduced to 0.")
                 return

        if final_life_gain > 0:
             player_gaining_life['life'] += final_life_gain
             source_name = getattr(self._safe_get_card(source_id), 'name', source_id)
             logging.debug(f"Lifelink: {player_gaining_life['name']} gained {final_life_gain} life from {source_name}.")
             # Trigger GAIN_LIFE event
             self.trigger_ability(source_id, "GAIN_LIFE", {"player": player_gaining_life, "amount": final_life_gain, "source_id": source_id})

    def apply_damage_to_permanent(self, target_id, amount, source_id, is_combat_damage=False, has_deathtouch=False):
        """Marks damage on a creature, considering deathtouch. Returns actual damage marked."""
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)
        if not target_card or not target_owner or 'creature' not in getattr(target_card, 'card_types', []):
            return 0 # Indicate no damage applied

        # Apply damage replacement effects targeting this creature
        damage_context = { "source_id": source_id, "target_id": target_id, "target_obj": target_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": is_combat_damage }
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                logging.debug(f"Damage to {target_card.name} prevented or reduced to 0.")
                return 0 # No damage applied
            # Update deathtouch status based on replacement? Less common, assume it sticks for now.
            # TODO: Handle redirection if target changes (complex)

        if actual_damage > 0:
             target_owner.setdefault("damage_counters", {})[target_id] = target_owner.get("damage_counters", {}).get(target_id, 0) + actual_damage
             if has_deathtouch:
                  target_owner.setdefault("deathtouch_damage", {})[target_id] = True
             source_name = getattr(self._safe_get_card(source_id),'name',source_id)
             logging.debug(f"{source_name} marked {actual_damage} damage on {target_card.name}{' (Deathtouch)' if has_deathtouch else ''}.")
             # Trigger DAMAGED event immediately after marking
             self.trigger_ability(target_id, "DAMAGED", {"amount": actual_damage, "source_id": source_id, "is_combat": is_combat_damage})
             # SBA check will happen later in the game loop
             return actual_damage # Return damage actually marked
        return 0 # No damage applied

    def amass(self, player, amount):
        """Perform Amass N. Finds or creates Army token and adds counters."""
        army_token_id = None
        # Find existing Army token
        for cid in player["battlefield"]:
            card = self._safe_get_card(cid)
            if card and "Army" in getattr(card, 'subtypes', []):
                army_token_id = cid
                break
        # Create if doesn't exist
        if not army_token_id:
            token_data = {"name":"Zombie Army", "power":0, "toughness":0, "card_types":["creature"], "subtypes":["Zombie", "Army"], "colors":[0,0,1,0,0]} # Black zombie
            army_token_id = self.create_token(player, token_data)
            if army_token_id:
                logging.debug("Created 0/0 Zombie Army token for Amass.")
            else:
                 logging.error("Failed to create Army token for Amass.")
                 return False

        if army_token_id:
             success = self.add_counter(army_token_id, "+1/+1", amount)
             if success: logging.debug(f"Amass {amount}: Added {amount} +1/+1 counters to Army.")
             return success
        return False
    
    def explore(self, player, creature_id):
        """Perform explore for a creature."""
        if not player or "library" not in player or not player["library"]:
            logging.debug("Explore: Library empty.")
            return False # Nothing to reveal

        top_card_id = player["library"].pop(0) # Remove from top
        top_card = self._safe_get_card(top_card_id)
        if not top_card: # Should not happen if library is just IDs
            logging.error(f"Explore failed: Invalid card ID {top_card_id} found in library.")
            return False
        card_name = getattr(top_card,'name','Unknown Card')
        exploring_creature = self._safe_get_card(creature_id)
        exploring_creature_name = getattr(exploring_creature, 'name', creature_id) if exploring_creature else creature_id
        logging.debug(f"Exploring (via {exploring_creature_name}): Revealed {card_name}")

        is_land = 'land' in getattr(top_card, 'type_line', '').lower()

        if is_land:
            success_move = self.move_card(top_card_id, player, "library_implicit", player, "hand") # Use implicit source zone
            if success_move:
                 logging.debug(f"Explore hit a land ({card_name}), put into hand.")
                 self.trigger_ability(creature_id, "EXPLORED_LAND", {"revealed_card_id": top_card_id})
            else:
                 player["library"].insert(0, top_card_id) # Put back if move fails? Rare.
            return success_move
        else:
            # Put +1/+1 counter on exploring creature
            success_counter = self.add_counter(creature_id, "+1/+1", 1)
            if success_counter: logging.debug(f"Explore hit nonland, put +1/+1 counter on {exploring_creature_name}")

            # AI choice: top or graveyard? Use CardEvaluator if available.
            put_in_gy = True # Default to graveyard
            if self.card_evaluator:
                 value = self.card_evaluator.evaluate_card(top_card_id, "explore_nonland")
                 if value > 0.6: # Threshold to keep non-land on top
                      put_in_gy = False
            elif getattr(top_card, 'cmc', 0) >= 4: # Simple heuristic: Keep expensive non-lands
                put_in_gy = False

            if put_in_gy:
                 success_move = self.move_card(top_card_id, player, "library_implicit", player, "graveyard")
                 if success_move: logging.debug(f"Explore: Put nonland {card_name} into graveyard.")
                 else: player["library"].insert(0, top_card_id) # Put back if move fails
                 self.trigger_ability(creature_id, "EXPLORED_NONLAND_GY", {"revealed_card_id": top_card_id})
                 return success_move
            else:
                 player["library"].insert(0, top_card_id) # Put back on top
                 logging.debug(f"Explore: Kept nonland {card_name} on top.")
                 self.trigger_ability(creature_id, "EXPLORED_NONLAND_TOP", {"revealed_card_id": top_card_id})
                 return True

    def adapt(self, player, creature_id, amount):
        """Perform adapt N."""
        card = self._safe_get_card(creature_id)
        # Adapt only if creature has no +1/+1 counters
        if card and getattr(card, 'counters', {}).get('+1/+1', 0) == 0:
            success = self.add_counter(creature_id, '+1/+1', amount)
            if success:
                logging.debug(f"Adapt {amount}: Added {amount} counters to {card.name}.")
                self.trigger_ability(creature_id, "ADAPTED", {"amount": amount})
            return success
        else:
            logging.debug(f"Adapt: Cannot adapt {getattr(card,'name',creature_id)} (already has +1/+1 counters or not found).")
            return False

    def goad_creature(self, target_id):
        """Mark creature as goaded."""
        card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)
        if not card or 'creature' not in getattr(card, 'card_types', []) or not target_owner: return False

        # Track goaded status, perhaps on the player dictionary
        target_owner.setdefault("goaded_creatures", set()).add(target_id)
        # Could store turn goaded for duration: target_owner.setdefault("goaded_status", {})[target_id] = self.turn
        logging.debug(f"Goaded {card.name}")
        self.trigger_ability(target_id, "GOADED") # Trigger ability if needed
        return True

    def prevent_damage(self, target, amount):
        """Register damage prevention. (Uses Replacement System)"""
        if not self.replacement_effects:
             logging.warning("Cannot prevent damage: ReplacementEffectSystem missing.")
             return False
        target_key = target['name'] if isinstance(target, dict) else target # Player dict or permanent ID
        source_name = "Generic Prevention" # Need source context usually
        logging.debug(f"Registering {amount} damage prevention for {target_key}.")

        def condition(ctx):
            # Basic check: Target matches, damage > 0
            return ctx.get('target_id') == target_key and ctx.get('damage_amount', 0) > 0

        def replacement(ctx):
            original_damage = ctx.get('damage_amount', 0)
            prevented = min(original_damage, amount)
            ctx['damage_amount'] = max(0, original_damage - prevented)
            logging.debug(f"Prevention: Prevented {prevented} damage to {target_key}. Remaining: {ctx['damage_amount']}")
            # TODO: Track remaining prevention shield if limited use
            return ctx

        # Needs a source ID and duration, use placeholders
        self.replacement_effects.register_effect({
             'source_id': 'PREVENTION_EFFECT', 'event_type': 'DAMAGE',
             'condition': condition, 'replacement': replacement,
             'duration': 'end_of_turn', 'controller_id': None, # Affects target, not controller based
             'description': f"Prevent {amount} damage to {target_key}"
        })
        return True

    def redirect_damage(self, source_filter, original_target, new_target):
        """Register damage redirection. (Uses Replacement System)"""
        if not self.replacement_effects:
             logging.warning("Cannot redirect damage: ReplacementEffectSystem missing.")
             return False
        original_target_key = original_target['name'] if isinstance(original_target, dict) else original_target
        new_target_key = new_target['name'] if isinstance(new_target, dict) else new_target
        new_target_is_player = isinstance(new_target, dict)
        new_target_obj = new_target if new_target_is_player else self._safe_get_card(new_target_key)
        new_target_owner = new_target if new_target_is_player else self.get_card_controller(new_target_key)

        logging.debug(f"Registering damage redirection from {original_target_key} to {new_target_key}.")

        def condition(ctx):
            # Check source matches filter (basic: allow any for now)
            # Check original target matches
            return ctx.get('target_id') == original_target_key and ctx.get('damage_amount', 0) > 0

        def replacement(ctx):
            original_damage = ctx.get('damage_amount', 0)
            logging.debug(f"Redirecting {original_damage} damage from {original_target_key} to {new_target_key}.")
            ctx['damage_amount'] = 0 # Prevent original damage
            ctx['redirected'] = True
            # --- Schedule separate damage event to new target ---
            # Avoid applying damage directly inside replacement to prevent loops
            def deal_redirected_damage():
                 if new_target_is_player:
                     if hasattr(new_target_obj, 'life'): new_target_obj['life'] -= original_damage
                 else:
                     self.apply_damage_to_permanent(new_target_key, original_damage, ctx.get('source_id', 'redirect_source'))

            if not hasattr(self, 'delayed_triggers'): self.delayed_triggers = []
            self.delayed_triggers.append(deal_redirected_damage)
            return ctx

        # Needs source ID and duration
        self.replacement_effects.register_effect({
             'source_id': 'REDIRECT_EFFECT', 'event_type': 'DAMAGE',
             'condition': condition, 'replacement': replacement,
             'duration': 'end_of_turn', 'controller_id': None, # Belongs to game state rule?
             'description': f"Redirect damage from {original_target_key} to {new_target_key}"
        })
        return True


    def counter_spell(self, stack_index):
        """Counter spell at stack_index."""
        if 0 <= stack_index < len(self.stack):
            item_type, card_id, controller, context = self.stack.pop(stack_index)
            if item_type == "SPELL":
                # Prevent "leaves stack" triggers if appropriate? Rules check needed.
                # Move to graveyard unless specified otherwise (e.g., exile by counter)
                target_zone = context.get('counter_to_zone', 'graveyard')
                self.move_card(card_id, controller, "stack_implicit", controller, target_zone)
                logging.debug(f"Countered spell {self._safe_get_card(card_id).name}, moved to {target_zone}.")
                self.last_stack_size = len(self.stack) # Update stack size immediately
                return True
            else: # Not a spell, put it back
                self.stack.insert(stack_index, (item_type, card_id, controller, context))
        return False

    def counter_ability(self, stack_index):
        """Counter ability/trigger at stack_index."""
        if 0 <= stack_index < len(self.stack):
            item_type, card_id, controller, context = self.stack[stack_index]
            if item_type == "ABILITY" or item_type == "TRIGGER":
                self.stack.pop(stack_index)
                logging.debug(f"Countered {item_type} from {self._safe_get_card(card_id).name}")
                self.last_stack_size = len(self.stack)
                return True
        return False

    def add_temp_buff(self, card_id, buff_data):
         """Add a temporary buff until end of turn."""
         owner = self._find_card_controller(card_id)
         if owner:
             if not hasattr(owner, 'temp_buffs'): owner['temp_buffs'] = {}
             if card_id not in owner['temp_buffs']: owner['temp_buffs'][card_id] = {'power':0, 'toughness':0, 'until_end_of_turn': True}
             owner['temp_buffs'][card_id]['power'] += buff_data.get('power', 0)
             owner['temp_buffs'][card_id]['toughness'] += buff_data.get('toughness', 0)
             return True
         return False
     
    def _find_card_controller(self, card_id):
        """Find which player controls a card currently on the battlefield."""
        for p in [self.p1, self.p2]:
            if card_id in p.get("battlefield",[]):
                return p
        return None

    def _get_permanent_at_idx(self, player, index):
         """Safely get permanent from battlefield index."""
         if index < len(player["battlefield"]):
             return self._safe_get_card(player["battlefield"][index])
         return None

    def _share_color(self, card1, card2):
        """Check if two cards share a color."""
        if not card1 or not card2 or not hasattr(card1, 'colors') or not hasattr(card2, 'colors'): return False
        # Compare the 5-element color arrays
        return any(c1 and c2 for c1, c2 in zip(card1.colors[:5], card2.colors[:5]))

    # --- Mana System Helper Getters ---
    def _get_equip_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            match = re.search(r"equip\s*(\{.*?\})", card.oracle_text.lower())
            if match: return match.group(1)
            match = re.search(r"equip\s*(\d+)", card.oracle_text.lower())
            if match: return f"{{{match.group(1)}}}"
        return None

    def _get_fortify_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"fortify\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"fortify\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_reconfigure_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"reconfigure\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"reconfigure\s*(\d+)", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None
    
        # Add helper method to resolve individual mode effects
    def _resolve_mode_effects(self, spell_id, controller, effect_text, targets, context):
        """
        Resolve a specific mode effect.
        
        Args:
            spell_id: The ID of the spell
            controller: The player casting the spell
            effect_text: The text of the effect to apply
            targets: Targets for this mode
            context: Additional context
        """
        # Parse and apply the effect based on common patterns
        effect_text = effect_text.lower()
        
        # Import modules we'll need
        import re
        from .ability_types import DamageEffect, DrawCardEffect, GainLifeEffect
        
        # Try to create a proper effect using ability_handler
        effect = None
        if hasattr(self, 'ability_handler') and hasattr(self.ability_handler, '_create_ability_effects'):
            try:
                effects = self.ability_handler._create_ability_effects(effect_text, targets)
                if effects:
                    for effect in effects:
                        effect.apply(self, spell_id, controller, targets)
                    return
            except Exception as e:
                logging.error(f"Error creating effect from text '{effect_text}': {str(e)}")
        
        # Fallback pattern matching for common effects
        if "draw" in effect_text and "card" in effect_text:
            # Card draw effect
            match = re.search(r"draw (\w+) cards?", effect_text)
            count = 1
            if match:
                count_word = match.group(1)
                if count_word.isdigit():
                    count = int(count_word)
                elif count_word == "two":
                    count = 2
                elif count_word == "three":
                    count = 3
                    
            for _ in range(count):
                self._draw_phase(controller)
            logging.debug(f"Mode effect: drew {count} cards")
            
        elif "damage" in effect_text:
            # Damage effect
            match = re.search(r"(\d+) damage", effect_text)
            damage = 2  # Default
            if match:
                damage = int(match.group(1))
                
            # Determine target
            opponent = self.p2 if controller == self.p1 else self.p1
            
            if "to target player" in effect_text or "to any target" in effect_text:
                # Damage to opponent
                opponent["life"] -= damage
                logging.debug(f"Mode effect: dealt {damage} damage to opponent")
                
            elif "to target creature" in effect_text or "to target permanent" in effect_text:
                # For simplicity, target the strongest opponent creature
                creatures = [cid for cid in opponent["battlefield"] 
                        if self._safe_get_card(cid) and 
                        hasattr(self._safe_get_card(cid), 'card_types') and 
                        'creature' in self._safe_get_card(cid).card_types]
                
                if creatures:
                    target = max(creatures, key=lambda cid: self._safe_get_card(cid).power 
                                                        if hasattr(self._safe_get_card(cid), 'power') else 0)
                    target_card = self._safe_get_card(target)
                    
                    # Check if lethal damage
                    if target_card.toughness <= damage:
                        self.move_card(target, opponent, "battlefield", opponent, "graveyard")
                        logging.debug(f"Mode effect: killed {target_card.name} with {damage} damage")
                    else:
                        # Add damage counter
                        if "damage_counters" not in opponent:
                            opponent["damage_counters"] = {}
                        opponent["damage_counters"][target] = opponent["damage_counters"].get(target, 0) + damage
                        logging.debug(f"Mode effect: dealt {damage} damage to {target_card.name}")
        
        elif "gain" in effect_text and "life" in effect_text:
            # Life gain effect
            match = re.search(r"gain (\d+) life", effect_text)
            life_gain = 2  # Default
            if match:
                life_gain = int(match.group(1))
                
            controller["life"] += life_gain
            logging.debug(f"Mode effect: gained {life_gain} life")
        
        elif "create" in effect_text and "token" in effect_text:
            # Token creation effect
            match = re.search(r"create (?:a|an|\d+) (.*?) token", effect_text)
            if match:
                token_desc = match.group(1)
                
                # Parse token details
                power, toughness = 1, 1
                pt_match = re.search(r"(\d+)/(\d+)", token_desc)
                if pt_match:
                    power = int(pt_match.group(1))
                    toughness = int(pt_match.group(2))
                
                # Parse token type
                token_type = "creature"
                if "artifact" in token_desc:
                    token_type = "artifact"
                if "treasure" in token_desc:
                    token_type = "treasure"
                    
                # Create token data
                token_data = {
                    "name": f"{token_desc.title()} Token",
                    "power": power,
                    "toughness": toughness,
                    "card_types": [token_type],
                    "subtypes": [],
                    "oracle_text": ""
                }
                
                # Add specific token abilities
                if "flying" in token_desc:
                    token_data["oracle_text"] += "Flying\n"
                if "vigilance" in token_desc:
                    token_data["oracle_text"] += "Vigilance\n"
                if "treasure" in token_desc:
                    token_data["oracle_text"] += "{T}, Sacrifice this artifact: Add one mana of any color."
                    
                # Create the token
                self.create_token(controller, token_data)
                logging.debug(f"Mode effect: created a {token_desc} token")
        
        elif "exile" in effect_text:
            # Exile effect
            opponent = self.p2 if controller == self.p1 else self.p1
            
            if "exile target permanent" in effect_text or "exile target creature" in effect_text:
                # For simplicity, target the strongest opponent creature
                target_type = "permanent" if "target permanent" in effect_text else "creature"
                
                if target_type == "creature":
                    targets = [cid for cid in opponent["battlefield"] 
                            if self._safe_get_card(cid) and 
                            hasattr(self._safe_get_card(cid), 'card_types') and 
                            'creature' in self._safe_get_card(cid).card_types]
                else:
                    targets = opponent["battlefield"]
                    
                if targets:
                    # For creatures, target the strongest one
                    if target_type == "creature":
                        target = max(targets, key=lambda cid: self._safe_get_card(cid).power 
                                                        if hasattr(self._safe_get_card(cid), 'power') else 0)
                    else:
                        # For any permanent, just take the first one
                        target = targets[0]
                        
                    target_card = self._safe_get_card(target)
                    self.move_card(target, opponent, "battlefield", opponent, "exile")
                    logging.debug(f"Mode effect: exiled {target_card.name}")
        
        elif "counter target" in effect_text:
            # Counter spell effect
            if self.stack:
                # Get the top spell on the stack
                top_item = self.stack[-1]
                
                if isinstance(top_item, tuple) and len(top_item) >= 3 and top_item[0] == "SPELL":
                    spell_id = top_item[1]
                    spell = self._safe_get_card(spell_id)
                    
                    # Check if this spell meets the counter conditions
                    can_counter = True
                    
                    if "counter target creature spell" in effect_text:
                        can_counter = hasattr(spell, 'card_types') and 'creature' in spell.card_types
                    elif "counter target noncreature spell" in effect_text:
                        can_counter = hasattr(spell, 'card_types') and 'creature' not in spell.card_types
                    
                    if can_counter:
                        # Remove from stack
                        self.stack.pop()
                        
                        # Move to graveyard
                        spell_controller = top_item[2]
                        spell_controller["graveyard"].append(spell_id)
                        
                        logging.debug(f"Mode effect: countered {spell.name}")
    
    def find_card_location(self, card_id):
        """
        Find which player controls a card and in which zone it is.
        Also handles finding the controller of the source of an effect on the stack.

        Args:
            card_id: ID of the card or stack item source to locate

        Returns:
            tuple: (player_object, zone_string) or (None, None) if not found
        """
        zones = ["battlefield", "hand", "graveyard", "exile", "library"]
        special_zones_map = {
             "adventure_cards": "adventure_zone", "phased_out": "phased_out",
             "foretold_cards": "foretold_zone", "suspended_cards": "suspended",
             "unearthed_cards": "unearthed_zone", # Add other special tracking if needed
             "morphed_cards": "face_down_zone", # Represent face-down state
             "manifested_cards": "face_down_zone",
             "commander_zone": "command", # Standardize command zone name
             "companion": "companion_zone",
        }

        # Check standard zones for both players
        for player in [self.p1, self.p2]:
            if not player: continue # Safety check
            for zone in zones:
                if zone in player and isinstance(player[zone], (list, set)) and card_id in player[zone]:
                    return player, zone

            # Check player-specific special zones (like revealed hand?) - Not standard MTG, skip for now.

        # Check game-level special zones / tracking dicts
        for attr_name, zone_name in special_zones_map.items():
            if hasattr(self, attr_name):
                 container = getattr(self, attr_name)
                 if isinstance(container, set) and card_id in container:
                     # Find original owner/controller if possible, default to p1
                     owner = self._find_card_owner_fallback(card_id) # Use fallback owner finder
                     return owner, zone_name
                 elif isinstance(container, dict) and card_id in container:
                      # Check if the dict value stores the controller
                      entry = container[card_id]
                      controller = entry.get("controller") if isinstance(entry, dict) else None
                      if controller: return controller, zone_name
                      # Fallback owner find
                      owner = self._find_card_owner_fallback(card_id)
                      return owner, zone_name

        # Check stack (Handles spells and abilities)
        for item in self.stack:
            # Stack items are tuples: (type, source_id, controller, context)
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == card_id:
                 return item[2], "stack" # Return the controller and "stack" zone

        # If not found in any common zone
        # logging.debug(f"Card/Source ID {card_id} not found in any tracked zone.")
        return None, None 
    
    # Add a helper to find original owner if controller isn't readily available
    def _find_card_owner_fallback(self, card_id):
        """Fallback to find card owner based on original deck assignment or DB."""
        # Check original decks if tracked
        if hasattr(self, 'original_p1_deck') and card_id in self.original_p1_deck:
             return self.p1
        if hasattr(self, 'original_p2_deck') and card_id in self.original_p2_deck:
             return self.p2
        # Last resort - default to p1 if owner ambiguous
        return self.p1

    # Consolidate get_card_controller (use find_card_location)
    def get_card_controller(self, card_id):
        """Find the controller of a card currently on the battlefield."""
        player, zone = self.find_card_location(card_id)
        if zone == "battlefield":
             return player
        # Consider returning controller even if not on battlefield?
        # Depends on rules context. For most purposes, only battlefield controller matters.
        # If you need owner regardless of zone, use _find_card_owner_fallback or similar.
        return None
            
    def _resolve_spree_spell(self, spell_id, controller, context):
        """
        Resolve a Spree spell with selected modes.
        
        Args:
            spell_id: The ID of the Spree spell
            controller: The player casting the spell
            context: Context containing selected modes
        """
        spell = self._safe_get_card(spell_id)
        if not spell or not hasattr(spell, 'spree_modes'):
            return
        
        # Get selected modes from context
        selected_modes = context.get("selected_modes", [])
        
        # First, apply the base spell effect
        if hasattr(spell, 'card_types'):
            # Handle different card types for the base spell
            if 'instant' in spell.card_types or 'sorcery' in spell.card_types:
                # For simplicity, just apply targeting and effects
                targets = context.get("targets")
                self.resolve_spell_effects(spell_id, controller, targets, context)
            else:
                # For permanents, put them on the battlefield
                controller["battlefield"].append(spell_id)
                self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
        
        # Apply effects for each selected mode
        for mode_idx in selected_modes:
            if mode_idx < len(spell.spree_modes):
                mode = spell.spree_modes[mode_idx]
                effect_text = mode.get("effect", "")
                
                # Create a context for this specific mode
                mode_context = dict(context)
                mode_context["mode_text"] = effect_text
                
                # Process targeting for this mode
                target_desc = mode.get("targets", "")
                mode_targets = context.get(f"mode_{mode_idx}_targets")
                
                # Apply the mode effect
                self._resolve_mode_effects(spell_id, controller, effect_text, mode_targets, mode_context)
                
                logging.debug(f"Applied Spree mode {mode_idx} for {spell.name}")
        
        # Move to graveyard if it's an instant or sorcery
        if hasattr(spell, 'card_types') and ('instant' in spell.card_types or 'sorcery' in spell.card_types):
            if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                controller["graveyard"].append(spell_id)
