# yt-summarizer

local pipeline that gives you a text summary of a YouTube video.

`summarize.sh` does the following:

1. Downloads YouTube audio (& metadata) using `yt-dlp`
2. Splits audio into chunks with `ffmpeg`
3. Transcribes the chunks locally using `voxmlx`
4. Sends the transcript to a local model in LM Studio for a concise summary

## Requirements (macOS only)

- `yt-dlp`
- `ffmpeg`
- `curl`
- `jq`
- `conda` with a `voxmlx` environment (`voxmlx` CLI available)
- LM Studio server running and reachable

## Usage

```bash
./summarize.sh "https://www.youtube.com/watch?v=..."
```

Optional environment variables (set to the standard values I use):

```bash
LM_HOST=localhost
LM_PORT=5432
LM_MODEL=liquid/lfm2.5-1.2b
CHUNK_SECONDS=60
MAX_OUTPUT_TOKENS=600
TEMPERATURE=0.2
KEEP_WORKDIR=0
LM_API_TOKEN=
```

## Reference

- `voxmlx`: https://github.com/awnihannun/voxmlx
- `yt-dlp`: https://github.com/yt-dlp/yt-dlp
