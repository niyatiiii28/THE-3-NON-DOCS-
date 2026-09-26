# PatientTriage.ai
### Rules-based, age-adjusted, uncertainty-aware triage assistant for hospital emergency departments

> ** Live Demo:** [Try PatientTriage.ai](https://3-non-docs.streamlit.app/)

AI-assisted emergency department triage support system designed to help clinicians prioritize and route patients while keeping clinical judgment in the loop.
**Accenture Innovation Challenge 2026 — Round 2 Prototype**

---

## 1. Problem Framing

Emergency departments triage patients under three simultaneous constraints: **seconds**, not minutes,
per decision; **incomplete data**, since roughly half of arriving patients have no prior record on file;
and **asymmetric risk**, where missing a critical case is categorically worse than over-prioritizing a
minor one. No two EDs look alike — patient mix, staffing, and existing systems vary enormously, from a
100-visit/day rural department to a 500+/day urban trauma center.

Standard severity scales (e.g., a single adult-calibrated 5-level model) introduce **silent safety risk**
when applied uniformly across pediatric, adult, and geriatric patients — a fever of 38.5°C means something
very different in a 3-year-old than in a 75-year-old with a blunted febrile response.

**PatientTriage.ai** is a decision-support assistant — never a decision-maker — that scores incoming and
waiting patients, surfaces its own confidence and reasoning, and is deliberately biased to escalate rather
than downgrade whenever data is missing, ambiguous, or conflicting.

## 2. Target Users

| User | What they need from this tool |
|---|---|
| **Triage nurse** | A recommendation they can read in 3 seconds, with the "why" visible on demand, and full authority to override |
| **Charge nurse / surge coordinator** | Real-time visibility into the whole queue, and alerts when someone has waited past a safe threshold |
| **ED medical director / compliance officer** | An audit trail sufficient for clinical and legal review, and confidence that the tool is explainable, not a black box |
| **Hospital IT / CMIO** | A system that can be deployed with partial data (some patients have no EHR history) and scales across differently-resourced sites |

## 3. Solution Design

### 3.1 Decision model — rules-based, not black-box ML
Every score must be explainable to a clinician glancing at a screen while managing several other patients.
We use a transparent, weighted rules engine rather than an ML risk model as the **primary and sole driver
of severity level**. An ML model could sit alongside it in a later phase as a secondary confidence signal —
but per the brief's requirement that "recommendations must remain reviewable and explainable," it is never
allowed to be the unexplainable source of a severity decision.

### 3.2 Age-adjusted vital thresholds
Every vital sign (heart rate, respiratory rate, SpO2, temperature, systolic BP) is scored against **pediatric
(<12), adult (12–64), and geriatric (65+)**-specific critical/warning bands, not one universal model. See
`triage_engine.py::VITAL_THRESHOLDS`.

### 3.3 Confidence as a first-class output
Every score ships with a confidence percentage. Confidence is reduced by: missing vitals, absence of prior
history (zero-history patients), self-reported symptom language known to correlate with under-reporting
(e.g., "vague pain," "just feels off"), and geriatric patients not mentioning pain despite research showing
under-reporting is common in this group. **The prototype never returns a score without a confidence
indicator**, per the brief's requirement.

### 3.4 Escalation bias — the asymmetric-cost design (explicit, testable rules)
1. **Any single critical vital reading forces a minimum Level 2**, regardless of the composite score.
2. **Confidence below 60% forces the severity one level more urgent.**

These two rules are the concrete mechanism by which the system is "deliberately tuned to bias toward
escalation under uncertainty rather than optimized for average accuracy," as the brief requires. They trade
some over-triage (costs staff time) to avoid under-triage (costs lives).

### 3.5 Waiting-room safety monitor
Every waiting patient's elapsed wait time is checked against a safe maximum for their current severity
level (Level 1: 0 min / immediate, Level 2: 10 min, Level 3: 30 min, Level 4: 60 min, Level 5: 120 min).
Exceeding this window triggers a re-assessment alert, visible on the Live Queue screen. In a full
implementation this would also re-trigger on newly recorded vitals showing deterioration.

### 3.6 Surge behavior
Under 3× volume, the system does **not** relax its thresholds — the same escalation rules apply at 1× or
3× load. What changes operationally is alert frequency and volume, which is why the design calls for a
dedicated surge/charge-nurse role to own re-assessment alerts during high-volume periods rather than
letting them queue silently. The Surge Simulation tab in the prototype demonstrates this.

### 3.7 Clinician override & audit trail
Any recommendation can be overridden by a clinician. Every override captures: patient ID, timestamp,
original severity + confidence, new severity, clinician ID, a **mandatory** free-text reason, and the
system's flags at time of decision — the minimum fields required for post-hoc clinical and legal review.

## 4. Regulatory & Data Assumptions

- **Assumed jurisdiction: HIPAA (United States).**
- Patient identifiers in this prototype are simulated; a production system would store only
  hashed/tokenized IDs in logs, encrypt vitals and history at rest and in transit, and apply role-based
  access control so only authorized clinical staff can view PHI or the audit log.
- Consent model: standard ED treatment consent covers triage-support use of clinical data collected at
  intake; any secondary use (e.g., model improvement) would require separate disclosure per HIPAA's
  minimum-necessary standard.
- Retention: audit log entries retained per the hospital's existing medical-record retention policy
  (typically 6–10 years depending on state law), since overrides are part of the clinical record.
- ~50% of simulated patients have `has_history: false`, matching the reference assumption of mixed data
  availability at intake.

## 5. Business Case & Impact

- **Reduced under-triage risk**: explicit escalation-under-uncertainty rules directly target the highest-cost
  failure mode (a missed critical case), rather than optimizing for average accuracy across all cases.
- **Faster, more consistent first-pass triage**: a rules-based score is available the instant vitals are
  entered, reducing variance between individual nurses' snap judgments, especially during a surge.
- **Reduced liability exposure**: a structured, timestamped audit trail for every override strengthens the
  hospital's position in post-incident review.
- **Deployable with partial data**: the confidence mechanism means the system is honest about zero-history
  patients rather than silently treating missing data as "no risk factors" — this is what makes it usable
  on day one at a new site, without a data-migration project first.

## 6. Phased Roadmap

| Phase | Scope |
|---|---|
| **Phase 0 (this prototype)** | Rules-based scoring engine, simulated data, single-department dashboard |
| **Phase 1 — Pilot** | Deploy read-only (shadow mode) at one ED alongside existing paper/manual triage; compare recommendations against actual clinician decisions for 4–6 weeks before going live |
| **Phase 2 — Single-site live** | Go live with override authority intact; integrate with the hospital's EHR for real patient history lookups; formal audit-log compliance review |
| **Phase 3 — Multi-site scaling** | Configurable thresholds per site (rural vs. urban trauma center); bed-management and staff-roster integration for routing, not just scoring |
| **Phase 4 — Secondary ML signal** | Introduce an ML risk model as an additional confidence input only, never as the sole driver of severity — with continuous monitoring for calibration drift and demographic bias |

## 7. Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Alert fatigue** from frequent re-assessment prompts during surges | Route surge-period alerts to a dedicated charge-nurse role rather than the treating nurse; tune wait thresholds per department size during pilot |
| **Staff distrust / workaround** of a new tool under time pressure | Shadow-mode pilot phase so nurses see the tool agree with them most of the time before it has any authority; override is always one click and always logged as valid clinical judgment, not a "failure" |
| **Over-triage burden** from the deliberate escalation bias | Monitored explicitly during pilot; escalation-triggered cases are tagged so leadership can track the over-triage rate as a known, accepted cost — not a silent side effect |
| **Bias in self-reported symptom weighting** (e.g., under-reporting flags) | Flags reduce *confidence*, not severity directly — the system escalates on uncertainty rather than making unverified assumptions about why a patient may be under-reporting |
| **Integration variance across hospitals** | Phase 3 treats bed/roster/EHR integration as separate, optional modules; the core scoring engine works standalone on intake data alone |
| **PHI exposure / compliance failure** | Encryption at rest/in transit, role-based access, audit log immutability — treated as Phase 2 gating requirements before go-live, not an afterthought |

## 8. Prototype — What's Included

- `triage_engine.py` — core scoring logic: age-adjusted thresholds, confidence calculation, escalation
  rules, wait-time safety monitor, audit entry builder
- `app.py` — Streamlit dashboard with four working views: **Live Queue**, **Patient Detail & Override**,
  **Surge Simulation**, **Audit Log**, plus **Design Notes**
- `data/simulated_patients.json` — 18 simulated patients, including:
  - an ambiguous presentation with normal-looking vitals (`P006`)
  - pediatric cases (`P002`, `P007`, `P010`, `P015`)
  - geriatric cases (`P003`, `P008`, `P011`, `P013`, `P017`)
  - zero-history (first-time) patients (`P004`, `P006`, `P010`, `P012`, `P014`, `P018`)

### Run it locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Then open the local URL Streamlit prints (typically `http://localhost:8501`).

### What to try in the demo
1. **Live Queue** — see all patients sorted by severity, with confidence and wait time
2. Open **Patient Detail & Override** on `P006` (Tomas Weber) — an ambiguous chest-discomfort case with
   normal vitals, to see how reduced confidence changes the picture even without abnormal readings
3. Log an override on any patient and check it appears in **Audit Log**
4. Go to **Surge Simulation** and trigger a 3× load to see queue growth and re-assessment alerts increase

## 9. What's Explicitly Out of Scope for This Prototype

- Real EHR integration (simulated `has_history` flag stands in for it)
- Authentication / role-based access control (assumed present in production)
- The ML secondary-signal layer described in Phase 4
- Multi-site configuration UI (thresholds are currently in code, not an admin panel)

---
*Built as a Round 2 submission for the Accenture Innovation Challenge 2026, PatientTriage.ai track.*
