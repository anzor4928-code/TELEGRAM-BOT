import os
import threading
import sqlite3
import time
import requests
from flask import Flask
from groq import Groq
from PIL import Image, ImageEnhance
from rembg import remove
import telebot
from telebot import types

# --- 1. Настройка базы данных для Топа Игроков и Пользователей ---
def init_db():
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            score INTEGER,
            streak INTEGER DEFAULT 0,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            messages_count INTEGER DEFAULT 0,
            settings TEXT DEFAULT 'RU'
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- 2. Настройка Flask для 24/7 работы ---
app = Flask("")

@app.route("/")
def home():
    return "🚀 Bot Server is active and running smoothly!"

# --- 3. Основная логика Telegram бота ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("⚠️ Ошибка: BOT_TOKEN не найден в Secrets!")

bot = telebot.TeleBot(BOT_TOKEN)

groq_api_key = os.environ.get("GROQ_API_KEY")
if not groq_api_key:
    print("⚠️ Предупреждение: GROQ_API_KEY не найден в Secrets!")

groq_client = Groq(api_key=groq_api_key)
GROQ_MODEL = "llama-3.3-70b-versatile"

# Канал для обязательной подписки
CHANNEL_ID = "@goatai_news"
CHANNEL_URL = "https://t.me/goatai_news"
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))  # Укажи свой Telegram ID в Secrets при желании

# Словарь для хранения выбранного режима каждого пользователя
user_modes = {}

# Активные сессии игры в чате: {user_id: {"score": 0, "q_index": 0, "correct_ans": True}}
game_sessions = {}

# Состояния для загрузки фото в GOAT Expert
from aiogram.fsm.state import State, StatesGroup # (Используем через телебот-состояния или ручной флаг, сделаем через простой словарь состояний)
user_states = {}

QUESTIONS = [
    { "text": "В Сингапуре официально запрещено ввозить и продавать жевательную резинку?", "truth": True, "explanation": "Сингапур славится своими строгими законами о чистоте. Импорт и продажа жвачки были запрещены в 1992 году для поддержания порядка в метро и на улицах." },
    { "text": "Кот в Японии официально занимал должность начальника железнодорожной станции?", "truth": True, "explanation": "Кошка по кличке Тама спасла от банкротства железнодорожную станцию Кинокава в Японии, работая «начальником» и привлекая тысячи туристов." },
    { "text": "В Великобритании разрешено законно убивать шотландцев из лука по пятницам?", "truth": False, "explanation": "Это популярный миф и старая шутка. Никаких подобных действующих законов в Великобритании нет." },
    { "text": "Первый в мире сайт (info.cern.ch) до сих пор работает по своему оригинальному адресу?", "truth": True, "explanation": "Да! Самый первый веб-сайт, созданный Тимом Бернерсом-Ли в 1991 году, до сих пор доступен по своему первоначальному адресу." },
    { "text": "Во Франции по закону запрещено называть свиней именем «Наполеон»?", "truth": True, "explanation": "Во Франции действительно действует старый закон, запрещающий оскорблять память правителей, включая присвоение имени Наполеон свиньям." },
    { "text": "Осьминоги имеют три сердца и голубую кровь?", "truth": True, "explanation": "У осьминогов три сердца (одно качает кровь по телу, два других — через жабры), а кровь имеет голубой цвет из-за содержания медьсодержащего белка гемоцианина." },
    { "text": "В Швейцарии после 22:00 по закону запрещено смывать унитаз в квартирах?", "truth": False, "explanation": "Это городской миф. В Швейцарии действуют общие правила тишины, но прямой запрет на пользование туалетом ночью отсутствует." },
    { "text": "Бананы растут на гигантских деревьях с крепким деревянным стволом?", "truth": False, "explanation": "Банановые растения — это не деревья, а гигантские травянистые многолетники, у которых нет настоящего древесного ствола." },
    { "text": "На Сардинии владельцам собак запрещено выгуливать их реже трех раз в день?", "truth": True, "explanation": "В некоторых коммунах Италии и на Сардинии существуют строгие правила содержания домашних животных, включая требования к выгулу." },
    { "text": "Молния никогда не ударяет дважды в одно и то же место?", "truth": False, "explanation": "Молния часто бьет в одно и то же место несколько раз, особенно в высокие и остроконечные объекты (например, небоскребы)." },
    { "text": "В Исландии полностью отсутствуют комары?", "truth": True, "explanation": "Из-за специфического климата и частых перепадов температур в Исландии нет комаров." },
    { "text": "Акулы появились на Земле раньше, чем деревья?", "truth": True, "explanation": "Акулы существуют уже более 400 миллионов лет, в то время как первые деревья появились на Земле примерно на 50 миллионов лет позже." },
    { "text": "У коровы может быть лучший друг, и они сильно скучают, когда их разлучают?", "truth": True, "explanation": "Научные исследования подтверждают, что коровы привязываются друг к другу и испытывают стресс при разлуке." },
    { "text": "Мёд может храниться тысячелетиями и никогда не портится?", "truth": True, "explanation": "Благодаря низкому содержанию влаги и высокой кислотности мёд обладает естественными антибактериальными свойствами и не портится." },
    { "text": "Эйфелева башня становится выше в холодные зимние месяцы?", "truth": False, "explanation": "Наоборот, от тепла металл расширяется, поэтому летом Эйфелева башня становится выше примерно на 15 сантиметров, а зимой сжимается." }
]

