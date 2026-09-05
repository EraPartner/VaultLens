# Codex Cloud Working Agreement

This overlay is appended to the global agreement by `setup.sh`. The repository root `AGENTS.md`
also applies. Do not duplicate either source here.

## Changes and pull requests

- Preserve unrelated user changes in a dirty worktree.
- Explain what changed and why, and leave a focused diff for review.
- Do not add an AI co-author line.
- Do not run `git add`, `git commit`, `git tag`, `git push`, or `gh pr create` from the cloud
  terminal.
- Do not configure, disable, or work around commit signing in a cloud session.
- Codex may create a pull request through the platform-managed **Open pull request** action.
- The **Open pull request** action is a post-task platform control. It does not need to appear as a
  terminal command, MCP resource, or agent-visible `make_pr` tool. Its absence during the agent run
  is not an implementation blocker; finish the reviewed diff for the platform handoff.
- In a pull-request-linked cloud task, inspect the current diff, review comments, and checks. Make
  requested in-scope changes, rerun relevant portable checks, and let the connected GitHub
  integration update the same pull request branch when repository permissions allow it.
- When the user explicitly asks to merge that pull request, refresh its current state first.
  Merge only through the connected GitHub integration. Required checks must pass, required approvals
  must be present, no blocking review may remain, and the integration must expose the required
  permission. Do not use an admin bypass or override. Do not merge a different pull request or
  report success without reading back the resulting pull request state.
- Do not change repository settings or branch protections, or update a default or protected branch
  directly outside the approved pull request merge.
- Following a pull request means handling its current state and user-triggered follow-ups. Do not
  claim continuous monitoring unless a separate automation is configured.

## Git handoff in Codex cloud

- Finish the requested changes and portable checks, then leave the worktree ready for review. Any
  Git publication needed for the **Open pull request** action or a pull-request-linked follow-up
  happens through the connected remote service, not shell commands in the cloud environment, and
  it does not use the local Secure Enclave signing flow.
- A missing remote, upstream, shell push credential, or signing key is expected and is not a task
  blocker. Do not add or rewrite a remote merely to make shell Git operations work.
- Do not run `gh auth`, request `GH_TOKEN` or `GITHUB_TOKEN`, or persist a Git credential in the
  cloud container.
- Never ask for Touch ID, a Secure Enclave key, KeePassXC, or another host credential from a cloud
  session. Report host-only commit, signing, and push steps as not run.
- If the user explicitly requires terminal Git publishing, a direct protected-branch update, or a
  merge that the connected integration cannot perform, explain that the work must continue through
  the appropriate reviewed workflow. Do not weaken or bypass this cloud policy.
