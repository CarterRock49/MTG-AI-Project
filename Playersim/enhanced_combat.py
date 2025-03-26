import logging
from collections import defaultdict
from .combat import EnhancedCombatResolver

class ExtendedCombatResolver(EnhancedCombatResolver):
    """
    Extended version of the EnhancedCombatResolver that adds support for:
    - Planeswalker damage
    - Battle damage
    - First strike ordering
    - Ninjutsu
    - Multiple blocker assignment
    
    This class inherits from EnhancedCombatResolver and extends it with new capabilities.
    """
    
    def __init__(self, game_state):
        super().__init__(game_state)
        self.planeswalker_damage = defaultdict(int)
        self.battle_damage = defaultdict(int)
        
    def resolve_combat(self):
        """Override to add support for planeswalker and battle damage"""
        try:
            gs = self.game_state
            
            # Reset tracking structures
            self.planeswalker_damage.clear()
            self.battle_damage.clear()
            
            # Call the parent implementation to handle most of the combat
            total_damage_to_opponent = super().resolve_combat()
            
            # Process planeswalker damage
            self._apply_planeswalker_damage()
            
            # Process battle damage
            self._apply_battle_damage()
            
            return total_damage_to_opponent
            
        except Exception as e:
            logging.error(f"Error in extended combat resolution: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return 0
        
    def protect_planeswalker(self, attacked_planeswalker_id, defender_id):
        """Set up protection for a planeswalker"""
        gs = self.game_state
        
        if not hasattr(gs, "planeswalker_protectors"):
            gs.planeswalker_protectors = {}
        
        gs.planeswalker_protectors[attacked_planeswalker_id] = defender_id
        
        defender_card = gs._safe_get_card(defender_id)
        planeswalker_card = gs._safe_get_card(attacked_planeswalker_id)
        
        if defender_card and planeswalker_card:
            logging.debug(f"COMBAT: {defender_card.name} is now protecting {planeswalker_card.name}")
            return True
        return False
                
    def _process_attacker_damage(self, attacker_id, attacker_player, defender_player,
                            damage_to_creatures, damage_to_players, creatures_dealt_damage,
                            killed_creatures, is_first_strike):
        """Override to handle damage to planeswalkers AND BATTLES"""
        gs = self.game_state

        # --- Handle Planeswalker Targeting ---
        if hasattr(gs, "planeswalker_attack_targets") and attacker_id in gs.planeswalker_attack_targets:
            planeswalker_id = gs.planeswalker_attack_targets[attacker_id]
            planeswalker_card = gs._safe_get_card(planeswalker_id)

            if not planeswalker_card:
                # Fallback to regular damage if planeswalker is gone
                return super()._process_attacker_damage(
                    attacker_id, attacker_player, defender_player,
                    damage_to_creatures, damage_to_players, creatures_dealt_damage,
                    killed_creatures, is_first_strike
                )

            # Check if planeswalker is protected
            if hasattr(gs, "planeswalker_protectors") and planeswalker_id in gs.planeswalker_protectors:
                protector_id = gs.planeswalker_protectors[planeswalker_id]
                if self._redirect_damage_to_protector(attacker_id, protector_id):
                     # Check if creature should deal damage this phase
                     if self._should_deal_damage_this_phase(gs._safe_get_card(attacker_id), is_first_strike):
                         # Mark damage dealt for lifelink etc.
                         attacker_card = gs._safe_get_card(attacker_id)
                         damage = self._get_card_power(attacker_card, attacker_player)
                         creatures_dealt_damage.add(attacker_id)
                         # Apply lifelink for redirected damage
                         if self._has_keyword(attacker_card, "lifelink"):
                              attacker_player["life"] += damage
                              logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life (redirected)")
                         return damage
                     else:
                          return 0

            # Damage to Planeswalker
            if self._should_deal_damage_this_phase(gs._safe_get_card(attacker_id), is_first_strike):
                 return self._process_planeswalker_damage(attacker_id, attacker_player, planeswalker_id, damage_to_creatures, creatures_dealt_damage)
            else:
                 return 0

        # --- Handle Battle Targeting ---
        if hasattr(gs, "battle_attack_targets") and attacker_id in gs.battle_attack_targets:
            battle_id = gs.battle_attack_targets[attacker_id]
            battle_card = gs._safe_get_card(battle_id)

            if not battle_card:
                # Fallback if battle is gone
                return super()._process_attacker_damage(
                    attacker_id, attacker_player, defender_player,
                    damage_to_creatures, damage_to_players, creatures_dealt_damage,
                    killed_creatures, is_first_strike
                )

            # Check if battle is protected (by defender creatures)
            # If blocks exist for this battle attacker, damage goes to blockers first
            blockers = gs.current_block_assignments.get(attacker_id, [])
            valid_blockers = [b for b in blockers if b not in killed_creatures]

            if valid_blockers:
                 # Damage assignment handled by super method logic for blocked creatures
                 return super()._process_attacker_damage(
                     attacker_id, attacker_player, defender_player,
                     damage_to_creatures, damage_to_players, creatures_dealt_damage,
                     killed_creatures, is_first_strike
                 )
            else:
                # Damage to Battle (unblocked)
                if self._should_deal_damage_this_phase(gs._safe_get_card(attacker_id), is_first_strike):
                    return self._process_battle_damage(attacker_id, attacker_player, battle_id, creatures_dealt_damage)
                else:
                    return 0

        # --- Normal Damage (to player or blockers) ---
        # Check if damage should be dealt this phase first
        if not self._should_deal_damage_this_phase(gs._safe_get_card(attacker_id), is_first_strike):
            return 0

        # If not attacking a planeswalker or battle, use the parent implementation
        return super()._process_attacker_damage(
            attacker_id, attacker_player, defender_player,
            damage_to_creatures, damage_to_players, creatures_dealt_damage,
            killed_creatures, is_first_strike
        )
        
    def _process_planeswalker_damage(self, attacker_id, attacker_player, planeswalker_id, damage_to_creatures, creatures_dealt_damage):
        """Process damage to a planeswalker."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        planeswalker_card = gs._safe_get_card(planeswalker_id)
        if not attacker_card or not planeswalker_card: return 0

        damage = self._get_card_power(attacker_card, attacker_player)
        self.planeswalker_damage[planeswalker_id] += damage
        creatures_dealt_damage.add(attacker_id)
        logging.debug(f"COMBAT: {attacker_card.name} deals {damage} damage to planeswalker {planeswalker_card.name}")

        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life (vs Planeswalker)")

        return damage
    
    def _should_deal_damage_this_phase(self, card, is_first_strike_phase):
        """Check if a creature should deal damage in the current phase."""
        if not card: return False
        has_first_strike = self._has_keyword(card, "first strike")
        has_double_strike = self._has_keyword(card, "double strike")

        if is_first_strike_phase:
            # Only FS/DS deal damage in first strike phase
            return has_first_strike or has_double_strike
        else:
            # Creatures without FS deal damage, and DS creatures deal damage *again*
            return not has_first_strike or has_double_strike  
        
    def _process_battle_damage(self, attacker_id, attacker_player, battle_id, creatures_dealt_damage):
        """Process damage dealt to a battle card."""
        gs = self.game_state
        attacker_card = gs._safe_get_card(attacker_id)
        battle_card = gs._safe_get_card(battle_id)

        if not attacker_card or not battle_card:
            return 0

        damage = self._get_card_power(attacker_card, attacker_player)

        # Use the specific method for damaging battles
        self.damage_to_battle(battle_id, attacker_id, damage)

        # Mark damage dealt
        creatures_dealt_damage.add(attacker_id)

        # Apply lifelink if attacker has it
        if self._has_keyword(attacker_card, "lifelink"):
            attacker_player["life"] += damage
            logging.debug(f"COMBAT: Lifelink from {attacker_card.name} gained {damage} life (vs Battle)")

        return damage
    
    
        
    def _apply_planeswalker_damage(self):
        """Apply damage to planeswalkers and check if any died with enhanced effect processing"""
        gs = self.game_state
        
        planeswalkers_to_remove = []
        
        for planeswalker_id, damage in self.planeswalker_damage.items():
            planeswalker_card = gs._safe_get_card(planeswalker_id)
            if not planeswalker_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if planeswalker_id in player["battlefield"]:
                    controller = player
                    break
            
            if not controller:
                continue
                
            # Check for protection abilities
            has_protection = False
            if hasattr(planeswalker_card, 'oracle_text'):
                protection_text = planeswalker_card.oracle_text.lower()
                if "prevent all damage" in protection_text or "protection from all colors" in protection_text:
                    has_protection = True
                    logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} has protection, damage prevented")
                    continue
            
            # Apply damage as loyalty loss
            if hasattr(planeswalker_card, "loyalty"):
                # Create damage context for possible replacement effects
                damage_context = {
                    "target_id": planeswalker_id,
                    "target_is_planeswalker": True,
                    "damage_amount": damage,
                    "is_combat_damage": True,
                    "controller": controller,
                    "damage_source_type": "combat"
                }
                
                # Apply damage replacement effects if available
                modified_context = damage_context
                if hasattr(gs, 'apply_replacement_effect'):
                    modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE_TO_PLANESWALKER", damage_context)
                    if was_replaced:
                        damage = modified_context.get("damage_amount", damage)
                        # Check if damage was redirected or prevented entirely
                        if damage <= 0 or modified_context.get("prevented", False):
                            logging.debug(f"COMBAT: Damage to planeswalker {planeswalker_card.name} was prevented or redirected")
                            continue
                
                # Apply the damage and track for counters systems consistency
                original_loyalty = planeswalker_card.loyalty
                planeswalker_card.loyalty -= damage
                
                # Ensure loyalty counters are tracked properly
                if hasattr(controller, "loyalty_counters"):
                    controller["loyalty_counters"][planeswalker_id] = planeswalker_card.loyalty
                
                logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} lost {damage} loyalty, now at {planeswalker_card.loyalty}")
                
                # Track damage for statistics
                if not hasattr(gs, "damage_this_turn"):
                    gs.damage_this_turn = {}
                gs.damage_this_turn[planeswalker_id] = gs.damage_this_turn.get(planeswalker_id, 0) + damage
                
                # Trigger damage events
                if hasattr(gs, 'trigger_ability'):
                    # Trigger on the planeswalker itself
                    gs.trigger_ability(planeswalker_id, "DEALT_DAMAGE", {
                        "damage_amount": damage, 
                        "previous_loyalty": original_loyalty,
                        "is_combat_damage": True
                    })
                    
                    # Also trigger for any permanents that watch for damage to planeswalkers
                    for permanent_id in controller["battlefield"]:
                        if permanent_id != planeswalker_id:  # Don't re-trigger on the planeswalker
                            gs.trigger_ability(permanent_id, "PLANESWALKER_DAMAGED", {
                                "planeswalker_id": planeswalker_id,
                                "damage_amount": damage,
                                "is_combat_damage": True
                            })
                    
                # Check for special ultimates that trigger when damaged
                if hasattr(planeswalker_card, 'damage_triggers_ultimate') and planeswalker_card.damage_triggers_ultimate:
                    if hasattr(planeswalker_card, 'ultimate_damage_threshold'):
                        threshold = planeswalker_card.ultimate_damage_threshold
                        damage_this_turn = gs.damage_this_turn.get(planeswalker_id, 0)
                        
                        if damage_this_turn >= threshold:
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} triggering ultimate from damage threshold")
                            
                            # Trigger the ultimate ability
                            if hasattr(planeswalker_card, 'ultimate_ability_index'):
                                ultimate_idx = planeswalker_card.ultimate_ability_index
                                
                                # Process the ultimate directly if ability handler available
                                if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'activate_planeswalker_ability'):
                                    gs.ability_handler.activate_planeswalker_ability(planeswalker_id, ultimate_idx, controller)
                                # Otherwise use the game state method
                                elif hasattr(gs, 'activate_planeswalker_ability'):
                                    gs.activate_planeswalker_ability(planeswalker_id, ultimate_idx, controller)
                    
                # Check if planeswalker died
                if planeswalker_card.loyalty <= 0:
                    # Check for "dies" replacement effects before moving to graveyard
                    if hasattr(gs, 'apply_replacement_effect'):
                        death_context = {
                            "card_id": planeswalker_id,
                            "card_type": "planeswalker",
                            "controller": controller,
                            "destination": "graveyard",
                            "from_damage": True,
                            "from_combat": True
                        }
                        modified_death, was_replaced = gs.apply_replacement_effect("DIES", death_context)
                        
                        # If not replaced, move to graveyard
                        if not was_replaced or modified_death.get("destination", "graveyard") == "graveyard":
                            gs.move_card(planeswalker_id, controller, "battlefield", controller, "graveyard")
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} died from loyalty loss")
                            planeswalkers_to_remove.append(planeswalker_id)
                            
                            # Trigger death event
                            if hasattr(gs, 'trigger_ability'):
                                # Trigger on the planeswalker
                                gs.trigger_ability(planeswalker_id, "DIES", {
                                    "from_damage": True,
                                    "from_combat": True
                                })
                                
                                # Also trigger for permanents watching for planeswalker deaths
                                for permanent_id in controller["battlefield"]:
                                    if permanent_id != planeswalker_id:
                                        gs.trigger_ability(permanent_id, "PLANESWALKER_DIED", {
                                            "planeswalker_id": planeswalker_id,
                                            "from_damage": True,
                                            "from_combat": True
                                        })
                        else:
                            # Handle custom replacement effect (e.g., exile instead of graveyard)
                            dest = modified_death.get("destination", "graveyard")
                            gs.move_card(planeswalker_id, controller, "battlefield", controller, dest)
                            logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} moved to {dest} instead of dying")
                            planeswalkers_to_remove.append(planeswalker_id)
                    else:
                        # Default behavior without replacement effects
                        gs.move_card(planeswalker_id, controller, "battlefield", controller, "graveyard")
                        logging.debug(f"COMBAT: Planeswalker {planeswalker_card.name} died from loyalty loss")
                        planeswalkers_to_remove.append(planeswalker_id)
                        
                        # Trigger death event
                        if hasattr(gs, 'trigger_ability'):
                            gs.trigger_ability(planeswalker_id, "DIES", {
                                "from_damage": True,
                                "from_combat": True
                            })
                        
        # Remove dead planeswalkers from attack targets
        if hasattr(gs, "planeswalker_attack_targets"):
            for attacker_id, pw_id in list(gs.planeswalker_attack_targets.items()):
                if pw_id in planeswalkers_to_remove:
                    del gs.planeswalker_attack_targets[attacker_id]
                    
        # Remove dead planeswalkers from protectors
        if hasattr(gs, "planeswalker_protectors"):
            for pw_id in list(gs.planeswalker_protectors.keys()):
                if pw_id in planeswalkers_to_remove:
                    del gs.planeswalker_protectors[pw_id]
                        
    def _apply_battle_damage(self):
        """
        Apply damage to battles with enhanced effect processing.
        Handles both single-faced battles and double-faced battles (Sieges),
        with proper handling of defeat mechanics including casting by the defeating player.
        """
        gs = self.game_state
        
        battles_to_remove = []
        chapters_advanced = []  # Track battle cards that advanced chapters
        
        # Track which battles were defeated and by whom
        defeated_battles = {}  # Maps battle_id to the player who defeated it
        
        for battle_id, damage in self.battle_damage.items():
            battle_card = gs._safe_get_card(battle_id)
            if not battle_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if battle_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                continue
            
            # Determine the opposing player (who's dealing damage to this battle)
            opposing_player = gs.p2 if controller == gs.p1 else gs.p1
                
            # Check for damage prevention effects
            if hasattr(gs, 'battle_damage_prevention') and gs.battle_damage_prevention.get(battle_id, False):
                logging.debug(f"COMBAT: Damage to battle {battle_card.name} prevented")
                continue
                
            # Apply damage to battle
            if not hasattr(battle_card, "damage"):
                battle_card.damage = 0
                
            # Create damage context for possible replacement effects
            damage_context = {
                "target_id": battle_id,
                "target_is_battle": True,
                "damage_amount": damage,
                "is_combat_damage": True,
                "controller": controller
            }
            
            # Apply damage replacement effects if available
            modified_context = damage_context
            if hasattr(gs, 'apply_replacement_effect'):
                modified_context, was_replaced = gs.apply_replacement_effect("DAMAGE_TO_BATTLE", damage_context)
                if was_replaced:
                    damage = modified_context.get("damage_amount", damage)
                    # Check if damage was redirected or prevented entirely
                    if damage <= 0 or modified_context.get("prevented", False):
                        logging.debug(f"COMBAT: Damage to battle {battle_card.name} was prevented or redirected")
                        continue
            
            # Apply the damage
            original_damage = battle_card.damage
            battle_card.damage += damage
            
            # Update defense counters (primary tracking mechanism for battles)
            if not hasattr(gs, 'battle_cards'):
                gs.battle_cards = {}
                
            # Initialize defense counters if not already set
            if battle_id not in gs.battle_cards:
                if hasattr(battle_card, 'defense'):
                    gs.battle_cards[battle_id] = battle_card.defense
                else:
                    gs.battle_cards[battle_id] = 0
            
            # Remove defense counters equal to damage dealt
            gs.battle_cards[battle_id] = max(0, gs.battle_cards[battle_id] - damage)
            current_defense = gs.battle_cards[battle_id]
            
            logging.debug(f"COMBAT: Battle {battle_card.name} took {damage} damage, now has {current_defense} defense counters")
            
            # Trigger damage events
            if hasattr(gs, 'trigger_ability'):
                gs.trigger_ability(battle_id, "DEALT_DAMAGE", {
                    "damage_amount": damage, 
                    "previous_damage": original_damage,
                    "is_combat_damage": True
                })
                
            # Check if battle should advance a chapter due to damage
            if hasattr(battle_card, "current_chapter") and hasattr(battle_card, "damage_triggers_advancement"):
                if battle_card.damage_triggers_advancement:
                    # Check damage thresholds for chapter advancement
                    if hasattr(battle_card, "chapter_thresholds") and battle_card.chapter_thresholds:
                        # Find the next threshold this damage exceeds
                        for threshold in sorted(battle_card.chapter_thresholds):
                            if original_damage < threshold <= battle_card.damage:
                                battle_card.current_chapter += 1
                                logging.debug(f"COMBAT: Battle {battle_card.name} advanced to chapter {battle_card.current_chapter} due to damage")
                                
                                # Add to chapters advanced tracking
                                chapters_advanced.append({
                                    "battle_id": battle_id,
                                    "controller": controller,
                                    "chapter": battle_card.current_chapter
                                })
                                
                                # Trigger chapter advancement
                                if hasattr(gs, 'trigger_ability'):
                                    gs.trigger_ability(battle_id, "CHAPTER_ADVANCED", {"chapter": battle_card.current_chapter})
                                break
            
            # Check if battle is defeated (defense counters reduced to 0)
            battle_defeated = current_defense <= 0
                
            if battle_defeated:
                logging.debug(f"COMBAT: Battle {battle_card.name} defeated with 0 defense counters")
                
                # Record which player defeated this battle
                defeated_battles[battle_id] = opposing_player
                
                # Check for "defeated" replacement effects before moving to graveyard
                defeat_prevented = False
                if hasattr(gs, 'apply_replacement_effect'):
                    defeat_context = {
                        "card_id": battle_id,
                        "card_type": "battle",
                        "controller": controller,
                        "destination": "graveyard",
                        "defeat_reason": "damage",
                        "defeating_player": opposing_player
                    }
                    modified_defeat, was_replaced = gs.apply_replacement_effect("BATTLE_DEFEATED", defeat_context)
                    
                    if was_replaced and modified_defeat.get("prevented", False):
                        defeat_prevented = True
                        logging.debug(f"COMBAT: Battle {battle_card.name} defeat was prevented by replacement effect")
                
                if not defeat_prevented:
                    # Add to list of battles to remove
                    battles_to_remove.append(battle_id)
                    
                    # Trigger defeat event
                    if hasattr(gs, 'trigger_ability'):
                        gs.trigger_ability(battle_id, "BATTLE_DEFEATED", {
                            "from_damage": True,
                            "defeating_player": opposing_player
                        })
                        
        # Process defeated battles
        for battle_id in battles_to_remove:
            battle_card = gs._safe_get_card(battle_id)
            if not battle_card:
                continue
                
            # Find controller
            controller = None
            for player in [gs.p1, gs.p2]:
                if battle_id in player["battlefield"]:
                    controller = player
                    break
                    
            if not controller:
                continue
                
            # Get the player who defeated the battle
            defeating_player = defeated_battles.get(battle_id, None)
            if not defeating_player:
                # Fallback - use opponent as defeating player
                defeating_player = gs.p2 if controller == gs.p1 else gs.p1
            
            # Determine if this is a double-faced battle (Siege)
            is_double_faced = False
            if hasattr(battle_card, 'is_tdfc') and battle_card.is_tdfc:
                is_double_faced = True
            elif hasattr(battle_card, 'faces') and len(getattr(battle_card, 'faces', [])) > 1:
                is_double_faced = True
            elif hasattr(battle_card, 'subtypes') and 'siege' in [s.lower() for s in battle_card.subtypes]:
                is_double_faced = True
            
            # Move the battle to graveyard
            gs.move_card(battle_id, controller, "battlefield", controller, "graveyard")
            logging.debug(f"COMBAT: Battle {battle_card.name} moved to graveyard after defeat")
            
            # For double-faced battles (Sieges), the defeating player gets to cast the back face
            if is_double_faced:
                # Get back face information
                back_face_id = None
                
                # Different ways to access back face depending on implementation
                if hasattr(battle_card, 'back_face_id'):
                    back_face_id = battle_card.back_face_id
                elif hasattr(battle_card, 'faces') and len(battle_card.faces) > 1:
                    # If using a faces array, get the second face
                    back_face_id = battle_card.faces[1].get('id', None)
                
                if back_face_id is not None:
                    # Cast the back face for the defeating player without mana cost
                    if hasattr(gs, 'cast_spell'):
                        # Prepare context with "no_cost" to indicate free casting
                        context = {
                            "no_cost": True,
                            "from_battle_defeat": True,
                            "original_battle_id": battle_id,
                            "skip_default_movement": True  # Prevent moving to graveyard after resolution
                        }
                        
                        # Get the back face card object
                        back_face = gs._safe_get_card(back_face_id)
                        if back_face:
                            logging.debug(f"COMBAT: Player {defeating_player['name']} casting back face {back_face.name} after defeating battle")
                            
                            # Move the back face to the defeating player's hand temporarily
                            if battle_id in controller["graveyard"]:
                                controller["graveyard"].remove(battle_id)
                            defeating_player["hand"].append(back_face_id)
                            
                            # Cast the spell
                            gs.cast_spell(back_face_id, defeating_player, context=context)
                        else:
                            logging.warning(f"COMBAT: Could not find back face card for battle {battle_card.name}")
                    else:
                        logging.warning(f"COMBAT: Cannot cast back face, cast_spell method not available")
                else:
                    logging.warning(f"COMBAT: Could not determine back face for battle {battle_card.name}")
            else:
                # For single-faced battles, just process any "on defeat" rewards
                if hasattr(battle_card, 'defeat_reward'):
                    reward = battle_card.defeat_reward
                    logging.debug(f"COMBAT: Processing defeat reward for single-faced battle {battle_card.name}")
                    
                    # Process different reward types
                    if reward.get('type') == 'draw_cards':
                        count = reward.get('amount', 1)
                        for _ in range(count):
                            if hasattr(defeating_player, "library") and defeating_player["library"]:
                                card_id = defeating_player["library"].pop(0)
                                defeating_player["hand"].append(card_id)
                        logging.debug(f"COMBAT: Player {defeating_player['name']} drew {count} cards as battle reward")
                    
                    elif reward.get('type') == 'create_token':
                        token_data = reward.get('token_data', {})
                        if hasattr(gs, 'create_token') and token_data:
                            gs.create_token(defeating_player, token_data)
                            logging.debug(f"COMBAT: Player {defeating_player['name']} created token as battle reward")
                    
                    elif reward.get('type') == 'gain_life':
                        amount = reward.get('amount', 1)
                        defeating_player["life"] += amount
                        logging.debug(f"COMBAT: Player {defeating_player['name']} gained {amount} life as battle reward")
        
        # Clean up any battle-related data for removed battles
        if battles_to_remove and hasattr(gs, 'battle_damage_prevention'):
            for battle_id in battles_to_remove:
                if battle_id in gs.battle_damage_prevention:
                    del gs.battle_damage_prevention[battle_id]
        
        # Process chapter abilities for advanced chapters
        for chapter_data in chapters_advanced:
            battle_id = chapter_data["battle_id"]
            chapter = chapter_data["chapter"]
            controller = chapter_data["controller"]
            
            # Process chapter abilities if available
            battle_card = gs._safe_get_card(battle_id)
            if battle_card and hasattr(battle_card, 'chapter_abilities'):
                if chapter in battle_card.chapter_abilities:
                    ability = battle_card.chapter_abilities[chapter]
                    
                    # Build ability context
                    ability_context = {
                        "chapter": chapter,
                        "from_damage": True,
                        "controller": controller
                    }
                    
                    # Process chapter ability
                    if hasattr(gs, 'ability_handler') and hasattr(gs.ability_handler, 'process_chapter_ability'):
                        gs.ability_handler.process_chapter_ability(battle_id, chapter, ability, ability_context)
                    else:
                        logging.debug(f"COMBAT: Processing chapter {chapter} ability for {battle_card.name}")
                        gs.trigger_ability(battle_id, "CHAPTER_ABILITY", ability_context)
                
    def process_ninjutsu(self, ninjutsu_card_id, attacker_id):
        """Process the ninjutsu ability during combat"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for ninjutsu")
            return False
            
        return self.game_state.combat_action_handler.handle_ninjutsu(ninjutsu_card_id, attacker_id)
        
    def assign_first_strike_order(self):
        """Set the damage assignment order for first strike"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for first strike ordering")
            return False
            
        return self.game_state.combat_action_handler.handle_first_strike_order()
        
    def assign_multiple_blockers(self, attacker_index):
        """Assign multiple blockers to an attacker"""
        if not hasattr(self.game_state, 'combat_action_handler'):
            logging.warning("No combat action handler available for multiple blocker assignment")
            return False
            
        return self.game_state.combat_action_handler.handle_assign_multiple_blockers(attacker_index)
        
    def damage_to_battle(self, battle_id, source_id, amount):
        """Deal damage to a battle"""
        gs = self.game_state
        battle_card = gs._safe_get_card(battle_id)
        source_card = gs._safe_get_card(source_id)
        
        if not battle_card or not source_card:
            return False
            
        # Record the damage
        self.battle_damage[battle_id] += amount
        
        logging.debug(f"COMBAT: {source_card.name} deals {amount} damage to battle {battle_card.name}")
        return True
