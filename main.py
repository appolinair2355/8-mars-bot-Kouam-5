import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_CYCLES, ALL_SUITS, SUIT_DISPLAY
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: logger.error("API_ID manquant"); exit(1)
if not API_HASH: logger.error("API_HASH manquant"); exit(1)
if not BOT_TOKEN: logger.error("BOT_TOKEN manquant"); exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

cycle_trackers: Dict[str, any] = {}
game_history: Dict[int, List[str]] = {}

# Structure améliorée pour les prédictions avec suivi des rattrapages
# {
#   game_number: {
#     'suit': '♠',
#     'message_id': 12345,
#     'status': 'en_cours',  # en_cours, ✅0️⃣, ✅1️⃣, ✅2️⃣, ❌
#     'rattrapage': 0,  # 0, 1, ou 2
#     'original_game': 761,  # Numéro original de prédiction
#     'check_count': 0
#   }
# }
pending_predictions: Dict[int, dict] = {}

# Pour les rattrapages: stocke les infos du jeu original
# Clé: numéro de rattrapage (761+1, 761+2), Valeur: numéro original
rattrapage_tracking: Dict[int, int] = {}

current_game_number = 0
last_source_game_number = 0
prediction_channel_ok = False
client = None
suit_block_until: Dict[str, datetime] = {}

# Nombre de tours configurable
CONSECUTIVE_FAILURES_NEEDED = int(os.getenv('FAILURES_NEEDED', '2'))

# ============================================================================
# DÉTECTION DES MESSAGES FINALISÉS
# ============================================================================

def is_message_finalized(message: str) -> bool:
    """
    Vérifie si le message du canal source est finalisé (pas en cours).

    Un message est finalisé s'il:
    - Ne contient PAS ⏰ (en cours)
    - Contient un résultat validé (✅, 🔰, ▶️, ou des parenthèses avec couleurs)

    Returns:
        True si le message est finalisé et peut être analysé
    """
    # Si contient ⏰, c'est en cours → pas finalisé
    if '⏰' in message:
        return False

    # Si contient des indicateurs de résultat final
    if any(indicator in message for indicator in ['✅', '🔰', '▶️', 'FINAL', 'RÉSULTAT']):
        return True

    # Si contient des parenthèses avec des couleurs, c'est probablement un résultat
    if '(' in message and ')' in message:
        return True

    # Si contient un numéro de jeu (#Nxxx) et pas d'indicateur "en cours"
    if re.search(r'#N\s*\d+', message, re.IGNORECASE):
        # Vérifier s'il n'y a pas de mot comme "en cours", "attente", "pending"
        lower_msg = message.lower()
        if any(word in lower_msg for word in ['en cours', 'attente', 'pending', 'wait', '⏳']):
            return False
        return True

    return False

def extract_parentheses_groups(message: str) -> List[str]:
    """Extrait le contenu entre parenthèses (groupes de résultats)."""
    # Format: 8(J♣️8♦️) - 7(2♥️5♠️) ou (♠️ ♦️)
    groups = re.findall(r"\(([^)]*)\)", message)

    # Si on trouve des groupes avec des scores devant (ex: 8(J♣️)), 
    # on extrait aussi le score
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        # Retourner les groupes avec leur score si présent
        return [f"{score}:{content}" if score else content for score, content in scored_groups]

    return groups

def get_suits_in_group(group_str: str) -> List[str]:
    """Extrait les couleurs d'un groupe de parenthèses."""
    # Ignorer la partie score si présente (ex: "8:J♣️8♦️" → on prend "J♣️8♦️")
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]

    suits = []
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')

    for suit in ALL_SUITS:
        if suit in normalized:
            suits.append(suit)
    return suits

# ============================================================================
# SUIVI DES CYCLES (Simplifié pour cette version)
# ============================================================================

