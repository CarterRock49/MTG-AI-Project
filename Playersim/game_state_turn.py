"""Turn structure, phase progression, priority, and turn-cycle mechanics.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import logging
import re


class GameStateTurnMixin:
    """Turn structure, phase progression, priority, and turn-cycle mechanics."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def initialize_day_night_cycle(self):
        """Initialize the day/night cycle state and tracking."""
        # Start with neither day nor night
        self.day_night_state = None
        # Track if we've already checked day/night transition this turn
        self.day_night_checked_this_turn = False
        logging.debug("Day/night cycle initialized (neither day nor night)")

    def _phasing_player_key(self, player):
        """Return a clone-safe key for a player object."""
        if player is self.p1:
            return "p1"
        if player is self.p2:
            return "p2"
        return None

    def _phasing_player_from_key(self, key):
        if key == "p1":
            return self.p1
        if key == "p2":
            return self.p2
        return None

    def _build_phasing_groups(self, player, direct_ids):
        """Group directly phasing permanents with attached permanents."""
        reverse_links = {}
        attached_to = {}
        for attachment_controller in (self.p1, self.p2):
            if not attachment_controller:
                continue
            battlefield = attachment_controller.get("battlefield", [])
            for attachment_id, target_id in attachment_controller.get("attachments", {}).items():
                if attachment_id not in battlefield:
                    continue
                reverse_links.setdefault(target_id, []).append((attachment_id, attachment_controller))
                attached_to[attachment_id] = target_id

        direct_ids = list(dict.fromkeys(direct_ids))
        direct_set = set(direct_ids)
        roots = [card_id for card_id in direct_ids if attached_to.get(card_id) not in direct_set]
        roots.extend(card_id for card_id in direct_ids if card_id not in roots)

        groups = []
        claimed = set()
        for root_id in roots:
            if root_id in claimed:
                continue
            members = []
            pending = [(root_id, player)]
            while pending:
                card_id, controller = pending.pop(0)
                if card_id in claimed:
                    continue
                if card_id not in controller.get("battlefield", []):
                    continue
                claimed.add(card_id)
                members.append((card_id, controller))
                pending.extend(reverse_links.get(card_id, []))
            if members:
                groups.append((root_id, members))
        return groups

    def _phase_out_group(self, root_id, members, phase_player):
        """Phase out a permanent and everything attached to it as one group."""
        phase_player.setdefault("phased_out_permanents", set()).add(root_id)
        for card_id, controller in members:
            battlefield = controller.get("battlefield", [])
            if card_id not in battlefield:
                continue
            battlefield.remove(card_id)
            self.phased_out.add(card_id)
            self.phased_out_state[card_id] = {
                "controller": self._phasing_player_key(controller),
                "phase_in_with": root_id,
                "tapped": card_id in controller.get("tapped_permanents", set()),
            }
            controller.get("tapped_permanents", set()).discard(card_id)

            card = self._safe_get_card(card_id)
            logging.debug(f"Phased out: {getattr(card, 'name', card_id)}")
            if self.ability_handler:
                self.ability_handler.unregister_card_abilities(card_id)
            else:
                if self.layer_system:
                    self.layer_system.remove_effects_by_source(card_id)
                if self.replacement_effects:
                    self.replacement_effects.remove_effects_by_source(card_id)

        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()

    def _phase_in_group(self, root_id):
        """Restore a directly phased permanent and its indirect attachments."""
        member_ids = [
            card_id for card_id in list(self.phased_out)
            if self.phased_out_state.get(card_id, {}).get("phase_in_with") == root_id
        ]
        restored = []
        for card_id in member_ids:
            stored = self.phased_out_state.get(card_id, {})
            controller = self._phasing_player_from_key(stored.get("controller"))
            if not controller:
                controller = self._find_card_owner_fallback(card_id)
            if not controller:
                logging.error(f"Cannot phase in {card_id}: controller is unknown.")
                continue

            if card_id not in controller.get("battlefield", []):
                controller["battlefield"].append(card_id)
            self.phased_out.discard(card_id)
            if stored.get("tapped"):
                controller.setdefault("tapped_permanents", set()).add(card_id)
            else:
                controller.get("tapped_permanents", set()).discard(card_id)
            if hasattr(self, "_last_card_locations"):
                self._last_card_locations[card_id] = (controller, "battlefield")
            restored.append((card_id, controller))

        # All group members must exist again before abilities and attachment
        # effects are rebuilt, especially for cross-controller Auras.
        for card_id, controller in restored:
            if self.ability_handler:
                self.ability_handler.register_card_abilities(card_id, controller)
            elif self.replacement_effects:
                self.replacement_effects.register_card_replacement_effects(card_id, controller)

        restored_ids = {card_id for card_id, _ in restored}
        for attachment_controller in (self.p1, self.p2):
            if not attachment_controller:
                continue
            for attachment_id, target_id in attachment_controller.get("attachments", {}).items():
                if attachment_id in restored_ids:
                    self._register_attachment_effects(attachment_id, target_id)

        if self.layer_system:
            self.layer_system.invalidate_cache()
            self.layer_system.apply_all_effects()

        for card_id, controller in restored:
            self.phased_out_state.pop(card_id, None)
            card = self._safe_get_card(card_id)
            logging.debug(f"Phased in: {getattr(card, 'name', card_id)}")
            self.trigger_ability(card_id, "PHASED_IN", {"controller": controller})
        return [card_id for card_id, _ in restored]

    def _untap_phase(self, player, previous_turn_spells_cast=None):
        """Reset mana and untap all permanents, handling Phasing."""
        # --- Phasing ---
        # 1. Phase In Permanents that should return
        permanents_phasing_in = []
        if hasattr(self, 'phased_out'):
            # Check player's phased-out permanents first
            player_phased_out = player.get("phased_out_permanents", set())
            for card_id in list(player_phased_out): # Iterate copy
                 if card_id in self.phased_out: # Confirm it's in global set
                      permanents_phasing_in.extend(self._phase_in_group(card_id))
                 player_phased_out.discard(card_id)

        # 2. Check Permanents with Phasing on Battlefield
        # CR 702.26 (July 2026 fix): all phasing events of an untap step are
        # simultaneous -- a permanent that just phased in must NOT be phased
        # right back out by this scan. The old sequential in-then-out made
        # every phasing permanent oscillate invisibly (net: gone forever).
        _just_phased_in = set(permanents_phasing_in)
        permanents_phasing_out = []
        for card_id in list(player.get("battlefield",[])): # Iterate copy
             if card_id in _just_phased_in:
                 continue
             card = self._safe_get_card(card_id)
             # Check keyword via Layer System result preferred
             if card and self.check_keyword(card_id, "phasing"):
                 permanents_phasing_out.append(card_id)

        # 3. Phase Out identified permanents
        if permanents_phasing_out:
            for root_id, members in self._build_phasing_groups(player, permanents_phasing_out):
                self._phase_out_group(root_id, members, player)

        # CR 727.1a: this turn-based action happens after phasing and before
        # permanents untap, using the previous turn's active-player spell count.
        if previous_turn_spells_cast is not None:
            self.check_day_night_transition(previous_turn_spells_cast)

        # --- Standard Untap Actions ---
        # Reset mana pools
        player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        player["conditional_mana"] = {}
        player["phase_restricted_mana"] = {}

        # Untap permanents *that did not phase out*. Route every attempt
        # through untap_permanent so stun and other replacements apply.
        attempted_ids = set()
        for card_id in list(player.get("battlefield", [])): # Iterate copy, only those currently on BF
            # Repeated deck copies currently share definition IDs. Never make
            # several untap attempts against the same tracked object in one
            # untap step, which would consume several stun counters at once.
            if card_id in attempted_ids:
                 continue
            attempted_ids.add(card_id)
            if card_id in player.get("tapped_permanents", set()):
                 self.untap_permanent(card_id, player)

        player["entered_battlefield_this_turn"] = set() # Clear sickness status
        player["land_played"] = False
        player["damage_counters"] = {} # Damage removed in Cleanup usually, but safe reset here? Rule 514.2. Okay.
        logging.debug(f"Untap Phase for {player['name']} complete.")

    def _draw_phase(self, player):
        """Draw a card from the library with replacement effect handling."""
        self._draw_card(player)

    def _draw_card(self, player):
        """Draw one card through the canonical replacement/telemetry path."""
        if not player or not player.get("library"):
            if player:
                player["attempted_draw_from_empty"] = True
                player["life"] = 0
            self.terminal_reason = "decking"
            # Drawing from an empty library is an ordinary rules-defined loss,
            # not an engine degradation.  Keep it visible without polluting
            # warning triage for successful long games.
            logging.info("Draw Phase: No cards left in library. Player loses the game!")
            self.check_state_based_actions()
            return None

        player_key = 'p1' if player is self.p1 else 'p2'
        draw_context = {
            "player": player,
            "draw_count": 1,
            "card_id": player["library"][0],
        }
        modified_context, was_replaced = self.apply_replacement_effect(
            "DRAW", draw_context)
        if was_replaced:
            # Replacement implementations perform their own zone movement.
            # Effects such as dredge are not draws and intentionally record
            # nothing unless the replacement exposes an actual drawn card.
            drawn_card_id = (modified_context or {}).get("drawn_card_id")
            if drawn_card_id is not None:
                self._record_card_draw(player_key, drawn_card_id)
            return drawn_card_id

        card_id = player["library"].pop(0)
        was_first_draw = self.cards_drawn_this_turn.get(player_key, 0) == 0
        player["hand"].append(card_id)
        self._record_card_draw(player_key, card_id)

        miracle_handled = self.handle_miracle_draw(
            card_id, player, is_first_draw=was_first_draw)
        card = self._safe_get_card(card_id)
        logging.debug(
            "Draw Phase: Drew %s%s",
            getattr(card, 'name', card_id),
            " and opened a miracle window" if miracle_handled else "")
        return card_id

    def _record_card_draw(self, player_key, card_id):
        """Record a completed draw without conflating it with opening hands."""
        self.cards_drawn_this_turn[player_key] = (
            self.cards_drawn_this_turn.get(player_key, 0) + 1)
        self.draw_history.setdefault(player_key, {}).setdefault(
            int(self.turn), []).append(card_id)

    def _end_phase(self, player):
        """Cleanup at end phase."""
        player["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        # Enforce hand size limits, etc.
        if len(player["hand"]) > self.max_hand_size:
            player["hand"] = player["hand"][:self.max_hand_size]
        # Revert any temporary control effects at end of turn
        self._revert_temporary_control()

    def _can_respond_to_stack(self, player=None):
        """
        Check if the player can respond to the stack, considering effects like Split Second.
        
        Args:
            player: The player who is trying to respond
            
        Returns:
            bool: Whether the player can respond to the stack
        """
        # If Split Second is active, no player can respond
        if hasattr(self, 'split_second_active') and self.split_second_active:
            return False
            
        # Otherwise, check normal priority rules
        return self.check_priority(player)

    def _can_act_at_sorcery_speed(self, player):
        """
        Check if the specified player can act at sorcery speed.
        Rules:
        1. It must be the player's turn (Active Player).
        2. It must be a Main Phase.
        3. The stack must be empty.
        """
        # Must be the active player
        if player != self._get_active_player():
            return False
            
        # Priority is represented as a transient phase while the underlying
        # turn phase is retained in previous_priority_phase. After a spell or
        # ability resolves, the active player still has sorcery-speed timing
        # in that same main phase when the stack is empty.
        effective_phase = self.phase
        if (effective_phase == self.PHASE_PRIORITY
                and self.previous_priority_phase is not None):
            effective_phase = self.previous_priority_phase
        if effective_phase not in [self.PHASE_MAIN_PRECOMBAT,
                                   self.PHASE_MAIN_POSTCOMBAT]:
            return False
            
        # Stack must be empty
        if self.stack:
            return False
            
        return True

    def _pass_priority(self):
            """
            Handle passing priority between players or advancing state.
            Strictly enforces Rule 117:
            - If a player passes, priority goes to the next player.
            - If both pass in succession:
                - If stack is not empty: Resolve top item. AP gets priority.
                - If stack is empty: Advance phase. AP gets priority in new phase.
            """
            if self.finish_optional_copy_targeting():
                return
            if self.phase == self.PHASE_CHOOSE and self.choice_context:
                choice_type = self.choice_context.get("type")
                if choice_type == "opening_hand":
                    # Declines the player's remaining begin-game placements.
                    self.complete_opening_hand_choice(None)
                    return
                if choice_type == "linked_exile" and self.choice_context.get("optional", False):
                    self.decline_linked_exile_choice()
                    return
                if choice_type == "mockingbird_copy":
                    self.complete_mockingbird_copy_choice(None)
                    return
                if choice_type == "bargain":
                    self.complete_bargain_choice(None)
                    return
                if choice_type == "choose_mode":
                    self.finalize_modal_spell_choice()
                    return
                if choice_type == "choose_x" and self.choice_context.get("min_x", 0) <= 0:
                    self.choose_x_for_pending_spell(0)
                    return
                if choice_type == "collect_evidence":
                    self.finish_collect_evidence_choice()
                    return

            # --- 1. Critical Recovery: If priority is lost (None), reset to Active Player ---
            # This fixes the infinite NO_OP loop bug.
            if self.priority_player is None:
                # In Untap (0) and Cleanup (15), no player has priority by default.
                # However, if we are 'stuck' here with no actions happening, we force advance.
                if self.phase == self.PHASE_UNTAP:
                    logging.warning("Stuck in UNTAP with no priority. Forcing advance to UPKEEP.")
                    self._empty_mana_pools()
                    self.phase = self.PHASE_UPKEEP
                    self.priority_player = self._get_active_player()
                    self.priority_pass_count = 0
                    return
                
                # In all other phases (Upkeep, Main, Combat, etc.), SOMEONE must have priority.
                # Default to Active Player (Rule 117.1).
                if self.phase != self.PHASE_CLEANUP:
                    active_p = self._get_active_player()
                    logging.warning(f"Priority was None in interactive phase {self._PHASE_NAMES.get(self.phase)}. Resetting to Active Player: {active_p['name']}")
                    self.priority_player = active_p
                    self.priority_pass_count = 0
                    return

            # --- 2. Standard Pass Logic ---
            self.priority_pass_count += 1
            
            # Calculate next player (toggle)
            current_prio = self.priority_player
            active_p = self._get_active_player()
            non_active_p = self._get_non_active_player()
            
            # If current is Active, next is Non-Active. Otherwise, back to Active.
            next_player = non_active_p if current_prio == active_p else active_p
            
            # Tentatively assign priority to the next player
            self.priority_player = next_player 
            # logging.debug(f"Priority passed from {current_prio['name'] if current_prio else 'None'} to {next_player['name']}")

            # --- 3. Check if State Should Change (Both Players Passed) ---
            if self.priority_pass_count >= 2:
                # Both players passed in succession. Action required.
                self.priority_pass_count = 0 # Reset pass count immediately

                # A. Check Split Second (Rare, but overrides stack logic)
                if getattr(self, 'split_second_active', False):
                    # Find the Split Second spell/ability on top
                    split_second_item = None
                    if self.stack and isinstance(self.stack[-1], tuple) and len(self.stack[-1]) > 3 and self.stack[-1][3].get("is_split_second", False):
                        split_second_item = self.stack[-1]

                    if split_second_item:
                        logging.debug("Split Second active: Resolving split second spell/ability.")
                        self.resolve_top_of_stack()
                        # A mid-resolution choice retains priority with its
                        # chooser; otherwise AP gets priority after resolution.
                        if self.choice_context:
                            self.priority_player = self.choice_context.get('player')
                        elif self.targeting_context:
                            self.priority_player = self.targeting_context.get('controller')
                        else:
                            self.priority_player = active_p
                        return
                    else:
                        # Split second flag was true but no item found; clean up state
                        self.split_second_active = False
                        self.priority_player = active_p
                        return

                # B. Check Stack Resolution
                elif self.stack:
                    # Process triggers FIRST that might have resulted from the passes
                    initial_stack_size = len(self.stack)
                    if self.ability_handler: 
                        self.ability_handler.process_triggered_abilities()

                    # Check if new triggers were added to the stack
                    if len(self.stack) > initial_stack_size:
                        # New items added: Active Player gets priority to respond (Rule 117.3c)
                        self.priority_player = active_p
                        self.last_stack_size = len(self.stack)
                        logging.debug("Triggers added after pass, priority returned to AP.")
                        return

                    # No new triggers, resolve the top item
                    # logging.debug("Both passed, resolving stack...")
                    self.resolve_top_of_stack()

                    # Rule 117.3b: Active player receives priority after a spell/ability resolves.
                    if self.targeting_context:
                        self.priority_player = self.targeting_context.get('controller')
                    elif self.sacrifice_context:
                        self.priority_player = self.sacrifice_context.get('controller')
                    elif self.choice_context:
                        self.priority_player = self.choice_context.get('player')
                    else:
                        self.priority_player = active_p
                    self.last_stack_size = len(self.stack)
                    return

                # C. Check Phase Advance (Stack is Empty)
                else:
                    # Advance phase only if stack is empty AND no special choice context is pending
                    if not (self.targeting_context or self.sacrifice_context or self.choice_context):
                        self._advance_phase() # This resets priority and handles next phase start
                    else: 
                        # If choice context pending, force priority to the chooser to prevent stuck state
                        chooser = None
                        if self.targeting_context: chooser = self.targeting_context.get("controller")
                        elif self.sacrifice_context: chooser = self.sacrifice_context.get("controller")
                        elif self.choice_context: chooser = self.choice_context.get("player")
                        
                        self.priority_player = chooser if chooser else active_p

    def _empty_mana_pools(self):
            """CR 500.4: empty every mana pool when a step or phase ends."""
            empty_pool = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
            for player in (self.p1, self.p2):
                if not player:
                    continue
                player["mana_pool"] = dict(empty_pool)
                player["conditional_mana"] = {}
                player["phase_restricted_mana"] = {}

    def _advance_phase(self):
            """
            Advance to the next phase in the turn sequence.
            Ensures Active Player receives priority immediately (Rule 117.1).
            Handles Cleanup Step loops (Rule 514.3).
            """
            # Phase sequence definition
            phase_sequence = [
                self.PHASE_UNTAP,
                self.PHASE_UPKEEP,
                self.PHASE_DRAW,
                self.PHASE_MAIN_PRECOMBAT,
                self.PHASE_BEGIN_COMBAT,
                self.PHASE_DECLARE_ATTACKERS,
                self.PHASE_DECLARE_BLOCKERS,
                self.PHASE_FIRST_STRIKE_DAMAGE,
                self.PHASE_COMBAT_DAMAGE,
                self.PHASE_END_OF_COMBAT,
                self.PHASE_MAIN_POSTCOMBAT,
                self.PHASE_END_STEP,
                self.PHASE_CLEANUP
            ]

            old_phase = self.phase
            
            # --- Handle Special Phase Exits ---
            # If we were in a sub-phase (Targeting, etc.) and are done, return to the game phase
            if old_phase in [self.PHASE_PRIORITY, self.PHASE_TARGETING, self.PHASE_SACRIFICE, self.PHASE_CHOOSE]:
                if not self.stack and not self.targeting_context and not self.sacrifice_context and not self.choice_context:
                    resume_phase = self.previous_priority_phase
                    if resume_phase not in phase_sequence:
                        resume_phase = getattr(self, '_last_turn_phase', None)
                    if resume_phase not in phase_sequence:
                        raise RuntimeError(
                            "Cannot advance transient phase "
                            f"{old_phase}: no valid underlying turn phase "
                            f"(previous={self.previous_priority_phase!r}, "
                            f"last={getattr(self, '_last_turn_phase', None)!r}).")

                    # Normalize the actual phase before advancing from the
                    # phase that opened this internal priority window. Nested
                    # choices can overwrite the single legacy resume slot with
                    # PHASE_PRIORITY; _last_turn_phase remains authoritative.
                    self.phase = resume_phase
                    self.previous_priority_phase = None
                    self._phase_action_count = 0
                else:
                    return # State busy, do not advance

            # --- Phase Advancement Loop (handles skipping) ---
            current_phase_for_check = self.phase
            loop_limit = 0
            
            while loop_limit < 20: # Safety break for infinite skips
                loop_limit += 1

                # Find current phase index
                try:
                    current_idx = phase_sequence.index(current_phase_for_check)
                except ValueError:
                    raise RuntimeError(
                        f"Cannot advance unknown phase {current_phase_for_check}; "
                        "refusing to reset the turn to main phase.")

                # Determine next phase
                next_idx = (current_idx + 1) % len(phase_sequence)
                next_phase_in_sequence = phase_sequence[next_idx]

                # CR 505.5a: a registered additional combat phase is entered
                # instead of the postcombat main phase, once per registration.
                if (current_phase_for_check == self.PHASE_END_OF_COMBAT
                        and next_phase_in_sequence == self.PHASE_MAIN_POSTCOMBAT
                        and getattr(self, 'extra_combat_phases', 0) > 0):
                    self.extra_combat_phases -= 1
                    self.current_attackers = []
                    self.current_block_assignments = {}
                    self.combat_damage_dealt = False
                    if hasattr(self, 'exerted_this_combat'):
                        self.exerted_this_combat = set()
                    next_phase_in_sequence = self.PHASE_BEGIN_COMBAT
                    logging.debug(
                        f"Entering an additional combat phase "
                        f"({self.extra_combat_phases} more pending).")

                # --- 1. Handle Turn Transition (Cleanup -> Untap) ---
                if current_phase_for_check == self.PHASE_CLEANUP and next_phase_in_sequence == self.PHASE_UNTAP:
                    # Perform Cleanup Actions (Rule 514)
                    active_p = self._get_active_player()
                    non_active_p = self._get_non_active_player()
                    
                    # Discard down to hand size, remove damage, etc.
                    if self._cleanup_step_actions(active_p):
                        return
                    if self._cleanup_step_actions(non_active_p, discard_to_max=False):
                        return
                    
                    # Rule 514.3: Check for State-Based Actions or Triggers
                    sba_happened = self.check_state_based_actions()
                    
                    # If SBAs happened or triggers triggered, players get priority, then another cleanup happens.
                    if sba_happened or self.stack:
                        logging.info("Events occurred during Cleanup. Active Player gets priority.")
                        self.priority_player = active_p
                        self.priority_pass_count = 0
                        return # Stop advancement, handle priority in Cleanup

                    # A cleanup loop can grant mana while players receive
                    # priority. Empty it before the next turn begins.
                    self._empty_mana_pools()

                    # Preserve the prior active player's count before the new
                    # turn resets per-turn casting history.
                    previous_turn_spell_count = sum(
                        1 for spell in self.spells_cast_this_turn
                        if isinstance(spell, tuple) and len(spell) >= 2
                        and spell[1] == active_p)

                    # If clean, proceed to NEXT TURN
                    self.turn += 1
                    logging.info(f"=== ADVANCING TO TURN {self.turn} ===")
                    
                    self._reset_turn_tracking_variables()
                    new_active_p = self._get_active_player() # Update active player var

                    # UNTAP STEP (Rule 502) - No priority
                    self._untap_phase(new_active_p, previous_turn_spell_count)
                    self.check_state_based_actions() 

                    # Auto-advance to UPKEEP (Rule 503) - Priority starts here
                    self.phase = self.PHASE_UPKEEP
                    self._phase_action_count = 0
                    
                    # Trigger "Beginning of Upkeep"
                    self._handle_beginning_of_phase_triggers() 

                    # Rule 117.1: Active Player gets priority
                    self.priority_player = new_active_p
                    self.priority_pass_count = 0
                    self.last_stack_size = len(self.stack)
                    # logging.debug(f"Turn {self.turn} Upkeep. Priority to {new_active_p['name']}")
                    
                    # Check Game End (Turn Limit)
                    if self.turn > self.max_turns:
                        # Logic handled in check_state_based_actions / env step
                        pass
                    return

                # --- 2. Check for Phase Skipping (e.g. First Strike) ---
                should_skip = False
                if next_phase_in_sequence == self.PHASE_FIRST_STRIKE_DAMAGE and not self._combat_has_first_strike():
                    should_skip = True
                
                # If explicitly skipping Untap (should be handled by logic above, but failsafe)
                if next_phase_in_sequence == self.PHASE_UNTAP:
                    should_skip = True # Loop back around to handle turn transition logic
                
                if should_skip:
                    current_phase_for_check = next_phase_in_sequence
                    continue # Loop to next

                # --- 3. Enter Next Phase ---
                self._empty_mana_pools()
                self.phase = next_phase_in_sequence
                self._phase_action_count = 0
                active_p = self._get_active_player()

                # Handle "At Beginning of Step" triggers
                if self.phase == self.PHASE_DRAW:
                    self._draw_phase(active_p) # Turn-based action: Draw
                
                self._handle_beginning_of_phase_triggers()

                # Rule 117.1: Active Player gets priority
                self.priority_player = active_p
                self.priority_pass_count = 0
                self.last_stack_size = len(self.stack)
                
                # logging.debug(f"Entering {self._PHASE_NAMES.get(self.phase)}. Priority to {active_p['name']}")
                return # Phase advanced successfully

            # Fallback if loop limit hit
            logging.error("Phase advancement loop limit reached. Defaulting to Main Phase.")
            self._empty_mana_pools()
            self.phase = self.PHASE_MAIN_PRECOMBAT
            self.priority_player = self._get_active_player()
            self.priority_pass_count = 0

    def _reset_turn_tracking_variables(self):
        """Helper to reset variables at the start of a new turn."""
        self.combat_damage_dealt = False
        self.day_night_checked_this_turn = False
        self.spells_cast_this_turn = []
        self.attackers_this_turn = set()
        self.creatures_died_this_turn = {}
        self.extra_combat_phases = 0
        self.damage_dealt_this_turn = {}
        self.cards_drawn_this_turn = {'p1': 0, 'p2': 0} # Reset draw counts
        self.cards_milled_this_turn = {'p1': 0, 'p2': 0} # Reset mill counts
        # Proper graveyard tracking reset
        if not hasattr(self, 'cards_to_graveyard_this_turn'): self.cards_to_graveyard_this_turn = {}
        self.cards_to_graveyard_this_turn[self.turn] = []
        turns_to_keep = 1
        keys_to_delete = [t for t in self.cards_to_graveyard_this_turn if t < self.turn - turns_to_keep]
        for t in keys_to_delete: del self.cards_to_graveyard_this_turn[t]
        self.gravestorm_count = 0
        self.boast_activated = set()
        self.forecast_used = set()
        self.life_gained_this_turn = {}
        self.damage_this_turn = {}
        # Player flags
        for player in [self.p1, self.p2]:
            if player:
                player["land_played"] = False
                player["entered_battlefield_this_turn"] = set()
                player["activated_this_turn"] = set()
                player["targeted_permanents_this_turn"] = set()
                player["lost_life_this_turn"] = False
                player["pw_activations"] = {} # Reset PW activations per turn
        logging.debug(f"Turn {self.turn}: Reset turn tracking variables.")

    def register_delayed_trigger(self, effect, phase=None, description=""):
        """Register a delayed triggered ability (CR 603.7).

        effect: zero-argument callable performing the ability's effect.
        phase:  the phase constant at whose beginning the trigger fires
                (e.g. self.PHASE_END_STEP for "at the beginning of the next
                end step"). None means "as soon as the current event fully
                resolves" (fired at the next state-based check), matching the
                legacy bare-callable producers.
        Triggers fire exactly once, then expire.
        """
        if not hasattr(self, 'delayed_triggers') or self.delayed_triggers is None:
            self.delayed_triggers = []
        self.delayed_triggers.append({
            "phase": phase,
            "effect": effect,
            "description": description or "delayed trigger",
        })

    def process_delayed_triggers(self, current_phase=None):
        """Fire due delayed triggers; returns how many fired.

        current_phase=None fires only the asap class (bare callables and
        phase=None registrations). With a phase, also fires triggers waiting
        for that phase's beginning. Iterates a snapshot so effects that
        register new delayed triggers wait for the next occurrence (a trigger
        created during an end step fires at the NEXT end step, per 603.7a).
        """
        pending = getattr(self, 'delayed_triggers', None)
        if not pending:
            return 0
        fired = 0
        snapshot = list(pending)
        for entry in snapshot:
            is_bare = callable(entry) and not isinstance(entry, dict)
            phase = None if is_bare else entry.get("phase")
            due = (phase is None) or (current_phase is not None and phase == current_phase)
            if not due:
                continue
            try:
                pending.remove(entry)
            except ValueError:
                continue  # already consumed by re-entrant processing
            effect = entry if is_bare else entry.get("effect")
            desc = "legacy asap trigger" if is_bare else entry.get("description", "delayed trigger")
            try:
                if callable(effect):
                    effect()
                    fired += 1
                    logging.debug(f"Delayed trigger fired: {desc}")
            except Exception as e:
                logging.error(f"Error firing delayed trigger '{desc}': {e}")
        return fired

    def _handle_beginning_of_phase_triggers(self):
        """Handles 'at the beginning of <phase>' triggers and related actions."""
        gs = self # Alias
        # CR 603.7: delayed triggers waiting for the beginning of this phase.
        self.process_delayed_triggers(self.phase)
        active_player = gs._get_active_player()
        non_active_player = gs._get_non_active_player()
        if not active_player or not non_active_player: # Safety check if players are None
            logging.error("Cannot handle phase triggers: player object is None.")
            return

        trigger_context_ap = {"controller": active_player}
        trigger_context_nap = {"controller": non_active_player}

        # --- Get the correct event type string ---
        phase_event_map = {
            self.PHASE_UPKEEP: "BEGINNING_OF_UPKEEP",
            self.PHASE_DRAW: "BEGINNING_OF_DRAW", # Usually no triggers, but possible
            self.PHASE_MAIN_PRECOMBAT: "BEGINNING_OF_PRECOMBAT_MAIN", # Specific event
            self.PHASE_BEGIN_COMBAT: "BEGINNING_OF_COMBAT",
            self.PHASE_END_STEP: "BEGINNING_OF_END_STEP"
            # Add other phases if they have standard beginning triggers
        }
        event_type = phase_event_map.get(self.phase)

        if event_type:
            # Saga Counters (Beginning of Precombat Main - Rule 714.2b) - Should happen AFTER upkeep triggers resolve
            if self.phase == self.PHASE_MAIN_PRECOMBAT and hasattr(gs, 'advance_saga_counters'):
                 self.advance_saga_counters(active_player)

            # Trigger abilities via AbilityHandler - Pass None as source_id for general phase triggers
            # *** CORRECTED CALL: Use self.ability_handler.check_abilities ***
            if self.ability_handler:
                # check_abilities scans every registered ability and stamps
                # each with its own controller, so one dispatch reaches both
                # players; a second call with the other player's context only
                # queued exact duplicates of every matching trigger.
                self.ability_handler.check_abilities(None, event_type, trigger_context_ap)
                # Process any queued triggers immediately AFTER checking
                self.ability_handler.process_triggered_abilities()
            else:
                 logging.warning("Cannot trigger beginning of phase abilities: AbilityHandler missing.")

            # Check SBAs after triggers have been processed
            self.check_state_based_actions()
        else:
             # Handle phases without standard beginning triggers if necessary
             # e.g., DECLARE_ATTACKERS might have its own triggers checked elsewhere
             pass

    def _cleanup_step_actions(self, player, discard_to_max=True):
        """Handle cleanup actions; return True while waiting for a choice."""
        if not player:
            return False

        # 1. Discard down to maximum hand size
        max_hand = self.max_hand_size # Can be modified by effects
        if discard_to_max and len(player.get("hand", [])) > max_hand:
            num_to_discard = len(player["hand"]) - max_hand
            logging.info(f"{player['name']} discarding {num_to_discard} card(s) due to hand size.")
            if self.start_discard_choice(
                    [player], count=num_to_discard, source_id=None,
                    cause="cleanup_discard"):
                return bool(self.choice_context)

        # 2. Remove damage marked on permanents
        player["damage_counters"] = {}
        player["deathtouch_damage"] = {}
        logging.debug(f"Removed damage from {player['name']}'s creatures.")

        # 2b. Impulse-draw permission expires (July 2026 sweep): cards exiled
        # with "you may play this turn" lose that permission at end of turn.
        _impulse = getattr(self, 'impulse_until_eot', None)
        if _impulse:
            _castable = getattr(self, 'cards_castable_from_exile', set())
            for _cid in list(_impulse):
                _castable.discard(_cid)
            self.impulse_until_eot = set()

        # "Until the end of your next turn" graveyard Adventure permissions
        # remain usable through that turn's cleanup, then expire.
        self.graveyard_adventure_permissions = [
            entry for entry in getattr(
                self, "graveyard_adventure_permissions", [])
            if entry.get("expires_turn", self.turn) > self.turn
        ]

        # 3. "Until end of turn" and "this turn" effects end
        # --- LayerSystem handles duration removal ---
        if hasattr(self, 'layer_system'):
            # BUGFIX: was remove_temporary_effects(self.turn), which does not exist
            # on LayerSystem -> AttributeError on every cleanup step.
            removed_count = self.layer_system.cleanup_expired_effects() or 0
            if removed_count > 0: logging.debug(f"Cleanup: Removed {removed_count} temporary layer effects.")
        if getattr(self, "replacement_effects", None):
            removed_replacements = (
                self.replacement_effects.cleanup_expired_effects() or 0)
            if removed_replacements > 0:
                logging.debug(
                    f"Cleanup: Removed {removed_replacements} replacement effects.")
        # Clear other temporary game state trackers (need specific cleanup logic)
        if hasattr(self, 'haste_until_eot'): self.haste_until_eot.clear()
        for _player in (self.p1, self.p2):
            _player.pop('saddled_permanents', None)
        self.delayed_event_triggers = [
            entry for entry in getattr(self, "delayed_event_triggers", [])
            if entry.get("expires_turn", self.turn) > self.turn
        ]
        return False

    def _get_next_turn_player(self):
        """Determines who the active player will be on the next turn."""
        # Simple 2-player toggle based on current turn and agent assignment
        current_turn_player_is_p1 = (self.turn % 2 == 1) == self.agent_is_p1
        return self.p2 if current_turn_player_is_p1 else self.p1

    def _combat_has_first_strike(self):
        """Checks if any attacking or blocking creature has first strike or double strike."""
        for creature_id in self.current_attackers:
             if self.check_keyword(creature_id, "first strike") or self.check_keyword(creature_id, "double strike"):
                 return True
        for blockers in self.current_block_assignments.values():
             for blocker_id in blockers:
                  if self.check_keyword(blocker_id, "first strike") or self.check_keyword(blocker_id, "double strike"):
                      return True
        return False

    def _get_active_player(self):
            """Returns the active player (whose turn it is) with strict error checking."""
            # Determine active player based on turn number and agent assignment
            # Turn 1, 3, 5... = P1's turn. Turn 2, 4, 6... = P2's turn.
            active_is_p1 = (self.turn % 2 != 0) 
            
            if active_is_p1:
                if self.p1 is None:
                    logging.critical("CRITICAL: p1 is None in _get_active_player. Defaulting to p2 if available.")
                    return self.p2
                return self.p1
            else:
                if self.p2 is None:
                    logging.critical("CRITICAL: p2 is None in _get_active_player. Defaulting to p1 if available.")
                    return self.p1
                return self.p2

    def _get_non_active_player(self):
        """Returns the non-active player (NAP)."""
        active_is_p1 = (self.turn % 2 != 0)
        if active_is_p1:
            return self.p2 if self.p2 else self.p1
        else:
            return self.p1 if self.p1 else self.p2

    def _check_phase_progress(self):
        """Ensure phase progression is happening correctly, forcing termination if needed."""
        # Add current phase to history (keeping only recent history)
        self._phase_history.append(self.phase)
        if len(self._phase_history) > 30:
            self._phase_history.pop(0)
        
        # Check for being stuck in the same phase
        if len(self._phase_history) >= 20 and all(p == self._phase_history[0] for p in self._phase_history):
            logging.warning(f"Detected potential phase stagnation in phase {self._phase_history[0]}")
            # Force advance to next turn as an escape mechanism
            if self.phase in [self.PHASE_PRIORITY, self.PHASE_END_STEP, self.PHASE_CLEANUP]:
                self._empty_mana_pools()
                self.phase = self.PHASE_UNTAP
                self.turn += 1
                self._phase_history = []  # Reset history after forced progress
                self.progress_was_forced = True
                logging.warning(f"Force-advancing to turn {self.turn} to break potential stall")
                return True
        
        return False

    def check_day_night_transition(self, spells_cast=None):
        """Apply the turn-start day/night action from the prior turn's count."""
        if self.day_night_checked_this_turn:
            return

        if spells_cast is None:
            active_player = self._get_active_player()
            spells_cast = sum(
                1 for spell in self.spells_cast_this_turn
                if isinstance(spell, tuple) and len(spell) >= 2
                and spell[1] == active_player)

        old_state = self.day_night_state

        if self.day_night_state == "day" and spells_cast == 0:
            self.day_night_state = "night"
            logging.debug("It becomes night (the previous active player cast no spells)")
        elif self.day_night_state == "night" and spells_cast >= 2:
            self.day_night_state = "day"
            logging.debug(
                f"It becomes day (the previous active player cast {spells_cast} spells)")

        if self.day_night_state != old_state:
            self.transform_day_night_cards()

        self.day_night_checked_this_turn = True

    @staticmethod
    def _day_night_face_indexes(card):
        faces = getattr(card, "faces", None) or []
        if len(faces) < 2:
            return None
        day_index = night_index = None
        for index, face in enumerate(faces):
            text = str(face.get("oracle_text", "")).lower()
            if "daybound" in text:
                day_index = index
            if "nightbound" in text:
                night_index = index
        if day_index is None or night_index is None:
            return None
        return day_index, night_index

    def prepare_day_night_entry(self, card_id):
        """Set a daybound/nightbound permanent's entry face without transforming it."""
        card = self._safe_get_card(card_id)
        indexes = self._day_night_face_indexes(card) if card else None
        if not indexes:
            return False

        day_index, night_index = indexes
        if self.day_night_state is None:
            current_index = getattr(card, "current_face", day_index)
            self.day_night_state = "night" if current_index == night_index else "day"
            self.transform_day_night_cards(exclude_ids={card_id})

        desired_index = day_index if self.day_night_state == "day" else night_index
        if getattr(card, "current_face", None) != desired_index:
            card.set_current_face(desired_index)
        return True

    def transform_day_night_cards(self, exclude_ids=None):
        """Synchronize every daybound/nightbound permanent to the designation."""
        if self.day_night_state not in ("day", "night"):
            return []

        excluded = set(exclude_ids or [])
        transformed_cards = []
        for player in [self.p1, self.p2]:
            for card_id in player["battlefield"]:
                if card_id in excluded:
                    continue
                card = self._safe_get_card(card_id)
                indexes = self._day_night_face_indexes(card) if card else None
                if not indexes:
                    continue
                day_index, night_index = indexes
                desired_index = day_index if self.day_night_state == "day" else night_index
                old_index = getattr(card, "current_face", None)
                if old_index != desired_index and self.transform_card(card_id):
                    transformed_cards.append(card_id)
                    context = {
                        "card": card,
                        "controller": player,
                        "from_state": "day" if old_index == day_index else "night",
                        "to_state": self.day_night_state,
                    }
                    self.trigger_ability(card_id, "DAY_NIGHT_CHANGED", context)
                    logging.debug(f"{card.name} transformed due to day/night change")

        return transformed_cards

    def check_priority(self, player=None):
        """
        Check if player has priority and can take actions.
        In Magic: The Gathering, priority determines which player can take game actions.
        """
        # If player is None, check active player
        if player is None:
            player = self._get_active_player()
        
        # In these phases, no player gets priority
        if self.phase in [self.PHASE_UNTAP, self.PHASE_CLEANUP]:
            return False
            
        # In general, active player gets priority first in each step
        active_player = self._get_active_player()
        
        # If stack is not empty, the player who last added to the stack passes priority
        if self.stack and hasattr(self, 'last_stack_actor'):
            return player != self.last_stack_actor
            
        # Otherwise active player has priority by default
        return player == active_player

    def advance_saga_counters(self, player):
        """
        Advance saga counters at the beginning of the main phase.
        This implements the rules for Saga enchantments from Dominaria.
        
        Args:
            player: The player whose Sagas to advance
            
        Returns:
            list: List of Sagas that were advanced
        """
        # CR 714 (July 2026 sweep): the single source of truth for lore
        # counters is player['saga_counters'] -- the store setup seeds and the
        # SBA (game_state_damage) reads. This method previously wrote to a
        # SEPARATE gs.saga_counters dict, so advancing a saga never moved the
        # counter the SBA checks: sagas advanced on paper but never sacrificed.
        saga_store = player.setdefault("saga_counters", {})
        
        # Find all Sagas in the battlefield
        sagas = []
        for card_id in player["battlefield"]:
            card = self._safe_get_card(card_id)
            if (card and hasattr(card, 'card_types') and 'enchantment' in card.card_types
                and hasattr(card, 'subtypes') and 'saga' in [s.lower() for s in card.subtypes]):
                sagas.append(card_id)
        
        advanced_sagas = []
        
        # Process each Saga
        for saga_id in sagas:
            # Get current chapter
            current_chapter = saga_store.get(saga_id, 0)
            
            # Advance to next chapter
            new_chapter = current_chapter + 1
            saga_store[saga_id] = new_chapter
            advanced_sagas.append(saga_id)
            
            # Trigger chapter ability
            saga_card = self._safe_get_card(saga_id)
            context = {
                "card": saga_card,
                "controller": player,
                "chapter": new_chapter
            }
            
            self.trigger_ability(saga_id, "SAGA_CHAPTER", context)
            logging.debug(f"Saga {saga_card.name} advanced to chapter {new_chapter}")
            
            # Check if saga is completed (usually after chapter 3)
            chapter_count = 0
            if hasattr(saga_card, 'oracle_text'):
                # Count chapter abilities (look for "I", "II", "III", etc.)
                chapter_pattern = re.compile(r"(^|\n)([IVX]+) —", re.MULTILINE)
                chapter_matches = chapter_pattern.findall(saga_card.oracle_text)
                chapter_count = len(chapter_matches)
            
            # Default to 3 chapters if we couldn't determine count
            if chapter_count == 0:
                chapter_count = 3
            
            # If we're past the last chapter, sacrifice the saga
            if new_chapter > chapter_count:
                self.move_card(saga_id, player, "battlefield", player, "graveyard")
                self.trigger_ability(saga_id, "SAGA_SACRIFICED", {"chapter": new_chapter})
                logging.debug(f"Saga {saga_card.name} completed and sacrificed")
        
        return advanced_sagas

