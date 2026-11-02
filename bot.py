import os
import re
import sqlite3

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", "0"))

GUILD = discord.Object(id=GUILD_ID)
MC_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# 서버 분위기에 맞춘 색상 (붉은 계열)
THEME_COLOR = 0xE05A4F

# ----------------------------------------------------------------------
# 데이터베이스 (guests.db 파일에 저장돼요)
# ----------------------------------------------------------------------
db = sqlite3.connect("guests.db")
db.execute(
    """
    CREATE TABLE IF NOT EXISTS guests (
        discord_id INTEGER PRIMARY KEY,
        uuid TEXT UNIQUE NOT NULL,
        mc_name TEXT NOT NULL,
        kor_name TEXT NOT NULL
    )
    """
)
db.commit()


def uuid_owner(uuid: str):
    """이 UUID로 이미 등록한 디스코드 유저 ID를 돌려줘요. 없으면 None."""
    row = db.execute("SELECT discord_id FROM guests WHERE uuid = ?", (uuid,)).fetchone()
    return row[0] if row else None


def save_guest(discord_id: int, uuid: str, mc_name: str, kor_name: str):
    db.execute(
        "INSERT OR REPLACE INTO guests (discord_id, uuid, mc_name, kor_name) VALUES (?, ?, ?, ?)",
        (discord_id, uuid, mc_name, kor_name),
    )
    db.commit()


# ----------------------------------------------------------------------
# 마인크래프트 닉네임 조회 (Mojang API)
# ----------------------------------------------------------------------
def format_uuid(raw: str) -> str:
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


async def fetch_profile(name: str):
    """닉네임이 있으면 (정확한 닉네임, UUID)를, 없으면 None을 돌려줘요."""
    url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["name"], format_uuid(data["id"])
            if resp.status in (204, 404):
                return None
            raise RuntimeError(f"Mojang API 응답 오류: {resp.status}")


# ----------------------------------------------------------------------
# 2단계: 확인 버튼
# ----------------------------------------------------------------------
class ConfirmView(discord.ui.View):
    def __init__(self, mc_name: str, uuid: str, kor_name: str, new_nick: str):
        super().__init__(timeout=180)
        self.mc_name = mc_name
        self.uuid = uuid
        self.kor_name = kor_name
        self.new_nick = new_nick

    @discord.ui.button(label="이 계정이 맞아요", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        member = interaction.user
        role = interaction.guild.get_role(GUEST_ROLE_ID)
        if role is None:
            await interaction.edit_original_response(
                content="손님 역할을 찾지 못했어요. 관리자에게 문의해 주세요. (GUEST_ROLE_ID 확인 필요)",
                embed=None,
                view=None,
            )
            return

        # 다른 사람이 먼저 같은 계정으로 등록했는지 한 번 더 확인
        owner = uuid_owner(self.uuid)
        if owner is not None and owner != member.id:
            await interaction.edit_original_response(
                content="이미 다른 디스코드 계정으로 등록된 마인크래프트 계정이에요.",
                embed=None,
                view=None,
            )
            return

        save_guest(member.id, self.uuid, self.mc_name, self.kor_name)

        problems = []

        try:
            await member.add_roles(role, reason="손님 등록")
        except discord.Forbidden:
            problems.append("역할 부여 실패: 봇의 역할이 손님 역할보다 위에 있는지 확인해 주세요.")

        try:
            await member.edit(nick=self.new_nick, reason="손님 등록")
        except discord.Forbidden:
            problems.append("닉네임 변경 실패: 서버 주인이거나 봇보다 높은 역할이면 봇이 바꿀 수 없어요.")

        if problems:
            text = "등록은 저장됐지만 일부 작업이 안 됐어요.\n" + "\n".join(f"- {p}" for p in problems)
        else:
            text = f"✅ 등록이 끝났어요! 이제 **{self.new_nick}** 님으로 표시돼요. PRISM SHOP에 오신 걸 환영합니다!"

        await interaction.edit_original_response(content=text, embed=None, view=None)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="등록을 취소했어요. 다시 하려면 등록 버튼을 눌러 주세요.",
            embed=None,
            view=None,
        )
        self.stop()


