# Creator Report Pipeline

Turn transcripts into a local HTML report with traceable source text and separate generated summaries.

This Python core combines transcript caching, platform captions, ASR fallback, persistent jobs, and report exports. It is extracted from a larger Bilibili/YouTube reporting application. The included offline demo renders a fictional channel so you can inspect the report format without downloading videos or calling a model.

## Quick start

Requires **Python 3.11 or later**. From a shell with your preferred virtual environment active:

```sh
git clone https://github.com/sudo-ciallo-world/creator-report-pipeline.git
cd creator-report-pipeline
python -m pip install -r requirements.txt
python demo.py
```

Open **`demo-output/index.html`** in a browser. The demo generates a channel overview and two video detail pages, with bundled fonts, icons, and a generated waveform image.

The channel, transcripts, summaries, source URLs, and image are synthetic. No account, API key, downloaded media, or speech model is needed for this demo.

## What the demo exports

| Output | Contents |
| --- | --- |
| `index.html` | Channel overview and links to detail pages |
| `videos/*.html` | Per-video transcript and summary views |
| `full_transcripts.md` | Source transcript text, without generated summaries |
| `channel_summary.txt` | Generated interpretation stored separately |
| `creator_corpus.json` | Structured report data |
| `assets/` | Local fonts, icons, waveform image, and asset license notices |

The source/summary distinction is also checked in [demo.py](demo.py) and the report tests.

## Pipeline behavior

The online orchestration code selects transcripts in this order:

```text
Transcript cache
    -> platform captions
    -> configured ASR policy
    -> per-video summaries and channel synthesis
    -> HTML, Markdown, text, and JSON exports
```

The fixture demo starts at the rendering step. For integrations using the orchestration modules:

- `auto` transcription tries Gemini when configured, then local transcription on failure.
- `gemini` and `local` modes restrict the provider choice.
- Charging-only video failures are skipped instead of being forwarded to ASR.
- Jobs have configurable worker limits, cancellation, and retry links.
- On restart, running jobs become interrupted; queued jobs can be re-enqueued.

Timeouts bound awaited stages. They do not guarantee termination of an underlying native thread or external process.

## Tests

Install the additional test dependencies:

```sh
python -m pip install -r requirements-test.txt
python -m pytest -p pytest_asyncio.plugin -q -o asyncio_mode=auto
```

The 22 focused tests cover queue recovery, storage migration and retries, source normalization, cache/caption/ASR selection, report rendering, export separation, and offline contracts. They use temporary storage and fake providers. The test guard blocks external connections and native subprocess creation.

## Code map

| Path | Responsibility |
| --- | --- |
| [demo.py](demo.py) / [fixtures/](fixtures/) | Offline synthetic report |
| [src/creator_report/jobs.py](src/creator_report/jobs.py) | Persistent job coordination |
| [src/creator_report/storage.py](src/creator_report/storage.py) | SQLite storage and recovery |
| [src/creator_report/pipeline.py](src/creator_report/pipeline.py) | Transcript and reporting workflow |
| [src/creator_report/adapters/](src/creator_report/adapters/) | Bilibili and YouTube adapters |
| [src/creator_report/asr.py](src/creator_report/asr.py) | Transcription provider integration |
| [src/creator_report/reports.py](src/creator_report/reports.py) / [templates/](templates/) | Report rendering and exports |

## Using the online modules

This repository ships a source-tree pipeline snapshot. The web server, settings UI, and service launchers are outside the release; there is no full application launch command here.

[requirements-online.txt](requirements-online.txt) lists optional provider dependencies. An online integration must supply its own provider configuration, credentials, and any required native tools. See [config.py](src/creator_report/config.py) for settings and environment variable names.

The included checks establish offline behavior. Live platform access, Gemini transcription, GPU behavior, the complete web UI, and installation on a clean machine have not been validated for this public snapshot.

## Contributing

Use fictional channel/video identifiers and synthetic transcripts in reports and tests. Include the expected export or provider-selection behavior and a focused regression case. Run the tests above before submitting a change.

## Development and license

The project maintainer led the workflow, architecture, integration, debugging, and validation. Implementation used substantial AI-assisted coding. Platform extraction, speech recognition, pretrained models, database tooling, and web libraries are reused components.

Most development took place locally before the first public snapshot on **October 18, 2026**. Public commits record released snapshots and subsequent changes.

Project code and documentation use the [MIT license](LICENSE). Bundled Lucide/Feather assets and Noto fonts retain their own terms in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). See [SOURCE_MAP.md](SOURCE_MAP.md) and [LICENSE-DECISION.md](LICENSE-DECISION.md) for extraction and licensing details.
