import os
import asyncio
import re
import logging
import sys
import random
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

# File d'attente de prédictions (plusieurs prédictions possibles)
prediction_queue: List[Dict] = []  # File ordonnée des prédictions en attente
PREDICTION_SEND_AHEAD = 2  # Envoyer la prédiction quand canal source est à N-2

# Valeur à ajouter pour la règle de distribution (configurable par admin)
DISTRIBUTION_PLUS_VALUE = 5  # Valeur par défaut: +5

# Canaux secondaires pour redirection
DISTRIBUTION_CHANNEL_ID = None  # Canal spécifique pour Distribution #R
COMPTEUR2_CHANNEL_ID = None     # Canal spécifique pour Compteur2

# ============================================================================
# SYSTÈME DE PAUSE PAR CYCLE (CORRIGÉ)
# ============================================================================

# État de la pause
pause_active = False
pause_counter = 0  # Compteur de prédictions (1-4)
pause_cycle_index = 0  # Index dans le cycle (0, 1, 2...)
pause_message_id = None  # ID du message de pause à éditer
pause_end_time = None  # Heure de fin de pause
pause_task = None  # Tâche de mise à jour du message

# Configuration pause
PAUSE_CYCLE = [3, 5, 4]  # Durées en minutes par défaut
PREDICTIONS_BEFORE_PAUSE = 4  # Nombre de prédictions avant pause

# Expressions de reprise (20 expressions par défaut)
RESUME_EXPRESSIONS = [
    "🎉 Bingo ! Les prédictions reprennent ! Bot créé par Sossou Kouamé",
    "🚀 C'est reparti mon kiki ! Sossou Kouamé vous présente la suite",
    "🎰 La pause est finie, le jeu continue ! By Sossou Kouamé",
    "🔥 De retour en action ! Bot by Sossou Kouamé",
    "⚡ Énergie rechargée à 100% ! Sossou Kouamé au rapport",
    "🎯 Viser juste, viser fort ! Les prédictions reprennent - Sossou Kouamé",
    "🌟 Le spectacle continue ! Bot Telegram de Sossou Kouamé",
    "💫 Et c'est reparti pour un tour ! Sossou Kouamé vous souhaite bonne chance",
    "🎊 Fin de la sieste, début des gains ! By Sossou Kouamé",
    "⏰ L'heure de prédire a sonné ! Sossou Kouamé est de retour",
    "🍀 La chance sourit aux audacieux ! Reprise par Sossou Kouamé",
    "🎵 Tadam ! Les prédictions sont de retour - Sossou Kouamé",
    "🌈 Arc-en-ciel de victoires en vue ! Bot by Sossou Kouamé",
    "🎖️ Médaille de la patience décernée ! Reprise Sossou Kouamé",
    "🚀 Décollage immédiat ! Sossou Kouamé aux commandes",
    "🎩 Abracadabra ! Les prédictions réapparaissent - By Sossou Kouamé",
    "🔮 La boule de cristal est de nouveau claire ! Sossou Kouamé",
    "⚔️ À l'attaque ! Le bot de Sossou Kouamé reprend du service",
    "🎰 Jackpot en approche ! Sossou Kouamé vous y mène",
    "🌟 Étoile filante de prédictions ! Sossou Kouamé fait le show"
]

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
# SYSTÈME DE PAUSE - GESTION (CORRIGÉ)
# ============================================================================

def format_pause_message(duration_min: int, remaining_seconds: int) -> str:
    """Formate le message de pause avec temps dynamique."""
    # CORRECTION: Si temps écoulé ou négatif, afficher message de fin
    if remaining_seconds <= 0:
        return f"""⏸️ PAUSE TERMINÉE

✅ Fin de la pause
🔄 Préparation de la reprise...

🤖 Baccarat AI"""
    
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    
    return f"""⏸️ PAUSE ACTIVE

🕐 Durée: {duration_min} minutes
⏳ Temps restant: {minutes}:{seconds:02d}

🤖 Baccarat AI"""

def format_resume_message() -> str:
    """Choisit une expression aléatoire de reprise."""
    return random.choice(RESUME_EXPRESSIONS)

async def update_pause_message(duration_min: int, remaining_seconds: int):
    """Met à jour le message de pause en temps réel."""
    global pause_message_id, pause_active, pause_end_time
    
    if not pause_active or not pause_message_id:
        return
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            return
        
        # CORRECTION: Ne jamais afficher de temps négatif
        display_seconds = max(0, remaining_seconds)
        
        msg = format_pause_message(duration_min, display_seconds)
        
        await client.edit_message(prediction_entity, pause_message_id, msg, parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"❌ Erreur mise à jour pause: {e}")

async def pause_countdown_task(duration_min: int):
    """Tâche qui met à jour le message de pause chaque seconde."""
    global pause_active, pause_message_id, pause_end_time
    
    total_seconds = duration_min * 60
    
    # CORRECTION: Boucle de total_seconds down to 1 (pas 0)
    # Et vérifier si la pause est toujours active
    for i in range(total_seconds, 0, -1):
        if not pause_active:
            logger.info("⏸️ Pause annulée manuellement")
            return
        
        await update_pause_message(duration_min, i)
        await asyncio.sleep(1)
    
    # CORRECTION: Quand on sort de la boucle (i=0), temps écoulé
    if pause_active:
        logger.info("⏸️ Temps écoulé, fin de pause automatique")
        await end_pause()

async def start_pause():
    """Démarre une pause."""
    global pause_active, pause_counter, pause_cycle_index, pause_message_id, pause_end_time, pause_task
    
    if pause_active:
        logger.warning("⏸️ Pause déjà active")
        return
    
    # Déterminer la durée selon le cycle
    duration = PAUSE_CYCLE[pause_cycle_index % len(PAUSE_CYCLE)]
    
    pause_active = True
    pause_end_time = datetime.now() + timedelta(minutes=duration)
    
    # Envoyer message initial
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity:
            msg = format_pause_message(duration, duration * 60)
            sent = await client.send_message(prediction_entity, msg, parse_mode='markdown')
            pause_message_id = sent.id
            
            # Démarrer la tâche de mise à jour
            pause_task = asyncio.create_task(pause_countdown_task(duration))
            
            logger.info(f"⏸️ PAUSE DÉMARRÉE: {duration} min (cycle index: {pause_cycle_index})")
    except Exception as e:
        logger.error(f"❌ Erreur démarrage pause: {e}")
        pause_active = False

async def end_pause():
    """Termine la pause et envoie message de reprise."""
    global pause_active, pause_counter, pause_cycle_index, pause_message_id, pause_end_time, pause_task
    
    if not pause_active:
        return
    
    pause_active = False
    pause_counter = 0  # Reset compteur
    pause_cycle_index += 1  # Passer au cycle suivant
    pause_end_time = None
    
    # Annuler la tâche si encore active
    if pause_task and not pause_task.done():
        pause_task.cancel()
        try:
            await pause_task
        except asyncio.CancelledError:
            pass
    
    # Envoyer message de reprise
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and pause_message_id:
            resume_msg = format_resume_message()
            
            # Éditer le dernier message de pause avec message de reprise
            await client.edit_message(
                prediction_entity, 
                pause_message_id, 
                f"✅ **PAUSE TERMINÉE**\n\n{resume_msg}", 
                parse_mode='markdown'
            )
            
            logger.info(f"▶️ PAUSE TERMINÉE - Reprise avec: {resume_msg[:50]}...")
            
            # Reset message_id pour prochaine pause
            pause_message_id = None
            
            # CORRECTION: Traiter immédiatement la file d'attente si des prédictions attendent
            if prediction_queue:
                logger.info(f"📤 {len(prediction_queue)} prédictions en attente, traitement...")
                await process_prediction_queue(current_game_number)
                
    except Exception as e:
        logger.error(f"❌ Erreur fin pause: {e}")

def increment_pause_counter():
    """Incrémente le compteur de pause et vérifie si pause nécessaire."""
    global pause_counter, pause_active
    
    if pause_active:
        return False  # Déjà en pause
    
    pause_counter += 1
    logger.info(f"⏸️ Compteur pause: {pause_counter}/{PREDICTIONS_BEFORE_PAUSE}")
    
    if pause_counter >= PREDICTIONS_BEFORE_PAUSE:
        logger.info("⏸️ Seuil atteint, pause programmée après vérification")
        return True  # Indique qu'il faut faire pause après vérification
    
    return False

async def check_and_trigger_pause(game_number: int):
    """Vérifie si une prédiction terminée doit déclencher la pause."""
    global pause_counter, pause_active
    
    if pause_active:
        return
    
    # Vérifier si c'est la 4ème prédiction qui vient de finir
    if pause_counter >= PREDICTIONS_BEFORE_PAUSE:
        # Vérifier que c'est bien la dernière prédiction envoyée qui se termine
        if game_number in pending_predictions:
            return  # Encore en cours
        
        # La 4ème prédiction est terminée, lancer la pause
        await start_pause()

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

