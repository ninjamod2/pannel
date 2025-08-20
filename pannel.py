import os
from uuid import uuid4
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, relationship
from sqlalchemy import Enum as SAEnum
from enum import Enum

# ========= ENV & CONFIG =========
TOKEN = os.getenv("8073518014:AAF90nA5ns0pI307RvyHysXHJa8aLjk1CaA")
ADMIN_IDS = {int(x) for x in os.getenv("1240179115", "6662804820").split(",") if x}
UPI_ID = os.getenv("UPI_ID", "ninjagamerop0786@ybl")
QR_PATH = os.getenv("QR_PATH", "assets/qr.jpg")  # put your QR image here

if not TOKEN:
    raise RuntimeError("BOT_TOKEN missing in env")

Base = declarative_base()
engine = create_engine("sqlite:///bot.db", echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# ========= MODELS =========
class OrderStatus(str, Enum):
    pending = "pending"               # plan selected
    awaiting_approval = "awaiting_approval"  # user clicked "I've paid"
    paid = "paid"
    cancelled = "cancelled"

class KeyStatus(str, Enum):
    unassigned = "unassigned"
    assigned = "assigned"
    expired = "expired"
    revoked = "revoked"

class User(Base):
    __tablename__ = "users"
    tg_user_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

class Plan(Base):
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    days = Column(Integer, nullable=False)
    price_inr = Column(Integer, nullable=False)
    currency = Column(String, default="INR")
    product = relationship("Product")

class Key(Base):
    __tablename__ = "keys"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    key_value = Column(String, unique=True, nullable=False)
    duration_days = Column(Integer, nullable=False)
    assigned_to_user_id = Column(Integer, nullable=True)
    assigned_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    status = Column(SAEnum(KeyStatus), default=KeyStatus.unassigned)
    product = relationship("Product")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(Integer, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))
    plan_id = Column(Integer, ForeignKey("plans.id"))
    amount = Column(Integer, nullable=False)
    currency = Column(String, default="INR")
    status = Column(SAEnum(OrderStatus), default=OrderStatus.pending)
    payment_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    product = relationship("Product")
    plan = relationship("Plan")

Base.metadata.create_all(engine)

DEFAULT_PLANS = [1, 3, 7, 15, 30, 60]
DEFAULT_PRICES = {1:120, 3:299, 7:499, 15:699, 30:999, 60:1499}

# ========= HELPERS =========
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def ensure_user(update: Update):
    with SessionLocal() as db:
        u = db.get(User, update.effective_user.id)
        if not u:
            u = User(
                tg_user_id=update.effective_user.id,
                username=update.effective_user.username,
                is_admin=is_admin(update.effective_user.id)
            )
            db.add(u)
            db.commit()
    return True

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Products", callback_data="menu:products")],
        [InlineKeyboardButton("📦 My Orders", callback_data="menu:orders")],
        [InlineKeyboardButton("🛠️ Support", callback_data="menu:support")],
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Pending Orders", callback_data="admin:orders:page:1")],
        [InlineKeyboardButton("📦 Inventory", callback_data="admin:inventory")],
        [InlineKeyboardButton("➕ Seed Products", callback_data="admin:seed")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:home")],
    ])

def back_kb(to_cb: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=to_cb)]])

