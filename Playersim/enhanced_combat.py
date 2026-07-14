import logging
from collections import defaultdict
from .combat import CombatResolver

class ExtendedCombatResolver(CombatResolver):
    """
    Extended version of the CombatResolver that adds support for:
    - Planeswalker damage
    - Battle damage
    - First strike ordering
    - Ninjutsu
    - Multiple blocker assignment
    
    This class inherits from CombatResolver and extends it with new capabilities.
    """
    
    def __init__(self, game_state):
        super().__init__(game_state)
        self.planeswalker_damage = defaultdict(int) # Tracks damage dealt TO planeswalkers this combat
        self.battle_damage = defaultdict(int) # Tracks damage dealt TO battles this combat
        self._reset_resolution_tracking() # Initialize internal tracking

    def _reset_resolution_tracking(self):
        """Helper to reset tracking vars used within resolve_combat steps."""
        self.combat_log = []
        # Tracks potential life gain from sources with lifelink
        self.potential_lifegain = defaultdict(lambda: defaultdict(int)) # {player_key: {source_id: amount}}
        # Tracks actual damage applied after replacements/prevention for lifelink trigger
        # Maps source ID to the total damage it actually dealt this step
        self._damage_applied_this_step = defaultdict(float) # Use float for potential division
        # Tracks the final damage value applied to each target ID after replacements
        self.final_damage_applied = defaultdict(int)
        self.combat_triggers = [] # Used by _add_combat_trigger

    def _clear_combat_state(self):
        """Remove participants and per-combat bookkeeping after combat ends."""
        gs = self.game_state
        gs.current_attackers = []
        gs.current_block_assignments = {}
        gs.blocked_attackers_this_combat = set()
        gs.first_strike_damage_dealt = False
        gs.first_strike_damage_participants = set()
        gs.exerted_this_combat = set()
        for attr in (
                "planeswalker_attack_targets", "battle_attack_targets",
                "planeswalker_protectors", "first_strike_ordering"):
            value = getattr(gs, attr, None)
            if hasattr(value, "clear"):
                value.clear()
        
    def resolve_combat(self):
        """
        Implements a complete combat resolution sequence following MTG rules:
        Marks damage based on steps, applies damage, handles lifelink, relies on GameState for SBAs.
        Returns dict with damage_to_opponent and potential_lifegain breakdown.
        """
        try:
            gs = self.game_state

            # Reset internal tracking for this call
            self._reset_resolution_tracking()

            # Public phase progression resolves one damage step at a time so
            # players receive a real priority window between first-strike and
            # regular damage. Direct resolver callers outside a damage phase
            # retain the legacy whole-combat behavior used by simulations and
            # focused rules tests.
            if gs.phase == gs.PHASE_FIRST_STRIKE_DAMAGE:
                resolution_mode = "first"
            elif gs.phase == gs.PHASE_COMBAT_DAMAGE:
                resolution_mode = "regular"
            else:
                resolution_mode = "all"

            if (resolution_mode != "first" and gs.combat_damage_dealt):
                logging.debug("Combat damage already applied this turn, skipping resolution.")
                # Return 0 damage dealt and no lifelink gain possibility
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            if not gs.current_attackers:
                logging.debug("No attackers declared; skipping combat resolution.")
                if resolution_mode == "first":
                    gs.first_strike_damage_dealt = True
                else:
                    gs.combat_damage_dealt = True
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            if (resolution_mode == "first"
                    and getattr(gs, "first_strike_damage_dealt", False)):
                logging.debug("First-strike combat damage already applied, skipping resolution.")
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            # CR 509.1h: an attacker remains blocked for the rest of combat
            # even if every blocker later leaves. Preserve that declaration
            # separately because zone cleanup removes dead blockers from the
            # live assignment map between damage steps.
            if not hasattr(gs, "blocked_attackers_this_combat"):
                gs.blocked_attackers_this_combat = set()
            gs.blocked_attackers_this_combat.update(
                attacker_id
                for attacker_id, blockers in
                getattr(gs, "current_block_assignments", {}).items()
                if blockers)

            # Combat ownership follows the turn, never the observation seat.
            # ``agent_is_p1`` changes while the environment services both
            # policies and is not a stable rules-state source of truth.
            attacker_player = gs._get_active_player()
            defender_player = gs._get_non_active_player()

            self._log_combat_state() # Log participants and their stats

            # Check if first strike damage step is needed
            combatants = list(gs.current_attackers) # Start with attackers
            for blockers in gs.current_block_assignments.values(): combatants.extend(blockers) # Add blockers
            if resolution_mode == "all":
                gs.first_strike_damage_participants = {
                    cid for cid in combatants
                    if ((card := gs._safe_get_card(cid)) is not None
                        and (self._has_keyword(card, "first strike")
                             or self._has_keyword(card, "double strike")))
                }
            first_strike_participants = set(getattr(
                gs, "first_strike_damage_participants", set()))
            needs_first_strike_step = bool(first_strike_participants)

            if resolution_mode == "first" and not needs_first_strike_step:
                # The step was created at blocker declaration, but its last
                # first/double-strike combatant may have left before damage.
                # Complete the empty first-strike step without leaking regular
                # damage into it.
                gs.first_strike_damage_dealt = True
                return {"damage_to_opponent": 0, "potential_lifegain": {}}

            # --- Structures to MARK damage before application (with source tracking) ---
            # {target_id: {'amount': int, 'sources': {source_id: damage}, 'deathtouch': bool}}
            damage_marked_on_creatures = defaultdict(lambda: {'amount': 0, 'sources': defaultdict(int), 'deathtouch': False})
            # {player_key: {source_id: damage}} - Player key is 'p1' or 'p2'
            damage_marked_on_players = {"p1": defaultdict(int), "p2": defaultdict(int)}
            # {target_id: {source_id: damage}} - Target ID is planeswalker/battle card_id
            damage_marked_on_planeswalkers = defaultdict(lambda: defaultdict(int))
            damage_marked_on_battles = defaultdict(lambda: defaultdict(int))
            # Sets to track which creatures dealt damage in each step (for triggers)
            creatures_dealt_damage_fs = set()
            creatures_dealt_damage_regular = set()

            # --- STEP 1: First Strike Damage Phase (Calculation & Application) ---
            if needs_first_strike_step and resolution_mode != "regular":
                 logging.debug("COMBAT EXT: Calculating First Strike Damage")
                 # Iterate copies of lists to avoid modification issues during processing
                 attackers_copy = list(gs.current_attackers)
                 block_assignments_copy = {k: list(v) for k, v in gs.current_block_assignments.items()}

                 # Calculate damage marks
                 for attacker_id in attackers_copy:
                     # Check if still valid (on battlefield)
                     _, attacker_zone = gs.find_card_location(attacker_id)
                     if attacker_zone != 'battlefield': continue
                     self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, creatures_dealt_damage_fs, is_first_strike=True)

                 for attacker_id, blockers in block_assignments_copy.items():
                     # Check if attacker still valid
                     _, attacker_zone = gs.find_card_location(attacker_id)
                     if attacker_zone != 'battlefield': continue
                     for blocker_id in blockers:
                         # Check if blocker still valid
                         _, blocker_zone = gs.find_card_location(blocker_id)
                         if blocker_zone != 'battlefield': continue
                         self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_marked_on_creatures, creatures_dealt_damage_fs, is_first_strike=True)

                 # --- Apply First Strike Damage Marks & Handle Lifelink ---
                 # Apply marked damage, which populates self._damage_applied_this_step and self.final_damage_applied
                 self._apply_marked_damage(damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, is_first_strike=True)

                 # Trigger combat damage events AFTER damage application & lifelink gain
                 # self._process_combat_triggers(creatures_dealt_damage_fs, is_first_strike=True) # Trigger processing deferred to main loop

                 # --- CR 510.4 BUGFIX (July 2026): state-based actions MUST run
                 # between the first-strike and regular damage steps. Both steps
                 # happen inside this one call, so 'the game loop will check
                 # later' never happened in between -- creatures dealt lethal
                 # first-strike damage stayed on the battlefield (the regular
                 # step only skips combatants that have LEFT it) and struck
                 # back. First strike was cosmetic.
                 if hasattr(gs, 'check_state_based_actions'):
                     gs.check_state_based_actions()

                 gs.first_strike_damage_dealt = True

                 # Clear marking structures for the regular damage step
                 damage_marked_on_creatures.clear()
                 damage_marked_on_players = {"p1": defaultdict(int), "p2": defaultdict(int)}
                 damage_marked_on_planeswalkers.clear()
                 damage_marked_on_battles.clear()
                 self._damage_applied_this_step.clear() # Clear step-specific lifelink tracker

                 if resolution_mode == "first":
                     defender_key = "p2" if defender_player == gs.p2 else "p1"
                     return {
                         "damage_to_opponent": self.final_damage_applied.get(
                             defender_key, 0),
                         "potential_lifegain": {
                             p: dict(sources)
                             for p, sources in self.potential_lifegain.items()}
                     }

            # --- STEP 2: Regular Damage Phase (Calculation & Application) ---
            logging.debug("COMBAT EXT: Calculating Regular Damage")
            # Refresh attacker/blocker lists potentially changed by SBAs after First Strike
            attackers_copy = list(gs.current_attackers)
            block_assignments_copy = {k: list(v) for k, v in gs.current_block_assignments.items()}

            # Calculate damage marks
            for attacker_id in attackers_copy:
                 _, zone = gs.find_card_location(attacker_id) # Check if still on battlefield
                 if zone != 'battlefield': continue
                 self._process_attacker_damage(attacker_id, attacker_player, defender_player, damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, creatures_dealt_damage_regular, is_first_strike=False)

            for attacker_id, blockers in block_assignments_copy.items():
                 _, attacker_zone = gs.find_card_location(attacker_id)
                 if attacker_zone != 'battlefield': continue
                 for blocker_id in blockers:
                      _, blocker_zone = gs.find_card_location(blocker_id)
                      if blocker_zone != 'battlefield': continue
                      self._process_blocker_damage(blocker_id, attacker_id, attacker_player, defender_player, damage_marked_on_creatures, creatures_dealt_damage_regular, is_first_strike=False)

            # --- Apply Regular Damage Marks & Handle Lifelink ---
            # Apply marks, triggering lifelink via GS helper based on actual damage dealt
            self._apply_marked_damage(damage_marked_on_creatures, damage_marked_on_players, damage_marked_on_planeswalkers, damage_marked_on_battles, is_first_strike=False)

            # Trigger combat damage events after application
            # self._process_combat_triggers(creatures_dealt_damage_regular, is_first_strike=False) # Deferred to main loop

            # --- SBAs Checked Externally by Game Loop ---

            gs.combat_damage_dealt = True # Mark damage phase as completed for this turn

            # Combatants remain attacking/blocking through the end-of-combat
            # step. GameState._advance_phase clears them when that step ends.

            # --- Determine Final Results ---
            # Get total actual damage applied to the opponent player from final_damage_applied
            defender_key = "p2" if defender_player == gs.p2 else "p1"
            total_damage_to_opponent = self.final_damage_applied.get(defender_key, 0)

            logging.debug(f"COMBAT EXT RESOLUTION COMPLETE: Total applied damage to opponent player: {total_damage_to_opponent}")

            # Return potential lifegain calculated earlier and actual damage dealt to opponent
            return {
                "damage_to_opponent": total_damage_to_opponent,
                 # Convert potential_lifegain to a standard dict for the return value
                 "potential_lifegain": {p: dict(sources) for p, sources in self.potential_lifegain.items()}
            }

        except Exception as e:
            logging.error(f"Error in extended combat resolution: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return {"damage_to_opponent": 0, "potential_lifegain": {}} # Return default on error
        
    def _apply_marked_damage(self, marked_creatures, marked_players, marked_pws, marked_battles, is_first_strike=False):
        """Applies the calculated damage marks using GameState methods."""
        gs = self.game_state
        if not hasattr(self, '_damage_applied_this_step'): self._damage_applied_this_step = defaultdict(int)
        else: self._damage_applied_this_step.clear()
        if not hasattr(self, 'final_damage_applied'):
            self.final_damage_applied = defaultdict(int)
        # Keep the first-strike totals when applying the regular step. The
        # per-resolution accumulator is already cleared by
        # _reset_resolution_tracking().


        # Apply to Creatures
        for target_id, damage_info in marked_creatures.items():
            base_amount = damage_info.get("amount", 0)
            sources = damage_info.get("sources", {}) # {source_id: damage}
            is_combat = True

            if base_amount <= 0: continue

            # Damage from separate sources is applied and attributed
            # separately. Aggregating it under the first source broke
            # protection, lifelink, deathtouch, and source-side triggers when
            # multiple blockers damaged one attacker simultaneously.
            for source_id, marked_by_source in sources.items():
                if marked_by_source <= 0:
                    continue
                source_has_deathtouch = bool(
                    hasattr(gs, 'check_keyword')
                    and gs.check_keyword(source_id, "deathtouch"))
                damage_marked = gs.apply_damage_to_permanent(
                    target_id, marked_by_source, source_id, is_combat,
                    source_has_deathtouch)
                if damage_marked > 0:
                    self.final_damage_applied[target_id] += damage_marked
                    self._damage_applied_this_step[source_id] += damage_marked


        # Apply to Players
        for player_key, source_damage_map in marked_players.items():
            player_obj = gs.p1 if player_key == "p1" else gs.p2
            if not player_obj: continue

            total_damage_to_player = 0
            for source_id, damage_amount in source_damage_map.items():
                 if damage_amount <= 0: continue
                 # Use GS method to apply damage to player, it returns actual damage dealt (after replacements)
                 damage_applied = gs.damage_player(
                     player_obj, damage_amount, source_id,
                     is_combat_damage=True, defer_sba=True)
                 if damage_applied > 0:
                     total_damage_to_player += damage_applied
                     # Attribute applied damage back to the correct source
                     self._damage_applied_this_step[source_id] += damage_applied
            # Store total applied to player
            self.final_damage_applied[player_key] += total_damage_to_player

        # Apply to Planeswalkers
        for target_id, source_damage_map in marked_pws.items():
             total_damage_to_pw = 0
             for source_id, damage_amount in source_damage_map.items():
                  if damage_amount <= 0: continue
                  damage_applied = gs.damage_planeswalker(
                      target_id, damage_amount, source_id,
                      defer_sba=True, is_combat_damage=True)
                  if damage_applied > 0:
                      total_damage_to_pw += damage_applied
                      self._damage_applied_this_step[source_id] += damage_applied
             self.final_damage_applied[target_id] += total_damage_to_pw


        # Apply to Battles
        for target_id, source_damage_map in marked_battles.items():
             total_damage_to_battle = 0
             for source_id, damage_amount in source_damage_map.items():
                  if damage_amount <= 0: continue
                  damage_applied = gs.damage_battle(
                      target_id, damage_amount, source_id,
                      is_combat_damage=True, defer_sba=True)
                  if damage_applied > 0:
                      total_damage_to_battle += damage_applied
                      self._damage_applied_this_step[source_id] += damage_applied
             self.final_damage_applied[target_id] += total_damage_to_battle


        # Lifelink is applied once by each canonical GameState damage helper.
        # Keeping it there covers combat and noncombat damage, printed and
        # layer-granted lifelink, without a duplicate delayed replacement.

        # Combat damage and lifelink happen simultaneously. Defer state-based
        # actions until every source/target pair and all associated life gain
        # have been applied.
        if hasattr(gs, 'check_state_based_actions'):
            gs.check_state_based_actions()

        self._damage_applied_this_step.clear() # Clear for next phase
        
    def _process_blocker_damage(self, blocker_id, attacker_id, attacker_player, defender_player,
                                damage_marked_on_creatures, creatures_dealt_damage_step, is_first_strike):
        """Calculates damage assignment from a blocker, MARKS it, returns total potential damage."""
        gs = self.game_state
        blocker_card = gs._safe_get_card(blocker_id)

        if not self._should_deal_damage_this_phase(blocker_card, is_first_strike): return 0

        attacker_card = gs._safe_get_card(attacker_id)
        _, blocker_zone = gs.find_card_location(blocker_id)
        _, attacker_zone = gs.find_card_location(attacker_id)
        if not blocker_card or not attacker_card or blocker_zone != 'battlefield' or attacker_zone != 'battlefield': return 0

        damage = self._get_card_power(blocker_card, defender_player)
        if damage <= 0: return 0

        has_deathtouch = self._has_keyword(blocker_card, "deathtouch")
        has_lifelink = self._has_keyword(blocker_card, "lifelink")
        total_potential_damage = 0

        # Mark damage on the attacker
        damage_info = damage_marked_on_creatures[attacker_id] # Get the defaultdict entry
        damage_info["amount"] += damage
        # Track damage source for attacker damage
        damage_info["sources"][blocker_id] = damage_info["sources"].get(blocker_id, 0) + damage
        damage_info["deathtouch"] = damage_info["deathtouch"] or (has_deathtouch and damage > 0)

        total_potential_damage = damage

        logging.debug(f"COMBAT EXT Mark: Blocker {blocker_card.name} assigns {damage} to attacker {attacker_card.name}")
        creatures_dealt_damage_step.add(blocker_id)
        # Add triggers here
        self._add_combat_trigger(blocker_id, "deals_combat_damage_to_creature", {"damage_amount": damage, "target_id": attacker_id}, is_first_strike)
        self._add_combat_trigger(attacker_id, "is_dealt_combat_damage", {"damage_amount": damage, "source_id": blocker_id}, is_first_strike)

        # Update potential lifegain (approximate)
        if total_potential_damage > 0 and has_lifelink:
             player_key = "p2" if defender_player == gs.p2 else "p1"
             self.potential_lifegain[player_key][blocker_id] += total_potential_damage
             logging.debug(f"COMBAT EXT Potential Lifelink: {blocker_card.name} may gain {total_potential_damage} life")

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
                
    def _mark_attacked_object_damage(
            self, attacker_id, attacker_card, defender_player, amount,
            damage_marked_on_players, damage_marked_on_planeswalkers,
            damage_marked_on_battles, is_first_strike, is_trample=False):
        """Mark damage on the player, planeswalker, or battle being attacked."""
        if amount <= 0:
            return 0
        gs = self.game_state
        pw_target_id = getattr(
            gs, 'planeswalker_attack_targets', {}).get(attacker_id)
        battle_target_id = getattr(
            gs, 'battle_attack_targets', {}).get(attacker_id)
        trigger_context = {"damage_amount": amount}
        if is_trample:
            trigger_context["is_trample"] = True

        if pw_target_id is not None:
            damage_marked_on_planeswalkers[pw_target_id][attacker_id] += amount
            trigger_context["target_id"] = pw_target_id
            logging.debug(
                "COMBAT EXT Mark: %s will deal %s%s damage to PW %s",
                attacker_card.name, amount, " trample" if is_trample else "",
                getattr(gs._safe_get_card(pw_target_id), 'name', pw_target_id))
            self._add_combat_trigger(
                attacker_id, "deals_combat_damage_to_planeswalker",
                trigger_context, is_first_strike)
        elif battle_target_id is not None:
            damage_marked_on_battles[battle_target_id][attacker_id] += amount
            trigger_context["target_id"] = battle_target_id
            logging.debug(
                "COMBAT EXT Mark: %s will deal %s%s damage to Battle %s",
                attacker_card.name, amount, " trample" if is_trample else "",
                getattr(gs._safe_get_card(battle_target_id), 'name',
                        battle_target_id))
            self._add_combat_trigger(
                attacker_id, "deals_combat_damage_to_battle",
                trigger_context, is_first_strike)
        else:
            defender_key = "p2" if defender_player == gs.p2 else "p1"
            damage_marked_on_players[defender_key][attacker_id] += amount
            logging.debug(
                "COMBAT EXT Mark: %s will deal %s%s damage to player %s",
                attacker_card.name, amount, " trample" if is_trample else "",
                defender_player['name'])
            self._add_combat_trigger(
                attacker_id, "deals_combat_damage_to_player",
                trigger_context, is_first_strike)
        return amount

    def _process_attacker_damage(self, attacker_id, attacker_player, defender_player,
                                damage_marked_on_creatures, damage_marked_on_players,
                                damage_marked_on_planeswalkers, damage_marked_on_battles,
                                creatures_dealt_damage_step, is_first_strike):
        """Calculates damage assignment from an attacker, MARKS it, returns total potential damage."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)

        if not self._should_deal_damage_this_phase(attacker_card, is_first_strike): return 0
        if not attacker_card: return 0

        damage = self._get_card_power(attacker_card, attacker_player)
        if damage <= 0: return 0

        has_trample = self._has_keyword(attacker_card, "trample")
        has_deathtouch = self._has_keyword(attacker_card, "deathtouch")
        has_lifelink = self._has_keyword(attacker_card, "lifelink") # Store for potential lifegain calc

        total_potential_damage = 0

        blockers = gs.current_block_assignments.get(attacker_id, [])
        valid_blockers = [bid for bid in blockers if gs.find_card_location(bid)[1] == 'battlefield'] # Check if blocker still exists
        was_blocked = attacker_id in getattr(
            gs, "blocked_attackers_this_combat", set())

        if not valid_blockers:
            if was_blocked and not has_trample:
                logging.debug(
                    "COMBAT EXT: %s remains blocked after its blockers left; "
                    "no damage reaches the attacked object.", attacker_card.name)
                total_potential_damage = 0
            else:
                total_potential_damage = self._mark_attacked_object_damage(
                    attacker_id, attacker_card, defender_player, damage,
                    damage_marked_on_players,
                    damage_marked_on_planeswalkers,
                    damage_marked_on_battles, is_first_strike,
                    is_trample=was_blocked and has_trample)
        else: # Blocked
            # --- Damage Assignment Logic ---
            ordered_blockers = valid_blockers # Use simple order for now, ordering handled by first_strike_ordering/user choice in handler
            # Re-fetch order if defined
            if hasattr(gs, 'first_strike_ordering') and attacker_id in gs.first_strike_ordering:
                 # Use the defined order, filtering out any now-invalid blockers
                 ordered_blockers = [b_id for b_id in gs.first_strike_ordering[attacker_id] if b_id in valid_blockers]
            # Add any remaining valid blockers not in the defined order (shouldn't happen if order set correctly)
            ordered_blockers.extend([b_id for b_id in valid_blockers if b_id not in ordered_blockers])

            remaining_damage = damage
            potential_damage_this_step = 0

            for blocker_id in ordered_blockers:
                if remaining_damage <= 0 and not has_deathtouch: break # Can stop assigning if no deathtouch and no damage left
                blocker_card = gs._safe_get_card(blocker_id)
                if not blocker_card: continue

                blocker_toughness = self._get_card_toughness(blocker_card, defender_player)
                existing_damage = defender_player.get("damage_counters", {}).get(blocker_id, 0)
                # Lethal damage: 1 for deathtouch, or toughness - existing damage
                lethal_needed = max(0, blocker_toughness - existing_damage)
                if has_deathtouch and lethal_needed > 0:
                    lethal_needed = 1

                # Must assign at least lethal, unless insufficient damage remains
                assign_amount = min(remaining_damage, lethal_needed)

                # Apply assigned blocker damage to the marking dict
                damage_info = damage_marked_on_creatures[blocker_id]
                damage_info["amount"] += assign_amount
                damage_info["sources"][attacker_id] = damage_info["sources"].get(attacker_id, 0) + assign_amount
                damage_info["deathtouch"] = damage_info["deathtouch"] or (has_deathtouch and assign_amount > 0)
                logging.debug(f"COMBAT EXT Mark: {attacker_card.name} assigns {assign_amount} to blocker {blocker_card.name}")
                self._add_combat_trigger(attacker_id, "deals_combat_damage_to_creature", {"damage_amount": assign_amount, "target_id": blocker_id}, is_first_strike)
                self._add_combat_trigger(blocker_id, "is_dealt_combat_damage", {"damage_amount": assign_amount, "source_id": attacker_id}, is_first_strike)

                remaining_damage -= assign_amount
                potential_damage_this_step += assign_amount


            # Trample damage
            if has_trample and remaining_damage > 0:
                 potential_damage_this_step += self._mark_attacked_object_damage(
                     attacker_id, attacker_card, defender_player,
                     remaining_damage, damage_marked_on_players,
                     damage_marked_on_planeswalkers,
                     damage_marked_on_battles, is_first_strike,
                     is_trample=True)

            total_potential_damage = potential_damage_this_step

        # Mark creature as having dealt damage if any damage was assigned
        if total_potential_damage > 0:
            creatures_dealt_damage_step.add(attacker_id)
            # Update potential lifegain (this remains approximate until final application)
            if has_lifelink:
                 player_key = "p1" if attacker_player == gs.p1 else "p2"
                 self.potential_lifegain[player_key][attacker_id] += total_potential_damage
                 logging.debug(f"COMBAT EXT Potential Lifelink: {attacker_card.name} may gain {total_potential_damage} life")

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
        card_id = getattr(card, "card_id", None)
        first_step_combatants = getattr(
            self.game_state, "first_strike_damage_participants", set())
        has_double_strike = self._has_keyword(card, "double strike")    

        if is_first_strike_phase:
            return card_id in first_step_combatants
        # CR 510.4: creatures that were eligible for the first damage step do
        # not deal regular damage unless they currently have double strike.
        # Everyone that missed the first step still deals regular damage even
        # if its current keywords changed in the between-step window.
        return card_id not in first_step_combatants or has_double_strike
                
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
