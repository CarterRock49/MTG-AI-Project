"""Handlers for ability activation, targeting, and player choices.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
from collections import defaultdict
from .ability_types import ActivatedAbility, ManaAbility


class ChoiceHandlersMixin:
    """Handlers for ability activation, targeting, and player choices."""

    __slots__ = ()

    def _get_target_selection_candidates(self, player, context):
        """Return the exact ordered candidates represented by SELECT_TARGET.

        Mask generation and execution share this helper so filtering a final,
        unaffordable target set cannot change the meaning of an action index.
        """
        gs = self.game_state
        context = context or {}
        if not gs.targeting_system:
            return []

        valid_map = gs.targeting_system.get_valid_targets(
            context.get("source_id"), player,
            context.get("required_type", "target"),
            effect_text=context.get("effect_text"))
        selected_targets = list(context.get("selected_targets", []))
        excluded_targets = set(context.get("excluded_target_ids", []))
        candidates = sorted({
            target_id
            for category_targets in valid_map.values()
            for target_id in category_targets
            if (target_id not in selected_targets
                and target_id not in excluded_targets)
        }, key=lambda target_id: (isinstance(target_id, str), target_id))

        # Selecting the last available slot auto-finalizes in
        # _handle_select_target.  Do not expose a candidate that would turn a
        # mask-valid action into a failed cast at that boundary.
        max_targets = int(context.get(
            "max_targets", context.get("required_count", 1)))
        if len(selected_targets) + 1 >= max_targets:
            candidates = [
                target_id for target_id in candidates
                if gs._can_finalize_targeted_cast(
                    context, selected_targets + [target_id])
            ]
        return candidates

    def _handle_target_page_next(self, param=None, context=None, **kwargs):
        gs = self.game_state
        ctx = getattr(gs, 'targeting_context', None)
        player = self._get_policy_player(context)
        requested_page_count = (context or {}).get('page_count')
        if ctx and ctx.get('controller') is player:
            if requested_page_count is None:
                valid_map = gs.targeting_system.get_valid_targets(
                    ctx.get('source_id'), player,
                    ctx.get('required_type', 'target'),
                    effect_text=ctx.get('effect_text'))
                candidates = sorted(
                    {target for targets in valid_map.values()
                     for target in targets
                     if (target not in ctx.get('selected_targets', [])
                         and target not in ctx.get('excluded_target_ids', []))},
                    key=lambda target_id: (
                        isinstance(target_id, str), target_id))
                page_count = max(1, (len(candidates) + 9) // 10)
            else:
                page_count = max(1, int(requested_page_count))
            ctx['target_page'] = (
                int(ctx.get('target_page', 0)) + 1) % page_count
            return 0.0, True
        choice = getattr(gs, 'choice_context', None)
        if (choice and choice.get('player') is player
                and choice.get('type') in (
                    'sacrifice_effect', 'activation_sacrifice_cost',
                    'dig_select', 'distribute_counters', 'discard',
                    'specialize_discard', 'forced_sacrifice')):
            choice_options = choice.get('options', [])
            if choice.get('type') in ('discard', 'specialize_discard'):
                choice_options = choice.get('player', {}).get('hand', [])
            elif choice.get('type') == 'forced_sacrifice':
                choice_options = choice.get('player', {}).get('battlefield', [])
            page_count = max(
                1, int(requested_page_count)
                if requested_page_count is not None
                else (len(choice_options) + 9) // 10)
            choice['choice_page'] = (
                int(choice.get('choice_page', 0)) + 1) % page_count
            return 0.0, True
        return -0.1, False

    def _advance_or_finish_sacrifice_effect(self, ctx, performed):
        """Advance an affected-player sacrifice choice or resume resolution."""
        gs = self.game_state
        ctx['sacrifice_performed'] = bool(
            ctx.get('sacrifice_performed', False) or performed)
        permanent_type = str(ctx.get('permanent_type', 'permanent')).rstrip('s')
        pending = ctx.get('pending_players', [])
        if pending:
            next_choice = pending.pop(0)
            next_player = gs.p1 if next_choice['player_id'] == 'p1' else gs.p2
            ctx['player'] = next_player
            ctx['remaining'] = next_choice['remaining']
            ctx['optional'] = bool(next_choice.get('optional', False))
            ctx['choice_page'] = 0
            ctx['options'] = [
                cid for cid in next_player.get('battlefield', [])
                if permanent_type == 'permanent'
                or permanent_type in {
                    str(t).lower()
                    for t in getattr(gs._safe_get_card(cid), 'card_types', [])
                }
            ]
            gs.priority_player = next_player
            return True

        followup = ctx.get('reflexive_followup')
        if followup and ctx.get('sacrifice_performed'):
            from .ability_types import TriggeredAbility
            trigger = TriggeredAbility(
                followup['source_id'],
                trigger_condition=followup['trigger_condition'],
                effect=followup['trigger_effect_text'],
                effect_text=(f"{followup['trigger_condition'].capitalize()}, "
                             f"{followup['trigger_effect_text']}."))
            trigger._is_reflexive_trigger = True
            trigger_context = {
                'ability': trigger, 'source_id': followup['source_id'],
                'effect_text': followup['trigger_effect_text'],
                'is_reflexive_trigger': True,
                'reflexive_prerequisite': followup['prerequisite_text'],
            }
            followup_controller = (
                gs.p1 if followup['controller_id'] == 'p1' else gs.p2)
            gs.ability_handler.active_triggers.append(
                (trigger, followup_controller, trigger_context))
        gs._resume_effect_continuation(ctx)
        return False

    def _finish_dig_select_choice(self, ctx):
        """Place unchosen cards and resume a Dig-style effect continuation."""
        gs = self.game_state
        player = ctx.get('player')
        options = list(ctx.get('options', []))
        source_zone = ctx.get('source_zone', 'library_implicit')
        destination = ctx.get('rest_destination', 'bottom')

        if destination == 'bottom':
            if source_zone == 'library_implicit':
                player['library'].extend(options)
            else:
                for card_id in options:
                    gs.move_card(
                        card_id, player, source_zone, player, 'library',
                        cause='dig_rest_bottom')
        elif destination == 'top':
            if source_zone == 'library_implicit':
                player['library'][:0] = options
            else:
                moved = []
                for card_id in options:
                    if gs.move_card(
                            card_id, player, source_zone, player, 'library',
                            cause='dig_rest_top'):
                        moved.append(card_id)
                for card_id in moved:
                    player['library'].remove(card_id)
                player['library'][:0] = moved
        elif destination == 'graveyard' and source_zone != 'graveyard':
            for card_id in options:
                gs.move_card(
                    card_id, player, source_zone, player, 'graveyard',
                    cause='dig_discard')
        elif destination != 'stay':
            logging.warning(
                "Unknown Dig rest destination %r; leaving cards in %s.",
                destination, source_zone)

        gs._resume_effect_continuation(ctx)
        return True

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

    def _handle_scry_surveil_choice(self, param, context, action_index=None, **kwargs):
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
        if action_index is None:
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
        if current_choice_type not in ["scry", "surveil", "explore"]:
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
        if (destination == "graveyard" and current_choice_type not in ["surveil", "explore"]) or \
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
            else:  # Surveil / explore
                # Put directly back on top of library
                player["library"].insert(0, card_id)
                logging.debug(f"{current_choice_type.capitalize()}: Kept {card_name} on top")
                reward = 0.05 + card_value * 0.05
                
        elif destination == "bottom":  # Scry only
            context.setdefault("put_on_bottom", []).append(card_id)
            logging.debug(f"Scry: Putting {card_name} on bottom")
            reward = 0.05 - card_value * 0.05  # Lower reward for putting good cards on bottom
            
        elif destination == "graveyard":  # Surveil / explore
            success_move = gs.move_card(
                card_id, player, "library_implicit", player, "graveyard",
                cause=current_choice_type)
            if not success_move:
                logging.error(f"Failed to move {card_name} to graveyard during surveil")
                gs.choice_context = None
                gs.phase = gs.PHASE_PRIORITY
                return -0.1, False
                
            logging.debug(f"{current_choice_type.capitalize()}: Put {card_name} into graveyard")
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
            
            if current_choice_type == "explore":
                exploring_id = context.get("exploring_creature_id")
                event = ("EXPLORED_NONLAND_GY" if destination == "graveyard"
                         else "EXPLORED_NONLAND_TOP")
                event_context = {
                    "controller": player,
                    "revealed_card_id": card_id,
                    "source_id": context.get("source_id"),
                }
                gs.trigger_ability(exploring_id, event, event_context)
                gs.trigger_ability(exploring_id, "EXPLORED", event_context)

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
            previous_phase = getattr(gs, 'previous_priority_phase', None) or gs.PHASE_PRIORITY
            gs.choice_context = None
            gs.phase = previous_phase
            gs.previous_priority_phase = None
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
        controller_id = context.get('controller_id')
        if controller_id == 'p1':
            player = gs.p1
        elif controller_id == 'p2':
            player = gs.p2
        else:
            player = gs.p1 if gs.agent_is_p1 else gs.p2

        # Ordinary activated abilities belong to the player whose policy is
        # acting, including the non-active player. A mismatched priority owner
        # is never allowed to activate through a stale action context.
        if gs.priority_player is not None and gs.priority_player is not player:
            logging.warning("ACTIVATE_ABILITY called by a player without priority.")
            return -0.15, False
        
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
        if bf_idx < 0 or bf_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ACTIVATE_ABILITY: Invalid battlefield index {bf_idx}")
            return -0.2, False
        
        # Get card and validate
        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        ability_source_occurrence = (card_id, bf_idx)
        staged_source_occurrence = context.get('activation_source_occurrence')
        if staged_source_occurrence is not None:
            normalized_source = ActivatedAbility._as_sacrifice_occurrence(
                staged_source_occurrence)
            if (normalized_source is None
                    or normalized_source != ability_source_occurrence):
                logging.warning("ACTIVATE_ABILITY source occurrence changed while staged.")
                return -0.15, False
            ability_source_occurrence = normalized_source
        
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

        effect_text = getattr(ability, 'effect', getattr(ability, 'effect_text', 'Unknown Effect'))
        requires_target = "target" in effect_text.lower()
        activation_targets = context.get("activation_targets")
        if requires_target and not activation_targets:
            target_type = gs._get_target_type_from_text(effect_text)
            min_targets, max_targets = gs._target_bounds_from_text(effect_text)
            valid_map = gs.targeting_system.get_valid_targets(
                card_id, player, target_type, effect_text=effect_text)
            if not any(valid_map.values()):
                logging.debug(f"Cannot activate {card.name}: no legal targets.")
                return -0.05, False
            if (gs.phase not in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE,
                                gs.PHASE_CHOOSE]
                    and gs.previous_priority_phase is None):
                gs.previous_priority_phase = gs.phase
            gs.phase = gs.PHASE_TARGETING
            gs.targeting_context = {
                "source_id": card_id,
                "controller": player,
                "effect_text": effect_text,
                "required_type": target_type,
                "required_count": max_targets,
                "min_targets": min_targets,
                "max_targets": max_targets,
                "selected_targets": [],
                "resume_activation": True,
                "activation_context": {
                    "battlefield_idx": bf_idx,
                    "ability_idx": ability_idx,
                    "controller_id": "p1" if player is gs.p1 else "p2",
                    "activation_source_occurrence": ability_source_occurrence,
                },
            }
            gs.priority_player = player
            gs.priority_pass_count = 0
            logging.debug(f"Waiting for a target before paying {card.name}'s activation cost.")
            return 0.02, True
        
        # Prepare cost context
        cost_context = {
            "card_id": card_id,
            "card": card,
            "ability": ability,
            "is_ability": True,
            "cause": "ability_activation",
            "activation_source_occurrence": ability_source_occurrence,
        }
        cost_context.update(context)
        
        # Verify costs can be paid
        cost_str = getattr(ability, 'cost', None)
        if not cost_str:
            logging.error(f"Ability for {card.name} missing 'cost' attribute")
            return -0.15, False
        
        can_pay = ability.can_pay_cost(gs, player, cost_context)
        
        if not can_pay:
            logging.debug(f"Cannot afford cost {cost_str} for {card.name} ability {ability_idx}")
            return -0.05, False
        
        cost_lower = cost_str.lower()
        if ("sacrifice this" in cost_lower
                and card_id not in player.get("battlefield", [])):
            return -0.05, False

        # Non-self sacrifice costs are choices, not evaluator heuristics. Stage
        # every selected permanent first; ``pay_cost`` then commits all cost
        # components together after the final choice.
        sacrifice_spec = ability.get_sacrifice_cost_spec()
        chosen_occurrences = list(
            context.get("activation_sacrifice_occurrences", []))
        chosen_sacrifices = list(context.get("activation_sacrifice_ids", []))
        staged_choices = chosen_occurrences or chosen_sacrifices
        if (sacrifice_spec and not sacrifice_spec["self_sacrifice"]
                and len(staged_choices) < sacrifice_spec["count"]):
            normalized_selected, _ = ability._normalize_sacrifice_selections(
                gs, player, staged_choices, sacrifice_spec["requirement"],
                ability_source_occurrence)
            if normalized_selected is None:
                return -0.05, False
            remaining = sacrifice_spec["count"] - len(normalized_selected)
            candidate_occurrences = ability.get_sacrifice_cost_candidates(
                gs, player, sacrifice_spec["requirement"],
                excluded=normalized_selected,
                source_occurrence=ability_source_occurrence,
                return_occurrences=True)
            if len(candidate_occurrences) < remaining:
                logging.debug(
                    f"Cannot activate {card.name}: sacrifice cost needs "
                    f"{remaining} more matching permanents.")
                return -0.05, False
            activation_context = dict(context)
            activation_context.update({
                "battlefield_idx": bf_idx,
                "ability_idx": ability_idx,
                "controller_id": "p1" if player is gs.p1 else "p2",
                "activation_source_occurrence": ability_source_occurrence,
                "activation_sacrifice_occurrences": normalized_selected,
                "activation_sacrifice_ids": [
                    occurrence[0] for occurrence in normalized_selected],
            })
            gs.choice_context = {
                "type": "activation_sacrifice_cost",
                "player": player,
                "source_id": card_id,
                "options": [occurrence[0]
                            for occurrence in candidate_occurrences],
                "option_occurrences": candidate_occurrences,
                "selected": normalized_selected,
                "remaining": remaining,
                "requirement": sacrifice_spec["requirement"],
                "activation_context": activation_context,
                "resume_phase": gs.phase,
                "choice_page": 0,
            }
            gs.phase = gs.PHASE_CHOOSE
            gs.priority_player = player
            gs.priority_pass_count = 0
            logging.debug(
                f"Waiting for {remaining} sacrifice-cost choice(s) before "
                f"activating {card.name}.")
            return 0.02, True

        # ActivatedAbility owns the full cost transaction. This preserves the
        # target-before-cost order while actually committing tap, sacrifice,
        # life, counter, and mana components together.
        costs_paid = ability.pay_cost(
            gs, player, sacrifice_choices=staged_choices,
            source_occurrence=ability_source_occurrence)
        
        if not costs_paid:
            logging.warning(f"Failed to pay cost for {card.name} ability {ability_idx}")
            return -0.05, False

        # CR 605.3: mana abilities resolve immediately and never use the
        # stack.  Variable-color production is still a player decision.
        if isinstance(ability, ManaAbility):
            produced = dict(getattr(ability, 'mana_produced', {}) or {})
            any_amount = int(produced.pop('any', 0) or 0)
            any_amount += int(produced.pop('choice', 0) or 0)
            if produced:
                gs.mana_system.add_mana(player, produced)
            if any_amount:
                gs.choice_context = {
                    'type': 'mana_ability_color', 'player': player,
                    'amount': any_amount, 'options': ['W', 'U', 'B', 'R', 'G'],
                    'resume_phase': gs.phase,
                }
                gs.phase = gs.PHASE_CHOOSE
                gs.priority_player = player
            return 0.1, True
        
        # ``ActivatedAbility.pay_cost`` is the single owner of Exhaust
        # bookkeeping. The handler still dispatches the activation event,
        # but must not mark the same ability a second time.
        if is_exhaust:
            # Trigger exhaust event
            if gs.ability_handler:
                exhaust_context = {"activator": player, "source_card_id": card_id, "ability_index": internal_idx}
                gs.ability_handler.check_abilities(card_id, "EXHAUST_ABILITY_ACTIVATED", exhaust_context)
                gs.ability_handler.process_triggered_abilities()
        
        stack_context = {
            "ability_index": internal_idx,
            "effect_text": effect_text,
            "ability": ability,
            "is_exhaust": is_exhaust,
            "targets": activation_targets or {}
        }
        gs.add_to_stack("ABILITY", card_id, player, stack_context)
        if context.get("commit_activation_targets"):
            gs.notify_targets_committed(
                card_id, player, activation_targets or {},
                stack_context=stack_context)
        logging.debug(f"Added ability {internal_idx} for {card.name} to stack")
        
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
        context = kwargs.get('context',{})
        player = self._get_policy_player(context)
        bf_idx = context.get('battlefield_idx', param)

        if bf_idx is None or not isinstance(bf_idx, int):
            logging.error(f"Loyalty ability handler called without valid param (battlefield_idx): {bf_idx}.")
            return -0.2, False

        if bf_idx >= len(player.get("battlefield", [])):
             logging.warning(f"Invalid battlefield index {bf_idx} for loyalty ability.")
             return -0.2, False

        card_id = player["battlefield"][bf_idx]
        card = gs._safe_get_card(card_id)
        if (not card or not getattr(card, "loyalty_abilities", [])
                or card_id not in player.get("loyalty_counters", {})):
            logging.warning(f"Card at index {bf_idx} ({getattr(card, 'name', 'N/A')}) has no live loyalty ability.")
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

    def _finalize_targeting_choice(self):
        """Commit the current target selection and resume its pending action."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        ctx = gs.targeting_context
        if not ctx or ctx.get("controller") != player:
            logging.warning("Cannot finalize targeting for this player.")
            return -0.2, False

        selected_targets = list(ctx.get("selected_targets", []))
        required_count = ctx.get("required_count", 1)
        min_targets = ctx.get("min_targets", required_count)
        max_targets = ctx.get("max_targets", required_count)
        if not min_targets <= len(selected_targets) <= max_targets:
            logging.warning(
                f"Cannot finalize {len(selected_targets)} targets; "
                f"expected between {min_targets} and {max_targets}.")
            return -0.1, False

        target_slots = ctx.get('target_slots') or []
        target_slot_index = int(ctx.get('target_slot_index', 0))
        if target_slots and target_slot_index + 1 < len(target_slots):
            ctx.setdefault('targets_by_slot', []).append(selected_targets)
            target_slot_index += 1
            next_slot = target_slots[target_slot_index]
            ctx['target_slot_index'] = target_slot_index
            ctx['selected_targets'] = []
            ctx['required_type'] = next_slot.get('required_type', 'target')
            ctx['effect_text'] = next_slot.get('effect_text', '')
            ctx['required_count'] = int(next_slot.get('required_count', 1))
            ctx['min_targets'] = int(
                next_slot.get('min_targets', ctx['required_count']))
            ctx['max_targets'] = int(
                next_slot.get('max_targets', ctx['required_count']))
            ctx['target_page'] = 0
            return 0.02, True

        if target_slots:
            targets_by_slot = list(ctx.get('targets_by_slot', [])) + [selected_targets]
            ctx['targets_by_slot'] = targets_by_slot
            committed_targets = [
                target_id
                for slot_targets in targets_by_slot
                for target_id in slot_targets
            ]
        else:
            targets_by_slot = []
            committed_targets = selected_targets

        if (ctx.get("resume_cast")
                and not gs._can_finalize_targeted_cast(ctx, committed_targets)):
            logging.warning(
                "Cannot finalize targeting: the committed targets leave the "
                "deferred spell unaffordable.")
            return -0.1, False

        categorized_targets = defaultdict(list)
        for target_id in committed_targets:
            categorized_targets[gs._determine_target_category(target_id)].append(target_id)
        categorized_targets = dict(categorized_targets)

        def restore_phase(priority_player):
            if "targeting_return_phase" in ctx:
                gs.phase = ctx.get("targeting_return_phase")
                gs.previous_priority_phase = ctx.get(
                    "targeting_return_previous_priority_phase")
            elif gs.previous_priority_phase is not None:
                gs.phase = gs.previous_priority_phase
                gs.previous_priority_phase = None
            else:
                gs.phase = gs.PHASE_PRIORITY
            gs.priority_player = priority_player
            gs.priority_pass_count = 0

        if ctx.get("resume_activation"):
            activation_context = dict(ctx.get("activation_context", {}))
            activation_context["activation_targets"] = categorized_targets
            activation_context["commit_activation_targets"] = True
            if targets_by_slot:
                activation_context["targets_by_slot"] = targets_by_slot
            gs.targeting_context = None
            restore_phase(player)
            return self._handle_activate_ability(None, activation_context)

        if ctx.get("resume_cast"):
            cast_context = dict(ctx.get("original_cast_context", {}))
            cast_context["targets"] = categorized_targets
            if targets_by_slot:
                cast_context["targets_by_slot"] = targets_by_slot
            card_id = ctx.get("source_id")
            controller = ctx.get("controller")
            gs.targeting_context = None
            restore_phase(controller)
            success = gs.cast_spell(card_id, controller, cast_context)
            if not success:
                logging.warning(
                    f"Could not finish casting target-priced spell {card_id} "
                    f"with targets {categorized_targets}.")
                return -0.1, False
            return 0.05, True

        # Loyalty abilities pay their cost before targeting, but are not put on
        # the stack until their targets are committed.  The old generic branch
        # only knew how to update an object already on the stack, so Kaito's -2
        # could never leave its target choice.
        stack_info = ctx.get("stack_info")
        if isinstance(stack_info, dict):
            stack_context = dict(stack_info.get("context", {}))
            stack_context["targets"] = categorized_targets
            if targets_by_slot:
                stack_context["targets_by_slot"] = targets_by_slot
            item_type = stack_info.get("item_type", "ABILITY")
            source_id = stack_info.get("source_id", ctx.get("source_id"))
            controller = stack_info.get("controller", ctx.get("controller"))
            if source_id is None or controller is None:
                logging.error("Targeted stack continuation is missing source/controller.")
                return -0.2, False
            gs.targeting_context = None
            gs.add_to_stack(item_type, source_id, controller, stack_context)
            gs.notify_targets_committed(
                source_id, controller, categorized_targets,
                stack_context=stack_context)
            # add_to_stack deliberately preserves special-choice phases.  A
            # loyalty ability now on the stack instead returns to priority over
            # the main phase saved in previous_priority_phase.
            gs.phase = gs.PHASE_PRIORITY
            gs.priority_player = controller
            gs.priority_pass_count = 0
            return 0.05, True

        pending_effect = ctx.get("resume_effect")
        if pending_effect is not None:
            source_id = ctx.get("source_id")
            controller = ctx.get("controller")
            continuation = ctx.get('effect_continuation')
            gs.targeting_context = None
            restore_phase(controller)
            try:
                gs.notify_targets_committed(source_id, controller, categorized_targets)
                result = pending_effect._apply_effect(
                    gs, source_id, controller, categorized_targets)
            except NotImplementedError:
                logging.error(
                    f"Effect application not implemented for: {pending_effect.effect_text}")
                return -0.15, False
            except Exception as exc:
                logging.exception(
                    f"Error resuming targeted effect '{pending_effect.effect_text}': {exc}")
                return -0.15, False
            if result is None:
                logging.warning(
                    f"Effect application returned None for: {pending_effect.effect_text}")
                return -0.15, False
            if continuation:
                continuation['success'] = bool(result) and bool(
                    continuation.get('success', True))
                next_choice = getattr(gs, 'choice_context', None)
                if (next_choice
                        and next_choice.get('type') in gs._ASYNC_EFFECT_CHOICE_TYPES):
                    next_choice['effect_continuation'] = continuation
                    return 0.05, True
                gs._run_effect_sequence(
                    continuation.get('effects', []),
                    continuation.get('source_id'),
                    gs._effect_controller_from_id(
                        continuation.get('controller_id')),
                    continuation.get('targets'),
                    continuation.get('resolution_context', {}),
                    finalizer=continuation.get('finalizer'),
                    initial_success=continuation.get('success', True))
                return 0.05, True
            return 0.05, bool(result)

        source_id = ctx.get("source_id")
        copy_instance_id = ctx.get("copy_instance_id")
        target_instance_id = ctx.get("target_instance_id")
        found_stack_item = False
        committed_stack_context = None
        if source_id is not None:
            for index in range(len(gs.stack) - 1, -1, -1):
                item = gs.stack[index]
                if not isinstance(item, tuple) or len(item) < 3 or item[1] != source_id:
                    continue
                stack_context = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
                if copy_instance_id and stack_context.get("copy_instance_id") != copy_instance_id:
                    continue
                if target_instance_id and stack_context.get("target_instance_id") != target_instance_id:
                    continue

                stack_context["targets"] = categorized_targets
                if targets_by_slot:
                    stack_context["targets_by_slot"] = targets_by_slot
                stack_context["target_choice_pending"] = False
                gs.stack[index] = item[:3] + (stack_context,)
                committed_stack_context = stack_context
                found_stack_item = True
                logging.debug(
                    f"Updated stack item {index} (Source: {source_id}) "
                    f"with targets: {categorized_targets}")
                break

        if not found_stack_item:
            logging.error(
                f"Targeting context active (Source: {source_id}) but no matching stack item exists.")
            gs.targeting_context = None
            restore_phase(gs._get_active_player())
            return -0.2, False

        gs.notify_targets_committed(
            source_id, ctx.get("controller"), categorized_targets,
            stack_context=committed_stack_context)
        gs.targeting_context = None
        if gs.start_pending_stack_target_choice():
            logging.debug("Targeting complete; another stack target choice is pending.")
            return 0.05, True
        restore_phase(gs._get_active_player())
        logging.debug("Targeting complete, returning to priority phase.")
        return 0.05, True

    def _handle_select_target(self, param, context, **kwargs):
        """Handle SELECT_TARGET, whose parameter indexes the valid target list."""
        gs = self.game_state
        player = gs.p1 if gs.agent_is_p1 else gs.p2
        target_choice_index = param

        if not gs.targeting_context or gs.targeting_context.get("controller") != player:
            logging.warning("SELECT_TARGET called but not in targeting phase for this player.")
            return -0.2, False

        ctx = gs.targeting_context
        required_count = ctx.get("required_count", 1)
        selected_targets = ctx.get("selected_targets", [])
        if not gs.targeting_system:
            logging.error("Targeting system not available during target selection.")
            return -0.15, False

        valid_targets_list = self._get_target_selection_candidates(player, ctx)

        absolute_index = int(ctx.get('target_page', 0)) * 10 + target_choice_index
        if not isinstance(target_choice_index, int) or not 0 <= absolute_index < len(valid_targets_list):
            logging.error(
                f"Invalid SELECT_TARGET action parameter: {target_choice_index}. "
                f"Valid indices: 0-{len(valid_targets_list) - 1}")
            return -0.1, False

        target_id = valid_targets_list[absolute_index]
        ctx['target_page'] = 0
        selected_targets.append(target_id)
        ctx["selected_targets"] = selected_targets
        logging.debug(
            f"Selected target {len(selected_targets)}/{required_count}: "
            f"{target_id} (Choice Index {target_choice_index})")

        max_targets = ctx.get("max_targets", required_count)
        if len(selected_targets) > max_targets:
            logging.error("Selected more targets than allowed.")
            return -0.15, False
        if len(selected_targets) == max_targets:
            return self._finalize_targeting_choice()
        return 0.02, True

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
        if choice_type in ["scry", "surveil", "explore"]:
            # Handle scry/surveil choices (PUT_ON_TOP, PUT_ON_BOTTOM, PUT_TO_GRAVEYARD)
            action_index = kwargs.get('action_index')
            
            if action_index == 306:  # PUT_ON_TOP
                return self._handle_scry_surveil_choice(param, context, action_index=306)
            elif action_index == 307:  # PUT_ON_BOTTOM
                if choice_type in ["surveil", "explore"]:
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
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'opening_hand'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Opening-hand choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_opening_hand_choice(param)
            return (0.05 if success else -0.1), success
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'forced_sacrifice'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Forced-sacrifice choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_forced_sacrifice_choice(param)
            return (0.0 if success else -0.1), success
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'keyword_grant'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if ctx.get('player') is not player:
                logging.warning("Keyword-grant choice called for the wrong player.")
                return -0.2, False
            options = ctx.get('options', [])
            if not (0 <= param < len(options)):
                return -0.1, False
            from .ability_types import GainKeywordEffect
            ok = GainKeywordEffect(
                options[param], target_type="target creature",
                duration=ctx.get('duration', 'end_of_turn')).apply(
                    gs, ctx.get('source_id'), player,
                    {"creatures": [ctx.get('target_id')]})
            gs.choice_context = None
            gs.phase = ctx.get('resume_phase', gs.PHASE_MAIN_PRECOMBAT)
            gs.previous_priority_phase = ctx.get(
                'previous_priority_phase_before_choice')
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            return (0.05 if ok else -0.1), bool(ok)
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'saddle'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if ctx.get('player') is not player:
                return -0.2, False
            options = ctx.get('options', [])
            if not (0 <= param < len(options)):
                return -0.1, False
            card_id = options[param]
            if card_id in ctx.get('selected', []) or card_id in player.get('tapped_permanents', set()):
                return -0.1, False
            ctx.setdefault('selected', []).append(card_id)
            try:
                power = max(0, int(getattr(gs._safe_get_card(card_id), 'power', 0) or 0))
            except (TypeError, ValueError):
                power = 0
            ctx['selected_power'] = ctx.get('selected_power', 0) + power
            return 0.0, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'hand_selection'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            if ctx.get('player') is not player or not (0 <= param < len(options)):
                return -0.1, False
            card_id = options[param]
            target_player = ctx['target_player']
            if card_id not in target_player.get('hand', []):
                return -0.1, False
            if not gs.move_card(card_id, target_player, 'hand', target_player, 'graveyard', cause='discard'):
                return -0.1, False
            if ctx.get('rummage'):
                gs._draw_phase(target_player)
            gs.phase = ctx.get('resume_phase', gs.PHASE_MAIN_PRECOMBAT)
            gs.choice_context = None
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'mana_ability_color'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            if ctx.get('player') is not player or not (0 <= param < len(options)):
                return -0.1, False
            gs.mana_system.add_mana(player, {options[param]: int(ctx.get('amount', 1))})
            gs.phase = ctx.get('resume_phase', gs.PHASE_PRIORITY)
            gs.choice_context = None
            # Mana abilities can be activated by the non-active player. The
            # color sub-choice must return priority to that same player.
            gs.priority_player = player
            gs.priority_pass_count = 0
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'activation_sacrifice_cost'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            option_occurrences = ctx.get('option_occurrences', [])
            absolute_param = int(ctx.get('choice_page', 0)) * 10 + param
            if (ctx.get('player') is not player
                    or not (0 <= absolute_param < len(options))
                    or len(option_occurrences) != len(options)):
                return -0.1, False
            card_id = options[absolute_param]
            occurrence = ActivatedAbility._as_sacrifice_occurrence(
                option_occurrences[absolute_param])
            selected = ctx.setdefault('selected', [])
            battlefield = player.get('battlefield', [])
            if (occurrence is None or occurrence in selected
                    or not 0 <= occurrence[1] < len(battlefield)
                    or battlefield[occurrence[1]] != card_id):
                return -0.1, False
            selected.append(occurrence)
            ctx['remaining'] = max(0, int(ctx.get('remaining', 1)) - 1)
            if ctx['remaining'] > 0:
                # Remove the exact physical slot, not every option sharing its
                # card id.  The action/observation layers continue to see ids.
                options.pop(absolute_param)
                option_occurrences.pop(absolute_param)
                ctx['options'] = options
                ctx['option_occurrences'] = option_occurrences
                ctx['choice_page'] = 0
                if len(ctx['options']) < ctx['remaining']:
                    return -0.1, False
                return 0.02, True

            activation_context = dict(ctx.get('activation_context', {}))
            activation_context['activation_sacrifice_occurrences'] = list(selected)
            activation_context['activation_sacrifice_ids'] = [
                selected_occurrence[0] for selected_occurrence in selected]
            resume_phase = ctx.get('resume_phase', gs.PHASE_PRIORITY)
            gs.choice_context = None
            gs.phase = resume_phase
            gs.priority_player = player
            gs.priority_pass_count = 0
            return self._handle_activate_ability(None, activation_context)
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'sacrifice_effect'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            absolute_param = int(ctx.get('choice_page', 0)) * 10 + param
            if ctx.get('player') is not player or not (0 <= absolute_param < len(options)):
                return -0.1, False
            card_id = options[absolute_param]
            ctx['choice_page'] = 0
            card = gs._safe_get_card(card_id)
            permanent_type = str(ctx.get('permanent_type', 'permanent')).rstrip('s')
            types = {str(t).lower() for t in getattr(card, 'card_types', [])} if card else set()
            if (card_id not in player.get('battlefield', [])
                    or (permanent_type != 'permanent' and permanent_type not in types)):
                return -0.1, False
            owner = gs._find_card_owner_fallback(card_id) or player
            if not gs.move_card(
                    card_id, player, 'battlefield', owner, 'graveyard',
                    cause='sacrifice'):
                return -0.1, False
            gs.trigger_ability(card_id, 'SACRIFICED', {'controller': player})
            ctx['optional'] = False
            ctx['remaining'] = max(0, int(ctx.get('remaining', 1)) - 1)
            remaining_candidates = [cid for cid in player.get('battlefield', [])
                                    if permanent_type == 'permanent'
                                    or permanent_type in {str(t).lower() for t in getattr(gs._safe_get_card(cid), 'card_types', [])}]
            if ctx['remaining'] > 0 and remaining_candidates:
                ctx['options'] = remaining_candidates
                return 0.02, True
            self._advance_or_finish_sacrifice_effect(ctx, performed=True)
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'distribute_counters'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            absolute_param = int(ctx.get('choice_page', 0)) * 10 + param
            if ctx.get('player') is not player or not (0 <= absolute_param < len(options)):
                return -0.1, False
            card_id = options[absolute_param]
            allocations = ctx.setdefault('allocations', {})
            allocations[card_id] = int(allocations.get(card_id, 0)) + 1
            ctx['remaining'] = max(0, int(ctx.get('remaining', 1)) - 1)
            if ctx['remaining'] > 0:
                return 0.02, True
            if any(int(allocations.get(target_id, 0)) <= 0 for target_id in options):
                logging.error("Counter distribution completed without assigning every target.")
                return -0.1, False
            for target_id, count in allocations.items():
                if not gs.add_counter(target_id, ctx.get('counter_type', '+1/+1'), count):
                    return -0.1, False
            gs._resume_effect_continuation(ctx)
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'dig_select'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            options = ctx.get('options', [])
            absolute_param = int(ctx.get('choice_page', 0)) * 10 + param
            if ctx.get('player') is not player or not (0 <= absolute_param < len(options)):
                return -0.1, False
            card_id = options[absolute_param]
            source_zone = ctx.get('source_zone', 'library_implicit')
            destination = ctx.get('destination', 'hand')
            if not gs.move_card(
                    card_id, player, source_zone, player, destination,
                    cause=ctx.get('move_cause', 'dig')):
                return -0.1, False
            options.pop(absolute_param)
            ctx['choice_page'] = 0
            ctx.setdefault('selected', []).append(card_id)
            ctx['remaining'] = max(0, int(ctx.get('remaining', 1)) - 1)
            if ctx['remaining'] > 0 and options:
                return 0.02, True
            self._finish_dig_select_choice(ctx)
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'optional_sacrifice_proliferate'):
            ctx = gs.choice_context
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            source_id = ctx.get('source_id')
            if ctx.get('player') is not player or param != 0 or source_id not in player.get('battlefield', []):
                return -0.1, False
            if not gs.move_card(source_id, player, 'battlefield', player, 'graveyard', cause='sacrifice'):
                return -0.1, False
            gs.proliferate(player, targets='all')
            gs.phase = ctx.get('resume_phase', gs.PHASE_MAIN_PRECOMBAT)
            gs.choice_context = None
            return 0.05, True
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'as_enters_creature_type'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Creature-type choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_as_enters_creature_type(param)
            return (0.05 if success else -0.1), success
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'mockingbird_copy'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Mockingbird copy choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_mockingbird_copy_choice(param)
            return (0.05 if success else -0.1), success
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'bargain'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Bargain choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_bargain_choice(param)
            return (0.05 if success else -0.1), success
        if (getattr(gs, 'choice_context', None)
                and gs.choice_context.get('type') == 'manifest_dread'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("Manifest dread choice called for the wrong player.")
                return -0.2, False
            success = gs.complete_manifest_dread_choice(param)
            return (0.05 if success else -0.1), success
        casting_choice = getattr(gs, 'choice_context', None)
        if casting_choice and casting_choice.get('type') in (
                'casting_additional_return', 'collect_evidence'):
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if casting_choice.get('player') is not player:
                logging.warning("Casting-cost choice called for the wrong player.")
                return -0.2, False
            if casting_choice.get('type') == 'casting_additional_return':
                success = gs.choose_casting_additional_return(param)
            else:
                success = gs.choose_collect_evidence_card(param)
            return (0.02 if success else -0.1), success
        # CR 603.3b / 510.1c: ordering choices share the 353-362 index range.
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'linked_exile':
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') is not player:
                logging.warning("LINKED_EXILE choice called for the wrong player.")
                return -0.2, False
            if gs.choose_linked_exile_card(param):
                return 0.05, True
            logging.warning(f"Invalid linked-exile option index {param}.")
            return -0.1, False
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'order_triggers':
            return self._handle_order_triggers(param, context)
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'order_blockers':
            return self._handle_order_blockers(param, context)
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'land_mana':
            player = gs.p1 if gs.agent_is_p1 else gs.p2
            if gs.choice_context.get('player') != player:
                logging.warning("LAND_MANA choice called for the wrong player.")
                return -0.2, False
            if gs.mana_system.complete_land_mana_choice(param):
                return 0.05, True
            logging.warning(f"Invalid land mana option index {param}.")
            return -0.1, False
        if getattr(gs, 'choice_context', None) and gs.choice_context.get('type') == 'mutate_position':
            if param not in (0, 1):
                logging.warning(f"Invalid mutate position choice: {param}")
                return -0.1, False
            if gs.complete_mutate_position_choice(mutate_on_top=(param == 0)):
                return 0.1, True
            logging.error("Could not complete the pending mutate position choice.")
            return -0.2, False
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

            if not gs.modal_mode_is_selectable(ctx, chosen_mode_idx):
                logging.warning(
                    "Mode %s for card %s has no legal mandatory targets.",
                    chosen_mode_idx, ctx.get('card_id'))
                return -0.1, False

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
                 if gs.finalize_modal_spell_choice():
                      return 0.1, True
                 logging.error("Failed to finalize completed modal choice.")
                 return -0.5, False
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

        if not gs.choice_context or gs.choice_context.get("type") != "choose_x" or gs.choice_context.get("player") != player:
            logging.warning("CHOOSE_X called out of context.")
            return -0.2, False
        if gs.choose_x_for_pending_spell(param):
            logging.debug(f"Chose X={param} and resumed casting.")
            return 0.05, True
        logging.error(f"Invalid or unaffordable X choice: {param}")
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
             if chosen_color not in set(ctx.get("available_colors", "WUBRG")):
                  logging.warning(f"Color {chosen_color} is not available for this choice.")
                  return -0.1, False
             ctx["chosen_color"] = chosen_color

             if ctx.get("resume_specialize"):
                  if gs.complete_specialize_choice(chosen_color):
                       return 0.05, True
                  logging.error(f"Could not complete Specialize with color {chosen_color}.")
                  return -0.15, False

             # --- FINALIZING LOGIC (Update Stack, Change Phase) ---
             found_stack_item = False
             source_id = ctx.get("source_id")
             copy_instance_id = ctx.get("copy_instance_id")
             if source_id is not None:
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
        player = self._get_policy_player(context)
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
        player = self._get_policy_player(context)
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
