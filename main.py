import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set
import pytz

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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
NEAR_MISS_THRESHOLD = 0.75  # String similarity threshold
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

# Game State Storage
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
        self.leaderboard: Dict[int, float] = {}  # user_id -> points
        self.streaks: Dict[int, int] = {}  # user_id -> streak count
        self.steal_used: Dict[int, bool] = {}  # user_id -> used steal
        self.fastest_guesses: Dict[int, float] = {}  # user_id -> fastest time
        self.total_correct: Dict[int, int] = {}  # user_id -> correct count
        self.user_names: Dict[int, str] = {}  # user_id -> name
        
    def reset(self):
        """Reset daily data at midnight"""
        self.used_words.clear()
        self.leaderboard.clear()
        self.streaks.clear()
        self.steal_used.clear()
        self.fastest_guesses.clear()
        self.total_correct.clear()


class ActiveGame:
    def __init__(self, group_id: int, question: Dict):
        self.group_id = group_id
        self.question = question
        self.current_hint = 0
        self.hint_start_time: Optional[datetime] = None
        self.answered = False
        self.first_messages: Dict[int, str] = {}  # user_id -> message per hint
        self.hint_message_id: Optional[int] = None
        self.category_revealed = False
        self.wrong_guessers: List[tuple] = []  # [(user_id, timestamp)]
        self.near_miss_shown: Set[int] = set()  # user_ids who got near miss feedback
        self.timer_task: Optional[asyncio.Task] = None


