import io
import json
import re

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Journal Revenue Summarizer", page_icon="📊", layout="wide")

# ------------------------------------------------------------------
# 1. PENGATURAN KATEGORI (default = aturan lama Anda)
# ------------------------------------------------------------------
DEFAULT_RULES = [
    ("Sugar & Spice", "sugar & spice, loyalty reward adjustment, bf package"),
    ("Shio", "shio"),
    ("The Lounge", "lobby lounge"),
    ("In-Room Dining", "room service"),
    ("Minibar", "minibar"),
    ("Banquet", "banquet"),
    ("Wedding", "wedding"),
    ("Spa", "spa"),
    ("Guest Laundry", "laundry"),
    ("Room Revenue", "accomodation, checkout, upselling, check in, show"),
]
DEFAULT_FALLBACK = "Other Revenue"
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
    return fallback


# ------------------------------------------------------------------
# 2. PARSER PDF
# ------------------------------------------------------------------
NUM = r"-?\s?[\d,]+"
HEADER_RE = re.compile(r"^Transaction Code\s+(\d{3,5})\s+(.+)$")
TOTAL_RE = re.compile(rf"^Transaction Code\s*Total\s+({NUM})\s+({NUM})\s*$")
GRAND_RE = re.compile(rf"^Grand Total\s+({NUM})\s+({NUM})\s*$")
ROW_RE = re.compile(
    rf"^(\d{{2}}-\d{{2}}-\d{{2}})\s+(\d{{2}}:\d{{2}})\s+(\S+)\s+(.*?)\s+(\d{{3,5}})\s+"
    rf"(.*?)\s+(IDR|USD\S*)\s+({NUM})\s+({NUM})\s+(\S+)$"
)


def to_int(s):
    return int(s.replace(" ", "").replace(",", ""))


@st.cache_data(show_spinner=False)
def parse_pdf(data: bytes):
    """Baca PDF sekali saja (kategori ditentukan terpisah, jadi aturan bisa diubah tanpa baca ulang)."""
    rows, report_totals, grand = [], {}, None
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

                m = TOTAL_RE.match(line)
                if m and cur_code:
                    report_totals[cur_code] = (to_int(m.group(1)), to_int(m.group(2)))
                    continue

                m = GRAND_RE.match(line)
                if m:
                    grand = (to_int(m.group(1)), to_int(m.group(2)))
                    continue

                m = ROW_RE.match(line)
                if m and cur_code:
                    debit, credit = to_int(m.group(8)), to_int(m.group(9))
                    rows.append(
                        {
                            "Date": m.group(1),
                            "Time": m.group(2),
                            "Room": m.group(3),
                            "Name": m.group(4),
                            "Code": cur_code,
                            "Description": cur_desc,
                            "Currency": m.group(7),
                            "Debit": debit,
                            "Credit": credit,
                            "Net": debit - credit,
                            "Cash ID": m.group(10),
                        }
                    )
    return pd.DataFrame(rows), report_totals, grand


def is_non_revenue(code):
    """Transfer (8370) dan semua pembayaran/settlement (9xxx) bukan revenue."""
    c = int(code)
    return c == 8370 or c >= 9000


# ------------------------------------------------------------------
# 3. UI
# ------------------------------------------------------------------
st.title("📊 Journal Revenue Summarizer")
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
    fallback = c_a.text_input("Kategori default (jika tidak ada yang cocok)", value=ss.fallback).strip() or DEFAULT_FALLBACK
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
include_non_rev = st.sidebar.checkbox(
    "Sertakan Transfer (8370) & Pembayaran (9xxx)", value=False,
    help="Default: dikeluarkan karena bukan revenue (cash, BCA, city ledger, dll).",
)

if not uploaded:
    st.info("Silakan upload file PDF terlebih dahulu.")
    st.stop()

with st.spinner("Membaca PDF..."):
    df, report_totals, grand = parse_pdf(uploaded.getvalue())

if df.empty:
    st.error("Tidak ada transaksi yang terbaca. Pastikan formatnya sama dengan laporan Journal by Cashier and Transaction Code.")
    st.stop()

# ---- Tentukan kategori ----
cat_map = {
    d: get_category(d, rules, fallback, exclude_kws) for d in df["Description"].unique()
}
df["Kategori"] = df["Description"].map(cat_map)
df = df[["Date", "Time", "Room", "Name", "Code", "Description", "Kategori",
         "Currency", "Debit", "Credit", "Net", "Cash ID"]]

# ---- Rekonsiliasi dengan total di laporan ----
# Laporan menjumlahkan nilai desimal asli, sedangkan PDF hanya menampilkan angka bulat,
# jadi selisih beberapa rupiah per kode adalah pembulatan biasa.
TOLERANCE = 50
calc = df.groupby("Code")[["Debit", "Credit"]].sum()
recon = []
for code, (d, c) in report_totals.items():
    cd = int(calc.loc[code, "Debit"]) if code in calc.index else 0
    cc = int(calc.loc[code, "Credit"]) if code in calc.index else 0
    if cd != d or cc != c:
        recon.append({"Code": code, "Debit laporan": d, "Debit hitung": cd,
                      "Credit laporan": c, "Credit hitung": cc,
                      "Selisih": max(abs(cd - d), abs(cc - c))})

