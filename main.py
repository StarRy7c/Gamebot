import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Timezone
IST = pytz.timezone('Asia/Kolkata')

# Game Configuration
HINT_DURATION = 20  # seconds per hint
MAX_HINTS = 5
FAST_FINGER_BONUS_SECONDS = 5
NEAR_MISS_THRESHOLD = 0.75
STEAL_WINDOW_SECONDS = 2

# Scoring
BASE_POINTS = {
    1: 10,
    2: 8,
    3: 6,
    4: 4,
    5: 2
}

STREAK_MULTIPLIERS = {
    2: 1.1,
    3: 1.2
}

# Milestone achievements
MILESTONES = {
    50: "🌟 *MILESTONE UNLOCKED!* 🌟\n{name} has reached *50 points*! What a brain! 🧠✨",
    100: "🏆 *LEGENDARY ACHIEVEMENT!* 🏆\n{name} has crushed *100 points*! Absolute genius! 🎯🔥",
    150: "👑 *MASTERMIND STATUS!* 👑\n{name} has conquered *150 points*! Unstoppable! 💪⚡",
    200: "🌌 *GALAXY BRAIN!* 🌌\n{name} has dominated with *200 points*! Beyond legendary! 🚀🌟"
}


class GameState:
    def __init__(self):
        self.active_games: Dict[int, 'ActiveGame'] = {}
        self.daily_data: Dict[int, 'DailyData'] = {}
        self.questions: List[Dict] = []
        
    def load_questions(self, filepath: str):
        """Load questions from JSON file"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.questions = json.load(f)
            logger.info(f"Loaded {len(self.questions)} questions")
        except Exception as e:
            logger.error(f"Error loading questions: {e}")
            self.questions = []
    
    def get_random_unused_question(self, group_id: int) -> Optional[Dict]:
        """Get a random question not used today in this group"""
        if group_id not in self.daily_data:
            self.daily_data[group_id] = DailyData()
        
        used_words = self.daily_data[group_id].used_words
        available = [q for q in self.questions if q['word'].lower() not in used_words]
        
        if not available:
            return None
        
        import random
        return random.choice(available)


class DailyData:
    def __init__(self):
        self.used_words: Set[str] = set()
        self.leaderboard: Dict[int, float] = {}
        self.streaks: Dict[int, int] = {}
        self.steal_used: Dict[int, bool] = {}
        self.fastest_guesses: Dict[int, float] = {}
        self.total_correct: Dict[int, int] = {}
        self.user_names: Dict[int, str] = {}
        self.milestones_reached: Dict[int, Set[int]] = {}
        
    def reset(self):
        """Reset daily data at midnight"""
        self.used_words.clear()
        self.leaderboard.clear()
        self.streaks.clear()
        self.steal_used.clear()
        self.fastest_guesses.clear()
        self.total_correct.clear()
        self.milestones_reached.clear()


class ActiveGame:
    def __init__(self, group_id: int, total_questions: int):
        self.group_id = group_id
        self.total_questions = total_questions
        self.current_question_num = 0
        self.current_question: Optional[Dict] = None
        self.current_hint = 0
        self.hint_start_time: Optional[datetime] = None
        self.answered = False
        self.first_messages: Dict[int, str] = {}
        self.hint_message_id: Optional[int] = None
        self.category_revealed = False
        self.wrong_guessers: List[tuple] = []
        self.near_miss_shown: Set[int] = set()
        self.timer_task: Optional[asyncio.Task] = None
        self.game_leaderboard: Dict[int, float] = {}  # Points in this game session


game_state = GameState()


def calculate_similarity(str1: str, str2: str) -> float:
    """Calculate string similarity ratio"""
    return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()


def is_near_miss(guess: str, answer: str) -> bool:
    """Check if guess is close to answer"""
    return calculate_similarity(guess, answer) >= NEAR_MISS_THRESHOLD


def calculate_points(hint_number: int, time_taken: float, streak: int) -> float:
    """Calculate points for a correct guess"""
    base = BASE_POINTS.get(hint_number, 0)
    
    if time_taken <= FAST_FINGER_BONUS_SECONDS:
        base += 1
    
    multiplier = 1.0
    if streak >= 3:
        multiplier = 1.2
    elif streak >= 2:
        multiplier = 1.1
    
    return base * multiplier


def get_add_to_group_button():
    """Create an inline button to add bot to group"""
    keyboard = [[
        InlineKeyboardButton(
            "➕ Add to Your Group",
            url="https://t.me/your_bot_username?startgroup=true"
        )
    ]]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with impressive welcome"""
    
    welcome_text = (
        "🧠 *WELCOME TO INFERENCE MASTER* 🧠\n\n"
        "Think you're smart? Prove it! 🎯\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 *HOW IT WORKS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💡 Get 5 progressive hints\n"
        "⏱️ 20 seconds per hint\n"
        "🏃 Guess faster = MORE points\n"
        "🔥 Build streaks for multipliers\n"
        "😈 Steal points from wrong guessers\n"
        "🏆 Dominate the daily leaderboard\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *WHY YOUR GROUP NEEDS THIS* ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✨ Test your friends' IQ\n"
        "🎯 Daily fresh challenges\n"
        "🤝 Compete & have fun together\n"
        "🧹 Clean chat - no spam!\n"
        "🆓 100% FREE forever\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💎 *SCORING SYSTEM*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🥇 Hint 1: *10 pts* (genius level)\n"
        "🥈 Hint 2: *8 pts* (brilliant)\n"
        "🥉 Hint 3: *6 pts* (smart)\n"
        "📌 Hint 4: *4 pts* (decent)\n"
        "📍 Hint 5: *2 pts* (better late than never)\n\n"
        "⚡ *Fast Bonus:* +1 pt for <5 sec\n"
        "🔥 *Streaks:* 2x = 1.1×, 3x = 1.2×\n"
        "😈 *Steal Mode:* Snatch points within 2 sec\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *READY TO DOMINATE?*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Add me to your group and type:\n"
        "**/play** to start the brain battle! 🧠⚔️\n\n"
        "Your friends won't know what hit them! 🚀"
    )
    
    keyboard = [[
        InlineKeyboardButton("➕ Add to Group & Start Playing!", url="https://t.me/your_bot_username?startgroup=true")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rules command"""
    rules_text = (
        "📜 *GAME RULES*\n\n"
        "🎯 *Objective:*\n"
        "Guess the word from progressive hints!\n\n"
        "⏱️ *Hint System:*\n"
        "• 5 hints total, revealed one by one\n"
        "• 20 seconds per hint\n"
        "• Only your FIRST message per hint counts\n"
        "• Category revealed after Hint 3\n\n"
        "💎 *Scoring:*\n"
        "Hint 1: 10 pts | Hint 2: 8 pts | Hint 3: 6 pts\n"
        "Hint 4: 4 pts  | Hint 5: 2 pts\n\n"
        "⚡ *Bonuses:*\n"
        "• Guess within 5 seconds: +1 point\n"
        "• 2-guess streak: 1.1× multiplier\n"
        "• 3+ streak: 1.2× multiplier\n\n"
        "😈 *Steal Mode:*\n"
        "• One steal per game session\n"
        "• If someone guesses wrong, answer correctly within 2 seconds\n"
        "• Steal their points, they get -1\n\n"
        "🎮 *Game Modes:*\n"
        "• Choose 3, 5, or 10 questions per game\n"
        "• Game leaderboard after each question\n"
        "• Final winner announced at end\n\n"
        "🏆 *Leaderboard:*\n"
        "• Game leaderboard: During game\n"
        "• Daily leaderboard: /leaderboard command\n"
        "• Reach 50, 100+ points for special recognition!\n\n"
        "🎭 *Features:*\n"
        "• Near-miss hints when close\n"
        "• Daily reset at midnight\n"
        "• No word repeats per day\n"
        "• Clean chat (wrong guesses ignored)\n"
    )
    await update.message.reply_text(rules_text, parse_mode='Markdown')


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /play command with question count selection"""
    chat_id = update.effective_chat.id
    
    if update.effective_chat.type == 'private':
        await update.message.reply_text("This game can only be played in groups!")
        return
    
    if chat_id in game_state.active_games:
        await update.message.reply_text("A game is already in progress! Wait for it to finish or use /stop")
        return
    
    keyboard = [
        [
            InlineKeyboardButton("🎯 3 Questions (Quick)", callback_data="game_3"),
            InlineKeyboardButton("🎮 5 Questions (Classic)", callback_data="game_5"),
        ],
        [
            InlineKeyboardButton("🔥 10 Questions (Marathon)", callback_data="game_10"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎮 *CHOOSE YOUR CHALLENGE*\n\n"
        "Select how many questions you want to play:\n\n"
        "🎯 *Quick (3):* Fast-paced fun\n"
        "🎮 *Classic (5):* Perfect balance\n"
        "🔥 *Marathon (10):* Ultimate brain test\n\n"
        "Ready to prove your genius? 🧠",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


async def game_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle game mode selection"""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    data = query.data
    
    if not data.startswith("game_"):
        return
    
    num_questions = int(data.split("_")[1])
    
    question = game_state.get_random_unused_question(chat_id)
    if not question:
        await query.edit_message_text(
            "All questions have been used today! Come back tomorrow for fresh questions. 🌅"
        )
        return
    
    game = ActiveGame(chat_id, num_questions)
    game.current_question = question
    game_state.active_games[chat_id] = game
    
    if chat_id not in game_state.daily_data:
        game_state.daily_data[chat_id] = DailyData()
    game_state.daily_data[chat_id].used_words.add(question['word'].lower())
    
    await query.edit_message_text(
        f"🎮 *GAME STARTING!*\n\n"
        f"📊 *Mode:* {num_questions} Questions\n"
        f"🎯 *Get ready to think!*\n\n"
        f"First question coming up... 🧠",
        parse_mode='Markdown'
    )
    
    await asyncio.sleep(2)
    await start_question(context, chat_id)


async def start_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Start a new question in the game"""
    game = game_state.active_games.get(chat_id)
    if not game:
        return
    
    game.current_question_num += 1
    game.current_hint = 0
    game.answered = False
    game.category_revealed = False
    game.first_messages.clear()
    game.wrong_guessers.clear()
    game.near_miss_shown.clear()
    
    if game.current_question_num > 1:
        question = game_state.get_random_unused_question(chat_id)
        if not question:
            await end_game(context, chat_id)
            return
        game.current_question = question
        game_state.daily_data[chat_id].used_words.add(question['word'].lower())
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"❓ *Question {game.current_question_num}/{game.total_questions}*\n\nGet ready... 🎯",
        parse_mode='Markdown'
    )
    
    await asyncio.sleep(1)
    await start_hint(context, chat_id)


async def start_hint(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Start a new hint window"""
    game = game_state.active_games.get(chat_id)
    if not game or game.answered:
        return
    
    game.current_hint += 1
    game.hint_start_time = datetime.now(IST)
    game.first_messages.clear()
    game.wrong_guessers.clear()
    
    if game.current_hint > MAX_HINTS:
        await handle_no_answer(context, chat_id)
        return
    
    category_text = ""
    if game.current_hint == 3 and not game.category_revealed:
        game.category_revealed = True
        category_text = f"\n\n🧠 *Category:* {game.current_question.get('category', 'Unknown')}"
    
    hint_text = (
        f"💡 *Hint {game.current_hint}/{MAX_HINTS}*\n"
        f"❓ *Question {game.current_question_num}/{game.total_questions}*\n\n"
        f"_{game.current_question['hints'][game.current_hint - 1]}_\n\n"
        f"⏰ Time remaining: *{HINT_DURATION}s*"
        f"{category_text}"
    )
    
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=hint_text,
        parse_mode='Markdown'
    )
    game.hint_message_id = message.message_id
    
    if game.timer_task:
        game.timer_task.cancel()
    game.timer_task = asyncio.create_task(
        update_hint_timer(context, chat_id, HINT_DURATION)
    )


async def update_hint_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, duration: int):
    """Update hint message with countdown"""
    game = game_state.active_games.get(chat_id)
    if not game or game.answered:
        return
    
    checkpoints = [10, 5]
    
    for checkpoint in checkpoints:
        await asyncio.sleep(duration - checkpoint)
        
        if game.answered or chat_id not in game_state.active_games:
            return
        
        category_text = ""
        if game.category_revealed:
            category_text = f"\n\n🧠 *Category:* {game.current_question.get('category', 'Unknown')}"
        
        hint_text = (
            f"💡 *Hint {game.current_hint}/{MAX_HINTS}*\n"
            f"❓ *Question {game.current_question_num}/{game.total_questions}*\n\n"
            f"_{game.current_question['hints'][game.current_hint - 1]}_\n\n"
            f"⏰ Time remaining: *{checkpoint}s*"
            f"{category_text}"
        )
        
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.hint_message_id,
                text=hint_text,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error updating timer: {e}")
    
    await asyncio.sleep(5)
    
    if not game.answered and chat_id in game_state.active_games:
        await start_hint(context, chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all messages in group"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    
    if chat_id not in game_state.active_games:
        return
    
    game = game_state.active_games[chat_id]
    
    if game.answered:
        return
    
    if user_id in game.first_messages:
        return
    
    game.first_messages[user_id] = message_text
    
    if chat_id not in game_state.daily_data:
        game_state.daily_data[chat_id] = DailyData()
    game_state.daily_data[chat_id].user_names[user_id] = update.effective_user.first_name
    
    correct_answer = game.current_question['word'].lower()
    user_guess = message_text.strip().lower()
    
    if user_guess == correct_answer:
        await handle_correct_guess(update, context, chat_id, user_id)
    else:
        await handle_wrong_guess(update, context, chat_id, user_id, user_guess, correct_answer)


async def handle_correct_guess(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                               chat_id: int, user_id: int):
    """Handle correct answer"""
    game = game_state.active_games[chat_id]
    daily_data = game_state.daily_data[chat_id]
    
    time_taken = (datetime.now(IST) - game.hint_start_time).total_seconds()
    
    steal_happened = False
    stolen_from = None
    
    if game.wrong_guessers and not daily_data.steal_used.get(user_id, False):
        for wrong_user_id, wrong_time in game.wrong_guessers:
            time_diff = (datetime.now(IST) - wrong_time).total_seconds()
            if time_diff <= STEAL_WINDOW_SECONDS:
                steal_happened = True
                stolen_from = wrong_user_id
                daily_data.steal_used[user_id] = True
                break
    
    current_streak = daily_data.streaks.get(user_id, 0) + 1
    daily_data.streaks[user_id] = current_streak
    
    points = calculate_points(game.current_hint, time_taken, current_streak)
    
    daily_data.leaderboard[user_id] = daily_data.leaderboard.get(user_id, 0) + points
    game.game_leaderboard[user_id] = game.game_leaderboard.get(user_id, 0) + points
    
    if user_id not in daily_data.fastest_guesses or time_taken < daily_data.fastest_guesses[user_id]:
        daily_data.fastest_guesses[user_id] = time_taken
    
    daily_data.total_correct[user_id] = daily_data.total_correct.get(user_id, 0) + 1
    
    steal_text = ""
    if steal_happened and stolen_from:
        daily_data.leaderboard[stolen_from] = daily_data.leaderboard.get(stolen_from, 0) - 1
        game.game_leaderboard[stolen_from] = game.game_leaderboard.get(stolen_from, 0) - 1
        daily_data.streaks[stolen_from] = 0
        stolen_name = daily_data.user_names.get(stolen_from, "Unknown")
        steal_text = f"\n\n😈 *STEAL!* Snatched points from {stolen_name}!"
    
    user_name = update.effective_user.first_name
    
    result_text = (
        f"✅ *CORRECT!* Well done {user_name}! 🎉\n\n"
        f"🎯 Answer: *{game.current_question['word']}*\n"
        f"💰 Points earned: *+{points:.1f}*\n"
        f"⏱️ Time: *{time_taken:.1f}s*\n"
        f"🔥 Streak: *{current_streak}*"
        f"{steal_text}"
    )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=result_text,
        parse_mode='Markdown'
    )
    
    game.answered = True
    
    if game.timer_task:
        game.timer_task.cancel()
    
    await asyncio.sleep(2)
    
    await show_game_leaderboard(context, chat_id)
    
    await asyncio.sleep(2)
    
    # Check for milestones
    total_points = daily_data.leaderboard[user_id]
    if user_id not in daily_data.milestones_reached:
        daily_data.milestones_reached[user_id] = set()
    
    for milestone, message in MILESTONES.items():
        if total_points >= milestone and milestone not in daily_data.milestones_reached[user_id]:
            daily_data.milestones_reached[user_id].add(milestone)
            await context.bot.send_message(
                chat_id=chat_id,
                text=message.format(name=user_name),
                parse_mode='Markdown'
            )
            await asyncio.sleep(1)
    
    if game.current_question_num < game.total_questions:
        await start_question(context, chat_id)
    else:
        await end_game(context, chat_id)


async def handle_wrong_guess(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, user_id: int, guess: str, answer: str):
    """Handle wrong answer"""
    game = game_state.active_games[chat_id]
    daily_data = game_state.daily_data[chat_id]
    
    if user_id in daily_data.streaks:
        daily_data.streaks[user_id] = 0
    
    game.wrong_guessers.append((user_id, datetime.now(IST)))
    
    if is_near_miss(guess, answer) and user_id not in game.near_miss_shown:
        game.near_miss_shown.add(user_id)
        await update.message.reply_text("👀 Very close... think again.")


async def handle_no_answer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Handle when no one answers correctly"""
    game = game_state.active_games[chat_id]
    if not game:
        return
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ *Time's up!*\n\nThe answer was: *{game.current_question['word']}*\n\nMoving to next question...",
        parse_mode='Markdown'
    )
    
    game.answered = True
    
    if game.timer_task:
        game.timer_task.cancel()
    
    await asyncio.sleep(2)
    
    if game.current_question_num < game.total_questions:
        await start_question(context, chat_id)
    else:
        await end_game(context, chat_id)


async def show_game_leaderboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Show current game leaderboard"""
    game = game_state.active_games.get(chat_id)
    if not game or not game.game_leaderboard:
        return
    
    daily_data = game_state.daily_data[chat_id]
    
    sorted_players = sorted(
        game.game_leaderboard.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]
    
    leaderboard_text = (
        f"📊 *GAME LEADERBOARD*\n"
        f"After Question {game.current_question_num}/{game.total_questions}\n\n"
    )
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for idx, (user_id, points) in enumerate(sorted_players):
        medal = medals[idx] if idx < len(medals) else f"{idx + 1}."
        name = daily_data.user_names.get(user_id, "Unknown")
        streak = daily_data.streaks.get(user_id, 0)
        streak_emoji = f" 🔥×{streak}" if streak > 0 else ""
        
        leaderboard_text += f"{medal} {name}: *{points:.1f} pts*{streak_emoji}\n"
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=leaderboard_text,
        parse_mode='Markdown'
    )


async def end_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """End the game and show final results"""
    game = game_state.active_games.get(chat_id)
    if not game:
        return
    
    daily_data = game_state.daily_data[chat_id]
    
    if not game.game_leaderboard:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎮 Game ended! No one scored in this round."
        )
        del game_state.active_games[chat_id]
        return
    
    sorted_players = sorted(
        game.game_leaderboard.items(),
        key=lambda x: x[1],
        reverse=True
    )
    
    winner_id, winner_points = sorted_players[0]
    winner_name = daily_data.user_names.get(winner_id, "Champion")
    
    result_text = (
        f"🎮 *GAME COMPLETE!* 🎮\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *WINNER* 🏆\n"
        f"👑 *{winner_name}* with *{winner_points:.1f} points*!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 *Final Standings:*\n\n"
    )
    
    medals = ["🥇", "🥈", "🥉"]
    for idx, (user_id, points) in enumerate(sorted_players[:3]):
        medal = medals[idx] if idx < 3 else f"{idx + 1}."
        name = daily_data.user_names.get(user_id, "Unknown")
        result_text += f"{medal} {name}: *{points:.1f} pts*\n"
    
    result_text += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Congratulations {winner_name}!* 🎯\n"
        f"Amazing brain power! 🧠⚡\n"
    )
    
    keyboard = [[
        InlineKeyboardButton("➕ Add to Your Group", url="https://t.me/your_bot_username?startgroup=true")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=result_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    
    if game.timer_task:
        game.timer_task.cancel()
    
    del game_state.active_games[chat_id]


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leaderboard command - shows DAILY leaderboard"""
    chat_id = update.effective_chat.id
    
    if chat_id not in game_state.daily_data:
        await update.message.reply_text("No games played yet today!")
        return
    
    daily_data = game_state.daily_data[chat_id]
    
    if not daily_data.leaderboard:
        await update.message.reply_text("No scores recorded yet today!")
        return
    
    sorted_players = sorted(
        daily_data.leaderboard.items(),
        key=lambda x: x[1],
        reverse=True
    )[:10]
    
    leaderboard_text = (
        "🏆 *DAILY LEADERBOARD* 🏆\n"
        "All games today combined\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    medals = ["🥇", "🥈", "🥉"]
    for idx, (user_id, points) in enumerate(sorted_players):
        medal = medals[idx] if idx < 3 else f"{idx + 1}."
        name = daily_data.user_names.get(user_id, "Unknown")
        streak = daily_data.streaks.get(user_id, 0)
        streak_emoji = f" 🔥×{streak}" if streak > 0 else ""
        
        leaderboard_text += f"{medal} {name}: *{points:.1f} pts*{streak_emoji}\n"
    
    await update.message.reply_text(leaderboard_text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in game_state.daily_data:
        await update.message.reply_text("No data available yet!")
        return
    
    daily_data = game_state.daily_data[chat_id]
    
    points = daily_data.leaderboard.get(user_id, 0)
    streak = daily_data.streaks.get(user_id, 0)
    fastest = daily_data.fastest_guesses.get(user_id)
    correct = daily_data.total_correct.get(user_id, 0)
    
    stats_text = (
        f"📊 *YOUR DAILY STATS*\n\n"
        f"💰 Total Points: *{points:.1f}*\n"
        f"🔥 Current Streak: *{streak}*\n"
        f"✅ Correct Guesses: *{correct}*\n"
    )
    
    if fastest:
        stats_text += f"⚡ Fastest Guess: *{fastest:.1f}s*\n"
    
    if daily_data.steal_used.get(user_id):
        stats_text += f"\n😈 Steal used in current game"
    else:
        stats_text += f"\n😈 Steal available!"
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if update.effective_chat.type != 'private':
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['creator', 'administrator']:
            await update.message.reply_text("Only admins can stop the game!")
            return
    
    if chat_id in game_state.active_games:
        game = game_state.active_games[chat_id]
        if game.timer_task:
            game.timer_task.cancel()
        del game_state.active_games[chat_id]
        await update.message.reply_text("Game stopped!")
    else:
        await update.message.reply_text("No game is currently active!")


async def daily_reset(context: ContextTypes.DEFAULT_TYPE):
    """Reset daily data at midnight IST"""
    logger.info("Performing daily reset...")
    
    for chat_id, daily_data in game_state.daily_data.items():
        if daily_data.leaderboard:
            await post_daily_results(context, chat_id)
    
    for daily_data in game_state.daily_data.values():
        daily_data.reset()
    
    logger.info("Daily reset complete")


async def post_daily_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Post end-of-day results"""
    daily_data = game_state.daily_data[chat_id]
    
    if not daily_data.leaderboard:
        return
    
    sorted_players = sorted(
        daily_data.leaderboard.items(),
        key=lambda x: x[1],
        reverse=True
    )
    
    result_text = "🏆 *DAILY RESULTS* 🏆\n\n"
    
    medals = ["🥇 Winner", "🥈 Runner-up", "🥉 Third"]
    for idx in range(min(3, len(sorted_players))):
        user_id, points = sorted_players[idx]
        name = daily_data.user_names.get(user_id, "Unknown")
        result_text += f"{medals[idx]}: {name} — *{points:.1f} pts*\n"
    
    max_streak = max(daily_data.streaks.values()) if daily_data.streaks else 0
    min_time = min(daily_data.fastest_guesses.values()) if daily_data.fastest_guesses else 0
    
    result_text += f"\n🔥 Longest Streak: *{max_streak}*\n"
    if min_time > 0:
        result_text += f"⚡ Fastest Guess: *{min_time:.1f}s*\n"
    
    result_text += "\n🌅 See you tomorrow for fresh questions!"
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=result_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error posting daily results: {e}")


def main():
    """Start the bot"""
    # Bot token - hardcoded for Railway deployment
    TOKEN = "8253975107:AAEDZ8P_b-nmudbgOFICAWdP2_DXs51KkuI"
    
    if not TOKEN:
        TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        return
    
    questions_file = os.getenv('QUESTIONS_FILE', 'questions.json')
    game_state.load_questions(questions_file)
    
    if not game_state.questions:
        logger.error("No questions loaded! Bot cannot start.")
        return
    
    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CallbackQueryHandler(game_selection_callback, pattern="^game_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    try:
        job_queue = application.job_queue
        if job_queue:
            midnight_ist = time(hour=0, minute=0, tzinfo=IST)
            job_queue.run_daily(daily_reset, time=midnight_ist)
            logger.info("Daily reset scheduler configured")
        else:
            logger.warning("JobQueue not available - daily reset will not run automatically")
    except Exception as e:
        logger.warning(f"Could not set up job queue: {e}")
    
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
