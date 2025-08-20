import os
import logging
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

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
    60: 1499
}

# Products list
PRODUCTS = [
    "mars loader",
    "kill loader",
    "bgmi loader",
    "bat loader"
]

# Product short names for commands
PRODUCT_SHORT_NAMES = {
    "mars": "mars loader",
    "kill": "kill loader",
    "bgmi": "bgmi loader",
    "bat": "bat loader"
}

# UPI details
UPI_ID = "xyz@upi"  # Replace with actual UPI ID

# Conversation states
SELECT_PRODUCT, SELECT_PLAN, PAYMENT_PROOF = range(3)

# Database connection pool
db_pool = None


async def init_db_pool():
    """Initialize the database connection pool and create tables if they don't exist."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        min_size=5,
        max_size=20
    )
    
    async with db_pool.acquire() as conn:
        # Create keys table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                duration_days INT NOT NULL,
                key_value STRING NOT NULL,
                is_used BOOL DEFAULT FALSE,
                added_at TIMESTAMP DEFAULT now()
            )
        """)
        
        # Check if product_name column exists in keys table
        column_exists = await conn.fetchval(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'keys' AND column_name = 'product_name'
            """
        )
        
        # Add product_name column if it doesn't exist
        if not column_exists:
            await conn.execute("""
                ALTER TABLE keys ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to keys table")
        
        # Create orders table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                user_id STRING NOT NULL,
                username STRING,
                duration_days INT NOT NULL,
                amount DECIMAL NOT NULL,
                status STRING DEFAULT 'pending', -- pending, approved, rejected
                key_assigned STRING,
                created_at TIMESTAMP DEFAULT now(),
                approved_at TIMESTAMP
            )
        """)
        
        # Check if product_name column exists in orders table
        column_exists = await conn.fetchval(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'orders' AND column_name = 'product_name'
            """
        )
        
        # Add product_name column if it doesn't exist
        if not column_exists:
            await conn.execute("""
                ALTER TABLE orders ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to orders table")
        
        # Create sales_history table if it doesn't exist
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
        
        # Check if product_name column exists in sales_history table
        column_exists = await conn.fetchval(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'sales_history' AND column_name = 'product_name'
            """
        )
        
        # Add product_name column if it doesn't exist
        if not column_exists:
            await conn.execute("""
                ALTER TABLE sales_history ADD COLUMN product_name STRING NOT NULL DEFAULT 'bgmi loader'
            """)
            logger.info("Added product_name column to sales_history table")
        
        logger.info("Database tables initialized successfully")


