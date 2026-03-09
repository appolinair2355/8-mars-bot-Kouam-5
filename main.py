import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timedelta
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    ALL_SUITS, SUIT_DISPLAY
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: 
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH: 
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN: 
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

pending_predictions: Dict[int, dict] = {}
current_game_number = 0
last_source_game_number = 0
last_prediction_time: Optional[datetime] = None
prediction_channel_ok = False
client = None
suit_block_until: Dict[str, datetime] = {}
waiting_finalization: Dict[int, dict] = {}

# Compteur2 - Gestion des costumes manquants (interne uniquement)
compteur2_trackers: Dict[str, 'Compteur2Tracker'] = {}
compteur2_seuil_B = 2  # Seuil par défaut
compteur2_active = True

# Gestion des écarts entre prédictions
MIN_GAP_BETWEEN_PREDICTIONS = 2  # Écart minimum entre 2 prédictions
last_prediction_number_sent = 0  # Dernier numéro de prédiction envoyé

# Historiques pour la commande /history
finalized_messages_history: List[Dict] = []
MAX_HISTORY_SIZE = 50
prediction_history: List[Dict] = []

# NOUVEAU: File d'attente de prédictions (plusieurs prédictions possibles)
prediction_queue: List[Dict] = []  # File ordonnée des prédictions en attente
PREDICTION_SEND_AHEAD = 2  # Envoyer la prédiction quand canal source est à N-2

# Valeur à ajouter pour la règle de distribution (configurable par admin)
DISTRIBUTION_PLUS_VALUE = 5  # Valeur par défaut: +5

# ============================================================================
# FONCTION UTILITAIRE - Conversion ID Canal
# ============================================================================

def normalize_channel_id(channel_id) -> int:
    if not channel_id:
        return None
    
    channel_str = str(channel_id)
    
    if channel_str.startswith('-100'):
        return int(channel_str)
    
    if channel_str.startswith('-'):
        return int(channel_str)
    
    return int(f"-100{channel_str}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        
        normalized_id = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized_id)
        
        if hasattr(entity, 'broadcast') and entity.broadcast:
            logger.info(f"✅ Canal résolu: {entity.title} (ID: {normalized_id})")
            return entity
        
        if hasattr(entity, 'megagroup') and entity.megagroup:
            logger.info(f"✅ Groupe résolu: {entity.title} (ID: {normalized_id})")
            return entity
            
        return entity
        
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# CLASSE COMPTEUR2 TRACKER
# ============================================================================

@dataclass
class Compteur2Tracker:
    """Tracker pour le compteur2 (costumes manquants dans 1er groupe)."""
    suit: str
    counter: int = 0
    last_increment_game: int = 0
    
    def get_display_name(self) -> str:
        names = {
            '♠': '♠️ Pique',
            '♥': '❤️ Cœur',
            '♦': '♦️ Carreau',
            '♣': '♣️ Trèfle'
        }
        return names.get(self.suit, self.suit)
    
    def increment(self, game_number: int):
        self.counter += 1
        self.last_increment_game = game_number
        logger.info(f"📊 Compteur2 {self.suit}: {self.counter} (incrémenté au jeu #{game_number})")
    
    def reset(self, game_number: int):
        if self.counter > 0:
            logger.info(f"🔄 Compteur2 {self.suit}: reset de {self.counter} à 0 (trouvé au jeu #{game_number})")
        self.counter = 0
        self.last_increment_game = 0
    
    def check_threshold(self, seuil_B: int) -> bool:
        return self.counter >= seuil_B

# ============================================================================
# FONCTIONS D'HISTORIQUE
# ============================================================================

def add_to_history(game_number: int, message_text: str, first_group: str, suits_found: List[str]):
    global finalized_messages_history
    
    entry = {
        'timestamp': datetime.now(),
        'game_number': game_number,
        'message_text': message_text[:200],
        'first_group': first_group,
        'suits_found': suits_found,
        'predictions_verified': []
    }
    
    finalized_messages_history.insert(0, entry)
    
    if len(finalized_messages_history) > MAX_HISTORY_SIZE:
        finalized_messages_history = finalized_messages_history[:MAX_HISTORY_SIZE]

