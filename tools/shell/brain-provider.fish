function brain-provider --description 'Show or select the local Brain provider preference'
    set -l project (__brain_project_root)
    or return 1
    set -l python /opt/homebrew/bin/python3
    set -q BRAIN_PYTHON; and set python $BRAIN_PYTHON
    if not test -x "$python"
        set python (command -s python3)
    end
    if test (count $argv) -eq 0
        "$python" "$project/tools/llm_provider.py" show
    else
        "$python" "$project/tools/llm_provider.py" select $argv
    end
end
