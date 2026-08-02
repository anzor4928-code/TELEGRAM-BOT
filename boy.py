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

# --- 1. Настройка базы данных для Топа Игроков ---
def init_db():
    conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            score INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- 2. Настройка Flask для 24/7 работы (чтобы Render не усыплял бота) ---
app = Flask("")

@app.route("/")
def home():
    return "🚀 Bot Server is active and running smoothly!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()

keep_alive()

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

# Словарь для хранения выбранного режима каждого пользователя
user_modes = {}

# Активные сессии игры в чате: {user_id: {"score": 0, "q_index": 0, "correct_ans": True}}
game_sessions = {}

QUESTIONS = [
    { "text": "В Сингапуре официально запрещено ввозить и продавать жевательную резинку?", "truth": True },
    { "text": "Кот в Японии официально занимал должность начальника железнодорожной станции?", "truth": True },
    { "text": "В Великобритании разрешено законно убивать шотландцев из лука по пятницам?", "truth": False },
    { "text": "Первый в мире сайт (info.cern.ch) до сих пор работает по своему оригинальному адресу?", "truth": True },
    { "text": "Во Франции по закону запрещено называть свиней именем «Наполеон»?", "truth": True },
    { "text": "Осьминоги имеют три сердца и голубую кровь?", "truth": True },
    { "text": "В Швейцарии после 22:00 по закону запрещено смывать унитаз в квартирах?", "truth": False },
    { "text": "Бананы растут на гигантских деревьях с крепким деревянным стволом?", "truth": False },
    { "text": "На Сардинии владельцам собак запрещено выгуливать их реже трех раз в день?", "truth": True },
    { "text": "Молния никогда не ударяет дважды в одно и то же место?", "truth": False },
    { "text": "В Исландии полностью отсутствуют комары?", "truth": True },
    { "text": "Акулы появились на Земле раньше, чем деревья?", "truth": True },
    { "text": "У коровы может быть лучший друг, и они сильно скучают, когда их разлучают?", "truth": True },
    { "text": "В США существует закон, запрещающий появляться с мороженым в кармане по воскресеньям?", "truth": True },
    { "text": "Мёд может храниться тысячелетиями и никогда не портится?", "truth": True },
    { "text": "Эйфелева башня становится выше в холодные зимние месяцы?", "truth": False },
    { "text": "Человеческий мозг генерирует достаточно электричества, чтобы питать небольшую лампочку?", "truth": True },
    { "text": "В австралийском городе Мельбурн запрещено носить розовые шорты после полудня в воскресенье?", "truth": False },
    { "text": "Пингвины умеют летать под водой?", "truth": True },
    { "text": "Клубника является ягодой с точки зрения ботаники?", "truth": False },
    { "text": "В Аризоне (США) рубка кактусов карается тюремным заключением на срок до 25 лет?", "truth": True },
    { "text": "У стрекоз зрение охватывает почти 360 градусов?", "truth": True },
    { "text": "В состав губной помады входит рыбья чешуя?", "truth": True },
    { "text": "Морские огурцы дышат через свой задний проход?", "truth": True },
    { "text": "У улиток может быть до 25 000 зубов?", "truth": True },
    { "text": "Все полярные медведи левши?", "truth": False },
    { "text": "Самое короткое в мире эссе состоит из одного слова?", "truth": False },
    { "text": "В Риме кошки имеют законное право жить в исторических руинах без выселения?", "truth": True },
    { "text": "Арахис является орехом?", "truth": False },
    { "text": "В Японии можно купить квадратные арбузы, чтобы их было удобнее хранить в холодильнике?", "truth": True },
    { "text": "Фламинго розовеют из-за того, что едят водоросли, богатые каротиноидами?", "truth": True },
    { "text": "В Австралии кенгуру больше, чем людей?", "truth": True },
    { "text": "Великая Китайская стена видна из космоса невооруженным глазом?", "truth": False },
    { "text": "У человека костей больше в детстве, чем во взрослом возрасте?", "truth": True },
    { "text": "В космосе космонавты могут расти в росте на несколько сантиметров из-за отсутствия гравитации?", "truth": True },
    { "text": "Звук распространяется в воде быстрее, чем в воздухе?", "truth": True },
    { "text": "Самая длинная река в мире — это Амазонка?", "truth": False },
    { "text": "У мышей щекотка вызывает смех, который слышен человеку?", "truth": False },
    { "text": "В Антарктиде есть одна действующая река под названием Оникс?", "truth": True },
    { "text": "Паутина прочнее стали при сравнении одинаковой толщины нитей?", "truth": True },
    { "text": "Абсолютный ноль по шкале Кельвина равен -273.15 градусам Цельсия?", "truth": True },
    { "text": "В Древнем Риме мочу использовали для стирки и отбеливания одежды?", "truth": True },
    { "text": "Тигры имеют полосатую не только шерсть, но и кожу под ней?", "truth": True },
    { "text": "В пучине океанов давление настолько сильное, что алмазы там плавятся в жидкость?", "truth": False },
    { "text": "Альберт Эйнштейн завалил экзамен по математике в школе?", "truth": False },
    { "text": "У кошек на передних лапах больше пальцев, чем на задних?", "truth": True },
    { "text": "В Лондоне официально запрещено умирать в здании Парламента?", "truth": True },
    { "text": "Хамелеоны меняют цвет кожи исключительно для маскировки под окружающую среду?", "truth": False },
    { "text": "Некоторые виды грибов способны управлять поведением насекомых, превращая их в зомби?", "truth": True },
    { "text": "Самая высокая гора в Солнечной системе находится на Марсе?", "truth": True },
    { "text": "В человеческом теле содержится достаточно железа, чтобы сделать небольшой гвоздь?", "truth": True },
    { "text": "В Новой Зеландии овец больше, чем людей?", "truth": True },
    { "text": "У коров есть региональные акценты в их «мычании»?", "truth": True },
    { "text": "Деревья могут общаться друг с другом под землей с помощью грибковой сети?", "truth": True },
    { "text": "В Ватикане уровень преступности на душу населения выше, чем в любом другом государстве?", "truth": True },
    { "text": "Около 70% кислорода на Земле производят океанские водоросли и фитопланктон?", "truth": True },
    { "text": "Комары предпочитают кусать людей с первой группой крови чаще остальных?", "truth": False },
    { "text": "В природе существуют черные лебеди?", "truth": True },
    { "text": "Вомбаты производят экскременты кубической формы?", "truth": True },
    { "text": "Первый будильник в истории мог звонить только в одно фиксированное время — в 4 часа утра?", "truth": False },
    { "text": "Человеческий глаз способен различать около 10 миллионов оттенков цветов?", "truth": True },
    { "text": "В Плутоне сутки длятся дольше, чем один полный земной год?", "truth": False },
    { "text": "На Венере идут дожди из расплавленного свинца?", "truth": False },
    { "text": "У гусениц больше мышц, чем у взрослого человека?", "truth": True },
    { "text": "В Древней Греции бросание яблока в девушку считалось официальным предложением руки и сердца?", "truth": True },
    { "text": "Некоторые черепахи умеют дышать через задний проход во время зимовки под водой?", "truth": True },
    { "text": "У слонов потовые железы расположены исключительно на кончике хобота?", "truth": False },
    { "text": "В Саудовской Аравии песок импортируют из других стран?", "truth": True },
    { "text": "Укус муравья-пули считается одним из самых болезненных укусов насекомых в мире?", "truth": True },
    { "text": "Слово «робот» впервые появилось в научно-фантастической пьесе Карела Чапека?", "truth": True },
    { "text": "Собаки способны понимать до 250 слов и жестов человека?", "truth": True },
    { "text": "В Орегоне (США) разрешено самостоятельно заправлять бензин на любой заправке?", "truth": False },
    { "text": "У совы глазные яблоки неподвижны в глазницах, поэтому они могут поворачивать голову на 270 градусов?", "truth": True },
    { "text": "Колибри — единственная птица, способная летать задом наперед?", "truth": True },
    { "text": "В теле взрослого человека содержится около 100 тысяч километров кровеносных сосудов?", "truth": True },
    { "text": "Сахар вызывает привыкание сильнее, чем некоторые наркотические вещества?", "truth": True },
    { "text": "Молния обладает температурой, которая в несколько раз горячее поверхности Солнца?", "truth": True },
    { "text": "На Луне следы астронавтов программы «Аполлон» останутся навечно из-за отсутствия атмосферы и ветра?", "truth": True },
    { "text": "У акул есть веки, которыми они полностью закрывают глаза при нападении?", "truth": False },
    { "text": "В Средние века животных судили в судах наравне с людьми за совершенные преступления?", "truth": True },
    { "text": "Пингвины могут прыгать в высоту на 2 метра над водой?", "truth": True },
    { "text": "Вода замерзает быстрее в горячем виде, чем в холодном (эффект Мпембы)?", "truth": True },
    { "text": "У мужчин волосы на голове растут быстрее, чем у женщин?", "truth": False },
    { "text": "В Антарктиде официально запрещено иметь домашних кошек и собак?", "truth": True },
    { "text": "Глаз страуса больше, чем его собственный мозг?", "truth": True },
    { "text": "В Токио больше ресторанов, отмеченных звездами Мишлен, чем в Париже?", "truth": True },
    { "text": "Люди могут слышать звук падения метеорита в верхних слоях атмосферы в реальном времени?", "truth": False },
    { "text": "У змей нет ушей, но они прекрасно улавливают звуковые вибрации земли через челюсть?", "truth": True },
    { "text": "В Древнем Египте жрецы сбривали абсолютно все волосы на теле, включая брови и ресницы?", "truth": True },
    { "text": "Самый громкий звук живой природы издают крошечные раки-щелкуны?", "truth": True },
    { "text": "В США существует закон, запрещающий кидать китовых усов на тротуар по воскресеньям?", "truth": True },
    { "text": "У пчел пять глаз?", "truth": True },
    { "text": "В Сахаре иногда выпадает снег?", "truth": True },
    { "text": "Морские котики могут спать под водой, задерживая дыхание?", "truth": True },
    { "text": "У медуз мозг состоит из нервного кольца и полностью автономен?", "truth": False },
    { "text": "В некоторых штатах США до сих пор действует запрет на ловлю рыбы на лошади?", "truth": True },
    { "text": "Земля вращается вокруг Солнца по абсолютно идеальной круговой орбите?", "truth": False },
    { "text": "У ленивцев на переваривание одного листочка может уходить до двух недель?", "truth": True },
    { "text": "В эпоху Возрождения богатые люди ели толченые египетские мумии как лекарство от всех болезней?", "truth": True },
    { "text": "Самое долгоживущее млекопитающее на Земле — гренландский кит?", "truth": True }
]

