import os
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False

from config import settings, TARIFFS
from database import (
    get_or_create_user, apply_tariff, get_admin_stats,
    admin_grant_premium_by_identifier, get_all_user_ids,
    add_to_blacklist, remove_from_blacklist, get_blacklist,
    create_gift_code, redeem_gift_code
)
from osint_engine import TG_API_ID, TG_API_HASH, TELETHON_SESSION_FILE, _telethon_lock

admin_payments_router = Router()

AUTHORIZED_ADMINS: set[int] = set(settings.ADMIN_IDS)
_pending_telethon_clients: dict[int, tuple[any, str, str]] = {}


class AdminStates(StatesGroup):
    waiting_password = State()
    waiting_grant = State()
    waiting_gift_days = State()
    waiting_block = State()
    waiting_unblock = State()
    waiting_broadcast = State()
    waiting_tg_phone = State()
    waiting_tg_code = State()
    waiting_tg_2fa = State()


def get_tariffs_kb() -> InlineKeyboardMarkup:
    buttons = []
    for key, t in TARIFFS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{t['title']} — {t['price']} ⭐",
            callback_data=f"buy:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Обновить статистику", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📱 Пробив ТГ по номеру (Подключить Telethon)", callback_data="adm:telethon")],
        [InlineKeyboardButton(text="🎁 Выдать/Снять Premium (по ID или @нику)", callback_data="adm:grant")],
        [InlineKeyboardButton(text="🎟 Создать подарочный промокод (для акций)", callback_data="adm:gift")],
        [
            InlineKeyboardButton(text="🛡 Заблокировать поиск цели", callback_data="adm:block"),
            InlineKeyboardButton(text="🔓 Разблокировать цель", callback_data="adm:unblock")
        ],
        [InlineKeyboardButton(text="📋 Список защищённых целей", callback_data="adm:bl_list")],
        [InlineKeyboardButton(text="📢 Сделать рассылку всем", callback_data="adm:broadcast")]
    ])


# ==================== ТАРИФЫ (70 И 700 ЗВЁЗД) И ПРОМОКОДЫ ====================

@admin_payments_router.message(Command("premium"))
async def cmd_premium(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    status = f"✅ Активен до {user['premium_until'][:10]}" if user["is_premium"] else "❌ Тариф FREE"

    text = (
        "💎 <b>Управление подпиской и тарифами</b>\n\n"
        f"▪️ <b>Ваш статус:</b> {status}\n"
        f"▪️ <b>Бесплатных запросов за неделю:</b> {user['weekly_used']} / {settings.FREE_WEEKLY_LIMIT}\n\n"
        "🎟 <i>Есть подарочный промокод? Введите:</i> <code>/promo КОД</code>\n\n"
        "👇 <i>Выберите тариф (оплата через Telegram Stars):</i>"
    )
    await message.answer(text, reply_markup=get_tariffs_kb(), parse_mode="HTML")


@admin_payments_router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Введите промокод: <code>/promo GIFT-...</code>", parse_mode="HTML")
        return
    ok, days = await redeem_gift_code(message.from_user.id, command.args)
    if ok:
        await message.answer(f"🎉 Промокод активирован! Вам начислено <b>+{days} дн. Premium</b>!", parse_mode="HTML")
    else:
        await message.answer("❌ Промокод недействителен или уже был использован.")


@admin_payments_router.callback_query(F.data.startswith("buy:"))
async def cb_buy_tariff(callback: CallbackQuery, bot: Bot):
    tariff_key = callback.data.split(":", 1)[1]
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=tariff["title"],
        description="Безлимитный доступ к глубокому OSINT-поиску (DEEP + Pivoting).",
        payload=f"tariff:{tariff_key}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["price"])]
    )


@admin_payments_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@admin_payments_router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    tariff_key = payment.invoice_payload.split(":", 1)[1]
    tariff = TARIFFS.get(tariff_key, TARIFFS["prem_30"])

    await apply_tariff(
        user_id=message.from_user.id,
        tariff_key=tariff_key,
        days=tariff["days"],
        credits=tariff["deep_credits"],
        amount_xtr=payment.total_amount,
        charge_id=payment.provider_payment_charge_id or "xtr",
        tg_charge_id=payment.telegram_payment_charge_id
    )
    await message.answer(
        f"✅ <b>Оплата успешно проведена!</b>\nТариф: <b>{tariff['title']}</b> активирован.",
        parse_mode="HTML"
    )


# ==================== АДМИН-ПАНЕЛЬ ====================

async def show_admin_dashboard(message_or_cb: Message | CallbackQuery):
    stats = await get_admin_stats()
    text = (
        "🛠 <b>Закрытая Админ-панель OSINT Бота</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['users']}</b>\n"
        f"💎 Активных Premium: <b>{stats['premium']}</b>\n"
        f"📁 Отчётов за 7 дней: <b>{stats['reports']}</b>\n"
        f"🛡 Защищённых целей (в блоке): <b>{stats['blocked']}</b>\n"
        f"⭐ Заработано всего: <b>{stats['xtr_total']} XTR</b>\n\n"
        "Выберите действие на клавиатуре ниже:"
    )
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=get_admin_menu_kb(), parse_mode="HTML")
    else:
        await message_or_cb.message.edit_text(text, reply_markup=get_admin_menu_kb(), parse_mode="HTML")