# Global game state
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
    
    # Fast finger bonus
    if time_taken <= FAST_FINGER_BONUS_SECONDS:
        base += 1
    
    # Streak multiplier
    multiplier = 1.0
    if streak >= 3:
        multiplier = 1.2
    elif streak >= 2:
        multiplier = 1.1
    
    return base * multiplier


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    await update.message.reply_text(
        "🧠 *INFERENCE GUESSING GAME*\n\n"
        "Welcome to the thinking game! Guess the word from progressive hints.\n\n"
        "📋 *Commands:*\n"
        "/play - Start a new question\n"
        "/leaderboard - View daily rankings\n"
        "/stats - Your personal stats\n"
        "/rules - Game rules\n"
        "/stop - Stop current game\n\n"
        "🎯 *Quick Rules:*\n"
        "• 5 hints, 20 seconds each\n"
        "• Earlier guesses = more points\n"
        "• Fast answers get bonus points\n"
        "• Build streaks for multipliers\n"
        "• One steal chance per game!\n\n"
        "Ready to test your inference skills? Type /play!",
        parse_mode='Markdown'
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
        "• One steal per game\n"
        "• If someone guesses wrong, answer correctly within 2 seconds\n"
        "• Steal their points, they get -1\n\n"
        "🎭 *Features:*\n"
        "• Near-miss hints when close\n"
        "• Daily leaderboard reset\n"
        "• No word repeats per day\n"
        "• Clean chat (wrong guesses ignored)\n\n"
        "🏆 *Winning:*\n"
        "Most points at end of day wins!"
    )
    await update.message.reply_text(rules_text, parse_mode='Markdown')


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /play command to start a new question"""
    chat_id = update.effective_chat.id
    
    # Only allow in groups
    if update.effective_chat.type == 'private':
        await update.message.reply_text("This game can only be played in groups!")
        return
    
    # Check if game already active
    if chat_id in game_state.active_games:
        await update.message.reply_text("A game is already in progress! Wait for it to finish.")
        return
    
    # Get unused question
    question = game_state.get_random_unused_question(chat_id)
    if not question:
        await update.message.reply_text(
            "All questions have been used today! Come back tomorrow for fresh questions. 🌅"
        )
        return
    
    # Create new game
    game = ActiveGame(chat_id, question)
    game_state.active_games[chat_id] = game
    
    # Mark word as used
    if chat_id not in game_state.daily_data:
        game_state.daily_data[chat_id] = DailyData()
    game_state.daily_data[chat_id].used_words.add(question['word'].lower())
    
    # Start first hint
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
    
    # Check if all hints exhausted
    if game.current_hint > MAX_HINTS:
        await end_game_no_answer(context, chat_id)
        return
    
    # Reveal category after hint 3
    category_text = ""
    if game.current_hint == 3 and not game.category_revealed:
        game.category_revealed = True
        category_text = f"\n\n🧠 *Category unlocked:* {game.question.get('category', 'Unknown')}"
    
    # Send hint message
    hint_text = (
        f"💡 *Hint {game.current_hint}/{MAX_HINTS}*\n\n"
        f"_{game.question['hints'][game.current_hint - 1]}_\n\n"
        f"⏰ Time remaining: *{HINT_DURATION}s*"
        f"{category_text}"
    )
    
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=hint_text,
        parse_mode='Markdown'
    )
    game.hint_message_id = message.message_id
    
    # Start timer countdown
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
    
    # Update at 10s and 5s remaining
    checkpoints = [10, 5]
    
    for checkpoint in checkpoints:
        await asyncio.sleep(duration - checkpoint)
        
        if game.answered or chat_id not in game_state.active_games:
            return
        
        category_text = ""
        if game.category_revealed:
            category_text = f"\n\n🧠 *Category:* {game.question.get('category', 'Unknown')}"
        
        hint_text = (
            f"💡 *Hint {game.current_hint}/{MAX_HINTS}*\n\n"
            f"_{game.question['hints'][game.current_hint - 1]}_\n\n"
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
    
    # Wait for remaining time
    await asyncio.sleep(5)
    
    # Move to next hint
    if not game.answered and chat_id in game_state.active_games:
        await start_hint(context, chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all messages in group"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Check if game is active
    if chat_id not in game_state.active_games:
        return
    
    game = game_state.active_games[chat_id]
    
    # Check if already answered
    if game.answered:
        return
    
    # Check if user already sent message for this hint
    if user_id in game.first_messages:
        return  # Silently ignore subsequent messages
    
    # Store first message
    game.first_messages[user_id] = message_text
    
    # Check if correct answer
    correct_answer = game.question['word'].lower()
    user_guess = message_text.strip().lower()
    
    # Store user name
    if chat_id not in game_state.daily_data:
        game_state.daily_data[chat_id] = DailyData()
    game_state.daily_data[chat_id].user_names[user_id] = update.effective_user.first_name
    
    if user_guess == correct_answer:
        await handle_correct_guess(update, context, chat_id, user_id)
    else:
        await handle_wrong_guess(update, context, chat_id, user_id, user_guess, correct_answer)


async def handle_correct_guess(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                               chat_id: int, user_id: int):
    """Handle correct answer"""
    game = game_state.active_games[chat_id]
    daily_data = game_state.daily_data[chat_id]
    
    # Calculate time taken
    time_taken = (datetime.now(IST) - game.hint_start_time).total_seconds()
    
    # Check for steal opportunity
    steal_happened = False
    stolen_from = None
    
    if game.wrong_guessers and not daily_data.steal_used.get(user_id, False):
        # Check if any wrong guess was within steal window
        for wrong_user_id, wrong_time in game.wrong_guessers:
            time_diff = (datetime.now(IST) - wrong_time).total_seconds()
            if time_diff <= STEAL_WINDOW_SECONDS:
                steal_happened = True
                stolen_from = wrong_user_id
                daily_data.steal_used[user_id] = True
                break
    
    # Update streak
    current_streak = daily_data.streaks.get(user_id, 0) + 1
    daily_data.streaks[user_id] = current_streak
    
    # Calculate points
    points = calculate_points(game.current_hint, time_taken, current_streak)
    
    # Add to leaderboard
    daily_data.leaderboard[user_id] = daily_data.leaderboard.get(user_id, 0) + points
    
    # Track fastest guess
    if user_id not in daily_data.fastest_guesses or time_taken < daily_data.fastest_guesses[user_id]:
        daily_data.fastest_guesses[user_id] = time_taken
    
    # Track total correct
    daily_data.total_correct[user_id] = daily_data.total_correct.get(user_id, 0) + 1
    
    # Handle steal penalties
    steal_text = ""
    if steal_happened and stolen_from:
        daily_data.leaderboard[stolen_from] = daily_data.leaderboard.get(stolen_from, 0) - 1
        daily_data.streaks[stolen_from] = 0  # Reset victim's streak
        stolen_name = daily_data.user_names.get(stolen_from, "Unknown")
        steal_text = f"\n\n😈 *STEAL!* Took points from @{stolen_name} (-1 pt)"
    
    # Penalty for victim if steal
    if steal_happened:
        # Already handled above
        pass
    
    # Build result message
    user_name = update.effective_user.first_name
    
    result_text = (
        f"✅ *CORRECT!*\n\n"
        f"🎯 Answer: *{game.question['word']}*\n"
        f"👤 Winner: @{user_name}\n"
        f"💰 Points earned: *{points:.1f}*\n"
        f"⏱️ Time: *{time_taken:.1f}s*\n"
        f"🔥 Streak: *{current_streak}*"
        f"{steal_text}\n\n"
        f"Total points: *{daily_data.leaderboard[user_id]:.1f}*"
    )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=result_text,
        parse_mode='Markdown'
    )
    
    # Mark game as answered
    game.answered = True
    
    # Cancel timer
    if game.timer_task:
        game.timer_task.cancel()
    
    # Remove game after short delay
    await asyncio.sleep(3)
    if chat_id in game_state.active_games:
        del game_state.active_games[chat_id]


async def handle_wrong_guess(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, user_id: int, guess: str, answer: str):
    """Handle wrong answer"""
    game = game_state.active_games[chat_id]
    daily_data = game_state.daily_data[chat_id]
    
    # Reset streak
    if user_id in daily_data.streaks:
        daily_data.streaks[user_id] = 0
    
    # Add to wrong guessers for steal tracking
    game.wrong_guessers.append((user_id, datetime.now(IST)))
    
    # Check for near miss
    if is_near_miss(guess, answer) and user_id not in game.near_miss_shown:
        game.near_miss_shown.add(user_id)
        await update.message.reply_text("👀 Very close... think again.")


async def end_game_no_answer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """End game when no one answers correctly"""
    game = game_state.active_games.get(chat_id)
    if not game:
        return
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ *Time's up!*\n\nThe answer was: *{game.question['word']}*\n\nBetter luck next time!",
        parse_mode='Markdown'
    )
    
    # Cancel timer
    if game.timer_task:
        game.timer_task.cancel()
    
    # Remove game
    if chat_id in game_state.active_games:
        del game_state.active_games[chat_id]


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leaderboard command"""
    chat_id = update.effective_chat.id
    
    if chat_id not in game_state.daily_data:
        await update.message.reply_text("No games played yet today!")
        return
    
    daily_data = game_state.daily_data[chat_id]
    
    if not daily_data.leaderboard:
        await update.message.reply_text("No scores recorded yet!")
        return
    
    # Sort by points
    sorted_players = sorted(
        daily_data.leaderboard.items(),
        key=lambda x: x[1],
        reverse=True
    )[:10]  # Top 10
    
    leaderboard_text = "🏆 *DAILY LEADERBOARD* 🏆\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for idx, (user_id, points) in enumerate(sorted_players):
        medal = medals[idx] if idx < 3 else f"{idx + 1}."
        name = daily_data.user_names.get(user_id, "Unknown")
        streak = daily_data.streaks.get(user_id, 0)
        streak_emoji = "🔥" if streak > 0 else ""
        
        leaderboard_text += f"{medal} @{name}: *{points:.1f} pts* {streak_emoji}\n"
    
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
        f"📊 *YOUR STATS*\n\n"
        f"💰 Total Points: *{points:.1f}*\n"
        f"🔥 Current Streak: *{streak}*\n"
        f"✅ Correct Guesses: *{correct}*\n"
    )
    
    if fastest:
        stats_text += f"⚡ Fastest Guess: *{fastest:.1f}s*\n"
    
    if daily_data.steal_used.get(user_id):
        stats_text += f"\n😈 Steal used today"
    else:
        stats_text += f"\n😈 Steal available!"
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check admin rights in group
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
    
    # Post results for all groups
    for chat_id, daily_data in game_state.daily_data.items():
        if daily_data.leaderboard:
            await post_daily_results(context, chat_id)
    
    # Reset all data
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
    
    # Top 3
    medals = ["🥇 Winner", "🥈 Runner-up", "🥉 Third"]
    for idx in range(min(3, len(sorted_players))):
        user_id, points = sorted_players[idx]
        name = daily_data.user_names.get(user_id, "Unknown")
        result_text += f"{medals[idx]}: @{name} — *{points:.1f} pts*\n"
    
    # Find longest streak
    max_streak = max(daily_data.streaks.values()) if daily_data.streaks else 0
    
    # Find fastest guess
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
    
    # Fallback to environment variable if needed
    if not TOKEN:
        TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        return
    
    # Load questions
    questions_file = os.getenv('QUESTIONS_FILE', 'questions.json')
    game_state.load_questions(questions_file)
    
    if not game_state.questions:
        logger.error("No questions loaded! Bot cannot start.")
        return
    
    # Create application with proper configuration for Python 3.13+
    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedule daily reset at midnight IST
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
    
    # Start bot
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
