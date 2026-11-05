# Shortly — Plan
> From docs/HOW_IT_WORKS.md. Build with `/build-phase <id>`, ship with `/ship`.

## Progress
- [ ] Phase 0 — Foundation
- [ ] Phase 1 — Links table
- [ ] Phase 2 — Create and redirect
- [ ] Phase 3 — Report queue

## Phase 0 — Foundation
**Goal:** Next.js app with lint, test and CI wired; one smoke test.
**Depends on:** none
**Work in:** `web/`, `.github/`
**Not in this phase:** any feature code
### Done when
- `npm run lint`
- `npm test`

## Phase 1 — Links table
**Goal:** `links` table (code, url, clicks, created_at) with a migration and data-access functions.
**Depends on:** Phase 0
**Work in:** `db/`
**Not in this phase:** API routes
**Skills:** `tdd`
### Done when
- `npm run db:migrate -- --dry-run`
- `npm test -- db`

## Phase 2 — Create and redirect
**Goal:** `POST /api/links` and `GET /:code` end to end, with the form on the home page.
**Depends on:** Phase 1
**Work in:** `web/app/`, `api/`
**Not in this phase:** analytics page
### Done when
- `npm test -- links redirect`
- `npm run build`

## Phase 3 — Report queue
**Goal:** a "Report" button on redirect pages and an `/admin/reports` list where a human hides or keeps a link.
**Depends on:** Phase 2
**Work in:** `web/app/admin/`, `api/reports/`
**Not in this phase:** automatic blocking
### Done when
- `npm test -- reports`
- `npm run build`
