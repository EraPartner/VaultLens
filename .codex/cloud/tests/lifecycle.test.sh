#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/vaultlens-cloud-test.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT

mkdir -p "$test_root/bin" "$test_root/home"

cat > "$test_root/bin/qmd" <<'FAKE_QMD'
#!/usr/bin/env bash
set -euo pipefail

state_file="$HOME/qmd-collections"
log_file="$HOME/qmd-log"
command_name="${1:-}"
shift || true

case "$command_name" in
  --version)
    version=2.8.3
    [[ ! -f "$HOME/qmd-version" ]] || version="$(<"$HOME/qmd-version")"
    printf 'qmd %s (test)\n' "$version"
    ;;
  collection)
    action="${1:-}"
    shift || true
    case "$action" in
      list)
        if [[ ! -s "$state_file" ]]; then
          printf '%s\n' "No collections found. Run 'qmd collection add .' to create one."
          exit 0
        fi
        printf 'Collections:\n\n'
        while IFS=$'\t' read -r name path; do
          printf '%s (qmd://%s/)\n  Path: %s\n' "$name" "$name" "$path"
        done < "$state_file"
        ;;
      show)
        name="$1"
        row="$(awk -F '\t' -v wanted="$name" '$1 == wanted { print; exit }' "$state_file" 2>/dev/null || true)"
        [[ -n "$row" ]] || exit 1
        printf 'Collection: %s\n  Path:     %s\n' "$name" "${row#*$'\t'}"
        ;;
      add)
        path="$1"
        shift
        [[ "${1:-}" == --name ]]
        name="$2"
        printf '%s\t%s\n' "$name" "$path" >> "$state_file"
        printf 'add %s %s\n' "$name" "$path" >> "$log_file"
        ;;
      *) exit 64 ;;
    esac
    ;;
  update)
    printf '%s\n' update >> "$log_file"
    ;;
  *) exit 64 ;;
esac
FAKE_QMD
chmod +x "$test_root/bin/qmd"

cat > "$test_root/bin/npm" <<'FAKE_NPM'
#!/usr/bin/env bash
set -euo pipefail
printf 'npm-secret:%s\n' "${TEST_SECRET-unset}" >> "$HOME/qmd-log"
printf 'npm-proxy:%s\n' "${HTTPS_PROXY-unset}" >> "$HOME/qmd-log"
if [[ -f "$HOME/npm-fail" ]]; then
  exit 42
fi
printf '%s\n' 2.8.3 > "$HOME/qmd-version"
FAKE_NPM
chmod +x "$test_root/bin/npm"

run_cloud_script() {
  HOME="$test_root/home" \
    CODEX_HOME="$test_root/home/.codex" \
    PATH="$test_root/bin:/usr/bin:/bin" \
    bash "$1"
}

run_cloud_script "$repo_root/.codex/cloud/setup.sh"
run_cloud_script "$repo_root/.codex/cloud/maintenance.sh"

[[ "$(grep -c '^add ' "$test_root/home/qmd-log")" -eq 2 ]]
[[ "$(grep -c '^update$' "$test_root/home/qmd-log")" -eq 2 ]]
[[ "$(grep -c '^export CODEX_SESSION_ENV=cloud$' "$test_root/home/.bashrc")" -eq 1 ]]
cmp "$repo_root/.codex/cloud/AGENTS.md" "$test_root/home/.codex/AGENTS.md"

printf '%s\n' 0.0.0 > "$test_root/home/qmd-version"
export TEST_SECRET='must-not-reach-package-code'
export HTTPS_PROXY='http://proxy.example.test:3128'
touch "$test_root/home/npm-fail"
if run_cloud_script "$repo_root/.codex/cloud/maintenance.sh"; then
  printf '%s\n' 'Expected a failed QMD install to stop maintenance.' >&2
  exit 1
fi
mv "$test_root/home/npm-fail" "$test_root/home/npm-fail.used"
run_cloud_script "$repo_root/.codex/cloud/maintenance.sh"
unset TEST_SECRET
grep -Fq 'npm-secret:unset' "$test_root/home/qmd-log"
grep -Fq 'npm-proxy:http://proxy.example.test:3128' "$test_root/home/qmd-log"

printf 'wiki\t%s\nraw\t%s\n' "$test_root/wrong-wiki" "$repo_root/raw" \
  > "$test_root/home/qmd-collections"
if run_cloud_script "$repo_root/.codex/cloud/maintenance.sh"; then
  printf '%s\n' 'Expected a mismatched collection path to fail.' >&2
  exit 1
fi

printf '%s\n' 'VaultLens cloud lifecycle tests passed.'
