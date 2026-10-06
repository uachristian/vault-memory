---
author: owner
created: 2026-01-01T00:00:00+00:00
updated: 2026-01-01T00:00:00+00:00
source: owner
status: published
tags: [meta, private]
---

# private/ — sealed personal lane

Personal material (health, finances, family, journals) lives here, separate
from shared knowledge.

- **Sealed by default.** The plugin denies `private/` (config `private_paths`)
  to every profile for search, read, prefetch and capture.
- **Grant per profile.** List a profile in `private_profiles` to give it *read*
  access. Captures are still never written here (it is neither a direct-write
  nor a trusted-promotion lane).
- **No leakage into general context.** Even a granted profile only receives
  private notes in automatic per-turn context when the message matches
  `private_intent_terms` (or the profile is in
  `private_autocontext_profiles`). Explicit search still respects the grant.
- **No links out.** Shared notes may not name private notes as `related_hub`
  or `related` peers.
- You may add more sealed lanes (e.g. a stricter `private-sealed/`) by listing
  them in `private_paths`; keep them out of `private_profiles` entirely.
