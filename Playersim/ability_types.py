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
        """Register the static ability's effect with the LayerSystem."""
        if not hasattr(game_state, 'layer_system') or not game_state.layer_system:
            logging.warning(f"Layer system not found, cannot apply static ability: {self.effect_text}")
            return False

        effect_lower = self.effect.lower()
        layer = self._determine_layer_for_effect(effect_lower)

        if layer is None:
            logging.debug(f"Could not determine layer for static effect: '{self.effect_text}'")
            return False # Cannot apply if layer unknown

        # Find affected cards if not specified
        controller = game_state.get_card_controller(self.card_id) # Assuming GS has this method
        if not controller: return False # Should not happen if card exists

        if affected_cards is None:
            affected_cards = self.get_affected_cards(game_state, controller)

        if not affected_cards:
            return False # No targets to affect

        # Prepare base effect data
        effect_data = {
            'source_id': self.card_id,
            'layer': layer,
            'affected_ids': affected_cards,
            'effect_text': self.effect_text, # Store original text for reference/debugging
            'duration': 'permanent', # Static effects are usually permanent while source is on battlefield
            # Condition: effect is active only if the source card is on the battlefield
            'condition': lambda gs_check: self.card_id in gs_check.get_card_controller(self.card_id).get("battlefield", []) if gs_check.get_card_controller(self.card_id) else False,
            # Layer 7 specifics added by handlers below
        }

        # --- Delegate to specific parsers to fill effect_type and effect_value ---
        parsed = False
        if layer == 7:
            parsed_data = self._parse_layer7_effect(effect_lower)
            if parsed_data:
                effect_data.update(parsed_data) # Adds 'sublayer', 'effect_type', 'effect_value'
                parsed = True
        elif layer == 6:
            parsed_data = self._parse_layer6_effect(effect_lower)
            if parsed_data:
                effect_data.update(parsed_data) # Adds 'effect_type', 'effect_value'
                parsed = True
        elif layer == 5:
            parsed_data = self._parse_layer5_effect(effect_lower)
            if parsed_data:
                effect_data.update(parsed_data) # Adds 'effect_type', 'effect_value'
                parsed = True
        elif layer == 4:
            parsed_data = self._parse_layer4_effect(effect_lower)
            if parsed_data:
                effect_data.update(parsed_data) # Adds 'effect_type', 'effect_value'
                parsed = True
        # ... add parsers for layers 3, 2, 1 if needed ...

        if parsed:
            effect_id = game_state.layer_system.register_effect(effect_data)
            if effect_id:
                logging.debug(f"Registered static effect '{self.effect_text}' (ID: {effect_id}) for {self.card_id}")
                return True
            else:
                logging.warning(f"Failed to register static effect '{self.effect_text}' for {self.card_id}")
                return False
        else:
            logging.warning(f"Static ability parser could not interpret effect: '{self.effect_text}'")
            return False
        
    def _parse_layer7_effect(self, effect_lower):
        """Parse P/T effects for Layer 7."""
        # Layer 7a: Set P/T
        match = re.search(r"has base power and toughness (\d+)/(\d+)", effect_lower)
        if match:
            power = int(match.group(1)); toughness = int(match.group(2))
            return {'sublayer': 'a', 'effect_type': 'set_base_pt', 'effect_value': (power, toughness)}
        match = re.search(r"\bpower and toughness are each equal to\b", effect_lower) # Characteristic-defining
        if match:
            # TODO: Implement CDA logic (complex)
            # Return a placeholder or function for CDA
            logging.warning("CDA P/T setting not fully implemented.")
            return {'sublayer': 'a', 'effect_type': 'set_pt_cda', 'effect_value': lambda gs, card: (len(gs.p1.graveyard), len(gs.p1.graveyard))} # Example CDA

        # Layer 7b: P/T setting (e.g., Becomes X/X) - Note: Might overlap with 7a, clarify rules
        match = re.search(r"becomes a (\d+)/(\d+)", effect_lower)
        if match:
            power = int(match.group(1)); toughness = int(match.group(2))
            # This might be layer 7b *if* it overrides previous P/T settings but not base P/T? Rules are tricky. Let's use 7b.
            return {'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)}

        # Layer 7c: P/T modification (+X/+Y, -X/-Y)
        match = re.search(r"gets ([+\-]\d+)/([+\-]\d+)", effect_lower)
        if match:
            p_mod = int(match.group(1)); t_mod = int(match.group(2))
            return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        match = re.search(r"get \+(\d+)/\+(\d+)", effect_lower) # Anthem pattern
        if match:
            p_mod = int(match.group(1)); t_mod = int(match.group(2))
            return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        match = re.search(r"get \-(\d+)/\-(\d+)", effect_lower) # Penalty pattern
        if match:
            p_mod = -int(match.group(1)); t_mod = -int(match.group(2))
            return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}

        # Layer 7d: Switch P/T
        if "switch its power and toughness" in effect_lower:
            return {'sublayer': 'd', 'effect_type': 'switch_pt', 'effect_value': True}

        return None

    def _parse_layer6_effect(self, effect_lower):
        """Parse ability adding/removing effects for Layer 6."""
        # Add abilities
        match = re.search(r"(?:have|gains|gain)\s+(flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance|protection from|ward)", effect_lower)
        if match:
            ability = match.group(1).strip()
            # Special handling for protection/ward if needed
            if "protection from" in ability:
                protected_from_match = re.search(r"protection from ([\w\s]+)", effect_lower)
                protected_from = protected_from_match.group(1).strip() if protected_from_match else "unknown"
                return {'effect_type': 'add_ability', 'effect_value': f"protection from {protected_from}"}
            elif "ward" in ability:
                ward_cost_match = re.search(r"ward (\{.*?\})", effect_lower) or re.search(r"ward (\d+)", effect_lower)
                ward_cost = ward_cost_match.group(1).strip() if ward_cost_match else "1" # Default ward 1?
                return {'effect_type': 'add_ability', 'effect_value': f"ward {ward_cost}"}
            else:
                return {'effect_type': 'add_ability', 'effect_value': ability}

        # Remove abilities
        match = re.search(r"lose all abilities", effect_lower)
        if match:
            return {'effect_type': 'remove_all_abilities', 'effect_value': True}
        match = re.search(r"loses (flying|first strike|...)", effect_lower) # Add keywords
        if match:
            ability = match.group(1).strip()
            return {'effect_type': 'remove_ability', 'effect_value': ability}

        # Prevent attacking/blocking
        if "can't attack" in effect_lower: return {'effect_type': 'cant_attack', 'effect_value': True}
        if "can't block" in effect_lower: return {'effect_type': 'cant_block', 'effect_value': True}
        if "attack each combat if able" in effect_lower: return {'effect_type': 'must_attack', 'effect_value': True}
        if "block each combat if able" in effect_lower: return {'effect_type': 'must_block', 'effect_value': True}

        return None

    def _parse_layer5_effect(self, effect_lower):
        """Parse color adding/removing effects for Layer 5."""
        colors = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        target_colors = [0] * 5
        found_color = False

        # Check if setting specific colors
        for color, index in colors.items():
            if f"is {color}" in effect_lower or f"are {color}" in effect_lower:
                target_colors[index] = 1
                found_color = True

        if found_color: # If specific colors are set, assume it *sets* the colors
            return {'effect_type': 'set_color', 'effect_value': target_colors}

        # Check if adding colors
        added_colors = [0] * 5
        found_add = False
        for color, index in colors.items():
            if f"is also {color}" in effect_lower:
                added_colors[index] = 1
                found_add = True
        if found_add:
            return {'effect_type': 'add_color', 'effect_value': added_colors}

        # Check if becoming colorless
        if "becomes colorless" in effect_lower:
            return {'effect_type': 'set_color', 'effect_value': [0, 0, 0, 0, 0]}

        return None

    def _parse_layer4_effect(self, effect_lower):
        """Parse type/subtype adding/removing effects for Layer 4."""
        # Add Type
        match = re.search(r"is also a(n)?\s+(\w+)", effect_lower)
        if match:
            type_to_add = match.group(2).strip()
            # Validate type?
            return {'effect_type': 'add_type', 'effect_value': type_to_add}

        # Set Type
        match = re.search(r"becomes a(n)?\s+(\w+)\s+(in addition to its other types)?", effect_lower)
        if match:
            type_to_set = match.group(2).strip()
            in_addition = match.group(3) is not None
            if in_addition:
                return {'effect_type': 'add_type', 'effect_value': type_to_set}
            else:
                # Need to distinguish SETTING type vs ADDING type - requires rules clarity. Assume "becomes" SETS.
                return {'effect_type': 'set_type', 'effect_value': type_to_set} # Might need a dedicated set_type handler

        # Add Subtype
        match = re.search(r"(?:is|are) also\s+(\w+)\s+((?:creature|artifact|enchantment|land|planeswalker)s?)", effect_lower)
        if match:
            subtype_to_add = match.group(1).strip().capitalize() # Subtypes usually capitalized
            # Check if it's adding a subtype to a valid permanent type
            return {'effect_type': 'add_subtype', 'effect_value': subtype_to_add}

        return None

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
        # Check if "target" appears outside of quotes or parenthetical remarks for more accuracy
        cleaned_text = re.sub(r'\([^()]*?\)', '', effect_text.lower()) # Remove parenthetical text
        cleaned_text = re.sub(r'"[^"]*?"', '', cleaned_text) # Remove quoted text
        self.requires_target = "target" in cleaned_text
        
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
        effective_targets = targets if targets is not None else {} # Ensure targets is a dict

        # Resolve targets if required and not provided/empty
        if self.requires_target and (not effective_targets or not any(effective_targets.values())):
            logging.debug(f"Effect '{self.effect_text}' requires target, resolving...")
            resolved_targets = None
            # Prefer GameState's targeting system if available
            if hasattr(game_state, 'targeting_system') and game_state.targeting_system:
                # Resolve targeting based on the effect text itself
                 # Need source card object for targeting system
                source_card = game_state._safe_get_card(source_id)
                if source_card:
                     resolved_targets = game_state.targeting_system.resolve_targeting(source_id, controller, self.effect_text)
            elif hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                 resolved_targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(source_id, self.effect_text, controller)
            else: # Fallback to simple targeting
                 resolved_targets = resolve_simple_targeting(game_state, source_id, controller, self.effect_text)

            if resolved_targets and any(resolved_targets.values()):
                 effective_targets = resolved_targets # Use resolved targets
                 logging.debug(f"Resolved targets: {effective_targets}")
            else:
                logging.warning(f"Targeting failed or yielded no targets for effect: {self.effect_text}")
                return False # Cannot proceed without required targets

        # Check condition if present
        if self.condition and not self._evaluate_condition(game_state, source_id, controller):
            logging.debug(f"Condition not met for effect: {self.effect_text}")
            return False

        # Call the implementation-specific effect application
        try:
            result = self._apply_effect(game_state, source_id, controller, effective_targets) # Pass resolved targets
            if result is None: # Handle NotImplementedError cases gracefully
                logging.warning(f"Effect application returned None for: {self.effect_text}. Might be unimplemented.")
                return False # Treat unimplemented as failure
            return result
        except NotImplementedError:
             logging.error(f"Effect application not implemented for: {self.effect_text}")
             return False
        except Exception as e:
             logging.error(f"Error applying effect '{self.effect_text}': {e}")
             import traceback
             logging.error(traceback.format_exc())
             return False
    
    def _apply_effect(self, game_state, source_id, controller, targets):
        """
        Implementation-specific effect application.
        Should be overridden by subclasses.
        """
        raise NotImplementedError(f"_apply_effect method must be implemented by subclasses for '{self.effect_text}'")
    
    def _evaluate_condition(self, game_state, source_id, controller):
        """
        Evaluate if condition is met. (Implementation remains similar)
        """
        if not self.condition:
            return True

        # Implement sophisticated condition parsing and evaluation here if needed
        # Example placeholder:
        condition_text = str(self.condition).lower() # Convert condition (potentially a function) to string for basic check
        if "if you control a creature" in condition_text:
            return any('creature' in getattr(game_state._safe_get_card(cid),'card_types',[]) for cid in controller["battlefield"])

        # Default to true for unrecognized conditions or non-string conditions
        return True


