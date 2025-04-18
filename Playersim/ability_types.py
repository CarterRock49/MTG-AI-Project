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
        """Resolve the ability's effect with improved error handling and target validation."""
        card = game_state._safe_get_card(self.card_id)
        if not card:
            logging.warning(f"Cannot resolve ability: card {self.card_id} not found")
            return False

        try:
            # Check if ability requires targeting based on its effect text
            text_to_check = getattr(self, 'effect', getattr(self, 'effect_text', ''))
            requires_target = "target" in text_to_check.lower() # Basic check
            targets_resolved = {} # Targets resolved for this instance

            if requires_target:
                targets_resolved = self._handle_targeting(game_state, controller)
                # Validate targets just before resolution (they might have become invalid)
                # Targets should be in a structured dict {cat:[id,...]} by now if resolved properly
                if not game_state._validate_targets_on_resolution(self.card_id, controller, targets_resolved):
                    logging.info(f"Ability {self.effect_text} fizzled: Targets became invalid before resolution.")
                    return False # Fizzle (counts as resolved successfully technically)

            # Delegate to specific implementation, passing resolved targets
            return self._resolve_ability_implementation(game_state, controller, targets_resolved)

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
        """Ability-specific implementation of resolution. Uses EffectFactory and handles sequences."""
        effect_text_to_use = getattr(self, 'effect', getattr(self, 'effect_text', None))
        if not effect_text_to_use:
            logging.error(f"Cannot resolve triggered ability implementation for {self.card_id}: Missing effect text.")
            return False

        # Special handling for specific sequenced keywords like Living Weapon
        if getattr(self, 'keyword', None) == 'living weapon':
            # Sequence: Create Germ, then Attach
            logging.debug(f"Resolving Living Weapon for {self.card_id}")
            # 1. Create Germ Token
            germ_token_data = {"name": "Phyrexian Germ", "power": 0, "toughness": 0, "card_types":["creature"], "subtypes":["Phyrexian", "Germ"], "colors":[0,0,1,0,0]} # Black
            created_token_id = None
            if hasattr(game_state, 'create_token'):
                created_token_id = game_state.create_token(controller, germ_token_data)
            else: # Fallback
                token_id = f"TOKEN_Germ_{random.randint(1000,9999)}"
                germ_token_data['is_token'] = True
                new_token = Card(germ_token_data)
                new_token.card_id = token_id
                game_state.card_db[token_id] = new_token
                controller.setdefault("tokens",[]).append(token_id)
                controller["battlefield"].append(token_id)
                created_token_id = token_id
                game_state.trigger_ability(created_token_id, "ENTERS_BATTLEFIELD", {"controller": controller})

            # 2. Attach Equipment (self.card_id) to the token
            if created_token_id:
                if hasattr(game_state, 'equip_permanent'):
                     # No cost associated with Living Weapon attachment
                     if game_state.equip_permanent(controller, self.card_id, created_token_id, bypass_cost=True):
                          logging.debug(f"Living Weapon: Attached {self.card_id} to Germ token {created_token_id}.")
                          return True
                     else:
                          logging.warning(f"Living Weapon: Failed to attach {self.card_id} to Germ token {created_token_id}.")
                          return False # Attachment failed
                else:
                     logging.warning("Living Weapon: GameState missing 'equip_permanent' method.")
                     return False
            else:
                logging.warning(f"Living Weapon: Failed to create Germ token for {self.card_id}.")
                return False # Token creation failed

        # Default: Use EffectFactory for other triggers
        effects = self._create_ability_effects(effect_text_to_use, targets)
        if not effects:
            logging.warning(f"No effects created for triggered ability: {effect_text_to_use}")
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
    def __init__(self, card_id, cost=None, effect=None, effect_text="", is_exhaust=False, activation_index=None):
        # Ensure base __init__ gets original effect_text if available
        super().__init__(card_id, effect_text or f"{cost or ''}: {effect or ''}".strip(': '))

        # Determine cost and effect using parsing if not explicitly provided
        parsed_cost, parsed_effect = None, None
        if cost is None and effect is None and self.effect_text:
            parsed_cost, parsed_effect = self._parse_cost_effect(self.effect_text) # Parse *before* using is_exhaust flag directly

        # Prioritize provided args, then parsed values, then empty string
        self.cost = str(cost) if cost is not None else (str(parsed_cost) if parsed_cost is not None else "")
        self.effect = str(effect) if effect is not None else (str(parsed_effect) if parsed_effect is not None else "")

        # --- Exhaust Handling: If cost starts with 'Exhaust', set flag and strip ---
        # This assumes Exhaust marker is correctly included in the *parsed* cost by _parse_cost_effect
        self.is_exhaust = is_exhaust # Initialize with passed flag
        if self.cost.lower().startswith("exhaust"):
            self.is_exhaust = True
            # Remove "Exhaust" and separator (comma or dash)
            self.cost = re.sub(r"^\s*Exhaust\s*[,—\u2014-]?\s*", "", self.cost, flags=re.IGNORECASE).strip()

        self.activation_index = activation_index

        # --- Validation Refinement ---
        # Validate cost AFTER potential Exhaust stripping
        # Cost usually required, unless implicit (handled by caller/handler) or keyword ability parsed this way
        implicit_cost_keywords = ["morph", "manifest", "foretell", "channel", "transmute", "madness", "flashback", "cycling", "unearth", "retrace", "ninjutsu"] # Add more known implicit costs
        if not self.cost:
             if not any(kw in (self.effect_text or "").lower() for kw in implicit_cost_keywords):
                 # Only log if cost is missing AND it doesn't seem like an implicit keyword ability
                 # Check if original text HAD a separator - suggests parsing *should* have worked
                 if parsed_cost is None and (":" in self.effect_text or "—" in self.effect_text or "\u2014" in self.effect_text):
                     logging.debug(f"ActivatedAbility created for '{self.effect_text}', but cost parsing failed or yielded empty despite separator. Ability may be invalid.")
                 # Else: Silently allow empty cost, maybe it's intentional or handled differently

        # Update effect_text if needed based on final cost/effect/exhaust
        prefix = "Exhaust, " if self.is_exhaust else ""
        reconstructed_text = f"{prefix}{self.cost}: {self.effect}".strip(': ')
        if reconstructed_text != self.effect_text and self.cost and self.effect: # Only update if parts found and different
            self.effect_text = reconstructed_text


    @staticmethod
    def _parse_cost_effect(text):
        """
        Revised parser for Activated Abilities. Prioritizes explicit separators.
        Less strict cost validation after separator found. Includes Exhaust in cost part.
        """
        if not text: return None, None
        text = text.strip()

        # --- 1. Prioritize Explicit Separators (Colon or Dash) ---
        # Regex includes colon, en dash, em dash, unicode em dash, surrounded by optional spaces
        separator_match = re.match(r'^\s*(.+?)\s*[:—\u2014-]\s*(.+)\s*$', text, re.DOTALL)

        if separator_match:
            cost_part = separator_match.group(1).strip()
            effect_part = separator_match.group(2).strip().rstrip('.')

            # --- 2. Flexible Cost Validation ---
            # Does the cost part contain ANY plausible cost indicator?
            # (Mana symbols {}, tap {T}/Tap, cost keywords, sacrifice/discard/pay life/remove counter, or Exhaust)
            cost_indicators_pattern = r'\{[WUBRGCXSPMTQ0-9\/.]+\}|\(\{T\}\)|\b(Tap|Sacrifice|Discard|Pay\s+\d+\s+life|Remove\s+.*?\s+counter|Exhaust|Cycling|Equip|Flashback|Level\s+up)\b|^\s*\d+\s*$'
            # --- Adjusted: check raw cost_part, don't use cost_lower ---
            if re.search(cost_indicators_pattern, cost_part, re.IGNORECASE):
                logging.debug(f"Parsed Separator Cost/Effect: Cost='{cost_part}', Effect='{effect_part}'")
                return cost_part, effect_part
            else:
                # Found a separator, but the part before it doesn't look like a typical cost.
                # Could be part of the effect (e.g., "Target creature gets +X/+0 until end of turn, where X is...")
                # Or could be a less common cost type not captured by the regex.
                # Treat as likely effect text to be safer than misinterpreting cost.
                logging.debug(f"Found separator in '{text}', but left side '{cost_part}' not recognized as standard cost. Treating whole as effect.")
                # Fall through to check keyword-only patterns

        # --- 3. Check for Keyword-Only structures where Effect is Implicit ---
        # Example: "Cycling {2}" or "Equip {1}" or "Flashback {B}" without explicit effect text following
        keyword_cost_patterns = {
            # Regex matches Keyword + Cost pattern at the START and consumes the WHOLE string (or ends near punctuation)
            r"^\s*(Cycling|Equip|Fortify|Reconfigure|Unearth|Flashback|Bestow|Dash|Buyback|Madness|Transmute|Channel|Kicker|Entwine|Overload|Splice|Surge|Embalm|Eternalize|Jump-start|Escape|Awaken|Level up|Retrace|Ninjutsu)\s*(\{.+?\}|\d+|pay\s+\d+\s+life|discard\s+\S+\s+card|sacrifice\s+\S+\s+\S+|exile\s+\S+\s+\S+)\s*[.]?$": "keyword_with_explicit_cost",
             r"^(Morph)\s*(?:\{(\d+|[Xx])\})?\s*[.]?$": "keyword_with_optional_cost", # Morph cost optional here
             r"^(Outlast|Monstrosity|Adapt|Reinforce|Scavenge|Crew)\s*(\{\d+\}|\d+)\s*[.]?$": "keyword_with_numeric_value" # Value is intrinsic part
        }
        for pattern, pattern_type in keyword_cost_patterns.items():
            match = re.match(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                 keyword = match.group(1)
                 # Reconstruct a plausible cost string from the matched group
                 cost_str = match.group(2).strip() if len(match.groups()) > 1 and match.group(2) else "{0}" # Default {0} if no cost group
                 if cost_str.isdigit(): cost_str = f"{{{cost_str}}}" # Normalize

                 # Generate an implicit effect description
                 effect_map = {
                     "cycling": "Draw a card.", "equip": "Attach to target creature.",
                     "flashback": "Cast from graveyard, then exile.", "level up": "Put a level counter on this.",
                     # Add more standard effects...
                 }
                 effect_part = effect_map.get(keyword.lower(), f"Activate {keyword} ability.")
                 logging.debug(f"Parsed Implicit Keyword Effect: Keyword='{keyword}', Cost='{cost_str}', ImplicitEffect='{effect_part}'")
                 # Return the parsed COST and the generated implicit EFFECT
                 return cost_str, effect_part

        # --- 4. Fallback ---
        logging.debug(f"Could not parse activated ability structure from '{text}'. Assuming static/triggered or malformed.")
        return None, text # Return None for cost, full text as effect

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


    def pay_cost(self, game_state, controller):
        """Pay the activation cost of this ability with comprehensive cost handling."""
        # Use self.cost (which has Exhaust prefix removed if applicable by __init__)
        cost_text = self.cost
        cost_lower = cost_text.lower() if cost_text else ""
        all_costs_paid = True
        rollback_steps = []

        # --- Handle is_exhaust flag (set during init) ---
        if self.is_exhaust:
            activation_idx = getattr(self, 'activation_index', -1)
            if activation_idx == -1:
                 logging.error(f"Exhaust ability on {self.card_id} missing activation_index. Cannot pay cost.")
                 return False
            if game_state.check_exhaust_used(self.card_id, activation_idx):
                 logging.debug(f"Cannot pay cost: Exhaust ability {activation_idx} for {self.card_id} already used this turn.")
                 return False
            # Exhaust itself isn't 'paid' here, but marked later IF other costs succeed

        # --- Non-Mana Costs FIRST (Logic mostly unchanged, uses cost_lower) ---
        # Tap Cost ({T} or tap)
        if "{t}" in cost_lower or re.search(r'\btap\b', cost_lower): # Added word boundary tap check
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             if self.card_id in controller.get("tapped_permanents", set()):
                 logging.debug(f"Cannot pay tap cost: {card_name} already tapped.")
                 return False # Cannot pay cost if already tapped
             # --- ADDED: Check if card can be tapped (e.g., not summoning sick if tapping requires it as an action) ---
             # Rule 302.6: A creature's activated ability with the tap symbol in its cost can't be activated unless the creature has been under its controller's control continuously since their most recent turn began. Ignore this restriction if the creature has haste.
             # Check if card is creature AND lacks haste AND entered this turn
             card_obj = game_state._safe_get_card(self.card_id)
             is_creature = card_obj and 'creature' in getattr(card_obj,'card_types',[])
             entered_this_turn = card_obj and self.card_id in controller.get("entered_battlefield_this_turn",set())
             has_haste = game_state.check_keyword(self.card_id, 'haste') # Use central check

             if is_creature and entered_this_turn and not has_haste:
                  logging.debug(f"Cannot pay tap cost: {card_name} has summoning sickness.")
                  return False # Summoning sickness prevents tapping for cost
             # --- END ADDED CHECK ---
             if not game_state.tap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay tap cost: {card_name} couldn't be tapped (e.g., 'can't be tapped' effect).")
                 self._perform_rollback(game_state, controller, rollback_steps)
                 return False
             rollback_steps.append(("untap", self.card_id))
             logging.debug(f"Paid tap cost for {card_name}")

        # ... (Untap, Sacrifice, Discard, Pay Life, Remove Counters logic remains the same, ensure they use cost_lower correctly) ...
        # Untap Cost ({Q} or untap) - Use cost_lower for regex
        if "{q}" in cost_lower or re.search(r'\buntap\b', cost_lower):
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             if self.card_id not in controller.get("tapped_permanents", set()):
                 logging.debug(f"Cannot pay untap cost: {card_name} already untapped.")
                 return False
             if not game_state.untap_permanent(self.card_id, controller):
                 logging.debug(f"Cannot pay untap cost for {card_name} (e.g., 'doesn't untap' effect)")
                 self._perform_rollback(game_state, controller, rollback_steps)
                 return False
             rollback_steps.append(("tap", self.card_id))
             logging.debug(f"Paid untap cost for {card_name}")

        # Sacrifice Cost - Use cost_lower for regex
        sac_match = re.search(r"sacrifice\s+((?:a|an|another|one|two|three|\d+)\s+)?(.*?)(?:$|,?\s*\{)", cost_lower)
        if sac_match:
             sac_req = sac_match.group(0).replace("sacrifice ", "").strip()
             count_str = sac_match.group(1)
             count = 1
             if count_str: count = self._word_to_number(count_str.strip())
             for i in range(count):
                  sacrifice_paid, _ = self._pay_sacrifice_cost_with_rollback(game_state, controller, sac_req, self.card_id, rollback_steps)
                  if not sacrifice_paid:
                      logging.debug(f"Failed sacrifice cost: Required {count}, attempt {i+1} failed for '{sac_req}'.")
                      self._perform_rollback(game_state, controller, rollback_steps); return False
             logging.debug(f"Paid sacrifice cost ({count}x '{sac_req}').")

        # Discard Cost - Use cost_lower for regex
        discard_match = re.search(r"discard\s+((?:a|an|one|two|three|\d+)\s+)?(\w+\s+)?cards?(?:\s+at random)?(?:$|,?\s*\{)", cost_lower)
        if discard_match:
             count_str = discard_match.group(1)
             count = 1 if not count_str else self._word_to_number(count_str.strip())
             is_random = "at random" in cost_lower
             discard_paid, _ = self._pay_discard_cost_with_rollback(game_state, controller, count, is_random, rollback_steps)
             if not discard_paid: self._perform_rollback(game_state, controller, rollback_steps); return False
             logging.debug(f"Paid discard cost ({count} cards{' randomly' if is_random else ''}).")

        # Pay Life Cost - Use cost_lower for regex
        life_match = re.search(r"pay\s+(\d+)\s+life", cost_lower)
        if life_match:
             amount = int(life_match.group(1))
             if controller["life"] < amount: # Rule: Can pay life even if it brings you to 0 or less. Only prevent if already 0 or less? Re-check 119.4. Need > 0 to pay.
                  # Corrected: Cannot pay if life is less than cost, unless an effect allows paying with life you don't have (very rare). Rule 119.4
                  logging.debug(f"Cannot pay life cost {amount}: Only have {controller['life']} life.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             controller["life"] -= amount
             rollback_steps.append(("gain_life", amount))
             logging.debug(f"Paid {amount} life. Life is now {controller['life']}")
             # Trigger life loss event
             if hasattr(game_state, 'trigger_ability'):
                 game_state.trigger_ability(self.card_id, "LOSE_LIFE", {"player": controller, "amount": amount, "cause": "cost"})


        # Remove Counters Cost - Use cost_lower for regex
        counter_match = re.search(r"remove\s+(?:(?:a|an|one|two|three|\d+)\s+)?(\w+|[+\-]\d+/[+\-]\d+)\s+counters?(?: from.*?)?(?:$|,?\s*\{)", cost_lower)
        if counter_match:
            count_word_match = re.search(r"remove\s+(a|an|one|two|three|\d+)", cost_lower)
            count = 1
            if count_word_match: count = self._word_to_number(count_word_match.group(1))
            counter_type = counter_match.group(1)
            if '/' in counter_type and counter_type.replace('/','').replace('+','').replace('-','').isdigit(): pass
            else: counter_type = counter_type.upper()
            # Check source of counters (default: self, but could be target)
            counter_source_id = self.card_id # Assume self unless "from TARGET" specified
            from_match = re.search(r"from (.*?)($|,?\s*\{)", cost_lower)
            if from_match: # Needs to resolve target specification based on context
                logging.warning(f"Parsing 'remove counter from TARGET' cost is complex and not fully supported.")
                # For now, assume source is self if 'from' part exists but isn't parsed
                # Better implementation would need target context here.

            source_card = game_state._safe_get_card(counter_source_id)
            current_counter_count = 0
            if counter_source_id == self.card_id and source_card and hasattr(source_card, 'counters'): # Check self
                 current_counter_count = source_card.counters.get(counter_type, 0)
            # Check other permanents/players if source differs and is implemented

            if current_counter_count < count:
                logging.debug(f"Cannot remove {count} {counter_type}: Only {current_counter_count} available on {counter_source_id}.")
                self._perform_rollback(game_state, controller, rollback_steps); return False
            if not game_state.add_counter(counter_source_id, counter_type, -count): # Use add_counter for removal
                logging.warning(f"Failed to remove {count} {counter_type} counters from {counter_source_id}.")
                self._perform_rollback(game_state, controller, rollback_steps); return False
            rollback_steps.append(("add_counter", counter_source_id, counter_type, count))
            logging.debug(f"Paid by removing {count} {counter_type} counters from {counter_source_id}.")


        # --- Mana Costs LAST (uses cost_text for parsing) ---
        mana_cost_paid = True
        paid_mana_details = None
        # Regex for standard mana symbols {.}
        mana_symbols = re.findall(r'\{([WUBRGCXSPMTQA0-9\/\.]+)\}', cost_text)
        if mana_symbols:
            if hasattr(game_state, 'mana_system') and game_state.mana_system:
                mana_cost_str = "".join(f"{{{s}}}" for s in mana_symbols) # Reconstruct from found symbols only
                if mana_cost_str: # Ensure non-empty mana cost string
                    parsed_cost = game_state.mana_system.parse_mana_cost(mana_cost_str)
                    can_pay_mana = game_state.mana_system.can_pay_mana_cost(controller, parsed_cost)
                    if can_pay_mana:
                        # --- PAY MANA ---
                        paid_mana_details = game_state.mana_system.pay_mana_cost_get_details(controller, parsed_cost)
                        if paid_mana_details:
                            mana_cost_paid = True
                            rollback_steps.append(("refund_mana", paid_mana_details))
                            logging.debug(f"Paid mana cost: {mana_cost_str}")
                        else: # Payment failed internally
                            mana_cost_paid = False
                            logging.warning(f"Failed to pay mana cost '{mana_cost_str}' (pay_mana_cost_get_details returned None).")
                    else: # Cannot afford
                        mana_cost_paid = False
                        logging.debug(f"Cannot afford mana cost '{mana_cost_str}'.")
            else: # No mana system fallback
                logging.warning("Mana system not found, cannot handle mana costs properly.")
                mana_cost_paid = False # Cannot pay mana without system

            if not mana_cost_paid:
                logging.error(f"Rolling back costs due to failed mana payment for '{self.cost}'.")
                self._perform_rollback(game_state, controller, rollback_steps)
                return False

        # --- Mark Exhaust AFTER other costs paid ---
        if self.is_exhaust:
             activation_idx = getattr(self, 'activation_index', -1)
             if activation_idx == -1: # Should have index if is_exhaust was set
                  logging.error("CRITICAL: Exhaust activation_index lost during cost payment.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             if not game_state.mark_exhaust_used(self.card_id, activation_idx):
                  logging.error(f"Failed to mark Exhaust used for {self.card_id} index {activation_idx} AFTER paying costs.")
                  self._perform_rollback(game_state, controller, rollback_steps)
                  return False
             rollback_steps.append(("clear_exhaust", self.card_id, activation_idx))
             logging.debug(f"Marked Exhaust as used for {self.card_id} ability {activation_idx}.")

        # --- Final Check ---
        if all_costs_paid and mana_cost_paid:
             card_name = getattr(game_state._safe_get_card(self.card_id), 'name', self.card_id)
             logging.debug(f"Successfully paid cost '{self.effect_text}' for {card_name}")
             return True
        else:
             # This path shouldn't be reached due to early returns on failure
             logging.error(f"Cost payment check reached end incorrectly for '{self.cost}'. Rolling back.")
             self._perform_rollback(game_state, controller, rollback_steps)
             return False
         
    def _pay_discard_cost_with_rollback(self, game_state, controller, count, rollback_steps):
        """Helper to handle discard cost payment and potential rollback."""
        # Logic assumes discarding first N cards. Need choice logic if not random.
        discarded_ids = []
        if len(controller["hand"]) < count: return False, None # Should be checked before calling

        hand_copy = controller["hand"][:] # Work on copy
        successfully_discarded = []
        failed_to_discard = False

        for _ in range(count):
            if hand_copy:
                discard_id = hand_copy.pop(0) # Take from front of copy
                # Perform discard via move_card
                if game_state.move_card(discard_id, controller, "hand", controller, "graveyard", cause="ability_cost"):
                    successfully_discarded.append(discard_id)
                else:
                    # If move failed, abort cost payment immediately
                    failed_to_discard = True
                    break
            else: # Should not happen if initial check passed
                 failed_to_discard = True
                 break

        if failed_to_discard:
             # Add rollback steps for successfully discarded cards *before* the failure
             for success_id in successfully_discarded:
                  rollback_steps.append(("return_from_graveyard_to_hand", success_id)) # Specific return
             return False, None
        else:
             # Add rollback steps for all successfully discarded cards
             for success_id in successfully_discarded:
                 rollback_steps.append(("return_from_graveyard_to_hand", success_id))
             logging.debug(f"Paid discard cost ({len(successfully_discarded)} cards).")
             return True, successfully_discarded

    def _perform_rollback(self, game_state, controller, rollback_steps):
        """Performs rollback steps in reverse order."""
        logging.warning(f"Performing cost payment rollback: {rollback_steps}")
        for step in reversed(rollback_steps):
            action = step[0]
            try:
                if action == "untap": game_state.untap_permanent(step[1], controller)
                elif action == "tap": game_state.tap_permanent(step[1], controller)
                elif action == "return_from_graveyard": game_state.move_card(step[1], controller, "graveyard", controller, "battlefield")
                elif action == "return_from_graveyard_to_hand": game_state.move_card(step[1], controller, "graveyard", controller, "hand")
                elif action == "gain_life": controller["life"] += step[1]
                elif action == "add_counter": game_state.add_counter(step[1], step[2], step[3])
                elif action == "refund_mana": game_state.mana_system.add_mana(controller, step[1]) # Assumes add_mana handles refunding specific details
                elif action == "restore_mana_pool": controller["mana_pool"] = step[1] # Basic fallback
            except Exception as e:
                logging.error(f"Error during rollback step {step}: {e}")
    
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
            parsed_condition, parsed_effect = self._parse_condition_effect(effect_text) # Uses updated parser

        # Ensure attributes are strings, default to empty if None
        self.trigger_condition = str(trigger_condition) if trigger_condition is not None else (str(parsed_condition) if parsed_condition is not None else "Unknown")
        self.effect = str(effect) if effect is not None else (str(parsed_effect) if parsed_effect is not None else "Unknown")
        self.trigger_condition = self.trigger_condition.lower() # Store lower
        self.effect = self.effect.lower() # Store lower
        self.additional_condition = additional_condition # Can be string or callable

        # Validation after potential parsing
        if self.trigger_condition == "unknown":
             raise ValueError(f"TriggeredAbility requires trigger_condition. Got text='{effect_text}'")
        if self.effect == "unknown":
             raise ValueError(f"TriggeredAbility requires effect. Got text='{effect_text}'")

        # Store original text if not provided
        if not effect_text:
            self.effect_text = f"{self.trigger_condition.capitalize()}, {self.effect.capitalize()}." # Reconstruct from parts

    def _parse_condition_effect(self, text):
        """Attempt to parse 'When/Whenever/At..., Effect.' or 'When/Whenever/At... — Effect' format. Handles em dash."""
        # Regex includes comma, colon, en dash, em dash, unicode em dash as separators
        match = re.match(r'^\s*(when|whenever|at)\s+([^,:—\u2014]+?),?[:—\u2014]?\s*(.+)\s*$', text.strip(), re.IGNORECASE | re.DOTALL)
        if match:
            # Combine trigger parts
            trigger_part = f"{match.group(1)} {match.group(2)}".strip()
            effect_part = match.group(3).strip()
            # Remove trailing period if present
            if effect_part.endswith('.'): effect_part = effect_part[:-1]
            # Simple validation: effect shouldn't contain trigger keywords unless nested
            if not re.match(r'^(when|whenever|at)\b', effect_part.lower()):
                 return trigger_part, effect_part
            else: # Effect seems to contain another trigger keyword, parse likely failed
                 logging.debug(f"Possible nested trigger in effect part: '{effect_part}'. Parse might be inaccurate.")
                 # Return best guess
                 return trigger_part, effect_part
        logging.warning(f"Could not parse Trigger[?,:,\u2014] Effect from '{text}'")
        return None, None # Return None if parse fails
        
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


    def _evaluate_condition(self, condition_text, context):
        """Evaluate if a trigger's conditional clause is met. (Expanded Fallback)"""
        if not condition_text or not context: return True
        gs = context.get('game_state')
        trigger_controller = context.get('controller') # Controller of the trigger source
        source_card = context.get('source_card') # Card with the trigger
        if not gs or not trigger_controller or not source_card: return True

        # Use the card evaluator for condition checking if available
        if hasattr(gs, 'card_evaluator') and gs.card_evaluator and hasattr(gs.card_evaluator, 'evaluate_condition'):
            try:
                # Pass context and condition text
                return gs.card_evaluator.evaluate_condition(condition_text, context)
            except NotImplementedError:
                logging.warning(f"CardEvaluator does not implement condition: {condition_text}")
            except Exception as e:
                logging.error(f"Error evaluating condition via CardEvaluator: {e}")

        # --- Basic Fallback Parsing ---
        logging.debug(f"Evaluating basic trigger condition: '{condition_text}'")
        condition_lower = condition_text.lower()
        opponent = gs.p2 if trigger_controller == gs.p1 else gs.p1

        # Check "if you control..."
        control_match = re.search(r"if\s+(you control|an opponent controls)\s+(?:a|an|another|at least|exactly|\d+)?\s*([\w\s\-]+?)(?: with|$|,|\.|$)", condition_lower)
        if control_match:
            who_controls, required_type = control_match.group(1), control_match.group(2)
            player_to_check = trigger_controller if who_controls == "you control" else opponent
            required_type = required_type.strip()
            return any(self._card_matches_criteria(gs._safe_get_card(cid), required_type)
                       for cid in player_to_check.get("battlefield", []))

        # Check life total comparison
        life_match = re.search(r"if\s+(your life total is|you have)\s+(?:at least|exactly|less than|more than|\d+)\s+(\d+)", condition_lower)
        comparison_match = re.search(r"(at least|exactly|less than|more than)", condition_lower)
        if life_match and comparison_match:
            threshold = int(life_match.group(2))
            comparison = comparison_match.group(1)
            current_life = trigger_controller.get("life", 0)
            if comparison == "at least" or comparison == "more than": return current_life >= threshold
            if comparison == "less than": return current_life < threshold
            if comparison == "exactly": return current_life == threshold

        # Check card count in hand/graveyard
        card_count_match = re.search(r"if\s+you have\s+(?:at least|exactly|less than|more than|\d+)\s+(\d+)\s+cards?\s+in\s+(?:your|an opponent's)\s+(hand|graveyard)", condition_lower)
        if card_count_match:
            threshold = int(card_count_match.group(1))
            zone_name = card_count_match.group(2)
            player_to_check = trigger_controller # Assume 'your' for now
            # TODO: Add logic for "opponent's" hand/graveyard check
            comparison_match = re.search(r"(at least|exactly|less than|more than)", condition_lower)
            comparison = comparison_match.group(1) if comparison_match else "at least" # Default
            count_in_zone = len(player_to_check.get(zone_name, []))
            if comparison == "at least" or comparison == "more than": return count_in_zone >= threshold
            if comparison == "less than": return count_in_zone < threshold
            if comparison == "exactly": return count_in_zone == threshold

        # Check number of permanents controlled
        permanent_count_match = re.search(r"if\s+(you control|an opponent controls)\s+(?:at least|exactly|less than|more than|\d+)\s+(\d+)\s+(creatures?|artifacts?|lands?|permanents?)", condition_lower)
        if permanent_count_match:
             who_controls, threshold_str, type_to_count = permanent_count_match.groups()
             threshold = int(threshold_str)
             player_to_check = trigger_controller if who_controls == "you control" else opponent
             type_to_count = type_to_count.replace('s','') # Singularize
             current_count = sum(1 for cid in player_to_check.get("battlefield", []) if self._card_matches_criteria(gs._safe_get_card(cid), type_to_count))
             comparison_match = re.search(r"(at least|exactly|less than|more than)", condition_lower)
             comparison = comparison_match.group(1) if comparison_match else "at least" # Default
             if comparison == "at least" or comparison == "more than": return current_count >= threshold
             if comparison == "less than": return current_count < threshold
             if comparison == "exactly": return current_count == threshold

        logging.warning(f"Could not parse trigger condition: '{condition_text}'. Assuming True.")
        return True # Default to true if condition unparsed
             

    def _card_matches_criteria(self, card, criteria):
         """Basic check if card matches simple criteria. (Helper)"""
         if not card: return False
         types = getattr(card, 'card_types', [])
         subtypes = getattr(card, 'subtypes', [])
         type_line = getattr(card, 'type_line', '').lower()
         name = getattr(card, 'name', '').lower()

         if criteria == "any": return True
         if criteria == "basic land" and 'basic' in type_line and 'land' in type_line: return True
         if criteria == "land" and 'land' in type_line: return True
         if criteria in types: return True
         if criteria in subtypes: return True
         if criteria == name: return True
         # Add checks for colors, CMC, P/T if needed for more complex searches
         return False

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
        """Register the static ability's effect with the LayerSystem. Validates controller."""
        gs = game_state # Alias

        # --- 1. Check Controller and Zone ---
        # Static abilities only function while the source is on the battlefield typically
        card_owner, card_zone = gs.find_card_location(self.card_id)
        if not card_owner or card_zone != 'battlefield':
            # This static ability source isn't on the battlefield, do not apply.
            # Log if expected to be applied but isn't found.
            # logging.debug(f"StaticAbility source {self.card_id} not on battlefield (Zone: {card_zone}). Skipping apply.")
            return False # Signal that application was skipped/failed due to zone

        # Use the determined owner as the controller for this ability instance
        controller = card_owner

        # --- 2. Pre-validation: Check if effect looks like non-static ---
        # Prevent attempting to register activated/triggered abilities as static layers
        non_static_pattern = r'^\s*(\{.*?\}|tap|sacrifice|pay\s\d+\slife|discard|remove.*?counter)\s*[:—\u2014]' # Matches Cost: Effect
        trigger_pattern = r'^\s*(when|whenever|at)\b'
        # Also check for explicit action verbs less common in static abilities applied continuously
        action_verbs = r'\b(destroy|exile|counter|return target|deals? damage|create token|search|target player draws?|target player loses?)\b'

        if re.match(non_static_pattern, self.effect) or re.match(trigger_pattern, self.effect) or re.search(action_verbs, self.effect):
             # Log only if it wasn't caught by the parser earlier (this is a double-check)
             # logging.warning(f"StaticAbility.apply skipped: Effect text '{self.effect_text}' resembles activated/triggered/action ability.")
             return False # Do not register this with LayerSystem

        # --- 3. Proceed with Registration ---
        if not hasattr(gs, 'layer_system') or not gs.layer_system:
            logging.warning(f"Layer system not found, cannot apply static ability: {self.effect_text}")
            return False

        # Clean effect text for layer determination
        effect_lower_clean = self.effect.lower().strip('.—\u2014: ')
        layer = self._determine_layer_for_effect(effect_lower_clean)

        if layer is None:
            logging.warning(f"StaticAbility.apply: Could not determine layer for static effect: '{self.effect_text}'")
            return False

        if affected_cards is None:
            affected_cards = self.get_affected_cards(game_state, controller)
            if affected_cards is None: affected_cards = [] # Ensure list

        # --- Parse and Register Potentially Multiple Layer Effects ---
        # Handle complex static abilities that affect multiple layers (like Kaito)
        parsed_effects_data = self._parse_multi_layer_effect(effect_lower_clean)
        registered_count = 0

        if parsed_effects_data: # Got specific parsed data
            for layer_data in parsed_effects_data:
                final_data = {
                    'source_id': self.card_id,
                    'affected_ids': affected_cards, # Apply to same targets unless overridden
                    'effect_text': self.effect_text, # Store original text
                    'duration': 'permanent',
                    'condition': lambda gs_check: (self.card_id in controller.get("battlefield", [])), # Standard condition
                    'controller_id': controller,
                    **layer_data # Merge parsed layer, sublayer, type, value
                }
                if game_state.layer_system.register_effect(final_data):
                    registered_count += 1
            if registered_count > 0:
                 logging.debug(f"Registered {registered_count} layer effects from static ability '{self.effect_text}'")
                 return True
            else:
                 logging.warning(f"Parsed multi-layer data for '{self.effect_text}', but failed to register any.")
                 return False

        else: # Fallback: Try to parse as single layer effect (less robust)
            parsed_data_single = None
            try:
                if layer == 7: parsed_data_single = self._parse_layer7_effect(effect_lower_clean)
                elif layer == 6: parsed_data_single = self._parse_layer6_effect(effect_lower_clean)
                elif layer == 5: parsed_data_single = self._parse_layer5_effect(effect_lower_clean)
                elif layer == 4: parsed_data_single = self._parse_layer4_effect(effect_lower_clean)
                # Layers 1-3 less common for this kind of fallback
            except Exception as parse_e:
                logging.error(f"Error parsing single Layer {layer} effect '{self.effect_text}': {parse_e}", exc_info=True)

            if parsed_data_single:
                 effect_data = {
                     'source_id': self.card_id,
                     'layer': layer,
                     'affected_ids': affected_cards,
                     'effect_text': self.effect_text,
                     'duration': 'permanent',
                     'condition': lambda gs_check: (self.card_id in controller.get("battlefield", [])),
                     'controller_id': controller,
                     **parsed_data_single # Add sublayer, type, value
                 }
                 if game_state.layer_system.register_effect(effect_data):
                      logging.debug(f"Registered static effect (single) '{self.effect_text}' in Layer {layer}")
                      return True
                 else:
                      logging.warning(f"Failed to register single-layer static effect '{self.effect_text}'")
                      return False
            else:
                 logging.warning(f"Static ability parser could not interpret effect (apply): '{self.effect_text}'")
                 return False


    def _parse_multi_layer_effect(self, effect_lower_clean):
        """
        Attempt to parse complex static abilities that affect multiple layers.
        Returns a list of effect data dictionaries, one for each layer/sublayer.
        Returns None if no complex pattern is matched.
        """
        # Example: Kaito: "During your turn, as long as Kaito has one or more loyalty counters on him, he's a 3/4 Ninja creature and has hexproof."
        kaito_match = re.match(r"(during your turn,)?\s*(as long as .+?,)?\s*(?:it's|he's|she's)\s+a\s+(\d+)/(\d+)\s+(.*?)\s+creature(?: and has (.*?))?(?:\.|$)", effect_lower_clean)
        if kaito_match:
            turn_restriction, condition_part, power_str, toughness_str, types_part, extra_keywords = kaito_match.groups()
            power = safe_int(power_str); toughness = safe_int(toughness_str)
            # --- Extract conditional logic ---
            # Base condition is presence on battlefield. Add "as long as" condition.
            # More complex conditional function generation needed here. Simplified for now.
            conditional_func = lambda gs: (self.card_id in self.game_state.get_card_controller(self.card_id).get("battlefield", [])) # Basic presence
            # Add loyalty check for Kaito
            if "loyalty counter" in (condition_part or ""):
                 controller = self.game_state.get_card_controller(self.card_id)
                 conditional_func = lambda gs: (controller and controller.get("loyalty_counters", {}).get(self.card_id, 0) > 0 and
                                                  self.card_id in controller.get("battlefield", []))

            # Base effect data for this ability
            base_data = {'affected_ids': [self.card_id], 'condition': conditional_func}

            effects = []
            # Layer 4: Add types (e.g., "Ninja")
            types_to_add = [t.strip() for t in types_part.split() if t.strip().capitalize() in Card.SUBTYPE_VOCAB or t.strip() in Card.ALL_CARD_TYPES]
            if types_to_add:
                # Determine if it adds *in addition* or sets the type
                # Kaito becomes a Ninja Creature -> SETS Creature type, adds Ninja subtype? Rules check.
                # Rule 205.1b: If effect makes it a type without "in addition", it loses other card types.
                # It keeps supertypes and subtypes appropriate to the new type.
                # This is complex. Simplify: Assume SET Creature type, ADD relevant subtypes.
                effects.append({**base_data, 'layer': 4, 'effect_type': 'set_type', 'effect_value': ["Creature"]})
                effects.append({**base_data, 'layer': 4, 'effect_type': 'add_subtype', 'effect_value': [t for t in types_to_add if t.lower() != 'creature']}) # Add non-creature parts as subtypes
            # Layer 6: Add keywords (e.g., "hexproof")
            if extra_keywords:
                 keywords_to_add = [kw.strip() for kw in extra_keywords.split('and') if kw.strip()]
                 for kw in keywords_to_add:
                     if kw in Card.ALL_KEYWORDS:
                          effects.append({**base_data, 'layer': 6, 'effect_type': 'add_ability', 'effect_value': kw})
            # Layer 7b: Set P/T
            effects.append({**base_data, 'layer': 7, 'sublayer': 'b', 'effect_type': 'set_pt', 'effect_value': (power, toughness)})
            return effects

        # Add patterns for other multi-layer static abilities here

        return None # No multi-layer pattern matched
        
    def _parse_layer1_effect(self, effect_lower):
        """Parse continuous copy effects for Layer 1 (Rare for static abilities)."""
        # Examples: "Creatures you control are copies of X" (X needs context)
        # Copy effects are usually established by spells/ETBs. Static abilities
        # granting copy status continuously are very rare and hard to parse generically.
        # This parser will look for simple markers but may not be fully functional
        # without knowing the target of the copy effect.

        copy_match = re.search(r"\b(is|are)\s+(a\s+)?copy of\s+(.+)", effect_lower)
        if copy_match:
            target_description = copy_match.group(3).strip()
            # Problem: Need to resolve 'target_description' to a specific card ID
            # which usually happens when the copy effect is created, not via static text.
            # We can register a marker effect, but LayerSystem needs the target ID.
            logging.warning(f"Layer 1 'copy' effect found ('{effect_lower}'), but target '{target_description}' cannot be resolved generically from static text. Effect may not apply correctly.")
            # For now, return a placeholder or None, as LayerSystem copy needs a target ID.
            # return {'effect_type': 'become_copy', 'effect_value': target_description} # Placeholder
            return None

        # Keyword "Changeling" is technically Layer 1-ish (sets types) but handled as Layer 4/6 usually.
        # If "changeling" is the *only* effect text, it implies type/ability setting.
        if effect_lower == "changeling":
             # Let Layer 4 handle the type setting, Layer 6 handle ability implications.
             return None

        return None # No common static Layer 1 effect parsed

    def _parse_layer2_effect(self, effect_lower):
        """Parse continuous control-changing effects for Layer 2 (Very rare for static abilities)."""
        # Examples: "You control target creature." (This is usually established by the effect resolution)
        # An Aura like "Control Magic" establishes this, but it's tied to the Aura's attachment state.
        # A static ability on Permanent A granting control of Permanent B continuously without targeting
        # is almost non-existent.

        gain_control_match = re.search(r"\b(gain|have)\s+control of\s+(.+)", effect_lower)
        if gain_control_match:
             target_description = gain_control_match.group(2).strip()
             # Similar to Layer 1, static control gain needs a target defined elsewhere.
             logging.warning(f"Layer 2 'control' effect found ('{effect_lower}'), but target '{target_description}' cannot be resolved generically from static text. Effect may not apply correctly.")
             # Returning None as control changes are typically handled by the source effect's resolution.
             return None

        return None # No common static Layer 2 effect parsed

    def _parse_layer3_effect(self, effect_lower):
        """Parse continuous text-changing effects for Layer 3 (Extremely rare)."""
        # Examples: "Creatures named X have text Y" (very specific and rare)
        # Most text-implication effects (like losing abilities) are handled functionally in Layer 6.
        # Literal text replacement is usually an activated/triggered ability (e.g., Mind Bend).

        text_change_match = re.search(r"text becomes\s+['\"](.+)['\"]", effect_lower)
        if text_change_match:
            new_text = text_change_match.group(1).strip()
            # Need to know the target subject of the text change.
            logging.warning(f"Layer 3 'text becomes' effect found ('{effect_lower}'), but determining target/subject generically is complex. Effect may not apply correctly.")
            # return {'effect_type': 'change_text', 'effect_value': new_text} # Placeholder
            return None

        # "Loses all abilities" implies text change but is handled functionally in Layer 6.
        # We avoid double-registering it here.

        return None # No common static Layer 3 effect parsed
        
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

    def _parse_layer6_effect(self, effect_lower_clean):
        """Parse ability adding/removing effects for Layer 6. (Uses cleaned text)"""
        # Check for removal first (more specific patterns)
        if "lose all abilities" in effect_lower_clean:
            return {'effect_type': 'remove_all_abilities', 'effect_value': True}

        # Simple "loses X" check - uses cleaned text
        lose_match = re.search(r"loses ([\w\s\-]+?)(?: and |,|$)", effect_lower_clean) # Removed check for trailing punctuation as it should be stripped
        if lose_match:
            ability_to_lose = lose_match.group(1).strip()
            # Normalize: Check against canonical keywords
            normalized_kw_lose = None
            for official_kw in Card.ALL_KEYWORDS:
                 # Use exact match after cleaning
                 if ability_to_lose == official_kw.lower():
                     normalized_kw_lose = official_kw # Use canonical name
                     break
            if normalized_kw_lose:
                 # Found a standard keyword being lost
                 return {'effect_type': 'remove_ability', 'effect_value': normalized_kw_lose}
            else:
                logging.debug(f"Potential non-keyword ability loss detected: '{ability_to_lose}' (not standard)")

        # Check for additions: "gains/has [ability list]" - uses cleaned text
        # Regex updated to stop at potential separators or end of string reliably
        gain_match = re.search(r"\b(have|has|gains?|gain)\s+(.*?)(?: and |,| until| —|\u2014|$)", effect_lower_clean)
        if gain_match:
            gained_abilities_text = gain_match.group(2).strip()
            # Split potential list by comma
            potential_gains = gained_abilities_text.split(',')
            # Process first matched keyword (refine later if multiple needed per effect)
            for potential_kw_phrase in potential_gains:
                potential_kw_phrase = potential_kw_phrase.strip()
                if not potential_kw_phrase: continue

                # Handle parametrized keywords explicitly first
                if potential_kw_phrase.startswith("protection from"):
                    # Use safer splitting
                    parts = potential_kw_phrase.split("protection from", 1)
                    if len(parts) == 2:
                        protected_from_value = parts[1].strip()
                        return {'effect_type': 'add_ability', 'effect_value': f"protection from {protected_from_value}"}
                elif potential_kw_phrase.startswith("ward"):
                    # Regex for ward cost ({X}, N, Pay X life etc.) - improved
                    ward_cost_match = re.match(r"ward\s*(?:-|—)?\s*(\{.*?\})$|\bward\s*(\d+)$|\bward\s*(pay \d+ life|discard a card)", potential_kw_phrase)
                    ward_cost = "{1}" # Default ward {1}
                    if ward_cost_match:
                         cost_part = ward_cost_match.group(1) or ward_cost_match.group(2) or ward_cost_match.group(3)
                         if cost_part:
                              if cost_part.isdigit(): ward_cost = f"{{{cost_part}}}"
                              else: ward_cost = cost_part.strip() # Takes {X}, pay N life, discard...
                    return {'effect_type': 'add_ability', 'effect_value': f"ward {ward_cost}"}

                # Check simple keywords against canonical list (using cleaned phrase)
                for official_kw in Card.ALL_KEYWORDS:
                    if potential_kw_phrase == official_kw.lower():
                        return {'effect_type': 'add_ability', 'effect_value': official_kw}
                # If it gets here after checking a phrase part, it wasn't a recognized keyword
                break # Move to next check after first phrase part processed

        # Check specific "can't attack/block" / "must attack/block" phrases - use cleaned text
        if "can't attack" in effect_lower_clean: return {'effect_type': 'add_ability', 'effect_value': 'cant_attack'}
        if "can't block" in effect_lower_clean: return {'effect_type': 'add_ability', 'effect_value': 'cant_block'}
        if "attacks each combat if able" in effect_lower_clean or "must attack if able" in effect_lower_clean:
            return {'effect_type': 'add_ability', 'effect_value': 'must_attack'}
        if "blocks each combat if able" in effect_lower_clean or "must block if able" in effect_lower_clean:
            return {'effect_type': 'add_ability', 'effect_value': 'must_block'}

        # Check if the *entire* cleaned effect is just a keyword
        for official_kw in Card.ALL_KEYWORDS:
             if effect_lower_clean == official_kw.lower():
                  return {'effect_type': 'add_ability', 'effect_value': official_kw}
        # Handle comma separated lists like "Flying, lifelink"
        parts = [p.strip() for p in effect_lower_clean.split(',')]
        if len(parts) > 1 and all(p in [k.lower() for k in Card.ALL_KEYWORDS] for p in parts):
             # Need to return multiple effects? Or handle list? Return first for now.
             return {'effect_type': 'add_ability', 'effect_value': Card.ALL_KEYWORDS[[k.lower() for k in Card.ALL_KEYWORDS].index(parts[0])]} # Return canonical name of first


        return None # No Layer 6 effect parsed


    def _parse_layer5_effect(self, effect_lower):
        """Parse color adding/removing effects for Layer 5."""
        colors_map = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        color_indices = {'W': 0, 'U': 1, 'B': 2, 'R': 3, 'G': 4}
        target_colors = None # None means no change from this effect
        effect_type = None

        # Check if SETTING specific colors (e.g., "is blue", "are white and black")
        # Matches "is [color]" or "are [color1] and [color2]" but NOT "is also"
        if re.search(r"\b(is|are)\b(?!\s+also)", effect_lower):
             is_setting = False
             found_colors_in_set = [0] * 5
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + re.escape(color_name) + r'\b', effect_lower):
                       found_colors_in_set[index] = 1
                       is_setting = True
             # Check for "is colorless"
             if re.search(r'\bis colorless\b', effect_lower):
                  found_colors_in_set = [0] * 5
                  is_setting = True # Setting to colorless is a type of setting

             if is_setting:
                  effect_type = 'set_color'
                  target_colors = found_colors_in_set

        # Check if ADDING colors (e.g., "is also blue")
        elif re.search(r"\b(is also|are also)\b", effect_lower):
             added_colors = [0] * 5
             found_addition = False
             for color_name, index in colors_map.items():
                  if re.search(r'\b' + re.escape(color_name) + r'\b', effect_lower):
                       added_colors[index] = 1
                       found_addition = True
             if found_addition:
                  effect_type = 'add_color'
                  target_colors = added_colors

        # Check if removing colors / becoming colorless (if not caught by "is colorless")
        elif "loses all colors" in effect_lower or "becomes colorless" in effect_lower:
             effect_type = 'set_color'
             target_colors = [0,0,0,0,0]

        if effect_type and target_colors is not None:
             return {'effect_type': effect_type, 'effect_value': target_colors}

        return None # No Layer 5 effect parsed


    def _parse_layer4_effect(self, effect_lower):
        """Parse type/subtype adding/removing effects for Layer 4."""
        # Patterns to detect type/subtype changes
        set_type_match = re.search(r"becomes? a(?:n)? ([\w\s]+?)(?: in addition| that's still|$)", effect_lower)
        add_type_match = re.search(r"(is|are) also a(?:n)? (\w+)", effect_lower)
        set_subtype_match = re.search(r"becomes? a(?:n)? ([\w\s]+?) creature", effect_lower)
        add_subtype_match = re.search(r"(?:is|are) also ([\w\s]+)", effect_lower)
        lose_type_match = re.search(r"loses all creature types", effect_lower) # Example removal

        # --- Process Type Setting/Adding ---
        # Handle "becomes TYPE..." / "is TYPE..."
        if set_type_match:
             type_text = set_type_match.group(1).strip()
             # Determine if it's setting or adding based on keywords
             is_addition = "in addition" in set_type_match.group(0) or "also a" in set_type_match.group(0) or "still a" in set_type_match.group(0)

             parts = type_text.split()
             types = [p for p in parts if p in Card.ALL_CARD_TYPES] # Filter known card types
             subtypes = [p.capitalize() for p in parts if p.capitalize() in Card.SUBTYPE_VOCAB] # Check known subtypes

             if types: # Change primary card types
                  effect_type = 'add_type' if is_addition else 'set_type'
                  logging.debug(f"Layer 4: Parsed {effect_type} with value {types}")
                  # set_type clears old types and subtypes unless specified together.
                  if not is_addition:
                      return {'effect_type': 'set_type_and_subtype', 'effect_value': (types, subtypes)}
                  else: # Just adding the type(s)
                      return {'effect_type': effect_type, 'effect_value': types}
             # If no main card types found, but parts exist, check subtypes
             elif subtypes and is_addition:
                 logging.debug(f"Layer 4: Parsed add_subtype from 'becomes/is also' clause: {subtypes}")
                 return {'effect_type': 'add_subtype', 'effect_value': subtypes}

        # Handle "is also a [type]" (Redundant with above, but safe fallback)
        elif add_type_match:
             type_text = add_type_match.group(2).strip()
             if type_text in Card.ALL_CARD_TYPES:
                  logging.debug(f"Layer 4: Parsed add_type with value {[type_text]}")
                  return {'effect_type': 'add_type', 'effect_value': [type_text]}
             elif type_text.capitalize() in Card.SUBTYPE_VOCAB: # Check if it's a subtype instead
                  logging.debug(f"Layer 4: Parsed add_subtype from 'is also a' clause: {[type_text.capitalize()]}")
                  return {'effect_type': 'add_subtype', 'effect_value': [type_text.capitalize()]}

        # --- Process Subtype Setting/Adding ---
        elif add_subtype_match: # "are also Saprolings"
             subtype_text = add_subtype_match.group(1).strip()
             potential_subtypes = [s.capitalize() for s in subtype_text.split() if s.capitalize() in Card.SUBTYPE_VOCAB]
             if potential_subtypes:
                  logging.debug(f"Layer 4: Parsed add_subtype with value {potential_subtypes}")
                  return {'effect_type': 'add_subtype', 'effect_value': potential_subtypes}

        # --- Process Type/Subtype Removal ---
        elif lose_type_match: # "loses all creature types"
             logging.debug("Layer 4: Parsed lose_all_subtypes (Creature)")
             # This effect is complex: Removes subtypes associated with 'creature' type.
             # Need better subtype mapping or specific LayerSystem handling.
             # For now, return a generic marker or handle in LayerSystem application.
             return {'effect_type': 'lose_subtype_by_type', 'effect_value': 'creature'}

        return None # No Layer 4 effect parsed

    def _determine_layer_for_effect(self, effect_lower):
        """Determine the appropriate layer for an effect based on its text. (Improved Pattern Matching)"""
        # Strip common punctuation and leading/trailing separators that might interfere
        cleaned_effect = effect_lower.strip('.—\u2014: ')

        # Layer 1: Copy effects
        if "copy" in cleaned_effect or "becomes a copy" in cleaned_effect: return 1
        # Layer 2: Control-changing effects
        if "gain control" in cleaned_effect or "exchange control" in cleaned_effect: return 2
        # Layer 3: Text-changing effects
        if "text becomes" in cleaned_effect: return 3

        # Layer 4: Type-changing effects
        # Check for "becomes [type]", "is also [type]", or specific type removals
        # Use word boundaries to avoid partial matches within other words
        type_pattern = r"\b(becomes?|is also|are also)\b.*\b(artifact|creature|enchantment|land|planeswalker|battle)\b"
        if re.search(type_pattern, cleaned_effect) or "loses all creature types" in cleaned_effect:
            return 4

        # Layer 5: Color-changing effects
        color_pattern = r"\b(is|are|becomes?)\b.*\b(white|blue|black|red|green|colorless)\b"
        if re.search(color_pattern, cleaned_effect) or "loses all colors" in cleaned_effect:
            return 5

        # Layer 6: Ability adding/removing effects
        # Use word boundaries for most keywords
        # Need to handle multi-word keywords and parametrized keywords like protection
        for kw in Card.ALL_KEYWORDS:
            kw_lower = kw.lower()
            # Use word boundaries for single-word keywords, simple substring for multi-word
            pattern = r'\b' + re.escape(kw_lower) + r'\b' if ' ' not in kw_lower else re.escape(kw_lower)
            # Check if the text explicitly grants or removes this keyword
            if re.search(rf"\b(gains?|has|lose|loses)\b.*\b{pattern}", cleaned_effect):
                 return 6
        # Catch cases like "lose all abilities", "can't attack/block", "must attack/block"
        if "lose all abilities" in cleaned_effect: return 6
        if any(restriction in cleaned_effect for restriction in ["can't attack", "can't block", "must attack", "must block"]): return 6

        # Layer 7: Power/toughness changing effects
        pt_patterns = [
            r"([+\-]\d+)\s*/\s*([+\-]\d+)",  # +N/+M, -N/-M
            r"\b(base power and toughness|base power|base toughness)\s+(?:is|are)\b", # Set base P/T
            r"\b(is|are|becomes)\s+\d+/\d+", # Set P/T to specific value
            r"(?:power and toughness are each equal to|power is equal to|toughness is equal to)", # CDA P/T setting
            r"switch.*power and toughness" # Switch P/T
        ]
        if any(re.search(pattern, cleaned_effect) for pattern in pt_patterns):
            return 7

        # If no standard static effect pattern matched, return None
        # Avoid classifying activated/triggered text like "Exile target creature..."
        # Basic check: Does it contain common action verbs typical of non-static effects?
        non_static_verbs = [r'\bexile\b', r'\bdestroy\b', r'\bcounter\b', r'\btap\b', r'\buntap\b', r'\bdraw\b', r'\bdiscard\b', r'\bsacrifice\b', r'\bsearch\b']
        if any(re.search(verb, cleaned_effect) for verb in non_static_verbs):
            # If it looks like an activated/triggered effect text, don't assign a layer
            # Exception: If it ALSO contains "gains/has/loses", it might be Layer 6. Handled above.
            is_layer6 = False
            for kw in Card.ALL_KEYWORDS:
                 pattern = r'\b' + re.escape(kw.lower()) + r'\b' if ' ' not in kw.lower() else re.escape(kw.lower())
                 if re.search(rf"\b(gains?|has|lose|loses)\b.*\b{pattern}", cleaned_effect):
                      is_layer6 = True; break
            if not is_layer6: return None # Looks like non-static

        # Final check: If it's just a keyword like "Flying" or "Lifelink" alone. This is Layer 6.
        # Use word boundaries and match entire cleaned string for single keywords.
        for kw in Card.ALL_KEYWORDS:
             kw_lower = kw.lower()
             if kw_lower == cleaned_effect:
                  return 6
        # Handle comma separated lists like "Flying, lifelink"
        parts = [p.strip() for p in cleaned_effect.split(',')]
        if len(parts) > 1 and all(p in [k.lower() for k in Card.ALL_KEYWORDS] for p in parts):
            return 6

        # If unsure, return None or log warning
        # Returning None is safer to avoid misclassification.
        logging.warning(f"LayerSystem: Could not determine layer for effect text: '{effect_lower}' (Cleaned: '{cleaned_effect}')")
        return None

    def _find_all_battlefield_cards(self, game_state):
        """Helper function to find all cards on the battlefield."""
        battlefield_cards = []
        for player in [game_state.p1, game_state.p2]:
            battlefield_cards.extend(player["battlefield"])
        return battlefield_cards

    def get_affected_cards(self, game_state, controller):
        """Determine which cards this static ability affects (Improved Scope Parsing)"""
        effect_lower = self.effect.lower() if self.effect else ""
        affected_cards = []
        me = controller
        opp = game_state.p2 if me == game_state.p1 else game_state.p1

        # Common scopes using regex for more flexibility
        scopes = {
            r"\bcreatures? you control\b": (me, "creature"),
            r"\bartifacts? you control\b": (me, "artifact"),
            r"\bpermanents? you control\b": (me, "permanent"),
            r"\blands? you control\b": (me, "land"),
            r"\bplaneswalkers? you control\b": (me, "planeswalker"),
            r"\bcreatures? opponents? control\b": (opp, "creature"),
            r"\bpermanents? opponents? control\b": (opp, "permanent"),
            r"\b(each|all) creatures?\b": (None, "creature"), # Affects both players
            r"\b(each|all) permanents?\b": (None, "permanent"),
            r"\b(each|all) artifacts?\b": (None, "artifact"),
            r"\b(each|all) enchantments?\b": (None, "enchantment"),
            r"\b(each|all) lands?\b": (None, "land"),
            r"\b(each|all) planeswalkers?\b": (None, "planeswalker"),
            r"\byou control\b": (me, "any"), # Generic "you control"
            r"opponents control\b": (opp, "any"), # Generic "opponents control"
            # More specific scopes like "attacking creatures", "untapped creatures", etc.
            r"\battacking creatures?\b": (None, "attacking_creature"),
            r"\bblocking creatures?\b": (None, "blocking_creature"),
            r"\buntapped creatures?\b": (None, "untapped_creature"),
            r"\btapped creatures?\b": (None, "tapped_creature"),
        }

        matched_scope = False
        for pattern, (player_scope, type_scope) in scopes.items():
            if re.search(pattern, effect_lower):
                players_to_check = []
                if player_scope is None: # Affects all players
                    players_to_check = [p for p in [me, opp] if p] # Check both if they exist
                else:
                    players_to_check.append(player_scope)

                for p in players_to_check:
                    if not p: continue # Skip if player is None
                    for card_id in p.get("battlefield", []): # Use get for safety
                         card = game_state._safe_get_card(card_id)
                         if self._card_matches_scope_criteria(card, type_scope, card_id, game_state, p):
                              affected_cards.append(card_id)
                matched_scope = True
                break # Stop after first matching scope (most specific should come first ideally)

        # Default: Affects the source card itself if no other scope matched
        if not matched_scope:
            affected_cards.append(self.card_id)

        # Remove duplicates and return
        return list(set(affected_cards))
    
    def _card_matches_scope_criteria(self, card, type_scope, card_id, game_state, player):
        """Helper to check if a card matches the scope criteria (type, state)."""
        if not card: return False
        # Check basic type
        if type_scope != "any":
            card_types = getattr(card, 'card_types', [])
            if type_scope != "permanent" and type_scope not in card_types and type_scope not in getattr(card,'subtypes',[]): # Allow subtype match
                return False # Type doesn't match

        # Check specific states
        if type_scope == "attacking_creature":
            if card_id not in getattr(game_state, 'current_attackers', []): return False
        elif type_scope == "blocking_creature":
             is_blocking = any(card_id in blockers for blockers in getattr(game_state, 'current_block_assignments', {}).values())
             if not is_blocking: return False
        elif type_scope == "tapped_creature":
             if 'creature' not in getattr(card, 'card_types', []) or card_id not in player.get("tapped_permanents", set()): return False
        elif type_scope == "untapped_creature":
             if 'creature' not in getattr(card, 'card_types', []) or card_id in player.get("tapped_permanents", set()): return False

        return True


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
        count_str = "X" if count == 'x' else str(count) if count != 1 else "a"
        card_str = "cards" if (isinstance(count, int) and count > 1) or count == 'x' else "card"
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} draw{'s' if target in ['controller','opponent','target_player'] else ''} {count_str} {card_str}", condition)
        self.base_count = count # Store original 'x' or number
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0
        if x_value > 0:
            effective_count = x_value
            logging.debug(f"DrawCardEffect: Using X={x_value} for draw count.")
        else:
            effective_count = text_to_number(self.base_count)
        # --- End X Cost Handling ---

        if effective_count <= 0: return True # Draw 0 has no effect

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"DrawCardEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
            num_drawn = 0
            success_player = True
            # Use effective_count
            for _ in range(effective_count):
                if hasattr(game_state, '_draw_card'):
                    drawn_card_id = game_state._draw_card(p)
                    if drawn_card_id: num_drawn += 1
                    else: success_player = False; break
                else: # Fallback
                    if p["library"]: p["hand"].append(p["library"].pop(0)); num_drawn += 1
                    else: p["attempted_draw_from_empty"] = True; success_player = False; break
            logging.debug(f"DrawCardEffect: Player {p['name']} drew {num_drawn} card(s).")
            overall_success &= success_player
        return overall_success


