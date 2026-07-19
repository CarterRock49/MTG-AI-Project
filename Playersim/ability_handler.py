import logging
import copy
from .targeting import TargetingSystem  # noqa: F401  (re-export: kept for backward compatibility)

import numpy as np
# Remove KeywordEffects if not used directly after refactoring
# from .keyword_effects import KeywordEffects
from .ability_types import (Ability, ActivatedAbility, TriggeredAbility, StaticAbility,
                            TargetingOverrideAbility, ManaAbility, CreateTokenEffect)
import re
from collections import defaultdict
from .card import Card
from .ability_utils import EffectFactory
# *** CHANGED: Import TargetingSystem from its new file ***


_GENERIC_ABILITY_WORD_TRIGGER_PREFIX = re.compile(
    r'^\s*(?P<label>[A-Za-z0-9][^"“”\n\r{}:\u2022\u25cf'
    r'\u2013\u2014\ufffd]{0,79}?)\s*'
    r'[\u2013\u2014\ufffd]\s*'
    r'(?P<trigger>When(?:ever)?|At)\b',
    re.IGNORECASE)
_SAGA_CHAPTER_LABEL = re.compile(
    r'^[ivxlcdm]+(?:\s*,\s*[ivxlcdm]+)*$', re.IGNORECASE)
_UNMODELED_ABILITY_WORD_STATE_GATES = {'max speed', 'solved'}


