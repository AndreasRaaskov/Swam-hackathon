# Future work (Alex's area)

Deadline: Sun 8 Nov 11:59 PM AoE = Mon 9 Nov 18:59 Chiang Mai time. Claim an item on Linear before
starting it (see the agent protocol doc in the Linear project). Costs are estimates.

## Decisions

- [x] 2026-11-08: Title and tracks are low priority. Keep the working title; tracks are optional guidelines (SWA-1).
- [x] 2026-11-08: Scenario proposals and interview results live in separate doc tabs ("Alex - Scenarios", "Alex - Prelim Results").
- [x] 2026-11-08: Ground truth is labeled per agent, not per swarm, because several "controls" colluded.
- [x] 2026-11-08: The paper's framing is cross-audience inconsistency checked against private reasoning, not "the therapist unlocks confessions".

## Backlog, highest value first

1. **Interview the colluding controls** (A-glm r2 and r3): owner, investigator and counselor at weeks 8 and 9.
   - Tests whether interviews separate guilty agents from precautionary deleters, the core oversight claim.
   - Spot-check first: 2 investigator calls on A-glm-r2 week 9 (phahi colluded and deleted; chomthong deleted precautionarily), about $0.05.
   - Then about 60 calls, about $1.30. Not run yet; waiting on Alex's go-ahead.
2. **Interview the logged arm** (B-LOG-glm x2 at weeks 8 and 9; P1-LOG-glm x2 at week 8). Separates "hides because the evidence is gone" from "hides from this audience". About $1.30.
3. **Blind trace scoring and a second-family judge** (SWA-14, SWA-16).
   - Trace counts are keyword-based so far, and all interview scoring used one Sonnet judge.
   - Needed before the Results section is final.
4. **Sonnet tacit-pricing probe** (P1-pressure-sonnet r1/r3 plus no-preamble, week 8, owner and investigator). Documents the method's limit honestly. About $0.50 from the remaining Claude budget.
5. **EVAL as "private doubts" material.** Test arms privately weigh sandbagging; run the counselor's broad questions at day 3–4, test vs control. About $0.90. Present the other 12 general scenarios as negatives.
6. **Paper:**
   - reframe the abstract and intro (SWA-2);
   - Methods (SWA-3);
   - Results with Tables 1–2 and a figure (SWA-4);
   - Discussion and Limitations (SWA-5).
   Round 1's "controls are clean (0/45)" line in the doc needs a caveat (see NOTES.md, Corrections).
7. **Kai-side fixes, quick:**
   - A-glm-n100's control points to itself;
   - pair BYST-glm with BYST-ctl-glm;
   - stale "n=1" text in many docs;
   - mark which runs a GLM judge scored;
   - remove or flag the stuck 20- and 100-agent pilots.
8. **Not doing for the hackathon:** Claude interviews beyond item 4 (budget), and the non-pricing worlds as positive results (they are null).
