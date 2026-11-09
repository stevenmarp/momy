import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from momy import cli

PORT = int(os.environ.get("MOMY_UI_PORT", "8765"))
WORK = Path(os.environ.get("MOMY_DIR") or Path.home() / "MoMy")
FONT = Path(__file__).with_name("BricolageGrotesque.woff2")
MEDIA = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".opus", ".mkv", ".mov", ".aac", ".flac"}
LABEL = re.compile(r"^(\*\*\[[\d:]+\] )(.+?)(:\*\*)", re.M)
LINE = re.compile(r"^\*\*\[([\d:]+)\] (.+?):\*\* ?(.*)$")
NAME_OK = re.compile(r"[^\n*:\[\]|]{1,40}")

lock = threading.Lock()
status = {"running": False}
job = None


def seconds(clock):
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 3600 + m * 60 + s


def set_step(st, step, percent=None, detail=""):
    if st.get("step") != step:
        st["step_started"] = time.time()
    st.update(step=step, percent=percent, detail=detail)


def progress(line, st):
    """Turn one log line of momy.cli into the progress shown in the page."""
    line = line.strip()
    if not line:
        return
    if line.startswith("Reading "):
        set_step(st, 1)
    elif line.startswith("Downloading "):
        st["detail"] = "Mengunduh model, hanya sekali"
    elif m := re.match(r"Finding speakers (\d+)%", line):
        set_step(st, 2, int(m[1]))
    elif m := re.match(r"Transcribing ([\d:]+) / ([\d:]+)", line):
        done, total = seconds(m[1]), max(seconds(m[2]), 1)
        set_step(st, 3, min(100, round(100 * done / total)), f"{m[1]} dari {m[2]}")
    elif line.startswith("Transcript: "):
        set_step(st, 4, None, "Menunggu model AI")
    elif m := re.match(r"Summarizing part (\d+)/(\d+)", line):
        set_step(st, 4, round(100 * (int(m[1]) - 1) / int(m[2])), f"Bagian {m[1]} dari {m[2]}")
    elif not line.startswith(("Summary: ", "Retrying", "Warning")):
        st["last"] = line


def friendly(error):
    if "Cannot reach the LLM" in error:
        return ("Model AI untuk menulis notulen tidak bisa dihubungi. Jalankan Ollama atau cek Pengaturan AI "
                "(kanan atas), lalu klik Buat ulang notulen. Transkripnya sudah tersimpan.")
    if re.search(r"LLM error 40[13]", error):
        return "Layanan AI menolak API key-nya (salah, kedaluwarsa, atau kena limit). Cek Pengaturan AI, lalu Buat ulang notulen."
    if "LLM error 404" in error:
        return "Nama model tidak ditemukan di layanan AI. Cek nama model di Pengaturan AI (untuk Ollama: jalankan ollama pull <model>)."
    if "No speech found" in error:
        return "Tidak ada suara percakapan di rekaman ini."
    return error


def config_view():
    current = cli.settings()
    return {"base_url": current["base_url"], "model": current["model"], "has_key": bool(current["api_key"]),
            "ollama": cli.is_local(current["base_url"]) and urlparse(current["base_url"]).port == 11434,
            "host": urlparse(current["base_url"]).netloc,
            "locked": [k for k in cli.DEFAULTS if os.environ.get(f"MOMY_{k.upper()}")]}


def check_llm(base_url, api_key, model):
    request = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {api_key or 'none'}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            ids = [m.get("id") for m in json.load(response).get("data", [])]
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": "API key ditolak." if e.code in (401, 403) else f"Layanan menjawab error {e.code}."}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "message": f"Tidak bisa terhubung ke {base_url} ({getattr(e, 'reason', e)})."}
    if ids and model not in ids and f"models/{model}" not in ids:
        names = [i.removeprefix("models/") for i in ids[:8]]
        return {"ok": False, "message": f"Terhubung, tapi model “{model}” tidak ada. Tersedia: {', '.join(names)}"}
    return {"ok": True, "message": "Terhubung. Model siap dipakai."}


def meeting_name(path):
    name = path.name
    return name.split(".transcript.")[0] if ".transcript." in name else path.stem


def start(path, speakers=None):
    global job
    cmd = [sys.executable, "-m", "momy.cli", str(path), "-o", str(WORK)]
    if speakers:
        cmd += ["--speakers", str(speakers)]
    status.clear()
    status.update(running=True, name=meeting_name(path), started=time.time(), last="", error=None, done=False)
    set_step(status, 4 if path.suffix == ".md" else 1, None, "Menunggu model AI" if path.suffix == ".md" else "")
    job = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=watch, args=(job,), daemon=True).start()


def watch(proc):
    rest = b""
    for chunk in iter(lambda: proc.stderr.read1(4096), b""):
        *lines, rest = re.split(rb"[\r\n]", rest + chunk)
        with lock:
            for line in lines:
                progress(line.decode(errors="replace"), status)
    code = proc.wait()
    with lock:
        progress(rest.decode(errors="replace"), status)
        status["running"] = False
        if status.get("cancelled"):
            return
        if code == 0:
            status["done"] = True
        else:
            status["error"] = friendly(status.get("last") or f"Proses berhenti (kode {code}).")


def speakers(transcript):
    talk, quote = {}, {}
    for line in transcript.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        label, text = m[2], m[3]
        talk[label] = talk.get(label, 0) + len(text)
        if len(quote.get(label, "")) < 60 and len(text) > len(quote.get(label, "")):
            quote[label] = text if len(text) <= 160 else text[:157].rsplit(" ", 1)[0] + "…"
    total = sum(talk.values()) or 1
    return [{"label": label, "share": round(100 * chars / total), "quote": quote[label]}
            for label, chars in sorted(talk.items(), key=lambda kv: -kv[1])]


def rename(transcript, mapping):
    return LABEL.sub(lambda m: m[1] + mapping.get(m[2], m[2]) + m[3], transcript)


def meetings():
    found = {}
    for path in WORK.glob("*.md"):
        for kind in ("summary", "transcript"):
            if path.name.endswith(f".{kind}.md"):
                name = path.name[: -len(f".{kind}.md")]
                item = found.setdefault(name, {"name": name, "summary": False, "transcript": False, "time": 0})
                item[kind] = True
                item["time"] = max(item["time"], path.stat().st_mtime)
    return sorted(found.values(), key=lambda m: -m["time"])


