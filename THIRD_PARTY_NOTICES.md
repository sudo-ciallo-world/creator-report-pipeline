# Third-Party Boundaries

Bundled assets are preserved with their supplied notices:

- `static/vendor/lucide.min.js`: Lucide ISC terms and the Feather-derived icon MIT notice in `static/vendor/LUCIDE-LICENSE.txt`.
- `static/fonts/*.woff2`: Noto font subsets; supplied SIL OFL 1.1 terms in `static/fonts/OFL.txt`.
- Generated demo reports copy these notices alongside the corresponding assets.

Python libraries are declared as dependencies, not vendored or relabeled as original source. The larger application uses Pydantic, Jinja2, HTTPX, SQLModel, yt-dlp, youtube-transcript-api, python-dotenv, google-genai, and faster-whisper. The minimal demo needs only Pydantic and Jinja2.

ASR models, FFmpeg executables, platform content, cookies, real subtitles, and downloaded media are not distributed. Review the license and service conditions of the exact installed dependencies and model weights before a separate binary/model distribution or commercial deployment.

No project-level license is assigned by this preparation. Confirm rights in the application code separately from the bundled asset terms.
