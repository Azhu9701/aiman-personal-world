# Contributing

Contributions should make the application easier to verify and safer to use with personal data. Read [WORLD.md](WORLD.md) and [privacy.md](docs/privacy.md) before changing data flow, candidate meaning, or actions.

## Run from source

Use Python 3.10+. No package installation is required for the synthetic demo or normalized-archive viewer:

```bash
python3 personal_world.py demo
python3 -B -m unittest discover -v
python3 -B scripts/release_check.py
```

With Node 20+ available, run the DOM interaction and local API integration harness:

```bash
node scripts/test_frontend.mjs
```

The harness creates an isolated synthetic state and local test server, then cleans them up. It is not a real-browser screenshot comparison, visual review, or complete accessibility audit. Python tests needing optional native or media modules explicitly skip if unavailable; record those skips instead of presenting them as verified paths.

## Keep examples artificial

Do not add real chats, contact names, usernames, keys, tokens, databases, device-specific paths, screenshots, decrypted media, user counts, or private experiment reports. Removing names from a real conversation is not sufficient anonymization. Write a new minimal fixture from scratch.

The public `examples/demo.json` has a strict artificial-data contract: explicit `synthetic: true`, a visible label, `demo:` identities, declared counts, known fields, and text-only messages. New fixture formats require a deliberate release-checker/schema change and review; do not bypass the checker with a filename or a synthetic label.

Optional WeChat adapter work can use manually created SQLite schemas and manufactured payloads. Do not add key acquisition, application re-signing, runtime attachment, private binary dumps, or system-protection changes to this release.

## Evidence and behavior changes

For rule changes, show both a useful match and a plausible counterexample. Include later completion or cancellation, offline progress without repeating the topic word, unrelated announcements, same-topic different projects, and same-name different conversations where relevant.

Keep message IDs and evidence links stable. A score must remain an explained retrieval ordering, not a probability or a personal reputation score. Shared keywords do not establish technical compatibility, availability, identity, or real cooperation.

Changing the evidence underlying a decision must change its source fingerprint. The server must reject stale review/draft writes; clients should ask the user to inspect current evidence instead of silently retrying a previous decision with a fresh fingerprint.

Keep raw evidence separate from derived views and user annotations. Existing drafts preserve their source version and remain unsent. Network calls, publishing, new model dependencies, or third-party action capabilities require explicit design review and must not be introduced as hidden side effects.

## Pull requests

Explain the concrete problem, changed behavior, relevant evidence or synthetic case, and verification performed. State optional tests skipped and compatibility gaps. Update documentation when an interface, data field, privacy boundary, or claim changes.

Use [security reporting](SECURITY.md) for vulnerabilities or suspected private-data exposure. Public issues and PRs must remain synthetic.

## Prepare a public source release

1. Review prospective source files and the diff. Keep personal state outside the repository.
2. Run tests and `scripts/release_check.py` after the final changes.
3. Inspect the actual Git-tracked set before creating an archive; ignored files that were force-added are still public and still checked.
4. Make the source archive from the reviewed commit, not by recursively zipping a working directory containing local state.
5. Inspect the extracted release with the same checker and ensure the MIT license and third-party notices are included.

Inside a Git repository, the checker scans tracked and unignored untracked files; before Git initialization it scans the source tree. It rejects runtime artifacts, unexpected JSON, binary files disguised as text, common credential patterns, and private hardcoded paths. It reports issue categories and source locations, not the suspected private value.

The checker is a guardrail, not a privacy or security proof. It does not know whether ordinary prose was copied from a private chat. A new exception needs a narrow justification and a meaningful test; do not disable a check simply to make a release green.

## Licensing

Contributions to original project code are provided under the MIT license in this repository. Preserve existing attribution and third-party notices. A dependency's license is independent from this project's MIT license; do not bundle native libraries, codecs, models, or vendor directories without checking their own redistribution terms.
