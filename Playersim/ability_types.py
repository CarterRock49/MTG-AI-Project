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
            # Check if ability requires targeting based on its effect text
            requires_target = "target" in getattr(self, 'effect', getattr(self, 'effect_text', '')).lower()
            targets = None # Targets will be resolved if needed

            # If targets are needed, resolve them
            if requires_target:
                targets = self._handle_targeting(game_state, controller)
                # Fizzle if targeting required but failed or yielded no targets
                if targets is None or (isinstance(targets, dict) and not any(targets.values())):
                    logging.debug(f"Targeting failed for ability: {self.effect_text}. Fizzling.")
                    return False

            # Delegate to specific implementation, passing resolved targets
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
            # Prefer GameState's targeting system instance first
            if hasattr(game_state, 'targeting_system') and game_state.targeting_system:
                # Pass the correct effect text (prefer self.effect if exists)
                text_for_targeting = getattr(self, 'effect', self.effect_text)
                return game_state.targeting_system.resolve_targeting(
                    self.card_id, controller, text_for_targeting)

            # Check AbilityHandler's targeting system as a secondary option
            elif hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system') and game_state.ability_handler.targeting_system:
                text_for_targeting = getattr(self, 'effect', self.effect_text)
                # Method name might be different here, use the specific one if known
                # Assuming resolve_targeting_for_ability exists
                if hasattr(game_state.ability_handler.targeting_system, 'resolve_targeting_for_ability'):
                    return game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
                        self.card_id, text_for_targeting, controller)
                # Fallback if method name differs or is resolve_targeting
                elif hasattr(game_state.ability_handler.targeting_system, 'resolve_targeting'):
                    return game_state.ability_handler.targeting_system.resolve_targeting(
                        self.card_id, controller, text_for_targeting)


            # Fall back to simple targeting if no system instance found
            text_for_targeting = getattr(self, 'effect', self.effect_text)
            logging.warning(f"TargetingSystem instance not found on GameState or AbilityHandler. Falling back to simple targeting for {self.card_id}")
            return self._resolve_simple_targeting(game_state, controller, text_for_targeting)

    def _resolve_ability_implementation(self, game_state, controller, targets=None):
        """Ability-specific implementation of resolution. Uses EffectFactory if not overridden."""
        # logging.warning(f"Default ability resolution used for {self.effect_text}")
        # Default: Create effects from the primary effect text and apply them
        effect_text_to_use = getattr(self, 'effect', getattr(self, 'effect_text', None))
        if not effect_text_to_use:
            logging.error(f"Cannot resolve ability implementation for {self.card_id}: Missing effect text.")
            return False

        effects = self._create_ability_effects(effect_text_to_use, targets)
        if not effects:
            logging.warning(f"No effects created for ability: {effect_text_to_use}")
            return False

        success = True
        for effect_obj in effects:
            if not effect_obj.apply(game_state, self.card_id, controller, targets):
                 success = False # Mark failure if any effect fails, but try others

        return success


    def _create_ability_effects(self, effect_text, targets=None):
        """Create appropriate AbilityEffect objects based on the effect text"""
        return EffectFactory.create_effects(effect_text, targets)

    def _resolve_simple_targeting(self, game_state, controller, effect_text):
        """Simplified targeting resolution when targeting system isn't available"""
        return resolve_simple_targeting(game_state, self.card_id, controller, effect_text)

    def __str__(self):
        return f"Ability({self.effect_text})"


