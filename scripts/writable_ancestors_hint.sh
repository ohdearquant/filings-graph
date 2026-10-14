# Sourced by demo.sh and build_store.sh; defines one function, nothing runs on source.
# kkernel will not serve a store whose path has a group- or world-writable directory above it, and its
# model loader applies the same rule to the embedding model under ~/.lattice/models (a sticky directory
# such as /tmp is allowed). When either refuses, list the directories that qualify.
writable_ancestors_hint() {
  local start d perm found=""
  for start in "$1" "$HOME/.lattice/models"; do
    [ -d "$start" ] || continue
    d="$(cd "$start" && pwd -P)"
    while [ "$d" != "/" ]; do
      perm="$(ls -ld "$d" | cut -c1-10)"
      if { [ "${perm:5:1}" = w ] || [ "${perm:8:1}" = w ]; } && [ "${perm:9:1}" != t ] && [ "${perm:9:1}" != T ]; then
        case " $found " in *" $d "*) ;; *) found="$found $d" ;; esac
      fi
      d="$(dirname "$d")"
    done
  done
  if [ -n "$found" ]; then
    echo "kkernel refuses a store or model file with a group- or world-writable directory above it. Writable here:$found" >&2
    echo "Clone into a directory that is not group- or world-writable (for example one with mode 755), or run chmod g-w on each directory listed above." >&2
  fi
}