class DrawCardEffect(AbilityEffect):
    """Effect that causes players to draw cards."""
    def __init__(self, count=1, target="controller", condition=None):
        super().__init__(f"Draw {count} card(s)", condition)
        self.count = count
        self.target = target

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply draw card effect with target handling."""
        target_player = controller # Default to controller
        player_desc = "controller"

        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
            player_desc = "opponent"
        elif self.target == "target_player" and targets and targets.get("players"):
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2
            player_desc = f"Player {player_id}"
        elif self.target == "each player":
             # Draw for both players
             success = True
             for p in [game_state.p1, game_state.p2]:
                  num_drawn = 0
                  for _ in range(self.count):
                      if p["library"]:
                          card_drawn = p["library"].pop(0)
                          p["hand"].append(card_drawn)
                          num_drawn += 1
                      else: p["attempted_draw_from_empty"] = True; success=False; break
                  logging.debug(f"DrawCardEffect ({self.target}): {p['name']} drew {num_drawn} card(s).")
                  if not success: break # Stop if someone decked out
             return success
        elif self.target == "each opponent":
             opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
             target_player = opponent
             player_desc = "each opponent"

        if not target_player: return False

        # Apply draw effect
        num_drawn = 0
        success_draw = True
        for _ in range(self.count):
            if target_player["library"]:
                card_drawn = target_player["library"].pop(0)
                target_player["hand"].append(card_drawn)
                num_drawn += 1
            else:
                 target_player["attempted_draw_from_empty"] = True
                 success_draw = False
                 break # Stop drawing if library empty

        logging.debug(f"DrawCardEffect: {player_desc} drew {num_drawn} card(s).")
        # Update draw tracking if needed by GS
        if hasattr(game_state, 'cards_drawn_this_turn'):
            player_key = "p1" if target_player == game_state.p1 else "p2"
            game_state.cards_drawn_this_turn[player_key] = game_state.cards_drawn_this_turn.get(player_key, 0) + num_drawn

        return success_draw


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
    """Effect that deals damage to targets."""
    def __init__(self, amount, target_type="any", condition=None):
        super().__init__(f"Deal {amount} damage to target {target_type}", condition)
        self.amount = amount
        self.target_type = target_type # e.g., "creature", "player", "any target", "each opponent"

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply damage effect with proper targeting and keywords (Lifelink, Deathtouch)."""
        if self.amount <= 0: return True # No damage to deal

        source_card = game_state._safe_get_card(source_id)
        # CONSOLIDATION: Check for keywords directly here or via a helper that *doesn't* rely on KeywordAbility
        # Assuming a helper function exists on GameState or AbilityHandler for keyword checks:
        has_lifelink = False
        has_deathtouch = False
        if hasattr(game_state, 'ability_handler'):
             # Note: This helper needs to be implemented correctly in AbilityHandler without KeywordAbility
             if hasattr(game_state.ability_handler, '_has_keyword_check'): # Example helper name
                has_lifelink = game_state.ability_handler._has_keyword_check(source_card, "lifelink")
                has_deathtouch = game_state.ability_handler._has_keyword_check(source_card, "deathtouch")
             # Fallback if helper doesn't exist (less accurate)
             elif source_card and hasattr(source_card, 'oracle_text'):
                 has_lifelink = "lifelink" in source_card.oracle_text.lower()
                 has_deathtouch = "deathtouch" in source_card.oracle_text.lower()

        targets_to_damage = [] # List of (target_id, target_obj, target_player, is_player_target)

        # --- Collect targets logic (remains mostly the same) ---
        # ... (target collection logic as before) ...
        if self.target_type == "each opponent":
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            targets_to_damage.append(("p1" if opponent == game_state.p1 else "p2", opponent, opponent, True))
        elif self.target_type == "any target":
            # Combine players and creatures/planeswalkers from targets dict
             if targets.get("players"):
                 for pid in targets["players"]:
                     p_obj = game_state.p1 if pid == "p1" else game_state.p2
                     targets_to_damage.append((pid, p_obj, p_obj, True))
             categories = ["creatures", "planeswalkers", "battles"] # Add battles if targetable by damage
             for cat in categories:
                 if targets.get(cat):
                     for t_id in targets[cat]:
                          target_location = game_state.find_card_location(t_id)
                          if target_location:
                                t_owner, _ = target_location
                                t_obj = game_state._safe_get_card(t_id)
                                if t_obj and t_owner: targets_to_damage.append((t_id, t_obj, t_owner, False))
                          else: # Could be targeting something not on battlefield (e.g., grave for some effects)
                               # Basic object fetch if location unknown (less safe)
                               t_obj = game_state._safe_get_card(t_id)
                               # Cannot determine owner easily without location, might need context
                               if t_obj: targets_to_damage.append((t_id, t_obj, None, False)) # Owner unknown

        else: # Specific target type like "creature", "player"
            cat_map = {"creature": "creatures", "player": "players", "planeswalker": "planeswalkers", "battle": "battles"}
            target_cat = cat_map.get(self.target_type)
            if target_cat and targets.get(target_cat):
                for t_id in targets[target_cat]:
                     if target_cat == "players":
                         p_obj = game_state.p1 if t_id == "p1" else game_state.p2
                         targets_to_damage.append((t_id, p_obj, p_obj, True))
                     else:
                         target_location = game_state.find_card_location(t_id)
                         if target_location:
                              t_owner, _ = target_location
                              t_obj = game_state._safe_get_card(t_id)
                              if t_obj and t_owner: targets_to_damage.append((t_id, t_obj, t_owner, False))
                         else: # Target might be e.g. in graveyard
                              t_obj = game_state._safe_get_card(t_id)
                              if t_obj: targets_to_damage.append((t_id, t_obj, None, False)) # Owner unknown

        if not targets_to_damage:
            logging.warning(f"DamageEffect: No valid targets found for '{self.effect_text}'.")
            return False

        total_damage_dealt = 0
        success = False

        for target_id, target_obj, target_owner, is_player in targets_to_damage:
            # Apply damage replacement effects (e.g., prevention)
            damage_context = { "source_id": source_id, "target_id": target_id, "target_is_player": is_player, "damage_amount": self.amount, "is_combat_damage": False } # Assume non-combat
            # Use replacement effect system if available
            actual_damage = self.amount
            if hasattr(game_state, 'replacement_effects') and game_state.replacement_effects:
                modified_context, _ = game_state.replacement_effects.apply_replacements("DAMAGE", damage_context)
                actual_damage = modified_context.get("damage_amount", 0)
            else: # Fallback if no system
                 actual_damage = self.amount # Apply full damage

            if actual_damage <= 0:
                 logging.debug(f"Damage to {target_id} prevented or reduced to 0.")
                 continue # Damage prevented

            if is_player:
                # Target owner must be the player object itself
                player_obj = target_obj # Target object is the player dict
                if player_obj:
                    player_obj["life"] -= actual_damage
                    logging.debug(f"DamageEffect: {source_card.name if source_card else source_id} dealt {actual_damage} damage to {player_obj['name']}.")
                    total_damage_dealt += actual_damage
                    success = True
                 # TODO: Check for player loss SBA
            elif isinstance(target_obj, Card): # Target is permanent (creature, planeswalker, battle)
                 if 'creature' in getattr(target_obj, 'card_types', []):
                      # Use damage counters on player dict for tracking damage on creatures
                      if target_owner: # Owner must be known to track damage
                          target_owner.setdefault("damage_counters", {})[target_id] = target_owner["damage_counters"].get(target_id, 0) + actual_damage
                          logging.debug(f"DamageEffect: {source_card.name if source_card else source_id} dealt {actual_damage} damage to {target_obj.name}.")
                          # Mark deathtouch damage
                          if has_deathtouch and actual_damage > 0:
                               target_owner.setdefault("deathtouch_damage", {})[target_id] = True
                          total_damage_dealt += actual_damage
                          success = True
                      else: logging.warning(f"Cannot apply damage to creature {target_id} without known owner.")
                      # TODO: Check for lethal damage SBA
                 elif 'planeswalker' in getattr(target_obj, 'card_types', []):
                     if target_owner: # Owner must be known
                         target_owner.setdefault("loyalty_counters", {}) # Ensure dict exists
                         current_loyalty = target_owner["loyalty_counters"].get(target_id, getattr(target_obj,'loyalty',0))
                         target_owner["loyalty_counters"][target_id] = current_loyalty - actual_damage
                         logging.debug(f"DamageEffect: {source_card.name if source_card else source_id} dealt {actual_damage} damage to PW {target_obj.name} (loyalty {target_owner['loyalty_counters'][target_id]}).")
                         total_damage_dealt += actual_damage
                         success = True
                          # TODO: Check for 0 loyalty SBA
                     else: logging.warning(f"Cannot apply damage to PW {target_id} without known owner.")
                 elif 'battle' in getattr(target_obj, 'type_line', ''):
                     # Battles might have defense counters tracked globally or on the card
                     # Assume tracking on GameState for now
                     if hasattr(game_state, 'battle_cards'):
                         current_defense = game_state.battle_cards.get(target_id, getattr(target_obj,'defense',0))
                         game_state.battle_cards[target_id] = max(0, current_defense - actual_damage)
                         logging.debug(f"DamageEffect: {source_card.name if source_card else source_id} dealt {actual_damage} damage to Battle {target_obj.name} (defense {game_state.battle_cards[target_id]}).")
                         total_damage_dealt += actual_damage
                         success = True
                         # TODO: Check for defeated battle
                     else: logging.warning(f"Battle card system not found, cannot apply damage to battle {target_id}")


        # CONSOLIDATION: Apply lifelink directly here
        if has_lifelink and total_damage_dealt > 0:
             # Check for life gain replacement effects
             gain_context = {'player': controller, 'life_amount': total_damage_dealt, 'source_type': 'lifelink', 'source_id': source_id}
             final_life_gain = total_damage_dealt
             if hasattr(game_state, 'replacement_effects') and game_state.replacement_effects:
                  modified_gain_context, _ = game_state.replacement_effects.apply_replacements("LIFE_GAIN", gain_context)
                  final_life_gain = modified_gain_context.get('life_amount', 0)

             if final_life_gain > 0:
                  controller["life"] += final_life_gain
                  logging.debug(f"Lifelink triggered for {source_card.name if source_card else source_id}, gained {final_life_gain} life.")
                  # Trigger gain life events
                  if hasattr(game_state, 'trigger_ability'):
                       game_state.trigger_ability(source_id, "LIFE_GAINED", {"amount": final_life_gain, "controller": controller})

        # Important: Trigger SBAs after applying damage
        game_state.check_state_based_actions()

        return success


