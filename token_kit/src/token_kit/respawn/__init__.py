"""Lane respawn reader: the consumer for the recycler's RESPAWN_REQUEST rows.

Entry point `bin/respawn-reader`, logic in `reader.py`. The behaviour spec is
reference_bash/lane_recycler/respawn_reader.sh and the language-neutral case
file `respawn_cases.jsonl`, which this port passes unchanged.
"""
