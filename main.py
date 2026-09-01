#!/usr/bin/env python3
"""
Telegram Bot for MLBB CN31 Lookup / Ban / Creation Date
- 3 separate buttons
- 5 free checks per day per user (admin unlimited)
- Uses CN31 token for ban checks
"""

import asyncio
import json
import logging
import socket
import struct
import sys
import time
from datetime import datetime, date
from typing import Dict, Optional, List, Any, Tuple

import requests
import zstandard as zstd
from Crypto.Cipher import AES
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ============================================================
# CONFIGURATION (EDIT THESE)
# ============================================================
BOT_TOKEN = "8702549007:AAGoaaBDMgqmYo9Apo_GtO6NVPzl7cmBlwk"   # YOUR BOT TOKEN
ADMIN_ID = 8621676055                                         # YOUR TELEGRAM USER ID (integer)

# ============================================================
# DAILY LIMIT TRACKING (in-memory)
# ============================================================
user_daily_usage = {}  # {user_id: {"date": "2026-09-01", "count": int}}

def get_today_str() -> str:
    return date.today().isoformat()

def get_remaining_checks(user_id: int) -> int:
    """Return remaining checks for today (admin always returns 999)."""
    if user_id == ADMIN_ID:
        return 999
    today = get_today_str()
    if user_id not in user_daily_usage or user_daily_usage[user_id]["date"] != today:
        user_daily_usage[user_id] = {"date": today, "count": 0}
    used = user_daily_usage[user_id]["count"]
    return max(0, 5 - used)

def consume_check(user_id: int) -> bool:
    """Attempt to consume one check. Returns True if successful."""
    if user_id == ADMIN_ID:
        return True
    today = get_today_str()
    if user_id not in user_daily_usage or user_daily_usage[user_id]["date"] != today:
        user_daily_usage[user_id] = {"date": today, "count": 0}
    if user_daily_usage[user_id]["count"] >= 5:
        return False
    user_daily_usage[user_id]["count"] += 1
    return True

# ============================================================
# ORIGINAL SCRIPT LOGIC (copied from lookupss.py)
# ============================================================

# CN31 Servers
CN31_SERVERS = [
    "http://217.216.35.81:8080",
    "http://217.216.35.129:8082",
    "http://62.146.237.138:8080",
    "https://solver-server-production.up.railway.app",
    "http://solver-server-production.up.railway.app",
    "https://solver-solver-production.up.railway.app",
    "http://solver-solver-production.up.railway.app",
    "https://solar-solver-production.up.railway.app",
    "http://solar-solver-production.up.railway.app",
]

TOKEN_PATHS = [
    "/get-token",
    "/token",
    "/cookies",
    "/?device_id={device_id}",
    "/v1/token",
    "/api/token",
    "/cn31",
]

AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV = b'\x00' * 16

