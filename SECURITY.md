# Security policy

## Scope

AIMAN Personal World v0.1.0 is an early, single-user local application. Its server binds to loopback, data APIs require a per-state session, and writes are limited to same-origin user reviews and local drafts. It is not designed for a public endpoint or multi-user hosting.

Application-level encryption at rest is not implemented. POSIX permissions help separate local accounts, but do not defend against a compromised user session, an administrator, browser extensions, or copied backups. Windows ACL verification is not implemented. Read [privacy.md](docs/privacy.md) for the current boundary.

We do not claim a completed independent security audit or guarantee a response time. Security fixes target the current source version; the project does not offer a maintained-version support matrix yet.

## Report privately when possible

Do not include a real archive, key, access token, database, media cache, contact list, or private screenshot in a public issue.

If the repository's **Security → Report a vulnerability** option is available, use that private reporting channel. Availability depends on repository settings; this file does not imply it is enabled. If no private channel is offered, open a minimal public issue asking the maintainer for a private reporting method, without exploit details or private data.

Provide a synthetic reproduction, affected version, operating system, expected boundary, and observed behavior. For an access-control issue, describe the relevant request shape using a fabricated token placeholder and artificial state, never a live credential.

## If private material was exposed

Remove public access and invalidate affected credentials through their owning application or service. A copied archive or decrypted media cannot be made unreadable by rotating a browser session token. Review repository history, release archives, issue attachments, logs, downloads, and backups where applicable. Avoid posting more private material while describing the incident.

The source release checker can detect common risky artifacts, but it cannot certify arbitrary content as safe. Report a bypass with a manufactured fixture rather than a real secret.

## Dependency and parser issues

Native SQLCipher bindings, image libraries, audio codecs, and FFmpeg are optional, independently installed components. Keep their licenses and update them according to their upstream guidance. A locally saved attachment is still untrusted input. This project does not download missing media or execute attachments to inspect them.
