import os
import ssl
import truststore
import certifi
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

truststore.inject_into_ssl()
load_dotenv()

WEBAPP_URL = os.getenv("WEBAPP_URL", "")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton(
            "🐦 Open Crave",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    await update.message.reply_text(
        "Welcome to Crave! Tap below to open the app.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — Open the app\n"
        "/help — Show this message"
    )

def main():
    token = os.getenv("BOT_TOKEN")
    request = HTTPXRequest(httpx_kwargs={"proxy": None})
    app = ApplicationBuilder().token(token).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