class SdpDataType:
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END << 4])

    def _unpack_from_binary(self):
        if not self.data:
            return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value: int) -> bytes:
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

    def _pack_header(self, tag: int, data_type: int) -> None:
        if tag < 15:
            self.data += bytes([(data_type << 4) | tag])
        else:
            self.data += bytes([(data_type << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag: int, value: Any) -> None:
        if isinstance(value, bool):
            self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpDataType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed))
            self.data += packed
        elif isinstance(value, str) or isinstance(value, bytes):
            self._pack_header(tag, SdpDataType.STRING)
            encoded = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(encoded))
            self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpDataType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()):
                    self._pack(k, v)
                self.data += bytes([SdpDataType.STRUCT_END << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise Exception(f"Unsupported type: {type(value)}")

    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data):
                return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            data_type = header >> 4
            self.offset += 1
            if tag == 15:
                tag = self._read_number()
            if data_type == SdpDataType.INTEGER_POSITIVE:
                return tag, self._read_number()
            elif data_type == SdpDataType.INTEGER_NEGATIVE:
                return tag, -self._read_number()
            elif data_type == SdpDataType.FLOAT:
                value = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", value)[0]
            elif data_type == SdpDataType.DOUBLE:
                value = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", value)[0]
            elif data_type == SdpDataType.STRING:
                length = self._read_number()
                try:
                    value = self.data[self.offset:self.offset + length].decode('utf-8')
                except UnicodeDecodeError:
                    value = self.data[self.offset:self.offset + length]
                self.offset += length
                return tag, value
            elif data_type == SdpDataType.LIST:
                length = self._read_number()
                value = []
                for _ in range(length):
                    _, item = self._unpack()
                    value.append(item)
                return tag, value
            elif data_type == SdpDataType.DICT:
                length = self._read_number()
                value = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    value[k] = v
                return tag, value
            elif data_type == SdpDataType.STRUCT_BEGIN:
                struct_data = {}
                while True:
                    sub_tag, sub_value = self._unpack()
                    if sub_value == SdpDataType.STRUCT_END:
                        break
                    struct_data[sub_tag] = sub_value
                return tag, SdpStruct(struct_data)
            elif data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else:
                raise Exception(f"Unknown data type: {data_type}")
        except Exception as e:
            raise Exception(f"Error unpacking data: {e}")

class TCPResolver:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.sock = None
        self.sequence = 1
        self.queue_data = b''

        parts = device_id.split('_')
        if len(parts) >= 2:
            dev_info = parts[1]
            self.imei_md5 = dev_info[:32] if len(dev_info) >= 32 else dev_info
            self.android_id = dev_info[32:48] if len(dev_info) >= 48 else ""
            self.advertising_id = dev_info[48:] if len(dev_info) > 48 else ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect(('login.ml.youngjoygame.com', 30021))
        self.sock.settimeout(8)

    def _close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send_data(self, packet_id, sdp):
        packet = SdpStruct({
            0: packet_id,
            1: self.sequence,
            5: sdp.data
        }).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, 'big') + buf
        self.sock.send(buf)
        self.sequence += 1

    def _recv_data(self):
        try:
            while len(self.queue_data) < 4:
                data = self.sock.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 0xFFFFFF
            comp_type = flags >> 24

            while len(self.queue_data) < size:
                data = self.sock.recv(4096)
                if not data:
                    return None, None
                self.queue_data += data

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]

            if comp_type == 16:
                data = zstd.decompress(data)
            elif comp_type == 2:
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(data).rstrip(b'\x00')
            else:
                pass

            result = SdpStruct(data)
            packet_id = result[0]
            if packet_id is None:
                return None, None

            res = result.get(6, result.get(5, None))
            if not res or not isinstance(res, bytes):
                return packet_id, None

            return packet_id, SdpStruct(res)

        except socket.timeout:
            return -1, None
        except Exception:
            return None, None

    def resolve(self) -> Dict:
        try:
            self._connect()
            self._send_data(1, SdpStruct({
                0: self.device_id,
                1: f'gps_adid={self.advertising_id}&android_id={self.android_id}&device_unique_id={self.imei_md5}',
                2: '2.1.61.1173.1',
                3: 'and_usa',
                4: 'en'
            }))

            pkt_id, res = self._recv_data()

            if pkt_id == 2 and res:
                self._close()
                return {
                    "success": True,
                    "account_id": res.get(0),
                    "zone_id": res.get(2, [0])[0] if res.get(2) else 0,
                    "session_key": res.get(1),
                    "ban_flag": res.get(3) or res.get(10) or res.get(20),
                    "creation_ts": res.get(19, 0),
                }
            else:
                self._close()
                return {"success": False, "error": "Login failed"}
        except Exception as e:
            return {"success": False, "error": f"TCP error: {str(e)[:80]}"}

def fetch_cn31_token(device_id: str) -> Optional[str]:
    """Try all CN31 servers and token endpoints to get a token."""
    for server in CN31_SERVERS:
        for path_template in TOKEN_PATHS:
            url = server + path_template.format(device_id=device_id)
            try:
                resp = requests.get(url, timeout=5, headers={"Accept": "application/json"})
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        token = data.get("token") or data.get("access_token") or data.get("cookie") or data.get("cn31")
                        if token:
                            return str(token)
                    except:
                        text = resp.text.strip()
                        if text.startswith("CN31_"):
                            return text
            except:
                continue
    return None