class ActivatedAbility(Ability):
    """Ability that can be activated by paying a cost"""
    def __init__(self, card_id, cost=None, effect=None, effect_text=""):
        super().__init__(card_id, effect_text)
        # Allow parsing from effect_text if cost/effect not provided
        parsed_cost, parsed_effect = None, None
        if cost is None and effect is None and effect_text:
             parsed_cost, parsed_effect = self._parse_cost_effect(effect_text)
        self.cost = cost if cost is not None else parsed_cost
        self.effect = effect if effect is not None else parsed_effect

        # Validation after potential parsing
        if self.cost is None or self.effect is None: # Allow empty cost/effect if text provides it implicitly? Check rules. For now, require both.
            raise ValueError(f"ActivatedAbility requires cost and effect. Got cost='{self.cost}', effect='{self.effect}' from text='{effect_text}'")

        # Store original text if not provided
        if not effect_text:
            self.effect_text = f"{self.cost}: {self.effect}"
        
    def _parse_cost_effect(self, text):
        """Attempt to parse 'Cost: Effect' format."""
        match = re.match(r'^\s*([^:]+?)\s*:\s*(.+)\s*$', text.strip())
        if match:
            cost_part = match.group(1).strip()
            effect_part = match.group(2).strip()
            # Basic validation: Cost should contain '{' or keyword like 'Tap'
            if '{' in cost_part or re.search(r'\b(tap|sacrifice|discard|pay)\b', cost_part.lower()):
                 return cost_part, effect_part
        # Check for keyword costs without colon (e.g., Cycling {2}, Equip {1})
        match_keyword_cost = re.match(r"^(cycling|equip|flashback|kicker|level up|morph|unearth|reconfigure|fortify|channel|adapt|monstrosity)\s*(.*?)(?::|$)", text.lower().strip())
        if match_keyword_cost:
            keyword = match_keyword_cost.group(1)
            # Extract cost from the rest of the text
            rest_of_text = match_keyword_cost.group(2).strip()
            cost_match = re.search(r"(\{[^}]+\}|[0-9]+)", rest_of_text)
            cost_part = cost_match.group(1) if cost_match else "{0}" # Default free? Risky.
            if cost_part.isdigit(): cost_part = f"{{{cost_part}}}"
            # Effect is derived from the keyword action itself
            effect_map = {
                "cycling": "Discard this card: Draw a card.",
                "equip": "Attach to target creature.",
                "flashback": "Cast from graveyard, then exile.",
                "level up": "Put a level counter on this.",
                "morph": "Turn this face up.",
                # Add more keyword effects
            }
            effect_part = effect_map.get(keyword, f"Perform {keyword} effect.")
            return cost_part, effect_part

        # Assume no cost found if no ':' or keyword pattern matched
        logging.debug(f"Could not parse Cost: Effect from '{text}'")
        return None, text # Assume entire text is the effect if cost not found

    def resolve(self, game_state, controller, targets=None):
        """Resolve this activated ability using the default implementation."""
        # Overriding resolve allows specific subclasses (like ManaAbility) to change behavior.
        # This calls the default Ability._resolve_ability_implementation.
        return super()._resolve_ability_implementation(game_state, controller, targets)


    def resolve_with_targets(self, game_state, controller, targets=None):
        """Resolve this ability with specific targets."""
        # This method is useful if the activation logic needs to pass pre-selected targets.
        # Default implementation calls the main resolve logic.
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
        cost_text = self.cost.lower()
        all_costs_paid = True

        # --- Non-Mana Costs FIRST ---
        # Tap Cost
        if "{t}" in cost_text:
             if not game_state.tap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay tap cost: {game_state._safe_get_card(self.card_id).name} couldn't be tapped.")
                 return False
             logging.debug(f"Paid tap cost for {game_state._safe_get_card(self.card_id).name}")
        # Untap Cost {Q} (Less common)
        if "{q}" in cost_text:
             if not game_state.untap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay untap cost for {game_state._safe_get_card(self.card_id).name}")
                 return False
             logging.debug(f"Paid untap cost for {game_state._safe_get_card(self.card_id).name}")

        # Sacrifice Cost
        sac_match = re.search(r"sacrifice (a|an|another|\d*)?\s*([^:,{]+)", cost_text)
        if sac_match:
             sac_req = sac_match.group(0).replace("sacrifice ", "").strip() # Get the full requirement text
             if game_state.ability_handler._can_sacrifice(game_state, controller, sac_req):
                 if not game_state.ability_handler._pay_sacrifice_cost(game_state, controller, sac_req, self.card_id): # Pass source ID
                     return False # Failed to pay sacrifice
             else:
                 logging.debug(f"Cannot meet sacrifice requirement: {sac_req}")
                 return False
        # Discard Cost
        discard_match = re.search(r"discard (\w+|\d*) cards?", cost_text)
        if discard_match:
             count_str = discard_match.group(1)
             count = text_to_number(count_str)
             if len(controller["hand"]) < count:
                 logging.debug("Cannot pay discard cost: not enough cards.")
                 return False
             # TODO: Implement choice for discard if not random
             # Simple: discard first N cards
             for _ in range(count):
                  if controller["hand"]:
                       discard_id = controller["hand"][0]
                       game_state.move_card(discard_id, controller, "hand", controller, "graveyard")
             logging.debug(f"Paid discard cost ({count} cards).")
        # Pay Life Cost
        life_match = re.search(r"pay (\d+) life", cost_text)
        if life_match:
             amount = int(life_match.group(1))
             if controller["life"] < amount:
                 logging.debug("Cannot pay life cost: not enough life.")
                 return False
             controller["life"] -= amount
             logging.debug(f"Paid {amount} life.")
        # Remove Counters Cost
        counter_match = re.search(r"remove (\w+|\d*) ([\w\s\-]+) counters?", cost_text)
        if counter_match:
             count_str, counter_type = counter_match.groups()
             count = text_to_number(count_str)
             counter_type = counter_type.strip()
             if not game_state.add_counter(self.card_id, counter_type, -count): # Use add_counter with negative
                 logging.debug(f"Cannot remove {count} {counter_type} counters.")
                 return False
             logging.debug(f"Paid by removing {count} {counter_type} counters.")

        # --- Mana Costs LAST ---
        if hasattr(game_state, 'mana_system') and game_state.mana_system:
             mana_cost_str = re.sub(r"(?:\{[tq]\}|sacrifice.*?|discard.*?|pay \d+ life|remove.*?)(?:,|$)\s*", "", self.cost).strip()
             if mana_cost_str: # Check if there's any mana cost left
                 parsed_cost = game_state.mana_system.parse_mana_cost(mana_cost_str)
                 if not game_state.mana_system.pay_mana_cost(controller, parsed_cost):
                     logging.warning(f"Failed to pay mana cost '{mana_cost_str}' after non-mana costs.")
                     # IMPORTANT: Rollback non-mana costs if mana payment fails
                     # This is complex and not fully implemented here. Assume failure is final.
                     return False
                 logging.debug(f"Paid mana cost: {mana_cost_str}")
        else:
             # Basic mana check/payment if no system
             if any(c in cost_text for c in "WUBRGC123456789X"):
                 if sum(controller["mana_pool"].values()) == 0: return False # Simplistic check
                 controller["mana_pool"] = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0} # Assume all mana used

        logging.debug(f"Successfully paid cost '{self.cost}' for {game_state._safe_get_card(self.card_id).name}")
        return True
    
    def _can_sacrifice(game_state, controller, sacrifice_req):
        """Basic check if controller can meet sacrifice requirements"""
        if not sacrifice_req: return False
        req_lower = sacrifice_req.lower()
        valid_types = ['creature', 'artifact', 'enchantment', 'land', 'planeswalker', 'permanent']
        req_type = next((t for t in valid_types if t in req_lower), None)

        # Check if self sacrifice
        if "this permanent" in req_lower or "this creature" in req_lower or req_lower == 'it':
            # In pay_cost, self.card_id will be the source card ID.
            # Here, we only check if *a* sacrifice is possible, specific card checked later.
            return True # Assume the source itself is valid if required.

        # Check if player controls any permanent of the required type
        if req_type:
            for card_id in controller.get("battlefield", []):
                card = game_state._safe_get_card(card_id)
                if card and (req_type == 'permanent' or req_type in getattr(card, 'card_types', [])):
                    return True # Found at least one valid permanent
            return False # No valid permanent found

        # If no type specified, assume any permanent can be sacrificed
        return bool(controller.get("battlefield"))
    
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
    
    def _pay_sacrifice_cost(game_state, controller, sacrifice_req, ability_source_id):
        """Basic payment of sacrifice cost (AI chooses simplest valid target)"""
        if not sacrifice_req: return False
        req_lower = sacrifice_req.lower()
        valid_types = ['creature', 'artifact', 'enchantment', 'land', 'planeswalker', 'permanent']
        req_type = next((t for t in valid_types if t in req_lower), None)
        target_id_to_sacrifice = None

        if "this permanent" in req_lower or "this creature" in req_lower or req_lower == 'it':
            target_id_to_sacrifice = ability_source_id # Sacrifice the source itself
        else:
            # Find a suitable permanent (simple choice: first valid found)
            valid_options = []
            for card_id in controller.get("battlefield", []):
                card = game_state._safe_get_card(card_id)
                if card and (req_type == 'permanent' or not req_type or req_type in getattr(card, 'card_types', [])):
                    valid_options.append(card_id)
            # Basic AI: sacrifice least valuable (e.g., lowest CMC, or a token)
            if valid_options:
                # Prefer tokens
                tokens = [opt for opt in valid_options if "TOKEN" in opt]
                if tokens: target_id_to_sacrifice = tokens[0]
                else:
                    # Choose lowest CMC non-token
                    non_tokens = sorted([opt for opt in valid_options if "TOKEN" not in opt], key=lambda cid: getattr(game_state._safe_get_card(cid), 'cmc', 99))
                    if non_tokens: target_id_to_sacrifice = non_tokens[0]

        if target_id_to_sacrifice and target_id_to_sacrifice in controller.get("battlefield", []):
            sac_card_name = getattr(game_state._safe_get_card(target_id_to_sacrifice), 'name', target_id_to_sacrifice)
            if game_state.move_card(target_id_to_sacrifice, controller, "battlefield", controller, "graveyard"):
                logging.debug(f"Sacrificed {sac_card_name} to pay cost.")
                return True
        logging.warning(f"Could not find valid permanent to sacrifice for '{sacrifice_req}'")
        return False
    
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
    def __init__(self, card_id, trigger_condition=None, effect=None, effect_text="", additional_condition=None):
        super().__init__(card_id, effect_text)
        # Allow parsing from effect_text if condition/effect not provided
        parsed_condition, parsed_effect = None, None
        if trigger_condition is None and effect is None and effect_text:
            parsed_condition, parsed_effect = self._parse_condition_effect(effect_text)
        self.trigger_condition = (trigger_condition if trigger_condition is not None else parsed_condition or "Unknown").lower()
        self.effect = (effect if effect is not None else parsed_effect or "Unknown").lower()
        self.additional_condition = additional_condition  # Extra condition beyond the trigger

        # Validation after potential parsing
        if not self.trigger_condition or self.trigger_condition == "unknown":
             raise ValueError(f"TriggeredAbility requires trigger_condition. Got text='{effect_text}'")
        if not self.effect or self.effect == "unknown":
             raise ValueError(f"TriggeredAbility requires effect. Got text='{effect_text}'")

        # Store original text if not provided
        if not effect_text:
            self.effect_text = f"{self.trigger_condition.capitalize()}, {self.effect.capitalize()}."
            
    def _parse_condition_effect(self, text):
        """Attempt to parse 'When/Whenever/At..., Effect.' format."""
        # More robust regex to handle variations and potential intervening text
        match = re.match(r'^\s*(when|whenever|at)\s+([^,]+?),?\s*(.+)\s*$', text.strip(), re.IGNORECASE | re.DOTALL)
        if match:
             # Combine trigger parts
             trigger_part = f"{match.group(1)} {match.group(2)}".strip()
             effect_part = match.group(3).strip()
             # Simple validation: effect shouldn't contain trigger keywords unless nested
             if not re.match(r'^(when|whenever|at)\b', effect_part.lower()):
                 # Remove trailing period if present
                 if effect_part.endswith('.'): effect_part = effect_part[:-1]
                 return trigger_part, effect_part
        logging.debug(f"Could not parse Trigger, Effect from '{text}'")
        return None, None
        
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
        return self._resolve_ability_implementation(game_state, controller, targets)

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

    def _evaluate_condition(self, condition_text, context):
        """Evaluate if a trigger's conditional clause is met."""
        if not condition_text or not context: return True
        gs = context.get('game_state')
        controller = context.get('controller') # Controller of the trigger source
        if not gs or not controller: return True

        # Use the card evaluator for condition checking if available
        if hasattr(gs, 'card_evaluator') and gs.card_evaluator:
            try:
                # Pass context and condition text
                return gs.card_evaluator.evaluate_condition(condition_text, context)
            except NotImplementedError:
                logging.warning(f"CardEvaluator does not implement condition: {condition_text}")
            except Exception as e:
                logging.error(f"Error evaluating condition via CardEvaluator: {e}")

        # --- Basic Fallback Parsing ---
        logging.debug(f"Evaluating basic condition: '{condition_text}'")
        # Check "if you control..."
        control_match = re.search(r"if you control (?:a|an|another|\d+)?\s*([\w\s\-]+?)(?: with|$)", condition_text)
        if control_match:
            required_type = control_match.group(1).strip()
            return any(self._card_matches_criteria(gs._safe_get_card(cid), required_type)
                       for cid in controller.get("battlefield", []))

        # Check opponent control
        opp_control_match = re.search(r"if an opponent controls (?:a|an|\d+)?\s*([\w\s\-]+?)(?: with|$)", condition_text)
        if opp_control_match:
            required_type = opp_control_match.group(1).strip()
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            return any(self._card_matches_criteria(gs._safe_get_card(cid), required_type)
                       for cid in opponent.get("battlefield", []))

        # Check life total
        life_match = re.search(r"if (you have|your life total is) (\d+) or more life", condition_text)
        if life_match and controller["life"] >= int(life_match.group(2)): return True
        life_match = re.search(r"if (you have|your life total is) (\d+) or less life", condition_text)
        if life_match and controller["life"] <= int(life_match.group(2)): return True

        # Check card count in hand/graveyard
        card_count_match = re.search(r"if you have (\d+) or more cards in (your hand|your graveyard)", condition_text)
        if card_count_match:
             count = int(card_count_match.group(1))
             zone = card_count_match.group(2).replace("your ", "")
             if len(controller.get(zone, [])) >= count: return True

        logging.warning(f"Could not parse trigger condition: '{condition_text}'. Assuming True.")
        return True # Default to true if condition unparsed
    
    def _check_additional_condition(self, context):
        """Checks self.additional_condition using the same evaluation logic."""
        if not self.additional_condition: return True
        return self._evaluate_condition(self.additional_condition, context)
    

    def resolve(self, game_state, controller, targets=None):
        """Resolve this triggered ability using the default implementation."""
        return super()._resolve_ability_implementation(game_state, controller, targets)


