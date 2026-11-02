# PHPSiteCare

Automated migration, repair, patching and verification of Laravel sites, with a built-in malware scanner. It runs as a small FastAPI service with a single-page web UI and is designed for shared hosting and cPanel servers that host many older Laravel applications.

## Features

- **Migrator**: upgrades an older Laravel application (5.x to 11.x) to Laravel 12 on PHP 8.2+ without touching your application code.
- **Repair mode**: re-applies the Laravel 12 fixes on a site that is already upgraded. It is detected automatically.
- **Patch mode**: updates an already-upgraded site's `Handler.php`, `Controller.php` and route files for Laravel 12, then marks the site as patched.
- **AI fix (optional)**: after the migration, Claude fixes the remaining PHP errors with small, validated search and replace edits.
- **Rollback**: every change is recorded, and a failed run is undone automatically. Any finished run can also be rolled back from the UI.
- **Malware scanner**: scans a project, or all folders, for backdoors and suspicious files, and lets you quarantine selected findings.
- **Live UI**: step progress, live log, warnings, notes, a final conclusion card, run history, and light and dark themes.

## How the migration works

A full run executes these steps in order:

| # | Step | What it does |
|---|------|--------------|
| 1 | Preflight checks | Confirms it is a Laravel project, PHP is 8.2 or newer, Composer works, the folder is writable and at least 512 MB of disk is free. Detects a nested web root. |
| 2 | Prepare Laravel 12 skeleton | Creates a cached `laravel/laravel:^12.0` skeleton in `data/skeleton`. |
| 3 | Resolve and download packages | Builds a new `composer.json` in a scratch folder and resolves it. Constraints are relaxed where needed, and packages that cannot run on Laravel 12 are dropped. Packages that only fail on a PHP 7 limit are kept from the existing install in `packages_local`. Packages Composer reports as malware are not reinstalled. |
| 4 | Rename old framework files | `vendor`, `bootstrap`, `config`, `artisan`, `composer.*`, the old Kernels, `Handler`, `RouteServiceProvider`, `index.php` and `.htaccess` are renamed with the suffix `_old_<jobid>`. Nothing is deleted. |
| 5 | Install Laravel 12 skeleton | Installs the new `bootstrap`, `config`, `artisan` and `public/index.php`, then merges your old config values into the new config files. |
| 6 | Rewrite composer.json | Writes the resolved `composer.json`. |
| 7 | Install packages | Installs the resolved `vendor` and `composer.lock` and dumps the autoloader. |
| 8 | Generate bootstrap/app.php | Rebuilds routing, middleware aliases and groups, CSRF exceptions, trusted proxies and the scheduler (moved to `routes/console.php`) from the old Kernel. |
| 9 | Fix auth user model | Points `config/auth.php` at `App\Models\User` or `App\User`. |
| 10 | Patch .env | Adds `CACHE_STORE`, `SESSION_DRIVER`, `QUEUE_CONNECTION` and `MAIL_MAILER`, and sets `APP_URL` if a base URL is given. |
| 11 | Guard session_start() | Wraps raw `session_start()` calls so they are safe on Laravel 12. |
| 12 | Replace RouteServiceProvider::HOME | Replaces the removed constant with its literal value. |
| 13 | Write index.php and .htaccess | Writes a root `index.php` and a hardened `.htaccess`. Skipped when the project sits under an existing web root. |
| 14 | Permissions | Creates and fixes `storage` and `bootstrap/cache`. |
| 15 | AI fix remaining errors | Optional, see below. |
| 16 | Verify | Boots the app, runs `route:list`, probes the home page and extra paths in process, requests the check URLs, and checks removed packages for leftover references. |

Repair mode runs only steps 1, 8 to 16, since the framework is already on Laravel 12.

### Behaviours worth knowing

