import asyncio
import logging
import aiosqlite
import pandas as pd
from datetime import datetime, date
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)



logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ✅ 初始化数据库
async def init_db():
    async with aiosqlite.connect("finance.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                type TEXT,
                amount REAL,
                source TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS excel_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT,
                upload_date TEXT
            )
        """)
        await db.commit()

# ✅ 保存交易记录
async def save_transaction(date, t_type, amount, source):
    async with aiosqlite.connect("finance.db") as db:
        await db.execute(
            "INSERT INTO transactions (date, type, amount, source) VALUES (?, ?, ?, ?)",
            (date, t_type, amount, source)
        )
        await db.commit()

# ✅ 保存 Excel 文件信息
async def save_excel_info(file_name):
    upload_date = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("finance.db") as db:
        await db.execute(
            "INSERT INTO excel_files (file_name, upload_date) VALUES (?, ?)",
            (file_name, upload_date)
        )
        await db.commit()

# ✅ 获取统计
async def get_summary(target_date=None):
    async with aiosqlite.connect("finance.db") as db:
        if target_date:
            cursor = await db.execute("SELECT type, amount FROM transactions WHERE date = ?", (target_date,))
        else:
            cursor = await db.execute("SELECT type, amount FROM transactions")
        rows = await cursor.fetchall()

    income = sum(r[1] for r in rows if r[0] == "income")
    expense = sum(r[1] for r in rows if r[0] == "expense")
    return income, expense, income - expense

# ✅ 获取 Excel 文件（按上传日期）
async def get_excel_files_by_date(target_date):
    async with aiosqlite.connect("finance.db") as db:
        cursor = await db.execute("SELECT file_name FROM excel_files WHERE upload_date = ?", (target_date,))
        files = await cursor.fetchall()
    return [f[0] for f in files]

# ✅ /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Кіріс қосу", switch_inline_query_current_chat="/add income "),
            InlineKeyboardButton(text="💸 Шығын қосу", switch_inline_query_current_chat="/add expense "),
        ],
        [
            InlineKeyboardButton(text="📊 Жалпы есеп", switch_inline_query_current_chat="/summary"),
            InlineKeyboardButton(text="📅 Бүгінгі есеп", switch_inline_query_current_chat="/today"),
        ],
        [
            InlineKeyboardButton(text="📤 Excel жүктеу", switch_inline_query_current_chat="/upload"),
            InlineKeyboardButton(text="📂 Excel алу", switch_inline_query_current_chat="/getexcel "),
        ]
    ])

    await message.answer(
        "Сәлем! Мен сіздің қаржылық көмекшіңізмін 🤖\n\n"
        "Маған Excel файлын жіберіңіз немесе төмендегі батырмаларды пайдаланыңыз:\n"
        "`/add income 2000` немесе `/add expense 500`\n\n"
        "📊 Сондай-ақ:\n"
        "`/summary` — барлық кіріс/шығысты көру\n"
        "`/today` — бүгінгі статистиканы көру\n"
        "`/summary 2025-10-04` — белгілі күнді көру",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ✅ /add
@dp.message(Command("add"))
async def add_manual(message: Message):
    try:
        args = message.text.split()
        if len(args) != 3:
            raise ValueError
        t_type = args[1].lower()
        amount = float(args[2])
        if t_type not in ["income", "expense"]:
            await message.answer("❌ Түрін дұрыс көрсетіңіз: income немесе expense")
            return

        date_now = datetime.now().strftime("%Y-%m-%d")
        await save_transaction(date_now, t_type, amount, "manual")
        await message.answer(f"✅ {t_type} {amount} тг {date_now} күні сақталды！")

    except Exception:
        await message.answer("❌ Формат қате. Мысалы: `/add income 2000`", parse_mode="Markdown")

# ✅ /summary
@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    args = message.text.split()
    target_date = args[1] if len(args) == 2 else None
    income, expense, balance = await get_summary(target_date)
    title = f"📅 {target_date} күнгі есеп:" if target_date else "📊 Жалпы есеп:"
    await message.answer(f"{title}\n💰 Кіріс: {income:.2f} тг\n💸 Шығын: {expense:.2f} тг\n⚖️ Баланс: {balance:.2f} тг")

# ✅ /today
@dp.message(Command("today"))
async def cmd_today(message: Message):
    today_str = date.today().strftime("%Y-%m-%d")
    income, expense, balance = await get_summary(today_str)
    await message.answer(
        f"📅 Бүгінгі ({today_str}) есеп:\n"
        f"💰 Кіріс: {income:.2f} тг\n💸 Шығын: {expense:.2f} тг\n⚖️ Баланс: {balance:.2f} тг"
    )

# ✅ /getexcel
@dp.message(Command("getexcel"))
async def cmd_getexcel(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Қате формат. Мысалы: `/getexcel 2025-10-04`")
        return

    target_date = args[1]
    files = await get_excel_files_by_date(target_date)
    if not files:
        await message.answer(f"📂 {target_date} үшін ешқандай Excel табылмады.")
        return

    for file_name in files:
        with open(file_name, "rb") as f:
            await bot.send_document(message.chat.id, f, caption=f"📎 {file_name}")

# ✅ /upload
@dp.message(Command("upload"))
async def cmd_upload(message: Message):
    await message.answer("📤 Excel файлын жіберіңіз (.xlsx немесе .xls), мен оны талдап сақтаймын.")

# ✅ 接收 Excel 文件
@dp.message(lambda msg: msg.document)
async def handle_excel_file(message: Message):
    file_name = message.document.file_name
    if not (file_name.endswith(".xlsx") or file_name.endswith(".xls")):
        await message.answer("❌ Тек Excel файлдарын (.xlsx немесе .xls) қабылдаймын.")
        return

    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, file_name)
    await save_excel_info(file_name)

    try:
        df = pd.read_excel(file_name)
        count = 0
        for _, row in df.iterrows():
            date_val = str(row.get("Date", datetime.now().strftime("%Y-%m-%d")))
            t_type = str(row.get("Type", "income")).lower()
            amount = float(row.get("Amount", 0))
            await save_transaction(date_val, t_type, amount, "excel")
            count += 1
        await message.answer(f"📊 Файл '{file_name}' жүктелді және {count} жазба сақталды!")
    except Exception as e:
        await message.answer(f"❌ Excel оқу кезінде қате: {e}")

# ✅ 启动主程序
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