class StaticAbility(Ability):
    """Continuous ability that affects the game state"""
    def __init__(self, card_id, effect, effect_text=""):
        super().__init__(card_id, effect_text)
        self.effect = effect.lower() if effect else "" # Handle potential None effect
        # Set effect_text from effect if not provided
        if not effect_text and self.effect:
            self.effect_text = self.effect.capitalize()
        
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
        # Layer 7a: Set Base P/T (e.g., from copy effects or abilities setting base)
        match = re.search(r"(?:base power and toughness|base power|base toughness)\s+(?:is|are)\s+(\d+)/(\d+)", effect_lower)
        if match:
            power = safe_int(match.group(1)); toughness = safe_int(match.group(2))
            if power is not None and toughness is not None:
                 return {'sublayer': 'a', 'effect_type': 'set_base_pt', 'effect_value': (power, toughness)}
        # Handle Characteristic-Defining Abilities setting base P/T
        match_cda = re.search(r"(?:power and toughness are each equal to|power is equal to|toughness is equal to)\b", effect_lower)
        if match_cda:
             # Register CDA P/T setting effect, actual calculation deferred to LayerSystem application
             cda_type = 'unknown'
             if "number of cards in your graveyard" in effect_lower: cda_type = 'graveyard_count_self'
             elif "number of creatures you control" in effect_lower: cda_type = 'creature_count_self'
             # Add more common CDA types
             logging.debug(f"Registering Layer 7a CDA effect: {cda_type}")
             return {'sublayer': 'a', 'effect_type': 'set_pt_cda', 'effect_value': cda_type} # Pass CDA type identifier

        # Layer 7b: Setting P/T to specific values (without changing base P/T). Examples: "becomes a 1/1", "is a 0/1"
        # Note: These often come with type changes in Layer 4. Layer 7 only handles the P/T part.
        match = re.search(r"\bis a\b\s+(\d+)/(\d+)", effect_lower) or re.search(r"\bbecomes a\b\s+(\d+)/(\d+)", effect_lower)
        if match:
             power = safe_int(match.group(1)); toughness = safe_int(match.group(2))
             if power is not None and toughness is not None:
                  return {'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)}

        # Layer 7c: P/T modification from static abilities (+X/+Y, -X/-Y), anthems etc.
        # Simple +/- N/N modifications
        match = re.search(r"gets? ([+\-]\d+)/([+\-]\d+)", effect_lower)
        if match:
            p_mod = safe_int(match.group(1)); t_mod = safe_int(match.group(2))
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Anthem patterns (+N/+N)
        match = re.search(r"(?:get|have)\s*\+\s*(\d+)/\+\s*(\d+)", effect_lower)
        if match:
            p_mod = safe_int(match.group(1)); t_mod = safe_int(match.group(2))
            if p_mod is not None and t_mod is not None:
                 return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Penalty patterns (-N/-N)
        match = re.search(r"(?:get|have)\s*\-\s*(\d+)/\-\s*(\d+)", effect_lower)
        if match:
            p_mod = -safe_int(match.group(1), 0); t_mod = -safe_int(match.group(2), 0)
            if p_mod is not None and t_mod is not None: # Check result of safe_int
                return {'sublayer': 'c', 'effect_type': 'modify_pt', 'effect_value': (p_mod, t_mod)}
        # Variable P/T modification (e.g., +X/+X where X is count)
        match_var = re.search(r"(?:get|have)\s*\+X/\+X\s+where X is the number of (\w+)", effect_lower)
        if match_var:
             count_type = match_var.group(1).strip()
             # Register variable P/T effect, calculation deferred
             logging.debug(f"Registering Layer 7c variable P/T effect based on: {count_type}")
             return {'sublayer': 'c', 'effect_type': 'modify_pt_variable', 'effect_value': count_type}

        # Layer 7d: Switch P/T
        if "switch" in effect_lower and "power and toughness" in effect_lower:
            return {'sublayer': 'd', 'effect_type': 'switch_pt', 'effect_value': True}

        return None # No Layer 7 effect parsed

    def _parse_layer6_effect(self, effect_lower):
        """Parse ability adding/removing effects for Layer 6."""
        # Add common abilities
        common_keywords = [
            "flying", "first strike", "double strike", "trample", "vigilance", "haste",
            "lifelink", "deathtouch", "indestructible", "hexproof", "shroud", "reach",
            "menace", "defender", "unblockable", "protection from", "ward" # Add others as needed
        ]
        for kw in common_keywords:
            # Use word boundaries for single keywords to avoid partial matches (e.g., "linking" != "lifelink")
            pattern = r"(?:have|has|gains|gain)\s+"
            if ' ' not in kw and kw in ["flash", "haste", "reach", "ward", "band", "fear"]: # Use word boundary
                pattern += r'\b' + re.escape(kw) + r'\b'
            else: # Use substring check for multi-word or less ambiguous
                pattern += re.escape(kw)

            match = re.search(pattern, effect_lower)
            if match:
                 # Handle parametrized keywords
                 if kw == "protection from":
                     protected_from_match = re.search(r"protection from ([\w\s]+)", effect_lower)
                     protected_from = protected_from_match.group(1).strip() if protected_from_match else "unknown"
                     return {'effect_type': 'add_ability', 'effect_value': f"protection from {protected_from}"}
                 elif kw == "ward":
                     ward_cost_match = re.search(r"ward (\{.*?\})", effect_lower) or re.search(r"ward (\d+)", effect_lower)
                     ward_cost = ward_cost_match.group(1).strip() if ward_cost_match else "{1}" # Default ward {1}
                     if ward_cost.isdigit(): ward_cost = f"{{{ward_cost}}}" # Normalize
                     return {'effect_type': 'add_ability', 'effect_value': f"ward {ward_cost}"}
                 else:
                      # Handle simple keywords directly
                      # Normalize keyword to match Card.ALL_KEYWORDS if possible
                      normalized_kw = kw # Placeholder
                      for official_kw in Card.ALL_KEYWORDS:
                           if kw == official_kw.lower():
                               normalized_kw = official_kw
                               break
                      return {'effect_type': 'add_ability', 'effect_value': normalized_kw}


        # Remove abilities
        if "lose all abilities" in effect_lower:
            return {'effect_type': 'remove_all_abilities', 'effect_value': True}
        # Simple "loses X" check
        lose_match = re.search(r"loses (flying|trample|etc)", effect_lower) # Add keywords to check
        if lose_match:
            return {'effect_type': 'remove_ability', 'effect_value': lose_match.group(1).strip()}

        # Prevent attacking/blocking
        if "can't attack" in effect_lower: return {'effect_type': 'cant_attack', 'effect_value': True}
        if "can't block" in effect_lower: return {'effect_type': 'cant_block', 'effect_value': True}
        if "attacks each combat if able" in effect_lower: return {'effect_type': 'must_attack', 'effect_value': True}
        if "blocks each combat if able" in effect_lower: return {'effect_type': 'must_block', 'effect_value': True}


        return None

    def _parse_layer5_effect(self, effect_lower):
        """Parse color adding/removing effects for Layer 5."""
        colors_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        color_indices = {'W': 0, 'U': 1, 'B': 2, 'R': 3, 'G': 4}
        target_colors = None # None means no change from this effect
        effect_type = None

        # Check if SETTING specific colors (e.g., "is blue", "are white and black")
        is_setting = False
        found_colors_in_set = [0] * 5
        if re.search(r"\bis\b|\bare\b", effect_lower) and not re.search(r"\bis also\b|\bare also\b", effect_lower):
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + color_name + r'\b', effect_lower):
                       found_colors_in_set[index] = 1
                       is_setting = True
             # Check for "is colorless"
             if "colorless" in effect_lower and not any(found_colors_in_set):
                  found_colors_in_set = [0] * 5
                  is_setting = True

             if is_setting:
                  effect_type = 'set_color'
                  target_colors = found_colors_in_set

        # Check if ADDING colors (e.g., "is also blue")
        elif re.search(r"\bis also\b|\bare also\b", effect_lower):
             added_colors = [0] * 5
             found_addition = False
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + color_name + r'\b', effect_lower):
                       added_colors[index] = 1
                       found_addition = True
             if found_addition:
                  effect_type = 'add_color'
                  target_colors = added_colors

        # Check if removing colors / becoming colorless
        elif "loses all colors" in effect_lower or "becomes colorless" in effect_lower:
             effect_type = 'set_color'
             target_colors = [0,0,0,0,0]

        if effect_type and target_colors is not None:
             return {'effect_type': effect_type, 'effect_value': target_colors}

        return None # No Layer 5 effect parsed


    def _parse_layer4_effect(self, effect_lower):
        """Parse type/subtype adding/removing effects for Layer 4."""
        # Patterns to detect type/subtype changes
        set_type_match = re.search(r"becomes? a(?:n)? ([\w\s]+?) (?:in addition|until|$)", effect_lower) # e.g., "becomes an artifact creature"
        add_type_match = re.search(r"is also a(?:n)? (\w+)", effect_lower) # e.g., "is also an artifact"
        set_subtype_match = re.search(r"becomes? a(?:n)? ([\w\s]+?) creature", effect_lower) # Specific case: "becomes a Goblin creature" implies subtype change
        add_subtype_match = re.search(r"(?:is|are) also ([\w\s]+)", effect_lower) # e.g., "are also Saprolings"
        lose_type_match = re.search(r"loses all creature types", effect_lower) # Example removal

        # --- Process Type Setting/Adding ---
        if set_type_match:
             type_text = set_type_match.group(1).strip()
             # Determine if it's setting or adding based on "in addition"
             is_addition = "in addition" in set_type_match.group(0)

             # Split into card types and potential subtypes
             parts = type_text.split()
             types = [p for p in parts if p in Card.ALL_CARD_TYPES] # Filter known card types
             subtypes = [p for p in parts if p not in Card.ALL_CARD_TYPES and p.capitalize() in Card.SUBTYPE_VOCAB] # Check known subtypes

             if types: # Change primary card types
                  effect_type = 'add_type' if is_addition else 'set_type'
                  logging.debug(f"Layer 4: Parsed {effect_type} with value {types}")
                  # If setting type, also potentially clears subtypes? Rules check.
                  # Let's assume set_type implies setting ONLY these types (clears old subtypes unless re-specified)
                  if not is_addition: # Also setting subtypes if specified with type
                      return {'effect_type': 'set_type_and_subtype', 'effect_value': (types, subtypes)}
                  else: # Just adding the type
                       return {'effect_type': effect_type, 'effect_value': types} # Return list of types
             # If no card types matched but parts exist, assume it might be adding subtypes implicitly
             elif subtypes and is_addition:
                  logging.debug(f"Layer 4: Parsed add_subtype from 'becomes' clause: {subtypes}")
                  return {'effect_type': 'add_subtype', 'effect_value': subtypes} # Return list

        # Handle "is also a [type]"
        elif add_type_match:
             type_text = add_type_match.group(1).strip()
             if type_text in Card.ALL_CARD_TYPES:
                  logging.debug(f"Layer 4: Parsed add_type with value {[type_text]}")
                  return {'effect_type': 'add_type', 'effect_value': [type_text]}
             elif type_text.capitalize() in Card.SUBTYPE_VOCAB: # Check if it's a subtype instead
                  logging.debug(f"Layer 4: Parsed add_subtype from 'is also a' clause: {[type_text.capitalize()]}")
                  return {'effect_type': 'add_subtype', 'effect_value': [type_text.capitalize()]}

        # --- Process Subtype Setting/Adding ---
        elif add_subtype_match:
             # "are also Saprolings"
             subtype_text = add_subtype_match.group(1).strip()
             # Check if the word(s) are known subtypes
             potential_subtypes = [s.capitalize() for s in subtype_text.split() if s.capitalize() in Card.SUBTYPE_VOCAB]
             if potential_subtypes:
                  logging.debug(f"Layer 4: Parsed add_subtype with value {potential_subtypes}")
                  return {'effect_type': 'add_subtype', 'effect_value': potential_subtypes} # Return list of subtypes

        # --- Process Type/Subtype Removal ---
        elif lose_type_match:
             logging.debug("Layer 4: Parsed lose_all_subtypes (Creature)")
             return {'effect_type': 'lose_all_subtypes', 'effect_value': 'creature'} # Specify which subtypes to lose

        # Add specific subtype removal if needed: re.search(r"is no longer a (\w+)", effect_lower) -> 'remove_subtype'

        return None # No Layer 4 effect parsed

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
        # Effect text derived implicitly for ManaAbility if not provided
        effect = f"Add {self._format_mana(mana_produced)}."
        if not effect_text:
            effect_text = f"{cost}: {effect}"
        super().__init__(card_id, cost, effect, effect_text)
        self.mana_produced = mana_produced # Expects dict like {'G': 1, 'C': 2}


    def _format_mana(self, mana_dict):
        """Helper to format mana dict into string like {G}{G}{1}"""
        parts = []
        for color in ['W', 'U', 'B', 'R', 'G']:
             parts.extend([f"{{{color}}}"] * mana_dict.get(color, 0))
        if mana_dict.get('C', 0): parts.append(f"{{{mana_dict['C']}}}")
        if mana_dict.get('X', 0): parts.append(f"{{{mana_dict['X']}X}}") # How to represent X?
        # Add other types (Snow, Phyrexian, Hybrid) if needed
        return "".join(parts)

    def resolve(self, game_state, controller):
        """Add the produced mana to the controller's mana pool using ManaSystem"""
        if hasattr(game_state, 'mana_system') and game_state.mana_system:
             game_state.mana_system.add_mana(controller, self.mana_produced)
        else: # Fallback if no mana system
             for color, amount in self.mana_produced.items():
                  pool = controller.setdefault("mana_pool", {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0})
                  pool[color] = pool.get(color, 0) + amount
                  card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
                  logging.debug(f"(Fallback) Mana ability of {card_name} added {amount} {color} mana.")
        return True # Mana abilities usually succeed if cost paid

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
        if self.requires_target and (not effective_targets or not any(v for v in effective_targets.values())): # Check if any target list is non-empty
            logging.debug(f"Effect '{self.effect_text}' requires target, resolving...")
            resolved_targets = None
            # Prefer GameState's targeting system if available
            if hasattr(game_state, 'targeting_system') and game_state.targeting_system:
                # Resolve targeting based on the effect text itself
                # Ensure source_id is valid before attempting to get the card
                if source_id:
                    source_card = game_state._safe_get_card(source_id)
                    if source_card: # Proceed only if source card found
                        resolved_targets = game_state.targeting_system.resolve_targeting(source_id, controller, self.effect_text)
                    else:
                        logging.warning(f"Source card {source_id} not found for targeting.")
                else:
                     logging.warning("Source ID missing for targeting.")

            elif hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
                 # Ensure source_id exists
                 if source_id:
                     resolved_targets = game_state.ability_handler.targeting_system.resolve_targeting_for_ability(source_id, self.effect_text, controller)
                 else:
                      logging.warning("Source ID missing for targeting via ability handler.")

            else: # Fallback to simple targeting
                 if source_id:
                     resolved_targets = resolve_simple_targeting(game_state, source_id, controller, self.effect_text)
                 else:
                      logging.warning("Source ID missing for simple targeting.")


            if resolved_targets and any(v for v in resolved_targets.values()):
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
        # Default implementation logs a warning
        logging.warning(f"_apply_effect not implemented for effect type: {type(self).__name__} ('{self.effect_text}')")
        return False # Return False to indicate failure


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
        # Determine description based on count
        count_text = f"{count} cards" if isinstance(count, int) and count > 1 else "a card" if count == 1 else f"{count} card(s)"
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} draws {count_text}", condition)
        self.count = count
        self.target = target # e.g., "controller", "opponent", "target_player", "each_player"
        self.requires_target = "target" in target # Check if specific targeting is needed


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_players = []
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", [])
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"DrawCardEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([game_state.p1, game_state.p2])

        if not target_players: return False

        overall_success = True
        for p in target_players:
            num_drawn = 0
            success_player = True
            for _ in range(self.count):
                if hasattr(game_state, '_draw_card'): # Use GameState method preferred
                    drawn_card_id = game_state._draw_card(p)
                    if drawn_card_id: num_drawn += 1
                    else: success_player = False; break
                else: # Fallback
                    if p["library"]:
                         card_drawn = p["library"].pop(0); p["hand"].append(card_drawn)
                         num_drawn += 1
                    else: p["attempted_draw_from_empty"] = True; success_player = False; break
            logging.debug(f"DrawCardEffect: Player {p['name']} drew {num_drawn} card(s).")
            overall_success &= success_player

        return overall_success


