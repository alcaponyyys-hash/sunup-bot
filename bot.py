import os
import random
import asyncio
from datetime import datetime, time, timedelta, timezone

import discord
from discord.ext import commands, tasks
import asyncpg
from dotenv import load_dotenv

# =====================
# LOAD ENV (local)
# =====================
# No Railway, as variáveis vêm do painel. Localmente, você pode usar .env.
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# =====================
# CONFIG (PREENCHA!)
# =====================
GUILD_ID = 591819416786698251

MAIN_CHANNEL_ID = 709772107239653396      # canal de aviso (bot online)
DROP_CHANNEL_ID = 709772107239653396      # canal dos drops
JOIN_CHANNEL_ID = 714835863888068688      # canal de participação

TOP_ROLE_ID = 1467871506267902166          # cargo TOP 1
EVENT_ROLE_ID = 1467871344187281541        # cargo ☀️ SUN

JOIN_EMOJI = "☀️"

# Timezone Brasil fixo (-03:00)
TZ = timezone(timedelta(hours=-3))

# janela de drops
START_TIME = time(19, 0)
END_TIME = time(23, 59, 59)

# intervalo aleatório dentro da janela
MIN_WAIT = 600     # 10 min
MAX_WAIT = 1800    # 30 min

# =====================
# DROPS (ESCALÁVEL)
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

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

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
# POSTGRES (asyncpg)
# =====================
db_pool: asyncpg.Pool | None = None


async def init_db():
    """Cria pool e tabelas no Postgres."""
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não encontrado. No Railway, crie um Postgres e adicione a variável DATABASE_URL."
        )

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS points (
            user_id BIGINT PRIMARY KEY,
            score  INTEGER NOT NULL DEFAULT 0
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)


async def add_points(user_id: int, pts: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO points (user_id, score)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET score = points.score + EXCLUDED.score;
        """, int(user_id), int(pts))


async def get_score(user_id: int) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT score FROM points WHERE user_id = $1;", int(user_id))
        return int(row["score"]) if row else 0


async def get_top(limit: int = 10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
        SELECT user_id, score
        FROM points
        ORDER BY score DESC
        LIMIT $1;
        """, int(limit))
        return [(int(r["user_id"]), int(r["score"])) for r in rows]


