# Legal-critical terms: always surface for human review

This is an e-signature product. A mistranslation in a tooltip is a typo; a mistranslation of the verb on a signing action can change what a signer believes they are agreeing to. So a small set of terms always goes to a human for a glance when their string changes, **regardless of the confidence the authority stack assigns**. This is what makes the "targeted spot-check" review posture safe: the human reviews the risky few, not all ~6k strings.

## The seed list

Flag any changed string whose meaning hinges on one of these actions or concepts, in any locale:

- **Sign** / signature / e-sign
- **Decline** / refuse
- **Void** / cancel / revoke
- **Withdraw** (consent)
- **Delegate** / reassign
- **Consent** / agree / accept terms
- **Initials**
- **Accept**
- **Reject**
- **Expire** / expiry / deadline

## How to use it

When the review pass (Step 2) processes the changed strings, any hit on this list is added to the review queue with a `legal-critical` tag, even if tiers 0–2 of the authority stack gave it high confidence. The reviewer note should name the term and the locales it changed in. Keep the list tight: it earns its weight only if it stays the genuinely consequential terms, not every verb.