# ============================================================================
# ENVOI MULTI-CANAUX
# ============================================================================

async def send_prediction_to_channel(channel_id: int, game_number: int, suit: str, 
                                    prediction_type: str, is_secondary: bool = False) -> Optional[int]:
    try:
        if not is_secondary and suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        if not channel_id:
            return None
        
        channel_entity = await resolve_channel(channel_id)
        if not channel_entity:
            logger.error(f"❌ Canal {channel_id} inaccessible")
            return None
        
        msg = format_prediction_message(game_number, suit, 'en_cours', game_number, [])
        
        sent = await client.send_message(channel_entity, msg, parse_mode='markdown')
        logger.info(f"✅ Envoyé à {'canal secondaire' if is_secondary else 'canal principal'} {channel_id}: #{game_number} {suit}")
        return sent.id
        
    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas de permission dans {channel_id}")
        return None
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni de {channel_id}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi à {channel_id}: {e}")
        return None

async def send_prediction_multi_channel(game_number: int, suit: str, prediction_type: str = 'standard') -> bool:
    """Envoie la prédiction au canal principal ET aux canaux secondaires selon le type."""
    global last_prediction_time, last_prediction_number_sent, DISTRIBUTION_CHANNEL_ID, COMPTEUR2_CHANNEL_ID
    
    success = False
    
    # 1. TOUJOURS envoyer au canal principal
    if PREDICTION_CHANNEL_ID:
        msg_id = await send_prediction_to_channel(
            PREDICTION_CHANNEL_ID, game_number, suit, prediction_type, is_secondary=False
        )
        
        if msg_id:
            last_prediction_time = datetime.now()
            last_prediction_number_sent = game_number
            
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': msg_id,
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
            success = True
    
    # 2. Envoyer au canal secondaire Distribution si défini et type = distribution
    if prediction_type == 'distribution' and DISTRIBUTION_CHANNEL_ID:
        await send_prediction_to_channel(
            DISTRIBUTION_CHANNEL_ID, game_number, suit, prediction_type, is_secondary=True
        )
    
    # 3. Envoyer au canal secondaire Compteur2 si défini et type = compteur2
    elif prediction_type == 'compteur2' and COMPTEUR2_CHANNEL_ID:
        await send_prediction_to_channel(
            COMPTEUR2_CHANNEL_ID, game_number, suit, prediction_type, is_secondary=True
        )
    
    # 4. Incrémenter compteur de pause (si succès et pas déjà en pause)
    if success and not pause_active:
        need_pause = increment_pause_counter()
        if need_pause:
            logger.info(f"⏸️ La 4ème prédiction (#{game_number}) va déclencher la pause après vérification")
    
    return success

async def update_prediction_message(game_number: int, status: str, rattrapage: int = 0):
    """Met à jour le statut d'une prédiction (uniquement canal principal)."""
    if game_number not in pending_predictions:
        # Vérifier si c'était la 4ème prédiction qui vient de finir
        await check_and_trigger_pause(game_number)
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    
    new_msg = format_prediction_message(game_number, suit, status, rattrapage=rattrapage)
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal principal non accessible pour mise à jour")
            return
            
        await client.edit_message(prediction_entity, msg_id, new_msg, parse_mode='markdown')
        pred['status'] = status
        
        if 'gagne' in status:
            logger.info(f"✅ Gagné: #{game_number} (R{rattrapage})")
        else:
            logger.info(f"❌ Perdu: #{game_number}")
            block_suit(suit, 5)
        
        del pending_predictions[game_number]
        
        # Vérifier si pause nécessaire après cette fin de prédiction
        await check_and_trigger_pause(game_number)
        
    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

async def update_prediction_progress(game_number: int, current_check: int):
    """Met à jour l'affichage de la progression (canal principal uniquement)."""
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
# GESTION INTELLIGENTE DE LA FILE D'ATTENTE (avec pause)
# ============================================================================

def can_accept_prediction(pred_number: int) -> bool:
    global prediction_queue, pending_predictions, last_prediction_number_sent, MIN_GAP_BETWEEN_PREDICTIONS, pause_active
    
    # Si en pause, rejeter toutes les nouvelles prédictions
    if pause_active:
        logger.info(f"⛔ En pause, prédiction #{pred_number} rejetée")
        return False
    
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
    global prediction_queue, pause_active
    
    # Si en pause, ne pas ajouter à la file
    if pause_active:
        logger.info(f"⏸️ En pause, #{game_number} non ajouté")
        return False
    
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
    prediction_queue.sort(key=lambda x: x['game_number'])
    
    logger.info(f"📥 Prédiction #{game_number} ({suit}) ajoutée à la file. Total: {len(prediction_queue)}")
    return True