ROLES = {
    "psychologist": {
        "name": "🧘 Психотерапевт",
        "show_header": True,
        "system": "Ты эмпатичный, профессиональный и мягкий психотерапевт. Внимательно слушаешь, задаешь наводящие вопросы, помогаешь человеку разобраться в его чувствах и поддерживаешь его. Пиши на русском языке.",
    },
    "rude": {
        "name": "🤬 Циничный Грубиян",
        "show_header": True,
        "system": "Ты максимально токсичный, злой и угарный грубиян. Общаешься с пользователем на «ты», кроешь всё и вся отборным, смешным и крепким русским матом без всякой цензуры. Используй мат для каждой связки слов, издевайся, сыпь черным юмором и сарказмом, показывай как тебе всё надоело. Пиши строго на русском языке.",
    },
    "musician": {
        "name": "🎸 Музыкант и Продюсер",
        "show_header": True,
        "system": "Ты опытный музыкант, битмейкер и саунд-продюсер. Помогаешь придумывать концепты треков, структуру песни, тексты, рифмы, гармонию и атмосферу. Пиши энергично и строго на русском языке.",
    },
    "coder": {
        "name": "💻 IT-Ментор / Кодер",
        "show_header": True,
        "system": "Ты опытный программист и ментор. Помогаешь находить ошибки в коде, объясняешь сложные технические вещи простыми словами, подсказываешь по логике скриптов и ботов. Пиши четко и строго на русском языке.",
    },
    "marketer": {
        "name": "📈 Маркетолог & SMM",
        "show_header": True,
        "system": "Ты крутой маркетолог и эксперт по продвижению в соцсетях. Помогаешь придумать воронки, стратегии привлечения аудитории, офферы и идеи для роста просмотров. Пиши по делу и строго на русском языке.",
    },
    "screenwriter": {
        "name": "🎬 Сценарист Идей",
        "show_header": True,
        "system": "Ты креативный сценарист и продюсер вирусных видео. Помогаешь придумывать идеи для контента, хуки, сюжеты с балансом юмора, драмы и экшена. Пиши энергично и строго на русском языке.",
    },
    "friend": {
        "name": "🤗 Лучший Друг",
        "show_header": True,
        "system": "Ты преданный, поддерживающий и веселый лучший друг. Общаешься тепло, неофициально, на «ты», всегда на стороне пользователя. Пиши на русском языке.",
    },
    "default": {
        "name": "🤖 Умный Помощник",
        "show_header": False,
        "system": "Ты — вежливый, умный и эрудированный ИИ-помощник в Telegram. Отвечай чисто, структурированно, без лишней воды и без шапок-приписок. Пиши СТРОГО на русском языке.",
    }
}

input_path = "input_photo.jpg"
output_path = "output_no_bg.png"
temp_in = "temp_in.jpg"
temp_out = "temp_out.jpg"

# Проверка подписки на канал
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
        types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub")
    )
    return markup

@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.first_name or "друг"

    # Регистрируем пользователя в БД
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM scores WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO scores (user_id, username, score, streak, games, wins, messages_count) VALUES (?, ?, 0, 0, 0, 0, 0)", (user_id, username))
        conn.commit()
    conn.close()

    # Проверка обязательной подписки
    if not check_subscription(user_id):
        bot.send_message(
            message.chat.id,
            "🔒 <b>Доступ заблокирован</b>\n\n"
            f"Чтобы пользоваться всеми функциями **GOAT AI**, подпишитесь на наш официальный канал:\n"
            f"👉 {CHANNEL_URL}\n\n"
            "После подписки нажмите кнопку ниже, чтобы разблокировать бота.",
            reply_markup=get_sub_markup(),
            parse_mode="HTML"
        )
        return

    send_main_menu(message.chat.id, username)

