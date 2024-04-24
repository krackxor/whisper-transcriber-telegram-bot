# main.py
# ~~~~~~~
# openai-whisper transcriber-bot for Telegram

# version of this program
version_number = "0.12"

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# https://github.com/FlyingFathead/whisper-transcriber-telegram-bot/
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import time
import re
import signal
import asyncio
import logging
import configparser

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CallbackContext
from telegram.ext import CommandHandler

# Adjust import paths based on new structure
from transcription_handler import process_url_message, set_user_model, get_whisper_model
from utils.bot_token import get_bot_token
from utils.utils import print_startup_message

# Configure basic logging
logging.basicConfig(format='[%(asctime)s] %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Read configuration for restart behavior
config = configparser.ConfigParser()
config.read('config/config.ini')
restart_on_failure = config.getboolean('GeneralSettings', 'RestartOnConnectionFailure', fallback=True)

# Initialize the lock outside of your function to ensure it's shared across all invocations.
queue_lock = asyncio.Lock()

class TranscriberBot:

    # version of this program
    version_number = version_number

    # Class-level attribute for global locking
    processing_lock = asyncio.Lock() 

    def __init__(self):
        self.token = get_bot_token()
        self.task_queue = asyncio.Queue()  # queue tasks
        self.is_processing = asyncio.Lock()  # Lock to ensure one transcription at a time

        self.restart_on_failure = restart_on_failure  # Controls the restart behavior on connection failure

        self.config = configparser.ConfigParser()
        self.config.read('config/config.ini')
        self.model = self.config.get('WhisperSettings', 'Model', fallback='medium.en')
        self.valid_models = self.config.get('ModelSettings', 'ValidModels', fallback='tiny, base, small, medium, large').split(', ')

        self.model_change_limits = {}  # Dictionary to track user rate limits
        self.model_change_cooldown = 20  # Cooldown period in seconds
        self.user_models = {} # Use a dictionary to manage models per user.
        self.user_models_lock = asyncio.Lock()  # Lock for handling user_models dictionary

    async def handle_message(self, update: Update, context: CallbackContext) -> None:

        user_id = update.effective_user.id  # Update the user_id
        message_text = update.message.text  # Get the text of the message received

        # Log the received message along with the user ID
        logger.info(f"Received a message from user ID {user_id}: {message_text}")
        
        # Check and log the model before starting transcription
        current_model = get_whisper_model(user_id)

        logger.debug(f"Current model for user {user_id} before transcription: {current_model}")

        if update.message and update.message.text:
            urls = re.findall(r'(https?://\S+)', update.message.text)

            if urls:
                await self.task_queue.put((update.message.text, context.bot, update))
                queue_length = self.task_queue.qsize()

                logger.info(f"Task added to the queue. Current queue size: {queue_length}")

                # Check if this is the only job and nothing is currently processing.
                response_text = "Your request is next and is currently being processed." if queue_length == 1 else f"Your request has been added to the queue. There are {queue_length - 1} jobs ahead of yours."
                await update.message.reply_text(response_text)
            else:
                await update.message.reply_text("No valid URL detected in your message. Please send a message that includes a valid URL. If you need help, type: /help")

    async def process_queue(self):
        while True:
            message_text, bot, update = await self.task_queue.get()
            async with TranscriberBot.processing_lock:  # Use the class-level lock
                user_id = update.effective_user.id
                model = get_whisper_model(user_id)
                await process_url_message(message_text, bot, update, model)
            self.task_queue.task_done()

    async def shutdown(self, signal, loop):
        """Cleanup tasks tied to the service's shutdown."""
        logger.info(f"Received exit signal {signal.name}...")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        [task.cancel() for task in tasks]

        logger.info(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    async def help_command(self, update: Update, context: CallbackContext) -> None:
        models_list = ', '.join(self.valid_models)  # Dynamically generate the list of valid models
        help_text = f"""<b>Welcome to the Whisper Transcriber Bot!</b>

<b>Version:</b> {self.version_number}

<b>How to Use:</b>
- Send any supported media URL to have its audio transcribed.
- Use /model to change the transcription model.
- Use /help or /about to display this help message.

<b>Whisper model currently in use:</b>
<code>{self.model}</code>

<b>Available Whisper models:</b>
{models_list}

<b>Bot code by FlyingFathead.</b>
Source code on <a href='https://github.com/FlyingFathead/whisper-transcriber-telegram-bot/'>GitHub</a>.

<b>Disclaimer:</b>
The original author is NOT responsible for how this bot is utilized. All code and outputs are provided 'AS IS' without warranty of any kind. Users assume full responsibility for the operation and output of the bot. This applies to both legal and ethical responsibilities. Use at your own risk.
"""
        await update.message.reply_text(help_text, parse_mode='HTML')

    async def model_command(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        current_time = time.time()
        models_list = ', '.join(self.valid_models)  # Dynamically generate the list of valid models
        
        if not context.args:
            current_model = get_whisper_model(user_id)
            await update.message.reply_text(
                f"<b>Current model:</b>\n<code>{current_model}</code>\n\n"
                f"<b>Available models:</b>\n{models_list}\n\n"
                "To change the model, use commands like:\n"
                "<code>/model medium.en</code>\n"
                "<code>/model large-v3</code>\n\n"
                "<b>Model Details:</b>\n"
                "- <b>Tiny</b>: Fastest, uses ~1GB VRAM, about 32x faster than large.\n"
                "- <b>Base</b>: Faster, uses ~1GB VRAM, about 16x faster than large.\n"
                "- <b>Small</b>: Balanced, uses ~2GB VRAM, about 6x faster than large.\n"
                "- <b>Medium</b>: More precise, uses ~5GB VRAM, about 2x faster than large.\n"
                "- <b>Large</b>: Most precise, processes at real-time (1x speed), uses ~10GB VRAM.\n\n"
                "Note: '.en' models (e.g., 'tiny.en') are optimized for English and offer better accuracy for English audio. "
                "As model size increases, the benefit of English optimization becomes less significant. Choose based on your "
                "needs for speed, memory usage, and linguistic accuracy. "
                "As a general guideline, larger models are more accurate but also slower. "
                "I.e. best balance in speed vs. accuracy in English language is likely <code>medium.en</code>.",
                parse_mode='HTML')
            return

        new_model = context.args[0]
        if new_model in self.valid_models:
            set_user_model(user_id, new_model)  # Update user-specific model
            self.model_change_limits[user_id] = current_time  # Update cooldown tracker
            await update.message.reply_text(f"Model set to: <code>{new_model}</code>", parse_mode='HTML')
        else:
            await update.message.reply_text(
                f"Invalid model specified.\n\n"
                f"Available models:\n{models_list}",
                parse_mode='HTML')

    def run(self):
        loop = asyncio.get_event_loop()
        
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s, loop)))

        connected = False
        while not connected:
            try:
                self.application = Application.builder().token(self.token).build()

                # Adding handlers
                text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
                self.application.add_handler(text_handler)

                # Here's where you add the command handler for /help
                help_handler = CommandHandler(['help', 'about'], self.help_command)
                self.application.add_handler(help_handler)

                # Adding model command handler
                model_handler = CommandHandler('model', self.model_command)
                self.application.add_handler(model_handler)

                loop.create_task(self.process_queue())
                self.application.run_polling()
                connected = True
            except Exception as e:
                logger.error(f"Failed to start polling due to an error: {e}")
                if self.restart_on_failure:
                    logger.info("Attempting to reconnect in 10 seconds...")
                    time.sleep(10)
                else:
                    logger.error("Restart on failure is disabled. Exiting...")
                    break

if __name__ == '__main__':
    print_startup_message(version_number)  # Print startup message
    bot = TranscriberBot()
    bot.run()
