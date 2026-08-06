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


def get_profile_row(user_id):
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT score, streak, games, wins, messages_count, xp, level, coins"
      " FROM scores WHERE user_id = ?",
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


def analyze_photo_with_ai(image_path, system_prompt):
  """Отправляет фото в vision-модель Groq и возвращает реальный разбор
  именно этого изображения (а не заготовленный текст)."""
  with open(image_path, "rb") as f:
    b64_image = base64.b64encode(f.read()).decode("utf-8")

  completion = groq_client.chat.completions.create(
      model=GROQ_VISION_MODEL,
      messages=[
          {"role": "system", "content": system_prompt},
          {
              "role": "user",
              "content": [
                  {
                      "type": "text",
                      "text": "Проанализируй это изображение согласно своей роли.",
                  },
                  {
                      "type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                  },
              ],
          },
      ],
      temperature=0.7,
      max_completion_tokens=800,
  )
  return completion.choices[0].message.content.strip()


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
GROQ_MODEL = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile отключается 16.08.26
GROQ_VISION_MODEL = "qwen/qwen3.6-27b"  # модель с поддержкой изображений

CHANNEL_ID = "@goatai_news"
CHANNEL_URL = "https://t.me/goatai_news"
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

user_modes = {}      # chat_id -> ключ роли GOAT Chat (в памяти, для скорости)
game_sessions = {}   # user_id -> состояние текущей игры
user_states = {}     # user_id -> строка состояния ожидания (фото/текст)

input_path = "input_photo.jpg"
output_path = "output_no_bg.png"
temp_in = "temp_in.jpg"
temp_out = "temp_out.jpg"

HONESTY_RULE = (
    "Правило: твой анализ всегда честный. Без лести. Без грубости."
    " Только аргументы и конкретные, применимые советы. Пиши строго на"
    " русском языке. Отвечай простыми словами, без сложных терминов и"
    " профессионального жаргона — как будто объясняешь другу. Формат: 3-4"
    " коротких пункта, без воды. Пиши предметно именно про то, что видишь"
    " на этом конкретном фото или в этом тексте, а не общими фразами."
)

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
            " приправу. Общаешься с пользователем на «ты», грубо, с"
            " сарказмом и чёрным юмором. При этом ты всё равно по существу"
            " отвечаешь на вопрос пользователя — просто делаешь это"
            " матерно и без обиняков. Не оскорбляешь пользователя лично и"
            " не переходишь на темы дискриминации — мат используешь как"
            " экспрессию речи (междометия, усилители, связки), а не как"
            " оскорбления в адрес человека. Пиши строго на русском языке."
        ),
    },
}
DEFAULT_ROLE_KEY = "assistant"

