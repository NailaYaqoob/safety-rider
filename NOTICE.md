# Ownership and licensing

This repository contains **two layers of code with different owners.** Read this
before reusing anything from it.

---

## 1. The upstream template — FortyGuard's

| | |
|---|---|
| **What** | `fortyguard/`, `notebooks/`, `docs/`, `data/`, `assets/`, `LICENSE` |
| **Owner** | FortyGuard, Inc. |
| **Licence** | MIT — see [LICENSE](LICENSE) |
| **Source** | [FortyGuard-Tech/temperature-api-quickstart](https://github.com/FortyGuard-Tech/temperature-api-quickstart) |

Used as provided. The MIT licence in `LICENSE` governs this layer and is
unchanged; nothing in this file alters it.

---

## 2. Safety Rider — the original work

| | |
|---|---|
| **What** | `safety_rider/`, `tests/`, `requirements-service.txt`, this file, `CONTRIBUTORS.md`, `README.md` |
| **Owner** | © 2026 Naila Yaqoob |

### The governing terms

Ownership and the licence granted to FortyGuard are set by the **FortyGuard
Hackathon'26 Participant Handbook**, which states in full:

> **4. Your project:** You own what you build. By submitting, you let us show
> and share your project and your team's name to run and promote the hackathon.
> Your work must be your own, use our Temperature data, and not copy anyone
> else's.

That paragraph is the agreement. This file records it and does not extend it.

### What it means

**You own what you build.** Ownership of Safety Rider rests with the author.
Building on FortyGuard's MIT-licensed template does not transfer it, and
submitting to the hackathon does not either.

**FortyGuard may show and share the project and the author's name** for the
purpose of running and promoting the hackathon — demos, galleries, write-ups,
social posts, and similar promotional use.

**The grant is limited to that purpose.** It is not a transfer of copyright, not
an exclusive licence, and not permission to sell the work or build commercial
derivatives from it. The author remains free to use, publish, license, or
commercialise this work however she chooses.

**The work is original.** `safety_rider/` and `tests/` were written for this
project and are not copied from anyone else, as the handbook requires.

### Attribution

When showing or sharing, please credit:

> **Safety Rider** — Naila Yaqoob ([@NailaYaqoob](https://github.com/NailaYaqoob)),
> built on the FortyGuard Temperature API.

### Everyone else

No licence to Safety Rider is granted to anyone other than FortyGuard by this
file. If you want to use it, ask the author.

---

## 3. FortyGuard's API and data

Per handbook rule 3, FortyGuard's data, API, and models remain theirs, API
access is granted for this hackathon project only, and access ends when the
hackathon ends. The handbook also requires the API key to be kept private:

- No credential is committed to this repository. `.env` is git-ignored;
  `.env.example` contains placeholders only.
- Cached API responses fetched at runtime (`data/heatmaps/heatmap_rider_*`,
  `data/env_params/env_params_rider_*`) are git-ignored.
- Sample responses that ship with the upstream template are FortyGuard's and
  are covered by the MIT licence above.

---

## A note on precedence

This file is a plain-language record of the handbook's terms, written for people
reading the repository. It is **not legal advice** and it is **not a separate
agreement**. Where anything here differs from the Participant Handbook or any
other document signed with FortyGuard, **those govern.**

---

*Last updated: 2026-08-22*