def add_prediction_to_history(game_number: int, suit: str, verification_games: List[int], prediction_type: str = 'standard'):
    global prediction_history
    
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'predicted_at': datetime.now(),
        'verification_games': verification_games,
        'status': 'en_cours',
        'verified_at': None,
        'verified_by_game': None,
        'rattrapage_level': 0,
        'verified_by': [],
        'type': prediction_type
    })
    
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_in_history(game_number: int, suit: str, verified_by_game: int, 
                                verified_by_group: str, rattrapage_level: int, final_status: str):
    global finalized_messages_history, prediction_history
    
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['verified_by'].append({
                'game_number': verified_by_game,
                'first_group': verified_by_group,
                'rattrapage_level': rattrapage_level
            })
            pred['status'] = final_status
            pred['verified_at'] = datetime.now()
            pred['verified_by_game'] = verified_by_game
            pred['rattrapage_level'] = rattrapage_level
            break
    
    for msg in finalized_messages_history:
        if msg['game_number'] == verified_by_game:
            msg['predictions_verified'].append({
                'predicted_game': game_number,
                'suit': suit,
                'rattrapage_level': rattrapage_level
            })
            break

# ============================================================================
# INITIALISATION
# ============================================================================

def initialize_trackers():
    """Initialise les trackers Compteur2."""
    global compteur2_trackers
    
    for suit in ALL_SUITS:
        compteur2_trackers[suit] = Compteur2Tracker(suit=suit)
        logger.info(f"📊 Compteur2 {suit}: initialisé")

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def is_message_being_edited(message: str) -> bool:
    return '⏰' in message

def extract_parentheses_groups(message: str) -> List[str]:
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        return [f"{score}:{content}" if score else content for score, content in scored_groups]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]
    
    normalized = group_str
    for old, new in [('❤️', '♥'), ('❤', '♥'), ('♥️', '♥'),
                     ('♠️', '♠'), ('♦️', '♦'), ('♣️', '♣')]:
        normalized = normalized.replace(old, new)
    
    return [suit for suit in ALL_SUITS if suit in normalized]

def block_suit(suit: str, minutes: int = 5):
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# GESTION DES PRÉDICTIONS - MESSAGES SIMPLIFIÉS
# ============================================================================

def format_prediction_message(game_number: int, suit: str, status: str = 'en_cours', 
                             current_check: int = None, verified_games: List[int] = None,
                             rattrapage: int = 0) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    
    if status == 'en_cours':
        verif_parts = []
        
        for i in range(3):
            check_num = game_number + i
            
            if current_check == check_num:
                verif_parts.append(f"🔵{check_num}")
            elif verified_games and check_num in verified_games:
                continue
            else:
                verif_parts.append(f"#{check_num}")
        
        verif_line = " | ".join(verif_parts)
        
        return f"""🎰 PRÉDICTION #{game_number}
🎯 Couleur: {suit_display}
📊 Statut: En cours ⏳
🔍 Vérification: {verif_line}"""
    
    elif status == 'gagne':
        if rattrapage == 0:
            status_text = "✅0️⃣GAGNÉ DIRECT 🎉"
        else:
            status_text = f"✅{rattrapage}️⃣GAGNÉ R{rattrapage} 🎉"
        
        return f"""🏆 **PRÉDICTION #{game_number}**

🎯 **Couleur:** {suit_display}
✅ **Statut:** {status_text}"""
    
    elif status == 'perdu':
        return f"""💔 **PRÉDICTION #{game_number}**

🎯 **Couleur:** {suit_display}
❌ **Statut:** PERDU 😭"""
    
    return ""

