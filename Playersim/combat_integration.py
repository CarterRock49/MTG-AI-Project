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
        pass # Already integrated

    # Ensure resolver linkage if resolver exists
    if hasattr(game_state, 'combat_resolver'):
         # Link handler to resolver if resolver expects it
         if hasattr(game_state.combat_resolver, 'action_handler'):
             game_state.combat_resolver.action_handler = game_state.combat_action_handler

    return game_state.combat_action_handler # Return the instance from game_state


def apply_combat_action(game_state, action_type, param=None, context=None): # Added context parameter
    """
    Apply a specific combat action, serving as a lightweight bridge between
    action application and combat action handling, with improved parameter handling.

    Args:
        game_state: The game state object
        action_type: String specifying the action type
        param: Optional parameter for the action (can be simple value)
        context: Optional dictionary with additional context for complex actions.

    Returns:
        bool: True if action was successfully applied
    """
    combat_action_handler = integrate_combat_actions(game_state)
    if not combat_action_handler:
        logging.error("Cannot apply combat action: CombatActionHandler is missing.")
        return False

    try:
        # Map action types to their corresponding handler methods within CombatActionHandler
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
            # *** REFACTOR: Pass context to the handler method ***
            # Check signature? Or just pass context generally? Pass context.
            if param is not None and context is not None:
                 # Pass both param and context (e.g., ASSIGN_MULTIPLE_BLOCKERS)
                 return handler_method(param=param, context=context)
            elif context is not None:
                 # Pass only context (e.g., NINJUTSU, PROTECT_PLANESWALKER, DEFEND_BATTLE)
                 return handler_method(param=None, context=context)
            elif param is not None:
                 # Pass only param (e.g., ATTACK_PLANESWALKER, ATTACK_BATTLE)
                 return handler_method(param=param)
            else:
                 # No param or context needed (e.g., DECLARE_..._DONE, FIRST_STRIKE_ORDER?)
                 return handler_method()
        else:
            logging.warning(f"No specific combat handler found for action type: {action_type}")
            return False # Action not recognized within combat context

    except TypeError as te:
         # Catch cases where the parameter structure doesn't match the handler signature
         logging.error(f"Parameter mismatch calling combat handler for {action_type} with param {param} and context {context}: {te}")
         import traceback
         logging.error(traceback.format_exc())
         return False
    except Exception as e:
        logging.error(f"Error processing combat action {action_type}: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return False