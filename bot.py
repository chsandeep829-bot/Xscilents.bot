import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import urllib.parse
import aiohttp
from aiohttp import web
import qrcode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
TOKEN = "8979881938:AAEAcd8z64fDbJfwTvi6-Bw0eJCJa6M_RTY"

# GitHub Configuration (Set these in Render Environment Variables)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "your_github_pat_here")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "your-username/key-store-database")

# Uropay API Credentials (Replace with your actual Uropay keys if required)
URO_API_KEY = os.environ.get("URO_API_KEY", "your_uropay_api_key_here")
URO_INITIATE_URL = "https://api.uropay.com/v1/payment/initiate"  # Update URL if Uropay endpoint differs

# ---------- DATA STORAGE ----------
active_checkout_sessions = {}
used_utrs = set()
user_purchased_keys = {}


# ---------- PRODUCT TO GITHUB FILE MAPPING ----------
def get_file_path_for_product(product_name):
  product_name = product_name.upper()
  if "5 HOURS" in product_name:
    return "keys_5h.txt"
  elif "1 DAY" in product_name:
    return "keys_1d.txt"
  elif "3 DAYS" in product_name:
    return "keys_3d.txt"
  elif "7 DAYS" in product_name:
    return "keys_7d.txt"
  elif "30 DAYS" in product_name:
    return "keys_30d.txt"
  elif "FULL SEASON" in product_name:
    return "keys_season.txt"
  return None


# ---------- GITHUB HELPER FUNCTIONS ----------
async def fetch_keys_from_github(file_path):
  url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
  headers = {
      "Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
  }
  async with aiohttp.ClientSession() as session:
    try:
      async with session.get(url, headers=headers) as resp:
        if resp.status == 200:
          data = await resp.json()
          file_content = base64.b64decode(data["content"]).decode("utf-8")
          keys = [line.strip() for line in file_content.splitlines() if line.strip()]
          return keys, data.get("sha")
    except Exception as e:
      logger.error(f"Error fetching keys from GitHub ({file_path}): {e}")
  return [], None


async def remove_key_from_github(file_path, key_to_remove):
  keys, sha = await fetch_keys_from_github(file_path)
  if not sha or key_to_remove not in keys:
    return False

  keys.remove(key_to_remove)
  updated_content = "\n".join(keys) + ("\n" if keys else "")
  encoded_content = base64.b64encode(updated_content.encode("utf-8")).decode("utf-8")

  url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
  headers = {
      "Authorization": f"Bearer {GITHUB_TOKEN}",
      "Accept": "application/vnd.github+json",
  }
  payload = {
      "message": f"Auto-remove sold key: {key_to_remove}",
      "content": encoded_content,
      "sha": sha,
  }

  async with aiohttp.ClientSession() as session:
    try:
      async with session.put(url, headers=headers, json=payload) as resp:
        if resp.status in [200, 201]:
          logger.info(f"Successfully removed key {key_to_remove} from {file_path}.")
          return True
    except Exception as e:
      logger.error(f"Error updating GitHub keys file ({file_path}): {e}")
  return False


# ---------- MENUS ----------
main_menu = ReplyKeyboardMarkup(
    [
        ["🔑 Purchase Key", "📋 My Keys"],
        ["🎁 Redeem Code", "📖 How to Buy"],
        ["🆔 My ID", "🆘 Contact Support"],
    ],
    resize_keyboard=True,
)

brands_menu = ReplyKeyboardMarkup([["XSCILENT LOADER"], ["⬅️ Back"]], resize_keyboard=True)

xscilent_menu = ReplyKeyboardMarkup(
    [
        ["XSCILENT 5 HOURS - ₹40", "XSCILENT 1 DAY - ₹100"],
        ["XSCILENT 3 DAYS - ₹180", "XSCILENT 7 DAYS - ₹300"],
        ["XSCILENT 30 DAYS - ₹800", "XSCILENT FULL SEASON - ₹1200"],
        ["⬅️ Back to Brands"],
    ],
    resize_keyboard=True,
)


# ---------- WEB SERVER ROUTES FOR RENDER ----------
async def health_check(request):
  return web.Response(text="Bot is running and active on Render!", status=200)


