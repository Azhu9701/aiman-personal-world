# AIMAN Personal World

**A local workspace for browsing your chat history, tracing evidence, and reviewing possible next steps yourself.**

Version 0.3.0 is an early, working release. It imports normalized chat archives and uses local keywords and rules to organize demand, supply, and collaboration signals around topics such as robotics, robotic arms, manufacturing capacity, and control systems. Each candidate leads back to its source messages. The self-profile page now lets owners review their own statements, maintain approved descriptions, and preview selected sharing material.

This version does not call an LLM, perform semantic search, establish that an opportunity was never pursued, or score a person's trustworthiness. Connections to public AIMAN Robotics / Manufacturing worlds are a roadmap item.

[中文](README.md) · [World definition](WORLD.md) · [Privacy](docs/privacy.md) · [API](docs/api.md) · [Roadmap](docs/roadmap.md)

## Run the synthetic demo first

Use **Python 3.10 or newer**. The demo and normalized-archive viewer use the Python standard library. No WeChat installation, database keys, model, cloud account, or media dependency is needed.

1. Download and extract this repository, then open a terminal in its source directory.
2. Run:

   ```bash
   python3 --version
   python3 personal_world.py demo
   ```

   On Windows, `py -3` can be used instead of `python3` when Python Launcher is installed.

3. Your browser opens the local application, using port `18792` by default. The interface keeps the synthetic-data label visible.

The demo contains **8 invented conversations and 40 manually written messages spanning 2023–2026**. All people, names, needs, capacity statements, events, and dates are fictional. A separate demo state directory protects existing personal data. The command does not discover or open a WeChat account. [Inspect the fixture](examples/demo.json).

Use `python3 personal_world.py demo --no-open` to prepare the demo without starting a server or browser, for example in a script or headless environment. Run `python3 personal_world.py demo` again to open an authenticated demo page. Entering the local address manually does not establish a session. See [getting started](docs/getting-started.md) for state directories, ports, and other commands.

## What works today

| Workspace | Purpose | How to verify it |
|---|---|---|
| Workbench | Inspect import scope and organize rule matches for demand, supply, and collaboration | Check the synthetic label, coverage, message IDs, and context |
| Chat history | Browse, search, paginate, and export imported conversations | Open source messages and compatible local attachments |
| Timeline | Browse and filter evidence over time | Return to the original conversation and inspect later discussion |
| Graph | Connect conversations, topics, and conversation-scoped senders | Inspect the message evidence for each edge; equal display names do not establish identity |

Chat browsing, search, pagination, available local attachments, and conversation export remain available. Compatible local images, video, voice, files, and avatars can be attached; missing or unsupported content keeps an explicit status.

You can review a candidate, mark it for follow-up, dismiss it, mark it complete, or create a local template draft. **A review is a user annotation; a draft has not been sent.** This release contains no function for contacting WeChat users or other third parties.

## Bring your own archive

For an existing normalized JSON or JSONL export:

```bash
python3 personal_world.py import "/path/to/your-export.jsonl"
python3 personal_world.py verify
python3 personal_world.py open
```

The importer creates a separate index. Stable, globally unique message IDs preserve the route back to evidence. A row number reused by another conversation or database is not a global identity. See [the data format](docs/data-format.md).

An optional WeChat adapter reads your local databases when you already have authorized access to the data and valid per-database SQLCipher keys. This release **does not include key acquisition, application re-signing, WeChat debugging, or system-protection changes**. It never performs those operations automatically. See [WeChat import](docs/wechat-import.md) for prerequisites and limits.

## Local data, explicit uncertainty

The source archive and imported index provide message evidence. Rule candidates and graph edges are derived views. `workspace.sqlite` stores human review and draft state; it does not become a canonical source of facts.

The server listens on `127.0.0.1` by default, and data APIs require a local session token. Archives, indexes, media, reviews, and drafts stay on your computer. There is no telemetry, cloud inference, automatic external lookup, or message sending in this release. [Privacy design](docs/privacy.md).

**Application-level encryption at rest is not implemented.** On POSIX systems such as macOS and Linux, private state directories use `0700` and private files use `0600`. Windows ACL self-checking is not implemented. Permissions do not replace disk encryption: exports, media, annotations, and backups may contain readable private information. You may enable FileVault, BitLocker, or other operating-system disk encryption yourself; this project neither enables it nor claims it is already enabled.

