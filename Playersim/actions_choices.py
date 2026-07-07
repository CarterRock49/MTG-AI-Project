"""Handlers for ability activation, targeting, and player choices.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
from collections import defaultdict


class ChoiceHandlersMixin:
    """Handlers for ability activation, targeting, and player choices."""

    __slots__ = ()

    def recommend_ability_activation(self, card_id, ability_idx):
        """
        Determine if now is a good time to activate an ability.
        Uses Strategic Planner if available, otherwise basic heuristics.
        """
        gs = self.game_state
        if hasattr(gs, 'strategic_planner') and gs.strategic_planner:
            try:
                return gs.strategic_planner.recommend_ability_activation(card_id, ability_idx)
            except Exception as e:
                logging.warning(f"Error using strategic planner for ability recommendation: {e}")
        # Fallback heuristic
        return True, 0.6 # Default to recommend with medium confidence

    def _handle_surveil_choice(self, param, **kwargs):
        """Handle Surveil choice: PUT_TO_GRAVEYARD"""
        gs = self.game_state
        if hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("type") == "surveil":
            context = gs.choice_context
            player = context["player"]
            if not context.get("cards"):
                logging.warning("Surveil choice made but no cards left to process.")
                gs.choice_context = None # Clear context
                gs.phase = gs.PHASE_PRIORITY # Return to priority
                return 0.0, True

            card_id = context["cards"].pop(0)
            card = gs._safe_get_card(card_id)
            card_name = card.name if card else card_id
            gs.move_card(card_id, player, "library_top_temp", player, "graveyard") # Assume temp zone
            logging.debug(f"Surveil: Put {card_name} into graveyard.")

            # If done surveiling, clear context and return to priority
            if not context.get("cards"):
                logging.debug("Surveil finished.")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                gs.priority_pass_count = 0 # Priority back to active
                gs.priority_player = gs._get_active_player()
            return 0.05, True
        logging.warning("PUT_TO_GRAVEYARD called outside of Surveil context.")
        return -0.1, False

    def _handle_scry_surveil_choice(self, param, context, **kwargs):
        """
        Unified and improved handler for Scry/Surveil actions with better validation and outcomes tracking.
        
        Args:
            param: The action parameter (unused directly)
            context: Context including card index information
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        action_index = kwargs.get('action_index')

        # Determine action type based on action index
        if action_index == 305:  # PUT_TO_GRAVEYARD (Surveil only)
            destination = "graveyard"
            action_name = "PUT_TO_GRAVEYARD"
        elif action_index == 306:  # PUT_ON_TOP (Both Scry and Surveil)
            destination = "top"
            action_name = "PUT_ON_TOP"
        elif action_index == 307:  # PUT_ON_BOTTOM (Scry only)
            destination = "bottom"
            action_name = "PUT_ON_BOTTOM"
        else:
            logging.error(f"Invalid scry/surveil action index: {action_index}")
            return -0.2, False

        # Validate context existence and structure
        if not hasattr(gs, 'choice_context') or gs.choice_context is None:
            logging.warning(f"{action_name} called outside of CHOOSE context")
            return -0.2, False

        context = gs.choice_context
        current_choice_type = context.get("type")

        # Validate context type matches action
        if current_choice_type not in ["scry", "surveil"]:
            logging.warning(f"{action_name} called in incorrect context: {current_choice_type}")
            return -0.2, False
            
        # Validate player is the one making the choice
        if context.get("player") != player:
            logging.warning(f"{action_name} called for incorrect player")
            return -0.2, False
            
        # Validate cards available to process
        if not context.get("cards"):
            logging.warning(f"{action_name} called but no cards to process")
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False

        # Validate action type against context type
        if (destination == "graveyard" and current_choice_type != "surveil") or \
        (destination == "bottom" and current_choice_type != "scry"):
            logging.warning(f"Invalid action {action_name} for {current_choice_type}")
            return -0.1, False

        # Process the card choice
        card_id = context["cards"].pop(0)
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)
        
        # Evaluate card value in current context
        card_value = 0.0
        if self.card_evaluator and card:
            card_value = self.card_evaluator.evaluate_card(card_id, "general", 
                                                        context_details={"destination": destination,
                                                                        "action": current_choice_type})

        # Process the card based on destination
        if destination == "top":
            if current_choice_type == "scry":
                # Add to list of cards kept on top
                context.setdefault("kept_on_top", []).append(card_id)
                logging.debug(f"Scry: Keeping {card_name} on top (pending order)")
                reward = 0.05 + card_value * 0.05  # Higher reward for keeping good cards on top
            else:  # Surveil
                # Put directly back on top of library
                player["library"].insert(0, card_id)
                logging.debug(f"Surveil: Kept {card_name} on top")
                reward = 0.05 + card_value * 0.05
                
        elif destination == "bottom":  # Scry only
            context.setdefault("put_on_bottom", []).append(card_id)
            logging.debug(f"Scry: Putting {card_name} on bottom")
            reward = 0.05 - card_value * 0.05  # Lower reward for putting good cards on bottom
            
        elif destination == "graveyard":  # Surveil only
            success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
            if not success_move:
                logging.error(f"Failed to move {card_name} to graveyard during surveil")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                return -0.1, False
                
            logging.debug(f"Surveil: Put {card_name} into graveyard")
            # Higher reward for putting bad cards in graveyard, but also reward for
            # putting good recursion targets there
            has_recursion = False
            if card and any(x in getattr(card, 'oracle_text', '').lower() for x in 
                        ['from your graveyard', 'from a graveyard']):
                has_recursion = True
                
            reward = 0.05 + (0.05 if has_recursion else -0.05 * card_value)

        # Check if all cards have been processed
        if not context.get("cards"):
            logging.debug(f"{current_choice_type.capitalize()} finished")
            
            # For Scry, finalize the library order
            if current_choice_type == "scry":
                bottom_cards = context.get("put_on_bottom", [])
                top_cards = context.get("kept_on_top", [])
                
                # Apply strategic ordering for top cards (default: keep original order)
                ordered_top_cards = top_cards
                
                # Add cards back to library in the correct order
                player["library"] = ordered_top_cards + player["library"]  # Top cards first
                player["library"].extend(bottom_cards)  # Bottom cards last
                logging.debug(f"Scry final: {len(top_cards)} cards on top, {len(bottom_cards)} on bottom")

            # Clear context and return to previous phase
            previous_phase = getattr(gs, 'previous_priority_phase', gs.PHASE_PRIORITY)
            gs.choice_context = None
            gs.phase = previous_phase
            gs.priority_pass_count = 0
            gs.priority_player = gs._get_active_player()
            
            # Additional reward for completing the full scry/surveil
            reward += 0.05
        
        return reward, True

    def _handle_scry_choice(self, param, **kwargs):
        """Handle Scry choice: PUT_ON_BOTTOM"""
        gs = self.game_state
        # ... (existing checks and logic) ...
        if hasattr(gs, 'choice_context') and gs.choice_context and gs.choice_context.get("type") == "scry":
            context = gs.choice_context
            player = context["player"]
            if not context.get("cards"):
                # ... (handle error) ...
                gs.choice_context = None # --- ADD: Clear context ---
                gs.phase = gs.PHASE_PRIORITY # --- ADD: Set Phase ---
                return -0.1, False

            card_id = context["cards"].pop(0)
            # ... (rest of logic for putting on bottom) ...

            # If done, finalize and return to priority
            if not context.get("cards"):
                 # ... (logic to put cards back on library) ...
                 logging.debug("Scry finished.")
                 # --- Phase Transition ---
                 gs.choice_context = None
                 if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                      gs.phase = gs.previous_priority_phase
                      gs.previous_priority_phase = None
                 else:
                      gs.phase = gs.PHASE_PRIORITY
                 gs.priority_player = gs._get_active_player()
                 gs.priority_pass_count = 0
                 # --- End Phase Transition ---
            return 0.05, True
        logging.warning("PUT_ON_BOTTOM called outside of Scry context.")
        return -0.1, False

    def _handle_activate_ability(self, param, context, **kwargs):
        """
        Enhanced and more robust handler for ability activation with improved error handling,
        cost validation, and support for different ability types.
        
        Args:
            param: Not used directly (None from ACTION_MEANINGS)
            context: Must contain 'battlefield_idx' and 'ability_idx'
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        player = gs._get_active_player()
        
        # Get indices from context
        bf_idx = context.get('battlefield_idx')
        ability_idx = context.get('ability_idx')
        
        # Validate context contains needed indices
        if bf_idx is None or ability_idx is None:
            logging.error(f"ACTIVATE_ABILITY missing required indices in context: {context}")
            return -0.15, False
        
        # Validate indices are integers
        if not isinstance(bf_idx, int) or not isinstance(ability_idx, int):
            logging.error(f"ACTIVATE_ABILITY indices must be integers: {context}")
            return -0.15, False
        
        # Validate battlefield index
        if bf_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ACTIVATE_ABILITY: Invalid battlefield index {bf_idx}")
            return -0.2, False
        
        # Get card and validate
        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        
        if not card:
            logging.warning(f"ACTIVATE_ABILITY: Card not found for ID {card_id}")
            return -0.2, False
        
        # Verify AbilityHandler exists
        if not hasattr(gs, 'ability_handler') or not gs.ability_handler:
            logging.error("Cannot activate ability: AbilityHandler not found")
            return -0.15, False
        
        # Get ability and validate index
        activated_abilities = gs.ability_handler.get_activated_abilities(card_id)
        
        if not activated_abilities:
            logging.warning(f"No activated abilities found for {getattr(card, 'name', card_id)}")
            return -0.15, False
        
        if ability_idx >= len(activated_abilities):
            logging.warning(f"Invalid ability index {ability_idx} for {getattr(card, 'name', card_id)}")
            return -0.15, False
        
        ability = activated_abilities[ability_idx]
        internal_idx = getattr(ability, 'activation_index', ability_idx)
        
        # Check if ability is exhausted
        is_exhaust = getattr(ability, 'is_exhaust', False)
        if is_exhaust and gs.check_exhaust_used(card_id, internal_idx):
            logging.debug(f"Cannot activate Exhaust ability for {card.name}: Already used")
            return -0.05, False
        
        # Check if card is tapped (for abilities requiring untapped state)
        is_tap_ability = False
        if hasattr(ability, 'cost') and '{T}' in ability.cost:
            is_tap_ability = True
            if card_id in player.get("tapped_permanents", set()):
                logging.debug(f"Cannot activate tap ability for {card.name}: Already tapped")
                return -0.05, False
        
        # Check for timing issues with sorcery-speed restrictions
        requires_sorcery = "activate only as a sorcery" in getattr(ability, 'effect_text', '').lower()
        if requires_sorcery and not gs._can_act_at_sorcery_speed(player):
            logging.debug(f"Cannot activate sorcery-speed ability now for {card.name}")
            return -0.05, False
        
        # Prepare cost context
        cost_context = {
            "card_id": card_id,
            "card": card,
            "ability": ability,
            "is_ability": True,
            "cause": "ability_activation"
        }
        cost_context.update(context)
        
        # Verify costs can be paid
        cost_str = getattr(ability, 'cost', None)
        if not cost_str:
            logging.error(f"Ability for {card.name} missing 'cost' attribute")
            return -0.15, False
        
        can_pay = gs.mana_system.can_pay_mana_cost(player, cost_str, cost_context) if gs.mana_system else True
        
        if not can_pay:
            logging.debug(f"Cannot afford cost {cost_str} for {card.name} ability {ability_idx}")
            return -0.05, False
        
        # Pay costs
        costs_paid = gs.mana_system.pay_mana_cost(player, cost_str, cost_context) if gs.mana_system else True
        
        if not costs_paid:
            logging.warning(f"Failed to pay cost for {card.name} ability {ability_idx}")
            return -0.05, False
        
        # Mark exhaust used (if applicable)
        if is_exhaust:
            gs.mark_exhaust_used(card_id, internal_idx)
            # Trigger exhaust event
            if gs.ability_handler:
                exhaust_context = {"activator": player, "source_card_id": card_id, "ability_index": internal_idx}
                gs.ability_handler.check_abilities(card_id, "EXHAUST_ABILITY_ACTIVATED", exhaust_context)
                gs.ability_handler.process_triggered_abilities()
        
        # Handle targeting if needed
        effect_text = getattr(ability, 'effect', getattr(ability, 'effect_text', 'Unknown Effect'))
        requires_target = "target" in effect_text.lower()
        
        if requires_target:
            # Set up targeting phase
            if gs.phase not in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                gs.previous_priority_phase = gs.phase
            
            gs.phase = gs.PHASE_TARGETING
            gs.targeting_context = {
                "source_id": card_id,
                "controller": player,
                "effect_text": effect_text,
                "required_type": gs._get_target_type_from_text(effect_text),
                "required_count": 1,
                "min_targets": 1,
                "max_targets": 1,
                "selected_targets": [],
                "stack_info": {
                    "item_type": "ABILITY",
                    "source_id": card_id,
                    "controller": player,
                    "context": {
                        "ability_index": internal_idx,
                        "effect_text": effect_text,
                        "ability": ability,
                        "is_exhaust": is_exhaust,
                        "targets": {}
                    }
                }
            }
            
            # Set priority to choosing player
            gs.priority_player = player
            gs.priority_pass_count = 0
            logging.debug(f"Set up targeting for ability: {effect_text}")
        else:
            # Add directly to stack
            stack_context = {
                "ability_index": internal_idx,
                "effect_text": effect_text,
                "ability": ability,
                "is_exhaust": is_exhaust,
                "targets": {}
            }
            gs.add_to_stack("ABILITY", card_id, player, stack_context)
            logging.debug(f"Added non-targeting ability {internal_idx} for {card.name} to stack")
        
        # Evaluate strategic value of activation
        ability_value = 0
        if self.card_evaluator:
            try:
                ability_value, _ = self.evaluate_ability_activation(card_id, internal_idx)
            except Exception as e:
                logging.error(f"Error evaluating ability activation: {e}")
        
        # Return reward based on strategic value
        return 0.1 + ability_value * 0.4, True

    def _handle_loyalty_ability(self, param, action_type, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Needs priority to activate PW ability
        bf_idx = param # Param IS the battlefield index from action mapping
        context = kwargs.get('context',{})

        if bf_idx is None or not isinstance(bf_idx, int):
            logging.error(f"Loyalty ability handler called without valid param (battlefield_idx): {bf_idx}.")
            return -0.2, False

        if bf_idx >= len(player.get("battlefield", [])):
             logging.warning(f"Invalid battlefield index {bf_idx} for loyalty ability.")
             return -0.2, False

        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        if not card or 'planeswalker' not in getattr(card, 'card_types', []):
            logging.warning(f"Card at index {bf_idx} ({getattr(card, 'name', 'N/A')}) is not a planeswalker.")
            return -0.15, False

        # Find appropriate ability index
        ability_idx = -1
        if hasattr(card, 'loyalty_abilities'):
            for idx, ability in enumerate(card.loyalty_abilities):
                cost = ability.get('cost', 0)
                is_ultimate = ability.get('is_ultimate', False)
                if action_type == "LOYALTY_ABILITY_PLUS" and cost > 0: ability_idx = idx; break
                if action_type == "LOYALTY_ABILITY_ZERO" and cost == 0: ability_idx = idx; break
                if action_type == "LOYALTY_ABILITY_MINUS" and cost < 0 and not is_ultimate: ability_idx = idx; break
                if action_type == "ULTIMATE_ABILITY" and is_ultimate: ability_idx = idx; break

        if ability_idx != -1:
            success = gs.activate_planeswalker_ability(card_id, ability_idx, player)
            if success:
                ability_value, _ = self.evaluate_ability_activation(card_id, ability_idx)
                return 0.05 + ability_value * 0.1, True # Success
            else:
                logging.debug(f"Planeswalker ability activation failed for {card.name}, Index {ability_idx}")
                return -0.1, False # Failure
        else:
            logging.warning(f"Could not find matching loyalty ability for action {action_type} on {card.name}")
            return -0.15, False # Failure

    def evaluate_ability_activation(self, card_id, ability_idx):
        """Evaluate strategic value of activating an ability."""
        if hasattr(self.game_state, 'strategic_planner') and self.game_state.strategic_planner:
            return self.game_state.strategic_planner.evaluate_ability_activation(card_id, ability_idx)
        return 0.5, "Default ability value" # Fallback

    def _handle_select_target(self, param, context, **kwargs):
        """Handles the SELECT_TARGET action. Param is the index (0-9) into the list of currently valid targets."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        target_choice_index = param # Param is the index chosen by the agent

        # Validate context
        if not gs.targeting_context or gs.targeting_context.get("controller") != player:
            logging.warning("SELECT_TARGET called but not in targeting phase for this player.")
            return -0.2, False

        ctx = gs.targeting_context
        required_count = ctx.get('required_count', 1)
        selected_targets = ctx.get('selected_targets', [])

        # Regenerate the list of valid targets the agent could have chosen from NOW
        # This ensures the index maps correctly even if valid targets changed slightly
        valid_targets_list = []
        if gs.targeting_system:
            valid_map = gs.targeting_system.get_valid_targets(ctx["source_id"], player, ctx["required_type"])
            # Flatten map consistently (e.g., sorted by category then ID)
            for category in sorted(valid_map.keys()):
                valid_targets_list.extend(sorted(valid_map[category]))
            # Ensure list uniqueness if needed (should be handled by get_valid_targets ideally)
            valid_targets_list = sorted(list(set(valid_targets_list))) # Simple unique sort
        else:
            logging.error("Targeting system not available during target selection.")
            return -0.15, False # Cannot select target without system

        # Validate the chosen index
        if 0 <= target_choice_index < len(valid_targets_list):
            target_id = valid_targets_list[target_choice_index] # Get the ID using the agent's chosen index

            if target_id not in selected_targets: # Avoid duplicates unless context allows
                selected_targets.append(target_id)
                ctx["selected_targets"] = selected_targets
                logging.debug(f"Selected target {len(selected_targets)}/{required_count}: {target_id} (Choice Index {target_choice_index})")

                # If enough targets are now selected, finalize targeting
                min_targets = ctx.get('min_targets', required_count) # Use min_targets
                if len(selected_targets) >= min_targets: # Met minimum requirement
                    # Max targets check handled here or implicitly by required_count? Check max too.
                    max_targets = ctx.get('max_targets', required_count)
                    if len(selected_targets) > max_targets:
                         logging.error("Selected more targets than allowed!") # Should not happen if mask is correct
                         return -0.15, False # Error state

                    # Proceed to finalize (put targets into stack item context)
                    found_stack_item = False
                    source_id = ctx.get("source_id")
                    copy_instance_id = ctx.get("copy_instance_id") # Handle copies needing targets
                    if source_id:
                        for i in range(len(gs.stack) - 1, -1, -1):
                            item = gs.stack[i]
                            # Match by source ID and potentially copy ID if available
                            item_matches = (isinstance(item, tuple) and item[1] == source_id)
                            if copy_instance_id: # If targeting a copy, match its specific ID
                                item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                            if item_matches:
                                new_stack_context = item[3] if len(item) > 3 else {}
                                # Structure targets (e.g., categorized or flat list)
                                # Categorization needed if resolution logic depends on type
                                categorized_targets = defaultdict(list)
                                for tid in selected_targets:
                                     # Determine category (simple example)
                                     cat = gs._determine_target_category(tid) # Need helper method
                                     categorized_targets[cat].append(tid)
                                new_stack_context['targets'] = dict(categorized_targets) # Store categorized dict
                                gs.stack[i] = item[:3] + (new_stack_context,)
                                found_stack_item = True
                                logging.debug(f"Updated stack item {i} (Source: {source_id}) with targets: {new_stack_context['targets']}")
                                break
                    if not found_stack_item:
                         # Check if targeting context was for an ability *not* on stack (e.g. ETB choice?)
                         # This requires a different update mechanism if choice isn't for stack item.
                         logging.error(f"Targeting context active (Source: {source_id}) but couldn't find matching stack item!")
                         gs.targeting_context = None # Clear potentially invalid context
                         gs.phase = gs.PHASE_PRIORITY
                         return -0.2, False

                    # Clear targeting context and return to previous phase
                    gs.targeting_context = None
                    if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                         gs.phase = gs.previous_priority_phase
                         gs.previous_priority_phase = None
                    else: gs.phase = gs.PHASE_PRIORITY # Fallback
                    gs.priority_pass_count = 0
                    gs.priority_player = gs._get_active_player()
                    logging.debug("Targeting complete, returning to priority phase.")
                    return 0.05, True # Success
                else:
                    # More targets needed, stay in targeting phase
                    # Let agent choose next target index.
                    return 0.02, True # Incremental success
            else: # Target already selected
                 logging.warning(f"Target {target_id} (Index {target_choice_index}) already selected.")
                 return -0.05, False # Redundant selection
        else: # Invalid index 'param' provided by agent
             logging.error(f"Invalid SELECT_TARGET action parameter: {target_choice_index}. Valid indices: 0-{len(valid_targets_list)-1}")
             return -0.1, False # Invalid choice

    def _handle_sacrifice_permanent(self, param, context, **kwargs):
        """Handles the SACRIFICE_PERMANENT action. Param is the index (0-9) into the list of currently valid sacrifices."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        sacrifice_choice_index = param # Agent's choice index

        # Validate context
        if not hasattr(gs, 'sacrifice_context') or not gs.sacrifice_context or gs.sacrifice_context.get("controller") != player:
            logging.warning("SACRIFICE_PERMANENT called but not in sacrifice phase for this player.")
            return -0.2, False

        ctx = gs.sacrifice_context
        required_count = ctx.get('required_count', 1)
        selected_perms = ctx.get('selected_permanents', [])

        # Regenerate the list of valid permanents the agent could have chosen from NOW
        valid_perms = []
        perm_type_req = ctx.get('required_type')
        for i, perm_id in enumerate(player.get("battlefield", [])): # Use get for safety
            perm_card = gs._safe_get_card(perm_id)
            if not perm_card: continue
            is_valid_type = False
            if not perm_type_req or perm_type_req == "permanent": is_valid_type = True
            elif hasattr(perm_card, 'card_types') and perm_type_req in perm_card.card_types: is_valid_type = True
            elif hasattr(perm_card, 'subtypes') and perm_type_req in perm_card.subtypes: is_valid_type = True

            if is_valid_type:
                 # Check additional conditions from context if needed (e.g., "non-token")
                 valid_perms.append(perm_id)

        # Validate the chosen index
        if 0 <= sacrifice_choice_index < len(valid_perms):
            sac_id = valid_perms[sacrifice_choice_index] # <<< Use agent's chosen index

            if sac_id not in selected_perms: # Avoid duplicates
                selected_perms.append(sac_id)
                ctx["selected_permanents"] = selected_perms # Update the context
                sac_card = gs._safe_get_card(sac_id)
                logging.debug(f"Selected sacrifice {len(selected_perms)}/{required_count}: {getattr(sac_card, 'name', sac_id)} (Choice Index {sacrifice_choice_index})")

                # If enough sacrifices selected, finalize
                if len(selected_perms) >= required_count:
                    sac_reward_mod = 0
                    # Find stack item requiring the sacrifice and update its context
                    found_stack_item = False
                    stack_source_id = ctx.get("source_id")
                    if stack_source_id:
                        for i in range(len(gs.stack) - 1, -1, -1):
                            item = gs.stack[i]
                            if isinstance(item, tuple) and item[1] == stack_source_id:
                                new_stack_context = item[3] if len(item) > 3 else {}
                                new_stack_context['sacrificed_permanents'] = selected_perms
                                gs.stack[i] = item[:3] + (new_stack_context,)
                                found_stack_item = True
                                logging.debug(f"Updated stack item {i} (Source: {stack_source_id}) with sacrifices: {selected_perms}")
                                break
                    # Handle cases where sacrifice isn't for stack (e.g., cost payment)
                    # Need context to know how to proceed if not stack related. Assume stack for now.
                    if not found_stack_item and stack_source_id:
                        logging.error(f"Sacrifice context active (Source: {stack_source_id}), but couldn't find matching stack item!")

                    # Calculate reward based on value of sacrificed cards
                    if hasattr(self, 'card_evaluator'):
                        for sacrifice_id in selected_perms:
                            sac_reward_mod -= self.card_evaluator.evaluate_card(sacrifice_id, "sacrifice") * 0.2

                    # Clear context and return to previous phase
                    gs.sacrifice_context = None
                    if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                         gs.phase = gs.previous_priority_phase
                         gs.previous_priority_phase = None
                    else: gs.phase = gs.PHASE_PRIORITY
                    gs.priority_pass_count = 0
                    gs.priority_player = gs._get_active_player()
                    logging.debug("Sacrifice choice complete, returning to priority phase.")
                    return 0.1 + sac_reward_mod, True
                else:
                    # More sacrifices needed, stay in SACRIFICE phase
                    return 0.02, True # Incremental success
            else: # Card already selected
                 logging.warning(f"Sacrifice choice index {sacrifice_choice_index} points to already selected permanent {sac_id}.")
                 return -0.05, False
        else: # Invalid index 'param' provided by agent
            logging.error(f"Invalid SACRIFICE_PERMANENT action parameter: {sacrifice_choice_index}. Valid indices: 0-{len(valid_perms)-1}")
            return -0.1, False

    def _handle_special_choice_actions(self, param, context, **kwargs):
        """
        Handle special choice actions like mode selection, color selection, X value choice, etc.
        Delegates to specific handlers based on the context type.
        """
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        choice_type = None
        
        # Get choice context
        if not hasattr(gs, 'choice_context') or not gs.choice_context:
            logging.warning("Special choice action called, but no choice context found.")
            return -0.2, False
        
        choice_type = gs.choice_context.get("type")
        
        # Verify player has authority to make this choice
        if gs.choice_context.get("player") != player:
            logging.warning("Received choice action for non-active choice player.")
            return -0.2, False
        
        # Delegate to appropriate choice handler based on type
        if choice_type == "scry" or choice_type == "surveil":
            # Handle scry/surveil choices (PUT_ON_TOP, PUT_ON_BOTTOM, PUT_TO_GRAVEYARD)
            action_index = kwargs.get('action_index')
            
            if action_index == 306:  # PUT_ON_TOP
                return self._handle_scry_surveil_choice(param, context, action_index=306)
            elif action_index == 307:  # PUT_ON_BOTTOM
                if choice_type == "surveil":
                    logging.warning("Cannot PUT_ON_BOTTOM during Surveil choice.")
                    return -0.1, False
                return self._handle_scry_surveil_choice(param, context, action_index=307)
            elif action_index == 305:  # PUT_TO_GRAVEYARD
                if choice_type == "scry":
                    logging.warning("Cannot PUT_TO_GRAVEYARD during Scry choice.")
                    return -0.1, False
                return self._handle_scry_surveil_choice(param, context, action_index=305)
        
        elif choice_type == "dredge":
            # Use the specific dredge handler - Index 308
            return self._handle_dredge(param, context)
        
        elif choice_type == "choose_mode":
            # Handle choosing modes from options - Indices 353-362
            return self._handle_choose_mode(param, context)
        
        elif choice_type == "choose_x":
            # Handle choosing X value - Indices 363-372
            return self._handle_choose_x(param, context)
        
        elif choice_type == "choose_color":
            # Handle choosing color - Indices 373-377
            return self._handle_choose_color(param, context)
        
        elif choice_type == "pay_kicker":
            # Handle kicker payment choice - Indices 405-406
            action_index = kwargs.get('action_index')
            if action_index == 405:  # PAY_KICKER
                return self._handle_pay_kicker(True, context)
            elif action_index == 406:  # DONT_PAY_KICKER
                return self._handle_pay_kicker(False, context)
        
        elif choice_type == "pay_additional":
            # Handle additional cost payment choice - Indices 407-408
            action_index = kwargs.get('action_index')
            if action_index == 407:  # PAY_ADDITIONAL_COST
                return self._handle_pay_additional_cost(True, context)
            elif action_index == 408:  # DONT_PAY_ADDITIONAL_COST
                return self._handle_pay_additional_cost(False, context)
        
        elif choice_type == "pay_escalate":
            # Handle escalate payment - Index 409
            return self._handle_pay_escalate(param, context)
        
        # If we reach here, either the choice type is unrecognized or the action doesn't match the choice
        logging.warning(f"Unhandled special choice type: {choice_type} or mismatched action")
        return -0.1, False

    def _handle_order_blockers(self, param, context, **kwargs):
        """CR 510.1c: assign damage to pending blocker [param] next."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        ctx = getattr(gs, 'choice_context', None)
        if not ctx or ctx.get('type') != 'order_blockers' or ctx.get('player') != player:
            logging.warning("ASSIGN_DAMAGE order action called out of context.")
            return -0.2, False
        handler = getattr(gs, 'combat_action_handler', None)
        if handler and handler.blocker_order_chosen(param):
            return 0.05, True
        logging.warning(f"Invalid blocker order index {param}.")
        return -0.1, False

    def _handle_order_triggers(self, param, context, **kwargs):
        """CR 603.3b: put pending trigger [param] onto the stack next.
        Delegates the mechanics to AbilityHandler.order_trigger_chosen."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        ctx = getattr(gs, 'choice_context', None)
        if not ctx or ctx.get('type') != 'order_triggers' or ctx.get('player') != player:
            logging.warning("ORDER_TRIGGER action called out of context.")
            return -0.2, False
        if gs.ability_handler and gs.ability_handler.order_trigger_chosen(param):
            return 0.05, True
        logging.warning(f"Invalid ORDER_TRIGGER index {param}.")
        return -0.1, False

    def _handle_choose_mode(self, param, context, **kwargs):
        """Handles the CHOOSE_MODE action. Param is the chosen mode index (0-9). Finalizes choice if criteria met."""
        gs = self.game_state
        # CR 603.3b / 510.1c: ordering choices share the 353-362 index range.
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'order_triggers':
            return self._handle_order_triggers(param, context)
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'order_blockers':
            return self._handle_order_blockers(param, context)
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        chosen_mode_idx = param # Agent's choice index from action

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_mode" or gs.choice_context.get("player") != player:
             logging.warning("CHOOSE_MODE called out of context.")
             return -0.2, False

        ctx = gs.choice_context
        num_choices = ctx.get("num_choices", 0)
        min_required = ctx.get("min_required", 1)
        max_required = ctx.get("max_required", 1)
        selected_modes = ctx.get("selected_modes", [])
        available_modes_text = ctx.get("available_modes", [])

        # Validate chosen mode index
        if 0 <= chosen_mode_idx < num_choices:
            # Check if maximum choices already reached
            if len(selected_modes) >= max_required:
                logging.warning(f"Attempted to select more modes than allowed ({max_required}) for {ctx.get('card_id')}.")
                return -0.1, False

            # Check if mode already selected (disallow unless specific rule allows - rare)
            if chosen_mode_idx in selected_modes:
                logging.warning(f"Mode index {chosen_mode_idx} already selected for {ctx.get('card_id')}.")
                return -0.05, False # Penalty for redundant choice

            # --- Valid Choice Made ---
            selected_modes.append(chosen_mode_idx)
            ctx["selected_modes"] = selected_modes # Update context immediately
            chosen_mode_text = available_modes_text[chosen_mode_idx] if chosen_mode_idx < len(available_modes_text) else f"Mode {chosen_mode_idx}"
            logging.debug(f"Selected mode {len(selected_modes)}/{max_required}: Mode Index {chosen_mode_idx} ('{chosen_mode_text[:30]}...')")

            # Check if the choice is now complete
            # Complete if max required reached OR min required reached and player passes/finalizes
            finalize_choice = False
            if len(selected_modes) >= max_required:
                 finalize_choice = True
                 logging.debug("Maximum modes selected.")
            # Note: Need a way for player to signal completion if min < max.
            # Could use PASS_PRIORITY action when in PHASE_CHOOSE, or a dedicated FINISH_CHOICE action.
            # For now, assume finalize only when max is reached.

            if finalize_choice:
                 # --- Finalizing the Choice ---
                 card_id = ctx.get("card_id")
                 cast_controller = ctx.get("controller") # Get original caster
                 original_cast_context = ctx.get("original_cast_context", {})
                 final_paid_cost = ctx.get("final_paid_cost", {})

                 if not card_id or not cast_controller:
                      logging.error("CRITICAL: Choice context missing card_id or controller during finalization.")
                      # Clear broken context
                      gs.choice_context = None
                      gs.phase = gs.PHASE_PRIORITY
                      return -0.5, False # Indicate major state error

                 # Prepare the final context for the stack item
                 final_stack_context = original_cast_context.copy() # Start with original cast context
                 final_stack_context['selected_modes'] = selected_modes # Embed the chosen modes
                 final_stack_context['final_paid_cost'] = final_paid_cost # Include cost paid for copies etc.
                 # Clear temporary choice flags from context if any
                 final_stack_context.pop('available_modes', None)
                 final_stack_context.pop('min_required', None)
                 final_stack_context.pop('max_required', None)

                 # Add the spell WITH CHOSEN MODES to the stack
                 gs.add_to_stack("SPELL", card_id, cast_controller, final_stack_context)
                 # add_to_stack handles phase transition back to PRIORITY and resets priority

                 logging.debug(f"Finalized mode choice for {card_id}. Spell added to stack.")

                 # Clear choice context AFTER adding to stack
                 gs.choice_context = None

                 return 0.1, True # Successful choice and finalization
            else:
                 # More modes can/must be chosen, stay in PHASE_CHOOSE
                 return 0.05, True # Incremental success
        else:
            # Invalid mode index chosen by agent
            logging.error(f"Invalid CHOOSE_MODE action parameter: {chosen_mode_idx}. Valid indices: 0-{num_choices-1}")
            return -0.1, False

    def _handle_choose_x(self, param, context, **kwargs):
        """Handles CHOOSE_X action. Param is the chosen X value (1-10)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        x_value = param # Use agent's chosen value directly

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_x" or gs.choice_context.get("player") != player:
            logging.warning("CHOOSE_X called out of context.")
            return -0.2, False

        ctx = gs.choice_context
        max_x = ctx.get("max_x", 0) # Get max allowed based on affordability check done earlier
        min_x = ctx.get("min_x", 0)

        # Validate chosen X value
        if min_x <= x_value <= max_x:
            ctx["chosen_x"] = x_value # Store chosen value

            # --- FINALIZING LOGIC (Update Stack, Pay X Cost, Change Phase) ---
            found_stack_item = False
            source_id = ctx.get("source_id")
            copy_instance_id = ctx.get("copy_instance_id")
            if source_id:
                for i in range(len(gs.stack) - 1, -1, -1):
                    item = gs.stack[i]
                    item_matches = (isinstance(item, tuple) and item[1] == source_id)
                    if copy_instance_id: item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                    if item_matches:
                        new_stack_context = item[3] if len(item) > 3 else {}
                        new_stack_context['X'] = x_value # Store chosen X
                        # Store final cost including X component? Or assume ManaSystem tracks pending?
                        # Store chosen X is usually enough for resolution logic.
                        gs.stack[i] = item[:3] + (new_stack_context,)
                        found_stack_item = True
                        logging.debug(f"Updated stack item {i} (Source: {source_id}) with X={x_value}")
                        break
            if not found_stack_item: logging.error("X choice context active but couldn't find stack item!")

            # Pay the X cost (ManaSystem needed)
            # NOTE: Cost payment for X was originally planned during cast_spell setup,
            # but rules state costs paid on resolution *after* choices.
            # For simplicity, assume cost was checked during CHOOSE_X action generation,
            # and PAY it now. More accurate would be to require agent PAY_X action, or pay during resolution.
            # Pay now for simplification.
            x_cost_paid = False
            if gs.mana_system:
                paid_details = gs.mana_system.pay_mana_cost_get_details(player, {'generic': x_value})
                if paid_details: x_cost_paid = True
                else: logging.error(f"Failed to pay X={x_value} mana cost!")
            if not x_cost_paid:
                logging.error("Aborting CHOOSE_X: Failed to pay the required mana for X.")
                gs.choice_context = None # Clear invalid state
                gs.phase = gs.PHASE_PRIORITY # Return to priority
                # Need to handle stack potentially? Rollback?
                return -0.2, False # Failed cost payment

            # Clear choice context and return to previous phase
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else: gs.phase = gs.PHASE_PRIORITY
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            logging.debug(f"Chose X={x_value} and paid cost.")
            return 0.05, True
        else: # Invalid X value
             logging.error(f"Invalid CHOOSE_X action parameter: {x_value}. Valid range: [{min_x}-{max_x}]")
             return -0.1, False

    def _handle_choose_color(self, param, context, **kwargs):
        """Handles CHOOSE_COLOR action. Param is the color index (0-4)."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        color_idx = param # Use agent's choice

        # Validate context
        if not gs.choice_context or gs.choice_context.get("type") != "choose_color" or gs.choice_context.get("player") != player:
            logging.warning("CHOOSE_COLOR called out of context.")
            return -0.2, False

        ctx = gs.choice_context
        # Validate chosen color index
        if 0 <= color_idx <= 4:
             chosen_color = ['W','U','B','R','G'][color_idx]
             ctx["chosen_color"] = chosen_color

             # --- FINALIZING LOGIC (Update Stack, Change Phase) ---
             found_stack_item = False
             source_id = ctx.get("source_id")
             copy_instance_id = ctx.get("copy_instance_id")
             if source_id:
                 for i in range(len(gs.stack) - 1, -1, -1):
                     item = gs.stack[i]
                     item_matches = (isinstance(item, tuple) and item[1] == source_id)
                     if copy_instance_id: item_matches &= (item[3].get('copy_instance_id') == copy_instance_id)

                     if item_matches:
                         new_stack_context = item[3] if len(item) > 3 else {}
                         new_stack_context['chosen_color'] = chosen_color
                         gs.stack[i] = item[:3] + (new_stack_context,)
                         found_stack_item = True
                         logging.debug(f"Updated stack item {i} (Source: {source_id}) with chosen color={chosen_color}")
                         break
             if not found_stack_item: logging.error("Color choice context active but couldn't find stack item!")

             # Clear choice context and return to previous phase
             gs.choice_context = None
             if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                  gs.phase = gs.previous_priority_phase
                  gs.previous_priority_phase = None
             else: gs.phase = gs.PHASE_PRIORITY
             gs.priority_player = gs._get_active_player()
             gs.priority_pass_count = 0
             logging.debug(f"Chose color {chosen_color}")
             return 0.05, True
        else: # Invalid color index
            logging.error(f"Invalid CHOOSE_COLOR action parameter: {color_idx}. Valid indices: 0-4")
            return -0.1, False

        # --- Placeholder Handlers for unimplemented actions ---
    def _handle_unimplemented(self, param, action_type, **kwargs):
        logging.warning(f"Action handler for {action_type} not implemented.")
        fc = getattr(self.game_state, 'fidelity_counters', None)
        if fc is not None:
            fc["unimplemented_action"] += 1
            fc["unimplemented_action_types"].add(str(action_type))
        return -0.05 # Small penalty for trying unimplemented action

    def _handle_search_library(self, param, context=None, **kwargs):
        gs = self.game_state
        player = gs._get_active_player() # Assumes active player searches
        criteria = self._get_search_criteria_from_param(param)
        if not criteria: return -0.1, False # Invalid search param

        if hasattr(gs, 'search_library_and_choose'):
            # Use context provided by agent/env if available
            ai_choice_context = kwargs.get('context', {}) # Get full context
            ai_choice_context['goal'] = criteria # Add goal if not present

            found_id = gs.search_library_and_choose(player, criteria, ai_choice_context=ai_choice_context)
            if found_id:
                 return 0.4, True # Successful search + find
            else: # Search failed, still shuffle (which is considered success of the *action*)
                 # gs.shuffle_library(player) handled inside search_library_and_choose
                 return 0.0, True # Search performed but nothing found
        logging.error("SEARCH_LIBRARY: GameState missing search_library_and_choose method.")
        return -0.15, False # Failure (cannot perform search)

    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()

        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        # Add more specific checks if needed
        return False

    def _get_search_criteria_from_param(self, param):
        """Helper to map param index (e.g., 299-303) to search criteria."""
        search_map = {0: "basic land", 1: "creature", 2: "instant", 3: "sorcery", 4: "artifact"}
        return search_map.get(param, None) # param would be 0-4 if derived from 299-303

    def _handle_select_spree_mode(self, param, context, **kwargs): # Param is None
        gs = self.game_state
        player = gs._get_active_player() # Assume player choosing mode has priority
        hand_idx = context.get('hand_idx')
        mode_idx = context.get('mode_idx')

        if hand_idx is None or mode_idx is None: logging.error(f"SELECT_SPREE_MODE context missing indices: {context}"); return -0.15, False
        if not isinstance(hand_idx, int) or not isinstance(mode_idx, int): logging.error(f"SELECT_SPREE_MODE context indices non-integer: {context}"); return -0.15, False

        if hand_idx >= len(player["hand"]): logging.warning(f"Invalid hand index {hand_idx} for SELECT_SPREE_MODE."); return -0.2, False

        card_id = player["hand"][hand_idx]
        card = gs._safe_get_card(card_id)
        if not card or not getattr(card, 'is_spree', False) or mode_idx >= len(getattr(card,'spree_modes',[])):
            logging.warning(f"Invalid card or mode index for Spree: Hand:{hand_idx}, Mode:{mode_idx}"); return -0.1, False

        # Manage pending context
        if not hasattr(gs, 'pending_spell_context') or gs.pending_spell_context.get('card_id') != card_id:
            gs.pending_spell_context = {'card_id': card_id, 'hand_idx': hand_idx, 'selected_spree_modes': set(), 'spree_costs': {}, 'source_zone': 'hand'}

        selected_modes = gs.pending_spell_context.setdefault('selected_spree_modes', set())
        mode_cost_str = card.spree_modes[mode_idx].get('cost', '')

        if not self._can_afford_cost_string(player, mode_cost_str, context=context):
            logging.warning(f"Cannot afford Spree mode {mode_idx} cost {mode_cost_str} for {card.name}")
            return -0.05, False

        if mode_idx in selected_modes:
            logging.warning(f"Spree mode {mode_idx} already selected for {card.name}")
            # Deselect? Or just fail? Let's fail redundant selection.
            return -0.05, False

        selected_modes.add(mode_idx)
        gs.pending_spell_context['spree_costs'][mode_idx] = mode_cost_str
        logging.debug(f"Added Spree mode {mode_idx} (Cost: {mode_cost_str}) to pending cast for {card.name}")
        gs.phase = gs.PHASE_PRIORITY # Stay in priority
        return 0.05, True # Successful mode selection

    def _handle_put_to_graveyard(self, param, context, **kwargs):
        """Handle Surveil choice: PUT_TO_GRAVEYARD. Relies on context from _add_special_choice_actions."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        if not hasattr(gs, 'choice_context') or gs.choice_context is None or gs.choice_context.get("type") != "surveil":
            logging.warning("PUT_TO_GRAVEYARD called outside of Surveil context.")
            return -0.2, False
        if gs.choice_context.get("player") != player:
            logging.warning("Received PUT_TO_GRAVEYARD choice for non-active choice player.")
            return -0.2, False # Wrong player

        context = gs.choice_context
        if not context.get("cards"):
            logging.warning("Surveil choice PUT_TO_GRAVEYARD made but no cards left to process.")
            gs.choice_context = None # Clear context
            gs.phase = gs.PHASE_PRIORITY # Return to priority
            return -0.1, False # Minor error, but invalid state

        card_id = context["cards"].pop(0) # Process first card in the list
        card = gs._safe_get_card(card_id)
        card_name = getattr(card, 'name', card_id)

        # Use move_card to handle replacements/triggers
        success_move = gs.move_card(card_id, player, "library_implicit", player, "graveyard", cause="surveil")
        if not success_move:
            logging.error(f"Failed to move {card_name} to graveyard during surveil.")
            # Put card back? State is potentially inconsistent. End choice phase.
            gs.choice_context = None
            gs.phase = gs.PHASE_PRIORITY
            return -0.1, False

        logging.debug(f"Surveil: Put {card_name} into graveyard.")

        # If done surveiling, clear context and return to previous phase
        if not context.get("cards"):
            logging.debug("Surveil finished.")
            gs.choice_context = None
            if hasattr(gs, 'previous_priority_phase') and gs.previous_priority_phase is not None:
                 gs.phase = gs.previous_priority_phase
                 gs.previous_priority_phase = None
            else:
                 gs.phase = gs.PHASE_PRIORITY # Fallback
            gs.priority_pass_count = 0 # Reset priority
            gs.priority_player = gs._get_active_player()
        # Else, stay in CHOICE phase for next card

        # Positive reward for making a valid choice
        card_eval_score = 0
        if self.card_evaluator and card:
             # Evaluate card being put in GY (might be good for recursion)
             card_eval_score = self.card_evaluator.evaluate_card(card_id, "general", context_details={"destination":"graveyard"})
        # Reward higher if putting low-value card in GY
        return 0.05 - card_eval_score * 0.05, True
