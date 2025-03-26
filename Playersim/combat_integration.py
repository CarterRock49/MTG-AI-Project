import logging
from .combat_actions import CombatActionHandler

def integrate_combat_actions(game_state):
    """
    Integrate the CombatActionHandler with the game state. Ensures a single instance.
    """
    if not hasattr(game_state, 'combat_action_handler') or game_state.combat_action_handler is None:
        logging.debug("Creating and integrating CombatActionHandler")
        game_state.combat_action_handler = CombatActionHandler(game_state)
    else:
        # Optionally re-link if needed, but usually just return existing
        pass

    # Ensure resolver linkage if resolver exists
    if hasattr(game_state, 'combat_resolver'):
        game_state.combat_resolver.action_handler = game_state.combat_action_handler

    return game_state.combat_action_handler # Return the instance from game_state


def apply_combat_action(game_state, action_type, param=None):
    """
    Apply a specific combat action, serving as a lightweight bridge between 
    action application and combat action handling.

    Args:
        game_state: The game state object
        action_type: String specifying the action type
        param: Optional parameter for the action

    Returns:
        bool: True if action was successfully applied
    """
    # Ensure combat action handler is available
    combat_action_handler = integrate_combat_actions(game_state)

    # Simplified action mapping with improved error handling
    try:
        # Call methods on the handler with proper error handling
        if action_type == "FIRST_STRIKE_ORDER":
            return combat_action_handler.handle_first_strike_order()
        elif action_type == "ASSIGN_COMBAT_DAMAGE":
            if isinstance(param, dict):
                return combat_action_handler.handle_assign_combat_damage(param)
            else:
                return combat_action_handler.handle_assign_combat_damage()
        elif action_type == "NINJUTSU":
            attacker_id = game_state.current_attackers[-1] if game_state.current_attackers else None
            return combat_action_handler.handle_ninjutsu(param, attacker_id)
        elif action_type == "DECLARE_ATTACKERS_DONE":
            return combat_action_handler.handle_declare_attackers_done()
        elif action_type == "DECLARE_BLOCKERS_DONE":
            return combat_action_handler.handle_declare_blockers_done()
        elif action_type == "ATTACK_PLANESWALKER":
            return combat_action_handler.handle_attack_planeswalker(param)
        elif action_type == "ASSIGN_MULTIPLE_BLOCKERS":
            return combat_action_handler.handle_assign_multiple_blockers(param)
        elif action_type == "ATTACK_BATTLE":
            # Just pass the battle index - creature index is stored separately
            return combat_action_handler.handle_attack_battle(param)
        elif action_type == "DEFEND_BATTLE":
            if isinstance(param, tuple) and len(param) == 2:
                return combat_action_handler.handle_defend_battle(param[0], param[1])
            return False
        elif action_type == "PROTECT_PLANESWALKER":
            # Handle both tuple parameter and simple parameter cases
            if isinstance(param, tuple) and len(param) >= 1:
                planeswalker_id = param[0]
                defender_idx = param[1] if len(param) >= 2 else None
            else:
                planeswalker_id = param
                defender_idx = None
            return combat_action_handler.handle_protect_planeswalker(planeswalker_id, defender_idx)

    except Exception as e:
        logging.error(f"Error processing combat action {action_type}: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return False