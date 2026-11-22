import logging
logging.basicConfig(level=logging.INFO)

import asyncio
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден BOT_TOKEN в файле .env")

# "or 0" спасает от пустой строки: в .env.example стоит "ADMIN_ID=" без значения
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
if not ADMIN_ID:
    logging.warning("ADMIN_ID не задан в .env: уведомления о заявках никуда не придут")

bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_FILE = "bot.db"


# ---------- БАЗА ДАННЫХ ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


        try:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'new'"
            )
        except sqlite3.OperationalError:
            pass  # колонка уже есть


        # стартовые данные, только если база пустая
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO categories (name) VALUES (?)",
                [("Торты",), ("Капкейки",), ("Наборы",)],
            )
            conn.executemany(
                "INSERT INTO products (category_id, name, description, price) "
                "VALUES (?, ?, ?, ?)",
                [
                    (1, "Медовик", "Классический медовик со сметанным кремом, 1 кг", 1800),
                    (1, "Красный бархат", "Бисквит с крем-чизом и ягодами, 1,5 кг", 2900),
                    (2, "Капкейки с вишней", "6 штук, шоколадное тесто и вишнёвая начинка", 1200),
                    (2, "Капкейки ванильные", "6 штук, ванильный крем и посыпка", 1000),
                    (3, "Сладкий бокс", "Капкейки, макаруны и зефир в подарочной коробке", 2200),
                    (3, "Набор макаронс", "12 штук, ассорти вкусов", 1500),
                ],
            )
    print("База данных готова")


def db_get_categories() -> list:
    with db() as conn:
        return conn.execute("SELECT id, name FROM categories ORDER BY id").fetchall()


def db_get_products(category_id: int) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT id, name, description, price, photo FROM products "
            "WHERE category_id = ? ORDER BY id",
            (category_id,),
        ).fetchall()


def db_get_product(product_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, category_id, name, description, price, photo FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()


def db_add_order(user_id: int, product_id: int, client_name: str, phone: str, comment: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders (user_id, product_id, client_name, phone, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, product_id, client_name, phone, comment,
             datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        return cur.lastrowid  # номер заявки


def db_get_user_orders(user_id: int) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT o.id, COALESCE(p.name, 'товар удалён'), o.created_at "
            "FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "WHERE o.user_id = ? AND o.status = 'new' ORDER BY o.id DESC",
            (user_id,),
        ).fetchall()


def db_get_order(order_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT o.id, o.user_id, o.status, o.client_name, o.phone, "
            "COALESCE(p.name, 'товар удалён') "
            "FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "WHERE o.id = ?",
            (order_id,),
        ).fetchone()


def db_cancel_order(order_id: int):
    with db() as conn:
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))


def db_add_product(category_id: int, name: str, description: str, price: int, photo) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO products (category_id, name, description, price, photo) "
            "VALUES (?, ?, ?, ?, ?)",
            (category_id, name, description, price, photo),
        )
        return cur.lastrowid


def db_set_product_photo(product_id: int, photo: str):
    with db() as conn:
        conn.execute("UPDATE products SET photo = ? WHERE id = ?", (photo, product_id))


def db_delete_product(product_id: int):
    with db() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


def db_get_active_orders() -> list:
    with db() as conn:
        return conn.execute(
            "SELECT o.id, COALESCE(p.name, 'товар удалён'), o.client_name "
            "FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "WHERE o.status = 'new' ORDER BY o.id DESC LIMIT 20"
        ).fetchall()


def db_get_order_full(order_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT o.id, o.user_id, o.status, o.client_name, o.phone, o.comment, "
            "o.created_at, COALESCE(p.name, 'товар удалён'), COALESCE(p.price, 0) "
            "FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "WHERE o.id = ?",
            (order_id,),
        ).fetchone()


