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
    action application and combat action handling, with improved parameter handling.

    Args:
        game_state: The game state object
        action_type: String specifying the action type
        param: Optional parameter for the action (can be simple value or tuple)

    Returns:
        bool: True if action was successfully applied
    """
    combat_action_handler = integrate_combat_actions(game_state)
    if not combat_action_handler:
        logging.error("Cannot apply combat action: CombatActionHandler is missing.")
        return False

    try:
        # Map action types to their corresponding handler methods
        handlers = {
            "FIRST_STRIKE_ORDER": combat_action_handler.handle_first_strike_order,
            "ASSIGN_COMBAT_DAMAGE": combat_action_handler.handle_assign_combat_damage,
            "NINJUTSU": combat_action_handler.handle_ninjutsu,
            "DECLARE_ATTACKERS_DONE": combat_action_handler.handle_declare_attackers_done,
            "DECLARE_BLOCKERS_DONE": combat_action_handler.handle_declare_blockers_done,
            "ATTACK_PLANESWALKER": combat_action_handler.handle_attack_planeswalker,
            "ASSIGN_MULTIPLE_BLOCKERS": combat_action_handler.handle_assign_multiple_blockers,
            "ATTACK_BATTLE": combat_action_handler.handle_attack_battle,
            "DEFEND_BATTLE": combat_action_handler.handle_defend_battle,
            "PROTECT_PLANESWALKER": combat_action_handler.handle_protect_planeswalker
        }

        handler_method = handlers.get(action_type)

        if handler_method:
            # Call the method, unpacking parameters if param is a tuple
            if isinstance(param, tuple):
                return handler_method(*param) # Assumes handler accepts unpacked tuple args
            elif param is not None:
                return handler_method(param) # Single parameter
            else:
                return handler_method() # No parameter
        else:
            logging.warning(f"No specific combat handler found for action type: {action_type}")
            return False # Action not recognized within combat context

    except TypeError as te:
         # Catch cases where the parameter structure doesn't match the handler signature
         logging.error(f"Parameter mismatch calling handler for {action_type} with param {param}: {te}")
         import traceback
         logging.error(traceback.format_exc())
         return False
    except Exception as e:
        logging.error(f"Error processing combat action {action_type}: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return False