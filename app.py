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
    ("Banquet", "banquet"),
    ("Wedding", "wedding"),
    ("Spa", "spa"),
    ("Guest Laundry", "laundry"),
    ("Room Revenue", "accomodation, accommodation, checkout, upselling, check in, show"),
]
DEFAULT_FALLBACK = "Other Revenue"
DEFAULT_EXCLUDE = "tax, service charge, transportation, rounding, bf package, payable others, company, tips"
EXCLUDED_LABEL = "Tax / Service Charge / Transportation / Rounding / bf package / payable others / company / tips"

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
ROW_RE = re.compile(
    rf"^\d{{2}}-\d{{2}}-\d{{2,4}}\s+\d{{2}}:\d{{2}}\s+.*?\s(IDR|USD\S*)\s+({AMT})\s+({AMT})(?:\s+\S+)*$"
)


def to_num(s):
    s = s.replace(" ", "")
    neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()-").replace(",", "")
    v = float(s) if s else 0.0
    return -v if neg else v


ROW_COLS = ["Hal", "Code", "Description", "Currency", "Debit", "Credit", "Net"]
LOG_COLS = ["Hal", "Jenis", "Code", "Teks"]


@st.cache_data(show_spinner=False)
def parse_pdf(data: bytes):
    """
    Baca PDF sekali saja. Return:
      rows : transaksi yang berhasil dibaca
      log  : semua baris teks + jenisnya (Header / Baris / GAGAL / Lain), untuk audit
    Baris yang diawali tanggal+jam tapi gagal dibaca ditandai 'GAGAL', bukan dibuang diam-diam.
    """
    rows, log = [], []
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
                        }
                    )
                    log.append({"Hal": pno, "Jenis": "Baris", "Code": cur_code, "Teks": line})
                else:
                    log.append({"Hal": pno, "Jenis": "GAGAL", "Code": cur_code, "Teks": line})

    return pd.DataFrame(rows, columns=ROW_COLS), pd.DataFrame(log, columns=LOG_COLS)


def make_fmt(frame):
    """Tampilkan tanpa desimal kalau semua angka bulat, selain itu 2 desimal."""
    cols = ["Debit", "Credit", "Net"]
    dec = 0 if ((frame[cols] % 1) == 0).all().all() else 2
    return dec, {c: f"{{:,.{dec}f}}" for c in cols}


# ------------------------------------------------------------------
# 3. UI
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
    st.info("Silakan upload file PDF terlebih dahulu.")
    st.stop()

with st.spinner("Membaca PDF..."):
    df, log = parse_pdf(uploaded.getvalue())

if df.empty:
    st.error(
        "Tidak ada transaksi yang terbaca. Pastikan formatnya sama dengan laporan "
        "Journal by Cashier and Transaction Code."
    )
    with st.expander("Teks mentah yang terbaca dari PDF (300 baris pertama)"):
        st.dataframe(log.head(300), use_container_width=True, hide_index=True)
    st.stop()

# ---- Kategori & status tiap baris ----
cls = {d: classify(d, rules, fallback, exclude_kws, whole_word) for d in df["Description"].unique()}
df["Kategori"] = df["Description"].map(lambda d: cls[d][0])
df["Keyword"] = df["Description"].map(lambda d: cls[d][1])

df["Status"] = STATUS_OK
df.loc[df["Code"].apply(is_non_revenue), "Status"] = STATUS_NONREV
df.loc[df["Kategori"] == EXCLUDED_LABEL, "Status"] = STATUS_EXCL

work = df[df["Status"] == STATUS_OK]
dec, fmt = make_fmt(df)

# ---- Ringkasan status pembacaan ----
n_gagal = int((log["Jenis"] == "GAGAL").sum())
m1, m2, m3, m4 = st.columns(4)
m1.metric("Kode transaksi (header)", f"{df['Code'].nunique():,}")
m2.metric("Baris terbaca", f"{len(df):,}")
m3.metric("Baris GAGAL terbaca", f"{n_gagal:,}")
m4.metric("Total Net revenue", f"{work['Net'].sum():,.{dec}f}")

if n_gagal:
    st.warning(
        f"{n_gagal} baris transaksi tidak cocok dengan format yang diharapkan dan **tidak ikut dihitung**. "
        "Lihat tab *Audit & Diagnostik*."
    )
if (df["Currency"] != "IDR").any():
    st.warning("Ada baris non-IDR (mis. USD). Angkanya dijumlahkan apa adanya, tanpa konversi kurs.")
if work.empty:
    st.warning("Tidak ada transaksi revenue setelah Tax, Service Charge, Transfer, dan Pembayaran dikeluarkan.")

# ---- Tabel hasil ----
summary = (
    work.groupby("Kategori")[["Debit", "Credit", "Net"]].sum()
    .sort_values("Net", ascending=False).reset_index()
)
by_code = (
    work.groupby(["Kategori", "Code", "Description"])[["Debit", "Credit", "Net"]]
    .sum().reset_index().sort_values(["Kategori", "Code"])
)
audit = (
    df.groupby(["Code", "Description", "Status", "Kategori", "Keyword"], dropna=False)
    .agg(Baris=("Net", "size"), Debit=("Debit", "sum"), Credit=("Credit", "sum"), Net=("Net", "sum"))
    .reset_index().sort_values("Code")
)
audit["Kategori"] = audit["Kategori"].where(audit["Status"] == STATUS_OK, "-")
audit = audit.rename(columns={"Keyword": "Keyword cocok"})

tab_sum, tab_code, tab_audit = st.tabs(["Ringkasan per Kategori", "Rincian per Kode", "🔎 Audit & Diagnostik"])

with tab_sum:
    st.dataframe(summary.style.format(fmt), use_container_width=True, hide_index=True)

with tab_code:
    pick = st.selectbox("Filter kategori", ["Semua"] + summary["Kategori"].tolist())
    view = by_code if pick == "Semua" else by_code[by_code["Kategori"] == pick]
    st.dataframe(view.style.format(fmt), use_container_width=True, hide_index=True)

with tab_audit:
    st.caption(
        "Semua kode transaksi yang terbaca, termasuk yang TIDAK dihitung, lengkap dengan alasannya. "
        "Kalau ada transaksi yang 'hilang', cari di sini."
    )
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
    st.dataframe(view_a.style.format(fmt), use_container_width=True, hide_index=True)

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