def db_set_order_status(order_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


# ---------- КАРТОЧКИ И КНОПКИ ----------
def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " ₽"   # 1800 -> "1 800 ₽"


def card_text(name: str, description: str, price: int) -> str:
    return f"🍰 {name}\n\n{description}\n\nЦена: {format_price(price)}"


def kb_categories():
    rows = []
    for cid, name in db_get_categories():
        rows.append([InlineKeyboardButton(text=name, callback_data=f"cat:{cid}")])
    rows.append([InlineKeyboardButton(text="📋 Мои заявки", callback_data="myorders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_card(category_id: int, index: int, total: int, product_id: int):
    rows = []
    if total > 1:
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data=f"prod:{category_id}:{index - 1}"),
            InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"prod:{category_id}:{index + 1}"),
        ])
    rows.append([InlineKeyboardButton(text="🛒 Хочу заказать", callback_data=f"order:{product_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


BANNER_FILE = "banner.jpg"
NO_PHOTO_FILE = "no_photo.jpg"
_file_ids = {}   # сюда запоминаем номера картинок после первой отправки


def pic(path: str):
    # уже отправляли -> берём номер, иначе грузим файл с компьютера
    return _file_ids.get(path) or FSInputFile(path)


def remember(path: str, sent):
    if path not in _file_ids and getattr(sent, "photo", None):
        _file_ids[path] = sent.photo[-1].file_id


async def show_photo_screen(callback: CallbackQuery, photo: str, caption: str, kb):
    # photo — номер фото товара из базы ИЛИ имя файла (баннер, заглушка)
    is_file = photo in (BANNER_FILE, NO_PHOTO_FILE)
    media = pic(photo) if is_file else photo
    msg = callback.message

    if msg.photo is not None:
        # было фото -> стало фото: меняем на месте, чат не дёргается
        try:
            sent = await msg.edit_media(
                InputMediaPhoto(media=media, caption=caption), reply_markup=kb
            )
            if is_file:
                remember(photo, sent)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return   # на экране и так то же самое
        except Exception:
            pass

    # старое сообщение было текстом: удаляем и присылаем с картинкой (один раз)
    try:
        await msg.delete()
    except Exception:
        pass
    sent = await msg.answer_photo(media, caption=caption, reply_markup=kb)
    if is_file:
        remember(photo, sent)


async def send_main_menu(message: types.Message, text: str):
    # главное меню: баннер + кнопки категорий
    sent = await message.answer_photo(pic(BANNER_FILE), caption=text, reply_markup=kb_categories())
    remember(BANNER_FILE, sent)


async def show_card(callback: CallbackQuery, category_id: int, index: int):
    products = db_get_products(category_id)
    if not products:
        await callback.answer("В этой категории пока нет товаров", show_alert=True)
        return
    index = index % len(products)   # листание по кругу
    product_id, name, description, price, photo = products[index]
    await show_photo_screen(
        callback,
        photo or NO_PHOTO_FILE,     # нет фото -> заглушка
        card_text(name, description, price),
        kb_card(category_id, index, len(products), product_id),
    )
    await callback.answer()


# ---------- ОБРАБОТЧИКИ ----------
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    # сбрасываем недооформленную заявку, иначе следующее сообщение запишется как имя/телефон
    if await state.get_state() is not None:
        await state.clear()
        await message.answer("Оформление прервано.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(
        message,
        "Здравствуйте! Это витрина домашней кондитерской 🍰\n\nВыберите категорию:",
    )


@dp.message(Command("myid"))
async def my_id(message: types.Message):
    await message.answer(f"Ваш ID: {message.from_user.id}")


@dp.callback_query(F.data.startswith("cat:"))
async def open_category(callback: CallbackQuery):
    category_id = int(callback.data.split(":")[1])
    await show_card(callback, category_id, 0)


@dp.callback_query(F.data.startswith("prod:"))
async def flip_card(callback: CallbackQuery):
    _, category_id, index = callback.data.split(":")
    await show_card(callback, int(category_id), int(index))


@dp.callback_query(F.data == "cats")
async def back_to_categories(callback: CallbackQuery):
    await show_photo_screen(callback, BANNER_FILE, "Выберите категорию:", kb_categories())
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()   # кнопка-счётчик "1/2" ничего не делает


# ---------- ЗАЯВКА ----------
CANCEL_TEXT = "❌ Отмена"
SKIP_TEXT = "Без комментария"
MAX_COMMENT = 500


class Order(StatesGroup):
    name = State()
    phone = State()
    comment = State()


def kb_name(full_name: str):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=full_name)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
    )


def kb_phone():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
    )


def kb_comment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SKIP_TEXT)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
    )


