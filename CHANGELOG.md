# Changelog

## DeepSeek Harness plugin preview

- Package the complete local workspace as @aiman-world/dsh-personal-world for DSH 0.2.0-rc.2 using native host and client contracts.
- Expose eight local module links and two non-private status/navigation tools. No private chat-to-model bridge, calendar writes or automatic invitations are included.
- Declare the official client renderer/sidebar dependencies and wait for the slots service before mounting the native sidebar entry.
- Add strict public npm manifest validation, plugin contract tests and an isolated real-Harness browser check.

## 0.4.0 — Agenda preview

- Add message-anchored Chinese date hints with explicit ambiguity, source identity and possible cancellation/change context.
- Add owner-reviewed local itinerary, full start/end validation, manual travel/preparation buffers, overlap checks and alternative proposals.
- Add selected-only RFC5545 export with stable random UIDs, UTF-8 folding and no attendees, alarms or raw chat. No external calendar writes.
- Add private agenda state, revision/source checks, shared state lock, expiry and source-change export protection.
- Add isolated synthetic browser and date/API/ICS tests. Real chat extraction remains unverified in this iteration.


## 0.3.0

- Add a local self-profile page with literal first-person candidates, bounded primary self-text analysis, non-sensitive topic counts and monthly ratios.
- Separate owner-confirmed/corrected prose from source quotes; retain source evidence and pause export after evidence changes.
- Add explicit confirmation, dismissal, conversation exclusion and text-removing deletion with exact-candidate suppression. No deletion of chat archives or external copies.
- Add revision-conflict protection and selected-only export preview without raw conversations or message identifiers.
- Keep analysis local with no model requests, personality scores or automatic sharing; document heuristic privacy filtering and unencrypted local storage.
- Add handwritten examples plus synthetic API, concurrency, provenance, deletion and isolated Chromium checks.


## 0.2.0 — 2026-10-13

- Add local JSON/JSONL history upload, preview and explicit commit/cancel, with source namespaces and conservative conflict handling.
- Preserve records absent from a later snapshot; reject same-ID conversation/timestamp collisions rather than silently merging people or histories.
- Retain normalized inputs by hash and audit each normalized collection record against its retained origin.
- Reuse a consistent SQLite backup and write only new/changed rows and changed FTS entries. Native WeChat extraction remains a fully checked snapshot.
- Route configured refresh through cumulative merge and provide refresh --preview for review before publication.
- Add monthly coverage and source/import batch inspection, with unknown and empty ranges explicitly distinguished.
- Prevent a failed media re-read from erasing an existing available attachment: preserve the complete previous record/origin, and stop when message content also changed.
- Add a durable archive/index publication journal, controlled recovery, source-change checks and reader pause during publication.
- Extend synthetic tests and real Chromium file-selection/preview/commit coverage. No background watcher, data deletion UI, cloud inference or application-level encryption was added.


## 0.1.1 — 2026-10-13

- Persist edited draft titles and bodies in an append-only revision table; preserve generated v1 rows and read old workspaces without migration.
- Reject conflicting saves across windows/processes; retain local input, allow explicit reload, and preserve all saved revisions.
- Recheck source versions around draft writes and reject edits when source evidence or reviewed counterpart visibility has changed.
- Exclude completed/dismissed/stale reviewed counterparts from related recommendations and newly generated templates without changing source fingerprints.
- Add a paginated review ledger that preserves annotations even when current source records cannot be located.
- Add authenticated draft detail/history and edit endpoints; keep all actions local with no message-send API.
- Extend synthetic workflow/DOM tests and add isolated real-Chromium rendering, save/reload and mobile-width smoke tests.


## v0.1.0 · 2026-10-13

Initial source release of AIMAN Personal World, an early local evidence workspace.

### Added

- Python 3.10+ entry point for an isolated synthetic demo, archive import, local serving, configuration, manual refresh, diagnostics, and verification.
- Local chat browsing, keyword search, stable pagination, source-message context, conversation export, and compatible cached media.
- A personal workbench, chat history, evidence timeline, and conversation-scoped sender/topic graph.
- Explicit local rules for topic, demand, supply, and collaboration signals; fixed discussion windows with later-context and possible-progress counterevidence.
- Limited cross-conversation demand/supply suggestions with separate source-message evidence and unverified compatibility labels.
- Human review notes and local template drafts, source-version checks on writes, and stale-evidence indicators.
- A fully manual Chinese demo with 8 fictional conversations and 40 messages across 2023–2026, including completed work, offline progress, announcements, unrelated content, and equal display names in different conversations.
- Standard-library synthetic tests, a DOM/API integration test harness, and a source-release privacy checker.
- Public documentation for scope, evidence, uncertainty, data handling, API use, and optional WeChat import with already available keys.

### Boundaries

There is no LLM inference, semantic search, automatic external lookup, contact sending, personal reputation scoring, ASR, image OCR, or complete sticker decoding. Refresh is manual and complete rather than incremental. Public AIMAN Robotics / Manufacturing world connections remain a roadmap item.

Data is local, but application-level encryption at rest is not implemented. POSIX private permissions are checked; Windows ACL verification is not implemented. Optional native dependencies and unsupported source schemas require explicit checks. A synthetic test pass does not establish compatibility with every client version or a complete browser visual/accessibility audit.

### Licensing

Original code is MIT licensed. Adapted DAT helpers retain upstream MIT attribution in `THIRD-PARTY-NOTICES.txt`. This release excludes WeChat binaries, key-acquisition tools, runtime vendor directories, model weights, keys, and user data.