# Настройки ролей
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

@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_name = message.from_user.first_name or "друг"

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_draw = types.InlineKeyboardButton("🎨 Сгенерировать арт", callback_data="ask_draw")
    btn_modes = types.InlineKeyboardButton("🎭 Выбрать ИИ-личность", callback_data="choose_mode")

    btn_game = types.InlineKeyboardButton("🎮 Игра: Правда или Ложь", callback_data="start_chat_game")
    btn_top = types.InlineKeyboardButton("🏆 Топ игроков", callback_data="show_leaderboard")

    btn_help = types.InlineKeyboardButton("📖 Справка", callback_data="help")
    btn_about = types.InlineKeyboardButton("✨ О системе", callback_data="about")

    markup.add(btn_draw, btn_modes, btn_game, btn_top, btn_help, btn_about)

    welcome_text = (
        f"✨ <b>Добро пожаловать, {user_name}!</b>\n"
        "Я — ваш персональный ИИ-ассистент. Могу общаться в разных ролях, обрабатывать фото, генерировать картинки и играть в игры прямо в чате!\n\n"
        "🛠 <b>Доступный функционал:</b>\n"
        "• 🎭 <b>Личности ИИ</b> — команда <code>/modes</code>\n"
        "• 🎮 <b>Игра в чате</b> — «Правда или Ложь» (100+ вопросов) с очками\n"
        "• 🌐 <b>Универсальный переводчик</b> — <code>/tr [текст]</code>\n"
        "• 🎨 <b>Генератор изображений</b> — <code>/img</code>\n"
        "• ✂️ <b>Удаление фона / Улучшение фото</b>\n\n"
        "💬 <i>Просто отправьте сообщение в чат или выберите кнопку ниже!</i>"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["help"])
