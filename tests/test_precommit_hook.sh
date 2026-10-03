#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook_source="$repo_root/.githooks/pre-commit"

if [[ ! -x "$hook_source" ]]; then
  printf 'FAIL: expected executable hook at %s\n' "$hook_source" >&2
  exit 1
fi

if [[ ! -x "$repo_root/frontend/node_modules/.bin/git-format-staged" ]]; then
  printf 'FAIL: install frontend dependencies before running this test\n' >&2
  exit 1
fi

if [[ ! -x "$repo_root/backend/.venv/bin/ruff" ]]; then
  printf 'FAIL: sync backend dependencies before running this test\n' >&2
  exit 1
fi

test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
test_repo="$test_dir/repo"

mkdir -p \
  "$test_repo/.githooks" \
  "$test_repo/frontend/apps/web/src" \
  "$test_repo/frontend/packages/ui/src/styles" \
  "$test_repo/backend/app" \
  "$test_repo/unrelated"
git -C "$test_repo" init -q
git -C "$test_repo" config user.name "Trellis Hook Test"
git -C "$test_repo" config user.email "trellis-hook-test@example.invalid"
git -C "$test_repo" config core.hooksPath .githooks

cp "$hook_source" "$test_repo/.githooks/pre-commit"
chmod +x "$test_repo/.githooks/pre-commit"
cp "$repo_root/frontend/.prettierrc" "$test_repo/frontend/.prettierrc"
cp \
  "$repo_root/frontend/packages/ui/src/styles/globals.css" \
  "$test_repo/frontend/packages/ui/src/styles/globals.css"
cp "$repo_root/backend/pyproject.toml" "$test_repo/backend/pyproject.toml"
ln -s "$repo_root/frontend/node_modules" "$test_repo/frontend/node_modules"
ln -s "$repo_root/backend/.venv" "$test_repo/backend/.venv"

git -C "$test_repo" commit --allow-empty -q -m "test: initialize hook fixture"
printf 'def staged(number):\n    return number + 1\n' \
  > "$test_repo/backend/app/partial.py"
git -C "$test_repo" add backend/app/partial.py
git -C "$test_repo" commit -q -m "test: seed partial file"

printf 'const value={message:"hello",items:[1,2]}\n' \
  > "$test_repo/frontend/apps/web/src/unformatted.ts"
printf 'body{color:red}\n' > "$test_repo/frontend/apps/web/src/unformatted.css"
printf 'def add( left,right ):return left+right\n' \
  > "$test_repo/backend/app/unformatted.py"
printf 'leave this file alone\n' > "$test_repo/unrelated/notes.txt"
git -C "$test_repo" add \
  frontend/apps/web/src/unformatted.ts \
  frontend/apps/web/src/unformatted.css \
  backend/app/unformatted.py \
  unrelated/notes.txt
git -C "$test_repo" commit -q -m "test: format staged files"

git -C "$test_repo" show HEAD:frontend/apps/web/src/unformatted.ts \
  | grep -F 'const value = { message: "hello", items: [1, 2] }' >/dev/null
git -C "$test_repo" show HEAD:frontend/apps/web/src/unformatted.css \
  | grep -F '  color: red;' >/dev/null
git -C "$test_repo" show HEAD:backend/app/unformatted.py \
  | grep -F 'def add(left, right):' >/dev/null
git -C "$test_repo" show HEAD:backend/app/unformatted.py \
  | grep -F '    return left + right' >/dev/null
[[ "$(git -C "$test_repo" show HEAD:unrelated/notes.txt)" == "leave this file alone" ]]
git -C "$test_repo" diff --quiet

printf 'def staged( number ):return number+2\n' \
  > "$test_repo/backend/app/partial.py"
git -C "$test_repo" add backend/app/partial.py
printf '\ndef unstaged(): return  "keep me"\n' \
  >> "$test_repo/backend/app/partial.py"
git -C "$test_repo" commit -q -m "test: preserve unstaged edits"
git -C "$test_repo" show HEAD:backend/app/partial.py \
  | grep -F 'def staged(number):' >/dev/null
git -C "$test_repo" show HEAD:backend/app/partial.py \
  | grep -F '    return number + 2' >/dev/null
git -C "$test_repo" show HEAD:backend/app/partial.py \
  | grep -F 'unstaged' >/dev/null && {
    printf 'FAIL: the commit included unstaged Python changes\n' >&2
    exit 1
  }
grep -F 'def unstaged(): return  "keep me"' \
  "$test_repo/backend/app/partial.py" >/dev/null
git -C "$test_repo" diff --quiet --cached
git -C "$test_repo" diff --quiet -- backend/app/partial.py && {
  printf 'FAIL: the hook discarded unstaged Python changes\n' >&2
  exit 1
}

printf 'const value = ;\n' > "$test_repo/frontend/apps/web/src/invalid.ts"
git -C "$test_repo" add frontend/apps/web/src/invalid.ts
if git -C "$test_repo" commit -m "test: reject formatter errors" > "$test_dir/commit.log" 2>&1; then
  printf 'FAIL: the hook allowed an unformattable staged file\n' >&2
  exit 1
fi
if ! grep -F 'Prettier formatting failed' "$test_dir/commit.log" >/dev/null; then
  printf 'FAIL: formatter failure did not produce a useful message\n' >&2
  cat "$test_dir/commit.log" >&2
  exit 1
fi

printf 'PASS: pre-commit formatter integration\n'
