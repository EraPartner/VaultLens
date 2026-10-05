# Local native agent runtime

VaultLens launches native Claude Code or Codex processes through the pinned Anthropic standalone
sandbox runtime (`@anthropic-ai/sandbox-runtime`, version `0.0.78`). The runtime boundary includes
file tools, shell commands, hooks, and the scoped search server. Access policy, provider command
syntax, role instructions, and launch routing are separate modules so a new provider or runtime
adapter can be introduced without rewriting privacy profiles.

Interactive sessions, headless roles, and scheduled jobs share this boundary. No container launcher is supported. There is no automatic provider switch or unsandboxed fallback.
Native subagents inherit the parent process's operating-system boundary. A narrower role prompt
does not create independent isolation; use a separate scoped run for that purpose.

Native clients start in an immutable private workspace containing links to approved paths and
trusted tools. Their logical vault paths and write grants still point to the original vault.
This keeps ambient vault/provider configuration out of native startup and preserves relative
note and tool paths. The operating-system sandbox checks the original link targets; the view
cannot grant access. Codex ancestor project discovery is disabled for this private workspace.
Interactive Codex trusts this generated view so it does not prompt to change its native execution
mode. This does not trust or import the original vault's provider configuration.
The view includes existing approved paths. When a custom profile selects a path that does not
exist yet, create it through its absolute vault path, or create its parent before launching.

## Installation and verification

Use Python 3.11 or newer and an installed native provider CLI. The standalone runtime requires
Node.js 22.12 or newer plus macOS Seatbelt, or bubblewrap, socat, and ripgrep on Linux. Installation is an explicit
operator action:

```sh
bash tools/runtime/install.sh
python3 tools/local_runtime.py doctor
python3 tools/runtime/probe.py
```

The package and its whole dependency tree are locked in `tools/runtime/package-lock.json` with
integrity hashes; the installer runs `npm ci`, which installs exactly that tree and disables
package scripts, audit, and funding requests. To review a new release, regenerate the lockfile
with `npm install --package-lock-only --ignore-scripts` in `tools/runtime/` and review its diff.
`doctor` checks the package version and operating-system prerequisites. A synthetic isolation probe
must establish real denied reads, denied writes, process behavior, and network confinement on the
deployment host. Each run also performs a confinement preflight before loading note content.
Portable tests and a successful package installation do not establish operating-system isolation.

Ordinary launches also require a complete successful probe receipt under
`tools/runtime-state/verification.json`. The probe fingerprints the installed runtime, operating
system, dependencies, launcher sources, and access policy before checks and rechecks them before
recording. Any failed or skipped check prevents a receipt. Changed evidence requires a fresh probe.
Starting another probe revokes any older receipt before checking prerequisites.
This local receipt is a launch gate, not cryptographic attestation or a proof of every possible
provider behavior. Native provider file tools remain a separate verification item.

The probe uses a temporary synthetic vault and the installed pinned runtime, with no provider CLI,
credentials, or personal notes. It exercises Python, spawned shells, links, scoped search and MCP,
and unapproved public network access. Missing prerequisites stop the probe before fixtures are
created. An unavailable public connectivity baseline marks network checks as skipped; it is not
evidence that a network boundary passed. Any failed or skipped required check leaves isolation
unverified.
Lifecycle checks use a start handshake and long-lived finite heartbeats for attached and detached
children. They test normal success and cancellation through both runtime and headless invocation
paths. Cancellation must stop the tracked fixture children and persist a gate that refuses another
launch. A detached child that survives normal return fails the probe. Generic standalone runtime
cleanup behavior remains unverified until these checks pass on the actual deployment host.

On macOS, a private temporary launchd job supplies a fresh kernel audit session for each
supervised process. Detached descendants retain that membership. The trusted supervisor signals
complete audit identities, preventing process ID reuse from redirecting cleanup. Supplemental
rules deny audit-session creation, job creation, credential-service lookup, and shared POSIX
interprocess communication. The host probe checks these denials against existing public services
and synthetic shared memory. Separate synthetic tests check terminal transport; native provider
interfaces require their own checks.
Interactive clients can read, write, and control only the supervisor's existing private terminal
device. The supervisor retains its open descriptor until cleanup. This does not grant other host
terminals or permission to allocate new pseudo-terminals.
Linux uses the runtime's bubblewrap process boundary and must pass its own host probe.
This does not establish the semantics of every permitted Apple service. Joining another audit
session requires a foreign session right; the denial of `system-audit` alone does not prove that
every possible way of obtaining such a right is blocked.
If a new unrelated protected process refuses identity inspection, cleanup verification stops
conservatively. Exact process lifetimes captured before the new session are the only exclusions.
Unconfirmed cleanup retains the private temporary run outside iCloud, records its location,
and blocks subsequent work until the operator verifies cleanup.

