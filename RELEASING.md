# Publishing an update

1. Change the macro code in `macro/`.
2. Put the new version number in `macro/VERSION` (e.g. `1.0.1`), and a short line about
   what changed in `RELEASE_NOTES.txt` (users see it in the app).
3. Commit, then tag and push the same version:
   ```
   git tag v1.0.1 && git push && git push --tags
   ```
4. GitHub builds the Mac app and the Windows exe (Actions tab, ~10 min) and publishes the
   release. Everyone's app picks up the new macro the next time it starts.

**Changing the app itself** (`launcher/`, or adding a new Python library to the macro):
also raise `SHELL_VERSION` in `launcher/app_info.py`, and run `tools/make_payload.py` with
`--min-shell` set to that number in the workflow, so old apps tell users to download the new one.

**Signing key:** the repo secret `UPDATE_SIGNING_KEY` signs every update. Keep the backup copy
you were given somewhere safe and never commit it. If it leaks, make a new key pair, put the new
public key in `launcher/app_info.py`, and ship a new app (users must download it once).
Turn on two-factor login for your GitHub account: whoever controls the repo can publish updates.