@dataclass
class SuitCycleTracker:
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    last_checked_index: int = -1
    miss_counter: int = 0
    current_tour: int = 1

    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        if game_number not in self.cycle_numbers:
            return None

        self.last_checked_index = self.cycle_numbers.index(game_number)

        if suit_found:
            self.miss_counter = 0
            self.current_tour = 1
            return None

        self.miss_counter += 1

        if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
            pred_idx = self.last_checked_index + 1
            if pred_idx < len(self.cycle_numbers):
                pred_num = self.cycle_numbers[pred_idx]
                self.miss_counter = 0
                self.current_tour = 1
                return pred_num
        else:
            self.current_tour = 2

        return None

def initialize_trackers(max_game: int = 2000):
    """Crée les trackers pour chaque couleur."""
    global cycle_trackers

    for suit, config in SUIT_CYCLES.items():
        start = config['start']
        interval = config['interval']
        cycle_nums = list(range(start, max_game + 1, interval))

        cycle_trackers[suit] = SuitCycleTracker(suit=suit, cycle_numbers=cycle_nums)
        logger.info(f"📊 {suit}: {len(cycle_nums)} numéros (jusqu'à {max(cycle_nums)})")

# ============================================================================
# ENVOI DES PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction avec le format demandé."""
    try:
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""

        if not PREDICTION_CHANNEL_ID:
            logger.warning("⚠️ Canal non configuré")
            return None

        sent_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)

        pending_predictions[game_number] = {
            'suit': suit,
            'message_id': sent_msg.id,
            'status': 'en_cours',
            'rattrapage': is_rattrapage,
            'original_game': game_number if is_rattrapage == 0 else None,
            'check_count': 0
        }

        logger.info(f"✅ Prédiction envoyée: #{game_number} - {suit} (rattrapage: {is_rattrapage})")
        return sent_msg.id

    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

