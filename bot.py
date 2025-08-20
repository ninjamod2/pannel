import os
import logging
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

import asyncpg
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "26257")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]

# Price plans
DEFAULT_PLANS = [1, 3, 7, 15, 30, 60]
DEFAULT_PRICES = {
    1: 120,
    3: 299,
    7: 499,
    15: 699,
    30: 999,
    60: 1499,
}

# Conversation states
SELECT_PRODUCT, SELECT_PLAN, PAYMENT_PROOF = range(3)
# Admin states
ADMIN_ADD_PRODUCT_NAME = 100

# UPI details
UPI_ID = os.getenv("UPI_ID", "ninjagamerop0786@ybl")  # Replace with actual UPI ID

# Database connection pool
db_pool: Optional[asyncpg.Pool] = None

# Products will be loaded from DB
PRODUCTS: list[str] = []

# Optional product short names for CLI admin key ops (unchanged legacy)
PRODUCT_SHORT_NAMES = {
    "mars": "mars loader",
    "kill": "kill loader",
    "bgmi": "bgmi loader",
    "bat": "bat loader",
}


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="cancel")]])


def with_cancel_row(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows = list(rows)
    rows.append([InlineKeyboardButton("🚫 Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def init_db_pool():
    """Initialize DB pool and create tables."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        min_size=2,
        max_size=10,
    )

    async with db_pool.acquire() as conn:
        # keys
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            duration_days INT NOT NULL,
            key_value STRING NOT NULL,
            is_used BOOL DEFAULT FALSE,
            added_at TIMESTAMP DEFAULT now()
        )
        """)
        col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'keys' AND column_name = 'product_name'
        """)
        if not col:
            await conn.execute("""
            ALTER TABLE keys ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to keys")

        await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_key_value_unique ON keys (key_value)
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_keys_lookup ON keys (product_name, duration_days, is_used)
        """)

        # orders
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            user_id STRING NOT NULL,
            username STRING,
            duration_days INT NOT NULL,
            amount DECIMAL NOT NULL,
            status STRING DEFAULT 'pending',
            key_assigned STRING,
            created_at TIMESTAMP DEFAULT now(),
            approved_at TIMESTAMP
        )
        """)
        col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'orders' AND column_name = 'product_name'
        """)
        if not col:
            await conn.execute("""
            ALTER TABLE orders ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to orders")

        col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'orders' AND column_name = 'approved_by'
        """)
        if not col:
            await conn.execute("""
            ALTER TABLE orders ADD COLUMN approved_by STRING
            """)
            logger.info("Added approved_by column to orders")

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders (status, created_at)
        """)

        # sales_history
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_history (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            user_id STRING NOT NULL,
            username STRING,
            duration_days INT NOT NULL,
            amount DECIMAL NOT NULL,
            key_given STRING,
            created_at TIMESTAMP DEFAULT now()
        )
        """)
        col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'sales_history' AND column_name = 'product_name'
        """)
        if not col:
            await conn.execute("""
            ALTER TABLE sales_history ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to sales_history")

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sales_created ON sales_history (created_at)
        """)

        # products
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            name STRING PRIMARY KEY,
            is_active BOOL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT now()
        )
        """)
        cnt = await conn.fetchval("SELECT COUNT(*) FROM products")
        if (cnt or 0) == 0:
            defaults = ["mars loader", "kill loader", "bgmi loader", "bat loader"]
            for p in defaults:
                await conn.execute("INSERT INTO products (name) VALUES ($1)", p)
            logger.info("Seeded default products")

        logger.info("Database initialized")


async def load_products_from_db():
    global PRODUCTS
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM products WHERE is_active = TRUE ORDER BY name")
        PRODUCTS = [r["name"] for r in rows] or []
    logger.info(f"Loaded products: {PRODUCTS}")


async def get_available_keys_count(product: str, duration: int) -> int:
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM keys
            WHERE duration_days = $1 AND product_name = $2 AND is_used = FALSE
            """,
            duration, product
        )
        return int(count or 0)


