#!/bin/bash
# KREVZONE — týdenní běh: stáhnout nové horory → AI texty → manifest → build webu.
# Spouští ho launchd (scripts/com.krevzone.weekly.plist) každé pondělí v 6:00.
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/weekly-$(date +%F).log"
exec >>"$LOG" 2>&1
PY=/usr/bin/python3

echo "===== $(date '+%F %T') ====="
$PY -u scripts/fetch_films.py || echo "⚠️  fetch_films selhal — pokračuji s daty, která už jsou v DB"
$PY -u scripts/enrich_ai.py || echo "⚠️  enrich_ai selhal — filmy bez AI textů se zpracují příští týden"
$PY -u scripts/export_manifest.py || { echo "✗ export selhal"; exit 1; }
$PY -u _build.py || { echo "✗ build selhal"; exit 1; }
# publish to GitHub Pages (fork SyrekObecny/horrorweb, branch main)
git add -A index.html filmy rozbory
if git diff --cached --quiet; then
  echo "Žádné změny k publikaci"
else
  git commit -q -m "Týdenní rozbor $(date +%F) (automaticky)" && git push -q mine main && echo "✓ nasazeno na GitHub Pages" || echo "✗ push selhal"
fi
echo "===== hotovo $(date '+%F %T') ====="
