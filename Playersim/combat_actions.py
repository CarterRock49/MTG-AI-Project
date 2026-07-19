import logging
import re

from .ability_types import Ability
from .enhanced_combat import ExtendedCombatResolver  # If referenced directly


class NinjutsuStackAbility(Ability):
    """The stack object created after Ninjutsu's activation costs are paid."""

    def __init__(self, card_id, returned_attacker_id,
                 attack_target_kind=None, attack_target_id=None,
                 source_zone_generation=None):
        super().__init__(
            card_id,
            "Put this card onto the battlefield from your hand tapped and "
            "attacking.")
        self.effect = self.effect_text
        self.returned_attacker_id = returned_attacker_id
        self.attack_target_kind = attack_target_kind
        self.attack_target_id = attack_target_id
        self.source_zone_generation = source_zone_generation
        self.keyword = "ninjutsu"
        self.requires_target = False

    def resolve(self, game_state, controller, context=None):
        """Put the source card in combat only if it is still in that hand."""
        owner, zone = game_state.find_card_location(self.card_id)
        source = game_state._safe_get_card(self.card_id)
        current_generation = getattr(
            source, "_zone_change_generation", None) if source else None
        if (owner is not controller or zone != "hand"
                or (self.source_zone_generation is not None
                    and current_generation != self.source_zone_generation)):
            # Ninjutsu does not reveal the card until resolution. If it left
            # its owner's hand in response, the ability simply does nothing.
            return True

        if not game_state.move_card(
                self.card_id, controller, "hand", controller, "battlefield",
                cause="ninjutsu_enter", context={"used_ninjutsu": True}):
            return False

        game_state.tap_permanent(self.card_id, controller)
        if self.card_id not in game_state.current_attackers:
            game_state.current_attackers.append(self.card_id)

        if (self.attack_target_kind == "planeswalker"
                and self.attack_target_id is not None):
            game_state.planeswalker_attack_targets[
                self.card_id] = self.attack_target_id
        elif (self.attack_target_kind == "battle"
              and self.attack_target_id is not None):
            game_state.battle_attack_targets[
                self.card_id] = self.attack_target_id

        # A Ninja entering before the first combat-damage step can create a
        # first-strike step. Between damage steps, it participates only in the
        # remaining regular-damage step (CR 510.4).
        first_step_participants = game_state.first_strike_damage_participants
        if not game_state.first_strike_damage_dealt:
            has_first_strike = game_state.check_keyword(
                self.card_id, "first strike")
            has_double_strike = game_state.check_keyword(
                self.card_id, "double strike")
            if has_first_strike or has_double_strike:
                first_step_participants.add(self.card_id)
                underlying_phase = (
                    game_state.previous_priority_phase
                    if game_state.phase == game_state.PHASE_PRIORITY
                    else game_state.phase)
                if underlying_phase == game_state.PHASE_COMBAT_DAMAGE:
                    if game_state.phase == game_state.PHASE_PRIORITY:
                        game_state.previous_priority_phase = \
                            game_state.PHASE_FIRST_STRIKE_DAMAGE
                    else:
                        game_state.phase = \
                            game_state.PHASE_FIRST_STRIKE_DAMAGE

        logging.info(
            "Ninjutsu resolved: %s entered tapped and attacking.",
            getattr(game_state._safe_get_card(self.card_id),
                    "name", self.card_id))
        return True