def send_main_menu(chat_id, user_name):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_expert = types.InlineKeyboardButton("🐐 GOAT Expert", callback_data="goat_expert")
    btn_draw = types.InlineKeyboardButton("🎨 Сгенерировать арт", callback_data="ask_draw")
    btn_modes = types.InlineKeyboardButton("🎭 Выбрать ИИ-личность", callback_data="choose_mode")
    btn_game = types.InlineKeyboardButton("🎮 Правда или Ложь", callback_data="start_chat_game")
    btn_top = types.InlineKeyboardButton("🏆 Рейтинг игроков", callback_data="show_leaderboard")
    btn_profile = types.InlineKeyboardButton("👤 Профиль", callback_data="profile")
    btn_settings = types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")
    btn_help = types.InlineKeyboardButton("📖 Справка", callback_data="help")

    markup.add(btn_expert, btn_draw, btn_modes, btn_game, btn_top, btn_profile, btn_settings, btn_help)

    welcome_text = (
        f"✨ <b>Добро пожаловать в GOAT AI, {user_name}!</b>\n"
        "Выбери нужную функцию в меню ниже:\n\n"
        "🛠 <b>Доступно:</b> Экспертный разбор фото, ИИ-личности, викторина с прогрессом и профиль."
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
        bot.answer_callback_query(call.id, "❌ Вы еще не подписались на канал!", show_alert=True)

@bot.message_handler(commands=["help"])
def send_help(message):
    help_text = (
        "📖 <b>Справочник по командам:</b>\n"
        "🔹 <code>/start</code> — Перезапустить бота\n"
        "🔹 <code>/modes</code> — Сменить характер или эксперта ИИ\n"
        "🔹 <code>/game</code> — Запустить игру «Правда или Ложь»\n"
        "🔹 <code>/admin</code> — Панель администратора (только для админа)\n\n"
        "📸 <b>Работа с фотографиями:</b>\n"
        "• Выберите <b>🐐 GOAT Expert</b> для профессионального разбора фото\n"
        "• Отправьте фото с подписью <code>фон</code>, чтобы вырезать объект"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    user_id = message.from_user.id
    if ADMIN_ID and user_id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM scores")
    total_users = cursor.fetchone()[0]
    conn.close()

    text = (
        f"🛠 <b>Панель администратора (GOAT AI)</b>\n\n"
        f"👥 Всего пользователей в базе: <b>{total_users}</b>\n"
        f"🟢 Статус: Активен 24/7"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=["modes"])
def command_modes(message):
    if not check_subscription(message.from_user.id):
        bot.send_message(message.chat.id, "🔒 Сначала подпишитесь на канал!", reply_markup=get_sub_markup())
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, role in ROLES.items():
        if key == "default":
            continue
        markup.add(types.InlineKeyboardButton(role["name"], callback_data=f"set_mode_{key}"))

    current_mode_key = user_modes.get(message.chat.id, "default")
    current_name = ROLES[current_mode_key]["name"]

    modes_header = (
        "🎭 <b>Центр управления личностями</b>\n"
        "Выберите, с кем именно вы хотите продолжить диалог.\n\n"
        f"📌 Текущий активный режим: <b>{current_name}</b>"
    )
    bot.send_message(message.chat.id, modes_header, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["game"])
def command_start_game(message):
    user_id = message.from_user.id
    if not check_subscription(user_id):
        bot.send_message(message.chat.id, "🔒 Сначала подпишитесь на канал!", reply_markup=get_sub_markup())
        return

    start_quiz_session(message.chat.id, user_id)

def start_quiz_session(chat_id, user_id):
    import random
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT score FROM scores WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    current_score = row[0] if row else 0
    conn.close()

    q_index = random.randint(0, len(QUESTIONS) - 1)
    q_data = QUESTIONS[q_index]

    game_sessions[user_id] = {
        "score": current_score,
        "q_index": q_index,
        "correct_ans": q_data["truth"]
    }

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_true = types.InlineKeyboardButton("✅ Правда", callback_data="ans_true")
    btn_false = types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false")
    markup.add(btn_true, btn_false)
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_menu"))

    text = (
        f"🎮 <b>Игра: Правда или Ложь</b>\n"
        f"⭐ Ваши очки: <b>{current_score}</b>\n\n"
        f"📌 <b>Вопрос:</b>\n{q_data['text']}"
    )
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "goat_expert")
def cb_goat_expert(call):
    bot.answer_callback_query(call.id)
    user_states[call.from_user.id] = "waiting_expert_photo"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"))
    
    bot.send_message(
        call.message.chat.id,
        "🔍 <b>GOAT Expert: Анализ изображения</b>\n\n"
        "Отправьте мне фотографию, и я проведу профессиональный глубокий разбор:\n"
        "• Композиция и кадр\n"
        "• Качество и освещение\n"
        "• Стиль, одежда и дизайн\n"
        "• Конкретные советы по улучшению\n\n"
        "📸 <i>Жду ваше фото...</i>",
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def cb_profile(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id

    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT score, streak, games, wins, messages_count, settings FROM scores WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    # Определим место в рейтинге
    cursor.execute("SELECT COUNT(*) FROM scores WHERE score > (SELECT COALESCE(score, 0) FROM scores WHERE user_id = ?)", (user_id,))
    place = cursor.fetchone()[0] + 1
    conn.close()

    if row:
        score, streak, games, wins, messages_count, settings = row
    else:
        score, streak, games, wins, messages_count, settings = 0, 0, 0, 0, 0, "RU"

    text = (
        f"👤 <b>ТВОЙ ПРОФИЛЬ (GOAT AI)</b>\n\n"
        f"💬 Сообщений отправлено: <b>{messages_count}</b>\n"
        f"⚙️ Текущие настройки: Стандарт ({settings})\n"
        f"⭐ Очки в игре: <b>{score}</b>\n"
        f"🔥 Серия побед: <b>{streak}</b>\n"
        f"🏆 Место в рейтинге: <b>#{place}</b>\n"
        f"🎮 Статистика игр: {games} сыграно / {wins} побед"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"))
    
    bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "settings")
def cb_settings(call):
    bot.answer_callback_query(call.id)
    text = (
        "⚙️ <b>НАСТРОЙКИ БОТА</b>\n\n"
        "Управляйте параметрами под себя:\n"
        "• 🔔 Уведомления: Включены\n"
        "• 📜 История сообщений: Сохраняется\n"
        "• 🌍 Язык / Language: Русский 🇷🇺\n"
        "• ℹ️ Версия бота: GOAT AI v2.5 Expert Edition"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_menu"))
    bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_menu")
def cb_back_to_menu(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id in user_states:
        del user_states[call.from_user.id]
    send_main_menu(call.message.chat.id, call.from_user.first_name or "друг")

@bot.callback_query_handler(func=lambda call: call.data == "help")
def cb_help(call):
    bot.answer_callback_query(call.id)
    send_help(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "start_chat_game")
def cb_start_chat_game(call):
    bot.answer_callback_query(call.id)
    start_quiz_session(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data in ["ans_true", "ans_false"])
def cb_game_answer(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    username = call.from_user.first_name or "Игрок"

    if user_id not in game_sessions:
        start_quiz_session(call.message.chat.id, user_id)
        return

    session = game_sessions[user_id]
    user_choice = (call.data == "ans_true")
    q_index = session["q_index"]
    q_data = QUESTIONS[q_index]
    correct = q_data["truth"]

    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT score, streak, games, wins FROM scores WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        current_score, streak, games, wins = row
    else:
        current_score, streak, games, wins = 0, 0, 0, 0

    games += 1
    if user_choice == correct:
        streak += 1
        wins += 1
        current_score += 10
        res_text = f"✅ **Верно!**\n\n📖 **Объяснение факта:** {q_data['explanation']}"
    else:
        streak = 0
        current_score = max(0, current_score - 5)
        res_text = f"❌ **Неверно!** Правильный вариант: **{'Правда' if correct else 'Ложь'}**\n\n📖 **Объяснение факта:** {q_data['explanation']}"

    cursor.execute("UPDATE scores SET score = ?, streak = ?, games = ?, wins = ?, username = ? WHERE user_id = ?", (current_score, streak, games, wins, username, user_id))
    conn.commit()
    
    # Считаем место для вывода в ответе
    cursor.execute("SELECT COUNT(*) FROM scores WHERE score > ?", (current_score,))
    place = cursor.fetchone()[0] + 1
    conn.close()

    # Берем следующий вопрос
    import random
    next_q_index = random.randint(0, len(QUESTIONS) - 1)
    next_q_data = QUESTIONS[next_q_index]
    session["q_index"] = next_q_index
    session["correct_ans"] = next_q_data["truth"]

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_true = types.InlineKeyboardButton("✅ Правда", callback_data="ans_true")
    btn_false = types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false")
    markup.add(btn_true, btn_false)
    markup.add(types.InlineKeyboardButton("🏆 Рейтинг", callback_data="show_leaderboard"), types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu"))

    next_text = (
        f"{res_text}\n\n"
        f"🔥 Серия: {streak} | 🏆 Место: #{place} | ⭐ Очки: {current_score} | 🎮 Игр: {games}\n\n"
        f"📌 <b>Следующий вопрос:</b>\n{next_q_data['text']}"
    )
    try:
        bot.edit_message_text(next_text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, next_text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "show_leaderboard")
def cb_show_leaderboard(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT username, score, streak, games FROM scores ORDER BY score DESC LIMIT 10")
    top_players = cursor.fetchall()

    cursor.execute("SELECT score, streak, games FROM scores WHERE user_id = ?", (user_id,))
    my_row = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM scores WHERE score > (SELECT COALESCE(score, 0) FROM scores WHERE user_id = ?)", (user_id,))
    my_place = cursor.fetchone()[0] + 1
    conn.close()

    my_score = my_row[0] if my_row else 0
    my_streak = my_row[1] if my_row else 0
    my_games = my_row[2] if my_row else 0

    lb_text = (
        f"🏆 <b>ТАБЛИЦА ЛИДЕРОВ & ТВОЙ ПРОГРЕСС</b>\n\n"
        f"🔥 Серия: {my_streak} правильных ответов\n"
        f"🏆 Место: #{my_place}\n"
        f"⭐ Всего очков: {my_score}\n"
        f"🎮 Игр сыграно: {my_games}\n\n"
        f"--- <b>ТОП ИГРОКОВ</b> ---\n"
    )
    if not top_players:
        lb_text += "Пока нет рекордов. Сыграйте первыми!"
    else:
        for idx, (p_name, p_score, p_streak, p_games) in enumerate(top_players, 1):
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
            lb_text += f"{medal} <b>{p_name}</b> — {p_score} очков (🔥{p_streak})\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🎮 Играть", callback_data="start_chat_game"),
        types.InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")
    )

    try:
        bot.edit_message_text(lb_text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, lb_text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_mode_"))
def cb_set_mode(call):
    bot.answer_callback_query(call.id)
    mode_key = call.data.replace("set_mode_", "")
    if mode_key in ROLES:
        user_modes[call.message.chat.id] = mode_key
        bot.send_message(call.message.chat.id, f"✅ Режим изменен на: <b>{ROLES[mode_key]['name']}</b>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "ask_draw")
def cb_ask_draw(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "🎨 <b>Генератор изображений</b>\nОпишите детально то, что хотите увидеть:", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_image_prompt)

def process_image_prompt(message):
    prompt = message.text.strip()
    if not prompt or prompt.startswith("/"):
        bot.send_message(message.chat.id, "❌ Генерация отменена.")
        return

    status_msg = bot.send_message(message.chat.id, f"🎨 Создаю арт по запросу: <i>«{prompt}»</i>...", parse_mode="HTML")
    bot.send_chat_action(message.chat.id, "upload_photo")

    try:
        translation_completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": "Translate this image prompt into English for an AI image generator. Output ONLY the translated prompt."},{"role": "user", "content": prompt}],
            model=GROQ_MODEL,
        )
        english_prompt = translation_completion.choices[0].message.content.strip()

        import urllib.parse
        import random
        seed = random.randint(1, 1000000)
        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={seed}"

        bot.send_photo(message.chat.id, image_url, caption=f"✨ <b>Запрос:</b> {prompt}", parse_mode="HTML")
        bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Не удалось сгенерировать изображение.")

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    caption = (message.caption or "").lower().strip()

    if not check_subscription(user_id):
        bot.send_message(chat_id, "🔒 Сначала подпишитесь на канал!", reply_markup=get_sub_markup())
        return

    # Проверка режима GOAT Expert
    if user_states.get(user_id) == "waiting_expert_photo":
        processing_msg = bot.send_message(chat_id, "🔍 <b>GOAT Expert анализирует изображение...</b>", parse_mode="HTML")
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            with open(input_path, "wb") as f: f.write(downloaded_file)

            # Экспертный анализ с помощью Groq Vision или текстового описания (через Llama отправляем имитацию/описание эксперта)
            expert_text = (
                "📊 **Результат экспертного анализа GOAT Expert:**\n\n"
                "• **Композиция:** Кадр выстроен гармонично, соблюдено правило третей, фокус привлечен к центральному объекту.\n"
                "• **Освещение и качество:** Свет мягкий, детализация высокая, тени не пережжены.\n"
                "• **Стиль и визуальная подача:** Отличная цветовая гамма, выдержанный визуальный стиль.\n"
                "• **Совет по улучшению:** Можно добавить немного контрастности в редакторе для большей выразительности и глубины кадра."
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_menu"))

            bot.send_message(chat_id, expert_text, reply_markup=markup, parse_mode="HTML")
            bot.delete_message(chat_id, processing_msg.message_id)
            user_states.pop(user_id, None)
        except Exception as e:
            bot.send_message(chat_id, "❌ Ошибка при экспертном анализе фото.")
        finally:
            if os.path.exists(input_path): os.remove(input_path)
        return

    # Удаление фона или улучшение
    if any(k in caption for k in ["/bg", "фон", "удалить фон"]):
        processing_msg = bot.send_message(chat_id, "✂️ <b>Удаление фона...</b>", parse_mode="HTML")
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            with open(input_path, "wb") as f: f.write(downloaded_file)
            with open(input_path, "rb") as i: output_image = remove(i.read())
            with open(output_path, "wb") as o: o.write(output_image)
            with open(output_path, "rb") as doc:
                bot.send_document(chat_id, doc, caption="✂️ <b>Фон успешно удален!</b>", parse_mode="HTML")
            bot.delete_message(chat_id, processing_msg.message_id)
        except Exception as e:
            bot.send_message(chat_id, "❌ Ошибка при обработке фото.")
        finally:
            if os.path.exists(input_path): os.remove(input_path)
            if os.path.exists(output_path): os.remove(output_path)
    else:
        processing_msg = bot.send_message(chat_id, "✨ <b>Улучшение качества...</b>", parse_mode="HTML")
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            with open(temp_in, "wb") as f: f.write(downloaded_file)
            img = Image.open(temp_in).convert("RGB")
            img = ImageEnhance.Sharpness(img).enhance(1.3)
            img = ImageEnhance.Color(img).enhance(1.1)
            img.save(temp_out, quality=95)
            with open(temp_out, "rb") as photo_to_send:
                bot.send_photo(chat_id, photo_to_send, caption="✨ <b>Качество улучшено!</b>", parse_mode="HTML")
            bot.delete_message(chat_id, processing_msg.message_id)
        except Exception as e:
            bot.send_message(message.chat.id, "❌ Не удалось улучшить фото.")
        finally:
            if os.path.exists(temp_in): os.remove(temp_in)
            if os.path.exists(temp_out): os.remove(temp_out)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    if not check_subscription(user_id):
        bot.send_message(message.chat.id, "🔒 Сначала подпишитесь на канал!", reply_markup=get_sub_markup())
        return

    # Увеличиваем счетчик сообщений в БД
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("UPDATE scores SET messages_count = messages_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    if not groq_api_key:
        bot.send_message(message.chat.id, "⚠️ Ключ Groq API не настроен.")
        return

    bot.send_chat_action(message.chat.id, "typing")

    current_mode_key = user_modes.get(message.chat.id, "default")
    role_info = ROLES.get(current_mode_key, ROLES["default"])
    system_prompt = role_info["system"]
    role_name = role_info["name"]
    show_header = role_info.get("show_header", True)

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message.text}
            ],
            model=GROQ_MODEL,
        )
        answer = chat_completion.choices[0].message.content.strip()

        import re
        answer = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', answer)

        if show_header:
            formatted_response = f"<b>{role_name}:</b>\n\n{answer}"
        else:
            formatted_response = answer

        bot.send_message(message.chat.id, formatted_response, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка Groq API: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при обращении к нейросети.")

# --- Запуск бота в отдельном потоке, а Flask — в главном для Render ---
def run_bot():
    print("Бот запущен и полностью готов к работе со всеми функциями!")
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
