import io
import json
import re

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Manual Posting Summarizer", page_icon="📊", layout="wide")

# ------------------------------------------------------------------
# 1. KONFIGURASI KATEGORI
# ------------------------------------------------------------------
DEFAULT_RULES = [
    ("Sugar & Spice", "sugar & spice, loyalty"),
    ("Shio", "shio"),
    ("The Lounge", "lobby lounge"),
    ("In-Room Dining", "room service"),
    ("Minibar", "minibar"),
    ("Banquet", "gen banquet"),
    ("Wedding", "wedding"),
    ("Spa", "spa"),
    ("Guest Laundry", "laundry"),
    ("Room Revenue", "accomodation, accommodation, checkout, upselling, check in, show"),
]
DEFAULT_FALLBACK = "Other Revenue"
DEFAULT_EXCLUDE = "tax, service charge, transportation, rounding, bf package, payable others, company, unclaimed"
EXCLUDED_LABEL = "Tax / Service Charge / Transportation / Rounding / bf package / payable others / company / unclaimed"

# Frasa ini dihapus dulu sebelum pengecekan "exclude" (supaya tidak terbuang karena kata 'tax').
KEEP_PHRASES = ["miscellaneous w/ tax"]

STATUS_OK = "Dihitung"
STATUS_EXCL = "Dikecualikan (keyword)"
STATUS_NONREV = "Non-revenue (kode transfer/pembayaran)"


def norm(text):
    """Huruf kecil + rapikan semua jenis spasi (termasuk spasi ganda dan non-breaking space)."""
    return " ".join(str(text).lower().split())


def split_keywords(text):
    return [k for k in (norm(x) for x in str(text).split(",")) if k]


def default_rules_df():
    return pd.DataFrame(DEFAULT_RULES, columns=["Kategori", "Keyword"])


def parse_rules(edf):
    """Tabel editor -> list[(nama, [keyword])]. Urutan baris = prioritas."""
    rules = []
    for _, r in edf.iterrows():
        name = "" if pd.isna(r.get("Kategori")) else str(r["Kategori"]).strip()
        raw = "" if pd.isna(r.get("Keyword")) else str(r["Keyword"])
        kws = split_keywords(raw)
        if name and kws:
            rules.append((name, kws))
    return rules


def kw_in(kw, text, whole_word):
    if not whole_word:
        return kw in text
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text) is not None


def find_excluded(text, exclude_kws):
    """Keyword exclude selalu dicocokkan per KATA utuh ('taxi' tidak dianggap 'tax')."""
    for kw in exclude_kws:
        if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text):
            return kw
    return None


def classify(description, rules, fallback, exclude_kws, whole_word):
    """Return (kategori, keyword_yang_cocok)."""
    desc = norm(description)

    check = desc
    for p in KEEP_PHRASES:
        check = check.replace(p, " ")
    kw = find_excluded(check, exclude_kws)
    if kw:
        return EXCLUDED_LABEL, kw

    for name, kws in rules:
        for k in kws:
            if kw_in(k, desc, whole_word):
                return name, k
    return fallback, ""


def is_non_revenue(code):
    """Transfer (8370) dan semua pembayaran/settlement (9xxx) bukan revenue."""
    c = int(code)
    return c == 8370 or c >= 9000


# ------------------------------------------------------------------
# 2. PARSER PDF
# ------------------------------------------------------------------
# Angka: 1,234 | -1,234 | - 1,234 | (1,234) | 1,234- | 12.50
AMT = r"\(?-?\s?\d[\d,]*(?:\.\d+)?\)?-?"
DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}\b")
HEADER_RE = re.compile(r"^Transaction Code\s+(\d{3,5})\b\s*(.*)$", re.IGNORECASE)
# Di hasil ekstraksi, baris total sering tertulis 'Transaction CodeTotal' (tanpa spasi)
TOTAL_RE = re.compile(rf"^Transaction Code\s*Total\s+({AMT})\s+({AMT})\s*$", re.IGNORECASE)
# Kode transaksi di baris TIDAK dipakai (kadang menempel ke kolom nama: 'LAUN6080'),
# kode diambil dari header 'Transaction Code xxxx'.
ROW_RE = re.compile(
    rf"^\d{{2}}-\d{{2}}-\d{{2,4}}\s+\d{{2}}:\d{{2}}\s+.*?\s(IDR|USD\S*)\s+({AMT})\s+({AMT})(?:\s+\S+)*$"
)


