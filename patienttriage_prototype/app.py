import json
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from triage_engine import (
    score_patient, needs_reassessment, new_audit_entry,
    SEVERITY_LABELS, SAFE_WAIT_MINUTES, REASSESS_INTERVAL_MINUTES,
    simulate_vital_deterioration, get_age_band,
)

st.set_page_config(page_title="PatientTriage.ai", layout="wide", page_icon="🏥")

DATA_PATH = "data/simulated_patients.json"

SEVERITY_COLOR = {1: "#b30000", 2: "#e8590c", 3: "#f2b705", 4: "#2f9e44", 5: "#1971c2"}


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
def init_state():
    if "patients" not in st.session_state:
        with open(DATA_PATH) as f:
            raw = json.load(f)
        now = datetime.utcnow()
        for i, p in enumerate(raw):
            p["arrival_time"] = now - timedelta(minutes=random.randint(0, 90))
            p["status"] = "waiting"
            p["last_reassessment_time"] = p["arrival_time"]
        st.session_state.patients = raw
        st.session_state.audit_log = []
        st.session_state.overrides = {}
        st.session_state.surge_active = False
        st.session_state.surge_extra = []
        # Demo/simulation state
        st.session_state.time_offset_minutes = 0
        st.session_state.previous_queue_state = {}  # patient_id -> {severity, position, last_reassess}
        st.session_state.reassessment_log = []  # list of reassessment events


init_state()


# ---------------------------------------------------------------------------
# Core queue/scoring functions (pure logic, no UI)
# ---------------------------------------------------------------------------
def rescored_queue():
    rows = []
    all_patients = st.session_state.patients + st.session_state.surge_extra
    # Use simulated time (real time + offset)
    now = datetime.utcnow() + timedelta(minutes=st.session_state.get("time_offset_minutes", 0))

    # Get previous state for change detection
    prev_state = st.session_state.get("previous_queue_state", {})

    # First pass: reassess and score all patients
    scored_patients = []
    for p in all_patients:
        reassess_info = maybe_reassess_patient(p, now)
        result = score_patient(p)
        effective_severity = st.session_state.overrides.get(p["id"], result.severity)
        effective_label = SEVERITY_LABELS[effective_severity]
        wait_min = (now - p["arrival_time"]).total_seconds() / 60
        reassess_flag = needs_reassessment(effective_severity, wait_min)
        scored_patients.append({
            "patient": p,
            "result": result,
            "effective_severity": effective_severity,
            "effective_label": effective_label,
            "is_overridden": p["id"] in st.session_state.overrides,
            "wait_min": wait_min,
            "reassess": reassess_flag,
            "reassess_info": reassess_info,
            "last_reassess_time": p.get("last_reassessment_time"),
        })

    # Sort by severity then wait time
    scored_patients.sort(key=lambda r: (r["effective_severity"], -r["wait_min"]))

    # Second pass: assign positions and detect changes
    current_state = {}
    for position, sp in enumerate(scored_patients):
        p = sp["patient"]
        pid = p["id"]
        prev = prev_state.get(pid, {})

        old_position = prev.get("position")
        position_change = None
        if old_position is not None and old_position != position:
            position_change = {"old": old_position + 1, "new": position + 1}

        old_severity = prev.get("severity")
        severity_change = None
        if old_severity is not None and old_severity != sp["effective_severity"]:
            severity_change = {"old": old_severity, "new": sp["effective_severity"]}

        # Track reassessment events for activity log
        if sp["reassess_info"].get("reassessed"):
            event = {
                "timestamp": now.isoformat(),
                "patient_id": pid,
                "patient_name": p["name"],
                "old_severity": sp["reassess_info"].get("old_severity"),
                "new_severity": sp["reassess_info"].get("new_severity"),
                "vital_changes": sp["reassess_info"].get("vital_changes", {}),
                "position_change": position_change,
                "elapsed_minutes": sp["reassess_info"].get("elapsed_minutes"),
            }
            st.session_state.reassessment_log.insert(0, event)
            # Keep only last 20 events
            if len(st.session_state.reassessment_log) > 20:
                st.session_state.reassessment_log = st.session_state.reassessment_log[:20]

        current_state[pid] = {
            "severity": sp["effective_severity"],
            "position": position,
            "last_reassess": sp["last_reassess_time"],
        }

        rows.append({
            "patient": p,
            "result": sp["result"],
            "effective_severity": sp["effective_severity"],
            "effective_label": sp["effective_label"],
            "is_overridden": sp["is_overridden"],
            "wait_min": sp["wait_min"],
            "reassess": sp["reassess"],
            "reassess_info": sp["reassess_info"],
            "last_reassess_time": sp["last_reassess_time"],
            "position": position + 1,
            "position_change": position_change,
            "severity_change": severity_change,
            "reassessed_this_cycle": sp["reassess_info"].get("reassessed", False),
        })

    # Update previous state for next render
    st.session_state.previous_queue_state = current_state
    return rows


