import asyncio
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError, TelegramNetworkError, TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

# UNDERGROUND Telegram game bot
# Python 3.11+, aiogram 3.x
# Set BOT_TOKEN as an environment variable. Never hard-code your token here.

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "underground.sqlite3")
NIGHT_SECONDS = 60
MORNING_SECONDS = 60
MIN_PLAYERS = 4
MAX_PLAYERS = 20

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("underground")

# Exact role counts explicitly agreed so far; other counts use a smooth
# interpolation: 2 guards, roughly 40% dolls, remainder visitors.
def role_counts(n: int):
    # User-approved anchor counts, with a steady progression for player
    # counts between the examples.
    dolls_by_count = {
        4: 1, 5: 2, 6: 2, 7: 2, 8: 3, 9: 3, 10: 4,
        11: 4, 12: 4, 13: 4, 14: 5, 15: 5, 16: 5,
        17: 6, 18: 6, 19: 7, 20: 7,
    }
    dolls = dolls_by_count[n]
    return 2, dolls, n - 2 - dolls

# The user has supplied 3 of 5 locations. The 2 remaining locations
# and the sixth item at Civil Defense Warehouse are intentionally pending.
LOCATIONS = [
    ("АТС", ["Ящик ГП-5", "Плакаты ГО", "Чайник", "Кружка ЧОПа", "Лом", "Ящик Нерехт"]),
    ("Поддом", ["Плед", "Журнал", "Ручка", "Чайник", "Панорамник", "Фонарик"]),
    ("Склад ГО", ["Ящик ГП-5", "Ящик ММ-1", "Ящик ГП-7", "Дозиметр ДП-5В", "Ящик АИ-2"]),
]

ROLE_GUARD = "ЧОП"
ROLE_DOLL = "Кукла"
ROLE_VISITOR = "Посетитель"

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("""CREATE TABLE IF NOT EXISTS players (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, started_private INTEGER DEFAULT 0
)""")
conn.commit()

@dataclass
class Player:
    user_id: int
    name: str
    role: str = ""
    alive: bool = True
    action: Optional[tuple] = None

@dataclass
class Game:
    chat_id: int
    chat_title: str
    players: Dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"  # lobby/night/morning/finished
    location: Optional[str] = None
    items: List[str] = field(default_factory=list)
    stolen: List[str] = field(default_factory=list)
    round_no: int = 0
    doll_votes: Dict[int, str] = field(default_factory=dict)
    guard_actions: Dict[int, tuple] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: Optional[asyncio.Task] = None

GAMES: Dict[int, Game] = {}
BOT: Optional[Bot] = None


def display_name(user) -> str:
    return (user.full_name or user.username or str(user.id)).replace("<", "‹").replace(">", "›")


