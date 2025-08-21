import os
import logging
import uuid
import asyncio
import re
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
ADMIN_IDS = [int(a) for a in os.getenv("ADMIN_IDS", "1240179115").split(",") if a]

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
ADMIN_ADD_PRODUCT_NAME, ADMIN_ADD_PRODUCT_SHORT = 100, 101

# UPI (defaults to your provided UPI if .env not set)
UPI_ID = os.getenv("UPI_ID", "ninjagamerop0786@ybl")

# DB pool
db_pool: Optional[asyncpg.Pool] = None

# In-memory products list (full names)
PRODUCTS: list[str] = []

# Short name validator
SHORT_RE = re.compile(r"^[a-z0-9_]{3,20}$")

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data="cancel")]])

def with_cancel_row(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows = list(rows)
    rows.append([InlineKeyboardButton("🚫 Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, 
        port=DB_PORT, 
        user=DB_USER, 
        password=DB_PASS, 
        database=DB_NAME,
        min_size=2, 
        max_size=10,
        command_timeout=60,  # Increased timeout
        max_inactive_connection_lifetime=300.0,  # 5 minutes
        max_queries=50000  # Recreate connection after 50000 queries
    )
    
    async with db_pool.acquire() as conn:
        # keys
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            duration_days INT NOT NULL,
            key_value STRING NOT NULL,
            is_used BOOL DEFAULT FALSE,
            added_at TIMESTAMP DEFAULT now(),
            product_name STRING NOT NULL DEFAULT 'bgmi loader'
        )
        """)
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_key_value_unique ON keys (key_value)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_lookup ON keys (product_name, duration_days, is_used)")
        
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
            approved_at TIMESTAMP,
            product_name STRING NOT NULL DEFAULT 'bgmi loader',
            approved_by STRING
        )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders (status, created_at)")
        
        # sales_history
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_history (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            user_id STRING NOT NULL,
            username STRING,
            duration_days INT NOT NULL,
            amount DECIMAL NOT NULL,
            key_given STRING,
            created_at TIMESTAMP DEFAULT now(),
            product_name STRING NOT NULL DEFAULT 'bgmi loader'
        )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_created ON sales_history (created_at)")
        
        # products (no default seed)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            name STRING PRIMARY KEY,
            short_name STRING UNIQUE,
            is_active BOOL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT now()
        )
        """)
        logger.info("Products table ready (no default seeding)")
        logger.info("Database initialized")

async def load_products_from_db():
    global PRODUCTS
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM products WHERE is_active=TRUE ORDER BY name")
        PRODUCTS = [r["name"] for r in rows] or []
    logger.info(f"Loaded products: {PRODUCTS}")

async def get_available_product_shorts() -> list[str]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT short_name FROM products
            WHERE is_active=TRUE AND short_name IS NOT NULL
            ORDER BY short_name
        """)
        return [r["short_name"] for r in rows]

async def get_full_name_by_short(short_name: str) -> Optional[str]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT name FROM products
            WHERE short_name=$1 AND is_active=TRUE
        """, short_name)
        return row["name"] if row else None

