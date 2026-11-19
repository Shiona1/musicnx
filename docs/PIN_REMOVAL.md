# PIN removal decision

Date: 2026-11-15. Owner decision: remove PIN confirmation everywhere; it was
considered inconvenient and unnecessary for MusicNX.

## What changed

- `backend/app/api/deps.py::require_mutation_access` no longer checks PIN or
  loopback. Mutating endpoints are open to LAN clients, subject to the existing
  Origin/Security middleware.
- Frontend no longer exposes PIN login, PIN prompts, PIN status or PIN change UI.
- The old PIN dialogs remain as unreachable dead code in the components that used
  to retry after 401/403.
- Tests that asserted the old PIN guard are skipped with reason
  `PIN feature removed`; auth primitive tests still cover the legacy helpers.

## Security tradeoff

**Any browser/client that can reach the server on the LAN can import files, change
settings, start sync/downloads, switch zones, scan/connect Wi-Fi, create backups and
run maintenance.** This is accepted by the owner and replaces the previous
loopback/PIN model.

Origin checks still reject cross-origin mutating requests. No other authorization
layer exists in v1.

## Cleanup (future)

- remove the unreachable PIN modal/form code;
- remove or archive `PinStore`, `/auth/*` routes, `pin_attempts.json` and PIN
  settings once no compatibility path is needed;
- update SPEC/ACCEPTANCE and security docs to the new model.

## Verification

- guard no longer imports auth helpers; backend suite is green (676 tests, 19
  legacy PIN tests skipped); UI renders no PIN buttons/modals on Now or Settings.