A message mentioning a robotic arm and a need may be an old request, a quotation, an announcement, a negation, or completed work. Finding no later rule match only describes the imported data and current rules. It does not establish that nothing happened offline or elsewhere. Candidate scores order retrieval results; they are not success probabilities, reputation scores, or verified business facts. Cross-conversation demand/supply suggestions indicate shared specific topics, not verified compatibility or current availability.

Rules currently use mainly Chinese vocabulary with a few English aliases; this is not general language understanding. Playable voice does not mean searchable speech: this version has no ASR, image OCR, or complete sticker decoding.

## Develop and check a release

```bash
python3 -B -m unittest discover -v
python3 -B scripts/release_check.py
```

Tests use synthetic data. Media tests that need optional packages explicitly skip when those packages are unavailable; a skipped test is not a validated capability. CI is configured to run the standard-library suite and release checks on Ubuntu and macOS, plus `scripts/test_frontend.mjs` through Node. The latter checks DOM interaction and local API integration; it is not a browser visual, layout, or native-media playback audit.

The release checker inspects publishable files for runtime data, key/configuration artifacts, databases, non-synthetic JSON, private hardcoded paths, and suspicious secret values. It cannot prove that arbitrary prose contains no private information. Human review remains necessary. [Contributing](CONTRIBUTING.md).

## Version and license

**v0.2.0** is an early personal evidence workspace, not a complete world model or autonomous business-decision system.

Original project code is [MIT licensed](LICENSE). Adapted DAT image-format helpers retain their upstream MIT notice in [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt). Pilk, SQLCipher bindings, FFmpeg, and other separately installed dependencies keep their own licenses. Source releases do not bundle WeChat binaries, runtime vendor directories, model weights, or user data.

[Changelog](CHANGELOG.md) · [Security reporting](SECURITY.md)

## v0.1.1 workflow improvements

Edit a draft title and body and choose **Save changes**. Saved edits survive page reloads; the original generated version and subsequent saved revisions remain available. Concurrent stale saves are rejected rather than silently overwriting another editor. Unsaved browser edits remain in the current page; autosave is not implemented.

Completed, dismissed, or stale reviewed counterparts are excluded from related recommendations and new draft templates. Source fingerprints remain independent of human status. A changed source returns a previously closed item to the needs-review lane while preserving its original annotation. The review ledger keeps earlier notes visible even when the source is no longer locatable. A stale draft can still be read or copied, but editing requires a freshly generated draft after checking current evidence.

`scripts/test_browser.mjs` provides a real Chromium smoke check using only handwritten synthetic data and a new temporary browser profile. No private browser session or downloaded browser is used.

## v0.2.0 — History backfill and incremental merge

The Sources dialog now accepts local normalized JSON/JSONL files. Preview additions, exact duplicates and conflicts before applying. Stable IDs are deduplicated only within an explicit namespace. Backfills keep existing content by default; an explicit prefer-incoming policy updates same-ID content. Different conversation or timestamp identity collisions block confirmation. Missing rows never mean automatic deletion.

Configured WeChat refreshes now merge instead of replacing accumulated history. Collection files are regenerated and the index is copied for safe publication, but only new/changed rows and changed FTS entries are written. Source extraction remains a fully verified snapshot, not incremental encrypted-page reading or a background watcher.

Retained normalized input snapshots support per-record origin auditing. Existing reviews and draft revisions remain separate. A durable publication journal supports recovery after interruption between the archive/index swaps; unknown external changes cause a stop instead of forced overwrite. These snapshots are private, are not automatically expired, and do not disappear when you delete a message in the original client. Application-level at-rest encryption and selective deletion are still not implemented.

See [history import and recovery](docs/history-import.md) for source namespaces, the 512 MiB batch limit, CLI commands and verification scope.

## v0.3.0 — Self profile

Open **个人画像** from the main navigation. The local rules summarize topic mentions and monthly coverage, and identify literal Chinese first-person self-statements. Nothing becomes an approved profile until the owner reviews it. Confirm/edit/dismiss/delete, exclude conversations, and explicitly preview selected confirmed statements before copying or downloading them. Original evidence remains separately accessible.

Only primary-namespace text marked as self-sent contributes; imported identity markers are not independent identity authentication. No personality diagnosis, sensitive trait inference, third-party reputation scores, external model calls, or automatic sharing. Filtering is heuristic and can miss private content. Profile storage has no application-level encryption. Deleting a profile item does not delete its raw chat, source snapshots or previously exported copies. See [details](docs/personal-profile.md).
