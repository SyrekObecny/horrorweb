# Týdenní pipeline KREVZONE

| Krok | Skript | Co dělá |
|---|---|---|
| 1 | `fetch_films.py` | Wikidata (SPARQL) → horory s premiérou v okně dnes−7 … dnes+60 dní, které mají článek na en/cs Wikipedii. Detaily (režie, scénář, obsazení, stopáž, země, studio, IMDb, YouTube trailer) + text článku z Wikipedie + plakát z Wikipedie do `media/posters/`. Ukládá do `data/krevzone.db`. |
| 2 | `enrich_ai.py` | Při každém běhu stáhne seznam modelů z OpenRouteru, vybere ty **zdarma** a zkouší je postupně, dokud jeden nevrátí validní český JSON (synopse, pullquote, recenze, hodnocení, zajímavosti, tagy, role). Úspěšnost modelů se loguje do tabulky `ai_runs` a příště jsou úspěšnější modely první. |
| 3 | `export_manifest.py` | DB → `rozbory/<pondělí>/manifest.json` + plakáty. Ruční úpravy v manifestu se při dalším exportu zachovají. |
| 4 | `../_build.py` | Vygeneruje HTML webu. |

`weekly.sh` spustí všechny kroky a zapíše log do `logs/weekly-YYYY-MM-DD.log`.

## Nastavení

```bash
echo 'OPENROUTER_API_KEY=sk-or-v1-…' > .env          # v .gitignore
cp scripts/com.krevzone.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.krevzone.weekly.plist   # každé pondělí 6:00
launchctl start com.krevzone.weekly                               # spustit hned (test)
```

## Užitečné příkazy

```bash
python3 scripts/enrich_ai.py --list-models          # aktuální free modely a jejich úspěšnost
python3 scripts/enrich_ai.py --force --slug hope    # přegenerovat texty jednoho filmu
sqlite3 data/krevzone.db "UPDATE ai_content SET locked=1 WHERE wikidata_id='Q…'"  # AI už text nepřepíše
sqlite3 data/krevzone.db "SELECT model, SUM(ok), COUNT(*) FROM ai_runs GROUP BY model"
```
