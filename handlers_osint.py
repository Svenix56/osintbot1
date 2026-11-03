import asyncio
import html
import json
import time
import uuid
from dataclasses import dataclass, field
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject

from config import settings
from database import (
    get_or_create_user, increment_free_usage, consume_deep_credit,
    save_report, get_user_reports, get_report_by_id, is_target_blocked
)
from validators import detect_target_type, TargetType
from osint_engine import OSINTEngine
from handlers_admin_payments import get_tariffs_kb

osint_router = Router()
engine = OSINTEngine()

PENDING_TARGETS: dict[str, str] = {}
# Хранилище времени последнего поиска каждого пользователя (анти-спам 10 сек)
LAST_SEARCH_TIME: dict[int, float] = {}


@dataclass(order=True)
class ScanJob:
    priority: int
    created_at: float = field(compare=True)
    user_id: int = field(compare=False)
    chat_id: int = field(compare=False)
    status_msg_id: int = field(compare=False)
    target: str = field(compare=False)
    target_type: TargetType = field(compare=False)
    deep: bool = field(compare=False)


task_queue: asyncio.PriorityQueue[ScanJob] = asyncio.PriorityQueue()


def check_cooldown(user_id: int) -> int:
    """Проверяет, прошло ли 10 секунд с прошлого запроса. Возвращает 0, если можно искать, или остаток секунд."""
    now = time.time()
    last = LAST_SEARCH_TIME.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < settings.SEARCH_COOLDOWN_SECONDS:
        return int(settings.SEARCH_COOLDOWN_SECONDS - elapsed) + 1
    LAST_SEARCH_TIME[user_id] = now
    return 0


async def send_report_result(bot: Bot, chat_id: int, status_msg_id: int | None, target: str, short_10_html: str, full_text: str, deep: bool):
    if not deep:
        if status_msg_id:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg_id, text=short_10_html, parse_mode="HTML", disable_web_page_preview=True
            )
        else:
            await bot.send_message(chat_id=chat_id, text=short_10_html, parse_mode="HTML", disable_web_page_preview=True)
    else:
        preview = f"{short_10_html}\n\n📎 <i>Полное досье со всеми техническими данными прикреплено в файле ниже:</i>"
        if status_msg_id:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg_id, text=preview, parse_mode="HTML", disable_web_page_preview=True
            )
        else:
            await bot.send_message(chat_id=chat_id, text=preview, parse_mode="HTML", disable_web_page_preview=True)

        txt_bytes = full_text.encode("utf-8")
        safe_name = "".join(c if c.isalnum() else "_" for c in target)[:25]
        doc = BufferedInputFile(txt_bytes, filename=f"osint_{safe_name}.txt")
        await bot.send_document(chat_id=chat_id, document=doc, caption=f"📊 Полный отчёт по объекту: <code>{html.escape(target)}</code>", parse_mode="HTML")


async def queue_worker(bot: Bot):
    while True:
        job: ScanJob = await task_queue.get()
        try:
            short_10_html, full_text = await engine.execute_scan(job.target, job.target_type, deep=job.deep)
            await save_report(job.user_id, job.target, "DEEP" if job.deep else "QUICK", full_text)
            await send_report_result(bot, job.chat_id, job.status_msg_id, job.target, short_10_html, full_text, job.deep)
        except Exception as e:
            await bot.edit_message_text(
                chat_id=job.chat_id, message_id=job.status_msg_id, text=f"⚠️ Ошибка при выполнении поиска: {e}"
            )
        finally:
            task_queue.task_done()


