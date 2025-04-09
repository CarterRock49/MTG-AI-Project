import logging
# Remove KeywordEffects if not used directly after refactoring
# from .keyword_effects import KeywordEffects
from .ability_types import Ability, ActivatedAbility, TriggeredAbility, StaticAbility, KeywordAbility, ManaAbility, AbilityEffect
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
        self.registered_abilities = {} # {card_id: [Ability, ...]}
        self.active_triggers = [] # Stores (Ability, controller) tuples to be processed
        self.targeting_system = None # Initialize targeting system reference

        if game_state is not None:
            # *** CHANGED: Initialize TargetingSystem correctly ***
            try:
                 self.targeting_system = TargetingSystem(game_state)
                 # Link it back to the game_state if necessary
                 if not hasattr(game_state, 'targeting_system'):
                      game_state.targeting_system = self.targeting_system
            except Exception as e:
                 logging.error(f"Error initializing TargetingSystem: {e}")
            # Initialize abilities AFTER targeting system is ready, if needed
            self._initialize_abilities()


    def handle_class_level_up(self, class_idx):
        """
        Handle leveling up a Class card with proper trigger processing.
        
        Args:
            class_idx: Index of the Class card in the battlefield
            
        Returns:
            bool: True if the class was successfully leveled up
        """
        gs = self.game_state
        
        # Get active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Check if class index is valid
        if class_idx < 0 or class_idx >= len(active_player["battlefield"]):
            logging.warning(f"Invalid class index: {class_idx}")
            return False
        
        # Get the class card
        class_id = active_player["battlefield"][class_idx]
        class_card = gs._safe_get_card(class_id)
        
        # Verify it's a class card
        if not class_card or not hasattr(class_card, 'is_class') or not class_card.is_class:
            logging.warning(f"Card with index {class_idx} is not a Class")
            return False
        
        # Check if the class can level up
        if not class_card.can_level_up():
            logging.warning(f"Class {class_card.name} cannot level up further")
            return False
        
        # Get the cost to level up
        next_level = class_card.current_level + 1 if hasattr(class_card, 'current_level') else 2
        level_cost = class_card.get_level_cost(next_level)
        
        # Check if we can afford to level up
        can_afford = True
        if level_cost and hasattr(gs, 'mana_system'):
            level_cost_parsed = gs.mana_system.parse_mana_cost(level_cost)
            can_afford = gs.mana_system.can_pay_mana_cost(active_player, level_cost_parsed)
        
        if not can_afford:
            logging.debug(f"Cannot afford to level up {class_card.name}")
            return False
        
        # Pay the cost
        if level_cost and hasattr(gs, 'mana_system'):
            gs.mana_system.pay_mana_cost(active_player, level_cost_parsed)
        
        # Level up the class
        success = class_card.level_up()
        
        if success:
            # Trigger level-up effects
            context = {
                "class_id": class_id,
                "previous_level": next_level - 1,
                "new_level": next_level,
                "controller": active_player
            }
            
            # Trigger ability for level change
            self.check_abilities(class_id, "CLASS_LEVEL_UP", context)
            
            # Reset priority for new abilities
            gs.phase = gs.PHASE_PRIORITY
            
            logging.debug(f"Leveled up {class_card.name} to level {next_level}")
            
            # Check if class became a creature at this level
            if hasattr(class_card, 'get_current_class_data'):
                level_data = class_card.get_current_class_data()
                
                if level_data and 'type_line' in level_data:
                    # Check if type line includes 'creature' at new level but not before
                    if ('creature' in level_data['type_line'].lower() and 
                        (not hasattr(class_card, 'previous_type_line') or 
                        'creature' not in class_card.previous_type_line.lower())):
                        
                        # Update card types
                        if hasattr(class_card, 'card_types'):
                            if 'creature' not in class_card.card_types:
                                class_card.card_types.append('creature')
                        
                        # Set power/toughness if specified
                        if 'power' in level_data:
                            class_card.power = level_data['power']
                        if 'toughness' in level_data:
                            class_card.toughness = level_data['toughness']
                        
                        logging.debug(f"Class {class_card.name} became a creature at level {next_level}")
                        
                    # Save current type line for future reference
                    class_card.previous_type_line = level_data['type_line']
            
            # Re-parse abilities for this level if needed
            self._parse_and_register_abilities(class_id, class_card)
        
        return success
        
    def handle_unlock_door(self, room_idx):
        """
        Handle unlocking a door on a Room card with proper trigger processing.
        
        Args:
            room_idx: Index of the Room card in the battlefield
            
        Returns:
            bool: True if door was unlocked successfully
        """
        gs = self.game_state
        
        # Get active player
        active_player = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Check if room index is valid
        if room_idx < 0 or room_idx >= len(active_player["battlefield"]):
            logging.warning(f"Invalid room index: {room_idx}")
            return False
        
        # Get the room card
        room_id = active_player["battlefield"][room_idx]
        room_card = gs._safe_get_card(room_id)
        
        # Verify it's a room card
        if not room_card or not hasattr(room_card, 'is_room') or not room_card.is_room:
            logging.warning(f"Card with index {room_idx} is not a Room")
            return False
        
        # Check if door2 is locked
        if not hasattr(room_card, 'door2') or room_card.door2.get('unlocked', False):
            logging.warning(f"Door is already unlocked or does not exist for Room {room_card.name}")
            return False
        
        # Get door cost
        door_cost = room_card.door2.get('mana_cost', '')
        
        # Check if we can afford to unlock
        can_afford = True
        if hasattr(gs, 'mana_system'):
            door_cost_parsed = gs.mana_system.parse_mana_cost(door_cost)
            can_afford = gs.mana_system.can_pay_mana_cost(active_player, door_cost_parsed)
        else:
            # Simple cost check (fallback)
            total_cost = 0
            for char in door_cost:
                if char.isdigit():
                    total_cost += int(char)
            can_afford = active_player.get("mana", 0) >= total_cost
        
        if not can_afford:
            logging.debug(f"Cannot afford to unlock door for {room_card.name}")
            return False
        
        # Pay the cost
        if hasattr(gs, 'mana_system'):
            door_cost_parsed = gs.mana_system.parse_mana_cost(door_cost)
            gs.mana_system.pay_mana_cost(active_player, door_cost_parsed)
        else:
            # Simple cost deduction
            active_player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        
        # Unlock the door
        room_card.door2['unlocked'] = True
        
        # Check if both doors are now unlocked
        fully_unlocked = False
        if hasattr(room_card, 'door1') and room_card.door1.get('unlocked', False):
            fully_unlocked = True
        
        # Process trigger effects
        context = {
            "door_number": 2,
            "room_id": room_id,
            "controller": active_player,
            "cost_paid": door_cost,
            "fully_unlocked": fully_unlocked
        }
        
        # Trigger door unlocked ability
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)
        
        # Also trigger specific door2 unlocked event
        self.check_abilities(room_id, "DOOR2_UNLOCKED", context)
        
        # If fully unlocked, trigger that too
        if fully_unlocked:
            self.check_abilities(room_id, "ROOM_FULLY_UNLOCKED", context)
        
        # Check for chapter advancement on the room card
        if hasattr(room_card, 'current_chapter') and hasattr(room_card, 'max_chapters'):
            room_card.current_chapter += 1
            logging.debug(f"Room {room_card.name} advanced to chapter {room_card.current_chapter}")
            
            # Create chapter context
            chapter_context = {
                "room_id": room_id,
                "controller": active_player,
                "chapter": room_card.current_chapter
            }
            
            # Trigger chapter ability if available
            if hasattr(self, 'apply_chapter_ability'):
                self.apply_chapter_ability(room_id, room_card.current_chapter, chapter_context)
            
            # Check if final chapter reached
            if room_card.current_chapter >= room_card.max_chapters:
                logging.debug(f"Room {room_card.name} completed all chapters")
                # Trigger completion effect
                self.check_abilities(room_id, "ROOM_COMPLETED", chapter_context)
        
        # Reset priority for new abilities
        gs.phase = gs.PHASE_PRIORITY
        
        logging.debug(f"Unlocked door 2 for Room {room_card.name}")
        
        # Re-parse abilities to include newly unlocked door
        self._parse_and_register_abilities(room_id, room_card)
        
        return True
    
    def _parse_text_with_patterns(self, text, patterns, ability_type, card_id, card, abilities_list):
            """
            Parse oracle text using regex patterns and create abilities of the specified type.
            Correctly handles reminder text removal before group extraction.

            Args:
                text: The oracle text to parse
                patterns: List of (pattern, restriction_generator) tuples for regex matching
                ability_type: Type of ability to create (ActivatedAbility, TriggeredAbility, etc.)
                card_id: ID of the card with the ability
                card: Card object
                abilities_list: List to add the created abilities to
            """
            # Pre-process text to remove reminder text in parentheses
            # Using a non-greedy match .*? to handle nested parentheses potentially better
            processed_text = re.sub(r'\([^()]*?\)', '', text.lower()).strip()
            # Double check cleaning edge cases like trailing spaces
            processed_text = re.sub(r'\s+([:.,;])', r'\1', processed_text).strip() # Remove space before punctuation

            for pattern, restriction_func in patterns:
                # Use the processed text without reminder text for matching
                matches = re.finditer(pattern, processed_text) # Use processed_text here
                for match in matches:
                    # Create ability based on type
                    if ability_type == "activated":
                        # Check number of groups expected by pattern (usually cost:effect = 2)
                        num_groups = pattern.count('(') - pattern.count('\(') # Estimate groups
                        if num_groups >= 2:
                            try:
                                cost, effect = match.groups()[:2] # Get the first two groups
                                # Check if cost/effect are valid (not None and not empty)
                                if cost is not None and effect is not None and cost.strip() and effect.strip():
                                    ability = ActivatedAbility(
                                        card_id=card_id,
                                        cost=cost.strip(),
                                        effect=effect.strip(),
                                        effect_text=match.group(0) # Use original match text for display
                                    )
                                    abilities_list.append(ability)
                                    logging.debug(f"Registered activated ability for {card.name}: {cost.strip()}: {effect.strip()}")
                                else:
                                    logging.debug(f"Skipped invalid activated ability match: groups were '{cost}', '{effect}'")
                            except IndexError:
                                logging.warning(f"Regex pattern '{pattern}' for activated ability generated fewer than 2 groups for match '{match.group(0)}'")
                        else:
                            # Fallback/Warning if pattern doesn't look like cost:effect
                            logging.debug(f"Activated ability pattern '{pattern}' doesn't seem to match 'cost:effect' structure.")

                    elif ability_type == "triggered":
                        # Assuming pattern captures the full "Trigger, Effect" string
                        full_match_text = match.group(0).strip()
                        # Split by the first comma NOT inside parentheses (already removed)
                        parts = full_match_text.split(',', 1)
                        trigger = parts[0].strip()
                        effect = parts[1].strip() if len(parts) > 1 else ""

                        if not trigger or not effect:
                            logging.debug(f"Skipped invalid triggered ability match: trigger='{trigger}', effect='{effect}'")
                            continue

                        # Extract any "if" conditions (already handled in original logic)
                        condition = None
                        if " if " in effect:
                            effect_parts = effect.split(" if ", 1)
                            effect = effect_parts[0].strip()
                            condition = "if " + effect_parts[1].strip()
                        elif " only if " in effect:
                            effect_parts = effect.split(" only if ", 1)
                            effect = effect_parts[0].strip()
                            condition = "only if " + effect_parts[1].strip()

                        ability = TriggeredAbility(
                            card_id=card_id,
                            trigger_condition=trigger,
                            effect=effect,
                            effect_text=full_match_text, # Original matched text
                            additional_condition=condition
                        )
                        abilities_list.append(ability)
                        logging.debug(f"Registered triggered ability for {card.name}: {full_match_text}")

                    elif ability_type == "static":
                        # Static ability is typically the whole match
                        effect = match.group(0).strip()
                        if not effect:
                            continue

                        # Apply any restrictions from the pattern (unmodified)
                        restrictions = {}
                        if callable(restriction_func):
                            restrictions = restriction_func(match) # Apply restrictions based on match object

                        ability = StaticAbility(
                            card_id=card_id,
                            effect=effect,
                            effect_text=effect
                        )
                        # Optionally add restrictions to the ability object if needed later
                        # setattr(ability, 'restrictions', restrictions)
                        abilities_list.append(ability)
                        logging.debug(f"Registered static ability for {card.name}: {effect}")
                    
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
        
    def trigger_ability(self, card_id, event_type, context=None):
        """
        Enhanced ability triggering with comprehensive system integration.
        
        Args:
            card_id: ID of the card triggering the ability
            event_type: Type of event that triggered the ability
            context: Additional context for the trigger
            
        Returns:
            list: List of triggered abilities that were added to the stack
        """
        if context is None:
            context = {}
        
        triggered_abilities = []
        
        # Add card type info to context
        card = self._safe_get_card(card_id)
        if card:
            if hasattr(card, 'card_types'):
                context["card_types"] = card.card_types
            context["source_card_id"] = card_id
            context["source_card_name"] = getattr(card, 'name', f"Card {card_id}")
        
        # Determine the card's owner and controller
        owner = self._find_card_owner(card_id)
        if not owner and card_id is not None:
            logging.warning(f"Could not find owner for card {card_id}")
            return triggered_abilities
        
        # Use ability handler if available
        if hasattr(self, 'ability_handler') and self.ability_handler:
            # Get triggered abilities
            handler_abilities = self.ability_handler.check_abilities(card_id, event_type, context)
            
            if handler_abilities:
                # Add each triggered ability to the stack
                for ability in handler_abilities:
                    # Create a unique ID for this ability instance
                    ability_instance_id = f"trigger_{card_id}_{event_type}_{len(self.stack)}"
                    
                    # Add to the stack unless it's a static ability
                    if getattr(ability, 'is_static', False):
                        # Static abilities don't use the stack
                        logging.debug(f"Static ability from {context.get('source_card_name', f'Card {card_id}')} applied directly")
                    else:
                        # Add to stack
                        self.stack.append(("TRIGGER", card_id, owner, {
                            "ability_instance_id": ability_instance_id,
                            "event_type": event_type,
                            "ability": ability,
                            "context": context
                        }))
                        
                        # Reset priority system for new stack item
                        self.priority_pass_count = 0
                        self.last_stack_size = len(self.stack)
                        self.phase = self.PHASE_PRIORITY
                        
                        triggered_abilities.append(ability_instance_id)
                        
                        logging.debug(f"Added triggered ability to stack: {ability.effect_text if hasattr(ability, 'effect_text') else 'Unknown ability'}")
                
                # Process triggered abilities that don't use the stack
                if hasattr(self.ability_handler, 'process_triggered_abilities'):
                    self.ability_handler.process_triggered_abilities()
        
        # Trigger replacement effects if applicable
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            replacement_context = {
                "event_type": event_type,
                "source_card_id": card_id,
                "affected_id": context.get('affected_id'),
                **context
            }
            
            # Apply any replacement effects
            modified_context, was_replaced = self.replacement_effects.apply_replacements(event_type, replacement_context)
        
        # Layer system interaction for continuous effects
        if hasattr(self, 'layer_system') and self.layer_system:
            # Refresh the layer system
            self.layer_system.apply_all_effects()
        
        return triggered_abilities

    def handle_modal_ability(self, card_id, controller, mode_index):
        """
        Handle a modal ability (with multiple options).
        
        Args:
            card_id: The ID of the card with the modal ability
            controller: The player activating the ability
            mode_index: Which mode was chosen
                    
        Returns:
            bool: Whether the ability was successfully activated and added to the stack
        """
        card = self.game_state._safe_get_card(card_id)
        if not card or not hasattr(card, 'oracle_text'):
            return False
        
        # Parse the modes from the oracle text
        modes = self._parse_modal_text(card.oracle_text)
        
        if not modes or mode_index >= len(modes):
            logging.debug(f"Invalid mode index {mode_index} for {card.name}")
            return False
        
        # Get the chosen mode text
        mode_text = modes[mode_index]
        
        # Get the cost of the modal ability
        ability_cost = "0"  # Default to no additional cost
        
        # Check for costs in the modal ability text
        cost_pattern = r'([{][^}]+[}])'
        cost_matches = re.findall(cost_pattern, card.oracle_text.split("choose")[0]) if "choose" in card.oracle_text.lower() else []
        
        if cost_matches:
            ability_cost = ''.join(cost_matches)
        
        # Check if the player can pay the cost
        if ability_cost != "0" and hasattr(self.game_state, 'mana_system') and self.game_state.mana_system:
            parsed_cost = self.game_state.mana_system.parse_mana_cost(ability_cost)
            if not self.game_state.mana_system.can_pay_mana_cost(controller, parsed_cost):
                logging.debug(f"Cannot pay cost for modal ability: {ability_cost}")
                return False
            
            # Pay the cost
            paid = self.game_state.mana_system.pay_mana_cost(controller, parsed_cost)
            if not paid:
                return False
            
            logging.debug(f"Paid cost {ability_cost} for modal ability")
        
        # Create effects for this mode
        mode_effects = EffectFactory.create_effects(mode_text)
        
        # Determine targets if needed
        targets = {}
        if "target" in mode_text.lower() and hasattr(self, 'targeting_system'):
            targets = self.targeting_system.resolve_targeting(card_id, controller, mode_text)
            
            # If targeting fails and was required, refund the cost and return failure
            if not targets and "target" in mode_text.lower():
                if ability_cost != "0" and hasattr(self.game_state, 'mana_system'):
                    # Refund the cost if targeting failed
                    self.game_state.mana_system.refund_mana_cost(controller, parsed_cost)
                return False
        
        # Add the ability to the stack
        self.game_state.add_to_stack("ABILITY", card_id, controller, {
            "ability_text": mode_text,
            "mode_index": mode_index,
            "mode_text": mode_text,
            "targets": targets,
            "effects": mode_effects
        })
        
        logging.debug(f"Added modal ability to stack: {mode_text} for {card.name}")
        return True

    def _parse_modal_text(self, oracle_text):
        """Parse modal text from card oracle text."""
        modes = []
        
        # Convert text to lowercase for easier matching
        text = oracle_text.lower()
        
        # Check for modal markers
        modal_markers = [
            "choose one —", 
            "choose one or both —", 
            "choose one or more —",
            "choose two —",
            "choose up to one —",
            "choose up to two —",
            "choose up to three —"
        ]
        
        modal_match = None
        for marker in modal_markers:
            if marker in text:
                modal_match = marker
                break
        
        if modal_match:
            # Split text after the modal marker
            modal_text = text.split(modal_match, 1)[1].strip()
            
            # Split by bullet points or numbered markers
            if '•' in modal_text or '●' in modal_text:
                # Split by bullet points
                mode_parts = re.split(r'[•●]', modal_text)
                for part in mode_parts:
                    part = part.strip()
                    if part:
                        modes.append(part)
            else:
                # If no bullet points, try to split by sequentially numbered options
                # e.g., "1. Do this. 2. Do that."
                mode_parts = re.split(r'\d+\.\s+', modal_text)
                for part in mode_parts[1:]:  # Skip the first empty part
                    part = part.strip()
                    if part:
                        modes.append(part)
        
        # Post-process modes to clean up
        clean_modes = []
        for mode in modes:
            # Remove trailing periods and other punctuation
            mode = re.sub(r'[;,\.]+$', '', mode).strip()
            if mode:
                clean_modes.append(mode)
        
        return clean_modes
        
    def register_card_abilities(self, card_id, player):
        """Register all abilities for a card as it enters the battlefield."""
        try:
            card = self.game_state._safe_get_card(card_id)
            if not card:
                logging.warning(f"Cannot register abilities: card {card_id} not found")
                return

            # Parse and register abilities (relies on _parse_and_register_abilities)
            self._parse_and_register_abilities(card_id, card)

            # Check for replacement effects
            if hasattr(self.game_state, 'replacement_effect_system') and self.game_state.replacement_effect_system:
                self.game_state.replacement_effect_system.register_card_replacement_effects(card_id, player)

            # Check for static abilities that need to be applied via LayerSystem
            # Layer system application now happens centrally in LayerSystem.apply_all_effects
            # We just need to ensure effects are registered. StaticAbility.apply handles registration.
            for ability in self.registered_abilities.get(card_id, []):
                if isinstance(ability, StaticAbility):
                    # StaticAbility.apply will register with LayerSystem if needed
                    ability.apply(self.game_state) # Pass only game_state now

        except Exception as e:
            logging.error(f"Error registering abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
                
    def unregister_card_abilities(self, card_id):
        """Unregister all abilities for a card as it leaves the battlefield."""
        try:
            # Remove from registered abilities
            if card_id in self.registered_abilities:
                del self.registered_abilities[card_id]
                
            # Remove from active triggers
            self.active_triggers = [trigger for trigger in self.active_triggers 
                                if trigger[0].card_id != card_id]
            
            # Remove from replacement effects
            if hasattr(self.game_state, 'replacement_effect_system'):
                # Specific method to remove effects by source
                self.game_state.replacement_effect_system.remove_effects_by_source(card_id)
                
            # Remove from layer system
            if hasattr(self.game_state, 'layer_system'):
                # Remove effects originating from this card
                self.game_state.layer_system.remove_effects_by_source(card_id)
        
        except Exception as e:
            logging.error(f"Error unregistering abilities for card {card_id}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def handle_door_unlock_triggers(self, room_id, door_number):
        """Handle triggers that occur when a door is unlocked."""
        gs = self.game_state
        card = gs._safe_get_card(room_id)
        if not card:
            return

        # Find controller using GameState method
        controller = gs.get_card_controller(room_id) # Use GameState helper

        if not controller:
            logging.warning(f"Could not find controller for room {room_id}") # Added logging
            return

        # Create context for the trigger
        context = {
            "door_number": door_number,
            "controller": controller,
            "game_state": gs
        }

        # Trigger abilities
        self.check_abilities(room_id, "DOOR_UNLOCKED", context)

        # If this is a specific door, also trigger abilities for that door
        if door_number > 0:
            self.check_abilities(room_id, f"DOOR{door_number}_UNLOCKED", context)

        # Process door effect triggers from card data if available
        if hasattr(card, 'door_effects') and card.door_effects.get("unlock_trigger"):
            for effect_text in card.door_effects.get("unlock_trigger", []):
                # Create a custom effect and add to stack
                effect_context = {
                    "effect_text": effect_text,
                    "controller": controller,
                    "source_id": room_id
                }

                # Add to stack
                gs.add_to_stack("ABILITY", room_id, controller, effect_context)

        logging.debug(f"Processed door unlock triggers for door {door_number} of {card.name}")
    
    def _initialize_abilities(self):
        """Parse abilities from all cards in the database and register them"""
        gs = self.game_state
        # Ensure card_db is a dict
        if not isinstance(gs.card_db, dict):
             logging.error("GameState card_db is not a dictionary, cannot initialize abilities.")
             return
        for card_id, card in gs.card_db.items():
            self._parse_and_register_abilities(card_id, card)
            
    def _parse_and_register_abilities(self, card_id, card):
        """Parse a card's oracle text to identify and register abilities, including keywords."""
        if not card: return

        abilities = []
        oracle_text = getattr(card, 'oracle_text', '').lower() if hasattr(card, 'oracle_text') else ''
        keywords_text = getattr(card, 'keywords_list_text', []) # Assume card might have pre-parsed keyword list

        # 1. Handle Class Card Levels
        if hasattr(card, 'is_class') and card.is_class:
            current_level = getattr(card, 'current_level', 1)
            level_data = card.get_current_class_data() if hasattr(card, 'get_current_class_data') else None
            if level_data and 'abilities' in level_data:
                for ability_text in level_data['abilities']:
                    self._parse_ability_text(card_id, card, ability_text.lower(), abilities)
                # Explicit keywords from level
                level_keywords = level_data.get('keywords', [])
                for kw_text in level_keywords: # kw_text might be "flying" or "trample 2"
                     keyword_name = kw_text.split()[0] # Extract base keyword
                     self._create_keyword_ability(card_id, card, keyword_name, abilities, kw_text)

        # 2. Parse Standard Oracle Text if not fully handled by Class levels or if not a Class card
        ability_texts = []
        if oracle_text:
            processed_oracle = re.sub(r'\([^)]*\)', '', oracle_text).strip()
            # Split by newline AND common ability separators like '•'
            potential_texts = re.split(r'\n\s*•\s*|\n', processed_oracle)
            ability_texts = [text.strip() for text in potential_texts if text.strip()]

        for ability_text in ability_texts:
             self._parse_ability_text(card_id, card, ability_text, abilities)

        # 3. Parse Explicit Keywords (from keywords array/list if available)
        # --- Consolidated keyword parsing using _create_keyword_ability ---
        # Get keywords already parsed from text/levels to avoid duplicates
        parsed_keywords_from_text = {ab.keyword for ab in abilities if hasattr(ab, 'keyword')} # Check Static/Triggered too

        # Check keywords array first for efficiency
        if hasattr(card, 'keywords') and isinstance(card.keywords, list):
            for i, keyword_name in enumerate(Card.ALL_KEYWORDS):
                if i < len(card.keywords) and card.keywords[i] == 1:
                    if keyword_name not in parsed_keywords_from_text:
                        self._create_keyword_ability(card_id, card, keyword_name, abilities)
                        parsed_keywords_from_text.add(keyword_name) # Mark as handled
        # Check text list as fallback/addition
        elif keywords_text:
             for kw_text in keywords_text: # e.g., "Flying", "Trample", "Annihilator 2"
                  parts = kw_text.lower().split()
                  keyword_name = parts[0]
                  if keyword_name in Card.ALL_KEYWORDS: # Check if it's a known keyword base
                       if keyword_name not in parsed_keywords_from_text:
                           self._create_keyword_ability(card_id, card, keyword_name, abilities, kw_text) # Pass full text
                           parsed_keywords_from_text.add(keyword_name)


        # Store all parsed abilities
        if abilities:
            self.registered_abilities[card_id] = abilities
            logging.debug(f"Registered {len(abilities)} abilities/keywords for {card.name} ({card_id})")

        # Special Case: Immediately register continuous effects via StaticAbility.apply
        for ability in abilities:
            if isinstance(ability, StaticAbility):
                 # apply() method now handles registration with LayerSystem
                 ability.apply(self.game_state)

    def _create_keyword_ability(self, card_id, card, keyword_name, abilities_list, full_keyword_text=None):
            """
            Creates the appropriate Ability object for a given keyword. Handles parameters better.
            Acknowledges complexity for keywords requiring specific rules/effects.
            """
            keyword_lower = keyword_name.lower()
            full_text = (full_keyword_text or keyword_name).lower() # Use full text if provided

            # Avoid duplicates based on simple name match (can be enhanced later)
            if any(isinstance(a, (KeywordAbility, StaticAbility, TriggeredAbility, ActivatedAbility)) and getattr(a, 'keyword', None) == keyword_lower for a in abilities_list):
                 return

            def parse_value(text, keyword):
                 match = re.search(rf"{re.escape(keyword)}\s+(\d+)", text)
                 return int(match.group(1)) if match else 1

            def parse_cost(text, keyword):
                 # Enhanced cost parsing to handle various formats {W}{2}, {X}, etc.
                 cost_patterns = [
                     rf"{re.escape(keyword)}\s*(?:—|-|:)?\s*(\{{[^\}}]+\}}|\d+|[xX])"
                     rf"{re.escape(keyword)}\s*(\d+)\b", # Just a number
                     rf"{re.escape(keyword)}\s*(\{{[xX]\}})\b", # {X}
                 ]
                 for pattern in cost_patterns:
                     match = re.search(pattern, text)
                     if match:
                         cost_part = match.group(1)
                         if cost_part.isdigit(): return f"{{{cost_part}}}" # Normalize number
                         return cost_part # Return cost string like {W}{2} or {X}
                 return "{0}" # Default free if no cost found

            # --- Static Combat/Attribute Keywords -> StaticAbility grant ---
            static_keywords = [
                "flying", "first strike", "double strike", "trample", "vigilance", "haste",
                "menace", "reach", "defender", "indestructible", "hexproof", "shroud",
                "unblockable", "fear", "intimidate", "shadow", "horsemanship", "flanking", # Note: Flanking also has a trigger
                "banding", "phasing", "lifelink", "deathtouch", "changeling", "devoid",
                "protection", "ward" # Protection/Ward parameters handled via effect_text
            ]
            if keyword_lower in static_keywords:
                 # The effect text includes parameters like "protection from red" or "ward {2}"
                 ability_effect_text = f"This permanent has {full_text}."
                 ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
                 setattr(ability, 'keyword', keyword_lower)
                 abilities_list.append(ability)
                 return

            # --- Triggered Keywords -> TriggeredAbility ---
            # (Map updated slightly, simplified effects where full impl is complex)
            triggered_map = {
                 "prowess": ("whenever you cast a noncreature spell", "this creature gets +1/+1 until end of turn"),
                 "cascade": ("when you cast this spell", "cascade."), # Effect handled by game logic
                 "storm": ("when you cast this spell", "storm."), # Effect handled by game logic
                 "riot": ("this permanent enters the battlefield", "choose haste or a +1/+1 counter"),
                 "enrage": ("whenever this creature is dealt damage", "{effect_from_oracle}"), # Placeholder
                 "afflict": ("whenever this creature becomes blocked", "defending player loses N life"),
                 "mentor": ("whenever this creature attacks", "put a +1/+1 counter on target attacking creature with lesser power."),
                 "afterlife": ("when this permanent dies", "create N 1/1 white and black Spirit creature tokens with flying."),
                 "annihilator": ("whenever this creature attacks", "defending player sacrifices N permanents."),
                 "bloodthirst": ("this creature enters the battlefield", "if an opponent was dealt damage this turn, it enters with N +1/+1 counters."),
                 "bushido": ("whenever this creature blocks or becomes blocked", "it gets +N/+N until end of turn."),
                 "evolve": ("whenever a creature enters the battlefield under your control", "if that creature has greater power or toughness than this creature, put a +1/+1 counter on this creature."),
                 "fabricate": ("when this permanent enters the battlefield", "put N +1/+1 counters on it or create N 1/1 colorless Servo artifact creature tokens."),
                 "fading": ("this permanent enters the battlefield", "it enters with N fade counters on it. at the beginning of your upkeep, remove a fade counter. if you can't, sacrifice it."),
                 "flanking": ("whenever a creature without flanking blocks this creature", "the blocking creature gets -1/-1 until end of turn."),
                 "gravestorm": ("when you cast this spell", "gravestorm."), # Effect handled by game logic
                 "haunt": ("when this permanent dies", "exile it haunting target creature. (Haunt trigger happens when haunted creature dies)"),
                 "ingest": ("whenever this creature deals combat damage to a player", "that player exiles the top card of their library."),
                 "infect": ("this deals damage", "damage is dealt in the form of -1/-1 counters (creatures) or poison counters (players)."), # Static/replacement
                 "modular": ("this enters the battlefield", "with N +1/+1 counters. when it dies, you may put its +1/+1 counters on target artifact creature."),
                 "persist": ("when this permanent dies", "if it had no -1/-1 counters, return it with a -1/-1 counter."),
                 "poisonous": ("whenever this creature deals combat damage to a player", "that player gets N poison counters."),
                 "rampage": ("whenever this creature becomes blocked", "it gets +N/+N for each creature blocking it beyond the first."),
                 "renown": ("whenever this creature deals combat damage to a player", "if it isn't renowned, put N +1/+1 counters on it and it becomes renowned."),
                 "ripple": ("when you cast this spell", "ripple N."), # Effect handled by game logic
                 "soulshift": ("when this permanent dies", "you may return target spirit card with mana value N or less from your graveyard to hand."),
                 "sunburst": ("this permanent enters the battlefield", "with a +1/+1 counter or charge counter for each color of mana spent to cast it."),
                 "training": ("whenever this creature attacks with another creature with greater power", "put a +1/+1 counter on this creature."),
                 "undying": ("when this permanent dies", "if it had no +1/+1 counters, return it with a +1/+1 counter."),
                 "vanishing": ("this permanent enters the battlefield", "with N time counters. at the beginning of your upkeep, remove a time counter. when the last is removed, sacrifice it."),
                 "wither": ("this deals damage", "damage is dealt to creatures in the form of -1/-1 counters."), # Static/replacement
                 # --- Add potentially missed triggered keywords ---
                 "decayed": ("this creature can't block. when it attacks", "sacrifice it at end of combat."),
                 "battle cry": ("whenever this creature attacks", "each other attacking creature gets +1/+0 until end of turn."),
                 "explore": ("when this creature enters the battlefield", "explore."), # Effect handled by game logic
                 "extort": ("whenever you cast a spell", "you may pay {W/B}. if you do, each opponent loses 1 life and you gain that much life."),
                 "melee": ("whenever this creature attacks", "it gets +1/+1 until end of turn for each opponent you attacked this combat."),
                 "investigate": ("when {condition}", "create a Clue token."), # Trigger varies
            }
            if keyword_lower in triggered_map:
                 trigger, effect = triggered_map[keyword_lower]
                 val = parse_value(full_text, keyword_lower)
                 effect = effect.replace(" N ", f" {val} ")
                 # Create TriggeredAbility, but acknowledge complex effects might need specific handlers
                 ability = TriggeredAbility(card_id, trigger, effect, full_text)
                 setattr(ability, 'keyword', keyword_lower)
                 abilities_list.append(ability)
                 return

            # --- Activated Keywords -> ActivatedAbility ---
            activated_map = {
                "cycling": ("draw a card."),
                "equip": ("attach to target creature you control. activate only as a sorcery."),
                "fortify": ("attach to target land you control. activate only as a sorcery."),
                "unearth": ("return this card from your graveyard to the battlefield. it gains haste. exile it at the beginning of the next end step or if it would leave the battlefield. unearth only as a sorcery."),
                "flashback": ("cast from graveyard, then exile."), # Rule modification
                "retrace": ("cast from graveyard by discarding a land."), # Rule modification
                "scavenge": ("exile this card from graveyard: put N +1/+1 counters on target creature. scavenge only as a sorcery."),
                "transfigure": ("sacrifice this creature: search library for creature with same mana value, put onto battlefield, shuffle. activate only as a sorcery."),
                "transmute": ("discard this card: search library for card with same mana value, put into hand, shuffle. activate only as a sorcery."),
                "auraswap": ("exchange this aura with an aura card in your hand."), # Needs hand interaction
                "outlast": ("put a +1/+1 counter on this creature. activate only as a sorcery."),
                "reinforce": ("discard this card: put N +1/+1 counters on target creature."),
                "reconfigure": ("attach to target creature you control or unattach. activate only as a sorcery."),
                # --- Add potentially missed activated keywords ---
                "adapt": ("if this creature has no +1/+1 counters on it, put N +1/+1 counters on it."),
                "level up": ("put a level counter on this creature. level up only as a sorcery."), # Needs Class card logic
                "monstrosity": ("put N +1/+1 counters on this creature and it becomes monstrous."),
                "crew": ("tap N power of creatures you control: this vehicle becomes an artifact creature."),
                "channel": ("discard this card: {effect}."), # Effect varies
                "forecast": ("reveal this card from hand during upkeep: {effect}."), # Effect varies
            }
            cost_str = parse_cost(full_text, keyword_lower)
            val = parse_value(full_text, keyword_lower) # Still need value for effects like reinforce N

            if keyword_lower in activated_map and cost_str != "{0}": # Ensure cost was found
                 effect = activated_map[keyword_lower]
                 effect = effect.replace(" N ", f" {val} ")
                 # Create ActivatedAbility
                 ability = ActivatedAbility(card_id, cost_str, effect, full_text)
                 setattr(ability, 'keyword', keyword_lower)
                 abilities_list.append(ability)
                 return

            # --- Rule Modifying Keywords -> StaticAbility (effect handled by game rules) ---
            rule_keywords = [
                "affinity", "convoke", "delve", "improvise", "bestow", "buyback",
                "entwine", "escape", "kicker", "madness", "overload", "splice",
                "surge", "split second", "suspend", "companion", "demonstrate", # Demonstrate handled by rule
                "embalm", "eternalize", "jump-start", "rebound", "spree" # Handled by rules/context
            ]
            if keyword_lower in rule_keywords:
                 ability_effect_text = f"This card has {full_text}."
                 ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
                 setattr(ability, 'keyword', keyword_lower)
                 abilities_list.append(ability)
                 return

            # --- Fallback for keywords not fully mapped ---
            logging.warning(f"Keyword '{keyword_lower}' (from '{full_text}') not fully mapped. Creating generic StaticAbility.")
            ability_effect_text = f"This permanent has {full_text}." # Treat as static grant
            ability = StaticAbility(card_id, ability_effect_text, ability_effect_text)
            setattr(ability, 'keyword', keyword_lower)
            abilities_list.append(ability)
        
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
        """Parse a single ability text and add appropriate ability objects to the list."""
        ability_text = ability_text.lower()
        
        # Check for activated ability pattern
        if ":" in ability_text:
            parts = ability_text.split(":", 1)
            if len(parts) == 2:
                cost, effect = parts[0].strip(), parts[1].strip()
                ability = ActivatedAbility(
                    card_id=card_id,
                    cost=cost,
                    effect=effect,
                    effect_text=ability_text
                )
                abilities_list.append(ability)
                return
                
        # Check for triggered ability patterns
        for pattern, event_type in [
            (r'when(ever)?\s+', 'GENERIC_TRIGGER'),
            (r'at the beginning of', 'PHASE_TRIGGER'),
            (r'when(ever)?\s+you unlock', 'DOOR_UNLOCKED')
        ]:
            if re.search(pattern, ability_text):
                ability = TriggeredAbility(
                    card_id=card_id,
                    trigger_condition=ability_text,
                    effect=ability_text.split(",", 1)[1].strip() if "," in ability_text else ability_text,
                    effect_text=ability_text
                )
                abilities_list.append(ability)
                return
                
        # Default to static ability
        ability = StaticAbility(
            card_id=card_id,
            effect=ability_text,
            effect_text=ability_text
        )
        abilities_list.append(ability)
                    
    def _create_token(self, controller, token_data):
        """Create a token creature or artifact"""
        gs = self.game_state
        
        # Create token tracking if it doesn't exist
        if not hasattr(controller, "tokens"):
            controller["tokens"] = []
        
        # Generate unique token ID
        token_id = f"TOKEN_{len(controller['tokens'])}"
        
        # Create token based on provided data
        token = Card({
            "name": token_data.get("name", "Token"),
            "type_line": token_data.get("type_line", "Creature"),
            "card_types": token_data.get("card_types", ["creature"]),
            "subtypes": token_data.get("subtypes", []),
            "power": token_data.get("power", 1),
            "toughness": token_data.get("toughness", 1),
            "oracle_text": token_data.get("oracle_text", ""),
            "keywords": token_data.get("keywords", [0] * 11),
            "colors": token_data.get("colors", [0, 0, 0, 0, 0])
        })
        
        # Add token to game
        gs.card_db[token_id] = token
        controller["battlefield"].append(token_id)
        controller["tokens"].append(token_id)
        
        # Mark token as having summoning sickness
        controller["entered_battlefield_this_turn"].add(token_id)
        
        logging.debug(f"Created {token_data.get('name', 'Token')} token")
        return token_id

    def _destroy_permanent(self, permanent_id, player=None):
        """Destroy a permanent, handling indestructible"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Check for indestructible
        card = gs._safe_get_card(permanent_id)
        if card and hasattr(card, 'oracle_text') and "indestructible" in card.oracle_text.lower():
            logging.debug(f"Cannot destroy {card.name} due to indestructible")
            return False
            
        # Move to graveyard
        gs.move_card(permanent_id, player, "battlefield", player, "graveyard")
        logging.debug(f"Destroyed {card.name if card else permanent_id}")
        return True

    def _exile_permanent(self, permanent_id, player=None):
        """Exile a permanent, bypassing indestructible"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Move to exile
        gs.move_card(permanent_id, player, "battlefield", player, "exile")
        card = gs._safe_get_card(permanent_id)
        logging.debug(f"Exiled {card.name if card else permanent_id}")
        return True
    
    def _add_counters(self, permanent_id, counter_type, count, player=None):
        """Add counters to a permanent"""
        gs = self.game_state
        
        # Find owner if not provided
        if not player:
            for p in [gs.p1, gs.p2]:
                if permanent_id in p["battlefield"]:
                    player = p
                    break
        
        if not player:
            logging.debug(f"Could not find owner for permanent {permanent_id}")
            return False
            
        # Get the card
        card = gs._safe_get_card(permanent_id)
        if not card:
            return False
            
        # Initialize counters if needed
        if not hasattr(card, "counters"):
            card.counters = {}
            
        # Add counters
        card.counters[counter_type] = card.counters.get(counter_type, 0) + count
        
        # Apply counter effects
        if counter_type == "+1/+1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power += count
            card.toughness += count
        elif counter_type == "-1/-1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
            card.power -= count
            card.toughness -= count
            
        logging.debug(f"Added {count} {counter_type} counters to {card.name}")
        return True
    

    def _parse_activated_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse activated abilities from card text using enhanced patterns"""
        # Define more comprehensive patterns for activated abilities
        activated_patterns = [
            # Standard "Cost: Effect" pattern with named capture groups
            (r'(?P<cost>[^:]+?):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": m.group("cost").strip(), "effect": m.group("effect").strip()}),
            
            # Handle loyalty abilities like "+1: Do something"
            (r'(?P<loyalty>[+\-][\d]+):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": m.group("loyalty").strip(), "effect": m.group("effect").strip(), "loyalty_ability": True}),
            
            # Handle tap/untap symbol in costs with other potential costs
            (r'(?P<tap>\{[T]\}|\{[Q]\})(?P<additional_cost>[^:]*?):\s+(?P<effect>[^\.;]+)', 
            lambda m: {"cost": (m.group("tap") + (m.group("additional_cost") if m.group("additional_cost") else "")).strip(), 
                    "effect": m.group("effect").strip()})
        ]
        
        # Parse and add abilities
        self._parse_text_with_patterns(
            oracle_text, activated_patterns, "activated", card_id, card, abilities_list)
    
    def _parse_static_abilities(self, card_id, card, oracle_text, abilities_list):
        """Parse static abilities from card text with improved pattern matching"""
        # Define enhanced patterns for static abilities with functions to extract data
        static_patterns = [
            # "Creatures you control get +X/+Y" pattern
            (r'(?P<target>creatures\s+you\s+control)\s+get\s+(?P<bonus>[+\-][\d]+/[+\-][\d]+)', 
            lambda m: {'power_boost': int(m.group("bonus").split('/')[0]), 
                    'toughness_boost': int(m.group("bonus").split('/')[1]), 
                    'affected_type': m.group("target")}),
            
            # "X creatures get -A/-B" pattern
            (r'(?P<target>[^\.;]+?creatures[^\.;]+?)\s+get\s+(?P<penalty>[+\-][\d]+/[+\-][\d]+)', 
            lambda m: {'power_penalty': int(m.group("penalty").split('/')[0]), 
                    'toughness_penalty': int(m.group("penalty").split('/')[1]), 
                    'affected_type': m.group("target")}),
            
            # "While this door is locked/unlocked" pattern
            (r'while\s+this\s+door\s+is\s+(?P<state>locked|unlocked)', 
            lambda m: {'door_state': m.group("state")}),
            
            # "X spells cost {Y} less" pattern
            (r'(?P<spell_type>[^\.;]+?)\s+spells\s+(?:you\s+cast\s+)?cost\s+\{(?P<amount>[\d]+)\}\s+less', 
            lambda m: {'spell_type': m.group("spell_type").strip(), 'cost_reduction': int(m.group("amount"))}),
            
            # "Creatures can't attack" pattern
            (r'(?P<entity>creatures)(?P<qualifier>[^\.;]*?)\s+can\'t\s+(?P<restriction>attack)', 
            lambda m: {'prevent_attack': True, 'entity': m.group("entity"), 'qualifier': m.group("qualifier").strip()}),
            
            # "You can cast X spells as though they had flash" pattern
            (r'you\s+can\s+cast\s+(?P<spell_type>[^\.;]+?)\s+(?:spells\s+)?as\s+though\s+(?:it|they)\s+had\s+flash', 
            lambda m: {'spell_type_with_flash': m.group("spell_type").strip()}),
            
            # "X creatures you control have Y" pattern
            (r'(?P<creature_type>[^\.;]+?)\s+creatures\s+you\s+control\s+have\s+(?P<ability>[^\.;]+)', 
            lambda m: {'creature_type': m.group("creature_type").strip(), 'granted_ability': m.group("ability").strip()})
        ]
        
        # Parse and add abilities
        self._parse_text_with_patterns(
            oracle_text, static_patterns, "static", card_id, card, abilities_list)
    

    def check_abilities(self, card_id, event_type, context=None):
        """
        Checks for triggered abilities based on game events. Relies on parsed abilities.
        (Now fully uses the TriggeredAbility logic).
        """
        if context is None: context = {}
        gs = self.game_state
        card = gs._safe_get_card(card_id) # Card associated with the event ORIGIN

        # Add game state and event type to context for conditions
        context['game_state'] = gs
        context['event_type'] = event_type # Ensure event type is in context

        triggered_abilities_found = [] # Abilities triggered by this event

        # Check abilities registered for *all* cards currently in a relevant zone
        cards_to_check = set()
        for p in [gs.p1, gs.p2]:
             cards_to_check.update(p.get("battlefield", []))
             cards_to_check.update(p.get("graveyard", [])) # For abilities triggering from GY (e.g., Haunt, Recover)
             # Add other zones if needed (hand, exile for madness etc.)
             # cards_to_check.update(p.get("hand", []))
             # cards_to_check.update(p.get("exile", []))

        # Check abilities on permanents currently phased out? Rules check needed. Maybe not.

        for ability_source_id in cards_to_check:
            source_card = gs._safe_get_card(ability_source_id)
            # Basic check: Ability source must exist
            if not source_card: continue

            # Optimization: Check if card is actually in a zone where its abilities function
            # E.g., battlefield abilities only active on battlefield (unless specified otherwise)
            source_location = gs.find_card_location(ability_source_id)
            # Determine if ability works from its current zone (default: battlefield)
            # TODO: This check needs refinement based on specific ability rules (e.g., cycling from hand)
            # if not source_location or source_location[1] not in ["battlefield", "graveyard"]: # Simplistic check
            #      continue

            registered_abilities = self.registered_abilities.get(ability_source_id, [])
            for ability in registered_abilities:
                # Only check TriggeredAbility instances
                if isinstance(ability, TriggeredAbility):
                    # Prepare context specific to this potential trigger check
                    trigger_check_context = context.copy()
                    trigger_check_context['source_card_id'] = ability_source_id
                    trigger_check_context['source_card'] = source_card
                    # Add event card info if not already the same as source
                    if 'event_card_id' not in trigger_check_context: trigger_check_context['event_card_id'] = card_id
                    if 'event_card' not in trigger_check_context: trigger_check_context['event_card'] = card

                    try:
                        if ability.can_trigger(event_type, trigger_check_context):
                            # Find controller of the ability source at the time of trigger check
                            ability_controller = gs.get_card_controller(ability_source_id)
                            if ability_controller:
                                # Queue the trigger: (Ability object, Controller dict)
                                self.active_triggers.append((ability, ability_controller))
                                triggered_abilities_found.append(ability)
                                # Reduced verbosity logging
                                # logging.debug(f"Queued trigger: '{ability.trigger_condition}' from {ability_source_id} due to {event_type}")
                            else:
                                 # This can happen if the source left the battlefield before trigger check
                                 # logging.warning(f"Could not find controller for triggered ability source {ability_source_id}")
                                 pass # Ability cannot trigger without a controller
                    except Exception as e:
                         logging.error(f"Error checking trigger condition for {ability.effect_text} from {ability_source_id}: {e}")
                         # Continue checking other abilities

        # Return the list of ability objects that triggered (used by trigger_ability)
        return triggered_abilities_found

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
        """Process all pending triggered abilities, adding them to the stack in APNAP order."""
        gs = self.game_state
        if not self.active_triggers:
            return
            
        # Sort triggers by APNAP order
        ap_triggers = []
        nap_triggers = []
        active_player = gs._get_active_player()
        
        for ability, controller in self.active_triggers:
            if controller == active_player:
                ap_triggers.append((ability, controller))
            else:
                nap_triggers.append((ability, controller))
        
        # Process Active Player's triggers first
        for ability, controller in ap_triggers:
            # Check if ability is a valid object with card_id attribute
            if not ability or not hasattr(ability, 'card_id'):
                logging.warning("Skipping invalid ability in active_triggers")
                continue
                
            # Add context information
            context = {
                "ability": ability,
                "trigger_type": ability.trigger_condition if hasattr(ability, 'trigger_condition') else "",
                "effect_text": ability.effect_text,
                "source_id": ability.card_id
            }
            
            gs.add_to_stack("ABILITY", ability.card_id, controller, context)
            logging.debug(f"Added AP triggered ability to stack: {ability.effect_text}")
        
        # Then process Non-Active Player's triggers
        for ability, controller in nap_triggers:
            # Check if ability is a valid object with card_id attribute
            if not ability or not hasattr(ability, 'card_id'):
                logging.warning("Skipping invalid ability in active_triggers")
                continue
                
            # Add context information
            context = {
                "ability": ability,
                "trigger_type": ability.trigger_condition if hasattr(ability, 'trigger_condition') else "",
                "effect_text": ability.effect_text,
                "source_id": ability.card_id
            }
            
            gs.add_to_stack("ABILITY", ability.card_id, controller, context)
            logging.debug(f"Added NAP triggered ability to stack: {ability.effect_text}")
        
        # Clear active triggers
        self.active_triggers = []
    
    def resolve_ability(self, ability_type, card_id, controller, context=None):
        """
        Resolve an ability from the stack. Finds the specific ability instance from the context.
        (Now uses unified fallback logic).
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id) # Source card for logging
        source_name = card.name if card else f"Card {card_id}"

        if context and "ability" in context and isinstance(context["ability"], Ability):
            ability = context["ability"] # Get the specific Ability object instance

            # Validate Targets using Targeting System
            targets_on_stack = context.get("targets", {}) # Default to empty dict
            if self.targeting_system: # Check if targeting system exists
                if not self.targeting_system.validate_targets(ability.card_id, targets_on_stack, controller):
                    logging.debug(f"Targets for '{getattr(ability, 'effect_text', 'Unknown')}' from {source_name} became invalid. Fizzling.")
                    return # Fizzle

            # If targets still valid (or none required), resolve the ability
            try:
                if hasattr(ability, 'resolve_with_targets') and targets_on_stack:
                    ability.resolve_with_targets(gs, controller, targets_on_stack)
                elif hasattr(ability, 'resolve'):
                    ability.resolve(gs, controller) # Standard resolve
                else:
                    # Fallback using generic resolution if specific resolve missing
                    logging.warning(f"Ability object for {source_name} lacks specific resolve method. Using generic effect application.")
                    ability._resolve_ability_effect(gs, controller, targets_on_stack)

                logging.debug(f"Resolved {ability_type} ability for {source_name}: {getattr(ability,'effect_text','Unknown effect')}")

            except Exception as e:
                logging.error(f"Error resolving ability {ability_type} ({getattr(ability,'effect_text','Unknown')}) for {source_name}: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())

        else: # Fallback if 'ability' object missing in context
             logging.warning(f"No valid 'ability' object found in context for resolving {ability_type} from {source_name}. Attempting fallback resolution from text.")
             effect_text = context.get('effect_text', '') if context else ''
             targets_on_stack = context.get("targets", {}) # Get targets passed when added to stack
             if effect_text:
                  # Use EffectFactory and generic resolution
                  effects = EffectFactory.create_effects(effect_text, targets=targets_on_stack)
                  for effect_obj in effects:
                       # Re-validate targets just before applying this specific effect?
                       # Might be overkill if overall validation passed, but safer.
                       # effect_obj.apply(gs, card_id, controller, targets_on_stack) # Simple apply
                       # Enhanced apply with pre-validation:
                       if effect_obj.requires_target and self.targeting_system:
                            if not self.targeting_system.validate_targets(card_id, targets_on_stack, controller):
                                 logging.debug(f"Targets invalid for effect '{effect_obj.effect_text}'. Skipping.")
                                 continue # Skip this specific effect if its targets are now bad
                       effect_obj.apply(gs, card_id, controller, targets_on_stack)
             else:
                  logging.error(f"Cannot resolve {ability_type} from {source_name}: Missing ability object and effect text in context.")
                  
    def _check_keyword_internal(self, card, keyword):
        """
        Centralized check for keywords, considering layers and static abilities.
        (Internal helper for TargetingSystem and other modules).
        """
        if not card or not isinstance(card, Card): return False
        keyword_lower = keyword.lower()
        card_id = getattr(card, 'card_id', None)
        if not card_id: # Fallback for temporary cards maybe?
            return keyword_lower in getattr(card, 'oracle_text', '').lower()

        # 1. Check Layer System modifications (Layer 6)
        gs = self.game_state
        if hasattr(gs, 'layer_system'):
             if gs.layer_system.has_effect(card_id, 'remove_ability', keyword_lower) or \
                gs.layer_system.has_effect(card_id, 'remove_all_abilities'):
                  return False # Ability explicitly removed
             if gs.layer_system.has_effect(card_id, 'add_ability', keyword_lower):
                  return True # Ability explicitly granted

        # 2. Check static abilities registered for the card (e.g., from itself or levels)
        registered_abilities = self.registered_abilities.get(card_id, [])
        # Check StaticAbility grant (more specific text match)
        if any(isinstance(ab, StaticAbility) and ab.effect_text == f"This permanent has {keyword_lower}." for ab in registered_abilities):
             return True
        # Check KeywordAbility instances
        if any(isinstance(ab, KeywordAbility) and ab.keyword == keyword_lower for ab in registered_abilities):
             return True
        # Broader check in StaticAbility effect text (less precise)
        if any(isinstance(ab, StaticAbility) and f"has {keyword_lower}" in ab.effect for ab in registered_abilities):
             return True

        # 3. Fallback: Check the Card object's calculated 'keywords' array (reflects layers if updated)
        if hasattr(card, 'keywords') and isinstance(card.keywords, list):
            try:
                idx = Card.ALL_KEYWORDS.index(keyword_lower)
                if idx < len(card.keywords) and card.keywords[idx] == 1:
                    return True
            except ValueError: pass # Keyword not in list

        # 4. Last Resort: Check original oracle text (least reliable for current state)
        # This should rarely be needed if layers and abilities are parsed correctly
        # elif hasattr(card, 'oracle_text') and keyword_lower in getattr(card, 'oracle_text', '').lower():
        #      return True

        return False
            
    def handle_attack_triggers(self, attacker_id):
        """Handle triggered abilities that trigger when a creature attacks."""
        gs = self.game_state
        card = gs._safe_get_card(attacker_id)
        if not card:
            return
            
        # Find controller
        controller = None
        for player in [gs.p1, gs.p2]:
            if attacker_id in player["battlefield"]:
                controller = player
                break
                
        if not controller:
            return
        
        # First check abilities on the attacking creature itself
        if hasattr(card, 'oracle_text'):
            oracle_text = card.oracle_text.lower()
            
            # Check for "when/whenever this creature attacks" triggers
            if "when this creature attacks" in oracle_text or "whenever this creature attacks" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": controller}
                self.check_abilities(attacker_id, "ATTACKS", context)
                
        # Now check for abilities that trigger on any creature attacking
        for permanent_id in controller["battlefield"]:
            if permanent_id == attacker_id:
                continue  # Already checked
                
            permanent = gs._safe_get_card(permanent_id)
            if not permanent or not hasattr(permanent, 'oracle_text'):
                continue
                
            oracle_text = permanent.oracle_text.lower()
            
            # Check for "when/whenever a creature attacks" triggers
            if "when a creature attacks" in oracle_text or "whenever a creature attacks" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": controller}
                self.check_abilities(permanent_id, "CREATURE_ATTACKS", context)
                
        # Check opponent's triggers for "when/whenever a creature attacks you"
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        for permanent_id in opponent["battlefield"]:
            permanent = gs._safe_get_card(permanent_id)
            if not permanent or not hasattr(permanent, 'oracle_text'):
                continue
                
            oracle_text = permanent.oracle_text.lower()
            
            # Check for "when/whenever a creature attacks you" triggers
            if "when a creature attacks you" in oracle_text or "whenever a creature attacks you" in oracle_text:
                # Parse the ability and add to triggered abilities
                context = {"attacker_id": attacker_id, "controller": opponent}
                self.check_abilities(permanent_id, "CREATURE_ATTACKS_YOU", context)
