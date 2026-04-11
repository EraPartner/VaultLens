# Second Brain Wiki System

A template for Karpathy's LLM Wiki pattern - ready to use as a Second Brain.

## What's Included

- `AGENTS.md` - Operating schema
- `wiki/` - Wiki structure with templates
- `tools/` - Maintenance CLI tools

## What's NOT Included

Raw sources and wiki content go elsewhere - they're your data.

## Quick Setup

```bash
# Clone or copy the system
git clone https://github.com/yourusername/second-brain-system.git your-wiki
cd your-wiki

# Initialize fresh directories for YOUR data
mkdir -p raw/sources raw/assets raw/inbox
mkdir -p wiki/sources wiki/entities wiki/concepts wiki/topics

# Open in Obsidian
open .
```

## Directory Structure After Setup

```
your-wiki/
├── AGENTS.md              # Schema (copy this)
├── raw/
│   ├── sources/          # YOUR sources
│   ├── assets/           # YOUR assets
│   └── inbox/            # YOUR inbox
├── wiki/                 # YOUR wiki content
│   ├── sources/
│   ├── entities/
│   ├── concepts/
│   ├── topics/
│   ├── syntheses/
│   ├── comparisons/
│   ├── queries/
│   ├── reports/
│   ├── system/
│   ├── _templates/
│   ├── index.md
│   └── log.md
└── tools/                # Copy this
    ├── wiki.py
    ├── wiki_extra.py
    └── scripts/
```

## Syncing

Add your data directories to your own git repo:

```bash
git init
echo "raw/
wiki/
!.gitkeep" > .gitignore
git add .
git commit -m "Initial wiki with my data"
```

## See Also

- Original pattern: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f