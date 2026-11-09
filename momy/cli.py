import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

__version__ = "0.1.0"

SAMPLE_RATE = 16000
CACHE = Path(os.environ.get("MOMY_CACHE") or Path.home() / ".cache" / "momy")
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "momy" / "config.json"
DEFAULTS = {"base_url": "http://localhost:11434/v1", "model": "gemma3:4b", "api_key": ""}
SEGMENTATION_URL = (
    "https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx"
)
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "nemo_en_titanet_small.onnx"
)

NOTES_PROMPT = """Kamu notulis rapat. Berikut potongan {part} dari transkrip rapat (Bahasa Indonesia, kadang campur English).
Pembicara diberi label "Speaker N".

Tulis catatan ringkas dalam Bahasa Indonesia, hanya berdasarkan isi potongan ini:
- Poin pembahasan penting
- Keputusan yang diambil
- Tugas: siapa, apa, deadline (kalau disebut)
- Pertanyaan/topik yang belum selesai
Kalau nama asli pembicara disebut jelas, tulis "Nama (Speaker N)". Jangan mengarang.

Potongan transkrip:
{text}"""

SUMMARY_PROMPT = """Kamu notulis rapat profesional. Di bawah ini {source} rapat (Bahasa Indonesia, kadang campur English).
Pembicara diberi label "Speaker N".

Tulis notulen dalam Bahasa Indonesia, format Markdown persis seperti ini:

## Ringkasan
(maksimal 7 bullet, poin terpenting)

## Keputusan
(bullet; tulis "- Tidak ada keputusan eksplisit." kalau tidak ada)

## Action Items
| Siapa | Apa | Deadline |
|---|---|---|
(satu baris per tugas; "-" kalau deadline tidak disebut)

## Topik Belum Selesai
(bullet; tulis "- Tidak ada." kalau tidak ada)

Aturan:
- Hanya pakai informasi yang ada di teks. Jangan mengarang nama, tanggal, atau angka.
- Kalau nama asli pembicara disebut jelas, tulis "Nama (Speaker N)"; kalau tidak, pakai "Speaker N".
- Istilah English yang dipakai peserta boleh dipertahankan.

{text}"""


def settings():
    """The LLM to use: MOMY_BASE_URL / MOMY_MODEL / MOMY_API_KEY win over the config file the app writes."""
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {key: os.environ.get(f"MOMY_{key.upper()}") or saved.get(key) or value for key, value in DEFAULTS.items()}


def save_settings(values):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.touch(mode=0o600, exist_ok=True)
    CONFIG.chmod(0o600)
    CONFIG.write_text(json.dumps(values, indent=2), encoding="utf-8")


def is_local(base_url):
    return urllib.parse.urlparse(base_url).hostname in ("localhost", "127.0.0.1", "::1")


def log(msg, end="\n"):
    print(msg, file=sys.stderr, end=end, flush=True)


def fetch(url, name):
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        log(f"Downloading {name} ...")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(path)
    return str(path)


def clock(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcribe(audio, model_name, language, threads):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        download_root=str(CACHE / "whisper"),
    )
    segments, info = model.transcribe(
        audio,
        language=None if language == "auto" else language,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        log(f"\rTranscribing {clock(seg.end)} / {clock(info.duration)}", end="")
        words += [(w.start, w.end, w.word) for w in seg.words]
    log("")
    return words


def diarize(audio, threshold, threads):
    import sherpa_onnx

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=fetch(SEGMENTATION_URL, "pyannote-segmentation-3.0.onnx")
            ),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=fetch(EMBEDDING_URL, "nemo-titanet-small.onnx"), num_threads=threads
        ),
        # A fixed num_clusters cuts sherpa's complete-linkage tree badly on noisy audio
        # ([198, 2, 2, 2] s for 4 people), so --speakers is applied afterwards instead.
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise SystemExit("Invalid diarization config")

    def progress(done, total):
        log(f"\rFinding speakers {done * 100 // total}%", end="")
        return 0

    result = sherpa_onnx.OfflineSpeakerDiarization(config).process(audio, callback=progress)
    log("")
    return [(t.start, t.end, t.speaker) for t in result.sort_by_start_time()]


