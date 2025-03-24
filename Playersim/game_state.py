import random
import logging
import numpy as np
from .card import Card
from .debug import DEBUG_MODE
import re
from .ability_types import TriggeredAbility
class GameState:
    
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
                "card_evaluator", "spells_cast_this_turn", "_phase_history", "card_db",
                "strategic_planner", "attackers_this_turn", 'strategy_memory',
                "_logged_card_ids", "_logged_errors", "targeting_system",
                "_phase_action_count", "priority_player", "stats_tracker",
                # New slot variables for special card types
                "adventure_cards", "saga_counters", "mdfc_cards", "battle_cards",
                "cards_castable_from_exile", "cast_as_back_face",
                # Additional slots for various tracking variables
                "phased_out", "suspended_cards", "kicked_cards", "evoked_cards",
                "foretold_cards", "blitz_cards", "dash_cards", "unearthed_cards",
                "jump_start_cards", "buyback_cards", "flashback_cards",
                "life_gained_this_turn", "damage_this_turn", "exile_at_end_of_combat",
                "haste_until_eot", "has_haste_until_eot", "progress_was_forced",
                "_turn_limit_checked", "miracle_card", "miracle_cost", "miracle_player",
                # New tracking variables
                "split_second_active", "rebounded_cards", "banding_creatures",
                "crewed_vehicles", "morphed_cards", "cards_to_graveyard_this_turn",
                "boast_activated", "forecast_used", "epic_spells", "city_blessing",
                "myriad_tokens", "persist_returned", "undying_returned", "gravestorm_count"]
            
    # Phase constants
    PHASE_UNTAP = 0
    PHASE_UPKEEP = 1
    PHASE_DRAW = 2
    PHASE_MAIN_PRECOMBAT = 3
    PHASE_BEGIN_COMBAT = 4
    PHASE_DECLARE_ATTACKERS = 5
    PHASE_DECLARE_BLOCKERS = 6
    PHASE_COMBAT_DAMAGE = 7
    PHASE_END_COMBAT = 8
    PHASE_MAIN_POSTCOMBAT = 9
    PHASE_END_STEP = 10
    PHASE_PRIORITY = 11
    PHASE_TARGETING = 12
    PHASE_BEGINNING_OF_COMBAT = 13
    PHASE_END_OF_COMBAT = 14
    PHASE_CLEANUP = 15
    PHASE_FIRST_STRIKE_DAMAGE = 16

    def __init__(self, card_db, max_turns=20, max_hand_size=7, max_battlefield=20):
        """
        Initialize GameState with consolidated subsystem initialization.
        
        Args:
            card_db: Dictionary or list of card objects
            max_turns: Maximum number of turns before game ends
            max_hand_size: Maximum hand size
            max_battlefield: Maximum battlefield size
        """
        # Basic game parameters
        self.max_turns = max_turns
        self.max_hand_size = max_hand_size
        self.max_battlefield = max_battlefield
        
        # Initialize base variables
        self.turn = 1
        self.phase = self.PHASE_UNTAP
        self.agent_is_p1 = True
        self.combat_damage_dealt = False
        self.stack = []
        self.priority_pass_count = 0
        self.last_stack_size = 0
        self.phase_history = np.zeros(3, dtype=np.int32)
        self._phase_history = []
        self._phase_action_count = 0
        
        # Combat state initialization
        self.current_attackers = []
        self.current_block_assignments = {}
        self.current_spell_requires_target = False
        self.current_spell_card_id = None
        self.optimal_attackers = None
        self.attack_suggestion_used = False
        
        # Initialize system references as None (will be created by _init_subsystems)
        self.mana_system = None
        self.combat_resolver = None
        self.card_evaluator = None
        self.strategic_planner = None
        self.strategy_memory = None
        self.stats_tracker = None
        self.ability_handler = None
        self.layer_system = None
        self.replacement_effects = None
        self.targeting_system = None
        
        # Process card_db properly
        if isinstance(card_db, list):
            self.card_db = {i: card for i, card in enumerate(card_db)}
        elif isinstance(card_db, dict):
            self.card_db = card_db
        else:
            self.card_db = {}
            logging.error(f"Invalid card database format: {type(card_db)}")
        
        # Initialize all subsystems
        self._init_subsystems()
        
    def _init_subsystems(self):
        """
        Consolidated initialization method for all game subsystems.
        Replaces separate initialization methods for clarity and maintainability.
        """
        # Initialize card and rules handlers
        self._init_ability_handler()
        self._init_rules_systems()
        self._init_tracking_variables()
        self._init_strategic_planner()
        self._init_ability_handler()
        # Initialize day/night cycle
        self.initialize_day_night_cycle()
        
        # Initialize statistics tracking
        self.cards_played = {0: [], 1: []}
        self.mulligan_data = {'p1': 0, 'p2': 0}
        
        logging.debug("Initialized all game subsystems")

    def _init_rules_systems(self):
        """Initialize rules systems including layers, replacements, and mana handling."""
        # Initialize layer system for continuous effects
        try:
            from .layer_system import LayerSystem
            self.layer_system = LayerSystem(self)
            
            # Register common continuous effects for all permanents in play
            if hasattr(self.layer_system, 'register_common_effects'):
                self.layer_system.register_common_effects()
                
            logging.debug("Layer system initialized successfully")
        except ImportError as e:
            logging.warning(f"Layer system not available: {e}")
            self.layer_system = None
        
        # Initialize replacement effects system
        try:
            from .replacement_effects import ReplacementEffectSystem
            self.replacement_effects = ReplacementEffectSystem(self)
            
            # Cross-reference the layer system
            if self.layer_system:
                self.replacement_effects.layer_system = self.layer_system
            
            # Register common effects
            if self.replacement_effects:
                if hasattr(self.replacement_effects, 'register_common_effects'):
                    self.replacement_effects.register_common_effects()
            
            logging.debug("Replacement effects system initialized successfully")
        except ImportError as e:
            logging.warning(f"Replacement effects system not available: {e}")
            self.replacement_effects = None
        
        # Initialize enhanced mana system
        try:
            from .enhanced_mana_system import EnhancedManaSystem
            self.mana_system = EnhancedManaSystem(self)
            logging.debug("Enhanced mana system initialized successfully")
        except ImportError as e:
            logging.warning(f"Enhanced mana system not available: {e}")
            self.mana_system = None
        
        # Initialize combat resolver
        try:
            from .enhanced_combat import ExtendedCombatResolver
            self.combat_resolver = ExtendedCombatResolver(self)
            logging.debug("Combat resolver initialized successfully")
        except ImportError as e:
            logging.warning(f"Combat resolver not available: {e}")
            self.combat_resolver = None
        
        # Initialize card evaluator
        try:
            from .enhanced_card_evaluator import EnhancedCardEvaluator
            self.card_evaluator = EnhancedCardEvaluator(self)
            logging.debug("Card evaluator initialized successfully")
        except ImportError as e:
            logging.warning(f"Card evaluator not available: {e}")
            self.card_evaluator = None
        
        # Initialize targeting system
        try:
            if self.ability_handler and hasattr(self.ability_handler, 'targeting_system'):
                self.targeting_system = self.ability_handler.targeting_system
            else:
                from .ability_handler import TargetingSystem
                self.targeting_system = TargetingSystem(self)
            logging.debug("Targeting system initialized successfully")
        except ImportError as e:
            logging.warning(f"Targeting system not available: {e}")
            self.targeting_system = None
        except Exception as e:
            logging.error(f"Error initializing targeting system: {str(e)}")
            self.targeting_system = None
        
        # Initialize statistics tracker if available
        try:
            from .deck_stats_tracker import StatsTracker
            self.stats_tracker = StatsTracker(self)
            logging.debug("Statistics tracker initialized successfully")
        except ImportError as e:
            logging.warning(f"Statistics tracker not available: {e}")
            self.stats_tracker = None

    def _init_tracking_variables(self):
        """Initialize all game state tracking variables with proper defaults."""
        # Initialize turn tracking
        self.spells_cast_this_turn = []
        self.attackers_this_turn = set()
        self.damage_dealt_this_turn = {}
        self.cards_drawn_this_turn = {"p1": 0, "p2": 0}
        self.life_gained_this_turn = {}
        self.damage_this_turn = {}
        
        # Initialize special card state tracking
        self.adventure_cards = set()
        self.saga_counters = {}
        self.mdfc_cards = set()
        self.battle_cards = {}
        self.cards_castable_from_exile = set()
        self.cast_as_back_face = set()
        
        # Initialize effect tracking
        self.until_end_of_turn_effects = {}
        self.temp_control_effects = {}
        
        # Initialize phase tracking
        self._phase_history = []
        self._phase_action_count = 0
        self.progress_was_forced = False
        
        # Initialize abilities activated tracking
        self.abilities_activated_this_turn = []
        
        # Initialize special state tracking
        self.phased_out = set()
        self.suspended_cards = {}
        self.kicked_cards = set()
        self.evoked_cards = set()
        self.foretold_cards = set()
        self.blitz_cards = set()
        self.dash_cards = set()
        self.unearthed_cards = set()
        self.jump_start_cards = set()
        self.buyback_cards = set()
        self.flashback_cards = set()
        self.exile_at_end_of_combat = []
        self.haste_until_eot = set()
        self.has_haste_until_eot = set()
        self._turn_limit_checked = False
        
        # Initialize new tracking variables
        self.split_second_active = False
        self.rebounded_cards = {}
        self.banding_creatures = set()
        self.crewed_vehicles = set()
        self.morphed_cards = {}
        self.cards_to_graveyard_this_turn = {self.turn: []}
        self.boast_activated = set()
        self.forecast_used = set()
        self.epic_spells = {}
        self.city_blessing = {}
        self.myriad_tokens = []
        self.persist_returned = set()
        self.undying_returned = set()
        self.gravestorm_count = 0
        
        # Initialize logging trackers
        if not hasattr(self, '_logged_card_ids'):
            type(self)._logged_card_ids = set()
        if not hasattr(self, '_logged_errors'):
            type(self)._logged_errors = set()
        
        logging.debug("Initialized all tracking variables")

    def _init_strategic_planner(self):
        """
        Initialize the strategic planner with enhanced error handling and fallback mechanisms.
        """
        try:
            # Import at function level to avoid circular imports
            from .strategic_planner import MTGStrategicPlanner
            
            # Create strategic planner with robust initialization
            self.strategic_planner = MTGStrategicPlanner(
                game_state=self, 
                card_evaluator=self.card_evaluator, 
                combat_resolver=self.combat_resolver
            )
            
            # Additional initialization steps
            if hasattr(self.strategic_planner, 'init_after_reset'):
                self.strategic_planner.init_after_reset()
            
            logging.debug("Strategic planner initialized successfully")
            return self.strategic_planner
        
        except ImportError as e:
            logging.warning(f"Strategic planner module not available: {e}")
            self.strategic_planner = None
        
        except Exception as e:
            logging.error(f"Error initializing strategic planner: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            self.strategic_planner = None
        
        # Fallback: create a minimal strategic planner if possible
        try:
            from .strategic_planner import MTGStrategicPlanner
            self.strategic_planner = MTGStrategicPlanner(
                game_state=self, 
                card_evaluator=None, 
                combat_resolver=None
            )
            logging.warning("Created minimal strategic planner without evaluators")
            return self.strategic_planner
        except Exception as minimal_init_error:
            logging.error(f"Could not create even a minimal strategic planner: {minimal_init_error}")
            self.strategic_planner = None
        
        return None

    def initialize_day_night_cycle(self):
        """Initialize the day/night cycle state and tracking."""
        # Start with neither day nor night
        self.day_night_state = None
        # Track if we've already checked day/night transition this turn
        self.day_night_checked_this_turn = False
        logging.debug("Day/night cycle initialized (neither day nor night)")
    
    def _init_ability_handler(self):
        if hasattr(self, 'ability_handler') and self.ability_handler is not None:
            return
            
        try:
            from .ability_handler import AbilityHandler
            self.ability_handler = AbilityHandler(self)
            logging.debug("AbilityHandler initialized successfully")
            
            # Rest of the method remains the same
        except ImportError as e:
            logging.warning(f"AbilityHandler not available: {e}")
            self.ability_handler = None
        except TypeError as e:
            logging.error(f"Error initializing AbilityHandler: {e}")
            import traceback
            logging.error(traceback.format_exc())
            self.ability_handler = None
        
    
    def reset(self, p1_deck, p2_deck, seed=None):
        """Reset the game state with new decks and initialize all subsystems"""
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
                
        # Reset basic game state
        self.turn = 1
        self.phase = self.PHASE_UNTAP
        self.combat_damage_dealt = False
        self.stack = []
        self.priority_pass_count = 0
        self.last_stack_size = 0
        self._phase_action_count = 0
        self._phase_history = []
        self.initialize_day_night_cycle()
        self.cards_played = {0: [], 1: []}
        # Initialize player states
        self.p1 = self._init_player(p1_deck)
        self.p2 = self._init_player(p2_deck)
        # Add mulligan state flags
        self.mulligan_in_progress = True
        self.mulligan_player = self.p1 if self.agent_is_p1 else self.p2
        self.mulligan_count = {'p1': 0, 'p2': 0}
        # Reset combat state
        self.current_attackers = []
        self.current_block_assignments = {}
        self.current_spell_requires_target = False
        self.current_spell_card_id = None
        
        # Reset combat optimization variables
        self.optimal_attackers = None
        self.attack_suggestion_used = False
        self.mulligan_data = {'p1': 0, 'p2': 0}
        # Clear and re-initialize ability handler
        if hasattr(self, 'ability_handler') and self.ability_handler:
            self.ability_handler = None
        
        # Initialize ability handler AFTER player states are set up
        self._init_ability_handler()
        
        # Initialize rules systems and reset all tracking variables
        self._init_rules_systems()
        
        # Initialize turn tracking
        self.spells_cast_this_turn = []
        self.attackers_this_turn = set()
        self.damage_dealt_this_turn = {}
        self.cards_drawn_this_turn = {"p1": 0, "p2": 0}
        self.until_end_of_turn_effects = {}
        
        # Reset special card tracking
        self.adventure_cards = set()
        self.saga_counters = {}
        self.mdfc_cards = set()
        self.battle_cards = {}
        self.cards_castable_from_exile = set()
        self.cast_as_back_face = set()
        
        # Reset additional tracking variables
        self.temp_control_effects = {}
        self.phased_out = set() if hasattr(self, 'phased_out') else set()
        self.suspended_cards = {} if hasattr(self, 'suspended_cards') else {}
        self.kicked_cards = set() if hasattr(self, 'kicked_cards') else set()
        self.evoked_cards = set() if hasattr(self, 'evoked_cards') else set()
        self.foretold_cards = set() if hasattr(self, 'foretold_cards') else set()
        self.blitz_cards = set() if hasattr(self, 'blitz_cards') else set()
        self.dash_cards = set() if hasattr(self, 'dash_cards') else set()
        self.unearthed_cards = set() if hasattr(self, 'unearthed_cards') else set()
        self.jump_start_cards = set() if hasattr(self, 'jump_start_cards') else set()
        self.buyback_cards = set() if hasattr(self, 'buyback_cards') else set()
        self.flashback_cards = set() if hasattr(self, 'flashback_cards') else set()
        self.exile_at_end_of_combat = [] if hasattr(self, 'exile_at_end_of_combat') else []
        self.haste_until_eot = set() if hasattr(self, 'haste_until_eot') else set()
        self.has_haste_until_eot = set() if hasattr(self, 'has_haste_until_eot') else set()
        self.life_gained_this_turn = {} if hasattr(self, 'life_gained_this_turn') else {}
        self.damage_this_turn = {} if hasattr(self, 'damage_this_turn') else {}
        
        # Reset new tracking variables
        self.split_second_active = False if hasattr(self, 'split_second_active') else False
        self.rebounded_cards = {} if hasattr(self, 'rebounded_cards') else {}
        self.banding_creatures = set() if hasattr(self, 'banding_creatures') else set()
        self.crewed_vehicles = set() if hasattr(self, 'crewed_vehicles') else set()
        self.morphed_cards = {} if hasattr(self, 'morphed_cards') else {}
        self.cards_to_graveyard_this_turn = {self.turn: []} if hasattr(self, 'cards_to_graveyard_this_turn') else {self.turn: []}
        self.boast_activated = set() if hasattr(self, 'boast_activated') else set()
        self.forecast_used = set() if hasattr(self, 'forecast_used') else set()
        self.epic_spells = {} if hasattr(self, 'epic_spells') else {}
        self.city_blessing = {} if hasattr(self, 'city_blessing') else {}
        self.myriad_tokens = [] if hasattr(self, 'myriad_tokens') else []
        self.persist_returned = set() if hasattr(self, 'persist_returned') else set()
        self.undying_returned = set() if hasattr(self, 'undying_returned') else set()
        self.gravestorm_count = 0 if hasattr(self, 'gravestorm_count') else 0
        
        self.progress_was_forced = False
        self._turn_limit_checked = False
        
        # Initialize strategic planner after p1 and p2 are set up
        if hasattr(self, 'strategic_planner') and self.strategic_planner:
            if hasattr(self.strategic_planner, 'init_after_reset'):
                self.strategic_planner.init_after_reset()
        
        # Initialize ability handler turn tracking if available
        if hasattr(self, 'ability_handler') and self.ability_handler:
            if hasattr(self.ability_handler, 'initialize_turn_tracking'):
                self.ability_handler.initialize_turn_tracking()
        
        logging.debug("Game state reset. Starting new game.")
    
    def _init_player(self, deck):
        """Initialize a player's state with a given deck and draw 7 cards for the starting hand."""
        import copy
        
        if not deck:
            raise ValueError("Tried to initialize player with empty deck!")
        
        player = {
            "library": copy.deepcopy(deck),  # Use deep copy
            "hand": [],
            "battlefield": [],
            "graveyard": [],
            "exile": [],
            "life": 20,
            "mana_pool": {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0},
            "mana_production": {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0},
            "land_played": False,
            "tapped_permanents": set(),
            "damage_counters": {},
            "entered_battlefield_this_turn": set(),
            "activated_this_turn": set(),         # Planeswalkers activated this turn
            "pw_activations": {},                 # Track activations per planeswalker
            "name": "Player1" if self.agent_is_p1 else "Player2"  # Add name for debugging
        }
        random.shuffle(player["library"])

        for _ in range(7):
            if player["library"]:
                player["hand"].append(player["library"].pop(0))
            else:
                logging.warning("Not enough cards in the deck to draw 7 cards!")
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
        """Reset mana and untap all permanents at the beginning of turn."""
        player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        player["tapped_permanents"] = set()
        player["entered_battlefield_this_turn"] = set()
        player["land_played"] = False
        player["damage_counters"] = {}
        logging.debug("Untap Phase: Reset land_played, cleared tapped permanents, and updated mana.")

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
        original_controller = self._find_card_owner(card_id)
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
            current_controller = self._find_card_owner(card_id)
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
        
    def _find_card_owner(self, card_id):
        """Find the player who owns a card (different from controller)."""
        # In a simplified model, we can consider the controller to be the owner
        for player in [self.p1, self.p2]:
            for zone in ["battlefield", "hand", "graveyard", "exile", "library"]:
                if card_id in player[zone]:
                    return player
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
        """Enhanced priority handling with full APNAP (Active Player, Non-Active Player) support."""
        self.priority_pass_count += 1
        
        active_player = self._get_active_player()
        non_active_player = self._get_non_active_player()
        
        # Track who has priority
        if not hasattr(self, 'priority_player'):
            self.priority_player = active_player
        
        # First priority always goes to active player
        # When stack changes, priority goes back to active player
        if self.last_stack_size != len(self.stack):
            self.priority_player = active_player
            self.priority_pass_count = 0
            self.last_stack_size = len(self.stack)
            logging.debug(f"Stack changed: Priority passed to {active_player['name']}")
            
            # Check if the top spell has Split Second
            if self.stack:
                top_item = self.stack[-1]
                spell_type, card_id, caster = top_item
                card = self._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "split second" in card.oracle_text.lower():
                    self.split_second_active = True
                    logging.debug(f"Split Second active: {card.name} prevents responses")
            return
        
        # Switch priority between players
        current_priority_player = self.priority_player
        next_priority_player = non_active_player if current_priority_player == active_player else active_player
        self.priority_player = next_priority_player
        
        # Log the priority passing
        if self.stack:
            top_item = self.stack[-1]
            spell_type, card_id, caster = top_item
            card_name = self._safe_get_card(card_id).name if self._safe_get_card(card_id) else "Unknown"
            logging.debug(f"Priority passed from {current_priority_player['name']} to {next_priority_player['name']}. {card_name} on stack.")
        else:
            logging.debug(f"Priority passed from {current_priority_player['name']} to {next_priority_player['name']} with empty stack.")
        
        # Check if both players have passed in succession
        if self.priority_pass_count >= 2:
            if self.stack:
                # Process any triggered abilities before resolving stack
                if hasattr(self, 'ability_handler') and self.ability_handler:
                    self.ability_handler.process_triggered_abilities()
                
                # Resolve top of stack
                logging.debug(f"Both players passed priority. Resolving top of stack.")
                self.resolve_top_of_stack()  # Changed from self._resolve_top_of_stack()
                
                # Reset split_second_active after resolving the spell
                if hasattr(self, 'split_second_active') and self.split_second_active:
                    self.split_second_active = False
                    logging.debug("Split Second effect has ended")
                    
                # Reset priority count and give priority to active player
                self.priority_pass_count = 0
                self.priority_player = active_player
            else:
                # Move to next phase
                prev_phase = self.phase
                self._advance_phase()
                logging.debug(f"Both players passed with empty stack. Moving from {prev_phase} to {self.phase}")
                self.priority_pass_count = 0
                self.priority_player = active_player
    
    def _move_to_graveyard(self, player, card_id, source_zone):
        """
        Legacy method that has been deprecated. Use move_card instead.
        
        Args:
            player: The player who owns the card
            card_id: ID of the card to move
            source_zone: Source zone of the card
            
        Returns:
            bool: Whether the movement was successful
        """
        logging.warning("_move_to_graveyard is deprecated. Use move_card instead.")
        return self.move_card(card_id, player, source_zone, player, "graveyard")
    
    def move_card(self, card_id, from_player, from_zone, to_player, to_zone, cause=None):
        """
        Move a card between zones with proper tracking and zone transition rules.
        
        Args:
            card_id: ID of the card to move
            from_player: The player who currently owns the card
            from_zone: The current zone of the card
            to_player: The player who will own the card
            to_zone: The destination zone for the card
            cause: Optional cause for the movement (e.g., 'cast', 'destroy', 'exile')
        
        Returns:
            bool: Whether the movement was successful
        """
        # Validate the card exists in the source zone
        if from_zone not in from_player or card_id not in from_player[from_zone]:
            logging.warning(f"Card {card_id} not found in {from_zone}")
            return False
        
        # Get the card object for later use
        card = self._safe_get_card(card_id)
        card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
        
        # Handle any replacement effects before moving the card
        # Create an event context for potential replacement effects
        event_context = {
            'card_id': card_id,
            'from_player': from_player,
            'from_zone': from_zone,
            'to_player': to_player,
            'to_zone': to_zone,
            'cause': cause
        }
        
        # Apply replacement effects if applicable
        if from_zone == "battlefield" and to_zone == "graveyard":
            event_type = "DIES"
        elif from_zone != "battlefield" and to_zone == "battlefield":
            event_type = "ENTERS_BATTLEFIELD"
        elif to_zone == "exile":
            event_type = "EXILE"
        else:
            event_type = "ZONE_CHANGE"
        
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            modified_context, was_replaced = self.replacement_effects.apply_replacements(event_type, event_context)
            
            # Update variables if a replacement occurred
            if was_replaced:
                card_id = modified_context.get('card_id', card_id)
                from_player = modified_context.get('from_player', from_player)
                from_zone = modified_context.get('from_zone', from_zone)
                to_player = modified_context.get('to_player', to_player)
                to_zone = modified_context.get('to_zone', to_zone)
                
                # If the event was completely prevented, return
                if modified_context.get('prevented', False):
                    logging.debug(f"Movement of {card_name} from {from_zone} to {to_zone} was prevented by a replacement effect")
                    return True
        
        # Remove from source zone
        from_player[from_zone].remove(card_id)
        
        # Special handling when leaving battlefield
        if from_zone == "battlefield":
            # Remove any attachments
            if hasattr(from_player, "attachments"):
                # If this card was attached to something, remove the attachment
                if card_id in from_player["attachments"]:
                    del from_player["attachments"][card_id]
                
                # If other cards were attached to this card, remove those attachments
                attached_to_this = [
                    attachment_id for attachment_id, target_id in from_player["attachments"].items()
                    if target_id == card_id
                ]
                for attachment_id in attached_to_this:
                    del from_player["attachments"][attachment_id]
            
            # Remove from tracking sets
            if card_id in from_player.get("entered_battlefield_this_turn", set()):
                from_player["entered_battlefield_this_turn"].remove(card_id)
            
            if card_id in from_player.get("tapped_permanents", set()):
                from_player["tapped_permanents"].remove(card_id)
            
            # Remove temporary control effects
            if hasattr(self, "temp_control_effects") and card_id in self.temp_control_effects:
                del self.temp_control_effects[card_id]
            
            # Remove continuous effects from this source
            if hasattr(self, "layer_system") and self.layer_system:
                self.layer_system.remove_effects_by_source(card_id)
            
            if hasattr(self, "replacement_effects") and self.replacement_effects:
                self.replacement_effects.remove_effects_by_source(card_id)
                
            card = self._safe_get_card(card_id)
            if card and hasattr(card, 'is_room') and card.is_room:
                # Reset door unlock states - door1 always starts unlocked, door2 always starts locked
                if hasattr(card, 'door1'):
                    card.door1['unlocked'] = True
                if hasattr(card, 'door2'):
                    card.door2['unlocked'] = False
                    
            # Special handling for Classes - reset level when leaving battlefield
            if card and hasattr(card, 'is_class') and card.is_class:
                card.current_level = 1  # Reset to level 1
        
        # Add to destination zone
        to_player[to_zone].append(card_id)
        
        # Special handling when entering battlefield
        if to_zone == "battlefield":
            # Initialize tracking sets if they don't exist
            if "entered_battlefield_this_turn" not in to_player:
                to_player["entered_battlefield_this_turn"] = set()
            
            # Mark as having entered this turn
            to_player["entered_battlefield_this_turn"].add(card_id)
            
            # Register replacement and continuous effects from this card
            if hasattr(self, "replacement_effects") and self.replacement_effects:
                self.replacement_effects.register_card_replacement_effects(card_id, to_player)
            
            # Register any continuous effects from this card
            if hasattr(self, "layer_system") and self.layer_system:
                self.layer_system.register_common_effects()
        
        # Handle card type-specific rules
        self.handle_card_type_specific_rules(card_id, to_zone, to_player)
        
        # Handle token movement (tokens cease to exist when they leave the battlefield)
        if card and hasattr(card, 'is_token') and card.is_token and from_zone == "battlefield" and to_zone != "battlefield":
            # Remove token from destination zone immediately
            if card_id in to_player[to_zone]:
                to_player[to_zone].remove(card_id)
            
            # Remove from card database if appropriate
            if card_id in self.card_db:
                del self.card_db[card_id]
        
        # Trigger appropriate abilities
        if from_zone == "battlefield" and to_zone == "graveyard":
            # Card died
            self.trigger_ability(card_id, "DIES", {"from_battlefield": True})
        elif from_zone != "battlefield" and to_zone == "battlefield":
            # Card entered the battlefield
            self.trigger_ability(card_id, "ENTERS_BATTLEFIELD", {"controller": to_player})
        elif to_zone == "exile":
            # Card was exiled
            self.trigger_ability(card_id, "EXILED", {"from_zone": from_zone})
        elif from_zone != "hand" and to_zone == "hand":
            # Card returned to hand
            self.trigger_ability(card_id, "RETURNED_TO_HAND", {"from_zone": from_zone})
        
        logging.debug(f"Moved {card_name} from {from_player['name']}'s {from_zone} to {to_player['name']}'s {to_zone}")
        
        self.check_state_based_actions()
        
        return True
    
    def resolve_planeswalker(self, card_id, controller):
        """Handle resolving a planeswalker spell."""
        card = self._safe_get_card(card_id)
        if not card or 'planeswalker' not in card.card_types:
            return False
        
        # Make sure loyalty value is set
        if not hasattr(card, "loyalty"):
            if hasattr(card, "_init_planeswalker"):
                card._init_planeswalker(card.__dict__)
            else:
                # Default to loyalty 3 if method not available
                card.loyalty = 3
        
        # Add to battlefield
        controller["battlefield"].append(card_id)
        
        # Initialize loyalty counters dictionary if it doesn't exist
        if not hasattr(controller, "loyalty_counters"):
            controller["loyalty_counters"] = {}
        
        controller["loyalty_counters"][card_id] = card.loyalty
        
        # Track that it entered this turn (for "summoning sickness" equivalent)
        controller["entered_battlefield_this_turn"].add(card_id)
        
        # Trigger "enters the battlefield" abilities
        self.trigger_ability(card_id, "ENTERS_BATTLEFIELD")
        self.check_state_based_actions()
        logging.debug(f"Planeswalker {card.name} entered with {card.loyalty} loyalty")
        return True
    

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
        target_controller = self._find_card_owner(target_card_id)
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
        """Activate a planeswalker loyalty ability with enhanced ruleset support."""
        card = self._safe_get_card(card_id)
        if not card:
            return False
            
        # Check if card is a planeswalker
        if not hasattr(card, 'card_types') or 'planeswalker' not in card.card_types:
            # Try to detect planeswalker from type line
            if hasattr(card, 'type_line') and 'planeswalker' in card.type_line.lower():
                card.card_types = getattr(card, 'card_types', []) + ['planeswalker']
            else:
                return False
        
        # Initialize the planeswalker if needed
        if not hasattr(card, 'loyalty_abilities') or not card.loyalty_abilities:
            if hasattr(card, '_init_planeswalker'):
                card._init_planeswalker(card.__dict__)
            else:
                # Default to starting loyalty 3 if method not available
                card.loyalty = 3
                card.loyalty_abilities = []
        
        # Check for planeswalker activation tracking
        if not hasattr(controller, "activated_this_turn"):
            controller["activated_this_turn"] = set()
                
        if card_id in controller["activated_this_turn"]:
            logging.debug(f"Planeswalker {card.name} already activated this turn")
            return -0.5  # Return negative value as penalty
                
        # Check if ability index is valid
        if not hasattr(card, 'loyalty_abilities'):
            logging.warning(f"Cannot activate {card.name}: no loyalty abilities defined")
            return False
        
        if ability_idx >= len(card.loyalty_abilities):
            logging.warning(f"Invalid ability index {ability_idx} for {card.name}")
            return False
        
        ability = card.loyalty_abilities[ability_idx]
        cost = ability["cost"]
        
        # Check if enough loyalty
        current_loyalty = 0
        if hasattr(controller, "loyalty_counters"):
            current_loyalty = controller["loyalty_counters"].get(card_id, 0)
        else:
            # Initialize loyalty counters dictionary if it doesn't exist
            controller["loyalty_counters"] = {}
            current_loyalty = getattr(card, 'loyalty', 0)
            controller["loyalty_counters"][card_id] = current_loyalty
            
        if current_loyalty + cost < 0:
            logging.debug(f"Not enough loyalty to activate {card.name}'s ability (current: {current_loyalty}, cost: {cost})")
            return False
        
        # Pay cost
        controller["loyalty_counters"][card_id] = current_loyalty + cost
        
        # Mark as activated
        controller["activated_this_turn"].add(card_id)
        
        # Process effect
        effect = ability["effect"]
        logging.debug(f"Activating {card.name}'s loyalty ability: {effect}")
        
        # Process effect based on text patterns
        self._process_planeswalker_ability_effect(card_id, controller, effect)
        
        # Add ability to stack
        self.stack.append(("PLANESWALKER_ABILITY", card_id, controller, {"ability_index": ability_idx}))
        
        # Reset priority
        self.priority_pass_count = 0
        self.last_stack_size = len(self.stack)
        self.phase = self.PHASE_PRIORITY
        
        # Check state-based actions after resolution
        self.check_state_based_actions()
        
        return True
    
    def _process_planeswalker_ability_effect(self, card_id, controller, effect_text):
        """
        Process planeswalker ability effects based on text patterns.
        
        Args:
            card_id: The ID of the planeswalker
            controller: The player controlling the planeswalker
            effect_text: The text of the ability effect
        """
        # First check if we have an ability handler
        if hasattr(self, 'ability_handler') and self.ability_handler:
            # Try to use the ability handler to process the effect
            try:
                ability_context = {
                    "source_id": card_id,
                    "controller": controller,
                    "effect_text": effect_text,
                    "is_planeswalker_ability": True
                }
                
                # Check if ability handler has a parse effect text function
                if hasattr(self.ability_handler, 'parse_effect_text'):
                    effects = self.ability_handler.parse_effect_text(effect_text, ability_context)
                    for effect in effects:
                        effect.apply(self, card_id, controller, ability_context)
                    return
            except Exception as e:
                logging.error(f"Error processing planeswalker ability with ability handler: {str(e)}")
                # Fall through to default handling
        
        # Default handling for common planeswalker effects
        effect_text = effect_text.lower() if isinstance(effect_text, str) else ""
        
        # Parse common effects based on text patterns
        if "draw" in effect_text and "card" in effect_text:
            # Draw cards effect
            import re
            match = re.search(r"draw (\w+) cards?", effect_text)
            count = 1  # Default
            if match:
                count_word = match.group(1)
                if count_word.isdigit():
                    count = int(count_word)
                elif count_word == "a":
                    count = 1
                elif count_word == "two":
                    count = 2
                elif count_word == "three":
                    count = 3
                    
            for _ in range(count):
                if controller["library"]:
                    controller["hand"].append(controller["library"].pop(0))
            
            logging.debug(f"Planeswalker ability: Drew {count} cards")
        
        elif "damage" in effect_text:
            # Damage effect
            import re
            match = re.search(r"(\d+) damage", effect_text)
            damage = 1  # Default
            if match:
                damage = int(match.group(1))
                
            # Determine target
            opponent = self.p2 if controller == self.p1 else self.p1
            
            if "to target player" in effect_text or "to any target" in effect_text:
                # Damage to opponent
                opponent["life"] -= damage
                logging.debug(f"Planeswalker ability: Dealt {damage} damage to opponent")
                
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
                    if hasattr(target_card, 'toughness') and target_card.toughness <= damage:
                        self.move_card(target, opponent, "battlefield", opponent, "graveyard")
                        logging.debug(f"Planeswalker ability: Killed {target_card.name} with {damage} damage")
                    else:
                        # Add damage counter
                        if "damage_counters" not in opponent:
                            opponent["damage_counters"] = {}
                        opponent["damage_counters"][target] = opponent["damage_counters"].get(target, 0) + damage
                        logging.debug(f"Planeswalker ability: Dealt {damage} damage to {target_card.name}")
        
        elif "gain" in effect_text and "life" in effect_text:
            # Life gain effect
            import re
            match = re.search(r"gain (\d+) life", effect_text)
            life_gain = 1  # Default
            if match:
                life_gain = int(match.group(1))
                
            controller["life"] += life_gain
            logging.debug(f"Planeswalker ability: Gained {life_gain} life")
        
        elif "create" in effect_text and "token" in effect_text:
            # Token creation effect
            import re
            match = re.search(r"create (?:a|an|\d+) (.*?) token", effect_text)
            if match:
                token_desc = match.group(1)
                
                # Parse token details
                power, toughness = 1, 1
                pt_match = re.search(r"(\d+)/(\d+)", token_desc)
                if pt_match:
                    power = int(pt_match.group(1))
                    toughness = int(pt_match.group(2))
                
                # Create token data
                token_data = {
                    "name": f"{token_desc.title()} Token",
                    "power": power,
                    "toughness": toughness,
                    "card_types": ["creature"],
                    "subtypes": [],
                    "oracle_text": ""
                }
                
                # Add specific token abilities
                if "flying" in token_desc:
                    token_data["oracle_text"] += "Flying\n"
                if "vigilance" in token_desc:
                    token_data["oracle_text"] += "Vigilance\n"
                    
                # Create the token
                if hasattr(self, 'create_token'):
                    self.create_token(controller, token_data)
                    logging.debug(f"Planeswalker ability: Created a {token_desc} token")
        
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
                    logging.debug(f"Planeswalker ability: Exiled {target_card.name}")
    
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
        
    # In game_state.py, update the add_to_stack method
    def add_to_stack(self, item_type, card_id, controller, targets=None):
        """Add an item to the stack with proper targeting"""
        # For backward compatibility, add as tuple format
        stack_item = (item_type, card_id, controller)
        
        self.stack.append(stack_item)
        logging.debug(f"Added to stack: {self._safe_get_card(card_id).name}")
        
        # Reset priority system for new stack item
        self.priority_pass_count = 0
        self.last_stack_size = len(self.stack)
        self.phase = self.PHASE_PRIORITY  # Enter priority phase
        
        return True
    
    def cast_spell(self, card_id, player, targets=None, additional_costs=None):
        """
        Cast a spell from hand to the stack.
        
        Args:
            card_id: ID of the spell card to cast
            player: Player dictionary of the player casting the spell
            targets: Dictionary of targets for the spell
            additional_costs: Dictionary of additional costs (kicker, etc.)
            
        Returns:
            bool: Whether the spell was successfully cast
        """
        # Check if card exists in hand
        if card_id not in player["hand"]:
            logging.warning(f"Spell {card_id} not found in hand")
            return False
        
        # Check if it's a valid phase to cast spells
        if not self._can_cast_now(card_id, player):
            logging.warning(f"Cannot cast spell during current phase/stack state")
            return False
        
        # Get the card object
        card = self._safe_get_card(card_id)
        if not card:
            logging.warning(f"Invalid card ID: {card_id}")
            return False
        
        # Calculate mana cost
        mana_cost = {}
        if hasattr(self, 'mana_system') and hasattr(card, 'mana_cost'):
            mana_cost = self.mana_system.parse_mana_cost(card.mana_cost)
        
        # Handle additional costs
        context = {
            "targets": targets or {},
            "additional_costs": additional_costs or {}
        }
        
        # Check if player can pay the cost
        if hasattr(self, 'mana_system') and not self.mana_system.can_pay_mana_cost(player, mana_cost):
            logging.warning(f"Cannot pay mana cost for {card.name}")
            return False
        
        # Pay mana cost
        if hasattr(self, 'mana_system'):
            self.mana_system.pay_mana_cost(player, mana_cost)
        
        # Move spell from hand to stack
        player["hand"].remove(card_id)
        self.stack.append(("SPELL", card_id, player, context))
        
        # Track spell cast for triggers
        if not hasattr(self, 'spells_cast_this_turn'):
            self.spells_cast_this_turn = []
        self.spells_cast_this_turn.append((card_id, player))
        
        # Track the card for statistics
        player_idx = 0 if player == self.p1 else 1
        self.track_card_played(card_id, player_idx)
        
        # Handle cast triggers
        self.handle_cast_trigger(card_id, player, context)
        
        # Reset priority system for new stack item
        self.priority_pass_count = 0
        self.last_stack_size = len(self.stack)
        self.phase = self.PHASE_PRIORITY  # Enter priority phase
        
        logging.debug(f"Cast spell: {card.name}")
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
        """
        Tap a permanent, triggering any appropriate abilities.
        
        Args:
            card_id: ID of the permanent to tap
            player: Player who controls the permanent
            
        Returns:
            bool: Whether the permanent was successfully tapped
        """
        # Check if the card is on the battlefield
        if card_id not in player["battlefield"]:
            logging.warning(f"Permanent {card_id} not found on battlefield")
            return False
        
        # Check if card is already tapped
        if "tapped_permanents" in player and card_id in player["tapped_permanents"]:
            logging.debug(f"Permanent {card_id} is already tapped")
            return True
        
        # Initialize tapped_permanents set if it doesn't exist
        if "tapped_permanents" not in player:
            player["tapped_permanents"] = set()
        
        # Tap the permanent
        player["tapped_permanents"].add(card_id)
        
        # Get the card object
        card = self._safe_get_card(card_id)
        if card:
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Tapped {card_name}")
        
        # Trigger any "when this becomes tapped" abilities
        self.trigger_ability(card_id, "BECOMES_TAPPED", {"controller": player})
        
        return True

    def untap_permanent(self, card_id, player):
        """
        Untap a permanent, triggering any appropriate abilities.
        
        Args:
            card_id: ID of the permanent to untap
            player: Player who controls the permanent
            
        Returns:
            bool: Whether the permanent was successfully untapped
        """
        # Check if the card is on the battlefield
        if card_id not in player["battlefield"]:
            logging.warning(f"Permanent {card_id} not found on battlefield")
            return False
        
        # Check if card is already untapped
        if "tapped_permanents" not in player or card_id not in player["tapped_permanents"]:
            logging.debug(f"Permanent {card_id} is already untapped")
            return True
        
        # Untap the permanent
        player["tapped_permanents"].remove(card_id)
        
        # Get the card object
        card = self._safe_get_card(card_id)
        if card:
            card_name = card.name if hasattr(card, 'name') else f"Card {card_id}"
            logging.debug(f"Untapped {card_name}")
        
        # Trigger any "when this becomes untapped" abilities
        self.trigger_ability(card_id, "BECOMES_UNTAPPED", {"controller": player})
        
        return True

    def resolve_top_of_stack(self):
        """
        Resolve the top item of the stack with comprehensive handling for all card types and effects.
        """
        if not self.stack:
            return False
            
        # Get the top item of the stack
        top_item = self.stack.pop()
        
        try:
            # Different handling based on item type
            if isinstance(top_item, tuple) and len(top_item) >= 3:
                item_type, item_id, controller = top_item[:3]
                context = top_item[3] if len(top_item) > 3 else {}
                
                logging.debug(f"Resolving stack item: {item_type} {item_id}")
                
                # Check if this spell had split second
                card = self._safe_get_card(item_id)
                had_split_second = False
                if card and hasattr(card, 'oracle_text') and "split second" in card.oracle_text.lower():
                    had_split_second = True
                
                # Resolve based on item type
                if item_type == "SPELL":
                    # Check if it's a Spree spell
                    spell = self._safe_get_card(item_id)
                    if spell and hasattr(spell, 'is_spree') and spell.is_spree and context.get("is_spree", False):
                        self._resolve_spree_spell(item_id, controller, context)
                    else:
                        # Regular spell resolution
                        self._resolve_spell(item_id, controller, context)
                elif item_type == "ABILITY":
                    self._resolve_ability(item_id, controller, context)
                elif item_type == "TRIGGER":
                    self._resolve_triggered_ability(item_id, controller, context)
                else:
                    logging.warning(f"Unknown stack item type: {item_type}")
                    return False
                    
                # Turn off split second if this spell had it
                if had_split_second and hasattr(self, 'split_second_active') and self.split_second_active:
                    self.split_second_active = False
                    logging.debug(f"Split Second: Effect ended after {card.name if card else 'spell'} resolved")
                
                # Check state-based actions after resolution
                self.check_state_based_actions()
                
                # Process any triggered abilities that might have occurred during resolution
                if hasattr(self, 'ability_handler') and self.ability_handler:
                    self.ability_handler.process_triggered_abilities()
                    
                return True
            else:
                logging.warning(f"Invalid stack item format: {top_item}")
                return False
        except Exception as e:
            logging.error(f"Error resolving stack item: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
            # Turn off split second if there was an error
            if hasattr(self, 'split_second_active') and self.split_second_active:
                self.split_second_active = False
                logging.debug("Split Second: Effect ended due to resolution error")
                
            return False
        
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

    def _create_token_copy(self, original_card, controller):
        """Create a token copy of a card."""
        # Create token tracking if it doesn't exist
        if not hasattr(controller, "tokens"):
            controller["tokens"] = []
        
        token_id = f"TOKEN_COPY_{len(controller['tokens'])}"
        
        # Create token data based on original card
        token_data = {}
        for attr in ['name', 'type_line', 'card_types', 'subtypes', 'power', 'toughness', 
                    'oracle_text', 'keywords', 'colors', 'cmc']:
            if hasattr(original_card, attr):
                token_data[attr] = getattr(original_card, attr)
        
        # Add "Copy" to name
        if 'name' in token_data:
            token_data['name'] = f"{token_data['name']} Copy"
        
        # Create the token
        from .card import Card
        token = Card(token_data)
        
        # Add to game
        self.card_db[token_id] = token
        controller["battlefield"].append(token_id)
        controller["tokens"].append(token_id)
        
        # Mark as entered this turn (summoning sickness for creatures)
        if 'creature' in token_data.get('card_types', []):
            if "entered_battlefield_this_turn" not in controller:
                controller["entered_battlefield_this_turn"] = set()
            controller["entered_battlefield_this_turn"].add(token_id)
        
        logging.debug(f"Created token copy of {original_card.name}")
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
        """
        Resolve a creature spell - put it onto the battlefield and handle ETB effects.
        
        Args:
            spell_id: The ID of the creature spell
            controller: The player casting the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # If this is a copy, we need to create a token copy instead
        if context.get("is_copy", False):
            self._create_token_copy(spell, controller)
            return
            
        # Put the creature onto the battlefield
        controller["battlefield"].append(spell_id)
        
        # Mark as having summoning sickness
        if "entered_battlefield_this_turn" not in controller:
            controller["entered_battlefield_this_turn"] = set()
        controller["entered_battlefield_this_turn"].add(spell_id)
        
        # Trigger enters-the-battlefield abilities
        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
        
        # Handle any replacement effects
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            self.replacement_effects.apply_replacements("ENTERS_BATTLEFIELD", {
                "card_id": spell_id,
                "controller": controller
            })

    def _resolve_planeswalker_spell(self, spell_id, controller, context=None):
        """
        Resolve a planeswalker spell - put it onto the battlefield with loyalty counters.
        
        Args:
            spell_id: The ID of the planeswalker spell
            controller: The player casting the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # If this is a copy, we need to create a token copy instead
        if context.get("is_copy", False):
            self._create_token_copy(spell, controller)
            return
            
        # Make sure spell is recognized as a planeswalker
        if not hasattr(spell, 'card_types') or 'planeswalker' not in spell.card_types:
            if hasattr(spell, 'type_line') and 'planeswalker' in spell.type_line.lower():
                spell.card_types = getattr(spell, 'card_types', []) + ['planeswalker']
                
        # Initialize the planeswalker if needed
        if not hasattr(spell, 'loyalty') or not hasattr(spell, 'loyalty_abilities'):
            if hasattr(spell, '_init_planeswalker'):
                spell._init_planeswalker(spell.__dict__)
                
        # Put the planeswalker onto the battlefield
        controller["battlefield"].append(spell_id)
        
        # Add loyalty counters
        loyalty = getattr(spell, 'loyalty', 3)  # Default to 3 if not specified
        if not hasattr(controller, "loyalty_counters"):
            controller["loyalty_counters"] = {}
        controller["loyalty_counters"][spell_id] = loyalty
        
        # Track that it entered this turn (for "summoning sickness" equivalent)
        if "entered_battlefield_this_turn" not in controller:
            controller["entered_battlefield_this_turn"] = set()
        controller["entered_battlefield_this_turn"].add(spell_id)
        
        # Trigger enters-the-battlefield abilities
        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})
        
        # Apply the "planeswalker uniqueness rule"
        self._apply_planeswalker_uniqueness_rule(controller)
        
        logging.debug(f"Planeswalker {spell.name if hasattr(spell, 'name') else 'Unknown'} entered with {loyalty} loyalty")
        return True

    def _resolve_permanent_spell(self, spell_id, controller, context=None):
        """
        Resolve an artifact or enchantment spell - put it onto the battlefield.
        
        Args:
            spell_id: The ID of the artifact or enchantment spell
            controller: The player casting the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # If this is a copy, we need to create a token copy instead
        if context.get("is_copy", False):
            self._create_token_copy(spell, controller)
            return
            
        # Put the permanent onto the battlefield
        controller["battlefield"].append(spell_id)
        
        # Handle auras specifically
        if hasattr(spell, 'subtypes') and 'aura' in spell.subtypes:
            self._resolve_aura_attachment(spell_id, controller, context)
        
        # Trigger enters-the-battlefield abilities
        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})

    def _resolve_land_spell(self, spell_id, controller, context=None):
        """
        Resolve a land - put it onto the battlefield and handle special land effects.
        
        Args:
            spell_id: The ID of the land
            controller: The player playing the land
            context: Additional land context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # Put the land onto the battlefield
        controller["battlefield"].append(spell_id)
        
        # Mark that a land has been played this turn
        controller["land_played"] = True
        
        # Check if land enters tapped
        if hasattr(spell, 'oracle_text') and "enters the battlefield tapped" in spell.oracle_text.lower():
            if "tapped_permanents" not in controller:
                controller["tapped_permanents"] = set()
            controller["tapped_permanents"].add(spell_id)
        
        # Trigger enters-the-battlefield abilities
        self.trigger_ability(spell_id, "ENTERS_BATTLEFIELD", {"controller": controller})

    def _resolve_instant_sorcery_spell(self, spell_id, controller, context=None):
        """
        Resolve an instant or sorcery spell - apply its effects and put it in the graveyard.
        
        Args:
            spell_id: The ID of the instant or sorcery spell
            controller: The player casting the spell
            context: Additional spell context
        """
        if context is None:
            context = {}
            
        spell = self._safe_get_card(spell_id)
        if not spell:
            return
            
        # Handle targeting if needed
        targets = context.get("targets")
        if not targets and hasattr(self, 'targeting_system'):
            targets = self.targeting_system.resolve_targeting_for_spell(spell_id, controller)
            
        # Apply spell effects
        self.resolve_spell_effects(spell_id, controller, targets, context)
        
        # If this is a copy, don't move it to the graveyard
        if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
            controller["graveyard"].append(spell_id)
            
    def _word_to_number(self, word):
        """Convert word representation of number to int."""
        from .ability_utils import text_to_number
        return text_to_number(word)
    
    def _advance_phase(self):
        """Advance to the next phase in the turn sequence with improved progress detection and handling."""
        # Call our progress monitoring function
        progress_forced = self._check_phase_progress()
        if progress_forced:
            # Signal that progress was forced (this can be used to apply a penalty in the environment)
            self.progress_was_forced = True
            return
        
        phase_sequence = [
            self.PHASE_UNTAP,
            self.PHASE_UPKEEP,
            self.PHASE_DRAW,
            self.PHASE_MAIN_PRECOMBAT,
            self.PHASE_BEGIN_COMBAT,  # Beginning of combat step
            self.PHASE_DECLARE_ATTACKERS,
            self.PHASE_DECLARE_BLOCKERS,
            self.PHASE_FIRST_STRIKE_DAMAGE,  # First strike damage step
            self.PHASE_COMBAT_DAMAGE,        # Regular combat damage
            self.PHASE_END_OF_COMBAT,        # End of combat step
            self.PHASE_MAIN_POSTCOMBAT,
            self.PHASE_END_STEP,
            self.PHASE_CLEANUP               # Cleanup step
        ]
        
        # Phase names for better logging
        phase_names = {
            self.PHASE_UNTAP: "UNTAP",
            self.PHASE_UPKEEP: "UPKEEP",
            self.PHASE_DRAW: "DRAW",
            self.PHASE_MAIN_PRECOMBAT: "MAIN_PRECOMBAT",
            self.PHASE_BEGIN_COMBAT: "BEGINNING_OF_COMBAT",
            self.PHASE_DECLARE_ATTACKERS: "DECLARE_ATTACKERS",
            self.PHASE_DECLARE_BLOCKERS: "DECLARE_BLOCKERS",
            self.PHASE_FIRST_STRIKE_DAMAGE: "FIRST_STRIKE_DAMAGE",
            self.PHASE_COMBAT_DAMAGE: "COMBAT_DAMAGE",
            self.PHASE_END_OF_COMBAT: "END_OF_COMBAT",
            self.PHASE_MAIN_POSTCOMBAT: "MAIN_POSTCOMBAT",
            self.PHASE_END_STEP: "END_STEP",
            self.PHASE_PRIORITY: "PRIORITY",
            self.PHASE_TARGETING: "TARGETING",
            self.PHASE_CLEANUP: "CLEANUP"
        }
        
        old_phase = self.phase
        old_phase_name = phase_names.get(old_phase, f"UNKNOWN({old_phase})")
        
        # Special case for PHASE_PRIORITY (11)
        if self.phase == self.PHASE_PRIORITY:
            # If stack is empty, return to previous phase
            if not self.stack:
                # Find the previous phase from which priority was called
                previous_phase = getattr(self, 'previous_priority_phase', self.PHASE_MAIN_PRECOMBAT)
                self.phase = previous_phase
                new_phase_name = phase_names.get(self.phase, f"UNKNOWN({self.phase})")
                logging.debug(f"Returning from PRIORITY phase to {new_phase_name}")
                self._phase_action_count = 0
                return
            # Otherwise stay in PRIORITY until stack resolves
            return
        
        # Handle direct transition from END_STEP to CLEANUP
        if self.phase == self.PHASE_END_STEP:
            # Process end of turn triggers
            active_player = self._get_active_player()
            
            # Check for day/night cycle transitions at end step
            if hasattr(self, 'day_night_state'):
                self.check_day_night_transition()
            
            # Check for end step triggers from all permanents
            for player in [self._get_active_player(), self._get_non_active_player()]:
                for card_id in player["battlefield"]:
                    # Skip cards that entered during end step (they trigger next turn)
                    if card_id in player.get("skip_end_step_trigger", set()):
                        continue
                        
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'oracle_text'):
                        continue
                        
                    if "at the beginning of the end step" in card.oracle_text.lower():
                        # Trigger the ability
                        self.trigger_ability(card_id, "END_STEP", {"controller": player})
            
            # Go directly to cleanup
            self.phase = self.PHASE_CLEANUP
            logging.debug(f"Direct transition from END_STEP to CLEANUP")
            
            # Perform cleanup actions
            active_player = self._get_active_player()
            self._end_phase(active_player)
            
            self._phase_action_count = 0
            return
        
        # Check for first strike damage step
        if self.phase == self.PHASE_DECLARE_BLOCKERS:
            has_first_or_double_strike = False
            
            # Check attackers for first/double strike
            for attacker_id in self.current_attackers:
                attacker = self._safe_get_card(attacker_id)
                if not attacker:
                    continue
                    
                # Check keywords array for first/double strike
                if hasattr(attacker, 'keywords') and len(attacker.keywords) > 3:
                    if attacker.keywords[1] or attacker.keywords[3]:  # First strike or double strike
                        has_first_or_double_strike = True
                        break
                        
                # Also check oracle text for first/double strike
                if hasattr(attacker, 'oracle_text') and any(ks in attacker.oracle_text.lower() 
                                                        for ks in ["first strike", "double strike"]):
                    has_first_or_double_strike = True
                    break
            
            # Also check blockers for first/double strike
            if not has_first_or_double_strike:
                for blockers in self.current_block_assignments.values():
                    for blocker_id in blockers:
                        blocker = self._safe_get_card(blocker_id)
                        if not blocker:
                            continue
                            
                        # Check keywords array for first/double strike
                        if hasattr(blocker, 'keywords') and len(blocker.keywords) > 3:
                            if blocker.keywords[1] or blocker.keywords[3]:  # First strike or double strike
                                has_first_or_double_strike = True
                                break
                                
                        # Also check oracle text for first/double strike
                        if hasattr(blocker, 'oracle_text') and any(ks in blocker.oracle_text.lower() 
                                                                for ks in ["first strike", "double strike"]):
                            has_first_or_double_strike = True
                            break
                            
                    if has_first_or_double_strike:
                        break
            
            # Skip first strike damage if no first/double strike creatures involved
            if not has_first_or_double_strike:
                # Find the indices to skip past first strike damage
                try:
                    current_idx = phase_sequence.index(self.phase)
                    first_strike_idx = phase_sequence.index(self.PHASE_FIRST_STRIKE_DAMAGE)
                    combat_damage_idx = phase_sequence.index(self.PHASE_COMBAT_DAMAGE)
                    
                    # Store current phase before changing it (for priority system)
                    self.previous_priority_phase = self.phase
                    
                    # Jump directly to combat damage
                    self.phase = phase_sequence[combat_damage_idx]
                    new_phase_name = phase_names.get(self.phase, f"UNKNOWN({self.phase})")
                    logging.debug(f"Skipping FIRST_STRIKE_DAMAGE phase (no first strike creatures)")
                    
                    # Reset phase action counter
                    self._phase_action_count = 0
                    return
                except ValueError:
                    # Fallback if phase not found
                    logging.error(f"Could not find phase in sequence: {self.phase}")
        
        try:
            current_idx = phase_sequence.index(self.phase)
            next_idx = (current_idx + 1) % len(phase_sequence)
            
            # Handle turn transition (CLEANUP to UNTAP)
            if self.phase == self.PHASE_CLEANUP and next_idx == 0:
                prev_turn = self.turn
                self.turn += 1
                self.combat_damage_dealt = False  # Reset for new turn
                
                # Reset day/night transition check for the new turn
                if hasattr(self, 'day_night_checked_this_turn'):
                    self.day_night_checked_this_turn = False
                
                # Reset turn-based tracking for both players
                for player in [self.p1, self.p2]:
                    player["land_played"] = False
                    player["entered_battlefield_this_turn"] = set()
                    
                    # Reset player-specific turn tracking
                    if hasattr(player, "activated_this_turn"):
                        player["activated_this_turn"] = set()
                        
                    if hasattr(player, "skip_end_step_trigger"):
                        player["skip_end_step_trigger"] = set()
                
                # Reset game-state tracking
                self.spells_cast_this_turn = []
                self.attackers_this_turn = set()
                self.damage_dealt_this_turn = {}
                self.cards_drawn_this_turn = {"p1": 0, "p2": 0}
                
                # Clear effects that say "this turn"
                self.until_end_of_turn_effects = {}
                
                # Reset ability handler tracking
                if hasattr(self, 'ability_handler') and self.ability_handler:
                    if hasattr(self.ability_handler, 'initialize_turn_tracking'):
                        self.ability_handler.initialize_turn_tracking()
                
                logging.info(f"=== ADVANCING FROM TURN {prev_turn} TO TURN {self.turn} ===")
                
                # Check if turn limit exceeded
                if self.turn > self.max_turns:
                    logging.info(f"Turn limit reached! Current turn: {self.turn}, Max turns: {self.max_turns}")
                    # Set game end flags based on life totals
                    if self.p1["life"] > self.p2["life"]:
                        self.p1["won_game"] = True
                        self.p2["lost_game"] = True
                    elif self.p2["life"] > self.p1["life"]:
                        self.p2["won_game"] = True
                        self.p1["lost_game"] = True
                    else:
                        # Draw
                        self.p1["game_draw"] = True
                        self.p2["game_draw"] = True
                    
                    # Force state-based actions check
                    self.check_state_based_actions()
            
            # Store current phase before changing it (for priority system)
            self.previous_priority_phase = self.phase
            
            # Move to next phase
            self.phase = phase_sequence[next_idx]
            new_phase_name = phase_names.get(self.phase, f"UNKNOWN({self.phase})")
            logging.debug(f"Advancing from phase {old_phase_name} to {new_phase_name}")
            
            # Reset phase action counter
            self._phase_action_count = 0
            
            # Cleanup "until end of turn" effects during end step or cleanup
            if self.phase == self.PHASE_END_STEP or self.phase == self.PHASE_CLEANUP:
                if hasattr(self, 'layer_system') and self.layer_system:
                    self.layer_system.cleanup_expired_effects()
                if hasattr(self, 'replacement_effects') and self.replacement_effects:
                    self.replacement_effects.cleanup_expired_effects()
        
        except ValueError:
            logging.error(f"Current phase {old_phase_name} not found in sequence")
            # Fallback to a safe phase
            self.phase = self.PHASE_MAIN_PRECOMBAT
            self._phase_action_count = 0
            
            # Signal that progress was forced
            self.progress_was_forced = True
            
            logging.debug(f"Phase error - falling back to MAIN_PRECOMBAT")
    
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
        """
        Create a deep copy of the game state for lookahead simulation.
        
        Returns:
            GameState: A copy of the current game state
        """
        import copy
        
        # Create a new game state with the same parameters
        new_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)
        
        # Copy basic attributes
        new_state.turn = self.turn
        new_state.phase = self.phase
        new_state.agent_is_p1 = self.agent_is_p1
        new_state.combat_damage_dealt = self.combat_damage_dealt
        
        # Deep copy player states
        new_state.p1 = copy.deepcopy(self.p1)
        new_state.p2 = copy.deepcopy(self.p2)
        
        # Copy combat state
        new_state.current_attackers = self.current_attackers.copy()
        new_state.current_block_assignments = copy.deepcopy(self.current_block_assignments)
        
        # Copy stack
        new_state.stack = copy.deepcopy(self.stack)
        new_state.priority_pass_count = self.priority_pass_count
        new_state.last_stack_size = self.last_stack_size
        
        # Copy special card tracking
        new_state.adventure_cards = self.adventure_cards.copy() if hasattr(self, 'adventure_cards') else set()
        new_state.saga_counters = copy.deepcopy(self.saga_counters) if hasattr(self, 'saga_counters') else {}
        new_state.mdfc_cards = self.mdfc_cards.copy() if hasattr(self, 'mdfc_cards') else set()
        new_state.battle_cards = copy.deepcopy(self.battle_cards) if hasattr(self, 'battle_cards') else {}
        new_state.cards_castable_from_exile = self.cards_castable_from_exile.copy() if hasattr(self, 'cards_castable_from_exile') else set()
        new_state.cast_as_back_face = self.cast_as_back_face.copy() if hasattr(self, 'cast_as_back_face') else set()
        
        # Copy turn tracking
        new_state.spells_cast_this_turn = self.spells_cast_this_turn.copy() if hasattr(self, 'spells_cast_this_turn') else []
        new_state.attackers_this_turn = self.attackers_this_turn.copy() if hasattr(self, 'attackers_this_turn') else set()
        new_state.damage_dealt_this_turn = copy.deepcopy(self.damage_dealt_this_turn) if hasattr(self, 'damage_dealt_this_turn') else {}
        new_state.cards_drawn_this_turn = copy.deepcopy(self.cards_drawn_this_turn) if hasattr(self, 'cards_drawn_this_turn') else {"p1": 0, "p2": 0}
        new_state.until_end_of_turn_effects = copy.deepcopy(self.until_end_of_turn_effects) if hasattr(self, 'until_end_of_turn_effects') else {}
        
        # Copy subsystem references (to ensure they're initialized with the same objects)
        new_state.card_evaluator = self.card_evaluator if hasattr(self, 'card_evaluator') else None
        new_state.combat_resolver = self.combat_resolver if hasattr(self, 'combat_resolver') else None
        
        # Initialize ability handler for the cloned state
        new_state._init_ability_handler()
        
        # Initialize rules systems (layer system, replacement effects, mana system)
        new_state._init_rules_systems()
        
        # Initialize a new action handler for the copied state
        from .actions import ActionHandler
        new_state.action_handler = ActionHandler(new_state)
        
        # Copy strategy memory if available
        if hasattr(self, 'strategy_memory') and self.strategy_memory:
            new_state.strategy_memory = self.strategy_memory  # Usually just a reference is enough
        
        return new_state
        
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
        """
        Add counters to a permanent on the battlefield.
        
        Args:
            card_id: ID of the card to add counters to
            counter_type: Type of counter to add (+1/+1, -1/-1, loyalty, etc.)
            count: Number of counters to add (can be negative to remove)
            
        Returns:
            bool: True if operation succeeded
        """
        # Find the card owner
        card_owner = None
        for player in [self.p1, self.p2]:
            if card_id in player["battlefield"]:
                card_owner = player
                break
        
        if not card_owner:
            logging.warning(f"Cannot add counter to card {card_id} - not on battlefield")
            return False
        
        # Get the card
        card = self._safe_get_card(card_id)
        if not card:
            return False
        
        # Initialize counter tracking on the card
        if not hasattr(card, "counters"):
            card.counters = {}
        
        # Add or remove counters
        current_count = card.counters.get(counter_type, 0)
        new_count = max(0, current_count + count)  # Cannot go below 0
        card.counters[counter_type] = new_count
        
        # Apply counter effects
        if counter_type == "+1/+1":
            # Update power and toughness if creature
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                # Apply +1/+1 effects, but also handle potential -1/-1 counters
                plus_counters = card.counters.get("+1/+1", 0)
                minus_counters = card.counters.get("-1/-1", 0)
                
                # Apply the net change only
                if count > 0:
                    # We added +1/+1 counters
                    delta = count - min(count, minus_counters)
                    if delta > 0:
                        card.power += delta
                        card.toughness += delta
                    
                    # Remove -1/-1 counters if needed
                    removed_minus = min(count, minus_counters)
                    if removed_minus > 0:
                        card.counters["-1/-1"] -= removed_minus
                
                logging.debug(f"Added {count} +1/+1 counters to {card.name}, now has {new_count} +1/+1 counters")
        
        elif counter_type == "-1/-1":
            # Update power and toughness if creature
            if hasattr(card, 'card_types') and 'creature' in card.card_types:
                # Apply -1/-1 effects, but also handle potential +1/+1 counters
                plus_counters = card.counters.get("+1/+1", 0)
                minus_counters = card.counters.get("-1/-1", 0)
                
                # Apply the net change only
                if count > 0:
                    # We added -1/-1 counters
                    delta = count - min(count, plus_counters)
                    if delta > 0:
                        card.power = max(0, card.power - delta)
                        card.toughness = max(0, card.toughness - delta)
                    
                    # Remove +1/+1 counters if needed
                    removed_plus = min(count, plus_counters)
                    if removed_plus > 0:
                        card.counters["+1/+1"] -= removed_plus
                
                logging.debug(f"Added {count} -1/-1 counters to {card.name}, now has {new_count} -1/-1 counters")
        
        elif counter_type == "loyalty" and hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
            # Update loyalty tracker directly
            if not hasattr(card_owner, "loyalty_counters"):
                card_owner["loyalty_counters"] = {}
            
            card_owner["loyalty_counters"][card_id] = new_count
            logging.debug(f"Changed loyalty counters on {card.name} by {count}, now has {new_count} loyalty")
        
        # Trigger "whenever a counter is placed" abilities
        if count > 0:
            self.trigger_ability(card_id, "COUNTER_ADDED", {
                "counter_type": counter_type,
                "count": count
            })
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
        """Comprehensive state-based actions check following MTG rules 704."""
        actions_performed = False
        iteration_count = 0
        
        # State-based actions should be checked repeatedly until no more apply
        while iteration_count < 10:  # Safety limit to prevent infinite loops
            iteration_count += 1
            current_actions_performed = False
            
            # 704.5a Player with 0 or less life loses the game
            for player in [self.p1, self.p2]:
                if player["life"] <= 0 and not player.get("lost_game", False):
                    logging.debug(f"SBA: Player {player['name']} loses due to 0 or negative life")
                    player["lost_game"] = True
                    current_actions_performed = True
            
            # 704.5b Player who attempted to draw from an empty library loses the game
            for player in [self.p1, self.p2]:
                if player.get("attempted_draw_from_empty", False) and not player.get("lost_game", False):
                    logging.debug(f"SBA: Player {player['name']} loses due to drawing from empty library")
                    player["lost_game"] = True
                    current_actions_performed = True
            
            # 704.5c Player with 10 or more poison counters loses the game
            for player in [self.p1, self.p2]:
                if player.get("poison_counters", 0) >= 10 and not player.get("lost_game", False):
                    player["lost_game"] = True
                    logging.debug(f"SBA: Player {player['name']} loses due to 10+ poison counters")
                    current_actions_performed = True
            
            # Turn limit check - forcing game end if turn limit exceeded
            if self.turn > self.max_turns and not getattr(self, '_turn_limit_checked', False):
                logging.debug(f"SBA: Turn limit of {self.max_turns} exceeded, ending game")
                self._turn_limit_checked = True
                
                # Determine winner by life total
                if self.p1["life"] > self.p2["life"]:
                    self.p1["won_game"] = True
                    self.p2["lost_game"] = True
                    logging.debug(f"SBA: Player 1 wins with {self.p1['life']} life vs {self.p2['life']}")
                elif self.p2["life"] > self.p1["life"]:
                    self.p2["won_game"] = True
                    self.p1["lost_game"] = True
                    logging.debug(f"SBA: Player 2 wins with {self.p2['life']} life vs {self.p1['life']}")
                else:
                    # Draw
                    self.p1["game_draw"] = True
                    self.p2["game_draw"] = True
                    logging.debug(f"SBA: Game ended in a draw at {self.p1['life']} life")
                
                current_actions_performed = True
            
            # 704.5d Creature with toughness ≤ 0 is put into owner's graveyard
            for player in [self.p1, self.p2]:
                dead_creatures = []
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'creature' not in card.card_types:
                        continue
                    
                    # Calculate effective toughness including -1/-1 counters
                    base_toughness = card.toughness if hasattr(card, 'toughness') else 0
                    minus_counters = 0
                    if hasattr(card, 'counters'):
                        minus_counters = card.counters.get("-1/-1", 0)
                    
                    # Check for 0 or negative toughness (separate from damage)
                    if base_toughness - minus_counters <= 0:
                        dead_creatures.append(card_id)
                        logging.debug(f"SBA: Creature {card.name} died from 0 or negative toughness due to -1/-1 counters")
                
                # Process removals
                for card_id in dead_creatures:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        self.trigger_ability(card_id, "DIES")
                        current_actions_performed = True
            
            # 704.5e Planeswalker with 0 loyalty is put into owner's graveyard
            for player in [self.p1, self.p2]:
                dead_planeswalkers = []
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'planeswalker' not in card.card_types:
                        continue
                    
                    if not hasattr(player, "loyalty_counters"):
                        player["loyalty_counters"] = {}
                    
                    loyalty = player["loyalty_counters"].get(card_id, 0)
                    if loyalty <= 0:
                        dead_planeswalkers.append(card_id)
                
                # Process removals
                for card_id in dead_planeswalkers:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        current_actions_performed = True
                        logging.debug(f"SBA: Planeswalker {self._safe_get_card(card_id).name} died from 0 loyalty")
            
            # 704.5f/h Creature with lethal damage (with regeneration and totem armor consideration)
            for player in [self.p1, self.p2]:
                dead_creatures = []
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'creature' not in card.card_types:
                        continue
                    
                    # Check for lethal damage
                    damage = player.get("damage_counters", {}).get(card_id, 0)
                    
                    # Calculate current toughness including counters
                    base_toughness = card.toughness if hasattr(card, 'toughness') else 0
                    plus_counters = 0
                    minus_counters = 0
                    if hasattr(card, 'counters'):
                        plus_counters = card.counters.get("+1/+1", 0)
                        minus_counters = card.counters.get("-1/-1", 0)
                    
                    effective_toughness = base_toughness + plus_counters - minus_counters
                    
                    if effective_toughness > 0 and damage >= effective_toughness:
                        # Check for indestructible
                        indestructible = (hasattr(card, 'oracle_text') and 
                                        'indestructible' in card.oracle_text.lower())
                        if indestructible:
                            continue
                        
                        # Check if can regenerate
                        can_regenerate = False
                        if hasattr(card, 'oracle_text') and "regenerate" in card.oracle_text.lower():
                            # Check if regeneration mana is available or regeneration is already activated
                            regeneration_active = card_id in player.get("regeneration_shields", set())
                            
                            # If regeneration is active or can be activated
                            if regeneration_active or sum(player["mana_pool"].values()) >= 2:  # Typical regeneration cost
                                if not regeneration_active:
                                    # Deduct mana (simplified implementation)
                                    for color in ['C', 'G', 'B', 'R', 'U', 'W']:  # Priority order
                                        if player["mana_pool"].get(color, 0) > 0:
                                            player["mana_pool"][color] -= 1
                                            break
                                    
                                    # Add regeneration shield if not already there
                                    if not hasattr(player, "regeneration_shields"):
                                        player["regeneration_shields"] = set()
                                    player["regeneration_shields"].add(card_id)
                                
                                # Apply regeneration effect
                                can_regenerate = True
                                # Tap the creature and remove damage
                                player["tapped_permanents"].add(card_id)
                                if card_id in player.get("damage_counters", {}):
                                    del player["damage_counters"][card_id]
                                
                                # Remove regeneration shield
                                if hasattr(player, "regeneration_shields") and card_id in player["regeneration_shields"]:
                                    player["regeneration_shields"].remove(card_id)
                                    
                                logging.debug(f"SBA: Regenerated {card.name}")
                                current_actions_performed = True
                        
                        # If can't regenerate, mark for destruction
                        if not can_regenerate:
                            dead_creatures.append(card_id)
                
                # Handle Totem Armor replacement effect before processing death
                for card_id in dead_creatures[:]:  # Copy the list to allow modification
                    # Check if creature has an aura with Totem Armor
                    if card_id in player["battlefield"]:
                        has_totem_armor = False
                        totem_aura_id = None
                        
                        for aura_id in list(player["battlefield"]):
                            aura = self._safe_get_card(aura_id)
                            if (aura and hasattr(aura, 'card_types') and 'enchantment' in aura.card_types and
                                hasattr(aura, 'oracle_text') and "totem armor" in aura.oracle_text.lower()):
                                
                                # Check if this aura is attached to the dying creature
                                if (hasattr(player, "attachments") and
                                    aura_id in player["attachments"] and
                                    player["attachments"][aura_id] == card_id):
                                    
                                    has_totem_armor = True
                                    totem_aura_id = aura_id
                                    break
                        
                        if has_totem_armor and totem_aura_id:
                            # Totem Armor prevents destruction
                            dead_creatures.remove(card_id)
                            # Destroy the aura instead
                            self.move_card(totem_aura_id, player, "battlefield", player, "graveyard")
                            # Remove damage from creature
                            if card_id in player.get("damage_counters", {}):
                                del player["damage_counters"][card_id]
                                
                            logging.debug(f"SBA: Totem Armor saved {self._safe_get_card(card_id).name}, destroying {self._safe_get_card(totem_aura_id).name} instead")
                            current_actions_performed = True
                
                # Process removals after handling regeneration and totem armor
                for card_id in dead_creatures:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        self.trigger_ability(card_id, "DIES")
                        current_actions_performed = True
                        logging.debug(f"SBA: Creature {self._safe_get_card(card_id).name} died from lethal damage")
            
            # 704.5g Creature dealt damage by a source with deathtouch is destroyed
            for player in [self.p1, self.p2]:
                dead_from_deathtouch = []
                
                # Check for creatures that were dealt damage by deathtouch sources
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'creature' not in card.card_types:
                        continue
                    
                    # Check for deathtouch damage
                    deathtouch_damage = player.get("deathtouch_damage", {}).get(card_id, 0)
                    if deathtouch_damage > 0:
                        # Check for indestructible
                        indestructible = (hasattr(card, 'oracle_text') and 
                                        'indestructible' in card.oracle_text.lower())
                        if not indestructible:
                            # Check for regeneration (similar to above)
                            can_regenerate = False
                            if hasattr(card, 'oracle_text') and "regenerate" in card.oracle_text.lower():
                                # Simplified regeneration check
                                if sum(player["mana_pool"].values()) >= 2:
                                    can_regenerate = True
                                    player["tapped_permanents"].add(card_id)
                                    if hasattr(player, "deathtouch_damage"):
                                        player["deathtouch_damage"].pop(card_id, None)
                                    logging.debug(f"SBA: Regenerated {card.name} from deathtouch")
                            
                            if not can_regenerate:
                                dead_from_deathtouch.append(card_id)
                
                # Handle Totem Armor for deathtouch (similar to above)
                for card_id in dead_from_deathtouch[:]:
                    if card_id in player["battlefield"]:
                        has_totem_armor = False
                        totem_aura_id = None
                        
                        for aura_id in list(player["battlefield"]):
                            aura = self._safe_get_card(aura_id)
                            if (aura and hasattr(aura, 'card_types') and 'enchantment' in aura.card_types and
                                hasattr(aura, 'oracle_text') and "totem armor" in aura.oracle_text.lower()):
                                
                                # Check if attached to dying creature
                                if (hasattr(player, "attachments") and
                                    aura_id in player["attachments"] and
                                    player["attachments"][aura_id] == card_id):
                                    
                                    has_totem_armor = True
                                    totem_aura_id = aura_id
                                    break
                        
                        if has_totem_armor and totem_aura_id:
                            dead_from_deathtouch.remove(card_id)
                            self.move_card(totem_aura_id, player, "battlefield", player, "graveyard")
                            if hasattr(player, "deathtouch_damage"):
                                player["deathtouch_damage"].pop(card_id, None)
                            logging.debug(f"SBA: Totem Armor saved {self._safe_get_card(card_id).name} from deathtouch")
                            current_actions_performed = True
                
                # Process removals from deathtouch
                for card_id in dead_from_deathtouch:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        self.trigger_ability(card_id, "DIES")
                        current_actions_performed = True
                        logging.debug(f"SBA: Creature {self._safe_get_card(card_id).name} died from deathtouch damage")
            
            # 704.5i Attached Aura with illegal target or no target is put into owner's graveyard
            for player in [self.p1, self.p2]:
                illegal_auras = []
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'enchantment' not in card.card_types:
                        continue
                    
                    # Check if Aura
                    if not hasattr(card, 'subtypes') or 'aura' not in card.subtypes:
                        continue
                    
                    # Check if attached to something
                    if not hasattr(player, "attachments"):
                        player["attachments"] = {}
                    
                    attached_to = player["attachments"].get(card_id)
                    if attached_to is None:
                        # Not attached to anything, check if it should be
                        if hasattr(card, 'oracle_text') and 'enchant' in card.oracle_text.lower():
                            illegal_auras.append(card_id)
                            continue
                    
                    # Check if target is still valid
                    target_valid = False
                    for p in [self.p1, self.p2]:
                        if attached_to in p["battlefield"]:
                            target_valid = True
                            break
                    
                    if not target_valid:
                        illegal_auras.append(card_id)
                
                # Process removals
                for card_id in illegal_auras:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        current_actions_performed = True
                        logging.debug(f"SBA: Aura {self._safe_get_card(card_id).name} died from illegal or missing target")
            
            # 704.5j Attached Equipment becomes unattached if attached to illegal target
            for player in [self.p1, self.p2]:
                illegal_equipments = []
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'artifact' not in card.card_types:
                        continue
                    
                    # Check if Equipment
                    if not hasattr(card, 'subtypes') or 'equipment' not in card.subtypes:
                        continue
                    
                    # Check if attached
                    if not hasattr(player, "attachments"):
                        player["attachments"] = {}
                    
                    attached_to = player["attachments"].get(card_id)
                    if attached_to is None:
                        continue  # Equipment can be unattached
                    
                    # Check if target is still valid
                    target_valid = False
                    for p in [self.p1, self.p2]:
                        if attached_to in p["battlefield"]:
                            equipped_card = self._safe_get_card(attached_to)
                            if equipped_card and hasattr(equipped_card, 'card_types') and 'creature' in equipped_card.card_types:
                                target_valid = True
                                break
                    
                    if not target_valid:
                        illegal_equipments.append(card_id)
                
                # Process unattaching
                for card_id in illegal_equipments:
                    if card_id in player["battlefield"] and card_id in player["attachments"]:
                        del player["attachments"][card_id]
                        current_actions_performed = True
                        logging.debug(f"SBA: Equipment {self._safe_get_card(card_id).name} became unattached from illegal target")
            
            # 704.5k Legend rule: If a player controls two or more legendary permanents with the same name,
            for player in [self.p1, self.p2]:
                # Group legendary permanents by name
                legendary_by_name = {}
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'type_line'):
                        continue
                        
                    # Check if legendary
                    if 'legendary' in card.type_line.lower():
                        name = card.name.lower() if hasattr(card, 'name') else ""
                        if name not in legendary_by_name:
                            legendary_by_name[name] = []
                        legendary_by_name[name].append(card_id)
                
                # Apply legend rule for each group
                for name, card_ids in legendary_by_name.items():
                    if len(card_ids) > 1:
                        # Let player choose one to keep (in simulation, keep the newest)
                        sorted_ids = sorted(card_ids)  # Sort by ID (newer cards typically have higher IDs)
                        to_keep = sorted_ids[-1]
                        
                        # Remove the others
                        for card_id in sorted_ids[:-1]:
                            if card_id in player["battlefield"]:
                                self.move_card(card_id, player, "battlefield", player, "graveyard")
                                current_actions_performed = True
                                logging.debug(f"SBA: Legend rule applied to {self._safe_get_card(card_id).name}")

            # Enhanced "planeswalker uniqueness rule" (only for older versions of MTG)
            if getattr(self, 'use_old_planeswalker_rule', False):  # Only if using older rules
                for player in [self.p1, self.p2]:
                    # Group planeswalkers by type
                    planeswalkers_by_type = {}
                    for card_id in list(player["battlefield"]):
                        card = self._safe_get_card(card_id)
                        if not card or not hasattr(card, 'card_types') or not hasattr(card, 'subtypes'):
                            continue
                            
                        # Check if planeswalker
                        if 'planeswalker' in card.card_types:
                            for subtype in card.subtypes:
                                if subtype.lower() != 'planeswalker':  # Skip the 'planeswalker' type itself
                                    planeswalker_type = subtype.lower()
                                    if planeswalker_type not in planeswalkers_by_type:
                                        planeswalkers_by_type[planeswalker_type] = []
                                    planeswalkers_by_type[planeswalker_type].append(card_id)
                    
                    # Apply planeswalker uniqueness rule for each type
                    for pw_type, card_ids in planeswalkers_by_type.items():
                        if len(card_ids) > 1:
                            # Let player choose one to keep (in simulation, keep the newest)
                            sorted_ids = sorted(card_ids)
                            to_keep = sorted_ids[-1]
                            
                            # Remove the others
                            for card_id in sorted_ids[:-1]:
                                if card_id in player["battlefield"]:
                                    self.move_card(card_id, player, "battlefield", player, "graveyard")
                                    current_actions_performed = True
                                    logging.debug(f"SBA: Planeswalker uniqueness rule applied to {self._safe_get_card(card_id).name}")
            
            # 704.5m "World" rule: If two or more permanents have the supertype world,
            # all except the newest are put into their owners' graveyards
            world_permanents = []
            for player in [self.p1, self.p2]:
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'type_line'):
                        continue
                    
                    # Check if world
                    if 'world' in card.type_line.lower():
                        world_permanents.append((card_id, player))
            
            if len(world_permanents) > 1:
                # Sort by timestamp (we'll use card_id as a proxy)
                world_permanents.sort(key=lambda x: x[0])
                
                # Keep the newest world permanent, put the rest into graveyard
                for card_id, player in world_permanents[:-1]:
                    if card_id in player["battlefield"]:
                        self.move_card(card_id, player, "battlefield", player, "graveyard")
                        current_actions_performed = True
                        logging.debug(f"SBA: World permanent {self._safe_get_card(card_id).name} died from world rule")
            
            # 704.5n Tokens that left the battlefield cease to exist
            for player in [self.p1, self.p2]:
                if hasattr(player, "tokens"):
                    removed_tokens = []
                    for token_id in player["tokens"]:
                        # Check if token is not on battlefield
                        if token_id not in player["battlefield"]:
                            removed_tokens.append(token_id)
                            
                            # Remove from card database
                            if token_id in self.card_db:
                                del self.card_db[token_id]
                                
                            # Remove from all zones (cleanup)
                            for zone in ["hand", "graveyard", "exile"]:
                                if token_id in player[zone]:
                                    player[zone].remove(token_id)
                                    
                    # Update tokens list
                    if removed_tokens:
                        player["tokens"] = [t for t in player["tokens"] if t not in removed_tokens]
                        for token_id in removed_tokens:
                            logging.debug(f"SBA: Token {token_id} ceased to exist after leaving battlefield")
                        current_actions_performed = True
            
            # 704.5q +1/+1 and -1/-1 counters annihilate each other
            for player in [self.p1, self.p2]:
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'counters'):
                        continue
                    
                    plus_counters = card.counters.get("+1/+1", 0)
                    minus_counters = card.counters.get("-1/-1", 0)
                    
                    if plus_counters > 0 and minus_counters > 0:
                        # Remove the smaller number of counters
                        remove_count = min(plus_counters, minus_counters)
                        card.counters["+1/+1"] -= remove_count
                        card.counters["-1/-1"] -= remove_count
                        
                        # Ensure we don't have negative counter counts
                        if card.counters["+1/+1"] <= 0:
                            del card.counters["+1/+1"]
                        if card.counters["-1/-1"] <= 0:
                            del card.counters["-1/-1"]
                            
                        # Update power/toughness (neutral effect)
                        current_actions_performed = True
                        logging.debug(f"SBA: Removed {remove_count} +1/+1 and -1/-1 counters from {card.name}")
            
            # NEW STATE-BASED ACTION: 704.5r If a permanent has both a +1/+1 counter and a -1/-1 counter on it,
            # one counter of each kind is removed
            for player in [self.p1, self.p2]:
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'counters'):
                        continue
                    
                    plus_counters = card.counters.get("+1/+1", 0)
                    minus_counters = card.counters.get("-1/-1", 0)
                    
                    if plus_counters > 0 and minus_counters > 0:
                        # Remove one counter of each kind
                        card.counters["+1/+1"] -= 1
                        card.counters["-1/-1"] -= 1
                        
                        # Clean up empty counter entries
                        if card.counters["+1/+1"] <= 0:
                            del card.counters["+1/+1"]
                        if card.counters["-1/-1"] <= 0:
                            del card.counters["-1/-1"]
                        
                        current_actions_performed = True
                        logging.debug(f"SBA: Removed one +1/+1 and one -1/-1 counter from {card.name}")
            
            # NEW STATE-BASED ACTION: 704.5s Creature with lethal infect/wither damage
            for player in [self.p1, self.p2]:
                for card_id in list(player["battlefield"]):
                    card = self._safe_get_card(card_id)
                    if not card or not hasattr(card, 'card_types') or 'creature' not in card.card_types:
                        continue
                    
                    infect_damage = player.get("infect_damage", {}).get(card_id, 0)
                    wither_damage = player.get("wither_damage", {}).get(card_id, 0)
                    total_negative_counters = infect_damage + wither_damage
                    
                    # If there's enough damage to cause lethal -1/-1 counters
                    if total_negative_counters > 0:
                        # Initialize counters attribute if needed
                        if not hasattr(card, 'counters'):
                            card.counters = {}
                        
                        # Add -1/-1 counters
                        current_minus = card.counters.get("-1/-1", 0)
                        card.counters["-1/-1"] = current_minus + total_negative_counters
                        
                        # Update card's stats
                        if hasattr(card, 'power'):
                            card.power = max(0, card.power - total_negative_counters)
                        if hasattr(card, 'toughness'):
                            card.toughness = max(0, card.toughness - total_negative_counters)
                        
                        # Clear the damage tracking
                        if hasattr(player, "infect_damage") and card_id in player["infect_damage"]:
                            del player["infect_damage"][card_id]
                        if hasattr(player, "wither_damage") and card_id in player["wither_damage"]:
                            del player["wither_damage"][card_id]
                        
                        current_actions_performed = True
                        logging.debug(f"SBA: Added {total_negative_counters} -1/-1 counters to {card.name} from infect/wither")
            
            # NEW STATE-BASED ACTION: 704.5t Player with 15 or more experience counters gets an emblem
            # (Simplified version of rule for specific cards like "Architect of Thought")
            for player in [self.p1, self.p2]:
                experience_counters = player.get("experience_counters", 0)
                has_special_emblem = player.get("has_experience_emblem", False)
                
                if experience_counters >= 15 and not has_special_emblem:
                    player["has_experience_emblem"] = True
                    player["emblems"] = player.get("emblems", []) + ["experience_emblem"]
                    current_actions_performed = True
                    logging.debug(f"SBA: Player {player['name']} got emblem from 15+ experience counters")
            
            # NEW STATE-BASED ACTION: 704.5u If a permanent with an ability that triggers "at the beginning of the end step" 
            # enters the battlefield during the end step, its ability won't trigger until the next turn's end step
            if self.phase == self.PHASE_END_STEP:
                for player in [self.p1, self.p2]:
                    for card_id in list(player["battlefield"]):
                        # Check if entered this turn during end step
                        if card_id in player["entered_battlefield_this_turn"]:
                            card = self._safe_get_card(card_id)
                            if not card or not hasattr(card, 'oracle_text'):
                                continue
                            
                            # Check for "at the beginning of the end step" triggers
                            if "at the beginning of the end step" in card.oracle_text.lower():
                                # Mark this card to skip its end step trigger this turn
                                if not hasattr(player, "skip_end_step_trigger"):
                                    player["skip_end_step_trigger"] = set()
                                player["skip_end_step_trigger"].add(card_id)
                                current_actions_performed = True
                                logging.debug(f"SBA: {card.name} marked to skip end step trigger this turn")
            
            # Additional check: Phasing
            # Phased-out permanents phase in at the beginning of their controller's untap step
            for player in [self.p1, self.p2]:
                if hasattr(self, 'phased_out') and self.phase == self.PHASE_UNTAP:
                    phased_cards = [card_id for card_id in self.phased_out 
                                if self._find_card_owner(card_id) == player]
                    
                    for card_id in phased_cards:
                        self.phased_out.remove(card_id)
                        current_actions_performed = True
                        logging.debug(f"SBA: {self._safe_get_card(card_id).name} phased in")
            
            # Update actions_performed flag
            actions_performed = actions_performed or current_actions_performed
            
            # Break if no actions were performed this iteration
            if not current_actions_performed:
                break
        
        # Return whether any actions were performed
        return actions_performed
    
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
                        
    def get_card_controller(self, card_id):
        """
        Find the controller of a card across all zones.
        
        Args:
            card_id: The ID of the card to find the controller for
        
        Returns:
            The player dictionary controlling the card, or None if not found
        """
        for player in [self.p1, self.p2]:
            for zone in ["battlefield", "hand", "graveyard", "exile"]:
                if card_id in player.get(zone, []):
                    return player
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
