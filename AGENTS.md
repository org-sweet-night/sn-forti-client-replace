# Repository rules

## Layout

- Keep native application code in `app/`, VPN service code in `service/`, native tools in `helpers/`, maintenance entry points in `scripts/`, tests in `tests/`, and supporting documentation in `docs/`.
- Put generated applications and runtime dependencies under ignored `dist/`. Use ignored `tmp/` for disposable local evidence. Do not put generated files in the repository root.

## Installation and upgrades

- Use `scripts/install.sh` only for a new installation. Use `scripts/update.sh` for an existing installation; do not replace only the App or use the partial `install.py --upgrade` as a full release.
- Prepare and validate the complete release before touching installed files. Run `python3 -m unittest discover -s tests`, the version self-test, and relevant build/signature checks.
- An active VPN must not be disconnected without user authorization. Quit the App and stop the VPN before a complete upgrade or recovery.
- Run the updater's `--check` after an upgrade. A successful file copy is not proof of a completed update: verify managed files, helper authorization, credential readability, and intended connection recovery separately.
- Delete transaction backups only after verification succeeds. Use `--recover` for an interrupted transaction; never delete recovery data just to make a retry proceed.
- Preserve connection settings, profiles, Keychain items, login preferences, and live state. Clean only exact app-owned entries with verified ownership. Report unknown files instead of deleting them.
- `--cleanup` removes only known historical residue after checking the current installation. It does not certify an upgrade or authorize credential changes. A credential compatibility failure must roll back; obtain user approval before reauthorization.
- Remove disposable staging files from the current operation. Do not claim a system-wide absence of leftovers: report the managed paths checked, any retained recovery data, and unresolved findings.
- Update the README installation record only after observing the installed state. Keep source commit, build number, test results, installation, and VPN connectivity evidence distinct.
