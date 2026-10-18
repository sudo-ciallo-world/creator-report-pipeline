# Source Map

Selected from the current working tree of the larger Creator Report application:

- `src/creator_report/`: domain models, storage, queue, transcript/ASR integration, synthesis helpers, and reporting.
- `templates/report_index.html`, `templates/video_detail.html`.
- Local Lucide browser bundle and Noto font subsets with adjacent license files.
- Seven pre-existing test modules for queue, storage, transcription order, reports, pipeline, estimates, and source resolution.

Preparation adds the synthetic fixture, waveform generator, cache/import-location regressions, offline test guard, documentation, and dependency lists. The storage test's sample channel identifier was replaced with an explicit synthetic identifier. The renderer now copies the bundled assets' license notices into generated reports as well.

The application UI/server and service launchers are deliberately absent. This repository is a source-tree pipeline snapshot, not an installable copy of the full web application.

The local source repository has an origin, but account ownership, remote visibility, and the complete origin of every historical line were not independently established here. Do not describe a fresh history as historical proof of authorship. The source working tree has later changes beyond its last commit; this snapshot includes the selected current files.