class GainLifeEffect(AbilityEffect):
    """Effect that causes players to gain life."""
    def __init__(self, amount, target="controller", condition=None):
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} gain {amount} life", condition)
        self.amount = amount
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_players = []
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", [])
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"GainLifeEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([game_state.p1, game_state.p2])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             if hasattr(game_state, 'gain_life'):
                  actual_gained = game_state.gain_life(p, self.amount, source_id)
                  # Assume gain_life returns amount gained. If 0 or negative, could be prevented/replaced.
                  if actual_gained <= 0:
                      # Check if it was due to replacement, which counts as success
                      # This requires more context or a different return from gain_life.
                      # Simplified: consider it failure if 0 life gained.
                      # overall_success = False
                      logging.debug(f"GainLifeEffect (via gs): Life gain for {p['name']} resulted in {actual_gained} net gain.")
                  else: pass # Log handled inside gain_life
             else: # Fallback
                  # Manual replacement check needed here ideally
                  original_life = p.get('life', 0)
                  p['life'] += self.amount
                  gained = p['life'] - original_life
                  if gained > 0: logging.debug(f"GainLifeEffect (Manual): Player {p['name']} gained {gained} life.")
                  else: overall_success = False
        return overall_success


class DamageEffect(AbilityEffect):
    """Effect that deals damage to targets."""
    def __init__(self, amount, target_type="any target", condition=None):
        target_type_str = str(target_type).lower() if target_type is not None else "any target"
        super().__init__(f"Deal {amount} damage to {target_type_str}", condition)
        self.amount = amount
        self.target_type = target_type_str # e.g., "creature", "player", "any target", "each opponent"
        self.requires_target = "target" in self.target_type or "any" in self.target_type

    def _apply_effect(self, game_state, source_id, controller, targets):
        targets_to_damage = [] # List of (target_id, target_obj, target_owner, is_player)
        processed_ids = set()

        # Consolidate target IDs based on target_type and provided targets dict
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 logging.warning(f"DamageEffect requires targets, but none found in dict: {targets}")
                 return False
            # Extract all IDs from relevant categories provided
            relevant_categories = set()
            if self.target_type == "any target": relevant_categories = {"creatures", "players", "planeswalkers", "battles"}
            elif self.target_type == "creature": relevant_categories = {"creatures"}
            elif self.target_type == "player": relevant_categories = {"players"}
            elif self.target_type == "planeswalker": relevant_categories = {"planeswalkers"}
            elif self.target_type == "battle": relevant_categories = {"battles"}
            elif self.target_type == "permanent": relevant_categories = {"creatures", "planeswalkers", "battles", "artifacts", "enchantments", "lands"}
            else: # Specific target like "target opponent creature" needs TargetingSystem pre-filtering
                 relevant_categories.add(self.target_type + "s" if not self.target_type.endswith('s') else self.target_type)

            for cat, id_list in targets.items():
                if cat in relevant_categories:
                    for target_id in id_list:
                        if target_id not in processed_ids:
                            processed_ids.add(target_id)
                            targets_to_damage.append(target_id)

        elif "each opponent" in self.target_type:
             opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
             targets_to_damage.append("p2" if opponent == game_state.p2 else "p1")
        elif "each creature" in self.target_type:
             targets_to_damage.extend(game_state.get_all_creatures())
        elif "each player" in self.target_type:
             targets_to_damage.extend(["p1", "p2"])

        if not targets_to_damage:
             logging.warning(f"DamageEffect: No valid targets collected for '{self.effect_text}'. Provided: {targets}")
             return False

        # Check source characteristics
        has_lifelink = game_state.check_keyword(source_id, "lifelink") if hasattr(game_state, 'check_keyword') else False
        has_deathtouch = game_state.check_keyword(source_id, "deathtouch") if hasattr(game_state, 'check_keyword') else False
        has_infect = game_state.check_keyword(source_id, "infect") if hasattr(game_state, 'check_keyword') else False


        total_actual_damage = 0
        success_overall = False

        for target_id in targets_to_damage:
             target_owner, target_zone = game_state.find_card_location(target_id)
             is_player_target = target_id in ["p1", "p2"]
             target_obj = target_owner if is_player_target else game_state._safe_get_card(target_id)

             if not target_obj or (not is_player_target and target_zone != "battlefield"):
                  logging.debug(f"Damage target {target_id} invalid or not on battlefield.")
                  continue

             damage_applied = 0
             try:
                 if is_player_target:
                     # Infect applies poison counters instead of life loss
                     if has_infect:
                          target_owner.setdefault("poison_counters", 0)
                          target_owner["poison_counters"] += self.amount
                          damage_applied = self.amount # Track for lifelink based on intended damage
                          logging.debug(f"{target_owner['name']} got {self.amount} poison counters from infect.")
                     elif hasattr(game_state, 'damage_player'):
                          damage_applied = game_state.damage_player(target_owner, self.amount, source_id) # This returns actual damage dealt after replacements
                     else: # Fallback
                          target_owner['life'] -= self.amount
                          damage_applied = self.amount
                 else: # Permanent target
                      if 'creature' in getattr(target_obj, 'card_types', []):
                           if has_infect: # Damage is -1/-1 counters
                                self.add_counter(target_id, '-1/-1', self.amount)
                                damage_applied = self.amount # Track for lifelink based on intended damage
                           else:
                                damage_applied = game_state.apply_damage_to_permanent(target_id, self.amount, source_id, False, has_deathtouch)
                      elif 'planeswalker' in getattr(target_obj, 'card_types', []):
                           # Infect doesn't change PW damage
                           damage_applied = game_state.damage_planeswalker(target_id, self.amount, source_id)
                      elif 'battle' in getattr(target_obj, 'type_line', ''):
                           # Infect doesn't change battle damage
                           damage_applied = game_state.damage_battle(target_id, self.amount, source_id)

                 if damage_applied > 0:
                      total_actual_damage += damage_applied
                      success_overall = True
             except Exception as dmg_e:
                  logging.error(f"Error applying damage to {target_id}: {dmg_e}", exc_info=True)
                  # Continue to next target

        # Apply lifelink based on total actual damage dealt this instance
        if has_lifelink and total_actual_damage > 0:
            # Gain life using the appropriate method, considering replacements
            if hasattr(game_state, 'gain_life'):
                game_state.gain_life(controller, total_actual_damage, source_id)
            else: # Fallback
                controller['life'] += total_actual_damage

        # SBAs checked in main loop
        return success_overall

