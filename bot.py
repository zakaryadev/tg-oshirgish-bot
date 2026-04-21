import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    User,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# --- НАСТРОЙКИ ---
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
# Загружаем список админов через запятую
admin_raw = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", ""))
ADMIN_IDS = [int(i.strip()) for i in admin_raw.split(",") if i.strip()]

CARD_DETAILS_TEXT = (
    "Для оплаты переведите сумму на карту:\n"
    "`9860 1901 0336 8022`\n\n"
    "После оплаты отправьте фото чека сюда."
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# Таймер по умолчанию
current_default_timer = timedelta(days=30)

# --- FSM ---
class AdminStates(StatesGroup):
    waiting_custom_timer = State()
    waiting_search_query = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                kick_date TIMESTAMP,
                joined_at TIMESTAMP,
                custom_timer_seconds INTEGER,
                payment_status TEXT DEFAULT 'none',
                receipt_file_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_links (
                invite_link TEXT PRIMARY KEY,
                target_user_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor = await db.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "joined_at" not in cols: await db.execute("ALTER TABLE users ADD COLUMN joined_at TIMESTAMP")
        if "custom_timer_seconds" not in cols: await db.execute("ALTER TABLE users ADD COLUMN custom_timer_seconds INTEGER")
        if "payment_status" not in cols: await db.execute("ALTER TABLE users ADD COLUMN payment_status TEXT DEFAULT 'none'")
        if "receipt_file_id" not in cols: await db.execute("ALTER TABLE users ADD COLUMN receipt_file_id TEXT")
        if "name" not in cols: await db.execute("ALTER TABLE users ADD COLUMN name TEXT")
        if "username" not in cols: await db.execute("ALTER TABLE users ADD COLUMN username TEXT")
        await db.commit()

async def log_action(user_id: int, action: str, details: str = None):
    async with aiosqlite.connect("subscribers.db") as db:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("INSERT INTO logs (user_id, action, details, timestamp) VALUES (?, ?, ?, ?)", (user_id, action, details, ts))
        await db.commit()

async def save_user_info(user: User):
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"
    username = user.username
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("""
            INSERT INTO users (user_id, name, username) 
            VALUES (?, ?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET 
            name=excluded.name, username=excluded.username
        """, (user.id, name, username))
        await db.commit()

# --- ВСПОМОГАТЕЛЬНЫЕ ---
def parse_time_string(time_args: list[str]) -> timedelta:
    text = "".join(time_args).lower()
    days = sum(int(x) for x in re.findall(r'(\d+)d', text))
    hours = sum(int(x) for x in re.findall(r'(\d+)h', text))
    minutes = sum(int(x) for x in re.findall(r'(\d+)m', text))
    return timedelta(days=days, hours=hours, minutes=minutes)

def format_delta(td: timedelta) -> str:
    if not td: return "∞"
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days: parts.append(f"{days}д")
    if hours: parts.append(f"{hours}ч")
    if minutes: parts.append(f"{minutes}м")
    return " ".join(parts) if parts else "0м"

async def force_revoke_link(invite_link_url: str):
    try: await bot.revoke_chat_invite_link(chat_id=CHANNEL_ID, invite_link=invite_link_url)
    except Exception: pass

async def get_user_timer_delta(user_id: int) -> timedelta:
    async with aiosqlite.connect("subscribers.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT custom_timer_seconds FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
    if row and row["custom_timer_seconds"]: return timedelta(seconds=row["custom_timer_seconds"])
    return current_default_timer

# --- КЛАВИАТУРЫ ---
def build_admin_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="👥 Пользователи")], [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🆔 Мой ID")]], resize_keyboard=True)

def build_admin_receipt_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{user_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{user_id}")], [InlineKeyboardButton(text="⏱ Таймер доступа", callback_data=f"timer_menu:{user_id}")]])

TIMER_PRESETS = [("1 день", "1d"), ("3 дня", "3d"), ("7 дней", "7d"), ("15 дней", "15d"), ("30 дней", "30d"), ("✏️ Своё", "custom")]

def build_timer_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for label, code in TIMER_PRESETS:
        row.append(InlineKeyboardButton(text=label, callback_data=f"set_timer:{user_id}:{code}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"timer_back:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- ОБРАБОТЧИКИ ---
@dp.message(Command("start"))
async def start_command(message: Message):
    await save_user_info(message.from_user)
    if message.from_user.id in ADMIN_IDS:
        await message.answer("👋 Админ-панель активна.", reply_markup=build_admin_main_menu())
    else:
        await message.answer(CARD_DETAILS_TEXT, parse_mode="Markdown")
        await message.answer(f"Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")

@dp.message(F.text == "👥 Пользователи")
async def btn_users_handler(message: Message):
    if message.from_user.id in ADMIN_IDS: await _show_users_list(message)

@dp.message(F.text == "⚙️ Настройки")
async def btn_settings_handler(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer(f"⚙️ <b>Настройки</b>\n\nТаймер по умолчанию: <b>{format_delta(current_default_timer)}</b>\n\nДля смены: <code>/set_default_timer 30d</code>", parse_mode="HTML")

@dp.message(F.text == "🆔 Мой ID")
@dp.message(Command("myid"))
async def btn_id_handler(message: Message):
    await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.message(Command("set_default_timer"))
async def set_default_timer_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    global current_default_timer
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: <code>/set_default_timer 30d</code>", parse_mode="HTML")
        return
    new_td = parse_time_string(args)
    if new_td.total_seconds() < 60:
        await message.answer("Минимальное время - 1 минута.")
        return
    current_default_timer = new_td
    await message.answer(f"✅ Таймер по умолчанию изменен на: <b>{format_delta(new_td)}</b>", parse_mode="HTML")

# --- УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ---
async def _show_users_list(message, page: int = 0):
    limit = 10
    offset = page * limit
    async with aiosqlite.connect("subscribers.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE payment_status = 'approved'")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT user_id, name, username, kick_date FROM users WHERE payment_status = 'approved' ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset))
        rows = await cursor.fetchall()
    
    if not rows:
        text = "👥 Список пуст."
        kb = None
    else:
        text = f"👥 <b>Пользователи</b> ({page+1}/{max(1, (total+limit-1)//limit)})\nВсего: {total}"
        buttons = []
        for r in rows:
            name_part = r['name'] if r['name'] else str(r['user_id'])
            if r['username']:
                name_part += f" (@{r['username']})"
            buttons.append([InlineKeyboardButton(text=f"👤 {name_part} (до {str(r['kick_date']).split('.')[0]})", callback_data=f"manage_user:{r['user_id']}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"users_page:{page-1}"))
        if offset + limit < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"users_page:{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="🔍 Поиск", callback_data="search_users")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(message, CallbackQuery): await message.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else: await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("users_page:"))
async def users_page_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    await _show_users_list(callback, int(callback.data.split(":")[1]))
    await callback.answer()

@dp.callback_query(F.data == "search_users")
async def search_users_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.waiting_search_query)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="users_page:0")]])
    await callback.message.edit_text("🔍 <b>Введите ID, имя или username для поиска:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.message(AdminStates.waiting_search_query)
async def process_search_query(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    query = message.text.strip()
    await state.clear()
    
    async with aiosqlite.connect("subscribers.db") as db:
        db.row_factory = aiosqlite.Row
        search_term = f"%{query}%"
        if query.isdigit():
            cursor = await db.execute("SELECT * FROM users WHERE payment_status = 'approved' AND (user_id = ? OR name LIKE ? OR username LIKE ?) LIMIT 10", (int(query), search_term, search_term))
        else:
            cursor = await db.execute("SELECT * FROM users WHERE payment_status = 'approved' AND (name LIKE ? OR username LIKE ?) LIMIT 10", (search_term, search_term))
        rows = await cursor.fetchall()

    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад к списку", callback_data="users_page:0")]])
        await message.answer(f"По запросу «{query}» ничего не найдено.", reply_markup=kb)
        return

    text = f"🔍 <b>Результаты поиска по «{query}»</b>:\nНайдено: {len(rows)} (макс. 10)"
    buttons = []
    for r in rows:
        name_part = r['name'] if r['name'] else str(r['user_id'])
        if r['username']:
            name_part += f" (@{r['username']})"
        buttons.append([InlineKeyboardButton(text=f"👤 {name_part}", callback_data=f"manage_user:{r['user_id']}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад к списку", callback_data="users_page:0")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("manage_user:"))
async def manage_user_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    user_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect("subscribers.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        cursor = await db.execute("SELECT action, timestamp FROM logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5", (user_id,))
        logs = await cursor.fetchall()
    if not user: return
    log_txt = "\n".join([f"• <i>{l['timestamp']}</i>: {l['action']}" for l in logs])
    
    name_display = user['name'] if user['name'] else str(user_id)
    if user['username']:
        name_display += f" (@{user['username']})"
        
    text = f"👤 <b>Пользователь:</b> {name_display}\n🆔 <b>ID:</b> <code>{user_id}</code>\n📅 <b>Зашел:</b> {str(user['joined_at']).split('.')[0]}\n⏰ <b>До:</b> {str(user['kick_date']).split('.')[0]}\n\n📜 <b>Логи:</b>\n{log_txt}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Таймер", callback_data=f"timer_menu:{user_id}"),
         InlineKeyboardButton(text="🚫 Кик", callback_data=f"kick_confirm:{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="users_page:0")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data.startswith("kick_confirm:"))
async def kick_confirm_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = callback.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data=f"kick_exec:{uid}"), InlineKeyboardButton(text="❌ Нет", callback_data=f"manage_user:{uid}")]])
    await callback.message.edit_text(f"Удалить {uid}?", reply_markup=kb); await callback.answer()

@dp.callback_query(F.data.startswith("kick_exec:"))
async def kick_exec_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = int(callback.data.split(":")[1])
    try:
        await bot.ban_chat_member(CHANNEL_ID, uid); await bot.unban_chat_member(CHANNEL_ID, uid)
        async with aiosqlite.connect("subscribers.db") as db:
            await db.execute("UPDATE users SET kick_date = NULL, payment_status = 'kicked' WHERE user_id = ?", (uid,))
            await db.commit()
        await log_action(uid, "admin_kicked")
        await callback.answer("Удален", show_alert=True); await _show_users_list(callback, 0)
    except Exception as e: await callback.answer(f"Ошибка: {e}")

# --- ТАЙМЕРЫ ---
@dp.callback_query(F.data.startswith("timer_menu:"))
async def timer_menu_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=build_timer_keyboard(uid)); await callback.answer()

@dp.callback_query(F.data.startswith("timer_back:"))
async def timer_back_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=build_admin_receipt_keyboard(uid)); await callback.answer()

