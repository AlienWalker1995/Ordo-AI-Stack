# Read a secret the way the render delivers it, for our shell entrypoints (source this file).
#
# A file-delivered secret (ordo/secret_files.py) reaches a container as a read-only file under
# /run/secrets and an env var holding its path, so the value stays out of `docker inspect`.
# `ordo_secret_file_env NAME...` exports each NAME from the file NAME_FILE points at and unsets
# NAME_FILE (the postgres image's file_env convention), for software that only reads NAME. The
# value then lives in this process tree's environment, never in the container config.
#
# A NAME without NAME_FILE is left as it is. A NAME_FILE that cannot be read, or NAME and
# NAME_FILE both set, fails (exit 1): a declared secret never silently reads as unset.
#
# The canonical copy is ordo/secret-env.sh; each image that needs it carries a byte-identical copy
# in its own build context (enforced by tests/substrate/test_secret_files.py).

ordo_secret_file_env() {
  for ordo_sfe_name in "$@"; do
    eval "ordo_sfe_path=\${${ordo_sfe_name}_FILE:-}"
    [ -n "$ordo_sfe_path" ] || continue
    eval "ordo_sfe_value=\${${ordo_sfe_name}:-}"
    if [ -n "$ordo_sfe_value" ]; then
      echo "secret-env: both ${ordo_sfe_name} and ${ordo_sfe_name}_FILE are set; the render sets one of them" >&2
      exit 1
    fi
    if [ ! -r "$ordo_sfe_path" ]; then
      echo "secret-env: ${ordo_sfe_name}_FILE=${ordo_sfe_path} cannot be read" >&2
      exit 1
    fi
    ordo_sfe_value="$(cat "$ordo_sfe_path")"
    export "${ordo_sfe_name}=${ordo_sfe_value}"
    unset "${ordo_sfe_name}_FILE"
  done
  unset ordo_sfe_name ordo_sfe_path ordo_sfe_value
}