class GainLifeEffect(AbilityEffect):
    """Effect that causes players to gain life."""
    def __init__(self, amount, target="controller", condition=None):
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        amount_str = "X" if amount == 'x' else str(amount) # Represent X in description
        super().__init__(f"{target_desc} gain {amount_str} life", condition)
        # Store original amount which might be 'x' or a number
        # text_to_number handles 'x' -> 1, but we need the actual X from context
        self.base_amount = amount # Store the original 'x' or number
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        # Use X from context if available, otherwise use the base amount (converted)
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0 # Get X value from resolved context
        if x_value > 0:
            effective_amount = x_value
            logging.debug(f"GainLifeEffect: Using X={x_value} for life gain amount.")
        else:
            # Convert base amount only if not using X
            effective_amount = text_to_number(self.base_amount)
        # --- End X Cost Handling ---

        if effective_amount <= 0: return True # Gain 0 or less has no effect

        target_players = []
        # --- Target selection logic (remains the same) ---
        if self.target == "controller":
            target_players.append(controller)
        elif self.target == "opponent":
            target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids:
                target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else:
                 logging.warning(f"GainLifeEffect target_player failed: No player ID in targets {targets}")
                 return False
        elif self.target == "each_player":
             target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             if hasattr(game_state, 'gain_life'):
                  # Pass effective_amount derived from X or base value
                  actual_gained = game_state.gain_life(p, effective_amount, source_id)
                  if actual_gained <= 0:
                      pass # Logging handled in gain_life
                  else: pass
             else: # Fallback
                  original_life = p.get('life', 0)
                  p['life'] += effective_amount
                  gained = p['life'] - original_life
                  if gained > 0: logging.debug(f"GainLifeEffect (Manual): Player {p['name']} gained {gained} life.")
                  else: overall_success = False # Less precise check without gain_life
        return overall_success



