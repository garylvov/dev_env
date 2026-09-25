# Source this file to enable Token Kit now and in future Bash sessions.
# No client installation, project configuration, or model calls.
_token_kit_add_to_bashrc() {
    local kit_dir kit_bin kit_entry kit_rc
    if [[ -z ${HOME:-} ]]; then
        printf '%s\n' 'Token Kit: HOME is unset.' >&2
        return 1
    fi
    kit_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || return 1
    kit_bin="$kit_dir/src/token_kit/bin"
    if [[ ! -x "$kit_bin/token-kit" || "$kit_bin" == *:* ]]; then
        printf '%s\n' 'Token Kit: expected a checkout with an executable launcher and no colon in its path.' >&2
        return 1
    fi
    kit_rc="$HOME/.bashrc"
    printf -v kit_entry 'case ":$PATH:" in *:%q:*) ;; *) export PATH=%q:"$PATH" ;; esac' "$kit_bin" "$kit_bin"
    if [[ ! -f "$kit_rc" ]] || ! grep -Fqx -- "$kit_entry" "$kit_rc"; then
        printf '\n# Token Kit\n%s\n' "$kit_entry" >> "$kit_rc" || return 1
    fi
    case ":$PATH:" in
        *:"$kit_bin":*) ;;
        *) export PATH="$kit_bin:$PATH" ;;
    esac
    if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
        printf '%s\n' 'Token Kit added to ~/.bashrc. Open a new Bash terminal to use it.'
    else
        printf '%s\n' 'Token Kit is on PATH now and in future Bash terminals.'
    fi
}

if _token_kit_add_to_bashrc; then
    unset -f _token_kit_add_to_bashrc
else
    unset -f _token_kit_add_to_bashrc
    return 1 2>/dev/null || exit 1
fi
