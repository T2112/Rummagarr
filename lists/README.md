# Lidarr genre starter lists

## Main custom list

https://raw.githubusercontent.com/T2112/lidarr-soulseek/main/lists/lidarr-custom-list.json

Curated popular / widely recorded artists across rock, pop, hip-hop, country, jazz, blues, metal, R&B/soul, electronic, folk, punk, indie, reggae, Latin, funk, classical, goth, emo, women artists, game soundtracks, electro-swing, and more.

**Current status (2026-10-27):** **1057** unique MusicBrainz artist IDs. All names from artists.txt are covered via exact match, accent/variant, or known alias (e.g. Jackson 5 = The Jacksons, Debbie Harry = Deborah Harry, Mos Def = Yasiin Bey, Charlie Pride = Charley Pride, Damian Marley = Damian “Jr. Gong” Marley, Tchaikovsky = Пётр Ильич Чайковский, Go-Go's = The Go-Go’s, etc.). 0 new IDs this run (39 exact-name variants queried; 18 accepted matches already present; frequent MusicBrainz 503s with backoff; remaining no-results are known aliases already covered).

## Festival list (Woodstock + Qlimax + Ozzfest)

https://raw.githubusercontent.com/T2112/lidarr-soulseek/main/lists/festivals-woodstock-qlimax-ozzfest.json

Separate import list of musicians, DJs, and groups that played:
- Woodstock 1969, 1994, and 1999
- Qlimax (all official Dutch editions, 2000–2024)
- Ozzfest (US / UK / Europe / Japan editions, 1996–2018)

About 457 MusicBrainz artists after resolving official lineups. A handful of local openers and one-off aliases without a usable MusicBrainz page were skipped.

## Safer add settings

Lidarr → Settings → Import Lists → + → Custom List → paste either URL.

Recommended:
- Monitor: **Latest Album** (or First Album)
- Monitor new items: **New** or **None**
- Start search for missing albums: **off** until you have tagged the batch
- Add a tag such as `genre-starter` so you can delete the whole batch if needed

Lidarr skips artists already in your library. Adding hundreds of artists with "all albums + search" will queue more than a Soulseek worker can finish quickly.

Files in this folder / project:
- `lidarr-custom-list.json` / `artists.txt` / `artists-by-genre.txt`
- `festivals-woodstock-qlimax-ozzfest.json` / festival artists text