class DamageEffect(AbilityEffect):
    """Effect that deals damage to targets."""
    def __init__(self, amount, target_type="any target", condition=None):
        target_type_str = str(target_type).lower() if target_type is not None else "any target"
        amount_str = "X" if amount == 'x' else str(amount) # Represent X in description
        super().__init__(f"Deal {amount_str} damage to {target_type_str}", condition)
        # Store original amount which might be 'x' or a number
        self.base_amount = amount
        self.target_type = target_type_str # e.g., "creature", "player", "any target", "each opponent"
        self.requires_target = "target" in self.target_type or "any" in self.target_type or "each" not in self.target_type

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0
        if x_value > 0:
            effective_amount = x_value
            logging.debug(f"DamageEffect: Using X={x_value} for damage amount.")
        else:
            effective_amount = text_to_number(self.base_amount)
        # --- End X Cost Handling ---

        if effective_amount <= 0: return True # No damage dealt

        targets_to_damage = [] # List of target_id
        processed_ids = set()

        # --- Target Collection Logic (remains the same) ---
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 logging.warning(f"DamageEffect requires targets, but none found in dict: {targets}")
                 return False
            relevant_categories = set()
            if self.target_type == "any target": relevant_categories = {"creatures", "players", "planeswalkers", "battles"}
            elif self.target_type == "creature": relevant_categories = {"creatures"}
            elif self.target_type == "player": relevant_categories = {"players"}
            elif self.target_type == "planeswalker": relevant_categories = {"planeswalkers"}
            elif self.target_type == "battle": relevant_categories = {"battles"}
            elif self.target_type == "permanent": relevant_categories = {"creatures", "planeswalkers", "battles", "artifacts", "enchantments", "lands"}
            else:
                 base_cat = self.target_type.replace('target ', '') # Basic removal
                 relevant_categories.add(base_cat + "s" if not base_cat.endswith('s') else base_cat)

            for cat, id_list in targets.items():
                if cat in relevant_categories:
                    for target_id in id_list:
                        if target_id not in processed_ids:
                            processed_ids.add(target_id)
                            targets_to_damage.append(target_id)
        elif "each opponent" in self.target_type:
             opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
             opp_id = "p2" if opponent == game_state.p2 else "p1"
             targets_to_damage.append(opp_id)
        elif "each creature" in self.target_type:
             targets_to_damage.extend(game_state.get_all_creatures()) # Assumes GS helper exists
        elif "each player" in self.target_type:
             targets_to_damage.extend(["p1", "p2"])

        if not targets_to_damage:
             logging.warning(f"DamageEffect: No valid targets collected for '{self.effect_text}'. Provided: {targets}")
             return False

        # --- Damage Application Logic (uses effective_amount, otherwise remains the same) ---
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
                     if has_infect:
                          target_owner.setdefault("poison_counters", 0)
                          target_owner["poison_counters"] += effective_amount # Use effective amount for counters
                          damage_applied = effective_amount # Track for lifelink based on intended damage
                          logging.debug(f"{target_owner['name']} got {effective_amount} poison counters from infect.")
                     elif hasattr(game_state, 'damage_player'):
                          # Pass effective_amount
                          damage_applied = game_state.damage_player(target_owner, effective_amount, source_id)
                     else: # Fallback
                          target_owner['life'] -= effective_amount; damage_applied = effective_amount
                 else: # Permanent target
                      if 'creature' in getattr(target_obj, 'card_types', []):
                           if has_infect: # Damage is -1/-1 counters
                                if hasattr(game_state,'add_counter'):
                                    game_state.add_counter(target_id, '-1/-1', effective_amount) # Use effective amount
                                    damage_applied = effective_amount
                           else:
                                damage_applied = game_state.apply_damage_to_permanent(target_id, effective_amount, source_id, False, has_deathtouch) # Pass effective amount
                      elif 'planeswalker' in getattr(target_obj, 'card_types', []):
                           damage_applied = game_state.damage_planeswalker(target_id, effective_amount, source_id) # Pass effective amount
                      elif 'battle' in getattr(target_obj, 'type_line', ''):
                           damage_applied = game_state.damage_battle(target_id, effective_amount, source_id) # Pass effective amount

                 if damage_applied > 0:
                      total_actual_damage += damage_applied
                      success_overall = True
             except Exception as dmg_e:
                  logging.error(f"Error applying damage to {target_id}: {dmg_e}", exc_info=True)

        # --- Lifelink logic (remains the same) ---
        if has_lifelink and total_actual_damage > 0:
            if hasattr(game_state, 'gain_life'): game_state.gain_life(controller, total_actual_damage, source_id)
            else: controller['life'] += total_actual_damage

        return success_overall

