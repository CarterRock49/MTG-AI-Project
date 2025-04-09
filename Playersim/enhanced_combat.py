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
        """Override to add support for planeswalker and battle damage"""
        try:
            gs = self.game_state

            # Reset tracking structures specific to extended logic
            self.planeswalker_damage.clear()
            self.battle_damage.clear()

            # --- Core Combat Logic ---
            # This part now closely resembles the original resolve_combat,
            # but relies on the overridden _process_attacker/blocker_damage methods below.
            # We still need the logic for first strike determination and step processing here.

            self.combat_log = []
            self.creatures_killed = 0
            # Removed cards_drawn reference
            self.damage_prevention.clear()
            self.combat_triggers = []

            if gs.combat_damage_dealt:
                logging.debug("Combat damage already applied this turn, skipping.")
                return 0
            if not gs.current_attackers:
                logging.debug("No attackers declared; skipping combat resolution.")
                return 0

            attacker_player = gs.p1 if gs.agent_is_p1 else gs.p2
            defender_player = gs.p2 if gs.agent_is_p1 else gs.p1

            self._log_combat_state() # Use base logging
            self._process_combat_abilities() # Use base/extended processing

            # Check for first strike/double strike (Use CONSOLIDATED _has_keyword)
            needs_first_strike_step = False
            combatants = gs.current_attackers[:]
            for blockers in gs.current_block_assignments.values(): combatants.extend(blockers)
            for cid in combatants:
                card = gs._safe_get_card(cid)
                # CONSOLIDATION: Use central keyword check
                if card and (self._has_keyword(card, "first strike") or self._has_keyword(card, "double strike")):
                    needs_first_strike_step = True; break

            damage_to_creatures = defaultdict(int)
            damage_to_players = {"p1": 0, "p2": 0}
            creatures_dealt_damage = set()
            killed_in_first_strike = set()
            total_damage_to_opponent = 0

            # STEP 1: First Strike Damage (if needed)
            if needs_first_strike_step:
                 logging.debug("COMBAT EXT: First strike damage step")
                 # Process first strike attackers/blockers using OVERRIDDEN methods
                 for attacker_id in gs.current_attackers:
                     # No need to check FS/DS here, _process_attacker_damage handles it via is_first_strike=True
                     self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_to_creatures, damage_to_players, creatures_dealt_damage, killed_in_first_strike, is_first_strike=True)

                 for attacker_id, blockers in gs.current_block_assignments.items():
                     if attacker_id in killed_in_first_strike: continue
                     for blocker_id in blockers:
                          # No need to check FS/DS here, _process_blocker_damage handles it via is_first_strike=True
                          self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_to_creatures, creatures_dealt_damage, killed_in_first_strike, is_first_strike=True)

                 # Use GameState SBA check for consistency
                 gs.check_state_based_actions()
                 # Update killed_in_first_strike based on SBA results (this requires SBA to update a set or return results)
                 # Simplified: Assume SBA handles graveyard moves. We just need to know who died *during* this step.
                 # This interaction needs careful design. For now, assume SBA marks dead creatures.
                 killed_in_first_strike.update(gs.get_creatures_that_died_this_step()) # Hypothetical GS method

                 self._process_combat_triggers(creatures_dealt_damage, is_first_strike=True) # Process FS triggers

                 logging.debug(f"COMBAT EXT: First strike damage - {len(killed_in_first_strike)} creatures died")


            # STEP 2: Regular Damage
            logging.debug("COMBAT EXT: Regular damage step")
            creatures_dealt_damage.clear() # Reset for regular damage step triggers/lifelink

            # Process regular damage using OVERRIDDEN methods
            for attacker_id in gs.current_attackers:
                 if attacker_id in killed_in_first_strike: continue
                 # No need to check FS/DS here, _process_attacker_damage handles it via is_first_strike=False
                 self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_to_creatures, damage_to_players, creatures_dealt_damage, killed_in_first_strike, is_first_strike=False)

            for attacker_id, blockers in gs.current_block_assignments.items():
                 if attacker_id in killed_in_first_strike: continue
                 for blocker_id in blockers:
                      if blocker_id in killed_in_first_strike: continue
                      # No need to check FS/DS here, _process_blocker_damage handles it via is_first_strike=False
                      self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_to_creatures, creatures_dealt_damage, killed_in_first_strike, is_first_strike=False)


            # --- Damage Application for PW/Battles (Moved here) ---
            self._apply_planeswalker_damage() # Applies damage stored in self.planeswalker_damage
            self._apply_battle_damage()     # Applies damage stored in self.battle_damage

            # STEP 3: Process Combat Triggers for regular step
            self._process_combat_triggers(creatures_dealt_damage, is_first_strike=False) # Process regular triggers

            # STEP 4: State-Based Actions (Final check after all damage/effects)
            gs.check_state_based_actions() # Let GS handle final deaths

            defender_key = "p2" if defender_player == gs.p2 else "p1"
            total_damage_to_opponent = damage_to_players[defender_key]

            # Cleanup and mark combat as resolved
            # Cleanup might happen in the GameState step logic after combat phases
            # gs.current_attackers = []
            # gs.current_block_assignments = {}
            gs.combat_damage_dealt = True # Mark damage as dealt for this turn's combat

            logging.debug(f"COMBAT EXT SUMMARY: Total damage to opponent player: {total_damage_to_opponent}")
            # creatures_killed count is harder to track accurately here without SBA results

            return total_damage_to_opponent

        except Exception as e:
            logging.error(f"Error in extended combat resolution: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return 0
        
    def _process_blocker_damage(self, blocker_id, attacker_id, attacker_player, defender_player,
                            damage_to_creatures, creatures_dealt_damage, killed_creatures, is_first_strike):
        """Process damage from a blocking creature, calling super for base logic."""
        gs = self.game_state

        # Check if damage should be dealt this phase first
        if not self._should_deal_damage_this_phase(gs._safe_get_card(blocker_id), is_first_strike):
            return 0

        # Call base class method for standard damage logic
        # Assume base class handles damage assignment, lifelink checks, triggers.
        # Similar logic to _process_attacker_damage needs to be in the base class method.
        # For now, mimic core logic:

        blocker_card = gs._safe_get_card(blocker_id)
        attacker_card = gs._safe_get_card(attacker_id)
        if not blocker_card or not attacker_card or blocker_id in killed_creatures or attacker_id in killed_creatures: return 0

        damage = self._get_card_power(blocker_card, defender_player)
        if damage <= 0: return 0

        # CONSOLIDATION: Use central keyword check
        has_deathtouch = self._has_keyword(blocker_card, "deathtouch")

        # Apply replacement effects for damage to attacker
        damage_context = {"source_id": blocker_id, "target_id": attacker_id, "target_is_player": False, "damage_amount": damage, "is_combat_damage": True}
        if hasattr(gs, 'replacement_effects'):
            modified_context, _ = gs.replacement_effects.apply_replacements("DAMAGE", damage_context)
            actual_damage = modified_context.get("damage_amount", 0)
        else: actual_damage = damage

        total_damage_dealt = 0
        if actual_damage > 0:
             damage_to_creatures[attacker_id] += actual_damage
             logging.debug(f"COMBAT EXT: Blocker {blocker_card.name} assigns {actual_damage} damage to attacker {attacker_card.name}.")
             total_damage_dealt += actual_damage
             creatures_dealt_damage.add(blocker_id)
             self._add_combat_trigger(blocker_id, "deals_combat_damage_to_creature", {"damage_amount": actual_damage, "target_id": attacker_id}, is_first_strike)
             self._add_combat_trigger(attacker_id, "is_dealt_combat_damage", {"damage_amount": actual_damage, "source_id": blocker_id}, is_first_strike)


        # Deathtouch check
        if has_deathtouch and actual_damage > 0:
             # Mark attacker for lethal damage check later
             attacker_player.setdefault("deathtouch_damage", {})[attacker_id] = True
             logging.debug(f"COMBAT EXT: Deathtouch from blocker {blocker_card.name} marked attacker {attacker_card.name} for death.")


        # Lifelink check (defer gain)
        # CONSOLIDATION: Use central keyword check
        if self._has_keyword(blocker_card, "lifelink") and total_damage_dealt > 0:
             if not hasattr(self, 'potential_lifegain'): self.potential_lifegain = defaultdict(int)
             self.potential_lifegain[defender_player['name']] += total_damage_dealt

        return total_damage_dealt # Return total assigned damage
        
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
                                 damage_to_creatures, damage_to_players, creatures_dealt_damage,
                                 killed_creatures, is_first_strike):
        """Override to handle damage to planeswalkers AND BATTLES, calling super for base logic."""
        gs = self.game_state

        # Check if damage should be dealt this phase first (use base class helper)
        if not self._should_deal_damage_this_phase(gs._safe_get_card(attacker_id), is_first_strike):
            return 0

        # --- Handle Planeswalker Targeting ---
        if hasattr(gs, "planeswalker_attack_targets") and attacker_id in gs.planeswalker_attack_targets:
            planeswalker_id = gs.planeswalker_attack_targets[attacker_id]
            # ... (Planeswalker specific logic, including _process_planeswalker_damage) ...
            # This part seems okay, relies on self.planeswalker_damage dict
            attacker_card = gs._safe_get_card(attacker_id)
            damage = self._get_card_power(attacker_card, attacker_player) # Use base getter
            self.planeswalker_damage[planeswalker_id] += damage # Store damage to apply later
            creatures_dealt_damage.add(attacker_id)
            logging.debug(f"COMBAT EXT: {attacker_card.name} assigns {damage} damage to planeswalker {gs._safe_get_card(planeswalker_id).name}")
             # CONSOLIDATION: Use central keyword check
            if self._has_keyword(attacker_card, "lifelink"):
                 # Lifelink gain happens during apply_combat_results or final damage application
                 pass # Defer actual life gain
            return damage # Return damage assigned

        # --- Handle Battle Targeting ---
        if hasattr(gs, "battle_attack_targets") and attacker_id in gs.battle_attack_targets:
            battle_id = gs.battle_attack_targets[attacker_id]
            # ... (Battle specific logic, including _process_battle_damage) ...
            # Check if blocked
            blockers = gs.current_block_assignments.get(attacker_id, [])
            valid_blockers = [b for b in blockers if b not in killed_creatures]
            if valid_blockers:
                 # Blocked: Damage assignment handled by standard combat rules below
                 pass # Fall through to standard damage logic
            else:
                 # Unblocked: Damage goes to battle
                 attacker_card = gs._safe_get_card(attacker_id)
                 damage = self._get_card_power(attacker_card, attacker_player) # Use base getter
                 self.battle_damage[battle_id] += damage # Store damage to apply later
                 creatures_dealt_damage.add(attacker_id)
                 logging.debug(f"COMBAT EXT: {attacker_card.name} assigns {damage} damage to battle {gs._safe_get_card(battle_id).name}")
                 # CONSOLIDATION: Use central keyword check
                 if self._has_keyword(attacker_card, "lifelink"):
                      pass # Defer life gain
                 return damage # Return damage assigned

        # --- Normal Damage (to player or blockers) ---
        # Call the base class method to handle standard damage assignment (player or blockers)
        # The base class needs _process_attacker_damage defined now.
        # We assume the base class handles damage assignment to creatures/players and lifelink triggering correctly.
        # It should populate damage_to_creatures, damage_to_players, and call _add_combat_trigger appropriately.
        # For now, mimic the core logic needed here, assuming base class is not fully implemented yet.

        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card or attacker_id in killed_creatures: return 0

        blockers = gs.current_block_assignments.get(attacker_id, [])
        valid_blockers = [b for b in blockers if b not in killed_creatures]
        damage = self._get_card_power(attacker_card, attacker_player)
        if damage <= 0: return 0

        # CONSOLIDATION: Use central keyword check
        has_trample = self._has_keyword(attacker_card, "trample")
        has_deathtouch = self._has_keyword(attacker_card, "deathtouch")

        total_damage_dealt = 0

        if not valid_blockers: # Unblocked damage to player
             # Apply replacement effects for damage to player
             damage_context = {"source_id": attacker_id, "target_id": ("p2" if defender_player == gs.p2 else "p1"), "target_is_player": True, "damage_amount": damage, "is_combat_damage": True}
             if hasattr(gs, 'replacement_effects'):
                 modified_context, _ = gs.replacement_effects.apply_replacements("DAMAGE", damage_context)
                 actual_damage = modified_context.get("damage_amount", 0)
             else: actual_damage = damage

             if actual_damage > 0:
                  defender_player_key = "p2" if defender_player == gs.p2 else "p1"
                  damage_to_players[defender_player_key] += actual_damage
                  # Actual life loss happens in apply_combat_results or SBA check
                  logging.debug(f"COMBAT EXT: {attacker_card.name} assigns {actual_damage} damage to player {defender_player['name']}.")
                  total_damage_dealt += actual_damage
                  creatures_dealt_damage.add(attacker_id)
                  self._add_combat_trigger(attacker_id, "deals_combat_damage_to_player", {"damage_amount": actual_damage}, is_first_strike)

        else: # Blocked damage
             # Determine damage assignment order (Needs AI input or heuristic)
             # Simplified: Assign lethal to blockers in order, trample leftover
             remaining_damage = damage
             ordered_blockers = sorted(valid_blockers, key=lambda bid: self._get_card_toughness(gs._safe_get_card(bid), defender_player)) # Simple order

             for blocker_id in ordered_blockers:
                  if remaining_damage <= 0: break
                  blocker_card = gs._safe_get_card(blocker_id)
                  if not blocker_card: continue
                  blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                  current_damage_on_blocker = damage_to_creatures.get(blocker_id, 0) # Damage assigned *this step*
                  lethal_needed = max(0, blocker_toughness - current_damage_on_blocker)
                  assign_amount = 1 if has_deathtouch else lethal_needed
                  actual_assign = min(remaining_damage, assign_amount)

                  # Apply replacement effects for damage to blocker
                  damage_context = {"source_id": attacker_id, "target_id": blocker_id, "target_is_player": False, "damage_amount": actual_assign, "is_combat_damage": True}
                  if hasattr(gs, 'replacement_effects'):
                      modified_context, _ = gs.replacement_effects.apply_replacements("DAMAGE", damage_context)
                      actual_damage = modified_context.get("damage_amount", 0)
                  else: actual_damage = actual_assign

                  if actual_damage > 0:
                       damage_to_creatures[blocker_id] += actual_damage
                       # Actual damage marking happens in apply_combat_results or SBA check
                       logging.debug(f"COMBAT EXT: {attacker_card.name} assigns {actual_damage} damage to blocker {blocker_card.name}.")
                       total_damage_dealt += actual_damage # Track total for lifelink
                       creatures_dealt_damage.add(attacker_id)
                       self._add_combat_trigger(attacker_id, "deals_combat_damage_to_creature", {"damage_amount": actual_damage, "target_id": blocker_id}, is_first_strike)
                       self._add_combat_trigger(blocker_id, "is_dealt_combat_damage", {"damage_amount": actual_damage, "source_id": attacker_id}, is_first_strike)


                  remaining_damage -= actual_assign # Reduce remaining based on *attempted* assign

             # Trample damage
             if has_trample and remaining_damage > 0:
                  # Apply replacement effects for trample damage to player
                  damage_context = {"source_id": attacker_id, "target_id": ("p2" if defender_player == gs.p2 else "p1"), "target_is_player": True, "damage_amount": remaining_damage, "is_combat_damage": True, "is_trample": True}
                  if hasattr(gs, 'replacement_effects'):
                       modified_context, _ = gs.replacement_effects.apply_replacements("DAMAGE", damage_context)
                       actual_damage = modified_context.get("damage_amount", 0)
                  else: actual_damage = remaining_damage

                  if actual_damage > 0:
                       defender_player_key = "p2" if defender_player == gs.p2 else "p1"
                       damage_to_players[defender_player_key] += actual_damage
                       logging.debug(f"COMBAT EXT: {attacker_card.name} assigns {actual_damage} trample damage to player {defender_player['name']}.")
                       total_damage_dealt += actual_damage
                       creatures_dealt_damage.add(attacker_id)
                       self._add_combat_trigger(attacker_id, "deals_combat_damage_to_player", {"damage_amount": actual_damage, "is_trample": True}, is_first_strike)


        # Lifelink check (defer actual gain)
        # CONSOLIDATION: Use central keyword check
        if self._has_keyword(attacker_card, "lifelink") and total_damage_dealt > 0:
             # Store potential life gain, apply later
             if not hasattr(self, 'potential_lifegain'): self.potential_lifegain = defaultdict(int)
             self.potential_lifegain[attacker_player['name']] += total_damage_dealt

        return total_damage_dealt # Return total assigned damage
    
    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central AbilityHandler."""
        gs = self.game_state
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
             card_id = getattr(card, 'card_id', None)
             if card_id:
                  # Assumes AbilityHandler has a check_keyword method
                  return gs.ability_handler.check_keyword(card_id, keyword)

        # Fallback basic check if no handler or method found
        logging.warning(f"Using basic keyword fallback check for {keyword} on {getattr(card, 'name', 'Unknown')}")
        if card and hasattr(card, 'oracle_text') and isinstance(card.oracle_text, str):
             return keyword.lower() in card.oracle_text.lower()
        return False
        
    def _process_planeswalker_damage(self, attacker_id, attacker_player, planeswalker_id, damage_to_creatures, creatures_dealt_damage):
        """Process damage to a planeswalker."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        planeswalker_card = gs._safe_get_card(planeswalker_id)
        if not attacker_card or not planeswalker_card: return 0

        damage = self._get_card_power(attacker_card, attacker_player)
        self.planeswalker_damage[planeswalker_id] += damage
        creatures_dealt_damage.add(attacker_id)
        logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to planeswalker {planeswalker_card.name}")

        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life (vs Planeswalker)")

        return damage
    
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
        
    def _process_battle_damage(self, attacker_id, attacker_player, battle_id, creatures_dealt_damage):
        """Process damage dealt to a battle card."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        battle_card = gs._safe_get_card(battle_id)

        if not attacker_card or not battle_card:
            return 0

        damage = self._get_card_power(attacker_card, attacker_player)

        # Use the specific method for damaging battles
        self.damage_to_battle(battle_id, attacker_id, damage)

        # Mark damage dealt
        creatures_dealt_damage.add(attacker_id)

        # Apply lifelink if attacker has it
        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life (vs Battle)")

        return damage
        
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
        
    def damage_to_battle(self, battle_id, source_id, amount):
        """Deal damage to a battle"""
        gs = self.game_state
        battle_card = gs._safe_get_card(battle_id)
        source_card = gs._safe_get_card(source_id)
        
        if not battle_card or not source_card:
            return False
            
        # Record the damage
        self.battle_damage[battle_id] += amount
        
        logging.debug(f"COMBAT: {source_card.name} deals {amount} damage to battle {battle_card.name}")
        return True
