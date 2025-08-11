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

# DNS সমাধানের জন্য প্রয়োজনীয় লাইব্রেরি
try:
    import dns.asyncresolver
except ImportError:
    print("❌ [FATAL] dnspython লাইব্রেরি ইনস্টল করা নেই। দয়া করে `pip install dnspython` চালান।")
    exit()

from httpcore import Request, Response

# --- [মডিউল ১: চূড়ান্ত এবং নির্ভরযোগ্য কনফিগারেশন] ---
BOT_TOKEN_HARDCODED = "8328958637:AAEZ88XR-Ksov_RHDyT0_nKPgBEL1K876Y8"
CHANNEL_ID_HARDCODED = "-1002557789082"
BITLY_TOKEN_HARDCODED = "2feb4ec89bdbb72e24eaf85536d6149d948393cc"

BOT_TOKEN = os.environ.get("BOT_TOKEN", BOT_TOKEN_HARDCODED)
CHANNEL_ID = os.environ.get("CHANNEL_ID", CHANNEL_ID_HARDCODED)
BITLY_ACCESS_TOKEN = os.environ.get("BITLY_ACCESS_TOKEN", BITLY_TOKEN_HARDCODED)

DATABASE_FILE = "hybrid_news_database.db"

# --- [মডিউল ২: ডেটাবেস ম্যানেজমেন্ট] (অপরিবর্তিত) ---
def setup_database():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS posted_articles (unique_id TEXT PRIMARY KEY, source TEXT NOT NULL)')
    conn.commit()
    conn.close()

def is_article_posted(unique_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT unique_id FROM posted_articles WHERE unique_id = ?', (unique_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_article_to_db(unique_id, source):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO posted_articles (unique_id, source) VALUES (?, ?)', (unique_id, source))
    conn.commit()
    conn.close()

# --- [মডিউল ৩: পুরনো httpx-এর জন্য কাস্টম ডিএনএস সমাধানকারী] ---
class CustomDNSResolverTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: Request) -> Response:
        # request.url.host একটি বাইটস স্ট্রিং হতে পারে, তাই স্ট্রিং-এ রূপান্তর করা হচ্ছে
        try:
            hostname = request.url.host.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            hostname = request.url.host

        try:
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
            answers = await resolver.resolve(hostname)
            ip = answers[0].address
            
            # URL-এর host পরিবর্তন করে IP বসানো হচ্ছে
            request.url = request.url.copy_with(host=ip)
            # সার্ভারকে আসল ডোমেইন নেম জানানোর জন্য Host হেডার সেট করা হচ্ছে
            request.headers["Host"] = hostname
        except Exception as e:
            print(f"--> [DNS WARNING] হোস্টনেম '{hostname}' সমাধান করা যায়নি: {e}.")
            pass
        
        return await super().handle_async_request(request)

