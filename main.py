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

# Gestion des écarts entre prédictions
MIN_GAP_BETWEEN_PREDICTIONS = 2  # Écart minimum entre 2 prédictions
last_prediction_number_sent = 0  # Dernier numéro de prédiction envoyé

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
# GESTION DES PRÉDICTIONS - MESSAGES JOLIS
# ============================================================================

def format_prediction_message(game_number: int, suit: str, prediction_type: str, status: str = 'en_cours', rattrapage: int = 0) -> str:
    """Formate un message de prédiction joli."""
    
    suit_display = SUIT_DISPLAY.get(suit, suit)
    type_emoji = {'distribution': '🎯', 'compteur2': '📊'}.get(prediction_type, '🤖')
    type_name = {'distribution': 'DISTRIBUTION #R', 'compteur2': 'COMPTEUR2'}.get(prediction_type, 'PRÉDICTION')
    
    if status == 'en_cours':
        if rattrapage == 0:
            status_line = "📊 Statut: En cours ⏳"
            verif_line = f"🔍 Vérification: #{game_number} | #{game_number+1} | #{game_number+2}"
        else:
            status_line = f"📊 Statut: Rattrapage R{rattrapage} ⏳"
            verif_line = f"🔍 Vérification: En attente #{game_number}"
    elif status == 'gagne':
        if rattrapage == 0:
            status_line = "✅ Statut: GAGNÉ DIRECT 🎉"
        else:
            status_line = f"✅ Statut: GAGNÉ R{rattrapage} 🎉"
        verif_line = ""
    elif status == 'perdu':
        status_line = "❌ Statut: PERDU 😭"
        verif_line = ""
    else:
        status_line = f"📊 Statut: {status}"
        verif_line = ""
    
    header_emoji = "🎰" if status == 'en_cours' else "🏆" if 'gagne' in status else "💔"
    
    lines = [
        f"{header_emoji} **{type_name} #{game_number}**",
        "",
        f"🎯 **Couleur:** {suit_display}",
        status_line,
    ]
    
    if verif_line:
        lines.append(verif_line)
    
    if status == 'en_cours':
        lines.append("")
        lines.append("⏳ *En attente du résultat...*")
    
    lines.append("")
    lines.append("🤖 Baccarat AI")
    
    return "\n".join(lines)