class AddCountersEffect(AbilityEffect):
    """Effect that adds counters to permanents or players."""
    def __init__(self, counter_type, count=1, target_type="creature", condition=None):
        count_str = "X" if count == 'x' else str(count) # Represent X in description
        super().__init__(f"Put {count_str} {counter_type} counter(s) on target {target_type}", condition)
        self.counter_type = counter_type.replace('_','/') # Allow P/T format storage
        # Store original count which might be 'x' or a number
        self.base_count = count
        self.target_type = target_type.lower() # Normalize
        self.requires_target = "target" in target_type or "each" not in target_type # Check if it targets specifically

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0
        if x_value > 0:
            effective_count = x_value
            logging.debug(f"AddCountersEffect: Using X={x_value} for counter count.")
        else:
            effective_count = text_to_number(self.base_count) # Use original base count
        # --- End X Cost Handling ---

        if effective_count <= 0: return True # Adding 0 or less has no effect

        targets_to_affect = []
        processed_ids = set()
        # --- Target Collection Logic (remains the same) ---
        if self.requires_target:
            if not targets or not any(v for v in targets.values()):
                 logging.warning(f"AddCountersEffect requires targets, none provided/resolved: {targets}")
                 return False
            relevant_categories = set()
            if "creature" in self.target_type: relevant_categories.add("creatures")
            if "artifact" in self.target_type: relevant_categories.add("artifacts")
            if "planeswalker" in self.target_type: relevant_categories.add("planeswalkers")
            if "enchantment" in self.target_type: relevant_categories.add("enchantments")
            if "land" in self.target_type: relevant_categories.add("lands")
            if "permanent" in self.target_type: relevant_categories.update(["creatures", "artifacts", "enchantments", "planeswalkers", "lands", "battles"])
            if "player" in self.target_type: relevant_categories.add("players")
            if not relevant_categories: relevant_categories.add(self.target_type+"s")

            for cat, id_list in targets.items():
                 if cat in relevant_categories:
                     targets_to_affect.extend(id_list)
        elif "self" == self.target_type: targets_to_affect.append(source_id)
        elif "each creature" == self.target_type: targets_to_affect.extend(game_state.get_all_creatures())
        elif "each opponent" == self.target_type:
            opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
            opp_id = "p2" if opponent == game_state.p2 else "p1"
            targets_to_affect.append(opp_id)

        if not targets_to_affect:
            logging.warning(f"AddCountersEffect: No valid targets collected for '{self.effect_text}'. Targets: {targets}")
            return False

        unique_targets = set(targets_to_affect)
        success_count = 0
        # --- Counter Application (uses effective_count, otherwise remains the same) ---
        for target_id in unique_targets:
             target_owner, target_zone = game_state.find_card_location(target_id)
             is_player_target = target_id in ["p1", "p2"]
             target_obj = target_owner if is_player_target else game_state._safe_get_card(target_id)

             if not target_obj or (not is_player_target and target_zone != "battlefield"):
                 logging.debug(f"AddCountersEffect: Target {target_id} invalid or not on battlefield.")
                 continue

             if is_player_target: # Add counters to player
                 if self.counter_type == 'poison':
                     target_owner.setdefault("poison_counters", 0); target_owner["poison_counters"] += effective_count # Use effective count
                     success_count += 1; logging.debug(f"Added {effective_count} poison counter(s) to player {target_owner['name']}.")
                 elif self.counter_type == 'energy':
                     target_owner.setdefault("energy_counters", 0); target_owner["energy_counters"] += effective_count # Use effective count
                     success_count += 1; logging.debug(f"Added {effective_count} energy counter(s) to player {target_owner['name']}.")
                 else: logging.warning(f"Cannot add counter type '{self.counter_type}' to player.")
             else: # Add counters to permanent
                  if hasattr(game_state, 'add_counter') and callable(game_state.add_counter):
                      # Pass effective_count
                      if game_state.add_counter(target_id, self.counter_type, effective_count): success_count += 1
                  else: # Fallback
                      target_card = target_obj
                      if not hasattr(target_card, 'counters'): target_card.counters = {}
                      target_card.counters[self.counter_type] = target_card.counters.get(self.counter_type, 0) + effective_count # Use effective count
                      logging.debug(f"Fallback AddCounters: Added {effective_count} {self.counter_type} to {target_card.name}")
                      success_count += 1

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
        # Handle 'x' for description
        if count == 'x':
             count_text = "X card(s)"
             self.base_count = 'x'
        elif count == -1: # Represents "all"
             count_text = "their hand"
             self.base_count = -1
        else: # Specific number
             count_num = text_to_number(count) # Ensure it's a number
             count_text = f"{count_num} card{'s' if count_num != 1 else ''}"
             self.base_count = count_num

        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        random_text = " at random" if is_random else ""
        super().__init__(f"{target_desc} discards {count_text}{random_text}", condition)
        # self.base_count stored above
        self.target = target
        self.is_random = is_random
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0
        if x_value > 0:
            effective_count = x_value
            logging.debug(f"DiscardEffect: Using X={x_value} for discard count.")
        else:
            effective_count = self.base_count # Already numeric (-1 for all, or N)
        # --- End X Cost Handling ---

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"DiscardEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = False
        for p in target_players:
            player_hand = p.get("hand", [])
            if not player_hand: continue

            # Use effective_count
            discard_count_needed = len(player_hand) if effective_count == -1 else min(effective_count, len(player_hand))
            if discard_count_needed <= 0: continue

            cards_to_discard = []
            # Discard logic (random or choice)... (remains the same)
            if self.is_random:
                  cards_to_discard = random.sample(player_hand, discard_count_needed)
            else: # Player chooses
                  sorted_hand = sorted([(cid, getattr(game_state._safe_get_card(cid), 'cmc', 0)) for cid in player_hand], key=lambda x: -x[1])
                  cards_to_discard = [cid for cid, cmc in sorted_hand[:discard_count_needed]]

            num_discarded_this_player = 0
            for card_id in cards_to_discard:
                 # Discard movement logic... (remains the same, including Madness checks)
                 if card_id in p.get("hand",[]):
                    discard_context = {'card_id': card_id, 'player': p, 'cause': 'discard'}
                    modified_context, replaced = game_state.apply_replacement_effect("DISCARD", discard_context)
                    if replaced and modified_context.get('prevented', False): continue
                    final_dest_zone = modified_context.get('to_zone', 'graveyard')
                    madness_cost = None
                    if final_dest_zone == 'exile':
                        card_obj = game_state._safe_get_card(card_id)
                        if card_obj and "madness" in getattr(card_obj,'oracle_text','').lower():
                             madness_cost = game_state._get_madness_cost_str_gs(card_obj)

                    if game_state.move_card(card_id, p, "hand", p, final_dest_zone, cause="discard", context={"source_id": source_id}):
                        num_discarded_this_player += 1
                        if madness_cost:
                             if not hasattr(game_state, 'madness_cast_available'): game_state.madness_cast_available = None # Use None instead of madness_trigger
                             game_state.madness_cast_available = {'card_id': card_id, 'player': p, 'cost': madness_cost}
                             logging.debug(f"Card {card_id} discarded with Madness, moved to exile. Player can cast for {madness_cost}.")
                    else: logging.warning(f"Failed to move {card_id} from hand to {final_dest_zone} during discard.")


            if num_discarded_this_player > 0:
                 # Tracking logic... (remains the same)
                 overall_success = True

        return overall_success