def maybe_reassess_patient(patient: dict, now: datetime) -> dict:
    """
    Check if patient is due for vital reassessment based on effective severity.
    If due, simulate new vitals and update last_reassessment_time.
    Returns dict with reassessment details.
    """
    # Determine effective severity (override takes precedence for safety)
    engine_severity = score_patient(patient).severity
    override_severity = st.session_state.overrides.get(patient["id"])
    effective_severity = min(engine_severity, override_severity) if override_severity else engine_severity

    interval = REASSESS_INTERVAL_MINUTES.get(effective_severity, 60)
    last_reassess = patient.get("last_reassessment_time")
    if last_reassess is None:
        last_reassess = patient["arrival_time"]

    elapsed = (now - last_reassess).total_seconds() / 60
    if elapsed >= interval:
        # Time to reassess - simulate vital changes
        age_band = get_age_band(patient["age"])
        dt_minutes = elapsed
        old_vitals = patient.get("vitals", {}).copy()
        old_severity = score_patient(patient).severity
        new_vitals = simulate_vital_deterioration(
            patient.get("vitals", {}), effective_severity, dt_minutes, age_band
        )
        patient["vitals"] = new_vitals
        patient["last_reassessment_time"] = now
        new_severity = score_patient(patient).severity

        # Compute vital changes
        vital_changes = {}
        for k in old_vitals:
            if k in new_vitals and old_vitals[k] != new_vitals[k]:
                vital_changes[k] = {"old": old_vitals[k], "new": new_vitals[k]}

        return {
            "reassessed": True,
            "elapsed_minutes": round(elapsed, 1),
            "interval_minutes": interval,
            "old_vitals": old_vitals,
            "new_vitals": new_vitals,
            "vital_changes": vital_changes,
            "old_severity": old_severity,
            "new_severity": new_severity,
            "severity_changed": old_severity != new_severity,
        }
    return {
        "reassessed": False,
        "elapsed_minutes": round(elapsed, 1),
        "interval_minutes": interval,
    }


