import streamlit as st


def render_tier(tier: str):
    color_map = {
        "PLATINUM": "🟣",
        "GOLD": "🟡",
        "SILVER": "⚪",
        "BRONZE": "🟤"
    }
    color = color_map.get(tier, "🔵")

    # Render custom HTML badge
    st.markdown(
        f'<div class="tier-badge tier-{tier}">{color} {tier} TIER</div>',
        unsafe_allow_html=True
    )