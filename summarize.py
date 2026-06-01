#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


USAGE = """Usage: ./summarize.sh [-qa] [-t] "https://www.youtube.com/watch?v=..."

Flags:
  -qa   Disable interactive Q&A mode after the summary
  -t    Transcribe only; print transcript to stdout and skip LM Studio

Required env:
  LM_API_TOKEN=...  (not required with -t)

Optional env:
  LM_HOST=localhost LM_PORT=5432 LM_MODEL=liquid/lfm2.5-1.2b
  CHUNK_SECONDS=60 KEEP_WORKDIR=0
  CACHE_DIR=~/.cache/yt-summarizer REFRESH_CACHE=0

Metadata env:
  INCLUDE_DESCRIPTION=1 INCLUDE_TAGS=1 INCLUDE_CHAPTERS=1
"""


STOPWORDS = {
    "about", "after", "again", "also", "and", "any", "are", "because",
    "but", "can", "did", "does", "for", "from", "had", "has", "have",
    "how", "into", "its", "just", "like", "not", "only", "out", "that",
    "the", "then", "this", "was", "were", "what", "when", "where",
    "which", "with", "would", "you",
}

CACHE_SCHEMA_VERSION = "v2"


class PipelineError(Exception):
    pass


@dataclass
class Config:
    condaenv: str
    lm_host: str
    lm_port: str
    lm_model: str
    chunk_seconds: int
    max_output_tokens: int
    temperature: float
    keep_workdir: bool
    lm_api_token: str
    cache_dir: Path
    refresh_cache: bool
    include_description: bool
    include_tags: bool
    include_chapters: bool
    pipeline_window_chunks: int
    qa_retrieval_notes: int
    qa_retrieval_chunks: int


@dataclass
class Runtime:
    config: Config
    workdir: Path
    logdir: Path
    lm_dir: Path
    transcribe_only: bool
    lm_counter: int = 0


@dataclass
class TranscriptChunk:
    index: int
    total: int
    name: str
    text: str


def env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


def env_int(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise PipelineError(f"{name} must be an integer, got {raw!r}") from exc


def env_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError as exc:
        raise PipelineError(f"{name} must be a number, got {raw!r}") from exc


def load_config() -> Config:
    return Config(
        condaenv=os.environ.get("CONDAENV", "voxmlx"),
        lm_host=os.environ.get("LM_HOST", "localhost"),
        lm_port=os.environ.get("LM_PORT", "5432"),
        lm_model=os.environ.get("LM_MODEL", "liquid/lfm2.5-1.2b"),
        chunk_seconds=env_int("CHUNK_SECONDS", "60"),
        max_output_tokens=env_int("MAX_OUTPUT_TOKENS", "2000"),
        temperature=env_float("TEMPERATURE", "0.2"),
        keep_workdir=env_bool("KEEP_WORKDIR", "0"),
        lm_api_token=os.environ.get("LM_API_TOKEN", ""),
        cache_dir=Path(os.environ.get("CACHE_DIR", "~/.cache/yt-summarizer")).expanduser(),
        refresh_cache=env_bool("REFRESH_CACHE", "0"),
        include_description=env_bool("INCLUDE_DESCRIPTION", "1"),
        include_tags=env_bool("INCLUDE_TAGS", "1"),
        include_chapters=env_bool("INCLUDE_CHAPTERS", "1"),
        pipeline_window_chunks=env_int("PIPELINE_WINDOW_CHUNKS", "3"),
        qa_retrieval_notes=env_int("QA_RETRIEVAL_NOTES", "8"),
        qa_retrieval_chunks=env_int("QA_RETRIEVAL_CHUNKS", "4"),
    )


def say(rt: Runtime, message: str) -> None:
    stream = sys.stderr if rt.transcribe_only else sys.stdout
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=stream, flush=True)


def need(command: str) -> None:
    if shutil.which(command) is None:
        raise PipelineError(f"Missing dependency: {command}")


def run_quiet(rt: Runtime, label: str, logfile: Path, args: list[str], cwd: Path | None = None) -> None:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    say(rt, label)
    with logfile.open("wb") as log:
        proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, cwd=cwd)
    if proc.returncode != 0:
        show_log_failure(label, logfile)
        raise PipelineError(f"Command failed: {label}")


def show_log_failure(label: str, logfile: Path) -> None:
    print(f"\n--- command failed: {label} ---", file=sys.stderr)
    print(f"Log: {logfile}\n", file=sys.stderr)
    try:
        lines = logfile.read_text(errors="replace").splitlines()
        for line in lines[-200:]:
            print(line, file=sys.stderr)
    except OSError:
        pass