@dp.callback_query(F.data.startswith("order:"))
async def order_start(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = db_get_product(product_id)
    if not product:
        await callback.answer("Этот товар больше недоступен", show_alert=True)
        return
    await state.clear()
    await state.set_state(Order.name)
    await state.update_data(product_id=product_id)
    await callback.message.answer(
        f"Оформляем заявку: {product[2]}\n\n"
        "Как к вам обращаться? Напишите имя или нажмите кнопку ниже.",
        reply_markup=kb_name(callback.from_user.full_name),
    )
    await callback.answer()


# Отмена должна стоять ВЫШЕ шагов заявки,
# иначе "❌ Отмена" запишется как имя или комментарий
@dp.message(Command("cancel"))
@dp.message(F.text == CANCEL_TEXT)
async def order_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message, "Выберите категорию:")


@dp.message(Order.name, F.text)
async def order_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напишите, пожалуйста, имя.")
        return
    if len(name) > 50:
        await message.answer("Слишком длинное имя, напишите покороче.")
        return
    await state.update_data(client_name=name)
    await state.set_state(Order.phone)
    await message.answer(
        "Оставьте номер телефона, чтобы мы могли связаться с вами.\n"
        "Нажмите кнопку ниже или напишите номер.",
        reply_markup=kb_phone(),
    )


