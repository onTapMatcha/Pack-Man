# main.py

import json
import logging
import os
import re
import time
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "8015883196"))
MAIN_MENU_URL = os.getenv("MAIN_MENU_URL", "https://t.me/YourMainChannel")

# Optional backup channel ID
# Example: -1001234567890
BACKUP_CHANNEL_ID_RAW = os.getenv("BACKUP_CHANNEL_ID", "").strip()
BACKUP_CHANNEL_ID: Optional[int] = int(BACKUP_CHANNEL_ID_RAW) if BACKUP_CHANNEL_ID_RAW else None

from pathlib import Path
import os

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

PRODUCTS_FILE = DATA_DIR / "products.json"

# How long to keep recent forwarded/sent messages in memory
RECENT_CACHE_SECONDS = 60 * 30  # 30 minutes

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# STORAGE
# =========================================================

DEFAULT_PRODUCTS: Dict[str, Dict[str, Any]] = {}

WELCOME_TEXT = f"""
<b>Welcome</b>

Open a product link from the channel to view the saved product.

<a href="{escape(MAIN_MENU_URL, quote=True)}">Back to Menu</a>
""".strip()

ADMIN_HELP = """
<b>Admin Flow</b>

1. Post your product in the backup channel exactly how you want it
2. Forward that post or album to this bot
3. Reply to one of the forwarded messages with:

<code>/save slug</code>

Optional custom back link:
<code>/save slug | https://t.me/YourChannel</code>

You can also send media directly to the bot instead of using the backup channel.
If you do that, the bot will use the local copy it received.

<b>Commands</b>
<code>/menu</code>
<code>/listproducts</code>
<code>/deleteproduct slug</code>
<code>/preview slug</code>
<code>/help</code>
""".strip()


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
    if "admin_state" not in context.application.bot_data:
        context.application.bot_data["admin_state"] = {
            "recent_items": []
        }
    return context.application.bot_data["admin_state"]


def prune_recent_items(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_admin_state(context)
    now = time.time()
    state["recent_items"] = [
        item for item in state["recent_items"]
        if now - item["timestamp"] <= RECENT_CACHE_SECONDS
    ]


def safe_get_message_html(message) -> str:
    # Preserve caption/text formatting as closely as possible.
    if getattr(message, "caption", None):
        html = getattr(message, "caption_html_urled", None)
        if html:
            return html
        return escape(message.caption)

    if getattr(message, "text", None):
        html = getattr(message, "text_html_urled", None)
        if html:
            return html
        return escape(message.text)

    return ""


def guess_display_name_from_html(html_text: str, slug: str) -> str:
    # Basic HTML tag strip for first-line preview
    plain = re.sub(r"<[^>]+>", "", html_text or "").strip()
    if not plain:
        return slug

    first_line = plain.splitlines()[0].strip()
    return first_line[:80] if first_line else slug


def parse_save_command(text: str) -> Optional[Dict[str, str]]:
    """
    Accepted:
      /save slug
      /save slug | back_url
    """
    raw = text.strip()
    if not raw.lower().startswith("/save"):
        return None

    rest = raw[5:].strip()
    if not rest:
        return None

    parts = [p.strip() for p in rest.split("|")]
    slug = parts[0].lower().replace(" ", "").replace("/", "").replace("\\", "")

    if not slug:
        return None

    back_url = MAIN_MENU_URL
    if len(parts) >= 2 and parts[1]:
        back_url = parts[1]

    return {
        "slug": slug,
        "back_url": back_url,
    }


def extract_media_from_message(message) -> Optional[Dict[str, str]]:
    if not message:
        return None

    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}

    if message.video:
        return {"type": "video", "file_id": message.video.file_id}

    return None


def extract_forward_source(message) -> Dict[str, Any]:
    """
    If admin forwarded a channel post to the bot, try to capture the original source chat/message id.
    """
    result = {
        "source_chat_id": None,
        "source_message_id": None,
    }

    origin = getattr(message, "forward_origin", None)
    if not origin:
        return result

    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)

    if chat and getattr(chat, "id", None) is not None and message_id is not None:
        result["source_chat_id"] = chat.id
        result["source_message_id"] = message_id

    return result


