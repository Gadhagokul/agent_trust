import streamlit as st


def apply_theme():
    st.set_page_config(
        page_title="Agent Trust Dashboard",
        layout="wide",
        page_icon="📊"
    )

    st.markdown("""
        <style>
        /* Card Container */
        div.metric-card {
            background-color: #1E1E1E;
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        div.metric-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 12px rgba(0,0,0,0.2);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        /* Card Title */
        .metric-title {
            color: #A0AEC0;
            font-size: 0.9rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }
        
        /* Card Value */
        .metric-value {
            color: #FFFFFF;
            font-size: 2.2rem;
            font-weight: 700;
            line-height: 1.2;
        }
        
        /* Card Subtext */
        .metric-subtext {
            color: #718096;
            font-size: 0.85rem;
            margin-top: 4px;
        }
        
        /* Tier Badge Specific styling */
        .tier-badge {
            display: inline-block;
            padding: 12px 24px;
            border-radius: 50px;
            font-size: 1.5rem;
            font-weight: bold;
            text-align: center;
            margin-bottom: 20px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }
        .tier-PLATINUM { background: linear-gradient(135deg, #E5E4E2 0%, #B4B4B4 100%); color: #000; }
        .tier-GOLD { background: linear-gradient(135deg, #FFD700 0%, #DAA520 100%); color: #000; }
        .tier-SILVER { background: linear-gradient(135deg, #C0C0C0 0%, #A9A9A9 100%); color: #000; }
        .tier-BRONZE { background: linear-gradient(135deg, #CD7F32 0%, #8B4513 100%); color: #FFF; }
        </style>
    """, unsafe_allow_html=True)