# --- [মডিউল ৪: নেটওয়ার্ক এবং ইউটিলিটি] ---
async def create_retry_client():
    """
    cPanel-এর পুরনো httpx সংস্করণের জন্য চূড়ান্ত ক্লায়েন্ট যা SSL ভেরিফিকেশন বাইপাস করে।
    """
    print("[INFO] কাস্টম DNS এবং SSL-Bypass ক্লায়েন্ট তৈরির চেষ্টা করা হচ্ছে...")
    try:
        # আমাদের কাস্টম ট্রান্সপোর্ট ব্যবহার করা হচ্ছে যা network আর্গুমেন্ট ছাড়া কাজ করে
        transport = CustomDNSResolverTransport(retries=2, verify=False)

        # ক্লায়েন্ট তৈরি করার সময়ও SSL ভেরিফিকেশন বন্ধ করা হচ্ছে
        client = httpx.AsyncClient(
            transport=transport,
            timeout=40,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        print("⚠️  [WARNING] ক্লায়েন্ট SSL ভেরিফিকেশন ছাড়া তৈরি হয়েছে।")
        print("✅ [SUCCESS] ক্লায়েন্ট সফলভাবে চূড়ান্ত কনফিগারেশন দিয়ে তৈরি হয়েছে।")
        return client
    except Exception as e:
        print(f"❌ [ERROR] কাস্টম ক্লায়েন্ট তৈরিতে একটি অপ্রত্যাশিত ত্রুটি ঘটেছে: {e}")
        return None

# --- [বাকি সমস্ত কোড অপরিবর্তিত] ---
async def fetch_api_data(session, url):
    try:
        response = await session.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [NETWORK ERROR] API থেকে ডেটা আনার সময় সমস্যা: {url} | এরর: {e}")
        return None

async def shorten_url(session, long_url):
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
    message = (f"<b>📢 নতুন সরকারি চাকরির বিজ্ঞপ্তি!</b>\n\n<b>🏢 প্রতিষ্ঠান:</b> {job_info.get('organization', 'N/A')}\n<b>📄 শিরোনাম:</b> {job_info.get('title', 'N/A')}\n<b>📅 আবেদনের শেষ তারিখ:</b> {job_info.get('end_date', 'N/A')}\n")
    details_url = await shorten_url(session, job_info.get('url', '#'))
    apply_url = await shorten_url(session, job_info.get('apply_url', '#'))
    keyboard = [[InlineKeyboardButton("📄 বিস্তারিত দেখুন", url=details_url)], [InlineKeyboardButton("✅ আবেদন করুন", url=apply_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
    return True

async def send_news_alert(bot: Bot, news_info: dict, session: httpx.AsyncClient):
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

def find_image_url_from_story(story_data):
    try:
        if key := story_data.get("metadata", {}).get("social-share", {}).get("image", {}).get("key"):
            return f"https://images.prothomalo.com/{key}"
        if "cards" in story_data:
            for card in story_data.get("cards", []):
                for element in card.get("story-elements", []):
                    if element.get("type") == "image" and element.get("image-s3-key"):
                        return f"https://images.prothomalo.com/{element['image-s3-key']}"
    except Exception as e:
        print(f"--> [WARN] ছবি পার্স করার সময় একটি অপ্রত্যাশিত ত্রুটি ঘটেছে: {e}")
    return None

async def check_teletalk_jobs(session, bot):
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
                photo_url = find_image_url_from_story(story_data)
                
                news_info = {"title": headline, "subheadline": subheadline, "url": f"https://www.prothomalo.com/{slug}", "photo_url": photo_url}
                if await send_news_alert(bot, news_info, session):
                    add_article_to_db(story_id, "prothomalo")
                    await asyncio.sleep(10)

async def main_loop():
    if not BOT_TOKEN or not CHANNEL_ID or "YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        print("❌ [FATAL] BOT_TOKEN or CHANNEL_ID is not set.")
        return

    bot = Bot(token=BOT_TOKEN)
    session = await create_retry_client()
    
    if session is None:
        print("❌ [FATAL] HTTP Client could not be created. Exiting.")
        return

    try:
        await bot.get_me()
        await bot.send_message(chat_id=CHANNEL_ID, text="✅ সমন্বিত নিউজ ও জব বুলেটিন বট সফলভাবে অনলাইন। (Build: cPanel-Final)")
        print("✅ [SUCCESS] বট সফলভাবে টেলিগ্রামের সাথে সংযোগ স্থাপন করেছে।")
    except Exception as e:
        print(f"❌ [STARTUP FAILED] Could not connect to Telegram. Error: {e}")
        return

    print("--- [INFO] Hybrid scheduler is now running. ---")

    while True:
        try:
            await check_teletalk_jobs(session, bot)
            await check_prothomalo_news(session, bot)
            check_interval_minutes = 5
            print(f"[{time.strftime('%H:%M:%S')}] [SLEEP] Waiting for {check_interval_minutes} minutes...")
            await asyncio.sleep(check_interval_minutes * 60)
        except Exception as e:
            print(f"❌ [MAIN LOOP ERROR] An unexpected error occurred: {e}")
            await asyncio.sleep(60)

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
