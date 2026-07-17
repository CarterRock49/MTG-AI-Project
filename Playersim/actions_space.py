"""Action-space generation: which actions are valid right now.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
import re
import numpy as np
from .card import Card
from .ability_utils import has_damage_prevention_instruction
from .debug import debug_log_valid_actions 
from .targeting import aura_cast_targeting_text


class ActionSpaceMixin:
    """Action-space generation: which actions are valid right now."""

    __slots__ = ()

    def _add_battle_attack_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_battle_attack_actions"""
        if self.combat_handler:
            self.combat_handler._add_battle_attack_actions(player, valid_actions, set_valid_action)

    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central checker."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if card_id is None: return False

        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            # Use AbilityHandler's check method if available
            if hasattr(gs.ability_handler, 'check_keyword'):
                 return gs.ability_handler.check_keyword(card_id, keyword)

        # Fallback: Check the card's own keyword array
        logging.warning(f"Using basic card keyword fallback check for {keyword} on {getattr(card, 'name', 'Unknown')}")
        if hasattr(card, 'has_keyword'):
             return card.has_keyword(keyword) # Assumes card object has checker
        return False

    def _mechanic_activation_context(self, player, battlefield_index, needle):
        """Return canonical activation context for a named mechanic effect."""
        gs = self.game_state
        battlefield = player.get("battlefield", [])
        if not 0 <= battlefield_index < len(battlefield):
            return None
        card_id = battlefield[battlefield_index]
        if not getattr(gs, "ability_handler", None):
            return None
        for ability_index, ability in enumerate(
                gs.ability_handler.get_activated_abilities(card_id)):
            effect = getattr(
                ability, "effect", getattr(ability, "effect_text", "")) or ""
            if (needle in effect.lower()
                    and gs.ability_handler.can_activate_ability(
                        card_id, ability_index, player)):
                return {
                    "battlefield_idx": battlefield_index,
                    "ability_idx": ability_index,
                    "controller_id": "p1" if player is gs.p1 else "p2",
                }
        return None

    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_multiple_blocker_actions"""
        if self.combat_handler:
            self.combat_handler._add_multiple_blocker_actions(player, valid_actions, set_valid_action)

    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_ninjutsu_actions"""
        if self.combat_handler:
            self.combat_handler._add_ninjutsu_actions(player, valid_actions, set_valid_action)

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_equipment_aura_actions"""
        if self.combat_handler:
            self.combat_handler._add_equipment_aura_actions(player, valid_actions, set_valid_action)

    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Delegate to CombatActionHandler._add_planeswalker_actions"""
        if self.combat_handler:
            self.combat_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)

    def generate_valid_actions(self):
            """
            Return the current action mask as boolean array with reasoning. 
            Includes CRITICAL STATE AUTO-CORRECTION to prevent infinite NO_OP loops.
            Handles all phases, sub-steps, and complex casting sequences.
            """
            gs = self.game_state
            self.last_mask_error = None
            try:
                valid_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                action_reasons = {} # Reset reasons for this generation
                overflow_action_catalog = []

                def set_valid_action(index, reason="", context=None):
                    # Ensures CONCEDE (12) isn't added here, handled at the end.
                    if 0 <= index < self.ACTION_SPACE_SIZE and index != 12:
                        action_type, _ = self.get_action_info(index)
                        if action_type not in self.action_handlers:
                            raise RuntimeError(
                                f"Mask attempted to expose unhandled action "
                                f"{index} ({action_type}): {reason}")
                        normalized_context = context or {}
                        existing = action_reasons.get(index)
                        if (valid_actions[index] and index >= 13 and index != 479
                                and existing
                                and existing.get("context", {}) != normalized_context):
                            # One fixed action id can represent only one live
                            # source. Preserve the first directly and route all
                            # colliding mechanic/source contexts through the
                            # shared paginated catalog.
                            overflow_entry = {
                                "label": reason or action_type,
                                "action_index": index,
                                "action_context": normalized_context,
                            }
                            # Multiple mask helpers may discover the same
                            # legal action (response helpers and general
                            # prevention helpers overlap, for example). A
                            # catalog entry represents a unique dispatch
                            # context, not the number of discovery paths.
                            if not any(
                                    entry.get("action_index") == index
                                    and entry.get("action_context", {}) ==
                                    normalized_context
                                    for entry in overflow_action_catalog):
                                overflow_action_catalog.append(overflow_entry)
                            return
                        valid_actions[index] = True
                        action_reasons[index] = {
                            "reason": reason, "context": normalized_context}
                    elif index != 12:
                        logging.error(f"INVALID ACTION INDEX during generation: {index} bounds (0-{self.ACTION_SPACE_SIZE-1}) Reason: {reason}")

                # --- 1. Player Validation & Perspective ---
                perspective_player = gs.p1 if gs.agent_is_p1 else gs.p2 # Player from whose view we generate
                if not gs.p1 or not gs.p2 or not perspective_player:
                    logging.error("Player object(s) missing or invalid. Defaulting to CONCEDE.")
                    valid_actions[12] = True; action_reasons[12] = {"reason": "Error: Players not initialized", "context": {}}
                    self.action_reasons_with_context = action_reasons; self.action_reasons = {k: v.get("reason","Err") for k, v in action_reasons.items()}
                    
                    debug_log_valid_actions(self.game_state, valid_actions, self.action_reasons_with_context, self.get_action_info)
                    return valid_actions

                current_turn_player = gs._get_active_player()

                # --- 1b. Orphaned-decision auto-correction ---
                # Choice/targeting/sacrifice actions are routed by phase, so a
                # pending context whose phase was overwritten by another
                # transition (e.g. a combat step finishing around a trigger's
                # as-enters choice) becomes unreachable and the episode decays
                # into a non-progressing PASS loop.  Restore the decision
                # phase; previous_priority_phase already records where play
                # resumes once the decision completes.
                pending_decision_phase = None
                pending_decision_context = None
                if getattr(gs, 'targeting_context', None):
                    pending_decision_phase = gs.PHASE_TARGETING
                    pending_decision_context = gs.targeting_context
                elif getattr(gs, 'sacrifice_context', None):
                    pending_decision_phase = gs.PHASE_SACRIFICE
                    pending_decision_context = gs.sacrifice_context
                elif getattr(gs, 'choice_context', None):
                    pending_decision_phase = gs.PHASE_CHOOSE
                    pending_decision_context = gs.choice_context
                if (pending_decision_phase is not None
                        and gs.phase not in (gs.PHASE_TARGETING,
                                             gs.PHASE_SACRIFICE,
                                             gs.PHASE_CHOOSE)):
                    logging.warning(
                        "Pending decision context found outside its decision "
                        "phase (phase %s); restoring phase %s so the mask can "
                        "reach it.", gs.phase, pending_decision_phase)
                    if getattr(gs, 'previous_priority_phase', None) is None:
                        gs.previous_priority_phase = gs.phase
                    gs.phase = pending_decision_phase
                    chooser = (pending_decision_context.get("controller")
                               or pending_decision_context.get("player"))
                    if chooser is not None:
                        gs.priority_player = chooser

                # --- 2. Mulligan Phase Logic ---
                if getattr(gs, 'mulligan_in_progress', False):
                    mulligan_player = getattr(gs, 'mulligan_player', None)
                    bottoming_player = getattr(gs, 'bottoming_player', None)

                    # Check if the perspective player needs to bottom cards
                    if bottoming_player == perspective_player:
                        hand_size = len(perspective_player.get("hand", []))
                        current_bottomed = getattr(gs, 'bottoming_count', 0)
                        total_needed = getattr(gs, 'cards_to_bottom', 0)
                        needed_now = max(0, total_needed - current_bottomed)

                        if needed_now > 0:
                            # Generate BOTTOM_CARD actions for valid hand indices
                            # Map indices 0-3 to actions 226-229
                            for i in range(min(hand_size, 4)): 
                                set_valid_action(226 + i, f"BOTTOM_CARD index {i}", context={'hand_idx': i})
                        else: 
                            set_valid_action(224, "NO_OP (Finished bottoming)")
                        return valid_actions

                    # Check if the perspective player needs to make a mulligan decision
                    elif mulligan_player == perspective_player:
                        set_valid_action(225, "KEEP_HAND")
                        # Can always mulligan if under limit
                        if gs.mulligan_count.get('p1' if perspective_player == gs.p1 else 'p2', 0) < 7:
                            set_valid_action(6, "MULLIGAN")
                        return valid_actions

                    # Check if perspective player is waiting for opponent
                    elif bottoming_player or mulligan_player:
                        set_valid_action(224, "NO_OP (Waiting for opponent)")
                        return valid_actions

                    # State Error: Mulligan in progress but no player assigned
                    # Allow NO_OP to cycle step and hopefully trigger external recovery
                    set_valid_action(224, "NO_OP (Mulligan Error Cycle)")
                    return valid_actions

                # --- 3. Special Choice Phase Logic (Targeting, Sacrifice, Choose) ---
                if gs.phase in [gs.PHASE_TARGETING, gs.PHASE_SACRIFICE, gs.PHASE_CHOOSE]:
                    special_phase_name = gs._PHASE_NAMES.get(gs.phase, "SPECIAL")
                    context = getattr(gs, 'targeting_context', None) or \
                            getattr(gs, 'sacrifice_context', None) or \
                            getattr(gs, 'choice_context', None)

                    if context:
                        acting_player = (context.get('controller')
                                         or context.get('player'))

                        if acting_player == perspective_player:
                            if gs.phase == gs.PHASE_TARGETING:
                                self._add_targeting_actions(
                                    perspective_player, valid_actions,
                                    set_valid_action)

                            elif gs.phase == gs.PHASE_SACRIFICE:
                                self._add_sacrifice_actions(
                                    perspective_player, valid_actions,
                                    set_valid_action)

                            elif gs.phase == gs.PHASE_CHOOSE:
                                self._add_special_choice_actions(
                                    perspective_player, valid_actions,
                                    set_valid_action)
                        else:
                            set_valid_action(
                                224,
                                f"NO_OP (Waiting for opponent in "
                                f"{special_phase_name})")

                        # If valid actions found (or NO_OP added), return.
                        if np.sum(valid_actions) > 0:
                            self.action_reasons_with_context = action_reasons
                            return valid_actions

                        # Fallback if a live context generated no actions.
                        set_valid_action(
                            224, f"NO_OP (Fallback {special_phase_name})")
                        return valid_actions
                    # No context means the transient wrapper is orphaned.
                    # Continue into ordinary priority generation; PASS then
                    # resolves a pending stack or lets _advance_phase restore
                    # the underlying turn phase.

                # --- 4. Regular Game Play & State Integrity Check ---
                
                priority_player_obj = getattr(gs, 'priority_player', None)

                # Phases where SOMEONE must have priority (all except Untap/Cleanup)
                interactive_phases = [
                    gs.PHASE_UPKEEP, gs.PHASE_DRAW, gs.PHASE_MAIN_PRECOMBAT, 
                    gs.PHASE_BEGIN_COMBAT, gs.PHASE_DECLARE_ATTACKERS, gs.PHASE_DECLARE_BLOCKERS, 
                    gs.PHASE_FIRST_STRIKE_DAMAGE, gs.PHASE_COMBAT_DAMAGE, gs.PHASE_END_OF_COMBAT, 
                    gs.PHASE_MAIN_POSTCOMBAT, gs.PHASE_END_STEP, gs.PHASE_PRIORITY
                ]

                if priority_player_obj is None and gs.phase in interactive_phases:
                    self.last_mask_error = (
                        f"priority is missing in interactive phase "
                        f"{gs._PHASE_NAMES.get(gs.phase, gs.phase)}")
                    logging.error("Invalid state during mask generation: %s", self.last_mask_error)

                # --- Check Priority Match ---
                has_priority = (priority_player_obj == perspective_player)

                if not has_priority:
                    # --- Perspective Player Does NOT Have Priority ---
                    # Allow NO_OP when waiting.
                    set_valid_action(224, "NO_OP (Waiting for priority)")
                    
                else:
                    # --- Perspective Player HAS Priority (or was just auto-assigned it) ---
                    set_valid_action(11, "PASS_PRIORITY") # Always allowed

                    split_second_is_active = getattr(gs, 'split_second_active', False)
                    
                    if split_second_is_active:
                        # Only add mana abilities (and PASS already added)
                        logging.debug("Split Second active, only allowing Mana abilities and PASS.")
                        self._add_mana_ability_actions(perspective_player, valid_actions, set_valid_action)
                        overflow_action_catalog.extend(
                            self._add_overflow_ability_catalog_action(
                                perspective_player, mana_only=True))
                    else:
                        is_my_turn = (current_turn_player == perspective_player)
                        opponent_player = gs.p2 if perspective_player == gs.p1 else gs.p1
                        
                        # Check Timing
                        can_act_sorcery_speed = gs._can_act_at_sorcery_speed(perspective_player)
                        can_act_instant_speed = gs.phase not in [gs.PHASE_UNTAP, gs.PHASE_CLEANUP]

                        # Sorcery Speed Actions
                        if can_act_sorcery_speed:
                            self._add_sorcery_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)
                            self._add_basic_phase_actions(is_my_turn, valid_actions, set_valid_action)
                            # Include Split Cards logic which handles Fuse/Split casting
                            self._add_split_card_actions(perspective_player, valid_actions, set_valid_action)

                        # Instant Speed Actions
                        if can_act_instant_speed:
                            self._add_instant_speed_actions(perspective_player, opponent_player, valid_actions, set_valid_action)
                            self._add_damage_prevention_actions(perspective_player, valid_actions, set_valid_action)

                        overflow_action_catalog.extend(
                            self._add_overflow_ability_catalog_action(
                                perspective_player))

                        # Combat Actions
                        if hasattr(self, 'combat_handler') and self.combat_handler:
                            active_p_gs = gs._get_active_player()
                            non_active_p_gs = gs._get_non_active_player()

                            if (gs.phase == gs.PHASE_DECLARE_ATTACKERS
                                    and perspective_player == active_p_gs
                                    and not gs.stack):
                                overflow_action_catalog.extend(
                                    self.combat_handler._add_attack_declaration_actions(
                                        perspective_player, non_active_p_gs,
                                        valid_actions, set_valid_action))
                            elif (gs.phase == gs.PHASE_DECLARE_BLOCKERS
                                    and perspective_player == non_active_p_gs
                                    and not gs.stack):
                                overflow_action_catalog.extend(
                                    self.combat_handler._add_block_declaration_actions(
                                        perspective_player, valid_actions,
                                        set_valid_action))

                            # Ninjutsu is activated after blockers are declared by the
                            # attacking player, so it must be exposed independently of
                            # the defending player's block-declaration actions above.
                            self.combat_handler._add_ninjutsu_actions(
                                perspective_player, valid_actions, set_valid_action)

                        # Pending Spell Contexts (Complex Casting)
                        pending_context = getattr(gs, 'pending_spell_context', None)
                        if pending_context and 'card_id' in pending_context and \
                        pending_context.get('controller') == perspective_player:
                            
                            card_id = pending_context['card_id']
                            card = gs._safe_get_card(card_id)
                            
                            # Kicker / Escalate / Additional Costs
                            self._add_kicker_options(perspective_player, valid_actions, set_valid_action)
                            
                            # Spree Modes
                            if getattr(card, 'is_spree', False):
                                self._add_spree_mode_actions(perspective_player, valid_actions, set_valid_action)

                            # Offspring Cost Payment
                            if card and getattr(card, 'is_offspring', False) and \
                            pending_context.get('pay_offspring') is None and \
                            pending_context.get('waiting_for_choice') == 'offspring_cost':
                                    cost_str = getattr(card, 'offspring_cost', None)
                                    if cost_str and self._can_afford_cost_string(perspective_player, cost_str, context=pending_context):
                                        offspring_context = {'action_source': 'offspring_payment_opportunity'}
                                        set_valid_action(295, f"Optional: PAY_OFFSPRING_COST for {getattr(card, 'name', card_id)}", context=offspring_context)

                if overflow_action_catalog:
                    set_valid_action(
                        479, "OPEN_OVERFLOW_ACTION_CATALOG",
                        context={
                            "open_action_catalog": True,
                            "catalog_type": "overflow_action",
                            "controller_id": (
                                "p1" if perspective_player is gs.p1 else "p2"),
                            "options": overflow_action_catalog,
                            "resume_phase": gs.phase,
                        })

                # Pass remains a dispatcher-level compatibility alias for old
                # replays, but the policy mask exposes exactly one declaration
                # completion action.  Offering PASS beside 438/439 doubled the
                # probability mass on "declare nothing / stop declaring" and
                # biased exploration away from adding attackers or blockers.
                if valid_actions[438] or valid_actions[439]:
                    valid_actions[11] = False
                    action_reasons.pop(11, None)

                # An incomplete declaration (for example one blocker on a
                # menace attacker) cannot expose either completion route.
                if (not gs.stack and gs.phase == gs.PHASE_DECLARE_BLOCKERS
                        and priority_player_obj == perspective_player
                        and perspective_player == gs._get_non_active_player()
                        and hasattr(self, 'combat_handler')
                        and self.combat_handler
                        and not self.combat_handler._can_finish_block_declaration()):
                    valid_actions[11] = False
                    action_reasons.pop(11, None)

                # --- 5. Final Concede Check ---
                self.action_reasons_with_context = action_reasons.copy()
                self.action_reasons = {k: v.get("reason","Unknown") for k, v in action_reasons.items()}
                
                num_valid_non_concede = np.sum(valid_actions)

                if num_valid_non_concede == 0:
                    # Only add CONCEDE if *truly* no other action (not even NO_OP or PASS) is available.
                    valid_actions[12] = True
                    concede_reason = "CONCEDE (No other valid actions)"
                    if 12 not in action_reasons:
                        action_reasons[12] = {"reason": concede_reason, "context": {}}
                        self.action_reasons_with_context[12] = action_reasons[12]; self.action_reasons[12] = concede_reason
                    logging.warning("generate_valid_actions: No valid actions found, only CONCEDE is available.")

                debug_log_valid_actions(self.game_state, valid_actions, self.action_reasons_with_context, self.get_action_info)
                return valid_actions

            except Exception as e:
                # Critical Fallback
                self.last_mask_error = f"{type(e).__name__}: {e}"
                logging.critical(f"CRITICAL error generating valid actions: {str(e)}", exc_info=True)
                fallback_actions = np.zeros(self.ACTION_SPACE_SIZE, dtype=bool)
                fallback_actions[11] = True # Pass Priority
                fallback_actions[12] = True # Concede
                self.action_reasons = {11: "Crit Err - PASS", 12: "Crit Err - CONCEDE"}
                self.action_reasons_with_context = {11: {"reason":"Crit Err - PASS","context":{}}, 12: {"reason":"Crit Err - CONCEDE","context":{}}}
                debug_log_valid_actions(self.game_state, fallback_actions, self.action_reasons_with_context, self.get_action_info)
                return fallback_actions

    def _add_basic_phase_actions(self, is_my_turn, valid_actions, set_valid_action):
        """Adds basic actions available based on the current phase, assuming priority and no stack."""
        gs = self.game_state

        # MAIN_PHASE_END (Action 3)
        if is_my_turn and gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT]:
            set_valid_action(3, f"End Main Phase {gs._PHASE_NAMES.get(gs.phase)}")

        # BEGIN_COMBAT_END (Action 8)
        if is_my_turn and gs.phase == gs.PHASE_BEGIN_COMBAT:
            set_valid_action(8, "End Begin Combat Step")

        # COMBAT_DAMAGE (Action 4) - Only if combat occurred and damage assignment is next
        # This action is less of a player choice and more a system transition.
        # Might be better handled by the combat handler logic triggering the phase change.
        # Let's *not* add it here, assuming the declare blockers done action transitions to damage steps.

        # END_COMBAT (Action 9)
        if is_my_turn and gs.phase == gs.PHASE_END_OF_COMBAT:
             set_valid_action(9, "End Combat Phase")

        # END_STEP (Action 10) - Renamed from END_PHASE to match ACTION_MEANINGS
        if is_my_turn and gs.phase == gs.PHASE_END_STEP:
            # Passing priority in End Step handles moving to Cleanup
            pass # Let PASS_PRIORITY handle this transition

        # UPKEEP_PASS (Action 7)
        if is_my_turn and gs.phase == gs.PHASE_UPKEEP:
            set_valid_action(7, "End Upkeep Step")

    def _add_mana_ability_actions(self, player, valid_actions, set_valid_action):
            """Add actions only for mana abilities (used during Split Second)."""
            gs = self.game_state
            if not hasattr(gs, 'ability_handler'): return

            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if not card: continue

                abilities = gs.ability_handler.get_activated_abilities(card_id)
                for j, ability in enumerate(abilities):
                    if j >= 3: break # Limit abilities per card

                    # Check if it's a Mana Ability specifically
                    is_mana_ability = False
                    if hasattr(gs.ability_handler, 'is_mana_ability') and callable(gs.ability_handler.is_mana_ability):
                        is_mana_ability = gs.ability_handler.is_mana_ability(ability)
                    elif isinstance(ability, gs.ability_handler.ManaAbility): # Fallback check if is_mana_ability doesn't exist
                        is_mana_ability = True

                    if is_mana_ability:
                        if gs.ability_handler.can_activate_ability(card_id, j, player):
                            # Map (battlefield_idx, ability_idx) to action index
                            action_idx = 100 + (i * 3) + j
                            if action_idx < 160: # Ensure it's within ACTIVATE_ABILITY range
                                set_valid_action(
                                    action_idx,
                                    f"MANA_ABILITY {card.name} ability {j}",
                                    context={
                                        "battlefield_idx": i,
                                        "ability_idx": j,
                                        "controller_id": "p1" if player is gs.p1 else "p2",
                                    })

            # Add tapping basic lands for mana (simplification)
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if card and 'land' in getattr(card, 'type_line', '') and card_id not in player.get("tapped_permanents", set()):
                    mana_options = (gs.mana_system._land_mana_options(player, card)
                                    if gs.mana_system else [])
                    if mana_options:
                        # Check if it's JUST a mana ability (no targets, no loyalty cost)
                        if ":" not in card.oracle_text.lower() or "{t}: add" in card.oracle_text.lower():
                            action_idx = 68 + i
                            if action_idx < 88: # Check it's within TAP_LAND_FOR_MANA range
                                set_valid_action(action_idx, f"TAP_LAND_FOR_MANA {card.name}")

    def _add_sorcery_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable only at sorcery speed. (Updated for Offspring/Impending)"""
        gs = self.game_state
        # --- Play Land ---
        if gs.can_play_land_this_turn(player):
            for i in range(min(len(player["hand"]), 10)):
                try:
                    card_id = player["hand"][i]
                    card = gs._safe_get_card(card_id)
                    if card and 'land' in getattr(card, 'type_line', '').lower():
                        # Pin both the observed card and acting seat.  The
                        # action index only encodes a hand slot; generated
                        # context makes execution reject stale/rebound slots
                        # instead of silently attempting a different card.
                        controller_id = "p1" if player is gs.p1 else "p2"
                        play_land_context = {
                            'hand_idx': i,
                            'card_id': card_id,
                            'controller_id': controller_id,
                        }
                        action_index = 13 + i if i < 7 else 393 + (i - 7)
                        set_valid_action(action_index, f"PLAY_LAND {card.name}", context=play_land_context)

                        # MDFC Land Back - Hand index 0-7 -> Actions 180-187
                        back_face_data = getattr(card, 'back_face', None)
                        if (i < 8 and hasattr(card, 'is_mdfc') and card.is_mdfc()
                                and back_face_data
                                and 'land' in back_face_data.get('type_line','').lower()):
                            mdfc_land_context = {
                                'hand_idx': i,
                                'card_id': card_id,
                                'controller_id': controller_id,
                                'play_back_face': True,
                            }
                            set_valid_action(180 + i, f"PLAY_MDFC_LAND_BACK {back_face_data.get('name', 'Unknown')}", context=mdfc_land_context)
                except IndexError:
                    logging.warning(f"IndexError accessing hand for PLAY_LAND at index {i}")
                    break # Stop if index is out of bounds

        # --- Play Sorcery-speed Spells ---
        for i in range(min(len(player["hand"]), 10)):
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'): continue

                # Determine if card is typically sorcery speed
                if not gs.can_player_cast_spells(player):
                    continue
                is_sorcery_speed_type = 'land' not in card.type_line.lower() and not ('instant' in card.card_types or self._has_flash(card_id))

                if is_sorcery_speed_type:
                    # Check base cost affordability FIRST for the standard PLAY_SPELL action
                    if (self._spell_cast_supported(card)
                            and self._can_afford_card(player, card, context={})):
                        if self._targets_available(card, player, opponent):
                            # --- STANDARD PLAY_SPELL ACTION ---
                            # Provide context: hand_idx
                            play_context = {
                                'hand_idx': i,
                                'card_id': card_id,
                                'controller_id': (
                                    'p1' if player is gs.p1 else 'p2'),
                            }
                            action_index = 20 + i if i < 8 else 396 + (i - 8)
                            set_valid_action(action_index, f"PLAY_SPELL {card.name}", context=play_context)
                            # Offer Kicker / Additional Cost Payment (If applicable) - Should check affordability
                            # ... Add checks for PAY_KICKER (405/406), PAY_ADDITIONAL_COST (407/408) based on card text ...

                    # --- OFFER ALTERNATIVE CASTING MODES (Impending) ---
                    if getattr(card, 'is_impending', False) and getattr(card, 'impending_cost', None):
                         if gs.mana_system.can_pay_replacing_cost_with_lands(
                                 player, card_id, card.impending_cost,
                                 'impending', context={'hand_idx': i}):
                              # Provide context: hand_idx
                              impending_context = {'hand_idx': i}
                              set_valid_action(294, f"Alt: CAST_FOR_IMPENDING {card.name}", context=impending_context)

                    # --- Other alternative/related actions (MDFC back, Adventure) ---
                    # Offer MDFC Spell Back (Sorcery) - Hand index 0-7 -> Actions 188-195
                    back_face_data = getattr(card, 'back_face', None)
                    if (i < 8 and hasattr(card, 'is_mdfc') and card.is_mdfc()
                            and getattr(card, 'layout', '') != 'adventure'
                            and back_face_data):
                        back_type_line = back_face_data.get('type_line','').lower()
                        back_types = [
                            str(card_type).lower()
                            for card_type in back_face_data.get('card_types', [])]
                        back_has_flash = self._has_flash_text(back_face_data.get('oracle_text',''))
                        if ('land' not in back_type_line
                                and 'instant' not in back_type_line
                                and not ('instant' in back_types or back_has_flash)):
                            if self._can_afford_card(player, back_face_data, is_back_face=True, context={}):
                                 if self._targets_available_from_data(
                                         back_face_data, player, opponent,
                                         source_id=card_id):
                                     mdfc_back_context = {
                                         'hand_idx': i,
                                         'cast_as_back_face': True,
                                     }
                                     set_valid_action(188 + i, f"PLAY_MDFC_BACK {back_face_data.get('name', 'Unknown')}", context=mdfc_back_context)

                    # Offer Adventure (Sorcery) - Hand index 0-7 -> Actions 196-203
                    if i < 8 and hasattr(card, 'has_adventure') and card.has_adventure():
                        adv_data = card.get_adventure_data()
                        if adv_data and ('sorcery' in adv_data.get('type','').lower() or 'instant' in adv_data.get('type','').lower()):
                            if self._can_afford_cost_string(player, adv_data.get('cost',''), context={}):
                                if self._targets_available_from_text(
                                        adv_data.get('effect',''), player,
                                        opponent, source_id=card_id):
                                    adventure_context = {'hand_idx': i, 'play_adventure': True}
                                    set_valid_action(196 + i, f"PLAY_ADVENTURE {adv_data.get('name', 'Unknown')}", context=adventure_context)

            except IndexError:
                 logging.warning(f"IndexError accessing hand for PLAY_SPELL at index {i}"); break

        # --- Other Sorcery-speed Actions ---
        self._add_plot_actions(player, valid_actions, set_valid_action)
        self._add_warp_actions(player, valid_actions, set_valid_action)
        self._add_prepared_cast_action(player, valid_actions, set_valid_action)
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        # PW abilities handled by _add_planeswalker_actions
        if hasattr(self, 'combat_handler') and self.combat_handler:
            self.combat_handler._add_planeswalker_actions(player, valid_actions, set_valid_action)

        self._add_level_up_actions(player, valid_actions, set_valid_action)
        self._add_unlock_door_actions(player, valid_actions, set_valid_action)
        # Renamed _add_equip_actions to _add_equipment_aura_actions (assuming it handles fortify/reconfigure too)
        if hasattr(self, '_add_equipment_aura_actions') and callable(self._add_equipment_aura_actions):
             self._add_equipment_aura_actions(player, valid_actions, set_valid_action)
        else: # Fallback if rename not done
            if hasattr(self, '_add_equip_actions') and callable(self._add_equip_actions): self._add_equip_actions(player, valid_actions, set_valid_action)

        self._add_morph_actions(player, valid_actions, set_valid_action)
        self._add_exile_casting_actions(player, valid_actions, set_valid_action)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)
        self._add_emblem_graveyard_actions(
            player, opponent, valid_actions, set_valid_action,
            is_sorcery_speed=True)
        self._add_specific_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=True)

    def _add_instant_speed_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions performable at instant speed. (Updated for Offspring/Impending)"""
        gs = self.game_state
        # --- Play Instant/Flash Spells and instant-speed alternate faces ---
        for i in range(min(len(player["hand"]), 10)):
            try:
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line') or not hasattr(card, 'card_types'): continue

                if not gs.can_player_cast_spells(player):
                    continue
                is_instant_speed = 'instant' in card.card_types or self._has_flash(card_id)

                if (is_instant_speed and 'land' not in card.type_line.lower()
                        and self._spell_cast_supported(card)
                        and self._can_afford_card(player, card, context={})
                        and self._targets_available(card, player, opponent)):
                    play_context = {
                        'hand_idx': i,
                        'card_id': card_id,
                        'controller_id': 'p1' if player is gs.p1 else 'p2',
                    }
                    action_index = 20 + i if i < 8 else 396 + (i - 8)
                    set_valid_action(
                        action_index, f"PLAY_SPELL (Instant) {card.name}",
                        context=play_context)

                # The front of an MDFC/Adventure is commonly a sorcery-speed
                # permanent. Its instant back/adventure half is nevertheless
                # castable while only instant-speed actions are available.
                if i >= 8:
                    continue
                back_face_data = getattr(card, 'back_face', None)
                if (hasattr(card, 'is_mdfc') and card.is_mdfc()
                        and getattr(card, 'layout', '') != 'adventure'
                        and back_face_data):
                    back_type_line = back_face_data.get('type_line', '').lower()
                    back_types = [
                        str(card_type).lower()
                        for card_type in back_face_data.get('card_types', [])]
                    back_has_flash = self._has_flash_text(
                        back_face_data.get('oracle_text', ''))
                    if (('instant' in back_type_line or 'instant' in back_types
                            or back_has_flash)
                            and self._can_afford_card(
                                player, back_face_data, is_back_face=True,
                                context={})
                            and self._targets_available_from_data(
                                back_face_data, player, opponent,
                                source_id=card_id)):
                        set_valid_action(
                            188 + i,
                            f"PLAY_MDFC_BACK {back_face_data.get('name', 'Unknown')}",
                            context={
                                'hand_idx': i,
                                'cast_as_back_face': True,
                            })

                if hasattr(card, 'has_adventure') and card.has_adventure():
                    adventure_data = card.get_adventure_data()
                    if (adventure_data
                            and 'instant' in adventure_data.get('type', '').lower()
                            and self._can_afford_cost_string(
                                player, adventure_data.get('cost', ''), context={})
                            and self._targets_available_from_text(
                                adventure_data.get('effect', ''), player,
                                opponent, source_id=card_id)):
                        set_valid_action(
                            196 + i,
                            f"PLAY_ADVENTURE {adventure_data.get('name', 'Unknown')}",
                            context={'hand_idx': i, 'play_adventure': True})

            except IndexError:
                 logging.warning(f"IndexError accessing hand for Instant/Flash spell at index {i}"); break

        # --- Other instant speed actions (no changes needed) ---
        self._add_warp_actions(player, valid_actions, set_valid_action,
                               require_instant_speed=True)
        self._add_prepared_cast_action(player, valid_actions, set_valid_action)
        self._add_morph_actions(player, valid_actions, set_valid_action)
        self._add_ability_activation_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        self._add_land_tapping_actions(player, valid_actions, set_valid_action)
        self._add_alternative_casting_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)
        self._add_emblem_graveyard_actions(
            player, opponent, valid_actions, set_valid_action,
            is_sorcery_speed=False)
        self._add_cycling_actions(player, valid_actions, set_valid_action)
        if gs.stack: self._add_response_actions(player, valid_actions, set_valid_action)
        self._add_specific_mechanics_actions(player, valid_actions, set_valid_action, is_sorcery_speed=False)

    def _targets_available_from_data(
            self, card_data, caster, opponent=None, source_id=None):
        """Check target availability from card data dict."""
        oracle_text = card_data.get('oracle_text', '').lower()
        type_line = str(card_data.get('type_line', '') or '').lower()
        card_types = {
            str(card_type).lower()
            for card_type in card_data.get('card_types', [])}
        is_spell = bool(card_types.intersection({'instant', 'sorcery'})) \
            or 'instant' in type_line or 'sorcery' in type_line
        is_aura = 'aura' in {
            str(subtype).lower()
            for subtype in card_data.get('subtypes', [])} \
            or 'aura' in type_line
        if not is_spell and not is_aura:
            return True
        if is_aura and 'target' not in oracle_text:
            enchant_match = re.search(
                r"\benchant\s+([^\n.(]+)", oracle_text)
            if not enchant_match:
                return False
            oracle_text = f"target {enchant_match.group(1).strip()}."
        elif 'target' not in oracle_text:
            return True
        card_id = card_data.get('id')
        if card_id is None:
            card_id = source_id
        return self._targets_available_from_text(
            oracle_text, caster, opponent, source_id=card_id)

    def _targets_available_from_text(
            self, effect_text, caster, opponent=None, source_id=None):
        """Check mandatory target availability through canonical legality."""
        gs = self.game_state
        if 'target' not in effect_text.lower(): return True
        if source_id is None or not getattr(gs, 'targeting_system', None):
            return False
        gift_branches = gs._gift_targeting_texts(effect_text)
        if gift_branches:
            return any(self._targets_available_from_text(
                branch, caster, opponent, source_id=source_id)
                for branch in gift_branches)
        try:
            target_slots = gs._ordinary_target_slots(effect_text)
            if target_slots:
                for slot in target_slots:
                    minimum = int(slot.get('min_targets', 0))
                    if minimum == 0:
                        continue
                    valid_targets = gs.targeting_system.get_valid_targets(
                        source_id, caster,
                        slot.get('required_type', 'target'),
                        effect_text=slot.get('effect_text', ''))
                    valid_ids = {
                        target_id
                        for targets in valid_targets.values()
                        for target_id in targets}
                    if len(valid_ids) < minimum:
                        return False
                return True
            targeting_text = gs._ordinary_single_targeting_text(effect_text)
            minimum, _ = gs._target_bounds_from_text(targeting_text)
            if minimum == 0:
                return True
            target_type = gs._get_target_type_from_text(targeting_text)
            valid_targets = gs.targeting_system.get_valid_targets(
                source_id, caster, target_type,
                effect_text=targeting_text)
            valid_ids = {
                target_id
                for targets in valid_targets.values()
                for target_id in targets}
            return len(valid_ids) >= minimum
        except Exception as error:
            logging.warning(
                "Target availability probe failed for source %r: %s",
                source_id, error)
            return False

    def _add_special_choice_actions(self, player, valid_actions, set_valid_action):
        """Add actions for discard and the other dedicated choice phases."""
        gs = self.game_state
        if gs.phase != gs.PHASE_CHOOSE: # Only generate these during the dedicated CHOICE phase
            return

        if hasattr(gs, 'choice_context') and gs.choice_context:
            context = gs.choice_context
            choice_type = context.get("type")
            source_id = context.get("source_id")
            choice_player = context.get("player")

            if choice_player != player: # Not this player's choice
                set_valid_action(11, "PASS_PRIORITY (Waiting for opponent choice)")
                return

            # Discard -- the fixed action range exposes one ten-card page.
            if choice_type in [
                    "discard", "specialize_discard", "connive_discard"]:
                hand = player.get("hand", [])
                page_count = max(1, (len(hand) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for page_index, card_id in enumerate(
                        hand[page * 10:(page + 1) * 10]):
                    if card_id in context.get("selected_current", []):
                        continue
                    if (choice_type == "specialize_discard"
                            and not gs.get_specialize_discard_colors(card_id).intersection(
                                context.get("available_colors", []))):
                        continue
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        238 + page_index,
                        f"DISCARD_CARD {card_name}",
                        context={"hand_idx": page * 10 + page_index},
                    )
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"DISCARD_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            # CR 103.6c: begin-game battlefield placements from the opening
            # hand (Leylines). PASS declines only the current card.
            elif choice_type == "opening_hand":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"BEGIN_GAME_ON_BATTLEFIELD {card_name}",
                        context={"option_index": option_index},
                    )
                set_valid_action(11, "DECLINE_OPENING_HAND_PLACEMENT")

            # Forced sacrifice (Phyrexian Obliterator): the player picks each
            # of their own permanents to sacrifice; the choice is mandatory.
            elif choice_type == "forced_sacrifice":
                battlefield = player.get("battlefield", [])
                page_count = max(1, (len(battlefield) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(
                        battlefield[page * 10:(page + 1) * 10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"FORCED_SACRIFICE {card_name}",
                        context={"option_index": option_index},
                    )
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"FORCED_SACRIFICE_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "keyword_grant":
                options = list(context.get("options", []))
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get("choice_page", 0)) % page_count
                for option_index, keyword in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    set_valid_action(
                        353 + option_index,
                        f"CHOOSE_KEYWORD {keyword}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"KEYWORD_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "saddle":
                selected = set(context.get("selected", []))
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    if card_id in selected:
                        continue
                    card = gs._safe_get_card(card_id)
                    set_valid_action(353 + option_index,
                                     f"SADDLE_TAP {getattr(card, 'name', card_id)}",
                                     context={"option_index": option_index})
                if context.get("selected_power", 0) >= context.get("required_power", 0):
                    set_valid_action(11, "FINISH_SADDLE")

            elif choice_type == "hand_selection":
                options = context.get("options", [])
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    card = gs._safe_get_card(card_id)
                    set_valid_action(353 + option_index,
                                     f"CHOOSE_HAND_CARD {getattr(card, 'name', card_id)}",
                                     context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"HAND_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                if context.get('optional'):
                    set_valid_action(11, "DECLINE_HAND_CARD")

            elif choice_type == "prepared_source":
                options = list(context.get("options", []))
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get("choice_page", 0)) % page_count
                for option_index, card_id in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    face = gs._prepare_spell_face(gs._safe_get_card(card_id)) or {}
                    set_valid_action(
                        353 + option_index,
                        f"PREPARED_CAST {face.get('name', card_id)}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"PREPARED_SOURCE_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "prepared_payment":
                options = list(context.get("options", []))
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get("choice_page", 0)) % page_count
                selected = len(context.get("selected_cards", []))
                required = int(context.get("required_count", 8))
                for option_index, card_id in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    card = gs._safe_get_card(card_id)
                    set_valid_action(
                        353 + option_index,
                        f"PREPARE_EXILE {getattr(card, 'name', card_id)} "
                        f"({selected + 1}/{required})",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"PREPARE_PAYMENT_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                set_valid_action(11, "DECLINE_PREPARE_PAYMENT")

            elif choice_type in {"mana_ability_color", "mana_ability_package"}:
                for option_index, color in enumerate(context.get('options', [])[:5]):
                    remaining = int(context.get('remaining', 1))
                    set_valid_action(353 + option_index,
                                     f"ADD {color} MANA ({remaining} left)",
                                     context={"option_index": option_index})

            elif choice_type == "mana_ability_output":
                for option_index, package in enumerate(
                        context.get('options', [])[:10]):
                    label = "".join(
                        f"{{{symbol}}}" * int(amount)
                        for symbol, amount in package.items()
                        if symbol not in {'any', 'choice', 'any_combination'})
                    if package.get('any') or package.get('choice'):
                        label += "ANY_COLOR"
                    if package.get('any_combination'):
                        label += "ANY_COMBINATION"
                    set_valid_action(
                        353 + option_index,
                        f"CHOOSE_MANA_OUTPUT {label or package}",
                        context={"option_index": option_index})

            elif choice_type == "ward_payment":
                options = list(context.get("options", []))
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get("choice_page", 0)) % page_count
                kind = context.get("payment_kind", "payment")
                for option_index, option in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    # Ward choices may be declarative tokens such as ``pay``
                    # rather than card IDs.  Looking those up through
                    # ``_safe_get_card`` emits a false missing-card warning.
                    card = (gs.card_db.get(option)
                            if isinstance(option, (int, str)) else None)
                    label = getattr(card, "name", option)
                    set_valid_action(
                        353 + option_index,
                        f"PAY_WARD_{kind.upper()} {label}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"WARD_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                set_valid_action(11, "DECLINE_WARD_PAYMENT")

            elif choice_type == "activation_sacrifice_cost":
                # Choice construction/execution owns the option list.  Mask
                # generation is observational and must not rewrite live state
                # (or desynchronize the parallel option_occurrences list).
                candidates = list(context.get('options', []))
                page_count = max(1, (len(candidates) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(
                        candidates[page * 10:(page + 1) * 10]):
                    if card_id not in player.get('battlefield', []):
                        continue
                    card = gs._safe_get_card(card_id)
                    set_valid_action(
                        353 + option_index,
                        f"SACRIFICE_COST {getattr(card, 'name', card_id)}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"SACRIFICE_COST_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "activation_discard_cost":
                candidates = list(context.get('options', []))
                page_count = max(1, (len(candidates) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(
                        candidates[page * 10:(page + 1) * 10]):
                    if card_id not in player.get('hand', []):
                        continue
                    card = gs._safe_get_card(card_id)
                    set_valid_action(
                        353 + option_index,
                        f"DISCARD_ACTIVATION_COST {getattr(card, 'name', card_id)}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"DISCARD_COST_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "sacrifice_effect":
                candidates = list(context.get('options', []))
                page_count = max(1, (len(candidates) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(candidates[page * 10:(page + 1) * 10]):
                    card = gs._safe_get_card(card_id)
                    if card_id not in player.get('battlefield', []):
                        continue
                    set_valid_action(353 + option_index,
                                     f"SACRIFICE_EFFECT {getattr(card, 'name', card_id)}",
                                     context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"SACRIFICE_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                if context.get('optional'):
                    set_valid_action(11, "DECLINE_SACRIFICE_EFFECT")

            elif choice_type == "distribute_counters":
                all_options = context.get('options', [])
                allocations = context.get('allocations', {})
                unassigned = [card_id for card_id in all_options if not allocations.get(card_id)]
                legal_options = (unassigned if int(context.get('remaining', 0)) <= len(unassigned)
                                 else all_options)
                page_count = max(1, (len(all_options) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                page_options = all_options[page * 10:(page + 1) * 10]
                for option_index, card_id in enumerate(page_options):
                    if card_id not in legal_options:
                        continue
                    card = gs._safe_get_card(card_id)
                    set_valid_action(353 + option_index,
                                     f"PUT_{context.get('counter_type', '+1/+1')}_COUNTER {getattr(card, 'name', card_id)}",
                                     context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"COUNTER_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            elif choice_type == "dig_select":
                options = context.get('options', [])
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, card_id in enumerate(options[page * 10:(page + 1) * 10]):
                    card = gs._safe_get_card(card_id)
                    set_valid_action(353 + option_index,
                                     f"DIG_TAKE {getattr(card, 'name', card_id)}",
                                     context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"DIG_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                if context.get('optional'):
                    set_valid_action(11, "DECLINE_DIG_SELECTION")

            elif choice_type == "resolution_choice":
                options = context.get('options', [])
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, option in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    # Resolution choices also contain symbolic options
                    # (``pay``, ``counters``, ``spirit``).  Only resolve an
                    # option as a card when it is actually present in card_db.
                    card = (gs.card_db.get(option)
                            if isinstance(option, (int, str)) else None)
                    label = getattr(card, 'name', option)
                    set_valid_action(
                        353 + option_index,
                        f"RESOLUTION_CHOICE {label}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"CHOICE_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})
                if context.get('optional'):
                    set_valid_action(11, "DECLINE_RESOLUTION_CHOICE")

            elif choice_type == "optional_sacrifice_proliferate":
                source_id = context.get('source_id')
                if source_id in player.get('battlefield', []):
                    set_valid_action(353, "SACRIFICE_AND_PROLIFERATE",
                                     context={"option_index": 0})
                set_valid_action(11, "DECLINE_SACRIFICE")

            # Mandatory "as enters" choices are committed before ETB events.
            elif choice_type.startswith("as_enters_"):
                choice_label = choice_type[len("as_enters_"):].upper()
                for option_index, option in enumerate(context.get("options", [])[:10]):
                    set_valid_action(
                        353 + option_index,
                        f"CHOOSE_{choice_label} {option}",
                        context={"option_index": option_index},
                    )

            # Non-target choices made while paying a spell's casting cost.
            elif choice_type == "mockingbird_copy":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"COPY_AS {card_name}",
                        context={"option_index": option_index},
                    )
                set_valid_action(11, "DECLINE_MOCKINGBIRD_COPY")

            elif choice_type == "bargain":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"BARGAIN {card_name}",
                        context={"option_index": option_index},
                    )
                set_valid_action(11, "DECLINE_BARGAIN")

            elif choice_type == "gift":
                card = gs._safe_get_card(context.get("card_id"))
                branches = gs._gift_targeting_texts(
                    getattr(card, "oracle_text", "") if card else "")
                if len(branches) == 2:
                    if self._targets_available_from_text(
                            branches[1], choice_player,
                            source_id=context.get("card_id")):
                        set_valid_action(
                            353, "PROMISE_GIFT",
                            context={"option_index": 0})
                    if self._targets_available_from_text(
                            branches[0], choice_player,
                            source_id=context.get("card_id")):
                        set_valid_action(11, "DECLINE_GIFT")

            elif choice_type == "manifest_dread":
                for option_index, card_id in enumerate(context.get("options", [])[:2]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"MANIFEST_DREAD {card_name}",
                        context={"option_index": option_index},
                    )

            elif choice_type == "casting_additional_return":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"RETURN_FOR_ADDITIONAL_COST {card_name}",
                        context={"option_index": option_index},
                    )

            elif choice_type == "collect_evidence":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    mana_value = getattr(card, "cmc", 0) if card else 0
                    set_valid_action(
                        353 + option_index,
                        f"COLLECT_EVIDENCE {card_name} (MV {mana_value})",
                        context={"option_index": option_index},
                    )
                selected = context.get("selected_cards", [])
                if (not selected
                        or context.get("selected_mana_value", 0) >= context.get("threshold", 0)):
                    label = "FINISH_COLLECT_EVIDENCE" if selected else "DECLINE_COLLECT_EVIDENCE"
                    set_valid_action(11, label)

            # Optional card selection inside a linked temporary-exile effect.
            elif choice_type == "linked_exile":
                for option_index, card_id in enumerate(context.get("options", [])[:10]):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, "name", card_id)
                    set_valid_action(
                        353 + option_index,
                        f"EXILE_LINKED_CARD {card_name}",
                        context={"option_index": option_index},
                    )
                if context.get("optional", False):
                    set_valid_action(11, "DECLINE_LINKED_EXILE")

            # Scry / Surveil / Explore
            elif choice_type in ["scry", "surveil", "explore"] and context.get("cards"):
                card_id = context["cards"][0] # Process one card at a time
                card = gs._safe_get_card(card_id)
                card_name = getattr(card, 'name', card_id)
                set_valid_action(306, f"PUT_ON_TOP {card_name}") # Action for Top - Index 306 maps to PUT_ON_TOP
                if choice_type == "scry":
                    set_valid_action(307, f"PUT_ON_BOTTOM {card_name}") # Action for Bottom - Index 307 maps to PUT_ON_BOTTOM
                else: # Surveil / Explore
                    set_valid_action(305, f"PUT_TO_GRAVEYARD {card_name}") # Action for GY - Index 305 maps to PUT_TO_GRAVEYARD

            # Dredge (Replace Draw)
            elif choice_type == "dredge" and "card_id" in context:
                card_id = context.get("card_id")
                dredge_val = context.get("value")
                if len(player["library"]) >= dredge_val:
                    # Find card index in graveyard
                    gy_idx = -1
                    for idx, gy_id in enumerate(player.get("graveyard", [])):
                        if gy_id == card_id and idx < 6: # GY Index 0-5 (Action space limited)
                            gy_idx = idx
                            break
                    if gy_idx != -1:
                        # Provide context for DREDGE action handler
                        dredge_action_context = {'gy_idx': gy_idx}
                        set_valid_action(308, f"DREDGE {gs._safe_get_card(card_id).name}", context=dredge_action_context)
                # Allow skipping the dredge replacement
                set_valid_action(11, "Skip Dredge") # PASS_PRIORITY effectively skips

            # Order blockers for damage assignment (CR 510.1c) - indices 353-362
            elif choice_type == "order_blockers":
                pending = context.get("pending", [])
                atk_card = gs._safe_get_card(context.get("attacker_id"))
                atk_name = getattr(atk_card, 'name', '?')
                for i, bid in enumerate(pending[:10]):
                    b_card = gs._safe_get_card(bid)
                    b_name = getattr(b_card, 'name', bid)
                    set_valid_action(353 + i, f"ASSIGN_DAMAGE {atk_name} -> {b_name} next")

            # Order simultaneous triggers (CR 603.3b) - reuses indices 353-362
            elif choice_type == "order_triggers":
                pending = context.get("pending", [])
                for i, entry in enumerate(pending[:10]):
                    ability = entry[0]
                    card = gs._safe_get_card(getattr(ability, 'card_id', None))
                    name = getattr(card, 'name', getattr(ability, 'card_id', '?'))
                    set_valid_action(353 + i, f"ORDER_TRIGGER {name} onto stack next")

            elif choice_type == "copy_retarget_slots":
                index = int(context.get("slot_index", 0))
                slots = context.get("slots", [])
                if 0 <= index < len(slots):
                    target = slots[index].get("target_id")
                    card = gs._safe_get_card(target)
                    label = getattr(card, "name", target)
                    set_valid_action(353, f"KEEP_COPY_TARGET {label}")
                    # Expose retarget only if a distinct legal replacement is
                    # available for this individual inherited target.
                    valid = gs.targeting_system.get_valid_targets(
                        context.get("source_id"), player,
                        slots[index].get("required_type", "target"),
                        effect_text=context.get("effect_text", ""))
                    if any(candidate != target
                           for values in valid.values() for candidate in values):
                        set_valid_action(354, f"RETARGET_COPY_SLOT {index + 1}")

            elif choice_type == "action_catalog":
                options = list(context.get("options", []))
                page_count = max(1, (len(options) + 9) // 10)
                page = int(context.get("choice_page", 0)) % page_count
                for option_index, entry in enumerate(
                        options[page * 10:(page + 1) * 10]):
                    set_valid_action(
                        353 + option_index,
                        f"CATALOG_ACTION {entry.get('label', option_index)}",
                        context={"option_index": option_index})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479,
                        f"ACTION_CATALOG_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            # A resolving mutating creature spell goes over or under its target.
            elif choice_type == "mutate_position":
                set_valid_action(353, "PUT_MUTATE_OVER")
                set_valid_action(354, "PUT_MUTATE_UNDER")

            # Select one of a land's mana abilities. This range includes
            # colorless and restricted outputs that CHOOSE_COLOR cannot model.
            elif choice_type == "land_mana":
                for i, option in enumerate(context.get("options", [])[:10]):
                    label = f"CHOOSE_LAND_MANA {option.get('symbol', '?')}"
                    if option.get("damage"):
                        label += f" ({option['damage']} damage)"
                    set_valid_action(353 + i, label)

            # Choose Mode
            elif choice_type in {"choose_mode", "resolution_modal"}:
                num_choices = context.get("num_choices", 0)
                max_modes = context.get("max_required", 1)
                min_modes = context.get("min_required", 1)
                selected_count = len(context.get("selected_modes", []))

                # Allow choosing another mode if max not reached
                if selected_count < max_modes:
                    for i in range(min(num_choices, 10)): # Mode index 0-9
                        # Prevent selecting the same mode twice unless allowed
                        if (i not in context.get("selected_modes", [])
                                and gs.modal_mode_is_selectable(context, i)):
                            # FIXED: Use correct index range 353-362 for CHOOSE_MODE
                            set_valid_action(353 + i, f"CHOOSE_MODE {i+1}")

                # Allow finalizing choice if minimum met (and min != max)
                if selected_count >= min_modes and min_modes != max_modes:
                    set_valid_action(11, "PASS_PRIORITY (Finish Mode Choice)")

            elif choice_type == "harmonize_tap":
                for option_index, creature_id in enumerate(
                        context.get("options", [])[:10]):
                    creature = gs._safe_get_card(creature_id)
                    set_valid_action(
                        353 + option_index,
                        f"HARMONIZE_TAP {getattr(creature, 'name', creature_id)}")
                if context.get("can_decline"):
                    set_valid_action(11, "HARMONIZE_DONT_TAP")

            elif choice_type == "trigger_mode":
                for option_index, _ in enumerate(context.get("options", [])[:10]):
                    set_valid_action(
                        353 + option_index,
                        f"CHOOSE_TRIGGER_MODE {option_index + 1}")

            # Choose X
            elif choice_type == "choose_x":
                values = list(context.get('affordable_values', []))
                if not values:
                    min_x = int(context.get("min_x", 0))
                    max_x = int(context.get("max_x", 0))
                    values = list(range(min_x, max_x + 1))
                if 0 in values:
                    set_valid_action(11, "CHOOSE_X_VALUE 0")
                positive_values = [value for value in values if value > 0]
                page_count = max(1, (len(positive_values) + 9) // 10)
                page = int(context.get('choice_page', 0)) % page_count
                for option_index, x_value in enumerate(
                        positive_values[page * 10:(page + 1) * 10]):
                    set_valid_action(
                        363 + option_index, f"CHOOSE_X_VALUE {x_value}",
                        context={"x_value": x_value})
                if page + 1 < page_count:  # one-way paging: no 479 on the final page
                    set_valid_action(
                        479, f"CHOOSE_X_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

            # Choose Color
            elif choice_type == "choose_color":
                available_colors = set(context.get("available_colors", "WUBRG"))
                for i in range(5): # Color index 0-4 (WUBRG)
                    if ['W','U','B','R','G'][i] not in available_colors:
                        continue
                    # FIXED: Use correct index range 373-377 for CHOOSE_COLOR
                    set_valid_action(373 + i, f"CHOOSE_COLOR {['W','U','B','R','G'][i]}")

            # Kicker / Additional Cost / Escalate Choices (Using correct indices now)
            elif choice_type == "pay_kicker":
                set_valid_action(405, "PAY_KICKER") # Param=True
                set_valid_action(406, "DONT_PAY_KICKER") # Param=False
            elif choice_type == "pay_additional":
                set_valid_action(407, "PAY_ADDITIONAL_COST") # Param=True
                set_valid_action(408, "DONT_PAY_ADDITIONAL_COST") # Param=False
            elif choice_type == "pay_escalate":
                max_extra = context.get('max_modes', 1) - context.get('num_selected', 1)
                for i in range(min(max_extra, 3)): # Allow paying for 1, 2, or 3 extra modes max
                    num_extra = i + 1
                    # Check affordability of paying N times
                    escalate_cost = context.get('escalate_cost_each')
                    if escalate_cost and gs.mana_system.can_pay_mana_cost(player, f"{escalate_cost}*{num_extra}"):
                            escalate_action_context = {'num_extra_modes': num_extra}
                            set_valid_action(409, f"PAY_ESCALATE for {num_extra} extra mode(s)", context=escalate_action_context)
                set_valid_action(11, "PASS_PRIORITY (Finish Escalate/Don't pay)")

        else:
            # If no choice context is active during PHASE_CHOOSE, allow PASS
            set_valid_action(11, "PASS_PRIORITY (No choices pending?)")

    def _add_sacrifice_actions(self, player, valid_actions, set_valid_action):
         """Add SACRIFICE_PERMANENT actions when in the sacrifice phase."""
         gs = self.game_state
         if hasattr(gs, 'sacrifice_context') and gs.sacrifice_context:
             context = gs.sacrifice_context
             source_id = context.get('source_id')
             source_card = gs._safe_get_card(source_id)
             source_name = source_card.name if source_card and hasattr(source_card, 'name') else source_id
             required_count = context.get('required_count', 1)
             selected_count = len(context.get('selected_permanents', []))

             # Determine valid permanents to sacrifice based on context (e.g., 'creature', 'artifact')
             permanent_type_req = context.get('required_type')
             valid_permanents = []
             for i, perm_id in enumerate(player["battlefield"]):
                  perm_card = gs._safe_get_card(perm_id)
                  if not perm_card: continue
                  if not permanent_type_req or permanent_type_req in getattr(perm_card, 'card_types', []):
                       valid_permanents.append(perm_id)

             # Generate SACRIFICE_PERMANENT actions
             if selected_count < required_count:
                 for i, perm_id in enumerate(valid_permanents):
                     if i >= 10: break # Limit to action space indices 284-293
                     perm_card = gs._safe_get_card(perm_id)
                     perm_name = perm_card.name if perm_card and hasattr(perm_card, 'name') else perm_id
                     set_valid_action(284 + i, f"SACRIFICE ({i}): {perm_name} for {source_name}")
             else:
                  set_valid_action(11, "PASS_PRIORITY (Sacrifices selected)")

    def _add_targeting_actions(self, player, valid_actions, set_valid_action):
        """Add SELECT_TARGET actions when in the targeting phase."""
        gs = self.game_state
        if hasattr(gs, 'targeting_context') and gs.targeting_context:
            context = gs.targeting_context
            source_id = context.get('source_id')
            source_card = gs._safe_get_card(source_id)
            source_name = source_card.name if source_card and hasattr(source_card, 'name') else source_id
            required_count = context.get('required_count', 1)
            min_targets = context.get('min_targets', required_count)
            max_targets = context.get('max_targets', required_count)
            selected_count = len(context.get('selected_targets', []))

            if not getattr(gs, 'targeting_system', None):
                logging.warning(
                    "Targeting system not available, cannot generate specific targeting actions.")
            valid_targets_list = self._get_target_selection_candidates(
                player, context)

            # Generate SELECT_TARGET actions for available targets
            if context.get('allow_keep_original_targets') and selected_count == 0:
                set_valid_action(11, "KEEP_ORIGINAL_TARGETS")
            elif (selected_count >= min_targets
                  and gs._can_finalize_targeted_cast(
                      context, context.get('selected_targets', []))):
                set_valid_action(11, "FINISH_TARGET_SELECTION")
            if selected_count < max_targets:
                page = int(context.get('target_page', 0))
                page_count = max(1, (len(valid_targets_list) + 9) // 10)
                if page >= page_count:
                    page = 0
                page_targets = valid_targets_list[page * 10:(page + 1) * 10]
                for i, target_id in enumerate(page_targets):
                    if isinstance(target_id, str) and target_id in ["p1", "p2"]:
                        target_name = "Player 1" if target_id == "p1" else "Player 2"
                    else:
                        target_card = gs._safe_get_card(target_id)
                        target_name = (target_card.name
                                       if target_card and hasattr(target_card, 'name')
                                       else target_id)
                    set_valid_action(274 + i, f"SELECT_TARGET ({i}): {target_name} for {source_name}")
                # TARGET_PAGE_NEXT is a one-way policy action.  Keeping it
                # legal on the final page lets a deterministic policy cycle
                # between pages forever without selecting or finishing.
                if page + 1 < page_count:
                    set_valid_action(
                        479, f"TARGET_PAGE_NEXT ({page + 1}/{page_count})",
                        context={"page_count": page_count})

    def _add_level_up_actions(self, player, valid_actions, set_valid_action):
        """Add actions for leveling up Class cards and leveler creatures."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 5)): # Class index 0-4
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_class') and card.is_class and hasattr(card, 'can_level_up') and card.can_level_up():
                 next_level = card.current_level + 1
                 cost = card.get_level_cost(next_level)
                 if self._can_afford_cost_string(player, cost):
                     set_valid_action(
                         253 + i, f"LEVEL_UP_CLASS {card.name} to {next_level}",
                         context={
                             "battlefield_idx": i, "card_id": card_id,
                             "controller_id": "p1" if player is gs.p1 else "p2",
                         })
        # Leveler creatures (CR 711): repeatable "Level up {cost}", sorcery-speed.
        for i in range(min(len(player["battlefield"]), 5)):  # Leveler index 0-4
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and getattr(card, 'is_leveler', False) and getattr(card, 'level_up_cost', None):
                if self._can_afford_cost_string(player, card.level_up_cost):
                    set_valid_action(
                        467 + i, f"LEVEL_UP_CREATURE {card.name}",
                        context={
                            "battlefield_idx": i, "card_id": card_id,
                            "controller_id": "p1" if player is gs.p1 else "p2",
                        })

    def _add_unlock_door_actions(self, player, valid_actions, set_valid_action):
        """Add actions for unlocking Room doors."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 5)): # Room index 0-4
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if not card or not getattr(card, "is_room", False):
                continue
            transaction = gs.ability_handler.get_unlockable_room_door(
                player, i, room_id=card_id)
            if transaction:
                set_valid_action(
                    248 + i, f"UNLOCK_DOOR {card.name}",
                    context={
                        "battlefield_idx": i,
                        "card_id": card_id,
                        "controller_id": "p1" if player is gs.p1 else "p2",
                        "door_number": transaction["door_number"],
                    })

    def _add_equip_actions(self, player, valid_actions, set_valid_action):
        gs = self.game_state
        # Identify creatures and equipment indices on player's battlefield
        creature_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                             if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])]
        equipment_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                              if gs._safe_get_card(cid) and 'equipment' in getattr(gs._safe_get_card(cid), 'subtypes', [])]

        action_map = {} # Store unique (type, param) to action index

        for eq_idx, equip_id in equipment_indices:
            if eq_idx >= 10: continue # Action space limit for source? Maybe rethink this index mapping.
            equip_card = gs._safe_get_card(equip_id)
            cost_match = re.search(r"equip (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
            if cost_match:
                 cost_str = cost_match.group(1)
                 if cost_str.isdigit(): cost_str = f"{{{cost_str}}}" # Normalize cost
                 if self._can_afford_cost_string(player, cost_str):
                      # Allow equipping to each creature
                      for c_idx, creature_id in creature_indices:
                           # Map (eq_idx, c_idx) to a unique action index if needed, or use tuple param directly.
                           # Let's assume EQUIP action uses tuple param (equip_idx, creature_idx)
                           param_tuple = (eq_idx, c_idx)
                           # Need a way to map this tuple back to a *single* action index like 445.
                           # Current ACTION_MEANINGS for 445 doesn't support this complex param well.
                           # Compromise: Use action 445, but the handler expects a tuple passed via context.
                           # Agent needs to provide this context. Set action as valid, assuming agent handles context.
                           set_valid_action(445, f"EQUIP {equip_card.name} to {gs._safe_get_card(creature_id).name}")

            # Reconfigure
            if "reconfigure" in getattr(equip_card, 'oracle_text', '').lower():
                cost_match = re.search(r"reconfigure (\{[^\}]+\}|[0-9]+)", getattr(equip_card, 'oracle_text', '').lower())
                if cost_match:
                    cost_str = cost_match.group(1)
                    if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                    if self._can_afford_cost_string(player, cost_str):
                         # Reconfigure needs the equip_idx. Action 449 assumes this.
                         set_valid_action(449, f"RECONFIGURE {equip_card.name}") # Param = eq_idx

        # Unequip
        if hasattr(player, "attachments"):
            for equip_id, target_id in player["attachments"].items():
                equip_card = gs._safe_get_card(equip_id)
                if equip_card and 'equipment' in getattr(equip_card, 'subtypes', []):
                    # Find index of equipment on battlefield
                    eq_idx = -1
                    for i, cid in enumerate(player["battlefield"]):
                        if cid == equip_id:
                            eq_idx = i
                            break
                    if eq_idx != -1:
                         set_valid_action(446, f"UNEQUIP {equip_card.name}") # Param = eq_idx

    def _add_morph_actions(self, player, valid_actions, set_valid_action):
         """Add actions for turning Morph/Manifest cards face up."""
         gs = self.game_state
         morph_added = False
         manifest_added = False
         for i in range(min(len(player["battlefield"]), 20)):
             card_id = player["battlefield"][i]
             if not morph_added and card_id in getattr(gs, "morphed_cards", {}):
                 info = gs.morphed_cards[card_id]
                 original = info.get("original_printed", {})
                 if not original and info.get("original"):
                     original_card = Card(info["original"])
                     original = original_card._printed
                 cost_match = re.search(
                     r"morph\s*((?:\{[^}]+\})+)",
                     original.get("oracle_text", ""), re.IGNORECASE)
                 if (cost_match and self._can_afford_cost_string(
                         player, cost_match.group(1))):
                     set_valid_action(
                         455, f"TURN_MORPH_FACE_UP {original.get('name', card_id)}",
                         context={"battlefield_idx": i})
                     morph_added = True
             if not manifest_added and card_id in getattr(gs, "manifested_cards", {}):
                 original = gs.manifested_cards[card_id].get(
                     "original_printed", {})
                 if ("creature" in original.get("card_types", [])
                         and self._can_afford_cost_string(
                             player, original.get("mana_cost", ""))):
                     set_valid_action(
                         456, f"TURN_MANIFEST_FACE_UP {original.get('name', card_id)}",
                         context={"battlefield_idx": i})
                     manifest_added = True

    def _add_attack_target_actions(self, player, opponent, valid_actions, set_valid_action, possible_attackers):
        """Add actions for choosing targets for attackers (Planeswalkers, Battles)."""
        gs = self.game_state
        if not getattr(gs, 'current_attackers', []):
            return
        # Attacker ID needs to be associated with the target choice.
        # Current approach assumes the *last declared attacker* is the one choosing target.

        # Planeswalkers
        opponent_planeswalkers = [(idx, card_id) for idx, card_id in enumerate(opponent["battlefield"])
                                   if gs._safe_get_card(card_id) and 'planeswalker' in getattr(gs._safe_get_card(card_id), 'card_types', [])]
        for i in range(min(len(opponent_planeswalkers), 5)): # PW index 0-4
            pw_idx, pw_id = opponent_planeswalkers[i]
            pw_card = gs._safe_get_card(pw_id)
            set_valid_action(378 + i, f"ATTACK_PLANESWALKER {pw_card.name}")

        # Battles
        opponent_battles = [(idx, card_id) for idx, card_id in enumerate(opponent["battlefield"])
                             if gs._safe_get_card(card_id) and 'battle' in getattr(gs._safe_get_card(card_id), 'type_line', '')]
        for battle_idx_rel, (abs_idx, battle_id) in enumerate(opponent_battles):
            if battle_idx_rel >= 5: break # Battle index 0-4 relative to available battles
            battle_card = gs._safe_get_card(battle_id)
            set_valid_action(462 + battle_idx_rel, f"ATTACK_BATTLE {battle_card.name}")

    def _can_use_specialized_response_cast(self, card_id, player, card):
        """Whether a response shortcut may cast without announcement steps.

        The 430-434 handlers pass response-specific target context directly to
        ``cast_spell``.  Modal and Spree cards must announce modes first, so
        they must use the ordinary PLAY_SPELL flow instead of these shortcuts.
        """
        gs = self.game_state
        cast_context = {
            'source_zone': 'hand',
            'hand_idx': (player.get('hand', []).index(card_id)
                         if card_id in player.get('hand', []) else None),
        }
        if not gs._can_cast_now(card_id, player, context=cast_context):
            return False
        if getattr(card, 'is_spree', False):
            return False
        parser = getattr(getattr(gs, 'ability_handler', None),
                         '_parse_modal_text', None)
        if parser:
            try:
                modes, _, _ = parser(getattr(card, 'oracle_text', ''))
                if modes:
                    return False
            except Exception:
                # A parser error is not permission to bypass announcement.
                return False
        return True

    def _add_response_actions(self, player, valid_actions, set_valid_action):
        """Add actions for responding to stack (counters, etc.)."""
        gs = self.game_state
        if not gs.can_player_cast_spells(player):
            return
        if not gs.stack: return

        stack_has_opponent_spell = any(isinstance(item, tuple) and item[0] == "SPELL" and item[2] != player for item in gs.stack)
        stack_has_opponent_ability = any(isinstance(item, tuple) and item[0] == "ABILITY" and item[2] != player for item in gs.stack)

        # Counter Spell - Using correct action index 430
        if stack_has_opponent_spell:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target spell" in card.oracle_text.lower():
                    if (self._can_use_specialized_response_cast(
                            card_id, player, card)
                            and self._can_afford_card(player, card)):
                        # "Counter target spell" can carry rider clauses
                        # ("with mana value 2", Spell Snare) that cast_spell
                        # validates through the targeting system.  The mask
                        # must apply the same validation and aim the context
                        # at a spell that is actually a legal target, or a
                        # mask-valid 430 fails execution and strict training
                        # aborts the run (round-7.91 v2 run-stopper).
                        try:
                            valid_targets_map = (
                                gs.targeting_system.get_valid_targets(
                                    card_id, player, "spell",
                                    effect_text=card.oracle_text))
                        except Exception as targeting_error:
                            logging.warning(
                                "Skipping COUNTER_SPELL mask for %s: target "
                                "validation failed (%s)",
                                getattr(card, 'name', card_id),
                                targeting_error)
                            continue
                        valid_spell_ids = {
                            target_id
                            for ids in valid_targets_map.values()
                            for target_id in ids
                        }
                        counter_context = {'hand_idx': i}
                        for stack_idx, item in enumerate(gs.stack):
                            if (isinstance(item, tuple)
                                    and item[0] == "SPELL"
                                    and item[2] != player
                                    and item[1] in valid_spell_ids):
                                counter_context['target_spell_idx'] = stack_idx
                                break
                        if 'target_spell_idx' not in counter_context:
                            continue
                        set_valid_action(430, f"COUNTER_SPELL with {card.name}", context=counter_context)

        # Counter Ability - Using correct action index 431
        if stack_has_opponent_ability:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and ("counter target ability" in card.oracle_text.lower() or 
                                                            "counter target activated ability" in card.oracle_text.lower()):
                    if (self._can_use_specialized_response_cast(
                            card_id, player, card)
                            and self._can_afford_card(player, card)):
                        # Include necessary context for handler
                        counter_ability_context = {'hand_idx': i}
                        # Find a valid target ability to include in context
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "ABILITY" and item[2] != player:
                                counter_ability_context['target_ability_idx'] = stack_idx
                                break
                        set_valid_action(431, f"COUNTER_ABILITY with {card.name}", context=counter_ability_context)

        # Prevent Damage - Using correct action index 432
        # Check if a damage spell/ability is on stack or if combat damage is pending
        damage_pending = gs.phase in [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE] or \
                        any(isinstance(item, tuple) and "damage" in getattr(gs._safe_get_card(item[1]), 'oracle_text', '').lower() for item in gs.stack)
        if damage_pending:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if (card and hasattr(card, 'oracle_text')
                        and has_damage_prevention_instruction(card.oracle_text)
                        and self._can_use_specialized_response_cast(
                            card_id, player, card)
                        and self._can_afford_card(player, card)):
                        prevent_context = {'hand_idx': i}
                        # Find damage source if applicable
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and "damage" in getattr(gs._safe_get_card(item[1]), 'oracle_text', '').lower():
                                prevent_context['damage_source_idx'] = stack_idx
                                break
                        set_valid_action(432, f"PREVENT_DAMAGE with {card.name}", context=prevent_context)

        # Redirect Damage - Using correct action index 433
        # Similar to prevent damage, check for damage sources
        if damage_pending:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "redirect" in card.oracle_text.lower() and "damage" in card.oracle_text.lower():
                    if (self._can_use_specialized_response_cast(
                            card_id, player, card)
                            and self._can_afford_card(player, card)):
                        redirect_context = {'hand_idx': i}
                        set_valid_action(433, f"REDIRECT_DAMAGE with {card.name}", context=redirect_context)

        # Stifle Trigger - Using correct action index 434
        # For now, enable if a triggered ability is on stack
        stack_has_trigger = any(isinstance(item, tuple) and item[0] == "TRIGGER" for item in gs.stack)
        if stack_has_trigger:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "counter target triggered ability" in card.oracle_text.lower():
                    if (self._can_use_specialized_response_cast(
                            card_id, player, card)
                            and self._can_afford_card(player, card)):
                        stifle_context = {'hand_idx': i}
                        # Find a valid target trigger to include in context
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "TRIGGER":
                                stifle_context['target_trigger_idx'] = stack_idx
                                break
                        set_valid_action(434, f"STIFLE_TRIGGER with {card.name}", context=stifle_context)

    def _add_cycling_actions(self, player, valid_actions, set_valid_action):
        """Add cycling actions."""
        gs = self.game_state
        for i in range(min(len(player["hand"]), 8)): # Hand index 0-7
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                cost_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if cost_match:
                    cost_str = cost_match.group(1)
                    # Normalize cost string if it's just a number
                    if cost_str.isdigit(): cost_str = f"{{{cost_str}}}"
                    if self._can_afford_cost_string(player, cost_str):
                        # FIXED: Use correct action index 427 for CYCLING
                        cycling_context = {'hand_idx': i}
                        set_valid_action(427, f"CYCLING {card.name}", context=cycling_context)

    def _add_room_door_actions(self, player, valid_actions, set_valid_action, is_sorcery_timing):
        """Add actions for Room doors, only at sorcery speed."""
        # Rooms can only be accessed at sorcery speed
        if not is_sorcery_timing:
            return
            
        gs = self.game_state
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5:  # Limit to first 5 Room cards
                break
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_room') or not card.is_room:
                continue
                
            # Check if door2 is locked (door1 is automatically unlocked when played)
            if hasattr(card, 'door2') and not card.door2.get('unlocked', False):
                door_cost = card.door2.get('mana_cost', '')
                
                # Check if we can afford the door cost
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, door_cost)
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford:
                    set_valid_action(248 + idx, f"UNLOCK_DOOR for {card.name}")

    def _add_class_level_actions(self, player, valid_actions, set_valid_action, is_sorcery_timing):
        """Add actions for Class level-ups, strictly at sorcery speed."""
        # Class level-ups can only be done at sorcery speed
        if not is_sorcery_timing:
            return
            
        gs = self.game_state
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5:  # Limit to 5 Class cards
                break
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_class') or not card.is_class:
                continue
                
            # Check if class can level up further
            if not hasattr(card, 'can_level_up') or not card.can_level_up():
                continue
                
            # Get cost for next level
            next_level = card.current_level + 1
            level_cost = card.get_level_cost(next_level)
            
            # Only allow leveling if current level supports it
            if next_level == 2 or (next_level == 3 and card.current_level == 2):
                # Check if we can afford the level cost
                can_afford = False
                if hasattr(gs, 'mana_system') and level_cost:
                    can_afford = gs.mana_system.can_pay_mana_cost(player, level_cost)
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                if can_afford:
                    set_valid_action(253 + idx, f"LEVEL_UP_CLASS to level {next_level} for {card.name}")

    def _add_counter_management_actions(self, player, valid_actions, set_valid_action):
        """Add actions for counter management."""
        gs = self.game_state
        
        # Only show counter actions if we're in a context that requires them
        if hasattr(gs, 'counter_context') and gs.counter_context:
            context = gs.counter_context
            counter_type = context.get('counter_type', '+1/+1')
            action_type = context.get('action_type', 'ADD_COUNTER')
            
            # ADD_COUNTER actions (indices 314-323)
            if action_type == "ADD_COUNTER":
                valid_targets = []
                # Target determination based on counter type
                if counter_type == '+1/+1':
                    # Creatures can get +1/+1 counters
                    for perm_id in player["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and 'creature' in getattr(perm_card, 'card_types', []):
                            valid_targets.append(perm_id)
                elif counter_type == 'loyalty':
                    # Planeswalkers get loyalty counters
                    for perm_id in player["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and 'planeswalker' in getattr(perm_card, 'card_types', []):
                            valid_targets.append(perm_id)
                # Generic case for other counter types
                else:
                    for perm_id in player["battlefield"]:
                        valid_targets.append(perm_id)
                
                # Generate ADD_COUNTER actions
                for i, perm_id in enumerate(valid_targets[:10]):  # Limit to 10 targets
                    perm_card = gs._safe_get_card(perm_id)
                    perm_name = getattr(perm_card, 'name', perm_id) if perm_card else str(perm_id)
                    counter_context = {'counter_type': counter_type, 'target_identifier': perm_id}
                    set_valid_action(314 + i, f"ADD {counter_type} COUNTER to {perm_name}", context=counter_context)
                    
            # REMOVE_COUNTER actions (indices 324-333)
            elif action_type == "REMOVE_COUNTER":
                valid_targets = []
                # Find permanents that have the specified counter type
                for perm_id in player["battlefield"]:
                    perm_card = gs._safe_get_card(perm_id)
                    if perm_card and hasattr(perm_card, 'counters') and perm_card.counters.get(counter_type, 0) > 0:
                        valid_targets.append(perm_id)
                
                # Generate REMOVE_COUNTER actions
                for i, perm_id in enumerate(valid_targets[:10]):  # Limit to 10 targets
                    perm_card = gs._safe_get_card(perm_id)
                    perm_name = getattr(perm_card, 'name', perm_id) if perm_card else str(perm_id)
                    counter_context = {'counter_type': counter_type, 'target_identifier': perm_id}
                    set_valid_action(324 + i, f"REMOVE {counter_type} COUNTER from {perm_name}", context=counter_context)
                    
            # PROLIFERATE action (index 334)
            elif action_type == "PROLIFERATE":
                # Check if there are any permanents with counters
                has_permanents_with_counters = False
                
                for perm_id in player["battlefield"]:
                    perm_card = gs._safe_get_card(perm_id)
                    if perm_card and hasattr(perm_card, 'counters') and any(count > 0 for count in perm_card.counters.values()):
                        has_permanents_with_counters = True
                        break
                
                if not has_permanents_with_counters:
                    # Check opponent's permanents
                    opponent = gs.p2 if player == gs.p1 else gs.p1
                    for perm_id in opponent["battlefield"]:
                        perm_card = gs._safe_get_card(perm_id)
                        if perm_card and hasattr(perm_card, 'counters') and any(count > 0 for count in perm_card.counters.values()):
                            has_permanents_with_counters = True
                            break
                
                if has_permanents_with_counters:
                    set_valid_action(334, "PROLIFERATE to add counters to all permanents with counters")

    def _check_valid_targets_exist(self, card, current_player, opponent):
        """
        Check if valid targets exist for a card requiring targets using the TargetingSystem.
        
        Args:
            card: The card being cast
            current_player: The player casting the spell
            opponent: The opponent player
            
        Returns:
            bool: Whether valid targets exist for the card
        """
        gs = self.game_state
        oracle_text = getattr(card, 'oracle_text', '')
        if gs._target_bounds_from_text(oracle_text)[0] == 0:
            return True
        
        # Use TargetingSystem if available
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
            targeting_system = gs.ability_handler.targeting_system
            targets = targeting_system.resolve_targeting_for_spell(card.card_id, current_player)
            return targets is not None and any(targets.values())
        
        # Fallback to simple target existence check if no targeting system
        card_text = card.oracle_text.lower() if hasattr(card, 'oracle_text') else ""
        
        # Check for target creatures
        if 'target creature' in card_text:
            # Check for controller restrictions
            your_creatures_only = 'target creature you control' in card_text
            opponent_creatures_only = 'target creature an opponent controls' in card_text or 'target creature you don\'t control' in card_text
            
            for p in [current_player, opponent]:
                # Skip if targeting restrictions don't allow this player's creatures
                if your_creatures_only and p != current_player:
                    continue
                if opponent_creatures_only and p == current_player:
                    continue
                    
                for c_id in p["battlefield"]:
                    c = gs._safe_get_card(c_id)
                    if c and hasattr(c, 'card_types') and 'creature' in c.card_types:
                        # Check for protection, hexproof, shroud
                        if p != current_player and self._check_for_protection(c, card):
                            continue
                        return True
        
        # Check for target players
        elif 'target player' in card_text or 'target opponent' in card_text:
            return True
            
        # Check for target permanent with more specific type checking
        elif 'target permanent' in card_text:
            your_permanents_only = 'target permanent you control' in card_text
            opponent_permanents_only = 'target permanent an opponent controls' in card_text
            
            for p in [current_player, opponent]:
                if your_permanents_only and p != current_player:
                    continue
                if opponent_permanents_only and p == current_player:
                    continue
                    
                if p["battlefield"]:
                    for perm_id in p["battlefield"]:
                        perm = gs._safe_get_card(perm_id)
                        if perm and not self._check_for_protection(perm, card):
                            return True
        
        # Check for specific permanent types
        elif any(f'target {ptype}' in card_text for ptype in ['artifact', 'enchantment', 'land', 'planeswalker']):
            for ptype in ['artifact', 'enchantment', 'land', 'planeswalker']:
                if f'target {ptype}' in card_text:
                    for p in [current_player, opponent]:
                        for perm_id in p["battlefield"]:
                            perm = gs._safe_get_card(perm_id)
                            if perm and hasattr(perm, 'card_types') and ptype in perm.card_types:
                                # Check for controller restrictions
                                if f'target {ptype} you control' in card_text and p != current_player:
                                    continue
                                if f'target {ptype} an opponent controls' in card_text and p == current_player:
                                    continue
                                if not self._check_for_protection(perm, card):
                                    return True
        
        # Default to true if targeting requirements cannot be determined
        return 'target' not in card_text

    def _check_for_protection(self, target_card, source_card):
        """Check if target has protection, hexproof, or shroud with enhanced handling."""
        gs = self.game_state
        
        # Use targeting_system if available
        if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'targeting_system'):
            targeting_system = gs.ability_handler.targeting_system
            
            # Find controllers for context
            target_controller = None
            source_controller = None
            for player in [gs.p1, gs.p2]:
                for zone in ["battlefield", "hand", "graveyard", "exile"]:
                    if zone in player and target_card.card_id in player[zone]:
                        target_controller = player
                    if source_card and hasattr(source_card, 'card_id') and zone in player and source_card.card_id in player[zone]:
                        source_controller = player
            
            # Default if controllers can't be determined
            if not target_controller:
                target_controller = gs.p1
            if not source_controller:
                source_controller = gs.p2
                
            # Use comprehensive protection check
            if targeting_system._has_protection_from(target_card, source_card, target_controller, source_controller):
                return True
                
            # Check hexproof against opposing source
            if target_controller != source_controller and targeting_system._has_hexproof(target_card):
                return True
                
            # Check shroud against any source
            if targeting_system._has_shroud(target_card):
                return True
                
            return False
        
        # Fallback to basic check if targeting system not available
        # Protection
        if hasattr(target_card, 'oracle_text') and "protection from" in target_card.oracle_text.lower():
            # Check color protection
            if hasattr(source_card, 'colors'):
                for i, color in enumerate(['white', 'blue', 'black', 'red', 'green']):
                    if f"protection from {color}" in target_card.oracle_text.lower() and source_card.colors[i]:
                        return True
            # Check for protection from all
            if "protection from everything" in target_card.oracle_text.lower():
                return True
        
        # Hexproof
        if hasattr(target_card, 'oracle_text') and "hexproof" in target_card.oracle_text.lower():
            # Check if source is controlled by target controller (hexproof only affects opponents)
            return True  # For simplicity assume opponent is casting
        
        # Shroud
        if hasattr(target_card, 'oracle_text') and "shroud" in target_card.oracle_text.lower():
            return True
            
        return False

    def _add_spree_mode_actions(self, player, valid_actions, set_valid_action):
        """Add actions for Spree mode selection during casting."""
        gs = self.game_state
        # Check if a Spree spell is being prepared (e.g., via a 'PREPARE_SPREE' phase/context)
        if hasattr(gs, 'spree_context') and gs.spree_context:
            context = gs.spree_context
            card_id = context.get('card_id')
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'spree_modes'):
                 selected_modes = context.get("selected_modes", set())
                 base_cost_paid = context.get("base_cost_paid", False)

                 # Base cost must be paid first (conceptually)
                 if not base_cost_paid:
                     # Maybe add an action "PAY_BASE_SPREE_COST"? Or handle implicitly.
                     pass

                 # Allow selecting additional modes if base cost is handled
                 if base_cost_paid:
                     for mode_idx, mode_data in enumerate(card.spree_modes):
                          # Action space mapping needs adjustment: (card_idx, mode_idx)
                          # Example mapping: card 0-7, mode 0-1 -> indices 258-273
                          # Need the hand_idx of the spree card. Assume it's stored in context.
                          hand_idx = context.get("hand_idx")
                          if hand_idx is not None and hand_idx < 8 and mode_idx < 2:
                               action_index = 258 + (hand_idx * 2) + mode_idx
                               mode_cost = mode_data.get('cost', '')
                               if self._can_afford_cost_string(player, mode_cost):
                                    # Prevent re-selecting the same mode
                                    if mode_idx not in selected_modes:
                                        set_valid_action(action_index, f"SELECT_SPREE_MODE {mode_idx} for {card.name}")
                     # Add action to finalize spree casting? Or use PLAY_SPELL?
                     set_valid_action(20 + hand_idx, f"CAST_SPREE {card.name} with selected modes")    

    def _add_instant_casting_actions(self, player, valid_actions, set_valid_action):
        """Add actions for casting instants and flash spells (only those valid at instant speed)."""
        gs = self.game_state
        
        for i in range(20, 28):
            hand_idx = i - 20
            if hand_idx < len(player["hand"]):
                card_id = player["hand"][hand_idx]
                card = gs._safe_get_card(card_id)
                
                if not card or not hasattr(card, 'card_types'):
                    continue
                    
                # Use mana_system if available, otherwise fall back to simpler check
                can_afford = False
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(player, card.mana_cost if hasattr(card, 'mana_cost') else "")
                else:
                    # Simple check - at least some mana available
                    can_afford = sum(player["mana_pool"].values()) > 0
                
                # Check if card can be cast at instant speed
                is_instant_speed = 'instant' in card.card_types or (hasattr(card, 'oracle_text') and 'flash' in card.oracle_text.lower())
                
                if is_instant_speed and can_afford:
                    # Check if the spell requires targets
                    requires_target = False
                    valid_targets_exist = True
                    
                    if hasattr(card, 'oracle_text'):
                        requires_target = 'target' in card.oracle_text.lower()
                        
                        # If it requires targets, check if any are available
                        if requires_target:
                            valid_targets_exist = self._check_valid_targets_exist(card, player, 
                                                        gs.p2 if player == gs.p1 else gs.p1)
                    
                    # Only allow casting if valid targets exist (if required)
                    if requires_target and not valid_targets_exist:
                        continue
                        
                    # Check for Spree instant 
                    if hasattr(card, 'is_spree') and card.is_spree:
                        set_valid_action(i, f"PLAY_SPREE_INSTANT for {card.name}")
                    else:
                        # Regular instant playing
                        set_valid_action(i, f"PLAY_INSTANT for {card.name}")
                
                    # Check for Adventure with instant type
                    if hasattr(card, 'has_adventure') and card.has_adventure():
                        adventure_data = card.get_adventure_data()
                        if adventure_data and 'instant' in adventure_data.get('type', '').lower():
                            adventure_cost = adventure_data.get('cost', '')
                            if hasattr(gs, 'mana_system'):
                                can_afford_adventure = gs.mana_system.can_pay_mana_cost(player, adventure_cost)
                            else:
                                can_afford_adventure = sum(player["mana_pool"].values()) > 0
                            
                            if can_afford_adventure:
                                set_valid_action(196 + hand_idx, f"PLAY_ADVENTURE for {adventure_data.get('name', 'Unknown')}")

    def _can_afford_card(self, player, card_or_data, is_back_face=False, context=None):
        """Check affordability using ManaSystem, handling dict or Card object."""
        gs = self.game_state
        if context is None: context = {}
        else: context = dict(context)
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) > 0 # Basic check

        if isinstance(card_or_data, dict): # E.g., back face data
            cost_str = card_or_data.get('mana_cost', '')
            card_id = card_or_data.get('id') # Need ID for context
        elif isinstance(card_or_data, Card):
            cost_str = getattr(card_or_data, 'mana_cost', '')
            card_id = getattr(card_or_data, 'card_id', None)
            # Conditional mana restrictions inspect the spell object. The
            # live cast installs it before payment; the mask probe must carry
            # the same context or it hides casts payable with restricted mana.
            context.setdefault('card', card_or_data)
        else:
            return False # Invalid input

        if (isinstance(card_or_data, Card)
                and getattr(card_or_data, 'is_spree', False)):
            return any(
                gs.spree_mode_is_selectable(
                    card_id, player, [], mode_index, context=context)
                for mode_index in range(len(
                    getattr(card_or_data, 'spree_modes', []) or [])))

        if not cost_str and not context.get('use_alt_cost'): return True # Free spell (unless alt cost used)

        oracle_text = getattr(card_or_data, 'oracle_text', '') \
            if isinstance(card_or_data, Card) else card_or_data.get('oracle_text', '')
        requires_return = bool(re.search(
            r"as an additional cost to cast this spell,\s*return a permanent "
            r"you control to its owner'?s hand", oracle_text.lower()))
        if requires_return and not player.get("battlefield"):
            return False

        try:
            parsed_cost = gs.mana_system.parse_mana_cost(cost_str)
            # Apply cost modifiers based on context (Kicker, Additional, Alternative)
            final_cost = gs.mana_system.apply_cost_modifiers(player, parsed_cost, card_id, context)
            if requires_return:
                # CR 601.2g activates mana abilities before CR 601.2h pays
                # the return cost. The selected permanent may therefore tap
                # for mana and then leave the battlefield.
                return bool(player.get("battlefield")) and \
                    gs.mana_system.can_pay_mana_cost_with_lands(
                        player, final_cost, context)
            if gs.mana_system.can_pay_mana_cost_with_lands(player, final_cost, context):
                return True
            return gs.mana_system.can_pay_with_target_dependent_reduction(
                player, parsed_cost, card_id, context)
        except Exception as e:
            card_name = getattr(card_or_data, 'name', 'Unknown') if isinstance(card_or_data, Card) else card_or_data.get('name', 'Unknown')
            logging.warning(f"Error checking mana cost for '{card_name}': {e}")
            return False

    def _can_afford_cost_string(self, player, cost_string, context=None):
        """Check affordability directly from a cost string using ManaSystem."""
        gs = self.game_state
        if context is None: context = {}
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) > 0 # Basic check
        if not cost_string: return True

        try:
            parsed_cost = gs.mana_system.parse_mana_cost(cost_string)
            # No cost modifiers applied here, assumes string is the final cost
            return gs.mana_system.can_pay_mana_cost_with_lands(player, parsed_cost, context)
        except Exception as e:
            logging.warning(f"Error checking mana cost string '{cost_string}': {e}")
            return False

    def _spree_alt_cast_payable(self, player, card_id, card, context):
        """Whether an alternative-cost cast of a Spree spell can be announced.

        Announcing a Spree spell adds at least one mode cost on top of the
        base or alternative cost (CR 702.172a), and targets are checked per
        chosen mode.  Mask sites that only verified the flashback/harmonize
        cost offered casts that cast_spell then refused, which strict
        evaluation fidelity treats as fatal (Three Steps Ahead, 2026-07-13).
        """
        gs = self.game_state
        return any(
            gs.spree_mode_is_selectable(
                card_id, player, [], mode_index, context=context)
            for mode_index in range(len(
                getattr(card, 'spree_modes', []) or [])))

    def _has_flash(self, card_id):
        """Check if card has flash keyword."""
        card = self.game_state._safe_get_card(card_id)
        return self._has_flash_text(getattr(card, 'oracle_text', ''))

    def _has_flash_text(self, oracle_text):
        """Check if oracle text contains flash keyword."""
        return oracle_text and 'flash' in oracle_text.lower()

    def _spell_cast_supported(self, card):
        """Gate spells whose casting flow the engine cannot complete yet."""
        if getattr(card, 'is_spree', False):
            if getattr(card, 'spree_modes', None):
                return True
            # A malformed Spree declaration remains an explicit support gap.
            if not getattr(card, '_spree_support_gap_reported', False):
                try:
                    from .card_support import report_unsupported
                    report_unsupported(
                        getattr(card, 'name', 'unknown'),
                        "spree modes could not be parsed",
                        severity="unparsed")
                    card._spree_support_gap_reported = True
                except Exception:
                    pass
            return False
        return True

    def _targets_available(self, card, caster, opponent):
        """Check target availability using TargetingSystem."""
        gs = self.game_state
        # Spree target requirements belong only to modes actually announced.
        # _can_afford_card probes every single mode through the same cumulative
        # cost/target predicate used by the live chooser.
        if getattr(card, 'is_spree', False):
            return True
        card_id = getattr(card, 'card_id', None)
        if card_id is None or not hasattr(card, 'oracle_text'):
            return True # No target needed or cannot check
        gift_branches = gs._gift_targeting_texts(card.oracle_text)
        if gift_branches:
            return any(self._targets_available_from_text(
                branch, caster, opponent, source_id=card_id)
                for branch in gift_branches)
        aura_target_text = aura_cast_targeting_text(card)
        if not aura_target_text and 'target' not in card.oracle_text.lower():
            return True # No target needed.
        # Permanent spells other than Auras do not choose targets on cast
        # (CR 601.2c); 'target' in their text belongs to triggered/activated
        # abilities or reminder text and must not block casting them.
        card_types = [str(t).lower() for t in getattr(card, 'card_types', [])]
        if 'instant' not in card_types and 'sorcery' not in card_types:
            subtypes = [str(s).lower() for s in getattr(card, 'subtypes', [])]
            if 'aura' not in subtypes:
                return True
        if hasattr(gs, 'targeting_system') and gs.targeting_system:
            try:
                if aura_target_text:
                    target_type = gs._get_target_type_from_text(
                        aura_target_text)
                    valid_targets = gs.targeting_system.get_valid_targets(
                        card_id, caster, target_type,
                        effect_text=aura_target_text)
                    return any(valid_targets.values())
                target_slots = gs._ordinary_target_slots(card.oracle_text)
                if target_slots:
                    for slot in target_slots:
                        minimum = int(slot.get('min_targets', 0))
                        if minimum == 0:
                            continue
                        valid_targets = gs.targeting_system.get_valid_targets(
                            card_id, caster,
                            slot.get('required_type', 'target'),
                            effect_text=slot.get('effect_text', ''))
                        valid_ids = {
                            target_id
                            for target_ids in valid_targets.values()
                            for target_id in target_ids
                        }
                        if len(valid_ids) < minimum:
                            return False
                    return True
                targeting_text = gs._ordinary_single_targeting_text(
                    card.oracle_text)
                minimum, _ = gs._target_bounds_from_text(targeting_text)
                if minimum == 0:
                    return True
                target_type = gs._get_target_type_from_text(targeting_text)
                valid_targets = gs.targeting_system.get_valid_targets(
                    card_id, caster, target_type,
                    effect_text=targeting_text)
                valid_ids = {
                    target_id
                    for target_ids in valid_targets.values()
                    for target_id in target_ids
                }
                return len(valid_ids) >= minimum
            except Exception as e:
                 logging.warning(f"Error checking targets with TargetingSystem for {card.name}: {e}")
                 return False
        else:
            return True # Assume targets exist if no system

    def _add_ability_activation_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for activating abilities."""
        gs = self.game_state
        if not hasattr(gs, 'ability_handler'): return

        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if not card: continue

            abilities = gs.ability_handler.get_activated_abilities(card_id)
            for j, ability in enumerate(abilities):
                if j >= 3: break # Limit abilities per card

                # Check timing restriction
                requires_sorcery = "activate only as a sorcery" in getattr(ability, 'effect_text', '').lower()
                if requires_sorcery and not is_sorcery_speed: continue
                if not requires_sorcery and is_sorcery_speed: continue # If checking only sorcery speed

                if gs.ability_handler.can_activate_ability(card_id, j, player):
                   # Repeatable abilities remain legal as long as their real
                   # costs and timing permit them.  A synthetic three-use cap
                   # changed game rules and biased harvested card values.
                   set_valid_action(
                       100 + (i * 3) + j,
                       f"ACTIVATE {card.name} ability {j}",
                       context={
                           "battlefield_idx": i,
                           "ability_idx": j,
                           "controller_id": "p1" if player is gs.p1 else "p2",
                       })

    def _add_overflow_ability_catalog_action(self, player, mana_only=False):
        """Return legal actions that have no dedicated fixed action slot."""
        gs = self.game_state
        if not getattr(gs, "ability_handler", None):
            return []
        if mana_only:
            from .ability_types import ManaAbility
        can_sorcery = gs._can_act_at_sorcery_speed(player)
        options = []
        if not mana_only:
            opponent = gs.p2 if player is gs.p1 else gs.p1
            for hand_idx, card_id in enumerate(player.get("hand", [])[10:], 10):
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                controller_id = "p1" if player is gs.p1 else "p2"
                action_context = {
                    "hand_idx": hand_idx, "card_id": card_id,
                    "controller_id": controller_id,
                }
                if (can_sorcery and gs.can_play_land_this_turn(player)
                        and "land" in getattr(card, "card_types", [])):
                    options.append({
                        "label": f"Play {getattr(card, 'name', card_id)}",
                        "handler": "play_land", "action_context": action_context,
                    })
                    continue
                is_instant = (
                    "instant" in getattr(card, "card_types", [])
                    or self._has_flash(card_id))
                if (not is_instant and not can_sorcery):
                    continue
                if ("land" not in getattr(card, "card_types", [])
                        and self._spell_cast_supported(card)
                        and self._can_afford_card(player, card, context={})
                        and self._targets_available(card, player, opponent)):
                    options.append({
                        "label": f"Cast {getattr(card, 'name', card_id)}",
                        "handler": "play_spell", "action_context": action_context,
                    })
            options.extend(self._overflow_graveyard_actions(player, opponent))
            for battlefield_idx, card_id in enumerate(
                    player.get("battlefield", [])[5:], 5):
                card = gs._safe_get_card(card_id)
                if not card:
                    continue
                action_context = {
                    "battlefield_idx": battlefield_idx,
                    "card_id": card_id,
                    "controller_id": "p1" if player is gs.p1 else "p2",
                }
                if (getattr(card, "is_class", False)
                        and card.can_level_up()):
                    next_level = card.current_level + 1
                    if self._can_afford_cost_string(
                            player, card.get_level_cost(next_level)):
                        options.append({
                            "label": f"Level {card.name} to {next_level}",
                            "handler": "level_up_class",
                            "action_context": action_context,
                        })
                if (getattr(card, "is_leveler", False)
                        and getattr(card, "level_up_cost", None)
                        and self._can_afford_cost_string(
                            player, card.level_up_cost)):
                    options.append({
                        "label": f"Level up {card.name}",
                        "handler": "level_up_creature",
                        "action_context": action_context,
                    })
        for battlefield_idx, card_id in enumerate(player.get("battlefield", [])):
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            abilities = gs.ability_handler.get_activated_abilities(card_id)
            for ability_idx, ability in enumerate(abilities):
                if battlefield_idx < 20 and ability_idx < 3:
                    continue
                if mana_only and not isinstance(ability, ManaAbility):
                    continue
                effect_text = getattr(ability, "effect_text", "") or ""
                requires_sorcery = (
                    "activate only as a sorcery" in effect_text.lower())
                if requires_sorcery and not can_sorcery:
                    continue
                if not gs.ability_handler.can_activate_ability(
                        card_id, ability_idx, player):
                    continue
                options.append({
                    "label": (f"{getattr(card, 'name', card_id)} "
                              f"ability {ability_idx}"),
                    "handler": "activate_ability",
                    "action_context": {
                        "battlefield_idx": battlefield_idx,
                        "ability_idx": ability_idx,
                        "controller_id": "p1" if player is gs.p1 else "p2",
                    },
                })
        return options

    def _overflow_graveyard_actions(self, player, opponent):
        """Return cast/play permissions beyond the six fixed graveyard slots."""
        gs = self.game_state
        can_sorcery = gs._can_act_at_sorcery_speed(player)
        has_emblem = any(
            emblem.get("kind") == "graveyard_permanents"
            for emblem in player.get("emblems", []))
        has_land_permission = gs.can_play_lands_from_graveyard(player)
        casting_allowed = gs.can_player_cast_spells(player)
        permanent_types = {
            "creature", "artifact", "enchantment", "planeswalker", "battle"}
        options = []
        for graveyard_index, card_id in enumerate(
                player.get("graveyard", [])[6:], 6):
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            if ("land" not in getattr(card, "card_types", [])
                    and not casting_allowed):
                continue
            context = {"source_zone": "graveyard",
                       "source_idx": graveyard_index}
            if gs.has_graveyard_adventure_permission(player, card_id):
                adventure = card.get_adventure_data() or {}
                if ("instant" not in adventure.get("type", "").lower()
                        and not can_sorcery):
                    continue
                if (self._can_afford_cost_string(
                        player, adventure.get("cost", ""), context={})
                        and self._targets_available_from_text(
                            adventure.get("effect", ""), player, opponent,
                            source_id=card_id)):
                    context.update({"graveyard_adventure_cast": True,
                                    "cast_as_adventure": True})
                else:
                    continue
            else:
                harmonize_cost = gs.harmonize_cost_for(player, card_id)
                flashback_cost = gs.flashback_cost_for(player, card_id)
                if harmonize_cost:
                    if not can_sorcery:
                        continue
                    candidates = [
                        cid for cid in player.get("battlefield", [])
                        if cid not in player.get("tapped_permanents", set())
                        and "creature" in getattr(
                            gs._safe_get_card(cid), "card_types", [])]
                    reductions = [0] + [
                        max(0, int(getattr(
                            gs._safe_get_card(cid), "power", 0) or 0))
                        for cid in candidates]
                    if getattr(card, "is_spree", False):
                        if not any(
                                self._spree_alt_cast_payable(
                                    player, card_id, card, {
                                        "card": card,
                                        "use_alt_cost": "harmonize",
                                        "harmonize_cost": harmonize_cost,
                                        "harmonize_reduction": reduction})
                                for reduction in reductions):
                            continue
                    elif not any(
                            gs.mana_system.can_pay_mana_cost_with_lands(
                                player,
                                gs.mana_system.calculate_alternative_cost(
                                    card_id, player, "harmonize", {
                                        "harmonize_cost": harmonize_cost,
                                        "harmonize_reduction": reduction}),
                                {"card": card})
                            for reduction in reductions):
                        continue
                    context.update({"harmonize_cast": True,
                                    "harmonize_cost": harmonize_cost})
                elif flashback_cost:
                    if ("instant" not in getattr(card, "card_types", [])
                            and not can_sorcery):
                        continue
                    flashback_context = {"card": card,
                                         "flashback_cost": flashback_cost,
                                         "use_alt_cost": "flashback"}
                    if getattr(card, "is_spree", False):
                        if not self._spree_alt_cast_payable(
                                player, card_id, card, flashback_context):
                            continue
                    elif (not self._can_afford_cost_string(
                            player, flashback_cost,
                            context=flashback_context)
                            or not self._targets_available(
                                card, player, opponent)):
                        continue
                    context.update({"flashback_cast": True,
                                    "flashback_cost": flashback_cost})
                elif has_emblem or has_land_permission:
                    card_types = set(getattr(card, "card_types", []))
                    is_land = "land" in card_types
                    if is_land:
                        if (not can_sorcery
                                or not gs.can_play_land_this_turn(player)):
                            continue
                    elif (not has_emblem
                          or not card_types.intersection(permanent_types)
                          or (not can_sorcery and not self._has_flash(card_id))):
                        continue
                    if is_land and has_land_permission:
                        context["controlled_permanent_land_play"] = True
                    else:
                        context["emblem_graveyard_cast"] = True
                    if (not is_land
                            and (not self._can_afford_card(
                                player, card, context=context)
                                 or not self._targets_available(
                                     card, player, opponent))):
                        continue
                else:
                    continue
            options.append({
                "label": f"Play from graveyard: {getattr(card, 'name', card_id)}",
                "handler": "play_from_graveyard",
                "action_context": context,
            })
        return options

    def _add_land_tapping_actions(self, player, valid_actions, set_valid_action):
        """Add actions for tapping lands for mana or effects."""
        gs = self.game_state
        for i in range(min(len(player["battlefield"]), 20)): # Tap land indices 0-19
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and 'land' in getattr(card, 'type_line', '') and card_id not in player.get("tapped_permanents", set()):
                # Check for mana abilities
                mana_options = (gs.mana_system._land_mana_options(player, card)
                                if gs.mana_system else [])
                if mana_options:
                     set_valid_action(68 + i, f"TAP_LAND_FOR_MANA {card.name}")
                # Check for other tap abilities
                if hasattr(card, 'oracle_text') and "{t}:" in card.oracle_text.lower() and "add {" not in card.oracle_text.lower():
                     if i < 12: # Tap land for effect indices 0-11
                          set_valid_action(88 + i, f"TAP_LAND_FOR_EFFECT {card.name}")

    def _add_exile_casting_actions(self, player, valid_actions, set_valid_action):
        """Add actions for casting spells from exile."""
        gs = self.game_state

        for i, option in enumerate(gs.get_exile_cast_options(player)[:8]):
            card = gs._safe_get_card(option["card_id"])
            if not card:
                continue
            is_land = bool(
                "land" in getattr(card, "card_types", [])
                or "land" in str(getattr(card, "type_line", "")).lower())
            if is_land:
                can_afford = bool(
                    option.get("permission") == "ordinary"
                    and gs.can_play_land_this_turn(player)
                    and gs._can_act_at_sorcery_speed(player)
                    and (gs.priority_player is None
                         or gs.priority_player is player))
            elif not gs.can_player_cast_spells(player):
                continue
            elif option.get("permission") == "plot":
                can_afford = True
            elif option.get("alternative_cost") and hasattr(gs, 'mana_system'):
                can_afford = gs.mana_system.can_pay_mana_cost(
                    player, option["alternative_cost"])
            elif hasattr(gs, 'mana_system'):
                can_afford = gs.mana_system.can_pay_mana_cost(
                    player, getattr(card, "mana_cost", ""))
            else:
                can_afford = sum(player["mana_pool"].values()) > 0
            if can_afford:
                permission = option.get("permission", "exile")
                set_valid_action(
                    230 + i,
                    f"{'PLAY' if is_land else 'CAST'}_FROM_EXILE "
                    f"{card.name} ({permission})",
                    context={"exile_option_index": i})

    def _add_plot_actions(self, player, valid_actions, set_valid_action):
        """Expose Plot as one hand-indexed sorcery-speed special action."""
        gs = self.game_state
        action_indices = [296, 297, 298, 309, 310, 311, 312, 313]
        for hand_index, card_id in enumerate(player.get("hand", [])[:8]):
            card = gs._safe_get_card(card_id)
            cost = getattr(card, "plot_cost", None) if card else None
            if (card and getattr(card, "is_plot", False) and cost
                    and self._can_afford_cost_string(player, cost)):
                set_valid_action(
                    action_indices[hand_index], f"PLOT {card.name}",
                    context={"hand_idx": hand_index})

    def _add_warp_actions(self, player, valid_actions, set_valid_action,
                          require_instant_speed=False):
        """Expose Warp using Plot's mutually exclusive hand-indexed slots."""
        if not self.game_state.can_player_cast_spells(player):
            return
        action_indices = [296, 297, 298, 309, 310, 311, 312, 313]
        for hand_index, card_id in enumerate(player.get("hand", [])[:8]):
            card = self.game_state._safe_get_card(card_id)
            warp_cost = getattr(card, "warp_cost", None) if card else None
            if (not card or not getattr(card, "is_warp", False)
                    or not warp_cost):
                continue
            if require_instant_speed and not self._has_flash(card_id):
                continue
            if self._can_afford_cost_string(player, warp_cost):
                set_valid_action(
                    action_indices[hand_index], f"WARP_CAST {card.name}",
                    context={"hand_idx": hand_index, "warp_cast": True})

    def _add_prepared_cast_action(self, player, valid_actions,
                                  set_valid_action):
        """Expose virtual spell copies made by prepared permanents."""
        gs = self.game_state
        if not gs.can_player_cast_spells(player):
            return
        options = [
            card_id for card_id in player.get("battlefield", [])
            if card_id in getattr(gs, "prepared_cards", set())
            and gs.can_cast_prepared_copy(card_id, player)
        ]
        if not options:
            return
        labels = [
            (gs._prepare_spell_face(gs._safe_get_card(card_id)) or {})
            .get("name", card_id)
            for card_id in options
        ]
        context = {
            "options": options,
            "controller_id": "p1" if player is gs.p1 else "p2",
        }
        if len(options) == 1:
            context["source_id"] = options[0]
        set_valid_action(
            451, "PREPARED_CAST " + ", ".join(map(str, labels)),
            context=context)

    def _add_token_copy_actions(self, player, valid_actions, set_valid_action):
        """Add actions for token creation and copying."""
        gs = self.game_state
        
        # Check for cards or effects that can create tokens
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text'):
                oracle_text = card.oracle_text.lower()
                
                # CREATE_TOKEN actions (indices 410-414)
                if "create a token" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability that creates tokens
                    create_pattern = re.search(r"\{[^\}]+\}:.*?create a", oracle_text)
                    if create_pattern:
                        # Determine token type (up to 5 predefined types)
                        token_types = ["creature", "treasure", "clue", "food", "blood"]
                        for idx, token_type in enumerate(token_types):
                            if token_type in oracle_text:
                                token_context = {'battlefield_idx': i, 'token_type': idx}
                                set_valid_action(410 + idx, f"CREATE_{token_type.upper()}_TOKEN with {card.name}", context=token_context)
                                break
                
                # COPY_PERMANENT action (index 415)
                if "copy target" in oracle_text and "permanent" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    copy_pattern = re.search(r"\{[^\}]+\}:.*?copy target", oracle_text)
                    if copy_pattern:
                        # Find valid targets to copy
                        for target_idx, target_id in enumerate(player["battlefield"]):
                            if target_id != card_id:  # Can't copy itself
                                target_card = gs._safe_get_card(target_id)
                                if target_card:
                                    copy_context = {'battlefield_idx': i, 'target_identifier': target_id}
                                    set_valid_action(415, f"COPY_PERMANENT {target_card.name}", context=copy_context)
                                    break  # Just one action is enough, context will specify target
                
                # COPY_SPELL action (index 416)
                if "copy target" in oracle_text and "spell" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    copy_pattern = re.search(r"\{[^\}]+\}:.*?copy target", oracle_text)
                    if copy_pattern and gs.stack:
                        # Find valid spells on stack
                        for stack_idx, item in enumerate(gs.stack):
                            if isinstance(item, tuple) and item[0] == "SPELL" and item[2] != player:
                                spell_id = item[1]
                                spell = gs._safe_get_card(spell_id)
                                if spell:
                                    copy_context = {'battlefield_idx': i, 'target_stack_identifier': stack_idx}
                                    set_valid_action(416, f"COPY_SPELL {spell.name}", context=copy_context)
                                    break
                
                # POPULATE action (index 417)
                if "populate" in oracle_text and not card_id in player.get("tapped_permanents", set()):
                    # Check for activated ability
                    populate_pattern = re.search(r"\{[^\}]+\}:.*?populate", oracle_text)
                    if populate_pattern:
                        # Find valid token creatures to copy
                        has_token = False
                        for token_idx, token_id in enumerate(player["battlefield"]):
                            token_card = gs._safe_get_card(token_id)
                            if token_card and getattr(token_card, 'is_token', False) and 'creature' in getattr(token_card, 'card_types', []):
                                populate_context = {'battlefield_idx': i, 'target_token_identifier': token_id}
                                set_valid_action(417, f"POPULATE to copy {token_card.name}", context=populate_context)
                                has_token = True
                                break
                        
                        if not has_token:
                            # Can't populate without token creatures
                            continue

    def _add_specific_mechanics_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for specialized MTG mechanics, considering timing."""
        gs = self.game_state

        if is_sorcery_speed:
            for battlefield_index, card_id in enumerate(player.get("battlefield", [])[:20]):
                card = gs._safe_get_card(card_id)
                match = re.search(r"\bsaddle\s+(\d+)", getattr(card, "oracle_text", ""), re.IGNORECASE) if card else None
                if not match:
                    continue
                candidates = [other_id for other_id in player.get("battlefield", [])[:10]
                              if other_id != card_id and other_id not in player.get("tapped_permanents", set())
                              and gs._is_creature(other_id)]
                def saddle_power(cid):
                    try:
                        return max(0, int(getattr(gs._safe_get_card(cid), "power", 0) or 0))
                    except (TypeError, ValueError):
                        return 0
                if sum(saddle_power(cid) for cid in candidates) >= int(match.group(1)):
                    set_valid_action(478, f"SADDLE {card.name}", context={"battlefield_idx": battlefield_index})
                    break

        # --- Specialize (reuses per-permanent Transform actions 160-179) ---
        if is_sorcery_speed:
            for battlefield_index, card_id in enumerate(player.get("battlefield", [])[:20]):
                card = gs._safe_get_card(card_id)
                if (not card or not getattr(card, "is_specialize", False)
                        or card_id in getattr(gs, "specialized_cards", {})):
                    continue
                variants = gs.get_specialize_variants(card_id)
                cost = getattr(card, "specialize_cost", None)
                if (not variants or not cost
                        or not self._can_afford_cost_string(player, cost)):
                    continue
                if any(
                        gs.get_specialize_discard_colors(hand_id).intersection(variants)
                        for hand_id in player.get("hand", [])[:10]):
                    set_valid_action(
                        160 + battlefield_index,
                        f"SPECIALIZE {card.name}")
        
        # --- Investigate (Action 418) ---
        # Check for cards with "Investigate" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            activation_context = self._mechanic_activation_context(
                player, i, "investigate")
            if card and activation_context:
                set_valid_action(418, f"INVESTIGATE with {card.name}",
                                 context=activation_context)

        # --- Foretell (Action 419 - Sorcery speed only) ---
        if is_sorcery_speed:
            for i in range(min(len(player.get("hand",[])), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "foretell" in card.oracle_text.lower():
                    # Foretell cost is always {2} mana
                    if self._can_afford_cost_string(player, "{2}"):
                        context = {'hand_idx': i}
                        set_valid_action(419, f"FORETELL {card.name}", context=context)

        # --- Amass (Action 420) ---
        # Check for cards with "Amass" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            activation_context = self._mechanic_activation_context(
                player, i, "amass")
            if card and activation_context:
                set_valid_action(420, f"AMASS with {card.name}",
                                 context=activation_context)

        # --- Learn (Action 421) ---
        # Adding this if there's a "Learn" trigger waiting for resolution
        if hasattr(gs, 'learn_pending') and gs.learn_pending and gs.learn_pending.get('player') == player:
            set_valid_action(421, "LEARN (Draw and discard or get Lesson)")

        # --- Venture (Action 422) ---
        # Check for cards with "Venture into the dungeon" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            activation_context = self._mechanic_activation_context(
                player, i, "venture")
            if card and activation_context:
                set_valid_action(422, f"VENTURE with {card.name}",
                                 context=activation_context)

        # --- Exert (Action 423) ---
        # Only available during combat for attackers
        if gs.phase == gs.PHASE_DECLARE_ATTACKERS:
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "exert" in card.oracle_text.lower():
                    # Only for creatures that can attack and aren't already being exerted
                    if 'creature' in getattr(card, 'card_types', []) and card_id not in player.get("tapped_permanents", set()):
                        if not hasattr(gs, 'exerted_this_combat') or card_id not in gs.exerted_this_combat:
                            exert_context = {'creature_idx': i}
                            set_valid_action(423, f"EXERT {card.name}", context=exert_context)

        # --- Explore (Action 424) ---
        # Check for cards with "Explore" as an activated ability
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            activation_context = self._mechanic_activation_context(
                player, i, "explore")
            if card and activation_context:
                set_valid_action(424, f"EXPLORE with {card.name}",
                                 context=activation_context)

        # --- Adapt (Action 425 - Sorcery speed) ---
        if is_sorcery_speed:
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                card = gs._safe_get_card(card_id)
                activation_context = self._mechanic_activation_context(
                    player, i, "adapt")
                if card and activation_context:
                    set_valid_action(425, f"ADAPT with {card.name}",
                                     context=activation_context)

        # --- Mutate (Action 426 - Sorcery speed) ---
        if is_sorcery_speed:
            # Check for mutate cards in hand
            for hand_idx, card_id in enumerate(player["hand"][:8]):
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "mutate " in card.oracle_text.lower():
                    # Check for valid targets on the battlefield (non-Human creatures)
                    has_valid_target = False
                    for target_idx, target_id in enumerate(player["battlefield"]):
                        target_card = gs._safe_get_card(target_id)
                        if (target_card and 'creature' in getattr(target_card, 'card_types', []) and 
                                'human' not in getattr(target_card, 'subtypes', [])):
                            has_valid_target = True
                            break
                    
                    if has_valid_target:
                        # Extract mutate cost
                        match = re.search(
                            r"\bmutate\s*((?:\{[^}]+\})+)",
                            card.oracle_text, re.IGNORECASE)
                        mutate_cost = match.group(1) if match else None
                        
                        if mutate_cost and self._can_afford_cost_string(player, mutate_cost):
                            mutate_context = {'hand_idx': hand_idx}
                            set_valid_action(426, f"MUTATE {card.name}", context=mutate_context)

        # --- Cycling (Action 427 - Instant speed) ---
        if not is_sorcery_speed:  # Only at instant speed
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "cycling" in card.oracle_text.lower():
                    cycling_match = re.search(r"cycling (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                    if cycling_match:
                        cost_str = cycling_match.group(1)
                        if cost_str.isdigit(): 
                            cost_str = f"{{{cost_str}}}"
                        
                        if self._can_afford_cost_string(player, cost_str):
                            cycling_context = {'hand_idx': i}
                            set_valid_action(427, f"CYCLING {card.name}", context=cycling_context)

        # --- Goad (Action 428) ---
        # Check for cards that can goad
        for i in range(min(len(player["battlefield"]), 20)):
            card_id = player["battlefield"][i]
            card = gs._safe_get_card(card_id)
            activation_context = self._mechanic_activation_context(
                player, i, "goad")
            if card and activation_context:
                set_valid_action(428, f"GOAD with {card.name}",
                                 context=activation_context)

        # --- Boast (Action 429 - Only after attacking) ---
        if gs.phase >= gs.PHASE_DECLARE_ATTACKERS:  # After declare attackers phase
            for i in range(min(len(player["battlefield"]), 20)):
                card_id = player["battlefield"][i]
                if card_id in getattr(gs, 'attackers_this_turn', set()):  # Check if it attacked
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'oracle_text') and "boast —" in card.oracle_text.lower():
                        # Check if already boasted this turn
                        if not hasattr(gs, 'boast_activated') or card_id not in gs.boast_activated:
                            boast_context = {'creature_idx': i}
                            set_valid_action(429, f"BOAST with {card.name}", context=boast_context)

    def _add_zone_movement_actions(self, player, valid_actions, set_valid_action):
        """Add actions for zone movement."""
        gs = self.game_state
        
        # RETURN_FROM_GRAVEYARD actions (indices 335-340)
        for i, card_id in enumerate(player.get("graveyard", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card: continue
            
            # Check if a card in hand or on battlefield can return this card
            can_return = False
            return_source = None
            
            # Check hand for cards that can return from graveyard
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and "return target" in hand_card.oracle_text.lower() and "from your graveyard" in hand_card.oracle_text.lower():
                    # Determine if this card is a valid target based on type
                    card_type_pattern = re.search(r"return target ([a-z]+) card from your graveyard", hand_card.oracle_text.lower())
                    if card_type_pattern:
                        required_type = card_type_pattern.group(1)
                        if required_type in getattr(card, 'card_types', []) or required_type in getattr(card, 'subtypes', []):
                            can_return = True
                            return_source = hand_card.name
                            break
                    else:
                        can_return = True  # No type restriction found
                        return_source = hand_card.name
                        break
            
            if can_return:
                context = {'gy_idx': i, 'source': return_source}
                set_valid_action(335 + i, f"RETURN_FROM_GRAVEYARD {card.name}", context=context)
        
        # REANIMATE actions (indices 341-346)
        for i, card_id in enumerate(player.get("graveyard", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card or 'creature' not in getattr(card, 'card_types', []): continue
            
            # Check if a card in hand or on battlefield can reanimate this creature
            can_reanimate = False
            reanimate_source = None
            
            # Check hand for cards that can reanimate
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and ("return target creature" in hand_card.oracle_text.lower() and "to the battlefield" in hand_card.oracle_text.lower()):
                    can_reanimate = True
                    reanimate_source = hand_card.name
                    break
            
            if can_reanimate:
                context = {'gy_idx': i, 'source': reanimate_source}
                set_valid_action(341 + i, f"REANIMATE {card.name}", context=context)
        
        # RETURN_FROM_EXILE actions (indices 347-352)
        for i, card_id in enumerate(player.get("exile", [])[:6]):  # Limit to first 6
            card = gs._safe_get_card(card_id)
            if not card: continue
            
            # Check if a card in hand or on battlefield can return this card from exile
            can_return_from_exile = False
            return_exile_source = None
            
            # Check hand for cards that can return from exile
            for hand_card_id in player.get("hand", []):
                hand_card = gs._safe_get_card(hand_card_id)
                if hand_card and hasattr(hand_card, 'oracle_text') and "return target" in hand_card.oracle_text.lower() and "from exile" in hand_card.oracle_text.lower():
                    can_return_from_exile = True
                    return_exile_source = hand_card.name
                    break
            
            if can_return_from_exile:
                context = {'exile_idx': i, 'source': return_exile_source}
                set_valid_action(347 + i, f"RETURN_FROM_EXILE {card.name}", context=context)

    def _add_alternative_casting_actions(self, player, valid_actions, set_valid_action, is_sorcery_speed):
        """Add actions for alternative casting costs."""
        gs = self.game_state
        if not gs.can_player_cast_spells(player):
            return
        opponent = gs.p2 if player is gs.p1 else gs.p1
        # Evoke changes only the cost, so it follows the card's normal timing.
        # Action 221 carries the selected hand slot in generated context; if
        # multiple Evoke cards are legal, set_valid_action's shared catalog
        # preserves the additional distinct contexts.
        for i in range(min(len(player.get("hand", [])), 8)):
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if (not card or not hasattr(card, "oracle_text")
                    or not re.search(
                        r"(?:^|\n)evoke\s+(?:\{[^}]+\})+",
                        card.oracle_text, re.IGNORECASE)):
                continue
            if not is_sorcery_speed and not self._has_flash(card_id):
                continue
            cast_context = {
                "hand_idx": i,
                "source_zone": "hand",
                "use_alt_cost": "evoke",
                "card": card,
            }
            evoke_cost = gs.mana_system.calculate_alternative_cost(
                card_id, player, "evoke", cast_context)
            if evoke_cost is None:
                continue
            final_cost = gs.mana_system.apply_cost_modifiers(
                player, evoke_cost, card_id, cast_context)
            if gs.mana_system.can_pay_mana_cost_with_lands(
                    player, final_cost, cast_context):
                set_valid_action(
                    221, f"EVOKE_CAST {card.name}",
                    context={"hand_idx": i})

        # Flashback
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "flashback" in card.oracle_text.lower():
                is_instant = 'instant' in getattr(card, 'card_types', [])
                if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"flashback ((?:\{[^\}]+\})+)", card.oracle_text.lower())
                    if not cost_match:
                        continue
                    if getattr(card, 'is_spree', False):
                        castable = self._spree_alt_cast_payable(
                            player, card_id, card,
                            {"card": card,
                             "flashback_cost": cost_match.group(1),
                             "use_alt_cost": "flashback"})
                    else:
                        castable = (
                            self._can_afford_cost_string(
                                player, cost_match.group(1))
                            and self._targets_available(
                                card, player, opponent))
                    if castable:
                        # Context needs gy_idx
                        context = {'gy_idx': i}
                        # FIXED: Use correct action ID for CAST_WITH_FLASHBACK (398)
                        set_valid_action(398, f"CAST_WITH_FLASHBACK {card.name}", context=context)

        # Jump-start
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "jump-start" in card.oracle_text.lower():
                is_instant = 'instant' in getattr(card, 'card_types', [])
                if is_sorcery_speed or is_instant: # Check timing
                    if len(player["hand"]) > 0 and self._can_afford_card(player, card):
                        # FIXED: Use correct action ID for CAST_WITH_JUMP_START (399)
                        discard_idx = min(
                            range(len(player["hand"])),
                            key=lambda idx: self.card_evaluator.evaluate_card(
                                player["hand"][idx], "discard",
                                context_details={
                                    "perspective": (
                                        "p1" if player is gs.p1 else "p2"),
                                })
                            if self.card_evaluator else idx)
                        context = {'gy_idx': i, 'discard_idx': discard_idx}
                        set_valid_action(399, f"CAST_WITH_JUMP_START {card.name}", context=context)

        # Escape
        for i in range(min(len(player.get("graveyard",[])), 6)):
            card_id = player["graveyard"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "escape" in card.oracle_text.lower():
                is_instant = 'instant' in getattr(card, 'card_types', [])
                if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"escape—([^\,]+), exile ([^\.]+)", card.oracle_text.lower())
                    if cost_match:
                        cost_str = cost_match.group(1).strip()
                        exile_req_str = cost_match.group(2).strip()
                        exile_count = self._word_to_number(re.search(r"(\w+|\d+)", exile_req_str).group(1)) if re.search(r"(\w+|\d+)", exile_req_str) else 1

                        # Check if enough *other* cards exist in GY
                        if len(player["graveyard"]) > exile_count and self._can_afford_cost_string(player, cost_str):
                            # FIXED: Use correct action ID for CAST_WITH_ESCAPE (400)
                            escape_indices = [
                                idx for idx in range(len(player["graveyard"]))
                                if idx != i
                            ][:exile_count]
                            context = {
                                'gy_idx': i,
                                'gy_indices_escape': escape_indices,
                            }
                            set_valid_action(400, f"CAST_WITH_ESCAPE {card.name}", context=context)

        # Madness (Triggered when discarded, check if castable)
        if hasattr(gs, 'madness_cast_available') and gs.madness_cast_available:
            madness_info = gs.madness_cast_available
            if madness_info['player'] == player:
                card_id = madness_info['card_id']
                cost_str = madness_info['cost']
                card = gs._safe_get_card(card_id)

                # Find the card in exile
                exile_idx = -1
                for idx, exiled_id in enumerate(player.get("exile", [])):
                    if exiled_id == card_id:
                        exile_idx = idx
                        break

                # Check affordability
                if exile_idx != -1 and self._can_afford_cost_string(player, cost_str):
                    # FIXED: Use correct action ID for CAST_FOR_MADNESS (401)
                    context = {'exile_idx': exile_idx, 'card_id': card_id}
                    set_valid_action(401, f"CAST_FOR_MADNESS {card.name if card else card_id}", context=context)

        # Overload
        for i in range(min(len(player["hand"]), 8)):
            card_id = player["hand"][i]
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "overload" in card.oracle_text.lower():
                 is_instant = 'instant' in getattr(card, 'card_types', [])
                 if is_sorcery_speed or is_instant: # Check timing
                    cost_match = re.search(r"overload (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match and self._can_afford_cost_string(player, cost_match.group(1)):
                        context = {'hand_idx': i}
                        set_valid_action(402, f"CAST_WITH_OVERLOAD {card.name}", context=context)

        # Emerge (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "emerge" in card.oracle_text.lower():
                    cost_match = re.search(r"emerge (\{[^\}]+\})", card.oracle_text.lower())
                    if cost_match:
                        # Check if there's a creature to sacrifice
                        sacrifice_options = [
                            (idx, cid) for idx, cid in enumerate(player.get("battlefield", []))
                            if 'creature' in getattr(
                                gs._safe_get_card(cid), 'card_types', [])
                        ]
                        # Simplified cost check - full check happens later
                        if sacrifice_options and self._can_afford_cost_string(player, cost_match.group(1)):
                             sacrifice_idx, sacrifice_id = max(
                                 sacrifice_options,
                                 key=lambda pair: getattr(
                                     gs._safe_get_card(pair[1]), 'cmc', 0) or 0)
                             context = {
                                 'hand_idx': i,
                                 'sacrifice_idx': sacrifice_idx,
                                 'sacrificed_creature': sacrifice_id,
                             }
                             set_valid_action(403, f"CAST_FOR_EMERGE {card.name}", context=context)

        # Delve (Sorcery speed only)
        if is_sorcery_speed:
            for i in range(min(len(player["hand"]), 8)):
                card_id = player["hand"][i]
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'oracle_text') and "delve" in card.oracle_text.lower():
                    delve_indices = list(range(len(player.get("graveyard", []))))
                    affordability_context = {'delve_cards': delve_indices}
                    if delve_indices and self._can_afford_card(
                            player, card, context=affordability_context):
                        context = {
                            'hand_idx': i,
                            'gy_indices': delve_indices,
                        }
                        set_valid_action(404, f"CAST_FOR_DELVE {card.name}", context=context)

    def _add_emblem_graveyard_actions(self, player, opponent, valid_actions,
                                      set_valid_action, is_sorcery_speed):
        """Expose one distinct action per visible graveyard permission."""
        has_emblem = any(
                emblem.get("kind") == "graveyard_permanents"
                for emblem in player.get("emblems", []))
        has_land_permission = self.game_state.can_play_lands_from_graveyard(
            player)
        has_adventure_permission = any(
            self.game_state.has_graveyard_adventure_permission(
                player, card_id)
            for card_id in player.get("graveyard", []))
        has_flashback = any(
            self.game_state.flashback_cost_for(player, card_id)
            for card_id in player.get("graveyard", []))
        has_harmonize = any(
            self.game_state.harmonize_cost_for(player, card_id)
            for card_id in player.get("graveyard", []))
        if (not has_emblem and not has_land_permission
                and not has_adventure_permission
                and not has_flashback and not has_harmonize):
            return
        gs = self.game_state
        casting_allowed = gs.can_player_cast_spells(player)
        permanent_spell_types = {
            "creature", "artifact", "enchantment", "planeswalker", "battle"
        }
        for graveyard_index, card_id in enumerate(player.get("graveyard", [])[:6]):
            card = gs._safe_get_card(card_id)
            if not card:
                continue
            if ("land" not in getattr(card, "card_types", [])
                    and not casting_allowed):
                continue
            if gs.has_graveyard_adventure_permission(player, card_id):
                adventure = card.get_adventure_data() or {}
                adventure_type = adventure.get("type", "").lower()
                is_instant_adventure = "instant" in adventure_type
                if (not is_sorcery_speed and not is_instant_adventure):
                    continue
                if not self._can_afford_cost_string(
                        player, adventure.get("cost", ""), context={}):
                    continue
                if not self._targets_available_from_text(
                        adventure.get("effect", ""), player, opponent,
                        source_id=card_id):
                    continue
                set_valid_action(
                    472 + graveyard_index,
                    f"CAST_ADVENTURE_FROM_GRAVEYARD {card.name}",
                    context={
                        "source_zone": "graveyard",
                        "source_idx": graveyard_index,
                        "graveyard_adventure_cast": True,
                        "cast_as_adventure": True,
                    })
                continue
            flashback_cost = gs.flashback_cost_for(player, card_id)
            harmonize_cost = gs.harmonize_cost_for(player, card_id)
            if harmonize_cost:
                if not is_sorcery_speed:
                    continue
                candidates = [
                    creature_id for creature_id in player.get("battlefield", [])
                    if creature_id not in player.get("tapped_permanents", set())
                    and "creature" in getattr(
                        gs._safe_get_card(creature_id), "card_types", [])]
                reductions = [0] + [
                    max(0, int(getattr(
                        gs._safe_get_card(creature_id), "power", 0) or 0))
                    for creature_id in candidates]
                if getattr(card, "is_spree", False):
                    payable = any(
                        self._spree_alt_cast_payable(
                            player, card_id, card, {
                                "card": card,
                                "use_alt_cost": "harmonize",
                                "harmonize_cost": harmonize_cost,
                                "harmonize_reduction": reduction})
                        for reduction in reductions)
                else:
                    payable = any(
                        gs.mana_system.can_pay_mana_cost_with_lands(
                            player,
                            gs.mana_system.calculate_alternative_cost(
                                card_id, player, "harmonize", {
                                    "harmonize_cost": harmonize_cost,
                                    "harmonize_reduction": reduction,
                                }),
                            {"card": card})
                        for reduction in reductions)
                if not payable:
                    continue
                set_valid_action(
                    472 + graveyard_index,
                    f"CAST_WITH_HARMONIZE {card.name}",
                    context={
                        "source_zone": "graveyard",
                        "source_idx": graveyard_index,
                        "harmonize_cast": True,
                        "harmonize_cost": harmonize_cost,
                    })
                continue
            if flashback_cost:
                is_instant = "instant" in getattr(card, "card_types", [])
                if not is_sorcery_speed and not is_instant:
                    continue
                flashback_context = {"card": card,
                                     "flashback_cost": flashback_cost,
                                     "use_alt_cost": "flashback"}
                if getattr(card, "is_spree", False):
                    if not self._spree_alt_cast_payable(
                            player, card_id, card, flashback_context):
                        continue
                else:
                    if not self._can_afford_cost_string(
                            player, flashback_cost,
                            context=flashback_context):
                        continue
                    if not self._targets_available(card, player, opponent):
                        continue
                set_valid_action(
                    472 + graveyard_index,
                    f"CAST_WITH_FLASHBACK {card.name}",
                    context={
                        "source_zone": "graveyard",
                        "source_idx": graveyard_index,
                        "flashback_cast": True,
                        "flashback_cost": flashback_cost,
                    })
                continue
            card_types = set(getattr(card, "card_types", []))
            is_land = "land" in card_types
            if is_land:
                if (not (has_emblem or has_land_permission)
                        or not is_sorcery_speed
                        or not gs.can_play_land_this_turn(player)):
                    continue
            elif (not has_emblem
                  or not card_types.intersection(permanent_spell_types)):
                continue
            elif not is_sorcery_speed and not self._has_flash(card_id):
                continue
            cast_context = {
                "source_zone": "graveyard",
                "source_idx": graveyard_index,
            }
            if is_land and has_land_permission:
                cast_context["controlled_permanent_land_play"] = True
            else:
                cast_context["emblem_graveyard_cast"] = True
            if not is_land:
                if not self._can_afford_card(player, card, context=cast_context):
                    continue
                if not self._targets_available(card, player, opponent):
                    continue
            verb = "PLAY" if is_land else "CAST"
            set_valid_action(
                472 + graveyard_index,
                f"{verb}_FROM_GRAVEYARD {card.name}",
                context=cast_context)

    def _add_x_cost_actions(self, player, valid_actions, set_valid_action):
        """Legacy no-op: X is chosen before a spell is paid for and stacked.

        The old post-cast path used indices 358+, which are CHOOSE_MODE actions,
        and exposed them without a choose-X context.  The casting pipeline now
        creates the dedicated ``choose_x`` phase and actions 363-372 (plus PASS
        for X=0), so a spell already on the stack needs no further X action.
        """
        return

    def _add_kicker_options(self, player, valid_actions, set_valid_action):
        """Add options for paying kicker and related additional costs."""
        gs = self.game_state
        
        # Check for pending spell context that might need kicker decisions
        pending_context = getattr(gs, 'pending_spell_context', None)
        if pending_context and 'card_id' in pending_context and pending_context.get('controller') == player:
            card_id = pending_context['card_id']
            card = gs._safe_get_card(card_id)
            
            # Kicker options - Use correct indices 405 and 406
            if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                kicker_match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if kicker_match:
                    cost_str = kicker_match.group(1)
                    if cost_str.isdigit(): 
                        cost_str = f"{{{cost_str}}}"
                    
                    # Only show PAY_KICKER if it's affordable
                    if self._can_afford_cost_string(player, cost_str, context=pending_context):
                        set_valid_action(405, f"PAY_KICKER for {card.name}")
                    
                    # Always allow NOT paying kicker (it's optional)
                    set_valid_action(406, f"DONT_PAY_KICKER for {card.name}")
            
            # Additional Cost options - Use correct indices 407 and 408
            if card and hasattr(card, 'oracle_text') and "additional cost" in card.oracle_text.lower():
                # Parse the additional cost to determine if it's optional or mandatory
                cost_info = self._get_additional_cost_info(card)
                is_optional = cost_info.get("optional", True) if cost_info else True
                
                # Only show PAY_ADDITIONAL_COST if it's payable
                if cost_info and self._can_pay_specific_additional_cost(player, cost_info, pending_context):
                    set_valid_action(407, f"PAY_ADDITIONAL_COST for {card.name}")
                
                # Only show DON'T_PAY option if the cost is optional
                if is_optional:
                    set_valid_action(408, f"DONT_PAY_ADDITIONAL_COST for {card.name}")
            
            # Escalate options - Use correct index 409
            if card and hasattr(card, 'oracle_text') and "escalate" in card.oracle_text.lower():
                escalate_match = re.search(r"escalate (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                if escalate_match:
                    cost_str = escalate_match.group(1)
                    if cost_str.isdigit(): 
                        cost_str = f"{{{cost_str}}}"
                    
                    # Extract info about available modes
                    num_modes = 0
                    if hasattr(card, 'modes'):
                        num_modes = len(card.modes)
                    elif "choose one" in card.oracle_text.lower():
                        num_modes = card.oracle_text.lower().count("•")
                    
                    # Only show PAY_ESCALATE if more than one mode exists and cost is affordable
                    if num_modes > 1 and self._can_afford_cost_string(player, cost_str, context=pending_context):
                        # For each possible extra mode (up to 3)
                        for extra_modes in range(1, min(num_modes, 4)):
                            # Check if we can afford the cost multiple times
                            if self._can_afford_cost_string(player, f"{cost_str}*{extra_modes}", context=pending_context):
                                escalate_context = {'num_extra_modes': extra_modes}
                                set_valid_action(409, f"PAY_ESCALATE for {extra_modes} extra mode(s)", context=escalate_context)
                                break  # Just add one action; context will specify how many modes
        
        # Check spells currently on the stack that belong to the player
        # This handles cases where the spell is already on stack but needs kicker decision
        for item in gs.stack:
            if isinstance(item, tuple) and len(item) >= 3:
                stack_type, card_id, controller = item[:3]
                if stack_type == "SPELL" and controller == player:
                    card = gs._safe_get_card(card_id)
                    context = item[3] if len(item) > 3 else {}
                    
                    # Check if this spell is waiting for kicker decision
                    if context.get('waiting_for_kicker_choice'):
                        # Similar logic as above, but for stack items
                        if card and hasattr(card, 'oracle_text') and "kicker" in card.oracle_text.lower():
                            kicker_match = re.search(r"kicker (\{[^\}]+\}|[0-9]+)", card.oracle_text.lower())
                            if kicker_match and self._can_afford_cost_string(player, kicker_match.group(1), context=context):
                                set_valid_action(405, f"PAY_KICKER for {card.name}")
                            set_valid_action(406, f"DONT_PAY_KICKER for {card.name}")

    def _add_split_card_actions(self, player, valid_actions, set_valid_action):
        """Add actions for split cards."""
        gs = self.game_state
        if not gs.can_player_cast_spells(player):
            return
        
        for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
            card = gs._safe_get_card(card_id)
            
            # Check if it's a split card
            is_split = False
            if card and hasattr(card, 'layout'):
                is_split = card.layout == "split"
            elif card and hasattr(card, 'oracle_text') and "//" in card.oracle_text:
                is_split = True
            
            if is_split:
                # Extract information about both halves
                has_left_half = hasattr(card, 'left_half')
                has_right_half = hasattr(card, 'right_half')
                
                # Left half casting - Use correct action index 445
                if has_left_half:
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    can_afford_left = self._can_afford_cost_string(player, left_cost)
                    
                    if can_afford_left:
                        context = {'hand_idx': idx}
                        set_valid_action(445, f"CAST_LEFT_HALF of {card.name}", context=context)
                
                # Right half casting - Use correct action index 446
                if has_right_half:
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    can_afford_right = self._can_afford_cost_string(player, right_cost)
                    
                    if can_afford_right:
                        context = {'hand_idx': idx}
                        set_valid_action(446, f"CAST_RIGHT_HALF of {card.name}", context=context)
                
                # Fuse (both halves) - Use correct action index 447
                if has_left_half and has_right_half and "fuse" in getattr(card, 'oracle_text', '').lower():
                    # Need to afford both costs
                    left_cost = card.left_half.get('mana_cost', card.mana_cost)
                    right_cost = card.right_half.get('mana_cost', card.mana_cost)
                    
                    # This is simplistic; a real implementation should combine costs correctly
                    total_cost = left_cost + right_cost  
                    can_afford_both = self._can_afford_cost_string(player, total_cost)
                    
                    if can_afford_both:
                        context = {'hand_idx': idx}
                        set_valid_action(447, f"CAST_FUSE of {card.name}", context=context)
            
            # Check if it's an aftermath card
            is_aftermath = False
            if card and hasattr(card, 'layout'):
                is_aftermath = card.layout == "aftermath"
            elif card and hasattr(card, 'oracle_text') and "aftermath" in card.oracle_text.lower():
                is_aftermath = True
            
            # Add aftermath actions for graveyard - Use correct action index 448
            if is_aftermath:
                for g_idx, g_card_id in enumerate(player["graveyard"][:6]):  # First 6 in graveyard
                    g_card = gs._safe_get_card(g_card_id)
                    if g_card and hasattr(g_card, 'layout') and g_card.layout == "aftermath":
                        # Check if it has a castable aftermath half
                        if hasattr(g_card, 'right_half'):
                            right_cost = g_card.right_half.get('mana_cost', g_card.mana_cost)
                            can_afford = self._can_afford_cost_string(player, right_cost)
                            
                            if can_afford:
                                context = {'gy_idx': g_idx}
                                set_valid_action(448, f"AFTERMATH_CAST of {g_card.name}", context=context)

    def _add_damage_prevention_actions(self, player, valid_actions, set_valid_action):
        """Add actions for preventing or redirecting damage."""
        gs = self.game_state
        if not gs.can_player_cast_spells(player):
            return
        
        # Check if damage is being dealt (only relevant in combat or with damage effects on stack)
        is_damage_being_dealt = gs.phase in [gs.PHASE_COMBAT_DAMAGE, gs.PHASE_FIRST_STRIKE_DAMAGE]
        # Stack responses are already enumerated by _add_response_actions.
        # Keep this helper for the no-stack combat-damage window only, avoiding
        # duplicate routes to the same response in the overflow catalog.
        if not is_damage_being_dealt or gs.stack:
            return

        # Check for prevention effects in hand.
        for idx, card_id in enumerate(player["hand"][:8]):  # Limit to first 8
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                continue
            if (has_damage_prevention_instruction(card.oracle_text)
                    and self._can_use_specialized_response_cast(
                        card_id, player, card)):
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(
                        player, card.mana_cost
                        if hasattr(card, 'mana_cost') else "")
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                if can_afford:
                    set_valid_action(
                        432, f"PREVENT_DAMAGE with {card.name}",
                        context={'hand_idx': idx})

            if ("redirect" in card.oracle_text.lower()
                    and "damage" in card.oracle_text.lower()
                    and self._can_use_specialized_response_cast(
                        card_id, player, card)):
                if hasattr(gs, 'mana_system'):
                    can_afford = gs.mana_system.can_pay_mana_cost(
                        player, card.mana_cost
                        if hasattr(card, 'mana_cost') else "")
                else:
                    can_afford = sum(player["mana_pool"].values()) > 0
                if can_afford:
                    set_valid_action(
                        433, f"REDIRECT_DAMAGE with {card.name}",
                        context={'hand_idx': idx})

