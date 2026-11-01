# Franchise Finder

A public Warsaw map for exploring franchise locations and comparing building surroundings, built with React, TypeScript, MapLibre, FastAPI and PostgreSQL/PostGIS. No account is required.

Address search selects official building footprints. Eight franchise brands have logo markers; building details show nearby census population, businesses, transit and missing premises information. Data coverage and source dates are visible. Suitability modelling is the next development stage; sales, profit and premises availability are not predicted.

## Local startup

Install Docker Desktop and start its Linux engine. From the project root:

```powershell
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
docker compose up --build -d --wait
```

Website: http://localhost:5173 · API: http://localhost:8000/docs · PostgreSQL: localhost:5433.

The database volume persists between starts. A fresh volume needs public data imported before buildings and details appear. Python 3.12 and uv are required for acquisition:

```powershell
py -3.12 -m uv sync --frozen --directory backend
py -3.12 scripts/download_warsaw.py
py -3.12 -m uv run --frozen --directory backend python -m app.cli.import_buildings ../.local/data/warsaw-buildings.geojson
py -3.12 scripts/download_warsaw_addresses.py
py -3.12 -m uv run --frozen --directory backend python -m app.cli.import_addresses ../.local/data/warsaw-addresses.geojson
py -3.12 -m uv run --no-project --with pyproj==3.8.0 scripts/profile/download_population.py
py -3.12 scripts/profile/download_pois.py
py -3.12 -m uv run --frozen --directory backend python -m app.cli.import_profile --population ../.local/data/profile/population.geojson --population-metadata ../.local/data/profile/population.metadata.json --pois ../.local/data/profile/pois.geojson --pois-metadata ../.local/data/profile/pois.metadata.json
```

Official outlet downloaders are in `scripts/franchises`; import their snapshots with `backend/app/cli/import_franchises.py`. Domino's normalization requires a verified public-directory browser capture supplied through `--input`; it is not an automatic download. Every CLI supports `--help`.

## Checks

```powershell
py -3.12 -m uv run --frozen --directory backend pytest -q
py -3.12 -m uv run --frozen --directory backend ruff check .
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
```

Set `MASTERFRANCHISES_TEST_POSTGIS=1` for isolated PostGIS integration tests with the local database running. Local frontend development uses Node.js 24.

Municipal geometry and addresses come from Warsaw; population is GUS NSP 2021, not current footfall. OSM data is © OpenStreetMap contributors under [ODbL](https://www.openstreetmap.org/copyright). Official brand artwork belongs to its respective owners; asset provenance is retained in the logo configuration. Building geometry does not establish unit area, vacancy or commercial permission.

Secrets, downloaded datasets, model artifacts, research archives and generated builds remain outside Git. Authored code contains no comments or docstrings. This Compose setup is for local development; production hosting is a separate deployment step.
