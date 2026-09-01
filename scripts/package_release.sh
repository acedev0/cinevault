#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(PYTHONPATH="$PROJECT_DIR" python3 -c 'from cinevault import __version__; print(__version__)')"
OUTPUT_DIR="${1:-$PROJECT_DIR/release}"
STAGE_DIR="$(mktemp -d)"
PACKAGE_DIR="$STAGE_DIR/CineVault-$VERSION"

cleanup() { rm -rf -- "$STAGE_DIR"; }
trap cleanup EXIT

mkdir -p "$PACKAGE_DIR/scripts" "$OUTPUT_DIR"
files=(
  cinevault pytests assets docs .github .devcontainer
  install.sh start.sh requirements.txt requirements-dev.txt pyproject.toml package.json package-lock.json
  Dockerfile docker-compose.yml .dockerignore .gitignore .replit replit.nix
  README.md SECURITY.md CONTRIBUTING.md SUPPORT.md CODE_OF_CONDUCT.md LICENSE
)
for item in "${files[@]}"; do
  cp -a "$PROJECT_DIR/$item" "$PACKAGE_DIR/"
done
cp -a "$PROJECT_DIR/scripts/cinevaultctl" "$PACKAGE_DIR/scripts/"
cp -a "$PROJECT_DIR/scripts/package_release.sh" "$PACKAGE_DIR/scripts/"
cp -a "$PROJECT_DIR/scripts/replit-run.sh" "$PACKAGE_DIR/scripts/"
cp -a "$PROJECT_DIR/scripts/poster-fetcher.mjs" "$PACKAGE_DIR/scripts/"
find "$PACKAGE_DIR" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$PACKAGE_DIR" -type f -name '*.pyc' -delete
chmod +x "$PACKAGE_DIR/install.sh" "$PACKAGE_DIR/start.sh" "$PACKAGE_DIR/scripts/cinevaultctl" "$PACKAGE_DIR/scripts/package_release.sh" "$PACKAGE_DIR/scripts/replit-run.sh"

rm -f -- "$OUTPUT_DIR/CineVault-$VERSION.zip" "$OUTPUT_DIR/CineVault-$VERSION.zip.sha256"
(cd "$STAGE_DIR" && zip -q -r "$OUTPUT_DIR/CineVault-$VERSION.zip" "CineVault-$VERSION")
sha256sum "$OUTPUT_DIR/CineVault-$VERSION.zip" > "$OUTPUT_DIR/CineVault-$VERSION.zip.sha256"
printf '%s\n' "$OUTPUT_DIR/CineVault-$VERSION.zip"
