import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import aiohttp
from aiohttp import web
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

# Uropay Official API Credentials
URO_API_KEY = os.environ.get("URO_API_KEY", "your_uropay_api_key_here")
URO_SECRET = os.environ.get("URO_SECRET", "your_uropay_secret_here")
URO_BASE_URL = "https://api.uropay.me"

# ---------- DATA STORAGE ----------
active_checkout_sessions = {}
user_purchased_keys = {}


# ---------- HELPER: HASH SECRET FOR AUTHORIZATION ----------
def get_hashed_secret(secret: str) -> str:
  return hashlib.sha512(secret.encode("utf-8")).hexdigest()


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


# ---------- WEBHOOK SIGNATURE VERIFICATION (UROPAY SPEC) ----------
FIXED_TAIL = ["uroPayOrderId", "merchantOrderId", "detectedAt", "environment"]


def build_transaction_payload(payload):
  fixed_set = set(FIXED_TAIL + ["event"])
  ordered = {}
  if "event" in payload:
    ordered["event"] = payload["event"]
  middle = sorted((k for k in payload if k not in fixed_set))
  for k in middle:
    ordered[k] = payload[k]
  for k in FIXED_TAIL:
    ordered[k] = payload.get(k)
  return ordered


def build_order_status_payload(payload):
  return {
      "event": payload["event"],
      "uroPayOrderId": payload["uroPayOrderId"],
      "merchantOrderId": payload["merchantOrderId"],
      "orderStatus": payload["orderStatus"],
      "submittedUTR": payload.get("submittedUTR"),
      "environment": payload["environment"],
  }


def build_utr_submitted_payload(payload):
  return {
      "event": payload["event"],
      "uroPayOrderId": payload["uroPayOrderId"],
      "merchantOrderId": payload["merchantOrderId"],
      "orderStatus": payload["orderStatus"],
      "submittedUTR": payload.get("submittedUTR"),
      "amount": payload["amount"],
      "customerName": payload["customerName"],
      "customerEmail": payload["customerEmail"],
      "customerVPA": payload.get("customerVPA"),
      "environment": payload["environment"],
      "utrSubmittedAt": payload.get("utrSubmittedAt"),
  }