def check_ban_http(account_id: int, zone_id: int, token: str) -> Tuple[bool, str]:
    if not token:
        return False, "No token"
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'Referer': 'https://account.cn31.mobilelegends.com/',
    })
    endpoints = [
        f'https://account.cn31.mobilelegends.com/v1/ban/info?uid={account_id}&zone={zone_id}',
        f'https://account.cn31.mlbb.com/v1/status?uid={account_id}',
    ]
    for url in endpoints:
        try:
            resp = session.get(url, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ban_status", 0) > 0:
                    reason = data.get("ban_reason", "Permanent Ban")
                    return True, reason
                if data.get("is_banned") is True:
                    return True, data.get("reason", "Unknown")
                if data.get("code") in (1002, 1003, 1004):
                    return True, f"Code {data.get('code')}"
                if data.get("ban_time", 0) > 0:
                    return True, f"Banned until {data.get('ban_time')}"
            if resp.status_code in (401, 403):
                continue
        except:
            continue
    return False, ""

PUBLIC_API = "https://mlbbbbv2.onrender.com/lookup"

def fetch_public_stats(account_id: int, zone_id: int) -> Dict:
    try:
        resp = requests.post(PUBLIC_API, json={"role_id": str(account_id), "zone_id": str(zone_id)}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return {"success": True, "data": data.get("player_data", {})}
        return {"success": False, "error": "API error"}
    except:
        return {"success": False, "error": "API timeout"}

def format_ts(ts: int) -> str:
    if not ts or ts == 0:
        return "N/A"
    if ts > 10000000000:
        ts_sec = int(ts / 1000)
    else:
        ts_sec = int(ts)
    try:
        return datetime.fromtimestamp(ts_sec).strftime('%Y-%m-%d %H:%M:%S UTC')
    except:
        return str(ts)

# ============================================================
# BOT HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    remaining = get_remaining_checks(user_id)
    keyboard = [
        [
            InlineKeyboardButton("📊 Lookup (stats)", callback_data="lookup"),
            InlineKeyboardButton("🚫 Ban Check", callback_data="ban"),
            InlineKeyboardButton("📅 Creation Date", callback_data="creation"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        "🤖 *MLBB CN31 Checker*\n"
        f"Remaining checks today: *{remaining if remaining < 999 else '∞'}*\n\n"
        "Choose an action below. You will be prompted to send a Device ID."
    )
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data
    context.user_data["action"] = action
    await query.edit_message_text(
        f"📨 Please send the *Device ID* for {action.upper()}.",
        parse_mode="Markdown"
    )

async def handle_device_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    device_id = update.message.text.strip()
    if not device_id:
        await update.message.reply_text("❌ Empty input. Please send a valid Device ID.")
        return

    action = context.user_data.get("action")
    if not action:
        await update.message.reply_text("❌ Please use /start to choose an action first.")
        return

    if not consume_check(user_id):
        remaining = get_remaining_checks(user_id)
        await update.message.reply_text(
            f"❌ You have used all 5 free checks for today. Remaining: {remaining}. Try again tomorrow."
        )
        return

    status_msg = await update.message.reply_text("⏳ Processing... please wait.")

    try:
        if action == "lookup":
            result = await asyncio.to_thread(do_lookup, device_id)
        elif action == "ban":
            result = await asyncio.to_thread(do_ban_check, device_id)
        elif action == "creation":
            result = await asyncio.to_thread(do_creation_date, device_id)
        else:
            result = "❌ Unknown action."

        await status_msg.edit_text(result, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)[:500]}")

# ============================================================
# ACTION FUNCTIONS (run synchronously in threads)
# ============================================================

def do_lookup(device_id: str) -> str:
    resolver = TCPResolver(device_id)
    tcp = resolver.resolve()
    if not tcp.get("success"):
        return f"❌ TCP resolve failed: {tcp.get('error', 'Unknown')}"

    account_id = tcp.get("account_id")
    zone_id = tcp.get("zone_id")
    if not account_id:
        return "❌ No account_id obtained."

    stats = fetch_public_stats(account_id, zone_id)
    if not stats.get("success"):
        return f"⚠️ Public stats unavailable: {stats.get('error', 'No data')}"

    pd = stats.get("data", {})
    lines = [
        "📊 *LOOKUP RESULTS*",
        f"Account ID: `{account_id}`",
        f"Zone ID: `{zone_id}`",
        f"Nickname: {pd.get('nickname', '—')}",
        f"Level: {pd.get('level', '—')}",
        f"Current Rank: {pd.get('current_rank', '—')}",
        f"Highest Rank: {pd.get('high_rank', '—')}",
        f"Heroes: {pd.get('hero_count', '—')}",
        f"Skins: {pd.get('skin_count', '—')}",
        f"Win Rate: {pd.get('win_rate', '—')}%",
        f"Matches: {pd.get('matches', '—')}",
        f"MVP: {pd.get('mvp', '—')}",
        f"Location: {pd.get('location', '—')}",
        f"Last Login: {pd.get('last_login', '—')}",
        f"Collector Tier: {pd.get('collector_tier', '—')} ({pd.get('collector_point', '—')} pts)",
    ]
    if pd.get("squad"):
        lines.append(f"Squad: {pd.get('squad')}")

    breakdown = pd.get('skin_breakdown', {})
    if breakdown:
        lines.append("Skin Breakdown:")
        for tier in ["Supreme", "Grand", "Exquisite", "Deluxe", "Exceptional", "Common"]:
            val = breakdown.get(tier, 0)
            if val:
                lines.append(f"  {tier}: {val}")

    top_heroes = pd.get('top_heroes', [])
    if top_heroes:
        lines.append("🏅 Top Heroes:")
        for i, hero in enumerate(top_heroes[:5], 1):
            if isinstance(hero, dict):
                name = hero.get("hero_name") or hero.get("name") or "Unknown"
                mmr = hero.get("mmr") or hero.get("power") or "—"
                lines.append(f"  {i}. {name} (MMR: {mmr})")
            else:
                lines.append(f"  {i}. {hero}")

    return "\n".join(lines)

def do_ban_check(device_id: str) -> str:
    token = fetch_cn31_token(device_id)
    if not token:
        return "❌ Could not obtain CN31 token from any server."

    resolver = TCPResolver(device_id)
    tcp = resolver.resolve()
    if not tcp.get("success"):
        return f"❌ TCP resolve failed: {tcp.get('error', 'Unknown')}"

    account_id = tcp.get("account_id")
    zone_id = tcp.get("zone_id")
    if not account_id:
        return "❌ No account_id obtained."

    banned, reason = check_ban_http(account_id, zone_id, token)
    if banned:
        return f"🚫 *BANNED*\nReason: {reason or 'Unknown'}\nAccount: `{account_id}`"
    else:
        return f"✅ *VALID* (not banned)\nAccount: `{account_id}`"

def do_creation_date(device_id: str) -> str:
    resolver = TCPResolver(device_id)
    tcp = resolver.resolve()
    if not tcp.get("success"):
        return f"❌ TCP resolve failed: {tcp.get('error', 'Unknown')}"

    creation_ts = tcp.get("creation_ts", 0)
    account_id = tcp.get("account_id")
    zone_id = tcp.get("zone_id")
    if not account_id:
        return "❌ No account_id obtained."

    formatted = format_ts(creation_ts)
    return (
        f"📅 *Creation Date*\n"
        f"Account: `{account_id}`\n"
        f"Zone: `{zone_id}`\n"
        f"Timestamp: `{creation_ts}`\n"
        f"Date: {formatted}"
    )

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_id))
    print("🤖 Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()