# yt-summarizer

local pipeline that gives you a text summary of a YouTube video, and allows for interactive Q&A about the video content.

`summarize.sh` delegates to `summarize.py`, which does the following:

1. Downloads YouTube audio (& metadata) using `yt-dlp`
2. Splits audio into chunks with `ffmpeg`
3. Transcribes the chunks locally using `voxmlx`
4. Extracts structured evidence notes from transcript windows with LM Studio
5. Classifies the video type and writes a final summary from the compact evidence notes
6. Runs interactive Q&A with retrieval over the evidence notes and relevant transcript excerpts

With `-t`, it stops after transcription and prints the transcript to stdout.

## Requirements

- `yt-dlp`
- `ffmpeg`
- `python3`
- `conda` with a `voxmlx` environment
- `voxmlx` [CLI](https://github.com/awni/voxmlx) installed & available
- LM Studio server running and reachable

## Installation

macOS only.

1. Clone the repo:

```bash
git clone https://github.com/DD-65/yt-summarizer
cd yt-summarizer
```

2. Install system dependencies:

```bash
brew install yt-dlp ffmpeg
```

3. Install Conda if you do not already have it, then create the transcription environment:

```bash
conda create -n voxmlx python=3.11 -y
conda activate voxmlx
```

4. Install the `voxmlx` CLI in that environment using the instructions from the [voxmlx repo](https://github.com/awni/voxmlx), and make sure `voxmlx` is callable on PATH afterwards.

5. Start LM Studio and enable its local server. The script expects these defaults unless you override them:

- Server hostname: `localhost`
- Server port: `5432`
- LLM used: `liquid/lfm2.5-1.2b`

6. Set your LM Studio API token as an environment variable:

```bash
export LM_API_TOKEN=your_token_here
```

7. Verify the script:

```bash
./summarize.sh --help
```

8. Run it on a video:

```bash
./summarize.sh "https://www.youtube.com/watch?v=..."
```

## Usage

```bash
./summarize.sh "https://www.youtube.com/watch?v=..."
```

Summary only (disables the default Q&A loop):

```bash
./summarize.sh -qa "https://www.youtube.com/watch?v=..."
```

Transcription only (skips LM Studio and prints the transcript to stdout):

```bash
./summarize.sh -t "https://www.youtube.com/watch?v=..."
```

Required environment variables:

```bash
LM_API_TOKEN=...
```

`LM_API_TOKEN` is only required when generating a summary or using Q&A mode.

Optional environment variables (set to the standard values I use):

```bash
CONDAENV=voxmlx
LM_HOST=localhost
LM_PORT=5432
LM_MODEL=liquid/lfm2.5-1.2b
CHUNK_SECONDS=60
MAX_OUTPUT_TOKENS=600
TEMPERATURE=0.2
KEEP_WORKDIR=0
CACHE_DIR=~/.cache/yt-summarizer
REFRESH_CACHE=0
PIPELINE_WINDOW_CHUNKS=3
QA_RETRIEVAL_NOTES=8
QA_RETRIEVAL_CHUNKS=4
```

## Reference

- `voxmlx`: https://github.com/awni/voxmlx
- `yt-dlp`: https://github.com/yt-dlp/yt-dlp

## Example output (default behavior):
```
$ ./summarize.sh https://youtu.be/[video]            
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
