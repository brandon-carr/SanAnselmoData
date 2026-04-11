# Web

This folder is reserved for the permit website.

Suggested layout:

- `web/pages/`
  Static pages or entry HTML files.
- `web/assets/`
  Images, icons, and other bundled static assets.
- `web/styles/`
  Shared CSS files.
- `web/components/`
  Reusable UI partials, templates, or client-side modules.

Notes:

- The data pipeline lives in `scripts/` and writes runtime data into `data/`.
- The website should read from `data/current_permits.json` and other published data files.
- Keeping the site in `web/` helps separate presentation from scraping/geocoding logic.
