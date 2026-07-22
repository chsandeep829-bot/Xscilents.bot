import asyncio
import io
import logging
import os
import random
import re
import urllib.parse
from aiohttp import web
import qrcode
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Enable console logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- CONFIGURATION ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8979881938:AAEAcd8z64fDbJfwTvi6-Bw0eJCJa6M_RTY")
MERCHANT_UPI_ID = os.getenv("MERCHANT_UPI_ID", "c.sandeep@superyes")
MERCHANT_NAME = os.getenv("MERCHANT_NAME", "Key Store")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "https://xscilents-bot.onrender.com")

# ---------- DATA STORAGE & MULTI-FILE MANAGEMENT ----------
active_checkout_sessions = {}
processed_transactions = set()
user_purchased_keys = {}

def get_filename_for_product(product_name):
    """Determine which text file to use based on the selected product."""
    product_name = product_name.upper()
    if "5 HOURS" in product_name:
        return "keys_5h.txt"
    elif "1 DAY" in product_name:
        return "keys_1d.txt"
    elif "7 DAYS" in product_name:
        return "keys_7d.txt"
    elif "30 DAYS" in product_name:
        return "keys_30d.txt"
    elif "FULL SEASON" in product_name:
        return "keys_season.txt"
    return "keys_general.txt"

def load_license_keys(filename):
    """Load available keys from the specified text file or create defaults."""
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    default_keys = [f"SAMPLE-{filename.split('_')[1].split('.')[0].upper()}-1234"]
    save_license_keys(filename, default_keys)
    return default_keys

def save_license_keys(filename, keys_list):
    """Save remaining keys back to the specified text file."""
    with open(filename, "w") as f:
        f.write("\n".join(keys_list) + "\n")

# ---------- MENUS ----------
main_menu = ReplyKeyboardMarkup(
    [
        ["🔑 Purchase Key", "📋 My Keys"],
        ["🎁 Redeem Code", "📖 How to Buy"],
        ["🆔 My ID", "🆘 Contact Support"],
    ],
    resize_keyboard=True,
)

brands_menu = ReplyKeyboardMarkup(
    [["XSCILENT LOADER"], ["⬅️ Back"]], resize_keyboard=True
)

xscilent_menu = ReplyKeyboardMarkup(
    [
        ["XSCILENT 5 HOURS - ₹40", "XSCILENT 1 DAY - ₹100"],
        ["XSCILENT 7 DAYS - ₹300", "XSCILENT 30 DAYS - ₹800"],
        ["XSCILENT FULL SEASON - ₹1200", "⬅️ Back to Brands"],
    ],
    resize_keyboard=True,
)


# ---------- AUTOMATION WEB RECEIVER ----------
async def index(request):
    """Simple root health-check endpoint for UptimeRobot."""
    return web.Response(text="Xscilent Bot is active and running!", status=200)