def read(name, kind):
    path = WORK / f"{Path(name).name}.{kind}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def unique(path):
    n = 2
    while path.exists() or path.with_suffix(".transcript.md").exists():
        path = path.with_name(f"{path.stem.rsplit(' (', 1)[0]} ({n}){path.suffix}")
        n += 1
    return path


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Only this page may talk to the server: blocks DNS rebinding and cross-site posts.
        host = self.headers.get("Host", "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and (
            self.command == "GET" or self.headers.get("X-MoMy") == "1"
        )

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {"error": "forbidden"})
        url = urlparse(self.path)
        name = parse_qs(url.query).get("name", [""])[0]
        if url.path == "/":
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/font.woff2" and FONT.is_file():
            return self.reply(200, FONT.read_bytes(), "font/woff2")
        if url.path == "/api/status":
            with lock:
                st = dict(status)
            st["now"] = time.time()
            return self.reply(200, st)
        if url.path == "/api/config":
            return self.reply(200, config_view())
        if url.path == "/api/meetings":
            return self.reply(200, meetings())
        if url.path == "/api/meeting":
            transcript = read(name, "transcript")
            summary = read(name, "summary")
            if transcript is None and summary is None:
                return self.reply(404, {"error": "Rapat tidak ditemukan."})
            return self.reply(200, {"name": Path(name).name, "summary": summary, "transcript": transcript,
                                    "speakers": speakers(transcript or "")})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        global job
        if not self.trusted():
            return self.reply(403, {"error": "forbidden"})
        url = urlparse(self.path)
        query = parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/cancel":
            with lock:
                if status.get("running") and job:
                    status["cancelled"] = True
                    job.terminate()
            return self.reply(200, {"ok": True})
        if url.path in ("/api/config", "/api/config/test"):
            body = json.loads(self.rfile.read(length) or b"{}")
            current = cli.settings()
            base_url = str(body.get("base_url", "")).strip()
            model = str(body.get("model", "")).strip()
            api_key = str(body.get("api_key", "")).strip() or current["api_key"]
            if not re.fullmatch(r"https?://\S+", base_url) or not 0 < len(model) <= 100:
                return self.reply(400, {"error": "Isi alamat API (http:// atau https://) dan nama model."})
            if url.path == "/api/config/test":
                return self.reply(200, check_llm(base_url, api_key, model))
            cli.save_settings({"base_url": base_url, "model": model, "api_key": "" if body.get("clear_key") else api_key})
            return self.reply(200, config_view())
        with lock:
            if status.get("running"):
                return self.reply(409, {"error": "Masih ada rapat yang sedang diproses."})
        if url.path == "/api/upload":
            raw = Path(unquote(query.get("filename", [""])[0])).name
            clean = re.sub(r"[^\w\-. ()]", "_", raw).strip(" .")
            if Path(clean).suffix.lower() not in MEDIA:
                return self.reply(400, {"error": "Format file tidak didukung. Pakai mp4, m4a, mp3, wav, webm, atau ogg."})
            speakers_n = query.get("speakers", [""])[0]
            speakers_n = int(speakers_n) if speakers_n.isdigit() and 1 <= int(speakers_n) <= 30 else None
            WORK.mkdir(parents=True, exist_ok=True)
            target = unique(WORK / clean)
            part = target.with_name(target.name + ".part")
            with open(part, "wb") as out:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            if left:
                part.unlink(missing_ok=True)
                return self.reply(400, {"error": "Unggahan terputus. Coba lagi."})
            part.rename(target)
            with lock:
                start(target, speakers_n)
            return self.reply(200, {"name": meeting_name(target)})
        body = json.loads(self.rfile.read(length) or b"{}")
        name = Path(str(body.get("name", ""))).name
        path = WORK / f"{name}.transcript.md"
        if not path.is_file():
            return self.reply(404, {"error": "Transkrip rapat ini tidak ada."})
        if url.path == "/api/rename":
            mapping = {}
            for old, new in (body.get("mapping") or {}).items():
                new = str(new).strip()
                if new and new != old:
                    if not NAME_OK.fullmatch(new):
                        return self.reply(400, {"error": f"Nama “{new}” tidak boleh memuat * : [ ] | dan maksimal 40 huruf."})
                    mapping[str(old)] = new
            path.write_text(rename(path.read_text(encoding="utf-8"), mapping), encoding="utf-8")
        elif url.path != "/api/summarize":
            return self.reply(404, {"error": "not found"})
        with lock:
            start(path)
        return self.reply(200, {"name": name})

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoMy · Minutes of Meeting, yours</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='14' fill='%23FF6B6B' stroke='%23141414' stroke-width='2.5'/%3E%3Ccircle cx='11' cy='11' r='3' fill='%23fff'/%3E%3C/svg%3E">
<style>
@font-face { font-family: "Bricolage"; src: url(/font.woff2) format("woff2"); font-weight: 400 800; font-display: swap; }
:root {
  color-scheme: dark;
  --sun: #141414; --ink: #fff6d6; --paper: #1f1e1b; --cream: #2b2924; --muted: #c9c1aa; --muted-sun: #c9c1aa; --chip-fg: #141414;
  --coral: #ff6b6b; --teal: #1fb5a5; --orange: #ffa62b; --violet: #7b68ee; --pink: #ff8fc7; --green: #5cc98a; --blue: #4d8dff;
  --danger: #ff8a7a; --danger-soft: #3a1d18; --line: 2px solid var(--ink);
  --display: "Bricolage", system-ui, sans-serif; --s1: 4px; --s2: 8px; --s3: 16px; --s4: 24px; --s5: 32px; --s6: 56px;
}
* { box-sizing: border-box; }
html { background: var(--sun); }
body { margin: 0; color: var(--ink); background: var(--sun); font: 16px/1.55 system-ui, "Ubuntu", "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 920px; margin: 0 auto; padding: 0 var(--s3); }
h1, h2, h3, .display { font-family: var(--display); letter-spacing: -0.02em; }
p { margin: 0 0 var(--s3); }
:focus-visible { outline: 3px solid var(--ink); outline-offset: 3px; }
svg.i { width: 20px; height: 20px; flex: none; stroke: currentColor; fill: none; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
[hidden] { display: none !important; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* balls */
.ball { --c: var(--coral); display: inline-block; width: 1em; height: 1em; border-radius: 50%; flex: none;
  background: radial-gradient(circle at 30% 28%, #fff 0 13%, transparent 14%),
              radial-gradient(circle at 38% 34%, var(--c) 0 58%, color-mix(in srgb, var(--c) 78%, #000) 100%); }
.ball.out { box-shadow: 0 0 0 2px var(--ink); }

/* header */
.bar { display: flex; align-items: center; justify-content: space-between; gap: var(--s3); padding: var(--s4) 0 var(--s3); flex-wrap: wrap; }
.bar .actions { margin-left: auto; }
dialog { border: var(--line); border-radius: 24px; background: var(--paper); color: var(--ink); width: min(560px, calc(100% - 32px)); padding: var(--s5); }
dialog::backdrop { background: rgba(0, 0, 0, .65); }
dialog h2 { font-size: 28px; font-weight: 800; margin: 0 0 var(--s2); }
dialog .field { max-width: none; margin: var(--s3) 0; }
dialog .field input:disabled { opacity: .6; }
dialog a { color: var(--ink); font-weight: 700; }
#geminiHelp { margin: var(--s3) 0 0; }
#setMsg.ok { color: var(--green); font-weight: 700; } #setMsg.bad { color: var(--danger); font-weight: 700; }
.mark { font: 800 34px/1 var(--display); letter-spacing: -0.05em; background: none; border: 0; color: var(--ink); cursor: pointer; padding: var(--s1) 0; display: inline-flex; align-items: baseline; }
.mark .ball { width: .54em; height: .54em; margin: 0 .03em; }
.hero { padding: var(--s6) 0 var(--s5); }
.hero h1 { font-size: clamp(76px, 21vw, 184px); font-weight: 800; line-height: .82; letter-spacing: -0.06em; margin: 0 0 var(--s4); display: flex; align-items: baseline; }
.hero h1 .ball { width: .55em; height: .55em; margin: 0 .035em; }
.hero .tag { font: 700 clamp(20px, 3.2vw, 28px)/1.25 var(--display); max-width: 24ch; margin: var(--s4) 0 var(--s2); }
.hero .sub { color: var(--muted-sun); font-weight: 600; }

/* cards & buttons */
.card { background: var(--paper); border: var(--line); border-radius: 24px; padding: var(--s5); margin-bottom: var(--s4); }
.card h2 { font-size: 26px; font-weight: 800; margin: 0 0 var(--s3); line-height: 1.1; }
.btn { font: 700 16px/1 system-ui, sans-serif; min-height: 48px; padding: 0 22px; border-radius: 999px; border: var(--line);
  background: var(--paper); color: var(--ink); cursor: pointer; display: inline-flex; align-items: center; gap: var(--s2);
  transition: transform .15s ease, background-color .15s ease; text-decoration: none; }
.btn:hover { background: var(--cream); }
.btn:active { transform: translateY(1px); }
.btn.primary { background: var(--ink); color: var(--chip-fg, var(--sun)); }
.btn.primary:hover { transform: translateY(-2px); background: var(--ink); }
.btn.danger { color: var(--danger); border-color: var(--danger); }
.btn.danger:hover { background: var(--danger-soft); }
.btn:disabled { opacity: .6; cursor: not-allowed; transform: none; }
.link { font: inherit; font-weight: 700; color: var(--ink); background: none; border: 0; padding: 0; min-height: 44px; cursor: pointer; text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 4px; }
.chip { display: inline-block; font-weight: 800; font-size: .9rem; padding: 3px 12px; border-radius: 999px; background: var(--ink); color: var(--chip-fg, var(--sun)); white-space: nowrap; font-variant-numeric: tabular-nums; }
.chip.warn { background: var(--danger-soft); color: var(--danger); }
.muted { color: var(--muted); }
.error { color: var(--danger); background: var(--danger-soft); border-radius: 16px; padding: var(--s3); margin: var(--s3) 0 0; font-weight: 600; }

/* upload */
.drop { border: 3px dashed var(--ink); border-radius: 20px; padding: var(--s5) var(--s3); text-align: center; transition: background-color .15s ease; }
.drop.over { background: var(--cream); }
.drop .balls { font-size: 26px; display: inline-flex; gap: 6px; margin-bottom: var(--s3); }
.drop strong { display: block; font: 800 22px/1.2 var(--display); margin-bottom: var(--s1); }
.chosen { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; padding: var(--s3); border-radius: 16px; background: var(--cream); border: var(--line); }
.chosen .file { flex: 1 1 240px; min-width: 0; overflow-wrap: anywhere; }
.field { display: grid; gap: var(--s1); margin: var(--s4) 0; max-width: 380px; }
.field label { font-weight: 700; }
.field input { font: inherit; min-height: 48px; padding: 0 var(--s3); border: var(--line); border-radius: 14px; background: var(--paper); color: var(--ink); }
.field small { color: var(--muted); }

/* past meetings */
.list { list-style: none; margin: 0; padding: 0; }
.list li + li { border-top: 2px dashed color-mix(in srgb, var(--ink) 25%, transparent); }
.list button { width: 100%; border: 0; background: none; color: var(--ink); display: flex; align-items: center; gap: var(--s3); justify-content: space-between; padding: var(--s3) var(--s2); min-height: 64px; text-align: left; cursor: pointer; border-radius: 14px; font: inherit; }
.list button:hover { background: var(--cream); }
.list .title { font: 800 19px/1.2 var(--display); overflow-wrap: anywhere; }
.list .meta { color: var(--muted); font-size: .9rem; }

/* progress */
.steps { list-style: none; margin: 0 0 var(--s4); padding: 0; display: grid; gap: var(--s4); }
.steps li { display: grid; grid-template-columns: 40px 1fr; gap: var(--s3); align-items: start; }
.sball { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; font-weight: 800; border: var(--line); background: var(--paper); color: var(--ink); }
.steps li.done .sball { border: 0; font-size: 40px; }
.steps li.done .sball .ball { box-shadow: 0 0 0 2px var(--ink); }
.steps li.active .sball { border: 9px solid var(--c); box-shadow: 0 0 0 2px var(--ink), inset 0 0 0 2px var(--ink); animation: spin 2.4s linear infinite; border-top-color: color-mix(in srgb, var(--c) 45%, #fff); }
@keyframes spin { to { transform: rotate(360deg); } }
.steps .name { font: 800 20px/1.2 var(--display); }
.steps li.todo .name { color: var(--muted); font-weight: 700; }
.bar-p { height: 14px; border-radius: 99px; border: var(--line); background: var(--cream); overflow: hidden; margin: var(--s2) 0 var(--s1); }
.bar-p span { display: block; height: 100%; background: var(--c); border-right: 2px solid var(--ink); transition: width .5s ease; }
.bar-p.wait span { width: 28%; animation: slide 1.6s ease-in-out infinite; border-left: 2px solid var(--ink); }
@keyframes slide { 0% { transform: translateX(-110%); } 100% { transform: translateX(380%); } }
.detail { color: var(--muted); font-size: .95rem; font-weight: 600; }

/* result */
.result-head h2 { font-size: clamp(28px, 5vw, 40px); margin: 0 0 var(--s4); overflow-wrap: anywhere; }
[role=tablist] { display: inline-flex; gap: 4px; padding: 4px; border: var(--line); border-radius: 999px; margin-bottom: var(--s4); max-width: 100%; overflow-x: auto; background: var(--paper); }
[role=tab] { font: 700 16px/1 system-ui, sans-serif; min-height: 44px; padding: 0 18px; border: 0; border-radius: 999px; background: none; color: var(--ink); cursor: pointer; white-space: nowrap; }
[role=tab]:hover { background: var(--cream); }
[role=tab][aria-selected=true] { background: var(--ink); color: var(--chip-fg, var(--sun)); }
.actions { display: flex; gap: var(--s2); flex-wrap: wrap; margin-bottom: var(--s4); }
.doc { max-width: 74ch; }
.doc h2 { font-size: 26px; font-weight: 800; margin: var(--s5) 0 var(--s2); display: flex; align-items: center; gap: 10px; }
.doc h2::before { content: ""; width: 18px; height: 18px; border-radius: 50%; background: var(--c, var(--coral)); box-shadow: 0 0 0 2px var(--ink); flex: none; }
.doc h2:nth-of-type(4n+2)::before { --c: var(--teal); } .doc h2:nth-of-type(4n+3)::before { --c: var(--orange); } .doc h2:nth-of-type(4n)::before { --c: var(--violet); }
.doc h3 { font-size: 20px; margin: var(--s4) 0 var(--s2); }
.doc ul { margin: 0 0 var(--s3); padding-left: 1.3em; }
.doc li { margin-bottom: 6px; }
.doc li::marker { color: var(--ink); }
.doc code { font-size: .9em; background: var(--cream); padding: 1px 6px; border-radius: 6px; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; margin-bottom: var(--s3); border: var(--line); border-radius: 18px; }
table { border-collapse: collapse; width: 100%; font-size: .95rem; }
th, td { text-align: left; vertical-align: top; padding: 10px var(--s3); }
th { background: var(--ink); color: var(--chip-fg, var(--sun)); font-weight: 800; }
td { border-top: 2px solid color-mix(in srgb, var(--ink) 18%, transparent); }
.turn { display: grid; grid-template-columns: 72px 1fr; gap: var(--s3); padding: var(--s3) 0; border-bottom: 2px dashed color-mix(in srgb, var(--ink) 18%, transparent); }
.turn time { color: var(--muted); font-variant-numeric: tabular-nums; font-size: .9rem; font-weight: 600; padding-top: 3px; }
.who { display: flex; align-items: center; gap: 8px; font-weight: 800; }
.who .ball { font-size: 14px; box-shadow: 0 0 0 2px var(--ink); }
.speaker { display: grid; grid-template-columns: 1fr minmax(200px, 270px); gap: var(--s4); padding: var(--s4) 0; border-bottom: 2px dashed color-mix(in srgb, var(--ink) 18%, transparent); align-items: center; }
.speaker .who { font: 800 20px/1.2 var(--display); }
.speaker .who .ball { font-size: 26px; }
.speaker blockquote { margin: var(--s2) 0 0; color: var(--muted); font-style: italic; overflow-wrap: anywhere; }
.speaker .field { margin: 0; }

/* footer */
footer { position: relative; margin-top: var(--s6); padding: var(--s6) 0 320px; overflow: hidden; }
footer .big { font: 800 clamp(34px, 6.4vw, 60px)/1 var(--display); letter-spacing: -0.04em; margin: 0 0 var(--s3); max-width: 15ch; }
footer .about { max-width: 58ch; font-size: 1.05rem; font-weight: 500; }
footer .count { font-weight: 700; line-height: 2; }
footer .fine { color: var(--muted-sun); font-size: .92rem; font-weight: 600; }
footer .copy { font-weight: 800; margin-top: var(--s4); }
.pile { position: absolute; left: 0; bottom: 0; width: 100%; height: 600px; pointer-events: none; z-index: 0; }
footer .wrap { position: relative; z-index: 1; }

@media (max-width: 640px) {
  .card { padding: var(--s4) 20px; border-radius: 20px; }
  .speaker { grid-template-columns: 1fr; gap: var(--s3); }
  .turn { grid-template-columns: 1fr; gap: 2px; }
  footer { padding-bottom: 220px; }
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
  .bar-p.wait span { width: 100%; opacity: .5; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="bar" id="bar">
    <button type="button" class="mark" id="home-mark" aria-label="MoMy, kembali ke semua rapat">M<span class="ball out" aria-hidden="true"></span>My</button>
    <div class="actions" style="margin: 0 0 0 auto">
      <button type="button" class="btn" id="back"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5m0 0 6-6m-6 6 6 6"/></svg>Semua rapat</button>
      <button type="button" class="btn" id="openSettings"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>Pengaturan AI</button>
    </div>
  </header>

  <dialog id="settings" aria-labelledby="setTitle">
    <form id="setForm">
      <h2 id="setTitle">Pengaturan AI</h2>
      <p class="muted">Suara dan transkrip selalu diproses di laptop ini. Di sini kamu pilih model AI yang menulis notulennya: gratis dan privat lewat <b>Ollama</b> di laptop ini, atau layanan online yang punya API mirip OpenAI.</p>
      <div class="actions" style="margin-bottom: 0">
        <button type="button" class="btn" id="presetLocal">Pakai Ollama di laptop ini</button>
        <button type="button" class="btn" id="presetGemini">Pakai Gemini (gratis)</button>
      </div>
      <p class="muted" id="geminiHelp" hidden>Ambil API key gratis di <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Google AI Studio</a> (login akun Google, tanpa kartu kredit), lalu tempel di kolom API key. Di paket gratis, Google boleh memakai teks yang dikirim untuk memperbaiki layanannya, jadi untuk rapat rahasia lebih aman pakai Ollama.</p>
      <div class="field"><label for="setUrl">Alamat API</label><input id="setUrl" type="url" required autocomplete="off"><small>Ollama: http://localhost:11434/v1</small></div>
      <div class="field"><label for="setModel">Nama model</label><input id="setModel" required maxlength="100" autocomplete="off"><small>Contoh untuk Ollama: gemma3:4b</small></div>
      <div class="field"><label for="setKey">API key</label><input id="setKey" type="password" autocomplete="off"><small id="keyHint">Kosongkan untuk Ollama.</small></div>
      <p id="setMsg" role="status" class="muted"></p>
      <div class="actions"><button type="submit" class="btn primary">Simpan</button><button type="button" class="btn" id="setTest">Tes koneksi</button><button type="button" class="btn" id="setClose">Tutup</button></div>
    </form>
  </dialog>

  <main>
    <section id="home">
      <div class="hero">
        <h1 aria-label="MoMy">M<span class="ball out" aria-hidden="true"></span>My</h1>
        <p class="tag">Kamu fokus rapat, MoMy yang nyatet.</p>
        <p class="sub">MoMy = Minutes of Meeting, yours.</p>
      </div>
      <div class="card">
        <h2>Rapat baru</h2>
        <div class="drop" id="drop">
          <div class="balls" aria-hidden="true"><span class="ball out" style="--c:var(--coral)"></span><span class="ball out" style="--c:var(--teal)"></span><span class="ball out" style="--c:var(--orange)"></span></div>
          <strong>Tarik file rekaman ke sini</strong>
          <p class="muted">mp4, m4a, mp3, wav: dari Zoom, Google Meet, atau rekaman HP</p>
          <button type="button" class="btn primary" id="pick"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3m0 0 4 4m-4-4-4 4"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>Pilih file</button>
          <input type="file" id="file" accept="audio/*,video/*,.m4a,.mp4,.mp3,.wav,.webm,.ogg" hidden>
        </div>
        <div class="chosen" id="chosen" hidden>
          <div class="file"><b id="fileName"></b><div class="muted" id="fileSize"></div></div>
          <button type="button" class="link" id="change">Ganti file</button>
        </div>
        <div class="field" id="speakersField" hidden>
          <label for="speakers">Jumlah orang yang bicara (boleh dikosongkan)</label>
          <input id="speakers" type="number" min="1" max="30" inputmode="numeric">
          <small>Kalau tahu, pembagian siapa-bicara-apa jadi lebih akurat.</small>
        </div>
        <button type="button" class="btn primary" id="go" hidden>Buat notulen</button>
        <p class="error" id="homeError" role="alert" hidden></p>
      </div>

      <div class="card">
        <h2>Rapat sebelumnya</h2>
        <ul class="list" id="past"></ul>
        <p class="muted" id="pastEmpty" hidden>Belum ada. Rapat yang kamu proses akan muncul di sini, lengkap dengan notulennya.</p>
      </div>
    </section>

    <section id="work" class="card" hidden aria-labelledby="workTitle">
      <h2 id="workTitle"></h2>
      <ol class="steps" id="steps"></ol>
      <p class="sr-only" id="live" aria-live="polite"></p>
      <p class="muted">Biarkan laptop menyala. Halaman ini boleh ditutup; prosesnya tetap jalan dan hasilnya muncul di <b>Rapat sebelumnya</b>.</p>
      <button type="button" class="btn danger" id="cancel">Batalkan</button>
    </section>

    <section id="fail" class="card" hidden>
      <h2>Notulen belum jadi</h2>
      <p class="error" id="failMsg" role="alert"></p>
      <div class="actions" style="margin-top: var(--s4)">
        <button type="button" class="btn primary" id="retry" hidden>Buat ulang notulen</button>
        <button type="button" class="btn" id="failBack">Kembali</button>
      </div>
    </section>

    <section id="result" class="card" hidden>
      <div class="result-head"><h2 id="resultTitle"></h2></div>
      <div role="tablist" aria-label="Hasil rapat">
        <button role="tab" id="tab-sum" aria-controls="panel-sum" aria-selected="true">Notulen</button>
        <button role="tab" id="tab-tr" aria-controls="panel-tr" aria-selected="false" tabindex="-1">Transkrip</button>
        <button role="tab" id="tab-sp" aria-controls="panel-sp" aria-selected="false" tabindex="-1">Nama pembicara</button>
      </div>
      <div role="tabpanel" id="panel-sum" aria-labelledby="tab-sum">
        <div class="actions" id="sumActions">
          <button type="button" class="btn primary" id="copy"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg><span>Salin notulen</span></button>
          <button type="button" class="btn" id="dlSum"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>Unduh .md</button>
        </div>
        <div class="doc" id="sumDoc"></div>
        <div id="noSum" hidden>
          <p>Transkrip sudah ada, tapi notulennya belum dibuat.</p>
          <button type="button" class="btn primary" id="makeSum">Buat notulen dari transkrip</button>
        </div>
      </div>
      <div role="tabpanel" id="panel-tr" aria-labelledby="tab-tr" hidden>
        <div class="actions"><button type="button" class="btn" id="dlTr"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>Unduh transkrip</button></div>
        <div id="trDoc"></div>
      </div>
      <div role="tabpanel" id="panel-sp" aria-labelledby="tab-sp" hidden>
        <p class="muted doc">Tiap warna satu orang. Baca kutipannya untuk mengenali siapa orangnya, lalu isi nama aslinya. Dua label dengan nama yang sama akan digabung. Setelah disimpan, notulen dibuat ulang (±30 detik).</p>
        <form id="spForm"><div id="spList"></div>
          <p class="error" id="spError" role="alert" hidden></p>
          <div class="actions" style="margin-top: var(--s4)"><button type="submit" class="btn primary">Simpan nama &amp; buat ulang notulen</button></div>
        </form>
      </div>
    </section>
  </main>
</div>
<footer>
  <div class="wrap">
    <p class="big">Minutes of Meeting, yours.</p>
    <p class="about">Notulen rapat yang punya kamu sendiri: rekamannya nggak keluar dari laptop, dan semua yang dibahas dia ingetin. Bonusnya, “MoMy” kebaca kayak <i>mommy</i>, sosok yang inget semua omongan kamu.</p>
    <p class="count" id="countLine" hidden>Sudah <span class="chip" id="count">0</span> rapat dirapikan di laptop ini.</p>
    <p class="fine" id="privacy">Suara dan pembicara diproses di laptop ini.</p>
    <p class="copy">© <span id="year">2026</span> Stevenmarp</p>
  </div>
  <canvas class="pile" id="pile" aria-hidden="true"></canvas>
</footer>
<script>
const $ = id => document.getElementById(id);
const STEPS = ["Membaca rekaman", "Mengenali pembicara", "Menulis transkrip", "Menyusun notulen"];
const STEP_COLORS = ["var(--coral)", "var(--teal)", "var(--orange)", "var(--violet)"];
const PALETTE = ["var(--coral)", "var(--teal)", "var(--orange)", "var(--violet)", "var(--pink)", "var(--green)", "var(--blue)", "#ffffff", "#141414", "#b9792f", "#c9e265", "#9fd8ff"];
const views = ["home", "work", "fail", "result"];
let chosen = null, current = null, poll = null, lastName = null, colorOf = {};

function show(view) { views.forEach(v => $(v).hidden = v !== view); $("home-mark").hidden = $("back").hidden = view === "home"; window.scrollTo(0, 0); }
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-MoMy": "1", ...(opts.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Gagal (${res.status})`);
  return data;
}
const esc = s => s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, "$1<em>$2</em>").replace(/`(.+?)`/g, "<code>$1</code>");
function table(rows) {
  const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const [head, ...body] = rows.filter(r => !/^\|[\s:|-]+\|?$/.test(r.trim()));
  return `<div class="table-wrap"><table><thead><tr>${cells(head).map(c => `<th scope="col">${inline(c)}</th>`).join("")}</tr></thead>` +
    `<tbody>${body.map(r => `<tr>${cells(r).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function markdown(src) {
  const out = []; let list = null, rows = null;
  const flush = () => { if (list) out.push(`<ul>${list.join("")}</ul>`); if (rows) out.push(table(rows)); list = rows = null; };
  for (const line of src.split("\n")) {
    let m;
    if ((m = line.match(/^(#{1,3}) (.*)/))) { flush(); if (m[1].length > 1) out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
    else if ((m = line.match(/^\s*[-*] (.*)/))) { if (rows) flush(); (list = list || []).push(`<li>${inline(m[1])}</li>`); }
    else if (line.trim().startsWith("|")) { if (list) flush(); (rows = rows || []).push(line); }
    else if (!line.trim()) flush();
    else { flush(); out.push(`<p>${inline(line)}</p>`); }
  }
  flush(); return out.join("");
}
const ball = (color, extra = "") => `<span class="ball out ${extra}" style="--c:${color}" aria-hidden="true"></span>`;
function size(n) { return n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(0) + " MB"; }
function when(t) { return new Intl.DateTimeFormat("id-ID", { dateStyle: "medium", timeStyle: "short" }).format(new Date(t * 1000)); }
function minutes(s) { const m = Math.max(1, Math.round(s / 60)); return m < 60 ? `${m} menit` : `${Math.floor(m / 60)} jam ${m % 60} menit`; }
function download(text, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = filename; a.click(); URL.revokeObjectURL(a.href);
}

/* footer pile: a small physics toy. Balls fall in when the footer comes into view, the cursor
   knocks them around, scrolling jiggles them. The loop only runs while the footer is visible
   and something still moves, so an idle page costs no CPU. */
function pile() {
  const cv = $("pile"), ctx = cv.getContext("2d");
  const COLORS = ["#ff6b6b", "#1fb5a5", "#ffa62b", "#7b68ee", "#141414", "#ffffff", "#ff8fc7"];
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const cursor = { x: -999, y: -999, vx: 0, vy: 0, r: 46, t: 0 };
  let W = 0, H = 0, dpr = 1, balls = [], ink = "#141414", sun = "#ffd23f";
  let raf = 0, last = 0, calm = 0, visible = false, lastScroll = scrollY, seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;

  function theme() {
    const cs = getComputedStyle(document.documentElement);
    ink = cs.getPropertyValue("--ink").trim(); sun = cs.getPropertyValue("--sun").trim();
  }
  function resize() {
    const box = cv.getBoundingClientRect();
    dpr = devicePixelRatio || 1; W = box.width; H = box.height;
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  }
  function spawn() {
    seed = 7;
    const spread = Math.min(W, 1000);
    balls = Array.from({ length: Math.round(Math.min(W, 900) / (W < 640 ? 15 : 12)) }, (_, i) => {
      const r = (W < 640 ? 9 : 11) + rnd() * (W < 640 ? 22 : 30);
      return { r, m: r * r, x: r + Math.pow(rnd(), 1.8) * (spread - 2 * r), y: -r - i * 16 - rnd() * 60,
               vx: 0, vy: 0, c: COLORS[Math.floor(rnd() * COLORS.length)], ring: rnd() < .22 };
    });
  }
  function step(dt) {
    // Position-based: move, push overlaps apart, then derive velocity from what really moved.
    // A ball held up by others ends with zero velocity, so the pile can come to a full stop.
    for (const b of balls) { b.ox = b.x; b.oy = b.y; b.vy += 1700 * dt; b.x += b.vx * dt; b.y += b.vy * dt; }
    for (let it = 0; it < 4; it++) {
      for (let i = 0; i < balls.length; i++) {
        const a = balls[i];
        for (let j = i + 1; j < balls.length; j++) {
          const b = balls[j], dx = b.x - a.x, dy = b.y - a.y, d = a.r + b.r, d2 = dx * dx + dy * dy;
          if (d2 >= d * d || d2 === 0) continue;
          const dist = Math.sqrt(d2), nx = dx / dist, ny = dy / dist, ia = 1 / a.m, ib = 1 / b.m;
          const push = (d - dist) / (ia + ib);
          a.x -= nx * push * ia; a.y -= ny * push * ia; b.x += nx * push * ib; b.y += ny * push * ib;
        }
        const cx = a.x - cursor.x, cy = a.y - cursor.y, cd = a.r + cursor.r, c2 = cx * cx + cy * cy;
        if (c2 < cd * cd && c2 > 0) { const dist = Math.sqrt(c2); a.x += cx / dist * (cd - dist); a.y += cy / dist * (cd - dist); }
        a.y = Math.min(a.y, H - a.r); a.x = Math.min(Math.max(a.x, a.r), W - a.r);
        if (a.inside) a.y = Math.max(a.y, a.r); else if (a.y > a.r) a.inside = true;
      }
    }
    for (const b of balls) {
      let vx = (b.x - b.ox) / dt, vy = (b.y - b.oy) / dt;
      if (b.y >= H - b.r - .5) vx *= .9;
      const speed = Math.hypot(vx, vy), cap = speed > 2600 ? 2600 / speed : .998;
      b.vx = speed < 6 ? 0 : vx * cap; b.vy = speed < 6 ? 0 : vy * cap;
    }
  }
  function draw() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    ctx.lineWidth = 2.5; ctx.strokeStyle = ink;
    for (const b of balls) {
      if (b.y + b.r < 0) continue;
      ctx.beginPath(); ctx.arc(b.x, b.y, b.r - 1, 0, 7); ctx.fillStyle = b.c; ctx.fill(); ctx.stroke();
      if (b.ring) { ctx.beginPath(); ctx.arc(b.x, b.y, b.r * .48, 0, 7); ctx.fillStyle = sun; ctx.fill(); ctx.stroke(); }
      else if (b.c !== "#ffffff") { ctx.beginPath(); ctx.arc(b.x - b.r * .36, b.y - b.r * .38, b.r * .17, 0, 7); ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fill(); }
    }
  }
  function frame(t) {
    const dt = last ? Math.min(.033, (t - last) / 1000) : 1 / 60; last = t;
    step(dt / 2); step(dt / 2); draw();
    let moved = 0;
    for (const b of balls) { moved = Math.max(moved, Math.abs(b.x - (b.px ?? b.x)) + Math.abs(b.y - (b.py ?? b.y))); b.px = b.x; b.py = b.y; }
    const moving = moved > .5 || performance.now() - cursor.t < 250;
    calm = moving ? 0 : calm + 1;
    if (visible && calm < 45) raf = requestAnimationFrame(frame);
    else { raf = 0; last = 0; }
  }
  function wake() { if (!raf && visible && !still.matches) { calm = 0; raf = requestAnimationFrame(frame); } }
  function settle() { for (let i = 0; i < 1500; i++) step(1 / 120); for (const b of balls) b.vx = b.vy = 0; draw(); }

  addEventListener("pointermove", e => {
    const box = cv.getBoundingClientRect(), x = e.clientX - box.left, y = e.clientY - box.top, now = performance.now();
    if (x < -80 || y < -80 || x > W + 80 || y > H + 80) { cursor.x = cursor.y = -999; return; }
    const dt = Math.max(16, now - cursor.t) / 1000;
    if (cursor.x > -999) { cursor.vx = (x - cursor.x) / dt; cursor.vy = (y - cursor.y) / dt; }
    cursor.x = x; cursor.y = y; cursor.t = now; wake();
  }, { passive: true });
  addEventListener("scroll", () => {
    const d = Math.abs(scrollY - lastScroll); lastScroll = scrollY;
    if (!visible || still.matches || !d) return;
    for (const b of balls) if (b.y > H - b.r * 3) b.vy -= Math.min(260, d * 6) * (.4 + rnd() * .8);
    wake();
  }, { passive: true });
  addEventListener("resize", () => { resize(); spawn(); still.matches ? settle() : wake(); });
  new IntersectionObserver(([entry]) => {
    const was = visible; visible = entry.isIntersecting;
    if (visible && !was && !still.matches) { spawn(); wake(); }
  }, { threshold: .05 }).observe(cv);

  theme(); resize(); spawn();
  if (still.matches) settle();
}

/* home */
function choose(file) {
  if (!file) return;
  chosen = file; $("homeError").hidden = true;
  $("fileName").textContent = file.name; $("fileSize").textContent = size(file.size);
  $("drop").hidden = true; ["chosen", "speakersField", "go"].forEach(id => $(id).hidden = false);
  $("go").focus();
}
function resetHome() {
  chosen = null; $("file").value = ""; $("speakers").value = "";
  $("drop").hidden = false; ["chosen", "speakersField", "go"].forEach(id => $(id).hidden = true);
  $("go").disabled = false; $("go").textContent = "Buat notulen";
}
$("pick").onclick = () => $("file").click();
$("change").onclick = () => { resetHome(); $("pick").focus(); };
$("file").onchange = e => choose(e.target.files[0]);
const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => choose(e.dataTransfer.files[0]));
$("go").onclick = () => {
  const n = $("speakers").value.trim();
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?filename=${encodeURIComponent(chosen.name)}&speakers=${encodeURIComponent(n)}`);
  xhr.setRequestHeader("X-MoMy", "1");
  $("go").disabled = true;
  xhr.upload.onprogress = e => { if (e.lengthComputable) $("go").textContent = `Mengunggah… ${Math.round(100 * e.loaded / e.total)}%`; };
  xhr.onload = () => {
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status === 200) { resetHome(); watch(); }
    else { $("go").disabled = false; $("go").textContent = "Buat notulen"; $("homeError").textContent = data.error || "Gagal mengunggah."; $("homeError").hidden = false; }
  };
  xhr.onerror = () => { $("go").disabled = false; $("go").textContent = "Buat notulen"; $("homeError").textContent = "Server MoMy tidak menjawab. Buka lagi aplikasinya."; $("homeError").hidden = false; };
  xhr.send(chosen);
};
async function loadPast() {
  const items = await api("/api/meetings").catch(() => []);
  $("past").innerHTML = items.map((m, i) => `<li><button type="button" data-name="${esc(m.name)}">
      <span style="display:flex;gap:14px;align-items:center;min-width:0">${ball(PALETTE[i % 7], "")}<span style="min-width:0"><span class="title">${esc(m.name)}</span><br><span class="meta">${when(m.time)}</span></span></span>
      ${m.summary ? '<span class="chip">Notulen siap</span>' : '<span class="chip warn">Belum ada notulen</span>'}
    </button></li>`).join("");
  $("past").querySelectorAll(".ball").forEach(b => b.style.fontSize = "28px");
  $("pastEmpty").hidden = items.length > 0;
  $("count").textContent = items.filter(m => m.summary).length;
  $("countLine").hidden = !items.some(m => m.summary);
  $("past").querySelectorAll("button").forEach(b => b.onclick = () => openMeeting(b.dataset.name));
}
function goHome() { stopPoll(); show("home"); loadPast(); }

/* work */
function stopPoll() { if (poll) clearInterval(poll); poll = null; }
function watch() { show("work"); stopPoll(); tick(); poll = setInterval(tick, 1000); }
async function tick() {
  let st; try { st = await api("/api/status"); } catch (_) { return; }
  lastName = st.name || lastName;
  if (!st.running) {
    stopPoll();
    if (st.done) return openMeeting(st.name);
    if (st.error) return fail(st.error, st.name);
    return goHome();
  }
  $("workTitle").textContent = st.name;
  const elapsed = st.now - (st.step_started || st.now);
  $("steps").innerHTML = STEPS.map((label, i) => {
    const n = i + 1, state = n < st.step ? "done" : n === st.step ? "active" : "todo", c = STEP_COLORS[i];
    let extra = "";
    if (state === "active") {
      const p = st.percent;
      const eta = p >= 3 && p < 100 ? ` · sekitar ${minutes(elapsed * (100 - p) / p)} lagi` : "";
      extra = p == null ? `<div class="bar-p wait" style="--c:${c}" role="progressbar" aria-label="${label}"><span></span></div>`
        : `<div class="bar-p" style="--c:${c}" role="progressbar" aria-label="${label}" aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100"><span style="width:${p}%"></span></div>`;
      extra += `<div class="detail">${esc([p != null ? p + "%" : "", st.detail].filter(Boolean).join(" · ") + eta)}</div>`;
    }
    const icon = state === "done" ? ball(c) : state === "active" ? "" : n;
    return `<li class="${state}"><span class="sball" style="--c:${c}" aria-hidden="true">${icon}</span>
      <div><span class="name">${label}</span><span class="sr-only"> (langkah ${n} dari 4${state === "done" ? ", selesai" : ""})</span>${extra}</div></li>`;
  }).join("");
  $("live").textContent = `Langkah ${st.step} dari 4: ${STEPS[st.step - 1]}`;
}
$("cancel").onclick = async () => {
  if (!confirm("Batalkan proses rapat ini? Yang sudah dikerjakan akan hilang.")) return;
  await api("/api/cancel", { method: "POST" }).catch(() => {});
};
function fail(message, name) {
  show("fail"); $("failMsg").textContent = message; lastName = name;
  api(`/api/meeting?name=${encodeURIComponent(name)}`).then(m => $("retry").hidden = !m.transcript).catch(() => $("retry").hidden = true);
}
$("retry").onclick = () => summarize(lastName);
$("failBack").onclick = goHome;
async function summarize(name) {
  try { await api("/api/summarize", { method: "POST", body: JSON.stringify({ name }) }); watch(); }
  catch (e) { alert(e.message); }
}

/* result */
function selectTab(tab) {
  document.querySelectorAll("[role=tab]").forEach(t => {
    const on = t === tab; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  });
}
document.querySelectorAll("[role=tab]").forEach((t, i, all) => {
  t.onclick = () => selectTab(t);
  t.onkeydown = e => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (d) { const next = all[(i + d + all.length) % all.length]; selectTab(next); next.focus(); }
  };
});
async function openMeeting(name) {
  try { current = await api(`/api/meeting?name=${encodeURIComponent(name)}`); }
  catch (e) { return fail(e.message, name); }
  show("result"); selectTab($("tab-sum"));
  $("resultTitle").textContent = current.name;
  if (current.summary) current.summary = current.summary.replace(/^- (File|LLM|Model): .*\n/gm, "");
  const hasSum = !!current.summary;
  $("sumDoc").innerHTML = hasSum ? markdown(current.summary) : "";
  $("sumActions").hidden = !hasSum; $("noSum").hidden = hasSum;
  $("tab-tr").hidden = $("tab-sp").hidden = !current.transcript;
  colorOf = {}; current.speakers.forEach((s, i) => colorOf[s.label] = PALETTE[i % PALETTE.length]);
  $("trDoc").innerHTML = (current.transcript || "").split("\n").map(line => {
    const m = line.match(/^\*\*\[([\d:]+)\] (.+?):\*\* ?(.*)$/);
    return m ? `<div class="turn"><time>${m[1]}</time><div><span class="who">${ball(colorOf[m[2]] || "#ffffff")}${esc(m[2])}</span>${esc(m[3])}</div></div>` : "";
  }).join("");
  $("spList").innerHTML = current.speakers.map((s, i) => `<div class="speaker">
      <div><span class="who">${ball(colorOf[s.label])}${esc(s.label)} <span class="chip">${s.share}%</span></span><blockquote>“${esc(s.quote)}”</blockquote></div>
      <div class="field"><label for="sp${i}">Nama untuk ${esc(s.label)}</label>
      <input id="sp${i}" data-label="${esc(s.label)}" value="${/^Speaker \d+$/.test(s.label) ? "" : esc(s.label)}" maxlength="40" autocomplete="off"></div>
    </div>`).join("");
  $("spError").hidden = true;
}
$("back").onclick = goHome;
$("home-mark").onclick = goHome;
$("makeSum").onclick = () => summarize(current.name);
$("copy").onclick = async () => {
  await navigator.clipboard.writeText(current.summary);
  $("copy").querySelector("span").textContent = "Tersalin";
  setTimeout(() => $("copy").querySelector("span").textContent = "Salin notulen", 2000);
};
$("dlSum").onclick = () => download(current.summary, `${current.name}.summary.md`);
$("dlTr").onclick = () => download(current.transcript, `${current.name}.transcript.md`);
$("spForm").onsubmit = async e => {
  e.preventDefault();
  const mapping = {};
  $("spList").querySelectorAll("input").forEach(inp => { if (inp.value.trim()) mapping[inp.dataset.label] = inp.value.trim(); });
  try { await api("/api/rename", { method: "POST", body: JSON.stringify({ name: current.name, mapping }) }); watch(); }
  catch (err) { $("spError").textContent = err.message; $("spError").hidden = false; }
};

$("year").textContent = new Date().getFullYear();

/* AI settings */
let config = null;
async function loadConfig() {
  config = await api("/api/config").catch(() => null);
  if (!config) return;
  $("privacy").textContent = config.note || (config.ollama
    ? `Suara, pembicara, dan notulen semuanya diproses di laptop ini (${config.model} lewat Ollama).`
    : `Suara dan pembicara diproses di laptop ini. Notulen ditulis ${config.model} lewat ${config.host}: teks transkripnya dikirim ke sana.`);
}
function setMsg(text, kind = "") { $("setMsg").textContent = text; $("setMsg").className = kind || "muted"; }
function settingsBody() { return JSON.stringify({ base_url: $("setUrl").value.trim(), model: $("setModel").value.trim(), api_key: $("setKey").value.trim() }); }
$("openSettings").onclick = async () => {
  await loadConfig();
  $("setUrl").value = config ? config.base_url : ""; $("setModel").value = config ? config.model : ""; $("setKey").value = "";
  $("keyHint").textContent = config && config.has_key ? "Sudah tersimpan. Kosongkan untuk tetap memakai yang lama." : "Kosongkan untuk Ollama.";
  const locked = config ? config.locked : [];
  $("setUrl").disabled = locked.includes("base_url"); $("setModel").disabled = locked.includes("model"); $("setKey").disabled = locked.includes("api_key");
  setMsg(locked.length ? "Sebagian pengaturan diatur lewat environment (MOMY_...), jadi tidak bisa diubah di sini." : "");
  $("settings").showModal();
};
$("setClose").onclick = () => $("settings").close();
$("presetLocal").onclick = () => { $("setUrl").value = "http://localhost:11434/v1"; $("setModel").value = "gemma3:4b"; $("setKey").value = ""; $("geminiHelp").hidden = true; setMsg(""); };
$("presetGemini").onclick = () => {
  $("setUrl").value = "https://generativelanguage.googleapis.com/v1beta/openai"; $("setModel").value = "gemini-flash-lite-latest";
  $("geminiHelp").hidden = false; setMsg(""); $("setKey").focus();
};
$("setTest").onclick = async () => {
  setMsg("Mengetes…");
  try { const r = await api("/api/config/test", { method: "POST", body: settingsBody() }); setMsg(r.message, r.ok ? "ok" : "bad"); }
  catch (e) { setMsg(e.message, "bad"); }
};
$("setForm").onsubmit = async e => {
  e.preventDefault();
  try { await api("/api/config", { method: "POST", body: settingsBody() }); await loadConfig(); setMsg("Tersimpan.", "ok"); setTimeout(() => $("settings").close(), 700); }
  catch (err) { setMsg(err.message, "bad"); }
};
loadConfig();
pile();
api("/api/status").then(st => st.running ? watch() : goHome()).catch(goHome);
</script>
</body>
</html>
"""


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    url = f"http://127.0.0.1:{PORT}/"
    print(f"MoMy: {url}  (folder: {WORK})", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
