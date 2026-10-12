"""
Journal Revenue Summarizer
Upload PDF "Journal by Cashier and Transaction Code" (Opera/PMS) -> ringkasan per kategori.
Jalankan lokal:  streamlit run app.py
"""
import io
import re

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Journal Revenue Summarizer", page_icon="📊", layout="wide")


# ------------------------------------------------------------------
# 1. KATEGORI (sesuai fungsi Anda, tidak diubah)
# ------------------------------------------------------------------
def get_category(description):
    desc_lower = description.lower()
    if any(
        kw in desc_lower
        for kw in ["sugar & spice", "loyalty reward adjustment", "bf package"]
    ):
        return "Sugar & Spice"
    elif "shio" in desc_lower:
        return "Shio"
    elif "lobby lounge" in desc_lower:
        return "The Lounge"
    elif "room service" in desc_lower:
        return "In-Room Dining"
    elif "minibar" in desc_lower:
        return "Minibar"
    elif "banquet" in desc_lower:
        return "Banquet"
    elif "wedding" in desc_lower:
        return "Wedding"
    elif "spa" in desc_lower:
        return "Spa"
    elif "laundry" in desc_lower:
        return "Guest Laundry"
    elif any(
        kw in desc_lower
        for kw in ["accomodation", "checkout", "upselling", "check in", "show"]
    ):
        return "Room Revenue"
    else:
        return "Other Revenue"


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


def parse_pdf(file):
    rows, report_totals, grand = [], {}, None
    cur_code, cur_desc = None, None

    with pdfplumber.open(file) as pdf:
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
                            "Kategori": get_category(cur_desc),
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

uploaded = st.file_uploader("Upload PDF", type="pdf")
include_non_rev = st.sidebar.checkbox(
    "Sertakan Transfer (8370) & Pembayaran (9xxx)", value=False,
    help="Default: dikeluarkan karena bukan revenue (cash, BCA, city ledger, dll).",
)

if not uploaded:
    st.info("Silakan upload file PDF terlebih dahulu.")
    st.stop()

with st.spinner("Membaca PDF..."):
    df, report_totals, grand = parse_pdf(uploaded)

if df.empty:
    st.error("Tidak ada transaksi yang terbaca. Pastikan formatnya sama dengan laporan Journal by Cashier and Transaction Code.")
    st.stop()

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
work = df if include_non_rev else df[~df["Code"].apply(is_non_revenue)]
payments = df[df["Code"].apply(is_non_revenue)]

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

if not payments.empty:
    with st.expander("Transfer & Pembayaran (tidak masuk revenue)"):
        pay = payments.groupby(["Code", "Description"])[["Debit", "Credit", "Net"]].sum().reset_index()
        st.dataframe(pay.style.format(fmt), use_container_width=True, hide_index=True)

# ---- Validasi ----
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
    summary.to_excel(xw, "Ringkasan", index=False)
    by_code.to_excel(xw, "Per Kode", index=False)
    work.to_excel(xw, "Detail", index=False)
st.download_button(
    "⬇️ Download Excel", buf.getvalue(), "ringkasan_revenue.xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