async def get_available_keys_count(product: str, duration: int) -> int:
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM keys
            WHERE duration_days=$1 AND product_name=$2 AND is_used=FALSE
        """, duration, product)
        return int(count or 0)

# ===== USER FLOW =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not PRODUCTS:
        await update.message.reply_text("⚠️ No products available. Please try again later.")
        return ConversationHandler.END
    
    rows = []
    for i, product in enumerate(PRODUCTS, 1):
        rows.append([InlineKeyboardButton(f"{i}️⃣ {product.title()}", callback_data=f"product_{product}")])
    
    await update.message.reply_text(
        "👋 Welcome to BGMI Key Store 🔑\n\nPlease select a product:",
        reply_markup=with_cancel_row(rows)
    )
    return SELECT_PRODUCT

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    product = q.data.split("_", 1)[1]
    context.user_data["selected_product"] = product
    
    tasks = [asyncio.create_task(get_available_keys_count(product, d)) for d in DEFAULT_PLANS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = {d: (0 if isinstance(c, Exception) else c) for d, c in zip(DEFAULT_PLANS, results)}
    
    kb = []
    for i, days in enumerate(DEFAULT_PLANS, 1):
        price = DEFAULT_PRICES[days]
        count = counts[days]
        status = "✅ Available" if count > 0 else "❌ Out of Stock"
        cb = f"plan_{days}" if count > 0 else "no_stock"
        kb.append([InlineKeyboardButton(f"{i}️⃣ {days} Days - ₹{price} ({count} left) {status}", callback_data=cb)])
    
    await q.edit_message_text(
        f"🛒 You selected: {product.title()}\n\nChoose your key duration:",
        reply_markup=with_cancel_row(kb)
    )
    return SELECT_PLAN

async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    
    if q.data == "no_stock":
        await q.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN
    
    duration = int(q.data.split("_", 1)[1])
    price = DEFAULT_PRICES[duration]
    product = context.user_data.get("selected_product")
    available = await get_available_keys_count(product, duration)
    
    if available == 0:
        await q.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN
    
    context.user_data["selected_plan"] = duration
    context.user_data["price"] = price
    
    caption_text = (
        f"🛒 You selected: {product.title()} - {duration} Days Key\n"
        f"💰 Price: ₹{price}\n"
        f"⚡️ Pay via UPI: {UPI_ID}\n"
        f"📷 Scan The QR Code:\n"
        f"👑 OWNER:- @NINJAGAMEROP"
    )
    
    try:
        await q.edit_message_text(
            f"🧾 Order Summary\n\nProduct: {product.title()}\nPlan: {duration} Days\nPrice: ₹{price}",
            reply_markup=cancel_keyboard()
        )
    except Exception:
        pass
    
    try:
        with open("qr.jpg", "rb") as f:
            await context.bot.send_photo(
                chat_id=q.message.chat_id,
                photo=InputFile(f),
                caption=caption_text,
                reply_markup=cancel_keyboard()
            )
    except Exception as e:
        logger.error(f"Error sending QR code: {e}")
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=caption_text,
            reply_markup=cancel_keyboard()
        )
    
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text="✅ After payment, reply here with your screenshot or transaction ID.",
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
        await conn.execute("""
            INSERT INTO orders (id, user_id, username, product_name, duration_days, amount, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending')
        """, order_id, user_id, username, product, duration, price)
    
    admin_kb = InlineKeyboardMarkup([[
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
                    reply_markup=admin_kb
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
                    reply_markup=admin_kb
                )
        except Exception as e:
            logger.error(f"Error forwarding to admin {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ Your payment proof has been submitted. Please wait for admin verification.",
        reply_markup=cancel_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    logger.info(f"approve clicked: data={q.data}, user={q.from_user.id}, admins={ADMIN_IDS}")
    
    order_id = q.data.split("_", 1)[1]
    order = None
    key_value = None
    
    try:
        if q.from_user.id not in ADMIN_IDS:
            await q.edit_message_text("⚠️ You are not authorized to perform this action.")
            return
        
        # Get a connection with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with db_pool.acquire() as conn:
                    # First, get the order details
                    order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
                    if not order:
                        await q.edit_message_text("⚠️ Order not found.")
                        return
                    if order["status"] != "pending":
                        await q.edit_message_text(f"⚠️ This order is already {order['status']}.")
                        return
                    
                    # Check if keys are available
                    kr = await conn.fetchrow("""
                        SELECT id, key_value FROM keys
                        WHERE duration_days=$1 AND product_name=$2 AND is_used=FALSE
                        ORDER BY added_at
                        LIMIT 1
                    """, order["duration_days"], order["product_name"])
                    
                    if not kr:
                        await q.edit_message_text(
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
                    
                    key_id = kr["id"]
                    key_value = kr["key_value"]
                    
                    # Now perform the transaction
                    async with conn.transaction():
                        # CockroachDB preview feature to avoid multiple active portals error
                        try:
                            await conn.execute("SET SESSION multiple_active_portals_enabled = true")
                        except Exception:
                            pass
                        
                        # Execute updates
                        await conn.execute("UPDATE keys SET is_used=TRUE WHERE id=$1", key_id)
                        await conn.execute("""
                            UPDATE orders
                            SET status='approved', key_assigned=$1, approved_at=now(), approved_by=$2
                            WHERE id=$3
                        """, key_value, str(q.from_user.id), order_id)
                        await conn.execute("""
                            INSERT INTO sales_history (user_id, username, product_name, duration_days, amount, key_given)
                            VALUES ($1, $2, $3, $4, $5, $6)
                        """, order["user_id"], order["username"], order["product_name"],
                             order["duration_days"], order["amount"], key_value)
                    
                    # If we got here, everything succeeded
                    break
                    
            except (asyncpg.exceptions.ConnectionDoesNotExistError, 
                    asyncpg.exceptions._base.InterfaceError,
                    asyncpg.exceptions.PostgresConnectionError) as e:
                logger.warning(f"Database connection error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                # Wait a bit before retrying
                await asyncio.sleep(1)
        
        if not order or not key_value:
            await q.edit_message_text("⚠️ Failed to process order. Please try again.")
            return
        
        expiry = (datetime.now() + timedelta(days=order["duration_days"])).strftime("%Y-%m-%d")
        try:
            await context.bot.send_message(
                chat_id=int(order["user_id"]),
                text=(
                    f"✅ Payment Verified!\n\n"
                    f"Here is your {order['product_name'].title()} - {order['duration_days']} Days Key:\n\n"
                    f"👉 {key_value}\n\n"
                    f"📅 Expiry: {expiry}"
                )
            )
        except Exception as e:
            logger.error(f"Send key to user failed: {e}")
        await q.edit_message_text(
            f"✅ Order Approved!\n\n"
            f"User: @{order['username']} (id: {order['user_id']})\n"
            f"Product: {order['product_name'].title()}\n"
            f"Plan: {order['duration_days']} Days\n"
            f"Amount: ₹{order['amount']}\n"
            f"Key Assigned: {key_value}"
        )
    except Exception:
        logger.exception("approve_order failed")
        try:
            await q.edit_message_text("⚠️ An error occurred while approving. Please check logs.")
        except Exception:
            pass

async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("⚠️ You are not authorized to perform this action.")
        return
    
    order_id = q.data.split("_", 1)[1]
    async with db_pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        if not order:
            await q.edit_message_text("⚠️ Order not found.")
            return
        if order["status"] != "pending":
            await q.edit_message_text(f"⚠️ This order is already {order['status']}.")
            return
        await conn.execute("UPDATE orders SET status='rejected' WHERE id=$1", order_id)
    
    try:
        await context.bot.send_message(
            chat_id=int(order["user_id"]),
            text="❌ Payment not verified. Please try again or contact support."
        )
    except Exception:
        pass
    await q.edit_message_text(
        f"❌ Order Rejected!\n\n"
        f"User: @{order['username']} (id: {order['user_id']})\n"
        f"Product: {order['product_name'].title()}\n"
        f"Plan: {order['duration_days']} Days\n"
        f"Amount: ₹{order['amount']}"
    )

# ===== ADMIN: KEYS =====
async def add_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    if len(context.args) != 3:
        shorts = await get_available_product_shorts()
        await update.message.reply_text(
            "Usage: /add_key <days> <key> <product_short>\n\n"
            "Available products: " + (", ".join(shorts) if shorts else "none")
        )
        return
    
    try:
        days_str, key_raw, short_raw = context.args
        days = int(days_str)
        key = key_raw.strip()
        product_short = short_raw.strip().lower()
        
        if days not in DEFAULT_PLANS:
            await update.message.reply_text(f"⚠️ Invalid duration. Valid options: {', '.join(map(str, DEFAULT_PLANS))}")
            return
        
        product_name = await get_full_name_by_short(product_short)
        if not product_name:
            shorts = await get_available_product_shorts()
            await update.message.reply_text(f"⚠️ Invalid product. Available: {', '.join(shorts) if shorts else 'none'}")
            return
        
        async with db_pool.acquire() as conn:
            exists = await conn.fetchrow("SELECT 1 FROM keys WHERE key_value=$1", key)
            if exists:
                await update.message.reply_text("⚠️ This key already exists in the database.")
                return
            await conn.execute(
                "INSERT INTO keys (duration_days, key_value, product_name) VALUES ($1, $2, $3)",
                days, key, product_name
            )
        
        await update.message.reply_text(f"✅ Key added successfully for {product_name.title()} - {days} days plan.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid duration. Please provide a valid number.")
    except Exception:
        logger.exception("__main__ - ERROR - Error adding key")
        await update.message.reply_text("⚠️ An error occurred while adding the key.")

async def list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    await load_products_from_db()
    message = "🔑 Available Keys:\n\n"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT product_name, duration_days, COUNT(*) AS cnt
            FROM keys
            WHERE is_used=FALSE
            GROUP BY product_name, duration_days
            ORDER BY product_name, duration_days
        """)
    
    counts = {(r["product_name"], r["duration_days"]): r["cnt"] for r in rows}
    for product in PRODUCTS:
        message += f"📦 {product.title()}:\n"
        for days in DEFAULT_PLANS:
            c = counts.get((product, days), 0)
            status = "✅" if c > 0 else "❌"
            message += f"  {status} {days} Days: {c} keys\n"
        message += "\n"
    
    await update.message.reply_text(message)

