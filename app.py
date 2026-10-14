import io
import json
import re

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Manual Posting Summarizer", page_icon="📊", layout="wide")

# ------------------------------------------------------------------
# 1. PENGATURAN KATEGORI (default = aturan lama)
# ------------------------------------------------------------------
DEFAULT_RULES = [
    ("Sugar & Spice", "sugar & spice, loyalty reward adjustment"),
    ("Shio", "shio"),
    ("The Lounge", "lobby lounge"),
    ("In-Room Dining", "room service"),
    ("Minibar", "minibar"),
    ("Banquet", "banquet"),
    ("Wedding", "wedding"),
    ("Spa", "spa"),
    ("Guest Laundry", "laundry"),
    ("Room Revenue", "accomodation, checkout, upselling, check in, show"),
    ("Other Revenue", "miscellaneous"),
]
DEFAULT_FALLBACK = ""  # kosong = transaksi yang tidak cocok TIDAK ditarik
NOT_PULLED_LABEL = "Tidak Ditarik"
DEFAULT_EXCLUDE = "tax, service charge"
EXCLUDED_LABEL = "Tax / Service Charge"


def default_rules_df():
    return pd.DataFrame(DEFAULT_RULES, columns=["Kategori", "Keyword"])


def parse_rules(edf):
    """Tabel editor -> list[(nama, [keyword lowercase])]. Urutan baris = prioritas."""
    rules = []
    for _, r in edf.iterrows():
        name = "" if pd.isna(r.get("Kategori")) else str(r["Kategori"]).strip()
        raw = "" if pd.isna(r.get("Keyword")) else str(r["Keyword"])
        kws = [k.strip().lower() for k in raw.split(",") if k.strip()]
        if name and kws:
            rules.append((name, kws))
    return rules


def split_keywords(text):
    return [k.strip().lower() for k in text.split(",") if k.strip()]


def is_excluded(description, exclude_kws):
    """Cocok per KATA utuh (jadi 'taxi' tidak ikut terhitung sebagai 'tax')."""
    for kw in exclude_kws:
        if re.search(rf"\b{re.escape(kw)}\b", description, flags=re.IGNORECASE):
            return True
    return False


def get_category(description, rules, fallback, exclude_kws):
    if is_excluded(description, exclude_kws):
        return EXCLUDED_LABEL
    desc_lower = description.lower()
    for name, kws in rules:
        if any(kw in desc_lower for kw in kws):
            return name
    return fallback or NOT_PULLED_LABEL


# ------------------------------------------------------------------
# 2. PARSER PDF
# ------------------------------------------------------------------
NUM = r"-?\s?[\d,]+"
HEADER_RE = re.compile(r"^Transaction Code\s+(\d{3,5})\s+(.+)$")
ROW_RE = re.compile(
    rf"^(\d{{2}}-\d{{2}}-\d{{2}})\s+(\d{{2}}:\d{{2}})\s+(\S+)\s+(.*?)\s+(\d{{3,5}})\s+"
    rf"(.*?)\s+(IDR|USD\S*)\s+({NUM})\s+({NUM})\s+(\S+)$"
)


def to_int(s):
    return int(s.replace(" ", "").replace(",", ""))


@st.cache_data(show_spinner=False)
def parse_pdf(data: bytes):
    """Baca PDF sekali saja (kategori ditentukan terpisah, jadi aturan bisa diubah tanpa baca ulang)."""
    rows = []
    cur_code, cur_desc = None, None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.strip()

                m = HEADER_RE.match(line)
                if m:
                    cur_code, cur_desc = m.group(1), m.group(2).strip()
                    continue

                m = ROW_RE.match(line)
                if m and cur_code:
                    debit, credit = to_int(m.group(8)), to_int(m.group(9))
                    rows.append(
                        {
                            "Code": cur_code,
                            "Description": cur_desc,
                            "Debit": debit,
                            "Credit": credit,
                            "Net": debit - credit,
                        }
                    )
    return pd.DataFrame(rows)


def is_non_revenue(code):
    """Transfer (8370) dan semua pembayaran/settlement (9xxx) bukan revenue."""
    c = int(code)
    return c == 8370 or c >= 9000


# ------------------------------------------------------------------
# 3. UI
# ------------------------------------------------------------------
st.title("📊 Manual Posting Summarizer")
st.caption("Upload PDF *Journal by Cashier and Transaction Code* untuk dijumlahkan per kategori.")

# ---- Pengaturan kategori ----
ss = st.session_state
ss.setdefault("rules_df", default_rules_df())
ss.setdefault("fallback", DEFAULT_FALLBACK)
ss.setdefault("exclude_text", DEFAULT_EXCLUDE)
ss.setdefault("ver", 0)

