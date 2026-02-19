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
- `conda` with a `voxmlx` environment
- `voxmlx` (CLI)[https://github.com/awni/voxmlx] installed & available
- LM Studio server running and reachable

## Usage

```bash
./summarize.sh "https://www.youtube.com/watch?v=..."
```

Q&A mode (runs summary first, then opens an interactive Q&A loop over the same transcript context):

```bash
./summarize.sh -qa "https://www.youtube.com/watch?v=..."
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
CACHE_DIR=~/.cache/yt-summarizer
REFRESH_CACHE=0
```

## Reference

- `voxmlx`: https://github.com/awni/voxmlx
- `yt-dlp`: https://github.com/yt-dlp/yt-dlp

## Example output (with -qa flag):
```
$ ./summarize.sh -qa https://youtu.be/[video]            
[15:18:38] Workdir: [directory]
[15:18:38] Fetching video metadata...
[15:18:40] Saving metadata JSON...
[15:18:43] Downloading + extracting audio (FLAC)...
[15:18:47] Downloaded: [video_title].flac
[15:18:47] Splitting into 60s chunks...
[15:18:48] Chunks: 11
[15:18:48] Activating conda env: voxmlx
[15:18:48] Transcribing chunks with voxmlx...
[15:18:48]   [1/11] seg_0000.wav
[15:19:02]   [2/11] seg_0001.wav
[15:19:13]   [3/11] seg_0002.wav
[15:19:25]   [4/11] seg_0003.wav
[15:19:36]   [5/11] seg_0004.wav
[15:19:48]   [6/11] seg_0005.wav
[15:20:00]   [7/11] seg_0006.wav
[15:20:12]   [8/11] seg_0007.wav
[15:20:23]   [9/11] seg_0008.wav
[15:20:35]   [10/11] seg_0009.wav
[15:20:46]   [11/11] seg_0010.wav
[15:20:51] Summarizing with LM Studio (model: [selected LLM, I use liquid/lfm2.5-1.2b])


The video reviews a $300 laptop, focusing on battery life, performance optimization, and undervolting techniques. It outlines a step-by-step process involving battery upgrades, power management settings, and performance testing. Key points include the laptop’s initial poor battery performance, improvements after a 104Wh battery installation, and the impact of undervolting on CPU performance. The reviewer tested power consumption, wattage limits, and real-world usage scenarios, concluding that the device achieves strong efficiency with careful tuning. Pricing is not specified, but the plan emphasizes cost-effective upgrades for better battery and performance. The final takeaway is that targeted adjustments can significantly extend battery life and boost efficiency. 

Caveats: Results may vary based on usage patterns, and the internal battery quality was a recurring issue.

[15:20:58] Q&A mode enabled. Ask questions about the video.
[15:20:58] Press Enter on an empty line, or type 'exit'/'quit' to stop.
Q> which os does the laptop run?

The laptop runs Windows.

Q> with which laptop does he start? what is the base product?

The laptop runs Windows. The base product mentioned is a ThinkPad T580.
```