# ----------------------------------------------------------------------
# 1단계: 닉네임 입력창 (모달)
# ----------------------------------------------------------------------
class RegisterModal(discord.ui.Modal, title="손님 등록"):
    mc_name = discord.ui.TextInput(
        label="마인크래프트 닉네임",
        placeholder="마인크래프트 닉네임을 입력해 주세요",
        min_length=3,
        max_length=16,
    )
    kor_name = discord.ui.TextInput(
        label="한글 닉네임",
        placeholder="서버에서 사용할 한글 닉네임을 입력해 주세요",
        min_length=1,
        max_length=12,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        name = self.mc_name.value.strip()
        kor = self.kor_name.value.strip()

        if not MC_NAME_PATTERN.match(name):
            await interaction.followup.send(
                "마인크래프트 닉네임은 영문, 숫자, _ 만 사용할 수 있어요. (3~16자)", ephemeral=True
            )
            return

        try:
            profile = await fetch_profile(name)
        except Exception:
            await interaction.followup.send(
                "마인크래프트 서버에서 정보를 가져오지 못했어요. 잠시 후 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send(
                "그런 마인크래프트 닉네임을 찾을 수 없어요. 철자를 다시 확인해 주세요.", ephemeral=True
            )
            return

        real_name, uuid = profile

        owner = uuid_owner(uuid)
        if owner is not None and owner != interaction.user.id:
            await interaction.followup.send(
                "이미 다른 디스코드 계정으로 등록된 마인크래프트 계정이에요.", ephemeral=True
            )
            return

        new_nick = f"{kor} · {real_name}"

        embed = discord.Embed(title="🔴 계정 정보 확인", color=THEME_COLOR)
        embed.description = "✨ 아래 정보가 내 계정과 맞는지 확인해 주세요."
        embed.add_field(name="마인크래프트 닉네임", value=real_name, inline=False)
        embed.add_field(name="UUID", value=uuid, inline=False)
        embed.add_field(name="한글 닉네임", value=kor, inline=False)
        embed.add_field(name="적용될 서버 닉네임", value=new_nick, inline=False)
        embed.set_footer(text="PRISM SHOP")

        view = ConfirmView(real_name, uuid, kor, new_nick)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ----------------------------------------------------------------------
# 채널에 올려두는 "손님 등록하기" 버튼
# ----------------------------------------------------------------------
class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 봇을 껐다 켜도 버튼이 계속 동작해요

    @discord.ui.button(
        label="손님 등록하기",
        style=discord.ButtonStyle.primary,
        emoji="✨",
        custom_id="guest_register_button",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterModal())


# ----------------------------------------------------------------------
# 봇 본체
# ----------------------------------------------------------------------
class GuestBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(RegisterView())
        await self.tree.sync(guild=GUILD)

    async def on_ready(self):
        print(f"로그인 완료: {self.user}")


bot = GuestBot()


@bot.tree.command(
    name="setup",
    description="이 채널에 손님 등록 안내와 버튼을 올려요 (관리자 전용)",
    guild=GUILD,
)
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(title="🔴 PRISM SHOP에 오신 걸 환영해요!", color=THEME_COLOR)
    embed.description = (
        "✨ 그림 주문을 하려면 먼저 손님 등록이 필요해요.\n"
        "✨ 아래 버튼을 눌러 닉네임을 입력하면 끝!\n\n"
        "등록이 끝나면 손님 역할이 자동으로 지급돼요."
    )
    embed.set_footer(text="PRISM SHOP")
    await interaction.channel.send(embed=embed, view=RegisterView())
    await interaction.response.send_message("등록 안내를 올렸어요!", ephemeral=True)


if not TOKEN or not GUILD_ID or not GUEST_ROLE_ID:
    raise SystemExit(".env 파일에 DISCORD_TOKEN, GUILD_ID, GUEST_ROLE_ID를 모두 채워 주세요.")

bot.run(os.getenv('DISCORD_TOKEN'))