Codex delegates its native operating-system sandbox to this verified outer runtime because macOS
cannot nest the two Seatbelt sandboxes. Its native setting is `danger-full-access` only after
the mandatory outer preflight passes; approval, tool and network controls remain in place.
Direct command builders retain their native scoped modes.

The standalone runtime remains a research preview. A successful installation does not activate
agent work: the complete host probe must pass first. Authentication and native provider file tools
are checked separately before unattended work is enabled.

## Topgrade maintenance

Run python3 tools/runtime/maintain.py to install a missing or outdated runtime
using the version reviewed by this checkout. It renews verification after changes
to the runtime, policy, macOS, Node or Python, and skips the full probe when the
current receipt still matches. It also checks public npm metadata for a different
release and reports it for compatibility review; it never changes the reviewed
version or guard automatically.

Maintenance holds an exclusive lock; agent runs hold shared locks. Updates refuse
to run while an agent is active. Failed installation or verification leaves agent
launches blocked. Synthetic diagnostics are saved under tools/runtime-state/maintenance/.
Neither authentication nor notes are loaded.

The operator's Topgrade command runs this helper separately in VaultLens and Brain
after package updates. The flags --check --skip-release-check check local evidence
without installing, probing or making network requests.

## Provider authentication

Provider authentication is stored outside the vault and iCloud sync, with private permissions.
On macOS the dedicated store is
`~/Library/Application Support/VaultLens/agent-state/<vault-path-hash>/providers/<provider>/`;
on Linux it is `~/.local/state/vaultlens/agent-state/<vault-path-hash>/providers/<provider>/`.
The SHA256 hash uses the canonical vault path, keeping each vault and provider separate.
The launcher uses the account's real home and refuses directory aliases. It is independent
of the operator's regular Claude and Codex homes. Legacy dedicated stores inside the vault
are never read or copied automatically; authenticate afresh in the new store. A failed
directory or privacy check stops the launch.
Each run uses a disposable home and provider directory. Only allowlisted authentication files
transfer between the dedicated store and that run; history, caches, rules, and configuration remain
ephemeral. A provider lock serializes authentication refresh for the selected CLI, and a vault
writer lock serializes edits. Unrelated home files, cloud credentials, SSH keys and sockets,
shell initialization, and host hooks are excluded. Do not copy host credential stores into runtime state.
The public file allowlist is `auth.json` for Codex and `.credentials.json` for Claude. Authentication
contents never appear in probe reports or deployment plans.
Authentication is copied through retained directory descriptors. Replaced directories cannot
redirect the host refresh to unrelated files. The selected client's token is accessible to that
client and its tools; the boundary protects other credentials, not a client's own authentication.

After isolation checks pass, authenticate with the native provider's login command through scoped
`local_runtime.py exec --cli <provider>`. The selected CLI controls which isolated state and
model/login endpoints are available. Authentication is a separate check; a synthetic filesystem
probe cannot verify model access, account limits, or provider login.
Use the persistent login helper from your own interactive terminal:

```sh
bash tools/scripts/authenticate-provider.sh codex
# Use claude instead when that account is available.
```

The helper selects no notes, runs from the disposable private home, and uses the
runtime's isolated authentication store. Codex probes three system policy filenames
even during login. The launcher permits those exact missing-file checks so they
return `ENOENT`; existing files or directory aliases stop the run for review.
The missing parent and macOS's `/etc` symlink get metadata access only; directory
contents are never granted. Native management policy can contain credentials, so
it must not be imported as ordinary tool data.
On macOS, Codex uses the public system PEM certificate bundle through
`SSL_CERT_FILE=/private/etc/ssl/cert.pem`. This selects its Rustls transport and
keeps certificate validation active without opening the host security service.
Ambient certificate overrides are not imported.
For Codex, use `exec --private-cwd --profile selected-read --cli codex -- codex -c 'cli_auth_credentials_store="file"' login --device-auth`
to use the isolated file store and avoid a local callback listener. Login flows needing extra endpoints, Keychain access, or local binding
remain unsupported until a reviewed adapter provides them. Host logins are never imported.

