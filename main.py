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

# Compteur2 - Gestion des costumes manquants
compteur2_trackers: Dict[str, 'Compteur2Tracker'] = {}
compteur2_seuil_B = 2  # Seuil par défaut
compteur2_active = True
last_prediction_number = 0
prediction_queue = []
last_prediction_sent_time = None
games_without_prediction = 0

# Historiques pour la commande /history
finalized_messages_history: List[Dict] = []
MAX_HISTORY_SIZE = 50
prediction_history: List[Dict] = []

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
        'verified_by': [],
        'type': prediction_type
    })
    
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_in_history(game_number: int, suit: str, verified_by_game: int, 
                                verified_by_group: str, rattrapage_level: int, final_status: Optional[str] = None):
    global finalized_messages_history, prediction_history
    
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['verified_by'].append({
                'game_number': verified_by_game,
                'first_group': verified_by_group,
                'rattrapage_level': rattrapage_level
            })
            if final_status:
                pred['status'] = final_status
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
# GESTION DES PRÉDICTIONS
# ============================================================================

async def can_send_prediction() -> bool:
    global pending_predictions, last_prediction_sent_time
    
    for pred in pending_predictions.values():
        if pred['status'] == 'en_cours':
            return False
    
    if last_prediction_sent_time:
        elapsed = datetime.now() - last_prediction_sent_time
        if elapsed > timedelta(minutes=30):
            logger.warning(f"⚠️ Prédiction en cours bloquée depuis {elapsed.total_seconds()/60:.1f}min")
    
    return True

async def check_and_force_restart_if_needed():
    global games_without_prediction
    
    if games_without_prediction >= 20:
        logger.warning(f"🚨 BLOCAGE DÉTECTÉ: {games_without_prediction} numéros sans prédiction")
        await perform_full_reset("🚨 Redémarrage forcé - Blocage détecté (>20 numéros)")
        return True
    
    if current_game_number >= 1440:
        logger.warning(f"🚨 RESET #1440 atteint")
        await perform_full_reset("🚨 Reset automatique - Numéro #1440 atteint")
        return True
    
    return False

