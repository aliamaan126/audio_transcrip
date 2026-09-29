# Audio Transcription Pipeline

Asynchronous speech-to-text service. Clients upload audio or video over HTTP, the API stores the
file and returns a job id immediately, and Celery workers normalize, chunk, transcribe and merge
the audio in the background. Results are available as JSON, SRT or WebVTT.

## Overview

- `POST /v1/transcriptions` accepts a multipart upload, validates it by magic bytes, streams it to
  object storage, and queues a job.
- Workers convert anything ffmpeg can read to 16 kHz mono PCM WAV, then transcribe it with
  faster-whisper.
- Audio longer than a configurable threshold (default 10 minutes) is split at silence boundaries
  into overlapping chunks, transcribed in parallel, and merged back into a single transcript.
- Jobs, transcripts and chunk state live in PostgreSQL; raw audio lives in MinIO with a retention
  policy.

## Architecture

```
                 ┌──────────┐
   client ──────▶│ FastAPI  │──── raw audio ────▶ MinIO
                 │  (api)   │
                 └────┬─────┘
                      │ job row              ┌────────────┐
                      ├─────────────────────▶│ PostgreSQL │
                      │                      └────────────┘
                      │ enqueue                     ▲
                      ▼                             │
                 ┌──────────┐                       │
                 │  Redis   │◀── chord state ───┐   │
                 └────┬─────┘                   │   │
                      │                         │   │
                      ▼                         │   │
              ┌───────────────┐                 │   │
              │ Celery worker │ ffmpeg ─────────┘   │
              │  process_job  │─── chunks ──▶ MinIO │
              └───────┬───────┘                     │
                      │ chord(group)                │
                      ▼                             │
              ┌───────────────┐                     │
              │transcribe_chunk│ × N  (whisper)     │
              └───────┬───────┘                     │
                      │ callback                    │
                      ▼                             │
              ┌───────────────┐  merged transcript  │
              │  finalize_job │─────────────────────┘
              └───────┬───────┘
                      │
                      ▼
              webhook → callback_url
```

## Setup

```bash
cp .env.example .env          # optional, compose ships working defaults
docker compose up --build
```

This starts `postgres`, `redis`, `minio`, a one-shot `migrate` job, the `api` on
<http://localhost:8000>, a `worker`, and a `beat` scheduler for retention cleanup. Interactive API
docs are at <http://localhost:8000/docs>, the MinIO console at <http://localhost:9001>.

Scale workers horizontally:

```bash
docker compose up --scale worker=4
```

Run locally without Docker: install with `pip install -e ".[dev]"`, make sure `ffmpeg` and
`ffprobe` are on `PATH`, point `.env` at your own Postgres/Redis/MinIO, then

```bash
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker --loglevel=info
```

## API usage

Every endpoint except `/health` requires `X-API-Key`. Keys come from the `API_KEYS` environment
variable (comma separated).

Upload:

```bash
curl -X POST http://localhost:8000/v1/transcriptions \
  -H "X-API-Key: local-dev-key" \
  -F "file=@sample_audio/speech_16k_mono.wav" \
  -F "language=en" \
  -F "callback_url=https://example.com/hooks/transcription"
```

```json
{
  "job_id": "0f9a1f0a-2b1a-4f0e-9f0e-0a1b2c3d4e5f",
  "status": "queued",
  "segments": [],
  "created_at": "2024-05-01T10:00:00Z"
}
```

Poll:

```bash
curl http://localhost:8000/v1/transcriptions/$JOB_ID -H "X-API-Key: local-dev-key"
```

```json
{
  "job_id": "0f9a1f0a-2b1a-4f0e-9f0e-0a1b2c3d4e5f",
  "status": "completed",
  "language": "en",
  "duration": 812.4,
  "full_text": "...",
  "segments": [{ "id": 0, "start": 0.0, "end": 4.2, "text": "...", "confidence": 0.93 }],
  "error": null,
  "created_at": "2024-05-01T10:00:00Z",
  "completed_at": "2024-05-01T10:01:44Z",
  "progress": { "completed_chunks": 18, "total_chunks": 18 }
}
```

Export, retry, delete:

```bash
curl "http://localhost:8000/v1/transcriptions/$JOB_ID/export?format=srt" -H "X-API-Key: local-dev-key"
curl -X POST http://localhost:8000/v1/transcriptions/$JOB_ID/retry -H "X-API-Key: local-dev-key"
curl -X DELETE http://localhost:8000/v1/transcriptions/$JOB_ID -H "X-API-Key: local-dev-key"
curl http://localhost:8000/health
```

Errors always use the same shape:

```json
{ "error": { "code": "unsupported_media_type", "message": "..." } }
```

On completion or failure the job payload is POSTed to `callback_url` if one was supplied.