with st.expander("⚙️ Pengaturan kategori", expanded=False):
    st.markdown(
        "Tentukan kategori dengan **kata kunci** (pisahkan dengan koma). "
        "Sebuah transaksi masuk ke kategori **pertama** (dari atas) yang kata kuncinya "
        "ada di deskripsi kode transaksi. Anda bisa menambah, mengubah, atau menghapus baris."
    )
    edited = st.data_editor(
        ss.rules_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=f"rules_editor_{ss.ver}",
        column_config={
            "Kategori": st.column_config.TextColumn("Kategori", required=True),
            "Keyword": st.column_config.TextColumn("Keyword (pisahkan dengan koma)", required=True),
        },
    )
    c_a, c_b = st.columns(2)
    fallback = c_a.text_input(
        "Kategori default (kosongkan = yang tidak cocok tidak ditarik)", value=ss.fallback
    ).strip()
    exclude_text = c_b.text_input(
        "Dikecualikan dari revenue (kata kunci)", value=ss.exclude_text,
        help="Deskripsi yang mengandung kata ini (kata utuh) tidak dihitung sebagai revenue.",
    )

    b1, b2, b3 = st.columns(3)
    cfg_json = json.dumps(
        {
            "rules": [{"Kategori": n, "Keyword": ", ".join(k)} for n, k in parse_rules(edited)],
            "fallback": fallback,
            "exclude": exclude_text,
        },
        ensure_ascii=False, indent=2,
    )
    b1.download_button("💾 Simpan pengaturan (JSON)", cfg_json, "kategori_config.json", "application/json")
    if b2.button("↩️ Kembalikan ke default"):
        ss.rules_df, ss.fallback, ss.exclude_text = default_rules_df(), DEFAULT_FALLBACK, DEFAULT_EXCLUDE
        ss.ver += 1
        st.rerun()
    cfg_file = b3.file_uploader("Muat pengaturan (JSON)", type="json", key=f"cfg_up_{ss.ver}",
                                label_visibility="collapsed")
    if cfg_file is not None:
        try:
            cfg = json.load(cfg_file)
            ss.rules_df = pd.DataFrame(cfg["rules"], columns=["Kategori", "Keyword"])
            ss.fallback = cfg.get("fallback", DEFAULT_FALLBACK)
            ss.exclude_text = cfg.get("exclude", DEFAULT_EXCLUDE)
            ss.ver += 1
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"File pengaturan tidak valid: {e}")

rules = parse_rules(edited)
exclude_kws = split_keywords(exclude_text)

uploaded = st.file_uploader("Upload PDF", type="pdf")
if not uploaded:
    st.info("Silakan upload file PDF terlebih dahulu.")
    st.stop()

with st.spinner("Membaca PDF..."):
    df = parse_pdf(uploaded.getvalue())

if df.empty:
    st.error("Tidak ada transaksi yang terbaca. Pastikan formatnya sama dengan laporan Journal by Cashier and Transaction Code.")
    st.stop()

# ---- Tentukan kategori & buang non-revenue ----
cat_map = {d: get_category(d, rules, fallback, exclude_kws) for d in df["Description"].unique()}
df["Kategori"] = df["Description"].map(cat_map)

work = df[~df["Kategori"].isin([EXCLUDED_LABEL, NOT_PULLED_LABEL]) & ~df["Code"].apply(is_non_revenue)]
if work.empty:
    st.warning("Tidak ada transaksi revenue setelah Tax, Service Charge, Transfer, Pembayaran, dan kategori yang tidak ditarik dikeluarkan.")
    st.stop()

fmt = {"Debit": "{:,.0f}", "Credit": "{:,.0f}", "Net": "{:,.0f}"}

# ---- 1. Ringkasan per Kategori ----
summary = (
    work.groupby("Kategori")[["Debit", "Credit", "Net"]].sum()
    .sort_values("Net", ascending=False).reset_index()
)
st.subheader("Ringkasan per Kategori")
st.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)

# ---- 2. Rincian per Kode Transaksi ----
by_code = (
    work.groupby(["Kategori", "Code", "Description"])[["Debit", "Credit", "Net"]]
    .sum().reset_index().sort_values(["Kategori", "Code"])
)
st.subheader("Rincian per Kode Transaksi")
pick = st.selectbox("Filter kategori", ["Semua"] + summary["Kategori"].tolist())
view = by_code if pick == "Semua" else by_code[by_code["Kategori"] == pick]
st.dataframe(view.style.format(fmt), use_container_width=True, hide_index=True)