def to_num(s):
    s = s.replace(" ", "")
    neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()-").replace(",", "")
    v = float(s) if s else 0.0
    return -v if neg else v


ROW_COLS = ["Hal", "Code", "Description", "Currency", "Debit", "Credit", "Net", "Teks"]
TOTAL_COLS = ["Hal", "Code", "Debit", "Credit"]
LOG_COLS = ["Hal", "Jenis", "Code", "Teks"]


@st.cache_data(show_spinner=False)
def parse_pdf(data: bytes):
    """
    Baca PDF sekali saja. Return (rows, totals, log):
      rows   : transaksi yang berhasil dibaca
      totals : baris 'Transaction Code Total' yang tercetak di PDF (untuk rekonsiliasi)
      log    : semua baris teks + jenisnya (Header / Baris / Total / GAGAL / Lain), untuk audit
    Baris yang diawali tanggal+jam tapi gagal dibaca ditandai 'GAGAL', bukan dibuang diam-diam.
    """
    rows, totals, log = [], [], []
    cur_code, cur_desc = None, ""

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            for raw in (page.extract_text() or "").splitlines():
                line = " ".join(raw.split())
                if not line:
                    continue

                m = HEADER_RE.match(line)
                if m:
                    cur_code, cur_desc = m.group(1), m.group(2).strip()
                    log.append({"Hal": pno, "Jenis": "Header", "Code": cur_code, "Teks": line})
                    continue

                m = TOTAL_RE.match(line)
                if m:
                    if cur_code:
                        totals.append(
                            {"Hal": pno, "Code": cur_code, "Debit": to_num(m.group(1)), "Credit": to_num(m.group(2))}
                        )
                    log.append({"Hal": pno, "Jenis": "Total", "Code": cur_code, "Teks": line})
                    continue

                if not DATE_RE.match(line):
                    log.append({"Hal": pno, "Jenis": "Lain", "Code": cur_code, "Teks": line})
                    continue

                m = ROW_RE.match(line)
                if m and cur_code:
                    debit, credit = to_num(m.group(2)), to_num(m.group(3))
                    rows.append(
                        {
                            "Hal": pno,
                            "Code": cur_code,
                            "Description": cur_desc,
                            "Currency": m.group(1),
                            "Debit": debit,
                            "Credit": credit,
                            "Net": debit - credit,
                            "Teks": line,
                        }
                    )
                    log.append({"Hal": pno, "Jenis": "Baris", "Code": cur_code, "Teks": line})
                else:
                    log.append({"Hal": pno, "Jenis": "GAGAL", "Code": cur_code, "Teks": line})

    return (
        pd.DataFrame(rows, columns=ROW_COLS),
        pd.DataFrame(totals, columns=TOTAL_COLS),
        pd.DataFrame(log, columns=LOG_COLS),
    )