class AddCountersEffect(AbilityEffect):
    """Effect that adds counters to permanents or players."""
    def __init__(self, counter_type, count=1, target_type="creature", condition=None):
        super().__init__(f"Put {count} {counter_type} counter(s) on target {target_type}", condition)
        self.counter_type = counter_type.replace('_','/') # Allow P/T format storage
        self.count = count
        self.target_type = target_type.lower() # Normalize
        self.requires_target = "target" in target_type


    def _apply_effect(self, game_state, source_id, controller, targets):
        if self.count <= 0: return True

        targets_to_affect = []
        processed_ids = set()

        # --- Target Collection ---
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 logging.warning(f"AddCountersEffect requires targets, none provided/resolved: {targets}")
                 return False
            # Determine relevant categories from target_type
            relevant_categories = set()
            if "creature" in self.target_type: relevant_categories.add("creatures")
            if "artifact" in self.target_type: relevant_categories.add("artifacts")
            if "planeswalker" in self.target_type: relevant_categories.add("planeswalkers")
            if "enchantment" in self.target_type: relevant_categories.add("enchantments")
            if "land" in self.target_type: relevant_categories.add("lands")
            if "permanent" in self.target_type: relevant_categories.update(["creatures", "artifacts", "enchantments", "planeswalkers", "lands", "battles"])
            if "player" in self.target_type: relevant_categories.add("players")
            if not relevant_categories: relevant_categories.add(self.target_type+"s") # Fallback pluralize

            for cat, id_list in targets.items():
                 if cat in relevant_categories:
                     targets_to_affect.extend(id_list)
        elif "self" == self.target_type:
             targets_to_affect.append(source_id)
        elif "each creature" == self.target_type:
             targets_to_affect.extend(game_state.get_all_creatures())
        elif "each opponent" == self.target_type:
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            opp_id = "p2" if opponent == game_state.p2 else "p1"
            targets_to_affect.append(opp_id)
        # Add other 'each' cases

        if not targets_to_affect:
            logging.warning(f"AddCountersEffect: No valid targets collected for '{self.effect_text}'. Targets provided: {targets}")
            return False

        # Use set to avoid processing duplicates if multiple categories targeted the same ID
        unique_targets = set(targets_to_affect)
        success_count = 0

        for target_id in unique_targets:
             target_owner, target_zone = game_state.find_card_location(target_id)
             is_player_target = target_id in ["p1", "p2"]
             target_obj = target_owner if is_player_target else game_state._safe_get_card(target_id)

             if not target_obj or (not is_player_target and target_zone != "battlefield"):
                 logging.debug(f"AddCountersEffect: Target {target_id} invalid or not on battlefield.")
                 continue

             if is_player_target: # Add counters to player (poison, energy, experience)
                 if self.counter_type == 'poison':
                     target_owner.setdefault("poison_counters", 0)
                     target_owner["poison_counters"] += self.count
                     success_count += 1
                     logging.debug(f"Added {self.count} poison counter(s) to player {target_owner['name']}.")
                 elif self.counter_type == 'energy':
                     target_owner.setdefault("energy_counters", 0)
                     target_owner["energy_counters"] += self.count
                     success_count += 1
                     logging.debug(f"Added {self.count} energy counter(s) to player {target_owner['name']}.")
                 # Add experience etc.
                 else:
                     logging.warning(f"Cannot add counter type '{self.counter_type}' to player.")

             else: # Add counters to permanent
                  # Use GameState's add_counter method for consistency
                  if hasattr(game_state, 'add_counter') and callable(game_state.add_counter):
                      if game_state.add_counter(target_id, self.counter_type, self.count):
                          success_count += 1
                          # Logging handled by add_counter
                  else: # Fallback
                      target_card = target_obj
                      if not hasattr(target_card, 'counters'): target_card.counters = {}
                      target_card.counters[self.counter_type] = target_card.counters.get(self.counter_type, 0) + self.count
                      logging.debug(f"Fallback AddCounters: Added {self.count} {self.counter_type} to {target_card.name}")
                      success_count += 1


        # SBAs checked in main loop after action/resolution completes
        return success_count > 0