@dp.callback_query(F.data.startswith("set_timer:"))
async def set_timer_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    uid, code = int(callback.data.split(":")[1]), callback.data.split(":")[2]
    if code == "custom":
        await state.set_state(AdminStates.waiting_custom_timer); await state.update_data(uid=uid)
        await callback.message.answer(f"Введите время для {uid} (напр. 15d):"); await callback.answer(); return
    dt = parse_time_string([code])
    await _save_timer(uid, dt)
    await callback.answer(f"Установлено: {format_delta(dt)}", show_alert=True)

async def _save_timer(uid: int, td: timedelta):
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("INSERT INTO users (user_id, custom_timer_seconds) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET custom_timer_seconds=excluded.custom_timer_seconds", (uid, int(td.total_seconds())))
        await db.commit()

@dp.message(AdminStates.waiting_custom_timer)
async def custom_timer_msg(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    data = await state.get_data(); uid = data['uid']
    dt = parse_time_string(message.text.split())
    if dt.total_seconds() < 60: await message.answer("Мин. 1 мин."); return
    await _save_timer(uid, dt); await state.clear()
    await message.answer(f"✅ Для {uid} установлено: {format_delta(dt)}")

# --- ОДОБРЕНИЕ / ЧЕКИ ---
@dp.callback_query(F.data.startswith("approve:"))
async def approve_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = int(callback.data.split(":")[1])
    td = await get_user_timer_delta(uid)
    kd = datetime.now() + td
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("UPDATE users SET payment_status='approved', kick_date=? WHERE user_id=?", (kd, uid))
        await db.commit()
    await log_action(uid, "approved")
    try:
        link = await bot.create_chat_invite_link(CHANNEL_ID, expire_date=datetime.now()+timedelta(hours=24), member_limit=1)
        async with aiosqlite.connect("subscribers.db") as db:
            await db.execute("INSERT OR REPLACE INTO invite_links (invite_link, target_user_id, created_at) VALUES (?, ?, ?)", (link.invite_link, uid, datetime.now()))
            await db.commit()
        await bot.send_message(uid, f"✅ Оплата принята!\nДоступ на: {format_delta(td)}\n\n{link.invite_link}")
        await callback.answer("Одобрено"); await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e: await callback.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("reject:"))
