function __brain_launch --description 'Run Brain commands through the local access-profile runtime'
    set -l project (__brain_project_root)
    or return 1
    set -l python /opt/homebrew/bin/python3
    set -q BRAIN_PYTHON; and set python $BRAIN_PYTHON
    if not test -x "$python"
        set python (command -s python3)
    end
    "$python" "$project/tools/brain_launch.py" --execute --root "$project" $argv
end