def run_capture_json(rt: Runtime, label: str, logfile: Path, args: list[str], output_file: Path) -> Any:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    say(rt, label)
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    logfile.write_bytes(proc.stderr)
    if proc.returncode != 0:
        show_log_failure(label, logfile)
        raise PipelineError(f"Command failed: {label}")
    output_file.write_bytes(proc.stdout)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{label} returned invalid JSON") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        raise SystemExit(0)
    parser = argparse.ArgumentParser(add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("-qa", action="store_true", dest="disable_qa")
    parser.add_argument("-t", action="store_true", dest="transcribe_only")
    parser.add_argument("url", nargs="?")
    parser.add_argument("extra", nargs="*")
    ns = parser.parse_args(argv)
    if ns.extra:
        raise PipelineError(f"Unexpected extra argument: {ns.extra[0]}")
    if not ns.url:
        raise PipelineError(USAGE)
    return ns


def require_deps(transcribe_only: bool) -> None:
    need("yt-dlp")
    need("ffmpeg")
    need("conda")
    if not transcribe_only:
        need("python3")


def metadata_value(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def normalize_video_id(meta: dict[str, Any], url: str) -> str:
    video_id = metadata_value(meta, "id")
    if video_id:
        return video_id
    source = metadata_value(meta, "webpage_url") or url
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", source).strip("_")
    return cleaned[:120] or "video"


def format_chapters(meta: dict[str, Any], include_chapters: bool) -> str:
    if not include_chapters:
        return ""
    chapters = meta.get("chapters") or []
    if not isinstance(chapters, list):
        return ""
    lines = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title") or "Untitled")
        start = chapter.get("start_time") or 0
        end = chapter.get("end_time") or 0
        lines.append(f"- {title} ({start}s-{end}s)")
    return "\n".join(lines)


def join_list(meta: dict[str, Any], key: str) -> str:
    value = meta.get(key) or []
    if not isinstance(value, list):
        return ""
    return ", ".join(str(item) for item in value if item)


def trim(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n...[truncated]"


def build_metadata_block(meta: dict[str, Any], cfg: Config, include_description: bool) -> str:
    lines = [
        "METADATA",
        f"Title: {metadata_value(meta, 'title')}",
        f"Channel: {metadata_value(meta, 'channel', 'uploader')}",
        f"Channel ID: {metadata_value(meta, 'channel_id', 'uploader_id')}",
        f"URL: {metadata_value(meta, 'webpage_url')}",
        f"Upload date: {metadata_value(meta, 'upload_date')}",
        f"Duration (seconds): {metadata_value(meta, 'duration')}",
        f"Language: {metadata_value(meta, 'language')}",
    ]
    if cfg.include_tags:
        lines.append(f"Categories: {join_list(meta, 'categories')}")
        lines.append(f"Tags: {join_list(meta, 'tags')}")
    chapters = format_chapters(meta, cfg.include_chapters)
    if chapters:
        lines.append("Chapters:")
        lines.append(chapters)
    description = metadata_value(meta, "description")
    if include_description and cfg.include_description and description:
        lines.append("Description (creator-provided context only; not transcript evidence):")
        lines.append(trim(description, 1800))
    return "\n".join(lines).strip()


def fetch_metadata(rt: Runtime, url: str) -> dict[str, Any]:
    meta_json = rt.workdir / "meta.json"
    meta_log = rt.logdir / "yt-dlp-meta.log"
    return run_capture_json(
        rt,
        "Fetching video metadata...",
        meta_log,
        [
            "yt-dlp",
            "--no-progress",
            "--dump-single-json",
            "--no-warnings",
            "--restrict-filenames",
            url,
        ],
        meta_json,
    )


def build_or_load_transcript(rt: Runtime, meta: dict[str, Any], url: str) -> Path:
    cfg = rt.config
    all_txt = rt.workdir / "transcript_full.txt"
    video_id = normalize_video_id(meta, url)
    cache_tx_dir = cfg.cache_dir / "transcripts"
    cache_tx_dir.mkdir(parents=True, exist_ok=True)
    cache_txt = cache_tx_dir / f"{video_id}_chunk{cfg.chunk_seconds}.txt"

    if not cfg.refresh_cache and cache_txt.is_file() and cache_txt.stat().st_size > 0:
        say(rt, f"Using cached transcript: {cache_txt}")
        shutil.copyfile(cache_txt, all_txt)
        return all_txt

    dl_log = rt.logdir / "yt-dlp.log"
    run_quiet(
        rt,
        "Downloading + extracting audio (FLAC)...",
        dl_log,
        [
            "yt-dlp",
            "--no-progress",
            "-P",
            f"home:{rt.workdir}",
            "-P",
            f"temp:{rt.workdir}",
            "-x",
            "--audio-format",
            "flac",
            "--audio-quality",
            "0",
            "--restrict-filenames",
            "-o",
            str(rt.workdir / "%(title)s.%(ext)s"),
            url,
        ],
    )

    audio_files = sorted(
        path for path in rt.workdir.glob("*.flac")
        if path.is_file() and not path.name.endswith(".part")
    )
    if not audio_files:
        raise PipelineError(f"Could not find downloaded .flac in {rt.workdir} (see {dl_log})")
    audio_file = audio_files[0]
    say(rt, f"Downloaded: {audio_file.name}")

    chunk_dir = rt.workdir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    split_log = rt.logdir / "ffmpeg-split.log"
    run_quiet(
        rt,
        f"Splitting into {cfg.chunk_seconds}s chunks...",
        split_log,
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(audio_file),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "segment",
            "-segment_time",
            str(cfg.chunk_seconds),
            "-reset_timestamps",
            "1",
            "-c:a",
            "pcm_s16le",
            str(chunk_dir / "seg_%04d.wav"),
        ],
    )

    chunks = sorted(chunk_dir.glob("seg_*.wav"))
    if not chunks:
        raise PipelineError(f"No chunks created (see {split_log})")
    say(rt, f"Chunks: {len(chunks)}")

    say(rt, f"Transcribing chunks with conda env: {cfg.condaenv}")
    tx_dir = rt.workdir / "transcripts"
    tx_dir.mkdir(parents=True, exist_ok=True)
    with all_txt.open("w", encoding="utf-8") as transcript:
        for i, chunk in enumerate(chunks, start=1):
            out_txt = tx_dir / f"seg_{i - 1:04d}.txt"
            tlog = rt.logdir / f"voxmlx_{i - 1:04d}.log"
            say(rt, f"  [{i}/{len(chunks)}] {chunk.name}")
            proc = subprocess.run(
                ["conda", "run", "-n", cfg.condaenv, "voxmlx", "--audio", str(chunk)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out_txt.write_bytes(proc.stdout)
            tlog.write_bytes(proc.stderr)
            if proc.returncode != 0:
                show_log_failure(f"voxmlx failed on {chunk.name}", tlog)
                raise PipelineError(f"voxmlx failed on {chunk.name}")
            transcript.write(f"----- CHUNK {i} / {len(chunks)} : {chunk.name} -----\n")
            transcript.write(proc.stdout.decode("utf-8", errors="replace"))
            transcript.write("\n")

    shutil.copyfile(all_txt, cache_txt)
    say(rt, f"Saved transcript cache: {cache_txt}")
    return all_txt


def parse_transcript(path: Path) -> list[TranscriptChunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = re.compile(r"^----- CHUNK (\d+) / (\d+) : (.*?) -----\s*$", re.MULTILINE)
    matches = list(marker.finditer(text))
    if not matches:
        return [TranscriptChunk(index=1, total=1, name=path.name, text=text.strip())]

    chunks = []
    for pos, match in enumerate(matches):
        start = match.end()
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
        chunk_text = text[start:end].strip()
        chunks.append(
            TranscriptChunk(
                index=int(match.group(1)),
                total=int(match.group(2)),
                name=match.group(3),
                text=chunk_text,
            )
        )
    return chunks


def windows(chunks: list[TranscriptChunk], size: int) -> list[list[TranscriptChunk]]:
    size = max(1, size)
    return [chunks[i:i + size] for i in range(0, len(chunks), size)]


def lm_call(rt: Runtime, name: str, system_prompt: str, user_input: str, max_tokens: int | None = None) -> str:
    cfg = rt.config
    rt.lm_counter += 1
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "call"
    req_json = rt.lm_dir / f"{rt.lm_counter:03d}_{safe_name}.request.json"
    resp_json = rt.lm_dir / f"{rt.lm_counter:03d}_{safe_name}.response.json"
    lm_log = rt.logdir / f"lmstudio_{rt.lm_counter:03d}_{safe_name}.log"

    payload = {
        "model": cfg.lm_model,
        "system_prompt": system_prompt,
        "input": user_input,
        "temperature": cfg.temperature,
        "max_output_tokens": max_tokens or cfg.max_output_tokens,
        "stream": False,
        "store": False,
    }
    req_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://{cfg.lm_host}:{cfg.lm_port}/api/v1/chat",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {cfg.lm_api_token}"} if cfg.lm_api_token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        resp_json.write_bytes(raw)
        lm_log.write_text(str(exc), encoding="utf-8")
        raise PipelineError(f"LM Studio request failed for {name}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        lm_log.write_text(str(exc), encoding="utf-8")
        raise PipelineError(f"LM Studio request failed for {name}: {exc.reason}") from exc

    resp_json.write_bytes(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"LM Studio response for {name} was not JSON; see {resp_json}") from exc

    output = parsed.get("output") or []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    message = parsed.get("error", {}).get("message") or parsed.get("message") or parsed.get("detail")
    if message:
        raise PipelineError(f"LM Studio returned an error for {name}: {message}")
    raise PipelineError(f"LM Studio response for {name} had no output message; see {resp_json}")


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_jsonish(text: str) -> Any:
    stripped = strip_json_fence(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    starts = [idx for idx in (stripped.find("{"), stripped.find("[")) if idx != -1]
    if not starts:
        raise ValueError("No JSON object or array found in model output")
    start = min(starts)
    end_obj = stripped.rfind("}")
    end_arr = stripped.rfind("]")
    end = max(end_obj, end_arr)
    if end <= start:
        raise ValueError("No complete JSON object or array found in model output")
    return json.loads(stripped[start:end + 1])


def safe_json_from_model(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = parse_jsonish(text)
        if isinstance(parsed, dict):
            return parsed
        return {**fallback, "items": parsed}
    except Exception:
        return {**fallback, "raw_model_output": text.strip()}


EXTRACT_SYSTEM = """You are a precise transcript fact extractor for a small-model summary pipeline.

Rules:
- Output valid JSON only.
- Use the transcript window as evidence. Metadata is context only and is not evidence.
- Extract concrete, checkable details rather than broad summaries.
- Preserve exact names, commands, dates, numbers, prices, dimensions, specs, examples, comparisons, tests, outcomes, recommendations, and stated limitations.
- Prefer useful details over category labels. Do not write sentences about absent categories.
- Do not infer beyond the transcript. If wording is unclear, put it in uncertainties.
- If the transcript mentions speculation, mark it as a claim rather than a demonstrated result.
"""


PROFILE_SYSTEM = """You classify video notes for a summary pipeline.

Rules:
- Output valid JSON only.
- Choose the primary type that best fits the evidence.
- Do not add facts. Only classify and identify what the final summary must emphasize.
"""


FINAL_SUMMARY_SYSTEM = """You are an expert content summarizer.

Goal:
Create a detailed, evidence-based summary that makes the video mostly unnecessary to watch.

Rules:
- Output only the summary text.
- Use only the provided evidence brief and profile. Metadata is context only, not evidence.
- Do not invent details. Omit unsupported claims.
- Write a detailed summary, not a short abstract. For a normal video, aim for 2-4 substantial paragraphs or 8-12 detailed bullets, depending on what fits the content.
- Include concrete details when they materially help: names, numbers, dates, settings, steps, examples, tests, results, recommendations, caveats, or quote-like paraphrases.
- Include multiple concrete details across the summary when the evidence contains them; do not collapse the video into generic topic labels.
- Do not mention categories of information that are absent.
- Do not reveal internal workflow labels, prompt labels, schema field names, or reasoning scaffolds.
- Do not add Pricing, Caveats, Policy, or similar sections just because the prompt mentions those concepts.
- Do not give generic advice, warnings, buying advice, safety advice, legal advice, update advice, or best-practice recommendations unless the transcript evidence explicitly says the speaker recommended it.
- Avoid generic phrases such as "talks about", "covers", or "explores" unless immediately followed by exactly what was said or demonstrated.
- Distinguish demonstrated outcomes from potential risks or speculation.
- If the transcript is incomplete or noisy, briefly say what is unclear and summarize only what is supported.
- Adapt the summary to the video: for reviews include items and verdicts; for tutorials include steps and results; for comparisons include metrics and tradeoffs; for explainers include the central idea and how it works.
"""


QA_SYSTEM = """You are a precise Q&A assistant for a single YouTube video.

Rules:
- Use only the provided summary, evidence brief, and transcript excerpts.
- Answer only the current question.
- Include at least one concrete detail when available.
- If the detail is not explicitly in context, say so briefly.
- Do not invent details or speculate.
- Distinguish demonstrated outcomes from potential risks.
- For follow-ups, use the prior Q&A only to resolve references; do not recap unless asked.
- If the current question asks for a new distinction or condition, answer that distinction directly instead of repeating the prior answer.
- Do not reveal internal workflow labels, prompt labels, schema field names, or reasoning scaffolds.
- Do not mention absent categories such as missing pricing unless the user specifically asks whether that category was mentioned.
"""


def extract_window_notes(rt: Runtime, meta: dict[str, Any], chunks: list[TranscriptChunk]) -> dict[str, Any]:
    start = chunks[0].index
    end = chunks[-1].index
    transcript = "\n\n".join(
        f"CHUNK {chunk.index}/{chunk.total} ({chunk.name})\n{chunk.text}"
        for chunk in chunks
    )
    user_input = f"""{build_metadata_block(meta, rt.config, include_description=False)}

TRANSCRIPT WINDOW
Chunks: {start}-{end}
{trim(transcript, 14000)}

Return JSON with this schema:
{{
  "window": {{"start_chunk": {start}, "end_chunk": {end}}},
  "headline": "",
  "main_points": [],
  "important_entities": [],
  "specific_details": [
    {{"fact": "", "support": "chunk number or phrase", "confidence": "high|medium|low"}}
  ],
  "numbers_dates_prices_specs": [],
  "steps_or_sequence": [],
  "tests_or_examples": [],
  "observed_results": [],
  "opinions_or_recommendations": [],
  "claims_vs_evidence": [],
  "uncertainties": []
}}"""
    output = lm_call(rt, f"extract_{start}_{end}", EXTRACT_SYSTEM, user_input, max_tokens=1200)
    fallback = {
        "window": {"start_chunk": start, "end_chunk": end},
        "headline": "",
        "main_points": [],
        "important_entities": [],
        "specific_details": [],
        "numbers_dates_prices_specs": [],
        "steps_or_sequence": [],
        "tests_or_examples": [],
        "observed_results": [],
        "opinions_or_recommendations": [],
        "claims_vs_evidence": [],
        "uncertainties": ["Model did not return valid structured JSON for this window."],
    }
    return safe_json_from_model(output, fallback)


def value_to_phrases(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        phrases = []
        for item in value:
            phrases.extend(value_to_phrases(item))
        return phrases
    if isinstance(value, dict):
        if isinstance(value.get("fact"), str):
            phrase = value["fact"].strip()
            support = value.get("support")
            if support:
                phrase = f"{phrase} ({support})"
            return [phrase] if phrase else []
        if isinstance(value.get("plain_explanation"), str):
            return [value["plain_explanation"].strip()]
        if isinstance(value.get("claim"), str):
            claim = value["claim"].strip()
            evidence = value.get("evidence")
            if evidence:
                claim = f"{claim} ({evidence})"
            return [claim] if claim else []
        if isinstance(value.get("chain"), list):
            parts = [str(part).strip() for part in value["chain"] if str(part).strip()]
            support = value.get("support")
            phrase = "; ".join(parts)
            if support:
                phrase = f"{phrase} ({support})"
            return [phrase] if phrase else []
        phrases = []
        for key in ("name", "title", "item", "step", "result", "outcome", "detail", "description", "verdict"):
            if isinstance(value.get(key), str) and value[key].strip():
                phrases.append(value[key].strip())
        if phrases:
            return phrases
        return [json.dumps(value, ensure_ascii=False)]
    return [str(value)]


def add_brief_lines(lines: list[str], heading: str, value: Any, limit: int = 8) -> None:
    phrases = []
    seen = set()
    for phrase in value_to_phrases(value):
        phrase = re.sub(r"\s+", " ", phrase).strip()
        if not phrase or phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        phrases.append(phrase)
    if not phrases:
        return
    lines.append(f"{heading}:")
    for phrase in phrases[:limit]:
        lines.append(f"- {phrase}")


def notes_to_evidence_brief(notes: list[dict[str, Any]], max_chars: int = 28000) -> str:
    lines = []
    for note in notes:
        window = note.get("window") if isinstance(note.get("window"), dict) else {}
        start = window.get("start_chunk", "?")
        end = window.get("end_chunk", "?")
        lines.append(f"From chunks {start}-{end}:")
        headline = note.get("headline")
        if isinstance(headline, str) and headline.strip():
            lines.append(f"- {headline.strip()}")
        add_brief_lines(lines, "Main points", note.get("main_points") or note.get("topics"), limit=6)
        add_brief_lines(lines, "People, products, tools, places, or terms", note.get("important_entities") or note.get("entities"), limit=12)
        add_brief_lines(lines, "Concrete facts", note.get("specific_details") or note.get("concrete_facts"), limit=12)
        add_brief_lines(lines, "Numbers, dates, prices, settings, or specs", note.get("numbers_dates_prices_specs") or note.get("numbers_specs_prices"), limit=10)
        add_brief_lines(lines, "Order of events or steps", note.get("steps_or_sequence") or note.get("mechanisms"), limit=10)
        add_brief_lines(lines, "Examples, tests, or demonstrations", note.get("tests_or_examples") or note.get("tests_demos"), limit=10)
        add_brief_lines(lines, "Results", note.get("observed_results") or note.get("outcomes"), limit=10)
        add_brief_lines(lines, "Opinions, judgments, or recommendations", note.get("opinions_or_recommendations") or note.get("opinions_verdicts"), limit=8)
        add_brief_lines(lines, "Claims, limitations, or uncertainty", note.get("claims_vs_evidence") or note.get("uncertainties") or note.get("caveats_uncertainty"), limit=8)
        add_brief_lines(lines, "Other useful details", note.get("good_summary_points") or note.get("raw_model_output"), limit=6)
        lines.append("")
    return trim("\n".join(lines), max_chars)


def notes_to_compact_text(notes: list[dict[str, Any]], max_chars: int = 28000) -> str:
    return notes_to_evidence_brief(notes, max_chars=max_chars)


def classify_video(rt: Runtime, meta: dict[str, Any], notes: list[dict[str, Any]]) -> dict[str, Any]:
    user_input = f"""{build_metadata_block(meta, rt.config, include_description=True)}

EVIDENCE BRIEF
{notes_to_evidence_brief(notes, max_chars=22000)}

Return JSON:
{{
  "primary_type": "security|product_review|tutorial|news_commentary|interview|list_roundup|benchmark_comparison|lecture_explainer|other",
  "secondary_types": [],
  "summary_focus": [],
  "required_sections": [],
  "avoid_sections": [],
  "key_entities": [],
  "likely_user_questions": [],
  "uncertainty_warnings": []
}}"""
    output = lm_call(rt, "classify_video", PROFILE_SYSTEM, user_input, max_tokens=900)
    fallback = {
        "primary_type": "other",
        "secondary_types": [],
        "summary_focus": [],
        "required_sections": [],
        "avoid_sections": [],
        "key_entities": [],
        "likely_user_questions": [],
        "uncertainty_warnings": [],
    }
    return safe_json_from_model(output, fallback)


def profile_to_brief(profile: dict[str, Any]) -> str:
    lines = []
    primary = profile.get("primary_type")
    if isinstance(primary, str) and primary.strip():
        lines.append(f"Likely video style: {primary.strip().replace('_', ' ')}")
    add_brief_lines(lines, "Useful emphasis", profile.get("summary_focus"), limit=8)
    add_brief_lines(lines, "Important names or terms", profile.get("key_entities"), limit=12)
    add_brief_lines(lines, "Possible uncertainty to handle carefully", profile.get("uncertainty_warnings"), limit=6)
    return "\n".join(lines).strip() or "Likely video style: general explainer"


def generate_summary(rt: Runtime, meta: dict[str, Any], notes: list[dict[str, Any]], profile: dict[str, Any]) -> str:
    user_input = f"""{build_metadata_block(meta, rt.config, include_description=True)}

VIDEO PROFILE
{profile_to_brief(profile)}

EVIDENCE BRIEF
{notes_to_evidence_brief(notes)}

Write the final summary now."""
    summary = lm_call(rt, "final_summary", FINAL_SUMMARY_SYSTEM, user_input, max_tokens=rt.config.max_output_tokens)
    return clean_model_prose(summary)


def cache_pipeline_paths(rt: Runtime, meta: dict[str, Any], url: str) -> dict[str, Path]:
    video_id = normalize_video_id(meta, url)
    base = rt.config.cache_dir / "pipeline" / f"{video_id}_chunk{rt.config.chunk_seconds}_win{rt.config.pipeline_window_chunks}_{CACHE_SCHEMA_VERSION}"
    base.mkdir(parents=True, exist_ok=True)
    return {
        "dir": base,
        "notes": base / "chunk_notes.jsonl",
        "profile": base / "profile.json",
        "summary": base / "summary.txt",
    }


def load_cached_pipeline(rt: Runtime, paths: dict[str, Path]) -> tuple[list[dict[str, Any]], dict[str, Any], str] | None:
    if rt.config.refresh_cache:
        return None
    if not (paths["notes"].is_file() and paths["profile"].is_file() and paths["summary"].is_file()):
        return None
    notes = []
    for line in paths["notes"].read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            notes.append(json.loads(line))
    profile = json.loads(paths["profile"].read_text(encoding="utf-8"))
    summary = paths["summary"].read_text(encoding="utf-8", errors="replace").strip()
    if notes and summary:
        return notes, profile, summary
    return None


def save_pipeline_cache(paths: dict[str, Path], notes: list[dict[str, Any]], profile: dict[str, Any], summary: str) -> None:
    paths["notes"].write_text(
        "\n".join(json.dumps(note, ensure_ascii=False) for note in notes) + "\n",
        encoding="utf-8",
    )
    paths["profile"].write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["summary"].write_text(summary.strip() + "\n", encoding="utf-8")


def build_pipeline(rt: Runtime, meta: dict[str, Any], url: str, transcript_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    paths = cache_pipeline_paths(rt, meta, url)
    cached = load_cached_pipeline(rt, paths)
    if cached:
        say(rt, f"Using cached pipeline notes: {paths['dir']}")
        return cached

    chunks = parse_transcript(transcript_path)
    if not chunks or not chunks[0].text:
        raise PipelineError("Transcript is empty.")

    say(rt, f"Extracting transcript evidence with {rt.config.lm_model}")
    notes = []
    for group in windows(chunks, rt.config.pipeline_window_chunks):
        start = group[0].index
        end = group[-1].index
        say(rt, f"  evidence window chunks {start}-{end}")
        notes.append(extract_window_notes(rt, meta, group))

    say(rt, "Classifying video structure...")
    profile = classify_video(rt, meta, notes)

    say(rt, "Writing final summary from compact evidence...")
    summary = generate_summary(rt, meta, notes, profile)
    save_pipeline_cache(paths, notes, profile, summary)
    say(rt, f"Saved pipeline cache: {paths['dir']}")
    return notes, profile, summary


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[A-Za-z0-9_.'-]{3,}", text.lower()):
        if token in STOPWORDS:
            continue
        tokens.append(token)
        if token.endswith("s") and len(token) > 4:
            tokens.append(token[:-1])
    return tokens


def question_type(question: str) -> str:
    q = question.strip().lower()
    if re.search(r"\b(list|which|what items|all items|products|tools|names)\b", q):
        return "list"
    if re.search(r"\b(need|needs|required|requirement|condition|conditions|look like|lookalike|for .* to occur|to happen|trigger|valid|validity|sufficient|enough)\b", q):
        return "requirements"
    if re.search(r"\b(how exactly|how did|why|explain|mechanism|root cause|occur)\b", q):
        return "mechanism"
    if re.match(r"^(is|are|was|were|do|does|did|can|could|would|should|has|have|will)\b", q):
        return "yes_no"
    if re.search(r"\b(compare|versus|vs\.?|difference|better|worse)\b", q):
        return "comparison"
    return "direct"


def score_text(tokens: list[str], text: str) -> int:
    lower = text.lower()
    return sum(lower.count(token) for token in tokens)


def note_search_text(note: dict[str, Any], qtype: str) -> str:
    if qtype == "requirements":
        priority = {
            "specific_details": note.get("specific_details") or note.get("concrete_facts"),
            "steps_or_sequence": note.get("steps_or_sequence") or note.get("mechanisms"),
            "tests_or_examples": note.get("tests_or_examples") or note.get("tests_demos"),
            "claims_vs_evidence": note.get("claims_vs_evidence"),
            "uncertainties": note.get("uncertainties") or note.get("caveats_uncertainty"),
        }
        return json.dumps(priority, ensure_ascii=False) + "\n" + json.dumps(note, ensure_ascii=False)
    return json.dumps(note, ensure_ascii=False)


def retrieval_tokens(question: str, qtype: str) -> list[str]:
    tokens = tokenize(question)
    if qtype == "requirements":
        tokens.extend([
            "need", "needs", "required", "requirement", "condition", "conditions",
            "valid", "invalid", "sufficient", "enough", "trigger", "occur",
            "happen", "failed", "fails", "rejected", "reject", "attempt",
            "structure", "structured", "format", "must", "only", "unless",
        ])
    return tokens


def retrieve_context(
    question: str,
    notes: list[dict[str, Any]],
    transcript_chunks: list[TranscriptChunk],
    cfg: Config,
) -> tuple[list[dict[str, Any]], list[TranscriptChunk]]:
    qtype = question_type(question)
    lower_question = question.lower()
    broad_list_request = (
        bool(re.search(r"\ball\b", lower_question))
        or bool(re.search(r"\blist\b.*\b(items|products|tools)\b", lower_question))
        or bool(re.search(r"\bwhat\b.*\b(items|products|tools)\b.*\btested\b", lower_question))
        or bool(re.search(r"\bwhich\b.*\b(items|products|tools)\b.*\btested\b", lower_question))
    )
    if qtype == "list" and broad_list_request:
        return notes, transcript_chunks[: cfg.qa_retrieval_chunks]

    tokens = retrieval_tokens(question, qtype)
    if not tokens:
        return notes[: cfg.qa_retrieval_notes], transcript_chunks[: cfg.qa_retrieval_chunks]

    note_scores = []
    for idx, note in enumerate(notes):
        text = note_search_text(note, qtype)
        note_scores.append((score_text(tokens, text), idx, note))
    note_scores.sort(key=lambda item: (-item[0], item[1]))
    selected_notes = [item[2] for item in note_scores[: cfg.qa_retrieval_notes] if item[0] > 0]
    if not selected_notes:
        selected_notes = notes[: min(len(notes), cfg.qa_retrieval_notes)]

    chunk_scores = []
    for chunk in transcript_chunks:
        score = score_text(tokens, chunk.text)
        if qtype == "requirements" and re.search(
            r"\b(need|required|condition|valid|invalid|sufficient|enough|trigger|failed|rejected|attempt|must|unless|only)\b",
            chunk.text,
            flags=re.IGNORECASE,
        ):
            score += 3
        chunk_scores.append((score, chunk.index, chunk))
    chunk_scores.sort(key=lambda item: (-item[0], item[1]))
    selected_chunks = [item[2] for item in chunk_scores[: cfg.qa_retrieval_chunks] if item[0] > 0]
    if not selected_chunks:
        selected_chunks = transcript_chunks[: min(len(transcript_chunks), cfg.qa_retrieval_chunks)]
    return selected_notes, selected_chunks


def qa_style_instruction(qtype: str) -> str:
    if qtype == "list":
        return "The user wants a list. Do not start with Yes/No. Use concise bullets; include a short description or result for each item when available."
    if qtype == "mechanism":
        return "The user is asking how something works. Explain it in normal language from cause to effect, using the most relevant concrete details."
    if qtype == "requirements":
        return "The user is asking about requirements or conditions. Focus on what must be true, what is not sufficient by itself, and any failed attempts or constraints mentioned in the transcript."
    if qtype == "yes_no":
        return "The user asked a yes/no-style question. Start with Yes, No, or Not clear, then give the concrete reason."
    if qtype == "comparison":
        return "The user wants a comparison. Compare the relevant entities directly and mention the deciding detail."
    return "Answer directly without a generic recap."


def clean_model_prose(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(
        r"(?is)\bThe intermediate state involves\s+([^.!?]+?),\s+and\s+the observed result is\s+([^.!?]+)[.!?]",
        r"This leads to \1, with \2.",
        cleaned,
    )
    cleaned = re.sub(
        r"(?is)\bThe observed result is\s+([^.!?]+)[.!?]",
        r"This results in \1.",
        cleaned,
    )
    # Drop process-reporting sentences that is sometimes copied from prompts
    sentence_patterns = [
        r"(?is)(?:^|(?<=[.!?])\s+)No\s+(?:pricing|price|policy|sponsor|discount|promo|promotion|caveat|specification|spec|benchmark|test)\s+[^.!?]*(?:included|mentioned|provided|found|available)[^.!?]*[.!?]",
        r"(?is)(?:^|(?<=[.!?])\s+)The\s+evidence\s+(?:does\s+not|doesn't|did\s+not|didn't)\s+[^.!?]*[.!?]",
        r"(?is)(?:^|(?<=[.!?])\s+)There\s+(?:is|are|was|were)\s+no\s+[^.!?]*(?:in\s+the\s+evidence|included\s+in\s+the\s+evidence|mentioned\s+in\s+the\s+evidence)[^.!?]*[.!?]",
    ]
    for pattern in sentence_patterns:
        cleaned = re.sub(pattern, " ", cleaned)

    replacements = {
        "The intermediate state involves ": "",
        "the intermediate state involves ": "",
        "The observed result is ": "",
        "the observed result is ": "",
        "The evidence notes indicate that ": "",
        "The evidence brief indicates that ": "",
        "The evidence says that ": "",
        "According to the evidence notes, ": "",
        "According to the evidence brief, ": "",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)

    cleaned = re.sub(r"\b(?:Question type|Evidence notes|Evidence brief|Specific details):\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def answer_question(
    rt: Runtime,
    meta: dict[str, Any],
    summary: str,
    notes: list[dict[str, Any]],
    transcript_chunks: list[TranscriptChunk],
    history: list[tuple[str, str]],
    question: str,
) -> str:
    selected_notes, selected_chunks = retrieve_context(question, notes, transcript_chunks, rt.config)
    qtype = question_type(question)
    history_text = "\n".join(
        f"Previous user question: {q}\nPrevious answer: {a}"
        for q, a in history[-4:]
    ).strip()
    transcript_excerpt = "\n\n".join(
        f"CHUNK {chunk.index}/{chunk.total}\n{trim(chunk.text, 1800)}"
        for chunk in selected_chunks
    )
    user_input = f"""{build_metadata_block(meta, rt.config, include_description=False)}

FINAL SUMMARY
{trim(summary, 5000)}

RELEVANT EVIDENCE BRIEF
{notes_to_evidence_brief(selected_notes, max_chars=16000)}

RELEVANT TRANSCRIPT EXCERPTS
{trim(transcript_excerpt, 9000)}

PRIOR Q&A
{history_text if history_text else "(none)"}

{qa_style_instruction(qtype)}
{"Do not repeat the previous answer. The user is asking a follow-up, so answer the new distinction directly." if history_text else ""}

Current user question: {question}
"""
    answer = lm_call(rt, "qa", QA_SYSTEM, user_input, max_tokens=min(rt.config.max_output_tokens, 900))
    if qtype != "yes_no":
        answer = re.sub(r"^\s*(yes|no),?\s+", "", answer, flags=re.IGNORECASE)
    return clean_model_prose(answer)


def run_qa_loop(
    rt: Runtime,
    meta: dict[str, Any],
    transcript_path: Path,
    notes: list[dict[str, Any]],
    summary: str,
) -> None:
    chunks = parse_transcript(transcript_path)
    say(rt, "Q&A mode enabled. Ask questions about the video.")
    say(rt, "Press Enter on an empty line, or type 'exit'/'quit' to stop.")
    history: list[tuple[str, str]] = []
    while True:
        try:
            question = input("Q> ")
        except EOFError:
            break
        if not question.strip():
            break
        if question.strip().lower() in {"exit", "quit", "/quit"}:
            break
        answer = answer_question(rt, meta, summary, notes, chunks, history, question)
        print(f"\n{answer}\n", flush=True)
        history.append((question, answer))


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except PipelineError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    cfg = load_config()
    if not args.transcribe_only and not cfg.lm_api_token:
        print("\nERROR: LM Studio API token is not set. Export LM_API_TOKEN before running summarize.sh.", file=sys.stderr)
        return 1

    try:
        require_deps(args.transcribe_only)
    except PipelineError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="ytsum."))
    rt = Runtime(
        config=cfg,
        workdir=workdir,
        logdir=workdir / "logs",
        lm_dir=workdir / "lm",
        transcribe_only=args.transcribe_only,
    )
    rt.logdir.mkdir(parents=True, exist_ok=True)
    rt.lm_dir.mkdir(parents=True, exist_ok=True)

    success = False
    try:
        say(rt, f"Workdir: {workdir}")
        meta = fetch_metadata(rt, args.url)
        transcript_path = build_or_load_transcript(rt, meta, args.url)

        if args.transcribe_only:
            sys.stdout.write(transcript_path.read_text(encoding="utf-8", errors="replace"))
            success = True
            return 0

        notes, _profile, summary = build_pipeline(rt, meta, args.url, transcript_path)
        print(f"\n\n{summary.strip()}\n", flush=True)

        if not args.disable_qa:
            run_qa_loop(rt, meta, transcript_path, notes, summary)

        success = True
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except PipelineError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if success:
            if cfg.keep_workdir:
                say(rt, f"Keeping workdir: {workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)
        else:
            print("\n[!] Script failed. Keeping workdir for debugging:", file=sys.stderr)
            print(f"    {workdir}", file=sys.stderr)
            print(f"    LM calls: {rt.lm_dir}", file=sys.stderr)
            print(f"    Logs:     {rt.logdir}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