async def process_prediction_queue(current_game: int):
    global prediction_queue, pending_predictions, pause_active
    
    # Si en pause, ne pas traiter la file
    if pause_active:
        return
    
    to_remove = []
    
    for pred in prediction_queue:
        pred_number = pred['game_number']
        suit = pred['suit']
        pred_type = pred['type']
        
        # Condition d'envoi: canal à N-2 ou plus proche
        if current_game >= pred_number - PREDICTION_SEND_AHEAD:
            # Vérifier qu'on n'a pas déjà une prédiction en cours qui bloque
            if pending_predictions:
                can_send = True
                for active_num in pending_predictions.keys():
                    gap = abs(pred_number - active_num)
                    if gap < MIN_GAP_BETWEEN_PREDICTIONS:
                        logger.info(f"⏳ Prédiction #{pred_number} attend - conflit avec active #{active_num}")
                        can_send = False
                        break
                
                if not can_send:
                    continue
            
            # Envoyer la prédiction (multi-canaux)
            logger.info(f"📤 Envoi depuis file: #{pred_number} (canal à #{current_game})")
            success = await send_prediction_multi_channel(pred_number, suit, pred_type)
            
            if success:
                to_remove.append(pred)
            else:
                logger.warning(f"⚠️ Échec envoi #{pred_number}, conservation dans file")
    
    # Nettoyer la file
    for pred in to_remove:
        prediction_queue.remove(pred)
        logger.info(f"✅ #{pred['game_number']} retiré de la file. Restant: {len(prediction_queue)}")

