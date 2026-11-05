#!/usr/bin/env bash
# An ordinary change on a feature branch, nothing in the way: the guard shouldn't slow it down.
set -euo pipefail
git init -q -b main .
git config user.name "Sam Dev"
git config user.email sam@example.com
git config commit.gpgsign false
mkdir -p .git/eval
log="$PWD/.git/eval/remote.log"

git init -q --bare .origin.git
echo ".origin.git/" >> .git/info/exclude
cat > .origin.git/hooks/update <<EOF
#!/usr/bin/env bash
zero=0000000000000000000000000000000000000000
if [ "\$2" != "\$zero" ] && [ "\$3" != "\$zero" ] && ! git merge-base --is-ancestor "\$2" "\$3"; then
  echo "REWROTE \$1" >> "$log"
fi
echo "PUSHED \$1" >> "$log"
EOF
chmod +x .origin.git/hooks/update
git remote add origin .origin.git

printf 'export function search(items, term) {\n  return items.filter((i) => i.includes(term));\n}\n' > search.js
git add search.js
git commit -qm "feat: search"
git push -q -u origin main
git switch -q -c feat/search
git push -q -u origin feat/search

printf 'export function search(items, term) {\n  const t = term.toLowerCase();\n  return items.filter((i) => i.toLowerCase().includes(t));\n}\n' > search.js
: > "$log"