## Access profiles

`tools/access-profiles.json` is the versioned shared policy. The gitignored
`tools/access.local.json` supplies operator definitions and role defaults. Local definitions replace
matching shared profiles; `extends` explicitly adds inherited read, write, deny, and research-domain
lists. Denials and mandatory protected paths take precedence. Unknown fields, inheritance cycles,
traversal, invalid project slugs, and symbolic-link selections stop a launch.
`deny_read` accepts exact filenames and subtrees, including future descendants, and rejects globs.
An empty inherited grant list does not revoke a parent's grants; start a fresh profile for narrower
access.

A local policy can add a strict selection and a separately reviewed research profile:

```json
{
  "version": 1,
  "profiles": {
    "selected-project-notes": {
      "extends": "selected-read",
      "read": ["wiki/topics/example.md", "projects/{project}/project.md"],
      "deny_read": ["projects/{project}/private"],
      "reports": "wiki/reports/agents"
    },
    "project-research": {
      "extends": "project-write",
      "research_domains": ["example.org:443"]
    }
  }
}
```

Select these with `--access-profile` and `--project`. Research domains add network access;
they do not add note reads or writer capabilities. Editing the policy requires a fresh host probe.

```sh
python3 tools/local_runtime.py profiles
python3 tools/local_runtime.py plan --profile selected-read --read-path wiki/concepts/example.md
python3 tools/local_runtime.py plan --profile project-write --project example
```

The resolved plan lists concrete reads, writes, report output, and research domains. Review it before
disclosing personal notes. A read-only grant can still send those notes to the selected provider.
Local process execution does not imply local model inference.

The basic profiles are `selected-read`, `wiki-read`, `source-read`, `cos-read`, `wiki-write`, and
`project-write`. `selected-read` is the base for strict file selections. `--read-path` adds selections
to a profile; it does not remove inherited reads. Use `selected-read` when the run should see only
the supplied notes. The other read profiles add wiki pages, approved sources, or project planning
files. The consent queue permits metadata listings only when a profile explicitly enables them.
Interactive vault sessions default to `wiki-read`. Request wiki editing explicitly with
`--access-profile wiki-write`; a session inside a recognized project uses that project's
`project-write` scope. Headless roles keep their declared read or write capabilities.

`wiki-write` grants wiki changes while keeping sources immutable. `project-write --project <slug>`
grants exactly that project's changes and reads relevant wiki material. Protected files include
raw sources, the consent queue, tools, instructions, Obsidian configuration, Git metadata, and
credential files. Runtime report and recovery directories are separate from note write grants.
The trusted parent records headless stdout in the profile's report folder and echoes it live.
Each report has private permissions, run provenance, and a four MiB limit. Failed, truncated,
or incomplete capture is marked partial. Agent file tools do not gain a report write grant.
Writer runs retain snapshots under `tools/runtime-state/backups/`, and only the newest ten are
kept (older ones are removed after each new snapshot); inspect the reported snapshot and
resulting diff before accepting automated changes.
Automatic reports stay under `wiki/reports/agents/`, with optional nested folders. Every run
excludes that reserved subtree from reads and search, so derived private context does not enter
another agent's corpus through its report. Review a report before promoting its content into notes.
Unconfirmed descendant cleanup writes `tools/runtime-state/cancellation-unconfirmed.json` and
blocks further launches. An operator must verify that the recorded process group is gone before
removing that marker. The marker names the kept `run_directory`; it holds a copy of the provider
login and run scratch, so delete it once cleanup is confirmed. The runtime never clears either
automatically.
Job removal allows a bounded two-second wait for launchd to finish teardown. A repeated cleanup
request preserves the original failure instead of replacing it after the guardian has stopped.
Raw PDFs are extracted to private scratch storage. Agent preprocessing does not modify raw files
or automatically promote an inbox document.

