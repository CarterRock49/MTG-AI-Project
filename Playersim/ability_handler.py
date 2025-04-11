import logging

import numpy as np
# Remove KeywordEffects if not used directly after refactoring
# from .keyword_effects import KeywordEffects
from .ability_types import Ability, ActivatedAbility, TriggeredAbility, StaticAbility, ManaAbility, AbilityEffect
import re
from collections import defaultdict
from .card import Card
from .ability_utils import EffectFactory
# *** CHANGED: Import TargetingSystem from its new file ***
from .targeting import TargetingSystem # Import TargetingSystem from targeting.py

class AbilityHandler:
    """Handles card abilities and special effects"""


    def __init__(self, game_state=None):
        self.game_state = game_state
        self.registered_abilities = {} # {card_id: [Ability, ...]} Stores parsed abilities for quick lookup
        self.active_triggers = [] # Stores (Ability, controller) tuples to be processed on stack
        # *** CHANGED: Initialize targeting_system reference directly ***
        # Initialize targeting_system reference as None initially
        self.targeting_system = None

        if game_state is not None:
            # Ensure GameState has a reference to this handler if needed
            if not hasattr(game_state, 'ability_handler'):
                game_state.ability_handler = self

            # --- Initialize TargetingSystem and link it ---
            try:
                # Assuming targeting.py is in the same directory or PYTHONPATH
                from .targeting import TargetingSystem
                self.targeting_system = TargetingSystem(game_state)
                # Link back to game_state if it doesn't have one yet
                if not hasattr(game_state, 'targeting_system'):
                     game_state.targeting_system = self.targeting_system
                logging.debug("TargetingSystem initialized in AbilityHandler and linked to GameState.")
            except ImportError:
                 logging.error("TargetingSystem module not found (targeting.py). Targeting will be limited.")
            except Exception as e:
                 logging.error(f"Error initializing TargetingSystem in AbilityHandler: {e}")

            # Initialize abilities AFTER subsystems are ready
            # Let GameState._init_subsystems call this handler's _initialize_abilities
            # self._initialize_abilities() # Call is removed, GS handles initialization order now.
        else:
             logging.warning("AbilityHandler initialized without a GameState reference.")


    def handle_class_level_up(self, class_idx):
        """
        Handle leveling up a Class card with proper trigger processing.
        Includes mana cost payment and trigger processing.

        Args:
            class_idx: Index of the Class card in the controller's battlefield.

        Returns:
            bool: True if the class was successfully leveled up.
        """
        gs = self.game_state
        active_player = gs._get_active_player() # Use GS helper method

        # Validate index
        if not (0 <= class_idx < len(active_player.get("battlefield", []))): # Use get
            logging.warning(f"Invalid class index: {class_idx}")
            return False

        # Get the class card
        class_id = active_player["battlefield"][class_idx]
        class_card = gs._safe_get_card(class_id)

        # Verify it's a valid, levelable Class card
        if not class_card or not hasattr(class_card, 'is_class') or not class_card.is_class:
            logging.warning(f"Card {class_id} (index {class_idx}) is not a Class.")
            return False
        # *** FIXED typo card_card -> class_card ***
        if not hasattr(class_card, 'can_level_up') or not class_card.can_level_up():
            logging.warning(f"Class {class_card.name} cannot level up further.")
            return False

        # Determine next level and cost
        next_level = getattr(class_card, 'current_level', 1) + 1 # Default current level is 1 if not set
        level_cost_str = class_card.get_level_cost(next_level) # Assumes this method exists on Card object

        if not level_cost_str:
            logging.warning(f"No cost found for level {next_level} of {class_card.name}.")
            return False

        # Check affordability using ManaSystem
        parsed_cost_dict = None # Renamed for clarity
        if hasattr(gs, 'mana_system') and gs.mana_system:
            # Use mana system to parse and check affordability
            try:
                parsed_cost_dict = gs.mana_system.parse_mana_cost(level_cost_str)
                if not gs.mana_system.can_pay_mana_cost(active_player, parsed_cost_dict):
                    logging.debug(f"Cannot afford to level up {class_card.name} (Cost: {level_cost_str})")
                    return False
            except Exception as e:
                logging.error(f"Error checking mana cost for level up: {e}")
                return False
        else:
            logging.warning("Mana system not found, cannot check level-up affordability.")
            return False # Cannot proceed without mana system

        # --- Pay the cost using ManaSystem ---
        # Pass the parsed dict for efficiency
        if not gs.mana_system.pay_mana_cost(active_player, parsed_cost_dict): # Pass parsed cost
            logging.warning(f"Failed to pay level-up cost {level_cost_str} for {class_card.name}")
            # Mana system should handle rollback internally if needed
            return False

        # Level up the class (Card object handles its state change)
        previous_level = getattr(class_card, 'current_level', 1)
        success = False
        if hasattr(class_card, 'level_up'): # Ensure method exists
            success = class_card.level_up() # This should update current_level and potentially P/T, type

        if success:
            logging.info(f"Leveled up {class_card.name} from level {previous_level} to {next_level}")

            # Re-register abilities for the new level
            # parse_and_register clears old ones first
            self._parse_and_register_abilities(class_id, class_card)

            # Trigger level-up event using unified check_abilities
            context = {
                "class_id": class_id,
                "previous_level": previous_level,
                "new_level": next_level,
                "controller": active_player
            }
            # Trigger a generic level up event
            self.check_abilities(class_id, "LEVEL_UP", context)
            # Trigger a specific class level up event
            self.check_abilities(class_id, "CLASS_LEVEL_UP", context) # Event origin is the class itself


            # Check if it became a creature *at this level* (handled by Card.level_up or here)
            # Example: Card might gain 'creature' type now. Ensure Layer System updates.
            if hasattr(gs, 'layer_system') and gs.layer_system:
                 gs.layer_system.invalidate_cache() # Force recalculation
                 # gs.layer_system.apply_all_effects() # Redundant if applying below

            gs.phase = gs.PHASE_PRIORITY # Reset priority
            # Let the game loop handle layer application before next action

        return success
        
    def handle_unlock_door(self, room_idx):
        """
        Handle unlocking a door on a Room card with proper trigger processing.
        Includes mana cost payment and trigger processing.

        Args:
            room_idx: Index of the Room card in the controller's battlefield.

        Returns:
            bool: True if door was unlocked successfully.
        """
        gs = self.game_state
        active_player = gs._get_active_player() # Use GS helper method

        # Validate index
        if not (0 <= room_idx < len(active_player.get("battlefield", []))): # Use get
            logging.warning(f"Invalid room index: {room_idx}")
            return False

        # Get the room card
        room_id = active_player["battlefield"][room_idx]
        room_card = gs._safe_get_card(room_id)

        # Verify it's a Room card
        # *** FIXED typo card_room -> room_card ***
        if not room_card or not hasattr(room_card, 'is_room') or not room_card.is_room:
            logging.warning(f"Card {room_id} (index {room_idx}) is not a Room.")
            return False

        # --- Determine which door to unlock ---
        # Assume action always targets the *next* locked door sequentially, or door 2 if available?
        # For simplicity, let's try door 2 if it exists and is locked. If not, try door 1.
        door_to_unlock_num = None
        door_data = None

        if hasattr(room_card, 'door2') and not getattr(room_card,'door2',{}).get('unlocked', False):
            door_to_unlock_num = 2
            door_data = room_card.door2
        elif hasattr(room_card, 'door1') and not getattr(room_card,'door1',{}).get('unlocked', False):
             # Note: Door 1 unlocking might not always be an explicit action
             # If door 1 has a cost, we can proceed. If not, it might unlock implicitly.
             door1_cost = getattr(room_card, 'door1', {}).get('mana_cost')
             if door1_cost:
                 door_to_unlock_num = 1
                 door_data = room_card.door1
             else:
                 logging.debug(f"Door 1 of {room_card.name} has no cost, assumed implicitly unlocked or via other means.")
                 # If Door 2 also doesn't exist or is unlocked, no action possible.
                 if not hasattr(room_card, 'door2') or getattr(room_card,'door2',{}).get('unlocked', False):
                     logging.warning(f"No available doors to unlock for {room_card.name}.")
                     return False
        # Check if door 1 is already unlocked but door 2 is not (this block is needed if logic above doesn't catch it)
        elif hasattr(room_card, 'door2') and not getattr(room_card,'door2',{}).get('unlocked', False):
            door_to_unlock_num = 2
            door_data = room_card.door2


        if door_to_unlock_num is None:
             logging.warning(f"No lockable door found or selected for {room_card.name}.")
             return False

        # --- Get Door Cost ---
        door_cost_str = door_data.get('mana_cost', '')
        if not door_cost_str:
            logging.warning(f"No mana cost defined for Door {door_to_unlock_num} of {room_card.name}. Assuming free?")
            door_cost_str = "{0}" # Treat as free if no cost defined

        # Check affordability using ManaSystem
        parsed_cost_dict = None # Renamed for clarity
        if hasattr(gs, 'mana_system') and gs.mana_system:
             try:
                 parsed_cost_dict = gs.mana_system.parse_mana_cost(door_cost_str)
                 if not gs.mana_system.can_pay_mana_cost(active_player, parsed_cost_dict):
                     logging.debug(f"Cannot afford to unlock Door {door_to_unlock_num} for {room_card.name} (Cost: {door_cost_str})")
                     return False
             except Exception as e:
                  logging.error(f"Error checking mana cost for door unlock: {e}")
                  return False
        else:
            logging.warning("Mana system not found, cannot check door unlock affordability.")
            return False # Cannot proceed without mana system

        # --- Pay the cost ---
        if not gs.mana_system.pay_mana_cost(active_player, parsed_cost_dict): # Pass parsed cost
            logging.warning(f"Failed to pay unlock cost {door_cost_str} for Door {door_to_unlock_num} of {room_card.name}")
            return False

        # Unlock the door (Update the card's state)
        door_data['unlocked'] = True
        logging.info(f"Unlocked Door {door_to_unlock_num} for Room {room_card.name}")

        # --- Check if fully unlocked ---
        fully_unlocked = True
        for n in [1, 2]: # Check standard door numbers
             door_attr = f"door{n}"
             if hasattr(room_card, door_attr):
                  # Safely get nested attribute
                  if not getattr(getattr(room_card, door_attr, {}),'unlocked', False):
                       fully_unlocked = False; break

        # --- Prepare context for triggers ---
        context = {
            "door_number": door_to_unlock_num,
            "room_id": room_id,
            "controller": active_player,
            "cost_paid": door_cost_str,
            "fully_unlocked": fully_unlocked
        }

        # --- Trigger Events ---
        # Generic door unlocked event
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)
        # Specific door unlocked event
        self.check_abilities(room_id, f"DOOR{door_to_unlock_num}_UNLOCKED", context)
        # Room fully unlocked event
        if fully_unlocked:
            self.check_abilities(room_id, "ROOM_FULLY_UNLOCKED", context)

        # --- Handle Chapter Advancement (if applicable) ---
        if hasattr(room_card, 'advance_chapter') and callable(getattr(room_card, 'advance_chapter')):
             chapter_advanced = room_card.advance_chapter() # Card handles its chapter logic
             if chapter_advanced:
                 chapter_context = {
                     "room_id": room_id,
                     "controller": active_player,
                     "chapter": getattr(room_card, 'current_chapter', None)
                 }
                 logging.debug(f"Room {room_card.name} advanced to chapter {chapter_context['chapter']}")
                 # Trigger chapter ability event
                 self.check_abilities(room_id, "CHAPTER_ADVANCED", chapter_context)

                 # Check for completion
                 if hasattr(room_card, 'is_complete') and room_card.is_complete():
                      logging.debug(f"Room {room_card.name} completed.")
                      self.check_abilities(room_id, "ROOM_COMPLETED", chapter_context)


        # Re-register abilities if unlocking changes available effects or state
        self._parse_and_register_abilities(room_id, room_card)
        # Trigger layer update if needed
        if hasattr(gs, 'layer_system') and gs.layer_system:
             gs.layer_system.invalidate_cache()
             # gs.layer_system.apply_all_effects() # Redundant if applying below

        gs.phase = gs.PHASE_PRIORITY # Reset priority
        # Let game loop handle layer application

        return True
    

    def _parse_text_with_patterns(self, text, patterns, ability_type, card_id, card, abilities_list):
            """
            Parse oracle text using regex patterns and create abilities of the specified type.
            Handles reminder text removal and uses Ability subclasses.

            Args:
                text: The oracle text segment to parse (e.g., one line or sentence)
                patterns: List of (pattern, attribute_extractor_func) tuples for regex matching
                ability_type: Type of ability to create ("activated", "triggered", "static", "mana")
                card_id: ID of the card with the ability
                card: Card object
                abilities_list: List to add the created abilities to
            """
            # Pre-process text: Remove reminder text and normalize whitespace
            processed_text = re.sub(r'\s*\([^()]*?\)\s*', ' ', text.lower()).strip() # Remove () and trim
            processed_text = re.sub(r'\s+([:.,;])', r'\1', processed_text) # Remove space before punctuation
            processed_text = re.sub(r'\s+', ' ', processed_text) # Normalize multiple spaces

            if not processed_text: return

            for pattern, extractor_func in patterns:
                matches = re.finditer(pattern, processed_text)
                for match in matches:
                    # Extract attributes using the provided function
                    attributes = extractor_func(match)
                    if not attributes: continue # Skip if extractor didn't find valid parts

                    attributes['card_id'] = card_id
                    attributes['effect_text'] = match.group(0).strip() # Original matched text

                    try:
                        ability = None
                        if ability_type == "activated":
                            # Ensure cost and effect are present
                            if 'cost' in attributes and 'effect' in attributes and attributes['cost'] and attributes['effect']:
                                 # Check for Mana Ability
                                 if "add {" in attributes['effect'] or "add mana" in attributes['effect']:
                                      mana_produced = self._parse_mana_produced(attributes['effect'])
                                      if mana_produced and any(mana_produced.values()):
                                           ability = ManaAbility(mana_produced=mana_produced, **attributes)
                                      else:
                                           # It mentioned adding mana, but parsing failed or yielded none
                                           # Treat as regular activated ability for now
                                           logging.warning(f"Mana parsing failed for: '{attributes['effect']}'")
                                           ability = ActivatedAbility(**attributes)
                                 else: # Normal Activated Ability
                                      ability = ActivatedAbility(**attributes)
                            else: logging.debug(f"Skipped invalid activated ability match: {attributes}")
                        elif ability_type == "triggered":
                             # Ensure trigger_condition and effect are present
                            if 'trigger_condition' in attributes and 'effect' in attributes and attributes['trigger_condition'] and attributes['effect']:
                                ability = TriggeredAbility(**attributes)
                            else: logging.debug(f"Skipped invalid triggered ability match: {attributes}")
                        elif ability_type == "static":
                             # Ensure effect is present
                            if 'effect' in attributes and attributes['effect']:
                                ability = StaticAbility(**attributes)
                            else: logging.debug(f"Skipped invalid static ability match: {attributes}")
                        elif ability_type == "mana": # Explicit Mana Ability check (redundant?)
                             if 'cost' in attributes and 'mana_produced' in attributes and any(attributes['mana_produced'].values()):
                                 ability = ManaAbility(**attributes)
                             else: logging.debug(f"Skipped invalid mana ability match: {attributes}")

                        if ability:
                            # Link the card object to the ability
                            setattr(ability, 'source_card', card)
                            abilities_list.append(ability)
                            # Debug log specific to the created type
                            logging.debug(f"Registered {type(ability).__name__} for {card.name}: {ability.effect_text}")

                    except TypeError as te:
                        logging.error(f"Error creating {ability_type} ability from attributes {attributes} for card {card.name}. Error: {te}")
                    except Exception as e:
                        logging.error(f"Unexpected error creating ability for {card.name}: {e}")
                    
    def _get_complex_trigger_phrases(self):
        """Get mapping of complex trigger phrases to event types."""
        return {
            "whenever you gain life": "GAIN_LIFE",
            "whenever you lose life": "LOSE_LIFE",
            "whenever a creature you control dies": "CREATURE_DIED",
            "whenever a land enters the battlefield under your control": "LAND_PLAYED",
            "whenever you cast your first spell each turn": "CAST_SPELL",
            "whenever a spell or ability an opponent controls targets": "TARGETED",
            "whenever you complete a room": "ROOM_COMPLETED",
            "when this class level increases": "CLASS_LEVEL_UP",
            "when this door is unlocked": "DOOR_UNLOCKED"
        }

    def handle_modal_ability(self, card_id, controller, mode_index, context=None):
        """
        Handle activation of a modal ability, putting the chosen mode onto the stack.
        Assumes cost payment is handled externally or as part of activating the 'main' ability.
        Context may contain the base_ability object.

        Args:
            card_id (str): The ID of the card source.
            controller (dict): The player activating the ability.
            mode_index (int): The index of the chosen mode.
            context (dict, optional): Additional context, may contain 'base_ability'. Defaults to None.

        Returns:
            bool: True if the modal choice was successfully processed and added to the stack.
        """
        gs = self.game_state
        if context is None: context = {}
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            logging.warning(f"Cannot handle modal ability: Card {card_id} not found or has no text.")
            return False

        # Try to get the base ability from context, otherwise find it
        base_ability = context.get("base_ability")
        if not base_ability:
            logging.debug(f"No base_ability in context, searching on card {card.name}...")
            # Fallback: Find the first activated ability with "choose" or potential modal structure
            for ability in self.registered_abilities.get(card_id, []):
                if isinstance(ability, ActivatedAbility):
                     effect_lower = getattr(ability, 'effect', '').lower()
                     if "choose" in effect_lower or '•' in effect_lower: # Basic check for modal marker
                          base_ability = ability
                          logging.debug(f"Found potential base modal ability: {ability.effect_text}")
                          break
            if not base_ability:
                 logging.warning(f"Could not find base modal ability on {card.name}")
                 return False

        # Parse the modes from the base ability's effect text or card's oracle text
        # Use base ability text first, fall back to full card text if needed
        modal_text_source = getattr(base_ability, 'effect', None) or card.oracle_text
        modes = self._parse_modal_text(modal_text_source)

        if not modes or not (0 <= mode_index < len(modes)):
            logging.warning(f"Invalid mode index {mode_index} for {card.name} (Modes: {modes})")
            return False

        # Get the chosen mode text
        mode_effect_text = modes[mode_index]
        logging.debug(f"Chosen mode {mode_index}: '{mode_effect_text}'")

        # Create effects for this mode using EffectFactory
        # Pass base_ability for context if EffectFactory uses it
        mode_effects = EffectFactory.create_effects(mode_effect_text)
        if not mode_effects:
             logging.warning(f"Could not create effects for modal choice '{mode_effect_text}'")
             return False

        # Determine targets specifically for THIS mode if needed
        targets = {}
        if "target" in mode_effect_text.lower():
             if self.targeting_system:
                # Let targeting system handle target resolution for this specific mode text
                # Assume targeting_system uses effect_text to determine target requirements
                targets = self.targeting_system.resolve_targeting(card_id, controller, mode_effect_text)
                # Targeting failure check
                # The resolve_targeting function should return None on failure or if no targets chosen when required
                if targets is None:
                    logging.debug(f"Targeting failed for chosen mode '{mode_effect_text}'.")
                    # Rollback costs? Needs careful state management if cost paid before choice.
                    return False
             else:
                 logging.warning("Targeting required for mode, but TargetingSystem not available.")
                 return False # Cannot proceed without targeting


        # Add the specific mode's resolution to the stack
        gs.add_to_stack("ABILITY", card_id, controller, {
            "ability": base_ability, # Link base ability object
            "is_modal_choice": True,
            "chosen_mode_index": mode_index,
            "effect_text": mode_effect_text, # Use mode's text for resolution
            "targets": targets,              # Targets chosen for this mode
            "effects": mode_effects          # Store pre-created effects for resolution
        })

        logging.info(f"Added chosen modal ability to stack: Mode {mode_index} ('{mode_effect_text}') for {card.name}")
        gs.phase = gs.PHASE_PRIORITY # Ensure priority resets
        return True
    
    def _parse_modal_text(self, text):
        """Parse modal text from card or ability effect text."""
        modes = []
        text_lower = text.lower()

        # Find the start of the modes using various markers
        markers = [
            "choose one —", "choose two —", "choose one or more —",
            "choose up to one —", "choose up to two —", "choose up to three —",
            "choose one or both —", "choose one •"
        ]
        start_index = -1
        marker_len = 0
        for marker in markers:
            idx = text_lower.find(marker)
            if idx != -1:
                start_index = idx
                marker_len = len(marker)
                break

        if start_index == -1: # No standard marker found, maybe just bullet points?
             if '•' in text_lower or '●' in text_lower:
                 # Assume modes start after the first colon or the start of the text if no colon
                 colon_idx = text_lower.find(':')
                 modal_text_start = colon_idx + 1 if colon_idx != -1 else 0
                 modal_text = text[modal_text_start:]
             else:
                 return [] # Cannot reliably parse modes
        else:
            modal_text = text[start_index + marker_len:]

        modal_text = modal_text.strip()

        # Split by bullet points or similar markers
        mode_parts = re.split(r'\s*[•●]\s*', modal_text) # Split by bullet, trim whitespace

        # Clean up modes
        for part in mode_parts:
            # Remove reminder text first
            cleaned_part = re.sub(r'\s*\([^()]*?\)\s*', ' ', part).strip()
            # Remove leading/trailing punctuation and whitespace
            cleaned_part = re.sub(r'^[;,\.]+|[;,\.]+$', '', cleaned_part).strip()
            if cleaned_part:
                modes.append(cleaned_part)

        # Further split based on sentence structure if no bullets found but "choose" was present
        if not modes and ("choose one" in text_lower or "choose two" in text_lower) and start_index != -1:
             # Try splitting by sentence ending punctuation followed by uppercase letter (heuristic)
             # This is less reliable
             sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z•●-])', modal_text) # Split on sentence end + capital letter
             for sentence in sentences:
                  cleaned_sentence = re.sub(r'\s*\([^()]*?\)\s*', ' ', sentence).strip()
                  cleaned_sentence = re.sub(r'^[;,\.]+|[;,\.]+$', '', cleaned_sentence).strip()
                  if cleaned_sentence:
                       modes.append(cleaned_sentence)

        return modes
        
    def register_card_abilities(self, card_id, player):
        """
        Parse and register abilities for a card. Also registers static/replacement effects.
        This is called when a card enters a zone where its abilities might function (e.g., battlefield).
        """
        try:
            card = self.game_state._safe_get_card(card_id)
            if not card:
                logging.warning(f"Cannot register abilities: card {card_id} not found")
                return

            # Ensure card has the correct reference to the GameState
            if hasattr(card, 'game_state') and card.game_state is None:
                 card.game_state = self.game_state

            # 1. Parse and register functional abilities (Activated, Triggered, Static)
            self._parse_and_register_abilities(card_id, card)

            # 2. Register Replacement Effects defined on the card
            if hasattr(self.game_state, 'replacement_effects') and self.game_state.replacement_effects:
                registered_replace_ids = self.game_state.replacement_effects.register_card_replacement_effects(card_id, player)
                if registered_replace_ids: logging.debug(f"Registered {len(registered_replace_ids)} replacement effects for {card.name}")

            # 3. Apply Static Abilities via Layer System
            # The StaticAbility.apply() method called during _parse_and_register... handles registration with LayerSystem.
            # We trigger a recalculation of layers *after* all registration is done.
            if hasattr(self.game_state, 'layer_system'):
                 self.game_state.layer_system.apply_all_effects() # Ensure layers update immediately

        except Exception as e:
            logging.error(f"Error registering abilities for card {card_id} ({getattr(card, 'name', 'Unknown')}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
                
    def unregister_card_abilities(self, card_id):
        """
        Unregister all effects and abilities associated with a card leaving a zone (e.g., battlefield).
        """
        try:
            card_name = getattr(self.game_state._safe_get_card(card_id), 'name', f"Card {card_id}")
            logging.debug(f"Unregistering abilities and effects for {card_name} ({card_id}).")

            # Remove from registered abilities cache
            if card_id in self.registered_abilities:
                del self.registered_abilities[card_id]

            # Remove any pending triggers from this source
            self.active_triggers = [(ab, ctrl) for ab, ctrl in self.active_triggers if ab.card_id != card_id]

            # Remove continuous effects from LayerSystem
            if hasattr(self.game_state, 'layer_system'):
                removed_layer_count = self.game_state.layer_system.remove_effects_by_source(card_id)
                if removed_layer_count > 0: logging.debug(f"Removed {removed_layer_count} continuous effects from {card_name}.")

            # Remove replacement effects
            if hasattr(self.game_state, 'replacement_effects'):
                removed_replace_count = self.game_state.replacement_effects.remove_effects_by_source(card_id)
                if removed_replace_count > 0: logging.debug(f"Removed {removed_replace_count} replacement effects from {card_name}.")

            # Important: Trigger Layer system recalculation after removal
            if hasattr(self.game_state, 'layer_system'):
                 self.game_state.layer_system.apply_all_effects()

        except Exception as e:
            logging.error(f"Error unregistering abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def _initialize_abilities(self):
        """Parse abilities from all cards in the database (typically done at game start)."""
        gs = self.game_state
        if not isinstance(gs.card_db, dict):
            logging.error("GameState card_db is not a dictionary, cannot initialize abilities.")
            return
        logging.debug(f"Initializing abilities for {len(gs.card_db)} cards in database.")
        count = 0
        for card_id, card in gs.card_db.items():
             if card: # Ensure card exists
                 # Ensure card has a link to game_state for context during parsing/evaluation
                 if not hasattr(card, 'game_state') or card.game_state is None:
                      setattr(card, 'game_state', gs)
                 self._parse_and_register_abilities(card_id, card)
                 count += 1
        logging.debug(f"Finished initializing abilities for {count} cards.")
            
    def _parse_and_register_abilities(self, card_id, card):
        """
        Parse a card's text to identify and register its abilities.
        Handles regular cards, Class cards, and MDFCs. Clears previous abilities first.
        Uses _create_keyword_ability for keywords found in text blocks.
        Applies static abilities immediately after parsing. (Revised)
        """
        if not card:
            logging.warning(f"Attempted to parse abilities for non-existent card {card_id}.")
            return

        # Ensure card has game_state reference if missing
        if not hasattr(card, 'game_state') or card.game_state is None:
             setattr(card, 'game_state', self.game_state)

        # Clear existing registered abilities for this card_id to avoid duplication
        self.registered_abilities[card_id] = []
        abilities_list = self.registered_abilities[card_id] # Get reference to the list

        try:
            texts_to_parse = []
            keywords_from_card_data = [] # Keywords explicitly listed on card data

            # --- Get Text Source(s) ---
            current_face_index = getattr(card, 'current_face', 0)
            card_data_source = card # Default to main card object

            if hasattr(card, 'is_class') and card.is_class:
                current_level_data = card.get_current_class_data()
                if current_level_data:
                    texts_to_parse.extend(current_level_data.get('abilities', []))
                    # Handle level up action text
                    if hasattr(card, 'can_level_up') and card.can_level_up():
                        next_level = getattr(card, 'current_level', 1) + 1
                        cost_str = card.get_level_cost(next_level)
                        if cost_str:
                            texts_to_parse.append(f"{cost_str}: Level up to {next_level}.")
                # Keywords usually defined per level? If so, need to get from level_data. Assuming base card for now.
                if hasattr(card, 'keywords'): keywords_from_card_data = card.keywords

            elif hasattr(card, 'faces') and card.faces and current_face_index < len(card.faces):
                 # MDFC or Transforming DFC - use current face
                 face_data = card.faces[current_face_index]
                 card_data_source = face_data # Get attributes from the face dict/object
                 texts_to_parse.append(face_data.get('oracle_text', ''))
                 keywords_from_card_data = face_data.get('keywords', [])
                 # Add transform action if applicable
                 if card.can_transform(self.game_state):
                     transform_cost = card.get_transform_cost()
                     if transform_cost: texts_to_parse.append(f"{transform_cost}: Transform {card.name}.")
                     else: texts_to_parse.append(f"Transform {card.name}.") # No cost transform trigger/action

            else:
                 # Standard card or other types
                 texts_to_parse.append(getattr(card, 'oracle_text', ''))
                 if hasattr(card, 'keywords'): keywords_from_card_data = card.keywords

            # --- Parse Keywords ---
            # Use _get_parsed_keywords helper
            parsed_keywords = self._get_parsed_keywords(keywords_from_card_data)
            for keyword_text in parsed_keywords:
                 first_word = keyword_text.split()[0] # Just the base keyword
                 self._create_keyword_ability(card_id, card, first_word, abilities_list, full_keyword_text=keyword_text)

            # --- Process Oracle Text ---
            processed_text_hashes = set() # Avoid processing duplicate lines/clauses
            for text_block in texts_to_parse:
                if not text_block or not isinstance(text_block, str): continue

                # Split text into potential ability clauses using robust splitting
                # Split by bullets, newlines, or sentence-ending punctuation followed by potential start of new clause
                split_pattern = r'\s*•\s*|\n|\.(?=\s+[A-Z{\(])|\)(?=\s+[A-Z{\(])' # Enhanced splitting
                clauses = filter(None, [c.strip() for c in re.split(split_pattern, text_block)])

                for clause in clauses:
                     if not clause: continue
                     # Remove reminder text specific to this clause
                     cleaned_clause_text = re.sub(r'\s*\([^()]*?\)\s*', ' ', clause).strip()
                     cleaned_clause_text = re.sub(r'\s+([:.,;])', r'\1', cleaned_clause_text) # Clean space before punctuation
                     cleaned_clause_text = re.sub(r'\s+', ' ', cleaned_clause_text).strip() # Normalize spaces

                     if not cleaned_clause_text: continue
                     text_hash = hash(cleaned_clause_text)

                     if text_hash not in processed_text_hashes:
                         # Check if this clause IS a keyword before general parsing
                         is_keyword_clause, keyword_found = self._is_keyword_clause(cleaned_clause_text)

                         if is_keyword_clause:
                              if isinstance(keyword_found, list): # Handle comma-separated lists
                                   for sub_kw in keyword_found:
                                        self._create_keyword_ability(card_id, card, sub_kw, abilities_list, full_keyword_text=sub_kw)
                              else: # Single keyword
                                   self._create_keyword_ability(card_id, card, keyword_found, abilities_list, full_keyword_text=cleaned_clause_text)
                         else: # If not purely a keyword clause, parse normally
                             self._parse_ability_text(card_id, card, cleaned_clause_text, abilities_list)
                         processed_text_hashes.add(text_hash)

            # Log final count for this card
            logging.debug(f"Parsed {len(abilities_list)} abilities for {card.name} ({card_id})")

            # Immediately apply newly registered Static Abilities
            # This ensures LayerSystem gets updated with the effect intentions.
            for ability in abilities_list:
                if isinstance(ability, StaticAbility):
                    ability.apply(self.game_state)

        except Exception as e:
            logging.error(f"Error parsing abilities for card {card_id} ({getattr(card, 'name', 'Unknown')}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            # Ensure list exists even on error
            if card_id not in self.registered_abilities: self.registered_abilities[card_id] = []
            
    # Helper function to parse keywords list/array
    def _get_parsed_keywords(self, keywords_data):
        """Parses keywords from list or numpy array format."""
        parsed_keywords = set()
        if isinstance(keywords_data, list):
            # Check if list of numbers (binary array) or strings
            if keywords_data and isinstance(keywords_data[0], (int, np.integer)):
                 parsed_keywords.update(kw_name.lower() for i, kw_name in enumerate(Card.ALL_KEYWORDS) if i < len(keywords_data) and keywords_data[i] == 1)
            elif keywords_data: # Assume list of strings
                 parsed_keywords.update(kw.lower() for kw in keywords_data if isinstance(kw, str) and kw)
        elif isinstance(keywords_data, np.ndarray):
             parsed_keywords.update(kw_name.lower() for i, kw_name in enumerate(Card.ALL_KEYWORDS) if i < len(keywords_data) and keywords_data[i] == 1)
        return parsed_keywords

    # Helper function to check if a clause is purely keywords
    def _is_keyword_clause(self, clause_text):
        """Checks if a clause is one or more comma-separated keywords."""
        clause_lower = clause_text.lower()
        # Handle comma-separated keywords first
        if ',' in clause_lower:
            sub_keywords = [sk.strip().lower() for sk in clause_lower.split(',')]
            # Check if ALL parts are known keywords
            if all(any(sk == kw.lower() for kw in Card.ALL_KEYWORDS) for sk in sub_keywords):
                return True, sub_keywords # Return list of keywords

        # Check if the entire clause matches a single keyword (case-insensitive)
        for kw in Card.ALL_KEYWORDS:
            # Exact match for the whole clause
            if clause_lower == kw.lower():
                return True, kw.lower() # Return the single keyword string

        return False, None

    def _create_keyword_ability(self, card_id, card, keyword_name, abilities_list, full_keyword_text=None):
        """
        Creates the appropriate Ability object for a given keyword.
        Distinguishes static, triggered, activated, and rule-modifying keywords. (Improved)
        Connects rule-modifying keywords to game state/systems where appropriate.
        """
        keyword_lower = keyword_name.lower()
        full_text = (full_keyword_text or keyword_name).lower() # Use full text if provided

        # Value/Cost parsing (remains similar)
        current_value = None
        is_parametrized_keyword = False
        cost_str = None
        if keyword_lower == "protection":
            match = re.search(r"protection from (.*)", full_text)
            if match: current_value = match.group(1).strip(); is_parametrized_keyword = True
        elif keyword_lower == "ward":
            # ... (keep existing ward parsing logic) ...
            ward_cost_match = re.search(r"ward\s*(\{.*?\})", full_text) or \
                            re.search(r"ward\s*(\d+)", full_text) or \
                            re.search(r"ward\s*(- \d+)", full_text) or \
                            re.search(r"ward\s+-\s+discard a card", full_text) or \
                            re.search(r"ward\s+-\s+pay (\d+) life", full_text) or \
                            re.search(r"ward\s+-\s+sacrifice a", full_text)
            if ward_cost_match:
                cost_group = ward_cost_match.group(1) or ward_cost_match.group(2) # Find which group matched
                if cost_group: current_value = cost_group.strip()
                else: # Handle text-based ward costs
                    if "discard a card" in full_text: current_value = "discard_card"
                    elif "pay life" in full_text: current_value = "pay_life" # TODO: Capture amount
                    elif "sacrifice" in full_text: current_value = "sacrifice" # TODO: Capture sacrifice type
                    else: current_value = "ward_generic" # Fallback
            else: current_value = "ward_generic"
            is_parametrized_keyword = True
        elif keyword_lower in ["annihilator", "afflict", "fading", "vanishing", "rampage", "poisonous", "afterlife", "cascade", "reinforce", "crew", "scavenge", "monstrosity", "adapt", "discover"]: # Keywords with N/Value
            value_match = re.search(rf"{re.escape(keyword_lower)}\s+(\d+|x)\b", full_text, re.IGNORECASE)
            if value_match:
                val_str = value_match.group(1)
                current_value = val_str if val_str == 'x' else int(val_str)
            else: # Default values if number omitted or parsing fails
                defaults = {'annihilator':1, 'poisonous':1, 'afterlife':1, 'fading':1, 'vanishing':1, 'reinforce':1, 'crew':1, 'scavenge':1, 'monstrosity':1, 'adapt':1, 'afflict':1, 'rampage':1, 'cascade':0, 'discover':0} # Cascade/Discover default 0 value?
                current_value = defaults.get(keyword_lower, 1)
            is_parametrized_keyword = True
        elif keyword_lower in ["cycling", "equip", "fortify", "reconfigure", "unearth", "flashback", "suspend", "bestow", "dash", "buyback", "madness", "transmute", "channel", "kicker", "entwine", "overload", "splice", "surge", "embalm", "eternalize", "jump-start", "escape", "awaken", "level up"]: # Keywords with cost
            cost_str = self._parse_cost(full_text, keyword_lower) # Use helper to parse cost
            current_value = cost_str # Store cost string as the value
            is_parametrized_keyword = True

        # Helper function (moved inside or make static?)
        def parse_value(text, keyword): # Renamed from _parse_value to avoid conflict
            keyword_pattern = re.escape(keyword)
            match_num = re.search(f"{keyword_pattern}\\s+(\\d+)", text)
            if match_num: return int(match_num.group(1))
            # Default values for keywords that often omit '1'
            if keyword in ['annihilator', 'poisonous']: return 1
            return 1 # Default value if not specified

        # Helper function (remains the same)
        def parse_cost(text, keyword): # Renamed from _parse_cost
            # ... (keep existing cost parsing logic) ...
            keyword_pattern = re.escape(keyword)
            # More specific patterns for costs
            patterns = [
                # Standard mana cost {..}
                rf"{keyword_pattern}\s*(\{{" + r"[WUBRGCXSPMTQA0-9\/\.]+" + r"+\}})",
                # Numeric cost (convert to {N})
                rf"{keyword_pattern}\s*(\d+)\b",
                # Specific cost types (Ward, Retrace etc.) - handled by ward parsing above
                # Cycling (can be mana or discard)
                rf"cycling\s+(discard.+)\b",
                rf"cycling\s+-\s+pay (\d+) life",
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    cost_part = match.group(1).strip()
                    if cost_part.isdigit(): return f"{{{cost_part}}}" # Normalize '1' to '{1}'
                    if cost_part.startswith('{') and cost_part.endswith('}'): return cost_part
                    if "discard" in cost_part: return "discard" # Simplified representation
                    if "pay life" in cost_part: return "pay_life" # Simplified
            if keyword == "retrace": return "Discard a land card"
            if keyword == "level up": # Cost often precedes "Level Up"
                cost_match = re.search(r"(\{.*?\})\s*:\s*Level Up", text, re.IGNORECASE)
                if cost_match: return cost_match.group(1)
            return "{0}" # Default free if no explicit cost found

        # --- Duplicate Check ---
        # Check for duplicates based on keyword AND value if it's parametrized
        if is_parametrized_keyword:
            if any(getattr(a, 'keyword', None) == keyword_lower and getattr(a, 'keyword_value', None) == current_value for a in abilities_list):
                return # Skip if same keyword AND same value/cost already present
        else: # Non-parametrized keywords check only by name
            if any(getattr(a, 'keyword', None) == keyword_lower for a in abilities_list):
                return # Skip exact duplicates

        # --- Static Grant Keywords (Layer 6) -> StaticAbility ---
        # (List remains the same)
        static_keywords = [
            "flying", "first strike", "double strike", "trample", "vigilance", "haste",
            "lifelink", "deathtouch", "indestructible", "hexproof", "shroud", "reach",
            "menace", "defender", "unblockable",
            "protection", "ward", # Handled above for parameterization
            "landwalk", "islandwalk", "swampwalk", "mountainwalk", "forestwalk", "plainswalk", # Specific walks imply landwalk
            "fear", "intimidate", "shadow", "horsemanship",
            "phasing", "banding",
            "infect", "wither",
            "changeling", # Static type-setting ability
        ]
        is_static_grant = keyword_lower in static_keywords or "walk" in keyword_lower

        if is_static_grant:
            # ... (StaticAbility creation remains the same, stores keyword/value) ...
            ability_effect_text = f"This permanent has {full_text}."
            # Handle protection/ward special values if parsed
            if keyword_lower == "protection" and current_value:
                ability = StaticAbility(card_id, f"Protection from {current_value}", ability_effect_text)
            elif keyword_lower == "ward" and current_value:
                ability = StaticAbility(card_id, f"Ward {current_value}", ability_effect_text)
            elif "walk" in keyword_lower: # Handle specific landwalk
                ability = StaticAbility(card_id, f"{keyword_name.capitalize()}", ability_effect_text) # Use original case if possible
            else:
                ability = StaticAbility(card_id, f"{keyword_name.capitalize()}", ability_effect_text)

            setattr(ability, 'keyword', keyword_lower)
            setattr(ability, 'keyword_value', current_value) # Store cost/value/protection detail
            abilities_list.append(ability)
            # Apply should register with LayerSystem
            ability.apply(self.game_state)
            logging.debug(f"Created StaticAbility for keyword: {full_text}")
            return


        # --- Triggered Keywords -> TriggeredAbility ---
        # (Map remains largely the same, ensure values use parsed 'current_value')
        triggered_map = {
            "prowess": ("whenever you cast a noncreature spell", "this creature gets +1/+1 until end of turn."),
            "cascade": ("when you cast this spell", "Exile cards until you hit a nonland card with lesser mana value. You may cast it without paying its mana cost."), # Value 'current_value' holds cascade number
            "storm": ("when you cast this spell", "Copy this spell for each spell cast before it this turn."),
            "exalted": ("whenever a creature you control attacks alone", "that creature gets +1/+1 until end of turn."),
            "annihilator": ("whenever this creature attacks", f"defending player sacrifices {current_value} permanents."),
            "battle cry": ("whenever this creature attacks", "each other attacking creature gets +1/+0 until end of turn."),
            "extort": ("whenever you cast a spell", "you may pay {W/B}. If you do, each opponent loses 1 life and you gain that much life."),
            "afflict": ("whenever this creature becomes blocked", f"defending player loses {current_value} life."),
            "enrage": ("whenever this creature is dealt damage", "trigger its enrage effect."),
            "mentor": ("whenever this creature attacks", "put a +1/+1 counter on target attacking creature with lesser power."),
            "afterlife": ("when this permanent dies", f"create {current_value} 1/1 white and black Spirit creature tokens with flying."),
            "ingest": ("whenever this creature deals combat damage to a player", "that player exiles the top card of their library."),
            "poisonous": ("whenever this creature deals combat damage to a player", f"that player gets {current_value} poison counters."),
            "rebound": ("if this spell was cast from hand, instead of graveyard", "exile it. At beginning of your next upkeep, you may cast it from exile without paying its mana cost."),
            "gravestorm": ("when you cast this spell", "Copy this spell for each permanent put into a graveyard this turn."),
            "training": ("whenever this creature attacks with another creature with greater power", "put a +1/+1 counter on this creature."),
            "undying": ("when this permanent dies", "if it had no +1/+1 counters on it, return it to the battlefield under its owner's control with a +1/+1 counter on it."),
            "persist": ("when this permanent dies", "if it had no -1/-1 counters on it, return it to the battlefield under its owner's control with a -1/-1 counter on it."),
            "decayed": ("this creature can't block.", "When it attacks, sacrifice it at end of combat."), # Multi-part
            "rampage": ("whenever this creature becomes blocked", f"it gets +{current_value}/+{current_value} until end of turn for each creature blocking it beyond the first."),
            "fading": ("this permanent enters the battlefield with N fade counters on it.", "at the beginning of your upkeep, remove a fade counter. if you can't, sacrifice it."), # Multi-part, N=current_value
            "vanishing": ("this permanent enters the battlefield with N time counters on it.", "at the beginning of your upkeep, remove a time counter. when the last is removed, sacrifice it."), # Multi-part, N=current_value
            "haunt": ("When this creature dies, exile it haunting target creature.", "When the haunted creature dies, trigger haunt effect."), # Simplified text
            "discover": ("when you cast this spell", f"Exile cards until you hit a nonland card with mana value {current_value} or less. You may cast it without paying its mana cost or put it into your hand."), # N=current_value
        }
        if keyword_lower in triggered_map:
            trigger_cond, effect_desc = triggered_map[keyword_lower]
            # Adjust effect text for N if necessary (some descriptions hardcoded above)
            effect = effect_desc

            # Handle multi-part triggers (Decayed, Fading, Vanishing)
            if keyword_lower == "decayed":
                # Add the static 'cant block' part
                static_part = StaticAbility(card_id, "This creature can't block.", "This creature can't block.")
                setattr(static_part, 'keyword', 'cant_block_static')
                abilities_list.append(static_part); static_part.apply(self.game_state)
                # Set up the attack trigger
                trigger_cond = "when this creature attacks"; effect = "sacrifice it at end of combat."
            elif keyword_lower == "fading" or keyword_lower == "vanishing":
                counter_type = "fade" if keyword_lower == "fading" else "time"
                val = current_value # Parsed value for N
                # ETB trigger
                etb_trigger = f"when this permanent enters the battlefield"; etb_effect = f"put {val} {counter_type} counters on it."
                etb_ability = TriggeredAbility(card_id, etb_trigger, etb_effect, effect_text=f"ETB {counter_type} counters")
                setattr(etb_ability, 'keyword', f"{keyword_lower}_etb"); abilities_list.append(etb_ability)
                # Upkeep trigger
                trigger_cond = "at the beginning of your upkeep"; effect = f"remove a {counter_type} counter from it. if you can't, sacrifice it."

            ability = TriggeredAbility(card_id, trigger_cond, effect, effect_text=full_text)
            setattr(ability, 'keyword', keyword_lower)
            setattr(ability, 'keyword_value', current_value) # Store N value if parsed
            abilities_list.append(ability)
            logging.debug(f"Created TriggeredAbility for keyword: {full_text}")
            return


        # --- Activated Keywords -> ActivatedAbility ---
        # (Map remains largely the same, use 'cost_str' or 'current_value' for cost)
        activated_map = {
            "cycling": ("discard this card: draw a card."),
            "equip": ("attach to target creature you control. Equip only as a sorcery."),
            "fortify": ("attach to target land you control. Fortify only as a sorcery."),
            "level up": ("put a level counter on this creature. Level up only as a sorcery."),
            "unearth": ("return this card from your graveyard to the battlefield. It gains haste. Exile it at the beginning of the next end step or if it would leave the battlefield. Unearth only as a sorcery."),
            "channel": ("discard this card: activate its channel effect."),
            "transmute": ("discard this card: search your library for a card with the same mana value as this card, reveal it, put it into your hand, then shuffle. Transmute only as a sorcery."),
            "reconfigure": ("attach to target creature you control or unattach from a creature. Reconfigure only as a sorcery."),
            "crew": (f"tap any number of untapped creatures you control with total power {current_value} or greater: this Vehicle becomes an artifact creature until end of turn."),
            "scavenge": (f"exile this card from your graveyard: Put {current_value} +1/+1 counters on target creature. Activate only as a sorcery."),
            "reinforce": (f"discard this card: Put {current_value} +1/+1 counters on target creature."),
            "morph": ("turn this face up."), # Cost handled separately
            "outlast": ("put a +1/+1 counter on this creature. Outlast only as a sorcery."),
            "monstrosity": (f"put {current_value} +1/+1 counters on this creature and it becomes monstrous. Activate only as a sorcery."),
            "adapt": (f"If this creature has no +1/+1 counters on it, put {current_value} +1/+1 counters on it."),
            "boast": ("activate boast effect."), # Cost/effect on card
            "flashback": ("Cast this card from your graveyard for its flashback cost. Then exile it."), # Value is cost
            "jump-start": ("Cast this card from your graveyard by discarding a card in addition to paying its other costs. Then exile it."), # Value is cost
            "retrace": ("You may cast this card from your graveyard by discarding a land card in addition to paying its other costs."), # Value is cost
            "embalm": ("Exile this card from your graveyard: Create a token that's a copy of it, except it's a white Zombie [OriginalType] with no mana cost. Embalm only as a sorcery."), # Value is cost
            "eternalize": ("Exile this card from your graveyard: Create a token that's a copy of it, except it's a 4/4 black Zombie [OriginalType] with no mana cost. Eternalize only as a sorcery."), # Value is cost
        }
        # (Zone lists remain the same)
        battlefield_activated = ["equip", "fortify", "level up", "reconfigure", "crew", "outlast", "monstrosity", "adapt", "boast"]
        hand_activated = ["cycling", "channel", "transmute", "reinforce"]
        gy_activated = ["unearth", "scavenge", "flashback", "jump-start", "retrace", "embalm", "eternalize"] # Added Embalm/Eternalize
        other_zone_activated = ["morph"] # Morph is special activation from face-down

        if keyword_lower in activated_map:
            # Use parsed cost (cost_str stored in current_value for these)
            cost_to_use = current_value if current_value else "{0}" # Default free if cost parsing failed
            if cost_to_use is not None:
                effect_desc = activated_map[keyword_lower]
                ability = ActivatedAbility(card_id, cost_to_use, effect_desc, effect_text=full_text)
                setattr(ability, 'keyword', keyword_lower)
                setattr(ability, 'keyword_value', cost_to_use) # Store parsed cost as value
                # Add zone information
                if keyword_lower in hand_activated: setattr(ability, 'zone', 'hand')
                elif keyword_lower in gy_activated: setattr(ability, 'zone', 'graveyard')
                elif keyword_lower in battlefield_activated: setattr(ability, 'zone', 'battlefield')
                elif keyword_lower in other_zone_activated: setattr(ability, 'zone', 'face_down')
                else: setattr(ability, 'zone', 'battlefield') # Default
                abilities_list.append(ability)
                logging.debug(f"Created ActivatedAbility for keyword: {full_text} (Zone: {getattr(ability, 'zone', 'unknown')})")
                return


        # --- Rule Modifying Keywords -> StaticAbility OR Special Handling ---
        rule_keywords = {
            # Cost reduction
            "affinity": "artifact", "convoke": True, "delve": True, "improvise": True,
            # Alt/Additional costs
            "bestow": "cost", "buyback": "cost", "entwine": "cost", "escape": "cost_and_gy", "kicker": "cost",
            "madness": "cost", # Handled as ActivatedAbility from exile
            "overload": "cost", "splice": "cost", "surge": "cost", "spree": True,
            # Timing/Rules
            "split second": True, "suspend": "cost_and_time", # Suspend is complex: ETB + Activated + Triggered?
            "companion": True,
            # Alt casting zone/timing (Already handled as Activated from GY)
            # "embalm": "cost", "eternalize": "cost", "jump-start": "cost_and_discard", "rebound": True,
            # Other
            "living weapon": True, # Handled as TriggeredAbility ETB
            "awaken": "cost_and_counters", # Alternative cost modifies spell effect
            # "cascade": True, "discover": "value", # Handled as TriggeredAbility
        }
        if keyword_lower in rule_keywords:
            # For cost modifiers (Affinity, Delve, Kicker etc.), store cost/info
            # For rules (Split Second, Companion), flag presence
            ability_effect_text = f"This card has the rule-modifying keyword: {full_text}."
            ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
            setattr(ability, 'keyword', keyword_lower)

            cost_context = rule_keywords[keyword_lower]
            kw_cost = None
            kw_value = None
            if cost_context == "cost": kw_cost = parse_cost(full_text, keyword_lower)
            elif cost_context == "value": kw_value = parse_value(full_text, keyword_lower)
            elif cost_context == "cost_and_gy": # Escape
                kw_cost = parse_cost(full_text, keyword_lower)
                match = re.search(r"exile (\w+|\d+) other cards?", full_text, re.IGNORECASE)
                if match: kw_value = self._word_to_number(match.group(1))
                else: kw_value = 1 # Default exile 1? Needs rules check per card.
            elif cost_context == "cost_and_time": # Suspend
                kw_cost = parse_cost(full_text, keyword_lower)
                kw_value = parse_value(full_text, keyword_lower)
            elif cost_context == "cost_and_counters": # Awaken
                kw_cost = parse_cost(full_text, keyword_lower)
                match = re.search(r"put (\w+|\d+) \+1/\+1 counters", full_text, re.IGNORECASE)
                if match: kw_value = self._word_to_number(match.group(1))
                else: kw_value = 1
            elif isinstance(cost_context, str): # Affinity type, etc.
                kw_value = cost_context

            setattr(ability, 'keyword_cost', kw_cost)
            setattr(ability, 'keyword_value', kw_value)
            abilities_list.append(ability)
            # Apply registers the static ability marker. Game systems query this.
            ability.apply(self.game_state)

            # --- Special Handling for Complex Keywords ---
            if keyword_lower == "suspend":
                # Suspend: Needs initial activation, exile, counter removal trigger
                # TODO: Implement suspend state tracking and triggers in GameState/AbilityHandler
                # Activation is not standard - happens on cast attempt usually.
                logging.warning("Suspend keyword parsing is placeholder - needs full implementation.")
            elif keyword_lower == "split second":
                # Split Second flag needs to be checked by GameState/ActionHandler
                # The static ability signals its presence on the stack item.
                logging.debug("Registered Split Second marker.")
            # Rebound is handled by GameState resolution logic checking context flags
            # Living Weapon, Cascade handled by Triggered logic
            # Madness handled by Activated from Exile logic
            # ... other complex keywords ...
            logging.debug(f"Registered rule-modifying keyword '{keyword_lower}' as StaticAbility marker.")
            return

        # --- Fallback ---
        # Only log if it wasn't handled as a basic static grant keyword above
        if not is_static_grant:
            logging.warning(f"Keyword '{keyword_lower}' (from '{full_text}') not explicitly mapped or parsed.")
        
    def _parse_triggered_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse triggered abilities from card text with improved patterns"""
        # Define enhanced patterns for triggered abilities
        trigger_patterns = [
            # "When/Whenever X, do Y" pattern with named capture groups
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip()}),
            
            # "At the beginning of X, do Y" pattern
            (r'(?P<trigger>at\s+the\s+beginning\s+of\s+[^,\.]+?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip()}),
            
            # Pattern for ETB triggers
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?\s+enters\s+the\s+battlefield[^,\.]*?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "etb": True}),
            
            # Pattern for death triggers
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?\s+dies[^,\.]*?),\s+(?P<effect>[^\.;]+)', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "dies": True}),
            
            # Pattern with "if" condition
            (r'(?P<trigger>when(?:ever)?\s+[^,\.]+?),\s+(?P<effect>[^\.;]+?)(?:\s+if\s+(?P<condition>[^\.;]+))?', 
            lambda m: {"trigger_condition": m.group("trigger").strip(), 
                    "effect": m.group("effect").strip(),
                    "additional_condition": ("if " + m.group("condition").strip()) if m.group("condition") else None}),
                    
            # Door unlocked triggers
            (r'(?P<trigger>when(?:ever)?\s+(?:you\s+unlock\s+this\s+door|this\s+door\s+becomes\s+unlocked)[^,\.]*?),\s+(?P<effect>[^\.;]+)',
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "door_unlock": True}),
            
            # Class level up triggers  
            (r'(?P<trigger>when\s+this\s+class\s+level\s+increases[^,\.]*?),\s+(?P<effect>[^\.;]+)',
            lambda m: {"trigger_condition": m.group("trigger").strip(), "effect": m.group("effect").strip(), "class_level_up": True})
        ]
        
        # Get complex trigger phrases to event type mappings
        complex_triggers = self._get_complex_trigger_phrases()
        
        # Split text by periods to get separate abilities
        sentences = oracle_text.split('.')
        
        for sentence in sentences:
            sentence = sentence.strip().lower()
            if not sentence:
                continue
                
            # Check for complex triggers first
            for phrase, event_type in complex_triggers.items():
                if phrase in sentence:
                    trigger = sentence.split(',', 1)
                    if len(trigger) > 1:
                        effect = trigger[1].strip()
                        # Create TriggeredAbility
                        ability = TriggeredAbility(
                            card_id=card_id,
                            trigger_condition=phrase,
                            effect=effect,
                            effect_text=sentence
                        )
                        abilities_list.append(ability)
                        logging.debug(f"Registered complex triggered ability for {card.name}: {sentence}")
                    break
                    
            # Parse and add abilities using regular patterns
            self._parse_text_with_patterns(
                sentence, trigger_patterns, "triggered", card_id, card, abilities_list)
            
    def _parse_ability_text(self, card_id, card, ability_text, abilities_list):
        """
        Parse a single ability text string. Delegates parsing logic to Ability subclasses.
        Tries to identify Activated, Triggered, or Static.
        """
        ability_text = ability_text.strip()
        if not ability_text: return

        # Try parsing as Activated Ability first (common format: Cost: Effect)
        try:
            ability = ActivatedAbility(card_id=card_id, effect_text=ability_text, cost="placeholder", effect="placeholder") # Temp cost/effect
            # If ActivatedAbility constructor successfully parses cost/effect:
            if ability.cost != "placeholder" and ability.effect != "placeholder":
                 # Check if it's actually a Mana Ability
                 if "add {" in ability.effect.lower() or "add mana" in ability.effect.lower():
                      mana_produced = self._parse_mana_produced(ability.effect)
                      if mana_produced and any(mana_produced.values()):
                           mana_ability = ManaAbility(card_id=card_id, cost=ability.cost, mana_produced=mana_produced, effect_text=ability_text)
                           setattr(mana_ability, 'source_card', card)
                           abilities_list.append(mana_ability)
                           logging.debug(f"Registered ManaAbility for {card.name}: {mana_ability.effect_text}")
                           return # Successfully parsed as Mana Ability
                      # else: Failed mana parsing, treat as regular activated
                 # Parsed as Activated (non-mana)
                 setattr(ability, 'source_card', card)
                 abilities_list.append(ability)
                 logging.debug(f"Registered ActivatedAbility for {card.name}: {ability.effect_text}")
                 return # Successfully parsed as Activated Ability
            # else: Did not parse as Activated, continue checking
        except ValueError: pass # Cost: Effect pattern not found or invalid
        except Exception as e: logging.error(f"Error parsing as ActivatedAbility: {e}")

        # Try parsing as Triggered Ability ("When/Whenever/At ... , ...")
        try:
            ability = TriggeredAbility(card_id=card_id, effect_text=ability_text, trigger_condition="placeholder", effect="placeholder") # Temp placeholders
            # If TriggeredAbility constructor successfully parses:
            if ability.trigger_condition != "placeholder" and ability.effect != "placeholder":
                 setattr(ability, 'source_card', card)
                 abilities_list.append(ability)
                 logging.debug(f"Registered TriggeredAbility for {card.name}: {ability.effect_text}")
                 return # Successfully parsed as Triggered Ability
            # else: Did not parse as Triggered, continue checking
        except ValueError: pass # Trigger pattern not found or invalid
        except Exception as e: logging.error(f"Error parsing as TriggeredAbility: {e}")

        # Assume Static Ability if not Activated or Triggered
        # Basic check: Avoid adding plain keywords again if already handled by _create_keyword_ability
        is_simple_keyword = ability_text.lower() in [kw.lower() for kw in Card.ALL_KEYWORDS]
        already_registered_as_keyword = any(getattr(a, 'keyword', None) == ability_text.lower() for a in abilities_list)

        if not is_simple_keyword or not already_registered_as_keyword:
             try:
                 # Effect is the whole text for static abilities
                 ability = StaticAbility(card_id=card_id, effect=ability_text, effect_text=ability_text)
                 setattr(ability, 'source_card', card)
                 abilities_list.append(ability)
                 logging.debug(f"Registered StaticAbility for {card.name}: {ability.effect_text}")
                 # --- >>> APPLY STATIC ABILITY <<< ---
                 # StaticAbility.apply registers with LayerSystem
                 try:
                     ability.apply(self.game_state)
                     logging.debug(f"Called apply() for StaticAbility: {ability.effect_text}")
                 except Exception as apply_e:
                     logging.error(f"Error calling apply() for static ability '{ability.effect_text}': {apply_e}")
                 return # Parsed as Static
             except Exception as e:
                 logging.error(f"Error parsing as StaticAbility: {e}")

        # If none of the above worked, log it (unless it was a handled keyword)
        if not is_simple_keyword or not already_registered_as_keyword:
            logging.debug(f"Could not classify ability text for {card.name}: '{ability_text}'")
        
    def _parse_mana_produced(self, mana_text):
         """Parses a mana production string (e.g., "add {G}{G}") into a dict."""
         produced = defaultdict(int) # Use defaultdict for easier counting
         mana_text_lower = mana_text.lower()

         # Find mana symbols like {W}, {2}, {G}, {C}, {X}, {S}, {W/P}, {G/U}, {2/B}
         # More robust regex to handle different symbols
         symbols = re.findall(r'\{([wubrgcsx\d\/p]+)\}', mana_text_lower)

         for sym in symbols:
             if sym.isdigit(): produced['C'] += int(sym)
             elif sym == 'c': produced['C'] += 1
             elif sym == 'w': produced['W'] += 1
             elif sym == 'u': produced['U'] += 1
             elif sym == 'b': produced['B'] += 1
             elif sym == 'r': produced['R'] += 1
             elif sym == 'g': produced['G'] += 1
             elif sym == 'x': produced['X'] += 1 # Represents variable amount
             elif sym == 's': produced['S'] += 1 # Snow mana
             elif '/p' in sym: # Phyrexian
                  color = sym.split('/')[0].upper()
                  if color in produced: produced[color] += 1 # Treat as colored for production pool check? Or special type? Let's count as color.
             elif '/' in sym: # Hybrid
                  parts = [p.upper() for p in sym.split('/')]
                  # Store hybrid possibilities, decision made at payment time
                  # For now, just count as 1 potential mana of either type for checks?
                  # Simplification: Count as 1 generic/any?
                  produced['hybrid'] += 1 # Add hybrid count? Ambiguous. Let's count as generic for now.
                  produced['C'] += 1
             # Add handling for {2/W} etc. if needed

         # Handle "add one mana of any color"
         if "one mana of any color" in mana_text_lower:
             produced['any'] = produced.get('any', 0) + 1
         # Handle "two mana of any one color" etc.
         num_word_map = {"two": 2, "three": 3, "four": 4, "five": 5}
         num_match = re.search(r"(two|three|four|five) mana", mana_text_lower)
         if num_match:
             count = num_word_map.get(num_match.group(1), 0)
             if "mana of any one color" in mana_text_lower:
                  # Requires choice, store separately or as generic?
                  produced['choice'] = produced.get('choice', 0) + count
             elif "in any combination of colors" in mana_text_lower:
                  produced['any_combination'] = produced.get('any_combination', 0) + count
             elif "of any color" in mana_text_lower: # E.g., "Add three mana of any color."
                 produced['any'] = produced.get('any', 0) + count


         # Convert defaultdict back to regular dict for consistency if needed
         return dict(produced)
    
    def check_abilities(self, event_origin_card_id, event_type, context=None):
        """
        Checks all registered triggered abilities to see if they should trigger based on the event.
        Adds valid triggers to the self.active_triggers queue. Now checks graveyard.
        """
        if context is None: context = {}
        gs = self.game_state

        # Add game state and event type to context for condition checks
        context['game_state'] = gs
        context['event_type'] = event_type # Ensure event type is consistently available

        event_card = gs._safe_get_card(event_origin_card_id)
        context['event_card_id'] = event_origin_card_id
        context['event_card'] = event_card

        logging.debug(f"Checking triggers for event: {event_type} (Origin: {getattr(event_card, 'name', event_origin_card_id)}) Context: {context.keys()}")

        # Determine zones to check based on event type
        zones_to_check = {"battlefield"} # Default zone
        # Add graveyard checks for specific events
        # Example: Abilities triggering on discard, or specific card abilities (Bloodghast)
        graveyard_trigger_events = ["DISCARD", "DIES", "CAST_SPELL"] # Add more as needed
        # Specific "Whenever X is put into your graveyard from anywhere" or similar text?
        if event_type in graveyard_trigger_events:
            zones_to_check.add("graveyard")

        # Check abilities on permanents in relevant zones
        cards_to_check_ids = set()
        for p in [gs.p1, gs.p2]:
            for zone_name in zones_to_check:
                 cards_to_check_ids.update(p.get(zone_name, []))
            # Consider adding hand/library/exile if specific triggers warrant it

        for ability_source_id in cards_to_check_ids:
            source_card = gs._safe_get_card(ability_source_id)
            if not source_card: continue

            registered_abilities = self.registered_abilities.get(ability_source_id, [])
            # Find current zone of the source
            source_controller, source_zone = gs.find_card_location(ability_source_id)

            for ability in registered_abilities:
                if isinstance(ability, TriggeredAbility):
                    # --- Check if the ability can trigger from its current zone ---
                    can_trigger_from_zone = False
                    ability_zone = getattr(ability, 'zone', 'battlefield').lower() # Where the ability normally functions from
                    trigger_text = getattr(ability, 'trigger_condition', '').lower()

                    # Battlefield abilities usually require source on battlefield
                    if ability_zone == 'battlefield' and source_zone == 'battlefield':
                        can_trigger_from_zone = True
                    # Graveyard abilities trigger only from graveyard
                    elif ability_zone == 'graveyard' and source_zone == 'graveyard':
                        can_trigger_from_zone = True
                    # Abilities that trigger from anywhere (rare, need specific check)
                    elif "from anywhere" in trigger_text:
                        can_trigger_from_zone = True
                    # Hand triggers? (e.g., Cycling triggers 'when cycled')
                    elif ability_zone == 'hand' and source_zone == 'hand' and event_type == "CYCLING":
                        can_trigger_from_zone = True
                    # Dies triggers check when leaving battlefield
                    elif source_zone == 'battlefield' and "dies" in trigger_text and event_type == "DIES":
                        can_trigger_from_zone = True # Trigger condition check handles the rest

                    if not can_trigger_from_zone:
                        # logging.debug(f"Skipping ability check for {source_card.name}: Cannot trigger from zone '{source_zone}'.")
                        continue

                    # Prepare context specific to this ability check
                    trigger_check_context = context.copy()
                    trigger_check_context['source_card_id'] = ability_source_id
                    trigger_check_context['source_card'] = source_card
                    trigger_check_context['source_zone'] = source_zone # Pass current zone

                    try:
                        if ability.can_trigger(event_type, trigger_check_context):
                            # Ability controller might differ from card controller if zone changes
                            # Get controller based on ability source (or owner if no controller)
                            ability_controller = source_controller if source_controller else gs.get_card_owner(ability_source_id) # Get owner as fallback
                            if ability_controller:
                                self.active_triggers.append((ability, ability_controller))
                                logging.debug(f"Queued trigger: '{ability.trigger_condition}' from {source_card.name} ({ability_source_id} in {source_zone}) due to {event_type}")
                            else:
                                logging.warning(f"Trigger source {ability_source_id} has no controller/owner, cannot queue trigger.")
                    except Exception as e:
                        logging.error(f"Error checking trigger condition for {ability.effect_text} from {source_card.name}: {e}")
                        import traceback; logging.error(traceback.format_exc())


        # Return value indicates if any triggers were added, but not used externally right now
        return bool(self.active_triggers) # Or just return None

    def get_activated_abilities(self, card_id):
        """Get all activated abilities for a given card"""
        card_abilities = self.registered_abilities.get(card_id, [])
        return [ability for ability in card_abilities if isinstance(ability, ActivatedAbility)]
    
    def can_activate_ability(self, card_id, ability_index, controller):
        """Check if a specific activated ability can be activated"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            
            if hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
                # Use enhanced mana system if available
                cost = ability.cost
                parsed_cost = self.game_state.mana_system.parse_mana_cost(cost)
                return self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)
            else:
                # Fallback to basic check
                return ability.can_activate(self.game_state, controller)
                
        return False

    def activate_ability(self, card_id, ability_index, controller):
        """Activate a specific activated ability"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            
            if hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
                # Use enhanced mana system if available
                cost = ability.cost
                parsed_cost = self.game_state.mana_system.parse_mana_cost(cost)
                can_pay = self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)
                
                if can_pay:
                    # Pay cost
                    paid = self.game_state.mana_system.pay_mana_cost(controller, parsed_cost)
                    if paid:
                        # Add to stack
                        self.game_state.add_to_stack("ABILITY", card_id, controller)
                        logging.debug(f"Activated ability {ability_index} for {self.game_state._safe_get_card(card_id).name}")
                        return True
            else:
                # Fallback to basic activation
                if ability.can_activate(self.game_state, controller):
                    cost_paid = ability.pay_cost(self.game_state, controller)
                    if cost_paid:
                        # Add to stack
                        self.game_state.add_to_stack("ABILITY", card_id, controller)
                        logging.debug(f"Activated ability {ability_index} for {self.game_state._safe_get_card(card_id).name}")
                        return True
                        
        return False
    
    def process_triggered_abilities(self):
        """
        Process all pending triggered abilities from the queue, adding them to the stack in APNAP order.
        Ensures GameState context is passed correctly.
        """
        gs = self.game_state
        if not self.active_triggers:
            return

        ap_triggers = []
        nap_triggers = []
        active_player = gs._get_active_player()

        # Sort triggers by Active Player (AP) and Non-Active Player (NAP)
        for ability, controller in self.active_triggers:
             if controller == active_player: ap_triggers.append((ability, controller))
             else: nap_triggers.append((ability, controller))

        # Clear the queue *before* adding to stack to prevent potential re-trigger loops within resolution
        queued_triggers = ap_triggers + nap_triggers
        self.active_triggers = [] # Clear the processing queue

        # Add AP triggers to stack first
        for ability, controller in ap_triggers:
            if not ability or not hasattr(ability, 'card_id'): continue # Safety check

            # Pass the ability object itself in the context for resolution
            context_for_stack = {
                "ability": ability, # Pass the specific instance
                "source_id": ability.card_id,
                "trigger_condition": getattr(ability, 'trigger_condition', 'Unknown Trigger'),
                "effect_text": getattr(ability, 'effect_text', 'Unknown Effect'),
                # Add any original context items needed for resolution if not captured by ability?
            }
            gs.add_to_stack("TRIGGER", ability.card_id, controller, context_for_stack)
            logging.debug(f"Added AP Triggered Ability to stack: {context_for_stack['effect_text']}")

        # Add NAP triggers to stack
        for ability, controller in nap_triggers:
            if not ability or not hasattr(ability, 'card_id'): continue # Safety check

            context_for_stack = {
                 "ability": ability, # Pass the specific instance
                 "source_id": ability.card_id,
                 "trigger_condition": getattr(ability, 'trigger_condition', 'Unknown Trigger'),
                 "effect_text": getattr(ability, 'effect_text', 'Unknown Effect'),
            }
            gs.add_to_stack("TRIGGER", ability.card_id, controller, context_for_stack)
            logging.debug(f"Added NAP Triggered Ability to stack: {context_for_stack['effect_text']}")

        # After adding triggers, the main game loop should handle stack resolution.

    def resolve_ability(self, ability_type, card_id, controller, context=None):
        """
        Resolve an ability from the stack. Now expects 'ability' object in context.
        Relies on the Ability object's resolve method or fallback generic resolution.
        Handles target validation using the main TargetingSystem instance.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        source_name = getattr(card, 'name', f"Card {card_id}") if card else f"Card {card_id}"

        if not context: context = {}

        ability = context.get("ability") # Get the specific Ability object instance
        effect_text_from_context = context.get("effect_text", "Unknown") # Fallback text
        targets_on_stack = context.get("targets", {}) # Targets chosen when added to stack

        logging.debug(f"Attempting to resolve {ability_type} '{effect_text_from_context}' from {source_name} with context keys {list(context.keys())}")

        # --- Path 1: Use Ability Object ---
        if ability and isinstance(ability, Ability):
            ability_effect_text = getattr(ability, 'effect_text', effect_text_from_context) # Prefer ability's text
            logging.debug(f"Resolving via Ability object ({type(ability).__name__}) method.")

            # *** CHANGED: Use self.targeting_system instance ***
            valid_targets = True # Assume valid if no targeting system or no targets needed
            if getattr(ability, 'requires_target', False) or (targets_on_stack and any(targets_on_stack.values())): # Check if targets are present or ability requires them
                if self.targeting_system:
                    validation_targets = targets_on_stack if isinstance(targets_on_stack, dict) else {}
                    # Pass effect text for context-specific validation if possible
                    validation_text = ability_effect_text if ability_effect_text != "Unknown" else None
                    valid_targets = self.targeting_system.validate_targets(card_id, validation_targets, controller, effect_text=validation_text)
                    if not valid_targets:
                         logging.info(f"Targets for '{ability_effect_text}' from {source_name} became invalid via TargetingSystem. Fizzling.")
                         return # Fizzle
                else:
                    # Basic fallback validation if no targeting system
                    # Simple check: Does target still exist in expected zone?
                    # This is very rudimentary.
                    for cat, target_list in targets_on_stack.items():
                        if not target_list: continue
                        for target_id in target_list:
                            target_owner, target_zone = gs.find_card_location(target_id)
                            # Basic check: must exist somewhere
                            if not target_owner:
                                 valid_targets = False; break
                        if not valid_targets: break
                    if not valid_targets:
                         logging.info(f"Targets for '{ability_effect_text}' from {source_name} became invalid (simple check). Fizzling.")
                         return # Fizzle

            if not valid_targets: # This check seems redundant given returns above, but safe.
                return # Fizzle if targets invalid

            # Resolve using the ability's method
            try:
                # Determine the appropriate resolve method based on signature
                resolve_method = None
                resolve_kwargs = {'game_state': gs, 'controller': controller}
                # Try most specific first
                if hasattr(ability, 'resolve_with_targets') and callable(ability.resolve_with_targets):
                    resolve_method = ability.resolve_with_targets
                    resolve_kwargs['targets'] = targets_on_stack # Ensure targets are passed if method expects them
                elif hasattr(ability, 'resolve') and callable(ability.resolve):
                    resolve_method = ability.resolve
                    # Check signature if possible to avoid passing targets if not expected
                    import inspect
                    sig = inspect.signature(ability.resolve)
                    if 'targets' in sig.parameters:
                         resolve_kwargs['targets'] = targets_on_stack
                    # else: resolve only expects game_state, controller

                # Add fallbacks for older/different method names if necessary
                elif hasattr(ability, '_resolve_ability_implementation') and callable(ability._resolve_ability_implementation):
                    resolve_method = ability._resolve_ability_implementation
                    # Check signature for targets...
                    import inspect
                    sig = inspect.signature(resolve_method)
                    if 'targets' in sig.parameters: resolve_kwargs['targets'] = targets_on_stack
                elif hasattr(ability, '_resolve_ability_effect') and callable(ability._resolve_ability_effect):
                    resolve_method = ability._resolve_ability_effect
                    # Check signature for targets...
                    import inspect
                    sig = inspect.signature(resolve_method)
                    if 'targets' in sig.parameters: resolve_kwargs['targets'] = targets_on_stack


                if resolve_method:
                    resolve_method(**resolve_kwargs) # Call with prepared arguments
                    logging.debug(f"Resolved {type(ability).__name__} ability for {source_name}: {ability_effect_text}")
                else:
                    logging.error(f"No resolve method found for ability object {type(ability).__name__} on {source_name}. Attempting internal effect creation.")
                    # Attempt fallback using _create_ability_effects if resolve methods missing
                    if hasattr(ability, '_create_ability_effects') and callable(ability._create_ability_effects):
                        effect_text_to_use = getattr(ability, 'effect', ability_effect_text) # Prefer 'effect' attribute if present
                        effects = ability._create_ability_effects(effect_text_to_use, targets_on_stack)
                        if effects:
                             for effect_obj in effects:
                                  effect_obj.apply(gs, card_id, controller, targets_on_stack)
                             logging.debug(f"Resolved effects for {type(ability).__name__} via internal creation for {source_name}.")
                        else:
                             logging.error(f"Internal effect creation failed for {type(ability).__name__} on {source_name}.")
                    else:
                         logging.error(f"Could not resolve {type(ability).__name__} on {source_name}: No resolve method or effect creator found.")

            except Exception as e:
                logging.error(f"Error resolving ability {ability_type} ({ability_effect_text}) for {source_name}: {str(e)}")
                import traceback; logging.error(traceback.format_exc())

        # --- Path 2: Fallback using Effect Text from Context (largely unchanged) ---
        else:
             logging.warning(f"No valid 'ability' object found in context for resolving {ability_type} from {source_name}. Attempting fallback resolution from effect text: '{effect_text_from_context}'")
             if effect_text_from_context and effect_text_from_context != "Unknown":
                 logging.debug(f"Resolving via EffectFactory fallback using text: '{effect_text_from_context}'.")
                 # Validate targets using TargetingSystem
                 valid_targets_for_text = True
                 if self.targeting_system:
                      validation_targets = targets_on_stack if isinstance(targets_on_stack, dict) else {}
                      # Only validate if text actually contains "target"
                      if "target" in effect_text_from_context.lower():
                          valid_targets_for_text = self.targeting_system.validate_targets(card_id, validation_targets, controller, effect_text=effect_text_from_context)

                 if not valid_targets_for_text:
                      logging.info(f"Targets for fallback effect '{effect_text_from_context}' from {source_name} became invalid. Fizzling.")
                      return # Fizzle

                 # Use EffectFactory to create effects from text
                 effects = EffectFactory.create_effects(effect_text_from_context, targets=targets_on_stack)
                 if not effects:
                     logging.error(f"Cannot resolve {ability_type} from {source_name}: EffectFactory failed for text '{effect_text_from_context}'.")
                     return

                 # Apply created effects
                 for effect_obj in effects:
                      try:
                          # Ensure targets are passed correctly
                          target_arg = targets_on_stack if isinstance(targets_on_stack, dict) else None
                          effect_obj.apply(gs, card_id, controller, target_arg)
                      except NotImplementedError:
                          logging.error(f"Fallback effect application not implemented for: {effect_obj.effect_text}")
                      except Exception as e:
                          logging.error(f"Error applying fallback effect '{effect_obj.effect_text}': {e}", exc_info=True)
                 logging.debug(f"Resolved fallback {ability_type} for {source_name} using effect text.")
             else:
                 logging.error(f"Cannot resolve {ability_type} from {source_name}: Missing ability object and effect text in context.")
                  
    def _check_keyword_internal(self, card, keyword):
        """
        Centralized keyword check. PREFERS GameState's check_keyword method.
        Falls back to LayerSystem results (keywords array) or direct card check. (Revised)
        """
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)

        # 1. PREFER GameState's method if it exists (should encapsulate layer checking)
        if hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
            try:
                # Pass the card_id, let GameState handle card lookup and layer checking
                result = gs.check_keyword(card_id, keyword)
                # Logging handled within gs.check_keyword if needed
                return result
            except Exception as e:
                logging.warning(f"Error calling GameState.check_keyword for {card_id}, {keyword}: {e}. Falling back.")
                # Fall through to internal check on error

        # --- Fallback Logic (if GameState doesn't have check_keyword or it fails) ---
        if not card or not isinstance(card, Card):
            # logging.debug(f"Keyword Check Fallback: Invalid card object for ID {card_id}.")
            return False
        if not card_id:
            # logging.debug(f"Keyword Check Fallback: Card object {card} missing card_id.")
            return False # Need ID for layer checks

        keyword_lower = keyword.lower()

        # 2. Use LayerSystem (check calculated 'keywords' array on LIVE card object)
        # Ensure the live card object is used, as its attributes are updated by LayerSystem
        live_card = gs._safe_get_card(card_id) # Get the live card reference
        if not live_card: live_card = card # Fallback if not found in GS state

        if hasattr(live_card, 'keywords') and isinstance(live_card.keywords, (list, np.ndarray)):
            try:
                if not Card.ALL_KEYWORDS:
                    logging.error("Card.ALL_KEYWORDS is not defined or empty.")
                    return False # Safety check
                # Use the Card class static variable for consistency
                kw_list = [k.lower() for k in Card.ALL_KEYWORDS] # Ensure lowercase list
                idx = kw_list.index(keyword_lower)
                if idx < len(live_card.keywords):
                    has_kw = bool(live_card.keywords[idx])
                    # logging.debug(f"Keyword Check Fallback (Layer): '{keyword_lower}' on '{live_card.name}' -> {has_kw}")
                    return has_kw
                else: # Index exists but is out of bounds for this card's array
                    logging.warning(f"Keyword Check Fallback (Layer): Keyword index {idx} out of bounds for {live_card.name} (Len: {len(live_card.keywords)})")
            except ValueError: # Keyword not in ALL_KEYWORDS list
                 # Allow checking for dynamic keywords like 'cant_attack' added by effects
                 # If LayerSystem adds custom keywords to the 'keywords' attribute directly (not via array), check them.
                 # This requires LayerSystem consistency. Let's assume not for now.
                 # logging.debug(f"Keyword Check Fallback (Layer): '{keyword_lower}' not in standard list.")
                 pass
            except IndexError: # Should not happen if length checked
                 pass
            except Exception as e:
                 logging.error(f"Error checking keyword array for {live_card.name}: {e}")

        # 3. REMOVED Oracle Text Fallback: Base text check is unreliable due to layer effects.
        #    The definitive state comes from the layered 'keywords' array.
        #    If not found there, it shouldn't be considered active.

        # logging.debug(f"Keyword Check Fallback: Keyword '{keyword_lower}' not found via any method for '{getattr(live_card, 'name', card_id)}'.")
        return False

    def check_keyword(self, card_id, keyword):
        """Public interface for checking keywords using the internal logic."""
        card = self.game_state._safe_get_card(card_id)
        return self._check_keyword_internal(card, keyword)
                                            

    def get_protection_details(self, card_id):
        """
        Gets detailed protection strings (e.g., "red", "creatures") for a card.
        Queries the live card object which should have its final characteristics
        after LayerSystem application, including stored protection details.
        """
        gs = self.game_state
        live_card = gs._safe_get_card(card_id)
        if not live_card:
            logging.debug(f"get_protection_details: Card {card_id} not found.")
            return None

        # --- Retrieve Protection Details from Live Card Object ---
        # Assumes LayerSystem stores the result in 'active_protections' attribute.
        # The default value is an empty set if the attribute doesn't exist.
        active_protections = getattr(live_card, 'active_protections', set())

        # Convert to lowercase list for consistency
        final_details = list(str(p).lower() for p in active_protections)

        logging.debug(f"Final protection details for {getattr(live_card,'name','Unknown')} ({card_id}): {final_details if final_details else None}")
        return final_details if final_details else None
            
    def handle_attack_triggers(self, attacker_id):
        """Handle abilities triggering when a specific creature attacks."""
        gs = self.game_state
        card = gs._safe_get_card(attacker_id)
        if not card: return

        controller = gs.get_card_controller(attacker_id)
        if not controller: return

        # Prepare base context
        context = {"attacker_id": attacker_id, "controller": controller}

        # 1. Abilities on the attacker itself ("When this creature attacks...")
        self.check_abilities(attacker_id, "ATTACKS", context) # Event originates from the attacker

        # 2. Abilities on other permanents controlled by the attacker ("Whenever a creature you control attacks...")
        for permanent_id in controller.get("battlefield", []):
            if permanent_id != attacker_id: # Check other permanents
                 # Pass the attacker context, but the event origin is the other permanent
                 self.check_abilities(permanent_id, "CREATURE_ATTACKS", context)

        # 3. Abilities triggered by an opponent's creature attacking
        opponent = gs._get_non_active_player() if controller == gs._get_active_player() else gs._get_active_player()
        for permanent_id in opponent.get("battlefield", []):
             # Pass the attacker context, event origin is opponent's permanent
             self.check_abilities(permanent_id, "CREATURE_ATTACKS_OPPONENT", context) # More specific event name

        # Let GameState/Environment loop handle processing self.active_triggers