# ========== USER FLOW ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not PRODUCTS:
        await update.message.reply_text("⚠️ No products available. Please try again later.")
        return ConversationHandler.END

    product_rows = []
    for i, product in enumerate(PRODUCTS, 1):
        product_rows.append([
            InlineKeyboardButton(f"{i}️⃣ {product.title()}", callback_data=f"product_{product}")
        ])
    reply_markup = with_cancel_row(product_rows)

    await update.message.reply_text(
        "👋 Welcome to BGMI Key Store 🔑\n\nPlease select a product:",
        reply_markup=reply_markup
    )
    return SELECT_PRODUCT


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    product = query.data.split("_", 1)[1]
    context.user_data["selected_product"] = product

    tasks = [asyncio.create_task(get_available_keys_count(product, d)) for d in DEFAULT_PLANS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = {d: (0 if isinstance(c, Exception) else c) for d, c in zip(DEFAULT_PLANS, results)}

    keyboard = []
    for i, days in enumerate(DEFAULT_PLANS, 1):
        price = DEFAULT_PRICES[days]
        count = counts[days]
        status = "✅ Available" if count > 0 else "❌ Out of Stock"
        cb = f"plan_{days}" if count > 0 else "no_stock"
        keyboard.append([InlineKeyboardButton(
            f"{i}️⃣ {days} Days - ₹{price} ({count} left) {status}",
            callback_data=cb
        )])
    reply_markup = with_cancel_row(keyboard)

    await query.edit_message_text(
        f"🛒 You selected: {product.title()}\n\nChoose your key duration:",
        reply_markup=reply_markup
    )
    return SELECT_PLAN


async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "no_stock":
        await query.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN

    duration = int(query.data.split("_", 1)[1])
    price = DEFAULT_PRICES[duration]
    product = context.user_data.get("selected_product")

    available_count = await get_available_keys_count(product, duration)
    if available_count == 0:
        await query.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN

    context.user_data["selected_plan"] = duration
    context.user_data["price"] = price

    await query.edit_message_text(
        f"🛒 You selected: {product.title()} - {duration} Days Key\n\n"
        f"💰 Price: ₹{price}\n\n"
        f"⚡ Pay via UPI: {UPI_ID}\n\n"
        f"📷 Scan QR below:",
        reply_markup=cancel_keyboard()
    )

    try:
        with open("qr.jpg", "rb") as qr_file:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=InputFile(qr_file),
                reply_markup=cancel_keyboard()
            )
    except Exception as e:
        logger.error(f"Error sending QR: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⚠️ Error loading QR code. Please proceed with the UPI payment.",
            reply_markup=cancel_keyboard()
        )

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="✅ After payment, send your screenshot or transaction ID here.",
        reply_markup=cancel_keyboard()
    )
    return PAYMENT_PROOF