def lobby_keyboard(chat_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎒 Войти в игру", callback_data=f"join:{chat_id}")
    kb.button(text="🚪 Выйти", callback_data=f"leave:{chat_id}")
    kb.adjust(2)
    return kb.as_markup()


def players_text(game: Game):
    names = [f"• {p.name}" for p in game.players.values()]
    return "\n".join(names) if names else "Пока никого нет"

async def announce(game: Game, text: str):
    await BOT.send_message(game.chat_id, text)

async def update_lobby(game: Game):
    await announce(game, f"🎭 <b>UNDERGROUND — набор игроков</b>\n\n"
                   f"Участников: <b>{len(game.players)}/{MAX_PLAYERS}</b>\n"
                   f"Минимум для старта: <b>{MIN_PLAYERS}</b>\n\n{players_text(game)}\n\n"
                   "Нажмите кнопку, чтобы войти или выйти.", reply_markup=lobby_keyboard(game.chat_id))

async def private_ready(user_id: int) -> bool:
    try:
        await BOT.send_message(user_id, "✅ Личка с UNDERGROUND подключена. Ты сможешь получать роль и игровые действия здесь.")
        return True
    except Exception:
        return False

async def send_role(player: Player, game: Game):
    text = (f"🎭 <b>Твоя роль: {player.role}</b>\n\n"
            f"Локация: <b>{game.location}</b>\n"
            f"Игра началась. Не раскрывай свою роль другим игрокам.")
    if player.role == ROLE_GUARD:
        text += "\n\nКаждую ночь ты можешь выбрать: проверить игрока или задержать игрока."
    elif player.role == ROLE_DOLL:
        other_dolls = [p.name for p in game.players.values() if p.role == ROLE_DOLL and p.user_id != player.user_id]
        text += "\n\nТвоя задача — вместе с другими куклами украсть всё имущество."
        if other_dolls:
            text += "\nДругие куклы: " + ", ".join(other_dolls)
    else:
        text += "\n\nТы можешь давать показания и обсуждать подозрения в общей группе. Победа вместе с ЧОП, если все куклы задержаны."
    await BOT.send_message(player.user_id, text)

async def begin_game(game: Game):
    async with game.lock:
        if game.phase != "lobby":
            return
        if len(game.players) < MIN_PLAYERS:
            await announce(game, f"❌ Для старта нужно минимум {MIN_PLAYERS} игрока. Сейчас: {len(game.players)}.")
            return
        if len(game.players) > MAX_PLAYERS:
            await announce(game, f"❌ Максимум игроков: {MAX_PLAYERS}.")
            return
        # Check that every participant has opened the bot before assigning roles.
        ready = []
        for p in game.players.values():
            if await private_ready(p.user_id):
                ready.append(p.user_id)
        if len(ready) != len(game.players):
            missing = [p.name for p in game.players.values() if p.user_id not in ready]
            await announce(game, "❌ Не удалось написать в личку всем игрокам. Каждый должен открыть бота и нажать Start.\n"
                                  "Не подключились: " + ", ".join(missing) + "\nНабор сохранён, попробуйте снова после подключения.")
            return
        n = len(game.players)
        guards, dolls, visitors = role_counts(n)
        roles = [ROLE_GUARD] * guards + [ROLE_DOLL] * dolls + [ROLE_VISITOR] * visitors
        random.shuffle(roles)
        for player, role in zip(game.players.values(), roles):
            player.role = role
            player.alive = True
            player.action = None
        loc_name, loc_items = random.choice(LOCATIONS)
        game.location = loc_name
        game.items = list(loc_items)
        game.stolen = []
        game.round_no = 0
        game.phase = "night"
        await announce(game, f"🚨 <b>ЭКСПЕДИЦИЯ НАЧАЛАСЬ!</b>\nЛокация выбрана случайно: <b>{loc_name}</b>\n"
                              f"Участников: {n}\nРоли распределены случайным образом.\n\n🌙 Первая ночь начинается. Длительность: 60 секунд.")
        for p in game.players.values():
            try:
                await send_role(p, game)
            except Exception:
                log.exception("Failed to send role to %s", p.user_id)
        await start_night(game)

async def start_night(game: Game):
    if game.phase == "finished":
        return
    game.phase = "night"
    game.round_no += 1
    game.doll_votes.clear()
    game.guard_actions.clear()
    for p in game.players.values():
        p.action = None
    await announce(game, f"🌙 <b>НОЧЬ №{game.round_no}</b>\nДлительность: 1 минута. Все действия — только в личке бота.")
    for p in game.players.values():
        if not p.alive:
            continue
        try:
            if p.role == ROLE_GUARD:
                kb = InlineKeyboardBuilder()
                kb.button(text="🔎 Проверить", callback_data=f"gcheck:{game.chat_id}")
                kb.button(text="🚔 Задержать", callback_data=f"garrest:{game.chat_id}")
                kb.adjust(1)
                await BOT.send_message(p.user_id, "🌙 Ночь. Выбери действие ЧОП:", reply_markup=kb.as_markup())
            elif p.role == ROLE_DOLL:
                kb = InlineKeyboardBuilder()
                for item in game.items:
                    kb.button(text=f"🧤 {item}", callback_data=f"dv:{game.chat_id}:{item}")
                kb.adjust(1)
                await BOT.send_message(p.user_id, "🌙 Ночь. Проголосуй за имущество, которое куклы хотят украсть:", reply_markup=kb.as_markup())
        except Exception:
            log.exception("Could not send night action")
    game.timer_task = asyncio.create_task(phase_timer(game, NIGHT_SECONDS, "night"))

async def phase_timer(game: Game, seconds: int, expected_phase: str):
    await asyncio.sleep(seconds)
    async with game.lock:
        if game.phase != expected_phase:
            return
        if expected_phase == "night":
            await resolve_night(game)
        elif expected_phase == "morning":
            await end_morning(game)

async def resolve_night(game: Game):
    # Aggregate one vote per living doll. Item with most votes is stolen.
    living_dolls = [p for p in game.players.values() if p.alive and p.role == ROLE_DOLL]
    counts: Dict[str, int] = {}
    for item in game.doll_votes.values():
        counts[item] = counts.get(item, 0) + 1
    stolen_now = None
    if counts:
        highest = max(counts.values())
        winners = sorted([item for item, count in counts.items() if count == highest])
        # A tie means no theft this round; do not randomly break a tie.
        if len(winners) == 1:
            stolen_now = winners[0]
            if stolen_now in game.items:
                game.items.remove(stolen_now)
                game.stolen.append(stolen_now)

    arrested_ids = set()
    checks = []
    for guard_id, action in game.guard_actions.items():
        kind, target_id = action
        target = game.players.get(target_id)
        if not target or not target.alive:
            continue
        if kind == "arrest":
            arrested_ids.add(target_id)
        elif kind == "check":
            checks.append((guard_id, target_id, target.role))

    # Reveal investigation results privately to each guard.
    for guard_id, target_id, role in checks:
        try:
            await BOT.send_message(guard_id, f"🔎 Результат проверки: <b>{game.players[target_id].name}</b> — роль <b>{role}</b>.")
        except Exception:
            pass

    # Arrest takes effect after night; announce role publicly at morning start.
    for uid in arrested_ids:
        game.players[uid].alive = False

    if stolen_now:
        theft_text = f"🧤 Куклы украли: <b>{stolen_now}</b>."
    elif counts and len(set(counts.values())) > 0:
        theft_text = "🧤 Куклы не смогли выбрать один предмет: ничья в голосовании. Имущество не украдено."
    else:
        theft_text = "🧤 Этой ночью куклы не совершили кражу."

    arrest_text = ""
    if arrested_ids:
        lines = [f"🚔 Задержан: <b>{game.players[uid].name}</b> — роль <b>{game.players[uid].role}</b>." for uid in arrested_ids]
        arrest_text = "\n" + "\n".join(lines)
    else:
        arrest_text = "\n🚔 Этой ночью задержаний не было."

    await announce(game, f"🌅 <b>НАСТУПИЛО УТРО</b>\n{theft_text}{arrest_text}\n\n"
                          f"📦 Украдено имущества: <b>{len(game.stolen)}/{len(game.stolen)+len(game.items)}</b>\n"
                          f"Осталось: <b>{len(game.items)}</b>")

    # Win conditions are checked after all night effects.
    if not game.items:
        await finish_game(game, "🎭 <b>ПОБЕДА КУКОЛ!</b> Они украли всё имущество локации.")
        return
    if not any(p.alive and p.role == ROLE_DOLL for p in game.players.values()):
        await finish_game(game, "🛡 <b>ПОБЕДА ЧОП И ПОСЕТИТЕЛЕЙ!</b> Все куклы задержаны.")
        return
    game.phase = "morning"
    game.timer_task = asyncio.create_task(phase_timer(game, MORNING_SECONDS, "morning"))

async def finish_game(game: Game, text: str):
    game.phase = "finished"
    await announce(game, text + "\n\nИгра завершена. Для новой партии используйте /newgame.")

async def end_morning(game: Game):
    if game.phase != "morning":
        return
    await announce(game, "🌙 <b>Утро закончилось. Наступает новая ночь.</b>")
    await start_night(game)

# Dispatcher and handlers
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    conn.execute("INSERT INTO players(user_id, username, first_name, started_private) VALUES(?,?,?,1) "
                 "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, started_private=1",
                 (user.id, user.username, user.first_name))
    conn.commit()
    await message.answer("👋 Добро пожаловать в <b>UNDERGROUND</b>!\n\nЛичка подключена. Здесь ты будешь получать свою роль и ночные действия.\n\nДобавь меня в игровую группу, чтобы начать экспедицию.")

@dp.message(Command("newgame"))
async def cmd_newgame(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда /newgame работает в игровой группе.")
        return
    old = GAMES.get(message.chat.id)
    if old and old.phase not in ("finished", "lobby"):
        await message.answer("Сначала закончите текущую игру.")
        return
    game = Game(chat_id=message.chat.id, chat_title=message.chat.title or "Группа")
    GAMES[message.chat.id] = game
    await update_lobby(game)

@dp.message(Command("join"))
async def cmd_join(message: Message):
    game = GAMES.get(message.chat.id)
    if not game or game.phase != "lobby":
        await message.answer("Сейчас нет открытого набора. Создать набор: /newgame")
        return
    await add_player(game, message.from_user, message)

async def add_player(game: Game, user, message_or_callback):
    if game.phase != "lobby":
        return
    if user.id in game.players:
        text = "Ты уже в списке участников."
    elif len(game.players) >= MAX_PLAYERS:
        text = f"Достигнут лимит — {MAX_PLAYERS} игроков."
    else:
        game.players[user.id] = Player(user_id=user.id, name=display_name(user))
        text = f"✅ {display_name(user)} присоединился к игре. Игроков: {len(game.players)}."
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text)
    else:
        await message_or_callback.answer(text, show_alert=False)
    await announce(game, f"👤 {text}")

@dp.callback_query(F.data.startswith("join:"))
async def cb_join(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    if not game:
        await callback.answer("Набор уже закрыт.", show_alert=True)
        return
    await add_player(game, callback.from_user, callback)

@dp.callback_query(F.data.startswith("leave:"))
async def cb_leave(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    if game and game.phase == "lobby":
        game.players.pop(callback.from_user.id, None)
        await callback.answer("Ты вышел из списка.")
        await announce(game, f"🚪 {display_name(callback.from_user)} вышел из набора. Участников: {len(game.players)}.")
    else:
        await callback.answer("Сейчас выйти нельзя.", show_alert=True)

@dp.message(Command("startgame"))
async def cmd_startgame(message: Message):
    game = GAMES.get(message.chat.id)
    if not game:
        await message.answer("Сначала откройте набор командой /newgame.")
        return
    await begin_game(game)

@dp.message(Command("status"))
async def cmd_status(message: Message):
    game = GAMES.get(message.chat.id)
    if not game:
        await message.answer("В этой группе сейчас нет игры. Создать: /newgame")
        return
    await message.answer(f"Состояние: {game.phase}\nИгроков: {len(game.players)}\nЛокация: {game.location or 'ещё не выбрана'}\nУкрадено: {len(game.stolen)}")

@dp.callback_query(F.data.startswith("gcheck:"))
async def cb_guard_check(callback: CallbackQuery):
    await guard_target_menu(callback, "check")

@dp.callback_query(F.data.startswith("garrest:"))
async def cb_guard_arrest(callback: CallbackQuery):
    await guard_target_menu(callback, "arrest")

async def guard_target_menu(callback: CallbackQuery, kind: str):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    guard = game.players.get(callback.from_user.id) if game else None
    if not game or game.phase != "night" or not guard or not guard.alive or guard.role != ROLE_GUARD:
        await callback.answer("Действие сейчас недоступно.", show_alert=True)
        return
    if callback.from_user.id in game.guard_actions:
        await callback.answer("Ты уже сделал действие этой ночью.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p in game.players.values():
        if p.alive and p.user_id != callback.from_user.id:
            kb.button(text=p.name, callback_data=f"gt:{chat_id}:{kind}:{p.user_id}")
    kb.adjust(1)
    await callback.message.answer("Выбери игрока:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("gt:"))
async def cb_guard_target(callback: CallbackQuery):
    _, chat_id_s, kind, target_s = callback.data.split(":", 3)
    chat_id, target_id = int(chat_id_s), int(target_s)
    game = GAMES.get(chat_id)
    guard = game.players.get(callback.from_user.id) if game else None
    target = game.players.get(target_id) if game else None
    if not game or game.phase != "night" or not guard or guard.role != ROLE_GUARD or not guard.alive:
        await callback.answer("Действие недоступно.", show_alert=True); return
    if callback.from_user.id in game.guard_actions:
        await callback.answer("Ты уже сделал действие этой ночью.", show_alert=True); return
    if not target or not target.alive or target_id == callback.from_user.id:
        await callback.answer("Игрок недоступен.", show_alert=True); return
    game.guard_actions[callback.from_user.id] = (kind, target_id)
    await callback.message.edit_text("✅ Действие принято. Результат будет утром.")
    await callback.answer()

@dp.callback_query(F.data.startswith("dv:"))
async def cb_doll_vote(callback: CallbackQuery):
    _, chat_id_s, item = callback.data.split(":", 2)
    chat_id = int(chat_id_s)
    game = GAMES.get(chat_id)
    player = game.players.get(callback.from_user.id) if game else None
    if not game or game.phase != "night" or not player or not player.alive or player.role != ROLE_DOLL:
        await callback.answer("Голосование недоступно.", show_alert=True); return
    if item not in game.items:
        await callback.answer("Этот предмет уже недоступен.", show_alert=True); return
    game.doll_votes[callback.from_user.id] = item
    await callback.answer("Голос принят. Его результат будет объявлен утром.")
    try:
        await callback.message.edit_text(f"✅ Твой голос за предмет «{item}» принят. Можно изменить выбор до конца ночи, нажав /vote в личке.")
    except Exception:
        pass

@dp.message(Command("vote"))
async def cmd_vote(message: Message):
    if message.chat.type != "private":
        return
    player_id = message.from_user.id
    game = next((g for g in GAMES.values() if g.phase == "night" and player_id in g.players and g.players[player_id].alive and g.players[player_id].role == ROLE_DOLL), None)
    if not game:
        await message.answer("Сейчас у тебя нет активного голосования."); return
    kb = InlineKeyboardBuilder()
    for item in game.items:
        kb.button(text=f"🧤 {item}", callback_data=f"dv:{game.chat_id}:{item}")
    kb.adjust(1)
    await message.answer("Выбери имущество:", reply_markup=kb.as_markup())

@dp.message(F.chat.type == "private")
async def private_fallback(message: Message):
    await message.answer("Игровые действия появляются здесь автоматически во время ночи. Команда /vote позволяет куклам изменить голос.")

async def main():
    global BOT
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь токен как переменную окружения BOT_TOKEN.")
    BOT = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    log.info("UNDERGROUND bot starting")
    # Telegram may rate-limit getMe during startup. Wait instead of crashing.
    retry_delay = 5
    while True:
        try:
            await dp.start_polling(BOT, handle_signals=True)
            break
        except TelegramRetryAfter as exc:
            delay = max(float(exc.retry_after), 1.0) + 3.0
            log.warning("Telegram rate limit during startup: waiting %.0f seconds", delay)
            await asyncio.sleep(delay)
        except TelegramUnauthorizedError:
            log.error("BOT_TOKEN rejected by Telegram. Check Railway Variables.")
            raise
        except (TelegramNetworkError, TelegramAPIError) as exc:
            log.warning("Telegram connection error: %s; retrying in %s seconds", exc, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)

if __name__ == "__main__":
    asyncio.run(main())
