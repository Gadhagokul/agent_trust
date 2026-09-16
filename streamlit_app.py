# streamlit_app.py
"""Agent Trust Score Dashboard (single page, frontend-only viewer).

Runs ``GET /v1/trust-score?agent_id=<id>`` against the FastAPI backend and
renders the full transparent scoring breakdown. Display only: trust scores are
always computed by the backend. The Supplier Quota feature lives in the API
and is not surfaced here.

Run:  streamlit run streamlit_app.py
"""

import requests
import streamlit as st
from pydantic_settings import BaseSettings, SettingsConfigDict


# ──────────────────────────────────────────────
# Config & API client
# ──────────────────────────────────────────────
class Settings(BaseSettings):
    API_BASE_URL: str = "http://127.0.0.1:8000"
    TRUST_API_TOKEN: str = ""
    REQUEST_TIMEOUT: int = 8

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class TrustAPIClient:
    def __init__(
        self,
        settings: Settings | None = None,
    ):
        settings = settings or Settings()
        self.base_url = settings.API_BASE_URL.rstrip("/")
        self.headers = {"Accept": "application/json"}
        if settings.TRUST_API_TOKEN:
            self.headers["Authorization"] = f"Bearer {settings.TRUST_API_TOKEN}"
        self.timeout = settings.REQUEST_TIMEOUT

    def get_trust_score(self, agent_id: int) -> dict:
        try:
            resp = requests.get(
                f"{self.base_url}/v1/trust-score",
                params={"agent_id": agent_id},
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Connection error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("Unauthorized: check TRUST_API_TOKEN / API_BASE_URL.")
        if resp.status_code == 404:
            raise RuntimeError("Not found: verify the agent ID.")
        if resp.status_code == 429:
            raise RuntimeError("Rate limit exceeded, slow down requests.")
        if resp.status_code >= 500:
            raise RuntimeError("Server error: backend unavailable (503).")
        if resp.status_code != 200:
            raise RuntimeError(f"API error: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


# ──────────────────────────────────────────────
# Theme (light / enterprise)
# ──────────────────────────────────────────────
def apply_theme():
    st.set_page_config(
        page_title="Agent Trust Score Dashboard",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(
        """
        <style>
        .stApp { background-color: #FFFFFF; }
        .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1100px; }

        .page-title {
            color: #1E3A8A; font-size: 2.3rem; font-weight: 800;
            margin-bottom: 4px; letter-spacing: -0.01em;
        }
        .page-subtitle { color: #64748B; font-size: 1rem; margin-bottom: 20px; }

        .section-title {
            color: #1E3A8A; font-size: 1.45rem; font-weight: 700;
            margin: 28px 0 14px; padding-bottom: 8px;
            border-bottom: 2px solid #E2E8F0;
        }

        div.metric-card {
            background-color: #1E293B;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 18px 22px;
            box-shadow: 0 4px 10px rgba(30, 41, 59, 0.12);
            height: 100%;
        }
        div.metric-card.emphasized {
            border: 2px solid #2563EB;
            background: linear-gradient(135deg, #1E293B, #28334C);
        }
        .metric-title {
            color: #94A3B8; font-size: 0.82rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px;
        }
        .metric-value { color: #F8FAFC; font-size: 2rem; font-weight: 800; line-height: 1.2; }
        .metric-value.accent { color: #60A5FA; }
        .metric-subtext { color: #8B9BB4; font-size: 0.82rem; margin-top: 4px; }

        div.info-box {
            background-color: #ECFDF5; border: 1px solid #A7F3D0;
            border-radius: 12px; padding: 14px 20px; color: #065F46;
            font-size: 1.05rem; font-weight: 600; margin: 6px 0 8px;
        }
        div.warn-box {
            background-color: #FFF1F2; border: 1px solid #FECDD3;
            border-radius: 12px; padding: 16px 20px; color: #9F1239;
            margin: 10px 0 8px;
        }
        .warn-title {
            font-size: 1.1rem; font-weight: 800; margin-bottom: 6px;
            color: #BE123C;
        }
        .warn-reasons { font-size: 0.98rem; line-height: 1.6; }

        .tier-badge {
            display: inline-block; padding: 12px 26px; border-radius: 50px;
            font-size: 1.35rem; font-weight: 800; letter-spacing: 0.02em;
            box-shadow: 0 6px 18px rgba(30, 41, 59, 0.18); margin: 6px 0 4px;
        }
        .tier-HIGH RISK { background: #DBEAFE; color: #1E40AF; }
        .tier-SILVER { background: #D1FAE5; color: #065F46; }
        .tier-GOLD { background: #FEF3C7; color: #92400E; }
        .tier-PLATINUM { background: #EDE9FE; color: #5B21B6; }
        .tier-BRONZE { background: #FFEDD5; color: #9A3412; }

        .calc-caption { color: #64748B; font-size: 0.88rem; margin-top: 22px; }
        div[data-testid="stTabs"] button p { font-size: 1rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_title(text: str):
    st.markdown(f'<div class="section-title">{text}</div>', unsafe_allow_html=True)


def metric_card(title: str, value: str, subtext: str = "", accent: bool = False):
    css = "emphasized" if accent else ""
    value_css = "metric-value accent" if accent else "metric-value"
    st.markdown(
        f"""
        <div class="metric-card {css}">
            <div class="metric-title">{title}</div>
            <div class="{value_css}">{value}</div>
            <div class="metric-subtext">{subtext}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────
def _num(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value}"


def _fmt_currency(value) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.0f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


# ──────────────────────────────────────────────
# Page
# ──────────────────────────────────────────────
def render():
    apply_theme()

    client = TrustAPIClient()

    st.markdown(
        '<div class="page-title">🛡️ Agent Trust Score Dashboard</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="page-subtitle">Transparent trust scoring: Reliability · Financial · '
        "Experience · Search-to-Booking</div>",
        unsafe_allow_html=True,
    )

    col_id, col_btn = st.columns([3, 1])
    with col_id:
        agent_id = st.number_input(
            "Enter Agent ID", min_value=1, step=1, value=1, label_visibility="visible"
        )
    with col_btn:
        st.write("")
        fetch = st.button("Fetch Score", use_container_width=True, type="primary")

    if fetch:
        try:
            st.session_state["trust_result"] = client.get_trust_score(int(agent_id))
        except RuntimeError as exc:
            st.error(exc)
            st.session_state.pop("trust_result", None)
            return

    data = st.session_state.get("trust_result")
    if not data:
        st.info("Enter an agent ID and click 'Fetch Score' to view the breakdown.")
        return

    _render_agent_header(data)
    _render_breakdown(data)
    _render_score_summary(data)
    _render_conversions(data)
    _render_credit_signals(data)

    st.markdown(
        f'<div class="calc-caption">Calculated at: {data.get("calculated_at", "—")}</div>',
        unsafe_allow_html=True,
    )


def _render_agent_header(data: dict):
    agent_name = data.get("agent_name") or f"Agent {data.get('agent_id')}"
    st.markdown(f'<div class="info-box">Agent: {agent_name}</div>', unsafe_allow_html=True)

    if data.get("high_risk_flag"):
        reasons = data.get("high_risk_reasons") or ["High risk of further default."]
        reasons_html = "<br>".join(str(r) for r in reasons)
        st.markdown(
            f"""
            <div class="warn-box">
                <div class="warn-title">🚨 SEVERE WARNING:</div>
                <div class="warn-reasons">{reasons_html}<br>High risk of further default.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    tier = (data.get("tier") or "UNKNOWN").upper()
    emoji = {
        "PLATINUM": "🟣",
        "GOLD": "🟡",
        "SILVER": "🟢",
        "BRONZE": "🟤",
        "HIGH RISK": "🔵",
    }.get(tier, "⚪")
    st.markdown(
        f'<span class="tier-badge tier-{tier}">{emoji} {tier} TIER</span>',
        unsafe_allow_html=True,
    )


def _render_breakdown(data: dict):
    section_title("Transparent Trust Score Breakdown")
    scores = data.get("scores", {})

    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Reliability (40%)", _num(scores.get("reliability_score")))
    with c2:
        metric_card("Financial (25%)", _num(scores.get("financial_score")))
    with c3:
        metric_card("Experience (15%)", _num(scores.get("experience_score")))

    s2b = scores.get("search_to_booking_score")
    if s2b is None:
        st.markdown(
            """
            <div class="metric-card" style="margin-top:14px;">
                <div class="metric-title">Search-to-Booking (20%)</div>
                <div class="metric-value" style="color:#94A3B8;">N/A</div>
                <div class="metric-subtext">No search data — composite renormalized over the
                active components.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="metric-card" style="margin-top:14px;">
                <div class="metric-title">Search-to-Booking (20%)</div>
                <div class="metric-value">{_num(s2b)}</div>
                <div class="metric-subtext">Effective searches → confirmed/ticketed bookings</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_score_summary(data: dict):
    section_title("Overall Trust Score")
    scores = data.get("scores", {})

    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card(
            "Composite",
            _num(scores.get("composite_trust_score")),
            "Weighted components",
        )
    with c2:
        metric_card(
            "ML Calibration",
            _num(scores.get("ml_calibration_score")),
            "Model confidence",
        )
    with c3:
        metric_card(
            "Final /100",
            _num(scores.get("overall_score")),
            "Tier assignment",
            accent=True,
        )


def _tab_metrics(label: str, data: dict | None):
    if not data:
        st.info("No conversion data available for this window.")
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Total Searches", f"{data.get('searches', 0):,}")
    with c2:
        metric_card(
            "System Failures (BookStep)",
            f"{data.get('adjusted_bookstep_failed', 0):,}",
            f"raw {data.get('bookstep_failed', 0):,}",
        )
    with c3:
        metric_card(
            "Agent Failures",
            f"{data.get('other_step_failed', 0):,}",
            "Failed at other steps",
        )
    with c4:
        metric_card(
            "Effective Searches",
            f"{data.get('effective_searches', 0):,}",
            "Failures already capped",
        )

    c5, c6, c7 = st.columns(3)
    with c5:
        metric_card(
            "Bookings",
            f"{data.get('bookings', 0):,}",
            "Confirmed / ticketed",
            accent=bool(data.get("bookings")),
        )
    with c6:
        metric_card(
            "Revenue Output",
            _fmt_currency(data.get("booking_volume")),
            "Total booked amount",
        )
    with c7:
        metric_card(
            "Avg Value",
            _fmt_currency(data.get("avg_booking_value")),
            f"Stability ±{_fmt_currency(data.get('revenue_consistency'))}",
        )


def _render_conversions(data: dict):
    section_title("Search-to-Booking Conversions")
    features = data.get("features", {})
    windows = [
        ("Daily", "daily"),
        ("Weekly", "weekly"),
        ("Monthly", "monthly"),
        ("Yearly (Lifetime)", "yearly"),
    ]
    tabs = st.tabs([label for label, _key in windows])
    for tab, (_label, key) in zip(tabs, windows, strict=True):
        with tab:
            _tab_metrics(_label, features.get(key))

    activity = features.get("search_activity") or {}
    if activity:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            metric_card("Searches (365d)", f"{activity.get('searches', 0):,}", "Created + reused")
        with c2:
            metric_card("Created", f"{activity.get('created', 0):,}", "New search sessions")
        with c3:
            metric_card("Reused", f"{activity.get('reused', 0):,}", "Cached sessions")
        with c4:
            metric_card(
                "Bookings (365d)",
                f"{activity.get('bookings', 0):,}",
                "Confirmed / ticketed",
                accent=bool(activity.get("bookings")),
            )


def _render_credit_signals(data: dict):
    section_title("Current Credit & Default Signals")
    features = data.get("features", {})

    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card(
            "Current Delay",
            f"{features.get('current_max_delay_days', 0)} days",
            "Max overdue on active cycle",
        )
    with c2:
        metric_card(
            "Unpaid Credits",
            f"{features.get('current_overdue_count', 0):,}",
            "Open overdue count",
        )
    with c3:
        metric_card(
            "Default Rate",
            _fmt_pct(features.get("current_overdue_ratio")),
            "Share of overdue credits",
        )


def main():
    render()


if __name__ == "__main__":
    main()