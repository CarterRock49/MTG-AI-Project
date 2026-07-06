"""Handlers and validity checks for combat actions.

Extracted from actions.py. This module defines behavior only (a mixin);
all state lives on ActionHandler, which composes every mixin.
"""

import logging
from .enhanced_combat import ExtendedCombatResolver
from .combat_integration import apply_combat_action


class CombatHandlersMixin:
    """Handlers and validity checks for combat actions."""

    __slots__ = ()

    def is_valid_attacker(self, card_id):
        """Delegate to CombatActionHandler.is_valid_attacker"""
        if self.combat_handler:
            return self.combat_handler.is_valid_attacker(card_id)
        # --- Fallback logic (kept for reference, but delegation is preferred) ---
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        me = gs.p1 if gs.agent_is_p1 else gs.p2
        if not card or 'creature' not in getattr(card, 'card_types', []): return False
        if card_id in me.get("tapped_permanents", set()): return False
        # Use GameState's _has_haste method which likely checks LayerSystem
        if card_id in me.get("entered_battlefield_this_turn", set()) and not self._has_haste(card_id): return False
        # Check defender keyword via centralized check
        if self._has_keyword(card, "defender"): return False
        return True

    def find_optimal_attack(self):
        """Delegate to CombatActionHandler.find_optimal_attack"""
        if self.combat_handler:
            return self.combat_handler.find_optimal_attack()
        return [] # _handle_search_library

    def setup_combat_systems(self):
        """Delegate to CombatActionHandler.setup_combat_systems"""
        if self.combat_handler:
            self.combat_handler.setup_combat_systems()

    def _has_first_strike(self, card):
        """Delegate to CombatActionHandler._has_first_strike"""
        if self.combat_handler:
            return self.combat_handler._has_first_strike(card)
        # _handle_search_library
        if not card: return False
        if hasattr(card, 'oracle_text') and "first strike" in card.oracle_text.lower(): return True
        if hasattr(card, 'keywords') and len(card.keywords) > 5 and card.keywords[5] == 1: return True
        return False

    def _handle_attack(self, param, **kwargs):
        gs = self.game_state
        player = gs._get_active_player()
        battlefield_idx = param

        if battlefield_idx >= len(player.get("battlefield", [])):
            logging.warning(f"ATTACK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        card_id = player["battlefield"][battlefield_idx]
        card = gs._safe_get_card(card_id)
        if not card: return -0.2, False

        can_attack = False
        if self.combat_handler:
            can_attack = self.combat_handler.is_valid_attacker(card_id)
        else: # Fallback
            if 'creature' in getattr(card, 'card_types', []):
                 tapped_set = player.get("tapped_permanents", set())
                 entered_set = player.get("entered_battlefield_this_turn", set())
                 has_haste = self._has_keyword(card, "haste")
                 if card_id not in tapped_set and not (card_id in entered_set and not has_haste):
                      can_attack = True

        if not hasattr(gs, 'current_attackers'): gs.current_attackers = []
        if not hasattr(gs, 'planeswalker_attack_targets'): gs.planeswalker_attack_targets = {}
        if not hasattr(gs, 'battle_attack_targets'): gs.battle_attack_targets = {}

        if card_id in gs.current_attackers:
            gs.current_attackers.remove(card_id)
            gs.planeswalker_attack_targets.pop(card_id, None)
            gs.battle_attack_targets.pop(card_id, None)
            logging.debug(f"ATTACK: Deselected {card.name}")
            return -0.05, True # Deselection successful
        else:
            if can_attack:
                 gs.current_attackers.append(card_id)
                 logging.debug(f"ATTACK: Declared {card.name} as attacker.")
                 return 0.1, True # Declaration successful
            else:
                 logging.warning(f"ATTACK: {card.name} cannot attack now.")
                 return -0.1, False # Cannot attack (failure)

    def _handle_block(self, param, **kwargs):
        gs = self.game_state
        blocker_player = gs._get_non_active_player()
        battlefield_idx = param
        context = kwargs.get('context', {})

        if battlefield_idx >= len(blocker_player.get("battlefield", [])):
            logging.warning(f"BLOCK: Invalid battlefield index {battlefield_idx}")
            return -0.2, False

        blocker_id = blocker_player["battlefield"][battlefield_idx]
        blocker_card = gs._safe_get_card(blocker_id)
        if not blocker_card or 'creature' not in getattr(blocker_card, 'card_types', []):
             logging.warning(f"BLOCK: {blocker_id} is not a creature.")
             return -0.15, False

        if not hasattr(gs, 'current_block_assignments'): gs.current_block_assignments = {}
        currently_blocking_attacker = None
        for atk_id, blockers_list in gs.current_block_assignments.items():
            if blocker_id in blockers_list:
                currently_blocking_attacker = atk_id; break

        if currently_blocking_attacker:
            gs.current_block_assignments[currently_blocking_attacker].remove(blocker_id)
            if not gs.current_block_assignments[currently_blocking_attacker]:
                del gs.current_block_assignments[currently_blocking_attacker]
            logging.debug(f"BLOCK: Unassigned {blocker_card.name} from blocking {gs._safe_get_card(currently_blocking_attacker).name}")
            return -0.05, True # Deselection successful
        else:
            target_attacker_id = context.get('target_attacker_id')
            if target_attacker_id is None:
                possible_targets = [atk_id for atk_id in getattr(gs, 'current_attackers', []) if self._can_block(blocker_id, atk_id)]
                if possible_targets:
                    possible_targets.sort(key=lambda atk_id: getattr(gs._safe_get_card(atk_id),'power',0), reverse=True)
                    target_attacker_id = possible_targets[0]
                    logging.debug(f"BLOCK: AI chose attacker {gs._safe_get_card(target_attacker_id).name} for {blocker_card.name}")
                else:
                     logging.warning(f"BLOCK: No valid attacker found for {blocker_card.name} to block.")
                     return -0.1, False # No valid attacker to assign

            # Validate chosen/found target
            if target_attacker_id not in getattr(gs, 'current_attackers', []) or not self._can_block(blocker_id, target_attacker_id):
                 logging.warning(f"BLOCK: Cannot legally block chosen attacker {target_attacker_id}")
                 return -0.1, False # Invalid block target

            if target_attacker_id not in gs.current_block_assignments:
                gs.current_block_assignments[target_attacker_id] = []
            if blocker_id not in gs.current_block_assignments[target_attacker_id]:
                gs.current_block_assignments[target_attacker_id].append(blocker_id)
                logging.debug(f"BLOCK: Assigned {blocker_card.name} to block {gs._safe_get_card(target_attacker_id).name}")
                return 0.1, True # Assignment successful
            else: # Should not happen if selection/deselection logic is right
                 logging.debug(f"BLOCK: Redundant block assignment ignored for {blocker_card.name}")
                 return -0.01, False # Redundant action failed

    def _handle_exert(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player declaring attacker
        creature_idx = context.get('creature_idx')

        if creature_idx is None: logging.warning(f"Exert context missing 'creature_idx'"); return -0.15, False
        try: creature_idx = int(creature_idx)
        except (ValueError, TypeError): logging.warning(f"Exert context has non-integer index: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Exert index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        # Exert choice typically made when declaring attackers.
        # Check if card IS attacking. Combat handler integration needed.
        # Assume for now: if card is in `current_attackers`, can exert.
        if card_id in getattr(gs, 'current_attackers', []):
            if not hasattr(gs, 'exerted_this_combat'): gs.exerted_this_combat = set()
            if card_id not in gs.exerted_this_combat:
                gs.exerted_this_combat.add(card_id)
                card = gs._safe_get_card(card_id)
                logging.debug(f"Exerted {card.name}")
                gs.trigger_ability(card_id, "EXERTED", {"controller": player})
                return 0.2, True # Success
            else: # Already exerted this combat
                logging.debug(f"Cannot Exert: {card_id} already exerted.")
                return -0.05, False # Cannot exert again
        else: # Cannot exert if not attacking
             logging.debug(f"Cannot Exert: {card_id} not currently attacking.")
             return -0.1, False

    def _handle_boast(self, param, context, **kwargs):
        gs = self.game_state; player = gs._get_active_player() # Player activating boast
        creature_idx = context.get('creature_idx')

        if creature_idx is None: logging.warning(f"Boast context missing 'creature_idx'"); return -0.15, False
        try: creature_idx = int(creature_idx)
        except (ValueError, TypeError): logging.warning(f"Boast context has non-integer index: {context}"); return -0.15, False

        if creature_idx >= len(player["battlefield"]): logging.warning(f"Boast index out of bounds: {creature_idx}"); return -0.15, False

        card_id = player["battlefield"][creature_idx]
        card = gs._safe_get_card(card_id)
        if not card or "boast" not in getattr(card, 'oracle_text', '').lower():
             logging.warning(f"Boast card {card_id} invalid or has no Boast."); return -0.1, False

        # Check condition: Attacked this turn?
        if not hasattr(gs, 'attackers_this_turn') or card_id not in gs.attackers_this_turn:
             logging.debug(f"Cannot Boast: {card.name} did not attack this turn."); return -0.1, False
        # Check condition: Already boasted this turn?
        if card_id in getattr(gs, 'boast_activated', set()):
             logging.debug(f"Cannot Boast: {card.name} already boasted this turn."); return -0.1, False

        # Find boast ability (better than assuming index 0 or 1)
        ability_idx_to_activate = -1
        if hasattr(gs, 'ability_handler'):
            abilities = gs.ability_handler.get_activated_abilities(card_id)
            for idx, ab in enumerate(abilities):
                if "boast —" in getattr(ab, 'effect_text', '').lower(): # Check for "Boast —" marker
                    ability_idx_to_activate = idx; break

        if ability_idx_to_activate != -1:
            if gs.ability_handler.can_activate_ability(card_id, ability_idx_to_activate, player):
                # Use generic activate_ability which handles costs and stack
                success = gs.ability_handler.activate_ability(card_id, ability_idx_to_activate, player)
                if success:
                    if not hasattr(gs, 'boast_activated'): gs.boast_activated = set()
                    gs.boast_activated.add(card_id) # Mark after successful activation
                    return 0.3, True # Success
                else: return -0.1, False # Activation failed (e.g. cost)
            else: return -0.1, False # Cannot activate currently
        else:
             logging.warning(f"No ability with 'Boast —' marker found for {card.name}"); return -0.1, False

    # --- Combat Handler Wrappers ---
    def _handle_declare_attackers_done(self, param, context, **kwargs):
        success = apply_combat_action(self.game_state, "DECLARE_ATTACKERS_DONE", param, context=context)
        return (0.05 if success else -0.1), success

    def _handle_declare_blockers_done(self, param, context, **kwargs):
        success = apply_combat_action(self.game_state, "DECLARE_BLOCKERS_DONE", param, context=context)
        return (0.05 if success else -0.1), success

    def _handle_attack_planeswalker(self, param, context, **kwargs):
        # Param = relative PW index (0-4)
        # Context needs attacker ID
        success = apply_combat_action(self.game_state, "ATTACK_PLANESWALKER", param, context=context)
        return (0.1 if success else -0.1), success

    def _handle_assign_multiple_blockers(self, param, context, **kwargs):
        # Context needs {attacker_id: ..., blocker_ids: [...], order: [...]}
        success = apply_combat_action(self.game_state, "ASSIGN_MULTIPLE_BLOCKERS", param, context=context)
        return (0.1 if success else -0.1), success

    def _handle_first_strike_order(self, param, context, **kwargs):
        # Context needs {attacker_id: ..., order: [...]}
        success = apply_combat_action(self.game_state, "FIRST_STRIKE_ORDER", param, context=context)
        return (0.05 if success else -0.1), success

    def _handle_assign_combat_damage(self, param, context, **kwargs):
        # Context might have manual assignments, or None for auto
        success = apply_combat_action(self.game_state, "ASSIGN_COMBAT_DAMAGE", param, context=context)
        return (0.05 if success else -0.1), success

    def _handle_protect_planeswalker(self, param, context, **kwargs):
        # Context needs {blocker_id: ..., planeswalker_id: ...}
        success = apply_combat_action(self.game_state, "PROTECT_PLANESWALKER", param, context=context)
        return (0.15 if success else -0.1), success

    def _handle_attack_battle(self, param, **kwargs):
         # Param needs to be (attacker_idx, battle_idx)
         # The ACTION_MEANING needs fixing.
         # We need to select an attacker.
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         # Select first valid attacker? This needs better logic.
         attacker_idx = -1
         for idx, cid in enumerate(player["battlefield"]):
             if self.is_valid_attacker(cid):
                 attacker_idx = idx
                 break
         if attacker_idx != -1 and param is not None:
             # Store mapping for combat handler
             gs._battle_attack_creatures = getattr(gs, '_battle_attack_creatures', {})
             gs._battle_attack_creatures[param] = attacker_idx # Map battle_idx to creature_idx
             return 0.1 if apply_combat_action(gs, "ATTACK_BATTLE", param) else -0.1
         return -0.15 # No valid attacker or battle index

    def _handle_defend_battle(self, param, **kwargs):
         return 0.1 if apply_combat_action(self.game_state, "DEFEND_BATTLE", param) else -0.1

    def _handle_ninjutsu(self, param, **kwargs):
         # Param needs (ninja_hand_idx, attacker_idx)
         # Simple version: assume first ninja, first unblocked attacker
         ninja_idx = -1
         attacker_id = None
         # Find ninja
         gs = self.game_state
         player = gs.p1 if gs.agent_is_p1 else gs.p2
         for idx, cid in enumerate(player["hand"]):
              card = gs._safe_get_card(cid)
              if card and "ninjutsu" in getattr(card, 'oracle_text', '').lower():
                   ninja_idx = idx
                   break
         # Find unblocked attacker
         unblocked = [aid for aid in gs.current_attackers if aid not in gs.current_block_assignments or not gs.current_block_assignments[aid]]
         if unblocked: attacker_id = unblocked[0]

         if ninja_idx != -1 and attacker_id is not None:
             return 0.3 if apply_combat_action(gs, "NINJUTSU", ninja_idx, attacker_id) else -0.1 # Pass both params
         return -0.15

    # --- Helper method to check blocking capability ---
    def _can_block(self, blocker_id, attacker_id):
         """Check if blocker_id can legally block attacker_id."""
         return ExtendedCombatResolver._check_block_restrictions(self, blocker_id, attacker_id)

    def _has_haste(self, card_id):
        """Centralized haste check using AbilityHandler."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card: return False
        if hasattr(gs, 'ability_handler') and gs.ability_handler:
             return gs.ability_handler.check_keyword(card_id, "haste")
        return 'haste' in getattr(card,'oracle_text','').lower() # Fallback
