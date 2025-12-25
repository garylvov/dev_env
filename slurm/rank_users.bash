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
' | sort -k2 -nr

