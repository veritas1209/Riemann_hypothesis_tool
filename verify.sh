#!/usr/bin/env bash
set -Eeuo pipefail

kit_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  printf 'Run this command in x86_64 Ubuntu 22.04/24.04, including WSL2 Ubuntu.\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null; then
  for option in "$@"; do
    case "$option" in
      --help|-h)
        printf 'Usage: bash verify.sh [--work-dir PATH] [--threads N] [--status|--stop|--resume|--prepare-only]\n'
        exit 0 ;;
      --status|--stop)
        printf 'Python 3 is not installed; cannot read or change status.\n' >&2
        exit 1 ;;
    esac
  done
  if [[ ! -r /etc/os-release ]]; then
    printf 'Cannot identify Ubuntu.\n' >&2; exit 1
  fi
  # This file is installed by the operating system, not downloaded project code.
  . /etc/os-release
  if [[ ${ID:-} != ubuntu || ( ${VERSION_ID:-} != 22.04 && ${VERSION_ID:-} != 24.04 ) ]]; then
    printf 'Supported systems: Ubuntu 22.04 and 24.04.\n' >&2; exit 1
  fi
  elevate=()
  if (( EUID != 0 )); then elevate=(sudo); fi
  printf 'Installing Python 3 with apt; sudo may request your password.\n'
  "${elevate[@]}" apt-get update
  "${elevate[@]}" apt-get install -y python3
fi
exec python3 -u "$kit_dir/scripts/bootstrap.py" "$@"
