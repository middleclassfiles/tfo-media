import os
import sys
import time
import re
import json
import shutil
import datetime
import subprocess
import psycopg2
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MPT_DIR = os.environ.get("MPT_DIR", "mpt")
MPT_URL = "http://127.0.0.1:8080"
REPO_POSTS = "posts"
GH_OWNER = os.environ.get("GH_OWNER", "middleclassfiles")
GH_REPO = os.environ.get("GH_REPO", "tfo-media")
BIO_URL = "https://middleclassfiles.github.io/top-fashion-op/"
VOICE = "en-IN-NeerjaNeural"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def db():
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"],
        port=int(os.environ.get("SUPABASE_DB_PORT", "5432")),
        dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"),
        user=os.environ["SUPABASE_DB_USER"],
        password=os.environ["SUPABASE_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    )


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)


def tg_notify(chat_id, text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as e:
        log(f"tg notify failed: {e}")


def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        return r.text
    except Exception:
        return ""


def slug_title(url):
    m = re.search(r"/([^/]+)/p/[a-zA-Z0-9]+/?$", url)
    if m:
        name = re.sub(r"[-_]+", " ", m.group(1)).strip()
        return name.title() if name else ""
    return ""


def extract_meta(html):
    def og(name):
        m = re.search(r'<meta[^>]+property=["\']og:' + name + r'["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:' + name + r'["\']', html)
        return m.group(1) if m else ""
    title = og("title") or ""
    price = ""
    m = re.search(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        price = m.group(1)
    if not title:
        m = re.search(r"<title[^>]*>([^<]+)</title>", html)
        if m:
            title = m.group(1).strip()
    return title, price


def gen_script(title, price):
    prompt = (
        "You write short product promo voiceover scripts for Instagram Reels. "
        f"Product: {title}" + (f" Price: Rs {price}." if price else "") +
        " Write one script, max 70 words, friendly excited Indian English, "
        "strong hook first sentence, 2-3 benefits, one clear call to action to "
        "tap the link in bio. Output only the script text."
    )
    body = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def mpt_alive():
    try:
        requests.get(MPT_URL + "/docs", timeout=5)
        return True
    except Exception:
        return False


def start_mpt():
    if mpt_alive():
        return True
    log("starting video machine")
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=MPT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        time.sleep(2)
        if mpt_alive():
            return True
    return False


def make_video(subject):
    body = {
        "video_subject": subject[:100],
        "video_aspect": "9:16",
        "voice_name": VOICE,
        "subtitle_enabled": True,
    }
    r = requests.post(MPT_URL + "/api/v1/videos", json=body, timeout=30)
    r.raise_for_status()
    task_id = r.json()["data"]["task_id"]
    for _ in range(90):
        time.sleep(10)
        t = requests.get(MPT_URL + f"/api/v1/tasks/{task_id}", timeout=30).json()["data"]
        if t.get("state") == 1 and t.get("videos"):
            return t["videos"][0]
        if t.get("failed_stage") or t.get("error"):
            raise RuntimeError(f"video failed: {t.get('error')}")
    raise RuntimeError("video timed out")


def buffer_post(video_url, text, due_at):
    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }
    """
    is_now = due_at <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    variables = {
        "input": {
            "text": text,
            "channelId": os.environ["BUFFER_CHANNEL_ID"],
            "schedulingType": "automatic",
            "mode": "shareNow" if is_now else "customScheduled",
            "dueAt": None if is_now else due_at.isoformat(),
            "metadata": {"instagram": {"type": "reel", "isAiGenerated": True, "shouldShareToFeed": True}},
            "assets": [{"video": {"url": video_url, "metadata": {"thumbnailOffset": 2000}}}],
        }
    }
    r = requests.post(
        "https://api.buffer.com",
        headers={"Authorization": f"Bearer {os.environ['BUFFER_KEY']}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=60,
    )
    return r.json()


def buffer_channel_id():
    q = """
    query { account { organizations { id } } }
    """
    r = requests.post(
        "https://api.buffer.com",
        headers={"Authorization": f"Bearer {os.environ['BUFFER_KEY']}", "Content-Type": "application/json"},
        json={"query": q},
        timeout=30,
    )
    org = r.json()["data"]["account"]["organizations"][0]["id"]
    q2 = """
    query Channels($orgId: OrganizationId!) {
      channels(input: { organizationId: $orgId }) {
        id
        service
      }
    }
    """
    r = requests.post(
        "https://api.buffer.com",
        headers={"Authorization": f"Bearer {os.environ['BUFFER_KEY']}", "Content-Type": "application/json"},
        json={"query": q2, "variables": {"orgId": org}},
        timeout=30,
    )
    for ch in r.json()["data"]["channels"]:
        if ch["service"] == "instagram":
            return ch["id"]
    raise RuntimeError("no instagram channel in buffer")


def process_row(conn, row, channel_id):
    row_id = row[0]
    chat_id = row[1]
    link = row[2]
    due_at = row[3]
    log(f"processing row {row_id}: {link}")

    def fail(msg):
        with conn.cursor() as cur:
            cur.execute(
                "update tfo_scheduled_posts set status='failed', error=%s, updated_at=now() where id=%s",
                (msg[:500], row_id),
            )
        conn.commit()
        tg_notify(chat_id, f"\u274c Could not make this video: {msg}")

    try:
        html = fetch_page(link)
        title, price = extract_meta(html)
        if not title:
            title = slug_title(link)
        if not title:
            title = link[:60]
        log(f"title: {title}")
        script = gen_script(title, price)
        log(f"script: {script[:80]}")
        if not start_mpt():
            raise RuntimeError("video machine failed to start")
        video_rel = make_video(script)
        src = os.path.join(MPT_DIR, "storage", "tasks", video_rel.strip("/"))
        if not os.path.exists(src):
            raise RuntimeError(f"video file missing: {src}")
        dest_dir = os.path.join(REPO_POSTS, str(row_id))
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dest_dir, "video.mp4"))
        proc = subprocess.run(
            ["git", "add", REPO_POSTS], capture_output=True, text=True, timeout=60
        )
        proc = subprocess.run(
            [
                "git", "-c", "user.name=video-factory",
                "-c", "user.email=video-factory@users.noreply.github.com",
                "commit", "-m", f"video {row_id}",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 and "nothing to commit" not in proc.stderr:
            raise RuntimeError(f"git commit: {proc.stderr[-200:]}")
        proc = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"git push: {proc.stderr[-200:]}")
        video_url = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/posts/{row_id}/video.mp4"
        caption = f"{title}\n\nFound this gem and had to share \u2764\n\nShop it here \u2192 link in bio\n\n#fashion #ootd #styleinspo #affiliate #ad"
        res = buffer_post(video_url, caption, due_at)
        post_id = res.get("data", {}).get("createPost", {}).get("post", {}).get("id")
        if not post_id:
            raise RuntimeError(f"buffer: {res.get('data', {}).get('createPost', {}).get('message', str(res)[:200])}")
        with conn.cursor() as cur:
            cur.execute(
                "update tfo_scheduled_posts set status='done', buffer_post_id=%s, updated_at=now() where id=%s",
                (post_id, row_id),
            )
        conn.commit()
        tg_notify(
            chat_id,
            f"\u2705 <b>Video is scheduled!</b>\n\n{title}\n\n"
            f"It will go live on Instagram at your chosen time.\n"
            f"Manage your queue anytime: publish.buffer.com",
        )
        log(f"row {row_id} DONE (buffer {post_id})")
        return True
    except Exception as e:
        log(f"row {row_id} FAILED: {e}")
        fail(str(e))
        return False


def main():
    if not start_mpt():
        log("video machine not available - aborting")
        return
    try:
        channel_id = os.environ.get("BUFFER_CHANNEL_ID") or buffer_channel_id()
    except Exception as e:
        log(f"buffer channel lookup failed: {e}")
        return
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select id, chat_id, link, due_at from tfo_scheduled_posts "
                "where status='pending' and due_at <= now() + interval '10 minutes' "
                "order by due_at limit 2"
            )
            rows = cur.fetchall()
        if not rows:
            log("nothing due")
            return
        for row in rows:
            process_row(conn, row, channel_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
