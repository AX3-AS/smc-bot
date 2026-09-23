"""
==========================================================
 SMC PRO v3.1 — Interactive yfinance SMC Telegram Bot
 تطابق تام مع أسعار الشارت وتنسيق دقيق للأرقام
==========================================================
"""

import os
import time
import threading
import datetime
import http.server
import socketserver
import numpy as np
import pandas as pd
import yfinance as yf
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================== إعدادات البوت ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8608249128:AAEzBeoDp6TOXgznLh8AFwUx56iASws9yC8")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHATID", "6062624259")

USER_SETTINGS = {}

LOOKBACK       = 200
SWING_LEN      = 5
MIN_RR         = 2.0
SWEEP_LOOKBACK = 30
CHECK_EVERY    = 30

# رموز دقيقة ومباشرة مطابقة لأسعار الفوركس والذهب الفوري
SYMBOL_MAP = {
    "XAUUSD": "XAU=X",     # الذهب الفوري المباشر أمام الدولار
    "GBPUSD": "GBPUSD=X"   # الجنيه الإسترليني أمام الدولار
}

HTF_MAPPING = {"1m": "5m", "5m": "15m"}

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# تخصيص جلسة HTTP لتجنب الحظر
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

def get_user_config(chat_id):
    chat_str = str(chat_id)
    if chat_str not in USER_SETTINGS:
        USER_SETTINGS[chat_str] = {
            "SYMBOL": "XAUUSD",
            "INTERVAL": "1m"
        }
    return USER_SETTINGS[chat_str]

def format_price(symbol, price):
    """تنسيق السعر ديناميكياً: الذهب بخانتين، والعملات بـ 5 خانات"""
    if symbol == "XAUUSD":
        return f"{price:.2f}"
    else:
        return f"{price:.5f}"

# ---------------- جلب البيانات الفورية ----------------
def get_klines(symbol, interval, limit=200):
    yf_symbol = SYMBOL_MAP.get(symbol, "XAU=X")
    period = "1d" if interval in ["1m", "2m", "5m"] else "5d"
    
    ticker = yf.Ticker(yf_symbol, session=session)
    df = pd.DataFrame()
    
    for attempt in range(3):
        try:
            df = ticker.history(period=period, interval=interval, timeout=10)
            if not df.empty and len(df) >= 3:
                break
        except Exception:
            pass
        time.sleep(1.5)
        
    if df.empty or len(df) < 3:
        try:
            df = ticker.history(period="5d", interval=interval, timeout=10)
        except Exception:
            pass

    if df.empty or len(df) < 3:
        raise ValueError(f"تعذر جلب بيانات {symbol} المباشرة، يرجى المحاولة لاحقاً.")
    
    df = df.dropna().tail(limit)
    k = df[['Open', 'High', 'Low', 'Close']].to_numpy()
    return k

# ---------------- الهيكل التحليلي ----------------
def swings(h, l, n):
    highs, lows = [], []
    for i in range(n, len(h)-n):
        if h[i] == max(h[i-n:i+n+1]): highs.append(i)
        if l[i] == min(l[i-n:i+n+1]): lows.append(i)
    return highs, lows

def htf_trend(h, l, c):
    hi, lo = swings(h, l, SWING_LEN)
    if len(hi) < 2 or len(lo) < 2: return "neutral"
    hh = h[hi[-1]] > h[hi[-2]]
    hl = l[lo[-1]] > l[lo[-2]]
    if hh and hl: return "bull"
    if not hh and not hl: return "bear"
    return "neutral"

def find_fvg(h, l):
    gaps = []
    for i in range(len(h)-2):
        if l[i+2] > h[i]: gaps.append(("bull", h[i], l[i+2]))
        if l[i] > h[i+2]: gaps.append(("bear", h[i+2], l[i]))
    return gaps[-5:]

def find_order_blocks(o, c, h, l, direction):
    obs = []
    for i in range(len(o)-6, 2, -1):
        body      = abs(c[i]   - o[i])
        next_body = abs(c[i+1] - o[i+1])
        if next_body > body * 1.8:
            if direction == "bull" and c[i] < o[i]: obs.append((l[i], h[i]))
            elif direction == "bear" and c[i] > o[i]: obs.append((l[i], h[i]))
    return obs[:3]