async def enqueue_search(bot: Bot, chat_id: int, user_id: int, username: str | None, raw_target: str, deep: bool):
    # 1. Анти-спам: не более 1 запроса в 10 секунд для всех пользователей
    wait_sec = check_cooldown(user_id)
    if wait_sec > 0:
        await bot.send_message(
            chat_id,
            f"⏳ <b>Пожалуйста, подождите {wait_sec} сек.</b>\n"
            f"Ограничение защиты от перегрузки: не более 1 поиска каждые {settings.SEARCH_COOLDOWN_SECONDS} секунд.",
            parse_mode="HTML"
        )
        return

    # 2. Проверка чёрного списка (защиты от пробива)
    if await is_target_blocked(raw_target):
        await bot.send_message(chat_id, "🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>", parse_mode="HTML")
        return

    user = await get_or_create_user(user_id, username)
    target_type, clean_target = detect_target_type(raw_target)

    if target_type == TargetType.UNKNOWN:
        await bot.send_message(chat_id, "❌ Не удалось определить тип цели. Отправьте ник, email, телефон, госномер авто, домен, IP, ID или Имя Фамилию.")
        return

    if deep:
        if not user["is_premium"]:
            used_credit = await consume_deep_credit(user_id)
            if not used_credit:
                await bot.send_message(
                    chat_id,
                    "🔒 Режим <b>DEEP + Pivoting</b> доступен по подписке <b>Premium</b> (50 ⭐ / мес или 700 ⭐ навсегда).",
                    reply_markup=get_tariffs_kb(),
                    parse_mode="HTML"
                )
                return
    else:
        if not user["is_premium"] and user["weekly_used"] >= settings.FREE_WEEKLY_LIMIT:
            await bot.send_message(
                chat_id,
                f"⛔ Вы исчерпали бесплатный лимит ({settings.FREE_WEEKLY_LIMIT} запросов в неделю).\n"
                "Оформите <b>Premium</b> за 50 ⭐ / мес для снятия всех ограничений!",
                reply_markup=get_tariffs_kb(),
                parse_mode="HTML"
            )
            return
        if not user["is_premium"]:
            await increment_free_usage(user_id)

    priority = 1 if (user["is_premium"] or deep) else 2
    status_msg = await bot.send_message(
        chat_id,
        f"🔎 <b>Идёт сбор досье...</b>\n"
        f"▪️ <b>Объект:</b> <code>{html.escape(clean_target)}</code> ({target_type.value.upper()})\n"
        f"▪️ <b>Режим:</b> {'🚀 Полный (DEEP + Pivoting)' if deep else '⚡ Краткий (10 пунктов)'}",
        parse_mode="HTML"
    )

    await task_queue.put(ScanJob(
        priority=priority,
        created_at=time.time(),
        user_id=user_id,
        chat_id=chat_id,
        status_msg_id=status_msg.message_id,
        target=clean_target,
        target_type=target_type,
        deep=deep
    ))


@osint_router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "🕵️‍♂️ <b>OSINT-бот разведки по открытым источникам</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Просто <b>отправьте мне в чат</b> любую цель:\n\n"
        "🔹 <b>Никнейм / Ссылку:</b> <code>@durov</code>, <code>vk.com/durov</code>\n"
        "🔹 <b>Email:</b> <code>example@gmail.com</code>\n"
        "🔹 <b>Телефон:</b> <code>+79991234567</code>\n"
        "🔹 <b>Госномер авто:</b> <code>А777АА777</code>\n"
        "🔹 <b>Telegram / VK ID:</b> <code>123456789</code> (или перешлите сообщение)\n"
        "🔹 <b>Имя и Фамилию:</b> <code>Иван Петров</code>\n"
        "🔹 <b>Домен или IP:</b> <code>example.com</code>, <code>1.1.1.1</code>\n"
        "🔹 <b>Фото файлом:</b> извлечение скрытых EXIF / GPS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📁 /history — Ваши отчёты за последние 7 дней\n"
        "💎 /premium — Подписка (50 ⭐ мес / 700 ⭐ навсегда)\n"
        "🔐 /admin — Панель администратора",
        parse_mode="HTML"
    )