async def handle_notification_webhook(request):
  try:
    data = await request.json()
    detected_utr = data.get("utr", data.get("transaction_id", data.get("payment_token", "")))
    detected_amount = data.get("amount", data.get("paid_amount", 0))

    if detected_utr and detected_amount:
      detected_utr = str(detected_utr)
      detected_amount = float(detected_amount)

      if detected_utr in used_utrs:
        return web.Response(text="Duplicate transaction ignored.", status=200)

      matched_user_id = None
      matched_session = None

      for user_id, session in list(active_checkout_sessions.items()):
        if abs(float(session["price"]) - detected_amount) < 0.01:
          matched_user_id = user_id
          matched_session = session
          break

      if matched_user_id and matched_session:
        file_path = get_file_path_for_product(matched_session["product"])
        if not file_path:
          return web.Response(text="Invalid product mapping.", status=400)

        keys, _ = await fetch_keys_from_github(file_path)
        if not keys:
          await request.app["tg_bot"].send_message(
              chat_id=matched_user_id,
              text="⚠️ **Payment Confirmed!** However, stock pool for this duration is empty. Contact support.",
          )
          return web.Response(text="Stock Empty fallback executed.", status=200)

        delivered_key = keys[0]
        success = await remove_key_from_github(file_path, delivered_key)
        if not success:
          return web.Response(text="Failed to update key repository.", status=500)

        used_utrs.add(detected_utr)
        active_checkout_sessions.pop(matched_user_id, None)

        if matched_user_id not in user_purchased_keys:
          user_purchased_keys[matched_user_id] = []
        user_purchased_keys[matched_user_id].append({
            "product": matched_session["product"],
            "key": delivered_key,
            "price": matched_session["price"],
        })

        await request.app["tg_bot"].send_message(
            chat_id=matched_user_id,
            text=(
                "✅ **Payment Received and Verified Automatically!**\n\n"
                f"📦 Product: `{matched_session['product']}`\n"
                f"🔑 Your Key:\n`{delivered_key}`"
            ),
            parse_mode="Markdown",
            reply_markup=main_menu,
        )
        return web.Response(text="Key Auto-Delivered successfully.", status=200)

    return web.Response(text="No matching active transaction found.", status=200)
  except Exception as e:
    logger.error(f"Error handling Webhook: {e}")
    return web.Response(text="Internal server error.", status=500)


# ---------- START COMMAND ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "👋 Welcome to Key Store", reply_markup=main_menu
  )


