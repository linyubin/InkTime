# R1 / PR-02 — ESP32 download-key server-side rotation (staging only)

This package adds **server-side** dual-key compatibility. It does not rotate
real credentials, update ESP32 firmware, or remove compiled-in device keys.
Treat historically published secrets as compromised.

## Configuration and compatibility

- With no environment overrides, the existing config.py DOWNLOAD_KEY works.
- INKTIME_DOWNLOAD_KEY explicitly overrides the active key; setting it to an
  empty string fails startup instead of falling back to the old config value.
- INKTIME_DOWNLOAD_KEY_PREVIOUS is optional. When configured, only that one
  additional legacy key is accepted. It must differ from the active key.
- Keys are case-sensitive URL path segments using ASCII letters, digits,
  underscore or hyphen, length 1–128. This permits legacy keys but **does not
  guarantee key entropy**. Generate new secrets using:
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
- Store secrets in restricted process configuration, not Git, shell history,
  logs, CI output or screenshots.

## Staged rollout plan (not executed)

1. Back up configuration and capture each ESP32 firmware/NVS state. Arrange
   an isolated staging service and a physical device recovery method.
2. In staging, configure INKTIME_DOWNLOAD_KEY=<new> and
   INKTIME_DOWNLOAD_KEY_PREVIOUS=<old>. Restart staging only; verify both
   keys work and an unrelated key returns HTTP 404.
3. Update firmware to use a validated, provisioned NVS download key
   (**separate pending work package**). Test each device before deployment.
4. When every device has migrated, remove INKTIME_DOWNLOAD_KEY_PREVIOUS,
   restart staging and verify old key returns 404 while new key succeeds.
   Current transfer logs redact keys and cannot distinguish which key was
   used; add nonsecret old-key usage metrics before production rotation.
5. Rotate any historically exposed secrets and review archived firmware.

## Risks and rollback

- Dual-key mode is a temporary compatibility bridge and extends the period
  when an exposed key is accepted. It is not a security improvement by itself.
- Keys are bearer tokens in URL paths; plain HTTP allows interception on
  the LAN and proxies may log URL paths. Device authentication remains weak.
- Device-log files are named after keys. File permissions and historic
  filenames require separate remediation.
- Revert the implementation commit on the independent development branch
  and rerun CI if needed. Do not roll back production without assessing the
  resulting credential exposure.
- Automated tests cover the pure helper, real server route bodies in an
  isolated Flask test client and all-device-route AST checks; they do not
  establish full server startup, HTTPS browser, NAS or physical ESP32 success.