def merge_minor_speakers(turns, keep=None):
    """Clusters with very little speech are mostly noise from short segments: give their
    turns to the nearest (in time) turn of a kept speaker. keep=N keeps the N speakers
    with the most speech; otherwise everyone above max(5 s, 1% of all speech) stays."""
    talk = Counter()
    for start, end, speaker in turns:
        talk[speaker] += end - start
    if keep:
        kept = {speaker for speaker, _ in talk.most_common(keep)}
    else:
        floor = max(5.0, 0.01 * sum(talk.values()))
        kept = {speaker for speaker, seconds in talk.items() if seconds >= floor} or set(talk)
    anchors = [turn for turn in turns if turn[2] in kept]
    return [
        (start, end, speaker if speaker in kept
         else min(anchors, key=lambda t: max(t[0] - end, start - t[1], 0))[2])
        for start, end, speaker in turns
    ]


def assign_speakers(words, turns, pause=1.0):
    """Cut the words into sentences (end punctuation or a pause), give each sentence the
    speaker whose turns cover most of it, then merge consecutive sentences of one speaker
    into (speaker, start, text) lines."""
    sentences, current = [], []
    for i, word in enumerate(words):
        current.append(word)
        last = i + 1 == len(words)
        if last or word[2].rstrip().endswith((".", "?", "!")) or words[i + 1][0] - word[1] > pause:
            sentences.append(current)
            current = []
    lines, labels = [], {}
    for sentence in sentences:
        votes = Counter()
        for start, end, _ in sentence:
            if turns:
                # O(words * turns): seconds for a 2 h meeting, cheap next to Whisper.
                turn = max(turns, key=lambda t: min(end, t[1]) - max(start, t[0]))
                votes[turn[2]] += end - start
        speaker = votes.most_common(1)[0][0] if votes else 0
        label = labels.setdefault(speaker, f"Speaker {len(labels) + 1}")
        text = "".join(w[2] for w in sentence)
        if lines and lines[-1][0] == label:
            lines[-1][2] += text
        else:
            lines.append([label, sentence[0][0], text])
    return [(label, start, text.strip()) for label, start, text in lines]


def render_transcript(lines):
    return "\n\n".join(f"**[{clock(start)}] {label}:** {text}" for label, start, text in lines)


def split(text, limit):
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) > limit:
            chunks.append(current)
            current = ""
        current += para + "\n\n"
    return chunks + [current] if current.strip() else chunks


def chat(prompt, model, base_url, api_key, max_tokens=1500):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                content = json.load(response)["choices"][0]["message"]["content"] or ""
            return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        except urllib.error.HTTPError as e:
            error = f"LLM error {e.code}: {e.read().decode(errors='replace')[:500]}"
            if e.code < 500 or attempt == 3:
                raise SystemExit(error)
        except urllib.error.URLError as e:
            raise SystemExit(f"Cannot reach the LLM at {base_url}: {e.reason}. Is Ollama running?")
        # A 5xx is often the local model running out of memory; Ollama restarts it.
        log(f"{error}\nRetrying in 15 s ({attempt}/2) ...")
        time.sleep(15)


def summarize(text, limit, ask, source="transkrip"):
    if len(text) <= limit:
        return ask(SUMMARY_PROMPT.format(source=source, text=text))
    chunks = split(text, limit)
    notes = []
    for i, chunk in enumerate(chunks, 1):
        log(f"Summarizing part {i}/{len(chunks)} ...")
        notes.append(ask(NOTES_PROMPT.format(part=f"{i}/{len(chunks)}", text=chunk)))
    merged = "\n\n".join(notes)
    if len(merged) >= len(text):
        return ask(SUMMARY_PROMPT.format(source="catatan", text=merged[:limit]))
    return summarize(merged, limit, ask, source="catatan per bagian dari")


