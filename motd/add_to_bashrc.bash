# Source from ~/.bashrc to display the OSCAR banner in interactive terminals.
# The subshell keeps the caller's variables, options, and working directory intact.
(
    case $- in
        *i*)
            if [[ -t 1 ]]; then
                if [[ -n ${NO_COLOR:-} || ${TERM:-dumb} == dumb ]]; then
                    printf 'OSCAR\n'
                else
                    cat -- "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/oscar.ans"
                fi
            fi
            ;;
    esac
)