# BuffEffect requires no changes, it registers with LayerSystem

class CreateTokenEffect(AbilityEffect):
    """Effect that creates token creatures."""
    def __init__(self, power, toughness, creature_type="Creature", count=1, keywords=None, colors=None, is_legendary=False, controller_gets=True, condition=None):
        token_desc = f"{count} {power}/{toughness} {','.join(colors) if colors else ''} {creature_type} token{' with ' + ', '.join(keywords) if keywords else ''}"
        super().__init__(f"Create {token_desc}", condition)
        self.power = power
        self.toughness = toughness
        self.creature_type = creature_type
        self.count = count
        self.keywords = keywords or []
        self.colors = colors # List of 'white', 'blue' etc. or None
        self.is_legendary = is_legendary
        self.controller_gets = controller_gets

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_player = controller
        if not self.controller_gets: # e.g., "opponent creates..."
            target_player = game_state.p2 if controller == game_state.p1 else game_state.p1

        # Convert color names to the 5-dim list format
        color_list = [0] * 5
        if self.colors:
            color_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
            for color_name in self.colors:
                if color_name.lower() in color_map:
                     color_list[color_map[color_name.lower()]] = 1

        # Handle "artifact creature" type line properly
        card_types_list = ["token"] # Always a token
        subtypes_list = []
        base_type = "Creature"
        if "artifact" in self.creature_type.lower(): card_types_list.append("artifact")
        if "creature" in self.creature_type.lower(): card_types_list.append("creature")
        # Extract base type and subtypes
        parts = self.creature_type.split()
        # Assume the last part is the main creature type, preceding are artifact/etc. or subtypes?
        # Heuristic: Check against known creature types
        main_type_found = False
        for part in reversed(parts):
             if part.capitalize() in Card.SUBTYPE_VOCAB and not main_type_found:
                  # Assuming the *last* subtype listed is the main creature type for naming
                  # unless it's 'artifact' or similar card type word.
                  if part.lower() not in ["artifact", "enchantment", "creature"]: # Needs more robust check
                      base_type = part.capitalize()
                      subtypes_list.append(base_type)
                      main_type_found = True
             elif part.capitalize() in Card.SUBTYPE_VOCAB:
                  subtypes_list.append(part.capitalize())

        if not main_type_found and parts: # If only "artifact" or similar given, use name
            base_type = parts[-1].capitalize()

        # Build type line
        type_line = "Token "
        if self.is_legendary: type_line += "Legendary "
        if "artifact" in card_types_list: type_line += "Artifact "
        if "creature" in card_types_list: type_line += "Creature "
        if "enchantment" in card_types_list: type_line += "Enchantment "
        type_line += f"— {' '.join(sorted(list(set(subtypes_list))))}" # Use sorted unique subtypes

        token_data = {
            "name": f"{base_type} Token",
            "type_line": type_line.strip(),
            "card_types": list(set(card_types_list)),
            "subtypes": sorted(list(set(subtypes_list))),
            "supertypes": ["legendary", "token"] if self.is_legendary else ["token"],
            "power": self.power,
            "toughness": self.toughness,
            "oracle_text": " ".join(self.keywords) if self.keywords else "",
            "keywords": [0] * len(Card.ALL_KEYWORDS),
            "colors": color_list,
            "is_token": True,
        }

        # Map keywords
        kw_indices = {kw.lower(): i for i, kw in enumerate(Card.ALL_KEYWORDS)}
        for kw in self.keywords:
             if kw.lower() in kw_indices:
                  token_data["keywords"][kw_indices[kw.lower()]] = 1

        created_token_ids = []
        for _ in range(self.count):
             if hasattr(game_state, 'create_token'):
                 token_id = game_state.create_token(target_player, token_data.copy())
                 if token_id: created_token_ids.append(token_id)
             else: # Fallback
                 # Simplified fallback, doesn't use full game_state methods
                 token_id = f"TOKEN_{random.randint(1000,9999)}_{token_data['name']}"
                 new_token = Card(token_data)
                 game_state.card_db[token_id] = new_token
                 target_player.setdefault("tokens",[]).append(token_id)
                 target_player["battlefield"].append(token_id)
                 created_token_ids.append(token_id)


        return len(created_token_ids) > 0



class ReturnToHandEffect(AbilityEffect):
    """Effect that returns cards to their owner's hand."""
    def __init__(self, target_type="permanent", zone="battlefield", condition=None):
        target_type_str = str(target_type).lower() if target_type is not None else "permanent"
        zone_str = str(zone).lower() if zone is not None else "battlefield"
        super().__init__(f"Return target {target_type_str} from {zone_str} to its owner's hand", condition)
        self.target_type = target_type_str
        self.zone = zone_str
        self.requires_target = "target" in self.effect_text.lower()


    def _apply_effect(self, game_state, source_id, controller, targets):
        returned_count = 0
        target_ids_to_process = []

        # --- Target Collection (Improved) ---
        relevant_categories = set()
        if "creature" == self.target_type: relevant_categories.add("creatures")
        elif "artifact" == self.target_type: relevant_categories.add("artifacts")
        # ... add other specific types ...
        elif "permanent" == self.target_type: relevant_categories.update(["creatures", "artifacts", "enchantments", "planeswalkers", "lands", "battles"])
        elif "card" == self.target_type: relevant_categories.add("cards") # Assumes target dict might have 'cards' key for GY/Exile targets
        else: relevant_categories.add(self.target_type + "s") # Pluralize fallback

        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                logging.warning(f"ReturnToHandEffect requires targets, none provided/resolved: {targets}")
                return False
            for category in relevant_categories:
                 target_ids_to_process.extend(targets.get(category, []))
        # Add handling for non-targeted effects like "Return all creatures..." if needed

        if not target_ids_to_process:
             logging.warning(f"ReturnToHandEffect: No valid target IDs collected for '{self.effect_text}'. Targets: {targets}")
             return False

        # Process unique targets
        for target_id in set(target_ids_to_process):
            location_info = game_state.find_card_location(target_id)
            if not location_info:
                 logging.warning(f"ReturnToHandEffect: Could not find location for target {target_id}.")
                 continue

            target_owner, current_zone = location_info

            # Validate source zone specified in effect constructor
            if self.zone != 'any' and current_zone != self.zone:
                logging.debug(f"ReturnToHandEffect: Target {target_id} not in expected zone '{self.zone}', found in '{current_zone}'. Skipping.")
                continue

            # Perform the move using GameState method
            if game_state.move_card(target_id, target_owner, current_zone, target_owner, "hand", cause="return_to_hand", context={"source_id": source_id}):
                 returned_count += 1
                 # Logging handled within move_card
            else:
                 logging.warning(f"ReturnToHandEffect: Failed to move {target_id} to hand from {current_zone}.")

        return returned_count > 0

