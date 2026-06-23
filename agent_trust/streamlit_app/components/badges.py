import streamlit as st

def render_badges(badges: list[str]):
    if not badges:
        return
        
    st.markdown("### Gamification Badges")
    
    badge_html = "<div style='display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px;'>"
    
    badge_colors = {
        "Trusted Partner": "#F6E05E", # Yellow/Gold
        "Perfect Payer": "#68D391",   # Green
        "Booking Champion": "#63B3ED",# Blue
        "Fraud Free": "#B794F4"       # Purple
    }
    
    badge_emojis = {
        "Trusted Partner": "🏆",
        "Perfect Payer": "💳",
        "Booking Champion": "✈️",
        "Fraud Free": "🛡️"
    }
    
    for badge in badges:
        color = badge_colors.get(badge, "#A0AEC0")
        emoji = badge_emojis.get(badge, "🏅")
        
        badge_html += f"""
        <div style='background-color: {color}; color: #1A202C; padding: 5px 15px; border-radius: 20px; font-weight: bold; display: flex; align-items: center; gap: 5px;'>
            <span>{emoji}</span> {badge}
        </div>
        """
        
    badge_html += "</div>"
    st.markdown(badge_html, unsafe_allow_html=True)