- **Gateway packages** with no Laravel 12 version (instamojo, paytm, paypal, nexmo, paystack, corecomponent, cinetpay, ups) are taken out of Composer only. Application code is never deleted or commented out.
- **Citigate** is protected: its files and route files are never skipped or edited by the tool, its callback paths are exempt from CSRF, and the AI is forbidden to touch it.
- **Route files** whose controllers all do not exist are skipped by default, because the old site could not serve them either. This can be turned off.
- **Middleware** used in routes but not registered in the old Kernel is mapped by name to a single matching class in `app/Middleware`.
- **Staging sites** served from a sub-folder are supported through the base URL option.

## Patch mode

For a site that is already on Laravel 12, **Patch existing upgraded site** runs:

1. Patch preflight: Laravel 12 installed, writable.
2. Writes `app/Exceptions/Handler.php`.
3. Writes `app/Http/Controllers/Controller.php`.
4. Upgrades every `routes/*.php` except `console.php` and `channels.php`.
5. Verify: caches cleared, app boots, home page and extra paths probed.
6. Marks the site as patched in `data/patched.json`.

The route upgrade is plain pattern matching and uses no AI:

- `'HomeController@index'` becomes `[HomeController::class, 'index']`.
- `Route::resource('x', 'XController')` becomes `Route::resource('x', XController::class)`.
- Matching `use` lines are added, plus `Route` and `Auth` facade imports when missing.
- No route is removed or reordered.
- Lines mentioning citigate are left alone, and files with a group `namespace` are skipped with a warning.
- Each upgraded file must pass `php -l` before it is written.
- A failed health check rolls the whole patch back, and rolling back a patch removes the patched mark.

## AI fix

Enabled when an Anthropic API key is available. After the migration the tool repeatedly looks for an error (`route:list`, the in-process home probe, the check URLs), finds the file the error points at, and asks Claude for the smallest set of exact edits through a single `apply_fix` tool call.

Guard rails:

- Only files under `app/`, `routes/` and `resources/views/` can be edited.
- Each `old` string must occur exactly once.
- Deleting code is rejected, and anything mentioning citigate is rejected.
- Edited PHP must pass `php -l`.
- At most 3 attempts per file, and limits on API calls and tokens.
- Every edited file keeps a backup.

## Rollback and safety

Each run keeps a manifest of created, modified and renamed paths.

- **Modified** files get a `.bak-<jobid>` copy next to them.
- **Renamed** files get an `_old_<jobid>` suffix.
- **Rollback** restores backups and renames originals back. Files that were created are renamed with `.rolled-<jobid>`, and any conflicting file is renamed with `_failed_<jobid>`. Nothing is ever deleted.
- A failed run rolls back automatically unless `auto_rollback` is disabled.
- After a rollback the tool checks that the files match their backups.

The tool does **not** test payments, admin areas, uploads, the scheduler, extra auth guards or old class aliases. Click through those after a run.

## Malware scanner

The Malware tab scans one project, an absolute path, or `*` for every folder. Options are deep scan (include `vendor` and `storage`), scanning public uploads for executable files, and a list of folders to skip. Findings are grouped by severity (high, medium, low) with file, line, rule, reason and snippet. A scan can be stopped. Selected findings can be quarantined, which moves them out of the web root, and a scan's quarantine can be restored. Scans keep a history and a final conclusion.

## Requirements

- Linux (the tool uses POSIX file ownership checks)
- Python 3.10 or newer with `fastapi`, `uvicorn` and `pydantic`
- PHP 8.2 or newer (cPanel `ea-php83`, `84`, `82` and `85` paths are auto-detected, or set `PHP_BIN`)
- Composer (on `PATH`, or `composer.phar` in the home directory or beside the tool, or set `COMPOSER_BIN`)
- Outbound access to Packagist and, for the AI fix, `api.anthropic.com`

## Installation