def verify_webhook_signature(payload: dict, secret: str, signature: str) -> bool:
  if payload.get("event") == "order.status.utrsubmitted":
    ordered = build_utr_submitted_payload(payload)
  elif "orderStatus" in payload:
    ordered = build_order_status_payload(payload)
  else:
    ordered = build_transaction_payload(payload)

  hashed_secret = hashlib.sha512(secret.encode()).hexdigest()
  body = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
  computed = hmac.new(hashed_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
  return hmac.compare_digest(computed, signature)


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
    signature = request.headers.get("X-Uropay-Signature", "")

    if URO_SECRET and not verify_webhook_signature(data, URO_SECRET, signature):
      logger.warning("Invalid webhook signature received.")
      return web.Response(text="Unauthorized signature", status=401)

    uro_pay_order_id = data.get("uroPayOrderId")
    merchant_order_id = data.get("merchantOrderId")
    event = data.get("event", "")

    if event in ["companion.sms.data", "order.status.changed"] or data.get("orderStatus") == "COMPLETED":
      matched_user_id = None
      for user_id, session in list(active_checkout_sessions.items()):
        if session.get("merchantOrderId") == merchant_order_id or session.get("uroPayOrderId") == uro_pay_order_id:
          matched_user_id = user_id
          break

      if matched_user_id:
        session = active_checkout_sessions[matched_user_id]
        file_path = get_file_path_for_product(session["product"])
        if file_path:
          keys, _ = await fetch_keys_from_github(file_path)
          if keys:
            delivered_key = keys[0]
            success = await remove_key_from_github(file_path, delivered_key)
            if success:
              active_checkout_sessions.pop(matched_user_id, None)

              if matched_user_id not in user_purchased_keys:
                user_purchased_keys[matched_user_id] = []
              user_purchased_keys[matched_user_id].append({
                  "product": session["product"],
                  "key": delivered_key,
                  "price": session["price"],
              })

              await request.app["tg_bot"].send_message(
                  chat_id=matched_user_id,
                  text=(
                      "✅ **Payment Verified & Key Delivered Successfully!**\n\n"
                      f"📦 Product: `{session['product']}`\n"
                      f"🔑 Your Key:\n`{delivered_key}`"
                  ),
                  parse_mode="Markdown",
                  reply_markup=main_menu,
              )

    return web.Response(text="Webhook processed successfully", status=200)
  except Exception as e:
    logger.error(f"Error handling Webhook: {e}", exc_info=True)
    return web.Response(text="Internal server error", status=500)


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

  if user_id in active_checkout_sessions and active_checkout_sessions[user_id].get("waiting_for_utr"):
    if re.match(r"^\d{6,15}$", text.strip()):
      utr_number = text.strip()
      session = active_checkout_sessions[user_id]
      uro_pay_order_id = session["uroPayOrderId"]

      hashed_secret = get_hashed_secret(URO_SECRET)
      headers = {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-API-KEY": URO_API_KEY,
          "Authorization": f"Bearer {hashed_secret}"
      }
      payload = {
          "uroPayOrderId": uro_pay_order_id,
          "referenceNumber": utr_number
      }

      async with aiohttp.ClientSession() as client:
        try:
          async with client.patch(f"{URO_BASE_URL}/order/update", headers=headers, json=payload, timeout=10) as resp:
            if resp.status in [200, 201]:
              session["waiting_for_utr"] = False
              await update.message.reply_text(
                  "✅ **UTR Submitted Successfully!**\n\n"
                  "Verifying payment through Uropay companion app... Your key will be sent here automatically as soon as confirmed.",
                  parse_mode="Markdown",
                  reply_markup=main_menu
              )
              return
        except Exception as e:
          logger.error(f"Error updating Uropay order: {e}")

      await update.message.reply_text("❌ Failed to submit UTR to Uropay. Please check the UTR or contact support.")
      return

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
        "3️⃣ Scan the official QR code and pay via UPI.\n"
        "4️⃣ Send your **12-digit UTR / Reference Number** directly in chat when prompted to get your key instantly! 🚀"
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
      amount_in_paise = base_price * 100
      merchant_order_id = f"ORD{random.randint(10000, 99999)}"

      hashed_secret = get_hashed_secret(URO_SECRET)
      headers = {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-API-KEY": URO_API_KEY,
          "Authorization": f"Bearer {hashed_secret}"
      }
      payload = {
          "amount": amount_in_paise,
          "merchantOrderId": merchant_order_id,
          "customerName": update.effective_user.first_name or "Telegram User",
          "customerEmail": f"user_{user_id}@telegram.org",
          "transactionNote": f"Payment for {text}"
      }

      uro_pay_order_id = None
      qr_code_base64 = None

      async with aiohttp.ClientSession() as client:
        try:
          async with client.post(f"{URO_BASE_URL}/order/generate", headers=headers, json=payload, timeout=15) as resp:
            resp_text = await resp.text()
            logger.info(f"Uropay response status: {resp.status}, body: {resp_text}")
            if resp.status in [200, 201]:
              res_data = json.loads(resp_text)
              data = res_data.get("data", {})
              uro_pay_order_id = data.get("uroPayOrderId")
              qr_code_base64 = data.get("qrCode")
            else:
              await update.message.reply_text(f"❌ UroPay Error ({resp.status}): {resp_text}")
              return
        except Exception as api_err:
          logger.error(f"Uropay API call exception: {api_err}", exc_info=True)
          await update.message.reply_text(f"❌ API Exception: {str(api_err)}")
          return

      if not uro_pay_order_id or not qr_code_base64:
        await update.message.reply_text("❌ Failed to initiate Uropay order. Missing order ID or QR code in response.")
        return

      active_checkout_sessions[user_id] = {
          "product": text,
          "price": float(base_price),
          "merchantOrderId": merchant_order_id,
          "uroPayOrderId": uro_pay_order_id,
          "waiting_for_utr": True
      }

      if "," in qr_code_base64:
        qr_code_base64 = qr_code_base64.split(",")[1]
      img_bytes = base64.b64decode(qr_code_base64)

      bio = io.BytesIO(img_bytes)
      bio.name = "uro_qr.png"
      bio.seek(0)

      checkout_caption = (
          f"💳 **Payment Checkout (Uropay)**\n\n"
          f"💵 Amount: **₹{base_price}**\n"
          f"📦 Item: `{text}`\n\n"
          f"📲 Scan the QR code above using any UPI app to pay.\n"
          f"👉 **After payment, please reply directly in this chat with your 12-digit UTR / Reference Number.**"
      )

      await update.message.reply_photo(
          photo=bio, caption=checkout_caption, parse_mode="Markdown"
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