async def send_prediction(game_number: int, suit: str, prediction_type: str = 'standard') -> Optional[int]:
    """Envoie une prédiction au canal configuré."""
    global last_prediction_time, last_prediction_number_sent
    
    try:
        # Vérifier si la couleur est bloquée
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        # Vérifier l'écart minimum
        if last_prediction_number_sent > 0:
            gap = game_number - last_prediction_number_sent
            if gap < MIN_GAP_BETWEEN_PREDICTIONS:
                logger.info(f"⏳ Écart insuffisant: {gap} < {MIN_GAP_BETWEEN_PREDICTIONS}. Attente...")
                return None
        
        if not PREDICTION_CHANNEL_ID:
            logger.error("❌ PREDICTION_CHANNEL_ID non configuré!")
            return None
        
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error(f"❌ Impossible d'accéder au canal {PREDICTION_CHANNEL_ID}")
            return None
        
        # Créer le message joli
        msg = format_prediction_message(game_number, suit, prediction_type, 'en_cours', 0)
        
        try:
            sent = await client.send_message(prediction_entity, msg, parse_mode='markdown')
            last_prediction_time = datetime.now()
            last_prediction_number_sent = game_number
            
            # Stockage de la prédiction avec infos de vérification
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent.id,
                'status': 'en_cours',
                'type': prediction_type,
                'sent_time': datetime.now(),
                'verification_games': [game_number, game_number + 1, game_number + 2],
                'verified_games': [],  # Jeux déjà vérifiés
                'found_at': None,  # Où le costume a été trouvé
                'rattrapage': 0
            }
            
            # Historique
            add_prediction_to_history(game_number, suit, [game_number, game_number + 1, game_number + 2], prediction_type)
            
            logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} ({prediction_type})")
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
    """Met à jour le statut d'une prédiction avec message joli."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    pred_type = pred.get('type', 'standard')
    
    # Créer le message mis à jour
    new_msg = format_prediction_message(game_number, suit, pred_type, status, rattrapage)
    
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

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    """
    Vérifie dynamiquement si une prédiction est gagnante.
    Met à jour le message à chaque vérification.
    """
    suits_in_result = get_suits_in_group(first_group)
    
    # Vérifier si ce numéro correspond à une prédiction en cours
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred['status'] != 'en_cours':
            return False
            
        target_suit = pred['suit']
        pred_type = pred.get('type', 'standard')
        
        # Vérifier si déjà vérifié ce jeu
        if game_number in pred['verified_games']:
            return False
        
        pred['verified_games'].append(game_number)
        
        logger.info(f"🔍 Vérification #{game_number}: {target_suit} dans {suits_in_result}?")
        
        if target_suit in suits_in_result:
            # GAGNÉ DIRECT (R0)
            await update_prediction_message(game_number, 'gagne', 0)
            update_prediction_in_history(game_number, target_suit, game_number, first_group, 0, 'gagne_r0')
            return True
        else:
            # Pas trouvé, passer au suivant
            pred['rattrapage'] = 1
            logger.info(f"❌ #{game_number} non trouvé, attente #{game_number + 1}")
            # Mettre à jour le message pour montrer la progression
            await update_prediction_progress(game_number, 1)
            return False
    
    # Vérifier les rattrapages (R1, R2)
    for original_game, pred in list(pending_predictions.items()):
        if pred['status'] != 'en_cours':
            continue
            
        target_suit = pred['suit']
        rattrapage = pred.get('rattrapage', 0)
        
        # Vérifier si ce numéro correspond au rattrapage attendu
        expected_game = original_game + rattrapage
        
        if game_number == expected_game and rattrapage > 0:
            # Vérifier si déjà vérifié
            if game_number in pred['verified_games']:
                return False
            
            pred['verified_games'].append(game_number)
            
            logger.info(f"🔍 Vérification R{rattrapage} #{game_number}: {target_suit} dans {suits_in_result}?")
            
            if target_suit in suits_in_result:
                # GAGNÉ R1 ou R2
                await update_prediction_message(original_game, 'gagne', rattrapage)
                update_prediction_in_history(original_game, target_suit, game_number, first_group, rattrapage, f'gagne_r{rattrapage}')
                return True
            else:
                # Pas trouvé, passer au suivant ou perdre
                if rattrapage < 2:
                    pred['rattrapage'] = rattrapage + 1
                    logger.info(f"❌ R{rattrapage} échoué, attente #{original_game + rattrapage + 1}")
                    # Mettre à jour le message pour montrer la progression
                    await update_prediction_progress(original_game, rattrapage + 1)
                    return False
                else:
                    # R2 échoué = PERDU
                    logger.info(f"❌ R2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, 'perdu', 2)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
                    return False
    
    return False

async def update_prediction_progress(game_number: int, current_rattrapage: int):
    """Met à jour l'affichage de la progression de la vérification."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    pred_type = pred.get('type', 'standard')
    suit_display = SUIT_DISPLAY.get(suit, suit)
    type_name = {'distribution': 'DISTRIBUTION #R', 'compteur2': 'COMPTEUR2'}.get(pred_type, 'PRÉDICTION')
    
    # Construire les lignes de vérification
    verif_lines = []
    original = game_number
    for i in range(3):
        check_num = original + i
        if i < current_rattrapage:
            status = "❌"
        elif i == current_rattrapage:
            status = "⏳"
        else:
            status = "⏸️"
        verif_lines.append(f"{status} #{check_num}")
    
    msg = f"""🎰 **{type_name} #{game_number}**

🎯 **Couleur:** {suit_display}
📊 Statut: En cours ⏳
🔍 Vérification: {' | '.join(verif_lines)}

⏳ *En attente du résultat...*

🤖 Baccarat AI"""
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity:
            await client.edit_message(prediction_entity, msg_id, msg, parse_mode='markdown')
    except Exception as e:
        logger.error(f"❌ Erreur update progress: {e}")

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
        logger.info(f"🎯 #R DÉTECTÉ: {missing_suit} manquant → Prédiction #{prediction_number}")
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
    global current_game_number, last_source_game_number
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    # Vérification auto-reset
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
    
    # Vérification des prédictions existantes (dynamique)
    await check_prediction_result(game_number, first_group)
    
    # NOUVEAU: Vérifier s'il y a des prédictions en cours avant d'en créer de nouvelles
    if pending_predictions:
        logger.info(f"⏳ Prédiction(s) en cours, pas de nouvelle prédiction pour l'instant")
        return
    
    # Distribution #R
    distribution_result = check_distribution_rule(game_number, message_text)
    if distribution_result:
        suit, pred_num = distribution_result
        await send_prediction(pred_num, suit, 'distribution')
        return
    
    # Compteur2
    if compteur2_active:
        update_compteur2(game_number, first_group)
        
        compteur2_preds = get_compteur2_ready_predictions(game_number)
        for suit, pred_num in compteur2_preds:
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
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time, waiting_finalization
    global last_prediction_number_sent, compteur2_trackers
    
    stats = len(pending_predictions)
    
    for tracker in compteur2_trackers.values():
        tracker.counter = 0
        tracker.last_increment_game = 0
    
    pending_predictions.clear()
    waiting_finalization.clear()
    last_prediction_time = None
    last_prediction_number_sent = 0
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
✅ Nouvelle analyse

