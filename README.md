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

### Quick Ingestion

To subscribe to a list, copy the raw file URL from the desired maintainer folder and add it to your Pi-hole configuration via the web UI (**Group Management → Adlists**) or directly via CLI:

```bash
# Example: Adding a source list directly via command line
sqlite3 /etc/pihole/gravity.db "INSERT INTO adlist (address, enabled, comment) VALUES ('https://raw.githubusercontent.com/your-username/repository/main/maintainers/oisd/light.txt', 1, 'OISD Light');"
pihole -g
```

## Maintenance & Integrity

Lists are validated and updated on an automated cycle. Upstream sources are sanitized to ensure valid domain syntax and eliminate redundant entries before mirror syncing.