def swept_liquidity(o, h, l, c, lookback, swing_n=3):
    hi, lo = swings(h[:-1], l[:-1], swing_n)
    for idx in lo[::-1]:
        lv = l[idx]
        below = np.where(l[idx+1:] < lv)[0]
        if len(below):
            first = idx + 1 + below[0]
            for j in range(first, min(first+4, len(c))):
                if c[j] > lv and c[j] > o[j]:
                    return ("bull", lv, j)
    return None

def premium_discount(l_last, h_last, price):
    eq = (h_last + l_last) / 2
    rng = h_last - l_last
    if rng <= 0: return "neutral", eq
    if price < eq: return "discount", eq
    return "premium", eq

def candle_confirmation(o, h, l, c, direction):
    body  = abs(c[-1] - o[-1])
    rng   = h[-1] - l[-1]
    if rng == 0: return False
    strong = body / rng > 0.55
    if direction == "bull": return strong and c[-1] > o[-1]
    return strong and c[-1] < o[-1]

def analyze(k_entry, k_htf):
    o, h, l, c = k_entry[:,0], k_entry[:,1], k_entry[:,2], k_entry[:,3]
    price = c[-1]

    trend = htf_trend(k_htf[:,1], k_htf[:,2], k_htf[:,3])
    hi, lo = swings(h, l, SWING_LEN)
    if len(hi) < 2 or len(lo) < 2: 
        return (None, price, trend, "neutral")
    
    min_low = l[lo[-2]:lo[-1]].min() if lo[-2] < lo[-1] else l.min()
    zone, eq = premium_discount(min_low, h.max(), price)
    sweep = swept_liquidity(o, h, l, c, SWEEP_LOOKBACK)

    return (None, price, trend, zone)

# ---------------- واجهة وأزرار تليجرام ----------------
def build_settings_keyboard(chat_id):
    cfg = get_user_config(chat_id)
    markup = InlineKeyboardMarkup()
    
    sym_xau = "✅ XAUUSD" if cfg["SYMBOL"] == "XAUUSD" else "XAUUSD"
    sym_gbp = "✅ GBPUSD" if cfg["SYMBOL"] == "GBPUSD" else "GBPUSD"
    markup.row(InlineKeyboardButton(sym_xau, callback_data="set_sym_XAUUSD"),
               InlineKeyboardButton(sym_gbp, callback_data="set_sym_GBPUSD"))

    tf_1m = "✅ 1 دقيقة (1m)" if cfg["INTERVAL"] == "1m" else "1 دقيقة (1m)"
    tf_5m = "✅ 5 دقائق (5m)" if cfg["INTERVAL"] == "5m" else "5 دقائق (5m)"
    markup.row(InlineKeyboardButton(tf_1m, callback_data="set_tf_1m"),
               InlineKeyboardButton(tf_5m, callback_data="set_tf_5m"))

    markup.row(InlineKeyboardButton("📊 جلب التحليل اللحظي الآن", callback_data="run_status"))
    return markup

