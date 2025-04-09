import logging
from .ability_types import Ability, ActivatedAbility, TriggeredAbility, StaticAbility, KeywordAbility, ManaAbility, AbilityEffect
import re
from collections import defaultdict
from .card import Card
from .keyword_effects import KeywordEffects
from .ability_utils import EffectFactory

class AbilityHandler:
    """Handles card abilities and special effects"""
    

    def __init__(self, game_state=None):
        self.game_state = game_state
        self.registered_abilities = {} # {card_id: [Ability, ...]}
        self.active_triggers = [] # Stores (Ability, controller) tuples to be processed
        self.targeting_system = None # Initialize targeting system reference

        if game_state is not None:
            self._initialize_abilities()
            # Initialize TargetingSystem here after GameState is available
            try:
                 # Make sure TargetingSystem is imported correctly
                 from .ability_handler import TargetingSystem
                 self.targeting_system = TargetingSystem(game_state)
                 # Link it back to the game_state if necessary
                 if not hasattr(game_state, 'targeting_system'):
                      game_state.targeting_system = self.targeting_system
            except ImportError as e:
                 logging.error(f"Could not import TargetingSystem: {e}")
            except Exception as e:
                 logging.error(f"Error initializing TargetingSystem: {e}")


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
        """Creates the appropriate Ability object for a given keyword. Now handles parameters."""
        keyword_lower = keyword_name.lower()
        full_text = (full_keyword_text or keyword_name).lower() # Use full text if provided for parameter parsing

        # Check if this exact keyword (considering parameters potentially) is already added
        # This requires KeywordAbility to store its parameters if applicable
        # For now, using simple name check.
        if any(isinstance(a, (KeywordAbility, StaticAbility, TriggeredAbility, ActivatedAbility)) and getattr(a, 'keyword', None) == keyword_lower for a in abilities_list):
             return # Avoid duplicates based on simple name match

        # Helper to parse value like "Keyword N"
        def parse_value(text, keyword):
             match = re.search(rf"{keyword}\s+(\d+)", text)
             return int(match.group(1)) if match else 1 # Default to 1 if no number

        # Static Combat Keywords -> StaticAbility granting the keyword
        static_combat = ["flying", "first strike", "double strike", "trample", "vigilance", "haste", "menace", "reach", "defender", "indestructible", "hexproof", "shroud", "unblockable", "fear", "intimidate", "shadow", "horsemanship", "flanking", "banding", "decayed", "phasing"]
        if keyword_lower in static_combat:
             ability_effect_text = f"This permanent has {full_text}."
             abilities_list.append(StaticAbility(card_id, ability_effect_text, ability_effect_text))
             setattr(abilities_list[-1], 'keyword', keyword_lower) # Add keyword attr for tracking
             return

        # Other Static Keywords -> StaticAbility + potentially specific layer logic registration
        if keyword_lower in ["lifelink", "deathtouch", "changeling", "devoid", "protection", "ward"]:
             ability_effect_text = f"This permanent has {full_text}."
             # Need to pass parameters like "protection from red" or "ward {2}"
             # The StaticAbility object itself might store this parameter if needed,
             # or the LayerSystem registration parses it from the effect_text.
             abilities_list.append(StaticAbility(card_id, ability_effect_text, ability_effect_text))
             setattr(abilities_list[-1], 'keyword', keyword_lower) # Add keyword attr for tracking
             return

        # Triggered Keywords -> TriggeredAbility
        triggered_map = {
             "prowess": ("whenever you cast a noncreature spell", "this creature gets +1/+1 until end of turn"),
             "cascade": ("when you cast this spell", "exile cards from the top of your library until you exile a nonland card that costs less. you may cast it without paying its mana cost."),
             "storm": ("when you cast this spell", "copy it for each spell cast before it this turn."),
             "riot": ("this permanent enters the battlefield", "choose haste or a +1/+1 counter"), # Needs choice handling
             "enrage": ("whenever this creature is dealt damage", "{effect_from_oracle}"), # Needs effect parsing
             "afflict": ("whenever this creature becomes blocked", "defending player loses N life"), # Needs N parsing
             "mentor": ("whenever this creature attacks", "put a +1/+1 counter on target attacking creature with lesser power."), # Needs targeting
             "afterlife": ("when this permanent dies", "create N 1/1 white and black Spirit creature tokens with flying."), # Needs N parsing
             "annihilator": ("whenever this creature attacks", "defending player sacrifices N permanents."), # Needs N parsing
             "bloodthirst": ("this creature enters the battlefield", "if an opponent was dealt damage this turn, it enters with N +1/+1 counters."), # Needs N parsing & condition check
             "bushido": ("whenever this creature blocks or becomes blocked", "it gets +N/+N until end of turn."), # Needs N parsing
             "evolve": ("whenever a creature enters the battlefield under your control", "if that creature has greater power or toughness than this creature, put a +1/+1 counter on this creature."),
             "fabricate": ("when this permanent enters the battlefield", "put N +1/+1 counters on it or create N 1/1 colorless Servo artifact creature tokens."), # Needs N parsing & choice
             "fading": ("this permanent enters the battlefield", "it enters with N fade counters on it. at the beginning of your upkeep, remove a fade counter. if you can't, sacrifice it."), # Needs N parsing & upkeep trigger
             "flanking": ("whenever a creature without flanking blocks this creature", "the blocking creature gets -1/-1 until end of turn."),
             "gravestorm": ("when you cast this spell", "copy it for each permanent put into a graveyard this turn."),
             "haunt": ("when this permanent dies", "exile it haunting target creature."), # Needs effect on haunted creature death
             "ingest": ("whenever this creature deals combat damage to a player", "that player exiles the top card of their library."),
             "infect": ("this deals damage", "deals damage to creatures in the form of -1/-1 counters and players in the form of poison counters."), # Static effect + trigger interpretation
             "modular": ("this enters the battlefield", "with N +1/+1 counters. when it dies, you may put its +1/+1 counters on target artifact creature."), # Needs N parsing & death trigger
             "persist": ("when this permanent dies", "if it had no -1/-1 counters, return it with a -1/-1 counter."),
             "poisonous": ("whenever this creature deals combat damage to a player", "that player gets N poison counters."), # Needs N parsing
             "rampage": ("whenever this creature becomes blocked", "it gets +N/+N for each creature blocking it beyond the first."), # Needs N parsing
             "renown": ("whenever this creature deals combat damage to a player", "if it isn't renowned, put N +1/+1 counters on it and it becomes renowned."), # Needs N parsing & state tracking
             "ripple": ("when you cast this spell", "you may reveal the top N cards of your library. you may cast any revealed cards with the same name without paying their mana costs."), # Needs N parsing
             "soulshift": ("when this permanent dies", "you may return target spirit card with cmc N or less from your graveyard to hand."), # Needs N parsing
             "sunburst": ("this permanent enters the battlefield", "with a +1/+1 counter or charge counter for each color of mana spent to cast it."), # Needs mana spent tracking
             "training": ("whenever this creature attacks with another creature with greater power", "put a +1/+1 counter on this creature."),
             "undying": ("when this permanent dies", "if it had no +1/+1 counters, return it with a +1/+1 counter."),
             "vanishing": ("this permanent enters the battlefield", "with N time counters. at the beginning of your upkeep, remove a time counter. when the last is removed, sacrifice it."), # Needs N parsing & upkeep trigger
             "wither": ("this deals damage", "deals damage to creatures in the form of -1/-1 counters."), # Static effect + trigger interpretation
        }
        if keyword_lower in triggered_map:
             trigger, effect = triggered_map[keyword_lower]
             # Replace N or parse specific effects
             val = parse_value(full_text, keyword_lower)
             effect = effect.replace(" N ", f" {val} ") # Simple replacement
             # TODO: More complex effect parsing/parameterization needed for many keywords
             abilities_list.append(TriggeredAbility(card_id, trigger, effect, full_text))
             setattr(abilities_list[-1], 'keyword', keyword_lower)
             return

        # Activated Keywords -> ActivatedAbility
        activated_map = {
            "cycling": ("draw a card."),
            "equip": ("attach to target creature you control. activate only as a sorcery."),
            "fortify": ("attach to target land you control. activate only as a sorcery."),
            "unearth": ("return this card from your graveyard to the battlefield. it gains haste. exile it at the beginning of the next end step or if it would leave the battlefield. unearth only as a sorcery."),
            "flashback": ("you may cast this card from your graveyard for its flashback cost. then exile it."), # Cost is parsed, effect is rule modification
            "retrace": ("you may cast this card from your graveyard by discarding a land card in addition to paying its other costs."),
            "scavenge": ("exile this card from your graveyard: put a number of +1/+1 counters equal to this card's power on target creature. scavenge only as a sorcery."),
            "transfigure": ("sacrifice this creature: search your library for a creature card with the same cmc, put it onto the battlefield, then shuffle. activate only as a sorcery."),
            "transmute": ("discard this card: search your library for a card with the same cmc, reveal it, put it into your hand, then shuffle. activate only as a sorcery."),
            "auraswap": ("exchange this aura with an aura card in your hand."),
            "outlast": ("put a +1/+1 counter on this creature. activate only as a sorcery."),
            "recover": ("when a creature is put into your graveyard from the battlefield, you may pay {cost}. if you do, return this card from your graveyard to your hand."), # This is actually triggered! Needs fixing.
            "reinforce": ("discard this card: put n +1/+1 counters on target creature."), # Needs N parsing
            # Reconfigure handled via specific ActionHandler
        }
        cost_match = re.search(rf"{keyword_lower}(?:\s*(\d+))?\s*(?:—|-)?\s*(\{{\".*?\"\}})", full_text)
        cost_str = cost_match.group(2) if cost_match else None
        val_str = cost_match.group(1) if cost_match and cost_match.group(1) else None
        if not cost_str: # Try other cost formats like just number or ability words
             cost_match = re.search(rf"{keyword_lower}(?:\s*(\d+))?\s*(?:—|-)?\s*(\d+|[xX])", full_text)
             cost_str = f"{{{cost_match.group(2)}}}" if cost_match else "{0}" # Assume free if no cost found? Check rules.
             if not val_str and cost_match and cost_match.group(1): val_str = cost_match.group(1)

        val = int(val_str) if val_str and val_str.isdigit() else 1

        if keyword_lower in activated_map and cost_str:
             effect = activated_map[keyword_lower]
             effect = effect.replace(" n ", f" {val} ") # Simple replace
             # Need to handle cost/effect parameterization better
             abilities_list.append(ActivatedAbility(card_id, cost_str, effect, full_text))
             setattr(abilities_list[-1], 'keyword', keyword_lower)
             return

        # Rule Modifying / Cost Keywords - Register as StaticAbility for clarity, actual effect handled elsewhere
        rule_keywords = ["affinity", "convoke", "delve", "improvise", "bestow", "buyback", "entwine", "escape", "kicker", "madness", "overload", "splice", "surge", "split second", "suspend", "companion"]
        if keyword_lower in rule_keywords:
             ability_effect_text = f"This card has {full_text}."
             abilities_list.append(StaticAbility(card_id, ability_effect_text, ability_effect_text))
             setattr(abilities_list[-1], 'keyword', keyword_lower) # Add keyword attr for tracking
             return

        # Fallback for completely unparsed keywords (should be fewer now)
        logging.warning(f"Keyword '{keyword_lower}' (from '{full_text}') not fully mapped to specific ability type.")
        # Add as generic static grant
        ability_effect_text = f"This permanent has {full_text}."
        abilities_list.append(StaticAbility(card_id, ability_effect_text, ability_effect_text))
        setattr(abilities_list[-1], 'keyword', keyword_lower)
        
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

