import logging
import re
import random
from .enhanced_mana_system import EnhancedManaSystem
from .card import Card
from .ability_utils import text_to_number, safe_int, resolve_simple_targeting, EffectFactory


class Ability:
    """Base class for card abilities"""
    def __init__(self, card_id, effect_text=""):
        self.card_id = card_id
        self.effect_text = effect_text
        self.source_card = None # Add a reference to the card object i
        
    def can_trigger(self, event, context):
        """Check if this ability should trigger"""
        return False
    def resolve(self, game_state, controller):
        """Resolve the ability's effect with improved error handling"""
        card = game_state._safe_get_card(self.card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {self.card_id} not found")
            return False
            
        try:
            # Check if ability requires targeting
            requires_target = "target" in self.effect_text.lower()
            targets = None
            
            # Handle targeting
            if requires_target:
                targets = self._handle_targeting(game_state, controller)
                if not targets or (isinstance(targets, dict) and not any(targets.values())):
                    logging.debug(f"Targeting failed for ability: {self.effect_text}")
                    return False
            
            # Delegate to specific implementation
            return self._resolve_ability_implementation(game_state, controller, targets)
        except Exception as e:
            logging.error(f"Error resolving ability ({type(self).__name__}): {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False
        
    def _handle_targeting(self, game_state, controller):
        """
        Handle targeting for this ability by using TargetingSystem if available.
        
        Args:
            game_state: The game state
            controller: The player controlling the ability
            
        Returns:
            dict: Dictionary of targets for this ability
        """
        # First try to use the targeting system
        if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
            return game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                self.card_id, self.effect_text, controller)
        
        # If targeting system is not available, try direct targeting system import
        try:
            from .ability_handler import TargetingSystem
            targeting_system = TargetingSystem(game_state)
            return targeting_system.resolve_targeting_for_ability(self.card_id, self.effect_text, controller)
        except ImportError:
            # Fall back to simple targeting if targeting system is not available
            return self._resolve_simple_targeting(game_state, controller, self.effect_text)
    
    def _resolve_ability_implementation(self, game_state, controller, targets=None):
        """Ability-specific implementation of resolution. Should be overridden by subclasses."""
        logging.warning(f"Default ability resolution used for {self.effect_text}")
        return self._resolve_ability_effect(game_state, controller, targets)
    
    def _create_ability_effects(self, effect_text, targets=None):
        """Create appropriate AbilityEffect objects based on the effect text"""
        return EffectFactory.create_effects(effect_text, targets)
    
    def _resolve_simple_targeting(self, game_state, controller, effect_text):
        """Simplified targeting resolution when targeting system isn't available"""
        return resolve_simple_targeting(game_state, self.card_id, controller, effect_text)
    
    def __str__(self):
        return f"Ability({self.effect_text})"
    
    def _resolve_ability_effect(self, game_state, controller, targets=None):
        """Common resolution logic for abilities."""
        card = game_state._safe_get_card(self.card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {self.card_id} not found")
            return False
            
        try:
            # Convert effect text to lowercase for easier parsing
            effect = self.effect.lower() if hasattr(self, 'effect') else ""
            
            # Create and apply appropriate effects
            effects = self._create_ability_effects(effect, targets)
            
            for effect_obj in effects:
                effect_obj.apply(game_state, self.card_id, controller, targets)
                
            logging.debug(f"Resolved ability: {self.effect_text}")
            return True
                
        except Exception as e:
            logging.error(f"Error resolving ability: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return False


class ActivatedAbility(Ability):
    """Ability that can be activated by paying a cost"""
    def __init__(self, card_id, cost, effect, effect_text=""):
        super().__init__(card_id, effect_text)
        self.cost = cost
        self.effect = effect
        
    def resolve(self, game_state, controller):
        """Resolve this activated ability using effect classes."""
        # Check if ability requires targeting
        requires_target = "target" in self.effect.lower() if hasattr(self, 'effect') else False
        targets = None
        
        # Handle targeting
        if requires_target:
            # Use targeting system if available
            if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                    self.card_id, self.effect, controller)
            else:
                # Simplified targeting
                targets = self._resolve_simple_targeting(game_state, controller, self.effect)
            
            # Check if targeting failed
            if not targets or (isinstance(targets, dict) and not any(targets.values())):
                logging.debug(f"Targeting failed for ability: {self.effect_text}")
                return False
        
        # Use common resolution logic
        return self._resolve_ability_effect(game_state, controller, targets)

    def resolve_with_targets(self, game_state, controller, targets=None):
        """Resolve this ability with specific targets."""
        # Subclasses might need to override this if they have special target handling
        # Default implementation just calls the standard resolve logic
        return self._resolve_ability_implementation(game_state, controller, targets)

    def can_activate(self, game_state, controller):
        """Check if this ability can be activated using EnhancedManaSystem."""
        if hasattr(game_state, 'mana_system') and game_state.mana_system:
            parsed_cost = game_state.mana_system.parse_mana_cost(self.cost)
            return game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)

        # Fallback to basic cost check if mana_system is not available
        return super().can_activate(game_state, controller)

    def pay_cost(self, game_state, controller):
        """Pay the activation cost of this ability with comprehensive cost handling."""
        # Check for complex costs (includes mana and non-mana components)
        cost_text = self.cost.lower()
        
        # Track if all costs were paid successfully
        all_costs_paid = True
        
        # Process non-mana costs first
        
        # Check for tap symbol
        if "{t}" in cost_text or "tap" in cost_text:
            # Check if card is already tapped
            if self.card_id in controller.get("tapped_permanents", set()):
                logging.debug(f"Cannot pay tap cost: {game_state._safe_get_card(self.card_id).name} is already tapped")
                return False
            # Add to tapped_permanents
            if "tapped_permanents" not in controller:
                controller["tapped_permanents"] = set()
            controller["tapped_permanents"].add(self.card_id)
            logging.debug(f"Paid tap cost for {game_state._safe_get_card(self.card_id).name}")
        
        # Check for sacrifice costs
        if "sacrifice" in cost_text:
            # Extract what needs to be sacrificed
            sacrifice_match = re.search(r"sacrifice ([^:,]+)", cost_text)
            if sacrifice_match:
                sacrifice_req = sacrifice_match.group(1).strip()
                
                # Check if we can meet the sacrifice requirement
                if not self._can_sacrifice(game_state, controller, sacrifice_req):
                    logging.debug(f"Cannot pay sacrifice cost: {sacrifice_req}")
                    return False
                    
                # Pay the sacrifice cost
                self._pay_sacrifice_cost(game_state, controller, sacrifice_req)
        
        # Check for discard costs
        if "discard" in cost_text:
            # Extract what needs to be discarded
            discard_match = re.search(r"discard ([^:,]+)", cost_text)
            if discard_match:
                discard_req = discard_match.group(1).strip()
                
                # Check if hand has enough cards
                if discard_req == "your hand" and not controller["hand"]:
                    logging.debug("Cannot pay discard cost: hand is empty")
                    return False
                elif discard_req.startswith("a ") or discard_req.startswith("one "):
                    if not controller["hand"]:
                        logging.debug("Cannot pay discard cost: hand is empty")
                        return False
                
                # Pay the discard cost
                self._pay_discard_cost(game_state, controller, discard_req)
        
        # Check for exile costs
        if "exile" in cost_text and "from" in cost_text:
            # Extract what needs to be exiled
            exile_match = re.search(r"exile ([^:,]+) from ([^:,]+)", cost_text)
            if exile_match:
                exile_what = exile_match.group(1).strip()
                exile_from = exile_match.group(2).strip()
                
                # Handle different types of exile costs
                if exile_from == "your graveyard":
                    if not self._can_exile_from_graveyard(game_state, controller, exile_what):
                        logging.debug(f"Cannot pay exile cost: {exile_what} from {exile_from}")
                        return False
                    self._pay_exile_from_graveyard_cost(game_state, controller, exile_what)
                elif exile_from == "your hand":
                    if not self._can_exile_from_hand(game_state, controller, exile_what):
                        logging.debug(f"Cannot pay exile cost: {exile_what} from {exile_from}")
                        return False
                    self._pay_exile_from_hand_cost(game_state, controller, exile_what)
        
        # Check for life payment
        if "pay" in cost_text and "life" in cost_text:
            # Extract life amount
            life_match = re.search(r"pay (\d+) life", cost_text)
            if life_match:
                life_amount = int(life_match.group(1))
                
                # Check if player has enough life
                if controller["life"] <= life_amount:
                    logging.debug(f"Cannot pay life cost: not enough life")
                    return False
                    
                # Pay the life
                controller["life"] -= life_amount
                logging.debug(f"Paid {life_amount} life for ability")
        
        # Check for "remove X counters"
        if "remove" in cost_text and "counter" in cost_text:
            counter_match = re.search(r"remove (\d+) ([^:,]+) counters? from", cost_text)
            if counter_match:
                counter_amount = int(counter_match.group(1))
                counter_type = counter_match.group(2).strip()
                
                # Check if permanent has enough counters
                card = game_state._safe_get_card(self.card_id)
                if not hasattr(card, 'counters') or counter_type not in card.counters or card.counters[counter_type] < counter_amount:
                    logging.debug(f"Cannot pay counter removal cost: not enough {counter_type} counters")
                    return False
                    
                # Remove counters
                card.counters[counter_type] -= counter_amount
                
                # Apply counter removal effects
                if counter_type == "+1/+1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
                    card.power -= counter_amount
                    card.toughness -= counter_amount
                elif counter_type == "-1/-1" and hasattr(card, 'power') and hasattr(card, 'toughness'):
                    card.power += counter_amount
                    card.toughness += counter_amount
                    
                logging.debug(f"Removed {counter_amount} {counter_type} counters from {card.name}")
        
        # Handle mana costs using enhanced mana system if available
        if hasattr(game_state, 'mana_system') and game_state.mana_system:
            # Check for mana components in the cost
            if any(symbol in cost_text for symbol in ["{w}", "{u}", "{b}", "{r}", "{g}", "{c}", "{x}"]):
                mana_cost = game_state.mana_system.parse_mana_cost(cost_text)
                if not game_state.mana_system.can_pay_mana_cost(controller, mana_cost):
                    logging.debug(f"Cannot pay mana cost: {cost_text}")
                    return False
                
                # Pay the mana cost
                game_state.mana_system.pay_mana_cost(controller, mana_cost)
                logging.debug(f"Paid mana cost: {cost_text}")
                
        logging.debug(f"Successfully paid all costs for ability of {game_state._safe_get_card(self.card_id).name if hasattr(game_state._safe_get_card(self.card_id), 'name') else self.card_id}")
        return all_costs_paid
    
    def _can_sacrifice(self, game_state, controller, sacrifice_req):
        """Check if controller can meet sacrifice requirements"""
        # Handle various sacrifice costs
        card_type_requirement = None
        
        # Common patterns
        if 'creature' in sacrifice_req:
            card_type_requirement = 'creature'
        elif 'artifact' in sacrifice_req:
            card_type_requirement = 'artifact'
        elif 'land' in sacrifice_req:
            card_type_requirement = 'land'
        elif 'permanent' in sacrifice_req:
            # Any permanent can be sacrificed
            return len(controller["battlefield"]) > 0
        
        if card_type_requirement:
            # Check if player controls any permanents of the required type
            for card_id in controller["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and card_type_requirement in card.card_types:
                    return True
            return False
            
        # If we reach here, assume the cost can be paid
        return True
    
    def _pay_generic_mana(self, game_state, controller, amount):
        """Pay generic mana cost using available colored mana"""
        # First use colorless mana if available
        colorless_used = min(controller["mana_pool"].get('C', 0), amount)
        controller["mana_pool"]['C'] -= colorless_used
        amount -= colorless_used
        
        # Then use colored mana in a reasonable order (usually save WUBRG for colored costs)
        colors = ['G', 'R', 'B', 'U', 'W']  # Priority order for spending
        
        for color in colors:
            if amount <= 0:
                break
                
            available = controller["mana_pool"].get(color, 0)
            used = min(available, amount)
            controller["mana_pool"][color] -= used
            amount -= used
            
        if amount > 0:
            logging.warning(f"Failed to pay all generic mana costs, {amount} mana short")
            
        return amount <= 0
    
    def _pay_sacrifice_cost(self, game_state, controller, sacrifice_req):
        """Pay a sacrifice cost"""
        # Parse the sacrifice requirement
        card_type_requirement = None
        
        if 'creature' in sacrifice_req:
            card_type_requirement = 'creature'
        elif 'artifact' in sacrifice_req:
            card_type_requirement = 'artifact'
        elif 'land' in sacrifice_req:
            card_type_requirement = 'land'
        elif 'permanent' in sacrifice_req or sacrifice_req.strip() == 'it':
            # Sacrificing self or any permanent
            if sacrifice_req.strip() == 'it':
                # Sacrifice the ability source itself
                target_id = self.card_id
            else:
                # Just pick the first permanent
                target_id = controller["battlefield"][0] if controller["battlefield"] else None
        else:
            # Default to sacrificing the source of the ability
            target_id = self.card_id
        
        # Find a card to sacrifice
        if card_type_requirement:
            # Find a permanent of the required type
            for card_id in controller["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and card_type_requirement in card.card_types:
                    target_id = card_id
                    break
        
        # Perform the sacrifice
        if target_id and target_id in controller["battlefield"]:
            game_state.move_card(target_id, controller, "battlefield", controller, "graveyard")
            logging.debug(f"Sacrificed {game_state._safe_get_card(target_id).name} to pay ability cost")
        else:
            logging.warning("Failed to find a valid permanent to sacrifice")
    
    def _pay_discard_cost(self, game_state, controller, discard_req):
        """Pay a discard cost"""
        # Parse the discard requirement
        if 'a card' in discard_req or 'card' in discard_req:
            # Discard any card
            if controller["hand"]:
                card_id = controller["hand"][0]  # Just pick the first card
                game_state.move_card(card_id, controller, "hand", controller, "graveyard")
                logging.debug(f"Discarded {game_state._safe_get_card(card_id).name} to pay ability cost")
        elif 'your hand' in discard_req:
            # Discard entire hand
            while controller["hand"]:
                card_id = controller["hand"][0]
                game_state.move_card(card_id, controller, "hand", controller, "graveyard")
            logging.debug(f"Discarded entire hand to pay ability cost")
            
    def _can_exile_from_graveyard(self, game_state, controller, exile_req):
        """Check if controller can meet exile from graveyard requirements"""
        # Handle various exile requirements
        if exile_req == "a creature card":
            for card_id in controller["graveyard"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    return True
            return False
        elif exile_req == "a card":
            return len(controller["graveyard"]) > 0
        
        # Default to assuming requirement can be met
        return True

    def _pay_exile_from_graveyard_cost(self, game_state, controller, exile_req):
        """Pay an exile from graveyard cost"""
        # Find appropriate card to exile
        target_id = None
        
        if exile_req == "a creature card":
            for card_id in controller["graveyard"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    target_id = card_id
                    break
        elif exile_req == "a card":
            if controller["graveyard"]:
                target_id = controller["graveyard"][0]
        
        # Perform the exile
        if target_id:
            game_state.move_card(target_id, controller, "graveyard", controller, "exile")
            card = game_state._safe_get_card(target_id)
            logging.debug(f"Exiled {card.name if card else target_id} from graveyard to pay cost")

    def _can_exile_from_hand(self, game_state, controller, exile_req):
        """Check if controller can meet exile from hand requirements"""
        if not controller["hand"]:
            return False
        
        if exile_req == "a creature card":
            for card_id in controller["hand"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    return True
            return False
        
        # Default to assuming requirement can be met if hand has cards
        return len(controller["hand"]) > 0

    def _pay_exile_from_hand_cost(self, game_state, controller, exile_req):
        """Pay an exile from hand cost"""
        # Find appropriate card to exile
        target_id = None
        
        if exile_req == "a creature card":
            for card_id in controller["hand"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    target_id = card_id
                    break
        elif exile_req == "a card":
            if controller["hand"]:
                target_id = controller["hand"][0]
        
        # Perform the exile
        if target_id:
            game_state.move_card(target_id, controller, "hand", controller, "exile")
            card = game_state._safe_get_card(target_id)
            logging.debug(f"Exiled {card.name if card else target_id} from hand to pay cost")


class TriggeredAbility(Ability):
    """Ability that triggers on certain game events"""
    def __init__(self, card_id, trigger_condition, effect, effect_text="", additional_condition=None):
        super().__init__(card_id, effect_text)
        self.trigger_condition = trigger_condition.lower()
        self.effect = effect.lower()
        self.additional_condition = additional_condition  # Extra condition beyond the trigger
        
    def can_trigger(self, event_type, context=None):
        """Check if the ability should trigger based on an event and additional conditions with improved pattern matching."""
        # Define trigger condition patterns with more flexibility
        trigger_conditions = {
            "ENTERS_BATTLEFIELD": [
                r"when(ever)?\s+.*enters the battlefield",
                r"when(ever)?\s+.*enters",
                r"when(ever)?\s+.*comes into play"
            ],
            "ATTACKS": [
                r"when(ever)?\s+.*attacks",
                r"when(ever)?\s+.*declares? attack",
                r"when(ever)?\s+.*becomes? attacking"
            ],
            "BLOCKS": [
                r"when(ever)?\s+.*blocks",
                r"when(ever)?\s+.*declares? block",
                r"when(ever)?\s+.*becomes? blocking"
            ],
            "DEALS_DAMAGE": [
                r"when(ever)?\s+.*deals damage",
                r"when(ever)?\s+.*deals combat damage",
                r"when(ever)?\s+damage is dealt"
            ],
            "DIES": [
                r"when(ever)?\s+.*dies",
                r"when(ever)?\s+.*is put into a graveyard from the battlefield",
                r"when(ever)?\s+.*goes to the graveyard"
            ],
            "CASTS": [
                r"when(ever)?\s+.*cast",
                r"when(ever)?\s+.*casts?",
                r"when(ever)?\s+.*play"
            ],
            "BEGINNING_OF_UPKEEP": [
                r"at the beginning of (your|each) upkeep",
                r"at the beginning of the upkeep",
                r"during (your|each) upkeep"
            ],
            "END_OF_TURN": [
                r"at the end of (your|each) turn",
                r"at the beginning of (your|the|each) end step",
                r"at the end of (the|each) turn"
            ],
            "DISCARD": [
                r"when(ever)?\s+.*discard",
                r"when(ever)?\s+.*discards?",
                r"when(ever)?\s+.*is discarded"
            ],
            "DOOR_UNLOCKED": [
                r"when(ever)?\s+.*unlock",
                r"when(ever)?\s+.*unlocks?",
                r"when(ever)?\s+.*becomes? unlocked"
            ],
            "GAIN_LIFE": [
                r"when(ever)?\s+.*gain(s)? life",
                r"when(ever)?\s+.*life is gained"
            ],
            "LOSE_LIFE": [
                r"when(ever)?\s+.*lose(s)? life",
                r"when(ever)?\s+.*life is lost"
            ]
        }
        
        # Helper function to check if text matches any pattern
        def matches_any_pattern(text, patterns):
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            return False
        
        # Get condition patterns for this event
        event_patterns = trigger_conditions.get(event_type, [])
        
        # Check if our trigger condition matches any of the patterns
        if matches_any_pattern(self.trigger_condition, event_patterns):
            # Parse for any conditional clause in the trigger text
            condition_clause = self._extract_condition_clause(self.effect_text)
            
            # If there's a conditional clause, evaluate it
            if condition_clause:
                if not self._evaluate_condition(condition_clause, context):
                    return False
            
            # Check explicitly added additional condition if present
            if self.additional_condition and context:
                if not self._check_additional_condition(context):
                    return False
                    
            return True
                    
        return False

    def resolve_with_targets(self, game_state, controller, targets=None):
        """Resolve this ability with specific targets."""
        return self._resolve_ability_effect(game_state, controller, targets)

    def _extract_condition_clause(self, text):
        """Extract a conditional clause from ability text (usually after 'if' or 'only if')"""
        if not text:
            return None
            
        # Common patterns for conditions in ability text
        patterns = [
            r'if ([^,.]+)',  # Match "if X"
            r'only if ([^,.]+)',  # Match "only if X"
            r'unless ([^,.]+)'  # Match "unless X"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text.lower())
            if match:
                return match.group(1).strip()
                
        return None

    def _evaluate_condition(self, condition, context):
        """Evaluate if a condition is met based on the game context"""
        if not condition or not context:
            return True  # Default to true if no condition or context
            
        game_state = context.get('game_state')
        controller = context.get('controller')
        if not game_state or not controller:
            return True  # Can't evaluate without game state and controller
        
        # Evaluate common condition patterns
        condition = condition.lower()
        
        # "if you control X or more Y"
        control_match = re.search(r'you control (\w+) or more ([^,.]+)', condition)
        if control_match:
            count_text, permanent_type = control_match.groups()
            required_count = text_to_number(count_text)
            
            # Count matching permanents
            matching_count = 0
            for card_id in controller["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line'):
                    continue
                    
                # Check if card type matches
                if permanent_type.lower() in card.type_line.lower():
                    matching_count += 1
            
            return matching_count >= required_count
        
        # "if you have X or more life/cards in hand"
        resource_match = re.search(r'you have (\w+) or more ([^,.]+)', condition)
        if resource_match:
            count_text, resource_type = resource_match.groups()
            required_count = text_to_number(count_text)
            
            if "life" in resource_type:
                return controller["life"] >= required_count
            elif "cards in hand" in resource_type or "cards in your hand" in resource_type:
                return len(controller["hand"]) >= required_count
        
        # "if an opponent controls X or more Y"
        opponent_match = re.search(r'(an opponent|your opponent) controls (\w+) or more ([^,.]+)', condition)
        if opponent_match:
            _, count_text, permanent_type = opponent_match.groups()
            required_count = text_to_number(count_text)
            
            # Get opponent
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            
            # Count matching permanents
            matching_count = 0
            for card_id in opponent["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'type_line'):
                    continue
                    
                # Check if card type matches
                if permanent_type.lower() in card.type_line.lower():
                    matching_count += 1
            
            return matching_count >= required_count
        
        # "if you've X or more Y this turn" (e.g., drawn cards, gained life)
        action_match = re.search(r'you\'ve (\w+) (\w+) or more ([^,.]+) this turn', condition)
        if action_match:
            action, count_text, what = action_match.groups()
            required_count = text_to_number(count_text)
            
            # Check tracking variables based on the action type
            if action == "drawn" and "cards" in what and hasattr(game_state, 'cards_drawn_this_turn'):
                cards_drawn = game_state.cards_drawn_this_turn.get(controller, 0)
                return cards_drawn >= required_count
            elif action == "gained" and "life" in what and hasattr(game_state, 'life_gained_this_turn'):
                life_gained = game_state.life_gained_this_turn.get(controller, 0)
                return life_gained >= required_count
        
        # Default to true for unrecognized conditions
        return True

    # Using the text_to_number utility function from ability_utils
    
    def _check_additional_condition(self, context):
        """Check if any additional conditions are met"""
        if not self.additional_condition:
            return True
            
        # Process common conditional patterns
        condition = self.additional_condition.lower()
        
        # "if you control X" conditions
        if "if you control" in condition:
            controller = context.get("controller")
            if not controller:
                return False
                
            # Extract what needs to be controlled
            import re
            match = re.search(r"if you control (a|an|[\d]+) ([\w\s]+)", condition)
            if match:
                count_req = match.group(1)
                permanent_type = match.group(2)
                
                # Count matching permanents
                count = 0
                for perm_id in controller["battlefield"]:
                    perm = self.game_state._safe_get_card(perm_id)
                    if not perm or not hasattr(perm, 'type_line'):
                        continue
                        
                    # Check if permanent matches the required type
                    if permanent_type in perm.type_line.lower():
                        count += 1
                
                # Check if count requirement is met
                if count_req.isdigit():
                    return count >= int(count_req)
                else:
                    return count >= 1  # "a" or "an" requires at least 1
        
        # Default to True if we can't parse the condition
        return True
    
    def resolve(self, game_state, controller):
        """Resolve this triggered ability using effect classes."""
        card = game_state._safe_get_card(self.card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {self.card_id} not found")
            return
            
        try:
            # Convert effect text to lowercase for easier parsing
            effect = self.effect.lower() if hasattr(self, 'effect') else ""
            
            # Check if ability requires targeting
            requires_target = "target" in effect
            targets = None
            
            # Handle targeting
            if requires_target:
                # Use targeting system if available
                if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                    targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                        self.card_id, self.effect_text, controller)
                    
                    # Check if targeting failed
                    if not targets or (isinstance(targets, dict) and not any(targets.values())):
                        logging.debug(f"Targeting failed for triggered ability: {self.effect_text}")
                        return
                else:
                    # Simplified targeting
                    targets = self._resolve_simple_targeting(game_state, controller, effect)
            
            # Create and apply appropriate effects
            effects = self._create_ability_effects(effect, targets)
            
            for effect_obj in effects:
                effect_obj.apply(game_state, self.card_id, controller, targets)
                
            logging.debug(f"Resolved triggered ability: {self.effect_text}")
                
        except Exception as e:
            logging.error(f"Error resolving triggered ability: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
        
    # Using the base class implementation from Ability for _create_ability_effects and _resolve_simple_targeting

class StaticAbility(Ability):
    """Continuous ability that affects the game state"""
    def __init__(self, card_id, effect, effect_text=""):
        super().__init__(card_id, effect_text)
        self.effect = effect.lower()
        
    def apply(self, game_state, affected_cards=None):
        """Apply the static ability's effect to the game state with comprehensive handling."""
        effect = self.effect.lower()
        
        # Find affected cards if none specified
        if affected_cards is None:
            # Find controller
            controller = None
            for player in [game_state.p1, game_state.p2]:
                if self.card_id in player["battlefield"]:
                    controller = player
                    break
            
            if controller:
                affected_cards = self.get_affected_cards(game_state, controller)
        
        # If no cards are affected, nothing to do
        if not affected_cards:
            return False
        
        # Create a dictionary mapping effect patterns to handler methods
        effect_handlers = {
            r"creatures (you control )?get \+(\d+)/\+(\d+)": self._apply_anthem_effect,
            r"creatures (you control )?have (flying|trample|vigilance|haste|hexproof|deathtouch|lifelink)": self._apply_keyword_granting,
            r"creatures (you control )?gain (flying|trample|vigilance|haste|hexproof|deathtouch|lifelink)": self._apply_keyword_granting,
            r"(all|each) (creature|artifact|land)s? (are|is|become) ([a-z]+)": self._apply_type_changing,
            r"([a-z]+) spells you cast cost \{(\d+)\} less to cast": self._apply_cost_reduction,
            r"can't attack": self._apply_prevention_effect,
            r"can't block": self._apply_prevention_effect,
            r"spells your opponents cast cost \{(\d+)\} more": self._apply_cost_increase,
            r"damage is doubled": self._apply_damage_modification,
            r"damage is prevented": self._apply_damage_modification,
            r"enters the battlefield tapped": self._apply_etb_effect,
            r"doesn't untap during (its controller's|your) untap step": self._apply_untap_prevention
        }
        
        # Iterate through effect patterns and apply matching handlers
        for pattern, handler in effect_handlers.items():
            match = re.search(pattern, effect)
            if match:
                args = [game_state, affected_cards, effect, match]
                result = handler(*args)
                if result:
                    return True
        
        # If no specific handler matched, try generic layer-based handling
        if hasattr(game_state, 'layer_system') and game_state.layer_system:
            # Try to determine the appropriate layer for this effect
            layer = self._determine_layer_for_effect(effect)
            if layer:
                # Register generic effect in layer system
                game_state.layer_system.register_effect({
                    'source_id': self.card_id,
                    'layer': layer,
                    'affected_ids': affected_cards,
                    'effect_text': effect,
                    'duration': 'permanent',
                    'condition': lambda: self.card_id in self._find_all_battlefield_cards(game_state)
                })
                return True
        
        # Default behavior for unrecognized effects
        return False

    # Add helper method to determine which layer an effect belongs to
    def _determine_layer_for_effect(self, effect):
        """Determine the appropriate layer for an effect based on its text."""
        # Layer 1: Copy effects
        if "copy" in effect or "becomes a copy" in effect:
            return 1
        
        # Layer 2: Control-changing effects
        if "gain control" in effect or "exchange control" in effect:
            return 2
        
        # Layer 3: Text-changing effects
        if "text becomes" in effect or "lose all abilities" in effect:
            return 3
        
        # Layer 4: Type-changing effects
        if "become" in effect and any(type_word in effect for type_word in 
                                    ["artifact", "creature", "enchantment", "land", "planeswalker"]):
            return 4
        
        # Layer 5: Color-changing effects
        if "becomes" in effect and any(color in effect for color in 
                                    ["white", "blue", "black", "red", "green", "colorless"]):
            return 5
        
        # Layer 6: Ability-adding/removing effects
        if "gain" in effect or "have" in effect or "lose" in effect:
            if any(keyword in effect for keyword in 
                ["flying", "trample", "vigilance", "haste", "hexproof", "deathtouch", "lifelink"]):
                return 6
        
        # Layer 7: Power/toughness
        if "get +" in effect or "+1/+1" in effect or "-1/-1" in effect:
            return 7
        
        # Default to no specific layer
        return None

    def _find_all_battlefield_cards(self, game_state):
        """Helper function to find all cards on the battlefield."""
        battlefield_cards = []
        for player in [game_state.p1, game_state.p2]:
            battlefield_cards.extend(player["battlefield"])
        return battlefield_cards
    
    def _apply_anthem_effect(self, game_state, affected_cards, effect, match):
        """Apply anthem effect that boosts power/toughness of creatures."""
        if not affected_cards:
            return False
            
        # Extract power and toughness boost values
        power_boost, toughness_boost = 0, 0
        if match:
            power_toughness = match.group(3)
            if "/" in power_toughness:
                parts = power_toughness.split("/")
                try:
                    power_boost = int(parts[0])
                    toughness_boost = int(parts[1])
                except (ValueError, IndexError):
                    logging.warning(f"Could not parse anthem effect values: {power_toughness}")
                    return False
        
        # Apply the boost to all affected cards
        for card_id in affected_cards:
            card = game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'card_types') or 'creature' not in card.card_types:
                continue
                
            # Update power and toughness
            if hasattr(card, 'power') and hasattr(card, 'toughness'):
                card.power += power_boost
                card.toughness += toughness_boost
        
        logging.debug(f"Applied anthem effect: {power_boost}/+{toughness_boost} to {len(affected_cards)} creatures")
        return True

    def _apply_keyword_granting(self, game_state, affected_cards, effect, match):
        """Grant keyword abilities to affected cards."""
        if not affected_cards:
            return False
            
        # Determine which keyword to grant
        keyword = None
        if match:
            keyword_text = match.group(2)
            keyword = keyword_text.strip().lower() if keyword_text else None
        
        if not keyword:
            return False
            
        # Map of keywords to their index in the keywords array
        keyword_indices = {
            'flying': 0, 'trample': 1, 'hexproof': 2, 'lifelink': 3, 'deathtouch': 4,
            'first strike': 5, 'double strike': 6, 'vigilance': 7, 'flash': 8, 'haste': 9, 'menace': 10
        }
        
        # Apply keyword to all affected cards
        for card_id in affected_cards:
            card = game_state._safe_get_card(card_id)
            if not card:
                continue
                
            # If the card has a keywords array, update it
            if hasattr(card, 'keywords'):
                if keyword in keyword_indices and len(card.keywords) > keyword_indices[keyword]:
                    card.keywords[keyword_indices[keyword]] = 1
                else:
                    # Otherwise, add to oracle text
                    if not hasattr(card, 'granted_keywords'):
                        card.granted_keywords = set()
                    card.granted_keywords.add(keyword)
            else:
                # If no keywords array, create one
                card.keywords = [0] * 11
                if keyword in keyword_indices:
                    card.keywords[keyword_indices[keyword]] = 1
        
        logging.debug(f"Granted keyword '{keyword}' to {len(affected_cards)} permanents")
        return True

    def _apply_type_changing(self, game_state, affected_cards, effect, match):
        """Change permanent types (e.g., 'all lands are creatures')."""
        if not affected_cards:
            return False
            
        # Extract the new type
        new_type = None
        if match:
            new_type = match.group(4).strip().lower() if len(match.groups()) >= 4 else None
        
        if not new_type:
            return False
            
        # Apply type change to all affected cards
        for card_id in affected_cards:
            card = game_state._safe_get_card(card_id)
            if not card:
                continue
                
            # Add the new type to card_types if it's not already there
            if hasattr(card, 'card_types'):
                if new_type not in card.card_types:
                    card.card_types.append(new_type)
            else:
                card.card_types = [new_type]
                
            # If changing to creature, make sure it has power/toughness
            if new_type == 'creature' and (not hasattr(card, 'power') or not hasattr(card, 'toughness')):
                # Default to 1/1 if no specific p/t is mentioned
                pt_match = re.search(r'(\d+)/(\d+)', effect)
                if pt_match:
                    card.power = int(pt_match.group(1))
                    card.toughness = int(pt_match.group(2))
                else:
                    card.power = 1
                    card.toughness = 1
        
        logging.debug(f"Changed type to '{new_type}' for {len(affected_cards)} permanents")
        return True

    def _apply_cost_reduction(self, game_state, affected_cards, effect, match):
        """Apply cost reduction to spells."""
        if not hasattr(game_state, 'cost_modifiers'):
            game_state.cost_modifiers = []
            
        # Extract cost reduction amount and spell type
        amount = 0
        spell_type = "all"
        if match:
            spell_type = match.group(1).strip().lower() if len(match.groups()) >= 1 else "all"
            amount_str = match.group(2) if len(match.groups()) >= 2 else "0"
            try:
                amount = int(amount_str)
            except ValueError:
                amount = 1  # Default if parsing fails
        
        # Create a cost modifier
        cost_mod = {
            'source_id': self.card_id,
            'amount': amount,  # Positive for reduction
            'spell_type': spell_type,
            'duration': 'static'
        }
        
        game_state.cost_modifiers.append(cost_mod)
        logging.debug(f"Applied cost reduction of {amount} to {spell_type} spells")
        return True

    def _apply_prevention_effect(self, game_state, affected_cards, effect, match):
        """Apply effects that prevent certain actions."""
        if not hasattr(game_state, 'prevention_effects'):
            game_state.prevention_effects = []
        
        # Determine what's being prevented
        prevention_type = None
        if "can't attack" in effect:
            prevention_type = "attack"
        elif "can't block" in effect:
            prevention_type = "block"
        elif "can't be blocked" in effect:
            prevention_type = "be_blocked"
        elif "can't be countered" in effect:
            prevention_type = "be_countered"
        elif "can't be targeted" in effect:
            prevention_type = "be_targeted"
        
        if not prevention_type:
            return False
        
        # Extract qualifiers from the effect
        qualifier = ""
        if match and len(match.groups()) >= 2:
            qualifier = match.group(2).strip()
        
        # Register the prevention effect
        prevention_effect = {
            'source_id': self.card_id,
            'type': prevention_type,
            'affected_cards': affected_cards.copy() if affected_cards else [],
            'qualifier': qualifier,
            'duration': 'static'
        }
        
        game_state.prevention_effects.append(prevention_effect)
        logging.debug(f"Applied prevention effect: {prevention_type} with qualifier '{qualifier}'")
        return True

    def _apply_cost_increase(self, game_state, affected_cards, effect, match):
        """Apply cost increase to spells."""
        if not hasattr(game_state, 'cost_modifiers'):
            game_state.cost_modifiers = []
            
        # Extract cost increase amount and spell type
        amount = 0
        spell_type = "all"
        if match:
            spell_type = "opponent" if "your opponents cast" in effect else "all"
            amount_str = match.group(1) if len(match.groups()) >= 1 else "0"
            try:
                amount = int(amount_str)
            except ValueError:
                amount = 1  # Default if parsing fails
        
        # Create a cost modifier (negative amount for increase)
        cost_mod = {
            'source_id': self.card_id,
            'amount': -amount,  # Negative for increase
            'spell_type': spell_type,
            'duration': 'static'
        }
        
        game_state.cost_modifiers.append(cost_mod)
        logging.debug(f"Applied cost increase of {amount} to {spell_type} spells")
        return True

    def _apply_damage_modification(self, game_state, affected_cards, effect, match):
        """Apply damage modification effects like doubling or prevention."""
        if not hasattr(game_state, 'damage_modifiers'):
            game_state.damage_modifiers = []
        
        # Determine modification type
        modifier = None
        if "damage is doubled" in effect:
            modifier = "double"
        elif "damage is prevented" in effect or "prevent all damage" in effect:
            modifier = "prevent"
        elif "damage can't be prevented" in effect:
            modifier = "cant_prevent"
        
        if not modifier:
            return False
        
        # Register the damage modifier
        damage_mod = {
            'source_id': self.card_id,
            'modifier': modifier,
            'affected_cards': affected_cards.copy() if affected_cards else [],
            'duration': 'static'
        }
        
        game_state.damage_modifiers.append(damage_mod)
        logging.debug(f"Applied damage modification: {modifier}")
        return True

    def _apply_etb_effect(self, game_state, affected_cards, effect, match):
        """Apply enters-the-battlefield replacement effects."""
        if not hasattr(game_state, 'replacement_effects'):
            return False
        
        # Create a replacement effect for ETB
        replacement_effect = {
            'source_id': self.card_id,
            'event_type': 'ENTERS_BATTLEFIELD',
            'replacement': effect,
            'affected_cards': affected_cards.copy() if affected_cards else [],
            'duration': 'static'
        }
        
        # Register the replacement effect
        game_state.replacement_effects.register_effect(replacement_effect)
        logging.debug(f"Applied ETB replacement effect: {effect}")
        return True

    def _apply_untap_prevention(self, game_state, affected_cards, effect, match):
        """Apply effects that prevent permanents from untapping."""
        if not hasattr(game_state, 'untap_prevention'):
            game_state.untap_prevention = []
        
        # Extract qualifiers from the effect
        controller = "its controller"
        if match and len(match.groups()) >= 1:
            controller = match.group(1).strip()
        
        # Register the untap prevention effect
        untap_effect = {
            'source_id': self.card_id,
            'affected_cards': affected_cards.copy() if affected_cards else [],
            'controller': controller,
            'duration': 'static'
        }
        
        game_state.untap_prevention.append(untap_effect)
        logging.debug(f"Applied untap prevention effect for {controller}'s untap step")
        return True

    def _apply_pt_modification(self, game_state, affected_cards, effect):
        """Apply power/toughness modifications (Layer 7)"""
        # Handle +X/+Y boosts
        boost_pattern = r'get \+(\d+)/\+(\d+)'
        match = re.search(boost_pattern, effect)
        if match and affected_cards:
            power_boost = int(match.group(1))
            toughness_boost = int(match.group(2))
            
            for card_id in affected_cards:
                card = game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'power') or not hasattr(card, 'toughness'):
                    continue
                    
                # Use layer system if available
                if hasattr(game_state, 'layer_system') and game_state.layer_system:
                    game_state.layer_system.add_pt_effect(card_id, power_boost, toughness_boost, self.card_id)
                else:
                    # Direct modification fallback
                    card.power += power_boost
                    card.toughness += toughness_boost
                
                logging.debug(f"Applied +{power_boost}/+{toughness_boost} to {card.name}")
            return True
        
        # Handle -X/-Y penalties
        penalty_pattern = r'get -(\d+)/-(\d+)'
        match = re.search(penalty_pattern, effect)
        if match and affected_cards:
            power_penalty = int(match.group(1))
            toughness_penalty = int(match.group(2))
            
            for card_id in affected_cards:
                card = game_state._safe_get_card(card_id)
                if not card or not hasattr(card, 'power') or not hasattr(card, 'toughness'):
                    continue
                    
                # Use layer system if available
                if hasattr(game_state, 'layer_system') and game_state.layer_system:
                    game_state.layer_system.add_pt_effect(card_id, -power_penalty, -toughness_penalty, self.card_id)
                else:
                    # Direct modification fallback
                    card.power = max(0, card.power - power_penalty)
                    card.toughness = max(0, card.toughness - toughness_penalty)
                
                logging.debug(f"Applied -{power_penalty}/-{toughness_penalty} to {card.name}")
            return True
        
        return False

    def _apply_ability_granting(self, game_state, affected_cards, effect):
        """Apply ability granting effects (Layer 6)"""
        if not affected_cards:
            return False
            
        # Map of ability text to keyword indices
        ability_map = {
            'flying': 0, 'trample': 1, 'hexproof': 2, 'lifelink': 3, 'deathtouch': 4,
            'first strike': 5, 'double strike': 6, 'vigilance': 7, 'flash': 8, 'haste': 9, 'menace': 10
        }
        
        # Extract the granted ability
        granted_ability = None
        for ability in ability_map:
            if f"gain {ability}" in effect or f"have {ability}" in effect:
                granted_ability = ability
                break
        
        if not granted_ability:
            return False
        
        ability_idx = ability_map.get(granted_ability)
        
        for card_id in affected_cards:
            card = game_state._safe_get_card(card_id)
            if not card:
                continue
                
            # Use layer system if available
            if hasattr(game_state, 'layer_system') and game_state.layer_system:
                game_state.layer_system.add_ability_effect(card_id, granted_ability, self.card_id)
            else:
                # Direct modification fallback
                if hasattr(card, 'keywords') and len(card.keywords) > ability_idx:
                    card.keywords[ability_idx] = 1
            
            logging.debug(f"Granted {granted_ability} to {card.name}")
        
        return True

    def _apply_type_modification(self, game_state, affected_cards, effect):
        """Apply type changing effects (Layer 4)"""
        if not affected_cards:
            return False
            
        # Detect what type is being set
        new_type = None
        type_pattern = r'(is|are) a(n)? ([a-zA-Z]+)'
        match = re.search(type_pattern, effect)
        if match:
            new_type = match.group(3).lower()
        
        if not new_type:
            return False
        
        for card_id in affected_cards:
            card = game_state._safe_get_card(card_id)
            if not card:
                continue
                
            # Use layer system if available
            if hasattr(game_state, 'layer_system') and game_state.layer_system:
                game_state.layer_system.add_type_effect(card_id, new_type, self.card_id)
            else:
                # Direct modification fallback
                if hasattr(card, 'card_types') and new_type not in card.card_types:
                    card.card_types.append(new_type)
            
            logging.debug(f"Changed type of {card.name} to include {new_type}")
        
        return True

    def _apply_cost_modification(self, game_state, affected_cards, effect):
        """Apply cost modification effects"""
        # Extract cost change amount
        amount = 0
        cost_pattern = r'cost \{(\d+)\} (less|more)'
        match = re.search(cost_pattern, effect)
        if match:
            amount = int(match.group(1))
            if match.group(2) == 'more':
                amount = -amount  # Negative for increased cost
        
        if amount == 0:
            return False
        
        # Apply cost modification to the game state
        if hasattr(game_state, 'cost_modifiers'):
            game_state.cost_modifiers.append({
                'source': self.card_id,
                'amount': amount,
                'affected_cards': affected_cards,
                'condition': self.additional_condition
            })
            logging.debug(f"Applied cost modification of {amount} to affected cards")
            return True
        
        return False

    def _apply_prevention_effect(self, game_state, affected_cards, effect):
        """Apply prevention effects"""
        # Identify what is being prevented
        prevention_type = None
        
        if "can't attack" in effect:
            prevention_type = "attack"
        elif "can't block" in effect:
            prevention_type = "block"
        elif "can't be blocked" in effect:
            prevention_type = "be_blocked"
        elif "can't be targeted" in effect:
            prevention_type = "be_targeted"
        elif "can't be countered" in effect:
            prevention_type = "be_countered"
        
        if not prevention_type or not affected_cards:
            return False
        
        # Register the prevention effect
        if hasattr(game_state, 'prevention_effects'):
            game_state.prevention_effects.append({
                'source': self.card_id,
                'type': prevention_type,
                'affected_cards': affected_cards
            })
            logging.debug(f"Applied prevention effect: {prevention_type}")
            return True
        
        return False

    def _apply_mana_effect(self, game_state, affected_cards, effect):
        """Apply mana production modification effects"""
        # Extract mana colors being added
        colors = []
        color_words = ['white', 'blue', 'black', 'red', 'green']
        color_symbols = ['W', 'U', 'B', 'R', 'G']
        
        for i, color in enumerate(color_words):
            if color in effect:
                colors.append(color_symbols[i])
        
        if not colors or not affected_cards:
            return False
        
        # Register the mana effect
        if hasattr(game_state, 'mana_effects'):
            game_state.mana_effects.append({
                'source': self.card_id,
                'colors': colors,
                'affected_cards': affected_cards
            })
            logging.debug(f"Applied mana effect: adding {colors} to affected lands")
            return True
        
        return False

    def _apply_damage_modification(self, game_state, affected_cards, effect):
        """Apply damage modification effects"""
        modifier = None
        
        if 'damage is doubled' in effect:
            modifier = 'double'
        elif 'damage is prevented' in effect:
            modifier = 'prevent'
        
        if not modifier or not affected_cards:
            return False
        
        # Register the damage modification
        if hasattr(game_state, 'damage_modifiers'):
            game_state.damage_modifiers.append({
                'source': self.card_id,
                'modifier': modifier,
                'affected_cards': affected_cards
            })
            logging.debug(f"Applied damage modifier: {modifier}")
            return True
        
        return False
            
    def get_affected_cards(self, game_state, controller):
        """Determine which cards this static ability affects"""
        effect = self.effect.lower()
        affected_cards = []
        
        # Parse the effect to determine the scope
        if 'creatures you control' in effect:
            # Affects all of controller's creatures
            for card_id in controller["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and 'creature' in card.card_types:
                    affected_cards.append(card_id)
                    
        elif 'all creatures' in effect:
            # Affects all creatures in play
            for player in [game_state.p1, game_state.p2]:
                for card_id in player["battlefield"]:
                    card = game_state._safe_get_card(card_id)
                    if card and 'creature' in card.card_types:
                        affected_cards.append(card_id)
                        
        # Add more scopes as needed
        
        return affected_cards


class KeywordAbility(Ability):
    """Ability that represents a keyword ability like flying, trample, etc."""
    def __init__(self, card_id, keyword):
        super().__init__(card_id, keyword)
        self.keyword = keyword.lower()
        
    def can_trigger(self, event_type, context):
        """Some keywords trigger on specific events"""
        # For triggered keyword abilities like lifelink
        if self.keyword == "lifelink" and event_type == "DEALS_DAMAGE":
            return True
        elif self.keyword == "deathtouch" and event_type == "DEALS_DAMAGE":
            return True
        
        # Most keywords don't trigger
        return False
    
    def resolve(self, game_state, controller):
        """Resolve any triggered effect from the keyword"""
        if self.keyword == "lifelink" and hasattr(self, "damage_dealt"):
            controller["life"] += self.damage_dealt
            logging.debug(f"Lifelink gained {self.damage_dealt} life")
    
    def __str__(self):
        return f"KeywordAbility({self.keyword})"


class ManaAbility(ActivatedAbility):
    """Special case of activated ability that produces mana"""
    def __init__(self, card_id, cost, mana_produced, effect_text=""):
        super().__init__(card_id, cost, "", effect_text)
        self.mana_produced = mana_produced
        
    def resolve(self, game_state, controller):
        """Add the produced mana to the controller's mana pool"""
        for color, amount in self.mana_produced.items():
            controller["mana_pool"][color] += amount
            
        card_name = game_state._safe_get_card(self.card_id).name
        logging.debug(f"Mana ability of {card_name} produced {self.mana_produced}")


class AbilityEffect:
    """Base class for ability effects with improved targeting integration."""
    def __init__(self, effect_text, condition=None):
        """
        Initialize the ability effect.
        
        Args:
            effect_text: Description of the effect
            condition: Optional condition for the effect (default: None)
        """
        self.effect_text = effect_text
        self.condition = condition
        self.requires_target = "target" in effect_text.lower()
        
    def apply(self, game_state, source_id, controller, targets=None):
        """
        Apply the effect to the game state with improved targeting.
        
        Args:
            game_state: The game state instance
            source_id: ID of the source card/ability
            controller: Player who controls the effect
            targets: Dictionary of targets for the effect
            
        Returns:
            bool: Whether the effect was successfully applied
        """
        if self.requires_target and (not targets or not any(targets.values())):
            # Try to get targets using the targeting system
            if hasattr(game_state, 'targeting_system') and game_state.targeting_system:
                targets = game_state.targeting_system.resolve_targeting(
                    source_id, controller, self.effect_text)
            elif hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                    source_id, self.effect_text, controller)
                
            # Check if targeting failed
            if not targets or not any(targets.values()):
                logging.debug(f"Targeting failed for effect: {self.effect_text}")
                return False
        
        # Check condition if present
        if self.condition and not self._evaluate_condition(game_state, source_id, controller):
            logging.debug(f"Condition not met for effect: {self.effect_text}")
            return False
        
        # Call the implementation-specific effect application
        result = self._apply_effect(game_state, source_id, controller, targets)
        if result is None:
            logging.debug(f"Effect application not implemented for: {self.effect_text}")
            raise NotImplementedError(f"Effect application for '{self.effect_text}' is not implemented")
        return result
    
    def _apply_effect(self, game_state, source_id, controller, targets):
        """
        Implementation-specific effect application.
        Should be overridden by subclasses.
        """
        raise NotImplementedError(f"_apply_effect method must be implemented by subclasses for '{self.effect_text}'")
    
    def _evaluate_condition(self, game_state, source_id, controller):
        """
        Evaluate if condition is met.
        
        Args:
            game_state: The game state instance
            source_id: ID of the source card/ability
            controller: Player who controls the effect
            
        Returns:
            bool: Whether the condition is met
        """
        if not self.condition:
            return True
            
        # Implement sophisticated condition parsing and evaluation
        condition_text = self.condition.lower()
        
        # Common condition patterns
        if "if you control" in condition_text:
            # Parse what needs to be controlled
            import re
            match = re.search(r"if you control (a|an|[\d]+) ([a-zA-Z\s]+)", condition_text)
            if match:
                count_req = match.group(1)
                permanent_type = match.group(2)
                
                # Count matching permanents
                count = 0
                for perm_id in controller["battlefield"]:
                    perm = game_state._safe_get_card(perm_id)
                    if not perm or not hasattr(perm, 'type_line'):
                        continue
                        
                    # Check if permanent matches the required type
                    if permanent_type in perm.type_line.lower():
                        count += 1
                
                # Check if count requirement is met
                if count_req.isdigit():
                    return count >= int(count_req)
                else:
                    return count >= 1  # "a" or "an" requires at least 1
        
        # Default to true for unrecognized conditions
        return True

class DrawCardEffect(AbilityEffect):
    """Effect that causes players to draw cards with improved condition support."""
    def __init__(self, count=1, target="controller", condition=None):
        """
        Initialize draw card effect.
        
        Args:
            count: Number of cards to draw
            target: Who draws the cards ('controller', 'opponent', 'target_player')
            condition: Optional condition for the effect
        """
        super().__init__(f"Draw {count} card(s)", condition)
        self.count = count
        self.target = target
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply draw card effect with target handling."""
        # First, check the base class conditions
        if not super().apply(game_state, source_id, controller, targets):
            return False
        
        target_player = controller  # Default to controller
        
        # Determine target player
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2
        
        # Apply draw effect
        for _ in range(self.count):
            game_state._draw_phase(target_player)
        
        # Track draws for triggers
        if not hasattr(game_state, 'cards_drawn_this_turn'):
            game_state.cards_drawn_this_turn = {}
        
        # Use player key instead of player dict
        player_key = "p1" if target_player == game_state.p1 else "p2"
        game_state.cards_drawn_this_turn[player_key] = game_state.cards_drawn_this_turn.get(player_key, 0) + self.count
        
        logging.debug(f"Drew {self.count} card(s) for {'controller' if target_player == controller else 'opponent'}")
        return True


class GainLifeEffect(AbilityEffect):
    """Effect that causes players to gain life."""
    def __init__(self, amount, target="controller", condition=None):
        """
        Initialize life gain effect.
        
        Args:
            amount: Amount of life to gain
            target: Who gains life ('controller', 'opponent', 'target_player')
            condition: Optional condition for the effect
        """
        super().__init__(f"Gain {amount} life", condition)
        self.amount = amount
        self.target = target
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply life gain effect with target handling."""
        # First, check the base class conditions
        if not super().apply(game_state, source_id, controller, targets):
            return False
        
        target_player = controller  # Default to controller
        
        # Determine target player
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2
        
        if not target_player:
            return False
            
        # Apply life gain
        target_player["life"] += self.amount
        logging.debug(f"Gained {self.amount} life for {'controller' if target_player == controller else 'opponent'}")
        
        # Track life gain for triggers
        if not hasattr(game_state, 'life_gained_this_turn'):
            game_state.life_gained_this_turn = {}
        
        # Use player name as key to avoid unhashable dict issue
        player_key = "p1" if target_player == game_state.p1 else "p2"
        game_state.life_gained_this_turn[player_key] = game_state.life_gained_this_turn.get(player_key, 0) + self.amount
        
        # Trigger "whenever you gain life" abilities
        for card_id in target_player["battlefield"]:
            card = game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                continue
            if "whenever you gain life" in card.oracle_text.lower():
                if hasattr(game_state, 'trigger_ability'):
                    game_state.trigger_ability(card_id, "GAIN_LIFE", {
                        "amount": self.amount, 
                        "player_key": player_key  # Use string key instead of dict
                    })
                    
        return True


class DamageEffect(AbilityEffect):
    """Effect that deals damage to targets with improved targeting."""
    def __init__(self, amount, target_type="any", condition=None):
        """
        Initialize damage effect.
        
        Args:
            amount: Amount of damage to deal
            target_type: Type of valid targets ('any', 'creature', 'player', 'planeswalker')
            condition: Optional condition for the effect
        """
        super().__init__(f"Deal {amount} damage to target {target_type}", condition)
        self.amount = amount
        self.target_type = target_type
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply damage effect with proper targeting."""
        # First, check the base class conditions
        if not super().apply(game_state, source_id, controller, targets):
            return False
        
        source_card = game_state._safe_get_card(source_id)
        damage_dealt = False
        
        # Handle player targets
        if (self.target_type == "player" or self.target_type == "any") and targets and "players" in targets:
            for player_id in targets["players"]:
                target_player = game_state.p1 if player_id == "p1" else game_state.p2
                
                if target_player:
                    target_player["life"] -= self.amount
                    logging.debug(f"Dealt {self.amount} damage to player (now at {target_player['life']} life)")
                    damage_dealt = True
                    
                    # Track damage for triggers
                    if not hasattr(game_state, 'damage_this_turn'):
                        game_state.damage_this_turn = {}
                    turn = game_state.turn
                    
                    # Use turn and player key to track
                    damage_key = f"{turn}_{'p1' if target_player == game_state.p1 else 'p2'}"
                    if turn not in game_state.damage_this_turn:
                        game_state.damage_this_turn[turn] = []
                    game_state.damage_this_turn[turn].append((source_id, damage_key))
        
        # Handle creature targets
        if (self.target_type == "creature" or self.target_type == "any") and targets and "creatures" in targets:
            for creature_id in targets["creatures"]:
                # Find controller of the creature
                target_controller = None
                for player in [game_state.p1, game_state.p2]:
                    if creature_id in player["battlefield"]:
                        target_controller = player
                        break
                
                if not target_controller:
                    continue
                    
                target = game_state._safe_get_card(creature_id)
                if not target:
                    continue
                
                # Track damage
                if not hasattr(target_controller, "damage_counters"):
                    target_controller["damage_counters"] = {}
                target_controller["damage_counters"][creature_id] = target_controller["damage_counters"].get(creature_id, 0) + self.amount
                
                logging.debug(f"Dealt {self.amount} damage to {target.name}")
                damage_dealt = True
                
                # Check for deathtouch
                has_deathtouch = False
                if source_card and hasattr(source_card, 'oracle_text'):
                    has_deathtouch = "deathtouch" in source_card.oracle_text.lower()
                
                # Check if lethal damage
                if has_deathtouch or (hasattr(target, 'toughness') and 
                                    target_controller["damage_counters"].get(creature_id, 0) >= target.toughness):
                    # Check for indestructible
                    if not (hasattr(target, 'oracle_text') and "indestructible" in target.oracle_text.lower()):
                        # Destroy the creature
                        game_state.move_card(creature_id, target_controller, "battlefield", target_controller, "graveyard")
                        logging.debug(f"Creature {target.name} destroyed by damage")
        
        return damage_dealt

class AddCountersEffect(AbilityEffect):
    """Effect that adds counters to permanents."""
    def __init__(self, counter_type, count=1, target_type="creature"):
        """
        Initialize counter adding effect.
        
        Args:
            counter_type: Type of counter to add (e.g., '+1/+1', '-1/-1', 'loyalty')
            count: Number of counters to add
            target_type: Type of target ('creature', 'artifact', 'planeswalker', 'self')
        """
        super().__init__(f"Put {count} {counter_type} counter(s) on target {target_type}")
        self.counter_type = counter_type
        self.count = count
        self.target_type = target_type
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply counter adding effect with target handling."""
        applied = False
        
        # Handle targeting based on target type
        target_ids = []
        
        if self.target_type == "self":
            target_ids = [source_id]
        elif targets:
            if self.target_type == "creature" and "creatures" in targets:
                target_ids = targets["creatures"]
            elif self.target_type == "artifact" and "artifacts" in targets:
                target_ids = targets["artifacts"]
            elif self.target_type == "planeswalker" and "planeswalkers" in targets:
                target_ids = targets["planeswalkers"]
            elif self.target_type == "permanent" and "permanents" in targets:
                target_ids = targets["permanents"]
        
        # Apply counters to each target
        for target_id in target_ids:
            # Find controller of the target
            target_controller = None
            for player in [game_state.p1, game_state.p2]:
                for zone in ["battlefield"]:  # Could expand to other zones if needed
                    if target_id in player[zone]:
                        target_controller = player
                        break
                if target_controller:
                    break
            
            if not target_controller:
                continue
                
            target = game_state._safe_get_card(target_id)
            if not target:
                continue
                
            # Initialize counters if needed
            if not hasattr(target, "counters"):
                target.counters = {}
                
            # Add counters
            target.counters[self.counter_type] = target.counters.get(self.counter_type, 0) + self.count
            
            # Apply effects based on counter type
            if self.counter_type == "+1/+1" and hasattr(target, 'power') and hasattr(target, 'toughness'):
                target.power += self.count
                target.toughness += self.count
                logging.debug(f"Added {self.count} +1/+1 counters to {target.name}")
            elif self.counter_type == "-1/-1" and hasattr(target, 'power') and hasattr(target, 'toughness'):
                target.power = max(0, target.power - self.count)
                target.toughness = max(0, target.toughness - self.count)
                logging.debug(f"Added {self.count} -1/-1 counters to {target.name}")
            elif self.counter_type == "loyalty" and hasattr(target, 'loyalty'):
                target.loyalty += self.count
                logging.debug(f"Added {self.count} loyalty counters to {target.name}")
            else:
                logging.debug(f"Added {self.count} {self.counter_type} counters to {target.name}")
                
            applied = True
            
            # Check if the creature dies from -1/-1 counters
            if self.counter_type == "-1/-1" and hasattr(target, 'toughness') and target.toughness <= 0:
                game_state.move_card(target_id, target_controller, "battlefield", target_controller, "graveyard")
                logging.debug(f"Creature {target.name} died from -1/-1 counters")
        
        return applied

class CreateTokenEffect(AbilityEffect):
    """Effect that creates token creatures."""
    def __init__(self, power, toughness, creature_type="Creature", count=1, keywords=None, controller_gets=True, condition=None):
        """
        Initialize token creation effect.
        
        Args:
            power: Power of the token
            toughness: Toughness of the token
            creature_type: Type of the token creature
            count: Number of tokens to create
            keywords: List of keywords for the token
            controller_gets: Whether the controller gets the tokens
            condition: Optional condition for the effect
        """
        token_desc = f"{count} {power}/{toughness} {creature_type} token"
        super().__init__(f"Create {token_desc}", condition)
        self.power = power
        self.toughness = toughness
        self.creature_type = creature_type
        self.count = count
        self.keywords = keywords or []
        self.controller_gets = controller_gets
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply token creation effect."""
        # First, check the base class conditions
        if not super().apply(game_state, source_id, controller, targets):
            return False
        
        # Determine who gets the tokens
        token_controller = controller
        if not self.controller_gets and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            token_controller = game_state.p1 if player_id == "p1" else game_state.p2
        
        if not token_controller:
            return False
            
        # Prepare token data
        token_data = {
            "name": f"{self.creature_type} Token",
            "type_line": f"Token Creature — {self.creature_type}",
            "card_types": ["creature"],
            "subtypes": [self.creature_type.lower()],
            "power": self.power,
            "toughness": self.toughness,
            "oracle_text": " ".join(self.keywords) if self.keywords else "",
            "keywords": [0] * 11  # Default to no keywords
        }
        
        # Set keywords if any
        for keyword in self.keywords:
            keyword_idx = self._get_keyword_index(keyword)
            if keyword_idx >= 0 and keyword_idx < 11:
                token_data["keywords"][keyword_idx] = 1
        
        # Create tokens
        created_tokens = []
        for _ in range(self.count):
            if hasattr(game_state, 'create_token'):
                token_id = game_state.create_token(token_controller, token_data)
                created_tokens.append(token_id)
            else:
                # Fallback token creation
                if not hasattr(token_controller, "tokens"):
                    token_controller["tokens"] = []
                
                token_count = len(token_controller["tokens"])
                token_id = f"TOKEN_{token_count}_{self.creature_type.replace(' ', '_')}"
                token = Card(token_data)
                game_state.card_db[token_id] = token
                token_controller["battlefield"].append(token_id)
                token_controller["tokens"].append(token_id)
                created_tokens.append(token_id)
                
        # Track token creation
        if not hasattr(game_state, 'tokens_created_this_turn'):
            game_state.tokens_created_this_turn = {}
        
        # Use player key for tracking
        player_key = "p1" if token_controller == game_state.p1 else "p2"
        current_tokens = game_state.tokens_created_this_turn.get(player_key, 0)
        game_state.tokens_created_this_turn[player_key] = current_tokens + self.count
        
        logging.debug(f"Created {self.count} {self.power}/{self.toughness} {self.creature_type} tokens")
        return len(created_tokens) > 0
    
    def _get_keyword_index(self, keyword):
        """Map keyword to its index in the keywords array."""
        keyword_indices = {
            "flying": 0, "trample": 1, "hexproof": 2, "lifelink": 3, "deathtouch": 4,
            "first strike": 5, "double strike": 6, "vigilance": 7, "flash": 8, "haste": 9, "menace": 10
        }
        return keyword_indices.get(keyword.lower(), -1)

class DestroyEffect(AbilityEffect):
    """Effect that destroys permanents."""
    def __init__(self, target_type="permanent", can_target_indestructible=False):
        """
        Initialize destroy effect.
        
        Args:
            target_type: Type of target ('creature', 'artifact', 'enchantment', 'land', 'permanent')
            can_target_indestructible: Whether it can destroy indestructible permanents
        """
        super().__init__(f"Destroy target {target_type}")
        self.target_type = target_type
        self.can_target_indestructible = can_target_indestructible
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply destroy effect with target handling."""
        destroyed = False
        
        if not targets:
            return False
            
        # Get the appropriate target list
        target_ids = []
        if self.target_type == "creature" and "creatures" in targets:
            target_ids = targets["creatures"]
        elif self.target_type == "artifact" and "artifacts" in targets:
            target_ids = targets["artifacts"]
        elif self.target_type == "enchantment" and "enchantments" in targets:
            target_ids = targets["enchantments"]
        elif self.target_type == "land" and "lands" in targets:
            target_ids = targets["lands"]
        elif self.target_type == "permanent" and "permanents" in targets:
            target_ids = targets["permanents"]
        
        # Process each target
        for target_id in target_ids:
            # Find target controller
            target_controller = None
            for player in [game_state.p1, game_state.p2]:
                if target_id in player["battlefield"]:
                    target_controller = player
                    break
            
            if not target_controller:
                continue
                
            target = game_state._safe_get_card(target_id)
            if not target:
                continue
                
            # Check for indestructible
            if not self.can_target_indestructible and hasattr(target, 'oracle_text') and "indestructible" in target.oracle_text.lower():
                logging.debug(f"Cannot destroy {target.name} (indestructible)")
                continue
            
            # Destroy the permanent
            game_state.move_card(target_id, target_controller, "battlefield", target_controller, "graveyard")
            logging.debug(f"Destroyed {target.name}")
            destroyed = True
            
        return destroyed

class CounterSpellEffect(AbilityEffect):
    """Effect that counters a spell on the stack."""
    def __init__(self, target_type="spell"):
        """
        Initialize counter spell effect.
        
        Args:
            target_type: Type of spell to counter ('spell', 'creature spell', 'noncreature spell')
        """
        super().__init__(f"Counter target {target_type}")
        self.target_type = target_type
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply counter spell effect with target handling."""
        if not targets or "spells" not in targets or not targets["spells"]:
            # Try to get targets if not provided
            if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                    source_id, self.effect_text, controller)
                
                # Check if targeting failed
                if not targets or "spells" not in targets or not targets["spells"]:
                    logging.debug(f"Targeting failed for counter spell effect")
                    return False
            else:
                return False
                
        for spell_id in targets["spells"]:
            # Find the spell on the stack
            for i, item in enumerate(game_state.stack):
                if not isinstance(item, tuple) or len(item) < 3:
                    continue
                    
                stack_type, stack_id, spell_caster = item[:3]
                
                if stack_type != "SPELL" or stack_id != spell_id:
                    continue
                    
                spell = game_state._safe_get_card(spell_id)
                if not spell:
                    continue
                    
                # Check for "can't be countered"
                if hasattr(spell, 'oracle_text') and "can't be countered" in spell.oracle_text.lower():
                    logging.debug(f"Cannot counter {spell.name} - it can't be countered")
                    return False
                    
                # Remove from stack and move to graveyard
                game_state.stack.pop(i)
                spell_caster["graveyard"].append(spell_id)
                logging.debug(f"Countered {spell.name}")
                return True
                
        return False

class DiscardEffect(AbilityEffect):
    """Effect that causes players to discard cards."""
    def __init__(self, count=1, target="controller"):
        """
        Initialize discard effect.
        
        Args:
            count: Number of cards to discard (-1 for entire hand)
            target: Who discards ('controller', 'opponent', 'target_player')
        """
        super().__init__(f"Discard {count} card(s)")
        self.count = count
        self.target = target
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply discard effect with target handling."""
        target_player = controller  # Default to controller
        
        # Determine target player
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2
        
        if not target_player:
            return False
            
        # Handle discard
        if self.count == -1:
            # Discard entire hand
            discard_count = len(target_player["hand"])
            while target_player["hand"]:
                card_id = target_player["hand"].pop(0)
                target_player["graveyard"].append(card_id)
                
                # Trigger discard effects
                game_state.trigger_ability(card_id, "DISCARD", {"controller": target_player})
                
            logging.debug(f"Player discarded entire hand ({discard_count} cards)")
            
            # Track discards
            if not hasattr(game_state, 'cards_discarded_this_turn'):
                game_state.cards_discarded_this_turn = {}
            
            player_key = "p1" if target_player == game_state.p1 else "p2"
            game_state.cards_discarded_this_turn[player_key] = game_state.cards_discarded_this_turn.get(player_key, 0) + discard_count
            
            return True
        else:
            # Discard specified number of cards
            discard_count = min(self.count, len(target_player["hand"]))
            
            # In a real game, player would choose which cards to discard
            # For AI, we'll use a simple priority (highest mana cost first)
            if discard_count > 0:
                # Sort hand by mana cost (highest first)
                sorted_hand = sorted(
                    [(i, game_state._safe_get_card(card_id).cmc if hasattr(game_state._safe_get_card(card_id), 'cmc') else 0) 
                    for i, card_id in enumerate(target_player["hand"])],
                    key=lambda x: -x[1]
                )
                
                # Discard highest cost cards first
                for i in range(discard_count):
                    if i < len(sorted_hand):
                        idx = sorted_hand[i][0]
                        if idx < len(target_player["hand"]):
                            card_id = target_player["hand"].pop(idx)
                            target_player["graveyard"].append(card_id)
                            
                            # Trigger discard effects
                            game_state.trigger_ability(card_id, "DISCARD", {"controller": target_player})
                
                logging.debug(f"Player discarded {discard_count} card(s)")
                
                # Track discards
                if not hasattr(game_state, 'cards_discarded_this_turn'):
                    game_state.cards_discarded_this_turn = {}
                
                player_key = "p1" if target_player == game_state.p1 else "p2"
                game_state.cards_discarded_this_turn[player_key] = game_state.cards_discarded_this_turn.get(player_key, 0) + discard_count
                
                return True
                
        return False

class MillEffect(AbilityEffect):
    """Effect that mills cards from library to graveyard."""
    def __init__(self, count=1, target="controller"):
        """
        Initialize mill effect.
        
        Args:
            count: Number of cards to mill
            target: Whose library to mill ('controller', 'opponent', 'target_player')
        """
        super().__init__(f"Mill {count} card(s)")
        self.count = count
        self.target = target
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply mill effect with target handling."""
        target_player = controller  # Default to controller
        
        # Determine target player
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2
        
        if not target_player:
            return False
            
        # Mill cards
        mill_count = min(self.count, len(target_player["library"]))
        for _ in range(mill_count):
            if target_player["library"]:
                card_id = target_player["library"].pop(0)
                target_player["graveyard"].append(card_id)
                
                # Trigger mill effects if needed
                game_state.trigger_ability(card_id, "MILLED", {"controller": target_player})
        
        logging.debug(f"Milled {mill_count} card(s) from {target_player['name']}'s library")
        
        # Track milled cards
        if not hasattr(game_state, 'cards_milled_this_turn'):
            game_state.cards_milled_this_turn = {}
        
        player_key = "p1" if target_player == game_state.p1 else "p2"
        game_state.cards_milled_this_turn[player_key] = game_state.cards_milled_this_turn.get(player_key, 0) + mill_count
        
        # If no cards left in library, player will lose on next draw
        if not target_player["library"]:
            target_player["library_empty_warning"] = True
            
        return mill_count > 0
class ExileEffect(AbilityEffect):
    """Effect that exiles permanents or cards from zones."""
    def __init__(self, target_type="permanent", zone="battlefield"):
        """
        Initialize exile effect.

        Args:
            target_type: Type of target ('creature', 'artifact', 'permanent', 'graveyard')
            zone: Zone to exile from ('battlefield', 'graveyard', 'hand')
        """
        super().__init__(f"Exile target {target_type}")
        self.target_type = target_type
        self.zone = zone

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply exile effect with target handling."""
        exiled = False

        if not targets:
            return False

        # Get the appropriate target list based on target type and zone
        target_ids = []
        if self.zone == "battlefield":
            if self.target_type == "creature" and "creatures" in targets:
                target_ids = targets["creatures"]
            elif self.target_type == "artifact" and "artifacts" in targets:
                target_ids = targets["artifacts"]
            elif self.target_type == "permanent" and "permanents" in targets:
                target_ids = targets["permanents"]
        elif self.zone == "graveyard" and "graveyard" in targets:
            target_ids = targets["graveyard"]
        elif self.zone == "hand" and "hand" in targets:
            target_ids = targets["hand"]

        # Process each target
        for target_id in target_ids:
            # Find target owner
            target_owner = None
            for player in [game_state.p1, game_state.p2]:
                if self.zone in player and target_id in player[self.zone]:
                    target_owner = player
                    break

            if not target_owner:
                continue

            target = game_state._safe_get_card(target_id)
            if not target:
                continue

            # Exile the card using move_card for triggers/replacements
            if game_state.move_card(target_id, target_owner, self.zone, target_owner, "exile", cause="exile_effect", context={"source_id": source_id}):
                logging.debug(f"Exiled {target.name} from {self.zone}")
                exiled = True

        return exiled

class ReturnToHandEffect(AbilityEffect):
    """Effect that returns cards to their owner's hand."""
    def __init__(self, target_type="permanent", zone="battlefield"):
        """
        Initialize return to hand effect.

        Args:
            target_type: Type of target ('creature', 'artifact', 'permanent')
            zone: Zone to return from ('battlefield', 'graveyard')
        """
        super().__init__(f"Return target {target_type} to its owner's hand")
        self.target_type = target_type
        self.zone = zone

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply return to hand effect with target handling."""
        returned = False

        if not targets:
            return False

        # Get the appropriate target list
        target_ids = []
        if self.zone == "battlefield":
            if self.target_type == "creature" and "creatures" in targets:
                target_ids = targets["creatures"]
            elif self.target_type == "artifact" and "artifacts" in targets:
                target_ids = targets["artifacts"]
            elif self.target_type == "permanent" and "permanents" in targets:
                target_ids = targets["permanents"]
        elif self.zone == "graveyard" and "graveyard" in targets:
            target_ids = targets["graveyard"]

        # Process each target
        for target_id in target_ids:
            # Find target owner
            target_owner = None
            for player in [game_state.p1, game_state.p2]:
                # Check all zones a card might be in
                for check_zone in ["battlefield", "graveyard", "exile", "hand", "library"]:
                    if check_zone in player and target_id in player[check_zone]:
                         # If the target zone is specified, check if the card is there
                        if self.zone == check_zone:
                            target_owner = player
                            break
                         # If zone is 'any' or matches, store owner and check if zone is correct
                        elif self.zone == "any":
                             target_owner = player
                             break
                if target_owner: break

            if not target_owner:
                logging.warning(f"Cannot return {target_id}: owner not found or not in specified zone '{self.zone}'")
                continue

            target = game_state._safe_get_card(target_id)
            if not target:
                continue

            # Return to hand using move_card
            if game_state.move_card(target_id, target_owner, self.zone, target_owner, "hand", cause="return_to_hand", context={"source_id": source_id}):
                logging.debug(f"Returned {target.name} to hand from {self.zone}")
                returned = True

        return returned

class CopySpellEffect(AbilityEffect):
    """Effect that copies a spell on the stack."""
    def __init__(self, target_type="spell", copy_count=1):
        """
        Initialize copy spell effect.
        
        Args:
            target_type: Type of spell to copy ('spell', 'instant', 'sorcery')
            copy_count: Number of copies to create
        """
        super().__init__(f"Copy target {target_type}")
        self.target_type = target_type
        self.copy_count = copy_count
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply copy spell effect with target handling."""
        if not targets or "spells" not in targets or not targets["spells"]:
            return False
            
        for spell_id in targets["spells"]:
            # Find the spell on the stack
            for i, item in enumerate(game_state.stack):
                if not isinstance(item, tuple) or len(item) < 3:
                    continue
                    
                stack_type, stack_id, spell_caster = item[:3]
                
                if stack_type != "SPELL" or stack_id != spell_id:
                    continue
                    
                spell = game_state._safe_get_card(spell_id)
                if not spell:
                    continue
                    
                # Check for "can't be copied"
                if hasattr(spell, 'oracle_text') and "can't be copied" in spell.oracle_text.lower():
                    logging.debug(f"Cannot copy {spell.name} - it can't be copied")
                    return False
                    
                # Create copies on the stack
                for _ in range(self.copy_count):
                    # Copy the spell, changing controller if needed
                    context = item[3] if len(item) > 3 else {}
                    new_context = dict(context)
                    new_context["is_copy"] = True
                    new_context["copied_by"] = source_id
                    
                    game_state.stack.append((stack_type, stack_id, controller, new_context))
                    logging.debug(f"Copied {spell.name} onto the stack")
                
                return True
                
        return False

class TransformEffect(AbilityEffect):
    """Effect that transforms a permanent."""
    def __init__(self):
        super().__init__("Transform this permanent")
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply transform effect to the source permanent."""
        source_card = game_state._safe_get_card(source_id)
        if source_card and hasattr(source_card, "transform"):
            source_card.transform()
            logging.debug(f"Transformed {source_card.name}")
            return True
        return False

class FightEffect(AbilityEffect):
    """Effect that makes creatures fight each other."""
    def __init__(self):
        super().__init__("Fight")
        self.requires_target = True
        
    def apply(self, game_state, source_id, controller, targets=None):
        """Apply fight effect between source and target creature."""
        if not targets or "creatures" not in targets or not targets["creatures"]:
            logging.debug("Fight effect: No target creature specified")
            return False
            
        source_card = game_state._safe_get_card(source_id)
        target_id = targets["creatures"][0]
        target_card = game_state._safe_get_card(target_id)
        
        if not source_card or not target_card:
            return False
            
        if not hasattr(source_card, 'power') or not hasattr(target_card, 'power'):
            return False
            
        # Source deals damage to target
        source_damage = source_card.power
        if source_damage > 0:
            if not hasattr(target_card, "damage_taken"):
                target_card.damage_taken = 0
            target_card.damage_taken += source_damage
            logging.debug(f"{source_card.name} deals {source_damage} damage to {target_card.name}")
            
        # Target deals damage to source
        target_damage = target_card.power
        if target_damage > 0:
            if not hasattr(source_card, "damage_taken"):
                source_card.damage_taken = 0
            source_card.damage_taken += target_damage
            logging.debug(f"{target_card.name} deals {target_damage} damage to {source_card.name}")
            
        # Check for lethal damage
        if hasattr(source_card, 'toughness') and source_card.damage_taken >= source_card.toughness:
            # Find source controller
            source_controller = None
            for player in [game_state.p1, game_state.p2]:
                if source_id in player["battlefield"]:
                    source_controller = player
                    break
                    
            if source_controller:
                game_state.move_card(source_id, source_controller, "battlefield", source_controller, "graveyard")
                logging.debug(f"{source_card.name} died from combat damage")
                
        if hasattr(target_card, 'toughness') and target_card.damage_taken >= target_card.toughness:
            # Find target controller
            target_controller = None
            for player in [game_state.p1, game_state.p2]:
                if target_id in player["battlefield"]:
                    target_controller = player
                    break
                    
            if target_controller:
                game_state.move_card(target_id, target_controller, "battlefield", target_controller, "graveyard")
                logging.debug(f"{target_card.name} died from combat damage")
                
        return True


class SearchLibraryEffect(AbilityEffect):
    """Effect that allows searching a library for cards."""
    def __init__(self, search_type="any", target="controller"):
        super().__init__(f"Search for {search_type}")
        self.search_type = search_type
        self.target = target

    def _apply_effect(self, game_state, source_id, controller, targets=None):
        # Simplified implementation for automation
        logging.debug(f"Searching library for {self.search_type}")

        # Determine whose library to search
        target_player = controller
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2

        # Find a matching card in the library using GameState method
        found_card_id = None
        if hasattr(game_state, 'search_library_and_choose'):
             # Pass AI context if available for smarter choices
             ai_context = {"goal": "ramp" if self.search_type == "basic land" else "threat" if self.search_type == "creature" else "answer"}
             found_card_id = game_state.search_library_and_choose(target_player, self.search_type, ai_choice_context=ai_context)
        else:
            # Basic fallback
            for card_id in target_player["library"]:
                 card = game_state._safe_get_card(card_id)
                 if self._card_matches_criteria(card, self.search_type): # Requires helper _card_matches_criteria
                     found_card_id = card_id
                     break

        if found_card_id:
            # Move the card to hand (default behavior)
            # Note: GameState search_library method should handle move and shuffle
            found_card = game_state._safe_get_card(found_card_id)
            logging.debug(f"Search found: '{found_card.name}' matching '{self.search_type}'")
            return True
        else:
            logging.debug(f"Search failed for '{self.search_type}'.")
            # Still need to shuffle even if search fails
            if hasattr(game_state, 'shuffle_library'):
                 game_state.shuffle_library(target_player)
            else:
                 random.shuffle(target_player["library"])
                 logging.debug(f"Shuffled library after failed search.")
            return False # Search itself succeeded, but nothing found

    # Add helper method if not already present in GameState/AbilityUtils
    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()

        if criteria == "any": return True
        if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if criteria == "land" and 'land' in type_line: return True
        if criteria in types: return True
        if criteria in subtypes: return True
        return False

class TapEffect(AbilityEffect):
    """Effect that taps a permanent."""
    def __init__(self, target_type="permanent"):
        super().__init__(f"Tap target {target_type}")
        self.target_type = target_type
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply tap effect with target handling."""
        if not targets:
            return False

        target_ids = []
        if self.target_type == "creature" and "creatures" in targets:
            target_ids = targets["creatures"]
        elif self.target_type == "artifact" and "artifacts" in targets:
            target_ids = targets["artifacts"]
        elif self.target_type == "land" and "lands" in targets:
            target_ids = targets["lands"]
        elif self.target_type == "permanent" and "permanents" in targets:
            target_ids = targets["permanents"]

        if not target_ids:
            return False

        tapped = False
        for target_id in target_ids:
            # Find target controller
            target_controller = None
            for player in [game_state.p1, game_state.p2]:
                if "battlefield" in player and target_id in player["battlefield"]:
                    target_controller = player
                    break

            if not target_controller:
                continue

            # Use game_state's tap_permanent method for consistency
            if game_state.tap_permanent(target_id, target_controller):
                tapped = True

        return tapped

class UntapEffect(AbilityEffect):
    """Effect that untaps a permanent."""
    def __init__(self, target_type="permanent"):
        super().__init__(f"Untap target {target_type}")
        self.target_type = target_type
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply untap effect with target handling."""
        if not targets:
            return False

        target_ids = []
        if self.target_type == "creature" and "creatures" in targets:
            target_ids = targets["creatures"]
        elif self.target_type == "artifact" and "artifacts" in targets:
            target_ids = targets["artifacts"]
        elif self.target_type == "land" and "lands" in targets:
            target_ids = targets["lands"]
        elif self.target_type == "permanent" and "permanents" in targets:
            target_ids = targets["permanents"]

        if not target_ids:
            return False

        untapped = False
        for target_id in target_ids:
            # Find target controller
            target_controller = None
            for player in [game_state.p1, game_state.p2]:
                if "battlefield" in player and target_id in player["battlefield"]:
                    target_controller = player
                    break

            if not target_controller:
                continue

            # Use game_state's untap_permanent method for consistency
            if game_state.untap_permanent(target_id, target_controller):
                untapped = True

        return untapped

class ScryEffect(AbilityEffect):
    """Effect that allows scrying."""
    def __init__(self, count=1):
        super().__init__(f"Scry {count}")
        self.count = count
        
    def apply(self, game_state, source_id, controller, targets=None):
        # Simplified implementation - would typically involve UI interaction
        logging.debug(f"Scrying {self.count}")
        
        # In a real implementation, this would show the top cards and let the player reorder
        # For now, just simulate the scry by looking at the top cards
        if controller["library"]:
            top_cards = controller["library"][:min(self.count, len(controller["library"]))]
            for card_id in top_cards:
                card = game_state._safe_get_card(card_id)
                if card:
                    logging.debug(f"Scry saw {card.name}")
                    
        # Track scry for triggers
        if not hasattr(game_state, 'scry_this_turn'):
            game_state.scry_this_turn = {}
        
        player_key = "p1" if controller == game_state.p1 else "p2"
        game_state.scry_this_turn[player_key] = game_state.scry_this_turn.get(player_key, 0) + self.count
        
        return True

class LifeDrainEffect(AbilityEffect):
    """Effect that drains life from opponents."""
    def __init__(self, amount=1):
        super().__init__(f"Each opponent loses {amount} life and you gain {amount} life")
        self.amount = amount
        
    def apply(self, game_state, source_id, controller, targets=None):
        # Get opponent
        opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
        
        # Apply life loss
        opponent["life"] -= self.amount
        logging.debug(f"Opponent lost {self.amount} life (now at {opponent['life']})")
        
        # Apply life gain
        controller["life"] += self.amount
        logging.debug(f"You gained {self.amount} life (now at {controller['life']})")
        
        # Track life changes for triggers
        player_key = "p1" if controller == game_state.p1 else "p2"
        opponent_key = "p2" if controller == game_state.p1 else "p1"
        
        # Track life gain
        if not hasattr(game_state, 'life_gained_this_turn'):
            game_state.life_gained_this_turn = {}
        game_state.life_gained_this_turn[player_key] = game_state.life_gained_this_turn.get(player_key, 0) + self.amount
        
        # Track life loss
        if not hasattr(game_state, 'life_lost_this_turn'):
            game_state.life_lost_this_turn = {}
        game_state.life_lost_this_turn[opponent_key] = game_state.life_lost_this_turn.get(opponent_key, 0) + self.amount
        
        return True
    
    
    