async def send_prediction(game_number: int, suit: str, prediction_type: str = 'standard', is_rattrapage: int = 0) -> Optional[int]:
    global last_prediction_time, last_prediction_sent_time, last_prediction_number, games_without_prediction
    
    try:
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        if not await can_send_prediction():
            logger.info(f"⏳ Prédiction en cours existante, mise en file d'attente: #{game_number} {suit}")
            prediction_queue.append({
                'game_number': game_number,
                'suit': suit,
                'type': prediction_type,
                'added_at': datetime.now()
            })
            return None
        
        if last_source_game_number < game_number - 2:
            logger.info(f"⏳ Attente approche numéro cible: {last_source_game_number}/{game_number - 2}")
            prediction_queue.append({
                'game_number': game_number,
                'suit': suit,
                'type': prediction_type,
                'added_at': datetime.now()
            })
            return None
        
        if not PREDICTION_CHANNEL_ID:
            logger.error("❌ PREDICTION_CHANNEL_ID non configuré dans config.py!")
            return None
        
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error(f"❌ Impossible d'accéder au canal {PREDICTION_CHANNEL_ID}")
            return None
        
        type_indicator = ""
        if prediction_type == 'distribution':
            type_indicator = " [#R]"
        elif prediction_type == 'compteur2':
            type_indicator = " [C2]"
            
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours....{type_indicator}"""
        
        try:
            sent = await client.send_message(prediction_entity, msg)
            last_prediction_time = datetime.now()
            last_prediction_sent_time = datetime.now()
            last_prediction_number = game_number
            games_without_prediction = 0
            
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent.id,
                'status': 'en_cours',
                'rattrapage': is_rattrapage,
                'original_game': game_number if is_rattrapage == 0 else None,
                'awaiting_rattrapage': 0,
                'sent_time': datetime.now(),
                'type': prediction_type
            }
            
            if is_rattrapage == 0:
                verification_games = [game_number, game_number + 1, game_number + 2]
                add_prediction_to_history(game_number, suit, verification_games, prediction_type)
                logger.info(f"📋 Prédiction #{game_number} {suit} ({prediction_type}): vérification sur {verification_games}")
            
            logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} ({prediction_type})")
            return sent.id
            
        except ChatWriteForbiddenError:
            logger.error(f"❌ Bot n'a pas la permission d'écrire dans le canal")
            return None
        except UserBannedInChannelError:
            logger.error(f"❌ Bot banni du canal")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erreur inattendue envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def process_prediction_queue():
    global prediction_queue
    
    if not prediction_queue:
        return
    
    if not await can_send_prediction():
        return
    
    if prediction_queue:
        pred = prediction_queue[0]
        if last_source_game_number >= pred['game_number'] - 2:
            prediction_queue.pop(0)
            await send_prediction(pred['game_number'], pred['suit'], pred['type'])
        else:
            if datetime.now() - pred['added_at'] > timedelta(minutes=5):
                logger.warning(f"⚠️ Prédiction en file d'attente trop vieille, suppression: #{pred['game_number']}")
                prediction_queue.pop(0)

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    suits_in_result = get_suits_in_group(first_group)
    
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            pred_type = pred.get('type', 'standard')
            logger.info(f"🔍 Vérif #{game_number} original ({pred_type}): {target_suit} dans {suits_in_result}")
            
            if target_suit in suits_in_result:
                await update_prediction_message(game_number, '✅0️⃣', True)
                update_prediction_in_history(game_number, target_suit, game_number, first_group, 0, 'gagne_r0')
                await process_prediction_queue()
                return True
            else:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"❌ #{game_number} échoué, attente rattrapage #{game_number + 1}")
                return False
    
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting > 0 and game_number == original_game + awaiting:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif rattrapage R{awaiting} #{game_number}: {target_suit}")
            
            if target_suit in suits_in_result:
                status = f'✅{awaiting}️⃣'
                await update_prediction_message(original_game, status, True, awaiting)
                final_status = f'gagne_r{awaiting}'
                update_prediction_in_history(original_game, target_suit, game_number, first_group, awaiting, final_status)
                await process_prediction_queue()
                return True
            else:
                if awaiting < 2:
                    pred['awaiting_rattrapage'] = awaiting + 1
                    logger.info(f"❌ R{awaiting} échoué, attente #{original_game + awaiting + 1}")
                    return False
                else:
                    logger.info(f"❌ R2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, '❌', False)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
                    await process_prediction_queue()
                    return False
    
    return False

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    pred_type = pred.get('type', 'standard')
    
    type_indicator = ""
    if pred_type == 'distribution':
        type_indicator = " [#R]"
    elif pred_type == 'compteur2':
        type_indicator = " [C2]"
    
    if status == '✅0️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅0️⃣ GAGNÉ{type_indicator}"
    elif status == '✅1️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅1️⃣ GAGNÉ{type_indicator}"
    elif status == '✅2️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅2️⃣ GAGNÉ{type_indicator}"
    else:
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ❌ PERDU 😭{type_indicator}"
    
    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {result_line}"""
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal de prédiction non accessible pour mise à jour")
            return
            
        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status
        
        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {status}")
        else:
            logger.info(f"❌ Perdu: #{game_number}")
            block_suit(suit, 5)
        
        del pending_predictions[game_number]
        
    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# GESTION #R ET COMPTEUR2
# ============================================================================

def extract_first_two_groups(message: str) -> tuple:
    groups = extract_parentheses_groups(message)
    if len(groups) >= 2:
        return groups[0], groups[1]
    elif len(groups) == 1:
        return groups[0], ""
    return "", ""

def check_distribution_rule(game_number: int, message_text: str) -> Optional[tuple]:
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
        prediction_number = game_number + 5
        logger.info(f"🎯 #R DÉTECTÉ: {missing_suit} manquant dans groupes '{first_group}' et '{second_group}' → Prédiction #{prediction_number}")
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
# TRAITEMENT DES MESSAGES
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    global current_game_number, last_source_game_number, games_without_prediction
    
    current_game_number = game_number
    last_source_game_number = game_number
    games_without_prediction += 1
    
    await check_and_force_restart_if_needed()
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return
    
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    
    logger.info(f"📊 Jeu #{game_number}: {suits_in_first} dans '{first_group[:30]}...'")
    
    add_to_history(game_number, message_text, first_group, suits_in_first)
    
    if await check_prediction_result(game_number, first_group):
        return
    
    distribution_result = check_distribution_rule(game_number, message_text)
    if distribution_result:
        suit, pred_num = distribution_result
        if pred_num not in pending_predictions:
            await send_prediction(pred_num, suit, 'distribution')
            return
    
    if compteur2_active:
        update_compteur2(game_number, first_group)
        
        compteur2_preds = get_compteur2_ready_predictions(game_number)
        for suit, pred_num in compteur2_preds:
            if pred_num not in pending_predictions:
                await send_prediction(pred_num, suit, 'compteur2')

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
            logger.info(f"⏳ Message en cours d'édition (⏰), ignoré pour l'instant")
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
# RESET AUTOMATIQUE
# ============================================================================