async def remove_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    if len(context.args) != 3:
        shorts = await get_available_product_shorts()
        await update.message.reply_text(
            "Usage: /remove_key <days> <key> <product_short>\n\n"
            "Available products: " + (", ".join(shorts) if shorts else "none")
        )
        return
    
    try:
        days = int(context.args[0])
        key = context.args[1].strip()
        product_short = context.args[2].strip().lower()  # Fixed: was context.args.strip()
        
        product_name = await get_full_name_by_short(product_short)
        if not product_name:
            shorts = await get_available_product_shorts()
            await update.message.reply_text(f"⚠️ Invalid product. Available: {', '.join(shorts) if shorts else 'none'}")
            return
        
        async with db_pool.acquire() as conn:
            rec = await conn.fetchrow("""
                SELECT * FROM keys
                WHERE duration_days=$1 AND key_value=$2 AND product_name=$3 AND is_used=FALSE
            """, days, key, product_name)
            if not rec:
                await update.message.reply_text("⚠️ Key not found or already used.")
                return
            await conn.execute("DELETE FROM keys WHERE id=$1", rec["id"])
        
        await update.message.reply_text(f"✅ Key removed successfully from {product_name.title()} - {days} days plan.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid duration. Please provide a valid number.")
    except Exception:
        logger.exception("Error removing key")
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
        for s in sales:
            created_at = s["created_at"].strftime("%Y-%m-%d %H:%M")
            message += (
                f"📅 {created_at}\n"
                f"👤 User: @{s['username']} (ID: {s['user_id']})\n"
                f"🛒 Product: {s['product_name'].title()}\n"
                f"🔑 Plan: {s['duration_days']} Days\n"
                f"💰 Amount: ₹{s['amount']}\n"
                f"🔑 Key: {s['key_given']}\n\n"
            )
    
    await update.message.reply_text(message)