async def payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or user.first_name
    product = context.user_data.get("selected_product")
    duration = context.user_data.get("selected_plan")
    price = context.user_data.get("price")

    if not product or not duration or not price:
        await update.message.reply_text("⚠️ Session expired. Please start again with /start", reply_markup=cancel_keyboard())
        return ConversationHandler.END

    order_id = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orders (id, user_id, username, product_name, duration_days, amount, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending')
            """,
            order_id, user_id, username, product, duration, price
        )

    admin_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}"),
    ]])

    for admin_id in ADMIN_IDS:
        try:
            if update.message.photo:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=update.message.photo[-1].file_id,
                    caption=(
                        f"🆕 New Order Request\n\n"
                        f"User: @{username} (id: {user_id})\n"
                        f"Product: {product.title()}\n"
                        f"Plan: {duration} Days\n"
                        f"Amount: ₹{price}\n"
                        f"Status: Pending\n"
                        f"Order ID: {order_id}"
                    ),
                    reply_markup=admin_keyboard
                )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🆕 New Order Request\n\n"
                        f"User: @{username} (id: {user_id})\n"
                        f"Product: {product.title()}\n"
                        f"Plan: {duration} Days\n"
                        f"Amount: ₹{price}\n"
                        f"Status: Pending\n"
                        f"Transaction ID: {update.message.text}\n"
                        f"Order ID: {order_id}"
                    ),
                    reply_markup=admin_keyboard
                )
        except Exception as e:
            logger.error(f"Forward to admin {admin_id} failed: {e}")

    await update.message.reply_text(
        "✅ Your payment proof has been submitted. Please wait for admin verification.",
        reply_markup=cancel_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return

    order_id = query.data.split("_", 1)[1]

    async with db_pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if not order:
            await query.edit_message_text("⚠️ Order not found.")
            return
        if order["status"] != "pending":
            await query.edit_message_text(f"⚠️ This order is already {order['status']}.")
            return

        async with conn.transaction():
            key_record = await conn.fetchrow(
                """
                SELECT * FROM keys
                WHERE duration_days = $1 AND product_name = $2 AND is_used = FALSE
                ORDER BY added_at
                LIMIT 1
                """,
                order["duration_days"], order["product_name"]
            )
            if not key_record:
                await query.edit_message_text(
                    f"⚠️ No keys available for {order['product_name']} - {order['duration_days']} days plan."
                )
                try:
                    await context.bot.send_message(
                        chat_id=int(order["user_id"]),
                        text="⚠️ Sorry, no keys available for your selected plan right now. Please contact support."
                    )
                except Exception:
                    pass
                return

            await conn.execute(
                "UPDATE keys SET is_used = TRUE WHERE id = $1",
                key_record["id"]
            )
            await conn.execute(
                """
                UPDATE orders
                SET status = 'approved', key_assigned = $1, approved_at = now(), approved_by = $2
                WHERE id = $3
                """,
                key_record["key_value"], str(query.from_user.id), order_id
            )
            await conn.execute(
                """
                INSERT INTO sales_history (user_id, username, product_name, duration_days, amount, key_given)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                order["user_id"], order["username"], order["product_name"],
                order["duration_days"], order["amount"], key_record["key_value"]
            )

    expiry_date = datetime.now() + timedelta(days=order["duration_days"])
    expiry_str = expiry_date.strftime("%Y-%m-%d")

    try:
        await context.bot.send_message(
            chat_id=int(order["user_id"]),
            text=(
                f"✅ Payment Verified!\n\n"
                f"Here is your {order['product_name'].title()} - {order['duration_days']} Days Key:\n\n"
                f"👉 {key_record['key_value']}\n\n"
                f"📅 Expiry: {expiry_str}"
            )
        )
    except Exception as e:
        logger.error(f"Send key to user failed: {e}")

    await query.edit_message_text(
        f"✅ Order Approved!\n\n"
        f"User: @{order['username']} (id: {order['user_id']})\n"
        f"Product: {order['product_name'].title()}\n"
        f"Plan: {order['duration_days']} Days\n"
        f"Amount: ₹{order['amount']}\n"
        f"Key Assigned: {key_record['key_value']}"
    )


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return

    order_id = query.data.split("_", 1)[1]

    async with db_pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if not order:
            await query.edit_message_text("⚠️ Order not found.")
            return
        if order["status"] != "pending":
            await query.edit_message_text(f"⚠️ This order is already {order['status']}.")
            return

        await conn.execute(
            "UPDATE orders SET status = 'rejected' WHERE id = $1",
            order_id
        )

    try:
        await context.bot.send_message(
            chat_id=int(order["user_id"]),
            text="❌ Payment not verified. Please try again or contact support."
        )
    except Exception:
        pass

    await query.edit_message_text(
        f"❌ Order Rejected!\n\n"
        f"User: @{order['username']} (id: {order['user_id']})\n"
        f"Product: {order['product_name'].title()}\n"
        f"Plan: {order['duration_days']} Days\n"
        f"Amount: ₹{order['amount']}"
    )


# ========== ADMIN COMMANDS (keys) ==========

async def add_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    if len(context.args) != 3:
        await update.message.reply_text("Usage: /add_key <days> <key> <product_short>\n\nAvailable products: mars, kill, bgmi, bat")
        return

    try:
        days = int(context.args[0])
        key = context.args[1]
        product_short = context.args.lower()

        if days not in DEFAULT_PLANS:
            await update.message.reply_text(f"⚠️ Invalid duration. Valid options: {', '.join(map(str, DEFAULT_PLANS))}")
            return

        if product_short not in PRODUCT_SHORT_NAMES:
            await update.message.reply_text(f"⚠️ Invalid product. Valid options: {', '.join(PRODUCT_SHORT_NAMES.keys())}")
            return

        product_name = PRODUCT_SHORT_NAMES[product_short]

        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT 1 FROM keys WHERE key_value = $1", key)
            if existing:
                await update.message.reply_text("⚠️ This key already exists in the database.")
                return

            await conn.execute(
                "INSERT INTO keys (duration_days, key_value, product_name) VALUES ($1, $2, $3)",
                days, key, product_name
            )

        await update.message.reply_text(f"✅ Key added successfully for {product_name.title()} - {days} days plan.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid duration. Please provide a valid number.")
    except Exception as e:
        logger.error(f"Error adding key: {e}")
        await update.message.reply_text("⚠️ An error occurred while adding the key.")


