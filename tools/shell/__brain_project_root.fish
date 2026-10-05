function __brain_project_root --description 'Resolve the nearest VaultLens vault for brain-* wrappers'
    # A vault is identified by its operating schema and Python entry points.
    # Container images and private launcher names are not part of this contract.
    set -l current "$PWD"
    while test "$current" != "/" -a "$current" != ""
        if test -f "$current/AGENTS.md"; and test -f "$current/tools/wiki.py"; and \
                test -f "$current/tools/agents/wiki-agent.py"
            printf '%s\n' "$current"
            return 0
        end
        set current (command dirname "$current")
    end

    # No built-in default location: outside a vault checkout, BRAIN_HOME must name the vault.
    set -l fallback ""
    set -q BRAIN_HOME; and set fallback "$BRAIN_HOME"
    if test -z "$fallback"; or not test -f "$fallback/AGENTS.md"; or not test -f "$fallback/tools/wiki.py"; or \
            not test -f "$fallback/tools/agents/wiki-agent.py"
        printf 'brain: vault markers missing at %s (AGENTS.md, tools/wiki.py, tools/agents/wiki-agent.py)\n' "$fallback" >&2
        printf '  (set BRAIN_HOME to override the project root)\n' >&2
        return 1
    end
    printf '%s\n' "$fallback"
end
