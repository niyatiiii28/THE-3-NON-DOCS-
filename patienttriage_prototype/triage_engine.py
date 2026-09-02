"""
PatientTriage.ai — Core Scoring Engine
========================================
Rules-based, age-adjusted, uncertainty-aware triage scoring.

Design principles (see README for full rationale):
1. Rules-based over black-box ML: every score must be explainable in one
   glance by a nurse who has 3 seconds to read it.
2. Age-adjusted thresholds: a single adult-calibrated vital sign model is
   treated as a safety bug, not a simplification. Pediatric, adult, and
   geriatric bands each have their own critical/warning ranges.
3. Escalation bias under uncertainty: whenever data is missing, ambiguous,
   or self-reporting conflicts with observed vitals, the system rounds
   toward MORE urgent, never less. Under-triage is the failure mode we
   design against.
4. Confidence is a first-class output, not an afterthought — every score
   ships with a confidence percentage and the reasons behind it.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import uuid


# ---------------------------------------------------------------------------
# ESI-style severity scale (1 = most urgent / resuscitation, 5 = least urgent)
# ---------------------------------------------------------------------------
SEVERITY_LABELS = {
    1: "Level 1 — Resuscitation (immediate)",
    2: "Level 2 — Emergent (<10 min)",
    3: "Level 3 — Urgent (<30 min)",
    4: "Level 4 — Less urgent (<60 min)",
    5: "Level 5 — Non-urgent (<120 min)",
}

# Safe maximum wait time (minutes) before a waiting patient at this level
# MUST be re-assessed, regardless of what else is happening in the department.
SAFE_WAIT_MINUTES = {1: 0, 2: 10, 3: 30, 4: 60, 5: 120}

# Reassessment interval (minutes) by effective severity — higher risk = more frequent
# Based on half the safe-wait window for Levels 2-4, continuous for Level 1.
REASSESS_INTERVAL_MINUTES = {1: 1, 2: 5, 3: 10, 4: 20, 5: 60}


def get_age_band(age: int) -> str:
    if age < 12:
        return "pediatric"
    elif age >= 65:
        return "geriatric"
    return "adult"


# ---------------------------------------------------------------------------
# Age-adjusted vital sign thresholds.
# Format: (critical_low, warning_low, warning_high, critical_high)
# A value outside the "critical" band is a hard trigger toward Level 1/2
# regardless of the composite score.
# ---------------------------------------------------------------------------
VITAL_THRESHOLDS = {
    "pediatric": {
        "heart_rate":     (60, 90, 140, 180),
        "resp_rate":      (15, 20, 30, 40),
        "spo2":           (85, 92, 100, 100),   # low is bad; treat 100 as ceiling
        "temp_c":         (35.0, 36.0, 38.0, 40.0),  # 38.5 in a toddler is more urgent than in an adult
        "bp_systolic":    (70, 80, 120, 160),
    },
    "adult": {
        "heart_rate":     (40, 50, 100, 130),
        "resp_rate":      (8, 12, 20, 28),
        "spo2":           (88, 94, 100, 100),
        "temp_c":         (35.0, 36.0, 38.3, 39.5),
        "bp_systolic":    (80, 90, 140, 180),
    },
    "geriatric": {
        "heart_rate":     (45, 55, 95, 120),
        "resp_rate":      (10, 14, 22, 26),
        "spo2":           (88, 93, 100, 100),
        "temp_c":         (35.5, 36.0, 37.8, 38.5),  # blunted febrile response — lower threshold is MORE urgent
        "bp_systolic":    (90, 100, 130, 170),
    },
}

VITAL_WEIGHT = 20          # composite score points per vital that's in "warning" range
CRITICAL_VITAL_TRIGGER = True  # any critical vital forces Level <=2


def _score_vital(band: str, vital_name: str, value: Optional[float]):
    """Returns (points, is_critical, note) for a single vital."""
    if value is None:
        return 0, False, None
    lo_crit, lo_warn, hi_warn, hi_crit = VITAL_THRESHOLDS[band][vital_name]

    if vital_name == "spo2":
        # only low SpO2 matters
        if value < lo_crit:
            return VITAL_WEIGHT * 2, True, f"SpO2 {value}% critically low for {band}"
        if value < lo_warn:
            return VITAL_WEIGHT, False, f"SpO2 {value}% below normal for {band}"
        return 0, False, None

    if value < lo_crit or value > hi_crit:
        return VITAL_WEIGHT * 2, True, f"{vital_name} {value} outside critical range for {band} ({lo_crit}-{hi_crit})"
    if value < lo_warn or value > hi_warn:
        return VITAL_WEIGHT, False, f"{vital_name} {value} outside normal range for {band}"
    return 0, False, None


# High-risk chief complaint / symptom keywords → immediate weight, independent
# of vitals (captures "sick-looking but vitals not yet decompensated" cases).
RED_FLAG_SYMPTOMS = {
    "chest pain": 35, "chest discomfort": 30, "difficulty breathing": 40, "shortness of breath": 35,
    "stroke symptoms": 45, "slurred speech": 40, "facial droop": 45,
    "severe bleeding": 40, "unresponsive": 60, "seizure": 45,
    "suicidal ideation": 35, "severe allergic reaction": 45, "anaphylaxis": 50,
}

# Symptoms known to be commonly under-reported (self-report unreliable) —
# these REDUCE confidence rather than reduce severity, per the escalation-bias rule.
UNDER_REPORTING_FLAGS = {"vague pain", "generalized weakness", "just feels off", "fatigue"}


@dataclass
class TriageResult:
    patient_id: str
    severity: int
    severity_label: str
    confidence: float
    composite_score: float
    triggered_flags: list = field(default_factory=list)
    escalated_for_uncertainty: bool = False
    escalated_for_critical_vital: bool = False
    age_band: str = ""
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def score_patient(patient: dict) -> TriageResult:
    age = patient["age"]
    band = get_age_band(age)
    vitals = patient.get("vitals", {})
    flags = []
    composite = 0.0
    any_critical = False

    # --- 1. Vitals, age-adjusted ---
    for vname in ["heart_rate", "resp_rate", "spo2", "temp_c", "bp_systolic"]:
        pts, is_crit, note = _score_vital(band, vname, vitals.get(vname))
        composite += pts
        if is_crit:
            any_critical = True
        if note:
            flags.append(note)

    # --- 2. Red-flag chief complaint / symptoms (independent signal) ---
    complaint_text = " ".join(
        [patient.get("chief_complaint", "")] + patient.get("self_reported_symptoms", [])
    ).lower()
    for phrase, weight in RED_FLAG_SYMPTOMS.items():
        if phrase in complaint_text:
            composite += weight
            flags.append(f"Red-flag symptom reported: '{phrase}'")

    # --- 3. Data completeness / confidence ---
    confidence = 100.0
    missing_vitals = [v for v in ["heart_rate", "resp_rate", "spo2", "temp_c", "bp_systolic"] if vitals.get(v) is None]
    if missing_vitals:
        confidence -= 8 * len(missing_vitals)
        flags.append(f"Missing vitals: {', '.join(missing_vitals)}")

    if not patient.get("has_history", False):
        confidence -= 15
        flags.append("Zero-history patient (no prior records on file)")

    under_report_hits = [p for p in UNDER_REPORTING_FLAGS if p in complaint_text]
    if under_report_hits:
        confidence -= 10
        flags.append("Presentation includes commonly under-reported symptom language")

    if patient.get("age", 30) >= 65 and "pain" not in complaint_text:
        # geriatric patients frequently under-report pain — flag it, don't score it away
        confidence -= 5
        flags.append("Geriatric patient — pain/symptom under-reporting risk")

    confidence = max(confidence, 25.0)  # never claim near-zero confidence silently; floor forces visible caution

    # --- 4. Map composite score -> severity level ---
    if composite >= 80:
        severity = 1
    elif composite >= 50:
        severity = 2
    elif composite >= 30:
        severity = 3
    elif composite >= 10:
        severity = 4
    else:
        severity = 5

    escalated_for_uncertainty = False
    escalated_for_critical_vital = False

    # --- 5. SAFETY OVERRIDE RULES (asymmetric cost: bias toward escalation) ---
    if any_critical and severity > 2:
        severity = 2
        escalated_for_critical_vital = True
        flags.append("ESCALATED: critical vital reading forces minimum Level 2")

    if confidence < 60 and severity > 1:
        severity = max(1, severity - 1)
        escalated_for_uncertainty = True
        flags.append(f"ESCALATED: confidence {confidence:.0f}% below safe threshold — rounded up one level")

    return TriageResult(
        patient_id=patient["id"],
        severity=severity,
        severity_label=SEVERITY_LABELS[severity],
        confidence=round(confidence, 1),
        composite_score=round(composite, 1),
        triggered_flags=flags,
        escalated_for_uncertainty=escalated_for_uncertainty,
        escalated_for_critical_vital=escalated_for_critical_vital,
        age_band=band,
    )


def needs_reassessment(severity: int, minutes_waited: float) -> bool:
    """Waiting-room safety monitor: has this patient exceeded their safe wait window?"""
    return minutes_waited >= SAFE_WAIT_MINUTES[severity]


def new_audit_entry(patient_id, original: TriageResult, new_severity: int, clinician_id: str, reason: str):
    return {
        "audit_id": str(uuid.uuid4())[:8],
        "patient_id": patient_id,
        "timestamp": datetime.utcnow().isoformat(),
        "original_severity": original.severity,
        "original_confidence": original.confidence,
        "overridden_severity": new_severity,
        "clinician_id": clinician_id,
        "reason": reason,
        "system_flags_at_time": original.triggered_flags,
    }


def simulate_vital_deterioration(vitals: dict, severity: int, dt_minutes: float, age_band: str) -> dict:
    """
    Simulate vital sign evolution over time.
    Higher severity = higher probability of deterioration.
    Returns updated vitals dict (non-mutating).
    """
    import random
    import math

    new_vitals = vitals.copy()
    if not new_vitals:
        return new_vitals

    # Deterioration probability scales with severity (1=highest risk)
    # Level 1: ~15% per reassessment, Level 5: ~3%
    base_deterioration_prob = {1: 0.15, 2: 0.12, 3: 0.08, 4: 0.05, 5: 0.03}
    prob = base_deterioration_prob.get(severity, 0.05)

    # Time factor: longer intervals = more change
    time_factor = math.sqrt(max(dt_minutes, 1) / 5.0)

    for vname, value in new_vitals.items():
        if value is None:
            continue

        # Only deteriorate if random check passes
        if random.random() > prob * time_factor:
            continue

        # Get thresholds for this vital and age band to bias direction
        thresholds = VITAL_THRESHOLDS.get(age_band, VITAL_THRESHOLDS["adult"])
        lo_crit, lo_warn, hi_warn, hi_crit = thresholds.get(vname, (0, 0, 0, 0))

        if vname == "heart_rate":
            # Tachycardia bias for deterioration
            drift = random.gauss(3, 5)
            new_vitals[vname] = max(30, min(220, value + drift))
        elif vname == "resp_rate":
            # Tachypnea bias
            drift = random.gauss(1.5, 2)
            new_vitals[vname] = max(4, min(60, value + drift))
        elif vname == "spo2":
            # Desaturation bias (only goes down)
            drift = -abs(random.gauss(1, 2))
            new_vitals[vname] = max(50, min(100, value + drift))
        elif vname == "temp_c":
            # Fever bias for infection/sepsis
            drift = random.gauss(0.1, 0.3)
            new_vitals[vname] = round(max(32.0, min(42.0, value + drift)), 1)
        elif vname == "bp_systolic":
            # Hypotension bias for shock
            drift = -abs(random.gauss(3, 6))
            new_vitals[vname] = max(40, min(250, value + drift))

    return new_vitals