class AddCountersEffect(AbilityEffect):
    """Effect that adds counters to permanents."""
    def __init__(self, counter_type, count=1, target_type="creature", condition=None):
        super().__init__(f"Put {count} {counter_type} counter(s) on target {target_type}", condition)
        self.counter_type = counter_type
        self.count = count
        self.target_type = target_type.lower() # Normalize

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply counter adding effect."""
        if self.count <= 0: return True # Nothing to add

        targets_to_affect = []
        # Determine targets based on target_type
        if self.target_type == "self":
             targets_to_affect.append(source_id)
        else:
            target_cat_map = {"creature": "creatures", "artifact": "artifacts", "planeswalker": "planeswalkers", "permanent": "permanents"}
            target_cat = target_cat_map.get(self.target_type)
            if target_cat and targets.get(target_cat):
                 targets_to_affect.extend(targets[target_cat])

        if not targets_to_affect:
            logging.warning(f"AddCountersEffect: No valid targets found for '{self.effect_text}'. Targets provided: {targets}")
            return False

        success = False
        for target_id in targets_to_affect:
             # Use GameState's add_counter method for consistency
             if hasattr(game_state, 'add_counter') and callable(game_state.add_counter):
                 if game_state.add_counter(target_id, self.counter_type, self.count):
                      success = True
             else: # Fallback if GS method missing
                  target_card = game_state._safe_get_card(target_id)
                  if target_card:
                       if not hasattr(target_card, 'counters'): target_card.counters = {}
                       target_card.counters[self.counter_type] = target_card.counters.get(self.counter_type, 0) + self.count
                       logging.debug(f"Fallback AddCounters: Added {self.count} {self.counter_type} to {target_card.name}")
                       success = True

        # SBAs might need checking after counter addition (esp. -1/-1)
        if success: game_state.check_state_based_actions()

        return success

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
    def __init__(self, target_type="permanent", can_target_indestructible=False, condition=None):
        super().__init__(f"Destroy target {target_type}", condition)
        self.target_type = target_type.lower() # Normalize
        self.can_target_indestructible = can_target_indestructible

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply destroy effect."""
        destroyed_count = 0
        targets_to_affect = []
        target_cat_map = {"creature": "creatures", "artifact": "artifacts", "enchantment": "enchantments", "planeswalker": "planeswalkers", "land":"lands", "permanent": "permanents"}
        target_cat = target_cat_map.get(self.target_type)

        if target_cat and targets.get(target_cat):
             targets_to_affect.extend(targets[target_cat])
        elif self.target_type == "all": # Handle "destroy all creatures" etc.
            # Determine which type 'all' refers to
            if "all creatures" in self.effect_text.lower(): search_type = "creature"
            elif "all artifacts" in self.effect_text.lower(): search_type = "artifact"
            # Add more types...
            else: search_type = "permanent" # Default assumption for "destroy all"
            for p in [game_state.p1, game_state.p2]:
                for p_id in p["battlefield"]:
                    p_card = game_state._safe_get_card(p_id)
                    if p_card and (search_type=="permanent" or search_type in getattr(p_card,'card_types',[])):
                         targets_to_affect.append(p_id)


        if not targets_to_affect:
            logging.warning(f"DestroyEffect: No valid targets found for '{self.effect_text}'. Targets provided: {targets}")
            return False

        for target_id in targets_to_affect:
            target_card = game_state._safe_get_card(target_id)
            if not target_card: continue
            target_owner = game_state.get_card_controller(target_id)
            if not target_owner: continue

            # Check indestructible
            is_indestructible = hasattr(game_state,'ability_handler') and game_state.ability_handler._has_keyword(target_card, "indestructible")
            if is_indestructible and not self.can_target_indestructible:
                logging.debug(f"DestroyEffect: Cannot destroy {target_card.name} (Indestructible).")
                continue

            # Check regeneration/totem armor replacement effects
            destroy_context = {"card_id": target_id, "controller": target_owner, "to_zone": "graveyard", "cause": "destroy_effect"}
            modified_context, replaced = game_state.apply_replacement_effect("DIES", destroy_context) # Check DIES replacements

            if replaced and modified_context.get('prevented'):
                 logging.debug(f"DestroyEffect: Destruction of {target_card.name} prevented (e.g., regeneration).")
                 continue # Destruction prevented/replaced by regeneration/etc

            final_dest_zone = modified_context.get('to_zone', 'graveyard')

            # Move card using GameState method
            if game_state.move_card(target_id, target_owner, "battlefield", target_owner, final_dest_zone, cause="destroy_effect", context={"source_id": source_id}):
                destroyed_count += 1
                logging.debug(f"DestroyEffect: Moved {target_card.name} to {final_dest_zone}.")
            else:
                 logging.warning(f"DestroyEffect: Failed to move {target_card.name} to {final_dest_zone}.")

        # SBAs will handle the actual death triggers etc.
        if destroyed_count > 0: game_state.check_state_based_actions()
        return destroyed_count > 0

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
    def __init__(self, target_type="permanent", zone="battlefield", condition=None):
        super().__init__(f"Exile target {target_type} from {zone}", condition)
        self.target_type = target_type.lower()
        self.zone = zone.lower()

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply exile effect."""
        exiled_count = 0
        targets_to_affect = []
        # Map target types to categories expected in targets dict
        target_cat_map = {
            "creature": "creatures", "artifact": "artifacts", "enchantment": "enchantments",
            "planeswalker": "planeswalkers", "land": "lands", "permanent": "permanents",
            "card": "cards", # For graveyard/hand etc.
            "spell": "spells" # For stack
        }
        target_cat = target_cat_map.get(self.target_type)

        # Find targets based on category or explicit 'target' keyword
        if target_cat and targets.get(target_cat):
             targets_to_affect.extend(targets[target_cat])
        elif self.target_type == "any": # If effect is "exile any target"
             for cat in ["creatures", "players", "planeswalkers", "battles"]: # Common 'any target' types
                  targets_to_affect.extend(targets.get(cat, []))
        elif "target" in self.effect_text.lower() and not targets_to_affect:
             # If 'target' is mentioned but no specific category matched, look across common categories
             for cat in ["creatures", "players", "planeswalkers", "artifacts", "enchantments", "permanents", "spells", "cards"]:
                  targets_to_affect.extend(targets.get(cat, []))

        if not targets_to_affect:
            logging.warning(f"ExileEffect: No valid targets found for '{self.effect_text}'. Targets provided: {targets}")
            return False

        for target_id in targets_to_affect:
            # Find target location and owner
            location_info = game_state.find_card_location(target_id)
            if not location_info:
                # Special check for player targets
                if target_id in ["p1", "p2"]:
                    logging.warning(f"ExileEffect: Cannot exile player {target_id}.")
                    continue
                logging.warning(f"ExileEffect: Could not find location for target {target_id}.")
                continue

            target_owner, current_zone = location_info

            # Check if the source zone matches the effect's requirement
            if self.zone != 'any' and current_zone != self.zone:
                logging.warning(f"ExileEffect: Target {target_id} not in expected zone '{self.zone}', found in '{current_zone}'.")
                continue

            # Move card using GameState method
            if game_state.move_card(target_id, target_owner, current_zone, target_owner, "exile", cause="exile_effect", context={"source_id": source_id}):
                 exiled_count += 1
                 card_name = game_state._safe_get_card(target_id).name if game_state._safe_get_card(target_id) else target_id
                 logging.debug(f"ExileEffect: Exiled {card_name} from {current_zone}.")
            else:
                 logging.warning(f"ExileEffect: Failed to move {target_id} to exile from {current_zone}.")

        # SBAs check needed? Usually exile doesn't trigger immediate SBAs unless it causes another effect.
        return exiled_count > 0
class ReturnToHandEffect(AbilityEffect):
    """Effect that returns cards to their owner's hand."""
    def __init__(self, target_type="permanent", zone="battlefield"):
        """
        Initialize return to hand effect.

        Args:
            target_type: Type of target ('creature', 'artifact', 'permanent', 'card')
            zone: Zone to return from ('battlefield', 'graveyard', 'exile')
        """
        super().__init__(f"Return target {target_type} from {zone} to its owner's hand")
        self.target_type = target_type.lower()
        self.zone = zone.lower()
        self.requires_target = "target" in self.effect_text.lower()

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Apply exile effect with target handling and zone validation."""
        exiled = False

        if not targets:
            logging.warning(f"Exile effect requires targets but none were provided or resolved.")
            return False

        target_ids_to_process = []

        # Get target IDs based on category specified in 'targets' dictionary
        # Allow for flexibility based on the targeting system's output
        # Use more specific categories first if available
        categories_in_order = [
            "spells", # From stack
            "creatures", "artifacts", "enchantments", "planeswalkers", "lands", # Battlefield types
            "permanents", # General battlefield
            "graveyard", # Cards in graveyard
            "hand", # Cards in hand
            "library", # Cards in library (rare for targeted exile)
            "cards", # General card in any allowed zone
            "any" # Any valid target
        ]
        if self.target_type in categories_in_order: # Add specific target type first if provided
            categories_in_order.insert(0, self.target_type)

        found_targets = False
        for category in categories_in_order:
            if category in targets and targets[category]:
                target_ids_to_process.extend(targets[category])
                found_targets = True
                # Usually stop after finding targets in the most relevant category, unless effect targets multiple types
                # Let's assume we process all found targets for simplicity for now.
                # break # Uncomment if only the first matching category should be processed

        if not found_targets:
            logging.warning(f"No valid target IDs found in the provided targets dictionary for type '{self.target_type}' and zone '{self.zone}'. Targets: {targets}")
            return False

        # Process each target
        for target_id in target_ids_to_process:
            # Find target's current location and owner using GameState method
            location_info = game_state.find_card_location(target_id)

            if not location_info:
                logging.warning(f"Cannot exile {target_id}: Card location not found.")
                continue

            target_owner, current_zone = location_info

            # Validate if the card is actually in the specified source zone for exile
            # Allow 'any' or if the zone matches the *intended* zone, not necessarily current zone (rules are complex)
            # Simplify: Check if current zone is one of the plausible zones for this type.
            plausible_zones = {
                "creature": ["battlefield"], "artifact": ["battlefield"], "enchantment": ["battlefield"],
                "planeswalker": ["battlefield"], "land": ["battlefield"], "permanent": ["battlefield"],
                "spell": ["stack"], "ability": ["stack"],
                "card": ["graveyard", "hand", "library", "battlefield", "exile"], # Can exile from exile (rare)
                "graveyard": ["graveyard"], "hand": ["hand"], "library": ["library"],
                "battlefield": ["battlefield"], "stack": ["stack"]
            }
            expected_zones = plausible_zones.get(self.target_type, ["battlefield", "stack", "graveyard", "hand", "library", "exile"])
            if self.zone != "any" and self.zone not in expected_zones: # Add self.zone check
                expected_zones = [self.zone] # Override if specific zone mentioned

            if current_zone not in expected_zones:
                logging.warning(f"Cannot exile {target_id}: Expected target type '{self.target_type}' in zone(s) '{expected_zones}', but found in '{current_zone}'.")
                continue

            # Exile the card using move_card
            if game_state.move_card(target_id, target_owner, current_zone, target_owner, "exile", cause="exile_effect", context={"source_id": source_id}):
                card = game_state._safe_get_card(target_id)
                card_name = card.name if card else target_id
                logging.debug(f"Exiled {card_name} from {current_zone}")
                exiled = True
            else:
                logging.warning(f"Failed to move card {target_id} to exile from {current_zone}.")

        return exiled
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
    def __init__(self, search_type="any", target="controller", destination="hand", count=1):
        super().__init__(f"Search for {search_type}")
        self.search_type = search_type
        self.target = target
        self.destination = destination # e.g., 'hand', 'battlefield', 'graveyard'
        self.count = count # How many cards to find

    def _apply_effect(self, game_state, source_id, controller, targets=None):
        """Apply search effect, allowing for multiple finds and destinations."""
        logging.debug(f"Searching library for {self.count} '{self.search_type}'")

        target_player = controller
        if self.target == "opponent":
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1
        elif self.target == "target_player" and targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            target_player = game_state.p1 if player_id == "p1" else game_state.p2

        found_cards = []
        search_count_remaining = self.count

        if hasattr(game_state, 'search_library_and_choose'):
             ai_context = {"goal": "ramp" if self.search_type == "basic land" else "threat" if self.search_type == "creature" else "answer",
                           "count_needed": search_count_remaining}
             # Loop to find multiple cards if needed
             while search_count_remaining > 0:
                 found_card_id = game_state.search_library_and_choose(
                     target_player,
                     self.search_type,
                     ai_choice_context=ai_context,
                     exclude_ids=found_cards # Exclude already found cards
                 )
                 if found_card_id:
                     found_cards.append(found_card_id)
                     search_count_remaining -= 1
                     ai_context["count_needed"] = search_count_remaining # Update context
                 else:
                     break # No more matching cards found
        else:
            # Basic fallback (only finds first matching card)
            temp_library = target_player["library"][:] # Copy to iterate
            found_indices = []
            for i, card_id in enumerate(temp_library):
                 if search_count_remaining <= 0: break
                 card = game_state._safe_get_card(card_id)
                 if self._card_matches_criteria(card, self.search_type):
                     found_cards.append(card_id)
                     found_indices.append(i) # Store original index before removal affects others
                     search_count_remaining -= 1
            # Remove found cards from library (using original indices in reverse)
            for idx in sorted(found_indices, reverse=True):
                target_player["library"].pop(idx)

        if found_cards:
            success_moves = 0
            for card_id in found_cards:
                card = game_state._safe_get_card(card_id)
                card_name = card.name if card else card_id
                # Use move_card for proper handling of destination zone and triggers
                if game_state.move_card(card_id, target_player, "library_implicit", target_player, self.destination, cause="search_effect"):
                     success_moves += 1
                     logging.debug(f"Search found '{card_name}', moved to {self.destination}.")
                else:
                     logging.warning(f"Search found '{card_name}', but failed to move to {self.destination}.")
                     # Optionally return card to library or other fallback
                     target_player["library"].append(card_id) # Put it back for now

            # Shuffle library after search attempt (even if not all cards moved successfully)
            if hasattr(game_state, 'shuffle_library'):
                game_state.shuffle_library(target_player)
            else:
                random.shuffle(target_player["library"])

            return success_moves > 0 # Return true if at least one card was successfully moved

        else:
            logging.debug(f"Search failed for '{self.search_type}' in {target_player['name']}'s library.")
            # Shuffle even if search fails
            if hasattr(game_state, 'shuffle_library'):
                 game_state.shuffle_library(target_player)
            else:
                 random.shuffle(target_player["library"])
            return False # Indicate nothing was found/moved

    def _card_matches_criteria(self, card, criteria):
        """Basic check if card matches simple criteria."""
        if not card: return False
        types = getattr(card, 'card_types', [])
        subtypes = getattr(card, 'subtypes', [])
        type_line = getattr(card, 'type_line', '').lower()
        name = getattr(card, 'name', '').lower() # Search by name

        crit_lower = criteria.lower()

        if crit_lower == "any": return True
        if crit_lower == "basic land" and 'basic' in type_line and 'land' in type_line: return True
        if crit_lower == "land" and 'land' in type_line: return True
        if crit_lower in types: return True
        if crit_lower in subtypes: return True
        if crit_lower == name: return True
        # Allow searching for partial names
        if criteria in name: return True
        # Allow searching by card type e.g. "artifact creature"
        if all(word in type_line for word in crit_lower.split()): return True

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
            logging.warning("Tap effect requires targets but none were provided or resolved.")
            return False

        target_ids = []
        possible_categories = [self.target_type, "permanents", "creatures", "artifacts", "lands"]
        found_targets = False
        for category in possible_categories:
             if category in targets and targets[category]:
                  target_ids.extend(targets[category])
                  found_targets = True

        if not found_targets:
            logging.warning(f"No valid target IDs found for TapEffect (type: {self.target_type}). Targets: {targets}")
            return False

        tapped = False
        for target_id in target_ids:
            # Find target controller using GameState helper
            target_controller = game_state.get_card_controller(target_id) # Assume this exists in GameState

            if not target_controller:
                logging.warning(f"Cannot tap {target_id}: Controller not found.")
                continue

            # Use game_state's tap_permanent method for consistency and triggers
            if hasattr(game_state, 'tap_permanent') and callable(game_state.tap_permanent):
                 if game_state.tap_permanent(target_id, target_controller):
                      tapped = True
            else: # Basic fallback
                 tapped_set = target_controller.setdefault("tapped_permanents", set())
                 if target_id not in tapped_set:
                      tapped_set.add(target_id)
                      logging.debug(f"Tapped {game_state._safe_get_card(target_id).name} (Basic)")
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
            logging.warning("Untap effect requires targets but none were provided or resolved.")
            return False

        target_ids = []
        possible_categories = [self.target_type, "permanents", "creatures", "artifacts", "lands"]
        found_targets = False
        for category in possible_categories:
             if category in targets and targets[category]:
                  target_ids.extend(targets[category])
                  found_targets = True

        if not found_targets:
            logging.warning(f"No valid target IDs found for UntapEffect (type: {self.target_type}). Targets: {targets}")
            return False

        untapped = False
        for target_id in target_ids:
            # Find target controller using GameState helper
            target_controller = game_state.get_card_controller(target_id)

            if not target_controller:
                logging.warning(f"Cannot untap {target_id}: Controller not found.")
                continue

            # Use game_state's untap_permanent method for consistency and triggers
            if hasattr(game_state, 'untap_permanent') and callable(game_state.untap_permanent):
                 if game_state.untap_permanent(target_id, target_controller):
                     untapped = True
            else: # Basic fallback
                 tapped_set = target_controller.setdefault("tapped_permanents", set())
                 if target_id in tapped_set:
                     tapped_set.remove(target_id)
                     logging.debug(f"Untapped {game_state._safe_get_card(target_id).name} (Basic)")
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
    
    
    