# ============================================================================
# VÉRIFICATION DES PRÉDICTIONS AVEC RATTRAPAGES ✅0️⃣ ✅1️⃣ ✅2️⃣ ❌
# ============================================================================

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie les résultats avec système de rattrapages.

    Logique:
    - Vérifie d'abord le jeu prédit (original)
    - Si échec, vérifie jeu+1 (rattrapage 1)
    - Si échec, vérifie jeu+2 (rattrapage 2)
    - Si toujours échec → PERDU

    Format des mises à jour:
    - ✅0️⃣ = Trouvé au premier essai
    - ✅1️⃣ = Trouvé au rattrapage 1
    - ✅2️⃣ = Trouvé au rattrapage 2
    - ❌ = Perdu après 3 tentatives
    """

    # 1. VÉRIFICATION POUR LE JEU ORIGINAL (rattrapage 0)
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]

        # Ne vérifier que si c'est le jeu original (pas un rattrapage déjà)
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            suits_in_result = get_suits_in_group(first_group)

            logger.info(f"🔍 Vérification #{game_number} (original): {target_suit} dans {suits_in_result}")

            if target_suit in suits_in_result:
                # ✅0️⃣ TROUVÉ AU PREMIER ESSAI!
                await update_prediction_message(game_number, '✅0️⃣', trouve=True)
                return True
            else:
                # Échec, lancer le rattrapage 1
                next_game = game_number + 1
                logger.info(f"❌ #{game_number} échoué, lancement rattrapage 1 sur #{next_game}")

                # Créer le rattrapage 1
                await create_rattrapage(game_number, next_game, 1)
                return False

    # 2. VÉRIFICATION POUR LES RATTRAPAGES (1 et 2)
    # Chercher si ce jeu est un rattrapage d'une prédiction originale
    for original_game, pred in list(pending_predictions.items()):
        rattrapage_num = pred.get('rattrapage', 0)

        # Vérifier si ce jeu correspond à un rattrapage actif
        if rattrapage_num > 0 and game_number == original_game + rattrapage_num:
            target_suit = pred['suit']
            suits_in_result = get_suits_in_group(first_group)

            logger.info(f"🔍 Vérification rattrapage {rattrapage_num} (#{game_number}): {target_suit} dans {suits_in_result}")

            if target_suit in suits_in_result:
                # TROUVÉ AU RATTRAPAGE!
                status = f'✅{rattrapage_num}️⃣'
                await update_prediction_message(original_game, status, trouve=True, rattrapage=rattrapage_num)
                return True
            else:
                # Échec du rattrapage
                if rattrapage_num < 2:
                    # Lancer rattrapage suivant
                    next_rattrapage = rattrapage_num + 1
                    next_game = original_game + next_rattrapage
                    logger.info(f"❌ Rattrapage {rattrapage_num} échoué, lancement {next_rattrapage} sur #{next_game}")
                    await create_rattrapage(original_game, next_game, next_rattrapage)
                    return False
                else:
                    # Dernier rattrapage (2) échoué → PERDU
                    logger.info(f"❌ Rattrapage 2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, '❌', trouve=False)
                    return False

    return False

async def create_rattrapage(original_game: int, rattrapage_game: int, rattrapage_num: int):
    """Crée un rattrapage pour une prédiction existante."""
    if original_game not in pending_predictions:
        return

    pred = pending_predictions[original_game]
    suit = pred['suit']

    # Marquer le rattrapage dans le tracking
    rattrapage_tracking[rattrapage_game] = original_game

    logger.info(f"📋 Rattrapage {rattrapage_num} créé: #{rattrapage_game} (original: #{original_game})")

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """
    Met à jour le message de prédiction avec le statut final.

    Status possibles:
    - ✅0️⃣ : Trouvé au premier essai
    - ✅1️⃣ : Trouvé au rattrapage 1
    - ✅2️⃣ : Trouvé au rattrapage 2  
    - ❌ : Perdu après tous les rattrapages
    """
    if game_number not in pending_predictions:
        logger.warning(f"⚠️ Pas de prédiction trouvée pour #{game_number}")
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']

    # Construire le message de résultat
    if status == '✅0️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅0️⃣"
    elif status == '✅1️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅1️⃣ GAGNÉ"
    elif status == '✅2️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅2️⃣ GAGNÉ"
    else:  # ❌
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} :❌ PERDU 😭"

    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {result_line}"""

    try:
        await client.edit_message(PREDICTION_CHANNEL_ID, msg_id, new_msg)

        # Mettre à jour le statut dans pending
        pred['status'] = status

        if trouve:
            logger.info(f"✅ Prédiction gagnante: #{game_number} - {status}")
        else:
            logger.info(f"❌ Prédiction perdue: #{game_number}")
            # Bloquer la couleur 5 minutes
            block_suit(suit, 5)

        # Supprimer la prédiction active (terminée)
        # Note: On garde en mémoire un peu pour l'historique si besoin
        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"❌ Erreur mise à jour message: {e}")

def block_suit(suit: str, minutes: int = 5):
    """Bloque une couleur pendant X minutes."""
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# TRAITEMENT DES MESSAGES ENTRANTS
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    """Traite un message finalisé du canal source."""
    global current_game_number, last_source_game_number

    current_game_number = game_number
    last_source_game_number = game_number

    # Extraire les groupes de parenthèses
    groups = extract_parentheses_groups(message_text)

    if not groups:
        logger.warning(f"⚠️ Jeu #{game_number}: Aucun groupe de parenthèses trouvé")
        return

    # Premier groupe = résultat principal
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)

    logger.info(f"📊 Jeu #{game_number} finalisé: {suits_in_first} dans '{first_group}'")

    # 1. VÉRIFIER LES PRÉDICTIONS ACTIVES (avec rattrapages)
    prediction_checked = await check_prediction_result(game_number, first_group)

    if prediction_checked:
        return  # C'était une prédiction, on s'arrête là

    # 2. ANALYSE DES CYCLES (pour nouvelles prédictions)
    # ... (logique d'analyse des cycles pour créer de nouvelles prédictions)
    for suit, tracker in cycle_trackers.items():
        if game_number not in tracker.cycle_numbers:
            continue

        suit_found = suit in suits_in_first
        pred_num = tracker.process_verification(game_number, suit_found)

        if pred_num:
            # Créer une nouvelle prédiction
            await send_prediction(pred_num, suit, is_rattrapage=0)

