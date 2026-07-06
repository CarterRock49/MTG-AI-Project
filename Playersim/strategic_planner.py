import logging
import numpy as np
import random
import re

from .strategic_planner_archetypes import ArchetypeAnalysisMixin
from .strategic_planner_evaluation import CardEvaluationMixin
from .strategic_planner_threats import ThreatSynergyMixin
from .strategic_planner_search import SearchDecisionMixin
from .strategic_planner_search import MCTSNode  # noqa: F401  (re-export: MCTSNode lives with the search mixin)


class MTGStrategicPlanner(
    ArchetypeAnalysisMixin,
    CardEvaluationMixin,
    ThreatSynergyMixin,
    SearchDecisionMixin,
):
    """Advanced strategic decision making system for Magic: The Gathering AI."""
    

    def __init__(self, game_state, card_evaluator=None, combat_resolver=None):
        self.game_state = game_state # Must be a valid GameState instance
        self.card_evaluator = card_evaluator
        self.combat_resolver = combat_resolver

        # Initialize strategy parameters with safe defaults
        self.aggression_level = 0.5  # 0.0 = defensive, 1.0 = all-out aggression
        self.risk_tolerance = 0.5    # 0.0 = risk averse, 1.0 = high risk
        self.strategy_type = "midrange"  # Default strategy type

        # Strategy types (Keep existing definitions)
        self.strategies = {
            "aggro": {
                "description": "Aggressive strategy: Play fast creatures and attack quickly",
                "aggression_level": 0.8,
                "risk_tolerance": 0.7,
                "card_weights": {"creature": 1.5, "instant": 0.7, "sorcery": 0.7, "artifact": 0.5, "enchantment": 0.5, "planeswalker": 1.2, "land": 0.7}
            },
            "control": {
                "description": "Control strategy: Counter spells, remove threats, win late game",
                "aggression_level": 0.2,
                "risk_tolerance": 0.3,
                "card_weights": {"creature": 0.7, "instant": 1.5, "sorcery": 1.3, "artifact": 1.0, "enchantment": 1.0, "planeswalker": 1.5, "land": 0.8}
            },
            "midrange": {
                "description": "Midrange strategy: Efficient creatures and value plays",
                "aggression_level": 0.5,
                "risk_tolerance": 0.5,
                "card_weights": {"creature": 1.2, "instant": 1.0, "sorcery": 1.0, "artifact": 0.8, "enchantment": 0.8, "planeswalker": 1.3, "land": 0.8}
            },
            "combo": {
                "description": "Combo strategy: Assemble a game-winning combination",
                "aggression_level": 0.4,
                "risk_tolerance": 0.9,
                "card_weights": {"creature": 0.8, "instant": 1.0, "sorcery": 1.2, "artifact": 1.3, "enchantment": 1.3, "planeswalker": 0.7, "land": 0.9}
            }
            # Keep other strategies...
        }

        # Always initialize with a default strategy first
        self._initialize_strategy_params("midrange")

        # Remember the current game state analysis
        self.current_analysis = None
        self.opponent_archetype = None

        # Defer archetype detection until init_after_reset or when needed
        logging.debug("Strategic Planner initialized. Deck archetype detection deferred.")


    def init_after_reset(self):
        """
        Initialize the strategic planner after the game state has been reset
        and p1/p2 have been established. Relies on GameState being fully ready.
        """
        try:
            gs = self.game_state

            # Check if game state is properly initialized including action_handler
            if not hasattr(gs, 'p1') or not hasattr(gs, 'p2') or not gs.p1 or not gs.p2:
                logging.warning("StrategicPlanner cannot init_after_reset: GS players not ready.")
                return
            # --- Check if action_handler is available on the GameState ---
            if not hasattr(gs, 'action_handler') or gs.action_handler is None:
                logging.warning("StrategicPlanner cannot init_after_reset: GS action_handler not ready.")
                return
            # --- End Check ---

            # Ensure player states have minimum required attributes
            for player in [gs.p1, gs.p2]:
                if not all(attr in player for attr in ["hand", "battlefield", "library"]):
                    logging.warning(f"StrategicPlanner init_after_reset: Player state missing attributes ({player.get('name', 'Unknown')}). Using default strategy.")
                    self._initialize_strategy_params('midrange')
                    return

            # Detect deck archetype with proper error handling
            try:
                archetype = self._detect_deck_archetype()
                logging.debug(f"Strategic planner initialized with deck archetype: {archetype}")
            except Exception as e:
                logging.warning(f"Error detecting deck archetype during init_after_reset: {e}")
                import traceback
                logging.debug(traceback.format_exc())
                self._initialize_strategy_params('midrange') # Default on error
        except Exception as e:
            logging.error(f"Error in strategic planner init_after_reset: {e}")
            import traceback
            logging.error(traceback.format_exc())
            self._initialize_strategy_params('midrange') # Ensure default

    
    
            

        
    
    
            
    
    
    
    
    

    
    
    
        
    
    
    
    
    


    
    

    

    


    



        
    