async def handle_notification_webhook(request):
    """Listens for payment notifications forwarded from the SMS Forwarder app."""
    try:
        content_type = request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            data = await request.post()

        title = data.get("title", "")
        message = (
            data.get("body", "") 
            or data.get("message", "") 
            or data.get("key", "") 
            or data.get("text", "")
        )
        received_text = f"{title} {message}"
        
        logger.info("--- WEBHOOK RECEIVED ---")
        logger.info(f"Parsed Content: {received_text}")
        logger.info(f"Active Sessions in Memory: {list(active_checkout_sessions.keys())}")

        is_sbi_deposit = "Deposited in your SBI bank" in received_text or "SBI" in received_text
        amt_match = re.search(
            r"(?:Rs\.?|INR|₹)\s*(\d+(?:\.\d{1,2})?)", received_text, re.IGNORECASE
        )

        if is_sbi_deposit and amt_match:
            detected_amount = float(amt_match.group(1))
            logger.info(f"Detected Amount: ₹{detected_amount}")
            
            tx_signature = received_text.strip()
            if tx_signature in processed_transactions:
                logger.warning("Duplicate transaction ignored.")
                return web.Response(text="Duplicate transaction ignored.", status=200)

            if not active_checkout_sessions:
                logger.warning("❌ PAYMENT FAILED: No active checkout sessions found in memory!")
                return web.Response(text="No active checkout sessions.", status=200)

            for user_id, session in list(active_checkout_sessions.items()):
                logger.info(f"Comparing session price ₹{session['price']} with detected amount ₹{detected_amount}")
                if float(session["price"]) == detected_amount:

                    product_name = session["product"]
                    target_file = get_filename_for_product(product_name)
                    current_keys = load_license_keys(target_file)

                    if not current_keys:
                        await request.app["tg_bot"].send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ **Payment Confirmed for {product_name}!** However, stock pool for this duration is empty."
                                " Contact support immediately."
                            ),
                        )
                        return web.Response(text="Stock Empty fallback executed.", status=200)

                    delivered_key = current_keys.pop(0)
                    save_license_keys(target_file, current_keys)

                    processed_transactions.add(tx_signature)
                    active_checkout_sessions.pop(user_id, None)

                    if user_id not in user_purchased_keys:
                        user_purchased_keys[user_id] = []
                    user_purchased_keys[user_id].append({
                        "product": product_name,
                        "key": delivered_key,
                        "price": session["price"],
                    })

                    await request.app["tg_bot"].send_message(
                        chat_id=user_id,
                        text=(
                            "✅ **Payment Received and Verified Automatically!**\n\n📦"
                            f" Product: `{product_name}`\n🔑 Your"
                            f" Key:\n`{delivered_key}`"
                        ),
                        parse_mode="Markdown",
                        reply_markup=main_menu,
                    )
                    logger.info(f"SUCCESS: Key delivered to user {user_id}")
                    return web.Response(text="Key Auto-Delivered successfully.", status=200)

        logger.warning("❌ Notification received, but no matching price found in active sessions.")
        return web.Response(text="No matching transaction found.", status=200)
        
    except Exception as e:
        logger.error(f"Error handling Webhook: {e}", exc_info=True)
        return web.Response(text="Internal server error.", status=500)


# ---------- START COMMAND ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Key Store", reply_markup=main_menu
    )