class CounterSpellEffect(AbilityEffect):
    """Effect that counters a spell on the stack."""
    def __init__(self, target_type="spell", condition=None):
        target_type_str = str(target_type).lower() if target_type else "spell"
        super().__init__(f"Counter target {target_type_str}", condition)
        self.target_type = target_type_str
        self.requires_target = True


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = targets.get("spells", []) # Expect targets in 'spells' list
        if not target_ids:
            logging.warning(f"CounterSpellEffect failed: No spell target provided in targets {targets}")
            return False

        countered_count = 0
        # Typically counters one target, but handle list in case of "Counter up to two..."
        for target_id in target_ids:
            # Find the spell on the stack
            target_item = None
            target_index = -1
            for i, item in enumerate(game_state.stack):
                 if isinstance(item, tuple) and len(item) > 3 and item[0] == "SPELL" and item[1] == target_id:
                      target_item = item
                      target_index = i
                      break

            if not target_item:
                logging.warning(f"CounterSpellEffect: Target spell {target_id} not found on stack.")
                continue # Try next target if any

            spell_type, spell_id, spell_caster, spell_context = target_item
            spell = game_state._safe_get_card(spell_id)
            if not spell: continue # Should not happen

            # Check "can't be countered"
            # Use central check if available, otherwise text check
            can_be_countered = True
            if hasattr(game_state, 'check_rule'): # Ideal way
                 can_be_countered = not game_state.check_rule('cant_be_countered', {'card_id': spell_id})
            elif hasattr(spell, 'oracle_text'): # Fallback
                 can_be_countered = "can't be countered" not in spell.oracle_text.lower()

            if not can_be_countered:
                logging.debug(f"Cannot counter {spell.name} - it can't be countered")
                continue

            # Remove from stack and move to graveyard
            game_state.stack.pop(target_index)
            if not spell_context.get("is_copy", False): # Don't move copies
                # Handle replacements for going to GY (e.g., Rest in Peace -> Exile)
                # Use move_card with stack_implicit source
                game_state.move_card(spell_id, spell_caster, "stack_implicit", spell_caster, "graveyard", cause="countered")
            logging.debug(f"Countered {spell.name}")
            countered_count += 1
            # Stop after countering one spell unless effect says "up to N"?
            break # Default: Counter first valid target

        # Check SBAs? Unlikely needed here, main loop handles post-resolution checks.
        return countered_count > 0

class DiscardEffect(AbilityEffect):
    """Effect that causes players to discard cards."""
    def __init__(self, count=1, target="opponent", is_random=False, condition=None):
        count_text = "entire hand" if count == -1 else f"{count} card(s)"
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        random_text = " at random" if is_random else ""
        super().__init__(f"{target_desc} discards {count_text}{random_text}", condition)
        self.count = count
        self.target = target
        self.is_random = is_random
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_players = []
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", [])
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"DiscardEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([game_state.p1, game_state.p2])

        if not target_players: return False

        overall_success = False
        for p in target_players:
             player_id_str = "p1" if p == game_state.p1 else "p2"
             player_hand = p.get("hand", [])
             if not player_hand: continue # Cannot discard from empty hand

             discard_count_needed = len(player_hand) if self.count == -1 else min(self.count, len(player_hand))
             if discard_count_needed <= 0: continue

             cards_to_discard = []
             if self.is_random:
                  cards_to_discard = random.sample(player_hand, discard_count_needed)
             else:
                  # Player chooses - Needs AI/Player Interaction or default logic
                  # Default: Discard highest CMC cards
                  sorted_hand = sorted([(cid, getattr(game_state._safe_get_card(cid), 'cmc', 0)) for cid in player_hand], key=lambda x: -x[1])
                  cards_to_discard = [cid for cid, cmc in sorted_hand[:discard_count_needed]]

             num_discarded_this_player = 0
             for card_id in cards_to_discard:
                  # Double check card is still in hand before moving
                  if card_id in p.get("hand",[]):
                      # Check replacement effects for discard (e.g., Madness)
                      discard_context = {'card_id': card_id, 'player': p, 'cause': 'discard'}
                      modified_context, replaced = game_state.apply_replacement_effect("DISCARD", discard_context)

                      if replaced and modified_context.get('prevented', False):
                          logging.debug(f"Discard of {card_id} prevented by replacement.")
                          continue

                      final_dest_zone = modified_context.get('to_zone', 'graveyard') # Madness goes to exile first
                      madness_cost = None
                      if final_dest_zone == 'exile': # Check if it was Madness related
                            # If Madness applies, store cost for casting option
                            card_obj = game_state._safe_get_card(card_id)
                            if card_obj and "madness" in getattr(card_obj,'oracle_text','').lower():
                                 madness_cost = game_state.action_handler._get_madness_cost_str(card_obj)

                      # Perform the move
                      if game_state.move_card(card_id, p, "hand", p, final_dest_zone, cause="discard", context={"source_id": source_id}):
                          num_discarded_this_player += 1
                          # If madness cost found, set up trigger/choice state
                          if madness_cost:
                               if not hasattr(game_state, 'madness_trigger'): game_state.madness_trigger = None
                               game_state.madness_trigger = {'card_id': card_id, 'player': p, 'cost': madness_cost}
                               # Need a mechanism to let the player choose to cast it
                               # Possibly transition to a specific CHOICE subphase? Or just track state.
                               logging.debug(f"Card {card_id} discarded with Madness, moved to exile. Player can cast for {madness_cost}.")
                      else:
                          logging.warning(f"Failed to move {card_id} from hand to {final_dest_zone} during discard.")

             if num_discarded_this_player > 0:
                  # Track discards for triggers
                  if not hasattr(game_state, 'cards_discarded_this_turn'): game_state.cards_discarded_this_turn = {}
                  player_id = 'p1' if p == game_state.p1 else 'p2'
                  game_state.cards_discarded_this_turn[player_id] = game_state.cards_discarded_this_turn.get(player_id, 0) + num_discarded_this_player
                  logging.debug(f"DiscardEffect: Player {p['name']} discarded {num_discarded_this_player} card(s).")
                  overall_success = True


        return overall_success


class MillEffect(AbilityEffect):
    """Effect that mills cards from library to graveyard."""
    def __init__(self, count=1, target="opponent", condition=None):
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} mills {count} card(s)", condition)
        self.count = count
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_players = []
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", [])
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"MillEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([game_state.p1, game_state.p2])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             player_id_str = "p1" if p == game_state.p1 else "p2"
             if not p.get("library"):
                  logging.debug(f"MillEffect: Player {p['name']}'s library is empty.")
                  continue # Skip empty library

             num_to_mill = min(self.count, len(p["library"]))
             if num_to_mill <= 0: continue

             milled_ids = []
             # Get IDs first
             ids_to_mill = p["library"][:num_to_mill]

             # Perform moves
             actual_milled_count = 0
             for card_id in ids_to_mill:
                  # Use move_card to handle triggers (like shuffle from GY) and replacements
                  success_move = game_state.move_card(card_id, p, "library", p, "graveyard", cause="mill", context={"source_id": source_id})
                  if success_move:
                      actual_milled_count += 1
                  else:
                      # Card didn't move (e.g., Rest in Peace -> Exile)
                      # Or failed for another reason. Stop milling for this player? Or continue?
                      # Assume stop if move fails catastrophically, but usually replacements just change destination.
                      # Logging within move_card should indicate what happened.
                      pass

             logging.debug(f"MillEffect: Milled {actual_milled_count} card(s) from {p['name']}'s library.")
             overall_success &= (actual_milled_count > 0)

             # Track mill count
             if actual_milled_count > 0:
                  if not hasattr(game_state, 'cards_milled_this_turn'): game_state.cards_milled_this_turn = {}
                  player_id = 'p1' if p == game_state.p1 else 'p2'
                  game_state.cards_milled_this_turn[player_id] = game_state.cards_milled_this_turn.get(player_id, 0) + actual_milled_count
                  # Check empty library warning
                  if not p.get("library"): p["library_empty_warning"] = True

        return overall_success

