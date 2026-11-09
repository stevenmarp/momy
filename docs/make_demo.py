"""Build docs/demo/index.html: the real MoMy page with its server calls answered in the browser
from one meeting that was already processed (docs/demo/meeting.*.md). Run after changing momy/ui.py:

    python docs/make_demo.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from momy import ui  # noqa: E402

DEMO = ROOT / "docs" / "demo"
NAME = "Kickoff proyek remote TV"

MOCK = r"""
<script>
/* Demo: answer the app's API from the meeting embedded below, never pretend to process anything. */
(() => {
  const data = JSON.parse(document.getElementById("demo-data").textContent);
  const NOTE = "Demo di browser: rapat contohnya sudah diproses MoMy sebelumnya. Untuk rekamanmu sendiri, install MoMy di laptopmu.";
  const INSTALL = "Ini demo, jadi tidak ada yang diproses di sini. Untuk rekaman dan pengaturan AI-mu sendiri, install MoMy di laptop: momy.stevenmarp.xyz/#install";
  let done = false;
  const relabel = (text, map) => text.replace(/^(\*\*\[[\d:]+\] )(.+?)(:\*\*)/gm, (m, a, who, b) => a + (map[who] || who) + b);
  const rename = (text, map) => Object.entries(map).reduce((t, [old, name]) =>
    t.split(` (${old})`).join("").split(old).join(name), text);
  function speakers(transcript) {
    const talk = {}, quote = {};
    for (const line of transcript.split("\n")) {
      const m = line.match(/^\*\*\[([\d:]+)\] (.+?):\*\* ?(.*)$/);
      if (!m) continue;
      talk[m[2]] = (talk[m[2]] || 0) + m[3].length;
      if ((quote[m[2]] || "").length < 60 && m[3].length > (quote[m[2]] || "").length)
        quote[m[2]] = m[3].length <= 160 ? m[3] : m[3].slice(0, 157).replace(/ \S*$/, "") + "…";
    }
    const total = Object.values(talk).reduce((a, b) => a + b, 0) || 1;
    return Object.entries(talk).sort((a, b) => b[1] - a[1]).map(([label, n]) => ({ label, share: Math.round(100 * n / total), quote: quote[label] }));
  }
  const reply = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  window.fetch = async (url, opts = {}) => {
    const path = new URL(url, location.href).pathname.replace(/^.*\/api\//, "/api/");
    if (path === "/api/status") { const st = { running: false, done, name: data.name, now: Date.now() / 1000 }; done = false; return reply(200, st); }
    if (path === "/api/meetings") return reply(200, [{ name: data.name, summary: true, transcript: true, time: data.time }]);
    if (path === "/api/meeting") return reply(200, { ...data, speakers: speakers(data.transcript) });
    if (path === "/api/config") return opts.method === "POST" ? reply(400, { error: INSTALL }) : reply(200, { note: NOTE, model: "", base_url: "", has_key: false, ollama: false, host: "", locked: [] });
    if (path === "/api/config/test") return reply(400, { error: INSTALL });
    if (path === "/api/rename") {
      const map = JSON.parse(opts.body).mapping || {};
      data.transcript = relabel(data.transcript, map); data.summary = rename(data.summary, map); done = true;
      return reply(200, { name: data.name });
    }
    if (path === "/api/summarize") { done = true; return reply(200, { name: data.name }); }
    return reply(200, { ok: true });
  };
  window.XMLHttpRequest = class {
    constructor() { this.upload = {}; }
    open() {} setRequestHeader() {}
    send() { setTimeout(() => { this.status = 400; this.responseText = JSON.stringify({ error: INSTALL }); this.onload && this.onload(); }, 300); }
  };
})();
</script>
"""

BANNER = """
    <div class="card demo-note" role="note">
      <b>Demo</b> · Ini aplikasi MoMy yang asli, berisi satu rapat contoh yang sudah diproses: rapat kickoff berbahasa Inggris
      dari AMI Meeting Corpus (CC BY 4.0), notulennya ditulis dalam Bahasa Indonesia. Buka notulennya, lihat transkrip, coba ganti
      nama pembicara. Untuk rekamanmu sendiri, <a href="../#install">install MoMy</a>.
    </div>"""


def main():
    transcript = (DEMO / "meeting.transcript.md").read_text(encoding="utf-8")
    summary = (DEMO / "meeting.summary.md").read_text(encoding="utf-8")
    data = {"name": NAME, "transcript": transcript, "summary": summary, "time": 1794225600}
    page = ui.PAGE
    replacements = [
        ("url(/font.woff2)", "url(../BricolageGrotesque.woff2)"),
        ("<title>MoMy · Minutes of Meeting, yours</title>", "<title>Demo · MoMy</title>"),
        ("  <main>\n", "  <main>\n" + BANNER + "\n"),
        (".sr-only {", ".demo-note { padding: var(--s4); line-height: 1.6; } .demo-note a { color: var(--ink); font-weight: 800; }\n.sr-only {"),
        ("<script>\nconst $ = id", '<script type="application/json" id="demo-data">'
         + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>\n" + MOCK + "<script>\nconst $ = id"),
    ]
    for old, new in replacements:
        if old not in page:
            raise SystemExit(f"momy/ui.py changed, update make_demo.py: {old[:40]!r} not found")
        page = page.replace(old, new, 1)
    (DEMO / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote {DEMO / 'index.html'} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
