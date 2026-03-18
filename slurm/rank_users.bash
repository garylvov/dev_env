squeue -o "%u %t %b" | \
awk '
$2 == "R" {
    # Match gpu:<count> OR gpu:<model>:<count>
    if (match($3, /gpu(:[A-Za-z0-9_-]+)?:([0-9]+)/, m)) {
        gpus = m[2] + 0
        user[$1] += gpus
    }
}
END {
    for (u in user)
        print u, user[u]
}
' | sort -k2 -nr | while read -r uname gpus; do
    gecos=$(getent passwd "$uname" | cut -d: -f5)
    email="$gecos"
    # Derive full name from email (part before @, underscores to spaces, title case)
    fullname=$(echo "$email" | sed 's/@.*//' | tr '_' ' ' | sed 's/\b\(.\)/\u\1/g')
    printf "%-15s %4s GPUs   %-25s %s\n" "$uname" "$gpus" "$fullname" "$email"
done

