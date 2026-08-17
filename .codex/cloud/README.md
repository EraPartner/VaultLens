# Codex cloud environment

Set the environment setup command to:

```bash
bash .codex/cloud/setup.sh
```

The setup installs the portable global working agreement, installs QMD when needed, registers the
tracked `wiki/` and `raw/` collections, and builds the keyword index. It deliberately skips
`qmd embed`, whose first run downloads a large model. Run it explicitly only when semantic search
is useful enough to justify the time and storage.

Brain is not part of this environment. Do not upload it as a repository, secret, mount, archive,
or setup-script input. Cloud work is limited to the public or tracked VaultLens template.
Host hooks and host-only Obsidian tools remain local.
