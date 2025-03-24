"""Utility functions for ability processing."""
import logging
import re
from .ability_types import AbilityEffect, DrawCardEffect, GainLifeEffect, DamageEffect, \
    CounterSpellEffect, CreateTokenEffect, DestroyEffect, ExileEffect, \
    DiscardEffect, MillEffect

def is_beneficial_effect(effect_text):
    """
    Determine if an effect text describes an effect that is beneficial to its target.
    
    Args:
        effect_text: The text of the effect to analyze
        
    Returns:
        bool: True if the effect is likely beneficial to the target, False otherwise
    """
    # Convert to lowercase for case-insensitive matching
    effect_text = effect_text.lower() if effect_text else ""
    
    # Effects that are usually harmful
    harmful_terms = [
        "destroy", "exile", "sacrifice", "damage", "lose", "-1/-1", 
        "dies", "discard", "return to", "counter", "tap", "doesn't untap",
        "can't attack", "can't block", "can't cast", "skip", "remove"
    ]
    
    # Effects that are usually beneficial
    beneficial_terms = [
        "gain", "draw", "put", "+1/+1", "create", "search", "add", 
        "untap", "hexproof", "indestructible", "protection", "return", 
        "prevent", "regenerate", "restore", "double", "copy", "trample"
    ]
    
    # Context-aware special cases
    # For "return" effects, check if it's a bounce (harmful) or recursion (beneficial)
    if "return" in effect_text:
        if "return target" in effect_text and "to owner's hand" in effect_text:
            return False  # Bounce effect - harmful
        if "return target" in effect_text and "from your graveyard" in effect_text:
            return True   # Recursion effect - beneficial
    
    # For damage effects
    if "damage" in effect_text:
        if "prevent" in effect_text or "prevented" in effect_text:
            return True  # Preventing damage is beneficial
        if "deals damage to you" in effect_text:
            return False  # Damage to you is harmful
    
    # Check for harmful terms
    for term in harmful_terms:
        if term in effect_text:
            return False
    
    # Check for beneficial terms
    for term in beneficial_terms:
        if term in effect_text:
            return True
    
    # Default to neutral/harmful
    return False

def text_to_number(text):
    """Convert text number (e.g., 'three') to integer."""
    text_to_num = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10
    }
    
    if text.lower() in text_to_num:
        return text_to_num[text.lower()]
    
    try:
        return int(text)
    except ValueError:
        return 1  # Default to 1 if conversion fails

def resolve_simple_targeting(game_state, card_id, controller, effect_text):
    """Simplified targeting resolution when targeting system isn't available"""
    targets = {"creatures": [], "players": [], "spells": [], "lands": [], 
            "artifacts": [], "enchantments": [], "permanents": []}
    opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
    
    # Target creature
    if "target creature" in effect_text:
        for player in [opponent, controller]:  # Prioritize opponent targets
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    # Check restrictions
                    if "you control" in effect_text and player != controller:
                        continue
                    if "opponent controls" in effect_text and player == controller:
                        continue
                    targets["creatures"].append(card_id)
                    break  # Just take the first valid target
            if targets["creatures"]:
                break
    
    # Target player
    if "target player" in effect_text or "target opponent" in effect_text:
        if "target opponent" in effect_text:
            targets["players"].append("p2" if controller == game_state.p1 else "p1")
        else:
            # Default to targeting opponent
            targets["players"].append("p2" if controller == game_state.p1 else "p1")
    
    # Target land
    if "target land" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'type_line') and 'land' in card.type_line.lower():
                    targets["lands"].append(card_id)
                    break  # Just take the first valid target
            if targets["lands"]:
                break
    
    # Target artifact
    if "target artifact" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'artifact' in card.card_types:
                    targets["artifacts"].append(card_id)
                    break  # Just take the first valid target
            if targets["artifacts"]:
                break
    
    # Target enchantment
    if "target enchantment" in effect_text:
        for player in [opponent, controller]:
            for card_id in player["battlefield"]:
                card = game_state._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'enchantment' in card.card_types:
                    targets["enchantments"].append(card_id)
                    break  # Just take the first valid target
            if targets["enchantments"]:
                break
    
    # Target permanent (any permanent type)
    if "target permanent" in effect_text:
        for player in [opponent, controller]:
            if player["battlefield"]:
                targets["permanents"].append(player["battlefield"][0])  # Just take the first one
                break
    
    # Target spell (on the stack)
    if "target spell" in effect_text:
        for item in reversed(list(game_state.stack)):  # Start from top of stack
            if isinstance(item, tuple) and len(item) >= 3 and item[0] == "SPELL":
                spell_id = item[1]
                spell_card = game_state._safe_get_card(spell_id)
                
                if not spell_card:
                    continue
                    
                # Check for spell type restrictions
                if "creature spell" in effect_text and (not hasattr(spell_card, 'card_types') or 
                                                    'creature' not in spell_card.card_types):
                    continue
                elif "noncreature spell" in effect_text and (hasattr(spell_card, 'card_types') and 
                                                        'creature' in spell_card.card_types):
                    continue
                
                targets["spells"].append(spell_id)
                break
    
    return targets

