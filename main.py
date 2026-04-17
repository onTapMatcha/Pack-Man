# main.py

import json
import logging
import os
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Your Telegram numeric user ID
ADMIN_ID = int(os.getenv("ADMIN_ID", "8015883196"))

# Main channel / menu link
MAIN_MENU_URL = os.getenv("MAIN_MENU_URL", "https://t.me/YourMainChannel")

# Where products get stored
PRODUCTS_FILE = Path("products.json")

# Delay used to collect all items from one incoming album
ALBUM_FLUSH_SECONDS = 1.5

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# DEFAULT STORAGE
# =========================================================

DEFAULT_PRODUCTS: Dict[str, Dict[str, Any]] = {}

WELCOME_TEXT = f"""
<b>Welcome</b>

Open a product link from the channel to view its media, price, and details.

<a href="{escape(MAIN_MENU_URL, quote=True)}">Back to Menu</a>
""".strip()

ADMIN_HELP = """
<b>Admin Commands</b>

1) Send or forward a product photo/video or a whole album to this bot
2) Then save it with:

<code>/save slug | Product Name | $Price | Description</code>

Example:
<code>/save bluecart | Blue Cart | $25 | Smooth pull\\nLimited stock</code>

Optional custom back link:
<code>/save bluecart | Blue Cart | $25 | Smooth pull\\nLimited stock | https://t.me/YourChannel</code>

Other commands:
<code>/previewdraft</code> - preview the current unsaved draft
<code>/listproducts</code> - list saved products
<code>/deleteproduct slug</code> - delete a saved product
<code>/menu</code> - show clickable product links
""".strip()


# =========================================================
# FILE STORAGE
# =========================================================

def load_products() -> Dict[str, Dict[str, Any]]:
    if not PRODUCTS_FILE.exists():
        save_products(DEFAULT_PRODUCTS)
        return dict(DEFAULT_PRODUCTS)

    try:
        with PRODUCTS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.exception("Failed loading products.json: %s", e)

    return dict(DEFAULT_PRODUCTS)


def save_products(products: Dict[str, Dict[str, Any]]) -> None:
    with PRODUCTS_FILE.open("w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)


# =========================================================
# HELPERS
# =========================================================

def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)


