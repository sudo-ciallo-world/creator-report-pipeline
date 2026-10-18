# Creator Report Pipeline

Turn creator transcripts into a local HTML report while keeping source text separate from generated summaries.

This is a runnable pipeline snapshot from a larger Bilibili/YouTube reporting application. It contains the actual report renderer, storage, queue, platform adapters, and transcription-fallback orchestration. The web server, settings UI, launchers, personal configuration, and collected media are excluded.

## Offline Demo

Python 3.11 or later:

```sh
python -m pip install -r requirements.txt
python demo.py
```

Open `demo-output/index.html`. The fixture's channel, transcripts, summaries, source URLs, and waveform image are synthetic. No videos are downloaded and no model is called. The output includes detail pages, source-only Markdown transcripts, a separate summary text file, and structured corpus JSON.

## Focused Tests

```sh
python -m pip install -r requirements-test.txt
python -m pytest -p pytest_asyncio.plugin -q -o asyncio_mode=auto
```

Tests use temporary storage and fake providers. External network connections and native subprocesses are blocked during tests.

The prepared snapshot passed 22 focused tests.

The implemented selection order is cache, then platform captions, then the configured ASR policy. In `auto` mode, Gemini is tried when configured and local transcription follows on failure; explicit `gemini` or `local` modes restrict that choice. Charging-only video failures are skipped rather than sent to ASR. Provider capability and live platform access are not implied by these unit tests.

The queue has a configurable worker limit, cancellation, and restart handling: running work is marked interrupted and queued work can be re-enqueued. Timeouts bound awaited stages; they do not establish that an underlying native thread or external process has been killed.

## Provenance And AI Assistance

Most development took place locally before this public snapshot, first published on October 18, 2026. Public commits record the released snapshot and subsequent changes.

sudo-ciallo-world conceived and led the product workflow, architecture direction, queue/cache/fallback integration, report structure, debugging, validation, and acceptance criteria. Implementation was developed with substantial AI-assisted coding. Platform extraction, speech recognition, pretrained models, database tooling, and web libraries are reused.

This is distinct from a separate Bilibili batch-processing browser extension. It does not claim original ASR or language-model research.

See `THIRD_PARTY_NOTICES.md`, `SOURCE_MAP.md`, and `LICENSE`. The MIT license covers only sudo-ciallo-world's licensable code; third-party components retain their own notices. Live API access, Gemini transcription, GPU behavior, complete web UI behavior, and a clean-machine installation were not revalidated during this preparation.