# ------------------------------------------------------------------
# 3. PERHITUNGAN
# ------------------------------------------------------------------
def build_tables(df, totals, rules, fallback, exclude_kws, whole_word):
    """
    Return (df, per_kode, summary, by_code, audit).
    per_kode : satu baris per kode transaksi (jumlah baris + total PDF + selisih)
    Angka (Debit/Credit/Net) diambil langsung dari baris 'Transaction Code Total' di PDF.
    Kalau sebuah kode tidak punya baris total, terpaksa memakai jumlah baris (ditandai di kolom 'Sumber').
    """
    df = df.copy()
    cls = {d: classify(d, rules, fallback, exclude_kws, whole_word) for d in df["Description"].unique()}
    df["Kategori"] = df["Description"].map(lambda d: cls[d][0])
    df["Keyword"] = df["Description"].map(lambda d: cls[d][1])

    df["Status"] = STATUS_OK
    df.loc[df["Code"].apply(is_non_revenue), "Status"] = STATUS_NONREV
    df.loc[df["Kategori"] == EXCLUDED_LABEL, "Status"] = STATUS_EXCL

    per_kode = (
        df.groupby("Code", sort=True)
        .agg(
            Description=("Description", "first"),
            Status=("Status", "first"),
            Kategori=("Kategori", "first"),
            Keyword=("Keyword", "first"),
            Baris=("Net", "size"),
            D_baris=("Debit", "sum"),
            C_baris=("Credit", "sum"),
        )
        .reset_index()
    )

    pdf_tot = totals.groupby("Code")[["Debit", "Credit"]].sum()
    per_kode["D_pdf"] = per_kode["Code"].map(pdf_tot["Debit"])
    per_kode["C_pdf"] = per_kode["Code"].map(pdf_tot["Credit"])
    has_pdf = per_kode["D_pdf"].notna()

    per_kode["Net baris"] = per_kode["D_baris"] - per_kode["C_baris"]
    per_kode["Net PDF"] = per_kode["D_pdf"] - per_kode["C_pdf"]
    per_kode["Selisih"] = per_kode["Net baris"] - per_kode["Net PDF"]

    per_kode["Sumber"] = has_pdf.map({True: "Total PDF", False: "Jumlah baris"})
    per_kode["Debit"] = per_kode["D_pdf"].where(has_pdf, per_kode["D_baris"])
    per_kode["Credit"] = per_kode["C_pdf"].where(has_pdf, per_kode["C_baris"])
    per_kode["Net"] = per_kode["Debit"] - per_kode["Credit"]

    ok = per_kode[per_kode["Status"] == STATUS_OK]
    summary = (
        ok.groupby("Kategori")[["Debit", "Credit", "Net"]].sum()
        .sort_values("Net", ascending=False).reset_index()
    )
    by_code = ok[["Kategori", "Code", "Description", "Debit", "Credit", "Net"]].sort_values(["Kategori", "Code"])

    audit = per_kode[
        ["Code", "Description", "Status", "Kategori", "Keyword", "Sumber", "Baris", "Net baris", "Net PDF", "Selisih"]
    ].rename(columns={"Keyword": "Keyword cocok"})
    audit["Kategori"] = audit["Kategori"].where(audit["Status"] == STATUS_OK, "-")

    return df, per_kode, summary, by_code, audit


def decimals_for(df):
    """0 desimal kalau semua angka bulat, selain itu 2 desimal."""
    return 0 if ((df[["Debit", "Credit", "Net"]] % 1) == 0).all().all() else 2


# ------------------------------------------------------------------
# 4. UI
# ------------------------------------------------------------------
st.title("📊 Manual Posting Summarizer")
st.caption("Upload PDF *Journal by Cashier and Transaction Code* untuk dijumlahkan per kategori.")

ss = st.session_state
ss.setdefault("rules_df", default_rules_df())
ss.setdefault("fallback", DEFAULT_FALLBACK)
ss.setdefault("exclude_text", DEFAULT_EXCLUDE)
ss.setdefault("whole_word", False)
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
    fallback = (
        c_a.text_input(
            "Kategori default (jika tidak ada yang cocok)", value=ss.fallback, key=f"fb_{ss.ver}"
        ).strip()
        or DEFAULT_FALLBACK
    )
    exclude_text = c_b.text_input(
        "Dikecualikan dari revenue (kata kunci)",
        value=ss.exclude_text,
        key=f"ex_{ss.ver}",
        help="Deskripsi yang mengandung kata ini (kata utuh) tidak dihitung sebagai revenue.",
    )
    whole_word = st.checkbox(
        "Cocokkan keyword kategori sebagai kata utuh (mis. 'spa' tidak cocok dengan 'space')",
        value=ss.whole_word,
        key=f"ww_{ss.ver}",
    )

    b1, b2, b3 = st.columns(3)
    cfg_json = json.dumps(
        {
            "rules": [{"Kategori": n, "Keyword": ", ".join(k)} for n, k in parse_rules(edited)],
            "fallback": fallback,
            "exclude": exclude_text,
            "whole_word": whole_word,
        },
        ensure_ascii=False,
        indent=2,
    )
    b1.download_button("💾 Simpan pengaturan (JSON)", cfg_json, "kategori_config.json", "application/json")
    if b2.button("↩️ Kembalikan ke default"):
        ss.rules_df, ss.fallback, ss.exclude_text = default_rules_df(), DEFAULT_FALLBACK, DEFAULT_EXCLUDE
        ss.whole_word = False
        ss.ver += 1
        st.rerun()
    cfg_file = b3.file_uploader(
        "Muat pengaturan (JSON)", type="json", key=f"cfg_up_{ss.ver}", label_visibility="collapsed"
    )
    if cfg_file is not None:
        try:
            cfg = json.load(cfg_file)
            ss.rules_df = pd.DataFrame(cfg["rules"], columns=["Kategori", "Keyword"])
            ss.fallback = cfg.get("fallback", DEFAULT_FALLBACK)
            ss.exclude_text = cfg.get("exclude", DEFAULT_EXCLUDE)
            ss.whole_word = bool(cfg.get("whole_word", False))
            ss.ver += 1
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"File pengaturan tidak valid: {e}")