class SearchLibraryEffect(AbilityEffect):
    """Effect that allows searching a library for cards."""
    def __init__(self, search_type="any", destination="hand", count=1, condition=None, shuffle_required=True):
        target_desc = "your library" # Assuming most searches target controller's library
        dest_desc = f"into {destination}" if destination != 'library' else "on top of your library" # Basic phrasing
        super().__init__(f"Search {target_desc} for {count} {search_type} card(s) and put {dest_desc}", condition)
        self.search_type = search_type
        self.destination = destination.lower()
        self.count = count
        self.shuffle_required = shuffle_required # Usually true unless effect says otherwise

    def _apply_effect(self, game_state, source_id, controller, targets):
        # Search usually targets controller's library unless specified otherwise
        player_to_search = controller
        if targets and "players" in targets and targets["players"]:
            player_id = targets["players"][0]
            player_to_search = game_state.p1 if player_id == "p1" else game_state.p2
        # Add logic here if effect text specifies searching opponent's library

        found_card_ids = []
        num_to_find = self.count
        search_attempts = 0
        max_search_attempts = self.count * 2 + 1 # Safety break for choosing

        while num_to_find > 0 and search_attempts < max_search_attempts:
            search_attempts += 1
            # Use GameState method which should incorporate AI choice/player interaction
            if hasattr(game_state, 'search_library_and_choose'):
                 ai_context = {"goal": self.search_type, "count_needed": num_to_find}
                 # Provide list of already found cards to avoid duplicates
                 found_id = game_state.search_library_and_choose(player_to_search, self.search_type, ai_choice_context=ai_context, exclude_ids=found_card_ids)
                 if found_id:
                      found_card_ids.append(found_id)
                      num_to_find -= 1
                 else: # No more valid cards found
                      break
            else: # Fallback if GS method missing
                 logging.warning("SearchLibraryEffect requires GameState.search_library_and_choose method.")
                 break

        # Move found cards to destination
        success_moves = 0
        if found_card_ids:
             for card_id in found_card_ids:
                  card = game_state._safe_get_card(card_id)
                  card_name = card.name if card else card_id
                  # Card is implicitly removed by search_library_and_choose, use library_implicit source
                  if game_state.move_card(card_id, player_to_search, "library_implicit", player_to_search, self.destination, cause="search_effect"):
                      success_moves += 1
                      logging.debug(f"Search found '{card_name}' matching '{self.search_type}', moved to {self.destination}.")
                  else:
                      logging.warning(f"Search found '{card_name}', but failed to move to {self.destination}.")
                      # Return to library?
                      player_to_search.setdefault("library",[]).append(card_id) # Add back to lib if move fails

             # Shuffle library if required (and if library was searched)
             if self.shuffle_required and search_attempts > 1 : # Avoid shuffle if only peeked at top and took it
                 game_state.shuffle_library(player_to_search)
        else: # Nothing found
            logging.debug(f"Search failed for '{self.search_type}' in {player_to_search['name']}'s library.")
            # Shuffle library even if search fails, if it was inspected
            if self.shuffle_required: game_state.shuffle_library(player_to_search)

        return success_moves > 0

class TapEffect(AbilityEffect):
    """Effect that taps a permanent."""
    def __init__(self, target_type="permanent", condition=None):
        super().__init__(f"Tap target {target_type}", condition)
        self.target_type = target_type.lower()
        self.requires_target = True


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        # Collect targets from relevant categories
        cats = ["creatures", "artifacts", "lands", "permanents"] # Add others if needed
        for cat in cats:
             target_ids.extend(targets.get(cat, []))
        if not target_ids:
            logging.warning(f"TapEffect failed: No targets provided/resolved in dict {targets}")
            return False

        tapped_count = 0
        for target_id in set(target_ids): # Process unique targets
             target_owner, target_zone = game_state.find_card_location(target_id)
             if not target_owner or target_zone != "battlefield":
                  logging.debug(f"TapEffect: Target {target_id} not valid for tapping.")
                  continue
             # Filter by type if necessary (e.g., "Tap target creature")
             if self.target_type != "permanent":
                 card = game_state._safe_get_card(target_id)
                 if not card or self.target_type not in getattr(card,'card_types',[]) : continue # Skip if type mismatch

             if game_state.tap_permanent(target_id, target_owner):
                  tapped_count += 1
                  # Logging inside tap_permanent

        return tapped_count > 0

class UntapEffect(AbilityEffect):
    """Effect that untaps a permanent."""
    def __init__(self, target_type="permanent", condition=None):
        super().__init__(f"Untap target {target_type}", condition)
        self.target_type = target_type.lower()
        self.requires_target = True


    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = []
        cats = ["creatures", "artifacts", "lands", "permanents"]
        for cat in cats:
             target_ids.extend(targets.get(cat, []))
        if not target_ids:
            logging.warning(f"UntapEffect failed: No targets provided/resolved in dict {targets}")
            return False

        untapped_count = 0
        for target_id in set(target_ids): # Process unique targets
             target_owner, target_zone = game_state.find_card_location(target_id)
             if not target_owner or target_zone != "battlefield":
                  logging.debug(f"UntapEffect: Target {target_id} not valid for untapping.")
                  continue
             # Filter by type if necessary
             if self.target_type != "permanent":
                 card = game_state._safe_get_card(target_id)
                 if not card or self.target_type not in getattr(card,'card_types',[]): continue # Skip if type mismatch

             if game_state.untap_permanent(target_id, target_owner):
                  untapped_count += 1
                  # Logging inside untap_permanent

        return untapped_count > 0

# Remaining Effects - Assume placeholders for now or need specific implementation if used:
class ScryEffect(AbilityEffect):
    def _apply_effect(self, game_state, source_id, controller, targets):
        # TODO: Implement Scry logic (requires game state context setup)
        logging.warning(f"ScryEffect._apply_effect not fully implemented.")
        # Basic simulation: Peek at cards
        count = getattr(self, 'count', 1)
        top_cards = controller.get("library", [])[:count]
        if top_cards: logging.debug(f"Simulated Scry: Looked at {len(top_cards)} card(s).")
        # Need to trigger choice phase
        return True # Assume effect applied for simulation

class LifeDrainEffect(AbilityEffect):
    def _apply_effect(self, game_state, source_id, controller, targets):
        opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
        amount = getattr(self, 'amount', 1)
        # Apply damage/life loss using GameState methods for replacements/triggers
        damage_dealt = game_state.damage_player(opponent, amount, source_id)
        life_gained = game_state.gain_life(controller, amount, source_id)
        return damage_dealt > 0 or life_gained > 0

class CopySpellEffect(AbilityEffect):
    def _apply_effect(self, game_state, source_id, controller, targets):
        # TODO: Implement copy spell logic
        logging.warning(f"CopySpellEffect._apply_effect not fully implemented.")
        # Basic: Find target spell on stack, add copy with context
        target_ids = targets.get("spells", [])
        if not target_ids: return False
        target_id = target_ids[0] # Assume first target
        for i, item in enumerate(game_state.stack):
             if isinstance(item,tuple) and item[0]=="SPELL" and item[1]==target_id:
                  item_type, spell_id, original_controller, context = item
                  new_context = context.copy()
                  new_context["is_copy"] = True
                  new_context["copied_by"] = source_id
                  new_context["needs_new_targets"] = True # Copy usually needs new targets
                  game_state.add_to_stack("SPELL", spell_id, controller, new_context) # Copy controlled by effect controller
                  return True
        return False

class TransformEffect(AbilityEffect):
    def _apply_effect(self, game_state, source_id, controller, targets):
        # Effect targets the source card itself
        target_ids = targets.get("self", [source_id]) # Default to source
        if not target_ids: target_ids = [source_id]
        target_id = target_ids[0]
        target_owner = game_state.get_card_controller(target_id)
        if not target_owner: return False # Check owner exists
        card = game_state._safe_get_card(target_id)
        if card and hasattr(card, 'transform') and card.can_transform(game_state):
            card.transform()
            return True
        return False

class FightEffect(AbilityEffect):
    def _apply_effect(self, game_state, source_id, controller, targets):
        # Assumes source_id is one fighter, targets["creatures"][0] is the other
        if not targets or "creatures" not in targets or not targets["creatures"]: return False
        target_id = targets["creatures"][0]

        source_card = game_state._safe_get_card(source_id)
        target_card = game_state._safe_get_card(target_id)
        if not source_card or 'creature' not in getattr(source_card, 'card_types', []) or \
           not target_card or 'creature' not in getattr(target_card, 'card_types', []):
            return False

        source_power = getattr(source_card, 'power', 0)
        target_power = getattr(target_card, 'power', 0)
        # Apply damage simultaneously using GameState methods
        if source_power > 0:
             game_state.apply_damage_to_permanent(target_id, source_power, source_id, is_combat_damage=False)
        if target_power > 0:
             game_state.apply_damage_to_permanent(source_id, target_power, target_id, is_combat_damage=False)
        # SBAs checked in main loop
        return True