async def send_prediction(game_number: int, suit: str, prediction_type: str = 'standard') -> Optional[int]:
    """Envoie une prédiction au canal configuré."""
    global last_prediction_time, last_prediction_number_sent
    
    try:
        # Vérifier si la couleur est bloquée
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        if not PREDICTION_CHANNEL_ID:
            logger.error("❌ PREDICTION_CHANNEL_ID non configuré!")
            return None
        
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error(f"❌ Impossible d'accéder au canal {PREDICTION_CHANNEL_ID}")
            return None
        
        msg = format_prediction_message(game_number, suit, 'en_cours', game_number, [])
        
        try:
            sent = await client.send_message(prediction_entity, msg, parse_mode='markdown')
            last_prediction_time = datetime.now()
            last_prediction_number_sent = game_number
            
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent.id,
                'status': 'en_cours',
                'type': prediction_type,
                'sent_time': datetime.now(),
                'verification_games': [game_number, game_number + 1, game_number + 2],
                'verified_games': [],
                'found_at': None,
                'rattrapage': 0,
                'current_check': game_number
            }
            
            add_prediction_to_history(game_number, suit, [game_number, game_number + 1, game_number + 2], prediction_type)
            
            logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} (type: {prediction_type})")
            return sent.id
            
        except ChatWriteForbiddenError:
            logger.error(f"❌ Bot n'a pas la permission d'écrire dans le canal")
            return None
        except UserBannedInChannelError:
            logger.error(f"❌ Bot banni du canal")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def update_prediction_message(game_number: int, status: str, rattrapage: int = 0):
    """Met à jour le statut d'une prédiction."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    
    new_msg = format_prediction_message(game_number, suit, status, rattrapage=rattrapage)
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal de prédiction non accessible pour mise à jour")
            return
            
        await client.edit_message(prediction_entity, msg_id, new_msg, parse_mode='markdown')
        pred['status'] = status
        
        if 'gagne' in status:
            logger.info(f"✅ Gagné: #{game_number} (R{rattrapage})")
        else:
            logger.info(f"❌ Perdu: #{game_number}")
            block_suit(suit, 5)
        
        del pending_predictions[game_number]
        
    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

async def update_prediction_progress(game_number: int, current_check: int):
    """Met à jour l'affichage de la progression - efface les numéros passés."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    verified_games = pred.get('verified_games', [])
    
    pred['current_check'] = current_check
    
    msg = format_prediction_message(game_number, suit, 'en_cours', current_check, verified_games)
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity:
            await client.edit_message(prediction_entity, msg_id, msg, parse_mode='markdown')
    except Exception as e:
        logger.error(f"❌ Erreur update progress: {e}")

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    suits_in_result = get_suits_in_group(first_group)
    
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred['status'] != 'en_cours':
            return False
            
        target_suit = pred['suit']
        
        if game_number in pred['verified_games']:
            return False
        
        pred['verified_games'].append(game_number)
        
        logger.info(f"🔍 Vérification #{game_number}: {target_suit} dans {suits_in_result}?")
        
        if target_suit in suits_in_result:
            await update_prediction_message(game_number, 'gagne', 0)
            update_prediction_in_history(game_number, target_suit, game_number, first_group, 0, 'gagne_r0')
            return True
        else:
            pred['rattrapage'] = 1
            next_check = game_number + 1
            logger.info(f"❌ #{game_number} non trouvé, attente #{next_check}")
            await update_prediction_progress(game_number, next_check)
            return False
    
    for original_game, pred in list(pending_predictions.items()):
        if pred['status'] != 'en_cours':
            continue
            
        target_suit = pred['suit']
        rattrapage = pred.get('rattrapage', 0)
        expected_game = original_game + rattrapage
        
        if game_number == expected_game and rattrapage > 0:
            if game_number in pred['verified_games']:
                return False
            
            pred['verified_games'].append(game_number)
            
            logger.info(f"🔍 Vérification R{rattrapage} #{game_number}: {target_suit} dans {suits_in_result}?")
            
            if target_suit in suits_in_result:
                await update_prediction_message(original_game, 'gagne', rattrapage)
                update_prediction_in_history(original_game, target_suit, game_number, first_group, rattrapage, f'gagne_r{rattrapage}')
                return True
            else:
                if rattrapage < 2:
                    pred['rattrapage'] = rattrapage + 1
                    next_check = original_game + rattrapage + 1
                    logger.info(f"❌ R{rattrapage} échoué, attente #{next_check}")
                    await update_prediction_progress(original_game, next_check)
                    return False
                else:
                    logger.info(f"❌ R2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, 'perdu', 2)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
                    return False
    
    return False

# ============================================================================
# GESTION #R ET COMPTEUR2 (interne uniquement)
# ============================================================================

def extract_first_two_groups(message: str) -> tuple:
    groups = extract_parentheses_groups(message)
    if len(groups) >= 2:
        return groups[0], groups[1]
    elif len(groups) == 1:
        return groups[0], ""
    return "", ""

def check_distribution_rule(game_number: int, message_text: str) -> Optional[tuple]:
    global DISTRIBUTION_PLUS_VALUE
    
    if '#R' not in message_text:
        return None
    
    first_group, second_group = extract_first_two_groups(message_text)
    
    if not first_group and not second_group:
        return None
    
    suits_first = set(get_suits_in_group(first_group))
    suits_second = set(get_suits_in_group(second_group))
    all_suits_found = suits_first.union(suits_second)
    
    all_suits = set(ALL_SUITS)
    missing_suits = all_suits - all_suits_found
    
    if len(missing_suits) == 1:
        missing_suit = list(missing_suits)[0]
        prediction_number = game_number + DISTRIBUTION_PLUS_VALUE
        logger.info(f"🎯 #R DÉTECTÉ: {missing_suit} manquant → Prédiction #{prediction_number} (base #{game_number} + {DISTRIBUTION_PLUS_VALUE})")
        return (missing_suit, prediction_number)
    
    return None

def update_compteur2(game_number: int, first_group: str):
    global compteur2_trackers, compteur2_seuil_B
    
    suits_in_first = set(get_suits_in_group(first_group))
    
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        
        if suit in suits_in_first:
            tracker.reset(game_number)
        else:
            tracker.increment(game_number)

def get_compteur2_ready_predictions(current_game: int) -> List[tuple]:
    global compteur2_trackers, compteur2_seuil_B
    
    ready = []
    for suit in ALL_SUITS:
        tracker = compteur2_trackers[suit]
        if tracker.check_threshold(compteur2_seuil_B):
            pred_number = current_game + 2
            ready.append((suit, pred_number))
            tracker.reset(current_game)
    
    return ready

# ============================================================================
# NOUVEAU: GESTION INTELLIGENTE DE LA FILE D'ATTENTE
# ============================================================================

def can_accept_prediction(pred_number: int) -> bool:
    """
    Vérifie si une nouvelle prédiction peut être acceptée en vérifiant l'écart
    avec toutes les prédictions existantes (file d'attente + en cours).
    """
    global prediction_queue, pending_predictions, last_prediction_number_sent, MIN_GAP_BETWEEN_PREDICTIONS
    
    # Vérifier écart avec la dernière prédiction envoyée
    if last_prediction_number_sent > 0:
        gap = pred_number - last_prediction_number_sent
        if gap < MIN_GAP_BETWEEN_PREDICTIONS:
            logger.info(f"⛔ Écart insuffisant avec dernier envoyé (#{last_prediction_number_sent}): {gap} < {MIN_GAP_BETWEEN_PREDICTIONS}")
            return False
    
    # Vérifier écart avec toutes les prédictions en cours de vérification
    for existing_num in pending_predictions.keys():
        gap = abs(pred_number - existing_num)
        if gap < MIN_GAP_BETWEEN_PREDICTIONS:
            logger.info(f"⛔ Écart insuffisant avec prédiction en cours (#{existing_num}): {gap} < {MIN_GAP_BETWEEN_PREDICTIONS}")
            return False
    
    # Vérifier écart avec toutes les prédictions dans la file d'attente
    for queued_pred in prediction_queue:
        existing_num = queued_pred['game_number']
        gap = abs(pred_number - existing_num)
        if gap < MIN_GAP_BETWEEN_PREDICTIONS:
            logger.info(f"⛔ Écart insuffisant avec file d'attente (#{existing_num}): {gap} < {MIN_GAP_BETWEEN_PREDICTIONS}")
            return False
    
    return True

def add_to_prediction_queue(game_number: int, suit: str, prediction_type: str) -> bool:
    """
    Ajoute une prédiction à la file d'attente si l'écart est respecté.
    La file est maintenue triée par numéro de prédiction.
    """
    global prediction_queue
    
    # Vérifier si déjà dans la file
    for pred in prediction_queue:
        if pred['game_number'] == game_number:
            logger.info(f"⚠️ Prédiction #{game_number} déjà dans la file")
            return False
    
    # Vérifier l'écart dynamique
    if not can_accept_prediction(game_number):
        logger.info(f"❌ Prédiction #{game_number} rejetée - écart insuffisant")
        return False
    
    new_pred = {
        'game_number': game_number,
        'suit': suit,
        'type': prediction_type,
        'added_at': datetime.now()
    }
    
    prediction_queue.append(new_pred)
    # Trier par numéro de prédiction (ordre croissant)
    prediction_queue.sort(key=lambda x: x['game_number'])
    
    logger.info(f"📥 Prédiction #{game_number} ({suit}) ajoutée à la file. Total: {len(prediction_queue)}")
    return True

async def process_prediction_queue(current_game: int):
    """
    Traite la file d'attente et envoie les prédictions dont c'est le moment.
    Une prédiction est envoyée quand: canal >= pred_number - PREDICTION_SEND_AHEAD
    ET qu'il n'y a pas de conflit avec les prédictions déjà envoyées.
    """
    global prediction_queue, pending_predictions
    
    to_remove = []
    
    for pred in prediction_queue:
        pred_number = pred['game_number']
        suit = pred['suit']
        pred_type = pred['type']
        
        # Condition d'envoi: canal à N-2 ou plus proche
        if current_game >= pred_number - PREDICTION_SEND_AHEAD:
            # Vérifier qu'on n'a pas déjà une prédiction en cours qui bloque
            if pending_predictions:
                # Vérifier écart avec les prédictions actives
                can_send = True
                for active_num in pending_predictions.keys():
                    gap = abs(pred_number - active_num)
                    if gap < MIN_GAP_BETWEEN_PREDICTIONS:
                        logger.info(f"⏳ Prédiction #{pred_number} attend - conflit avec active #{active_num}")
                        can_send = False
                        break
                
                if not can_send:
                    continue
            
            # Envoyer la prédiction
            logger.info(f"📤 Envoi depuis file: #{pred_number} (canal à #{current_game})")
            msg_id = await send_prediction(pred_number, suit, pred_type)
            
            if msg_id:
                to_remove.append(pred)
            else:
                logger.warning(f"⚠️ Échec envoi #{pred_number}, conservation dans file")
    
    # Nettoyer la file
    for pred in to_remove:
        prediction_queue.remove(pred)
        logger.info(f"✅ #{pred['game_number']} retiré de la file. Restant: {len(prediction_queue)}")

# ============================================================================
# TRAITEMENT DES MESSAGES
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    global current_game_number, last_source_game_number
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    # Vérification auto-reset uniquement au #1440
    if current_game_number >= 1440:
        logger.warning(f"🚨 RESET #1440 atteint")
        await perform_full_reset("🚨 Reset automatique - Numéro #1440 atteint")
        return
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return
    
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    
    logger.info(f"📊 Jeu #{game_number}: {suits_in_first} dans '{first_group[:30]}...'")
    
    add_to_history(game_number, message_text, first_group, suits_in_first)
    
    # Vérification des prédictions existantes
    await check_prediction_result(game_number, first_group)
    
    # NOUVEAU: Traiter la file d'attente (envoi des prédictions prêtes)
    await process_prediction_queue(game_number)
    
    # Distribution #R - Ajouter à la file si écart OK
    distribution_result = check_distribution_rule(game_number, message_text)
    if distribution_result:
        suit, pred_num = distribution_result
        added = add_to_prediction_queue(pred_num, suit, 'distribution')
        if added:
            logger.info(f"🎯 Distribution: #{pred_num} en file d'attente")
    
    # Compteur2 - Ajouter à la file si écart OK
    if compteur2_active:
        update_compteur2(game_number, first_group)
        
        compteur2_preds = get_compteur2_ready_predictions(game_number)
        for suit, pred_num in compteur2_preds:
            added = add_to_prediction_queue(pred_num, suit, 'compteur2')
            if added:
                logger.info(f"📊 Compteur2: #{pred_num} en file d'attente")

async def handle_message(event, is_edit: bool = False):
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        normalized_source = normalize_channel_id(SOURCE_CHANNEL_ID)
        if chat_id != normalized_source:
            return
        
        message_text = event.message.message
        edit_info = " [EDITÉ]" if is_edit else ""
        logger.info(f"📨{edit_info} Msg {event.message.id}: {message_text[:60]}...")
        
        if is_message_being_edited(message_text):
            logger.info(f"⏳ Message en cours d'édition (⏰), ignoré")
            if '⏰' in message_text:
                match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
                if match:
                    waiting_finalization[int(match.group(1))] = {
                        'msg_id': event.message.id,
                        'text': message_text
                    }
            return
        
        if not is_message_finalized(message_text):
            logger.info(f"⏳ Non finalisé ignoré")
            return
        
        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:^|[^\d])(\d{3,4})(?:[^\d]|$)", message_text)
        
        if not match:
            logger.warning("⚠️ Numéro non trouvé")
            return
        
        game_number = int(match.group(1))
        
        if game_number in waiting_finalization:
            del waiting_finalization[game_number]
        
        await process_game_result(game_number, message_text)
        
    except Exception as e:
        logger.error(f"❌ Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_new_message(event):
    await handle_message(event, False)

async def handle_edited_message(event):
    await handle_message(event, True)

# ============================================================================
# RESET ET NOTIFICATIONS
# ============================================================================

async def notify_admin_reset(reason: str, stats: int, queue_stats: int):
    """Envoie une notification de reset à l'admin en privé."""
    if not ADMIN_ID or ADMIN_ID == 0:
        logger.warning("⚠️ ADMIN_ID non configuré, impossible de notifier")
        return
    
    try:
        admin_entity = await client.get_entity(ADMIN_ID)
        
        msg = f"""🔄 **RESET SYSTÈME**

{reason}

✅ Compteurs internes remis à zéro
✅ {stats} prédictions actives cleared
✅ {queue_stats} prédictions en file cleared
✅ Nouvelle analyse

🤖 Baccarat AI"""
        
        await client.send_message(admin_entity, msg, parse_mode='markdown')
        logger.info(f"✅ Notification reset envoyée à l'admin {ADMIN_ID}")
        
    except Exception as e:
        logger.error(f"❌ Impossible de notifier l'admin: {e}")

async def auto_reset_system():
    """Mode veille - plus de reset à 1h00."""
    while True:
        try:
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time, waiting_finalization
    global last_prediction_number_sent, compteur2_trackers, prediction_queue
    
    stats = len(pending_predictions)
    queue_stats = len(prediction_queue)
    
    for tracker in compteur2_trackers.values():
        tracker.counter = 0
        tracker.last_increment_game = 0
    
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    last_prediction_time = None
    last_prediction_number_sent = 0
    suit_block_until.clear()
    
    logger.info(f"🔄 {reason} - {stats} actives cleared, {queue_stats} file cleared, Compteur2 reset")
    
    await notify_admin_reset(reason, stats, queue_stats)

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_plus(event):
    global DISTRIBUTION_PLUS_VALUE
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            await event.respond(
                f"➕ **CONFIGURATION DISTRIBUTION #R**\n\n"
                f"Valeur actuelle: **+{DISTRIBUTION_PLUS_VALUE}**\n\n"
                f"💡 Règle: Quand #R détecté au jeu #N, prédiction à #N+{DISTRIBUTION_PLUS_VALUE}\n\n"
                f"**Usage:** `/plus [1-20]`"
            )
            return
        
        arg = parts[1]
        
        try:
            plus_val = int(arg)
            if not 1 <= plus_val <= 20:
                await event.respond("❌ La valeur doit être entre 1 et 20")
                return
            
            old_val = DISTRIBUTION_PLUS_VALUE
            DISTRIBUTION_PLUS_VALUE = plus_val
            
            await event.respond(
                f"✅ **Valeur modifiée**\n\n"
                f"Ancienne: +{old_val}\n"
                f"Nouvelle: **+{plus_val}**"
            )
            logger.info(f"Admin change valeur distribution: +{old_val} → +{plus_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/plus [1-20]`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_plus: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_gap(event):
    global MIN_GAP_BETWEEN_PREDICTIONS
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            await event.respond(
                f"📏 **CONFIGURATION DES ÉCARTS**\n\n"
                f"Écart minimum actuel: **{MIN_GAP_BETWEEN_PREDICTIONS}** numéros\n\n"
                f"💡 Les prédictions doivent être espacées d'au moins {MIN_GAP_BETWEEN_PREDICTIONS} numéros\n\n"
                f"**Usage:** `/gap [2-10]`"
            )
            return
        
        arg = parts[1].lower()
        
        try:
            gap_val = int(arg)
            if not 2 <= gap_val <= 10:
                await event.respond("❌ L'écart doit être entre 2 et 10")
                return
            
            old_gap = MIN_GAP_BETWEEN_PREDICTIONS
            MIN_GAP_BETWEEN_PREDICTIONS = gap_val
            
            await event.respond(
                f"✅ **Écart modifié**\n\n"
                f"Ancien: {old_gap}\n"
                f"Nouveau: **{gap_val}**"
            )
            logger.info(f"Admin change écart: {old_gap} → {gap_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/gap [2-10]`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_gap: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_queue(event):
    """NOUVEAU: Voir la file d'attente de prédictions."""
    global prediction_queue
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📋 **FILE D'ATTENTE DES PRÉDICTIONS**",
        f"Écart minimum: {MIN_GAP_BETWEEN_PREDICTIONS} numéros",
        f"Envoi quand canal >= N-{PREDICTION_SEND_AHEAD}",
        "",
    ]
    
    if not prediction_queue:
        lines.append("❌ Aucune prédiction en attente")
    else:
        lines.append(f"**{len(prediction_queue)} prédictions en attente:**\n")
        
        for i, pred in enumerate(prediction_queue, 1):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            pred_type = pred['type']
            pred_num = pred['game_number']
            distance = pred_num - current_game_number
            
            if pred_type == 'distribution':
                type_str = "🎯 Distrib"
            elif pred_type == 'compteur2':
                type_str = "📊 Cpt2"
            else:
                type_str = "🤖 Auto"
            
            if current_game >= pred_num - PREDICTION_SEND_AHEAD:
                status = "🟢 PRÊT (attente conflit)"
            else:
                status = f"⏳ Dans {pred_num - PREDICTION_SEND_AHEAD - current_game} numéros"
            
            lines.append(f"{i}. #{pred_num} {suit} | {type_str} | {status}")
    
    lines.append("")
    lines.append(f"🎮 Canal actuel: #{current_game_number}")
    lines.append(f"🎯 Dernier envoyé: #{last_prediction_number_sent if last_prediction_number_sent else 'Aucune'}")
    
    await event.respond("\n".join(lines))

async def cmd_compteur2(event):
    global compteur2_seuil_B, compteur2_active, compteur2_trackers
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            status_str = "✅ ACTIF" if compteur2_active else "❌ INACTIF"
            
            lines = [
                "📊 **COMPTEUR2 (INTERNE)**",
                f"Statut: {status_str}",
                f"🎯 Seuil B: {compteur2_seuil_B}",
                f"📏 Écart: {MIN_GAP_BETWEEN_PREDICTIONS}",
                f"📋 File d'attente: {len(prediction_queue)}",
                f"🎮 Dernier jeu: #{current_game_number}",
                "",
                "📈 **Compteurs:**"
            ]
            
            for suit in ALL_SUITS:
                tracker = compteur2_trackers.get(suit)
                if tracker:
                    progress = min(tracker.counter, compteur2_seuil_B)
                    bar = f"[{'█' * progress}{'░' * (compteur2_seuil_B - progress)}]"
                    
                    if tracker.counter >= compteur2_seuil_B:
                        status = "🔮 PRÊT"
                    elif tracker.counter > 0:
                        status = f"⏳ {tracker.counter}/{compteur2_seuil_B}"
                    else:
                        status = "✅ Attente"
                    
                    lines.append(f"{tracker.get_display_name()}: {bar} {status}")
            
            lines.extend([
                "",
                "**Usage:** `/compteur2 [2-10/on/off/reset]`"
            ])
            
            await event.respond("\n".join(lines))
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            compteur2_active = False
            await event.respond("❌ **Compteur2 DÉSACTIVÉ**")
        elif arg == 'on':
            compteur2_active = True
            await event.respond("✅ **Compteur2 ACTIVÉ**")
        elif arg == 'reset':
            for tracker in compteur2_trackers.values():
                tracker.counter = 0
                tracker.last_increment_game = 0
            prediction_queue.clear()
            await event.respond("🔄 **Compteurs et file remis à zéro**")
        else:
            try:
                b_val = int(arg)
                if not 2 <= b_val <= 10:
                    await event.respond("❌ B doit être entre 2 et 10")
                    return
                
                old_b = compteur2_seuil_B
                compteur2_seuil_B = b_val
                
                await event.respond(f"✅ **Seuil B: {old_b} → {b_val}**")
                logger.info(f"Admin change seuil B: {old_b} → {b_val}")
            except ValueError:
                await event.respond("❌ Usage: `/compteur2 [2-10/on/off/reset]`")
                
    except Exception as e:
        logger.error(f"Erreur cmd_compteur2: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📜 **HISTORIQUE**",
        "═══════════════════",
        ""
    ]
    
    recent = prediction_history[:10]
    
    if not recent:
        lines.append("❌ Aucune prédiction")
    else:
        for i, pred in enumerate(recent, 1):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            
            if pred.get('type') == 'distribution':
                rule = f"🎯#R(+{DISTRIBUTION_PLUS_VALUE})"
            elif pred.get('type') == 'compteur2':
                rule = "📊C2"
            else:
                rule = "🤖"
            
            emojis = {
                'en_cours': '🎰',
                'gagne_r0': '🏆',
                'gagne_r1': '🏆',
                'gagne_r2': '🏆',
                'perdu': '💔'
            }
            emoji = emojis.get(status, '❓')
            
            lines.append(f"{i}. {emoji} **#{pred['predicted_game']}** {suit} | {rule} | {status}")
            lines.append(f"   🕐 {pred_time}")
            lines.append("")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    global compteur2_active, compteur2_seuil_B, DISTRIBUTION_PLUS_VALUE
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    compteur2_str = "✅ ON" if compteur2_active else "❌ OFF"
    
    active_info = []
    for num, pred in sorted(pending_predictions.items()):
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        rattrapage = pred.get('rattrapage', 0)
        current_check = pred.get('current_check', num)
        
        if rattrapage == 0:
            status = f"⏳ Vérification #{current_check}"
        else:
            status = f"⏳ R{rattrapage} (🔵#{current_check})"
        
        active_info.append(f"• #{num} {suit}: {status}")
    
    queue_info = []
    for pred in prediction_queue[:5]:
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        queue_info.append(f"• #{pred['game_number']} {suit}")
    
    lines = [
        "📊 **STATUT**",
        "",
        f"➕ Distribution: +{DISTRIBUTION_PLUS_VALUE}",
        f"📏 Écart: {MIN_GAP_BETWEEN_PREDICTIONS}",
        f"📊 Compteur2: {compteur2_str} (B={compteur2_seuil_B})",
        f"📋 File: {len(prediction_queue)} | Actives: {len(pending_predictions)}",
        f"⏱️ Envoi: N-{PREDICTION_SEND_AHEAD}",
        f"🎮 Canal: #{current_game_number}",
        f"🎯 Dernier: #{last_prediction_number_sent if last_prediction_number_sent else '-'}",
        ""
    ]
    
    if active_info:
        lines.append("**🔮 Actives:**")
        lines.extend(active_info)
        lines.append("")
    
    if queue_info:
        lines.append("**📋 File (top 5):**")
        lines.extend(queue_info)
        lines.append("")
    
    lines.append("**/queue** pour voir toute la file")
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    help_text = f"""📖 **BACCARAT AI**

**🎮 Systèmes:**
• 🎯 Distribution #R → N+{DISTRIBUTION_PLUS_VALUE}
• 📊 Compteur2 → N+2 (seuil B)

**📋 Règles:**
• Écart minimum: **{MIN_GAP_BETWEEN_PREDICTIONS}** numéros
• Plusieurs prédictions possibles en file d'attente
• Envoi différé: N-{PREDICTION_SEND_AHEAD}
• Vérification dynamique avec 🔵

**🔧 Commandes:**
`/plus [1-20]` - Valeur #R
`/gap [2-10]` - Écart minimum
`/queue` - Voir la file d'attente
`/compteur2 [B/on/off/reset]`
`/status` - Statut complet
`/history` - Historique
`/reset` - Reset manuel

🤖 Baccarat AI"""
    
    await event.respond(help_text)

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🔄 Reset...")
    await perform_full_reset("Reset manuel")
    await event.respond("✅ Reset effectué!")

def setup_handlers():
    client.add_event_handler(cmd_plus, events.NewMessage(pattern=r'^/plus'))
    client.add_event_handler(cmd_gap, events.NewMessage(pattern=r'^/gap'))
    client.add_event_handler(cmd_queue, events.NewMessage(pattern=r'^/queue$'))
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

async def start_bot():
    global client, prediction_channel_ok
    
    session = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    
    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
        initialize_trackers()
        
        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK")
            except Exception as e:
                logger.error(f"❌ Erreur canal prédiction: {e}")
        
        logger.info("🤖 Bot démarré")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return
        
        asyncio.create_task(auto_reset_system())
        
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT AI 🤖 Running"))
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 Web server port {PORT}")
        logger.info(f"➕ Distribution: +{DISTRIBUTION_PLUS_VALUE}")
        logger.info(f"📏 Écart: {MIN_GAP_BETWEEN_PREDICTIONS}")
        logger.info(f"📋 File d'attente: ACTIVE")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
