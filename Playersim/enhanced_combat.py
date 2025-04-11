import logging
from collections import defaultdict
from .combat import EnhancedCombatResolver

class ExtendedCombatResolver(EnhancedCombatResolver):
    """
    Extended version of the EnhancedCombatResolver that adds support for:
    - Planeswalker damage
    - Battle damage
    - First strike ordering
    - Ninjutsu
    - Multiple blocker assignment
    
    This class inherits from EnhancedCombatResolver and extends it with new capabilities.
    """
    
    def __init__(self, game_state):
        super().__init__(game_state)
        self.planeswalker_damage = defaultdict(int)
        self.battle_damage = defaultdict(int)
        
    def resolve_combat(self):
        """
        Implements a complete combat resolution sequence following MTG rules:
        Marks damage based on steps, relies on GameState for SBAs and lifelink application.
        Returns damage dealt to opponent player and potential lifegain.
        """
        try:
            gs = self.game_state

            # Reset tracking for this resolution step
            self.combat_log = []
            # self.creatures_killed = 0 # Tracked by GameState SBAs
            self.potential_lifegain = defaultdict(int) # Track potential gain for players
            self.planeswalker_damage = defaultdict(int) # Store potential PW damage
            self.battle_damage = defaultdict(int)     # Store potential Battle damage
            # self.damage_prevention.clear() # Should be handled by Replacement Effects system
            self.combat_triggers = []

            if gs.combat_damage_dealt:
                logging.debug("Combat damage already applied this turn, skipping.")
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            if not gs.current_attackers:
                logging.debug("No attackers declared; skipping combat resolution.")
                gs.combat_damage_dealt = True # Mark as dealt even if no attackers
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
            defender_player = gs.p2 if gs.agent_is_p1 else gs.p1

            self._log_combat_state()
            self._process_combat_abilities()

            # Check if first strike damage step is needed (Consolidated Check)
            needs_first_strike_step = False
            combatants = gs.current_attackers[:]
            for blockers in gs.current_block_assignments.values(): combatants.extend(blockers)
            for cid in combatants:
                card = gs._safe_get_card(cid)
                if card and (self._has_keyword(card, "first strike") or self._has_keyword(card, "double strike")):
                    needs_first_strike_step = True; break

            # Initialize damage MARKING structures (NOT applying damage yet)
            # These are temporary for calculation within this step
            damage_marked_on_creatures = defaultdict(int)
            damage_marked_on_players = {"p1": 0, "p2": 0}
            damage_marked_on_planeswalkers = defaultdict(int)
            damage_marked_on_battles = defaultdict(int)
            creatures_dealt_damage_fs = set() # Track who dealt damage for triggers/lifelink
            creatures_dealt_damage_regular = set()
            # killed_in_first_strike = set() # Tracked by GameState SBAs now

            # --- STEP 1: First Strike Damage Calculation (if needed) ---
            if needs_first_strike_step:
                 logging.debug("COMBAT EXT: Calculating First Strike Damage")
                 for attacker_id in gs.current_attackers:
                     self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, creatures_dealt_damage_fs, is_first_strike=True)
                 for attacker_id, blockers in gs.current_block_assignments.items():
                     for blocker_id in blockers:
                          self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_marked_on_creatures, creatures_dealt_damage_fs, is_first_strike=True)

                 # --- Apply First Strike Damage Marks ---
                 self._apply_marked_damage(damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles)
                 self._process_combat_triggers(creatures_dealt_damage_fs, is_first_strike=True)
                 # GameState checks SBAs AFTER this damage step resolves in main loop
                 # gs.check_state_based_actions()

                 # Clear markings for regular step
                 damage_marked_on_creatures.clear()
                 damage_marked_on_players = {"p1": 0, "p2": 0}
                 damage_marked_on_planeswalkers.clear()
                 damage_marked_on_battles.clear()


            # --- STEP 2: Regular Damage Calculation ---
            logging.debug("COMBAT EXT: Calculating Regular Damage")
            for attacker_id in gs.current_attackers:
                 # Need to know if creature survived FS SBAs - check its location
                 _, zone = gs.find_card_location(attacker_id)
                 if zone != 'battlefield': continue
                 self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, creatures_dealt_damage_regular, is_first_strike=False)

            for attacker_id, blockers in gs.current_block_assignments.items():
                 _, attacker_zone = gs.find_card_location(attacker_id)
                 if attacker_zone != 'battlefield': continue
                 for blocker_id in blockers:
                      _, blocker_zone = gs.find_card_location(blocker_id)
                      if blocker_zone != 'battlefield': continue
                      self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_marked_on_creatures, creatures_dealt_damage_regular, is_first_strike=False)

            # --- Apply Regular Damage Marks ---
            self._apply_marked_damage(damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles)
            self._process_combat_triggers(creatures_dealt_damage_regular, is_first_strike=False)
            # GameState checks SBAs AFTER this damage step resolves in main loop
            # gs.check_state_based_actions()

            gs.combat_damage_dealt = True # Mark damage as dealt for this turn's combat

            # Get total opponent damage from final application step
            defender_key = "p2" if defender_player == gs.p2 else "p1"
            total_damage_to_opponent = self.final_damage_applied.get(defender_key, 0)

            logging.debug(f"COMBAT EXT SUMMARY: Total calculated damage to opponent player: {total_damage_to_opponent}")

            # Return potential lifegain calculated during damage steps
            return {
                "damage_to_opponent": total_damage_to_opponent,
                 "potential_lifegain": dict(self.potential_lifegain) # Convert defaultdict
            }

        except Exception as e:
            logging.error(f"Error in extended combat resolution: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return {"damage_to_opponent": 0, "potential_lifegain": {}} # Return default on error
        
    def _apply_marked_damage(self, marked_creatures, marked_players, marked_pws, marked_battles):
        """Applies the calculated damage marks using GameState methods."""
        gs = self.game_state
        # Use a temporary attribute to track total applied damage for lifelink this step
        if not hasattr(self, '_damage_applied_this_step'): self._damage_applied_this_step = defaultdict(int)
        else: self._damage_applied_this_step.clear()

        # Apply to Creatures
        for target_id, damage_info in marked_creatures.items():
            amount = damage_info.get("amount", 0)
            # If multiple sources dealt damage, source_id attribution gets tricky.
            # Use the list of sources for triggers? Or just mark as 'combat_damage'?
            # Let's pass the list of source IDs.
            source_ids = damage_info.get("sources", ['combat_damage'])
            source_id_for_lifelink = source_ids[0] if source_ids else 'combat_damage' # Simplification for lifelink source
            is_combat = True # Assume combat damage
            has_deathtouch = damage_info.get("deathtouch", False)
            # GameState method handles marking damage/counters/etc.
            damage_marked = gs.apply_damage_to_permanent(target_id, amount, source_id_for_lifelink, is_combat, has_deathtouch)
            if damage_marked > 0:
                 for source_id in source_ids:
                      self._damage_applied_this_step[source_id] += damage_marked # Track total damage dealt by each source

        # Apply to Players
        for player_key, damage in marked_players.items():
            if damage > 0:
                player_obj = gs.p1 if player_key == "p1" else gs.p2
                # Use generic combat source for player damage attribution?
                source_id = 'combat_damage' # Simplification
                damage_applied = gs.damage_player(player_obj, damage, source_id, is_combat_damage=True) # GS handles life loss
                if damage_applied > 0:
                     # Need to attribute player damage back to the attackers that dealt it.
                     # This requires tracking which attackers assigned damage to player. Complex.
                     # Simplification: Attribute all player damage to the first unblocked attacker for lifelink purposes? Very weak.
                     # Better: Store source_ids with player damage marks.
                     # Assume _process_attacker_damage stored this info. (Not currently done).
                     # For now, lifelink for player damage needs explicit source tracking.
                     # Let's skip lifelink gain from direct player damage in this simplified model.
                     # self._damage_applied_this_step['player_damage_source'] += damage_applied # Can't easily link source
                     pass # Lifelink from player damage needs better source attribution


        # Apply to Planeswalkers
        for target_id, damage in marked_pws.items():
             source_id = 'combat_damage' # Placeholder
             for atk_id, pw_id in getattr(gs, 'planeswalker_attack_targets', {}).items():
                  if pw_id == target_id: source_id = atk_id; break # Get actual attacker source
             damage_applied = gs.damage_planeswalker(target_id, damage, source_id) # GS handles loyalty removal
             if damage_applied > 0:
                 self._damage_applied_this_step[source_id] += damage_applied

        # Apply to Battles
        for target_id, damage in marked_battles.items():
             source_id = 'combat_damage' # Placeholder
             for atk_id, btl_id in getattr(gs, 'battle_attack_targets', {}).items():
                  if btl_id == target_id: source_id = atk_id; break # Get actual attacker source
             damage_applied = gs.damage_battle(target_id, damage, source_id) # GS handles defense removal
             if damage_applied > 0:
                 self._damage_applied_this_step[source_id] += damage_applied

        # --- Handle Lifelink Based on Applied Damage ---
        for source_id, total_damage_dealt in self._damage_applied_this_step.items():
            if total_damage_dealt <= 0: continue
            source_card = gs._safe_get_card(source_id)
            if source_card and self._has_keyword(source_card, "lifelink"):
                lifelink_controller = gs.get_card_controller(source_id)
                if lifelink_controller:
                     # Use GameState's centralized lifelink handler
                     gs.handle_lifelink_gain(source_id, lifelink_controller, total_damage_dealt)

        self._damage_applied_this_step.clear() # Clear for next phase
        
    def _process_blocker_damage(self, blocker_id, attacker_id, attacker_player, defender_player,
                                damage_marked_on_creatures, creatures_dealt_damage_step, is_first_strike):
        """Calculates damage assignment from a blocker, MARKS it, returns total potential damage."""
        gs = self.game_state
        blocker_card = gs._safe_get_card(blocker_id)

        # Use helper to check if this creature should deal damage now
        if not self._should_deal_damage_this_phase(blocker_card, is_first_strike):
            return 0

        attacker_card = gs._safe_get_card(attacker_id)
        # Check if blocker/attacker still valid (on battlefield)
        _, blocker_zone = gs.find_card_location(blocker_id)
        _, attacker_zone = gs.find_card_location(attacker_id)
        if not blocker_card or not attacker_card or blocker_zone != 'battlefield' or attacker_zone != 'battlefield':
            return 0

        damage = self._get_card_power(blocker_card, defender_player)
        if damage <= 0: return 0

        # CONSOLIDATION: Use central keyword check
        has_deathtouch = self._has_keyword(blocker_card, "deathtouch")
        has_lifelink = self._has_keyword(blocker_card, "lifelink")

        total_potential_damage = 0

        # Apply replacement effects? Should be handled during apply_marked_damage using GameState method.
        # For calculation simplicity, assume damage goes through for now.
        # Mark damage on the attacker
        damage_info = damage_marked_on_creatures.get(attacker_id, {"amount": 0, "sources": [], "deathtouch": False})
        damage_info["amount"] += damage
        damage_info["sources"].append(blocker_id)
        damage_info["deathtouch"] = damage_info["deathtouch"] or has_deathtouch
        damage_marked_on_creatures[attacker_id] = damage_info
        total_potential_damage = damage

        logging.debug(f"COMBAT EXT Mark: Blocker {blocker_card.name} will deal {damage} to attacker {attacker_card.name}")
        creatures_dealt_damage_step.add(blocker_id)
        # Add triggers here
        self._add_combat_trigger(blocker_id, "deals_combat_damage_to_creature", {"damage_amount": damage, "target_id": attacker_id}, is_first_strike)
        self._add_combat_trigger(attacker_id, "is_dealt_combat_damage", {"damage_amount": damage, "source_id": blocker_id}, is_first_strike)

        # If any damage potentially dealt, handle lifelink
        if total_potential_damage > 0 and has_lifelink:
             player_key = "p2" if defender_player == gs.p2 else "p1"
             self.potential_lifegain[player_key] += total_potential_damage
             logging.debug(f"COMBAT EXT Potential Lifelink: {blocker_card.name} gains {total_potential_damage} life")

        return total_potential_damage
        
    def protect_planeswalker(self, attacked_planeswalker_id, defender_id):
        """Set up protection for a planeswalker"""
        gs = self.game_state
        
        if not hasattr(gs, "planeswalker_protectors"):
            gs.planeswalker_protectors = {}
        
        gs.planeswalker_protectors[attacked_planeswalker_id] = defender_id
        
        defender_card = gs._safe_get_card(defender_id)
        planeswalker_card = gs._safe_get_card(attacked_planeswalker_id)
        
        if defender_card and planeswalker_card:
            logging.debug(f"COMBAT: {defender_card.name} is now protecting {planeswalker_card.name}")
            return True
        return False
                
    def _process_attacker_damage(self, attacker_id, attacker_player, defender_player,
                                damage_marked_on_creatures, damage_marked_on_players,
                                damage_marked_on_planeswalkers, damage_marked_on_battles,
                                creatures_dealt_damage_step, is_first_strike):
        """Calculates damage assignment from an attacker, MARKS it, returns total potential damage."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)

        # Use helper to check if this creature should deal damage now
        if not self._should_deal_damage_this_phase(attacker_card, is_first_strike):
            return 0

        if not attacker_card: return 0 # Should already be checked by caller?

        damage = self._get_card_power(attacker_card, attacker_player)
        if damage <= 0: return 0

        # CONSOLIDATION: Use central keyword check
        has_trample = self._has_keyword(attacker_card, "trample")
        has_deathtouch = self._has_keyword(attacker_card, "deathtouch")
        has_lifelink = self._has_keyword(attacker_card, "lifelink")

        total_potential_damage = 0 # Track potential damage for lifelink

        # Handle Planeswalker/Battle targeting first
        # NOTE: Damage is marked directly here, not applied until _apply_marked_damage
        pw_target_id = getattr(gs, 'planeswalker_attack_targets', {}).get(attacker_id)
        battle_target_id = getattr(gs, 'battle_attack_targets', {}).get(attacker_id)
        # Check if blocked
        blockers = gs.current_block_assignments.get(attacker_id, [])
        # Check if blockers are still valid (on battlefield)
        valid_blockers = [bid for bid in blockers if gs.find_card_location(bid) == (defender_player, 'battlefield')]

        if pw_target_id and not valid_blockers:
            damage_marked_on_planeswalkers[pw_target_id] = damage_marked_on_planeswalkers.get(pw_target_id, 0) + damage
            total_potential_damage = damage
            logging.debug(f"COMBAT EXT Mark: {attacker_card.name} will deal {damage} to PW {gs._safe_get_card(pw_target_id).name}")
            # Add trigger for PW damage here
            self._add_combat_trigger(attacker_id, "deals_combat_damage_to_planeswalker", {"damage_amount": damage, "target_id": pw_target_id}, is_first_strike)
        elif battle_target_id and not valid_blockers:
            damage_marked_on_battles[battle_target_id] = damage_marked_on_battles.get(battle_target_id, 0) + damage
            total_potential_damage = damage
            logging.debug(f"COMBAT EXT Mark: {attacker_card.name} will deal {damage} to Battle {gs._safe_get_card(battle_target_id).name}")
            # Add trigger for Battle damage here
            self._add_combat_trigger(attacker_id, "deals_combat_damage_to_battle", {"damage_amount": damage, "target_id": battle_target_id}, is_first_strike)
        elif not valid_blockers: # Unblocked, target player
            defender_key = "p2" if defender_player == gs.p2 else "p1"
            damage_marked_on_players[defender_key] = damage_marked_on_players.get(defender_key, 0) + damage
            total_potential_damage = damage
            logging.debug(f"COMBAT EXT Mark: {attacker_card.name} will deal {damage} to player {defender_player['name']}")
            self._add_combat_trigger(attacker_id, "deals_combat_damage_to_player", {"damage_amount": damage}, is_first_strike)
        else: # Blocked
            # --- Damage Assignment Logic ---
            # Use GameState's ordering if available
            ordered_blockers = gs.first_strike_ordering.get(attacker_id, valid_blockers) if hasattr(gs, 'first_strike_ordering') else valid_blockers
            # Filter to only currently valid blockers
            ordered_blockers = [bid for bid in ordered_blockers if bid in valid_blockers]
            if not ordered_blockers: ordered_blockers = valid_blockers # Fallback if ordering invalid
            if not hasattr(ordered_blockers, 'sort'): # Ensure it's sortable (simple heuristic)
                # --- FIX: Check card existence before getting toughness ---
                ordered_blockers = sorted(ordered_blockers, key=lambda bid: self._get_card_toughness(gs._safe_get_card(bid), defender_player) if gs._safe_get_card(bid) else 0)


            remaining_damage = damage
            assigned_to_blockers = defaultdict(int)
            potential_damage_this_step = 0 # Track damage assigned in this blocker loop for lifelink

            for blocker_id in ordered_blockers:
                if remaining_damage <= 0: break
                blocker_card = gs._safe_get_card(blocker_id)
                if not blocker_card: continue

                blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                # Consider damage already marked THIS STEP by other attackers (if relevant?) - Complex, skip for now
                # Check damage already marked on card from PREVIOUS steps/sources
                existing_damage = defender_player.get("damage_counters", {}).get(blocker_id, 0)
                # Damage needed to be lethal *this step*
                lethal_needed = max(1, blocker_toughness - existing_damage) if has_deathtouch else max(0, blocker_toughness - existing_damage)


                assign_amount = 1 if has_deathtouch else lethal_needed
                # Cannot assign more than total remaining damage
                actual_assign = min(remaining_damage, assign_amount)

                assigned_to_blockers[blocker_id] += actual_assign
                remaining_damage -= actual_assign
                potential_damage_this_step += actual_assign

            # Apply assigned blocker damage to the marking dict
            for blocker_id, assigned_damage in assigned_to_blockers.items():
                 # Store source and deathtouch info with the damage mark
                 damage_info = damage_marked_on_creatures.get(blocker_id, {"amount": 0, "sources": [], "deathtouch": False})
                 damage_info["amount"] += assigned_damage
                 damage_info["sources"].append(attacker_id)
                 damage_info["deathtouch"] = damage_info["deathtouch"] or has_deathtouch
                 damage_marked_on_creatures[blocker_id] = damage_info
                 logging.debug(f"COMBAT EXT Mark: {attacker_card.name} will deal {assigned_damage} to blocker {gs._safe_get_card(blocker_id).name}")
                 # Add triggers here (don't wait for application)
                 self._add_combat_trigger(attacker_id, "deals_combat_damage_to_creature", {"damage_amount": assigned_damage, "target_id": blocker_id}, is_first_strike)
                 self._add_combat_trigger(blocker_id, "is_dealt_combat_damage", {"damage_amount": assigned_damage, "source_id": attacker_id}, is_first_strike)


            # Trample damage
            if has_trample and remaining_damage > 0:
                 defender_key = "p2" if defender_player == gs.p2 else "p1"
                 damage_marked_on_players[defender_key] = damage_marked_on_players.get(defender_key, 0) + remaining_damage
                 potential_damage_this_step += remaining_damage
                 logging.debug(f"COMBAT EXT Mark: {attacker_card.name} will deal {remaining_damage} trample damage to player {defender_player['name']}")
                 self._add_combat_trigger(attacker_id, "deals_combat_damage_to_player", {"damage_amount": remaining_damage, "is_trample": True}, is_first_strike)

            total_potential_damage = potential_damage_this_step # Update total potential damage

        # If any damage potentially dealt, mark creature and handle lifelink
        if total_potential_damage > 0:
            creatures_dealt_damage_step.add(attacker_id)
            if has_lifelink:
                 player_key = "p1" if attacker_player == gs.p1 else "p2"
                 # Ensure key exists before incrementing
                 self.potential_lifegain[player_key] = self.potential_lifegain.get(player_key, 0) + total_potential_damage
                 logging.debug(f"COMBAT EXT Potential Lifelink: {attacker_card.name} gains {total_potential_damage} life")


        return total_potential_damage
    
    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central AbilityHandler."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if not card_id: return False

        # --- DELEGATED CHECK ---
        # Always delegate to AbilityHandler or GameState's check_keyword method
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            if hasattr(gs.ability_handler, 'check_keyword'):
                 try:
                     # Use AbilityHandler's public method
                     return gs.ability_handler.check_keyword(card_id, keyword)
                 except Exception as e:
                      logging.error(f"Error checking keyword via AbilityHandler in CombatResolver: {e}")
                      # Fall through to GameState check on error
            # else: Fall through if check_keyword doesn't exist on handler
        if hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
             try:
                 return gs.check_keyword(card_id, keyword)
             except Exception as e:
                  logging.error(f"Error checking keyword via GameState in CombatResolver: {e}")
                  # Fall through on error
        # --- END DELEGATED CHECK ---

        # --- REMOVED Fallback ---
        # Basic check is unreliable here. Assume false if delegation fails.
        logging.warning(f"Keyword check failed in CombatResolver for {keyword} on {getattr(card, 'name', 'Unknown')}: Neither AbilityHandler nor GameState check method succeeded.")
        return False
    
    def _should_deal_damage_this_phase(self, card, is_first_strike_phase):
        """Check if a creature should deal damage in the current phase."""
        if not card: return False
        has_first_strike = self._has_keyword(card, "first strike")
        has_double_strike = self._has_keyword(card, "double strike")

        if is_first_strike_phase:
            # Only FS/DS deal damage in first strike phase
            return has_first_strike or has_double_strike
        else:
            # Creatures without FS deal damage, and DS creatures deal damage *again*
            return not has_first_strike or has_double_strike  
        
    def _apply_planeswalker_damage(self):
        """Apply damage to planeswalkers and check if any died with enhanced effect processing"""
        gs = self.game_state
        
        planeswalkers_to_remove = []
        
        for planeswalker_id, damage in self.planeswalker_damage.items():
            planeswalker_card = gs._safe_get_card(planeswalker_id)
            if not planeswalker_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if planeswalker_id in player["battlefield"]:
                    controller = player
                    break
            
            if not controller:
                continue
                
            # Check for protection abilities
            has_protection = False
            if hasattr(planeswalker_card, 'oracle_text'):
                protection_text = planeswalker_card.oracle_text.lower()
                if "prevent all damage" in protection_text or "protection from all colors" in protection_text:
                    has_protection = True
                    logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} has protection, damage prevented")
                    continue
            
            # Apply damage as loyalty loss
            if hasattr(planeswalker_card, "loyalty"):
                # Create damage context for possible replacement effects
                damage_context = {
                    "target_id": planeswalker_id,
                    "target_is_planeswalker": True,
                    "damage_amount": damage,
                    "is_combat_damage": True,
                    "controller": controller,
                    "damage_source_type": "combat"
                }
                
                # Apply damage replacement effects if available
                modified_context = damage_context
                if hasattr(gs, 'apply_replacement_effect'):
                    modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE_TO_PLANESWALKER", damage_context)
                    if was_replaced:
                        damage = modified_context.get("damage_amount", damage)
                        # Check if damage was redirected or prevented entirely
                        if damage <= 0 or modified_context.get("prevented", False):
                            logging.debug(f"COMBAT: Damage to planeswalker {planeswalker_card.name} was prevented or redirected")
                            continue
                
                # Apply the damage and track for counters systems consistency
                original_loyalty = planeswalker_card.loyalty
                planeswalker_card.loyalty -= damage
                
                # Ensure loyalty counters are tracked properly
                if hasattr(controller, "loyalty_counters"):
                    controller["loyalty_counters"][planeswalker_id] = planeswalker_card.loyalty
                
                logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} lost {damage} loyalty, now at {planeswalker_card.loyalty}")
                
                # Track damage for statistics
                if not hasattr(gs, "damage_this_turn"):
                    gs.damage_this_turn = {}
                gs.damage_this_turn[planeswalker_id] = gs.damage_this_turn.get(planeswalker_id, 0) + damage
                
                # Trigger damage events
                if hasattr(gs, 'trigger_ability'):
                    # Trigger on the planeswalker itself
                    gs.trigger_ability(planeswalker_id, "DEALT_DAMAGE", {
                        "damage_amount": damage, 
                        "previous_loyalty": original_loyalty,
                        "is_combat_damage": True
                    })
                    
                    # Also trigger for any permanents that watch for damage to planeswalkers
                    for permanent_id in controller["battlefield"]:
                        if permanent_id != planeswalker_id:  # Don't re-trigger on the planeswalker
                            gs.trigger_ability(permanent_id, "PLANESWALKER_DAMAGED", {
                                "planeswalker_id": planeswalker_id,
                                "damage_amount": damage,
                                "is_combat_damage": True
                            })
                    
                # Check for special ultimates that trigger when damaged
                if hasattr(planeswalker_card, 'damage_triggers_ultimate') and planeswalker_card.damage_triggers_ultimate:
                    if hasattr(planeswalker_card, 'ultimate_damage_threshold'):
                        threshold = planeswalker_card.ultimate_damage_threshold
                        damage_this_turn = gs.damage_this_turn.get(planeswalker_id, 0)
                        
                        if damage_this_turn >= threshold:
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} triggering ultimate from damage threshold")
                            
                            # Trigger the ultimate ability
                            if hasattr(planeswalker_card, 'ultimate_ability_index'):
                                ultimate_idx = planeswalker_card.ultimate_ability_index
                                
                                # Process the ultimate directly if ability handler available
                                if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'activate_planeswalker_ability'):
                                    gs.ability_handler.activate_planeswalker_ability(planeswalker_id, ultimate_idx, controller)
                                # Otherwise use the game state method
                                elif hasattr(gs, 'activate_planeswalker_ability'):
                                    gs.activate_planeswalker_ability(planeswalker_id, ultimate_idx, controller)
                    
                # Check if planeswalker died
                if planeswalker_card.loyalty <= 0:
                    # Check for "dies" replacement effects before moving to graveyard
                    if hasattr(gs, 'apply_replacement_effect'):
                        death_context = {
                            "card_id": planeswalker_id,
                            "card_type": "planeswalker",
                            "controller": controller,
                            "destination": "graveyard",
                            "from_damage": True,
                            "from_combat": True
                        }
                        modified_death, was_replaced = gs.apply_replacement_effect("DIES", death_context)
                        
                        # If not replaced, move to graveyard
                        if not was_replaced or modified_death.get("destination", "graveyard") == "graveyard":
                            gs.move_card(planeswalker_id, controller, "battlefield", controller, "graveyard")
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} died from loyalty loss")
                            planeswalkers_to_remove.append(planeswalker_id)
                            
                            # Trigger death event
                            if hasattr(gs, 'trigger_ability'):
                                # Trigger on the planeswalker
                                gs.trigger_ability(planeswalker_id, "DIES", {
                                    "from_damage": True,
                                    "from_combat": True
                                })
                                
                                # Also trigger for permanents watching for planeswalker deaths
                                for permanent_id in controller["battlefield"]:
                                    if permanent_id != planeswalker_id:
                                        gs.trigger_ability(permanent_id, "PLANESWALKER_DIED", {
                                            "planeswalker_id": planeswalker_id,
                                            "from_damage": True,
                                            "from_combat": True
                                        })
                        else:
                            # Handle custom replacement effect (e.g., exile instead of graveyard)
                            dest = modified_death.get("destination", "graveyard")
                            gs.move_card(planeswalker_id, controller, "battlefield", controller, dest)
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} moved to {dest} instead of dying")
                            planeswalkers_to_remove.append(planeswalker_id)
                    else:
                        # Default behavior without replacement effects
                        gs.move_card(planeswalker_id, controller, "battlefield", controller, "graveyard")
                        logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} died from loyalty loss")
                        planeswalkers_to_remove.append(planeswalker_id)
                        
                        # Trigger death event
                        if hasattr(gs, 'trigger_ability'):
                            gs.trigger_ability(planeswalker_id, "DIES", {
                                "from_damage": True,
                                "from_combat": True
                            })
                        
        # Remove dead planeswalkers from attack targets
        if hasattr(gs, "planeswalker_attack_targets"):
            for attacker_id, pw_id in list(gs.planeswalker_attack_targets.items()):
                if pw_id in planeswalkers_to_remove:
                    del gs.planeswalker_attack_targets[attacker_id]
                    
        # Remove dead planeswalkers from protectors
        if hasattr(gs, "planeswalker_protectors"):
            for pw_id in list(gs.planeswalker_protectors.keys()):
                if pw_id in planeswalkers_to_remove:
                    del gs.planeswalker_protectors[pw_id]
                        
    def _apply_battle_damage(self):
        """
        Apply damage to battles with enhanced effect processing.
        Handles both single-faced battles and double-faced battles (Sieges),
        with proper handling of defeat mechanics including casting by the defeating player.
        """
        gs = self.game_state
        
        battles_to_remove = []
        chapters_advanced = []  # Track battle cards that advanced chapters
        
        # Track which battles were defeated and by whom
        defeated_battles = {}  # Maps battle_id to the player who defeated it
        
        for battle_id, damage in self.battle_damage.items():
            battle_card = gs._safe_get_card(battle_id)
            if not battle_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if battle_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                continue
            
            # Determine the opposing player (who's dealing damage to this battle)
            opposing_player = gs.p2 if controller == gs.p1 else gs.p1
                
            # Check for damage prevention effects
            if hasattr(gs, 'battle_damage_prevention') and gs.battle_damage_prevention.get(battle_id, False):
                logging.debug(f"COMBAT: Damage to battle {battle_card.name} prevented")
                continue
                
            # Apply damage to battle
            if not hasattr(battle_card, "damage"):
                battle_card.damage = 0
                
            # Create damage context for possible replacement effects
            damage_context = {
                "target_id": battle_id,
                "target_is_battle": True,
                "damage_amount": damage,
                "is_combat_damage": True,
                "controller": controller
            }
            
            # Apply damage replacement effects if available
            modified_context = damage_context
            if hasattr(gs, 'apply_replacement_effect'):
                modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE_TO_BATTLE", damage_context)
                if was_replaced:
                    damage = modified_context.get("damage_amount", damage)
                    # Check if damage was redirected or prevented entirely
                    if damage <= 0 or modified_context.get("prevented", False):
                        logging.debug(f"COMBAT: Damage to battle {battle_card.name} was prevented or redirected")
                        continue
            
            # Apply the damage
            original_damage = battle_card.damage
            battle_card.damage += damage
            
            # Update defense counters (primary tracking mechanism for battles)
            if not hasattr(gs, 'battle_cards'):
                gs.battle_cards = {}
                
            # Initialize defense counters if not already set
            if battle_id not in gs.battle_cards:
                if hasattr(battle_card, 'defense'):
                    gs.battle_cards[battle_id] = battle_card.defense
                else:
                    gs.battle_cards[battle_id] = 0
            
            # Remove defense counters equal to damage dealt
            gs.battle_cards[battle_id] = max(0, gs.battle_cards[battle_id] - damage)
            current_defense = gs.battle_cards[battle_id]
            
            logging.debug(f"COMBAT: Battle {battle_card.name} took {damage} damage, now has {current_defense} defense counters")
            
            # Trigger damage events
            if hasattr(gs, 'trigger_ability'):
                gs.trigger_ability(battle_id, "DEALT_DAMAGE", {
                    "damage_amount": damage, 
                    "previous_damage": original_damage,
                    "is_combat_damage": True
                })
                
            # Check if battle should advance a chapter due to damage
            if hasattr(battle_card, "current_chapter") and hasattr(battle_card, "damage_triggers_advancement"):
                if battle_card.damage_triggers_advancement:
                    # Check damage thresholds for chapter advancement
                    if hasattr(battle_card, "chapter_thresholds") and battle_card.chapter_thresholds:
                        # Find the next threshold this damage exceeds
                        for threshold in sorted(battle_card.chapter_thresholds):
                            if original_damage < threshold <= battle_card.damage:
                                battle_card.current_chapter += 1
                                logging.debug(f"COMBAT: Battle {battle_card.name} advanced to chapter {battle_card.current_chapter} due to damage")
                                
                                # Add to chapters advanced tracking
                                chapters_advanced.append({
                                    "battle_id": battle_id,
                                    "controller": controller,
                                    "chapter": battle_card.current_chapter
                                })
                                
                                # Trigger chapter advancement
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(battle_id, "CHAPTER_ADVANCED", {"chapter": battle_card.current_chapter})
                                break
            
            # Check if battle is defeated (defense counters reduced to 0)
            battle_defeated = current_defense <= 0
                
            if battle_defeated:
                logging.debug(f"COMBAT: Battle {battle_card.name} defeated with 0 defense counters")
                
                # Record which player defeated this battle
                defeated_battles[battle_id] = opposing_player
                
                # Check for "defeated" replacement effects before moving to graveyard
                defeat_prevented = False
                if hasattr(gs, 'apply_replacement_effect'):
                    defeat_context = {
                        "card_id": battle_id,
                        "card_type": "battle",
                        "controller": controller,
                        "destination": "graveyard",
                        "defeat_reason": "damage",
                        "defeating_player": opposing_player
                    }
                    modified_defeat, was_replaced = gs.apply_replacement_effect("BATTLE_DEFEATED", defeat_context)
                    
                    if was_replaced and modified_defeat.get("prevented", False):
                        defeat_prevented = True
                        logging.debug(f"COMBAT: Battle {battle_card.name} defeat was prevented by replacement effect")
                
                if not defeat_prevented:
                    # Add to list of battles to remove
                    battles_to_remove.append(battle_id)
                    
                    # Trigger defeat event
                    if hasattr(gs, 'trigger_ability'):
                        gs.trigger_ability(battle_id, "BATTLE_DEFEATED", {
                            "from_damage": True,
                            "defeating_player": opposing_player
                        })
                        
        # Process defeated battles
        for battle_id in battles_to_remove:
            battle_card = gs._safe_get_card(battle_id)
            if not battle_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if battle_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                continue
                
            # Get the player who defeated the battle
            defeating_player = defeated_battles.get(battle_id, None)
            if not defeating_player:
                # Fallback - use opponent as defeating player
                defeating_player = gs.p2 if controller == gs.p1 else gs.p1
            
            # Determine if this is a double-faced battle (Siege)
            is_double_faced = False
            if hasattr(battle_card, 'is_tdfc') and battle_card.is_tdfc:
                is_double_faced = True
            elif hasattr(battle_card, 'faces') and len(getattr(battle_card, 'faces', [])) > 1:
                is_double_faced = True
            elif hasattr(battle_card, 'subtypes') and 'siege' in [s.lower() for s in battle_card.subtypes]:
                is_double_faced = True
            
            # Move the battle to graveyard
            gs.move_card(battle_id, controller, "battlefield", controller, "graveyard")
            logging.debug(f"COMBAT: Battle {battle_card.name} moved to graveyard after defeat")
            
            # For double-faced battles (Sieges), the defeating player gets to cast the back face
            if is_double_faced:
                # Get back face information
                back_face_id = None
                
                # Different ways to access back face depending on implementation
                if hasattr(battle_card, 'back_face_id'):
                    back_face_id = battle_card.back_face_id
                elif hasattr(battle_card, 'faces') and len(battle_card.faces) > 1:
                    # If using a faces array, get the second face
                    back_face_id = battle_card.faces[1].get('id', None)
                
                if back_face_id is not None:
                    # Cast the back face for the defeating player without mana cost
                    if hasattr(gs, 'cast_spell'):
                        # Prepare context with "no_cost" to indicate free casting
                        context = {
                            "no_cost": True,
                            "from_battle_defeat": True,
                            "original_battle_id": battle_id,
                            "skip_default_movement": True  # Prevent moving to graveyard after resolution
                        }
                        
                        # Get the back face card object
                        back_face = gs._safe_get_card(back_face_id)
                        if back_face:
                            logging.debug(f"COMBAT: Player {defeating_player['name']} casting back face {back_face.name} after defeating battle")
                            
                            # Move the back face to the defeating player's hand temporarily
                            if battle_id in controller["graveyard"]:
                                controller["graveyard"].remove(battle_id)
                            defeating_player["hand"].append(back_face_id)
                            
                            # Cast the spell
                            gs.cast_spell(back_face_id, defeating_player, context=context)
                        else:
                            logging.warning(f"COMBAT: Could not find back face card for battle {battle_card.name}")
                    else:
                        logging.warning(f"COMBAT: Cannot cast back face, cast_spell method not available")
                else:
                    logging.warning(f"COMBAT: Could not determine back face for battle {battle_card.name}")
            else:
                # For single-faced battles, just process any "on defeat" rewards
                if hasattr(battle_card, 'defeat_reward'):
                    reward = battle_card.defeat_reward
                    logging.debug(f"COMBAT: Processing defeat reward for single-faced battle {battle_card.name}")
                    
                    # Process different reward types
                    if reward.get('type') == 'draw_cards':
                        count = reward.get('amount', 1)
                        for _ in range(count):
                            if hasattr(defeating_player, "library") and defeating_player["library"]:
                                card_id = defeating_player["library"].pop(0)
                                defeating_player["hand"].append(card_id)
                        logging.debug(f"COMBAT: Player {defeating_player['name']} drew {count} cards as battle reward")
                    
                    elif reward.get('type') == 'create_token':
                        token_data = reward.get('token_data', {})
                        if hasattr(gs, 'create_token') and token_data:
                            gs.create_token(defeating_player, token_data)
                            logging.debug(f"COMBAT: Player {defeating_player['name']} created token as battle reward")
                    
                    elif reward.get('type') == 'gain_life':
                        amount = reward.get('amount', 1)
                        defeating_player["life"] += amount
                        logging.debug(f"COMBAT: Player {defeating_player['name']} gained {amount} life as battle reward")
        
        # Clean up any battle-related data for removed battles
        if battles_to_remove and hasattr(gs, 'battle_damage_prevention'):
            for battle_id in battles_to_remove:
                if battle_id in gs.battle_damage_prevention:
                    del gs.battle_damage_prevention[battle_id]
        
        # Process chapter abilities for advanced chapters
        for chapter_data in chapters_advanced:
            battle_id = chapter_data["battle_id"]
            chapter = chapter_data["chapter"]
            controller = chapter_data["controller"]
            
            # Process chapter abilities if available
            battle_card = gs._safe_get_card(battle_id)
            if battle_card and hasattr(battle_card, 'chapter_abilities'):
                if chapter in battle_card.chapter_abilities:
                    ability = battle_card.chapter_abilities[chapter]
                    
                    # Build ability context
                    ability_context = {
                        "chapter": chapter,
                        "from_damage": True,
                        "controller": controller
                    }
                    
                    # Process chapter ability
                    if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'process_chapter_ability'):
                        gs.ability_handler.process_chapter_ability(battle_id, chapter, ability, ability_context)
                    else:
                        logging.debug(f"COMBAT: Processing chapter {chapter} ability for {battle_card.name}")
                        gs.trigger_ability(battle_id, "CHAPTER_ABILITY", ability_context)
                
    def process_ninjutsu(self, ninjutsu_card_id, attacker_id):
        """Process the ninjutsu ability during combat"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for ninjutsu")
            return False
            
        return self.game_state.combat_action_handler.handle_ninjutsu(ninjutsu_card_id, attacker_id)
        
    def assign_first_strike_order(self):
        """Set the damage assignment order for first strike"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for first strike ordering")
            return False
            
        return self.game_state.combat_action_handler.handle_first_strike_order()
        
    def assign_multiple_blockers(self, attacker_index):
        """Assign multiple blockers to an attacker"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for multiple blocker assignment")
            return False
            
        return self.game_state.combat_action_handler.handle_assign_multiple_blockers(attacker_index)
