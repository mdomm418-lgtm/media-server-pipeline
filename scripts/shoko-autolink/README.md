# Shoko Autolink

Shoko Autolink is an automated scripting pipeline designed to seamlessly bridge Sonarr and Shoko Anime Server. It automatically detects and links TVDB-based anime episodes from Sonarr to their proper AniDB counterparts within Shoko, managing complex mapping rules, naming conventions, and API edge cases.

## Features

- **Automated Drop-Folder Scanning**: Automatically triggers Shoko drop folder rescans as new files are imported from Sonarr.
- **Smart Parsers**: Uses heuristic-based parsers to intelligently match sub-group tagged anime files (e.g., `[SubGroup] Series Name - 05 [1080p].mkv`).
- **Episode Offset Mappings**: Resolves complicated season/episode mappings (like split-cour series) by translating continuous absolute TVDB episodes to split AniDB seasons.
- **Fuzzy Name Matching**: Standardizes spaces and removes special characters to correctly link folders that have slightly different names in Sonarr vs. Shoko.
- **AniDB Ban Resilience**: Actively monitors Shoko's live API for HTTP ban statuses. If Shoko gets temporarily banned from AniDB, the script intelligently skips metadata refresh requests to prevent ban extensions, falling back to cached local mapping data!
- **State & Auditing**: Maintains detailed `needs_review` queues and run caches, ensuring no file falls through the cracks.

## Workflow Pipeline
1. **Trigger Scan**: Pings `Action/RunImport` to get Shoko to ingest newly downloaded files.
2. **Fetch Unrecognized**: Pulls all files Shoko cannot identify via simple hash matches.
3. **Parse Filename**: Normalizes and matches filenames to extract episode numbers.
4. **Resolve Series**: Translates the Sonarr series folder to a specific Shoko/AniDB series using custom maps and community `anime-lists`.
5. **Calculate Offset**: Applies episode offsets (e.g., Sonarr Season 4 Episode 1 = AniDB Episode 76).
6. **Link & Refresh**: Tells Shoko to link the file ID to the specific AniDB Episode ID and refreshes series metadata (unless banned).

## Usage

Create a `.env` file containing your credentials:
```env
SHOKO_USERNAME="your_username"
SHOKO_PASSWORD="your_password"
SONARR_API_KEY="your_sonarr_api_key"
```

Copy `config.example.yaml` to `config.yaml` and adjust the configuration to match your setup.

Run the linker manually:
```bash
set -a && source .env && set +a
python3 link_shoko_unrecognized.py run --config config.yaml
```

Check the review queue for failed parses/resolves:
```bash
python3 link_shoko_unrecognized.py status --config config.yaml
```

Retry the queue after fixing `anidb_map.json`:
```bash
python3 link_shoko_unrecognized.py retry-review --config config.yaml
```

## Setup (Cron)

We recommend running the linker periodically via `cron`. See `crontab.example` for an example using `flock` to ensure instances do not overlap.