def _split_generic_ability_word_trigger_prefix(text):
    """Return ``(ability_word, trigger_text)`` for a safe leading label."""
    clause = str(text or '').strip()
    match = _GENERIC_ABILITY_WORD_TRIGGER_PREFIX.match(clause)
    if match is None:
        return None
    label = ' '.join(match.group('label').split())
    normalized = label.casefold()
    if (_SAGA_CHAPTER_LABEL.fullmatch(label)
            or normalized in _UNMODELED_ABILITY_WORD_STATE_GATES
            or re.match(r'^(?:choose|mode|chapter)\b', normalized)):
        return None
    return normalized, clause[match.start('trigger'):]

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


    def handle_class_level_up(self, class_idx, controller=None):
        """
        Handle leveling up a Class card with proper trigger processing.
        Includes mana cost payment and trigger processing.

        Args:
            class_idx: Index of the Class card in the controller's battlefield.

        Returns:
            bool: True if the class was successfully leveled up.
        """
        gs = self.game_state
        active_player = controller or gs._get_active_player()

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
                if not gs.mana_system.can_pay_mana_cost_with_lands(
                        active_player, parsed_cost_dict):
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
        
    def get_unlockable_room_door(self, controller, room_idx, room_id=None,
                                 door_number=None):
        """Return one exact payable Room-door transaction, or ``None``.

        Room unlocks are special actions whose masks count untapped lands.  The
        execution path must use that same land-aware affordability predicate;
        checking only floated mana made mask-valid unlocks fail at runtime.
        ``room_id`` and ``door_number`` pin a generated action to the physical
        Room and locked half that were inspected by the mask.
        """
        gs = self.game_state
        battlefield = controller.get("battlefield", []) if controller else []
        if (not isinstance(room_idx, int)
                or not 0 <= room_idx < len(battlefield)
                or not getattr(gs, "mana_system", None)
                or not gs._can_act_at_sorcery_speed(controller)):
            return None

        live_room_id = battlefield[room_idx]
        if room_id is not None and live_room_id != room_id:
            return None
        room_card = gs._safe_get_card(live_room_id)
        if not room_card or not getattr(room_card, "is_room", False):
            return None

        if door_number is not None:
            candidate_numbers = [door_number]
        else:
            # A normally cast Room has exactly one locked door. Preserve the
            # legacy preference for door 2 if malformed/staged state has both.
            candidate_numbers = [2, 1]
        for candidate_number in candidate_numbers:
            if candidate_number not in (1, 2):
                continue
            door = getattr(room_card, f"door{candidate_number}", None) or None
            if not door or door.get("unlocked", False):
                continue
            cost_text = door.get("mana_cost", "")
            if not cost_text:
                continue
            parsed_cost = gs.mana_system.parse_mana_cost(cost_text)
            payment_context = {
                "card_id": live_room_id,
                "card": room_card,
                "is_ability": True,
                "cause": "room_unlock",
            }
            if not gs.mana_system.can_pay_mana_cost_with_lands(
                    controller, parsed_cost, payment_context):
                continue
            return {
                "room_id": live_room_id,
                "room_card": room_card,
                "door_number": candidate_number,
                "door": door,
                "cost_text": cost_text,
                "parsed_cost": parsed_cost,
                "payment_context": payment_context,
            }
        return None

    def handle_unlock_door(self, room_idx, controller=None, room_id=None,
                           door_number=None):
        """
        Handle unlocking a door on a Room card with proper trigger processing.
        Includes mana cost payment and trigger processing.

        Args:
            room_idx: Index of the Room card in the controller's battlefield.

        Returns:
            bool: True if door was unlocked successfully.
        """
        gs = self.game_state
        controller = controller or gs._get_active_player()
        transaction = self.get_unlockable_room_door(
            controller, room_idx, room_id=room_id, door_number=door_number)
        if not transaction:
            logging.debug(
                "No payable locked Room door at index %s for %s.",
                room_idx, controller.get("name", "unknown") if controller else "unknown")
            return False

        room_id = transaction["room_id"]
        room_card = transaction["room_card"]
        door_to_unlock_num = transaction["door_number"]
        door_data = transaction["door"]
        door_cost_str = transaction["cost_text"]
        if not gs.mana_system.pay_mana_cost(
                controller, transaction["parsed_cost"],
                transaction["payment_context"]):
            logging.warning(
                f"Failed to pay unlock cost {door_cost_str} for Door "
                f"{door_to_unlock_num} of {room_card.name}")
            return False

        # Unlock the door (Update the card's state)
        door_data['unlocked'] = True
        logging.info(f"Unlocked Door {door_to_unlock_num} for Room {room_card.name}")

        # --- Check if fully unlocked ---
        parsed_doors = [
            door for door in (
                getattr(room_card, "door1", None),
                getattr(room_card, "door2", None))
            if door]
        fully_unlocked = bool(parsed_doors) and all(
            door.get("unlocked", False) for door in parsed_doors)

        # --- Prepare context for triggers ---
        context = {
            "door_number": door_to_unlock_num,
            "room_id": room_id,
            "controller": controller,
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
                     "controller": controller,
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
                                           _fc = getattr(self.game_state, 'fidelity_counters', None)
                                           if _fc is not None: _fc["unparsed_mana"] += 1
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
        mode_effects = EffectFactory.create_effects(mode_effect_text, source_name=getattr(self.game_state._safe_get_card(card_id), 'name', None))
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
            """
            Parse modal text to extract mode descriptions and choice requirements.

            Args:
                text (str): The oracle text or ability effect text.

            Returns:
                tuple: (modes_list, min_choices, max_choices) or (None, 0, 0) if not modal.
                    modes_list contains the text of each mode.
            """
            modes = []
            min_choices, max_choices = 0, 0
            if not text:
                return None, 0, 0

            text_lower = text.lower()

            # Define markers and their corresponding min/max choices
            # Order matters - check more specific ones first
            markers = [
                ("choose two or more —", 2, float('inf')), # Very rare, treat max as inf for now
                ("choose three —", 3, 3),
                ("choose two —", 2, 2),
                ("choose one or both —", 1, 2), # Specific handling needed later?
                ("choose up to three —", 0, 3),
                ("choose up to two —", 0, 2),
                ("choose up to one —", 0, 1),
                ("choose one —", 1, 1),
                # *** CHANGED: Added em dash variations and common bullet style ***
                (r"choose one\s*[-—\u2014]\s*", 1, 1),
                (r"choose one\s*[•●]\s*", 1, 1) # Alternative marker using bullet itself
            ]

            start_index = -1
            marker_len = 0
            chosen_marker_info = None

            for marker_text, min_req, max_req in markers:
                # Use regex search for more flexible matching (handles whitespace variations)
                match = re.search(marker_text, text_lower)
                if match:
                    # Basic check: Is it preceded by text that might negate it being the primary modal?
                    # (e.g., part of another ability description) - Needs complex parsing. For now, assume first found marker is primary.
                    start_index = match.start() # Use match start index
                    marker_len = match.end() - match.start() # Use match length
                    chosen_marker_info = (match.group(0), min_req, max_req) # Use matched marker text
                    min_choices, max_choices = min_req, max_req
                    logging.debug(f"Found modal marker: '{match.group(0)}' in '{text[:100]}...'")
                    break

            modal_text = ""
            if start_index != -1:
                # Extract text *after* the matched marker
                modal_text = text[start_index + marker_len:]
            # Handle case where modes use bullet points without a standard "Choose X" intro
            # Ensure bullet is likely start of a list item (preceded by newline or start of text, possibly indented)
            # Use MULTILINE flag for ^
            elif re.search(r'(?:\n|^)\s*[•●]\s+', text, re.MULTILINE):
                # Assume standard "choose one" if no text marker found but bullets exist
                min_choices, max_choices = 1, 1
                # Find first bullet point that seems part of a list
                bullet_match = re.search(r"(?:\n|^)\s*[•●]\s+", text, re.MULTILINE)
                if bullet_match:
                    modal_text = text[bullet_match.start():] # Start parsing from the first bullet
                    logging.debug(f"Found bullet point modes without standard intro, assuming 'Choose one'.")
                else: # Should not happen if outer re.search passed, but safety check
                    return None, 0, 0
            else: # No modal markers or list-like bullets found
                return None, 0, 0

            modal_text = modal_text.strip()

            # Split by bullet points (primary method)
            # Handle different bullet characters and whitespace robustly
            # Use regex that captures the content *after* the bullet, handles start/end string
            mode_parts = re.split(r'(?:\n|^)\s*[•●]\s*', modal_text) # Split based on bullet at start of line

            # Clean up modes
            for part in mode_parts:
                if not part.strip(): continue # Skip empty parts resulting from split

                # Remove reminder text first
                cleaned_part = re.sub(r'\s*\([^()]*?\)\s*', ' ', part).strip()
                # Clean surrounding whitespace again after removal
                cleaned_part = cleaned_part.strip()
                # *** CHANGED: More robust cleanup - remove trailing punctuation AND any leading list-like markers if split didn't catch them ***
                cleaned_part = re.sub(r'^[-•●\d\W]+', '', cleaned_part).strip() # Remove leading list markers/punct
                cleaned_part = re.sub(r'[;,\.]+$', '', cleaned_part).strip() # Remove trailing punctuation

                if cleaned_part:
                    modes.append(cleaned_part)

            # If no modes found via bullets but a text marker *was* found, try sentence splitting (less reliable)
            if not modes and chosen_marker_info:
                logging.debug("No bullet points found after modal marker, trying sentence split.")
                # Split by sentence ending punctuation followed by potential start of next mode (Cap letter, list marker)
                sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z•●\d\+\-])', modal_text) # Split on sentence end + potential mode start
                for sentence in sentences:
                    # Apply same cleanup logic
                    cleaned_sentence = re.sub(r'\s*\([^()]*?\)\s*', ' ', sentence).strip()
                    cleaned_sentence = re.sub(r'^[-•●\d\W]+', '', cleaned_sentence).strip()
                    cleaned_sentence = re.sub(r'[;,\.]+$', '', cleaned_sentence).strip()
                    if cleaned_sentence:
                        modes.append(cleaned_sentence)

            # Handle 'inf' max_choices if needed (replace with actual number of modes)
            if max_choices == float('inf'):
                max_choices = len(modes) if modes else 0

            if not modes:
                # Log specific marker info if available
                marker_log = f" after marker '{chosen_marker_info[0]}'" if chosen_marker_info else ""
                logging.warning(f"Found modal indication{marker_log} but failed to parse mode texts from: '{modal_text[:100]}...'")
                _fc = getattr(self.game_state, 'fidelity_counters', None)
                if _fc is not None: _fc["unparsed_modal"] += 1
                return None, 0, 0

            logging.debug(f"Parsed modes: {modes}, MinChoices: {min_choices}, MaxChoices: {max_choices}")
            return modes, min_choices, max_choices
        
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
        Remove live effects associated with a card leaving the battlefield.

        Parsed ability definitions remain cached as last-known information so
        battlefield-to-graveyard triggers can be recognized after the move and
        abilities that function in other zones remain available. A trigger that
        has already fired also exists independently of its source, so pending
        triggers are intentionally preserved here.
        """
        try:
            card_name = getattr(self.game_state._safe_get_card(card_id), 'name', f"Card {card_id}")
            logging.debug(f"Unregistering abilities and effects for {card_name} ({card_id}).")

            # Remove continuous effects from LayerSystem
            if hasattr(self.game_state, 'layer_system'):
                removed_layer_count = self.game_state.layer_system.remove_effects_by_source(
                    card_id,
                    preserve_durations={"end_of_turn", "until_your_next_turn"})
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
            Handles regular cards, Class cards, MDFCs, keywords, and multi-ability paragraphs including bullet points.
            Clears previous abilities first. Applies static abilities immediately after parsing.
            Tracks activated ability indices.
            """
            if not card:
                logging.warning(f"Attempted to parse abilities for non-existent card {card_id}.")
                return

            # Ensure card has game_state reference if missing
            if not hasattr(card, 'game_state') or card.game_state is None:
                setattr(card, 'game_state', self.game_state)

            # Clear existing registered abilities for this card_id
            self.registered_abilities[card_id] = []

            # Fidelity snapshot: if any unparsed counters advance while parsing this
            # card, the card gets attributed by name for the downstream stats consumer.
            _fc = getattr(self.game_state, 'fidelity_counters', None)
            _fid_before = ((_fc.get("unparsed_mana", 0) + _fc.get("unparsed_modal", 0)
                            + _fc.get("unparsed_effects", 0)) if _fc is not None else 0)
            abilities_list = self.registered_abilities[card_id]
            activated_ability_counter = 0 # Tracks the index for non-mana activated abilities

            try:
                oracle_text_sources = []
                keywords_from_card_data = []
                processed_special_text_markers = "" # Track text handled by Spree, Class etc.

                # --- Get Text Source(s) and Keywords ---
                current_face_index = getattr(card, 'current_face', 0)
                card_layout = getattr(card, 'layout', None)
                is_mdfc = card_layout == 'modal_dfc'

                # Handle different card layouts/types to get text sources
                if hasattr(card, 'faces') and card.faces and current_face_index < len(card.faces):
                    face_data = card.faces[current_face_index]
                    oracle_text_sources.append(face_data.get('oracle_text', ''))
                    keywords_from_card_data = face_data.get('keywords', [])
                    # Special text handling for MDFC faces if needed (e.g., Spree)
                    # if is_mdfc and 'spree' in (kw.lower() for kw in keywords_from_card_data):
                        # processed_special_text_markers += getattr(card, '_spree_related_text_marker', '')
                elif hasattr(card, 'is_class') and card.is_class:
                    current_level_data = card.get_current_class_data()
                    if current_level_data:
                        # Class abilities are cumulative, get all abilities for the current level
                        oracle_text_sources.extend(current_level_data.get('all_abilities', []))
                        # These sources are already the exact unlocked Class
                        # ability rows; no level declaration text reaches the
                        # generic parser.  Processed markers are reserved for
                        # casting-owned blocks that remain in their original
                        # source text (Spree and Tiered).
                    if hasattr(card, 'keywords'): keywords_from_card_data = card.keywords
                else: # Normal card or other layout type
                    oracle_text_sources.append(getattr(card, 'oracle_text', ''))
                    if hasattr(card, 'keywords'): keywords_from_card_data = card.keywords
                    # Spree marker (check if base card itself is Spree)
                    if hasattr(card, 'is_spree') and card.is_spree:
                        processed_special_text_markers = getattr(card, '_spree_related_text_marker', '')
                    # Tiered rows are labelled spell modes with additional
                    # costs, not independent activated abilities.  Mark the
                    # complete declaration block as casting-owned so names
                    # such as ``Thunder — {0} — ...`` never reach the generic
                    # cost/effect parser.
                    if getattr(card, 'is_tiered', False):
                        tiered_marker = getattr(
                            card, '_tiered_related_text_marker', '')
                        if tiered_marker:
                            processed_special_text_markers = "\n".join(
                                marker for marker in (
                                    processed_special_text_markers,
                                    tiered_marker)
                                if marker)

                # --- Parse Keywords from Explicit Data ---
                handled_keywords = set() # Keep track of keywords already handled by _create_keyword_ability
                parsed_keywords_from_data = self._get_parsed_keywords(keywords_from_card_data)
                for keyword_text in parsed_keywords_from_data:
                    base_kw = keyword_text.split()[0].lower() # Get base keyword (e.g., 'flying')
                    oracle_lines = [
                        line.strip().lower()
                        for line in (getattr(card, 'oracle_text', '') or '').splitlines()
                        if line.strip()
                    ]
                    keyword_lines = [
                        line for line in oracle_lines
                        if re.search(r'\b' + re.escape(base_kw) + r'\b', line)
                    ]
                    conditional_markers = re.compile(
                        r'\b(?:during|as long as|if|when|whenever|until|unless)\b')
                    if (keyword_lines
                            and all(conditional_markers.search(line)
                                    for line in keyword_lines)):
                        # Scryfall's keyword list includes words used only by a
                        # conditional ability. The condition's own StaticAbility
                        # registers the layer effect; an unconditional keyword
                        # ability here would make it permanently active.
                        continue
                    if base_kw in Card.ALL_KEYWORDS:
                        if base_kw not in handled_keywords:
                            if self._create_keyword_ability(card_id, card, base_kw, abilities_list, full_keyword_text=keyword_text):
                                handled_keywords.add(base_kw)
                    # Handle comma-separated keywords explicitly if base check fails
                    elif ',' in keyword_text:
                        for sub_kw in keyword_text.split(','):
                            sub_kw_clean = sub_kw.strip().lower()
                            base_sub_kw = sub_kw_clean.split()[0]
                            if base_sub_kw in Card.ALL_KEYWORDS and base_sub_kw not in handled_keywords:
                                if self._create_keyword_ability(card_id, card, base_sub_kw, abilities_list, full_keyword_text=sub_kw_clean):
                                    handled_keywords.add(base_sub_kw)

                # --- Process Oracle Text Blocks ---
                processed_clauses_hashes = set()
                for text_block in oracle_text_sources:
                    if not text_block or not isinstance(text_block, str): continue

                    # Remove reminder text early. Only eat spaces/tabs around
                    # it, never newlines: "Impending 4—{2}{W}{W} (reminder)\n
                    # Whenever..." must stay two clauses, not fuse into one
                    # unclassifiable line.
                    cleaned_text_block = re.sub(r'[ \t]*\([^()]*?\)[ \t]*', ' ', text_block).strip()
                    if not cleaned_text_block: continue

                    # Check if the whole block was handled by special parser (Spree, Class)
                    if processed_special_text_markers and cleaned_text_block in processed_special_text_markers:
                        continue

                    # Split into clauses (paragraphs first, then single newlines)
                    potential_clauses = re.split(r'\n{2,}', cleaned_text_block) # Split by blank lines
                    final_clauses = []
                    for clause in potential_clauses:
                        # Numeric die-table rows belong to the ability on the
                        # preceding line. Preserve that block as one clause.
                        for sub_clause in clause.split('\n'):
                            cleaned_sub_clause = sub_clause.strip().rstrip('.').strip()
                            if not cleaned_sub_clause:
                                continue
                            if (re.match(r"^\d+(?:\s*[-\u2013\u2014]\s*\d+)?\s*\|",
                                         cleaned_sub_clause)
                                    and final_clauses):
                                final_clauses[-1] += "\n" + cleaned_sub_clause
                            elif (re.match(r"^[•●]\s*", cleaned_sub_clause)
                                    and final_clauses
                                    and (re.search(
                                        r"\bchoose\s+(?:one|two|one or both)\b",
                                        final_clauses[-1], re.IGNORECASE)
                                        or "\n•" in final_clauses[-1]
                                        or "\n●" in final_clauses[-1])):
                                final_clauses[-1] += "\n" + cleaned_sub_clause
                            else:
                                final_clauses.append(cleaned_sub_clause)


                    # Process each resulting clause/line
                    for clause_text in final_clauses:
                        if not clause_text: continue

                        # Loyalty abilities are parsed and activated by Card's
                        # dedicated loyalty engine. Treating their effect text
                        # as a normal static/activated ability can apply quoted
                        # emblem rules directly from the planeswalker.
                        if re.match(r'^[+\-\u2212]?\d+\s*:', clause_text):
                            continue

                        clause_hash = hash(clause_text)
                        if clause_hash in processed_clauses_hashes: continue # Skip identical text blocks
                        processed_clauses_hashes.add(clause_hash) # Mark original clause as processed

                        # Check if clause text was specifically handled by a marker
                        if processed_special_text_markers and clause_text in processed_special_text_markers:
                            continue

                        # Check if the clause *only* contains keywords already handled
                        # Parameterized keyword costs are not separate ability
                        # words.  For example, Oildeep Gearhulk's
                        # ``Lifelink, ward {1}`` is already represented by its
                        # two explicit keyword abilities; treating the ``1``
                        # inside the cost as an unhandled word synthesized a
                        # duplicate StaticAbility that no layer could classify.
                        keyword_only_clause = re.sub(
                            r'\{[^}]+\}', ' ', clause_text.lower())
                        words_in_clause = set(re.findall(
                            r'\b[a-z][a-z-]*\b', keyword_only_clause))
                        is_just_handled_keywords = False
                        if words_in_clause.issubset(handled_keywords):
                            # Further check: doesn't look like activated/triggered
                            if not (':' in clause_text or any(trig in clause_text.lower() for trig in ['when', 'whenever', 'at the beginning'])):
                                is_just_handled_keywords = True
                        if is_just_handled_keywords: continue # Skip parsing this clause

                        # --- *** NEW: Handle Bullet Points Within a Clause *** ---
                        if '•' in clause_text or '●' in clause_text:
                            # Check if this clause looks like a modal preamble (e.g., starts with "Choose one —")
                            # We rely on _parse_modal_text to handle these structures during activation phase.
                            # For registration, treat bullet points here as *potential* separate static/triggered abilities,
                            # unless the context clearly makes them modal options.
                            is_likely_modal_preamble = re.search(
                                r"\bchoose\s+(?:one|two|one or both)\s*[-—–]",
                                clause_text.lower())

                            if is_likely_modal_preamble:
                                # Don't split modal choices here; handle during activation.
                                # Just try to classify the preamble itself if it grants static ability (rare)
                                logging.debug(f"Skipping bullet split for likely modal preamble: '{clause_text[:50]}...'")
                                # (Optionally, parse just the preamble text before the bullets if needed)
                                # preamble_text = re.split(r'\s*[•●]', clause_text, 1)[0].strip()
                                # created_abilities = self._classify_and_parse_ability_clause(...) # Parse preamble
                                pass # Skip detailed parsing of modes here

                            else:
                                # Split by bullet points and process each part
                                logging.debug(f"Found bullets in clause, splitting: '{clause_text[:50]}...'")
                                # Use a regex that splits by the bullet and optional surrounding space, keeping content
                                # Split on newline + optional space + bullet + optional space OR just bullet + optional space
                                sub_parts = re.split(r'(?:\n|^)\s*[•●]\s*|\s*[•●]\s*', clause_text)

                                processed_sub_parts = 0
                                for sub_part in sub_parts:
                                    cleaned_sub_part = sub_part.strip().rstrip('.').strip()
                                    if not cleaned_sub_part: continue

                                    # Re-check if sub-part was marked as handled
                                    if processed_special_text_markers and cleaned_sub_part in processed_special_text_markers:
                                        continue

                                    # Classify and parse the sub-part
                                    created_abilities_sub = self._classify_and_parse_ability_clause(
                                        card_id, card, cleaned_sub_part, current_activated_index=activated_ability_counter
                                    )

                                    # Process results for sub-part
                                    if created_abilities_sub:
                                        processed_sub_parts += 1
                                        new_activated_count_sub = 0
                                        for ability in created_abilities_sub:
                                            setattr(ability, 'source_card', card)
                                            abilities_list.append(ability)
                                            if isinstance(ability, ActivatedAbility) and not isinstance(ability, ManaAbility):
                                                new_activated_count_sub += 1
                                        activated_ability_counter += new_activated_count_sub
                                # If we split by bullets and processed parts, skip processing the original full clause
                                if processed_sub_parts > 0:
                                    continue # Move to the next clause_text from final_clauses

                        # --- End Bullet Point Handling ---

                        # --- Process Clause as a Whole (If no bullets handled it) ---
                        created_abilities = self._classify_and_parse_ability_clause(
                            card_id, card, clause_text, current_activated_index=activated_ability_counter
                        )

                        # Process results
                        if created_abilities:
                            new_activated_count = 0
                            for ability in created_abilities:
                                # Attach card reference if not already done by classify function
                                if not getattr(ability, 'source_card', None):
                                    setattr(ability, 'source_card', card)
                                abilities_list.append(ability)
                                # Increment activated counter ONLY if a NEW ActivatedAbility was added
                                if isinstance(ability, ActivatedAbility) and not isinstance(ability, ManaAbility):
                                    new_activated_count += 1
                            activated_ability_counter += new_activated_count # Update main counter

                # Offspring's enters trigger lives only in reminder text on
                # current printed cards. Reminder removal leaves the keyword
                # declaration but no trigger clause, so synthesize the real
                # rules event and bind it to the paid-cost context recorded by
                # the battlefield-entry transaction.
                if (getattr(card, 'is_offspring', False)
                        and not any(
                            getattr(ab, '_is_offspring_etb_trigger', False)
                            for ab in abilities_list)):
                    def offspring_cost_was_paid(
                            ctx, _cid=card_id):
                        gs = (ctx or {}).get('game_state')
                        entering_card_id = (ctx or {}).get(
                            'card_id', (ctx or {}).get('event_card_id'))
                        paid_context = getattr(
                            gs, '_offspring_cost_paid_context', {}) if gs else {}
                        was_paid = bool(
                            gs and entering_card_id == _cid
                            and paid_context.get(_cid, False))
                        if was_paid:
                            # Freeze this immutable entry fact onto the event
                            # context before the transient per-entry map is
                            # cleared. Card IDs survive zone changes, so the
                            # map must not remain live until resolution.
                            ctx['_offspring_cost_was_paid'] = True
                        return was_paid

                    offspring_trigger = TriggeredAbility(
                        card_id=card_id,
                        trigger_condition="when this permanent enters",
                        effect="create a 1/1 token copy of it",
                        effect_text=(
                            "When this permanent enters, create a 1/1 token "
                            "copy of it."),
                        additional_condition=offspring_cost_was_paid)
                    offspring_trigger._is_offspring_etb_trigger = True
                    offspring_trigger.keyword = "offspring"
                    offspring_trigger.keyword_cost = getattr(
                        card, 'offspring_cost', None)
                    setattr(offspring_trigger, 'source_card', card)
                    abilities_list.append(offspring_trigger)
                    logging.debug(
                        f"Synthesized Offspring ETB trigger for {card.name}")

                # --- Synthesize the Impending end-step tick when its text is
                # reminder-only (the Overlords): the remove-a-counter trigger
                # exists solely inside stripped reminder text, so nothing
                # printed can register it. Only fires while time counters are
                # active on this permanent.
                if (getattr(card, 'is_impending', False)
                        and not any(getattr(ab, '_is_impending_remove_counter', False)
                                    for ab in abilities_list)):
                    tick = TriggeredAbility(
                        card_id=card_id,
                        trigger_condition="at the beginning of your end step",
                        effect="remove a time counter from it",
                        effect_text="At the beginning of your end step, remove a time counter from it.")
                    tick._is_impending_remove_counter = True
                    tick.additional_condition = (
                        lambda ctx, _cid=card_id: bool(
                            ctx and ctx.get('game_state')
                            and ctx['game_state']._is_impending_active(_cid)))
                    setattr(tick, 'source_card', card)
                    abilities_list.append(tick)
                    logging.debug(f"Synthesized Impending end-step tick for {card.name}")

                # An ability that explicitly watches an event "while this card
                # is in your graveyard" functions only from that graveyard.
                # Return-from-graveyard instructions have the same CR 113.6
                # exception unless their trigger is the source's zone change.
                for ability in abilities_list:
                    if (not isinstance(ability, TriggeredAbility)
                            or getattr(ability, 'zone', None)):
                        continue
                    effect_text = str(getattr(ability, 'effect', '')).lower()
                    trigger_text = str(
                        getattr(ability, 'trigger_condition', '')).lower()
                    watches_while_in_graveyard = bool(re.search(
                        r"\bwhile\s+this\s+card\s+is\s+in\s+your\s+graveyard\b",
                        trigger_text))
                    returns_self_from_graveyard = bool(re.search(
                        r"return\s+this\s+card\s+from\s+your\s+graveyard\s+to\s+the\s+battlefield",
                        effect_text))
                    source_moves_in_trigger = bool(re.search(
                        r"\b(?:dies|is discarded|is put into (?:a|your) graveyard|"
                        r"put into (?:a|your) graveyard from)\b",
                        trigger_text))
                    if (watches_while_in_graveyard
                            or (returns_self_from_graveyard
                                and not source_moves_in_trigger)):
                        ability.zone = 'graveyard'
                    if watches_while_in_graveyard:
                        # The event that first moved this object into its
                        # graveyard happened before this zone-only ability
                        # began functioning there.
                        ability._requires_preexisting_source_zone = True

                # --- Apply Static Abilities and Finalize ---
                logging.debug(f"Parsed {len(abilities_list)} total functional abilities for {card.name} ({card_id})")

                # Immediately apply newly registered Static Abilities
                static_abilities_applied_texts = []
                for ability in abilities_list:
                    if isinstance(ability, StaticAbility):
                        try:
                            # Ensure apply method exists before calling
                            if hasattr(ability, 'apply') and callable(ability.apply):
                                if ability.apply(self.game_state): # Apply returns True on successful registration
                                    static_abilities_applied_texts.append(ability.effect_text)
                        except Exception as static_apply_e:
                            logging.error(f"Error applying static ability '{ability.effect_text}' for {card.name}: {static_apply_e}", exc_info=True)
                if static_abilities_applied_texts:
                    logging.debug(f"Applied {len(static_abilities_applied_texts)} static abilities for {card.name}")

            except Exception as e:
                # Ensure card_id has an entry, even if empty, on error
                card_name_log = getattr(card, 'name', 'Unknown') # Safely get name for logging
                logging.error(f"Error parsing abilities for card {card_id} ({card_name_log}): {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                if card_id not in self.registered_abilities: self.registered_abilities[card_id] = []
                _fc = getattr(self.game_state, 'fidelity_counters', None)
                if _fc is not None:
                    _fc["unparsed_effects"] += 1
                    _fc.setdefault("unparsed_cards", set()).add(card_name_log)

            # Fidelity attribution: counters advanced during this card's parse.
            if _fc is not None:
                _after = (_fc.get("unparsed_mana", 0) + _fc.get("unparsed_modal", 0)
                          + _fc.get("unparsed_effects", 0))
                if _after > _fid_before:
                    _fc.setdefault("unparsed_cards", set()).add(getattr(card, 'name', f"card_{card_id}"))
            
    def _classify_and_parse_ability_clause(self, card_id, card, clause_text, current_activated_index):
        """
        Attempts to classify and parse a single text clause (paragraph/sentence)
        into one or more Activated, Triggered, or Static abilities.
        Handles Offspring ETB and Impending end step triggers.
        Uses the stricter ActivatedAbility parser.

        Returns:
            list: A list of created Ability objects (can be empty).
        """
        if not clause_text: return []

        abilities_found = []
        text_lower = clause_text.lower().strip()
        text_lower_stripped = text_lower.rstrip('.').strip() # Used for certain checks

        # Summon: Esper Maduin's chapter headings are triggered abilities,
        # even though they do not use ordinary ``when/whenever/at`` grammar.
        # They used to be discarded by the declaration filter below, so its
        # lore counters advanced without instructions reaching the stack.
        # Keep this exact-card gate until the wider Saga family (including
        # shared chapter headings) has matching runtime evidence.
        chapter_match = (
            re.match(
                r"^\s*([IVX]+)\s+[^A-Za-z0-9{]+\s*(.+?)\s*$",
                clause_text, re.IGNORECASE | re.DOTALL)
            if str(getattr(card, "name", "")).casefold()
            == "summon: esper maduin" else None)
        if chapter_match:
            roman = chapter_match.group(1).upper()
            total = 0
            previous = 0
            for symbol in reversed(roman):
                value = {"I": 1, "V": 5, "X": 10}.get(symbol, 0)
                total += -value if value < previous else value
                previous = max(previous, value)
            effect_text = chapter_match.group(2).strip().rstrip('.').strip()
            if total > 0 and effect_text:
                ability = TriggeredAbility(
                    card_id=card_id,
                    trigger_condition=f"saga chapter {total}",
                    effect=effect_text,
                    effect_text=clause_text.strip())
                ability.saga_chapter = total
                setattr(ability, 'source_card', card)
                abilities_found.append(ability)
                return abilities_found

        # Rules-changing static abilities such as Nowhere to Run are neither
        # layer-6 ability removal nor ordinary action effects. Keep them as
        # live battlefield markers for target legality and ward triggering.
        if ("creatures your opponents control can be the targets" in text_lower_stripped
                and "as though they didn't have hexproof" in text_lower_stripped):
            ability = TargetingOverrideAbility(
                card_id, "hexproof", effect_text=clause_text.strip())
            setattr(ability, 'source_card', card)
            abilities_found.append(ability)
            return abilities_found
        if "ward abilities of those creatures don't trigger" in text_lower_stripped:
            full_oracle = getattr(card, "oracle_text", "").lower()
            if "creatures your opponents control can be the targets" in full_oracle:
                ability = TargetingOverrideAbility(
                    card_id, "ward", effect_text=clause_text.strip())
                setattr(ability, 'source_card', card)
                abilities_found.append(ability)
                return abilities_found

        # --- 0. Skip likely Replacement Effects / Non-functional clauses ---
        # ... (patterns remain the same) ...
        replacement_or_non_ability_patterns = [
            r"^as\s+.*\s+enters\b",
            r"^if.*?would.*?instead",
            r"^if\s+a\s+source\s+would\s+deal\s+damage\s+to.*?prevent",
            r"^(?:this\s+\w+|[\w' -]+)\s+enters(?:\s+the\s+battlefield)?\s+tapped(?:\s+unless\b.*)?$",
            r"^if\s+this\s+card\s+is\s+in\s+your\s+opening\s+hand,?\s+you\s+may\s+begin\s+the\s+game\s+with\s+it\s+on\s+the\s+battlefield$",
            r"^enchant\b",
            # These declaration lines have dedicated engine paths. Treating
            # them as layer effects duplicates work and emits false warnings.
            r"^\s*saddle\s+\d+\s*$",
            r"^\s*plot\s*(?:\{[^}]+\})+\s*$",
            r"^\s*you may have this creature enter as a copy of\b",
            r"^\s*mind swap\s*[â€”\u2014-]\s*you may have .* enter as a copy of\b",
            r"^\s*domain\b", r"^\s*as an additional cost", r"^\s*collect evidence\s+\d+",
            r"^\s*[ivx]+\s*[—\u2014-]", r"^\s*Split second\b",
        ]
        # Keyword-cost declaration lines; Card parses these costs itself and
        # the reminder text that used to hide them is stripped before split.
        replacement_or_non_ability_patterns += [
            r"^\s*impending\s+\d+\s*(?:[——-]\s*(?:\{[^}]+\})+)?\s*$",
            r"^\s*offspring\s*(?:\{[^}]+\})+\s*$",
            # Handled by the discard pipeline (Obstinate Baloth).
            r"^\s*if a spell or ability an opponent controls causes you to discard this card",
            # Handled by ETB-counter replacement registration.
            r"^\s*this (?:creature|permanent|artifact|enchantment) enters(?: the battlefield)? with .*counter",
            r"^\s*[\w' ,.-]+ enters(?: the battlefield)? with .*counter",
            # Conditional replacements are registered by ReplacementEffectSystem.
            r"^\s*as long as .*\bif .*\bwould .*\binstead\b",
            # Dedicated rules/cost paths read these live declarations from
            # their battlefield source.  They are not layer effects.
            r"^\s*noncreature spells you cast cost \{\d+\} less to cast as long as there are \w+ or more lesson cards in your graveyard\s*$",
            r"^\s*this spell costs \{x\} less to cast, where x is the greatest mana value among elementals you control\s*$",
            r"^\s*this spell costs \{x\} less to cast, where x is the number of cards in your graveyard that are instant cards, sorcery cards, and/or have an adventure\s*$",
            r"^\s*this spell costs \{\d+\} less to cast for each instant and sorcery card in your graveyard\s*$",
            r"^\s*the first non-lemur creature spell with flying you cast during each of your turns costs \{1\} less to cast\s*$",
            r"^\s*this creature enters tapped if it(?:'|\u2019)s not your turn\s*$",
            r"^\s*warp\s+(?:\{[^}]+\})+\s*$",
            # CombatActionHandler evaluates this Delirium restriction from
            # live Oracle text for both attack and block legality.
            r"^\s*delirium\b.*can(?:'|\u2019)t attack or block unless there are four or more card types among cards in your graveyard\s*$",
            r"^\s*you may play an additional land on each of your turns\s*$",
            r"^\s*you may play lands from your graveyard\s*$",
            r"^\s*your opponents can(?:'|\u2019)t cast spells during your turn\s*$",
            # Stack counter legality and the as-enters type transaction own
            # these declarations; neither is a continuous layer effect.
            r"^\s*this spell can(?:'|\u2019)t be countered\s*$",
            r"^\s*this land is the chosen type\s*$",
            # Both keywords are already registered from card metadata.
            r"^\s*flying\s*,\s*ward\s+(?:\{[^}]+\}|\d+)\s*$",
            # Prepare state and its virtual exile copy are maintained by the
            # zone/casting transaction, not by a continuous layer.
            r"^\s*this (?:creature|permanent) enters prepared\s*$",
        ]
        if any(re.match(pattern, text_lower_stripped, re.IGNORECASE) for pattern in replacement_or_non_ability_patterns):
            # logging.debug(f"Skipping clause '{clause_text}' as likely Replacement/Non-functional Ability.")
            return []

        # --- 1. Handle Specific Keyword Triggers First (Offspring ETB, Impending End Step) ---

        # --- OFFSPRING ETB Trigger ---
        # Check card flag AND text pattern for ETB with cost paid condition leading to token copy
        if getattr(card, 'is_offspring', False) and \
           re.match(r"^\s*when this (?:creature|permanent) enters", text_lower_stripped) and \
           ("if the offspring cost was paid" in text_lower_stripped or "if its offspring cost was paid" in text_lower_stripped) and \
           ("token" in text_lower_stripped and ("copy of it" in text_lower_stripped or "copy of that creature" in text_lower_stripped or "1/1 token copy" in text_lower_stripped)):
            try:
                # Parse the main trigger part ("when this enters...") and the effect part
                trigger_condition, effect_part = TriggeredAbility._parse_condition_effect(clause_text.strip())
                if trigger_condition != "Unknown" and effect_part != "Unknown":
                    ability = TriggeredAbility(card_id=card_id, trigger_condition=trigger_condition, effect=effect_part, effect_text=clause_text.strip())
                    ability._is_offspring_etb_trigger = True # Flag it

                    # Add the condition function to check if the cost was paid *for this instance*
                    def offspring_condition(trigger_context):
                        gs = trigger_context.get('game_state')
                        # *** FIXED: Get the entering card ID from the ETB event context ***
                        entering_card_id = trigger_context.get('card_id') # This card triggered the ETB
                        cost_paid_context = getattr(gs, '_offspring_cost_paid_context', {})
                        # Check if the cost was paid for THIS specific card entering
                        was_paid = cost_paid_context.get(entering_card_id, False)
                        if was_paid:
                            trigger_context['_offspring_cost_was_paid'] = True
                        return gs and entering_card_id and was_paid

                    ability.additional_condition = offspring_condition # Use callable condition
                    setattr(ability, 'source_card', card) # Link card early
                    abilities_found.append(ability)
                    logging.debug(f"Registered Offspring ETB Trigger for {card.name}")
                    return abilities_found # Successfully handled as Offspring ETB
                else:
                     logging.warning(f"Offspring ETB structure parsed incorrectly for {card.name}: '{clause_text}'")

            except Exception as e: logging.error(f"Error parsing Offspring trigger: '{clause_text}'. E: {e}", exc_info=True)

        # --- IMPENDING End Step Trigger ---
        if getattr(card, 'is_impending', False) and \
           re.match(r"^\s*at the beginning of your end step", text_lower_stripped) and \
           "remove a time counter" in text_lower_stripped:
            try:
                trigger_condition, effect_part = TriggeredAbility._parse_condition_effect(clause_text.strip())
                if trigger_condition != "Unknown" and effect_part != "Unknown":
                     ability = TriggeredAbility(card_id=card_id, trigger_condition=trigger_condition, effect=effect_part, effect_text=clause_text.strip())
                     ability._is_impending_remove_counter = True # Flag it
                     setattr(ability, 'source_card', card) # Link card early
                     abilities_found.append(ability)
                     logging.debug(f"Registered Impending End Step trigger for {card.name}")
                     return abilities_found # Handled as Impending trigger
                else:
                     logging.warning(f"Impending End Step structure parsed incorrectly for {card.name}: '{clause_text}'")
            except Exception as e: logging.error(f"Error parsing Impending End Step trigger: '{clause_text}'. E: {e}", exc_info=True)


        # --- 2. Try parsing as Activated Ability (Stricter Check) ---
        # ... (Rest of the method remains the same) ...
        is_exhaust = False; text_to_parse_activated = clause_text
        exhaust_match = re.match(r"^\s*Exhaust\s*[,—\u2014-]?\s*(.+)", text_to_parse_activated, re.IGNORECASE | re.DOTALL)
        if exhaust_match: is_exhaust = True; text_to_parse_activated = exhaust_match.group(1).strip()

        activated_ability_instance = None
        try:
            ability = ActivatedAbility(
                card_id=card_id,
                effect_text=text_to_parse_activated,
                is_exhaust=is_exhaust,
                activation_index=current_activated_index # Pass index during init
            )
            if getattr(ability, 'cost', None) is not None and getattr(ability, 'effect', None) is not None and getattr(ability, 'cost', None) != "":
                mana_produced = self._parse_mana_produced(ability.effect)
                if mana_produced and any(mana_produced.values()):
                    ability = ManaAbility(
                        card_id=card_id, cost=ability.cost,
                        mana_produced=mana_produced,
                        effect_text=text_to_parse_activated)
                if current_activated_index is not None and getattr(ability, 'activation_index', None) is None:
                     setattr(ability, 'activation_index', current_activated_index)
                setattr(ability, 'source_card', card)
                abilities_found.append(ability)
                activated_ability_instance = ability
        except ValueError as e: pass
        except Exception as e: logging.error(f"Error attempting to parse as ActivatedAbility: {e}")

        if activated_ability_instance:
            if isinstance(activated_ability_instance, ManaAbility): logging.debug(f"Registered ManaAbility for {card.name}")
            else: logging.debug(f"Registered ActivatedAbility for {card.name}{' (Exhaust)' if is_exhaust else ''}")
            return abilities_found

        # --- 3. Check for Standard Triggered Ability ---
        # ... (Rest of the method remains the same) ...
        is_likely_triggered = False
        trigger_match = re.match(r'^\s*(When|Whenever|At\sthe\sbeginning\sof)\b', clause_text.strip(), re.IGNORECASE)
        etb_match = re.match(r"^\s*(?:(?:this|that)\s+(?:permanent|creature)\s+)?enters?\s+the\s+battlefield\b", text_lower_stripped, re.IGNORECASE)
        triggering_keywords_at_start = [
            'Valiant', 'Eerie', 'Prowess', 'Riot', 'Delirium',
            'Landfall', 'Opus',
        ]
        keyword_trigger_match = re.match(rf"^\s*({'|'.join(triggering_keywords_at_start)})\s*[—\u2014-]?\s*(?:When|Whenever|At)\b", clause_text.strip(), re.IGNORECASE)

        generic_ability_word_trigger = (
            None if keyword_trigger_match else
            _split_generic_ability_word_trigger_prefix(clause_text))

        if (trigger_match or etb_match or keyword_trigger_match
                or generic_ability_word_trigger):
             is_likely_triggered = True

        if is_likely_triggered:
             try:
                 trigger_text = clause_text.strip()
                 ability_word = None
                 if keyword_trigger_match:
                     ability_word = keyword_trigger_match.group(1).lower()
                     trigger_text = re.sub(
                         rf"^\s*{re.escape(keyword_trigger_match.group(1))}\s*[—\u2014-]?\s*",
                         "", trigger_text, count=1, flags=re.IGNORECASE)
                 elif generic_ability_word_trigger:
                     ability_word, trigger_text = generic_ability_word_trigger
                 ability = TriggeredAbility(card_id=card_id, effect_text=trigger_text)
                 if ability.trigger_condition != "Unknown" and ability.effect != "Unknown":
                     if ability_word:
                         ability.ability_word = ability_word
                         ability.oracle_ability_text = clause_text.strip()
                     setattr(ability, 'source_card', card)
                     abilities_found.append(ability)
                     logging.debug(f"Registered TriggeredAbility for {card.name}")
                     return abilities_found
             except ValueError: pass
             except Exception as e: logging.error(f"Error parsing as Triggered: '{clause_text}'. E: {e}")

        # --- 4. Try Static Ability (If not Activated/Triggered and fits criteria) ---
        # ... (Rest of the method remains the same) ...
        permanent_types = {
            'creature', 'artifact', 'enchantment', 'land', 'planeswalker',
            'battle', 'class', 'room',
        }
        is_permanent_type = (
            card and hasattr(card, 'card_types')
            and any(ct in permanent_types for ct in card.card_types))
        action_verb_pattern = r'\b(destroy|exile|counter|draw|discard|create|search|tap|untap|target|deal|sacrifice|return.*?to|put.*?on|put.*?into|attach|manifest|look at)\b'
        has_action_verb = bool(re.search(action_verb_pattern, text_lower))

        if is_permanent_type and not activated_ability_instance and not is_likely_triggered and not has_action_verb:
             try:
                 ability = StaticAbility(card_id=card_id, effect=clause_text.strip(), effect_text=clause_text.strip())
                 if ability.effect:
                      setattr(ability, 'source_card', card)
                      abilities_found.append(ability)
                      logging.debug(f"Registered StaticAbility for {card.name}")
                      return abilities_found
             except Exception as e: logging.error(f"Error parsing as Static: '{clause_text}'. E: {e}")

        # --- 5. Log Unclassified (If not parsed and seems relevant) ---
        # ... (Rest of the method remains the same) ...
        is_inst_sorc = card and hasattr(card, 'card_types') and any(ct in ['instant', 'sorcery'] for ct in card.card_types)
        is_adventure_effect = is_inst_sorc and hasattr(card, 'layout') and card.layout == 'adventure'
        if not abilities_found and not is_inst_sorc and not is_adventure_effect and \
           clause_text.strip() and not clause_text.strip().startswith("(") and is_permanent_type:
            logging.warning(f"Could not classify ability clause for {card.name}: '{clause_text}'")
            fidelity = getattr(self.game_state, "fidelity_counters", None)
            if fidelity is not None:
                fidelity["unparsed_effects"] = (
                    fidelity.get("unparsed_effects", 0) + 1)
                fidelity.setdefault("unparsed_cards", set()).add(card.name)
            try:
                from .card_support import report_unsupported
                report_unsupported(
                    card.name,
                    f"unclassified ability clause: {clause_text[:80]}",
                    severity="unparsed")
            except Exception:
                logging.debug(
                    "Could not persist unclassified support evidence for %s.",
                    card.name, exc_info=True)

        return abilities_found
            
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
        Distinguishes static, triggered, activated, and rule-modifying keywords.
        Connects rule-modifying keywords to game state/systems where appropriate.
        Returns True if an ability was successfully created and added, False otherwise.
        """
        keyword_lower = keyword_name.lower().strip() # Ensure clean keyword
        full_text = (full_keyword_text or keyword_name).lower().strip() # Use full text if provided

        # Card stores intrinsic keywords as a binary vector, so rebuilding the
        # keyword name above normally loses a printed numeric parameter.  Crew
        # needs that number for its non-mana activation cost; recover the
        # intrinsic declaration rather than silently using the generic Crew 1
        # fallback.  Checking the line with Card's intrinsic-keyword parser
        # avoids borrowing a value from text that merely grants Crew elsewhere.
        if (keyword_lower == "crew"
                and not re.search(r"\bcrew\s+(?:\d+|x)\b", full_text,
                                  re.IGNORECASE)):
            for oracle_line in str(
                    getattr(card, "oracle_text", "") or "").splitlines():
                if ("crew" in Card.intrinsic_keyword_names(oracle_line)
                        and re.search(r"\bcrew\s+(?:\d+|x)\b", oracle_line,
                                      re.IGNORECASE)):
                    full_text = oracle_line.lower().strip()
                    break

        # --- Basic validation ---
        if not keyword_lower or not full_text:
            logging.warning(f"Skipping keyword ability creation due to empty keyword/text.")
            return False # <<< Added Return

        # Enchant is an Aura attachment restriction, not a continuous layer
        # effect. Attachment legality reads the printed text directly.
        if keyword_lower == "enchant":
            return True
        # These ability/action words are implemented by their Oracle-text
        # transaction. Scryfall also lists them in ``keywords``; they do not
        # create an independent battlefield ability.
        if keyword_lower in {
                "scry", "landfall", "opus", "mind swap", "double"}:
            return True
        if keyword_lower == "evoke":
            cost_str = None
            for source_text in (
                    full_text, getattr(card, "oracle_text", "") or ""):
                match = re.search(
                    r"(?:^|\n)evoke\s+((?:\{[^}]+\})+)",
                    str(source_text), re.IGNORECASE)
                if match:
                    cost_str = match.group(1)
                    break

            def was_evoked(trigger_context):
                return str((trigger_context or {}).get(
                    "use_alt_cost", "")).lower() == "evoke"

            ability = TriggeredAbility(
                card_id,
                trigger_condition="when this creature enters",
                effect="sacrifice this creature",
                effect_text=(
                    "When this creature enters, sacrifice it if its evoke "
                    "cost was paid."),
                additional_condition=was_evoked)
            ability.keyword = "evoke"
            ability.keyword_cost = cost_str
            setattr(ability, "source_card", card)
            abilities_list.append(ability)
            return True
        if keyword_lower in {"warp", "ninjutsu"}:
            # These declarations are owned by dedicated public-action paths.
            # Warp handles its alternate cast and delayed exile in the casting
            # pipeline; Ninjutsu handles timing, return-as-cost, payment, and
            # tapped-and-attacking placement in the combat pipeline. Creating
            # a second generic ActivatedAbility would expose either mechanic
            # from the wrong zone.
            return True

        current_value = None
        is_parametrized_keyword = False
        cost_str = None
        ability = None # Initialize ability variable

        # --- Internal Helpers (Assume these exist elsewhere or are defined above) ---
        # Placeholder implementations for clarity:
        def _parse_value(text, keyword):
             keyword_pattern = re.escape(keyword)
             match_num = re.search(f"{keyword_pattern}\\s+(\\d+|x)\\b", text, re.IGNORECASE)
             if match_num:
                 val_str = match_num.group(1)
                 return val_str if val_str.lower() == 'x' else int(val_str)
             defaults = {'annihilator':1, 'poisonous':1, 'afterlife':1, 'fading':1, 'vanishing':1,
                         'reinforce':1, 'crew':1, 'scavenge':1, 'monstrosity':1, 'adapt':1,
                         'afflict':1, 'rampage':1, 'cascade':0, 'discover':0, 'suspend': 1,
                         'frenzy': 1} # Added frenzy default
             return defaults.get(keyword, 1)

        def _parse_cost(text, keyword):
             keyword_pattern = re.escape(keyword)
             # Basic Regex for mana costs: {W}, {2}, {G/U}, {X}, {W/P} etc. Needs refinement.
             mana_pattern = r"(\{([WUBRGCXSPMTQA0-9\/\.]+)\})"
             # Look for cost immediately after keyword, possibly with separator
             match_cost = re.search(rf"{keyword_pattern}\s*(?:—|-|–|:)?\s*({mana_pattern}+|\d+|pay \d+ life|discard a card|sacrifice (?:a|an) \w+)\b", text, re.IGNORECASE)
             if match_cost:
                 cost_part = match_cost.group(1).strip()
                 if cost_part.isdigit(): return f"{{{cost_part}}}" # Normalize '2' to '{2}'
                 # Check if already mana symbol format or handle life/discard/sac costs
                 if cost_part.startswith('{') or "life" in cost_part or "discard" in cost_part or "sacrifice" in cost_part:
                     return cost_part
             # Handle keyword-specific cost patterns (e.g., Level up {COST}: ..., Suspend N—{COST})
             if keyword == "level up":
                 cost_match = re.search(r"(\{.*?\})\s*:\s*Level Up", text, re.IGNORECASE)
                 if cost_match: return cost_match.group(1)
             if keyword == "suspend":
                 cost_match = re.search(r"suspend\s+\d+\s*—\s*(\{.*?\})", text, re.IGNORECASE)
                 if cost_match: return cost_match.group(1)
             if keyword == "retrace": return "Discard a land card"
             # Ward cost variations
             ward_match = re.search(rf"ward\s*(?:—|-|–|:)?\s*({mana_pattern}+|\d+|pay \d+ life|discard a card|sacrifice (?:a|an) \w+)", text, re.IGNORECASE)
             if ward_match:
                 ward_cost_part = ward_match.group(1).strip()
                 if ward_cost_part.isdigit(): return f"{{{ward_cost_part}}}"
                 return ward_cost_part

             return "{0}" # Default free cost

        def _word_to_number(word):
            mapping = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
            if isinstance(word, str) and word.isdigit(): return int(word)
            return mapping.get(str(word).lower(), 1)

        def _keyword_source_texts():
            """Return full text candidates for parameterized keyword parsing."""
            texts = []
            if full_text:
                texts.append(full_text)
            current_face_index = getattr(card, 'current_face', 0)
            faces = getattr(card, 'faces', None)
            if faces and current_face_index < len(faces):
                face_text = faces[current_face_index].get('oracle_text', '')
                if face_text:
                    texts.append(face_text)
            oracle_text = getattr(card, 'oracle_text', '')
            if oracle_text:
                texts.append(oracle_text)
            seen = set()
            unique_texts = []
            for text in texts:
                normalized = str(text).lower().strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    unique_texts.append(normalized)
            return unique_texts
        # --- End Internal Helpers ---


        # --- Value/Cost Parsing (Moved Ward before N to handle complex ward first) ---
        if keyword_lower == "protection":
            for source_text in _keyword_source_texts():
                match = re.search(r"protection from ([^.\n]+)", source_text)
                if match:
                    current_value = match.group(1).strip()
                    break
            is_parametrized_keyword = True
        elif keyword_lower == "ward":
            cost_str = None
            for source_text in _keyword_source_texts():
                cost_str = _parse_cost(source_text, keyword_lower) # Use cost parser
                if cost_str and cost_str != "{0}":
                    break
            if cost_str and cost_str != "{0}": current_value = cost_str # Use parsed cost as value
            else: current_value = "ward_generic" # Fallback
            is_parametrized_keyword = True
        elif keyword_lower in ["annihilator", "afflict", "fading", "vanishing", "rampage", "poisonous", "afterlife", "cascade", "reinforce", "crew", "scavenge", "monstrosity", "adapt", "discover", "frenzy"]:
            # Scryfall's keyword vector retains only the base word (``Crew``),
            # while the parameter lives in the face/oracle text (``Crew 4``).
            # Search every source text before accepting the generic default;
            # otherwise every real Vehicle silently registers as Crew 1.
            current_value = None
            keyword_pattern = re.escape(keyword_lower)
            for source_text in _keyword_source_texts():
                value_match = re.search(
                    rf"\b{keyword_pattern}\s+(\d+|x)\b",
                    source_text, re.IGNORECASE)
                if not value_match:
                    continue
                value_text = value_match.group(1)
                current_value = (
                    value_text if value_text.lower() == 'x'
                    else int(value_text))
                break
            if current_value is None:
                current_value = _parse_value(full_text, keyword_lower)
            is_parametrized_keyword = True
        elif keyword_lower in ["cycling", "equip", "fortify", "reconfigure", "unearth", "flashback", "bestow", "dash", "buyback", "madness", "transmute", "channel", "kicker", "entwine", "overload", "splice", "surge", "embalm", "eternalize", "jump-start", "escape", "awaken", "level up", "retrace", "ninjutsu"]:
            cost_str = _parse_cost(full_text, keyword_lower)
            current_value = cost_str
            is_parametrized_keyword = True
        elif keyword_lower == "suspend":
            cost_str = _parse_cost(full_text, keyword_lower)
            n_value = _parse_value(full_text, keyword_lower)
            is_parametrized_keyword = True
            current_value = (n_value, cost_str) # Store tuple (N, Cost)

        # --- Duplicate Check ---
        # This checks if an identical keyword (with the same value if parametrized) already exists in the list *for this card*.
        keyword_already_exists = False
        for existing_ability in abilities_list:
             if getattr(existing_ability, 'keyword', None) == keyword_lower:
                 # If keyword is parametrized, value must also match
                 if is_parametrized_keyword:
                     if getattr(existing_ability, 'keyword_value', None) == current_value:
                         keyword_already_exists = True; break
                 else: # If not parametrized, just matching keyword is enough
                      keyword_already_exists = True; break
        if keyword_already_exists:
             # logging.debug(f"Skipping duplicate keyword '{keyword_lower}' (Value: {current_value}) for {card.name}")
             return False # Do not add duplicate

        # --- Static Grant Keywords (Layer 6) -> StaticAbility ---
        static_keywords = ["flying", "first strike", "double strike", "trample", "vigilance", "haste", "lifelink", "deathtouch", "indestructible", "hexproof", "shroud", "reach", "menace", "defender", "unblockable", "protection", "ward", "landwalk", "islandwalk", "swampwalk", "mountainwalk", "forestwalk", "plainswalk", "fear", "intimidate", "shadow", "horsemanship", "infect", "wither", "changeling", "phasing", "banding", "flash"]
        is_static_grant = keyword_lower in static_keywords or "walk" in keyword_lower
        if is_static_grant:
            ability_effect_text = f"This permanent has {full_text}."
            ability_title = keyword_name.capitalize()
            if keyword_lower == "protection" and current_value: ability_title = f"Protection from {current_value}"
            elif keyword_lower == "ward" and current_value: ability_title = f"Ward {current_value}"
            elif "walk" in keyword_lower: ability_title = keyword_name.capitalize()

            ability = StaticAbility(card_id, ability_title, ability_effect_text)
            setattr(ability, 'keyword', keyword_lower)
            setattr(ability, 'keyword_value', current_value) # Store protection/ward detail here
            # Apply logic moved to main parser loop


        # --- Triggered Keywords -> TriggeredAbility ---
        triggered_map = { # Map keyword to (trigger_condition, effect_desc_template)
            "prowess": ("whenever you cast a noncreature spell", "this creature gets +1/+1 until end of turn."),
            "cascade": ("when you cast this spell", "Exile cards until you hit a nonland card with mana value less than {N}. You may cast it without paying its mana cost."),
            "storm": ("when you cast this spell", "Copy this spell for each spell cast before it this turn."),
            "exalted": ("whenever a creature you control attacks alone", "that creature gets +1/+1 until end of turn."),
            "annihilator": ("whenever this creature attacks", "defending player sacrifices {N} permanents."),
            "battle cry": ("whenever this creature attacks", "each other attacking creature gets +1/+0 until end of turn."),
            "extort": ("whenever you cast a spell", "you may pay {W/B}. If you do, each opponent loses 1 life and you gain that much life."),
            "afflict": ("whenever this creature becomes blocked", "defending player loses {N} life."),
            "enrage": ("whenever this creature is dealt damage", "trigger its enrage effect."),
            "mentor": ("whenever this creature attacks", "put a +1/+1 counter on target attacking creature with lesser power."),
            "afterlife": ("when this permanent dies", "create {N} 1/1 white and black Spirit creature tokens with flying."),
            "ingest": ("whenever this creature deals combat damage to a player", "that player exiles the top card of their library."),
            "poisonous": ("whenever this creature deals combat damage to a player", "that player gets {N} poison counters."),
            "rebound": ("if this spell was cast from hand, instead of graveyard", "exile it. At beginning of your next upkeep, you may cast it from exile without paying its mana cost."),
            "gravestorm": ("when you cast this spell", "Copy this spell for each permanent put into a graveyard this turn."),
            "training": ("whenever this creature attacks with another creature with greater power", "put a +1/+1 counter on this creature."),
            "undying": ("when this permanent dies", "if it had no +1/+1 counters on it, return it to the battlefield under its owner's control with a +1/+1 counter on it."),
            "persist": ("when this permanent dies", "if it had no -1/-1 counters on it, return it to the battlefield under its owner's control with a -1/-1 counter on it."),
            "decayed": ("this creature can't block.", "When it attacks, sacrifice it at end of combat."), # Multi-part
            "rampage": ("whenever this creature becomes blocked", "it gets +{N}/+{N} until end of turn for each creature blocking it beyond the first."),
            "fading": ("this permanent enters the battlefield with {N} fade counters on it.", "at the beginning of your upkeep, remove a fade counter. if you can't, sacrifice it."), # Multi-part
            "vanishing": ("this permanent enters the battlefield with {N} time counters on it.", "at the beginning of your upkeep, remove a time counter. when the last is removed, sacrifice it."), # Multi-part
            "haunt": ("When this creature dies, exile it haunting target creature.", "When the haunted creature dies, trigger haunt effect."),
            "discover": ("when you cast this spell", "Exile cards until you hit a nonland card with mana value {N} or less. You may cast it without paying its mana cost or put it into your hand."),
            "living weapon": ("when this equipment enters the battlefield", "create a 0/0 black Phyrexian Germ creature token, then attach this to it."),
            "frenzy": ("whenever this creature attacks and isn't blocked", "it gets +{N}/+0 until end of turn."),
        }
        if keyword_lower in triggered_map:
            trigger_cond_tmpl, effect_desc_tmpl = triggered_map[keyword_lower]
            val_str = str(current_value) if current_value is not None else "N"
            effect_desc = effect_desc_tmpl.format(N=val_str)
            trigger_cond = trigger_cond_tmpl # Trigger condition usually doesn't change with N

            if keyword_lower == "decayed":
                # Create static 'cant block' ability
                static_part = StaticAbility(card_id, "This creature can't block.", "This creature can't block.")
                setattr(static_part, 'keyword', 'cant_block_static')
                setattr(static_part, 'source_card', card)
                abilities_list.append(static_part);
                if hasattr(static_part, 'apply') and callable(static_part.apply):
                     static_part.apply(self.game_state)
                # Define the trigger part
                trigger_cond = "when this creature attacks"; effect_desc = "sacrifice it at end of combat."
                ability = TriggeredAbility(card_id, trigger_cond, effect_desc, effect_text=f"Decayed - {effect_desc}")

            elif keyword_lower == "fading" or keyword_lower == "vanishing":
                counter_type = "fade" if keyword_lower == "fading" else "time"
                val = current_value # Parsed value for N
                etb_trigger = "when this permanent enters the battlefield"
                etb_effect = f"put {val} {counter_type} counters on it."
                etb_ability = TriggeredAbility(card_id, etb_trigger, etb_effect, effect_text=f"{keyword_name} ETB {val} {counter_type} counters")
                setattr(etb_ability, 'keyword', f"{keyword_lower}_etb")
                setattr(etb_ability, 'source_card', card)
                abilities_list.append(etb_ability)
                # Define the upkeep trigger part
                trigger_cond = "at the beginning of your upkeep"
                effect_desc = f"remove a {counter_type} counter from it. if you can't, sacrifice it."
                ability = TriggeredAbility(card_id, trigger_cond, effect_desc, effect_text=f"{keyword_name} Upkeep Check")
            else:
                # Standard trigger creation
                ability = TriggeredAbility(card_id, trigger_cond, effect_desc, effect_text=full_text)

            # Set attributes and add to list if created
            if isinstance(ability, TriggeredAbility):
                 setattr(ability, 'keyword', keyword_lower)
                 setattr(ability, 'keyword_value', current_value) # Store N value if parsed
                 # Set source_card handled below in the final check
                 # abilities_list.append(ability) # Append handled in the final check

        # --- Activated Keywords -> ActivatedAbility ---
        activated_map = { # Map keyword to effect description template (cost handled separately)
            "cycling": "discard this card: draw a card.",
            "equip": "attach to target creature you control. Equip only as a sorcery.",
            "fortify": "attach to target land you control. Fortify only as a sorcery.",
            "level up": "put a level counter on this creature. Level up only as a sorcery.",
            "unearth": "return this card from your graveyard to the battlefield. It gains haste. Exile it at the beginning of the next end step or if it would leave the battlefield. Unearth only as a sorcery.",
            "channel": "discard this card: activate its channel effect.",
            "transmute": "discard this card: search your library for a card with the same mana value as this card, reveal it, put it into your hand, then shuffle. Transmute only as a sorcery.",
            "reconfigure": "attach to target creature you control or unattach from a creature. Reconfigure only as a sorcery.",
            "crew": "tap any number of untapped creatures you control with total power {N} or greater: this Vehicle becomes an artifact creature until end of turn.",
            "scavenge": "exile this card from your graveyard: Put {N} +1/+1 counters on target creature. Activate only as a sorcery.",
            "reinforce": "discard this card: Put {N} +1/+1 counters on target creature.",
            "morph": "turn this face up.",
            "outlast": "put a +1/+1 counter on this creature. Outlast only as a sorcery.",
            "monstrosity": "put {N} +1/+1 counters on this creature and it becomes monstrous. Activate only as a sorcery.",
            "adapt": "If this creature has no +1/+1 counters on it, put {N} +1/+1 counters on it.",
            "boast": "activate boast effect.", # Cost/effect on card
            "flashback": "Cast this card from your graveyard for its flashback cost. Then exile it.",
            "jump-start": "Cast this card from your graveyard by discarding a card in addition to paying its other costs. Then exile it.",
            "retrace": "You may cast this card from your graveyard by discarding a land card in addition to paying its other costs.",
            "embalm": "Exile this card from your graveyard: Create a token that's a copy of it, except it's a white Zombie [OriginalType] with no mana cost. Embalm only as a sorcery.",
            "eternalize": "Exile this card from your graveyard: Create a token that's a copy of it, except it's a 4/4 black Zombie [OriginalType] with no mana cost. Eternalize only as a sorcery.",
            "ninjutsu": "Return an unblocked attacker you control to hand: Put this card onto the battlefield from your hand tapped and attacking.",
        }
        battlefield_activated = ["equip", "fortify", "level up", "reconfigure", "crew", "outlast", "monstrosity", "adapt", "boast"]
        hand_activated = ["cycling", "channel", "transmute", "reinforce", "ninjutsu"]
        gy_activated = ["unearth", "scavenge", "flashback", "jump-start", "retrace", "embalm", "eternalize"]
        other_zone_activated = ["morph"]

        if keyword_lower in activated_map:
            cost_to_use = ("{0}" if keyword_lower == "crew"
                           else current_value if current_value is not None
                           else "{0}")
            effect_desc_tmpl = activated_map[keyword_lower]
            val_str = str(
                current_value if current_value is not None
                else _parse_value(full_text, keyword_lower))
            effect_desc = effect_desc_tmpl.format(N=val_str)

            # Cost is parsed from current_value/cost_str for ActivatedAbility
            # Use full_text as the effect_text passed to constructor for better context
            ability = ActivatedAbility(card_id, cost_to_use, effect_desc, effect_text=full_text)
            setattr(ability, 'keyword', keyword_lower)
            setattr(ability, 'keyword_value', cost_to_use) # Store parsed cost
            if keyword_lower == "crew":
                setattr(ability, 'crew_power', int(val_str.strip('{}')))

            if keyword_lower in hand_activated: setattr(ability, 'zone', 'hand')
            elif keyword_lower in gy_activated: setattr(ability, 'zone', 'graveyard')
            elif keyword_lower == 'morph': setattr(ability, 'zone', 'face_down')
            else: setattr(ability, 'zone', 'battlefield') # Default to battlefield

            # Append handled in final check


        # --- Rule Modifying Keywords -> Metadata Marker ---
        rule_keywords = {
            "affinity": "artifact", "convoke": True, "delve": True, "improvise": True,
            "bestow": "cost", "buyback": "cost", "entwine": "cost", "escape": "cost_and_gy", "kicker": "cost",
            "madness": "cost", "overload": "cost", "splice": "cost", "surge": "cost", "spree": True,
            "split second": True, "suspend": "cost_and_time", "companion": True, "rebound": True,
            "phasing": True, "banding": True, "awaken": "cost_and_counters",
        }
        if keyword_lower in rule_keywords:
            # The casting/rules pipelines implement these mechanics directly
            # from card data and Oracle text. Keep the parsed keyword metadata
            # available to consumers, but do not misrepresent it as a
            # continuous StaticAbility: doing so sends declarations such as
            # Kicker and Spree into the layer system as unsupported effects.
            ability_effect_text = f"Rule Keyword: {full_text}"
            ability = Ability(card_id, ability_effect_text)
            setattr(ability, 'metadata_only', True)
            setattr(ability, 'keyword', keyword_lower)
            kw_cost = None
            kw_value = None
            cost_context = rule_keywords[keyword_lower]

            if cost_context == "cost": kw_cost = _parse_cost(full_text, keyword_lower)
            elif cost_context == "cost_and_gy": # Escape
                kw_cost = _parse_cost(full_text, keyword_lower)
                match = re.search(r"exile (\w+|\d+) other cards?", full_text, re.IGNORECASE)
                kw_value = _word_to_number(match.group(1)) if match else 1
            elif cost_context == "cost_and_time": # Suspend
                 kw_value, kw_cost = current_value if isinstance(current_value, tuple) and len(current_value) == 2 else (_parse_value(full_text, keyword_lower), _parse_cost(full_text, keyword_lower))
            elif cost_context == "cost_and_counters": # Awaken
                kw_cost = _parse_cost(full_text, keyword_lower)
                match = re.search(r"put (\w+|\d+) \+1/\+1 counters", full_text, re.IGNORECASE)
                kw_value = _word_to_number(match.group(1)) if match else 1
            elif isinstance(cost_context, str): kw_value = cost_context

            setattr(ability, 'keyword_cost', kw_cost)
            setattr(ability, 'keyword_value', kw_value)
            # Append handled in final check

        # --- Impending Keyword ---
        if keyword_name.lower().startswith("impending"):
            # Parse N from "Impending N"
            match = re.match(r"impending\s+(\d+)", full_keyword_text or keyword_name, re.IGNORECASE)
            n = int(match.group(1)) if match else 1
            # Register two triggers: one for end step to remove a time counter, one for last counter removed
            # 1. End step: remove a time counter
            end_step_trigger = TriggeredAbility(
                card_id, "at the beginning of your end step", f"remove a time counter from this permanent.",
                effect_text=f"Impending {n} - Remove time counter at end step"
            )
            end_step_trigger._is_impending_remove_counter = True
            end_step_trigger.impending_n = n
            abilities_list.append(end_step_trigger)
            # 2. Last counter removed: becomes a creature
            last_counter_trigger = TriggeredAbility(
                card_id, "when the last time counter is removed from this permanent", "it becomes a creature.",
                effect_text=f"Impending {n} - Becomes creature when last counter removed"
            )
            last_counter_trigger._is_impending_final_trigger = True
            last_counter_trigger.impending_n = n
            abilities_list.append(last_counter_trigger)
            return True

        # --- Final Append and Logging ---
        if ability:
            setattr(ability, 'source_card', card) # Ensure card link
            abilities_list.append(ability)
            # Static abilities are registered exactly once by the final loop
            # in _parse_and_register_abilities.  Applying keyword statics here
            # as well duplicated their layer entries.
            logging.debug(f"Created {type(ability).__name__} for keyword: {full_text} (Card: {card.name})")
            return True # Indicate success
        else:
             # Fallback warning if keyword wasn't categorized
             if not is_static_grant and keyword_lower not in triggered_map and keyword_lower not in activated_map and keyword_lower not in rule_keywords:
                  logging.warning(f"Keyword '{keyword_lower}' (from '{full_text}') on {card.name} not explicitly mapped or parsed.")
             return False # Indicate no ability created
            
    def _parse_ability_text(self, card_id, card, ability_text, abilities_list, is_exhaust=False, current_activated_index=None):
        """
        Parse a single ability text string. Delegates parsing logic to Ability subclasses.
        Tries to identify Activated, Triggered, or Static, in that order. Handles em dashes.
        Avoids classifying effects clearly handled by replacements (like copy ETBs).
        Accepts is_exhaust and current_activated_index to correctly label exhaust abilities.
        Checks card type before defaulting to StaticAbility.
        """
        ability_text = ability_text.strip()
        if not ability_text: return

        # --- Check for Replacement Effect Patterns first ---
        replacement_keywords = ["if", "would", "instead", "as", "with"]
        text_lower_for_check = ability_text.lower() # Use lower for keyword checks
        if all(kw in text_lower_for_check for kw in ["if", "would", "instead"]) or \
           (text_lower_for_check.startswith("as ") and "enters the battlefield" in text_lower_for_check) or \
           (text_lower_for_check.startswith("enters the battlefield with ") and "counter" in text_lower_for_check):
            logging.debug(f"Skipping parsing functional ability for text resembling replacement/ETB: '{ability_text}'")
            return

        # 1. Try parsing as Activated Ability (Cost[:—] Effect)
        try:
            # Temporarily create to attempt parsing
            ability = ActivatedAbility(card_id=card_id, effect_text=ability_text, cost="placeholder", effect="placeholder")
            # Check if the constructor successfully found a cost AND effect
            if ability.cost != "placeholder" and ability.effect is not None:
                 # Check if it's actually a Mana Ability
                 if "add {" in ability.effect.lower() or "add mana" in ability.effect.lower():
                      mana_produced = self._parse_mana_produced(ability.effect)
                      if mana_produced and any(mana_produced.values()):
                           mana_ability = ManaAbility(card_id=card_id, cost=ability.cost, mana_produced=mana_produced, effect_text=ability_text)
                           setattr(mana_ability, 'source_card', card)
                           abilities_list.append(mana_ability)
                           logging.debug(f"Registered ManaAbility for {card.name}: {mana_ability.effect_text}")
                           return # Parsed as Mana Ability, EXITS function
                 # Parsed as standard Activated
                 setattr(ability, 'source_card', card)
                 # --- ADDED: Attach exhaust flags and activation index ---
                 if is_exhaust and current_activated_index is not None:
                     setattr(ability, 'is_exhaust', True)
                     setattr(ability, 'activation_index', current_activated_index)
                     logging.debug(f"  Attached is_exhaust=True, activation_index={current_activated_index}")
                 elif current_activated_index is not None: # Store index even if not exhaust
                      setattr(ability, 'activation_index', current_activated_index)
                 else:
                      # Fallback: If index wasn't provided but we *know* it's exhaust (e.g. direct call), error?
                      if is_exhaust:
                         logging.error(f"Exhaust ability '{ability_text}' parsed without an activation index!")
                 # --- END ADDED ---
                 abilities_list.append(ability)
                 logging.debug(f"Registered ActivatedAbility for {card.name}: {ability.effect_text}{' (Exhaust)' if is_exhaust else ''}")
                 return # Parsed as Activated Ability, EXITS function
            # If placeholder remains, it didn't parse as activated. Fall through.
        except ValueError: pass # Pattern not found or invalid cost/effect structure
        except Exception as e: logging.error(f"Error attempting to parse as ActivatedAbility: {e}")

        # 2. Try parsing as Triggered Ability (When/Whenever/At ..., Effect)
        try:
            ability = TriggeredAbility(card_id=card_id, effect_text=ability_text, trigger_condition="placeholder", effect="placeholder")
            if ability.trigger_condition != "placeholder" and ability.effect is not None:
                 setattr(ability, 'source_card', card)
                 abilities_list.append(ability)
                 logging.debug(f"Registered TriggeredAbility for {card.name}: {ability.effect_text}")
                 return # Parsed as Triggered, EXITS function
            # If placeholder remains, fall through.
        except ValueError: pass
        except Exception as e: logging.error(f"Error attempting to parse as TriggeredAbility: {e}")

        # 3. Assume Static if not Activated/Triggered AND doesn't look like simple keyword AND IS a permanent type
        # Basic check to avoid re-registering keywords already handled
        is_simple_keyword = ability_text.lower() in [kw.lower() for kw in Card.ALL_KEYWORDS]
        already_registered_as_keyword = any(getattr(a, 'keyword', None) == ability_text.lower() for a in abilities_list)

        # Add check for common action verbs unlikely in static abilities
        action_verbs = [r'\b(destroy|exile|counter|draw|discard|create|search|tap|untap|target|deal|sacrifice)\b']
        is_likely_action = any(re.search(verb, text_lower_for_check) for verb in action_verbs)

        # --- ADDED: Check if the source card is a type that can have inherent static abilities ---
        permanent_types = {'creature', 'artifact', 'enchantment', 'land', 'planeswalker', 'battle', 'class', 'room'}
        is_permanent_type = card and hasattr(card, 'card_types') and any(ct in permanent_types for ct in card.card_types)
        # --- END ADDED ---

        # Only consider static if it's a permanent type, not likely an action, and not a handled keyword
        if is_permanent_type and not is_likely_action and (not is_simple_keyword or not already_registered_as_keyword):
            try:
                # Treat the whole text as the static effect description
                ability = StaticAbility(card_id=card_id, effect=ability_text, effect_text=ability_text)
                setattr(ability, 'source_card', card)
                abilities_list.append(ability)
                logging.debug(f"Registered StaticAbility for {card.name}: {ability.effect_text}")
                # StaticAbility.apply() called later by _parse_and_register_abilities
                return # Parsed as Static, EXITS function
            except Exception as e:
                logging.error(f"Error attempting to parse as StaticAbility: {e}")

        # If none of the above worked, log it unless it was handled keyword or a non-permanent
        if not is_permanent_type:
             # It's an Instant/Sorcery effect, skip static classification. Let EffectFactory handle on resolution.
             logging.debug(f"Skipped static classification for non-permanent card {card.name}: '{ability_text}'")
        elif not is_simple_keyword or not already_registered_as_keyword:
            if not is_likely_action:
                 logging.debug(f"Could not classify ability text for {card.name}: '{ability_text}' (Potentially Replacement/Action/Keyword?)")
        
    def _parse_mana_produced(self, mana_text):
         """Parses a mana production string (e.g., "add {G}{G}") into a dict."""
         produced = defaultdict(int) # Use defaultdict for easier counting
         mana_text_lower = mana_text.lower()

         # Alternative output packages are one choice, not cumulative mana.
         # This covers forms such as "Add {C} or one mana of any color" and
         # "Add {W}{W} or {U}{U}" while preserving the compact two-color
         # path below for ordinary dual lands.
         add_clause = re.search(r"\badd\s+(.+?)(?:\.|$)", mana_text_lower)
         alternatives = (re.split(r"\s+or\s+", add_clause.group(1))
                         if add_clause else [])
         if len(alternatives) > 1:
             output_options = []
             for alternative in alternatives:
                 package = defaultdict(int)
                 for symbol in re.findall(
                         r"\{([wubrgc]|\d+)\}", alternative):
                     if symbol.isdigit():
                         package["C"] += int(symbol)
                     else:
                         package[symbol.upper()] += 1
                 if "mana of any color" in alternative:
                     amount_match = re.search(
                         r"(one|two|three|four|five|\d+)\s+mana",
                         alternative)
                     number_words = {
                         "one": 1, "two": 2, "three": 3,
                         "four": 4, "five": 5,
                     }
                     raw = amount_match.group(1) if amount_match else "one"
                     package["any"] += (
                         int(raw) if raw.isdigit() else number_words[raw])
                 output_options.append(dict(package))
             simple_colors = [
                 next(iter(option))
                 for option in output_options
                 if len(option) == 1
                 and next(iter(option)) in "WUBRG"
                 and next(iter(option.values())) == 1
             ]
             if len(simple_colors) != len(output_options):
                 return {"output_options": output_options}

         # Mutually exclusive printed outputs are a policy color choice, not
         # cumulative production ("Add {R} or {G}" must never add both).
         choice_clause = re.search(
             r"\badd\s+\{([wubrg])\}\s+or\s+\{([wubrg])\}",
             mana_text_lower)
         if choice_clause and choice_clause.group(1) != choice_clause.group(2):
             return {"choice": 1}

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
        Handles EXHAUST_ABILITY_ACTIVATED event and checks activator relationship. (Revised Zone Logic)
        """
        if context is None: context = {}
        gs = self.game_state
        if not gs: logging.error("check_abilities: GameState missing!"); return False

        # Add game state and event type to context for condition checks
        context['game_state'] = gs
        context['event_type'] = event_type
        event_controller = context.get('controller')
        if (event_type == 'ENTER_EXILE'
                and context.get('from_zone') == 'battlefield'):
            event_controller = context.get('from_player') or event_controller
        context.setdefault('event_controller', event_controller)

        event_card = gs._safe_get_card(event_origin_card_id)
        context['event_card_id'] = event_origin_card_id
        context['event_card'] = event_card

        event_card_name = getattr(event_card, 'name', event_origin_card_id) if event_origin_card_id else "Game Event"
        # logging.debug(f"Checking triggers for event: {event_type} (Origin: {event_card_name}) Context keys: {list(context.keys())}")
        # if event_type == "EXHAUST_ABILITY_ACTIVATED": logging.debug(f"  (Exhaust activation by {context.get('activator', {}).get('name', 'Unknown')})")

        # Collect ALL registered abilities first, regardless of zone initially
        abilities_to_check = []
        player_refs = {'p1': gs.p1, 'p2': gs.p2}
        # Iterate through players and their owned cards that *might* have abilities functioning in certain zones
        # Battlefield is primary, Graveyard is common, Hand/Library/Exile are rare but possible
        potential_zones = ["battlefield", "graveyard", "hand"]
        for player_id, player_obj in player_refs.items():
             if player_obj:
                 for zone_name in potential_zones:
                     for card_id in player_obj.get(zone_name, []):
                          if card_id in self.registered_abilities:
                               # Store (card_id, ability, player_controlling_ability, zone_name)
                               abilities_to_check.extend([(card_id, ab, player_obj, zone_name) for ab in self.registered_abilities[card_id]])

        # Self-cast triggers ("When you cast this spell, ...") live on the
        # spell itself, which is on the stack during CAST_SPELL — a zone the
        # loop above never scans, so those triggers silently never fired.
        if event_type == "CAST_SPELL":
            cast_id = context.get('cast_card_id')
            caster = context.get('casting_player') or context.get('controller')
            if cast_id is not None and caster and cast_id in self.registered_abilities:
                abilities_to_check.extend(
                    (cast_id, ab, caster, 'stack')
                    for ab in self.registered_abilities[cast_id])

        # ENTER_EXILE is emitted after the moved card reaches exile. Include
        # only that event object so its own battlefield LKI trigger can be
        # checked; unrelated cards in exile remain outside the live scan.
        if (event_type == "ENTER_EXILE"
                and event_origin_card_id in self.registered_abilities):
            exile_owner, event_zone = gs.find_card_location(
                event_origin_card_id)
            if event_zone == 'exile' and exile_owner is not None:
                trigger_controller = (
                    context.get('event_controller') or exile_owner)
                abilities_to_check.extend(
                    (event_origin_card_id, ab, trigger_controller, 'exile')
                    for ab in self.registered_abilities[
                        event_origin_card_id])

        # Iterate through collected potential triggers
        queued_trigger_count = 0
        for card_id, ability, controller, zone_name in abilities_to_check:
             if not isinstance(ability, TriggeredAbility): continue

             layer_system = getattr(gs, 'layer_system', None)
             if (zone_name == 'battlefield' and layer_system
                     and layer_system.source_has_lost_all_abilities(card_id)):
                 continue
             if (card_id == event_origin_card_id
                     and (context.get('last_known') or {}).get(
                         'lost_all_abilities', False)):
                 continue

             source_card = gs._safe_get_card(card_id) # Fetch card for context checks
             if not source_card: continue # Card disappeared?

             # --- Zone Filtering Logic ---
             # Check if the ability can trigger from its *current* zone for the *given* event
             # Abilities generally function from the battlefield unless specified otherwise.
             triggering_zone = getattr(ability, 'zone', 'battlefield').lower() # e.g., 'graveyard' for escapes
             trigger_text = getattr(ability, 'trigger_condition', '').lower()
             can_trigger_from_current_zone = False

             # 1. Standard Zone Match: If ability trigger zone matches current card zone
             if triggering_zone == zone_name:
                 entered_required_zone_during_event = (
                     getattr(
                         ability, '_requires_preexisting_source_zone', False)
                     and card_id == event_origin_card_id
                     and context.get('from_zone') != zone_name)
                 can_trigger_from_current_zone = not entered_required_zone_during_event

             # 2. Zone Change Triggers: Check if event involves a zone change relevant to the ability.
             #    Uses LTB rule: Ability triggers based on game state *before* the event.
             elif event_type == "DIES": # Moving from Battlefield to Graveyard
                 # Only the object that just died may use last-known battlefield
                 # information from its new graveyard location. Other battlefield
                 # watchers already passed the standard zone match above; cards in
                 # hand/graveyard must not gain a phantom battlefield trigger.
                 if (card_id == event_origin_card_id and zone_name == 'graveyard'
                         and triggering_zone == 'battlefield'
                         and ("dies" in trigger_text
                              or "put into a graveyard from the battlefield" in trigger_text)):
                     can_trigger_from_current_zone = True
             elif event_type == "LEAVE_BATTLEFIELD": # Moving from Battlefield to somewhere else
                 if triggering_zone == 'battlefield' and ("leaves the battlefield" in trigger_text):
                     can_trigger_from_current_zone = True
             elif event_type == "ENTER_EXILE":
                  # A permanent's own exile trigger looks back to the object
                  # immediately before it left the battlefield. The move hook
                  # supplies that LKI after placing the physical card in exile.
                  if (card_id == event_origin_card_id
                          and zone_name == 'exile'
                          and triggering_zone == 'battlefield'
                          and context.get('from_zone') == 'battlefield'
                          and ("is put into exile" in trigger_text
                               or "is exiled" in trigger_text)):
                       can_trigger_from_current_zone = True
             elif event_type == "CAST_SPELL":
                  # The spell being cast triggers its own "when you cast this
                  # spell" ability from the stack.
                  if (zone_name == 'stack'
                          and context.get('cast_card_id') == card_id
                          and ("when you cast" in trigger_text
                               or "whenever you cast" in trigger_text)):
                       can_trigger_from_current_zone = True
                  # Cast triggers can be on the card itself (trigger zone = source zone) or on other cards
                  elif zone_name == triggering_zone and ("when you cast" in trigger_text or "whenever you cast" in trigger_text):
                       # Needs to check if the card being cast *is this card* OR if it triggers on *any* cast
                       cast_source_id = context.get('cast_card_id')
                       if cast_source_id == card_id: # It triggered itself being cast
                            can_trigger_from_current_zone = True
                       elif "a spell" in trigger_text or "noncreature spell" in trigger_text: # Triggers on other spells cast
                            # Condition needs controller check (e.g. "Whenever YOU cast")
                            # This ability source needs to be in its trigger zone (e.g., BF) when *another* spell is cast.
                            if zone_name == triggering_zone: # Check zone again for this case
                                can_trigger_from_current_zone = True
             # 3. "From Anywhere" Triggers
             elif "from anywhere" in trigger_text: # Handles zone-independent triggers
                  can_trigger_from_current_zone = True
             # 4. Other Specific Cases (Add as needed)
             # e.g., Madness discard trigger from hand moving to exile

             if not can_trigger_from_current_zone:
                 continue # Ability cannot trigger based on zone/event combination

             # --- Prepare Context for this Specific Trigger Check ---
             trigger_check_context = context.copy()
             # ``source_zone`` in an incoming event can describe the object
             # that caused the event (notably a permanent cast from exile).
             # Preserve that provenance before replacing it with the trigger
             # source's current zone below.
             if "source_zone" in context:
                  trigger_check_context.setdefault(
                      "event_source_zone", context.get("source_zone"))
             trigger_check_context['source_card_id'] = card_id
             trigger_check_context['source_card'] = source_card
             trigger_check_context['controller'] = controller # Player controlling the source
             trigger_check_context['source_zone'] = zone_name # Pass current zone
             trigger_check_context['source_zone_generation'] = int(getattr(
                 source_card, '_zone_change_generation', 0) or 0)

             # --- Event Matching & Condition Check ---
             try:
                 should_check_event = False
                 if event_type == "EXHAUST_ABILITY_ACTIVATED":
                     # ... (Exhaust specific check logic remains the same) ...
                     if "activate an exhaust ability" in trigger_text:
                        activator = context.get("activator")
                        if activator:
                            if "you activate" in trigger_text and activator == controller: should_check_event = True
                            elif "an opponent activates" in trigger_text and activator != controller: should_check_event = True
                            elif "you activate" not in trigger_text and "opponent activates" not in trigger_text: should_check_event = True # Any activation

                 else: # Standard event matching via TriggeredAbility.can_trigger
                     should_check_event = ability.can_trigger(event_type, trigger_check_context)

                 # --- Queue Trigger ---
                 if should_check_event:
                     if controller: # Ensure controller is valid
                         # Make a copy of the context at the time of triggering
                         context_for_queue = trigger_check_context.copy()
                         # Add additional info if needed by resolution
                         context_for_queue['original_zone'] = zone_name # Zone at time of trigger
                         self.active_triggers.append((ability, controller, context_for_queue)) # Store context too
                         queued_trigger_count += 1
                         # logging.debug(f"Queued trigger: '{ability.trigger_condition}' from {getattr(source_card,'name','Unknown')} ({card_id} in {zone_name}) for {controller.get('name','?')}")
                     else:
                         logging.warning(f"Trigger source {card_id} has no valid controller, cannot queue trigger.")
             except Exception as e:
                 logging.error(f"Error checking trigger condition for {ability.effect_text} from {getattr(source_card,'name','Unknown')}: {e}", exc_info=True)

        # if queued_trigger_count > 0: logging.info(f"Queued {queued_trigger_count} triggers for event {event_type}")
        return queued_trigger_count > 0

    def get_activated_abilities(self, card_id):
        """Get all activated abilities for a given card"""
        layer_system = getattr(self.game_state, 'layer_system', None)
        if (layer_system
                and self.game_state.get_card_controller(card_id)
                and layer_system.source_has_lost_all_abilities(card_id)):
            return []
        card_abilities = self.registered_abilities.get(card_id, [])
        return [ability for ability in card_abilities if isinstance(ability, ActivatedAbility)]

    def suppresses_target_protection(self, controller, target_id, protection):
        """Return whether a live source creates this targeting exception."""
        gs = self.game_state
        protection = str(protection).lower()
        if not gs or protection not in {"hexproof", "ward"} or not controller:
            return False
        target_controller, target_zone = gs.find_card_location(target_id)
        if (target_zone != "battlefield" or not target_controller
                or target_controller is controller or not gs._is_creature(target_id)):
            return False
        for source_id in controller.get("battlefield", []):
            if (getattr(gs, 'layer_system', None)
                    and gs.layer_system.source_has_lost_all_abilities(source_id)):
                continue
            for ability in self.registered_abilities.get(source_id, []):
                if (getattr(ability, "targeting_override", None) == protection
                        and getattr(ability, "scope", None) == "opponent_creatures"):
                    return True
        return False
    
    def crew_cost_payable(self, card_id, ability, controller):
        """Check untapped-creature total power against a crew requirement.

        Crew's real cost lives outside the parsed mana cost string (the
        ability is registered with cost '{0}'), so both the action mask and
        the execution handler must share this predicate or they diverge.
        """
        gs = self.game_state
        required_power = int(getattr(ability, 'crew_power', 0) or 0)
        if required_power <= 0:
            return True
        available_power = 0
        for cid in controller.get('battlefield', []):
            if cid == card_id or cid in controller.get(
                    'tapped_permanents', set()):
                continue
            card = gs._safe_get_card(cid)
            if 'creature' not in getattr(card, 'card_types', []):
                continue
            available_power += max(0, int(getattr(card, 'power', 0) or 0))
            if available_power >= required_power:
                return True
        return available_power >= required_power

    @staticmethod
    def get_ability_targeting_text(ability):
        """Return the targeting instruction used to announce an ability.

        Some keyword reminder text is stripped before activated abilities are
        registered. Earthbend still has a mandatory target by definition, so
        reconstruct that instruction for mask, choice, and resolution paths.
        """
        effect_text = getattr(
            ability, "effect", getattr(ability, "effect_text", "")) or ""
        lowered = effect_text.lower()
        if "earthbend" in lowered and "target" not in lowered:
            return "Target land you control"
        return effect_text

    def activated_ability_functions_from_zone(
            self, card_id, ability, controller):
        """Whether this activated ability functions from the source's zone."""
        expected_zone = str(
            getattr(ability, 'zone', 'battlefield') or 'battlefield').lower()
        holder, actual_zone = self.game_state.find_card_location(card_id)
        if holder is not controller:
            return False
        if expected_zone == 'face_down':
            card = self.game_state._safe_get_card(card_id)
            return bool(
                actual_zone == 'battlefield'
                and getattr(card, 'is_face_down', False))
        return actual_zone == expected_zone

    def can_activate_ability(self, card_id, ability_index, controller):
        """Check if a specific activated ability can be activated"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            if not self.activated_ability_functions_from_zone(
                    card_id, ability, controller):
                return False
            if not ability.can_pay_cost(self.game_state, controller):
                return False

            if (getattr(ability, 'keyword', '').lower() == 'crew'
                    and not self.crew_cost_payable(
                        card_id, ability, controller)):
                return False

            timing_text = (getattr(ability, "effect_text", "") or "").lower()
            if ("activate only during your turn" in timing_text
                    and self.game_state._get_active_player() is not controller):
                return False

            # CR 602.2b: a required target must exist before an ability can be
            # activated.  The execution handler already enforced this, but
            # the mask-side predicate checked costs only, exposing Floodpits
            # Drowner with no stun-counter creature to target.
            effect_text = self.get_ability_targeting_text(ability)
            if "target" in effect_text.lower():
                minimum, _ = self.game_state._target_bounds_from_text(
                    effect_text)
                if minimum > 0:
                    target_type = self.game_state._get_target_type_from_text(
                        effect_text)
                    valid_map = self.targeting_system.get_valid_targets(
                        card_id, controller, target_type,
                        effect_text=effect_text)
                    if not any(valid_map.values()):
                        return False
            return True
                
        return False

    def activate_ability(self, card_id, ability_index, controller,
                         sacrifice_choices=None):
        """Activate a specific activated ability"""
        activated_abilities = self.get_activated_abilities(card_id)
        if 0 <= ability_index < len(activated_abilities):
            ability = activated_abilities[ability_index]
            if not self.activated_ability_functions_from_zone(
                    card_id, ability, controller):
                return False
            if not ability.can_pay_cost(self.game_state, controller):
                return False
            if ability.pay_cost(
                    self.game_state, controller,
                    sacrifice_choices=sacrifice_choices):
                # Add to stack WITH the ability object: resolution needs it (an
                # empty context made every generic activation resolve to nothing).
                effect_text = getattr(ability, 'effect', '') or getattr(
                    ability, 'effect_text', '')
                targeting_text = self.get_ability_targeting_text(ability)
                stack_context = {
                    "ability": ability,
                    "effect_text": effect_text,
                }
                if "target" in targeting_text.lower():
                    stack_context["targeting_text"] = targeting_text
                    stack_context["target_choice_pending"] = True
                self.game_state.add_to_stack(
                    "ABILITY", card_id, controller, stack_context)
                if stack_context.get("target_choice_pending"):
                    self.game_state.start_pending_stack_target_choice()
                card = self.game_state._safe_get_card(card_id)
                logging.debug(
                    f"Activated ability {ability_index} for "
                    f"{getattr(card, 'name', card_id)}")
                return True
                        
        return False
    
    def process_triggered_abilities(self):
        """
        Move all pending triggered abilities from the queue to the stack in
        APNAP order (CR 603.3b): the active player puts their simultaneous
        triggers on the stack in an order of their choice, then the non-active
        player does the same.

        The ordering choice is surfaced to the AGENT as a real decision (an
        'order_triggers' choice_context in PHASE_CHOOSE, actions 353-362) when
        the agent's player has 2+ simultaneous triggers. The scripted/random
        opponent auto-orders in queue order for now -- routing the opponent's
        ordering through a policy is a self-play (Tier 3) follow-up. Zero or
        one triggers bypass the choice entirely, so training dynamics are
        untouched in the common case.
        """
        gs = self.game_state
        if not self.active_triggers:
            return

        active_player = gs._get_active_player()
        ap_triggers = []
        nap_triggers = []
        for entry in self.active_triggers:
            ability, controller = entry[0], entry[1]
            if controller == active_player:
                ap_triggers.append(entry)
            else:
                nap_triggers.append(entry)

        # Clear the queue *before* adding to stack to prevent potential
        # re-trigger loops within resolution.
        self.active_triggers = []

        # AP batch first; the NAP batch is stacked once AP's ordering is done.
        self._stack_trigger_batch_with_choice(ap_triggers, next_batch=nap_triggers)
        if not (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'order_triggers'):
            gs.start_pending_stack_target_choice()

    def _push_trigger_to_stack(self, ability, controller, context_at_trigger):
        """Put a single queued trigger onto the stack with its captured context."""
        gs = self.game_state
        if not ability or not hasattr(ability, 'card_id'):
            return False
        if context_at_trigger is None:
            context_at_trigger = {}
        if 'ability' not in context_at_trigger:
            context_at_trigger['ability'] = ability
        if 'source_id' not in context_at_trigger:
            context_at_trigger['source_id'] = ability.card_id
        if 'effect_text' not in context_at_trigger:
            context_at_trigger['effect_text'] = getattr(ability, 'effect_text', 'Unknown Effect')
        source_card = gs._safe_get_card(ability.card_id)
        source_name = str(getattr(source_card, "name", "") or "").casefold()
        modal_modes = None
        if source_name == "cosmogrand zenith":
            modal_modes, _, _ = self._parse_modal_text(
                getattr(ability, "effect", ""))
        pending_choice = getattr(gs, 'choice_context', None)
        if (modal_modes and "selected_trigger_mode" not in context_at_trigger
                and pending_choice
                and pending_choice.get('type') != 'order_triggers'):
            # Replacing a foreign pending choice would strand it: nothing
            # restores a clobbered context (July 13 reward-v2 deadlock).
            # An order_triggers context is exempt because both ordering
            # flows deliberately stage it, then re-link it as the child's
            # parent after this call returns. This branch should be
            # unreachable now that batch stacking pauses on a mode choice;
            # resolving unmoded is a loud, non-fatal degradation.
            logging.warning(
                "Modal trigger for %s stacked without a mode choice: a %s "
                "choice is already pending.", ability.card_id,
                pending_choice.get('type'))
            gs.add_to_stack(
                "TRIGGER", ability.card_id, controller, context_at_trigger)
            return True
        if modal_modes and "selected_trigger_mode" not in context_at_trigger:
            context_at_trigger["modal_trigger_modes"] = list(modal_modes)
            context_at_trigger["mode_choice_pending"] = True
            gs.add_to_stack(
                "TRIGGER", ability.card_id, controller, context_at_trigger)
            resume_phase = gs.phase
            if gs.phase not in [gs.PHASE_CHOOSE, gs.PHASE_TARGETING,
                                gs.PHASE_SACRIFICE]:
                gs.previous_priority_phase = gs.phase
            gs.phase = gs.PHASE_CHOOSE
            gs.choice_context = {
                "type": "trigger_mode", "player": controller,
                "source_id": ability.card_id, "options": list(modal_modes),
                "resume_phase": resume_phase,
            }
            gs.priority_player = controller
            gs.priority_pass_count = 0
            return True
        targeting_text = getattr(ability, 'effect', context_at_trigger['effect_text'])
        requires_target = bool(getattr(
            ability, 'requires_target', "target" in targeting_text.lower()))
        # Earthbend's target instruction is defined by the mechanic reminder
        # text and some card parsers intentionally strip that parenthetical
        # from the trigger effect. Targets still have to be chosen as the
        # trigger is put on the stack (CR 603.3d), not during resolution.
        if ("earthbend" in targeting_text.lower()
                and "target" not in targeting_text.lower()):
            targeting_text = "Target land you control"
            requires_target = True
        # A zone/event context can carry the targets of the spell or ability
        # that caused this trigger. They are event data, never the triggered
        # ability's own targets (CR 603.3d). This separation is required even
        # for a nontargeted trigger: otherwise Namor's cast trigger inherits a
        # Spell Snare/Bounce Off target, validates it as though Namor targeted
        # it, and mutates the physical spell's target payload.
        if context_at_trigger.get('event_type'):
            if context_at_trigger.get('targets'):
                context_at_trigger['event_targets'] = \
                    copy.deepcopy(context_at_trigger['targets'])
            for inherited_key in (
                    'targets', 'targets_by_slot', 'target_slots',
                    'instruction_target_slots', 'spree_target_slots',
                    'required_count', 'min_targets', 'max_targets',
                    'num_targets', 'targeting_text',
                    'target_choice_pending'):
                context_at_trigger.pop(inherited_key, None)
        if (requires_target and "target" in targeting_text.lower()
                and not context_at_trigger.get('targets')):
            context_at_trigger['targeting_text'] = targeting_text
            context_at_trigger['target_choice_pending'] = True
        gs.add_to_stack("TRIGGER", ability.card_id, controller, context_at_trigger)
        return True

    def choose_trigger_mode(self, mode_index):
        """Commit a modal trigger's mode before any player receives priority."""
        gs = self.game_state
        choice = getattr(gs, "choice_context", None)
        if not (choice and choice.get("type") == "trigger_mode"):
            return False
        options = choice.get("options", [])
        if not isinstance(mode_index, int) or not 0 <= mode_index < len(options):
            return False
        source_id = choice.get("source_id")
        for stack_index in range(len(gs.stack) - 1, -1, -1):
            item = gs.stack[stack_index]
            if not (isinstance(item, tuple) and len(item) >= 4
                    and item[0] == "TRIGGER" and item[1] == source_id
                    and item[3].get("mode_choice_pending")):
                continue
            context = dict(item[3])
            selected_text = options[mode_index]
            selected_ability = copy.copy(context.get("ability"))
            if not selected_ability:
                return False
            selected_ability.effect = selected_text.lower()
            selected_ability.effect_text = selected_text
            selected_ability.requires_target = "target" in selected_text.lower()
            context.update({
                "ability": selected_ability,
                "effect_text": selected_text,
                "selected_trigger_mode": mode_index,
                "mode_choice_pending": False,
            })
            gs.stack[stack_index] = item[:3] + (context,)
            parent_order = choice.get("parent_order_triggers")
            if parent_order:
                # Choosing a modal trigger's mode is nested inside CR 603.3b
                # ordering when that trigger was one of several simultaneous
                # triggers.  Restore the mutated parent instead of resuming
                # the raw CHOOSE phase with no context.
                gs.choice_context = parent_order
                gs.phase = gs.PHASE_CHOOSE
                gs.priority_player = parent_order.get("player", item[2])
                gs.priority_pass_count = 0
                self._continue_trigger_order(parent_order)
                return True

            resume_phase = gs._normalized_choice_resume_phase(
                choice.get("resume_phase", gs.PHASE_PRIORITY))
            gs.choice_context = None
            gs.phase = resume_phase
            if (resume_phase == gs.PHASE_PRIORITY
                    and gs.previous_priority_phase not in gs._TURN_PHASES
                    and gs._last_turn_phase in gs._TURN_PHASES):
                gs.previous_priority_phase = gs._last_turn_phase
            gs.priority_player = controller = item[2]
            gs.priority_pass_count = 0
            gs.start_pending_stack_target_choice()
            return True
        return False

    def _continue_trigger_order(self, context):
        """Continue or finish one preserved simultaneous-trigger ordering.

        A modal trigger can open its own policy choice while it is being put
        onto the stack.  The ordering context must survive that child choice,
        including when the modal trigger is the final auto-stacked entry.
        """
        gs = self.game_state
        pending = context.get("pending", [])

        # Once only one trigger remains its relative position is forced.  It
        # can itself open a nested mode choice, so detect the context change
        # before finalizing the parent batch.
        if len(pending) == 1:
            ability, controller, trigger_context = pending.pop(0)
            gs.choice_context = context
            self._push_trigger_to_stack(
                ability, controller, trigger_context)
            if gs.choice_context is not context:
                child = gs.choice_context
                if child and child.get("type") == "trigger_mode":
                    child["parent_order_triggers"] = context
                return True

        if pending:
            gs.choice_context = context
            gs.phase = gs.PHASE_CHOOSE
            gs.priority_player = context.get("player")
            gs.priority_pass_count = 0
            return True

        next_batch = context.get("next_batch") or []
        gs.choice_context = None
        # The stack is now non-empty: players receive priority (CR 117.3c).
        # Keep previous_priority_phase as the underlying turn-phase anchor.
        gs.phase = gs.PHASE_PRIORITY
        self._stack_trigger_batch_with_choice(next_batch)
        if not getattr(gs, "choice_context", None):
            gs.start_pending_stack_target_choice()
        return True

    def _stack_trigger_batch_with_choice(self, batch, next_batch=None):
        """Stack one player's simultaneous triggers (CR 603.3b).

        2+ triggers controlled by the agent's player open an 'order_triggers'
        choice; anything else (0/1 triggers, opponent's batch, or a choice
        context already active) is stacked immediately in queue order.
        """
        gs = self.game_state
        batch = [e for e in (batch or []) if e[0] is not None and hasattr(e[0], 'card_id')]
        controller = batch[0][1] if batch else None
        interactive = (len(batch) >= 2
                       and not getattr(gs, 'choice_context', None))

        if not interactive:
            added = 0
            for index, (ability, ctrl, trig_ctx) in enumerate(batch):
                choice_before = getattr(gs, 'choice_context', None)
                if self._push_trigger_to_stack(ability, ctrl, trig_ctx):
                    added += 1
                choice_after = getattr(gs, 'choice_context', None)
                if choice_after is not None and choice_after is not choice_before:
                    # Pushing this trigger paused the game for a policy
                    # choice (a modal trigger's mode). Stacking must stop
                    # here: continuing used to bury the pause under the
                    # rest of the batch and the NAP batch, and the deferred
                    # target opener then stamped PHASE_TARGETING over the
                    # CHOOSE pause, stranding the choice forever (July 13
                    # reward-v2 deadlock). Park the remainder on the same
                    # continuation the nested CR 603.3b ordering flow uses;
                    # choose_trigger_mode resumes it.
                    choice_after['parent_order_triggers'] = {
                        'type': 'order_triggers',
                        'player': ctrl,
                        'pending': list(batch[index + 1:]),
                        'next_batch': list(next_batch) if next_batch else [],
                        'source_id': getattr(ability, 'card_id', None),
                        'resolved': False,
                    }
                    if added:
                        logging.debug(
                            f"Added {added} triggered abilities to stack "
                            f"(auto order, paused for a mode choice).")
                    return
            if added:
                logging.debug(f"Added {added} triggered abilities to stack (auto order).")
            if next_batch:
                self._stack_trigger_batch_with_choice(next_batch)
            return

        # Enter the ordering choice (same pattern as scry/surveil).
        if gs.phase not in [gs.PHASE_CHOOSE, gs.PHASE_TARGETING,
                            gs.PHASE_SACRIFICE]:
            # PHASE_PRIORITY is itself a transient wrapper. Preserve an
            # existing real turn-phase anchor instead of replacing it with
            # another wrapper while opening the ordering choice.
            if (gs.phase != gs.PHASE_PRIORITY
                    or gs.previous_priority_phase not in gs._TURN_PHASES):
                gs.previous_priority_phase = gs.phase
        gs.phase = gs.PHASE_CHOOSE
        gs.choice_context = {
            'type': 'order_triggers',
            'player': controller,
            'pending': list(batch),
            'next_batch': list(next_batch) if next_batch else [],
            'source_id': batch[0][0].card_id,
            'resolved': False,
        }
        gs.priority_pass_count = 0
        gs.priority_player = controller
        logging.debug(
            f"CR 603.3b: {controller['name']} must order {len(batch)} simultaneous triggers.")

    def order_trigger_chosen(self, index):
        """Apply the agent's ordering choice: pending[index] goes on the stack
        next. When one trigger remains it is auto-stacked (no pointless extra
        decision); when the batch empties, the choice closes, the phase is
        restored, and any waiting NAP batch is stacked. Returns True if the
        choice was valid and applied."""
        gs = self.game_state
        ctx = getattr(gs, 'choice_context', None)
        if not ctx or ctx.get('type') != 'order_triggers':
            logging.warning("order_trigger_chosen called without an order_triggers context.")
            return False
        pending = ctx.get('pending', [])
        if not isinstance(index, int) or not (0 <= index < len(pending)):
            logging.warning(f"order_trigger_chosen: invalid index {index} for {len(pending)} pending.")
            return False

        ability, controller, trig_ctx = pending.pop(index)
        self._push_trigger_to_stack(ability, controller, trig_ctx)
        if gs.choice_context is not ctx:
            child = gs.choice_context
            if child and child.get("type") == "trigger_mode":
                child["parent_order_triggers"] = ctx
            return True
        return self._continue_trigger_order(ctx)

    def resolve_ability(self, ability_type, card_id, controller, context=None):
        """
        Resolve an ability from the stack. Now expects 'ability' object in context.
        Relies on the Ability object's resolve method or fallback generic resolution.
        Handles target validation using the main TargetingSystem instance.
        Includes special Offspring resolution.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        source_name = getattr(card, 'name', f"Card {card_id}") if card else f"Card {card_id}"

        if not context: context = {}

        ability = context.get("ability") # Get the specific Ability object instance
        effect_text_from_context = context.get("effect_text", "Unknown") # Fallback text
        targets_on_stack = context.get("targets", {}) # Targets chosen when added to stack

        logging.debug(f"Attempting to resolve {ability_type} '{effect_text_from_context}' from {source_name} with context keys {list(context.keys())}")

        resolution_success = False # Track overall success

        # --- OFFSPRING Trigger Special Resolution ---
        # *** Check if the ability instance has the flag set during parsing ***
        is_offspring_trigger = ability and hasattr(ability, '_is_offspring_etb_trigger') and ability._is_offspring_etb_trigger

        if is_offspring_trigger:
            logging.debug(f"Attempting Offspring trigger resolution for {source_name}")
            offspring_context_map = getattr(gs, '_offspring_cost_paid_context', {})
            # The entry transaction freezes payment onto this trigger. A
            # card_id can leave and re-enter before the trigger resolves, so a
            # live map keyed only by card_id is not a safe antecedent. Keep the
            # map fallback solely for older serialized stack contexts.
            cost_was_paid = bool(context.get(
                '_offspring_cost_was_paid',
                offspring_context_map.get(card_id, False)))

            if cost_was_paid:
                if card: # Need the source card that entered to copy
                    token_creator_player = controller
                    # Create and apply the copy effect
                    copy_effect = CreateTokenEffect(power=1, toughness=1, count=1,
                                                    is_copy=True, source_card_for_copy=card,
                                                    controller_gets=True)
                    if copy_effect.apply(gs, card_id, token_creator_player, None):
                         logging.info(f"Successfully resolved Offspring ETB for {card.name}")
                         resolution_success = True
                    else:
                         logging.error(f"Offspring token creation failed for {card.name}")
                         # Even on failure, clean up context to avoid re-triggering
                else:
                    logging.error(f"Offspring trigger cannot resolve: Source card {card_id} not found.")
            else:
                 logging.debug(f"Offspring trigger condition (cost paid) not met for {card_id} on resolution.")
                 resolution_success = True # Trigger resolves, but effect doesn't happen

            # --- Cleanup Offspring Context AFTER Resolution Attempt ---
            if card_id in offspring_context_map:
                del offspring_context_map[card_id]
                logging.debug(f"Cleaned up offspring context for {card_id}.")
            return resolution_success # Exit after handling Offspring

        # --- IMPENDING Trigger Special Resolution ---
        is_impending_remove_counter_trigger = ability and hasattr(ability, '_is_impending_remove_counter') and ability._is_impending_remove_counter
        is_impending_final_trigger = ability and hasattr(ability, '_is_impending_final_trigger') and ability._is_impending_final_trigger

        if is_impending_remove_counter_trigger:
            # ...(Impending logic remains the same)...
            logging.debug(f"Resolving Impending End Step trigger for {source_name}")
            if gs._is_impending_active(card_id):
                 if gs.add_counter(card_id, 'time', -1):
                      logging.debug(f"Removed time counter from Impending {source_name}.")
                      resolution_success = True
                 else:
                      logging.warning(f"Failed to remove time counter from Impending {source_name}.")
                      resolution_success = False
            else:
                 logging.debug(f"Impending trigger resolves, but {source_name} has no time counters or left BF.")
                 resolution_success = True
            return resolution_success

        if is_impending_final_trigger:
            logging.debug(f"Impending 'last counter removed' trigger resolving for {source_name} (Effect applied via counter removal).")
            return True # Trigger resolves, effect already happened.


        # --- Path 1: Use Ability Object (Standard Resolution - if not special trigger) ---
        elif ability and isinstance(ability, Ability):
            # ...(Rest of standard ability resolution remains the same)...
            # --- Target Validation ---
            ability_effect_text = getattr(ability, 'effect_text', effect_text_from_context)
            ability_targeting_text = (
                context.get("targeting_text")
                or getattr(ability, 'effect', ability_effect_text))
            valid_targets = True
            committed_target_count = len(gs._flatten_target_ids(
                targets_on_stack if isinstance(targets_on_stack, dict) else {}))
            requires_target = (
                getattr(ability, 'requires_target', False)
                or bool(context.get("targeting_text"))
                or committed_target_count > 0)
            # Trigger text can contain a later reflexive "When you do"
            # ability whose target does not belong to the parent trigger.
            # TriggeredAbility.requires_target has already scoped that marker
            # to the parent instruction, so do not re-infer a target here from
            # the full nested text and incorrectly fizzle the parent.
            parsed_min_targets, _ = (
                gs._target_bounds_from_text(ability_targeting_text)
                if requires_target and "target" in ability_targeting_text.lower()
                else (0, 0))

            if parsed_min_targets > committed_target_count:
                # A mandatory targeted object should have entered the target
                # choice flow before reaching resolution.  If a legacy/stale
                # stack object leaks through, it has no legal target to affect:
                # fizzle it cleanly rather than running TapEffect (or another
                # effect) with an empty target dictionary.
                logging.debug(
                    "Targeted %s from %s resolved without its mandatory "
                    "targets (%s/%s): %s. Fizzling.",
                    ability_type, source_name, committed_target_count,
                    parsed_min_targets, ability_targeting_text)
                return True

            if requires_target:
                if self.targeting_system:
                    validation_targets = targets_on_stack if isinstance(targets_on_stack, dict) else {}
                    validation_text = (ability_targeting_text
                                       if ability_targeting_text != "Unknown"
                                       else None)
                    valid_targets = self.targeting_system.validate_targets(card_id, validation_targets, controller, effect_text=validation_text)
                    if not valid_targets:
                         logging.info(f"Targets for '{ability_effect_text}' from {source_name} became invalid via TargetingSystem. Fizzling.")
                         return True # Fizzling counts as successful resolution
                else: # Fallback simple validation
                     is_valid_fallback = True
                     for cat, target_list in targets_on_stack.items():
                          if not target_list: continue
                          for target_id in target_list:
                               owner, zone = gs.find_card_location(target_id)
                               if not owner: is_valid_fallback = False; break
                          if not is_valid_fallback: break
                     if not is_valid_fallback:
                          logging.info(f"Targets for '{ability_effect_text}' from {source_name} became invalid (simple check). Fizzling.")
                          return True # Fizzling counts as successful resolution
                     valid_targets = True # Passed simple check

            # --- Call Appropriate Resolve Method ---
            try:
                resolve_method = None; resolve_kwargs = {'game_state': gs, 'controller': controller}; resolve_method_name = "None"
                # ...(logic to find resolve method)...
                if hasattr(ability, 'resolve_with_targets') and callable(ability.resolve_with_targets):
                    resolve_method_name = "resolve_with_targets"
                    resolve_method = ability.resolve_with_targets
                    resolve_kwargs['targets'] = targets_on_stack
                elif hasattr(ability, 'resolve') and callable(ability.resolve):
                    resolve_method_name = "resolve"; resolve_method = ability.resolve
                    import inspect; sig = inspect.signature(ability.resolve)
                    if 'targets' in sig.parameters: resolve_kwargs['targets'] = targets_on_stack
                elif hasattr(ability, '_resolve_ability_implementation') and callable(ability._resolve_ability_implementation):
                    resolve_method_name = "_resolve_ability_implementation"; resolve_method = ability._resolve_ability_implementation
                    import inspect; sig = inspect.signature(resolve_method)
                    if 'targets' in sig.parameters: resolve_kwargs['targets'] = targets_on_stack
                elif hasattr(ability, '_resolve_ability_effect') and callable(ability._resolve_ability_effect):
                     resolve_method_name = "_resolve_ability_effect"; resolve_method = ability._resolve_ability_effect
                     import inspect; sig = inspect.signature(resolve_method)
                     if 'targets' in sig.parameters: resolve_kwargs['targets'] = targets_on_stack

                if resolve_method:
                    import inspect
                    if 'context' in inspect.signature(resolve_method).parameters:
                        resolve_kwargs['context'] = context
                    logging.debug(f"Resolving via Ability object method: {resolve_method_name}")
                    resolve_result = resolve_method(**resolve_kwargs)
                    resolution_success = resolve_result is not False # Assume True unless explicitly False
                else: # Fallback: Use internal effect creation
                    logging.error(f"No standard resolve method found for ability object {type(ability).__name__} on {source_name}. Attempting effect factory.")
                    if hasattr(ability, '_create_ability_effects') and callable(ability._create_ability_effects):
                        effect_text_to_use = getattr(ability, 'effect', ability_effect_text)
                        effects = ability._create_ability_effects(effect_text_to_use, targets_on_stack)
                        if effects:
                            resolution_success, _ = gs._run_effect_sequence(
                                effects, card_id, controller, targets_on_stack,
                                context=context)
                        else:
                             logging.error(f"Internal effect creation failed for {type(ability).__name__} on {source_name}.")
                             resolution_success = False
                    else:
                         logging.error(f"Could not resolve {type(ability).__name__} on {source_name}: No resolve method or effect creator found.")
                         resolution_success = False

            except Exception as e:
                logging.error(f"Error resolving ability {ability_type} ({ability_effect_text}) for {source_name}: {str(e)}")
                import traceback; logging.error(traceback.format_exc())
                resolution_success = False

        # --- Path 2: Fallback using Effect Text from Context (If no Ability object) ---
        else:
             # ...(Fallback logic remains the same)...
             logging.warning(f"No valid 'ability' object found in context for resolving {ability_type} from {source_name}. Attempting fallback resolution from effect text: '{effect_text_from_context}'")
             if effect_text_from_context and effect_text_from_context != "Unknown":
                 logging.debug(f"Resolving via EffectFactory fallback using text: '{effect_text_from_context}'.")
                 # Validate targets using TargetingSystem
                 valid_targets_for_text = True
                 if self.targeting_system and "target" in effect_text_from_context.lower():
                     validation_targets = targets_on_stack if isinstance(targets_on_stack, dict) else {}
                     valid_targets_for_text = self.targeting_system.validate_targets(card_id, validation_targets, controller, effect_text=effect_text_from_context)

                 if not valid_targets_for_text:
                      logging.info(f"Targets for fallback effect '{effect_text_from_context}' from {source_name} became invalid. Fizzling.")
                      return True # Fizzling counts as successful resolution

                 # Use EffectFactory to create effects from text
                 effects = EffectFactory.create_effects(effect_text_from_context, targets=targets_on_stack, source_name=source_name)
                 if not effects:
                     logging.error(f"Cannot resolve {ability_type} from {source_name}: EffectFactory failed for text '{effect_text_from_context}'.")
                     return False # Failure: couldn't parse effects

                 target_arg = targets_on_stack if isinstance(targets_on_stack, dict) else None
                 resolution_success, _ = gs._run_effect_sequence(
                     effects, card_id, controller, target_arg, context=context)
                 logging.debug(f"Resolved fallback {ability_type} for {source_name} using effect text. Overall success: {resolution_success}")
             else:
                 logging.error(f"Cannot resolve {ability_type} from {source_name}: Missing ability object and effect text in context.")
                 resolution_success = False # Failure: no info to resolve

        pending_choice = getattr(gs, 'choice_context', None)
        if (pending_choice
                and pending_choice.get('type') in gs._ASYNC_EFFECT_CHOICE_TYPES
                and pending_choice.get('effect_continuation') is not None):
            event_context = dict(context)
            event_context.pop('ability', None)
            pending_choice['effect_continuation']['finalizer'] = {
                'kind': 'ability', 'source_id': card_id,
                'controller_id': gs._effect_controller_id(controller),
                'ability_type': ability_type, 'context': event_context,
            }
            return True

        # --- Post-Resolution Cleanup & SBA Check ---
        if resolution_success:
            # Trigger a generic RESOLVED event if needed
            gs.trigger_ability(card_id, f"{ability_type}_RESOLVED", context)
        gs.check_state_based_actions() # Check SBAs after any ability resolution attempt

        return resolution_success

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

    def get_ward_costs(self, card_id):
        """Return parsed ward costs for a card's active ward keyword abilities."""
        gs = self.game_state
        live_card = gs._safe_get_card(card_id)
        if not live_card:
            return []

        costs = []
        for ability in self.registered_abilities.get(card_id, []):
            if getattr(ability, 'keyword', None) == "ward":
                cost = getattr(ability, 'keyword_value', None)
                if cost and cost != "ward_generic":
                    costs.append(str(cost))

        if costs:
            return costs

        # Ward can be granted by an attached Aura such as a Royal Role. The
        # layer marks the target as having ward; preserve the Aura's printed
        # payment value here instead of degrading it to ward_generic.
        for player in (gs.p1, gs.p2):
            for attachment_id, target_id in player.get("attachments", {}).items():
                if target_id != card_id:
                    continue
                attachment = gs._safe_get_card(attachment_id)
                attachment_text = getattr(attachment, 'oracle_text', '') or ''
                for match in re.finditer(
                        r"ward\s*((?:\{[WUBRGCXSPMTQA0-9/\.]+\})+|\d+|pay \d+ life)",
                        attachment_text, re.IGNORECASE):
                    cost = match.group(1).strip()
                    costs.append(f"{{{cost}}}" if cost.isdigit() else cost)
        if costs:
            return costs

        text = getattr(live_card, 'oracle_text', '') or ''
        for match in re.finditer(
                r"ward\s*(?:—|-|–|:)?\s*((?:\{[WUBRGCXSPMTQA0-9\/\.]+\})+|\d+|pay \d+ life)",
                text,
                re.IGNORECASE):
            cost = match.group(1).strip()
            costs.append(f"{{{cost}}}" if cost.isdigit() else cost)
        return costs
            
    def handle_attack_triggers(self, attacker_id, extra_context=None):
        """Handle abilities triggering when a specific creature attacks."""
        gs = self.game_state
        card = gs._safe_get_card(attacker_id)
        if not card: return

        controller = gs.get_card_controller(attacker_id)
        if not controller: return

        # Prepare base context
        context = {"attacker_id": attacker_id, "controller": controller}
        if extra_context:
            context.update(extra_context)

        # One ATTACKS dispatch reaches every registered ability on both
        # players' permanents (check_abilities scans all zones), so attacker
        # self-triggers, same-controller watchers, and defender-side watchers
        # are all scoped inside TriggeredAbility.can_trigger. The old
        # per-permanent CREATURE_ATTACKS / CREATURE_ATTACKS_OPPONENT loops had
        # no can_trigger mapping and queued nothing.
        self.check_abilities(attacker_id, "ATTACKS", context)

        # Let GameState/Environment loop handle processing self.active_triggers