# ========= HANDLERS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if update.message:
        await update.message.reply_text("मुख्य मेन्यू:", reply_markup=main_menu_kb())
    else:
        await update.callback_query.edit_message_text("मुख्य मेन्यू:", reply_markup=main_menu_kb())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data

    if data == "menu:home":
        await q.edit_message_text("मुख्य मेन्यू:", reply_markup=main_menu_kb())
        return

    if data == "menu:products":
        with SessionLocal() as db:
            products = db.query(Product).all()
        if not products:
            await q.edit_message_text("Products उपलब्ध नहीं हैं. Admin पहले setup करे.", reply_markup=back_kb("menu:home"))
            return
        buttons = []
        for p in products:
            buttons.append([InlineKeyboardButton(p.name, callback_data=f"product:{p.id}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
        await q.edit_message_text("कृपया Product चुनें:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("product:"):
        pid = int(data.split(":")[1])
        with SessionLocal() as db:
            plans = db.query(Plan).filter(Plan.product_id==pid).order_by(Plan.days).all()
            p = db.get(Product, pid)
        if not plans:
            await q.edit_message_text("Plans सेट नहीं हैं.", reply_markup=back_kb("menu:products"))
            return
        buttons = []
        for pl in plans:
            label = f"{pl.days} दिन – ₹{pl.price_inr}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"plan:{pid}:{pl.id}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:products")])
        await q.edit_message_text(f"{p.name}\nअवधि/Plan चुनें:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("plan:"):
        _, pid, plan_id = data.split(":")
        pid = int(pid); plan_id = int(plan_id)
        with SessionLocal() as db:
            pl = db.get(Plan, plan_id)
            if not pl:
                await q.edit_message_text("Plan नहीं मिला.", reply_markup=back_kb(f"product:{pid}"))
                return
            # Create order
            o = Order(
                tg_user_id=uid, product_id=pid, plan_id=pl.id,
                amount=pl.price_inr, currency=pl.currency,
                status=OrderStatus.pending
            )
            db.add(o); db.commit()
            o.payment_ref = f"O#{o.id}"
            db.commit()

        # Show Pay screen with QR + monospace UPI ID
        pay_text = (
            f"Order {o.payment_ref}\n"
            f"Product: {pl.product.name}\n"
            f"Plan: {pl.days} दिन\n"
            f"Amount: ₹{pl.price_inr}\n\n"
            f"कृपया नीचे दिए गए UPI QR से भुगतान करें.\n"
            f"UPI ID: `{UPI_ID}`\n"
            f"Note/Message में लिखें: `{o.payment_ref}`\n\n"
            f"भुगतान के बाद नीचे वाला बटन दबाएँ."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("मैंने भुगतान कर दिया ✅", callback_data=f"paid:{o.id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"product:{pid}")]
        ])

        # Send QR as new message with caption, then edit original
        try:
            await q.message.reply_photo(
                photo=open(QR_PATH, "rb"),
                caption=pay_text,
                parse_mode="Markdown",
                reply_markup=kb
            )
            await q.edit_message_text("भुगतान निर्देश भेज दिए गए हैं।", reply_markup=back_kb(f"product:{pid}"))
        except FileNotFoundError:
            # Fallback if QR not found: just send text
            await q.edit_message_text(pay_text, parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("paid:"):
        oid = int(data.split(":")[1])
        with SessionLocal() as db:
            o = db.get(Order, oid)
            if not o or o.tg_user_id != uid:
                await q.edit_message_text("Order नहीं मिला.", reply_markup=back_kb("menu:orders"))
                return
            if o.status in [OrderStatus.paid, OrderStatus.cancelled]:
                await q.edit_message_text("यह order पहले ही process हो चुका है.", reply_markup=back_kb("menu:orders"))
                return
            o.status = OrderStatus.awaiting_approval
            db.commit()
            pl = db.get(Plan, o.plan_id)

        await q.edit_message_text(f"{o.payment_ref} approval के लिए भेज दिया गया है. कृपया प्रतीक्षा करें.", reply_markup=back_kb("menu:home"))

        # Notify both admins with Approve/Reject buttons
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{oid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{oid}")
            ],
            [InlineKeyboardButton("📄 Details", callback_data=f"admin:order:{oid}:detail")]
        ])
        notify_text = (
            f"Pending Approval\n"
            f"{o.payment_ref} | User: {o.tg_user_id}\n"
            f"Plan: {pl.days} दिन | Amount: ₹{pl.price_inr}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notify_text, reply_markup=admin_kb)
            except Exception:
                pass
        return

    # ======= ADMIN SECTION =======
    if data == "menu:orders":
        # User orders list
        with SessionLocal() as db:
            orders = db.query(Order).filter(Order.tg_user_id==uid).order_by(Order.created_at.desc()).limit(10).all()
        if not orders:
            await q.edit_message_text("आपके कोई recent orders नहीं हैं.", reply_markup=back_kb("menu:home"))
            return
        lines = []
        for o in orders:
            lines.append(f"{o.payment_ref} | ₹{o.amount} | {o.status} | {o.created_at.strftime('%Y-%m-%d %H:%M')}")
        await q.edit_message_text("Recent Orders:\n" + "\n".join(lines), reply_markup=back_kb("menu:home"))
        return

    if data == "menu:support":
        txt = (
            "सहायता चाहिए? यहाँ संपर्क करें:\n"
            "- इस चैट में अपना प्रश्न लिखें.\n"
            f"- भुगतान UPI: `{UPI_ID}`"
        )
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=back_kb("menu:home"))
        return

    if data == "admin:inventory":
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        with SessionLocal() as db:
            # Count unassigned keys by plan (days)
            rows = db.execute(
                "SELECT duration_days, COUNT(*) FROM keys WHERE status='unassigned' GROUP BY duration_days ORDER BY duration_days"
            ).fetchall()
        lines = ["Unassigned Keys:"]
        if rows:
            for d, cnt in rows:
                lines.append(f"{d} दिन: {cnt}")
        else:
            lines.append("कोई keys नहीं मिलीं.")
        await q.edit_message_text("\n".join(lines), reply_markup=admin_menu_kb())
        return

    if data == "admin:seed":
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        with SessionLocal() as db:
            if not db.query(Product).first():
                p = Product(name="Premium Access", description="Access to premium service via key.")
                db.add(p); db.commit()
                for d in DEFAULT_PLANS:
                    db.add(Plan(product_id=p.id, days=d, price_inr=DEFAULT_PRICES[d]))
                db.commit()
                await q.edit_message_text("Default product & plans seeded.", reply_markup=admin_menu_kb())
            else:
                await q.edit_message_text("Products पहले से मौजूद हैं.", reply_markup=admin_menu_kb())
        return

    if data.startswith("admin:orders:page:"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        page = int(data.split(":")[-1])
        page_size = 10
        offset = (page-1)*page_size
        with SessionLocal() as db:
            orders = db.query(Order).order_by(Order.created_at.desc()).offset(offset).limit(page_size).all()
        if not orders:
            await q.edit_message_text("No orders.", reply_markup=admin_menu_kb())
            return
        lines = []
        for o in orders:
            lines.append(f"{o.payment_ref} | user:{o.tg_user_id} | ₹{o.amount} | {o.status}")
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:orders:page:{page-1}"))
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin:orders:page:{page+1}"))
        kb = InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Back", callback_data="menu:home")]])
        await q.edit_message_text("\n".join(lines), reply_markup=kb)
        return

    if data.startswith("admin:order:") and data.endswith(":detail"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        oid = int(data.split(":")[2])
        with SessionLocal() as db:
            o = db.get(Order, oid)
            pl = db.get(Plan, o.plan_id) if o else None
        if not o:
            await q.edit_message_text("Order not found.", reply_markup=admin_menu_kb())
            return
        txt = (
            f"{o.payment_ref}\n"
            f"user: {o.tg_user_id}\n"
            f"status: {o.status}\n"
            f"amount: ₹{o.amount}\n"
            f"plan: {pl.days if pl else '-'} दिन\n"
            f"created: {o.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{oid}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{oid}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:orders:page:1")]
        ])
        await q.edit_message_text(txt, reply_markup=kb)
        return

    if data.startswith("admin:approve:"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        oid = int(data.split(":")[2])
        with SessionLocal() as db:
            o = db.get(Order, oid)
            if not o:
                await q.edit_message_text("Order not found.", reply_markup=admin_menu_kb()); return
            if o.status == OrderStatus.paid:
                await q.edit_message_text("Already approved.", reply_markup=admin_menu_kb()); return
            if o.status == OrderStatus.cancelled:
                await q.edit_message_text("Order cancelled.", reply_markup=admin_menu_kb()); return

            # Assign key (matching product & duration)
            pl = db.get(Plan, o.plan_id)
            # row lock emulate with immediate assign in sqlite (best-effort)
            k = db.query(Key).filter(
                Key.product_id==o.product_id,
                Key.duration_days==pl.days,
                Key.status==KeyStatus.unassigned
            ).first()
            if not k:
                await q.edit_message_text("No available key in inventory for this plan.", reply_markup=admin_menu_kb())
                return
            # mark and deliver
            k.status = KeyStatus.assigned
            k.assigned_to_user_id = o.tg_user_id
            k.assigned_at = datetime.utcnow()
            k.expires_at = k.assigned_at + timedelta(days=k.duration_days)
            o.status = OrderStatus.paid
            db.commit()

            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=o.tg_user_id,
                    text=(
                        f"आपका {o.payment_ref} approve हुआ.\n"
                        f"Key: {k.key_value}\n"
                        f"Valid till: {k.expires_at.date()}"
                    )
                )
            except Exception:
                pass

        await q.edit_message_text(f"Order {oid} approved and key delivered.", reply_markup=admin_menu_kb())
        return

    if data.startswith("admin:reject:"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        oid = int(data.split(":")[2])
        # Show predefined reasons
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("No payment found", callback_data=f"admin:reject_reason:{oid}:no_payment")],
            [InlineKeyboardButton("Wrong amount", callback_data=f"admin:reject_reason:{oid}:wrong_amount")],
            [InlineKeyboardButton("Invalid proof", callback_data=f"admin:reject_reason:{oid}:invalid_proof")],
            [InlineKeyboardButton("Timeout", callback_data=f"admin:reject_reason:{oid}:timeout")],
            [InlineKeyboardButton("Other (custom)", callback_data=f"admin:reject_custom:{oid}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:orders:page:1")]
        ])
        await q.edit_message_text(f"Reject Order {oid}: reason चुनें", reply_markup=kb)
        return

    if data.startswith("admin:reject_reason:"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        _, _, oid, reason_code = data.split(":")
        oid = int(oid)
        reasons_map = {
            "no_payment": "No payment found",
            "wrong_amount": "Wrong amount",
            "invalid_proof": "Invalid proof",
            "timeout": "Timeout",
        }
        reason = reasons_map.get(reason_code, "Rejected")
        with SessionLocal() as db:
            o = db.get(Order, oid)
            if not o:
                await q.edit_message_text("Order not found.", reply_markup=admin_menu_kb()); return
            if o.status in [OrderStatus.paid, OrderStatus.cancelled]:
                await q.edit_message_text("Order already processed.", reply_markup=admin_menu_kb()); return
            o.status = OrderStatus.cancelled
            db.commit()
        try:
            await context.bot.send_message(chat_id=o.tg_user_id, text=f"आपका {o.payment_ref} reject किया गया: {reason}")
        except Exception:
            pass
        await q.edit_message_text(f"Order {oid} rejected: {reason}", reply_markup=admin_menu_kb())
        return

    if data.startswith("admin:reject_custom:"):
        if not is_admin(uid):
            await q.edit_message_text("Unauthorized.", reply_markup=back_kb("menu:home"))
            return
        oid = int(data.split(":")[2])
        # Put admin into "expecting custom reason" state via user_data
        context.user_data["awaiting_custom_reject_for"] = oid
        await q.edit_message_text(f"Order {oid} reject reason टाइप करें (एक संदेश में).", reply_markup=back_kb("admin:orders:page:1"))
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle custom reject reason typed by admin
    uid = update.effective_user.id
    if context.user_data.get("awaiting_custom_reject_for") and is_admin(uid):
        oid = context.user_data.pop("awaiting_custom_reject_for")
        reason = update.message.text.strip()[:300]
        with SessionLocal() as db:
            o = db.get(Order, oid)
            if not o:
                await update.message.reply_text("Order not found."); return
            if o.status in [OrderStatus.paid, OrderStatus.cancelled]:
                await update.message.reply_text("Order already processed."); return
            o.status = OrderStatus.cancelled
            db.commit()
        try:
            await update.message.reply_text(f"Order {oid} rejected: {reason}")
            await update.get_bot().send_message(chat_id=o.tg_user_id, text=f"आपका {o.payment_ref} reject किया गया: {reason}")
        except Exception:
            pass
        return

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # open admin menu by command
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text("Admin Menu:", reply_markup=admin_menu_kb())

async def seed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    with SessionLocal() as db:
        if not db.query(Product).first():
            p = Product(name="Premium Access", description="Access to premium service via key.")
            db.add(p); db.commit()
            for d in DEFAULT_PLANS:
                db.add(Plan(product_id=p.id, days=d, price_inr=DEFAULT_PRICES[d], currency="INR"))
            db.commit()
            await update.message.reply_text("Default product & plans seeded.")
        else:
            await update.message.reply_text("Products पहले से मौजूद हैं.")

async def add_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /add_key <product_id> <days> <key_value>
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    try:
        _, product_id, days, key_value = update.message.text.split(" ", 3)
        product_id = int(product_id); days = int(days)
    except Exception:
        await update.message.reply_text("Usage: /add_key <product_id> <days> <key_value>")
        return
    with SessionLocal() as db:
        db.add(Key(product_id=product_id, key_value=key_value, duration_days=days))
        db.commit()
    await update.message.reply_text("Key added.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await update.message.reply_text("मुख्य मेन्यू:", reply_markup=main_menu_kb())

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("seed_products", seed_cmd))
    app.add_handler(CommandHandler("add_key", add_key_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