@admin_payments_router.message(Command("admin"))
async def cmd_admin(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    if command.args and command.args.strip() == settings.ADMIN_PASSWORD:
        AUTHORIZED_ADMINS.add(uid)
        try:
            await message.delete()
        except Exception:
            pass

    if uid in AUTHORIZED_ADMINS:
        await state.clear()
        await show_admin_dashboard(message)
    else:
        await state.set_state(AdminStates.waiting_password)
        await message.answer("🔐 <b>Вход в админ-панель</b>\nВведите пароль администратора:", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.text and message.text.strip() == settings.ADMIN_PASSWORD:
        AUTHORIZED_ADMINS.add(message.from_user.id)
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔓 Пароль принят!")
        await show_admin_dashboard(message)
    else:
        await state.clear()
        await message.answer("❌ Неверный пароль.")


@admin_payments_router.callback_query(F.data.startswith("adm:"))
async def cb_admin_actions(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in AUTHORIZED_ADMINS:
        await callback.answer("Требуется авторизация /admin", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "stats":
        await show_admin_dashboard(callback)
    elif action == "telethon":
        if not HAS_TELETHON:
            await callback.message.answer("❌ Библиотека telethon не установлена. Выполните: pip install telethon")
            return
        await state.set_state(AdminStates.waiting_tg_phone)
        await callback.message.answer(
            "📱 <b>Подключение прямого пробива Telegram по номеру (Telethon)</b>\n\n"
            "Отправьте номер телефона аккаунта Telegram (например: <code>+79991234567</code>), "
            "с которого бот будет проверять номера в базе Telegram:",
            parse_mode="HTML"
        )
    elif action == "grant":
        await state.set_state(AdminStates.waiting_grant)
        await callback.message.answer(
            "🎁 <b>Выдача / Снятие Premium</b>\n"
            "Отправьте через пробел: <code>ID_или_@ник количество_дней</code>\n"
            "<i>Пример:</i> <code>@ivan_test 30</code> или <code>12345678 0</code>",
            parse_mode="HTML"
        )
    elif action == "gift":
        await state.set_state(AdminStates.waiting_gift_days)
        await callback.message.answer(
            "🎟 <b>Создание подарочного промокода</b>\n"
            "Отправьте через пробел: <code>количество_дней количество_активаций</code>\n"
            "<i>Пример:</i> <code>30 5</code>",
            parse_mode="HTML"
        )
    elif action == "block":
        await state.set_state(AdminStates.waiting_block)
        await callback.message.answer(
            "🛡 <b>Блокировка поиска (Защита данных)</b>\n"
            "Отправьте любой ник, email, телефон, ID или ссылку, которую бот <b>никому не должен искать</b>:",
            parse_mode="HTML"
        )
    elif action == "unblock":
        await state.set_state(AdminStates.waiting_unblock)
        await callback.message.answer("🔓 Отправьте цель, которую нужно удалить из чёрного списка:")
    elif action == "bl_list":
        bl = await get_blacklist()
        if not bl:
            await callback.message.answer("📋 Чёрный список пуст.")
        else:
            lines = [f"• <code>{item['target_norm']}</code> ({item['created_at'][:10]})" for item in bl[:50]]
            await callback.message.answer("🛡 <b>Защищённые от поиска цели:</b>\n" + "\n".join(lines), parse_mode="HTML")
    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.answer("📢 Отправьте текст сообщения для рассылки всем пользователям бота:")


# ==================== ПОДКЛЮЧЕНИЕ TELETHON В ЧАТЕ БОТА ====================

@admin_payments_router.message(AdminStates.waiting_tg_phone)
async def process_tg_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    async with _telethon_lock:
        try:
            client = TelegramClient("osint_userbot", TG_API_ID, TG_API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                await state.clear()
                await message.answer(
                    f"✅ <b>Этот модуль уже авторизован!</b>\n"
                    f"Подключён аккаунт: {me.first_name} (@{me.username or 'без ника'})",
                    parse_mode="HTML"
                )
                return

            sent = await client.send_code_request(phone)
            _pending_telethon_clients[message.from_user.id] = (client, phone, sent.phone_code_hash)
            await state.set_state(AdminStates.waiting_tg_code)
            await message.answer(
                "📩 <b>Код подтверждения отправлен в ваш Telegram!</b>\n\n"
                "⚠️ <b>ВАЖНО:</b> Напишите цифры кода сюда <b>через пробел</b> (например: <code>1 2 3 4 5</code>), "
                "чтобы Telegram не аннулировал код!",
                parse_mode="HTML"
            )
        except Exception as e:
            await state.clear()
            await message.answer(f"❌ Ошибка отправки кода: {e}")


@admin_payments_router.message(AdminStates.waiting_tg_code)
async def process_tg_code(message: Message, state: FSMContext):
    pending = _pending_telethon_clients.get(message.from_user.id)
    if not pending:
        await state.clear()
        await message.answer("Сессия истекла, нажмите кнопку в /admin заново.")
        return

    client, phone, code_hash = pending
    code = message.text.replace(" ", "").replace("-", "").strip()

    async with _telethon_lock:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
            _pending_telethon_clients.pop(message.from_user.id, None)
            me = await client.get_me()
            await client.disconnect()
            await state.clear()
            await message.answer(
                f"✅ <b>Модуль Telethon успешно подключён!</b>\n"
                f"Аккаунт: {me.first_name} (@{me.username or 'без ника'})\n\n"
                "Теперь при вводе любого номера телефона бот будет напрямую доставать его "
                "<b>@username, Telegram ID и Имя</b> из базы Telegram!",
                parse_mode="HTML"
            )
        except SessionPasswordNeededError:
            await state.set_state(AdminStates.waiting_tg_2fa)
            await message.answer(
                "🔐 На вашем аккаунте включён <b>Облачный пароль (2FA)</b>.\n"
                "Отправьте ваш облачный пароль от Telegram следующим сообщением (бот сразу удалит его из чата):",
                parse_mode="HTML"
            )
        except Exception as e:
            _pending_telethon_clients.pop(message.from_user.id, None)
            try:
                await client.disconnect()
            except Exception:
                pass
            await state.clear()
            await message.answer(f"❌ Не удалось войти: {e}")


@admin_payments_router.message(AdminStates.waiting_tg_2fa)
async def process_tg_2fa(message: Message, state: FSMContext):
    await state.clear()
    pending = _pending_telethon_clients.pop(message.from_user.id, None)
    pwd = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not pending:
        await message.answer("Сессия истекла, попробуйте снова через /admin.")
        return

    client, _, _ = pending
    async with _telethon_lock:
        try:
            await client.sign_in(password=pwd)
            me = await client.get_me()
            await client.disconnect()
            await message.answer(
                f"✅ <b>Облачный пароль принят! Telethon успешно подключён!</b>\n"
                f"Аккаунт: {me.first_name} (@{me.username or 'без ника'})\n\n"
                "Теперь пробив номеров телефонов напрямую через Telegram полностью работает!",
                parse_mode="HTML"
            )
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            await message.answer(f"❌ Ошибка 2FA пароля: {e}")


# ==================== ОСТАЛЬНЫЕ ФУНКЦИИ АДМИНКИ ====================

@admin_payments_router.message(AdminStates.waiting_grant)
async def process_grant(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        ident, days_str = message.text.strip().split()
        days = int(days_str)
        ok, target_uid, status_info = await admin_grant_premium_by_identifier(ident, days)
        if ok:
            await message.answer(f"✅ Успешно! Пользователь <code>{ident}</code> (ID: {target_uid}) — Premium {status_info}.", parse_mode="HTML")
            if days > 0 and target_uid:
                try:
                    await bot.send_message(target_uid, f"🎁 Администратор подарил вам подписку <b>PREMIUM на {days} дн.</b>!", parse_mode="HTML")
                except Exception:
                    pass
        else:
            await message.answer(f"⚠️ {status_info}")
    except Exception:
        await message.answer("❌ Неверный формат. Пример: @username 30")


@admin_payments_router.message(AdminStates.waiting_gift_days)
async def process_gift_create(message: Message, state: FSMContext):
    await state.clear()
    try:
        parts = message.text.strip().split()
        days = int(parts[0])
        uses = int(parts[1]) if len(parts) > 1 else 1
        code = await create_gift_code(days, uses)
        await message.answer(
            f"🎟 <b>Подарочный промокод создан!</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"Срок Premium: <b>{days} дн.</b> | Активаций: <b>{uses}</b>\n\n"
            f"Активация: <code>/promo {code}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await message.answer("❌ Ошибка. Отправьте два числа через пробел, например: 30 1")


@admin_payments_router.message(AdminStates.waiting_block)
async def process_block_target(message: Message, state: FSMContext):
    await state.clear()
    norm = await add_to_blacklist(message.text)
    await message.answer(f"🛡 Цель <code>{norm}</code> добавлена в защиту! Теперь бот никому не выдаст по ней данные.", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_unblock)
async def process_unblock_target(message: Message, state: FSMContext):
    await state.clear()
    norm = await remove_from_blacklist(message.text)
    await message.answer(f"🔓 Цель <code>{norm}</code> разблокирована.", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uids = await get_all_user_ids()
    sent = 0
    for uid in uids:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            pass
    await message.answer(f"📢 Рассылка завершена! Доставлено: {sent} из {len(uids)}.")