🤖 Baccarat AI"""
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_gap(event):
    """Commande /gap - Configure l'écart minimum entre prédictions."""
    global MIN_GAP_BETWEEN_PREDICTIONS
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            # Afficher le statut actuel
            await event.respond(
                f"📏 **CONFIGURATION DES ÉCARTS**\n\n"
                f"Écart minimum actuel: **{MIN_GAP_BETWEEN_PREDICTIONS}** numéros\n\n"
                f"💡 Règle: Une prédiction ne peut être lancée que si l'écart "
                f"avec la dernière prédiction est d'au moins {MIN_GAP_BETWEEN_PREDICTIONS} numéros.\n\n"
                f"**Usage:**\n"
                f"`/gap [2-10]` - Définir l'écart minimum\n"
                f"Exemple: `/gap 3` pour 3 numéros d'écart minimum"
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
                f"Ancien: {old_gap} numéros\n"
                f"Nouveau: **{gap_val}** numéros\n\n"
                f"💡 Les prochaines prédictions respecteront cet écart minimum."
            )
            logger.info(f"Admin change écart: {old_gap} → {gap_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/gap [2-10]`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_gap: {e}")
        await event.respond(f"❌ Erreur: {e}")

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
                f"📏 Écart minimum: {MIN_GAP_BETWEEN_PREDICTIONS} numéros",
                f"🎮 Dernier jeu: #{current_game_number}",
                f"📋 Prédictions actives: {len(pending_predictions)}",
                f"🎯 Dernière prédiction: #{last_prediction_number_sent if last_prediction_number_sent else 'Aucune'}",
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
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]
    
    recent_predictions = prediction_history[:10]
    
    if not recent_predictions:
        lines.append("❌ Aucune prédiction dans l'historique")
    else:
        for i, pred in enumerate(recent_predictions, 1):
            pred_game = pred['predicted_game']
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            pred_type = pred.get('type', 'standard')
            
            type_emoji = {'distribution': '🎯', 'compteur2': '📊'}.get(pred_type, '❓')
            
            if status == 'en_cours':
                status_str = "⏳ En cours..."
                result_emoji = "🎰"
            elif status == 'gagne_r0':
                status_str = "✅ GAGNÉ DIRECT"
                result_emoji = "🏆"
            elif status == 'gagne_r1':
                status_str = "✅ GAGNÉ R1"
                result_emoji = "🏆"
            elif status == 'gagne_r2':
                status_str = "✅ GAGNÉ R2"
                result_emoji = "🏆"
            elif status == 'perdu':
                status_str = "❌ PERDU"
                result_emoji = "💔"
            else:
                status_str = f"❓ {status}"
                result_emoji = "❓"
            
            lines.append(f"{i}. {result_emoji} **#{pred_game}** {suit} | {type_emoji} {status_str}")
            lines.append(f"   🕐 {pred_time}")
            
            if pred.get('verified_by_game'):
                lines.append(f"   ✅ Vérifié au jeu #{pred['verified_by_game']}")
            
            lines.append("")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    global compteur2_active, compteur2_seuil_B
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    compteur2_str = "✅ ON" if compteur2_active else "❌ OFF"
    
    # Info prédictions actives
    active_info = []
    for num, pred in sorted(pending_predictions.items()):
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        rattrapage = pred.get('rattrapage', 0)
        verified = len(pred.get('verified_games', []))
        
        if rattrapage == 0 and verified == 0:
            status = "⏳ Attente vérification"
        elif rattrapage > 0:
            status = f"⏳ Rattrapage R{rattrapage}"
        else:
            status = f" {verified}/3 vérifiés"
        
        active_info.append(f"• #{num} {suit}: {status}")
    
    lines = [
        "📊 **STATUT DU SYSTÈME**",
        "",
        f"📏 Écart minimum: **{MIN_GAP_BETWEEN_PREDICTIONS}** numéros",
        f"📊 Compteur2: {compteur2_str} (seuil B={compteur2_seuil_B})",
        f"🎮 Dernier jeu: #{current_game_number}",
        f"🎯 Dernière prédiction: #{last_prediction_number_sent if last_prediction_number_sent else 'Aucune'}",
        f"📋 Prédictions actives: {len(pending_predictions)}",
        ""
    ]
    
    if active_info:
        lines.append("**🔮 PRÉDICTIONS EN COURS:**")
        lines.extend(active_info)
        lines.append("")
    
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
    lines.extend([
        "**Légende:**",
        "✅=Trouvé ❌=Manqué ⏳=Attente 🔮=Prêt",
        "🎯=Distribution #R 📊=Compteur2"
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
• Écart minimum: **{MIN_GAP_BETWEEN_PREDICTIONS}** numéros entre prédictions
• Vérification dynamique sur 3 numéros (prédit, +1, +2)
• Auto-reset si blocage
• Reset complet au #1440

**🔧 Commandes Admin:**

`/status` - Voir tous les compteurs
`/compteur2 [B/on/off/reset]` - Gérer Compteur2
`/gap [2-10]` - Configurer l'écart minimum
`/history` - Historique des prédictions
`/reset` - Reset manuel complet
`/help` - Cette aide

**💡 Détails:**
• ⏰ = Message en cours d'édition (ignoré)
• ✅/🔰 = Message finalisé (traité)
• Messages de prédiction mis à jour en temps réel

🤖 Baccarat AI"""
    
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
    client.add_event_handler(cmd_gap, events.NewMessage(pattern=r'^/gap'))
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
        logger.info(f"📊 Écart minimum: {MIN_GAP_BETWEEN_PREDICTIONS} numéros")
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
