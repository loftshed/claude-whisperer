# Changelog

## Unreleased

- Restore bailout with explicit per-session arming. Installation, existing config
  and old threshold state leave it inactive. Disarming cancels pending requests;
  session end and reset clear the opt-in. Manual handoffs stay available.

- fix(bailout): let sessions run to the real usage limit so they resume

## 1.1.0

- Initial repository release baseline; no versioned changelog was recorded.
