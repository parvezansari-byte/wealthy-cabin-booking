"""Global styles. Everything visual lives here so views stay readable."""
import streamlit as st

PALETTE = {
    "available": ("#15803D", "#ECFDF3", "#B7EBC9"),   # text, background, border
    "occupied": ("#B42318", "#FEF3F2", "#F9C9C5"),
    "mine": ("#1D4ED8", "#EFF4FF", "#C3D4FE"),
    "maintenance": ("#344054", "#EAECF0", "#C9CED8"),
    "inactive": ("#667085", "#F9FAFB", "#D0D5DD"),
}
# Wealthy brand colours — change these three to the exact brand values if needed.
BRAND_PRIMARY = "#5B2BD6"   # buttons, links, highlights (Wealthy purple)
BRAND_DEEP = "#24104F"      # sidebar / banner base
BRAND_ACCENT = "#A78BFA"    # soft glows

ICONS = {"available": "●", "occupied": "■", "mine": "★", "maintenance": "▲", "inactive": "○"}

CSS = """
<style>
:root { --wcb-brand: __PRIMARY__; --wcb-brand-deep: __DEEP__; --wcb-brand-accent: __ACCENT__; }
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {
  --wcb-navy: var(--wcb-brand-deep); --wcb-primary: var(--wcb-brand); --wcb-ink: #0F172A; --wcb-muted: #5B6475;
  --wcb-line: #E3E8F2; --wcb-card: #FFFFFF; --wcb-bg: #F5F7FB; --wcb-radius: 14px;
}
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
}
/* ---- Background: soft brand-coloured glows over a faint dot grid ---- */
[data-testid="stAppViewContainer"], .stApp {
  background:
    radial-gradient(900px 520px at 8% -8%, rgba(91, 43, 214, .22), transparent 60%),
    radial-gradient(760px 480px at 100% 0%, rgba(167, 139, 250, .26), transparent 62%),
    radial-gradient(820px 560px at 85% 105%, rgba(236, 72, 153, .10), transparent 60%),
    radial-gradient(rgba(36, 16, 79, .07) 1px, transparent 1.2px) 0 0 / 22px 22px,
    linear-gradient(180deg, #FAF8FF 0%, #F1EDFB 100%) !important;
  background-attachment: fixed !important;
}
[data-testid="stHeader"] { background: transparent !important; }
.block-container { padding-top: 3.75rem; padding-bottom: 3rem; max-width: 1280px; }
h1, h2, h3 { letter-spacing: -0.01em; color: var(--wcb-ink); }
[data-testid="stSidebar"] {
  background:
    radial-gradient(420px 300px at 0% 0%, rgba(167, 139, 250, .28), transparent 70%),
    radial-gradient(380px 320px at 100% 100%, rgba(236, 72, 153, .16), transparent 70%),
    linear-gradient(180deg, #1A0B3D 0%, #24104F 55%, #3A1A80 100%) !important;
}
/* Cards float slightly above the background */
.wcb-metric, .wcb-card, .wcb-row, div[data-testid="stForm"] {
  box-shadow: 0 1px 2px rgba(15, 23, 42, .04), 0 8px 24px -12px rgba(36, 16, 79, .18);
}
.wcb-card { transition: transform .15s ease, box-shadow .15s ease; }
.wcb-card:hover { transform: translateY(-2px); box-shadow: 0 2px 4px rgba(15, 23, 42, .05), 0 16px 32px -14px rgba(36, 16, 79, .28); }
.wcb-hero { position: relative; overflow: hidden; box-shadow: 0 18px 40px -20px rgba(36, 16, 79, .55); }
.wcb-hero::after { content: ""; position: absolute; right: -60px; top: -80px; width: 280px; height: 280px; border-radius: 50%;
  background: radial-gradient(circle, rgba(196, 181, 253, .40), transparent 65%); pointer-events: none; }
.wcb-hero::before { content: ""; position: absolute; right: 120px; bottom: -120px; width: 240px; height: 240px; border-radius: 50%;
  background: radial-gradient(circle, rgba(244, 114, 182, .22), transparent 65%); pointer-events: none; }
.wcb-auth-brand .logo { filter: drop-shadow(0 6px 12px rgba(36, 16, 79, .25)); }
[data-testid="stSidebar"] * { color: #E7ECF8 !important; }
[data-testid="stSidebar"] a[aria-current="page"] { background: rgba(255,255,255,.12); border-radius: 8px; }
[data-testid="stSidebar"] .stButton button { background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.2); }

.wcb-hero { background: linear-gradient(120deg, #24104F 0%, #4A21B8 55%, #6D3BE8 100%); color: #fff; border-radius: var(--wcb-radius);
  padding: 22px 26px; margin-bottom: 18px; }
.wcb-hero h1 { color: #fff; font-size: 1.55rem; margin: 0 0 4px 0; padding: 0; }
.wcb-hero p { margin: 0; color: #E4DCFB; font-size: .92rem; }
.wcb-page-title { margin: 0 0 2px 0; font-size: 1.75rem; line-height: 1.25; font-weight: 700; color: var(--wcb-ink); }
.wcb-page-sub { margin: 0 0 18px 0; color: var(--wcb-muted); font-size: .92rem; }

.wcb-metric { background: var(--wcb-card); border: 1px solid var(--wcb-line); border-radius: var(--wcb-radius);
  padding: 14px 16px; height: 100%; }
.wcb-metric .k { font-size: .78rem; color: var(--wcb-muted); text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }
.wcb-metric .v { font-size: 1.7rem; font-weight: 700; color: var(--wcb-ink); line-height: 1.2; margin-top: 4px; }
.wcb-metric .s { font-size: .8rem; color: var(--wcb-muted); }

.wcb-card { background: var(--wcb-card); border: 1px solid var(--wcb-line); border-radius: var(--wcb-radius);
  padding: 16px 16px 10px 16px; border-top: 4px solid var(--wcb-line); min-height: 196px; }
.wcb-card .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.wcb-card .code { font-size: .74rem; font-weight: 600; color: var(--wcb-muted); letter-spacing: .05em; }
.wcb-card .name { font-size: 1.08rem; font-weight: 700; color: var(--wcb-ink); margin: 0; }
.wcb-card .meta { color: var(--wcb-muted); font-size: .82rem; margin-top: 2px; }
.wcb-card .line { font-size: .86rem; color: var(--wcb-ink); margin-top: 10px; }
.wcb-card .line b { font-weight: 600; }
.wcb-chips { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
.wcb-chip { font-size: .72rem; padding: 2px 8px; border-radius: 999px; background: #F1F4FA; color: #344054; border: 1px solid var(--wcb-line); }

.wcb-badge { display: inline-flex; align-items: center; gap: 6px; font-size: .76rem; font-weight: 600;
  padding: 3px 10px; border-radius: 999px; border: 1px solid; white-space: nowrap; }
.wcb-legend { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 12px 0; }

.wcb-row { background: var(--wcb-card); border: 1px solid var(--wcb-line); border-radius: 12px; padding: 12px 16px; }
.wcb-row .ref { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .78rem; color: var(--wcb-muted); }
.wcb-row .title { font-weight: 600; color: var(--wcb-ink); }
.wcb-row .when { color: #344054; font-size: .88rem; }

.wcb-empty { text-align: center; padding: 28px 16px; border: 1px dashed #CBD3E3; border-radius: var(--wcb-radius);
  color: var(--wcb-muted); background: #FAFBFE; }
.wcb-empty .t { font-weight: 600; color: var(--wcb-ink); margin-bottom: 4px; }

.wcb-auth-brand { text-align: center; margin: 10px 0 18px 0; }
.wcb-auth-brand .logo { font-size: 2rem; }
.wcb-auth-brand h1 { font-size: 1.45rem; margin: 6px 0 2px 0; padding: 0; }
.wcb-auth-brand p { color: var(--wcb-muted); margin: 0; font-size: .9rem; }

.stButton button[kind="primary"] { background: var(--wcb-primary); border-color: var(--wcb-primary); }
.stButton button { border-radius: 10px; font-weight: 600; }
.stButton button[kind="primary"]:hover { filter: brightness(1.08); }
button[data-baseweb="tab"][aria-selected="true"] { color: var(--wcb-brand) !important; }
div[data-baseweb="tab-highlight"] { background-color: var(--wcb-brand) !important; }
a, a:visited { color: var(--wcb-brand); }
div[data-testid="stForm"] { border-radius: var(--wcb-radius); border-color: var(--wcb-line); background: var(--wcb-card); }
@media (max-width: 640px) {
  .wcb-hero { padding: 16px; } .wcb-hero h1 { font-size: 1.2rem; }
  .wcb-card { min-height: 0; }
}
</style>
"""


def inject_css() -> None:
    css = (CSS.replace("__PRIMARY__", BRAND_PRIMARY).replace("__DEEP__", BRAND_DEEP)
              .replace("__ACCENT__", BRAND_ACCENT))
    st.markdown(css, unsafe_allow_html=True)