class TargetingSystem:
    """
    Enhanced system for handling targeting in Magic: The Gathering.
    Supports comprehensive restrictions, protection effects, and validates targets.
    """
    
    def __init__(self, game_state):
        self.game_state = game_state
    
    def get_valid_targets(self, card_id, controller, target_type=None):
            """
            Returns a list of valid targets for a card, based on its text and target type,
            using the unified _is_valid_target checker.

            Args:
                card_id: ID of the card doing the targeting
                controller: Player dictionary of the card's controller
                target_type: Optional specific target type to filter for

            Returns:
                dict: Dictionary of target types to lists of valid targets
            """
            gs = self.game_state
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return {}

            oracle_text = card.oracle_text.lower()
            opponent = gs.p2 if controller == gs.p1 else gs.p1

            valid_targets = {
                "creature": [], "player": [], "permanent": [], "spell": [],
                "land": [], "artifact": [], "enchantment": [], "planeswalker": [],
                "card": [], # For graveyard/exile etc.
                "ability": [], # For targeting abilities on stack
                "other": [] # Fallback
            }
            all_target_types = list(valid_targets.keys())

            # Parse targeting requirements from the oracle text
            target_requirements = self._parse_targeting_requirements(oracle_text)

            # If no requirements found but text has "target", add a generic requirement
            if not target_requirements and "target" in oracle_text:
                target_requirements.append({"type": "target"}) # Generic target

            # Filter requirements if a specific target type is requested
            if target_type:
                target_requirements = [req for req in target_requirements if req.get("type") == target_type or req.get("type") in ["any", "target"]]
                if not target_requirements: return {} # No matching requirement for requested type

            # Define potential target sources
            target_sources = [
                # Players
                ("p1", gs.p1, "player"),
                ("p2", gs.p2, "player"),
                # Battlefield
                *[(perm_id, gs.get_card_controller(perm_id), "battlefield") for player in [gs.p1, gs.p2] for perm_id in player.get("battlefield", [])],
                # Stack (Spells and Abilities)
                *[(item[1], item[2], "stack") for item in gs.stack if isinstance(item, tuple) and len(item) >= 3],
                # Graveyards
                *[(card_id, player, "graveyard") for player in [gs.p1, gs.p2] for card_id in player.get("graveyard", [])],
                # Exile
                *[(card_id, player, "exile") for player in [gs.p1, gs.p2] for card_id in player.get("exile", [])],
            ]

            processed_valid = defaultdict(set) # Use set to avoid duplicates

            # Check each requirement against potential targets
            for requirement in target_requirements:
                req_type = requirement.get("type", "target") # Use "target" as fallback
                required_zone = requirement.get("zone")

                for target_id, target_obj_or_owner, current_zone in target_sources:
                    # Skip if zone doesn't match (unless zone isn't specified in req)
                    if required_zone and current_zone != required_zone:
                        continue

                    target_object = None
                    target_owner = None

                    if current_zone == "player":
                        target_object = target_obj_or_owner # target_obj_or_owner is the player dict
                        target_owner = target_obj_or_owner # Player owns themselves? Or maybe None? Let's use the player.
                    elif current_zone == "stack":
                        target_object = None
                        # Find the actual stack item (tuple or Card) based on target_id
                        for item in gs.stack:
                            if isinstance(item, tuple) and len(item) >= 3 and item[1] == target_id:
                                target_object = item # The stack item tuple itself
                                target_owner = item[2] # Controller of the spell/ability
                                break
                            # Less common: stack item is just Card object?
                            # elif isinstance(item, Card) and item.card_id == target_id:
                            #      target_object = item; target_owner = ??? # Need controller info
                        if target_object is None: continue # Stack item not found correctly
                    elif current_zone in ["battlefield", "graveyard", "exile", "library"]:
                        target_object = gs._safe_get_card(target_id)
                        target_owner = target_obj_or_owner # target_obj_or_owner is the player dict
                    else:
                        continue # Unknown zone

                    if not target_object: continue # Could be player or Card or Stack Tuple

                    target_info = (target_object, target_owner, current_zone) # Pass tuple to checker

                    # Use the unified validation function
                    if self._is_valid_target(card_id, target_id, controller, target_info, requirement):
                        # Determine primary category for this target
                        primary_cat = "other"
                        actual_types = set()
                        if isinstance(target_object, Card):
                            actual_types.update(getattr(target_object, 'card_types', []))
                            if 'creature' in actual_types: primary_cat = 'creature'
                            elif 'land' in actual_types: primary_cat = 'land'
                            elif 'planeswalker' in actual_types: primary_cat = 'planeswalker'
                            elif 'artifact' in actual_types: primary_cat = 'artifact'
                            elif 'enchantment' in actual_types: primary_cat = 'enchantment'
                            elif current_zone == 'stack': primary_cat = 'spell'
                            elif current_zone == 'graveyard' or current_zone == 'exile': primary_cat = 'card'
                        elif current_zone == 'player': primary_cat = 'player'
                        elif current_zone == 'stack' and isinstance(target_object, tuple): primary_cat = 'ability' # Could be spell too

                        # If specific type requested, use that, otherwise use derived primary category
                        cat_to_add = target_type if target_type else primary_cat

                        # Ensure category exists and add target
                        if cat_to_add in valid_targets:
                            processed_valid[cat_to_add].add(target_id)
                        elif req_type in valid_targets: # Fallback to requirement type
                            processed_valid[req_type].add(target_id)
                        else: # Last resort: "other"
                            processed_valid["other"].add(target_id)

            # Convert sets back to lists for the final dictionary
            final_valid_targets = {cat: list(ids) for cat, ids in processed_valid.items() if ids}
            return final_valid_targets
        
    def _has_keyword_check(self, card, keyword):
        """Helper within TargetingSystem to check keywords using GS AbilityHandler."""
        gs = self.game_state
        if not card or not isinstance(card, Card): return False # Ensure it's a Card object
        card_id = getattr(card, 'card_id', None)
        if not card_id: return False

        # Prefer checking via AbilityHandler if available
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            registered_abilities = gs.ability_handler.registered_abilities.get(card_id, [])
            # Check StaticAbility grants or KeywordAbility instances
            if any(isinstance(ab, StaticAbility) and f"has {keyword}" in ab.effect for ab in registered_abilities): return True
            if any(isinstance(ab, KeywordAbility) and ab.keyword == keyword for ab in registered_abilities): return True
            # Check if the static grant was parsed correctly (alternative check)
            if any(isinstance(ab, StaticAbility) and ab.effect_text == f"This permanent has {keyword}." for ab in registered_abilities): return True


        # Fallback checks (less ideal as they don't respect layer system removals)
        # Check keywords array on the card object (reflects layers maybe?)
        if hasattr(card, 'keywords') and isinstance(card.keywords, list):
            try:
                idx = Card.ALL_KEYWORDS.index(keyword)
                if idx < len(card.keywords) and card.keywords[idx] == 1: return True
            except ValueError: pass
        # Last resort: Check oracle text directly
        elif hasattr(card, 'oracle_text') and keyword in getattr(card, 'oracle_text', '').lower():
             # Add specific checks for multi-word/variants if needed
             return True

        return False
    
    def resolve_targeting_for_ability(self, card_id, ability_text, controller):
        """
        Handle targeting for an ability using the unified targeting system.
        
        Args:
            card_id: ID of the card with the ability
            ability_text: Text of the ability requiring targets
            controller: Player controlling the ability
            
        Returns:
            dict: Selected targets or None if targeting failed
        """
        return self.resolve_targeting(card_id, controller, ability_text)
    
    def resolve_targeting_for_spell(self, spell_id, controller):
        """
        Handle targeting for a spell using the unified targeting system.
        
        Args:
            spell_id: ID of the spell requiring targets
            controller: Player casting the spell
            
        Returns:
            dict: Selected targets or None if targeting failed
        """
        return self.resolve_targeting(spell_id, controller)
    
    def _is_valid_target(self, source_id, target_id, caster, target_info, requirement):
        """Unified check for any target type."""
        gs = self.game_state
        target_type = requirement.get("type")
        target_obj, target_owner, target_zone = target_info # Expect target_info=(obj, owner, zone)

        if not target_obj: return False

        # 1. Zone Check
        req_zone = requirement.get("zone")
        if req_zone and target_zone != req_zone: return False
        if not req_zone and target_zone not in ["battlefield", "stack", "player"]: # Default targetable zones
            # Check if the type allows targeting outside default zones
            if target_type == "card" and target_zone not in ["graveyard", "exile", "library"]: return False
            # Other types usually target battlefield/stack/players unless zone specified
            elif target_type != "card": return False


        # 2. Type Check
        actual_types = set()
        if isinstance(target_obj, dict) and target_id in ["p1", "p2"]: # Player target
            actual_types.add("player")
            # Also check owner relationship for player targets
            if requirement.get("opponent_only") and target_obj == caster: return False
            if requirement.get("controller_is_caster") and target_obj != caster: return False # Target self only
        elif isinstance(target_obj, Card): # Card object
            actual_types.update(getattr(target_obj, 'card_types', []))
            actual_types.update(getattr(target_obj, 'subtypes', []))
        elif isinstance(target_obj, tuple): # Stack item (Ability/Trigger)
             item_type = target_obj[0]
             if item_type == "ABILITY": actual_types.add("ability")
             elif item_type == "TRIGGER": actual_types.add("ability"); actual_types.add("triggered") # Allow target triggered ability

        # Check against required type
        valid_type = False
        if target_type == "target": valid_type = True # Generic "target" - skip specific type check initially
        elif target_type == "any": # Creature, Player, Planeswalker
             valid_type = any(t in actual_types for t in ["creature", "player", "planeswalker"])
        elif target_type == "card" and isinstance(target_obj, Card): valid_type = True # Targeting a card in specific zone
        elif target_type in actual_types: valid_type = True
        elif target_type == "permanent" and any(t in actual_types for t in ["creature", "artifact", "enchantment", "land", "planeswalker"]): valid_type = True
        elif target_type == "spell" and target_zone == "stack" and isinstance(target_obj, Card): valid_type = True # Targeting spell on stack

        if not valid_type: return False


        # 3. Protection / Hexproof / Shroud / Ward (Only for permanents, players, spells)
        if target_zone in ["battlefield", "stack", "player"]:
             source_card = gs._safe_get_card(source_id)
             if isinstance(target_obj, dict) and target_id in ["p1","p2"]: # Player
                  # TODO: Add player protection checks (e.g., Leyline of Sanctity)
                  pass
             elif isinstance(target_obj, Card): # Permanent or Spell
                 # Protection
                 if self._has_protection_from(target_obj, source_card, target_owner, caster): return False
                 # Hexproof (if targeted by opponent)
                 if caster != target_owner and self._has_keyword_check(target_obj, "hexproof"): return False
                 # Shroud (if targeted by anyone)
                 if self._has_keyword_check(target_obj, "shroud"): return False
                 # Ward (Check handled separately - involves paying cost)


        # 4. Specific Requirement Checks (applies mostly to battlefield permanents)
        if target_zone == "battlefield" and isinstance(target_obj, Card):
            # Owner/Controller
            if requirement.get("controller_is_caster") and target_owner != caster: return False
            if requirement.get("controller_is_opponent") and target_owner == caster: return False

            # Exclusions
            if requirement.get("exclude_land") and 'land' in actual_types: return False
            if requirement.get("exclude_creature") and 'creature' in actual_types: return False
            if requirement.get("exclude_color") and self._has_color(target_obj, requirement["exclude_color"]): return False

            # Inclusions
            if requirement.get("must_be_artifact") and 'artifact' not in actual_types: return False
            if requirement.get("must_be_aura") and 'aura' not in actual_types: return False
            if requirement.get("must_be_basic") and 'basic' not in getattr(target_obj,'type_line',''): return False
            if requirement.get("must_be_nonbasic") and 'basic' in getattr(target_obj,'type_line',''): return False

            # State
            if requirement.get("must_be_tapped") and target_id not in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_untapped") and target_id in target_owner.get("tapped_permanents", set()): return False
            if requirement.get("must_be_attacking") and target_id not in getattr(gs, 'current_attackers', []): return False
            # Note: Blocking state needs better tracking than just the current assignments dict
            # if requirement.get("must_be_blocking") and not is_blocking(target_id): return False
            if requirement.get("must_be_face_down") and not getattr(target_obj, 'face_down', False): return False

            # Color Restriction
            colors_req = requirement.get("color_restriction", [])
            if colors_req:
                if not any(self._has_color(target_obj, color) for color in colors_req): return False
                if "multicolored" in colors_req and sum(getattr(target_obj,'colors',[0]*5)) <= 1: return False
                if "colorless" in colors_req and sum(getattr(target_obj,'colors',[0]*5)) > 0: return False

            # Stat Restrictions
            if "power_restriction" in requirement:
                pr = requirement["power_restriction"]
                power = getattr(target_obj, 'power', None)
                if power is None: return False
                if pr["comparison"] == "greater" and not power >= pr["value"]: return False
                if pr["comparison"] == "less" and not power <= pr["value"]: return False
                if pr["comparison"] == "exactly" and not power == pr["value"]: return False
            if "toughness_restriction" in requirement:
                tr = requirement["toughness_restriction"]
                toughness = getattr(target_obj, 'toughness', None)
                if toughness is None: return False
                if tr["comparison"] == "greater" and not toughness >= tr["value"]: return False
                if tr["comparison"] == "less" and not toughness <= tr["value"]: return False
                if tr["comparison"] == "exactly" and not toughness == tr["value"]: return False
            if "mana value_restriction" in requirement:
                cmcr = requirement["mana value_restriction"]
                cmc = getattr(target_obj, 'cmc', None)
                if cmc is None: return False
                if cmcr["comparison"] == "greater" and not cmc >= cmcr["value"]: return False
                if cmcr["comparison"] == "less" and not cmc <= cmcr["value"]: return False
                if cmcr["comparison"] == "exactly" and not cmc == cmcr["value"]: return False

            # Subtype Restriction
            if "subtype_restriction" in requirement:
                if requirement["subtype_restriction"] not in actual_types: return False

        # 5. Spell/Ability Specific Checks
        if target_zone == "stack":
             source_card = gs._safe_get_card(source_id)
             if isinstance(target_obj, Card): # Spell target
                 # Can't be countered? (Only if source is a counter)
                 if "counter target spell" in getattr(source_card, 'oracle_text', '').lower():
                     if "can't be countered" in getattr(target_obj, 'oracle_text', '').lower(): return False
                 # Spell Type
                 st_req = requirement.get("spell_type_restriction")
                 if st_req == "instant" and 'instant' not in actual_types: return False
                 if st_req == "sorcery" and 'sorcery' not in actual_types: return False
                 if st_req == "creature" and 'creature' not in actual_types: return False
                 if st_req == "noncreature" and 'creature' in actual_types: return False
             elif isinstance(target_obj, tuple): # Ability target
                 ab_req = requirement.get("ability_type_restriction")
                 item_type = target_obj[0]
                 if ab_req == "activated" and item_type != "ABILITY": return False
                 if ab_req == "triggered" and item_type != "TRIGGER": return False


        return True # All checks passed
    
    def _has_color(self, card, color_name):
        """Check if a card has a specific color."""
        if not card or not hasattr(card, 'colors') or len(getattr(card,'colors',[])) != 5: return False
        color_index_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        if color_name not in color_index_map: return False
        return card.colors[color_index_map[color_name]] == 1
    
    def _parse_targeting_requirements(self, oracle_text):
        """Parse targeting requirements from oracle text with comprehensive rules."""
        requirements = []
        oracle_text = oracle_text.lower()
        
        # Pattern to find "target X" phrases, excluding nested clauses
        # Matches "target [adjectives] type [restrictions]"
        target_pattern = r"target\s+((?:(?:[a-z\-]+)\s+)*?)?([a-z]+)\s*((?:(?:with|of|that)\s+[^,\.;\(]+?|you control|an opponent controls|you don\'t control)*)"

        matches = re.finditer(target_pattern, oracle_text)

        for match in matches:
            req = {"type": match.group(2).strip()} # Basic type (creature, player, etc.)
            adjectives = match.group(1).strip().split() if match.group(1) else []
            restrictions = match.group(3).strip()

            # ---- Map Type ----
            type_map = {
                "creature": "creature", "player": "player", "opponent": "player", "permanent": "permanent",
                "spell": "spell", "ability": "ability", "land": "land", "artifact": "artifact",
                "enchantment": "enchantment", "planeswalker": "planeswalker", "card": "card", # General card (often in GY/Exile)
                "instant": "spell", "sorcery": "spell", "aura": "enchantment",
                # Add more specific types if needed
            }
            req["type"] = type_map.get(req["type"], req["type"]) # Normalize type

            # ---- Process Adjectives & Restrictions ----
            # Owner/Controller
            if "you control" in restrictions: req["controller_is_caster"] = True
            elif "an opponent controls" in restrictions or "you don't control" in restrictions: req["controller_is_opponent"] = True
            elif "target opponent" in oracle_text: req["opponent_only"] = True # Different phrasing

            # State/Status
            if "tapped" in adjectives: req["must_be_tapped"] = True
            if "untapped" in adjectives: req["must_be_untapped"] = True
            if "attacking" in adjectives: req["must_be_attacking"] = True
            if "blocking" in adjectives: req["must_be_blocking"] = True
            if "face-down" in adjectives or "face down" in restrictions: req["must_be_face_down"] = True

            # Card Type / Supertype / Subtype Restrictions
            if "nonland" in adjectives: req["exclude_land"] = True
            if "noncreature" in adjectives: req["exclude_creature"] = True
            if "nonblack" in adjectives: req["exclude_color"] = 'black'
            # ... add more non-X types

            if "basic" in adjectives and req["type"] == "land": req["must_be_basic"] = True
            if "nonbasic" in adjectives and req["type"] == "land": req["must_be_nonbasic"] = True

            if "artifact creature" in match.group(0): req["must_be_artifact_creature"] = True
            elif "artifact" in adjectives and req["type"]=="creature": req["must_be_artifact"] = True # Adj before type
            elif "artifact" in adjectives and req["type"]=="permanent": req["must_be_artifact"] = True

            if "aura" in adjectives and req["type"]=="enchantment": req["must_be_aura"] = True # Check Aura specifically

            # Color Restrictions (from adjectives or restrictions)
            colors = {"white", "blue", "black", "red", "green", "colorless", "multicolored"}
            found_colors = colors.intersection(set(adjectives)) or colors.intersection(set(restrictions.split()))
            if found_colors: req["color_restriction"] = list(found_colors)

            # Power/Toughness/CMC Restrictions (from restrictions)
            pt_cmc_pattern = r"(?:with|of)\s+(power|toughness|mana value)\s+(\d+)\s+(or greater|or less|exactly)"
            pt_match = re.search(pt_cmc_pattern, restrictions)
            if pt_match:
                 stat, value, comparison = pt_match.groups()
                 req[f"{stat}_restriction"] = {"comparison": comparison.replace("or ","").strip(), "value": int(value)}

            # Zone restrictions (usually implied by context, but check)
            if "in a graveyard" in restrictions: req["zone"] = "graveyard"; req["type"]="card" # Override type
            elif "in exile" in restrictions: req["zone"] = "exile"; req["type"]="card"
            elif "on the stack" in restrictions: req["zone"] = "stack" # Type should be spell/ability

            # Spell/Ability type restrictions
            if req["type"] == "spell":
                 if "instant" in adjectives: req["spell_type_restriction"] = "instant"
                 elif "sorcery" in adjectives: req["spell_type_restriction"] = "sorcery"
                 elif "creature" in adjectives: req["spell_type_restriction"] = "creature"
                 elif "noncreature" in adjectives: req["spell_type_restriction"] = "noncreature"
                 # ... add others
            elif req["type"] == "ability":
                if "activated" in adjectives: req["ability_type_restriction"] = "activated"
                elif "triggered" in adjectives: req["ability_type_restriction"] = "triggered"

            # Specific subtypes
            # Look for "target Goblin creature", "target Island land" etc.
            subtype_match = re.search(r"target\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:creature|land|artifact|etc)", match.group(0))
            if subtype_match:
                potential_subtype = subtype_match.group(1).strip()
                # TODO: Check if potential_subtype is a known subtype for the target type
                req["subtype_restriction"] = potential_subtype

            requirements.append(req)

        # Special cases not matching the main pattern
        if "any target" in oracle_text:
             requirements.append({"type": "any"}) # Any target includes creatures, players, planeswalkers

        if not requirements and "target" in oracle_text:
             # Fallback if "target" exists but pattern failed
             requirements.append({"type": "target"})

        # Refine types based on restrictions
        for req in requirements:
            if req.get("must_be_artifact_creature"): req["type"] = "creature"; req["must_be_artifact"]=True
            if req.get("must_be_aura"): req["type"] = "enchantment"
            if req.get("type") == "opponent": req["type"] = "player"; req["opponent_only"] = True
            if req.get("type") == "card": # Refine card targets
                if req.get("zone") == "graveyard": pass # Okay
                elif req.get("zone") == "exile": pass # Okay
                else: req["zone"] = "graveyard" # Default to GY if zone unspecified for 'card'

        return requirements

    

    def _has_protection_from(self, target_card, source_card, target_owner, source_controller):
        """Robust protection check using _has_keyword_check."""
        if not target_card or not source_card: return False

        # Check specific "protection from X" grants first
        protection_details = None
        card_id = getattr(target_card, 'card_id', None)
        if card_id and hasattr(self.game_state, 'ability_handler'):
             abilities = self.game_state.ability_handler.registered_abilities.get(card_id, [])
             for ab in abilities:
                  if isinstance(ab, StaticAbility) and "protection from" in ab.effect:
                       protection_details = ab.effect.split("protection from", 1)[-1].strip().lower()
                       break # Found protection grant

        if protection_details:
            # Perform checks based on protection_details against source_card
            source_colors = getattr(source_card, 'colors', [0]*5)
            source_types = getattr(source_card, 'card_types', [])
            source_subtypes = getattr(source_card, 'subtypes', [])

            if protection_details == "everything": return True
            if protection_details == "white" and source_colors[0]: return True
            if protection_details == "blue" and source_colors[1]: return True
            if protection_details == "black" and source_colors[2]: return True
            if protection_details == "red" and source_colors[3]: return True
            if protection_details == "green" and source_colors[4]: return True
            if protection_details == "all colors" and any(source_colors): return True
            if protection_details == "colorless" and not any(source_colors): return True
            if protection_details == "multicolored" and sum(source_colors) > 1: return True
            if protection_details == "monocolored" and sum(source_colors) == 1: return True
            if protection_details == "creatures" and "creature" in source_types: return True
            if protection_details == "artifacts" and "artifact" in source_types: return True
            if protection_details == "enchantments" and "enchantment" in source_types: return True
            if protection_details == "planeswalkers" and "planeswalker" in source_types: return True
            if protection_details == "instants" and "instant" in source_types: return True
            if protection_details == "sorceries" and "sorcery" in source_types: return True
            if protection_details == "opponent" and target_owner != source_controller: return True # Opponent check
            # Check specific subtypes
            if protection_details in source_subtypes: return True
            # Check specific named card? Needs name comparison.
            if protection_details == getattr(source_card, 'name', '').lower(): return True

        # If no specific grant found via abilities, can fallback to oracle text check if needed
        # But prefer ability checks as they reflect current game state better potentially

        return False
    
    def _has_hexproof(self, card):
        """Robust hexproof check using _has_keyword_check."""
        return self._has_keyword_check(card, "hexproof")
    
    def _has_shroud(self, card):
        """Check if card has shroud."""
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        return "shroud" in card.oracle_text.lower()
    
    def resolve_targeting(self, source_id, controller, effect_text=None, target_types=None):
        """
        Unified method to resolve targeting for both spells and abilities.
        
        Args:
            source_id: ID of the spell or ability source
            controller: Player who controls the source
            effect_text: Text of the effect requiring targets
            target_types: Specific types of targets to find
            
        Returns:
            dict: Selected targets or None if targeting failed
        """
        gs = self.game_state
        source_card = gs._safe_get_card(source_id)
        if not source_card:
            return None
            
        # Use effect_text if provided, otherwise try to get it from the card
        text_to_parse = effect_text
        if not text_to_parse and hasattr(source_card, 'oracle_text'):
            text_to_parse = source_card.oracle_text
            
        if not text_to_parse:
            return None
            
        # Get valid targets
        valid_targets = self.get_valid_targets(source_id, controller, target_types)
        
        # If no valid targets, targeting fails
        if not valid_targets or not any(valid_targets.values()):
            return None
        
        # Determine target requirements from the text
        target_requirements = self._parse_targeting_requirements(text_to_parse.lower())
        
        # Extract category names from the target requirements
        target_categories = [req.get("type", "other") for req in target_requirements]
        
        # Get AI selected targets based on effect type and context
        selected_targets = self._select_targets_by_strategy(
            source_card, valid_targets, len(target_requirements), target_categories, controller)
        
        if selected_targets and any(selected_targets.values()):
            return selected_targets
        else:
            return None
    
    def validate_targets(self, card_id, targets, controller):
        """
        Validate if the selected targets are legal for the card.
        
        Args:
            card_id: ID of the card doing the targeting
            targets: Dictionary of target categories to target IDs
            controller: Player dictionary of the card's controller
            
        Returns:
            bool: Whether the targets are valid
        """
        valid_targets = self.get_valid_targets(card_id, controller)
        
        # Check if all targets are valid
        for category, target_list in targets.items():
            if category not in valid_targets:
                return False
                
            for target in target_list:
                if target not in valid_targets[category]:
                    return False
                    
        return True
    
    def _select_targets_by_strategy(self, card, valid_targets, target_count, target_categories, controller):
        """
        Select targets strategically based on card type and effect.
        
        Args:
            card: The card requiring targets
            valid_targets: Dictionary of valid targets by category
            target_count: Number of targets required
            target_categories: Categories of targets needed
            controller: Player controlling the effect
            
        Returns:
            dict: Selected targets by category
        """
        gs = self.game_state
        selected_targets = {}
        
        # Determine if this is a beneficial or harmful effect
        is_beneficial = self._is_beneficial_effect(card.oracle_text.lower() if hasattr(card, 'oracle_text') else "")
        
        # Get opponent
        opponent = gs.p2 if controller == gs.p1 else gs.p1
        
        # Select targets for each required category
        for category in target_categories:
            if category not in valid_targets or not valid_targets[category]:
                continue
                
            # How many targets we still need
            remaining_count = target_count - sum(len(targets) for targets in selected_targets.values())
            if remaining_count <= 0:
                break
                
            # Get targets for this category
            category_targets = valid_targets[category]
            selected_for_category = []
            
            # Strategy depends on category and whether effect is beneficial
            if category == "creatures":
                if is_beneficial:
                    # For beneficial effects, target own creatures
                    own_creatures = [cid for cid in category_targets if cid in controller["battlefield"]]
                    
                    # Sort by most valuable (highest power/toughness)
                    own_creatures.sort(
                        key=lambda cid: (
                            getattr(gs._safe_get_card(cid), 'power', 0) +
                            getattr(gs._safe_get_card(cid), 'toughness', 0)
                        ),
                        reverse=True
                    )
                    
                    selected_for_category = own_creatures[:remaining_count]
                else:
                    # For harmful effects, target opponent creatures
                    opp_creatures = [cid for cid in category_targets if cid in opponent["battlefield"]]
                    
                    # Sort by most threatening (highest power/toughness)
                    opp_creatures.sort(
                        key=lambda cid: (
                            getattr(gs._safe_get_card(cid), 'power', 0) +
                            getattr(gs._safe_get_card(cid), 'toughness', 0)
                        ),
                        reverse=True
                    )
                    
                    selected_for_category = opp_creatures[:remaining_count]
                    
            elif category == "players":
                if is_beneficial:
                    # Target self for beneficial effects
                    if "p1" in category_targets and controller == gs.p1:
                        selected_for_category = ["p1"]
                    elif "p2" in category_targets and controller == gs.p2:
                        selected_for_category = ["p2"]
                else:
                    # Target opponent for harmful effects
                    if "p1" in category_targets and controller != gs.p1:
                        selected_for_category = ["p1"]
                    elif "p2" in category_targets and controller != gs.p2:
                        selected_for_category = ["p2"]
            
            elif category == "spells":
                # For counterspells, target opponent's spells
                opponent_spells = []
                for spell_id in category_targets:
                    for item in gs.stack:
                        if isinstance(item, tuple) and len(item) >= 3:
                            _, stack_id, spell_controller = item[:3]
                            if stack_id == spell_id and spell_controller != controller:
                                opponent_spells.append(spell_id)
                
                # Take the most recently cast spell (top of stack)
                if opponent_spells:
                    selected_for_category = [opponent_spells[0]]
            
            # Handle other categories similarly...
            elif category in ["artifacts", "enchantments", "lands", "planeswalkers", "permanents"]:
                if is_beneficial:
                    own_permanents = [cid for cid in category_targets if cid in controller["battlefield"]]
                    selected_for_category = own_permanents[:remaining_count]
                else:
                    opp_permanents = [cid for cid in category_targets if cid in opponent["battlefield"]]
                    selected_for_category = opp_permanents[:remaining_count]
            
            elif category == "graveyard":
                # For graveyard targeting, choose your own cards for beneficial effects
                if is_beneficial:
                    own_graveyard_cards = [cid for cid in category_targets if cid in controller["graveyard"]]
                    selected_for_category = own_graveyard_cards[:remaining_count]
                else:
                    opp_graveyard_cards = [cid for cid in category_targets if cid in opponent["graveyard"]]
                    selected_for_category = opp_graveyard_cards[:remaining_count]
            
            elif category == "exile":
                # Similar logic for exile zone
                own_exile_cards = [cid for cid in category_targets if cid in controller["exile"]]
                selected_for_category = own_exile_cards[:remaining_count]
                
            # Fallback for any other category - just take the first valid targets
            else:
                selected_for_category = category_targets[:remaining_count]
            
            # Add selected targets to result
            if selected_for_category:
                selected_targets[category] = selected_for_category
        
        return selected_targets

    def _is_beneficial_effect(self, oracle_text):
        """Determine if an effect is beneficial to the target."""
        from .ability_utils import is_beneficial_effect
        return is_beneficial_effect(oracle_text)
    
    def check_can_be_blocked(self, attacker_id, blocker_id):
        """
        Check if an attacker can be blocked by this blocker considering all restrictions.
        
        Args:
            attacker_id: The attacking creature ID
            blocker_id: The potential blocker creature ID
            
        Returns:
            bool: Whether the blocker can legally block the attacker
        """
        gs = self.game_state
        attacker = gs._safe_get_card(attacker_id)
        blocker = gs._safe_get_card(blocker_id)
        
        if not attacker or not blocker:
            return False
            
        # Get controller info
        attacker_controller = None
        blocker_controller = None
        
        for player in [gs.p1, gs.p2]:
            if attacker_id in player["battlefield"]:
                attacker_controller = player
            if blocker_id in player["battlefield"]:
                blocker_controller = player
                
        if not attacker_controller or not blocker_controller:
            return False
        
        # Check if blocker is tapped
        if blocker_id in blocker_controller.get("tapped_permanents", set()):
            return False
            
        # Check for protection
        if hasattr(attacker, 'oracle_text') and hasattr(blocker, 'oracle_text'):
            # Check if attacker has protection from blocker
            if self._has_protection_from(attacker, blocker, attacker_controller, blocker_controller):
                return False
                
            # Check if blocker has protection from attacker
            if self._has_protection_from(blocker, attacker, blocker_controller, attacker_controller):
                return False
        
        # Check for "can't be blocked" abilities
        if hasattr(attacker, 'oracle_text'):
            attacker_text = attacker.oracle_text.lower()
            
            # Absolute unblockable
            if "can't be blocked" in attacker_text and "except" not in attacker_text:
                return False
                
            # Menace (can't be blocked except by two or more creatures)
            if "menace" in attacker_text or "can't be blocked except by two or more creatures" in attacker_text:
                # Check if there are already blockers for this attacker
                existing_blockers = 0
                for blockers_list in gs.current_block_assignments.values():
                    if attacker_id in gs.current_block_assignments:
                        existing_blockers = len(gs.current_block_assignments[attacker_id])
                    
                if existing_blockers == 0:
                    return False  # First blocker can't block alone with menace
            
            # Flying (can only be blocked by creatures with flying or reach)
            if "flying" in attacker_text:
                has_flying = "flying" in blocker.oracle_text.lower() if hasattr(blocker, 'oracle_text') else False
                has_reach = "reach" in blocker.oracle_text.lower() if hasattr(blocker, 'oracle_text') else False
                
                if not has_flying and not has_reach:
                    return False
            
            # Check for other specific "can't be blocked except by" restrictions
            if "can't be blocked except by" in attacker_text:
                can_block = False
                
                # Common variations
                if "can't be blocked except by artifacts" in attacker_text:
                    if hasattr(blocker, 'card_types') and 'artifact' in blocker.card_types:
                        can_block = True
                        
                elif "can't be blocked except by walls" in attacker_text:
                    if hasattr(blocker, 'subtypes') and 'wall' in [s.lower() for s in blocker.subtypes]:
                        can_block = True
                
                # Return false if none of the exceptions apply
                if not can_block:
                    return False
        
        # Check for specific blocker restrictions
        if hasattr(blocker, 'oracle_text'):
            blocker_text = blocker.oracle_text.lower()
            
            # "Can't block" ability
            if "can't block" in blocker_text:
                return False
            
            # "Can block only" restrictions
            if "can block only" in blocker_text:
                can_block = False
                
                # Common variations
                if "can block only creatures with flying" in blocker_text:
                    if hasattr(attacker, 'oracle_text') and "flying" in attacker.oracle_text.lower():
                        can_block = True
                
                # Return false if none of the exceptions apply
                if not can_block:
                    return False
        
        # All checks pass, this blocker can block this attacker
        return True
    
    def check_must_attack(self, card_id):
        """
        Check if a creature must attack this turn.
        
        Args:
            card_id: The creature ID to check
            
        Returns:
            bool: Whether the creature must attack if able
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Check for "must attack each combat if able"
        if "must attack" in oracle_text and "if able" in oracle_text:
            return True
            
        # Check for "must attack [specific player/planeswalker] if able"
        if "must attack" in oracle_text and "if able" in oracle_text:
            # For simplicity, return true for any "must attack" restriction
            # In a full implementation, we would check specific attack requirements
            return True
            
        return False
    
    def check_must_block(self, card_id):
        """
        Check if a creature must block this turn.
        
        Args:
            card_id: The creature ID to check
            
        Returns:
            bool: Whether the creature must block if able
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        if not card or not hasattr(card, 'oracle_text'):
            return False
            
        oracle_text = card.oracle_text.lower()
        
        # Check for "must block if able"
        if "must block" in oracle_text and "if able" in oracle_text:
            return True
            
        # Check for "must block [specific creature] if able"
        if "must block" in oracle_text and "if able" in oracle_text:
            # For simplicity, return true for any "must block" restriction
            # In a full implementation, we would check specific block requirements
            return True
            
        return False