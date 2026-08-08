import json
import os
import random
import re
import sqlite3
import threading
import urllib.parse

import requests
import telebot
from flask import Flask
from groq import Groq
from PIL import Image, ImageEnhance
from telebot import types

# ================================================================
# 1. БАЗА ДАННЫХ
# ================================================================

DB_PATH = os.environ.get("DB_PATH", "leaderboard.db")


def _add_column_if_missing(cursor, table, column, coltype):
  try:
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
  except sqlite3.OperationalError:
    pass  # колонка уже существует


def init_db():
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            score INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            messages_count INTEGER DEFAULT 0,
            settings TEXT DEFAULT 'RU'
        )
    """)
  # Миграции для новой системы прогресса (XP/уровни/монеты)
  _add_column_if_missing(cursor, "scores", "xp", "INTEGER DEFAULT 0")
  _add_column_if_missing(cursor, "scores", "level", "INTEGER DEFAULT 1")
  _add_column_if_missing(cursor, "scores", "coins", "INTEGER DEFAULT 0")
  _add_column_if_missing(cursor, "scores", "active_mode", "TEXT DEFAULT 'default'")
  _add_column_if_missing(cursor, "scores", "joined_at", "TEXT")
  # Статистика для игры "GOAT Угадай"
  _add_column_if_missing(cursor, "scores", "guess_games", "INTEGER DEFAULT 0")
  _add_column_if_missing(cursor, "scores", "guess_wins", "INTEGER DEFAULT 0")
  _add_column_if_missing(
      cursor, "scores", "best_guess_questions", "INTEGER DEFAULT 0"
  )
  conn.commit()
  conn.close()


init_db()


def ensure_user(user_id, username):
  """Гарантирует наличие строки пользователя в БД."""
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("SELECT user_id FROM scores WHERE user_id = ?", (user_id,))
  if not cursor.fetchone():
    cursor.execute(
        "INSERT INTO scores (user_id, username, score, streak, games, wins,"
        " messages_count, xp, level, coins, active_mode, settings)"
        " VALUES (?, ?, 0, 0, 0, 0, 0, 0, 1, 0, 'default', 'RU')",
        (user_id, username),
    )
    conn.commit()
  else:
    cursor.execute(
        "UPDATE scores SET username = ? WHERE user_id = ?", (username, user_id)
    )
    conn.commit()
  conn.close()


def xp_needed_for_level(level):
  return level * 100


def add_xp(user_id, username, amount):
  """Начисляет XP, обрабатывает повышение уровня и награду монетами.

  Возвращает (leveled_up: bool, new_level: int, coins: int, xp: int)."""
  ensure_user(user_id, username)
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT xp, level, coins FROM scores WHERE user_id = ?", (user_id,)
  )
  row = cursor.fetchone()
  xp, level, coins = row if row else (0, 1, 0)

  xp += amount
  leveled_up = False
  while xp >= xp_needed_for_level(level):
    xp -= xp_needed_for_level(level)
    level += 1
    coins += 100
    leveled_up = True

  cursor.execute(
      "UPDATE scores SET xp = ?, level = ?, coins = ? WHERE user_id = ?",
      (xp, level, coins, user_id),
  )
  conn.commit()
  conn.close()
  return leveled_up, level, coins, xp


def add_coins(user_id, amount):
  """Начисляет монеты напрямую (например, бонус за победу), не трогая XP."""
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "UPDATE scores SET coins = coins + ? WHERE user_id = ?", (amount, user_id)
  )
  conn.commit()
  conn.close()


def update_guess_stats(user_id, won, questions_used):
  """Обновляет статистику игры «GOAT Угадай»: сыграно, угадано, лучший
  результат (минимум вопросов при победе)."""
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT best_guess_questions FROM scores WHERE user_id = ?", (user_id,)
  )
  row = cursor.fetchone()
  best = row[0] if row and row[0] else 0

  if won:
    new_best = questions_used if best == 0 else min(best, questions_used)
    cursor.execute(
        "UPDATE scores SET guess_games = guess_games + 1, guess_wins ="
        " guess_wins + 1, best_guess_questions = ? WHERE user_id = ?",
        (new_best, user_id),
    )
  else:
    cursor.execute(
        "UPDATE scores SET guess_games = guess_games + 1 WHERE user_id = ?",
        (user_id,),
    )
  conn.commit()
  conn.close()


def get_profile_row(user_id):
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT score, streak, games, wins, messages_count, xp, level, coins,"
      " guess_games, guess_wins, best_guess_questions FROM scores WHERE"
      " user_id = ?",
      (user_id,),
  )
  row = cursor.fetchone()
  cursor.execute(
      "SELECT COUNT(*) FROM scores WHERE score > (SELECT COALESCE(score, 0)"
      " FROM scores WHERE user_id = ?)",
      (user_id,),
  )
  place = cursor.fetchone()[0] + 1
  conn.close()
  return row, place


import base64
import io


def fetch_pollinations_image(url, timeout=120, retries=2):
  """Скачивает сгенерированное изображение с Pollinations сами, а не отдаём
  ссылку напрямую в Telegram. Генерация (особенно img2img/kontext) может
  занимать больше времени, чем Telegram готов ждать при скачивании фото по
  URL — из-за этого редактирование фото часто вообще не приходило"""
  last_error = None
  for attempt in range(retries):
    try:
      resp = requests.get(url, timeout=timeout)
      if resp.status_code == 200 and resp.content:
        return resp.content
      last_error = f"HTTP {resp.status_code}"
    except Exception as e:
      last_error = str(e)
    print(f"Ошибка скачивания изображения (попытка {attempt + 1}/{retries}):"
          f" {last_error}")
  return None


def render_bar(current, total, length=10):
  if total <= 0:
    filled = 0
  else:
    filled = min(length, int(length * current / total))
  return "▰" * filled + "▱" * (length - filled)


# ================================================================
# 2. FLASK ДЛЯ 24/7 РАБОТЫ
# ================================================================
app = Flask("")


@app.route("/")
def home():
  return "🚀 Bot Server is active and running smoothly!"


# ================================================================
# 3. ОСНОВНАЯ НАСТРОЙКА БОТА
# ================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
  print("⚠️ Ошибка: BOT_TOKEN не найден в Secrets!")

bot = telebot.TeleBot(BOT_TOKEN)

groq_api_key = os.environ.get("GROQ_API_KEY")
if not groq_api_key:
  print("⚠️ Предупреждение: GROQ_API_KEY не найден в Secrets!")

groq_client = Groq(api_key=groq_api_key)
GROQ_MODEL = os.environ.get(
    "GROQ_MODEL", "llama-3.3-70b-versatile"
)  # ⚠️ отключается 16.08.26 — при необходимости переопределить через Secrets

CHANNEL_ID = "@goatai_news"
CHANNEL_URL = "https://t.me/goatai_news"
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))


def notify_admin_error(context, error_text):
  """Присылает админу техническую деталь ошибки в Telegram, чтобы не лезть
  в серверные логи для диагностики."""
  if not ADMIN_ID:
    return
  try:
    bot.send_message(
        ADMIN_ID,
        f"⚠️ <b>Ошибка [{context}]</b>\n<code>{str(error_text)[:900]}</code>",
        parse_mode="HTML",
    )
  except Exception as notify_err:
    print(f"Не удалось уведомить админа об ошибке: {notify_err}")

user_modes = {}      # chat_id -> ключ роли GOAT Chat (в памяти, для скорости)
game_sessions = {}   # user_id -> состояние текущей игры "Правда или Ложь"
guess_sessions = {}  # user_id -> состояние игры "GOAT Угадай"
user_states = {}     # user_id -> строка состояния ожидания (фото/текст)

input_path = "input_photo.jpg"
output_path = "output_no_bg.png"
temp_in = "temp_in.jpg"
temp_out = "temp_out.jpg"

# ================================================================
# 4. РОЛИ ДЛЯ GOAT CHAT
# ================================================================
ROLES = {
    "assistant": {
        "name": "🤖 Ассистент",
        "show_header": False,
        "system": (
            "Ты — вежливый, умный и эрудированный ИИ-помощник в Telegram."
            " Отвечай чисто, структурированно, без лишней воды и без"
            " шапок-приписок. Пиши СТРОГО на русском языке."
        ),
    },
    "coder": {
        "name": "💻 Кодер",
        "show_header": True,
        "system": (
            "Ты опытный программист и ментор. Помогаешь находить ошибки в"
            " коде, объясняешь сложные технические вещи простыми словами,"
            " подсказываешь по логике скриптов и ботов. Пиши четко и строго"
            " на русском языке."
        ),
    },
    "marketer": {
        "name": "📈 Маркетолог",
        "show_header": True,
        "system": (
            "Ты крутой маркетолог и эксперт по продвижению в соцсетях."
            " Помогаешь придумать воронки, стратегии привлечения аудитории,"
            " офферы и идеи для роста просмотров. Пиши по делу и строго на"
            " русском языке."
        ),
    },
    "screenwriter": {
        "name": "🎬 Сценарист",
        "show_header": True,
        "system": (
            "Ты креативный сценарист и продюсер вирусных видео. Помогаешь"
            " придумывать идеи для контента, хуки, сюжеты с балансом юмора,"
            " драмы и экшена. Пиши энергично и строго на русском языке."
        ),
    },
    "tutor": {
        "name": "🎓 Репетитор",
        "show_header": True,
        "system": (
            "Ты терпеливый и внимательный репетитор. Объясняешь сложные темы"
            " простыми словами, приводишь примеры, проверяешь понимание"
            " наводящими вопросами и разбиваешь сложные темы на понятные"
            " шаги. Пиши строго на русском языке."
        ),
    },
    "musician": {
        "name": "🎵 Музыкант",
        "show_header": True,
        "system": (
            "Ты опытный музыкант, битмейкер и саунд-продюсер. Помогаешь"
            " придумывать концепты треков, структуру песни, тексты, рифмы,"
            " гармонию и атмосферу. Пиши энергично и строго на русском"
            " языке."
        ),
    },
    "psychologist": {
        "name": "🧘 Психолог",
        "show_header": True,
        "system": (
            "Ты эмпатичный, профессиональный и мягкий психолог. Внимательно"
            " слушаешь, задаешь наводящие вопросы, помогаешь человеку"
            " разобраться в его чувствах и поддерживаешь его. Пиши на"
            " русском языке."
        ),
    },
    "rude": {
        "name": "🤬 Грубиян",
        "show_header": True,
        "system": (
            "Ты — дерзкий, прямолинейный персонаж без фильтров, реально"
            " материшься в речи — используешь русский мат свободно, в"
            " каждом сообщении, как обычную часть своей речи, а не редкую"
            " приправу. Пиши матерные слова ПОЛНОСТЬЮ, без звёздочек и"
            " пропущенных букв — цензурить самого себя нельзя. Используй"
            " РАЗНООБРАЗНЫЙ мат, не повторяй одно и то же слово в каждом"
            " сообщении: чередуй разные корни и формы (блять, сука, хуй,"
            " пиздец, ебать, нахуй, ёбаный, охуеть, заебал и т.п. — по"
            " ситуации и смыслу), как это делает живой человек, а не"
            " зацикливайся на одном-двух словах. Общаешься с пользователем"
            " на «ты», грубо, с сарказмом и чёрным юмором. При этом ты"
            " всегда чётко и по существу отвечаешь на вопрос пользователя —"
            " коротко, ясно, без воды и путаных оборотов, просто делаешь"
            " это матерно и без обиняков. Не оскорбляешь пользователя лично"
            " и не переходишь на темы дискриминации — мат используешь как"
            " экспрессию речи (междометия, усилители, связки), а не как"
            " оскорбления в адрес человека. Пиши строго на русском языке."
        ),
    },
}
DEFAULT_ROLE_KEY = "assistant"


# ================================================================
# 6. ВИКТОРИНА «ПРАВДА ИЛИ ЛОЖЬ» — база вопросов
# ================================================================
QUESTIONS = [
    {
        "text": (
            "В Сингапуре официально запрещено ввозить и продавать жевательную"
            " резинку?"
        ),
        "truth": True,
        "explanation": (
            "Сингапур славится строгими законами о чистоте. Импорт и продажа"
            " жвачки запрещены с 1992 года для порядка на улицах."
        ),
    },
    {
        "text": (
            "Кот в Японии официально занимал должность начальника"
            " железнодорожной станции?"
        ),
        "truth": True,
        "explanation": (
            "Кошка по кличке Тама спасла от банкротства станцию Кинокава,"
            " работая «начальником» и привлекая туристов."
        ),
    },
    {
        "text": (
            "В Великобритании разрешено законно убивать шотландцев из лука"
            " по пятницам?"
        ),
        "truth": False,
        "explanation": (
            "Это популярный городской миф и старая шутка, никаких подобных"
            " законов в UK нет."
        ),
    },
    {
        "text": (
            "Первый в мире сайт (info.cern.ch) до сих пор работает по своему"
            " оригинальному адресу?"
        ),
        "truth": True,
        "explanation": (
            "Самый первый веб-сайт, созданный Т. Бернерсом-Ли в 1991 году,"
            " доступен по сей день."
        ),
    },
    {
        "text": (
            "Во Франции по закону запрещено называть свиней именем"
            " «Наполеон»?"
        ),
        "truth": True,
        "explanation": (
            "Во Франции действует старый закон, запрещающий оскорблять"
            " память правителей, включая кличку свиней."
        ),
    },
    {
        "text": "У осьминогов три сердца и голубая кровь?",
        "truth": True,
        "explanation": (
            "У них три сердца, а кровь голубая из-за медьсодержащего белка"
            " гемоцианина."
        ),
    },
    {
        "text": (
            "В Швейцарии после 22:00 по закону запрещено смывать унитаз в"
            " квартирах?"
        ),
        "truth": False,
        "explanation": (
            "Это миф. Действуют общие правила тишины, но запрета на"
            " пользование туалетом ночью нет."
        ),
    },
    {
        "text": (
            "Бананы растут на гигантских деревьях с крепким деревянным"
            " стволом?"
        ),
        "truth": False,
        "explanation": (
            "Банановые растения — это гигантские травянистые многолетники"
            " без древесного ствола."
        ),
    },
    {
        "text": (
            "На Сардинии владельцам собак запрещено выгуливать их реже трех"
            " раз в день?"
        ),
        "truth": True,
        "explanation": (
            "В некоторых коммунах Италии и на Сардинии действуют жесткие"
            " правила ухода за питомцами."
        ),
    },
    {
        "text": "Молния никогда не ударяет дважды в одно и то же место?",
        "truth": False,
        "explanation": (
            "Молния часто бьет в одно и то же место несколько раз, особенно"
            " в высокие здания."
        ),
    },
    {
        "text": "В Исландии полностью отсутствуют комары?",
        "truth": True,
        "explanation": (
            "Из-за уникального климата и резких перепадов температур в"
            " Исландии нет комаров."
        ),
    },
    {
        "text": "Акулы появились на Земле раньше, чем деревья?",
        "truth": True,
        "explanation": (
            "Акулы существуют более 400 млн лет, а первые деревья появились"
            " примерно на 50 млн лет позже."
        ),
    },
    {
        "text": (
            "У коровы может быть лучший друг, и они сильно скучают при"
            " разлуке?"
        ),
        "truth": True,
        "explanation": (
            "Научно доказано, что коровы привязываются друг к другу и"
            " испытывают стресс отдельно."
        ),
    },
    {
        "text": "Мёд может храниться тысячелетиями и никогда не портится?",
        "truth": True,
        "explanation": (
            "Благодаря низкой влажности и кислотности мёд обладает"
            " природными консервирующими свойствами."
        ),
    },
    {
        "text": "Эйфелева башня становится выше в холодные зимние месяцы?",
        "truth": False,
        "explanation": (
            "Наоборот, от тепла металл расширяется летом (выше на ~15 см), а"
            " зимой сжимается."
        ),
    },
    {
        "text": "Улитка может спать непрерывно в течение трех лет?",
        "truth": True,
        "explanation": (
            "При неблагоприятных условиях (засуха, холода) улитки могут"
            " впадать в спячку на срок до 3 лет."
        ),
    },
    {
        "text": "Фламинго рождаются с розовым оперением?",
        "truth": False,
        "explanation": (
            "Птенцы рождаются серыми или белыми. Розовый цвет они"
            " приобретают со временем из-за пигмента в водорослях и рачках,"
            " которых они едят."
        ),
    },
    {
        "text": (
            "Земля — единственная планета в Солнечной системе, где идут"
            " дожди?"
        ),
        "truth": False,
        "explanation": (
            "На Титане идут дожди из метана, а на Венере — из серной"
            " кислоты (правда, испаряющиеся до поверхности)."
        ),
    },
    {
        "text": (
            "Человеческий мозг генерирует достаточно энергии для питания"
            " лампочки?"
        ),
        "truth": True,
        "explanation": (
            "В бодрствующем состоянии мозг вырабатывает около 20 ватт"
            " электрической энергии, чего хватит для небольшой лампы."
        ),
    },
    {
        "text": "В Австралии кенгуру больше, чем людей?",
        "truth": True,
        "explanation": (
            "Популяция кенгуру в Австралии значительно превышает"
            " численность человеческого населения."
        ),
    },
    {
        "text": "Крокодилы могут высовывать язык наружу?",
        "truth": False,
        "explanation": (
            "У крокодилов язык прирос к нижней челюсти специальной"
            " мембраной, поэтому они не могут его высунуть."
        ),
    },
    {
        "text": "Арахис — это орех?",
        "truth": False,
        "explanation": (
            "Арахис на самом деле является бобовым растением, родственником"
            " фасоли и гороха, а не орехом."
        ),
    },
    {
        "text": "У кошек на передних лапах больше пальцев, чем на задних?",
        "truth": True,
        "explanation": (
            "У кошек обычно по 5 пальцев на передних лапах (включая"
            " прибылой палец) и по 4 на задних."
        ),
    },
    {
        "text": "Великая Китайская стена видна из космоса невооруженным глазом?",
        "truth": False,
        "explanation": (
            "Это миф. Стена слишком узкая и сливается с ландшафтом,"
            " разглядеть её без оптических приборов с орбиты невозможно."
        ),
    },
    {
        "text": "Пингвины умеют летать под водой?",
        "truth": True,
        "explanation": (
            "Хотя пингвины не летают по воздуху, их движения под водой"
            " очень напоминают полет птиц в небе."
        ),
    },
    {
        "text": "Абсолютный ноль температуры равен -273,15 градусам Цельсия?",
        "truth": True,
        "explanation": (
            "Да, это минимально возможная теоретическая температура во"
            " Вселенной, при которой прекращается тепловое движение атомов."
        ),
    },
    {
        "text": "В Сахаре никогда не бывает снега?",
        "truth": False,
        "explanation": (
            "В некоторых районах пустыни Сахара (например, в районе города"
            " Айн-Сефра) изредка выпадает снег."
        ),
    },
    {
        "text": "Сердце креветки расположено у нее в голове?",
        "truth": True,
        "explanation": (
            "Анатомия креветок устроена так, что их сердце и основные"
            " органы находятся в головном отделе."
        ),
    },
    {
        "text": "Золотая рыбка обладает памятью ровно в 3 секунды?",
        "truth": False,
        "explanation": (
            "Это миф. Научные эксперименты доказали, что золотые рыбки"
            " помнят события и могут обучаться в течение нескольких"
            " месяцев."
        ),
    },
    {
        "text": "У древесных лягушек бывает прозрачная кожа?",
        "truth": True,
        "explanation": (
            "Некоторые виды стеклянных лягушек обладают полностью"
            " прозрачной брюшной стенкой, через которую видны внутренние"
            " органы."
        ),
    },
    {
        "text": "В древнем Риме зубную пасту делали из мочи?",
        "truth": True,
        "explanation": (
            "Римляне использовали аммиак, содержащийся в старой моче, для"
            " отбеливания зубов и чистки полости рта."
        ),
    },
    {
        "text": "Павуки (пауки-птицееды) могут жить без еды больше года?",
        "truth": True,
        "explanation": (
            "Крупные пауки-птицееды благодаря медленному обмену веществ"
            " способны голодать до 12-18 месяцев, выпивая лишь воду."
        ),
    },
    {
        "text": "Вода проводит электрический ток в чистом виде без примесей?",
        "truth": False,
        "explanation": (
            "Дистиллированная (абсолютно чистая) вода практически не"
            " проводит ток. Ток проводят растворенные в ней соли и"
            " минералы."
        ),
    },
    {
        "text": "Ягоды арбуза ботанически считаются тыквинами?",
        "truth": True,
        "explanation": (
            "С точки зрения ботаники плод арбуза классифицируется как"
            " многосемянная тыквина (родственник дыни и тыквы)."
        ),
    },
    {
        "text": "В кубике Рубика ровно 43 квинтиллиона возможных комбинаций?",
        "truth": True,
        "explanation": (
            "Количество возможных конфигураций классического кубика Рубика"
            " составляет 43 252 003 274 489 856 000."
        ),
    },
]


# ================================================================
# 6б. ИГРА "GOAT УГАДАЙ" — локальная база персонажей + фильтрация
# ================================================================
# Признаки: у каждого персонажа все ключи заполнены True/False.
# real — реальный человек; animal — животное; остальные — сфера/категория.
GUESS_FEATURE_KEYS = [
    "real",
    "male",
    "animal",
    "sport",
    "football",
    "basketball",
    "mma",
    "blogger",
    "musician",
    "actor",
    "politician",
    "videogame_character",
    "movie_character",
    "cartoon_character",
    "superhero",
    "anime_character",
    "from_russia",
    "from_usa",
    "over_30",
]

GUESS_FEATURE_QUESTIONS = {
    "real": "Это реальный человек?",
    "male": "Это мужчина?",
    "animal": "Это животное?",
    "sport": "Он(-а) связан(-а) со спортом?",
    "football": "Он(-а) связан(-а) с футболом?",
    "basketball": "Он(-а) связан(-а) с баскетболом?",
    "mma": "Он(-а) связан(-а) с UFC или MMA?",
    "blogger": "Он(-а) известен(-на) в интернете (блогер/стример)?",
    "musician": "Он(-а) музыкант?",
    "actor": "Он(-а) актёр/актриса?",
    "politician": "Он(-а) политик?",
    "videogame_character": "Это персонаж из видеоигры?",
    "movie_character": "Это персонаж из фильма?",
    "cartoon_character": "Это персонаж из мультфильма?",
    "superhero": "Он(-а) обладает суперспособностями?",
    "anime_character": "Это персонаж из аниме?",
    "from_russia": "Он(-а) известен(-на) в первую очередь в России?",
    "from_usa": "Он(-а) из США?",
    "over_30": "Ему/ей (или на вид) больше 30 лет?",
}

# Порядок предпочтений для самых первых вопросов — задаём общие признаки
# раньше специфичных, чтобы быстрее отсекать целые категории.
GUESS_PRIORITY_ORDER = [
    "real",
    "animal",
    "male",
    "sport",
    "blogger",
    "musician",
    "actor",
    "football",
    "basketball",
    "mma",
    "politician",
    "videogame_character",
    "movie_character",
    "cartoon_character",
    "superhero",
    "anime_character",
    "from_russia",
    "from_usa",
    "over_30",
]


def _char(name, **kwargs):
  entry = {key: False for key in GUESS_FEATURE_KEYS}
  entry["name"] = name
  entry.update(kwargs)
  return entry


CHARACTERS = [
    # --- Футболисты ---
    _char("Криштиану Роналду", real=True, male=True, sport=True, football=True, over_30=True),
    _char("Лионель Месси", real=True, male=True, sport=True, football=True, over_30=True),
    _char("Неймар", real=True, male=True, sport=True, football=True, over_30=True),
    _char("Килиан Мбаппе", real=True, male=True, sport=True, football=True, over_30=True),
    _char("Эрлинг Холанд", real=True, male=True, sport=True, football=True, from_usa=False),
    _char("Роналдиньо", real=True, male=True, sport=True, football=True, over_30=True),
    # --- MMA / UFC ---
    _char("Хабиб Нурмагомедов", real=True, male=True, sport=True, mma=True, from_russia=True, over_30=True),
    _char("Конор Макгрегор", real=True, male=True, sport=True, mma=True, over_30=True),
    _char("Джон Джонс", real=True, male=True, sport=True, mma=True, from_usa=True, over_30=True),
    _char("Александр Волкановски", real=True, male=True, sport=True, mma=True, over_30=True),
    # --- Баскетбол ---
    _char("Леброн Джеймс", real=True, male=True, sport=True, basketball=True, from_usa=True, over_30=True),
    _char("Майкл Джордан", real=True, male=True, sport=True, basketball=True, from_usa=True, over_30=True),
    _char("Стефен Карри", real=True, male=True, sport=True, basketball=True, from_usa=True, over_30=True),
    # --- Музыканты ---
    _char("Эминем", real=True, male=True, musician=True, from_usa=True, over_30=True),
    _char("Дрейк", real=True, male=True, musician=True, from_usa=True, over_30=True),
    _char("Билли Айлиш", real=True, male=False, musician=True, from_usa=True, over_30=False),
    _char("Тейлор Свифт", real=True, male=False, musician=True, from_usa=True, over_30=True),
    _char("The Weeknd", real=True, male=True, musician=True, from_usa=True, over_30=True),
    _char("Моргенштерн", real=True, male=True, musician=True, blogger=True, from_russia=True, over_30=False),
    _char("Тимати", real=True, male=True, musician=True, from_russia=True, over_30=True),
    _char("Ariana Grande", real=True, male=False, musician=True, from_usa=True, over_30=True),
    # --- Актёры ---
    _char("Леонардо ДиКаприо", real=True, male=True, actor=True, from_usa=True, over_30=True),
    _char("Том Круз", real=True, male=True, actor=True, from_usa=True, over_30=True),
    _char("Уилл Смит", real=True, male=True, actor=True, from_usa=True, over_30=True),
    _char("Джонни Депп", real=True, male=True, actor=True, from_usa=True, over_30=True),
    _char("Дуэйн Джонсон", real=True, male=True, actor=True, sport=True, from_usa=True, over_30=True),
    _char("Скарлетт Йоханссон", real=True, male=False, actor=True, from_usa=True, over_30=True),
    # --- Блогеры / стримеры ---
    _char("MrBeast", real=True, male=True, blogger=True, from_usa=True, over_30=False),
    _char("PewDiePie", real=True, male=True, blogger=True, over_30=True),
    _char("Ивангай", real=True, male=True, blogger=True, from_russia=True, over_30=True),
    _char("A4", real=True, male=True, blogger=True, from_russia=True, over_30=False),
    # --- Политики ---
    _char("Владимир Путин", real=True, male=True, politician=True, from_russia=True, over_30=True),
    _char("Дональд Трамп", real=True, male=True, politician=True, from_usa=True, over_30=True),
    _char("Барак Обама", real=True, male=True, politician=True, from_usa=True, over_30=True),
    # --- Персонажи видеоигр ---
    _char("Марио", real=False, male=True, videogame_character=True),
    _char("Соник", real=False, male=True, videogame_character=True, animal=True),
    _char("Кратос", real=False, male=True, videogame_character=True, over_30=True),
    _char("Мастер Чиф", real=False, male=True, videogame_character=True, over_30=True),
    _char("Лара Крофт", real=False, male=False, videogame_character=True, over_30=True),
    _char("Геральт из Ривии", real=False, male=True, videogame_character=True, over_30=True),
    _char("Стив (Minecraft)", real=False, male=True, videogame_character=True),
    # --- Персонажи фильмов ---
    _char("Джек Воробей", real=False, male=True, movie_character=True, over_30=True),
    _char("Форрест Гамп", real=False, male=True, movie_character=True, over_30=True),
    _char("Йода", real=False, male=True, movie_character=True, over_30=True),
    _char("Нео (Матрица)", real=False, male=True, movie_character=True, over_30=True),
    _char("Гарри Поттер", real=False, male=True, movie_character=True, over_30=False),
    _char("Джокер", real=False, male=True, movie_character=True, superhero=True, over_30=True),
    # --- Персонажи мультфильмов ---
    _char("Губка Боб", real=False, male=True, cartoon_character=True, animal=False),
    _char("Микки Маус", real=False, male=True, cartoon_character=True, animal=True),
    _char("Гомер Симпсон", real=False, male=True, cartoon_character=True, over_30=True),
    _char("Багз Банни", real=False, male=True, cartoon_character=True, animal=True),
    _char("Скуби-Ду", real=False, male=True, cartoon_character=True, animal=True),
    _char("Кот Том", real=False, male=True, cartoon_character=True, animal=True),
    # --- Супергерои ---
    _char("Бэтмен", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=True),
    _char("Супермен", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=True),
    _char("Человек-паук", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=False),
    _char("Железный человек", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=True),
    _char("Чудо-женщина", real=False, male=False, superhero=True, movie_character=True, from_usa=True, over_30=True),
    _char("Дэдпул", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=True),
    _char("Тор", real=False, male=True, superhero=True, movie_character=True, over_30=True),
    _char("Халк", real=False, male=True, superhero=True, movie_character=True, from_usa=True, over_30=True),
    # --- Животные (реальные) ---
    _char("Хатико", real=True, male=True, animal=True),
    _char("Грампи Кэт", real=True, male=False, animal=True, blogger=True, from_usa=True),
    # --- Аниме-персонажи ---
    _char("Наруто Узумаки", real=False, male=True, anime_character=True, over_30=False),
    _char("Гоку", real=False, male=True, anime_character=True, over_30=True),
    _char("Луффи", real=False, male=True, anime_character=True, over_30=False),
    _char("Лайт Ягами", real=False, male=True, anime_character=True, over_30=False),
    _char("Сайтама", real=False, male=True, anime_character=True, over_30=True),
    _char("Леви Аккерман", real=False, male=True, anime_character=True, over_30=False),
]


GUESS_MODE_THRESHOLD = 5  # при таком и меньшем числе кандидатов — перебор по имени


def _guess_split_score(candidates, feature):
  """Возвращает (true_count, false_count) кандидатов по признаку —
  используется, чтобы выбрать вопрос, максимально близкий к 50/50."""
  true_count = sum(1 for c in candidates if c[feature])
  false_count = len(candidates) - true_count
  return true_count, false_count


def _choose_next_feature(candidates, asked_features):
  """Возвращает ключ следующего признака-вопроса или None, если больше нет
  признаков, которые реально разделяют оставшихся кандидатов (в этом случае
  вызывающий код должен перейти к прямому перебору имён)."""
  available = [f for f in GUESS_FEATURE_KEYS if f not in asked_features]
  if not available:
    return None

  scored = []
  for feature in available:
    true_count, false_count = _guess_split_score(candidates, feature)
    if true_count == 0 or false_count == 0:
      continue  # признак ничего не разделяет среди оставшихся
    balance = abs(true_count - false_count)
    priority = (
        GUESS_PRIORITY_ORDER.index(feature)
        if feature in GUESS_PRIORITY_ORDER
        else len(GUESS_PRIORITY_ORDER)
    )
    scored.append((balance, priority, feature))

  if not scored:
    return None  # ни один признак больше не делит кандидатов

  scored.sort(key=lambda x: (x[0], x[1]))
  return scored[0][2]


def _filter_candidates(candidates, feature, answer):
  if answer is None:  # "Не знаю" — не фильтруем
    return candidates
  return [c for c in candidates if c[feature] == answer]



def check_subscription(user_id):
  try:
    member = bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    if member.status in ["creator", "administrator", "member"]:
      return True
  except Exception as e:
    print(f"Ошибка проверки подписки: {e}")
  return False


def get_sub_markup():
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL),
      types.InlineKeyboardButton(
          "🔄 Проверить подписку", callback_data="check_sub"
      ),
  )
  return markup


def require_subscription(chat_id, user_id):
  """Возвращает True, если доступ разрешен. Иначе сама шлет сообщение."""
  if check_subscription(user_id):
    return True
  bot.send_message(
      chat_id,
      "🔒 Сначала подпишитесь на канал!",
      reply_markup=get_sub_markup(),
  )
  return False


# ================================================================
# 8. ГЛАВНОЕ МЕНЮ (компактное, 5 разделов)
# ================================================================
@bot.message_handler(commands=["start"])
def send_welcome(message):
  user_id = message.from_user.id
  username = message.from_user.first_name or "друг"
  ensure_user(user_id, username)

  if not check_subscription(user_id):
    bot.send_message(
        message.chat.id,
        "🔒 <b>Доступ заблокирован</b>\n\n"
        f"Чтобы пользоваться всеми функциями <b>GOAT AI</b>, подпишитесь на"
        f" наш официальный канал:\n👉 {CHANNEL_URL}\n\nПосле подписки нажмите"
        " кнопку ниже, чтобы разблокировать бота.",
        reply_markup=get_sub_markup(),
        parse_mode="HTML",
    )
    return

  send_main_menu(message.chat.id, username)


def send_main_menu(chat_id, user_name):
  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton("🎮 Игры", callback_data="games_menu"),
  )
  markup.add(
      types.InlineKeyboardButton("🐐 GOAT Chat", callback_data="goat_chat"),
      types.InlineKeyboardButton("🎨 Генерация", callback_data="ask_draw"),
      types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
      types.InlineKeyboardButton("🏆 Лидеры", callback_data="show_leaderboard"),
      types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
  )

  welcome_text = (
      f"🐐 <b>GOAT AI</b>\nДобро пожаловать, {user_name}!\n\n🎮 Загляните в"
      " «Игры» — там «Правда или Ложь» и новая «GOAT Угадай»!\n\nВыберите"
      " раздел:"
  )
  bot.send_message(chat_id, welcome_text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "games_menu")
def cb_games_menu(call):
  bot.answer_callback_query(call.id)
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton(
          "🧠 Правда или Ложь", callback_data="start_chat_game"
      ),
      types.InlineKeyboardButton("🔮 GOAT Угадай", callback_data="start_guess_game"),
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"),
  )
  bot.send_message(
      call.message.chat.id,
      "🎮 <b>Игры</b>\nВыберите игру:",
      reply_markup=markup,
      parse_mode="HTML",
  )


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def process_check_sub(call):
  user_id = call.from_user.id
  if check_subscription(user_id):
    bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
    try:
      bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
      pass
    send_main_menu(call.message.chat.id, call.from_user.first_name or "друг")
  else:
    bot.answer_callback_query(
        call.id, "❌ Вы еще не подписались на канал!", show_alert=True
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_menu")
def cb_back_to_menu(call):
  bot.answer_callback_query(call.id)
  user_states.pop(call.from_user.id, None)
  guess_sessions.pop(call.from_user.id, None)
  send_main_menu(call.message.chat.id, call.from_user.first_name or "друг")


@bot.message_handler(commands=["help"])
def send_help(message):
  help_text = (
      "📖 <b>Справочник по командам:</b>\n🔹 <code>/start</code> —"
      " Перезапустить бота\n🔹 <code>/game</code> — Игра «Правда или"
      " Ложь»\n🔹 <code>/top</code> — Таблица лидеров\n🔹"
      " <code>/admin</code> — Панель администратора (только для"
      " админа)\n\n🎮 <b>Игры:</b> «Правда или Ложь» и «GOAT Угадай» —"
      " раздел «🎮 Игры» в главном меню.\n🐐 <b>GOAT Chat:</b> выберите"
      " личность ИИ и просто пишите сообщения."
  )
  bot.send_message(message.chat.id, help_text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "help")
def cb_help(call):
  bot.answer_callback_query(call.id)
  send_help(call.message)


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
  user_id = message.from_user.id
  if ADMIN_ID and user_id != ADMIN_ID:
    bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
    return

  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("SELECT COUNT(*) FROM scores")
  total_users = cursor.fetchone()[0]
  conn.close()

  text = (
      f"🛠 <b>Панель администратора (GOAT AI)</b>\n\n👥 Всего пользователей в"
      f" базе: <b>{total_users}</b>\n🟢 Статус: Активен 24/7"
  )
  bot.send_message(message.chat.id, text, parse_mode="HTML")


# ================================================================
# 9. GOAT CHAT — выбор личности ИИ
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data == "goat_chat")
def cb_goat_chat(call):
  bot.answer_callback_query(call.id)
  user_id = call.from_user.id
  ensure_user(user_id, call.from_user.first_name or "друг")

  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("SELECT active_mode FROM scores WHERE user_id = ?", (user_id,))
  row = cursor.fetchone()
  conn.close()
  current_key = row[0] if row and row[0] in ROLES else DEFAULT_ROLE_KEY

  markup = types.InlineKeyboardMarkup(row_width=1)
  for key, role in ROLES.items():
    label = role["name"]
    if key == current_key:
      label = f"✅ {label}"
    markup.add(
        types.InlineKeyboardButton(label, callback_data=f"set_mode_{key}")
    )
  markup.add(
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")
  )

  current_name = ROLES[current_key]["name"]
  text = (
      "🐐 <b>GOAT Chat</b>\nВыберите личность ИИ, с которой хотите"
      f" общаться.\n\n⭐ Сейчас активна: <b>{current_name}</b>\n\nПосле"
      " выбора просто пишите сообщения в чат — бот будет отвечать в"
      " выбранном стиле."
  )
  bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("set_mode_"))
def cb_set_mode(call):
  bot.answer_callback_query(call.id)
  mode_key = call.data.replace("set_mode_", "")
  if mode_key in ROLES:
    user_id = call.from_user.id
    ensure_user(user_id, call.from_user.first_name or "друг")
    user_modes[call.message.chat.id] = mode_key
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE scores SET active_mode = ? WHERE user_id = ?",
        (mode_key, user_id),
    )
    conn.commit()
    conn.close()
    bot.send_message(
        call.message.chat.id,
        f"⭐ Используется как основной режим: <b>{ROLES[mode_key]['name']}</b>"
        "\n\nПросто напишите сообщение.",
        parse_mode="HTML",
    )


def send_level_up_message(chat_id, new_level, coins):
  bot.send_message(
      chat_id,
      f"🎉 <b>Новый уровень!</b>\nУровень {new_level}\nНаграда: +100"
      f" монет 🪙 (всего: {coins})",
      parse_mode="HTML",
  )


# ================================================================
# 11. ПРОФИЛЬ (уровень, XP, монеты)
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data == "profile")
def cb_profile(call):
  bot.answer_callback_query(call.id)
  user_id = call.from_user.id
  ensure_user(user_id, call.from_user.first_name or "друг")

  row, place = get_profile_row(user_id)
  if row:
    (
        score,
        streak,
        games,
        wins,
        messages_count,
        xp,
        level,
        coins,
        guess_games,
        guess_wins,
        best_guess_questions,
    ) = row
  else:
    score = streak = games = wins = messages_count = xp = coins = 0
    guess_games = guess_wins = best_guess_questions = 0
    level = 1

  needed = xp_needed_for_level(level)
  bar = render_bar(xp, needed)

  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("SELECT active_mode FROM scores WHERE user_id = ?", (user_id,))
  mode_row = cursor.fetchone()
  conn.close()
  active_mode_key = mode_row[0] if mode_row and mode_row[0] in ROLES else DEFAULT_ROLE_KEY
  active_mode_name = ROLES[active_mode_key]["name"]

  user_name = call.from_user.first_name or "друг"
  text = (
      f"👤 <b>{user_name}</b>\n"
      "━━━━━━━━━━━━━━━━\n"
      f"🏅 Уровень: {level}\n"
      f"⚡ XP\n{bar} {xp} / {needed}\n"
      f"🪙 Монеты: {coins}\n"
      f"🏆 Очков (игра): {score}\n"
      f"🔥 Серия: {streak}\n"
      f"💬 Сообщений: {messages_count}\n"
      f"🥇 Место в рейтинге: #{place}\n"
      f"🎭 Активный режим: {active_mode_name}\n"
      "━━━━━━━━━━━━━━━━\n"
      f"🔮 Игр «GOAT Угадай»: {guess_games}\n"
      f"🎯 Угадано: {guess_wins}\n"
      f"🧠 Лучший результат: {best_guess_questions or '—'}\n"
      "━━━━━━━━━━━━━━━━"
  )
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("🏆 Рейтинг", callback_data="show_leaderboard"),
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"),
  )
  bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")


# ================================================================
# 12. НАСТРОЙКИ (компактные)
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data == "settings")
def cb_settings(call):
  bot.answer_callback_query(call.id)
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton("🌍 Язык", callback_data="settings_lang"),
      types.InlineKeyboardButton("🤖 Модель ИИ", callback_data="settings_model"),
      types.InlineKeyboardButton(
          "🔔 Уведомления", callback_data="settings_notify"
      ),
      types.InlineKeyboardButton(
          "🧹 Очистить историю", callback_data="settings_clear"
      ),
      types.InlineKeyboardButton("ℹ️ О боте", callback_data="settings_about"),
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"),
  )
  bot.send_message(
      call.message.chat.id,
      "⚙️ <b>Настройки</b>",
      reply_markup=markup,
      parse_mode="HTML",
  )


@bot.callback_query_handler(func=lambda call: call.data == "settings_lang")
def cb_settings_lang(call):
  bot.answer_callback_query(
      call.id, "🌍 Сейчас доступен только русский язык 🇷🇺", show_alert=True
  )


@bot.callback_query_handler(func=lambda call: call.data == "settings_model")
def cb_settings_model(call):
  bot.answer_callback_query(
      call.id, "🤖 Текущая модель: Llama 3.3 70B (Groq)", show_alert=True
  )


@bot.callback_query_handler(func=lambda call: call.data == "settings_notify")
def cb_settings_notify(call):
  bot.answer_callback_query(call.id, "🔔 Уведомления включены", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "settings_clear")
def cb_settings_clear(call):
  user_id = call.from_user.id
  user_states.pop(user_id, None)
  game_sessions.pop(user_id, None)
  bot.answer_callback_query(call.id, "🧹 История очищена", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "settings_about")
def cb_settings_about(call):
  bot.answer_callback_query(call.id)
  bot.send_message(
      call.message.chat.id,
      "ℹ️ <b>GOAT AI</b>\nВерсия: 3.0\nИИ-помощник с личностями, честным"
      " анализом, викториной и системой прогресса.",
      parse_mode="HTML",
  )


# ================================================================
# 13. ВИКТОРИНА «ПРАВДА ИЛИ ЛОЖЬ»
# ================================================================
@bot.message_handler(commands=["game"])
def command_start_game(message):
  user_id = message.from_user.id
  if not require_subscription(message.chat.id, user_id):
    return
  start_quiz_session(message.chat.id, user_id)


@bot.message_handler(commands=["top"])
def command_top(message):
  user_id = message.from_user.id
  if not require_subscription(message.chat.id, user_id):
    return
  send_leaderboard(message.chat.id, user_id)


def start_quiz_session(chat_id, user_id):
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute("SELECT score FROM scores WHERE user_id = ?", (user_id,))
  row = cursor.fetchone()
  current_score = row[0] if row else 0
  conn.close()

  if user_id not in game_sessions:
    game_sessions[user_id] = {"score": current_score, "used_questions": []}

  session = game_sessions[user_id]
  session["score"] = current_score

  if len(session["used_questions"]) >= len(QUESTIONS):
    session["used_questions"] = []

  available_indices = [
      i for i in range(len(QUESTIONS)) if i not in session["used_questions"]
  ]
  q_index = random.choice(available_indices)
  session["used_questions"].append(q_index)
  session["q_index"] = q_index

  q_data = QUESTIONS[q_index]
  session["correct_ans"] = q_data["truth"]

  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton("✅ Правда", callback_data="ans_true"),
      types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false"),
  )
  markup.add(
      types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_menu")
  )

  text = (
      f"🎮 <b>Игра: Правда или Ложь</b>\n⭐ Ваши очки:"
      f" <b>{current_score}</b>\n\n📌 <b>Вопрос:</b>\n{q_data['text']}"
  )
  bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "start_chat_game")
def cb_start_chat_game(call):
  bot.answer_callback_query(call.id)
  start_quiz_session(call.message.chat.id, call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data in ["ans_true", "ans_false"])
def cb_game_answer(call):
  bot.answer_callback_query(call.id)
  user_id = call.from_user.id
  username = call.from_user.first_name or "Игрок"
  ensure_user(user_id, username)

  if user_id not in game_sessions:
    start_quiz_session(call.message.chat.id, user_id)
    return

  session = game_sessions[user_id]
  user_choice = call.data == "ans_true"
  q_index = session["q_index"]
  q_data = QUESTIONS[q_index]
  correct = q_data["truth"]

  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT score, streak, games, wins FROM scores WHERE user_id = ?",
      (user_id,),
  )
  row = cursor.fetchone()
  current_score, streak, games, wins = row if row else (0, 0, 0, 0)

  games += 1
  if user_choice == correct:
    streak += 1
    wins += 1
    current_score += 10
    res_text = (
        f"✅ <b>Верно!</b>\n\n📖 <b>Объяснение факта:</b>"
        f" {q_data['explanation']}"
    )
  else:
    streak = 0
    current_score = max(0, current_score - 5)
    correct_label = "Правда" if correct else "Ложь"
    res_text = (
        f"❌ <b>Неверно!</b> Правильный вариант: <b>{correct_label}</b>\n\n"
        f"📖 <b>Объяснение факта:</b> {q_data['explanation']}"
    )

  cursor.execute(
      "UPDATE scores SET score = ?, streak = ?, games = ?, wins = ?,"
      " username = ? WHERE user_id = ?",
      (current_score, streak, games, wins, username, user_id),
  )
  conn.commit()

  cursor.execute("SELECT COUNT(*) FROM scores WHERE score > ?", (current_score,))
  place = cursor.fetchone()[0] + 1
  conn.close()

  xp_gain = 10 if user_choice != correct else 25  # база 10 за игру + бонус за победу
  leveled_up, new_level, coins, xp = add_xp(user_id, username, xp_gain)

  if len(session["used_questions"]) >= len(QUESTIONS):
    session["used_questions"] = []

  available_indices = [
      i for i in range(len(QUESTIONS)) if i not in session["used_questions"]
  ]
  next_q_index = random.choice(available_indices)
  session["used_questions"].append(next_q_index)
  session["q_index"] = next_q_index
  next_q_data = QUESTIONS[next_q_index]
  session["correct_ans"] = next_q_data["truth"]

  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton("✅ Правда", callback_data="ans_true"),
      types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false"),
  )
  markup.add(
      types.InlineKeyboardButton("🏆 Рейтинг", callback_data="show_leaderboard"),
      types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"),
  )

  next_text = (
      f"{res_text}\n\n🔥 Серия: {streak} | 🏆 Место: #{place} | ⭐ Очки:"
      f" {current_score} | 🎮 Игр: {games}\n\n📌 <b>Следующий"
      f" вопрос:</b>\n{next_q_data['text']}"
  )
  try:
    bot.edit_message_text(
        next_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML",
    )
  except Exception:
    bot.send_message(
        call.message.chat.id, next_text, reply_markup=markup, parse_mode="HTML"
    )

  if leveled_up:
    send_level_up_message(call.message.chat.id, new_level, coins)


# ================================================================
# 9б. ИГРА "GOAT УГАДАЙ" — интерфейс и игровой цикл
# ================================================================
GUESS_MAX_TURNS = 20


def _guess_edit_or_send(chat_id, user_id, text, markup):
  """Редактирует текущее игровое сообщение, если возможно, иначе отправляет
  новое (и запоминает его id) — чтобы не спамить сообщениями."""
  session = guess_sessions.get(user_id)
  message_id = session.get("message_id") if session else None
  if message_id:
    try:
      bot.edit_message_text(
          text,
          chat_id=chat_id,
          message_id=message_id,
          reply_markup=markup,
          parse_mode="HTML",
      )
      return
    except Exception:
      pass  # сообщение могло устареть/удалиться — отправим новое
  msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
  if session is not None:
    session["message_id"] = msg.message_id


def _guess_question_markup():
  markup = types.InlineKeyboardMarkup(row_width=3)
  markup.add(
      types.InlineKeyboardButton("✅ Да", callback_data="guess_yes"),
      types.InlineKeyboardButton("❌ Нет", callback_data="guess_no"),
      types.InlineKeyboardButton("🤷 Не знаю", callback_data="guess_dunno"),
  )
  markup.add(
      types.InlineKeyboardButton("🏠 Завершить игру", callback_data="guess_end")
  )
  return markup


def _guess_confirm_markup():
  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton("✅ Да, угадал", callback_data="guess_correct"),
      types.InlineKeyboardButton("❌ Нет", callback_data="guess_incorrect"),
  )
  markup.add(
      types.InlineKeyboardButton("🏠 Завершить игру", callback_data="guess_end")
  )
  return markup


def _guess_advance(chat_id, user_id):
  """Определяет следующий шаг игры: вопрос, предположение по имени или
  завершение — и показывает его пользователю."""
  session = guess_sessions.get(user_id)
  if not session:
    return
  candidates = session["candidates"]

  if not candidates:
    _guess_ask_for_name(chat_id, user_id)
    return

  if session["turn"] >= GUESS_MAX_TURNS:
    _guess_give_up(chat_id, user_id)
    return

  if len(candidates) <= GUESS_MODE_THRESHOLD:
    session["mode"] = "guess_confirm"
    session["current_guess_name"] = candidates[0]["name"]
    session["turn"] += 1
    text = (
        "Кажется, я близок... 👀\n\nТы загадал <b>"
        f"{candidates[0]['name']}</b>?"
    )
    _guess_edit_or_send(chat_id, user_id, text, _guess_confirm_markup())
    return

  feature = _choose_next_feature(candidates, session["asked_features"])
  if feature is None:
    session["mode"] = "guess_confirm"
    session["current_guess_name"] = candidates[0]["name"]
    session["turn"] += 1
    text = (
        "Кажется, я близок... 👀\n\nТы загадал <b>"
        f"{candidates[0]['name']}</b>?"
    )
    _guess_edit_or_send(chat_id, user_id, text, _guess_confirm_markup())
    return

  session["mode"] = "question"
  session["current_feature"] = feature
  session["turn"] += 1
  text = (
      f"🔮 <b>Вопрос {session['turn']}/{GUESS_MAX_TURNS}</b>\n\n"
      f"{GUESS_FEATURE_QUESTIONS[feature]}"
  )
  _guess_edit_or_send(chat_id, user_id, text, _guess_question_markup())


def _guess_ask_for_name(chat_id, user_id):
  guess_sessions.pop(user_id, None)
  msg = bot.send_message(
      chat_id,
      "🤔 <b>Ты загадал кого-то, кого я пока не знаю.</b>\n\nКто это был?"
      " Напиши имя одним сообщением.",
      parse_mode="HTML",
  )
  bot.register_next_step_handler(msg, _guess_receive_unknown_name, user_id)


def _guess_receive_unknown_name(message, user_id):
  chat_id = message.chat.id
  name = (message.text or "").strip()
  update_guess_stats(user_id, won=False, questions_used=0)

  if name and not name.startswith("/"):
    notify_admin_error(
        "новый персонаж для 'GOAT Угадай'",
        f"Пользователь {message.from_user.first_name} загадал: {name}",
    )
    reply_text = (
        f"Спасибо! Запомнил — <b>{name}</b>. Добавим этот вариант в базу в"
        " будущем 🙌"
    )
  else:
    reply_text = "Ладно, в другой раз угадаю! 😄"

  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton(
          "🔄 Сыграть ещё", callback_data="start_guess_game"
      ),
      types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"),
  )
  bot.send_message(chat_id, reply_text, reply_markup=markup, parse_mode="HTML")


def _guess_give_up(chat_id, user_id):
  update_guess_stats(user_id, won=False, questions_used=0)
  markup = types.InlineKeyboardMarkup(row_width=2)
  markup.add(
      types.InlineKeyboardButton(
          "🔄 Сыграть ещё", callback_data="start_guess_game"
      ),
      types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"),
  )
  text = (
      "😵 <b>Ты меня переиграл.</b>\n\nЯ не смог угадать персонажа за"
      f" {GUESS_MAX_TURNS} вопросов. Кого ты загадал?"
  )
  _guess_edit_or_send(chat_id, user_id, text, markup)
  guess_sessions.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data == "start_guess_game")
def cb_start_guess_game(call):
  bot.answer_callback_query(call.id)
  chat_id = call.message.chat.id
  user_id = call.from_user.id
  if not require_subscription(chat_id, user_id):
    return

  ensure_user(user_id, call.from_user.first_name or "друг")
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton("▶️ Начать", callback_data="guess_begin"),
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"),
  )
  bot.send_message(
      chat_id,
      "🔮 <b>GOAT Угадай</b>\n\nЗагадай любого человека, персонажа,"
      " животное или известную личность.\nНе говори мне ответ.\n\nКогда"
      " будешь готов, нажми:",
      reply_markup=markup,
      parse_mode="HTML",
  )


@bot.callback_query_handler(func=lambda call: call.data == "guess_begin")
def cb_guess_begin(call):
  bot.answer_callback_query(call.id)
  chat_id = call.message.chat.id
  user_id = call.from_user.id

  guess_sessions[user_id] = {
      "candidates": CHARACTERS[:],
      "asked_features": set(),
      "turn": 0,
      "mode": None,
      "current_feature": None,
      "current_guess_name": None,
      "message_id": call.message.message_id,
  }
  _guess_advance(chat_id, user_id)


@bot.callback_query_handler(
    func=lambda call: call.data in ["guess_yes", "guess_no", "guess_dunno"]
)
def cb_guess_answer(call):
  bot.answer_callback_query(call.id)
  chat_id = call.message.chat.id
  user_id = call.from_user.id
  session = guess_sessions.get(user_id)
  if not session or session.get("mode") != "question":
    return

  answer_map = {"guess_yes": True, "guess_no": False, "guess_dunno": None}
  answer = answer_map[call.data]
  feature = session["current_feature"]
  session["asked_features"].add(feature)
  session["candidates"] = _filter_candidates(session["candidates"], feature, answer)
  _guess_advance(chat_id, user_id)


@bot.callback_query_handler(
    func=lambda call: call.data in ["guess_correct", "guess_incorrect"]
)
def cb_guess_result(call):
  bot.answer_callback_query(call.id)
  chat_id = call.message.chat.id
  user_id = call.from_user.id
  session = guess_sessions.get(user_id)
  if not session or session.get("mode") != "guess_confirm":
    return

  if call.data == "guess_correct":
    name = session["current_guess_name"]
    turns_used = session["turn"]

    update_guess_stats(user_id, won=True, questions_used=turns_used)
    add_coins(user_id, 25)
    leveled_up, new_level, coins, xp = add_xp(
        user_id, call.from_user.first_name or "друг", 50
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(
            "🔄 Сыграть ещё", callback_data="start_guess_game"
        ),
        types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"),
    )
    text = (
        f"🎯 <b>Я УГАДАЛ!</b>\n\nЭто был:\n🏆 <b>{name}</b>\n\nВопросов:"
        f" {turns_used}\n\n+50 XP\n+25 🪙"
    )
    _guess_edit_or_send(chat_id, user_id, text, markup)
    guess_sessions.pop(user_id, None)
    if leveled_up:
      send_level_up_message(chat_id, new_level, coins)
    return

  # Не угадал — убираем этого кандидата и продолжаем
  rejected_name = session["current_guess_name"]
  session["candidates"] = [
      c for c in session["candidates"] if c["name"] != rejected_name
  ]
  _guess_advance(chat_id, user_id)


@bot.callback_query_handler(func=lambda call: call.data == "guess_end")
def cb_guess_end(call):
  bot.answer_callback_query(call.id)
  user_id = call.from_user.id
  guess_sessions.pop(user_id, None)
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_menu")
  )
  try:
    bot.edit_message_text(
        "🚪 Игра остановлена.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
    )
  except Exception:
    bot.send_message(call.message.chat.id, "🚪 Игра остановлена.", reply_markup=markup)


def send_leaderboard(chat_id, user_id, edit_message_id=None):
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT username, score, streak, games FROM scores ORDER BY score DESC"
      " LIMIT 10"
  )
  top_players = cursor.fetchall()

  cursor.execute(
      "SELECT score, streak, games FROM scores WHERE user_id = ?", (user_id,)
  )
  my_row = cursor.fetchone()
  cursor.execute(
      "SELECT COUNT(*) FROM scores WHERE score > (SELECT COALESCE(score, 0)"
      " FROM scores WHERE user_id = ?)",
      (user_id,),
  )
  my_place = cursor.fetchone()[0] + 1
  conn.close()

  my_score = my_row[0] if my_row else 0
  my_streak = my_row[1] if my_row else 0
  my_games = my_row[2] if my_row else 0

  lb_text = (
      f"🏆 <b>ТАБЛИЦА ЛИДЕРОВ & ТВОЙ ПРОГРЕСС</b>\n\n🔥 Серия: {my_streak}"
      f" правильных ответов\n🏆 Место: #{my_place}\n⭐ Всего очков:"
      f" {my_score}\n🎮 Игр сыграно: {my_games}\n\n--- <b>ТОП ИГРОКОВ</b>"
      " ---\n"
  )
  if not top_players:
    lb_text += "Пока нет рекордов. Сыграйте первыми!"
  else:
    for idx, (p_name, p_score, p_streak, p_games) in enumerate(top_players, 1):
      medal = (
          "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
      )
      lb_text += f"{medal} <b>{p_name}</b> — {p_score} очков (🔥{p_streak})\n"

  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("🎮 Играть", callback_data="start_chat_game"),
      types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"),
  )

  if edit_message_id:
    try:
      bot.edit_message_text(
          lb_text,
          chat_id=chat_id,
          message_id=edit_message_id,
          reply_markup=markup,
          parse_mode="HTML",
      )
      return
    except Exception:
      pass
  bot.send_message(chat_id, lb_text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "show_leaderboard")
def cb_show_leaderboard(call):
  bot.answer_callback_query(call.id)
  send_leaderboard(
      call.message.chat.id, call.from_user.id, call.message.message_id
  )


# ================================================================
# 14. ГЕНЕРАЦИЯ И РЕДАКТИРОВАНИЕ ИЗОБРАЖЕНИЙ
# ================================================================
def translate_to_english(text):
  """Переводит короткий промпт на английский через Groq для лучшего качества."""
  completion = groq_client.chat.completions.create(
      messages=[
          {
              "role": "system",
              "content": (
                  "Translate this image prompt into English for an AI image"
                  " generator. Output ONLY the translated prompt."
              ),
          },
          {"role": "user", "content": text},
      ],
      model=GROQ_MODEL,
  )
  return completion.choices[0].message.content.strip()


@bot.callback_query_handler(func=lambda call: call.data == "ask_draw")
def cb_ask_draw(call):
  bot.answer_callback_query(call.id)
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton("✨ Сгенерировать с нуля", callback_data="gen_new"),
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"),
  )
  bot.send_message(
      call.message.chat.id,
      "🎨 <b>Генерация</b>\nЧто хотите сделать?",
      reply_markup=markup,
      parse_mode="HTML",
  )


@bot.callback_query_handler(func=lambda call: call.data == "gen_new")
def cb_gen_new(call):
  bot.answer_callback_query(call.id)
  msg = bot.send_message(
      call.message.chat.id,
      "🎨 <b>Генератор изображений</b>\nОпишите детально то, что хотите"
      " увидеть:",
      parse_mode="HTML",
  )
  bot.register_next_step_handler(msg, process_image_prompt)


def process_image_prompt(message):
  user_id = message.from_user.id
  if not require_subscription(message.chat.id, user_id):
    return

  prompt = (message.text or "").strip()
  if not prompt or prompt.startswith("/"):
    bot.send_message(message.chat.id, "❌ Генерация отменена.")
    return

  status_msg = bot.send_message(
      message.chat.id,
      f"🎨 Создаю арт по запросу: <i>«{prompt}»</i>...",
      parse_mode="HTML",
  )
  bot.send_chat_action(message.chat.id, "upload_photo")

  try:
    english_prompt = translate_to_english(prompt)

    seed = random.randint(1, 1000000)
    encoded_prompt = urllib.parse.quote(english_prompt)
    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?model=flux&width=1024&height=1024&nologo=true&enhance=true"
        f"&seed={seed}"
    )

    image_bytes = fetch_pollinations_image(image_url)
    if not image_bytes:
      bot.send_message(
          message.chat.id,
          "❌ Сервис генерации сейчас не отвечает. Попробуйте ещё раз через"
          " минуту.",
      )
      bot.delete_message(message.chat.id, status_msg.message_id)
      return

    bot.send_photo(
        message.chat.id,
        io.BytesIO(image_bytes),
        caption=f"✨ <b>Запрос:</b> {prompt}",
        parse_mode="HTML",
    )
    bot.delete_message(message.chat.id, status_msg.message_id)

    leveled_up, new_level, coins, xp = add_xp(
        user_id, message.from_user.first_name or "друг", 15
    )
    if leveled_up:
      send_level_up_message(message.chat.id, new_level, coins)
  except Exception as e:
    print(f"Ошибка генерации изображения: {e}")
    bot.send_message(message.chat.id, "❌ Не удалось сгенерировать изображение.")


# ================================================================
# 15. ОБРАБОТКА ФОТО (анализ / удаление фона / улучшение качества)
# ================================================================
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  caption = (message.caption or "").lower().strip()

  if not require_subscription(chat_id, user_id):
    return

  state = user_states.get(user_id, "")

  if state.startswith("waiting_analysis:"):
    # legacy-состояние из старой версии бота (фото-анализ убран) — просто
    # сбрасываем, чтобы пользователь не завис в старом режиме.
    user_states.pop(user_id, None)
    return

  if any(k in caption for k in ["/bg", "фон", "удалить фон"]):
    processing_msg = bot.send_message(
        chat_id, "✂️ <b>Удаление фона...</b>", parse_mode="HTML"
    )
    try:
      from rembg import remove

      file_info = bot.get_file(message.photo[-1].file_id)
      downloaded_file = bot.download_file(file_info.file_path)
      with open(input_path, "wb") as f:
        f.write(downloaded_file)
      with open(input_path, "rb") as i:
        output_image = remove(i.read())
      with open(output_path, "wb") as o:
        o.write(output_image)
      with open(output_path, "rb") as doc:
        bot.send_document(
            chat_id,
            doc,
            caption="✂️ <b>Фон успешно удален!</b>",
            parse_mode="HTML",
        )
      bot.delete_message(chat_id, processing_msg.message_id)
    except Exception as e:
      print(f"Ошибка удаления фона: {e}")
      bot.send_message(chat_id, "❌ Ошибка при обработке фото.")
    finally:
      if os.path.exists(input_path):
        os.remove(input_path)
      if os.path.exists(output_path):
        os.remove(output_path)
    return

  processing_msg = bot.send_message(
      chat_id, "✨ <b>Улучшение качества...</b>", parse_mode="HTML"
  )
  try:
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    with open(temp_in, "wb") as f:
      f.write(downloaded_file)
    img = Image.open(temp_in).convert("RGB")
    img = ImageEnhance.Sharpness(img).enhance(1.3)
    img = ImageEnhance.Color(img).enhance(1.1)
    img.save(temp_out, quality=95)
    with open(temp_out, "rb") as photo_to_send:
      bot.send_photo(
          chat_id,
          photo_to_send,
          caption="✨ <b>Качество улучшено!</b>",
          parse_mode="HTML",
      )
    bot.delete_message(chat_id, processing_msg.message_id)
  except Exception as e:
    print(f"Ошибка улучшения фото: {e}")
    bot.send_message(chat_id, "❌ Не удалось улучшить фото.")
  finally:
    if os.path.exists(temp_in):
      os.remove(temp_in)
    if os.path.exists(temp_out):
      os.remove(temp_out)


# ================================================================
# 16. ОБЫЧНЫЕ ТЕКСТОВЫЕ СООБЩЕНИЯ — GOAT CHAT
# ================================================================
@bot.message_handler(func=lambda message: True)
def handle_message(message):
  user_id = message.from_user.id
  username = message.from_user.first_name or "друг"
  if not require_subscription(message.chat.id, user_id):
    return

  ensure_user(user_id, username)

  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "UPDATE scores SET messages_count = messages_count + 1 WHERE user_id = ?",
      (user_id,),
  )
  cursor.execute("SELECT active_mode FROM scores WHERE user_id = ?", (user_id,))
  mode_row = cursor.fetchone()
  conn.commit()
  conn.close()

  if not groq_api_key:
    bot.send_message(message.chat.id, "❌ Ключ Groq API не настроен в Secrets.")
    return

  bot.send_chat_action(message.chat.id, "typing")

  current_mode_key = user_modes.get(
      message.chat.id,
      mode_row[0] if mode_row and mode_row[0] in ROLES else DEFAULT_ROLE_KEY,
  )
  role_info = ROLES.get(current_mode_key, ROLES[DEFAULT_ROLE_KEY])
  system_prompt = role_info["system"]
  role_name = role_info["name"]
  show_header = role_info.get("show_header", True)

  try:
    chat_completion = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message.text},
        ],
        model=GROQ_MODEL,
    )
    answer = chat_completion.choices[0].message.content.strip()
    answer = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", answer)

    if show_header:
      formatted_response = f"<b>{role_name}:</b>\n\n{answer}"
    else:
      formatted_response = answer

    bot.send_message(message.chat.id, formatted_response, parse_mode="HTML")

    leveled_up, new_level, coins, xp = add_xp(user_id, username, 3)
    if leveled_up:
      send_level_up_message(message.chat.id, new_level, coins)
  except Exception as e:
    print(f"Ошибка Groq API: {e}")
    bot.send_message(
        message.chat.id, "❌ Произошла ошибка при обращении к нейросети."
    )


# ================================================================
# 17. ЗАПУСК
# ================================================================
def run_bot():
  print("Бот запущен и полностью готов к работе со всеми функциями!")
  bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
  bot_thread = threading.Thread(target=run_bot)
  bot_thread.daemon = True
  bot_thread.start()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
