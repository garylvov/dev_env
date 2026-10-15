# Source with an ANSI banner path and optional plain-text fallback label.
# The subshell leaves caller variables, options, arguments, and cwd unchanged.
(
    case $- in
        *i*)
            if [[ -t 1 && -n ${1:-} && -r $1 ]]; then
                if [[ -n ${NO_COLOR:-} || ${TERM:-dumb} == dumb ]]; then
                    printf '%s\n' "${2:-${1##*/}}"
                else
                    cat -- "$1"
                fi
            fi
            ;;
    esac
)