async def get_available_keys_count(product: str, duration: int) -> int:
    """Get available keys count for a specific product and duration."""
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM keys 
            WHERE duration_days = $1 AND product_name = $2 AND is_used = FALSE
            """,
            duration, product
        )
    return count


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    
    # Create inline keyboard with product options
    keyboard = []
    for i, product in enumerate(PRODUCTS, 1):
        keyboard.append([InlineKeyboardButton(f"{i}️⃣ {product.title()}", callback_data=f"product_{product}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 Welcome to BGMI Key Store 🔑\n\n"
        "Please select a product:",
        reply_markup=reply_markup
    )
    
    return SELECT_PRODUCT


async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle product selection."""
    query = update.callback_query
    await query.answer()
    
    # Extract product name from callback data
    product = query.data.split("_")[1]
    
    # Store selected product in context
    context.user_data["selected_product"] = product
    
    # Get available keys count for each duration for this product
    available_counts = {}
    tasks = []
    
    for days in DEFAULT_PLANS:
        task = asyncio.create_task(get_available_keys_count(product, days))
        tasks.append((days, task))
    
    # Wait for all tasks to complete
    for days, task in tasks:
        available_counts[days] = await task
    
    # Create inline keyboard with plan options and available counts
    keyboard = []
    for i, days in enumerate(DEFAULT_PLANS, 1):
        price = DEFAULT_PRICES[days]
        count = available_counts[days]
        status = "✅ Available" if count > 0 else "❌ Out of Stock"
        keyboard.append([InlineKeyboardButton(
            f"{i}️⃣ {days} Days - ₹{price} ({count} left) {status}", 
            callback_data=f"plan_{days}" if count > 0 else "no_stock"
        )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🛒 You selected: {product.title()}\n\n"
        "Choose your key duration:",
        reply_markup=reply_markup
    )
    
    return SELECT_PLAN


async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle plan selection."""
    query = update.callback_query
    await query.answer()
    
    # Extract plan duration from callback data
    if query.data == "no_stock":
        await query.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN
    
    duration = int(query.data.split("_")[1])
    price = DEFAULT_PRICES[duration]
    product = context.user_data.get("selected_product")
    
    # Check if keys are available for this product and duration
    available_count = await get_available_keys_count(product, duration)
    
    if available_count == 0:
        await query.answer("This plan is currently out of stock.", show_alert=True)
        return SELECT_PLAN
    
    # Store selected plan in context
    context.user_data["selected_plan"] = duration
    context.user_data["price"] = price
    
    # Send payment details
    await query.edit_message_text(
        f"🛒 You selected: {product.title()} - {duration} Days Key\n\n"
        f"💰 Price: ₹{price}\n\n"
        f"⚡ Pay via UPI: {UPI_ID}\n\n"
        f"📷 Scan QR below:"
    )
    
    # Send QR code image
    try:
        with open("qr.png", "rb") as qr_file:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=InputFile(qr_file)
            )
    except Exception as e:
        logger.error(f"Error sending QR code: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⚠️ Error loading QR code. Please proceed with the UPI payment."
        )
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="✅ After payment, send your screenshot or transaction ID here."
    )
    
    return PAYMENT_PROOF


async def payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle payment proof submission."""
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or user.first_name
    
    product = context.user_data.get("selected_product")
    duration = context.user_data.get("selected_plan")
    price = context.user_data.get("price")
    
    if not product or not duration or not price:
        await update.message.reply_text("⚠️ Session expired. Please start again with /start")
        return ConversationHandler.END
    
    # Create order in database
    order_id = str(uuid.uuid4())
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orders (id, user_id, username, product_name, duration_days, amount, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending')
            """,
            order_id, user_id, username, product, duration, price
        )
    
    # Forward payment proof to admins
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Forward the message (photo or text) to all admins
    for admin_id in ADMIN_IDS:
        try:
            if update.message.photo:
                # Forward photo
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
                    reply_markup=reply_markup
                )
            else:
                # Forward text
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
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"Error forwarding message to admin {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ Your payment proof has been submitted. Please wait for admin verification."
    )
    
    # Clear user data
    context.user_data.clear()
    
    return ConversationHandler.END


async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle order approval."""
    query = update.callback_query
    await query.answer()
    
    # Check if user is admin
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return
    
    # Extract order ID from callback data
    order_id = query.data.split("_")[1]
    
    async with db_pool.acquire() as conn:
        # Get order details
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1", order_id
        )
        
        if not order:
            await query.edit_message_text("⚠️ Order not found.")
            return
        
        if order["status"] != "pending":
            await query.edit_message_text(f"⚠️ This order is already {order['status']}.")
            return
        
        # Get an unused key for the selected duration and product
        key_record = await conn.fetchrow(
            """
            SELECT * FROM keys 
            WHERE duration_days = $1 AND product_name = $2 AND is_used = FALSE 
            LIMIT 1
            """,
            order["duration_days"], order["product_name"]
        )
        
        if not key_record:
            # No keys available
            await query.edit_message_text(
                f"⚠️ No keys available for {order['product_name']} - {order['duration_days']} days plan."
            )
            
            # Notify user
            await context.bot.send_message(
                chat_id=int(order["user_id"]),
                text="⚠️ Sorry, no keys available for your selected plan right now. Please contact support."
            )
            return
        
        # Update order status and assign key
        await conn.execute(
            """
            UPDATE orders 
            SET status = 'approved', key_assigned = $1, approved_at = now()
            WHERE id = $2
            """,
            key_record["key_value"], order_id
        )
        
        # Mark key as used
        await conn.execute(
            "UPDATE keys SET is_used = TRUE WHERE id = $1",
            key_record["id"]
        )
        
        # Add to sales history
        await conn.execute(
            """
            INSERT INTO sales_history (user_id, username, product_name, duration_days, amount, key_given)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            order["user_id"], order["username"], order["product_name"], 
            order["duration_days"], order["amount"], key_record["key_value"]
        )
    
    # Calculate expiry date
    expiry_date = datetime.now() + timedelta(days=order["duration_days"])
    expiry_str = expiry_date.strftime("%Y-%m-%d")
    
    # Send key to user
    await context.bot.send_message(
        chat_id=int(order["user_id"]),
        text=(
            f"✅ Payment Verified!\n\n"
            f"Here is your {order['product_name'].title()} - {order['duration_days']} Days Key:\n\n"
            f"👉 {key_record['key_value']}\n\n"
            f"📅 Expiry: {expiry_str}"
        )
    )
    
    # Update admin message
    await query.edit_message_text(
        f"✅ Order Approved!\n\n"
        f"User: @{order['username']} (id: {order['user_id']})\n"
        f"Product: {order['product_name'].title()}\n"
        f"Plan: {order['duration_days']} Days\n"
        f"Amount: ₹{order['amount']}\n"
        f"Key Assigned: {key_record['key_value']}"
    )


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle order rejection."""
    query = update.callback_query
    await query.answer()
    
    # Check if user is admin
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⚠️ You are not authorized to perform this action.")
        return
    
    # Extract order ID from callback data
    order_id = query.data.split("_")[1]
    
    async with db_pool.acquire() as conn:
        # Get order details
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1", order_id
        )
        
        if not order:
            await query.edit_message_text("⚠️ Order not found.")
            return
        
        if order["status"] != "pending":
            await query.edit_message_text(f"⚠️ This order is already {order['status']}.")
            return
        
        # Update order status
        await conn.execute(
            "UPDATE orders SET status = 'rejected' WHERE id = $1",
            order_id
        )
    
    # Notify user
    await context.bot.send_message(
        chat_id=int(order["user_id"]),
        text="❌ Payment not verified. Please try again or contact support."
    )
    
    # Update admin message
    await query.edit_message_text(
        f"❌ Order Rejected!\n\n"
        f"User: @{order['username']} (id: {order['user_id']})\n"
        f"Product: {order['product_name'].title()}\n"
        f"Plan: {order['duration_days']} Days\n"
        f"Amount: ₹{order['amount']}"
    )


async def add_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a new key to the database."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /add_key <days> <key> <product>\n\nAvailable products: mars, kill, bgmi, bat")
        return
    
    try:
        days = int(context.args[0])
        key = context.args[1]
        product_short = context.args[2].lower()
        
        # Validate duration
        if days not in DEFAULT_PLANS:
            await update.message.reply_text(f"⚠️ Invalid duration. Valid options: {', '.join(map(str, DEFAULT_PLANS))}")
            return
        
        # Validate product
        if product_short not in PRODUCT_SHORT_NAMES:
            await update.message.reply_text(f"⚠️ Invalid product. Valid options: {', '.join(PRODUCT_SHORT_NAMES.keys())}")
            return
        
        product_name = PRODUCT_SHORT_NAMES[product_short]
        
        async with db_pool.acquire() as conn:
            # Check if key already exists
            existing_key = await conn.fetchrow(
                "SELECT * FROM keys WHERE key_value = $1", key
            )
            
            if existing_key:
                await update.message.reply_text("⚠️ This key already exists in the database.")
                return
            
            # Add the key
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
    """List available keys count per duration and product."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    message = "🔑 Available Keys:\n\n"
    
    # Get all key counts concurrently
    tasks = []
    for product in PRODUCTS:
        for days in DEFAULT_PLANS:
            task = asyncio.create_task(get_available_keys_count(product, days))
            tasks.append((product, days, task))
    
    # Wait for all tasks to complete
    key_counts = {}
    for product, days, task in tasks:
        try:
            key_counts[(product, days)] = await task
        except Exception as e:
            logger.error(f"Error getting key count for {product} {days} days: {e}")
            key_counts[(product, days)] = 0
    
    # Build the message
    for product in PRODUCTS:
        message += f"📦 {product.title()}:\n"
        for days in DEFAULT_PLANS:
            count = key_counts[(product, days)]
            status = "✅" if count > 0 else "❌"
            message += f"  {status} {days} Days: {count} keys\n"
        message += "\n"
    
    await update.message.reply_text(message)


async def remove_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a key from the database."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /remove_key <days> <key> <product>\n\nAvailable products: mars, kill, bgmi, bat")
        return
    
    try:
        days = int(context.args[0])
        key = context.args[1]
        product_short = context.args[2].lower()
        
        # Validate product
        if product_short not in PRODUCT_SHORT_NAMES:
            await update.message.reply_text(f"⚠️ Invalid product. Valid options: {', '.join(PRODUCT_SHORT_NAMES.keys())}")
            return
        
        product_name = PRODUCT_SHORT_NAMES[product_short]
        
        async with db_pool.acquire() as conn:
            # Check if key exists and is unused
            key_record = await conn.fetchrow(
                """
                SELECT * FROM keys 
                WHERE duration_days = $1 AND key_value = $2 AND product_name = $3 AND is_used = FALSE
                """,
                days, key, product_name
            )
            
            if not key_record:
                await update.message.reply_text("⚠️ Key not found or already used.")
                return
            
            # Remove the key
            await conn.execute(
                "DELETE FROM keys WHERE id = $1",
                key_record["id"]
            )
        
        await update.message.reply_text(f"✅ Key removed successfully from {product_name.title()} - {days} days plan.")
    
    except ValueError:
        await update.message.reply_text("⚠️ Invalid duration. Please provide a valid number.")
    except Exception as e:
        logger.error(f"Error removing key: {e}")
        await update.message.reply_text("⚠️ An error occurred while removing the key.")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 10 sales with details."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    message = "📊 Recent Sales History:\n\n"
    
    async with db_pool.acquire() as conn:
        # Get last 10 sales
        sales = await conn.fetch(
            """
            SELECT * FROM sales_history 
            ORDER BY created_at DESC 
            LIMIT 10
            """
        )
        
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
    """Export full sales history as CSV."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return
    
    try:
        import csv
        import io
        
        # Create CSV file in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            "Date", "User ID", "Username", "Product", "Duration (Days)", 
            "Amount", "Key Given"
        ])
        
        async with db_pool.acquire() as conn:
            # Get all sales
            sales = await conn.fetch(
                """
                SELECT * FROM sales_history 
                ORDER BY created_at DESC
                """
            )
            
            # Write data
            for sale in sales:
                created_at = sale["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([
                    created_at, sale["user_id"], sale["username"],
                    sale["product_name"], sale["duration_days"], 
                    sale["amount"], sale["key_given"]
                ])
        
        # Reset file pointer
        output.seek(0)
        
        # Send CSV file
        await update.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(
                file=output.getvalue().encode(),
                filename="sales_history.csv"
            ),
            caption="📊 Sales History Export"
        )
    
    except Exception as e:
        logger.error(f"Error exporting history: {e}")
        await update.message.reply_text("⚠️ An error occurred while exporting the sales history.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # Initialize database
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db_pool())

    # Add conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PRODUCT: [CallbackQueryHandler(product_selected, pattern="^product_")],
            SELECT_PLAN: [CallbackQueryHandler(plan_selected, pattern="^plan_")],
            PAYMENT_PROOF: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, payment_proof)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    # Admin commands
    application.add_handler(CommandHandler("add_key", add_key))
    application.add_handler(CommandHandler("list_keys", list_keys))
    application.add_handler(CommandHandler("remove_key", remove_key))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("export_history", export_history))

    # Admin approval/rejection callbacks
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_"))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()
