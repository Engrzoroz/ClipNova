# ClipNova — Created by Nabigh Ahmed ✓

Mobile-first YouTube clip workflow prototype.

## What V1 does

- Paste a YouTube URL and analyze video metadata
- Embedded preview
- Add multiple timestamp ranges
- Download only requested sections on the server
- Optional burned captions using available YouTube subtitles/auto-captions
- 720p / 1080p / 1440p / 4K / best quality target
- Single MP4 or ZIP output
- Android + iPhone responsive interface
- Animated “Created by Nabigh Ahmed ✓” footer

## Important use note

Use this only for videos you own, public-domain/Creative Commons material where permitted, or content you have permission to process.

## Local run with Docker

```bash
docker build -t clipnova .
docker run --rm -p 10000:10000 clipnova
```

Open: http://localhost:10000

## Local run without Docker

Install Python 3.12+, FFmpeg, then:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 10000
```

## Render deployment

1. Upload this project to a GitHub repository.
2. In Render choose New > Web Service.
3. Connect the repository.
4. Render should detect the Dockerfile.
5. Choose the Free instance if available.
6. Health check path: `/api/health`
7. Deploy.

## Free-hosting reality

4K video cutting and especially burned captions are CPU/bandwidth heavy. Free hosts can sleep, throttle, or hit bandwidth/storage limits. This V1 limits jobs to 6 clips, 10 minutes per clip, and 20 total output minutes.

## Caption behavior

V1 uses subtitles/automatic captions made available for the source video. If no suitable subtitle track is available, the clip is still exported and the UI reports that captions were unavailable. A future V2 can add a separate speech-to-text service.