async def set_setting(key: str, value: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO settings (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value;
        """, key, value)


async def get_setting(key: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1;", key)
        return row["value"] if row else None


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
    if random.random() < float(SUPER_DROP["chance"]):
        return {"emoji": SUPER_DROP["emoji"], "points": SUPER_DROP["points"], "super": True}
    r = pick_regular()
    return {"emoji": r["emoji"], "points": r["points"], "super": False}


# =====================
# SEND DROP
# =====================
async def send_drop(test: bool = False):
    global current_drop_message_id, current_drop_claimed
    global current_drop_emoji, current_drop_points

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return None

    channel = guild.get_channel(DROP_CHANNEL_ID)
    if not channel:
        return None

    current_drop_claimed = False
    selected = pick_drop()

    current_drop_emoji = selected["emoji"]
    current_drop_points = int(selected["points"])

    title = "🌟 **SUPER DROP!**" if selected["super"] else "☀️ **SUN UP DROP!**"
    ping = f"<@&{EVENT_ROLE_ID}>" if EVENT_ROLE_ID else ""
    prefix = "🧪 **DROP DE TESTE** 🧪\n" if test else ""

    content = (
        f"{prefix}{ping}\n{title}\n"
        f"Primeiro que reagir com {current_drop_emoji} ganha **{current_drop_points} ponto(s)**!"
    ).strip()

    msg = await channel.send(
        content,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

    current_drop_message_id = msg.id

    try:
        await msg.add_reaction(current_drop_emoji)
    except:
        # se falhar, o usuário pode reagir manualmente
        pass

    return msg


# =====================
# HELPERS: JOIN MESSAGE
# =====================
async def ensure_join_message(guild: discord.Guild):
    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if join_id:
        return int(join_id)

    channel = guild.get_channel(JOIN_CHANNEL_ID)
    if not channel:
        return None

    msg = await channel.send(
        "☀️ **SUN UP — PARTICIPAÇÃO**\n"
        f"Reaja com {JOIN_EMOJI} para participar do evento e receber o cargo **☀️ SUN**.\n"
        "Se remover a reação, você sai do evento (cargo removido)."
    )

    try:
        await msg.add_reaction(JOIN_EMOJI)
    except:
        pass

    await set_setting(JOIN_MESSAGE_KEY, str(msg.id))
    return msg.id


# =====================
# EVENTS
# =====================
@bot.event
async def on_ready():
    await init_db()

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("⚠️ Não encontrei o servidor. Verifique GUILD_ID.")
        return

    await ensure_join_message(guild)

    main = guild.get_channel(MAIN_CHANNEL_ID)
    if main:
        await main.send(
            "✅ **SUN UP Bot online** — Drops ativos das **19h às 23h59**",
            allowed_mentions=discord.AllowedMentions.none()
        )

    if not drop_loop.is_running():
        drop_loop.start()

    print("SUN UP Bot online ✅")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    global current_drop_claimed

    if not bot.user:
        return

    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return

    # ===== JOIN (participação) =====
    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if join_id and payload.message_id == int(join_id):
        if str(payload.emoji) == JOIN_EMOJI:
            role = guild.get_role(EVENT_ROLE_ID) if EVENT_ROLE_ID else None
            if role:
                try:
                    await member.add_roles(role, reason="Entrou no evento SUN UP (reação)")
                except:
                    pass
        return

    # ===== DROP (pontuação) =====
    if payload.message_id != current_drop_message_id:
        return

    if str(payload.emoji) != current_drop_emoji:
        return

    async with drop_lock:
        if current_drop_claimed:
            return

        current_drop_claimed = True
        await add_points(member.id, current_drop_points)

        channel = guild.get_channel(payload.channel_id)
        if channel:
            await channel.send(
                f"🎉 {discord.utils.escape_markdown(member.display_name)} ganhou **{current_drop_points} ponto(s)**!",
                allowed_mentions=discord.AllowedMentions.none()
            )


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    join_id = await get_setting(JOIN_MESSAGE_KEY)
    if not join_id or payload.message_id != int(join_id):
        return

    if str(payload.emoji) != JOIN_EMOJI:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return

    role = guild.get_role(EVENT_ROLE_ID) if EVENT_ROLE_ID else None
    if role:
        try:
            await member.remove_roles(role, reason="Saiu do evento SUN UP (removeu reação)")
        except:
            pass


# =====================
# DROP LOOP AUTOMÁTICO
# =====================
@tasks.loop(seconds=60)
async def drop_loop():
    if not in_window():
        return

    await asyncio.sleep(random.randint(MIN_WAIT, MAX_WAIT))

    if in_window():
        await send_drop(test=False)


# =====================
# COMMANDS
# =====================
@bot.command(name="drop")
@commands.has_permissions(administrator=True)
async def drop_test(ctx: commands.Context):
    """Força um drop manual (teste)."""
    msg = await send_drop(test=True)
    if msg:
        await ctx.send("🧪 Drop de teste enviado!", delete_after=5)
    else:
        await ctx.send("Não consegui enviar o drop. Verifique IDs/permissões.", delete_after=8)


@bot.command(name="rank")
async def rank(ctx: commands.Context):
    top = await get_top(10)
    if not top:
        await ctx.send(
            "Ainda não tem ranking. Bora reagir nos drops do SUN UP ☀️",
            allowed_mentions=discord.AllowedMentions.none()
        )
        return

    lines = []
    for i, (uid, pts) in enumerate(top, start=1):
        member = ctx.guild.get_member(uid) if ctx.guild else None
        name = discord.utils.escape_markdown(member.display_name) if member else f"User {uid}"
        lines.append(f"{i}. {name} — {pts}")

    await ctx.send(
        "🏆 **RANKING SUN UP** 🏆\n" + "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.command(name="meuspontos")
async def meuspontos(ctx: commands.Context):
    pts = await get_score(ctx.author.id)
    await ctx.send(
        f"☀️ Você tem **{pts} ponto(s)**",
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.command(name="help_sunup")
async def help_sunup(ctx: commands.Context):
    await ctx.send(
        "☀️ **SUN UP — AJUDA** ☀️\n\n"
        "• Reaja com ☀️ na mensagem de participação para receber o cargo **☀️ SUN**\n"
        "• Drops acontecem entre **19h e 23h59**\n"
        "• Cada drop mostra **1 emoji**\n"
        "• O primeiro a reagir com o emoji certo ganha pontos\n\n"
        "**Pontuação:**\n"
        "☀️ 1 ponto | 🌊 2 pontos | 🍹 3 pontos\n"
        "🌟 SUPER DROP — 10 pontos (1%)\n\n"
        "**Comandos:**\n"
        "`!rank` • `!meuspontos` • `!drop` (admin)\n",
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.command(name="setupjoin")
@commands.has_permissions(administrator=True)
async def setupjoin(ctx: commands.Context):
    """Cria/recria a mensagem de participação e salva o ID no DB."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.send("Não encontrei o servidor (GUILD_ID).")
        return

    channel = guild.get_channel(JOIN_CHANNEL_ID)
    if not channel:
        await ctx.send("Não encontrei o canal de participação (JOIN_CHANNEL_ID).")
        return

    msg = await channel.send(
        "☀️ **SUN UP — PARTICIPAÇÃO**\n"
        f"Reaja com {JOIN_EMOJI} para participar do evento e receber o cargo **☀️ SUN**.\n"
        "Se remover a reação, você sai do evento (cargo removido)."
    )

    try:
        await msg.add_reaction(JOIN_EMOJI)
    except:
        pass

    await set_setting(JOIN_MESSAGE_KEY, str(msg.id))
    await ctx.send(
        f"Mensagem de participação criada ✅ {msg.jump_url}",
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.command(name="check")
@commands.has_permissions(administrator=True)
async def check(ctx: commands.Context):
    guild = bot.get_guild(GUILD_ID)

    ok_guild = bool(guild)
    ok_main = bool(guild and guild.get_channel(MAIN_CHANNEL_ID))
    ok_drop = bool(guild and guild.get_channel(DROP_CHANNEL_ID))
    ok_join = bool(guild and guild.get_channel(JOIN_CHANNEL_ID))
    ok_event_role = bool(guild and EVENT_ROLE_ID and guild.get_role(EVENT_ROLE_ID))
    ok_top_role = bool(guild and TOP_ROLE_ID and guild.get_role(TOP_ROLE_ID))

    join_msg = await get_setting(JOIN_MESSAGE_KEY)

    await ctx.send(
        "🔎 **CHECK SUN UP BOT**\n"
        f"Agora: `{now().strftime('%d/%m %H:%M:%S')}` (UTC-03)\n"
        f"Janela: `19:00–23:59` -> {'DENTRO' if in_window() else 'FORA'}\n"
        f"GUILD_ID: `{GUILD_ID}` -> {'OK' if ok_guild else 'NÃO'}\n"
        f"MAIN_CHANNEL_ID: `{MAIN_CHANNEL_ID}` -> {'OK' if ok_main else 'NÃO'}\n"
        f"DROP_CHANNEL_ID: `{DROP_CHANNEL_ID}` -> {'OK' if ok_drop else 'NÃO'}\n"
        f"JOIN_CHANNEL_ID: `{JOIN_CHANNEL_ID}` -> {'OK' if ok_join else 'NÃO'}\n"
        f"EVENT_ROLE_ID: `{EVENT_ROLE_ID}` -> {'OK' if ok_event_role else 'NÃO'}\n"
        f"TOP_ROLE_ID: `{TOP_ROLE_ID}` -> {'OK' if ok_top_role else 'NÃO'}\n"
        f"JOIN_MESSAGE_ID (DB): `{join_msg}`\n"
        f"DATABASE_URL: `{'OK' if bool(DATABASE_URL) else 'NÃO'}`\n",
        allowed_mentions=discord.AllowedMentions.none()
    )


# =====================
# RUN
# =====================
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN não encontrado (Railway Variables ou .env).")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não encontrado (adicione Postgres no Railway e exponha a variável).")

bot.run(TOKEN)

