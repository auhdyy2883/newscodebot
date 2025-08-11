import asyncio
import httpx
import time
import sqlite3
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from datetime import datetime
import pytz
import os
import json

# --- [মডিউল ১: চূড়ান্ত এবং নির্ভরযোগ্য কনফিগারেশন] ---

# ধাপ ১: প্রথমে সরাসরি কোড থেকে টোকেনগুলো লোড করার চেষ্টা করা হচ্ছে (cPanel-এর জন্য ফলব্যাক)
BOT_TOKEN_HARDCODED = "8328958637:AAEZ88XR-Ksov_RHDyT0_nKPgBEL1K876Y8"  # <--- আপনার বট টোকেন এখানে দিন
CHANNEL_ID_HARDCODED = "-1002557789082" # <--- আপনার চ্যানেল আইডি এখানে দিন
BITLY_TOKEN_HARDCODED = "2feb4ec89bdbb72e24eaf85536d6149d948393cc" # <--- আপনার Bitly টোকেন দিন (না থাকলে খালি রাখুন)

# ধাপ ২: এরপর Environment Variable থেকে লোড করার চেষ্টা করা হচ্ছে (Render/VPS-এর জন্য)
# যদি এনভায়রনমেন্ট ভেরিয়েবল সেট করা থাকে, তাহলে সেটিই ব্যবহৃত হবে।
BOT_TOKEN = os.environ.get("BOT_TOKEN", BOT_TOKEN_HARDCODED)
CHANNEL_ID = os.environ.get("CHANNEL_ID", CHANNEL_ID_HARDCODED)
BITLY_ACCESS_TOKEN = os.environ.get("BITLY_ACCESS_TOKEN", BITLY_TOKEN_HARDCODED)

DATABASE_FILE = "hybrid_news_database.db"

