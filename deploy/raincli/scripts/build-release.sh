#!/usr/bin/env bash
# Build a release tarball from a committed revision (run locally in the repository).
#   deploy/raincli/scripts/build-release.sh [REV]   -> dist/raincli-<shortrev>.tar.gz
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
rev="$(git rev-parse --short "${1:-HEAD}")"
mkdir -p dist
git archive --format=tar.gz --prefix="raincli-$rev/" -o "dist/raincli-$rev.tar.gz" "$rev" raincli deploy/raincli
sha256sum "dist/raincli-$rev.tar.gz" | tee "dist/raincli-$rev.tar.gz.sha256"