async def handle_message(event):
    """Gère les messages du canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id != SOURCE_CHANNEL_ID:
            return

        message_text = event.message.message

        # 1. VÉRIFIER SI LE MESSAGE EST FINALISÉ
        if not is_message_finalized(message_text):
            logger.info(f"⏳ Message non finalisé ignoré: {message_text[:50]}...")
            return

        logger.info(f"✅ Message finalisé détecté: {message_text[:50]}...")

        # 2. Extraire le numéro de jeu
        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            # Essayer de trouver un numéro autrement
            match = re.search(r"(?:^|[^\d])(\d{1,4})(?:[^\d]|$)", message_text)

        if not match:
            logger.warning("⚠️ Numéro de jeu non trouvé dans le message finalisé")
            return

        game_number = int(match.group(1))

        # 3. Traiter le résultat
        await process_game_result(game_number, message_text)

    except Exception as e:
        logger.error(f"❌ Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_status(event):
    """Affiche le statut complet avec les compteurs de tous les costumes."""
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    msg = f"""📈 **COUNTERS DE MANQUES DES CYCLES**

🎮 Dernier jeu: #{current_game_number}
📋 File d'attente: {len(prediction_queue)} prédiction(s)
🔄 Rattrapages en cours: {len(rattrapage_tracking)}

"""

    # Afficher les compteurs pour chaque couleur
    for suit in ALL_SUITS:
        if suit in cycle_trackers:
            tracker = cycle_trackers[suit]
            current = tracker.get_current_target()

            # Barre de progression
            filled = "█" * tracker.miss_counter
            empty = "░" * (CONSECUTIVE_FAILURES_NEEDED - tracker.miss_counter)
            progress_bar = f"[{filled}{empty}]"

            msg += f"""📊 {tracker.get_display_name()}
   ├─ 🎯 Numéro du cycle analysé: #{current if current else 'Aucun'}
   ├─ 📉 Compteur de manques: {tracker.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED} {progress_bar}
   ├─ 🔄 Tour: {tracker.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}
"""

            if tracker.pending_prediction:
                msg += f"   └─ 🔮 PRÉDICTION: Jouer #{tracker.pending_prediction}\n\n"
            elif tracker.miss_counter == 1:
                next_check = tracker.get_next_target()
                msg += f"   └─ ⏳ Prochaine vérif: #{next_check if next_check else 'N/A'}\n\n"
            else:
                msg += f"   └─ ✅ En attente\n\n"

    # Afficher les prédictions actives
    if pending_predictions:
        msg += "**🔮 PRÉDICTIONS ACTIVES:**\n"
        for num, pred in sorted(pending_predictions.items()):
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            msg += f"• #{num}{ratt}: {pred['suit']} - {pred['status']}\n"
    else:
        msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(msg)

async def cmd_help(event):
    """Affiche l'aide complète avec toutes les commandes."""
    if event.is_group or event.is_channel: 
        return

    help_text = """📖 **BACCARAT AI - AIDE COMPLÈTE**

═══════════════════════════════════════
🎮 **SYSTÈME DE PRÉDICTION**
═══════════════════════════════════════

**Cycles des couleurs:**
• ♠️ Pique: tous les 5 jeux (1, 6, 11, 16...)
• ❤️ Cœur: tous les 6 jeux (1, 7, 13, 19...)
• ♦️ Carreau: tous les 6 jeux (1, 7, 13, 19...)
• ♣️ Trèfle: tous les 7 jeux (1, 8, 15, 22...)

**Système de rattrapages:**
• ✅0️⃣ = Trouvé au numéro prédit
• ✅1️⃣ = Trouvé au numéro+1 (rattrapage 1)
• ✅2️⃣ = Trouvé au numéro+2 (rattrapage 2)
• ❌ = Perdu après 3 tentatives

**Détection des messages:**
Le bot vérifie uniquement les messages **finalisés** (sans ⏰)

═══════════════════════════════════════
🔧 **COMMANDES ADMIN**
═══════════════════════════════════════

**/status**
Affiche les compteurs de manques, les prédictions actives et la file d'attente.

**/set_tours [1-5]**
Définit le nombre de manques avant prédiction.
Ex: `/set_tours 2` (défaut), `/set_tours 3`

**/channels**
Affiche les canaux configurés et leur statut d'accessibilité.

**/test**
Envoie une prédiction test au canal pour vérifier l'envoi automatique.

**/announce [message]**
Envoie une annonce décorée au canal de prédiction.
Ex: `/announce 🎉 Mise à jour ce soir!`

**/help**
Affiche cette aide.

═══════════════════════════════════════
👨‍💻 **DÉVELOPPEUR**
═══════════════════════════════════════

**Sossou Kouamé**
📱 WhatsApp: +229 01 95 50 15 64 😂

⏳BACCARAT AI 🤖⏳
"""

    await event.respond(help_text)
    logger.info(f"Help affichée pour user {event.sender_id}")