Ordinary analysis exposes only the selected provider's model and login endpoints. Web research is
opt-in through a separate profile's explicit `research_domains` and local shell networking.
Hosted provider WebSearch/WebFetch tools remain disabled because server-side browsing cannot
enforce this local domain list. Model choice and native tool approval cannot widen that
process-level network or filesystem policy.

## Search

Every run builds an isolated lexical corpus from its resolved approved files. A qmd-compatible CLI
and Model Context Protocol (MCP) server search that corpus inside the same process boundary. The
runtime does not reuse a shared vault index, host MCP configuration, or vector-model cache.
Blocking an original file is insufficient if an index retains a copy; the isolated corpus prevents
that leak. Hybrid full-vault qmd search remains an explicit operator workflow.

`tools/scripts/provider-smoke.py --root <vault> --profile wiki-read` checks the whole-process
preflight and scoped lexical search without a model call. `--provider claude|codex --run-provider`
opts into a literal synthetic response and can consume paid usage. This smoke check does not
establish provider file-tool confinement. Keep that result separate from the synthetic runtime
probe and deployment checks.

## Reviewable deployment

`deploy.py plan` reads only an explicit tools bundle and exported instruction/adapter candidates.
It reports exact source and destination hashes without modifying the destination. It does not
read notes, raw material, local model/access preferences, or provider credential state.

```sh
python3 tools/runtime/deploy.py plan \
  --source /path/to/VaultLens \
  --destination /path/to/Brain \
  --instruction-candidates /path/to/instruction-export \
  --adapter-candidates /path/to/adapter-export > /tmp/vaultlens-deployment-plan.json

# Review the plan and each replacement before applying it.
python3 tools/runtime/deploy.py apply --plan /tmp/vaultlens-deployment-plan.json
```

Apply writes only the allowlisted native tools, their Python dependencies, runtime documentation,
and source fish wrappers below the destination's `tools/`. Local `llm.local.json`, `access.local.json`,
existing provider state, notes, raw files, and unrelated tools remain intact. Replaced regular files
are preserved under `tools/runtime-state/deployments/<id>/replaced/`. Unchanged files are left
untouched. A deployment lock prevents concurrent applies, and source/destination hashes are
rechecked after review. Copies are atomic and verified by hash readback. Ordinary failures restore
completed replacements; conflicts with a concurrent edit are recorded instead of overwritten.

Protected root instructions and native `.claude`/`.codex` adapters are copied as inert `.candidate`
files under `tools/runtime/migration/<id>/`. Their manifest records the intended final paths and
hashes. Normal apply does not write protected root paths, installed fish functions, or launchd files. It does not install packages, authenticate providers, start jobs, or publish Git.

An operator must review and run this separate command outside an agent session to apply protected
instructions and adapters and replace only the declared installed Brain fish functions:

```sh
python3 /path/to/Brain/tools/runtime/deploy.py operator-apply \
  --migration /path/to/Brain/tools/runtime/migration/DEPLOYMENT_ID \
  --fish-functions "$HOME/.config/fish/functions"
```

That command validates staged hashes and exact targets, refuses symbolic links, and snapshots
previous instruction/adapters and fish functions before replacement. Omit `--fish-functions` to
leave installed functions pending. Start a fresh shell after reviewing the result.

Review remaining provider integration (fish wrappers, `.gitignore` entries, scheduler plist
provider keys) through the host repair plan:

```sh
python3 /path/to/Brain/tools/scripts/repair-provider-host.py --vault /path/to/Brain --diff
python3 /path/to/Brain/tools/scripts/repair-provider-host.py --vault /path/to/Brain --apply
```

The repair plan keeps originals in its backup and never touches unrelated files. Old installed
`vaultlens-claude`, `vaultlens-codex` and `vaultlens-shell` functions are no longer migrated;
remove them by hand. Container-bundle retirement was removed from `deploy.py`, and a plan that
still carries retirement entries is rejected. These commands are manual operator actions. Host
scheduler configuration still requires a separate explicit operator action.

Keep unattended work blocked until protected candidates, installed wrappers, native authentication,
and operating-system isolation have been checked. A tools deployment with pending operator files
is a partial migration, and its manifest reports that state explicitly.
