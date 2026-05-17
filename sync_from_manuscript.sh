#!/usr/bin/env bash
#
# sync_from_manuscript.sh -- copy the runnable code from the manuscript
#                            chapter folders into the public companion repo.
#
# The manuscript and the companion repo intentionally live in different
# directories so the book's build pipeline does not entangle with the
# code-distribution pipeline. This script bridges them.
#
# Usage:
#   ./sync_from_manuscript.sh ../../manuscript
#
# Convention: for each chapter directory in the manuscript (chXX-*), copy
# these subfolders if they exist:
#   - scripts/
#   - bicep/
#   - refactor/
#   - sql/
#   - oracle/
#   - cost-mgmt/
#   - runbook/
# The chapter markdown (chXX-*.md) is NOT copied — it belongs to the book.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <path-to-manuscript-root>" >&2
  exit 2
fi

MANUSCRIPT_ROOT="${1%/}"
if [[ ! -d "${MANUSCRIPT_ROOT}" ]]; then
  echo "ERROR: not a directory: ${MANUSCRIPT_ROOT}" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBFOLDERS=(
  scripts bicep refactor sql oracle cost-mgmt runbook
  lab app analyzer retry conversion validation compliance
  adf azure-function goldengate mv-refresh pipelines
)
COPIED=0
SKIPPED=0

shopt -s nullglob
for chapter_dir in "${MANUSCRIPT_ROOT}"/ch*-*/; do
  chapter_name="$(basename "${chapter_dir}")"
  dest="${HERE}/${chapter_name}"
  mkdir -p "${dest}"

  found_anything=0
  for sub in "${SUBFOLDERS[@]}"; do
    src_path="${chapter_dir}${sub}"
    if [[ -d "${src_path}" ]]; then
      rm -rf "${dest}/${sub}"
      cp -r "${src_path}" "${dest}/${sub}"
      echo "  [copied] ${chapter_name}/${sub}"
      found_anything=1
    fi
  done

  if [[ ${found_anything} -eq 1 ]]; then
    # Write a per-chapter README pointing back to the manuscript for context.
    cat > "${dest}/README.md" <<EOF
# ${chapter_name}

Runnable code for this chapter of *Migrating Oracle Databases to Azure Cloud*. See the book for full context — the scripts here are intentionally minimal and assume the data-chain conventions introduced in earlier chapters.

For premium content (extended lab data, production-grade Bicep modules, walkthroughs), see the gated companion at \`oracle-to-azure-premium\`. Claim access at <https://code.kinetistack.co/access>.
EOF
    COPIED=$((COPIED + 1))
  else
    rmdir "${dest}" 2>/dev/null || true
    echo "  [skipped] ${chapter_name} (no code subfolders)"
    SKIPPED=$((SKIPPED + 1))
  fi
done

echo
echo "[sync] copied ${COPIED} chapters; skipped ${SKIPPED}"
echo "[sync] review with: git status && git diff"
