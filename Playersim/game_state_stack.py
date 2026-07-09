"""The stack: casting, targeting on resolution, and spell/ability resolution.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import logging
from .ability_utils import EffectFactory
import re
from .ability_types import TriggeredAbility


class GameStateStackMixin:
    """The stack: casting, targeting on resolution, and spell/ability resolution."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

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

    def trigger_ability(self, card_id, event_type, context=None):
        """Forward ability triggering to the AbilityHandler"""
        if hasattr(self, 'ability_handler') and self.ability_handler:
            # BUGFIX: AbilityHandler's method is check_abilities; the old name
            # raised AttributeError on EVERY trigger check, which step()'s broad
            # exception handling converted into 'error' game endings.
            return self.ability_handler.check_abilities(card_id, event_type, context)
        return []

    def add_to_stack(self, item_type, source_id, controller, context=None):
            """
            Add an item to the stack with context.
            Sets priority to the controller (Rule 117.3c).
            """
            if context is None: context = {}
            # Ensure source_id is valid
            card = self._safe_get_card(source_id)
            card_name = getattr(card, 'name', source_id) if card else source_id

            stack_item = (item_type, source_id, controller, context)
            self.stack.append(stack_item)
            logging.debug(f"Added to stack: {item_type} {card_name} ({source_id}) with context keys: {context.keys()}")

            # *** RULE 117.3c: The player who cast the spell/ability gets priority. ***
            # By default, after adding to stack, the game state should NOT set priority to None.
            # It should be the player who took the action.
            self.priority_player = controller

            # Reset pass count because the state has changed (stack is not empty/same)
            self.priority_pass_count = 0
            self.last_stack_size = len(self.stack)

            # Handling Phase transitions related to Special Choices
            if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                # If not already in priority phase (e.g. we were in Main Phase), enter it
                if self.phase != self.PHASE_PRIORITY:
                    self.previous_priority_phase = self.phase # Store where we came from
                    self.phase = self.PHASE_PRIORITY
                logging.debug(f"Stack changed, priority returned to {self.priority_player['name']}")
            else:
                # Still update stack size even if not resetting priority context
                logging.debug("Added to stack during special choice phase, priority maintained.")

    @staticmethod
    def _combine_cost_dicts(cost_dict1, cost_dict2):
        """Helper to combine two parsed mana cost dictionaries."""
        combined = cost_dict1.copy()
        for key, value in cost_dict2.items():
            combined[key] = combined.get(key, 0) + value
        return combined

    def cast_spell(self, card_id, player, context=None):
        """
        Cast a spell: Validate source/timing -> Determine Cost -> Pay Costs -> Move to Stack (or Enter Choice Phase) -> Set up Targeting/Choices.
        Handles regular casts, alternative costs (incl. Impending), additional costs (incl. Offspring), modal spells, and targeting setup.
        """
        if context is None: context = {}
        card = self._safe_get_card(card_id)
        if not card:
             logging.error(f"Cannot cast spell: Invalid card_id {card_id}")
             return False

        # --- 1. Validate Source Zone and Timing ---
        source_zone = context.get("source_zone", "hand") # Default source
        source_idx = context.get("source_idx")
        source_list = None
        card_in_source = False
        # ...(rest of source zone validation remains the same)...
        if source_zone == "command":
            source_list = player.get(source_zone)
            if isinstance(source_list, (list, set)) and card_id in source_list: card_in_source = True
        elif source_zone in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone"]: card_in_source = True; source_list = []
        else:
             source_list = player.get(source_zone)
             if source_list is not None:
                  if isinstance(source_list, (list, set)) and card_id in source_list:
                       card_in_source = True
                       if source_idx is None and isinstance(source_list, list):
                            try: source_idx = source_list.index(card_id)
                            except ValueError: card_in_source = False
                  elif isinstance(source_list, dict) and card_id in source_list: card_in_source = True

        if not card_in_source:
            logging.warning(f"Cannot cast {getattr(card,'name', card_id)}: Not found in {player['name']}'s {source_zone}.")
            return False
        if not self._can_cast_now(card_id, player):
             logging.warning(f"Cannot cast {getattr(card,'name', card_id)}: Invalid timing (Phase: {self._PHASE_NAMES.get(self.phase)}, Prio: {getattr(self.priority_player,'name','None')}, Stack:{len(self.stack)}).")
             return False

        # --- 2. Check for Modal Spell ---
        modal_modes, min_modes, max_modes = None, 0, 0
        is_modal_spell = False
        if self.ability_handler and hasattr(self.ability_handler, '_parse_modal_text'):
             modal_modes, min_modes, max_modes = self.ability_handler._parse_modal_text(getattr(card, 'oracle_text', ''))
             if modal_modes: is_modal_spell = True

        # --- 3. Determine Base Cost String ---
        cast_for_impending = context.get('cast_for_impending', False) # Check flag set by handler
        alt_cost_type = None # Assume no alt cost initially
        if cast_for_impending: alt_cost_type = 'impending' # Flag for cost modification checks
        elif context.get('use_alt_cost'): # Check for other generic alt cost flags
             alt_cost_type = context.get('use_alt_cost')

        base_cost_str = "" # Default empty
        final_cost_dict = {} # Store parsed/modified cost

        if cast_for_impending:
             impending_cost_str = getattr(card, 'impending_cost', None)
             if not impending_cost_str: return False
             base_cost_str = impending_cost_str
             # context['cast_for_impending'] = True # Already set by caller
        elif alt_cost_type: # Other alternative costs handled first
             final_cost_dict = self.mana_system.calculate_alternative_cost(card_id, player, alt_cost_type, context)
             if final_cost_dict is None: return False
        else: # Normal cost
            base_cost_str = getattr(card, 'mana_cost', '')
            # MDFC back-face casting (July 2026): use the BACK face's mana cost
            # when this cast is flagged as the back face. Previously the spell
            # path always used the front cost, so casting a spell MDFC's back
            # face charged the wrong amount.
            if context.get('cast_as_back_face') and hasattr(card, 'get_face_cost'):
                _back_cost = card.get_face_cost(1)
                base_cost_str = _back_cost if _back_cost is not None else base_cost_str
                context['effect_text'] = card.get_face_text(1)

        # --- 4. Calculate Final Cost (Mana & Non-Mana) ---
        # Parse base cost if applicable
        if base_cost_str and not alt_cost_type:
            final_cost_dict = self.mana_system.parse_mana_cost(base_cost_str)
        elif not alt_cost_type: # Handle cases with no base cost (like Suspend resolution?)
            final_cost_dict = {} # Start with empty dict

        # Add additional mana costs ONLY IF NOT using a fully replacing alternative cost
        # Check alt_cost_type (Impending is handled above, others might replace fully)
        apply_additional_costs = alt_cost_type is None # Apply only if no replacing alt cost

        if apply_additional_costs:
            pay_offspring = context.get('pay_offspring', False)
            if pay_offspring and getattr(card, 'is_offspring', False):
                offspring_cost_str = getattr(card, 'offspring_cost', None)
                if offspring_cost_str:
                    offspring_cost_dict = self.mana_system.parse_mana_cost(offspring_cost_str)
                    # *** FIXED: Use internal helper ***
                    final_cost_dict = self._combine_cost_dicts(final_cost_dict, offspring_cost_dict)
                    context['paid_offspring'] = True # Add final flag for ETB trigger check
            # Kicker
            if context.get('kicked'):
                kicker_cost_str = context.get('kicker_cost_to_pay')
                if kicker_cost_str:
                    kicker_cost_dict = self.mana_system.parse_mana_cost(kicker_cost_str)
                    # *** FIXED: Use internal helper ***
                    final_cost_dict = self._combine_cost_dicts(final_cost_dict, kicker_cost_dict)
                    context['actual_kicker_paid'] = kicker_cost_str
            # Escalate
            escalate_count = context.get('escalate_count', 0)
            if escalate_count > 0:
                escalate_cost_each_str = context.get('escalate_cost_each')
                if escalate_cost_each_str:
                    escalate_cost_each_dict = self.mana_system.parse_mana_cost(escalate_cost_each_str)
                    # Combine cost N times
                    for _ in range(escalate_count):
                        # *** FIXED: Use internal helper repeatedly ***
                        final_cost_dict = self._combine_cost_dicts(final_cost_dict, escalate_cost_each_dict)

        # Apply Generic Cost Modifiers LAST
        final_cost_dict = self.mana_system.apply_cost_modifiers(player, final_cost_dict, card_id, context)

        # --- Check Affordability & Targets ---
        # ...(rest of checks and logic remain largely the same, just ensure final_cost_dict is used)...
        additional_cost_info = context.get('additional_cost_info')
        can_pay_non_mana_add = True
        if context.get('pay_additional') and additional_cost_info:
             if not self.mana_system._can_pay_non_mana_cost(player, additional_cost_info, context):
                  can_pay_non_mana_add = False
                  logging.warning(f"Cannot cast {card.name}: Cannot meet non-mana additional cost.")
        if not can_pay_non_mana_add: return False

        # Check final mana affordability
        if not self.mana_system.can_pay_mana_cost(player, final_cost_dict, context):
            cost_str_log = self._format_mana_cost_for_logging(final_cost_dict, context.get('X', 0))
            logging.warning(f"Cannot cast {card.name}: Cannot afford final cost {cost_str_log}.")
            return False

        # Check if required targets exist (only for non-modal before paying cost)
        requires_target = False; num_targets = 0; up_to_N = False; total_valid_targets = 0
        if not is_modal_spell:
            oracle_text = getattr(card, 'oracle_text', '').lower()
            requires_target = "target" in oracle_text
            num_targets = getattr(card, 'num_targets', 1) if requires_target else 0
            up_to_N = "up to" in oracle_text
            if requires_target and num_targets > 0:
                 if self.targeting_system:
                      valid_targets_map = self.targeting_system.get_valid_targets(card_id, player)
                      total_valid_targets = sum(len(v) for v in valid_targets_map.values())
                      min_required = 0 if up_to_N else num_targets
                      if total_valid_targets < min_required:
                          logging.warning(f"Cannot cast {card.name}: Not enough valid targets available ({total_valid_targets}/{min_required} needed).")
                          return False
                 else:
                      logging.warning("Cannot check target availability: TargetingSystem missing.")

        # --- Costs Paid Here ---
        # 1. Pay Non-Mana Additional Costs FIRST
        if context.get('pay_additional') and additional_cost_info:
            if not self.mana_system._pay_non_mana_cost(player, additional_cost_info, context):
                logging.warning(f"Failed to pay non-mana additional cost for {card.name}.")
                return False

        # 2. Pay Final Mana Cost
        paid_mana_details = self.mana_system.pay_mana_cost_get_details(player, final_cost_dict, context)
        if paid_mana_details is None:
             logging.warning(f"Failed to pay final mana cost for {card.name}. Rolling back non-mana costs...")
             if context.get('pay_additional') and additional_cost_info:
                  self.mana_system._rollback_non_mana_cost(player, additional_cost_info, context)
             return False

        # --- Move Card from Source Zone ---
        removed = False
        source_list_live = player.get(source_zone)
        if source_list_live is not None:
             if isinstance(source_list_live, list) and source_idx is not None and 0 <= source_idx < len(source_list_live) and source_list_live[source_idx] == card_id:
                  source_list_live.pop(source_idx)
                  removed = True
             elif isinstance(source_list_live, (list, set)) and card_id in source_list_live:
                 if isinstance(source_list_live, list): source_list_live.remove(card_id)
                 elif isinstance(source_list_live, set): source_list_live.discard(card_id)
                 removed = True
        elif source_zone in ["stack_implicit", "library_implicit", "hand_implicit", "nonexistent_zone"]: removed = True

        if not removed:
             logging.error(f"CRITICAL: Could not remove {card.name} from {source_zone} after paying costs.")
             if paid_mana_details: self.mana_system.add_mana(player, paid_mana_details.get('spent_specific',{}))
             if context.get('pay_additional') and additional_cost_info: self.mana_system._rollback_non_mana_cost(player, additional_cost_info, context)
             return False
        if source_zone == "exile" and hasattr(self, "cards_castable_from_exile"):
             self.cards_castable_from_exile.discard(card_id)

        # --- Prepare FINAL stack context ---
        final_stack_context = context.copy()
        final_stack_context["source_zone"] = source_zone
        final_stack_context["final_paid_cost"] = final_cost_dict
        final_stack_context["final_paid_details"] = paid_mana_details
        final_stack_context["requires_target"] = requires_target
        final_stack_context["num_targets"] = num_targets
        final_stack_context.pop('pay_offspring', None) # Clear intent flag
        final_stack_context.pop('kicker_cost_to_pay', None)
        final_stack_context.pop('additional_cost_info', None)
        final_stack_context.pop('source_idx', None)

        # --- Modal Divergence / Add to Stack / Targeting Phase ---
        if is_modal_spell:
             # ...(modal logic remains the same)...
             logging.debug(f"Entering CHOICE phase for modal spell: {card.name}")
             if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                 self.previous_priority_phase = self.phase
             self.phase = self.PHASE_CHOOSE
             self.choice_context = {
                 'type': 'choose_mode', 'player': player, 'card_id': card_id,
                 'num_choices': len(modal_modes), 'min_required': min_modes, 'max_required': max_modes,
                 'available_modes': modal_modes, 'selected_modes': [],
                 'original_cast_context': final_stack_context.copy(), # Store state BEFORE mode choice
                 'resolved': False
             }
             self.priority_player = player; self.priority_pass_count = 0
             logging.info(f"Modal spell {card.name} cast. Waiting for mode choice.")
        else:
             self.add_to_stack("SPELL", card_id, player, final_stack_context)
             if requires_target and num_targets > 0:
                  # ...(targeting setup remains the same)...
                  logging.debug(f"{card.name} requires target(s). Entering TARGETING phase.")
                  if self.phase not in [self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                      self.previous_priority_phase = self.phase
                  self.phase = self.PHASE_TARGETING
                  self.targeting_context = {
                      "source_id": card_id, "controller": player,
                      "required_type": self._get_target_type_from_text(getattr(card,'oracle_text','')),
                      "required_count": num_targets, "min_targets": 0 if up_to_N else num_targets,
                      "max_targets": num_targets, "selected_targets": [],
                      "effect_text": getattr(card, 'oracle_text', ''),
                      "stack_info": { # Store info to update the correct stack item later
                            "item_type": "SPELL", "source_id": card_id, "controller": player,
                            "context": final_stack_context # The context added to the stack
                       }
                  }
                  self.priority_player = player; self.priority_pass_count = 0

             logging.info(f"Successfully cast spell: {card.name} ({card_id}) from {source_zone}")

        # --- Track Cast & Trigger ---
        # ...(tracking/trigger remains the same)...
        self.track_card_played(card_id, player_idx = 0 if player == self.p1 else 1)
        if not hasattr(self, 'spells_cast_this_turn'): self.spells_cast_this_turn = []
        self.spells_cast_this_turn.append((card_id, player, final_stack_context)) # Include context

        cast_trigger_context = {'cast_card_id': card_id, 'card_id': card_id, 'controller': player, **final_stack_context}
        self.trigger_ability(None, "CAST_SPELL", cast_trigger_context)
        if 'creature' in getattr(card, 'card_types',[]): self.trigger_ability(None, "CAST_CREATURE_SPELL", cast_trigger_context)
        elif 'instant' in getattr(card, 'card_types',[]) or 'sorcery' in getattr(card, 'card_types',[]): self.trigger_ability(None, "CAST_NONCREATURE_SPELL", cast_trigger_context)

        # Clear pending context if this cast matches it
        if getattr(self, 'pending_spell_context', None) and self.pending_spell_context.get('card_id') == card_id:
            self.pending_spell_context = None

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

    def play_land(self, card_id, controller, play_back_face=False):
            """
            Play a land card from hand to battlefield, respecting the one-land-per-turn rule.
            Handles MDFC (Modal Double-Faced Card) lands.
            
            Args:
                card_id: ID of the land card to play
                controller: Player dictionary of the player playing the land
                play_back_face: Boolean, if True, play the back face of an MDFC
                
            Returns:
                bool: Whether the land was successfully played
            """
            # Check if card exists in hand
            if card_id not in controller["hand"]:
                logging.warning(f"Land {card_id} not found in hand")
                return False
            
            # Check if the card is actually a land (checking the correct face)
            card = self._safe_get_card(card_id)
            if not card:
                logging.warning(f"Card {card_id} invalid")
                return False

            is_land = False
            # If playing back face, check back face type line
            if play_back_face:
                if hasattr(card, 'back_face') and card.back_face and 'land' in card.back_face.get('type_line', '').lower():
                    is_land = True
                else:
                    logging.debug(f"Play land failed: Back face of {card.name} is not a land.")
            # If playing front face, check normal type line
            else:
                if hasattr(card, 'type_line') and 'land' in card.type_line.lower():
                    is_land = True

            if not is_land:
                logging.warning(f"Card {card.name} (Back: {play_back_face}) is not a land")
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
            
            # Register back face status if applicable so the engine knows how to treat it on BF
            if play_back_face:
                if not hasattr(self, 'cast_as_back_face'):
                    self.cast_as_back_face = set()
                self.cast_as_back_face.add(card_id)

            # Prepare context for move_card
            move_context = {'play_back_face': play_back_face}

            # Move the land from hand to battlefield
            result = self.move_card(card_id, controller, "hand", controller, "battlefield", cause="land_play", context=move_context)
            
            if result:
                # Mark that player has played a land this turn
                controller["land_played"] = True
                
                # Track the land play for statistics
                player_idx = 0 if controller == self.p1 else 1
                self.track_card_played(card_id, player_idx)
                
                # Determine properties for logging and tapped check based on the played face
                card_name = card.name
                oracle_text = getattr(card, 'oracle_text', '').lower()
                
                if play_back_face and hasattr(card, 'back_face'):
                    card_name = card.back_face.get('name', card_name)
                    oracle_text = card.back_face.get('oracle_text', '').lower()

                logging.debug(f"Played land {card_name}")
                
                # Check if land enters tapped based on the specific face's text
                if "enters the battlefield tapped" in oracle_text:
                    if not hasattr(controller, "tapped_permanents"):
                        controller["tapped_permanents"] = set()
                    controller["tapped_permanents"].add(card_id)
                    logging.debug(f"Land {card_name} enters tapped")
            else:
                # If move failed, cleanup the back face registration
                if play_back_face and hasattr(self, 'cast_as_back_face') and card_id in self.cast_as_back_face:
                    self.cast_as_back_face.remove(card_id)
            
            return result

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

    def _flatten_target_ids(self, targets):
        """Flatten a target dict/list into ordered target ids."""
        flattened = []
        seen = set()

        def add_target(target_id):
            if target_id is None or target_id == "X":
                return
            try:
                hash(target_id)
            except TypeError:
                return
            if target_id not in seen:
                seen.add(target_id)
                flattened.append(target_id)

        if isinstance(targets, dict):
            for key, value in targets.items():
                if key == "X":
                    continue
                if isinstance(value, (list, tuple, set)):
                    for target_id in value:
                        add_target(target_id)
                else:
                    add_target(value)
        elif isinstance(targets, (list, tuple, set)):
            for target_id in targets:
                add_target(target_id)
        return flattened

    def _pay_ward_costs_for_targets(self, item_type, source_id, controller, targets, context=None):
        """Auto-pay ward costs for opposing ward permanents targeted by a stack item."""
        if context is None:
            context = {}
        target_ids = self._flatten_target_ids(targets)
        if not target_ids:
            return True

        paid_costs = context.setdefault("ward_costs_paid", [])
        checked_targets = set()
        for target_id in target_ids:
            if target_id in checked_targets or target_id in ["p1", "p2"]:
                continue
            checked_targets.add(target_id)
            target_card = self._safe_get_card(target_id)
            target_controller = self.get_card_controller(target_id)
            if not target_card or not target_controller or target_controller == controller:
                continue
            if not hasattr(self, 'check_keyword') or not self.check_keyword(target_id, "ward"):
                continue

            ward_costs = []
            if self.ability_handler and hasattr(self.ability_handler, 'get_ward_costs'):
                ward_costs = self.ability_handler.get_ward_costs(target_id)
            if not ward_costs:
                ward_costs = ["ward_generic"]

            for ward_cost in ward_costs:
                if not self._pay_single_ward_cost(controller, ward_cost, source_id, target_id, context):
                    context["countered_by_ward"] = True
                    context["unpaid_ward_cost"] = ward_cost
                    return False
                paid_costs.append({"target_id": target_id, "cost": ward_cost})
        return True

    def _pay_single_ward_cost(self, player, ward_cost, source_id, target_id, context):
        """Pay one ward cost. Supports mana costs and simple life-payment ward."""
        if ward_cost is None:
            return False
        cost_text = str(ward_cost).strip()
        if not cost_text or cost_text == "ward_generic":
            return False

        life_match = re.fullmatch(r"pay\s+(\d+)\s+life", cost_text, re.IGNORECASE)
        if life_match:
            amount = int(life_match.group(1))
            if player.get("life", 0) < amount:
                return False
            player["life"] -= amount
            return True

        if cost_text.isdigit():
            cost_text = f"{{{cost_text}}}"
        if "{" not in cost_text:
            logging.debug(f"Unsupported ward cost '{ward_cost}' on target {target_id}.")
            return False
        if not hasattr(self, 'mana_system') or not self.mana_system:
            return False

        parsed_cost = self.mana_system.parse_mana_cost(cost_text)
        ward_context = dict(context)
        ward_context.update({"card_id": source_id, "ward_target_id": target_id})
        if not self.mana_system.can_pay_mana_cost(player, parsed_cost, ward_context):
            return False
        return self.mana_system.pay_mana_cost_get_details(player, parsed_cost, ward_context) is not None

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
                elif not self._pay_ward_costs_for_targets(item_type, item_id, controller, validation_targets, context):
                    logging.info(f"Stack Item {item_type} {card_name} countered by unpaid ward.")
                    if item_type == "SPELL" and not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                        self.move_card(item_id, controller, "stack_implicit", controller, "graveyard", cause="ward_countered", context=context)
                    resolution_success = True # Countering by ward successfully finishes this stack item
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
            # BUGFIX: a crash mid-resolution used to delete the card from the game
            # entirely (already off the stack, never reaching any zone). Best-effort
            # recovery: a real card spell goes to its controller's graveyard.
            try:
                if (isinstance(top_item, tuple) and len(top_item) >= 3
                        and top_item[0] == "SPELL" and isinstance(top_item[1], (int, str))):
                    _spell_id = top_item[1]
                    _owner, _zone = self.find_card_location(_spell_id)
                    if _zone is None and isinstance(top_item[2], dict):
                        top_item[2].setdefault("graveyard", []).append(_spell_id)
                        logging.warning(f"Recovered lost spell {_spell_id} to graveyard after resolution error.")
            except Exception:
                pass
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
        """Resolve a spell with handling for modal spells based on context."""
        if context is None: context = {}
        spell = self._safe_get_card(spell_id)
        if not spell:
             logging.warning(f"Cannot resolve spell: card {spell_id} not found")
             # Don't move to graveyard if it didn't exist
             return False

        spell_name = getattr(spell, "name", f"Spell {spell_id}")
        logging.debug(f"Resolving spell: {spell_name}")

        # Check if countered (e.g., by a replacement effect during resolution?) - less common
        if context.get("countered"):
             logging.debug(f"Spell {spell_name} was countered - moving to graveyard")
             if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                  self.move_card(spell_id, controller, "stack_implicit", controller, "graveyard")
             return False # Resolution stopped

        # Determine spell type and base characteristics post-layers (layers shouldn't affect stack usually)
        card_types = getattr(spell, 'card_types', [])

        # --- MODAL SPELL RESOLUTION ---
        selected_modes_indices = context.get("selected_modes") # Get list of chosen indices
        if selected_modes_indices is not None: # Check specifically for None, empty list is valid (for "up to" maybe)
            logging.debug(f"Resolving modal spell {spell_name} with chosen modes: {selected_modes_indices}")
            all_modes_text, _, _ = self.ability_handler._parse_modal_text(getattr(spell, 'oracle_text', ''))

            if not all_modes_text:
                 logging.error(f"Failed to re-parse modes for resolving modal spell {spell_name}")
                 # Move to GY if non-permanent?
                 return False

            resolution_effects_applied = False
            for mode_idx in selected_modes_indices:
                if 0 <= mode_idx < len(all_modes_text):
                     mode_text = all_modes_text[mode_idx]
                     logging.debug(f"Applying mode {mode_idx}: '{mode_text}'")
                     # Create and apply effects for THIS mode's text
                     # Pass targets that were selected *for the whole spell* if available
                     # If modes have separate targets, targeting phase needs modification. Assume shared targets for now.
                     mode_targets = context.get("targets") # Targets selected before spell was put on stack (if any)
                     effects = EffectFactory.create_effects(mode_text, mode_targets, source_name=getattr(spell, 'name', None))
                     for effect_obj in effects:
                         if effect_obj.apply(self, spell_id, controller, mode_targets):
                              resolution_effects_applied = True
                else:
                     logging.warning(f"Invalid mode index {mode_idx} found in context for {spell_name}")

            # Move non-permanent modal spells to graveyard after applying effects
            if not any(t in card_types for t in ['creature', 'artifact', 'enchantment', 'planeswalker', 'land', 'battle']):
                if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                    self.move_card(spell_id, controller, "stack_implicit", controller, "graveyard")

            self.trigger_ability(spell_id, "SPELL_RESOLVED", {"controller": controller, **context})
            return resolution_effects_applied

        # --- NON-MODAL SPELL RESOLUTION ---
        else:
            # Handle different card types (calls helpers which use move_card)
            if 'creature' in card_types:
                 success = self._resolve_creature_spell(spell_id, controller, context)
            elif 'planeswalker' in card_types:
                 success = self._resolve_planeswalker_spell(spell_id, controller, context)
            elif any(t in card_types for t in ['artifact', 'enchantment', 'battle']):
                 success = self._resolve_permanent_spell(spell_id, controller, context)
            elif 'land' in card_types:
                 success = self._resolve_land_spell(spell_id, controller, context)
            elif any(t in card_types for t in ['instant', 'sorcery']):
                 success = self._resolve_instant_sorcery_spell(spell_id, controller, context)
            else:
                 logging.warning(f"Unknown card type for resolution: {card_types} on {spell_name}")
                 if not context.get("is_copy", False) and not context.get("skip_default_movement", False):
                     self.move_card(spell_id, controller, "stack_implicit", controller, "graveyard")
                 success = False # Unknown type failed resolution

            # Post-resolution SBAs are handled by the main loop
            return success

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
        # When cast as an Adventure, the card is a creature whose Adventure
        # half is an instant/sorcery -- honor the flag rather than rejecting
        # it on the creature type (July 2026 sweep).
        if not spell or not (any(t in getattr(spell, 'card_types', []) for t in valid_types)
                             or context.get('cast_as_adventure')):
             logging.warning(f"Attempted to resolve {spell_id} as instant/sorcery, but type is invalid.")
             if not context.get("is_copy", False): controller["graveyard"].append(spell_id)
             return False

        spell_name = getattr(spell, 'name', f"Spell {spell_id}")
        logging.debug(f"Resolving Instant/Sorcery: {spell_name}")

        # Apply effects using AbilityHandler or EffectFactory
        if hasattr(self, 'ability_handler'):
            # --- MODIFIED: Pass full context ---
            effects = EffectFactory.create_effects(getattr(spell, 'oracle_text', ''), context.get('targets'), source_name=getattr(spell, 'name', None))
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
        # --- Adventure (CR 715.3f, July 2026 sweep) ---
        # A spell cast as its Adventure half goes to EXILE, and the owner may
        # later cast the creature from exile. cast_as_adventure was set at
        # cast time but nothing read it here, so adventure spells went to the
        # graveyard and the creature half was lost forever.
        elif context.get('cast_as_adventure'):
            final_zone = "exile"
            if not hasattr(self, 'cards_castable_from_exile'):
                self.cards_castable_from_exile = set()
            self.cards_castable_from_exile.add(spell_id)
            logging.debug(f"{spell_name} exiled on Adventure; creature side castable from exile.")
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

    def get_stack_item_controller(self, stack_item_id):
        """Find the controller of a spell or ability on the stack."""
        for item in self.stack:
            if isinstance(item, tuple) and len(item) >= 3 and item[1] == stack_item_id:
                return item[2] # The controller is the 3rd element
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