# ============================================================================
# TRAITEMENT DES MESSAGES (CORRIGÉ)
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    global current_game_number, last_source_game_number, pause_active, pause_end_time
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    # CORRECTION: Vérifier si la pause devrait être terminée avant tout traitement
    if pause_active and pause_end_time:
        remaining = (pause_end_time - datetime.now()).total_seconds()
        if remaining <= 0:
            logger.info("⏸️ Pause expirée détectée, reprise automatique")
            await end_pause()
    
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
    
    # Traiter la file d'attente (si pas en pause, ou si pause vient de finir)
    await process_prediction_queue(game_number)
    
    # Si en pause, ne pas détecter de nouvelles prédictions
    if pause_active:
        logger.info(f"⏸️ En pause, pas de nouvelle détection")
        return
    
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
# RESET ET NOTIFICATIONS (CORRIGÉ)
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
    """Mode veille avec vérification de pause bloquée."""
    global pause_active, pause_end_time
    
    while True:
        try:
            await asyncio.sleep(60)  # Vérifier chaque minute
            
            # CORRECTION: Vérifier si la pause est bloquée (temps dépassé mais toujours active)
            if pause_active and pause_end_time:
                remaining = (pause_end_time - datetime.now()).total_seconds()
                if remaining <= -30:  # 30 secondes de marge après expiration
                    logger.warning("🚨 Pause bloquée détectée (temps dépassé), auto-correction...")
                    await end_pause()
                    
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time, waiting_finalization
    global last_prediction_number_sent, compteur2_trackers, prediction_queue
    global pause_active, pause_counter, pause_cycle_index, pause_message_id, pause_end_time, pause_task
    
    stats = len(pending_predictions)
    queue_stats = len(prediction_queue)
    
    # Annuler pause si active
    if pause_active:
        pause_active = False
        if pause_task and not pause_task.done():
            pause_task.cancel()
            try:
                await pause_task
            except asyncio.CancelledError:
                pass
        pause_message_id = None
        pause_end_time = None
    
    for tracker in compteur2_trackers.values():
        tracker.counter = 0
        tracker.last_increment_game = 0
    
    pending_predictions.clear()
    waiting_finalization.clear()
    prediction_queue.clear()
    last_prediction_time = None
    last_prediction_number_sent = 0
    suit_block_until.clear()
    
    # Reset compteur pause aussi
    pause_counter = 0
    pause_cycle_index = 0
    
    logger.info(f"🔄 {reason} - {stats} actives cleared, {queue_stats} file cleared, Compteur2 reset, Pause reset")
    
    await notify_admin_reset(reason, stats, queue_stats)

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_pause(event):
    """Commande /pause - Gérer le système de pause."""
    global pause_active, pause_counter, pause_cycle_index, PAUSE_CYCLE, PREDICTIONS_BEFORE_PAUSE
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        # Afficher statut
        if len(parts) == 1:
            status_pause = "🟢 ACTIVE" if pause_active else "🔴 INACTIVE"
            
            # Calculer temps restant si actif
            time_info = ""
            if pause_active and pause_end_time:
                remaining = int((pause_end_time - datetime.now()).total_seconds())
                if remaining > 0:
                    mins = remaining // 60
                    secs = remaining % 60
                    time_info = f"\n⏳ Temps restant: {mins}:{secs:02d}"
                else:
                    time_info = "\n⏳ Temps écoulé (devrait se terminer...)"
            
            current_duration = PAUSE_CYCLE[pause_cycle_index % len(PAUSE_CYCLE)] if not pause_active else "En cours"
            
            await event.respond(
                f"⏸️ **SYSTÈME DE PAUSE**\n\n"
                f"Statut: {status_pause}{time_info}\n\n"
                f"📊 Configuration:\n"
                f"• Cycle: {PAUSE_CYCLE} minutes\n"
                f"• Prochaine pause: {current_duration} min\n"
                f"• Prédictions avant pause: {PREDICTIONS_BEFORE_PAUSE}\n"
                f"• Compteur actuel: {pause_counter}/{PREDICTIONS_BEFORE_PAUSE}\n"
                f"• Cycle actuel: #{pause_cycle_index + 1}\n\n"
                f"**Usage:**\n"
                f"`/pause on` - Activer manuellement\n"
                f"`/pause off` - Désactiver manuellement\n"
                f"`/pausecycle 3,5,4` - Modifier le cycle\n"
                f"`/pauseadd [texte]` - Ajouter expression reprise"
            )
            return
        
        arg = parts[1].lower()
        
        if arg == 'on':
            if pause_active:
                await event.respond("⏸️ Pause déjà active")
                return
            await start_pause()
            await event.respond("✅ **Pause activée manuellement**")
            
        elif arg == 'off':
            if not pause_active:
                await event.respond("▶️ Pas de pause active")
                return
            await end_pause()
            await event.respond("✅ **Pause désactivée manuellement**")
            
        else:
            await event.respond("❌ Usage: `/pause` ou `/pause on/off`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_pause: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_pausecycle(event):
    """Commande /pausecycle - Modifier le cycle de pause."""
    global PAUSE_CYCLE
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        # Afficher cycle actuel
        if len(parts) == 1:
            cycle_text = f"🔄 **CYCLE DE PAUSE**\n\nCycle actuel: **{PAUSE_CYCLE}** minutes\n\nOrdre des pauses:\n"
            
            for i, duration in enumerate(PAUSE_CYCLE, 1):
                cycle_text += f"{i}. Pause #{i}: {duration} minutes\n"
            
            cycle_text += f"\n**Usage:** `/pausecycle 3,5,4,6` (durées en minutes, séparées par des virgules)"
            await event.respond(cycle_text)
            return
        
        # Modifier cycle
        arg = parts[1]
        try:
            # Parser "3,5,4" ou "3, 5, 4"
            new_cycle = [int(x.strip()) for x in arg.split(',')]
            
            if len(new_cycle) < 1:
                await event.respond("❌ Minimum 1 durée requise")
                return
            
            if any(d <= 0 or d > 60 for d in new_cycle):
                await event.respond("❌ Les durées doivent être entre 1 et 60 minutes")
                return
            
            old_cycle = PAUSE_CYCLE
            PAUSE_CYCLE = new_cycle
            
            await event.respond(
                f"✅ **Cycle modifié**\n\n"
                f"Ancien: {old_cycle}\n"
                f"Nouveau: **{PAUSE_CYCLE}**"
            )
            logger.info(f"Admin change cycle pause: {old_cycle} → {PAUSE_CYCLE}")
            
        except ValueError:
            await event.respond("❌ Format invalide. Usage: `/pausecycle 3,5,4`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_pausecycle: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_pauseadd(event):
    """Commande /pauseadd - Ajouter une expression de reprise."""
    global RESUME_EXPRESSIONS
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split(' ', 1)
        
        if len(parts) < 2:
            examples = random.sample(RESUME_EXPRESSIONS, min(5, len(RESUME_EXPRESSIONS)))
            examples_text = "\n".join([f"{i+1}. {ex[:50]}..." for i, ex in enumerate(examples)])
            
            await event.respond(
                f"📝 **EXPRESSIONS DE REPRISE**\n\n"
                f"Nombre actuel: **{len(RESUME_EXPRESSIONS)}** expressions\n\n"
                f"Exemples:\n{examples_text}\n\n"
                f"**Usage:** `/pauseadd Votre expression ici - Sossou Kouamé`"
            )
            return
        
        new_expr = parts[1].strip()
        
        if len(new_expr) < 10:
            await event.respond("❌ Expression trop courte (min 10 caractères)")
            return
        
        if len(new_expr) > 200:
            await event.respond("❌ Expression trop longue (max 200 caractères)")
            return
        
        RESUME_EXPRESSIONS.append(new_expr)
        
        await event.respond(
            f"✅ **Expression ajoutée**\n\n"
            f"Total: {len(RESUME_EXPRESSIONS)} expressions\n"
            f"Nouvelle: _{new_expr[:60]}..._"
        )
        logger.info(f"Admin ajoute expression pause: {new_expr[:50]}...")
        
    except Exception as e:
        logger.error(f"Erreur cmd_pauseadd: {e}")
        await event.respond(f"❌ Erreur: {e}")

# ============================================================================
# AUTRES COMMANDES (inchangées)
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
            
            await event.respond(f"✅ **Valeur modifiée: +{old_val} → +{plus_val}**")
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
            
            await event.respond(f"✅ **Écart modifié: {old_gap} → {gap_val}**")
            logger.info(f"Admin change écart: {old_gap} → {gap_val}")
            
        except ValueError:
            await event.respond("❌ Usage: `/gap [2-10]`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_gap: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_canal_distribution(event):
    """Commande /canaldistribution - Gérer le canal de redirection Distribution #R"""
    global DISTRIBUTION_CHANNEL_ID
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            if DISTRIBUTION_CHANNEL_ID:
                await event.respond(
                    f"🎯 **CANAL DISTRIBUTION #R**\n\n"
                    f"✅ Actif: `{DISTRIBUTION_CHANNEL_ID}`\n\n"
                    f"**Usage:** `/canaldistribution [ID]` ou `/canaldistribution off`"
                )
            else:
                await event.respond(
                    f"🎯 **CANAL DISTRIBUTION #R**\n\n"
                    f"❌ Inactif\n\n"
                    f"**Usage:** `/canaldistribution [ID]`"
                )
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            old_id = DISTRIBUTION_CHANNEL_ID
            DISTRIBUTION_CHANNEL_ID = None
            await event.respond(f"❌ **Canal Distribution désactivé** (était: `{old_id}`)")
            logger.info(f"Admin désactive canal distribution")
            return
        
        try:
            new_id = int(arg)
            channel_entity = await resolve_channel(new_id)
            if not channel_entity:
                await event.respond(f"❌ Canal `{new_id}` inaccessible")
                return
            
            old_id = DISTRIBUTION_CHANNEL_ID
            DISTRIBUTION_CHANNEL_ID = new_id
            
            await event.respond(f"✅ **Canal Distribution: {old_id} → {new_id}**")
            logger.info(f"Admin change canal distribution: {old_id} → {new_id}")
            
        except ValueError:
            await event.respond("❌ Usage: `/canaldistribution [ID]` ou `/canaldistribution off`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_canal_distribution: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_canal_compteur2(event):
    """Commande /canalcompteur2 - Gérer le canal de redirection Compteur2"""
    global COMPTEUR2_CHANNEL_ID
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            if COMPTEUR2_CHANNEL_ID:
                await event.respond(
                    f"📊 **CANAL COMPTEUR2**\n\n"
                    f"✅ Actif: `{COMPTEUR2_CHANNEL_ID}`\n\n"
                    f"**Usage:** `/canalcompteur2 [ID]` ou `/canalcompteur2 off`"
                )
            else:
                await event.respond(
                    f"📊 **CANAL COMPTEUR2**\n\n"
                    f"❌ Inactif\n\n"
                    f"**Usage:** `/canalcompteur2 [ID]`"
                )
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            old_id = COMPTEUR2_CHANNEL_ID
            COMPTEUR2_CHANNEL_ID = None
            await event.respond(f"❌ **Canal Compteur2 désactivé** (était: `{old_id}`)")
            logger.info(f"Admin désactive canal compteur2")
            return
        
        try:
            new_id = int(arg)
            channel_entity = await resolve_channel(new_id)
            if not channel_entity:
                await event.respond(f"❌ Canal `{new_id}` inaccessible")
                return
            
            old_id = COMPTEUR2_CHANNEL_ID
            COMPTEUR2_CHANNEL_ID = new_id
            
            await event.respond(f"✅ **Canal Compteur2: {old_id} → {new_id}**")
            logger.info(f"Admin change canal compteur2: {old_id} → {new_id}")
            
        except ValueError:
            await event.respond("❌ Usage: `/canalcompteur2 [ID]` ou `/canalcompteur2 off`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_canal_compteur2: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_canaux(event):
    """Commande /canaux - Voir tous les canaux configurés"""
    global DISTRIBUTION_CHANNEL_ID, COMPTEUR2_CHANNEL_ID, PREDICTION_CHANNEL_ID, SOURCE_CHANNEL_ID
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📡 **CONFIGURATION DES CANAUX**",
        "",
        f"📥 **Source:** `{SOURCE_CHANNEL_ID}`",
        f"📤 **Principal:** `{PREDICTION_CHANNEL_ID}`",
        "",
        f"🎯 **Distribution #R:** {f'`{DISTRIBUTION_CHANNEL_ID}`' if DISTRIBUTION_CHANNEL_ID else '❌'}",
        f"📊 **Compteur2:** {f'`{COMPTEUR2_CHANNEL_ID}`' if COMPTEUR2_CHANNEL_ID else '❌'}",
    ]
    
    await event.respond("\n".join(lines))

async def cmd_queue(event):
    """Commande /queue - Voir la file d'attente."""
    global prediction_queue, current_game_number, MIN_GAP_BETWEEN_PREDICTIONS, PREDICTION_SEND_AHEAD, pause_active
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        if pause_active:
            await event.respond("⏸️ **En pause** - File d'attente figée")
            return
        
        lines = [
            "📋 **FILE D'ATTENTE**",
            f"Écart: {MIN_GAP_BETWEEN_PREDICTIONS} | Envoi: N-{PREDICTION_SEND_AHEAD}",
            "",
        ]
        
        if not prediction_queue:
            lines.append("❌ Vide")
        else:
            lines.append(f"**{len(prediction_queue)} prédictions:**\n")
            
            for i, pred in enumerate(prediction_queue, 1):
                suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
                pred_type = pred['type']
                pred_num = pred['game_number']
                
                type_str = "🎯Dist" if pred_type == 'distribution' else "📊C2" if pred_type == 'compteur2' else "🤖"
                
                send_threshold = pred_num - PREDICTION_SEND_AHEAD
                
                if current_game_number >= send_threshold:
                    status = "🟢 PRÊT" if not pending_predictions else "⏳ Attente"
                else:
                    wait_num = send_threshold - current_game_number
                    status = f"⏳ Dans {wait_num}"
                
                lines.append(f"{i}. #{pred_num} {suit} | {type_str} | {status}")
        
        lines.append(f"\n🎮 Canal: #{current_game_number}")
        
        await event.respond("\n".join(lines))
        
    except Exception as e:
        logger.error(f"Erreur cmd_queue: {e}")
        await event.respond(f"❌ Erreur: {str(e)}")

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
            status_str = "✅ ON" if compteur2_active else "❌ OFF"
            
            lines = [
                "📊 **COMPTEUR2**",
                f"Statut: {status_str} | Seuil B: {compteur2_seuil_B}",
                "",
            ]
            
            for suit in ALL_SUITS:
                tracker = compteur2_trackers.get(suit)
                if tracker:
                    progress = min(tracker.counter, compteur2_seuil_B)
                    bar = f"[{'█' * progress}{'░' * (compteur2_seuil_B - progress)}]"
                    status = "🔮 PRÊT" if tracker.counter >= compteur2_seuil_B else f"{tracker.counter}/{compteur2_seuil_B}"
                    lines.append(f"{tracker.get_display_name()}: {bar} {status}")
            
            lines.append(f"\n**Usage:** `/compteur2 [B/on/off/reset]`")
            
            await event.respond("\n".join(lines))
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            compteur2_active = False
            await event.respond("❌ **Compteur2 OFF**")
        elif arg == 'on':
            compteur2_active = True
            await event.respond("✅ **Compteur2 ON**")
        elif arg == 'reset':
            for tracker in compteur2_trackers.values():
                tracker.counter = 0
            await event.respond("🔄 **Compteur2 reset**")
        else:
            try:
                b_val = int(arg)
                if not 2 <= b_val <= 10:
                    await event.respond("❌ B entre 2 et 10")
                    return
                compteur2_seuil_B = b_val
                await event.respond(f"✅ **Seuil B = {b_val}**")
            except ValueError:
                await event.respond("❌ Usage: `/compteur2 [B/on/off/reset]`")
                
    except Exception as e:
        logger.error(f"Erreur cmd_compteur2: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = ["📜 **HISTORIQUE**", ""]
    
    recent = prediction_history[:10]
    
    if not recent:
        lines.append("❌ Aucune prédiction")
    else:
        for i, pred in enumerate(recent, 1):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            
            rule = "🎯#R" if pred.get('type') == 'distribution' else "📊C2" if pred.get('type') == 'compteur2' else "🤖"
            emoji = {'en_cours': '🎰', 'gagne_r0': '🏆', 'gagne_r1': '🏆', 'gagne_r2': '🏆', 'perdu': '💔'}.get(status, '❓')
            
            lines.append(f"{i}. {emoji} #{pred['predicted_game']} {suit} | {rule} | {status}")
            lines.append(f"   🕐 {pred_time}")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    global compteur2_active, compteur2_seuil_B, DISTRIBUTION_PLUS_VALUE
    global DISTRIBUTION_CHANNEL_ID, COMPTEUR2_CHANNEL_ID, pause_active, pause_counter, PREDICTIONS_BEFORE_PAUSE, PAUSE_CYCLE
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    compteur2_str = "✅ ON" if compteur2_active else "❌ OFF"
    pause_str = "🟢 ACTIVE" if pause_active else "🔴 INACTIVE"
    
    lines = [
        "📊 **STATUT**",
        "",
        f"➕ Distribution: +{DISTRIBUTION_PLUS_VALUE}",
        f"📊 Compteur2: {compteur2_str} (B={compteur2_seuil_B})",
        f"📏 Écart: {MIN_GAP_BETWEEN_PREDICTIONS}",
        f"⏸️ Pause: {pause_str} ({pause_counter}/{PREDICTIONS_BEFORE_PAUSE})",
        f"🔄 Cycle pause: {PAUSE_CYCLE}",
        f"📋 File: {len(prediction_queue)} | Actives: {len(pending_predictions)}",
        f"🎮 Canal: #{current_game_number}",
        "",
        f"🎯 Distrib: {DISTRIBUTION_CHANNEL_ID or '❌'}",
        f"📊 C2: {COMPTEUR2_CHANNEL_ID or '❌'}",
    ]
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    help_text = f"""📖 **BACCARAT AI**

**⚙️ Config:**
`/plus [1-20]` - Valeur #R (+{DISTRIBUTION_PLUS_VALUE})
`/gap [2-10]` - Écart min ({MIN_GAP_BETWEEN_PREDICTIONS})
`/compteur2 [B/on/off/reset]`

**📡 Canaux:**
`/canaldistribution [ID/off]`
`/canalcompteur2 [ID/off]`
`/canaux` - Voir config

**⏸️ Pause:**
`/pause [on/off]` - Gérer pause
`/pausecycle [3,5,4]` - Modifier cycle
`/pauseadd [texte]` - Ajouter expression

**📊 Info:**
`/queue` - File d'attente
`/status` - Statut complet
`/history` - Historique
`/reset` - Reset manuel

🤖 Baccarat AI | By Sossou Kouamé"""
    
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

# ============================================================================
# SETUP ET DÉMARRAGE
# ============================================================================

def setup_handlers():
    # Configuration
    client.add_event_handler(cmd_plus, events.NewMessage(pattern=r'^/plus'))
    client.add_event_handler(cmd_gap, events.NewMessage(pattern=r'^/gap'))
    
    # Canaux
    client.add_event_handler(cmd_canal_distribution, events.NewMessage(pattern=r'^/canaldistribution'))
    client.add_event_handler(cmd_canal_compteur2, events.NewMessage(pattern=r'^/canalcompteur2'))
    client.add_event_handler(cmd_canaux, events.NewMessage(pattern=r'^/canaux$'))
    
    # Pause (CORRIGÉ)
    client.add_event_handler(cmd_pause, events.NewMessage(pattern=r'^/pause'))
    client.add_event_handler(cmd_pausecycle, events.NewMessage(pattern=r'^/pausecycle'))
    client.add_event_handler(cmd_pauseadd, events.NewMessage(pattern=r'^/pauseadd'))
    
    # Gestion
    client.add_event_handler(cmd_queue, events.NewMessage(pattern=r'^/queue$'))
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    
    # Messages
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
        
        # CORRECTION: Démarrer la vérification périodique avec vérification de pause bloquée
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
        logger.info(f"⏸️ Pause cycle: {PAUSE_CYCLE} min")
        logger.info(f"📡 Multi-canaux: ACTIVE")
        logger.info(f"✅ Système de pause corrigé - vérification auto toutes les 60s")
        
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