def resolve_targeting(game_state, card_id, controller, effect_text):
    """Enhanced targeting resolution that uses TargetingSystem if available, otherwise falls back to simple targeting."""
    # Try to use TargetingSystem if available
    if hasattr(game_state, 'ability_handler') and hasattr(game_state.ability_handler, 'targeting_system'):
        return game_state.ability_handler.targeting_system.resolve_targeting_for_ability(
            card_id, effect_text, controller)
    
    # Fall back to simple targeting
    return resolve_simple_targeting(game_state, card_id, controller, effect_text)

class EffectFactory:
    """Factory class to create appropriate AbilityEffect objects based on effect text."""
    
    # Effect pattern definitions
    EFFECT_PATTERNS = {
        "draw": {
            "pattern": r"draw (\d+|\w+) cards?",
            "factory": lambda count, **kwargs: DrawCardEffect(text_to_number(count), **kwargs)
        },
        "gain_life": {
            "pattern": r"gain (\d+|\w+) life",
            "factory": lambda amount, **kwargs: GainLifeEffect(text_to_number(amount), **kwargs)
        },
        "damage": {
            "pattern": r"deal[s]? (\d+|\w+) damage",
            "factory": lambda amount, **kwargs: DamageEffect(text_to_number(amount), **kwargs)
        },
        "counter": {
            "pattern": r"counter target (.*?)(?=\.|$)",
            "factory": lambda target_type, **kwargs: CounterSpellEffect(target_type, **kwargs)
        },
        "token": {
            "pattern": r"create (\w+|a|an) (.*?) tokens?",
            "factory": lambda count, desc, **kwargs: CreateTokenEffect(
                power=kwargs.get('power', 1),
                toughness=kwargs.get('toughness', 1),
                creature_type=desc,
                count=text_to_number(count) if count not in ('a', 'an') else 1,
                keywords=kwargs.get('keywords', [])
            )
        },
        "destroy": {
            "pattern": r"destroy target (.*?)(?=\.|$)",
            "factory": lambda target_type, **kwargs: DestroyEffect(target_type, **kwargs)
        },
        "exile": {
            "pattern": r"exile target (.*?)(?=\.|$)",
            "factory": lambda target_type, **kwargs: ExileEffect(target_type, **kwargs)
        },
        "discard": {
            "pattern": r"discard (\w+|a|an) (.*?)(?=\.|$)",
            "factory": lambda count, desc=None, **kwargs: DiscardEffect(
                count=text_to_number(count) if count not in ('a', 'an') else 1,
                **kwargs
            )
        },
        "mill": {
            "pattern": r"(put|mill) (?:the )?top (\d+|\w+) cards?",
            "factory": lambda action, count, **kwargs: MillEffect(text_to_number(count), **kwargs)
        }
    }
    
    @staticmethod
    def create_effects(effect_text, targets=None):
        """Create appropriate AbilityEffect objects based on the effect text."""
        effects = []
        
        # Lower-case for easier comparison
        effect_text_lower = effect_text.lower() if effect_text else ""
        
        # Try each pattern to find matching effects
        for effect_type, effect_info in EffectFactory.EFFECT_PATTERNS.items():
            pattern = effect_info["pattern"]
            factory = effect_info["factory"]
            
            matches = re.finditer(pattern, effect_text_lower)
            for match in matches:
                try:
                    # Extract parameters from match
                    params = match.groups()
                    
                    # Create effect with correct parameters
                    if len(params) == 1:
                        effect = factory(params[0], target=EffectFactory._detect_target(effect_text_lower))
                    elif len(params) == 2:
                        effect = factory(params[0], params[1], target=EffectFactory._detect_target(effect_text_lower))
                    else:
                        # For complex patterns, pass all groups as kwargs
                        kwargs = {f"param{i}": param for i, param in enumerate(params)}
                        kwargs["target"] = EffectFactory._detect_target(effect_text_lower)
                        effect = factory(**kwargs)
                    
                    effects.append(effect)
                except Exception as e:
                    logging.error(f"Error creating effect for pattern '{pattern}': {str(e)}")
        
        # If no effects were created, return generic effect
        if not effects:
            logging.warning(f"Could not parse effect text: '{effect_text}'")
            return [AbilityEffect(effect_text)]
        
        return effects
    
    @staticmethod
    def _detect_target(effect_text):
        """Detect the target of an effect based on text analysis."""
        if "target opponent" in effect_text or "each opponent" in effect_text:
            return "opponent"
        elif "target player" in effect_text:
            return "target_player"
        else:
            return "controller"  # Default