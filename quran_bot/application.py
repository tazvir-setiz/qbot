import asyncio
import logging

from telegram import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .catalog import load_surahs
from .config import Config
from .database import Repository
from .handlers import Handlers
from .scheduling import DeliveryService


def build_application(config):
    repo = Repository(config.db_path)
    surahs = load_surahs(config.surah_path)

    async def startup(app):
        await repo.initialize()
        try:
            await app.bot.set_my_commands([
                BotCommand("start", "ورود به ربات"), BotCommand("menu", "پنل اصلی"),
                BotCommand("status", "وضعیت برنامه"), BotCommand("cancel", "لغو ورودی فعلی"),
                BotCommand("help", "راهنمای استفاده"),
                BotCommand("group", "انتخاب گروه و آزمایش ارسال"),
            ], scope=BotCommandScopeAllPrivateChats())
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except TelegramError as exc:
            logging.getLogger(__name__).warning("Could not configure command menu (%s)", type(exc).__name__)
        await service.start()
        logging.getLogger(__name__).info("Bot initialized; %d surahs loaded", len(surahs))

    async def shutdown(app):
        await service.stop()

    app = (Application.builder().token(config.token).post_init(startup)
           .post_stop(shutdown).post_shutdown(shutdown).build())
    service = DeliveryService(config, repo, surahs, app.bot)
    handlers = Handlers(config, repo, service)
    app.bot_data.update(service=service, repository=repo)
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", handlers.start, filters=private))
    app.add_handler(CommandHandler("cancel", handlers.cancel, filters=private))
    app.add_handler(CommandHandler(["menu", "status"], handlers.menu, filters=private))
    app.add_handler(CommandHandler("help", handlers.help, filters=private))
    app.add_handler(CommandHandler("group", handlers.group, filters=private))
    app.add_handler(CallbackQueryHandler(handlers.callback))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, handlers.message))
    app.add_error_handler(handlers.error)
    return app


def main():
    try:
        config = Config.load()
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP request logs include the token in the Telegram URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # PTB debug logs contain entire updates, including password messages.
    logging.getLogger("telegram").setLevel(logging.WARNING)
    with asyncio.Runner() as runner:
        runner.get_loop()
        app = build_application(config)
        app.run_polling(close_loop=False, drop_pending_updates=False, bootstrap_retries=3)
