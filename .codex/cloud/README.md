# Codex cloud environment

Set the environment lifecycle commands to:

```bash
# Setup script
bash .codex/cloud/setup.sh

# Maintenance script
bash .codex/cloud/maintenance.sh
```

Setup installs the portable global working agreement and the reviewed QMD 2.8.3 package version,
verifies the resolved binary, registers the tracked `wiki/` and `raw/` collections, and builds the
keyword index. Maintenance repeats the same idempotent checks after Codex restores a cached
environment on another branch, then refreshes the index. A collection name that points at another
checkout is an error rather than being silently reused.

QMD installation, lifecycle scripts, and index commands run with a sanitized environment. They
receive only `HOME`, `PATH`, `CODEX_SESSION_ENV`, and standard proxy or certificate variables; other
cloud setup secrets are not exposed to package code.

Both phases deliberately skip `qmd embed`, whose first run downloads a large model. In cloud
sessions, lead with `qmd search "<keywords>"`; it uses the prepared keyword index and needs no model.
Run `qmd embed` explicitly only when semantic search is useful enough to justify the time and
storage, and use `qmd query` only after that completes.

Brain is not part of this environment. Do not upload it as a repository, secret, mount, archive,
or setup-script input. Cloud work is limited to the public or tracked VaultLens template.
Host hooks and host-only Obsidian tools remain local.

The lifecycle behavior has a focused offline test:

```bash
bash .codex/cloud/tests/lifecycle.test.sh
```

## Pull request lifecycle

Use the platform-managed **Open pull request** action to create a pull request. A
pull-request-linked cloud task may inspect comments and checks, make in-scope follow-up changes,
and let the connected GitHub integration update the same branch. When the user explicitly asks to
merge, the integration may do so only after required checks and approvals pass and no blocking
review remains. Never use an admin bypass or directly update a protected branch.