def build_local_media_group(product: Dict[str, Any]) -> List[Any]:
    media_items = product.get("media", [])
    caption_html = product.get("content_html", "")
    output = []

    for index, item in enumerate(media_items):
        kwargs = {}
        if index == 0 and caption_html:
            kwargs["caption"] = caption_html
            kwargs["parse_mode"] = ParseMode.HTML

        if item["type"] == "photo":
            output.append(InputMediaPhoto(media=item["file_id"], **kwargs))
        elif item["type"] == "video":
            output.append(InputMediaVideo(media=item["file_id"], **kwargs))
        else:
            raise ValueError(f"Unsupported media type: {item['type']}")

    return output


def add_back_link_html(back_url: str) -> str:
    return f'<a href="{escape(back_url, quote=True)}">Back to Menu</a>'


def build_local_text_with_back_link(content_html: str, back_url: str) -> str:
    if content_html:
        return f"{content_html}\n\n{add_back_link_html(back_url)}"
    return add_back_link_html(back_url)


def record_recent_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    message = update.effective_message
    if not message:
        return

    media = extract_media_from_message(message)
    has_text = bool(getattr(message, "text", None))
    if not media and not has_text:
        return

    source = extract_forward_source(message)
    html_text = safe_get_message_html(message)

    state = get_admin_state(context)
    prune_recent_items(context)

    state["recent_items"].append({
        "timestamp": time.time(),
        "chat_id": update.effective_chat.id,
        "local_message_id": message.message_id,
        "media_group_id": message.media_group_id,
        "media": media,
        "html_text": html_text,
        "source_chat_id": source["source_chat_id"],
        "source_message_id": source["source_message_id"],
    })


def get_recent_item_by_message_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> Optional[Dict[str, Any]]:
    state = get_admin_state(context)
    prune_recent_items(context)

    for item in reversed(state["recent_items"]):
        if item["chat_id"] == chat_id and item["local_message_id"] == message_id:
            return item
    return None


def get_group_items_for_reference(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ref_item: Dict[str, Any],
) -> List[Dict[str, Any]]:
    media_group_id = ref_item.get("media_group_id")
    if not media_group_id:
        return [ref_item]

    state = get_admin_state(context)
    prune_recent_items(context)

    items = [
        item for item in state["recent_items"]
        if item["chat_id"] == chat_id and item.get("media_group_id") == media_group_id
    ]
    items.sort(key=lambda x: x["local_message_id"])
    return items


