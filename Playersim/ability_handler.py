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
        self.targeting_system = None # Initialize targeting system reference

        if game_state is not None:
            # Initialize TargetingSystem correctly
            try:
                 self.targeting_system = TargetingSystem(game_state)
                 # Link it back to the game_state if necessary
                 if not hasattr(game_state, 'targeting_system'):
                      game_state.targeting_system = self.targeting_system
                 logging.debug("TargetingSystem initialized and linked to GameState.")
            except Exception as e:
                 logging.error(f"Error initializing TargetingSystem: {e}")

            # Initialize abilities AFTER targeting system is ready
            self._initialize_abilities()


    def handle_class_level_up(self, class_idx):
        """
        Handle leveling up a Class card with proper trigger processing.

        Args:
            class_idx: Index of the Class card in the controller's battlefield.

        Returns:
            bool: True if the class was successfully leveled up.
        """
        gs = self.game_state
        active_player = gs._get_active_player() # Use GS helper method

        # Validate index
        if not (0 <= class_idx < len(active_player["battlefield"])):
            logging.warning(f"Invalid class index: {class_idx}")
            return False

        # Get the class card
        class_id = active_player["battlefield"][class_idx]
        class_card = gs._safe_get_card(class_id)

        # Verify it's a valid, levelable Class card
        if not class_card or not hasattr(class_card, 'is_class') or not class_card.is_class:
            logging.warning(f"Card {class_id} (index {class_idx}) is not a Class.")
            return False
        if not hasattr(class_card, 'can_level_up') or not class_card.can_level_up():
            logging.warning(f"Class {class_card.name} cannot level up further.")
            return False

        # Determine next level and cost
        next_level = getattr(class_card, 'current_level', 1) + 1 # Default current level is 1 if not set
        level_cost_str = class_card.get_level_cost(next_level) # Assumes this method exists

        if not level_cost_str:
            logging.warning(f"No cost found for level {next_level} of {class_card.name}.")
            return False

        # Check affordability using ManaSystem
        parsed_cost = None
        if hasattr(gs, 'mana_system') and gs.mana_system:
            parsed_cost = gs.mana_system.parse_mana_cost(level_cost_str)
            if not gs.mana_system.can_pay_mana_cost(active_player, parsed_cost):
                logging.debug(f"Cannot afford to level up {class_card.name} (Cost: {level_cost_str})")
                return False
        else:
            logging.warning("Mana system not found, cannot check level-up affordability.")
            return False # Cannot proceed without mana system

        # Pay the cost
        if not gs.mana_system.pay_mana_cost(active_player, parsed_cost):
            logging.warning(f"Failed to pay level-up cost for {class_card.name}")
            # Mana system should handle rollback internally if needed
            return False

        # Level up the class (Card object handles its state change)
        # previous_type_line should be tracked by the Card object during level_up
        previous_level = getattr(class_card, 'current_level', 1)
        success = class_card.level_up() # This should update current_level and potentially P/T, type

        if success:
            logging.info(f"Leveled up {class_card.name} from level {previous_level} to {next_level}")

            # Re-register abilities for the new level
            # Note: Unregistering old ones might be needed if they don't persist
            # self.unregister_card_abilities(class_id) # Optionally unregister first
            self._parse_and_register_abilities(class_id, class_card)

            # Trigger level-up event using unified check_abilities
            context = {
                "class_id": class_id,
                "previous_level": previous_level,
                "new_level": next_level,
                "controller": active_player
            }
            self.check_abilities(class_id, "CLASS_LEVEL_UP", context) # Event origin is the class itself

            # Check if it became a creature *at this level* (handled by Card.level_up or here)
            # Example: Card might gain 'creature' type now. Ensure Layer System updates.
            if hasattr(gs, 'layer_system'):
                gs.layer_system.apply_all_effects()

            gs.phase = gs.PHASE_PRIORITY # Reset priority

        return success
        

    def handle_unlock_door(self, room_idx):
        """
        Handle unlocking a door on a Room card with proper trigger processing.

        Args:
            room_idx: Index of the Room card in the controller's battlefield.

        Returns:
            bool: True if door was unlocked successfully.
        """
        gs = self.game_state
        active_player = gs._get_active_player() # Use GS helper method

        # Validate index
        if not (0 <= room_idx < len(active_player["battlefield"])):
            logging.warning(f"Invalid room index: {room_idx}")
            return False

        # Get the room card
        room_id = active_player["battlefield"][room_idx]
        room_card = gs._safe_get_card(room_id)

        # Verify it's a Room card
        if not room_card or not hasattr(room_card, 'is_room') or not room_card.is_room:
            logging.warning(f"Card {room_id} (index {room_idx}) is not a Room.")
            return False

        # Check which door to unlock (Assume door 2 for now, rule needs clarification)
        door_number = 2 # Assume we always try to unlock door 2 with this action
        door_attr = f"door{door_number}"
        door_data = getattr(room_card, door_attr, None)

        if not door_data or door_data.get('unlocked', False):
            logging.warning(f"Door {door_number} is already unlocked or does not exist for Room {room_card.name}")
            return False

        # Get door cost
        door_cost_str = door_data.get('mana_cost', '')
        if not door_cost_str:
            logging.warning(f"No mana cost defined for Door {door_number} of {room_card.name}.")
            return False # Cannot unlock without cost? Assume yes.

        # Check affordability using ManaSystem
        parsed_cost = None
        if hasattr(gs, 'mana_system') and gs.mana_system:
            parsed_cost = gs.mana_system.parse_mana_cost(door_cost_str)
            if not gs.mana_system.can_pay_mana_cost(active_player, parsed_cost):
                logging.debug(f"Cannot afford to unlock Door {door_number} for {room_card.name} (Cost: {door_cost_str})")
                return False
        else:
            logging.warning("Mana system not found, cannot check door unlock affordability.")
            return False # Cannot proceed without mana system

        # Pay the cost
        if not gs.mana_system.pay_mana_cost(active_player, parsed_cost):
            logging.warning(f"Failed to pay unlock cost for Door {door_number} of {room_card.name}")
            return False

        # Unlock the door (Update the card's state)
        door_data['unlocked'] = True
        logging.info(f"Unlocked Door {door_number} for Room {room_card.name}")

        # Check if fully unlocked
        fully_unlocked = all(getattr(room_card, f"door{n}", {}).get('unlocked', False) for n in [1, 2] if hasattr(room_card, f"door{n}"))

        # Prepare context for triggers
        context = {
            "door_number": door_number,
            "room_id": room_id,
            "controller": active_player,
            "cost_paid": door_cost_str,
            "fully_unlocked": fully_unlocked
        }

        # Trigger "DOOR_UNLOCKED" event for the room itself
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)
        # Trigger specific door event
        self.check_abilities(room_id, f"DOOR{door_number}_UNLOCKED", context)
        # Trigger if fully unlocked
        if fully_unlocked:
            self.check_abilities(room_id, "ROOM_FULLY_UNLOCKED", context)

        # Handle Chapter Advancement (if applicable on Room cards)
        if hasattr(room_card, 'advance_chapter') and callable(getattr(room_card, 'advance_chapter')):
             chapter_advanced = room_card.advance_chapter() # Card handles its chapter logic
             if chapter_advanced:
                 chapter_context = {
                     "room_id": room_id,
                     "controller": active_player,
                     "chapter": getattr(room_card, 'current_chapter', None)
                 }
                 logging.debug(f"Room {room_card.name} advanced to chapter {chapter_context['chapter']}")
                 # Apply chapter ability (assuming EffectFactory can handle text)
                 # Or call specific handler if complex
                 # self.apply_chapter_ability(...)
                 # Trigger room completion if final chapter reached
                 if hasattr(room_card, 'is_complete') and room_card.is_complete():
                      logging.debug(f"Room {room_card.name} completed.")
                      self.check_abilities(room_id, "ROOM_COMPLETED", chapter_context)

        # Re-register abilities if unlocking changes available effects
        self._parse_and_register_abilities(room_id, room_card)

        gs.phase = gs.PHASE_PRIORITY # Reset priority

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

    def handle_modal_ability(self, card_id, controller, mode_index):
        """
        Handle activation of a modal ability, putting the chosen mode onto the stack.
        Assumes the ability source (card_id) has modal properties or text parsable by _parse_modal_text.
        Assumes cost payment is handled externally or as part of activating the 'main' ability.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            logging.warning(f"Cannot handle modal ability: Card {card_id} not found or has no text.")
            return False

        # Get the activated ability object if possible (e.g., from stack context if resolving)
        # Or find the base ability that prompted the modal choice
        base_ability = None
        # Placeholder: Find the first activated ability with "choose"
        for ability in self.registered_abilities.get(card_id, []):
            if isinstance(ability, ActivatedAbility) and "choose" in getattr(ability, 'effect', '').lower():
                 base_ability = ability
                 break
        if not base_ability:
             logging.warning(f"Could not find base modal ability on {card.name}")
             return False

        # Parse the modes from the base ability's effect text or card's oracle text
        modes = self._parse_modal_text(getattr(base_ability, 'effect', card.oracle_text))

        if not modes or not (0 <= mode_index < len(modes)):
            logging.warning(f"Invalid mode index {mode_index} for {card.name} (Modes: {modes})")
            return False

        # Get the chosen mode text
        mode_effect_text = modes[mode_index]

        # Create effects for this mode
        # The EffectFactory needs to be robust enough to parse varied mode texts
        mode_effects = EffectFactory.create_effects(mode_effect_text)
        if not mode_effects:
             logging.warning(f"Could not create effects for modal choice '{mode_effect_text}'")
             return False

        # Determine targets specifically for THIS mode if needed
        targets = {}
        if "target" in mode_effect_text.lower() and self.targeting_system:
            targets = self.targeting_system.resolve_targeting(card_id, controller, mode_effect_text)
            # If targeting fails and was required, the action fails.
            if not targets and any(v for v in targets.values()): # Check if resolve_targeting returned empty dict meaning failure
                logging.debug(f"Targeting failed for chosen mode '{mode_effect_text}'.")
                # Rollback costs? This assumes cost was paid *before* mode choice.
                # If cost payment follows choice, this is okay.
                return False

        # Add the specific mode's resolution to the stack
        gs.add_to_stack("ABILITY", card_id, controller, {
            "ability_object": base_ability, # Link base ability for reference
            "is_modal_choice": True,
            "chosen_mode_index": mode_index,
            "effect_text": mode_effect_text, # Use mode's text
            "targets": targets,
            "effects": mode_effects # Store pre-created effects for resolution
        })

        logging.debug(f"Added chosen modal ability to stack: Mode {mode_index} ('{mode_effect_text}') for {card.name}")
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
        """
        if not card:
            logging.warning(f"Attempted to parse abilities for non-existent card {card_id}.")
            return

        # Clear existing registered abilities for this card_id to avoid duplication
        self.registered_abilities[card_id] = []
        abilities_list = self.registered_abilities[card_id] # Get reference to the list

        try:
            texts_to_parse = []
            keywords_to_parse = []

            # --- Get Text Source(s) ---
            if hasattr(card, 'is_class') and card.is_class:
                # For Class cards, parse abilities of the *current* level primarily
                current_level_data = card.get_current_class_data()
                if current_level_data:
                    texts_to_parse.extend(current_level_data.get('abilities', []))
                    keywords_to_parse.extend(current_level_data.get('keywords', []))
                # TODO: Consider inherited static abilities from previous levels? Rules check needed.
                # Add Level Up ability definition if applicable
                if hasattr(card, 'can_level_up') and card.can_level_up():
                     next_level = getattr(card, 'current_level', 1) + 1
                     cost_str = card.get_level_cost(next_level)
                     if cost_str:
                          # Create a specific text representation for the level-up action
                          texts_to_parse.append(f"{cost_str}: Level up to {next_level}.")
                          # Note: Actual leveling is handled by GameState/ActionHandler

            elif hasattr(card, 'faces') and card.faces and hasattr(card, 'current_face'):
                 # MDFC or Transforming DFC - use current face
                 current_face_data = card.faces[card.current_face]
                 texts_to_parse.append(current_face_data.get('oracle_text', ''))
                 keywords_to_parse.extend(current_face_data.get('keywords', [])) # Assumes keywords list on face data
            else:
                 # Standard card or other types
                 texts_to_parse.append(getattr(card, 'oracle_text', ''))
                 # Get keywords from the card's keyword list/array if it exists
                 if hasattr(card, 'keywords') and isinstance(card.keywords, list):
                     # Map binary array back to names? Or assume keywords_to_parse holds names?
                     # Assume Card.keywords holds names directly or requires mapping
                     # If it's the binary array:
                     # keywords_to_parse.extend([kw_name for i, kw_name in enumerate(Card.ALL_KEYWORDS) if i < len(card.keywords) and card.keywords[i] == 1])
                     # If it holds names already:
                      keywords_to_parse.extend(getattr(card,'keywords',[]))


            # --- Process Keywords ---
            unique_keywords = set(kw.lower() for kw in keywords_to_parse if kw) # Normalize and make unique
            for keyword_text in unique_keywords:
                # Use helper to create appropriate ability object (Static, Triggered, Activated)
                self._create_keyword_ability(card_id, card, keyword_text.split()[0], abilities_list, full_keyword_text=keyword_text)

            # --- Process Oracle Text ---
            for text_block in texts_to_parse:
                if not text_block: continue
                # Split text into potential ability clauses (e.g., by newline, sentence)
                # More robust splitting: Split by newline, then by sentence-ending punctuation if feasible.
                potential_abilities = []
                lines = text_block.strip().split('\n')
                for line in lines:
                     line = line.strip()
                     if not line: continue
                     # Further split by sentence enders followed by space/capital? More complex.
                     # Simple split by '.' or ';'? Risky. Let's try line by line first.
                     # Split by '•' only if it doesn't look like part of a cost
                     if '•' in line and ':' not in line.split('•')[0]:
                          potential_abilities.extend(p.strip() for p in re.split(r'\s*[•●]\s*', line) if p.strip())
                     elif line: # Treat the whole line as a potential ability
                         potential_abilities.append(line)

                for ability_text in potential_abilities:
                     self._parse_ability_text(card_id, card, ability_text, abilities_list)

            # Log final count for this card
            # logging.debug(f"Parsed {len(abilities_list)} abilities for {card.name} ({card_id})")

            # Immediately apply newly registered Static Abilities
            # LayerSystem application happens centrally after all effects are registered for the turn/event.
            # But the StaticAbility object needs to register its effect *intent* with the LayerSystem now.
            for ability in abilities_list:
                if isinstance(ability, StaticAbility):
                    ability.apply(self.game_state) # apply should now just call LayerSystem.register_effect


        except Exception as e:
            logging.error(f"Error parsing abilities for card {card_id} ({getattr(card, 'name', 'Unknown')}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            # Ensure list exists even on error
            if card_id not in self.registered_abilities: self.registered_abilities[card_id] = []

    def _create_keyword_ability(self, card_id, card, keyword_name, abilities_list, full_keyword_text=None):
        """
        Creates the appropriate Ability object for a given keyword.
        Distinguishes static, triggered, activated, and rule-modifying keywords.
        """
        keyword_lower = keyword_name.lower()
        full_text = (full_keyword_text or keyword_name).lower() # Use full text if provided

        # Avoid duplicates based on keyword name for this card
        if any(getattr(a, 'keyword', None) == keyword_lower for a in abilities_list):
            # Allow multiple instances of some keywords like Protection? Needs rules check.
            # For now, prevent exact duplicates.
            return

        # Helper function to parse numeric values from keyword text (e.g., Ward {2}, Annihilator 1)
        def parse_value(text, keyword):
            # More robust: handles Ward {cost}, Annihilator N, Modular N etc.
            keyword_pattern = re.escape(keyword)
            match_cost = re.search(f"{keyword_pattern}\\s*(\\{{.*?\\}})", text)
            if match_cost: # Ward {cost} case
                # Attempt to parse the cost symbols/numbers within {} ? Or just treat as 'value exists'?
                # Simple: return 1 indicating presence. Actual cost checked elsewhere.
                # Advanced: return cost string?
                return match_cost.group(1) # Return the cost string itself for Ward
            match_num = re.search(f"{keyword_pattern}\\s+(\\d+)", text)
            if match_num: return int(match_num.group(1))
            # Handle text numbers? ("Ward two") - Future enhancement
            return 1 # Default value if not specified or complex (like Equip)

        # Helper function to parse mana costs associated with keywords (Equip {1}, Cycling {W})
        def parse_cost(text, keyword):
            # Enhanced pattern to capture various cost formats
            keyword_pattern = re.escape(keyword)
            patterns = [
                # Cost like {.*?} after keyword and optional separator (—, -, :)
                f"{keyword_pattern}\\s*(?:—|-|:)?\\s*(\\{{[WUBRGCXSPMTQA0-9\\/]+\\}})",
                # Cost like {N} or N after keyword and optional separator
                f"{keyword_pattern}\\s*(?:—|-|:)?\\s*(\\{{\\d+\\}}|\\d+)",
                # Specific case like Ward cost {2} or Ward {B}
                "ward\\s*(\\{{.*?\\}})"
            ]
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    cost_part = match.group(1).strip()
                    # Normalize numbers like '1' to '{1}'
                    if cost_part.isdigit(): return f"{{{cost_part}}}"
                    # Return if already in {X} format
                    if cost_part.startswith('{') and cost_part.endswith('}'): return cost_part
            # Default to free if no cost found (or keyword doesn't have mana cost like Ward Pay Life)
            return "{0}"

        # --- Static Grant Keywords (Layer 6) -> StaticAbility ---
        # Keywords granting inherent properties, handled by Layer 6
        static_keywords = [
            "flying", "first strike", "double strike", "trample", "vigilance", "haste",
            "menace", "reach", "defender", "indestructible", "hexproof", "shroud",
            "unblockable", "fear", "intimidate", "shadow", "horsemanship",
            "banding", "phasing", "lifelink", "deathtouch", "changeling", "devoid",
            "infect", "wither", # These have static components affecting damage rules
            "protection", # Specifics parsed from full text
            "ward", # Specifics parsed from full text
            # Landwalk needs parsing for specific type
            "islandwalk", "swampwalk", "mountainwalk", "forestwalk", "plainswalk", "desertwalk", # etc.
        ]
        # Check for exact matches or variations like "landwalk"
        is_static_grant = keyword_lower in static_keywords or "walk" in keyword_lower

        if is_static_grant:
            # Effect text should capture parameters (e.g., "protection from red")
            ability_effect_text = f"This permanent has {full_text}."
            # Extract specifics for Protection or Ward if needed for layer system registration
            value = None
            if keyword_lower == "protection":
                match = re.search(r"protection from (.*)", full_text)
                if match: value = match.group(1).strip()
            elif keyword_lower == "ward":
                value = parse_value(full_text, keyword_lower) # Get cost or marker

            ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
            setattr(ability, 'keyword', keyword_lower) # Mark the keyword
            setattr(ability, 'keyword_value', value) # Store extracted value if any
            abilities_list.append(ability)
            # StaticAbility.apply will register this with the LayerSystem
            ability.apply(self.game_state)
            return

        # --- Triggered Keywords -> TriggeredAbility ---
        # Keywords that set up triggered abilities based on game events
        triggered_map = {
            # keyword: (trigger_phrase, simple_effect_description)
            "prowess": ("whenever you cast a noncreature spell", "this creature gets +1/+1 until end of turn."),
            "cascade": ("when you cast this spell", "Exile cards... You may cast..."), # Effect needs special handler
            "storm": ("when you cast this spell", "Copy this spell for each spell cast before it this turn."), # Effect needs special handler
            "riot": ("this permanent enters the battlefield", "Choose haste or a +1/+1 counter."), # Modal effect
            "enrage": ("whenever this creature is dealt damage", "{effect_from_oracle}"), # Needs specific effect text
            "afflict": ("whenever this creature becomes blocked", "defending player loses N life"),
            "mentor": ("whenever this creature attacks", "put a +1/+1 counter on target attacking creature with lesser power."),
            "afterlife": ("when this permanent dies", "create N 1/1 white and black Spirit creature tokens with flying."),
            "annihilator": ("whenever this creature attacks", "defending player sacrifices N permanents."),
            "bloodthirst": ("this creature enters the battlefield", "if an opponent was dealt damage this turn, it enters with N +1/+1 counters."),
            "bushido": ("whenever this creature blocks or becomes blocked", "it gets +N/+N until end of turn."),
            "evolve": ("whenever a creature enters the battlefield under your control", "if that creature has greater power or toughness, put a +1/+1 counter on this creature."),
            "fabricate": ("when this permanent enters the battlefield", "put N +1/+1 counters on it or create N 1/1 Servo tokens."), # Modal effect
            "flanking": ("whenever a creature without flanking blocks this creature", "the blocking creature gets -1/-1 until end of turn."),
            "gravestorm": ("when you cast this spell", "Copy this spell for each permanent put into a graveyard this turn."), # Needs handler
            "haunt": ("when this permanent dies", "exile it haunting target creature."), # Needs complex handler
            "ingest": ("whenever this creature deals combat damage to a player", "that player exiles the top card of their library."),
            "modular": ("this enters with N +1/+1 counters. when it dies", "move its +1/+1 counters to target artifact creature."), # Two triggers
            "persist": ("when this permanent dies", "if it had no -1/-1 counters, return it with a -1/-1 counter."),
            "poisonous": ("whenever this creature deals combat damage to a player", "that player gets N poison counters."),
            "rampage": ("whenever this creature becomes blocked", "it gets +N/+N for each creature blocking it beyond the first."),
            "renown": ("whenever this creature deals combat damage to a player", "if it isn't renowned, put N +1/+1 counters on it and it becomes renowned."),
            "ripple": ("when you cast this spell", "Reveal top N cards... You may cast copies..."), # Needs handler
            "soulshift": ("when this permanent dies", "return target Spirit card with mana value N or less from graveyard to hand."),
            "sunburst": ("this permanent enters the battlefield", "with a charge counter for each color of mana spent to cast it."), # Needs mana spent tracking
            "training": ("whenever this creature attacks with another creature with greater power", "put a +1/+1 counter on this creature."),
            "undying": ("when this permanent dies", "if it had no +1/+1 counters, return it with a +1/+1 counter."),
            "vanishing": ("this permanent enters the battlefield with N time counters.", "At upkeep, remove a counter. When last is removed, sacrifice it."), # Two triggers
            "fading": ("this permanent enters the battlefield with N fade counters.", "At upkeep, remove a counter. If you can't, sacrifice it."), # Two triggers
            "decayed": ("this creature can't block.", "When it attacks, sacrifice it at end of combat."), # One static, one triggered
            "battle cry": ("whenever this creature attacks", "each other attacking creature gets +1/+0 until end of turn."),
            "explore": ("when this creature enters the battlefield", "reveal top card..."), # Needs explore handler
            "extort": ("whenever you cast a spell", "you may pay {W/B}. If you do, each opponent loses 1 life and you gain that much life."),
            "melee": ("whenever this creature attacks", "it gets +1/+1 until end of turn for each opponent attacked."),
            # Add more triggered keywords here...
        }
        if keyword_lower in triggered_map:
            trigger_cond, effect_desc = triggered_map[keyword_lower]
            # Handle N values
            val = parse_value(full_text, keyword_lower)
            # Basic substitution for N, but complex effects need better handling
            effect = effect_desc.replace(" N ", f" {val} ").replace("{effect_from_oracle}", f"its {keyword_lower} effect")

            # Special handling for keywords with multiple parts (e.g., Modular, Fading)
            # TODO: Split complex keyword definitions into multiple ability objects if needed.
            # For now, create one trigger based on the primary event.
            if keyword_lower == "decayed": # Special case: has static + triggered
                static_part = StaticAbility(card_id, "This creature can't block.", "This creature can't block.")
                setattr(static_part, 'keyword', 'cant_block_static') # Internal keyword
                abilities_list.append(static_part)
                static_part.apply(self.game_state)
                # Create trigger for the second part
                trigger_cond = "when this creature attacks"
                effect = "sacrifice it at end of combat."

            ability = TriggeredAbility(card_id, trigger_cond, effect, effect_text=full_text)
            setattr(ability, 'keyword', keyword_lower)
            setattr(ability, 'keyword_value', val) # Store parsed value
            abilities_list.append(ability)
            return

        # --- Activated Keywords -> ActivatedAbility ---
        # Keywords representing activated abilities
        activated_map = {
            # keyword: simple_effect_description (cost is parsed separately)
            "cycling": ("discard this card: draw a card."), # Cost is part of keyword text
            "equip": ("attach to target creature you control. activate only as a sorcery."),
            "fortify": ("attach to target land you control. activate only as a sorcery."),
            "unearth": ("return this card from graveyard to battlefield with haste. exile it... activate only as a sorcery."),
            "flashback": ("cast from graveyard, then exile."), # Cost is part of keyword text
            "retrace": ("cast from graveyard by discarding a land card."), # Needs cost parsing & extra cost
            "scavenge": ("exile this card from graveyard: put N +1/+1 counters on target creature. activate only as a sorcery."),
            "transfigure": ("sacrifice this creature: search library for creature with same mana value... activate only as a sorcery."),
            "transmute": ("discard this card: search library for card with same mana value... activate only as a sorcery."),
            "auraswap": ("exchange this aura with an aura card in your hand."),
            "outlast": ("{1}, {T}: put a +1/+1 counter on this creature. activate only as a sorcery."), # Cost often varies slightly
            "reinforce": ("discard this card: put N +1/+1 counters on target creature."), # Needs cost parsing & discard
            "reconfigure": ("attach to target creature you control or unattach from a creature. activate only as a sorcery."),
            "adapt": ("if this creature has no +1/+1 counters on it, put N +1/+1 counters on it."), # Needs cost parsing
            "level up": ("put a level counter on this creature. level up only as a sorcery."), # Needs cost parsing & class logic
            "monstrosity": ("put N +1/+1 counters on this creature and it becomes monstrous."), # Needs cost parsing
            "crew": ("tap N power of untapped creatures you control: this vehicle becomes an artifact creature until end of turn."), # N is parsed
            "channel": ("discard this card: {effect}."), # Effect varies, parsed from full text
            "forecast": ("reveal this card from hand during upkeep: {effect}."), # Effect varies, parsed from full text
            # Add more activated keywords here...
        }
        if keyword_lower in activated_map:
            cost_str = parse_cost(full_text, keyword_lower)
            # For cycling, flashback, reinforce, channel - cost might be part of the text, not a standard equip/etc cost
            if keyword_lower in ["cycling", "flashback", "reinforce", "channel", "retrace", "transmute", "adapt", "monstrosity", "level up"]:
                # More specific cost parsing for these keywords
                keyword_pattern = re.escape(keyword_lower)
                cost_match = re.search(f"{keyword_pattern}\\s*(?:—|-|:)?\\s*(\\{{.*?\\}})", full_text)
                if cost_match: cost_str = cost_match.group(1)
                # Add checks for non-mana costs like discard for Retrace/Transmute if needed

            # Ensure a cost was found (even if {0} for free)
            if cost_str is not None: # Check for None explicitly
                effect_desc = activated_map[keyword_lower]
                # Parse value (N) if needed
                val = parse_value(full_text, keyword_lower)
                # Handle crew value differently
                if keyword_lower == "crew": val = parse_value(full_text, keyword_lower) # Get crew number

                effect = effect_desc.replace(" N ", f" {val} ").replace("{effect}.", "perform its specific effect.") # Substitute N, add placeholder

                # Create ActivatedAbility (or specific subclass if warranted, e.g., CyclingAbility)
                ability = ActivatedAbility(card_id, cost_str, effect, effect_text=full_text)
                setattr(ability, 'keyword', keyword_lower)
                setattr(ability, 'keyword_value', val) # Store parsed value (N or crew number)
                abilities_list.append(ability)
                return

        # --- Rule Modifying Keywords -> StaticAbility (but with nuance) ---
        # These modify rules for casting, targeting, or costs.
        # Ideally handled by specific game systems (casting, mana payment).
        # Registering as StaticAbility is a simplification for now.
        rule_keywords = [
            "affinity", "convoke", "delve", "improvise", # Cost reduction mechanics
            "bestow", "buyback", "entwine", "escape", "kicker", # Alternative/additional costs
            "madness", "overload", "splice", "surge", "spree", # Casting modifications
            "split second", "suspend", # Timing modifications
            "companion", "demonstrate", # Companion/Multiplayer mechanics
            "embalm", "eternalize", "jump-start", "rebound", # Alternative casting zones/timing
        ]
        if keyword_lower in rule_keywords:
            # NOTE: This is a placeholder. These keywords don't grant simple static abilities.
            # They require interaction with the game's casting/payment logic.
            ability_effect_text = f"This card has the rule-modifying keyword: {full_text}."
            # Create a StaticAbility object to flag the keyword's presence.
            # The *actual effect* needs to be implemented in the relevant game systems.
            ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
            setattr(ability, 'keyword', keyword_lower) # Mark the keyword type
            # Parse cost/value if applicable (e.g., Kicker {cost}, Suspend N {cost})
            cost_str = parse_cost(full_text, keyword_lower)
            val = parse_value(full_text, keyword_lower)
            setattr(ability, 'keyword_cost', cost_str)
            setattr(ability, 'keyword_value', val)
            abilities_list.append(ability)
            # We still 'apply' it so the layer system knows about the card having this property textually.
            ability.apply(self.game_state)
            logging.warning(f"Registered rule-modifying keyword '{keyword_lower}' as StaticAbility (Placeholder). Effect requires specific implementation in game rules.")
            return

        # --- Fallback ---
        # If keyword wasn't specifically handled, treat as a generic static grant.
        # This is often incorrect for complex unmapped keywords.
        logging.warning(f"Keyword '{keyword_lower}' (from '{full_text}') not explicitly mapped or parsed. Treating as generic StaticAbility.")
        ability_effect_text = f"This permanent has {full_text}."
        ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
        setattr(ability, 'keyword', keyword_lower)
        abilities_list.append(ability)
        ability.apply(self.game_state)
        
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
        if ability_text.lower() not in [kw.lower() for kw in Card.ALL_KEYWORDS]:
             try:
                 # Effect is the whole text for static abilities
                 ability = StaticAbility(card_id=card_id, effect=ability_text, effect_text=ability_text)
                 setattr(ability, 'source_card', card)
                 abilities_list.append(ability)
                 logging.debug(f"Registered StaticAbility for {card.name}: {ability.effect_text}")
                 # Register static effect intent immediately
                 ability.apply(self.game_state)
                 return # Parsed as Static
             except Exception as e:
                 logging.error(f"Error parsing as StaticAbility: {e}")

        # If none of the above worked, log it
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
        Adds valid triggers to the self.active_triggers queue.
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

        # Check abilities on permanents in relevant zones (mainly battlefield)
        cards_to_check_ids = set()
        for p in [gs.p1, gs.p2]:
            cards_to_check_ids.update(p.get("battlefield", []))
            # Add other zones if triggers can happen there (e.g., graveyard for Bloodghast)
            # cards_to_check_ids.update(p.get("graveyard", []))

        for ability_source_id in cards_to_check_ids:
            source_card = gs._safe_get_card(ability_source_id)
            if not source_card: continue

            registered_abilities = self.registered_abilities.get(ability_source_id, [])
            for ability in registered_abilities:
                if isinstance(ability, TriggeredAbility):
                    # Prepare context specific to this ability check
                    trigger_check_context = context.copy()
                    trigger_check_context['source_card_id'] = ability_source_id
                    trigger_check_context['source_card'] = source_card

                    try:
                        if ability.can_trigger(event_type, trigger_check_context):
                            ability_controller = gs.get_card_controller(ability_source_id)
                            if ability_controller:
                                self.active_triggers.append((ability, ability_controller))
                                logging.debug(f"Queued trigger: '{ability.trigger_condition}' from {source_card.name} ({ability_source_id}) due to {event_type}")
                            else:
                                # Source might have left the battlefield between event and check
                                # logging.debug(f"Trigger source {ability_source_id} has no controller, cannot queue trigger.")
                                pass
                    except Exception as e:
                        logging.error(f"Error checking trigger condition for {ability.effect_text} from {source_card.name}: {e}")

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
        Handles target validation.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        source_name = getattr(card, 'name', f"Card {card_id}") if card else f"Card {card_id}"

        if not context: context = {}

        ability = context.get("ability") # Get the specific Ability object instance
        effect_text_from_context = context.get("effect_text", "Unknown") # Fallback text
        targets_on_stack = context.get("targets", {}) # Targets chosen when added to stack

        # --- Path 1: Use Ability Object ---
        if ability and isinstance(ability, Ability):
            ability_effect_text = getattr(ability, 'effect_text', effect_text_from_context) # Prefer ability's text

            # Validate Targets using Targeting System just before resolution
            if self.targeting_system:
                if not self.targeting_system.validate_targets(card_id, targets_on_stack, controller):
                    logging.info(f"Targets for '{ability_effect_text}' from {source_name} became invalid. Fizzling.")
                    return # Fizzle if targets invalid

            # Resolve using the ability's method
            try:
                # Check if ability has a specific resolve method
                # Pass targets if the method accepts them, otherwise use standard resolve
                resolve_method = None
                if hasattr(ability, 'resolve_with_targets'): resolve_method = ability.resolve_with_targets
                elif hasattr(ability, 'resolve'): resolve_method = ability.resolve

                if resolve_method:
                    # Inspect if method expects targets
                    import inspect
                    sig = inspect.signature(resolve_method)
                    if 'targets' in sig.parameters:
                         resolve_method(gs, controller, targets=targets_on_stack)
                    else:
                         resolve_method(gs, controller) # Standard resolve without targets arg
                    logging.debug(f"Resolved {type(ability).__name__} ability for {source_name}: {ability_effect_text}")
                else:
                    # Fallback to generic internal resolution if no specific method found
                    logging.warning(f"Ability object for {source_name} lacks specific resolve method. Using generic effect application.")
                    ability._resolve_ability_effect(gs, controller, targets_on_stack) # Internal fallback

            except Exception as e:
                logging.error(f"Error resolving ability {ability_type} ({ability_effect_text}) for {source_name}: {str(e)}")
                import traceback; logging.error(traceback.format_exc())

        # --- Path 2: Fallback using Effect Text from Context ---
        else:
             logging.warning(f"No valid 'ability' object found in context for resolving {ability_type} from {source_name}. Attempting fallback resolution from effect text: '{effect_text_from_context}'")
             if effect_text_from_context and effect_text_from_context != "Unknown":
                 # Use EffectFactory and generic resolution
                 effects = EffectFactory.create_effects(effect_text_from_context, targets=targets_on_stack)
                 if not effects:
                     logging.error(f"Cannot resolve {ability_type} from {source_name}: EffectFactory failed for text '{effect_text_from_context}'.")
                     return

                 for effect_obj in effects:
                      # Re-validate targets just before applying this specific effect?
                      if effect_obj.requires_target and self.targeting_system:
                           if not self.targeting_system.validate_targets(card_id, targets_on_stack, controller):
                                logging.debug(f"Targets invalid for fallback effect '{effect_obj.effect_text}'. Skipping.")
                                continue # Skip this specific effect

                      # Apply the effect
                      try:
                           effect_obj.apply(gs, card_id, controller, targets_on_stack)
                      except NotImplementedError:
                           logging.error(f"Fallback effect application not implemented for: {effect_obj.effect_text}")
                      except Exception as e:
                           logging.error(f"Error applying fallback effect '{effect_obj.effect_text}': {e}")
                           import traceback; logging.error(traceback.format_exc())
                 logging.debug(f"Resolved fallback {ability_type} for {source_name} using effect text.")
             else:
                 logging.error(f"Cannot resolve {ability_type} from {source_name}: Missing ability object and effect text in context.")
                  
    def _check_keyword_internal(self, card, keyword):
        """
        Centralized keyword check using LayerSystem results (keywords array).
        """
        if not card or not isinstance(card, Card): return False
        keyword_lower = keyword.lower()
        gs = self.game_state

        # PRIMARY CHECK: Use the calculated 'keywords' array on the live card object.
        # This array reflects the final state after layer application.
        if hasattr(card, 'keywords') and isinstance(card.keywords, (list, np.ndarray)):
            try:
                 # Ensure Card.ALL_KEYWORDS is populated
                if not Card.ALL_KEYWORDS:
                     logging.error("Card.ALL_KEYWORDS is empty, cannot perform keyword check.")
                     return False # Fallback or error

                idx = Card.ALL_KEYWORDS.index(keyword_lower)
                if idx < len(card.keywords):
                    # Check if the keyword flag is set (assuming 1 means present)
                    has_kw = bool(card.keywords[idx])
                    # logging.debug(f"Keyword Check (Array): {keyword_lower}={has_kw} for {card.name}")
                    return has_kw
            except ValueError:
                # Keyword not in the canonical list (might be complex like "Protection from Red")
                # Fallback to checking granted/removed sets if available
                if keyword_lower in getattr(card, '_granted_abilities', set()) and keyword_lower not in getattr(card, '_removed_abilities', set()):
                    # logging.debug(f"Keyword Check (Sets): {keyword_lower}=True for {card.name}")
                    return True
            except IndexError:
                 logging.warning(f"Keyword index out of bounds for {keyword_lower} on {card.name}. Array length: {len(card.keywords)}")


        # Fallback: Check inherent abilities from oracle text (less reliable for game state)
        if hasattr(card, 'oracle_text'):
            # Use the approximate checker function
            inherent_set = self._approximate_keywords_set(getattr(card, 'oracle_text', ''))
            has_inherent = keyword_lower in inherent_set
            # logging.debug(f"Keyword Check (Text Fallback): {keyword_lower}={has_inherent} for {card.name}")
            return has_inherent

        # logging.debug(f"Keyword Check (Final Fallback): {keyword_lower}=False for {card.name}")
        return False
    
    def check_keyword(self, card_id, keyword):
         """Public interface for checking keywords using the internal logic."""
         card = self.game_state._safe_get_card(card_id)
         return self._check_keyword_internal(card, keyword)
                                             

    def get_protection_details(self, card_id):
         """Gets detailed protection strings (e.g., "red", "creatures") for a card."""
         card = self.game_state._safe_get_card(card_id)
         if not card: return None

         protections = set()
         # Check calculated keywords array for "protection" flag first
         has_general_protection = self._check_keyword_internal(card, "protection")

         if has_general_protection and hasattr(card, 'oracle_text'):
              text = card.oracle_text.lower()
              # Extract specifics like "protection from red", "protection from creatures"
              matches = re.findall(r"protection from ([\w\s]+)(?:\s*where|\.|$|,|;)", text)
              for match in matches:
                  protections.add(match.strip())

         # Check granted abilities
         granted = getattr(card, '_granted_abilities', set())
         for granted_ability in granted:
             if granted_ability.startswith("protection from "):
                 protections.add(granted_ability.replace("protection from ", "").strip())

         return list(protections) if protections else None
            
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