def main(argv=None):
    llm = settings()
    p = argparse.ArgumentParser(
        prog="momy",
        description="Meeting recording -> transcript with speakers -> meeting minutes. Runs locally.",
    )
    p.add_argument("input", help="audio/video file (mp3, wav, m4a, mp4, ...) or a .transcript.md made earlier")
    p.add_argument("-o", "--output-dir", help="where to write the files (default: next to the input)")
    p.add_argument("-s", "--speakers", type=int, help="number of speakers, if known (more accurate)")
    p.add_argument("--threshold", type=float, default=0.9,
                   help="speaker clustering threshold: "
                        "higher = fewer speakers (default: 0.9)")
    p.add_argument("--language", default="id", help="spoken language code, or 'auto' (default: id)")
    p.add_argument("--whisper", default=os.environ.get("MOMY_WHISPER", "large-v3-turbo"),
                   help="Whisper model: large-v3-turbo (best), small (about 2x faster), ... "
                        "(default: large-v3-turbo)")
    p.add_argument("--model", default=llm["model"], help="LLM model name at the endpoint (default: gemma3:4b)")
    p.add_argument("--base-url", default=llm["base_url"], help="OpenAI-compatible endpoint (default: local Ollama)")
    p.add_argument("--chunk-chars", type=int, default=os.environ.get("MOMY_CHUNK_CHARS"),
                   help="transcript characters per LLM call (default: 6000 for a local model, 120000 for a remote one)")
    p.add_argument("--max-tokens", type=int, default=os.environ.get("MOMY_MAX_TOKENS"),
                   help="answer length cap per LLM call (default: 1500 local, 16000 remote, for reasoning models)")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    p.add_argument("--no-summary", action="store_true", help="only write the transcript")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    source = Path(args.input)
    if not source.is_file():
        p.error(f"file not found: {source}")
    out_dir = Path(args.output_dir) if args.output_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.name.split(".transcript.")[0] if ".transcript." in source.name else source.stem
    started = time.time()

    if source.suffix.lower() in (".md", ".txt"):
        transcript = source.read_text(encoding="utf-8")
        duration, speakers = None, None
    else:
        from faster_whisper import decode_audio

        log(f"Reading {source.name} ...")
        audio = decode_audio(str(source), sampling_rate=SAMPLE_RATE)
        duration = len(audio) / SAMPLE_RATE
        turns = merge_minor_speakers(diarize(audio, args.threshold, args.threads), keep=args.speakers)
        words = transcribe(audio, args.whisper, args.language, args.threads)
        if not words:
            raise SystemExit("No speech found in the recording.")
        lines = assign_speakers(words, turns)
        speakers = len({label for label, _, _ in lines})
        transcript = render_transcript(lines)
        path = out_dir / f"{stem}.transcript.md"
        path.write_text(transcript + "\n", encoding="utf-8")
        log(f"Transcript: {path}")

    if args.no_summary:
        return
    local = is_local(args.base_url)
    chunk = args.chunk_chars or (6000 if local else 120000)
    max_tokens = args.max_tokens or (1500 if local else 16000)
    ask = lambda prompt: chat(prompt, args.model, args.base_url, llm["api_key"] or "none", max_tokens)
    body = summarize(transcript, chunk, ask)
    meta = [f"- File: `{source.name}`"]
    if duration:
        meta.append(f"- Durasi: {clock(duration)}")
    if speakers:
        meta.append(f"- Pembicara terdeteksi: {speakers}")
    meta.append(f"- LLM: `{args.model}`" if duration is None else f"- Model: Whisper `{args.whisper}`, LLM `{args.model}`")
    summary = f"# Notulen Rapat: {stem}\n\n" + "\n".join(meta) + "\n\n" + body + "\n"
    path = out_dir / f"{stem}.summary.md"
    path.write_text(summary, encoding="utf-8")
    log(f"Summary: {path} ({clock(time.time() - started)})")


if __name__ == "__main__":
    main()