# ============================================================================
# COMMANDES ADMIN COMPLÉMENTAIRES
# ============================================================================

async def cmd_set_tours(event):
    """Définit le nombre de tours avant prédiction."""
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    global CONSECUTIVE_FAILURES_NEEDED

    try:
        match = re.search(r'/set_tours\s+(\d+)', event.message.message)
        if not match:
            await event.respond("📖 Usage: `/set_tours [1-5]`\nEx: `/set_tours 2`")
            return

        new_value = int(match.group(1))
        if new_value < 1 or new_value > 5:
            await event.respond("❌ Entre 1 et 5 uniquement")
            return

        old_value = CONSECUTIVE_FAILURES_NEEDED
        CONSECUTIVE_FAILURES_NEEDED = new_value

        # Reset les compteurs
        for tracker in cycle_trackers.values():
            tracker.miss_counter = 0
            tracker.current_tour = 1

        await event.respond(f"✅ Changé: {old_value} → {new_value} tours\nLes compteurs ont été réinitialisés.")
        logger.info(f"Admin {event.sender_id}: {old_value} → {new_value} tours")

    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_channels(event):
    """Affiche les canaux configurés."""
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    source_status = "❌ Inaccessible"
    prediction_status = "❌ Inaccessible"

    try:
        if SOURCE_CHANNEL_ID:
            await client.get_entity(SOURCE_CHANNEL_ID)
            source_status = "✅ Accessible"
    except:
        pass

    try:
        if PREDICTION_CHANNEL_ID:
            await client.get_entity(PREDICTION_CHANNEL_ID)
            prediction_status = "✅ Accessible"
    except:
        pass

    msg = f"""📡 **CANAUX CONFIGURÉS**

🔹 **Canal Source**
   ├─ ID: `{SOURCE_CHANNEL_ID}`
   └─ Statut: {source_status}

🔹 **Canal Prédiction**
   ├─ ID: `{PREDICTION_CHANNEL_ID}`
   └─ Statut: {prediction_status}

🔹 **Admin ID**: `{ADMIN_ID}`

💡 Vérifiez que le bot est membre des canaux avec les bonnes permissions."""

    await event.respond(msg)