async def reject_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    uid = int(callback.data.split(":")[1])
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("UPDATE users SET payment_status='rejected' WHERE user_id=?", (uid,))
        await db.commit()
    await log_action(uid, "rejected")
    try: await bot.send_message(uid, "❌ Чек отклонен."); await callback.answer("Отклонено"); await callback.message.edit_reply_markup(reply_markup=None)
    except: pass

@dp.message(F.photo | F.document)
async def receipt_handler(message: Message):
    if message.from_user.id in ADMIN_IDS: return
    await save_user_info(message.from_user)
    fid = message.photo[-1].file_id if message.photo else message.document.file_id
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("INSERT INTO users (user_id, payment_status, receipt_file_id) VALUES (?, 'pending', ?) ON CONFLICT(user_id) DO UPDATE SET payment_status='pending', receipt_file_id=excluded.receipt_file_id", (message.from_user.id, fid))
        await db.commit()
    await log_action(message.from_user.id, "sent_receipt")
    await message.answer("✅ Чек отправлен.")
    for aid in ADMIN_IDS:
        try: await bot.send_photo(aid, fid, caption=f"Чек от {message.from_user.id}", reply_markup=build_admin_receipt_keyboard(message.from_user.id))
        except: pass

# --- JOIN & JOBS ---
@dp.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> MEMBER))
async def on_join(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID: return
    uid = event.new_chat_member.user.id
    await save_user_info(event.new_chat_member.user)
    link = event.invite_link.invite_link if event.invite_link else None
    if link: await force_revoke_link(link)
    td = await get_user_timer_delta(uid)
    kd = datetime.now() + td
    async with aiosqlite.connect("subscribers.db") as db:
        await db.execute("UPDATE users SET kick_date=?, joined_at=?, payment_status='approved' WHERE user_id=?", (kd, datetime.now(), uid))
        await db.commit()
    await log_action(uid, "joined")

async def auto_kick():
    async with aiosqlite.connect("subscribers.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id FROM users WHERE kick_date <= ?", (datetime.now(),))
        for r in await cursor.fetchall():
            try:
                await bot.ban_chat_member(CHANNEL_ID, r['user_id']); await bot.unban_chat_member(CHANNEL_ID, r['user_id'])
                await db.execute("UPDATE users SET kick_date=NULL, payment_status='expired' WHERE user_id=?", (r['user_id'],))
                await db.commit(); await log_action(r['user_id'], "expired")
            except: pass

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    logging.info(f"Бот запущен. Админы: {ADMIN_IDS}")
    scheduler.add_job(auto_kick, 'interval', seconds=10)
    scheduler.start()
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__": asyncio.run(main())