# ================================================================
# 5. GOAT АНАЛИЗ — категории
# ================================================================
ANALYSIS_CATEGORIES = {
    "appearance": {
        "label": "👤 Внешность",
        "type": "photo",
        "system": (
            "Ты — дружелюбный стилист. Простыми словами говоришь, идёт ли"
            " человеку то, что на нём надето, сочетается ли по цвету и"
            " стилю, и что можно было бы поменять. " + HONESTY_RULE
        ),
    },
    "photo": {
        "label": "📷 Фото",
        "type": "photo",
        "system": (
            "Ты — фотограф-друг. Простыми словами говоришь, удачный ли"
            " кадр: получилось интересно или скучно, что в кадре мешает,"
            " а что цепляет взгляд. " + HONESTY_RULE
        ),
    },
    "design": {
        "label": "🎨 Дизайн",
        "type": "photo",
        "system": (
            "Ты — дизайнер-практик. Простыми словами говоришь, удобно ли"
            " смотрится макет, не перегружен ли он, читается ли главное"
            " с первого взгляда. " + HONESTY_RULE
        ),
    },
    "text": {
        "label": "📝 Текст",
        "type": "text",
        "system": (
            "Ты — редактор-практик. Простыми словами говоришь, легко ли"
            " читается текст, где лишние слова, где мысль потерялась."
            " " + HONESTY_RULE
        ),
    },
    "idea": {
        "label": "💡 Идея",
        "type": "text",
        "system": (
            "Ты — предприниматель-практик. Простыми словами говоришь, есть"
            " ли в идее смысл, что в ней слабое место и что стоит"
            " проверить в первую очередь. " + HONESTY_RULE
        ),
    },
    "code": {
        "label": "💻 Код",
        "type": "text",
        "system": (
            "Ты — опытный разработчик. Простыми словами говоришь, что не"
            " так в коде, где может быть баг и как это поправить."
            " " + HONESTY_RULE
        ),
    },
    "interface": {
        "label": "📱 Интерфейс",
        "type": "photo",
        "system": (
            "Ты — практик по интерфейсам. Простыми словами говоришь,"
            " удобно ли пользоваться этим экраном, не запутается ли"
            " человек, что не сразу понятно. " + HONESTY_RULE
        ),
    },
    "business": {
        "label": "📈 Бизнес",
        "type": "text",
        "system": (
            "Ты — практичный бизнес-советчик. Простыми словами говоришь,"
            " что сильное, а что слабое в описанном деле, и что делать"
            " дальше. " + HONESTY_RULE
        ),
    },
}


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
# 7. ПОДПИСКА НА КАНАЛ
# ================================================================
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
      types.InlineKeyboardButton(
          "🎮 Игра «Правда или Ложь»", callback_data="start_chat_game"
      ),
  )
  markup.add(
      types.InlineKeyboardButton("🐐 GOAT Chat", callback_data="goat_chat"),
      types.InlineKeyboardButton("🔍 GOAT Анализ", callback_data="goat_analysis"),
      types.InlineKeyboardButton("🎨 Генерация", callback_data="ask_draw"),
      types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
      types.InlineKeyboardButton("🏆 Лидеры", callback_data="show_leaderboard"),
      types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
  )

  welcome_text = (
      f"🐐 <b>GOAT AI</b>\nДобро пожаловать, {user_name}!\n\n🎮 Не забудьте"
      " попробовать игру «Правда или Ложь» — испытайте себя и заработайте"
      " очки!\n\nВыберите раздел:"
  )
  bot.send_message(chat_id, welcome_text, reply_markup=markup, parse_mode="HTML")


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
  send_main_menu(call.message.chat.id, call.from_user.first_name or "друг")


@bot.message_handler(commands=["help"])
def send_help(message):
  help_text = (
      "📖 <b>Справочник по командам:</b>\n🔹 <code>/start</code> —"
      " Перезапустить бота\n🔹 <code>/game</code> — Игра «Правда или"
      " Ложь»\n🔹 <code>/top</code> — Таблица лидеров\n🔹"
      " <code>/admin</code> — Панель администратора (только для"
      " админа)\n\n🔍 <b>GOAT Анализ:</b> выберите категорию в меню и"
      " пришлите фото или текст.\n🐐 <b>GOAT Chat:</b> выберите личность"
      " ИИ и просто пишите сообщения."
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


# ================================================================
# 10. GOAT АНАЛИЗ
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data == "goat_analysis")
def cb_goat_analysis(call):
  bot.answer_callback_query(call.id)
  markup = types.InlineKeyboardMarkup(row_width=2)
  buttons = [
      types.InlineKeyboardButton(cat["label"], callback_data=f"analysis_{key}")
      for key, cat in ANALYSIS_CATEGORIES.items()
  ]
  markup.add(*buttons)
  markup.add(
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")
  )
  text = (
      "🔍 <b>GOAT Анализ</b>\nВыберите, что разобрать.\n\n<i>GOAT Анализ"
      " всегда честный — без лести и без грубости, только аргументы и"
      " советы.</i>"
  )
  bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("analysis_"))
def cb_analysis_category(call):
  bot.answer_callback_query(call.id)
  key = call.data.replace("analysis_", "")
  category = ANALYSIS_CATEGORIES.get(key)
  if not category:
    return

  user_id = call.from_user.id
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")
  )

  if category["type"] == "photo":
    user_states[user_id] = f"waiting_analysis:{key}"
    bot.send_message(
        call.message.chat.id,
        f"{category['label']}\n\n📸 Пришлите фотографию для анализа.",
        reply_markup=markup,
        parse_mode="HTML",
    )
  else:
    msg = bot.send_message(
        call.message.chat.id,
        f"{category['label']}\n\n📝 Пришлите текст для анализа одним"
        " сообщением.",
        reply_markup=markup,
        parse_mode="HTML",
    )
    bot.register_next_step_handler(
        msg, lambda m, k=key: process_text_analysis(m, k)
    )