def send_help(message):
    help_text = (
        "📖 <b>Справочник по командам:</b>\n"
        "🔹 <code>/start</code> — Перезапустить бота и меню\n"
        "🔹 <code>/modes</code> — Сменить характер или эксперта ИИ\n"
        "🔹 <code>/game</code> — Запустить игру «Правда или Ложь» прямо в чате\n"
        "🔹 <code>/tr [текст]</code> — Быстрый перевод текста\n"
        "🔹 <code>/img</code> — Сгенерировать картинку\n\n"
        "📸 <b>Работа с фотографиями:</b>\n"
        "• Отправьте фото с подписью <code>фон</code>, чтобы вырезать объект\n"
        "• Отправьте фото без подписи для улучшения качества"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(commands=["modes"])
def command_modes(message):
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
    import random
    q_index = random.randint(0, len(QUESTIONS) - 1)
    q_data = QUESTIONS[q_index]

    game_sessions[user_id] = {
        "score": game_sessions.get(user_id, {}).get("score", 0),
        "q_index": q_index,
        "correct_ans": q_data["truth"]
    }

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_true = types.InlineKeyboardButton("✅ Правда", callback_data="ans_true")
    btn_false = types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false")
    markup.add(btn_true, btn_false)

    current_score = game_sessions[user_id]["score"]
    text = (
        f"🎮 <b>Игра: Правда или Ложь</b>\n"
        f"⭐ Ваши очки: <b>{current_score}</b>\n\n"
        f"📌 <b>Вопрос:</b>\n{q_data['text']}"
    )
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["tr", "translate"])
def command_translate(message):
    text_to_translate = message.text.replace("/tr", "").replace("/translate", "").strip()
    if not text_to_translate:
        bot.send_message(message.chat.id, "🌐 <b>Ошибка:</b> укажите текст для перевода.\n<i>Пример:</i> <code>/tr Привет мир</code>", parse_mode="HTML")
        return

    bot.send_chat_action(message.chat.id, "typing")
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a professional universal translator. If Russian, translate to English. If other, translate to Russian. Output ONLY the translated text."},
                {"role": "user", "content": text_to_translate}
            ],
            model=GROQ_MODEL,
        )
        translation = completion.choices[0].message.content.strip()
        bot.send_message(message.chat.id, f"🌐 <b>Результат перевода:</b>\n{translation}", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Не удалось выполнить перевод.")

@bot.message_handler(commands=["img"])
def command_img_interactive(message):
    msg = bot.send_message(message.chat.id, "🎨 <b>Генератор изображений</b>\nОпишите детально то, что хотите увидеть:", parse_mode="HTML")
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
    caption = (message.caption or "").lower().strip()

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

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    username = call.from_user.first_name or "Игрок"

    if call.data == "ask_draw":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(chat_id, "🎨 <b>Генератор изображений</b>\nОпишите детально то, что хотите увидеть:", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_image_prompt)

    elif call.data == "choose_mode":
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, role in ROLES.items():
            if key == "default":
                continue
            markup.add(types.InlineKeyboardButton(role["name"], callback_data=f"set_mode_{key}"))

        current_mode_key = user_modes.get(chat_id, "default")
        current_name = ROLES[current_mode_key]["name"]

        modes_header = (
            "🎭 <b>Центр управления личностями</b>\n"
            "Выберите, с кем именно вы хотите продолжить диалог.\n\n"
            f"📌 Текущий активный режим: <b>{current_name}</b>"
        )
        bot.send_message(chat_id, modes_header, reply_markup=markup, parse_mode="HTML")

    elif call.data.startswith("set_mode_"):
        bot.answer_callback_query(call.id)
        mode_key = call.data.replace("set_mode_", "")
        if mode_key in ROLES:
            user_modes[chat_id] = mode_key
            bot.send_message(chat_id, f"✅ Режим изменен на: <b>{ROLES[mode_key]['name']}</b>", parse_mode="HTML")

    elif call.data == "start_chat_game":
        bot.answer_callback_query(call.id)
        import random
        q_index = random.randint(0, len(QUESTIONS) - 1)
        q_data = QUESTIONS[q_index]

        game_sessions[user_id] = {
            "score": game_sessions.get(user_id, {}).get("score", 0),
            "q_index": q_index,
            "correct_ans": q_data["truth"]
        }

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_true = types.InlineKeyboardButton("✅ Правда", callback_data="ans_true")
        btn_false = types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false")
        markup.add(btn_true, btn_false)

        current_score = game_sessions[user_id]["score"]
        text = (
            f"🎮 <b>Игра: Правда или Ложь</b>\n"
            f"⭐ Ваши очки: <b>{current_score}</b>\n\n"
            f"📌 <b>Вопрос:</b>\n{q_data['text']}"
        )
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    elif call.data in ["ans_true", "ans_false"]:
        bot.answer_callback_query(call.id)

        if user_id not in game_sessions:
            import random
            q_index = random.randint(0, len(QUESTIONS) - 1)
            q_data = QUESTIONS[q_index]
            game_sessions[user_id] = {
                "score": 0,
                "q_index": q_index,
                "correct_ans": q_data["truth"]
            }

        session = game_sessions[user_id]
        user_choice = (call.data == "ans_true")
        correct = session["correct_ans"]

        if user_choice == correct:
            session["score"] += 10
            res_text = "🎉 **Правильно!** +10 очков."
        else:
            session["score"] = max(0, session["score"] - 5)
            res_text = "❌ **Неверно!** -5 очков."

        conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT score FROM scores WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO scores (user_id, username, score) VALUES (?, ?, ?)", (user_id, username, session["score"]))
        else:
            if session["score"] > row[0]:
                cursor.execute("UPDATE scores SET score = ?, username = ? WHERE user_id = ?", (session["score"], username, user_id))
        conn.commit()
        conn.close()

        import random
        q_index = random.randint(0, len(QUESTIONS) - 1)
        q_data = QUESTIONS[q_index]
        session["q_index"] = q_index
        session["correct_ans"] = q_data["truth"]

        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_true = types.InlineKeyboardButton("✅ Правда", callback_data="ans_true")
        btn_false = types.InlineKeyboardButton("❌ Ложь", callback_data="ans_false")
        markup.add(btn_true, btn_false)

        next_text = (
            f"{res_text}\n\n"
            f"🎮 <b>Ваш счет: {session['score']} очков</b>\n\n"
            f"📌 <b>Следующий вопрос:</b>\n{q_data['text']}"
        )
        try:
            bot.edit_message_text(next_text, chat_id=chat_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, next_text, reply_markup=markup, parse_mode="HTML")

    elif call.data == "show_leaderboard":
        bot.answer_callback_query(call.id)
        conn = sqlite3.connect("leaderboard.db", check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT username, score FROM scores ORDER BY score DESC LIMIT 10")
        top_players = cursor.fetchall()
        conn.close()

        lb_text = "🏆 <b>Топ-10 игроков:</b>\n\n"
        if not top_players:
            lb_text += "Пока нет рекордов. Сыграйте первыми через /game!"
        else:
            for idx, (p_name, p_score) in enumerate(top_players, 1):
                medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
                lb_text += f"{medal} <b>{p_name}</b> — {p_score} очков\n"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🎮 Играть", callback_data="start_chat_game"))

        try:
            bot.edit_message_text(lb_text, chat_id=chat_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, lb_text, reply_markup=markup, parse_mode="HTML")

    elif call.data == "help":
        bot.answer_callback_query(call.id)
        send_help(call.message)
    elif call.data == "about":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "🤖 <b>Системная информация:</b>\n• Бот работает 24/7 на Python\n• Нейросеть: Llama 3.3 (Groq)\n• Встроена масштабная игра в чате (100+ вопросов) с таблицей лидеров", parse_mode="HTML")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
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

if __name__ == "__main__":
    print("Бот запущен и полностью готов к работе!")
    bot.infinity_polling()
