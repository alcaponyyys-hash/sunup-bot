import os
import random
import asyncio
from datetime import datetime, time, timedelta, timezone

import discord
from discord.ext import commands, tasks
import aiosqlite
from dotenv import load_dotenv

# =====================
# LOAD TOKEN
# =====================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# =====================
# CONFIG (PREENCHA)
# =====================
GUILD_ID = 1254110433346977983

MAIN_CHANNEL_ID = 1254110433980190722      # canal de aviso (bot online)
DROP_CHANNEL_ID = 1254110433980190722      # canal dos drops
JOIN_CHANNEL_ID = 1402645989978013767      # canal de participação

TOP_ROLE_ID = 1465857386232287263          # cargo TOP 1
EVENT_ROLE_ID = 1467642321867640994        # cargo ☀️ SUN

JOIN_EMOJI = "☀️"

# Timezone fixo Brasil (-03:00)
TZ = timezone(timedelta(hours=-3))

START_TIME = time(19, 0)
END_TIME = time(23, 59, 59)

MIN_WAIT = 600    # 10 min
MAX_WAIT = 1800   # 30 min

DB_PATH = "sunup.db"

# =====================
# DROPS
# =====================
REGULAR_POOL = [
    {"emoji": "☀️", "points": 1, "weight": 60},
    {"emoji": "🌊", "points": 2, "weight": 30},
    {"emoji": "🍹", "points": 3, "weight": 10},
]

SUPER_DROP = {
    "chance": 0.01,  # 1%
    "emoji": "🌟",
    "points": 10
}

# =====================
# BOT / INTENTS
# =====================
intents = discord.Intents.default()
intents.reactions = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =====================
# ESTADO DO DROP
# =====================
current_drop_message_id = None
current_drop_claimed = False
current_drop_emoji = None
current_drop_points = 0
drop_lock = asyncio.Lock()

JOIN_MESSAGE_KEY = "join_message_id"

# =====================
# DATABASE
# =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS points (
            user_id INTEGER PRIMARY KEY,
            score INTEGER DEFAULT 0
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        await db.commit()

async def add_points(user_id, pts):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO points(user_id, score)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET score = score + excluded.score
        """, (user_id, pts))
        await db.commit()

async def get_score(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT score FROM points WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_top(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, score FROM points ORDER BY score DESC LIMIT ?",
            (limit,)
        )
        return await cur.fetchall()

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        await db.commit()

async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

# =====================
# TIME HELPERS
# =====================
def now():
    return datetime.now(TZ)

def in_window():
    t = now().time()
    return START_TIME <= t <= END_TIME

# =====================
# DROP PICK
# =====================
def pick_regular():
    return random.choices(
        REGULAR_POOL,
        weights=[i["weight"] for i in REGULAR_POOL],
        k=1
    )[0]

def pick_drop():
    if random.random() < SUPER_DROP["chance"]:
        return {"emoji": SUPER_DROP["emoji"], "points": SUPER_DROP["points"], "super": True}
    r = pick_regular()
    return {"emoji": r["emoji"], "points": r["points"], "super": False}

# =====================
# SEND DROP
# =====================
async def send_drop():
    global current_drop_message_id, current_drop_claimed
    global current_drop_emoji, current_drop_points

    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(DROP_CHANNEL_ID)

    current_drop_claimed = False
    selected = pick_drop()

    current_drop_emoji = selected["emoji"]
    current_drop_points = selected["points"]

    title = "🌟 **SUPER DROP!**" if selected["super"] else "☀️ **SUN UP DROP!**"
    ping = f"<@&{EVENT_ROLE_ID}>"

    msg = await channel.send(
        f"{ping}\n{title}\n"
        f"Primeiro que reagir com {current_drop_emoji} ganha **{current_drop_points} ponto(s)**!",
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

    current_drop_message_id = msg.id

    try:
        await msg.add_reaction(current_drop_emoji)
    except:
        pass

# =====================
# EVENTS
# =====================
@bot.event
async def on_ready():
    await init_db()
    guild = bot.get_guild(GUILD_ID)

    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if not join_id:
        ch = guild.get_channel(JOIN_CHANNEL_ID)
        msg = await ch.send(
            "☀️ **SUN UP — PARTICIPAÇÃO**\n"
            "Reaja com ☀️ para participar do evento e receber o cargo **☀️ SUN**."
        )
        await msg.add_reaction(JOIN_EMOJI)
        await set_setting(JOIN_MESSAGE_KEY, str(msg.id))

    main = guild.get_channel(MAIN_CHANNEL_ID)
    await main.send("✅ **SUN UP Bot online** — Drops ativos das **19h às 23h59**")

    drop_loop.start()
    print("SUN UP Bot online")

@bot.event
async def on_raw_reaction_add(payload):
    global current_drop_claimed

    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)

    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if join_id and payload.message_id == int(join_id):
        if str(payload.emoji) == JOIN_EMOJI:
            await member.add_roles(guild.get_role(EVENT_ROLE_ID))
        return

    if payload.message_id != current_drop_message_id:
        return

    if str(payload.emoji) != current_drop_emoji:
        return

    async with drop_lock:
        if current_drop_claimed:
            return
        current_drop_claimed = True

        await add_points(member.id, current_drop_points)
        ch = guild.get_channel(payload.channel_id)
        await ch.send(f"🎉 {member.display_name} ganhou **{current_drop_points} ponto(s)**!")

@bot.event
async def on_raw_reaction_remove(payload):
    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if not join_id or payload.message_id != int(join_id):
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    await member.remove_roles(guild.get_role(EVENT_ROLE_ID))

# =====================
# DROP LOOP
# =====================
@tasks.loop(seconds=60)
async def drop_loop():
    if not in_window():
        return
    await asyncio.sleep(random.randint(MIN_WAIT, MAX_WAIT))
    if in_window():
        await send_drop()

# =====================
# COMMANDS
# =====================
@bot.command()
async def rank(ctx):
    top = await get_top()
    lines = []
    for i, (uid, pts) in enumerate(top, start=1):
        member = ctx.guild.get_member(uid)
        name = discord.utils.escape_markdown(member.display_name if member else f"User {uid}")
        lines.append(f"{i}. {name} — {pts}")

    await ctx.send(
        "🏆 **RANKING SUN UP** 🏆\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none()
    )

@bot.command()
async def meuspontos(ctx):
    pts = await get_score(ctx.author.id)
    await ctx.send(f"☀️ Você tem **{pts} ponto(s)**", allowed_mentions=discord.AllowedMentions.none())

@bot.command()
async def shelp(ctx):
    await ctx.send(
        "☀️ **SUN UP — AJUDA** ☀️\n\n"
        "• Reaja com ☀️ para participar do evento\n"
        "• Drops entre **19h e 23h59**\n"
        "• Primeiro a reagir ganha pontos\n\n"
        "☀️ 1 ponto | 🌊 2 pontos | 🍹 3 pontos\n"
        "🌟 SUPER DROP — 10 pontos (1%)\n\n"
        "`!rank` • `!meuspontos`",
        allowed_mentions=discord.AllowedMentions.none()
    )

# =====================
# RUN
# =====================
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN não encontrado")

bot.run(TOKEN)