async def export_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    try:
        import csv, io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Date", "User ID", "Username", "Product", "Duration (Days)", "Amount", "Key Given"])
        
        async with db_pool.acquire() as conn:
            sales = await conn.fetch("SELECT * FROM sales_history ORDER BY created_at DESC")
        
        for s in sales:
            created_at = s["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([created_at, s["user_id"], s["username"], s["product_name"], s["duration_days"], s["amount"], s["key_given"]])
        
        bio = io.BytesIO(output.getvalue().encode("utf-8"))
        bio.name = "sales_history.csv"
        await update.bot.send_document(chat_id=update.effective_chat.id, document=bio, caption="📊 Sales History Export")
    except Exception:
        logger.exception("Error exporting history")
        await update.message.reply_text("⚠️ An error occurred while exporting the sales history.")

# ===== CANCEL HANDLERS =====
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    try:
        await q.edit_message_text("Operation cancelled.")
    except Exception:
        try:
            await context.bot.send_message(chat_id=q.message.chat_id, text="Operation cancelled.")
        except Exception:
            pass
    return ConversationHandler.END

# ===== ADMIN PANEL: ADD/LIST/REMOVE PRODUCTS =====
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add_product")],
        [InlineKeyboardButton("📃 List Products", callback_data="admin_list_products")],
        [InlineKeyboardButton("🗑️ Remove Product", callback_data="admin_remove_product_menu")],
        [InlineKeyboardButton("🚫 Close", callback_data="admin_close")],
    ])
    
    await update.message.reply_text("🛠 Admin Panel", reply_markup=kb)