class MillEffect(AbilityEffect):
    """Effect that mills cards from library to graveyard."""
    def __init__(self, count=1, target="opponent", condition=None):
        count_str = "X" if count == 'x' else str(count) # Represent X in description
        target_text_map = {"controller": "You", "opponent": "Target opponent", "target_player": "Target player", "each_player": "Each player"}
        target_desc = target_text_map.get(target, "Target player")
        super().__init__(f"{target_desc} mills {count_str} card{'s' if count == 'x' or count > 1 else ''}", condition)
        self.base_count = count # Store original 'x' or number
        self.target = target
        self.requires_target = "target" in target

    def _apply_effect(self, game_state, source_id, controller, targets):
        # --- X Cost Handling ---
        x_value = targets.get('X', 0) if isinstance(targets, dict) else 0
        if x_value > 0:
            effective_count = x_value
            logging.debug(f"MillEffect: Using X={x_value} for mill count.")
        else:
            effective_count = text_to_number(self.base_count)
        # --- End X Cost Handling ---

        if effective_count <= 0: return True

        target_players = []
        # Target selection logic... (remains the same)
        if self.target == "controller": target_players.append(controller)
        elif self.target == "opponent": target_players.append(game_state.p2 if controller == game_state.p1 else game_state.p1)
        elif self.target == "target_player":
            player_ids = targets.get("players", []) if isinstance(targets, dict) else []
            if player_ids: target_players.append(game_state.p1 if player_ids[0] == "p1" else game_state.p2)
            else: logging.warning(f"MillEffect target_player failed: No player ID in targets {targets}"); return False
        elif self.target == "each_player": target_players.extend([p for p in [game_state.p1, game_state.p2] if p])

        if not target_players: return False

        overall_success = True
        for p in target_players:
             player_library = p.get("library", [])
             if not player_library: logging.debug(f"MillEffect: Player {p['name']}'s library is empty."); continue

             # Use effective_count
             num_to_mill = min(effective_count, len(player_library))
             if num_to_mill <= 0: continue

             ids_to_mill = player_library[:num_to_mill]
             actual_milled_count = 0
             for card_id in ids_to_mill:
                  # Use move_card (library source zone implicit)
                  success_move = game_state.move_card(card_id, p, "library", p, "graveyard", cause="mill", context={"source_id": source_id})
                  if success_move: actual_milled_count += 1
                  else: pass # Logging in move_card

             logging.debug(f"MillEffect: Milled {actual_milled_count} card(s) from {p['name']}'s library.")
             overall_success &= (actual_milled_count > 0)
             # Tracking logic... (remains the same)
             if actual_milled_count > 0:
                  if not hasattr(game_state, 'cards_milled_this_turn'): game_state.cards_milled_this_turn = {}
                  player_id = 'p1' if p == game_state.p1 else 'p2'
                  game_state.cards_milled_this_turn[player_id] = game_state.cards_milled_this_turn.get(player_id, 0) + actual_milled_count
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


