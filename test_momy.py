from momy.cli import assign_speakers, merge_minor_speakers, render_transcript, split, summarize


def test_assign_speakers():
    turns = [(0.0, 2.0, 7), (2.5, 4.0, 3), (5.0, 6.0, 7)]
    words = [
        (0.1, 0.5, " Halo"),
        (0.6, 1.0, " semua."),
        (2.6, 3.0, " Siap"),
        (3.1, 3.5, " pak."),
        (4.2, 4.4, " Oke"),
        (5.1, 5.5, " lanjut."),
    ]
    lines = assign_speakers(words, turns)
    assert lines == [
        ("Speaker 1", 0.1, "Halo semua."),
        ("Speaker 2", 2.6, "Siap pak."),
        ("Speaker 1", 4.2, "Oke lanjut."),
    ], lines
    assert render_transcript(lines[:1]) == "**[00:00:00] Speaker 1:** Halo semua."
    assert assign_speakers(words[:2], []) == [("Speaker 1", 0.1, "Halo semua.")]


def test_merge_minor_speakers():
    turns = [(0, 10, "A"), (10.5, 11, "X"), (11, 20, "B"), (30, 40, "A"), (41, 41.5, "Y")]
    assert merge_minor_speakers(turns) == [
        (0, 10, "A"), (10.5, 11, "B"), (11, 20, "B"), (30, 40, "A"), (41, 41.5, "A")
    ]
    assert {t[2] for t in merge_minor_speakers(turns, keep=1)} == {"A"}
    assert merge_minor_speakers([]) == []


def test_split():
    text = "\n\n".join(["a" * 40] * 5)
    chunks = split(text, 100)
    assert len(chunks) == 3, chunks
    assert "".join(chunks).split() == text.split()
    assert split("short", 100) == ["short\n\n"]


def test_summarize_reduces_until_it_fits():
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return "FINAL" if "## Ringkasan" in prompt else "note"

    text = "\n\n".join(["x" * 50] * 10)
    assert summarize("short", 1000, ask) == "FINAL" and len(calls) == 1
    calls.clear()
    assert summarize(text, 120, ask) == "FINAL"
    assert len(calls) == len(split(text, 120)) + 1, len(calls)


def test_chat_retries_server_errors():
    import http.server
    import json
    import threading

    from momy import cli as notulen

    codes = [500, 200]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            code = codes.pop(0)
            reply = {"choices": [{"message": {"content": "<think>hm</think> ok"}}]} if code == 200 else {"error": "oom"}
            body = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sleep, notulen.time.sleep = notulen.time.sleep, lambda seconds: None
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        assert notulen.chat("hi", "m", url, "k") == "ok"
        assert codes == []
    finally:
        notulen.time.sleep = sleep
        server.shutdown()


def test_ui_progress_rename_speakers():
    from momy import ui

    st = {}
    for line in ["Reading rapat.mp4 ...", "Finding speakers 40%", "Transcribing 00:30:00 / 01:00:00"]:
        ui.progress(line, st)
    assert (st["step"], st["percent"], st["detail"]) == (3, 50, "00:30:00 dari 01:00:00"), st
    ui.progress("Transcript: out/x.transcript.md", st)
    assert st["step"] == 4
    ui.progress("Cannot reach the LLM at http://x: refused. Is Ollama running?", st)
    assert ui.friendly(st["last"]).startswith("Model AI untuk menulis notulen tidak bisa dihubungi")

    transcript = "**[00:00:01] Speaker 1:** Halo semua, mulai ya.\n\n**[00:00:05] Speaker 2:** Siap.\n\n**[00:00:07] Speaker 1:** Speaker 2 setuju?"
    names = ui.rename(transcript, {"Speaker 1": "Rina", "Speaker 2": "Budi"})
    assert names.startswith("**[00:00:01] Rina:** Halo") and "**[00:00:05] Budi:** Siap." in names
    assert names.endswith("Rina:** Speaker 2 setuju?"), "only labels change, never the spoken text"
    stats = ui.speakers(transcript)
    assert [s["label"] for s in stats] == ["Speaker 1", "Speaker 2"] and stats[0]["share"] > stats[1]["share"]


def test_settings_env_beats_file_beats_default():
    import os
    import tempfile

    from momy import cli

    old_config, old_env = cli.CONFIG, os.environ.pop("MOMY_MODEL", None)
    try:
        cli.CONFIG = cli.Path(tempfile.mkdtemp()) / "momy" / "config.json"
        assert cli.settings()["model"] == "gemma3:4b"
        cli.save_settings({"base_url": "https://api.example.com/v1", "model": "saved-model", "api_key": "k"})
        assert oct(cli.CONFIG.stat().st_mode & 0o777) == "0o600"
        assert cli.settings()["model"] == "saved-model" and not cli.is_local(cli.settings()["base_url"])
        os.environ["MOMY_MODEL"] = "env-model"
        assert cli.settings()["model"] == "env-model"
    finally:
        cli.CONFIG = old_config
        os.environ.pop("MOMY_MODEL", None)
        if old_env is not None:
            os.environ["MOMY_MODEL"] = old_env


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