async def auto_reset_system():
    global last_prediction_time
    
    while True:
        try:
            now = datetime.now()
            
            if now.hour == 1 and now.minute == 0:
                logger.info("🕐 Reset 1h00")
                await perform_full_reset("🕐 Reset automatique 1h00")
                await asyncio.sleep(60)
            
            if last_prediction_time:
                elapsed = now - last_prediction_time
                if elapsed > timedelta(hours=1) and pending_predictions:
                    logger.info(f"⏰ Reset inactivité ({elapsed.total_seconds()/3600:.1f}h)")
                    await perform_full_reset("⏰ Reset inactivité 1h")
            
            await process_prediction_queue()
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time, waiting_finalization, prediction_queue
    global games_without_prediction, last_prediction_number, last_prediction_sent_time
    global compteur2_trackers
    
    stats = len(pending_predictions)
    
    for tracker in compteur2_trackers.values():
        tracker.counter = 0
        tracker.last_increment_game = 0
    
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    last_prediction_time = None
    last_prediction_sent_time = None
    last_prediction_number = 0
    games_without_prediction = 0
    suit_block_until.clear()
    
    logger.info(f"🔄 {reason} - {stats} prédictions cleared, Compteur2 reset")
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"""🔄 **RESET SYSTÈME**

{reason}

✅ Compteurs Compteur2 remis à zéro
✅ {stats} prédictions cleared
✅ File d'attente vidée
✅ Nouvelle analyse

⏳BACCARAT AI 🤖⏳"""
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

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
                "📊 **COMPTEUR2 - STATUT**",
                f"Statut: {status_str}",
                f"🎯 Seuil B (prédiction): {compteur2_seuil_B}",
                f"🎮 Dernier jeu analysé: #{current_game_number}",
                f"📋 Prédictions actives: {len(pending_predictions)}",
                f"⏳ File d'attente: {len(prediction_queue)}",
                f"🚫 Sans prédiction: {games_without_prediction}/20",
                "",
                "📈 **Compteurs actuels:**"
            ]
            
            for suit in ALL_SUITS:
                tracker = compteur2_trackers.get(suit)
                if tracker:
                    progress = min(tracker.counter, compteur2_seuil_B)
                    bar_filled = '█' * progress
                    bar_empty = '░' * (compteur2_seuil_B - progress)
                    bar = f"[{bar_filled}{bar_empty}]"
                    
                    if tracker.counter >= compteur2_seuil_B:
                        status = "🔮 PRÊT"
                    elif tracker.counter > 0:
                        status = f"⏳ En cours ({tracker.counter}/{compteur2_seuil_B})"
                    else:
                        status = "✅ En attente"
                    
                    lines.append(f"{tracker.get_display_name()}: {bar} {status}")
            
            lines.extend([
                "",
                "💡 **Règle:**",
                f"• Compte les costumes manquants dans le 1er groupe",
                f"• Incrémente quand costume absent",
                f"• Reset à 0 quand costume présent",
                f"• Prédiction à N+2 quand compteur = {compteur2_seuil_B}",
                "",
                "**Usage:**",
                "`/compteur2 [2-10]` - Définir le seuil B",
                "`/compteur2 on` - Activer",
                "`/compteur2 off` - Désactiver",
                "`/compteur2 reset` - Reset les compteurs"
            ])
            
            await event.respond("\n".join(lines))
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            compteur2_active = False
            await event.respond("❌ **Compteur2 DÉSACTIVÉ**")
            logger.info("Admin désactive Compteur2")
            return
        
        if arg == 'on':
            compteur2_active = True
            await event.respond("✅ **Compteur2 ACTIVÉ**")
            logger.info("Admin active Compteur2")
            return
        
        if arg == 'reset':
            for tracker in compteur2_trackers.values():
                tracker.counter = 0
                tracker.last_increment_game = 0
            await event.respond("🔄 **Compteurs Compteur2 remis à zéro**")
            logger.info("Admin reset Compteur2")
            return
        
        try:
            b_val = int(arg)
            if not 2 <= b_val <= 10:
                await event.respond("❌ B doit être entre 2 et 10")
                return
            
            old_b = compteur2_seuil_B
            compteur2_seuil_B = b_val
            
            await event.respond(
                f"✅ **Seuil B modifié**\n\n"
                f"Ancien: {old_b}\n"
                f"Nouveau: **{compteur2_seuil_B}**\n\n"
                f"💡 Règle: {compteur2_seuil_B} manques consécutifs → prédiction N+2"
            )
            logger.info(f"Admin change seuil B: {old_b} → {b_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/compteur2 [2-10]`, `/compteur2 on/off/reset`")
            
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
        "📜 **HISTORIQUE DES 5 DERNIERS MESSAGES FINALISÉS**",
        "═══════════════════════════════════════",
        ""
    ]
    
    recent_messages = finalized_messages_history[:5]
    
    if not recent_messages:
        lines.append("❌ Aucun message dans l'historique")
    else:
        for i, msg in enumerate(recent_messages, 1):
            time_str = msg['timestamp'].strftime('%H:%M:%S')
            game_num = msg['game_number']
            group = msg['first_group']
            suits = ', '.join([SUIT_DISPLAY.get(s, s) for s in msg['suits_found']]) if msg['suits_found'] else 'Aucune'
            
            verif_indicator = ""
            if msg['predictions_verified']:
                verif_details = []
                for v in msg['predictions_verified']:
                    suit_display = SUIT_DISPLAY.get(v['suit'], v['suit'])
                    if v['rattrapage_level'] == 0:
                        verif_details.append(f"✅0️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 1:
                        verif_details.append(f"✅1️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 2:
                        verif_details.append(f"✅2️⃣ #{v['predicted_game']}{suit_display}")
                verif_indicator = "\n   🔍 Vérification: " + " | ".join(verif_details)
            
            lines.append(
                f"{i}. 🕐 `{time_str}` | **Jeu #{game_num}**\n"
                f"   📝 `{group}`\n"
                f"   🎨 Couleurs: {suits}{verif_indicator}"
            )
            lines.append("")
    
    lines.append("🔮 **PRÉDICTIONS RÉCENTES**")
    lines.append("───────────────────────────────────────")
    
    recent_predictions = prediction_history[:5]
    
    if not recent_predictions:
        lines.append("❌ Aucune prédiction dans l'historique")
    else:
        for pred in recent_predictions:
            pred_game = pred['predicted_game']
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            pred_type = pred.get('type', 'standard')
            
            type_emoji = {'distribution': '#️⃣', 'compteur2': '2️⃣'}.get(pred_type, '❓')
            
            if status == 'en_cours':
                status_str = "⏳ En cours..."
            elif status == 'gagne_r0':
                status_str = "✅0️⃣ GAGNÉ direct"
            elif status == 'gagne_r1':
                status_str = "✅1️⃣ GAGNÉ R1"
            elif status == 'gagne_r2':
                status_str = "✅2️⃣ GAGNÉ R2"
            elif status == 'perdu':
                status_str = "❌ PERDU"
            else:
                status_str = f"❓ {status}"
            
            lines.append(f"{type_emoji} **#{pred_game}** {suit} | {status_str}")
            lines.append(f"   🕐 Prédit à: {pred_time}")
            
            if pred['verified_by']:
                lines.append("   📋 Vérifié par:")
                for v in pred['verified_by']:
                    r_text = f"R{v['rattrapage_level']}" if v['rattrapage_level'] > 0 else "Direct"
                    lines.append(f"      • Jeu #{v['game_number']} ({r_text}): `{v['first_group']}`")
            else:
                verif_games = pred['verification_games']
                if status == 'en_cours':
                    pending_games = [g for g in verif_games if g > current_game_number]
                    checked_games = [g for g in verif_games if g <= current_game_number]
                    
                    if checked_games:
                        lines.append(f"   ✅ Déjà vérifié: {', '.join(['#' + str(g) for g in checked_games])}")
                    if pending_games:
                        lines.append(f"   ⏳ En attente: {', '.join(['#' + str(g) for g in pending_games])}")
            
            lines.append("")
    
    lines.append("═══════════════════════════════════════")
    lines.append("💡 **Légende:**")
    lines.append("• #️⃣ = Distribution (#R)")
    lines.append("• 2️⃣ = Compteur2")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    global compteur2_active, compteur2_seuil_B
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    compteur2_str = "✅ ON" if compteur2_active else "❌ OFF"
    
    lines = [
        "📊 **STATUT DU SYSTÈME**",
        "",
        f"Mode Compteur2: {compteur2_str} (seuil B={compteur2_seuil_B})",
        f"🎮 Dernier jeu: #{current_game_number}",
        f"📋 Prédictions actives: {len(pending_predictions)}",
        f"⏳ File d'attente: {len(prediction_queue)}",
        f"🚫 Sans prédiction: {games_without_prediction}/20",
        ""
    ]
    
    lines.append("📊 **COMPTEUR2**")
    lines.append("─────────────────────")
    for suit in ALL_SUITS:
        tracker = compteur2_trackers.get(suit)
        if tracker:
            progress = min(tracker.counter, compteur2_seuil_B)
            bar_filled = '█' * progress
            bar_empty = '░' * (compteur2_seuil_B - progress)
            bar = f"[{bar_filled}{bar_empty}]"
            
            if tracker.counter >= compteur2_seuil_B:
                status = "🔮 PRÊT"
            elif tracker.counter > 0:
                status = f"⏳ En cours ({tracker.counter}/{compteur2_seuil_B})"
            else:
                status = "✅ En attente"
            
            lines.append(f"{tracker.get_display_name()}: {bar} {status}")
    
    lines.append("")
    
    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            ar = pred.get('awaiting_rattrapage', 0)
            ptype = pred.get('type', 'standard')
            
            type_emoji = {'distribution': '#️⃣', 'compteur2': '2️⃣'}.get(ptype, '❓')
            
            if ar > 0:
                status_str = f"attente R{ar} (#{num + ar})"
            else:
                status_str = pred['status']
            
            lines.append(f"• #{num} {suit} {type_emoji}: {status_str}")
        lines.append("")
    
    if prediction_queue:
        lines.append("**📥 FILE D'ATTENTE:**")
        for p in prediction_queue[:5]:
            suit = SUIT_DISPLAY.get(p['suit'], p['suit'])
            type_emoji = {'distribution': '#️⃣', 'compteur2': '2️⃣'}.get(p['type'], '❓')
            lines.append(f"• {type_emoji} #{p['predict_at']} {suit}")
        if len(prediction_queue) > 5:
            lines.append(f"... et {len(prediction_queue) - 5} autres")
        lines.append("")
    
    lines.extend([
        "**Légende:**",
        "✅=Trouvé ❌=Manqué ⏳=Attente 🔮=Prédiction",
        "#️⃣=Distribution 2️⃣=Compteur2"
    ])
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    help_text = f"""📖 **BACCARAT AI - AIDE COMPLÈTE**

**🎮 Systèmes de prédiction:**

1️⃣ **Distribution (#R)**
• Message avec #R et finalisé
• Vérifie 1er ET 2ème groupe de parenthèses
• Si 1 costume manquant exactement → prédit #N+5

2️⃣ **Compteur2**
• Compte costumes manquants dans 1er groupe
• Incrémente quand absent, reset quand présent
• Seuil B atteint → prédit N+2

**📋 Règles de sécurité:**
• Jamais 2 prédictions simultanées
• Attente N-2 avant envoi
• Auto-reset si >20 numéros sans prédiction
• Reset complet au #1440

**🔧 Commandes Admin:**

`/status` - Voir tous les compteurs
`/compteur2 [B/on/off/reset]` - Gérer Compteur2
`/history` - Historique messages et prédictions
`/reset` - Reset manuel complet
`/help` - Cette aide

**💡 Détails:**
• ⏰ = Message en cours d'édition (ignoré)
• ✅/🔰 = Message finalisé (traité)
• Vérification: 3 numéros (prédit, +1, +2)

⏳BACCARAT AI 🤖⏳"""
    
    await event.respond(help_text)

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel")
    await event.respond("✅ Reset effectué!")

def setup_handlers():
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
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédition inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal prédiction: {e}")
        
        logger.info("🤖 Bot démarré avec succès")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return
        
        asyncio.create_task(auto_reset_system())
        logger.info("🔄 Auto-reset démarré")
        
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT AI 🤖 Running"))
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 Web server port {PORT}")
        logger.info(f"📊 Compteur2: {'Actif B=' + str(compteur2_seuil_B) if compteur2_active else 'Inactif'}")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