async def send_product(chat_id: int, context: ContextTypes.DEFAULT_TYPE, slug: str) -> None:
    products = get_products(context)
    product = products.get(slug)

    if not product:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>Product not found.</b>\n\n"
                f'{add_back_link_html(MAIN_MENU_URL)}'
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    back_url = product.get("back_url", MAIN_MENU_URL)

    # Preferred mode: copy directly from the original backup-channel/source messages
    source_chat_id = product.get("source_chat_id")
    source_message_ids = product.get("source_message_ids") or []

    if source_chat_id and source_message_ids:
        try:
            if len(source_message_ids) == 1:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_ids[0],
                )
            else:
                await context.bot.copy_messages(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_ids=sorted(source_message_ids),
                )

            # Optional separate menu link after the copied content
            await context.bot.send_message(
                chat_id=chat_id,
                text=add_back_link_html(back_url),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        except Exception as e:
            logger.exception("Source-copy failed for %s: %s", slug, e)

    # Fallback mode: rebuild from stored local file_ids/content
    media = product.get("media", [])
    content_html = product.get("content_html", "")

    if media:
        try:
            await context.bot.send_media_group(
                chat_id=chat_id,
                media=build_local_media_group(product),
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=add_back_link_html(back_url),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            logger.exception("Fallback media send failed for %s: %s", slug, e)

    # Final text fallback
    await context.bot.send_message(
        chat_id=chat_id,
        text=build_local_text_with_back_link(content_html, back_url),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update) and is_admin(update):
        await update.effective_message.reply_text(
            ADMIN_HELP,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_text("Use /menu or open a product deep link.")


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
            display_name = escape(str(product.get("display_name", slug)))
            lines.append(f'• <a href="{deep_link}">{display_name}</a>')

    lines.append("")
    lines.append(add_back_link_html(MAIN_MENU_URL))

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


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
                "Use this by replying to a forwarded or sent product message:\n\n"
                "/save slug\n\n"
                "Optional:\n"
                "/save slug | https://t.me/YourChannel"
            )
        )
        return

    if not message.reply_to_message:
        await message.reply_text(
            "Reply to one of the forwarded product messages with /save slug"
        )
        return

    reply = message.reply_to_message
    ref_item = get_recent_item_by_message_id(
        context,
        chat_id=update.effective_chat.id,
        message_id=reply.message_id,
    )

    if not ref_item:
        # Build best-effort single item from the replied message directly
        direct_media = extract_media_from_message(reply)
        direct_html = safe_get_message_html(reply)
        direct_source = extract_forward_source(reply)

        if not direct_media and not getattr(reply, "text", None):
            await message.reply_text(
                "I couldn't use that replied message. Reply to a product media/text message."
            )
            return

        ref_item = {
            "chat_id": update.effective_chat.id,
            "local_message_id": reply.message_id,
            "media_group_id": reply.media_group_id,
            "media": direct_media,
            "html_text": direct_html,
            "source_chat_id": direct_source["source_chat_id"],
            "source_message_id": direct_source["source_message_id"],
        }

    grouped_items = get_group_items_for_reference(
        context,
        chat_id=update.effective_chat.id,
        ref_item=ref_item,
    )

    slug = parsed["slug"]
    back_url = parsed["back_url"]

    # Preserve original caption/text exactly as forwarded/sent
    content_html = ""
    for item in grouped_items:
        if item.get("html_text"):
            content_html = item["html_text"]
            break

    local_media: List[Dict[str, str]] = []
    source_chat_ids = set()
    source_message_ids: List[int] = []

    for item in grouped_items:
        if item.get("media"):
            local_media.append(item["media"])

        if item.get("source_chat_id") is not None and item.get("source_message_id") is not None:
            source_chat_ids.add(item["source_chat_id"])
            source_message_ids.append(int(item["source_message_id"]))

    source_chat_id: Optional[int] = None
    if len(source_chat_ids) == 1 and len(source_message_ids) == len(grouped_items):
        source_chat_id = next(iter(source_chat_ids))
        source_message_ids = sorted(set(source_message_ids))
    else:
        source_message_ids = []

    display_name = guess_display_name_from_html(content_html, slug)

    products = get_products(context)
    products[slug] = {
        "display_name": display_name,
        "back_url": back_url,
        "content_html": content_html,
        "media": local_media,
        "source_chat_id": source_chat_id,
        "source_message_ids": source_message_ids,
    }
    save_products(products)

    bot_username = (await context.bot.get_me()).username

    source_note = "Uses backup/source copy mode." if source_chat_id and source_message_ids else "Uses local fallback mode."

    await message.reply_text(
        (
            f"Saved product: {slug}\n"
            f"{source_note}\n\n"
            f"Deep link:\n"
            f"https://t.me/{bot_username}?start={slug}"
        )
    )


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    message = update.effective_message
    if not message or not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Use: /preview slug")
        return

    slug = parts[1].strip().lower()
    await send_product(update.effective_chat.id, context, slug)


async def list_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    products = get_products(context)
    if not products:
        await update.effective_message.reply_text("No saved products.")
        return

    lines = ["Saved products:\n"]
    for slug, product in products.items():
        display_name = product.get("display_name", slug)
        mode = "source" if product.get("source_chat_id") and product.get("source_message_ids") else "local"
        lines.append(f"• {slug} -> {display_name} ({mode})")

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


# =========================================================
# MESSAGE CAPTURE
# =========================================================

async def capture_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update):
        return

    record_recent_item(update, context)

    message = update.effective_message
    if not message:
        return

    media = extract_media_from_message(message)
    if media or getattr(message, "text", None):
        # Keep this lightweight. No reply spam for every album item.
        pass


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set TELEGRAM_TOKEN in your environment or in the code.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("preview", preview_command))
    application.add_handler(CommandHandler("listproducts", list_products_command))
    application.add_handler(CommandHandler("deleteproduct", delete_product_command))

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.User(user_id=ADMIN_ID)
            & (filters.PHOTO | filters.VIDEO | filters.TEXT),
            capture_admin_content,
        )
    )

    print("Bot is running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
