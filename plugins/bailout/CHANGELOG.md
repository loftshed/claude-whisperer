# Changelog

## Unreleased

- Restore bailout with explicit per-session arming. Installation, existing config
  and old threshold state leave it inactive. Disarming cancels pending requests;
  session end and reset clear the opt-in. Manual handoffs stay available.

## 1.1.1

- fix(bailout): let sessions run to the real usage limit so they resume