# ---- Filter revenue ----
is_excl = df["Kategori"] == EXCLUDED_LABEL          # tax / service charge -> bukan revenue
is_pay = df["Code"].apply(is_non_revenue)           # transfer & pembayaran
work = df[~is_excl] if include_non_rev else df[~is_excl & ~is_pay]
payments = df[is_pay & ~is_excl]
excluded = df[is_excl]

# ---- Ringkasan ----
summary = (
    work.groupby("Kategori")[["Debit", "Credit", "Net"]].sum()
    .sort_values("Net", ascending=False).reset_index()
)
by_code = (
    work.groupby(["Kategori", "Code", "Description"])[["Debit", "Credit", "Net"]]
    .sum().reset_index().sort_values(["Kategori", "Code"])
)

c1, c2, c3 = st.columns(3)
c1.metric("Total Net Revenue", f"Rp {work['Net'].sum():,.0f}")
c2.metric("Jumlah transaksi", f"{len(work):,}")
c3.metric("Jumlah kategori", summary.shape[0])
if not excluded.empty:
    st.caption(
        f"Tidak termasuk revenue: {len(excluded):,} transaksi Tax / Service Charge "
        f"(Net Rp {excluded['Net'].sum():,.0f})."
    )

st.subheader("Ringkasan per Kategori")
fmt = {"Debit": "{:,.0f}", "Credit": "{:,.0f}", "Net": "{:,.0f}"}
left, right = st.columns([3, 2])
left.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)
right.bar_chart(summary.set_index("Kategori")["Net"])

st.subheader("Rincian per Kode Transaksi")
cats = ["Semua"] + summary["Kategori"].tolist()
pick = st.selectbox("Filter kategori", cats)
view = by_code if pick == "Semua" else by_code[by_code["Kategori"] == pick]
st.dataframe(view.style.format(fmt), use_container_width=True, hide_index=True)

with st.expander("Detail seluruh transaksi"):
    detail = work if pick == "Semua" else work[work["Kategori"] == pick]
    st.dataframe(detail.style.format(fmt), use_container_width=True, hide_index=True)

# Bantu menentukan kategori: kode yang jatuh ke kategori default
unassigned = work[work["Kategori"] == fallback]
if not unassigned.empty:
    with st.expander(f"Kode transaksi di kategori default “{fallback}” (bisa Anda kategorikan)"):
        un = unassigned.groupby(["Code", "Description"])[["Debit", "Credit", "Net"]].sum().reset_index()
        st.dataframe(un.style.format(fmt), use_container_width=True, hide_index=True)

if not excluded.empty:
    with st.expander("Tax / Service Charge (tidak masuk revenue)"):
        ex = excluded.groupby(["Code", "Description"])[["Debit", "Credit", "Net"]].sum().reset_index()
        st.dataframe(ex.style.format(fmt), use_container_width=True, hide_index=True)

if not payments.empty:
    with st.expander("Transfer & Pembayaran (tidak masuk revenue)"):
        pay = payments.groupby(["Code", "Description"])[["Debit", "Credit", "Net"]].sum().reset_index()
        st.dataframe(pay.style.format(fmt), use_container_width=True, hide_index=True)

# ---- Validasi (memakai SEMUA transaksi, termasuk tax/service charge) ----
st.subheader("Validasi terhadap total laporan")
if not recon:
    st.success("Semua total per kode transaksi sama persis dengan laporan PDF ✅")
elif max(r["Selisih"] for r in recon) <= TOLERANCE:
    st.success("Total cocok dengan laporan PDF (selisih hanya pembulatan beberapa rupiah) ✅")
    with st.expander("Lihat selisih pembulatan"):
        st.dataframe(pd.DataFrame(recon), use_container_width=True, hide_index=True)
else:
    st.warning("Ada kode transaksi dengan selisih besar, kemungkinan ada baris yang tidak terbaca:")
    st.dataframe(pd.DataFrame(recon), use_container_width=True, hide_index=True)
if grand:
    gd, gc = int(df["Debit"].sum()), int(df["Credit"].sum())
    ok = abs(gd - grand[0]) <= TOLERANCE and abs(gc - grand[1]) <= TOLERANCE
    (st.success if ok else st.warning)(
        f"Grand Total laporan: Debit {grand[0]:,} / Credit {grand[1]:,} — "
        f"hasil hitung: Debit {gd:,} / Credit {gc:,}"
    )

# ---- Download ----
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    summary.to_excel(xw, sheet_name="Ringkasan", index=False)
    by_code.to_excel(xw, sheet_name="Per Kode", index=False)
    work.to_excel(xw, sheet_name="Detail", index=False)
    if not excluded.empty:
        excluded.to_excel(xw, sheet_name="Tax & Service (excluded)", index=False)
st.download_button(
    "⬇️ Download Excel", buf.getvalue(), "ringkasan_revenue.xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