@osint_router.message(Command("history"))
async def cmd_history(message: Message):
    reports = await get_user_reports(message.from_user.id)
    if not reports:
        await message.answer("📁 У вас пока нет сохранённых отчётов за последние 7 дней.")
        return

    buttons = []
    for r in reports:
        date_short = r["created_at"][:10]
        buttons.append([InlineKeyboardButton(
            text=f"[{r['mode']}] {r['target']} ({date_short})",
            callback_data=f"hist:{r['id']}"
        )])
    await message.answer(
        "📁 <b>Ваша история поиска (хранится 7 дней):</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@osint_router.callback_query(F.data.startswith("hist:"))
async def cb_history_item(callback: CallbackQuery, bot: Bot):
    rep_id = int(callback.data.split(":", 1)[1])
    rep = await get_report_by_id(rep_id, callback.from_user.id)
    if not rep:
        await callback.answer("Отчёт удалён (прошло более 7 дней).", show_alert=True)
        return
    await callback.answer()
    txt_bytes = rep["content"].encode("utf-8")
    safe_name = "".join(c if c.isalnum() else "_" for c in rep["target"])[:25]
    doc = BufferedInputFile(txt_bytes, filename=f"osint_{safe_name}.txt")
    await bot.send_document(callback.message.chat.id, document=doc, caption=f"📁 Архивный отчёт по цели: <code>{html.escape(rep['target'])}</code>", parse_mode="HTML")


@osint_router.message(Command("quick"))
async def cmd_quick(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Укажите цель: <code>/quick цель</code>", parse_mode="HTML")
        return
    await enqueue_search(bot, message.chat.id, message.from_user.id, message.from_user.username, command.args, deep=False)


@osint_router.message(Command("deep"))
async def cmd_deep(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Укажите цель: <code>/deep цель</code>", parse_mode="HTML")
        return
    await enqueue_search(bot, message.chat.id, message.from_user.id, message.from_user.username, command.args, deep=True)


@osint_router.message(F.forward_from)
async def handle_forwarded(message: Message):
    u = message.forward_from
    if await is_target_blocked(str(u.id)) or (u.username and await is_target_blocked(u.username)):
        await message.answer("🔒 Информация по данному пользователю защищена администратором.")
        return
    reg = engine.estimate_tg_reg_date(u.id)
    await message.answer(
        f"👤 <b>Досье по пересланному сообщению</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"▪️ <b>Имя:</b> {html.escape(u.full_name)}\n"
        f"▪️ <b>Username:</b> @{u.username or 'скрыт'}\n"
        f"▪️ <b>Telegram ID:</b> <code>{u.id}</code>\n"
        f"▪️ <b>Дата регистрации:</b> <b>{reg['estimated_registration']}</b>",
        parse_mode="HTML"
    )


@osint_router.message(F.document | F.photo)
async def handle_exif(message: Message, bot: Bot):
    wait_sec = check_cooldown(message.from_user.id)
    if wait_sec > 0:
        await message.answer(f"⏳ Подождите {wait_sec} сек. перед следующим запросом.")
        return

    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    f_info = await bot.get_file(file_id)
    f_bytes = (await bot.download_file(f_info.file_path)).read()
    exif = engine.extract_exif(f_bytes)
    text_rep = json.dumps(exif, indent=2, ensure_ascii=False) if exif else "EXIF метаданные не найдены (отправьте фото файлом без сжатия)."
    await save_report(message.from_user.id, "EXIF Фото", "QUICK", text_rep)
    await message.answer(f"📷 <b>EXIF метаданные снимка:</b>\n<pre>{html.escape(text_rep[:3500])}</pre>", parse_mode="HTML")


@osint_router.message(F.text & ~F.text.startswith("/"))
async def handle_plain_text_target(message: Message):
    if await is_target_blocked(message.text):
        await message.answer("🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>", parse_mode="HTML")
        return

    t_type, clean_target = detect_target_type(message.text)
    if t_type == TargetType.UNKNOWN:
        await message.answer("❌ Не удалось распознать цель. Отправьте корректный ник, email, телефон, госномер авто, домен, IP или Имя Фамилию.")
        return

    token = uuid.uuid4().hex[:10]
    PENDING_TARGETS[token] = clean_target

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Коротко: 10 пунктов без воды (FREE)", callback_data=f"run:q:{token}")],
        [InlineKeyboardButton(text="🚀 Полное досье + Pivoting (DEEP)", callback_data=f"run:d:{token}")]
    ])
    await message.answer(
        f"🎯 <b>Распознана цель:</b> <code>{html.escape(clean_target)}</code>\n"
        f"📂 <b>Категория:</b> <b>{t_type.value.upper()}</b>\n\n"
        "Выберите формат разведки:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@osint_router.callback_query(F.data.startswith("run:"))
async def cb_run_selected_mode(callback: CallbackQuery, bot: Bot):
    _, mode_flag, token = callback.data.split(":")
    target = PENDING_TARGETS.get(token)
    if not target:
        await callback.answer("Запрос устарел, отправьте цель заново.", show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    await enqueue_search(
        bot=bot,
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        raw_target=target,
        deep=(mode_flag == "d")
    )