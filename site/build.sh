#!/usr/bin/env bash
# Puts the public site together in _site/: the homepage at the root, and a read-only
# replay of the app under demo/<run>/ for each recorded run in replay/.
# Run `npm ci` in web/ first. Synthetic sandbox data only; the site calls no model and holds no key.
set -euo pipefail
cd "$(dirname "$0")/.."

(cd web && npm run build:replay)

rm -rf _site
mkdir -p _site
cp site/index.html web/public/pallas-logo.png _site/
cp -R web/public/avatars _site/

for run in replay/*/; do
  name=$(basename "$run")
  mkdir -p "_site/demo/$name"
  cp -R web/dist-replay/. "_site/demo/$name/"
  cp -R "$run/data" "$run/media" "_site/demo/$name/"
done