### CLI

For local runs without the API or any infrastructure:

```bash
python -m app.cli transcribe sample_audio/speech_16k_mono.wav --language en
```

It reuses the same audio, engine and merge services and prints the transcript as JSON.

## Design decisions

**Concurrency.** The API is stateless: it writes the upload to MinIO, inserts a row, and enqueues.
All CPU-bound work happens in Celery workers, which scale independently
(`docker compose up --scale worker=N`). Long audio fans out through a Celery chord so chunks of one
job are transcribed by different workers in parallel, with a callback that merges the result.
Rate limiting is a fixed-window counter per API key in Redis; if Redis is unreachable the limiter
fails open rather than taking the API down.

**Storage.** Raw uploads go to MinIO under `jobs/{job_id}/`, never to the database. Uploads are
streamed to a temporary file in 1 MB blocks while the SHA-256 is computed, then handed to boto3's
multipart upload, so memory use is independent of file size. Job metadata and transcripts live in
PostgreSQL, with segments stored as JSONB so they can be queried without a separate table. Raw
audio older than `RAW_AUDIO_RETENTION_DAYS` is deleted by a periodic task; transcripts are kept.

**Format handling.** The container is identified from magic bytes, not the filename or the
client-supplied content type, so a `.wav` extension on a PDF is rejected with 415. Everything that
passes is probed with ffprobe and transcoded by ffmpeg to 16 kHz mono 16-bit PCM WAV, which is what
Whisper models expect; video inputs simply have their audio track extracted. Files with no audio
stream, no duration, or a broken container fail immediately with `status=failed` and a readable
error rather than being retried.

**Long audio.** Files above `CHUNK_THRESHOLD_SECONDS` are split on silence detected by ffmpeg's
`silencedetect` filter. The planner walks the timeline and cuts at the silence closest to the
target chunk length within a 30–60 s window, falling back to a hard cut that avoids leaving a
sliver at the end. Consecutive chunks overlap by `CHUNK_OVERLAP_SECONDS` so no word is lost at a
boundary. Each chunk is a row in `chunks` with its offset and status; the merge step shifts every
segment by its chunk offset and strips the longest run of leading words that repeats the tail of
the previous chunk, which removes the duplicated speech the overlap introduces. Progress is
reported as `completed_chunks / total_chunks`, and chunk objects are deleted from MinIO once the
job completes.

**Retries.** Tasks are idempotent: `process_job` returns early for completed jobs, and
`transcribe_chunk` returns early for completed chunks, so a re-delivered message is harmless.
`acks_late` and `task_reject_on_worker_lost` mean a task whose worker dies is re-queued rather than
lost. Transient failures retry up to three times with exponential backoff; after that the job is
marked `failed` with the error stored. Chunk retries are explicit rather than automatic so the
chord callback always runs and can report which chunks failed. `POST /{id}/retry` re-runs a failed
job and skips chunks that already succeeded, so only the broken part is transcribed again.

**Engines.** Transcription is reached only through the `TranscriptionEngine` protocol.
`ENGINE=whisper` uses faster-whisper with automatic language detection and maps `avg_logprob` to a
0–1 confidence via `exp()`. `ENGINE=mock` is a deterministic engine used by the tests and useful
for developing without a model download.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Tests run against the mock engine and a filesystem stand-in for MinIO, so no infrastructure is
needed. They cover magic-byte validation and rejection of corrupt files, normalization output,
chunk planning and overlap dedupe, SRT/VTT formatting, the upload → poll → completed flow,
deduplication by hash, the retry endpoint, auth failures, engine failures exhausting retries, and
retention cleanup.

Tests that shell out to ffmpeg skip automatically when it is not installed. To run everything,
including those:

```bash
docker compose run --rm api pytest
```

`sample_audio/` holds two small synthetic files (a 6 s tone-and-silence WAV and a 2 s silent MP3)
used for format and pipeline checks; they contain no speech, so the mock engine is what produces
transcript text in tests.

## Limitations and next steps

- Rate limiting uses a fixed window, which allows a burst of up to 2× the limit across a window
  boundary. A sliding window or token bucket would be stricter.
- Webhook delivery retries three times and then gives up; there is no dead-letter queue or
  signature header for the receiver to verify.
- `silencedetect` is cheap and dependency-free but cuts on energy, not speech. Silero VAD behind
  the same interface would place chunk boundaries better on noisy recordings.
- Overlap dedupe compares words. It handles repeated speech well and leaves non-matching text
  alone, but it cannot recover from an engine transcribing the same audio two different ways.
- Progress is chunk-level; single-file jobs report no intermediate progress.
- The API key list is an environment variable. Real deployments would want per-key records,
  rotation and scopes.
- GPU inference, model warm-up on worker start, and speaker diarization are all out of scope here.