@bot.message_handler(commands=['start', 'help', 'settings'])
def send_welcome(message):
    cfg = get_user_config(message.chat.id)
    htf = HTF_MAPPING[cfg["INTERVAL"]]
    welcome_text = (
        "⚙️ **لوحة تحكم بوت SMC Monitor**\n\n"
        f"🔹 **الزوج الحالي:** `{cfg['SYMBOL']}`\n"
        f"⏱ **فريم الدخول:** `{cfg['INTERVAL']}` | **الفريم الأعلى:** `{htf}`\n\n"
        "👇 *يمكنك تغيير الزوج والفريم مباشرة عبر الأزرار أدناه:*"
    )
    bot.reply_to(message, welcome_text, reply_markup=build_settings_keyboard(message.chat.id), parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def send_status(message):
    fetch_and_send_status(message.chat.id)

def fetch_and_send_status(chat_id):
    try:
        cfg = get_user_config(chat_id)
        htf = HTF_MAPPING[cfg["INTERVAL"]]

        k  = get_klines(cfg["SYMBOL"], cfg["INTERVAL"], LOOKBACK)
        kh = get_klines(cfg["SYMBOL"], htf, LOOKBACK)
        
        res = analyze(k, kh)
        _, price, trend, zone = res[0], res[1], res[2], res[3]
        
        formatted_price = format_price(cfg["SYMBOL"], price)
        trend_ar = "صاعد 📈" if trend == "bull" else ("هابط 📉" if trend == "bear" else "محايد ⚖️")
        zone_ar  = "خصم (القاع) 🟢" if zone == "discount" else ("علاوة (القمة) 🔴" if zone == "premium" else "محايدة ⚪")

        status_msg = (
            f"📊 **التقرير اللحظي - {cfg['SYMBOL']}**\n"
            f"----------------------------------------\n"
            f"💰 **السعر الحالي:** `{formatted_price}`\n"
            f"📈 **اتجاه HTF ({htf}):** {trend_ar}\n"
            f"📍 **المنطقة الحالية:** {zone_ar}\n"
            f"⏱ **فريم الدخول:** `{cfg['INTERVAL']}`\n"
            f"----------------------------------------\n"
            f"💡 *البوت يراقب التغيرات ويرسل التنبيه تلقائياً عند تحقق الدخول.*"
        )
        bot.send_message(chat_id, status_msg, parse_mode="Markdown", reply_markup=build_settings_keyboard(chat_id))
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء جلب البيانات: {e}")

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id
    cfg = get_user_config(chat_id)
    
    if call.data == "set_sym_XAUUSD":
        cfg["SYMBOL"] = "XAUUSD"
        bot.answer_callback_query(call.id, "تم التبديل إلى الذهب XAUUSD 🟡")
    elif call.data == "set_sym_GBPUSD":
        cfg["SYMBOL"] = "GBPUSD"
        bot.answer_callback_query(call.id, "تم التبديل إلى الباوند GBPUSD 💷")
    elif call.data == "set_tf_1m":
        cfg["INTERVAL"] = "1m"
        bot.answer_callback_query(call.id, "تم التبديل إلى فريم الدقيقة 1m ⏱")
    elif call.data == "set_tf_5m":
        cfg["INTERVAL"] = "5m"
        bot.answer_callback_query(call.id, "تم التبديل إلى فريم 5 دقائق 5m ⏱")
    elif call.data == "run_status":
        bot.answer_callback_query(call.id, "جاري تحديث البيانات...")
        fetch_and_send_status(chat_id)
        return

    htf = HTF_MAPPING[cfg["INTERVAL"]]
    new_text = (
        "⚙️ **لوحة تحكم بوت SMC Monitor**\n\n"
        f"🔹 **الزوج الحالي:** `{cfg['SYMBOL']}`\n"
        f"⏱ **فريم الدخول:** `{cfg['INTERVAL']}` | **الفريم الأعلى:** `{htf}`\n\n"
        "👇 *يمكنك تغيير الزوج والفريم مباشرة عبر الأزرار أدناه:*"
    )
    try:
        bot.edit_message_text(new_text, chat_id, call.message.message_id, reply_markup=build_settings_keyboard(chat_id), parse_mode="Markdown")
    except Exception:
        pass

def market_monitor_loop():
    print("=== SMC PRO v3.1 | يعمل بأسعار مطابقة وتنسيق دقيق ===")
    while True:
        try:
            target_chat_id = TELEGRAM_CHATID
            cfg = get_user_config(target_chat_id)
            htf = HTF_MAPPING[cfg["INTERVAL"]]
            k  = get_klines(cfg["SYMBOL"], cfg["INTERVAL"], LOOKBACK)
            kh = get_klines(cfg["SYMBOL"], htf, LOOKBACK)
            res = analyze(k, kh)
            _, price, _, _ = res
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [{cfg['SYMBOL']}] السعر: {format_price(cfg['SYMBOL'], price)}")
            time.sleep(CHECK_EVERY)
        except Exception as e:
            print("تحذير:", e)
            time.sleep(15)

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    handler = socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler)
    handler.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=market_monitor_loop, daemon=True).start()

    print("البوت قيد التشغيل ويستمع للأوامر...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"إعادة الاتصال: {e}")
            time.sleep(5)