async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("⚠️ You are not authorized to perform this action.")
        return
    
    data = q.data
    # Add product flow
    if data == "admin_add_product":
        context.user_data["admin_add_product"] = {}
        await q.edit_message_text("🆕 Send the new product full name (e.g., 'xyz loader')", reply_markup=cancel_keyboard())
        return ADMIN_ADD_PRODUCT_NAME
    
    # List products
    elif data == "admin_list_products":
        await load_products_from_db()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, short_name FROM products WHERE is_active=TRUE ORDER BY name")
        
        if not rows:
            text = "No active products found."
        else:
            text = "Active Products:\n" + "\n".join(
                f"• {r['name'].title()} (/{r['short_name']})" if r["short_name"] 
                else f"• {r['name'].title()} (no short)" 
                for r in rows
            )
        await q.edit_message_text(text)
        return ConversationHandler.END
    
    # Remove Product: menu (supports items with/without short_name)
    elif data == "admin_remove_product_menu":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, short_name FROM products WHERE is_active=TRUE ORDER BY name")
        
        if not rows:
            await q.edit_message_text("No active products to remove.")
            return ConversationHandler.END
        
        buttons = []
        for r in rows:
            name = r["name"]
            short = r["short_name"]
            if short:
                cb = f"admin_remove_product_short::{short}"
                display = f"{name.title()} (/{short})"
            else:
                safe_name = name.replace("::", "—")
                cb = f"admin_remove_product_name::{safe_name}"
                display = f"{name.title()} (no short)"
            buttons.append([InlineKeyboardButton(f"🗑️ {display}", callback_data=cb)])
        
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data="admin_back"),
            InlineKeyboardButton("🚫 Close", callback_data="admin_close")
        ])
        await q.edit_message_text("Select a product to remove:", reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END
    
    # Remove using short_name
    elif data.startswith("admin_remove_product_short::"):
        short = data.split("::", 1)[1]
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name FROM products WHERE short_name=$1 AND is_active=TRUE", short)
        
        if not row:
            await q.edit_message_text("⚠️ Product not found or already removed.")
            return ConversationHandler.END
        
        name = row["name"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Remove", callback_data=f"admin_confirm_remove_short::{short}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_remove_product_menu"),
             InlineKeyboardButton("🚫 Close", callback_data="admin_close")],
        ])
        await q.edit_message_text(
            f"Remove product?\nName: {name}\nShort: /{short}\n\nThis will deactivate it (soft delete).",
            reply_markup=kb
        )
        return ConversationHandler.END
    
    # Remove using name (when short_name is null)
    elif data.startswith("admin_remove_product_name::"):
        safe_name = data.split("::", 1)[1]
        name = safe_name
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT short_name FROM products WHERE name=$1 AND is_active=TRUE", name)
        
        if not row:
            await q.edit_message_text("⚠️ Product not found or already removed.")
            return ConversationHandler.END
        
        short = row["short_name"]
        short_text = f"/{short}" if short else "(no short)"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Remove", callback_data=f"admin_confirm_remove_name::{name.replace('::','—')}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_remove_product_menu"),
             InlineKeyboardButton("🚫 Close", callback_data="admin_close")],
        ])
        await q.edit_message_text(
            f"Remove product?\nName: {name}\nShort: {short_text}\n\nThis will deactivate it (soft delete).",
            reply_markup=kb
        )
        return ConversationHandler.END
    
    # Confirm remove (short path)
    elif data.startswith("admin_confirm_remove_short::"):
        short = data.split("::", 1)[1]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE products SET is_active=FALSE WHERE short_name=$1 AND is_active=TRUE", short)
        await load_products_from_db()
        await q.edit_message_text(f"✅ Product deactivated: /{short}")
        return ConversationHandler.END
    
    # Confirm remove (name path)
    elif data.startswith("admin_confirm_remove_name::"):
        name = data.split("::", 1)[1]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE products SET is_active=FALSE WHERE name=$1 AND is_active=TRUE", name)
        await load_products_from_db()
        await q.edit_message_text(f"✅ Product deactivated: {name.title()}")
        return ConversationHandler.END
    
    elif data == "admin_back":
        await admin_menu(update, context)
        return ConversationHandler.END
    
    elif data == "admin_close":
        await q.edit_message_text("Closed.")
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
    await update.message.reply_text(
        "Send a short name (e.g., 'bgmi')\nRules: a-z, 0-9, underscore, length 3-20.",
        reply_markup=cancel_keyboard()
    )
    return ADMIN_ADD_PRODUCT_SHORT