# --- [মডিউল ২: ডেটাবেস ম্যানেজমেন্ট] ---
def setup_database():
    """ডেটাবেস এবং টেবিল তৈরি করে।"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS posted_articles (unique_id TEXT PRIMARY KEY, source TEXT NOT NULL)')
    conn.commit()
    conn.close()

def is_article_posted(unique_id):
    """ডেটাবেসে আর্টিকেলটি আগে পোস্ট করা হয়েছে কি না, তা পরীক্ষা করে।"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT unique_id FROM posted_articles WHERE unique_id = ?', (unique_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_article_to_db(unique_id, source):
    """পোস্ট করা আর্টিকেলের আইডি ডেটাবেসে যোগ করে।"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO posted_articles (unique_id, source) VALUES (?, ?)', (unique_id, source))
    conn.commit()
    conn.close()

# --- [মডিউল ৩: নেটওয়ার্ক এবং ইউটিলিটি] ---
async def create_retry_client():
    """স্ট্যান্ডার্ড সার্ভার পরিবেশের জন্য সহজ এবং নির্ভরযোগ্য HTTP ক্লায়েন্ট তৈরি করে।"""
    transport = httpx.AsyncHTTPTransport(retries=3)
    client = httpx.AsyncClient(transport=transport, timeout=30)
    print("✅ [SUCCESS] স্ট্যান্ডার্ড ক্লায়েন্ট সফলভাবে তৈরি হয়েছে।")
    return client

async def fetch_api_data(session, url):
    """একটি URL থেকে JSON ডেটা আনে এবং এরর হ্যান্ডেল করে।"""
    try:
        response = await session.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        print(f"[{time.strftime('%H:%M:%S')}] [HTTP ERROR] API থেকে ডেটা আনার সময় সমস্যা: {url} | স্ট্যাটাস কোড: {e.response.status_code}")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [NETWORK ERROR] API থেকে ডেটা আনার সময় সমস্যা: {url} | এরর: {e}")
    return None

async def shorten_url(session, long_url):
    """Bitly ব্যবহার করে একটি URL ছোট করে।"""
    if not BITLY_ACCESS_TOKEN or BITLY_ACCESS_TOKEN == "YOUR_BITLY_ACCESS_TOKEN_HERE":
        return long_url
    bitly_api_url = "https://api-ssl.bitly.com/v4/shorten"
    headers = {"Authorization": f"Bearer {BITLY_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"long_url": long_url}
    try:
        response = await session.post(bitly_api_url, headers=headers, json=payload)
        return response.json().get("link", long_url)
    except Exception:
        return long_url

async def send_job_alert(bot: Bot, job_info: dict, session: httpx.AsyncClient):
    """চাকরির বিজ্ঞপ্তি ফরম্যাট করে টেলিগ্রাম চ্যানেলে পাঠায়।"""
    message = (f"<b>📢 নতুন সরকারি চাকরির বিজ্ঞপ্তি!</b>\n\n<b>🏢 প্রতিষ্ঠান:</b> {job_info.get('organization', 'N/A')}\n<b>📄 শিরোনাম:</b> {job_info.get('title', 'N/A')}\n<b>📅 আবেদনের শেষ তারিখ:</b> {job_info.get('end_date', 'N/A')}\n")
    details_url = await shorten_url(session, job_info.get('url', '#'))
    apply_url = await shorten_url(session, job_info.get('apply_url', '#'))
    keyboard = [[InlineKeyboardButton("📄 বিস্তারিত দেখুন", url=details_url)], [InlineKeyboardButton("✅ আবেদন করুন", url=apply_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
    return True

async def send_news_alert(bot: Bot, news_info: dict, session: httpx.AsyncClient):
    """সংবাদ ফরম্যাট করে ছবিসহ বা ছবি ছাড়া টেলিগ্রাম চ্যানেলে পাঠায়।"""
    headline = news_info.get('title', 'N/A')
    subheadline = news_info.get('subheadline') or ''
    message = f"<b>{headline}</b>\n\n{subheadline}"
    short_url = await shorten_url(session, news_info.get('url', '#'))
    keyboard = [[InlineKeyboardButton("📄 বিস্তারিত পড়ুন", url=short_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    photo_url = news_info.get('photo_url')
    try:
        if photo_url:
            print(f"--> [INFO] ছবিসহ পোস্ট করার চেষ্টা করা হচ্ছে। ছবির URL: {photo_url}")
            headers = {'Referer': 'https://www.prothomalo.com/'}
            photo_response = await session.get(photo_url, headers=headers)
            photo_response.raise_for_status()
            photo_bytes = photo_response.content
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_bytes, caption=message, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
        print(f"--> [SUCCESS] খবর '{headline}' সফলভাবে চ্যানেলে পোস্ট করা হয়েছে।")
        return True
    except Exception as e:
        print(f"❌❌ [SEND ERROR] মেসেজ পাঠানো সম্ভব হয়নি: {e}")
        try:
            print("--> [RETRY] ছবি ছাড়া শুধু টেক্সট পাঠানোর চেষ্টা করা হচ্ছে...")
            await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=False)
            print(f"--> [SUCCESS] খবর '{headline}' (শুধু টেক্সট) সফলভাবে পোস্ট করা হয়েছে।")
            return True
        except Exception as final_e:
            print(f"❌❌ [FATAL SEND] দ্বিতীয়বার চেষ্টাতেও মেসেজ পাঠানো সম্ভব হয়নি: {final_e}")
            return False

# --- [মডিউল ৫: মূল লজিক] ---
async def check_teletalk_jobs(session, bot):
    """Teletalk API থেকে নতুন চাকরির খবর চেক করে।"""
    print(f"[{time.strftime('%H:%M:%S')}] [CHECK] Teletalk থেকে চাকরির খবর চেক করা হচ্ছে...")
    url = "https://alljobs.teletalk.com.bd/api/v1/govt-jobs/list?skipLimit=YES"
    response = await fetch_api_data(session, url)
    if response and response.get("status") == "success":
        for job in reversed(response.get("data", [])):
            job_id = f"teletalk_{job.get('id')}"
            if job_id and not is_article_posted(job_id):
                print(f"--> [NEW JOB] নতুন চাকরি সনাক্ত করা হয়েছে: আইডি {job_id}")
                try: end_date = datetime.strptime(job.get("application_end_date"), "%Y-%m-%d").strftime("%d %B, %Y")
                except: end_date = "N/A"
                base_url = "https://alljobs.teletalk.com.bd"
                circular_path = job.get("circular_link")
                job_info = {"title": job.get("job_title"), "organization": job.get("organization"), "end_date": end_date, "url": f"{base_url}{circular_path}" if circular_path else base_url, "apply_url": f"{base_url}/jobs/government/{job.get('organization_slug', '')}/apply/{job.get('id')}"}
                if await send_job_alert(bot, job_info, session):
                    add_article_to_db(job_id, "teletalk")
                    await asyncio.sleep(5)

async def check_prothomalo_news(session, bot):
    """Prothom Alo API থেকে নতুন সংবাদ চেক করে।"""
    print(f"[{time.strftime('%H:%M:%S')}] [CHECK] Prothom Alo থেকে সর্বশেষ খবর চেক করা হচ্ছে...")
    url = "https://www.prothomalo.com/api/v1/collections/latest?limit=15&item-type=story&fields=id,headline,slug,url,subheadline,cards,metadata"
    response = await fetch_api_data(session, url)
    if response and response.get("items"):
        for story_wrapper in reversed(response["items"]):
            story_data = story_wrapper.get("story", {})
            story_id = f"palo_{story_wrapper.get('id')}"
            headline = story_data.get("headline")
            slug = story_data.get("slug")
            if story_id and headline and not is_article_posted(story_id):
                print(f"--> [NEW POST] নতুন খবর সনাক্ত করা হয়েছে: {headline}")
                subheadline = story_data.get("subheadline")
                photo_url = None
                try:
                    if key := story_data.get("metadata", {}).get("social-share", {}).get("image", {}).get("key"):
                        photo_url = f"https://images.prothomalo.com/{key}"
                except Exception as e: print(f"--> [WARN] ছবি পার্স করার সময় এরর: {e}")
                news_info = {"title": headline, "subheadline": subheadline, "url": f"https://www.prothomalo.com/{slug}", "photo_url": photo_url}
                if await send_news_alert(bot, news_info, session):
                    add_article_to_db(story_id, "prothomalo")
                    await asyncio.sleep(10)

async def main_loop():
    """বটের মূল লুপ, যা নির্দিষ্ট সময় পর পর জব এবং নিউজ চেক করে।"""
    if not BOT_TOKEN or not CHANNEL_ID:
        # বাংলা টেক্সট বাদ দিয়ে ইংরেজি ব্যবহার করা হচ্ছে এনকোডিং সমস্যা এড়ানোর জন্য
        print("❌ [FATAL] BOT_TOKEN or CHANNEL_ID is not set. Please check your configuration.")
        return

    bot = Bot(token=BOT_TOKEN)
    session = await create_retry_client()
    
    try:
        await bot.get_me()
        await bot.send_message(chat_id=CHANNEL_ID, text="✅ সমন্বিত নিউজ ও জব বুলেটিন বট সফলভাবে অনলাইন।")
        print("✅ [SUCCESS] বট সফলভাবে টেলিগ্রামের সাথে সংযোগ স্থাপন করেছে।")
    except Exception as e:
        print(f"❌ [STARTUP FAILED] Could not connect to Telegram. Check BOT_TOKEN and CHANNEL_ID. Error: {e}")
        return

    print("--- [INFO] Hybrid scheduler is now running. ---")

    while True:
        try:
            await check_teletalk_jobs(session, bot)
            await check_prothomalo_news(session, bot)
            check_interval_minutes = 5
            print(f"[{time.strftime('%H:%M:%S')}] [SLEEP] Waiting for {check_interval_minutes} minutes until the next check...")
            await asyncio.sleep(check_interval_minutes * 60)
        except Exception as e:
            print(f"❌ [MAIN LOOP ERROR] An unexpected error occurred: {e}")
            await asyncio.sleep(60)

# --- [মডিউল ৬: প্রোগ্রাম শুরু] ---
if __name__ == '__main__':
    print("--- [INFO] Initializing database... ---")
    setup_database()
    
    print("--- [INFO] Starting bot... ---")
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n--- [INFO] Bot is shutting down. ---")
    except Exception as e:
        print(f"❌ [CRITICAL] A fatal error occurred while starting the program: {e}")
