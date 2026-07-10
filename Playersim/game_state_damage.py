"""Damage application, life totals, and state-based actions.

Extracted from game_state.py. This module defines behavior only (a mixin);
all state lives on GameState itself, which composes every mixin.
"""

import logging
import re
from collections import defaultdict


class GameStateDamageMixin:
    """Damage application, life totals, and state-based actions."""

    # Empty slots: preserves GameState's __slots__ semantics (no instance __dict__).
    __slots__ = ()

    def check_state_based_actions(self):
        """Re-entrancy-guarded entry point for state-based actions.

        BUGFIX: SBA application (e.g. 704.5q annihilation) removes counters,
        whose COUNTER_REMOVED triggers re-entered this method mid-application
        and annihilated again on half-updated state, wiping every counter.
        Nested calls now return immediately; the outer loop already iterates
        until the state is stable, so nothing is missed.
        """
        if getattr(self, '_sba_in_progress', False):
            return False
        self._sba_in_progress = True
        try:
            return self._check_state_based_actions_impl()
        finally:
            self._sba_in_progress = False

    def _check_state_based_actions_impl(self):
        # Legacy asap delayed triggers (damage redirection, deferred lifelink
        # gains) fire once the triggering event has fully resolved -- the
        # state-based check is that boundary.
        if getattr(self, "delayed_triggers", None) and hasattr(self, "process_delayed_triggers"):
            self.process_delayed_triggers()
        """
        Comprehensive state-based actions check following MTG rules 704.
        Repeats check until no SBAs are performed in an iteration.
        Returns True if any SBA was performed, False otherwise.
        """
        initial_actions_performed = False
        iteration_count = 0
        max_iterations = 10 # Safety limit

        while iteration_count < max_iterations:
            iteration_count += 1
            current_actions_performed = False
            if iteration_count > 1: # Only log repeats
                logging.debug(f"--- SBA Check Iteration {iteration_count} ---")

            # --- Layer Application ---
            # Ensure characteristics are up-to-date before checking SBAs
            if self.layer_system:
                self.layer_system.apply_all_effects()

            # --- Collect Potential Actions ---
            # Store as (priority, action_type, target_id, player, details)
            # Priority helps group similar actions (e.g., handle all player losses first)
            actions_to_take = []

            # --- 1. Check Player States ---
            players_to_check = [p for p in [self.p1, self.p2] if p] # Filter out None players
            for player in players_to_check:
                player_id = 'p1' if player == self.p1 else 'p2'
                player_name = player.get('name', player_id)

                # 704.5a: Player Loses (Life <= 0)
                if player.get("life", 0) <= 0 and not player.get("lost_game", False) and not player.get("won_game", False): # Check win flag too
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "life <= 0"}))

                # 704.5b: Player Loses (Draw Empty)
                elif player.get("attempted_draw_from_empty", False) and not player.get("lost_game", False) and not player.get("won_game", False):
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "draw_empty"}))

                # 704.5c: Player Loses (Poison >= 10)
                elif player.get("poison_counters", 0) >= 10 and not player.get("lost_game", False) and not player.get("won_game", False):
                    actions_to_take.append((1, "LOSE_GAME", player_id, player, {"reason": "poison >= 10"}))

            # Check Turn Limit Draw/Loss
            if self.turn > self.max_turns and not getattr(self, '_turn_limit_checked', False):
                if self.p1 and self.p2:
                    if self.p1.get("life",0) == self.p2.get("life",0) and not self.p1.get("won_game") and not self.p2.get("won_game") and not self.p1.get("lost_game") and not self.p2.get("lost_game"):
                        actions_to_take.append((1, "DRAW_GAME", "both", None, {"reason": "turn_limit_equal_life"}))
                    # Loss handled by 704.5a after life comparison, no direct SBA needed here
                    self._turn_limit_checked = True  # Set flag to avoid repeated checks

            # --- 2. Check Permanent States ---
            # Get all permanents on battlefield for efficient checking
            all_permanents = []
            for player in players_to_check:
                all_permanents.extend([(card_id, player) for card_id in list(player.get("battlefield", []))]) # Iterate copy

            # Keep track of multiple legendaries/planeswalkers
            legendary_groups = defaultdict(list)
            world_permanents = []
            role_groups = defaultdict(list)

            for card_id, player in all_permanents:
                card = self._safe_get_card(card_id)
                if not card: continue

                # --- Get current characteristics post-layers ---
                # Safely get characteristics using Layer System if available, else fallback to card object
                def get_char(cid, char_name, default):
                    if self.layer_system: return self.layer_system.get_characteristic(cid, char_name) or default
                    else: return getattr(self._safe_get_card(cid), char_name, default)

                current_types = get_char(card_id, 'card_types', [])
                current_subtypes = get_char(card_id, 'subtypes', [])
                current_supertypes = get_char(card_id, 'supertypes', [])
                current_toughness = get_char(card_id, 'toughness', 0)
                # Get PW loyalty correctly (can be modified)
                current_loyalty = player.get("loyalty_counters", {}).get(card_id, 0)
                # Also check base loyalty for entry into the tracking dict
                if 'planeswalker' in current_types and card_id not in player.get("loyalty_counters",{}):
                    # If PW just entered, its loyalty should be initialized
                    base_loyalty = getattr(card, 'loyalty', 0) # Get base from card object
                    player.setdefault("loyalty_counters", {})[card_id] = base_loyalty
                    current_loyalty = base_loyalty

                damage = player.get("damage_counters", {}).get(card_id, 0)
                deathtouch_flag = player.get("deathtouch_damage", {}).get(card_id, False)
                # Keywords obtained from layers should be on the card object
                is_indestructible = self.check_keyword(card_id, "indestructible") if hasattr(self,'check_keyword') else ('indestructible' in getattr(card,'oracle_text','').lower())

                # 704.5f: Creature with toughness <= 0 dies
                if 'creature' in current_types and current_toughness <= 0:
                    # Indestructible doesn't save from toughness <= 0
                    actions_to_take.append((2, "MOVE_TO_GY", card_id, player, {"reason": "toughness <= 0"}))

                # 704.5i: Planeswalker with 0 loyalty dies
                elif 'planeswalker' in current_types and current_loyalty <= 0:
                    actions_to_take.append((2, "MOVE_TO_GY", card_id, player, {"reason": "loyalty <= 0"}))

                # 704.5g/h: Creature with lethal damage or deathtouch damage is destroyed
                elif 'creature' in current_types and current_toughness > 0:
                    # Check if damage is >= toughness OR any deathtouch damage marked
                    is_lethal = (damage >= current_toughness) or deathtouch_flag
                    if is_lethal:
                        if not is_indestructible:
                            # Flag for potential destruction, replacements handled during application
                            actions_to_take.append((3, "CHECK_DESTROY", card_id, player, {"reason": "lethal_damage/deathtouch"}))
                        else:
                            # If indestructible but has lethal damage, remove the damage (Rule 704.5g implicitly requires this if destroy is skipped)
                            if damage > 0 and card_id in player.get("damage_counters",{}):
                                logging.debug(f"Removing lethal damage from indestructible creature {card.name}")
                                player["damage_counters"][card_id] = 0
                                # Clear deathtouch flag too if it triggered this
                                if card_id in player.get("deathtouch_damage",{}):
                                    del player["deathtouch_damage"][card_id]
                                # Need to mark action performed to trigger potential loop check/layer update
                                current_actions_performed = True

                # 704.5j: If an Aura is attached to an illegal object or player, or is not attached to an object or player, send to GY
                if 'aura' in current_subtypes:
                    attached_to = player.get("attachments", {}).get(card_id)
                    # Check if not attached OR if the target is illegal (incl. protection)
                    if attached_to is None or not self._is_legal_attachment(card_id, attached_to):
                        actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "aura_illegal_attachment"}))

                # 704.5k: If an Equipment or Fortification is attached to an illegal permanent or player, it becomes unattached
                elif 'equipment' in current_subtypes or 'fortification' in current_subtypes:
                    attached_to = player.get("attachments", {}).get(card_id)
                    # Check if attached AND if the target is illegal
                    if attached_to and not self._is_legal_attachment(card_id, attached_to):
                        actions_to_take.append((4, "UNEQUIP", card_id, player, {"reason": "equip_illegal_attachment"}))

                # 704.5l: Legend Rule
                if 'legendary' in current_supertypes:
                    name = getattr(card, 'name', None)
                    if name: legendary_groups[name].append((card_id, player))

                # 704.5m: World Rule
                if 'world' in current_supertypes:
                    world_permanents.append((card_id, player))

                # A permanent may have only the newest Role controlled by a
                # given player. Roles controlled by different players coexist.
                if 'role' in current_subtypes:
                    role_target = player.get("attachments", {}).get(card_id)
                    if role_target is not None:
                        role_groups[(id(player), role_target)].append((card_id, player))

                # 704.5p/q: +1/+1 vs -1/-1 Annihilation
                if hasattr(card, 'counters') and card.counters.get('+1/+1', 0) > 0 and card.counters.get('-1/-1', 0) > 0:
                    actions_to_take.append((5, "ANNIHILATE_COUNTERS", card_id, player, {}))

                # 704.5s: Battle with no defense counters is put into its owner's graveyard
                if 'battle' in getattr(card, 'type_line', '').lower() and getattr(self, 'battle_cards', {}).get(card_id, 0) <= 0:
                    actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "battle_no_defense"}))

                # 704.5u: Saga with final chapter completed
                if 'saga' in current_subtypes and player.get("saga_counters", {}).get(card_id, 0) > 0:
                    chapter_count = 0
                    if hasattr(card, 'oracle_text'):
                        chapter_pattern = re.compile(r"(^|\n)([IVX]+) —", re.MULTILINE)
                        chapter_matches = chapter_pattern.findall(card.oracle_text)
                        chapter_count = len(chapter_matches)

                    if chapter_count > 0 and player.get("saga_counters", {}).get(card_id, 0) > chapter_count:
                        actions_to_take.append((4, "MOVE_TO_GY", card_id, player, {"reason": "saga_completed"}))

                # 704.5v: Permanent with phased-out status phased in since player's most recent turn began
                if card_id in getattr(self, 'phased_out', set()) and hasattr(player, 'phased_out_since_turn'):
                    if player.get('phased_out_since_turn', {}).get(card_id, 0) < self.turn:
                        actions_to_take.append((4, "PHASE_IN", card_id, player, {}))

                # 704.5w: Day/night state check if the permanent has a day/night transformation
                # This would typically be handled by a separate day/night mechanic function

            # --- Consolidate Role Rule Checks ---
            for roles in role_groups.values():
                if len(roles) > 1:
                    for old_role_id, role_controller in roles[:-1]:
                        actions_to_take.append((
                            4, "MOVE_TO_GY", old_role_id, role_controller,
                            {"reason": "role_rule"}))

            # --- Consolidate Legend Rule Checks ---
            # 704.5j: Legend Rule
            for name, permanents in legendary_groups.items():
                if len(permanents) > 1:
                    # Group by controller first (legends with same name are processed per-player)
                    # BUGFIX: was keyed by the player dict itself (unhashable ->
                    # TypeError whenever the legend rule actually applied).
                    by_controller = defaultdict(list)
                    controller_by_key = {}
                    for card_id, player in permanents:
                        key = id(player)
                        controller_by_key[key] = player
                        by_controller[key].append(card_id)

                    for key, legends in by_controller.items():
                        player = controller_by_key[key]
                        if len(legends) > 1:
                            # Owner chooses which one to keep - keep only the newest one (implementation choice)
                            to_keep = legends[-1]
                            for legend_id in legends[:-1]:
                                actions_to_take.append((4, "MOVE_TO_GY", legend_id, player, {"reason": "legend_rule"}))

            # --- Consolidate World Rule Check ---
            # 704.5m: World Rule
            if len(world_permanents) > 1:
                # Determine newest (using card_id as proxy timestamp)
                world_permanents.sort(key=lambda x: getattr(self._safe_get_card(x[0]),'_timestamp',x[0]))
                newest_id, newest_controller = world_permanents[-1]
                for world_id, world_player in world_permanents[:-1]:
                    actions_to_take.append((4, "MOVE_TO_GY", world_id, world_player, {"reason": "world_rule"}))

            # --- 3. Check for * in Power/Toughness without defining ability ---
            # 704.5r: If creature has * in power/toughness and no ability defines it, set to 0
            for card_id, player in all_permanents:
                card = self._safe_get_card(card_id)
                if card and 'creature' in getattr(card, 'card_types', []):
                    # Check if power or toughness contains * and needs defining ability
                    power_str = str(getattr(card, 'power', '0'))
                    toughness_str = str(getattr(card, 'toughness', '0'))

                    if ('*' in power_str or '*' in toughness_str) and not hasattr(card, '_characteristic_defining_abilities'):
                        # Set undefined * power/toughness to 0
                        if '*' in power_str: card.power = 0
                        if '*' in toughness_str: card.toughness = 0
                        current_actions_performed = True
                        logging.debug(f"SBA: Set undefined */* values to 0 for {card.name}")

            # --- 4. Token existence checks and copy existence checks ---
            # Check for tokens in non-battlefield zones (handled separately for clarity)
            tokens_ceased = self._check_and_remove_invalid_tokens()
            if tokens_ceased:
                current_actions_performed = True

            # 704.5e: If a copy of a spell is in a zone other than the stack, it ceases to exist
            # 704.5d: If a token is in a zone other than the battlefield, it ceases to exist
            # These are best handled in the _check_and_remove_invalid_tokens method

            # --- 5. Apply Actions Simultaneously (Grouped by Type/Priority) ---
            # Process actions in priority order
            actions_to_take.sort(key=lambda x: x[0]) # Sort by priority
            processed_in_iteration = set()  # Track processed actions

            for priority, action_type, target, player_ref, details in actions_to_take:
                action_key = (action_type, target) # Unique key for this SBA application
                if action_key in processed_in_iteration: continue

                # BUGFIX: card ids are ints throughout the engine; the old isinstance(target, str)
                # check resolved target_id to None for EVERY card-level SBA, so destruction,
                # the legend rule, 0-toughness deaths, and counter annihilation never applied.
                target_id = target if isinstance(target, (int, str)) else None
                target_card = self._safe_get_card(target_id) if target_id else None
                target_name = getattr(target_card, 'name', target_id) if target_card else str(target)

                logging.debug(f"SBA Checking: {action_type} on {target_name} for {player_ref['name'] if player_ref else 'Game'}")

                performed_this_action = False
                if action_type == "LOSE_GAME":
                    if not player_ref.get("lost_game", False):
                        player_ref["lost_game"] = True
                        logging.info(f"SBA Applied: {player_ref['name']} loses ({details['reason']})")
                        performed_this_action = True
                        current_actions_performed = True

                elif action_type == "DRAW_GAME":
                    if not (self.p1 and self.p1.get("game_draw",False)) and not (self.p2 and self.p2.get("game_draw",False)):
                        if self.p1: self.p1["game_draw"] = True
                        if self.p2: self.p2["game_draw"] = True
                        logging.info(f"SBA Applied: Game draw ({details['reason']})")
                        performed_this_action = True
                        current_actions_performed = True

                elif action_type == "CHECK_DESTROY": # Lethal damage check
                    # Check replacements before moving to graveyard
                    destruction_replaced = False
                    replacement_details = None

                    # 1. Regeneration
                    if hasattr(self, 'apply_regeneration') and self.apply_regeneration(target_id, player_ref):
                        logging.info(f"SBA: {target_name} regenerated instead of being destroyed.")
                        destruction_replaced = True
                        replacement_details = "regenerated"
                        performed_this_action = True

                    # 2. Totem Armor
                    elif not destruction_replaced and hasattr(self, 'apply_totem_armor') and self.apply_totem_armor(target_id, player_ref):
                        logging.info(f"SBA: Totem Armor saved {target_name} from destruction.")
                        destruction_replaced = True
                        replacement_details = "totem_armor"
                        performed_this_action = True

                    # 3. Other "If X would be destroyed" replacements
                    elif not destruction_replaced and self.replacement_effects:
                        destroy_context = {'card_id': target_id, 'player': player_ref, 'cause': 'sba_damage', 'from_zone': 'battlefield'}
                        modified_context, replaced = self.replacement_effects.apply_replacements("DESTROYED", destroy_context)
                        if replaced:
                            destruction_replaced = True
                            replacement_details = modified_context.get('description', 'replaced')
                            logging.info(f"SBA: Destruction of {target_name} replaced ({replacement_details}).")
                            # Handle modified destination (e.g., exile)
                            final_dest = modified_context.get('to_zone')
                            if final_dest and final_dest != "battlefield":
                                if self.move_card(target_id, player_ref, "battlefield", player_ref, final_dest, cause="sba_replaced_destroy"):
                                    performed_this_action = True
                            elif modified_context.get('prevented'):
                                performed_this_action = True  # Action was "prevented" but still processed

                    # 4. If not replaced/prevented, perform move to GY
                    if not destruction_replaced:
                        if self.move_card(target_id, player_ref, "battlefield", player_ref, "graveyard", cause="sba_damage", context=details):
                            logging.info(f"SBA Applied: Moved {target_name} to graveyard (Lethal Damage)")
                            performed_this_action = True

                elif action_type == "MOVE_TO_GY": # Toughness, Loyalty, Aura, World Rule etc.
                    if self.move_card(target_id, player_ref, "battlefield", player_ref, "graveyard", cause="sba", context=details):
                        logging.info(f"SBA Applied: Moved {target_name} to graveyard ({details['reason']})")
                        performed_this_action = True

                elif action_type == "UNEQUIP":
                    if hasattr(self, 'unequip_permanent') and self.unequip_permanent(player_ref, target_id):
                        logging.info(f"SBA Applied: Unequipped {target_name} ({details['reason']})")
                        performed_this_action = True

                elif action_type == "PHASE_IN":
                    if hasattr(self, 'phase_in_permanent') and self.phase_in_permanent(target_id, player_ref):
                        logging.info(f"SBA Applied: Phased in {target_name}")
                        performed_this_action = True
                    else:
                        # Simple fallback if phase_in_permanent doesn't exist
                        if hasattr(self, 'phased_out') and target_id in self.phased_out:
                            self.phased_out.remove(target_id)
                            if target_id not in player_ref.get("battlefield", []):
                                player_ref["battlefield"].append(target_id)
                            logging.info(f"SBA Applied: Phased in {target_name} (Fallback method)")
                            performed_this_action = True

                elif action_type == "ANNIHILATE_COUNTERS":
                    if target_card and hasattr(target_card, 'counters'):
                        plus_count = target_card.counters.get('+1/+1', 0)
                        minus_count = target_card.counters.get('-1/-1', 0)
                        remove_amount = min(plus_count, minus_count)
                        if remove_amount > 0:
                            # Use add_counter for consistency and triggers
                            if hasattr(self, 'add_counter'):
                                self.add_counter(target_id, '+1/+1', -remove_amount)
                                self.add_counter(target_id, '-1/-1', -remove_amount)
                            else:
                                # Fallback direct modification
                                target_card.counters['+1/+1'] -= remove_amount
                                if target_card.counters['+1/+1'] <= 0:
                                    del target_card.counters['+1/+1']
                                target_card.counters['-1/-1'] -= remove_amount
                                if target_card.counters['-1/-1'] <= 0:
                                    del target_card.counters['-1/-1']

                            logging.info(f"SBA Applied: Annihilated {remove_amount} +/- counters on {target_name}")
                            performed_this_action = True

                # Mark as processed and update state
                processed_in_iteration.add(action_key)
                current_actions_performed = current_actions_performed or performed_this_action

            # --- End of Inner Action Loop ---

            # --- Update overall flag and break if stable ---
            initial_actions_performed = initial_actions_performed or current_actions_performed
            if not current_actions_performed:
                if iteration_count > 1: # Log stability only if it took more than one pass
                    logging.debug(f"--- SBA Check Stable after {iteration_count} iterations ---")
                break # Exit the while loop if no actions were performed this iteration

            # If game ended during this iteration, stop checking SBAs
            if any(p.get("lost_game") or p.get("won_game") or p.get("game_draw") for p in players_to_check if p):
                logging.debug("--- SBA Check: Game ended, stopping SBA loop ---")
                break

        if iteration_count >= max_iterations:
            logging.error("State-based actions check exceeded max iterations. Potential infinite loop.")

        # --- Final Layer Re-application ---
        if initial_actions_performed and self.layer_system:
            logging.debug("Re-applying layers after SBAs.")
            self.layer_system.apply_all_effects()

        return initial_actions_performed

    def damage_planeswalker(self, planeswalker_id, amount, source_id,
                            defer_sba=False):
        """Deal damage to a planeswalker (removes loyalty counters). Returns actual damage dealt."""
        pw_card = self._safe_get_card(planeswalker_id)
        owner = self.get_card_controller(planeswalker_id)
        if not pw_card or not owner or 'planeswalker' not in getattr(pw_card, 'card_types', []):
            return 0 # Indicate no damage applied

        # Apply damage replacement effects targeting this planeswalker
        damage_context = { "source_id": source_id, "target_id": planeswalker_id, "target_obj": pw_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": False } # Assume non-combat unless context passed
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                 logging.debug(f"Damage to PW {pw_card.name} prevented or reduced to 0.")
                 return 0 # No damage applied
            # TODO: Handle redirection if target changes

        if actual_damage > 0:
            # Use a dedicated method to remove loyalty counters
            counters_removed = self._remove_loyalty_counters(planeswalker_id, owner, actual_damage)

            if counters_removed > 0:
                source_name = getattr(self._safe_get_card(source_id),'name',source_id)
                current_loyalty = owner.get("loyalty_counters", {}).get(planeswalker_id, 0)
                logging.debug(f"{source_name} dealt {counters_removed} damage to {pw_card.name}. Loyalty now {current_loyalty}")
                self.trigger_ability(planeswalker_id, "DAMAGED", {"amount": counters_removed, "source_id": source_id})
                if not defer_sba:
                    self.check_state_based_actions() # PW might leave
                return counters_removed # Return damage actually applied as counter removal
        return 0 # No damage applied or counters removed

    def _remove_loyalty_counters(self, planeswalker_id, owner, amount):
        """Removes loyalty counters from a planeswalker. Returns amount removed."""
        if amount <= 0: return 0
        pw_card = self._safe_get_card(planeswalker_id)
        current_loyalty = owner.get("loyalty_counters", {}).get(planeswalker_id, getattr(pw_card, 'loyalty', 0) if pw_card else 0)
        amount_to_remove = min(amount, current_loyalty) # Cannot remove more than current loyalty
        new_loyalty = current_loyalty - amount_to_remove
        owner.setdefault("loyalty_counters", {})[planeswalker_id] = new_loyalty
        return amount_to_remove

    def damage_battle(self, battle_id, amount, source_id):
        """Deal damage to a battle (removes defense counters). Returns actual damage dealt."""
        battle_card = self._safe_get_card(battle_id)
        owner = self.get_card_controller(battle_id)
        if not battle_card or not owner or 'battle' not in getattr(battle_card, 'type_line', ''):
            return 0 # Indicate no damage applied

        # Apply damage replacement effects targeting this battle
        damage_context = { "source_id": source_id, "target_id": battle_id, "target_obj": battle_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": False } # Assume non-combat unless context passed
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
             # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                 logging.debug(f"Damage to Battle {battle_card.name} prevented or reduced to 0.")
                 return 0 # No damage applied
            # TODO: Handle redirection

        if actual_damage > 0:
            # Use add_defense_counter with negative amount
            success = self.add_defense_counter(battle_id, -actual_damage)
            if success:
                source_name = getattr(self._safe_get_card(source_id),'name',source_id)
                current_defense = getattr(self,'battle_cards',{}).get(battle_id,0) # Read current defense
                logging.debug(f"{source_name} dealt {actual_damage} damage to {battle_card.name}. Defense now {current_defense}")
                self.trigger_ability(battle_id, "DAMAGED", {"amount": actual_damage, "source_id": source_id})
                # SBA check for battle defeat handled within add_defense_counter or separate SBA check
                self.check_state_based_actions()
                return actual_damage # Return damage successfully applied
        return 0 # No damage applied

    def damage_player(self, player, amount, source_id, is_combat_damage=False):
        """Deals damage to a player, applying replacements. Returns actual damage dealt."""
        if not player or amount <= 0: return 0

        player_id = "p1" if player == self.p1 else "p2"
        player_name = player.get('name', player_id)

        damage_context = { "source_id": source_id, "target_id": player_id, "target_obj": player, "target_is_player": True, "damage_amount": amount, "is_combat_damage": is_combat_damage }
        actual_damage = amount

        # Apply replacements
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            if actual_damage <= 0 or modified_context.get("prevented"):
                logging.debug(f"Damage to player {player_name} prevented or reduced to 0.")
                return 0

        # Apply damage (life loss)
        if actual_damage > 0:
            player['life'] -= actual_damage
            logging.debug(f"Player {player_name} took {actual_damage} damage. Life now {player['life']}.")
            # Track damage this turn
            self.damage_dealt_this_turn[player_id] = self.damage_dealt_this_turn.get(player_id, 0) + actual_damage
            player['lost_life_this_turn'] = True
            # Trigger "damaged" or "lost life" events
            self.trigger_ability(None, "PLAYER_DAMAGED", {"player": player, "amount": actual_damage, "source_id": source_id})
            self.trigger_ability(None, "LOSE_LIFE", {"player": player, "amount": actual_damage, "source_id": source_id})
            self.check_state_based_actions() # Player might lose
            return actual_damage
        return 0

    def handle_lifelink_gain(self, source_id, player_gaining_life, damage_dealt):
        """Handles life gain specifically from lifelink, applying replacements."""
        if damage_dealt <= 0 or not player_gaining_life: return

        gain_context = {'player': player_gaining_life, 'life_amount': damage_dealt, 'source_id': source_id, 'source_type': 'lifelink'}
        final_life_gain = damage_dealt

        # Apply LIFE_GAIN replacement effects
        if hasattr(self, 'replacement_effects'):
            modified_gain_context, gain_replaced = self.replacement_effects.apply_replacements("LIFE_GAIN", gain_context)
            final_life_gain = modified_gain_context.get('life_amount', 0)
            if final_life_gain <= 0 or modified_gain_context.get('prevented'):
                 logging.debug(f"Lifelink gain from {source_id} prevented or reduced to 0.")
                 return

        if final_life_gain > 0:
             player_gaining_life['life'] += final_life_gain
             source_name = getattr(self._safe_get_card(source_id), 'name', source_id)
             logging.debug(f"Lifelink: {player_gaining_life['name']} gained {final_life_gain} life from {source_name}.")
             # Trigger GAIN_LIFE event
             self.trigger_ability(source_id, "GAIN_LIFE", {"player": player_gaining_life, "amount": final_life_gain, "source_id": source_id})

    def gain_life(self, player, amount, source_id=None):
        """Canonical life-gain entry (CR 119.3).

        Applies LIFE_GAIN replacement effects, increases the player's life, and
        fires the GAIN_LIFE trigger so that 'whenever you gain life' abilities
        work for ALL life gain. Previously only lifelink fired GAIN_LIFE (via
        handle_lifelink_gain); spell/ability life gain fell back to incrementing
        player['life'] directly and silently skipped the trigger, so life-gain-
        matters cards never saw non-lifelink life gain. GainLifeEffect already
        called gs.gain_life() behind a hasattr guard, so defining it here routes
        that path through the trigger.

        Returns the amount of life actually gained.
        """
        if not player or amount is None or amount <= 0:
            return 0
        gain_context = {'player': player, 'life_amount': amount, 'source_id': source_id}
        final_gain = amount
        if hasattr(self, 'replacement_effects') and self.replacement_effects:
            try:
                modified, _ = self.replacement_effects.apply_replacements("LIFE_GAIN", gain_context)
                final_gain = modified.get('life_amount', 0)
                if final_gain <= 0 or modified.get('prevented'):
                    logging.debug(f"gain_life: gain prevented/reduced to 0 for {player.get('name', '?')}.")
                    return 0
            except Exception as e:
                logging.warning(f"gain_life: replacement error: {e}")
                final_gain = amount
        if final_gain <= 0:
            return 0
        player['life'] = player.get('life', 0) + final_gain
        player['gained_life_this_turn'] = True
        self.trigger_ability(source_id, "GAIN_LIFE",
                             {"player": player, "amount": final_gain, "source_id": source_id})
        logging.debug(f"gain_life: {player.get('name', '?')} gained {final_gain} life (now {player['life']}).")
        return final_gain

    def apply_damage_to_permanent(self, target_id, amount, source_id, is_combat_damage=False, has_deathtouch=False):
        """Marks damage on a creature, considering deathtouch. Returns actual damage marked."""
        target_card = self._safe_get_card(target_id)
        target_owner = self.get_card_controller(target_id)
        if not target_card or not target_owner or 'creature' not in getattr(target_card, 'card_types', []):
            return 0 # Indicate no damage applied
        source_card = self._safe_get_card(source_id)
        source_controller = self.get_card_controller(source_id)
        if source_card and source_controller and hasattr(self, 'targeting_system') and self.targeting_system:
            if self.targeting_system._has_protection_from(
                    target_card, source_card, target_owner, source_controller):
                logging.debug(f"Damage from {getattr(source_card, 'name', source_id)} to "
                              f"{getattr(target_card, 'name', target_id)} prevented by protection.")
                return 0

        # Apply damage replacement effects targeting this creature
        damage_context = { "source_id": source_id, "target_id": target_id, "target_obj": target_card, "target_is_player": False, "damage_amount": amount, "is_combat_damage": is_combat_damage }
        actual_damage = amount
        if hasattr(self, 'replacement_effects'):
            modified_context, was_replaced = self.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
            # Check if prevented entirely
            if actual_damage <= 0 or modified_context.get("prevented"):
                logging.debug(f"Damage to {target_card.name} prevented or reduced to 0.")
                return 0 # No damage applied
            # Update deathtouch status based on replacement? Less common, assume it sticks for now.
            # TODO: Handle redirection if target changes (complex)

        if actual_damage > 0:
             target_owner.setdefault("damage_counters", {})[target_id] = target_owner.get("damage_counters", {}).get(target_id, 0) + actual_damage
             if has_deathtouch:
                  target_owner.setdefault("deathtouch_damage", {})[target_id] = True
             source_name = getattr(self._safe_get_card(source_id),'name',source_id)
             logging.debug(f"{source_name} marked {actual_damage} damage on {target_card.name}{' (Deathtouch)' if has_deathtouch else ''}.")
             # Trigger DAMAGED event immediately after marking
             self.trigger_ability(target_id, "DAMAGED", {"amount": actual_damage, "source_id": source_id, "is_combat": is_combat_damage})
             # SBA check will happen later in the game loop
             return actual_damage # Return damage actually marked
        return 0 # No damage applied

    def prevent_damage(self, target, amount):
        """Register damage prevention. (Uses Replacement System)"""
        if not self.replacement_effects:
             logging.warning("Cannot prevent damage: ReplacementEffectSystem missing.")
             return False
        target_key = target['name'] if isinstance(target, dict) else target # Player dict or permanent ID
        source_name = "Generic Prevention" # Need source context usually
        logging.debug(f"Registering {amount} damage prevention for {target_key}.")

        def condition(ctx):
            # Basic check: Target matches, damage > 0
            return ctx.get('target_id') == target_key and ctx.get('damage_amount', 0) > 0

        def replacement(ctx):
            original_damage = ctx.get('damage_amount', 0)
            prevented = min(original_damage, amount)
            ctx['damage_amount'] = max(0, original_damage - prevented)
            logging.debug(f"Prevention: Prevented {prevented} damage to {target_key}. Remaining: {ctx['damage_amount']}")
            # TODO: Track remaining prevention shield if limited use
            return ctx

        # Needs a source ID and duration, use placeholders
        self.replacement_effects.register_effect({
             'source_id': 'PREVENTION_EFFECT', 'event_type': 'DAMAGE',
             'condition': condition, 'replacement': replacement,
             'duration': 'end_of_turn', 'controller_id': None, # Affects target, not controller based
             'description': f"Prevent {amount} damage to {target_key}"
        })
        return True

    def redirect_damage(self, source_filter, original_target, new_target):
        """Register damage redirection. (Uses Replacement System)"""
        if not self.replacement_effects:
             logging.warning("Cannot redirect damage: ReplacementEffectSystem missing.")
             return False
        original_target_key = original_target['name'] if isinstance(original_target, dict) else original_target
        new_target_key = new_target['name'] if isinstance(new_target, dict) else new_target
        new_target_is_player = isinstance(new_target, dict)
        new_target_obj = new_target if new_target_is_player else self._safe_get_card(new_target_key)
        new_target_owner = new_target if new_target_is_player else self.get_card_controller(new_target_key)

        logging.debug(f"Registering damage redirection from {original_target_key} to {new_target_key}.")

        def condition(ctx):
            # Check source matches filter (basic: allow any for now)
            # Check original target matches
            return ctx.get('target_id') == original_target_key and ctx.get('damage_amount', 0) > 0

        def replacement(ctx):
            original_damage = ctx.get('damage_amount', 0)
            logging.debug(f"Redirecting {original_damage} damage from {original_target_key} to {new_target_key}.")
            ctx['damage_amount'] = 0 # Prevent original damage
            ctx['redirected'] = True
            # --- Schedule separate damage event to new target ---
            # Avoid applying damage directly inside replacement to prevent loops
            def deal_redirected_damage():
                 if new_target_is_player:
                     if hasattr(new_target_obj, 'life'): new_target_obj['life'] -= original_damage
                 else:
                     self.apply_damage_to_permanent(new_target_key, original_damage, ctx.get('source_id', 'redirect_source'))

            if not hasattr(self, 'delayed_triggers'): self.delayed_triggers = []
            self.delayed_triggers.append(deal_redirected_damage)
            return ctx

        # Needs source ID and duration
        self.replacement_effects.register_effect({
             'source_id': 'REDIRECT_EFFECT', 'event_type': 'DAMAGE',
             'condition': condition, 'replacement': replacement,
             'duration': 'end_of_turn', 'controller_id': None, # Belongs to game state rule?
             'description': f"Redirect damage from {original_target_key} to {new_target_key}"
        })
        return True

