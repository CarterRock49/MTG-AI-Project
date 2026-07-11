import random
import logging
import numpy as np
import copy

from .ability_utils import EffectFactory

# (Keep existing imports)
from .card import Card
import re
from .ability_types import StaticAbility, TriggeredAbility
from collections import defaultdict


from .game_state_setup import GameStateSetupMixin
from .game_state_turn import GameStateTurnMixin
from .game_state_zones import GameStateZonesMixin
from .game_state_stack import GameStateStackMixin
from .game_state_permanents import GameStatePermanentsMixin
from .game_state_damage import GameStateDamageMixin


class GameState(
    GameStateSetupMixin,
    GameStateTurnMixin,
    GameStateZonesMixin,
    GameStateStackMixin,
    GameStatePermanentsMixin,
    GameStateDamageMixin,
):

    # (Keep existing class variables like PHASE_ constants and __slots__)
    __slots__ = ["card_db", "max_turns", "max_hand_size", "max_battlefield", "day_night_checked_this_turn",
                 "fidelity_counters", "_sba_in_progress", "delayed_triggers",
                 "delayed_event_triggers", "copy_overrides", "plotted_cards",
                 "phase_history", "stack", "priority_pass_count", "last_stack_size",
                 "turn", "_phase", "_last_turn_phase", "agent_is_p1", "combat_damage_dealt", "day_night_state",
                 "current_attackers", "current_block_assignments", 'mulligan_data',
                 "current_spell_requires_target", "current_spell_card_id", "exhaust_ability_used",
                 "_last_card_locations", "_ceased_token_cards",
                 "optimal_attackers", "attack_suggestion_used", 'cards_played', 'play_history', 'phased_out_state',
                 "p1", "p2", "ability_handler", "damage_dealt_this_turn",
                 "previous_priority_phase", "layer_system", "until_end_of_turn_effects",
                 "mana_system", "replacement_effects", "cards_drawn_this_turn", "cards_milled_this_turn",
                 "combat_resolver", "temp_control_effects", "abilities_activated_this_turn",
                 "card_evaluator", "spells_cast_this_turn", "_phase_history",
                 "strategic_planner", "attackers_this_turn", "creatures_died_this_turn", 'strategy_memory',
                 "_logged_card_ids", "_logged_errors", "targeting_system",
                 "_phase_action_count", "priority_player", "stats_tracker",
                 "card_memory", 'original_p2_deck', "_consecutive_no_ops",
                 # *** ADDED action_handler ***
                 "action_handler", "impending_cards", "_offspring_cost_paid_context",
                 # Special card types
                 "adventure_cards", "saga_counters", "mdfc_cards", "battle_cards", 'battle_attack_targets',
                 "melded_permanents", "mutated_permanents", "specialized_cards",
                 "last_die_roll", "die_roll_history",
                 "cards_castable_from_exile", "impulse_until_eot", "cast_as_back_face", 'planeswalker_attack_targets',
                 # Additional slots for various tracking variables
                 "phased_out", 'original_p1_deck',
                 "suspended_cards",
                 "kicked_cards", "evoked_cards", 'planeswalker_protectors',
                 "foretold_cards", "blitz_cards", "dash_cards", "unearthed_cards",
                 "jump_start_cards", "buyback_cards", "flashback_cards",
                 "life_gained_this_turn", "damage_this_turn", "exile_at_end_of_combat",
                 "haste_until_eot", "has_haste_until_eot", "progress_was_forced",
                 "_turn_limit_checked", "miracle_card", "miracle_cost", "miracle_player",
                 "miracle_active", "miracle_card_id", "miracle_cost_parsed",
                 # New tracking variables
                 "split_second_active",
                 "rebounded_cards",
                 "banding_creatures",
                 "crewed_vehicles", "morphed_cards", "manifested_cards",
                 "cards_to_graveyard_this_turn", 'first_strike_ordering',
                 "boast_activated", "forecast_used", "epic_spells", "city_blessing",
                 "myriad_tokens", "persist_returned", "undying_returned", "gravestorm_count",
                 "madness_cast_available",
                 # Context slots
                 "targeting_context", "sacrifice_context", "choice_context",
                 "mulligan_in_progress", "mulligan_player", "mulligan_count",
                 "bottoming_in_progress", "bottoming_player", "cards_to_bottom", "bottoming_count",
                 "_opening_hand_players", "extra_combat_phases",
                 "spree_context", 'combat_action_handler', '_handle_level_up_class',
                 "dredge_pending",
                 "madness_trigger",
                 "pending_spell_context", "clash_context",
                 "surveil_in_progress", "cards_being_surveiled", "surveiling_player",
                 "scry_in_progress", "scrying_cards", "scrying_player", "scrying_tops", "scrying_bottoms"
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

    _TURN_PHASES = frozenset({
        PHASE_UNTAP, PHASE_UPKEEP, PHASE_DRAW, PHASE_MAIN_PRECOMBAT,
        PHASE_BEGIN_COMBAT, PHASE_DECLARE_ATTACKERS, PHASE_DECLARE_BLOCKERS,
        PHASE_FIRST_STRIKE_DAMAGE, PHASE_COMBAT_DAMAGE, PHASE_END_OF_COMBAT,
        PHASE_MAIN_POSTCOMBAT, PHASE_END_STEP, PHASE_CLEANUP,
    })

    def _get_phase(self):
        return self._phase

    def _set_phase(self, value):
        """Set the public phase while retaining the last real turn phase.

        Priority, targeting, sacrifice, and choice are transient engine
        wrappers, not turn phases.  Nested wrappers can legitimately consume
        ``previous_priority_phase``; this independent value lets the turn
        engine resume without guessing which phase was interrupted.
        """
        self._phase = value
        if value in self._TURN_PHASES:
            self._last_turn_phase = value

    phase = property(_get_phase, _set_phase)

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
        self._consecutive_no_ops = 0
        # Combat state initialization
        self.current_attackers = []
        self.current_block_assignments = {}
        self.exhaust_ability_used = {} # Add this line
        self._last_card_locations = {}
        self._ceased_token_cards = {}
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
        # *** ADDED: Initialize action_handler to None ***
        self.action_handler = None

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
        self._init_subsystems(include_agents=False)  # Agent layer is owned/attached by the environment # Centralized subsystem creation
        logging.info("GameState initialized.")
        
    def _init_subsystems(self, include_agents=True, reset_tracking=True):
        """Initialize game subsystems with error handling and correct dependencies.

        Rules-engine subsystems (layers, replacements, targeting, abilities, mana,
        combat) are always built: they are part of the game state itself. The agent
        layer (action handler, card evaluator, strategic planner) is built only when
        include_agents is True — clone() uses that so MCTS simulations get a fully
        self-contained stack, while the primary game state gets its agent layer
        constructed and attached by the environment (single ownership). Clones set
        reset_tracking=False because their copied state must survive subsystem
        reconstruction.
        """
        logging.debug("Initializing GameState subsystems...")
        self._init_rules_subsystems()
        if include_agents:
            self._init_agent_subsystems()
        else:
            self.action_handler = None
            self.card_evaluator = None
            self.strategic_planner = None
        logging.info("Finished initializing GameState subsystems.")
        # Init tracking variables AFTER subsystems that might reference them.
        if reset_tracking:
            self._init_tracking_variables()
            self.initialize_day_night_cycle()

    def _init_rules_subsystems(self):
        """Rules-engine subsystems owned by GameState itself."""
        # --- Layer System (needed by many others) ---
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

        # --- Replacement Effects ---
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

        # --- Targeting System ---
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

        # --- Ability Handler ---
        try:
            from .ability_handler import AbilityHandler
            self.ability_handler = AbilityHandler(self) # Init after its dependencies
            logging.debug("AbilityHandler initialized successfully.")
        except ImportError as e:
            logging.warning(f"AbilityHandler module not available: {e}")
            self.ability_handler = None
        except Exception as e:
            logging.error(f"Error initializing AbilityHandler: {e}")
            self.ability_handler = None

        # --- Mana System ---
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

        # --- Combat Resolver ---
        try:
            from .enhanced_combat import ExtendedCombatResolver
            self.combat_resolver = ExtendedCombatResolver(self)
            logging.debug("Combat resolver initialized successfully.")
        except ImportError as e:
            logging.warning(f"Combat resolver module not available: {e}")
            self.combat_resolver = None
        except Exception as e:
            logging.error(f"Error initializing CombatResolver: {e}")
            self.combat_resolver = None


    def _init_agent_subsystems(self):
        """Agent/strategy layer. Built here only for clones; the environment
        constructs and attaches these for the primary game state."""
        # --- CRITICAL: Action Handler first if others depend on it during init ---
        try:
            from .actions import ActionHandler # Import inside to avoid circular?
            self.action_handler = ActionHandler(self)
            logging.debug("ActionHandler initialized successfully.")
        except ImportError as e:
            logging.error(f"ActionHandler module not available: {e}")
            self.action_handler = None
        except Exception as e:
            logging.error(f"Error initializing ActionHandler: {e}")
            self.action_handler = None

        # --- Card Evaluator (can be created even if external refs are missing initially) ---
        try:
            from .enhanced_card_evaluator import EnhancedCardEvaluator
            self.card_evaluator = EnhancedCardEvaluator(
                self,
                getattr(self, 'stats_tracker', None), # Pass potential external refs
                getattr(self, 'card_memory', None)
            )
            logging.debug("Card evaluator initialized successfully.")
        except ImportError as e:
            logging.warning(f"Card evaluator module not available: {e}")
            self.card_evaluator = None
        except Exception as e:
            logging.error(f"Error initializing EnhancedCardEvaluator: {e}")
            self.card_evaluator = None

        # --- Strategic Planner ---
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


    def _init_tracking_variables(self):
            """Initialize all game state tracking variables with proper defaults."""
            # Player Independent Tracking
            # Simulation-fidelity telemetry: counts of rules the engine could not
            # faithfully execute this game. Downstream deck/card statistics should
            # weight or filter games with high counts — unfaithful games produce
            # misleading win-rate data.
            self._sba_in_progress = False  # re-entrancy guard for check_state_based_actions
            # CR 603.7 delayed triggered abilities + legacy asap callables.
            # Entries: dicts from register_delayed_trigger, or bare callables
            # (fire at the next state-based check).
            self.delayed_triggers = []
            self.delayed_event_triggers = []
            self.last_die_roll = {}
            self.die_roll_history = []
            for _meld_id, _meld_info in getattr(self, 'melded_permanents', {}).items():
                _meld_card = self._safe_get_card(_meld_id)
                if _meld_card and _meld_info.get('original_printed'):
                    _meld_card._printed = copy.deepcopy(_meld_info['original_printed'])
                    _meld_card.reset_to_printed()
            for _mutated_id, _mutated_info in getattr(self, 'mutated_permanents', {}).items():
                _mutated_card = self._safe_get_card(_mutated_id)
                _component_printed = _mutated_info.get('component_printed', {})
                if _mutated_card and _mutated_id in _component_printed:
                    _mutated_card._printed = copy.deepcopy(_component_printed[_mutated_id])
                    _mutated_card.reset_to_printed()
            for _specialized_id, _specialized_info in getattr(self, 'specialized_cards', {}).items():
                _specialized_card = self._safe_get_card(_specialized_id)
                if _specialized_card and _specialized_info.get('original_printed'):
                    _specialized_card._printed = copy.deepcopy(
                        _specialized_info['original_printed'])
                    _specialized_card.reset_to_printed()
            for _manifest_id, _manifest_info in getattr(self, 'manifested_cards', {}).items():
                _manifest_card = self._safe_get_card(_manifest_id)
                if _manifest_card and _manifest_info.get('original_printed'):
                    _manifest_card._printed = copy.deepcopy(
                        _manifest_info['original_printed'])
                    _manifest_card.reset_to_printed()
                    _manifest_card.face_down = False
            for _copy_id, _copy_info in getattr(self, 'copy_overrides', {}).items():
                _copy_card = self._safe_get_card(_copy_id)
                if _copy_card and _copy_info.get('original_printed'):
                    _copy_card._printed = copy.deepcopy(
                        _copy_info['original_printed'])
                    _copy_card.reset_to_printed()
            # BUGFIX: Card objects are shared via card_db across games, and
            # in-play state written onto them (counters) leaked into later
            # games -- game N+1 started with game N's +1/+1 counters, silently
            # corrupting every collected statistic after the first game.
            try:
                if isinstance(getattr(self, 'card_db', None), dict):
                    for _card in self.card_db.values():
                        if getattr(_card, 'counters', None):
                            _card.counters = {}
                        if (getattr(_card, 'faces', None)
                                and getattr(_card, 'current_face', 0) != 0
                                and hasattr(_card, 'set_current_face')):
                            _card.set_current_face(0)
                        # Same leakage class, wider blast radius: the layer
                        # write-back mutates shared card objects (name, P/T,
                        # keywords, ...). Restore printed characteristics so
                        # game N's continuous-effect output cannot leak into
                        # game N+1's live card state.
                        if hasattr(_card, 'reset_to_printed'):
                            _card.reset_to_printed()
            except Exception as _e:
                logging.error(f"Error clearing transient card state: {_e}")
            self.fidelity_counters = {
                "unimplemented_action": 0,
                "unimplemented_action_types": set(),
                "unparsed_mana": 0,
                "unparsed_modal": 0,
                "unparsed_effects": 0,
                "unparsed_cards": set(),  # names of cards whose text the engine could not fully parse
            }
            self.day_night_state = None
            self.day_night_checked_this_turn = False
            self.split_second_active = False
            self.phased_out = set() # Stores IDs of phased-out permanents
            self.phased_out_state = {} # Per-card controller/status and phase-in group
            self.melded_permanents = {} # Primary ID -> component/result identity data
            self.mutated_permanents = {} # Battlefield ID -> ordered physical components/identities
            self.specialized_cards = {} # Card ID -> perpetual specialized identity data
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
            self.copy_overrides = {}
            self.plotted_cards = []
            self.epic_spells = {}
            self.myriad_tokens = []
            self.persist_returned = set()
            self.undying_returned = set()
            self.banding_creatures = set() # Track creatures currently in bands

            # Turn-based tracking (resets each turn usually)
            self.spells_cast_this_turn = []
            self.attackers_this_turn = set()
            self.creatures_died_this_turn = {}
            self.damage_dealt_this_turn = {}
            self.cards_drawn_this_turn = {} # Initialize as empty, will be populated like {'p1': 0, 'p2': 0}
            self.cards_milled_this_turn = {} # MillEffect tracking; was written but never declared (crashed on __slots__)
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
            self.impending_cards = {}
            self._offspring_cost_paid_context = {}
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
            self.impulse_until_eot = set() # impulse-drawn cards whose play permission expires at end of turn
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
                    player["targeted_permanents_this_turn"] = set()
                    player["pw_activations"] = {}
                    player["lost_life_this_turn"] = False
                    player["attempted_draw_from_empty"] = False
                    player["poison_counters"] = 0
                    player["experience_counters"] = 0
                    player["energy_counters"] = 0
                    player["city_blessing"] = False
                    player["monarch"] = False
                    player["damage_counters"] = {}
                    player["deathtouch_damage"] = {} # FIXED: Must be a dict, not a set
                    player["loyalty_counters"] = {}
                    # player["saga_counters"] = {} # Moved to game level
                    player["attachments"] = {}
                    player["championed_cards"] = {}
                    player["linked_exile"] = {}
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


    def reset(self, p1_deck, p2_deck, seed=None):
            """Reset the game state with new decks and initialize all subsystems (Revised Mulligan/Priority)"""
            if seed is not None:
                random.seed(seed)
                np.random.seed(seed)

            logging.debug("Starting GameState reset...")
            # Reset basic game state
            self.turn = 1 # Start at Turn 1 conceptually
            self.phase = self.PHASE_UPKEEP # Initial conceptual phase
            self.combat_damage_dealt = False
            self.stack = []
            self.priority_pass_count = 0
            self.last_stack_size = 0
            self._phase_action_count = 0
            self._phase_history = [] # Explicit reset
            self.optimal_attackers = None
            self.attack_suggestion_used = False
            self.cards_played = {0: [], 1: []} # Explicit reset
            self.play_history = {0: {}, 1: {}} # {player_idx: {turn: [card_ids]}} — real play turns for stats
            self.exhaust_ability_used = {} # Reset exhaust tracking
            self._last_card_locations = {}
            self._ceased_token_cards = {}
            self._consecutive_no_ops = 0
            # Ensure decks exist and are at least copy-able, with fallbacks if necessary
            p1_deck_safe = p1_deck.copy() if isinstance(p1_deck, list) else []
            p2_deck_safe = p2_deck.copy() if isinstance(p2_deck, list) else []
            
            # Ensure original deck references exist even if empty
            self.original_p1_deck = p1_deck_safe
            self.original_p2_deck = p2_deck_safe

            # Initialize player states AFTER resetting other state
            self.p1 = self._init_player(p1_deck_safe, player_num=1)
            self.p2 = self._init_player(p2_deck_safe, player_num=2)
            
            # Set agent identity *after* players are created
            self.agent_is_p1 = True 

            # *** CRITICAL FIX: Initialize Priority Player ***
            # Rule 103.1: At the start of the game, the starting player (P1) has priority.
            # Even if Mulligan overrides control logic, this prevents 'None' state.
            self.priority_player = self.p1 

            # --- Mulligan State Setup (CRITICAL) ---
            # After players exist, before subsystems that might query mulligan state
            self.mulligan_in_progress = True # Start with mulligan phase active
            self.mulligan_player = self.p1 # P1 mulligans first
            self.mulligan_count = {'p1': 0, 'p2': 0} # Reset counts
            self.mulligan_data = {'p1': 0, 'p2': 0} # Reset separate tracker
            self.bottoming_in_progress = False
            self.bottoming_player = None
            self.cards_to_bottom = 0
            self.bottoming_count = 0
            # Ensure temporary mulligan flags are cleared on players
            if self.p1: self.p1.pop('_mulligan_decision_made', None); self.p1.pop('_needs_to_bottom_next', None); self.p1.pop('_bottoming_complete', None)
            if self.p2: self.p2.pop('_mulligan_decision_made', None); self.p2.pop('_needs_to_bottom_next', None); self.p2.pop('_bottoming_complete', None)

            # --- Subsystem Initialization ---
            try:
                self._init_subsystems(include_agents=False) # Agent layer is owned/attached by the environment
            except Exception as e:
                logging.error(f"Error initializing subsystems: {e}")
                # Continue with reset even if subsystem init fails

            # Initialize all tracking variables using the helper AFTER subsystems exist
            try:
                self._init_tracking_variables()
                self.initialize_day_night_cycle() # Call after tracking vars init
            except Exception as e:
                logging.error(f"Error initializing tracking variables: {e}")
                # Continue with reset even if tracking var init fails

            # Link external systems AFTER local subsystems are initialized
            self.strategy_memory = getattr(self, 'strategy_memory', None)
            self.stats_tracker = getattr(self, 'stats_tracker', None)
            self.card_memory = getattr(self, 'card_memory', None)
            if self.strategy_memory: self.strategy_memory.game_state = self
            if self.stats_tracker: self.stats_tracker.game_state = self # Link if needed
            if self.card_memory: self.card_memory.game_state = self # Link if needed
            # Link subsystems that depend on external trackers
            if self.card_evaluator:
                self.card_evaluator.stats_tracker = self.stats_tracker
                self.card_evaluator.card_memory = self.card_memory
            if self.strategic_planner and self.strategy_memory:
                self.strategic_planner.strategy_memory = self.strategy_memory

            # Final setup calls
            if self.strategic_planner and hasattr(self.strategic_planner, 'init_after_reset'):
                self.strategic_planner.init_after_reset()

            # Initialize card abilities via AbilityHandler AFTER it's linked
            if self.ability_handler and hasattr(self.ability_handler, '_initialize_abilities'):
                logging.debug("Initializing card abilities via AbilityHandler.")
                if isinstance(self.card_db, dict) and self.card_db:
                    self.ability_handler._initialize_abilities()
                else: logging.error("Cannot initialize abilities: card_db is not valid.")

            # Initial Layer application
            if self.layer_system:
                logging.debug("Applying initial layer effects after reset.")
                self.layer_system.apply_all_effects()

            # Verify mulligan state is consistent before proceeding
            self.check_mulligan_state()

            logging.debug("GameState reset complete. Mulligan phase active. Priority initialized to P1.")
    
    def _init_player(self, deck, player_num):
        """Initialize a player's state with a given deck and draw 7 cards for the starting hand."""

        if not deck:
            logging.warning(f"Initializing player {player_num} with empty deck! Creating minimal fallback deck.")
            # Create minimal fallback deck to avoid crashes
            fallback_deck = ["fallback_card_1", "fallback_card_2", "fallback_card_3", 
                            "fallback_card_4", "fallback_card_5", "fallback_card_6", 
                            "fallback_card_7", "fallback_card_8", "fallback_card_9"]
            for i, card_id in enumerate(fallback_deck):
                if card_id not in self.card_db:
                    # Create minimal card
                    self.card_db[card_id] = Card({
                        "name": f"Fallback Card {i+1}",
                        "type_line": "Creature",
                        "card_types": ["creature"],
                        "power": 1,
                        "toughness": 1,
                        "mana_cost": "{1}",
                        "cmc": 1,
                        "colors": [0,0,0,0,0],
                        "keywords": [0]*11,
                        "subtypes": [],
                        "oracle_text": ""
                    })
            deck = fallback_deck

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
            "targeted_permanents_this_turn": set(), # Battlefield objects targeted by this player this turn
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
            "linked_exile": {}, # Source ID -> cards exiled until that source leaves
            "ciphered_spells": {}, # For Cipher mechanic {creature_id: spell_id}
            "haunted_by": {}, # For Haunt mechanic {haunted_id: [haunter_id,...]}
            "hideaway_cards": {}, # For Hideaway mechanic {land_id: exiled_card_id}
            "mutation_stacks": {}, # For Mutate {base_creature_id: [top_card_id, ..., base_card_id]}
            "name": f"Player {player_num}" # Set name based on player number
        }
        
        # Ensure library exists and has cards
        if not player["library"]:
            logging.error(f"Critical error: Player {player_num} has empty library after initialization!")
            return player
            
        random.shuffle(player["library"])

        # Draw 7 cards, handling case where library has fewer than 7 cards
        cards_to_draw = min(7, len(player["library"]))
        for _ in range(cards_to_draw):
            if player["library"]:
                player["hand"].append(player["library"].pop(0))
            else:
                logging.warning(f"Not enough cards in Player {player_num}'s deck to draw 7 cards!")
                break
                
        return player
        
    
        
    
        
        




    
            
            
    
    
    
            
        
        
    
    


        
    





    

    
            

    
                

    
    
                        
        

    


        
    

    






        

        
                
                


                



        


            
    





        # Clear exhaust status (typically done at start of controller's untap)
        # Let's move exhaust clear to UNTAP phase
        # self.exhaust_ability_used.clear()




        

    def clone(self):
        """
        Create a deep copy of the game state for lookahead simulation.
        Handles deep copying mutable state, re-linking subsystems, and
        correcting object references within the cloned state.
        """

        logging.debug("Cloning GameState starting...")
        # 1. Create a new instance with basic parameters (card_db is shared reference)
        cloned_state = GameState(self.card_db, self.max_turns, self.max_hand_size, self.max_battlefield)

        # --- Copy Primitive/Immutable Attributes ---
        # List all attributes expected to be simple types (int, float, bool, str, None)
        primitive_attrs = [
            "turn", "phase", "_last_turn_phase", "agent_is_p1", "combat_damage_dealt", "day_night_state",
            "day_night_checked_this_turn", "priority_pass_count", "last_stack_size",
            "previous_priority_phase", "mulligan_in_progress", "bottoming_in_progress",
            "cards_to_bottom", "bottoming_count", "split_second_active", "gravestorm_count",
            "miracle_active", "miracle_card_id", "miracle_cost",
            "progress_was_forced", "_turn_limit_checked", "_phase_action_count"
        ]
        # Delayed triggers hold closures over THIS state; firing them from a
        # clone would mutate the original game. Clones start with none (v1
        # limitation: pending delayed triggers are invisible to lookahead).
        cloned_state.delayed_triggers = []
        cloned_state.delayed_event_triggers = copy.deepcopy(
            getattr(self, "delayed_event_triggers", []))
        for attr in primitive_attrs:
            if hasattr(self, attr):
                setattr(cloned_state, attr, getattr(self, attr))
            # else: # Attribute might not exist, skip silently or log warning if expected
            #     logging.warning(f"Clone: Primitive attribute '{attr}' not found on original state.")

        # --- Deep Copy Player States (MUST be deep) ---
        try:
            cloned_state.p1 = copy.deepcopy(self.p1) if self.p1 else None
            cloned_state.p2 = copy.deepcopy(self.p2) if self.p2 else None
            logging.debug("Cloned players p1 and p2.")
        except Exception as e:
            logging.error(f"CRITICAL Error deep copying player states: {e}", exc_info=True)
            # If players fail to copy, clone is likely unusable
            return None

        # --- Deep Copy Other Mutable Top-Level Attributes ---
        # Use deepcopy for dictionaries and lists/sets that might contain mutable items or need full separation.
        # Use shallow copy (.copy() or [:]) only if absolutely sure elements are immutable (like IDs) AND no nested mutables exist.
        mutable_attrs_deepcopy = [
            "stack", "current_block_assignments", "exhaust_ability_used", "_last_card_locations",
            "_ceased_token_cards",
            "impending_cards", "_offspring_cost_paid_context", "until_end_of_turn_effects",
            "temp_control_effects", "abilities_activated_this_turn", "spells_cast_this_turn",
            "cards_played", "damage_dealt_this_turn", "cards_drawn_this_turn",
            "life_gained_this_turn", "damage_this_turn", "cards_to_graveyard_this_turn",
            "saga_counters", "battle_cards", "suspended_cards", "rebounded_cards", "phased_out_state",
            "melded_permanents", "mutated_permanents", "specialized_cards", "last_die_roll", "die_roll_history",
            "foretold_cards", "epic_spells", "morphed_cards", "manifested_cards",
            "copy_overrides", "plotted_cards",
            "planeswalker_attack_targets", "battle_attack_targets", "planeswalker_protectors",
            "mulligan_count", "mulligan_data" # Dicts need deepcopy
            # Contexts will be handled separately due to player references
        ]
        mutable_attrs_copy = [ # Attributes safe for shallow copy (typically sets/lists of IDs/primitives)
            "current_attackers", "attackers_this_turn", "adventure_cards", "mdfc_cards",
            "cards_castable_from_exile", "cast_as_back_face", "phased_out", "kicked_cards",
            "evoked_cards", "blitz_cards", "dash_cards", "unearthed_cards", "jump_start_cards",
            "buyback_cards", "flashback_cards", "exile_at_end_of_combat", "haste_until_eot",
            "banding_creatures", "crewed_vehicles", "boast_activated", "forecast_used",
            "myriad_tokens", "persist_returned", "undying_returned",
            "_phase_history", # List of simple ints
            "first_strike_ordering", # List of simple IDs likely
            # Logging sets (copying is fine, doesn't affect game logic)
             "_logged_card_ids", "_logged_errors"
        ]

        for attr in mutable_attrs_deepcopy:
            if hasattr(self, attr):
                try:
                    setattr(cloned_state, attr, copy.deepcopy(getattr(self, attr)))
                except Exception as e:
                    logging.error(f"Error deep copying attribute '{attr}': {e}")
                    setattr(cloned_state, attr, {} if isinstance(getattr(self, attr), dict) else []) # Fallback empty
            # else: # Attribute might not exist
            #     logging.debug(f"Clone: Mutable attribute '{attr}' not found on original state.")

        for attr in mutable_attrs_copy:
            if hasattr(self, attr):
                val = getattr(self, attr)
                try:
                    if isinstance(val, list): setattr(cloned_state, attr, val[:])
                    elif isinstance(val, set): setattr(cloned_state, attr, val.copy())
                    # Add dict if safe: elif isinstance(val, dict): setattr(cloned_state, attr, val.copy())
                    else: setattr(cloned_state, attr, val) # Should not happen based on list contents
                except Exception as e:
                    logging.error(f"Error copying attribute '{attr}': {e}")
                    setattr(cloned_state, attr, [] if isinstance(val, (list, set)) else {}) # Fallback empty

        # --- Special Handling for Original Decks (Shallow list copy is fine) ---
        cloned_state.original_p1_deck = self.original_p1_deck[:] if hasattr(self,'original_p1_deck') else []
        cloned_state.original_p2_deck = self.original_p2_deck[:] if hasattr(self,'original_p2_deck') else []

        # --- Deep Copy Contexts and Fix Player References ---
        logging.debug("Cloning contexts and fixing player references...")
        context_attrs = ["targeting_context", "sacrifice_context", "choice_context",
                         "spree_context", "dredge_pending", "madness_cast_available",
                         "pending_spell_context", "clash_context"]
        context_keys_with_player_refs = ["controller", "player", "target_obj", "activator",
                                    "original_caster", "player_gaining_life", # Add keys that hold player refs
                                    "from_player", "to_player", # For move_card context if stored
                                    # Specific context player references:
                                    "mulligan_player", "bottoming_player", "surveiling_player", "scrying_player"
                                   ]

        for attr in context_attrs:
            if hasattr(self, attr):
                 orig_ctx = getattr(self, attr)
                 if orig_ctx:
                      try:
                           cloned_ctx = copy.deepcopy(orig_ctx)
                           # Fix player references within the copied context dict
                           if isinstance(cloned_ctx, dict):
                               for key in context_keys_with_player_refs:
                                   if key in cloned_ctx and cloned_ctx[key] is not None:
                                       # Handle direct player objects or potentially player IDs ('p1', 'p2')
                                       if cloned_ctx[key] == self.p1: cloned_ctx[key] = cloned_state.p1
                                       elif cloned_ctx[key] == self.p2: cloned_ctx[key] = cloned_state.p2
                                       # Add checks for 'p1'/'p2' string IDs if needed
                                       # else if cloned_ctx[key] == 'p1': # No change needed if storing IDs
                                       # else if cloned_ctx[key] == 'p2': # No change needed
                                       else: # Potential complex object reference? Keep for now unless known error.
                                           pass # logging.debug(f"Keeping non-player object reference in context '{attr}' key '{key}': {type(cloned_ctx[key])}")
                               # Fix ability references if contexts store them
                               if 'ability' in cloned_ctx and cloned_ctx['ability'] is not None:
                                    # Ability objects are hard to deep copy correctly.
                                    # Best approach: Remove from context, rely on resolution re-finding it.
                                    # OR: Store minimal info (card_id, ability_idx) instead of object.
                                    logging.debug(f"Removing Ability object reference from cloned context '{attr}'.")
                                    cloned_ctx['ability'] = None # Clear complex object reference
                           setattr(cloned_state, attr, cloned_ctx)
                      except Exception as e:
                           logging.error(f"Error deep copying context '{attr}': {e}", exc_info=True)
                           setattr(cloned_state, attr, None) # Set to None on error
                 else:
                      setattr(cloned_state, attr, None)


        # --- Fix Top-Level Player References ---
        player_ref_attrs = ["priority_player", "mulligan_player", "bottoming_player",
                            "miracle_player", "surveiling_player", "scrying_player"]
        for attr in player_ref_attrs:
             if hasattr(self, attr):
                 orig_player = getattr(self, attr)
                 cloned_player = None
                 if orig_player == self.p1: cloned_player = cloned_state.p1
                 elif orig_player == self.p2: cloned_player = cloned_state.p2
                 setattr(cloned_state, attr, cloned_player)

        # --- IMPORTANT: Re-initialize Subsystems LINKED TO THE CLONE ---
        # Subsystems must be created *after* players and basic state are copied.
        logging.debug("Re-initializing subsystems for the clone...")
        cloned_state._init_subsystems(reset_tracking=False) # Preserve copied game/player state

        # --- Copy Subsystem STATE (The most complex part) ---
        logging.debug("Copying subsystem states...")
        # Layer System (Deep copy effects, fix player references)
        if cloned_state.layer_system and self.layer_system and hasattr(self.layer_system, 'layers'):
            try:
                cloned_state.layer_system.layers = {} # Start fresh
                cloned_state.layer_system.effect_counter = self.layer_system.effect_counter # Copy simple counter
                cloned_state.layer_system._last_applied_state_hash = self.layer_system._last_applied_state_hash
                cloned_state.layer_system.timestamps = self.layer_system.timestamps.copy()
                cloned_state.layer_system.dependencies = copy.deepcopy(self.layer_system.dependencies)

                for layer_num, effects_dict_or_list in self.layer_system.layers.items():
                    if isinstance(effects_dict_or_list, dict): # Layer 7 has sublayers
                        cloned_state.layer_system.layers[layer_num] = defaultdict(list) # Recreate defaultdict
                        for sublayer, effects_list in effects_dict_or_list.items():
                             cloned_sublayer = []
                             for eid, data in effects_list:
                                  copied_data = copy.deepcopy(data)
                                  # Fix player references
                                  ref_keys = ['_controller', 'controller_id', 'player', 'owner', 'new_controller']
                                  for key in ref_keys:
                                       if key in copied_data and copied_data[key] is not None:
                                            orig_player = copied_data[key]
                                            copied_data[key] = cloned_state.p1 if orig_player == self.p1 else cloned_state.p2 if orig_player == self.p2 else None
                                  cloned_sublayer.append((eid, copied_data))
                             cloned_state.layer_system.layers[layer_num][sublayer] = cloned_sublayer
                    else: # Layers 1-6 are lists
                        cloned_layer = []
                        for eid, data in effects_dict_or_list:
                             copied_data = copy.deepcopy(data)
                             # Fix player references
                             ref_keys = ['_controller', 'controller_id', 'player', 'owner', 'new_controller']
                             for key in ref_keys:
                                  if key in copied_data and copied_data[key] is not None:
                                       orig_player = copied_data[key]
                                       copied_data[key] = cloned_state.p1 if orig_player == self.p1 else cloned_state.p2 if orig_player == self.p2 else None
                             cloned_layer.append((eid, copied_data))
                        cloned_state.layer_system.layers[layer_num] = cloned_layer
                logging.debug("Cloned LayerSystem state.")
            except Exception as e:
                 logging.error(f"Error cloning LayerSystem state: {e}", exc_info=True)

        # Replacement Effects (Deep copy effects, fix player references)
        if cloned_state.replacement_effects and self.replacement_effects:
            try:
                cloned_state.replacement_effects.active_effects = [] # Reset
                cloned_state.replacement_effects.effect_index = defaultdict(list) # Reset
                cloned_state.replacement_effects.effect_counter = self.replacement_effects.effect_counter # Copy counter

                for data in self.replacement_effects.active_effects:
                    copied_data = copy.deepcopy(data)
                    ref_keys = ['_controller', 'controller_id', 'player', 'owner']
                    for key in ref_keys:
                         if key in copied_data and copied_data[key] is not None:
                              orig_player = copied_data[key]
                              copied_data[key] = cloned_state.p1 if orig_player == self.p1 else cloned_state.p2 if orig_player == self.p2 else None
                    # Re-register in the cloned system to build index correctly
                    cloned_state.replacement_effects.active_effects.append(copied_data)
                    event_type = copied_data.get('event_type')
                    if event_type:
                        cloned_state.replacement_effects.effect_index[event_type].append(copied_data)
                logging.debug("Cloned ReplacementEffects state.")
            except Exception as e:
                 logging.error(f"Error cloning ReplacementEffects state: {e}", exc_info=True)

        # Ability Handler (Repopulate Abilities - safest)
        if cloned_state.ability_handler and self.ability_handler:
            logging.debug("Repopulating AbilityHandler registered abilities for clone...")
            try:
                 cloned_state.ability_handler.registered_abilities = {} # Clear default
                 for player in [cloned_state.p1, cloned_state.p2]:
                     if player:
                         # Check all potential zones for permanents with abilities
                         zones_to_check = ["battlefield", "graveyard"] # Add hand/exile if abilities can function there
                         for zone in zones_to_check:
                             for card_id in list(player.get(zone, [])): # Iterate copy
                                  card = cloned_state._safe_get_card(card_id) # Use clone's DB ref
                                  if card:
                                      # Ensure card object points to CLONED state
                                      setattr(card, 'game_state', cloned_state)
                                      cloned_state.ability_handler._parse_and_register_abilities(card_id, card)

                 # Copy active triggers, fixing controller references
                 cloned_state.ability_handler.active_triggers = []
                 if hasattr(self.ability_handler, 'active_triggers'):
                     for trigger_item in self.ability_handler.active_triggers:
                         if isinstance(trigger_item, tuple) and len(trigger_item) >= 2:
                             ability, controller_orig = trigger_item[:2]
                             context_orig = trigger_item[2] if len(trigger_item) > 2 else {}
                             controller_cloned = cloned_state.p1 if controller_orig == self.p1 else cloned_state.p2 if controller_orig == self.p2 else None
                             if controller_cloned:
                                 # Ability objects themselves are complex to deepcopy, reference original for now
                                 # Context needs deepcopy + player reference fixing
                                 context_cloned = copy.deepcopy(context_orig)
                                 ref_keys = ['controller', 'player', 'owner', 'activator', 'event_card', 'source_card'] # Look for player or card object refs
                                 for key in ref_keys:
                                     if key in context_cloned and context_cloned[key] is not None:
                                         if context_cloned[key] == self.p1: context_cloned[key] = cloned_state.p1
                                         elif context_cloned[key] == self.p2: context_cloned[key] = cloned_state.p2
                                         elif isinstance(context_cloned[key], Card):
                                             # Card objects are generally okay to shallow copy (point to DB template)
                                             pass
                                 cloned_state.ability_handler.active_triggers.append((ability, controller_cloned, context_cloned))
                     logging.debug(f"Copied {len(cloned_state.ability_handler.active_triggers)} active triggers to clone.")

            except Exception as e:
                 logging.error(f"Error cloning AbilityHandler state: {e}", exc_info=True)


        # Other subsystems: Most likely stateless or state handled elsewhere (e.g., player mana pool for ManaSystem)
        # Just ensure they are linked to the CLONED state (done by _init_subsystems)
        if cloned_state.mana_system: cloned_state.mana_system.game_state = cloned_state
        if cloned_state.combat_resolver: cloned_state.combat_resolver.game_state = cloned_state
        if cloned_state.targeting_system: cloned_state.targeting_system.game_state = cloned_state
        if cloned_state.card_evaluator: cloned_state.card_evaluator.game_state = cloned_state
        if cloned_state.strategic_planner: cloned_state.strategic_planner.game_state = cloned_state

        # ActionHandler *must* be the one created by the clone's _init_subsystems
        if cloned_state.action_handler:
            cloned_state.action_handler.game_state = cloned_state
            # Link combat handler if needed (created by ActionHandler init)
            if hasattr(cloned_state.action_handler, 'combat_handler') and cloned_state.action_handler.combat_handler:
                cloned_state.action_handler.combat_handler.game_state = cloned_state
        else:
             logging.error("CRITICAL: Clone ActionHandler is None after subsystem initialization!")


        # --- Link External Systems (Reference copy) ---
        cloned_state.strategy_memory = self.strategy_memory
        cloned_state.stats_tracker = self.stats_tracker
        cloned_state.card_memory = self.card_memory
        # Update internal references in cloned subsystems if they use external trackers
        if cloned_state.card_evaluator:
             cloned_state.card_evaluator.stats_tracker = cloned_state.stats_tracker
             cloned_state.card_evaluator.card_memory = cloned_state.card_memory
        if cloned_state.strategic_planner and hasattr(cloned_state.strategic_planner, 'strategy_memory'):
             cloned_state.strategic_planner.strategy_memory = cloned_state.strategy_memory


        # Re-apply layers on the clone to ensure all characteristics are correct
        # Crucial if card objects weren't deep copied or if repopulating abilities happened
        if cloned_state.layer_system:
             logging.debug("Applying all layer effects on the cloned state...")
             cloned_state.layer_system.apply_all_effects()


        logging.info("GameState cloned successfully.")
        return cloned_state
 
    def count_dynamic_quantity(self, expr, controller):
        """Count a "number of X you control / in your graveyard" style quantity.

        Shared by variable draw/life/pump effects (July 2026 parser expansion).
        expr is a lowercased noun phrase such as "creatures you control",
        "lands you control", "cards in your graveyard", "mountains you control".
        Returns an int (0 if nothing matches).
        """
        expr = (expr or "").lower().strip()
        p = controller
        opp = self.p2 if controller == self.p1 else self.p1

        # "creature(s) that died under your control this turn" (Callous
        # Sell-Sword) reads the per-player death tracking reset each turn.
        if re.search(r"creatures? that died under your control this turn", expr):
            died_key = "p1" if controller is self.p1 else (
                "p2" if controller is self.p2 else None)
            tracking = getattr(self, "creatures_died_this_turn", None) or {}
            return tracking.get(died_key, 0) if died_key else 0

        # Domain: "for each basic land type among lands you control" counts
        # DISTINCT basic land types, not lands. Nonbasic duals contribute each
        # printed basic land type. Must be checked before the generic "land"
        # branch below, which would count lands instead. Mirrors the counting
        # the mana system already uses for Domain cost reductions.
        if "basic land type" in expr:
            basic_types = {"plains", "island", "swamp", "mountain", "forest"}
            controlled_types = set()
            for cid in p.get("battlefield", []):
                c = self._safe_get_card(cid)
                if not c or 'land' not in [t.lower() for t in getattr(c, 'card_types', [])]:
                    continue
                controlled_types.update(
                    basic_types.intersection(
                        str(s).lower() for s in getattr(c, 'subtypes', [])))
            return len(controlled_types)

        def _types(cid):
            c = self._safe_get_card(cid)
            return [t.lower() for t in getattr(c, 'card_types', [])] if c else []

        def _subtypes(cid):
            c = self._safe_get_card(cid)
            return [t.lower() for t in getattr(c, 'subtypes', [])] if c else []

        # Graveyard counts.
        if "in your graveyard" in expr or "cards in your graveyard" in expr:
            zone = p.get("graveyard", [])
            if "creature" in expr:
                return sum(1 for c in zone if 'creature' in _types(c))
            return len(zone)
        if "in each graveyard" in expr or "in all graveyards" in expr:
            return len(p.get("graveyard", [])) + len(opp.get("graveyard", []))

        # Battlefield "you control" / "target player controls" / "opponents control".
        target = p
        if "you control" in expr or "under your control" in expr:
            target = p
        elif "an opponent controls" in expr or "opponents control" in expr or "your opponents control" in expr:
            target = opp

        bf = target.get("battlefield", [])
        # Specific creature/land/artifact/etc, or a subtype like Mountains/Elves.
        if "creature" in expr:
            return sum(1 for c in bf if 'creature' in _types(c))
        if "land" in expr and "island" not in expr and "mountain" not in expr and "forest" not in expr and "swamp" not in expr and "plains" not in expr:
            return sum(1 for c in bf if 'land' in _types(c))
        if "artifact" in expr:
            return sum(1 for c in bf if 'artifact' in _types(c))
        if "enchantment" in expr:
            return sum(1 for c in bf if 'enchantment' in _types(c))
        # Basic land subtypes and other subtypes ("Mountains you control").
        for sub in ("mountain", "island", "forest", "swamp", "plains", "elf", "goblin", "zombie", "soldier"):
            if sub + "s" in expr or (sub in expr):
                return sum(1 for c in bf if sub in _subtypes(c))
        # Fallback: total permanents controlled.
        if "permanent" in expr:
            return len(bf)
        return 0

    def _is_creature(self, card_id):
        """Whether a card id currently refers to a creature.

        Phantom-method fix (July 2026): this was called from at least 6 sites
        (BuffEffect/GainKeywordEffect/SacrificeEffect target selection, and the
        environment's my_dead_creatures / opp_dead_creatures OBSERVATIONS) but
        never existed -- every call returned an AttributeError that callers
        swallowed, so anthem/pump target sets silently excluded all creatures
        and the agent's dead-creature observations were always zero.
        """
        card = self._safe_get_card(card_id)
        if not card:
            return False
        return 'creature' in [t.lower() for t in getattr(card, 'card_types', [])]

    def _safe_get_card(self, card_id, default_value=None):
        """Safely get a card with proper error handling and type checking"""
        try:
            # ``None`` represents the absence of a source/choice throughout the
            # engine. Turning it into a truthy synthetic Card corrupts feature
            # checks and produces phantom card behavior.
            if card_id is None:
                return default_value
            # Handle case where card_id is itself already a Card object
            if isinstance(card_id, Card):
                return card_id
                    
            # Use standardized dictionary format
            if card_id in self.card_db:
                return self.card_db[card_id]

            # Tokens cease to exist immediately after leaving the
            # battlefield. Delayed triggers may still carry their last-known
            # ID, which is valid event context and must not be reported as a
            # corrupt database lookup. Keep warning if the ID is somehow still
            # present in a live zone/stack, because that would be a real bug.
            if isinstance(card_id, str) and card_id.startswith("TOKEN_"):
                ceased_card = self._ceased_token_cards.get(card_id)
                if ceased_card is not None:
                    return ceased_card
                live_zone_reference = any(
                    card_id in player.get(zone, ())
                    for player in (self.p1, self.p2) if player is not None
                    for zone in ("library", "hand", "battlefield",
                                 "graveyard", "exile")
                )
                live_stack_reference = any(
                    isinstance(item, tuple) and len(item) > 1
                    and item[1] == card_id
                    for item in self.stack
                )
                if not live_zone_reference and not live_stack_reference:
                    return default_value
                    
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
            
        

        
            
    
    







    
    
    
    

        

    


    
        
        
    
        
        


    

    











        
        


     

    

    
    


    








     





    
    
    

            
