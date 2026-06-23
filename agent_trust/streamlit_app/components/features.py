import streamlit as st


def render_features(features: dict):
    st.markdown("### Search-to-Booking Conversions")
    tabs = st.tabs(["Daily (24h)", "Weekly (7d)", "Monthly (30d)", "Yearly/Lifetime"])

    timeframes = [
        (tabs[0], features.get('daily', {})),
        (tabs[1], features.get('weekly', {})),
        (tabs[2], features.get('monthly', {})),
        (tabs[3], features.get('yearly', {}))
    ]

    for tab, data in timeframes:
        with tab:
            if not data:
                st.info("No conversion data available.")
                continue

            # Row 1: Search breakdown
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Total Searches</div>
                        <div class="metric-value">{data.get('searches', 0)}</div>
                        <div class="metric-subtext">Raw queries from search_sessions</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with c2:
                failed = data.get('bookstep_failed', 0)
                adjusted = data.get('adjusted_bookstep_failed', 0)
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">System Failures (BookStep)</div>
                        <div class="metric-value" style="color:#FC8181;">{adjusted} <span style="font-size:0.9rem;color:#718096">(Raw: {failed})</span></div>
                        <div class="metric-subtext">Excluded from rate (capped at 70%)</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with c3:
                other_failed = data.get('other_step_failed', 0)
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Agent Failures</div>
                        <div class="metric-value" style="color:#D69E2E;">{other_failed}</div>
                        <div class="metric-subtext">Non-BookStep failures; impacts score</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with c4:
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Effective Searches</div>
                        <div class="metric-value">{data.get('effective_searches', 0)}</div>
                        <div class="metric-subtext">Actual intent base</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            st.markdown("<br>", unsafe_allow_html=True)

            # Row 2: Conversion outcome
            c5, c7, c8 = st.columns(3)
            with c5:
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Bookings</div>
                        <div class="metric-value">{data.get('bookings', 0)}</div>
                        <div class="metric-subtext">Successful completions</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with c7:
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Revenue Output</div>
                        <div class="metric-value"><span style="font-size:1rem;color:#718096">$</span> {int(data.get('booking_volume', 0.0))}</div>
                        <div class="metric-subtext">Booked amount</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with c8:
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="metric-title">Avg Value & Stability</div>
                        <div class="metric-value"><span style="font-size:1rem;color:#718096">$</span> {int(data.get('avg_booking_value', 0))}</div>
                        <div class="metric-subtext">StdDev: ±${int(data.get('revenue_consistency', 0))}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )


            # Warn only when effective searches (after removing system faults) are high with no bookings
            if data.get('effective_searches', 0) >= 10 and data.get('bookings', 0) == 0:
                st.warning(
                    "⚠️ **High Effective Search Volume without Bookings**: "
                    "Agent has genuine intent signals but zero conversions.",
                    icon="⚠️"
                )

    st.markdown("<br>### Current Credit & Default Signals", unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Current Delay</div>
                <div class="metric-value">{features.get('current_credit_delay_days', 0)} <span style="font-size:1rem;color:#718096">days</span></div>
                <div class="metric-subtext">Active cycle overdue days</div>
            </div>
            """, 
            unsafe_allow_html=True
        )

    with col2:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Unpaid Credits</div>
                <div class="metric-value">{features.get('unpaid_count', 0)}</div>
                <div class="metric-subtext">Total defaulted loans</div>
            </div>
            """, 
            unsafe_allow_html=True
        )

    with col3:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Default Rate</div>
                <div class="metric-value">{features.get('unpaid_ratio', 0)} <span style="font-size:1rem;color:#718096">%</span></div>
                <div class="metric-subtext">% of credit completely unpaid</div>
            </div>
            """, 
            unsafe_allow_html=True
        )