# ---------- DIRECT CLAIM CALLBACK ----------
async def claim_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  data = query.data

  if data.startswith("claim_"):
    order_id = data.split("_")[1]
    user_id = query.from_user.id

    session = active_checkout_sessions.get(user_id)
    if not session or session["order_id"] != order_id:
      await query.message.reply_text("❌ Checkout session expired or already completed.")
      return

    file_path = get_file_path_for_product(session["product"])
    if not file_path:
      await query.message.reply_text("❌ Invalid product mapping.")
      return

    keys, _ = await fetch_keys_from_github(file_path)
    if not keys:
      await query.message.reply_text("⚠️ Stock pool for this duration is empty. Please contact support @c_sandeep.")
      return

    delivered_key = keys[0]
    success = await remove_key_from_github(file_path, delivered_key)
    if not success:
      await query.message.reply_text("❌ Failed to update key repository. Please contact support.")
      return

    active_checkout_sessions.pop(user_id, None)

    if user_id not in user_purchased_keys:
      user_purchased_keys[user_id] = []
    user_purchased_keys[user_id].append({
        "product": session["product"],
        "key": delivered_key,
        "price": session["price"],
    })

    await query.message.edit_text(
        f"✅ **Payment Done & Verified!**\n\n"
        f"📦 Product: `{session['product']}`\n"
        f"🔑 Your Key:\n`{delivered_key}`",
        parse_mode="Markdown"
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
        "2️⃣ Select your desired loader brand and duration.\n"
        "3️⃣ Pay via UPI to `c.sandeep@superyes`.\n"
        "4️⃣ Click **'✅ I Have Paid - Get Code Instantly'** to get your key immediately! 🚀"
    )
    await update.message.reply_text(
        guide_text, parse_mode="Markdown", reply_markup=main_menu
    )
    return
  elif text == "🆘 Contact Support":
    support_text = (
        "🆘 **Customer Support**\n\nIf you are facing any issues, reach out:\n\n💬 Support Admin: @c_sandeep"
    )
    await update.message.reply_text(
        support_text, parse_mode="Markdown", reply_markup=main_menu
    )
    return
  elif text == "🎁 Redeem Code":
    await update.message.reply_text(
        "🎁 **Redeem Code**\n\nSend voucher code directly in chat to redeem.",
        parse_mode="Markdown",
        reply_markup=main_menu,
    )
    return
  elif "₹" in text:
    try:
      prices = re.findall(r"₹(\d+)", text)
      if not prices:
        await update.message.reply_text("❌ Price processing failed.")
        return

      base_price = int(prices[0])
      random_suffix = random.randint(1000, 9999)
      order_id = f"ORD{random_suffix}"

      payment_url = None
      payment_token = order_id

      async with aiohttp.ClientSession() as client:
        headers = {
            "Authorization": f"Bearer {URO_API_KEY}",
            "Accept": "application/json"
        }
        payload = {
            "amount": str(base_price),
            "currency": "INR",
            "order_id": order_id,
            "callback_url": "https://xscilents-bot.onrender.com/webhook"
        }
        try:
          async with client.post(URO_INITIATE_URL, headers=headers, json=payload, timeout=10) as resp:
            resp_text = await resp.text()
            logger.info(f"Uropay response status: {resp.status}, body: {resp_text}")
            if resp.status in [200, 201]:
              res_data = json.loads(resp_text)
              payment_url = res_data.get("payment_url") or res_data.get("url")
        except Exception as api_err:
          logger.error(f"Uropay API call exception: {api_err}", exc_info=True)

      # Fallback safe URL if API URL is missing or invalid
      if not payment_url or not payment_url.startswith(("http://", "https://")):
        payment_url = "https://t.me/c_sandeep"

      active_checkout_sessions[user_id] = {
          "product": text,
          "price": float(base_price),
          "order_id": order_id,
          "token": payment_token
      }

      # Generate UPI QR Code
      upi_qr_string = f"upi://pay?pa=c.sandeep@superyes&pn=Sandeep&am={base_price}&cu=INR&tn={order_id}"
      qr = qrcode.QRCode(version=1, box_size=10, border=4)
      qr.add_data(upi_qr_string)
      qr.make(fit=True)
      img = qr.make_image(fill_color="black", back_color="white")

      bio = io.BytesIO()
      bio.name = "gateway_qr.png"
      img.save(bio, "PNG")
      bio.seek(0)

      checkout_caption = (
          f"💳 **Payment Checkout (Uropay)**\n\n"
          f"💵 Amount: **₹{base_price}**\n"
          f"📦 Item: `{text}`\n"
          f"🎯 Pay To UPI: `c.sandeep@superyes`\n\n"
          f"⚠️ *Skip the gateway confirmation page to avoid errors.*\n"
          f"After completing your payment, click the button below to get your code instantly!"
      )

      keyboard = [
          [InlineKeyboardButton("🌐 Open Payment Page", url=payment_url)],
          [InlineKeyboardButton("✅ I Have Paid - Get Code Instantly", callback_data=f"claim_{order_id}")]
      ]
      reply_markup_inline = InlineKeyboardMarkup(keyboard)

      await update.message.reply_photo(
          photo=bio, caption=checkout_caption, parse_mode="Markdown", reply_markup=reply_markup_inline
      )
    except Exception as e:
      logger.error(f"CRITICAL EXCEPTION IN CHECKOUT: {e}", exc_info=True)
      await update.message.reply_text("❌ Configuration error. Please try again.")
    return

  elif text == "🆔 My ID":
    await update.message.reply_text(
        f"Your User ID is: `{user_id}`", parse_mode="Markdown"
    )
    return

  return


# ---------- CONCURRENT EXECUTION RUNNER ----------
async def main():
  application = Application.builder().token(TOKEN).build()

  application.add_handler(CommandHandler("start", start))
  application.add_handler(CallbackQueryHandler(claim_key_callback, pattern="^claim_"))
  application.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, buttons)
  )

  await application.initialize()
  await application.start()

  web_app = web.Application()
  web_app["tg_bot"] = application.bot
  web_app.router.add_get("/", health_check)
  web_app.router.add_post("/webhook", handle_notification_webhook)

  server_port = int(os.environ.get("PORT", 8080))
  runner = web.AppRunner(web_app)
  await runner.setup()
  site = web.TCPSite(runner, "0.0.0.0", server_port)

  logger.info(f"Starting web server configuration on port: {server_port}")
  await site.start()

  logger.info("Bot service setup successfully. Initiating polling loop...")
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