def process_text_analysis(message, category_key):
  user_id = message.from_user.id
  if not require_subscription(message.chat.id, user_id):
    return

  content = (message.text or "").strip()
  if not content or content.startswith("/"):
    bot.send_message(message.chat.id, "❌ Анализ отменен.")
    return

  category = ANALYSIS_CATEGORIES[category_key]
  bot.send_chat_action(message.chat.id, "typing")

  if not groq_api_key:
    bot.send_message(message.chat.id, "❌ Ключ Groq API не настроен в Secrets.")
    return

  try:
    completion = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": category["system"]},
            {"role": "user", "content": content},
        ],
        model=GROQ_MODEL,
    )
    answer = completion.choices[0].message.content.strip()
    answer = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", answer)

    leveled_up, new_level, coins, xp = add_xp(
        user_id, message.from_user.first_name or "друг", 20
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "🏠 Главное меню", callback_data="back_to_menu"
        )
    )
    text = f"{category['label']}\n\n{answer}\n\n<i>+20 XP</i>"
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

    if leveled_up:
      send_level_up_message(message.chat.id, new_level, coins)
  except Exception as e:
    print(f"Ошибка Groq API (анализ текста): {e}")
    bot.send_message(message.chat.id, "❌ Не удалось выполнить анализ.")


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
    score, streak, games, wins, messages_count, xp, level, coins = row
  else:
    score = streak = games = wins = messages_count = xp = coins = 0
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


def _upload_to_litterbox(file_path, expire="1h"):
  with open(file_path, "rb") as f:
    resp = requests.post(
        "https://litterbox.catbox.moe/resources/internals/api.php",
        data={"reqtype": "fileupload", "time": expire},
        files={"fileToUpload": f},
        timeout=30,
    )
  if resp.status_code == 200 and resp.text.strip().startswith("http"):
    return resp.text.strip()
  raise ValueError(f"Litterbox вернул неожиданный ответ: {resp.text[:200]}")


def _upload_to_0x0(file_path):
  with open(file_path, "rb") as f:
    resp = requests.post(
        "https://0x0.st",
        files={"file": f},
        headers={"User-Agent": "GOAT-AI-Bot/1.0"},
        timeout=30,
    )
  if resp.status_code == 200 and resp.text.strip().startswith("http"):
    return resp.text.strip()
  raise ValueError(f"0x0.st вернул неожиданный ответ: {resp.text[:200]}")


def upload_temp_image(file_path, expire="1h"):
  """Загружает файл на анонимный временный хостинг и возвращает публичный URL.

  Пробует несколько бесплатных хостингов без регистрации (используем для
  img2img, чтобы не передавать во внешний сервис постоянную ссылку или токен
  бота). Каждый хостинг пробуется дважды на случай кратковременного сбоя,
  затем идёт переключение на следующий — так один нестабильный сервис не
  роняет всю функцию."""
  uploaders = [
      ("Litterbox", lambda: _upload_to_litterbox(file_path, expire)),
      ("0x0.st", lambda: _upload_to_0x0(file_path)),
  ]
  for name, uploader in uploaders:
    for attempt in range(2):
      try:
        return uploader()
      except Exception as e:
        print(f"Ошибка загрузки временного фото через {name} (попытка"
              f" {attempt + 1}/2): {e}")
  return None


@bot.callback_query_handler(func=lambda call: call.data == "ask_draw")
def cb_ask_draw(call):
  bot.answer_callback_query(call.id)
  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton("✨ Сгенерировать с нуля", callback_data="gen_new"),
      types.InlineKeyboardButton("🖌 Изменить моё фото", callback_data="gen_edit"),
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


@bot.callback_query_handler(func=lambda call: call.data == "gen_edit")
def cb_gen_edit(call):
  bot.answer_callback_query(call.id)
  user_id = call.from_user.id
  if not require_subscription(call.message.chat.id, user_id):
    return
  user_states[user_id] = "waiting_edit_photo"
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu")
  )
  bot.send_message(
      call.message.chat.id,
      "🖌 <b>Изменить фото</b>\n\n📸 Пришлите фотографию, которую нужно"
      " изменить.",
      reply_markup=markup,
      parse_mode="HTML",
  )


