# FlowDesk 0.2.26 — Windows verification

Requested after commit `f4f617e2d88cb8823cc6481b3ed917587ad66043` on 2026-11-14.

Purpose: run the existing `build-windows.yml` workflow on `main` to verify the latest Finam screener adjustments and publish the portable Windows EXE ZIP. This is a CI-only documentation commit; it does not change application behavior.

Verify that all offline tests, frozen EXE startup/shutdown and Finam runtime checks pass before treating the artifact as validated. Live Finam token-dependent signals are not covered by the workflow.
