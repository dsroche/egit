#!/usr/bin/env bash

set -euo pipefail
shopt -s nullglob

testdir=$(dirname "$(readlink -f "$0")")

export PATH="$testdir/tbin:$PATH"

rm -f "$testdir/temp"
tmpdir=$(mktemp -d --tmpdir egit_test_XXXXXXXX)

ln -s "$tmpdir" "$testdir/temp"

thome="$tmpdir/home"
mkdir "$thome"

cd "$testdir"/..

cleanup() {
  rm -f "$testdir/temp"
  chmod -R u+w "$tmpdir"
  rm -rf "$tmpdir"
  return 0
}
trap cleanup EXIT


python3 -m egit.locksync create "$thome/plain" servername remfold
python3 -m egit.locksync sync "$thome/plain"
python3 -m egit.locksync edit "$thome/plain"
echo "contents" >"$thome/plain/data/f0"
python3 -m egit.locksync save "$thome/plain"
python3 -m egit.locksync add "$thome/copy" servername remfold
diff -s "$thome/plain/data/f0" "$thome/copy/data/f0"

echo "TEST PASSED"
#tree test/temp

:
