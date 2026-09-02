# Download iNaturalist Species Photos

Download observation photos for one or more species from the [iNaturalist](https://www.inaturalist.org/) public API. No login is required.

Settings live in `config.yaml`. Re-running the script skips photos already listed in `metadata.csv`.

Example species: [Bactrocera dorsalis](https://www.inaturalist.org/taxa/358125-Bactrocera-dorsalis/browse_photos).

## Install

Python 3.10 or newer:

```bash
python -m pip install -r requirements.txt
```

If `python` opens Python 2 (common on some Windows machines), use `py -3` instead.

## Configure

Edit `config.yaml`:

```yaml
photo_limit: 20          # new photos per species this run; 0 = unlimited
size: original           # original / large / medium / small
quality: all             # all / research / needs_id / casual
licensed_only: false
out_dir: downloads
species:
  - Bactrocera dorsalis
  - Bactrocera cucurbitae
  - Ceratitis capitata
```

`photo_limit` is how many **new** photos to fetch this run. Existing rows in that species' `metadata.csv` are skipped. If fewer photos remain on iNaturalist, the script stops when the feed runs out.

## Usage

```bash
python download_photos.py
python download_photos.py --limit 20
python download_photos.py "Bactrocera dorsalis" --limit 20
python download_photos.py --no-limit
```

## Output

Files are written to `downloads/<Scientific_name>/`:

```
downloads/
  config.yaml
  run_settings.yaml
  Bactrocera_dorsalis/
    config.yaml
    run_settings.yaml
    metadata.csv
    obs396521042_photo727167554.jpg
    ...
```

`metadata.csv` has one row per photo (location, time, license, observer, quality grade, and notes). Photos are requested newest-first by observation ID, which is upload order, not observation date.

## Notes

This script uses the [iNaturalist API](https://api.inaturalist.org/v1/docs/) and waits about one second between API calls. Photo licenses vary; check `license` and `attribution` in `metadata.csv` before reuse, and follow the [iNaturalist terms](https://www.inaturalist.org/terms).
