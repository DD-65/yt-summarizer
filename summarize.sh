#!/usr/bin/env bash
set -euo pipefail

# ---- config (override via env) ----
LM_HOST="${LM_HOST:-localhost}"
LM_PORT="${LM_PORT:-5432}"
LM_MODEL="${LM_MODEL:-liquid/lfm2.5-1.2b}"
CHUNK_SECONDS="${CHUNK_SECONDS:-60}"
MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-2000}"
TEMPERATURE="${TEMPERATURE:-0.2}"
KEEP_WORKDIR="${KEEP_WORKDIR:-0}" # set to 1 to keep temp files always
LM_API_TOKEN="${LM_API_TOKEN:-}"  # optional

# Metadata controls (optional)
INCLUDE_DESCRIPTION="${INCLUDE_DESCRIPTION:-1}"  # 1 = include video description in prompt
INCLUDE_TAGS="${INCLUDE_TAGS:-1}"               # 1 = include tags/categories in prompt
INCLUDE_CHAPTERS="${INCLUDE_CHAPTERS:-1}"       # 1 = include chapters (if present) in prompt

# ---- helpers ----
say() { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*"; }

die() {
  # %b interprets \n etc
  printf "\nERROR: %b\n" "$*" >&2
  exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || die "Missing dependency: $1"; }

run_quiet() {
  local label="$1"; shift
  local logfile="$1"; shift
  mkdir -p "$(dirname "$logfile")"
  say "$label"
  if ! "$@" >"$logfile" 2>&1; then
    printf "\n--- command failed: %s ---\nLog: %s\n\n" "$label" "$logfile" >&2
    tail -n 200 "$logfile" >&2 || true
    exit 1
  fi
}

# ---- args ----
URL="${1:-}"
[[ -n "$URL" ]] || die $'Usage: ./summarize.sh "https://www.youtube.com/watch?v=..."\n\nOptional env:\n  LM_HOST=localhost LM_PORT=5432 LM_MODEL=liquid/lfm2.5-1.2b\n  CHUNK_SECONDS=60 KEEP_WORKDIR=0\n  LM_API_TOKEN=... (if your LM Studio server requires auth)\n\nMetadata env:\n  INCLUDE_DESCRIPTION=1 INCLUDE_TAGS=1 INCLUDE_CHAPTERS=1'

# ---- deps ----
need yt-dlp
need ffmpeg
need curl
need jq
need conda

# ---- workspace ----
WORKDIR="$(mktemp -d -t ytsum.XXXXXXXX)"
LOGDIR="$WORKDIR/logs"
mkdir -p "$LOGDIR"

cleanup() {
  status=$?
  if [[ "$status" -ne 0 ]]; then
    # Always keep workdir on error, so you can inspect response/logs
    printf "\n[!] Script failed. Keeping workdir for debugging:\n    %s\n" "$WORKDIR" >&2
    printf "    LM response: %s\n" "$WORKDIR/response.json" >&2
    printf "    Logs:        %s\n" "$WORKDIR/logs/\n" >&2
    exit "$status"
  fi

  if [[ "$KEEP_WORKDIR" == "1" ]]; then
    say "Keeping workdir: $WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

say "Workdir: $WORKDIR"

# ---- NEW: fetch metadata up front ----
META_JSON="$WORKDIR/meta.json"
META_LOG="$LOGDIR/yt-dlp-meta.log"

run_quiet "Fetching video metadata..." "$META_LOG" \
  yt-dlp \
    --no-progress \
    --dump-single-json \
    --no-warnings \
    --restrict-filenames \
    "$URL"

# Note: run_quiet redirects stdout to log, so we need to write JSON to file explicitly.
# Do a second call that writes to file (still quiet):
run_quiet "Saving metadata JSON..." "$META_LOG" \
  bash -c "yt-dlp --no-progress --dump-single-json --no-warnings --restrict-filenames \"$URL\" >\"$META_JSON\""

# ---- extract metadata fields ----
TITLE="$(jq -r '.title // empty' "$META_JSON")"
CHANNEL="$(jq -r '.channel // .uploader // empty' "$META_JSON")"
CHANNEL_ID="$(jq -r '.channel_id // .uploader_id // empty' "$META_JSON")"
WEBPAGE_URL="$(jq -r '.webpage_url // empty' "$META_JSON")"
UPLOAD_DATE="$(jq -r '.upload_date // empty' "$META_JSON")"        # YYYYMMDD
DURATION="$(jq -r '.duration // empty' "$META_JSON")"              # seconds
LANGUAGE="$(jq -r '.language // empty' "$META_JSON")"
DESCRIPTION="$(jq -r '.description // empty' "$META_JSON")"
TAGS_CSV="$(jq -r '(.tags // []) | join(", ")' "$META_JSON")"
CATEGORIES_CSV="$(jq -r '(.categories // []) | join(", ")' "$META_JSON")"

# Chapters (optional)
CHAPTERS=""
if [[ "$INCLUDE_CHAPTERS" == "1" ]]; then
  CHAPTERS="$(jq -r '
    if (.chapters // []) | length > 0 then
      (.chapters
        | map("- " + (.title // "Untitled") + " (" + ((.start_time // 0)|tostring) + "s–" + ((.end_time // 0)|tostring) + "s)")
        | join("\n"))
    else
      ""
    end
  ' "$META_JSON")"
fi

# ---- download audio as FLAC (force into WORKDIR) ----
DL_LOG="$LOGDIR/yt-dlp.log"

run_quiet "Downloading + extracting audio (FLAC)..." "$DL_LOG" \
  yt-dlp \
    --no-progress \
    -P "home:$WORKDIR" -P "temp:$WORKDIR" \
    -x --audio-format flac --audio-quality 0 \
    --restrict-filenames \
    -o "$WORKDIR/%(title)s.%(ext)s" \
    "$URL"

AUDIO_FILE="$(find "$WORKDIR" -maxdepth 1 -type f -name '*.flac' ! -name '*.part' -print | head -n 1 || true)"
[[ -n "${AUDIO_FILE:-}" ]] || die "Could not find downloaded .flac in $WORKDIR (see $DL_LOG)"
say "Downloaded: $(basename "$AUDIO_FILE")"

# ---- split into 1-minute segments ----
CHUNK_DIR="$WORKDIR/chunks"
mkdir -p "$CHUNK_DIR"
SPLIT_LOG="$LOGDIR/ffmpeg-split.log"

# ---- split into chunks as WAV PCM (most compatible) ----
run_quiet "Splitting into ${CHUNK_SECONDS}s chunks..." "$SPLIT_LOG" \
  ffmpeg -hide_banner -nostdin -y -i "$AUDIO_FILE" \
    -ar 16000 -ac 1 \
    -f segment -segment_time "$CHUNK_SECONDS" -reset_timestamps 1 \
    -c:a pcm_s16le \
    "$CHUNK_DIR/seg_%04d.wav"

CHUNK_LIST="$WORKDIR/chunk_list.txt"
ls -1 "$CHUNK_DIR"/seg_*.wav 2>/dev/null >"$CHUNK_LIST" || true
NUM_CHUNKS="$(wc -l <"$CHUNK_LIST" | tr -d ' ')"
[[ "$NUM_CHUNKS" -gt 0 ]] || die "No chunks created (see $SPLIT_LOG)"
say "Chunks: $NUM_CHUNKS"

# ---- activate conda env + transcribe ----
say "Activating conda env: voxmlx"
CONDA_BASE="$(conda info --base 2>/dev/null || true)"
[[ -n "${CONDA_BASE:-}" && -f "$CONDA_BASE/etc/profile.d/conda.sh" ]] || die "Cannot locate conda.sh (conda info --base failed?)"
# shellcheck disable=SC1090
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate voxmlx >/dev/null 2>&1 || die "Failed: conda activate voxmlx"

need voxmlx

TX_DIR="$WORKDIR/transcripts"
mkdir -p "$TX_DIR"
ALL_TXT="$WORKDIR/transcript_full.txt"
: >"$ALL_TXT"

say "Transcribing chunks with voxmlx..."
i=0
while IFS= read -r chunk; do
  i=$((i+1))
  out_txt="$TX_DIR/seg_$(printf "%04d" $((i-1))).txt"
  tlog="$LOGDIR/voxmlx_$(printf "%04d" $((i-1))).log"

  say "  [$i/$NUM_CHUNKS] $(basename "$chunk")"
  if ! voxmlx --audio "$chunk" >"$out_txt" 2>"$tlog"; then
    printf "\n--- voxmlx failed on %s ---\nLog: %s\n\n" "$(basename "$chunk")" "$tlog" >&2
    tail -n 200 "$tlog" >&2 || true
    exit 1
  fi

  {
    echo "----- CHUNK $i / $NUM_CHUNKS : $(basename "$chunk") -----"
    cat "$out_txt"
    echo
  } >>"$ALL_TXT"
done <"$CHUNK_LIST"

# ---- summarize with LM Studio REST API v1 ----
say "Summarizing with LM Studio (model: ${LM_MODEL})"

PROMPT_SYSTEM=$'You are an expert content summarizer.\n\nGoal:\nCreate a detailed, evidence-based summary that makes the video unnecessary to watch.\n\nRules:\n- Output ONLY the summary text (no preamble, no labels, no markdown symbols).\n- Do not invent details. Use METADATA only for context.\n\nRequired content:\n- A concise 2–3 sentence overview.\n- Then list 8–15 key points, each with at least one concrete supporting detail from the transcript (a specific example, number, comparison, test outcome, policy detail, or quote-like paraphrase).\n- Include pricing/plan terms/specs if mentioned.\n- Include what was tested or demonstrated and what the observed outcome was.\n- Include the final conclusion and any caveats.\n\nAnti-vagueness constraint:\n- Do not use generic phrases (“talks about”, “covers”, “explores”) unless you immediately specify what exactly was said/done and what happened.\n\nIf transcript is incomplete or noisy, briefly note that and only summarize what is clear.\n\nYou will receive:\n1) METADATA\n2) TRANSCRIPT\n\nWrite the summary now.'
PROMPT_SYSTEM2=$'You are an expert content summarizer.\n\nRules:\n- Output ONLY the summary text (no preamble, no labels, no formatting markers).\n- Write in clear, neutral, informative prose suitable for a YouTube video description or article abstract.\n- Use the provided METADATA only as context (e.g., correct title/channel naming, topic framing, disambiguation).\n- Do NOT treat metadata as transcript content.\n- Do NOT invent details; if something is not supported by the transcript (or clearly by the description), omit it.\n- Capture the main topic, key arguments, examples, and conclusions presented in the video.\n- Adapt the length of the summary to the length and density of the transcript.\n- Preserve the logical flow of the video.\n- Avoid filler, repetition, timestamps, speaker names, or meta commentary.\n- If transcript quality is poor or incomplete, briefly note that and summarize only what is clear.\n\nYou will receive:\n1) METADATA (title/channel/date/duration/description/tags/chapters/etc.)\n2) TRANSCRIPT (chunked)\n\nWrite a high-quality summary that allows someone to understand the full content without watching the video.'

# Build user prompt with a metadata header
META_BLOCK="METADATA
Title: ${TITLE}
Channel: ${CHANNEL}
Channel ID: ${CHANNEL_ID}
URL: ${WEBPAGE_URL:-$URL}
Upload date: ${UPLOAD_DATE}
Duration (seconds): ${DURATION}
Language: ${LANGUAGE}
"

if [[ "$INCLUDE_TAGS" == "1" ]]; then
  META_BLOCK="${META_BLOCK}Categories: ${CATEGORIES_CSV}
Tags: ${TAGS_CSV}
"
fi

if [[ "$INCLUDE_CHAPTERS" == "1" && -n "${CHAPTERS:-}" ]]; then
  META_BLOCK="${META_BLOCK}Chapters:
${CHAPTERS}

"
fi

if [[ "$INCLUDE_DESCRIPTION" == "1" && -n "${DESCRIPTION:-}" ]]; then
  META_BLOCK="${META_BLOCK}Description:
${DESCRIPTION}

"
fi

PROMPT_USER="$(cat <<EOF
${META_BLOCK}
TRANSCRIPT
$(cat "$ALL_TXT")
EOF
)"

REQ_JSON="$WORKDIR/request.json"
RESP_JSON="$WORKDIR/response.json"
LM_LOG="$LOGDIR/lmstudio.log"

# Use system_prompt + input as a single string
jq -n \
  --arg model "$LM_MODEL" \
  --arg system_prompt "$PROMPT_SYSTEM" \
  --arg input "$PROMPT_USER" \
  --argjson temperature "$TEMPERATURE" \
  --argjson max_output_tokens "$MAX_OUTPUT_TOKENS" \
  '{
    model: $model,
    system_prompt: $system_prompt,
    input: $input,
    temperature: $temperature,
    max_output_tokens: $max_output_tokens,
    stream: false,
    store: false
  }' >"$REQ_JSON"

HDR=(-H "Content-Type: application/json" -H "Accept: application/json")
if [[ -n "$LM_API_TOKEN" ]]; then
  HDR+=(-H "Authorization: Bearer $LM_API_TOKEN")
fi

if ! curl -sS "${HDR[@]}" \
  "http://${LM_HOST}:${LM_PORT}/api/v1/chat" \
  -d @"$REQ_JSON" >"$RESP_JSON" 2>"$LM_LOG"; then
  printf "\n--- LM Studio request failed ---\nLog: %s\n\n" "$LM_LOG" >&2
  tail -n 200 "$LM_LOG" >&2 || true
  exit 1
fi

# Native v1 response: output[].content
SUMMARY="$(jq -r '
  (.output // [])
  | map(select(.type=="message") | .content)
  | .[0] // empty
' "$RESP_JSON")"

if [[ -z "$SUMMARY" ]]; then
  ERRMSG="$(jq -r '.error?.message // .message // .detail // empty' "$RESP_JSON")"
  [[ -n "$ERRMSG" ]] && die $'LM Studio returned an error:\n'"$ERRMSG"$'\n\nWorkdir kept at:\n  '"$WORKDIR"
  die $'LM Studio response had no output message.\nWorkdir kept at:\n  '"$WORKDIR"
fi

printf "\n\n%s\n\n" "$SUMMARY"