class CombatActionHandler:
    """
    Handles specialized combat actions in MTG, implementing specific mechanics with clear, focused responsibilities.
    
    This class is specifically responsible for game state actions during combat, 
    distinguishing it from the combat resolution logic in the resolver.
    """
    
    def __init__(self, game_state):
        """
        Initialize the combat action handler with game state tracking.

        Args:
            game_state: The game state object
        """
        self.game_state = game_state

        # Initialize card evaluator if needed
        if hasattr(game_state, 'card_evaluator'):
            self.card_evaluator = game_state.card_evaluator
        else:
             # Initialize evaluator if not present in game_state yet
             try:
                 # Assuming EnhancedCardEvaluator is available
                 from .enhanced_card_evaluator import EnhancedCardEvaluator
                 self.card_evaluator = EnhancedCardEvaluator(game_state,
                 getattr(game_state, 'stats_tracker', None),
                 getattr(game_state, 'card_memory', None))
                 game_state.card_evaluator = self.card_evaluator
             except ImportError:
                 logging.warning("EnhancedCardEvaluator not found, evaluator functionality limited.")
                 self.card_evaluator = None
             except Exception as e:
                  logging.error(f"Failed to init CardEvaluator in CombatActionHandler: {e}")
                  self.card_evaluator = None

        # Initialize tracking dictionaries for combat state
        self._initialize_combat_state_tracking()

        logging.debug("CombatActionHandler initialized")

    def _add_planeswalker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for planeswalker loyalty abilities."""
        # Ensure planeswalker abilities can only be activated at sorcery speed
        gs = self.game_state
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        is_main_phase_empty_stack = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack

        if not is_my_turn or not is_main_phase_empty_stack:
            return # Can only activate at sorcery speed

        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 5: break # ACTION_MEANINGS only maps up to index 4 for ATTACK_PLANESWALKER/DEFEND_BATTLE
            card = gs._safe_get_card(card_id)
            if (card and getattr(card, "loyalty_abilities", [])
                    and card_id in player.get("loyalty_counters", {})):
                already_activated = card_id in player.get("activated_this_turn", set())
                if already_activated:
                    continue

                current_loyalty = player.get("loyalty_counters", {}).get(card_id, getattr(card, 'loyalty', 0))

                if hasattr(card, 'loyalty_abilities'):
                    for ability_idx, ability in enumerate(card.loyalty_abilities):
                        cost = ability.get('cost', 0)
                        is_ultimate = ability.get('is_ultimate', False)

                        # Check affordability based on loyalty
                        if current_loyalty + cost < 0 and cost < 0: continue # Cannot pay minus if loyalty goes < 0

                        effect_text = ability.get('effect', '')
                        if 'target' in effect_text.lower():
                            target_type = gs._get_target_type_from_text(effect_text)
                            minimum, _ = gs._target_bounds_from_text(effect_text)
                            valid_map = gs.targeting_system.get_valid_targets(
                                card_id, player, target_type,
                                effect_text=effect_text)
                            valid_ids = {
                                target_id for ids in valid_map.values()
                                for target_id in ids
                            }
                            if len(valid_ids) < minimum:
                                continue

                        action_context = {
                            "battlefield_idx": idx,
                            "ability_idx": ability_idx,
                            "controller_id": "p1" if player is gs.p1 else "p2",
                        }

                        if cost > 0:
                            # Corrected from 435 to 440 to match ACTION_MEANINGS
                            set_valid_action(440, f"LOYALTY_ABILITY_PLUS for {card.name} (Index {idx})", context=action_context)
                        elif cost == 0:
                            # Corrected from 436 to 441 to match ACTION_MEANINGS
                            set_valid_action(441, f"LOYALTY_ABILITY_ZERO for {card.name} (Index {idx})", context=action_context)
                        else: # cost < 0
                            if is_ultimate:
                                # Corrected from 438 to 443 to match ACTION_MEANINGS
                                set_valid_action(443, f"ULTIMATE_ABILITY for {card.name} (Index {idx})", context=action_context)
                            else:
                                # Corrected from 437 to 442 to match ACTION_MEANINGS
                                set_valid_action(442, f"LOYALTY_ABILITY_MINUS for {card.name} (Index {idx})", context=action_context)

    def _add_equipment_aura_actions(self, player, valid_actions, set_valid_action):
        """Add actions for equipment and aura manipulation with improved cost handling."""
        gs = self.game_state
        # Sorcery speed only
        is_my_turn = (gs.turn % 2 == 1) == gs.agent_is_p1
        is_main_phase_empty_stack = gs.phase in [gs.PHASE_MAIN_PRECOMBAT, gs.PHASE_MAIN_POSTCOMBAT] and not gs.stack
        if not is_my_turn or not is_main_phase_empty_stack: return

        creature_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                            if gs._safe_get_card(cid) and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])]
        equipment_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                            if gs._safe_get_card(cid) and 'equipment' in getattr(gs._safe_get_card(cid), 'subtypes', [])]
        fortification_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                                if gs._safe_get_card(cid) and 'fortification' in getattr(gs._safe_get_card(cid), 'subtypes', [])]

        # Equip (Action 450)
        for eq_idx, equip_id in equipment_indices:
            equip_card = gs._safe_get_card(equip_id)
            is_equipped = equip_id in getattr(player, "attachments", {})

            equip_cost_str = self._get_equip_cost_str(equip_card)
            if equip_cost_str and self._can_afford_cost_string(player, equip_cost_str):
                for c_idx, creature_id in creature_indices:
                    # Don't allow equipping to self if it's currently a creature
                    if equip_id == creature_id: continue
                    # Don't allow re-equipping to the same target
                    if is_equipped and player["attachments"][equip_id] == creature_id: continue
                    # Use context with both identifiers
                    context = {'equip_identifier': equip_id, 'target_identifier': creature_id}
                    set_valid_action(450, f"EQUIP {equip_card.name} to {gs._safe_get_card(creature_id).name}", context=context)

        # Fortify (Action 453)
        land_indices = [(idx, cid) for idx, cid in enumerate(player["battlefield"])
                        if gs._safe_get_card(cid) and 'land' in getattr(gs._safe_get_card(cid), 'type_line', '')]
        for fort_idx, fort_id in fortification_indices:
            fort_card = gs._safe_get_card(fort_id)
            fort_cost_str = self._get_fortify_cost_str(fort_card)
            if fort_cost_str and self._can_afford_cost_string(player, fort_cost_str):
                for l_idx, land_id in land_indices:
                    context = {'fort_identifier': fort_id, 'target_identifier': land_id}
                    set_valid_action(453, f"FORTIFY {fort_card.name} onto {gs._safe_get_card(land_id).name}", context=context)

        # Reconfigure (Action 454)
        for eq_idx, equip_id in equipment_indices:
            equip_card = gs._safe_get_card(equip_id)
            reconf_cost_str = self._get_reconfigure_cost_str(equip_card)
            if reconf_cost_str and self._can_afford_cost_string(player, reconf_cost_str):
                context = {'card_identifier': equip_id}
                set_valid_action(454, f"RECONFIGURE {equip_card.name}", context=context)

    def _add_ninjutsu_actions(self, player, valid_actions, set_valid_action):
        """Add actions for using Ninjutsu."""
        gs = self.game_state

        # The public phase machine uses the pending damage step as the
        # post-blockers priority window.  Restrict the action to the attacking
        # player while combat damage is still pending.  DECLARE_BLOCKERS is
        # retained for direct/legacy callers that explicitly model that
        # priority window without advancing the phase first.
        if (player is not gs._get_active_player()
                or gs.priority_player is not player
                or gs.phase not in (
                    gs.PHASE_DECLARE_BLOCKERS,
                    gs.PHASE_FIRST_STRIKE_DAMAGE,
                    gs.PHASE_COMBAT_DAMAGE)
                or (gs.phase == gs.PHASE_FIRST_STRIKE_DAMAGE
                    and getattr(gs, "first_strike_damage_dealt", False))
                or (gs.phase == gs.PHASE_COMBAT_DAMAGE
                    and getattr(gs, "combat_damage_dealt", False))):
            return

        # Find unblocked attackers controlled by the player
        unblocked_attackers = []
        if hasattr(gs, 'current_attackers'):
            for attacker_id in gs.current_attackers:
                if attacker_id in player["battlefield"]: # Is it mine?
                    # CR 509.1h: once blocked, always blocked for the rest of
                    # combat, even if every blocker later leaves combat.
                    is_blocked = bool(
                        (attacker_id in gs.current_block_assignments
                         and gs.current_block_assignments[attacker_id])
                        or attacker_id in getattr(
                            gs, "blocked_attackers_this_combat", set()))
                    if not is_blocked:
                        # Find index on battlefield for potential param
                        bf_idx = -1
                        for i, cid in enumerate(player["battlefield"]):
                            if cid == attacker_id: bf_idx = i; break
                        if bf_idx != -1:
                            unblocked_attackers.append((bf_idx, attacker_id))

        if not unblocked_attackers: return # No unblocked attackers to swap

        # Check hand for cards with Ninjutsu
        for hand_idx, card_id in enumerate(player["hand"]):
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text') and "ninjutsu" in card.oracle_text.lower():
                ninjutsu_cost_str = self._get_ninjutsu_cost_str(card) # Get cost
                if ninjutsu_cost_str and self._can_afford_cost_string(player, ninjutsu_cost_str):
                    # Allow Ninjutsu action for each possible swap
                    for atk_bf_idx, attacker_id in unblocked_attackers:
                        # Corrected from 432 to 437 to match ACTION_MEANINGS for NINJUTSU
                        context = {
                            'ninja_identifier': hand_idx,
                            'attacker_identifier': atk_bf_idx,
                        }
                        set_valid_action(
                            437,
                            f"NINJUTSU with {card.name} (H:{hand_idx}) for "
                            f"{gs._safe_get_card(attacker_id).name} (B:{atk_bf_idx}) "
                            f"Cost:{ninjutsu_cost_str}",
                            context=context)
        
    def _add_multiple_blocker_actions(self, player, valid_actions, set_valid_action):
        """Add actions for assigning multiple blockers."""
        gs = self.game_state
        # Check phase
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS or not gs.current_attackers: return

        possible_blockers = [
            (bf_idx, cid)
            for bf_idx, cid in enumerate(player["battlefield"])
            if gs._safe_get_card(cid)
            and 'creature' in getattr(gs._safe_get_card(cid), 'card_types', [])
            and cid not in player.get("tapped_permanents", set())]

        if len(possible_blockers) < 2: return # Need at least 2 to multi-block

        # Identify attackers that *can* be blocked by multiple creatures (not required by menace, just possible)
        for atk_idx, attacker_id in enumerate(gs.current_attackers):
             attacker_card = gs._safe_get_card(attacker_id)
             if not attacker_card: continue

             # Check if at least two valid blockers exist for this attacker
             num_valid_for_this_attacker = 0
             valid_blocker_indices = []
             valid_blocker_ids = []
             for blocker_idx, blocker_id in possible_blockers:
                  if self._can_block(blocker_id, attacker_id):
                       num_valid_for_this_attacker += 1
                       valid_blocker_indices.append(blocker_idx)
                       valid_blocker_ids.append(blocker_id)

             # Skip when this exact multi-block is already in place; the
             # handler treats the re-assignment as a failed no-op, so keeping
             # it mask-valid only lets a deterministic policy loop on it.
             if getattr(gs, 'current_block_assignments', {}).get(attacker_id) == valid_blocker_ids:
                 continue

             if num_valid_for_this_attacker >= 2:
                 if atk_idx < 10: # Action 383-392 assume attacker index 0-9
                    set_valid_action(
                        383 + atk_idx,
                        f"ASSIGN_MULTIPLE_BLOCKERS to {attacker_card.name} (Atk Index {atk_idx})",
                        context={"blocker_identifiers": valid_blocker_indices})

            
    def setup_combat_systems(self):
        """
        Set up combat systems for the game if not already present.
        Ensures that all combat-related components are properly initialized and connected.
        """
        gs = self.game_state
        
        # Initialize combat resolver if needed (use Extended by default)
        if not hasattr(gs, 'combat_resolver') or gs.combat_resolver is None:
            logging.debug("Initializing ExtendedCombatResolver.")
            try:
                 gs.combat_resolver = ExtendedCombatResolver(gs)
                 gs.combat_resolver.action_handler = self # Link resolver back to handler if needed by resolver
            except Exception as e:
                 logging.error(f"Failed to initialize ExtendedCombatResolver: {e}")

        # Ensure this handler instance is linked in the game state
        if not hasattr(gs, 'combat_action_handler') or gs.combat_action_handler is not self:
             gs.combat_action_handler = self

        # Initialize combat-related data structures if they don't exist
        combat_attrs = [ "current_attackers", "current_block_assignments",
                         "planeswalker_attack_targets", "battle_attack_targets",
                         "planeswalker_protectors", "first_strike_ordering",
                         "combat_damage_dealt"]
        defaults = { "current_attackers": [], "current_block_assignments": {},
                     "planeswalker_attack_targets": {}, "battle_attack_targets": {},
                     "planeswalker_protectors": {}, "first_strike_ordering": {},
                     "combat_damage_dealt": False}

        for attr in combat_attrs:
             if not hasattr(gs, attr):
                  setattr(gs, attr, defaults[attr])   

    def evaluate_attack_configuration(self, attackers):
        """
        Evaluate the expected value of a particular attack configuration using CombatResolver simulation.
        Returns an estimated reward value.
        """
        gs = self.game_state

        # Use ExtendedCombatResolver's simulate_combat if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'simulate_combat'):
            # Save current state relevant to simulation
            original_attackers = gs.current_attackers[:]
            original_block_assignments = {k: v[:] for k, v in gs.current_block_assignments.items()}

            # Set attackers for simulation
            gs.current_attackers = list(attackers) # Ensure it's a list
            gs.current_block_assignments = {} # Simulate blocks from scratch

            # Simulate combat (including optimal blocks estimation)
            # simulate_combat might need internal optimal block simulation first
            if hasattr(gs.combat_resolver, '_simulate_opponent_blocks'):
                gs.combat_resolver._simulate_opponent_blocks() # Simulate blocks based on current attackers
            simulation_results = gs.combat_resolver.simulate_combat()

            # Restore original state
            gs.current_attackers = original_attackers
            gs.current_block_assignments = original_block_assignments

            # Evaluate based on simulation results
            if isinstance(simulation_results, dict) and "expected_value" in simulation_results:
                # Add strategic adjustments based on game state
                value = simulation_results["expected_value"]
                # Apply aggression/risk modifiers?
                value += (self.game_state.strategic_planner.aggression_level - 0.5) * 0.1
                return value
            else:
                 logging.warning(f"Combat simulation did not return expected dictionary: {simulation_results}")
                 return -0.1 # Default penalty if simulation failed

        # Fallback if resolver or simulate_combat not found
        logging.warning("Combat simulation not available, using basic evaluation.")
        if not attackers: return -0.2 # Penalize not attacking if possible
        power = sum(getattr(gs._safe_get_card(a),'power',0) for a in attackers)
        return power * 0.1 # Simple evaluation
        
    def find_optimal_attack(self):
        """
        Find the optimal combination of attackers using strategic evaluation and combat simulation.
        """
        gs = self.game_state
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        
        # Get valid attackers
        potential_attackers = [cid for cid in me["battlefield"] if self.is_valid_attacker(cid)]

        if not potential_attackers: return []

        # Use the combat resolver's specialized method if available
        if hasattr(gs, 'combat_resolver') and hasattr(gs.combat_resolver, 'find_optimal_attack'):
            return gs.combat_resolver.find_optimal_attack(potential_attackers)

        # Fallback: Simplified evaluation if resolver method unavailable
        logging.warning("Using fallback find_optimal_attack.")
        import itertools
        best_combo, best_value = [], -float('inf')

        # Generate combinations (limit complexity)
        max_attackers = min(len(potential_attackers), 6) # Limit combinations
        for i in range(1, max_attackers + 1):
            for combo in itertools.combinations(potential_attackers, i):
                 # Evaluate this combination (using simplified eval here)
                 combo_power = sum(getattr(gs._safe_get_card(cid),'power',0) for cid in combo)
                 # Simple eval: just total power
                 value = combo_power
                 if value > best_value:
                      best_value = value; best_combo = list(combo)

        # Always consider attacking with all valid attackers if feasible
        if len(potential_attackers) <= 6:
             value = sum(getattr(gs._safe_get_card(cid),'power',0) for cid in potential_attackers)
             if value > best_value: best_combo = potential_attackers[:]

        logging.debug(f"Fallback optimal attack: {len(best_combo)} attackers with value {best_value:.2f}")
        return best_combo
        
    def is_valid_attacker(self, card_id):
        """Determine if a creature can attack, incorporating dynamic restrictions. Uses centralized keyword check."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        player = gs.get_card_controller(card_id)

        # Basic checks
        if not card or not player or card_id not in player.get("battlefield", []): return False
        if 'creature' not in getattr(card, 'card_types', []): return False

        if "can't attack or block unless there are four or more card types" in getattr(card, 'oracle_text', '').lower():
            types_found = {str(card_type).lower() for grave_id in player.get('graveyard', [])
                           for card_type in getattr(gs._safe_get_card(grave_id), 'card_types', [])}
            types_found.difference_update({'token', 'unknown'})
            if len(types_found) < 4:
                return False

        # Tapped check
        if card_id in player.get("tapped_permanents", set()): return False

        # Phased-out permanents are treated as though they do not exist.  They
        # can remain represented in the battlefield zone while phased out, so
        # battlefield membership alone is not sufficient here.
        if card_id in getattr(gs, "phased_out", set()): return False

        # Summoning Sickness check (using central keyword check for haste)
        if card_id in player.get("entered_battlefield_this_turn", set()) and not self._has_keyword(card, "haste"):
             return False

        # Defender check (using central keyword check)
        if (self._has_keyword(card, "defender")
                and not gs.has_defender_attack_permission(card_id)):
            return False

        # --- Check Layer System Effects for 'cant_attack' ---
        cant_attack = False
        if hasattr(gs, 'layer_system') and gs.layer_system:
            # This assumes LayerSystem calculates the 'keywords' array correctly,
            # including 'cant_attack' as a negative ability/restriction.
            # Need a consistent way to represent this. Let's assume 'cant_attack' is a pseudo-keyword.
            try:
                if self._has_keyword(card, "cant_attack"): # Check the effective keywords
                    cant_attack = True
            except Exception as e:
                 logging.warning(f"Error checking LayerSystem cant_attack effect: {e}")
        # Direct check if LayerSystem doesn't use keyword array for this
        # elif hasattr(gs, 'layer_system') and hasattr(gs.layer_system, 'has_effect'):
        #     if gs.layer_system.has_effect(card_id, 'cant_attack'): cant_attack = True

        if cant_attack:
            logging.debug(f"Attacker {card.name} invalid: 'Can't Attack' effect active.")
            return False

        # Check other game state restrictions if applicable (e.g., Ghostly Prison effect)
        # if gs.has_attack_restriction(player, card_id): return False # Example hook

        return True # All checks passed
    
    def _initialize_combat_state_tracking(self):
        """Initialize or reset tracking dictionaries for combat state."""
        gs = self.game_state
        # Use setattr to ensure attributes are created if they don't exist
        attrs_defaults = {
            "current_attackers": [],
            "current_block_assignments": {},
            "planeswalker_attack_targets": {},
            "battle_attack_targets": {},
            "planeswalker_protectors": {},
            "first_strike_ordering": {},
            "blocked_attackers_this_combat": set(),
            "first_strike_damage_participants": set(),
            "first_strike_damage_dealt": False,
            "combat_damage_dealt": False
        }
        for attr, default in attrs_defaults.items():
            if not hasattr(gs, attr):
                setattr(gs, attr, default)
            elif attr == "current_block_assignments": # Ensure nested dicts are cleared
                getattr(gs, attr).clear()
            elif isinstance(default, list): # Clear lists
                 getattr(gs, attr).clear()
            elif isinstance(default, set):
                 getattr(gs, attr).clear()
            elif isinstance(default, dict): # Clear dicts
                 getattr(gs, attr).clear()
            elif isinstance(default, bool): # Reset flags
                 setattr(gs, attr, default)
        logging.debug("Combat state tracking reset/initialized")
    
    def handle_first_strike_order(self, param=None, context=None, **kwargs):
            """Set the damage assignment order for first strike combat."""
            gs = self.game_state
            player = gs.p1 if gs.agent_is_p1 else gs.p2 # The ATTACKING player assigns damage order

            for attacker_id, blockers in gs.current_block_assignments.items():
                if len(blockers) <= 1: continue # No order needed
                attacker_card = gs._safe_get_card(attacker_id)
                if not attacker_card: continue

                # Get player choice for order (AI needs to provide this)
                # Placeholder: Default order (e.g., by toughness asc)
                ordered_blockers = sorted(blockers, key=lambda bid: getattr(gs._safe_get_card(bid), 'toughness', 0))

                gs.first_strike_ordering[attacker_id] = ordered_blockers # Store chosen order
                logging.debug(f"Set damage assignment order for {attacker_card.name}: {[gs._safe_get_card(bid).name for bid in ordered_blockers]}")

            return True # Succeeded in setting (or determining no need for) orders

    def handle_assign_combat_damage(self, param=None, context=None, **kwargs):
        """Handle assignment of combat damage."""
        damage_assignments = param # Map param to expected argument
        gs = self.game_state
        if not gs.combat_resolver: return False
        damage_phase = gs.phase

        if damage_assignments:
             # Apply manual assignments
             if hasattr(gs.combat_resolver, 'assign_manual_combat_damage'):
                  success = gs.combat_resolver.assign_manual_combat_damage(damage_assignments)
             else:
                  logging.warning("Manual damage assignment not supported by resolver.")
                  success = False # Fallback: Fail if resolver missing function
        else:
             # CR 510.1c: with 2+ blockers on any of the agent's attackers,
             # the damage assignment order is the attacking player's CHOICE.
             # Open the ordering choice and defer resolution; ordering
             # completion (blocker_order_chosen) finishes combat itself.
             if self.begin_blocker_order_choice():
                 return True
             # Auto-resolve damage if no ordering choice is needed
             _ = gs.combat_resolver.resolve_combat() # Resolve combat automatically
             success = self._finish_damage_step(damage_phase)
             if not success:
                  logging.error(
                      "Combat resolver returned without marking the current "
                      "damage step complete.")
             return success

        if not success:
            return False
        return self._finish_damage_step(damage_phase)

    def _finish_damage_step(self, damage_phase):
        """Validate resolver completion and enter the next combat step."""
        gs = self.game_state

        def choice_pending():
            # A damage trigger may have opened a decision mid-resolution (for
            # example an as-enters choice for a land fetched by a combat
            # trigger).  Overwriting gs.phase would orphan that context: the
            # action mask routes choices by phase, so the choice would become
            # unreachable and the episode a non-progressing PASS loop (the
            # round-7.91 run died exactly this way at turn 18).  Route the
            # combat transition through previous_priority_phase instead so
            # completing the choice lands in the next combat step.
            return (getattr(gs, "choice_context", None)
                    or getattr(gs, "targeting_context", None)
                    or getattr(gs, "sacrifice_context", None)) \
                and gs.phase in (gs.PHASE_CHOOSE, gs.PHASE_TARGETING,
                                 gs.PHASE_SACRIFICE)

        if damage_phase == gs.PHASE_FIRST_STRIKE_DAMAGE:
            if not getattr(gs, "first_strike_damage_dealt", False):
                return False
            if choice_pending():
                gs.previous_priority_phase = gs.PHASE_COMBAT_DAMAGE
                return True
            # This is the actual between-damage-steps priority window.
            gs._advance_phase()
            return gs.phase == gs.PHASE_COMBAT_DAMAGE

        if not getattr(gs, "combat_damage_dealt", False):
            return False
        gs._empty_mana_pools()
        if choice_pending():
            gs.previous_priority_phase = gs.PHASE_END_OF_COMBAT
            return True
        gs.phase = gs.PHASE_END_OF_COMBAT
        gs.priority_player = gs._get_active_player()
        gs.priority_pass_count = 0
        return True

    def begin_blocker_order_choice(self):
        """Open an ``order_blockers`` choice under CR 510.1c.

        Either seat owns this decision. The environment routes a non-agent
        chooser through the installed opponent policy (or the explicit
        scripted baseline), just like simultaneous trigger ordering.
        """
        gs = self.game_state
        if getattr(gs, 'choice_context', None):
            return False
        if not hasattr(gs, 'first_strike_ordering'):
            gs.first_strike_ordering = {}
        needs_order = []
        for attacker_id, blockers in gs.current_block_assignments.items():
            valid = [b for b in blockers if gs.find_card_location(b)[1] == 'battlefield']
            if len(valid) < 2 or attacker_id in gs.first_strike_ordering:
                continue
            needs_order.append((attacker_id, valid))
        if not needs_order:
            return False
        first_attacker, first_blockers = needs_order[0]
        chooser = gs.get_card_controller(first_attacker)
        if chooser is None:
            return False
        if gs.phase not in [gs.PHASE_CHOOSE, gs.PHASE_TARGETING, gs.PHASE_SACRIFICE]:
            gs.previous_priority_phase = gs.phase
        gs.phase = gs.PHASE_CHOOSE
        gs.choice_context = {
            'type': 'order_blockers',
            'player': chooser,
            'attacker_id': first_attacker,
            'pending': list(first_blockers),
            'ordered': [],
            'remaining_attackers': [a for a, _ in needs_order[1:]],
            'source_id': first_attacker,
            'resolved': False,
            'damage_phase': (
                gs.previous_priority_phase
                if gs.previous_priority_phase in (
                    gs.PHASE_FIRST_STRIKE_DAMAGE,
                    gs.PHASE_COMBAT_DAMAGE)
                else gs.PHASE_COMBAT_DAMAGE),
        }
        gs.priority_pass_count = 0
        gs.priority_player = chooser
        logging.debug(
            f"CR 510.1c: ordering choice opened for attacker {first_attacker} "
            f"({len(first_blockers)} blockers).")
        return True

    def blocker_order_chosen(self, index):
        """Apply one ordering pick: pending[index] takes damage next. When the
        current attacker's order completes, move to the next multi-blocked
        attacker, or close the choice and finish combat resolution. Returns
        True if the pick was valid."""
        gs = self.game_state
        ctx = getattr(gs, 'choice_context', None)
        if not ctx or ctx.get('type') != 'order_blockers':
            logging.warning("blocker_order_chosen called without an order_blockers context.")
            return False
        pending = ctx.get('pending', [])
        if not isinstance(index, int) or not (0 <= index < len(pending)):
            logging.warning(f"blocker_order_chosen: invalid index {index} for {len(pending)} pending.")
            return False
        ctx['ordered'].append(pending.pop(index))
        if len(pending) == 1:
            ctx['ordered'].append(pending.pop(0))
        if pending:
            return True
        # This attacker's order is complete.
        gs.first_strike_ordering[ctx['attacker_id']] = list(ctx['ordered'])
        remaining = ctx.get('remaining_attackers') or []
        while remaining:
            next_attacker = remaining.pop(0)
            valid = [b for b in gs.current_block_assignments.get(next_attacker, [])
                     if gs.find_card_location(b)[1] == 'battlefield']
            if len(valid) >= 2 and next_attacker not in gs.first_strike_ordering:
                ctx.update({'attacker_id': next_attacker, 'source_id': next_attacker,
                            'pending': list(valid), 'ordered': [],
                            'remaining_attackers': remaining})
                return True
        # All orders chosen: close the choice, restore the phase, and finish
        # the deferred combat resolution (mirrors handle_assign_combat_damage's
        # tail).
        gs.choice_context = None
        prev = ctx.get('damage_phase') or getattr(
            gs, 'previous_priority_phase', None)
        if prev is not None:
            gs.phase = prev
        _ = gs.combat_resolver.resolve_combat()
        if not self._finish_damage_step(prev):
            logging.error(
                "Combat resolver failed after damage-assignment ordering.")
            return False
        return True

    def handle_attack_battle(self, param=None, context=None, **kwargs):
        """Assign last declared attacker to target a specific battle. Param is battle index (0-4)."""
        battle_target_idx = param # Map param to expected argument
        gs = self.game_state
        # Check if it's the right phase and if attackers have been declared
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS or not gs.current_attackers:
            logging.warning("Cannot assign battle target outside Declare Attackers phase or with no attackers.")
            return False 

        if battle_target_idx is None:
            logging.error("handle_attack_battle called with None param.")
            return False

        # Combat targets belong to the defending (non-active) player.  Using
        # the fixed agent seat here made scripted/P2 policy actions inspect the
        # wrong battlefield.
        opponent = gs._get_non_active_player()
        # Get battles relative to opponent's battlefield
        opponent_battles = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"])
                            if gs._safe_get_card(cid) and 'battle' in getattr(gs._safe_get_card(cid), 'type_line', '')]

        # Parameter battle_target_idx is 0-4, maps to index within opponent_battles list
        if 0 <= battle_target_idx < len(opponent_battles):
            # Find the absolute battlefield index and card ID of the target battle
            abs_bf_idx, battle_id = opponent_battles[battle_target_idx] 

            # --- Assign Attacker Rule ---
            # Rule: Assume the *last* creature added to gs.current_attackers is the one choosing this target.
            if not gs.current_attackers:
                logging.warning("No attacker declared before assigning battle target.")
                return False 
            attacker_id = gs.current_attackers[-1]
            attacker_card = gs._safe_get_card(attacker_id)
            if not attacker_card: return False 

            # Ensure the battle_attack_targets dict exists
            if not hasattr(gs, 'battle_attack_targets'): gs.battle_attack_targets = {}
            if gs.battle_attack_targets.get(attacker_id) == battle_id:
                return False

            # Remove any previous target assignment for this attacker
            if attacker_id in gs.battle_attack_targets: del gs.battle_attack_targets[attacker_id]
            if hasattr(gs, 'planeswalker_attack_targets') and attacker_id in gs.planeswalker_attack_targets: del gs.planeswalker_attack_targets[attacker_id]

            # Assign attacker to battle
            gs.battle_attack_targets[attacker_id] = battle_id
            battle_card = gs._safe_get_card(battle_id)
            logging.debug(f"Attacker {attacker_card.name} now targeting Battle {battle_card.name}")
            return True 
        else:
            logging.warning(f"Invalid battle target index {battle_target_idx}. Available battles: {len(opponent_battles)}")
            return False 

    # --- Helpers for finding targets based on identifiers ---
    def _find_planeswalker_target(self, pw_identifier):
        gs = self.game_state
        pw_targets_on_stack = getattr(gs, 'planeswalker_attack_targets', {})

        target_pw_id = None
        attacked_targets = list(dict.fromkeys(pw_targets_on_stack.values()))
        # Card IDs are integers in the fixture database, so distinguish a
        # direct ID from a relative option index by checking the live mapping
        # first.
        if pw_identifier in attacked_targets:
            target_pw_id = pw_identifier
        elif isinstance(pw_identifier, int) and 0 <= pw_identifier < len(attacked_targets):
            target_pw_id = attacked_targets[pw_identifier]

        # Find attacker targeting this PW ID
        if target_pw_id is not None:
             for atk_id, target_pw in pw_targets_on_stack.items():
                  if target_pw == target_pw_id:
                       return target_pw_id, atk_id
        return None, None

    def _find_battle_target(self, battle_identifier):
        gs = self.game_state
        battle_targets_on_stack = getattr(gs, 'battle_attack_targets', {})

        target_battle_id = None
        attacked_targets = list(dict.fromkeys(battle_targets_on_stack.values()))
        if battle_identifier in attacked_targets:
            target_battle_id = battle_identifier
        elif isinstance(battle_identifier, int) and 0 <= battle_identifier < len(attacked_targets):
            target_battle_id = attacked_targets[battle_identifier]

        # Find attacker targeting this Battle ID
        if target_battle_id is not None:
             for atk_id, target_battle in battle_targets_on_stack.items():
                  if target_battle == target_battle_id:
                       return target_battle_id, atk_id
        return None, None

    # Helper to find a permanent ID from index or string ID
    def _find_permanent_id(self, player, identifier):
        """Finds a permanent ID on the player's battlefield using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["battlefield"]):
                  return player["battlefield"][identifier]
        elif isinstance(identifier, str):
             # Check if it's a direct ID
             if identifier in player["battlefield"]:
                  return identifier
             # Could potentially add lookup by name here if needed, but ID/index preferred
        return None

    # Helper to find a card ID in hand from index or ID string
    def _find_card_in_hand(self, player, identifier):
        """Finds a card ID in the player's hand using index or ID string."""
        if isinstance(identifier, int):
             if 0 <= identifier < len(player["hand"]):
                  return player["hand"][identifier]
        elif isinstance(identifier, str):
             if identifier in player["hand"]:
                  return identifier
        return None

    def handle_ninjutsu(self, param=None, context=None, **kwargs):
        """Handle the ninjutsu mechanic. Expects ('ninja_identifier', 'attacker_identifier') in context."""
        gs = self.game_state
        player = gs._get_active_player()
        if context is None: context = {}

        if (gs.priority_player is not player
                or gs.phase not in (
                    gs.PHASE_DECLARE_BLOCKERS,
                    gs.PHASE_FIRST_STRIKE_DAMAGE,
                    gs.PHASE_COMBAT_DAMAGE)
                or (gs.phase == gs.PHASE_FIRST_STRIKE_DAMAGE
                    and getattr(gs, "first_strike_damage_dealt", False))
                or (gs.phase == gs.PHASE_COMBAT_DAMAGE
                    and getattr(gs, "combat_damage_dealt", False))):
            logging.warning("Ninjutsu is not available in the current combat window.")
            return False

        # --- Get Parameters from Context ---
        # Assume context keys like 'ninja_hand_idx', 'attacker_bf_idx' are provided if param not used.
        # Use descriptive keys: 'ninja_identifier' and 'attacker_identifier' (can be index or ID).
        ninja_identifier = context.get('ninja_identifier')
        attacker_identifier = context.get('attacker_identifier')

        # Fallback logic using param if context keys are missing - LESS ROBUST
        # Assumes param contains a tuple or other structure if used this way.
        if ninja_identifier is None and attacker_identifier is None and isinstance(param, tuple) and len(param) == 2:
            ninja_identifier, attacker_identifier = param
            logging.warning("Using 'param' for Ninjutsu identifiers - context preferred.")

        if ninja_identifier is None or attacker_identifier is None:
            logging.error(f"Ninjutsu handler missing parameters 'ninja_identifier' or 'attacker_identifier' in context: {context} / param: {param}")
            return False

        # --- Validate Ninja ---
        ninja_id = self._find_card_in_hand(player, ninja_identifier)
        if not ninja_id: logging.warning(f"Invalid ninja identifier: {ninja_identifier}."); return False
        ninja_card = gs._safe_get_card(ninja_id)
        # Check using central keyword check now
        if not ninja_card or not self._has_keyword(ninja_card, "ninjutsu"):
            logging.warning(f"Card {getattr(ninja_card, 'name', 'N/A')} lacks Ninjutsu.")
            return False

        # --- Validate Attacker ---
        attacker_id = self._find_permanent_id(player, attacker_identifier)
        if attacker_id is None: logging.warning(f"Invalid attacker identifier: {attacker_identifier}."); return False
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card:
            logging.warning(f"Attacker card not found for ID {attacker_id}"); return False
        if attacker_id not in getattr(gs, 'current_attackers', []): # Check against gs list
            logging.warning(f"Selected permanent {attacker_card.name} is not a currently declared attacker."); return False
        # Check if unblocked
        if (getattr(gs, 'current_block_assignments', {}).get(attacker_id)
                or attacker_id in getattr(
                    gs, 'blocked_attackers_this_combat', set())):
            logging.warning("Attacker is blocked, cannot use Ninjutsu."); return False

        # --- Pay Cost ---
        ninjutsu_cost_str = self._get_ninjutsu_cost_str(ninja_card)
        if not ninjutsu_cost_str or not self._can_afford_cost_string(player, ninjutsu_cost_str):
             logging.warning(f"Cannot pay Ninjutsu cost {ninjutsu_cost_str}."); return False
        if not hasattr(gs, 'mana_system') or not gs.mana_system or not gs.mana_system.pay_mana_cost(player, ninjutsu_cost_str):
             logging.warning(f"Failed to pay Ninjutsu cost {ninjutsu_cost_str}.")
             # Need mana system rollback? Assume cost failed cleanly for now.
             return False # Payment failed

        # --- Pay the nonmana activation cost and put Ninjutsu on the stack ---
        # The returned creature's defender is locked in when the ability is
        # activated. The Ninja itself stays hidden in hand until resolution,
        # giving both players the required response window.
        pw_targets = getattr(gs, 'planeswalker_attack_targets', {})
        battle_targets = getattr(gs, 'battle_attack_targets', {})
        attack_target_kind = None
        attack_target_id = None
        if attacker_id in pw_targets:
            attack_target_kind = "planeswalker"
            attack_target_id = pw_targets.get(attacker_id)
        elif attacker_id in battle_targets:
            attack_target_kind = "battle"
            attack_target_id = battle_targets.get(attacker_id)

        success_return = gs.move_card(attacker_id, player, "battlefield", player, "hand", cause="ninjutsu_return")
        if not success_return: logging.error("Failed to return attacker for Ninjutsu."); return False

        if attacker_id in gs.current_attackers:
            gs.current_attackers.remove(attacker_id)
        gs.first_strike_damage_participants.discard(attacker_id)
        gs.blocked_attackers_this_combat.discard(attacker_id)
        gs.current_block_assignments.pop(attacker_id, None)
        pw_targets.pop(attacker_id, None)
        battle_targets.pop(attacker_id, None)

        ability = NinjutsuStackAbility(
            ninja_id, attacker_id, attack_target_kind, attack_target_id,
            source_zone_generation=getattr(
                ninja_card, "_zone_change_generation", None))
        ability.source_card = ninja_card
        gs.add_to_stack("ABILITY", ninja_id, player, {
            "ability": ability,
            "effect_text": ability.effect_text,
            "ninjutsu": True,
            "returned_attacker_id": attacker_id,
        })
        logging.info(
            "Ninjutsu activated: %s returned as a cost; %s remains in hand "
            "until the ability resolves.", attacker_card.name, ninja_card.name)
        return True
    
    def handle_declare_attackers_done(self, param=None, context=None, **kwargs):
        """Handle the end of the declare attackers phase."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS:
             logging.warning(f"Tried to end Declare Attackers in phase {gs.phase}")
             return False

        # CR 508.1: locking in attackers is when attack triggers fire. This
        # also populates gs.attackers_this_turn, which was initialized and
        # read (Boast legality, dead-creature observations) but never written
        # before July 2026 -- attack triggers themselves had no caller either.
        if not hasattr(gs, 'attackers_this_turn') or gs.attackers_this_turn is None:
            gs.attackers_this_turn = set()
        for attacker_id in list(getattr(gs, 'current_attackers', [])):
            attacker = gs._safe_get_card(attacker_id)
            controller = gs.get_card_controller(attacker_id)
            if (attacker and controller
                    and not self._has_keyword(attacker, "vigilance")):
                # CR 508.1f: tapping is part of declaring the creature as an
                # attacker, before attack triggers are put on the stack.
                gs.tap_permanent(attacker_id, controller)
            first_attack = attacker_id not in gs.attackers_this_turn
            gs.attackers_this_turn.add(attacker_id)
            if hasattr(gs, 'ability_handler') and gs.ability_handler:
                gs.ability_handler.handle_attack_triggers(
                    attacker_id, extra_context={"first_attack_this_turn": first_attack})

        gs._empty_mana_pools()
        gs.phase = gs.PHASE_DECLARE_BLOCKERS
        gs.priority_player = gs._get_non_active_player() # Priority to blocker
        gs.priority_pass_count = 0
        logging.debug(f"Ended Declare Attackers. Priority to {gs.priority_player['name']} in Declare Blockers.")
        return True
    
    def handle_declare_blockers_done(self, param=None, context=None, **kwargs):
        """Handle the end of the declare blockers phase."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
             logging.warning(f"Tried to end Declare Blockers in phase {gs.phase}")
             return False

        # Sequential declaration actions can be separated by state-based
        # changes. Drop blockers that have left combat before validating the
        # completed declaration; mask generation uses the same live view.
        gs.current_block_assignments = self._live_block_assignments()
        incomplete_menace = self._incomplete_menace_attacker()
        if incomplete_menace is not None:
            attacker_card = gs._safe_get_card(incomplete_menace)
            logging.warning(
                "Illegal block assignment: %s has menace and only one blocker.",
                getattr(attacker_card, 'name', incomplete_menace))
            return False

        gs.blocked_attackers_this_combat = {
            attacker_id
            for attacker_id, blockers in gs.current_block_assignments.items()
            if blockers
        }

        # Blocking and becoming blocked are declaration events, not damage
        # events. Dispatch them once the complete declaration has been proven
        # legal so printed triggers see the final set of blockers.
        controllers_that_blocked = set()
        for attacker_id, blockers in gs.current_block_assignments.items():
            if not blockers:
                continue
            attacker_controller = gs.get_card_controller(attacker_id)
            gs.trigger_ability(attacker_id, "BECOMES_BLOCKED", {
                "attacker_id": attacker_id,
                "blocker_ids": list(blockers),
                "blocker_count": len(blockers),
                "defending_player": gs._get_non_active_player(),
                "controller": attacker_controller,
            })
            for blocker_id in blockers:
                blocker_controller = gs.get_card_controller(blocker_id)
                first_for_controller = \
                    id(blocker_controller) not in controllers_that_blocked
                gs.trigger_ability(blocker_id, "BLOCKS", {
                    "attacker_id": attacker_id,
                    "blocker_id": blocker_id,
                    "blocker_ids": list(blockers),
                    "blocker_count": len(blockers),
                    "first_block_for_controller": first_for_controller,
                    "defending_player": gs._get_non_active_player(),
                    "controller": blocker_controller,
                })
                controllers_that_blocked.add(id(blocker_controller))

        # Determine if First Strike combat step is needed
        first_strike_participants = set()
        combatants = gs.current_attackers[:]
        for blockers in gs.current_block_assignments.values(): combatants.extend(blockers)
        for cid in combatants:
             card = gs._safe_get_card(cid)
             if card and (self._has_keyword(card, "first strike") or self._has_keyword(card, "double strike")):
                  first_strike_participants.add(cid)
        gs.first_strike_damage_participants = first_strike_participants
        needs_first_strike_step = bool(first_strike_participants)

        if needs_first_strike_step:
             gs._empty_mana_pools()
             gs.phase = gs.PHASE_FIRST_STRIKE_DAMAGE
             logging.debug("Ended Declare Blockers. Moving to First Strike Damage.")
        else:
             gs._empty_mana_pools()
             gs.phase = gs.PHASE_COMBAT_DAMAGE
             logging.debug("Ended Declare Blockers. Moving to Combat Damage (no first strike).")

        gs.first_strike_damage_dealt = False
        gs.combat_damage_dealt = False # Reset flag before damage steps
        gs.priority_player = gs._get_active_player() # Priority back to active player for damage step
        gs.priority_pass_count = 0
        return True

    def _incomplete_menace_attacker(self):
        """Return an attacker with an illegal one-blocker menace assignment.

        Blocking restrictions apply to the completed declaration, while this
        action API constructs that declaration over several policy actions.
        Keeping this predicate shared by mask generation and execution prevents
        a finish action that the executor must reject.

        The zone model intentionally permits repeated canonical card IDs for
        physical copies, so count list occurrences rather than distinct IDs.
        """
        gs = self.game_state
        assignments = self._live_block_assignments()
        for attacker_id, blockers in list(assignments.items()):
            attacker_card = gs._safe_get_card(attacker_id)
            if (attacker_card and self._has_keyword(attacker_card, "menace")
                    and len(blockers) == 1):
                return attacker_id
        return None

    def _live_block_assignments(self):
        """Return assignments backed by physical defender battlefield slots.

        Canonical card IDs can repeat, so consume occurrence counts instead of
        converting the battlefield to a set. Tapped blockers remain in combat;
        permanents that left the battlefield, phased out, or stopped being
        creatures do not.
        """
        from collections import Counter

        gs = self.game_state
        defender = gs._get_non_active_player()
        remaining = Counter(defender.get('battlefield', []))
        live = {}
        for attacker_id, blockers in getattr(
                gs, 'current_block_assignments', {}).items():
            if attacker_id not in getattr(gs, 'current_attackers', []):
                continue
            kept = []
            for blocker_id in blockers:
                blocker = gs._safe_get_card(blocker_id)
                if (remaining[blocker_id] <= 0 or not blocker
                        or 'creature' not in getattr(
                            blocker, 'card_types', [])
                        or blocker_id in getattr(gs, 'phased_out', set())):
                    continue
                remaining[blocker_id] -= 1
                kept.append(blocker_id)
            if kept:
                live[attacker_id] = kept
        return live

    def _blocking_attacker_for_slot(self, player, battlefield_index,
                                    assignments=None):
        """Map one physical battlefield occurrence to its assignment.

        Repeated canonical IDs are separate physical cards in list zones. The
        first matching battlefield occurrence corresponds to the first matching
        assignment occurrence, the second to the second, and so on.
        """
        battlefield = player.get('battlefield', [])
        if not (0 <= battlefield_index < len(battlefield)):
            return None
        blocker_id = battlefield[battlefield_index]
        occurrence_index = sum(
            1 for candidate_id in battlefield[:battlefield_index]
            if candidate_id == blocker_id)
        matching_assignments = []
        source_assignments = (
            self._live_block_assignments()
            if assignments is None else assignments)
        for attacker_id, blockers in source_assignments.items():
            matching_assignments.extend(
                attacker_id for assigned_id in blockers
                if assigned_id == blocker_id)
        if occurrence_index < len(matching_assignments):
            return matching_assignments[occurrence_index]
        return None

    def _can_finish_block_declaration(self):
        """Whether the current sequential block declaration is complete/legal."""
        return self._incomplete_menace_attacker() is None

    def _ordinary_block_targets(self, blocker_id):
        """Legal targets for one sequential BLOCK action.

        A menace attacker with no blockers must be blocked atomically by the
        multi-block action. Exposing a first ordinary blocker creates an
        illegal intermediate state and can strand a monotonic policy when no
        second blocker exists. A second ordinary blocker remains available for
        recovery from a pre-existing partial declaration.
        """
        gs = self.game_state
        assignments = self._live_block_assignments()
        targets = []
        for attacker_id in getattr(gs, 'current_attackers', []):
            if not self._can_block(blocker_id, attacker_id):
                continue
            attacker_card = gs._safe_get_card(attacker_id)
            if (attacker_card and self._has_keyword(attacker_card, "menace")
                    and not assignments.get(attacker_id)
                    and not self._can_start_sequential_menace_block(
                        attacker_id)):
                continue
            targets.append(attacker_id)
        return targets

    def _can_start_sequential_menace_block(self, attacker_id):
        """Whether menace must use sequential blocking for this attacker.

        The public map has atomic multi-block slots only for attacker indices
        0-9. For a later attacker, permit a first ordinary blocker only when at
        least two independently addressable blocker slots can complete the
        declaration. This preserves coverage without recreating the lone-
        blocker dead end fixed for the normal atomic range.
        """
        gs = self.game_state
        try:
            attacker_index = list(gs.current_attackers).index(attacker_id)
        except ValueError:
            return False
        if attacker_index < 10:
            return False

        from collections import Counter

        assigned_remaining = Counter(
            blocker_id
            for blockers in self._live_block_assignments().values()
            for blocker_id in blockers)
        defender = gs._get_non_active_player()
        available_count = 0
        # Slots beyond 19 are exposed through the paged action catalog and are
        # just as independently selectable as the fixed BLOCK actions.
        for candidate_id in defender.get('battlefield', []):
            if assigned_remaining[candidate_id] > 0:
                assigned_remaining[candidate_id] -= 1
                continue
            candidate = gs._safe_get_card(candidate_id)
            if (not candidate
                    or 'creature' not in getattr(candidate, 'card_types', [])
                    or candidate_id in defender.get('tapped_permanents', set())
                    or candidate_id in getattr(gs, 'phased_out', set())):
                continue
            if self._can_block(candidate_id, attacker_id):
                available_count += 1
        return available_count >= 2
    

    def handle_attack_planeswalker(self, param=None, context=None, **kwargs):
        """Handle attack targeting a planeswalker."""
        pw_target_idx = param # Map param to expected argument
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_ATTACKERS or not gs.current_attackers: return False
        
        if pw_target_idx is None:
             logging.error("handle_attack_planeswalker called with None param.")
             return False

        opponent = gs._get_non_active_player()
        opponent_planeswalkers = [(idx, cid) for idx, cid in enumerate(opponent["battlefield"])
                                   if gs._safe_get_card(cid) and 'planeswalker' in getattr(gs._safe_get_card(cid), 'card_types', [])]

        if 0 <= pw_target_idx < len(opponent_planeswalkers):
            abs_bf_idx, pw_id = opponent_planeswalkers[pw_target_idx]
            attacker_id = gs.current_attackers[-1] # Assign to last declared attacker
            if (getattr(gs, 'planeswalker_attack_targets', {}).get(
                    attacker_id) == pw_id):
                return False
            if not hasattr(gs, 'planeswalker_attack_targets'): gs.planeswalker_attack_targets = {}
            # One attacker has exactly one defender. Retargeting from a Battle
            # must clear the old assignment just as the Battle path clears a
            # planeswalker assignment.
            getattr(gs, 'battle_attack_targets', {}).pop(attacker_id, None)
            gs.planeswalker_attack_targets[attacker_id] = pw_id
            logging.debug(f"{gs._safe_get_card(attacker_id).name} now targeting PW {gs._safe_get_card(pw_id).name}")
            return True
        return False

    def handle_assign_multiple_blockers(self, param, context, **kwargs):
        """Handle assigning multiple blockers. Attacker index from PARAM, blocker identifiers from CONTEXT."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False # Ensure correct phase
        if context is None: context = {}

        attacker_idx = param # Param is the attacker index (0-9)
        if attacker_idx is None or not isinstance(attacker_idx, int) or not (0 <= attacker_idx < len(gs.current_attackers)):
            logging.error(f"Invalid or missing attacker index for multi-block: {attacker_idx}")
            return False
        attacker_id = gs.current_attackers[attacker_idx]
        attacker_card = gs._safe_get_card(attacker_id)
        if not attacker_card: return False

        # --- Get Blocker Identifiers from Context ---
        blocker_identifiers = context.get('blocker_identifiers') # List of indices or IDs
        if not blocker_identifiers or not isinstance(blocker_identifiers, list):
            logging.error("Missing or invalid 'blocker_identifiers' list in context for multi-block.")
            return False
        if len(set(blocker_identifiers)) != len(blocker_identifiers):
            logging.warning(
                "Multi-block context repeated one physical blocker slot.")
            return False

        # --- Validate Blockers ---
        player = gs._get_non_active_player() # Player controlling blockers
        valid_blocker_ids = []
        for identifier in blocker_identifiers:
            # Use helper to find ID from index or string ID
            blocker_id = self._find_permanent_id(player, identifier)
            if blocker_id is None:
                 logging.warning(f"Invalid blocker identifier {identifier} for multi-block.")
                 return False
            blocker_card = gs._safe_get_card(blocker_id)
            if not blocker_card: return False

            if not self._can_block(blocker_id, attacker_id):
                 logging.warning(f"Blocker {blocker_card.name} cannot block {attacker_card.name}")
                 return False
            valid_blocker_ids.append(blocker_id)

        if len(valid_blocker_ids) < 2:
            logging.warning("Must assign at least 2 valid blockers for ASSIGN_MULTIPLE_BLOCKERS action.")
            return False

        # Check Menace explicitly if needed (though _can_block might implicitly handle)
        if self._has_keyword(attacker_card, "menace") and len(valid_blocker_ids) < 2:
             logging.warning(f"Menace requires at least 2 blockers, only {len(valid_blocker_ids)} valid blockers assigned.")
             return False

        # --- Assign Block ---
        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        # Re-assigning the identical multi-block is a no-op; report failure so
        # the agent cannot farm the success reward by repeating the action
        # (seen as a 2000-step DECLARE_BLOCKERS stall in training).
        if gs.current_block_assignments.get(attacker_id) == valid_blocker_ids:
            logging.debug(
                f"Multi-block for {attacker_card.name} already assigned; redundant action ignored.")
            return False
        # Replace any existing single blocks for this attacker with the multi-block
        gs.current_block_assignments[attacker_id] = valid_blocker_ids

        blocker_names = [getattr(gs._safe_get_card(bid), 'name', bid) for bid in valid_blocker_ids]
        logging.info(f"Assigned multiple blockers ({', '.join(blocker_names)}) to {attacker_card.name}")
        return True
    

    def handle_defend_battle(self, param=None, context=None, **kwargs):
        """Assign a creature to block an attacker targeting a battle. Expects (battle_identifier, defender_identifier) in context."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS: return False
        if context is None: context = {}

        # --- Get Parameters from Context ---
        battle_identifier = context.get('battle_identifier') # Use consistent key
        defender_identifier = context.get('defender_identifier') # Use consistent key

        if battle_identifier is None or defender_identifier is None:
            logging.error(f"Defend Battle handler missing parameters in context: {context}")
            return False

        # --- Find Battle Being Attacked and the Attacker ---
        target_battle_id, attacker_id = self._find_battle_target(battle_identifier)
        if attacker_id is None:
            logging.warning(f"Battle {battle_identifier} not found or not being attacked.")
            return False

        # --- Find Defender ---
        player = gs._get_non_active_player()
        defender_id = self._find_permanent_id(player, defender_identifier)
        if defender_id is None:
             logging.warning(f"Invalid defender identifier {defender_identifier}.")
             return False

        # --- Validate Blocker ---
        if not self._can_block(defender_id, attacker_id):
            logging.warning(f"Defender {gs._safe_get_card(defender_id).name} cannot block attacker {gs._safe_get_card(attacker_id).name}")
            return False

        # --- Assign Block ---
        if attacker_id not in gs.current_block_assignments: gs.current_block_assignments[attacker_id] = []
        if defender_id not in gs.current_block_assignments[attacker_id]:
             gs.current_block_assignments[attacker_id].append(defender_id)
             logging.info(f"{gs._safe_get_card(defender_id).name} assigned to block {gs._safe_get_card(attacker_id).name} (defending Battle {gs._safe_get_card(target_battle_id).name})")
             return True
        logging.debug("Blocker already assigned to this attacker.")
        return False # Already assigned
    
    def _add_battle_attack_actions(self, player, valid_actions, set_valid_action):
        """Add actions for attacking battle cards."""
        gs = self.game_state
        
        # Only applicable in certain phases
        if (gs.phase != gs.PHASE_DECLARE_ATTACKERS
                or not getattr(gs, 'current_attackers', [])):
            return
            
        # Get opponent's battlefield
        opponent = gs.p2 if player == gs.p1 else gs.p1
        
        # Find battle cards on opponent's battlefield
        battle_cards = []
        for idx, card_id in enumerate(opponent["battlefield"]):
            if idx >= 5:  # Limit to 5 battle cards
                break
                
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'is_battle') and card.is_battle:
                battle_cards.append((idx, card_id, card))
        
        if not battle_cards:
            return  # No battle cards to attack
            
        # Get available untapped creatures
        available_creatures = []
        for idx, card_id in enumerate(player["battlefield"]):
            if idx >= 20:  # Limit to 20 creatures
                break
            if self.is_valid_attacker(card_id):
                available_creatures.append(
                    (idx, card_id, gs._safe_get_card(card_id)))
        
        # For each battle card, add action using indices 462-466 (for battle 0-4)
        for battle_idx, battle_id, battle_card in enumerate(battle_cards):
            if battle_idx >= 5: break  # Only handle 5 battles max
            
            # Use correct action index from ACTION_MEANINGS (462-466)
            action_idx = 462 + battle_idx
            
            # Battle info and damage potential
            battle_info = f" (Defense: {battle_card.defense})" if hasattr(battle_card, 'defense') else ""
            
            set_valid_action(action_idx, 
                f"ATTACK_BATTLE {battle_card.name}{battle_info}")
                
    def _add_attack_declaration_actions(self, player, opponent, valid_actions, set_valid_action):
        """Adds actions specific to the Declare Attackers step. (Called by ActionHandler)"""
        gs = self.game_state
        overflow_actions = []
        # Declare Attackers
        possible_attackers = []
        player_battlefield = player.get("battlefield", [])
        for i in range(len(player_battlefield)):
            try:
                card_id = player_battlefield[i]
                # ATTACK is a declaration action in the public policy API, not
                # an endlessly repeatable toggle.  Keeping an already-declared
                # creature's slot mask-valid let deterministic policies select
                # it again, remove it, and alternate forever between the same
                # two combat states.  Although the tabletop declaration is one
                # atomic choice, this sequential action interface intentionally
                # makes each selection monotonic until the declaration is done.
                if card_id in getattr(gs, 'current_attackers', []):
                    continue
                # Use internal validation which delegates back to GS/Layers etc.
                if self.is_valid_attacker(card_id):
                    card = gs._safe_get_card(card_id)
                    card_name = getattr(card, 'name', f'Creature {i}')
                    if i < 20:  # Direct policy slots 28-47.
                        set_valid_action(28 + i, f"ATTACK with {card_name}")
                    else:
                        overflow_actions.append({
                            "label": f"ATTACK with {card_name}",
                            # The exact battlefield occurrence is pinned in the
                            # context; action 28 supplies the public ATTACK
                            # dispatcher without pretending this is slot zero.
                            "action_index": 28,
                            "action_context": {
                                "battlefield_idx": i,
                                "card_id": card_id,
                            },
                        })
                    possible_attackers.append((i, card_id)) # Store index and ID
            except IndexError:
                logging.warning(f"Combat Handler: IndexError accessing battlefield for ATTACK at index {i}")
                break

        # Target-selection actions apply to the most recently declared
        # attacker.  Do not expose them before one exists: both public handlers
        # correctly reject that state, so the old mask violated its contract.
        if gs.current_attackers:
            # Add actions for attacking Planeswalkers (action indices 378-382, corrected from 373-377)
            opponent_planeswalkers = [(idx, card_id) for idx, card_id in enumerate(opponent.get("battlefield", []))
                                    if gs._safe_get_card(card_id) and 'planeswalker' in getattr(gs._safe_get_card(card_id), 'card_types', [])]
            for pw_rel_idx in range(min(len(opponent_planeswalkers), 5)): # PW relative index 0-4
                pw_abs_idx, pw_id = opponent_planeswalkers[pw_rel_idx]
                if getattr(gs, 'planeswalker_attack_targets', {}).get(
                        gs.current_attackers[-1]) == pw_id:
                    continue
                pw_card = gs._safe_get_card(pw_id)
                pw_name = getattr(pw_card, 'name', f'PW {pw_rel_idx}')
                set_valid_action(378 + pw_rel_idx, f"Target PLANESWALKER: {pw_name}")

            # Add actions for attacking Battles (action indices 462-466)
            opponent_battles = [(idx, card_id) for idx, card_id in enumerate(opponent.get("battlefield", []))
                                if gs._safe_get_card(card_id) and 'battle' in getattr(gs._safe_get_card(card_id), 'type_line', '')]
            for battle_rel_idx in range(min(len(opponent_battles), 5)): # Battle relative index 0-4
                battle_abs_idx, battle_id = opponent_battles[battle_rel_idx]
                if getattr(gs, 'battle_attack_targets', {}).get(
                        gs.current_attackers[-1]) == battle_id:
                    continue
                battle_card = gs._safe_get_card(battle_id)
                battle_name = getattr(battle_card, 'name', f'Battle {battle_rel_idx}')
                set_valid_action(462 + battle_rel_idx, f"Target BATTLE: {battle_name}")

        # Always allow finishing declaration if player has declared at least one action or no valid attacks
        # Corrected from 433 to 438 to match ACTION_MEANINGS
        set_valid_action(438, "Finish Declaring Attackers")
        return overflow_actions

    def _add_block_declaration_actions(self, player, valid_actions, set_valid_action):
        """Adds actions specific to the Declare Blockers step. (Called by ActionHandler)"""
        gs = self.game_state
        overflow_actions = []
        if not getattr(gs, 'current_attackers', []):
            set_valid_action(439, "Finish Declaring No Blockers")
            return overflow_actions

        player_battlefield = player.get("battlefield", [])
        possible_blockers = []
        live_assignments = self._live_block_assignments()
        for i in range(len(player_battlefield)):
            try:
                card_id = player_battlefield[i]
                card = gs._safe_get_card(card_id)
                if not card: continue

                card_name = getattr(card, 'name', f'Blocker {i}')
                blocking_attacker = self._blocking_attacker_for_slot(
                    player, i, live_assignments)
                if blocking_attacker is not None:
                    # Recovery must remain available even if the blocker was
                    # tapped or can no longer satisfy the original restriction.
                    attacker_card = gs._safe_get_card(blocking_attacker)
                    if (attacker_card
                            and self._has_keyword(attacker_card, "menace")
                            and len(live_assignments.get(
                                blocking_attacker, [])) == 1):
                        withdraw_context = {
                            "battlefield_idx": i,
                            "card_id": card_id,
                        }
                        if i < 20:
                            set_valid_action(
                                48 + i,
                                f"Withdraw incomplete menace block with {card_name}",
                                context=withdraw_context)
                        else:
                            overflow_actions.append({
                                "label": (
                                    "Withdraw incomplete menace block with "
                                    f"{card_name}"),
                                "action_index": 48,
                                "action_context": withdraw_context,
                            })
                    continue

                if ('creature' not in getattr(card, 'card_types', [])
                        or card_id in player.get("tapped_permanents", set())):
                    continue

                pairwise_targets = [
                    attacker_id for attacker_id in gs.current_attackers
                    if self._can_block(card_id, attacker_id)
                ]
                if pairwise_targets:

                    # Keep every physically indexed candidate for the atomic
                    # multi-block action, including duplicate canonical IDs.
                    possible_blockers.append((i, card_id))

                    ordinary_targets = self._ordinary_block_targets(card_id)
                    if ordinary_targets:
                        # Bind the exact target used to justify the mask. The
                        # executor previously chose a target independently,
                        # which could invalidate an otherwise legal mask slot.
                        def target_priority(attacker_id):
                            attacker = gs._safe_get_card(attacker_id)
                            has_menace = bool(
                                attacker
                                and self._has_keyword(attacker, "menace"))
                            assigned_count = len(
                                live_assignments.get(attacker_id, []))
                            # Complete a partial menace declaration first, then
                            # prioritize out-of-range menace that has no atomic
                            # action slot, before using the power heuristic.
                            return (
                                int(has_menace and assigned_count == 1),
                                int(has_menace and assigned_count == 0
                                    and self._can_start_sequential_menace_block(
                                        attacker_id)),
                                getattr(attacker, 'power', 0) or 0,
                            )

                        target_attacker_id = max(
                            ordinary_targets, key=target_priority)
                        block_context = {
                            "battlefield_idx": i,
                            "card_id": card_id,
                            "target_attacker_id": target_attacker_id,
                        }
                        if i < 20:  # Direct policy slots 48-67.
                            set_valid_action(
                                48 + i, f"Assign Block with {card_name}",
                                context=block_context)
                        else:
                            overflow_actions.append({
                                "label": f"Assign Block with {card_name}",
                                "action_index": 48,
                                "action_context": block_context,
                            })
            except IndexError:
                logging.warning(f"Combat Handler: IndexError accessing battlefield for BLOCK at index {i}")
                break

        # Assign multiple blockers action - corrected indices from 383-392 to match ACTION_MEANINGS
        if len(possible_blockers) >= 2:
            for atk_idx, attacker_id in enumerate(gs.current_attackers[:10]):
                attacker_card = gs._safe_get_card(attacker_id)
                attacker_name = getattr(attacker_card, 'name', f"Attacker {atk_idx}") if attacker_card else f"Attacker {atk_idx}"
                valid_multi_blockers_for_attacker = [
                    (bf_idx, blocker_id)
                    for bf_idx, blocker_id in possible_blockers
                    if self._can_block(blocker_id, attacker_id)
                ]
                # Skip when this exact multi-block is already assigned; the
                # handler rejects the redundant re-assignment, so masking it
                # valid only invites an action-selection loop.
                proposed_ids = [
                    blocker_id for _, blocker_id in
                    valid_multi_blockers_for_attacker]
                if live_assignments.get(attacker_id, []) == proposed_ids:
                    continue
                if len(valid_multi_blockers_for_attacker) >= 2:
                    # Corrected from 383 to match ACTION_MEANINGS
                    set_valid_action(
                        383 + atk_idx,
                        f"Assign Multiple Blockers to {attacker_name}",
                        context={
                            "blocker_identifiers": [
                                bf_idx for bf_idx, _ in
                                valid_multi_blockers_for_attacker
                            ]
                        })

        # The specialized labels share only one slot apiece, so expose one
        # deterministic legal pair and carry the exact identifiers required by
        # the handler.  Previously both actions were mask-valid with an empty
        # context and therefore guaranteed to fail.
        for attacker_id, pw_id in getattr(
                gs, 'planeswalker_attack_targets', {}).items():
            legal = [(bf_idx, blocker_id) for bf_idx, blocker_id in possible_blockers
                     if attacker_id in self._ordinary_block_targets(blocker_id)]
            if legal:
                blocker_idx, _ = legal[0]
                set_valid_action(
                    444, "Assign Blocker to protect Planeswalker",
                    context={"pw_identifier": pw_id,
                             "defender_identifier": blocker_idx})
                break

        for attacker_id, battle_id in getattr(
                gs, 'battle_attack_targets', {}).items():
            legal = [(bf_idx, blocker_id) for bf_idx, blocker_id in possible_blockers
                     if attacker_id in self._ordinary_block_targets(blocker_id)]
            if legal:
                blocker_idx, _ = legal[0]
                set_valid_action(
                    204, "Assign Blocker to defend Battle",
                    context={"battle_identifier": battle_id,
                             "defender_identifier": blocker_idx})
                break

        # The executor rejects a lone blocker on a menace attacker. Keep the
        # public mask aligned with that same completed-declaration predicate.
        if self._can_finish_block_declaration():
            set_valid_action(439, "Finish Declaring Blockers")
        return overflow_actions


    def _add_combat_damage_actions(self, player, valid_actions, set_valid_action):
        """Expose the mandatory combat-damage transition.

        Action 436 opens the real blocker-order choice when one is required,
        then resolves combat after the chooser completes it.  The legacy 435
        action silently sorted blockers and remained mask-valid afterward,
        which could create a deterministic policy loop instead of damage.
        """
        set_valid_action(436, "Resolve Combat Damage")


    def handle_protect_planeswalker(self, param=None, context=None, **kwargs):
        """Assign a creature to protect a planeswalker. Expects (pw_identifier, defender_identifier) in context."""
        gs = self.game_state
        if gs.phase != gs.PHASE_DECLARE_BLOCKERS:
            logging.warning("Cannot protect PW outside Declare Blockers phase.")
            return False
        if context is None: context = {}

        # --- Get Parameters from Context ---
        pw_identifier = context.get('pw_identifier') # Use consistent key
        defender_identifier = context.get('defender_identifier') # Use consistent key

        if pw_identifier is None or defender_identifier is None:
            logging.error(f"Protect Planeswalker handler missing parameters in context: {context}")
            return False

        # --- Find Planeswalker Being Attacked ---
        # Use _find_planeswalker_target helper which uses context identifiers
        target_pw_id, attacker_id = self._find_planeswalker_target(pw_identifier)
        if attacker_id is None:
            logging.warning(f"PW {pw_identifier} not found or not being attacked.")
            return False

        # --- Find Defender ---
        # Blocker is the non-agent player
        player = gs._get_non_active_player()
        defender_id = self._find_permanent_id(player, defender_identifier)
        if defender_id is None:
             logging.warning(f"Invalid defender identifier {defender_identifier}.")
             return False
        defender_card = gs._safe_get_card(defender_id)
        attacker_card = gs._safe_get_card(attacker_id)
        if not defender_card or not attacker_card: return False # Safety

        # --- Validate Blocker ---
        if not self._can_block(defender_id, attacker_id):
            logging.warning(f"Defender {defender_card.name} cannot block attacker {attacker_card.name}")
            return False

        # --- Assign Block ---
        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        if attacker_id not in gs.current_block_assignments: gs.current_block_assignments[attacker_id] = []
        if defender_id not in gs.current_block_assignments[attacker_id]:
             gs.current_block_assignments[attacker_id].append(defender_id)
             logging.info(f"{defender_card.name} assigned to block {attacker_card.name} (protecting PW {gs._safe_get_card(target_pw_id).name})")
             return True
        logging.debug("Blocker already assigned to this attacker.")
        return False # Already assigned
    
    # --- Mana Cost String Helpers ---
    def _get_equip_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
            # Match 'equip' followed optionally by em dash or hyphen, then cost
            match = re.search(r"equip\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
            if match: return match.group(1)
            match = re.search(r"equip\s*(\d+)\b", card.oracle_text.lower()) # Match digits only if bracketed cost not found
            if match: return f"{{{match.group(1)}}}"
        return None

    def _get_reconfigure_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"reconfigure\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"reconfigure\s*(\d+)\b", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
        return None

    def _get_ninjutsu_cost_str(self, card):
        if card and hasattr(card, 'oracle_text'):
             match = re.search(r"ninjutsu\s*(?:-|—)?\s*((?:\{[^}]+\})+)", card.oracle_text.lower())
             if match: return match.group(1)
             # Ninjutsu usually requires mana cost, less likely just digits
        return None

    def _get_fortify_cost_str(self, card):
         if card and hasattr(card, 'oracle_text'):
             match = re.search(r"fortify\s*(?:-|—)?\s*(\{.*?\})", card.oracle_text.lower())
             if match: return match.group(1)
             match = re.search(r"fortify\s*(\d+)\b", card.oracle_text.lower())
             if match: return f"{{{match.group(1)}}}"
         return None

    def _can_afford_cost_string(self, player, cost_string):
        """Helper to check affordability of a cost string."""
        gs = self.game_state
        if not hasattr(gs, 'mana_system') or not gs.mana_system:
            return sum(player.get("mana_pool", {}).values()) >= 1 if cost_string else True
        if not cost_string: return True
        return gs.mana_system.can_pay_mana_cost(player, cost_string)

    def _can_block(self, blocker_id, attacker_id):
        """Check if blocker_id can legally block attacker_id. Uses TargetingSystem."""
        gs = self.game_state
        blocker = gs._safe_get_card(blocker_id)
        blocker_controller = gs.get_card_controller(blocker_id)
        if (blocker_controller and blocker_id in blocker_controller.get(
                'suspected_permanents', set())):
            return False
        if (blocker and blocker_controller
                and "can't attack or block unless there are four or more card types" in getattr(blocker, 'oracle_text', '').lower()):
            types_found = {str(card_type).lower() for grave_id in blocker_controller.get('graveyard', [])
                           for card_type in getattr(gs._safe_get_card(grave_id), 'card_types', [])}
            types_found.difference_update({'token', 'unknown'})
            if len(types_found) < 4:
                return False
        # --- Check Phasing Status ---
        if hasattr(gs, 'phased_out'):
            if blocker_id in gs.phased_out:
                logging.debug(f"Blocker {blocker_id} cannot block: Phased Out.")
                return False
            if attacker_id in gs.phased_out: # Attacker phased out cannot be blocked
                 logging.debug(f"Attacker {attacker_id} cannot be blocked: Phased Out.")
                 # Is this check correct? Phased-out creatures can't attack. Validation happens earlier.
                 # Assume if attacker is attacking, it's phased in.
                 pass
        # --- End Phasing Check ---

        # Delegate to TargetingSystem preferred
        if hasattr(gs, 'targeting_system') and gs.targeting_system:
            if hasattr(gs.targeting_system, 'check_can_be_blocked'):
                try:
                    return gs.targeting_system.check_can_be_blocked(
                        attacker_id, blocker_id)
                except Exception as e:
                    logging.error(
                        f"Error checking block via TargetingSystem: {e}")
                    return False

        # --- Fallback logic (without Banding interaction) ---
        logging.warning("Using basic _can_block fallback in CombatActionHandler.")
        # ... (keep existing fallback logic, but Banding isn't handled here) ...
        return True


        # --- Fallback logic ---
        logging.warning("Using basic _can_block fallback in CombatActionHandler.")
        blocker = gs._safe_get_card(blocker_id); attacker = gs._safe_get_card(attacker_id)
        if not blocker or not attacker: return False
        if 'creature' not in getattr(blocker, 'card_types', []): return False # Must be creature
        if blocker_id in getattr(gs.get_card_controller(blocker_id), "tapped_permanents", set()): return False # Must be untapped

        # Use central _has_keyword for evasion checks
        if self._has_keyword(attacker, "flying") and not (self._has_keyword(blocker, "flying") or self._has_keyword(blocker, "reach")): return False
        if self._has_keyword(blocker, "can't block"): return False
        if self._has_keyword(attacker, "shadow") and not self._has_keyword(blocker, "shadow"): return False
        if self._has_keyword(attacker, "unblockable"): return False # Basic unblockable
        # Add other evasion/restriction checks if needed (fear, intimidate, landwalk etc.)

        return True

    def _has_keyword(self, card, keyword):
        """Checks if a card has a keyword using the central checker (AbilityHandler preferred)."""
        gs = self.game_state
        card_id = getattr(card, 'card_id', None)
        if card_id is None: return False

        # 1. Prefer AbilityHandler (handles static grants/removals)
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
            if hasattr(gs.ability_handler, 'check_keyword'):
                 try:
                     # Use AbilityHandler's public method
                     return gs.ability_handler.check_keyword(card_id, keyword)
                 except Exception as e:
                      logging.error(f"Error checking keyword via AbilityHandler in CombatActionHandler: {e}")
                      # Fall through to GameState check on error
            # else: Fall through if check_keyword doesn't exist on handler
        # --- DELEGATION ADDED: Check GameState next ---
        if hasattr(gs, 'check_keyword') and callable(gs.check_keyword):
            try:
                return gs.check_keyword(card_id, keyword)
            except Exception as e:
                 logging.error(f"Error checking keyword via GameState in CombatActionHandler: {e}")
                 
        logging.warning(f"Keyword check failed in CombatActionHandler for {keyword} on {getattr(card, 'name', 'Unknown')}: Delegation methods failed or keyword not found.")
        return False
     