# ---------------------------------------------------------------------------
# Page rendering function
# ---------------------------------------------------------------------------
def render_app():
    """Main app rendering - called when running via streamlit run"""
    page = st.sidebar.radio("View", ["Live Queue", "Patient Detail & Override", "Surge Simulation", "Audit Log", "Design Notes"])

    # ---------------------------------------------------------------------------
    # Sidebar — Demo Controls (only on Live Queue)
    # ---------------------------------------------------------------------------
    if page == "Live Queue":
        st.sidebar.markdown("---")
        st.sidebar.markdown("### ⏱️ Demo Time Controls")
        st.sidebar.caption("Advance simulated time to trigger reassessments")

        c1, c2 = st.sidebar.columns(2)
        with c1:
            if st.button("+5 min", use_container_width=True):
                st.session_state.time_offset_minutes += 5
                st.rerun()
            if st.button("+15 min", use_container_width=True):
                st.session_state.time_offset_minutes += 15
                st.rerun()
        with c2:
            if st.button("+60 min", use_container_width=True):
                st.session_state.time_offset_minutes += 60
                st.rerun()
            if st.button("Reset", use_container_width=True, type="secondary"):
                st.session_state.time_offset_minutes = 0
                st.rerun()

        st.sidebar.markdown(f"**Simulated offset: {st.session_state.time_offset_minutes} min**")
        if st.session_state.time_offset_minutes > 0:
            sim_time = datetime.utcnow() + timedelta(minutes=st.session_state.time_offset_minutes)
            st.sidebar.caption(f"Simulated time: {sim_time.strftime('%H:%M:%S')}")

    st.sidebar.markdown("---")
    st.sidebar.markdown("**Safe max wait before re-assessment**")
    for lvl, mins in SAFE_WAIT_MINUTES.items():
        st.sidebar.markdown(f"- Level {lvl}: {mins} min")

    # ---------------------------------------------------------------------------
    # LIVE QUEUE
    # ---------------------------------------------------------------------------
    if page == "Live Queue":
        # Auto-refresh every 60 seconds to check for reassessments
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=60_000, key="queue_autorefresh")
        except ImportError:
            st.info("💡 Install 'streamlit-autorefresh' for auto-refresh: pip install streamlit-autorefresh")

        st.title("Live Triage Queue")
        st.caption("Sorted by severity, then by longest wait within severity. Scores refresh on every load. Auto-refreshes every 60s for vital reassessment.")

        rows = rescored_queue()
        overdue = [r for r in rows if r["reassess"]]
        if overdue:
            st.error(f"⚠️ {len(overdue)} patient(s) have exceeded their safe wait window and require immediate re-assessment.")

        for r in rows:
            p, res, wait = r["patient"], r["result"], r["wait_min"]
            eff_sev = r["effective_severity"]
            eff_label = r["effective_label"]
            color = SEVERITY_COLOR[eff_sev]
            pos = r["position"]
            reassessed = r.get("reassessed_this_cycle", False)
            severity_change = r.get("severity_change")
            position_change = r.get("position_change")
            last_reassess = r.get("last_reassess_time")
            reassess_info = r.get("reassess_info", {})

            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2.5, 3, 1.5, 1.5])
                with c1:
                    st.markdown(f"**{p['name']}**  ·  age {p['age']} ({res.age_band})")
                    st.caption(p["chief_complaint"])
                    # Show last reassessment time
                    if last_reassess:
                        if isinstance(last_reassess, str):
                            last_reassess = datetime.fromisoformat(last_reassess.replace('Z', '+00:00'))
                        mins_ago = (datetime.utcnow() - last_reassess.replace(tzinfo=None)).total_seconds() / 60
                        st.caption(f"🩺 Last reassessed: {mins_ago:.0f} min ago")
                with c2:
                    # Triage level with change indicator
                    triage_html = (
                        f"<span style='background:{color};color:white;padding:3px 10px;border-radius:6px;font-weight:600'>"
                        f"{eff_label}</span>"
                    )
                    if severity_change:
                        old_label = SEVERITY_LABELS[severity_change["old"]]
                        new_label = SEVERITY_LABELS[severity_change["new"]]
                        direction = "⬆" if severity_change["new"] < severity_change["old"] else "⬇"
                        triage_html += f" <span style='color:{color};font-weight:bold'>{direction} {old_label} → {new_label}</span>"
                    st.markdown(triage_html, unsafe_allow_html=True)

                    # Reassessment badge
                    if reassessed:
                        st.caption("🔄 **Reassessed this cycle**")
                        vi = reassess_info.get("vital_changes", {})
                        if vi:
                            changes_str = ", ".join([f"{k}: {v['old']:.1f}→{v['new']:.1f}" if isinstance(v['new'], float) else f"{k}: {v['old']}→{v['new']}" for k, v in vi.items()])
                            st.caption(f"Vitals: {changes_str}")

                    if r["is_overridden"]:
                        st.caption("✏️ Clinician override applied")
                    elif res.escalated_for_critical_vital or res.escalated_for_uncertainty:
                        st.caption("🔺 Escalated by safety rule (see Patient Detail)")
                with c3:
                    st.metric("Confidence", f"{res.confidence:.0f}%")
                with c4:
                    # Position with change indicator
                    pos_display = f"#{pos}"
                    if position_change:
                        direction = "⬆" if position_change["new"] < position_change["old"] else "⬇"
                        pos_display += f" {direction} #{position_change['old']}→#{position_change['new']}"
                    st.metric("Position", pos_display)
                    st.metric("Waiting", f"{wait:.0f} min")
                    if r["reassess"]:
                        st.markdown("🔴 **Re-assess now**")

        # --- Recent Reassessment Activity Log ---
        if st.session_state.get("reassessment_log"):
            st.markdown("---")
            with st.expander("📋 Recent Reassessment Activity", expanded=True):
                for event in st.session_state.reassessment_log[:10]:
                    ts = datetime.fromisoformat(event["timestamp"].replace('Z', '+00:00'))
                    time_str = ts.strftime("%H:%M:%S")

                    old_sev = event.get("old_severity")
                    new_sev = event.get("new_severity")
                    sev_str = ""
                    if old_sev is not None and new_sev is not None:
                        if old_sev != new_sev:
                            direction = "⬆" if new_sev < old_sev else "⬇"
                            sev_str = f" {direction} Level {old_sev} → Level {new_sev}"
                        else:
                            sev_str = f" (Level {new_sev} unchanged)"

                    pos_change = event.get("position_change")
                    pos_str = ""
                    if pos_change:
                        direction = "⬆" if pos_change["new"] < pos_change["old"] else "⬇"
                        pos_str = f" | Queue: {direction} #{pos_change['old']} → #{pos_change['new']}"

                    vitals = event.get("vital_changes", {})
                    vitals_str = ""
                    if vitals:
                        changes = []
                        for k, v in vitals.items():
                            if isinstance(v.get('new'), float):
                                changes.append(f"{k}: {v['old']:.1f}→{v['new']:.1f}")
                            else:
                                changes.append(f"{k}: {v['old']}→{v['new']}")
                        vitals_str = f" | Vitals: {', '.join(changes)}"

                    st.markdown(
                        f"`{time_str}` **{event['patient_name']}** ({event['patient_id']}){sev_str}{pos_str}{vitals_str}"
                    )

        st.info("Open **Patient Detail & Override** in the sidebar to inspect scoring logic or log a clinician override.")

    # ---------------------------------------------------------------------------
    # PATIENT DETAIL & OVERRIDE
    # ---------------------------------------------------------------------------
    elif page == "Patient Detail & Override":
        st.title("Patient Detail & Clinician Override")

        all_patients = st.session_state.patients + st.session_state.surge_extra
        names = [f"{p['id']} — {p['name']}" for p in all_patients]
        choice = st.selectbox("Select patient", names)
        idx = names.index(choice)
        patient = all_patients[idx]
        result = score_patient(patient)

        # --- Apply override for display ---
        effective_severity = st.session_state.overrides.get(patient["id"], result.severity)
        effective_label = SEVERITY_LABELS[effective_severity]
        is_overridden = patient["id"] in st.session_state.overrides

        col1, col2 = st.columns([1, 1])
        with col1:
            st.subheader("Intake data")
            st.json({
                "age": patient["age"], "age_band": result.age_band,
                "has_history_on_file": patient["has_history"],
                "chief_complaint": patient["chief_complaint"],
                "self_reported_symptoms": patient["self_reported_symptoms"],
                "vitals": patient["vitals"],
            })

        with col2:
            st.subheader("System recommendation")
            color = SEVERITY_COLOR[effective_severity]
            st.markdown(
                f"<h3 style='color:{color}'>{effective_label}</h3>",
                unsafe_allow_html=True,
            )
            if is_overridden:
                st.caption("✏️ **Clinician override active** — showing overridden level")
            st.metric("Confidence", f"{result.confidence:.0f}%")
            st.metric("Composite score (internal)", result.composite_score)
            st.markdown("**Why this score — explainability trace:**")
            for flag in result.triggered_flags:
                st.markdown(f"- {flag}")
            if not result.triggered_flags:
                st.markdown("- No abnormal vitals or red-flag symptoms detected")

        st.markdown("---")
        st.subheader("Clinician override")
        st.caption("Every override is logged with the original score, the new score, clinician ID, and a mandatory reason — required for audit/compliance under the assumed HIPAA jurisdiction.")

        oc1, oc2, oc3 = st.columns([1, 2, 1])
        with oc1:
            new_sev = st.selectbox("Override to level", options=[1, 2, 3, 4, 5],
                                    index=effective_severity - 1, format_func=lambda x: SEVERITY_LABELS[x])
        with oc2:
            reason = st.text_input("Reason (required)", placeholder="e.g., clinical exam shows patient more stable than vitals suggest")
        with oc3:
            clinician_id = st.text_input("Clinician ID", value="RN-1042")

        if st.button("Log override", type="primary"):
            if not reason.strip():
                st.warning("A reason is required to log an override (compliance requirement).")
            else:
                entry = new_audit_entry(patient["id"], result, new_sev, clinician_id, reason)
                st.session_state.audit_log.append(entry)
                st.session_state.overrides[patient["id"]] = new_sev
                st.success(f"Override logged: {patient['id']} moved from Level {result.severity} → Level {new_sev} by {clinician_id}.")
                st.rerun()

    # ---------------------------------------------------------------------------
    # SURGE SIMULATION
    # ---------------------------------------------------------------------------
    elif page == "Surge Simulation":
        st.title("Surge Simulation (3× normal volume)")
        st.caption("Simulates a mass-casualty / flu-season surge by injecting 2× additional arrivals on top of the baseline 18-20 patient panel, and shows how the safe-wait-window monitor responds.")

        if st.button("🚨 Trigger 3× surge", type="primary"):
            base = st.session_state.patients
            extra = []
            pool_complaints = [
                ("Multi-vehicle collision victim, chest and abdominal pain", ["chest pain"], 40),
                ("High fever, febrile seizure reported by parent", ["seizure"], 3),
                ("Elderly fall, hip pain, unable to bear weight", ["fall", "hip pain"], 81),
                ("Minor laceration from kitchen accident", ["cut"], 33),
                ("Chest tightness after exertion", ["chest pain"], 58),
                ("Anxiety attack, hyperventilating", ["shortness of breath"], 24),
            ]
            now = datetime.utcnow()
            for i in range(len(base) * 2):
                complaint, symptoms, age = random.choice(pool_complaints)
                hr = random.randint(70, 160)
                arrival = now - timedelta(minutes=random.randint(0, 20))
                extra.append({
                    "id": f"SURGE-{i+1:03d}",
                    "name": f"Surge Patient {i+1}",
                    "age": age,
                    "has_history": random.random() > 0.6,
                    "chief_complaint": complaint,
                    "self_reported_symptoms": symptoms,
                    "vitals": {
                        "heart_rate": hr, "resp_rate": random.randint(12, 34),
                        "spo2": random.randint(86, 99), "temp_c": round(random.uniform(36.4, 39.5), 1),
                        "bp_systolic": random.randint(80, 150),
                    },
                    "arrival_time": arrival,
                    "last_reassessment_time": arrival,
                    "status": "waiting",
                })
            st.session_state.surge_extra = extra
            st.session_state.surge_active = True
            st.rerun()

        if st.session_state.surge_active:
            rows = rescored_queue()
            df = pd.DataFrame([{
                "Severity": r["effective_severity"],
                "Confidence": r["result"].confidence,
                "Wait (min)": round(r["wait_min"], 1),
                "Needs re-assessment": r["reassess"],
            } for r in rows])

            st.subheader(f"Queue under surge: {len(rows)} patients (baseline was {len(st.session_state.patients)})")

            c1, c2, c3 = st.columns(3)
            c1.metric("Total in queue", len(rows))
            c2.metric("Awaiting re-assessment", int(df["Needs re-assessment"].sum()))
            c3.metric("Avg confidence", f"{df['Confidence'].mean():.0f}%")

            st.bar_chart(df["Severity"].value_counts().sort_index())
            st.caption("Severity distribution under surge load — note the system does NOT relax its thresholds under load. The safe-wait clock and critical-vital overrides apply identically at 1× or 3× volume; what changes operationally is that re-assessment alerts fire more frequently and should route to a dedicated 'surge charge nurse' role rather than being silently queued.")

            st.dataframe(df, use_container_width=True)

            if st.button("Reset surge"):
                st.session_state.surge_extra = []
                st.session_state.surge_active = False
                st.rerun()
        else:
            st.info("Click the button above to inject surge volume and see how the queue and re-assessment alerts respond.")

    # ---------------------------------------------------------------------------
    # AUDIT LOG
    # ---------------------------------------------------------------------------
    elif page == "Audit Log":
        st.title("Clinician Override Audit Log")
        st.caption("Assumed jurisdiction: HIPAA (US). Every override captures who, when, original vs. new severity, system flags at time of decision, and a mandatory free-text reason — the minimum required for post-hoc clinical and legal review.")

        if not st.session_state.audit_log:
            st.info("No overrides logged yet. Go to **Patient Detail & Override** to log one.")
        else:
            for entry in reversed(st.session_state.audit_log):
                with st.container(border=True):
                    st.markdown(f"**{entry['patient_id']}** — {entry['timestamp']} — by `{entry['clinician_id']}`")
                    st.markdown(f"Level **{entry['original_severity']}** (confidence {entry['original_confidence']}%) → Level **{entry['overridden_severity']}**")
                    st.markdown(f"Reason: _{entry['reason']}_")
                    with st.expander("System flags present at time of override"):
                        for f in entry["system_flags_at_time"]:
                            st.markdown(f"- {f}")

    # ---------------------------------------------------------------------------
    # DESIGN NOTES
    # ---------------------------------------------------------------------------
    elif page == "Design Notes":
        st.title("Design Notes & Assumptions")
        st.markdown("""
**Jurisdiction assumed:** HIPAA (United States). Audit log entries above are the minimum
fields a HIPAA-compliant override record needs: who acted, when, what changed, and why.
Patient identifiers here are simulated names — a production system would store only
hashed/tokenized IDs in logs and encrypt vitals/history at rest and in transit.

**Why rules-based over ML for the scoring core:** explainability under time pressure is
a harder constraint than marginal accuracy gains. A nurse must be able to see *why* a
score fired in the time it takes to glance at a screen. An ML risk model could sit
alongside this engine as a secondary signal that adjusts confidence — but never as the
sole or unexplainable driver of a severity level.

**Escalation bias (asymmetric cost design):** two explicit rules force the system toward
over-triage rather than under-triage: (1) any single critical vital reading forces a
minimum Level 2 regardless of composite score, and (2) confidence below 60% forces the
score one level more urgent. This trades some over-triage (which costs staff time) to
avoid under-triage (which costs lives) — matching the brief's explicit instruction to bias
toward escalation.

**Age-adjustment:** every vital is scored against age-band-specific thresholds
(pediatric / adult / geriatric), not a single adult-calibrated model. This is treated as
a hard safety requirement, not a nice-to-have.

**Zero-history patients:** absence of prior records lowers confidence (which can trigger
escalation) rather than being ignored or treated as "no risk factors."
        """)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    render_app()