async def save_phone_and_ask_comment(message: types.Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    await state.set_state(Order.comment)
    await message.answer(
        "Добавьте комментарий: на какую дату, надпись на торте, пожелания.\n"
        "Или нажмите «Без комментария».",
        reply_markup=kb_comment(),
    )


@dp.message(Order.phone, F.contact)
async def order_phone_contact(message: types.Message, state: FSMContext):
    # клиент нажал «Поделиться номером»
    await save_phone_and_ask_comment(message, state, message.contact.phone_number)


@dp.message(Order.phone, F.text)
async def order_phone_text(message: types.Message, state: FSMContext):
    # клиент написал номер вручную: оставляем только цифры и считаем их
    digits = re.sub(r"\D", "", message.text)
    if not 10 <= len(digits) <= 15:
        await message.answer(
            "Не похоже на номер телефона. Напишите номер, например +7 900 123-45-67, "
            "или нажмите кнопку ниже."
        )
        return
    await save_phone_and_ask_comment(message, state, message.text.strip())


@dp.message(Order.comment, F.text)
async def order_comment(message: types.Message, state: FSMContext):
    comment = "" if message.text == SKIP_TEXT else message.text.strip()
    # без ограничения длинный комментарий не влезет в уведомление админу (лимит Telegram 4096)
    if len(comment) > MAX_COMMENT:
        await message.answer(
            f"Комментарий слишком длинный ({len(comment)} символов), "
            f"сократите, пожалуйста, до {MAX_COMMENT}."
        )
        return
    data = await state.get_data()
    product = db_get_product(data["product_id"])
    if not product:
        await state.clear()
        await message.answer("Этот товар больше недоступен.", reply_markup=ReplyKeyboardRemove())
        return

    product_id, category_id, name, description, price, photo = product
    order_id = db_add_order(
        message.from_user.id, product_id, data["client_name"], data["phone"], comment
    )
    await state.clear()

    await message.answer(
        f"✅ Заявка №{order_id} принята!\n\n"
        f"{name}, {format_price(price)}\n"
        f"Мы свяжемся с вами по номеру {data['phone']}.\n\n"
        "Отменить заявку можно в разделе «📋 Мои заявки».",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_main_menu(message, "Хотите посмотреть что-нибудь ещё?")

    # уведомление владельцу
    if ADMIN_ID:
        username = f"@{message.from_user.username}" if message.from_user.username else "нет"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 Заявка №{order_id}\n\n"
                f"Товар: {name}\n"
                f"Цена: {format_price(price)}\n\n"
                f"Имя: {data['client_name']}\n"
                f"Телефон: {data['phone']}\n"
                f"Telegram: {username}\n"
                f"Комментарий: {comment or '—'}",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа о заявке №%s", order_id)


# ---------- МОИ ЗАЯВКИ ----------
def my_orders_text(orders: list) -> str:
    if not orders:
        return "У вас нет активных заявок."
    lines = ["Ваши заявки:", ""]
    for order_id, product_name, created_at in orders:
        lines.append(f"№{order_id} — {product_name} (от {created_at})")
    lines.append("")
    lines.append("Чтобы отменить заявку, нажмите на неё.")
    return "\n".join(lines)


def kb_my_orders(orders: list):
    rows = []
    for order_id, product_name, created_at in orders:
        rows.append([InlineKeyboardButton(
            text=f"❌ №{order_id} {product_name}", callback_data=f"ocancel:{order_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def replace_message(callback: CallbackQuery, text: str, kb):
    # текстовое сообщение меняем на месте, а карточку с фото удаляем и шлём заново
    msg = callback.message
    if msg.photo is None:
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(text, reply_markup=kb)


@dp.message(Command("myorders"))
async def my_orders_command(message: types.Message):
    orders = db_get_user_orders(message.from_user.id)
    await message.answer(my_orders_text(orders), reply_markup=kb_my_orders(orders))


@dp.callback_query(F.data == "myorders")
async def my_orders_button(callback: CallbackQuery):
    orders = db_get_user_orders(callback.from_user.id)
    await show_photo_screen(callback, BANNER_FILE, my_orders_text(orders), kb_my_orders(orders))
    await callback.answer()


@dp.callback_query(F.data.startswith("ocancel:"))
async def order_ask_cancel(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    order = db_get_order(order_id)
    # проверяем, что заявка есть, принадлежит этому клиенту и ещё активна
    if not order or order[1] != callback.from_user.id or order[2] != "new":
        await callback.answer("Эта заявка уже неактивна", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"oyes:{order_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="myorders")],
    ])
    await show_photo_screen(callback, BANNER_FILE, f"Отменить заявку №{order_id} ({order[5]})?", kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("oyes:"))
async def order_do_cancel(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    order = db_get_order(order_id)
    if not order or order[1] != callback.from_user.id or order[2] != "new":
        await callback.answer("Эта заявка уже неактивна", show_alert=True)
        return
    db_cancel_order(order_id)
    _, user_id, status, client_name, phone, product_name = order

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"❌ Клиент отменил заявку №{order_id}\n\n"
                f"Товар: {product_name}\n"
                f"Имя: {client_name}\n"
                f"Телефон: {phone}",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа об отмене заявки №%s", order_id)

    orders = db_get_user_orders(callback.from_user.id)
    await show_photo_screen(
        callback,
        BANNER_FILE,
        f"Заявка №{order_id} отменена.\n\n" + my_orders_text(orders),
        kb_my_orders(orders),
    )
    await callback.answer("Отменено")


# ---------- АДМИНКА ----------
NO_PHOTO_TEXT = "Без фото"


class AddProduct(StatesGroup):
    category = State()
    photo = State()
    name = State()
    description = State()
    price = State()


class EditPhoto(StatesGroup):
    photo = State()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Заявки", callback_data="adm_orders")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm_add")],
        [InlineKeyboardButton(text="🖼 Фото товара", callback_data="adm_photo")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data="adm_del")],
    ])


def kb_admin_categories(prefix: str):
    rows = []
    for cid, name in db_get_categories():
        rows.append([InlineKeyboardButton(text=name, callback_data=f"{prefix}:{cid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel_only():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
        resize_keyboard=True,
    )


def kb_photo_step():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NO_PHOTO_TEXT)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
    )


async def send_card_preview(message: types.Message, product_id: int):
    # показывает карточку товара так, как её увидит клиент
    product = db_get_product(product_id)
    _, _, name, description, price, photo = product
    text = card_text(name, description, price)
    if photo:
        await message.answer_photo(photo, caption=text)
    else:
        await message.answer(text)


@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для владельца.")
        return
    await state.clear()
    await message.answer("Панель владельца:", reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await replace_message(callback, "Панель владельца:", kb_admin())
    await callback.answer()


# --- Добавление товара ---
@dp.callback_query(F.data == "adm_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddProduct.category)
    await replace_message(callback, "В какую категорию добавить товар?", kb_admin_categories("adm_cat"))
    await callback.answer()


@dp.callback_query(AddProduct.category, F.data.startswith("adm_cat:"))
async def admin_add_category(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":")[1])
    await state.update_data(category_id=category_id)
    await state.set_state(AddProduct.photo)
    await callback.message.answer(
        "Отправьте фото товара или нажмите «Без фото».",
        reply_markup=kb_photo_step(),
    )
    await callback.answer()


async def admin_ask_name(message: types.Message, state: FSMContext):
    await state.set_state(AddProduct.name)
    await message.answer("Название товара:", reply_markup=kb_cancel_only())


@dp.message(AddProduct.photo, F.photo)
async def admin_add_photo(message: types.Message, state: FSMContext):
    # Telegram присылает фото в нескольких размерах, [-1] — самое большое
    await state.update_data(photo=message.photo[-1].file_id)
    await admin_ask_name(message, state)


@dp.message(AddProduct.photo, F.text == NO_PHOTO_TEXT)
async def admin_add_no_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo=None)
    await admin_ask_name(message, state)


@dp.message(AddProduct.photo)
async def admin_add_photo_wrong(message: types.Message):
    await message.answer("Нужно отправить фото (картинкой, не файлом) или нажать «Без фото».")


@dp.message(AddProduct.name, F.text)
async def admin_add_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) > 60:
        await message.answer("Слишком длинное название, максимум 60 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(AddProduct.description)
    await message.answer("Описание: состав, вес, количество и т. п.")


@dp.message(AddProduct.description, F.text)
async def admin_add_description(message: types.Message, state: FSMContext):
    description = message.text.strip()
    if len(description) > 500:
        await message.answer("Слишком длинное описание, максимум 500 символов.")
        return
    await state.update_data(description=description)
    await state.set_state(AddProduct.price)
    await message.answer("Цена в рублях, только число. Например: 1500")


@dp.message(AddProduct.price, F.text)
async def admin_add_price(message: types.Message, state: FSMContext):
    text = message.text.replace(" ", "")
    if not text.isdigit() or not 0 < int(text) <= 1_000_000:
        await message.answer("Напишите цену числом, например 1500.")
        return
    data = await state.get_data()
    product_id = db_add_product(
        data["category_id"], data["name"], data["description"], int(text), data["photo"]
    )
    await state.clear()
    await message.answer("✅ Товар добавлен! Так его увидят клиенты:", reply_markup=ReplyKeyboardRemove())
    await send_card_preview(message, product_id)
    await message.answer("Панель владельца:", reply_markup=kb_admin())


# --- Фото существующего товара ---
@dp.callback_query(F.data == "adm_photo")
async def admin_photo_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await replace_message(callback, "Выберите категорию:", kb_admin_categories("adm_pcat"))
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_pcat:"))
async def admin_photo_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    products = db_get_products(category_id)
    if not products:
        await callback.answer("В этой категории пока нет товаров", show_alert=True)
        return
    rows = []
    for product_id, name, description, price, photo in products:
        mark = "🖼" if photo else "▫️"
        rows.append([InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"adm_pprod:{product_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_photo")])
    await replace_message(
        callback,
        "Выберите товар (🖼 — фото уже есть, ▫️ — без фото):",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_pprod:"))
async def admin_photo_product(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    product = db_get_product(product_id)
    if not product:
        await callback.answer("Этот товар уже удалён", show_alert=True)
        return
    await state.set_state(EditPhoto.photo)
    await state.update_data(product_id=product_id)
    await callback.message.answer(
        f"Отправьте новое фото для «{product[2]}».",
        reply_markup=kb_cancel_only(),
    )
    await callback.answer()


@dp.message(EditPhoto.photo, F.photo)
async def admin_photo_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_set_product_photo(data["product_id"], message.photo[-1].file_id)
    await state.clear()
    await message.answer("✅ Фото обновлено! Так теперь выглядит карточка:", reply_markup=ReplyKeyboardRemove())
    await send_card_preview(message, data["product_id"])
    await message.answer("Панель владельца:", reply_markup=kb_admin())


@dp.message(EditPhoto.photo)
async def admin_photo_wrong(message: types.Message):
    await message.answer("Нужно отправить фото (картинкой, не файлом).")

# --- Удаление товара ---
@dp.callback_query(F.data == "adm_del")
async def admin_del_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await replace_message(callback, "Из какой категории удалить товар?", kb_admin_categories("adm_dcat"))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_dcat:"))
async def admin_del_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    category_id = int(callback.data.split(":")[1])
    products = db_get_products(category_id)
    if not products:
        await callback.answer("В этой категории нет товаров", show_alert=True)
        return
    rows = []
    for product_id, name, description, price, photo in products:
        rows.append([InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"adm_dprod:{product_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_del")])
    await replace_message(callback, "Какой товар удалить?", InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_dprod:"))
async def admin_del_ask(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    product = db_get_product(product_id)
    if not product:
        await callback.answer("Этот товар уже удалён", show_alert=True)
        return
    _, category_id, name, description, price, photo = product
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_dyes:{product_id}")],
        [InlineKeyboardButton(text="⬅️ Нет", callback_data=f"adm_dcat:{category_id}")],
    ])
    await replace_message(
        callback,
        "Удалить этот товар из каталога?\n\n" + card_text(name, description, price),
        kb,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_dyes:"))
async def admin_del_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    product = db_get_product(product_id)
    if not product:
        await callback.answer("Этот товар уже удалён", show_alert=True)
        return
    db_delete_product(product_id)
    await replace_message(callback, f"🗑 Товар «{product[2]}» удалён.\n\nПанель владельца:", kb_admin())
    await callback.answer("Удалено")

# --- Заявки ---
STATUS_NAMES = {"new": "🟢 активная", "done": "✅ выполнена", "cancelled": "❌ отменена"}

async def show_admin_orders(callback: CallbackQuery, prefix: str = ""):
    orders = db_get_active_orders()
    if orders:
        text = prefix + "Активные заявки (нажмите, чтобы открыть):"
    else:
        text = prefix + "Активных заявок нет."
    rows = []
    for order_id, product_name, client_name in orders:
        rows.append([InlineKeyboardButton(
            text=f"№{order_id} {product_name} — {client_name}", callback_data=f"adm_ord:{order_id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu")])
    await replace_message(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data == "adm_orders")
async def admin_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await show_admin_orders(callback)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_ord:"))
async def admin_order_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = db_get_order_full(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    _, user_id, status, client_name, phone, comment, created_at, product_name, price = order
    text = (
        f"Заявка №{order_id} от {created_at}\n"
        f"Статус: {STATUS_NAMES.get(status, status)}\n\n"
        f"Товар: {product_name}\n"
        f"Цена: {format_price(price) if price else '—'}\n\n"
        f"Имя: {client_name}\n"
        f"Телефон: {phone}\n"
        f"Комментарий: {comment or '—'}"
    )
    rows = []
    if status == "new":
        rows.append([
            InlineKeyboardButton(text="✅ Выполнена", callback_data=f"adm_odone:{order_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_ocancel:{order_id}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="adm_orders")])
    await replace_message(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_odone:"))
async def admin_order_done(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = db_get_order_full(order_id)
    if not order or order[2] != "new":
        await callback.answer("Эта заявка уже неактивна", show_alert=True)
        return
    db_set_order_status(order_id, "done")
    await show_admin_orders(callback, f"✅ Заявка №{order_id} выполнена.\n\n")
    await callback.answer("Готово")

@dp.callback_query(F.data.startswith("adm_ocancel:"))
async def admin_order_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = db_get_order_full(order_id)
    if not order or order[2] != "new":
        await callback.answer("Эта заявка уже неактивна", show_alert=True)
        return
    db_set_order_status(order_id, "cancelled")
    user_id, product_name = order[1], order[7]
    try:
        await bot.send_message(
            user_id,
            f"К сожалению, ваша заявка №{order_id} ({product_name}) отменена кондитерской. "
            "Если есть вопросы, мы с вами свяжемся. Посмотреть каталог: /start",
        )
    except Exception:
        # чаще всего клиент просто заблокировал бота, но запишем причину в лог
        logging.warning("Не удалось уведомить клиента %s об отмене заявки №%s", user_id, order_id)
    await show_admin_orders(callback, f"❌ Заявка №{order_id} отменена, клиент получил уведомление.\n\n")
    await callback.answer("Отменено")


# Этот обработчик должен стоять ПОСЛЕДНИМ: ловит нажатия на старые кнопки
@dp.callback_query()
async def old_button(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Нажмите /start", show_alert=True)


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())