def get_products(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    if "products" not in context.application.bot_data:
        context.application.bot_data["products"] = load_products()
    return context.application.bot_data["products"]


def get_admin_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """
    Stores:
      current_draft: latest completed draft album/single media for admin
      pending_albums: temporary incoming album buffer
    """
    if "admin_state" not in context.application.bot_data:
        context.application.bot_data["admin_state"] = {
            "current_draft": None,
            "pending_albums": {},
        }
    return context.application.bot_data["admin_state"]


def build_caption(product: Dict[str, Any]) -> str:
    name = escape(product["name"])
    price = escape(product["price"])
    description = escape(product["description"])
    back_url = escape(product.get("back_url", MAIN_MENU_URL), quote=True)

    caption = (
        f"<b>{name}</b>\n"
        f"Price: {price}\n\n"
        f"{description}\n\n"
        f'<a href="{back_url}">Back to Menu</a>'
    )

    # Telegram media captions are limited in size.
    # Keep a little room for safety.
    if len(caption) > 1000:
        caption = caption[:980] + "..."

    return caption


def build_media_group(product: Dict[str, Any]) -> List[Any]:
    media_items = product.get("media", [])
    caption = build_caption(product)
    output = []

    for index, item in enumerate(media_items):
        media_type = item["type"]
        media_file = item["file_id"]

        kwargs = {}
        if index == 0:
            kwargs["caption"] = caption
            kwargs["parse_mode"] = ParseMode.HTML

        if media_type == "photo":
            output.append(InputMediaPhoto(media=media_file, **kwargs))
        elif media_type == "video":
            output.append(InputMediaVideo(media=media_file, **kwargs))
        else:
            raise ValueError(f"Unsupported media type: {media_type}")

    return output


def extract_media_from_message(update: Update) -> Optional[Dict[str, str]]:
    message = update.effective_message
    if not message:
        return None

    if message.photo:
        # Largest size is usually the last one
        return {"type": "photo", "file_id": message.photo[-1].file_id}

    if message.video:
        return {"type": "video", "file_id": message.video.file_id}

    return None


def parse_save_command(text: str) -> Optional[Dict[str, str]]:
    """
    Expected:
      /save slug | Product Name | $Price | Description
      /save slug | Product Name | $Price | Description | back_url
    """
    raw = text.strip()
    if not raw.lower().startswith("/save"):
        return None

    rest = raw[5:].strip()
    if not rest:
        return None

    parts = [p.strip() for p in rest.split("|")]
    if len(parts) < 4:
        return None

    slug = parts[0].lower().replace(" ", "").replace("/", "").replace("\\", "")
    name = parts[1]
    price = parts[2]
    description = parts[3]
    back_url = parts[4] if len(parts) >= 5 and parts[4] else MAIN_MENU_URL

    if not slug:
        return None

    return {
        "slug": slug,
        "name": name,
        "price": price,
        "description": description,
        "back_url": back_url,
    }


async def send_product(chat_id: int, context: ContextTypes.DEFAULT_TYPE, product_key: str) -> None:
    products = get_products(context)
    product = products.get(product_key)

    if not product:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>Product not found.</b>\n\n"
                f'<a href="{escape(MAIN_MENU_URL, quote=True)}">Back to Menu</a>'
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    media = product.get("media", [])
    if not media:
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_caption(product),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    try:
        await context.bot.send_media_group(
            chat_id=chat_id,
            media=build_media_group(product),
        )
    except Exception as e:
        logger.exception("Failed to send media group for %s: %s", product_key, e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_caption(product),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# =========================================================
# ALBUM COLLECTION
# =========================================================

async def flush_album_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    media_group_id = job_data["media_group_id"]

    admin_state = get_admin_state(context)
    pending_albums = admin_state["pending_albums"]
    draft = pending_albums.pop(media_group_id, None)

    if not draft:
        return

    items = draft.get("items", [])
    if not items:
        return

    admin_state["current_draft"] = {
        "media": items,
        "source_media_group_id": media_group_id,
    }

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"<b>Draft captured.</b>\n"
            f"Items: {len(items)}\n\n"
            f"Now send:\n"
            f"<code>/save slug | Product Name | $Price | Description</code>\n\n"
            f"Or preview it with:\n"
            f"<code>/previewdraft</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


async def capture_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    media_item = extract_media_from_message(update)
    if not media_item:
        return

    admin_state = get_admin_state(context)

    # Album
    if message.media_group_id:
        pending_albums = admin_state["pending_albums"]
        group_id = message.media_group_id

        if group_id not in pending_albums:
            pending_albums[group_id] = {"items": []}

        pending_albums[group_id]["items"].append(media_item)

        # Reset debounce timer for this album
        job_name = f"flush_album_{group_id}"
        current_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs:
            job.schedule_removal()

        context.job_queue.run_once(
            flush_album_job,
            when=ALBUM_FLUSH_SECONDS,
            name=job_name,
            data={"media_group_id": group_id},
        )
        return

    # Single media message
    admin_state["current_draft"] = {
        "media": [media_item],
        "source_media_group_id": None,
    }

    await message.reply_text(
        (
            "<b>Draft captured.</b>\n\n"
            "Now send:\n"
            "<code>/save slug | Product Name | $Price | Description</code>\n\n"
            "Or preview it with:\n"
            "<code>/previewdraft</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    payload = context.args[0].strip().lower() if context.args else ""

    if payload:
        await send_product(update.effective_chat.id, context, payload)
        return

    text = WELCOME_TEXT
    if is_private_chat(update) and is_admin(update):
        text += "\n\n" + ADMIN_HELP

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    bot_me = await context.bot.get_me()
    bot_username = bot_me.username
    products = get_products(context)

    lines = ["<b>Product Menu</b>\n"]

    if not products:
        lines.append("No products saved yet.")
    else:
        for slug, product in products.items():
            deep_link = f"https://t.me/{bot_username}?start={slug}"
            lines.append(f'• <a href="{deep_link}">{escape(product["name"])}</a>')

    lines.append("")
    lines.append(f'<a href="{escape(MAIN_MENU_URL, quote=True)}">Back to Main Page</a>')

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def preview_draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    admin_state = get_admin_state(context)
    draft = admin_state.get("current_draft")

    if not draft or not draft.get("media"):
        await update.effective_message.reply_text("No current draft.")
        return

    preview_product = {
        "name": "Draft Preview",
        "price": "$0",
        "description": "This is just the current unsaved draft.",
        "back_url": MAIN_MENU_URL,
        "media": draft["media"],
    }

    try:
        await context.bot.send_media_group(
            chat_id=update.effective_chat.id,
            media=build_media_group(preview_product),
        )
    except Exception as e:
        logger.exception("Preview draft failed: %s", e)
        await update.effective_message.reply_text("Failed to preview draft.")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    message = update.effective_message
    if not message or not message.text:
        return

    parsed = parse_save_command(message.text)
    if not parsed:
        await message.reply_text(
            (
                "Bad format.\n\n"
                "Use:\n"
                "/save slug | Product Name | $Price | Description\n\n"
                "Optional:\n"
                "/save slug | Product Name | $Price | Description | back_url"
            )
        )
        return

    admin_state = get_admin_state(context)
    draft = admin_state.get("current_draft")

    if not draft or not draft.get("media"):
        await message.reply_text(
            "No draft captured yet. Send or forward a photo/video or album to the bot first."
        )
        return

    products = get_products(context)
    slug = parsed["slug"]

    products[slug] = {
        "name": parsed["name"],
        "price": parsed["price"],
        "description": parsed["description"],
        "back_url": parsed["back_url"],
        "media": draft["media"],
    }

    save_products(products)

    await message.reply_text(
        (
            f"Saved product: {slug}\n\n"
            f"Deep link:\n"
            f"https://t.me/{(await context.bot.get_me()).username}?start={slug}"
        )
    )


async def list_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    products = get_products(context)
    if not products:
        await update.effective_message.reply_text("No saved products.")
        return

    lines = ["Saved products:\n"]
    for slug, product in products.items():
        lines.append(f"• {slug} -> {product['name']}")

    await update.effective_message.reply_text("\n".join(lines))


async def delete_product_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    message = update.effective_message
    if not message or not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Use: /deleteproduct slug")
        return

    slug = parts[1].strip().lower()
    products = get_products(context)

    if slug not in products:
        await message.reply_text("Product not found.")
        return

    del products[slug]
    save_products(products)
    await message.reply_text(f"Deleted product: {slug}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update) and is_admin(update):
        await update.effective_message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text("Use /menu or open a product deep link.")


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set TELEGRAM_TOKEN in your environment or in the code.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("previewdraft", preview_draft_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("listproducts", list_products_command))
    application.add_handler(CommandHandler("deleteproduct", delete_product_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_ID) & (filters.PHOTO | filters.VIDEO),
            capture_media,
        )
    )

    print("Bot is running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
