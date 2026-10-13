# Changelog

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
