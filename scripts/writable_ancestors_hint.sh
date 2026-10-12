# Sourced by demo.sh and build_store.sh; defines one function, nothing runs on source.
# kkernel will not serve a store whose path has a group- or world-writable directory above it
# (a sticky directory such as /tmp is allowed). When it refuses, list the directories that qualify.
writable_ancestors_hint() {
  local d perm found=""
  d="$(cd "$1" && pwd -P)"
  while [ "$d" != "/" ]; do
    perm="$(ls -ld "$d" | cut -c1-10)"
    if { [ "${perm:5:1}" = w ] || [ "${perm:8:1}" = w ]; } && [ "${perm:9:1}" != t ] && [ "${perm:9:1}" != T ]; then
      found="$found $d"
    fi
    d="$(dirname "$d")"
  done
  if [ -n "$found" ]; then
    echo "kkernel refuses a store with a group- or world-writable directory above it. Writable here:$found" >&2
    echo "Clone into a directory that is not group- or world-writable (for example one with mode 755), or run chmod g-w on each directory listed above." >&2
  fi
}