# ---------- CORE MESSAGE HANDLER ----------
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data is None:
        context.user_data = {}

    if text == "🔑 Purchase Key" or text == "⬅️ Back to Brands":
        await update.message.reply_text("🎮 Select a brand:", reply_markup=brands_menu)
        return
    elif text == "⬅️ Back":
        await update.message.reply_text("👋 Main Menu", reply_markup=main_menu)
        return
    elif text == "XSCILENT LOADER":
        await update.message.reply_text(
            "⏳ Select duration:", reply_markup=xscilent_menu
        )
        return
    elif text == "📋 My Keys":
        purchased = user_purchased_keys.get(user_id, [])
        if not purchased:
            await update.message.reply_text(
                "📋 You haven't purchased any keys yet.", reply_markup=main_menu
            )
        else:
            msg = "📋 **Your Purchased Keys:**\n\n"
            for idx, item in enumerate(purchased, 1):
                msg += (
                    f"{idx}. **{item['product']}**\n🔑 Key:"
                    f" `{item['key']}`\n💵 Price: ₹{item['price']}\n\n"
                )
            await update.message.reply_text(
                msg, parse_mode="Markdown", reply_markup=main_menu
            )
        return
    elif text == "📖 How to Buy":
        guide_text = (
            "📖 **How to Buy License Keys:**\n\n"
            "1️⃣ Tap **🔑 Purchase Key** from the main menu.\n"
            "2️⃣ Select your desired loader brand.\n"
            "3️⃣ Choose the required validity duration.\n"
            "4️⃣ Scan the QR code or tap the official UPI payment link (or copy"
            f" Merchant UPI ID: `{MERCHANT_UPI_ID}`).\n"
            "5️⃣ Complete the payment using any UPI app (GPay, PhonePe, Paytm,"
            " etc.).\n"
            "6️⃣ Our cloud system automatically detects your payment and delivers"
            " your key instantly! 🚀"
        )
        await update.message.reply_text(
            guide_text, parse_mode="Markdown", reply_markup=main_menu
        )
        return
    elif text == "🆘 Contact Support":
        support_text = (
            "🆘 **Customer Support**\n\nIf you are facing any issues with payments,"
            " key delivery, or loader activation, feel free to reach out:\n\n💬"
            " Support Admin: @c_sandeep\n⏰ Support Hours: 24/7 Automated"
            " Delivery\n\nPlease keep your Order ID handy when"
            " contacting support."
        )
        await update.message.reply_text(
            support_text, parse_mode="Markdown", reply_markup=main_menu
        )
        return
    elif text == "🎁 Redeem Code":
        await update.message.reply_text(
            "🎁 **Redeem Code**\n\nIf you have a voucher code or promotional token,"
            " please send it directly in chat or contact support to redeem it.",
            parse_mode="Markdown",
            reply_markup=main_menu,
        )
        return
    elif "₹" in text:
        try:
            prices = re.findall(r"₹(\d+)", text)
            if not prices:
                await update.message.reply_text(
                    "❌ Price processing failed. Please select a valid key amount."
                )
                return

            price_amount = str(prices[0])
            random_suffix = random.randint(1000, 9999)
            order_id = f"ORD{random_suffix}"

            active_checkout_sessions[user_id] = {
                "product": text,
                "price": price_amount,
                "order_id": order_id,
            }

            upi_payload = {
                "pa": str(MERCHANT_UPI_ID).strip(),
                "pn": str(MERCHANT_NAME).strip(),
                "am": price_amount,
                "cu": "INR",
                "tn": f"pay_ord{random_suffix}",
            }

            encoded_url = "upi://pay?" + urllib.parse.urlencode(
                upi_payload, quote_via=urllib.parse.quote
            )

            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(encoded_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            bio = io.BytesIO()
            bio.name = "upi_qr.png"
            img.save(bio, "PNG")
            bio.seek(0)

            checkout_caption = (
                f"💳 **Payment Checkout**\n\n💵 Amount: **₹{price_amount}**\n📦 Item:"
                f" `{text}`\n🧾 Order ID: `{order_id}`\n\n📱 **Scan the QR code above"
                " using any UPI app or tap the link below:**\n`"
                f"{encoded_url}`\n\nAlternatively, transfer to our merchant ID"
                f" manually:\n`{MERCHANT_UPI_ID}`"
            )

            await update.message.reply_photo(
                photo=bio, caption=checkout_caption, parse_mode="Markdown"
            )

            await update.message.reply_text(
                "🤖 **The cloud system is monitoring payments"
                " 24/7.**\n\nOnce completed, your license key will deliver right"
                " here instantly.",
                reply_markup=main_menu,
            )
        except Exception as e:
            logger.error(f"CRITICAL EXCEPTION IN CHECKOUT: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Configuration error. Please try again."
            )
        return

    elif text == "🆔 My ID":
        await update.message.reply_text(
            f"Your User ID is: `{user_id}`", parse_mode="Markdown"
        )
        return

    return


# ---------- CONCURRENT EXECUTION RUNNERS ----------
async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, buttons)
    )

    await application.initialize()
    await application.start()

    web_app = web.Application()
    web_app["tg_bot"] = application.bot
    
    web_app.router.add_get("/", index)
    web_app.router.add_post("/webhook", handle_notification_webhook)

    server_port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", server_port)

    logger.info(f"Starting web server target configuration on port: {server_port}")
    await site.start()

    logger.info(
        "Bot service setup successfully. Initiating polling loop handlers..."
    )

    await application.updater.start_polling()

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