async def cmd_test(event):
    """Teste l'envoi automatique au canal de prédiction."""
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    await event.respond("🧪 **TEST EN COURS**...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return

        try:
            entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            canal_nom = getattr(entity, 'title', 'Sans titre')
        except Exception as e:
            await event.respond(f"❌ Accès canal impossible: {e}")
            return

        # Envoi test
        test_msg = """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : TEST EN COURS...."""

        sent = await client.send_message(PREDICTION_CHANNEL_ID, test_msg)

        await asyncio.sleep(2)

        result_msg = """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : ✅ TEST RÉUSSI"""

        await client.edit_message(PREDICTION_CHANNEL_ID, sent.id, result_msg)
        await asyncio.sleep(2)
        await client.delete_messages(PREDICTION_CHANNEL_ID, [sent.id])

        await event.respond(f"""✅ **TEST RÉUSSI!**

📋 Résultats:
   ├─ Canal: {canal_nom}
   ├─ Envoi: ✅ OK
   ├─ Modification: ✅ OK
   └─ Suppression: ✅ OK

🎯 L'envoi automatique fonctionne!""")

    except Exception as e:
        await event.respond(f"❌ Échec: {e}")
        logger.error(f"Test échoué: {e}")

async def cmd_announce(event):
    """Envoie une annonce décorée au canal."""
    if event.is_group or event.is_channel: 
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    full_message = event.message.message

    if full_message.strip() in ['/announce', '/annonce']:
        await event.respond("""📢 **ENVOYER UNE ANNONCE**

Usage: `/announce Votre message`

Exemple:
`/announce 🎉 Nouvelle mise à jour!`

Le message sera décoré et signé automatiquement.""")
        return

    parts = full_message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("❌ Fournissez un message. Ex: `/announce Bonjour!`")
        return

    user_text = parts[1].strip()

    if len(user_text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        now = datetime.now()
        date_str = now.strftime("%d/%m/%Y")
        time_str = now.strftime("%H:%M")

        announce_msg = f"""╔══════════════════════════════════════╗
║     📢 ANNONCE OFFICIELLE 📢          ║
╠══════════════════════════════════════╣

{user_text}

╠══════════════════════════════════════╣
║  📅 {date_str}  🕐 {time_str}
╠══════════════════════════════════════╣
║  👨‍💻 Développé par: **Sossou Kouamé**
║  📱 WhatsApp: **+229 01 95 50 15 64** 😂
╚══════════════════════════════════════╝

⏳BACCARAT AI 🤖⏳"""

        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return

        sent = await client.send_message(PREDICTION_CHANNEL_ID, announce_msg)

        await event.respond(f"""✅ **ANNONCE ENVOYÉE!**

📋 Détails:
   ├─ Canal: {PREDICTION_CHANNEL_ID}
   ├─ Message ID: {sent.id}
   ├─ Longueur: {len(user_text)} caractères
   └─ Signé: Sossou Kouamé 😂

📤 Annonce publiée avec succès.""")

    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")
        logger.error(f"Announce échouée: {e}")


def setup_handlers():
    # Commandes simples (sans arguments)
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))

    # Commandes avec arguments - utiliser une fonction de filtre
    client.add_event_handler(cmd_set_tours, events.NewMessage(func=is_set_tours_command))
    client.add_event_handler(cmd_announce, events.NewMessage(func=is_announce_command))

    # Messages entrants
    client.add_event_handler(handle_message, events.NewMessage())

def is_set_tours_command(event):
    """Vérifie si le message est la commande /set_tours."""
    msg = event.message.message.strip()
    return msg.startswith('/set_tours')

def is_announce_command(event):
    """Vérifie si le message est la commande /announce."""
    msg = event.message.message.strip()
    return msg.startswith('/announce')

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    session_string = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
        initialize_trackers(2000)

        if PREDICTION_CHANNEL_ID:
            try:
                await client.get_entity(PREDICTION_CHANNEL_ID)
                prediction_channel_ok = True
                logger.info("✅ Canal prédiction OK")
            except Exception as e:
                logger.error(f"❌ Canal prédiction: {e}")

        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        # Serveur web
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()

        logger.info("🤖 BACCARAT AI avec rattrapages démarré!")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté")
