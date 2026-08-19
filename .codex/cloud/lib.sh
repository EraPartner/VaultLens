#!/usr/bin/env bash

VAULTLENS_QMD_VERSION="2.8.3"

vaultlens_package_env() {
  local name
  local -a clean_env=(
    env -i
    "HOME=$HOME"
    "PATH=$PATH"
    "CODEX_SESSION_ENV=${CODEX_SESSION_ENV:-cloud}"
  )
  for name in \
    HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY \
    http_proxy https_proxy all_proxy no_proxy \
    SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS \
    REQUESTS_CA_BUNDLE CURL_CA_BUNDLE; do
    if printenv "$name" >/dev/null 2>&1; then
      clean_env+=("$name=${!name}")
    fi
  done
  "${clean_env[@]}" "$@"
}

vaultlens_qmd_version() {
  vaultlens_package_env qmd --version 2>/dev/null | awk 'NR == 1 { print $2 }'
}

vaultlens_ensure_qmd() {
  local installed_version=""
  if command -v qmd >/dev/null 2>&1; then
    installed_version="$(vaultlens_qmd_version || true)"
  fi

  if [[ "$installed_version" != "$VAULTLENS_QMD_VERSION" ]]; then
    printf 'Installing QMD %s (found %s).\n' \
      "$VAULTLENS_QMD_VERSION" "${installed_version:-none}"
    # Prefer npm for QMD's native Node addons; older Bun releases can omit build helpers.
    if command -v npm >/dev/null 2>&1; then
      vaultlens_package_env npm install --global --no-fund --no-audit \
        --fetch-retries=2 --fetch-timeout=60000 \
        "@tobilu/qmd@$VAULTLENS_QMD_VERSION"
    elif command -v bun >/dev/null 2>&1; then
      vaultlens_package_env bun install --global "@tobilu/qmd@$VAULTLENS_QMD_VERSION"
    else
      printf '%s\n' 'Bun or npm is required to install QMD.' >&2
      return 1
    fi
  fi

  installed_version="$(vaultlens_qmd_version)"
  if [[ "$installed_version" != "$VAULTLENS_QMD_VERSION" ]]; then
    printf 'QMD verification failed: expected %s, resolved %s at %s.\n' \
      "$VAULTLENS_QMD_VERSION" "${installed_version:-unknown}" \
      "$(command -v qmd 2>/dev/null || printf missing)" >&2
    return 1
  fi
}

vaultlens_ensure_collection() {
  local name="$1"
  local requested_path="$2"
  local expected_path listing details actual_path

  expected_path="$(cd "$requested_path" && pwd -P)"
  listing="$(vaultlens_package_env qmd collection list)"

  if printf '%s\n' "$listing" | grep -Fq "$name (qmd://$name/)"; then
    details="$(vaultlens_package_env qmd collection show "$name")"
    actual_path="$(printf '%s\n' "$details" | awk -F': +' '$1 ~ /^[[:space:]]*Path$/ { print $2; exit }')"
    if [[ -z "$actual_path" || "$actual_path" != "$expected_path" ]]; then
      printf "QMD collection '%s' points at '%s', expected '%s'. Refusing to reuse it.\n" \
        "$name" "${actual_path:-unknown}" "$expected_path" >&2
      return 1
    fi
    return 0
  fi

  vaultlens_package_env qmd collection add "$expected_path" --name "$name"
  details="$(vaultlens_package_env qmd collection show "$name")"
  actual_path="$(printf '%s\n' "$details" | awk -F': +' '$1 ~ /^[[:space:]]*Path$/ { print $2; exit }')"
  if [[ "$actual_path" != "$expected_path" ]]; then
    printf "QMD collection '%s' verification failed after creation.\n" "$name" >&2
    return 1
  fi
}

vaultlens_cloud_prepare() {
  local phase="$1"
  local script_dir repo_root codex_home
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "$script_dir/../.." && pwd)"
  codex_home="${CODEX_HOME:-$HOME/.codex}"

  export CODEX_SESSION_ENV=cloud
  install -d "$codex_home"
  install -m 0644 "$script_dir/AGENTS.md" "$codex_home/AGENTS.md"
  touch "$HOME/.bashrc"
  grep -Fqx 'export CODEX_SESSION_ENV=cloud' "$HOME/.bashrc" || \
    printf '%s\n' 'export CODEX_SESSION_ENV=cloud' >> "$HOME/.bashrc"

  vaultlens_ensure_qmd

  cd "$repo_root" || exit 1
  vaultlens_ensure_collection wiki "$repo_root/wiki"
  vaultlens_ensure_collection raw "$repo_root/raw"
  vaultlens_package_env qmd update

  printf 'VaultLens cloud %s complete. Brain was not accessed; embeddings were skipped.\n' "$phase"
}