async def list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    message = "🔑 Available Keys:\n\n"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT product_name, duration_days, COUNT(*) AS cnt
            FROM keys
            WHERE is_used = FALSE
            GROUP BY product_name, duration_days
            ORDER BY product_name, duration_days
        """)
    counts = {(r["product_name"], r["duration_days"]): r["cnt"] for r in rows}

    await load_products_from_db()

    for product in PRODUCTS:
        message += f"📦 {product.title()}:\n"
        for days in DEFAULT_PLANS:
            count = counts.get((product, days), 0)
            status = "✅" if count > 0 else "❌"
            message += f"  {status} {days} Days: {count} keys\n"
        message += "\n"

    await update.message.reply_text(message)


async def remove_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    if len(context.args) != 3:
        await update.message.reply_text("Usage: /remove_key <days> <key> <product_short>\n\nAvailable products: mars, kill, bgmi, bat")
        return

    try:
        days = int(context.args[0])
        key = context.args[1]
        product_short = context.args.lower()

        if product_short not in PRODUCT_SHORT_NAMES:
            await update.message.reply_text(f"⚠️ Invalid product. Valid options: {', '.join(PRODUCT_SHORT_NAMES.keys())}")
            return

        product_name = PRODUCT_SHORT_NAMES[product_short]

        async with db_pool.acquire() as conn:
            rec = await conn.fetchrow(
                """
                SELECT * FROM keys
                WHERE duration_days = $1 AND key_value = $2 AND product_name = $3 AND is_used = FALSE
                """,
                days, key, product_name
            )
            if not rec:
                await update.message.reply_text("⚠️ Key not found or already used.")
                return

            await conn.execute("DELETE FROM keys WHERE id = $1", rec["id"])

        await update.message.reply_text(f"✅ Key removed successfully from {product_name.title()} - {days} days plan.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid duration. Please provide a valid number.")
    except Exception as e:
        logger.error(f"Error removing key: {e}")
        await update.message.reply_text("⚠️ An error occurred while removing the key.")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    message = "📊 Recent Sales History:\n\n"
    async with db_pool.acquire() as conn:
        sales = await conn.fetch("""
            SELECT * FROM sales_history
            ORDER BY created_at DESC
            LIMIT 10
        """)

    if not sales:
        message += "No sales history available."
    else:
        for sale in sales:
            created_at = sale["created_at"].strftime("%Y-%m-%d %H:%M")
            message += (
                f"📅 {created_at}\n"
                f"👤 User: @{sale['username']} (ID: {sale['user_id']})\n"
                f"🛒 Product: {sale['product_name'].title()}\n"
                f"🔑 Plan: {sale['duration_days']} Days\n"
                f"💰 Amount: ₹{sale['amount']}\n"
                f"🔑 Key: {sale['key_given']}\n\n"
            )

    await update.message.reply_text(message)


async def export_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    try:
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Date", "User ID", "Username", "Product", "Duration (Days)", "Amount", "Key Given"])

        async with db_pool.acquire() as conn:
            sales = await conn.fetch("""
                SELECT * FROM sales_history
                ORDER BY created_at DESC
            """)

        for sale in sales:
            created_at = sale["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([
                created_at, sale["user_id"], sale["username"],
                sale["product_name"], sale["duration_days"],
                sale["amount"], sale["key_given"]
            ])

        bio = io.BytesIO(output.getvalue().encode("utf-8"))
        bio.name = "sales_history.csv"
        await update.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            caption="📊 Sales History Export"
        )
    except Exception as e:
        logger.error(f"Error exporting history: {e}")
        await update.message.reply_text("⚠️ An error occurred while exporting the sales history.")


# ========== CANCEL HANDLERS ==========

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    try:
        await query.edit_message_text("Operation cancelled.")
    except Exception:
        try:
            await context.bot.send_message(chat_id=query.message.chat_id, text="Operation cancelled.")
        except Exception:
            pass
    return ConversationHandler.END


# ========== ADMIN PANEL: ADD/LIST PRODUCTS ==========

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add_product")],
        [InlineKeyboardButton("📃 List Products", callback_data="admin_list_products")],
        [InlineKeyboardButton("🚫 Close", callback_data="admin_close")],
    ])
    await update.message.reply_text("🛠 Admin Panel", reply_markup=kb)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return

    data = query.data
    if data == "admin_add_product":
        context.user_data["admin_add_product"] = {}
        await query.edit_message_text(
            "🆕 Send the new product name (e.g., 'xyz loader')",
            reply_markup=cancel_keyboard()
        )
        return ADMIN_ADD_PRODUCT_NAME

    elif data == "admin_list_products":
        await load_products_from_db()
        if not PRODUCTS:
            text = "No active products found."
        else:
            text = "Active Products:\n" + "\n".join(f"• {p.title()}" for p in PRODUCTS)
        await query.edit_message_text(text)
        return ConversationHandler.END

    elif data == "admin_close":
        await query.edit_message_text("Closed.")
        return ConversationHandler.END


async def admin_add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("⚠️ Please send a non-empty product name.", reply_markup=cancel_keyboard())
        return ADMIN_ADD_PRODUCT_NAME

    context.user_data["admin_add_product"]["name"] = name

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Add", callback_data="admin_confirm_add_product"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])
    await update.message.reply_text(f"Add product: {name}\nConfirm?", reply_markup=kb)
    return ADMIN_ADD_PRODUCT_NAME


async def admin_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return ConversationHandler.END

    if query.data != "admin_confirm_add_product":
        await query.edit_message_text("Unknown action.")
        return ConversationHandler.END

    data = context.user_data.get("admin_add_product", {})
    name = (data.get("name") or "").strip()
    if not name:
        await query.edit_message_text("⚠️ No product name found. Try again.")
        return ConversationHandler.END

    try:
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT * FROM products WHERE name = $1", name)
            if existing:
                if not existing["is_active"]:
                    await conn.execute("UPDATE products SET is_active=TRUE WHERE name=$1", name)
            else:
                await conn.execute("INSERT INTO products (name) VALUES ($1)", name)
    except Exception as e:
        logger.error(f"Add product error: {e}")
        await query.edit_message_text("⚠️ Failed to add product. Try a different name.")
        return ConversationHandler.END

    await load_products_from_db()
    await query.edit_message_text(f"✅ Product added: {name}")
    context.user_data.pop("admin_add_product", None)
    return ConversationHandler.END


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    application = Application.builder().token(BOT_TOKEN).build()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db_pool())
    loop.run_until_complete(load_products_from_db())

    # User conversation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PRODUCT: [
                CallbackQueryHandler(product_selected, pattern="^product_"),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
            SELECT_PLAN: [
                CallbackQueryHandler(plan_selected, pattern="^plan_"),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
            PAYMENT_PROOF: [
                MessageHandler(filters.PHOTO | (filters.TEXT & (~filters.COMMAND)), payment_proof),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)

    # Global cancel
    application.add_handler(CallbackQueryHandler(cancel_cb, pattern="^cancel$"))

    # Admin panel
    application.add_handler(CommandHandler("admin", admin_menu))

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_cb, pattern="^admin_add_product$")],
        states={
            ADMIN_ADD_PRODUCT_NAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), admin_add_product_name),
                CallbackQueryHandler(admin_confirm_cb, pattern="^admin_confirm_add_product$"),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_cb, pattern="^cancel$")],
        allow_reentry=True,
    )
    application.add_handler(admin_conv)

    # Generic admin callbacks (list/close)
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_list_products$"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_close$"))

    # Admin key/history commands
    application.add_handler(CommandHandler("add_key", add_key))
    application.add_handler(CommandHandler("list_keys", list_keys))
    application.add_handler(CommandHandler("remove_key", remove_key))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("export_history", export_history))

    # Admin order actions
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_"))

    application.run_polling()


if __name__ == "__main__":
    main()
