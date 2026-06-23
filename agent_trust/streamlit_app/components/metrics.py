import streamlit as st


def render_scores(scores: dict):
    st.markdown("### Transparent Trust Score Breakdown")
    
    # 1. Component breakdown
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(label="Reliability (50%)", value=f"{scores.get('reliability_score', 0):.1f}")
    with col2:
        st.metric(label="Financial (30%)", value=f"{scores.get('financial_score', 0):.1f}")
    with col3:
        st.metric(label="Experience (20%)", value=f"{scores.get('experience_score', 0):.1f}")

    st.markdown("<hr style='margin: 10px 0;'>", unsafe_allow_html=True)

    # 2. Final calculations
    col_comp, col_ml, col_final = st.columns(3)
    
    with col_comp:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Composite Trust Score</div>
                <div class="metric-value">{scores.get('composite_trust_score', 0):.1f}</div>
                <div class="metric-subtext">80% Weight (Business Logic)</div>
            </div>
            """, 
            unsafe_allow_html=True
        )

    with col_ml:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">ML Calibration</div>
                <div class="metric-value">{scores.get('ml_calibration_score', 0):.1f}</div>
                <div class="metric-subtext">20% Weight (Random Forest)</div>
            </div>
            """, 
            unsafe_allow_html=True
        )

    with col_final:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title" style="color: #4299E1; font-weight: bold;">Final Trust Score</div>
                <div class="metric-value">{scores.get('overall_score', 0)} <span style="font-size:1rem;color:#718096">/ 100</span></div>
                <div class="metric-subtext">Used for Tier Assignment</div>
            </div>
            """, 
            unsafe_allow_html=True
        )
    
    st.markdown("<br>", unsafe_allow_html=True)