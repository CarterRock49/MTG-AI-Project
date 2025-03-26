import logging
import re
from .card import Card

class KeywordEffects:
    """Class containing implementations of Magic keyword effects"""
    
    def __init__(self, game_state=None):
        self.game_state = game_state
        
    def _apply_demonstrate(self, card_id, event_type, context=None):
        """Apply demonstrate ability effects (copy spell when cast and choose new targets)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "demonstrate" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Create a copy of the spell on the stack
            gs.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
            logging.debug(f"Demonstrate: Created a copy of {card.name}")
            
        return True

    def _apply_living_weapon(self, card_id, event_type, context=None):
        """Apply living weapon ability effects (create a Germ token and attach equipment)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "living weapon" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Create a 0/0 black Germ token
            token_data = {
                "name": "Germ Token",
                "type_line": "creature — germ",
                "card_types": ["creature"],
                "subtypes": ["germ"],
                "power": 0,
                "toughness": 0,
                "oracle_text": "",
                "keywords": [0] * 11,
                "colors": [0, 0, 1, 0, 0]  # Black color
            }
            
            token_id = gs.create_token(controller, token_data)
            
            # Attach the equipment to the token
            if token_id:
                if not hasattr(controller, "attachments"):
                    controller["attachments"] = {}
                    
                controller["attachments"][card_id] = token_id
                logging.debug(f"Living Weapon: Created a 0/0 black Germ token and attached {card.name} to it")
            
        return True

    def _apply_melee(self, card_id, event_type, context=None):
        """Apply melee ability effects (Get +1/+1 for each opponent attacked)"""
        gs = self.game_state
        
        if event_type == "ATTACK_DECLARES":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "melee" not in card.oracle_text.lower():
                return True
                
            # Check if this creature is attacking
            if card_id not in gs.current_attackers:
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # In real MTG, melee depends on how many different opponents you attacked
            # For our single-opponent format, give +1/+1 if any attack was declared
            if gs.current_attackers:
                # Add temporary buff
                if not hasattr(controller, "temp_buffs"):
                    controller["temp_buffs"] = {}
                    
                if card_id not in controller["temp_buffs"]:
                    controller["temp_buffs"][card_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                    
                controller["temp_buffs"][card_id]["power"] += 1
                controller["temp_buffs"][card_id]["toughness"] += 1
                
                logging.debug(f"Melee: {card.name} got +1/+1 until end of turn")
                
        return True

    def _apply_miracle(self, card_id, event_type, context=None):
        """Apply miracle ability effects (Cast for alternate cost if it's the first card drawn)"""
        gs = self.game_state
        
        if event_type == "DRAW_CARD" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "miracle" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if this is the first card drawn this turn
            is_first_draw = False
            if not hasattr(gs, 'cards_drawn_this_turn'):
                gs.cards_drawn_this_turn = {}
                
            player_key = "p1" if controller == gs.p1 else "p2"
            if player_key not in gs.cards_drawn_this_turn:
                gs.cards_drawn_this_turn[player_key] = 0
                
            if gs.cards_drawn_this_turn[player_key] == 0:
                is_first_draw = True
                
            gs.cards_drawn_this_turn[player_key] += 1
            
            if is_first_draw:
                # In a real game, player would choose whether to reveal/cast
                # For our simulation, always choose to cast if affordable
                
                # Parse miracle cost
                match = re.search(r"miracle [^\(]([^\)]+)", card.oracle_text.lower())
                miracle_cost = match.group(1) if match else None
                
                if miracle_cost and hasattr(gs, 'mana_system'):
                    miracle_parsed = gs.mana_system.parse_mana_cost(miracle_cost)
                    
                    if gs.mana_system.can_pay_mana_cost(controller, miracle_parsed):
                        # Pay the miracle cost
                        gs.mana_system.pay_mana_cost(controller, miracle_parsed)
                        
                        # Cast the card
                        controller["hand"].remove(card_id)
                        gs.stack.append(("SPELL", card_id, controller, {"miracle": True}))
                        
                        logging.debug(f"Miracle: Cast {card.name} for its miracle cost")
                
        return True

    def _apply_offering(self, card_id, event_type, context=None):
        """Apply offering ability effects (sacrifice a creature to reduce cost)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("offering"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Find offering type (e.g., "Goblin offering", "Elf offering")
            offering_match = re.search(r"(\w+) offering", card.oracle_text.lower())
            offering_type = offering_match.group(1) if offering_match else None
            
            if not offering_type:
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Find creature of the specified type to sacrifice
            sacrifice_candidates = []
            for creature_id in controller["battlefield"]:
                creature = gs._safe_get_card(creature_id)
                if not creature or not hasattr(creature, 'card_types') or 'creature' not in creature.card_types:
                    continue
                    
                # Check if creature matches offering type
                if (hasattr(creature, 'subtypes') and offering_type.lower() in 
                    [subtype.lower() for subtype in creature.subtypes]):
                    sacrifice_candidates.append((creature_id, creature.cmc if hasattr(creature, 'cmc') else 0))
            
            if not sacrifice_candidates:
                return True
                s
            # Choose the highest CMC creature to maximize discount
            sacrifice_candidates.sort(key=lambda x: x[1], reverse=True)
            sacrifice_id, sacrifice_cmc = sacrifice_candidates[0]
            
            # Sacrifice the creature
            gs.move_card(sacrifice_id, controller, "battlefield", controller, "graveyard")
            
            # Reduce cost by CMC of sacrificed creature
            if "mana_cost" in context and sacrifice_cmc > 0:
                # Apply discount to generic mana cost
                if "generic" in context["mana_cost"]:
                    context["mana_cost"]["generic"] = max(0, context["mana_cost"]["generic"] - sacrifice_cmc)
                    
                logging.debug(f"Offering: Sacrificed {gs._safe_get_card(sacrifice_id).name} to reduce cost by {sacrifice_cmc}")
                
        return True

    def _apply_outlast(self, card_id, event_type, context=None):
        """Apply outlast ability effects (pay, tap: put a +1/+1 counter)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("outlast"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse outlast cost
            match = re.search(r"outlast [^\(]([^\)]+)", card.oracle_text.lower())
            outlast_cost = match.group(1) if match else None
            
            if not outlast_cost or not hasattr(gs, 'mana_system'):
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if card is already tapped
            if card_id in controller.get("tapped_permanents", set()):
                return True
                
            # Check if controller can pay
            outlast_parsed = gs.mana_system.parse_mana_cost(outlast_cost)
            
            if gs.mana_system.can_pay_mana_cost(controller, outlast_parsed):
                # Pay the cost
                gs.mana_system.pay_mana_cost(controller, outlast_parsed)
                
                # Tap the creature
                if "tapped_permanents" not in controller:
                    controller["tapped_permanents"] = set()
                    
                controller["tapped_permanents"].add(card_id)
                
                # Add a +1/+1 counter
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + 1
                
                # Update power/toughness
                if hasattr(card, 'power'):
                    card.power += 1
                if hasattr(card, 'toughness'):
                    card.toughness += 1
                    
                logging.debug(f"Outlast: Added a +1/+1 counter to {card.name}")
                
        return True

    def _apply_overload(self, card_id, event_type, context=None):
        """Apply overload ability effects (replace 'target' with 'each')"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("overload"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse overload cost
            match = re.search(r"overload [^\(]([^\)]+)", card.oracle_text.lower())
            overload_cost = match.group(1) if match else None
            
            if overload_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with overload cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(overload_cost)
                
                # Mark that this spell is being overloaded
                context["overloaded"] = True
                
                logging.debug(f"Overload: Using alternative cost for {card.name}")
                
        elif event_type == "SPELL_RESOLVES" and context and context.get("overloaded"):
            # When resolving, the spell affects all possible targets
            # This would be implemented in the spell resolution logic
            logging.debug(f"Overload: Spell affects each possible target instead of just one")
            
        return True

    def _apply_partner(self, card_id, event_type, context=None):
        """Apply partner ability effects (allows two commanders in Commander format)"""
        # This is more of a deck building rule for Commander format
        # Not much gameplay effect during a match
        return True

    def _apply_poisonous(self, card_id, event_type, context=None):
        """Apply poisonous ability effects (give poison counters on combat damage)"""
        gs = self.game_state
        
        if event_type == "DEALS_COMBAT_DAMAGE_TO_PLAYER" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse poisonous value
            match = re.search(r"poisonous (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            poisonous_value = int(match.group(1))
            damaged_player = context.get("damaged_player")
            
            if damaged_player:
                # Add poison counters
                damaged_player["poison_counters"] = damaged_player.get("poison_counters", 0) + poisonous_value
                
                logging.debug(f"Poisonous: {card.name} gave {poisonous_value} poison counters")
                
                # Check for poison win condition (10 or more poison counters)
                if damaged_player["poison_counters"] >= 10:
                    # In a real game, this would trigger a win for the opponent
                    logging.debug(f"Player has {damaged_player['poison_counters']} poison counters (10+ is lethal)")
                    
        return True

    def _apply_provoke(self, card_id, event_type, context=None):
        """Apply provoke ability effects (force a creature to block)"""
        gs = self.game_state
        
        if event_type == "DECLARES_ATTACK" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "provoke" not in card.oracle_text.lower():
                return True
                
            # Check if this creature is attacking
            if card_id not in gs.current_attackers:
                return True
                
            # Find controller and opponent
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            # Find a creature to provoke
            provoke_candidates = []
            for creature_id in opponent["battlefield"]:
                creature = gs._safe_get_card(creature_id)
                if not creature or not hasattr(creature, 'card_types') or 'creature' not in creature.card_types:
                    continue
                    
                # Check if creature is untapped
                if creature_id not in opponent.get("tapped_permanents", set()):
                    provoke_candidates.append(creature_id)
                    
            if provoke_candidates:
                # Choose a creature to provoke (in a real game, player would choose)
                provoke_id = provoke_candidates[0]
                
                # Force to block if possible
                if not hasattr(gs, 'forced_blocks'):
                    gs.forced_blocks = {}
                    
                gs.forced_blocks[provoke_id] = card_id
                
                logging.debug(f"Provoke: {card.name} forces {gs._safe_get_card(provoke_id).name} to block if able")
                
        return True

    def _apply_rampage(self, card_id, event_type, context=None):
        """Apply rampage ability effects (boost when blocked by multiple creatures)"""
        gs = self.game_state
        
        if event_type == "BLOCKED" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse rampage value
            match = re.search(r"rampage (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            rampage_value = int(match.group(1))
            blockers = context.get("blockers", [])
            
            # Rampage triggers for each blocker beyond the first
            if len(blockers) > 1:
                extra_blockers = len(blockers) - 1
                
                # Find controller
                controller = None
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
                if controller:
                    # Apply rampage bonus
                    bonus = extra_blockers * rampage_value
                    
                    # Add temporary buff
                    if not hasattr(controller, "temp_buffs"):
                        controller["temp_buffs"] = {}
                        
                    if card_id not in controller["temp_buffs"]:
                        controller["temp_buffs"][card_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                        
                    controller["temp_buffs"][card_id]["power"] += bonus
                    controller["temp_buffs"][card_id]["toughness"] += bonus
                    
                    logging.debug(f"Rampage {rampage_value}: {card.name} gets +{bonus}/+{bonus} from {extra_blockers} extra blockers")
                    
        return True

    def _apply_rebound(self, card_id, event_type, context=None):
        """Apply rebound ability effects (cast again next turn)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "rebound" not in card.oracle_text.lower():
                return True
                
            # Mark that this spell has rebound
            context["has_rebound"] = True
            
        elif event_type == "SPELL_RESOLVES" and context and context.get("has_rebound"):
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if spell was cast from hand
            if context.get("cast_from_hand", True):
                # Move to exile instead of graveyard
                if not hasattr(gs, 'rebounded_cards'):
                    gs.rebounded_cards = {}
                    
                gs.rebounded_cards[card_id] = {
                    "controller": controller,
                    "turn": gs.turn + 1  # Cast on next turn
                }
                
                # Prevent moving to graveyard
                context["skip_default_movement"] = True
                
                # Move card to exile
                controller["exile"].append(card_id)
                
                logging.debug(f"Rebound: {gs._safe_get_card(card_id).name} exiled to cast next turn")
                
        elif event_type == "UPKEEP" and hasattr(gs, 'rebounded_cards'):
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check for cards to rebound this turn
            for rebounded_id, rebound_info in list(gs.rebounded_cards.items()):
                if rebound_info["controller"] == controller and rebound_info["turn"] == gs.turn:
                    # Cast the card again for free
                    if rebounded_id in controller["exile"]:
                        controller["exile"].remove(rebounded_id)
                        gs.stack.append(("SPELL", rebounded_id, controller, {"cast_for_free": True}))
                        
                        # Remove from rebounded tracking
                        del gs.rebounded_cards[rebounded_id]
                        
                        logging.debug(f"Rebound: Cast {gs._safe_get_card(rebounded_id).name} for free from exile")
                        
        return True

    def _apply_reconfigure(self, card_id, event_type, context=None):
        """Apply reconfigure ability effects (Equipment creature can attach to others)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("reconfigure"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "reconfigure" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            target_id = context.get("target_id")
            
            if not controller or not target_id:
                return True
                
            # Parse reconfigure cost
            match = re.search(r"reconfigure [^\(]([^\)]+)", card.oracle_text.lower())
            reconfigure_cost = match.group(1) if match else None
            
            if not reconfigure_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay cost
            reconfigure_parsed = gs.mana_system.parse_mana_cost(reconfigure_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, reconfigure_parsed):
                return True
                
            # Check if target is valid (a creature)
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types:
                return True
                
            # Check if target is controlled by the same player
            if target_id not in controller["battlefield"]:
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, reconfigure_parsed)
            
            # Initialize attachments if needed
            if not hasattr(controller, "attachments"):
                controller["attachments"] = {}
                
            # Check current state
            currently_attached = False
            for equip_id, equipped_id in controller["attachments"].items():
                if equip_id == card_id:
                    currently_attached = True
                    break
                    
            if currently_attached:
                # Unattach and become a creature
                del controller["attachments"][card_id]
                logging.debug(f"Reconfigure: {card.name} unattached and became a creature")
            else:
                # Attach to target creature
                controller["attachments"][card_id] = target_id
                logging.debug(f"Reconfigure: {card.name} attached to {target_card.name}")
                
        return True

    def _apply_recover(self, card_id, event_type, context=None):
        """Apply recover ability effects (return from graveyard when a creature dies)"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            dying_card_id = context.get("card_id", card_id)
            controller = context.get("controller")
            
            if not controller:
                return True
                
            # Check if the dying card is a creature
            dying_card = gs._safe_get_card(dying_card_id)
            if not dying_card or not hasattr(dying_card, 'card_types') or 'creature' not in dying_card.card_types:
                return True
                
            # Look for cards with recover in the graveyard
            for recover_id in controller["graveyard"]:
                recover_card = gs._safe_get_card(recover_id)
                if not recover_card or not hasattr(recover_card, 'oracle_text') or "recover" not in recover_card.oracle_text.lower():
                    continue
                    
                # Parse recover cost
                match = re.search(r"recover [^\(]([^\)]+)", recover_card.oracle_text.lower())
                recover_cost = match.group(1) if match else None
                
                if not recover_cost or not hasattr(gs, 'mana_system'):
                    continue
                    
                # Check if controller can pay
                recover_parsed = gs.mana_system.parse_mana_cost(recover_cost)
                
                if gs.mana_system.can_pay_mana_cost(controller, recover_parsed):
                    # In a real game, the player would choose whether to pay
                    # For our simulation, always choose to pay if possible
                    
                    # Pay the cost
                    gs.mana_system.pay_mana_cost(controller, recover_parsed)
                    
                    # Return card to hand
                    gs.move_card(recover_id, controller, "graveyard", controller, "hand")
                    
                    logging.debug(f"Recover: Returned {recover_card.name} to hand")
                    break  # Only recover one card per creature death
                    
        return True

    def _apply_reinforce(self, card_id, event_type, context=None):
        """Apply reinforce ability effects (discard to add +1/+1 counters)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("reinforce"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse reinforce value and cost
            match = re.search(r"reinforce (\d+)—([^\(]+)", card.oracle_text.lower())
            if not match:
                return True
                
            reinforce_count = int(match.group(1))
            reinforce_cost = match.group(2).strip()
            
            controller = context.get("controller")
            target_id = context.get("target_id")
            
            if not controller or not target_id:
                return True
                
            # Check if card is in hand
            if card_id not in controller["hand"]:
                return True
                
            # Check if target is a valid creature
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types:
                return True
                
            # Check if controller can pay
            if hasattr(gs, 'mana_system'):
                reinforce_parsed = gs.mana_system.parse_mana_cost(reinforce_cost)
                
                if not gs.mana_system.can_pay_mana_cost(controller, reinforce_parsed):
                    return True
                    
                # Pay the cost
                gs.mana_system.pay_mana_cost(controller, reinforce_parsed)
                
                # Discard the card
                controller["hand"].remove(card_id)
                controller["graveyard"].append(card_id)
                
                # Add counters to target
                if not hasattr(target_card, "counters"):
                    target_card.counters = {}
                    
                target_card.counters["+1/+1"] = target_card.counters.get("+1/+1", 0) + reinforce_count
                
                # Update power/toughness
                if hasattr(target_card, 'power'):
                    target_card.power += reinforce_count
                if hasattr(target_card, 'toughness'):
                    target_card.toughness += reinforce_count
                    
                logging.debug(f"Reinforce: Added {reinforce_count} +1/+1 counters to {target_card.name}")
                
        return True

    def _apply_renown(self, card_id, event_type, context=None):
        """Apply renown ability effects (put +1/+1 counter when deals combat damage)"""
        gs = self.game_state
        
        if event_type == "DEALS_COMBAT_DAMAGE_TO_PLAYER" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse renown value
            match = re.search(r"renown (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            renown_value = int(match.group(1))
            
            # Check if already renowned
            if hasattr(card, "renowned") and card.renowned:
                return True
                
            # Make the creature renowned
            card.renowned = True
            
            # Add +1/+1 counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + renown_value
            
            # Update power/toughness
            if hasattr(card, 'power'):
                card.power += renown_value
            if hasattr(card, 'toughness'):
                card.toughness += renown_value
                
            logging.debug(f"Renown: {card.name} became renowned and got {renown_value} +1/+1 counters")
                
        return True

    def _apply_replicate(self, card_id, event_type, context=None):
        """Apply replicate ability effects (pay cost multiple times to copy spell)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("replicate_count"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "replicate" not in card.oracle_text.lower():
                return True
                
            # Get replicate count (how many times we're replicating)
            replicate_count = context.get("replicate_count", 0)
            
            if replicate_count <= 0:
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Create copies of the spell for each time replicate was paid
            for _ in range(replicate_count):
                gs.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
                
            logging.debug(f"Replicate: Created {replicate_count} copies of {card.name}")
                
        return True

    def _apply_retrace(self, card_id, event_type, context=None):
        """Apply retrace ability effects (cast from graveyard by discarding a land)"""
        gs = self.game_state
        
        if event_type == "CAST_FROM_GRAVEYARD" and context and context.get("retrace"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "retrace" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            # Find a land card in hand to discard
            land_in_hand = None
            for hand_id in controller["hand"]:
                hand_card = gs._safe_get_card(hand_id)
                if hand_card and hasattr(hand_card, 'card_types') and 'land' in hand_card.card_types:
                    land_in_hand = hand_id
                    break
                    
            if not land_in_hand:
                logging.debug(f"Retrace: No land in hand to discard")
                return True
                
            # Discard the land
            controller["hand"].remove(land_in_hand)
            controller["graveyard"].append(land_in_hand)
            
            # Move spell from graveyard to stack
            controller["graveyard"].remove(card_id)
            gs.stack.append(("SPELL", card_id, controller, {"retrace": True}))
            
            logging.debug(f"Retrace: Cast {card.name} from graveyard by discarding a land")
                
        return True

    def _apply_ripple(self, card_id, event_type, context=None):
        """Apply ripple ability effects (reveal cards to cast free copies)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse ripple value
            match = re.search(r"ripple (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            ripple_value = int(match.group(1))
            
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if there are enough cards in library
            if len(controller["library"]) < ripple_value:
                return True
                
            # Look at top cards
            revealed_cards = controller["library"][:ripple_value]
            
            # Find cards with the same name
            same_name_cards = []
            for revealed_id in revealed_cards:
                revealed_card = gs._safe_get_card(revealed_id)
                if revealed_card and hasattr(revealed_card, 'name') and revealed_card.name == card.name:
                    same_name_cards.append(revealed_id)
                    
            # Cast cards with the same name
            for same_id in same_name_cards:
                controller["library"].remove(same_id)
                gs.stack.append(("SPELL", same_id, controller, {"cast_for_free": True}))
                
            # Put the rest on the bottom
            for revealed_id in revealed_cards:
                if revealed_id not in same_name_cards and revealed_id in controller["library"]:
                    controller["library"].remove(revealed_id)
                    controller["library"].append(revealed_id)  # Put at bottom
                    
            logging.debug(f"Ripple {ripple_value}: Cast {len(same_name_cards)} free copies from the top of library")
                
        return True

    def _apply_scavenge(self, card_id, event_type, context=None):
        """Apply scavenge ability effects (exile from graveyard to add +1/+1 counters)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("scavenge"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "scavenge" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            target_id = context.get("target_id")
            
            if not controller or not target_id or card_id not in controller["graveyard"]:
                return True
                
            # Check if target is a valid creature
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types:
                return True
                
            # Parse scavenge cost
            match = re.search(r"scavenge [^\(]([^\)]+)", card.oracle_text.lower())
            scavenge_cost = match.group(1) if match else None
            
            if not scavenge_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            scavenge_parsed = gs.mana_system.parse_mana_cost(scavenge_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, scavenge_parsed):
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, scavenge_parsed)
            
            # Get number of counters (equal to power)
            counter_count = card.power if hasattr(card, 'power') else 0
            
            # Exile the card
            gs.move_card(card_id, controller, "graveyard", controller, "exile")
            
            # Add counters to target
            if counter_count > 0:
                if not hasattr(target_card, "counters"):
                    target_card.counters = {}
                    
                target_card.counters["+1/+1"] = target_card.counters.get("+1/+1", 0) + counter_count
                
                # Update power/toughness
                if hasattr(target_card, 'power'):
                    target_card.power += counter_count
                if hasattr(target_card, 'toughness'):
                    target_card.toughness += counter_count
                    
                logging.debug(f"Scavenge: Exiled {card.name} to put {counter_count} +1/+1 counters on {target_card.name}")
                
        return True

    def _apply_skulk(self, card_id, event_type, context=None):
        """Apply skulk ability effects (can't be blocked by higher power)"""
        gs = self.game_state
        
        if event_type == "BLOCKING" and context:
            attacker_id = context.get("attacker_id")
            blocker_id = context.get("blocker_id")
            
            if attacker_id != card_id:
                return True
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "skulk" not in card.oracle_text.lower():
                return True
                
            # Get attacker's power
            if not hasattr(card, 'power'):
                return True
                
            attacker_power = card.power
            
            # Get blocker's power
            blocker = gs._safe_get_card(blocker_id)
            if not blocker or not hasattr(blocker, 'power'):
                return True
                
            blocker_power = blocker.power
            
            # Check if blocker has greater power
            if blocker_power > attacker_power:
                logging.debug(f"Skulk: {blocker.name} can't block {card.name} (power {blocker_power} > {attacker_power})")
                return False  # Can't block
                
        return True

    def _apply_soulbond(self, card_id, event_type, context=None):
        """Apply soulbond ability effects (pair creatures for benefits)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            has_soulbond = "soulbond" in card.oracle_text.lower()
            
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Initialize soulbond pairs if needed
            if not hasattr(controller, "soulbond_pairs"):
                controller["soulbond_pairs"] = {}
                
            # Check if this card has soulbond or a soulbond creature entered
            if has_soulbond:
                # Look for an unpaired creature
                for creature_id in controller["battlefield"]:
                    if creature_id == card_id:
                        continue
                        
                    creature = gs._safe_get_card(creature_id)
                    if not creature or not hasattr(creature, 'card_types') or 'creature' not in creature.card_types:
                        continue
                        
                    # Check if creature is already paired
                    already_paired = False
                    for pair_a, pair_b in controller["soulbond_pairs"].items():
                        if creature_id == pair_a or creature_id == pair_b:
                            already_paired = True
                            break
                            
                    if not already_paired:
                        # Pair the creatures
                        controller["soulbond_pairs"][card_id] = creature_id
                        logging.debug(f"Soulbond: Paired {card.name} with {creature.name}")
                        break
            else:
                # Check if there's an unpaired soulbond creature
                for creature_id in controller["battlefield"]:
                    if creature_id == card_id:
                        continue
                        
                    creature = gs._safe_get_card(creature_id)
                    if not creature or not hasattr(creature, 'oracle_text') or "soulbond" not in creature.oracle_text.lower():
                        continue
                        
                    # Check if soulbond creature is already paired
                    already_paired = False
                    for pair_a, pair_b in controller["soulbond_pairs"].items():
                        if creature_id == pair_a or creature_id == pair_b:
                            already_paired = True
                            break
                            
                    if not already_paired:
                        # Pair the creatures
                        controller["soulbond_pairs"][creature_id] = card_id
                        logging.debug(f"Soulbond: Paired {creature.name} with {card.name}")
                        break
                        
        elif event_type == "LEAVES_BATTLEFIELD" and context:
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if hasattr(player, "soulbond_pairs"):
                    # Check if card was in a pair
                    pair_to_remove = None
                    for pair_a, pair_b in player["soulbond_pairs"].items():
                        if pair_a == card_id or pair_b == card_id:
                            pair_to_remove = pair_a
                            break
                            
                    if pair_to_remove:
                        del player["soulbond_pairs"][pair_to_remove]
                        controller = player
                        logging.debug(f"Soulbond: Pair broken due to creature leaving battlefield")
                        break
                
        return True

    def _apply_soulshift(self, card_id, event_type, context=None):
        """Apply soulshift ability effects (return Spirit card when dies)"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse soulshift value
            match = re.search(r"soulshift (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            soulshift_value = int(match.group(1))
            
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["graveyard"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Find Spirit cards with mana value <= soulshift value
            valid_spirits = []
            for spirit_id in controller["graveyard"]:
                if spirit_id == card_id:
                    continue
                    
                spirit = gs._safe_get_card(spirit_id)
                if not spirit or not hasattr(spirit, 'subtypes') or not hasattr(spirit, 'cmc'):
                    continue
                    
                is_spirit = 'spirit' in [s.lower() for s in spirit.subtypes]
                
                if is_spirit and spirit.cmc <= soulshift_value:
                    valid_spirits.append(spirit_id)
                    
            if valid_spirits:
                # Choose a Spirit (in a real game, player would choose)
                chosen_spirit = valid_spirits[0]
                
                # Return to hand
                gs.move_card(chosen_spirit, controller, "graveyard", controller, "hand")
                
                logging.debug(f"Soulshift: Returned {gs._safe_get_card(chosen_spirit).name} to hand")
                
        return True

    def _apply_spectacle(self, card_id, event_type, context=None):
        """Apply spectacle ability effects (alternative cost if opponent lost life)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("spectacle"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse spectacle cost
            match = re.search(r"spectacle [^\(]([^\)]+)", card.oracle_text.lower())
            spectacle_cost = match.group(1) if match else None
            
            if not spectacle_cost or not hasattr(gs, 'mana_system'):
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if opponent lost life this turn
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            has_lost_life = opponent.get("lost_life_this_turn", False)
            
            if has_lost_life:
                # Replace regular cost with spectacle cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(spectacle_cost)
                logging.debug(f"Spectacle: Using alternative cost for {card.name}")
                
        return True

    def _apply_split_second(self, card_id, event_type, context=None):
        """Apply split second ability effects (can't respond to this spell)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "split second" not in card.oracle_text.lower():
                return True
                
            # Set a flag on the stack that nothing can be cast in response
            if not hasattr(gs, 'split_second_active'):
                gs.split_second_active = False
                
            gs.split_second_active = True
            logging.debug(f"Split Second: {card.name} cannot be responded to")
            
        elif event_type == "SPELL_RESOLVES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "split second" not in card.oracle_text.lower():
                return True
                
            # Clear the split second flag
            if hasattr(gs, 'split_second_active'):
                gs.split_second_active = False
                
        return True

    def _apply_surge(self, card_id, event_type, context=None):
        """Apply surge ability effects (alternative cost if ally cast spell)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("surge"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse surge cost
            match = re.search(r"surge [^\(]([^\)]+)", card.oracle_text.lower())
            surge_cost = match.group(1) if match else None
            
            if not surge_cost or not hasattr(gs, 'mana_system'):
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if condition is met (spell cast earlier this turn)
            can_surge = False
            
            if hasattr(gs, 'spells_cast_this_turn'):
                player_key = "p1" if controller == gs.p1 else "p2"
                spells_cast = gs.spells_cast_this_turn.get(player_key, [])
                can_surge = len(spells_cast) > 0
                
            if can_surge:
                # Replace regular cost with surge cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(surge_cost)
                logging.debug(f"Surge: Using alternative cost for {card.name}")
                
        return True

    def _apply_totem_armor(self, card_id, event_type, context=None):
        """Apply totem armor ability effects (prevents destruction)"""
        gs = self.game_state
        
        if event_type == "DESTROY" and context:
            target_id = context.get("target_id", card_id)
            
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if target_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check if target has any auras with totem armor
            totem_armor_found = False
            
            if hasattr(controller, "attachments"):
                for aura_id, attached_id in controller["attachments"].items():
                    if attached_id != target_id:
                        continue
                        
                    aura = gs._safe_get_card(aura_id)
                    if not aura or not hasattr(aura, 'oracle_text') or "totem armor" not in aura.oracle_text.lower():
                        continue
                        
                    # Found totem armor - destroy aura instead
                    controller["battlefield"].remove(aura_id)
                    controller["graveyard"].append(aura_id)
                    
                    # Remove from attachments
                    del controller["attachments"][aura_id]
                    
                    # Remove any damage from target
                    if hasattr(controller, "damage_counters") and target_id in controller["damage_counters"]:
                        del controller["damage_counters"][target_id]
                        
                    logging.debug(f"Totem Armor: {aura.name} was destroyed instead of {gs._safe_get_card(target_id).name}")
                    
                    totem_armor_found = True
                    break
                    
            if totem_armor_found:
                return False  # Prevent the destruction
                
        return True

    def _apply_transfigure(self, card_id, event_type, context=None):
        """Apply transfigure ability effects (sacrifice to search for creature)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("transfigure"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "transfigure" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["battlefield"]:
                return True
                
            # Parse transfigure cost
            match = re.search(r"transfigure [^\(]([^\)]+)", card.oracle_text.lower())
            transfigure_cost = match.group(1) if match else None
            
            if not transfigure_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            transfigure_parsed = gs.mana_system.parse_mana_cost(transfigure_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, transfigure_parsed):
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, transfigure_parsed)
            
            # Get CMC of the card being sacrificed
            target_cmc = card.cmc if hasattr(card, 'cmc') else 0
            
            # Sacrifice the creature
            gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
            
            # Search for a creature with the same CMC
            found_creatures = []
            for library_id in controller["library"]:
                library_card = gs._safe_get_card(library_id)
                if not library_card or not hasattr(library_card, 'card_types') or not hasattr(library_card, 'cmc'):
                    continue
                    
                if 'creature' in library_card.card_types and library_card.cmc == target_cmc:
                    found_creatures.append(library_id)
                    
            if found_creatures:
                # Choose a creature (in a real game, player would choose)
                chosen_id = found_creatures[0]
                
                # Put chosen creature into hand
                controller["library"].remove(chosen_id)
                controller["hand"].append(chosen_id)
                
                # Shuffle library
                import random
                random.shuffle(controller["library"])
                
                logging.debug(f"Transfigure: Sacrificed {card.name} to find {gs._safe_get_card(chosen_id).name}")
                
        return True

    def _apply_transmute(self, card_id, event_type, context=None):
        """Apply transmute ability effects (discard to search for card with same CMC)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("transmute"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "transmute" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["hand"]:
                return True
                
            # Parse transmute cost
            match = re.search(r"transmute [^\(]([^\)]+)", card.oracle_text.lower())
            transmute_cost = match.group(1) if match else None
            
            if not transmute_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            transmute_parsed = gs.mana_system.parse_mana_cost(transmute_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, transmute_parsed):
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, transmute_parsed)
            
            # Get CMC of the card being discarded
            target_cmc = card.cmc if hasattr(card, 'cmc') else 0
            
            # Discard the card
            controller["hand"].remove(card_id)
            controller["graveyard"].append(card_id)
            
            # Search for a card with the same CMC
            found_cards = []
            for library_id in controller["library"]:
                library_card = gs._safe_get_card(library_id)
                if not library_card or not hasattr(library_card, 'cmc'):
                    continue
                    
                if library_card.cmc == target_cmc:
                    found_cards.append(library_id)
                    
            if found_cards:
                # Choose a card (in a real game, player would choose)
                chosen_id = found_cards[0]
                
                # Put chosen card into hand
                controller["library"].remove(chosen_id)
                controller["hand"].append(chosen_id)
                
                # Shuffle library
                import random
                random.shuffle(controller["library"])
                
                logging.debug(f"Transmute: Discarded {card.name} to find {gs._safe_get_card(chosen_id).name}")
                
        return True

    def _apply_tribute(self, card_id, event_type, context=None):
        """Apply tribute ability effects (opponent chooses counters or ability)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse tribute value
            match = re.search(r"tribute (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            tribute_value = int(match.group(1))
            
            # Find controller and opponent
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            # In a real game, opponent would choose
            # For our simulation, make a simple choice based on board state
            
            # Calculate number of creatures with power >= 2
            big_creatures = sum(1 for cid in controller["battlefield"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                            and 'creature' in gs._safe_get_card(cid).card_types
                            and hasattr(gs._safe_get_card(cid), 'power')
                            and gs._safe_get_card(cid).power >= 3)
            
            # If opponent already faces many threats, avoid giving +1/+1 counters
            give_counters = big_creatures < 2
            
            if give_counters:
                # Add tribute counters
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + tribute_value
                
                # Update power/toughness
                if hasattr(card, 'power'):
                    card.power += tribute_value
                if hasattr(card, 'toughness'):
                    card.toughness += tribute_value
                    
                logging.debug(f"Tribute: Opponent put {tribute_value} +1/+1 counters on {card.name}")
            else:
                # Will get the tribute ability instead
                if not hasattr(card, "tribute_denied"):
                    card.tribute_denied = True
                    
                logging.debug(f"Tribute: Opponent chose not to put counters on {card.name}")
                
                # Trigger the tribute ability
                gs.trigger_ability(card_id, "TRIBUTE_DENIED")
                
        return True

    def _apply_undaunted(self, card_id, event_type, context=None):
        """Apply undaunted ability effects (cost reduction based on opponents)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "undaunted" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Count opponents (in multiplayer, this would be more)
            opponent_count = 1  # In our simulation, always just one opponent
            
            # Apply discount
            if "mana_cost" in context and "generic" in context["mana_cost"]:
                discount = min(opponent_count, context["mana_cost"]["generic"])
                context["mana_cost"]["generic"] -= discount
                
                logging.debug(f"Undaunted: Reduced cost of {card.name} by {discount}")
                
        return True

    def _apply_vanishing(self, card_id, event_type, context=None):
        """Apply vanishing ability effects (remove counter each turn, sacrifice when empty)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse vanishing value
            match = re.search(r"vanishing (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            vanishing_value = int(match.group(1))
            
            # Add time counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            card.counters["time"] = vanishing_value
            logging.debug(f"Vanishing: {card.name} entered with {vanishing_value} time counters")
            
        elif event_type == "UPKEEP" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "vanishing" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Check if it's this player's upkeep
            if context.get("controller") != controller:
                return True
                
            # Check if card has time counters
            if not hasattr(card, "counters") or "time" not in card.counters:
                return True
                
            # Remove a time counter
            card.counters["time"] -= 1
            
            logging.debug(f"Vanishing: Removed a time counter from {card.name}, {card.counters['time']} remaining")
            
            # If no time counters left, sacrifice
            if card.counters["time"] <= 0:
                gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                logging.debug(f"Vanishing: Sacrificed {card.name} with no time counters")
                
        return True

    def _apply_wither(self, card_id, event_type, context=None):
        """Apply wither ability effects (damage as -1/-1 counters)"""
        gs = self.game_state
        
        if event_type == "DEALS_DAMAGE" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "wither" not in card.oracle_text.lower():
                return True
                
            damage_amount = context.get("damage_amount", 0)
            if damage_amount <= 0:
                return True
                
            # Get target of damage
            target_id = context.get("target_id")
            target_is_player = context.get("target_is_player", False)
            
            # Wither only affects creatures
            if target_is_player or not target_id:
                return True
                
            # Get the target creature
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types:
                return True
                
            # Find target controller
            target_controller = None
            for player in [gs.p1, gs.p2]:
                if target_id in player["battlefield"]:
                    target_controller = player
                    break
                    
            if not target_controller:
                return True
                
            # Apply -1/-1 counters instead of damage
            if not hasattr(target_card, "counters"):
                target_card.counters = {}
                
            target_card.counters["-1/-1"] = target_card.counters.get("-1/-1", 0) + damage_amount
            
            # Update power/toughness
            if hasattr(target_card, 'power'):
                target_card.power = max(0, target_card.power - damage_amount)
            if hasattr(target_card, 'toughness'):
                target_card.toughness = max(0, target_card.toughness - damage_amount)
                
            logging.debug(f"Wither: Applied {damage_amount} -1/-1 counters to {target_card.name}")
            
            # Check if creature dies from counters
            if hasattr(target_card, 'toughness') and target_card.toughness <= 0:
                gs.move_card(target_id, target_controller, "battlefield", target_controller, "graveyard")
                logging.debug(f"Wither: {target_card.name} died from -1/-1 counters")
                
            # Prevent normal damage
            context["damage_amount"] = 0
            
        return True

    def _apply_aftermath(self, card_id, event_type, context=None):
        """Apply aftermath ability effects (cast second half from graveyard)"""
        gs = self.game_state
        
        if event_type == "CAST_FROM_GRAVEYARD" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "aftermath" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            # Check if casting the aftermath half
            casting_aftermath = context.get("casting_aftermath", False)
            
            if casting_aftermath:
                # Move from graveyard to stack
                controller["graveyard"].remove(card_id)
                gs.stack.append(("SPELL", card_id, controller, {"aftermath": True}))
                
                logging.debug(f"Aftermath: Cast second half of {card.name} from graveyard")
                
        elif event_type == "SPELL_RESOLVES" and context and context.get("aftermath"):
            # After resolving an aftermath spell, exile it
            controller = context.get("controller")
            if controller:
                # Move to exile instead of graveyard
                context["skip_default_movement"] = True
                controller["exile"].append(card_id)
                
                logging.debug(f"Aftermath: Exiled {gs._safe_get_card(card_id).name} after casting from graveyard")
                
        return True
        
    def _apply_ingest(self, card_id, event_type, context=None):
        """Apply ingest ability effects (Opponent exiles top card of library when damaged)"""
        gs = self.game_state
        
        if event_type == "DEALS_COMBAT_DAMAGE_TO_PLAYER" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "ingest" not in card.oracle_text.lower():
                return True
                
            opponent = context.get("damaged_player")
            if not opponent:
                return True
                
            # Exile top card of opponent's library
            if opponent["library"]:
                exiled_card = opponent["library"].pop(0)
                opponent["exile"].append(exiled_card)
                
                logging.debug(f"Ingest: {card.name} caused opponent to exile {gs._safe_get_card(exiled_card).name} from library")
                
        return True

    def _apply_intimidate(self, card_id, event_type, context=None):
        """Apply intimidate ability effects (Can only be blocked by artifact creatures and/or creatures that share a color)"""
        gs = self.game_state
        
        if event_type == "BLOCKING" and context:
            if context.get("attacker_id") == card_id:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text') or "intimidate" not in card.oracle_text.lower():
                    return True
                    
                blocker_id = context.get("blocker_id")
                blocker = gs._safe_get_card(blocker_id)
                
                if not blocker:
                    return True
                    
                # Check if blocker is an artifact creature
                is_artifact = False
                if hasattr(blocker, 'card_types'):
                    is_artifact = 'artifact' in blocker.card_types
                    
                if is_artifact:
                    return True  # Artifact creatures can block
                    
                # Check if blocker shares a color with attacker
                shares_color = False
                if hasattr(card, 'colors') and hasattr(blocker, 'colors'):
                    for i in range(min(len(card.colors), len(blocker.colors))):
                        if card.colors[i] and blocker.colors[i]:
                            shares_color = True
                            break
                            
                if not shares_color:
                    logging.debug(f"Intimidate: {card.name} can't be blocked by {blocker.name} (needs shared color or artifact)")
                    return False
                    
        return True

    def _apply_jump_start(self, card_id, event_type, context=None):
        """Apply jump-start ability effects (Cast from graveyard by discarding a card)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("jump_start"):
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check if card is in graveyard
            if card_id not in controller["graveyard"]:
                return True
                
            # Require discarding a card
            if not controller["hand"]:
                logging.debug(f"Jump-start: No cards in hand to discard")
                return False
                
            # Discard a card
            discard_id = controller["hand"][0]  # Choose first card for simplicity
            controller["hand"].remove(discard_id)
            controller["graveyard"].append(discard_id)
            
            # Flag for exile after resolution
            if not hasattr(gs, 'jump_start_cards'):
                gs.jump_start_cards = set()
                
            gs.jump_start_cards.add(card_id)
            
            logging.debug(f"Jump-start: Discarded a card to cast {card.name} from graveyard")
            
        elif event_type == "SPELL_RESOLVES" and hasattr(gs, 'jump_start_cards') and card_id in gs.jump_start_cards:
            # Exile after resolution
            controller = context.get("controller")
            if controller:
                controller["exile"].append(card_id)
                gs.jump_start_cards.remove(card_id)
                
                # Prevent default movement to graveyard
                context["skip_default_movement"] = True
                
                logging.debug(f"Jump-start: Exiled {gs._safe_get_card(card_id).name} after resolution")
                
        return True

    def _apply_landwalk(self, card_id, event_type, context=None):
        """Apply landwalk ability effects (Can't be blocked if defender controls land of specified type)"""
        gs = self.game_state
        
        if event_type == "BLOCKING" and context:
            if context.get("attacker_id") == card_id:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    return True
                    
                # Parse landwalk type
                landwalk_type = None
                for land_type in ["plains", "island", "swamp", "mountain", "forest"]:
                    if f"{land_type}walk" in card.oracle_text.lower():
                        landwalk_type = land_type
                        break
                        
                if not landwalk_type:
                    return True
                    
                # Find defender
                blocker_id = context.get("blocker_id")
                blocker_controller = None
                for player in [gs.p1, gs.p2]:
                    if blocker_id in player["battlefield"]:
                        blocker_controller = player
                        break
                        
                if not blocker_controller:
                    return True
                    
                # Check if defender controls land of specified type
                has_land_type = False
                for land_id in blocker_controller["battlefield"]:
                    land = gs._safe_get_card(land_id)
                    if not land or not hasattr(land, 'card_types') or 'land' not in land.card_types:
                        continue
                        
                    # Check subtypes for the landwalk type
                    if hasattr(land, 'subtypes'):
                        for subtype in land.subtypes:
                            if subtype.lower() == landwalk_type:
                                has_land_type = True
                                break
                                
                    if has_land_type:
                        break
                        
                if has_land_type:
                    logging.debug(f"{landwalk_type.capitalize()}walk: {card.name} can't be blocked")
                    return False
                    
        return True
        
    def _apply_frenzy(self, card_id, event_type, context=None):
        """Apply frenzy ability effects (Get +1/+0 for each attacking creature)"""
        gs = self.game_state
        
        if event_type == "ATTACKS":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse frenzy value
            match = re.search(r"frenzy (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            frenzy_value = int(match.group(1))
            
            # Check if card is actually attacking
            if card_id not in gs.current_attackers:
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Count attacking creatures
            attacking_count = len(gs.current_attackers)
            
            # Add temporary buff equal to frenzy * number of attackers
            frenzy_bonus = frenzy_value * attacking_count
            
            if not hasattr(controller, "temp_buffs"):
                controller["temp_buffs"] = {}
                
            if card_id not in controller["temp_buffs"]:
                controller["temp_buffs"][card_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                
            controller["temp_buffs"][card_id]["power"] += frenzy_bonus
            
            logging.debug(f"Frenzy {frenzy_value}: {card.name} gets +{frenzy_bonus}/+0 until end of turn")
            
        return True

    def _apply_friends_forever(self, card_id, event_type, context=None):
        """Apply friends forever ability effects (Can have two commanders)"""
        # This is more of a Commander format rule that affects deck building
        # Not much to apply during gameplay
        return True

    def _apply_fuse(self, card_id, event_type, context=None):
        """Apply fuse ability effects (Cast both halves of split card)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("fuse"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "fuse" not in card.oracle_text.lower():
                return True
                
            # Mark that both halves of the split card should be cast
            context["cast_both_halves"] = True
            
            # If the split card has two effects, they'll both be applied during resolution
            logging.debug(f"Fuse: Casting both halves of {card.name}")
            
        elif event_type == "SPELL_RESOLVES" and context and context.get("cast_both_halves"):
            # Both halves of the split card are resolved
            # This would be handled in the resolve_spell method
            card = gs._safe_get_card(card_id)
            logging.debug(f"Fuse: Resolved both halves of {card.name if card else 'split card'}")
            
        return True

    def _apply_graft(self, card_id, event_type, context=None):
        """Apply graft ability effects (Enter with +1/+1 counters, move to creatures that enter)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse graft value
            match = re.search(r"graft (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            graft_value = int(match.group(1))
            
            # Add +1/+1 counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            card.counters["+1/+1"] = graft_value
            
            # Apply counter effect
            if hasattr(card, 'power'):
                card.power += graft_value
            if hasattr(card, 'toughness'):
                card.toughness += graft_value
                
            logging.debug(f"Graft: {card.name} entered with {graft_value} +1/+1 counters")
            
        elif event_type == "ENTERS_BATTLEFIELD" and context:
            # Check if another creature is entering
            entering_card_id = context.get("card_id")
            if not entering_card_id or entering_card_id == card_id:
                return True
                
            # Check if the card with graft has any counters
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'counters') or not card.counters.get("+1/+1", 0):
                return True
                
            # Check if this card has graft
            if not hasattr(card, 'oracle_text') or "graft" not in card.oracle_text.lower():
                return True
                
            # Find controllers
            graft_controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    graft_controller = player
                    break
                    
            entering_controller = None
            for player in [gs.p1, gs.p2]:
                if entering_card_id in player["battlefield"]:
                    entering_controller = player
                    break
                    
            if not graft_controller or not entering_controller:
                return True
                
            # Check if entering card is a creature
            entering_card = gs._safe_get_card(entering_card_id)
            if not entering_card or not hasattr(entering_card, 'card_types') or 'creature' not in entering_card.card_types:
                return True
                
            # Only graft to creatures you control
            if graft_controller != entering_controller:
                return True
                
            # Decide whether to move a counter (AI decision)
            should_move = True
            
            if should_move:
                # Move a counter from graft creature to entering creature
                card.counters["+1/+1"] -= 1
                
                # Update graft creature's power/toughness
                if hasattr(card, 'power'):
                    card.power -= 1
                if hasattr(card, 'toughness'):
                    card.toughness -= 1
                    
                # Add counter to entering creature
                if not hasattr(entering_card, 'counters'):
                    entering_card.counters = {}
                    
                entering_card.counters["+1/+1"] = entering_card.counters.get("+1/+1", 0) + 1
                
                # Update entering creature's power/toughness
                if hasattr(entering_card, 'power'):
                    entering_card.power += 1
                if hasattr(entering_card, 'toughness'):
                    entering_card.toughness += 1
                    
                logging.debug(f"Graft: Moved a +1/+1 counter from {card.name} to {entering_card.name}")
                
                # Remove counter entry if empty
                if card.counters["+1/+1"] <= 0:
                    del card.counters["+1/+1"]
                    
        return True

    def _apply_haunt(self, card_id, event_type, context=None):
        """Apply haunt ability effects (Exile haunting a creature, trigger when it dies)"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "haunt" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["graveyard"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Find a creature to haunt
            creatures = []
            for player in [gs.p1, gs.p2]:
                for creature_id in player["battlefield"]:
                    creature = gs._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types:
                        creatures.append((creature_id, player))
                        
            if not creatures:
                return True
                
            # Choose a creature to haunt (AI decision)
            # Prefer opponent's creatures
            opponent_creatures = [(cid, p) for cid, p in creatures if p != controller]
            
            if opponent_creatures:
                target_id, target_controller = opponent_creatures[0]
            elif creatures:
                target_id, target_controller = creatures[0]
            else:
                return True
                
            # Move to exile
            gs.move_card(card_id, controller, "graveyard", controller, "exile")
            
            # Track haunt
            if not hasattr(gs, 'haunt_targets'):
                gs.haunt_targets = {}
                
            gs.haunt_targets[card_id] = target_id
            
            # Mark the target as being haunted
            if not hasattr(target_controller, "haunted_by"):
                target_controller["haunted_by"] = {}
                
            if target_id not in target_controller["haunted_by"]:
                target_controller["haunted_by"][target_id] = []
                
            target_controller["haunted_by"][target_id].append(card_id)
            
            logging.debug(f"Haunt: {card.name} is haunting {gs._safe_get_card(target_id).name}")
            
        elif event_type == "DIES" and hasattr(gs, 'haunt_targets'):
            # Check if dying creature is being haunted
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["graveyard"] and hasattr(player, "haunted_by") and card_id in player["haunted_by"]:
                    controller = player
                    break
                    
            if not controller or not hasattr(controller, "haunted_by") or card_id not in controller["haunted_by"]:
                return True
                
            # Trigger effects from each card haunting this creature
            for haunter_id in controller["haunted_by"].get(card_id, []):
                haunter = gs._safe_get_card(haunter_id)
                if not haunter:
                    continue
                    
                # Apply haunt effect
                logging.debug(f"Haunt: {haunter.name}'s haunt effect triggered from {gs._safe_get_card(card_id).name}'s death")
                
                # Parse haunt effect from card
                # Since this is complex and varies by card, we'll just simulate a basic effect
                # In a real implementation, this would need more specific handling
                
                # Apply the effect (usually similar to the original card's effect)
                if "deal" in haunter.oracle_text.lower() and "damage" in haunter.oracle_text.lower():
                    # Find damage amount
                    damage_match = re.search(r"deals? (\d+) damage", haunter.oracle_text.lower())
                    damage = int(damage_match.group(1)) if damage_match else 1
                    
                    # Deal damage to opponent
                    opponent = gs.p2 if controller == gs.p1 else gs.p1
                    opponent["life"] -= damage
                    logging.debug(f"Haunt: Dealt {damage} damage from {haunter.name}'s haunt effect")
                    
                elif "gain" in haunter.oracle_text.lower() and "life" in haunter.oracle_text.lower():
                    # Find life amount
                    life_match = re.search(r"gain (\d+) life", haunter.oracle_text.lower())
                    life = int(life_match.group(1)) if life_match else 1
                    
                    # Haunter's controller gains life
                    haunter_controller = None
                    for player in [gs.p1, gs.p2]:
                        if haunter_id in player["exile"]:
                            haunter_controller = player
                            break
                            
                    if haunter_controller:
                        haunter_controller["life"] += life
                        logging.debug(f"Haunt: Gained {life} life from {haunter.name}'s haunt effect")
                        
            # Remove haunt tracking for this creature
            del controller["haunted_by"][card_id]
            
        return True

    def _apply_hidden_agenda(self, card_id, event_type, context=None):
        """Apply hidden agenda ability effects (Name a card before the game begins)"""
        # This is mostly a pre-game effect
        # During gameplay it just needs to check if named card matches
        return True

    def _apply_horsemanship(self, card_id, event_type, context=None):
        """Apply horsemanship ability effects (Can only be blocked by creatures with horsemanship)"""
        gs = self.game_state
        
        if event_type == "BLOCKING" and context:
            if context.get("attacker_id") == card_id:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text') or "horsemanship" not in card.oracle_text.lower():
                    return True
                    
                blocker_id = context.get("blocker_id")
                blocker = gs._safe_get_card(blocker_id)
                
                if not blocker:
                    return True
                    
                # Check if blocker has horsemanship
                has_horsemanship = False
                if hasattr(blocker, 'oracle_text'):
                    has_horsemanship = "horsemanship" in blocker.oracle_text.lower()
                    
                if not has_horsemanship:
                    logging.debug(f"Horsemanship: {card.name} can't be blocked by creatures without horsemanship")
                    return False
                    
        return True

    def _apply_improvise(self, card_id, event_type, context=None):
        """Apply improvise ability effects (Tap artifacts to reduce cost)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "improvise" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Get untapped artifacts that can be tapped for improvise
            untapped_artifacts = []
            for artifact_id in controller["battlefield"]:
                artifact = gs._safe_get_card(artifact_id)
                if (artifact and hasattr(artifact, 'card_types') and 'artifact' in artifact.card_types
                    and artifact_id not in controller.get("tapped_permanents", set())):
                    untapped_artifacts.append(artifact_id)
                    
            # Decide how many artifacts to tap (AI decision)
            if "mana_cost" in context and "generic" in context["mana_cost"]:
                artifacts_to_tap = min(len(untapped_artifacts), context["mana_cost"]["generic"])
                
                if artifacts_to_tap > 0:
                    # Tap artifacts to reduce cost
                    for i in range(artifacts_to_tap):
                        controller["tapped_permanents"].add(untapped_artifacts[i])
                        
                    # Reduce generic mana cost
                    context["mana_cost"]["generic"] -= artifacts_to_tap
                    
                    logging.debug(f"Improvise: Tapped {artifacts_to_tap} artifacts to reduce cost of {card.name}")
                    
        return True
    
    def _apply_fortify(self, card_id, event_type, context=None):
        """Apply fortify ability effects (Attach to lands like Equipment)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("fortify"):
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            target_id = context.get("target_id")
            
            if not card or not hasattr(card, 'oracle_text') or not controller or not target_id:
                return True
                
            # Check if card is a Fortification
            if not hasattr(card, 'subtypes') or 'fortification' not in [s.lower() for s in card.subtypes]:
                return True
                
            # Check if target is a land
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'land' not in target_card.card_types:
                logging.debug(f"Fortify: Target is not a land")
                return False
                
            # Check if target is controlled by the same player
            if target_id not in controller["battlefield"]:
                logging.debug(f"Fortify: Target land is not controlled by the same player")
                return False
                
            # Parse fortify cost
            match = re.search(r"fortify ([^\(]+)", card.oracle_text.lower())
            fortify_cost = match.group(1) if match else None
            
            if not fortify_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            fortify_parsed = gs.mana_system.parse_mana_cost(fortify_cost)
            if not gs.mana_system.can_pay_mana_cost(controller, fortify_parsed):
                logging.debug(f"Fortify: Cannot pay fortify cost")
                return False
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, fortify_parsed)
            
            # Attach Fortification to land
            if not hasattr(controller, "attachments"):
                controller["attachments"] = {}
                
            controller["attachments"][card_id] = target_id
            
            logging.debug(f"Fortify: Attached {card.name} to {target_card.name}")
            
        return True
            
    def _apply_extort(self, card_id, event_type, context=None):
        """Apply extort ability effects (Pay when casting to drain each opponent)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL":
            # Extort triggers whenever you cast any spell
            spell_id = context.get("spell_id")
            controller = context.get("controller")
            
            if not spell_id or not controller:
                return True
                
            # Check if this permanent has extort
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "extort" not in card.oracle_text.lower():
                return True
                
            # Check if card is on battlefield and controlled by spell's controller
            on_battlefield = False
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"] and player == controller:
                    on_battlefield = True
                    break
                    
            if not on_battlefield:
                return True
                
            # Check if controller can pay {W/B}
            if hasattr(gs, 'mana_system'):
                extort_cost = {"W/B": 1}  # Hybrid mana
                
                # Choose whether to pay (AI decision)
                opponent = gs.p2 if controller == gs.p1 else gs.p1
                should_pay = opponent["life"] > 5 and gs.mana_system.can_pay_mana_cost(controller, extort_cost)
                
                if should_pay:
                    # Pay the cost
                    gs.mana_system.pay_mana_cost(controller, extort_cost)
                    
                    # Extort each opponent (in multiplayer)
                    # In our simulation, just one opponent
                    controller["life"] += 1
                    opponent["life"] -= 1
                    
                    logging.debug(f"Extort: {card.name} extorted 1 life from opponent")
                    
        return True

    def _apply_fading(self, card_id, event_type, context=None):
        """Apply fading ability effects (Enters with time counters, sac when empty)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse fading value
            match = re.search(r"fading (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            fading_value = int(match.group(1))
            
            # Add time counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            card.counters["time"] = fading_value
            logging.debug(f"Fading: {card.name} entered with {fading_value} time counters")
            
        elif event_type == "UPKEEP" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "fading" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check if it's this player's upkeep
            if context.get("controller") != controller:
                return True
                
            # Remove a time counter
            if not hasattr(card, "counters"):
                card.counters = {}
                
            if card.counters.get("time", 0) > 0:
                card.counters["time"] -= 1
                logging.debug(f"Fading: Removed a time counter from {card.name}, {card.counters['time']} remaining")
                
                # If no time counters left, sacrifice
                if card.counters["time"] <= 0:
                    gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                    logging.debug(f"Fading: Sacrificed {card.name} with no time counters")
                    
        return True

    def _apply_fear(self, card_id, event_type, context=None):
        """Apply fear ability effects (Can only be blocked by black or artifact creatures)"""
        gs = self.game_state
        
        if event_type == "BLOCKING" and context:
            if context.get("attacker_id") == card_id:
                blocker_id = context.get("blocker_id")
                blocker = gs._safe_get_card(blocker_id)
                
                if not blocker:
                    return True
                    
                # Check if blocker is black or an artifact
                is_black = False
                is_artifact = False
                
                if hasattr(blocker, 'colors') and len(blocker.colors) > 2:
                    is_black = blocker.colors[2] == 1  # Black is at index 2 [W,U,B,R,G]
                    
                if hasattr(blocker, 'card_types'):
                    is_artifact = 'artifact' in blocker.card_types
                    
                # If not black or artifact, can't block
                if not is_black and not is_artifact:
                    logging.debug(f"Fear: {gs._safe_get_card(card_id).name} can't be blocked by non-black, non-artifact creatures")
                    return False
                    
        return True

    def _apply_flanking(self, card_id, event_type, context=None):
        """Apply flanking ability effects (Creatures blocking get -1/-1 until end of turn)"""
        gs = self.game_state
        
        if event_type == "BLOCKED" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "flanking" not in card.oracle_text.lower():
                return True
                
            blocker_ids = context.get("blockers", [])
            
            for blocker_id in blocker_ids:
                blocker = gs._safe_get_card(blocker_id)
                if not blocker:
                    continue
                    
                # Check if blocker has flanking
                has_flanking = False
                if hasattr(blocker, 'oracle_text'):
                    has_flanking = "flanking" in blocker.oracle_text.lower()
                    
                # If blocker doesn't have flanking, apply -1/-1
                if not has_flanking:
                    # Find blocker controller
                    blocker_controller = None
                    for player in [gs.p1, gs.p2]:
                        if blocker_id in player["battlefield"]:
                            blocker_controller = player
                            break
                            
                    if blocker_controller:
                        # Add temporary penalty
                        if not hasattr(blocker_controller, "temp_buffs"):
                            blocker_controller["temp_buffs"] = {}
                            
                        if blocker_id not in blocker_controller["temp_buffs"]:
                            blocker_controller["temp_buffs"][blocker_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                            
                        blocker_controller["temp_buffs"][blocker_id]["power"] -= 1
                        blocker_controller["temp_buffs"][blocker_id]["toughness"] -= 1
                        
                        logging.debug(f"Flanking: {blocker.name} gets -1/-1 until end of turn")
                        
                        # Check if the blocker now has 0 or less toughness
                        effective_toughness = blocker.toughness + blocker_controller["temp_buffs"][blocker_id]["toughness"]
                        if effective_toughness <= 0:
                            gs.move_card(blocker_id, blocker_controller, "battlefield", blocker_controller, "graveyard")
                            logging.debug(f"Flanking: {blocker.name} died from flanking effect")
                            
        return True

    def _apply_forecast(self, card_id, event_type, context=None):
        """Apply forecast ability effects (Activated from hand during upkeep)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("forecast"):
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check if card is in hand
            if card_id not in controller["hand"]:
                return True
                
            # Check if it's the controller's upkeep
            if gs.phase != gs.PHASE_UPKEEP:
                logging.debug(f"Forecast: Can only activate during controller's upkeep")
                return False
                
            # Parse forecast cost
            match = re.search(r"forecast — ([^,]+),", card.oracle_text.lower())
            forecast_cost = match.group(1).strip() if match else None
            
            if not forecast_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            forecast_parsed = gs.mana_system.parse_mana_cost(forecast_cost)
            if not gs.mana_system.can_pay_mana_cost(controller, forecast_parsed):
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, forecast_parsed)
            
            # Parse forecast effect
            effect_match = re.search(r"forecast — [^,]+, [^:]+: (.*?)(?=\.|$)", card.oracle_text)
            effect_text = effect_match.group(1).strip() if effect_match else ""
            
            # Apply forecast effect
            if "draw a card" in effect_text.lower():
                gs._draw_phase(controller)
                logging.debug(f"Forecast: Drew a card from {card.name}'s forecast ability")
                
            elif "create" in effect_text.lower() and "token" in effect_text.lower():
                # Parse token details
                power, toughness = 1, 1
                token_type = "Creature"
                
                # Create token
                token_data = {
                    "name": "Token",
                    "type_line": "creature",
                    "card_types": ["creature"],
                    "subtypes": [],
                    "power": power,
                    "toughness": toughness,
                    "oracle_text": "",
                    "keywords": [0] * 11  # No keywords by default
                }
                
                gs.create_token(controller, token_data)
                logging.debug(f"Forecast: Created a token from {card.name}'s forecast ability")
                
            # Flag that forecast was used this turn
            if not hasattr(gs, 'forecast_used'):
                gs.forecast_used = set()
                
            gs.forecast_used.add(card_id)
            
        elif event_type == "END_TURN":
            # Reset forecast tracking for the new turn
            if hasattr(gs, 'forecast_used'):
                gs.forecast_used.clear()
                
        return True
        
    def _apply_compleated(self, card_id, event_type, context=None):
        """Apply compleated ability effects (Can pay life instead of Phyrexian mana)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "compleated" not in card.oracle_text.lower():
                return True
                
            # Compleated ability modifies Phyrexian mana costs
            # Here we'll track if life was paid instead of mana
            if context.get("paid_life_for_phyrexian"):
                controller = context.get("controller")
                if controller:
                    # In a real game, a planeswalker with compleated enters with fewer loyalty
                    # counters if life was paid for Phyrexian mana
                    if hasattr(card, 'card_types') and 'planeswalker' in card.card_types:
                        if not hasattr(controller, "loyalty_counters"):
                            controller["loyalty_counters"] = {}
                            
                        # Reduce loyalty counters
                        loyalty = controller["loyalty_counters"].get(card_id, 0)
                        controller["loyalty_counters"][card_id] = max(0, loyalty - 2)
                        logging.debug(f"Compleated: {card.name} entered with fewer loyalty counters due to paying life")
                        
        return True

    def _apply_encore(self, card_id, event_type, context=None):
        """Apply encore ability effects (Exile from graveyard to create attacking token copies)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("encore"):
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check if card is in graveyard
            if card_id not in controller["graveyard"]:
                return True
                
            # Parse encore cost
            match = re.search(r"encore [^\(]([^\)]+)", card.oracle_text.lower())
            encore_cost = match.group(1) if match else None
            
            if not encore_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay cost
            encore_parsed = gs.mana_system.parse_mana_cost(encore_cost)
            if not gs.mana_system.can_pay_mana_cost(controller, encore_parsed):
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, encore_parsed)
            
            # Exile the card
            gs.move_card(card_id, controller, "graveyard", controller, "exile")
            
            # Create token copies for each opponent (in a multiplayer game)
            # In our simulation, we'll just create one token
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            # Create token data with haste
            token_data = {
                "name": f"{card.name} Token",
                "type_line": card.type_line if hasattr(card, 'type_line') else "Creature",
                "card_types": card.card_types.copy() if hasattr(card, 'card_types') else ["creature"],
                "subtypes": card.subtypes.copy() if hasattr(card, 'subtypes') else [],
                "power": card.power if hasattr(card, 'power') else 1,
                "toughness": card.toughness if hasattr(card, 'toughness') else 1,
                "oracle_text": card.oracle_text if hasattr(card, 'oracle_text') else "",
                "keywords": [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]  # Haste at index 9
            }
            
            # Create token and make it attack
            token_id = gs.create_token(controller, token_data)
            if token_id:
                gs.current_attackers.append(token_id)
                logging.debug(f"Encore: Created attacking token copy of {card.name} with haste")
                
                # Sacrifice at end of turn
                if not hasattr(gs, 'sacrifice_at_end_turn'):
                    gs.sacrifice_at_end_turn = []
                gs.sacrifice_at_end_turn.append((token_id, controller))
                
        elif event_type == "END_STEP":
            # Sacrifice tokens at end of turn
            if hasattr(gs, 'sacrifice_at_end_turn'):
                for token_id, token_controller in gs.sacrifice_at_end_turn:
                    if token_id in token_controller["battlefield"]:
                        gs.move_card(token_id, token_controller, "battlefield", token_controller, "graveyard")
                        logging.debug(f"Encore: Sacrificed token {token_id} at end of turn")
                
                # Clear the list
                gs.sacrifice_at_end_turn = []
                
        return True

    def _apply_entwine(self, card_id, event_type, context=None):
        """Apply entwine ability effects (Choose all modes by paying additional cost)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("entwine"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse entwine cost
            match = re.search(r"entwine [^\(]([^\)]+)", card.oracle_text.lower())
            entwine_cost = match.group(1) if match else None
            
            if entwine_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Add entwine cost to regular cost
                entwine_parsed = gs.mana_system.parse_mana_cost(entwine_cost)
                for color, amount in entwine_parsed.items():
                    if color in context["mana_cost"]:
                        context["mana_cost"][color] += amount
                    else:
                        context["mana_cost"][color] = amount
                        
                # Mark this spell as using entwine
                context["entwine"] = True
                logging.debug(f"Entwine: Paid additional cost to use all modes of {card.name}")
                
        elif event_type == "SPELL_RESOLVES" and context and context.get("entwine"):
            # When a spell with entwine resolves, all modes are chosen
            # This is handled in the resolve_modal_spell function in game_state.py
            pass
            
        return True

    def _apply_epic(self, card_id, event_type, context=None):
        """Apply epic ability effects (Copy the spell each upkeep but can't cast other spells)"""
        gs = self.game_state
        
        if event_type == "SPELL_RESOLVES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "epic" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Add epic effect
            if not hasattr(gs, 'epic_spells'):
                gs.epic_spells = {}
                
            gs.epic_spells[controller] = card_id
            logging.debug(f"Epic: {card.name} resolved with epic effect")
            
        elif event_type == "UPKEEP" and hasattr(gs, 'epic_spells'):
            controller = context.get("controller")
            if not controller or controller not in gs.epic_spells:
                return True
                
            epic_card_id = gs.epic_spells[controller]
            epic_card = gs._safe_get_card(epic_card_id)
            
            if epic_card:
                # Create a copy of the epic spell on the stack
                gs.stack.append(("SPELL", epic_card_id, controller, {"is_copy": True}))
                logging.debug(f"Epic: Copied {epic_card.name} during upkeep")
                
        elif event_type == "CAST_SPELL" and hasattr(gs, 'epic_spells'):
            controller = context.get("controller")
            if controller and controller in gs.epic_spells:
                # Can't cast spells while under an epic effect
                card = gs._safe_get_card(card_id)
                logging.debug(f"Epic: Cannot cast {card.name if card else 'spell'} due to epic effect")
                return False
                
        return True

    def _apply_escape(self, card_id, event_type, context=None):
        """Apply escape ability effects (Cast from graveyard by paying cost and exiling cards)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("escape"):
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check if card is in graveyard
            if card_id not in controller["graveyard"]:
                return True
                
            # Parse escape cost and exile requirement
            match = re.search(r"escape—([^,]+),\s*exile\s+([^\.]+)", card.oracle_text.lower())
            if not match:
                return True
                
            escape_cost = match.group(1).strip()
            exile_requirement = match.group(2).strip()
            
            # Parse exile count
            exile_count = 0
            count_match = re.search(r"(\d+|[a-z]+)\s+(?:other )?cards?", exile_requirement)
            if count_match:
                count_text = count_match.group(1)
                if count_text.isdigit():
                    exile_count = int(count_text)
                elif count_text == "five":
                    exile_count = 5
                elif count_text == "four":
                    exile_count = 4
                elif count_text == "three":
                    exile_count = 3
                elif count_text == "two":
                    exile_count = 2
                else:
                    exile_count = 1
            
            # Check if enough cards in graveyard to exile
            other_cards = [cid for cid in controller["graveyard"] if cid != card_id]
            if len(other_cards) < exile_count:
                logging.debug(f"Escape: Not enough cards in graveyard to escape {card.name}")
                return True
                
            # Check if controller can pay mana cost
            if "mana_cost" in context and hasattr(gs, 'mana_system'):
                context["mana_cost"] = gs.mana_system.parse_mana_cost(escape_cost)
                
                # Exile the required cards
                for _ in range(exile_count):
                    exile_id = other_cards.pop(0)
                    controller["graveyard"].remove(exile_id)
                    controller["exile"].append(exile_id)
                    
                # Move escaping card from graveyard to stack - handled in cast_spell
                controller["graveyard"].remove(card_id)
                
                logging.debug(f"Escape: Casting {card.name} from graveyard, exiled {exile_count} cards")
                
                # Flag for escape effects
                context["escaped"] = True
                
        elif event_type == "ENTERS_BATTLEFIELD" and context and context.get("escaped"):
            # Apply additional effects for escaped permanents
            # Some cards get +1/+1 counters when they escape
            card = gs._safe_get_card(card_id)
            if card and hasattr(card, 'oracle_text'):
                # Check for +1/+1 counter addition on escape
                counter_match = re.search(r"enters with (\w+) \+1/\+1 counters? on it", card.oracle_text.lower())
                if counter_match:
                    counter_count = 1
                    count_text = counter_match.group(1)
                    if count_text.isdigit():
                        counter_count = int(count_text)
                    elif count_text == "two":
                        counter_count = 2
                    elif count_text == "three":
                        counter_count = 3
                    
                    # Add the counters
                    if not hasattr(card, 'counters'):
                        card.counters = {}
                        
                    card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + counter_count
                    card.power += counter_count
                    card.toughness += counter_count
                    
                    logging.debug(f"Escape: {card.name} escaped with {counter_count} +1/+1 counters")
                    
        return True

    def _apply_exploit(self, card_id, event_type, context=None):
        """Apply exploit ability effects (Sacrifice a creature for benefit when ETB)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "exploit" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Find creatures that can be sacrificed
            creatures = [cid for cid in controller["battlefield"] 
                        if cid != card_id and
                        gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types]
            
            if not creatures:
                logging.debug(f"Exploit: No creatures to sacrifice for {card.name}")
                return True
                
            # In a real game, the player would choose whether to exploit and which creature
            # For the simulation, we'll make a simple decision
            
            # Calculate creature values
            creature_values = []
            for creature_id in creatures:
                creature = gs._safe_get_card(creature_id)
                if not creature:
                    continue
                    
                value = 0
                
                # Considers base power and toughness
                if hasattr(creature, 'power') and hasattr(creature, 'toughness'):
                    value += (creature.power + creature.toughness) / 2
                    
                # Penalties for creatures with good abilities
                if hasattr(creature, 'oracle_text'):
                    if "flying" in creature.oracle_text.lower():
                        value += 2
                    if "lifelink" in creature.oracle_text.lower():
                        value += 2
                    if "deathtouch" in creature.oracle_text.lower():
                        value += 2
                        
                # Bonus for token creatures
                if hasattr(controller, 'tokens') and creature_id in controller['tokens']:
                    value -= 3
                    
                creature_values.append((creature_id, value))
                
            # Sort by value (lower is better to sacrifice)
            creature_values.sort(key=lambda x: x[1])
            
            if creature_values:
                # Choose the lowest value creature
                sacrifice_id = creature_values[0][0]
                
                # Sacrifice the creature
                gs.move_card(sacrifice_id, controller, "battlefield", controller, "graveyard")
                
                # Trigger the exploit ability
                logging.debug(f"Exploit: Sacrificed {gs._safe_get_card(sacrifice_id).name} to exploit {card.name}")
                
                # Flag that exploit was used
                if not hasattr(controller, "exploited"):
                    controller["exploited"] = set()
                    
                controller["exploited"].add(card_id)
                
                # Trigger "when you exploit a creature" abilities
                gs.trigger_ability(card_id, "EXPLOITED")
                
        return True
        
    def _apply_enchant(self, card_id, event_type, context):
        """Handle enchant ability, which defines what an Aura can enchant."""
        card = self.game_state._safe_get_card(card_id)
        
        if event_type == "CAST_SPELL":
            if hasattr(card, 'card_types') and 'enchantment' in card.card_types:
                if hasattr(card, 'subtypes') and 'aura' in card.subtypes:
                    # Determine what this can enchant
                    enchant_type = "creature"  # Default
                    if hasattr(card, 'oracle_text'):
                        enchant_match = re.search(r'enchant (\w+)', card.oracle_text.lower())
                        if enchant_match:
                            enchant_type = enchant_match.group(1)
                    
                    logging.debug(f"Enchant {enchant_type} available for {card_id}")
                    return {"enchant_type": enchant_type}
        return False
        
    def _apply_emerge(self, card_id, event_type, context):
        """Handle emerge ability, which allows sacrificing creatures to reduce cost."""
        if event_type == "CAST_SPELL":
            controller = context.get('controller')
            if controller:
                # Find creatures that can be sacrificed
                creatures = []
                for creature_id in controller["battlefield"]:
                    creature = self.game_state._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types:
                        creatures.append((creature_id, creature.cmc if hasattr(creature, 'cmc') else 0))
                
                if creatures:
                    logging.debug(f"Emerge available for {card_id}")
                    return {"can_emerge": True, "sacrifice_candidates": creatures}
        return False
        
    def _apply_disturb(self, card_id, event_type, context):
        """Handle disturb ability, which allows casting from graveyard transformed."""
        card = self.game_state._safe_get_card(card_id)
        
        if event_type == "CHECK_CASTABLE":
            controller = context.get('controller')
            if controller and card_id in controller["graveyard"]:
                # Disturb allows casting from graveyard
                if hasattr(card, 'oracle_text') and "disturb" in card.oracle_text.lower():
                    logging.debug(f"Disturb available for {card_id}")
                    return {"can_cast_from_graveyard": True, "transforms_on_cast": True}
        return False
        
    def _apply_devour(self, card_id, event_type, context):
        """Handle devour ability, which allows sacrificing creatures for +1/+1 counters."""
        card = self.game_state._safe_get_card(card_id)
        
        if event_type == "ENTERS_BATTLEFIELD":
            # Extract devour number
            devour_amount = 1
            if hasattr(card, 'oracle_text'):
                devour_match = re.search(r'devour (\d+)', card.oracle_text.lower())
                if devour_match:
                    devour_amount = int(devour_match.group(1))
            
            controller = context.get('controller')
            if controller:
                # Find creatures that can be sacrificed
                creatures = []
                for creature_id in controller["battlefield"]:
                    if creature_id == card_id:
                        continue  # Skip self
                        
                    creature = self.game_state._safe_get_card(creature_id)
                    if creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types:
                        creatures.append(creature_id)
                
                if creatures:
                    logging.debug(f"Devour {devour_amount} available for {card_id}")
                    return {"can_devour": True, "devour_amount": devour_amount, "sacrifice_candidates": creatures}
        return False
        
    def _apply_conspire(self, card_id, event_type, context):
        """Handle conspire ability, which allows copying a spell by tapping creatures."""
        if event_type == "CAST_SPELL":
            controller = context.get('controller')
            if controller:
                # Find untapped creatures sharing a color with this spell
                untapped_creatures = []
                for creature_id in controller["battlefield"]:
                    creature = self.game_state._safe_get_card(creature_id)
                    if (creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types
                            and creature_id not in controller.get("tapped_permanents", set())):
                        untapped_creatures.append(creature_id)
                
                # Need at least two creatures to conspire
                if len(untapped_creatures) >= 2:
                    logging.debug(f"Conspire available for {card_id}")
                    return {"can_conspire": True, "untapped_creatures": untapped_creatures}
        return False
            
    def _apply_cleave(self, card_id, event_type, context=None):
        """Apply effects for the cleave keyword ability."""
        # Cleave is an alternative cost that removes bracketed text from the spell
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        # Check if this was cast with cleave (would be in context)
        if context and context.get("cast_with_cleave"):
            logging.debug(f"{card.name} was cast with cleave")
        
        return False  # No ongoing effect
    
    def _check_day_night_transition(self, card_id):
        """
        Check and update the day/night state based on spells cast this turn.
        This is called during the end step of each turn.
        """
        gs = self.game_state
        
        # Count spells cast by active player this turn
        active_player = gs._get_active_player()
        spells_cast = len([spell for spell in getattr(gs, "spells_cast_this_turn", []) 
                        if isinstance(spell, tuple) and len(spell) >= 2 and spell[1] == active_player])
        
        old_state = gs.day_night_state
        
        # Apply transition rules
        if gs.day_night_state is None:
            # If neither day nor night, and no spells were cast, it becomes night
            if spells_cast == 0:
                gs.day_night_state = "night"
                logging.debug("It becomes night (no spells cast)")
        elif gs.day_night_state == "day":
            # If day, and at least two spells were cast, it becomes night
            if spells_cast >= 2:
                gs.day_night_state = "night"
                logging.debug(f"It becomes night (player cast {spells_cast} spells)")
        elif gs.day_night_state == "night":
            # If night, and no spells were cast, it becomes day
            if spells_cast == 0:
                gs.day_night_state = "day"
                logging.debug("It becomes day (no spells cast)")
        
        # If the state changed, transform all daybound/nightbound cards
        if gs.day_night_state != old_state:
            self._transform_all_day_night_cards()

    def _transform_all_day_night_cards(self):
        """Transform all daybound/nightbound cards when day/night state changes."""
        gs = self.game_state
        
        # Check all permanents on the battlefield
        for player in [gs.p1, gs.p2]:
            for card_id in player["battlefield"]:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'oracle_text'):
                    continue
                
                oracle_text = card.oracle_text.lower()
                
                # Check for daybound or nightbound
                if "daybound" in oracle_text:
                    # Trigger transformation
                    self._apply_daybound(card_id, "DAY_NIGHT_CHANGED")
                elif "nightbound" in oracle_text:
                    # Trigger transformation
                    self._apply_nightbound(card_id, "DAY_NIGHT_CHANGED")

    def _apply_daybound(self, card_id, event_type, context=None):
        """
        Apply effects for the daybound keyword ability.
        
        Daybound appears on the front face (day side) of a double-faced card.
        When it becomes night, the card transforms to its night side.
        When it's day, the card stays on its day side.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return True
        
        # Initialize day/night state if not exists
        if not hasattr(gs, "day_night_state"):
            gs.day_night_state = None  # Start with neither day nor night
        
        # If this is the first daybound card encountered and it's neither day nor night,
        # make it day by default
        if gs.day_night_state is None and event_type == "ENTERS_BATTLEFIELD":
            gs.day_night_state = "day"
            logging.debug("Day/night cycle started: it became day")
        
        # Check for day/night transitions at end step
        if event_type == "END_STEP" and context:
            active_player = gs._get_active_player()
            if context.get("controller") == active_player:
                self._check_day_night_transition(card_id)
        
        # Handle transforming based on day/night state
        if event_type in ["UPKEEP", "DAY_NIGHT_CHANGED", "ENTERS_BATTLEFIELD"]:
            if gs.day_night_state == "night" and hasattr(card, "transform"):
                # Only transform if card is not already on night side
                if not getattr(card, "is_night_side", False):
                    card.transform()
                    logging.debug(f"{card.name} transformed to night side due to daybound")
                    # Trigger any abilities that care about transformation
                    gs.trigger_ability(card_id, "TRANSFORMED", {"card": card})
        
        return True

    def _apply_nightbound(self, card_id, event_type, context=None):
        """
        Apply effects for the nightbound keyword ability.
        
        Nightbound appears on the back face (night side) of a double-faced card.
        When it becomes day, the card transforms to its day side.
        When it's night, the card stays on its night side.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card:
            return True
        
        # Initialize day/night state if not exists
        if not hasattr(gs, "day_night_state"):
            gs.day_night_state = None  # Start with neither day nor night
        
        # If this is the first nightbound card encountered and it's neither day nor night,
        # make it night by default
        if gs.day_night_state is None and event_type == "ENTERS_BATTLEFIELD":
            gs.day_night_state = "night"
            logging.debug("Day/night cycle started: it became night")
        
        # Check for day/night transitions at end step
        if event_type == "END_STEP" and context:
            active_player = gs._get_active_player()
            if context.get("controller") == active_player:
                self._check_day_night_transition(card_id)
        
        # Handle transforming based on day/night state
        if event_type in ["UPKEEP", "DAY_NIGHT_CHANGED", "ENTERS_BATTLEFIELD"]:
            if gs.day_night_state == "day" and hasattr(card, "transform"):
                # Only transform if card is not already on day side
                if getattr(card, "is_night_side", True):
                    card.transform()
                    logging.debug(f"{card.name} transformed to day side due to nightbound")
                    # Trigger any abilities that care about transformation
                    gs.trigger_ability(card_id, "TRANSFORMED", {"card": card})
        
        return True

    def _apply_decayed(self, card_id, event_type, context=None):
        """Apply effects for the decayed keyword ability."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        # Decayed means "This creature can't block. When this creature attacks, sacrifice it at end of combat."
        if event_type == "BLOCKS" or event_type == "DECLARE_BLOCKERS":
            # Prevent blocking
            return False  # Cannot block
        
        if event_type == "ATTACKS":
            # Schedule sacrifice at end of combat
            if not hasattr(gs, "end_of_combat_triggers"):
                gs.end_of_combat_triggers = []
            
            gs.end_of_combat_triggers.append(("SACRIFICE", card_id))
            logging.debug(f"Decayed: {card.name} will be sacrificed at end of combat")
        
        return True
        
    def _apply_room_door_state(self, card_id, event_type, context=None):
        """Apply effects based on a Room's door state (locked/unlocked)"""
        gs = self.game_state
        
        if event_type == "CHECK_STATE":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_room') or not card.is_room:
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check door states and apply static effects
            door1_unlocked = card.door1.get('unlocked', False)
            door2_unlocked = card.door2.get('unlocked', False)
            
            # Door 1 static effects while unlocked
            if door1_unlocked:
                for ability in card.door1.get('static_abilities', []):
                    ability_type = ability.get('type', '')
                    ability_effect = ability.get('effect', '')
                    
                    # Apply ability based on type
                    if ability_type == 'land_ability' and ability_effect:
                        # Apply to all lands controller controls
                        for land_id in controller["battlefield"]:
                            land = gs._safe_get_card(land_id)
                            if land and hasattr(land, 'card_types') and 'land' in land.card_types:
                                # Apply effect (in a real implementation this would modify land abilities)
                                pass
                    
                    elif ability_type == 'hand_size' and 'no maximum hand size' in ability.get('description', ''):
                        # Set no maximum hand size flag
                        controller["no_max_hand_size"] = True
                    
                    elif ability_type == 'permanent_ability':
                        # Apply to matching permanents
                        scope = ability.get('scope', '')
                        effect = ability.get('effect', '')
                        
                        for perm_id in controller["battlefield"]:
                            perm = gs._safe_get_card(perm_id)
                            if perm and self._matches_scope(perm, scope):
                                # Apply effect (in a real implementation this would modify abilities)
                                pass
            
            # Door 2 static effects while unlocked
            if door2_unlocked:
                for ability in card.door2.get('static_abilities', []):
                    # Similar implementation as door1
                    pass
        
        return True
    
    def _matches_scope(self, card, scope):
        """Helper method to check if a card matches a scope description."""
        if not scope:
            return False
            
        scope = scope.lower()
        
        # Check card types
        if hasattr(card, 'card_types'):
            if 'creature' in scope and 'creature' in card.card_types:
                return True
            if 'artifact' in scope and 'artifact' in card.card_types:
                return True
            if 'enchantment' in scope and 'enchantment' in card.card_types:
                return True
            if 'land' in scope and 'land' in card.card_types:
                return True
            if 'planeswalker' in scope and 'planeswalker' in card.card_types:
                return True
        
        # Check subtypes
        if hasattr(card, 'subtypes'):
            for subtype in card.subtypes:
                if subtype.lower() in scope:
                    return True
        
        return False
        
    def _apply_class_level(self, card_id, event_type, context=None):
        """Apply class level effects based on current level."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'is_class') or not card.is_class:
            return True
            
        # Class cards typically have level-based abilities
        if event_type == "CLASS_LEVEL_UP" and context:
            # Re-register abilities based on new level
            self._parse_and_register_abilities(card_id, card)
            
            # Handle type changes (e.g., becoming a creature)
            level_data = card.get_current_class_data() if hasattr(card, 'get_current_class_data') else None
            if level_data and 'type_line' in level_data:
                # If class becomes a creature at this level
                if 'creature' in level_data['type_line'].lower() and not any('creature' in card_type for card_type in card.card_types):
                    # Add creature type
                    card.card_types.append('creature')
                    
                    # Set power/toughness if provided
                    if 'power' in level_data and 'toughness' in level_data:
                        card.power = level_data['power']
                        card.toughness = level_data['toughness']
                        
                    logging.debug(f"Class {card.name} became a creature at level {card.current_level}")
                    
            # Trigger any abilities related to leveling up
            gs.trigger_ability(card_id, "CLASS_LEVEL_CHANGED", context)
            
        return True
        
    def _trigger_saga_ability(self, card_id, ability_text, controller, chapter_number=None):
        """
        Trigger a saga chapter ability based on its text.
        This is a simplified implementation that covers common effects.
        """
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        
        # Convert ability text to lower case for easier matching
        text = ability_text.lower()
        
        # Common effects
        if "draw a card" in text:
            gs._draw_phase(controller)
            logging.debug(f"Saga ability: Drew a card")
            
        elif "create" in text and "token" in text:
            # Parse token creation
            # This is a simplified implementation
            if "1/1" in text:
                power, toughness = 1, 1
            elif "2/2" in text:
                power, toughness = 2, 2
            elif "3/3" in text:
                power, toughness = 3, 3
            else:
                power, toughness = 1, 1  # Default
                
            # Create token tracking if it doesn't exist
            if not hasattr(controller, "tokens"):
                controller["tokens"] = []
            
            # Create token
            token_id = f"TOKEN_{len(controller['tokens'])}"
            
            # Determine token type
            token_type = "Creature"
            if "creature" in text:
                # Try to extract creature type
                import re
                type_match = re.search(r"create [^.]+?(\d+/\d+) ([^.]+?) token", text)
                if type_match:
                    token_type = f"Creature — {type_match.group(2)}"
            
            # Create token
            token = Card({
                "name": f"{token_type} Token",
                "type_line": token_type.lower(),
                "card_types": ["creature"],
                "subtypes": [token_type.split("—")[1].strip().lower()] if "—" in token_type else [],
                "power": power,
                "toughness": toughness,
                "oracle_text": "",
                "keywords": [0] * 11  # Default no keywords
            })
            
            # Add token to game
            gs.card_db[token_id] = token
            controller["battlefield"].append(token_id)
            controller["tokens"].append(token_id)
            
            logging.debug(f"Saga ability: Created a {power}/{toughness} {token_type} token")
            
        elif "destroy" in text:
            # Handle destruction effects
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            if "destroy target" in text:
                # This would need proper targeting
                # For simplicity, we'll just destroy the first valid target
                
                target_types = []
                if "creature" in text:
                    target_types.append("creature")
                if "artifact" in text:
                    target_types.append("artifact")
                if "enchantment" in text:
                    target_types.append("enchantment")
                
                # Find a valid target
                for zone in ["battlefield"]:
                    for player in [opponent, controller]:  # Try opponent first
                        for target_id in player[zone]:
                            target_card = gs._safe_get_card(target_id)
                            if not target_card or not hasattr(target_card, 'card_types'):
                                continue
                                
                            for target_type in target_types:
                                if target_type in target_card.card_types:
                                    # Destroy this target
                                    gs.move_card(target_id, player, zone, player, "graveyard")
                                    logging.debug(f"Saga ability: Destroyed {target_card.name}")
                                    return  # Only destroy one target
        
        # Add more effect types as needed
        
        logging.debug(f"Saga ability: Applied {ability_text}")
        
    def _apply_saga(self, card_id, event_type, context=None):
        """Apply saga ability effects (add lore counters and trigger chapters)"""
        gs = self.game_state
        
        if event_type == "UPKEEP" and context:
            # Check if the card is a Saga
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or not hasattr(card, 'type_line') or "saga" not in card.type_line.lower():
                return True
                
            # Find controller
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Add a lore counter
            if not hasattr(card, "counters"):
                card.counters = {}
                
            card.counters["lore"] = card.counters.get("lore", 0) + 1
            lore_count = card.counters["lore"]
            
            logging.debug(f"Saga: Added lore counter to {card.name}, now at {lore_count}")
            
            # Trigger chapter ability based on lore counter count
            # Extract chapter abilities from oracle text
            oracle_text = card.oracle_text
            chapter_abilities = {}
            
            # Parse chapter abilities
            import re
            chapter_pattern = r"(I|II|III|IV|V)\s*—\s*([^•\n]+)"
            matches = re.findall(chapter_pattern, oracle_text)
            
            roman_to_int = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
            
            for roman, ability_text in matches:
                chapter_num = roman_to_int.get(roman, 0)
                if chapter_num > 0:
                    chapter_abilities[chapter_num] = ability_text.strip()
            
            # Trigger current chapter ability
            if lore_count in chapter_abilities:
                ability_text = chapter_abilities[lore_count]
                logging.debug(f"Saga: Triggering Chapter {lore_count}: {ability_text}")
                
                # Create a custom ability effect based on the text
                self._trigger_saga_ability(card_id, ability_text, controller)
                
            # Check if this was the final chapter
            if lore_count >= max(chapter_abilities.keys(), default=0):
                # Sacrifice the Saga
                gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                logging.debug(f"Saga: Sacrificed {card.name} after final chapter")
        
        return True
    
    def _apply_battle(self, card_id, event_type, context=None):
        """Apply battle card effects"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'type_line'):
                return True
                
            # Check if card is a Battle and not already on battlefield
            if "battle" not in card.type_line.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Initialize defense counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            # Get initial defense counter count
            import re
            match = re.search(r"defense\s+(\d+)", card.oracle_text.lower())
            initial_defense = int(match.group(1)) if match else 3  # Default to 3
            
            card.counters["defense"] = initial_defense
            logging.debug(f"Battle: {card.name} entered with {initial_defense} defense counters")
        
        elif event_type == "DEFENDER_DECLARES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'type_line'):
                return True
                
            # Check if card is a Battle
            if "battle" not in card.type_line.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Get defender
            defender_id = context.get("defender_id")
            defender = gs._safe_get_card(defender_id)
            
            if not defender or not hasattr(defender, 'power'):
                return True
                
            # Reduce defense counters
            if not hasattr(card, "counters"):
                card.counters = {}
                
            current_defense = card.counters.get("defense", 0)
            damage_amount = defender.power
            
            # Remove defense counters
            new_defense = max(0, current_defense - damage_amount)
            card.counters["defense"] = new_defense
            
            logging.debug(f"Battle: {card.name} defended, defense reduced from {current_defense} to {new_defense}")
            
            # Check if defense reached zero
            if new_defense == 0:
                # Check for back face (MDFC handling)
                if hasattr(card, 'is_mdfc') and card.is_mdfc and hasattr(card, 'back_face'):
                    # Exile the battle
                    gs.move_card(card_id, controller, "battlefield", controller, "exile")
                    
                    # Allow casting the back face from exile
                    if not hasattr(gs, 'cards_castable_from_exile'):
                        gs.cards_castable_from_exile = set()
                        
                    gs.cards_castable_from_exile.add(card_id)
                    
                    # Mark that this should be cast as the back face
                    if not hasattr(gs, 'cast_as_back_face'):
                        gs.cast_as_back_face = set()
                        
                    gs.cast_as_back_face.add(card_id)
                    
                    logging.debug(f"Battle: {card.name} exiled, back face can be cast")
                else:
                    # No back face, just sacrifice
                    gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                    logging.debug(f"Battle: {card.name} sacrificed with 0 defense")
        
        return True
        
    def _apply_mdfc(self, card_id, event_type, context=None):
        """Apply modal double-faced card effects"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and not context.get("casting_back_face"):
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            # Check if card is an MDFC - needs a property or method to detect this
            if not hasattr(card, 'is_mdfc') or not card.is_mdfc:
                return True
                
            # Check if casting back face is requested in context
            cast_back_face = context.get("cast_back_face", False)
            
            # If casting back face, use its properties
            if cast_back_face and hasattr(card, 'back_face'):
                back_face = card.back_face
                
                # Apply back face cost
                if "mana_cost" in context and hasattr(back_face, 'mana_cost'):
                    context["mana_cost"] = gs.mana_system.parse_mana_cost(back_face.mana_cost)
                    
                # Flag that we're casting the back face
                context["casting_back_face"] = True
                
                logging.debug(f"MDFC: Casting back face of {card.name}")
        
        elif event_type == "ENTERS_BATTLEFIELD" and context and context.get("casting_back_face"):
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            # Check if card is an MDFC and if back face was cast
            if (not hasattr(card, 'is_mdfc') or not card.is_mdfc or 
                not context.get("casting_back_face")):
                return True
                
            # If back face was cast, use those properties on the battlefield
            if hasattr(card, 'back_face'):
                back_face = card.back_face
                
                # Use back face properties while keeping the same card ID
                if hasattr(back_face, 'name'):
                    card.name = back_face.name
                    
                if hasattr(back_face, 'type_line'):
                    card.type_line = back_face.type_line
                    
                if hasattr(back_face, 'card_types'):
                    card.card_types = back_face.card_types.copy()
                    
                if hasattr(back_face, 'subtypes'):
                    card.subtypes = back_face.subtypes.copy()
                    
                if hasattr(back_face, 'power') and hasattr(back_face, 'toughness'):
                    card.power = back_face.power
                    card.toughness = back_face.toughness
                    
                if hasattr(back_face, 'oracle_text'):
                    card.oracle_text = back_face.oracle_text
                    
                if hasattr(back_face, 'colors'):
                    card.colors = back_face.colors.copy()
                    
                # Mark that this card is using its back face
                card.using_back_face = True
                
                logging.debug(f"MDFC: {card.name} entered as back face")
        
        return True
    
    def _apply_adventure(self, card_id, event_type, context=None):
        """Apply adventure card effects"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check if card has an adventure
            if "adventure" not in card.oracle_text.lower():
                return True
                
            # Check if casting as adventure
            if context.get("cast_as_adventure"):
                # Parse adventure cost and effect
                adventure_parts = self._parse_adventure_parts(card)
                if not adventure_parts:
                    return True
                    
                adventure_name = adventure_parts.get("name")
                adventure_cost = adventure_parts.get("cost")
                
                # Apply adventure cost
                if adventure_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                    # Replace regular cost with adventure cost
                    context["mana_cost"] = gs.mana_system.parse_mana_cost(adventure_cost)
                    logging.debug(f"Adventure: Casting {card.name} as adventure {adventure_name}")
                    
                    # Flag for exile instead of graveyard
                    if not hasattr(gs, 'adventure_cards'):
                        gs.adventure_cards = set()
                        
                    gs.adventure_cards.add(card_id)
                    
                    # Save original oracle text if needed
                    if not hasattr(card, 'original_oracle_text'):
                        card.original_oracle_text = card.oracle_text
                        
                    # Set oracle text to adventure effect
                    adventure_effect = adventure_parts.get("effect", "")
                    card.oracle_text = adventure_effect
        
        elif event_type == "SPELL_RESOLVES" and hasattr(gs, 'adventure_cards') and card_id in gs.adventure_cards:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not controller:
                return True
                
            # Exile instead of going to graveyard
            controller["exile"].append(card_id)
            gs.adventure_cards.remove(card_id)
            
            # Restore original oracle text
            if hasattr(card, 'original_oracle_text'):
                card.oracle_text = card.original_oracle_text
                
            # Mark this card as available to cast from exile
            if not hasattr(gs, 'cards_castable_from_exile'):
                gs.cards_castable_from_exile = set()
                
            gs.cards_castable_from_exile.add(card_id)
            
            logging.debug(f"Adventure: {card.name} exiled after adventure, can be cast as creature")
            
            # Prevent default move to graveyard
            context["skip_default_movement"] = True
        
        elif event_type == "CAST_FROM_EXILE" and context:
            if not hasattr(gs, 'cards_castable_from_exile') or card_id not in gs.cards_castable_from_exile:
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["exile"]:
                return True
                
            # Cast the creature from exile
            controller["exile"].remove(card_id)
            gs.stack.append(("SPELL", card_id, controller))
            
            # Remove from castable tracking
            gs.cards_castable_from_exile.remove(card_id)
            
            logging.debug(f"Adventure: Casting {gs._safe_get_card(card_id).name} from exile")
        
        return True
    
    
    def _apply_champion(self, card_id, event_type, context=None):
        """
        Champion ability: Exile a creature when this enters, return it when this leaves.
        """
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
            
            if not controller:
                return True
            
            # Find a creature to champion
            creatures = [cid for cid in controller["battlefield"] 
                        if gs._safe_get_card(cid) and 
                        hasattr(gs._safe_get_card(cid), 'card_types') and 
                        'creature' in gs._safe_get_card(cid).card_types and 
                        cid != card_id]
            
            if creatures:
                # Choose first creature for simplicity
                championed_creature = creatures[0]
                
                # Exile the championed creature
                gs.move_card(championed_creature, controller, "battlefield", controller, "exile")
                
                # Track the championed creature
                if not hasattr(controller, "championed_cards"):
                    controller["championed_cards"] = {}
                
                controller["championed_cards"][card_id] = championed_creature
                
                logging.debug(f"Champion: {gs._safe_get_card(card_id).name} championed {gs._safe_get_card(championed_creature).name}")
        
        elif event_type == "LEAVES_BATTLEFIELD":
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if hasattr(player, "championed_cards") and card_id in player["championed_cards"]:
                    controller = player
                    break
            
            if controller and hasattr(controller, "championed_cards"):
                # Return championed creature
                championed_creature = controller["championed_cards"].get(card_id)
                if championed_creature:
                    gs.move_card(championed_creature, controller, "exile", controller, "battlefield")
                    del controller["championed_cards"][card_id]
                    
                    logging.debug(f"Champion: Returned championed creature {gs._safe_get_card(championed_creature).name}")
        
        return True

    def _apply_changeling(self, card_id, event_type, context=None):
        # Modifies subtype checks. The logic needs to be in places where subtypes are checked.
        # This handler just confirms the keyword exists.
        return True

    def _apply_cipher(self, card_id, event_type, context=None):
        """
        Cipher ability: When the encoded spell deals combat damage, you may cast a copy
        """
        gs = self.game_state
        
        if event_type == "COMBAT_DAMAGE" and context and context.get("is_attacking"):
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
            
            if not controller:
                return True
            
            # Check if this creature has a ciphered spell
            if not hasattr(controller, "ciphered_spells"):
                controller["ciphered_spells"] = {}
            
            ciphered_spell = controller["ciphered_spells"].get(card_id)
            
            if ciphered_spell:
                # Cast a copy of the ciphered spell
                gs.stack.append(("SPELL", ciphered_spell, controller, {"is_copy": True}))
                logging.debug(f"Cipher: Cast spell {gs._safe_get_card(ciphered_spell).name} from cipher")
        
        return True
    
    def _apply_storm(self, card_id, event_type, context=None):
        """Apply storm ability effects (copy spell for each spell cast before it this turn)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "storm" not in card.oracle_text.lower():
                return True
            
            controller = context.get("controller")
            if not controller:
                return True
            
            # Mark that this spell has storm for resolution
            context["has_storm"] = True
            
            # Let game_state handle storm resolution
            # Storm will be handled in the _resolve_spell method
            return True
        
        return True
    
    def _apply_equip(self, card_id, event_type, context=None):
        """Apply equip ability effects (attach equipment to creature)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("equip"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse equip cost
            import re
            match = re.search(r"equip [^\(]([^\)]+)", card.oracle_text.lower())
            equip_cost = match.group(1) if match else None
            
            if not equip_cost or not hasattr(gs, 'mana_system'):
                return True
                
            controller = context.get("controller")
            target_id = context.get("target_id")
            
            if not controller or not target_id:
                return True
                
            # Check if controller can pay cost
            equip_parsed = gs.mana_system.parse_mana_cost(equip_cost)
            if not gs.mana_system.can_pay_mana_cost(controller, equip_parsed):
                logging.debug(f"Equip: Cannot pay equip cost for {card.name}")
                return True
                
            # Check if target is a valid creature
            target_card = gs._safe_get_card(target_id)
            if not target_card or not hasattr(target_card, 'card_types') or 'creature' not in target_card.card_types:
                logging.debug(f"Equip: Target is not a creature")
                return True
                
            # Check if target is controlled by the same player
            if target_id not in controller["battlefield"]:
                logging.debug(f"Equip: Target creature is not controlled by the same player")
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, equip_parsed)
            
            # Attach equipment to creature
            if not hasattr(controller, "attachments"):
                controller["attachments"] = {}
                
            controller["attachments"][card_id] = target_id
            logging.debug(f"Equip: Attached {card.name} to {target_card.name}")
            
        elif event_type == "LEAVES_BATTLEFIELD" and context:
            # Handle when equipped creature leaves battlefield
            leaving_card_id = card_id
            
            # Find equipment attached to this creature
            for player in [gs.p1, gs.p2]:
                if hasattr(player, "attachments"):
                    attached_equipment = [equip_id for equip_id, target_id in player["attachments"].items() 
                                        if target_id == leaving_card_id]
                    
                    # Unattach equipment
                    for equip_id in attached_equipment:
                        del player["attachments"][equip_id]
                        logging.debug(f"Equip: {gs._safe_get_card(equip_id).name} became unattached")
        
        return True
    
    def _apply_delve(self, card_id, event_type, context=None):
        """Apply delve ability effects (exile cards from graveyard to reduce cost)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "delve" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller or not hasattr(gs, 'mana_system'):
                return True
                
            # Check for delve cost reduction
            delve_count = context.get("delve_count", 0)
            if delve_count > 0 and "mana_cost" in context:
                # Each card exiled reduces generic mana cost by 1
                if "generic" in context["mana_cost"]:
                    context["mana_cost"]["generic"] = max(0, context["mana_cost"]["generic"] - delve_count)
                    
                # Exile the specified number of cards
                if len(controller["graveyard"]) >= delve_count:
                    # In a real game, player would choose which cards to exile
                    cards_to_exile = controller["graveyard"][:delve_count]
                    
                    for exile_id in cards_to_exile:
                        controller["graveyard"].remove(exile_id)
                        controller["exile"].append(exile_id)
                        
                    logging.debug(f"Delve: Exiled {delve_count} cards to reduce cost of {card.name}")
        
        return True
    
    def _apply_bestow(self, card_id, event_type, context=None):
        """Apply bestow ability effects (can cast as Aura or creature)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("bestow"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "bestow" not in card.oracle_text.lower():
                return True
                
            # Parse bestow cost
             
            match = re.search(r"bestow [^\(]([^\)]+)", card.oracle_text.lower())
            bestow_cost = match.group(1) if match else None
            
            if bestow_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with bestow cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(bestow_cost)
                
                # Mark it as being cast as an Aura
                context["cast_as_aura"] = True
                
                # The spell becomes an Aura Enchantment
                if not hasattr(card, "original_card_types"):
                    card.original_card_types = card.card_types.copy() if hasattr(card, 'card_types') else []
                    
                if hasattr(card, 'card_types'):
                    if 'enchantment' not in card.card_types:
                        card.card_types.append('enchantment')
                    if 'creature' in card.card_types:
                        card.card_types.remove('creature')
                        
                if hasattr(card, 'subtypes'):
                    if 'aura' not in card.subtypes:
                        card.subtypes.append('aura')
                        
                logging.debug(f"Bestow: {card.name} is being cast as an Aura using bestow")
                
        elif event_type == "LEAVES_BATTLEFIELD" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "bestow" not in card.oracle_text.lower():
                return True
                
            # If an Aura with bestow is no longer attached, it becomes a creature
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check if it was an Aura and is no longer attached
            is_aura = hasattr(card, 'card_types') and 'enchantment' in card.card_types and hasattr(card, 'subtypes') and 'aura' in card.subtypes
            is_attached = hasattr(controller, "attachments") and card_id in controller["attachments"]
            
            if is_aura and not is_attached:
                # Restore original card types
                if hasattr(card, "original_card_types"):
                    card.card_types = card.original_card_types
                    del card.original_card_types
                    
                # Remove Aura subtype if needed
                if hasattr(card, 'subtypes') and 'aura' in card.subtypes:
                    card.subtypes.remove('aura')
                    
                logging.debug(f"Bestow: {card.name} became a creature after being unattached")
        
        return True
    
    def _apply_blitz(self, card_id, event_type, context=None):
        """Apply blitz ability effects (cheaper casting with haste, draw a card, sacrifice at end of turn)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("blitz"):
            # Handle casting with blitz
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "blitz" not in card.oracle_text.lower():
                return True
                
            # Parse blitz cost
             
            match = re.search(r"blitz [^\(]([^\)]+)", card.oracle_text.lower())
            blitz_cost = match.group(1) if match else None
            
            if blitz_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with blitz cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(blitz_cost)
                
                # Flag for return to hand at end of turn
                if not hasattr(gs, 'blitz_cards'):
                    gs.blitz_cards = set()
                    
                gs.blitz_cards.add(card_id)
                logging.debug(f"Blitz: {card.name} cast for blitz cost")
            
        elif event_type == "ENTERS_BATTLEFIELD" and hasattr(gs, 'blitz_cards') and card_id in gs.blitz_cards:
            # Grant haste to blitzed creature
            card = gs._safe_get_card(card_id)
            if card:
                # Grant haste
                if not hasattr(gs, 'has_haste_until_eot'):
                    gs.has_haste_until_eot = set()
                    
                gs.has_haste_until_eot.add(card_id)
                logging.debug(f"Blitz: {card.name} gained haste")
        
        elif event_type == "DIES" and hasattr(gs, 'blitz_cards') and card_id in gs.blitz_cards:
            # Draw a card when the blitzed creature dies
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["graveyard"]:
                        controller = player
                        break
                        
            if controller:
                gs._draw_phase(controller)
                logging.debug(f"Blitz: Drew a card from {gs._safe_get_card(card_id).name} dying")
                
        elif event_type == "END_STEP" and hasattr(gs, 'blitz_cards') and card_id in gs.blitz_cards:
            # Sacrifice at end of turn
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if controller:
                # Sacrifice the creature
                gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                gs.blitz_cards.remove(card_id)
                logging.debug(f"Blitz: Sacrificed {gs._safe_get_card(card_id).name} at end of turn")
        
        return True    
    
    def _apply_cycling(self, card_id, event_type, context=None):
        """Apply cycling ability effects (discard to draw a card)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("cycling"):
            # Check if card is in hand
            controller = context.get("controller")
            if not controller or card_id not in controller["hand"]:
                return True
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse cycling cost
             
            match = re.search(r"cycling [^\(]([^\)]+)", card.oracle_text.lower())
            cycling_cost = match.group(1) if match else None
            
            if cycling_cost and hasattr(gs, 'mana_system'):
                # Check if controller can pay
                cycling_parsed = gs.mana_system.parse_mana_cost(cycling_cost)
                
                if gs.mana_system.can_pay_mana_cost(controller, cycling_parsed):
                    gs.mana_system.pay_mana_cost(controller, cycling_parsed)
                    
                    # Discard the card
                    controller["hand"].remove(card_id)
                    controller["graveyard"].append(card_id)
                    
                    # Draw a card
                    gs._draw_phase(controller)
                    
                    logging.debug(f"Cycling: Discarded {card.name} to draw a card")
                    
                    # Trigger "when you cycle" abilities on battlefield
                    for permanent_id in controller["battlefield"]:
                        permanent = gs._safe_get_card(permanent_id)
                        if permanent and hasattr(permanent, 'oracle_text') and "whenever you cycle" in permanent.oracle_text.lower():
                            gs.trigger_ability(permanent_id, "CYCLING_TRIGGER", {"cycling_card_id": card_id})
                    
            return True
        
        return True


    def _apply_flying(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's _check_block_restrictions
        return True


    def _apply_foretell(self, card_id, event_type, context=None):
        """Apply foretell ability effects"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and "from_hand" in context:
            # Move card to exile and mark it as foretold
            controller = context.get("controller")
            if controller:
                # Check if foretold cards tracking exists
                if not hasattr(controller, "foretold_cards"):
                    controller["foretold_cards"] = set()
                    
                gs.move_card(card_id, controller, "hand", controller, "exile")
                controller["foretold_cards"].add(card_id)
                logging.debug(f"Card {gs._safe_get_card(card_id).name} foretold")
                return True
        
        elif event_type == "CAST" and context and "from_exile" in context:
            # Check if card was foretold
            controller = context.get("controller")
            if controller and hasattr(controller, "foretold_cards") and card_id in controller["foretold_cards"]:
                # Apply cost reduction
                if "mana_cost" in context:
                    context["mana_cost"]["generic"] = max(0, context["mana_cost"]["generic"] - 2)
                
                # Remove from foretold cards
                controller["foretold_cards"].remove(card_id)
                return True
        
        return True

    def _apply_absorb(self, card_id, event_type, context=None):
        """Apply absorb ability effects (Prevents damage to creatures)"""
        if event_type == "DEALS_DAMAGE" and context and "damage_amount" in context:
            # Get the card's absorb value
            card = self.game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
            
            # Parse absorb value
            match = re.search(r"absorb (\d+)", card.oracle_text.lower())
            if match:
                absorb_value = int(match.group(1))
                original_damage = context["damage_amount"]
                # Reduce damage by absorb amount
                context["damage_amount"] = max(0, original_damage - absorb_value)
                logging.debug(f"Absorb {absorb_value} reduced damage from {original_damage} to {context['damage_amount']}")
        
        return True

    def _apply_affinity(self, card_id, event_type, context=None):
        # Handled in Mana System during cost calculation.
        return True

    def _apply_annihilator(self, card_id, event_type, context=None):
        """Apply annihilator ability effects (Forces opponent to sacrifice permanents when attacking)"""
        if event_type == "ATTACKS" and context:
            # Get the card
            card = self.game_state._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
            
            # Parse annihilator value
             
            match = re.search(r"annihilator (\d+)", card.oracle_text.lower())
            if match:
                annihilator_value = int(match.group(1))
                
                # Find the defending player
                gs = self.game_state
                attacker = context.get("controller")
                defender = gs.p2 if attacker == gs.p1 else gs.p1
                
                # Defender must sacrifice permanents
                permanents_to_sacrifice = min(annihilator_value, len(defender["battlefield"]))
                for _ in range(permanents_to_sacrifice):
                    if defender["battlefield"]:
                        # In a real game, defender would choose what to sacrifice
                        # Here we'll make a simple choice - sacrifice the least valuable permanent
                        
                        # Try to find lands first
                        lands = [cid for cid in defender["battlefield"] 
                                if gs._safe_get_card(cid) and 'land' in gs._safe_get_card(cid).type_line]
                        if lands:
                            sacrifice_id = lands[0]
                        else:
                            # Try to find lowest value creature
                            creatures = [cid for cid in defender["battlefield"] 
                                        if gs._safe_get_card(cid) and 'creature' in gs._safe_get_card(cid).card_types]
                            if creatures:
                                sacrifice_id = min(creatures, 
                                                key=lambda cid: gs._safe_get_card(cid).power + gs._safe_get_card(cid).toughness 
                                                if hasattr(gs._safe_get_card(cid), 'power') and hasattr(gs._safe_get_card(cid), 'toughness') else 0)
                            else:
                                # Just take the first permanent
                                sacrifice_id = defender["battlefield"][0]
                        
                        # Sacrifice the permanent
                        gs.move_card(sacrifice_id, defender, "battlefield", defender, "graveyard")
                        logging.debug(f"Annihilator {annihilator_value} forced sacrifice of {gs._safe_get_card(sacrifice_id).name}")
        
        return True

    def _apply_bloodthirst(self, card_id, event_type, context=None):
        """Apply bloodthirst ability effects (Enters with +1/+1 counters if opponent lost life)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Find controller and opponent
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            # Check if bloodthirst condition is met (opponent lost life this turn)
            if opponent.get("lost_life_this_turn", False):
                # Parse bloodthirst value
                 
                match = re.search(r"bloodthirst (\d+)", card.oracle_text.lower())
                bloodthirst_value = int(match.group(1)) if match else 1
                
                # Add +1/+1 counters
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + bloodthirst_value
                
                # Apply counter effect
                card.power += bloodthirst_value
                card.toughness += bloodthirst_value
                
                logging.debug(f"Bloodthirst: {card.name} entered with {bloodthirst_value} +1/+1 counters")
        
        return True

    def _apply_bushido(self, card_id, event_type, context=None):
        """Apply bushido ability effects (Gets +N/+N when blocks or becomes blocked)"""
        gs = self.game_state
        
        if event_type in ["BLOCKS", "BLOCKED"] and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse bushido value
             
            match = re.search(r"bushido (\d+)", card.oracle_text.lower())
            bushido_value = int(match.group(1)) if match else 1
            
            # Apply bushido buff until end of turn
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if controller:
                # Set up temporary buffs tracking
                if not hasattr(controller, "temp_buffs"):
                    controller["temp_buffs"] = {}
                    
                if card_id not in controller["temp_buffs"]:
                    controller["temp_buffs"][card_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                    
                # Add bushido bonus
                controller["temp_buffs"][card_id]["power"] += bushido_value
                controller["temp_buffs"][card_id]["toughness"] += bushido_value
                
                logging.debug(f"Bushido: {card.name} got +{bushido_value}/+{bushido_value} until end of turn")
        
        return True
    

    def _apply_cumulative_upkeep(self, card_id, event_type, context=None):
        """Apply cumulative upkeep ability effects (Increasing cost each turn or sacrifice)"""
        gs = self.game_state
        
        if event_type == "UPKEEP" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Find controller
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Initialize age counter if it doesn't exist
            if not hasattr(card, "age_counters"):
                card.age_counters = 0
                
            # Increment age counter
            card.age_counters += 1
            
            # Parse cumulative upkeep cost
             
            match = re.search(r"cumulative upkeep [^\(]([^\)]+)", card.oracle_text.lower())
            upkeep_cost = match.group(1) if match else "{1}"
            
            # For simplicity, we'll handle only mana costs and sacrifice
            if "sacrifice" in upkeep_cost:
                # Can't pay with sacrifice, so sacrifice the permanent
                gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                logging.debug(f"Cumulative upkeep: Sacrificed {card.name} due to inability to pay")
            else:
                # Try to pay mana cost
                if hasattr(gs, 'mana_system'):
                    # Scale by age counters
                    upkeep_mana = gs.mana_system.parse_mana_cost(upkeep_cost)
                    for color in upkeep_mana:
                        if color != 'generic':
                            upkeep_mana[color] *= card.age_counters
                        else:
                            upkeep_mana[color] = upkeep_mana[color] * card.age_counters
                            
                    # Check if controller can pay
                    if gs.mana_system.can_pay_mana_cost(controller, upkeep_mana):
                        gs.mana_system.pay_mana_cost(controller, upkeep_mana)
                        logging.debug(f"Cumulative upkeep: Paid for {card.name} ({card.age_counters} age counters)")
                    else:
                        # Can't pay, sacrifice the permanent
                        gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                        logging.debug(f"Cumulative upkeep: Sacrificed {card.name} due to inability to pay")
        
        return True

    def _apply_dredge(self, card_id, event_type, context=None):
        """Apply dredge ability effects (Return from graveyard instead of drawing)"""
        gs = self.game_state
        
        if event_type == "DRAW_REPLACEMENT" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check if card has dredge and is in graveyard
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            # Parse dredge value
             
            match = re.search(r"dredge (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            dredge_value = int(match.group(1))
            
            # Check if there are enough cards in library to mill
            if len(controller["library"]) < dredge_value:
                return True  # Can't dredge if not enough cards to mill
                
            # Mill cards
            milled_cards = []
            for _ in range(dredge_value):
                if controller["library"]:
                    milled_card = controller["library"].pop(0)
                    controller["graveyard"].append(milled_card)
                    milled_cards.append(milled_card)
            
            # Return the dredge card to hand
            controller["graveyard"].remove(card_id)
            controller["hand"].append(card_id)
            
            logging.debug(f"Dredge: Returned {card.name} to hand by milling {dredge_value} cards")
            
            # Indicate that draw was replaced
            context["draw_replaced"] = True
        
        return True

    def _apply_echo(self, card_id, event_type, context=None):
        """Apply echo ability effects (Pay cost or sacrifice next upkeep)"""
        gs = self.game_state
        
        if event_type == "UPKEEP" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check if card has echo
            if "echo" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Check if card entered last turn
            if hasattr(gs, 'entered_last_turn') and card_id in gs.entered_last_turn:
                # Parse echo cost - if not specified, assume same as mana cost
                echo_cost = None
                 
                match = re.search(r"echo [^\(]([^\)]+)", card.oracle_text.lower())
                if match:
                    echo_cost = match.group(1)
                else:
                    echo_cost = card.mana_cost if hasattr(card, 'mana_cost') else None
                    
                if echo_cost and hasattr(gs, 'mana_system'):
                    # Check if controller can and wants to pay
                    parsed_cost = gs.mana_system.parse_mana_cost(echo_cost)
                    if gs.mana_system.can_pay_mana_cost(controller, parsed_cost):
                        # In a real game, player would choose whether to pay
                        # For AI, we'll always pay if we can
                        gs.mana_system.pay_mana_cost(controller, parsed_cost)
                        logging.debug(f"Echo: Paid echo cost for {card.name}")
                    else:
                        # Can't pay, sacrifice the permanent
                        gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                        logging.debug(f"Echo: Sacrificed {card.name} due to inability to pay echo")
                else:
                    # No cost information, sacrifice as fallback
                    gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                    logging.debug(f"Echo: Sacrificed {card.name} (default behavior)")
        
        return True

    def _apply_evoke(self, card_id, event_type, context=None):
        """Apply evoke ability effects (Cast for alternative cost and sacrifice)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("evoke"):
            # Apply evoke alternative cost
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse evoke cost
             
            match = re.search(r"evoke [^\(]([^\)]+)", card.oracle_text.lower())
            evoke_cost = match.group(1) if match else None
            
            if evoke_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with evoke cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(evoke_cost)
                logging.debug(f"Evoke: Using alternative cost for {card.name}")
                
                # Flag for sacrifice when enters battlefield
                if not hasattr(gs, 'evoked_cards'):
                    gs.evoked_cards = set()
                    
                gs.evoked_cards.add(card_id)
        
        elif event_type == "ENTERS_BATTLEFIELD" and hasattr(gs, 'evoked_cards') and card_id in gs.evoked_cards:
            # Sacrifice the evoked creature after ETB triggers
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if controller:
                # Sacrifice after ETB triggers resolve
                gs.move_card(card_id, controller, "battlefield", controller, "graveyard")
                gs.evoked_cards.remove(card_id)
                logging.debug(f"Evoke: Sacrificed {card.name} after ETB triggers")
        
        return True

    def _apply_flash(self, card_id, event_type, context=None):
        # Allows casting at instant speed - handled by Action Handler's validation
        return True


    def _apply_kicker(self, card_id, event_type, context=None):
        """Apply kicker ability effects (Optional additional cost for effect)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("kicked"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse kicker cost
             
            match = re.search(r"kicker [^\(]([^\)]+)", card.oracle_text.lower())
            kicker_cost = match.group(1) if match else None
            
            if kicker_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                controller = context.get("controller")
                if not controller:
                    return True
                    
                # Add kicker cost to regular cost
                kicker_parsed = gs.mana_system.parse_mana_cost(kicker_cost)
                for color, amount in kicker_parsed.items():
                    if color in context["mana_cost"]:
                        context["mana_cost"][color] += amount
                    else:
                        context["mana_cost"][color] = amount
                        
                # Flag that spell was kicked
                if not hasattr(gs, 'kicked_cards'):
                    gs.kicked_cards = set()
                    
                gs.kicked_cards.add(card_id)
                logging.debug(f"Kicker: {card.name} was kicked for additional effects")
        
        elif event_type == "ENTERS_BATTLEFIELD" and hasattr(gs, 'kicked_cards') and card_id in gs.kicked_cards:
            # Apply kicked ETB effects
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            logging.debug(f"Kicker: Applying kicked ETB effects for {card.name}")
            # The specific effects would be handled by trigger_ability
            gs.trigger_ability(card_id, "KICKED_ETB")
            gs.kicked_cards.remove(card_id)
        
        return True

    def _apply_morph(self, card_id, event_type, context=None):
        """Apply morph ability effects (Cast face-down as 2/2, turn face-up later)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("morph"):
            # Cast face-down as a 2/2 creature
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "morph" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Replace mana cost with {3}
            if "mana_cost" in context:
                context["mana_cost"] = {"generic": 3}
                
            # Flag that this is a morphed card
            if not hasattr(gs, 'morphed_cards'):
                gs.morphed_cards = {}
                
            gs.morphed_cards[card_id] = {"face_down": True, "original": {
                "power": card.power if hasattr(card, 'power') else 0,
                "toughness": card.toughness if hasattr(card, 'toughness') else 0,
                "types": card.card_types.copy() if hasattr(card, 'card_types') else [],
                "subtypes": card.subtypes.copy() if hasattr(card, 'subtypes') else [],
                "name": card.name if hasattr(card, 'name') else "Unknown"
            }}
            
            # Modify card properties to be a 2/2 colorless creature
            card.power = 2
            card.toughness = 2
            card.card_types = ['creature']
            card.subtypes = []
            card.name = "Face-down Creature"
            
            logging.debug(f"Morph: {gs.morphed_cards[card_id]['original']['name']} cast face-down as a 2/2 creature")
        
        elif event_type == "ACTIVATE" and context and context.get("unmorph") and card_id in getattr(gs, 'morphed_cards', {}):
            # Turn face-up by paying morph cost
            morphed_info = gs.morphed_cards.get(card_id)
            if not morphed_info or not morphed_info["face_down"]:
                return True
                
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            # Parse morph cost
             
            match = re.search(r"morph [^\(]([^\)]+)", card.oracle_text.lower())
            morph_cost = match.group(1) if match else None
            
            if morph_cost and hasattr(gs, 'mana_system'):
                # Check if controller can pay
                morph_parsed = gs.mana_system.parse_mana_cost(morph_cost)
                
                if gs.mana_system.can_pay_mana_cost(controller, morph_parsed):
                    gs.mana_system.pay_mana_cost(controller, morph_parsed)
                    
                    # Restore original card properties
                    card.power = morphed_info["original"]["power"]
                    card.toughness = morphed_info["original"]["toughness"]
                    card.card_types = morphed_info["original"]["types"]
                    card.subtypes = morphed_info["original"]["subtypes"]
                    card.name = morphed_info["original"]["name"]
                    
                    # Mark as face-up
                    gs.morphed_cards[card_id]["face_down"] = False
                    
                    logging.debug(f"Morph: Turned {card.name} face-up")
                    
                    # Trigger "when turned face up" abilities
                    gs.trigger_ability(card_id, "TURNED_FACE_UP")
                else:
                    logging.debug(f"Morph: Cannot pay cost to turn face-up")
        
        return True
    
    def _apply_spree(self, card_id, event_type, context=None):
        """Apply Spree ability effects (choose multiple modes with additional costs)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_spree') or not card.is_spree:
                return True
                
            # Check if this is being cast with Spree modes
            if not context.get("is_spree"):
                return True
                
            # Get selected modes
            selected_modes = context.get("selected_modes", [])
            if not selected_modes:
                return True
                
            # Validate mode indices
            valid_modes = []
            for mode_idx in selected_modes:
                if hasattr(card, 'spree_modes') and mode_idx < len(card.spree_modes):
                    valid_modes.append(mode_idx)
                    
            # Update context with validated modes
            context["selected_modes"] = valid_modes
            
            # For each selected mode, apply any additional costs
            total_additional_cost = {}
            for mode_idx in valid_modes:
                mode = card.spree_modes[mode_idx]
                mode_cost = mode.get("cost", "")
                
                # Parse the cost
                if hasattr(gs, 'mana_system'):
                    parsed_cost = gs.mana_system.parse_mana_cost(mode_cost)
                    
                    # Add to total additional cost
                    for color, amount in parsed_cost.items():
                        if color in total_additional_cost:
                            total_additional_cost[color] += amount
                        else:
                            total_additional_cost[color] = amount
            
            # Store total additional cost in context
            context["additional_cost"] = total_additional_cost
            
            logging.debug(f"Spree: Casting {card.name} with {len(valid_modes)} additional modes")
            
        elif event_type == "SPELL_RESOLVES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'is_spree') or not card.is_spree:
                return True
                
            # Use the game state's spree resolution method
            if hasattr(gs, '_resolve_spree_spell'):
                controller = context.get("controller")
                if controller:
                    gs._resolve_spree_spell(card_id, controller, context)
                    return True
        
        return True

    def _resolve_spell_effect(self, effect_text, context=None):
        """
        Resolve a spell or ability effect based on its text.
        
        Args:
            effect_text: The text of the effect to resolve
            context: Additional context like targets and controller
        """
        if not context:
            return
            
        gs = self.game_state
        controller = context.get("controller")
        targets = context.get("targets", {})
        source_id = context.get("source_id")
        
        if not controller or not source_id:
            return
            
        # Normalize effect text
        effect_text = effect_text.lower()
        
        # Damage effects
        damage_match = re.search(r'deals?\s+(\d+)\s+damage', effect_text)
        if damage_match:
            damage_amount = int(damage_match.group(1))
            
            # Check for targets
            for target_type, target_ids in targets.items():
                for target_id in target_ids:
                    if target_type == "creatures" or target_type == "permanents":
                        # Deal damage to creature
                        target_card = gs._safe_get_card(target_id)
                        if target_card and hasattr(target_card, 'toughness'):
                            # Apply damage - in a real implementation this would use a damage system
                            if damage_amount >= target_card.toughness:
                                # Find target controller
                                for player in [gs.p1, gs.p2]:
                                    if target_id in player["battlefield"]:
                                        gs.move_card(target_id, player, "battlefield", player, "graveyard")
                                        break
                            logging.debug(f"Spree effect: Dealt {damage_amount} damage to {target_card.name}")
                    
                    elif target_type == "players":
                        # Deal damage to player
                        if target_id == "p1":
                            gs.p1["life"] -= damage_amount
                        elif target_id == "p2":
                            gs.p2["life"] -= damage_amount
                        logging.debug(f"Spree effect: Dealt {damage_amount} damage to player")
            
            # If no specific targets but mentions players or each opponent
            if "each opponent" in effect_text:
                # Deal damage to all opponents
                opponent = gs.p2 if controller == gs.p1 else gs.p1
                opponent["life"] -= damage_amount
                logging.debug(f"Spree effect: Dealt {damage_amount} damage to each opponent")
            
            return
        
        # Draw effects
        draw_match = re.search(r'draw\s+(\d+)\s+cards?', effect_text)
        if draw_match:
            draw_amount = int(draw_match.group(1))
            
            # Draw cards
            for _ in range(draw_amount):
                if controller["library"]:
                    card_id = controller["library"].pop(0)
                    controller["hand"].append(card_id)
                    
            logging.debug(f"Spree effect: Drew {draw_amount} cards")
            return
        
        # Discard effects
        discard_match = re.search(r'discard\s+(\d+)\s+cards?', effect_text)
        if discard_match:
            discard_amount = int(discard_match.group(1))
            
            # Find target player (if any)
            target_player = None
            for target_type, target_ids in targets.items():
                if target_type == "players" and target_ids:
                    player_id = target_ids[0]
                    target_player = gs.p1 if player_id == "p1" else gs.p2
                    break
            
            # Default to opponent if no target specified
            if not target_player and "target opponent" in effect_text:
                target_player = gs.p2 if controller == gs.p1 else gs.p1
            
            # Default to controller if still no target
            if not target_player:
                target_player = controller
                
            # Discard cards
            for _ in range(min(discard_amount, len(target_player["hand"]))):
                # In a real implementation, the player would choose
                card_id = target_player["hand"][0]
                target_player["hand"].remove(card_id)
                target_player["graveyard"].append(card_id)
            
            logging.debug(f"Spree effect: Discarded {discard_amount} cards")
            return
            
        # Token creation effects
        token_match = re.search(r'create\s+(.*?)\s+token', effect_text)
        if token_match:
            token_desc = token_match.group(1)
            
            # Create token tracking if it doesn't exist
            if not hasattr(controller, "tokens"):
                controller["tokens"] = []
                
            # Create token
            token_id = f"TOKEN_{len(controller['tokens'])}"
            
            # Create a simple token card object
            token = Card({
                "name": f"Token",
                "type_line": "creature",
                "card_types": ["creature"],
                "subtypes": [],
                "power": 1,
                "toughness": 1,
                "oracle_text": "",
                "keywords": [0] * 11  # Default no keywords
            })
            
            # Add token to game
            gs.card_db[token_id] = token
            controller["battlefield"].append(token_id)
            controller["tokens"].append(token_id)
            
            logging.debug(f"Spree effect: Created a token")
            return
        
        # Destruction effects
        if "destroy" in effect_text:
            # Handle target destruction
            for target_type, target_ids in targets.items():
                for target_id in target_ids:
                    if target_type in ["creatures", "artifacts", "enchantments", "permanents"]:
                        # Find target controller
                        for player in [gs.p1, gs.p2]:
                            if target_id in player["battlefield"]:
                                gs.move_card(target_id, player, "battlefield", player, "graveyard")
                                target_card = gs._safe_get_card(target_id)
                                logging.debug(f"Spree effect: Destroyed {target_card.name if target_card else 'permanent'}")
                                break
            
            # Mass destruction effects
            if "all creatures" in effect_text:
                for player in [gs.p1, gs.p2]:
                    # Find creatures
                    creatures = [cid for cid in player["battlefield"] 
                            if gs._safe_get_card(cid) and 
                            hasattr(gs._safe_get_card(cid), 'card_types') and 
                            'creature' in gs._safe_get_card(cid).card_types]
                    
                    # Destroy all creatures
                    for creature_id in creatures:
                        gs.move_card(creature_id, player, "battlefield", player, "graveyard")
                    
                logging.debug(f"Spree effect: Destroyed all creatures")
            
            return
        
        # Add more effect types as needed...
        
        # If no specific effect was handled, log a debug message
        logging.debug(f"Unhandled Spree effect: {effect_text}")
    
    def _apply_splice(self, card_id, event_type, context=None):
        """Apply splice ability effects (add text to another spell)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("splice_sources"):
            # The card being cast has had other cards spliced onto it
            card = gs._safe_get_card(card_id)
            splice_sources = context.get("splice_sources", [])
            
            if not card:
                return True
                
            # Track original text for restoration after resolution
            if not hasattr(card, 'original_oracle_text'):
                card.original_oracle_text = card.oracle_text if hasattr(card, 'oracle_text') else ""
                
            # Add spliced text
            additional_text = []
            for splice_id in splice_sources:
                splice_card = gs._safe_get_card(splice_id)
                if splice_card and hasattr(splice_card, 'oracle_text'):
                    # Extract the effect text (ignoring the splice instruction)
                    effect_text = splice_card.oracle_text
                    if "splice onto" in effect_text.lower():
                        effect_parts = effect_text.split('\n')
                        # Keep only non-splice parts
                        effect_text = '\n'.join([part for part in effect_parts 
                                            if "splice onto" not in part.lower()])
                    
                    additional_text.append(f"Spliced effect from {splice_card.name}: {effect_text}")
            
            # Combine original and spliced text
            if hasattr(card, 'oracle_text'):
                card.oracle_text = card.original_oracle_text + "\n" + "\n".join(additional_text)
                logging.debug(f"Splice: Added {len(splice_sources)} spliced effects to {card.name}")
        
        elif event_type == "SPLICE_ONTO" and context:
            # Check if this card has splice and can be spliced onto the target
            target_id = context.get("target_id")
            controller = context.get("controller")
            
            card = gs._safe_get_card(card_id)
            target_card = gs._safe_get_card(target_id)
            
            if not card or not hasattr(card, 'oracle_text') or not target_card:
                return True
                
            # Check for splice keyword and valid target
            oracle_text = card.oracle_text.lower()
            
            if "splice onto" not in oracle_text:
                return True
                
            # Check what can be spliced onto
            splice_target_type = None
            if "splice onto arcane" in oracle_text:
                splice_target_type = "arcane"
            
            # Check if target matches splice requirement
            is_valid_target = False
            if splice_target_type == "arcane" and hasattr(target_card, 'subtypes'):
                if "arcane" in [subtype.lower() for subtype in target_card.subtypes]:
                    is_valid_target = True
            
            if not is_valid_target:
                return True
                
            # Parse splice cost
            match = re.search(r"splice onto [^\(]([^\)]+)", oracle_text)
            splice_cost = match.group(1) if match else None
            
            if splice_cost and hasattr(gs, 'mana_system'):
                # Check if controller can pay
                splice_parsed = gs.mana_system.parse_mana_cost(splice_cost)
                
                if gs.mana_system.can_pay_mana_cost(controller, splice_parsed):
                    gs.mana_system.pay_mana_cost(controller, splice_parsed)
                    
                    # Add this card to the splice sources
                    if "splice_sources" not in context:
                        context["splice_sources"] = []
                        
                    context["splice_sources"].append(card_id)
                    
                    logging.debug(f"Splice: Spliced {card.name} onto {target_card.name}")
                    
                    # Card stays in hand
                    return True
        
        return True
    
    def _apply_sunburst(self, card_id, event_type, context=None):
        """Apply sunburst ability effects (adds counters equal to colors of mana spent)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for sunburst
            if "sunburst" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # In a real game, this would be based on colors of mana spent to cast
            # For this simulation, we'll use the mana pool as a proxy
            colors_used = 0
            for color in ['W', 'U', 'B', 'R', 'G']:
                if controller["mana_pool"].get(color, 0) > 0:
                    colors_used += 1
                    
            if colors_used > 0:
                # Add appropriate counters
                if 'artifact' in card.card_types:
                    # Artifacts get charge counters
                    if not hasattr(card, "counters"):
                        card.counters = {}
                        
                    card.counters["charge"] = card.counters.get("charge", 0) + colors_used
                    logging.debug(f"Sunburst: {card.name} entered with {colors_used} charge counters")
                elif 'creature' in card.card_types:
                    # Creatures get +1/+1 counters
                    if not hasattr(card, "counters"):
                        card.counters = {}
                        
                    card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + colors_used
                    
                    # Apply counter effect
                    card.power += colors_used
                    card.toughness += colors_used
                    
                    logging.debug(f"Sunburst: {card.name} entered with {colors_used} +1/+1 counters")
        
        return True

    def _apply_devoid(self, card_id, event_type, context=None):
        """Apply devoid ability effects (card has no color)"""
        gs = self.game_state
        
        # Devoid is a static ability that modifies card characteristics
        if event_type == "CHECK_COLOR":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for devoid
            if "devoid" in card.oracle_text.lower():
                # The card has no color
                return False  # Card has no color
        
        # When a card with devoid enters the battlefield, make sure it has no color
        elif event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for devoid
            if "devoid" in card.oracle_text.lower():
                # Set the card's colors to colorless
                if hasattr(card, 'colors'):
                    card.colors = [0, 0, 0, 0, 0]  # WUBRG all 0
                    logging.debug(f"Devoid: {card.name} is colorless")
        
        return True

    def _apply_jumpstart(self, card_id, event_type, context=None):
        """Apply jump-start ability effects (cast from graveyard by discarding a card)"""
        gs = self.game_state
        
        if event_type == "CAST_FROM_GRAVEYARD" and context:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check for jump-start
            if "jump-start" not in card.oracle_text.lower():
                return True
                
            # Check if card is in graveyard
            if card_id not in controller["graveyard"]:
                return True
                
            # Check if controller has a card to discard
            if not controller["hand"]:
                return True
                
            # Choose a card to discard (in a real game, player would choose)
            if controller["hand"]:
                discard_idx = 0
                discard_id = controller["hand"][discard_idx]
                controller["hand"].pop(discard_idx)
                controller["graveyard"].append(discard_id)
                
                # Move jump-start card from graveyard to stack
                controller["graveyard"].remove(card_id)
                gs.stack.append(("SPELL", card_id, controller, {"jump_start": True}))
                
                logging.debug(f"Jump-start: Discarded a card to cast {card.name} from graveyard")
                
                # Flag for exile instead of graveyard
                if not hasattr(gs, 'jump_start_cards'):
                    gs.jump_start_cards = set()
                    
                gs.jump_start_cards.add(card_id)
        
        elif event_type == "SPELL_RESOLVES" and hasattr(gs, 'jump_start_cards') and card_id in gs.jump_start_cards:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not controller:
                return True
                
            # Exile instead of going to graveyard
            controller["exile"].append(card_id)
            gs.jump_start_cards.remove(card_id)
            logging.debug(f"Jump-start: Exiled {card.name} after resolution")
            
            # Prevent default move to graveyard
            context["skip_default_movement"] = True
        
        return True

    def _apply_shadow(self, card_id, event_type, context=None):
        """Apply shadow ability effects (can only block or be blocked by shadow creatures)"""
        gs = self.game_state
        
        if event_type == "BLOCKING":
            if context and "attacker_id" in context and "blocker_id" in context:
                attacker_id = context["attacker_id"]
                blocker_id = context["blocker_id"]
                
                attacker = gs._safe_get_card(attacker_id)
                blocker = gs._safe_get_card(blocker_id)
                
                if not attacker or not blocker:
                    return True
                
                # Check if either has shadow
                attacker_has_shadow = False
                blocker_has_shadow = False
                
                if hasattr(attacker, 'oracle_text'):
                    attacker_has_shadow = "shadow" in attacker.oracle_text.lower()
                if hasattr(blocker, 'oracle_text'):
                    blocker_has_shadow = "shadow" in blocker.oracle_text.lower()
                    
                # Shadow creatures can block only shadow creatures
                if blocker_has_shadow and not attacker_has_shadow:
                    return False
                    
                # Non-shadow creatures can't block shadow creatures
                if not blocker_has_shadow and attacker_has_shadow:
                    return False
        
        return True

    def _apply_madness(self, card_id, event_type, context=None):
        """Apply madness ability effects (cast for alternative cost when discarded)"""
        gs = self.game_state
        
        if event_type == "DISCARD" and context:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check for madness
            if "madness" not in card.oracle_text.lower():
                return True
                
            # Parse madness cost
             
            match = re.search(r"madness [^\(]([^\)]+)", card.oracle_text.lower())
            madness_cost = match.group(1) if match else None
            
            if not madness_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # In real MTG, the card would go to exile first and the player
            # would choose whether to cast it for the madness cost
            # For this simulation, we'll check if they can afford it
            
            if gs.mana_system.can_pay_mana_cost(controller, madness_cost):
                # Pay madness cost
                gs.mana_system.pay_mana_cost(controller, madness_cost)
                
                # Instead of going to graveyard, card goes to stack
                context["skip_default_movement"] = True
                gs.stack.append(("SPELL", card_id, controller, {"madness": True}))
                
                logging.debug(f"Madness: Cast {card.name} for madness cost when discarded")
                return True
        
        return True

    def _apply_banding(self, card_id, event_type, context=None):
        """Apply banding ability effects (sophisticated combat damage assignment)"""
        gs = self.game_state
        
        if event_type == "DECLARE_ATTACKERS":
            # In real MTG, creatures with banding can attack in bands
            # For this simulation, we'll just flag creatures with banding
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for banding keyword
            if "banding" in card.oracle_text.lower():
                # Mark card as having active banding
                if not hasattr(gs, 'banding_creatures'):
                    gs.banding_creatures = set()
                    
                gs.banding_creatures.add(card_id)
                logging.debug(f"Banding: {card.name} is attacking with banding")
        
        elif event_type == "ASSIGN_COMBAT_DAMAGE" and context:
            blocker_id = context.get("blocker_id")
            attackers = context.get("attackers", [])
            
            # Check if any attacker has banding
            has_banding_attacker = False
            for attacker_id in attackers:
                if hasattr(gs, 'banding_creatures') and attacker_id in gs.banding_creatures:
                    has_banding_attacker = True
                    break
                    
            if has_banding_attacker:
                # If an attacker has banding, the attacking player decides
                # how the blocking creature's damage is assigned
                context["attacking_player_assigns_blocker_damage"] = True
                logging.debug(f"Banding: Attacking player will assign blocker damage")
        
        return True
    
    def _apply_companion(self, card_id, event_type, context=None):
        """Apply companion ability effects (card that starts outside the game)"""
        gs = self.game_state
        
        if event_type == "GAME_START" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for companion keyword
            if "companion" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if deck meets companion requirement
            companion_requirement_met = self._check_companion_requirement(card, controller)
            
            if companion_requirement_met:
                # In actual MTG, the companion starts in the sideboard
                # For this simulation, we'll create a special companion zone
                if not hasattr(controller, "companion"):
                    controller["companion"] = []
                    
                controller["companion"].append(card_id)
                logging.debug(f"Companion: {card.name} is available as a companion")
        
        elif event_type == "CAST_COMPANION" and context:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not controller:
                return True
                
            # Check if card is in companion zone
            if not hasattr(controller, "companion") or card_id not in controller["companion"]:
                return True
                
            # In current MTG rules, you pay 3 generic mana to put companion in hand first
            companion_tax = {"generic": 3}
            
            if hasattr(gs, 'mana_system') and gs.mana_system.can_pay_mana_cost(controller, companion_tax):
                # Pay companion tax
                gs.mana_system.pay_mana_cost(controller, companion_tax)
                
                # Move to hand
                controller["companion"].remove(card_id)
                controller["hand"].append(card_id)
                
                logging.debug(f"Companion: Paid 3 mana to put {card.name} into hand")
            
            return True
        
        return True

    def _check_companion_requirement(self, companion_card, controller):
        """Check if a deck meets the companion requirement"""
        gs = self.game_state
        
        # Get all cards in deck
        deck = controller["library"] + controller["hand"]
        
        # Get companion's requirement text
        requirement_text = ""
        if hasattr(companion_card, 'oracle_text'):
             
            match = re.search(r"companion — (.*?)\.", companion_card.oracle_text, re.IGNORECASE)
            if match:
                requirement_text = match.group(1).lower()
        
        # Parse common companion requirements
        if "singleton" in requirement_text or "no more than one" in requirement_text:
            # Check for singleton (no duplicates)
            card_names = {}
            for card_id in deck:
                card = gs._safe_get_card(card_id)
                if not card or not hasattr(card, 'name'):
                    continue
                    
                name = card.name.lower()
                if name in card_names:
                    return False  # Duplicate found
                card_names[name] = True
            return True
        
        elif "only cards with" in requirement_text:
            # Check for requirements like "only cards with even mana values"
            if "even" in requirement_text:
                for card_id in deck:
                    card = gs._safe_get_card(card_id)
                    if not card or not hasattr(card, 'cmc'):
                        continue
                        
                    if card.cmc % 2 != 0:
                        return False  # Odd cmc found
                return True
            elif "odd" in requirement_text:
                for card_id in deck:
                    card = gs._safe_get_card(card_id)
                    if not card or not hasattr(card, 'cmc'):
                        continue
                        
                    if card.cmc % 2 == 0:
                        return False  # Even cmc found
                return True
        
        # Default to True for simulation purposes
        return True
    
    def _apply_hideaway(self, card_id, event_type, context=None):
        """Apply hideaway ability effects (exile a card that can be cast later)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for hideaway keyword
            if "hideaway" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Parse hideaway number (some cards can have hideaway N)
             
            hideaway_count = 1
            match = re.search(r"hideaway (\d+)", card.oracle_text.lower())
            if match:
                hideaway_count = int(match.group(1))
            
            # Look at top N cards of library
            look_count = 4  # Default for hideaway
            if hideaway_count > 1:
                look_count = hideaway_count
                
            if not controller["library"]:
                return True
                
            # Look at top cards
            look_cards = controller["library"][:look_count]
            
            # Choose a card to exile (in a real game, the player would choose)
            if look_cards:
                # Simple heuristic: choose the highest mana cost card
                chosen_idx = 0
                highest_cmc = -1
                
                for i, card_id in enumerate(look_cards):
                    card = gs._safe_get_card(card_id)
                    if card and hasattr(card, 'cmc') and card.cmc > highest_cmc:
                        highest_cmc = card.cmc
                        chosen_idx = i
                
                # Exile the chosen card
                hideaway_card = controller["library"].pop(chosen_idx)
                controller["exile"].append(hideaway_card)
                
                # Track hideaway card
                if not hasattr(controller, "hideaway_cards"):
                    controller["hideaway_cards"] = {}
                    
                controller["hideaway_cards"][card_id] = hideaway_card
                
                # Put the rest on the bottom in random order
                bottom_cards = [controller["library"].pop(0) for _ in range(min(look_count-1, len(controller["library"])))]
                controller["library"].extend(bottom_cards)
                
                logging.debug(f"Hideaway: {card.name} hid away {gs._safe_get_card(hideaway_card).name}")
        
        elif event_type == "ACTIVATE_HIDEAWAY" and context:
            # When the hideaway card is played
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not controller:
                return True
                
            # Check if there's a hideaway card associated with this permanent
            if not hasattr(controller, "hideaway_cards") or card_id not in controller["hideaway_cards"]:
                return True
                
            hideaway_card_id = controller["hideaway_cards"][card_id]
            
            # Check if the card is still in exile
            if hideaway_card_id not in controller["exile"]:
                return True
                
            # Check if the condition to cast is met (varies by card)
            can_cast = True
            
            # Some hideaway cards require specific conditions
            if "you may cast" in card.oracle_text.lower():
                if "without paying its mana cost" in card.oracle_text.lower():
                    # Can cast for free
                    pass
                else:
                    # Need to pay cost
                    hideaway_card = gs._safe_get_card(hideaway_card_id)
                    if hideaway_card and hasattr(hideaway_card, 'mana_cost') and hasattr(gs, 'mana_system'):
                        can_cast = gs.mana_system.can_pay_mana_cost(controller, hideaway_card.mana_cost)
            
            if can_cast:
                # Remove from exile
                controller["exile"].remove(hideaway_card_id)
                
                # Cast the card
                gs.stack.append(("SPELL", hideaway_card_id, controller))
                
                # Remove from hideaway tracking
                del controller["hideaway_cards"][card_id]
                
                logging.debug(f"Hideaway: Cast {gs._safe_get_card(hideaway_card_id).name} from {card.name}")
        
        return True
    
    def _apply_unleash(self, card_id, event_type, context=None):
        """Apply unleash ability effects (enters with +1/+1 counter, can't block)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check for unleash keyword
            if "unleash" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # In a real game, the player would choose whether to use unleash
            # For simulation, we'll use a decision based on board state
            should_unleash = True
            
            # Check if opponent has creatures we want to block
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            opponent_creatures = [cid for cid in opponent["battlefield"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                            and 'creature' in gs._safe_get_card(cid).card_types]
            
            # If opponent has big threats, maybe better to block
            big_threats = sum(1 for cid in opponent_creatures 
                        if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power') 
                        and gs._safe_get_card(cid).power >= 4)
            
            if big_threats >= 2 and len(opponent_creatures) > 3:
                should_unleash = False
                
            # If we're aggressive or opponent is low on life, unleash
            if opponent["life"] <= 10 or controller["life"] >= 15:
                should_unleash = True
                
            if should_unleash:
                # Add +1/+1 counter
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + 1
                
                # Apply counter effect
                card.power += 1
                card.toughness += 1
                
                # Mark as unable to block
                if not hasattr(card, "cant_block"):
                    card.cant_block = True
                    
                logging.debug(f"Unleash: {card.name} entered with a +1/+1 counter but can't block")
        
        elif event_type == "BLOCKING":
            card = gs._safe_get_card(card_id)
            if not card:
                return True
                
            # Check if card has been unleashed
            if hasattr(card, "cant_block") and card.cant_block:
                return False  # Can't block
        
        return True
    
    def _apply_gravestorm(self, card_id, event_type, context=None):
        """Apply gravestorm ability effects (create copies based on cards that went to the graveyard this turn)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            controller = context.get("controller")
            
            if not card or not hasattr(card, 'oracle_text') or not controller:
                return True
                
            # Check for gravestorm keyword
            if "gravestorm" not in card.oracle_text.lower():
                return True
                
            # Count cards that went to graveyard this turn
            if not hasattr(gs, 'cards_to_graveyard_this_turn'):
                gs.cards_to_graveyard_this_turn = {}
                
            if gs.turn not in gs.cards_to_graveyard_this_turn:
                gs.cards_to_graveyard_this_turn[gs.turn] = []
                
            gravestorm_count = len(gs.cards_to_graveyard_this_turn[gs.turn])
            
            # Create copies
            for _ in range(gravestorm_count):
                # Add copy to stack
                gs.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
                
            logging.debug(f"Gravestorm: Created {gravestorm_count} copies of {card.name}")
        
        # Track cards going to graveyard
        elif event_type == "ZONE_CHANGE" and context:
            if context.get("to_zone") == "graveyard":
                # Record card going to graveyard
                if not hasattr(gs, 'cards_to_graveyard_this_turn'):
                    gs.cards_to_graveyard_this_turn = {}
                    
                if gs.turn not in gs.cards_to_graveyard_this_turn:
                    gs.cards_to_graveyard_this_turn[gs.turn] = []
                    
                gs.cards_to_graveyard_this_turn[gs.turn].append(context.get("card_id"))
        
        return True

    def _apply_mutate(self, card_id, event_type, context=None):
        """Apply mutate ability effects (Combining creature abilities)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("mutate_target"):
            # Get the target creature to mutate onto
            target_id = context.get("mutate_target")
            controller = context.get("controller")
            
            if not controller or not target_id:
                return True
                
            target_card = gs._safe_get_card(target_id)
            mutating_card = gs._safe_get_card(card_id)
            
            if not target_card or not mutating_card:
                return True
                
            # Check if target is a non-human creature controller owns
            if (target_id in controller["battlefield"] and 
                hasattr(target_card, 'card_types') and 'creature' in target_card.card_types and 
                not ('human' in target_card.subtypes if hasattr(target_card, 'subtypes') else False)):
                
                # Initialize mutation stacks if needed
                if not hasattr(controller, "mutation_stacks"):
                    controller["mutation_stacks"] = {}
                    
                if target_id not in controller["mutation_stacks"]:
                    controller["mutation_stacks"][target_id] = [target_id]
                    
                # Add mutating card to the stack (either on top or bottom)
                position = context.get("mutate_position", "top")
                if position == "top":
                    controller["mutation_stacks"][target_id].insert(0, card_id)
                    # Update power/toughness to mutating creature's values
                    if hasattr(mutating_card, 'power') and hasattr(target_card, 'power'):
                        target_card.power = mutating_card.power
                    if hasattr(mutating_card, 'toughness') and hasattr(target_card, 'toughness'):
                        target_card.toughness = mutating_card.toughness
                else:
                    controller["mutation_stacks"][target_id].append(card_id)
                    
                # When mutating, card doesn't enter battlefield normally
                # It becomes part of the mutation stack
                controller["mutation_zone"] = controller.get("mutation_zone", [])
                controller["mutation_zone"].append(card_id)
                
                # Combine abilities - add keywords and abilities from all cards in the stack
                all_keywords = []
                combined_oracle_text = []
                for stack_card_id in controller["mutation_stacks"][target_id]:
                    stack_card = gs._safe_get_card(stack_card_id)
                    if not stack_card:
                        continue
                        
                    # Combine keywords
                    if hasattr(stack_card, 'keywords'):
                        all_keywords.extend([i for i, val in enumerate(stack_card.keywords) if val == 1])
                    
                    # Combine abilities text (except mutate ability itself)
                    if hasattr(stack_card, 'oracle_text'):
                        # Split by line and filter out mutate text
                        abilities = [line for line in stack_card.oracle_text.split('\n') 
                                if "mutate" not in line.lower()]
                        combined_oracle_text.extend(abilities)
                
                # Update the target card's keywords to include all keywords in the stack
                if hasattr(target_card, 'keywords'):
                    new_keywords = target_card.keywords.copy()
                    for keyword_idx in all_keywords:
                        if keyword_idx < len(new_keywords):
                            new_keywords[keyword_idx] = 1
                    target_card.keywords = new_keywords
                
                # Update the target card's oracle text to include all abilities
                if hasattr(target_card, 'oracle_text'):
                    target_card.oracle_text = '\n'.join(combined_oracle_text)
                
                # Trigger "when this creature mutates" abilities
                topmost_card_id = controller["mutation_stacks"][target_id][0]
                gs.trigger_ability(topmost_card_id, "MUTATES")
                
                logging.debug(f"Mutate: {mutating_card.name} mutated onto {target_card.name}")
            
            return True
        
        return True

    def _apply_suspend(self, card_id, event_type, context=None):
        """Apply suspend ability effects (Exile with time counters, cast later)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("suspend"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Parse suspend cost and time counters
             
            match = re.search(r"suspend (\d+)—([^\(]+)", card.oracle_text.lower())
            if not match:
                return True
                
            time_counters = int(match.group(1))
            suspend_cost = match.group(2).strip()
            
            if "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with suspend cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(suspend_cost)
                
                # Move card to exile with time counters
                controller["hand"].remove(card_id)
                controller["exile"].append(card_id)
                
                # Track suspended cards
                if not hasattr(gs, 'suspended_cards'):
                    gs.suspended_cards = {}
                    
                gs.suspended_cards[card_id] = {
                    "time_counters": time_counters,
                    "controller": controller
                }
                
                logging.debug(f"Suspend: {card.name} exiled with {time_counters} time counters")
                
                # Skip standard cast process
                context["skip_standard_cast"] = True
        
        elif event_type == "UPKEEP" and hasattr(gs, 'suspended_cards'):
            # Process suspended cards
            for suspended_id, suspend_info in list(gs.suspended_cards.items()):
                suspended_card = gs._safe_get_card(suspended_id)
                if not suspended_card:
                    continue
                    
                controller = suspend_info["controller"]
                
                # Check if it's this player's upkeep
                if context.get("controller") != controller:
                    continue
                    
                # Remove a time counter
                suspend_info["time_counters"] -= 1
                
                logging.debug(f"Suspend: Removed time counter from {suspended_card.name}, {suspend_info['time_counters']} remaining")
                
                # If no counters left, cast it
                if suspend_info["time_counters"] <= 0:
                    # Remove from exile
                    if suspended_id in controller["exile"]:
                        controller["exile"].remove(suspended_id)
                        
                    # Add to stack (cast without paying cost)
                    gs.stack.append(("SPELL", suspended_id, controller))
                    
                    # Suspended spells gain haste if creatures
                    if hasattr(suspended_card, 'card_types') and 'creature' in suspended_card.card_types:
                        if not hasattr(gs, 'haste_until_eot'):
                            gs.haste_until_eot = set()
                            
                        gs.haste_until_eot.add(suspended_id)
                        
                    # Remove from suspended cards
                    del gs.suspended_cards[suspended_id]
                    
                    logging.debug(f"Suspend: Cast {suspended_card.name} for free (no time counters left)")
        
        return True

    def _apply_phasing(self, card_id, event_type, context=None):
        """Apply phasing ability effects (Card phases out/in)"""
        gs = self.game_state
        
        if event_type == "UPKEEP" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check if card has phasing
            if "phasing" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["battlefield"]:
                        controller = player
                        break
                        
            if not controller:
                return True
                
            # Initialize phased cards tracking
            if not hasattr(gs, 'phased_out'):
                gs.phased_out = set()
                
            # Toggle phased state
            if card_id in gs.phased_out:
                # Phase in
                gs.phased_out.remove(card_id)
                logging.debug(f"Phasing: {card.name} phased in")
            else:
                # Phase out
                gs.phased_out.add(card_id)
                logging.debug(f"Phasing: {card.name} phased out")
        
        # Cards that are phased out are treated as though they don't exist
        if hasattr(gs, 'phased_out') and card_id in gs.phased_out:
            # Prevent almost all interactions with phased-out cards
            if event_type in ["TARGETING", "DEALS_DAMAGE", "BLOCKS", "BLOCKED", "ATTACKS"]:
                return False
        
        return True

    def _apply_unearth(self, card_id, event_type, context=None):
        """Apply unearth ability effects (Return from graveyard temporarily)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("unearth"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            # Parse unearth cost
            match = re.search(r"unearth [^\(]([^\)]+)", card.oracle_text.lower())
            unearth_cost = match.group(1) if match else None
            
            if unearth_cost and hasattr(gs, 'mana_system'):
                # Check if controller can pay
                unearth_parsed = gs.mana_system.parse_mana_cost(unearth_cost)
                
                if gs.mana_system.can_pay_mana_cost(controller, unearth_parsed):
                    gs.mana_system.pay_mana_cost(controller, unearth_parsed)
                    
                    # Return to battlefield using game_state's method
                    gs.move_card(card_id, controller, "graveyard", controller, "battlefield")
                    
                    # Track unearthed cards
                    if not hasattr(gs, 'unearthed_cards'):
                        gs.unearthed_cards = set()
                        
                    gs.unearthed_cards.add(card_id)
                    
                    logging.debug(f"Unearth: Returned {card.name} from graveyard to battlefield")
                    
                    # Trigger ETB abilities
                    gs.trigger_ability(card_id, "ENTERS_BATTLEFIELD")
                else:
                    logging.debug(f"Unearth: Cannot pay cost to unearth")
        
        elif event_type == "END_STEP" and hasattr(gs, 'unearthed_cards') and card_id in gs.unearthed_cards:
            # Exile at end of turn
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if controller:
                # Exile the card using game_state's method
                gs.move_card(card_id, controller, "battlefield", controller, "exile")
                gs.unearthed_cards.remove(card_id)
                
                logging.debug(f"Unearth: Exiled {gs._safe_get_card(card_id).name} at end of turn")
        
        # Prevent unearthed creatures from leaving the battlefield except by exile
        elif event_type == "LEAVES_BATTLEFIELD" and hasattr(gs, 'unearthed_cards') and card_id in gs.unearthed_cards:
            if context and context.get("destination") != "exile":
                # Replace with exile
                context["destination"] = "exile"
                context["destination_zone"] = "exile"
                
                logging.debug(f"Unearth: Redirected {gs._safe_get_card(card_id).name} to exile instead of {context.get('destination_zone')}")
        
        return True

    def _apply_infect(self, card_id, event_type, context=None):
        """Apply infect ability effects (Damage as -1/-1 counters and poison)"""
        gs = self.game_state
        
        if event_type == "DEALS_DAMAGE" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Check if card has infect
            if "infect" not in card.oracle_text.lower():
                return True
                
            damage_amount = context.get("damage_amount", 0)
            if damage_amount <= 0:
                return True
                
            # Get target of damage
            target_id = context.get("target_id")
            target_is_player = context.get("target_is_player", False)
            
            if target_is_player:
                # Apply poison counters instead of damage
                target = context.get("target_player")
                if not target:
                    return True
                    
                target["poison_counters"] = target.get("poison_counters", 0) + damage_amount
                
                logging.debug(f"Infect: {card.name} gave {damage_amount} poison counters")
                
                # Prevent normal damage
                context["damage_amount"] = 0
            elif target_id:
                # Apply -1/-1 counters to creature
                target_card = gs._safe_get_card(target_id)
                if not target_card:
                    return True
                    
                # Find target controller
                target_controller = None
                for player in [gs.p1, gs.p2]:
                    if target_id in player["battlefield"]:
                        target_controller = player
                        break
                        
                if not target_controller:
                    return True
                    
                # Apply -1/-1 counters using game_state's method
                gs.add_counter(target_id, "-1/-1", damage_amount)
                logging.debug(f"Infect: {card.name} put {damage_amount} -1/-1 counters on {target_card.name}")
                
                # Prevent normal damage
                context["damage_amount"] = 0
        
        return True

    def _apply_ward(self, card_id, event_type, context=None):
        """Apply ward ability effects"""
        if event_type == "TARGETING" and context:
            # Ward triggers when targeted by opponent's spells/abilities
            source_controller = context.get("source_controller")
            card_controller = context.get("card_controller")
            
            if source_controller != card_controller:
                # In a real implementation, this would force the opponent to pay
                # a cost (mana or life) or have their spell countered
                # For now, we'll simulate the effect with a probability
                import random
                
                # 70% chance that targeting fails (simulating opponent not paying the cost)
                if random.random() < 0.7:
                    return False  # Targeting fails
        
        return True

    def _apply_prowess(self, card_id, event_type, context=None):
        """Apply prowess ability effects"""
        gs = self.game_state
        
        if event_type == "CAST_NONCREATURE_SPELL":
            # Find the controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
            
            if controller:
                # Add temporary +1/+1 until end of turn
                # This would need a proper implementation for temporary buffs
                if not hasattr(controller, "temp_buffs"):
                    controller["temp_buffs"] = {}
                
                if card_id not in controller["temp_buffs"]:
                    controller["temp_buffs"][card_id] = {"power": 0, "toughness": 0}
                    
                controller["temp_buffs"][card_id]["power"] += 1
                controller["temp_buffs"][card_id]["toughness"] += 1
                
                return True
        
        return True
    
    def _apply_cascade(self, card_id, event_type, context=None):
        """Apply cascade ability effects"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "cascade" not in card.oracle_text.lower():
                return True
            
            controller = context.get("controller")
            if not controller:
                return True
            
            # Mark that this spell has cascade for resolution
            context["has_cascade"] = True
            
            # Let game_state handle cascade resolution during spell resolution
            # rather than duplicating the implementation here
            return True
        
        return True

    def _apply_persist(self, card_id, event_type, context=None):
        """Apply persist ability effects"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["graveyard"]:
                        controller = player
                        break
            
            if controller:
                card = gs._safe_get_card(card_id)
                if card and not hasattr(card, "counters"):
                    card.counters = {}
                
                # Check if card didn't have a -1/-1 counter
                if not card.counters.get("-1/-1", 0) > 0:
                    # Return to battlefield with a -1/-1 counter
                    gs.move_card(card_id, controller, "graveyard", controller, "battlefield")
                    card.counters["-1/-1"] = 1
                    
                    # Apply counter effect
                    card.power -= 1
                    card.toughness -= 1
                    
                    return True
        
        return True

    def _apply_undying(self, card_id, event_type, context=None):
        """Apply undying ability effects"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            controller = context.get("controller")
            if not controller:
                for player in [gs.p1, gs.p2]:
                    if card_id in player["graveyard"]:
                        controller = player
                        break
            
            if controller:
                card = gs._safe_get_card(card_id)
                if card and not hasattr(card, "counters"):
                    card.counters = {}
                
                # Check if card didn't have a +1/+1 counter
                if not card.counters.get("+1/+1", 0) > 0:
                    # Return to battlefield with a +1/+1 counter
                    gs.move_card(card_id, controller, "graveyard", controller, "battlefield")
                    card.counters["+1/+1"] = 1
                    
                    # Apply counter effect
                    card.power += 1
                    card.toughness += 1
                    
                    return True
        
        return True
    
    def _apply_trample(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's _process_attacker_damage
        return True
    
    def _apply_hexproof(self, card_id, event_type, context=None):
        # Handled by Targeting System's _is_valid_..._target methods
        return True
        
    def _apply_lifelink(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's damage application and ReplacementEffectSystem
        return True
    
    def _apply_deathtouch(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's damage application and ReplacementEffectSystem
        return True
    
    def _apply_first_strike(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's resolve_combat method
        return True

    def _apply_double_strike(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's resolve_combat method
        return True
    
    def _apply_vigilance(self, card_id, event_type, context=None):
        # Prevents tapping when attacking - handled by ATTACKS handler/resolution
        return True
    
    def _apply_flash(self, card_id, event_type, context=None):
        """Apply flash ability effects - allows casting at instant speed"""
        # This is handled in action validation, not here
        return True
    
    def _apply_haste(self, card_id, event_type, context=None):
        # Removes summoning sickness - handled by Action Handler's validation
        return True
    
    def _apply_menace(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's _check_block_restrictions
        return True
    
    def _apply_reach(self, card_id, event_type, context=None):
        # Allows blocking fliers - handled by Combat Resolver's _check_block_restrictions
        return True
    
    def _apply_defender(self, card_id, event_type, context=None):
        # Prevents attacking - handled by Action Handler's validation
        return True
    
    def _apply_indestructible(self, card_id, event_type, context=None):
        # Handled by State Based Actions check and DestroyEffect
        return True    
    
    def _apply_boast(self, card_id, event_type, context=None):
        """Apply boast ability effects (activate only if creature attacked this turn)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("boast"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "boast" not in card.oracle_text.lower():
                return True
                
            # Check if this creature attacked this turn
            if not hasattr(gs, 'attackers_this_turn'):
                gs.attackers_this_turn = set()
                
            if card_id not in gs.attackers_this_turn:
                logging.debug(f"Boast: Cannot activate {card.name}'s boast ability as it didn't attack this turn")
                return False
                
            # Boast abilities can only be activated once per turn
            if not hasattr(gs, 'boast_activated'):
                gs.boast_activated = set()
                
            if card_id in gs.boast_activated:
                logging.debug(f"Boast: {card.name}'s boast ability already activated this turn")
                return False
                
            # Mark as activated
            gs.boast_activated.add(card_id)
            logging.debug(f"Boast: Activated {card.name}'s boast ability")
            
        elif event_type == "ATTACKS":
            # Track attackers for boast ability
            if not hasattr(gs, 'attackers_this_turn'):
                gs.attackers_this_turn = set()
                
            gs.attackers_this_turn.add(card_id)
            
        elif event_type == "BEGIN_TURN":
            # Reset boast tracking for the new turn
            if hasattr(gs, 'boast_activated'):
                gs.boast_activated.clear()
            if hasattr(gs, 'attackers_this_turn'):
                gs.attackers_this_turn.clear()
        
        return True
    
    def _apply_buyback(self, card_id, event_type, context=None):
        """Apply buyback ability effects (pay extra cost to return spell to hand)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("buyback"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "buyback" not in card.oracle_text.lower():
                return True
            
            # Parse buyback cost
            match = re.search(r"buyback [^\(]([^\)]+)", card.oracle_text.lower())
            buyback_cost = match.group(1) if match else None
            
            if buyback_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Add buyback cost to the spell's cost
                regular_cost = context["mana_cost"]
                buyback_parsed = gs.mana_system.parse_mana_cost(buyback_cost)
                
                # Combine costs
                for color, amount in buyback_parsed.items():
                    if color in regular_cost:
                        regular_cost[color] += amount
                    else:
                        regular_cost[color] = amount
                
                # Flag for return to hand instead of graveyard
                context["buyback"] = True
                logging.debug(f"Buyback: Paid additional cost for {card.name}")
        
        return True
    
    def _apply_crew(self, card_id, event_type, context=None):
        """Apply crew ability effects (tap creatures to turn Vehicle into artifact creature)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("crew"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                  
            match = re.search(r"crew (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            crew_value = int(match.group(1))
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if card is a Vehicle
            if not hasattr(card, 'subtypes') or 'vehicle' not in [s.lower() for s in card.subtypes]:
                return True
                
            # Get creatures to tap for crewing
            crew_creatures = context.get("crew_creatures", [])
            total_power = sum(gs._safe_get_card(cid).power 
                            for cid in crew_creatures 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'power'))
            
            # Check if enough power
            if total_power < crew_value:
                logging.debug(f"Crew: Not enough power to crew {card.name}, need {crew_value}, have {total_power}")
                return False
                
            # Tap all creatures used for crewing
            for creature_id in crew_creatures:
                controller["tapped_permanents"].add(creature_id)
                
            # Vehicle becomes an artifact creature until end of turn
            if not hasattr(gs, 'crewed_vehicles'):
                gs.crewed_vehicles = set()
                
            gs.crewed_vehicles.add(card_id)
            
            # If not already a creature, add the type
            if hasattr(card, 'card_types') and 'creature' not in card.card_types:
                card.card_types.append('creature')
                
            logging.debug(f"Crew: {card.name} is now an artifact creature")
            
        elif event_type == "END_TURN":
            # End crewing effect at end of turn
            if hasattr(gs, 'crewed_vehicles') and card_id in gs.crewed_vehicles:
                card = gs._safe_get_card(card_id)
                if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                    # Only remove creature type if it's not naturally a creature
                    if not hasattr(card, 'original_type_line') or 'creature' not in card.original_type_line.lower():
                        card.card_types.remove('creature')
                        
                gs.crewed_vehicles.remove(card_id)
                logging.debug(f"Crew: {card.name if card else 'Vehicle'} is no longer a creature")
        
        return True
    
    def _apply_casualty(self, card_id, event_type, context=None):
        """Apply casualty ability effects (sacrifice a creature with power N or greater to copy the spell)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("casualty"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse casualty value
             
            match = re.search(r"casualty (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            casualty_value = int(match.group(1))
            controller = context.get("controller")
            if not controller:
                return True
                
            # Find a valid creature to sacrifice
            valid_sacrifices = []
            for creature_id in controller["battlefield"]:
                creature = gs._safe_get_card(creature_id)
                if (creature and hasattr(creature, 'card_types') and 'creature' in creature.card_types
                    and hasattr(creature, 'power') and creature.power >= casualty_value):
                    valid_sacrifices.append(creature_id)
                    
            if not valid_sacrifices:
                logging.debug(f"Casualty: No valid creatures to sacrifice for {card.name}")
                return True
                
            # In a real game, player would choose which creature to sacrifice
            # Here we'll just pick the lowest power that meets the requirement
            valid_sacrifices.sort(key=lambda cid: gs._safe_get_card(cid).power)
            sacrifice_id = valid_sacrifices[0]
            
            # Sacrifice the creature
            gs.move_card(sacrifice_id, controller, "battlefield", controller, "graveyard")
            logging.debug(f"Casualty: Sacrificed {gs._safe_get_card(sacrifice_id).name} to copy {card.name}")
            
            # Create a copy of the spell on the stack
            gs.stack.append(("SPELL", card_id, controller, {"is_copy": True}))
        
        return True

    def _apply_amplify(self, card_id, event_type, context=None):
        """Apply amplify ability effects (reveal creature cards from hand to gain +1/+1 counters)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse amplify value
             
            match = re.search(r"amplify (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            amplify_value = int(match.group(1))
            
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Find matching creature types
            if not hasattr(card, 'subtypes'):
                return True
                
            creature_type = None
            for subtype in card.subtypes:
                if subtype.lower() != "creature":
                    creature_type = subtype
                    break
                    
            if not creature_type:
                return True
                
            # Count matching creatures in hand
            matching_creatures = 0
            for hand_card_id in controller["hand"]:
                hand_card = gs._safe_get_card(hand_card_id)
                if not hand_card or not hasattr(hand_card, 'subtypes') or not hasattr(hand_card, 'card_types'):
                    continue
                    
                if 'creature' in hand_card.card_types and creature_type in hand_card.subtypes:
                    matching_creatures += 1
                    
            # Apply amplify effect
            if matching_creatures > 0:
                counter_count = matching_creatures * amplify_value
                
                # Add +1/+1 counters
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + counter_count
                
                # Apply counter effect
                if hasattr(card, 'power'):
                    card.power += counter_count
                if hasattr(card, 'toughness'):
                    card.toughness += counter_count
                    
                logging.debug(f"Amplify {amplify_value}: Added {counter_count} +1/+1 counters from {matching_creatures} matching creatures")
                
        return True

    def _apply_ascend(self, card_id, event_type, context=None):
        """Apply ascend ability effects (gain the city's blessing if you control 10+ permanents)"""
        gs = self.game_state
        
        # Ascend is checked whenever a permanent enters or leaves the battlefield
        if event_type in ["ENTERS_BATTLEFIELD", "LEAVES_BATTLEFIELD", "CHECK_STATE"]:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "ascend" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check if player already has the city's blessing
            has_blessing = controller.get("city_blessing", False)
            if has_blessing:
                return True
                
            # Count permanents
            permanent_count = len(controller["battlefield"])
            
            # If 10+ permanents, gain the city's blessing
            if permanent_count >= 10:
                controller["city_blessing"] = True
                logging.debug(f"Ascend: Player gained the city's blessing with {permanent_count} permanents")
                
                # Trigger abilities that care about gaining the city's blessing
                for permanent_id in controller["battlefield"]:
                    permanent = gs._safe_get_card(permanent_id)
                    if permanent and hasattr(permanent, 'oracle_text') and "city's blessing" in permanent.oracle_text.lower():
                        gs.trigger_ability(permanent_id, "GAINED_BLESSING")
                        
        return True

    def _apply_assist(self, card_id, event_type, context=None):
        """Apply assist ability effects (other players can help pay for this spell)"""
        gs = self.game_state
        
        # Assist affects how spells are cast, not an ongoing effect
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "assist" not in card.oracle_text.lower():
                return True
                
            # In a real game, other players would help pay
            # For simulation, just reduce the cost by a small amount
            if "mana_cost" in context and "generic" in context["mana_cost"]:
                # Reduce generic mana cost by 1-2
                reduction = min(2, context["mana_cost"]["generic"])
                context["mana_cost"]["generic"] -= reduction
                
                logging.debug(f"Assist: Reduced cost of {card.name} by {reduction} generic mana")
                
        return True

    def _apply_aura_swap(self, card_id, event_type, context=None):
        """Apply aura swap ability effects (exchange an Aura in hand with this one)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("aura_swap"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "aura swap" not in card.oracle_text.lower():
                return True
                
            # Check if card is an Aura on the battlefield
            controller = context.get("controller")
            if not controller or card_id not in controller["battlefield"]:
                return True
                
            # Parse aura swap cost
             
            match = re.search(r"aura swap [^\(]([^\)]+)", card.oracle_text.lower())
            swap_cost = match.group(1) if match else None
            
            if not swap_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay cost
            parsed_cost = gs.mana_system.parse_mana_cost(swap_cost)
            if not gs.mana_system.can_pay_mana_cost(controller, parsed_cost):
                return True
                
            # Check if controller has an Aura in hand
            aura_in_hand = None
            for hand_id in controller["hand"]:
                hand_card = gs._safe_get_card(hand_id)
                if (hand_card and hasattr(hand_card, 'card_types') and 'enchantment' in hand_card.card_types and
                    hasattr(hand_card, 'subtypes') and 'aura' in hand_card.subtypes):
                    aura_in_hand = hand_id
                    break
                    
            if not aura_in_hand:
                return True
                
            # Get current attachment
            if not hasattr(controller, "attachments"):
                controller["attachments"] = {}
                
            attached_to = controller["attachments"].get(card_id)
            if not attached_to:
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, parsed_cost)
            
            # Swap the Auras
            controller["hand"].remove(aura_in_hand)
            controller["battlefield"].remove(card_id)
            
            controller["battlefield"].append(aura_in_hand)
            controller["hand"].append(card_id)
            
            # Update attachment
            controller["attachments"][aura_in_hand] = attached_to
            if card_id in controller["attachments"]:
                del controller["attachments"][card_id]
                
            # Trigger enters-the-battlefield for new Aura
            gs.trigger_ability(aura_in_hand, "ENTERS_BATTLEFIELD")
            
            logging.debug(f"Aura Swap: Exchanged {card.name} with {gs._safe_get_card(aura_in_hand).name}")
            
        return True
    
    

    def _apply_awaken(self, card_id, event_type, context=None):
        """Apply awaken ability effects (turn a land into a creature with counters)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("awaken"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse awaken value and cost
             
            match = re.search(r"awaken (\d+)—([^\(]+)", card.oracle_text.lower())
            if not match:
                return True
                
            awaken_value = int(match.group(1))
            awaken_cost = match.group(2).strip()
            
            # Check if controller can pay alternate cost
            controller = context.get("controller")
            if not controller:
                return True
                
            if "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with awaken cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(awaken_cost)
                
                # Mark for awaken effect on resolution
                context["awaken"] = awaken_value
                
                logging.debug(f"Awaken: Using alternate cost for {card.name}")
                
        elif event_type == "SPELL_RESOLVES" and context and context.get("awaken"):
            # Apply awaken effect on resolution
            controller = context.get("controller")
            if not controller:
                return True
                
            awaken_value = context.get("awaken", 0)
            
            # Find a land to awaken
            lands = [cid for cid in controller["battlefield"] 
                    if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'type_line') and 'land' in gs._safe_get_card(cid).type_line]
            
            if not lands:
                return True
                
            # Choose a land (in a real game, player would choose)
            target_land_id = lands[0]
            target_land = gs._safe_get_card(target_land_id)
            
            # Turn land into a creature
            if not hasattr(target_land, 'card_types'):
                target_land.card_types = ['land']
                
            if 'creature' not in target_land.card_types:
                target_land.card_types.append('creature')
                
            # Set power/toughness
            target_land.power = awaken_value
            target_land.toughness = awaken_value
            
            # Add +1/+1 counters
            if not hasattr(target_land, "counters"):
                target_land.counters = {}
                
            target_land.counters["+1/+1"] = awaken_value
            
            # Mark as entered this turn (has summoning sickness)
            controller["entered_battlefield_this_turn"].add(target_land_id)
            
            logging.debug(f"Awaken: Turned {target_land.name} into a {awaken_value}/{awaken_value} creature with {awaken_value} +1/+1 counters")
            
        return True
    
    def _apply_battle(self, card_id, event_type, context=None):
        # Handled by Battle card logic in GameState/Card
        return True

    def _apply_saga(self, card_id, event_type, context=None):
        # Handled by Saga card logic in GameState/Card
        return True

    def _apply_adventure(self, card_id, event_type, context=None):
        # Handled by Adventure card logic in GameState/Card/ActionHandler
        return True

    def _apply_mdfc(self, card_id, event_type, context=None):
        # Handled by MDFC logic in GameState/Card/ActionHandler
        return True

    def _apply_room_door_state(self, card_id, event_type, context=None):
        # Handled by Room card logic in GameState/Card/ActionHandler
        return True

    def _apply_class_level(self, card_id, event_type, context=None):
        # Handled by Class card logic in GameState/Card/ActionHandler
        return True
        
    def _apply_spree(self, card_id, event_type, context=None):
        """Apply effects for the spree keyword."""
        # Spree is an alternative casting mode, handled during spell casting/resolution.
        # The _apply_spree method here might check if the conditions for Spree are met,
        # or handle triggers related to Spree spells being cast.
        gs = self.game_state
        if event_type == "CAST_SPELL" and context and context.get("is_spree", False):
             card = gs._safe_get_card(card_id)
             logging.debug(f"Applying Spree effects for {card.name if card else 'Unknown'}")
             # Actual effect resolution happens based on chosen modes in _resolve_spree_spell
             return True
        return True # Return true if event doesn't match

    def _apply_battle_cry(self, card_id, event_type, context=None):
        """Apply battle cry ability effects (other attacking creatures get +1/+0)"""
        gs = self.game_state
        
        if event_type == "ATTACKS":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "battle cry" not in card.oracle_text.lower():
                return True
                
            # Check if card is actually attacking
            if card_id not in gs.current_attackers:
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Apply +1/+0 to other attacking creatures
            for attacker_id in gs.current_attackers:
                if attacker_id == card_id:
                    continue
                    
                attacker = gs._safe_get_card(attacker_id)
                if not attacker:
                    continue
                    
                # Add temporary buff
                if not hasattr(controller, "temp_buffs"):
                    controller["temp_buffs"] = {}
                    
                if attacker_id not in controller["temp_buffs"]:
                    controller["temp_buffs"][attacker_id] = {"power": 0, "toughness": 0, "until_end_of_turn": True}
                    
                controller["temp_buffs"][attacker_id]["power"] += 1
                
            logging.debug(f"Battle Cry: {card.name} gave other attackers +1/+0 until end of turn")
            
        return True

    def _apply_training(self, card_id, event_type, context=None):
        """Apply training ability effects (add a +1/+1 counter when attacking with a stronger creature)"""
        gs = self.game_state
        
        if event_type == "ATTACKS":
            # Check if card has training
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "training" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Check if this creature is attacking
            if card_id not in gs.current_attackers:
                return True
                
            # Get power of the creature with training
            if not hasattr(card, 'power'):
                return True
                
            training_power = card.power
            
            # Check if another attacking creature has greater power
            found_stronger = False
            for attacker_id in gs.current_attackers:
                if attacker_id == card_id:
                    continue
                    
                attacker = gs._safe_get_card(attacker_id)
                if not attacker or not hasattr(attacker, 'power'):
                    continue
                    
                if attacker.power > training_power:
                    found_stronger = True
                    break
            
            # If found a stronger attacker, add a +1/+1 counter
            if found_stronger:
                if not hasattr(card, "counters"):
                    card.counters = {}
                    
                card.counters["+1/+1"] = card.counters.get("+1/+1", 0) + 1
                
                # Update power/toughness
                card.power += 1
                card.toughness += 1
                
                logging.debug(f"Training: {card.name} got a +1/+1 counter from attacking with a stronger creature")
            
            return True
        
        return True

    def _apply_prowl(self, card_id, event_type, context=None):
        """Apply prowl ability effects (alternative cost if a creature of matching type dealt damage)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "prowl" not in card.oracle_text.lower():
                return True
                
            controller = context.get("controller")
            if not controller:
                return True
                
            # Check if prowl condition is met
            prowl_condition_met = False
            
            # Get card subtypes
            card_subtypes = card.subtypes if hasattr(card, 'subtypes') else []
            
            # Check if a creature with matching subtype dealt damage to opponent this turn
            if hasattr(gs, 'damage_this_turn'):
                for attacker_id in gs.damage_this_turn.get(gs.turn, []):
                    attacker = gs._safe_get_card(attacker_id)
                    if not attacker or not hasattr(attacker, 'subtypes'):
                        continue
                        
                    # Check if any subtype matches
                    for subtype in attacker.subtypes:
                        if subtype in card_subtypes:
                            prowl_condition_met = True
                            break
                            
                    if prowl_condition_met:
                        break
            
            # If prowl condition is met, allow alternative cost
            if prowl_condition_met and context.get("prowl"):
                # Parse prowl cost
                 
                match = re.search(r"prowl [^\(]([^\)]+)", card.oracle_text.lower())
                prowl_cost = match.group(1) if match else None
                
                if prowl_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                    # Replace regular cost with prowl cost
                    context["mana_cost"] = gs.mana_system.parse_mana_cost(prowl_cost)
                    logging.debug(f"Prowl: Using alternative cost for {card.name}")
            
            return True
        
        return True

    def _apply_myriad(self, card_id, event_type, context=None):
        """Apply myriad ability effects (create attacking token copies)"""
        gs = self.game_state
        
        if event_type == "ATTACKS":
            # Check if card has myriad
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text') or "myriad" not in card.oracle_text.lower():
                return True
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Find opponent
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            
            # Create token data based on original card
            token_data = {
                "name": f"{card.name} Token",
                "type_line": card.type_line if hasattr(card, 'type_line') else "Creature",
                "card_types": card.card_types if hasattr(card, 'card_types') else ["creature"],
                "subtypes": card.subtypes if hasattr(card, 'subtypes') else [],
                "power": card.power if hasattr(card, 'power') else 1,
                "toughness": card.toughness if hasattr(card, 'toughness') else 1,
                "oracle_text": card.oracle_text if hasattr(card, 'oracle_text') else "",
                "keywords": card.keywords if hasattr(card, 'keywords') else [0] * 11,
                "colors": card.colors if hasattr(card, 'colors') else [0, 0, 0, 0, 0]
            }
            
            # Create a token copy
            token_id = gs.create_token(controller, token_data)
            
            # Add to attackers
            if token_id:
                gs.current_attackers.append(token_id)
                
                # Remove summoning sickness from token (it's attacking)
                if token_id in controller["entered_battlefield_this_turn"]:
                    controller["entered_battlefield_this_turn"].remove(token_id)
                    
                logging.debug(f"Myriad: Created attacking token copy of {card.name}")
                
                # Track token for exile at end of combat
                if not hasattr(gs, 'exile_at_end_of_combat'):
                    gs.exile_at_end_of_combat = []
                gs.exile_at_end_of_combat.append((token_id, controller))
        
        elif event_type == "END_OF_COMBAT":
            # Check for tokens to exile
            if hasattr(gs, 'exile_at_end_of_combat'):
                for token_id, token_controller in gs.exile_at_end_of_combat:
                    # Use game_state's method to move to exile
                    if token_id in token_controller["battlefield"]:
                        gs.move_card(token_id, token_controller, "battlefield", token_controller, "exile")
                        logging.debug(f"Myriad: Exiled token {token_id} at end of combat")
                
                # Clear the list
                gs.exile_at_end_of_combat = []
        
        return True
    
    def _apply_class_level(self, card_id, event_type, context=None):
        """Apply class level effects based on current level."""
        gs = self.game_state
        card = gs._safe_get_card(card_id)
        if not card or not hasattr(card, 'is_class') or not card.is_class:
            return True
            
        # Class cards typically have level-based abilities
        if event_type == "CLASS_LEVEL_UP" and context:
            # Re-register abilities based on new level
            self._parse_and_register_abilities(card_id, card)
            
            # Handle type changes (e.g., becoming a creature)
            level_data = card.get_current_class_data() if hasattr(card, 'get_current_class_data') else None
            if level_data and 'type_line' in level_data:
                # If class becomes a creature at this level
                if 'creature' in level_data['type_line'].lower() and not any('creature' in card_type for card_type in card.card_types):
                    # Add creature type
                    card.card_types.append('creature')
                    
                    # Set power/toughness if provided
                    if 'power' in level_data and 'toughness' in level_data:
                        card.power = level_data['power']
                        card.toughness = level_data['toughness']
                        
                    logging.debug(f"Class {card.name} became a creature at level {card.current_level}")
                    
            # Trigger any abilities related to leveling up
            gs.trigger_ability(card_id, "CLASS_LEVEL_CHANGED", context)
            
        return True
            
    def _apply_modular(self, card_id, event_type, context=None):
        """Apply modular ability effects (move +1/+1 counters when dies)"""
        gs = self.game_state
        
        if event_type == "DIES" and context:
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Get controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["graveyard"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Parse modular value
            match = re.search(r"modular (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            modular_value = int(match.group(1))
            
            # Get actual counter value (initialized to modular value if no counters present)
            counter_value = 0
            if hasattr(card, 'counters'):
                counter_value = card.counters.get("+1/+1", modular_value)
            else:
                counter_value = modular_value
                
            # If no counters, no effect
            if counter_value <= 0:
                return True
                
            # Find valid artifact creature targets
            valid_targets = []
            for target_id in controller["battlefield"]:
                target_card = gs._safe_get_card(target_id)
                if (target_card and hasattr(target_card, 'card_types') and 
                    'artifact' in target_card.card_types and 
                    'creature' in target_card.card_types):
                    valid_targets.append(target_id)
                    
            if not valid_targets:
                return True
                
            # Choose best target (prefer creatures with existing +1/+1 counters)
            target_scores = []
            for target_id in valid_targets:
                target_card = gs._safe_get_card(target_id)
                score = 0
                
                # Prefer creatures that already have counters
                if hasattr(target_card, 'counters') and "+1/+1" in target_card.counters:
                    score += 10
                    
                # Prefer creatures with evasion
                if hasattr(target_card, 'oracle_text'):
                    if "flying" in target_card.oracle_text.lower():
                        score += 5
                    if "trample" in target_card.oracle_text.lower():
                        score += 3
                        
                # Prefer creatures with higher power/toughness
                if hasattr(target_card, 'power') and hasattr(target_card, 'toughness'):
                    score += (target_card.power + target_card.toughness) / 2
                    
                target_scores.append((target_id, score))
                
            # Pick highest scoring target
            if target_scores:
                target_scores.sort(key=lambda x: x[1], reverse=True)
                best_target_id = target_scores[0][0]
                
                # Add counters to target using game_state's method
                gs.add_counter(best_target_id, "+1/+1", counter_value)
                logging.debug(f"Modular: Moved {counter_value} +1/+1 counters from {card.name} to {gs._safe_get_card(best_target_id).name}")
                
        return True


    def _apply_protection(self, card_id, event_type, context=None):
        # Handled by Targeting System and Combat Resolver
        return True
    
    def _apply_ward(self, card_id, event_type, context=None):
        # Handled by Targeting System (requires opponent payment or counter)
        return True


    def _apply_afterlife(self, card_id, event_type, context=None):
        gs = self.game_state
        if event_type == "DIES":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'): return True
            match = re.search(r"afterlife (\d+)", card.oracle_text.lower())
            if match:
                 count = int(match.group(1))
                 controller = gs.get_card_controller(card_id) # Should be controller before death
                 if controller:
                     # Trigger token creation (happens *after* SBAs resolve normally)
                     # We can add it as a delayed trigger
                     def create_spirit_tokens():
                         for _ in range(count):
                              token_data = {"name": "Spirit", "type_line": "Token Creature - Spirit", "power": 1, "toughness": 1, "colors": [1,0,0,0,0], "keywords": [1,0,0,0,0,0,0,0,0,0,0]} # White, Flying
                              gs.create_token(controller, token_data)
                         logging.debug(f"Afterlife: Created {count} Spirit tokens after {card.name} died.")
                     if not hasattr(gs, 'delayed_triggers'): gs.delayed_triggers = []
                     gs.delayed_triggers.append(create_spirit_tokens)
        return True

    def _apply_dash(self, card_id, event_type, context=None):
        """Apply dash ability effects (alternative cost with haste and return to hand)"""
        gs = self.game_state
        
        if event_type == "CAST_SPELL" and context and context.get("dash"):
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse dash cost
            match = re.search(r"dash [^\(]([^\)]+)", card.oracle_text.lower())
            dash_cost = match.group(1) if match else None
            
            if dash_cost and "mana_cost" in context and hasattr(gs, 'mana_system'):
                # Replace regular cost with dash cost
                context["mana_cost"] = gs.mana_system.parse_mana_cost(dash_cost)
                
                # Flag for return to hand at end of turn
                if not hasattr(gs, 'dash_cards'):
                    gs.dash_cards = set()
                    
                gs.dash_cards.add(card_id)
                
                logging.debug(f"Dash: {card.name} cast for dash cost")
            
        elif event_type == "ENTERS_BATTLEFIELD" and hasattr(gs, 'dash_cards') and card_id in gs.dash_cards:
            # Grant haste to dashed creature
            card = gs._safe_get_card(card_id)
            if card:
                # This is a bit of a hack since we don't modify the card object directly
                # In a real implementation, we'd track "has_haste" separately
                if not hasattr(gs, 'has_haste_until_eot'):
                    gs.has_haste_until_eot = set()
                    
                gs.has_haste_until_eot.add(card_id)
                logging.debug(f"Dash: {card.name} gained haste")
                
        elif event_type == "END_STEP" and hasattr(gs, 'dash_cards') and card_id in gs.dash_cards:
            # Return to hand at end of turn
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if controller:
                # Return to hand using game_state's method
                gs.move_card(card_id, controller, "battlefield", controller, "hand")
                gs.dash_cards.remove(card_id)
                logging.debug(f"Dash: Returned {gs._safe_get_card(card_id).name} to hand at end of turn")
                
        return True

    def _apply_evolve(self, card_id, event_type, context=None):
        """Apply evolve ability effects (add +1/+1 counter when larger creature ETBs)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD" and context:
            # Check if this is another creature entering (not the evolve creature itself)
            entering_card_id = context.get("card_id")
            if not entering_card_id or entering_card_id == card_id:
                return True
                
            # Check if evolve creature is on battlefield
            evolve_card = gs._safe_get_card(card_id)
            entering_card = gs._safe_get_card(entering_card_id)
            
            if not evolve_card or not entering_card:
                return True
                
            # Check if both are creatures and controlled by same player
            evolve_controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    evolve_controller = player
                    break
                    
            if not evolve_controller or entering_card_id not in evolve_controller["battlefield"]:
                return True
                
            if not hasattr(evolve_card, 'card_types') or 'creature' not in evolve_card.card_types:
                return True
                
            if not hasattr(entering_card, 'card_types') or 'creature' not in entering_card.card_types:
                return True
                
            # Check if evolve creature has the ability
            if not hasattr(evolve_card, 'oracle_text') or "evolve" not in evolve_card.oracle_text.lower():
                return True
                
            # Check if entering creature has greater power or toughness
            if (hasattr(entering_card, 'power') and hasattr(evolve_card, 'power') and
                hasattr(entering_card, 'toughness') and hasattr(evolve_card, 'toughness')):
                
                if (entering_card.power > evolve_card.power or 
                    entering_card.toughness > evolve_card.toughness):
                    
                    # Add +1/+1 counter using game_state's method
                    gs.add_counter(card_id, "+1/+1", 1)
                    logging.debug(f"Evolve: {evolve_card.name} got a +1/+1 counter")
                    
        return True

    def _apply_fabricate(self, card_id, event_type, context=None):
        """Apply fabricate ability effects (choose +1/+1 counters or Servo tokens)"""
        gs = self.game_state
        
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse fabricate value
            match = re.search(r"fabricate (\d+)", card.oracle_text.lower())
            if not match:
                return True
                
            fabricate_value = int(match.group(1))
            
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if card_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                return True
                
            # Decision heuristic
            opponent = gs.p2 if controller == gs.p1 else gs.p1
            opponent_creatures = [cid for cid in opponent["battlefield"] 
                            if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'card_types') 
                            and 'creature' in gs._safe_get_card(cid).card_types]
            
            killable_creatures = sum(1 for cid in opponent_creatures 
                                if gs._safe_get_card(cid) and hasattr(gs._safe_get_card(cid), 'toughness') 
                                and gs._safe_get_card(cid).toughness <= 1)
            
            if killable_creatures >= fabricate_value / 2:
                # Create tokens using game_state's method
                for _ in range(fabricate_value):
                    token_data = {
                        "name": "Servo Token",
                        "type_line": "artifact creature — servo",
                        "card_types": ["artifact", "creature"],
                        "subtypes": ["servo"],
                        "power": 1,
                        "toughness": 1,
                        "oracle_text": "",
                        "keywords": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
                    }
                    
                    gs.create_token(controller, token_data)
                    
                logging.debug(f"Fabricate {fabricate_value}: Created {fabricate_value} 1/1 Servo tokens")
            else:
                # Add +1/+1 counters using game_state's method
                gs.add_counter(card_id, "+1/+1", fabricate_value)
                logging.debug(f"Fabricate {fabricate_value}: Added {fabricate_value} +1/+1 counters")
                
        return True

    def _apply_embalm(self, card_id, event_type, context=None):
        """Apply embalm ability effects (exile from graveyard to create token copy)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("embalm"):
            # Check if card is in graveyard
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse embalm cost
            match = re.search(r"embalm [^\(]([^\)]+)", card.oracle_text.lower())
            embalm_cost = match.group(1) if match else None
            
            if not embalm_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            embalm_parsed = gs.mana_system.parse_mana_cost(embalm_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, embalm_parsed):
                logging.debug(f"Embalm: Cannot pay embalm cost for {card.name}")
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, embalm_parsed)
            
            # Move card to exile using game_state's method
            gs.move_card(card_id, controller, "graveyard", controller, "exile")
            
            # Create token with same characteristics but as white Zombie
            token_data = {
                "name": f"{card.name} Token",
                "type_line": card.type_line,
                "card_types": card.card_types,
                "subtypes": card.subtypes + ["zombie"],  # Add Zombie type
                "power": card.power if hasattr(card, 'power') else 0,
                "toughness": card.toughness if hasattr(card, 'toughness') else 0,
                "oracle_text": card.oracle_text if hasattr(card, 'oracle_text') else "",
                "keywords": card.keywords if hasattr(card, 'keywords') else [0] * 11,
                "colors": [1, 0, 0, 0, 0]  # White color only
            }
            
            gs.create_token(controller, token_data)
            logging.debug(f"Embalm: Created a token copy of {card.name}")
            
        return True

    def _apply_eternalize(self, card_id, event_type, context=None):
        """Apply eternalize ability effects (similar to embalm but token is 4/4)"""
        gs = self.game_state
        
        if event_type == "ACTIVATE" and context and context.get("eternalize"):
            # Check if card is in graveyard
            controller = context.get("controller")
            if not controller or card_id not in controller["graveyard"]:
                return True
                
            card = gs._safe_get_card(card_id)
            if not card or not hasattr(card, 'oracle_text'):
                return True
                
            # Parse eternalize cost
            match = re.search(r"eternalize [^\(]([^\)]+)", card.oracle_text.lower())
            eternalize_cost = match.group(1) if match else None
            
            if not eternalize_cost or not hasattr(gs, 'mana_system'):
                return True
                
            # Check if controller can pay
            eternalize_parsed = gs.mana_system.parse_mana_cost(eternalize_cost)
            
            if not gs.mana_system.can_pay_mana_cost(controller, eternalize_parsed):
                logging.debug(f"Eternalize: Cannot pay eternalize cost for {card.name}")
                return True
                
            # Pay the cost
            gs.mana_system.pay_mana_cost(controller, eternalize_parsed)
            
            # Move card to exile using game_state's method
            gs.move_card(card_id, controller, "graveyard", controller, "exile")
            
            # Create token with same characteristics but as 4/4 black Zombie
            token_data = {
                "name": f"{card.name} Token",
                "type_line": card.type_line,
                "card_types": card.card_types,
                "subtypes": card.subtypes + ["zombie"],  # Add Zombie type
                "power": 4,  # Always 4/4
                "toughness": 4,
                "oracle_text": card.oracle_text if hasattr(card, 'oracle_text') else "",
                "keywords": card.keywords if hasattr(card, 'keywords') else [0] * 11,
                "colors": [0, 0, 1, 0, 0]  # Black color only
            }
            
            gs.create_token(controller, token_data)
            logging.debug(f"Eternalize: Created a 4/4 black Zombie token copy of {card.name}")
            
        return True
    
    def _apply_prowess(self, card_id, event_type, context=None):
        gs = self.game_state
        if event_type == "CAST_NONCREATURE_SPELL":
            controller = context.get("controller")
            # Check if the creature with prowess is controlled by the caster
            owner = None
            for p in [gs.p1, gs.p2]:
                 if card_id in p["battlefield"]: owner = p; break
            if owner == controller:
                 if hasattr(gs, 'add_temp_buff'):
                      gs.add_temp_buff(card_id, {"power": 1, "toughness": 1, "until_end_of_turn": True})
                      logging.debug(f"Prowess: {gs._safe_get_card(card_id).name} gets +1/+1.")
        return True
    
    def _apply_scry(self, card_id, event_type, context=None):
        # Usually triggered by spell resolution - handled in resolve_spell_effects
        return True

    def _apply_cascade(self, card_id, event_type, context=None):
        # Triggered on cast - handled in handle_cast_trigger
        return True

    
    def _apply_unblockable(self, card_id, event_type, context=None):
        # Handled by Combat Resolver's _check_block_restrictions
        return True

    def _apply_shroud(self, card_id, event_type, context=None):
        # Handled by Targeting System
        return True

    def _apply_regenerate(self, card_id, event_type, context=None):
        # Replaces destruction - handled in State Based Actions / Replacement Effects
        return True

    def _apply_persist(self, card_id, event_type, context=None):
        # Triggered on death - handled in DIES trigger processing
        return True
    
    def _apply_undying(self, card_id, event_type, context=None):
        # Triggered on death - handled in DIES trigger processing
        return True
    
    def _apply_riot(self, card_id, event_type, context=None):
        gs = self.game_state
        if event_type == "ENTERS_BATTLEFIELD":
            card = gs._safe_get_card(card_id)
            if not card: return True
            controller = gs.get_card_controller(card_id)
            if not controller: return True

            # AI Choice: Haste or +1/+1 counter? Simple: Choose haste if creature has high power already.
            choose_haste = getattr(card, 'power', 0) >= 3
            if choose_haste:
                 if hasattr(gs, 'give_haste_until_eot'): gs.give_haste_until_eot(card_id)
                 logging.debug(f"Riot: {card.name} chose haste.")
            else:
                 if hasattr(gs, 'add_counter'): gs.add_counter(card_id, "+1/+1", 1)
                 logging.debug(f"Riot: {card.name} chose a +1/+1 counter.")
        return True
    

    def _apply_enrage(self, card_id, event_type, context=None):
        # Triggered when damaged - handled in damage application / triggers
        return True

    def _apply_afflict(self, card_id, event_type, context=None):
        # Triggered when blocked - handled in block assignment / combat triggers
        return True
    
    def _apply_exalted(self, card_id, event_type, context=None):
        # Triggered when a creature attacks alone - handled in Declare Attackers / Combat Triggers
        return True

    def _apply_mentor(self, card_id, event_type, context=None):
        # Triggered when attacking - handled in Declare Attackers / Combat Triggers
        return True
    

    def _apply_convoke(self, card_id, event_type, context=None):
        # Casting cost modification - handled in Mana System / Casting
        return True