```bash
git clone <your-repo-url> upgrader
cd upgrader
python3 -m venv .venv
. .venv/bin/activate
pip install fastapi uvicorn pydantic
uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/`. Behind a reverse proxy the app is served under `/upgrader` by default, so `https://example.com/upgrader/` works without extra rewriting.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROJECTS_ROOT` | `~/public_html` if it exists, otherwise the parent of the tool folder | Folder that contains the Laravel projects |
| `PHP_BIN` | auto-detected | Path to the PHP binary |
| `COMPOSER_BIN` | auto-detected | Composer command |
| `ANTHROPIC_API_KEY` | none | API key for the AI fix. If unset, `data/anthropic.key` is read instead |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Model used by the AI fix |
| `AI_MAX_CALLS` | `12` | Maximum AI requests per run |
| `AI_TOKEN_BUDGET` | `200000` | Maximum AI tokens per run |
| `URL_PREFIX` | `/upgrader` | Path prefix stripped from incoming requests |

## Using the UI

**Migrator tab**

1. Enter a project folder name, or an absolute path, or pick one from Detected projects.
2. Optionally enter a base URL (also used for sub-folder staging) and extra paths to test.
3. Choose **Start migration**, or **Patch existing upgraded site** for a site already on Laravel 12.
4. Follow the steps and live log. When the run ends, read the warnings, notes and the final conclusion.
5. Use **Rollback this run** if needed. **Error log** shows recorded failures.

Options: *Preflight only* runs just the checks, *Already on Laravel 12* is auto-detected, and *Skip route files whose controllers are all missing* is on by default.

**Malware tab**: enter a folder (or `*`), choose the options, and start the scan. Review the findings, select files, and quarantine them.

## API

All paths are relative to the URL prefix.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| GET | `/api/state` | Projects root, PHP, Composer and AI availability |
| GET | `/api/projects` | Detected Laravel projects with version, migrated, patched and last status |
| GET | `/api/jobs` | Run history |
| POST | `/api/jobs` | Start a run: `project`, `base_url`, `extra_paths`, `dry_run`, `repair`, `ai`, `skip_missing`, `patch` |
| GET | `/api/jobs/{id}` | Run details |
| GET | `/api/jobs/{id}/log?offset=` | Incremental log, next offset in the `X-Next` header |
| POST | `/api/jobs/{id}/rollback` | Roll a run back |
| GET | `/api/errors` | Recorded errors |
| GET | `/api/malware/scans` | Scan history |
| POST | `/api/malware/scans` | Start a scan: `project`, `deep`, `uploads`, `exclude` |
| GET | `/api/malware/scans/{id}` | Scan details and findings |
| GET | `/api/malware/scans/{id}/log?offset=` | Incremental scan log |
| POST | `/api/malware/scans/{id}/stop` | Stop a scan |
| POST | `/api/malware/scans/{id}/quarantine` | Quarantine the listed files |
| POST | `/api/malware/scans/{id}/restore` | Restore a scan's quarantine |

## Repository layout

```
main.py            FastAPI app and routes
engine.py          Migration, repair, patch, AI fix, rollback, project detection
malware.py         Malware scanner and quarantine
worker.py          Background process that runs a job outside the web request
static/index.html  Single-page UI
data/              Runtime data (created automatically, do not commit)
```

Runtime files under `data/`:

- `jobs/` holds one `.json` and one `.log` per run.
- `work/` is scratch space and is cleaned after each run.
- `skeleton/` is the cached Laravel 12 skeleton.
- `errors.jsonl` is the error log.
- `patched.json` records which sites are patched.
- `anthropic.key` is the optional API key file.

## Before you publish

- **Add authentication.** The service can modify and move files on the server and has no login of its own. Never expose it publicly. Put it behind HTTP basic auth, a VPN or an IP allowlist.
- **Keep secrets out of Git.** Add this to `.gitignore`:

```
data/
.venv/
__pycache__/
*.pyc
.env
```

- Run it as the same user that owns the sites, or the permission check will stop the run and tell you which `chown` to use.
- Test on a copy of a site first, and keep a full backup before running on production.

## Limitations

- Targets Laravel 12 on PHP 8.2 to 8.5.
- Packages without a Laravel 12 compatible version are removed from Composer and must be replaced by hand. Any code still referencing them is reported as a warning.
- Dynamic or unusual route definitions that do not follow the standard `Controller@method` pattern are left as they are.
- Business logic, payments, admin panels and uploads need manual testing after a run.

## License

Add the license of your choice before publishing, for example MIT.