class ScryEffect(AbilityEffect):
    def __init__(self, count=1, condition=None):
        super().__init__(f"Scry {count}", condition)
        self.count = count

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Initiate the scry process by setting the game state."""
        if not controller or "library" not in controller or not controller["library"]:
            logging.debug(f"Cannot Scry: Player {controller.get('name', 'Unknown')} or library invalid.")
            return False # Cannot scry with no library

        count = min(self.count, len(controller["library"]))
        if count <= 0: return True # Scry 0 is valid, does nothing

        scried_cards = controller["library"][:count]
        if not scried_cards: return False # Should not happen

        # --- Set up state for external AI/ActionHandler to make choices ---
        # Store previous phase if not already in a special choice phase
        if game_state.phase not in [game_state.PHASE_CHOOSE, game_state.PHASE_TARGETING, game_state.PHASE_SACRIFICE]:
            game_state.previous_priority_phase = game_state.phase

        game_state.phase = game_state.PHASE_CHOOSE
        # Create context for the choice
        game_state.choice_context = {
            'type': 'scry',
            'player': controller,
            'count': count, # Original scry number
            'cards': scried_cards[:], # Copy of cards being looked at (list can be modified)
            'kept_on_top': [], # Store IDs player chooses to keep on top
            'put_on_bottom': [], # Store IDs player chooses to put on bottom
            'source_id': source_id,
            'resolved': False # Flag to indicate choice processing is complete
        }
        # Clear priority passing and set priority to the choosing player
        game_state.priority_pass_count = 0
        game_state.priority_player = controller # Scrying player has priority to choose

        logging.info(f"Entering Scry choice phase for {controller['name']} ({count} cards: {[getattr(game_state._safe_get_card(cid), 'name', cid) for cid in scried_cards]}).")
        return True # Initiated scry choice process successfully

class SurveilEffect(AbilityEffect):
    def __init__(self, count=1, condition=None):
        super().__init__(f"Surveil {count}", condition)
        self.count = count

    def _apply_effect(self, game_state, source_id, controller, targets):
        """Initiate the surveil process by setting the game state."""
        if not controller or "library" not in controller or not controller["library"]:
            logging.debug(f"Cannot Surveil: Player {controller.get('name', 'Unknown')} or library invalid.")
            return False # Cannot surveil with no library

        count = min(self.count, len(controller["library"]))
        if count <= 0: return True # Surveil 0 is valid, does nothing

        surveiled_cards = controller["library"][:count]
        if not surveiled_cards: return False

        # --- Set up state for external AI/ActionHandler to make choices ---
        # Store previous phase
        if game_state.phase not in [game_state.PHASE_CHOOSE, game_state.PHASE_TARGETING, game_state.PHASE_SACRIFICE]:
            game_state.previous_priority_phase = game_state.phase

        game_state.phase = game_state.PHASE_CHOOSE
        # Create context
        game_state.choice_context = {
            'type': 'surveil',
            'player': controller,
            'count': count,
            'cards': surveiled_cards[:], # Copy of cards to process
            'kept_on_top': [], # Unused for surveil, kept for potential future compatibility?
            'put_in_graveyard': [], # Track cards put in graveyard
            'source_id': source_id,
            'resolved': False
        }
        # Clear priority passing and set priority to the choosing player
        game_state.priority_pass_count = 0
        game_state.priority_player = controller

        logging.info(f"Entering Surveil choice phase for {controller['name']} ({count} cards: {[getattr(game_state._safe_get_card(cid), 'name', cid) for cid in surveiled_cards]}).")
        return True # Initiated surveil choice process successfully

class LifeDrainEffect(AbilityEffect):
    def __init__(self, amount=1, target="opponent", gain_target="controller", condition=None):
        super().__init__(f"Target {target} loses {amount} life and you gain {amount} life", condition)
        self.amount = amount
        self.target = target # "opponent", "each opponent", "target player"
        self.gain_target = gain_target # Usually "controller"
        self.requires_target = "target" in target # Requires specific player target?

    def _apply_effect(self, game_state, source_id, controller, targets):
        if self.amount <= 0: return True # No effect

        life_lost_this_instance = 0 # Track life lost by this specific effect application

        # --- Target(s) for Life Loss ---
        target_players_loss = []
        opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
        if self.target == "opponent":
            target_players_loss.append(opponent)
        elif self.target == "each opponent":
             # Assumes 2 players for now
             target_players_loss.append(opponent)
             # TODO: Extend for multi-player
        elif self.target == "target player":
             player_ids = targets.get("players", [])
             if player_ids:
                 p_target = game_state.p1 if player_ids[0] == "p1" else game_state.p2
                 target_players_loss.append(p_target)
             else:
                 logging.warning("LifeDrainEffect: Target player missing for life loss.")
                 return False # Needs target

        if not target_players_loss: return False

        # Apply life loss
        for p_loss in target_players_loss:
             # Life loss is different from damage
             # Use GameState method if available
             if hasattr(game_state, 'lose_life'):
                 actual_loss = game_state.lose_life(p_loss, self.amount, source_id=source_id)
                 life_lost_this_instance += actual_loss
             else: # Fallback direct modification
                 # Check for replacements manually (simplified)
                 loss_context = {'player': p_loss, 'life_amount': self.amount, 'source_id': source_id}
                 modified_context, replaced = game_state.apply_replacement_effect("LIFE_LOSS", loss_context)
                 actual_loss = modified_context.get('life_amount', 0) if not modified_context.get('prevented') else 0

                 if actual_loss > 0:
                      p_loss['life'] -= actual_loss
                      life_lost_this_instance += actual_loss
                      p_loss['lost_life_this_turn'] = True # Flag for Spectacle etc.
                      logging.debug(f"(Fallback) LifeDrainEffect: {p_loss['name']} lost {actual_loss} life.")
                      game_state.trigger_ability(None, "LOSE_LIFE", {"player": p_loss, "amount": actual_loss, "source_id": source_id})


        # --- Target for Life Gain ---
        player_gaining_life = None
        if self.gain_target == "controller":
            player_gaining_life = controller
        # TODO: Handle other gain targets if needed

        # Apply life gain (Amount depends on specific card - usually amount drained OR fixed amount)
        # Simple implementation: Gain amount equal to life lost *by this effect instance*.
        amount_to_gain = life_lost_this_instance

        if player_gaining_life and amount_to_gain > 0:
             if hasattr(game_state, 'gain_life'):
                 # gain_life handles logging and triggers
                 game_state.gain_life(player_gaining_life, amount_to_gain, source_id=source_id)
             else: # Fallback
                  gain_context = {'player': player_gaining_life, 'life_amount': amount_to_gain, 'source_id': source_id}
                  modified_gain_context, replaced = game_state.apply_replacement_effect("LIFE_GAIN", gain_context)
                  actual_gain = modified_gain_context.get('life_amount', 0) if not modified_gain_context.get('prevented') else 0
                  if actual_gain > 0:
                      player_gaining_life['life'] += actual_gain
                      logging.debug(f"(Fallback) LifeDrainEffect: {player_gaining_life['name']} gained {actual_gain} life.")
                      game_state.trigger_ability(source_id, "GAIN_LIFE", {"player": player_gaining_life, "amount": actual_gain, "source_id": source_id})

        # Check SBAs after life changes (done in main loop usually)
        # game_state.check_state_based_actions() # Optional immediate check
        return life_lost_this_instance > 0 # Return success if any life was lost


class CopySpellEffect(AbilityEffect):
    def __init__(self, target_type="spell", new_targets=True, condition=None):
        super().__init__(f"Copy target {target_type}{' and you may choose new targets' if new_targets else ''}", condition)
        self.target_type = target_type # spell, instant, sorcery
        self.new_targets = new_targets # If the copy can choose new targets
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        target_ids = targets.get("spells", []) # Expect spell target
        if not target_ids:
             logging.warning("CopySpellEffect: No spell target provided in targets dict.")
             return False

        original_spell_id = target_ids[0] # Assume first target

        # Find the original spell on the stack
        original_stack_item = None
        original_stack_idx = -1
        for i, item in enumerate(game_state.stack):
            if isinstance(item, tuple) and item[0] == "SPELL" and item[1] == original_spell_id:
                 original_stack_item = item
                 original_stack_idx = i
                 break

        if not original_stack_item:
             logging.warning(f"CopySpellEffect: Target spell {original_spell_id} not found on stack.")
             return False

        spell_type, spell_id, original_controller, original_context = original_stack_item

        # Check target type restriction if specified
        spell_card = game_state._safe_get_card(spell_id)
        if not spell_card: return False # Card vanished?
        if self.target_type == "instant" and 'instant' not in getattr(spell_card, 'card_types', []): return False
        if self.target_type == "sorcery" and 'sorcery' not in getattr(spell_card, 'card_types', []): return False

        # --- Create Copy Context ---
        import copy
        new_context = copy.deepcopy(original_context)
        new_context["is_copy"] = True
        new_context["copied_by"] = source_id
        new_context["original_caster"] = original_controller # Track original caster if needed
        new_context["needs_new_targets"] = self.new_targets
        # Reset choices/payments made for the original spell
        new_context.pop("selected_modes", None)
        new_context.pop("X", None) # Ensure X is re-chosen for copy if applicable
        new_context.pop("chosen_color", None)
        new_context.pop("targets", None) # Clear previous targets
        new_context.pop("paid_kicker", None) # Don't inherit kicker payment status
        new_context.pop("kicked", None) # Clear kicker flag
        # Remove any cost payment details
        new_context.pop("final_paid_cost", None)
        # Add a unique ID for this copy instance
        copy_instance_id = f"copy_{game_state.turn}_{len(game_state.stack)}_{random.randint(1000,9999)}"
        new_context['copy_instance_id'] = copy_instance_id

        # Add copy to stack (controlled by effect's controller)
        game_state.add_to_stack("SPELL", spell_id, controller, new_context)
        logging.debug(f"Created copy ({copy_instance_id}) of spell {spell_id} on stack, controlled by {controller['name']}.")

        # --- Handle Target Selection for the Copy ---
        original_requires_target = original_context.get("requires_target", False)
        original_num_targets = original_context.get("num_targets", 1)

        if self.new_targets and original_requires_target and original_num_targets > 0:
            # Set up targeting phase specifically for the *copy*
            logging.debug(f"Copy {copy_instance_id} needs new targets. Entering TARGETING phase.")
            game_state.previous_priority_phase = game_state.phase # Store current phase
            game_state.phase = game_state.PHASE_TARGETING
            game_state.targeting_context = {
                 "source_id": spell_id, # Refers to the spell card being copied
                 "copy_instance_id": copy_instance_id, # Identify the specific copy
                 "controller": controller, # Controller of the copy
                 "required_type": getattr(spell_card, 'target_type', 'target'), # Guess from original if possible
                 "required_count": original_num_targets,
                 "min_targets": 0 if "up to" in getattr(spell_card, 'oracle_text','').lower() else original_num_targets,
                 "max_targets": original_num_targets,
                 "selected_targets": [],
                 "effect_text": getattr(spell_card, 'oracle_text', '')
             }
            # Ensure priority is set correctly for the choice
            game_state.priority_player = controller
            game_state.priority_pass_count = 0
        else:
            # Copy uses original targets (if still valid) or resolves without targets
            resolved_targets = {} # Default empty targets
            if not self.new_targets and "targets" in original_context:
                 # Check validity of original targets for the copy (controller = current player)
                 if game_state._validate_targets_on_resolution(spell_id, controller, original_context["targets"]):
                     resolved_targets = original_context["targets"] # Copy targets
                     logging.debug(f"Copy {copy_instance_id} using original targets.")
                 else:
                     logging.debug(f"Original targets invalid for copy {copy_instance_id}, copy might fizzle.")
                     # Mark as no valid targets
            else:
                logging.debug(f"Copy {copy_instance_id} either doesn't require targets or uses no targets.")

            # Update the stack item with resolved targets context immediately
            stack_idx_to_update = -1
            for i, item in enumerate(reversed(game_state.stack)):
                if isinstance(item, tuple) and item[3].get('copy_instance_id') == copy_instance_id:
                     stack_idx_to_update = len(game_state.stack) - 1 - i
                     break
            if stack_idx_to_update != -1:
                new_context_with_targets = game_state.stack[stack_idx_to_update][3]
                new_context_with_targets["targets"] = resolved_targets
                game_state.stack[stack_idx_to_update] = game_state.stack[stack_idx_to_update][:3] + (new_context_with_targets,)
                logging.debug(f"Updated stack item {stack_idx_to_update} (Copy) with targets: {resolved_targets}")
            else:
                 logging.warning("Could not find newly added copy on stack to update targets!")

        return True

class TransformEffect(AbilityEffect):
    def __init__(self, condition=None):
        super().__init__("Transform this permanent", condition)
        self.requires_target = False # Usually affects self

    def _apply_effect(self, game_state, source_id, controller, targets):
        # Transform usually targets the source itself
        target_id = source_id
        # Allow context to override target if necessary (e.g., specific instruction)
        # Check if context provides a specific permanent target ID
        target_id_from_context = None
        if targets and isinstance(targets, dict) and "permanents" in targets and targets["permanents"]:
             target_id_from_context = targets["permanents"][0] # Assume first permanent target
        elif targets and isinstance(targets, list): # Handle flat list if passed by simple resolver
             # Cannot determine if it's the intended target, default to source_id
             pass

        if target_id_from_context and target_id_from_context != source_id:
             target_id = target_id_from_context
             logging.debug(f"Transform effect targeting {target_id} instead of source {source_id} due to context.")

        # Use GameState method to handle transformation and triggers
        if hasattr(game_state, 'transform_card') and callable(game_state.transform_card):
             # transform_card handles validation (is transformable, can transform now?)
             success = game_state.transform_card(target_id)
             if success:
                 card = game_state._safe_get_card(target_id)
                 logging.debug(f"Successfully triggered transform for {getattr(card,'name', target_id)}")
                 return True
             else:
                 logging.debug(f"Transform failed for {target_id} (handled by game_state.transform_card).")
                 return False
        else:
             logging.error("TransformEffect failed: GameState lacks 'transform_card' method.")
             return False

class FightEffect(AbilityEffect):
    def __init__(self, target_type="creature", condition=None):
        # Ensure effect text correctly reflects the source fighting the target
        super().__init__(f"This creature fights target {target_type}", condition)
        self.target_type = target_type # Usually creature
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        fighter1_id = source_id # The source of the fight effect
        fighter2_id = None

        # Expect target in creature list (most common) or permanents list
        target_candidates = targets.get("creatures", []) + targets.get("permanents", [])
        if target_candidates:
            # Filter out the source if it was accidentally targeted
            possible_targets = [tid for tid in target_candidates if tid != fighter1_id]
            if possible_targets:
                fighter2_id = possible_targets[0] # Assume first valid target
            else:
                 logging.warning(f"FightEffect from {fighter1_id}: No valid target provided other than self. Targets: {targets}")
                 return False
        else:
             logging.warning(f"FightEffect from {fighter1_id}: No target creature/permanent provided. Targets: {targets}")
             return False

        fighter1 = game_state._safe_get_card(fighter1_id)
        fighter2 = game_state._safe_get_card(fighter2_id)
        f1_owner, f1_zone = game_state.find_card_location(fighter1_id)
        f2_owner, f2_zone = game_state.find_card_location(fighter2_id)

        # Both must be creatures on the battlefield currently
        if not fighter1 or 'creature' not in getattr(fighter1, 'card_types', []) or f1_zone != 'battlefield':
            logging.debug(f"FightEffect: Fighter1 ({fighter1_id}) is not a valid creature on the battlefield.")
            return False
        if not fighter2 or 'creature' not in getattr(fighter2, 'card_types', []) or f2_zone != 'battlefield':
            logging.debug(f"FightEffect: Fighter2 ({fighter2_id}) is not a valid creature on the battlefield.")
            return False

        # Get current power post-layers (Important!)
        power1 = getattr(fighter1, 'power', 0) or 0 # Use 0 if power is None
        power2 = getattr(fighter2, 'power', 0) or 0

        logging.debug(f"Fight: {fighter1.name} ({power1} power) vs {fighter2.name} ({power2} power)")

        # Deal damage simultaneously using GameState methods that handle replacements etc.
        # Source of damage is the creature itself
        damage_dealt_by_1 = 0
        damage_dealt_by_2 = 0
        if power1 > 0:
             damage_dealt_by_1 = game_state.apply_damage_to_permanent(fighter2_id, power1, fighter1_id, is_combat_damage=False)
        if power2 > 0:
             damage_dealt_by_2 = game_state.apply_damage_to_permanent(fighter1_id, power2, fighter2_id, is_combat_damage=False)

        # SBAs checked in main loop after resolution
        game_state.trigger_ability(fighter1_id, "FIGHT_RESOLVED", {"opponent_id": fighter2_id, "damage_dealt": damage_dealt_by_1, "damage_taken": damage_dealt_by_2})
        game_state.trigger_ability(fighter2_id, "FIGHT_RESOLVED", {"opponent_id": fighter1_id, "damage_dealt": damage_dealt_by_2, "damage_taken": damage_dealt_by_1})
        # Return true if the fight happened (damage was attempted)
        return True
    
class BuffEffect(AbilityEffect):
    """Effect that buffs power/toughness. Registers with LayerSystem."""
    def __init__(self, power_mod, toughness_mod, target_type="creature", duration="end_of_turn", condition=None):
        super().__init__(f"{target_type} gets {power_mod:+}/{toughness_mod:+}", condition)
        self.power_mod = power_mod
        self.toughness_mod = toughness_mod
        self.target_type = target_type
        self.duration = duration # 'end_of_turn' or 'permanent' (until source leaves)
        self.requires_target = "target" in target_type # Check if it targets specifically

    def apply(self, game_state, source_id, controller, targets=None):
        """Register the buff with the Layer System."""
        if not hasattr(game_state, 'layer_system') or not game_state.layer_system:
             logging.warning("BuffEffect: LayerSystem not available.")
             return False

        if self.power_mod == 0 and self.toughness_mod == 0: return True # No change

        # Determine affected IDs
        affected_ids = []
        if self.requires_target:
            if targets and "creatures" in targets: affected_ids = targets["creatures"]
            elif targets and "permanents" in targets: affected_ids = targets["permanents"] # Assume can buff non-creatures if type is permanent
            # ... add other target types if needed
        elif self.target_type == "creatures you control":
             affected_ids = [cid for cid in controller.get("battlefield",[]) if game_state._is_creature(cid)]
        elif self.target_type == "all creatures":
             affected_ids.extend(game_state.get_all_creatures(game_state.p1))
             affected_ids.extend(game_state.get_all_creatures(game_state.p2))
        elif self.target_type == "self":
             affected_ids.append(source_id)

        if not affected_ids:
            logging.debug("BuffEffect: No affected targets found.")
            return False # No targets to buff

        # Register with Layer System
        effect_data = {
             'source_id': source_id,
             'layer': 7, 'sublayer': 'c', # Modifiers like +N/+N
             'affected_ids': affected_ids,
             'effect_type': 'modify_pt',
             'effect_value': (self.power_mod, self.toughness_mod),
             'duration': self.duration,
             'controller_id': controller, # Store controller for conditional effects
             'description': self.effect_text
        }
        # Add conditional logic if needed for the effect's activity
        if self.duration == 'until_source_leaves':
             effect_data['condition'] = lambda gs_check: source_id in gs_check.get_card_controller(source_id).get("battlefield", []) if gs_check.get_card_controller(source_id) else False
        elif self.duration == 'permanent': # Static anthem etc. needs source condition
            effect_data['condition'] = lambda gs_check: source_id in gs_check.get_card_controller(source_id).get("battlefield", []) if gs_check.get_card_controller(source_id) else False

        effect_id = game_state.layer_system.register_effect(effect_data)
        if effect_id:
            logging.debug(f"Registered Buff effect {effect_id} ({self.power_mod:+}/{self.toughness_mod:+}) from {source_id} duration {self.duration}")
            return True
        else:
            logging.warning(f"Failed to register Buff effect from {source_id}")
            return False

    def _apply_effect(self, game_state, source_id, controller, targets):
        # This effect works by registering with LayerSystem during the 'apply' phase,
        # so this direct application method shouldn't be called unless it's a one-shot buff
        # which isn't standard. Assume registration handled by apply().
        logging.warning("BuffEffect._apply_effect called directly. Buffs should be registered via LayerSystem.")
        # Re-register for safety?
        return self.apply(game_state, source_id, controller, targets)
    
class DestroyEffect(AbilityEffect):
    """Effect that destroys permanents."""
    def __init__(self, target_type="permanent", condition=None):
        super().__init__(f"Destroy target {target_type}", condition)
        self.target_type = target_type.lower() # e.g., "creature", "artifact", "nonland permanent", "all creatures"
        self.requires_target = "target" in target_type


    def _apply_effect(self, game_state, source_id, controller, targets):
        targets_to_destroy = []
        # --- Target Collection ---
        if "all " in self.target_type: # Handle board wipes
            wipe_type = self.target_type.split("all ")[1].replace('s','') # 'creature', 'permanent' etc.
            for p in [game_state.p1, game_state.p2]:
                for card_id in list(p.get("battlefield",[])): # Iterate copy
                     card = game_state._safe_get_card(card_id)
                     if card:
                          # Check if card matches type to wipe
                          matches = False
                          if wipe_type == "permanent": matches = True
                          elif wipe_type == "creature" and 'creature' in getattr(card, 'card_types', []): matches = True
                          elif wipe_type == "artifact" and 'artifact' in getattr(card, 'card_types', []): matches = True
                          # Add more wipe types
                          if matches: targets_to_destroy.append((card_id, p))
        elif self.requires_target:
            # Get target IDs from resolved targets dictionary
            cats = []
            if self.target_type == "creature": cats = ["creatures"]
            elif self.target_type == "artifact": cats = ["artifacts"]
            elif self.target_type == "enchantment": cats = ["enchantments"]
            elif self.target_type == "land": cats = ["lands"]
            elif self.target_type == "planeswalker": cats = ["planeswalkers"]
            elif self.target_type == "permanent": cats = ["creatures", "artifacts", "enchantments", "lands", "planeswalkers", "battles", "permanents"]
            elif self.target_type == "nonland permanent": cats = ["creatures", "artifacts", "enchantments", "planeswalkers", "battles", "permanents"]

            ids_found = []
            if targets:
                for cat in cats:
                    ids_found.extend(targets.get(cat, []))
            # Filter nonland if necessary
            if self.target_type == "nonland permanent":
                 ids_found = [tid for tid in ids_found if 'land' not in getattr(game_state._safe_get_card(tid),'card_types',[])]

            for target_id in set(ids_found): # Process unique targets
                 target_owner, target_zone = game_state.find_card_location(target_id)
                 if target_owner and target_zone == 'battlefield':
                     targets_to_destroy.append((target_id, target_owner))
        else: # Should not happen if requires_target is set correctly
            logging.warning(f"DestroyEffect has requires_target={self.requires_target} but no targets resolved.")
            return False

        if not targets_to_destroy: return False

        # --- Destruction ---
        destroyed_count = 0
        for card_id, owner in targets_to_destroy:
            card = game_state._safe_get_card(card_id)
            if not card: continue

            # 1. Check Indestructible
            if game_state.check_keyword(card_id, "indestructible"):
                 logging.debug(f"Cannot destroy {card.name}: Indestructible.")
                 continue

            # 2. Check Regeneration/Replacement Effects
            can_be_destroyed = True
            # Regeneration
            if game_state.apply_regeneration(card_id, owner):
                logging.debug(f"DestroyEffect: {card.name} regenerated.")
                can_be_destroyed = False
            # Totem Armor
            elif hasattr(game_state, 'apply_totem_armor') and game_state.apply_totem_armor(card_id, owner):
                 logging.debug(f"DestroyEffect: {card.name} saved by Totem Armor.")
                 can_be_destroyed = False
            # Other Replacements
            elif hasattr(game_state, 'replacement_effects'):
                 destroy_context = {'card_id': card_id, 'controller': owner, 'cause': 'destroy_effect', 'source_id': source_id}
                 modified_context, replaced = game_state.replacement_effects.apply_replacements("DESTROYED", destroy_context)
                 if replaced:
                      final_dest = modified_context.get('to_zone')
                      if final_dest and final_dest != "battlefield":
                          game_state.move_card(card_id, owner, "battlefield", owner, final_dest, cause="destroy_replaced")
                      # Else prevented
                      can_be_destroyed = False

            # 3. Perform Destruction (Move to Graveyard)
            if can_be_destroyed:
                if game_state.move_card(card_id, owner, "battlefield", owner, "graveyard", cause="destroy_effect", context={"source_id": source_id}):
                    destroyed_count += 1
                    # Logging handled by move_card

        # SBAs handled by main loop
        return destroyed_count > 0
    
class ExileEffect(AbilityEffect):
    """Effect that exiles permanents or cards from zones."""
    def __init__(self, target_type="permanent", zone="battlefield", condition=None):
        super().__init__(f"Exile target {target_type}" + (f" from {zone}" if zone != "battlefield" else ""), condition)
        self.target_type = target_type.lower()
        self.zone = zone.lower() # graveyard, hand, library, battlefield, stack
        self.requires_target = True

    def _apply_effect(self, game_state, source_id, controller, targets):
        targets_to_exile = []
        cats = []
        if self.target_type == "creature": cats = ["creatures"]
        elif self.target_type == "artifact": cats = ["artifacts"]
        # Add more specific types
        elif self.target_type == "permanent": cats = ["creatures", "artifacts", "enchantments", "lands", "planeswalkers", "battles", "permanents"]
        elif self.target_type == "card": cats = ["cards"] # GY/Exile/Hand/Lib targets
        elif self.target_type == "spell": cats = ["spells"] # Stack targets
        else: cats.append(self.target_type+"s") # Basic plural

        ids_found = []
        if targets:
            for cat in cats:
                 ids_found.extend(targets.get(cat, []))

        for target_id in set(ids_found):
            target_owner, target_zone = game_state.find_card_location(target_id)
            # Validate source zone specified in constructor matches current zone
            if self.zone == 'any' or target_zone == self.zone:
                 if target_owner: # Ensure target found
                     targets_to_exile.append((target_id, target_owner, target_zone))
            elif target_zone: # Found, but wrong zone
                 logging.debug(f"Exile target {target_id} found in {target_zone}, expected {self.zone}. Skipping.")

        if not targets_to_exile: return False

        exiled_count = 0
        for card_id, owner, current_zone in targets_to_exile:
             # Use move_card to handle replacements (e.g., "If would be exiled, put in GY instead")
             # Also handles triggers for leaving zone/entering exile
             if game_state.move_card(card_id, owner, current_zone, owner, "exile", cause="exile_effect", context={"source_id": source_id}):
                  exiled_count += 1
                  # Logging handled by move_card

        return exiled_count > 0