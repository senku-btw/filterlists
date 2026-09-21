# Pi-hole Blocklists

A structured, production-ready directory of network-wide blocking rulesets organized primarily by upstream maintainer, aggregator, and source origin rather than content category. 

Designed for network administrators who prefer explicit control over source provenance, this repository streamlines blocklist management by grouping hosts files according to their trusted maintainers (such as StevenBlack, OISD, Firebog, and AdGuard). Thus, structuring lists around upstream reliability mechanics, users can easily construct granular, high-integrity blocking configurations.

---

## Structure & Usage

Each directory within this repository corresponds to a distinct source maintainer or aggregator, containing curated list variants tailored for direct consumption by Pi-hole's `gravity` subsystem:

```
├── maintainers/
│   ├── OISD
│   ├── StevenBlack
│   └── The Firebog
│   └── The Block List Project
│   └── etc.
```