rules = parse_rules(edited)
exclude_kws = split_keywords(exclude_text)

uploaded = st.file_uploader("Upload PDF", type="pdf")

if not uploaded:
    ss.processed = False  # Reset jika file dihapus
    st.info("Silakan upload file PDF terlebih dahulu.")
    st.stop()

# Tombol untuk memproses PDF
if st.button("🚀 Proses PDF", type="primary", use_container_width=True):
    ss.processed = True

# Jika belum diklik, hentikan eksekusi di sini
if not ss.processed:
    st.warning("Silakan klik tombol **'Proses PDF'** di atas untuk mulai menganalisis dokumen.")
    st.stop()

# ------------------------------------------------------------------
# PROSES UTAMA (Eksekusi setelah tombol diklik)
# ------------------------------------------------------------------
with st.spinner("Membaca dan memproses PDF..."):
    df, totals, log = parse_pdf(uploaded.getvalue())

if df.empty:
    st.error(
        "Tidak ada transaksi yang terbaca. Pastikan formatnya sama dengan laporan "
        "Journal by Cashier and Transaction Code."
    )
    with st.expander("Teks mentah yang terbaca dari PDF (300 baris pertama)"):
        st.dataframe(log.head(300), use_container_width=True, hide_index=True)
    st.stop()

df, per_kode, summary, by_code, audit = build_tables(
    df, totals, rules, fallback, exclude_kws, whole_word
)
work = df[df["Status"] == STATUS_OK]
dec = decimals_for(df)
num_fmt = f"{{:,.{dec}f}}"
fmt = {c: num_fmt for c in ["Debit", "Credit", "Net"]}
fmt_audit = {c: num_fmt for c in ["Net baris", "Net PDF", "Selisih"]}

# ---- Ringkasan status pembacaan ----
n_gagal = int((log["Jenis"] == "GAGAL").sum())
tanpa_total = per_kode[per_kode["Sumber"] == "Jumlah baris"]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Baris terbaca", f"{len(df):,}")
m2.metric("Baris GAGAL terbaca", f"{n_gagal:,}")
m3.metric("Kode tanpa total di PDF", f"{len(tanpa_total):,}")
m4.metric("Total Net revenue", f"{summary['Net'].sum():,.{dec}f}")

if n_gagal:
    st.warning(
        f"{n_gagal} baris transaksi tidak cocok dengan format yang diharapkan dan **tidak ikut dihitung**. "
        "Lihat tab *Audit & Diagnostik*."
    )
if len(tanpa_total):
    st.warning(
        "Kode " + ", ".join(tanpa_total["Code"].tolist()) + " tidak punya baris 'Transaction Code Total' di PDF, "
        "jadi angkanya terpaksa dijumlahkan dari baris."
    )
if work.empty:
    st.warning("Tidak ada transaksi revenue setelah Tax, Service Charge, Transfer, dan Pembayaran dikeluarkan.")

tab_sum, tab_code, tab_detail, tab_audit = st.tabs(
    ["Ringkasan per Kategori", "Rincian per Kode", "Baris detail", "🔎 Audit & Diagnostik"]
)

with tab_sum:
    st.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)