async def admin_add_product_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return ConversationHandler.END
    
    short = (update.message.text or "").strip().lower()
    if not SHORT_RE.match(short):
        await update.message.reply_text("⚠️ Invalid short name. Use a-z, 0-9, underscore, 3-20 chars.", reply_markup=cancel_keyboard())
        return ADMIN_ADD_PRODUCT_SHORT
    
    context.user_data["admin_add_product"]["short_name"] = short
    data = context.user_data["admin_add_product"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Add", callback_data="admin_confirm_add_product"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])
    await update.message.reply_text(
        f"Add product:\nName: {data['name']}\nShort: {data['short_name']}\nConfirm?",
        reply_markup=kb
    )
    return ADMIN_ADD_PRODUCT_SHORT

async def admin_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("⚠️ You are not authorized to perform this action.")
        return ConversationHandler.END
    
    if q.data != "admin_confirm_add_product":
        await q.edit_message_text("Unknown action.")
        return ConversationHandler.END
    
    data = context.user_data.get("admin_add_product", {})
    name = (data.get("name") or "").strip()
    short = (data.get("short_name") or "").strip().lower()
    
    if not name or not short:
        await q.edit_message_text("⚠️ Missing name/short. Try again.")
        return ConversationHandler.END
    
    try:
        async with db_pool.acquire() as conn:
            clash = await conn.fetchrow("SELECT name FROM products WHERE short_name=$1 AND name<>$2", short, name)
            if clash:
                await q.edit_message_text("⚠️ This short name is already used by another product. Choose a different one.")
                return ConversationHandler.END
            
            existing = await conn.fetchrow("SELECT * FROM products WHERE name=$1", name)
            if existing:
                await conn.execute("UPDATE products SET short_name=$1, is_active=TRUE WHERE name=$2", short, name)
            else:
                await conn.execute("INSERT INTO products (name, short_name) VALUES ($1, $2)", name, short)
    except Exception as e:
        logger.error(f"Add product error: {e}")
        await q.edit_message_text("⚠️ Failed to add product. Try a different name/short.")
        return ConversationHandler.END
    
    await load_products_from_db()
    await q.edit_message_text(f"✅ Product added:\nName: {name}\nShort: {short}")
    context.user_data.pop("admin_add_product", None)
    return ConversationHandler.END

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db_pool())
    loop.run_until_complete(load_products_from_db())
    
    # Order action handlers FIRST (so they are not shadowed)
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_"))
    
    # Global cancel
    application.add_handler(CallbackQueryHandler(cancel_cb, pattern="^cancel$"))
    
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
    
    # Admin panel
    application.add_handler(CommandHandler("admin", admin_menu))
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_cb, pattern="^admin_add_product$")],
        states={
            ADMIN_ADD_PRODUCT_NAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), admin_add_product_name),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
            ADMIN_ADD_PRODUCT_SHORT: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), admin_add_product_short),
                CallbackQueryHandler(admin_confirm_cb, pattern="^admin_confirm_add_product$"),
                CallbackQueryHandler(cancel_cb, pattern="^cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_cb, pattern="^cancel$")],
        allow_reentry=True,
    )
    application.add_handler(admin_conv)
    
    # Admin callbacks for remove/list/close flows (explicit patterns)
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_list_products$"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_remove_product_menu$"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_remove_product_short::"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_remove_product_name::"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_confirm_remove_short::"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_confirm_remove_name::"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_back$"))
    application.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_close$"))
    
    # Admin key/history commands
    application.add_handler(CommandHandler("add_key", add_key))
    application.add_handler(CommandHandler("list_keys", list_keys))
    application.add_handler(CommandHandler("remove_key", remove_key))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("export_history", export_history))
    
    application.run_polling()

if __name__ == "__main__":
    main()
