import logging
from .combat_actions import CombatActionHandler

def integrate_combat_actions(game_state):
    """
    Integrate the CombatActionHandler with the game state. Ensures a single instance.
    Handles potential None value for combat_resolver.
    """
    if not hasattr(game_state, 'combat_action_handler') or game_state.combat_action_handler is None:
        logging.debug("Creating and integrating CombatActionHandler")
        game_state.combat_action_handler = CombatActionHandler(game_state)
    else:
        # Ensure the existing handler points to the current game_state if it changed
        if game_state.combat_action_handler.game_state != game_state:
            game_state.combat_action_handler.game_state = game_state
            logging.debug("Updated CombatActionHandler game_state reference.")
        else:
             pass # Already integrated

    # Ensure resolver linkage if resolver exists AND IS NOT NONE
    if hasattr(game_state, 'combat_resolver') and game_state.combat_resolver is not None:
         # Link handler to resolver if resolver expects it
         if hasattr(game_state.combat_resolver, 'action_handler'):
             game_state.combat_resolver.action_handler = game_state.combat_action_handler
    elif hasattr(game_state, 'combat_resolver'):
         # Resolver attribute exists but is None
         logging.debug("combat_resolver exists but is None, cannot link.")

    return game_state.combat_action_handler # Return the instance from game_state


def apply_combat_action(game_state, action_type, param=None, context=None): # Added context parameter
    """
    Apply a specific combat action, serving as a lightweight bridge between
    action application and combat action handling, with improved parameter handling.

    Args:
        game_state: The game state object
        action_type: String specifying the action type
        param: Optional parameter for the action (can be simple value or derived context)
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
            # Add more delegated actions here as needed
        }

        handler_method = handlers.get(action_type)

        if handler_method:
            # --- CLEANER CALL LOGIC ---
            # Prioritize passing context if it exists, as it might contain required info.
            # Handler methods are now expected to handle potentially receiving
            # param, context, or both.
            # Let the handler method's signature (*args, **kwargs) or explicit checks handle it.
            kwargs = {}
            if param is not None:
                kwargs['param'] = param
            if context is not None:
                kwargs['context'] = context
            else:
                 # Ensure context is at least an empty dict if None was passed
                 kwargs['context'] = {}

            # If the handler name implies it needs `param` but `param` is None, log warning
            # (e.g., ATTACK_PLANESWALKER needs the index from `param`)
            actions_expecting_param = ["ATTACK_PLANESWALKER", "ATTACK_BATTLE"] # Add others as needed
            if action_type in actions_expecting_param and param is None:
                 logging.warning(f"Action '{action_type}' likely expects a 'param' argument, but it was None. Handler might fail.")

            # Call the handler with keyword arguments
            return handler_method(**kwargs)

        else:
            logging.warning(f"No specific combat handler found for action type: {action_type}")
            return False # Action not recognized within combat context

    except TypeError as te:
         # Catch cases where the parameter structure doesn't match the handler signature
         logging.error(f"Parameter mismatch calling combat handler for {action_type}. Param: {param}, Context: {context}. Error: {te}")
         import traceback
         logging.error(traceback.format_exc())
         return False
    except Exception as e:
        logging.error(f"Error processing combat action {action_type}: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return False