import os
import sys
import re
import json
import time
import base64
import shutil
import asyncio
import datetime
import subprocess
import psycopg2
import requests

try:
    sys_stdout = sys.stdout
    sys_stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GH_OWNER = os.environ.get("GH_OWNER", "middleclassfiles")
GH_REPO = os.environ.get("GH_REPO", "tfo-media")
REPO_POSTS = "posts"
REPO_MODEL = "model"
BIO_URL = "https://middleclassfiles.github.io/top-fashion-op/"
VOICE = "en-IN-NeerjaNeural"
IDENTITY_URL = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/{REPO_MODEL}/identity.png"
IDENTITY_DESC = (
    "a beautiful 22 year old Indian woman, long straight black hair, glowing skin, "
    "warm confident smile, fashion influencer"
)
SEED = 777

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def db():
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"],
        hostaddr=os.environ.get("SUPABASE_DB_HOSTADDR", ""),
        port=int(os.environ.get("SUPABASE_DB_PORT", "6543")),
        dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"),
        user=os.environ["SUPABASE_DB_USER"],
        password=os.environ["SUPABASE_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    )


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


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


def slug_title(url):
    m = re.search(r"/([^/]+)/p/[a-zA-Z0-9]+/?$", url)
    if m:
        name = re.sub(r"[-_]+", " ", m.group(1)).strip()
        return name.title() if name else ""
    return ""


def gen_script(title, price):
    prompt = (
        "Write a 'Get Ready With Me' style voiceover script for an Instagram Reel, "
        "spoken by a young Indian woman who just received a new outfit she ordered. "
        f"Product: {title}" + (f" Price: Rs {price}." if price else "") +
        " Requirements: casual, excited, first-person like a girl talking to her "
        "friends while getting ready; start with a strong hook like 'Guys, today I "
        "ordered this and I am OBSESSED!'; mention how the fabric feels, how it fits, "
        "and how pretty it looks on her; max 80 words; end the script EXACTLY with "
        "this sentence: 'Comment LINK and I will send you the link of this dress.' "
        "Output only the script text."
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


async def synth_audio(text, out_path):
    import edge_tts
    communicate = edge_tts.Communicate(text, VOICE, rate="+5%")
    await communicate.save(out_path)


def gen_audio(text, out_path):
    asyncio.run(synth_audio(text, out_path))


def ai_image(prompt, out_path, image_url=None, seed=SEED, retries=3):
    url = (
        f"https://image.pollinations.ai/prompt/{prompt}"
        f"?width=720&height=1280&seed={seed}&model=flux&nologo=true"
    )
    if image_url:
        url += f"&image={requests.utils.quote(image_url, safe='')}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=240)
            if r.status_code == 200 and r.content[:2] != b"\x00":
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return True
            log(f"ai_image attempt {attempt+1}: HTTP {r.status_code}")
        except Exception as e:
            log(f"ai_image attempt {attempt+1}: {e}")
        time.sleep(5)
    return False


def scene(ffmpeg, img, out_mp4, dur, zoom_from, zoom_to, x="(iw-iw/zoom)/2", y="(ih-ih/zoom)/2"):
    fps = 25
    frames = int(dur * fps)
    z = f"max(1.0, {zoom_from}+({zoom_to}-{zoom_from})*on/{frames})"
    vf = (
        f"scale=1620:2880:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,"
        f"zoompan=z='{z}':d={fps}:x='{x}':y='{y}':s=1080x1920:fps={fps},"
        f"format=yuv420p"
    )
    subprocess.run(
        [ffmpeg, "-y", "-loop", "1", "-i", img, "-vf", vf,
         "-t", str(dur), "-r", "25", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-an", out_mp4],
        check=True, capture_output=True, text=True,
    )


def end_card(ffmpeg, img, out_mp4, dur, text):
    vf = (
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"eq=brightness=0.12:contrast=1.05,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{text}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=h*0.72:"
        f"shadowcolor=black:shadowx=3:shadowy=3,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"text='{BIO_URL}':fontcolor=white@0.9:fontsize=38:x=(w-text_w)/2:y=h*0.82:"
        f"shadowcolor=black:shadowx=2:shadowy=2,"
        f"format=yuv420p"
    )
    subprocess.run(
        [ffmpeg, "-y", "-loop", "1", "-i", img, "-vf", vf,
         "-t", str(dur), "-r", "25", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-an", out_mp4],
        check=True, capture_output=True, text=True,
    )


def make_srt(script, audio_dur, out_path):
    sentences = re.split(r"(?<=[.!?])\s+", script.strip())
    sentences = [s for s in sentences if s]
    total_chars = sum(len(s) for s in sentences) or 1
    srt = []
    t = 0.0
    for i, s in enumerate(sentences):
        dur = max(2.0, audio_dur * len(s) / total_chars)
        start = t
        end = min(t + dur, audio_dur)
        srt.append(f"{i+1}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{s}\n")
        t = end
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt))


def fmt_ts(sec):
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_video(workdir, title, script, dress_urls, identity_url):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    audio = os.path.join(workdir, "audio.mp3")
    gen_audio(script, audio)
    probe = subprocess.run(
        [ffmpeg, "-i", audio], capture_output=True, text=True
    )
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
    if not m:
        raise RuntimeError("could not read audio duration")
    audio_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    dress_hint = re.sub(r"[^a-zA-Z ]", "", title).strip()[:60] or "kurti"
    n_scenes = max(1, min(4, len(dress_urls)))
    scene_imgs = []
    for i in range(n_scenes):
        out_img = os.path.join(workdir, f"model_wearing_{i}.png")
        ok = ai_image(
            IDENTITY_DESC + f", now wearing this {dress_hint}, showing it off "
            f"like a model on a fashion shoot, full body, {i}-th different pose, "
            "minimal beige studio background",
            out_img,
            image_url=identity_url,
            seed=SEED + i,
        )
        if not ok:
            raise RuntimeError(f"ai_image scene {i} failed")
        scene_imgs.append(out_img)

    segments = []
    dur_each = max(3.0, (audio_dur + 1.5) / (n_scenes * 2 + 1))
    for i in range(n_scenes):
        seg_dress = os.path.join(workdir, f"dress_seg_{i}.mp4")
        dress_img = os.path.join(workdir, f"dress_{i}.jpg")
        r = requests.get(dress_urls[i], headers=HEADERS, timeout=60)
        r.raise_for_status()
        with open(dress_img, "wb") as f:
            f.write(r.content)
        scene(ffmpeg, dress_img, seg_dress, dur_each, 1.0, 1.08)
        segments.append(seg_dress)

        seg_model = os.path.join(workdir, f"model_seg_{i}.mp4")
        if i % 2 == 0:
            scene(ffmpeg, scene_imgs[i], seg_model, dur_each, 1.0, 1.10)
        else:
            scene(ffmpeg, scene_imgs[i], seg_model, dur_each, 1.10, 1.0)
        segments.append(seg_model)

    card = os.path.join(workdir, "card.png")
    shutil.copy2(scene_imgs[0], card)
    seg_card = os.path.join(workdir, "seg_card.mp4")
    end_card(ffmpeg, card, seg_card, 4.0, "Comment LINK to get the dress link")
    segments.append(seg_card)

    with open(os.path.join(workdir, "list.txt"), "w") as f:
        for s in segments:
            f.write(f"file '{os.path.abspath(s)}'\n")
    silent = os.path.join(workdir, "silent.mp4")
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.5",
         "-c:a", "aac", silent],
        check=True, capture_output=True, text=True,
    )
    concat = os.path.join(workdir, "concat.mp4")
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(workdir, "list.txt"),
         "-i", silent, "-map", "0:v", "-map", "1:a", "-c", "copy", concat],
        check=True, capture_output=True, text=True,
    )

    srt = os.path.join(workdir, "subs.srt")
    make_srt(script, audio_dur, srt)
    video_out = os.path.join(workdir, "video.mp4")
    subprocess.run(
        [ffmpeg, "-y", "-i", concat, "-i", audio, "-vf",
         f"subtitles={srt}:force_style='FontName=DejaVu Sans,FontSize=14,"
         f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,"
         f"Shadow=1,MarginV=40,Alignment=2'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "192k", "-shortest", video_out],
        check=True, capture_output=True, text=True,
    )
    return video_out


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
    dress_images = row[4] if len(row) > 4 and row[4] else None
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
        title = slug_title(link) or link[:60]
        log(f"title: {title}")
        script = gen_script(title, "")
        log(f"script: {script[:100]}")

        workdir = os.path.join(REPO_POSTS, str(row_id))
        os.makedirs(workdir, exist_ok=True)

        identity_url = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/{REPO_MODEL}/identity.png"
        with conn.cursor() as cur:
            cur.execute("select value from tfo_settings where key='model_photo'")
            mp = cur.fetchone()
        if mp and mp[0]:
            raw = base64.b64decode(mp[0])
            with open(os.path.join(workdir, "identity.jpg"), "wb") as f:
                f.write(raw)
            proc = subprocess.run(
                ["git", "add", REPO_POSTS], capture_output=True, text=True, timeout=60
            )
            proc = subprocess.run(
                [
                    "git", "-c", "user.name=video-factory",
                    "-c", "user.email=video-factory@users.noreply.github.com",
                    "commit", "-m", f"model {row_id}",
                ],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0 and "nothing to commit" not in proc.stderr:
                raise RuntimeError(f"git commit: {proc.stderr[-200:]}")
            proc = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"git push: {proc.stderr[-200:]}")
            identity_url = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/posts/{row_id}/identity.jpg"

        images = []
        if dress_images:
            try:
                images = json.loads(dress_images)
            except Exception:
                images = []
        if not images:
            raise RuntimeError("no dress photos for this post - send photos to the bot")

        dress_urls = []
        for i, b64 in enumerate(images[:4]):
            raw = base64.b64decode(b64)
            ext = "jpg"
            if raw[:3] == b"\x89PN":
                ext = "png"
            elif raw[:2] == b"BM":
                ext = "bmp"
            elif raw[:4] == b"RIFF":
                ext = "webp"
            elif raw[:2] in (b"\xff\xd8",):
                ext = "jpg"
            name = f"dress_{i}.{ext}"
            with open(os.path.join(workdir, name), "wb") as f:
                f.write(raw)
            dress_urls.append(
                f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/posts/{row_id}/{name}"
            )

        proc = subprocess.run(
            ["git", "add", REPO_POSTS], capture_output=True, text=True, timeout=60
        )
        proc = subprocess.run(
            [
                "git", "-c", "user.name=video-factory",
                "-c", "user.email=video-factory@users.noreply.github.com",
                "commit", "-m", f"assets {row_id}",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 and "nothing to commit" not in proc.stderr:
            raise RuntimeError(f"git commit: {proc.stderr[-200:]}")
        proc = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"git push: {proc.stderr[-200:]}")

        identity_url = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/{REPO_MODEL}/identity.png"
        video = build_video(workdir, title, script, dress_urls, identity_url)

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
        caption = (
            f"{title}\n\n\U0001F48C Just got this in today and I am OBSESSED \U0001F970\n\n"
            f"\U0001F4AC Comment LINK and I will send you the link of this dress\n\n"
            f"#fashion #ootd #grwm #styleinspo #affiliate #ad"
        )
        res = buffer_post(video_url, caption, due_at)
        post_id = res.get("data", {}).get("createPost", {}).get("post", {}).get("id")
        if not post_id:
            raise RuntimeError(f"buffer: {str(res)[:200]}")
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
    try:
        channel_id = os.environ.get("BUFFER_CHANNEL_ID") or buffer_channel_id()
    except Exception as e:
        log(f"buffer channel lookup failed: {e}")
        return
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select id, chat_id, link, due_at, dress_images from tfo_scheduled_posts "
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