with tab_code:
    pick = st.selectbox("Filter kategori", ["Semua"] + summary["Kategori"].tolist())
    view = by_code if pick == "Semua" else by_code[by_code["Kategori"] == pick]
    st.dataframe(view.style.format(fmt), use_container_width=True, hide_index=True)

with tab_detail:
    st.caption(
        "Setiap baris transaksi yang dibaca beserta teks aslinya, untuk dicocokkan dengan PDF. "
        "Tab ini hanya untuk pengecekan baris. Angka di Ringkasan memakai Total PDF."
    )
    k = st.selectbox("Kategori", ["Semua"] + sorted(work["Kategori"].unique()), key="det_kat")
    sub = work if k == "Semua" else work[work["Kategori"] == k]
    c = st.selectbox("Kode transaksi", ["Semua"] + sorted(sub["Code"].unique()), key="det_code")
    if c != "Semua":
        sub = sub[sub["Code"] == c]
    st.caption(f"{len(sub):,} baris | Total Net (jumlah baris): {sub['Net'].sum():,.{dec}f}")
    st.dataframe(
        sub[["Hal", "Code", "Description", "Debit", "Credit", "Net", "Teks"]].style.format(fmt),
        use_container_width=True,
        hide_index=True,
    )

with tab_audit:
    st.caption(
        "Semua kode transaksi yang terbaca, termasuk yang TIDAK dihitung, lengkap dengan alasannya. "
        "Kalau ada transaksi yang 'hilang', cari di sini."
    )

    selisih_kode = audit[audit["Selisih"].fillna(0).abs() > 0.005]
    st.markdown(f"**Kode yang jumlah barisnya berbeda dengan total PDF (informasi): {len(selisih_kode)}**")
    if selisih_kode.empty:
        st.success("Jumlah baris cocok dengan total PDF di semua kode.")
    else:
        st.caption("Aplikasi memakai Total PDF. Selisih ini biasanya pembulatan di sistem sumber.")
        st.dataframe(selisih_kode.style.format(fmt_audit, na_rep="-"), use_container_width=True, hide_index=True)

    q = norm(st.text_input("Cari kata (mis. banquet)", key="audit_q"))
    all_status = [STATUS_OK, STATUS_EXCL, STATUS_NONREV]
    status_pick = st.multiselect("Status", all_status, default=all_status)

    view_a = audit[audit["Status"].isin(status_pick)]
    if q:
        hit = view_a["Description"].str.lower().str.contains(q, regex=False) | view_a["Code"].str.contains(
            q, regex=False
        )
        view_a = view_a[hit]
    st.markdown("**Status tiap kode transaksi**")
    st.dataframe(view_a.style.format(fmt_audit, na_rep="-"), use_container_width=True, hide_index=True)

    gagal = log[log["Jenis"] == "GAGAL"]
    st.markdown(f"**Baris gagal terbaca: {len(gagal)}**")
    if gagal.empty:
        st.success("Semua baris yang diawali tanggal+jam berhasil dibaca.")
    else:
        st.dataframe(gagal[["Hal", "Code", "Teks"]], use_container_width=True, hide_index=True)

    hdr = log[log["Jenis"] == "Header"]
    tanpa_baris = hdr[~hdr["Code"].isin(df["Code"].unique())]
    if not tanpa_baris.empty:
        st.markdown("**Header kode transaksi tanpa satu pun baris terbaca**")
        st.dataframe(tanpa_baris[["Hal", "Code", "Teks"]], use_container_width=True, hide_index=True)

    if q:
        st.markdown(f"**Teks mentah PDF yang mengandung '{q}'**")
        raw_hit = log[log["Teks"].str.lower().str.contains(q, regex=False)]
        st.dataframe(raw_hit, use_container_width=True, hide_index=True)

# ---- Download ----
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    summary.to_excel(xw, sheet_name="Ringkasan", index=False)
    by_code.to_excel(xw, sheet_name="Per Kode", index=False)
    audit.to_excel(xw, sheet_name="Audit", index=False)
    if n_gagal:
        log[log["Jenis"] == "GAGAL"].to_excel(xw, sheet_name="Gagal Terbaca", index=False)
st.download_button(
    "⬇️ Download Excel", buf.getvalue(), "ringkasan_revenue.xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
