import streamlit as st
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from streamlit_app.clients.trust_api import TrustAPIClient
from streamlit_app.components.metrics import render_scores
from streamlit_app.components.features import render_features
from streamlit_app.components.tier_badge import render_tier
from streamlit_app.utils.error_handler import handle_error
from streamlit_app.utils.formatter import format_datetime
from streamlit_app.styles.theme import apply_theme


apply_theme()


def main():
    st.title("📊 Agent Trust Score Dashboard")

    agent_id = st.number_input("Enter Agent ID", min_value=1, step=1)

    if st.button("Fetch Trust Score"):
        client = TrustAPIClient()

        try:
            data = client.get_trust_score(agent_id)

            st.success(f"Agent: {data['agent_name']}")

            if data.get("high_risk_flag"):
                reasons = data.get("high_risk_reasons", [])
                if reasons:
                    for reason in reasons:
                        st.error(f"🚨 **SEVERE WARNING**: {reason}. High risk of further default.")
                else:
                    st.error("🚨 **SEVERE WARNING**: High risk patterns detected. High risk of further default.")

            render_tier(data["tier"])
            
            from streamlit_app.components.badges import render_badges
            render_badges(data.get("badges", []))
            
            render_scores(data["scores"])
            render_features(data["features"])

            st.caption(
                f"Calculated at: {format_datetime(data['calculated_at'])}"
            )

        except Exception as e:
            handle_error(e)


if __name__ == "__main__":
    main()