def process_edit_prompt(message, source_photo_path):
  user_id = message.from_user.id
  chat_id = message.chat.id
  if not require_subscription(chat_id, user_id):
    if os.path.exists(source_photo_path):
      os.remove(source_photo_path)
    return

  prompt = (message.text or "").strip()
  if not prompt or prompt.startswith("/"):
    bot.send_message(chat_id, "❌ Изменение отменено.")
    if os.path.exists(source_photo_path):
      os.remove(source_photo_path)
    return

  status_msg = bot.send_message(
      chat_id, "🖌 <b>Изменяю фото...</b>", parse_mode="HTML"
  )
  bot.send_chat_action(chat_id, "upload_photo")

  try:
    hosted_url = upload_temp_image(source_photo_path, expire="1h")
    if not hosted_url:
      bot.send_message(
          chat_id,
          "❌ Не удалось загрузить фото для обработки — временный хостинг"
          " сейчас недоступен. Подождите немного и попробуйте ещё раз.",
      )
      return

    english_prompt = translate_to_english(prompt)
    encoded_prompt = urllib.parse.quote(english_prompt)
    encoded_image_url = urllib.parse.quote(hosted_url, safe="")

    edit_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?model=kontext&image={encoded_image_url}"
        f"&width=1024&height=1024&nologo=true"
    )

    image_bytes = fetch_pollinations_image(edit_url)
    if not image_bytes:
      bot.send_message(
          chat_id,
          "❌ Сервис генерации сейчас не отвечает. Попробуйте ещё раз через"
          " минуту.",
      )
      bot.delete_message(chat_id, status_msg.message_id)
      return

    bot.send_photo(
        chat_id,
        io.BytesIO(image_bytes),
        caption=f"🖌 <b>Изменено:</b> {prompt}",
        parse_mode="HTML",
    )
    bot.delete_message(chat_id, status_msg.message_id)

    leveled_up, new_level, coins, xp = add_xp(
        user_id, message.from_user.first_name or "друг", 15
    )
    if leveled_up:
      send_level_up_message(chat_id, new_level, coins)
  except Exception as e:
    print(f"Ошибка редактирования изображения: {e}")
    bot.send_message(chat_id, "❌ Не удалось изменить фото.")
  finally:
    if os.path.exists(source_photo_path):
      os.remove(source_photo_path)


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

  if state == "waiting_edit_photo":
    user_states.pop(user_id, None)
    try:
      file_info = bot.get_file(message.photo[-1].file_id)
      downloaded_file = bot.download_file(file_info.file_path)
      edit_source_path = f"edit_source_{user_id}.jpg"
      with open(edit_source_path, "wb") as f:
        f.write(downloaded_file)
    except Exception as e:
      print(f"Ошибка загрузки фото для редактирования: {e}")
      bot.send_message(chat_id, "❌ Не удалось загрузить фото.")
      return

    msg = bot.send_message(
        chat_id,
        "📝 Опишите, что изменить на фото (например: «сделай в стиле"
        " акварели» или «добавь снег»):",
    )
    bot.register_next_step_handler(
        msg, lambda m, p=edit_source_path: process_edit_prompt(m, p)
    )
    return

  if state.startswith("waiting_analysis:"):
    category_key = state.split(":", 1)[1]
    category = ANALYSIS_CATEGORIES.get(category_key)
    user_states.pop(user_id, None)
    if not category:
      return

    processing_msg = bot.send_message(
        chat_id, f"🔍 <b>{category['label']}: анализирую...</b>", parse_mode="HTML"
    )
    try:
      file_info = bot.get_file(message.photo[-1].file_id)
      downloaded_file = bot.download_file(file_info.file_path)
      with open(input_path, "wb") as f:
        f.write(downloaded_file)

      expert_text = analyze_photo_with_ai(input_path, category["system"])

      leveled_up, new_level, coins, xp = add_xp(
          user_id, message.from_user.first_name or "друг", 20
      )

      markup = types.InlineKeyboardMarkup()
      markup.add(
          types.InlineKeyboardButton(
              "🏠 Главное меню", callback_data="back_to_menu"
          )
      )
      bot.send_message(
          chat_id,
          expert_text + "\n\n<i>+20 XP</i>",
          reply_markup=markup,
          parse_mode="HTML",
      )
      bot.delete_message(chat_id, processing_msg.message_id)

      if leveled_up:
        send_level_up_message(chat_id, new_level, coins)
    except Exception as e:
      print(f"Ошибка анализа фото: {e}")
      bot.send_message(chat_id, "❌ Ошибка при анализе фото.")
    finally:
      if os.path.exists(input_path):
        os.remove(input_path)
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
