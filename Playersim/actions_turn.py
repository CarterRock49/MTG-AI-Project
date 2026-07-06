"""Handlers for game flow: phases, priority, mulligans, conceding.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging


class TurnPhaseHandlersMixin:
    """Handlers for game flow: phases, priority, mulligans, conceding."""

    __slots__ = ()

    def should_hold_priority(self, player):
        """
        Determine if the player should hold priority based on game state.
        (Simplified version, can be expanded)
        """
        gs = self.game_state

        # Hold priority if stack is not empty and player has potential responses
        if gs.stack:
            # Check for instants/flash in hand
            if hasattr(gs, 'mana_system'):
                for card_id in player["hand"]:
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'card_types') and ('instant' in card.card_types or self._has_flash(card_id)):
                        if gs.mana_system.can_pay_mana_cost(player, getattr(card, 'mana_cost', "")):
                            return True

            # Check for activatable abilities
            if hasattr(gs, 'ability_handler'):
                for card_id in player["battlefield"]:
                    abilities = gs.ability_handler.get_activated_abilities(card_id)
                    for i in range(len(abilities)):
                        if gs.ability_handler.can_activate_ability(card_id, i, player):
                            return True
            return True # Hold priority if stack isn't empty, even without obvious responses for now

        # Hold priority during opponent's turn in certain phases (end step, combat)
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        if not is_my_turn and gs.phase in [gs.PHASE_END_STEP, gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS]:
             return True # Simplified: always consider holding priority on opponent's turn end/combat

        return False

    def check_mulligan_state(self):
            """
            Helper function to diagnose mulligan state inconsistencies.
            Fixed to target self.game_state attributes.
            """
            gs = self.game_state
            
            # Case 1: Both mulligan_player and bottoming_player are None but still in mulligan phase
            if gs.mulligan_in_progress and gs.mulligan_player is None and not gs.bottoming_in_progress:
                logging.error("Inconsistent state: In mulligan phase with no active mulligan player")
                unmade_decisions = 0
                for p, p_id in [(gs.p1, 'p1'), (gs.p2, 'p2')]:
                    if p and not p.get('_mulligan_decision_made', False):
                        unmade_decisions += 1
                        gs.mulligan_player = p
                        logging.info(f"Recovering mulligan state by assigning {p_id} as mulligan player")
                
                if unmade_decisions != 1:
                    logging.warning(f"Found {unmade_decisions} players with undecided mulligans. Forcing end of mulligan phase.")
                    self._end_mulligan_phase()
                    return False
                return True
            
            # Case 2: In bottoming phase but no bottoming player
            if gs.bottoming_in_progress and gs.bottoming_player is None:
                logging.error("Inconsistent state: In bottoming phase with no active bottoming player")
                needs_bottom_found = 0
                for p, p_id in [(gs.p1, 'p1'), (gs.p2, 'p2')]:
                    if p and p.get('_needs_to_bottom_next', False) and not p.get('_bottoming_complete', False):
                        needs_bottom_found += 1
                        gs.bottoming_player = p
                        gs.bottoming_count = 0
                        gs.cards_to_bottom = min(gs.mulligan_count.get(p_id, 0), len(p.get("hand", [])))
                        logging.info(f"Recovering bottoming state by assigning {p_id} as bottoming player")
                
                if needs_bottom_found != 1:
                    logging.warning(f"Found {needs_bottom_found} players needing to bottom. Forcing end of mulligan phase.")
                    self._end_mulligan_phase()
                    return False
                return True
            
            # Case 3: Neither mulligan nor bottoming in progress, but mulligan_in_progress flag is still set
            if gs.mulligan_in_progress and not gs.bottoming_in_progress and gs.mulligan_player is None:
                all_decided = True
                for p in [gs.p1, gs.p2]:
                    if p and not p.get('_mulligan_decision_made', False):
                        all_decided = False
                        break
                        
                if all_decided:
                    logging.info("All players have made mulligan decisions but phase not ended. Ending mulligan.")
                    self._end_mulligan_phase()
                    return False
                else:
                    logging.error("Inconsistent mulligan state: No bottoming, not all decided, but no mulligan_player")
                    self._end_mulligan_phase() 
                    return False
            
            # Case 4: Bottoming needed but stalled - check counters
            if gs.bottoming_in_progress and gs.bottoming_player:
                if gs.cards_to_bottom <= 0 or gs.bottoming_count >= gs.cards_to_bottom:
                    logging.error(f"Bottoming stalled: to_bottom={gs.cards_to_bottom}, count={gs.bottoming_count}")
                    gs.bottoming_player['_bottoming_complete'] = True
                    
                    other_player = gs.p2 if gs.bottoming_player == gs.p1 else gs.p1
                    if other_player and other_player.get('_needs_to_bottom_next', False) and not other_player.get('_bottoming_complete', False):
                        gs.bottoming_player = other_player
                        gs.bottoming_count = 0
                        other_id = 'p2' if other_player == gs.p2 else 'p1'
                        gs.cards_to_bottom = min(gs.mulligan_count.get(other_id, 0), len(other_player.get("hand", [])))
                        logging.info(f"Transitioning bottoming to next player: {other_player['name']}")
                    else:
                        logging.info("No more players need to bottom. Ending mulligan phase.")
                        self._end_mulligan_phase()
                        return False
            
            # Case 5: Final safety check - if in limbo, force end
            if (gs.mulligan_in_progress or gs.bottoming_in_progress) and gs.turn >= 1:
                logging.error("Critical inconsistency: In mulligan/bottoming but turn >= 1. Forcing end.")
                self._end_mulligan_phase()
                return False
            
            return True

    def _handle_no_op(self, param, context=None, **kwargs):
            """Handles NO_OP. Checks for stuck state and forces recovery."""
            gs = self.game_state
            logging.debug("Executed NO_OP action.")
            
            # Stuck State Recovery: 
            # If NO_OP executed when priority is None in a playable phase (not Untap/Cleanup),
            # it means the game state has lost track of the active actor.
            if gs.priority_player is None and gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP, gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                logging.warning("NO_OP executed while priority is None. Attempting recovery.")
                
                # 1. Try standard logic to assign priority
                gs._pass_priority() 
                
                # 2. Force assignment if standard logic skipped it (e.g. due to non-empty stack check failure)
                if gs.priority_player is None:
                    logging.warning("Recovery: Force assigning priority to Active Player.")
                    gs.priority_player = gs._get_active_player()
                    gs.priority_pass_count = 0
                
            return 0.0, True

    def _handle_end_turn(self, param, **kwargs):
        gs = self.game_state
        if gs.phase < gs.PHASE_END_STEP:
            gs.phase = gs.PHASE_END_STEP
            gs.priority_pass_count = 0 # Reset priority for end step
            gs.priority_player = gs._get_active_player()
            logging.debug("Fast-forwarding to End Step.")
        elif gs.phase == gs.PHASE_END_STEP:
            gs._pass_priority()
        return 0.0, True # Action logic succeeded

    def _handle_untap_next(self, param, **kwargs):
        gs = self.game_state
        gs._untap_phase(gs._get_active_player())
        gs.phase = gs.PHASE_UPKEEP
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return 0.01, True # Small reward, successful

    def _handle_draw_next(self, param, **kwargs):
        """Handle transitioning from draw step."""
        gs = self.game_state
        # Actually draw a card for the active player
        active_player = gs._get_active_player()
        if active_player["library"]:
            gs._draw_card(active_player)
        # Transition to main phase
        result, success = self._handle_phase_transition(gs.PHASE_DRAW, gs.PHASE_MAIN_PRECOMBAT, **kwargs)
        return (0.05, success) if success else result, success

    def _handle_phase_transition(self, current_phase, target_phase, **kwargs):
        """
        Centralized handler for phase transitions with improved validation and logging.
        
        Args:
            current_phase: Current game phase
            target_phase: Desired phase to transition to
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        
        # Validate current phase
        if gs.phase != current_phase:
            logging.warning(f"Phase transition attempted from incorrect phase. Expected: {current_phase}, Actual: {gs.phase}")
            return -0.1, False
        
        # Check if this transition is valid in the flow of gameplay
        valid_transitions = {
            gs.PHASE_MAIN_PRECOMBAT: [gs.PHASE_BEGIN_COMBAT],
            gs.PHASE_BEGIN_COMBAT: [gs.PHASE_DECLARE_ATTACKERS],
            gs.PHASE_DECLARE_ATTACKERS: [gs.PHASE_DECLARE_BLOCKERS],
            gs.PHASE_DECLARE_BLOCKERS: [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE],
            gs.PHASE_FIRST_STRIKE_DAMAGE: [gs.PHASE_COMBAT_DAMAGE],
            gs.PHASE_COMBAT_DAMAGE: [gs.PHASE_END_OF_COMBAT],
            gs.PHASE_END_OF_COMBAT: [gs.PHASE_MAIN_POSTCOMBAT],
            gs.PHASE_MAIN_POSTCOMBAT: [gs.PHASE_END_STEP],
            gs.PHASE_END_STEP: [gs.PHASE_CLEANUP],
            gs.PHASE_CLEANUP: [gs.PHASE_UNTAP],
            gs.PHASE_UNTAP: [gs.PHASE_UPKEEP],
            gs.PHASE_UPKEEP: [gs.PHASE_DRAW],
            gs.PHASE_DRAW: [gs.PHASE_MAIN_PRECOMBAT]
        }
        
        if target_phase not in valid_transitions.get(current_phase, []):
            logging.warning(f"Invalid phase transition: {current_phase} -> {target_phase}")
            return -0.05, False
        
        # Perform phase-specific checks
        if target_phase == gs.PHASE_DECLARE_ATTACKERS and current_phase == gs.PHASE_BEGIN_COMBAT:
            # Check if there are potential attackers
            active_player = gs._get_active_player()
            has_creatures = any('creature' in getattr(gs._safe_get_card(cid), 'card_types', []) 
                            for cid in active_player.get("battlefield", []))
            if not has_creatures:
                logging.debug("Skipping attack phase as there are no creatures to attack with")
                # Skip directly to end of combat
                gs.phase = gs.PHASE_END_OF_COMBAT
                return 0.01, True
        
        # Perform the transition
        gs.phase = target_phase
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        
        # Trigger any phase-specific effects
        if hasattr(gs, 'trigger_phase_change'):
            gs.trigger_phase_change(target_phase)
        
        logging.debug(f"Successfully transitioned from {current_phase} to {target_phase}")
        return 0.02, True

    def _handle_main_phase_end(self, param, **kwargs):
        """Handle transitioning from main phase."""
        gs = self.game_state
        if gs.phase == gs.PHASE_MAIN_PRECOMBAT:
            return self._handle_phase_transition(gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_BEGIN_COMBAT, **kwargs)
        elif gs.phase == gs.PHASE_MAIN_POSTCOMBAT:
            return self._handle_phase_transition(gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_END_STEP, **kwargs)
        else:
            logging.warning(f"MAIN_PHASE_END called during invalid phase: {gs.phase}")
            return -0.1, False

    def _handle_combat_damage(self, param, **kwargs):
        # This is often less of a choice and more a state transition.
        # If it's mapped to an action, it should just signify moving past the damage step.
        gs = self.game_state
        if hasattr(gs,'combat_resolver') and gs.combat_resolver:
            # Resolve might happen implicitly based on priority passes or combat actions.
            # This action might just confirm damage step is done?
            # Let's assume success means proceeding. Actual damage reward is separate.
            # If manual damage assignment is needed, that's a different action.
            logging.debug("COMBAT_DAMAGE action acknowledged (resolution likely handled elsewhere).")
            return 0.0, True # Proceeding is success
        logging.warning("COMBAT_DAMAGE action called but no combat resolver found.")
        return -0.1, False # Cannot proceed

    def _handle_end_phase(self, param, **kwargs):
        # Deprecated - Use specific phase end actions
        logging.warning("Generic END_PHASE action called (likely deprecated).")
        # Maybe map to PASS_PRIORITY?
        gs = self.game_state
        gs._pass_priority()
        return 0.0, True # Pass priority logic is successful

    def _handle_mulligan(self, param, **kwargs):
        """
        Improved handler for the MULLIGAN action with detailed tracking and error recovery.
        
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        # Use perspective player for the action, ensure they are the current mulligan player
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_mulligan_player = getattr(gs, 'mulligan_player', None)

        if current_mulligan_player != perspective_player:
             logging.warning(f"MULLIGAN action called by {perspective_player.get('name','?')}, but mulligan player is {getattr(current_mulligan_player,'name','None')}.")
             # Do not execute if it's not the correct player's turn to mulligan
             return -0.2, False # Invalid state/action timing

        player = current_mulligan_player # Use the validated player
        # ... (rest of validation logic from the original _handle_mulligan - checks count, hand size etc.) ...
        current_mulls = gs.mulligan_count.get('p1' if player == gs.p1 else 'p2', 0)
        hand_size = len(player.get("hand", []))
        if current_mulls >= 7: return -0.2, False
        if hand_size == 0: return -0.2, False

        result = None
        if hasattr(gs, 'perform_mulligan'):
            result = gs.perform_mulligan(player, keep_hand=False)
        else:
             logging.error("perform_mulligan missing from GameState.")
             return -0.2, False

        # Check result from perform_mulligan
        # perform_mulligan returns True if mulligan taken successfully
        if result is True:
            # Calculate penalty (remains the same)
            penalty = 0.05 * (current_mulls + 1)
            # Evaluate the quality of the hand just rejected
            # --- Hand evaluation needs to happen *before* the mulligan replaces the hand ---
            # This needs careful consideration: evaluate before calling perform_mulligan?
            # Let's skip hand evaluation for now to focus on fixing the flow.
            hand_quality = 0
            # Bigger penalty for mulliganing good hands
            adjusted_penalty = penalty # * (1 + hand_quality)
            logging.debug(f"Mulligan {current_mulls+1}: Penalty {-adjusted_penalty:.2f}")
            return -adjusted_penalty, True # Successful mulligan
        else:
            # If perform_mulligan returns False or None, it failed or was not allowed
            logging.warning(f"Mulligan action failed (perform_mulligan returned {result})")
            return -0.2, False

    def _handle_keep_hand(self, param, **kwargs):
        """
        Improved handler for the KEEP_HAND action with better validation and state transitions.
        
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_mulligan_player = getattr(gs, 'mulligan_player', None)

        if current_mulligan_player != perspective_player:
             logging.warning(f"KEEP_HAND action called by {perspective_player.get('name','?')}, but mulligan player is {getattr(current_mulligan_player,'name','None')}.")
             return -0.2, False # Invalid state/action timing

        player = current_mulligan_player # Use validated player

        result = None # perform_mulligan returns None for state transition, False for completion+no_draw
        if hasattr(gs, 'perform_mulligan'):
            result = gs.perform_mulligan(player, keep_hand=True)
        else:
            logging.error("perform_mulligan missing from GameState.")
            return -0.2, False

        # Process the result
        # Result = None: State transitioned (switched mulligan player or went to bottoming), requires new action mask. Success = True.
        # Result = False: Mulligan phase finished. Success = True.
        if result is None or result is False:
            # Evaluate hand quality (after keep decision)
            hand_quality = 0
            hand_size = len(player.get("hand", []))
            if self.card_evaluator and hand_size > 0:
                try: # Add try-except around evaluation
                     for card_id in player.get("hand", []):
                         hand_quality += self.card_evaluator.evaluate_card(card_id, "hand_strength") / max(1, hand_size)
                except Exception as e: logging.error(f"Error evaluating hand quality: {e}")

            reward = 0.05 + hand_quality * 0.1
            logging.debug(f"Keep hand: Reward {reward:.2f} (hand quality: {hand_quality:.2f})")
            return reward, True # Action processed successfully
        else: # Should not happen if perform_mulligan returns None/False correctly for keep
            logging.warning(f"Keep hand action failed (perform_mulligan returned unexpected {result})")
            return -0.1, False

    def _handle_bottom_card(self, param, context, **kwargs):
        """
        Improved handler for the BOTTOM_CARD action with better validation, evaluation, and tracking.
        
        Args:
            param: Hand index to bottom
            context: Additional context
            
        Returns:
            tuple: (reward, success_flag)
        """
        gs = self.game_state
        perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2
        current_bottoming_player = getattr(gs, 'bottoming_player', None)
        hand_idx_to_bottom = param # Param is already the index

        if current_bottoming_player != perspective_player:
            logging.warning(f"BOTTOM_CARD action called by {perspective_player.get('name','?')}, but bottoming player is {getattr(current_bottoming_player,'name','None')}.")
            return -0.2, False # Invalid state/action timing

        player = current_bottoming_player # Use validated player

        # --- Validation (moved from GameState method to Handler) ---
        if not getattr(gs, 'bottoming_in_progress', False):
            logging.warning("BOTTOM_CARD called but not in bottoming phase.")
            return -0.2, False
        if not isinstance(hand_idx_to_bottom, int) or hand_idx_to_bottom < 0:
             logging.warning(f"Invalid hand index for bottoming: {hand_idx_to_bottom}")
             return -0.15, False
        hand = player.get("hand", [])
        if hand_idx_to_bottom >= len(hand):
             logging.warning(f"Hand index {hand_idx_to_bottom} out of bounds (hand size: {len(hand)})")
             return -0.15, False
        # --- End Validation ---

        # Get card and evaluate before bottoming
        card_id = hand[hand_idx_to_bottom]
        card = gs._safe_get_card(card_id)
        card_value = 0
        if self.card_evaluator and card:
            try: # Add try-except around evaluation
                 eval_context = {"hand_size": len(hand), "mulligan_count": gs.mulligan_count.get('p1' if player == gs.p1 else 'p2', 0)}
                 card_value = self.card_evaluator.evaluate_card(card_id, "bottoming", context_details=eval_context)
            except Exception as e: logging.error(f"Error evaluating card for bottoming: {e}")

        # Attempt to bottom the card using game state
        success = False
        if hasattr(gs, 'bottom_card'):
            success = gs.bottom_card(player, hand_idx_to_bottom) # GS method handles the logic and state transitions
        else:
             logging.error("bottom_card method missing from GameState.")
             return -0.2, False

        # Calculate reward/penalty based on card value
        # More penalty for bottoming high-value cards
        reward_mod = -0.01 - (card_value * 0.05)
        # Apply reward based on success
        if success:
            # Check if bottoming is now complete (from GameState's perspective)
            if not gs.bottoming_in_progress and not gs.mulligan_in_progress:
                 final_reward = 0.05 + reward_mod
            elif not gs.bottoming_in_progress and gs.mulligan_in_progress: # Waiting for opponent bottoming
                 final_reward = 0.03 + reward_mod
            else: # More bottoming needed from this player
                 final_reward = 0.02 + reward_mod
            return final_reward, True
        else:
             logging.warning(f"Failed to bottom card at index {hand_idx_to_bottom} (GameState returned False).")
             return -0.05, False

    def _handle_upkeep_pass(self, param, **kwargs):
        """Handle transitioning from upkeep step."""
        gs = self.game_state
        
        # Ensure priority is assigned before transition
        if gs.priority_player is None:
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
            logging.warning("UPKEEP_PASS: Priority was None, assigned to active player")
        
        result, success = self._handle_phase_transition(gs.PHASE_UPKEEP, gs.PHASE_DRAW, **kwargs)
        
        # If transition succeeded, ensure priority is set for draw phase
        if success:
            gs.priority_player = gs._get_active_player()
            gs.priority_pass_count = 0
        
        return result, success

    def _handle_begin_combat_end(self, param, **kwargs):
        """Handle transitioning from begin combat step."""
        gs = self.game_state
        return self._handle_phase_transition(gs.PHASE_BEGIN_COMBAT, gs.PHASE_DECLARE_ATTACKERS, **kwargs)

    def _handle_end_combat(self, param, **kwargs):
        """Handle transitioning from end of combat step."""
        gs = self.game_state
        return self._handle_phase_transition(gs.PHASE_END_OF_COMBAT, gs.PHASE_MAIN_POSTCOMBAT, **kwargs)

    def _handle_end_step(self, param, **kwargs):
        """Handle transitioning from end step."""
        gs = self.game_state
        # Use phase transition for consistency
        result, success = self._handle_phase_transition(gs.PHASE_END_STEP, gs.PHASE_CLEANUP, **kwargs)
        # Apply end step effects if successful
        if success and hasattr(gs, '_end_step_effects'):
            gs._end_step_effects()
        return result, success

    def _handle_pass_priority(self, param, **kwargs):
        gs = self.game_state
        gs._pass_priority() # Let GameState handle the logic
        return 0.0, True # Action execution succeeded

    def _handle_concede(self, param, **kwargs):
        # Actual logic handled in apply_action check before handler call
        # This handler shouldn't technically be reached, but if it is:
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if me: me['lost_game'] = True
        logging.info("Player conceded.")
        return -10.0, True # Large penalty, action succeeded

    def _handle_no_op_search_fail(self, param, **kwargs):
        """Handles the dedicated NO_OP action when a search fails."""
        logging.debug("Executed NO_OP_SEARCH_FAIL action.")
        return 0.0, True # Action itself is always successful
